"""Explicit attached-account credentials; no ADC, key files or import-time I/O.

Construction is inert. Call verify_identity before constructing SDK clients.
Refresh ignores the SDK-provided transport and uses the bounded metadata reader.
"""
from dataclasses import dataclass
from datetime import datetime,timezone
import os,re,time,threading
from urllib.parse import urlsplit
from .hosted_control import require
from .provider_execution import _wall_deadline
from .workload_tokens import AttachedWorkloadTokenReader

METADATA='http://metadata.google.internal/computeMetadata/v1/'

@dataclass(frozen=True)
class AttachedCredentialsBundle:
    credentials:object
    token_reader:object
    verify_identity:object
    native_token_reader:object

def create_attached_credentials(config,*,role='service',activation_check,enabled=False,client_factory=None,
                                token_reader_factory=None,credentials_base=None,now=time.time):
    require(role in ('service','worker'),'Explicit workload role required.')
    project=config['target']['project_id'];email=config['runtime'][role+'_service_account']
    require(isinstance(project,str) and re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]',project)
        and isinstance(email,str) and re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]@'+re.escape(project)+r'\.iam\.gserviceaccount\.com',email),
        'Exact workload account in configured project required.')
    require(callable(activation_check),'Explicit runtime activation checker required.')
    if credentials_base is None:
        try:from google.auth.credentials import Credentials as credentials_base
        except ImportError:raise RuntimeError('Pinned Google authentication runtime unavailable; no metadata request attempted.') from None
    mutex=threading.RLock();state={'verified':False}
    def active():
        require(enabled is True and activation_check() is True,'Attached credentials are disabled or activation expired.')
        require(not any(os.environ.get(k) for k in ('STORAGE_EMULATOR_HOST','FIRESTORE_EMULATOR_HOST','GOOGLE_API_USE_MTLS_ENDPOINT','GOOGLE_API_USE_CLIENT_CERTIFICATE')),
                'Production workload forbids ambient SDK endpoint or certificate overrides.')
    def verify_identity():
        with mutex:
            active();state['verified']=False
            factory=client_factory
            if factory is None:
                import httpx
                factory=httpx.Client
            try:
                with factory(timeout=5,follow_redirects=False,trust_env=False) as client,_wall_deadline(client,15,time.monotonic) as check:
                    values=[]
                    for key in ('project/project-id','instance/service-accounts/default/email'):
                        active();check()
                        with client.stream('GET',METADATA+key,headers={'Metadata-Flavor':'Google','Accept-Encoding':'identity'}) as response:
                            require(response.status_code==200 and response.headers.get('Metadata-Flavor',response.headers.get('metadata-flavor'))=='Google'
                                and response.headers.get('content-encoding','identity')=='identity','Attached identity metadata is unavailable.')
                            raw=bytearray()
                            for block in response.iter_bytes():
                                check();require(len(raw)+len(block)<=1024,'Attached identity metadata exceeds bound.');raw.extend(block)
                            values.append(bytes(raw).decode('ascii'))
                active();require(values==[project,email],'Attached workload project/account differs from private configuration.')
                state['verified']=True;return True
            except Exception:
                state['verified']=False
                raise RuntimeError('Attached identity could not be verified; no SDK credential fallback attempted.') from None
    reader_factory=token_reader_factory or AttachedWorkloadTokenReader
    native_reader=reader_factory(service_account_email=email,activation_check=activation_check,enabled=enabled,client_factory=client_factory,now=now)
    def token_reader():
        with mutex:
            active();require(state['verified'],'Verify attached workload identity before requesting tokens.')
            # Recheck identity on every refresh, including SDK automatic refresh.
            verify_identity();token=native_reader();active();return token
    class ExactAttachedCredentials(credentials_base):
        def __init__(self):super().__init__()
        @property
        def service_account_email(self):return email
        @property
        def universe_domain(self):return 'googleapis.com'
        def refresh(self,request):
            token=token_reader();self.token=token.value
            self.expiry=datetime.fromtimestamp(token.expires_at,timezone.utc).replace(tzinfo=None)
        def before_request(self,request,method,url,headers):
            active();require(state['verified'],'Attached identity has not been verified.')
            parsed=urlsplit(url)
            require(parsed.scheme=='https' and parsed.hostname is not None
                and (parsed.hostname=='googleapis.com' or parsed.hostname.endswith('.googleapis.com'))
                and parsed.port in (None,443) and not parsed.username and not parsed.password,
                'Attached credentials may authenticate only explicit Google API TLS endpoints.')
            return super().before_request(request,method,url,headers)
        def get_cred_info(self):return {'credential_source':'explicit-attached-workload'}
    return AttachedCredentialsBundle(ExactAttachedCredentials(),lambda:token_reader().value,verify_identity,token_reader)
