"""Packaged declarative private-preview worker graph; no user Python required.

Only explicit signed policy/acceptance permits activation. Imports never discover
credentials, create SDK clients, contact metadata, or submit provider work.
"""
import copy,hashlib,json,time
from pathlib import Path
from .hosted_control import digest,canonical,require,validate_private_plan
from .deployment_plan import validate_config
from .preview_runtime import verify_runtime_authorization
from .original_input import OriginalInputRequestLoader
from .worker_bootstrap import WorkerBindings,validate_runtime


class WorkerActivation:
    def __init__(self,config,plan,acceptance,reviewer_public_key,policy,*,now=time.time):
        self.config=copy.deepcopy(config);self.plan=copy.deepcopy(plan)
        self.acceptance=copy.deepcopy(acceptance);self.key=bytes(reviewer_public_key);self.policy=policy;self.now=now
    def __call__(self):
        value=verify_runtime_authorization(self.acceptance,self.key,digest(self.config),digest(self.plan),
            preview_contract=self.plan.get('preview_contract'),now=self.now())
        require(value['evidence']['handoff_adapter']==OriginalInputRequestLoader(digest(self.config)).release_sha256,
                'Worker original-input adapter differs from signed acceptance.')
        require(self.plan.get('preview_contract') is not None,'Packaged worker supports explicitly approved private-preview mode only.')
        require(self.plan.get('exact_film_contract') is not None,'Packaged studio requires the reviewed exact-film completion contract.')
        if self.plan.get('exact_film_contract') is not None:
            from .exact_film_runtime import validate_contract
            validate_contract(self.plan['exact_film_contract'])
        self.policy.validate()
        infrastructure=self.policy.validate_infrastructure(self.config)
        require(infrastructure['requires_separate_reservation'] is False,
                'Quoted infrastructure requires a separate reservation adapter; no implied cloud dollar cap.')
        return True


class ExactFilmActivation:
    """Current runtime/compute scope only; never constructs a paid policy."""
    def __init__(self,config,plan,acceptance,reviewer_public_key,infrastructure_envelope,infrastructure_public_key,*,now=time.time):
        self.config=copy.deepcopy(config);self.plan=copy.deepcopy(plan);self.acceptance=copy.deepcopy(acceptance)
        self.key=bytes(reviewer_public_key);self.infrastructure=copy.deepcopy(infrastructure_envelope)
        self.infrastructure_key=bytes(infrastructure_public_key);self.now=now
    def __call__(self):
        value=verify_runtime_authorization(self.acceptance,self.key,digest(self.config),digest(self.plan),
            preview_contract=self.plan.get('preview_contract'),now=self.now())
        require(value['evidence']['handoff_adapter']==OriginalInputRequestLoader(digest(self.config)).release_sha256,
                'Render-only original-input lineage differs from signed runtime.')
        from .exact_film_runtime import validate_contract
        from .infrastructure_authorization import validate_exact_infrastructure
        validate_contract(self.plan.get('exact_film_contract'))
        validate_exact_infrastructure(self.infrastructure,self.infrastructure_key,self.config,self.plan,now=self.now)
        return True

    def for_job(self,job):
        self()
        from .infrastructure_authorization import validate_exact_infrastructure
        validate_exact_infrastructure(self.infrastructure,self.infrastructure_key,self.config,self.plan,job,now=self.now)
        return True


def _historical_semantics_forbidden(*args,**kwargs):
    raise ValueError('Packaged candidate worker cannot fabricate historical human/spoken-word semantic evidence.')


def build_worker_bindings(*,config,processing_plan,sdk_lock_bytes,acceptance,reviewer_public_key,
    policy_envelope=None,policy_public_key=None,billing_envelope=None,billing_public_key=None,workspace,enabled=False,
    credential_bundle=None,sdk_factories=None,transaction_runner=None,http_client_factory=None,read_transport=None,now=time.time,
    protocol='originals-v1',infrastructure_envelope=None):
    """Construct the actual graph; optional factories are offline test seams only.

    CLI declarative JSON cannot supply Python factories. Missing SDK, signature,
    billing evidence or attached account prevents provider/client activation.
    """
    checked=validate_config(config);plan=validate_private_plan(processing_plan)
    native_mode=checked.get('native_api_mode','vertex_eu')
    require(checked['firestore']['database']!='(default)','Packaged studio requires a dedicated named database; preserve existing default data.')
    validate_runtime(checked,sdk_lock_bytes)
    require(protocol in ('originals-v1','exact-film-v1'),'Explicit supported packaged worker protocol required.')
    require(enabled is True,'Packaged worker remains disabled until explicitly executed.')
    if protocol=='exact-film-v1':
        activation=ExactFilmActivation(checked,plan,acceptance,reviewer_public_key,infrastructure_envelope,policy_public_key,now=now)
        activation()
    else:
        from .declarative_policy import ApprovedProductionPolicy
        policy=ApprovedProductionPolicy(policy_envelope,public_key=policy_public_key,
            config_sha256=digest(checked),processing_plan_sha256=digest(plan),now=now)
        require(enabled is True,'Packaged worker remains disabled until explicitly executed.')
        require(policy.payload.get('native_api_mode','vertex_eu')==native_mode,'Signed native API mode differs from configured route.')
        activation=WorkerActivation(checked,plan,acceptance,reviewer_public_key,policy,now=now);activation()
        from .vertex_token_preflight import validate_billing_contract
        validate_billing_contract(policy,billing_envelope,billing_public_key,now=now)
        require(policy.payload['project']==checked['target']['project_id'] and policy.payload['narration_profile']==plan['narration_profile'],
                'Approved provider policy differs from private target/profile.')
        require(all(policy.payload['budget'][k]==checked['budget'][k] for k in checked['budget']),
                'Approved cumulative budget differs from private configuration.')
    root=Path(workspace)
    require(root.is_absolute() and root.is_dir() and not any(p.is_symlink() for p in (root,*root.parents)),
            'Explicit private existing worker workspace required.')
    require(not root.resolve().is_relative_to(Path(__file__).resolve().parent.parent/'web'),'Worker workspace cannot be public.')
    from .workload_credentials import create_attached_credentials
    credentials=credential_bundle or create_attached_credentials(checked,role='worker',activation_check=activation,enabled=True)
    credentials.verify_identity()
    if sdk_factories is None:
        try:
            from google.cloud import firestore,storage,secretmanager
        except ImportError:raise RuntimeError('Pinned Google SDK runtime is missing; no provider request attempted.') from None
        sdk_factories=dict(firestore=firestore.Client,storage=storage.Client,secrets=secretmanager.SecretManagerServiceClient)
    require(set(sdk_factories)=={'firestore','storage','secrets'},'Exact explicit SDK factories required.')
    project=checked['target']['project_id'];db=checked['firestore']['database']
    store=sdk_factories['storage'](project=project,credentials=credentials.credentials)
    journal_client=sdk_factories['firestore'](project=project,database=db,credentials=credentials.credentials)
    require(getattr(journal_client,'_database_string',None)==f'projects/{project}/databases/{db}',
            'SDK client must address exactly the configured named database.')
    if protocol=='exact-film-v1':
        # No Secret Manager client, provider transport, countTokens, source planner
        # or generation coordinator is constructed for retained-only rendering.
        return WorkerBindings(store,journal_client,credentials.token_reader,_historical_semantics_forbidden,
            transaction_runner=transaction_runner,read_transport=read_transport,generation_factory=None,activation_check=activation,exact_film_preflight=activation.for_job)
    secret_client=sdk_factories['secrets'](credentials=credentials.credentials)
    def key_reader(name):
        activation();credentials.verify_identity()
        response=secret_client.access_secret_version(request={'name':checked['secret_resources'][name]+'/versions/latest'},retry=None,timeout=15)
        raw=response.payload.data
        require(isinstance(raw,bytes) and 8<=len(raw)<=512,'Provider key is unavailable or malformed.')
        value=raw.decode('ascii');require(not any(c.isspace() for c in value),'Provider key contains whitespace.')
        return value
    from .vertex_token_preflight import ConfiguredNativeProofs
    proofs=ConfiguredNativeProofs(policy=policy,billing_envelope=billing_envelope,billing_public_key=billing_public_key,
        token_reader=lambda job:credentials.native_token_reader(),firestore_client=journal_client,storage_client=store,
        config=checked,activation_check=activation,enabled=True,transaction_runner=transaction_runner,
        client_factory=http_client_factory,now=now,api_key_reader=(lambda job:key_reader('gemini')) if native_mode=='gemini_developer' else None)
    from .provider_execution import ElevenLabsHTTPTransport,FalQueueTransport
    from .motion_continuation import FalExtensionTransport
    from .segment_capture import MotionTransportRouter
    narration=ElevenLabsHTTPTransport(lambda:key_reader('elevenlabs'),enabled=True,client_factory=http_client_factory)
    motion=MotionTransportRouter(image_transport=FalQueueTransport(lambda:key_reader('fal'),enabled=True,client_factory=http_client_factory),
        extension_transport=FalExtensionTransport(lambda:key_reader('fal'),enabled=True,client_factory=http_client_factory),activation_check=activation,enabled=True)
    from .source_planning import build_source_planner_factory
    from .funded_review import build_vertex_review_factory
    from .review_budget import FilmReviewFunding
    from .generation_coordinator import build_generation_factory
    source=build_source_planner_factory(config=policy.payload['source_config'],quote_loader=policy.source_quote_loader(proofs),
        token_reader=lambda job,runtime:credentials.native_token_reader(),activation_check=activation,enabled=True,now=now,client_factory=http_client_factory,
        native_api_mode=native_mode,key_reader=(lambda job,runtime:key_reader('gemini')) if native_mode=='gemini_developer' else None)
    review=build_vertex_review_factory(token_reader=credentials.native_token_reader,input_proof_loader=policy.review_input_proof_loader(proofs),
        max_input_tokens=policy.payload['review_quote']['max_input_tokens'],token_limit_contract_reference=policy.payload['review_quote']['token_limit_contract_reference'],
        workspace=root,activation_check=activation,enabled=True,now=now,client_factory=http_client_factory,
        native_api_mode=native_mode,key_reader=(lambda:key_reader('gemini')) if native_mode=='gemini_developer' else None)
    from .private_preview import FirestorePreviewAuthorizations
    auth=FirestorePreviewAuthorizations(journal_client,project_id=project,namespace=checked['firestore']['namespace'],
        transaction_runner=transaction_runner,budget_loader=policy.preview_budget_loader,
        preview_release_sha256=plan['preview_contract']['preview_release_sha256'],now=now)
    def authorization(job):
        # Derive only from explicitly signed prior administrator policy, then
        # persist the immutable owner/job-specific authorization for API reads.
        activation();proofs.prevalidate(job)
        value=policy.preview_authorization_loader(job)
        stored=auth.load(job)
        require(stored==(value['authorization'],value['expected_authorization_sha256']),'Recorded job authorization differs from signed policy.')
        return stored
    factory=build_generation_factory(narration_transport=narration,motion_transport=motion,
        narration_approval_loader=policy.narration_approval_loader,policy_loader=lambda job,plan:None,
        review_loader=None,activation_check=activation,source_kind='provider_receipts',enabled=True,
        review_funding=FilmReviewFunding(quote_loader=policy.review_quote_loader,enabled=True,now=now),
        source_planner_factory=source,review_loader_factory=review,
        preview_authorization_loader=authorization,preview_budget_loader=policy.preview_budget_loader,
        preview_quote_loader=policy.preview_quote_loader)
    def generation_factory(job,runtime):
        activation();proofs.prevalidate(job)
        # Reserve finite whole-job count preflights before spending on narration.
        proofs.reserve_budget(job)
        value=policy.preview_authorization_loader(job)
        auth.commit(job,value['authorization'],value['expected_authorization_sha256'])
        return factory(job=job,runtime=runtime)
    return WorkerBindings(store,journal_client,credentials.token_reader,_historical_semantics_forbidden,
        transaction_runner=transaction_runner,read_transport=read_transport,generation_factory=generation_factory,activation_check=activation)


def parse_policy_bundle(raw,policy_key_sha256,billing_key_sha256):
    """Explicit noncredential signed policy ENV data with independent key pins."""
    import base64,re
    from .worker_bootstrap import strict_json
    require(isinstance(raw,str) and 0<len(raw.encode('utf-8'))<=30000,'Bounded private policy bundle required.')
    value=strict_json(raw)
    require(isinstance(value,dict) and set(value) in ({'schema_version','policy','policy_public_key_base64','billing_contract','billing_public_key_base64'}, {'schema_version','policy','policy_public_key_base64','billing_contract','billing_public_key_base64','infrastructure'})
        and type(value['schema_version']) is int and value['schema_version']==1,'Exact declarative policy bundle required.')
    result=dict(policy_envelope=value['policy'],billing_envelope=value['billing_contract'])
    for field,pin,out in [('policy_public_key_base64',policy_key_sha256,'policy_public_key'),('billing_public_key_base64',billing_key_sha256,'billing_public_key')]:
        require(isinstance(pin,str) and re.fullmatch(r'[0-9a-f]{64}',pin),'Independent policy/billing public-key pin required.')
        require(isinstance(value[field],str) and len(value[field])==44,'Raw public-key encoding required.')
        key=base64.b64decode(value[field],validate=True)
        require(len(key)==32 and hashlib.sha256(key).hexdigest()==pin,'Policy bundle cannot nominate its own trust anchor.')
        result[out]=key
    from .declarative_policy import verify_signed
    verify_signed(result['policy_envelope'],result['policy_public_key'])
    verify_signed(result['billing_envelope'],result['billing_public_key'])
    if 'infrastructure' in value:
        verify_signed(value['infrastructure'],result['policy_public_key']);result['infrastructure_envelope']=copy.deepcopy(value['infrastructure'])
    return result


def load_policy_env(name,policy_key_sha256,billing_key_sha256,*,environ=None):
    import os,re
    require(isinstance(name,str) and re.fullmatch(r'BYRON_[A-Z0-9_]{1,100}',name),'Explicit BYRON policy variable name required.')
    values=os.environ if environ is None else environ
    require(name in values,'Selected policy variable is absent; no implicit credential/file fallback.')
    return parse_policy_bundle(values[name],policy_key_sha256,billing_key_sha256)
