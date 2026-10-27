"""Concrete immutable capture adapters against shared local cloud/provider fakes."""
import copy,hashlib,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import test_continuation_worker_stdlib as chain_tests
from test_cloud_journal_stdlib import Client
from test_cloud_storage_stdlib import FakeBucket
from byron_video.segment_capture import CloudSegmentObjects,FirestoreSegmentStore,capture_segment

class CaptureChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=chain_tests.ContinuationWorkerChecks();self.fixture.setUp();f=self.fixture
        self.bucket=FakeBucket();self.client=Client();self.calls=[];self.counter=0
        objects=CloudSegmentObjects(SimpleNamespace(bucket=lambda name:self.bucket),bucket='fixture-private-bucket')
        self.store=FirestoreSegmentStore(self.client,project_id='synthetic-project',namespace='fixture_namespace',objects=objects,transaction_runner=self.client.run)
        self.image=f.plan['input_order'][0]
        from test_motion_continuation_stdlib import ScopedJournal,NOW
        from byron_video.motion_continuation import bind_plan_budget
        self.journal=ScopedJournal(None,f.plan)
        wrapped=bind_plan_budget(plan=f.plan,expected_plan_sha256=f.sha,journal=self.journal,now=NOW)
        for scene in f.plan['scenes']:
            for segment in scene['segments']:
                record=f.records[(scene['image_id'],segment['sequence'])][0]
                request={'fixture_binding':record['operation_id']}
                wrapped.arm(record['operation_id'],request,segment['max_cost_cents'],f.plan['budget']['job_cap_cents'])
                wrapped.record(record['operation_id'],request,record['provider_handle'])
    def tearDown(self):self.fixture.tearDown()
    def transport(self,sequence):
        record=self.fixture.records[(self.image,sequence)][0];body=('synthetic-full-'+self.image+'-'+str(sequence)).encode()
        def status(handle):self.calls.append('status');return 'COMPLETED'
        def result(handle):self.calls.append('result');return dict(url=record['provider_url'],bytes=len(body),request_id=handle['request_id'])
        def download(result):self.calls.append('download');return dict(bytes=body,sha256=hashlib.sha256(body).hexdigest())
        return SimpleNamespace(status=status,result=result,download=download)
    def review(self,record):
        scene=next(s for s in self.fixture.plan['scenes'] if s['image_id']==self.image)
        return dict(reference='fixture_review',clip_sha256=record['video']['sha256'],source_sha256=scene['source_sha256'],prompt_sha256=hashlib.sha256(scene['approved_prompt'].encode()).hexdigest(),no_invented_action_reviewed=True)
    def capture(self,sequence=0,**overrides):
        self.counter+=1;f=self.fixture
        args=dict(job=f.job,plan=f.plan,expected_plan_sha256=f.sha,image_id=self.image,sequence=sequence,spend_journal=self.journal,transport=self.transport(sequence),segment_store=self.store,
            review_loader=self.review,root=Path(f.tmp.name)/f'capture-{self.counter}',narration_report=f.report,enabled=True)
        args.update(overrides)
        with patch('byron_video.portrait_composition.probe',return_value={'duration':10+sequence*5}):return capture_segment(**args)
    def test_capture_base_extension_real_adapters_and_recovery_no_requests(self):
        base=self.capture();extended=self.capture(1)
        self.assertEqual(base['state'],'READY');self.assertEqual(extended['state'],'READY')
        self.assertEqual(base['provider_requests'],3);self.assertEqual(base['paid_submissions'],0)
        self.assertEqual(extended['record']['prior_record_sha256'],base['record_sha256'])
        self.assertTrue(extended['record']['synthetic']);self.assertEqual(len(self.bucket.writes),2)
        self.assertEqual(extended['verified_clip'].duration_seconds,15)
        before=list(self.calls);again=self.capture(1);self.assertEqual(again['record'],extended['record']);self.assertEqual(self.calls,before)
    def test_fresh_capture_records_feed_real_chain_stager(self):
        from byron_video.continuation_worker import stage_committed_continuation
        f=self.fixture
        for image in f.plan['input_order']:
            self.image=image;self.capture();self.capture(1)
        def reader(ref):
            return f.reader(ref) if ref['content_type']=='audio/wav' else self.store.objects.read(ref)
        with patch('byron_video.portrait_composition.probe',side_effect=lambda path,**kw:dict(duration=15 if path.name.startswith('clip-') else 10)):
            staged=stage_committed_continuation(job=f.job,plan=f.plan,expected_plan_sha256=f.sha,narration_report=f.report,expected_report_sha256=f.report_sha,
                narration_ref=f.audio_ref,segment_loader=lambda owner,job,image,seq:self.store.get_record(f.job,image,seq),reader=reader,
                spend_journal=self.journal,root=Path(f.tmp.name)/'fresh-chain',expected_bucket='fixture-private-bucket',enabled=True)
        self.assertEqual(len(staged['clip_receipts']),4);self.assertEqual(len(self.bucket.writes),8)
        self.assertEqual(staged['provider_requests'],0)
    def test_unapproved_automated_candidate_escalates_without_human_relabel(self):
        result=self.capture(review_loader=lambda record:{'state':'REVIEW_REQUIRED','human_approved':False,'live_release':False})
        self.assertEqual(result['state'],'REVIEW_REQUIRED')
        self.assertEqual(result['review_protocol'],'unapproved_automated_candidate')
        with self.assertRaises(ValueError):self.store.get_record(self.fixture.job,self.image,0)

    def test_missing_semantic_review_pauses_then_resumes_without_download(self):
        first=self.capture(review_loader=lambda record:None);self.assertEqual(first['state'],'REVIEW_REQUIRED')
        before=list(self.calls);second=self.capture();self.assertEqual(second['state'],'READY');self.assertEqual(self.calls,before)
    def test_disabled_before_cloud_or_provider(self):
        with self.assertRaises(ValueError):self.capture(enabled=False)
        self.assertEqual(self.calls,[]);self.assertEqual(self.bucket.writes,[]);self.assertEqual(self.client.data,{})
    def test_pending_only_one_readonly_status(self):
        transport=self.transport(0);transport.status=lambda handle:'IN_QUEUE'
        self.assertEqual(self.capture(transport=transport)['state'],'IN_QUEUE');self.assertEqual(self.bucket.writes,[])
    def test_unknown_submission_never_capture_or_retry(self):
        for key,value in self.journal.client.data.items():
            if '/operations/' in key:
                value['state']='may_have_submitted';break
        with self.assertRaises(ValueError):self.capture()
        self.assertEqual(self.calls,[])
    def test_unknown_upload_holds_then_explicit_readonly_reconcile(self):
        self.bucket.fail_after=lambda name,match:True
        with self.assertRaises(TimeoutError):self.capture()
        before=list(self.calls);self.assertEqual(self.capture()['state'],'HOLD');self.assertEqual(self.calls,before)
        self.bucket.fail_after=None
        self.store.reconcile(self.fixture.job,self.image,0)
        self.assertEqual(self.capture()['state'],'READY');self.assertEqual(self.calls,before);self.assertEqual(len(self.bucket.writes),1)
    def test_unknown_capture_claim_never_uploads_on_replay(self):
        self.client.unknown_commit=True
        with self.assertRaises(TimeoutError):self.capture()
        before=list(self.calls);self.assertEqual(self.capture()['state'],'HOLD')
        self.assertEqual(self.calls,before);self.assertEqual(self.bucket.writes,[])
        with self.assertRaises(Exception):self.store.reconcile(self.fixture.job,self.image,0)
        self.assertEqual(self.bucket.writes,[])
    def test_unfunded_or_different_plan_operation_rejected(self):
        for key,value in self.journal.client.data.items():
            if '/plan_allocations/' in key:
                value['plan_sha256']='f'*64;break
        with self.assertRaises(ValueError):self.capture()
        self.assertEqual(self.calls,[])
    def test_provider_result_request_identity_cannot_change(self):
        transport=self.transport(0);original=transport.result
        def result(handle):value=original(handle);value['request_id']='other_request';return value
        transport.result=result
        with self.assertRaises(ValueError):self.capture(transport=transport)
        self.assertNotIn('download',self.calls);self.assertEqual(self.bucket.writes,[])
    def test_extension_requires_previous_accepted_clip(self):
        with self.assertRaises(ValueError):self.capture(1)
        self.assertEqual(self.calls,[])
    def test_wrong_output_review_rejected(self):
        review=self.review
        def wrong(record):value=review(record);value['clip_sha256']='f'*64;return value
        with self.assertRaises(ValueError):self.capture(review_loader=wrong)
        self.assertEqual(self.store.get_capture(self.fixture.job,self.image,0)['state'],'MEDIA')
    def test_wrong_narration_provenance_rejected(self):
        with self.assertRaises(ValueError):self.capture(narration_report={**self.fixture.report,'synthetic':False})
        self.assertEqual(self.calls,[])
    def test_recovered_generation_and_hash_checked(self):
        self.capture();name=next(iter(self.bucket.objects));generation,data,mime=self.bucket.objects[name]
        self.bucket.objects[name]=(generation,b'changed',mime)
        with self.assertRaises(ValueError):self.capture()
    def test_actual_downloaded_full_mp4_is_decoded_before_ready(self):
        import subprocess
        f=self.fixture;path=Path(f.tmp.name)/'provider-fixture.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=navy:s=72x128:r=24:d=10',
            '-c:v','libx264','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True)
        body=path.read_bytes();transport=self.transport(0);old=transport.result
        def result(handle):value=old(handle);value['bytes']=len(body);return value
        transport.result=result;transport.download=lambda result:dict(bytes=body,sha256=hashlib.sha256(body).hexdigest())
        result=capture_segment(job=f.job,plan=f.plan,expected_plan_sha256=f.sha,image_id=self.image,sequence=0,spend_journal=self.journal,
            transport=transport,segment_store=self.store,review_loader=self.review,root=Path(f.tmp.name)/'actual-capture',narration_report=f.report,enabled=True)
        self.assertEqual(result['state'],'READY');self.assertEqual(result['verified_clip'].duration_seconds,10)
    def test_reviewed_provider_rounding_crop_is_bound_and_persisted(self):
        import subprocess
        from byron_video.segment_capture import ReviewedProviderGeometry
        from byron_video.elevenlabs_adapter import fingerprint
        from byron_video.portrait_composition import release_bindings
        f=self.fixture
        f.job.snapshot['processing_plan'].update(release_bindings())
        f.job.snapshot_sha256=fingerprint(f.job.snapshot)
        path=Path(f.tmp.name)/'provider-rounded-fixture.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=navy:s=768x1344:r=24:d=10',
            '-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True)
        body=path.read_bytes();transport=self.transport(0);old=transport.result
        def result(handle):value=old(handle);value['bytes']=len(body);return value
        transport.result=result;transport.download=lambda result:dict(bytes=body,sha256=hashlib.sha256(body).hexdigest())
        result=capture_segment(job=f.job,plan=f.plan,expected_plan_sha256=f.sha,image_id=self.image,sequence=0,spend_journal=self.journal,
            transport=transport,segment_store=self.store,review_loader=self.review,root=Path(f.tmp.name)/'rounded-capture',narration_report=f.report,
            crop_resolver=ReviewedProviderGeometry(),enabled=True)
        self.assertEqual(result['state'],'READY')
        self.assertEqual(result['record']['presentation_crop'],{'left':6,'top':0,'width':756,'height':1344})
        scene=next(x for x in f.plan['scenes'] if x['image_id']==self.image)
        f.job.snapshot['processing_plan']['visual_style_sha256']='0'*64
        with self.assertRaises(ValueError):ReviewedProviderGeometry()(f.job,path,result['record']['video'],scene)

    def test_router_exact_pinned_endpoints_and_activation(self):
        from byron_video.segment_capture import MotionTransportRouter
        from byron_video.motion_continuation import IMAGE_ENDPOINT,EXTENSION_ENDPOINT
        events=[];active=[True]
        def transport(name):
            return SimpleNamespace(submit=lambda r:events.append(name),status=lambda h:'COMPLETED',result=lambda h:{'url':'fixture'},download=lambda r:events.append(name+'-download'))
        router=MotionTransportRouter(image_transport=transport('image'),extension_transport=transport('extension'),activation_check=lambda:active[0])
        with self.assertRaises(ValueError):router.submit({'endpoint':IMAGE_ENDPOINT})
        router.enabled=True;router.submit({'endpoint':IMAGE_ENDPOINT});router.submit({'endpoint':EXTENSION_ENDPOINT})
        router.download(router.result({'endpoint':EXTENSION_ENDPOINT}));self.assertEqual(events,['image','extension','extension-download'])
        with self.assertRaises(ValueError):router.submit({'endpoint':'unreviewed'})
        active[0]=False
        with self.assertRaises(ValueError):router.status({'endpoint':IMAGE_ENDPOINT})
    def test_wrong_full_duration_never_accepted(self):
        f=self.fixture
        with patch('byron_video.portrait_composition.probe',return_value={'duration':5}):
            with self.assertRaises(ValueError):capture_segment(job=f.job,plan=f.plan,expected_plan_sha256=f.sha,image_id=self.image,sequence=0,spend_journal=self.journal,transport=self.transport(0),segment_store=self.store,review_loader=self.review,root=Path(f.tmp.name)/'bad-duration',narration_report=f.report,enabled=True)
        self.assertEqual(self.store.get_capture(f.job,self.image,0)['state'],'MEDIA')

if __name__=='__main__':unittest.main()
