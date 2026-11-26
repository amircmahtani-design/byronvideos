"""Owner-managed named voices: offline fake Firestore and fake ElevenLabs only."""
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from byron_video.admin import AdminSession, install_admin_routes
from byron_video.voice_library import FirestoreVoiceLibrary, validate_library
from byron_video.voices import VoiceUnavailable, voice_name
from test_cloud_journal_stdlib import Client, PROJECT

BYRON = '4IzoqFsOBpzaW3TKxHEF'
OTHER = 'fixture_voice_0002'
ORIGIN = 'https://films.example'
TOKEN = 'c' * 43


class LibraryChecks(unittest.TestCase):
    def setUp(self):
        self.db = Client()
        self.library = FirestoreVoiceLibrary(self.db, project_id=PROJECT, namespace='fixture_voices', transaction_runner=self.db.run)

    def test_add_rename_default_and_remove(self):
        self.assertEqual(self.library.read(), {'voices': [], 'default_voice_id': None})
        self.assertEqual(self.library.add(BYRON, '  Byron   Narrator 1 '),
                         {'voices': [{'id': BYRON, 'name': 'Byron Narrator 1'}], 'default_voice_id': BYRON})
        self.library.add(OTHER, 'Second voice')
        self.assertEqual(self.library.read()['default_voice_id'], BYRON)
        self.library.add(OTHER, 'Renamed voice')
        self.assertEqual([v['name'] for v in self.library.read()['voices']], ['Byron Narrator 1', 'Renamed voice'])
        self.library.set_default(OTHER)
        self.library.remove(OTHER)
        self.assertEqual(self.library.read(), {'voices': [{'id': BYRON, 'name': 'Byron Narrator 1'}], 'default_voice_id': BYRON})
        self.library.remove(BYRON)
        self.assertEqual(self.library.read(), {'voices': [], 'default_voice_id': None})

    def test_invalid_input_and_duplicates_rejected(self):
        self.library.add(BYRON, 'Byron Narrator 1')
        for voice_id, name in [('https://elevenlabs.io/x', 'Link'), ('short', 'Short'), (OTHER, ''), (OTHER, '<b>x</b>'),
                               (OTHER, 'x' * 61), (OTHER, 'byron narrator 1')]:
            with self.subTest(voice_id=voice_id, name=name), self.assertRaises(ValueError):
                self.library.add(voice_id, name)
        with self.assertRaises(ValueError): self.library.set_default(OTHER)
        with self.assertRaises(ValueError): self.library.remove(OTHER)
        with self.assertRaises(ValueError):
            validate_library({'voices': [{'id': BYRON, 'name': 'A'}], 'default_voice_id': OTHER})


class NameLookupChecks(unittest.TestCase):
    def test_name_read_from_elevenlabs_by_exact_id(self):
        calls = []
        def get(url):
            calls.append(url); return 200, {'voice_id': BYRON, 'name': 'Byron Narrator 1', 'samples': ['ignored']}
        self.assertEqual(voice_name(get, BYRON), 'Byron Narrator 1')
        self.assertEqual(calls, ['https://api.elevenlabs.io/v1/voices/' + BYRON])
        for reply in [(404, {}), (200, {'voice_id': OTHER, 'name': 'Wrong'}), (200, {'voice_id': BYRON}), (401, {})]:
            with self.subTest(reply=reply), self.assertRaises(VoiceUnavailable):
                voice_name(lambda url: reply, BYRON)
        with self.assertRaises(VoiceUnavailable): voice_name(get, '../secrets')


class RouteChecks(unittest.TestCase):
    def app(self, namer=None):
        self.db = Client()
        library = FirestoreVoiceLibrary(self.db, project_id=PROJECT, namespace='fixture_voices', transaction_runner=self.db.run)
        app = FastAPI()
        install_admin_routes(app, session_resolver=lambda request: AdminSession('owner', TOKEN), private_store=object(),
                             voice_library=library, voice_namer=namer, allowed_origin=ORIGIN, activated=True)
        return TestClient(app, base_url=ORIGIN)

    def post(self, client, path, body=None, **headers):
        return client.post(path, json=body, headers={'origin': ORIGIN, 'x-csrf-token': TOKEN, **headers})

    def test_setup_mode_requires_typed_name_and_csrf(self):
        with self.app() as client:
            self.assertEqual(client.post('/api/admin/voices', json={'voice_id': BYRON, 'name': 'Byron Narrator 1'}).status_code, 403)
            self.assertEqual(self.post(client, '/api/admin/voices', {'voice_id': BYRON}).status_code, 422)
            response = self.post(client, '/api/admin/voices', {'voice_id': BYRON, 'name': 'Byron Narrator 1'})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['checked_with_elevenlabs'])
            listed = client.get('/api/admin/voices').json()
            self.assertEqual(listed['voices'], [{'id': BYRON, 'name': 'Byron Narrator 1'}])
            self.assertFalse(listed['names_from_elevenlabs'])
            self.assertEqual(self.post(client, '/api/admin/voices/refresh-names').status_code, 503)

    def test_connected_mode_takes_name_from_elevenlabs(self):
        names = {BYRON: 'Byron Narrator 1'}
        def namer(voice_id):
            if voice_id not in names: raise VoiceUnavailable('no')
            return names[voice_id]
        with self.app(namer) as client:
            response = self.post(client, '/api/admin/voices', {'voice_id': BYRON, 'name': 'Typed name is ignored'})
            self.assertEqual(response.json()['voices'], [{'id': BYRON, 'name': 'Byron Narrator 1'}])
            self.assertTrue(response.json()['checked_with_elevenlabs'])
            self.assertEqual(self.post(client, '/api/admin/voices', {'voice_id': OTHER}).status_code, 422)
            names[BYRON] = 'Byron Narrator One'
            self.assertEqual(self.post(client, '/api/admin/voices/refresh-names').json()['voices'][0]['name'], 'Byron Narrator One')


if __name__ == '__main__': unittest.main()
