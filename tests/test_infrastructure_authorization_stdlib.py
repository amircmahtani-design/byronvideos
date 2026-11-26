"""Separate current render-only consent; no SDK/network/provider access."""
import base64,copy,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from byron_video.hosted_control import canonical,digest
from byron_video.infrastructure_authorization import PURPOSE,validate_exact_infrastructure
from byron_video.production_worker import build_worker_bindings,ExactFilmActivation
from test_declarative_policy_stdlib import fixture
from test_service_bindings_stdlib import service_config
from test_hosted_uploads_stdlib import plan
from test_private_preview_flow_stdlib import signed_preview
from byron_video.exact_film_runtime import contract

class InfrastructureChecks(unittest.TestCase):
    def setUp(self):
        self.root=tempfile.TemporaryDirectory();self.addCleanup(self.root.cleanup)
        self.key=Ed25519PrivateKey.generate();self.public=self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        self.cfg=service_config();p,_=fixture();self.plan=plan();self.plan.update(preview_contract=p['preview_intent'],exact_film_contract=contract())
        self.acceptance,self.reviewer,_=signed_preview(self.cfg,self.plan);self.clock=[int(time.time())]
        scope=copy.deepcopy(p['infrastructure']);scope.update(target_sha256=digest(self.cfg['target']),**{k:self.cfg['target'][k] for k in ('cpu','memory','task_timeout_seconds','task_count','parallelism','max_retries')})
        self.payload=dict(purpose=PURPOSE,reference='fixture_exact_infrastructure',config_sha256=digest(self.cfg),processing_plan_sha256=digest(self.plan),owner_ids=['fixture_owner'],infrastructure=scope,issued_at=self.clock[0]-10,expires_at=self.clock[0]+300,paid_generation_allowed=False,publication_allowed=False)
        snapshot=dict(runtime_config_sha256=digest(self.cfg),processing_plan=self.plan)
        self.job=SimpleNamespace(owner_id='fixture_owner',snapshot=snapshot,snapshot_sha256=digest(snapshot))
    def sign(self,p):return dict(payload=copy.deepcopy(p),signature=base64.b64encode(self.key.sign(canonical(p).encode())).decode())
    def validate(self,p=None,job=None):return validate_exact_infrastructure(self.sign(p or self.payload),self.public,self.cfg,self.plan,job,now=lambda:self.clock[0])
    def test_current_exact_compute_is_not_paid_generation_permission(self):
        self.assertFalse(self.validate(job=self.job)['paid_generation_allowed'])
        self.assertTrue(ExactFilmActivation(self.cfg,self.plan,self.acceptance,self.reviewer,self.sign(self.payload),self.public,now=lambda:self.clock[0]).for_job(self.job))
    def test_expired_wrong_owner_config_or_rights_fail(self):
        for field,value in [('expires_at',self.clock[0]),('paid_generation_allowed',True),('publication_allowed',True),('config_sha256','a'*64)]:
            p=copy.deepcopy(self.payload);p[field]=value
            with self.assertRaises(ValueError):self.validate(p)
        self.job.owner_id='another_owner'
        with self.assertRaises(ValueError):self.validate(job=self.job)
    def test_unsigned_missing_or_expanded_attempt_scope_fails(self):
        with self.assertRaises(ValueError):validate_exact_infrastructure({},self.public,self.cfg,self.plan)
        p=copy.deepcopy(self.payload);p['infrastructure']['exact_film_execution_limit']=2
        with self.assertRaises(ValueError):self.validate(p)
    def build(self,protocol):
        calls=[]
        credentials=SimpleNamespace(verify_identity=lambda:calls.append('identity') or True,credentials=object(),token_reader=lambda:'not_used')
        def storage(**kwargs):calls.append('storage');return SimpleNamespace(project=self.cfg['target']['project_id'])
        def firestore(**kwargs):calls.append('firestore');return SimpleNamespace(project=self.cfg['target']['project_id'],_database_string='projects/'+self.cfg['target']['project_id']+'/databases/'+self.cfg['firestore']['database'])
        def secrets(**kwargs):raise AssertionError('render-only must not construct secrets/provider services')
        with patch('byron_video.production_worker.validate_runtime'),patch('byron_video.declarative_policy.ApprovedProductionPolicy.__init__',side_effect=AssertionError('expired generation policy must not be constructed')):
            result=build_worker_bindings(config=self.cfg,processing_plan=self.plan,sdk_lock_bytes=b'explicit SDK boundary fixture',acceptance=self.acceptance,reviewer_public_key=self.reviewer,
                policy_envelope={'expired':'not consulted'},policy_public_key=self.public,billing_envelope={'expired':'not consulted'},billing_public_key=self.public,
                infrastructure_envelope=self.sign(self.payload),workspace=Path(self.root.name),enabled=True,protocol=protocol,credential_bundle=credentials,
                sdk_factories=dict(storage=storage,firestore=firestore,secrets=secrets),now=lambda:self.clock[0])
        return result,calls
    def test_exact_builder_constructs_no_paid_policy_billing_secrets_or_generation(self):
        bindings,calls=self.build('exact-film-v1');self.assertIsNone(bindings.generation_factory)
        self.assertEqual(calls,['identity','storage','firestore']);self.assertTrue(bindings.activation_check());self.assertTrue(bindings.exact_film_preflight(self.job))
        with self.assertRaises(ValueError):bindings.semantic_verifier(None,None)
    def test_original_builder_cannot_use_render_only_consent_to_skip_policy(self):
        with self.assertRaises(AssertionError):self.build('originals-v1')
    def test_bound_dispatch_preflight_cannot_be_removed(self):
        from test_exact_film_dispatch_stdlib import ExactDispatchChecks
        f=ExactDispatchChecks();f.setUp();self.addCleanup(f.tearDown)
        f.dispatch.bind_job_preflight(lambda job:True);f.dispatch.job_preflight=None
        with self.assertRaises(ValueError):f.dispatch(f.job,f.approval)
        self.assertEqual(f.calls,[])

if __name__=='__main__':unittest.main()
