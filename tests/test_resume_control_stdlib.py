"""Authenticated resume uses fake cloud resources and no provider requests."""
import copy
from datetime import datetime,timedelta,timezone
import hashlib
import unittest
from byron_video.resume_control import FirestoreResumeJournal
from fresh_app_fixture import FreshApplicationFixture


class ResumeControlChecks(unittest.TestCase):
    def setUp(self):
        self.f=FreshApplicationFixture();self.job=self.f.submit()
        self.journal=FirestoreResumeJournal(self.f.firestore,project_id=self.f.config['target']['project_id'],
            namespace=self.f.config['firestore']['namespace'],transaction_runner=self.f.firestore.run)
    def tearDown(self):self.f.close()
    def held(self,reason='queue_deadline',terminal=True):
        job=self.f.runtime.jobs.get(self.f.owner,self.job.id)
        job=self.f.runtime.jobs.record_worker_hold(job.owner_id,job.id,job.receipt.execution_id,reason)
        if terminal:
            execution=self.f.resources[self.f.name+'/executions/'+job.receipt.execution_id]
            execution.update(completionTime=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),
                runningCount=0,succeededCount=1,conditions=[dict(type='Completed',state='CONDITION_SUCCEEDED')])
        return job
    def resume(self,key='fixture_resume_request_0001',headers=None,path=None,**kwargs):
        return self.f.client.post(path or '/api/jobs/'+self.job.id+'/resume',
            headers=headers if headers is not None else {**self.f.headers,'Idempotency-Key':key},**kwargs)

    def test_owner_resume_archives_previous_execution_and_preserves_every_frozen_input(self):
        prior=self.held();before=copy.deepcopy(prior.snapshot)
        response=self.resume();self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(response.json()['resume_attempt'],1);self.assertEqual(response.json()['status'],'queued')
        after=self.f.runtime.jobs.get(self.f.owner,self.job.id)
        self.assertEqual(after.snapshot,before);self.assertEqual(after.idempotency_key,prior.idempotency_key)
        self.assertNotEqual(after.receipt.execution_id,prior.receipt.execution_id)
        record=self.journal.find_request(after,'fixture_resume_request_0001')
        self.assertEqual(record['prior_receipt_json'],prior.receipt_json)
        self.assertEqual(record['prior_hold']['reason'],'queue_deadline')
        self.assertEqual(self.journal.active_dispatch(after).attempt,1)
        self.assertEqual(len(self.f.run_requests),2)
        replay=self.resume();self.assertTrue(replay.json()['reused']);self.assertEqual(len(self.f.run_requests),2)
        ordinary=self.f.client.post('/api/jobs',json=self.f.body,headers=self.f.headers)
        self.assertTrue(ordinary.json()['reused']);self.assertEqual(len(self.f.run_requests),2)

    def test_prior_worker_must_be_independently_terminal(self):
        prior=self.held(terminal=False)
        response=self.resume();self.assertEqual(response.status_code,409)
        self.assertEqual(self.f.runtime.jobs.get(self.f.owner,self.job.id),prior)
        self.assertEqual(len(self.f.run_requests),1)
        self.assertIsNone(self.journal.active_dispatch(prior))

    def test_csrf_origin_body_and_administrator_owner_allowlist(self):
        self.held()
        for headers in [{}, {**self.f.headers,'Origin':'https://attacker.example'}, {**self.f.headers,'X-CSRF-Token':'wrong'}]:
            self.assertEqual(self.resume(headers=headers).status_code,403)
        self.assertEqual(self.resume(json={'new_plan':'forbidden'}).status_code,422)
        self.assertEqual(self.resume(path='/api/admin/jobs/google_'+'b'*64+'/'+self.job.id+'/resume').status_code,404)
        self.assertEqual(len(self.f.run_requests),1)
        admin=self.resume(path='/api/admin/jobs/'+self.f.owner+'/'+self.job.id+'/resume')
        self.assertEqual(admin.status_code,202,admin.text);self.assertEqual(len(self.f.run_requests),2)

    def test_max_three_resume_attempts_preserves_append_only_history(self):
        snapshot=self.job.snapshot_json
        for number in range(1,4):
            self.held();response=self.resume(key=f'fixture_resume_request_{number:04}')
            self.assertEqual(response.status_code,202,response.text);self.assertEqual(response.json()['resume_attempt'],number)
            self.assertEqual(self.f.runtime.jobs.get(self.f.owner,self.job.id).snapshot_json,snapshot)
        before=self.held();blocked=self.resume(key='fixture_resume_request_0004')
        self.assertEqual(blocked.status_code,409);self.assertEqual(len(self.f.run_requests),4)
        self.assertEqual(self.f.runtime.jobs.get(self.f.owner,self.job.id),before)
        for number in range(1,4):self.assertEqual(self.journal.find_request(before,f'fixture_resume_request_{number:04}')['attempt'],number)

    def test_unknown_paid_or_render_outcome_is_not_eligible_for_resume(self):
        before=self.held(reason='provider_outcome_unknown')
        self.assertEqual(self.resume().status_code,409)
        self.assertEqual(self.f.runtime.jobs.get(self.f.owner,self.job.id),before)
        before=self.held()
        base='byron_control/'+self.f.config['firestore']['namespace']+'/owners/'+hashlib.sha256(self.f.owner.encode()).hexdigest()
        self.f.firestore.data[base+'/worker_attempts/'+self.job.id]={'synthetic':'attempted render outcome unknown'}
        self.assertEqual(self.resume().status_code,409)
        self.assertEqual(len(self.f.run_requests),1)

    def test_unknown_resume_post_is_never_automatically_reissued(self):
        self.held();calls=[]
        def lost(*args):calls.append(args);raise TimeoutError('Synthetic lost response')
        self.f.runtime.dispatcher.transport=lost
        first=self.resume();self.assertEqual(first.status_code,202,first.text);self.assertEqual(first.json()['status'],'HOLD')
        replay=self.resume();self.assertTrue(replay.json()['reused']);self.assertEqual(len(calls),1)
        self.assertEqual(self.resume(key='fixture_resume_request_0002').status_code,409)
        self.assertEqual(len(calls),1)

    def test_ui_resume_eligibility_requires_known_terminal_wait_and_no_review_blocker(self):
        self.held(terminal=False)
        path='/api/jobs/'+self.job.id
        self.assertFalse(self.f.client.get(path).json()['can_resume'])
        self.held()
        eligible=self.f.client.get(path).json()
        self.assertTrue(eligible['can_resume']);self.assertEqual(eligible['resume_attempts_remaining'],3)
        for reason in ('provider_outcome_unknown','narration_outcome_unknown','prompt_review','output_review'):
            self.held(reason=reason)
            self.assertFalse(self.f.client.get(path).json()['can_resume'])
        self.assertEqual(len(self.f.run_requests),1)

    def test_stale_resume_journal_cannot_recover_new_attempt(self):
        self.held();self.resume();job=self.f.runtime.jobs.get(self.f.owner,self.job.id)
        old=self.journal.active_dispatch(job)
        self.held();self.resume(key='fixture_resume_request_0002')
        with self.assertRaises(ValueError):old.get_dispatch_operation(job.owner_id,job.id,job.snapshot_sha256)


if __name__=='__main__':unittest.main()
