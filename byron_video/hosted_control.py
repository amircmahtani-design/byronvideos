"""Inactive, provider-neutral control API boundary; no live service is enabled.

There is intentionally no module-level app, CLI, credential reader, upload grant,
provider client, router installation, or production-readiness switch. The factory
is useful for offline tests. Its defaults deny authentication and dispatch.

Future client contract (NOT today's multipart/local-only frontend contract):
  POST /api/jobs JSON {script, images: [{id, position}, ...]} and Idempotency-Key
  GET  /api/jobs/{id}                 owner-scoped state, never private input
  GET  /api/jobs/{id}/video           owner-scoped verified MP4 stream only

Image IDs must already identify server-verified, private, owner-bound uploads.
A caller cannot supply filenames, storage paths, hashes, provider options, style,
or a completion claim. No upload/storage/authentication implementation is implied.

The durable-store contract requires atomic idempotency reservation, immutable
snapshots, and a committed HOLD checkpoint BEFORE the dispatcher is invoked.
Unknown dispatch outcomes are never automatically retried, including after a
restart. A real deployment must supply a reviewed shared transactional store;
SQLiteFixtureStore is an explicitly local single-machine test implementation,
not a hosted Firestore adapter or cloud-durability claim.

Completion is a trusted worker reconciliation function, not an HTTP endpoint.
Its injected verifier must actually decode/check the immutable MP4 and establish
job/snapshot/execution ownership. Download verifies that proof again and hashes
all bytes into a private temporary spool before returning any bytes to the user.
Mock verifier tests prove the boundary contract, not real media validation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import inspect
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import threading
from typing import Callable, Protocol

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from starlette.background import BackgroundTask

from .engine import MAX_SCRIPT_CHARS, validate_script
from .hosted_contract import freeze_job
from .elevenlabs_adapter import validate_profile

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
JOB_ID = re.compile(r"^[a-f0-9]{32}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
MAX_RESULT_BYTES = 1024 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_CONTROL_BODY_BYTES = 128 * 1024
DENIED_PLAN_KEYS = {"api_key", "apikey", "secret", "password", "token", "authorization", "credentials"}


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def require(condition, message="Invalid trusted handoff."):
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class Principal:
    """An opaque, stable subject from a separately verified authentication layer."""
    subject: str

    def __post_init__(self):
        require(isinstance(self.subject, str) and bool(SAFE_ID.fullmatch(self.subject)), "Invalid authenticated subject.")


@dataclass(frozen=True)
class VerifiedImage:
    """Trusted upload resolver result; never constructed from browser metadata.

    storage_id/version are opaque handles to an immutable private object. An
    adapter resolves them to its own bucket/object/generation; no path is used.
    """
    id: str
    owner_id: str
    sha256: str
    byte_count: int
    width: int
    height: int
    content_type: str
    storage_id: str
    storage_version: str
    original_bytes_verified: bool = False

    def validate(self, principal: Principal, selected_id: str):
        require(self.id == selected_id and self.owner_id == principal.subject, "Private image not found.")
        require(bool(SAFE_ID.fullmatch(self.id)) and bool(SAFE_ID.fullmatch(self.storage_id)))
        require(isinstance(self.storage_version, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.storage_version)))
        require(isinstance(self.sha256, str) and bool(SHA256.fullmatch(self.sha256)))
        require(type(self.byte_count) is int and 0 < self.byte_count <= MAX_IMAGE_BYTES)
        require(type(self.width) is int and type(self.height) is int and min(self.width, self.height) >= 64
                and self.width * self.height <= 40_000_000)
        require(self.content_type in {"image/png", "image/jpeg"} and self.original_bytes_verified is True)


@dataclass(frozen=True)
class DispatchReceipt:
    job_id: str
    snapshot_sha256: str
    execution_id: str

    def validate(self, job):
        require(self.job_id == job.id and self.snapshot_sha256 == job.snapshot_sha256)
        require(isinstance(self.execution_id, str) and bool(SAFE_ID.fullmatch(self.execution_id)))


@dataclass(frozen=True)
class VerifiedArtifact:
    """Result of an injected, trusted full media verifier, not a client assertion."""
    artifact_id: str
    job_id: str
    owner_id: str
    snapshot_sha256: str
    execution_id: str
    sha256: str
    byte_count: int
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    audio_codec: str
    fps: int
    source_order: tuple[str, ...]
    narrated_text_sha256: str
    caption_text_sha256: str
    full_decode_verified: bool = False
    verification_method: str = ""

    def __post_init__(self):
        if isinstance(self.source_order, (list, tuple)):
            object.__setattr__(self, "source_order", tuple(self.source_order))

    def validate(self, job):
        require(job.receipt is not None, "No verified dispatch receipt exists.")
        require(self.job_id == job.id and self.owner_id == job.owner_id
                and self.snapshot_sha256 == job.snapshot_sha256
                and self.execution_id == job.receipt.execution_id, "Artifact does not belong to this job.")
        require(isinstance(self.artifact_id, str) and bool(SAFE_ID.fullmatch(self.artifact_id)))
        require(isinstance(self.sha256, str) and bool(SHA256.fullmatch(self.sha256)))
        require(type(self.byte_count) is int and 0 < self.byte_count <= MAX_RESULT_BYTES)
        require(type(self.duration_seconds) in (int, float) and math.isfinite(self.duration_seconds)
                and 0 < self.duration_seconds <= 900)
        require(type(self.width) is int and type(self.height) is int and type(self.fps) is int
                and (self.width, self.height, self.fps) == (1080, 1920, 24),
                "Only the frozen portrait 1080×1920/24fps output is accepted.")
        snapshot = job.snapshot
        require(isinstance(self.source_order, tuple) and self.source_order == tuple(snapshot["input_order"]),
                "The verified source order differs from the frozen numbered order.")
        require(self.narrated_text_sha256 == snapshot["narrated_text_sha256"]
                and self.caption_text_sha256 == snapshot["narrated_text_sha256"],
                "Narration/caption verification must bind the exact frozen text.")
        require(self.video_codec == "h264" and self.audio_codec == "aac")
        require(self.full_decode_verified is True and self.verification_method == "ffmpeg-full-decode-v1",
                "A full media decode verification is required.")


@dataclass(frozen=True)
class JobRecord:
    id: str
    owner_id: str
    idempotency_key: str
    request_sha256: str
    snapshot_json: str
    snapshot_sha256: str
    state: str = "HOLD"
    receipt_json: str | None = None
    artifact_json: str | None = None

    @property
    def snapshot(self):
        result = json.loads(self.snapshot_json)
        require(digest(result) == self.snapshot_sha256, "Stored snapshot failed integrity validation.")
        return result

    @property
    def receipt(self):
        return DispatchReceipt(**json.loads(self.receipt_json)) if self.receipt_json else None

    @property
    def artifact(self):
        return VerifiedArtifact(**json.loads(self.artifact_json)) if self.artifact_json else None

    def public(self):
        # No input text, image IDs/storage references, execution IDs or owner ID.
        messages = {"HOLD": "Submission needs review. It will not be retried automatically.",
                    "queued": "The render task was accepted.", "running": "The render task is running.",
                    "complete": "Your verified film is ready to download."}
        require(self.state in messages, "Invalid stored job state.")
        result = {"id": self.id, "status": self.state, "progress": 100 if self.state == "complete" else 0,
                  "message": messages[self.state], "snapshot_sha256": self.snapshot_sha256,
                  "mode": "hosted_setup", "live_release": False}
        if self.state == "complete":
            require(self.artifact is not None, "Completion proof is missing.")
            self.artifact.validate(self)
            result["download_url"] = f"/api/jobs/{self.id}/video"
            result["duration_seconds"] = self.artifact.duration_seconds
        return result


class IdempotencyConflict(ValueError):
    pass


class DurableJobStore(Protocol):
    """Production implementation MUST preserve these atomic/immutable semantics."""
    def find_request(self, owner_id: str, key: str, request_sha256: str) -> JobRecord | None: ...
    def reserve(self, owner_id: str, key: str, request_sha256: str, snapshot: dict) -> tuple[JobRecord, bool]: ...
    def get(self, owner_id: str, job_id: str) -> JobRecord | None: ...
    def record_dispatch(self, owner_id: str, job_id: str, receipt: DispatchReceipt) -> JobRecord: ...
    def record_started(self, owner_id: str, job_id: str, execution_id: str) -> JobRecord: ...
    def record_verified_completion(self, owner_id: str, job_id: str, proof: VerifiedArtifact) -> JobRecord: ...


class SQLiteFixtureStore:
    """Private local test store only; not hosted shared storage or a cloud adapter."""
    def __init__(self, path: Path):
        self.path = Path(path)
        for candidate in (self.path, *self.path.parents):
            require(not candidate.is_symlink(), "Fixture database and all parents must not be symlinks.")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self.path.chmod(0o600)
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS control_job (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
            request_sha256 TEXT NOT NULL, snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
            state TEXT NOT NULL, receipt_json TEXT, artifact_json TEXT,
            UNIQUE(owner_id, idempotency_key))""")
        # SQLite's transactional unique index also rejects duplicate accepted
        # executions across owners and independent connections, including on
        # reopening an inconsistent older fixture database (fail closed).
        self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS control_execution_unique
            ON control_job(json_extract(receipt_json, '$.execution_id'))
            WHERE receipt_json IS NOT NULL""")

    def close(self):
        self.db.close()

    def _row(self, sql, values):
        row = self.db.execute(sql, values).fetchone()
        return JobRecord(*row) if row else None

    def find_request(self, owner_id, key, request_sha256):
        with self.lock:
            job = self._row("SELECT * FROM control_job WHERE owner_id=? AND idempotency_key=?", (owner_id, key))
            if job is not None and job.request_sha256 != request_sha256:
                raise IdempotencyConflict("This submission key already belongs to different input.")
            return job

    def get(self, owner_id, job_id):
        with self.lock:
            return self._row("SELECT * FROM control_job WHERE owner_id=? AND id=?", (owner_id, job_id))

    def reserve(self, owner_id, key, request_sha256, snapshot):
        encoded, snapshot_hash = canonical(snapshot), digest(snapshot)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = self.find_request(owner_id, key, request_sha256)
                if existing:
                    self.db.execute("COMMIT")
                    return existing, False
                job = JobRecord(secrets.token_hex(16), owner_id, key, request_sha256, encoded, snapshot_hash)
                # HOLD means "may have been submitted". Persist before any call.
                self.db.execute("INSERT INTO control_job VALUES (?,?,?,?,?,?,?,?,?)", tuple(asdict(job).values()))
                self.db.execute("COMMIT")
                return job, True
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def record_dispatch(self, owner_id, job_id, receipt):
        with self.lock:
            job = self.get(owner_id, job_id)
            require(job is not None and job.state == "HOLD" and job.receipt is None)
            require(isinstance(receipt, DispatchReceipt))
            receipt.validate(job)
            try:
                count = self.db.execute("UPDATE control_job SET state='queued',receipt_json=? WHERE id=? AND owner_id=? AND state='HOLD' AND receipt_json IS NULL",
                                        (canonical(asdict(receipt)), job_id, owner_id)).rowcount
            except sqlite3.IntegrityError:
                raise ValueError("Backend execution is already attached to a job.") from None
            require(count == 1, "Dispatch receipt was already reconciled.")
            return self.get(owner_id, job_id)

    def record_started(self, owner_id, job_id, execution_id):
        with self.lock:
            job = self.get(owner_id, job_id)
            require(job is not None and job.receipt is not None and job.receipt.execution_id == execution_id)
            count = self.db.execute("UPDATE control_job SET state='running' WHERE id=? AND owner_id=? AND state='queued'",
                                    (job_id, owner_id)).rowcount
            require(count == 1, "Task is not queued.")
            return self.get(owner_id, job_id)

    def record_verified_completion(self, owner_id, job_id, proof):
        with self.lock:
            job = self.get(owner_id, job_id)
            require(job is not None and job.state in {"queued", "running"})
            require(isinstance(proof, VerifiedArtifact))
            proof.validate(job)
            count = self.db.execute("UPDATE control_job SET state='complete',artifact_json=? WHERE id=? AND owner_id=? AND state IN ('queued','running')",
                                    (canonical(asdict(proof)), job_id, owner_id)).rowcount
            require(count == 1, "Task is not awaiting completion verification.")
            return self.get(owner_id, job_id)


class ImageSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(pattern=SAFE_ID.pattern, max_length=128)
    position: StrictInt = Field(ge=1, le=5)


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: StrictStr = Field(min_length=1, max_length=MAX_SCRIPT_CHARS)
    images: list[ImageSelection] = Field(min_length=4, max_length=5)


def validate_private_plan(value):
    require(isinstance(value, dict), "A trusted immutable processing plan is required.")
    def visit(item):
        if isinstance(item, dict):
            require(all(isinstance(k, str) and k.lower() not in DENIED_PLAN_KEYS for k in item),
                    "Credentials must never be copied into a job snapshot.")
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)
    # Detach nested objects and reject NaN/non-JSON objects before reservation.
    frozen = json.loads(canonical(value))
    require(set(frozen) == {"schema_version", "status", "visual_style_sha256", "narration_profile",
                            "motion_policy_sha256", "output"}, "The complete fixed processing plan is required.")
    require(type(frozen["schema_version"]) is int and frozen["schema_version"] == 1
            and frozen["status"] == "DRAFT_NOT_ACTIVATED", "This is an inactive draft plan only.")
    for name in ("visual_style_sha256", "motion_policy_sha256"):
        require(isinstance(frozen[name], str) and bool(SHA256.fullmatch(frozen[name])),
                "Pinned visual-style and motion-policy fingerprints are required.")
    frozen["narration_profile"] = validate_profile(frozen["narration_profile"])
    require(frozen["output"] == {"width": 1080, "height": 1920, "fps": 24}
            and all(type(v) is int for v in frozen["output"].values()), "Portrait output settings must be fixed.")
    return frozen


async def invoke(function, *args):
    if inspect.iscoroutinefunction(function):
        return await function(*args)
    result = await run_in_threadpool(function, *args)
    return await result if inspect.isawaitable(result) else result


async def reconcile_verified_completion(store: DurableJobStore, owner: Principal, job_id: str,
                                        artifact_id: str, verifier: Callable) -> JobRecord:
    """Internal trusted worker call only. Never expose as a browser completion API."""
    require(isinstance(owner, Principal) and bool(JOB_ID.fullmatch(job_id)))
    require(isinstance(artifact_id, str) and bool(SAFE_ID.fullmatch(artifact_id)))
    job = store.get(owner.subject, job_id)
    require(job is not None and job.state in {"queued", "running"}, "No accepted owner-bound task exists.")
    proof = await invoke(verifier, owner, job, artifact_id)
    require(isinstance(proof, VerifiedArtifact), "A trusted verification result is required.")
    proof.validate(job)
    require(proof.artifact_id == artifact_id, "Artifact identity changed during verification.")
    return store.record_verified_completion(owner.subject, job_id, proof)


def _verified_spool(reader: Callable, owner: Principal, job: JobRecord, proof: VerifiedArtifact):
    """Authenticate all bytes before response headers; never accept a disk path."""
    source = reader(owner, job, proof)
    if not callable(getattr(source, "read", None)) or not callable(getattr(source, "close", None)):
        close = getattr(source, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        raise ValueError("Artifact reader must return a binary stream.")
    spool = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    count, sha = 0, hashlib.sha256()
    try:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            require(isinstance(chunk, bytes), "Artifact stream is not binary.")
            require(len(chunk) <= 1024 * 1024, "Artifact reader exceeded its requested chunk bound.")
            count += len(chunk)
            require(count <= min(MAX_RESULT_BYTES, proof.byte_count), "Artifact size changed.")
            sha.update(chunk)
            spool.write(chunk)
        require(count == proof.byte_count and sha.hexdigest() == proof.sha256, "Artifact integrity changed.")
        spool.seek(0)
        return spool
    except BaseException:
        spool.close()
        raise
    finally:
        try:
            source.close()
        except Exception:
            spool.close()
            raise ValueError("Artifact stream could not be safely closed.") from None


class PrivateControlMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        consumed = 0
        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > MAX_CONTROL_BODY_BYTES:
                    raise HTTPException(413, "Control request is too large. Upload images through the reviewed private upload boundary.")
            return message
        async def private_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer")]
            await send(message)
        await self.app(scope, limited_receive, private_send)


def create_app(*, principal_resolver: Callable | None = None, store: DurableJobStore | None = None,
               dispatcher: Callable | None = None, upload_resolver: Callable | None = None,
               plan_provider: Callable | None = None, artifact_verifier: Callable | None = None,
               artifact_reader: Callable | None = None) -> FastAPI:
    """Construct an unmounted inactive boundary. No injected dependency is trusted
    from request headers/JSON automatically. The real auth adapter must verify
    credentials AND enforce its cookie/Origin/CSRF policy before returning a
    Principal. A principal resolver is not an approval to activate the service.
    """
    app = FastAPI(title="Byron hosted setup boundary — inactive", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(PrivateControlMiddleware)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, error):
        # Do not echo scripts, metadata, or malformed Unicode from Pydantic's
        # raw input field into errors (which can itself fail JSON encoding).
        message = ("The request contains invalid Unicode. Paste valid text; nothing was submitted."
                   if any(item.get("type") == "string_unicode" for item in error.errors()) else
                   "Check the script and four or five numbered private painting IDs. Extra paths, metadata and settings are not accepted.")
        return JSONResponse({"detail": message}, status_code=422)

    async def principal(request):
        if principal_resolver is None:
            raise HTTPException(401, "Authentication is not configured.")
        try:
            value = await invoke(principal_resolver, request)
            if not isinstance(value, Principal):
                raise ValueError("No verified principal")
            return value
        except Exception:
            raise HTTPException(401, "A verified creator session is required.") from None

    def owned(owner, job_id):
        if store is None:
            raise HTTPException(503, "The durable control store is not configured.")
        if not JOB_ID.fullmatch(job_id):
            raise HTTPException(404, "Film not found.")
        job = store.get(owner.subject, job_id)
        if job is None or job.owner_id != owner.subject or job.id != job_id:
            raise HTTPException(404, "Film not found.")
        try:
            job.snapshot  # A corrupt immutable snapshot is not silently accepted.
        except Exception:
            raise HTTPException(503, "Stored film verification needs review.") from None
        return job

    @app.get("/api/health")
    def health():
        return {"ready": False, "mode": "hosted_setup", "live_release": False,
                "message": "Inactive control boundary. Authentication, durable hosting and live dispatch are not activated."}

    @app.post("/api/jobs", status_code=202)
    async def submit(request: Request, body: Submission, idempotency_key: str | None = Header(default=None)):
        owner = await principal(request)
        if store is None or dispatcher is None or upload_resolver is None or plan_provider is None:
            raise HTTPException(503, "Hosted conversion is not configured.")
        if getattr(dispatcher, "synthetic_fixture", False) is not True:
            raise HTTPException(503, "Live dispatch is not activated in this candidate.")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key):
            raise HTTPException(422, "A stable, random Idempotency-Key is required.")
        try:
            body.script.encode("utf-8")
        except UnicodeEncodeError:
            raise HTTPException(422, "The script contains invalid Unicode. Paste valid text; nothing was submitted.") from None
        try:
            validate_script(body.script)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        selections = [item.model_dump() for item in body.images]
        if [item["position"] for item in selections] != list(range(1, len(selections) + 1)) or len({x["id"] for x in selections}) != len(selections):
            raise HTTPException(422, "Use four or five unique paintings in explicit numbered order.")
        request_hash = digest({"script": body.script, "images": selections})
        try:
            existing = store.find_request(owner.subject, idempotency_key, request_hash)
            if existing:
                require(existing.owner_id == owner.subject and existing.idempotency_key == idempotency_key
                        and existing.request_sha256 == request_hash and bool(JOB_ID.fullmatch(existing.id)))
                existing.snapshot
                return {"id": existing.id, "status": existing.state, "reused": True}
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:
            raise HTTPException(503, "Stored submission verification needs review. Nothing was resubmitted.") from None
        verified = []
        for selection in selections:
            try:
                image = await invoke(upload_resolver, owner, selection["id"])
                require(isinstance(image, VerifiedImage))
                image.validate(owner, selection["id"])
                verified.append({**asdict(image), "position": selection["position"]})
            except Exception:
                raise HTTPException(404, "A verified private painting could not be found for this creator.") from None
        try:
            plan = validate_private_plan(await invoke(plan_provider, owner))
        except Exception:
            raise HTTPException(503, "The trusted processing plan is not ready.") from None
        try:
            narration_contract = freeze_job(body.script,
                [{"id": item["id"], "position": item["position"], "sha256": item["sha256"]} for item in verified],
                plan["narration_profile"], plan["visual_style_sha256"])
        except ValueError:
            raise HTTPException(422, "The exact narration cannot be safely planned with the fixed profile. Nothing was submitted.") from None
        spoken = narration_contract["narrated_text"]
        snapshot = {"schema_version": 1, "raw_script": body.script,
                    "raw_script_sha256": hashlib.sha256(body.script.encode()).hexdigest(),
                    "narrated_text": spoken, "narrated_text_sha256": hashlib.sha256(spoken.encode()).hexdigest(),
                    "images": verified, "input_order": [x["id"] for x in verified],
                    "processing_plan": plan, "processing_plan_sha256": digest(plan),
                    "narration_contract": narration_contract,
                    "publication": "private_no_automatic_publish", "live_release": False}
        try:
            job, created = store.reserve(owner.subject, idempotency_key, request_hash, snapshot)
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from None
        if created:
            handoff = {"job_id": job.id, "owner_id": owner.subject,
                       "idempotency_sha256": hashlib.sha256(idempotency_key.encode()).hexdigest(),
                       "snapshot_sha256": job.snapshot_sha256, "frozen_job": job.snapshot}
            try:
                receipt = await invoke(dispatcher, handoff)
                require(isinstance(receipt, DispatchReceipt))
                receipt.validate(job)
                job = store.record_dispatch(owner.subject, job.id, receipt)
            except Exception:
                # Checkpoint already durable: timeout, malformed receipt, or failed
                # receipt persistence all remain uncertain; never resubmit here.
                job = store.get(owner.subject, job.id)
        return {"id": job.id, "status": job.state, "reused": not created}

    @app.get("/api/jobs/{job_id}")
    async def status(request: Request, job_id: str):
        owner = await principal(request)
        try:
            result = owned(owner, job_id).public()
            if result["status"] == "complete" and (artifact_verifier is None or artifact_reader is None):
                result.pop("download_url", None)
                result["message"] = "The film is verified, but the private download service is not configured."
            return result
        except (ValueError, TypeError, KeyError):
            raise HTTPException(503, "Stored film verification needs review.") from None

    @app.get("/api/jobs/{job_id}/video")
    async def video(request: Request, job_id: str):
        owner = await principal(request)
        job = owned(owner, job_id)
        try:
            expected = job.artifact
        except (ValueError, TypeError, KeyError):
            raise HTTPException(503, "Stored film verification needs review.") from None
        if job.state != "complete" or expected is None:
            raise HTTPException(409, "No verified finished film is available.")
        if artifact_verifier is None or artifact_reader is None:
            raise HTTPException(503, "Verified private downloads are not configured.")
        try:
            expected.validate(job)
            proof = await invoke(artifact_verifier, owner, job, expected.artifact_id)
            require(isinstance(proof, VerifiedArtifact) and proof == expected, "Artifact verification changed.")
            proof.validate(job)
            spool = await run_in_threadpool(_verified_spool, artifact_reader, owner, job, proof)
        except Exception:
            raise HTTPException(503, "The finished film could not be verified. No download was released.") from None
        def chunks():
            try:
                while chunk := spool.read(1024 * 1024):
                    yield chunk
            finally:
                spool.close()
        return StreamingResponse(chunks(), media_type="video/mp4",
                                 headers={"Content-Disposition": 'attachment; filename="byron-film.mp4"',
                                          "Content-Length": str(proof.byte_count)},
                                 background=BackgroundTask(spool.close))

    return app
