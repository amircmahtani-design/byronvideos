"""Explicit bounded resume of a known terminal worker; never ordinary retry.

One authorization transaction archives prior execution/HOLD and opens a separate
attempt-specific dispatch journal. Frozen input/plan/cost journals are untouched.
Unknown paid operations are not replayed; rendering already attempted requires
separate artifact reconciliation, not this resume path. Default routes disabled.
"""
from dataclasses import replace
import hashlib
import re
from fastapi import HTTPException, Request
from .cloud_journal import _FirestoreBoundary, FirestoreJobStore, FirestoreDispatchJournal
from .hosted_control import Principal, digest, invoke, require
from .original_input import OriginalInputCloudRunDispatcher

MAX_RESUMES=3
RESUMABLE=frozenset({'queue_deadline','source_review','prompt_review','output_review','motion_capture'})
KEY=re.compile(r'[A-Za-z0-9_-]{16,128}\Z')


class FirestoreResumeJournal(_FirestoreBoundary):
    def _paths(self,owner,job_id):
        require(isinstance(job_id,str) and re.fullmatch(r'[0-9a-f]{32}',job_id),'Invalid private job.')
        base=self._owner(owner)
        return base,self._doc(base+'/resumes/'+job_id),self._doc(base+'/jobs/'+job_id)
    def _attempt(self,base,job_id,number):
        require(type(number) is int and 1<=number<=MAX_RESUMES,'Resume attempt limit reached.')
        return self._doc(base+'/resume_attempts/'+job_id+'_'+str(number))
    def _validate(self,record,job):
        require(isinstance(record,dict) and set(record)=={'schema_version','owner_id','job_id','snapshot_sha256','config_sha256',
            'attempt','key_sha256','requested_by','prior_receipt_json','prior_hold','record_sha256'}
            and record['schema_version']==1 and type(record['schema_version']) is int
            and record['owner_id']==job.owner_id and record['job_id']==job.id and record['snapshot_sha256']==job.snapshot_sha256
            and record['config_sha256']==job.snapshot.get('runtime_config_sha256')
            and type(record['attempt']) is int and 1<=record['attempt']<=MAX_RESUMES
            and digest({k:v for k,v in record.items() if k!='record_sha256'})==record['record_sha256'],
            'Authorized resume record is missing or changed.')
        return record
    def find_request(self,job,key):
        require(isinstance(key,str) and KEY.fullmatch(key),'Stable resume reference required.')
        base,_,_=self._paths(job.owner_id,job.id)
        ref=self._doc(base+'/resume_keys/'+job.id+'_'+hashlib.sha256(key.encode()).hexdigest())
        value=self._read(ref)
        if value is None:return None
        record=self._validate(self._read(self._attempt(base,job.id,value['attempt'])),job)
        require(record['key_sha256']==hashlib.sha256(key.encode()).hexdigest() and value['record_sha256']==record['record_sha256'])
        return record
    def reserve(self,job,key,requested_by):
        """Caller must independently verify THIS exact prior execution is terminal.

        The current stored job must still equal the verified record at commit.
        Unknown commit outcomes never authorize a second dispatch automatically.
        """
        require(isinstance(key,str) and KEY.fullmatch(key),'Stable resume reference required.')
        Principal(requested_by)
        base,master_ref,job_ref=self._paths(job.owner_id,job.id)
        key_hash=hashlib.sha256(key.encode()).hexdigest();key_ref=self._doc(base+'/resume_keys/'+job.id+'_'+key_hash)
        hold_ref=self._doc(base+'/worker_holds/'+job.id);render_ref=self._doc(base+'/worker_attempts/'+job.id)
        def transaction(tx):
            existing=self._read(key_ref,tx)
            current=FirestoreJobStore._record(self._read(job_ref,tx),job.owner_id,job.id)
            master=self._read(master_ref,tx);hold=self._read(hold_ref,tx);render=self._read(render_ref,tx)
            if existing is not None:
                record=self._validate(self._read(self._attempt(base,job.id,existing['attempt']),tx),job)
                require(existing['record_sha256']==record['record_sha256'] and record['key_sha256']==key_hash)
                return record,False
            require(current==job and job.state=='HOLD' and job.receipt is not None and job.artifact is None,
                'Resume requires the exact independently terminal held execution; no unknown dispatch retry.')
            require(render is None,'Rendering was already attempted; reconcile the artifact without rerendering.')
            require(isinstance(hold,dict) and hold.get('owner_id')==job.owner_id and hold.get('job_id')==job.id
                and hold.get('snapshot_sha256')==job.snapshot_sha256 and hold.get('execution_id')==job.receipt.execution_id
                and hold.get('reason') in RESUMABLE,'This HOLD needs explicit receipt/artifact reconciliation, not a new worker.')
            if master is not None:
                require(master.get('owner_id')==job.owner_id and master.get('job_id')==job.id
                    and master.get('snapshot_sha256')==job.snapshot_sha256,'Resume history changed.')
                previous=self._validate(self._read(self._attempt(base,job.id,master['attempt']),tx),job)
                require(master.get('record_sha256')==previous['record_sha256'])
            number=1 if master is None else master['attempt']+1
            attempt_ref=self._attempt(base,job.id,number)
            require(self._read(attempt_ref,tx) is None,'Resume attempts are append-only.')
            config_sha=job.snapshot.get('runtime_config_sha256')
            require(isinstance(config_sha,str) and re.fullmatch(r'[0-9a-f]{64}',config_sha),'Frozen runtime configuration required.')
            record=dict(schema_version=1,owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,
                config_sha256=config_sha,attempt=number,key_sha256=key_hash,requested_by=requested_by,
                prior_receipt_json=job.receipt_json,prior_hold=hold)
            record['record_sha256']=digest(record)
            pointer=dict(owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,attempt=number,record_sha256=record['record_sha256'])
            tx.create(attempt_ref,record);tx.create(key_ref,dict(attempt=number,record_sha256=record['record_sha256']))
            (tx.create if master is None else tx.update)(master_ref,pointer)
            tx.update(job_ref,dict(state='HOLD',receipt_json=None))
            return record,True
        return self._run(transaction)
    def eligibility(self,job,*,ui=False):
        base,master_ref,_=self._paths(job.owner_id,job.id)
        require(job.state=='HOLD' and job.receipt is not None and job.artifact is None,'Known held worker required.')
        hold=self._read(self._doc(base+'/worker_holds/'+job.id))
        render=self._read(self._doc(base+'/worker_attempts/'+job.id))
        allowed={'queue_deadline','motion_capture'} if ui else RESUMABLE
        require(render is None and isinstance(hold,dict) and hold.get('owner_id')==job.owner_id
            and hold.get('job_id')==job.id and hold.get('snapshot_sha256')==job.snapshot_sha256
            and hold.get('execution_id')==job.receipt.execution_id and hold.get('reason') in allowed,
            'This HOLD requires review or outcome reconciliation.')
        master=self._read(master_ref)
        attempt=0
        if master is not None:
            record=self._validate(self._read(self._attempt(base,job.id,master['attempt'])),job)
            require(master.get('record_sha256')==record['record_sha256'])
            attempt=record['attempt']
        require(attempt<MAX_RESUMES,'Resume attempt limit reached.')
        return MAX_RESUMES-attempt
    def active_dispatch(self,job):
        base,master_ref,_=self._paths(job.owner_id,job.id);master=self._read(master_ref)
        if master is None:return None
        require(master.get('owner_id')==job.owner_id and master.get('job_id')==job.id
            and master.get('snapshot_sha256')==job.snapshot_sha256,'Resume pointer changed.')
        record=self._validate(self._read(self._attempt(base,job.id,master['attempt'])),job)
        require(master.get('record_sha256')==record['record_sha256'])
        return ResumeDispatchJournal(self,record)


class ResumeDispatchJournal(FirestoreDispatchJournal):
    def __init__(self,resumes,record):
        self.client,self.root,self._runner=resumes.client,resumes.root,resumes._runner
        self.record=record;self.attempt=record['attempt']
    def _ref(self,owner_id,job_id):
        require(owner_id==self.record['owner_id'] and job_id==self.record['job_id'],'Resume scope changed.')
        base=self._owner(owner_id);pointer=self._read(self._doc(base+'/resumes/'+job_id))
        require(pointer is not None and pointer['attempt']==self.attempt and pointer['record_sha256']==self.record['record_sha256'],
                'A stale resume attempt cannot dispatch or recover.')
        return self._doc(base+'/resume_dispatches/'+job_id+'_'+str(self.attempt))
    def _snapshot(self,value):
        require(value==self.record['snapshot_sha256'],'Resume cannot change frozen inputs.')
    def reserve_dispatch(self,owner_id,job_id,snapshot_sha256,request_sha256):
        self._snapshot(snapshot_sha256)
        return super().reserve_dispatch(owner_id,job_id,snapshot_sha256,request_sha256)
    def record_dispatch_operation(self,owner_id,job_id,snapshot_sha256,operation):
        self._snapshot(snapshot_sha256)
        return super().record_dispatch_operation(owner_id,job_id,snapshot_sha256,operation)
    def get_dispatch_operation(self,owner_id,job_id,snapshot_sha256):
        self._snapshot(snapshot_sha256)
        return super().get_dispatch_operation(owner_id,job_id,snapshot_sha256)


class ResumeService:
    def __init__(self,*,jobs,resumes,dispatcher,terminal_verifier,activation_check,enabled=False):
        require(type(dispatcher) is OriginalInputCloudRunDispatcher,'Explicit original-input dispatcher required.')
        self.jobs,self.resumes,self.dispatcher=jobs,resumes,dispatcher
        self.terminal_verifier,self.activation_check,self.enabled=terminal_verifier,activation_check,enabled is True
    def _check(self):
        require(self.enabled and callable(self.activation_check) and self.activation_check() is True,'Resume is not activated.')
    def availability(self,owner,job):
        result=dict(can_resume=False,resume_attempts_remaining=0,resume_attempt=0,resume_outcome_known=False)
        try:
            self._check()
            require(job.owner_id==owner.subject and job.snapshot.get('runtime_config_sha256')==self.dispatcher.request_loader.config_sha256)
            active=self.resumes.active_dispatch(job)
            if active is not None:
                result.update(resume_attempt=active.attempt,resume_outcome_known=job.receipt is not None)
            remaining=self.resumes.eligibility(job,ui=True)
            require(self.terminal_verifier(job) is True,'Prior execution is not stopped.')
            result.update(can_resume=True,resume_attempts_remaining=remaining)
        except Exception:
            pass
        return result
    def resume(self,owner,job_id,key,requested_by):
        self._check();job=self.jobs.get(owner.subject,job_id)
        if job is None:raise HTTPException(404,'Film not found.')
        existing=self.resumes.find_request(job,key)
        if existing is not None:return self._result(job,existing['attempt'],True)
        if job.state!='HOLD' or job.receipt is None or job.artifact is not None:
            raise HTTPException(409,'Only a known held worker can be resumed. Check film status; no new run was sent.')
        if self.terminal_verifier(job) is not True:
            raise HTTPException(409,'The previous worker is not independently confirmed stopped. No new run was sent.')
        self._check();record,created=self.resumes.reserve(job,key,requested_by)
        if not created:return self._result(self.jobs.get(owner.subject,job_id),record['attempt'],True)
        current=self.jobs.get(owner.subject,job_id);journal=self.resumes.active_dispatch(current)
        previous=self.dispatcher
        dispatcher=OriginalInputCloudRunDispatcher(target=previous.target,target_provider=previous.target_provider,
            store=self.jobs,journal=journal,request_loader=previous.request_loader,transport=previous.transport,
            execution_resolver=previous.execution_resolver,enabled=True,activation_check=self.activation_check)
        handoff=dict(owner_id=current.owner_id,job_id=current.id,snapshot_sha256=current.snapshot_sha256,frozen_job=current.snapshot)
        try:
            receipt=dispatcher(handoff);self.jobs.record_dispatch(owner.subject,job_id,receipt)
        except Exception:
            # Authorized attempt stays recorded. Never infer failure and re-POST.
            pass
        return self._result(self.jobs.get(owner.subject,job_id),record['attempt'],False)
    @staticmethod
    def _result(job,attempt,reused):
        return dict(id=job.id,status=job.state,resume_attempt=attempt,reused=reused,automatic_retries=0)


def install_resume_routes(app,*,service=None,creator_access=None,admin_access=None,allowed_owners=()):
    allowed=frozenset(allowed_owners)
    async def run(request,owner,job_id,requested_by):
        key=request.headers.get('idempotency-key','')
        if not KEY.fullmatch(key):raise HTTPException(422,'A stable resume reference is required.')
        # No body, plan, quote, spending limit or replacement input is accepted.
        if await request.body():raise HTTPException(422,'Resume the unchanged film without a request body.')
        try:return await invoke(service.resume,owner,job_id,key,requested_by)
        except HTTPException:raise
        except Exception:raise HTTPException(409,'Resume could not be confirmed. Check this same film; do not repeat or replace its inputs.') from None
    @app.post('/api/jobs/{job_id}/resume',status_code=202)
    async def creator_resume(job_id:str,request:Request):
        if service is None or creator_access is None:raise HTTPException(503,'Resume is not activated.')
        session=await creator_access.authorize(request)
        return await run(request,session.principal,job_id,session.principal.subject)
    @app.post('/api/admin/jobs/{owner_id}/{job_id}/resume',status_code=202)
    async def admin_resume(owner_id:str,job_id:str,request:Request):
        if service is None or admin_access is None:raise HTTPException(503,'Administrator resume is not activated.')
        session=await admin_access.authorize(request)
        if owner_id not in allowed:raise HTTPException(404,'Film not found.')
        return await run(request,Principal(owner_id),job_id,session.principal.subject)
