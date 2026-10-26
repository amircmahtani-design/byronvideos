# Verification record — local prototype

This is engineering evidence, not aesthetic or voice approval. Public regression
fixtures are original, labelled geometric test illustrations. All private user
inputs, output films and project-specific verification evidence remain excluded
from the public repository.

## Expanded input capacity — draft 0.1.2

- 86 Python tests and 10 JavaScript tests pass, plus JavaScript syntax and the
  source/compiled style check. Larger synthetic full-paste fixtures (title,
  byline, website and story) and five original JPEGs pass through the API without
  truncation, reordering or byte mutation. Exact input-limit boundaries are tested.
- Technical limits are now 2,000 words, 16,000 characters and 900 seconds of
  narration, with no minimum word count. The 90–180-second range remains only
  guidance, and each film uses one synthesis pass at its frozen voice speed.
  The 900-second processing boundary is tested in code, **not** a claim of a
  completed 15-minute stress render or a rendering-time guarantee.
- Standard HTTP(S) Markdown links use their visible text for speech/captions,
  never hidden URLs. The exact raw paste and narrated text are both retained in
  the private manifest. Story wording is not rewritten. URLs are never fetched.
- Current draft style: `draft-0.1.2`, SHA256
  `85dad97824c0efc4c1e550fc5415db87e97c65e2b1f162be463bdbd25b1fdd5a`.
  It remains **DRAFT**, with no human style, voice or pronunciation approval.
- Caption regression tests now keep adjacent quotation marks with their source
  words and rebalance short overflow continuations without rewriting or changing
  speech timestamps. Short complete dialogue remains intact.

## Historical duration-policy correction — draft 0.1.1

- 77 Python tests and 9 JavaScript tests pass, plus JavaScript syntax and the
  source/compiled style check. Boundary tests explicitly cover 89, 90, 180 and
  181 seconds: exactly one narration pass, unchanged input and fixed voice speed,
  non-blocking advisories outside the usual range, and completed MP4 downloads.
- Historical draft style: `draft-0.1.1`, SHA256
  `3361cc2370a95856bb63057724e20a32be19fbdd3650aa3c9150f195f7d9499a`.
  This is still **DRAFT**, not human aesthetic or voice approval.
- Real normal API path, without a short-script override: 61-word synthetic input
  accepted; one narration pass at frozen speed 0.93; complete/download succeeded
  with the actual 32.775-second narration duration and a non-blocking advisory.
  API-to-download elapsed time was 72.84 seconds with cached model files.
- Export: 32.791667-second MP4, 1920×1080, 24 fps, H.264/AAC, measured −16.06 LUFS
  and −1.47 dBTP. Every source word was preserved. The resulting video is
  byte-identical to the earlier short reference, confirming that this policy
  correction did not change its voice or visual treatment:
  `f4251e5b3c8b274b7f704940a9c50623d279f91c0a3627dde7d282cbb6bedea5`.
- Canonical URL and sitemap cover only the verified public landing page.
  Robots directives exclude future API/job/creator/upload paths; they are not
  access controls and do not replace private-backend authorization.

## Historical baseline — draft 0.1.0

- 66 Python tests: validation, exact-text alignment, safe image decoding, uncropped
  portrait/landscape/square motion, captions, local API, job/style snapshot isolation,
  tamper rejection, and exact synthetic compositor pixel baselines.
- 7 JavaScript unit tests; application JavaScript syntax check.
- Style source/compiled consistency check and independent audit, including rejected
  tampering of protocol prose, renderer/compiler code, runtime, font and references.
- Historical draft style: `draft-0.1.0`, SHA256
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
- Rights/attribution review for publication of user-supplied material. Private
  rendering does not establish publication rights.
- Real-browser/iPad interaction QA: a browser suite is provided, but local Chromium
  launch was blocked by this execution environment. Unit tests are not browser QA.
- Hosted conversion, authentication, private cloud storage, durable jobs, provider
  costs or production uptime. The static preview truthfully disables Convert when
  its backend is absent.
- GitHub-hosted CI execution and Netlify deployment were not performed by the local
  implementation task. The checked-in workflow is a proposed executable gate;
  its first remote run must be checked after publication.

The earlier 244-word synthetic stress test produced a 107.25-second 1080p film,
with −16.05 LUFS / −1.44 dBTP audio. Its real API repeat was byte-identical to the
separate CLI export. That full-length baseline belongs to draft 0.1.0; the correction
above was rechecked with the actual short API render and duration-boundary tests,
not represented as a new full-length render.
