"""Owner-setup renewal script with a synthetic bundle and throwaway keys; no cloud I/O."""
import base64
import hashlib
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from byron_video.bootstrap_bundle import parse_bootstrap_bundle
from test_administration_runtime_stdlib import fixture

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'renew_owner_setup.py'
spec = importlib.util.spec_from_file_location('renew_owner_setup', SCRIPT)
renew = importlib.util.module_from_spec(spec); spec.loader.exec_module(renew)


class RenewOwnerSetupChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        c, p, envelope, _ = fixture()
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        envelope['payload'].update(issued_at=1_700_000_000, expires_at=1_700_000_000 + 7 * 86400)  # Long expired.
        self.bundle = self.root / 'current.json'
        self.bundle.write_text(json.dumps({'schema_version': 1, 'config': c, 'processing_plan': p, 'acceptance': envelope,
                                           'reviewer_public_key_base64': base64.b64encode(public).decode()}), encoding='utf-8')
        self.key_sha = hashlib.sha256(public).hexdigest()
    def tearDown(self): self.temp.cleanup()

    def write_key(self, private, name='owner.pem'):
        path = self.root / name
        path.write_bytes(private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        return path

    def test_expired_setup_is_renewed_for_a_year_and_passes_the_studio_check(self):
        out = self.root / 'renewed.json'
        self.assertEqual(renew.main(['--bundle', str(self.bundle), '--private-key', str(self.write_key(self.private)), '--out', str(out)]), 0)
        text = out.read_text(encoding='utf-8')
        checked = parse_bootstrap_bundle(text, self.key_sha, administration_only=True)
        payload = checked['acceptance']['payload']
        self.assertAlmostEqual(payload['expires_at'] - time.time(), 365 * 86400, delta=120)
        self.assertEqual((payload['owner_only'], payload['generation_allowed'], payload['provider_calls_allowed']), (True, False, False))
        self.assertEqual(json.loads(text)['config'], json.loads(self.bundle.read_text(encoding='utf-8'))['config'])  # Config unchanged.
        self.assertNotIn('PRIVATE', text)

    def test_lost_key_is_replaced_by_a_new_one_that_the_studio_accepts_under_its_new_pin(self):
        key_file, out = self.root / 'keys' / 'new-owner.pem', self.root / 'renewed.json'
        self.assertEqual(renew.main(['--bundle', str(self.bundle), '--private-key', str(key_file), '--out', str(out), '--create-key']), 0)
        new = renew.load_private_key(key_file).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        text = out.read_text(encoding='utf-8')
        parse_bootstrap_bundle(text, hashlib.sha256(new).hexdigest(), administration_only=True)
        with self.assertRaisesRegex(ValueError, 'cannot nominate its own'):  # The old pin no longer opens it.
            parse_bootstrap_bundle(text, self.key_sha, administration_only=True)
        with self.assertRaisesRegex(SystemExit, 'already exists'):  # A key file is never overwritten.
            renew.main(['--bundle', str(self.bundle), '--private-key', str(key_file), '--out', str(self.root / 'y.json'), '--create-key'])

    def test_wrong_key_signs_nothing_and_output_is_never_overwritten(self):
        out = self.root / 'renewed.json'
        with self.assertRaisesRegex(SystemExit, 'does not belong'):
            renew.main(['--bundle', str(self.bundle), '--private-key', str(self.write_key(Ed25519PrivateKey.generate(), 'other.pem')), '--out', str(out)])
        self.assertFalse(out.exists())
        out.write_text('keep', encoding='utf-8')
        with self.assertRaisesRegex(SystemExit, 'already exists'):
            renew.main(['--bundle', str(self.bundle), '--private-key', str(self.write_key(self.private)), '--out', str(out)])
        with self.assertRaisesRegex(SystemExit, 'between 1 and 366'):
            renew.main(['--bundle', str(self.bundle), '--private-key', str(self.write_key(self.private)), '--out', str(self.root / 'x.json'), '--days', '400'])


if __name__ == '__main__': unittest.main()
