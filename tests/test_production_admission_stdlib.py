"""Signed static configuration is not current dispatch permission; no services."""
import copy,unittest
from byron_video.production_admission import ProductionAdmission
from byron_video.elevenlabs_adapter import fingerprint
from test_worker_binding_stdlib import fixture as worker_fixture


class AdmissionChecks(unittest.TestCase):
    def setUp(self):
        import test_vertex_token_preflight_stdlib as count_tests
        self.fixture=count_tests.CountTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;self.job=f.job;self.clock=f.clock
        self.config=dict(target=worker_fixture()[0],budget={k:f.p['budget'][k] for k in ('scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents','operation_cap_cents')})
        f.p['config_sha256']=fingerprint(self.config);f.p['infrastructure']['target_sha256']=fingerprint(self.config['target'])
        self.kwargs=dict(config=self.config,processing_plan=self.job.snapshot['processing_plan'],policy_envelope=f.sign(f.p),policy_public_key=f.pub,billing_envelope=f.sign(f.c),billing_public_key=f.pub,now=lambda:self.clock[0])
    def admission(self,**changes):return ProductionAdmission(**{**self.kwargs,**changes})
    def test_current_generation_admission_then_expired_readonly_restart(self):
        admitted=self.admission();self.assertTrue(admitted.for_job(self.job));self.clock[0]=1200
        self.assertTrue(admitted.validate_configuration());restarted=self.admission();self.assertTrue(restarted.validate_configuration())
        for fn in (admitted.validate,lambda:admitted.for_job(self.job),lambda:restarted.for_job(self.job),lambda:restarted.policy):
            with self.assertRaises(ValueError):fn()
    def test_bad_signature_or_missing_billing_blocks_even_expired_startup(self):
        self.clock[0]=1200
        with self.assertRaises(ValueError):self.admission(billing_envelope=None)
        corrupted=copy.deepcopy(self.kwargs['policy_envelope']);corrupted['payload']['budget']['prior_spend_cents']=0
        with self.assertRaises(ValueError):self.admission(policy_envelope=corrupted)
        with self.assertRaises(ValueError):self.admission(billing_public_key=b'x'*32)
    def test_future_signed_document_never_becomes_static_authority(self):
        p=copy.deepcopy(self.fixture.p);p['issued_at']=1001
        with self.assertRaises(ValueError):self.admission(policy_envelope=self.fixture.sign(p))
        self.clock[0]=800
        with self.assertRaises(ValueError):self.admission()
    def test_no_historical_validity_overlap_is_rejected(self):
        c=copy.deepcopy(self.fixture.c);c['issued_at']=1100;c['expires_at']=1200
        p=copy.deepcopy(self.fixture.p);p['token_preflight']['billing_contract_sha256']=fingerprint(c);self.clock[0]=1300
        with self.assertRaises(ValueError):self.admission(policy_envelope=self.fixture.sign(p),billing_envelope=self.fixture.sign(c))
    def test_current_job_voice_and_budget_checked_before_dispatch(self):
        admission=self.admission();changed=copy.deepcopy(self.job);changed.snapshot['processing_plan']['narration_profile']['voice_id']='not_allowlisted_voice';changed.snapshot_sha256=fingerprint(changed.snapshot)
        with self.assertRaises(ValueError):admission.for_job(changed)
        admission.config['budget']['prior_spend_cents']=0
        with self.assertRaises(ValueError):admission.validate_configuration()
    def test_no_historical_clock_object_escapes(self):
        self.clock[0]=1200;admission=self.admission();self.assertIs(admission.validate_configuration(),True)
        with self.assertRaises(ValueError):admission.policy
        self.assertNotIn('policy',admission.__dict__)
    def test_current_render_only_scope_does_not_revive_expired_generation(self):
        from byron_video.exact_film_runtime import contract
        f=self.fixture;plan=copy.deepcopy(self.job.snapshot['processing_plan']);plan['exact_film_contract']=contract()
        self.job.snapshot['processing_plan']=plan;self.job.snapshot['runtime_config_sha256']=fingerprint(self.config);self.job.snapshot_sha256=fingerprint(self.job.snapshot)
        policy=copy.deepcopy(f.p);policy['processing_plan_sha256']=fingerprint(plan)
        infrastructure=dict(purpose='byron-exact-film-infrastructure-authorization-v1',reference='fixture_exact_infra',config_sha256=fingerprint(self.config),processing_plan_sha256=fingerprint(plan),owner_ids=[self.job.owner_id],infrastructure=policy['infrastructure'],issued_at=1150,expires_at=1300,paid_generation_allowed=False,publication_allowed=False)
        self.clock[0]=1200
        admission=self.admission(processing_plan=plan,policy_envelope=f.sign(policy),infrastructure_envelope=f.sign(infrastructure))
        self.assertTrue(admission.for_exact_film(self.job));self.assertTrue(admission.validate_configuration())
        with self.assertRaises(ValueError):admission.for_job(self.job)
        self.clock[0]=1300
        with self.assertRaises(ValueError):admission.for_exact_film(self.job)
        self.assertTrue(admission.validate_configuration())
    def test_missing_exactfilm_permission_does_not_block_retained_reads(self):
        self.clock[0]=1200;admission=self.admission();self.assertTrue(admission.validate_configuration())
        with self.assertRaises(ValueError):admission.for_exact_film(self.job)

if __name__=='__main__':unittest.main()
