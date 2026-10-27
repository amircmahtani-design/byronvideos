# On-demand backend candidate — inactive

This is executable, offline-tested setup code, **not an activated hosted renderer**.
No existing UI, local API, Netlify routing, authentication or provider release gate
is changed. The factory is not mounted anywhere and has no global app or CLI.

## Implemented boundaries

`byron_video.hosted_control.create_app` provides an injectable private control API.
Its defaults deny access. Even with injected fixture dependencies, health remains
`ready:false`, `mode:hosted_setup`; real dispatch is rejected.

- Submit exact script plus four or five numbered, opaque image IDs. A trusted
  upload resolver—not browser metadata—supplies owner, original hash, dimensions,
  type and immutable private storage identity. No upload grant is implemented.
- Freeze explicit visual/motion fingerprints and a complete ElevenLabs profile
  using the existing exact-text request planner. No voice is chosen implicitly.
- Reserve a stable idempotency key atomically **before** dispatch. Timeout/crash or
  an uncertain receipt leaves HOLD; restarting/repeating does not redispatch.
- Read status only as the owning creator. Worker completion is an internal call,
  not a browser endpoint. Require an accepted execution, full-decode assertion,
  portrait 1080×1920/24 fps, original source order and exact narration/caption text
  fingerprints. These fields are assertions from a separately trusted verifier;
  the API itself does not establish pixel, semantic or spoken-word correctness.
- Downloads recheck verifier identity and hash every byte before releasing an MP4
  stream. No public object URL, private source text or storage path is returned.

`SQLiteFixtureStore` exercises transaction/restart semantics locally only. It is
**not** the proposed cloud ledger. Cloud Run's normal filesystem is temporary;
do not place this database there or pretend an object-storage mount supplies
database transactions.

`byron_video.cloud_render` validates an independently loaded owner-bound frozen
job against the render handoff. It binds original order/hashes, exact private
object generations, byte counts, content types and hashes. An owner namespace is
the SHA256 of the trusted opaque subject, not a browser-supplied path.

The request builder targets one configured Cloud Run Job with one task and an
explicit timeout. It requires a digest-pinned container, zero task retries and a
reviewed resource envelope. The trusted adapter must verify actual deployed
settings before submission. Single-task parallelism is **not** a global limit on
simultaneous executions or monthly spending; the future durable controller must
enforce those separate quotas. No token is obtained and no request is sent.

The synthetic-reader staging harness bounds downloads, verifies immutable bytes,
uses a private empty workspace and never deletes files it did not create. Its
test payloads are labelled **not actual MP4/WAV media**. Staging does not decode,
render, synthesize or claim a finished video. The existing real compositor proof
remains separate; it has not been silently replaced by these contract fixtures.

## UI compatibility

The layout and numbered reorder workflow remain unchanged. The candidate API
uses JSON and pre-verified upload IDs, while the current local client sends a
multipart request and accepts local readiness only. A small reviewed transport
adapter is still needed; do not falsely relabel a remote API as `mode:local`.
Cookie/Origin/CSRF policy belongs to the chosen real authentication layer; the
fixture header used in tests is **not** an authentication implementation.

## Proposed low-volume deployment

Prefer a Cloud Run Job that runs only when media is ready, private Cloud Storage
for media and a transactional Firestore job/spend ledger. Persistent job state
does not require an always-running CPU. This proposal is not an approved purchase
or provisioned infrastructure. The empty fields in `cloud-run.target.draft.json`
intentionally fail validation until a reviewed target exists.

Before activation:

1. Approve project/billing, region, operating cap, private data storage/retention
   and creator access method. Configure least-privilege service identities.
2. Choose the ElevenLabs account and exact voice/settings/sample; securely store
   restricted provider keys server-side. Approve provider processing and caps.
3. Implement reviewed private upload/storage and authentication adapters, durable
   transactional cloud state, global active-job/rate/budget limits, and live dispatch
   with unknown-outcome recovery.
   Do provider generation separately; never repeat paid generation on a render
   task retry. The journal is not a grant of spending authority.
4. Build and verify a pinned compositor container from sanitized source. The
   current executor has no Docker/Podman runtime, so **no container build or
   cloud benchmark is claimed**. Preserve old style releases and use a distinct
   ElevenLabs narration profile, not a relabelled Kokoro recording.
5. Bind actual narration/timings and unique motion footage into the shared
   compositor; test a real complete film, owner isolation, expiry, interrupted
   jobs, recovery and private Download before enabling hosted readiness.

All current code is unapproved setup, not final style approval. A client request
cannot select an arbitrary provider URL, shell command, image sequence or voice.

## Offline checks

```sh
BYRON_REQUIRE_MEDIA_TESTS=1 python -m pytest -q
python -m byron_video.style --check
python -m byron_video.motion_planning --check
node --test web/core.test.mjs
node --check web/app.mjs
```

`test_hosted_cloud_handoff.py` tests control submission → bound Cloud Run request
→ private synthetic staging → restart/idempotency handling. It is not provider,
container, hosted authentication or real-video evidence.

Official references checked 27 October 2026:
[Cloud Run run request](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs/run),
[job configuration/retries](https://docs.cloud.google.com/run/docs/create-jobs),
[container/storage contract](https://docs.cloud.google.com/run/docs/container-contract),
[compute pricing](https://cloud.google.com/run/pricing),
[private storage pricing](https://cloud.google.com/storage/pricing),
[Firestore pricing](https://cloud.google.com/firestore/pricing).
