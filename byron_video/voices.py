"""Read-only saved personal designed voices; transport is injected server-side.

No credential reader, public route, synthesis or automatic provider connection.
The transport must implement GET with bounded response/timeout and no redirects.
"""
import re
from urllib.parse import urlencode
import json

VOICE_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


class VoiceUnavailable(Exception):
    pass


class ElevenLabsVoiceReader:
    """Explicitly constructed server-only read transport; no paid endpoints.

    Supply a secret reader, never a key from browser requests. httpx is imported
    only on use. No object repr contains credentials and redirects are disabled.
    """
    def __init__(self, key_reader):
        self._key_reader = key_reader

    def _with_get(self, use):
        import httpx
        try:
            key = self._key_reader()
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                def get(url):
                    with client.stream("GET", url, headers={"xi-api-key": key}) as response:
                        if response.status_code != 200:
                            return response.status_code, {}
                        payload = bytearray()
                        for chunk in response.iter_bytes():
                            payload.extend(chunk)
                            if len(payload) > 2_000_000:
                                raise ValueError()
                        return response.status_code, json.loads(payload)
                return use(get)
        except VoiceUnavailable:
            raise
        except Exception:
            raise VoiceUnavailable("Saved voices could not be refreshed. Please try again.") from None

    def __call__(self, _owner):
        return self._with_get(list_voices)

    def name_of(self, voice_id):
        """Read-only: the voice's name exactly as written in ElevenLabs."""
        return self._with_get(lambda get: voice_name(get, voice_id))


def voice_name(get, voice_id):
    if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id):
        raise VoiceUnavailable("Paste the ElevenLabs Voice ID only.")
    status, data = get("https://api.elevenlabs.io/v1/voices/" + voice_id)
    if status in (401, 403):
        raise VoiceUnavailable("ElevenLabs voice access needs administrator attention.")
    name = data.get("name") if status == 200 and isinstance(data, dict) and data.get("voice_id") == voice_id else None
    if not isinstance(name, str) or not name.strip():
        raise VoiceUnavailable("ElevenLabs did not confirm this Voice ID for the connected account.")
    return name[:60]


def list_voices(get):
    voices, seen_tokens, token = {}, set(), None
    for _ in range(100):
        query = dict(voice_type="personal", category="generated", page_size=100,
                     include_total_count="false")
        if token:
            query["next_page_token"] = token
        try:
            status, data = get("https://api.elevenlabs.io/v2/voices?" + urlencode(query))
            if status in (401, 403):
                raise VoiceUnavailable("ElevenLabs voice access needs administrator attention.")
            if status != 200 or not isinstance(data, dict) or not isinstance(data.get("voices"), list):
                raise ValueError()
            for item in data["voices"]:
                # Do not expose samples, account/sharing metadata or arbitrary URLs.
                if item.get("category") != "generated" or item.get("is_owner") is not True:
                    continue
                voice_id, name = item.get("voice_id"), item.get("name")
                if isinstance(voice_id, str) and VOICE_ID.fullmatch(voice_id) and isinstance(name, str) and name.strip():
                    voices[voice_id] = {"id": voice_id, "name": name[:200]}
            if data.get("has_more") is False:
                return sorted(voices.values(), key=lambda v: (v["name"].casefold(), v["id"]))
            token = data.get("next_page_token")
            if not isinstance(token, str) or not token or len(token) > 4096 or token in seen_tokens:
                raise ValueError()
            seen_tokens.add(token)
        except VoiceUnavailable:
            raise
        except Exception:
            raise VoiceUnavailable("Saved voices could not be refreshed. Please try again.") from None
    raise VoiceUnavailable("Saved voices could not be refreshed. Please try again.")


def minimal_catalog(items):
    """Validate injected provider results and discard every unneeded field."""
    result = {}
    for item in items:
        voice_id, name = item["id"], item["name"]
        if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id) or not isinstance(name, str) or not name.strip():
            raise ValueError("Invalid catalog")
        result[voice_id] = {"id": voice_id, "name": name[:200]}
    return list(result.values())
