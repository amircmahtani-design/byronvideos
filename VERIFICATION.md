# Verification record — local prototype

This is engineering evidence, not aesthetic approval or a test of the user's
actual essay and paintings. All image fixtures are original, labelled geometric
test illustrations. No user assets have been supplied to this prototype.

## Passed locally

- 66 Python tests: validation, exact-text alignment, safe image decoding, uncropped
  portrait/landscape/square motion, captions, local API, job/style snapshot isolation,
  tamper rejection, and exact synthetic compositor pixel baselines.
- 7 JavaScript unit tests; application JavaScript syntax check.
- Style source/compiled consistency check and independent audit, including rejected
  tampering of protocol prose, renderer/compiler code, runtime, font and references.
- Final draft style: `draft-0.1.0`, SHA256
  `766ce4b02ef10674dae124a991e3cf371148447dbb3ef3224181452d6ecd3718`.
- Real short synthetic render: 32.775-second narration, 32.792-second MP4 container,
  1920×1080, 24 fps, H.264/yuv420p and AAC. Generation took 70.54 seconds in this
  environment (31.42 seconds narration and 38.92 seconds composition/encoding),
  with model files already cached. This is one measurement, not a latency guarantee.
- Encoded speech loudness measured at approximately −16.06 LUFS integrated and
  −1.47 dBTP, against draft targets −16 LUFS / −1.5 dBTP. No clipping detected.
- Original-image border/synthetic labels and bottom-caption safe area inspected in
  decoded frames; source-word preservation is checked before output is exported.

The first uncached official model setup took approximately 55 seconds separately.
The model, configuration and voice hashes are verified before loading. No paid
generation service or API key was used.

## Not yet established

- Human approval of the British voice, pronunciation and visual style.
- Independent forced-alignment accuracy or subjective full-film listening review;
  captions currently use the speech model's own duration predictions.
- The user's actual essay/painting sequence and rights/attribution review.
- Real-browser/iPad interaction QA: a browser suite is provided, but local Chromium
  launch was blocked by this execution environment. Unit tests are not browser QA.
- Hosted conversion, authentication, private cloud storage, durable jobs, provider
  costs or production uptime. The static preview truthfully disables Convert when
  its backend is absent.
- GitHub-hosted CI execution and Netlify deployment were not performed by the local
  implementation task. The checked-in workflow is a proposed executable gate;
  its first remote run must be checked after publication.

Full production-duration synthetic stress-test results are recorded separately
after completion; the 32-second fixture alone does not prove 90–180-second output.
