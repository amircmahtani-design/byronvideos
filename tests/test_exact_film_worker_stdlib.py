"""Offline stable-job final-render claim tests; no approval/provider shortcuts."""
import unittest
from types import SimpleNamespace
from byron_video.exact_film_worker import FirestoreExactFilmEvidence
from byron_video.hosted_control import DispatchReceipt,Principal
import test_preview_registry_stdlib as fixture_module
from test_cloud_journal_stdlib import OWNER,OTHER

class ExactFilmAttemptChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture_module.PreviewRegistryChecks();self.fixture.setUp()
        self.evidence=FirestoreExactFilmEvidence(self.fixture.db,**self.fixture.args)
        self.receipt=DispatchReceipt(self.fixture.job.id,self.fixture.job.snapshot_sha256,'exact_execution')
    def test_renewed_approval_cannot_create_second_final_render(self):
        f=self.fixture;self.evidence.reserve_attempt(OWNER,f.job,'a'*64,self.receipt)
        with self.assertRaises(ValueError):self.evidence.reserve_attempt(OWNER,f.job,'b'*64,self.receipt)
        with self.assertRaises(ValueError):self.evidence.reserve_attempt(OWNER,f.job,'a'*64,self.receipt)
        self.assertEqual(len([k for k in f.db.data if '/exact_film_worker_attempts/' in k]),1)
    def test_other_owner_cannot_reserve_render(self):
        before=dict(self.fixture.db.data)
        with self.assertRaises(ValueError):self.evidence.reserve_attempt(OTHER,self.fixture.job,'a'*64,self.receipt)
        self.assertEqual(before,self.fixture.db.data)
    def test_wrong_snapshot_execution_receipt_denied(self):
        bad=DispatchReceipt(self.fixture.job.id,'f'*64,'exact_execution')
        with self.assertRaises(ValueError):self.evidence.reserve_attempt(OWNER,self.fixture.job,'a'*64,bad)
    def test_output_requires_original_attempt_and_exact_approval(self):
        proof=SimpleNamespace(approval_sha256='a'*64,execution_id=self.receipt.execution_id)
        with self.assertRaises(ValueError):self.evidence._verify_attempt(self.fixture.job,proof)
        self.evidence.reserve_attempt(OWNER,self.fixture.job,'a'*64,self.receipt)
        self.evidence._verify_attempt(self.fixture.job,proof)
        proof.approval_sha256='b'*64
        with self.assertRaises(ValueError):self.evidence._verify_attempt(self.fixture.job,proof)
    def test_final_attempt_never_consumes_preview_or_historical_attempt(self):
        self.evidence.reserve_attempt(Principal(OWNER),self.fixture.job,'a'*64,self.receipt)
        keys=self.fixture.db.data
        self.assertFalse(any('/preview_worker_attempts/' in key or '/worker_attempts/' in key for key in keys))

if __name__=='__main__':unittest.main()
