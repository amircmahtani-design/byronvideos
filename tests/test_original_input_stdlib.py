"""Original-input dispatch tests use synthetic cloud responses, never real runs."""
import copy
from dataclasses import asdict
from types import SimpleNamespace
import unittest
from byron_video.hosted_control import VerifiedImage, digest
from byron_video.hosted_contract import freeze_job
from byron_video.original_input import (build_original_execution_request, validate_original_job,
    OriginalInputRequestLoader, OriginalInputCloudRunDispatcher, ENV_KEYS)
from byron_video.worker_binding import DispatchPending
from test_deployment_plan_stdlib import config
from test_hosted_uploads_stdlib import plan


def original_job():
    processing=plan();owner='fixture_owner'
    images=[{**asdict(VerifiedImage(f'painting_{i:03}',owner,str(i)*64,1000,640,480,'image/png',f'private_object_{i:03}','42',True)), 'position':i} for i in range(1,5)]
    contract=freeze_job('Exact synthetic words.',[{k:x[k] for k in ('id','position','sha256')} for x in images],processing['narration_profile'],processing['visual_style_sha256'])
    snapshot=dict(raw_script='Exact synthetic words.',narrated_text=contract['narrated_text'],narrated_text_sha256=contract['narrated_text_sha256'],
        images=images,input_order=[x['id'] for x in images],processing_plan=processing,processing_plan_sha256=digest(processing),
        narration_contract=contract,runtime_config_sha256=digest(config()))
    return SimpleNamespace(id='a'*32,owner_id=owner,state='HOLD',receipt=None,snapshot=snapshot,snapshot_sha256=digest(snapshot))


class Journal:
    def __init__(self):self.armed=False;self.operation=None
    def reserve_dispatch(self,*args):
        if self.armed:return False
        self.armed=True;return True
    def record_dispatch_operation(self,*args):self.operation=args[-1]
    def get_dispatch_operation(self,*args):return self.operation


class OriginalInputChecks(unittest.TestCase):
    def setUp(self):
        self.job=original_job();self.target=config()['target'];self.journal=Journal();self.calls=[]
        self.handoff=dict(owner_id=self.job.owner_id,job_id=self.job.id,snapshot_sha256=self.job.snapshot_sha256,frozen_job=self.job.snapshot)
        self.prefix='projects/synthetic-project/locations/us-central1/'
    def dispatcher(self,**changes):
        def transport(method,url,body):
            self.calls.append((method,url,body));return {'name':self.prefix+'operations/operation_0001'}
        def resolver(operation,target,job):
            return dict(operation_name=operation,execution_name=self.prefix+'jobs/byron-renderer/executions/execution_0001',
                target_sha256=digest(target),snapshot_sha256=job.snapshot_sha256)
        args=dict(target=self.target,target_provider=lambda:self.target,store=SimpleNamespace(get=lambda *args:self.job),journal=self.journal,
            request_loader=OriginalInputRequestLoader(digest(config())),transport=transport,execution_resolver=resolver,enabled=True)
        args.update(changes);return OriginalInputCloudRunDispatcher(**args)

    def test_run_request_contains_only_five_bound_identifiers_no_media(self):
        request=build_original_execution_request(self.target,self.job,digest(config()))
        env={x['name']:x['value'] for x in request['json']['overrides']['containerOverrides'][0]['env']}
        self.assertEqual(set(env),ENV_KEYS);self.assertEqual(env['BYRON_INPUT_PROTOCOL'],'originals-v1')
        self.assertNotIn(self.job.owner_id,str(env));self.assertNotIn('Exact synthetic words',str(env))
        self.assertNotIn('narration.wav',str(env));self.assertNotIn('clip',str(env));self.assertNotIn('voice_id',str(env))
        self.assertEqual(env['BYRON_SNAPSHOT_SHA256'],self.job.snapshot_sha256)

    def test_changed_generation_owner_order_or_voice_fails_before_dispatch(self):
        for change in [lambda s:s['images'][0].update(storage_version='latest'),
                lambda s:s['images'][0].update(owner_id='other_owner'),lambda s:s['images'].reverse(),
                lambda s:s.update(runtime_config_sha256='f'*64),lambda s:s['processing_plan']['narration_profile'].update(voice_id='CHANGED_VOICE')]:
            job=copy.deepcopy(self.job);change(job.snapshot);job.snapshot_sha256=digest(job.snapshot)
            with self.subTest(change=change),self.assertRaises(ValueError):validate_original_job(job,digest(config()))

    def test_disabled_dispatch_never_arms_or_posts(self):
        with self.assertRaises(ValueError):self.dispatcher(enabled=False)(self.handoff)
        self.assertFalse(self.journal.armed);self.assertEqual(self.calls,[])

    def test_one_run_receipt_requires_independent_execution_and_never_reposts(self):
        dispatcher=self.dispatcher();receipt=dispatcher(self.handoff)
        self.assertEqual(receipt.execution_id,'execution_0001');self.assertTrue(self.journal.armed);self.assertEqual(len(self.calls),1)
        with self.assertRaises(ValueError):dispatcher(self.handoff)
        self.assertEqual(len(self.calls),1)
        recovered=dispatcher.recover(self.job.owner_id,self.job.id)
        self.assertEqual(recovered,receipt);self.assertEqual(len(self.calls),1)

    def test_operation_is_not_fabricated_as_execution_receipt(self):
        with self.assertRaises(DispatchPending):self.dispatcher(execution_resolver=None)(self.handoff)
        self.assertEqual(len(self.calls),1);self.assertIsNotNone(self.journal.operation)
        self.assertEqual(self.job.state,'HOLD');self.assertIsNone(self.job.receipt)

    def test_uncertain_run_without_operation_stays_armed_and_never_retries(self):
        def lost(*args):self.calls.append(args);raise TimeoutError('synthetic uncertainty')
        dispatcher=self.dispatcher(transport=lost)
        with self.assertRaises(DispatchPending):dispatcher(self.handoff)
        with self.assertRaises(ValueError):dispatcher(self.handoff)
        with self.assertRaises(DispatchPending):dispatcher.recover(self.job.owner_id,self.job.id)
        self.assertTrue(self.journal.armed);self.assertEqual(len(self.calls),1)

    def test_generation_expiry_during_token_acquisition_never_posts(self):
        current=[True];calls=self.calls
        class TokenDelay:
            def submit_authorized(self,method,url,body,check):
                current[0]=False
                check()
                calls.append('POST')
        dispatcher=self.dispatcher(transport=TokenDelay())
        def preflight(job):
            if not current[0]:raise ValueError('expired generation')
            return True
        dispatcher.job_preflight=preflight
        with self.assertRaises(DispatchPending):dispatcher(self.handoff)
        self.assertTrue(self.journal.armed);self.assertEqual(self.calls,[])
        self.assertEqual(self.job.state,'HOLD')

    def test_expiry_after_reservation_blocks_post_and_keeps_hold(self):
        def check():return not self.journal.armed
        with self.assertRaises(DispatchPending):self.dispatcher(activation_check=check)(self.handoff)
        self.assertTrue(self.journal.armed);self.assertEqual(self.calls,[]);self.assertEqual(self.job.state,'HOLD')


if __name__=='__main__':unittest.main()
