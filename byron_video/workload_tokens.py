"""Disabled attached-workload OAuth access-token reader; no key-file fallback.

Only metadata for the independently configured service account may be read after
explicit activation. This is not login authentication and grants no IAM access.
"""
from datetime import datetime,timezone
from types import SimpleNamespace
from urllib.parse import urlsplit,unquote
import re,time
from .automated_review import need
from .provider_execution import _wall_deadline
from .vertex_review import WorkloadToken

SCOPE='https://www.googleapis.com/auth/cloud-platform'

class AttachedWorkloadTokenReader:
    def __init__(self,*,service_account_email,activation_check,enabled=False,client_factory=None,credentials_factory=None,now=time.time):
        need(isinstance(service_account_email,str) and re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com',service_account_email),
             'Exact private workload service-account configuration required.')
        self.email,self.activation,self.enabled=service_account_email,activation_check,enabled is True
        self.client_factory,self.credentials_factory,self.now=client_factory,credentials_factory,now
    def __call__(self):
        need(self.enabled and callable(self.activation) and self.activation() is True,'Attached workload token reader is disabled.')
        factory=self.credentials_factory
        if factory is None:
            try:from google.auth.compute_engine import Credentials
            except ImportError:raise RuntimeError('Pinned Google auth runtime unavailable; no metadata request attempted.') from None
            factory=Credentials
        credentials=factory(service_account_email=self.email,scopes=[SCOPE],universe_domain='googleapis.com')
        client_factory=self.client_factory
        if client_factory is None:
            import httpx
            client_factory=httpx.Client
        try:
            with client_factory(timeout=5,follow_redirects=False,trust_env=False) as client,_wall_deadline(client,20,time.monotonic) as check:
                calls=0;named_info_read=False
                def request(url,method='GET',body=None,headers=None,timeout=None,**kwargs):
                    nonlocal calls,named_info_read
                    calls+=1;check();need(calls<=5 and self.activation() is True,'Metadata request bound or activation expired.')
                    accounts='/computeMetadata/v1/instance/service-accounts/';prefix=accounts+self.email+'/'
                    parsed=urlsplit(url);path=unquote(parsed.path)
                    # google-auth always fetches the token via the 'default' alias after reading the
                    # named account's info; allow only that, once the exact account was read.
                    named=path.startswith(prefix) and path[len(prefix):] in ('','token')
                    default_token=named_info_read and path==accounts+'default/token'
                    need(parsed.scheme=='http' and parsed.hostname=='metadata.google.internal' and parsed.port in (None,80)
                         and not parsed.username and not parsed.password and not parsed.fragment and method=='GET' and body is None
                         and (named or default_token) and not kwargs,
                         'Only the configured attached-account metadata may be read; no credentials endpoint fallback.')
                    needed={str(k).lower():v for k,v in (headers or {}).items()}
                    need(needed.get('metadata-flavor')=='Google' and not any(k in needed for k in ('authorization','proxy-authorization')),
                         'Required Google metadata request header missing.')
                    with client.stream('GET',url,headers={'Metadata-Flavor':'Google','Accept-Encoding':'identity'}) as response:
                        need(response.status_code==200 and response.headers.get('Metadata-Flavor',response.headers.get('metadata-flavor'))=='Google'
                             and response.headers.get('content-encoding','identity')=='identity','Attached metadata response unavailable.')
                        raw=bytearray()
                        for part in response.iter_bytes():
                            check();need(len(raw)+len(part)<=65536,'Metadata response exceeds bound.');raw.extend(part)
                        if path==prefix:named_info_read=True
                        return SimpleNamespace(status=200,data=bytes(raw),headers=dict(response.headers))
                credentials.refresh(request)
            need(self.activation() is True and credentials.service_account_email==self.email and credentials.universe_domain=='googleapis.com',
                 'Refreshed workload identity differs from pinned private configuration.')
            expiry=credentials.expiry
            need(isinstance(expiry,datetime),'Expiring workload credential required.')
            if expiry.tzinfo is None:expiry=expiry.replace(tzinfo=timezone.utc)
            expires=expiry.timestamp();token=credentials.token
            need(isinstance(token,str) and 16<=len(token)<=8192 and token.isascii() and not any(c.isspace() for c in token)
                 and self.now()+30<expires<=self.now()+3600,'Bounded short-lived workload access token required.')
            return WorkloadToken(token,expires)
        except Exception:raise RuntimeError('Attached workload token unavailable; no provider request or credential fallback attempted.') from None
