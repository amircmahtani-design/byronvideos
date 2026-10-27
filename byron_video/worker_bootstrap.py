"""Explicit private worker assembly; validate-only by default, no ADC discovery.

The caller must supply reviewed authenticated clients, a workload-token reader and
an independent semantic verifier. This module constructs concrete storage/journal/
portrait/readback adapters. It does not configure accounts, secrets or access.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from types import ModuleType

from .deployment_plan import validate_config
from .cloud_journal import (_FirestoreBoundary, FirestoreJobStore, FirestoreDispatchJournal,
                            FirestoreArtifactRegistry)
from .cloud_storage import _PrivateBucket, CloudArtifactWriter
from .cloud_run_readback import CloudRunReadback, CloudRunReadTransport
from .cloud_render import validate_object, validate_manifest, build_execution_request
from .portrait_composition import PortraitCandidateCompositor
from .worker_binding import execute_and_commit_worker, canonical
from .hosted_control import Principal, DispatchReceipt
from .elevenlabs_adapter import fingerprint

SHA=re.compile(r'[0-9a-f]{64}\Z')
REQUIRED_PACKAGES={'google-cloud-storage','google-cloud-firestore','google-auth','httpx','pillow','numpy','fastapi'}


def require(ok,message):
    if not ok:raise ValueError(message)


def strict_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            require(key not in result,'Duplicate JSON field.')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Non-finite JSON.')))


def private_bytes(path,maximum):
    path=Path(path)
    require(path.is_absolute() and path.is_file() and not any(p.is_symlink() for p in (path,*path.parents)),
            'Use an explicit private regular file without symlinks.')
    require(0<path.stat().st_size<=maximum,'Private configuration file exceeds its bound.')
    require(path.stat().st_mode & 0o077 == 0,'Private configuration must not be group/world readable.')
    return path.read_bytes()


def validate_runtime(config,sdk_lock_bytes):
    require(hashlib.sha256(sdk_lock_bytes).hexdigest()==config['runtime']['sdk_lock_sha256'],'SDK lock does not match reviewed private configuration.')
    lock=strict_json(sdk_lock_bytes)
    require(isinstance(lock,dict) and set(lock)=={'python','packages'} and lock['python']==f'{sys.version_info.major}.{sys.version_info.minor}'
            and isinstance(lock['packages'],dict) and REQUIRED_PACKAGES<=set(lock['packages']), 'Complete pinned worker runtime lock required.')
    for name,version in lock['packages'].items():
        require(isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',name)
                and isinstance(version,str) and 0<len(version)<=100,'Invalid locked dependency.')
        try:actual=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:raise RuntimeError('Reviewed worker dependency is unavailable; no cloud call attempted.') from None
        require(actual==version,'Installed worker dependency differs from the reviewed lock.')
    release=Path(__file__).resolve().parent.parent/'style-guide/portrait-candidate/release.json'
    require(hashlib.sha256(release.read_bytes()).hexdigest()==config['runtime']['portrait_candidate_sha256'],
            'Portrait candidate release differs from private deployment configuration.')
    version=subprocess.run(['ffmpeg','-version'],check=True,capture_output=True,timeout=15).stdout.decode().splitlines()[0].split()[2]
    require(version==config['runtime']['ffmpeg_version'],'FFmpeg differs from the reviewed worker runtime.')
    return lock


@dataclass(frozen=True)
class WorkerBindings:
    """Already authenticated objects only; construction must perform no requests."""
    storage_client: object
    firestore_client: object
    token_reader: object
    semantic_verifier: object
    transaction_runner: object = None
    read_transport: object = None
    generation_factory: object = None
    activation_check: object = None
    exact_film_preflight: object = None


def explicit_google_clients(config,credentials,*,semantic_verifier,token_reader):
    """Optional concrete SDK factory, with explicitly supplied credentials only."""
    require(credentials is not None and getattr(credentials,'service_account_email',None)==config['runtime']['worker_service_account'],
            'Explicit credentials must belong to the reviewed workload identity.')
    require(callable(token_reader) and callable(semantic_verifier),'Explicit workload-token and semantic verification adapters required.')
    try:
        from google.cloud import storage,firestore
    except ImportError:raise RuntimeError('Reviewed Google storage/Firestore runtime is unavailable.') from None
    project=config['target']['project_id']
    return WorkerBindings(storage.Client(project=project,credentials=credentials),
        firestore.Client(project=project,database=config['firestore']['database'],credentials=credentials),token_reader,semantic_verifier)


class FirestoreWorkerEvidence(_FirestoreBoundary):
    """Immutable independently committed native report and clip-review receipts."""
    def _ref(self,owner,job_id):
        require(isinstance(job_id,str) and re.fullmatch(r'[0-9a-f]{32}',job_id),'Invalid private job.')
        return self._doc(self._owner(owner)+'/worker_evidence/'+job_id)

    def commit(self,owner,job,report,clip_receipts,*,continuation_context=None):
        owner_id=owner.subject if isinstance(owner,Principal) else owner
        require(owner_id==job.owner_id and isinstance(report,dict) and isinstance(clip_receipts,list),'Owner-bound native evidence required.')
        require(report.get('provenance',{}).get('owner_job_sha256')==hashlib.sha256((owner_id+'\0'+job.id).encode()).hexdigest(),
                'Narration receipt belongs to another owner/job.')
        require(report.get('exact_text')==job.snapshot['narrated_text']
                and report.get('profile_sha256')==fingerprint(job.snapshot['processing_plan']['narration_profile']),
                'Narration receipt differs from the frozen exact text/profile.')
        require([r.get('image_id') for r in clip_receipts]==job.snapshot['input_order'],'Clip evidence differs from ordered images.')
        record={'owner_id':owner_id,'job_id':job.id,'snapshot_sha256':job.snapshot_sha256,
                'report':report,'report_sha256':fingerprint(report),'clip_receipts':clip_receipts}
        if continuation_context is not None:
            require(isinstance(continuation_context,dict),'Committed continuation context must be an object.')
            record['continuation_context']=continuation_context
            record['continuation_context_sha256']=fingerprint(continuation_context)
        require(len(canonical(record))<=800_000,'Worker evidence exceeds the transactional document limit.')
        encoded=canonical(record);record=strict_json(encoded);ref=self._ref(owner,job.id)
        def commit(tx):
            old=self._read(ref,tx)
            require(old is None or canonical(old)==encoded,'Worker evidence is immutable after commitment.')
            if old is None:tx.create(ref,record)
            return record
        return self._run(commit)

    def reserve_attempt(self,owner,job,execution_id):
        require(job.receipt is not None and job.receipt.execution_id==execution_id,'Committed execution required.')
        ref=self._doc(self._owner(owner)+'/worker_attempts/'+job.id)
        record={'owner_id':job.owner_id,'job_id':job.id,'snapshot_sha256':job.snapshot_sha256,
                'execution_id':execution_id,'status':'started_unknown_until_artifact_commit'}
        def reserve(tx):
            old=self._read(ref,tx)
            require(old is None,'Worker execution was already attempted; reconcile its artifact without rerendering.')
            tx.create(ref,record)
            return record
        return self._run(reserve)

    def load(self,owner,job):
        owner_id=owner.subject if isinstance(owner,Principal) else owner
        value=self._read(self._ref(owner,job.id))
        base={'owner_id','job_id','snapshot_sha256','report','report_sha256','clip_receipts'}
        require(isinstance(value,dict) and set(value) in (base,base|{'continuation_context','continuation_context_sha256'})
                and value['owner_id']==owner_id==job.owner_id and value['job_id']==job.id
                and value['snapshot_sha256']==job.snapshot_sha256 and fingerprint(value['report'])==value['report_sha256'],
                'Committed worker evidence is missing or changed.')
        if 'continuation_context' in value:
            require(isinstance(value['continuation_context'],dict) and fingerprint(value['continuation_context'])==value['continuation_context_sha256'],
                    'Committed continuation context changed.')
        require(len(canonical(value))<=800_000,'Worker evidence exceeds its bound.')
        return value


class BoundCloudInputReader(_PrivateBucket):
    """Concrete generation-pinned GCS reader restricted to one frozen job."""
    def __init__(self,client,*,bucket,owner_namespace,job_id):
        super().__init__(client,bucket=bucket)
        require(SHA.fullmatch(owner_namespace) and re.fullmatch(r'[0-9a-f]{32}',job_id),'Invalid worker namespace.')
        self.bucket_name=bucket;self.job_prefix=f'owners/{owner_namespace}/jobs/{job_id}/'

    def __call__(self,ref):
        mime=ref.get('content_type') if isinstance(ref,dict) else None
        bound={'application/json':2_000_000,'audio/wav':100_000_000,'video/mp4':50_000_000}.get(mime)
        require(bound is not None,'Unexpected worker input type.')
        ref=validate_object(ref,bucket=self.bucket_name,prefix=self.job_prefix,content_type=mime,max_bytes=bound)
        stream,generation,_=self._download(ref['object'],generation=ref['generation'],maximum=bound,mime=mime,
                                         expected_sha=ref['sha256'],expected_size=ref['bytes'])
        return {'generation':generation,'content_type':mime,'stream':stream}

    def manifest(self,invocation):
        name=invocation['manifest_object'];generation=invocation['manifest_generation']
        require(name.startswith(self.job_prefix) and name.endswith('.json'),'Manifest escaped its private job namespace.')
        self._require_private_bucket()
        blob=self.bucket.blob(name,generation=int(generation));blob.reload(if_generation_match=int(generation),timeout=self.timeout,retry=None)
        ref=dict(bucket=self.bucket_name,object=name,generation=generation,sha256=invocation['manifest_sha256'],
                 bytes=blob.size,content_type=blob.content_type)
        response=self(ref)
        try:data=response['stream'].read(2_000_001)
        finally:response['stream'].close()
        value=strict_json(data)
        require(canonical(value)==data,'Worker manifest must be exact canonical JSON.')
        return value,ref


def validate_invocation(value,target):
    keys={'job_id','owner_namespace','manifest_bucket','manifest_object','manifest_generation','manifest_sha256','plan_sha256','execution_id'}
    require(isinstance(value,dict) and set(value)==keys and all(isinstance(v,str) for v in value.values()),'Complete explicit worker invocation required.')
    require(re.fullmatch(r'[0-9a-f]{32}',value['job_id']) and SHA.fullmatch(value['owner_namespace'])
            and SHA.fullmatch(value['manifest_sha256']) and SHA.fullmatch(value['plan_sha256'])
            and re.fullmatch(r'[1-9][0-9]{0,19}',value['manifest_generation'])
            and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',value['execution_id'])
            and value['manifest_bucket']==target['bucket'],'Invalid bound worker invocation.')
    prefix=f"owners/{value['owner_namespace']}/jobs/{value['job_id']}/"
    require(value['manifest_object'].startswith(prefix) and re.fullmatch(r'[A-Za-z0-9/_-]+\.json',value['manifest_object'])
            and '..' not in value['manifest_object'] and '//' not in value['manifest_object'],'Invalid private manifest path.')
    return dict(value)


def invocation_from_environment(environment,target):
    """Read only explicit job overrides, never credentials; verify later by API."""
    names={'job_id':'BYRON_JOB_ID','owner_namespace':'BYRON_OWNER_NAMESPACE',
        'manifest_bucket':'BYRON_MANIFEST_BUCKET','manifest_object':'BYRON_MANIFEST_OBJECT',
        'manifest_generation':'BYRON_MANIFEST_GENERATION','manifest_sha256':'BYRON_MANIFEST_SHA256',
        'plan_sha256':'BYRON_PLAN_SHA256','execution_id':'CLOUD_RUN_EXECUTION'}
    return validate_invocation({key:environment.get(name) for key,name in names.items()},target)


class PrivateWorkerRuntime:
    """Concrete adapter graph. Building it does not read cloud state or tokens."""
    def __init__(self,config,bindings,*,enabled=False,sdk_lock_bytes=None,clock=time.monotonic,sleep=time.sleep):
        self.config=validate_config(config);self.enabled=enabled is True;self.bindings=bindings
        self.clock,self.sleep=clock,sleep
        if self.enabled:
            require(isinstance(sdk_lock_bytes,bytes),'Reviewed SDK lock required before worker execution.')
            validate_runtime(self.config,sdk_lock_bytes)
        require(isinstance(bindings,WorkerBindings) and callable(bindings.token_reader) and callable(bindings.semantic_verifier),
                'Explicit authenticated clients/token reader/semantic verifier required.')
        project=config['target']['project_id']
        require(getattr(bindings.storage_client,'project',None)==project and getattr(bindings.firestore_client,'project',None)==project,
                'Authenticated SDK clients must match the configured project.')
        common=dict(project_id=project,namespace=config['firestore']['namespace'],transaction_runner=bindings.transaction_runner)
        self.store=FirestoreJobStore(bindings.firestore_client,**common)
        self.dispatch=FirestoreDispatchJournal(bindings.firestore_client,**common)
        from .resume_control import FirestoreResumeJournal
        self.resumes=FirestoreResumeJournal(bindings.firestore_client,**common)
        self.registry=FirestoreArtifactRegistry(bindings.firestore_client,**common)
        self.evidence=FirestoreWorkerEvidence(bindings.firestore_client,**common)
        from .preview_worker import FirestorePreviewEvidence
        self.preview_evidence=FirestorePreviewEvidence(bindings.firestore_client,**common)
        self.writer=CloudArtifactWriter(bindings.storage_client,bucket=config['target']['bucket'])
        self.read=bindings.read_transport or CloudRunReadTransport(bindings.token_reader,enabled=self.enabled)

    def _load_job(self,invocation):
        # Namespace is an opaque hash supplied by reviewed Cloud Run overrides.
        path=f"byron_control/{self.config['firestore']['namespace']}/owners/{invocation['owner_namespace']}/jobs/{invocation['job_id']}"
        document=self.bindings.firestore_client.document(path).get(retry=None,timeout=15)
        require(document.exists,'Committed private job is unavailable.')
        data=document.to_dict();owner=data.get('owner_id') if isinstance(data,dict) else None
        require(isinstance(owner,str) and hashlib.sha256(owner.encode()).hexdigest()==invocation['owner_namespace'],
                'Stored job owner differs from the invocation namespace.')
        return self.store._record(data,owner,invocation['job_id'])

    def _await_operation(self,job):
        # Cloud Run can start before the caller persists its operation receipt.
        # Poll only the existing private journal; never reissue RunJob/paid work.
        deadline=self.clock()+min(30,self.config['target']['task_timeout_seconds']/4)
        # Only an authenticated, terminal-prior, bounded append-only resume can
        # supply a different dispatch journal. No worker creates a resume here.
        resumes=getattr(self,'resumes',None)
        active=resumes.active_dispatch(job) if resumes is not None else None
        dispatch=active if active is not None else self.dispatch
        while True:
            operation=dispatch.get_dispatch_operation(job.owner_id,job.id,job.snapshot_sha256)
            if operation is not None:return operation
            require(self.clock()<deadline,'Dispatch operation is still unconfirmed; retain HOLD without resubmitting.')
            self.sleep(.5)

    def run(self,invocation,root):
        require(self.enabled,'Private worker execution remains disabled.')
        invocation=validate_invocation(invocation,self.config['target'])
        job=self._load_job(invocation);owner=Principal(job.owner_id)
        reader=BoundCloudInputReader(self.bindings.storage_client,bucket=self.config['target']['bucket'],
                                    owner_namespace=invocation['owner_namespace'],job_id=job.id)
        manifest,reference=reader.manifest(invocation)
        require(manifest.get('sha256')==invocation['plan_sha256'],'Invocation plan fingerprint changed.')
        trusted={'job_id':job.id,'owner_id':job.owner_id,'snapshot_sha256':job.snapshot_sha256,'frozen_job':job.snapshot}
        validate_manifest(manifest,self.config['target'],trusted_job=trusted)
        build_execution_request(self.config['target'],manifest,reference,trusted_job=trusted)
        readback=CloudRunReadback(target=self.config['target'],read=self.read,
            expected_task_template_sha256=self.config['runtime']['cloud_run_task_template_sha256'],
            expected_service_account=self.config['runtime']['worker_service_account'],handoff_loader=lambda j:(manifest,reference),enabled=True)
        operation=self._await_operation(job)
        binding=readback.execution_resolver(operation,self.config['target'],job)
        execution=binding['execution_name'].rsplit('/',1)[1]
        require(execution==invocation['execution_id'],'This process is not the independently verified worker execution.')
        receipt=DispatchReceipt(job.id,job.snapshot_sha256,execution)
        if job.receipt is None:job=self.store.record_dispatch(job.owner_id,job.id,receipt)
        require(job.receipt==receipt,'Committed execution receipt differs from this worker.')
        if job.state=='complete':
            require(job.artifact is not None,'Completed job has no artifact proof.')
            return {'completion_reconciled':True,'already_complete':True,'artifact':job.artifact}
        return self._render(job,manifest,reference,readback,reader,root,execution)

    def _render(self,job,manifest,reference,readback,reader,root,execution):
        owner=Principal(job.owner_id)
        value=self.evidence.load(owner,job)
        def release_loader(snapshot):
            require(snapshot==value['snapshot_sha256'],'Narration loader snapshot changed.')
            return value['report'],value['report_sha256']
        def clips_loader(snapshot):
            require(snapshot==value['snapshot_sha256'],'Clip loader snapshot changed.')
            return value['clip_receipts']
        options={}
        if 'continuation_context' in value:
            def continuation_loader(snapshot):
                require(snapshot==value['snapshot_sha256'],'Continuation loader snapshot changed.')
                return value['continuation_context'],value['continuation_context_sha256']
            options['continuation_context_loader']=continuation_loader
        compositor=PortraitCandidateCompositor(release_loader,clips_loader,self.config['runtime']['ffmpeg_version'],**options)
        self.evidence.reserve_attempt(owner,job,execution)
        job=self.store.record_started(job.owner_id,job.id,execution)
        return execute_and_commit_worker(artifact_registry=self.registry,target=self.config['target'],actual_target=readback.target_provider(),
            manifest=manifest,manifest_ref=reference,job=job,execution_id=execution,reader=reader,root=root,
            compositor=compositor,semantic_verifier=self.bindings.semantic_verifier,artifact_writer=self.writer,
            expected_ffmpeg_version=self.config['runtime']['ffmpeg_version'],enabled=True,activation_check=self.bindings.activation_check)


    def _preview_graph(self,job):
        from .generation_coordinator import FirestoreGenerationPlan
        from .private_preview import FirestorePreviewAuthorizations
        from .preview_registry import FirestorePreviewRegistry,PreviewAuthorizationVerifier,PREVIEW_PREFIX
        from .preview_composition import release_hash
        common=dict(project_id=self.config['target']['project_id'],namespace=self.config['firestore']['namespace'],
                    transaction_runner=self.bindings.transaction_runner)
        plans=FirestoreGenerationPlan(self.bindings.firestore_client,**common)
        def budget_loader(current):
            plan=plans.load(current)
            require(isinstance(plan,dict) and isinstance(plan.get('budget'),dict),'Immutable preview generation budget unavailable.')
            value=plan['budget'];configured=self.config['budget']
            require(all(value.get(key)==configured[key] for key in ('scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents'))
                and all(segment['max_cost_cents']<=configured['operation_cap_cents'] for scene in plan['scenes'] for segment in scene['segments']),
                'Private preview plan exceeds configured campaign/job/operation caps.')
            return value
        authorizations=FirestorePreviewAuthorizations(self.bindings.firestore_client,**common,budget_loader=budget_loader,preview_release_sha256=release_hash())
        verifier=PreviewAuthorizationVerifier(authorizations,self.preview_evidence)
        registry=FirestorePreviewRegistry(self.bindings.firestore_client,**common,authorization_verifier=verifier)
        writer=CloudArtifactWriter(self.bindings.storage_client,bucket=self.config['target']['bucket'],prefix=PREVIEW_PREFIX)
        return authorizations,registry,writer

    def _render_preview(self,job,manifest,reference,readback,reader,root,execution,coordinator):
        from .preview_composition import PrivatePreviewCompositor,release_hash
        from .preview_worker import execute_preview_worker
        authorizations,registry,writer=self._preview_graph(job)
        authorization,auth_sha=authorizations.load(job)
        value=self.preview_evidence.load(Principal(job.owner_id),job)
        verifier=getattr(coordinator.review_loader,'verify_preview_evidence',None)
        require(callable(verifier),'Independent funded native QC archive verifier required for private preview.')
        source_verifier=getattr(coordinator,'verify_preview_source',None)
        require(callable(source_verifier),'Independent native source archive verifier required for private preview.')
        def verify_source(candidate):return source_verifier(candidate,value['report'])
        require(verify_source(value['preview_context']['plan']['source_candidate']) is True,'Preview source archive verification failed.')
        def load(snapshot):
            require(snapshot==value['snapshot_sha256'],'Preview evidence loader snapshot changed.')
            return value
        compositor=PrivatePreviewCompositor(load,expected_authorization_sha256=auth_sha,expected_preview_release_sha256=release_hash(),
            expected_ffmpeg_version=self.config['runtime']['ffmpeg_version'],preview_evidence_verifier=verifier,preview_source_verifier=verify_source)
        job=self.store.record_started(job.owner_id,job.id,execution)
        return execute_preview_worker(target=self.config['target'],actual_target=readback.target_provider(),manifest=manifest,manifest_ref=reference,
            job=job,execution_id=execution,reader=reader,root=root,compositor=compositor,semantic_verifier=self.bindings.semantic_verifier,
            artifact_writer=writer,artifact_registry=registry,evidence=self.preview_evidence,expected_authorization_sha256=auth_sha,
            expected_preview_release_sha256=release_hash(),expected_context_sha256=value['preview_context_sha256'],
            expected_ffmpeg_version=self.config['runtime']['ffmpeg_version'],activation_check=self.bindings.activation_check,enabled=True)

    def run_exact_film(self,invocation,root):
        """Render retained owner-approved media only; never build a coordinator."""
        from .exact_film_dispatch import validate_invocation
        from .exact_film_runtime import require_job_contract
        from .exact_film_assembly import build_exact_film_adapters
        from .exact_film_policy import validate_exact_film_lineage,ExactFilmNativeReplay
        from .exact_film_composition import ExactFilmCompositor,release_hash
        from .exact_film_worker import execute_exact_film_worker
        from .cloud_storage import CloudArtifactReader
        from .preview_registry import PREVIEW_PREFIX
        require(self.enabled,'Exact-film worker is disabled.')
        invocation=validate_invocation(invocation,self.config)
        job=self._load_job(invocation);owner=Principal(job.owner_id)
        require(job.snapshot_sha256==invocation['snapshot_sha256'],'Exact-film frozen snapshot changed.')
        require_job_contract(job)
        def runtime_active():
            require(callable(self.bindings.activation_check) and self.bindings.activation_check() is True,
                    'Exact-film runtime acceptance is absent/expired.')
            if self.bindings.exact_film_preflight is not None:
                require(self.bindings.exact_film_preflight(job) is True,'Current owner-scoped render-only infrastructure consent required.')
            return True
        runtime_active()
        graph=build_exact_film_adapters(config=self.config,firestore_client=self.bindings.firestore_client,
            storage_client=self.bindings.storage_client,token_reader=self.bindings.token_reader,
            activation_check=runtime_active,transaction_runner=self.bindings.transaction_runner,
            read_transport=self.read,enabled=True)
        approval=graph.actions.get_approval(job)
        require(fingerprint(approval)==invocation['approval_sha256'],'Exact-film invocation approval changed.')
        # The execution may start before the API persists its operation receipt.
        deadline=self.clock()+min(30,self.config['target']['task_timeout_seconds']/4)
        while True:
            runtime_active();record=graph.journal.get(job,invocation['approval_sha256'])
            if record is not None and record.get('operation'):break
            require(self.clock()<deadline,'Exact-film operation unconfirmed; HOLD without another run.')
            self.sleep(.5)
        receipt=graph.dispatch.recover(job,approval)
        require(receipt.execution_id==invocation['execution_id'],'Exact-film process differs from verified execution.')
        existing=graph.registry.load(owner,job)
        if existing is not None:
            return dict(completion_reconciled=True,already_complete=True,artifact=existing[0],state='EXACT_FILM_READY')
        lineage=graph.actions.approval_lineage(owner,job,approval['approval_id'])
        def active():
            runtime_active()
            require(graph.actions.get_approval(job)==approval,'Current exact-film authorization changed.')
            validate_exact_film_lineage(job,lineage,expected_final_release_sha256=release_hash())
            return True
        replay=ExactFilmNativeReplay(self,job,lineage)
        compositor=ExactFilmCompositor(job=job,lineage=lineage,preview_evidence_verifier=replay.verify_review,
            preview_source_verifier=replay.verify_source,expected_ffmpeg_version=self.config['runtime']['ffmpeg_version'])
        def preview_reference(current_owner,current_job,artifact_id):
            active()
            require(current_owner.subject==job.owner_id and current_job==job and artifact_id==job.artifact.artifact_id,
                    'This-film permission cannot read another preview.')
            return lineage['preview_reference']
        preview_reader=CloudArtifactReader(self.bindings.storage_client,bucket=self.config['target']['bucket'],
            reference_resolver=preview_reference,prefix=PREVIEW_PREFIX)
        reader=BoundCloudInputReader(self.bindings.storage_client,bucket=self.config['target']['bucket'],
            owner_namespace=invocation['owner_namespace'],job_id=job.id)
        result=execute_exact_film_worker(target=self.config['target'],actual_target=graph.readback.target_provider(),
            manifest=lineage['retained_manifest'],manifest_ref=lineage['manifest_reference'],job=job,receipt=receipt,
            reader=reader,root=root,compositor=compositor,semantic_verifier=self.bindings.semantic_verifier,
            artifact_writer=graph.writer,artifact_registry=graph.registry.for_approval(approval),evidence=graph.evidence,
            lineage=lineage,preview_reader=preview_reader,activation_check=active,
            expected_ffmpeg_version=self.config['runtime']['ffmpeg_version'],enabled=True)
        return {**result,'state':'EXACT_FILM_READY'}

    def run_original(self,invocation,root):
        """One verified original-input execution owns preparation AND rendering."""
        from .original_input import validate_original_job,OriginalInputRequestLoader
        from .generation_coordinator import GenerationCoordinator
        require(self.enabled,'Private original-input worker is disabled.')
        def activated():
            require(callable(self.bindings.activation_check) and self.bindings.activation_check() is True,
                    'Original-input runtime acceptance is absent/expired.')
        activated()
        invocation=validate_original_invocation(invocation,self.config)
        job=self._load_job(invocation)
        require(job.snapshot_sha256==invocation['snapshot_sha256'],'Invocation snapshot differs from durable job.')
        validate_original_job(job,invocation['config_sha256'])
        reader=BoundCloudInputReader(self.bindings.storage_client,bucket=self.config['target']['bucket'],
                                    owner_namespace=invocation['owner_namespace'],job_id=job.id)
        readback=CloudRunReadback(target=self.config['target'],read=self.read,
            expected_task_template_sha256=self.config['runtime']['cloud_run_task_template_sha256'],
            expected_service_account=self.config['runtime']['worker_service_account'],
            expected_request_loader=OriginalInputRequestLoader(invocation['config_sha256']),enabled=True)
        operation=self._await_operation(job);activated()
        binding=readback.execution_resolver(operation,self.config['target'],job)
        execution=binding['execution_name'].rsplit('/',1)[1]
        require(execution==invocation['execution_id'],'Process differs from verified original-input execution.')
        receipt=DispatchReceipt(job.id,job.snapshot_sha256,execution)
        if job.receipt is None:job=self.store.record_dispatch(job.owner_id,job.id,receipt)
        require(job.receipt==receipt,'Original-input execution receipt changed.')
        if job.state=='complete':
            require(job.artifact is not None,'Completed job lacks artifact proof.')
            return {'completion_reconciled':True,'already_complete':True,'artifact':job.artifact}
        if job.state=='draft_preview_ready':
            require(job.artifact is not None,'Private draft lacks a proof.')
            _,registry,_=self._preview_graph(job)
            registry.verify(Principal(job.owner_id),job,job.artifact.artifact_id)
            return {'completion_reconciled':True,'already_complete':True,'artifact':job.artifact,'artifact_kind':'PRIVATE_DRAFT_PREVIEW'}
        require(callable(self.bindings.generation_factory),'Explicit original generation services are not bound.')
        coordinator=self.bindings.generation_factory(job,self)
        require(type(coordinator) is GenerationCoordinator,'Concrete generation coordinator required.')
        try:
            activated();result=coordinator.run(job,Path(root)/'generation')
        except Exception:
            self.store.record_worker_hold(job.owner_id,job.id,execution,'generation_validation')
            raise
        if result['state'] not in ('PREPARED','PREVIEW_PREPARED'):
            reason=result.get('phase','generation_validation')
            if reason in ('source_planning_uncertainty','source_planning_candidate'):reason='source_review'
            elif reason=='source_planning_outcome_unknown':reason='provider_outcome_unknown'
            elif result['state']=='REVIEW_REQUIRED' and reason=='motion_capture':reason='output_review'
            elif reason=='motion_capture' and result.get('capture',{}).get('state') in ('IN_QUEUE','IN_PROGRESS'):reason='queue_deadline'
            self.store.record_worker_hold(job.owner_id,job.id,execution,reason)
            return dict(result,completion_reconciled=False,already_complete=False)
        activated();handoff=result['handoff']
        # No second Cloud Run dispatch: original execution receipt owns final render.
        try:
            if result['state']=='PREVIEW_PREPARED':
                return self._render_preview(job,handoff['manifest'],handoff['reference'],readback,reader,Path(root)/'private-preview',execution,coordinator)
            return self._render(job,handoff['manifest'],handoff['reference'],readback,reader,Path(root)/'render',execution)
        except Exception:
            # A failed registry response may have committed. Read only first;
            # never render/upload again to recreate an uncertain result.
            current=self.store.get(Principal(job.owner_id),job.id)
            if current is not None and current.state=='draft_preview_ready' and current.artifact is not None:
                _,registry,_=self._preview_graph(current)
                registry.verify(Principal(current.owner_id),current,current.artifact.artifact_id)
                return {'completion_reconciled':True,'already_complete':True,'artifact':current.artifact,'artifact_kind':'PRIVATE_DRAFT_PREVIEW'}
            if current is not None and current.state=='complete' and current.artifact is not None:
                current.artifact.validate(current)
                return {'completion_reconciled':True,'already_complete':True,'artifact':current.artifact}
            self.store.record_worker_hold(job.owner_id,job.id,execution,'generation_validation')
            raise


def validate_original_invocation(value,config):
    keys={'input_protocol','job_id','owner_namespace','snapshot_sha256','config_sha256','execution_id'}
    require(isinstance(value,dict) and set(value)==keys and value['input_protocol']=='originals-v1',
            'Complete versioned original-input invocation required.')
    require(isinstance(value['job_id'],str) and re.fullmatch(r'[0-9a-f]{32}',value['job_id'])
            and all(isinstance(value[k],str) and SHA.fullmatch(value[k]) for k in ('owner_namespace','snapshot_sha256','config_sha256')),
            'Original-input identities/fingerprints are invalid.')
    require(value['config_sha256']==fingerprint(config),'Invocation private configuration changed.')
    require(isinstance(value['execution_id'],str) and re.fullmatch(r'[a-z][a-z0-9-]{1,62}',value['execution_id']),
            'Explicit Cloud Run execution identity required.')
    return dict(value)


def original_invocation_from_environment(environ,config):
    fields={'input_protocol':'BYRON_INPUT_PROTOCOL','job_id':'BYRON_JOB_ID','owner_namespace':'BYRON_OWNER_NAMESPACE',
            'snapshot_sha256':'BYRON_SNAPSHOT_SHA256','config_sha256':'BYRON_CONFIG_SHA256','execution_id':'CLOUD_RUN_EXECUTION'}
    return validate_original_invocation({key:environ.get(env) for key,env in fields.items()},config)


def build_private_worker(config,sdk_lock_bytes,bindings,*,enabled=False):
    checked=validate_config(config)
    validate_runtime(checked,sdk_lock_bytes)
    return PrivateWorkerRuntime(checked,bindings,enabled=enabled,sdk_lock_bytes=sdk_lock_bytes)


def load_private_bindings(path,expected_sha256,config):
    require(isinstance(expected_sha256,str) and SHA.fullmatch(expected_sha256),'Reviewed private bindings fingerprint required.')
    raw=private_bytes(path,256_000)
    require(hashlib.sha256(raw).hexdigest()==expected_sha256,'Private bindings source changed.')
    # Execute only the bytes whose digest was checked, not a second filesystem
    # read or cached bytecode that could differ after a path replacement.
    module=ModuleType('_byron_private_worker_bindings');module.__file__=str(path)
    exec(compile(raw,str(path),'exec'),module.__dict__)
    require(callable(getattr(module,'build_worker_bindings',None)),'Private module must explicitly construct worker bindings.')
    value=module.build_worker_bindings(config)
    require(isinstance(value,WorkerBindings),'Private module returned an invalid worker binding.')
    return value


def main(argv=None):
    parser=argparse.ArgumentParser(description='Private worker: validate-only unless --execute is explicitly supplied.')
    parser.add_argument('--config',type=Path);parser.add_argument('--sdk-lock',type=Path,default=Path('/opt/byron-runtime/sdk-lock.json'))
    parser.add_argument('--processing-plan',type=Path);parser.add_argument('--acceptance',type=Path);parser.add_argument('--reviewer-public-key',type=Path)
    parser.add_argument('--bootstrap-env');parser.add_argument('--reviewer-key-sha256')
    parser.add_argument('--production-policy',type=Path);parser.add_argument('--policy-public-key',type=Path)
    parser.add_argument('--billing-contract',type=Path);parser.add_argument('--billing-public-key',type=Path)
    parser.add_argument('--infrastructure-authorization',type=Path)
    parser.add_argument('--policy-env');parser.add_argument('--policy-key-sha256');parser.add_argument('--billing-key-sha256')
    parser.add_argument('--execute',action='store_true');parser.add_argument('--legacy-private-bindings',action='store_true')
    parser.add_argument('--bindings',type=Path);parser.add_argument('--bindings-sha256')
    parser.add_argument('--invocation',type=Path);parser.add_argument('--workspace',type=Path)
    parser.add_argument('--cloud-run-env',action='store_true',help='Independently verify explicit Cloud Run job overrides.')
    args=parser.parse_args(argv)
    try:
        bootstrap=None
        if args.bootstrap_env:
            require(not any((args.config,args.processing_plan,args.acceptance,args.reviewer_public_key,args.legacy_private_bindings)),
                    'Explicit ENV configuration cannot mix private files or legacy Python.')
            from .bootstrap_bundle import load_bootstrap_env
            bootstrap=load_bootstrap_env(args.bootstrap_env,args.reviewer_key_sha256)
            config=bootstrap['config']
        else:
            require(args.config is not None and args.reviewer_key_sha256 is None,'Explicit config file or pinned ENV bundle required.')
            config=validate_config(strict_json(private_bytes(args.config,256_000)))
        lock=private_bytes(args.sdk_lock,256_000);validate_runtime(config,lock)
        if not args.execute:
            print(json.dumps({'configuration_valid':True,'runtime_valid':True,'executed':False,'live_ready':False}));return 0
        require((args.invocation is not None) != args.cloud_run_env,'Select exactly one explicit invocation source.')
        if args.cloud_run_env:
            if os.environ.get('BYRON_EXACT_FILM_PROTOCOL') is not None:
                from .exact_film_dispatch import invocation_from_environment as exact_invocation
                invocation=exact_invocation(os.environ,config)
            else:
                invocation=(original_invocation_from_environment(os.environ,config) if os.environ.get('BYRON_INPUT_PROTOCOL') is not None
                            else invocation_from_environment(os.environ,config['target']))
        else:invocation=strict_json(private_bytes(args.invocation,16384))
        if invocation.get('protocol')=='exact-film-v1':
            from .exact_film_dispatch import validate_invocation as validate_exact_invocation
            invocation=validate_exact_invocation(invocation,config);protocol='exact-film-v1'
        elif invocation.get('input_protocol')=='originals-v1':
            invocation=validate_original_invocation(invocation,config);protocol='originals-v1'
        else:
            require(args.legacy_private_bindings,'Packaged worker requires an exact declared original or render-only protocol.')
            invocation=validate_invocation(invocation,config['target']);protocol='legacy'
        if args.legacy_private_bindings:
            require(args.bindings is not None and args.bindings_sha256 and args.workspace is not None,
                    'Legacy Python execution requires explicit reviewed file/hash/workspace.')
            bindings=load_private_bindings(args.bindings,args.bindings_sha256,config);workspace=args.workspace
        else:
            require(args.bindings is None and args.bindings_sha256 is None,'External Python requires explicit legacy mode.')
            if bootstrap is None:
                require(all((args.processing_plan,args.acceptance,args.reviewer_public_key)),'Packaged worker requires signed plan/runtime acceptance.')
                from .hosted_control import validate_private_plan
                bootstrap=dict(config=config,processing_plan=validate_private_plan(strict_json(private_bytes(args.processing_plan,256_000))),
                    acceptance=strict_json(private_bytes(args.acceptance,256_000)),reviewer_public_key=private_bytes(args.reviewer_public_key,32))
            from .production_worker import build_worker_bindings,load_policy_env
            if args.policy_env:
                require(not any((args.production_policy,args.policy_public_key,args.billing_contract,args.billing_public_key,args.infrastructure_authorization)),
                        'Explicit policy ENV cannot mix policy files.')
                policy=load_policy_env(args.policy_env,args.policy_key_sha256,args.billing_key_sha256)
            elif protocol=='exact-film-v1':
                require(args.infrastructure_authorization is not None and args.policy_public_key is not None
                    and not any((args.policy_key_sha256,args.billing_key_sha256,args.production_policy,args.billing_contract,args.billing_public_key)),
                    'Render-only execution requires current signed infrastructure consent and its independent key.')
                policy=dict(infrastructure_envelope=strict_json(private_bytes(args.infrastructure_authorization,256_000)),
                    policy_public_key=private_bytes(args.policy_public_key,32))
            else:
                require(args.infrastructure_authorization is None,'Original generation cannot consume render-only infrastructure consent.')
                require(all((args.production_policy,args.policy_public_key,args.billing_contract,args.billing_public_key))
                    and not any((args.policy_key_sha256,args.billing_key_sha256)),'Explicit signed policy/billing files or pinned ENV bundle required.')
                policy=dict(policy_envelope=strict_json(private_bytes(args.production_policy,256_000)),
                    policy_public_key=private_bytes(args.policy_public_key,32),billing_envelope=strict_json(private_bytes(args.billing_contract,256_000)),
                    billing_public_key=private_bytes(args.billing_public_key,32))
            import tempfile
            if args.workspace is None:workspace=Path(tempfile.mkdtemp(prefix='byron-worker-'))
            else:
                workspace=args.workspace
                require(workspace.is_absolute() and not workspace.exists() and workspace.parent.is_dir()
                    and not any(p.is_symlink() for p in (workspace.parent,*workspace.parent.parents)),'New private execution workspace required.')
                workspace.mkdir(mode=0o700)
            bindings=build_worker_bindings(**bootstrap,sdk_lock_bytes=lock,workspace=workspace,enabled=True,protocol=protocol,**policy)
        runtime=PrivateWorkerRuntime(config,bindings,enabled=True,sdk_lock_bytes=lock)
        if invocation.get('protocol')=='exact-film-v1':result=runtime.run_exact_film(invocation,workspace/'exact-film')
        else:result=(runtime.run_original(invocation,workspace) if 'input_protocol' in invocation else runtime.run(invocation,workspace/'render'))
        print(json.dumps({'completion_reconciled':result['completion_reconciled'],'already_complete':result.get('already_complete',False),'state':result.get('state','COMPLETE')}));return 0
    except Exception:
        print('Worker configuration/execution could not be verified. No automatic retry was performed.',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
