"""Saved-key display records: timestamps only, never key material."""
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from byron_video.admin import AdminSession, install_admin_routes
from byron_video.key_records import FirestoreKeyRecords
from test_cloud_journal_stdlib import Client, PROJECT

ORIGIN = 'https://films.example'
TOKEN = 'c' * 43


class Store:
    def __init__(self): self.saved = []
    def save_key(self, subject, provider, key): self.saved.append(provider)


class KeyRecordChecks(unittest.TestCase):
    def setUp(self):
        self.db = Client()
        self.records = FirestoreKeyRecords(self.db, project_id=PROJECT, namespace='fixture_keys', transaction_runner=self.db.run)

    def test_records_only_timestamps(self):
        self.assertEqual(self.records.read(), {})
        self.records.mark_saved('elevenlabs', 'first-elevenlabs-key-AAAA', now=1000)
        self.records.mark_saved('fal', 'fixture-fal-key-0123:BBBB', now=2000)
        self.records.mark_saved('elevenlabs', 'second-elevenlabs-key-CCCC', now=3000)
        self.assertEqual(self.records.read(), {'elevenlabs': {'saved_at': 3000, 'ending': 'CCCC'},
                                               'fal': {'saved_at': 2000, 'ending': 'BBBB'}})
        self.records.mark_saved('fal', 'short', now=4000)
        self.assertIsNone(self.records.read()['fal']['ending'])
        with self.assertRaises(ValueError): self.records.mark_saved('other')

    def test_save_route_reports_saved_and_status_lists_it_without_key(self):
        store = Store(); app = FastAPI()
        install_admin_routes(app, session_resolver=lambda request: AdminSession('owner', TOKEN), private_store=store,
                             key_records=self.records, allowed_origin=ORIGIN, activated=True)
        key = 'fixture-secret-key-0123456789'
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.post('/api/admin/keys/fal', json={'key': key}, headers={'origin': ORIGIN, 'x-csrf-token': TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['saved_at']['ending'], '6789')
            status = client.get('/api/admin/status')
            self.assertEqual(status.json()['saved_keys']['fal']['ending'], '6789')
            self.assertNotIn(key, response.text + status.text)
            self.assertNotIn(key[:-4], response.text + status.text)
        self.assertEqual(store.saved, ['fal'])


if __name__ == '__main__': unittest.main()
