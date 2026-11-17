"""HTTP auth/consent/replay boundary fixtures; no real identity/media/provider."""
import unittest
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from byron_video.hosted_control import Principal,require
from byron_video.hosted_uploads import CreatorAccess,CreatorSession
from byron_video.exact_film_control import ExactFilmService,install_exact_film_routes,APPROVE,VIEW

class Actions:
    def __init__(self):self.challenges={};self.requests={};self.records=[]
    def create_challenge(self,owner,job,artifact,action):
        key='challenge_'+str(len(self.challenges)+1).zfill(8);self.challenges[key]=(owner.subject,artifact,action)
        return dict(challenge_id=key,challenge_sha256='c'*64)
    def approve(self,owner,job,cid,sha,key,*,expected_action,expected_artifact_id):
        require(self.challenges[cid]==(owner.subject,expected_artifact_id,expected_action) and sha=='c'*64)
        if key in self.requests:return dict(record=self.requests[key],created=False)
        record=dict(action=expected_action,approval_id='approval_fixture_001',scope={'artifact_id':expected_artifact_id})
        self.requests[key]=record;self.records.append(record);return dict(record=record,created=True)
    def find_request(self,owner,job,key):return {'record':self.requests[key],'created':False} if key in self.requests else None
    def latest_approval(self,owner,job):return next((r for r in self.records if r['action']==APPROVE),None)

class ExactFilmControlChecks(unittest.TestCase):
    def setUp(self):
        self.owner='fixture_owner';self.job=SimpleNamespace(id='a'*32,owner_id=self.owner,state='draft_preview_ready',
            artifact=SimpleNamespace(artifact_id='preview_fixture_001'),snapshot={'processing_plan':{'exact_film_contract':{'fixture':True}}})
        self.actions=Actions();self.calls=[];self.active=True
        def dispatch(job,approval):self.calls.append(approval);raise TimeoutError('unknown synthetic outcome')
        dispatch.recover=lambda job,approval:None
        jobs=SimpleNamespace(get=lambda owner,id:self.job if (owner,id)==(self.owner,self.job.id) else None)
        self.service=ExactFilmService(jobs=jobs,actions=self.actions,dispatcher=dispatch,registry=SimpleNamespace(descriptor=lambda *a:None),
            artifact_reader=None,preview_registry=SimpleNamespace(verify=lambda *a:True),preview_reader=None,
            activation_check=lambda:self.active,enabled=True)
        def identity(request):
            owner=request.headers.get('x-fixture-owner');return CreatorSession(Principal(owner),'s'*32) if owner else None
        app=FastAPI();install_exact_film_routes(app,service=self.service,creator_access=CreatorAccess(identity,'https://studio.example'))
        self.client=TestClient(app);self.headers={'x-fixture-owner':self.owner,'Origin':'https://studio.example','X-CSRF-Token':'s'*32,'Idempotency-Key':'action_reference_0001'}
        self.path='/api/jobs/'+self.job.id
    def tearDown(self):self.client.close()
    def challenge(self,action='approve'):
        path='/film-approval' if action=='approve' else '/preview-view-renewal'
        field='preview_id' if action=='approve' else 'artifact_id'
        response=self.client.post(self.path+path+'/challenge',json={field:self.job.artifact.artifact_id},headers=self.headers)
        self.assertEqual(response.status_code,200)
        return path,{field:self.job.artifact.artifact_id,**response.json()}
    def test_owner_csrf_origin_and_extra_authority_fields_fail_closed(self):
        body={'preview_id':self.job.artifact.artifact_id}
        self.assertEqual(self.client.post(self.path+'/film-approval/challenge',json=body).status_code,401)
        for headers in [{**self.headers,'x-fixture-owner':'other_owner'},{**self.headers,'Origin':'https://other.example'},{**self.headers,'X-CSRF-Token':'bad'}]:
            self.assertGreaterEqual(self.client.post(self.path+'/film-approval/challenge',json=body,headers=headers).status_code,400)
        self.assertEqual(self.client.post(self.path+'/film-approval/challenge',json={**body,'public_release':True},headers=self.headers).status_code,422)
        self.assertEqual(len(self.actions.challenges),0);self.assertEqual(self.calls,[])
    def test_exact_owner_approval_dispatches_once_and_unknown_stays_status_only(self):
        path,body=self.challenge()
        for _ in range(2):
            result=self.client.post(self.path+path,json=body,headers=self.headers)
            self.assertEqual(result.status_code,202);self.assertTrue(result.json()['recorded'])
        self.assertEqual(len(self.calls),1)
        for _ in range(2):
            result=self.client.get(self.path+'/exact-film?reference=action_reference_0001',headers=self.headers)
            self.assertEqual(result.json()['status'],'HOLD');self.assertTrue(result.json()['action_recorded'])
        self.assertEqual(len(self.calls),1)
    def test_view_renewal_never_dispatches_and_cannot_consume_render_challenge(self):
        path,body=self.challenge('renew')
        result=self.client.post(self.path+path,json=body,headers=self.headers)
        self.assertEqual(result.status_code,200);self.assertEqual(result.json()['action'],'renew');self.assertEqual(self.calls,[])
        _,render=self.challenge()
        render['artifact_id']=render.pop('preview_id')
        result=self.client.post(self.path+'/preview-view-renewal',json=render,headers={**self.headers,'Idempotency-Key':'other_reference_0001'})
        self.assertEqual(result.status_code,409);self.assertEqual(len(self.actions.records),1);self.assertEqual(self.calls,[])
    def test_disabled_or_unsigned_job_intent_blocks_actions(self):
        self.active=False
        self.assertEqual(self.client.post(self.path+'/film-approval/challenge',json={'preview_id':self.job.artifact.artifact_id},headers=self.headers).status_code,503)
        self.active=True;self.job.snapshot={'processing_plan':{}}
        self.assertEqual(self.client.post(self.path+'/film-approval/challenge',json={'preview_id':self.job.artifact.artifact_id},headers=self.headers).status_code,409)
        self.assertEqual(self.actions.challenges,{})

    def test_independently_terminal_worker_without_output_is_hold_not_queued_forever(self):
        path,body=self.challenge()
        self.client.post(self.path+path,json=body,headers=self.headers)
        self.service.dispatcher.recover=lambda job,approval:object()
        for observed,expected in [('queued','queued'),('HOLD','HOLD'),('complete','HOLD')]:
            self.service.dispatcher.pending_status=lambda job,approval,receipt:observed
            result=self.client.get(self.path+'/exact-film',headers=self.headers)
            self.assertEqual(result.status_code,200);self.assertEqual(result.json()['status'],expected)
            self.assertNotIn('download_url',result.json())
        self.assertEqual(len(self.calls),1)
