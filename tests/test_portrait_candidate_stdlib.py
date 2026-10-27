"""Separate portrait/ElevenLabs candidate tests; synthetic media only."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from byron_video import portrait_composition as portrait
from byron_video.elevenlabs_adapter import fingerprint, plan_requests


class PortraitCandidateChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def fixture(self):
        profile={'schema_version':1,'version':'synthetic-fixture-1','status':'DRAFT','provider':'elevenlabs','voice_id':'synthetic_voice_fixture',
           'model_id':'eleven_multilingual_v2','voice_settings':{'stability':.5,'similarity_boost':.75,'style':0,'use_speaker_boost':True,'speed':.93},
           'output_format':'mp3_44100_128','seed':1,'apply_text_normalization':'off'}
        text='One two three four.'
        images=[{'id':f'image_{i:03}','sha256':str(i)*64,'position':i} for i in range(1,5)]
        frozen={'narrated_text':text,'narrated_text_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'processing_plan':{'narration_profile':profile,**portrait.release_bindings()},'images':images,'input_order':[x['id'] for x in images]}
        audio=self.root/'narration.wav';audio.write_bytes(b'synthetic-audio')
        report={'status':'DRAFT_RELEASE_CANDIDATE','provider':'elevenlabs','media_verified':True,'provider_receipts_verified':True,
            'live_release':False,'human_approved':False,'synthetic':True,'exact_text':text,'duration_seconds':5.9,
            'profile':profile,'profile_sha256':fingerprint(profile),'ffmpeg_version':'fixture',
            'audio_sha256':hashlib.sha256(audio.read_bytes()).hexdigest(),'words':[
                dict(text='One',start=0,end=1,char_start=0,char_end=3),dict(text='two',start=1,end=2,char_start=4,char_end=7),
                dict(text='three',start=2,end=3,char_start=8,char_end=13),dict(text='four.',start=3,end=4,char_start=14,char_end=19)]}
        report['provenance']={'owner_job_sha256':hashlib.sha256(b'owner_fixture\0job_fixture').hexdigest(),'exact_text_sha256':frozen['narrated_text_sha256'],'profile_sha256':fingerprint(profile),'plan_sha256':fingerprint(plan_requests(text,profile))}
        expected=fingerprint(report);receipts=[];files={'narration.wav':audio}
        planned=[]
        for i,img in enumerate(images,1):
            path=self.root/f'clip-{i:02}.mp4';path.write_bytes(f'SYNTHETIC-VIDEO-{i}'.encode());files[path.name]=path
            receipts.append(dict(image_id=img['id'],source_image_sha256=img['sha256'],provider='fal',model='minimax/h3-max-turbo/image-to-video',
                media_verified=True,no_invented_action_reviewed=True,synthetic=True,presentation_crop=None,request_id=f'fixture_req_{i}',operation_id=f'fixture_op_{i}',sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            planned.append(dict(image_id=img['id'],trim_length=2,presentation_crop=None))
        comp=self.root/'composition.json';comp.write_text(json.dumps(dict(schema_version=1,status='DRAFT_RELEASE_CANDIDATE',
            snapshot_sha256=fingerprint(frozen),narration_report_sha256=expected,clips=planned)));files['composition.json']=comp
        return dict(root=self.root,files=files,snapshot_sha256=fingerprint(frozen),owner_id='owner_fixture',job_id='job_fixture'),frozen,report,expected,receipts

    def validate(self,staged,frozen,report,expected,receipts):
        with patch.object(portrait,'run',return_value=SimpleNamespace(stdout=b'ffmpeg version fixture\n')),patch.object(portrait,'probe',side_effect=lambda path,video,presentation_crop=None:dict(duration=2 if video else 5.9,stream={})):
            return portrait.validate_candidate(staged,frozen,report=report,expected_report_sha256=expected,clip_receipts=receipts,expected_ffmpeg_version='fixture')

    def test_separate_visual_release_preserves_portrait_geometry(self):
        style=portrait.presentation()
        self.assertEqual((style['canvas']['width'],style['canvas']['height']),(1080,1920))
        self.assertNotIn('narration',style)
        self.assertEqual(style['transitions']['crossfade_seconds'],.7)

    def test_candidate_binds_exact_narration_and_unique_ordered_footage(self):
        args=self.fixture();result=self.validate(*args)
        self.assertEqual(result[-1],5.9)
        self.assertEqual(len(result[2]),4)
        for actual,expected in zip(result[-2],[0,1.3,2.6,3.9]):self.assertAlmostEqual(actual,expected)

    def test_frozen_visual_and_motion_release_must_match_candidate(self):
        for field in ('visual_style_sha256','motion_policy_sha256'):
            for value in (None,'f'*64):
                with self.subTest(field=field,value=value):
                    args=list(self.fixture())
                    if value is None:args[1]['processing_plan'].pop(field)
                    else:args[1]['processing_plan'][field]=value
                    args[0]['snapshot_sha256']=fingerprint(args[1])
                    with self.assertRaisesRegex(ValueError,'Frozen visual/motion release'):
                        self.validate(*args)

    def test_native_frame_count_and_declared_duration_must_agree(self):
        info={'format':{'format_name':'mov,mp4','duration':'2'},'streams':[{'codec_type':'video','codec_name':'h264','width':270,'height':480,'duration':'2','avg_frame_rate':'24/1','r_frame_rate':'24/1'}]}
        frames={'frames':[{'best_effort_timestamp_time':str(i/24)} for i in range(96)]}
        with patch.object(portrait,'run_bounded',side_effect=[SimpleNamespace(stdout=json.dumps(info).encode()),SimpleNamespace(stdout=json.dumps(frames).encode())]):
            with self.assertRaisesRegex(ValueError,'duration disagrees'):portrait.probe(self.root/'fixture.mp4',video=True)

    def test_metadata_stream_is_bounded_before_full_allocation(self):
        import sys
        with self.assertRaisesRegex(ValueError,'bounded output'):
            portrait.run_bounded([sys.executable,'-c','print("x"*100000)'],timeout=5,stdout_limit=1024)

    def test_legacy_or_changed_narration_is_not_relabelled(self):
        args=list(self.fixture());args[2]['provider']='kokoro';args[3]=fingerprint(args[2])
        with self.assertRaises(ValueError):self.validate(*args)

    def test_unreviewed_or_wrong_source_receipt_rejected(self):
        args=list(self.fixture());args[4][0]['source_image_sha256']='a'*64
        with self.assertRaises(ValueError):self.validate(*args)

    def test_missing_continuous_footage_rejected(self):
        args=self.fixture();p=args[0]['files']['composition.json'];v=json.loads(p.read_text());v['clips'][0]['trim_length']=1;p.write_text(json.dumps(v))
        with self.assertRaises(ValueError):self.validate(*args)

    def test_recovered_rounding_crop_stays_centered_and_bounded(self):
        self.assertEqual(portrait.validate_presentation_crop(dict(left=6,top=0,width=756,height=1344),768,1344)['width'],756)
        with self.assertRaises(ValueError):portrait.validate_presentation_crop(dict(left=0,top=0,width=756,height=1344),768,1344)

    def test_real_short_synthetic_composition_reuses_narration_without_tts(self):
        staged,frozen,report,_,receipts=self.fixture()
        audio=staged['files']['narration.wav'];audio.unlink()
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=24000:duration=5.9','-ac','1','-c:a','pcm_s16le',str(audio)],check=True,capture_output=True,timeout=30)
        for i,colour in enumerate(['red','blue','green','yellow'],1):
            path=staged['files'][f'clip-{i:02}.mp4'];path.unlink()
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',f'color=c={colour}:s=270x480:r=24:d=2','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True,timeout=30)
            receipts[i-1]['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        version=subprocess.check_output(['ffmpeg','-version']).decode().splitlines()[0].split()[2]
        report['ffmpeg_version']=version;report['audio_sha256']=hashlib.sha256(audio.read_bytes()).hexdigest();expected=fingerprint(report)
        comp=staged['files']['composition.json'];v=json.loads(comp.read_text());v['narration_report_sha256']=expected;comp.write_text(json.dumps(v))
        result=portrait.compose_candidate(staged,frozen,self.root/'film.mp4',report=report,expected_report_sha256=expected,clip_receipts=receipts,expected_ffmpeg_version=version)
        self.assertEqual(result['narration_provider'],'elevenlabs')
        self.assertEqual(result['synthesis_passes_this_composition'],0)
        self.assertFalse(result['human_approved'])
        self.assertEqual((result['width'],result['height'],result['fps']),(1080,1920,24))
        self.assertTrue(result['source_order_preserved'])
        self.assertFalse(result['loop']);self.assertFalse(result['retime']);self.assertFalse(result['freeze_extension'])
        self.assertEqual(result['narration_text'],frozen['narrated_text'])
        self.assertEqual(result['source_order'],frozen['input_order'])

    def test_native_release_to_composition_verified_registry_and_download(self):
        # Every service below is a local fake; FFmpeg decoding/composition is real.
        import base64
        from test_cloud_journal_stdlib import Client
        from test_cloud_storage_stdlib import FakeBucket
        from test_provider_execution_stdlib import profile
        from byron_video.cloud_journal import FirestoreJobStore,FirestoreArtifactRegistry
        from byron_video.cloud_storage import CloudArtifactWriter,CloudArtifactReader
        from byron_video.hosted_control import Principal,DispatchReceipt
        from byron_video.provider_execution import SpendApproval,FileResponseStore,WireResponse,execute_narration_once
        from byron_video.worker_journal import Journal
        from byron_video.elevenlabs_release import build_narration_candidate
        from byron_video.worker_binding import verify_artifact,SemanticEvidence
        client=Client();store=FirestoreJobStore(client,project_id='synthetic-project',namespace='fixture_namespace',transaction_runner=client.run)
        registry=FirestoreArtifactRegistry(client,project_id='synthetic-project',namespace='fixture_namespace',transaction_runner=client.run)
        owner=Principal('fixture_owner');text='Synthetic words, unchanged.';voice=profile()
        images=[dict(id=f'painting_{i:03}',position=i,sha256=str(i)*64) for i in range(1,5)]
        frozen=dict(narrated_text=text,narrated_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    images=images,input_order=[x['id'] for x in images],processing_plan=dict(narration_profile=voice,**portrait.release_bindings()))
        job,_=store.reserve(owner.subject,'fixture_request_000001','b'*64,frozen)
        job=store.record_dispatch(owner.subject,job.id,DispatchReceipt(job.id,job.snapshot_sha256,'fixture_execution_001'))
        version=subprocess.check_output(['ffmpeg','-version']).decode().splitlines()[0].split()[2]
        tone=self.root/'source-tone.mp3'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=44100:duration=4','-c:a','libmp3lame','-b:a','128k',str(tone)],check=True,capture_output=True)
        journal=Journal(self.root/'provider.sqlite');archive=FileResponseStore(self.root/'responses')
        requests=plan_requests(text,voice);approval=SpendApproval('synthetic_approval',fingerprint(requests),(2,),5,2)
        calls=[]
        def transport(request):
            calls.append(request);spoken=request['body']['text'];n=len(spoken)
            payload=json.dumps({'audio_base64':base64.b64encode(tone.read_bytes()).decode(),'alignment':dict(characters=list(spoken),
                character_start_times_seconds=[i*3/n for i in range(n)],character_end_times_seconds=[(i+1)*3/n for i in range(n)])}).encode()
            return WireResponse('synthetic_request_1',payload)
        try:
            execute_narration_once(owner_id=owner.subject,job_id=job.id,text=text,profile=voice,approval=approval,journal=journal,transport=transport,response_store=archive)
            report=build_narration_candidate(owner_id=owner.subject,job_id=job.id,text=text,profile=voice,approval=approval,journal=journal,response_store=archive,
                expected_profile_sha256=fingerprint(voice),expected_text_sha256=frozen['narrated_text_sha256'],job_root=self.root/'candidate',expected_ffmpeg_version=version,
                audio_settings={'integrated_lufs':-16,'true_peak_dbfs':-1,'lra':11},source_kind='synthetic_test')
        finally:journal.close()
        root=self.root/'candidate';files={'narration.wav':root/'narration.wav'};receipts=[];planned=[]
        for i,colour in enumerate(['red','blue','green','yellow'],1):
            path=root/f'clip-{i:02}.mp4';files[path.name]=path
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',f'color=c={colour}:s=270x480:r=24:d=2','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True)
            receipts.append(dict(image_id=images[i-1]['id'],source_image_sha256=images[i-1]['sha256'],provider='fal',model='minimax/h3-max-turbo/image-to-video',
                media_verified=True,no_invented_action_reviewed=True,synthetic=True,presentation_crop=None,request_id=f'fixture_req_{i}',operation_id=f'fixture_op_{i}',sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            planned.append(dict(image_id=images[i-1]['id'],trim_length=1.525,presentation_crop=None))
        expected=fingerprint(report);comp=root/'composition.json';comp.write_text(json.dumps(dict(schema_version=1,status='DRAFT_RELEASE_CANDIDATE',
            snapshot_sha256=job.snapshot_sha256,narration_report_sha256=expected,clips=planned)));files['composition.json']=comp
        staged=dict(root=root,files=files,snapshot_sha256=job.snapshot_sha256,owner_id=owner.subject,job_id=job.id)
        output=root/'film.mp4';result=portrait.compose_candidate(staged,frozen,output,report=report,expected_report_sha256=expected,clip_receipts=receipts,expected_ffmpeg_version=version)
        # Synthetic semantic evidence tests plumbing only, never actual speech QA.
        def semantics(path,j):
            return SemanticEvidence(hashlib.sha256(path.read_bytes()).hexdigest(),j.snapshot_sha256,tuple(j.snapshot['input_order']),
                j.snapshot['narrated_text_sha256'],j.snapshot['narrated_text_sha256'],True,True,True)
        proof=verify_artifact(output,job=job,artifact_id='fixture_artifact_001',semantic_verifier=semantics,expected_ffmpeg_version=version)
        bucket=FakeBucket();sdk=SimpleNamespace(bucket=lambda name:bucket)
        writer=CloudArtifactWriter(sdk,bucket='synthetic-private-bucket')
        reference=writer(owner,job,proof.artifact_id,output,proof)
        completed=registry.commit_verified(owner,job,proof,reference)
        reader=CloudArtifactReader(sdk,bucket='synthetic-private-bucket',reference_resolver=registry)
        with reader(owner,completed,proof) as stream:self.assertEqual(stream.read(),output.read_bytes())
        self.assertEqual(completed.state,'complete');self.assertEqual(registry.verify(owner,completed,proof.artifact_id),proof)
        self.assertEqual(len(calls),1);self.assertEqual(result['synthesis_passes_this_composition'],0)
        self.assertFalse(result['human_approved']);self.assertFalse(result['provider_api_integration_verified'])


if __name__=='__main__':unittest.main(verbosity=2)
