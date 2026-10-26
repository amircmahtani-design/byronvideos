"""Loopback-only, single-job prototype API. Not a hosted authentication boundary.

Run ``python -m byron_video.server`` and open http://127.0.0.1:8765.
Never expose this process through a tunnel, reverse proxy, or a public interface.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import importlib.util
import copy
import json
import ipaddress
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import warnings

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

MAX_SCRIPT_CHARS = 4_000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_REQUEST_BYTES = 100 * 1024 * 1024 + 128 * 1024
MAX_IMAGE_PIXELS = 40_000_000
JOB_TTL_SECONDS = 24 * 60 * 60
JOB_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


def _loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LocalOnlyMiddleware:
    """Reject remote clients, DNS rebinding, cross-origin writes and huge bodies.

    Host and peer checks intentionally make proxy/tunnel hosting unsupported.
    This is a local development safeguard, not user authentication.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw_host = headers.get(b"host", b"").decode("latin-1")
        try:
            parsed = urlsplit("http://" + raw_host)
            host_ok = bool(parsed.hostname) and _loopback_host(parsed.hostname)
            host_ok = host_ok and not parsed.username and not parsed.password and not parsed.path
            # Accessing .port also rejects malformed/non-numeric ports.
            port = parsed.port or 80
        except ValueError:
            host_ok, port = False, 0
        client = scope.get("client")
        client_ok = bool(client) and _loopback_host(client[0])
        if not host_ok or not client_ok:
            return await JSONResponse({"detail": "This prototype is available only on this computer."}, 403)(scope, receive, send)
        if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = headers.get(b"origin")
            if origin:
                try:
                    o = urlsplit(origin.decode("latin-1"))
                    expected_scheme = scope.get("scheme", "http")
                    expected_port = 443 if expected_scheme == "https" else 80
                    origin_ok = (o.scheme == expected_scheme and o.hostname == parsed.hostname
                                 and (o.port or expected_port) == port and not o.path
                                 and not o.query and not o.fragment and not o.username)
                except ValueError:
                    origin_ok = False
                if not origin_ok:
                    return await JSONResponse({"detail": "Cross-origin uploads are not allowed."}, 403)(scope, receive, send)
            if headers.get(b"sec-fetch-site", b"") == b"cross-site":
                return await JSONResponse({"detail": "Cross-site uploads are not allowed."}, 403)(scope, receive, send)
        length = headers.get(b"content-length")
        if length is not None:
            try:
                size = int(length)
                if size < 0:
                    raise ValueError
            except ValueError:
                return await JSONResponse({"detail": "Invalid request length."}, 400)(scope, receive, send)
            if size > MAX_REQUEST_BYTES:
                return await JSONResponse({"detail": "Upload is too large. Use at most 20 MB per painting."}, 413)(scope, receive, send)
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "Upload is too large. Use at most 20 MB per painting.")
            return message

        async def private_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ]
            await send(message)

        await self.app(scope, bounded_receive, private_send)


def dependency_status() -> tuple[bool, str]:
    """No model download or inference during a health check."""
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    missing += [name for name in ("kokoro", "torch", "soundfile") if importlib.util.find_spec(name) is None]
    if missing:
        return False, "Local setup needed: " + ", ".join(missing) + "."
    return True, "Local renderer available. The first conversion may download the voice model."


def load_style_snapshot() -> tuple[dict, dict]:
    """Validate the authoritative preset and detach it from future edits."""
    from .style import load_style, style_hash
    snapshot = copy.deepcopy(load_style())
    metadata = {"version": snapshot["style_version"], "hash": style_hash(snapshot),
                "approval_status": snapshot["approval"]["status"]}
    return snapshot, metadata


@dataclass
class Job:
    id: str
    directory: Path
    created: float
    status: str = "queued"
    progress: int = 0
    message: str = "Waiting to begin."
    style_snapshot: dict = field(default_factory=dict)
    style_metadata: dict = field(default_factory=dict)

    def public(self) -> dict:
        result = {"id": self.id, "status": self.status, "progress": self.progress, "message": self.message, "style": copy.deepcopy(self.style_metadata)}
        if self.status == "complete":
            result["download_url"] = f"/api/jobs/{self.id}/video"
        return result


class JobManager:
    def __init__(self, root: Path, renderer: Callable | None = None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="byron-render")
        self.jobs: dict[str, Job] = {}
        self.busy = False
        self.renderer = renderer
        self.cleanup()

    def cleanup(self):
        """Delete only marked, expired jobs under our root; never follow links."""
        cutoff = time.time() - JOB_TTL_SECONDS
        with self.lock:
            active = {j.id for j in self.jobs.values() if j.status in {"queued", "running"}}
            for child in self.root.iterdir():
                if (not JOB_ID_PATTERN.fullmatch(child.name) or child.is_symlink()
                        or not child.is_dir() or child.name in active):
                    continue
                marker = child / ".byron-job"
                if not marker.is_file() or marker.is_symlink():
                    continue
                if marker.stat().st_mtime < cutoff:
                    shutil.rmtree(child)
                    self.jobs.pop(child.name, None)

    def reserve(self, style_snapshot: dict, style_metadata: dict) -> Job:
        self.cleanup()
        with self.lock:
            if self.busy:
                raise HTTPException(429, "A film is already being made. Please wait for it to finish.")
            self.busy = True
            try:
                job_id = secrets.token_hex(16)
                directory = self.root / job_id
                directory.mkdir(mode=0o700)
                (directory / ".byron-job").touch(mode=0o600)
                job = Job(job_id, directory, time.time(),
                          style_snapshot=copy.deepcopy(style_snapshot), style_metadata=copy.deepcopy(style_metadata))
                (directory / "style.json").write_text(json.dumps(job.style_snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                (directory / "job.json").write_text(json.dumps({"id": job_id, "created": job.created,
                                                               "style": job.style_metadata}, indent=2) + "\n", encoding="utf-8")
                self.jobs[job_id] = job
                return job
            except Exception:
                self.busy = False
                raise

    def abandon(self, job: Job):
        with self.lock:
            self.jobs.pop(job.id, None)
            self.busy = False
        shutil.rmtree(job.directory, ignore_errors=True)

    def submit(self, job: Job, script: str, paths: list[Path]):
        self.executor.submit(self._run, job, script, paths)

    def _run(self, job: Job, script: str, paths: list[Path]):
        def progress(percent: int, message: str):
            with self.lock:
                job.progress = max(job.progress, min(99, max(0, int(percent))))
                job.message = str(message)[:250]

        try:
            with self.lock:
                job.status = "running"
                job.message = "Preparing the narration and paintings."
            renderer = self.renderer
            if renderer is None:
                from .engine import render
                renderer = render
            renderer(script, paths, job.directory / "film.mp4", progress=progress,
                     style_snapshot=copy.deepcopy(job.style_snapshot))
            if not (job.directory / "film.mp4").is_file() or (job.directory / "film.mp4").stat().st_size == 0:
                raise RuntimeError("Renderer did not produce a film")
            with self.lock:
                job.status, job.progress, job.message = "complete", 100, "Your film is ready to download."
        except Exception as exc:
            # Log exception class only: error strings may contain private text/paths.
            logger.warning("Local render failed (%s)", type(exc).__name__)
            with self.lock:
                job.status = "failed"
                job.message = (str(exc)[:500] if isinstance(exc, ValueError) else
                               "The film could not be made. Check the local renderer setup and try again. Your original files are unchanged.")
        finally:
            with self.lock:
                self.busy = False

    def find(self, job_id: str) -> Job:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise HTTPException(404, "Film not found.")
        self.cleanup()
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise HTTPException(404, "Film not found or expired. Please convert it again.")
            return job

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)


def _validate_image(path: Path, content_type: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                fmt = image.format
                if fmt not in {"PNG", "JPEG"} or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Only still JPEG and PNG images are supported.")
                if image.width * image.height > MAX_IMAGE_PIXELS or min(image.size) < 64:
                    raise ValueError("Painting dimensions are outside supported limits.")
                if content_type != {"PNG": "image/png", "JPEG": "image/jpeg"}[fmt]:
                    raise ValueError("Painting type does not match its contents.")
                image.verify()
            with Image.open(path) as image:
                image.load()
        return ".png" if fmt == "PNG" else ".jpg"
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning, ValueError) as exc:
        raise HTTPException(422, "A painting is not a valid still JPEG or PNG image. Use images from 64 pixels up to 40 megapixels.") from exc


def create_app(data_dir: Path | None = None, renderer: Callable | None = None,
               script_validator: Callable | None = None, readiness: Callable | None = None,
               style_provider: Callable | None = None) -> FastAPI:
    root = Path(data_dir or os.environ.get("BYRON_DATA_DIR", PROJECT_ROOT / ".private" / "jobs")).resolve()
    web_root = (PROJECT_ROOT / "web").resolve()
    if root == web_root or web_root in root.parents:
        raise ValueError("Private jobs must not be stored inside the public web directory.")
    manager = JobManager(root, renderer)

    @asynccontextmanager
    async def lifespan(app):
        yield
        manager.close()

    app = FastAPI(title="Byron Videos — local prototype", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.manager = manager
    app.add_middleware(LocalOnlyMiddleware)

    def current_style():
        try:
            snapshot, metadata = (style_provider or load_style_snapshot)()
            return copy.deepcopy(snapshot), copy.deepcopy(metadata)
        except Exception as exc:
            logger.warning("Style preset validation failed (%s)", type(exc).__name__)
            raise HTTPException(503, "The locked style preset needs repair. No film will be made with an unverified style.") from exc

    @app.get("/api/health")
    def health():
        ready, message = (readiness or dependency_status)()
        try:
            _, style = current_style()
        except HTTPException as exc:
            return {"ready": False, "mode": "local", "message": exc.detail}
        return {"ready": ready, "mode": "local", "message": message, "style": style}

    @app.post("/api/jobs", status_code=202)
    async def create_job(script: str = Form(...), images: list[UploadFile] = File(...)):
        job = None
        submitted = False
        try:
            ready, _ = (readiness or dependency_status)()
            if not ready:
                raise HTTPException(503, "The local renderer is not ready. Complete the setup in the README first.")
            if not script.strip() or len(script) > MAX_SCRIPT_CHARS:
                raise HTTPException(422, "Paste a script of no more than 4,000 characters.")
            validator = script_validator
            if validator is None:
                from .engine import validate_script
                validator = validate_script
            try:
                validator(script)  # Validate only; preserve the user's exact original string.
            except ValueError as exc:
                raise HTTPException(422, str(exc)[:500]) from exc
            if len(images) not in {4, 5}:
                raise HTTPException(422, "Choose exactly four or five paintings in the order you want them shown.")
            if any(file.content_type not in {"image/jpeg", "image/png"} for file in images):
                raise HTTPException(422, "Use JPEG or PNG paintings only.")
            snapshot, metadata = current_style()
            job = manager.reserve(snapshot, metadata)
            paths = []
            total = 0
            for index, upload in enumerate(images):
                # Never use user filenames as filesystem paths.
                path = job.directory / f"image-{index + 1:02}.upload"
                count = 0
                with path.open("xb") as destination:
                    while chunk := await upload.read(1024 * 1024):
                        count += len(chunk)
                        total += len(chunk)
                        if count > MAX_IMAGE_BYTES or total > 100 * 1024 * 1024:
                            raise HTTPException(413, "Use paintings no larger than 20 MB each.")
                        destination.write(chunk)
                suffix = _validate_image(path, upload.content_type)
                final = path.with_suffix(suffix)
                path.rename(final)
                paths.append(final)
            manager.submit(job, script, paths)
            submitted = True
            return {"id": job.id}
        finally:
            for file in images:
                await file.close()
            if job is not None and not submitted:
                manager.abandon(job)

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = manager.find(job_id)
        with manager.lock:
            return job.public()

    @app.get("/api/jobs/{job_id}/video")
    def job_video(job_id: str):
        job = manager.find(job_id)
        with manager.lock:
            if job.status != "complete":
                raise HTTPException(409, "Your film is not ready yet.")
        return FileResponse(job.directory / "film.mp4", media_type="video/mp4", filename="byron-film.mp4")

    if web_root.is_dir():
        app.mount("/", StaticFiles(directory=web_root, html=True), name="web")
    return app


def main():
    import uvicorn
    # Fixed loopback binding; no environment/CLI option to expose this prototype.
    uvicorn.run(create_app(), host="127.0.0.1", port=8765, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
