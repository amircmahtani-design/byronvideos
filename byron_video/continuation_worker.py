"""Stage and bind committed continuation media; never generate or accept it live.

Every segment is independently loaded from immutable storage and fully decoded.
No provider transport, paid retry or inferred content approval exists here.
"""
import hashlib
import json
from pathlib import Path
from .elevenlabs_adapter import fingerprint
from .motion_continuation import (
    _checked_plan,_journal_scope,scene_prompt,select_final_clips,VerifiedPriorClip,IMAGE_ENDPOINT,EXTENSION_ENDPOINT,
    validate_extension_handle,_identifier,_sha,require,
)
from .provider_execution import validate_fal_handle,_fal_media_url
from .worker_binding import safe_private_root,copy_verified_object,canonical
from .cloud_render import validate_object

MAX_CHAIN_BYTES=3_000_000_000


def _operation(owner,job,image,sequence):
    identity=owner+'\0'+job+'\0'+image
    if sequence:identity+='\0'+str(sequence)
    return ('extension_' if sequence else 'motion_')+hashlib.sha256(identity.encode()).hexdigest()


def _validate_record(record,expected,*,job,plan,plan_sha,scene,sequence,previous,expected_bucket,preview_evidence_verifier=None):
    fields={'schema_version','owner_id','job_id','snapshot_sha256','continuation_plan_sha256','image_id','source_sha256','sequence',
            'operation_id','journal_binding_sha256','provider_handle','provider_url','video','duration_seconds','presentation_crop',
            'prior_record_sha256','prior_clip_sha256','review','synthetic','full_decode_verified'}
    require(isinstance(record,dict) and set(record)==fields and _sha(expected) and fingerprint(record)==expected,'Independent segment record changed.')
    require(record['schema_version']==(2 if plan.get('preview_schema') else 1) and record['owner_id']==job.owner_id and record['job_id']==job.id
            and record['snapshot_sha256']==job.snapshot_sha256 and record['continuation_plan_sha256']==plan_sha
            and record['image_id']==scene['image_id'] and record['source_sha256']==scene['source_sha256']
            and type(record['sequence']) is int and record['sequence']==sequence and record['full_decode_verified'] is True
            and type(record['synthetic']) is bool and record['operation_id']==_operation(job.owner_id,job.id,scene['image_id'],sequence)
            and _sha(record['journal_binding_sha256']),'Segment owner/source/order/plan/operation changed.')
    handle=(validate_extension_handle if sequence else validate_fal_handle)(record['provider_handle'])
    require(_fal_media_url(record['provider_url']),'Untrusted provider result URL.')
    require(type(record['duration_seconds']) in (int,float)
            and abs(record['duration_seconds']-scene['segments'][sequence]['expected_full_duration_seconds'])<=1/24,
            'Each record must describe the full continuous clip, not an added prefix.')
    require((record['prior_record_sha256'],record['prior_clip_sha256'])==
            ((previous[1],previous[0]['video']['sha256']) if previous else (None,None)), 'Continuation chain changed its preceding full clip.')
    review=record['review']
    if plan.get('preview_schema'):
        from .private_preview import validate_preview_qc
        validate_preview_qc(review,candidate=record,plan=plan,job=job,require_current=False,archive_verifier=preview_evidence_verifier)
    else:
        require(isinstance(review,dict) and set(review)=={'reference','clip_sha256','source_sha256','prompt_sha256','no_invented_action_reviewed'}
            and _identifier(review['reference']) and review['clip_sha256']==record['video']['sha256']
            and review['source_sha256']==scene['source_sha256'] and review['prompt_sha256']==hashlib.sha256(scene_prompt(scene).encode()).hexdigest()
            and review['no_invented_action_reviewed'] is True,'Independent exact-output motion review is required; decode is not semantic approval.')
    owner_hash=hashlib.sha256(job.owner_id.encode()).hexdigest()
    prefix=f'owners/{owner_hash}/jobs/{job.id}/segments/{scene["image_id"]}/'
    ref=record['video']
    require(isinstance(ref,dict) and isinstance(ref.get('bucket'),str),'Complete immutable private segment object required.')
    validate_object(ref,bucket=expected_bucket,prefix=prefix,content_type='video/mp4',max_bytes=50_000_000)
    if plan.get('preview_schema'):require(ref['object']==prefix+f'{sequence:02}.mp4','Preview clip storage changed exact segment sequence.')
    return handle


def stage_committed_continuation(*,job,plan,expected_plan_sha256,narration_report,expected_report_sha256,
                                  narration_ref,segment_loader,reader,spend_journal,root,expected_bucket,enabled=False,now=None,preview_evidence_verifier=None,preview_source_verifier=None):
    """Consume independently committed segments into a fresh private workspace.

    segment_loader(owner,job,image_id,sequence) -> (record,independent_digest).
    reader must read immutable generations from trusted private storage. All
    claimed operation receipts are independently checked against spend_journal.
    This function does NOT mint per-clip reviews or call any provider.
    """
    require(enabled is True,'Continuation staging is disabled until explicitly bound.')
    require(isinstance(expected_bucket,str) and bool(expected_bucket),'Independently configured private bucket required.')
    _checked_plan(plan,expected_plan_sha256,0,False)
    is_preview=bool(plan.get('preview_schema'))
    if is_preview:
        from .private_preview import preview_plan
        preview_plan(plan,now=now,require_current=True)
        require(callable(preview_source_verifier) and preview_source_verifier(plan['source_candidate']) is True,'Native source-planning archive verification required before preview staging.')
    _journal_scope(spend_journal,plan)
    frozen=job.snapshot
    require(job.owner_id==plan['owner_id'] and job.id==plan['job_id'] and fingerprint(frozen)==job.snapshot_sha256
            and frozen['input_order']==plan['input_order']
            and [(x['id'],x['position'],x['sha256']) for x in frozen['images']]==
                [(s['image_id'],s['position'],s['source_sha256']) for s in plan['scenes']], 'Continuation differs from independently frozen job/source order.')
    require(fingerprint(narration_report)==expected_report_sha256==plan['narration_report_sha256']
            and narration_report.get('exact_text')==frozen['narrated_text']
            and narration_report.get('profile_sha256')==fingerprint(frozen['processing_plan']['narration_profile']),
            'Narration/selected voice differs from the frozen continuation job.')
    owner_hash=hashlib.sha256(job.owner_id.encode()).hexdigest()
    validate_object(narration_ref,bucket=expected_bucket,prefix=f'owners/{owner_hash}/jobs/{job.id}/',
                    content_type='audio/wav',max_bytes=100_000_000)
    require(narration_ref['sha256']==narration_report['audio_sha256'],'Stored narration differs from its decoded report.')
    chains=[];total=narration_ref['bytes'];objects=set();clip_hashes=set()
    # All metadata/provenance checks happen before consuming a workspace.
    for scene in plan['scenes']:
        records=[];previous=None
        for sequence in range(len(scene['segments'])):
            record,digest=segment_loader(job.owner_id,job.id,scene['image_id'],sequence)
            handle=_validate_record(record,digest,job=job,plan=plan,plan_sha=expected_plan_sha256,scene=scene,sequence=sequence,previous=previous,expected_bucket=expected_bucket,preview_evidence_verifier=preview_evidence_verifier)
            entry=spend_journal.get(record['operation_id'])
            require(isinstance(entry,dict) and entry.get('state')=='received' and entry.get('plan_sha256')==record['journal_binding_sha256']
                    and entry.get('receipt')==handle,'Segment is not backed by the original independently recorded paid-operation receipt.')
            require(record['synthetic'] is narration_report.get('synthetic'),'Synthetic and real provenance cannot be mixed.')
            identity=(record['video']['bucket'],record['video']['object'],record['video']['generation'])
            require(identity not in objects and record['video']['sha256'] not in clip_hashes,'Do not reuse segment objects or repeat clip bytes.')
            objects.add(identity);clip_hashes.add(record['video']['sha256']);total+=record['video']['bytes']
            require(total<=MAX_CHAIN_BYTES,'Finite continuation chain exceeds the staging byte budget.')
            records.append({'record':record,'sha256':digest});previous=(record,digest)
        chains.append({'image_id':scene['image_id'],'segments':records})
    root=safe_private_root(root)
    files={'narration.wav':copy_verified_object(reader,narration_ref,root/'narration.wav')}
    from .portrait_composition import probe
    verified=[];receipts=[];composition=[]
    for index,(scene,chain) in enumerate(zip(plan['scenes'],chains),1):
        final=None
        for sequence,item in enumerate(chain['segments']):
            record=item['record'];last=sequence==len(chain['segments'])-1
            name=f'clip-{index:02}.mp4' if last else f'chain-{index:02}-{sequence:02}.mp4'
            path=copy_verified_object(reader,record['video'],root/name)
            decoded=probe(path,video=True,presentation_crop=record['presentation_crop'])
            require(abs(decoded['duration']-record['duration_seconds'])<=1/24,'Immutable segment did not decode to its recorded full duration.')
            require(hashlib.sha256(path.read_bytes()).hexdigest()==record['video']['sha256'],'Clip changed during full decode.')
            proof=VerifiedPriorClip(job.owner_id,job.id,scene['image_id'],scene['source_sha256'],sequence,
                record['video']['sha256'],record['video']['bytes'],decoded['duration'],record['provider_handle']['request_id'],
                record['provider_url'],item['sha256'],True)
            if last:final=proof;files[name]=path
            else:path.unlink()  # Only our freshly created verified staging file.
        verified.append(final)
        record=chain['segments'][-1]['record']
        receipt=dict(image_id=scene['image_id'],source_image_sha256=scene['source_sha256'],provider='fal',
            model=record['provider_handle']['endpoint'],media_verified=True,synthetic=record['synthetic'],presentation_crop=record['presentation_crop'],
            request_id=record['provider_handle']['request_id'],operation_id=record['operation_id'],sha256=record['video']['sha256'])
        if is_preview:
            from .private_preview import RECEIPT_SCHEMA
            receipt.update(schema=RECEIPT_SCHEMA,preview_qc_sha256=fingerprint(record['review']),human_approved=False)
        else:receipt['no_invented_action_reviewed']=True
        receipts.append(receipt)
        composition.append(dict(image_id=scene['image_id'],trim_length=scene['required_seconds'],presentation_crop=record['presentation_crop']))
    selected=select_final_clips(plan=plan,expected_plan_sha256=expected_plan_sha256,verified_clips=verified,now=0)
    comp={'schema_version':1,'status':'PRIVATE_DRAFT_PREVIEW' if is_preview else 'DRAFT_RELEASE_CANDIDATE','snapshot_sha256':job.snapshot_sha256,
          'narration_report_sha256':expected_report_sha256,'clips':composition}
    path=root/'composition.json';path.write_bytes(canonical(comp));path.chmod(0o600);files['composition.json']=path
    context={'schema_version':1,'status':'DRAFT_CONTINUATION_PROVENANCE','owner_id':job.owner_id,'job_id':job.id,
             'snapshot_sha256':job.snapshot_sha256,'private_bucket':expected_bucket,'plan':plan,'plan_sha256':expected_plan_sha256,
             'narration_report_sha256':expected_report_sha256,'chains':chains,'selected_final_clips':selected,
             'clip_receipts_sha256':fingerprint(receipts)}
    if is_preview:
        context.update(schema_version=2,status='PRIVATE_DRAFT_PREVIEW_CONTEXT',authorization=plan['preview_authorization'],
            authorization_sha256=plan['preview_authorization_sha256'],preview_release_sha256=plan['preview_release_sha256'])
    return {'staged':dict(root=root,files=files,owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256),
            'clip_receipts':receipts,'continuation_context':context,'continuation_context_sha256':fingerprint(context),
            'narration_report':narration_report,'narration_report_sha256':expected_report_sha256,'provider_requests':0}


def validate_continuation_context(context,expected_hash,*,staged,frozen,clip_receipts,expected_report_sha256,_private_preview=False,preview_evidence_verifier=None):
    """Composition gate: context must be loaded independently from private ledger."""
    from types import SimpleNamespace
    require(isinstance(context,dict) and _sha(expected_hash) and fingerprint(context)==expected_hash
            and context.get('schema_version')==(2 if _private_preview else 1) and context.get('status')==('PRIVATE_DRAFT_PREVIEW_CONTEXT' if _private_preview else 'DRAFT_CONTINUATION_PROVENANCE')
            and (context.get('owner_id'),context.get('job_id'),context.get('snapshot_sha256'))==
                (staged['owner_id'],staged['job_id'],staged['snapshot_sha256'])
            and fingerprint(frozen)==staged['snapshot_sha256'] and context.get('narration_report_sha256')==expected_report_sha256
            and context.get('clip_receipts_sha256')==fingerprint(clip_receipts),'Independent continuation composition context changed.')
    plan=context['plan'];_checked_plan(plan,context['plan_sha256'],0,False)
    require(bool(plan.get('preview_schema')) is _private_preview,'Preview provenance cannot satisfy historical final validation.')
    require(plan['input_order']==frozen['input_order'] and len(context['chains'])==len(plan['scenes'])==len(clip_receipts),
            'Continuation context changed source order.')
    job=SimpleNamespace(owner_id=staged['owner_id'],id=staged['job_id'],snapshot_sha256=staged['snapshot_sha256'])
    require([(x['id'],x['position'],x['sha256']) for x in frozen['images']]==[(x['image_id'],x['position'],x['source_sha256']) for x in plan['scenes']], 'Continuation plan changed frozen images.')
    models=[];seen=set();proofs=[];expected_composition=[]
    for scene,chain,receipt in zip(plan['scenes'],context['chains'],clip_receipts):
        require(chain['image_id']==scene['image_id'] and len(chain['segments'])==len(scene['segments']),'Incomplete continuation chain.')
        previous=None
        for sequence,item in enumerate(chain['segments']):
            record=item['record'];_validate_record(record,item['sha256'],job=job,plan=plan,plan_sha=context['plan_sha256'],scene=scene,sequence=sequence,previous=previous,expected_bucket=context['private_bucket'],preview_evidence_verifier=preview_evidence_verifier)
            require(record['video']['sha256'] not in seen,'Repeated immutable segment bytes in continuation context.')
            seen.add(record['video']['sha256']);previous=(record,item['sha256'])
        final=previous[0]
        require(receipt['sha256']==final['video']['sha256'] and receipt['request_id']==final['provider_handle']['request_id']
                and receipt['operation_id']==final['operation_id'] and receipt['model']==final['provider_handle']['endpoint'],
                'Final clip was substituted or extension relabelled as initial image.')
        if _private_preview:
            from .private_preview import RECEIPT_SCHEMA
            require(receipt.get('schema')==RECEIPT_SCHEMA and receipt.get('human_approved') is False
                    and 'no_invented_action_reviewed' not in receipt and receipt.get('preview_qc_sha256')==fingerprint(final['review']),'Preview receipt was relabelled as historical approval.')
        models.append(receipt['model'])
        proofs.append(VerifiedPriorClip(job.owner_id,job.id,scene['image_id'],scene['source_sha256'],final['sequence'],
            final['video']['sha256'],final['video']['bytes'],final['duration_seconds'],final['provider_handle']['request_id'],
            final['provider_url'],previous[1],True))
        expected_composition.append(dict(image_id=scene['image_id'],trim_length=scene['required_seconds'],presentation_crop=final['presentation_crop']))
    require(context['selected_final_clips']==select_final_clips(plan=plan,expected_plan_sha256=context['plan_sha256'],verified_clips=proofs,now=0), 'Selected final full clips differ from the continuation plan.')
    composition=json.loads(Path(staged['files']['composition.json']).read_text())
    require(composition['status']==('PRIVATE_DRAFT_PREVIEW' if _private_preview else 'DRAFT_RELEASE_CANDIDATE') and composition['clips']==expected_composition,'Composition changed continuation coverage/trim/crop.')
    return models


from .cloud_storage import _PrivateBucket
from .cloud_journal import _FirestoreBoundary


class CloudPreparedInputStore(_PrivateBucket):
    """Write-once final input/manifest objects with private generation readback."""
    def __init__(self,client,*,bucket,timeout=30):
        super().__init__(client,bucket=bucket,timeout=timeout)
        self.bucket_name=bucket
    def write(self,owner_id,job_id,name,data):
        import re
        require(_identifier(owner_id) and _identifier(job_id) and isinstance(data,bytes) and data,'Explicit private owner/job input required.')
        allowed=name in {'composition.json','narration.wav','render.json'} or re.fullmatch(r'clip-0[1-5]\.mp4',name)
        require(allowed,'Only fixed prepared-input roles may be written.')
        mime='application/json' if name.endswith('.json') else 'audio/wav' if name.endswith('.wav') else 'video/mp4'
        maximum=2_000_000 if mime=='application/json' else 100_000_000 if mime=='audio/wav' else 50_000_000
        require(len(data)<=maximum,'Prepared input exceeds its bound.')
        prefix=f'owners/{hashlib.sha256(owner_id.encode()).hexdigest()}/jobs/{job_id}/'
        object_name=prefix+('' if name=='render.json' else 'inputs/')+name
        from .cloud_storage import _code
        from .provider_execution import ProviderOutcomeUnknown
        sha=hashlib.sha256(data).hexdigest()
        def existing():
            try:
                stream,generation,_=self._download(object_name,maximum=maximum,mime=mime,expected_sha=sha,expected_size=len(data))
            except Exception as error:
                if _code(error)==404:return None
                raise
            stream.close()
            return dict(bucket=self.bucket_name,object=object_name,generation=generation,sha256=sha,bytes=len(data),content_type=mime)
        found=existing()
        if found is not None:return found
        # A role reservation distinguishes never-attempted from an ambiguous
        # prior upload that left no readable object. It never authorizes overwrite.
        claim=canonical(dict(schema_version=1,object=object_name,sha256=sha,bytes=len(data),content_type=mime))
        claim_name=object_name+'.claim.json';claim_sha=hashlib.sha256(claim).hexdigest()
        try:
            stream,_,_=self._download(claim_name,maximum=4096,mime='application/json',expected_sha=claim_sha,expected_size=len(claim))
        except Exception as error:
            if _code(error)!=404:raise
        else:
            stream.close()
            found=existing()
            if found is not None:return found
            raise ProviderOutcomeUnknown('Prepared input upload is unresolved. Read-only reconciliation only; no overwrite or automatic reupload.')
        try:self._write(claim_name,claim,'application/json')
        except Exception:
            # Claim may have committed. Do not proceed to a media write after an
            # uncertain reservation or race with another preparing worker.
            found=existing()
            if found is not None:return found
            raise ProviderOutcomeUnknown('Prepared input reservation is unresolved; no automatic write retry.') from None
        try:self._write(object_name,data,mime)
        except Exception:
            found=existing()
            if found is not None:return found
            raise ProviderOutcomeUnknown('Prepared input publication is unresolved; no automatic write retry.') from None
        found=existing()
        require(found is not None,'Prepared input disappeared before immutable readback.')
        return found



class FirestorePreparedContinuation(_FirestoreBoundary):
    """Atomically publish only a verified prepared handoff, never media generation."""
    def _ref(self,owner_id,job_id):
        require(_identifier(owner_id) and _identifier(job_id),'Invalid prepared handoff identity.')
        return self._doc(self._owner(owner_id)+'/prepared_continuations/'+job_id)
    def commit(self,job,manifest,reference,context_sha256):
        payload={'owner_id':job.owner_id,'job_id':job.id,'snapshot_sha256':job.snapshot_sha256,
                 'manifest_json':canonical(manifest).decode(),'reference':reference,'continuation_context_sha256':context_sha256}
        require(_sha(context_sha256) and len(canonical(payload))<=700_000,'Invalid bounded prepared handoff.')
        ref=self._ref(job.owner_id,job.id)
        def transaction(tx):
            existing=self._read(ref,tx)
            if existing is not None:
                require(existing==payload,'Prepared handoff changed; never replace accepted media.')
                return False
            tx.create(ref,payload);return True
        return self._run(transaction)
    def get(self,job):
        if self._read(self._ref(job.owner_id,job.id)) is None:return None
        return self.load(job)
    def load(self,job):
        value=self._read(self._ref(job.owner_id,job.id))
        require(isinstance(value,dict) and set(value)=={'owner_id','job_id','snapshot_sha256','manifest_json','reference','continuation_context_sha256'}
                and value['owner_id']==job.owner_id and value['job_id']==job.id and value['snapshot_sha256']==job.snapshot_sha256,
                'Prepared continuation does not belong to this exact job.')
        manifest=json.loads(value['manifest_json'])
        require(canonical(manifest).decode()==value['manifest_json'],'Prepared manifest is not canonical.')
        return manifest,value['reference']


class PreparedContinuationHandoffLoader:
    """Control-plane read-only loader; never synthesizes, stages or generates."""
    def __init__(self,registry,*,target):
        from .cloud_render import validate_target
        self.registry=registry;self.target=validate_target(target)
        self.release_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    def __call__(self,job):
        from .cloud_render import build_execution_request
        manifest,reference=self.registry.load(job)
        trusted=dict(job_id=job.id,owner_id=job.owner_id,snapshot_sha256=job.snapshot_sha256,frozen_job=job.snapshot)
        build_execution_request(self.target,manifest,reference,trusted_job=trusted)
        return manifest,reference


def persist_prepared_continuation(*,job,prepared,target,input_store,evidence_store,handoff_registry,enabled=False,preview_evidence_verifier=None,preview_source_verifier=None,now=None):
    """Bind final inputs and evidence before exposing a read-only run handoff.

    This does not dispatch Cloud Run, generate clips, publish a finished film or
    accept style. Failed/unknown writes surface; no overwrite or automatic retry.
    """
    from .cloud_render import validate_target,validate_manifest,build_execution_request
    require(enabled is True,'Prepared continuation persistence is disabled.')
    target=validate_target(target)
    staged=prepared['staged'];context=prepared['continuation_context'];receipts=prepared['clip_receipts']
    require(staged['owner_id']==job.owner_id and staged['job_id']==job.id and staged['snapshot_sha256']==job.snapshot_sha256,
            'Prepared media belongs to another job.')
    # Validate every path before context validation reads composition metadata.
    for path in staged['files'].values():
        path=Path(path)
        root=Path(staged['root'])
        require(root.is_absolute() and path.is_absolute() and '..' not in path.parts
                and path.resolve().is_relative_to(root.resolve()) and path.is_file()
                and not any(p.is_symlink() for p in (path,*path.parents)), 'Prepared input path escaped its workspace.')
    if context.get('schema_version')==2:
        from .private_preview import validate_preview_context
        validate_preview_context(context,prepared['continuation_context_sha256'],staged=staged,frozen=job.snapshot,clip_receipts=receipts,
            expected_report_sha256=prepared['narration_report_sha256'],expected_authorization_sha256=context['authorization_sha256'],
            preview_release_sha256=context['preview_release_sha256'],preview_evidence_verifier=preview_evidence_verifier,preview_source_verifier=preview_source_verifier,now=now)
    else:
        validate_continuation_context(context,prepared['continuation_context_sha256'],staged=staged,frozen=job.snapshot,
            clip_receipts=receipts,expected_report_sha256=prepared['narration_report_sha256'])
    require(getattr(input_store,'bucket_name',None)==target['bucket'],'Prepared writer bucket differs from reviewed worker target.')
    refs={}
    for name,path in staged['files'].items():
        path=Path(path)
        data=path.read_bytes()
        if name=='narration.wav':require(hashlib.sha256(data).hexdigest()==prepared['narration_report']['audio_sha256'],'Narration changed before persistence.')
        elif name.startswith('clip-'):
            index=int(name[5:7])-1
            require(hashlib.sha256(data).hexdigest()==receipts[index]['sha256'],'Final clip changed before persistence.')
        refs[name]=input_store.write(job.owner_id,job.id,name,data)
    manifest={'schema_version':1,'status':'DRAFT_NOT_ACTIVATED','job_id':job.id,
        'owner_namespace':hashlib.sha256(job.owner_id.encode()).hexdigest(),'source_job_sha256':job.snapshot_sha256,
        'source_order':list(job.snapshot['input_order']),'composition':refs['composition.json'],'narration':refs['narration.wav'],
        'clips':[dict(image_id=image['id'],position=i,original_sha256=image['sha256'],video=refs[f'clip-{i:02}.mp4'])
                 for i,image in enumerate(job.snapshot['images'],1)]}
    manifest['sha256']=fingerprint(manifest)
    trusted=dict(job_id=job.id,owner_id=job.owner_id,snapshot_sha256=job.snapshot_sha256,frozen_job=job.snapshot)
    validate_manifest(manifest,target,trusted_job=trusted)
    reference=input_store.write(job.owner_id,job.id,'render.json',canonical(manifest))
    build_execution_request(target,manifest,reference,trusted_job=trusted)
    from .hosted_control import Principal
    evidence_store.commit(Principal(job.owner_id),job,prepared['narration_report'],receipts,continuation_context=context)
    handoff_registry.commit(job,manifest,reference,prepared['continuation_context_sha256'])
    return {'manifest':manifest,'reference':reference,'continuation_context_sha256':prepared['continuation_context_sha256'],
            'dispatched':False,'provider_requests':0}
