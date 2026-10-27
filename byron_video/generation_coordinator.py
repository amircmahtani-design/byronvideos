"""Original-input worker orchestration. Disabled, once-only paid operations, no polling loop.

A pass advances fresh original images through native narration and bounded motion
into a prepared render handoff IN THE SAME execution. Missing human review is a
real HOLD, never an invented approval or a provider resubmission.
"""
from __future__ import annotations
import copy
import hashlib
import math
import tempfile
import time
from pathlib import Path
from .elevenlabs_adapter import fingerprint
from .cloud_render import require
from .cloud_journal import _FirestoreBoundary
from .hosted_control import canonical
from .motion_originals import plan_motion, validate_timing_plan
from .motion_continuation import (plan_continuation, bind_plan_budget,
    submit_initial_image_once, submit_extension_once, scene_prompt)
from .provider_execution import plan_anchored_motion, ProviderOutcomeUnknown
from .continuation_worker import stage_committed_continuation, persist_prepared_continuation


class FirestoreGenerationPlan(_FirestoreBoundary):
    """Immutable complete funded plan; repeated passes load, never re-price it."""
    def _ref(self,job):
        return self._doc(self._owner(job.owner_id)+'/generation_plans/'+job.id)
    def load(self,job):
        value=self._read(self._ref(job))
        if value is None:return None
        require(value['owner_id']==job.owner_id and value['job_id']==job.id
                and value['snapshot_sha256']==job.snapshot_sha256,'Generation plan owner/snapshot changed.')
        plan=value['plan']
        require(fingerprint(plan)==value['plan_sha256'],'Stored generation plan changed.')
        return copy.deepcopy(plan)
    def commit(self,job,plan):
        require(plan['owner_id']==job.owner_id and plan['job_id']==job.id,'Generation plan identity differs.')
        payload={'owner_id':job.owner_id,'job_id':job.id,'snapshot_sha256':job.snapshot_sha256,
                 'plan':copy.deepcopy(plan),'plan_sha256':fingerprint(plan)}
        require(len(canonical(payload))<700_000,'Generation plan exceeds journal bound.')
        def transaction(tx):
            old=self._read(self._ref(job),tx)
            if old is not None:
                require(old==payload,'Generation plan is immutable; no silent re-price/re-plan.')
                return False
            tx.create(self._ref(job),payload);return True
        self._run(transaction)
        return copy.deepcopy(plan)


def narration_timing(job,report,source_reviews):
    """Derive ordered coverage from actual native word times, no audio estimate.

    Paragraph boundaries are preferred when they match source count; otherwise
    nearest sentence ends to equal-duration targets are used, with bounded gaps.
    Crossfade coverage follows the recovered compositor's symmetric overlaps.
    """
    frozen=job.snapshot;images=frozen['images'];text=frozen['narrated_text']
    require(report.get('exact_text')==text and report.get('profile_sha256')==fingerprint(frozen['processing_plan']['narration_profile'])
            and report.get('media_verified') is True,'Timing requires exact decoded selected-voice narration.')
    duration=report['duration_seconds'];words=report['words'];count=len(images)
    require(type(duration) in (int,float) and math.isfinite(duration) and 0<duration<=900 and count in (4,5),'Invalid narration coverage.')
    require(isinstance(words,list) and words and all(isinstance(w,dict) and type(w.get('start')) in (int,float)
        and type(w.get('end')) in (int,float) and math.isfinite(w['start']) and math.isfinite(w['end'])
        and 0<=w['start']<w['end']<=duration for w in words),'Native word timings required.')
    require(isinstance(source_reviews,dict) and set(source_reviews)==set(frozen['input_order']),'Source-visible reviews missing.')
    import re
    paragraph_starts=[m.end() for m in re.finditer(r'\n\s*\n',text)]
    paragraph_times=[0.0]+[next((w['start'] for w in words if w.get('char_start',-1)>=start),duration) for start in paragraph_starts]
    if len(paragraph_times)==count:
        bounds=paragraph_times+[duration]
    else:
        candidates=sorted({w['end'] for w in words if re.search(r'[.!?;]$',w.get('text',''))})
        bounds=[0.0];minimum=min(3.0,duration/(count*2))
        for index in range(1,count):
            target=duration*index/count
            options=[t for t in candidates if bounds[-1]+minimum<=t<=duration-(count-index)*minimum
                     and abs(t-target)<=duration/count*.35]
            bounds.append(min(options,key=lambda t:abs(t-target)) if options else target)
        bounds.append(duration)
    scenes=[];sources=[]
    for i,image in enumerate(images):
        start=0 if i==0 else bounds[i]-.35
        end=duration if i==count-1 else bounds[i+1]+.35
        scenes.append({'image_id':image['id'],'start_seconds':start,'end_seconds':end})
        sources.append({'id':image['id'],'position':i+1,'source_sha256':image['sha256'],
                        'duration_seconds':end-start,'source_review':copy.deepcopy(source_reviews[image['id']])})
    timing={'schema_version':1,'source':'exact_narration_word_times','narration_text_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'audio_sha256':report['audio_sha256'],'word_timing_sha256':fingerprint(words),
        'duration_seconds':duration,'crossfade_seconds':.7,'scenes':scenes}
    validate_timing_plan(timing,sources,timing['narration_text_sha256'])
    return timing,sources


def load_preview_context(job,authorization_loader,budget_loader,*,now=None):
    """Independent private approval, never a browser-provided mode/approval flag."""
    intent=job.snapshot.get('processing_plan',{}).get('preview_contract')
    if intent is None:return None
    from .private_preview import validate_intent,validate_authorization
    intent=validate_intent(intent)
    require(callable(authorization_loader) and callable(budget_loader),'Explicit job-bound private preview authority and budget loaders required.')
    value=authorization_loader(job)
    if isinstance(value,tuple) and len(value)==2:auth,expected=value
    else:
        require(isinstance(value,dict) and set(value)=={'authorization','expected_authorization_sha256'},'Independent preview authorization record required.')
        auth,expected=value['authorization'],value['expected_authorization_sha256']
    budget=budget_loader(job)
    checked=validate_authorization(auth,expected,job=job,budget=budget,preview_release_sha256=intent['preview_release_sha256'],now=now)
    return dict(authorization=checked,expected_authorization_sha256=expected,budget=copy.deepcopy(budget),preview_release_sha256=intent['preview_release_sha256'])


def preview_timing(job,report,source_candidate):
    """Use the candidate's exact unchanged story spans and native word timings.

    No timing estimate or word redistribution replaces the narrator. The overlap
    is the existing symmetric 0.7 seconds; excessive/ambiguous coverage fails.
    """
    from .source_planning import validate_candidate_result
    validate_candidate_result(source_candidate,job=job,narration_report=report)
    words=report.get('words');duration=report.get('duration_seconds');text=job.snapshot['narrated_text']
    require(type(duration) in (int,float) and math.isfinite(duration) and 0<duration<=900
            and isinstance(words,list) and words,'Exact native narration timing required for private preview.')
    previous=-1
    for word in words:
        require(isinstance(word,dict) and type(word.get('char_start')) is int and type(word.get('char_end')) is int
                and previous<=word['char_start']<word['char_end']<=len(text)
                and all(type(word.get(k)) in (int,float) and math.isfinite(word[k]) for k in ('start','end'))
                and 0<=word['start']<word['end']<=duration,'Native word span/timing changed.')
        previous=word['char_end']
    spans=source_candidate['proposals'];bounds=[0.]
    for scene in spans[1:]:
        boundary=scene['story_start']
        require(not any(w['char_start']<boundary<w['char_end'] for w in words),'Preview scene cannot split a spoken word.')
        upcoming=[w for w in words if w['char_start']>=boundary]
        require(upcoming,'Preview scene omitted native spoken words.')
        bounds.append(upcoming[0]['start'])
    bounds.append(duration)
    require(all(b-a>.7 for a,b in zip(bounds,bounds[1:])),'Preview story spans lack safe native overlap coverage.')
    scenes=[];sources=[]
    for i,image in enumerate(job.snapshot['images']):
        start=0 if i==0 else bounds[i]-.35
        end=duration if i==len(spans)-1 else bounds[i+1]+.35
        scenes.append(dict(image_id=image['id'],start_seconds=start,end_seconds=end))
        sources.append(dict(id=image['id'],duration_seconds=end-start))
    timing=dict(schema_version=1,source='exact_narration_word_times',narration_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        audio_sha256=report['audio_sha256'],word_timing_sha256=fingerprint(words),duration_seconds=duration,crossfade_seconds=.7,scenes=scenes)
    validate_timing_plan(timing,sources,timing['narration_text_sha256'])
    return timing


class GenerationCoordinator:
    """Concrete phase ordering using native adapters; one bounded status pass.

    policy_loader(job, motion_plan_or_None) independently supplies source_reviews,
    exact approved_prompts, current quotes and campaign budget. It MUST NOT turn
    generated proposals into approval. Missing policy returns REVIEW_REQUIRED.
    narration_stage(job,root) publishes native narration once and returns report,
    report_sha256,narration_ref. originals(job,image) returns exact private bytes
    and content_type. capture_segment is the concrete segment_capture function.
    """
    def __init__(self,*,narration_stage,policy_loader,originals,plan_store,spend_journal,
                 transport,segment_store,review_loader,reader,input_store,evidence_store,
                 handoff_registry,target,activation_check,capture_segment,enabled=False,now=None,
                 poll_timeout_seconds=0,poll_interval_seconds=5,max_poll_observations=120,clock=time.monotonic,sleep=time.sleep,crop_resolver=None,review_funding=None,source_planner=None,preview_authorization_loader=None,preview_budget_loader=None,preview_quote_loader=None):
        require(type(poll_timeout_seconds) in (int,float) and 0<=poll_timeout_seconds<=1800
                and type(poll_interval_seconds) in (int,float) and 0<poll_interval_seconds<=30
                and type(max_poll_observations) is int and 1<=max_poll_observations<=360,'Bounded queue polling settings required.')
        self.__dict__.update(locals());self.__dict__.pop('self')
    def _check(self):
        require(self.enabled is True,'Original-input generation remains disabled.')
        require(callable(self.activation_check) and self.activation_check() is True,'Generation activation is absent/expired.')
        if getattr(self,'_preview_job',None) is not None:
            load_preview_context(self._preview_job,self.preview_authorization_loader,self.preview_budget_loader,now=self.now)
    def run(self,job,root):
        self._job=job
        self._preview_job=job if job.snapshot.get('processing_plan',{}).get('preview_contract') is not None else None
        self._check()
        preview=load_preview_context(job,self.preview_authorization_loader,self.preview_budget_loader,now=self.now)
        if preview is not None:
            from .source_planning import SourcePlanningCandidate
            from .vertex_review import NativeVertexHTTP
            from .funded_review import FundedCandidateReview
            require(type(self.source_planner) is SourcePlanningCandidate and type(self.source_planner.transport) is NativeVertexHTTP
                    and type(self.review_loader) is FundedCandidateReview and callable(self.review_funding)
                    and callable(self.preview_quote_loader),'Private preview requires concrete native planning, funded native review and exact quotes.')
        deadline=self.clock()+self.poll_timeout_seconds;observations=0
        require(fingerprint(job.snapshot)==job.snapshot_sha256,'Frozen original job changed.')
        existing=self.handoff_registry.get(job)
        if existing is not None:
            from .cloud_render import validate_manifest,build_execution_request
            manifest,reference=existing
            trusted=dict(job_id=job.id,owner_id=job.owner_id,snapshot_sha256=job.snapshot_sha256,frozen_job=job.snapshot)
            validate_manifest(manifest,self.target,trusted_job=trusted)
            build_execution_request(self.target,manifest,reference,trusted_job=trusted)
            return {'state':'PREVIEW_PREPARED' if preview is not None else 'PREPARED','handoff':{'manifest':manifest,'reference':reference},'automatic_retries':0,'new_execution_required':False}
        root=Path(root)
        require(root.is_absolute() and not any(p.is_symlink() for p in (root,*root.parents)),'Private generation workspace required.')
        root.mkdir(parents=True,exist_ok=True,mode=0o700);root.chmod(0o700)
        # Validate ALL exact anchors before any paid narration/motion operation.
        # Inline provider transport caps originals at4MiB; never silently resize.
        from .hosted_uploads import prepare_image
        originals={}
        for image in job.snapshot['images']:
            self._check();original=self.originals(job,image)
            require(isinstance(original,dict) and isinstance(original.get('bytes'),bytes)
                    and 0<len(original['bytes'])<=4*1024*1024
                    and hashlib.sha256(original['bytes']).hexdigest()==image['sha256'],
                    'Exact original exceeds the provider anchor limit or changed; no paid operation was attempted.')
            prepare_image(original['bytes'],original['content_type']);originals[image['id']]=original
        if self.source_planner is not None:
            preflight=getattr(self.source_planner,'preflight',None)
            require(callable(preflight),'Source planner must validate its complete original-input envelope before narration.')
            self._check();preflight(job=job,originals=copy.deepcopy(originals))
            require(fingerprint(job.snapshot)==job.snapshot_sha256,'Source planning preflight changed frozen job.')
        self._check()
        try:native=self.narration_stage(job,root/'narration')
        except ProviderOutcomeUnknown:return {'state':'HOLD','phase':'narration_outcome_unknown','automatic_retries':0}
        report=native['report'];report_sha=native['report_sha256']
        require(fingerprint(report)==report_sha,'Native narration report changed.')
        plan=self.plan_store.load(job)
        if plan is None and preview is not None:
            self._check()
            try:proposed=self.source_planner(job=job,originals=copy.deepcopy(originals),narration_report=copy.deepcopy(report),root=root/'source-planning')
            except ProviderOutcomeUnknown:return {'state':'HOLD','phase':'source_planning_outcome_unknown','automatic_retries':0}
            from .source_planning import validate_candidate_result
            validate_candidate_result(proposed,job=job,narration_report=report)
            if proposed['unresolved_uncertainty'] or proposed['diversity_state']!='varied_proposal':
                return {'state':'REVIEW_REQUIRED','phase':'source_planning_uncertainty','candidate':proposed,'automatic_retries':0}
            timing=preview_timing(job,report,proposed)
            quotes=self.preview_quote_loader(job,proposed,timing)
            require(isinstance(quotes,dict) and set(quotes)=={'image_quote','extension_quote'},'Independent complete preview motion quotes required.')
            from .private_preview import plan_private_preview_continuation
            self._check()
            plan=plan_private_preview_continuation(job=job,source_candidate=proposed,timing=timing,narration_report=report,
                expected_report_sha256=report_sha,image_quote=quotes['image_quote'],extension_quote=quotes['extension_quote'],now=self.now,**preview)
            plan=self.plan_store.commit(job,plan)
        elif plan is None:
            policy=self.policy_loader(job,None)
            if not policy or not policy.get('source_reviews'):
                if self.source_planner is not None:
                    require(callable(self.source_planner),'Explicit private source-planning adapter required.')
                    self._check()
                    try:
                        proposed=self.source_planner(job=job,originals=copy.deepcopy(originals),
                            narration_report=copy.deepcopy(report),root=root/'source-planning')
                    except ProviderOutcomeUnknown:
                        return {'state':'HOLD','phase':'source_planning_outcome_unknown','automatic_retries':0}
                    require(fingerprint(job.snapshot)==job.snapshot_sha256 and fingerprint(report)==report_sha,
                            'Source planner changed immutable story/narration.')
                    from .source_planning import validate_candidate_result
                    validate_candidate_result(proposed,job=job,narration_report=report)
                    return {'state':'REVIEW_REQUIRED','phase':'source_planning_candidate',
                            'candidate':proposed,'automatic_retries':0,'human_approved':False,'live_release':False}
                return {'state':'REVIEW_REQUIRED','phase':'source_review','automatic_retries':0}
            timing,sources=narration_timing(job,report,policy['source_reviews'])
            motion=plan_motion(sources,timing['narration_text_sha256'],timing_plan=timing)
            self._check();approved=self.policy_loader(job,motion)
            if not approved or not approved.get('approved_prompts'):
                return {'state':'REVIEW_REQUIRED','phase':'prompt_review','motion_plan':motion,'automatic_retries':0}
            plan=plan_continuation(owner_id=job.owner_id,job_id=job.id,images=job.snapshot['images'],motion_plan=motion,
                timing=timing,narration_report=report,expected_narration_report_sha256=report_sha,
                approved_prompts=approved['approved_prompts'],image_quote=approved['image_quote'],
                extension_quote=approved['extension_quote'],budget=approved['budget'],now=self.now)
            self._check();plan=self.plan_store.commit(job,plan)
        require(plan['narration_report_sha256']==report_sha,'Saved plan differs from native narration.')
        plan_sha=fingerprint(plan)
        if preview is not None:
            require(plan.get('preview_authorization_sha256')==preview['expected_authorization_sha256']
                    and plan.get('snapshot_sha256')==job.snapshot_sha256,'Saved preview plan differs from independently loaded job authorization.')
            self.verify_preview_source(plan['source_candidate'],report)
        else:require('preview_schema' not in plan,'A historical job cannot acquire preview mode from stored state.')
        # Optional automated-review candidates must fund every planned clip before
        # any new motion POST. Historical human-review loaders remain unchanged.
        if getattr(self.review_loader,'requires_film_review_funding',False):
            require(callable(self.review_funding),'Automated review requires complete film funding before motion.')
        if self.review_funding is not None:
            require(callable(self.review_funding),'Explicit private review funding adapter required.')
            self._check()
            funded=self.review_funding(job=job,plan=copy.deepcopy(plan),journal=self.spend_journal)
            require(fingerprint(plan)==plan_sha and fingerprint(job.snapshot)==job.snapshot_sha256,
                    'Review funding changed the immutable generation scope.')
            from .review_budget import validate_funding
            validate_funding(funded,job=job,continuation_plan=plan,journal=self.spend_journal)
            binder=getattr(self.review_loader,'bind_funding',None)
            if getattr(self.review_loader,'requires_film_review_funding',False):
                require(callable(binder),'Fresh automated review requires its exact funded journal binding.')
            if binder is not None:
                require(callable(binder),'Invalid review funding binder.')
                binder(funded,job=job,plan=copy.deepcopy(plan))
        self._check();bind_plan_budget(plan=plan,expected_plan_sha256=plan_sha,journal=self.spend_journal,now=self.now)
        try:
            for scene in plan['scenes']:
                previous=None
                for segment in scene['segments']:
                    self._check();sequence=segment['sequence'];image_id=scene['image_id']
                    if observations>=self.max_poll_observations or (self.poll_timeout_seconds>0 and self.clock()>=deadline):
                        return {'state':'HOLD','phase':'queue_deadline','automatic_retries':0}
                    if sequence==0:
                        image=next(x for x in job.snapshot['images'] if x['id']==image_id)
                        original=originals[image_id]
                        request=plan_anchored_motion(image_id=image_id,original_bytes=original['bytes'],
                            content_type=original['content_type'],original_sha256=image['sha256'],prompt=scene_prompt(scene))
                        self._check();submit_initial_image_once(plan=plan,expected_plan_sha256=plan_sha,image_id=image_id,
                            request=request,journal=self.spend_journal,transport=self.transport,now=self.now)
                    else:
                        self._check();submit_extension_once(plan=plan,expected_plan_sha256=plan_sha,image_id=image_id,
                            sequence=sequence,prior_clip=previous,journal=self.spend_journal,transport=self.transport,now=self.now)
                    self._check()
                    while True:
                        self._check()
                        if observations>=self.max_poll_observations or (self.poll_timeout_seconds>0 and self.clock()>=deadline):
                            captured={'state':'IN_PROGRESS','reason':'queue_deadline'};break
                        captured=self.capture_segment(job=job,plan=plan,expected_plan_sha256=plan_sha,image_id=image_id,
                            sequence=sequence,spend_journal=self.spend_journal,transport=self.transport,segment_store=self.segment_store,
                            review_loader=self.review_loader,root=Path(tempfile.mkdtemp(prefix='capture-',dir=root))/'media',narration_report=report,crop_resolver=self.crop_resolver,enabled=True)
                        observations+=1
                        if captured['state'] not in ('IN_QUEUE','IN_PROGRESS'):break
                        remaining=deadline-self.clock()
                        if remaining<=0 or observations>=self.max_poll_observations:break
                        self._check();self.sleep(min(self.poll_interval_seconds,remaining));self._check()
                    if captured['state']!='READY':
                        return {'state':'HOLD' if captured['state'] in ('IN_QUEUE','IN_PROGRESS','HOLD') else captured['state'],
                                'phase':'motion_capture','image_id':image_id,'sequence':sequence,'capture':captured,'automatic_retries':0}
                    previous=captured['verified_clip']
        except ProviderOutcomeUnknown:
            return {'state':'HOLD','phase':'provider_outcome_unknown','automatic_retries':0}
        self._check()
        prepared=stage_committed_continuation(job=job,plan=plan,expected_plan_sha256=plan_sha,narration_report=report,
            expected_report_sha256=report_sha,narration_ref=native['narration_ref'],
            segment_loader=lambda owner,jid,image,seq:self.segment_store.get_record(job,image,seq),
            reader=self.reader,spend_journal=self.spend_journal,root=Path(tempfile.mkdtemp(prefix='stage-',dir=root))/'prepared',expected_bucket=self.target['bucket'],enabled=True,
            **({'preview_evidence_verifier':self.review_loader.verify_preview_evidence,'preview_source_verifier':lambda candidate:self.verify_preview_source(candidate,report)} if preview is not None else {}))
        self._check()
        handoff=persist_prepared_continuation(job=job,prepared=prepared,target=self.target,input_store=self.input_store,
            evidence_store=self.evidence_store,handoff_registry=self.handoff_registry,enabled=True,
            **({"preview_evidence_verifier":self.review_loader.verify_preview_evidence,"preview_source_verifier":lambda candidate:self.verify_preview_source(candidate,report)} if preview is not None else {}))
        return {'state':'PREVIEW_PREPARED' if preview is not None else 'PREPARED','handoff':handoff,'automatic_retries':0,'new_execution_required':False}

    def verify_preview_source(self,candidate,narration_report):
        self._check();job=self._job
        require(job.snapshot.get('processing_plan',{}).get('preview_contract') is not None,'Source verification is scoped to explicit preview intent.')
        originals={image['id']:self.originals(job,image) for image in job.snapshot['images']}
        root=Path(tempfile.mkdtemp(prefix='preview-source-readback-'))
        try:
            self.source_planner.verify_preview_candidate(candidate,job=job,originals=originals,
                narration_report=narration_report,root=root)
            return True
        finally:
            import shutil
            shutil.rmtree(root)


def build_generation_factory(*,narration_transport,motion_transport,narration_approval_loader,
                             policy_loader,review_loader,activation_check,source_kind='provider_receipts',enabled=False,review_funding=None,source_planner=None,review_loader_factory=None,source_planner_factory=None,preview_authorization_loader=None,preview_budget_loader=None,preview_quote_loader=None):
    """Concrete Google-backed per-job assembly with explicitly reviewed transports.

    Does not discover credentials or construct a token/key. The returned factory
    is used by WorkerBindings.generation_factory inside the verified worker only.
    Review/quote loaders are private trusted records, never browser parameters.
    """
    require(source_kind in ('provider_receipts','synthetic_test'),'Explicit provider provenance required.')
    def factory(job,runtime):
        from dataclasses import fields
        from .hosted_control import Principal,VerifiedImage
        from .cloud_storage import CloudUploadStore,CloudProviderResponseStore
        from .cloud_journal import FirestoreSpendJournal
        from .narration_stage import NarrationStage,CloudNarrationAudioStore,FirestoreNarrationRecords
        from .continuation_worker import CloudPreparedInputStore,FirestorePreparedContinuation
        from .segment_capture import capture_segment,CloudSegmentObjects,FirestoreSegmentStore,ReviewedProviderGeometry
        from .worker_bootstrap import BoundCloudInputReader
        require(enabled is True and runtime.enabled and callable(activation_check) and activation_check() is True,
                'Original-input production graph remains disabled.')
        config=runtime.config;bindings=runtime.bindings;bucket=config['target']['bucket'];budget=config['budget']
        common=dict(project_id=config['target']['project_id'],namespace=config['firestore']['namespace'],
                    transaction_runner=bindings.transaction_runner)
        journal=FirestoreSpendJournal(bindings.firestore_client,**common,owner_id=job.owner_id,job_id=job.id,
            budget_scope=budget['scope'],approved_total_cap_cents=budget['approved_total_cap_cents'],prior_spend_cents=budget['prior_spend_cents'])
        def preview_budget(j):
            require(callable(preview_budget_loader),'Explicit trusted preview budget loader required.')
            value=preview_budget_loader(j)
            require(isinstance(value,dict) and all(value.get(k)==budget[k] for k in ('scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents')),
                    'Private preview budget changed configured campaign caps.')
            return value
        def active():
            require(enabled is True and activation_check() is True,'Generation acceptance expired.')
            load_preview_context(job,preview_authorization_loader,preview_budget)
            return True
        class CheckedMotion:
            def submit(self,request):active();return motion_transport.submit(request)
            def submit_authorized(self,request,submission_check):
                active();require(callable(getattr(motion_transport,'submit_authorized',None)),'Final-send preview motion gate required.')
                def gate():active();return submission_check()
                return motion_transport.submit_authorized(request,gate)
            def status(self,handle):active();return motion_transport.status(handle)
            def result(self,handle):active();return motion_transport.result(handle)
            def download(self,result):active();return motion_transport.download(result)
        def narration(request):
            active()
            if job.snapshot.get('processing_plan',{}).get('preview_contract') is not None:
                require(callable(getattr(narration_transport,'submit_authorized',None)),'Final-send preview narration gate required.')
                def gate():active();current_quote(job,approval(job));return True
                return narration_transport.submit_authorized(request,gate)
            return narration_transport(request)
        def approval_record(j):
            active();loaded=narration_approval_loader(j)
            require(isinstance(loaded,dict) and set(loaded)=={'approval','quote'},'Independent current narration quote required.')
            value,quote=loaded['approval'],loaded['quote']
            require(isinstance(quote,dict) and set(quote)=={'reference','owner_id','job_id','plan_sha256','reservations_cents','valid_until_epoch'}
                    and quote['reference']==value.reference and quote['owner_id']==j.owner_id and quote['job_id']==j.id
                    and quote['plan_sha256']==value.plan_sha256 and quote['reservations_cents']==list(value.reservations_cents)
                    and type(quote['valid_until_epoch']) in (int,float) and math.isfinite(quote['valid_until_epoch']),
                    'Narration quote differs from the exact approved owner/job/request costs.')
            require(value.job_cap_cents==budget['job_cap_cents'] and value.operation_cap_cents<=budget['operation_cap_cents'],
                    'Narration approval exceeds configured private caps.')
            return value,copy.deepcopy(quote)
        def approval(j):return approval_record(j)[0]
        def current_quote(j,approved):
            active();value,quote=approval_record(j);expires=quote['valid_until_epoch']
            require(value==approved,'Narration approval changed before a new submission.')
            require(time.time()<expires<=time.time()+86400,'Narration quote expired before new submission.')
            return True
        native=NarrationStage(journal_factory=lambda j:journal,
            response_store_factory=lambda j:CloudProviderResponseStore(bindings.storage_client,bucket=bucket,owner=Principal(j.owner_id),job_id=j.id),
            transport=narration,audio_store=CloudNarrationAudioStore(bindings.storage_client,bucket=bucket),
            records=FirestoreNarrationRecords(bindings.firestore_client,**common),expected_ffmpeg_version=config['runtime']['ffmpeg_version'],
            activation_check=active,approval_loader=approval,submission_check=current_quote,source_kind=source_kind,enabled=True)
        uploads=CloudUploadStore(bindings.storage_client,bucket=bucket)
        def originals(j,image):
            active();names={field.name for field in fields(VerifiedImage)}
            record=VerifiedImage(**{key:image[key] for key in names});owner=Principal(j.owner_id);record.validate(owner,record.id)
            stream=uploads.read_original(owner,record)
            try:data=stream.read(record.byte_count+1)
            finally:stream.close()
            require(len(data)==record.byte_count and hashlib.sha256(data).hexdigest()==record.sha256,'Original image bytes changed.')
            return {'bytes':data,'content_type':record.content_type}
        def policy(j,motion):
            active();value=policy_loader(j,motion)
            if value and 'budget' in value:
                require(all(value['budget'].get(key)==budget[key] for key in
                    ('scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents')),'Motion approval changed configured spending cap.')
                require(all(value[k]['max_cost_cents']<=budget['operation_cap_cents'] for k in ('image_quote','extension_quote')),
                        'Quoted operation exceeds configured private cap.')
            return value
        scoped_review=review_loader
        if review_loader_factory is not None:
            require(callable(review_loader_factory),'Explicit per-job review factory required.')
            scoped_review=review_loader_factory(job=job,runtime=runtime,journal=journal)
        scoped_planner=source_planner
        if source_planner_factory is not None:
            require(callable(source_planner_factory),'Explicit per-job source-planner factory required.')
            scoped_planner=source_planner_factory(job=job,runtime=runtime,journal=journal)
        if job.snapshot.get('processing_plan',{}).get('preview_contract') is not None:
            require(scoped_planner is not None and scoped_review is not None,'Concrete private preview stages required.')
            scoped_planner.activation_check=active
            scoped_review.activation=active
        return GenerationCoordinator(narration_stage=native,policy_loader=policy,originals=originals,
            plan_store=FirestoreGenerationPlan(bindings.firestore_client,**common),spend_journal=journal,transport=CheckedMotion(),
            segment_store=FirestoreSegmentStore(bindings.firestore_client,**common,objects=CloudSegmentObjects(bindings.storage_client,bucket=bucket)),
            review_loader=scoped_review,reader=BoundCloudInputReader(bindings.storage_client,bucket=bucket,
                owner_namespace=hashlib.sha256(job.owner_id.encode()).hexdigest(),job_id=job.id),
            input_store=CloudPreparedInputStore(bindings.storage_client,bucket=bucket),
            evidence_store=runtime.preview_evidence if job.snapshot.get("processing_plan",{}).get("preview_contract") is not None else runtime.evidence,
            handoff_registry=FirestorePreparedContinuation(bindings.firestore_client,**common),target=config['target'],
            activation_check=active,capture_segment=capture_segment,enabled=True,
            poll_timeout_seconds=min(600,config['target']['task_timeout_seconds']//2),crop_resolver=ReviewedProviderGeometry(),review_funding=review_funding,source_planner=scoped_planner,preview_authorization_loader=preview_authorization_loader,
            preview_budget_loader=preview_budget,preview_quote_loader=preview_quote_loader)
    return factory
