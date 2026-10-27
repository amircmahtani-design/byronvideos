from types import SimpleNamespace
from fastapi.testclient import TestClient
from byron_video.admin import AdminSession, install_admin_routes
from byron_video.hosted_control import create_app
from byron_video.private_config import SecretManagerConfig

ORIGIN = 'https://studio.example'
TOKEN = 'synthetic-csrf-fixture-' + 'x' * 32
KEY = 'synthetic-key-never-live'


class Store:
    def __init__(self):
        self.calls = []
    def save_key(self, *args):
        self.calls.append(args)
    def save_default_voice(self, *args):
        self.calls.append(args)


def app(store, **kwargs):
    value = create_app()
    install_admin_routes(value, private_store=store,
                         session_resolver=kwargs.pop('session_resolver', lambda request: AdminSession('admin_fixture', TOKEN)),
                         allowed_origin=ORIGIN, activated=kwargs.pop('activated', True), **kwargs)
    return value


def headers():
    return {'Origin': ORIGIN, 'X-CSRF-Token': TOKEN}


def test_disabled_and_unauthorized_admin_never_writes():
    store = Store()
    for options, code in [({'activated':False}, 503), ({'session_resolver':lambda request:None},403)]:
        with TestClient(app(store, **options)) as client:
            assert client.get('/api/admin/status').status_code == code
            assert client.post('/api/admin/keys/elevenlabs', json={'key':KEY}, headers=headers()).status_code == code
    assert store.calls == []


def test_csrf_origin_provider_allowlist_and_no_readback():
    store = Store()
    with TestClient(app(store)) as client:
        for h in [{}, {'Origin':ORIGIN}, {**headers(), 'Origin':'https://attacker.example'}, {**headers(), 'Sec-Fetch-Site':'cross-site'}]:
            assert client.post('/api/admin/keys/elevenlabs', json={'key':KEY}, headers=h).status_code == 403
        assert client.post('/api/admin/keys/other', json={'key':KEY}, headers=headers()).status_code == 404
        response = client.post('/api/admin/keys/elevenlabs', json={'key':KEY}, headers=headers())
        assert response.json() == {'saved':True, 'tested':False, 'render_ready':False}
        assert response.headers['cache-control'] == 'no-store'
        assert KEY not in response.text
        assert KEY not in client.get('/api/admin/status').text
        assert client.get('/api/admin/keys/elevenlabs').status_code == 405
        assert client.post('/api/admin/test/elevenlabs', headers=headers()).status_code == 503
        # Validation responses must not echo submitted secret or extra fields.
        bad = client.post('/api/admin/keys/elevenlabs', json={'key':KEY, 'extra':KEY}, headers=headers())
        assert bad.status_code == 422 and KEY not in bad.text
    assert store.calls == [('admin_fixture', 'elevenlabs', KEY)]


def test_voice_access_checked_before_private_default_save():
    store = Store()
    with TestClient(app(store, voice_provider=lambda owner:[{'id':'fixture_voice_1', 'name':'Narrator'}])) as client:
        assert client.post('/api/admin/default-voice', json={'voice_id':'missing_voice'}, headers=headers()).status_code == 422
        assert store.calls == []
        response = client.post('/api/admin/default-voice', json={'voice_id':'fixture_voice_1'}, headers=headers())
        assert response.json() == {'saved':True, 'render_ready':False}
    assert store.calls == [('admin_fixture','fixture_voice_1')]


def test_test_success_is_not_render_readiness_and_errors_are_redacted():
    store = Store()
    with TestClient(app(store, connection_tester=lambda subject, provider:True)) as client:
        assert client.post('/api/admin/test/elevenlabs', headers=headers()).json() == {'connected':True, 'render_ready':False}
    def fail(*args):
        raise RuntimeError(KEY)
    store.save_key = fail
    with TestClient(app(store, connection_tester=fail)) as client:
        for path, body in [('/api/admin/keys/fal', {'key':KEY}), ('/api/admin/test/fal', None)]:
            response = client.post(path, json=body, headers=headers())
            assert response.status_code == 503 and KEY not in response.text


def test_secret_manager_writes_existing_resources_only():
    calls = []
    client = SimpleNamespace(add_secret_version=lambda **kwargs:calls.append(kwargs))
    resources = {k:'projects/fixture-project/secrets/' + k for k in ('elevenlabs','fal','default_voice')}
    store = SecretManagerConfig(client, admin_subject='admin_fixture', resources=resources)
    store.save_key('admin_fixture', 'elevenlabs', KEY)
    store.save_default_voice('admin_fixture','fixture_voice_1')
    assert calls[0]['request']['parent'] == resources['elevenlabs']
    assert calls[0]['request']['payload']['data'] == KEY.encode()
    assert len(calls) == 2


def test_non_ascii_csrf_rejected_without_server_error_or_write():
    store = Store()
    with TestClient(app(store), raise_server_exceptions=False) as client:
        response = client.post('/api/admin/keys/elevenlabs', json={'key':KEY},
                               headers={b'origin':ORIGIN.encode(), b'x-csrf-token':b'\xff'})
        assert response.status_code == 403
    with TestClient(app(store, session_resolver=lambda request:AdminSession('admin_fixture', 'é'*40)), raise_server_exceptions=False) as client:
        assert client.get('/api/admin/status').status_code == 403
    assert store.calls == []
