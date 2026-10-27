"""Explicit owner-only preview authorization; never historical human approval.

Only trusted private authorization records and native evidence loaders belong
here. A matching digest establishes immutable binding, not identity by itself.
"""
import copy
import hashlib
import json
import math
import time
from types import SimpleNamespace
from .elevenlabs_adapter import fingerprint
from .automated_review import need,exact,SHA,ID,candidate_policy,parse_review

AUTH_SCHEMA='byron-private-preview-authorization-v1'
QC_SCHEMA='byron-private-preview-qc-v1'
PLAN_SCHEMA='byron-private-preview-continuation-v1'
RECEIPT_SCHEMA='byron-private-preview-clip-v1'


def policy():
    from pathlib import Path
    return dict(schema='byron-private-preview-policy-v1',mode='PRIVATE_DRAFT_PREVIEW',owner_only=True,
        human_approved=False,public_release=False,source_grounding_required=True,clean_native_qc_every_segment=True,
        uncertainty='HOLD',sample_count=9,watermark='DRAFT – PRIVATE PREVIEW',loop=False,freeze=False,retime=False,
        implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


def validate_intent(contract):
    from .source_planning import policy as source_policy
    exact(contract,{'mode','preview_policy_sha256','preview_release_sha256','source_policy_sha256','review_policy_sha256'},'Exact server-frozen private-preview intent required.')
    need(contract['mode']=='PRIVATE_DRAFT_PREVIEW' and contract['preview_policy_sha256']==fingerprint(policy())
         and contract['source_policy_sha256']==fingerprint(source_policy()) and contract['review_policy_sha256']==fingerprint(candidate_policy())
         and isinstance(contract['preview_release_sha256'],str) and SHA.fullmatch(contract['preview_release_sha256']),
         'Preview intent changed current policy/release or historical mode.')
    return copy.deepcopy(contract)


def validate_authorization(authorization,expected_hash,*,job,budget,preview_release_sha256,now=None,require_current=True):
    from .source_planning import policy as source_policy
    now=time.time() if now is None else now
    exact(authorization,{'schema','reference','owner_id','job_id','snapshot_sha256','source_policy_sha256','review_policy_sha256',
        'preview_release_sha256','preview_policy_sha256','budget','expires_at','private_preview_approved','owner_only','public_release','human_style_accepted'},
        'Complete independent private-preview authorization required.')
    need(isinstance(expected_hash,str) and SHA.fullmatch(expected_hash) and fingerprint(authorization)==expected_hash,
         'Private-preview authorization changed from independently loaded approval.')
    need(authorization['schema']==AUTH_SCHEMA and isinstance(authorization['reference'],str) and ID.fullmatch(authorization['reference'])
        and (authorization['owner_id'],authorization['job_id'],authorization['snapshot_sha256'])==(job.owner_id,job.id,job.snapshot_sha256)
        and authorization['source_policy_sha256']==fingerprint(source_policy()) and authorization['preview_policy_sha256']==fingerprint(policy()) and authorization['review_policy_sha256']==fingerprint(candidate_policy())
        and authorization['preview_release_sha256']==preview_release_sha256 and isinstance(preview_release_sha256,str) and SHA.fullmatch(preview_release_sha256)
        and authorization['budget']==budget and authorization['private_preview_approved'] is True and authorization['owner_only'] is True
        and authorization['public_release'] is False and authorization['human_style_accepted'] is False,
        'Preview approval changed owner/job/policy/release/budget or implies public/human approval.')
    need(type(authorization['expires_at']) in (int,float) and math.isfinite(authorization['expires_at']),'Finite preview authorization expiry required.')
    if require_current:need(type(now) in (int,float) and math.isfinite(now) and now<authorization['expires_at']<=now+86400,'Private-preview authorization expired or exceeds one-day validity.')
    if hasattr(job,'snapshot'):
        need(fingerprint(job.snapshot)==job.snapshot_sha256,'Frozen preview job changed.')
        intent=validate_intent(job.snapshot.get('processing_plan',{}).get('preview_contract'))
        need(all(intent[k]==authorization[k] for k in ('preview_policy_sha256','preview_release_sha256','source_policy_sha256','review_policy_sha256')),'Authorization differs from server-frozen preview intent.')
    return copy.deepcopy(authorization)


def preview_plan(plan,*,now=None,require_current=True):
    need(plan.get('preview_schema')==PLAN_SCHEMA and plan.get('human_approved') is False and plan.get('owner_only') is True,
         'Distinct private-preview continuation schema required.')
    job=SimpleNamespace(owner_id=plan['owner_id'],id=plan['job_id'],snapshot_sha256=plan['snapshot_sha256'])
    validate_authorization(plan['preview_authorization'],plan['preview_authorization_sha256'],job=job,budget=plan['budget'],
        preview_release_sha256=plan['preview_release_sha256'],now=now,require_current=require_current)
    need(all('approved_prompt' not in scene and 'prompt_approval_reference' not in scene
             and isinstance(scene.get('preview_prompt'),str) and scene['preview_prompt']
             and scene.get('preview_authorization_sha256')==plan['preview_authorization_sha256'] for scene in plan['scenes']),
         'Preview prompts must retain explicit non-human provenance.')
    candidate=plan['source_candidate']
    need(fingerprint(candidate)==plan['source_candidate_sha256'] and candidate['human_approved'] is False and candidate['unresolved_uncertainty'] is False
         and len(candidate['proposals'])==len(plan['scenes']),'Preview source candidate provenance changed.')
    for scene,proposal in zip(plan['scenes'],candidate['proposals']):
        need((scene['image_id'],scene['source_sha256'],scene['preview_prompt'],scene['source_proposal_sha256'])==
             (proposal['image_id'],proposal['source_sha256'],proposal['proposed_prompt'],fingerprint(proposal))
             and proposal['approved'] is False and proposal['uncertainties']==[] and proposal['primary_category']!='none'
             and proposal['analysis_detail_state']!='LOW_TEXTURE_REVIEW_REQUIRED','Preview prompt was not the clean original source proposal.')
    return plan


def plan_private_preview_continuation(*,job,source_candidate,timing,narration_report,expected_report_sha256,
                                     authorization,expected_authorization_sha256,preview_release_sha256,
                                     image_quote,extension_quote,budget,now=None):
    from .source_planning import validate_candidate_result
    from .motion_planning import validate_timing_plan
    from .motion_continuation import _quote
    now=time.time() if now is None else now
    validate_authorization(authorization,expected_authorization_sha256,job=job,budget=budget,preview_release_sha256=preview_release_sha256,now=now)
    candidate=validate_candidate_result(source_candidate,job=job,narration_report=narration_report)
    need(candidate['unresolved_uncertainty'] is False and candidate['human_approved'] is False and candidate['provider_submission_authorized'] is False,
         'Uncertain source proposal cannot authorize private preview motion.')
    need(fingerprint(narration_report)==expected_report_sha256 and narration_report.get('media_verified') is True
         and narration_report.get('provider_receipts_verified') is True and narration_report.get('provider')=='elevenlabs',
         'Exact decoded narration report required for preview.')
    images=job.snapshot['images'];proposals=candidate['proposals']
    need(len(images) in (4,5) and len(proposals)==len(images) and len(timing['scenes'])==len(images),'Preview requires the same four/five paintings.')
    sources=[dict(id=i['id'],position=i['position'],source_sha256=i['sha256'],duration_seconds=s['end_seconds']-s['start_seconds']) for i,s in zip(images,timing['scenes'])]
    text_sha=hashlib.sha256(job.snapshot['narrated_text'].encode()).hexdigest();validate_timing_plan(timing,sources,text_sha)
    need(timing['audio_sha256']==narration_report['audio_sha256'] and timing['word_timing_sha256']==fingerprint(narration_report['words'])
         and abs(timing['duration_seconds']-narration_report['duration_seconds'])<1e-6,'Preview timing changed decoded narrator coverage.')
    image_quote=_quote(image_quote,'image',now);extension_quote=_quote(extension_quote,'extend',now)
    need(image_quote['reference']!=extension_quote['reference'],'Distinct extension price approval required.')
    exact(budget,{'scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents','retry_allowance_cents','approval_reference'},'Exact shared preview budget required.')
    need(all(type(budget[k]) is int for k in ('approved_total_cap_cents','prior_spend_cents','job_cap_cents','retry_allowance_cents'))
         and 0<=budget['prior_spend_cents']<budget['approved_total_cap_cents'] and 0<budget['job_cap_cents']<=budget['approved_total_cap_cents']
         and 0<=budget['retry_allowance_cents']<=budget['job_cap_cents'],'Invalid preview budget.')
    expected_budget=dict(scope_sha256=hashlib.sha256(budget['scope'].encode()).hexdigest(),approved_total_cap_cents=budget['approved_total_cap_cents'],prior_spend_cents=budget['prior_spend_cents'],job_cap_cents=budget['job_cap_cents'])
    need(candidate['budget_sha256']==fingerprint(expected_budget),'Source planner used another campaign budget.')
    scenes=[]
    for image,proposal,source in zip(images,proposals,sources):
        need(proposal['approved'] is False and proposal['source_supported_claim_only'] is True and proposal['uncertainties']==[]
             and proposal['primary_category']!='none' and proposal['analysis_detail_state']!='LOW_TEXTURE_REVIEW_REQUIRED',
             'Unsupported, uncertain or low-detail proposal stays HOLD.')
        required=source['duration_seconds'];count=max(0,math.ceil((required-10-1e-9)/5))
        need(1.625<=required<=60 and count<=10,'Preview cannot loop/freeze/retime or exceed sixty seconds per scene.')
        segments=[dict(sequence=0,kind='image',duration_seconds=10,expected_full_duration_seconds=10,max_cost_cents=image_quote['max_cost_cents'])]
        for seq in range(1,count+1):segments.append(dict(sequence=seq,kind='extend',duration_seconds=5,expected_prior_duration_seconds=10+5*(seq-1),expected_full_duration_seconds=10+5*seq,max_cost_cents=extension_quote['max_cost_cents']))
        scenes.append(dict(image_id=image['id'],position=image['position'],source_sha256=image['sha256'],required_seconds=required,
            preview_prompt=proposal['proposed_prompt'],source_proposal_sha256=fingerprint(proposal),preview_authorization_sha256=expected_authorization_sha256,
            segments=segments,assembly=dict(use_only_final_full_clip=True,trim_start_seconds=0,trim_duration_seconds=required,provider_audio='mute',loop=False,freeze=False,retime=False)))
    cost=sum(segment['max_cost_cents'] for scene in scenes for segment in scene['segments']);ceiling=cost+budget['retry_allowance_cents']
    need(ceiling<=budget['job_cap_cents'] and ceiling+budget['prior_spend_cents']<=budget['approved_total_cap_cents'],'Preview motion exceeds existing campaign/film cap.')
    result=dict(schema_version=1,status='DRAFT_NOT_ACTIVATED',preview_schema=PLAN_SCHEMA,owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,
        narration_report_sha256=expected_report_sha256,narration_text_sha256=text_sha,timing_plan_sha256=fingerprint(timing),motion_plan_sha256=fingerprint(candidate),
        duration_seconds=timing['duration_seconds'],crossfade_seconds=timing['crossfade_seconds'],input_order=[i['id'] for i in images],scenes=scenes,
        image_quote=image_quote,extension_quote=extension_quote,budget=copy.deepcopy(budget),quoted_generation_cents=cost,reserved_plan_ceiling_cents=ceiling,
        automatic_retries=0,max_extensions_per_scene=10,live_release=False,human_approved=False,owner_only=True,
        preview_authorization=copy.deepcopy(authorization),preview_authorization_sha256=expected_authorization_sha256,preview_release_sha256=preview_release_sha256,
        source_candidate=copy.deepcopy(candidate),source_candidate_sha256=fingerprint(candidate))
    result['sha256']=fingerprint(result);return result


def make_preview_qc(*,candidate,result,plan,expected_plan_sha256,authorization,expected_authorization_sha256,job,preview_release_sha256,archive_verification,now=None):
    from .motion_continuation import _checked_plan,scene_prompt
    from .vertex_review import ID_PREFIX,MODEL
    _checked_plan(plan,expected_plan_sha256,0,False)
    validate_authorization(authorization,expected_authorization_sha256,job=job,budget=plan['budget'],preview_release_sha256=preview_release_sha256,now=now)
    need(expected_authorization_sha256==plan['preview_authorization_sha256'],'QC authorization differs from motion authorization.')
    scope=(job.owner_id,job.id,job.snapshot_sha256,expected_plan_sha256)
    need(tuple(candidate.get(k) for k in ('owner_id','job_id','snapshot_sha256','continuation_plan_sha256'))==scope,'QC candidate changed frozen preview scope.')
    scene=next((s for s in plan['scenes'] if s['image_id']==candidate['image_id']),None)
    need(scene is not None and type(candidate['sequence']) is int and 0<=candidate['sequence']<len(scene['segments']),'Unplanned preview segment.')
    base={k:v for k,v in result.items() if k not in {'operation_id','provider_request_id','automatic_retries','policy_status','review_evidence_sha256'}}
    need(result.get('policy_status')=='UNAPPROVED_CANDIDATE' and result.get('automatic_retries')==0
         and fingerprint(base)==result.get('review_evidence_sha256') and result.get('state')=='CANDIDATE_PASS' and result.get('reasons')==[]
         and result.get('human_approved') is False and result.get('live_release') is False,
         'Only independently archived clean automated candidate evidence can authorize preview continuation.')
    need(isinstance(archive_verification,dict) and archive_verification.get('review_result_sha256')==fingerprint(result)
         and archive_verification.get('operation_id')==result.get('operation_id')
         and all(isinstance(archive_verification.get(k),str) and SHA.fullmatch(archive_verification[k]) for k in ('request_sha256','receipt_sha256','native_record_sha256')), 'Independent native archive verification required, not a self-hashed model response.')
    binding=result['binding'];need(all(binding[k]==candidate[k] for k in ('owner_id','job_id','image_id','sequence','source_sha256'))
         and binding['source_sha256']==scene['source_sha256'] and binding['clip_sha256']==candidate['video']['sha256']
         and binding['prompt_sha256']==hashlib.sha256(scene_prompt(scene).encode()).hexdigest()
         and binding['policy_sha256']==authorization['review_policy_sha256'] and binding['model']==MODEL,
         'QC changed exact source/clip/prompt/native model/policy.')
    need(isinstance(result.get('provider_request_id'),str) and result['provider_request_id'].startswith(ID_PREFIX)
         and SHA.fullmatch(result['provider_request_id'][len(ID_PREFIX):]),'Native archived review identity required.')
    budget=plan['budget'];expected_budget=dict(scope_sha256=hashlib.sha256(budget['scope'].encode()).hexdigest(),approved_total_cap_cents=budget['approved_total_cap_cents'],prior_spend_cents=budget['prior_spend_cents'],job_cap_cents=budget['job_cap_cents'])
    need(result['budget_sha256']==fingerprint(expected_budget),'QC used another shared film/campaign budget.')
    # Re-evaluate every existing confidence/semantic/pixel gate, not a copied PASS.
    raw=json.dumps(result['semantic_assessment'],separators=(',',':')).encode()
    recalculated=parse_review(raw,{'body':{'binding':binding,'evidence':{'metrics':result['pixel_metrics'],'samples':[None]*9}}})
    need(recalculated['state']=='CANDIDATE_PASS' and recalculated['reasons']==[] and recalculated['pose_metrics']==result['pose_metrics'],
         'Low-confidence, uncertain, frozen/repeated or changed-pose evidence stays HOLD.')
    return dict(schema=QC_SCHEMA,preview_authorization_sha256=expected_authorization_sha256,preview_release_sha256=preview_release_sha256,
        owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,continuation_plan_sha256=expected_plan_sha256,
        image_id=candidate['image_id'],sequence=candidate['sequence'],source_sha256=candidate['source_sha256'],clip_sha256=candidate['video']['sha256'],
        private_preview_eligible=True,human_approved=False,public_release=False,archive_verification=copy.deepcopy(archive_verification),native_review=copy.deepcopy(result),native_review_sha256=fingerprint(result))


def validate_preview_qc(review,*,candidate,plan,job,archive_verifier,now=None,require_current=True):
    need(isinstance(review,dict) and review.get('schema')==QC_SCHEMA and fingerprint(review.get('native_review'))==review.get('native_review_sha256'),'Preview QC provenance changed.')
    need(callable(archive_verifier),'Independent native archive reread is mandatory for preview QC.')
    verification=archive_verifier(review['native_review'])
    need(verification==review.get('archive_verification'),'Preview native archive or paid receipt changed.')
    effective=now if require_current else plan['preview_authorization']['expires_at']-1
    expected=make_preview_qc(candidate=candidate,result=review['native_review'],plan=plan,expected_plan_sha256=fingerprint(plan),
        authorization=plan['preview_authorization'],expected_authorization_sha256=plan['preview_authorization_sha256'],job=job,
        preview_release_sha256=plan['preview_release_sha256'],archive_verification=verification,now=effective)
    need(expected==review,'Preview QC proof changed; never substitute historical human review.')
    return review


from .cloud_journal import _FirestoreBoundary


class FirestorePreviewAuthorizations(_FirestoreBoundary):
    """Create-only private authorization; reads recheck current scoped policy.

    budget_loader(job) is trusted private config, never a browser amount. Creating
    a record is an explicit operator authorization operation, not auto-approval.
    """
    def __init__(self,*args,budget_loader,preview_release_sha256,now=None,**kwargs):
        super().__init__(*args,**kwargs);self.budget_loader=budget_loader;self.release=preview_release_sha256;self.now=now or time.time
    def _ref(self,job):
        need(isinstance(job.owner_id,str) and ID.fullmatch(job.owner_id) and isinstance(job.id,str) and ID.fullmatch(job.id),'Explicit private authorization identity required.')
        return self._doc(self._owner(job.owner_id)+'/preview_authorizations/'+job.id)
    def commit(self,job,authorization,expected_authorization_sha256):
        from .worker_binding import canonical
        validate_authorization(authorization,expected_authorization_sha256,job=job,budget=self.budget_loader(job),preview_release_sha256=self.release,now=self.now())
        value=dict(owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,authorization_sha256=expected_authorization_sha256,
                   authorization_json=canonical(authorization).decode())
        ref=self._ref(job)
        def transaction(tx):
            old=self._read(ref,tx)
            if old is not None:need(old==value,'Private preview authorization is immutable; never silently expand approval.');return False
            tx.create(ref,value);return True
        return self._run(transaction)
    def load(self,job):
        value=self._read(self._ref(job));need(isinstance(value,dict),'Independent private-preview authorization absent.')
        need((value.get('owner_id'),value.get('job_id'),value.get('snapshot_sha256'))==(job.owner_id,job.id,job.snapshot_sha256),'Authorization belongs to another frozen job.')
        authorization=json.loads(value['authorization_json'])
        validate_authorization(authorization,value['authorization_sha256'],job=job,budget=self.budget_loader(job),preview_release_sha256=self.release,now=self.now())
        return authorization,value['authorization_sha256']
    def __call__(self,job):return self.load(job)


def validate_preview_context(context,expected_hash,*,staged,frozen,clip_receipts,expected_report_sha256,
                             expected_authorization_sha256,preview_release_sha256,preview_evidence_verifier,preview_source_verifier,now=None):
    from .continuation_worker import validate_continuation_context
    need(isinstance(context,dict) and context.get('schema_version')==2 and context.get('status')=='PRIVATE_DRAFT_PREVIEW_CONTEXT'
         and context.get('authorization_sha256')==expected_authorization_sha256
         and context.get('preview_release_sha256')==preview_release_sha256,'Independent private-preview context/authorization/release required.')
    plan=context['plan']
    need(callable(preview_source_verifier) and preview_source_verifier(plan['source_candidate']) is True,'Native source-planning archive reread is mandatory for preview assembly.')
    job=SimpleNamespace(owner_id=staged['owner_id'],id=staged['job_id'],snapshot_sha256=staged['snapshot_sha256'],snapshot=frozen)
    validate_authorization(context['authorization'],expected_authorization_sha256,job=job,budget=plan['budget'],preview_release_sha256=preview_release_sha256,now=now)
    need(context['authorization']==plan['preview_authorization'] and plan['preview_authorization_sha256']==expected_authorization_sha256
         and plan['preview_release_sha256']==preview_release_sha256,'Context differs from exact authorized preview motion plan.')
    return validate_continuation_context(context,expected_hash,staged=staged,frozen=frozen,clip_receipts=clip_receipts,
                                        expected_report_sha256=expected_report_sha256,_private_preview=True,preview_evidence_verifier=preview_evidence_verifier)


def verify_preview_review(result,*,response_store,funding=None,journal=None,trusted_budget=None):
    """Authoritative native reread for both fresh preview eligibility and workers."""
    from .vertex_review import VertexVerifiedReviewResponseStore
    from .gemini_developer import DeveloperVerifiedReviewResponseStore
    from .automated_review import validate_request,validate_budget_context
    need(type(response_store) in (VertexVerifiedReviewResponseStore,DeveloperVerifiedReviewResponseStore),'Native verified response wrapper required for preview.')
    if funding is not None:journal=funding['journal'];trusted_budget=funding['trusted_budget']
    need(journal is not None and trusted_budget is not None,'Independently scoped review journal/budget required.')
    budget_sha=validate_budget_context(journal,trusted_budget)
    operation=result.get('operation_id');entry=journal.get(operation)
    need(entry and entry['state']=='received' and entry['reservation_cents']>0,'Preview review lacks original received paid operation.')
    receipt=entry['receipt'];snapshot=response_store.read_response(receipt['request_archive'])
    request=json.loads(snapshot.payload);validate_request(request)
    need(snapshot.request_id=='review_input_'+hashlib.sha256(snapshot.payload).hexdigest(),'Exact archived review request changed.')
    binding=request['body']['binding']
    expected_operation='vision_review_'+hashlib.sha256((binding['owner_id']+'\0'+binding['job_id']+'\0'+binding['image_id']+'\0'+str(binding['sequence'])).encode()).hexdigest()
    need(operation==expected_operation and journal.owner_id==binding['owner_id'] and journal.job_id==binding['job_id'],'Native review archive escaped paid owner/job/segment.')
    wire=response_store.read_response(receipt)
    native=response_store.evidence.verify_for_wire(wire,expected_request=request)
    base={**parse_review(wire.payload,request),'budget_sha256':budget_sha}
    actual={**base,'operation_id':operation,'provider_request_id':wire.request_id,'automatic_retries':0,'policy_status':'UNAPPROVED_CANDIDATE','review_evidence_sha256':fingerprint(base)}
    need(actual==result,'Claimed clean preview result differs from independently archived native evidence.')
    return dict(operation_id=operation,review_result_sha256=fingerprint(result),request_sha256=fingerprint(request),receipt_sha256=fingerprint(receipt),native_record_sha256=fingerprint(native))
