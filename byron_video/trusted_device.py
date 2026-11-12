"""Trusted device: after a verified Google sign-in, this browser stays signed in for 30 days.

Google ID tokens last an hour, so the hourly sign-in is replaced by a random device pass
issued only by a successful Google callback. The pass is a Secure, HttpOnly, SameSite=Lax
__Host- cookie; the server keeps only its SHA-256 (with owner, expiry and a revoked flag) in
the private Firestore namespace. Signing out revokes it. A missing, unknown, expired or
revoked record denies access; a live Google session is always checked first.
"""
import hashlib
import hmac
import re
import secrets
import time

from .cloud_journal import _FirestoreBoundary
from .google_login import RANDOM, SHA, hashed, need

DEVICE_COOKIE = '__Host-byron_device'
DEVICE_SECONDS = 30 * 86400
MAX_DEVICES = 32
OWNER = re.compile(r'google_[0-9a-f]{64}\Z')


class FirestoreTrustedDevices(_FirestoreBoundary):
    def issue(self, owner, *, now=None):
        now = time.time() if now is None else now
        need(isinstance(owner, str) and OWNER.fullmatch(owner))
        token = secrets.token_urlsafe(32)
        ref = self._doc('trusted_devices/pool')
        def save(tx):
            current = self._read(ref, tx)
            entries = {} if current is None else dict(current['entries'])
            entries = {k: v for k, v in entries.items() if v['expires'] > now and v['revoked'] is False}
            if len(entries) >= MAX_DEVICES:  # Oldest pass gives way; never refuses a fresh Google sign-in.
                entries.pop(min(entries, key=lambda k: entries[k]['expires']))
            entries[hashed(token)] = {'owner': owner, 'expires': now + DEVICE_SECONDS, 'revoked': False}
            if current is None: tx.create(ref, {'entries': entries})
            else: tx.update(ref, {'entries': entries})
        self._run(save)
        return token

    def owner(self, token, *, now=None):
        now = time.time() if now is None else now
        need(isinstance(token, str) and RANDOM.fullmatch(token))
        pool = self._read(self._doc('trusted_devices/pool'))
        row = None if pool is None else pool['entries'].get(hashed(token))
        need(row is not None and row.get('revoked') is False and row.get('expires', 0) > now
             and isinstance(row.get('owner'), str) and OWNER.fullmatch(row['owner']))
        return row['owner']

    def revoke(self, token):
        need(isinstance(token, str) and RANDOM.fullmatch(token))
        ref = self._doc('trusted_devices/pool'); key = hashed(token)
        def drop(tx):
            current = self._read(ref, tx)
            if current is None or key not in current['entries']: return
            entries = dict(current['entries']); entries[key] = {**entries[key], 'revoked': True}
            tx.update(ref, {'entries': entries})
        self._run(drop)


class TrustedDeviceIdentity:
    """Google session first; otherwise a live device pass for one of the allowed owners."""
    def __init__(self, identity, devices, *, owners, csrf_secret, creator_session, admin_session):
        need(isinstance(csrf_secret, bytes) and len(csrf_secret) >= 32 and owners and all(OWNER.fullmatch(o) for o in owners))
        self._identity, self._devices, self._owners, self._secret = identity, devices, frozenset(owners), csrf_secret
        self._creator, self._admin = creator_session, admin_session

    def _device(self, request):
        try:
            need(request.url.scheme == 'https')
            token = request.cookies.get(DEVICE_COOKIE)
            owner = self._devices.owner(token)
            need(owner in self._owners)
            csrf = hmac.new(self._secret, ('csrf-device-v1\0' + owner + '\0' + hashed(token)).encode(), hashlib.sha256).hexdigest()
            return owner, csrf
        except Exception:
            raise ValueError('A verified, permitted session is required.') from None

    def __call__(self, request):
        try: return self._identity(request)
        except Exception: return self._creator(*self._device(request))

    def admin_session(self, request):
        try: return self._identity.admin_session(request)
        except Exception: return self._admin(*self._device(request))


def install_device_cookies(app, devices, owner):
    """Issue a pass only on a successful Google callback; revoke it on sign-out."""
    need(OWNER.fullmatch(owner))
    @app.middleware('http')
    async def device_pass(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if (path == '/auth/callback' and response.status_code == 303 and any(
                k == b'set-cookie' and v.startswith(b'__Host-byron_session=') for k, v in response.raw_headers)):
            from starlette.concurrency import run_in_threadpool
            try:
                token = await run_in_threadpool(devices.issue, owner)
                response.set_cookie(DEVICE_COOKIE, token, max_age=DEVICE_SECONDS, secure=True, httponly=True, samesite='lax', path='/')
            except Exception:
                pass  # The hour-long Google session still works; the device simply is not remembered.
        elif path == '/auth/logout' and request.method == 'POST':
            token = request.cookies.get(DEVICE_COOKIE)
            if isinstance(token, str) and RANDOM.fullmatch(token):
                from starlette.concurrency import run_in_threadpool
                try: await run_in_threadpool(devices.revoke, token)
                except Exception: pass
            response.delete_cookie(DEVICE_COOKIE, secure=True, httponly=True, samesite='lax', path='/')
        return response
