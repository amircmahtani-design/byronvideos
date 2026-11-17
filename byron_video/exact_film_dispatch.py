"""One-shot render-only Cloud Run invocation, separate from paid generation.

Imports/construction perform no network I/O. An uncertain submission is never
retried. Recovery only reads the already-recorded Cloud Run operation.
"""
import hashlib
import json
import re
from dataclasses import asdict
from .cloud_journal import _FirestoreBoundary
from .cloud_render import validate_target,validate_deployed_target
from .hosted_control import canonical,digest,require,DispatchReceipt
from .worker_binding import DispatchPending

PROTOCOL='exact-film-v1'
SHA=re.compile(r'[a-f0-9]{64}')
ENV_NAMES={'protocol':'BYRON_EXACT_FILM_PROTOCOL','job_id':'BYRON_JOB_ID',
 'owner_namespace':'BYRON_OWNER_NAMESPACE','snapshot_sha256':'BYRON_SNAPSHOT_SHA256',
 'config_sha256':'BYRON_CONFIG_SHA256','approval_sha256':'BYRON_FILM_APPROVAL_SHA256'}


def build_request(target,job,config_sha256,approval_sha256):
    target=validate_target(target)
    require(job.state=='draft_preview_ready' and job.artifact is not None,'Retained private draft required.')
    require(SHA.fullmatch(config_sha256) and SHA.fullmatch(approval_sha256)
        and job.snapshot.get('runtime_config_sha256')==config_sha256,'Exact approved runtime required.')
    values=dict(protocol=PROTOCOL,job_id=job.id,owner_namespace=hashlib.sha256(job.owner_id.encode()).hexdigest(),
        snapshot_sha256=job.snapshot_sha256,config_sha256=config_sha256,approval_sha256=approval_sha256)
    name=f"projects/{target['project_id']}/locations/{target['region']}/jobs/{target['job_name']}"
    return {'method':'POST','url':f'https://run.googleapis.com/v2/{name}:run',
        'json':{'overrides':{'taskCount':1,'timeout':f"{target['task_timeout_seconds']}s",
        'containerOverrides':[{'name':'renderer','env':[{'name':ENV_NAMES[k],'value':v} for k,v in sorted(values.items())]}]}},
        'input_protocol':PROTOCOL}


def invocation_from_environment(env,config):
    value={k:env.get(v) for k,v in ENV_NAMES.items()};value['execution_id']=env.get('CLOUD_RUN_EXECUTION')
    return validate_invocation(value,config)


def validate_invocation(value,config):
    require(isinstance(value,dict) and set(value)==set(ENV_NAMES)|{'execution_id'}
        and all(isinstance(v,str) for v in value.values()),'Complete exact-film invocation required.')
    require(value['protocol']==PROTOCOL and re.fullmatch(r'[a-f0-9]{32}',value['job_id'])
        and all(SHA.fullmatch(value[k]) for k in ('owner_namespace','snapshot_sha256','config_sha256','approval_sha256'))
        and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',value['execution_id'])
        and value['config_sha256']==digest(config),'Exact-film invocation binding differs.')
    return dict(value)


class FirestoreExactFilmDispatchJournal(_FirestoreBoundary):
    def _ref(self,job,approval_sha):
        require(SHA.fullmatch(approval_sha),'Exact approval fingerprint required.')
        # One clean render per frozen job, even if a later approval is renewed.
        return self._doc(self._owner(job.owner_id)+'/exact_film_dispatch/'+job.id)
    def _base(self,job,approval_sha):
        return dict(owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,approval_sha256=approval_sha)
    def get(self,job,approval_sha):
        value=self._read(self._ref(job,approval_sha))
        if value is not None:
            require(all(value.get(k)==v for k,v in self._base(job,approval_sha).items()),'Clean dispatch lineage differs.')
        return value
    def get_receipt(self,job,approval):
        record=self.get(job,digest(approval))
        if record is None or not record.get('receipt_json'):return None
        receipt=DispatchReceipt(**json.loads(record['receipt_json']));receipt.validate(job)
        return receipt
    def reserve(self,job,approval_sha,request_sha):
        ref=self._ref(job,approval_sha);base=self._base(job,approval_sha)
        require(SHA.fullmatch(request_sha),'Exact request fingerprint required.')
        def run(tx):
            prior=self._read(ref,tx)
            if prior is not None:
                require(all(prior.get(k)==v for k,v in base.items()) and prior.get('request_sha256')==request_sha,'Dispatch changed.')
                return False
            tx.create(ref,{**base,'request_sha256':request_sha,'operation':None,'receipt_json':None});return True
        return self._run(run)
    def operation(self,job,approval_sha,operation):
        ref=self._ref(job,approval_sha)
        def run(tx):
            prior=self._read(ref,tx)
            require(prior is not None and all(prior.get(k)==v for k,v in self._base(job,approval_sha).items()),'Dispatch was not reserved.')
            require(prior.get('operation') in (None,operation),'Operation cannot change.')
            tx.update(ref,{'operation':operation})
        self._run(run)
    def receipt(self,job,approval_sha,receipt):
        receipt.validate(job);ref=self._ref(job,approval_sha)
        # An execution cannot be reused by a different approval or owner.
        unique=self._doc('exact_film_executions/'+hashlib.sha256(receipt.execution_id.encode()).hexdigest())
        data={**self._base(job,approval_sha),'receipt_json':canonical(asdict(receipt))}
        def run(tx):
            prior=self._read(ref,tx);used=self._read(unique,tx)
            require(prior is not None and prior.get('operation') and all(prior.get(k)==v for k,v in self._base(job,approval_sha).items()),'No recorded operation.')
            require(prior.get('receipt_json') in (None,data['receipt_json']) and (used is None or used==data),'Execution receipt changed/reused.')
            if used is None:tx.create(unique,data)
            tx.update(ref,{'receipt_json':data['receipt_json']})
        self._run(run);return receipt


class ExactFilmDispatcher:
    def __init__(self,*,target,config_sha256,journal,approval_loader,approval_validator,
                 readback,transport,activation_check,enabled=False,job_preflight=None):
        self.target=validate_target(target);self.config_sha256=config_sha256
        self.journal,self.approval_loader,self.approval_validator=journal,approval_loader,approval_validator
        self.readback,self.transport,self.activation_check=readback,transport,activation_check
        self.enabled=enabled is True;self.job_preflight=job_preflight;self._required_preflight=job_preflight
    def bind_job_preflight(self,callback):
        require(callable(callback) and self._required_preflight is None,'Bind one explicit render-only admission checker.')
        self.job_preflight=callback;self._required_preflight=callback
    def _check(self,job,approval):
        require(self.enabled and self.activation_check() is True,'Exact-film rendering is disabled.')
        require(self.job_preflight is self._required_preflight,'Render-only admission binding changed.')
        if self.job_preflight is not None:require(self.job_preflight(job) is True,'Current render-only compute consent is required.')
        loaded=self.approval_loader(job)
        require(loaded==approval and self.approval_validator(job,loaded) is True,'Current independent this-film authorization required.')
        return digest(loaded)
    def request_loader(self,job,target):
        approval=self.approval_loader(job)
        return build_request(target,job,self.config_sha256,digest(approval))
    def _receipt(self,job,approval,operation):
        sha=self._check(job,approval)
        binding=self.readback.execution_resolver(operation,self.target,job)
        prefix=f"projects/{self.target['project_id']}/locations/{self.target['region']}/jobs/{self.target['job_name']}/executions/"
        require(isinstance(binding,dict) and binding.get('operation_name')==operation
          and binding.get('snapshot_sha256')==job.snapshot_sha256 and binding.get('target_sha256')==digest(self.target)
          and isinstance(binding.get('execution_name'),str) and binding['execution_name'].startswith(prefix),'Exact clean execution readback required.')
        receipt=DispatchReceipt(job.id,job.snapshot_sha256,binding['execution_name'][len(prefix):]);receipt.validate(job)
        self._check(job,approval)
        return self.journal.receipt(job,sha,receipt)
    def __call__(self,job,approval):
        sha=self._check(job,approval)
        validate_deployed_target(self.readback.target_provider(),self.target)
        request=self.request_loader(job,self.target);self._check(job,approval)
        if not self.journal.reserve(job,sha,digest(request)):
            return self.recover(job,approval)
        try:
            self._check(job,approval)
            final_check=lambda:bool(self._check(job,approval))
            if hasattr(self.transport,'submit_authorized'):
                result=self.transport.submit_authorized('POST',request['url'],request['json'],final_check)
            else:
                # Explicit offline transport doubles; production assembly always
                # supplies the concrete final-gated workload transport.
                result=self.transport('POST',request['url'],request['json'])
            operation=result.get('name') if isinstance(result,dict) else None
            prefix=f"projects/{self.target['project_id']}/locations/{self.target['region']}/operations/"
            require(isinstance(operation,str) and operation.startswith(prefix)
                and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',operation[len(prefix):]),'No exact render operation receipt.')
            self.journal.operation(job,sha,operation)
            return self._receipt(job,approval,operation)
        except Exception:
            raise DispatchPending('Clean render may exist; retain HOLD and reconcile this operation without another POST.') from None
    def pending_status(self,job,approval,receipt):
        """A terminal execution without a committed artifact is HOLD, not queued."""
        self._check(job,approval)
        require(self.journal.get_receipt(job,approval)==receipt,'Exact clean receipt is unavailable.')
        receipt.validate(job)
        terminal=self.readback.terminal_execution_with_receipt(job,receipt)
        require(type(terminal) is bool,'Clean execution status could not be verified.')
        return 'HOLD' if terminal else 'queued'
    def recover(self,job,approval):
        sha=self._check(job,approval);record=self.journal.get(job,sha)
        if record is None or not record.get('operation'):
            raise DispatchPending('Clean render outcome unknown; no automatic resubmission.')
        validate_deployed_target(self.readback.target_provider(),self.target)
        return self._receipt(job,approval,record['operation'])
