# Byron Videos

Bad, Mad & Dangerous — a local-first video essay prototype.

**Status: working local prototype, not a deployed iPad service. No visual style or
voice has been approved.** The static page alone cannot render a film on Netlify.

## The intended experience

Paste the complete narration, choose **four or five PNG/JPEG paintings in order**,
press **Convert**, then download a narrated MP4. No timeline, scene editor, stock
search, image regeneration or script rewriting. All paintings remain fully visible;
gentle motion happens inside a charcoal matte, with captions beneath the artwork.

The provisional voice is Kokoro's British male `bm_george`. Its tone, pronunciation
and suitability still require a human listening review. Captions use the exact
source wording and Kokoro's token-duration predictions, not an independent speech
alignment check. Unalignable or omitted words fail the job instead of being hidden.

## Run locally

Requires Python 3.12, FFmpeg/FFprobe with libx264, and roughly 2 GB free disk space
for dependencies/model cache. The first setup downloads open model files; later
rendering can use the cache. No API key, paid service, account or recurring service
charge is required for the local prototype. Computer time and electricity still
have a cost. These commands are for Linux; native Mac/iPad installation is not
verified.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
export HF_HOME="$PWD/.cache/huggingface"
export XDG_CACHE_HOME="$PWD/.cache"
.venv/bin/python -m byron_video.style --check
.venv/bin/python -m byron_video.server
```

Open `http://127.0.0.1:8765` on **that same computer**. The server intentionally binds
only to loopback, rejects remote clients/cross-origin uploads, and must not be
tunnelled to the internet. It is not a production authentication boundary.
An iPad needs the separately authorized hosted backend described below.

CLI alternative:

```sh
.venv/bin/python -m byron_video.engine \
  --script /private/path/essay.txt \
  --images /private/path/one.jpg /private/path/two.jpg /private/path/three.png /private/path/four.jpg \
  --output /private/path/essay.mp4
```

Default output is 1920×1080, 24 fps, H.264/AAC, fast-start MP4. The full narration
must fit 90–180 seconds. Use about 180–400 words; the actual duration is measured.
A bounded voice-speed adjustment can bring a borderline script into range, but
the system never trims, rewrites or pads words. If it cannot fit, it explains why.
Maximum input: 4,000 characters, 450 words, 20 MB and 40 megapixels per painting.
Do not paste Markdown or pronunciation markup.

## Style consistency — first film and two-hundredth

[`style-guide/`](style-guide/README.md) is the editable source of truth. Human
protocols explain intent, an explicit JSON source specifies every machine setting,
and a deterministic compiler produces a compact runtime snapshot. There is no
AI summarization on each job. The API freezes the snapshot at job creation and
the renderer records its version and SHA256 fingerprint with the output.

Models, voice, font, timing, palette and motion are pinned. Missing/stale settings
fail closed. Approved presets require a separate approval record and an immutable
versioned snapshot; no initial preset is approved. A style edit is a new reviewed
version, not a silent drift. Lower-resolution CLI previews are permitted only for
drafts and are explicitly recorded as overrides.

## Privacy

- No external analytics, fonts, stock libraries or client-side third-party calls.
- Scripts/images are sent only to the same-origin local process.
- Models are fetched from the official Hugging Face repository, pinned to a revision
  and verified by SHA256 **before** loading. Script text is not sent to that host.
- Private uploads, outputs, SRT/JSON sidecars and model cache are git-ignored.
- Default jobs live in `.private/jobs`, not in `web/`; expired jobs are removed
  during startup/access/new-job cleanup after 24 hours. This is not a background
  guaranteed-deletion service. Stop/restart loses the in-memory job list.
- The generated JSON/SRT sidecars contain the essay text. Treat them as private.
- The public repository must contain code, protocols and explicitly synthetic
  test fixtures only. It must not contain unpublished essays or uploaded paintings.

## Verification

```sh
.venv/bin/python -m pytest -q
node --test web/core.test.mjs
.venv/bin/python -m byron_video.style --check
# Actual short synthetic end-to-end render (not the user's essay):
.venv/bin/python scripts/make_demo.py .private/demo
.venv/bin/python -m byron_video.engine --script .private/demo/script.txt \
  --images .private/demo/painting-1.png .private/demo/painting-2.png .private/demo/painting-3.png .private/demo/painting-4.png \
  --output .private/demo/synthetic-prototype.mp4 --demo-short
```

The short fixture is clearly labelled in both script and pictures. It proves the
local machinery, not a third-party platform, approved aesthetics, genuine painting
rights, or the intended father's iPad workflow. Browser automation tests are in
`web/ui.test.cjs`; they require an environment that can launch Chromium. See
`VERIFICATION.md` for executed checks and unverified items.

## What is still needed for a private iPad creator

Netlify can host a **public showcase** and frontend. It is not by itself a long-running
FFmpeg/TTS renderer. Before exposing conversion, approve a private backend/provider,
its operating budget and access model. A production system needs:

1. A durable job queue and an isolated CPU/GPU worker for TTS and FFmpeg.
2. Authenticated creator access and owner-scoped job/download checks.
3. Private object storage, short-lived upload/download URLs, lifecycle deletion,
   resource quotas and abuse/rate limits.
4. Exact versioned style snapshots, reproducible worker images and reference tests.
5. Restart-safe job state, cancellation, observable failures and bounded retries.
6. Separate explicit publication of approved finished films to the public showcase.

Do not expose this prototype API or place user uploads in Netlify public assets.
No cloud backend, authentication change or paid account is included here.
`netlify.toml` publishes only the static UI preview from `web/`; an unavailable
backend disables Convert and is not presented as a finished hosted service.

## Third-party notices

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Model licensing does not
establish rights to someone else's artwork, text or voice persona. Retain the
operator's rights/attribution records for each chosen painting reproduction.
