"""Disabled fresh nine-frame candidate review; never automatic film acceptance."""
import copy
from dataclasses import fields
import hashlib
from pathlib import Path
import tempfile
from .automated_review import need,sample_evidence,prepare_request,CandidateReviewLoader
from .elevenlabs_adapter import fingerprint
from .review_budget import validate_funding,FundedReviewExecutor
from .worker_binding import safe_private_root,copy_verified_object
from .cloud_render import validate_object


class CloudOriginalReviewReader:
    """Concrete private original read with full VerifiedImage owner/generation checks."""
    def __init__(self,storage_client,*,bucket):
        from .cloud_storage import CloudUploadStore
        self.store=CloudUploadStore(storage_client,bucket=bucket)
    def __call__(self,job,image):
        from .hosted_control import Principal,VerifiedImage
        names={field.name for field in fields(VerifiedImage)}
        record=VerifiedImage(**{key:image[key] for key in names});owner=Principal(job.owner_id);record.validate(owner,record.id)
        stream=self.store.read_original(owner,record)
        try:data=stream.read(record.byte_count+1)
        finally:stream.close()
        need(len(data)==record.byte_count and hashlib.sha256(data).hexdigest()==record.sha256,'Original review bytes changed.')
        return data


class PrivateReviewEvidence:
    """Reader-backed immutable GCS clip copy and local actual nine-frame sampling."""
    def __init__(self,*,original_reader,clip_reader,bucket,workspace):
        self.original_reader=original_reader;self.clip_reader=clip_reader;self.bucket=bucket;self.workspace=Path(workspace)
    def __call__(self,*,job,image,candidate):
        root=self.workspace
        need(root.is_absolute() and root.is_dir() and not any(p.is_symlink() for p in (root,*root.parents)), 'Private nonsymlink review parent required.')
        need(not root.resolve().is_relative_to(Path(__file__).resolve().parent.parent/'web'),'Review workspace cannot be public.')
        source=self.original_reader(job,image)
        need(isinstance(source,bytes) and 0<len(source)<=20_000_000 and hashlib.sha256(source).hexdigest()==image['sha256']==candidate['source_sha256'], 'Review must use exact frozen original bytes.')
        ref=candidate['video'];prefix=f"owners/{hashlib.sha256(job.owner_id.encode()).hexdigest()}/jobs/{job.id}/segments/{image['id']}/"
        need(type(candidate.get('sequence')) is int and 0<=candidate['sequence']<=10
             and ref.get('object')==prefix+f"{candidate['sequence']:02}.mp4",'Review storage object differs from exact segment sequence.')
        validate_object(ref,bucket=self.bucket,prefix=prefix,content_type='video/mp4',max_bytes=50_000_000)
        with tempfile.TemporaryDirectory(prefix='review-',dir=root) as temporary:
            stage=safe_private_root(Path(temporary)/'media')
            clip=copy_verified_object(self.clip_reader,ref,stage/'full.mp4')
            return sample_evidence(source_bytes=source,source_sha256=image['sha256'],clip_path=clip,clip_sha256=ref['sha256'],presentation_crop=candidate['presentation_crop'])


def _native_pair(transport,responses):
    from .vertex_review import VertexReviewTransport,VertexVerifiedReviewResponseStore
    from .gemini_developer import DeveloperReviewTransport,DeveloperVerifiedReviewResponseStore
    return (type(transport),type(responses)) in ((VertexReviewTransport,VertexVerifiedReviewResponseStore),(DeveloperReviewTransport,DeveloperVerifiedReviewResponseStore))


class FundedCandidateReview:
    """Fresh native caller with explicit funds; every result remains REVIEW_REQUIRED.

    response_store should be VertexVerifiedReviewResponseStore for the native
    Vertex adapter so actual usage/native evidence is revalidated on cache replay.
    Readers and exact input-token proof loaders are private deployment bindings.
    """
    requires_film_review_funding=True
    def __init__(self,*,journal,transport,response_store,evidence_loader,activation_check,enabled=False,now=None,trusted_budget=None):
        self.trusted_budget=copy.deepcopy(trusted_budget)
        self.journal=journal;self.transport=transport;self.responses=response_store;self.evidence=evidence_loader
        self.activation=activation_check;self.enabled=enabled;self.now=now;self.binding=None
    def _active(self):
        need(self.enabled is True and callable(self.activation) and self.activation() is True,'Fresh funded review is disabled or activation expired.')
    def bind_funding(self,funding,*,job,plan):
        self._active();validate_funding(funding,job=job,continuation_plan=plan,journal=self.journal)
        need(fingerprint(job.snapshot)==job.snapshot_sha256,'Frozen job changed before review funding.')
        if plan.get('preview_schema'):
            from .vertex_review import VertexReviewTransport,VertexVerifiedReviewResponseStore
            need(_native_pair(self.transport,self.responses)
                 and self.responses.evidence is self.transport.evidence,'Private preview requires concrete native evidence replay, not a supplied clean-result callable.')
        identity=(job.owner_id,job.id,job.snapshot_sha256,fingerprint(plan),funding['review_plan_sha256'])
        if self.binding is not None:need(self.binding==identity,'Review service cannot switch immutable owner/job/plan.')
        self.binding=identity;self.job=copy.deepcopy(job);self.plan=copy.deepcopy(plan);self.funding=funding
        self.executor=FundedReviewExecutor(funding=funding,transport=self.transport,response_store=self.responses,
            activation_check=self.activation,enabled=True,now=self.now)
    def __call__(self,candidate):
        self._active();need(self.binding is not None,'Whole-film review funding must precede any fresh call.')
        validate_funding(self.funding,job=self.job,continuation_plan=self.plan,journal=self.journal)
        expected=(self.job.owner_id,self.job.id,self.job.snapshot_sha256,fingerprint(self.plan))
        need(tuple(candidate.get(k) for k in ('owner_id','job_id','snapshot_sha256','continuation_plan_sha256'))==expected,
             'Review candidate differs from exact frozen owner/job/continuation plan.')
        scenes=[s for s in self.plan['scenes'] if s['image_id']==candidate.get('image_id')]
        need(len(scenes)==1,'Unplanned review image.');scene=scenes[0];sequence=candidate.get('sequence')
        need(type(sequence) is int and 0<=sequence<len(scene['segments']) and candidate.get('source_sha256')==scene['source_sha256']
             and candidate.get('full_decode_verified') is True,'Unplanned/undecoded review segment.')
        image=next(i for i in self.job.snapshot['images'] if i['id']==scene['image_id'])
        candidate_hash=fingerprint(candidate)
        evidence=self.evidence(job=self.job,image=copy.deepcopy(image),candidate=copy.deepcopy(candidate))
        need(fingerprint(candidate)==candidate_hash and evidence['source_sha256']==image['sha256']
             and evidence['clip_sha256']==candidate['video']['sha256'] and evidence['presentation_crop']==candidate['presentation_crop']
             and abs(evidence['duration_seconds']-candidate['duration_seconds'])<=1/24
             and abs(evidence['duration_seconds']-scene['segments'][sequence]['expected_full_duration_seconds'])<=1/24,
             'Sampled evidence differs from immutable source/clip/crop.')
        from .motion_continuation import scene_prompt
        request=prepare_request(owner_id=self.job.owner_id,job_id=self.job.id,image_id=scene['image_id'],sequence=sequence,
            prompt=scene_prompt(scene),evidence=evidence,config=self.funding['review_plan']['config'])
        self._active();result=self.executor(request)
        if self.plan.get('preview_schema') and result['state']=='CANDIDATE_PASS':
            from .private_preview import make_preview_qc
            verification=self.verify_preview_evidence(result)
            return make_preview_qc(candidate=candidate,result=result,plan=self.plan,expected_plan_sha256=fingerprint(self.plan),
                authorization=self.plan['preview_authorization'],expected_authorization_sha256=self.plan['preview_authorization_sha256'],
                job=self.job,preview_release_sha256=self.plan['preview_release_sha256'],archive_verification=verification,
                now=self.now() if callable(self.now) else self.now)
        from .motion_continuation import scene_prompt
        loader=CandidateReviewLoader(lambda ignored:result,expected_prompt_loader=lambda ignored:hashlib.sha256(scene_prompt(scene).encode()).hexdigest(),enabled=True)
        return loader(candidate)

    def verify_preview_evidence(self,result):
        self._active()
        from .private_preview import verify_preview_review
        from .vertex_review import VertexReviewTransport,VertexVerifiedReviewResponseStore
        need(_native_pair(self.transport,self.responses)
             and self.responses.evidence is self.transport.evidence,'Authoritative native archive is mandatory for advancing a private preview.')
        if self.binding is not None:
            return verify_preview_review(result,funding=self.funding,response_store=self.responses)
        need(self.trusted_budget is not None,'Independent preview readback budget required before prepared resume.')
        return verify_preview_review(result,journal=self.journal,trusted_budget=self.trusted_budget,response_store=self.responses)


def build_vertex_review_factory(*,token_reader,input_proof_loader,max_input_tokens,token_limit_contract_reference,
                                workspace,activation_check,enabled=False,now=None,client_factory=None,native_api_mode='vertex_eu',key_reader=None):
    """Per-job production construction using supplied authenticated SDK clients.

    No ADC discovery, network operation, directory creation or provider request
    happens during construction. input_proof_loader(job,logical,native) must load
    independently approved exact-request token and billing proof.
    """
    import time
    now=now or time.time
    def factory(*,job,runtime,journal):
        need(enabled is True and runtime.enabled is True and callable(activation_check) and activation_check() is True,
             'Per-job native review factory is disabled.')
        from .cloud_storage import CloudProviderResponseStore
        from .hosted_control import Principal
        from .worker_bootstrap import BoundCloudInputReader
        from .vertex_review import (FirestoreVertexAuditRecords,VertexNativeEvidence,VertexVerifiedReviewResponseStore,VertexReviewTransport)
        need(journal.owner_id==job.owner_id and journal.job_id==job.id,'Native review factory journal differs from exact owner/job.')
        config=runtime.config;bindings=runtime.bindings;project=config['target']['project_id'];bucket=config['target']['bucket']
        need(native_api_mode in ('vertex_eu','gemini_developer') and native_api_mode==config.get('native_api_mode','vertex_eu'),'Review route differs from explicit runtime.')
        auth={'token_reader':token_reader}
        if native_api_mode=='gemini_developer':
            from .gemini_developer import FirestoreDeveloperAuditRecords as FirestoreVertexAuditRecords,DeveloperNativeEvidence as VertexNativeEvidence,DeveloperVerifiedReviewResponseStore as VertexVerifiedReviewResponseStore,DeveloperReviewTransport as VertexReviewTransport
            need(callable(key_reader),'Explicit Developer key reader required.');auth={'key_reader':key_reader}
        owner=Principal(job.owner_id)
        archive=CloudProviderResponseStore(bindings.storage_client,bucket=bucket,owner=owner,job_id=job.id)
        records=FirestoreVertexAuditRecords(bindings.firestore_client,project_id=project,namespace=config['firestore']['namespace'],
            owner_id=job.owner_id,job_id=job.id,transaction_runner=bindings.transaction_runner)
        native_evidence=VertexNativeEvidence(archive=archive,records=records,project=project)
        responses=VertexVerifiedReviewResponseStore(archive,native_evidence)
        transport=VertexReviewTransport(project=project,**auth,
            input_proof_loader=lambda logical,native:input_proof_loader(job,logical,native),max_input_tokens=max_input_tokens,
            token_limit_contract_reference=token_limit_contract_reference,native_evidence=native_evidence,
            activation_check=activation_check,enabled=True,now=now,client_factory=client_factory)
        clip_reader=BoundCloudInputReader(bindings.storage_client,bucket=bucket,owner_namespace=hashlib.sha256(job.owner_id.encode()).hexdigest(),job_id=job.id)
        evidence=PrivateReviewEvidence(original_reader=CloudOriginalReviewReader(bindings.storage_client,bucket=bucket),
            clip_reader=clip_reader,bucket=bucket,workspace=workspace)
        return FundedCandidateReview(journal=journal,transport=transport,response_store=responses,evidence_loader=evidence,
            activation_check=activation_check,enabled=True,now=now,
            trusted_budget=dict(scope_sha256=hashlib.sha256(config["budget"]["scope"].encode()).hexdigest(),
                approved_total_cap_cents=config["budget"]["approved_total_cap_cents"],prior_spend_cents=config["budget"]["prior_spend_cents"],
                job_cap_cents=config["budget"]["job_cap_cents"]))
    return factory
