"""Explicit application assembly; disabled by default, no credential discovery.

Existing authenticated clients and private configuration are injected by the
operator. Construction performs no remote calls. Enabling requires an offline
operator-signed acceptance envelope bound to this exact configuration and plan.
A signature attests review; it does not itself establish that cloud/media/login
acceptance happened. No actual acceptance/key/signature is shipped with source.
"""
import base64
import hashlib
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, field

from .deployment_plan import validate_config
from .hosted_control import create_app, validate_private_plan, canonical, Principal, require
from .hosted_uploads import CreatorAccess, CreatorSession
from .google_identity import GoogleIdentityResolver
from .google_login import GoogleLogin, GoogleCodeExchange, FirestoreLoginJournal
from .private_config import SecretManagerConfig
from .preview_runtime import verify_runtime_authorization
from .voices import ElevenLabsVoiceReader
from .voice_library import FirestoreVoiceLibrary
from .key_records import FirestoreKeyRecords
from .admin import install_admin_routes
from .cloud_journal import FirestoreJobStore, FirestoreDispatchJournal, FirestoreArtifactRegistry
from .cloud_storage import CloudUploadStore, CloudArtifactReader, CloudArtifactWriter
from .cloud_run_readback import CloudRunReadTransport, CloudRunReadback
from .worker_binding import HttpxCloudRunTransport
from .original_input import OriginalInputCloudRunDispatcher, OriginalInputRequestLoader
from .resume_control import FirestoreResumeJournal, ResumeService, install_resume_routes

EVIDENCE = frozenset({'identity_flow', 'private_storage', 'provider_cost_and_disclosure',
    'real_media_acceptance', 'recovery', 'pinned_runtime', 'browser_device', 'handoff_adapter'})
SHA = re.compile(r'[0-9a-f]{64}\Z')


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def verify_acceptance(envelope, public_key, config_sha256, plan_sha256, *, now=None):
    """Offline Ed25519 verification with an independently injected trusted key.

    The key belongs to the operator/reviewer, not any hosted service. Never take
    it from the envelope, browser, HTTP header or public deployment configuration.
    cryptography must be in the separately reviewed pinned runtime; missing it
    denies enablement, never falls back to unsigned acceptance.
    """
    try:
        now = time.time() if now is None else now
        if not isinstance(envelope, dict) or set(envelope) != {'payload', 'signature'}:
            raise ValueError()
        payload = envelope['payload']
        if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'purpose', 'status',
                'config_sha256', 'plan_sha256', 'issued_at', 'expires_at', 'evidence'}
                or type(payload['schema_version']) is not int or payload['schema_version'] != 1
                or payload['purpose'] != 'byron-runtime-acceptance-v1' or payload['status'] != 'ACCEPTED'
                or payload['config_sha256'] != config_sha256 or payload['plan_sha256'] != plan_sha256
                or type(payload['issued_at']) is not int or type(payload['expires_at']) is not int
                or not payload['issued_at'] <= now < payload['expires_at']
                or not 0 < payload['expires_at'] - payload['issued_at'] <= 7 * 86400
                or not isinstance(payload['evidence'], dict) or set(payload['evidence']) != EVIDENCE
                or not all(isinstance(v, str) and SHA.fullmatch(v) for v in payload['evidence'].values())
                or not isinstance(public_key, bytes) or len(public_key) != 32):
            raise ValueError()
        signature = envelope['signature']
        if not isinstance(signature, str) or len(signature) > 100:
            raise ValueError()
        signature = base64.b64decode(signature, validate=True)
        if len(signature) != 64:
            raise ValueError()
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, canonical(payload).encode())
        return json.loads(canonical(payload))
    except Exception:
        raise ValueError('Runtime acceptance is missing, expired, unverified or does not bind this configuration and plan.') from None


class AcceptedRuntimeBinding:
    """Internal bound capability, never constructed from a browser value.

    Reverify the immutable signed envelope and expiry at each request/dispatch.
    Holding this object does not change CloudRunDispatcher's independent gate.
    """
    def __init__(self, *, envelope, public_key, config, plan, dispatcher, store, uploads,
                 access, voices, artifact_verifier, artifact_reader, handoff_loader, plan_provider, default_voice_provider, clock=time.time, production_admission=None):
        self._envelope_json = canonical(envelope)
        self._public_key = bytes(public_key)
        self._config_sha, self._plan_sha = digest(config), digest(plan)
        self._preview_contract_json=canonical(plan.get("preview_contract"))
        self._exact_film_contract_json=canonical(plan.get("exact_film_contract"))
        self.config_sha256 = self._config_sha
        self._target_sha = digest(config['target'])
        self.plan_provider, self.default_voice_provider = plan_provider, default_voice_provider
        self._clock = clock
        self._production_admission=production_admission
        self._job_preflight=dispatcher.job_preflight
        self.dispatcher, self.store, self.uploads = dispatcher, store, uploads
        self.access, self.voices = access, voices
        self.artifact_verifier, self.artifact_reader = artifact_verifier, artifact_reader
        self._handoff_loader = handoff_loader
        self.release_sha256 = digest(envelope)
        self.check()

    def check(self):
        if type(self.dispatcher) is not OriginalInputCloudRunDispatcher or self.dispatcher.enabled is not True:
            raise ValueError('Only the explicitly bound enabled Cloud Run dispatcher can use runtime acceptance.')
        if (type(self.store) is not FirestoreJobStore or type(self.uploads) is not CloudUploadStore
                or self.dispatcher.store is not self.store or self.dispatcher.handoff_loader is not self._handoff_loader
                or self.dispatcher.request_loader is not self._handoff_loader
                or self._handoff_loader.config_sha256 != self._config_sha
                or digest(self.dispatcher.target) != self._target_sha):
            raise ValueError('The concrete runtime binding changed.')
        if self._production_admission is not None:
            from .production_admission import ProductionAdmission
            if type(self._production_admission) is not ProductionAdmission or self.dispatcher.job_preflight is not self._job_preflight:
                raise ValueError('Packaged production admission changed.')
            self._production_admission.validate_configuration()
        payload = verify_runtime_authorization(json.loads(self._envelope_json), self._public_key,
            self._config_sha, self._plan_sha, preview_contract=json.loads(self._preview_contract_json),now=self._clock())
        exact_contract=json.loads(self._exact_film_contract_json)
        if exact_contract is not None:
            from .exact_film_runtime import validate_contract
            validate_contract(exact_contract)
        if getattr(self._handoff_loader, 'release_sha256', None) != payload['evidence']['handoff_adapter']:
            raise ValueError('The reviewed handoff adapter differs from acceptance.')
        return True

    def matches(self, *, dispatcher, store, uploads, access, voices, artifact_verifier, artifact_reader, plan_provider, default_voice_provider):
        return (dispatcher is self.dispatcher and store is self.store and uploads is self.uploads
            and access is self.access and voices is self.voices
            and artifact_verifier is self.artifact_verifier and artifact_reader is self.artifact_reader
            and plan_provider is self.plan_provider and default_voice_provider is self.default_voice_provider
            and self.check())


class _AdminSecrets:
    def __init__(self, client, subjects, resources):
        self._stores = {owner: SecretManagerConfig(client, admin_subject=owner, resources=resources) for owner in subjects}

    def _store(self, subject):
        if subject not in self._stores:
            raise ValueError('Administrator required.')
        return self._stores[subject]

    def save_key(self, subject, provider, key):
        return self._store(subject).save_key(subject, provider, key)

    def save_default_voice(self, subject, voice_id):
        return self._store(subject).save_default_voice(subject, voice_id)

    def read_for_server(self, name):
        return next(iter(self._stores.values())).read_for_server(name)


@dataclass(frozen=True)
class RuntimeAssembly:
    app: object
    jobs: object
    uploads: object
    artifacts: object
    artifact_writer: object
    dispatcher: object
    identity: object = field(repr=False)
    enabled: bool = False
    release_sha256: str | None = None
    previews: object = field(default=None,repr=False)
    exact_films: object = field(default=None,repr=False)


def assemble_runtime(*, config, processing_plan, firestore_client, storage_client,
                     secret_client, workload_token_reader, csrf_secret,
                     enabled=False, acceptance=None, reviewer_public_key=None, production_admission=None):
    """Assemble concrete adapters using explicit already-authenticated clients.

    GoogleLogin is mounted against the same durable session journal/identity
    resolver. Its exact private allowlist and OAuth secret reference are signed
    as part of configuration. The original-input request loader is constructed
    internally; HTTP dispatch never calls a provider or prepares media.
    SDK clients must be configured for exactly the existing private resources;
    no constructor discovers credentials, creates resources or grants access.
    """
    config = validate_config(config)
    plan = validate_private_plan(processing_plan)
    if plan['narration_profile'] != config['narration_profile']:
        raise ValueError('Processing plan narration differs from private configuration.')
    handoff_loader = OriginalInputRequestLoader(digest(config))
    if type(enabled) is not bool or not callable(workload_token_reader):
        raise ValueError('Explicit reviewed runtime dependencies are required.')
    if enabled:
        reviewed = verify_runtime_authorization(acceptance, reviewer_public_key, digest(config), digest(plan),preview_contract=plan.get("preview_contract"))
        if getattr(handoff_loader, 'release_sha256', None) != reviewed['evidence']['handoff_adapter']:
            raise ValueError('The reviewed handoff adapter differs from acceptance.')
    auth, target, runtime = config['identity'], config['target'], config['runtime']
    dbargs = dict(project_id=target['project_id'], namespace=config['firestore']['namespace'])
    # A project match alone cannot establish the selected database. Require the
    # explicit database identity exposed by the authenticated Firestore client.
    expected_db = f"projects/{target['project_id']}/databases/{config['firestore']['database']}"
    if getattr(firestore_client, '_database_string', None) != expected_db:
        raise ValueError('Firestore client database differs from private configuration.')
    login_journal = FirestoreLoginJournal(firestore_client, **dbargs)
    identity = GoogleIdentityResolver(audience=auth['oauth_audience'], allowed_subjects=auth['creator_subjects'],
        admin_subjects=auth['admin_subjects'], csrf_secret=csrf_secret, revoked=login_journal.revoked)
    access = CreatorAccess(identity, auth['origin'])
    jobs = FirestoreJobStore(firestore_client, **dbargs)
    journal = FirestoreDispatchJournal(firestore_client, **dbargs)
    artifacts = FirestoreArtifactRegistry(firestore_client, **dbargs)
    uploads = CloudUploadStore(storage_client, bucket=target['bucket'])
    from .preview_registry import FirestorePreviewRegistry, PREVIEW_PREFIX, PREVIEW_STATE
    preview_verifier=None
    if 'preview_contract' in plan:
        from .generation_coordinator import FirestoreGenerationPlan
        from .private_preview import FirestorePreviewAuthorizations
        from .preview_worker import FirestorePreviewEvidence
        from .preview_registry import PreviewAuthorizationVerifier
        generation_plans=FirestoreGenerationPlan(firestore_client, **dbargs)
        def preview_budget(current):
            frozen=generation_plans.load(current)
            require(isinstance(frozen,dict) and isinstance(frozen.get('budget'),dict), 'Immutable preview budget unavailable.')
            value=frozen['budget'];configured=config['budget']
            require(all(value.get(key)==configured[key] for key in ('scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents'))
                    and all(segment['max_cost_cents']<=configured['operation_cap_cents'] for scene in frozen['scenes'] for segment in scene['segments']),
                    'Private preview exceeds configured campaign/job/operation caps.')
            return value
        preview_authorizations=FirestorePreviewAuthorizations(firestore_client, **dbargs, budget_loader=preview_budget,
            preview_release_sha256=plan['preview_contract']['preview_release_sha256'])
        preview_evidence=FirestorePreviewEvidence(firestore_client, **dbargs)
        preview_verifier=PreviewAuthorizationVerifier(preview_authorizations,preview_evidence)
    previews=FirestorePreviewRegistry(firestore_client, **dbargs, authorization_verifier=preview_verifier)
    final_reader=CloudArtifactReader(storage_client, bucket=target['bucket'], reference_resolver=artifacts)
    preview_reader=CloudArtifactReader(storage_client, bucket=target['bucket'], reference_resolver=previews, prefix=PREVIEW_PREFIX)
    def artifact_reader(owner,job,proof):
        return (preview_reader if job.state==PREVIEW_STATE else final_reader)(owner,job,proof)
    artifact_writer = CloudArtifactWriter(storage_client, bucket=target['bucket'])
    owners = ['google_' + hashlib.sha256(('accounts.google.com\0' + subject).encode()).hexdigest() for subject in auth['admin_subjects']]
    native_mode = config.get('native_api_mode', 'vertex_eu')
    secret_names = ('elevenlabs','fal','default_voice') + (('gemini',) if native_mode == 'gemini_developer' else ())
    secrets = _AdminSecrets(secret_client, owners, {k:config['secret_resources'][k] for k in secret_names})
    elevenlabs = ElevenLabsVoiceReader(lambda: secrets.read_for_server('elevenlabs'))
    # Creators choose from the owner's named list; names were read from ElevenLabs when added.
    library = FirestoreVoiceLibrary(firestore_client, **dbargs)
    voices = lambda owner: library.read()['voices']
    read = CloudRunReadTransport(workload_token_reader, enabled=enabled)
    readback = CloudRunReadback(target=target, read=read, expected_task_template_sha256=runtime['cloud_run_task_template_sha256'],
        expected_service_account=runtime['worker_service_account'], handoff_loader=None,
        expected_request_loader=handoff_loader, enabled=enabled)
    dispatcher = OriginalInputCloudRunDispatcher(target=target, target_provider=readback.target_provider, store=jobs, journal=journal,
        request_loader=handoff_loader, transport=HttpxCloudRunTransport(workload_token_reader),
        execution_resolver=readback.execution_resolver, enabled=enabled)
    if production_admission is not None:
        from .production_admission import ProductionAdmission
        require(type(production_admission) is ProductionAdmission and production_admission.config==config and production_admission.plan==plan,
                'Signed production admission differs from assembled configuration.')
        production_admission.validate_configuration();dispatcher.job_preflight=production_admission.for_job
    def verifier(owner,job,artifact_id):
        return (previews if job.state==PREVIEW_STATE else artifacts).verify(owner,job,artifact_id)
    plan_json = canonical(plan)
    plan_provider = lambda owner: json.loads(plan_json)
    default_voice_provider = lambda owner: library.read()['default_voice_id']
    binding = AcceptedRuntimeBinding(envelope=acceptance, public_key=reviewer_public_key, config=config, plan=plan,
        dispatcher=dispatcher, store=jobs, uploads=uploads, access=access, voices=voices,
        artifact_verifier=verifier, artifact_reader=artifact_reader, handoff_loader=handoff_loader,
        plan_provider=plan_provider, default_voice_provider=default_voice_provider, production_admission=production_admission) if enabled else None
    dispatcher.activation_check = binding.check if binding else lambda: False
    resumes = FirestoreResumeJournal(firestore_client, **dbargs)
    resume_service = ResumeService(jobs=jobs, resumes=resumes, dispatcher=dispatcher,
        terminal_verifier=readback.terminal_execution, activation_check=binding.check,
        enabled=True) if binding else None
    # Detached fixed plan; per-job voice override happens only after catalog validation.
    app = create_app(store=jobs, dispatcher=dispatcher, plan_provider=plan_provider,
        voice_provider=voices, default_voice_provider=default_voice_provider,
        artifact_verifier=verifier, artifact_reader=artifact_reader, creator_access=access,
        private_upload_store=uploads, hosted_client_enabled=enabled, runtime_binding=binding,
        resume_status_provider=resume_service.availability if resume_service else None)
    def oauth_secret():
        response = secret_client.access_secret_version(request={'name': config['secret_resources']['google_oauth'] + '/versions/latest'})
        return response.payload.data.decode('utf-8')
    login = GoogleLogin(origin=auth['origin'], client_id=auth['oauth_audience'],
        redirect_uri=auth['origin'] + '/auth/callback', subject_emails=auth['subject_emails'],
        journal=login_journal, exchange=GoogleCodeExchange(oauth_secret, enabled=enabled),
        identity_resolver=identity, enabled=enabled, activation_check=binding.check if binding else lambda: False)
    login.mount(app)
    def test_connection(subject, provider):
        if provider != 'elevenlabs':
            raise ValueError('No reviewed read-only fal connection endpoint is configured.')
        elevenlabs(subject)
        return True
    def admin_session(request):
        if binding is None:
            raise ValueError('Administrator runtime is not activated.')
        binding.check()
        return identity.admin_session(request)
    install_admin_routes(app, session_resolver=admin_session, private_store=secrets,
        connection_tester=test_connection, voice_provider=elevenlabs, voice_library=library, voice_namer=elevenlabs.name_of,
        key_records=FirestoreKeyRecords(firestore_client, **dbargs),
        allowed_origin=auth['origin'], activated=enabled, native_api_mode=native_mode)
    def admin_creator_session(request):
        session = admin_session(request)
        return CreatorSession(Principal(session.subject), session.csrf_token)
    admin_access = CreatorAccess(admin_creator_session, auth['origin'])
    creator_owners = ['google_' + hashlib.sha256(('accounts.google.com\0' + subject).encode()).hexdigest()
                      for subject in auth['creator_subjects']]
    install_resume_routes(app, service=resume_service, creator_access=access,
        admin_access=admin_access, allowed_owners=creator_owners)
    exact_films=None
    if 'exact_film_contract' in plan:
        from .exact_film_runtime import validate_contract
        from .exact_film_assembly import build_exact_film_adapters
        from .exact_film_registry import RetainedPreviewViewVerifier
        from .exact_film_control import ExactFilmService,install_exact_film_routes
        validate_contract(plan['exact_film_contract'])
        exact_films=build_exact_film_adapters(config=config,firestore_client=firestore_client,storage_client=storage_client,
            token_reader=workload_token_reader,activation_check=binding.check if binding else lambda:False,
            read_transport=read,enabled=enabled)
        if production_admission is not None:
            exact_films.dispatch.bind_job_preflight(production_admission.for_exact_film)
        previews.authorization_verifier=RetainedPreviewViewVerifier(preview_verifier,exact_films.actions,exact_films.preview_evidence)
        exact_service=ExactFilmService(jobs=jobs,actions=exact_films.actions,dispatcher=exact_films.dispatch,
            registry=exact_films.registry,artifact_reader=exact_films.reader,preview_registry=previews,preview_reader=preview_reader,
            activation_check=binding.check if binding else lambda:False,enabled=enabled)
        exact_films.service=exact_service
        install_exact_film_routes(app,service=exact_service,creator_access=access)
    # API/auth routes have precedence; the unchanged public creator assets live
    # under web only. Private config, uploads and journals are never mounted.
    from starlette.staticfiles import StaticFiles
    web_root = Path(__file__).resolve().parent.parent / 'web'
    if web_root.is_symlink() or not (web_root / 'index.html').is_file():
        raise ValueError('The reviewed creator assets are missing or unsafe.')
    app.mount('/', StaticFiles(directory=str(web_root), html=True, follow_symlink=False), name='creator-assets')
    return RuntimeAssembly(app,jobs,uploads,artifacts,artifact_writer,dispatcher,identity,enabled,
        binding.release_sha256 if binding else None,previews,exact_films)
