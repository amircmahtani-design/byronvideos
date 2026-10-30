"""Explicit owner-only exact-film approval and retained-artifact view renewal."""
from fastapi import HTTPException,Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel,ConfigDict,StrictStr,Field
from .hosted_control import Principal,require,invoke,_verified_spool,JOB_ID
from .resume_control import KEY

APPROVE='RENDER_THIS_FILM_PRIVATELY_WITHOUT_DRAFT_WATERMARK'
VIEW='VIEW_THIS_PRIVATE_ARTIFACT'

class PreviewTarget(BaseModel):
    model_config=ConfigDict(extra='forbid')
    preview_id:StrictStr=Field(pattern=r'^[A-Za-z0-9_-]{8,128}$')
class ArtifactTarget(BaseModel):
    model_config=ConfigDict(extra='forbid')
    artifact_id:StrictStr=Field(pattern=r'^[A-Za-z0-9_-]{8,128}$')
class PreviewConsent(PreviewTarget):
    challenge_id:StrictStr=Field(pattern=r'^[A-Za-z0-9_-]{8,128}$')
    challenge_sha256:StrictStr=Field(pattern=r'^[0-9a-f]{64}$')
class ArtifactConsent(ArtifactTarget):
    challenge_id:StrictStr=Field(pattern=r'^[A-Za-z0-9_-]{8,128}$')
    challenge_sha256:StrictStr=Field(pattern=r'^[0-9a-f]{64}$')

class ExactFilmService:
    def __init__(self,*,jobs,actions,dispatcher,registry,artifact_reader,preview_registry,preview_reader,activation_check,enabled=False):
        self.jobs,self.actions,self.dispatcher=jobs,actions,dispatcher
        self.registry,self.artifact_reader=registry,artifact_reader
        self.preview_registry,self.preview_reader=preview_registry,preview_reader
        self.activation_check,self.enabled=activation_check,enabled is True
    def check(self):
        require(self.enabled and callable(self.activation_check) and self.activation_check() is True,'Exact-film actions are not activated.')
    def owned(self,owner,job_id):
        self.check()
        if not JOB_ID.fullmatch(job_id):raise HTTPException(404,'Film not found.')
        job=self.jobs.get(owner.subject,job_id)
        if job is None:raise HTTPException(404,'Film not found.')
        require(job.state=='draft_preview_ready' and job.artifact is not None,'A retained private DRAFT preview is required.')
        require(job.snapshot.get('processing_plan',{}).get('exact_film_contract') is not None,'Exact-film actions were not authorized for this frozen film.')
        return job
    def challenge(self,owner,job_id,artifact_id,action):
        job=self.owned(owner,job_id)
        if action==APPROVE:require(artifact_id==job.artifact.artifact_id,'Approve only this exact preview.')
        value=self.actions.create_challenge(owner,job,artifact_id,action)
        return {key:value[key] for key in ('challenge_id','challenge_sha256','expires_at') if key in value}
    def approve(self,owner,job_id,artifact_id,action,challenge_id,challenge_sha256,key):
        job=self.owned(owner,job_id)
        if action==APPROVE:require(artifact_id==job.artifact.artifact_id,'Approve only this exact preview.')
        self.check()
        result=self.actions.approve(owner,job,challenge_id,challenge_sha256,key,expected_action=action,expected_artifact_id=artifact_id)
        record=result['record']
        require(record['action']==action,'A view renewal cannot authorize rendering.')
        # Authoritative challenge action/scope are reloaded by policy, never supplied by JSON.
        if action==APPROVE and result['created']:
            self.check()
            try:self.dispatcher(job,record)
            except Exception:pass  # Durable one-shot attempt; status-only reconciliation, never repeat POST.
        return dict(id=job.id,reference=key,action='approve' if action==APPROVE else 'renew',recorded=True,reused=not result['created'])
    def status(self,owner,job_id,reference=None):
        job=self.owned(owner,job_id)
        result=dict(id=job.id,enabled=True,preview_id=job.artifact.artifact_id,status='not_requested',
            can_approve=False,preview_view_available=False,clean_view_available=False,private_only=True)
        if reference is not None:
            require(KEY.fullmatch(reference),'Invalid private action reference.')
            found=self.actions.find_request(owner,job,reference)
            result.update(reference=reference,action_recorded=found is not None)
        try:
            self.preview_registry.verify(owner,job,job.artifact.artifact_id)
            result['preview_view_available']=True
        except Exception:pass
        approval=self.actions.latest_approval(owner,job)
        if approval is None:
            result['can_approve']=True
            return result
        result['status']='HOLD'
        retained=self.registry.descriptor(owner,job)
        if retained is None:
            try:
                receipt=self.dispatcher.recover(job,approval)
                if receipt is not None:
                    pending=self.dispatcher.pending_status(job,approval,receipt)
                    require(pending in {'queued','HOLD'},'Unverified clean-copy completion status.')
                    result['status']=pending
            except Exception:pass
            return result
        result.update(clean_artifact_id=retained['artifact_id'],artifact_kind='OWNER_APPROVED_EXACT_FILM',status='view_authorization_required')
        try:
            self.registry.load(owner,job)
            result.update(status='ready',clean_view_available=True,download_url='/api/jobs/'+job.id+'/approved-video')
        except Exception:pass
        return result


def install_exact_film_routes(app,*,service=None,creator_access=None):
    async def session(request):
        if service is None or creator_access is None:raise HTTPException(503,'Exact-film owner actions are not activated.')
        value=await creator_access.authorize(request)
        try:service.check()
        except Exception:raise HTTPException(503,'Exact-film runtime authorization unavailable.') from None
        return value.principal
    def key(request):
        value=request.headers.get('idempotency-key','')
        if not KEY.fullmatch(value):raise HTTPException(422,'Stable private action reference required.')
        return value
    async def call(function,*args):
        try:return await invoke(function,*args)
        except HTTPException:raise
        except Exception:raise HTTPException(409,'This private film action could not be confirmed. Check the same film; do not regenerate or repeat an uncertain action.') from None
    @app.post('/api/jobs/{job_id}/film-approval/challenge')
    async def approve_challenge(job_id:str,body:PreviewTarget,request:Request):
        owner=await session(request);return await call(service.challenge,owner,job_id,body.preview_id,APPROVE)
    @app.post('/api/jobs/{job_id}/preview-view-renewal/challenge')
    async def view_challenge(job_id:str,body:ArtifactTarget,request:Request):
        owner=await session(request);return await call(service.challenge,owner,job_id,body.artifact_id,VIEW)
    @app.post('/api/jobs/{job_id}/film-approval',status_code=202)
    async def approve(job_id:str,body:PreviewConsent,request:Request):
        owner=await session(request);return await call(service.approve,owner,job_id,body.preview_id,APPROVE,body.challenge_id,body.challenge_sha256,key(request))
    @app.post('/api/jobs/{job_id}/preview-view-renewal')
    async def renew(job_id:str,body:ArtifactConsent,request:Request):
        owner=await session(request);return await call(service.approve,owner,job_id,body.artifact_id,VIEW,body.challenge_id,body.challenge_sha256,key(request))
    @app.get('/api/jobs/{job_id}/exact-film')
    async def status(job_id:str,request:Request,reference:str|None=None):
        owner=await session(request);return await call(service.status,owner,job_id,reference)
    @app.get('/api/jobs/{job_id}/approved-video')
    async def download(job_id:str,request:Request):
        owner=await session(request);job=await call(service.owned,owner,job_id)
        try:
            value=await invoke(service.registry.load,owner,job)
            require(value is not None,'No verified clean copy is available.')
            proof,reference,render_job,approval=value
            spool=await run_in_threadpool(_verified_spool,service.artifact_reader,owner,render_job,proof)
        except Exception:raise HTTPException(503,'The retained clean copy could not be verified or its viewing authorization expired. No download was released.') from None
        def chunks():
            try:
                while chunk:=spool.read(1024*1024):yield chunk
            finally:spool.close()
        return StreamingResponse(chunks(),media_type='video/mp4',headers={
            'Content-Disposition':'attachment; filename="byron-THIS-FILM-'+job.id+'.mp4"','Content-Length':str(proof.byte_count),
            'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','X-Byron-Artifact-Kind':'OWNER_APPROVED_EXACT_FILM'},background=BackgroundTask(spool.close))
