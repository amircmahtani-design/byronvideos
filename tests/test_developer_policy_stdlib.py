"""Synthetic signed Developer choice; no account, key or provider calls."""
import copy,hashlib,unittest
from pathlib import Path
import test_declarative_policy_stdlib as legacy
from byron_video import source_planning,vertex_review
from byron_video.elevenlabs_adapter import fingerprint
from byron_video.vertex_token_preflight import validate_billing_contract

class DeveloperPolicyTests(unittest.TestCase):
    setUp=legacy.PolicyTests.setUp
    sign=legacy.PolicyTests.sign
    policy=legacy.PolicyTests.policy
    def developer(self):
        from byron_video import gemini_developer as native
        p=copy.deepcopy(self.p);p['native_api_mode']='gemini_developer'
        p['native_disclosure']=dict(paid_service=True,non_eu_exclusive_processing=True,abuse_retention_days=55,data_disclosure_authorized=True)
        p['source_config'].update(native_api_mode='gemini_developer',max_output_tokens=3072)
        p['review_quote'].update(config=native.config(p['project']),max_output_tokens=8192,input_usd_per_million='0.25',output_usd_per_million='1.50')
        p['review_quote']['source_planning']['max_output_tokens']=3072
        return p
    def test_selected_developer_requires_all_explicit_disclosures(self):
        p=self.developer();self.policy(p)
        for field in p['native_disclosure']:
            bad=copy.deepcopy(p);bad['native_disclosure'][field]=False
            with self.assertRaises(ValueError):self.policy(bad)
        for field in ('paid_service','non_eu_exclusive_processing','data_disclosure_authorized'):
            bad=copy.deepcopy(p);bad['native_disclosure'][field]=1
            with self.assertRaises(ValueError):self.policy(bad)
        for field in ('native_api_mode','native_disclosure'):
            bad=copy.deepcopy(p);del bad[field]
            with self.assertRaises(ValueError):self.policy(bad)
    def test_selected_route_caps_and_source_mode_not_interchangeable(self):
        for branch,key,value in [('source_config','native_api_mode','vertex_eu'),('source_config','max_output_tokens',8192),('review_quote','max_output_tokens',1024),('review_quote','config',vertex_review.config('synthetic-project'))]:
            p=self.developer();p[branch][key]=value
            with self.assertRaises(ValueError):self.policy(p)
    def test_developer_contract_is_separate_and_compiler_bound(self):
        from byron_video import gemini_developer as native
        p=self.developer();controls=[]
        for output in (3072,8192):
            g=native.build_vertex_request(project=p['project'],system_instruction='bound',parts=[{'text':'bound'}],response_json_schema={},max_output_tokens=output)['body']['generationConfig'];del g['responseSchema']
            controls.append(dict(generation_controls=g,max_billable_output_tokens=output))
        c=dict(purpose='byron-gemini-developer-billable-output-contract-v1',reference='fixture_developer_billing',policy_reference=p['reference'],endpoint=native.endpoint(p['project']),model=native.MODEL,billed_controls=controls,
            compiler_sha256s={name:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for name,module in [('gemini_developer',native),('source_planning',source_planning)]},includes_thought_tokens=True,token_limit_contract_reference=p['source_config']['token_limit_contract_reference'],evidence_sha256='b'*64,evidence_url='https://ai.google.dev/gemini-api/docs/generate-content/thinking#token-limits-and-max_output_tokens',issued_at=900,expires_at=1100)
        p['token_preflight']['billing_contract_sha256']=fingerprint(c)
        self.assertEqual(validate_billing_contract(self.policy(p),self.sign(c),self.public,now=lambda:1000),c)
        for field,value in [('purpose','byron-vertex-billable-output-contract-v1'),('endpoint',vertex_review.endpoint(p['project'])),('includes_thought_tokens',False)]:
            bad=copy.deepcopy(c);bad[field]=value;p['token_preflight']['billing_contract_sha256']=fingerprint(bad)
            with self.assertRaises(ValueError):validate_billing_contract(self.policy(p),self.sign(bad),self.public,now=lambda:1000)

if __name__=='__main__':unittest.main()
