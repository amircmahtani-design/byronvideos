"""Unmounted administrator boundary. No auth, storage or network is activated.

Deployment must inject verified admin sessions and an encrypted private store.
The store contract has write-only methods here; keys are never returned to UI.
"""
from dataclasses import dataclass, field
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictStr

from .hosted_control import invoke
from .voices import minimal_catalog

PROVIDERS = frozenset({"elevenlabs", "fal"})


@dataclass(frozen=True)
class AdminSession:
    subject: str
    csrf_token: str = field(repr=False)


class KeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: SecretStr = Field(min_length=16, max_length=512)


class VoiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")


class NamedVoiceInput(VoiceInput):
    name: StrictStr | None = Field(default=None, min_length=1, max_length=120)


def install_admin_routes(app, *, session_resolver=None, private_store=None,
                         connection_tester=None, voice_provider=None, voice_library=None, voice_namer=None,
                         allowed_origin=None, activated=False, native_api_mode="vertex_eu"):
    """Explicit mounting only. Activation requires separate deployment approval.

    session_resolver must verify a real session AND the configured administrator
    identity, never trust a caller-supplied identity/header. Session CSRF tokens
    must be cryptographically random, session-bound and rotated with sessions.
    private_store must scope configuration to this app and encrypt it at rest.
    Test callbacks must use only reviewed read-only endpoints, never synthesis.
    """
    if native_api_mode not in {"vertex_eu", "gemini_developer"}:
        raise ValueError("Explicit supported native API mode required.")
    providers = PROVIDERS | ({"gemini"} if native_api_mode == "gemini_developer" else set())

    async def authorize(request, mutation=False):
        if not activated or session_resolver is None or not allowed_origin:
            raise HTTPException(503, "Administrator setup is not connected yet. Do not paste keys here.")
        origin = urlsplit(allowed_origin)
        if origin.scheme != "https" or origin.netloc == "" or origin.path or origin.query or origin.fragment:
            raise HTTPException(503, "Administrator setup needs review.")
        try:
            session = await invoke(session_resolver, request)
            if (not isinstance(session, AdminSession) or not session.subject
                    or not isinstance(session.csrf_token, str) or not session.csrf_token.isascii()
                    or not 32 <= len(session.csrf_token) <= 512):
                raise ValueError()
        except Exception:
            raise HTTPException(403, "An administrator session is required.") from None
        token = request.headers.get("x-csrf-token", "")
        if mutation and (request.headers.get("origin") != allowed_origin or
                         request.headers.get("sec-fetch-site") == "cross-site" or not token.isascii() or
                         not secrets.compare_digest(token, session.csrf_token)):
            raise HTTPException(403, "Reload this administrator page before trying again.")
        return session

    def provider_name(provider):
        if provider not in providers:
            raise HTTPException(404, "Provider not supported.")

    @app.get("/api/admin/status")
    async def status(request: Request):
        session = await authorize(request)
        if private_store is None:
            raise HTTPException(503, "Private configuration storage is not connected yet.")
        return {"enabled": True, "csrf_token": session.csrf_token, "render_ready": False, "native_api_mode": native_api_mode,
                "message": "Administrator connected. Saving keys does not activate film generation."}

    @app.post("/api/admin/keys/{provider}")
    async def save_key(provider: str, body: KeyInput, request: Request):
        session = await authorize(request, True)
        provider_name(provider)
        key = body.key.get_secret_value()
        if any(c.isspace() for c in key) or not key.isascii():
            raise HTTPException(422, "Paste the API key only, without spaces.")
        if private_store is None:
            raise HTTPException(503, "Private configuration storage is not connected yet.")
        try:
            await invoke(private_store.save_key, session.subject, provider, key)
        except Exception:
            raise HTTPException(503, "Key save could not be confirmed. Nothing was tested or generated.") from None
        return {"saved": True, "tested": False, "render_ready": False}

    @app.post("/api/admin/test/{provider}")
    async def test_connection(provider: str, request: Request):
        session = await authorize(request, True)
        provider_name(provider)
        if connection_tester is None:
            raise HTTPException(503, "A read-only connection test is not configured for this provider.")
        try:
            result = await invoke(connection_tester, session.subject, provider)
            if result is not True:
                raise ValueError()
        except Exception:
            raise HTTPException(503, "Connection could not be verified. Check the saved key and its permissions. No media was generated.") from None
        return {"connected": True, "render_ready": False}

    @app.post("/api/admin/default-voice")
    async def save_voice(body: VoiceInput, request: Request):
        session = await authorize(request, True)
        if private_store is None or voice_provider is None:
            raise HTTPException(503, "Connect ElevenLabs and private configuration storage first.")
        try:
            voices = minimal_catalog(await invoke(voice_provider, session.subject))
        except Exception:
            raise HTTPException(503, "The voice could not be checked. The previous default is unchanged.") from None
        if not any(v["id"] == body.voice_id for v in voices):
            raise HTTPException(422, "This ID is not an accessible saved personal designed voice. Check the ID and connected account.")
        try:
            await invoke(private_store.save_default_voice, session.subject, body.voice_id)
        except Exception:
            raise HTTPException(503, "Default voice save could not be confirmed.") from None
        return {"saved": True, "render_ready": False}

    # Named narrator list. With ElevenLabs connected, names come from ElevenLabs
    # (read-only lookup by ID); in setup-only mode the owner types the same name.
    async def library_call(method, *args, checked=False):
        if voice_library is None:
            raise HTTPException(503, "The voice list is not connected yet.")
        try:
            return {**await invoke(getattr(voice_library, method), *args), "checked_with_elevenlabs": checked}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(503, "The voice list change could not be confirmed. Reload and check the list.") from None

    async def elevenlabs_name(voice_id):
        try:
            name = await invoke(voice_namer, voice_id)
            if not isinstance(name, str) or not name.strip():
                raise ValueError()
            return name
        except Exception:
            raise HTTPException(422, "ElevenLabs did not confirm this Voice ID for the connected account. Check the ID.") from None

    @app.get("/api/admin/voices")
    async def list_named_voices(request: Request):
        await authorize(request)
        return {**await library_call("read"), "names_from_elevenlabs": voice_namer is not None}

    @app.post("/api/admin/voices")
    async def add_named_voice(body: NamedVoiceInput, request: Request):
        await authorize(request, True)
        if voice_namer is not None:
            return await library_call("add", body.voice_id, await elevenlabs_name(body.voice_id), checked=True)
        if body.name is None:
            raise HTTPException(422, "ElevenLabs is not connected yet, so type the voice name exactly as it appears in ElevenLabs.")
        return await library_call("add", body.voice_id, body.name)

    @app.post("/api/admin/voices/refresh-names")
    async def refresh_named_voices(request: Request):
        await authorize(request, True)
        if voice_namer is None:
            raise HTTPException(503, "Connect ElevenLabs first; names are read from ElevenLabs then.")
        library = await library_call("read")
        for voice in library["voices"]:
            library = await library_call("add", voice["id"], await elevenlabs_name(voice["id"]), checked=True)
        return library

    @app.post("/api/admin/voices/remove")
    async def remove_named_voice(body: VoiceInput, request: Request):
        await authorize(request, True)
        return await library_call("remove", body.voice_id)

    @app.post("/api/admin/voices/default")
    async def default_named_voice(body: VoiceInput, request: Request):
        await authorize(request, True)
        return await library_call("set_default", body.voice_id)
