"""Render-only exact-film proof/evidence. No generation coordinator or provider API."""
from dataclasses import dataclass,asdict,replace
import copy,json,re,hashlib,uuid
from pathlib import Path
from .hosted_control import VerifiedArtifact,Principal,canonical,require
from .cloud_journal import _FirestoreBoundary
from .elevenlabs_adapter import fingerprint
from .worker_binding import stage_bound_inputs,verify_artifact
from .exact_film_composition import release_hash,verify_equivalence

KIND='OWNER_APPROVED_EXACT_FILM'
PREFIX='byron-exact-film-v1'
SHA=re.compile(r'[0-9a-f]{64}\Z')

@dataclass(frozen=True)
class ExactFilmArtifact(VerifiedArtifact):
    artifact_kind:str=KIND
    approval_id:str=''
    preview_artifact_id:str=''
    approval_sha256:str=''
    preview_artifact_sha256:str=''
    preview_proof_sha256:str=''
    context_sha256:str=''
    retained_manifest_sha256:str=''
    final_release_sha256:str=''
    equivalence_sha256:str=''
    candidate_media_evidence_json: str = ''
    def validate(self,job):
        # job is an independently receipt-bound final-render view; never mutate
        # the original job's preview receipt/artifact to manufacture this view.
        super().validate(job)
        from .candidate_media_evidence import validate_evidence_json
        validate_evidence_json(self.candidate_media_evidence_json,artifact=self,job=job)
        require(all(isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',v) for v in (self.approval_id,self.preview_artifact_id)), 'Exact-film approval and preview IDs required.')
        require(self.artifact_kind==KIND and all(isinstance(v,str) and SHA.fullmatch(v) for v in
            (self.approval_sha256,self.preview_artifact_sha256,self.preview_proof_sha256,self.context_sha256,
             self.retained_manifest_sha256,self.final_release_sha256,self.equivalence_sha256)),
            'Exact-film proof requires owner approval, retained preview/media and equivalence bindings.')
        require(job.snapshot.get('processing_plan',{}).get('preview_contract',{}).get('mode')=='PRIVATE_DRAFT_PREVIEW',
                'Exact-film approval requires the original private-preview lineage.')
        return True

class FirestoreExactFilmEvidence(_FirestoreBoundary):
    def _ref(self,owner,job,approval_sha,collection):
        subject=owner.subject if isinstance(owner,Principal) else owner
        require(subject==job.owner_id and SHA.fullmatch(approval_sha),'Exact-film evidence owner/approval differs.')
        return self._doc(self._owner(owner)+'/'+collection+'/'+job.id+'_'+approval_sha)
    def reserve_attempt(self,owner,job,approval_sha256,receipt):
        receipt.validate(job)
        self._ref(owner,job,approval_sha256,'exact_film_worker_attempts')
        ref=self._doc(self._owner(owner)+'/exact_film_worker_attempts/'+job.id)
        value=dict(owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,
            approval_sha256=approval_sha256,execution_id=receipt.execution_id,status='started_unknown_until_exact_film_commit')
        def transaction(tx):
            require(self._read(ref,tx) is None,'This exact-film render was already attempted; reconcile without rerendering.')
            tx.create(ref,value);return value
        return self._run(transaction)
    def _verify_attempt(self,job,proof):
        value=self._read(self._doc(self._owner(job.owner_id)+'/exact_film_worker_attempts/'+job.id))
        require(isinstance(value,dict) and value.get('owner_id')==job.owner_id and value.get('job_id')==job.id
            and value.get('snapshot_sha256')==job.snapshot_sha256 and value.get('approval_sha256')==proof.approval_sha256
            and value.get('execution_id')==proof.execution_id and value.get('status')=='started_unknown_until_exact_film_commit',
            'Exact-film output has no matching immutable one-shot render attempt.')
    def record_output(self,owner,job,proof,render_proof,equivalence):
        require(type(proof) is ExactFilmArtifact,'Distinct exact-film artifact required.');proof.validate(job)
        self._verify_attempt(job,proof)
        require(equivalence.get('final_sha256')==proof.sha256 and equivalence.get('preview_sha256')==proof.preview_artifact_sha256
            and equivalence.get('all_frames_verified') is True and equivalence.get('decoded_audio_equal') is True
            and equivalence.get('frames_verified')==round(proof.duration_seconds*24)
            and fingerprint(equivalence)==proof.equivalence_sha256,'Exact actual decoded film equivalence required.')
        require(render_proof.get('retained_manifest_sha256')==proof.retained_manifest_sha256
            and render_proof.get('context_sha256')==proof.context_sha256 and render_proof.get('final_release_sha256')==proof.final_release_sha256
            and render_proof.get('approval_sha256')==proof.approval_sha256,'Exact retained-media render provenance differs.')
        value=dict(owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,execution_id=proof.execution_id,
            proof=asdict(proof),proof_sha256=fingerprint(asdict(proof)),render_proof=render_proof,equivalence=equivalence)
        value=json.loads(canonical(value))
        require(len(canonical(value))<=800000,'Exact-film output evidence exceeds bound.')
        ref=self._ref(owner,job,proof.approval_sha256,'exact_film_output_evidence')
        def transaction(tx):
            old=self._read(ref,tx);require(old is None or old==value,'Exact-film output evidence is immutable.')
            if old is None:tx.create(ref,json.loads(canonical(value)))
            return value
        return self._run(transaction)
    def verify(self,job,proof):
        require(type(proof) is ExactFilmArtifact,'Distinct exact-film output proof required.');proof.validate(job)
        self._verify_attempt(job,proof)
        value=self._read(self._ref(job.owner_id,job,proof.approval_sha256,'exact_film_output_evidence'))
        require(isinstance(value,dict) and value.get('owner_id')==job.owner_id and value.get('job_id')==job.id
            and value.get('snapshot_sha256')==job.snapshot_sha256 and value.get('execution_id')==proof.execution_id
            and value.get('proof_sha256')==fingerprint(asdict(proof)) and value.get('proof')==json.loads(canonical(asdict(proof))),
            'Exact-film output evidence changed.')
        equivalence=value['equivalence'];render=value['render_proof']
        require(fingerprint(equivalence)==proof.equivalence_sha256 and equivalence['final_sha256']==proof.sha256
            and equivalence['preview_sha256']==proof.preview_artifact_sha256 and equivalence['all_frames_verified'] is True
            and equivalence['decoded_audio_equal'] is True and render['approval_sha256']==proof.approval_sha256
            and render['retained_manifest_sha256']==proof.retained_manifest_sha256 and render['context_sha256']==proof.context_sha256
            and render['final_release_sha256']==proof.final_release_sha256,'Exact-film equivalence/input binding changed.')
        return True

def execute_exact_film_worker(*,target,actual_target,manifest,manifest_ref,job,receipt,reader,root,compositor,
                              semantic_verifier,artifact_writer,artifact_registry,evidence,lineage,preview_reader,
                              activation_check,expected_ffmpeg_version,enabled=False,now=None):
    """One render-only execution, with no path to provider or coordinator calls."""
    from .exact_film_policy import validate_exact_film_lineage
    from .exact_film_composition import ExactFilmCompositor
    from .preview_worker import PreviewArtifact
    def active():require(enabled is True and callable(activation_check) and activation_check() is True,
                         'Exact-film rendering is disabled or its current acceptance expired.')
    active();require(type(compositor) is ExactFilmCompositor,'Concrete retained-media exact-film compositor required.')
    require(getattr(artifact_writer,'prefix',None)==PREFIX,'Exact film must use its separate owner-private prefix.')
    validate_exact_film_lineage(job,lineage,expected_final_release_sha256=release_hash(),now=now)
    require(manifest==lineage['retained_manifest'] and manifest_ref==lineage['manifest_reference'],
            'Final invocation differs from owner-approved retained media.')
    receipt.validate(job);require(receipt.execution_id!=job.receipt.execution_id,'Separate verified render-only execution required.')
    render_job=replace(job,receipt_json=canonical(asdict(receipt)),artifact_json=None,state='running')
    owner=Principal(job.owner_id);trusted=dict(job_id=job.id,owner_id=job.owner_id,snapshot_sha256=job.snapshot_sha256,frozen_job=job.snapshot)
    staged=stage_bound_inputs(target=target,actual_target=actual_target,manifest=manifest,manifest_ref=manifest_ref,
                             trusted_job=trusted,reader=reader,root=root)
    preview_proof=PreviewArtifact(**lineage['preview_proof']);preview_proof.validate(job)
    viewed=Path(staged['root'])/'owner-viewed-preview.mp4';stream=preview_reader(owner,job,preview_proof)
    digest=hashlib.sha256();count=0
    try:
        with viewed.open('xb') as output:
            while True:
                block=stream.read(1024*1024)
                if not block:break
                require(isinstance(block,bytes),'Private preview reader must return bytes.')
                count+=len(block);require(count<=preview_proof.byte_count,'Retained viewed preview exceeded its committed byte count.')
                digest.update(block);output.write(block)
    finally:
        if hasattr(stream,'close'):stream.close()
    require(count==preview_proof.byte_count and digest.hexdigest()==preview_proof.sha256,'Viewed preview bytes differ from owner approval.')
    active();compositor.preflight(staged,job.snapshot)
    active();evidence.reserve_attempt(owner,job,lineage['approval_sha256'],receipt)
    output=Path(staged['root'])/'owner-approved-exact-film.mp4'
    active();result=compositor(staged,job.snapshot,output)
    from .candidate_media_evidence import RetainedMediaVerifier
    fidelity=RetainedMediaVerifier(compositor=compositor,staged=staged,job=job)
    media=verify_artifact(output,job=render_job,artifact_id=uuid.uuid4().hex,semantic_verifier=fidelity,
                          expected_ffmpeg_version=expected_ffmpeg_version,allow_candidate=True)
    equivalence=verify_equivalence(viewed,output,expected_frames=round(media.duration_seconds*24))
    proof=ExactFilmArtifact(**asdict(media),candidate_media_evidence_json=fidelity.evidence_json(),approval_id=lineage['approval']['approval_id'],preview_artifact_id=preview_proof.artifact_id,
        approval_sha256=lineage['approval_sha256'],preview_artifact_sha256=preview_proof.sha256,
        preview_proof_sha256=lineage['preview_proof_sha256'],context_sha256=lineage['inputs']['preview_context_sha256'],
        retained_manifest_sha256=lineage['manifest_sha256'],final_release_sha256=release_hash(),equivalence_sha256=fingerprint(equivalence))
    proof.validate(render_job)
    active();validate_exact_film_lineage(job,lineage,expected_final_release_sha256=release_hash(),now=now)
    evidence.record_output(owner,render_job,proof,result['render_proof'],equivalence)
    active();reference=artifact_writer(owner,render_job,proof.artifact_id,output,proof)
    require(hashlib.sha256(output.read_bytes()).hexdigest()==proof.sha256,'Exact-film bytes changed during private publication.')
    active();validate_exact_film_lineage(job,lineage,expected_final_release_sha256=release_hash(),now=now)
    completed=artifact_registry.commit_verified(owner,render_job,proof,reference)
    return dict(artifact=proof,private_reference=reference,completion_reconciled=True,committed_result=completed,
                artifact_kind=KIND,paid_generation_calls=0,automatic_publication=False,future_style_approval=False)
