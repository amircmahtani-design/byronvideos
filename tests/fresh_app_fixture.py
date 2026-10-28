"""Reusable full private HTTP graph with fake Google/cloud responses, no network.

Actual assembly, identity allowlist, CSRF, image validation, durable adapters,
original-input request and independently verified Cloud Run receipt are exercised.
No provider media is prepared here; a worker test must execute the queued job.
"""
import copy
import hashlib
import io
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
from PIL import Image
from byron_video.cloud_journal import _FirestoreBoundary
from byron_video.cloud_run_readback import normalized_task
from byron_video.hosted_control import digest
from byron_video.runtime_assembly import assemble_runtime
from byron_video.voices import ElevenLabsVoiceReader
from byron_video.voice_library import FirestoreVoiceLibrary
from byron_video.google_login import FirestoreLoginJournal
from test_cloud_journal_stdlib import Client as FakeFirestore, Ref as FakeFirestoreRef
from test_cloud_storage_stdlib import FakeBucket
from test_deployment_plan_stdlib import config
from test_hosted_uploads_stdlib import plan
from test_runtime_assembly_stdlib import signed

TOKEN='synthetic.signed.session'
SUBJECT='100000000001'
VOICE='fixture_voice_2'


class FreshApplicationFixture:
    def __init__(self,config_value=None,processing_plan=None):
        self.config=copy.deepcopy(config_value or config());self.processing_plan=copy.deepcopy(processing_plan or plan())
        t=self.config['target'];self.name=f"projects/{t['project_id']}/locations/{t['region']}/jobs/{t['job_name']}"
        self.operation=f"projects/{t['project_id']}/locations/{t['region']}/operations/fixture_operation"
        self.execution=self.name+'/executions/renderer-fixture'
        self.task=dict(containers=[dict(name='renderer',image=t['container_image'],env=[],resources=dict(limits={'cpu':'2','memory':'4Gi'}))],
            serviceAccount=self.config['runtime']['worker_service_account'],maxRetries=0,timeout=str(t['task_timeout_seconds'])+'s')
        self.config['runtime']['cloud_run_task_template_sha256']=digest(normalized_task(self.task))
        self.resources={self.name:dict(name=self.name,reconciling=False,terminalCondition={'state':'CONDITION_SUCCEEDED'},template=dict(taskCount=1,parallelism=1,template=self.task))}
        self.firestore=FakeFirestore();self.firestore._database_string=f"projects/{t['project_id']}/databases/{self.config['firestore']['database']}"
        self.bucket=FakeBucket();self.storage=SimpleNamespace(project=t['project_id'],bucket=lambda name:self.bucket)
        self.owner='google_'+hashlib.sha256(('accounts.google.com\0'+SUBJECT).encode()).hexdigest()
        token_sha=hashlib.sha256(TOKEN.encode()).hexdigest()
        FirestoreLoginJournal(self.firestore,project_id=t['project_id'],namespace=self.config['firestore']['namespace'],
            transaction_runner=self.firestore.run).rotate(self.owner,token_sha,time.time()+600)
        FirestoreVoiceLibrary(self.firestore,project_id=t['project_id'],namespace=self.config['firestore']['namespace'],
            transaction_runner=self.firestore.run).add(VOICE,'Chosen synthetic narrator')
        self.run_requests=[];self.read_requests=[];self.provider_requests=[]
        self.stack=ExitStack()
        def read(url):
            self.read_requests.append(url)
            return copy.deepcopy(self.resources[url.removeprefix('https://run.googleapis.com/v2/')])
        def run(method,url,body):
            assert method=='POST' and url=='https://run.googleapis.com/v2/'+self.name+':run'
            self.run_requests.append(copy.deepcopy(body))
            number=len(self.run_requests)
            operation=self.operation if number==1 else self.operation+'_'+str(number)
            execution=self.execution if number==1 else self.execution+'-'+str(number)
            task=copy.deepcopy(self.task);task['containers'][0]['env']=body['overrides']['containerOverrides'][0]['env']
            self.resources[operation]=dict(name=operation,done=False,metadata={
                '@type':'type.googleapis.com/google.cloud.run.v2.Execution','name':execution,'uid':'fixture-uid'})
            self.resources[execution]=dict(name=execution,job=self.name,uid='fixture-uid',taskCount=1,parallelism=1,template=task)
            return {'name':operation}
        def verify(token,audience):
            assert token==TOKEN
            now=time.time()
            return dict(sub=SUBJECT,aud=audience,iss='https://accounts.google.com',iat=now-10,exp=now+600)
        self.read=read
        old_get=FakeFirestoreRef.get
        self.stack.enter_context(patch.object(FakeFirestoreRef,'get',lambda ref,transaction=None,**kwargs:old_get(ref,transaction=transaction)))
        self.stack.enter_context(patch.object(_FirestoreBoundary,'_run',lambda boundary,callback:boundary.client.run(callback)))
        self.stack.enter_context(patch('byron_video.google_identity.verify_google_id_token',side_effect=verify))
        self.stack.enter_context(patch('byron_video.runtime_assembly.CloudRunReadTransport',side_effect=lambda *args,**kwargs:read))
        self.stack.enter_context(patch('byron_video.runtime_assembly.HttpxCloudRunTransport',side_effect=lambda *args,**kwargs:run))
        self.stack.enter_context(patch.object(ElevenLabsVoiceReader,'__call__',lambda reader,owner:[dict(id=VOICE,name='Chosen synthetic narrator')]))
        secrets=SimpleNamespace(access_secret_version=lambda **kwargs:SimpleNamespace(payload=SimpleNamespace(data=VOICE.encode())),
            add_secret_version=lambda **kwargs:None)
        envelope,public,_=signed(self.config,self.processing_plan)
        self.runtime=assemble_runtime(config=self.config,processing_plan=self.processing_plan,
            firestore_client=self.firestore,storage_client=self.storage,secret_client=secrets,
            workload_token_reader=lambda:'synthetic-workload-token',csrf_secret=b'synthetic-private-csrf-secret-at-least-32',
            enabled=True,acceptance=envelope,reviewer_public_key=public)
        self.client=self.stack.enter_context(TestClient(self.runtime.app,base_url=self.config['identity']['origin']))
        self.client.cookies.set('__Host-byron_session',TOKEN)
    def close(self):self.stack.close()
    def submit(self,script='A first original.\n\nA second original.\n\nA third original.\n\nA fourth original.'):
        capability=self.client.get('/api/creator-capabilities');assert capability.status_code==200,capability.text
        self.headers={'Origin':self.config['identity']['origin'],'X-CSRF-Token':capability.json()['csrf_token'],'Idempotency-Key':'fresh_fixture_request_0001'}
        voices=self.client.get('/api/voices');assert voices.status_code==200 and voices.json()['voices'][0]['id']==VOICE
        self.originals=[]
        for index in range(4):
            buffer=io.BytesIO();Image.new('RGB',(64,64),(40+index*30,80,100)).save(buffer,format='PNG')
            self.originals.append(buffer.getvalue())
        upload=self.client.post('/api/uploads',files=[('images',(f'{i}.png',data,'image/png')) for i,data in enumerate(self.originals)],headers=self.headers)
        assert upload.status_code==201,upload.text
        self.body=dict(script=script,images=upload.json()['images'],voice_id=VOICE)
        response=self.client.post('/api/jobs',json=self.body,headers=self.headers)
        assert response.status_code==202,response.text
        self.job=self.runtime.jobs.get(self.owner,response.json()['id'])
        return self.job
