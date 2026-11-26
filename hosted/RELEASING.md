# Releasing the films studio (films.badmaddangerous.com)

Everything that must stay connected, and the order to change it in. Follow this from either
computer; it is the same flow every time.

## The pieces

| Piece | Where | Must match |
|---|---|---|
| Code | GitHub `amircmahtani-design/byronvideos`: `main` **and** `candidate/private-studio-v9-20261027` | keep both branches equal |
| Build (image) | Artifact Registry `europe-west1 / badmad-dangerous / byron-studio / byron-studio`, tagged by commit | the **same** image on service and job |
| Settings | Secret Manager `byron-service-bootstrap`, one version per change | the **same** version on service and job |
| Owner key | a private file on your computer (`Documents\byron-studio-owner-key\…key`) | its fingerprint = `--reviewer-key-sha256` on service **and** job |
| Website | Cloud Run service `byron-studio` (europe-west1) | |
| Film maker | Cloud Run job `byron-renderer` (europe-west1) | |

The website and the film maker check each other: the website plans each film's recordings and
cost, the film maker re-checks that plan. **They must always run the same image, the same
settings version and the same key fingerprint.**

## Owner key: one key, both computers

There must be only one owner key. Every `--create-key` makes a new one and retires the old one.
After a key change, copy the key file to the other computer and your password manager the same
day. Never run `--create-key` while you still have the current key.

Current (24 Nov 2026): `owner-2026-11-24.key`, fingerprint
`7a78d3b66ca85905ffcb28a946f2f6b1795ae151489810743ee1acd1dec6a320`, settings version 26,
valid until 24 Nov 2027 (renew before then with `scripts/renew_owner_setup.py`).

## Releasing a code change

1. Commit to `candidate/private-studio-v9-20261027` and to `main` (keep them equal).
   Pull request #2 (`candidate/...` into `studio-release-base`) must stay **open**: the build only
   runs while it is open. Never merge or close it, and never make `main` identical to the
   candidate branch through that pull request.
2. Wait for GitHub Actions on that commit: "Regression checks", "Resolve public hosted runtime
   candidate" and "Publish the tested runtime image" must all succeed.
3. From the "Resolve public hosted runtime candidate" run, download the artifact
   `public-runtime-candidate-review` and unzip it (e.g. to `review/`).
4. Save the current settings (the latest `byron-service-bootstrap` version) to `settings.json`
   and run, from the commit you are releasing:

       python scripts/check_release.py --settings settings.json --review-dir review --pin <current fingerprint>

   - **READY**: continue with step 6 using the current settings version.
   - **STOP on "Software list"**: a fresh build re-resolves its supporting packages, so this is
     normal from time to time. Go to step 5.
5. Re-sign the settings for this build (current key, no `--create-key`):

       python scripts/renew_owner_setup.py --bundle settings.json --private-key <current key> --sdk-lock review/sdk-lock.json --out settings-new.json

   Run `check_release.py` again on `settings-new.json` until it says READY, then add its contents
   as a **new version** of `byron-service-bootstrap` (leave "Disable all past versions" unticked).
6. **Website first.** In `byron-studio` (Cloud Run > service > YAML > Edit) set the image to the
   new digest, the secret `key:` to the settings version, and `--reviewer-key-sha256` to the
   fingerprint. Save, then check the newest revision is Ready and gets 100% of traffic. If it
   is not Ready, Cloud Run keeps the old website; read its logs and fix before going on.
7. **Then the film maker.** Same three values in `byron-renderer` (job > YAML > Edit), Save and
   update job.
8. Check both YAMLs show the same image digest, settings version and fingerprint.

## Changing only settings (accounts, spending limit, renewal)

Use `scripts/renew_owner_setup.py` with the current key (see its help: `--spending-limit`,
`--remove-spending-limit`, `--days`). Adding a sign-in account: the person tries to sign in once,
the website log shows "Sign-in refused for <email> (Google account ID <number>)"; add that number
to `creator_subjects`, `admin_subjects` and `subject_emails`, then re-sign. Save as a new secret
version and point **both** service and job at it (steps 6-8; the image stays the same).

## If something goes wrong

The last known-good combination is always the previous image digest, settings version and
fingerprint shown in each YAML's history. Put the film maker back first (films stop failing),
then the website.
