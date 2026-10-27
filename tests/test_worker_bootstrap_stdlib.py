"""Explicit production assembly tested with local SDK/transport doubles only."""
import copy
from dataclasses import replace
import hashlib
import io
from contextlib import redirect_stdout,redirect_stderr
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from byron_video import worker_bootstrap as boot
from byron_video.hosted_control import Principal,DispatchReceipt
from byron_video.cloud_journal import FirestoreJobStore
from byron_video.worker_binding import canonical
from byron_video.elevenlabs_adapter import fingerprint
from test_cloud_journal_stdlib import Client
from test_cloud_storage_stdlib import FakeBucket
from test_deployment_plan_stdlib import config as deployment_config


class WorkerBootstrapChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.config=deployment_config()
        self.config['runtime']['portrait_candidate_sha256']=hashlib.sha256((Path(__file__).resolve().parent.parent/'style-guide/portrait-candidate/release.json').read_bytes()).hexdigest()
        self.lock=canonical({'python':f'{sys.version_info.major}.{sys.version_info.minor}','packages':{name:'fixture' for name in boot.REQUIRED_PACKAGES}})
        self.config['runtime']['sdk_lock_sha256']=hashlib.sha256(self.lock).hexdigest()
        self.client=Client();self.bucket=FakeBucket();self.calls=[]
        storage=SimpleNamespace(project='synthetic-project',bucket=lambda name:self.bucket)
        self.bindings=boot.WorkerBindings(storage,self.client,lambda:self.calls.append('token'),lambda *args:None,self.client.run,lambda url:self.calls.append(url))
        self.common=dict(project_id='synthetic-project',namespace=self.config['firestore']['namespace'],transaction_runner=self.client.run)
        self.store=FirestoreJobStore(self.client,**self.common)
        self.owner=Principal('fixture_owner')
        self.snapshot={'narrated_text':'Synthetic words.','narrated_text_sha256':hashlib.sha256(b'Synthetic words.').hexdigest(),
                       'input_order':[f'painting_{i:03}' for i in range(1,5)],
                       'images':[dict(id=f'painting_{i:03}',position=i,sha256=str(i)*64) for i in range(1,5)],
                       'processing_plan':{'narration_profile':self.config['narration_profile']}}
        self.job,_=self.store.reserve(self.owner.subject,'fixture_request_000001','a'*64,self.snapshot)
    def tearDown(self):self.tmp.cleanup()

    def runtime(self,enabled=True):
        with patch.object(boot,'validate_runtime',return_value={}):
            return boot.PrivateWorkerRuntime(self.config,self.bindings,enabled=enabled,sdk_lock_bytes=self.lock)

    def report(self):
        return {'provenance':{'owner_job_sha256':hashlib.sha256((self.owner.subject+'\0'+self.job.id).encode()).hexdigest()},
                'exact_text':self.snapshot['narrated_text'],'profile_sha256':fingerprint(self.config['narration_profile'])}
    def clips(self):return [dict(image_id=id) for id in self.snapshot['input_order']]

    def test_disabled_assembly_makes_no_cloud_or_token_call(self):
        runtime=boot.PrivateWorkerRuntime(self.config,self.bindings)
        with self.assertRaises(ValueError):runtime.run({},self.root/'work')
        self.assertEqual(self.calls,[]);self.assertEqual(self.bucket.writes,[])

    def test_enabled_direct_assembly_requires_runtime_lock(self):
        with self.assertRaises(ValueError):boot.PrivateWorkerRuntime(self.config,self.bindings,enabled=True)
        self.assertEqual(self.calls,[])

    def test_clients_must_match_project(self):
        bad=replace(self.bindings,storage_client=SimpleNamespace(project='other-project'))
        with self.assertRaises(ValueError):boot.PrivateWorkerRuntime(self.config,bad)

    def test_runtime_rejects_changed_lock_or_missing_dependency_before_cloud(self):
        with self.assertRaises(ValueError):boot.validate_runtime(self.config,b'{}')
        with patch.object(boot.importlib.metadata,'version',side_effect=boot.importlib.metadata.PackageNotFoundError):
            with self.assertRaises(RuntimeError):boot.validate_runtime(self.config,self.lock)
        self.assertEqual(self.calls,[])

    def test_runtime_validates_exact_versions_and_portrait_source_lock(self):
        with patch.object(boot.importlib.metadata,'version',return_value='fixture'),patch.object(boot.subprocess,'run',return_value=SimpleNamespace(stdout=b'ffmpeg version synthetic-version\n')):
            boot.validate_runtime(self.config,self.lock)
            changed=copy.deepcopy(self.config);changed['runtime']['portrait_candidate_sha256']='0'*64
            with self.assertRaises(ValueError):boot.validate_runtime(changed,self.lock)

    def test_worker_evidence_is_immutable_owner_bound_and_readback_checked(self):
        evidence=boot.FirestoreWorkerEvidence(self.client,**self.common)
        stored=evidence.commit(self.owner,self.job,self.report(),self.clips())
        self.assertEqual(evidence.load(self.owner,self.job),stored)
        self.assertEqual(evidence.commit(self.owner,self.job,self.report(),self.clips()),stored)
        changed=self.report();changed['other']='changed'
        with self.assertRaises(ValueError):evidence.commit(self.owner,self.job,changed,self.clips())
        with self.assertRaises(ValueError):evidence.load(Principal('other_owner'),self.job)

    def test_evidence_rejects_wrong_job_or_order(self):
        evidence=boot.FirestoreWorkerEvidence(self.client,**self.common);report=self.report()
        report['provenance']['owner_job_sha256']='b'*64
        with self.assertRaises(ValueError):evidence.commit(self.owner,self.job,report,self.clips())
        with self.assertRaises(ValueError):evidence.commit(self.owner,self.job,self.report(),list(reversed(self.clips())))

    def test_attempt_reservation_survives_unknown_outcome_and_blocks_rerender(self):
        job=self.store.record_dispatch(self.owner.subject,self.job.id,DispatchReceipt(self.job.id,self.job.snapshot_sha256,'execution_fixture'))
        evidence=boot.FirestoreWorkerEvidence(self.client,**self.common)
        self.client.unknown_commit=True
        with self.assertRaises(TimeoutError):evidence.reserve_attempt(self.owner,job,'execution_fixture')
        with self.assertRaises(ValueError):evidence.reserve_attempt(self.owner,job,'execution_fixture')

    def test_private_file_must_be_pinned_regular_and_not_world_readable(self):
        path=self.root/'private.json';path.write_bytes(b'{}');path.chmod(0o644)
        with self.assertRaises(ValueError):boot.private_bytes(path,100)
        path.chmod(0o600);self.assertEqual(boot.private_bytes(path,100),b'{}')
        link=self.root/'link';link.symlink_to(path)
        with self.assertRaises(ValueError):boot.private_bytes(link,100)

    def test_private_bindings_hash_checked_before_import(self):
        path=self.root/'bindings.py';path.write_text("raise RuntimeError('must not execute')\n");path.chmod(0o600)
        with self.assertRaises(ValueError):boot.load_private_bindings(path,'0'*64,self.config)

    def test_explicit_sdk_credentials_are_required_no_discovery(self):
        with self.assertRaises(ValueError):boot.explicit_google_clients(self.config,None,semantic_verifier=lambda *a:None,token_reader=lambda:None)
        self.assertEqual(self.calls,[])

    def test_json_duplicates_and_nonfinite_values_rejected(self):
        for raw in ['{"a":1,"a":2}','{"a":NaN}']:
            with self.assertRaises(ValueError):boot.strict_json(raw)

    def manifest_fixture(self):
        ns=hashlib.sha256(self.owner.subject.encode()).hexdigest();prefix=f'owners/{ns}/jobs/{self.job.id}/';data={}
        def ref(name,payload,mime):
            name=prefix+name;data[name]=payload
            return dict(bucket=self.config['target']['bucket'],object=name,generation='42',sha256=hashlib.sha256(payload).hexdigest(),bytes=len(payload),content_type=mime)
        manifest=dict(schema_version=1,status='DRAFT_NOT_ACTIVATED',job_id=self.job.id,owner_namespace=ns,source_job_sha256=self.job.snapshot_sha256,
            source_order=self.snapshot['input_order'],composition=ref('inputs/composition.json',b'{}','application/json'),
            narration=ref('inputs/narration.wav',b'synthetic audio','audio/wav'),clips=[])
        for i,image in enumerate(self.snapshot['images'],1):
            manifest['clips'].append(dict(image_id=image['id'],position=i,original_sha256=image['sha256'],video=ref(f'inputs/clip-{i}.mp4',f'video{i}'.encode(),'video/mp4')))
        manifest['sha256']=fingerprint(manifest);outer=ref('render.json',canonical(manifest),'application/json')
        for name,payload in data.items():self.bucket.objects[name]=(42,payload,'application/json' if name.endswith('.json') else 'audio/wav' if name.endswith('.wav') else 'video/mp4')
        invocation=dict(job_id=self.job.id,owner_namespace=ns,manifest_bucket=outer['bucket'],manifest_object=outer['object'],
            manifest_generation='42',manifest_sha256=outer['sha256'],plan_sha256=manifest['sha256'],execution_id='execution_fixture')
        return manifest,outer,invocation

    def test_bound_gcs_reader_uses_generation_hash_and_owner_namespace(self):
        manifest,outer,invocation=self.manifest_fixture()
        reader=boot.BoundCloudInputReader(self.bindings.storage_client,bucket=outer['bucket'],owner_namespace=invocation['owner_namespace'],job_id=self.job.id)
        self.assertEqual(reader.manifest(invocation),(manifest,outer))
        bad={**outer,'object':'owners/'+'0'*64+'/jobs/'+self.job.id+'/render.json'}
        with self.assertRaises(ValueError):reader(bad)
        self.assertEqual(self.bucket.writes,[])

    def test_private_runtime_wires_readback_receipts_compositor_and_atomic_registry(self):
        manifest,outer,invocation=self.manifest_fixture();runtime=self.runtime()
        runtime.evidence.commit(self.owner,self.job,self.report(),self.clips())
        runtime.dispatch.reserve_dispatch(self.owner.subject,self.job.id,self.job.snapshot_sha256,'a'*64)
        runtime.dispatch.record_dispatch_operation(self.owner.subject,self.job.id,self.job.snapshot_sha256,'projects/synthetic-project/locations/us-central1/operations/operation_fixture')
        prefix='projects/synthetic-project/locations/us-central1/jobs/byron-renderer/executions/'
        readback=SimpleNamespace(execution_resolver=lambda op,target,job:{'execution_name':prefix+'execution_fixture'},target_provider=lambda:self.config['target'])
        captured=[]
        def execute(**kwargs):captured.append(kwargs);return {'completion_reconciled':True}
        with patch.object(runtime,'_load_job',return_value=self.job),patch.object(boot,'CloudRunReadback',return_value=readback),patch.object(boot,'execute_and_commit_worker',side_effect=execute):
            result=runtime.run(invocation,self.root/'work')
        self.assertTrue(result['completion_reconciled']);self.assertEqual(len(captured),1)
        args=captured[0];self.assertIs(args['artifact_registry'],runtime.registry);self.assertIs(args['artifact_writer'],runtime.writer)
        self.assertIsInstance(args['compositor'],boot.PortraitCandidateCompositor);self.assertIsInstance(args['reader'],boot.BoundCloudInputReader)
        self.assertEqual(args['job'].state,'running');self.assertEqual(args['execution_id'],'execution_fixture')
        with patch.object(runtime,'_load_job',return_value=self.store.get(self.owner.subject,self.job.id)),patch.object(boot,'CloudRunReadback',return_value=readback):
            with self.assertRaises(ValueError):runtime.run(invocation,self.root/'another')
        self.assertEqual(self.calls,[])

    def test_continuation_context_is_hash_bound_and_immutable(self):
        evidence=boot.FirestoreWorkerEvidence(self.client,**self.common)
        context={'schema_version':1,'plan_sha256':'b'*64,'independent_receipts':True}
        value=evidence.commit(self.owner,self.job,self.report(),self.clips(),continuation_context=context)
        self.assertEqual(value['continuation_context_sha256'],fingerprint(context))
        self.assertEqual(evidence.load(self.owner,self.job)['continuation_context'],context)
        with self.assertRaises(ValueError):evidence.commit(self.owner,self.job,self.report(),self.clips(),continuation_context={**context,'plan_sha256':'c'*64})

    def test_cloud_run_environment_contains_only_explicit_verified_invocation_fields(self):
        _,_,inv=self.manifest_fixture()
        env={'BYRON_JOB_ID':inv['job_id'],'BYRON_OWNER_NAMESPACE':inv['owner_namespace'],'BYRON_MANIFEST_BUCKET':inv['manifest_bucket'],
             'BYRON_MANIFEST_OBJECT':inv['manifest_object'],'BYRON_MANIFEST_GENERATION':inv['manifest_generation'],'BYRON_MANIFEST_SHA256':inv['manifest_sha256'],
             'BYRON_PLAN_SHA256':inv['plan_sha256'],'CLOUD_RUN_EXECUTION':inv['execution_id'],'UNRELATED_SECRET':'never-read'}
        self.assertEqual(boot.invocation_from_environment(env,self.config['target']),inv)
        env['CLOUD_RUN_EXECUTION']=''
        with self.assertRaises(ValueError):boot.invocation_from_environment(env,self.config['target'])

    def test_job_lookup_by_namespace_is_bounded_and_checks_owner_hash(self):
        _,_,inv=self.manifest_fixture();runtime=self.runtime();original=self.client.document;seen=[]
        def document(path):
            ref=original(path)
            def get(**kwargs):seen.append(kwargs);return ref.get()
            return SimpleNamespace(get=get)
        with patch.object(self.client,'document',side_effect=document):
            self.assertEqual(runtime._load_job(inv),self.job)
        self.assertEqual(seen,[{'retry':None,'timeout':15}])
        altered={**inv,'owner_namespace':'0'*64}
        with patch.object(self.client,'document',side_effect=document):
            with self.assertRaises(ValueError):runtime._load_job(altered)

    def test_cli_default_validates_without_importing_bindings_or_running_worker(self):
        config=self.root/'config.json';config.write_text(json.dumps(self.config));config.chmod(0o600)
        lock=self.root/'sdk.json';lock.write_bytes(self.lock);lock.chmod(0o600)
        output=io.StringIO()
        with patch.object(boot,'validate_runtime',return_value={}),patch.object(boot,'load_private_bindings') as loader,redirect_stdout(output):
            code=boot.main(['--config',str(config),'--sdk-lock',str(lock)])
        self.assertEqual(code,0);loader.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())['executed'])
        self.assertEqual(self.calls,[])

    def test_cli_execute_without_explicit_bindings_does_not_load_or_echo_private_config(self):
        config=self.root/'config.json';config.write_text(json.dumps(self.config));config.chmod(0o600)
        lock=self.root/'sdk.json';lock.write_bytes(self.lock);lock.chmod(0o600)
        errors=io.StringIO()
        with patch.object(boot,'validate_runtime',return_value={}),patch.object(boot,'load_private_bindings') as loader,redirect_stderr(errors):
            code=boot.main(['--config',str(config),'--sdk-lock',str(lock),'--execute'])
        self.assertEqual(code,2);loader.assert_not_called();self.assertNotIn(self.config['target']['project_id'],errors.getvalue())

    def test_startup_receipt_race_only_polls_existing_journal(self):
        runtime=self.runtime();reads=[];sleeps=[];runtime.clock=lambda:0;runtime.sleep=lambda seconds:sleeps.append(seconds)
        def operation(*args):
            reads.append(args)
            return None if len(reads)==1 else 'projects/synthetic-project/locations/us-central1/operations/operation_fixture'
        runtime.dispatch=SimpleNamespace(get_dispatch_operation=operation)
        self.assertTrue(runtime._await_operation(self.job).endswith('operation_fixture'))
        self.assertEqual(len(reads),2);self.assertEqual(sleeps,[.5]);self.assertEqual(self.calls,[])

    def test_authorized_resume_uses_only_its_attempt_operation(self):
        runtime=self.runtime();seen=[]
        runtime.resumes=SimpleNamespace(active_dispatch=lambda job:SimpleNamespace(get_dispatch_operation=lambda *args:'resume-operation'))
        runtime.dispatch=SimpleNamespace(get_dispatch_operation=lambda *args:seen.append('forbidden-original'))
        self.assertEqual(runtime._await_operation(self.job),'resume-operation');self.assertEqual(seen,[])

    def test_corrupt_resume_never_falls_back_to_original_dispatch(self):
        runtime=self.runtime();seen=[]
        def invalid(job):raise ValueError('changed resume')
        runtime.resumes=SimpleNamespace(active_dispatch=invalid)
        runtime.dispatch=SimpleNamespace(get_dispatch_operation=lambda *args:seen.append('forbidden-original'))
        with self.assertRaises(ValueError):runtime._await_operation(self.job)
        self.assertEqual(seen,[])

    def test_unresolved_receipt_stops_without_runjob_or_paid_retry(self):
        runtime=self.runtime();ticks=iter([0,31]);runtime.clock=lambda:next(ticks);runtime.sleep=lambda seconds:self.fail('Deadline already passed')
        runtime.dispatch=SimpleNamespace(get_dispatch_operation=lambda *args:None)
        with self.assertRaises(ValueError):runtime._await_operation(self.job)
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main(verbosity=2)
