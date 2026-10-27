"""Local fake-journal preview attempt isolation; no native QC is claimed here."""
import unittest
from byron_video.preview_worker import FirestorePreviewEvidence
from byron_video.hosted_control import Principal
import test_preview_registry_stdlib as registry_tests
from test_cloud_journal_stdlib import OWNER,OTHER

class PreviewAttemptChecks(unittest.TestCase):
    def setUp(self):
        fixture=registry_tests.PreviewRegistryChecks();fixture.setUp();self.fixture=fixture
        self.evidence=FirestorePreviewEvidence(fixture.db,**fixture.args)
    def test_attempt_requires_exact_owner_without_mutating_any_namespace(self):
        f=self.fixture;before=dict(f.db.data)
        with self.assertRaises(ValueError):self.evidence.reserve_attempt(OTHER,f.job,f.job.receipt.execution_id)
        self.assertEqual(f.db.data,before)
    def test_attempt_is_one_shot_and_separate_from_historical_final(self):
        f=self.fixture
        self.evidence.reserve_attempt(Principal(OWNER),f.job,f.job.receipt.execution_id)
        self.assertTrue(any('/preview_worker_attempts/' in key for key in f.db.data))
        self.assertFalse(any('/worker_attempts/' in key for key in f.db.data))
        with self.assertRaises(ValueError):self.evidence.reserve_attempt(OWNER,f.job,f.job.receipt.execution_id)
    def test_attempt_requires_exact_committed_execution(self):
        with self.assertRaises(ValueError):self.evidence.reserve_attempt(OWNER,self.fixture.job,'different_execution')

if __name__=='__main__':unittest.main()
