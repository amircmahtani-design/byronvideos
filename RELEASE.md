# Release and rollback protocol

Passing once is a baseline, not an absolute guarantee against future provider,
browser, operating-system or hardware failures. Changes must prove that they have
not broken the baseline. Failures must be explicit; do not silently switch voices,
images, text, providers or style.

## Before publishing any change

1. Run `python -m byron_video.style --check`, `python -m pytest -q`,
   `node --test web/core.test.mjs`, and `node --check web/app.mjs` against the exact
   candidate. A failed check means **do not release**.
2. Review the diff and public-file allowlist. No uploads, scripts, MP4s, private
   metadata, environment files, model cache, generated job files or credentials.
3. For renderer/style/dependency changes, run the synthetic end-to-end fixture in
   the pinned environment. Verify H.264/AAC, output dimensions/duration, non-silent
   audio, integrated loudness/true peak, complete source wording, image order,
   caption timing, fades and safe-area frames. Also run a 90–180-second duration
   fixture. Compare the labelled synthetic visual baselines.
4. A deliberate appearance or voice change needs a new draft style and human
   approval before it becomes the approved release. Never overwrite approved
   snapshots, hand-edit compiled runtime JSON, or refresh golden images merely to
   conceal a failure. Preserve the previous approved version and its asset hashes.
5. Publish the reviewed commit; verify the remote SHA and its CI result. Verify
   that the deployment uses exactly that commit, then check the live page.

## Gates currently included

- GitHub Actions workflow: Python unit/API/style and synthetic compositor pixel
  regressions; JavaScript unit tests and syntax. It downloads no speech model and
  does **not** claim to run a real render or real-browser suite.
- Netlify build: JavaScript unit tests and syntax must pass before its static
  preview is published. Python/FFmpeg tests are **not** run by that static build.
- Full renderer and browser checks remain release-review steps. Until a hosted
  worker is approved, there is no production conversion service to monitor.
- Branch-protection/CI-required merge settings have not been changed. A checked-in
  workflow is not itself a claim that all future merges are platform-enforced.

## Rollback

If a live static preview regresses, redeploy the last verified Netlify deployment
and revert the offending code in a new commit; do not rewrite Git history. Verify
the expected SHA and live behavior again. Do not change access settings or secrets
as part of a routine rollback.

For a future renderer worker, retain an immutable worker image with its matching
approved style snapshot and dependencies. Roll back both together; never pair an
old style with an unreviewed new renderer. Jobs already started retain their frozen
snapshots. Failed/mismatched assets must stop the job, not trigger a substitute.

Once deployed, save the known-good Git SHA, Netlify deployment ID and verification
record in the operator's release notes. Do not put private job content in this file.
