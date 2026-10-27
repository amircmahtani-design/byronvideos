"""Worker boundary and real local decode tests; semantic evidence is synthetic."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from byron_video import worker_binding as worker
from byron_video.elevenlabs_adapter import fingerprint


def fixture():
    target={'schema_version':1,'status':'DRAFT','live_release':False,'project_id':'synthetic-project',
        'region':'us-central1','job_name':'byron-renderer','bucket':'synthetic-private-bucket',
        'container_image':'us-central1-docker.pkg.dev/synthetic-project/renderer/film@sha256:'+'a'*64,
        'cpu':'2','memory':'4Gi','task_timeout_seconds':1800,'task_count':1,'parallelism':1,'max_retries':0}
    snapshot={'input_order':[f'image-00{i}' for i in range(1,5)],
        'images':[{'id':f'image-00{i}','position':i,'sha256':str(i)*64} for i in range(1,5)],
        'narrated_text_sha256':hashlib.sha256(b'Synthetic exact text.').hexdigest()}
    trusted={'job_id':'a'*32,'owner_id':'owner-0001','snapshot_sha256':fingerprint(snapshot),'frozen_job':snapshot}
    ns=hashlib.sha256(trusted['owner_id'].encode()).hexdigest();data={}
    def ref(name,payload,mime):
        name=f'owners/{ns}/jobs/{trusted["job_id"]}/'+name;data[name]=payload
        return dict(bucket=target['bucket'],object=name,generation='1234',sha256=hashlib.sha256(payload).hexdigest(),bytes=len(payload),content_type=mime)
    manifest=dict(schema_version=1,status='DRAFT_NOT_ACTIVATED',job_id=trusted['job_id'],owner_namespace=ns,
        source_job_sha256=trusted['snapshot_sha256'],source_order=snapshot['input_order'],
        composition=ref('inputs/composition.json',b'{"fixture":true}','application/json'),
        narration=ref('inputs/narration.wav',b'SYNTHETIC-WAV-FIXTURE','audio/wav'),clips=[])
    for i,image in enumerate(snapshot['images'],1):
        manifest['clips'].append(dict(image_id=image['id'],position=i,original_sha256=image['sha256'],
             video=ref(f'inputs/clip-{i}.mp4',f'SYNTHETIC-VIDEO-{i}'.encode(),'video/mp4')))
    manifest['sha256']=fingerprint(manifest)
    outer=ref('render.json',worker.canonical(manifest),'application/json')
    def reader(r):
        return dict(generation=r['generation'],content_type=r['content_type'],stream=io.BytesIO(data[r['object']]))
    return target,manifest,outer,trusted,reader,data


class WorkerBindingChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_digest_pinned_container_required(self):
        valid='registry.example/runtime/python@sha256:'+'a'*64
        self.assertEqual(worker.validate_container_base(valid),valid)
        for bad in ['python:3.12','',None,valid+'suffix']:
            with self.subTest(bad=bad),self.assertRaises(ValueError):worker.validate_container_base(bad)

    def test_bound_inputs_download_exact_private_bytes(self):
        target,m,ref,t,reader,data=fixture()
        staged=worker.stage_bound_inputs(target=target,actual_target=copy.deepcopy(target),manifest=m,manifest_ref=ref,trusted_job=t,reader=reader,root=self.root/'run')
        self.assertEqual(staged['snapshot_sha256'],t['snapshot_sha256'])
        self.assertEqual(staged['files']['narration.wav'].read_bytes(),data[m['narration']['object']])
        self.assertEqual(staged['files']['narration.wav'].stat().st_mode & 0o777,0o600)
        self.assertEqual(staged['root'].stat().st_mode & 0o777,0o700)

    def test_target_mismatch_fails_before_storage(self):
        target,m,ref,t,reader,data=fixture();actual={**target,'max_retries':1};calls=[]
        with self.assertRaises(ValueError):worker.stage_bound_inputs(target=target,actual_target=actual,manifest=m,manifest_ref=ref,trusted_job=t,reader=lambda x:calls.append(x),root=self.root/'run')
        self.assertEqual(calls,[])

    def test_manifest_storage_tamper_rejected(self):
        target,m,ref,t,reader,data=fixture();data[ref['object']]+=b' '
        with self.assertRaises(ValueError):worker.stage_bound_inputs(target=target,actual_target=target,manifest=m,manifest_ref=ref,trusted_job=t,reader=reader,root=self.root/'run')
        self.assertFalse((self.root/'run/render.json').exists())

    def test_owner_snapshot_tamper_rejected_before_download(self):
        target,m,ref,t,reader,data=fixture();t['owner_id']='different-owner'
        with self.assertRaises(ValueError):worker.stage_bound_inputs(target=target,actual_target=target,manifest=m,manifest_ref=ref,trusted_job=t,reader=reader,root=self.root/'run')
        self.assertFalse((self.root/'run').exists())

    def test_generation_and_unbounded_reader_rejected(self):
        target,m,ref,t,reader,data=fixture()
        with self.assertRaises(ValueError):worker.copy_verified_object(lambda r:dict(generation='999',content_type=r['content_type'],stream=io.BytesIO(b'x')),ref,self.root/'bad')
        class Bad(io.BytesIO):
            def read(self,size):return b'x'*(size+1)
        with self.assertRaises(ValueError):worker.copy_verified_object(lambda r:dict(generation=r['generation'],content_type=r['content_type'],stream=Bad()),ref,self.root/'bad2')
        self.assertFalse((self.root/'bad2').exists())

    def test_workspace_reuse_and_symlink_rejected(self):
        with self.assertRaises(ValueError):worker.safe_private_root(self.root)
        (self.root/'link').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):worker.safe_private_root(self.root/'link/run')

    def test_worker_disabled_or_missing_compositor_fail_before_reads(self):
        args=dict(target=None,actual_target=None,manifest=None,manifest_ref=None,job=None,execution_id=None,
                  reader=None,root=self.root/'run',compositor=None,semantic_verifier=None,artifact_writer=None,expected_ffmpeg_version='x')
        with self.assertRaises(ValueError):worker.execute_worker(**args)
        with self.assertRaises(ValueError):worker.execute_worker(**args,enabled=True)
        self.assertFalse((self.root/'run').exists())

    def job(self):
        _,_,_,t,_,_=fixture()
        return SimpleNamespace(id=t['job_id'],owner_id=t['owner_id'],snapshot_sha256=t['snapshot_sha256'],
                 snapshot=t['frozen_job'],receipt=SimpleNamespace(execution_id='execution_fixture'))

    def evidence(self,path,job):
        # Synthetic contract fixture, NOT evidence of spoken/captioned content.
        return worker.SemanticEvidence(hashlib.sha256(path.read_bytes()).hexdigest(),job.snapshot_sha256,
             tuple(job.snapshot['input_order']),job.snapshot['narrated_text_sha256'],job.snapshot['narrated_text_sha256'],True,True,True)

    def test_probe_alone_cannot_mint_verified_artifact(self):
        path=self.root/'film.mp4';path.write_bytes(b'not media')
        with self.assertRaises(ValueError):worker.verify_artifact(path,job=self.job(),artifact_id='artifact_fixture',semantic_verifier=None,expected_ffmpeg_version='x')

    def test_full_decode_failure_never_mints_proof(self):
        path=self.root/'film.mp4';path.write_bytes(b'fake bytes')
        def run(args,timeout):
            if args[1]=='-version':return SimpleNamespace(stdout=b'ffmpeg version fixture\n')
            if args[0]=='ffprobe':return SimpleNamespace(stdout=json.dumps(dict(streams=[dict(codec_type='video',codec_name='h264',width=1080,height=1920,avg_frame_rate='24/1'),dict(codec_type='audio',codec_name='aac')],format=dict(duration='1',format_name='mp4'))).encode())
            raise subprocess.CalledProcessError(1,args)
        with self.assertRaises(subprocess.CalledProcessError):worker.verify_artifact(path,job=self.job(),artifact_id='artifact_fixture',semantic_verifier=self.evidence,expected_ffmpeg_version='fixture',run=run)

    def test_real_ffmpeg_full_decode_synthetic_portrait(self):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.fail('Installed FFmpeg/ffprobe required; do not silently skip media check.')
        path=self.root/'synthetic.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=black:s=1080x1920:r=24:d=0.25',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=0.25','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',
            '-c:a','aac','-shortest',str(path)],check=True,capture_output=True,timeout=60)
        version=subprocess.check_output(['ffmpeg','-version']).decode().splitlines()[0].split()[2]
        proof=worker.verify_artifact(path,job=self.job(),artifact_id='artifact_fixture',semantic_verifier=self.evidence,expected_ffmpeg_version=version)
        self.assertTrue(proof.full_decode_verified)
        self.assertEqual((proof.width,proof.height,proof.fps),(1080,1920,24))
        self.assertEqual(proof.sha256,hashlib.sha256(path.read_bytes()).hexdigest())
        with self.assertRaises(ValueError):worker.verify_artifact(path,job=self.job(),artifact_id='artifact_fixture',semantic_verifier=lambda p,j:None,expected_ffmpeg_version=version)

    def dispatcher(self, *, fail=False, resolver=True, enabled=True):
        target,m,ref,t,reader,data=fixture()
        job=self.job();job.state='HOLD';job.receipt=None
        calls=[]
        class Journal:
            armed=False;operation=None
            def reserve_dispatch(self,*args):
                if self.armed:return False
                self.armed=True;return True
            def record_dispatch_operation(self,*args):self.operation=args[-1]
            def get_dispatch_operation(self,*args):return self.operation
        journal=Journal()
        operation='projects/synthetic-project/locations/us-central1/operations/operation_fixture'
        def transport(*args):
            calls.append(args)
            if fail:raise TimeoutError('synthetic uncertain response')
            return {'name':operation}
        def execution(op,target,job):
            return dict(operation_name=op,execution_name='projects/synthetic-project/locations/us-central1/jobs/byron-renderer/executions/execution_fixture',
                        target_sha256=fingerprint(target),snapshot_sha256=job.snapshot_sha256)
        dispatcher=worker.CloudRunDispatcher(target=target,target_provider=lambda:target,
             store=SimpleNamespace(get=lambda owner,id:job),journal=journal,handoff_loader=lambda j:(m,ref),
             transport=transport,execution_resolver=execution if resolver else None,enabled=enabled)
        return dispatcher,t,calls,journal

    def test_dispatch_reserves_once_before_actual_run_shape(self):
        d,h,calls,journal=self.dispatcher()
        receipt=d(h)
        self.assertTrue(journal.armed)
        self.assertEqual(receipt.execution_id,'execution_fixture')
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0],'POST')
        self.assertTrue(calls[0][1].endswith('/jobs/byron-renderer:run'))
        with self.assertRaises(ValueError):d(h)
        self.assertEqual(len(calls),1)
        self.assertEqual(d.recover(h['owner_id'],h['job_id']),receipt)
        self.assertEqual(len(calls),1)

    def test_uncertain_dispatch_never_posts_twice(self):
        d,h,calls,journal=self.dispatcher(fail=True)
        with self.assertRaises(worker.DispatchPending):d(h)
        with self.assertRaises(ValueError):d(h)
        with self.assertRaises(worker.DispatchPending):d.recover(h['owner_id'],h['job_id'])
        self.assertEqual(len(calls),1)

    def test_operation_is_not_execution_receipt(self):
        d,h,calls,journal=self.dispatcher(resolver=False)
        with self.assertRaises(worker.DispatchPending):d(h)
        self.assertIsNotNone(journal.operation)
        with self.assertRaises(worker.DispatchPending):d.recover(h['owner_id'],h['job_id'])
        self.assertEqual(len(calls),1)

    def test_dispatch_disabled_before_transport(self):
        d,h,calls,journal=self.dispatcher(enabled=False)
        with self.assertRaises(ValueError):d(h)
        self.assertFalse(journal.armed);self.assertEqual(calls,[])

    def test_completion_commit_occurs_only_after_verified_writer_result(self):
        job=self.job();proof=object();reference={'generation':'1234'};calls=[]
        registry=SimpleNamespace(commit_verified=lambda owner,j,p,r:calls.append((owner.subject,j,p,r)) or 'complete')
        with patch.object(worker,'execute_worker',return_value={'artifact':proof,'private_reference':reference,'completion_reconciled':False}) as executed:
            result=worker.execute_and_commit_worker(artifact_registry=registry,job=job)
        self.assertTrue(result['completion_reconciled']);self.assertEqual(result['committed_job'],'complete')
        self.assertEqual(calls,[(job.owner_id,job,proof,reference)]);executed.assert_called_once()

    def test_unknown_completion_commit_does_not_repeat_worker(self):
        def fail(*args):raise TimeoutError('synthetic unknown commit')
        with patch.object(worker,'execute_worker',return_value={'artifact':object(),'private_reference':{}}) as executed:
            with self.assertRaises(TimeoutError):worker.execute_and_commit_worker(artifact_registry=SimpleNamespace(commit_verified=fail),job=self.job())
        executed.assert_called_once()

    def test_activation_expiry_during_handoff_prevents_arm_and_post(self):
        dispatcher,handoff,calls,journal=self.dispatcher();active=[True]
        def check():
            if not active[0]:raise ValueError('Synthetic activation expired')
            return True
        original=dispatcher.handoff_loader
        def loader(job):
            value=original(job);active[0]=False;return value
        dispatcher.activation_check=check;dispatcher.handoff_loader=loader
        with self.assertRaises(ValueError):dispatcher(handoff)
        self.assertFalse(journal.armed);self.assertEqual(calls,[])

    def test_activation_expiry_after_reserve_never_posts_or_rearms(self):
        dispatcher,handoff,calls,journal=self.dispatcher();active=[True]
        def check():
            if not active[0]:raise ValueError('Synthetic activation expired')
            return True
        reserve=journal.reserve_dispatch
        def arm(*args):
            result=reserve(*args);active[0]=False;return result
        dispatcher.activation_check=check;journal.reserve_dispatch=arm
        with self.assertRaises(worker.DispatchPending):dispatcher(handoff)
        self.assertTrue(journal.armed);self.assertEqual(calls,[])
        with self.assertRaises(ValueError):dispatcher.recover(handoff['owner_id'],handoff['job_id'])

    def test_activation_callback_must_explicitly_return_true(self):
        for verdict in [False,None,1,'approved']:
            dispatcher,handoff,calls,journal=self.dispatcher()
            dispatcher.activation_check=lambda verdict=verdict:verdict
            with self.subTest(verdict=verdict),self.assertRaises(ValueError):dispatcher(handoff)
            self.assertEqual(calls,[]);self.assertFalse(journal.armed)


if __name__=='__main__':unittest.main(verbosity=2)
