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


def install_admin_routes(app, *, session_resolver=None, private_store=None,
                         connection_tester=None, voice_provider=None,
                         allowed_origin=None, activated=False):
    """Explicit mounting only. Activation requires separate deployment approval.

    session_resolver must verify a real session AND the configured administrator
    identity, never trust a caller-supplied identity/header. Session CSRF tokens
    must be cryptographically random, session-bound and rotated with sessions.
    private_store must scope configuration to this app and encrypt it at rest.
    Test callbacks must use only reviewed read-only endpoints, never synthesis.
    """
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
        if provider not in PROVIDERS:
            raise HTTPException(404, "Provider not supported.")

    @app.get("/api/admin/status")
    async def status(request: Request):
        session = await authorize(request)
        if private_store is None:
            raise HTTPException(503, "Private configuration storage is not connected yet.")
        return {"enabled": True, "csrf_token": session.csrf_token, "render_ready": False,
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
