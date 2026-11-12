"""Synthetic signed ENV configuration only; no actual secrets or environment fetch."""
import base64,copy,hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from byron_video.bootstrap_bundle import load_bootstrap_env,parse_bootstrap_bundle,MAX_BUNDLE_BYTES
from byron_video import service_bootstrap as service
from test_service_bindings_stdlib import service_config
from test_hosted_uploads_stdlib import plan
from test_runtime_assembly_stdlib import signed

class BootstrapBundleChecks(unittest.TestCase):
    def setUp(self):
        self.config,self.plan=service_config(),plan();envelope,self.key,_=signed(self.config,self.plan)
        self.value=dict(schema_version=1,config=self.config,processing_plan=self.plan,acceptance=envelope,reviewer_public_key_base64=base64.b64encode(self.key).decode())
        self.raw=json.dumps(self.value);self.pin=hashlib.sha256(self.key).hexdigest()
    def test_explicit_environment_verified_against_separate_key_pin(self):
        value=load_bootstrap_env('BYRON_BOOTSTRAP',self.pin,environ={'BYRON_BOOTSTRAP':self.raw})
        self.assertEqual(value['config'],self.config);self.assertEqual(value['reviewer_public_key'],self.key)
    def test_no_ambient_name_missing_value_or_trust_anchor_fallback(self):
        for name,values,pin in [('PATH',{'PATH':self.raw},self.pin),('BYRON_BOOTSTRAP',{},self.pin),('BYRON_BOOTSTRAP',{'BYRON_BOOTSTRAP':self.raw},'0'*64)]:
            with self.subTest(name=name),self.assertRaises(ValueError):load_bootstrap_env(name,pin,environ=values)
    def test_duplicates_nonfinite_and_wrong_schema_are_rejected(self):
        for raw in [self.raw[:-1]+',"schema_version":1}',self.raw.replace('"schema_version": 1','"schema_version": NaN',1),json.dumps({**self.value,'api_key':'must-not-be-accepted'})]:
            with self.assertRaises(ValueError):parse_bootstrap_bundle(raw,self.pin)
        raw=self.raw.replace('"database": "byron-studio"','"database": "byron-studio", "database": "other"')
        with self.assertRaises(ValueError):parse_bootstrap_bundle(raw,self.pin)
    def test_config_secret_fields_must_remain_resource_names_not_payloads(self):
        value=copy.deepcopy(self.value);value['config']['secret_resources']['csrf']='literal-secret-not-allowed'
        with self.assertRaises(ValueError):parse_bootstrap_bundle(json.dumps(value),self.pin)
        value=copy.deepcopy(self.value);value['config']['csrf_secret']='literal-secret-not-allowed'
        with self.assertRaises(ValueError):parse_bootstrap_bundle(json.dumps(value),self.pin)
    def test_bound_is_utf8_bytes_and_tampered_signature_denies(self):
        with self.assertRaises(ValueError):parse_bootstrap_bundle('é'*(MAX_BUNDLE_BYTES//2+1),self.pin)
        value=copy.deepcopy(self.value);value['acceptance']['payload']['evidence']['identity_flow']='0'*64
        with self.assertRaises(ValueError):parse_bootstrap_bundle(json.dumps(value),self.pin)
    def test_cli_env_mode_validates_without_sdk_construction_and_forbids_mixed_files(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'lock';path.write_bytes(b'{}');path.chmod(0o600)
            args=['--bootstrap-env','BYRON_BOOTSTRAP','--reviewer-key-sha256',self.pin,'--sdk-lock',str(path)]
            with patch.dict('os.environ',{'BYRON_BOOTSTRAP':self.raw}),patch.object(service,'validate_service_runtime'),patch('byron_video.service_bindings.build_service_bindings') as factory:
                self.assertEqual(service.main(args),0);factory.assert_not_called()
                with self.assertRaises(SystemExit):service.main(args+['--config','/not/read'])
                factory.assert_not_called()
