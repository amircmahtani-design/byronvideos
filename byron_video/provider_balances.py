"""Owner-only, read-only provider balances (ElevenLabs credits, fal balance).

Uses SEPARATE billing-read keys, never the generation keys. Their Secret Manager names are
derived from the signed `secret_resources` (`<generation secret>-billing`), so the signed
studio config does not change. Each provider is reported on its own; a missing key, a key
without billing permission or a provider error becomes a plain state, never a failure.
No key, provider error body or raw response is ever returned or logged.
"""
import json
import re
import time

SUFFIX = '-billing'
RESOURCE = re.compile(r'projects/[A-Za-z0-9_-]+/secrets/[A-Za-z0-9_-]{1,247}')
ELEVENLABS_URL = 'https://api.elevenlabs.io/v1/user/subscription'
FAL_URL = 'https://api.fal.ai/v1/account/billing?expand=credits'
MAX_BYTES = 200_000
TIMEOUT = 10

MESSAGES = {
    'not_set_up': 'Not set up yet. No billing key is saved for the studio.',
    'no_permission': 'Not available. The billing key lacks billing permission.',
    'unavailable': 'Not available right now. The provider did not answer; try again later.',
}


def billing_resources(secret_resources):
    """`projects/P/secrets/byron-fal` → `projects/P/secrets/byron-fal-billing`, per provider."""
    names = {}
    for provider in ('elevenlabs', 'fal'):
        base = secret_resources[provider]
        if not isinstance(base, str) or not RESOURCE.fullmatch(base):
            raise ValueError('Signed secret resource required.')
        names[provider] = base + SUFFIX
    return names


def _state(state):
    return {'state': state, 'message': MESSAGES[state]}


def _number(value):
    return type(value) in (int, float) and value == value and abs(value) < 1e12


def parse_elevenlabs(data):
    used, limit = data.get('character_count'), data.get('character_limit')
    if not (_number(used) and _number(limit) and used >= 0 and limit >= 0):
        raise ValueError()
    reset = data.get('next_character_count_reset_unix')
    tier = data.get('tier')
    return {'state': 'available', 'credits_used': int(used), 'credits_limit': int(limit),
            'credits_remaining': max(int(limit) - int(used), 0),
            'resets_at': int(reset) if _number(reset) and reset > 0 else None,
            'plan': tier if isinstance(tier, str) and re.fullmatch(r'[A-Za-z0-9 _-]{1,40}', tier) else None}


def parse_fal(data):
    credits = data.get('credits')
    if not isinstance(credits, dict):
        raise ValueError()
    balance = credits.get('current_balance')
    if not _number(balance):
        raise ValueError()
    currency = credits.get('currency')
    return {'state': 'available', 'balance': round(float(balance), 2),
            'currency': currency.upper() if isinstance(currency, str) and re.fullmatch(r'[A-Za-z]{3}', currency) else 'USD'}


class ProviderBalances:
    """Explicit dependencies: a Secret Manager client and (for tests) an httpx-like client factory."""

    def __init__(self, secret_client, resources, *, http_factory=None, clock=time.time):
        if set(resources) != {'elevenlabs', 'fal'} or not all(
                isinstance(v, str) and RESOURCE.fullmatch(v.removesuffix(SUFFIX)) and v.endswith(SUFFIX) for v in resources.values()):
            raise ValueError('Explicit billing secret resources required.')
        self._secrets, self._resources = secret_client, dict(resources)
        self._http_factory, self._clock = http_factory, clock

    def __repr__(self):
        return 'ProviderBalances()'

    def _key(self, provider):
        try:
            response = self._secrets.access_secret_version(
                request={'name': self._resources[provider] + '/versions/latest'}, retry=None, timeout=TIMEOUT)
            key = response.payload.data.decode('utf-8').strip()
        except Exception:
            return None  # Missing secret, no version, or no access for the service account.
        return key if key and key.isascii() and not any(c.isspace() for c in key) and len(key) <= 512 else None

    def _client(self):
        if self._http_factory is not None:
            return self._http_factory()
        import httpx
        return httpx.Client(timeout=TIMEOUT, follow_redirects=False)

    def _get(self, url, headers, parse):
        try:
            with self._client() as client:
                with client.stream('GET', url, headers={**headers, 'Accept': 'application/json'}) as response:
                    if response.status_code in (401, 403):
                        return _state('no_permission')
                    if response.status_code != 200:
                        return _state('unavailable')
                    payload = bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload) > MAX_BYTES:
                            return _state('unavailable')
            data = json.loads(payload)
            if not isinstance(data, dict):
                return _state('unavailable')
            return parse(data)
        except Exception:
            return _state('unavailable')

    def elevenlabs(self):
        key = self._key('elevenlabs')
        if key is None:
            return _state('not_set_up')
        return self._get(ELEVENLABS_URL, {'xi-api-key': key}, parse_elevenlabs)

    def fal(self):
        key = self._key('fal')
        if key is None:
            return _state('not_set_up')
        return self._get(FAL_URL, {'Authorization': 'Key ' + key}, parse_fal)

    def __call__(self):
        return {'elevenlabs': self.elevenlabs(), 'fal': self.fal(), 'checked_at': int(self._clock())}
