"""Trusted-device pass: issued only after a Google callback, server-checked, revocable; fake Firestore."""
import unittest
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.testclient import TestClient

from byron_video.admin import AdminSession
from byron_video.hosted_control import Principal
from byron_video.hosted_uploads import CreatorSession
from byron_video.trusted_device import (DEVICE_COOKIE, DEVICE_SECONDS, MAX_DEVICES, FirestoreTrustedDevices,
                                        TrustedDeviceIdentity, install_device_cookies)
from test_cloud_journal_stdlib import Client, PROJECT

OWNER = 'google_' + 'a' * 64
OTHER = 'google_' + 'b' * 64


def request(token=None, scheme='https'):
    return SimpleNamespace(url=SimpleNamespace(scheme=scheme), cookies={} if token is None else {DEVICE_COOKIE: token})


class Expired:
    def __call__(self, request): raise ValueError('expired Google session')
    def admin_session(self, request): raise ValueError('expired Google session')


class TrustedDeviceChecks(unittest.TestCase):
    def setUp(self):
        self.db = Client()
        self.devices = FirestoreTrustedDevices(self.db, project_id=PROJECT, namespace='synthetic_devices', transaction_runner=self.db.run)
        self.identity = TrustedDeviceIdentity(Expired(), self.devices, owners=[OWNER], csrf_secret=b's' * 32,
            creator_session=lambda owner, csrf: CreatorSession(Principal(owner), csrf), admin_session=AdminSession)

    def test_pass_lasts_thirty_days_and_can_be_revoked(self):
        token = self.devices.issue(OWNER, now=1000)
        self.assertEqual(self.devices.owner(token, now=1000 + DEVICE_SECONDS - 1), OWNER)
        with self.assertRaises(ValueError): self.devices.owner(token, now=1000 + DEVICE_SECONDS)
        self.devices.revoke(token)
        with self.assertRaises(ValueError): self.devices.owner(token, now=1001)
        with self.assertRaises(ValueError): self.devices.owner('x' * 43, now=1001)  # Unknown pass.
        with self.assertRaises(ValueError): self.devices.issue('someone')

    def test_oldest_pass_gives_way_and_store_keeps_only_hashes(self):
        tokens = [self.devices.issue(OWNER, now=1000 + i) for i in range(MAX_DEVICES + 1)]
        with self.assertRaises(ValueError): self.devices.owner(tokens[0], now=2000)
        self.assertEqual(self.devices.owner(tokens[-1], now=2000), OWNER)
        self.assertNotIn(tokens[-1], repr(self.db.__dict__))

    def test_expired_google_session_falls_back_to_a_live_pass_for_the_owner_only(self):
        token = self.devices.issue(OWNER)
        creator, admin = self.identity(request(token)), self.identity.admin_session(request(token))
        self.assertEqual((creator.principal.subject, admin.subject), (OWNER, OWNER))
        self.assertEqual(creator.csrf_token, admin.csrf_token); self.assertGreaterEqual(len(creator.csrf_token), 32)
        for bad in (request(), request('y' * 43), request(token, scheme='http'), request(self.devices.issue(OTHER))):
            with self.assertRaises(ValueError): self.identity(bad)
            with self.assertRaises(ValueError): self.identity.admin_session(bad)

    def test_live_google_session_is_used_first(self):
        google = type('Google', (), {'__call__': lambda self, r: 'google creator', 'admin_session': lambda self, r: 'google admin'})()
        both = TrustedDeviceIdentity(google, self.devices, owners=[OWNER], csrf_secret=b's' * 32,
            creator_session=lambda *a: 'device', admin_session=lambda *a: 'device')
        self.assertEqual((both(request()), both.admin_session(request())), ('google creator', 'google admin'))

    def test_pass_issued_only_by_successful_callback_and_cleared_on_sign_out(self):
        app = FastAPI()
        @app.get('/auth/callback')
        def callback(ok: int = 1):
            response = RedirectResponse('/', status_code=303)
            if ok: response.set_cookie('__Host-byron_session', 'google-token', secure=True, httponly=True, path='/')
            return response
        @app.post('/auth/logout')
        def logout(request: Request):
            return Response(status_code=403)  # Google session already lapsed: the device still signs out.
        install_device_cookies(app, self.devices, OWNER)
        client = TestClient(app, base_url='https://testserver')
        failed = client.get('/auth/callback?ok=0', follow_redirects=False)
        self.assertNotIn(DEVICE_COOKIE, failed.headers.get('set-cookie', ''))
        signed = client.get('/auth/callback', follow_redirects=False)
        cookie = [c for c in signed.headers.get_list('set-cookie') if c.startswith(DEVICE_COOKIE + '=')]
        self.assertEqual(len(cookie), 1)
        for flag in ('HttpOnly', 'Secure', 'SameSite=lax', 'Path=/', f'Max-Age={DEVICE_SECONDS}'):
            self.assertIn(flag, cookie[0])
        token = cookie[0].split(';')[0].split('=', 1)[1]
        self.assertEqual(self.devices.owner(token), OWNER)
        out = client.post('/auth/logout', cookies={DEVICE_COOKIE: token})
        self.assertIn(DEVICE_COOKIE + '=""', out.headers.get('set-cookie', ''))
        with self.assertRaises(ValueError): self.devices.owner(token)


if __name__ == '__main__': unittest.main()
