# Local creator interface

This is a static **local-renderer client**, not a hosted converter. Serve it from the same origin as the local API. Opening it on a static host does not upload files or start a render: Convert remains disabled without the ready local API.

## API contract

- `GET /api/health` → `{ "ready": true, "mode": "local" }`
- `POST /api/jobs` accepts multipart `script` (exact source) and repeated `images` in the chosen order → `{ "id": "8-or-more-safe-characters" }`
- `GET /api/jobs/{id}` → `{ "status": "running", "progress": 30, "message": "Creating narration…" }`
- Completed status → `{ "status": "complete", "progress": 100, "message": "Ready", "download_url": "/api/jobs/{id}/video" }`
- Failed status → `{ "status": "failed", "message": "A plain-language explanation" }`
- Download URL must be the exact same-origin path above (no query string). `404` for status means the job is no longer available.

Queued/running/rendering/processing and complete/completed/done/succeeded are recognized. Progress is a server-provided number from 0 to 100, not a fabricated timer. A missing numeric value produces an indeterminate progress bar.

## Safety and privacy

- No remote fonts, analytics, trackers, third-party API requests or media fallbacks.
- Original file objects and exact script are submitted; no client-side image alterations, shortening, rewriting or stock image substitution.
- Order changes have labelled buttons rather than drag-only interaction.
- Standard HTTP(S) Markdown links use visible labels for speech/captions; the raw paste is retained privately by the renderer. No URL is fetched.
- Client technical limits: 4–5 JPEG/PNG files; 20 MiB per file; 16,000 script characters and 2,000 words. **The server must validate actual image bytes, dimensions, MIME, total size and the separate 900-second processing ceiling independently.** The usual 90–180 seconds is advisory only, with no minimum word count or automatic voice-speed changes.
- Completed responses can include `duration_seconds` and `duration_advisory`. These are informational beside Download; a duration advisory never hides or disables the completed MP4.
- The browser stores only the active job reference in session storage to recover after reload. Script and paintings are not persisted. File previews use temporary object URLs.
- Duplicate submission is blocked while a job is active. A lost POST connection is not automatically retried: it may already have created a job.
- Poll interruption can be retried without resubmitting. A known failed/expired job unlocks the input controls. Successful completion offers Download and Make another film.
- A published static page is not a private creator. A hosted implementation still needs an authorized backend, authentication, storage retention, upload validation, job ownership checks and operational review. Do not deploy this as if those controls existed.

## Checks

```sh
node --test web/core.test.mjs
node --check web/app.mjs
```

Optional browser interaction suite (requires Playwright and Chromium):

```sh
node web/ui.test.cjs
```

The optional suite uses **mocked APIs and synthetic one-pixel images**. It is not an end-to-end video-generation test. It checks the disabled/unavailable state, exact wording and order, double submission, reload recovery, completion/download, uncertain POST and phone overflow. If available, screenshots are written locally as `web/qa-ipad.png` and `web/qa-phone.png`; do not commit them automatically.

For the capacity correction, ten core tests passed and JavaScript parsed successfully. Browser execution was blocked by the environment's Chromium/process sandbox; therefore visual/device QA is not claimed. The typeface names prefer locally installed Playfair Display and Cormorant Garamond, with Georgia fallback. No fonts are fetched.

## Explicit hosted fixture transport (disabled by default)

`app.mjs` preserves the local multipart flow above. Only when local readiness is
absent does it inspect `GET /api/creator-capabilities`. The hosted branch requires
all of: `enabled: true`, `mode: "hosted"`, `protocol_version: 1`,
`live_release: false`, `synthetic_fixture: true`, and a session CSRF token.
It labels the workspace **no live generation**. A static deployment, missing capability, or a bare live-release claim does not
enable this branch. The separate accepted-runtime branch requires
`live_release: true`, `synthetic_fixture: false` and a 64-hex
`runtime_release_sha256` issued by the authenticated, signed-acceptance-bound
backend assembly (see `hosted/runtime-assembly.md`). No such runtime is enabled
by this source.
This client does not activate infrastructure, authentication or provider calls.

The explicit injected backend contract is:

1. `POST /api/uploads`: original multipart `images` in displayed order, with
   `Idempotency-Key` and `X-CSRF-Token`; returns `upload_id` and exactly four or five
   unique `{id, position}` receipts in numbered order. The backend independently
   validates and persists owner-bound original image bytes.
2. `POST /api/jobs`: JSON `{script, images: [{id, position}], voice_id}`, using
   the **same** per-submission idempotency key and CSRF token. Voice, script and
   file order are frozen before either request. No provider keys enter the client.
3. Poll the existing owner-scoped status and download endpoints. `HOLD` stays
   locked against replacement submissions. Status-only recovery is the default;
   independently terminal queue/capture HOLDs may expose explicit bounded resume. A
   missing hosted job also remains locked pending administrator review.

The transport never retries either POST automatically. An uncertain upload result
never proceeds to job submission. Lost/malformed job receipts and conflicts remain
locked. Session storage retains only opaque pending reference/stage and active job
reference/mode, not script, images, voice selection, CSRF or provider credentials.
Reload with a pending unknown submission stays locked; it does not resubmit.
A definite validation/auth rejection can be corrected and explicitly submitted
again. Before either hosted POST, the opaque reference/stage must round-trip through
session storage. If storage is unavailable, no first upload is sent. If its later
checkpoint fails after upload, no job request is sent and the previous reference
stays locked. A known job ID/mode must round-trip before the pending reference is
cleared; otherwise the known job remains accessible in memory and reload stays locked.
For an uncertain job receipt, Check film status uses only
`GET /api/submissions/{key}` to recover the existing owner-bound job. A missing or
unavailable result remains locked, never permission to resubmit. The opaque
reference is displayed for administrator review. Unknown upload outcomes still
require administrator review.

`node --test web/core.test.mjs` now includes offline hosted transport and recovery checks. These use injected responses and synthetic files, not browser interaction
or real authentication/storage/provider verification. Real backend adapters and
activation remain separately required; this is not a finished live film service.

### Exceptional worker continuation

The hidden-by-default **Resume this film** control appears only when the live
accepted backend confirms a resumable terminal queue/capture HOLD. It keeps the
same job and frozen inputs, requires a separately persisted opaque attempt key,
and never automatically retries. Unknown outcomes remain locked across reload.
See `hosted/resume-control.md` for the three-attempt bound and exclusions.
