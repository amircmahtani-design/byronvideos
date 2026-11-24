import json
from types import SimpleNamespace

import pytest
from byron_video.provider_balances import ProviderBalances, billing_resources

EL_KEY = 'synthetic-elevenlabs-billing-key'
FAL_KEY = 'synthetic-fal-billing-key'
SIGNED = {'elevenlabs': 'projects/p1/secrets/byron-elevenlabs', 'fal': 'projects/p1/secrets/byron-fal',
          'default_voice': 'projects/p1/secrets/v', 'csrf': 'projects/p1/secrets/c', 'google_oauth': 'projects/p1/secrets/o'}


class Secrets:
    def __init__(self, values):
        self.values, self.names = values, []

    def access_secret_version(self, *, request, retry, timeout):
        assert retry is None and 0 < timeout <= 15
        self.names.append(request['name'])
        if request['name'] not in self.values:
            raise RuntimeError('NotFound ' + request['name'])
        return SimpleNamespace(payload=SimpleNamespace(data=self.values[request['name']].encode()))


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def iter_bytes(self):
        yield self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()


class Http:
    def __init__(self, routes):
        self.routes, self.calls = routes, []
    def __call__(self):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def stream(self, method, url, headers):
        assert method == 'GET'
        self.calls.append((url, headers))
        route = self.routes[url.split('?')[0]]
        if isinstance(route, Exception):
            raise route
        return Response(*route)


EL = 'https://api.elevenlabs.io/v1/user/subscription'
FAL = 'https://api.fal.ai/v1/account/billing'
STORED = {'projects/p1/secrets/byron-elevenlabs-billing/versions/latest': EL_KEY,
          'projects/p1/secrets/byron-fal-billing/versions/latest': FAL_KEY + '\n'}


def reader(secrets, routes):
    http = Http(routes)
    return ProviderBalances(secrets, billing_resources(SIGNED), http_factory=http, clock=lambda: 1_764_000_000), http


def test_billing_names_derive_from_signed_names_without_new_config():
    assert billing_resources(SIGNED) == {'elevenlabs': 'projects/p1/secrets/byron-elevenlabs-billing',
                                         'fal': 'projects/p1/secrets/byron-fal-billing'}
    with pytest.raises(ValueError):
        billing_resources({**SIGNED, 'fal': 'byron-fal'})
    with pytest.raises(ValueError):
        ProviderBalances(Secrets({}), {'elevenlabs': SIGNED['elevenlabs'], 'fal': SIGNED['fal']})  # Generation keys refused.


def test_balances_read_with_billing_keys_only():
    secrets = Secrets(STORED)
    balances, http = reader(secrets, {
        EL: (200, {'character_count': 20058, 'character_limit': 121855, 'next_character_count_reset_unix': 1764374400,
                   'tier': 'creator', 'open_invoices': [{'secret': 'x'}]}),
        FAL: (200, {'username': 'someone', 'credits': {'current_balance': 42.517, 'currency': 'usd'}})})
    assert balances() == {
        'elevenlabs': {'state': 'available', 'credits_used': 20058, 'credits_limit': 121855, 'credits_remaining': 101797,
                       'resets_at': 1764374400, 'plan': 'creator'},
        'fal': {'state': 'available', 'balance': 42.52, 'currency': 'USD'},
        'checked_at': 1_764_000_000}
    assert all(name.endswith('-billing/versions/latest') for name in secrets.names)
    assert sorted(http.calls, key=lambda call: call[0]) == [
        (EL, {'xi-api-key': EL_KEY, 'Accept': 'application/json'}),
        (FAL + '?expand=credits', {'Authorization': 'Key ' + FAL_KEY, 'Accept': 'application/json'})]
    assert EL_KEY not in repr(balances)


def test_missing_key_lacking_permission_and_errors_are_states_not_failures():
    balances, http = reader(Secrets({}), {})
    result = balances()
    assert result['elevenlabs']['state'] == result['fal']['state'] == 'not_set_up' and http.calls == []
    for el, fal, expected in [((401, {'detail': {'status': 'missing_permissions', 'message': 'user_read ' + EL_KEY}}),
                               (403, {'detail': 'not permitted'}), 'no_permission'),
                              ((500, {}), RuntimeError('boom ' + FAL_KEY), 'unavailable'),
                              ((200, b'not json'), (200, {'credits': {'current_balance': 'lots'}}), 'unavailable'),
                              ((200, {'character_count': -1, 'character_limit': 5}), (200, {'credits': None}), 'unavailable'),
                              ((200, b'x' * 300_000), (302, {}), 'unavailable')]:
        balances, _ = reader(Secrets(STORED), {EL: el, FAL: fal})
        result = balances()
        assert result['elevenlabs']['state'] == result['fal']['state'] == expected
        assert 'permission' in result['fal']['message'] if expected == 'no_permission' else True
        text = json.dumps(result)
        assert EL_KEY not in text and FAL_KEY not in text and 'user_read' not in text and 'boom' not in text
