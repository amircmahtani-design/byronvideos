"""Packaged production builder integration with synthetic SDK/HTTP boundaries.

No generation/approval/quote/native-proof/semantic callback is substituted.
Signed policies are ephemeral synthetic test approvals, not live authority.
LOCAL_UNPINNED_MEDIA_WIRING_ONLY: Google SDK metadata and the media-version
admission are explicit unittest seams. Real media uses installed local libraries,
not the required pinned production runtime; compatibility acceptance is UNRUN.
A separate negative test proves unpatched production admission rejects it.
"""
import base64,copy,hashlib,importlib.metadata,io,json,os,subprocess,sys,tempfile,time,unittest
from contextlib import ExitStack
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from test_private_preview_flow_stdlib import PreviewFlowFixture,textured,signed_preview
from test_hosted_uploads_stdlib import plan as base_plan
from test_deployment_plan_stdlib import config as base_config
from test_declarative_policy_stdlib import fixture as policy_fixture
from test_source_planning_stdlib import semantic as source_reply
from test_automated_review_stdlib import reply as review_reply
from fresh_app_fixture import VOICE
from byron_video import vertex_review as vr,source_planning as sp
from byron_video.hosted_control import canonical,Principal
from byron_video.elevenlabs_adapter import fingerprint


class ProductionFixture:
    def __init__(self,root):
        self.root=Path(root);self.stack=ExitStack();self.flow=None;self.http_calls=[];self.created={}
        try:self.initialize()
        except BaseException:self.close();raise
    def close(self):
        if self.flow is not None:self.flow.close()
        self.stack.close()
    def initialize(self):
        from test_cloud_journal_stdlib import Transaction,Ref
        original_create,original_get=Transaction.create,Ref.get
        def create(tx,ref,value):
            result=original_create(tx,ref,value);self.created.setdefault(ref.path,datetime.now(timezone.utc));return result
        def get(ref,transaction=None):
            result=original_get(ref,transaction=transaction);result.create_time=self.created.get(ref.path);return result
        self.stack.enter_context(patch.object(Transaction,'create',create));self.stack.enter_context(patch.object(Ref,'get',get))
        from byron_video import runtime_assembly
        self.stack.enter_context(patch('byron_video.worker_binding.HttpxCloudRunTransport',side_effect=lambda *a,**kw:runtime_assembly.HttpxCloudRunTransport(*a,**kw)))
        self.stack.enter_context(patch('byron_video.cloud_run_readback.CloudRunReadTransport',side_effect=lambda *a,**kw:runtime_assembly.CloudRunReadTransport(*a,**kw)))
        # Explicit LOCAL_UNPINNED_MEDIA_WIRING_ONLY metadata seam; never production code.
        from byron_video.worker_bootstrap import REQUIRED_PACKAGES
        real_version=importlib.metadata.version
        google={'google-cloud-storage','google-cloud-firestore','google-auth','google-cloud-secret-manager'}
        expected_media={'pillow':'13.0.0','numpy':'2.5.4'}
        self.media_version_seam_used=any(real_version(k)!=v for k,v in expected_media.items())
        media_pins=expected_media if self.media_version_seam_used else {}
        versions={name:('0.0.synthetic' if name in google else media_pins.get(name,real_version(name))) for name in REQUIRED_PACKAGES|{'google-cloud-secret-manager'}}
        self.lock=json.dumps(dict(python=f'{sys.version_info.major}.{sys.version_info.minor}',packages=versions),sort_keys=True).encode()
        self.stack.enter_context(patch('importlib.metadata.version',side_effect=lambda name:('0.0.synthetic' if name in google else media_pins.get(name,real_version(name)))))
        cfg=base_config();cfg['firestore']['database']='synthetic-byron'
        cfg['runtime'].update(sdk_lock_sha256=hashlib.sha256(self.lock).hexdigest(),portrait_candidate_sha256=hashlib.sha256(Path('style-guide/portrait-candidate/release.json').read_bytes()).hexdigest())
        from byron_video.exact_film_runtime import contract
        processing={**base_plan(),'exact_film_contract':contract()}
        self.clock=[time.time()]
        self.policy_key=Ed25519PrivateKey.generate();self.public=self.policy_key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        def assemble(**kwargs):
            from byron_video.production_admission import ProductionAdmission
            self.configure_policy(kwargs['config'],kwargs['processing_plan'])
            self.admission=ProductionAdmission(config=kwargs['config'],processing_plan=kwargs['processing_plan'],policy_envelope=self.policy_envelope,policy_public_key=self.public,billing_envelope=self.billing_envelope,billing_public_key=self.public,infrastructure_envelope=self.infrastructure_envelope,infrastructure_public_key=self.public,now=lambda:self.clock[0])
            return runtime_assembly.assemble_runtime(**kwargs,production_admission=self.admission)
        with patch('fresh_app_fixture.assemble_runtime',assemble),patch('test_generation_coordinator_stdlib.config',lambda:copy.deepcopy(cfg)),patch('test_private_preview_flow_stdlib.plan',lambda:copy.deepcopy(processing)):
            self.flow=PreviewFlowFixture(self.root)
        self.api=self.flow.api;self.media=self.flow.media
        self.acceptance,self.reviewer,_=signed_preview(self.api.config,self.api.processing_plan)
        # Exercise real regular-file JSON input, not externally executable bindings.
        self.files={}
        for name,value in [('config',self.api.config),('plan',self.api.processing_plan),('acceptance',self.acceptance),('policy',self.policy_envelope),('billing',self.billing_envelope),('infrastructure',self.infrastructure_envelope)]:
            path=self.root/(name+'.json');path.write_text(canonical(value));path.chmod(0o600);self.files[name]=path
        for name,value in [('sdk-lock',self.lock),('reviewer',self.reviewer),('policy-key',self.public)]:
            path=self.root/name;path.write_bytes(value);path.chmod(0o600);self.files[name]=path
    def configure_policy(self,config,processing_plan):
        now=int(self.clock[0]);self.expires=now+300
        p,_=policy_fixture();p.update(issued_at=now-10,expires_at=self.expires,owner_ids=['google_'+hashlib.sha256(b'accounts.google.com\0' + b'100000000001').hexdigest()],config_sha256=fingerprint(config),processing_plan_sha256=fingerprint(processing_plan),preview_intent=processing_plan['preview_contract'],narration_profile=processing_plan['narration_profile'])
        p['allowed_voice_profiles']=[p['narration_profile'],{**p['narration_profile'],'voice_id':VOICE}]
        p['budget']={**config['budget'],'retry_allowance_cents':0,'approval_reference':'synthetic_packaged_budget'}
        p['infrastructure'].update(target_sha256=fingerprint(config['target']),**{k:config['target'][k] for k in ('cpu','memory','task_timeout_seconds','task_count','parallelism','max_retries')})
        p['review_quote'].update(valid_until_epoch=self.expires,approved_review_cap_cents=30)
        for name in ('image_quote','extension_quote'):p[name]['valid_until_epoch']=self.expires
        controls=[]
        for n in (2048,8192):
            generation=vr.build_vertex_request(project=p['project'],system_instruction='bound',parts=[{'text':'bound'}],response_json_schema={},max_output_tokens=n)['body']['generationConfig'];generation.pop('responseSchema');controls.append(dict(generation_controls=generation,max_billable_output_tokens=n))
        billing=dict(purpose='byron-vertex-billable-output-contract-v1',reference='synthetic_billing_contract',policy_reference=p['reference'],endpoint=vr.endpoint(p['project']),model=vr.MODEL,billed_controls=controls,compiler_sha256s={name:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for name,module in (('vertex_review',vr),('source_planning',sp))},includes_thought_tokens=True,token_limit_contract_reference=p['source_config']['token_limit_contract_reference'],evidence_sha256='b'*64,evidence_url='https://example.invalid/SYNTHETIC-ONLY',issued_at=now-10,expires_at=self.expires)
        p['token_preflight']['billing_contract_sha256']=fingerprint(billing)
        self.policy_envelope=self.sign(p);self.billing_envelope=self.sign(billing)
        self.infrastructure_envelope=self.sign(dict(purpose='byron-exact-film-infrastructure-authorization-v1',reference='synthetic_exact_compute_consent',config_sha256=fingerprint(config),processing_plan_sha256=fingerprint(processing_plan),owner_ids=p['owner_ids'],infrastructure=p['infrastructure'],issued_at=now-10,expires_at=now+600,paid_generation_allowed=False,publication_allowed=False))
    def sign(self,value):return dict(payload=copy.deepcopy(value),signature=base64.b64encode(self.policy_key.sign(canonical(value).encode())).decode())
    def submit(self):
        api=self.api;cap=api.client.get('/api/creator-capabilities');assert cap.status_code==200,cap.text
        api.headers={'Origin':api.config['identity']['origin'],'X-CSRF-Token':cap.json()['csrf_token'],'Idempotency-Key':'production_fresh_generate_001'}
        originals=[]
        for i in range(4):
            b=io.BytesIO();textured(i).save(b,format='PNG');originals.append(b.getvalue())
            target=self.root/f'textured-{i}.mp4';raw=b''.join(textured(i,(270,480),frame).tobytes() for frame in range(240))
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','rawvideo','-pixel_format','rgb24','-video_size','270x480','-framerate','24','-i','pipe:0','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(target)],input=raw,check=True,capture_output=True)
            self.media.clips[i+1]=target.read_bytes()
        uploaded=api.client.post('/api/uploads',files=[('images',(f'{i}.png',data,'image/png')) for i,data in enumerate(originals)],headers=api.headers);assert uploaded.status_code==201,uploaded.text
        api.body=dict(script=self.media.job.snapshot['raw_script'],images=uploaded.json()['images'],voice_id=VOICE)
        submitted=api.client.post('/api/jobs',json=api.body,headers=api.headers);assert submitted.status_code==202,submitted.text
        self.job=api.runtime.jobs.get(api.owner,submitted.json()['id']);return self.job
    def http(self,**kwargs):
        outer=self
        class Response:
            def __init__(self,data,headers=None):self.data=data;self.status_code=200;self.headers=headers or {}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_bytes(self):yield self.data
        class HTTP:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def close(self):pass
            def stream(self,method,url,**kw):
                outer.http_calls.append((method,url));body=json.loads(kw['content']) if 'content' in kw else kw.get('json')
                if url.startswith('http://metadata.google.internal/'):
                    if url.endswith('project/project-id'):data=outer.api.config['target']['project_id'].encode()
                    elif url.endswith('default/email'):data=outer.api.config['runtime']['worker_service_account'].encode()
                    else:data=vr.encoded(dict(access_token='synthetic_attached_access_token',expires_in=1800,token_type='Bearer'))
                    return Response(data,{'Metadata-Flavor':'Google'})
                if url.endswith(':countTokens'):return Response(vr.encoded(dict(totalTokens=1000)))
                if url.endswith(':generateContent'):
                    parts=body['contents'][0]['parts'];data=json.loads(parts[0]['text'])
                    if data['schema']==sp.SCHEMA:
                        evidence=dict(text=data['exact_story_as_untrusted_data'],binding=data['binding'],sources=[json.loads(part['text']) for part in parts[1::2]])
                        value=source_reply({'evidence':evidence})
                    else:value=review_reply({'body':{'binding':data['binding']}})
                    return Response(vr.encoded(dict(responseId='synthetic_native_'+str(len(outer.http_calls)),modelVersion=vr.MODEL,usageMetadata=dict(promptTokenCount=1000,candidatesTokenCount=500,totalTokenCount=1500),candidates=[dict(index=0,finishReason='STOP',content=dict(role='model',parts=[dict(text=json.dumps(value))]))])))
                if url.startswith('https://api.elevenlabs.io/'):
                    result=outer.media.narration({'body':body});return Response(result.payload,{'request-id':result.request_id})
                if url.startswith('https://queue.fal.run/'):
                    if method=='POST':return Response(vr.encoded(outer.media.submit({'endpoint':url.split('queue.fal.run/')[1]})))
                    rid=url.split('/requests/')[1].split('/')[0];index=outer.media.handles[rid]
                    if url.endswith('/status'):return Response(vr.encoded(dict(request_id=rid,status='COMPLETED')))
                    return Response(vr.encoded(dict(video=dict(url=f'https://fal.media/files/synthetic-{index}.mp4',file_size=len(outer.media.clips[index]),content_type='video/mp4'))))
                if url.startswith('https://fal.media/files/synthetic-'):return Response(outer.media.clips[int(url.rsplit('-',1)[1].split('.')[0])])
                raise AssertionError('Unexpected external URL: '+url)
        return HTTP()
    def worker(self,*,protocol='originals-v1'):
        from byron_video.workload_credentials import create_attached_credentials
        from byron_video.workload_tokens import AttachedWorkloadTokenReader
        from byron_video.production_worker import build_worker_bindings,WorkerActivation,ExactFilmActivation
        from byron_video.declarative_policy import ApprovedProductionPolicy
        from byron_video.worker_bootstrap import private_bytes,strict_json,PrivateWorkerRuntime,original_invocation_from_environment
        class SDKCredentialBase:
            def __init__(self):self.token=None;self.expiry=None
        class ComputeCredential:
            def __init__(self,**kw):self.service_account_email=kw['service_account_email'];self.universe_domain=kw['universe_domain']
            def refresh(self,request):
                response=request('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/'+self.service_account_email+'/token',headers={'Metadata-Flavor':'Google'})
                data=json.loads(response.data);self.token=data['access_token'];self.expiry=datetime.fromtimestamp(time.time()+data['expires_in'],timezone.utc)
        if protocol=='exact-film-v1':
            activation=ExactFilmActivation(self.api.config,self.api.processing_plan,self.acceptance,self.reviewer,self.infrastructure_envelope,self.public,now=lambda:self.clock[0])
        else:
            policy=ApprovedProductionPolicy(self.policy_envelope,public_key=self.public,config_sha256=fingerprint(self.api.config),processing_plan_sha256=fingerprint(self.api.processing_plan),now=lambda:self.clock[0])
            activation=WorkerActivation(self.api.config,self.api.processing_plan,self.acceptance,self.reviewer,policy,now=lambda:self.clock[0])
        credentials=create_attached_credentials(self.api.config,role='worker',activation_check=activation,enabled=True,client_factory=self.http,credentials_base=SDKCredentialBase,
            token_reader_factory=lambda **kw:AttachedWorkloadTokenReader(**kw,credentials_factory=ComputeCredential))
        # Fake only the three actual SDK constructors and their remote responses.
        def secret(request,**kw):return SimpleNamespace(payload=SimpleNamespace(data=b'synthetic_provider_key_long'))
        factories=dict(storage=lambda **kw:self.api.storage,firestore=lambda **kw:self.api.firestore,secrets=lambda **kw:SimpleNamespace(access_secret_version=secret))
        read=lambda name:strict_json(private_bytes(self.files[name],1_000_000))
        bindings=build_worker_bindings(config=read('config'),processing_plan=read('plan'),sdk_lock_bytes=private_bytes(self.files['sdk-lock'],128000),acceptance=read('acceptance'),reviewer_public_key=private_bytes(self.files['reviewer'],32),policy_envelope=read('policy'),policy_public_key=private_bytes(self.files['policy-key'],32),billing_envelope=read('billing'),billing_public_key=private_bytes(self.files['policy-key'],32),workspace=self.root,enabled=True,credential_bundle=credentials,sdk_factories=factories,transaction_runner=self.api.firestore.run,http_client_factory=self.http,read_transport=self.api.read,now=lambda:self.clock[0],protocol=protocol,infrastructure_envelope=read('infrastructure'))
        worker=PrivateWorkerRuntime(self.api.config,bindings,enabled=True,sdk_lock_bytes=self.lock)
        env={item['name']:item['value'] for item in self.api.run_requests[0]['overrides']['containerOverrides'][0]['env']};env['CLOUD_RUN_EXECUTION']='renderer-fixture'
        if protocol=='exact-film-v1':return worker,None
        return worker,original_invocation_from_environment(env,self.api.config)


class ProductionFreshChecks(unittest.TestCase):
    def test_actual_unpatched_media_admission_rejects_local_runtime(self):
        from byron_video.preview_composition import validate_preview,release_hash
        mismatch=(importlib.metadata.version('pillow'),importlib.metadata.version('numpy'))!=('13.0.0','2.5.4')
        frozen={};report=dict(status='DRAFT_RELEASE_CANDIDATE',provider='elevenlabs',media_verified=True,provider_receipts_verified=True,human_approved=False,live_release=False,synthetic=False)
        # On the pinned CI image the gate must pass and reach deliberately absent
        # fixture text; local mismatch must fail at the real production gate.
        expected=self.assertRaisesRegex(ValueError,'pinned media runtime') if mismatch else self.assertRaisesRegex(KeyError,'narrated_text')
        with expected:
            validate_preview(dict(root='.',snapshot_sha256=fingerprint(frozen)),frozen,report=report,expected_report_sha256=fingerprint(report),clip_receipts=[],context={},expected_context_sha256='',expected_authorization_sha256='',expected_preview_release_sha256=release_hash(),expected_ffmpeg_version='',preview_evidence_verifier=None,preview_source_verifier=None)

    def test_LOCAL_UNPINNED_MEDIA_WIRING_ONLY_packaged_draft_and_exact_film(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);f=ProductionFixture(root)
            try:
                job=f.submit()
                self.assertFalse(any('/preview_authorizations/' in key for key in f.api.firestore.data))
                worker,invocation=f.worker();result=worker.run_original(invocation,root/'packaged-worker')
                self.assertTrue(result['completion_reconciled'],result)
                done=f.api.runtime.jobs.get(f.api.owner,job.id);self.assertEqual(done.state,'draft_preview_ready')
                integrity=json.loads(done.artifact.candidate_media_evidence_json)
                for field in ('spoken_words_independently_verified','visible_caption_words_independently_verified','visual_source_order_independently_verified','human_semantics_verified','human_style_approved'):
                    self.assertIs(integrity[field],False)
                self.assertEqual(integrity['frames_verified'],384)
                self.assertEqual(sum(url.endswith(':countTokens') for _,url in f.http_calls),5)
                self.assertEqual(sum(url.endswith(':generateContent') for _,url in f.http_calls),5)
                self.assertEqual(f.media.calls.count('narration-submit'),1);self.assertEqual(f.media.calls.count('motion-submit'),4)
                draft=f.api.client.get('/api/jobs/'+job.id+'/video');self.assertEqual(draft.status_code,200,draft.text[:100])
                provider_trace=lambda:[call for call in f.http_calls if call[1].startswith('https://')]
                before=provider_trace()
                replay=worker.run_original(invocation,root/'no-repeat');self.assertTrue(replay['already_complete'])
                self.assertEqual(provider_trace(),before)
                # Generation expires, but still-current runtime and separate compute
                # authorization retain owner-only read/renew/render capabilities.
                f.clock[0]=f.expires+1
                with self.assertRaises(ValueError):worker.bindings.activation_check()
                f.api.runtime.previews.authorization_verifier.primary.authorizations.now=lambda:f.clock[0]
                rejected=f.api.client.post('/api/jobs',json=f.api.body,headers={**f.api.headers,'Idempotency-Key':'expired_generation_attempt_001'})
                self.assertEqual(len(f.api.run_requests),1)
                self.assertNotEqual(rejected.json().get('status'),'queued')
                self.assertEqual(f.api.client.get('/api/jobs/'+job.id+'/video').status_code,503)
                renew=f.api.client.post('/api/jobs/'+job.id+'/preview-view-renewal/challenge',json={'artifact_id':done.artifact.artifact_id},headers=f.api.headers)
                self.assertEqual(renew.status_code,200,renew.text)
                renew_body={k:renew.json()[k] for k in ('challenge_id','challenge_sha256')};renew_body['artifact_id']=done.artifact.artifact_id
                renewed=f.api.client.post('/api/jobs/'+job.id+'/preview-view-renewal',json=renew_body,headers={**f.api.headers,'Idempotency-Key':'expired_generation_view_001'})
                self.assertEqual(renewed.status_code,200,renewed.text)
                self.assertEqual(f.api.client.get('/api/jobs/'+job.id+'/video').content,draft.content)
                self.assertEqual(provider_trace(),before)
                challenge=f.api.client.post('/api/jobs/'+job.id+'/film-approval/challenge',json={'preview_id':done.artifact.artifact_id},headers=f.api.headers)
                self.assertEqual(challenge.status_code,200,challenge.text)
                consent={k:challenge.json()[k] for k in ('challenge_id','challenge_sha256')};consent['preview_id']=done.artifact.artifact_id
                response=f.api.client.post('/api/jobs/'+job.id+'/film-approval',json=consent,headers={**f.api.headers,'Idempotency-Key':'packaged_exact_film_001'})
                self.assertEqual(response.status_code,202,response.text);self.assertEqual(len(f.api.run_requests),2)
                from byron_video.exact_film_dispatch import invocation_from_environment
                env={item['name']:item['value'] for item in f.api.run_requests[1]['overrides']['containerOverrides'][0]['env']};env['CLOUD_RUN_EXECUTION']='renderer-fixture-2'
                clean_worker,_=f.worker(protocol='exact-film-v1')
                self.assertIsNone(clean_worker.bindings.generation_factory)
                final=clean_worker.run_exact_film(invocation_from_environment(env,f.api.config),root/'packaged-final')
                self.assertTrue(final['completion_reconciled'],final);self.assertEqual(provider_trace(),before)
                download=f.api.client.get('/api/jobs/'+job.id+'/approved-video');self.assertEqual(download.status_code,200,download.text[:100])
                self.assertNotEqual(download.content,draft.content);self.assertEqual(f.api.client.get('/api/jobs/'+job.id+'/video').content,draft.content)
            finally:f.close()

if __name__=='__main__':unittest.main()
