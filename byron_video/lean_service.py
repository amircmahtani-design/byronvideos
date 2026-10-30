"""Owner-only lean film routes matching the existing hosted creator contract.

POST /api/uploads (multipart `images`, Idempotency-Key) → {upload_id, images:[{id, position}]}
POST /api/jobs   JSON {script, images, voice_id, title_seconds} + Idempotency-Key → {id, status}
GET  /api/jobs/{id}, /api/jobs/{id}/video, /api/submissions/{key}, /api/creator-capabilities
Mutations require the owner session, exact Origin and session CSRF token (as admin.py).
"""
from dataclasses import asdict
import re
import time

from fastapi import Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field, StrictInt, StrictStr
from starlette.datastructures import UploadFile

from .elevenlabs_adapter import plan_requests
from .story_limits import STORY_MAX_CHARS, validate_story
from .spoken_text import prepare_story
from .hosted_control import (IdempotencyConflict, JOB_ID, SAFE_ID, PrivateControlMiddleware, Submission, VerifiedImage,
                             digest, invoke, require)
from .hosted_uploads import KEY, MAX_IMAGE_BYTES, batch_fingerprint, prepare_image
from .lean_film import LeanBudgetExceeded, LeanBusy, dollars, estimate_cents, freeze_inputs, narration_profile

EXPIRED = 'The studio setup has expired. Ask the owner to renew it; nothing was started or charged.'


class LeanSubmission(Submission):
    """The hosted submission shape with the studio's generous story length (see story_limits)."""
    script: StrictStr = Field(min_length=1, max_length=STORY_MAX_CHARS)
    title_seconds: StrictInt = Field(default=2, ge=0, le=5)  # Silent opening title card; 0 = none.


def install_lean_routes(app, *, access, owner_id, config, settings, jobs, ledger_factory, budget_reader, uploads,
                        objects, library, starter, check, release_sha256):
    """Mount before the static web assets. Every dependency is explicitly injected."""
    require(callable(check) and callable(starter) and callable(ledger_factory) and callable(budget_reader)
            and isinstance(release_sha256, str) and re.fullmatch(r'[0-9a-f]{64}', release_sha256), 'Explicit lean adapters required.')
    budget = config['budget']
    app.add_middleware(PrivateControlMiddleware, upload_enabled=True)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        if request.url.path not in {'/api/jobs', '/api/uploads'}:
            from fastapi.exception_handlers import request_validation_exception_handler
            return await request_validation_exception_handler(request, error)  # Admin routes unchanged.
        # Never echo the script or upload metadata back.
        message = ('The request contains invalid Unicode. Paste valid text; nothing was submitted.'
                   if any(item.get('type') == 'string_unicode' for item in error.errors()) else
                   'Check the story and four or five numbered paintings. Nothing was submitted.')
        return JSONResponse({'detail': message}, status_code=422)

    async def owner(request):
        try:
            require(await invoke(check) is True, EXPIRED)
        except Exception:
            raise HTTPException(503, EXPIRED) from None
        session = await access.authorize(request)  # 401 without session; 403 on Origin/CSRF mismatch.
        if session.principal.subject != owner_id:
            raise HTTPException(403, 'Only the studio owner can make films here.')
        return session

    def owned(job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise HTTPException(404, 'Film not found.')
        try:
            job = jobs.get(owner_id, job_id)
        except Exception:
            raise HTTPException(503, 'This film needs the owner to check it. Nothing was retried.') from None
        if job is None:
            raise HTTPException(404, 'Film not found.')
        return job

    @app.get('/api/health')
    def health():
        return {'ready': True, 'mode': 'hosted', 'live_release': True, 'lean_generation': True}

    @app.get('/api/creator-capabilities')
    async def capabilities(request: Request):
        session = await owner(request)
        return dict(enabled=True, render_ready=True, mode='hosted', protocol_version=1, csrf_token=session.csrf_token,
                    live_release=True, synthetic_fixture=False, runtime_release_sha256=release_sha256,
                    lean_generation=True, administration_only=False)

    @app.post('/api/uploads', status_code=201)
    async def upload(request: Request):
        session = await owner(request)  # Before parsing any uploaded bytes.
        key = request.headers.get('idempotency-key', '')
        if not KEY.fullmatch(key):
            raise HTTPException(422, 'A stable random Idempotency-Key is required.')
        try:
            async with request.form(max_files=5, max_fields=0) as form:
                files = form.getlist('images')
                if len(files) not in (4, 5) or len(form.multi_items()) != len(files) or not all(isinstance(x, UploadFile) for x in files):
                    raise HTTPException(422, 'Upload four or five ordered paintings only.')
                prepared = [await invoke(prepare_image, await file.read(MAX_IMAGE_BYTES + 1), file.content_type) for file in files]
            fingerprint = batch_fingerprint(prepared)
            try:
                upload_id, records = await invoke(uploads.commit_batch, session.principal, key, fingerprint, tuple(prepared))
                require(isinstance(upload_id, str) and bool(SAFE_ID.fullmatch(upload_id)) and len(records) == len(prepared)
                        and len({r.id for r in records}) == len(records), 'Upload receipt changed.')
                for record, expected in zip(records, prepared):
                    require(isinstance(record, VerifiedImage), 'Upload receipt changed.')
                    record.validate(session.principal, record.id)
                    require((record.sha256, record.byte_count, record.content_type, record.width, record.height)
                            == (expected.sha256, len(expected.data), expected.content_type, expected.width, expected.height), 'Upload receipt changed.')
            except IdempotencyConflict:
                raise HTTPException(409, 'This upload key already belongs to different paintings.') from None
            except Exception:
                raise HTTPException(503, 'Upload could not be confirmed. Do not submit again; ask the owner to check it.') from None
            return dict(upload_id=upload_id, images=[dict(id=r.id, position=i) for i, r in enumerate(records, 1)])
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(422, 'The paintings could not be read. Use four or five still JPEG or PNG files.') from None

    def create(principal, key, body):
        selections = [item.model_dump() for item in body.images]
        if [x['position'] for x in selections] != list(range(1, len(selections) + 1)) or len({x['id'] for x in selections}) != len(selections):
            raise HTTPException(422, 'Use four or five different paintings in their numbered order.')
        request_hash = digest({'script': body.script, 'images': selections, 'voice_id': body.voice_id, 'title_seconds': body.title_seconds})
        try:
            existing = jobs.find(owner_id, key)
        except Exception:
            raise HTTPException(503, 'This submission needs the owner to check it. Nothing was resubmitted.') from None
        if existing is not None:
            if existing.request_sha256 != request_hash:
                raise HTTPException(409, 'This submission key already belongs to a different film.')
            return {'id': existing.id, 'status': existing.status, 'reused': True}
        try:
            validate_story(body.script)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        try:
            text, _ = prepare_story(body.script)  # Title and web links are never read aloud or captioned.
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        try:
            voices = library.read()
        except Exception:
            raise HTTPException(503, 'Saved voices could not be loaded. Please try again.') from None
        voice = next((v for v in voices['voices'] if v['id'] == (body.voice_id or voices['default_voice_id'])), None)
        if voice is None:
            raise HTTPException(422, 'Choose one of the saved narrator voices.')
        images = []
        for selection in selections:
            try:
                record = uploads.resolve(principal, selection['id'])
                require(isinstance(record, VerifiedImage), 'Invalid painting.')
                record.validate(principal, selection['id'])
            except Exception:
                raise HTTPException(404, 'A saved painting could not be found. Please upload the paintings again.') from None
            images.append({**asdict(record), 'position': selection['position']})
        try:
            profile = narration_profile(config, voice['id'])
            requests = plan_requests(text, profile)  # Exact full text; never shortened or rewritten.
        except ValueError:
            raise HTTPException(422, 'The story cannot be narrated as written. Nothing was started.') from None
        try:
            estimate = estimate_cents(requests, len(images), settings, budget)
            remaining = budget_reader()
        except LeanBudgetExceeded as exc:
            raise HTTPException(402, str(exc)) from None
        except Exception:
            raise HTTPException(503, 'The spending limit could not be checked. Nothing was started.') from None
        if estimate > remaining:
            raise HTTPException(402, f'This film would cost up to {dollars(estimate)}, but only {dollars(max(0, remaining))} '
                                     'of the approved budget is left. Nothing was started or charged.')
        build = lambda job_id: freeze_inputs(owner_id=owner_id, job_id=job_id, script=body.script, narrated_text=text, images=images,
            voice=voice, profile=profile, requests=requests, settings=settings, budget=budget, request_sha256=request_hash,
            title_seconds=body.title_seconds)
        try:
            job, created = jobs.reserve(owner_id, key, request_hash, build)
        except IdempotencyConflict:
            raise HTTPException(409, 'This submission key already belongs to a different film.') from None
        except LeanBusy as exc:
            raise HTTPException(423, str(exc)) from None  # A definite refusal: nothing reserved or started.
        except Exception:
            raise HTTPException(503, 'The film could not be saved. Ask the owner to check this reference; do not submit again.') from None
        if not created:
            return {'id': job.id, 'status': job.status, 'reused': True}
        inputs = job.inputs
        try:
            ledger_factory(job.id).fund(inputs['plan_id'], inputs['plan_sha256'], inputs['allocations'])
        except LeanBudgetExceeded as exc:
            try:
                jobs.fail(owner_id, job.id, str(exc))
            finally:
                raise HTTPException(402, str(exc)) from None
        except Exception:
            pass  # Unknown commit: the worker re-funds idempotently before any paid call.
        if jobs.claim_start(owner_id, job.id):
            try:
                check()
                job = jobs.record_start(owner_id, job.id, starter(job.id))
            except Exception:
                job = jobs.start_uncertain(owner_id, job.id)  # Never started a second time automatically.
        return {'id': job.id, 'status': job.status, 'reused': False}

    @app.post('/api/jobs', status_code=202)
    async def submit(request: Request, body: LeanSubmission, idempotency_key: str | None = Header(default=None)):
        session = await owner(request)
        if not isinstance(idempotency_key, str) or not KEY.fullmatch(idempotency_key):
            raise HTTPException(422, 'A stable, random Idempotency-Key is required.')
        return await run_in_threadpool(create, session.principal, idempotency_key, body)

    @app.get('/api/jobs/{job_id}')
    async def status(request: Request, job_id: str):
        await owner(request)
        job = await run_in_threadpool(owned, job_id)
        return job.public(now=time.time())  # A silently stopped film is reported as stopped, not waiting.

    @app.get('/api/submissions/{key}')
    async def recover(request: Request, key: str):
        await owner(request)
        if not KEY.fullmatch(key):
            raise HTTPException(422, 'Use the saved submission reference.')
        try:
            job = await run_in_threadpool(jobs.find, owner_id, key)
        except Exception:
            raise HTTPException(503, 'Submission recovery needs the owner to check it. Nothing was retried.') from None
        if job is None:
            raise HTTPException(404, 'No film is saved for this reference yet. Keep it; do not submit again.')
        return {'id': job.id, 'status': job.status}

    @app.get('/api/jobs/{job_id}/video')
    async def video(request: Request, job_id: str):
        await owner(request)
        job = await run_in_threadpool(owned, job_id)
        if job.status != 'complete':
            raise HTTPException(409, 'This film is not finished yet.')
        ref = job.result['video']
        try:
            stream = await run_in_threadpool(objects.open, owner_id, job.id, ref)  # Bytes and hash verified first.
        except Exception:
            raise HTTPException(503, 'The finished film could not be verified. No download was released.') from None
        def chunks():
            try:
                while chunk := stream.read(1024 * 1024):
                    yield chunk
            finally:
                stream.close()
        # No Content-Length: a chunked stream is not subject to Cloud Run's 32 MiB HTTP/1 response
        # limit, which a full-length film (~66 MB for 3.5 minutes) exceeds.
        return StreamingResponse(chunks(), media_type='video/mp4', headers={
            'Content-Disposition': 'attachment; filename="byron-film.mp4"', 'Cache-Control': 'private, no-store',
            'X-Content-Type-Options': 'nosniff'})


def install_lean_generation(app, *, parts, bindings, settings, transaction_runner=None):
    """Concrete adapters from the signed setup bindings (no new credentials or SDKs)."""
    from .cloud_storage import CloudUploadStore
    from .hosted_control import Principal
    from .hosted_uploads import CreatorAccess, CreatorSession
    from .lean_film import LeanBudget, LeanJobStore, LeanLedger, LeanObjects, LeanStarter, owner_for
    from .worker_binding import HttpxCloudRunTransport
    config = parts.config; target = config['target']
    owner_id = owner_for(config)
    require(parts.owners == [owner_id], 'Lean generation owner differs from the setup owner.')
    dbargs = dict(project_id=target['project_id'], namespace=config['firestore']['namespace'], transaction_runner=transaction_runner)
    def session(request):
        value = parts.admin_session(request)
        return CreatorSession(Principal(value.subject), value.csrf_token)
    budget_view = LeanBudget(bindings.firestore_client, **dbargs)
    install_lean_routes(app, access=CreatorAccess(session, config['identity']['origin']), owner_id=owner_id, config=config,
        settings=settings, jobs=LeanJobStore(bindings.firestore_client, **dbargs),
        ledger_factory=lambda job_id: LeanLedger(bindings.firestore_client, **dbargs, owner_id=owner_id, job_id=job_id, budget=config['budget']),
        budget_reader=lambda: budget_view.remaining_cents(config['budget']),
        uploads=CloudUploadStore(bindings.storage_client, bucket=target['bucket']),
        objects=LeanObjects(bindings.storage_client, bucket=target['bucket']), library=parts.library,
        starter=LeanStarter(target, HttpxCloudRunTransport(bindings.workload_token_reader)),
        check=parts.check, release_sha256=parts.release_sha256)
