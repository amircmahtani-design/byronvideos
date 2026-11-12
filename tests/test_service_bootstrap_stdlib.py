"""No server, credentials or cloud calls are started by these bootstrap checks."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from byron_video import service_bootstrap as service
from test_deployment_plan_stdlib import config
from test_hosted_uploads_stdlib import plan


class ServiceBootstrapChecks(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def private(self,name,data):
        p=self.root/name;p.write_bytes(data);p.chmod(0o600);return p
    def test_default_validate_only_does_not_load_bindings_or_start_server(self):
        c=self.private('config.json',json.dumps(config()).encode());p=self.private('plan.json',json.dumps(plan()).encode());lock=self.private('lock.json',b'{}')
        with patch.object(service,'validate_service_runtime',return_value={}),patch.object(service,'load_bindings') as loader,patch('uvicorn.run') as run:
            self.assertEqual(service.main(['--config',str(c),'--processing-plan',str(p),'--sdk-lock',str(lock)]),0)
            loader.assert_not_called();run.assert_not_called()
    def test_missing_pinned_runtime_fails_before_private_code_import(self):
        c=self.private('config.json',json.dumps(config()).encode());p=self.private('plan.json',json.dumps(plan()).encode());lock=self.private('lock.json',b'{}')
        with patch.object(service,'load_bindings') as loader,self.assertRaises(SystemExit):
            service.main(['--config',str(c),'--processing-plan',str(p),'--sdk-lock',str(lock),'--serve'])
        loader.assert_not_called()
    def test_binding_hash_checked_before_execution(self):
        p=self.private('bindings.py',b'raise AssertionError("must not execute")\n')
        with self.assertRaises(ValueError):service.load_bindings(p,'0'*64,config())
    def test_hash_verified_bindings_must_return_explicit_type(self):
        p=self.private('bindings.py',b'def build_service_bindings(config): return {}\n')
        with self.assertRaises(ValueError):service.load_bindings(p,hashlib.sha256(p.read_bytes()).hexdigest(),config())
    def test_private_file_permission_and_symlink_fail(self):
        p=self.private('bindings.py',b'def build_service_bindings(config): return {}\n');p.chmod(0o644)
        with self.assertRaises(ValueError):service.load_bindings(p,hashlib.sha256(p.read_bytes()).hexdigest(),config())
        p.chmod(0o600);link=self.root/'link.py';link.symlink_to(p)
        with self.assertRaises(ValueError):service.load_bindings(link,hashlib.sha256(p.read_bytes()).hexdigest(),config())
    def test_explicit_trusted_proxy_set_never_wildcard(self):
        for invalid in ([],['*'],['0.0.0.0/0'],['::/0'],['proxy.example']):
            with self.assertRaises(ValueError):service.proxy_allowlist(invalid)
        self.assertEqual(service.proxy_allowlist(['127.0.0.1','10.0.0.0/24']),'127.0.0.1/32,10.0.0.0/24')
    def test_service_lock_must_include_acceptance_and_secret_runtime(self):
        with patch.object(service,'validate_runtime',return_value={'packages':{}}),self.assertRaises(ValueError):
            service.validate_service_runtime(config(),b'{}')

    def test_packaged_serve_missing_policy_stops_before_credentials_or_server(self):
        c=self.private('config.json',json.dumps(config()).encode());p=self.private('plan.json',json.dumps(plan()).encode())
        lock=self.private('lock.json',b'{}');acceptance=self.private('acceptance.json',b'{}');key=self.private('key.bin',b'k'*32)
        with patch.object(service,'validate_service_runtime',return_value={}),patch.object(service,'verify_runtime_authorization'),patch('byron_video.service_bindings.build_service_bindings') as factory,patch('uvicorn.run') as run,self.assertRaises(SystemExit):
            service.main(['--config',str(c),'--processing-plan',str(p),'--sdk-lock',str(lock),'--acceptance',str(acceptance),'--reviewer-public-key',str(key),'--trusted-proxy','127.0.0.1','--serve'])
        factory.assert_not_called();run.assert_not_called()

    def test_service_container_is_digest_required_nonroot_and_no_install(self):
        source=(Path(__file__).resolve().parent.parent/'Dockerfile.service').read_text()
        self.assertIn('ARG WORKER_BASE_IMAGE',source);self.assertIn('USER 65532:65532',source)
        self.assertIn('byron_video.service_bootstrap',source);self.assertNotIn('pip install',source)


if __name__=='__main__':unittest.main()
