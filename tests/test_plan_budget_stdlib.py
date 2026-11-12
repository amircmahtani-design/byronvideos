"""Atomic full-plan reservations against a local Firestore transaction fake."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from byron_video.cloud_journal import FirestoreSpendJournal
from test_cloud_journal_stdlib import Client


class FullPlanBudgetChecks(unittest.TestCase):
    def setUp(self):self.client=Client();self.journal=self.make()
    def make(self,job='f'*32,total=100,prior=10):
        return FirestoreSpendJournal(self.client,project_id='synthetic-project',namespace='fixture_namespace',owner_id='fixture_owner',job_id=job,
            budget_scope='fixture_campaign',approved_total_cap_cents=total,prior_spend_cents=prior,transaction_runner=self.client.run)
    def reserve(self,allocations=None,cap=80):
        return self.journal.reserve_plan('fixture_plan','a'*64,allocations or {'fixture_image':20,'fixture_extend':20,'fixture_allowance':5},cap)
    def used(self):return self.client.data[self.journal.root+'/'+self.journal.path]['reserved_cents']

    def test_existing_narration_near_cap_blocks_entire_plan_atomically(self):
        self.journal.arm('fixture_narration',{'stage':'narration'},40,80)
        before=dict(self.client.data)
        with self.assertRaisesRegex(ValueError,'Complete motion plan'):self.reserve()
        self.assertEqual(self.client.data,before)
        self.assertIsNone(self.journal.get_plan_reservation('fixture_plan','a'*64))
        self.assertIsNone(self.journal.get('fixture_image'))

    def test_exact_replay_does_not_double_reserve_changed_plan_rejected(self):
        self.assertTrue(self.reserve());self.assertEqual(self.used(),45)
        self.assertFalse(self.reserve());self.assertEqual(self.used(),45)
        with self.assertRaises(ValueError):self.reserve({'fixture_image':21})
        with self.assertRaises(ValueError):self.journal.reserve_plan('fixture_plan','b'*64,{'fixture_image':20},80)

    def test_claim_moves_reserved_to_unknown_without_additional_charge(self):
        self.reserve()
        self.assertEqual(self.journal.get_reserved('fixture_image','fixture_plan','a'*64)['state'],'reserved')
        self.journal.claim_reserved('fixture_image',{'request':'image'},20,80,'fixture_plan','a'*64)
        self.assertEqual(self.used(),45)
        self.assertEqual(self.journal.get_reserved('fixture_image','fixture_plan','a'*64)['state'],'may_have_submitted')
        self.journal.record('fixture_image',{'request':'image'},{'request_id':'fixture_request'})
        self.assertEqual(self.journal.get_reserved('fixture_image','fixture_plan','a'*64)['state'],'received')
        with self.assertRaises(ValueError):self.journal.claim_reserved('fixture_image',{'request':'image'},20,80,'fixture_plan','a'*64)
        self.assertEqual(self.used(),45)
        self.assertEqual(self.journal.get_reserved('fixture_allowance','fixture_plan','a'*64)['state'],'reserved')

    def test_concurrent_claims_allow_exactly_one_caller(self):
        self.reserve()
        def claim(_):
            try:self.journal.claim_reserved('fixture_image',{'request':'image'},20,80,'fixture_plan','a'*64);return True
            except ValueError:return False
        with ThreadPoolExecutor(4) as pool:self.assertEqual(sum(pool.map(claim,range(4))),1)
        self.assertEqual(self.used(),45)

    def test_campaign_reservations_from_other_job_prevent_partial_start(self):
        other=self.make(job='e'*32);other.arm('fixture_other_work',{'stage':'other'},50,80)
        with self.assertRaisesRegex(ValueError,'Complete motion plan'):self.reserve()
        self.assertIsNone(self.journal.get_plan_reservation('fixture_plan','a'*64))

    def test_changed_scope_seed_and_cross_job_plan_claim_rejected(self):
        self.reserve()
        changed=self.make(prior=0)
        with self.assertRaises(ValueError):changed.reserve_plan('fixture_other_plan','b'*64,{'fixture_other':1},80)
        with self.assertRaises(ValueError):self.make(job='e'*32).claim_reserved('fixture_image',{'request':'image'},20,80,'fixture_plan','a'*64)

    def test_unknown_atomic_reservation_commit_is_recovered_without_extra_hold(self):
        self.client.unknown_commit=True
        with self.assertRaises(TimeoutError):self.reserve()
        self.assertEqual(self.used(),45)
        self.assertFalse(self.reserve());self.assertEqual(self.used(),45)

    def test_unknown_claim_commit_cannot_be_reclaimed(self):
        self.reserve();self.client.unknown_commit=True
        with self.assertRaises(TimeoutError):self.journal.claim_reserved('fixture_image',{'request':'image'},20,80,'fixture_plan','a'*64)
        self.assertEqual(self.journal.get_reserved('fixture_image','fixture_plan','a'*64)['state'],'may_have_submitted')
        with self.assertRaises(ValueError):self.journal.claim_reserved('fixture_image',{'request':'image'},20,80,'fixture_plan','a'*64)
        self.assertEqual(self.used(),45)


if __name__=='__main__':unittest.main(verbosity=2)
