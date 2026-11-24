# Films studio (films.badmaddangerous.com)

Read `hosted/RELEASING.md` before any release, settings or key change.

## Always remind Amir before anything goes live

The studio runs in TWO places that must always match: the website (Cloud Run service
`byron-studio`) and the film maker (Cloud Run job `byron-renderer`), project `badmad-dangerous`,
region `europe-west1`. Whenever a change touches the build (image), the settings (secret
`byron-service-bootstrap` version) or the owner key, say so plainly and walk him through
updating BOTH, website first (confirm the new revision is Ready), then the film maker.
Then check both YAMLs show the same image digest, settings version and `--reviewer-key-sha256`.

## Owner key: Amir makes a new one when he needs it

- Start from the LATEST `byron-service-bootstrap` version in Secret Manager, never an older copy.
- `scripts/renew_owner_setup.py ... --create-key` (plus `--sdk-lock`, `--spending-limit` etc. as needed).
- The new fingerprint must replace `--reviewer-key-sha256` on BOTH service and job.
- Every older key stops working, including the one on his other computer. Tell him.
- Before deploying, run `scripts/check_release.py`; only deploy on READY.

## Branches

Keep `main` and `candidate/private-studio-v9-20261027` identical. Builds run only while pull
request #2 (candidate -> `studio-release-base`) is open: never merge or close it.

## Who uses it

Amir and his father share one studio (this repository is public: never write their emails or
account numbers here). His father knows the current studio
page well: keep what he sees unchanged unless Amir asks. The "What's left" section (fal and
ElevenLabs balances) must stay closed by default; Amir opens it himself.
