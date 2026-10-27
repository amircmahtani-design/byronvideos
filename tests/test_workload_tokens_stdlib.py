"""Offline injected metadata/SDK fixtures, never a credential lookup."""
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace
from byron_video.workload_tokens import AttachedWorkloadTokenReader,SCOPE
EMAIL='fixture-worker@fixture-project.iam.gserviceaccount.com'
class Response:
    status_code=200;headers={'metadata-flavor':'Google'}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_bytes(self):yield b'{}'
class Client:
    calls=[]
    def __init__(self,**kw):self.kw=kw;Client.calls.append(('client',kw))
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def close(self):pass
    def stream(self,*args,**kw):Client.calls.append((args,kw));return Response()
class Creds:
    bad_url=None;bad_identity=False;seconds=100
    def __init__(self,**kw):
        self.service_account_email=kw['service_account_email'];self.universe_domain=kw['universe_domain'];self.token='fixture-access-token-not-real'
        self.expiry=datetime.fromtimestamp(1000+self.seconds,timezone.utc)
    def refresh(self,request):
        url=self.bad_url or 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/'+EMAIL+'/token?scopes=fixture'
        request(url,headers={'metadata-flavor':'Google'})
        if self.bad_identity:self.service_account_email='other'
class TokenChecks(unittest.TestCase):
    def setUp(self):Client.calls=[];Creds.bad_url=None;Creds.bad_identity=False;Creds.seconds=100
    def reader(self,**kw):return AttachedWorkloadTokenReader(service_account_email=EMAIL,activation_check=lambda:True,
        client_factory=Client,credentials_factory=Creds,now=lambda:1000,**kw)
    def test_disabled_no_sdk_or_transport(self):
        with self.assertRaises(ValueError):self.reader()()
        self.assertEqual(Client.calls,[])
    def test_exact_attached_identity_short_lived_no_proxy(self):
        token=self.reader(enabled=True)();self.assertEqual(token.expires_at,1100)
        self.assertNotIn(token.value,repr(token));self.assertFalse(Client.calls[0][1]['trust_env']);self.assertFalse(Client.calls[0][1]['follow_redirects'])
    def test_no_other_host_account_key_or_https_fallback(self):
        for url in ['https://oauth2.googleapis.com/token','http://169.254.169.254/token',
                    'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
                    'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/'+EMAIL+'/../default/token']:
            Creds.bad_url=url
            with self.subTest(url=url),self.assertRaises(RuntimeError):self.reader(enabled=True)()
    def test_mismatched_identity_rejected(self):
        Creds.bad_identity=True
        with self.assertRaises(RuntimeError):self.reader(enabled=True)()
    def test_nonexpiring_or_expired_token_rejected(self):
        for seconds in (0,20,4000):
            Creds.seconds=seconds
            with self.subTest(seconds=seconds),self.assertRaises(RuntimeError):self.reader(enabled=True)()
    def test_missing_sdk_fails_closed_no_network(self):
        from unittest.mock import patch
        import builtins
        original=builtins.__import__
        def missing(name,*a,**kw):
            if name.startswith('google.auth'):raise ImportError()
            return original(name,*a,**kw)
        r=AttachedWorkloadTokenReader(service_account_email=EMAIL,activation_check=lambda:True,enabled=True,client_factory=Client)
        with patch('builtins.__import__',side_effect=missing),self.assertRaises(RuntimeError):r()
        self.assertEqual(Client.calls,[])
