"""Disabled Google OIDC authorization-code login; no account setup or discovery.

Only openid/email identity scopes, no offline access/refresh, provider credentials,
Google Cloud grants or Firebase. Explicit clients/configuration are mandatory.
"""
import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from urllib.parse import urlencode,urlsplit

from .cloud_journal import _FirestoreBoundary
from .google_identity import ISSUERS,SUBJECT,verify_google_id_token
from .provider_execution import _wall_deadline

AUTHORIZATION_URL='https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL='https://oauth2.googleapis.com/token'
LOGIN_COOKIE='__Host-byron_login'
SESSION_COOKIE='__Host-byron_session'
RANDOM=re.compile(r'[A-Za-z0-9_-]{43,128}\Z')
SHA=re.compile(r'[0-9a-f]{64}\Z')


def need(ok,message='Login could not be verified.'):
    if not ok:raise ValueError(message)


def hashed(value):return hashlib.sha256(value.encode()).hexdigest()


def owner_for(subject):return 'google_'+hashed('accounts.google.com\0'+subject)


class FirestoreLoginJournal(_FirestoreBoundary):
    """One-use browser-bound login transactions and allowlisted session registry.

    Missing session records deny access; absence never means implicitly valid.
    Tokens/codes are never stored. PKCE verifier stays in a private short-lived
    transaction record, never in browser JS or a public object store.
    """
    def begin(self,state,record):
        need(RANDOM.fullmatch(state) and set(record)=={'browser_sha256','nonce','verifier','expires','old_token_sha256'})
        ref=self._doc('login_states/pool')
        def write(tx):
            previous=self._read(ref,tx)
            entries={} if previous is None else dict(previous['entries'])
            entries={key:row for key,row in entries.items() if row['expires']>record['expires']-300}
            key=hashed(state)
            need(key not in entries and len(entries)<128,'Login admission is temporarily full; wait before starting again.')
            entries[key]={**record,'consumed':False}
            if previous is None:tx.create(ref,{'entries':entries})
            else:tx.update(ref,{'entries':entries})
        self._run(write)

    def consume(self,state,browser,now):
        need(isinstance(state,str) and RANDOM.fullmatch(state) and isinstance(browser,str) and RANDOM.fullmatch(browser))
        ref=self._doc('login_states/pool')
        def take(tx):
            pool=self._read(ref,tx);entries={} if pool is None else dict(pool['entries'])
            key=hashed(state);row=entries.get(key)
            need(row is not None and row.get('consumed') is False and row.get('expires',0)>now
                 and hmac.compare_digest(row['browser_sha256'],hashed(browser)))
            entries[key]={**row,'consumed':True,'verifier':None,'nonce':None}
            tx.update(ref,{'entries':entries})
            return row
        return self._run(take)

    def rotate(self,owner,token_hash,expires,old_hash=None,*,now=None):
        now=time.time() if now is None else now
        need(isinstance(owner,str) and re.fullmatch(r'google_[0-9a-f]{64}',owner) and SHA.fullmatch(token_hash)
             and type(expires) in (int,float) and math.isfinite(expires) and now<expires<=now+3600)
        need(old_hash is None or (isinstance(old_hash,str) and SHA.fullmatch(old_hash)))
        ref=self._doc('login_sessions/pool')
        def save(tx):
            current=self._read(ref,tx)
            entries={} if current is None else dict(current['entries'])
            entries={key:row for key,row in entries.items() if row['expires']>now}
            need(token_hash not in entries and len(entries)<256,'Session admission is full or token reused.')
            if old_hash in entries:entries[old_hash]={**entries[old_hash],'revoked':True}
            entries[token_hash]={'owner':owner,'expires':expires,'revoked':False}
            if current is None:tx.create(ref,{'entries':entries})
            else:tx.update(ref,{'entries':entries})
        self._run(save)

    def revoked(self,owner,token_hash,*,now=None):
        now=time.time() if now is None else now
        need(isinstance(token_hash,str) and SHA.fullmatch(token_hash))
        pool=self._read(self._doc('login_sessions/pool'))
        row=None if pool is None else pool['entries'].get(token_hash)
        return not (row is not None and row.get('owner')==owner and row.get('revoked') is False and row.get('expires',0)>now)

    def revoke(self,owner,token_hash):
        need(isinstance(token_hash,str) and SHA.fullmatch(token_hash))
        ref=self._doc('login_sessions/pool')
        def end(tx):
            pool=self._read(ref,tx);entries={} if pool is None else dict(pool['entries'])
            row=entries.get(token_hash);need(row is not None and row.get('owner')==owner)
            entries[token_hash]={**row,'revoked':True}
            tx.update(ref,{'entries':entries})
        self._run(end)


class GoogleCodeExchange:
    """One-shot bounded POST; an uncertain exchange requires a new login flow."""
    def __init__(self,client_secret_reader,*,enabled=False,client_factory=None,clock=time.monotonic):
        self.secret_reader,self.enabled,self.factory,self.clock=client_secret_reader,enabled is True,client_factory,clock
    def __call__(self,*,code,verifier,client_id,redirect_uri):
        need(self.enabled,'Google code exchange is disabled.')
        try:
            secret=self.secret_reader()
            need(isinstance(secret,str) and 16<=len(secret)<=512 and secret.isascii() and not any(c.isspace() for c in secret))
            factory=self.factory
            if factory is None:
                import httpx
                factory=httpx.Client
            with factory(timeout=10,follow_redirects=False,trust_env=False) as client,_wall_deadline(client,30,self.clock) as check:
                with client.stream('POST',TOKEN_URL,headers={'Accept':'application/json','Accept-Encoding':'identity'},data={
                    'code':code,'code_verifier':verifier,'client_id':client_id,'client_secret':secret,
                    'redirect_uri':redirect_uri,'grant_type':'authorization_code'}) as response:
                    need(response.status_code==200 and response.headers.get('content-encoding','identity').lower()=='identity')
                    data=bytearray()
                    for part in response.iter_bytes():
                        check();need(len(data)+len(part)<=128*1024);data.extend(part)
                    result=json.loads(data)
                    token=result.get('id_token')
                    need(isinstance(token,str) and token.isascii() and token.count('.')==2 and len(token)<=20000)
                    return token  # Do not persist returned access/refresh tokens.
        except Exception:
            raise ValueError('Google login exchange could not be verified. Start a new login; do not replay the code.') from None


class GoogleLogin:
    def __init__(self,*,origin,client_id,redirect_uri,subject_emails,journal,exchange,identity_resolver,
                 enabled=False,token_verifier=None,clock=time.time,activation_check=None):
        parsed=urlsplit(origin)
        need(parsed.scheme=='https' and parsed.netloc and parsed.path=='' and not parsed.username and not parsed.password
             and not parsed.query and not parsed.fragment,'Exact HTTPS origin required.')
        need(redirect_uri==origin+'/auth/callback','Only the exact registered callback URI is permitted.')
        need(isinstance(client_id,str) and 1<=len(client_id)<=512 and client_id.isascii())
        need(isinstance(subject_emails,dict) and bool(subject_emails) and all(isinstance(s,str) and SUBJECT.fullmatch(s)
             and isinstance(e,str) and 3<=len(e)<=254 and e.isascii() and '@' in e and not any(c.isspace() for c in e)
             for s,e in subject_emails.items()),'Private verified email-to-subject bindings required; never enroll by email alone.')
        self.origin,self.client_id,self.redirect_uri=origin,client_id,redirect_uri
        self.subject_emails=dict(subject_emails);self.journal,self.exchange,self.identity=journal,exchange,identity_resolver
        self.enabled,self.verify,self.clock=enabled is True,token_verifier or verify_google_id_token,clock
        self.activation_check=activation_check

    def _request(self,request,path):
        need(self.enabled,'Google login is disabled pending deployment acceptance.')
        if path!='/auth/logout':self._activation()
        need(str(request.base_url).rstrip('/')==self.origin and request.url.path==path)

    def _activation(self):
        if self.activation_check is not None:need(self.activation_check() is True,'Deployment acceptance expired.')

    def start(self,request):
        from fastapi.responses import RedirectResponse
        self._request(request,'/auth/login');need(not request.url.query)
        state,browser,nonce,verifier=(secrets.token_urlsafe(32) for _ in range(4))
        old=request.cookies.get(SESSION_COOKIE)
        old_hash=hashed(old) if isinstance(old,str) and old.isascii() and len(old)<=20000 else None
        self.journal.begin(state,dict(browser_sha256=hashed(browser),nonce=nonce,verifier=verifier,expires=self.clock()+300,old_token_sha256=old_hash))
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        response=RedirectResponse(AUTHORIZATION_URL+'?'+urlencode(dict(client_id=self.client_id,redirect_uri=self.redirect_uri,
            response_type='code',scope='openid email',state=state,nonce=nonce,code_challenge=challenge,code_challenge_method='S256',
            access_type='online',prompt='select_account')),status_code=303)
        response.set_cookie(LOGIN_COOKIE,browser,max_age=300,secure=True,httponly=True,samesite='lax',path='/')
        self._headers(response);return response

    @staticmethod
    def _headers(response):
        response.headers.update({'Cache-Control':'no-store','Pragma':'no-cache','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'"})

    def callback(self,request):
        from fastapi.responses import RedirectResponse
        self._request(request,'/auth/callback')
        pairs=list(request.query_params.multi_items());need(len(pairs)<=8 and len({k for k,v in pairs})==len(pairs))
        args=dict(pairs);need('error' not in args and set(args)<= {'code','state','scope','authuser','prompt','iss'})
        need(args.get('iss','https://accounts.google.com') in ISSUERS)
        code=args.get('code');need(isinstance(code,str) and 1<=len(code)<=2048 and code.isascii() and not any(c.isspace() for c in code))
        record=self.journal.consume(args.get('state'),request.cookies.get(LOGIN_COOKIE),self.clock())
        self._activation()
        token=self.exchange(code=code,verifier=record['verifier'],client_id=self.client_id,redirect_uri=self.redirect_uri)
        claims=self.verify(token,self.client_id)  # Cryptographic verification before any claim is trusted.
        need(isinstance(claims,dict))
        subject=claims.get('sub');nonce=claims.get('nonce');issued,expires=claims.get('iat'),claims.get('exp');now=self.clock()
        need(claims.get('iss') in ISSUERS and claims.get('aud')==self.client_id and claims.get('azp',self.client_id)==self.client_id
             and isinstance(subject,str) and subject in self.subject_emails and claims.get('email_verified') is True
             and isinstance(claims.get('email'),str) and claims['email'].casefold()==self.subject_emails[subject].casefold()
             and isinstance(nonce,str) and nonce.isascii() and hmac.compare_digest(nonce,record['nonce']))
        need(type(issued) in (int,float) and type(expires) in (int,float) and math.isfinite(issued) and math.isfinite(expires)
             and issued<=now<expires and 0<expires-issued<=7200)
        self._activation()
        self.journal.rotate(owner_for(subject),hashed(token),min(expires,now+3600),record['old_token_sha256'],now=now)
        response=RedirectResponse('/',status_code=303)
        response.set_cookie(SESSION_COOKIE,token,max_age=min(3600,int(expires-now)),secure=True,httponly=True,samesite='lax',path='/')
        response.delete_cookie(LOGIN_COOKIE,secure=True,httponly=True,samesite='lax',path='/')
        self._headers(response);return response

    def logout(self,request):
        from fastapi.responses import Response
        self._request(request,'/auth/logout')
        need(request.headers.get('origin')==self.origin and not request.url.query)
        session=self.identity(request);csrf=request.headers.get('x-csrf-token','')
        need(isinstance(csrf,str) and csrf.isascii() and hmac.compare_digest(csrf,session.csrf_token))
        token=request.cookies.get(SESSION_COOKIE);need(isinstance(token,str))
        self.journal.revoke(session.principal.subject,hashed(token))
        response=Response(status_code=204);response.delete_cookie(SESSION_COOKIE,secure=True,httponly=True,samesite='lax',path='/')
        self._headers(response);return response

    def mount(self,app):
        from fastapi import Request
        from fastapi.responses import JSONResponse
        def wrap(method):
            def route(request:Request):
                try:return method(request)
                except Exception:
                    response=JSONResponse({'detail':'Login unavailable or invalid; start a new sign-in.'},status_code=403)
                    response.delete_cookie(LOGIN_COOKIE,secure=True,httponly=True,samesite='lax',path='/');self._headers(response)
                    return response
            return route
        app.add_api_route('/auth/login',wrap(self.start),methods=['GET'])
        app.add_api_route('/auth/callback',wrap(self.callback),methods=['GET'])
        app.add_api_route('/auth/logout',wrap(self.logout),methods=['POST'])
