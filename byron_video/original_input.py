"""Versioned originals-only Cloud Run invocation; no media/provider work in HTTP.

The durable Firestore job is the input manifest: selected original identities,
content hashes and immutable storage generations are already frozen there.
Only opaque job/owner namespace and independently pinned hashes cross the run
boundary. The worker must independently reload that exact private snapshot.
"""
from dataclasses import fields
import hashlib
from pathlib import Path
import re
from .cloud_render import validate_target, validate_deployed_target
from .hosted_control import Principal, VerifiedImage, digest
from .hosted_contract import freeze_job
from .worker_binding import CloudRunDispatcher, DispatchPending, require

PROTOCOL = 'originals-v1'
ENV_KEYS = frozenset({'BYRON_INPUT_PROTOCOL','BYRON_JOB_ID','BYRON_OWNER_NAMESPACE','BYRON_SNAPSHOT_SHA256','BYRON_CONFIG_SHA256'})


def validate_original_job(job, config_sha256):
    require(isinstance(config_sha256,str) and re.fullmatch(r'[0-9a-f]{64}',config_sha256), 'Reviewed configuration fingerprint required.')
    snapshot=job.snapshot
    require(snapshot.get('runtime_config_sha256')==config_sha256 and digest(snapshot)==job.snapshot_sha256,
            'Generation configuration or frozen job changed.')
    require(isinstance(job.id,str) and re.fullmatch(r'[a-f0-9]{32}',job.id), 'Opaque committed job required.')
    owner=Principal(job.owner_id)
    images=snapshot.get('images')
    require(isinstance(images,list) and 4<=len(images)<=5 and snapshot.get('input_order')==[i.get('id') for i in images if isinstance(i,dict)],
            'Four or five frozen ordered originals are required.')
    names={f.name for f in fields(VerifiedImage)}
    records=[]
    for position,image in enumerate(images,1):
        require(isinstance(image,dict) and set(image)==names|{'position'} and type(image['position']) is int and image['position']==position,
                'Frozen original metadata or position differs.')
        record=VerifiedImage(**{k:image[k] for k in names});record.validate(owner,record.id)
        require(re.fullmatch(r'[1-9][0-9]{0,19}',record.storage_version),'Originals must use immutable Google Storage generations.')
        records.append(record)
    require(len({r.id for r in records})==len(records), 'Each original must have a unique owner-bound identity.')
    plan=snapshot.get('processing_plan')
    require(isinstance(plan,dict) and digest(plan)==snapshot.get('processing_plan_sha256'), 'Processing plan changed.')
    contract=freeze_job(snapshot.get('raw_script'),
        [dict(id=r.id,position=i,sha256=r.sha256) for i,r in enumerate(records,1)],
        plan['narration_profile'],plan['visual_style_sha256'])
    require(contract==snapshot.get('narration_contract') and contract['narrated_text']==snapshot.get('narrated_text')
        and contract['narrated_text_sha256']==snapshot.get('narrated_text_sha256'), 'Exact script or selected voice differs from the frozen contract.')
    return snapshot


def build_original_execution_request(target, job, config_sha256):
    target=validate_target(target);validate_original_job(job,config_sha256)
    values={'BYRON_INPUT_PROTOCOL':PROTOCOL,'BYRON_JOB_ID':job.id,
        'BYRON_OWNER_NAMESPACE':hashlib.sha256(job.owner_id.encode()).hexdigest(),
        'BYRON_SNAPSHOT_SHA256':job.snapshot_sha256,'BYRON_CONFIG_SHA256':config_sha256}
    name=f"projects/{target['project_id']}/locations/{target['region']}/jobs/{target['job_name']}"
    return {'method':'POST','url':f'https://run.googleapis.com/v2/{name}:run',
        'json':{'overrides':{'taskCount':1,'timeout':f"{target['task_timeout_seconds']}s",
            'containerOverrides':[{'name':'renderer','env':[{'name':k,'value':v} for k,v in sorted(values.items())]}]}},
        'input_protocol':PROTOCOL}


class OriginalInputRequestLoader:
    def __init__(self, config_sha256):
        require(isinstance(config_sha256,str) and re.fullmatch(r'[0-9a-f]{64}',config_sha256), 'Reviewed configuration fingerprint required.')
        self.config_sha256=config_sha256
        self.release_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    def __call__(self,job,target):
        return build_original_execution_request(target,job,self.config_sha256)


class OriginalInputCloudRunDispatcher(CloudRunDispatcher):
    """One run invocation for an original-input generation+render worker.

    No provider call or media preparation is made by this control-plane adapter.
    Existing dispatch journal HOLD/unknown-outcome semantics remain unchanged.
    """
    def __init__(self,*,target,target_provider,store,journal,request_loader,transport,
                 execution_resolver=None,enabled=False,activation_check=None):
        require(type(request_loader) is OriginalInputRequestLoader,'Use the exact versioned original-input request loader.')
        super().__init__(target=target,target_provider=target_provider,store=store,journal=journal,
            handoff_loader=request_loader,transport=transport,execution_resolver=execution_resolver,
            enabled=enabled,activation_check=activation_check)
        self.request_loader=request_loader
        self.job_preflight=None

    def __call__(self,handoff):
        self._check_activation()
        job=self.store.get(handoff['owner_id'],handoff['job_id'])
        require(job is not None and job.id==handoff['job_id'] and job.owner_id==handoff['owner_id']
            and job.state=='HOLD' and job.receipt is None and job.snapshot_sha256==handoff['snapshot_sha256']
            and job.snapshot==handoff['frozen_job'],'Committed original-input HOLD checkpoint required.')
        if self.job_preflight is not None:
            require(self.job_preflight(job) is True,'Signed per-job production admission failed before Cloud Run submission.')
        validate_deployed_target(self.target_provider(),self.target)
        self._check_activation()
        request=self.request_loader(job,self.target)
        self._check_activation()
        created=self.journal.reserve_dispatch(job.owner_id,job.id,job.snapshot_sha256,digest(request))
        require(created is True,'Generation dispatch already armed; recover without another POST.')
        try:
            def final_check():
                self._check_activation()
                current=self.store.get(job.owner_id,job.id)
                require(current is not None and current.state=='HOLD' and current.receipt is None
                    and current.snapshot_sha256==handoff['snapshot_sha256']
                    and digest(current.snapshot)==handoff['snapshot_sha256']
                    and current.owner_id==job.owner_id and current.id==job.id,
                    'Original job changed before final dispatch.')
                if self.job_preflight is not None:
                    require(self.job_preflight(current) is True,'Generation authorization expired before transmission.')
                return True
            final_check()
            if hasattr(self.transport,'submit_authorized'):
                result=self.transport.submit_authorized('POST',request['url'],request['json'],final_check)
            else:
                # Explicit offline transport doubles only; assembled production
                # uses the final-gated transport after workload-token acquisition.
                result=self.transport('POST',request['url'],request['json'])
            operation=result.get('name') if isinstance(result,dict) else None
            prefix=f"projects/{self.target['project_id']}/locations/{self.target['region']}/operations/"
            require(isinstance(operation,str) and operation.startswith(prefix)
                and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',operation[len(prefix):]),'No verified generation operation receipt.')
            self.journal.record_dispatch_operation(job.owner_id,job.id,job.snapshot_sha256,operation)
            return self._receipt(operation,job)
        except Exception:
            raise DispatchPending('Generation run may exist; recover the recorded operation, never submit a second run.') from None
