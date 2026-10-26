# Reference and release protocol

## Non-negotiable behavior

1. Preserve the complete supplied wording and painting order. Never rewrite,
   summarize, substitute stock, regenerate artwork or silently remove a word.
2. Show the whole original painting within a charcoal matte. Gentle motion must
   stay inside the matte; it must not crop a painting's edges. Do not use random
   framing, mood, colour grading or transition choices.
3. Use the locked British synthetic voice, with consistent pauses and a bounded
   tempo policy. A pronunciation failure blocks export rather than dropping a
   word. Human listening review is required for the first reference, names and
   meaningful voice/model changes.
4. Derive captions from original text and speech-model token timing, never an
   invented transcript. At most two readable lines, inside the locked bottom
   safe area. Model-predicted timestamps are not independent forced alignment;
   review synchronization on the reference before production approval.
5. Honour actual duration limits without cutting content. If a bounded tempo
   correction cannot fit the complete narration, explain the problem and ask
   for a suitable script rather than pretending conversion succeeded.

## Establishing the first reference

- First create a clearly labelled synthetic fixture covering portrait,
  landscape and square paintings, punctuation, a long caption, and all scene
  transitions. This can be public; actual user material stays private.
- Record exact script/input hashes, runtime snapshot/hash, dependency versions,
  model/config/voice hashes, font hash, encoder version and all render settings.
- Save full MP4, exact-text SRT, metadata and frames at prescribed timestamps.
  Include an opening frame, readable one- and two-line captions, each painting,
  transition midpoint and final frame. Pin frame dimensions and SHA-256 hashes.
- Verify decoded video/audio streams, duration, frame rate, complete source
  wording, non-silent audio, no clipped painting edges, no caption overflow,
  and smooth deterministic motion. Compare both before and after a change.
- Watch and listen end to end. Then render the user's authorized private essay
  and selected ordered paintings. Ask for actual style and voice approval.
- Only after that approval may a release record and frozen approved snapshot be
  created. A synthetic fixture is not evidence that the user approved a style.

## Regression checks: film 1 and film 200

**Exact checks:** source/runtime sync; schema; frozen version; all asset and
dependency fingerprints; source words and order; caption line count/safe area;
canvas/FPS/codec; style snapshot in every job and output manifest; deterministic
scene timings and bounds. Fail if a required field or expected reference is
missing. Golden frame tests must call the renderer's compositor using a fixed
synthetic source, timestamp and preset, not a separate reimplementation.

**Pixel checks:** keep exact golden pixel hashes for the pinned software
environment. When changing an encoder/library intentionally, compare decoded
frames with stated pixel/structural tolerances and inspect differences; do not
blindly regenerate and accept the goldens. MP4 bytes may differ across encoder
versions even when decoded frames are visually identical. Byte-for-byte video
identity is not claimed across operating systems or hardware.

**Human checks:** compare the full reference films side by side and listen to
voice, pace, pronunciation, loudness, caption timing and transition feel. Any
deliberate aesthetic change needs a new draft version and fresh approval.
Regression success alone never supplies that approval.

## Change checklist

- [ ] New draft version and explanation of intended difference.
- [ ] Human protocol and machine source agree; no unrelated style drift.
- [ ] Compiler and all tests pass; generated snapshot reviewed.
- [ ] Exact fonts/model/voice/input and runtime fingerprints verified.
- [ ] Synthetic golden checks pass or differences reviewed explicitly.
- [ ] Old and new reference MP4 watched and listened to end to end.
- [ ] User approves the exact new version and reference, recorded privately.
- [ ] Redacted approval metadata and immutable snapshot frozen, if publication
      of that non-sensitive record is authorized.
- [ ] New jobs select the new approved version explicitly; existing jobs retain
      the style snapshot they were created with.

## Current unapproved gaps

The initial draft's voice is provisional, its specified two-pass loudness mode
still needs end-to-end measurement and human listening approval, and the
approved private reference set does not yet exist. These must remain visible
rather than being concealed by a successful build.
