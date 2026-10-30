"""When each provider key was last saved and its last 4 characters; never the key.

The service can add secret versions but not read or list them, so the
administrator page uses these records to show that a key is saved.
"""
import re
import time

from .cloud_journal import _FirestoreBoundary
from .hosted_control import require

PROVIDERS = ("elevenlabs", "fal", "gemini")
ENDING = re.compile(r"[A-Za-z0-9_-]{4}")


def ending_of(key):
    tail = key[-4:] if isinstance(key, str) and len(key) >= 16 else ""
    return tail if ENDING.fullmatch(tail) else None


class FirestoreKeyRecords(_FirestoreBoundary):
    def _ref(self):
        return self._doc("settings/saved_keys")

    def read(self):
        value = self._read(self._ref()) or {}
        result = {}
        for p in PROVIDERS:
            row = value.get(p)
            if isinstance(row, dict) and isinstance(row.get("saved_at"), int) and row["saved_at"] > 0:
                ending = row.get("ending")
                result[p] = {"saved_at": row["saved_at"],
                             "ending": ending if isinstance(ending, str) and ENDING.fullmatch(ending) else None}
        return result

    def mark_saved(self, provider, key=None, now=None):
        require(provider in PROVIDERS, "Unknown provider.")
        row = {"saved_at": int(time.time() if now is None else now), "ending": ending_of(key)}
        ref = self._ref()

        def write(tx):
            current = self._read(ref, tx)
            if current is None:
                tx.create(ref, {provider: row})
            else:
                tx.update(ref, {provider: row})
        self._run(write)
        return row
