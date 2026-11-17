"""Fake exact count HTTP + synthetic independent billing evidence, never live."""
import base64,copy,hashlib,tempfile,unittest
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from byron_video.vertex_token_preflight import *
from byron_video.declarative_policy import ApprovedProductionPolicy
from byron_video.provider_execution import FileResponseStore
from byron_video.cloud_journal import FirestoreSpendJournal
from byron_video import vertex_review,source_planning
from test_cloud_journal_stdlib import Client,PROJECT
from test_declarative_policy_stdlib import fixture
from test_source_planning_stdlib import fixture as source_fixture

class CountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.key=Ed25519PrivateKey.generate();self.pub=self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        self.p,self.job=fixture();self.job.id='a'*32;self.clock=[1000];self.calls=[];self.raw=encoded(dict(totalTokens=1000,totalBillableCharacters=100,promptTokensDetails=[dict(modality='TEXT',tokenCount=100),dict(modality='IMAGE',tokenCount=900)]));self.fail=False;self.active=True
        original,images,report=source_fixture();self.job.snapshot['images']=original.snapshot['images'];self.job.snapshot['input_order']=original.snapshot['input_order'];self.job.snapshot['narrated_text']=original.snapshot['narrated_text'];self.job.snapshot['narrated_text_sha256']=original.snapshot['narrated_text_sha256'];self.job.snapshot_sha256=fingerprint(self.job.snapshot)
        report['profile_sha256']=fingerprint(self.p['narration_profile']);self.prepared=source_planning.prepare_source_request(job=self.job,originals=images,narration_report=report,config=self.p['source_config']);self.native=self.prepared['native']
        controls=[]
        for n in (2048,8192):
            g=copy.deepcopy(self.native['body']['generationConfig']);g.pop('responseSchema');g['maxOutputTokens']=n;controls.append(dict(generation_controls=g,max_billable_output_tokens=n))
        self.c=dict(purpose='byron-vertex-billable-output-contract-v1',reference='fixture_billing_contract',policy_reference=self.p['reference'],endpoint=self.native['url'],model=MODEL,billed_controls=controls,
            compiler_sha256s={name:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for name,module in (('vertex_review',vertex_review),('source_planning',source_planning))},includes_thought_tokens=True,token_limit_contract_reference=self.p['source_config']['token_limit_contract_reference'],evidence_sha256='b'*64,evidence_url='https://example.invalid/synthetic-billing-evidence',issued_at=900,expires_at=1100)
        self.p['token_preflight']['billing_contract_sha256']=fingerprint(self.c);self.p['token_preflight']['max_cost_cents']=1
        self.db=Client();self.common=dict(project_id=PROJECT,namespace='count_fixture',transaction_runner=self.db.run)
        self.records=FirestoreCountRecords(self.db,**self.common,owner_id=self.job.owner_id,job_id=self.job.id)
        b=self.p['budget'];self.journal=FirestoreSpendJournal(self.db,**self.common,owner_id=self.job.owner_id,job_id=self.job.id,budget_scope=b['scope'],approved_total_cap_cents=b['approved_total_cap_cents'],prior_spend_cents=b['prior_spend_cents'])
        self.archive=FileResponseStore(Path(self.tmp.name)/'archive');self.refresh()
    def sign(self,p):return dict(payload=copy.deepcopy(p),signature=base64.b64encode(self.key.sign(canonical(p).encode())).decode())
    def refresh(self):
        self.policy=ApprovedProductionPolicy(self.sign(self.p),public_key=self.pub,config_sha256=self.p['config_sha256'],processing_plan_sha256=self.p['processing_plan_sha256'],now=lambda:self.clock[0])
        outer=self
        class Response:
            status_code=200;headers={}
            def __enter__(s):return s
            def __exit__(s,*a):pass
            def iter_bytes(s):
                if outer.fail:raise TimeoutError()
                yield outer.raw
        class HTTP:
            def __init__(s,**kwargs):outer.http_options=kwargs
            def __enter__(s):return s
            def __exit__(s,*a):pass
            def close(s):pass
            def stream(s,*args,**kwargs):outer.calls.append((args,kwargs));return Response()
        self.transport=VertexCountHTTP(project=PROJECT,token_reader=lambda:WorkloadToken('fixture_access_token_long',self.clock[0]+100),activation_check=lambda:self.active,enabled=True,client_factory=HTTP,now=lambda:self.clock[0])
        self.compiler=CountedNativeProofs(policy=self.policy,billing_contract=self.sign(self.c),public_key=self.pub,records=self.records,archive=self.archive,journal=self.journal,transport=self.transport,enabled=True,now=lambda:self.clock[0])
    def load(self):return self.compiler.load(self.job,self.native,max_input_tokens=8192,max_output_tokens=2048,image_count=4,token_limit_contract_reference=self.p['source_config']['token_limit_contract_reference'])
    def test_exact_full_body_counted_then_cached_no_second_call_or_debit(self):
        proof=self.load();self.assertEqual(proof['max_input_tokens'],1100);self.assertEqual(proof,self.load());self.assertEqual(len(self.calls),1)
        args,kwargs=self.calls[0];self.assertTrue(args[1].endswith(':countTokens'));self.assertEqual(kwargs['content'],encoded(self.native['body']));self.assertFalse(self.http_options['follow_redirects']);self.assertFalse(self.http_options['trust_env'])
        self.assertEqual(self.journal.get('count_slot_'+fingerprint(dict(owner=self.job.owner_id,job=self.job.id,slot=0)))['reservation_cents'],1)
    def test_unknown_retains_claim_no_retry(self):
        self.fail=True
        with self.assertRaises(ProviderOutcomeUnknown):self.load()
        self.fail=False
        with self.assertRaises(ProviderOutcomeUnknown):self.load()
        self.assertEqual(len(self.calls),1)
    def test_missing_or_expired_contract_blocks_before_count_or_spend(self):
        self.compiler.contract['payload']['includes_thought_tokens']=False
        with self.assertRaises(ValueError):self.load()
        self.assertFalse(self.calls);self.assertIsNone(self.records.get(fingerprint(self.native)))
    def test_disabled_and_actual_input_over_ceiling_fail(self):
        self.compiler.enabled=False
        with self.assertRaises(ValueError):self.load()
        self.compiler.enabled=True;self.raw=encoded(dict(totalTokens=8192))
        with self.assertRaises(ValueError):self.load()
    def test_native_schema_or_endpoint_change_denied_before_disclosure(self):
        self.native['url']='https://example.invalid/count'
        with self.assertRaises(ValueError):self.load()
        self.assertFalse(self.calls)
    def test_unknown_price_and_permission_deny_policy(self):
        for field,value in [('max_cost_cents',None),('disclosure_authorized',False),('price_evidence_sha256','')]:
            p=copy.deepcopy(self.p);p['token_preflight'][field]=value
            with self.assertRaises(ValueError):ApprovedProductionPolicy(self.sign(p),public_key=self.pub,config_sha256=p['config_sha256'],processing_plan_sha256=p['processing_plan_sha256'],now=lambda:1000)
    def test_parser_rejects_missing_or_inconsistent_counts(self):
        for value in ({},{'totalTokens':True},{'totalTokens':1,'promptTokensDetails':[{'modality':'AUDIO','tokenCount':1}]},{'totalTokens':1,'unexpected':1}):
            with self.assertRaises(ValueError):parse_count(encoded(value))
    def test_expiry_after_token_refresh_blocks_post(self):
        def token():self.clock[0]=1100;return WorkloadToken('fixture_access_token_long',1200)
        self.transport.token_reader=token
        with self.assertRaises(ProviderOutcomeUnknown):self.load()
        self.assertFalse(self.calls)
    def test_source_quote_uses_actual_count_and_exact_request_binding(self):
        value=self.policy.source_quote_loader(self.compiler)(self.job,self.prepared)
        self.assertEqual(value['input_proof']['max_input_tokens'],1100)
        self.assertEqual(value['quote']['max_output_tokens'],2048)
        self.assertEqual(value['quote']['reservation_cents'],1)
    def test_dynamic_review_schema_counted_under_fixed_compiler_contract(self):
        from test_automated_review_stdlib import request_fixture
        from byron_video import automated_review
        old=request_fixture();self.job.snapshot['images'][0]['sha256']=old['body']['binding']['source_sha256'];self.job.snapshot_sha256=fingerprint(self.job.snapshot);request=automated_review.prepare_request(owner_id=self.job.owner_id,job_id=self.job.id,image_id=self.job.snapshot['images'][0]['id'],sequence=0,prompt=old['body']['approved_prompt_as_untrusted_data'],evidence=old['body']['evidence'],config=vertex_review.config(PROJECT))
        native=vertex_review.build_native_request(request,project=PROJECT)
        proof=self.policy.review_input_proof_loader(self.compiler)(self.job,request,native)
        self.assertEqual(proof['image_count'],10);self.assertEqual(proof['max_output_tokens'],8192);self.assertEqual(len(self.calls),1)
    def test_complete_count_allowance_reserved_once_in_shared_budget(self):
        first=fund_count_budget(self.policy,self.job,self.journal);second=fund_count_budget(self.policy,self.job,self.journal)
        self.assertEqual(first,second);self.assertEqual(first['total_cents'],56)
        shared=self.db.data[self.journal.shared_ref.path]
        self.assertEqual(shared['reserved_cents'],160)
        self.load();self.assertEqual(self.db.data[self.journal.shared_ref.path]['reserved_cents'],160)
    def test_cross_job_native_binding_fails_before_count_or_reservation(self):
        data=json.loads(self.native['body']['contents'][0]['parts'][0]['text']);data['binding']['job_id']='b'*32
        self.native['body']['contents'][0]['parts'][0]['text']=canonical(data)
        with self.assertRaises(ValueError):self.load()
        self.assertFalse(self.calls);self.assertIsNone(self.records.get(fingerprint(self.native)))
    def test_changed_original_source_hash_fails_before_disclosure(self):
        data=json.loads(self.native['body']['contents'][0]['parts'][0]['text']);data['binding']['sources'][0]['source_sha256']='f'*64;data['binding_sha256']=fingerprint(data['binding'])
        self.native['body']['contents'][0]['parts'][0]['text']=canonical(data)
        with self.assertRaises(ValueError):self.load()
        self.assertFalse(self.calls)
    def test_equal_count_responses_have_distinct_scoped_receipts(self):
        first=self.load();data=json.loads(self.native['body']['contents'][0]['parts'][0]['text'])
        data['binding']['narration_report_sha256']='c'*64;data['binding_sha256']=fingerprint(data['binding'])
        self.native['body']['contents'][0]['parts'][0]['text']=canonical(data)
        second=self.load();self.assertNotEqual(first['native_request_sha256'],second['native_request_sha256'])
        self.assertEqual(len(self.calls),2);self.assertEqual(second,self.load());self.assertEqual(len(self.calls),2)
        ids=[v['receipt']['request_id'] for k,v in self.db.data.items() if '/requests/' in k and v.get('state')=='received']
        self.assertEqual(len(set(ids)),2)
    def test_finite_claim_counter(self):
        binding=dict(policy_sha256='a'*64)
        self.records.reserve('1'*64,binding,1)
        with self.assertRaises(ValueError):self.records.reserve('2'*64,binding,1)

if __name__=='__main__':unittest.main()
