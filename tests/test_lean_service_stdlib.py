"""Lean owner-only routes, CSRF/origin, cap refusal, idempotent submit and bootstrap flag; offline."""
import base64
import hashlib
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from fastapi.testclient import TestClient

from byron_video.admin import AdminSession
from byron_video.administration_runtime import PURPOSE, assemble_lean
from byron_video.hosted_control import Principal, VerifiedImage, canonical, digest
from byron_video.hosted_uploads import CreatorAccess, CreatorSession
from byron_video.lean_film import LeanBudget, LeanJobStore, LeanLedger, lean_settings, owner_for
from byron_video.lean_service import install_lean_routes
from byron_video.service_bootstrap import ServiceBindings
from test_cloud_journal_stdlib import Client, PROJECT
from test_hosted_uploads_stdlib import image_bytes, plan
from test_lean_film_stdlib import NS, OWNER, TEXT, images, lean_config
from test_lean_worker_stdlib import Objects

ORIGIN = 'https://studio.example'
TOKEN = 'c' * 43
KEY = 'lean_submission_00000001'
GOOD = {'origin': ORIGIN, 'x-csrf-token': TOKEN, 'idempotency-key': KEY}


class Library:
    def read(self): return {'voices': [{'id': 'SYNTHETIC_TEST_VOICE', 'name': 'Narrator'}], 'default_voice_id': 'SYNTHETIC_TEST_VOICE'}


class Uploads:
    def __init__(self): self.records = {x['id']: VerifiedImage(**{k: v for k, v in x.items() if k != 'position'}) for x in images(5)}
    def resolve(self, principal, image_id):
        record = self.records[image_id]; assert principal.subject == record.owner_id
        return record
    def commit_batch(self, principal, key, fingerprint, prepared):
        return 'u' * 64, [VerifiedImage(f'upload_{i:04}', principal.subject, p.sha256, len(p.data), p.width, p.height, p.content_type,
                                        f'upload_{i:04}', '1', True) for i, p in enumerate(prepared, 1)]


def body(script=TEXT, count=4):
    return {'script': script, 'images': [{'id': f'painting_{i:04}', 'position': i} for i in range(1, count + 1)],
            'voice_id': 'SYNTHETIC_TEST_VOICE'}


class LeanRouteChecks(unittest.TestCase):
    def setUp(self):
        self.client, self.config = Client(), lean_config()
        self.store = LeanJobStore(self.client, project_id=PROJECT, namespace=NS, transaction_runner=self.client.run)
        self.objects, self.started, self.subject, self.remaining = Objects(), [], OWNER, None
        budget = LeanBudget(self.client, project_id=PROJECT, namespace=NS, transaction_runner=self.client.run)
        def session(request):
            if self.subject is None: raise ValueError('no session')
            return CreatorSession(Principal(self.subject), TOKEN)
        app = FastAPI()
        install_lean_routes(app, access=CreatorAccess(session, ORIGIN), owner_id=OWNER, config=self.config,
            settings=lean_settings(self.config), jobs=self.store,
            ledger_factory=lambda job_id: LeanLedger(self.client, project_id=PROJECT, namespace=NS, owner_id=OWNER, job_id=job_id,
                                                     budget=self.config['budget'], transaction_runner=self.client.run),
            budget_reader=lambda: budget.remaining_cents(self.config['budget']) if self.remaining is None else self.remaining,
            uploads=Uploads(), objects=self.objects, library=Library(),
            starter=lambda job_id: self.started.append(job_id) or 'projects/synthetic-project/locations/us-central1/operations/op_00000001',
            check=lambda: True, release_sha256='e' * 64)
        self.http = TestClient(app, base_url=ORIGIN)

    def test_capabilities_are_truthful_and_owner_only(self):
        value = self.http.get('/api/creator-capabilities').json()
        self.assertEqual({k: value[k] for k in ('enabled', 'render_ready', 'mode', 'protocol_version', 'live_release', 'synthetic_fixture')},
                         {'enabled': True, 'render_ready': True, 'mode': 'hosted', 'protocol_version': 1, 'live_release': True, 'synthetic_fixture': False})
        self.assertEqual(value['csrf_token'], TOKEN)
        self.assertEqual(self.http.get('/api/health').json()['mode'], 'hosted')
        self.subject = None; self.assertEqual(self.http.get('/api/creator-capabilities').status_code, 401)
        self.subject = 'google_' + 'b' * 64; self.assertEqual(self.http.get('/api/creator-capabilities').status_code, 403)

    def test_submit_needs_origin_and_csrf_then_is_idempotent_and_starts_once(self):
        for headers in ({'idempotency-key': KEY}, {**GOOD, 'origin': 'https://evil.example'}, {**GOOD, 'x-csrf-token': 'x' * 43},
                        {**GOOD, 'sec-fetch-site': 'cross-site'}):
            self.assertEqual(self.http.post('/api/jobs', json=body(), headers=headers).status_code, 403)
        self.assertIsNone(self.store.find(OWNER, KEY)); self.assertEqual(self.started, [])
        first = self.http.post('/api/jobs', json=body(), headers=GOOD)
        self.assertEqual(first.status_code, 202)
        job_id = first.json()['id']
        again = self.http.post('/api/jobs', json=body(), headers=GOOD).json()
        self.assertEqual((again['id'], again['reused']), (job_id, True)); self.assertEqual(self.started, [job_id])
        self.assertEqual(self.http.post('/api/jobs', json=body(TEXT + ' More.'), headers=GOOD).status_code, 409)
        status = self.http.get(f'/api/jobs/{job_id}').json()
        self.assertEqual(status['status'], 'queued'); self.assertGreater(status['expected_cost_cents'], 0)
        self.assertNotIn(TEXT, json.dumps(status))
        self.assertEqual(self.http.get(f'/api/submissions/{KEY}').json()['id'], job_id)
        job = self.store.get(OWNER, job_id)
        self.assertEqual(job.inputs['script'], TEXT); self.assertEqual(job.inputs['input_order'], [f'painting_{i:04}' for i in range(1, 5)])
        self.assertEqual(job.start_state, 'started')
        ledger = LeanLedger(self.client, project_id=PROJECT, namespace=NS, owner_id=OWNER, job_id=job_id,
                            budget=self.config['budget'], transaction_runner=self.client.run)
        self.assertEqual(ledger.remaining_cents(), 1000 - job.inputs['estimate_cents'])  # Whole film funded once.
        self.subject = 'google_' + 'b' * 64
        self.assertEqual(self.http.get(f'/api/jobs/{job_id}').status_code, 403)
        self.subject = OWNER
        self.assertEqual(self.http.get('/api/jobs/' + 'f' * 32).status_code, 404)

    def test_over_cap_is_refused_before_any_job_or_start(self):
        self.remaining = 50
        response = self.http.post('/api/jobs', json=body(), headers=GOOD)
        self.assertEqual(response.status_code, 402); self.assertIn('$0.50', response.json()['detail'])
        self.assertIsNone(self.store.find(OWNER, KEY)); self.assertEqual(self.started, [])

    def test_unknown_voice_and_unordered_paintings_are_rejected(self):
        self.assertEqual(self.http.post('/api/jobs', json={**body(), 'voice_id': 'UNKNOWN_VOICE_1'}, headers=GOOD).status_code, 422)
        reordered = body(); reordered['images'][0]['position'] = 2; reordered['images'][1]['position'] = 1
        self.assertEqual(self.http.post('/api/jobs', json=reordered, headers=GOOD).status_code, 422)
        self.assertEqual(self.started, [])

    def test_download_only_after_completion_and_streams_verified_bytes(self):
        job_id = self.http.post('/api/jobs', json=body(), headers=GOOD).json()['id']
        self.assertEqual(self.http.get(f'/api/jobs/{job_id}/video').status_code, 409)
        self.store.begin(OWNER, job_id)
        ref = self.objects.put(OWNER, job_id, 'film', b'synthetic mp4 bytes', 'video/mp4')
        self.store.complete(OWNER, job_id, {'video': ref, 'duration_seconds': 30.0})
        status = self.http.get(f'/api/jobs/{job_id}').json()
        self.assertEqual((status['status'], status['download_url']), ('complete', f'/api/jobs/{job_id}/video'))
        video = self.http.get(f'/api/jobs/{job_id}/video')
        self.assertEqual((video.status_code, video.content), (200, b'synthetic mp4 bytes'))
        self.assertEqual(video.headers['content-type'], 'video/mp4')

    def test_upload_route_is_owner_csrf_bound_and_returns_ordered_ids(self):
        files = [('images', (f'p{i}.png', image_bytes(i), 'image/png')) for i in range(1, 5)]
        self.assertEqual(self.http.post('/api/uploads', files=files, headers={'idempotency-key': KEY}).status_code, 403)
        response = self.http.post('/api/uploads', files=files, headers=GOOD)
        self.assertEqual(response.status_code, 201)
        self.assertEqual([x['position'] for x in response.json()['images']], [1, 2, 3, 4])
        bad = [('images', ('p.gif', b'GIF89a', 'image/gif'))] * 4
        self.assertEqual(self.http.post('/api/uploads', files=bad, headers=GOOD).status_code, 422)


def signed(config, processing_plan):
    private = Ed25519PrivateKey.generate(); key = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    now = int(time.time())
    payload = dict(schema_version=1, purpose=PURPOSE, config_sha256=digest(config), plan_sha256=digest(processing_plan),
                   issued_at=now - 1, expires_at=now + 600, owner_only=True, generation_allowed=False, provider_calls_allowed=False)
    return dict(payload=payload, signature=base64.b64encode(private.sign(canonical(payload).encode())).decode()), key


class LeanAssemblyChecks(unittest.TestCase):
    def setUp(self):
        self.config, self.plan = lean_config(), plan()
        self.envelope, self.key = signed(self.config, self.plan)
        firestore = Client(); firestore._database_string = 'projects/synthetic-project/databases/synthetic-setup'
        def forbidden(*a, **kw): raise AssertionError('Unexpected network/secret read')
        self.bindings = ServiceBindings(firestore, SimpleNamespace(bucket=lambda name: SimpleNamespace()),
                                        SimpleNamespace(access_secret_version=forbidden), forbidden, b'x' * 32)

    def test_lean_assembly_reports_render_ready_only_for_the_owner(self):
        runtime = assemble_lean(config=self.config, processing_plan=self.plan, bindings=self.bindings,
                                acceptance=self.envelope, reviewer_public_key=self.key, transaction_runner=self.bindings.firestore_client.run)
        owner = owner_for(self.config)
        with TestClient(runtime.app, base_url=ORIGIN) as http:
            self.assertEqual(http.get('/api/creator-capabilities').status_code, 401)
            self.assertEqual(http.post('/api/jobs', json=body()).status_code, 401)
            with patch.object(runtime.identity, 'admin_session', return_value=AdminSession(owner, TOKEN)):
                value = http.get('/api/creator-capabilities').json()
                self.assertTrue(value['enabled'] and value['render_ready'] and value['lean_generation'])
                self.assertEqual(value['runtime_release_sha256'], digest(self.envelope))
                self.assertEqual(http.post('/api/jobs', json=body(), headers={'idempotency-key': KEY}).status_code, 403)
                self.assertEqual(http.get('/api/admin/status').status_code, 200)
            self.assertEqual(http.get('/').status_code, 200)

    def test_lean_needs_its_signed_config_section(self):
        config = lean_config(); del config['lean_generation']
        envelope, key = signed(config, self.plan)
        with self.assertRaises(ValueError):
            assemble_lean(config=config, processing_plan=self.plan, bindings=self.bindings, acceptance=envelope, reviewer_public_key=key)

    def test_bootstrap_flag_requires_setup_bundle_and_selects_lean(self):
        from byron_video import service_bootstrap as bootstrap
        bundle = json.dumps(dict(schema_version=1, config=self.config, processing_plan=self.plan, acceptance=self.envelope,
                                 reviewer_public_key_base64=base64.b64encode(self.key).decode()))
        args = ['--bootstrap-env', 'BYRON_TEST_LEAN', '--reviewer-key-sha256', hashlib.sha256(self.key).hexdigest(),
                '--trusted-proxy', '127.0.0.1', '--serve']
        with patch.dict('os.environ', {'BYRON_TEST_LEAN': bundle}), \
                patch.object(bootstrap, 'private_bytes', return_value=b'{}'), \
                patch.object(bootstrap, 'validate_service_runtime', return_value={}), \
                patch('byron_video.service_bindings.build_service_bindings', return_value=self.bindings), \
                patch('byron_video.administration_runtime.assemble_lean', return_value=SimpleNamespace(app=None)) as lean, \
                patch('byron_video.administration_runtime.assemble_administration') as setup, patch('uvicorn.run') as serve:
            with self.assertRaises(SystemExit): bootstrap.main(args + ['--lean-generation'])
            lean.assert_not_called(); serve.assert_not_called()
            self.assertEqual(bootstrap.main(args + ['--administration-only', '--lean-generation']), 0)
            lean.assert_called_once(); setup.assert_not_called(); serve.assert_called_once()


if __name__ == '__main__': unittest.main()
