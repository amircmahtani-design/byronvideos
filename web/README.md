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
- Client limits: 4–5 JPEG/PNG files; 20 MiB per file; 4,000 script characters. **The server must validate actual image bytes, dimensions, MIME, total size and duration independently.**
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

At implementation time, the seven core tests passed and JavaScript parsed successfully. Browser execution was blocked by the environment's Chromium/process sandbox; therefore visual/device QA is not claimed. The typeface names prefer locally installed Playfair Display and Cormorant Garamond, with Georgia fallback. No fonts are fetched.
