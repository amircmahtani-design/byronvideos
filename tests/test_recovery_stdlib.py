"""Focused offline recovery checks; unittest runner, no pytest/install/network.

This supplements rather than replaces the full pytest release gate. All identity,
provider and storage values below are synthetic; no service is activated.
"""
import pathlib
import runpy
import tempfile
import unittest
from fastapi.testclient import TestClient
from byron_video import hosted_control as control
from byron_video.voices import list_voices, VoiceUnavailable

ROOT = pathlib.Path(__file__).resolve().parent.parent


class RecoveryChecks(unittest.TestCase):
    def test_existing_admin_cases(self):
        cases = runpy.run_path(str(ROOT / 'tests/test_admin.py'))
        for name, case in cases.items():
            if name.startswith('test_'):
                with self.subTest(name=name):
                    case()

    def test_catalog_pagination_filtering_and_minimal_metadata(self):
        def voice(id, **extra):
            return dict(voice_id=id, name='Narrator', category='generated', is_owner=True, **extra)
        pages = [dict(voices=[voice('fixture_voice_1'), dict(voice('shared_voice'), is_owner=False)],
                      has_more=True, next_page_token='a+b'),
                 dict(voices=[voice('fixture_voice_2'), voice('fixture_voice_1'),
                              dict(voice('premade_voice'), category='premade')], has_more=False)]
        urls = []
        def get(url):
            urls.append(url)
            return 200, pages.pop(0)
        self.assertEqual(list_voices(get), [dict(id='fixture_voice_1', name='Narrator'), dict(id='fixture_voice_2', name='Narrator')])
        self.assertIn('next_page_token=a%2Bb', urls[1])
        self.assertTrue(all('voice_type=personal' in u and 'category=generated' in u for u in urls))

    def test_provider_errors_are_redacted(self):
        for status in [401, 403, 429, 500]:
            with self.subTest(status=status), self.assertRaises(VoiceUnavailable) as raised:
                list_voices(lambda url: (status, {'secret': 'synthetic-never-disclose'}))
            self.assertNotIn('synthetic-never-disclose', str(raised.exception))

    def test_pagination_repeated_or_missing_token_fails_closed(self):
        for token in ['repeat', None, 'x' * 4097]:
            with self.subTest(token_length=len(token or '')), self.assertRaises(VoiceUnavailable):
                list_voices(lambda url: (200, dict(voices=[], has_more=True, next_page_token=token)))

    def test_pagination_is_bounded(self):
        calls = []
        def get(url):
            calls.append(url)
            return 200, dict(voices=[], has_more=True, next_page_token=str(len(calls)))
        with self.assertRaises(VoiceUnavailable):
            list_voices(get)
        self.assertEqual(len(calls), 100)

    def test_voice_route_requires_verified_creator(self):
        calls = []
        with TestClient(control.create_app(voice_provider=lambda owner: calls.append(owner))) as client:
            self.assertEqual(client.get('/api/voices').status_code, 401)
        self.assertEqual(calls, [])

    def test_voice_route_strips_private_metadata(self):
        app = control.create_app(principal_resolver=lambda request: control.Principal('fixture_owner'),
            voice_provider=lambda owner: [dict(id='fixture_voice_1', name='Narrator', account='private')],
            default_voice_provider=lambda owner: 'fixture_voice_1')
        with TestClient(app) as client:
            response = client.get('/api/voices')
            self.assertEqual(response.json(), dict(voices=[dict(id='fixture_voice_1', name='Narrator')], default_voice_id='fixture_voice_1'))
            self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_inactive_health_and_unmounted_admin(self):
        with TestClient(control.create_app()) as client:
            self.assertFalse(client.get('/api/health').json()['ready'])
            self.assertEqual(client.get('/api/admin/status').status_code, 404)

    def test_selected_voice_is_frozen_and_duplicate_is_not_dispatched(self):
        with tempfile.TemporaryDirectory() as root:
            store = control.SQLiteFixtureStore(pathlib.Path(root) / 'jobs.sqlite3')
            profile = dict(schema_version=1, version='synthetic-fixture-1', status='DRAFT', provider='elevenlabs',
                voice_id='SYNTHETIC_TEST_VOICE', model_id='eleven_multilingual_v2',
                voice_settings=dict(stability=.5, similarity_boost=.75, style=0, use_speaker_boost=True, speed=.93),
                output_format='mp3_44100_128', seed=1, apply_text_normalization='off')
            plan = dict(schema_version=1, status='DRAFT_NOT_ACTIVATED', visual_style_sha256='b'*64,
                motion_policy_sha256='c'*64, output=dict(width=1080, height=1920, fps=24), narration_profile=profile)
            catalog = [dict(id='fixture_voice_1', name='Narrator'), dict(id='fixture_voice_2', name='Another')]
            calls = []
            def dispatch(handoff):
                calls.append(handoff)
                return control.DispatchReceipt(handoff['job_id'], handoff['snapshot_sha256'], 'execution_' + handoff['job_id'])
            dispatch.synthetic_fixture = True
            app = control.create_app(principal_resolver=lambda request: control.Principal('fixture_owner'), store=store,
                dispatcher=dispatch, plan_provider=lambda owner: plan, voice_provider=lambda owner: catalog,
                upload_resolver=lambda owner,id: control.VerifiedImage(id,owner.subject,'a'*64,1234,640,480,'image/png','object_'+id,'42',True))
            body = dict(script='Synthetic unchanged words.', voice_id='fixture_voice_1',
                images=[dict(id=f'painting_{i:03}',position=i) for i in range(1,5)])
            headers = {'Idempotency-Key':'fixture_request_000001'}
            try:
                with TestClient(app) as client:
                    first = client.post('/api/jobs', json=body, headers=headers)
                    self.assertEqual(first.status_code, 202, first.text)
                    job_id = first.json()['id']
                    snapshot = store.get('fixture_owner', job_id).snapshot
                    self.assertEqual(snapshot['processing_plan']['narration_profile']['voice_id'], 'fixture_voice_1')
                    self.assertEqual(profile['voice_id'], 'SYNTHETIC_TEST_VOICE')
                    catalog.clear()
                    repeated = client.post('/api/jobs', json=body, headers=headers)
                    self.assertEqual(repeated.status_code, 202, repeated.text)
                    self.assertTrue(repeated.json()['reused'])
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(store.get('fixture_owner', job_id).snapshot, snapshot)
                    removed = client.post('/api/jobs', json=body, headers={'Idempotency-Key':'fixture_request_000002'})
                    self.assertEqual(removed.status_code, 422)
                    conflict = client.post('/api/jobs', json={**body, 'voice_id':'fixture_voice_2'}, headers=headers)
                    self.assertEqual(conflict.status_code, 409)
                    self.assertEqual(len(calls), 1)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
