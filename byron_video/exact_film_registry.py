"""Separate immutable exact-film copy; the original DRAFT job is never replaced."""
from dataclasses import asdict,replace
import json
from types import SimpleNamespace
from .hosted_control import Principal,DispatchReceipt,canonical,digest,require,JOB_ID,SAFE_ID
from .cloud_journal import _FirestoreBoundary,FirestoreJobStore,FirestoreArtifactRegistry,_safe_owner

EXACT_PREFIX='byron-exact-film-v1'
EXACT_KIND='OWNER_APPROVED_EXACT_FILM'


class FirestoreExactFilmRegistry(_FirestoreBoundary):
    def __init__(self,*args,actions,receipt_loader,evidence_verifier,**kwargs):
        super().__init__(*args,**kwargs)
        self.actions,self.receipt_loader,self.evidence_verifier=actions,receipt_loader,evidence_verifier
    def _refs(self,owner,job_id,artifact_id=None):
        require(isinstance(job_id,str) and JOB_ID.fullmatch(job_id),'Private film identity required.')
        base=self._owner(owner)
        result=(self._doc(base+'/jobs/'+job_id),self._doc(base+'/exact_film_completions/'+job_id))
        if artifact_id is not None:
            require(isinstance(artifact_id,str) and SAFE_ID.fullmatch(artifact_id),'Exact artifact identity required.')
            result+=(self._doc(base+'/exact_film_artifacts/'+artifact_id),)
        return result
    def _original(self,owner,job):
        require(job.owner_id==_safe_owner(owner),'Private exact film belongs to another owner.')
        ref,_=self._refs(owner,job.id)
        original=FirestoreJobStore._record(self._read(ref),owner,job.id)
        require(original is not None and original.state=='draft_preview_ready'
                and original.snapshot_sha256==job.snapshot_sha256,'Original private draft is unavailable or changed.')
        return original
    def _binding(self,owner,job,approval_id,proof,*,retained_read=False):
        from .exact_film_worker import ExactFilmArtifact
        require(type(proof) is ExactFilmArtifact,'Distinct exact-film proof required.')
        original=self._original(owner,job)
        principal=Principal(original.owner_id)
        if retained_read:
            approval=self.actions.read_approval(principal,original,approval_id,require_current=False)
            try:self.actions.read_approval(principal,original,approval_id)
            except Exception:
                require(self.actions.authorize_view(principal,original,proof.artifact_id,proof.sha256,digest(asdict(proof))) is True,
                        'Current exact retained-artifact view lease required.')
        else:approval=self.actions.read_approval(principal,original,approval_id)
        receipt=self.receipt_loader(original,approval)
        require(type(receipt) is DispatchReceipt,'Independently verified exact-film execution receipt required.')
        receipt.validate(original)
        render_job=replace(original,receipt_json=canonical(asdict(receipt)),artifact_json=None,state='running')
        proof.validate(render_job)
        require(proof.approval_id==approval_id and proof.preview_artifact_id==original.artifact.artifact_id
            and proof.approval_sha256==digest(approval)
            and proof.preview_artifact_sha256==original.artifact.sha256
            and proof.preview_proof_sha256==digest(asdict(original.artifact))
            and proof.context_sha256==approval['scope']['context_sha256']
            and proof.retained_manifest_sha256==approval['scope']['retained_manifest_sha256']
            and proof.final_release_sha256==approval['scope']['final_release_sha256'],
            'Exact-film proof differs from the owner-approved retained preview.')
        require(callable(self.evidence_verifier) and self.evidence_verifier(render_job,proof) is True,
                'Native same-media equivalence evidence is required.')
        return original,render_job,approval,receipt
    def for_approval(self,approval):
        approval_id=approval['approval_id']
        return SimpleNamespace(commit_verified=lambda owner,job,proof,reference:self.commit_verified(owner,job,proof,reference,approval_id=approval_id))
    def commit_verified(self,owner,job,proof,reference,*,approval_id):
        original,render_job,approval,receipt=self._binding(owner,job,approval_id,proof)
        encoded_reference=FirestoreArtifactRegistry._reference(reference,proof)
        job_ref,completion_ref,artifact_ref=self._refs(owner,job.id,proof.artifact_id)
        data=dict(owner_id=original.owner_id,job_id=original.id,snapshot_sha256=original.snapshot_sha256,
            approval_id=approval_id,proof_json=canonical(asdict(proof)),receipt_json=canonical(asdict(receipt)),reference_json=encoded_reference)
        def transaction(tx):
            current=FirestoreJobStore._record(self._read(job_ref,tx),owner,job.id)
            completion=self._read(completion_ref,tx);prior=self._read(artifact_ref,tx)
            require(current==original,'Original preview changed during exact-film commit.')
            if completion is not None or prior is not None:
                require(completion==prior==data,'Exact-film artifact cannot be replaced.')
                return render_job
            tx.create(completion_ref,data);tx.create(artifact_ref,data)
            return render_job
        return self._run(transaction)
    def load(self,owner,job):
        original=self._original(owner,job)
        _,completion_ref=self._refs(owner,job.id);data=self._read(completion_ref)
        if data is None:return None
        require(isinstance(data,dict) and set(data)=={'owner_id','job_id','snapshot_sha256','approval_id','proof_json','receipt_json','reference_json'}
                and (data['owner_id'],data['job_id'],data['snapshot_sha256'])==(original.owner_id,original.id,original.snapshot_sha256),
                'Exact-film completion belongs to another film.')
        from .exact_film_worker import ExactFilmArtifact
        proof=ExactFilmArtifact(**json.loads(data['proof_json']))
        original,render_job,approval,receipt=self._binding(owner,original,data['approval_id'],proof,retained_read=True)
        require(data['receipt_json']==canonical(asdict(receipt)),'Exact-film receipt changed.')
        _,_,artifact_ref=self._refs(owner,job.id,proof.artifact_id)
        require(self._read(artifact_ref)==data,'Exact-film immutable artifact record changed.')
        reference=json.loads(data['reference_json']);FirestoreArtifactRegistry._reference(reference,proof)
        return proof,reference,render_job,approval
    def descriptor(self,owner,job):
        original=self._original(owner,job)
        _,ref=self._refs(owner,job.id);data=self._read(ref)
        if data is None:return None
        require((data.get('owner_id'),data.get('job_id'),data.get('snapshot_sha256'))==(original.owner_id,original.id,original.snapshot_sha256))
        from .exact_film_worker import ExactFilmArtifact
        proof=ExactFilmArtifact(**json.loads(data['proof_json']))
        receipt=DispatchReceipt(**json.loads(data['receipt_json']));receipt.validate(original)
        proof.validate(replace(original,receipt_json=data['receipt_json'],artifact_json=None,state='running'))
        _,_,artifact_ref=self._refs(owner,job.id,proof.artifact_id)
        require(self._read(artifact_ref)==data,'Retained exact-film descriptor changed.')
        return dict(artifact_id=proof.artifact_id,artifact_kind=EXACT_KIND)
    def __call__(self,owner,job,artifact_id):
        value=self.load(owner,job)
        require(value is not None and value[0].artifact_id==artifact_id,'Private exact-film artifact not found.')
        return value[1]


class RetainedPreviewViewVerifier:
    """Only a new owner VIEW_ONLY lease can replace expired preview read authority."""
    def __init__(self,primary,actions,evidence):
        self.primary,self.actions,self.evidence=primary,actions,evidence
    def __call__(self,job,proof):
        try:
            if self.primary(job,proof) is True:return True
        except Exception:pass
        from .preview_worker import PreviewArtifact
        require(type(proof) is PreviewArtifact,'Only this retained DRAFT can use a preview view lease.')
        proof.validate(job)
        require(self.actions.authorize_view(Principal(job.owner_id),job,proof.artifact_id,proof.sha256,digest(asdict(proof))) is True,
                'Current owner-only retained DRAFT view lease required.')
        require(self.evidence.verify(job,proof) is True,'Retained DRAFT native evidence changed.')
        return True
