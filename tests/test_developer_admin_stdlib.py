"""Selected Developer mode secrets are write-only; no provider requests."""
import unittest
from types import SimpleNamespace
from fastapi.testclient import TestClient
from test_admin import app,Store,KEY,headers
from byron_video.private_config import SecretManagerConfig

class DeveloperAdminTests(unittest.TestCase):
    def test_default_route_rejects_gemini_even_with_valid_admin(self):
        store=Store()
        with TestClient(app(store)) as client:
            self.assertEqual(client.get('/api/admin/status').json()['native_api_mode'],'vertex_eu')
            self.assertEqual(client.post('/api/admin/keys/gemini',json={'key':KEY},headers=headers()).status_code,404)
        self.assertEqual(store.calls,[])
    def test_explicit_route_write_only_no_connection_test_or_readback(self):
        store=Store()
        with TestClient(app(store,native_api_mode='gemini_developer')) as client:
            status=client.get('/api/admin/status');self.assertEqual(status.json()['native_api_mode'],'gemini_developer')
            self.assertNotIn(KEY,status.text)
            self.assertEqual(client.post('/api/admin/keys/gemini',json={'key':KEY}).status_code,403)
            saved=client.post('/api/admin/keys/gemini',json={'key':KEY},headers=headers())
            self.assertEqual(saved.json(),{'saved':True,'tested':False,'render_ready':False});self.assertNotIn(KEY,saved.text)
            self.assertEqual(client.get('/api/admin/keys/gemini').status_code,405)
            self.assertEqual(client.post('/api/admin/test/gemini',headers=headers()).status_code,503)
        self.assertEqual(store.calls,[('admin_fixture','gemini',KEY)])
    def test_store_error_cannot_echo_secret(self):
        class Broken(Store):
            def save_key(self,*args):raise RuntimeError(KEY)
        with TestClient(app(Broken(),native_api_mode='gemini_developer')) as client:
            response=client.post('/api/admin/keys/gemini',json={'key':KEY},headers=headers())
            self.assertEqual(response.status_code,503);self.assertNotIn(KEY,response.text)
    def test_secret_adapter_optional_resource_and_owner(self):
        calls=[];client=SimpleNamespace(add_secret_version=lambda **kw:calls.append(kw))
        resources={k:'projects/synthetic-project/secrets/'+k for k in ('elevenlabs','fal','default_voice')}
        store=SecretManagerConfig(client,admin_subject='owner',resources=resources)
        with self.assertRaises(ValueError):store.save_key('owner','gemini',KEY)
        resources['gemini']='projects/synthetic-project/secrets/gemini'
        store=SecretManagerConfig(client,admin_subject='owner',resources=resources)
        with self.assertRaises(ValueError):store.save_key('other','gemini',KEY)
        store.save_key('owner','gemini',KEY)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0]['request']['parent'],resources['gemini'])
    def test_invalid_mode_never_mounts(self):
        with self.assertRaises(ValueError):app(Store(),native_api_mode='fallback')

if __name__=='__main__':unittest.main()
