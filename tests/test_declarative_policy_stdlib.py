"""Synthetic signed policy tests; no provider/disclosure/credential calls."""
import base64,copy,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from byron_video.declarative_policy import *
from byron_video.private_preview import policy
from byron_video.preview_composition import release_hash
from byron_video.source_planning import policy as source_policy
from byron_video.automated_review import candidate_policy
from byron_video.vertex_review import config
from byron_video.motion_continuation import IMAGE_ENDPOINT,EXTENSION_ENDPOINT
from test_provider_execution_stdlib import profile
from test_review_budget_stdlib import quote


def fixture():
    intent=dict(mode='PRIVATE_DRAFT_PREVIEW',preview_policy_sha256=fingerprint(policy()),source_policy_sha256=fingerprint(source_policy()),review_policy_sha256=fingerprint(candidate_policy()),preview_release_sha256=release_hash())
    plan=dict(narration_profile=profile(),preview_contract=intent)
    q=quote();q.update(config=config('synthetic-project'),max_output_tokens=8192)
    q['source_planning']=dict(image_count=4,max_input_tokens=8192,max_output_tokens=2048,text_tokens_ceiling=3000,execution_enabled=False,funded_operation_id=None)
    p=dict(purpose=PURPOSE,reference='fixture_policy_001',issued_at=900,expires_at=1100,owner_ids=['fixture_owner_001'],config_sha256='a'*64,processing_plan_sha256=fingerprint(plan),project='synthetic-project',
        private_preview_authorized=True,owner_only=True,public_release=False,human_style_accepted=False,preview_intent=intent,narration_profile=profile(),allowed_voice_profiles=[profile()],
        budget=dict(scope='fixture_campaign',approved_total_cap_cents=500,prior_spend_cents=104,job_cap_cents=200,operation_cap_cents=20,retry_allowance_cents=0,approval_reference='fixture_budget_001'),
        infrastructure=dict(purpose='byron-private-infrastructure-authorization-v1',reference='fixture_infrastructure',target_sha256='f'*64,cpu='2',memory='4Gi',task_timeout_seconds=1800,task_count=1,parallelism=1,max_retries=0,original_execution_limit=1,explicit_resume_limit=3,exact_film_execution_limit=1,authorization_mode='DOCUMENTED_BOUNDED_USE_CONSENT',bounded_use_approved=True,storage_scope='PRIVATE_JOB_INPUTS_INTERMEDIATES_PROOFS_OUTPUTS',storage_retention='UNTIL_EXPLICIT_OWNER_DELETION',evidence_sha256='e'*64,evidence_reference='synthetic_bounded_consent',per_execution_compute_allowance_cents=None,all_job_storage_allowance_cents=None,all_job_infrastructure_allowance_cents=None,independent_budget_scope=None),
        token_preflight=dict(disclosure_authorized=True,max_calls_per_job=56,max_cost_cents=0,all_in_usd_ceiling=True,price_evidence_sha256='c'*64,input_margin_tokens=100,count_bound_evidence_sha256='d'*64,billing_contract_sha256='e'*64),
        narration_usd_per_character='0.0001',all_in_usd_ceiling=True,
        image_quote=dict(kind='image',endpoint=IMAGE_ENDPOINT,duration_seconds=10,resolution='768P',max_cost_cents=2,valid_until_epoch=1100,reference='fixture_image_quote'),
        extension_quote=dict(kind='extend',endpoint=EXTENSION_ENDPOINT,duration_seconds=5,resolution='768P',max_cost_cents=3,valid_until_epoch=1100,reference='fixture_extend_quote'),review_quote=q,
        source_config=dict(project='synthetic-project',max_input_tokens=8192,max_output_tokens=2048,token_limit_contract_reference=q['token_limit_contract_reference']))
    snapshot=dict(processing_plan=plan,narrated_text='This exact text is preserved.',images=[dict(id='image_00'+str(i)) for i in range(4)])
    return p,SimpleNamespace(owner_id=p['owner_ids'][0],id='fixture_job_001',snapshot=snapshot,snapshot_sha256=fingerprint(snapshot))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.key=Ed25519PrivateKey.generate();self.public=self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw);self.p,self.job=fixture();self.clock=[1000]
    def sign(self,p):return dict(payload=copy.deepcopy(p),signature=base64.b64encode(self.key.sign(canonical(p).encode())).decode())
    def policy(self,p=None):return ApprovedProductionPolicy(self.sign(p or self.p),public_key=self.public,config_sha256='a'*64,processing_plan_sha256=self.p['processing_plan_sha256'],now=lambda:self.clock[0])
    def test_deterministic_exact_job_quotes_and_private_authorization(self):
        p=self.policy();a=p.narration_approval_loader(self.job);self.assertEqual(a,p.narration_approval_loader(self.job));self.assertEqual(a['approval'].reservations_cents,(1,))
        auth=p.preview_authorization_loader(self.job)['authorization'];self.assertEqual(auth['snapshot_sha256'],self.job.snapshot_sha256);self.assertFalse(auth['human_style_accepted']);self.assertFalse(auth['public_release'])
        self.assertEqual(p.preview_budget_loader(self.job)['prior_spend_cents'],104)
    def test_expiry_each_loader_no_rolling_renewal(self):
        p=self.policy();self.clock[0]=1100
        for fn in (p.narration_approval_loader,p.preview_budget_loader,p.preview_authorization_loader):
            with self.assertRaises(ValueError):fn(self.job)
    def test_wrong_key_tamper_config_and_plan_fail(self):
        envelope=self.sign(self.p);envelope['payload']['budget']['prior_spend_cents']=0
        with self.assertRaises(ValueError):ApprovedProductionPolicy(envelope,public_key=self.public,config_sha256='a'*64,processing_plan_sha256=self.p['processing_plan_sha256'],now=lambda:1000)
        with self.assertRaises(ValueError):ApprovedProductionPolicy(self.sign(self.p),public_key=b'x'*32,config_sha256='a'*64,processing_plan_sha256=self.p['processing_plan_sha256'],now=lambda:1000)
        with self.assertRaises(ValueError):ApprovedProductionPolicy(self.sign(self.p),public_key=self.public,config_sha256='b'*64,processing_plan_sha256=self.p['processing_plan_sha256'],now=lambda:1000)
    def test_scope_profile_or_frozen_job_changes_rejected(self):
        p=self.policy()
        for field,value in [('owner_id','wrong_owner_001'),('snapshot_sha256','b'*64)]:
            job=copy.deepcopy(self.job);setattr(job,field,value)
            with self.assertRaises(ValueError):p.preview_authorization_loader(job)
        self.job.snapshot['processing_plan']['narration_profile']['voice_id']='different_voice';self.job.snapshot_sha256=fingerprint(self.job.snapshot)
        with self.assertRaises(ValueError):p.narration_approval_loader(self.job)
    def test_no_implicit_prices_human_approval_or_five_frame_policy(self):
        changes=[('human_style_accepted',True),('private_preview_authorized',False),('narration_usd_per_character','NaN'),('all_in_usd_ceiling',False)]
        for key,value in changes:
            p=copy.deepcopy(self.p);p[key]=value
            with self.assertRaises(ValueError):self.policy(p)
        p=copy.deepcopy(self.p);p['review_quote']['sample_count']=5
        with self.assertRaises(ValueError):self.policy(p)
    def test_native_review_ceiling_and_planning_accounting_must_match(self):
        for branch,key,value in [('review_quote','max_output_tokens',2000),('source_config','max_output_tokens',4096)]:
            p=copy.deepcopy(self.p);p[branch][key]=value
            with self.assertRaises(ValueError):self.policy(p)
    def test_no_unsigned_native_proof_fallback(self):
        p=self.policy()
        with self.assertRaises(ValueError):p.source_quote_loader(lambda *args: {})
        with tempfile.TemporaryDirectory() as directory:
            proofs=SignedNativeProofs(Path(directory),public_key=self.public,policy_sha256=p.sha256,now=lambda:1000)
            with self.assertRaises(ValueError):proofs.load(self.job,{},max_input_tokens=100,max_output_tokens=100,image_count=4,token_limit_contract_reference='fixture_contract')
    def test_quote_full_segment_planning_declaration_preserved(self):
        p=self.policy();plan=dict(owner_id=self.job.owner_id,job_id=self.job.id,snapshot_sha256=self.job.snapshot_sha256)
        q=p.review_quote_loader(self.job,plan);self.assertEqual(q['source_planning']['image_count'],4);self.assertTrue(q['source_planning']['funded_operation_id'].startswith('source_planning_'))
        self.assertEqual(q['sample_count'],9);self.assertEqual(q['max_output_tokens'],8192)
    def test_allowlisted_picker_voice_binds_selected_snapshot_without_substitution(self):
        selected={**profile(),'voice_id':'fixture_voice_second'};self.p['allowed_voice_profiles'].append(selected)
        p=self.policy();self.job.snapshot['processing_plan']['narration_profile']=selected;self.job.snapshot_sha256=fingerprint(self.job.snapshot)
        approval=p.narration_approval_loader(self.job)['approval'];requests=plan_requests(self.job.snapshot['narrated_text'],selected)
        self.assertEqual(approval.plan_sha256,fingerprint(requests));self.assertIn('fixture_voice_second',requests[0]['url'])
    def test_infrastructure_missing_or_unapproved_cannot_be_free_by_default(self):
        for field,value in [('bounded_use_approved',False),('max_retries',1),('explicit_resume_limit',4),('storage_retention','AUTOMATIC_UNIMPLEMENTED_DELETE'),('per_execution_compute_allowance_cents',0)]:
            p=copy.deepcopy(self.p);p['infrastructure'][field]=value
            with self.assertRaises(ValueError):self.policy(p)
        p=copy.deepcopy(self.p);del p['infrastructure']
        with self.assertRaises(ValueError):self.policy(p)
    def test_infrastructure_target_independently_matches_config(self):
        from test_worker_binding_stdlib import fixture as worker_fixture
        config={'target':worker_fixture()[0]};p=copy.deepcopy(self.p);p['config_sha256']=fingerprint(config);p['infrastructure']['target_sha256']=fingerprint(config['target'])
        policy=ApprovedProductionPolicy(self.sign(p),public_key=self.public,config_sha256=fingerprint(config),processing_plan_sha256=p['processing_plan_sha256'],now=lambda:1000)
        self.assertFalse(policy.validate_infrastructure(config)['requires_separate_reservation'])
        config['target']['max_retries']=1
        with self.assertRaises(ValueError):policy.validate_infrastructure(config)
    def test_private_file_duplicate_and_symlink_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'policy.json';path.write_text('{"x":1,"x":2}')
            with self.assertRaises(ValueError):read_private_json(path)
            other=Path(directory)/'link';other.symlink_to(path)
            with self.assertRaises(ValueError):read_private_json(other)

if __name__=='__main__':unittest.main()
