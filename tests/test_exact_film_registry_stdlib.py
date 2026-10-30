from candidate_media_fixture import synthetic_evidence
"""Exact copy registry boundary with synthetic approval/media proof, not native QC."""
from dataclasses import asdict,replace
from types import SimpleNamespace
import unittest
from byron_video.hosted_control import VerifiedArtifact,DispatchReceipt,digest,Principal
from byron_video.exact_film_registry import FirestoreExactFilmRegistry,RetainedPreviewViewVerifier
from byron_video.exact_film_worker import ExactFilmArtifact
import test_preview_registry_stdlib as preview_tests

class ExactFilmRegistryChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=preview_tests.PreviewRegistryChecks();self.fixture.setUp()
        self.job=self.fixture.commit();self.owner=Principal(self.job.owner_id)
        self.receipt=DispatchReceipt(self.job.id,self.job.snapshot_sha256,'exact_fixture_execution')
        self.approval=dict(approval_id='exact_approval_fixture',scope=dict(context_sha256='c'*64,retained_manifest_sha256='e'*64,final_release_sha256='f'*64))
        self.current=True;self.lease=False
        def read(owner,job,id,require_current=True):
            if id!=self.approval['approval_id'] or owner.subject!=self.job.owner_id or (require_current and not self.current):raise ValueError('No current authority')
            return self.approval
        def view(owner,job,id,sha,proofsha):
            return self.lease and id==self.proof.artifact_id and sha==self.proof.sha256 and proofsha==digest(asdict(self.proof))
        self.actions=SimpleNamespace(read_approval=read,authorize_view=view)
        self.registry=FirestoreExactFilmRegistry(self.fixture.db,**self.fixture.args,actions=self.actions,
            receipt_loader=lambda job,approval:self.receipt,evidence_verifier=lambda job,proof:True)
        base={k:v for k,v in asdict(self.fixture.proof).items() if k in VerifiedArtifact.__dataclass_fields__}
        base.update(artifact_id='clean_fixture_artifact',execution_id=self.receipt.execution_id)
        self.proof=ExactFilmArtifact(**base,candidate_media_evidence_json=synthetic_evidence(self.job,base["sha256"],base["duration_seconds"]),approval_id=self.approval['approval_id'],preview_artifact_id=self.job.artifact.artifact_id,approval_sha256=digest(self.approval),preview_artifact_sha256=self.job.artifact.sha256,
            preview_proof_sha256=digest(asdict(self.job.artifact)),context_sha256='c'*64,retained_manifest_sha256='e'*64,final_release_sha256='f'*64,equivalence_sha256='1'*64)
    def commit(self):return self.registry.for_approval(self.approval).commit_verified(self.owner,self.job,self.proof,self.fixture.reference)
    def test_distinct_receipt_and_registry_never_replace_original_draft(self):
        before=self.fixture.jobs.get(self.job.owner_id,self.job.id);result=self.commit()
        self.assertEqual(result.receipt.execution_id,self.receipt.execution_id)
        self.assertEqual(self.fixture.jobs.get(self.job.owner_id,self.job.id),before)
        self.assertEqual(self.commit(),result)
        self.assertTrue(any('/exact_film_artifacts/' in key for key in self.fixture.db.data))
        self.assertEqual(self.registry.load(self.owner,self.job)[0],self.proof)
        with self.assertRaises(ValueError):self.proof.validate(self.job)
    def test_expired_approval_needs_new_exact_view_lease_not_new_render_authority(self):
        self.commit();self.current=False
        self.assertEqual(self.registry.descriptor(self.owner,self.job)['artifact_id'],self.proof.artifact_id)
        with self.assertRaises(ValueError):self.registry.load(self.owner,self.job)
        self.lease=True;self.assertEqual(self.registry.load(self.owner,self.job)[0],self.proof)
        with self.assertRaises(ValueError):self.commit()
    def test_changed_media_approval_context_and_owner_fail_closed(self):
        for change in [dict(approval_sha256='0'*64),dict(context_sha256='0'*64),dict(retained_manifest_sha256='0'*64),dict(final_release_sha256='0'*64),dict(preview_artifact_sha256='0'*64)]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.registry.commit_verified(self.owner,self.job,replace(self.proof,**change),self.fixture.reference,approval_id=self.approval['approval_id'])
        self.commit()
        with self.assertRaises(ValueError):self.registry.load(Principal('other_fixture_owner'),self.job)
    def test_preview_view_renewal_still_requires_exact_native_evidence(self):
        enabled=False
        class Views:
            def authorize_view(inner,owner,job,id,sha,proofsha):return enabled and id==self.job.artifact.artifact_id and sha==self.job.artifact.sha256 and proofsha==digest(asdict(self.job.artifact))
        evidence=SimpleNamespace(verify=lambda job,proof:True)
        wrapper=RetainedPreviewViewVerifier(lambda *args:False,Views(),evidence)
        with self.assertRaises(ValueError):wrapper(self.job,self.job.artifact)
        enabled=True;self.assertTrue(wrapper(self.job,self.job.artifact))
        evidence.verify=lambda *args:False
        with self.assertRaises(ValueError):wrapper(self.job,self.job.artifact)
