"""Optional Google Secret Manager adapter; never instantiated by the public app.

Inject an authenticated SecretManagerServiceClient and existing secret resource
names from private deployment configuration. Does not create secrets, projects,
credentials, grants, billing resources, or log payloads. ADC/workload identity
and narrowly scoped access require separate administrator approval.
"""
import re


class SecretManagerConfig:
    def __init__(self, client, *, admin_subject, resources):
        if set(resources) not in ({"elevenlabs", "fal", "default_voice"}, {"elevenlabs", "fal", "default_voice", "gemini"}):
            raise ValueError("Explicit private configuration resources required.")
        if not admin_subject or any(not re.fullmatch(r"projects/[A-Za-z0-9_-]+/secrets/[A-Za-z0-9_-]+", v) for v in resources.values()):
            raise ValueError("Invalid private configuration.")
        self._client, self._admin = client, admin_subject
        self._resources = dict(resources)

    def _authorize(self, subject):
        if subject != self._admin:
            raise ValueError("Administrator required.")

    def _write(self, subject, name, value):
        self._authorize(subject)
        self._client.add_secret_version(request={"parent": self._resources[name],
                                                "payload": {"data": value.encode("utf-8")}})

    def save_key(self, subject, provider, key):
        if provider not in {"elevenlabs", "fal", "gemini"} or provider not in self._resources:
            raise ValueError("Unknown provider.")
        self._write(subject, provider, key)

    def save_default_voice(self, subject, voice_id):
        from .voices import VOICE_ID
        if not VOICE_ID.fullmatch(voice_id):
            raise ValueError("Invalid voice ID.")
        self._write(subject, "default_voice", voice_id)

    def read_for_server(self, name):
        """Worker-only read; never mount as an HTTP handler or include in status."""
        response = self._client.access_secret_version(request={"name": self._resources[name] + "/versions/latest"})
        return response.payload.data.decode("utf-8")
