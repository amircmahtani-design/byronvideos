"""Identity contract tests use a fake verifier, NOT live signature/certificate QA."""
import unittest
from types import SimpleNamespace
from byron_video.google_identity import GoogleIdentityResolver

SUB = '100000000001'
OTHER = '100000000002'
TOKEN = 'synthetic.signed.fixture'


class IdentityChecks(unittest.TestCase):
    def claims(self, **changes):
        return dict(iss='https://accounts.google.com',aud='synthetic-client',sub=SUB,iat=950,exp=1050,**changes)

    def resolver(self, claims=None, **changes):
        return GoogleIdentityResolver(**dict(dict(audience='synthetic-client',allowed_subjects=[SUB,OTHER],admin_subjects=[SUB],
            csrf_secret=b'synthetic-private-csrf-secret-0000',revoked=lambda owner,digest:False,
            token_verifier=lambda token,audience:self.claims() if claims is None else claims,clock=lambda:1000),**changes))

    def request(self, token=TOKEN, scheme='https'):
        return SimpleNamespace(url=SimpleNamespace(scheme=scheme),cookies={'__Host-byron_session':token})

    def test_verified_subject_not_email_controls_identity(self):
        resolver=self.resolver({**self.claims(),'email':'attacker@example.test'})
        creator=resolver(self.request()); admin=resolver.admin_session(self.request())
        self.assertTrue(creator.principal.subject.startswith('google_'))
        self.assertEqual(creator.principal.subject,admin.subject)
        self.assertEqual(creator.csrf_token,admin.csrf_token)
        self.assertEqual(len(creator.csrf_token),64)
        self.assertNotIn('attacker',creator.principal.subject)

    def test_verifier_receives_explicit_audience(self):
        calls=[]
        def verify(token,audience):
            calls.append((token,audience));return self.claims()
        self.resolver(token_verifier=verify)(self.request())
        self.assertEqual(calls,[(TOKEN,'synthetic-client')])

    def test_bad_signature_or_missing_verifier_runtime_fails_closed(self):
        def fail(*args):raise ImportError('private token contents')
        with self.assertRaisesRegex(ValueError,'verified, permitted') as raised:
            self.resolver(token_verifier=fail)(self.request())
        self.assertNotIn('private token',str(raised.exception))

    def test_claims_fail_closed(self):
        for update in [dict(iss='https://attacker.test'),dict(aud='other'),dict(azp='other'),dict(sub='not-allowed'),
                       dict(exp=999),dict(iat=1001),dict(exp=True),dict(exp=float('nan')),dict(exp=999999),dict(sub='name@example.test')]:
            with self.subTest(update=update),self.assertRaises(ValueError):
                self.resolver({**self.claims(),**update})(self.request())

    def test_email_allowlist_rejected(self):
        with self.assertRaises(ValueError):self.resolver(allowed_subjects=['name@example.test'])

    def test_admin_requires_separate_subject_allowlist(self):
        resolver=self.resolver({**self.claims(),'sub':OTHER})
        self.assertIsNotNone(resolver(self.request()))
        with self.assertRaises(ValueError):resolver.admin_session(self.request())

    def test_revoked_or_unknown_status_denies(self):
        for state in [True,None,0]:
            with self.subTest(state=state),self.assertRaises(ValueError):
                self.resolver(revoked=lambda *args:state)(self.request())

    def test_cookie_and_https_required(self):
        for request in [self.request(scheme='http'),self.request(token='name@example.test'),self.request(token='x'*20001)]:
            with self.assertRaises(ValueError):self.resolver()(request)

    def test_csrf_is_bound_to_token_and_server_secret(self):
        one=self.resolver()(self.request()).csrf_token
        two=self.resolver()(self.request(token='synthetic.other.fixture')).csrf_token
        three=self.resolver(csrf_secret=b'x'*32)(self.request()).csrf_token
        self.assertNotEqual(one,two);self.assertNotEqual(one,three)

    def test_alternate_valid_issuer_preserves_owner(self):
        one=self.resolver()(self.request()).principal.subject
        two=self.resolver({**self.claims(),'iss':'accounts.google.com'})(self.request()).principal.subject
        self.assertEqual(one,two)


if __name__=='__main__':unittest.main()
