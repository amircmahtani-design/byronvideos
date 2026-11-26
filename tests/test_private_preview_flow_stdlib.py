"""Synthetic native services, real HTTP/storage/sampling/render/private DRAFT delivery.

No network or credential discovery. SDK admission is deliberately injected, not
live integration proof. Semantic model answers and billing proofs are synthetic.
"""
import base64,copy,hashlib,io,json,os,subprocess,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw
from fresh_app_fixture import FreshApplicationFixture,VOICE
from test_generation_coordinator_stdlib import FreshFixture
from test_hosted_uploads_stdlib import plan
from test_automated_review_stdlib import reply
from test_review_budget_stdlib import quote as review_quote
from byron_video import private_preview as pp,source_planning as sp,automated_review as ar,vertex_review as vr
from byron_video.elevenlabs_adapter import fingerprint
from byron_video.hosted_control import canonical
from byron_video.preview_composition import release_hash,verify_watermark
from byron_video.generation_coordinator import build_generation_factory,FirestoreGenerationPlan
from byron_video.funded_review import build_vertex_review_factory
from byron_video.review_budget import FilmReviewFunding,call_ceiling_cents
from byron_video.worker_bootstrap import WorkerBindings,PrivateWorkerRuntime,original_invocation_from_environment

CONTRACT='synthetic_preview_token_contract'
RATES=dict(input_usd_per_million='.275',output_usd_per_million='1.65')

def signed_preview(config,processing):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
    from byron_video.preview_runtime import PURPOSE,PREVIEW_EVIDENCE
    from byron_video.runtime_assembly import OriginalInputRequestLoader
    key=Ed25519PrivateKey.generate();now=int(time.time())
    payload=dict(schema_version=1,purpose=PURPOSE,status='PRIVATE_PREVIEW_AUTHORIZED',config_sha256=fingerprint(config),plan_sha256=fingerprint(processing),
        preview_contract_sha256=fingerprint(processing['preview_contract']),issued_at=now-10,expires_at=now+600,
        evidence={name:(OriginalInputRequestLoader(fingerprint(config)).release_sha256 if name=='handoff_adapter' else 'e'*64) for name in PREVIEW_EVIDENCE},owner_only=True,public_release=False)
    return dict(payload=payload,signature=base64.b64encode(key.sign(canonical(payload).encode())).decode()),key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),key


def textured(index,size=(512,512),frame=0):
    image=Image.new('RGB',size);draw=ImageDraw.Draw(image)
    for y in range(0,size[1],32):
        for x in range(0,size[0],32):
            draw.rectangle((x,y,x+31,y+31),fill=((x*13+y*7+index*31)%180+30,(x*3+y*11)%180+30,(x*7+y*3)%180+30))
    draw.rectangle((50+frame//4,120,78+frame//4,148),fill=(245,245,245))
    return image


class PreviewFlowFixture:
    def __init__(self,root,*,bad_qc=False,uncertain=False):
        self.root=Path(root);self.media=FreshFixture(root);self.posts=[];self.prepared={};self.logical={};self.bad_qc=bad_qc;self.uncertain=uncertain
        processing={**plan(),**self.media.job.snapshot['processing_plan']}
        processing['preview_contract']=dict(mode='PRIVATE_DRAFT_PREVIEW',preview_policy_sha256=fingerprint(pp.policy()),preview_release_sha256=release_hash(),source_policy_sha256=fingerprint(sp.policy()),review_policy_sha256=fingerprint(ar.candidate_policy()))
        with patch('fresh_app_fixture.signed',signed_preview):self.api=FreshApplicationFixture(config_value=self.media.config,processing_plan=processing)
        self.expires=time.time()+600;self.budget={**{k:self.api.config['budget'][k] for k in ('scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents')},'retry_allowance_cents':0,'approval_reference':'synthetic_preview_budget'}
        self.trusted={k:v for k,v in self.budget.items() if k in ('approved_total_cap_cents','prior_spend_cents','job_cap_cents')};self.trusted['scope_sha256']=hashlib.sha256(self.budget['scope'].encode()).hexdigest()
        self.source_config=dict(project=self.api.config['target']['project_id'],max_input_tokens=8192,max_output_tokens=2048,token_limit_contract_reference=CONTRACT)
    def close(self):self.api.close()
    def submit(self):
        api=self.api;c=api.client.get('/api/creator-capabilities');assert c.status_code==200,c.text
        api.headers={'Origin':api.config['identity']['origin'],'X-CSRF-Token':c.json()['csrf_token'],'Idempotency-Key':'private_preview_fixture_001'}
        originals=[]
        for i in range(4):
            image=textured(i);b=io.BytesIO();image.save(b,format='PNG');originals.append(b.getvalue())
            target=self.root/f'textured-{i}.mp4'
            raw=b''.join(textured(i,(270,480),f).tobytes() for f in range(240))
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','rawvideo','-pixel_format','rgb24','-video_size','270x480','-framerate','24','-i','pipe:0','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(target)],input=raw,check=True,capture_output=True)
            self.media.clips[i+1]=target.read_bytes()
        upload=api.client.post('/api/uploads',files=[('images',(f'{i}.png',b,'image/png')) for i,b in enumerate(originals)],headers=api.headers);assert upload.status_code==201,upload.text
        api.body=dict(script=self.media.job.snapshot['raw_script'],images=upload.json()['images'],voice_id=VOICE)
        response=api.client.post('/api/jobs',json=api.body,headers=api.headers);assert response.status_code==202,response.text
        self.job=api.runtime.jobs.get(api.owner,response.json()['id']);self.media.job=self.job
        self.auth=dict(schema=pp.AUTH_SCHEMA,reference='synthetic_owner_preview',owner_id=self.job.owner_id,job_id=self.job.id,snapshot_sha256=self.job.snapshot_sha256,
            **{k:v for k,v in self.job.snapshot['processing_plan']['preview_contract'].items() if k!='mode'},budget=self.budget,expires_at=self.expires,private_preview_approved=True,owner_only=True,public_release=False,human_style_accepted=False)
        common=dict(project_id=api.config['target']['project_id'],namespace=api.config['firestore']['namespace'],transaction_runner=api.firestore.run)
        pp.FirestorePreviewAuthorizations(api.firestore,**common,budget_loader=lambda job:self.budget,preview_release_sha256=release_hash()).commit(self.job,self.auth,fingerprint(self.auth))
        return self.job
    def proof(self,native,inputs,outputs,count):
        return dict(reference='synthetic_exact_bound',native_request_sha256=fingerprint(native),max_input_tokens=inputs,max_output_tokens=outputs,image_count=count,expires_at=self.expires,method='reviewed_worst_case_bound',evidence_sha256='b'*64,token_limit_contract_reference=CONTRACT,
            billing_bound=dict(reference='synthetic_billing_bound',evidence_sha256='d'*64,native_request_sha256=fingerprint(native),generation_config_sha256=fingerprint(native['body']['generationConfig']),model=vr.MODEL,endpoint=native['url'],max_billable_output_tokens=outputs,includes_thought_tokens=True,token_limit_contract_reference=CONTRACT,expires_at=self.expires))
    def planning_quote(self,job,prepared):
        self.prepared[fingerprint(prepared['native']['body'])]=copy.deepcopy(prepared)
        request={k:prepared[k] for k in ('native','binding','config')};request['evidence_sha256']=fingerprint(prepared['evidence'])
        return dict(quote=dict(reference='synthetic_planning_quote',request_sha256=fingerprint(request),budget_sha256=fingerprint(self.trusted),reservation_cents=call_ceiling_cents(max_input_tokens=8192,max_output_tokens=2048,**RATES),job_cap_cents=50,operation_cap_cents=10,**RATES,max_input_tokens=8192,max_output_tokens=2048,all_in_usd_ceiling=True,output_includes_thought_tokens=True,token_limit_contract_reference=CONTRACT,valid_until_epoch=self.expires),input_proof=self.proof(prepared['native'],8192,2048,4))
    def qc_proof(self,job,logical,native):
        self.logical[fingerprint(native['body'])]=copy.deepcopy(logical)
        return self.proof(native,15000,8192,10)
    def http(self,**kwargs):
        outer=self
        class Response:
            status_code=200;headers={}
            def __init__(self,data):self.data=data
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_bytes(self):yield self.data
        class HTTP:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def stream(self,method,url,**kw):
                body=json.loads(kw['content']);key=fingerprint(body);outer.posts.append(key)
                if key in outer.prepared:
                    from test_source_planning_stdlib import semantic
                    value=semantic(outer.prepared[key])
                    if outer.uncertain:value['scenes'][0]['uncertainties']=['Synthetic source uncertainty']
                else:
                    value=reply(outer.logical[key])
                    if outer.bad_qc:value['checks'][next(iter(value['checks']))]['passed']=False
                raw=vr.encoded(dict(responseId='synthetic_native_'+str(len(outer.posts)),modelVersion=vr.MODEL,usageMetadata=dict(promptTokenCount=1000,candidatesTokenCount=500,totalTokenCount=1500),candidates=[dict(index=0,finishReason='STOP',content=dict(role='model',parts=[dict(text=json.dumps(value))]))]))
                return Response(raw)
        return HTTP()
    def worker(self):
        api=self.api;m=self.media
        token=lambda:vr.WorkloadToken('synthetic-workload-token',time.time()+1800)
        planner=sp.build_source_planner_factory(config=self.source_config,quote_loader=self.planning_quote,token_reader=lambda j,r:token(),activation_check=lambda:True,enabled=True,client_factory=self.http)
        reviewer=build_vertex_review_factory(token_reader=token,input_proof_loader=self.qc_proof,max_input_tokens=15000,token_limit_contract_reference=CONTRACT,workspace=self.root,activation_check=lambda:True,enabled=True,client_factory=self.http)
        def review_prices(job,motion):
            q=review_quote();q.update(config=vr.config(api.config['target']['project_id']),max_output_tokens=8192,token_limit_contract_reference=CONTRACT,valid_until_epoch=self.expires,approved_review_cap_cents=30)
            q['source_planning']=dict(image_count=4,max_input_tokens=8192,max_output_tokens=2048,text_tokens_ceiling=3000,execution_enabled=False,funded_operation_id=sp.source_operation(job.owner_id,job.id))
            return q
        class Narration:
            def submit_authorized(self,request,check):assert check() is True;return m.narration(request)
        class Motion:
            def submit_authorized(self,request,check):assert check() is True;return m.submit(request)
            def status(self,handle):return m.status(handle)
            def result(self,handle):return m.result(handle)
            def download(self,value):return m.download(value)
        def quotes(*args):
            q=lambda kind,endpoint,duration:dict(kind=kind,endpoint=endpoint,duration_seconds=duration,resolution='768P',max_cost_cents=2,valid_until_epoch=self.expires,reference='synthetic_'+kind+'_quote')
            return dict(image_quote=q('image','minimax/h3-max-turbo/image-to-video',10),extension_quote=q('extend','minimax/h3-max-turbo/extend-video',5))
        factory=build_generation_factory(narration_transport=Narration(),motion_transport=Motion(),narration_approval_loader=m.approval,policy_loader=lambda *a:None,review_loader=None,
            activation_check=lambda:True,source_kind='synthetic_test',enabled=True,review_funding=FilmReviewFunding(quote_loader=review_prices,enabled=True),source_planner_factory=planner,review_loader_factory=reviewer,
            preview_authorization_loader=lambda j:(self.auth,fingerprint(self.auth)),preview_budget_loader=lambda j:self.budget,preview_quote_loader=quotes)
        bindings=WorkerBindings(api.storage,api.firestore,lambda:'synthetic-token',m.semantic,transaction_runner=api.firestore.run,generation_factory=factory,activation_check=lambda:True,read_transport=api.read)
        worker=PrivateWorkerRuntime(api.config,bindings,enabled=False);worker.enabled=True
        env={v['name']:v['value'] for v in api.run_requests[0]['overrides']['containerOverrides'][0]['env']};env['CLOUD_RUN_EXECUTION']='renderer-fixture'
        return worker,original_invocation_from_environment(env,api.config)


class PrivatePreviewFlowChecks(unittest.TestCase):
    def test_uncertain_source_blocks_all_motion(self):
        with tempfile.TemporaryDirectory() as temp:
            f=PreviewFlowFixture(Path(temp),uncertain=True)
            try:
                job=f.submit();worker,invocation=f.worker();result=worker.run_original(invocation,Path(temp)/'worker')
                self.assertIn(result['state'],('HOLD','REVIEW_REQUIRED'))
                self.assertEqual(f.media.calls.count('motion-submit'),0);self.assertEqual(len(f.posts),1)
                self.assertNotEqual(f.api.runtime.jobs.get(f.api.owner,job.id).state,'draft_preview_ready')
                self.assertNotEqual(f.api.client.get('/api/jobs/'+job.id+'/video').status_code,200)
            finally:f.close()
    def test_failed_native_qc_stops_next_scene_and_replay_does_not_resubmit(self):
        with tempfile.TemporaryDirectory() as temp:
            f=PreviewFlowFixture(Path(temp),bad_qc=True)
            try:
                job=f.submit();worker,invocation=f.worker();result=worker.run_original(invocation,Path(temp)/'worker')
                self.assertIn(result['state'],('HOLD','REVIEW_REQUIRED'))
                self.assertEqual(f.media.calls.count('motion-submit'),1);self.assertEqual(len(f.posts),2)
                self.assertNotEqual(f.api.client.get('/api/jobs/'+job.id+'/video').status_code,200)
                before=len(f.posts);worker.run_original(invocation,Path(temp)/'replay')
                self.assertEqual(len(f.posts),before);self.assertEqual(f.media.calls.count('motion-submit'),1)
            finally:f.close()
    def test_fresh_native_private_draft_then_idempotent_owner_download(self):
        with tempfile.TemporaryDirectory() as temp:
            f=PreviewFlowFixture(Path(temp))
            try:
                job=f.submit();worker,invocation=f.worker();result=worker.run_original(invocation,Path(temp)/'worker')
                self.assertTrue(result['completion_reconciled'],result)
                done=f.api.runtime.jobs.get(f.api.owner,job.id);self.assertEqual(done.state,'draft_preview_ready')
                self.assertEqual(len(f.posts),5);self.assertEqual(f.media.calls.count('motion-submit'),4)
                self.assertEqual(f.media.calls.count('narration-submit'),1)
                self.assertEqual(len(f.api.run_requests),1)
                status=f.api.client.get('/api/jobs/'+job.id);self.assertEqual(status.status_code,200)
                download=f.api.client.get(status.json()['download_url']);self.assertEqual(download.status_code,200,download.text[:100])
                self.assertEqual(hashlib.sha256(download.content).hexdigest(),done.artifact.sha256)
                self.assertEqual(download.headers['x-byron-artifact-kind'],'PRIVATE_DRAFT_PREVIEW')
                self.assertIn('DRAFT',download.headers['content-disposition'])
                self.assertIn('no-store',download.headers['cache-control'])
                self.assertEqual(len([name for name in f.api.bucket.objects if '/provider-responses/vertex_audit_' in name]),4)
                self.assertTrue(all(not p['approved'] for p in FirestoreGenerationPlan(f.api.firestore,project_id=f.api.config['target']['project_id'],namespace=f.api.config['firestore']['namespace'],transaction_runner=f.api.firestore.run).load(done)['source_candidate']['proposals']))
                film=Path(temp)/'download.mp4';film.write_bytes(download.content);proof=verify_watermark(film)
                if os.environ.get('BYRON_SYNTHETIC_PROOF_DIR'):
                    preserved=Path(os.environ['BYRON_SYNTHETIC_PROOF_DIR']);preserved.mkdir(parents=True,exist_ok=True)
                    (preserved/'film.mp4').write_bytes(download.content)
                    (preserved/'SYNTHETIC-ONLY.json').write_text(json.dumps(dict(watermark=proof,provider_calls='FAKE ONLY',semantic_answers='SYNTHETIC',sdk_admission='INJECTED TEST ONLY',live_acceptance=False),indent=2))
                self.assertTrue(proof['all_frames_verified']);self.assertGreater(proof['frames_verified'],300)
                before=(list(f.posts),list(f.media.calls));again=worker.run_original(invocation,Path(temp)/'replay')
                self.assertTrue(again['already_complete']);self.assertEqual(before,(f.posts,f.media.calls))
                f.api.client.cookies.clear();self.assertEqual(f.api.client.get(status.json()['download_url']).status_code,401)
            finally:f.close()

if __name__=='__main__':unittest.main()
