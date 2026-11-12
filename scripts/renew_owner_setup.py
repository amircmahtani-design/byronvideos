#!/usr/bin/env python3
"""Renew the studio's signed owner-setup authorization; runs locally, publishes nothing.

Reads the current noncredential bootstrap bundle (the BYRON_SERVICE_BOOTSTRAP secret value),
signs a new administration-only authorization for the SAME config and plan with the owner's
Ed25519 private key, verifies it with the studio's own check, and writes a new bundle file.
The private key is read only from the file you name; it is never printed, copied or uploaded.

  python scripts/renew_owner_setup.py --bundle current.json --private-key owner.key --out renewed.json [--days 365]

Then add renewed.json's contents as a new version of the bootstrap secret and point the studio
service and the film job at that version.

If the owner key is lost, add --create-key: a NEW key is generated and saved to --private-key (which
must not exist yet), the bundle's public owner key is replaced, and the new fingerprint is printed.
That fingerprint must then replace --reviewer-key-sha256 in BOTH the studio service and the film job.
"""
import argparse
import base64
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from byron_video.administration_runtime import PURPOSE, SETUP_MAX_SECONDS, utc, verify_administration  # noqa: E402
from byron_video.bootstrap_bundle import MAX_BUNDLE_BYTES, parse_bootstrap_bundle  # noqa: E402
from byron_video.hosted_control import canonical, digest  # noqa: E402


def load_private_key(path):
    """PEM (PKCS#8, unencrypted), raw 32 bytes, or base64 of 32 bytes."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    data = Path(path).read_bytes()
    if b'BEGIN' in data:
        key = load_pem_private_key(data, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SystemExit('The key file is not an Ed25519 private key.')
        return key
    raw = data if len(data) == 32 else base64.b64decode(data.strip(), validate=True)
    if len(raw) != 32:
        raise SystemExit('The key file is not a 32-byte Ed25519 private key.')
    return Ed25519PrivateKey.from_private_bytes(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bundle', required=True, type=Path, help='Current bootstrap bundle (JSON) to renew.')
    parser.add_argument('--private-key', required=True, type=Path, help="Owner's Ed25519 private key file.")
    parser.add_argument('--out', required=True, type=Path, help='Where to write the renewed bundle (must not exist).')
    parser.add_argument('--days', type=int, default=365, help='Validity in days (1-366, default 365).')
    parser.add_argument('--create-key', action='store_true', help='Owner key lost: make a new one at --private-key (must not exist).')
    args = parser.parse_args(argv)
    if not 1 <= args.days <= SETUP_MAX_SECONDS // 86400:
        raise SystemExit(f'--days must be between 1 and {SETUP_MAX_SECONDS // 86400}.')
    if args.out.exists():
        raise SystemExit('The output file already exists; choose a new name.')
    bundle = json.loads(args.bundle.read_text(encoding='utf-8-sig').strip())
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
    if args.create_key:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        if args.private_key.exists():
            raise SystemExit('A key file already exists there; nothing was replaced. Choose a new file name.')
        key = Ed25519PrivateKey.generate()
        args.private_key.parent.mkdir(parents=True, exist_ok=True)
        args.private_key.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        bundle['reviewer_public_key_base64'] = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    else:
        key = load_private_key(args.private_key)
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    if base64.b64encode(public).decode() != bundle.get('reviewer_public_key_base64'):
        raise SystemExit("This private key does not belong to the studio's pinned owner key; nothing was signed.")
    now = int(time.time())
    payload = {**bundle['acceptance']['payload'], 'schema_version': 1, 'purpose': PURPOSE,
               'config_sha256': digest(bundle['config']), 'plan_sha256': digest(bundle['processing_plan']),
               'issued_at': now - 60, 'expires_at': now - 60 + args.days * 86400,
               'owner_only': True, 'generation_allowed': False, 'provider_calls_allowed': False}
    envelope = {'payload': payload, 'signature': base64.b64encode(key.sign(canonical(payload).encode())).decode()}
    verify_administration(envelope, public, digest(bundle['config']), digest(bundle['processing_plan']))
    renewed = {**bundle, 'acceptance': envelope}
    text = json.dumps(renewed, separators=(',', ':'), sort_keys=True, ensure_ascii=False)
    if len(text.encode('utf-8')) > MAX_BUNDLE_BYTES:
        raise SystemExit('The renewed bundle is larger than Cloud Run allows; nothing was written.')
    parse_bootstrap_bundle(text, hashlib.sha256(public).hexdigest(), administration_only=True)  # Exactly what the studio checks.
    args.out.write_text(text, encoding='utf-8')
    print(f'Renewed owner setup written to {args.out}; valid until {utc(payload["expires_at"])}.')
    if args.create_key:
        print(f'NEW owner key saved to {args.private_key}. Back it up somewhere safe (password manager or private cloud folder).')
        print(f'Set --reviewer-key-sha256 to {hashlib.sha256(public).hexdigest()} on BOTH the studio service and the film job.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
