"""Fresh full HTTP → original-input worker → verified private download fixture.

Google/cloud/provider responses and semantic reviews are SYNTHETIC test doubles.
Native audio decoding, segment validation, portrait rendering, full MP4 decode,
private storage adapters and HTTP delivery are real local code. The pinned Google
SDK admission gate is not exercised: FreshFixture injects an admitted fake-SDK
worker for this offline contract test. No live readiness or human approval claim.
"""
import hashlib
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from fresh_app_fixture import FreshApplicationFixture,VOICE
from test_generation_coordinator_stdlib import FreshFixture
from test_hosted_uploads_stdlib import plan
from byron_video.hosted_control import Principal,digest
from byron_video.worker_bootstrap import original_invocation_from_environment


class FreshAppWorkerChecks(unittest.TestCase):
    def test_fresh_authenticated_generate_to_native_worker_and_private_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            media=FreshFixture(root)
            processing={**plan(),**media.job.snapshot['processing_plan']}
            api=FreshApplicationFixture(config_value=media.config,processing_plan=processing)
            try:
                job=api.submit(script=media.job.snapshot['raw_script'])
                self.assertEqual(job.state,'queued')
                self.assertTrue(all('narration' not in name and 'clip-' not in name for name in api.bucket.objects))
                self.assertEqual(len(api.run_requests),1)
                # Reuse ONLY synthetic provider response bytes/callbacks. All runtime
                # job, identity, storage and original bytes come from the HTTP graph.
                media.config=api.config;media.client=api.firestore;media.sdk=api.storage;media.bucket=api.bucket
                media.jobs=api.runtime.jobs;media.job=job;media.owner=Principal(job.owner_id)
                media.common=dict(project_id=api.config['target']['project_id'],namespace=api.config['firestore']['namespace'],transaction_runner=api.firestore.run)
                worker=media.runtime()
                worker.bindings=replace(worker.bindings,read_transport=api.read)
                worker.read=api.read
                env={x['name']:x['value'] for x in api.run_requests[0]['overrides']['containerOverrides'][0]['env']}
                env['CLOUD_RUN_EXECUTION']='renderer-fixture'
                invocation=original_invocation_from_environment(env,api.config)
                result=worker.run_original(invocation,root/'fresh-worker')
                self.assertTrue(result['completion_reconciled'])
                complete=api.runtime.jobs.get(api.owner,job.id)
                self.assertEqual(complete.state,'complete')
                self.assertEqual(complete.snapshot['processing_plan']['narration_profile']['voice_id'],VOICE)
                self.assertEqual(complete.snapshot['raw_script'],api.body['script'])
                self.assertEqual(complete.snapshot['runtime_config_sha256'],digest(api.config))
                self.assertEqual(media.calls.count('narration-submit'),1)
                self.assertEqual(media.calls.count('motion-submit'),4)
                status=api.client.get('/api/jobs/'+job.id)
                self.assertEqual(status.status_code,200);self.assertEqual(status.json()['status'],'complete')
                download=api.client.get(status.json()['download_url'])
                self.assertEqual(download.status_code,200)
                self.assertEqual(download.headers['content-type'],'video/mp4')
                self.assertEqual(hashlib.sha256(download.content).hexdigest(),complete.artifact.sha256)
                self.assertEqual((complete.artifact.width,complete.artifact.height),(1080,1920))
                counts=list(media.calls)
                replay=api.client.post('/api/jobs',json=api.body,headers=api.headers)
                self.assertTrue(replay.json()['reused']);self.assertEqual(len(api.run_requests),1)
                again=worker.run_original(invocation,root/'do-not-rerender')
                self.assertTrue(again['already_complete']);self.assertEqual(media.calls,counts)
                api.client.cookies.clear()
                self.assertEqual(api.client.get(status.json()['download_url']).status_code,401)
            finally:api.close()

    def test_queue_hold_explicit_resume_reuses_paid_receipts_and_downloads_same_frozen_film(self):
        from datetime import datetime,timedelta,timezone
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);media=FreshFixture(root)
            api=FreshApplicationFixture(config_value=media.config,processing_plan={**plan(),**media.job.snapshot['processing_plan']})
            try:
                job=api.submit(script=media.job.snapshot['raw_script'])
                media.config=api.config;media.client=api.firestore;media.sdk=api.storage;media.bucket=api.bucket
                media.jobs=api.runtime.jobs;media.job=job;media.owner=Principal(job.owner_id)
                media.common=dict(project_id=api.config['target']['project_id'],namespace=api.config['firestore']['namespace'],transaction_runner=api.firestore.run)
                media.queue_status='IN_QUEUE';worker=media.runtime();base_factory=worker.bindings.generation_factory
                def one_observation(current,runtime):
                    coordinator=base_factory(current,runtime);coordinator.poll_timeout_seconds=0
                    return coordinator
                worker.bindings=replace(worker.bindings,generation_factory=one_observation,read_transport=api.read);worker.read=api.read
                env={x['name']:x['value'] for x in api.run_requests[0]['overrides']['containerOverrides'][0]['env']}
                env['CLOUD_RUN_EXECUTION']='renderer-fixture'
                held=worker.run_original(original_invocation_from_environment(env,api.config),root/'held-worker')
                self.assertEqual(held['state'],'HOLD')
                self.assertEqual(api.client.get('/api/jobs/'+job.id).json()['status'],'HOLD')
                self.assertEqual(media.calls.count('narration-submit'),1);self.assertEqual(media.calls.count('motion-submit'),1)
                self.assertEqual(len(api.run_requests),1)
                # Fake Cloud Run GET reports the first process actually terminated;
                # no cancellation or resource mutation is performed by source code.
                api.resources[api.execution].update(completionTime=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),
                    runningCount=0,succeededCount=1,conditions=[dict(type='Completed',state='CONDITION_SUCCEEDED')])
                headers={**api.headers,'Idempotency-Key':'fixture_explicit_resume_0001'}
                resumed=api.client.post('/api/jobs/'+job.id+'/resume',headers=headers)
                self.assertEqual(resumed.status_code,202,resumed.text);self.assertEqual(resumed.json()['resume_attempt'],1)
                self.assertEqual(len(api.run_requests),2)
                self.assertEqual(api.client.post('/api/jobs/'+job.id+'/resume',headers=headers).json()['reused'],True)
                self.assertEqual(len(api.run_requests),2)
                media.queue_status='COMPLETED';media.job=api.runtime.jobs.get(api.owner,job.id)
                second=media.runtime();second.bindings=replace(second.bindings,read_transport=api.read);second.read=api.read
                env={x['name']:x['value'] for x in api.run_requests[1]['overrides']['containerOverrides'][0]['env']}
                env['CLOUD_RUN_EXECUTION']='renderer-fixture-2'
                result=second.run_original(original_invocation_from_environment(env,api.config),root/'resumed-worker')
                self.assertTrue(result['completion_reconciled'])
                complete=api.runtime.jobs.get(api.owner,job.id)
                self.assertEqual(complete.snapshot_json,job.snapshot_json)
                self.assertEqual(media.calls.count('narration-submit'),1);self.assertEqual(media.calls.count('motion-submit'),4)
                response=api.client.get('/api/jobs/'+job.id+'/video')
                self.assertEqual(response.status_code,200)
                self.assertEqual(hashlib.sha256(response.content).hexdigest(),complete.artifact.sha256)
                self.assertEqual(len(api.run_requests),2)
            finally:api.close()


if __name__=='__main__':unittest.main()
