"""Metadata identity fakes only; no Google SDK or token request is performed."""
import os,unittest
from unittest.mock import patch
from byron_video.workload_credentials import create_attached_credentials,METADATA
from byron_video.vertex_review import WorkloadToken
PROJECT='fixture-project';EMAIL='fixture-service@fixture-project.iam.gserviceaccount.com'
CONFIG={'target':{'project_id':PROJECT},'runtime':{'service_service_account':EMAIL,'worker_service_account':EMAIL}}
class Base:
    def __init__(self):self.token=None;self.expiry=None
    def before_request(self,request,method,url,headers):
        if self.token is None:self.refresh(request)
        headers['authorization']='Bearer '+self.token
class Response:
    status_code=200;headers={'metadata-flavor':'Google'}
    def __init__(self,data):self.data=data
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_bytes(self):yield self.data
class Client:
    calls=[];project=PROJECT;email=EMAIL
    def __init__(self,**kwargs):self.kwargs=kwargs;Client.calls.append(('client',kwargs))
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def close(self):pass
    def stream(self,method,url,**kwargs):
        Client.calls.append((method,url,kwargs))
        return Response((self.project if url.endswith('project-id') else self.email).encode())
class Reader:
    calls=0
    def __init__(self,**kwargs):pass
    def __call__(self):Reader.calls+=1;return WorkloadToken('synthetic-no-live-token',2000)
class WorkloadCredentialChecks(unittest.TestCase):
    def setUp(self):Client.calls=[];Reader.calls=0;Client.project=PROJECT;Client.email=EMAIL;self.active=True
    def bundle(self,**kwargs):return create_attached_credentials(CONFIG,activation_check=lambda:self.active,client_factory=Client,
        token_reader_factory=Reader,credentials_base=Base,**kwargs)
    def test_inert_construction_disabled_execution(self):
        b=self.bundle();self.assertEqual(Client.calls,[])
        with self.assertRaises(ValueError):b.verify_identity()
        self.assertEqual(Reader.calls,0)
    def test_identity_required_before_tokens_and_no_sdk_request_fallback(self):
        b=self.bundle(enabled=True)
        with self.assertRaises(ValueError):b.token_reader()
        self.assertTrue(b.verify_identity());self.assertEqual(b.token_reader(),'synthetic-no-live-token')
        self.assertEqual(b.native_token_reader().value,'synthetic-no-live-token')
        headers={};b.credentials.before_request(lambda: self.fail('ambient SDK transport invoked'),'GET','https://storage.googleapis.com/test',headers)
        self.assertEqual(headers['authorization'],'Bearer synthetic-no-live-token')
        for call in Client.calls:
            if call[0]=='client':self.assertEqual(call[1],dict(timeout=5,follow_redirects=False,trust_env=False))
            else:self.assertTrue(call[1] in (METADATA+'project/project-id',METADATA+'instance/service-accounts/default/email'))
    def test_wrong_project_or_account_fails_without_token(self):
        for field in ('project','email'):
            Client.project=PROJECT;Client.email=EMAIL;setattr(Client,field,'wrong')
            with self.assertRaises(RuntimeError):self.bundle(enabled=True).verify_identity()
        self.assertEqual(Reader.calls,0)
    def test_expired_activation_denies_cached_credentials(self):
        b=self.bundle(enabled=True);b.verify_identity();self.active=False
        with self.assertRaises(ValueError):b.credentials.before_request(None,'GET','https://storage.googleapis.com/',{})
    def test_endpoint_override_and_non_google_destination_denied(self):
        b=self.bundle(enabled=True);b.verify_identity()
        with patch.dict(os.environ,{'FIRESTORE_EMULATOR_HOST':'localhost:8080'}),self.assertRaises(ValueError):b.token_reader()
        for url in ('http://storage.googleapis.com/','https://evil.test/','https://googleapis.com.evil.test/','https://user@storage.googleapis.com/'):
            with self.subTest(url=url),self.assertRaises(ValueError):b.credentials.before_request(None,'GET',url,{})
    def test_missing_google_sdk_no_metadata_attempt(self):
        import builtins
        original=builtins.__import__
        def missing(name,*a,**kw):
            if name.startswith('google.auth'):raise ImportError()
            return original(name,*a,**kw)
        with patch('builtins.__import__',side_effect=missing),self.assertRaises(RuntimeError):
            create_attached_credentials(CONFIG,activation_check=lambda:True,enabled=True,client_factory=Client)
        self.assertEqual(Client.calls,[])

if __name__=='__main__':unittest.main()
