"""Owner-managed narrator list: ElevenLabs voice ID plus a friendly name.

Voice IDs are identifiers, not credentials, so they live in the dedicated
control database. The administrator adds, renames, removes and picks the
default; creators only ever see the names. No provider request is made here;
accessibility is checked separately when ElevenLabs is connected.
"""
import re

from .cloud_journal import _FirestoreBoundary
from .hosted_control import require
from .voices import VOICE_ID

NAME = re.compile(r"[^\x00-\x1f\x7f<>]{1,60}")
MAX_VOICES = 32
EMPTY = {"voices": [], "default_voice_id": None}


def clean_name(name):
    name = " ".join(name.split()) if isinstance(name, str) else ""
    require(bool(NAME.fullmatch(name)), "Use a short friendly name (up to 60 characters).")
    return name


def validate_library(value):
    """Return a normalized copy; reject anything malformed instead of guessing."""
    if value is None:
        return {"voices": [], "default_voice_id": None}
    require(isinstance(value, dict) and set(value) == {"voices", "default_voice_id"}, "Voice list needs review.")
    voices = value["voices"]
    require(isinstance(voices, list) and len(voices) <= MAX_VOICES, "Voice list needs review.")
    result, ids, names = [], set(), set()
    for item in voices:
        require(isinstance(item, dict) and set(item) == {"id", "name"}, "Voice list needs review.")
        voice_id, name = item["id"], clean_name(item["name"])
        require(isinstance(voice_id, str) and bool(VOICE_ID.fullmatch(voice_id)), "Voice list needs review.")
        require(voice_id not in ids and name.casefold() not in names, "Voice list needs review.")
        ids.add(voice_id); names.add(name.casefold())
        result.append({"id": voice_id, "name": name})
    default = value["default_voice_id"]
    require(default is None or default in ids, "Voice list needs review.")
    require(default is not None or not result, "Voice list needs review.")
    return {"voices": result, "default_voice_id": default}


class FirestoreVoiceLibrary(_FirestoreBoundary):
    """One small document; every change is a read-before-write transaction."""

    def _ref(self):
        return self._doc("voices/library")

    def read(self):
        return validate_library(self._read(self._ref()))

    def _change(self, edit):
        ref = self._ref()

        def apply(tx):
            current = self._read(ref, tx)
            library = edit(validate_library(current))
            library = validate_library(library)
            if current is None:
                tx.create(ref, library)
            else:
                tx.update(ref, library)
            return library
        return self._run(apply)

    def add(self, voice_id, name):
        """Add a voice, or rename it if the ID is already listed."""
        require(isinstance(voice_id, str) and bool(VOICE_ID.fullmatch(voice_id)), "Paste the ElevenLabs Voice ID only.")
        name = clean_name(name)

        def edit(library):
            voices = [v for v in library["voices"] if v["id"] != voice_id]
            require(all(v["name"].casefold() != name.casefold() for v in voices), "Another voice already uses this name.")
            require(len(voices) < MAX_VOICES, "The voice list is full. Remove a voice first.")
            position = next((i for i, v in enumerate(library["voices"]) if v["id"] == voice_id), len(voices))
            voices.insert(position, {"id": voice_id, "name": name})
            return {"voices": voices, "default_voice_id": library["default_voice_id"] or voice_id}
        return self._change(edit)

    def remove(self, voice_id):
        def edit(library):
            require(any(v["id"] == voice_id for v in library["voices"]), "That voice is not in the list.")
            voices = [v for v in library["voices"] if v["id"] != voice_id]
            default = library["default_voice_id"]
            if default == voice_id:
                default = voices[0]["id"] if voices else None
            return {"voices": voices, "default_voice_id": default}
        return self._change(edit)

    def set_default(self, voice_id):
        def edit(library):
            require(any(v["id"] == voice_id for v in library["voices"]), "That voice is not in the list.")
            return {**library, "default_voice_id": voice_id}
        return self._change(edit)
