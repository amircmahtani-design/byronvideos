# The Byron Videos style lock

**Current status: DRAFT, NOT APPROVED.** There is no approved first film or final
voice yet. This folder is the public-safe editing and consistency system, not a
claim that the visual style has already been accepted.

## One source, one condensed runtime

1. Edit the human rules in `PROTOCOL.md` when intent changes.
2. Make each effective numeric, typography, voice or asset change explicit in
   `style.source.json`. It is the complete machine-readable authority. After
   reviewing a protocol edit, explicitly update its SHA-256 in `protocols`;
   changing prose without updating the lock must fail, not silently drift.
3. Run `python -m byron_video.style`. The deterministic compiler validates every
   required setting and writes the compact `style.runtime.json` snapshot with
   canonical source and preset SHA-256 fingerprints.
4. Run `python -m byron_video.style --check` and the tests. Missing, unknown,
   unsafe or stale settings fail closed. There is **no LLM summary**, inference
   from prose, random style selection, or silent default substitution.
5. Review the source, protocol and generated diff together. Never edit the
   generated runtime independently.

The creator API and renderer load this exact compiled preset. At job creation,
the API must freeze a deep copy, its version and hash. Rendering uses that frozen
copy; a later edit cannot change an already queued film. Private output metadata
must preserve the full snapshot and render/dependency fingerprints, so every
film can be explained and reproduced.

This local prototype does **not** retain a runnable copy of every old worker or
asset version. If pinned code, model, font or dependency files are replaced, an
old job must fail closed rather than render with a different style. Production
must retain a per-version worker/container and its exact assets to replay old
releases. Stored metadata alone is not an executable archive.

The compiler reads human-editable JSON only to validate the compiled snapshot is
current. It does not send the protocol, script, paintings or style to a model.

## What is locked

- Canvas resolution, frame rate, aspect, colour-space declaration and encoding.
- Four-colour brand palette; complete-painting matte/contain layout.
- Caption font **file and hash**, size, line spacing, safe area, two-line limit,
  grouping and timestamp policy. The bundled unmodified DejaVu Sans carries its
  licence in `fonts/`; it is a **provisional screen-readable subtitle choice**,
  not an assertion that it is the existing website's editorial face.
- Exact Kokoro model/revision/config/voice fingerprints, language, seed, fixed tempo,
  pauses, runtime dependencies and no-rewrite rule. The usual duration is advisory;
  a separately labelled technical processing ceiling protects local resources.
- Motion amplitude, easing, direction rule and transition/fade durations.
- Two-pass EBU R128 audio target of -16 LUFS and -1.5 dB true peak, with an
  11 LU loudness-range target. These are locked renderer settings, not a claim
  of user approval. Each exported file needs measured loudness verification;
  the current audio status remains pending human listening approval.
- Reference-manifest fingerprint and approval status.
- Renderer and compiler source fingerprints: changing implementation cannot
  silently change an approved film's appearance with the same locked preset.

The existing website's Playfair Display / Cormorant Garamond identity remains the
editorial design direction. Adding either to rendered video requires a specific
licensed font file, fingerprint, reference render and new review; do not load an
unversioned remote font or silently substitute a system font.

## Approval and later changes

See `PROTOCOL.md`. Initial approval is a human decision after watching and
listening to the reference film. No code, generated fixture, green test or draft
may approve itself. `approved/` intentionally has no approved snapshot today.

An approved version is immutable: preserve its exact runtime bytes in
`approved/<version>.json`, record the user's approval and record hash, and keep
the corresponding references. The compiler refuses an approved source unless
its reviewed frozen snapshot already exists and matches byte for byte.
The structured JSON approval record binds the exact release version,
`effective_style_hash` (all preset fields except the approval record itself),
reference-manifest hash, non-sensitive evidence ID, UTC timestamp and approval
scope covering visual style, voice and audio. Voice and audio must both be
approved, and the approved reference manifest must contain video, frame,
subtitle and metadata fingerprints with matching approval evidence. Empty
approval files and empty reference sets cannot release a style. These checks
validate the record's integrity; they cannot manufacture or independently prove
a user's consent. A human must record an actual explicit approval first.
Never overwrite an approved version to change the look. Make a new draft
version, render comparison references, obtain fresh approval, then explicitly
select the new release. Old films and jobs keep their old version.

## Public versus private

Only code, protocols, licence notices, fingerprints permitted for publication
and clearly labelled synthetic QA references belong here. Real essays,
paintings, unpublished films, private URLs and user approval conversations do
not belong in this public repository. Store private reference evidence in an
authorized private location; public approval records must be redacted and
contain only a non-sensitive decision identifier, scope, version and date.

`references/manifest.json` currently has no approved assets. A synthetic
demonstration proves engineering behavior only; it does not replace the user's
essay test or human aesthetic/voice approval.
