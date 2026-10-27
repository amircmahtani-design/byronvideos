# Hosted setup bridge — not activated

This folder and the additive Python modules prepare the existing Byron
Videos app for a private hosted worker. **They do not make hosted Convert work.**
The current public UI, local server, renderer, style files and Netlify configuration
are unchanged. The live release gate is closed; there is no network transport,
credential reader, new public endpoint, authentication change or paid request.

The newer [on-demand candidate](on-demand.md) adds an inactive, default-deny control
API factory, immutable Cloud Run handoff and synthetic private-staging tests. It
does not mount a public endpoint or make hosted Convert ready. For low volume,
on-demand compute plus separate durable storage/ledger is preferred to the
always-running host baseline below; infrastructure remains unapproved.

## Implemented and testable now

- `byron_video.hosted_contract.freeze_job` freezes the raw paste, visible-link
  narration, explicit numbered image order, original image hashes, visual style
  hash, and complete narration profile. It never guesses or sorts a sequence.
- `byron_video.elevenlabs_adapter` plans ElevenLabs text-with-timestamps requests,
  validates the original-text character alignment and converts it to exact word
  spans. It does not substitute normalized wording or a different voice. The
  returned MP3 bytes are marked **unverified** until a real media decoder checks
  them. No tone or fixture is represented as ElevenLabs speech.
- `byron_video.narration_media` force-decodes MP3 with local-only protocols,
  joins planned chunks by exact decoded sample counts, offsets word/character
  timings, then normalizes once and verifies loudness/peak and unchanged duration.
  Actual FFmpeg tests use labelled synthetic tones, not real ElevenLabs speech.
  This stage is also explicitly synthetic-only until its live release is reviewed.
- `byron_video.worker_journal` checkpoints a reservation **before** a request. The
  cap cannot change; duplicate request/operation IDs are rejected. Interrupted
  calls remain `may_have_submitted` after restart and are not retried. Only an
  explicitly injected synthetic fixture transport can execute in this candidate.
- `narration-profile.draft.json` deliberately has no chosen voice or settings;
  validation fails until those decisions are explicit. This is not an approved
  aesthetic preset. No credentials belong in this file.

The generated-motion extension in [`style-guide/motion-planning`](../style-guide/motion-planning/PROTOCOL.md)
locally compiles and freezes a varied, source-supported motion plan. It does not
upload to vision services, reorder scenes, infer final approval or activate fal.
Its current feature records come from local source inspection; automatic feature
extraction is not implemented or represented as solved. The future worker must
load trusted saved review records rather than accept client-invented capabilities.
No new UI is added.

The existing browser already displays photos 1–5 and has labelled Move earlier /
Move later / Remove buttons. This order, not an AI sequence, is authoritative.
The server must still validate original image bytes, dimensions and MIME before
issuing upload grants or computing a trusted handoff. A hash-only plan is **not**
a substitute for those checks.

The 16,000-character / 2,000-word / 900-second technical limits remain; 90–180
seconds remains an advisory, never a rewrite, speed change or rejection. The
proposed Multilingual v2 model accepts 10,000 characters per request, so a larger
paste is split into exact, contiguous, preplanned spans. Every character occurs
once in those spans. These are distinct text chunks, **not automatic retries or
re-synthesis to meet a duration target**. No punctuation is inserted. Pathological
formatting that would create a whitespace-only request fails preflight before
spending. The real-provider speech/chunk continuity and timing review is still required;
synthetic media joining has been tested. Flash v2.5
is supported by this planner but is never selected as a silent fallback.

## Earlier compatible always-running baseline (not the low-volume recommendation)

Keep the existing Netlify site and its public branding. Run a separately approved
Linux container with Python 3.12, pinned FFmpeg/FFprobe, Pillow and NumPy for the
private creator API and a single-job worker. Serve the same layout and workflow, with a small reviewed hosted-readiness/client
contract adaptation, from that authenticated creator origin so its uploads and
job/download paths remain same-origin; do **not** tunnel the loopback server.
The container needs private persistent storage for original files, generated
clips, narration, SQLite job/operation state and completed MP4s. Use one writer,
one replica, bounded disk/CPU usage, owner-scoped downloads, and tested backups.
The journal assumes a trusted, already-authorized operator supplied the cap; it
does not grant spending authority or implement authentication.

A paid Render Docker web service with a persistent disk is one compatible
candidate, or an already-owned Linux host. This is a proposal, not an account or
purchase. Render background workers themselves receive no incoming traffic; an
API service is also needed if that service type is used. A multi-worker design
would instead require shared private object storage and a durable database/queue.

Netlify Background Functions stop after 15 minutes and can retry failed
invocations. They are not the current long-running FFmpeg runtime or safe paid
operation journal. Routing five 20 MB paintings through a small function payload
is not this design. If the creator must remain on the exact Netlify origin, a
separately reviewed small-control-API plus direct private signed-upload path is
needed instead of relabelling the local server as hosted.

## What remains before activation

1. **Host and operating limit:** choose an existing or new renderer host, region,
   monthly/resource budget, persistent private disk and recovery destination.
   Provisioning/billing is separate from routine code deployment.
2. **Creator access:** approve the login/access method and permitted creator(s).
   Never expose the current unauthenticated local API or a shared secret in JS.
3. **ElevenLabs:** choose the existing account, an exact British male voice ID,
   model/settings/seed and approved speech sample. Configure a restricted
   `ELEVENLABS_API_KEY` only in the worker's secure server-side secret store;
   never in chat, Git, browser, URL, screenshot, log or downloadable job manifest.
   Approve a per-film narration cap and actual script processing by this provider.
4. **fal:** reuse the separately reviewed model/motion work only after its live API
   transport, durable budget/unknown-outcome rules and server-side `FAL_KEY` are
   reviewed. Browser-generated clips prove quality experiments, not API wiring.
   The existing short motion sample has not established an approved full-film
   animation recipe or long-scene continuation.
5. **Shared compositor:** promote the sanitized, tested portrait compositor into
   a new draft release. Its recovered Kokoro profile cannot be presented as an
   ElevenLabs profile. Bind the distinct narration profile and newly tested media stages to the visual
   snapshot, verify real-provider pronunciation/alignment and chunk continuity;
   retain the old lock. Do not weaken the old synthetic or browser-import guards.
6. **Real end-to-end gate:** test original numbered uploads → one planned speech
   pass → sufficient unique motion footage → captions/crossfades → verified MP4
   → private Download. Also test restart, uncertain paid calls, duplicate submit,
   access isolation, expiry/deletion and rollback. Only then allow readiness=true
   in a separately reviewed hosted client contract; current client accepts local
   mode only and remains disabled on static Netlify.

No new credentials, external author-file disclosure, payments, accounts or access
changes were made by this implementation. No promise of exact provider determinism
is made: model names/settings/seeds and actual output hashes must be recorded and
accepted output reused rather than repeatedly regenerated.

## Verified-source notes

The public baseline is commit `5faf7243d40d3b5f23b20fa39b1f5aa0537b0f67`.
The private recovered portrait compositor was independently tested in a separate
candidate; it is **not silently included or activated** by these additive files.
Its browser-trial receipts/account references/private source material must not be
copied wholesale into this public repository.

## Tests

```sh
python -m pytest -q tests/test_hosted_setup.py
python -m pytest -q
python -m byron_video.style --check
python -m byron_video.motion_planning --check
node --test web/core.test.mjs
node --check web/app.mjs
```

These tests are offline contract/journal tests, not provider-quality tests,
container deployment, live authentication, or hosted video-generation evidence.
The existing GitHub test workflow picks up the new Python tests automatically.
No Docker build has been claimed; an immutable container digest and exact FFmpeg
runtime must be verified after the host choice, before a style release.

## Official references checked 27 October 2026

- [ElevenLabs original-text timestamps and speech request schema](https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps).
  Standard response logging is not zero-retention; enterprise-only zero-retention
  must not be assumed. This candidate makes no provider-retention guarantee.
- [ElevenLabs model limits](https://elevenlabs.io/docs/overview/models).
- [Render background workers](https://render.com/docs/background-workers) and
  [persistent disks](https://render.com/docs/disks).
- [Netlify Background Functions runtime and retries](https://docs.netlify.com/build/functions/background-functions/).
