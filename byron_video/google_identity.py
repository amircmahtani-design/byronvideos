"""Signed Google OIDC identity → allowlisted creator/admin session adapter.

This verifier performs no login callback, cookie issuance, account creation,
access grant or credential lookup. Companion google_login implements a separately
disabled OIDC code flow. Configure private audience/subject allowlists and CSRF secret
explicitly. The default verifier cryptographically checks Google ID tokens; it
never interprets an email string or unverified JWT payload as identity.

Official verifier contract:
https://googleapis.dev/python/google-auth/latest/reference/google.oauth2.id_token.html
"""
import hashlib
import hmac
import math
import re
import time

from .admin import AdminSession
from .hosted_control import Principal
from .hosted_uploads import CreatorSession

SUBJECT = re.compile(r'[0-9]{8,128}\Z')
ISSUERS = frozenset({'accounts.google.com','https://accounts.google.com'})


def verify_google_id_token(token, audience):
    """Lazy dependency import; missing runtime or verification failure denies access.

    Certificate fetches occur only when this adapter is explicitly used in a
    configured runtime. This module never verifies tokens during import/setup.
    """
    try:
        from google.oauth2 import id_token
        from google.auth.transport.requests import Request
        # Bounded certificate transport. Only the library's Google cert URL is used.
        class BoundedRequest(Request):
            def __call__(self, url, method='GET', body=None, headers=None, timeout=10, **kwargs):
                return super().__call__(url=url,method=method,body=body,headers=headers,timeout=10,**kwargs)
        return id_token.verify_oauth2_token(token, BoundedRequest(), audience=audience, clock_skew_in_seconds=0)
    except Exception:
        raise ValueError('Signed Google identity could not be verified.') from None


class GoogleIdentityResolver:
    """Use only with a reviewed login flow issuing a Secure, HttpOnly, SameSite
    __Host- cookie (Path=/, no Domain). OAuth state and nonce verification belong
    to the separately configured google_login callback, not this resolver.

    token_verifier is an injection seam for local tests, not an alternative to
    signature verification. Production should use the default verifier.
    revoked(owner_id, token_sha256) must return exactly False for a valid session;
    failures/unknown state deny. It can consult a durable server-side revocation
    journal. No caller header controls the identity or allowlist.
    """
    def __init__(self, *, audience, allowed_subjects, admin_subjects, csrf_secret,
                 revoked, cookie_name='__Host-byron_session', token_verifier=None, clock=time.time):
        if (not isinstance(audience,str) or not audience or len(audience)>512
                or not isinstance(csrf_secret,bytes) or len(csrf_secret)<32 or not callable(revoked)):
            raise ValueError('Explicit private identity configuration is required.')
        allowed, admins = frozenset(allowed_subjects), frozenset(admin_subjects)
        if not allowed or not admins <= allowed or not all(isinstance(x,str) and SUBJECT.fullmatch(x) for x in allowed):
            raise ValueError('Configure immutable Google subject allowlists, not emails.')
        if not re.fullmatch(r'__Host-[A-Za-z0-9_-]{1,80}',cookie_name):
            raise ValueError('A host-only secure session cookie name is required.')
        self._audience, self._allowed, self._admins = audience,allowed,admins
        self._secret, self._revoked, self._cookie = csrf_secret,revoked,cookie_name
        self._verify, self._clock = token_verifier or verify_google_id_token,clock

    def _session(self, request, admin=False):
        try:
            if request.url.scheme != 'https':
                raise ValueError()
            token = request.cookies.get(self._cookie)
            if not isinstance(token,str) or len(token)>20000 or token.count('.')!=2 or not token.isascii():
                raise ValueError()
            claims = self._verify(token,self._audience)  # Signature/issuer/audience verification FIRST.
            if not isinstance(claims,dict):
                raise ValueError()
            subject = claims.get('sub')
            if (claims.get('iss') not in ISSUERS or claims.get('aud')!=self._audience
                    or claims.get('azp',self._audience)!=self._audience
                    or subject not in self._allowed or (admin and subject not in self._admins)):
                raise ValueError()
            now = self._clock()
            issued, expires = claims.get('iat'),claims.get('exp')
            if (type(issued) not in (int,float) or type(expires) not in (int,float)
                    or not math.isfinite(issued) or not math.isfinite(expires)
                    or issued>now or expires<=now or expires<=issued or expires-issued>7200):
                raise ValueError()
            # Canonical issuer prevents alternate issuer spelling changing the owner.
            owner = 'google_' + hashlib.sha256(('accounts.google.com\0'+subject).encode()).hexdigest()
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            if self._revoked(owner,token_hash) is not False:
                raise ValueError()
            csrf = hmac.new(self._secret,('csrf-v1\0'+self._audience+'\0'+owner+'\0'+token_hash).encode(),hashlib.sha256).hexdigest()
            return owner,csrf
        except Exception:
            raise ValueError('A verified, permitted session is required.') from None

    def __call__(self, request):
        owner,csrf = self._session(request)
        return CreatorSession(Principal(owner),csrf)

    def admin_session(self, request):
        owner,csrf = self._session(request,admin=True)
        return AdminSession(owner,csrf)
