"""Synthetic setup authority and offline HTTP regressions; no cloud/provider I/O."""
import base64
import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from byron_video.administration_runtime import PURPOSE, verify_administration, assemble_administration
from byron_video.hosted_control import canonical, digest
from byron_video.service_bootstrap import ServiceBindings
from byron_video.admin import AdminSession
from byron_video.preview_runtime import verify_runtime_authorization
from test_deployment_plan_stdlib import config
from test_hosted_uploads_stdlib import plan


def fixture():
    c = config(); c['firestore']['database'] = 'synthetic-setup'; p = plan()
    private = Ed25519PrivateKey.generate()
    key = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    now = int(time.time())
    payload = dict(schema_version=1, purpose=PURPOSE, config_sha256=digest(c), plan_sha256=digest(p),
        issued_at=now-1, expires_at=now+600, owner_only=True, generation_allowed=False, provider_calls_allowed=False)
    envelope = dict(payload=payload, signature=base64.b64encode(private.sign(canonical(payload).encode())).decode())
    return c, p, envelope, key


class AdministrationChecks(unittest.TestCase):
    def setUp(self):
        self.c, self.p, self.envelope, self.key = fixture(); self.writes = []
        def forbidden(*a, **kw): raise AssertionError('Unexpected network/secret read')
        self.bindings = ServiceBindings(SimpleNamespace(project='synthetic-project',_database_string='projects/synthetic-project/databases/synthetic-setup'),
            SimpleNamespace(), SimpleNamespace(access_secret_version=forbidden, add_secret_version=forbidden), forbidden, b'x'*32)
    def runtime(self):
        return assemble_administration(config=self.c, processing_plan=self.p, bindings=self.bindings,
            acceptance=self.envelope, reviewer_public_key=self.key)
    def test_signed_setup_is_not_production_authority_and_tampering_fails(self):
        verify_administration(self.envelope, self.key, digest(self.c), digest(self.p))
        with self.assertRaises(ValueError):
            verify_runtime_authorization(self.envelope, self.key, digest(self.c), digest(self.p))
        for field, value in [('generation_allowed',True), ('provider_calls_allowed',True), ('expires_at',0), ('config_sha256','a'*64)]:
            bad = copy.deepcopy(self.envelope); bad['payload'][field] = value
            with self.assertRaises(Exception): verify_administration(bad,self.key,digest(self.c),digest(self.p))
        with self.assertRaises(Exception): verify_administration(self.envelope,b'x'*32,digest(self.c),digest(self.p))
    def test_no_dispatcher_or_generation_routes_even_with_owner_session(self):
        runtime = self.runtime(); self.assertIsNone(runtime.dispatcher)
        with TestClient(runtime.app, base_url='https://studio.example') as client:
            self.assertEqual(client.get('/').status_code,200)
            self.assertEqual(client.get('/admin.html').status_code,200)
            self.assertFalse(client.get('/api/creator-capabilities').json()['enabled'])
            for path in ['/api/jobs','/api/uploads','/api/jobs/test/resume','/api/exact-films','/api/voices']:
                self.assertGreaterEqual(client.post(path,json={}).status_code,400)
            self.assertEqual(client.get('/api/admin/status').status_code,403)
    def test_admin_owner_csrf_origin_and_provider_isolation(self):
        runtime = self.runtime(); token = 'c'*43
        with TestClient(runtime.app, base_url='https://studio.example') as client, \
                patch.object(runtime.identity,'admin_session',return_value=AdminSession('owner',token)), \
                patch('byron_video.runtime_assembly._AdminSecrets.save_key') as save:
            self.assertEqual(client.get('/api/admin/status').status_code,200)
            path = '/api/admin/keys/fal'; body = {'key':'synthetic-key-123456'}
            self.assertEqual(client.post(path,json=body).status_code,403)
            headers = {'origin':'https://evil.example','x-csrf-token':token}
            self.assertEqual(client.post(path,json=body,headers=headers).status_code,403)
            save.assert_not_called()
            headers['origin']='https://studio.example'
            self.assertEqual(client.post(path,json=body,headers=headers).status_code,200)
            save.assert_called_once()
            self.assertEqual(client.post('/api/admin/test/elevenlabs',headers=headers).status_code,503)
            self.assertEqual(client.post('/api/admin/default-voice',json={'voice_id':'syntheticVoice'},headers=headers).status_code,503)
        with TestClient(runtime.app) as client, patch.object(runtime.identity,'admin_session',side_effect=ValueError):
            self.assertEqual(client.get('/api/admin/status').status_code,403)
    def test_login_uses_same_activation_gate_and_exact_owner_mapping(self):
        with patch('byron_video.google_login.GoogleLogin') as login:
            self.runtime()
            args = login.call_args.kwargs
            self.assertTrue(args['activation_check']())
            self.assertEqual(args['subject_emails'],self.c['identity']['subject_emails'])
            self.assertEqual(args['redirect_uri'],'https://studio.example/auth/callback')
        runtime = self.runtime()
        with TestClient(runtime.app) as client, patch('byron_video.administration_runtime.time.time',return_value=time.time()+1000):
            self.assertEqual(client.get('/api/admin/status').status_code,403)

    def test_explicit_bootstrap_without_provider_policy_and_default_still_denies(self):
        import hashlib
        from byron_video import service_bootstrap as bootstrap
        bundle = json.dumps(dict(schema_version=1, config=self.c, processing_plan=self.p, acceptance=self.envelope,
            reviewer_public_key_base64=base64.b64encode(self.key).decode()))
        args = ['--bootstrap-env','BYRON_TEST_SETUP','--reviewer-key-sha256',hashlib.sha256(self.key).hexdigest(),
            '--trusted-proxy','127.0.0.1','--serve']
        with patch.dict('os.environ',{'BYRON_TEST_SETUP':bundle}), \
                patch.object(bootstrap,'private_bytes',return_value=b'{}'), \
                patch.object(bootstrap,'validate_service_runtime',return_value={}), \
                patch('byron_video.service_bindings.build_service_bindings',return_value=self.bindings) as factory, \
                patch('uvicorn.run') as serve, patch('byron_video.production_admission.ProductionAdmission') as admission:
            self.assertEqual(bootstrap.main(args+['--administration-only']),0)
            self.assertTrue(factory.call_args.kwargs['administration_only']); serve.assert_called_once(); admission.assert_not_called()
            factory.reset_mock(); serve.reset_mock()
            with self.assertRaises(SystemExit): bootstrap.main(args)
            factory.assert_not_called(); serve.assert_not_called()
            with self.assertRaises(SystemExit): bootstrap.main(args+['--administration-only','--policy-env','BYRON_POLICY'])
            factory.assert_not_called()


if __name__ == '__main__': unittest.main()
