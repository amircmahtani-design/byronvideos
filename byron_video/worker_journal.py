"""Single-worker durable operation boundary; not an HTTP/authentication service.

No retries after an uncertain provider POST. Restart retains 'may_have_submitted'.
Use only on a private persistent local filesystem; SQLite is not remote backup.
"""
from __future__ import annotations
import json
from pathlib import Path
import re
import sqlite3
from .elevenlabs_adapter import fingerprint


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError('Journal symlinks are forbidden.')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.path.chmod(0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS operation (id TEXT PRIMARY KEY, plan_sha TEXT NOT NULL UNIQUE, reservation INTEGER NOT NULL, state TEXT NOT NULL, receipt TEXT, request_id TEXT UNIQUE)')

    def close(self):
        self.db.close()

    def arm(self, operation_id, request, reservation_cents, job_cap_cents):
        if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', operation_id):
            raise ValueError('Invalid operation ID.')
        if type(reservation_cents) is not int or type(job_cap_cents) is not int or not 0 < reservation_cents <= job_cap_cents:
            raise ValueError('Explicit positive bounded reservation required.')
        digest = fingerprint(request)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            existing_cap = self.db.execute("SELECT value FROM settings WHERE key='job_cap_cents'").fetchone()
            if existing_cap is None:
                self.db.execute("INSERT INTO settings VALUES ('job_cap_cents',?)", (job_cap_cents,))
            elif existing_cap[0] != job_cap_cents:
                raise ValueError('The job budget cannot change after its first reservation.')
            if self.db.execute('SELECT 1 FROM operation WHERE id=?', (operation_id,)).fetchone():
                raise ValueError('Operation already exists; reconcile it, never resend.')
            if self.db.execute('SELECT 1 FROM operation WHERE plan_sha=?', (digest,)).fetchone():
                raise ValueError('This exact request is already reserved; do not submit it under a new operation ID.')
            used = self.db.execute('SELECT coalesce(sum(reservation),0) FROM operation').fetchone()[0]
            if used + reservation_cents > job_cap_cents:
                raise ValueError('Job reservation would exceed its fixed budget.')
            self.db.execute('INSERT INTO operation VALUES (?,?,?,?,NULL,NULL)', (operation_id, digest, reservation_cents, 'may_have_submitted'))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return digest

    def record(self, operation_id, request, receipt):
        if not isinstance(receipt, dict) or not isinstance(receipt.get('request_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,160}', receipt['request_id']):
            raise ValueError('Verified provider request ID required.')
        encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if self.db.execute('SELECT 1 FROM operation WHERE request_id=?', (receipt['request_id'],)).fetchone():
            raise ValueError('Provider request ID already belongs to an operation.')
        changed = self.db.execute("UPDATE operation SET state='received',receipt=?,request_id=? WHERE id=? AND plan_sha=? AND state='may_have_submitted'", (encoded, receipt['request_id'], operation_id, fingerprint(request))).rowcount
        if changed != 1:
            raise ValueError('Operation is absent, changed or already reconciled.')

    def get(self, operation_id):
        row = self.db.execute('SELECT plan_sha,reservation,state,receipt FROM operation WHERE id=?', (operation_id,)).fetchone()
        if row is None:
            return None
        return {'plan_sha256': row[0], 'reservation_cents': row[1], 'state': row[2], 'receipt': json.loads(row[3]) if row[3] else None}


def execute_fixture_once(journal, operation_id, request, reservation_cents, job_cap_cents, fixture_transport):
    """Offline testing only. Production networking is intentionally not implemented."""
    if getattr(fixture_transport, 'synthetic_fixture', False) is not True:
        raise ValueError('Only an explicitly synthetic transport may run in this candidate.')
    journal.arm(operation_id, request, reservation_cents, job_cap_cents)
    receipt = fixture_transport(request)  # If this raises, leave unknown; never retry.
    journal.record(operation_id, request, receipt)
    return receipt
