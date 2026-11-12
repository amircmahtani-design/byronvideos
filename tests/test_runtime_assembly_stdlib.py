"""Offline assembly/signature tests. Generated signing keys/evidence are SYNTHETIC.
No real acceptance is signed, no cloud client is created and no network occurs.
"""
import base64
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from byron_video.runtime_assembly import assemble_runtime, verify_acceptance, digest, EVIDENCE
from byron_video.hosted_control import canonical, create_app
from byron_video.original_input import OriginalInputRequestLoader
from test_deployment_plan_stdlib import config
from test_hosted_uploads_stdlib import plan


def signed(config_value, plan_value, *, now=None):
    now = int(time.time()) if now is None else now
    private = Ed25519PrivateKey.generate()  # Ephemeral synthetic fixture; never persisted.
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    payload = dict(schema_version=1,purpose='byron-runtime-acceptance-v1',status='ACCEPTED',
        config_sha256=digest(config_value),plan_sha256=digest(plan_value),issued_at=now-10,expires_at=now+600,
        evidence={name:(OriginalInputRequestLoader(digest(config_value)).release_sha256 if name=='handoff_adapter' else 'e'*64) for name in EVIDENCE})
    envelope = dict(payload=payload,signature=base64.b64encode(private.sign(canonical(payload).encode())).decode())
    return envelope,public,private


class NoIOFirestore:
    project = 'synthetic-project'
    _database_string = 'projects/synthetic-project/databases/(default)'
    def document(self,path):
        raise AssertionError('No database request expected')


class RuntimeAssemblyChecks(unittest.TestCase):
    def arguments(self, **changes):
        def forbidden(*args, **kwargs):
            raise AssertionError('No external operation expected')
        return dict(dict(config=config(), processing_plan=plan(), firestore_client=NoIOFirestore(),
            storage_client=SimpleNamespace(bucket=lambda name:SimpleNamespace(name=name)),
            secret_client=SimpleNamespace(access_secret_version=forbidden,add_secret_version=forbidden),
            workload_token_reader=forbidden,csrf_secret=b'synthetic-private-secret-at-least-32-bytes'),**changes)

    def test_disabled_assembly_does_not_call_any_external_dependency(self):
        runtime=assemble_runtime(**self.arguments())
        self.assertFalse(runtime.enabled);self.assertFalse(runtime.dispatcher.enabled)
        self.assertIsNone(runtime.release_sha256)
        with TestClient(runtime.app,base_url='https://studio.example') as client:
            self.assertEqual(client.get('/').status_code,200)
            self.assertEqual(client.get('/admin.html').status_code,200)
            self.assertEqual(client.get('/hosted.mjs').status_code,200)
            self.assertEqual(client.get('/deployment.private.template.json').status_code,404)
            self.assertFalse(client.get('/api/creator-capabilities').json()['enabled'])
            self.assertEqual(client.post('/api/uploads').status_code,503)
            self.assertEqual(client.post('/api/jobs',json={}).status_code,422)
            self.assertEqual(client.get('/api/admin/status').status_code,503)
            self.assertGreaterEqual(client.get('/auth/login').status_code,400)

    def test_disabled_preview_assembles_concrete_read_verifier_without_authorization_or_io(self):
        from byron_video.private_preview import policy,FirestorePreviewAuthorizations
        from byron_video.source_planning import policy as source_policy
        from byron_video.automated_review import candidate_policy
        from byron_video.preview_composition import release_hash
        from byron_video.preview_registry import PreviewAuthorizationVerifier
        from byron_video.preview_worker import FirestorePreviewEvidence
        processing=plan()
        processing['preview_contract']=dict(mode='PRIVATE_DRAFT_PREVIEW',preview_policy_sha256=digest(policy()),
            preview_release_sha256=release_hash(),source_policy_sha256=digest(source_policy()),review_policy_sha256=digest(candidate_policy()))
        runtime=assemble_runtime(**self.arguments(processing_plan=processing))
        self.assertFalse(runtime.enabled)
        verifier=runtime.previews.authorization_verifier
        self.assertIs(type(verifier),PreviewAuthorizationVerifier)
        self.assertIs(type(verifier.authorizations),FirestorePreviewAuthorizations)
        self.assertIs(type(verifier.evidence),FirestorePreviewEvidence)
        with TestClient(runtime.app) as client:
            self.assertFalse(client.get('/api/creator-capabilities').json()['enabled'])

    def test_enabled_requires_signed_exact_config_and_plan(self):
        args=self.arguments()
        for acceptance,key in [(None,None),({},b'x'*32)]:
            with self.assertRaises(ValueError):assemble_runtime(**args,enabled=True,acceptance=acceptance,reviewer_public_key=key)
        envelope,public,_=signed(args['config'],args['processing_plan'])
        runtime=assemble_runtime(**args,enabled=True,acceptance=envelope,reviewer_public_key=public)
        self.assertTrue(runtime.enabled);self.assertFalse(hasattr(runtime.dispatcher,'synthetic_fixture'))
        # No session: even an accepted runtime cannot expose resources to callers.
        with TestClient(runtime.app,base_url='https://studio.example') as client:
            self.assertEqual(client.get('/api/creator-capabilities').status_code,401)
            self.assertEqual(client.get('/api/voices').status_code,401)
            self.assertEqual(client.get('/api/admin/status').status_code,403)

    def test_tampering_expiry_and_wrong_signer_fail_closed(self):
        cfg,processing=config(),plan();envelope,public,_=signed(cfg,processing,now=1000)
        self.assertEqual(verify_acceptance(envelope,public,digest(cfg),digest(processing),now=1000)['status'],'ACCEPTED')
        wrong=Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        for key,config_sha,plan_sha,now in [(wrong,digest(cfg),digest(processing),1000),
                (public,'a'*64,digest(processing),1000),(public,digest(cfg),'b'*64,1000),
                (public,digest(cfg),digest(processing),1600),(public,digest(cfg),digest(processing),900)]:
            with self.subTest(now=now),self.assertRaises(ValueError):verify_acceptance(envelope,key,config_sha,plan_sha,now=now)
        altered=copy.deepcopy(envelope);altered['payload']['evidence']['identity_flow']='f'*64
        with self.assertRaises(ValueError):verify_acceptance(altered,public,digest(cfg),digest(processing),now=1000)

    def test_expired_bound_runtime_blocks_capability_and_login_before_io(self):
        args=self.arguments();envelope,public,_=signed(args['config'],args['processing_plan'])
        runtime=assemble_runtime(**args,enabled=True,acceptance=envelope,reviewer_public_key=public)
        binding=runtime.dispatcher.activation_check.__self__
        binding._clock=lambda:envelope['payload']['expires_at']
        with TestClient(runtime.app,base_url='https://studio.example') as client:
            self.assertEqual(client.get('/api/creator-capabilities').status_code,503)
            self.assertGreaterEqual(client.get('/auth/login').status_code,400)
        with self.assertRaises(ValueError):runtime.dispatcher.activation_check()

    def test_handoff_implementation_must_match_evidence(self):
        args=self.arguments();envelope,public,private=signed(args['config'],args['processing_plan'])
        envelope['payload']['evidence']['handoff_adapter']='f'*64
        envelope['signature']=base64.b64encode(private.sign(canonical(envelope['payload']).encode())).decode()
        with self.assertRaisesRegex(ValueError,'handoff'):assemble_runtime(**args,enabled=True,acceptance=envelope,reviewer_public_key=public)

    def test_wrong_database_and_profile_fail_before_any_external_call(self):
        args=self.arguments();args['firestore_client']._database_string='projects/synthetic-project/databases/other'
        with self.assertRaisesRegex(ValueError,'database'):assemble_runtime(**args)
        args=self.arguments();args['processing_plan']['narration_profile']['voice_id']='OTHER_SYNTHETIC_VOICE'
        with self.assertRaisesRegex(ValueError,'narration'):assemble_runtime(**args)

    def test_browser_boolean_cannot_replace_runtime_binding(self):
        with self.assertRaises(ValueError):create_app(runtime_binding=True,hosted_client_enabled=True)

    def test_missing_crypto_runtime_fails_without_unsigned_fallback(self):
        import builtins
        cfg,processing=config(),plan();envelope,public,_=signed(cfg,processing)
        original=builtins.__import__
        def unavailable(name,*args,**kwargs):
            if name.startswith('cryptography'):raise ImportError('Synthetic missing runtime')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=unavailable),self.assertRaises(ValueError):
            verify_acceptance(envelope,public,digest(cfg),digest(processing))


if __name__=='__main__':unittest.main()
