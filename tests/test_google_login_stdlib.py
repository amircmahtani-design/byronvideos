"""Offline fake-Google/fake-Firestore login contracts; not live login evidence."""
import copy
import unittest
from urllib.parse import urlsplit,parse_qs
from fastapi import FastAPI
from fastapi.testclient import TestClient
from byron_video.google_login import GoogleLogin,GoogleCodeExchange,FirestoreLoginJournal,LOGIN_COOKIE,SESSION_COOKIE,hashed,owner_for
from byron_video.google_identity import GoogleIdentityResolver
from test_cloud_journal_stdlib import Client,PROJECT


class LoginChecks(unittest.TestCase):
    def setUp(self):
        self.now=1000;self.db=Client();self.journal=FirestoreLoginJournal(self.db,project_id=PROJECT,namespace='synthetic_login',transaction_runner=self.db.run)
        self.claims={'iss':'https://accounts.google.com','aud':'synthetic-client','sub':'100000000001','email':'fixture@example.test','email_verified':True,'iat':990,'exp':2000}
        self.calls=[];self.counter=0;self.tokens={}
        def exchange(**kwargs):
            self.calls.append(kwargs);self.counter+=1;token=f'fixture.{self.counter}.signature';self.tokens[token]=copy.deepcopy(self.claims);return token
        self.identity=GoogleIdentityResolver(audience='synthetic-client',allowed_subjects=['100000000001'],admin_subjects=['100000000001'],csrf_secret=b'x'*32,
            revoked=lambda o,t:self.journal.revoked(o,t,now=self.now),token_verifier=lambda t,a:self.tokens[t],clock=lambda:self.now)
        self.login=GoogleLogin(origin='https://studio.example',client_id='synthetic-client',redirect_uri='https://studio.example/auth/callback',
            subject_emails={'100000000001':'fixture@example.test'},journal=self.journal,exchange=exchange,identity_resolver=self.identity,
            enabled=True,token_verifier=lambda t,a:self.tokens[t],clock=lambda:self.now)
        app=FastAPI();self.login.mount(app);self.client=TestClient(app,base_url='https://studio.example',follow_redirects=False)
    def start(self):
        response=self.client.get('/auth/login');self.assertEqual(response.status_code,303)
        args=parse_qs(urlsplit(response.headers['location']).query);self.claims['nonce']=args['nonce'][0]
        return args
    def finish(self,args):return self.client.get('/auth/callback',params={'state':args['state'][0],'code':'synthetic-code'})
    def test_login_pkce_fixed_redirect_and_cookie_security(self):
        args=self.start()
        self.assertEqual(args['scope'],['openid email']);self.assertEqual(args['code_challenge_method'],['S256'])
        self.assertEqual(args['redirect_uri'],['https://studio.example/auth/callback'])
        result=self.finish(args);self.assertEqual(result.status_code,303);self.assertEqual(result.headers['location'],'/')
        cookie=result.headers['set-cookie'];self.assertIn('HttpOnly',cookie);self.assertIn('Secure',cookie);self.assertIn('SameSite=lax',cookie);self.assertNotIn('Domain=',cookie)
        self.assertEqual(result.headers['referrer-policy'],'no-referrer');self.assertEqual(len(self.calls),1)
        self.assertFalse(self.journal.revoked(owner_for(self.claims['sub']),hashed(self.client.cookies.get(SESSION_COOKIE)),now=self.now))
    def test_replayed_callback_never_exchanges_twice(self):
        args=self.start();self.assertEqual(self.finish(args).status_code,303)
        self.assertEqual(self.finish(args).status_code,403);self.assertEqual(len(self.calls),1)
    def test_wrong_browser_missing_or_expired_state_denies_before_exchange(self):
        for mode in ('missing','wrong','expired'):
            args=self.start()
            if mode=='missing':self.client.cookies.clear()
            if mode=='wrong':self.client.cookies.set(LOGIN_COOKIE,'x'*43,domain='studio.example',path='/')
            if mode=='expired':self.now+=301
            self.assertEqual(self.finish(args).status_code,403)
        self.assertEqual(self.calls,[])
    def test_invalid_identity_claims_or_nonce_fail_closed(self):
        for patch in ({'nonce':'wrong'},{'email_verified':False},{'sub':'100000000002'},{'email':'other@example.test'},
                      {'aud':'other'},{'iss':'https://evil.example'},{'exp':999},{'iat':1001},{'azp':'other'}):
            original=copy.deepcopy(self.claims);args=self.start();self.claims.update(patch)
            self.assertEqual(self.finish(args).status_code,403,patch);self.claims=original
        self.assertFalse(any('/login_sessions/' in p for p in self.db.data))
    def test_signature_failure_never_creates_session(self):
        args=self.start();self.login.verify=lambda *a:(_ for _ in ()).throw(ValueError())
        self.assertEqual(self.finish(args).status_code,403)
        self.assertFalse(any('/login_sessions/' in p for p in self.db.data))
    def test_rotation_revokes_previous_registered_session(self):
        self.assertEqual(self.finish(self.start()).status_code,303);old=self.client.cookies.get(SESSION_COOKIE)
        self.assertEqual(self.finish(self.start()).status_code,303);new=self.client.cookies.get(SESSION_COOKIE)
        self.assertNotEqual(old,new);self.assertTrue(self.journal.revoked(owner_for(self.claims['sub']),hashed(old),now=self.now))
    def test_logout_requires_origin_and_csrf_then_revokes(self):
        self.assertEqual(self.finish(self.start()).status_code,303);token=self.client.cookies.get(SESSION_COOKIE)
        self.assertEqual(self.client.post('/auth/logout').status_code,403)
        request=self.client.build_request('POST','/auth/logout')
        from starlette.requests import Request
        scope={'type':'http','scheme':'https','method':'POST','path':'/auth/logout','server':('studio.example',443),
               'headers':[(b'cookie',f'{SESSION_COOKIE}={token}'.encode())]}
        csrf=self.identity(Request(scope)).csrf_token
        self.assertEqual(self.client.post('/auth/logout',headers={'origin':'https://studio.example','x-csrf-token':csrf}).status_code,204)
        self.assertTrue(self.journal.revoked(owner_for(self.claims['sub']),hashed(token),now=self.now))
    def test_no_open_redirect_or_host_header_trust(self):
        self.assertEqual(self.client.get('/auth/login?next=https://evil.example').status_code,403)
        self.assertEqual(self.client.get('/auth/login',headers={'host':'evil.example'}).status_code,403)
        args=self.start();self.assertEqual(self.client.get('/auth/callback',params=[('state',args['state'][0]),('state',args['state'][0]),('code','x')]).status_code,403)
        self.assertEqual(self.calls,[])
    def test_disabled_and_missing_session_fail_closed(self):
        self.login.enabled=False;self.assertEqual(self.client.get('/auth/login').status_code,403);self.assertEqual(self.db.data,{})
        self.assertTrue(self.journal.revoked(owner_for(self.claims['sub']),'a'*64,now=self.now))
        seen=[];transport=GoogleCodeExchange(lambda:seen.append(1))
        with self.assertRaises(ValueError):transport(code='x',verifier='x',client_id='x',redirect_uri='x')
        self.assertEqual(seen,[])
    def test_token_session_ttl_and_expired_registry_deny(self):
        self.claims['exp']=7000
        self.assertEqual(self.finish(self.start()).status_code,303)
        token=self.client.cookies.get(SESSION_COOKIE)
        self.assertTrue(self.journal.revoked(owner_for(self.claims['sub']),hashed(token),now=4601))

    def test_token_exchange_exact_endpoint_deadline_and_no_retry(self):
        from byron_video.google_login import TOKEN_URL
        class Reply:
            status_code=200;headers={}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_bytes(self):yield b'{"id_token":"fixture.token.signature","access_token":"discard"}'
        class HTTP:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def stream(self,*args,**kwargs):calls.append((args,kwargs));return Reply()
        for slow in (False,True):
            calls=[];opts=[]
            def factory(**kwargs):opts.append(kwargs);return HTTP()
            transport=GoogleCodeExchange(lambda:'synthetic-private-secret',enabled=True,client_factory=factory,
                clock=iter([0,0,31]).__next__ if slow else lambda:0)
            args=dict(code='synthetic-code',verifier='v'*43,client_id='synthetic-client',redirect_uri='https://studio.example/auth/callback')
            if slow:
                with self.assertRaises(ValueError):transport(**args)
            else:self.assertEqual(transport(**args),'fixture.token.signature')
            self.assertEqual(len(calls),1);self.assertEqual(calls[0][0],('POST',TOKEN_URL))
            self.assertEqual(calls[0][1]['data']['code_verifier'],'v'*43)
            self.assertEqual(opts,[dict(timeout=10,follow_redirects=False,trust_env=False)])

    def test_login_state_storage_is_fixed_and_live_slots_not_overwritten(self):
        slots={}
        for n in range(10000):
            state=('fixture_'+str(n)).ljust(43,'x');slots.setdefault(int(hashed(state),16)%128,state)
            if len(slots)==128:break
        record=dict(browser_sha256=hashed('b'*43),nonce='n'*43,verifier='v'*43,expires=1300,old_token_sha256=None)
        for state in slots.values():self.journal.begin(state,record)
        self.assertEqual(len(self.db.data),1)
        self.assertEqual(len(next(iter(self.db.data.values()))['entries']),128)
        state=next(iter(slots.values()))
        with self.assertRaises(ValueError):self.journal.begin(state,record)
        self.journal.begin(state,{**record,'expires':1600})
        self.assertEqual(len(self.db.data),1)
        self.assertEqual(len(next(iter(self.db.data.values()))['entries']),1)
        row=self.journal.consume(state,'b'*43,1301);self.assertEqual(row['expires'],1600)
        with self.assertRaises(ValueError):self.journal.consume(state,'b'*43,1301)

    def test_session_pool_is_bounded_and_missing_token_cannot_alias(self):
        owner=owner_for(self.claims['sub'])
        for n in range(256):self.journal.rotate(owner,format(n,'064x'),1500,now=1000)
        with self.assertRaises(ValueError):self.journal.rotate(owner,'f'*64,1600,now=1000)
        self.assertTrue(self.journal.revoked(owner,'f'*64,now=1000))
        self.journal.rotate(owner,'f'*64,2600,now=1501)
        self.assertTrue(self.journal.revoked(owner,'0'*64,now=1501))
        self.assertFalse(self.journal.revoked(owner,'f'*64,now=1501))
        self.assertEqual(len(self.db.data),1)
        self.assertEqual(len(next(iter(self.db.data.values()))['entries']),1)

    def test_expired_activation_blocks_start_and_code_exchange(self):
        self.login.activation_check=lambda:False
        self.assertEqual(self.client.get('/auth/login').status_code,403);self.assertEqual(self.db.data,{})
        self.login.activation_check=lambda:True;args=self.start()
        self.login.activation_check=lambda:False
        self.assertEqual(self.finish(args).status_code,403);self.assertEqual(self.calls,[])

    def test_uncertain_state_consume_never_replays_exchange(self):
        args=self.start();self.db.unknown_commit=True
        self.assertEqual(self.finish(args).status_code,403);self.assertEqual(self.finish(args).status_code,403);self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
