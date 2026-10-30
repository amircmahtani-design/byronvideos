"""Fresh original input trace: fake services, real native media, no prebuilt report.

Synthetic independent reviews below are test-only. Never reuse them as production
semantic/quote approval loaders. No network SDK or provider is called.
"""
import base64,copy,hashlib,io,json,subprocess,tempfile,time,unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from test_cloud_journal_stdlib import Client
from test_cloud_storage_stdlib import FakeBucket
from test_deployment_plan_stdlib import config
from byron_video.elevenlabs_adapter import fingerprint,plan_requests
from byron_video.hosted_contract import freeze_job
from byron_video.hosted_control import Principal,DispatchReceipt
from byron_video.hosted_uploads import prepare_image,batch_fingerprint
from byron_video.cloud_storage import CloudUploadStore,CloudArtifactReader
from byron_video.cloud_journal import FirestoreJobStore,FirestoreArtifactRegistry
from byron_video.worker_bootstrap import WorkerBindings,PrivateWorkerRuntime,FirestoreWorkerEvidence,validate_original_invocation,original_invocation_from_environment
from byron_video.generation_coordinator import build_generation_factory,narration_timing,FirestoreGenerationPlan
from byron_video.provider_execution import SpendApproval,WireResponse
from byron_video.portrait_composition import release_bindings
from byron_video.worker_binding import SemanticEvidence


class FreshFixture:
    """Reusable fresh fake-cloud/media fixture for API-to-worker integration."""
    def __init__(self,root,*,image_size=(64,64)):
        self.root=Path(root);self.config=config();self.calls=[];self.active=True
        self.client=Client();self.bucket=FakeBucket()
        self.sdk=SimpleNamespace(project='synthetic-project',bucket=lambda name:self.bucket)
        self.config['runtime']['ffmpeg_version']=subprocess.check_output(['ffmpeg','-version']).decode().splitlines()[0].split()[2]
        self.common=dict(project_id='synthetic-project',namespace=self.config['firestore']['namespace'],transaction_runner=self.client.run)
        self.jobs=FirestoreJobStore(self.client,**self.common);self.owner=Principal('fixture_owner')
        uploads=CloudUploadStore(self.sdk,bucket=self.config['target']['bucket']);originals=[]
        self.colours=['red','blue','green','yellow'];self.clips={}
        for index,colour in enumerate(self.colours):
            image=io.BytesIO();Image.new('RGB',image_size,colour).save(image,format='PNG');originals.append(prepare_image(image.getvalue(),'image/png'))
            clip=self.root/f'provider-result-{index}.mp4'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',f'color=c={colour}:s=270x480:r=24:d=10',
                '-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(clip)],check=True,capture_output=True)
            self.clips[index+1]=clip.read_bytes()
        _,records=uploads.commit_batch(self.owner,'fixture_original_upload_001',batch_fingerprint(originals),originals)
        images=[dict(asdict(record),position=i) for i,record in enumerate(records,1)]
        processing=dict(narration_profile=self.config['narration_profile'],**release_bindings())
        raw='First painting. Second painting. Third painting. Fourth painting.'
        contract=freeze_job(raw,[{k:x[k] for k in ('id','position','sha256')} for x in images],processing['narration_profile'],processing['visual_style_sha256'])
        snapshot=dict(raw_script=raw,narrated_text=contract['narrated_text'],narrated_text_sha256=contract['narrated_text_sha256'],
            images=images,input_order=[x['id'] for x in images],processing_plan=processing,processing_plan_sha256=fingerprint(processing),
            narration_contract=contract,runtime_config_sha256=fingerprint(self.config))
        self.job,_=self.jobs.reserve(self.owner.subject,'fixture_fresh_generate_001','b'*64,snapshot)
        tone=self.root/'provider-tone.mp3'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=44100:duration=16',
            '-c:a','libmp3lame','-b:a','128k',str(tone)],check=True,capture_output=True)
        self.tone=tone.read_bytes();self.handles={};self.approved=True;self.output_review=True;self.queue_status='COMPLETED'
    def activation(self):return self.active
    def narration(self,request):
        self.calls.append('narration-submit');text=request['body']['text'];n=len(text)
        payload=json.dumps(dict(audio_base64=base64.b64encode(self.tone).decode(),alignment=dict(characters=list(text),
            character_start_times_seconds=[i*15.9/n for i in range(n)],character_end_times_seconds=[(i+1)*15.9/n for i in range(n)]))).encode()
        return WireResponse('synthetic_narration_request',payload)
    def approval(self,job):
        requests=plan_requests(job.snapshot['narrated_text'],job.snapshot['processing_plan']['narration_profile'])
        approval=SpendApproval('synthetic_tts_quote',fingerprint(requests),(2,)*len(requests),50,10)
        return dict(approval=approval,quote=dict(reference=approval.reference,owner_id=job.owner_id,job_id=job.id,
            plan_sha256=approval.plan_sha256,reservations_cents=list(approval.reservations_cents),valid_until_epoch=time.time()+3600))
    def policy(self,job,motion):
        reviews={x['id']:dict(provenance='local_human_source_review',source_sha256=x['sha256'],reference_id='synthetic_source_review',
                  capabilities=['safe_camera_framing']) for x in job.snapshot['images']}
        if motion is None:return dict(source_reviews=reviews)
        if not self.approved:return None
        from byron_video.motion_continuation import EXTENSION_ENDPOINT
        quote=lambda kind,endpoint,duration:dict(kind=kind,endpoint=endpoint,duration_seconds=duration,resolution='768P',
                    max_cost_cents=2,valid_until_epoch=time.time()+3600,reference='synthetic_quote_'+kind)
        return dict(approved_prompts=[dict(image_id=s['image_id'],source_sha256=s['source_sha256'],scene_sha256=fingerprint(s),
            prompt=s['prompt'],reference='synthetic_prompt_approval',approved=True) for s in motion['scenes']],
            image_quote=quote('image','minimax/h3-max-turbo/image-to-video',10),extension_quote=quote('extend',EXTENSION_ENDPOINT,5),
            budget={**{k:self.config['budget'][k] for k in ('scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents')},
                    'retry_allowance_cents':0,'approval_reference':'synthetic_campaign_approval'})
    def submit(self,request):
        self.calls.append('motion-submit');index=len(self.handles)+1;request_id=f'synthetic_motion_{index}'
        base='https://queue.fal.run/minimax/h3-max-turbo/requests/'+request_id
        handle=dict(endpoint=request['endpoint'],request_id=request_id,response_url=base,status_url=base+'/status')
        self.handles[request_id]=index;return handle
    def status(self,handle):self.calls.append('motion-status');return self.queue_status
    def result(self,handle):
        self.calls.append('motion-result');index=self.handles[handle['request_id']]
        return dict(url=f'https://fal.media/files/synthetic-{index}.mp4',bytes=len(self.clips[index]),request_id=handle['request_id'])
    def download(self,result):
        self.calls.append('motion-download');body=self.clips[self.handles[result['request_id']]]
        return dict(bytes=body,sha256=hashlib.sha256(body).hexdigest())
    def review(self,record):
        if not self.output_review:return None
        # TEST-ONLY synthetic colour fixture review, not human approval of real imagery.
        plan=FirestoreGenerationPlan(self.client,**self.common).load(self.job)
        scene=next(x for x in plan['scenes'] if x['image_id']==record['image_id'])
        return dict(reference='synthetic_decoded_colour_review',clip_sha256=record['video']['sha256'],source_sha256=record['source_sha256'],
            prompt_sha256=hashlib.sha256(scene['approved_prompt'].encode()).hexdigest(),no_invented_action_reviewed=True)
    def semantic(self,output,job):
        frozen=job.snapshot
        return SemanticEvidence(hashlib.sha256(Path(output).read_bytes()).hexdigest(),fingerprint(frozen),tuple(frozen['input_order']),
            frozen['narrated_text_sha256'],frozen['narrated_text_sha256'],True,True,True)
    def runtime(self):
        factory=build_generation_factory(narration_transport=self.narration,motion_transport=self,narration_approval_loader=self.approval,
            policy_loader=self.policy,review_loader=self.review,activation_check=self.activation,source_kind='synthetic_test',enabled=True)
        bindings=WorkerBindings(self.sdk,self.client,lambda:'synthetic-token',self.semantic,transaction_runner=self.client.run,
            generation_factory=factory,activation_check=self.activation)
        runtime=PrivateWorkerRuntime(self.config,bindings,enabled=False);runtime.enabled=True # SDK lock deliberately not claimed tested.
        return runtime


class GenerationChecks(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_original_invocation_exact_config_and_protocol(self):
        c=config();value=dict(input_protocol='originals-v1',job_id='a'*32,owner_namespace='b'*64,snapshot_sha256='c'*64,
                             config_sha256=fingerprint(c),execution_id='renderer-fixture')
        self.assertEqual(validate_original_invocation(value,c),value)
        for key,bad in [('input_protocol','prepared'),('config_sha256','d'*64),('execution_id','../bad')]:
            with self.subTest(key=key),self.assertRaises(ValueError):validate_original_invocation({**value,key:bad},c)
    def test_default_factory_never_observes_provider(self):
        calls=[];factory=build_generation_factory(narration_transport=lambda x:calls.append(x),motion_transport=None,
            narration_approval_loader=None,policy_loader=None,review_loader=None,activation_check=lambda:True)
        with self.assertRaises(ValueError):factory(None,SimpleNamespace(enabled=True))
        self.assertEqual(calls,[])
    def test_fresh_native_generation_through_prepared_and_real_verified_artifact(self):
        f=FreshFixture(self.root);runtime=f.runtime();coordinator=runtime.bindings.generation_factory(f.job,runtime)
        statuses=iter(['IN_QUEUE','IN_PROGRESS','COMPLETED'])
        def status(handle):f.calls.append('motion-status');return next(statuses,'COMPLETED')
        f.status=status;coordinator.sleep=lambda seconds:None
        result=coordinator.run(f.job,self.root/'generation')
        self.assertEqual(result['state'],'PREPARED');self.assertFalse(result['new_execution_required'])
        before=list(f.calls);writes=len(f.bucket.writes)
        replay=coordinator.run(f.job,self.root/'replay')
        self.assertEqual(replay['state'],'PREPARED');self.assertEqual(f.calls,before);self.assertEqual(len(f.bucket.writes),writes)
        self.assertEqual(f.calls.count('narration-submit'),1);self.assertEqual(f.calls.count('motion-submit'),4)
        handoff=result['handoff'];execution='renderer-fixture';job=f.jobs.record_dispatch(f.owner.subject,f.job.id,DispatchReceipt(f.job.id,f.job.snapshot_sha256,execution))
        from byron_video.worker_bootstrap import BoundCloudInputReader
        reader=BoundCloudInputReader(f.sdk,bucket=f.config['target']['bucket'],owner_namespace=hashlib.sha256(job.owner_id.encode()).hexdigest(),job_id=job.id)
        readback=SimpleNamespace(target_provider=lambda:f.config['target'])
        rendered=runtime._render(job,handoff['manifest'],handoff['reference'],readback,reader,self.root/'render',execution)
        self.assertTrue(rendered['completion_reconciled']);complete=f.jobs.get(f.owner,job.id);self.assertEqual(complete.state,'complete')
        with CloudArtifactReader(f.sdk,bucket=f.config['target']['bucket'],reference_resolver=runtime.registry)(f.owner,complete,complete.artifact) as stream:
            self.assertEqual(hashlib.sha256(stream.read()).hexdigest(),complete.artifact.sha256)
        self.assertEqual(complete.artifact.width,1080);self.assertEqual(complete.artifact.height,1920)
    def test_pending_queue_is_hold_without_resubmission(self):
        f=FreshFixture(self.root);f.queue_status='IN_QUEUE';runtime=f.runtime();coordinator=runtime.bindings.generation_factory(f.job,runtime)
        coordinator.poll_timeout_seconds=0
        first=coordinator.run(f.job,self.root/'first');second=coordinator.run(f.job,self.root/'second')
        self.assertEqual((first['state'],second['state']),('HOLD','HOLD'))
        self.assertEqual(f.calls.count('narration-submit'),1);self.assertEqual(f.calls.count('motion-submit'),1)
        self.assertEqual(f.calls.count('motion-status'),2);self.assertEqual(f.calls.count('motion-download'),0)
    def test_missing_prompt_review_stops_before_paid_motion(self):
        f=FreshFixture(self.root);f.approved=False;runtime=f.runtime();c=runtime.bindings.generation_factory(f.job,runtime)
        result=c.run(f.job,self.root/'pending')
        self.assertEqual(result['phase'],'prompt_review');self.assertEqual(f.calls,['narration-submit'])
    def test_expired_activation_stops_before_narration(self):
        f=FreshFixture(self.root);runtime=f.runtime();c=runtime.bindings.generation_factory(f.job,runtime);f.active=False
        with self.assertRaises(ValueError):c.run(f.job,self.root/'pending')
        self.assertEqual(f.calls,[])

    def test_expired_quote_blocks_new_paid_call_but_not_recorded_readback(self):
        f=FreshFixture(self.root);runtime=f.runtime();coordinator=runtime.bindings.generation_factory(f.job,runtime)
        f.queue_status='IN_QUEUE';coordinator.poll_timeout_seconds=0
        self.assertEqual(coordinator.run(f.job,self.root/'first')['state'],'HOLD')
        original=f.approval
        def stale(job):
            value=original(job);value['quote']['valid_until_epoch']=time.time()-1;return value
        # The existing factory bound the previous loader; build a fresh explicit one.
        f.approval=stale;runtime=f.runtime();coordinator=runtime.bindings.generation_factory(f.job,runtime);coordinator.poll_timeout_seconds=0
        self.assertEqual(coordinator.run(f.job,self.root/'recovery')['state'],'HOLD')
        self.assertEqual(f.calls.count('narration-submit'),1);self.assertEqual(f.calls.count('motion-submit'),1)
        # A second fresh owner/job with no recorded narration must fail the same quote.
        fresh_snapshot=copy.deepcopy(f.job.snapshot)
        second,_=f.jobs.reserve(f.owner.subject,'fixture_second_generate_001','c'*64,fresh_snapshot)
        coordinator=runtime.bindings.generation_factory(second,runtime)
        with self.assertRaisesRegex(ValueError,'quote expired'):coordinator.run(second,self.root/'fresh-stale')
        self.assertEqual(f.calls.count('narration-submit'),1)

    def test_poll_observation_bound_and_revocation_before_next_get(self):
        f=FreshFixture(self.root);f.queue_status='IN_PROGRESS';runtime=f.runtime();coordinator=runtime.bindings.generation_factory(f.job,runtime)
        coordinator.max_poll_observations=2;coordinator.sleep=lambda seconds:None
        result=coordinator.run(f.job,self.root/'limited')
        self.assertEqual(result['state'],'HOLD');self.assertEqual(f.calls.count('motion-status'),2);self.assertEqual(f.calls.count('motion-submit'),1)
        coordinator.max_poll_observations=5
        def revoke(seconds):f.active=False
        coordinator.sleep=revoke
        with self.assertRaises(ValueError):coordinator.run(f.job,self.root/'revoked')
        self.assertEqual(f.calls.count('motion-status'),3);self.assertEqual(f.calls.count('motion-submit'),1)

    def test_exact_originals_checked_before_paid_narration(self):
        f=FreshFixture(self.root);runtime=f.runtime();coordinator=runtime.bindings.generation_factory(f.job,runtime)
        coordinator.originals=lambda job,image:dict(bytes=b'changed',content_type='image/png')
        with self.assertRaisesRegex(ValueError,'original'):coordinator.run(f.job,self.root/'wrong')
        self.assertEqual(f.calls,[])

    def test_missing_output_review_is_not_auto_approval(self):
        f=FreshFixture(self.root);f.output_review=False;runtime=f.runtime();coordinator=runtime.bindings.generation_factory(f.job,runtime)
        result=coordinator.run(f.job,self.root/'unreviewed')
        self.assertEqual(result['state'],'REVIEW_REQUIRED');self.assertEqual(f.calls.count('motion-submit'),1)
        before=f.calls.count('motion-download');f.output_review=True
        result=coordinator.run(f.job,self.root/'reviewed')
        self.assertEqual(result['state'],'PREPARED');self.assertEqual(f.calls.count('motion-submit'),4)
        self.assertEqual(f.calls.count('motion-download'),before+3)

    def test_review_funding_is_required_and_rejected_before_motion(self):
        f=FreshFixture(self.root);runtime=f.runtime();c=runtime.bindings.generation_factory(f.job,runtime)
        c.review_loader=SimpleNamespace(requires_film_review_funding=True)
        with self.assertRaisesRegex(ValueError,'complete film funding'):c.run(f.job,self.root/'missing-funding')
        self.assertEqual(f.calls,['narration-submit'])
        c.review_funding=lambda **kw:dict(state='FUNDED',owner_id='wrong',job_id=f.job.id)
        with self.assertRaisesRegex(ValueError,'Funding summary'):c.run(f.job,self.root/'wrong-funding')
        self.assertEqual(f.calls,['narration-submit'])

    def test_review_funding_receipt_must_exist_before_motion(self):
        f=FreshFixture(self.root);runtime=f.runtime();c=runtime.bindings.generation_factory(f.job,runtime)
        def forged(**kw):
            return dict(state='FUNDED',owner_id=f.job.owner_id,job_id=f.job.id,
                continuation_plan_sha256=fingerprint(kw['plan']),review_plan_id='review_fixture_plan',
                review_plan_sha256='f'*64,total_cents=4)
        c.review_funding=forged
        with self.assertRaisesRegex(ValueError,'Funding summary'):c.run(f.job,self.root/'forged')
        self.assertEqual(f.calls,['narration-submit'])

    def test_full_review_hold_funded_before_motion_and_replayed_without_debit(self):
        f=FreshFixture(self.root);runtime=f.runtime();c=runtime.bindings.generation_factory(f.job,runtime)
        f.queue_status='IN_QUEUE';c.poll_timeout_seconds=0
        from byron_video.review_budget import FilmReviewFunding
        from test_review_budget_stdlib import quote
        q=quote();q['valid_until_epoch']=time.time()+3600;q['approved_review_cap_cents']=20
        c.review_funding=FilmReviewFunding(quote_loader=lambda job,plan:q,enabled=True)
        self.assertEqual(c.run(f.job,self.root/'funded')['state'],'HOLD')
        before=copy.deepcopy(f.client.data)
        self.assertEqual(c.run(f.job,self.root/'funded-replay')['state'],'HOLD')
        self.assertEqual(before,f.client.data)
        self.assertEqual(f.calls.count('motion-submit'),1)
