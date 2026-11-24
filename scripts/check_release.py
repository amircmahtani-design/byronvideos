#!/usr/bin/env python3
"""Before deploying: will the studio accept this build with these settings and this owner key?

Runs locally, reads only files you name, publishes nothing. It checks exactly what the website
(byron-studio) and the film job (byron-renderer) check when they start, so a release that passes
here will not be refused with "SDK lock does not match reviewed private configuration" or similar.

  python scripts/check_release.py --settings settings.json --review-dir review/ --pin <reviewer-key-sha256>

--settings    the studio settings (the byron-service-bootstrap secret value you will deploy)
--review-dir  the unzipped 'public-runtime-candidate-review' artifact of the build you will deploy
              (GitHub > Actions > "Resolve public hosted runtime candidate" run for that commit)
--pin         the --reviewer-key-sha256 value you will set on BOTH the service and the job

Every line must say OK. See hosted/RELEASING.md for the whole release order.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(settings_text, review_dir, pin, repo_root=ROOT):
    """Return a list of (ok, message). Never raises for a mismatch; reports it."""
    results = []
    review = Path(review_dir)
    try:
        from byron_video.bootstrap_bundle import parse_bootstrap_bundle
        from byron_video.lean_film import lean_activation
        bundle = parse_bootstrap_bundle(settings_text, pin, administration_only=True)
        if 'lean_generation' in bundle['config']:  # Always present in the live studio.
            lean_activation(bundle['config'], bundle['processing_plan'], bundle['acceptance'], bundle['reviewer_public_key'])()
        results.append((True, 'Settings are signed by the owner key with this pin, not expired, and pass the film job check.'))
        runtime = bundle['config']['runtime']
    except Exception as exc:  # Report, so every other line can still be read.
        return results + [(False, f'Settings refused with this pin: {exc}')]
    lock = review / 'sdk-lock.json'
    if lock.is_file():
        actual = hashlib.sha256(lock.read_bytes()).hexdigest()
        results.append((actual == runtime['sdk_lock_sha256'],
                         f"Software list: build {actual[:12]}, settings {runtime['sdk_lock_sha256'][:12]}"
                         + ('' if actual == runtime['sdk_lock_sha256'] else
                            ' -> re-sign with: scripts/renew_owner_setup.py --sdk-lock <review>/sdk-lock.json')))
    else:
        results.append((False, f'No sdk-lock.json in {review}; unzip the build review artifact there.'))
    base = review / 'base-resolution.json'
    if base.is_file():
        digest = json.loads(base.read_text(encoding='utf-8'))['digest']
        results.append((digest == runtime['base_image'], f"Base system: build {digest[-12:]}, settings {runtime['base_image'][-12:]}"))
    materials = review / 'materials.json'
    if materials.is_file():
        ffmpeg = json.loads(materials.read_text(encoding='utf-8')).get('ffmpeg_version')
        results.append((ffmpeg == runtime['ffmpeg_version'], f"FFmpeg: build {ffmpeg}, settings {runtime['ffmpeg_version']}"))
    release = Path(repo_root) / 'style-guide/portrait-candidate/release.json'
    actual = hashlib.sha256(release.read_bytes()).hexdigest()
    results.append((actual == runtime['portrait_candidate_sha256'],
                    f"Portrait release: code {actual[:12]}, settings {runtime['portrait_candidate_sha256'][:12]}"
                    ' (run this from the commit you are deploying)'))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--settings', required=True, type=Path)
    parser.add_argument('--review-dir', required=True, type=Path)
    parser.add_argument('--pin', required=True)
    args = parser.parse_args(argv)
    results = check(args.settings.read_text(encoding='utf-8-sig').strip(), args.review_dir, args.pin)
    for ok, message in results:
        print(('OK    ' if ok else 'STOP  ') + message)
    ready = bool(results) and all(ok for ok, _ in results)
    print('\nREADY to deploy: same build, settings version and pin on BOTH service and job.' if ready
          else '\nNOT ready: fix every STOP line first. Nothing was changed.')
    return 0 if ready else 1


if __name__ == '__main__':
    raise SystemExit(main())
