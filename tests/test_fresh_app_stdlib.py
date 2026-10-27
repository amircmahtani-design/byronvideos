"""Fresh HTTP-to-original-worker invocation only; media completion is separate."""
import unittest
from fresh_app_fixture import FreshApplicationFixture,VOICE
from byron_video.original_input import validate_original_job
from byron_video.hosted_control import digest


class FreshApplicationChecks(unittest.TestCase):
    def test_signed_session_upload_voice_and_fresh_run_without_prepared_media(self):
        fixture=FreshApplicationFixture()
        try:
            job=fixture.submit()
            self.assertEqual(job.state,'queued');self.assertEqual(job.receipt.execution_id,'renderer-fixture')
            self.assertEqual(job.snapshot['processing_plan']['narration_profile']['voice_id'],VOICE)
            validate_original_job(job,digest(fixture.config))
            self.assertEqual(len(fixture.run_requests),1)
            env={x['name']:x['value'] for x in fixture.run_requests[0]['overrides']['containerOverrides'][0]['env']}
            self.assertEqual(env['BYRON_INPUT_PROTOCOL'],'originals-v1')
            self.assertTrue(all('narration' not in name and 'clip-' not in name for name in fixture.bucket.objects))
            repeat=fixture.client.post('/api/jobs',json=fixture.body,headers=fixture.headers)
            self.assertTrue(repeat.json()['reused']);self.assertEqual(len(fixture.run_requests),1)
            status=fixture.client.get('/api/jobs/'+job.id)
            self.assertEqual(status.json()['status'],'queued')
            self.assertEqual(fixture.client.get('/api/jobs/'+job.id+'/video').status_code,409)
        finally:fixture.close()


if __name__=='__main__':unittest.main()
