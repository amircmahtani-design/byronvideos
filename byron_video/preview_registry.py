"""Private draft registry; never writes final artifact records or public URLs."""
from dataclasses import asdict, replace
import json
from .cloud_journal import FirestoreArtifactRegistry, FirestoreJobStore, _safe_owner
from .hosted_control import canonical, require

PREVIEW_PREFIX='byron-private-preview-v1'
PREVIEW_STATE='draft_preview_ready'
PREVIEW_KIND='PRIVATE_DRAFT_PREVIEW'


class FirestorePreviewRegistry(FirestoreArtifactRegistry):
    def __init__(self,*args,authorization_verifier=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.authorization_verifier=authorization_verifier
    def _authorized(self,job,proof):
        require(callable(self.authorization_verifier) and self.authorization_verifier(job,proof) is True,
                'Current private draft authorization and watermark proof are required.')
    def _refs(self,owner,job_id,artifact_id):
        job,final=super()._refs(owner,job_id,artifact_id)
        return job,self._doc(self._owner(owner)+'/preview_artifacts/'+artifact_id)
    def commit_verified(self,owner,job,proof,reference):
        from .preview_worker import PreviewArtifact
        require(type(proof) is PreviewArtifact and job.owner_id==_safe_owner(owner))
        proof.validate(job);self._authorized(job,proof)
        encoded_ref=self._reference(reference,proof);encoded_proof=canonical(asdict(proof))
        job_ref,artifact_ref=self._refs(owner,job.id,proof.artifact_id)
        def transaction(tx):
            current=FirestoreJobStore._record(self._read(job_ref,tx),owner,job.id)
            prior=self._read(artifact_ref,tx)
            require(current is not None and current.snapshot_sha256==job.snapshot_sha256)
            proof.validate(current)
            data=dict(owner_id=current.owner_id,job_id=current.id,snapshot_sha256=current.snapshot_sha256,
                      proof_json=encoded_proof,reference_json=encoded_ref)
            if prior is not None:
                require(prior==data and current.state==PREVIEW_STATE and current.artifact==proof,'Private draft cannot be replaced.')
                return current
            require(current.state in {'queued','running'} and current.artifact is None,'Only an uncompleted dispatched job can yield a draft.')
            tx.create(artifact_ref,data)
            tx.update(job_ref,dict(state=PREVIEW_STATE,artifact_json=encoded_proof))
            return replace(current,state=PREVIEW_STATE,artifact_json=encoded_proof)
        return self._run(transaction)
    def _load(self,owner,job,artifact_id):
        from .preview_worker import PreviewArtifact
        require(job.owner_id==_safe_owner(owner) and job.state==PREVIEW_STATE)
        _,ref=self._refs(owner,job.id,artifact_id);data=self._read(ref)
        require(isinstance(data,dict) and set(data)=={'owner_id','job_id','snapshot_sha256','proof_json','reference_json'}
                and data['owner_id']==job.owner_id and data['job_id']==job.id and data['snapshot_sha256']==job.snapshot_sha256,
                'Private draft proof is unavailable.')
        proof=PreviewArtifact(**json.loads(data['proof_json']));proof.validate(job);self._authorized(job,proof)
        require(proof==job.artifact and proof.artifact_id==artifact_id,'Private draft proof changed.')
        reference=json.loads(data['reference_json']);self._reference(reference,proof)
        return proof,reference


class PreviewAuthorizationVerifier:
    """Concrete independently stored authorization + native watermark evidence."""
    def __init__(self,authorizations,evidence):
        from .private_preview import FirestorePreviewAuthorizations
        from .preview_worker import FirestorePreviewEvidence
        require(type(authorizations) is FirestorePreviewAuthorizations and type(evidence) is FirestorePreviewEvidence,
                'Concrete private authorization and native draft evidence stores required.')
        self.authorizations,self.evidence=authorizations,evidence
    def __call__(self,job,proof):
        from .preview_worker import PreviewArtifact
        from .private_preview import validate_intent
        require(type(proof) is PreviewArtifact,'Distinct private draft proof required.')
        intent=validate_intent(job.snapshot.get('processing_plan',{}).get('preview_contract'))
        authorization,independent_sha=self.authorizations.load(job)
        require(independent_sha==proof.authorization_sha256
                and authorization['preview_policy_sha256']==proof.preview_policy_sha256==intent['preview_policy_sha256']
                and authorization['preview_release_sha256']==proof.preview_release_sha256==intent['preview_release_sha256'],
                'Private draft authorization/policy differs from the frozen intent.')
        require(self.evidence.verify(job,proof) is True,'Native private draft evidence is unavailable.')
        inputs=self.evidence.load(job.owner_id,job)
        require(inputs['preview_context']['preview_release_sha256']==intent['preview_release_sha256'],
                'Private draft compositor release differs from authorized intent.')
        return True
