"""Owner-private draft proof/attempt/evidence, separate from final release state."""
from dataclasses import dataclass,asdict
import copy,hashlib,json,math,re,uuid
from pathlib import Path
from .hosted_control import VerifiedArtifact,Principal,canonical,require
from .cloud_journal import _FirestoreBoundary
from .elevenlabs_adapter import fingerprint
from .worker_binding import stage_bound_inputs,verify_artifact
from .preview_composition import verify_watermark,release_hash

KIND='PRIVATE_DRAFT_PREVIEW'
SHA=re.compile(r'[0-9a-f]{64}\Z')


@dataclass(frozen=True)
class PreviewArtifact(VerifiedArtifact):
    artifact_kind: str = KIND
    authorization_sha256: str = ''
    preview_policy_sha256: str = ''
    preview_release_sha256: str = ''
    context_sha256: str = ''
    watermark_proof_sha256: str = ''
    candidate_media_evidence_json: str = ''
    def validate(self,job):
        super().validate(job)
        from .candidate_media_evidence import validate_evidence_json
        validate_evidence_json(self.candidate_media_evidence_json,artifact=self,job=job)
        require(self.artifact_kind==KIND and all(isinstance(value,str) and SHA.fullmatch(value) for value in
            (self.authorization_sha256,self.preview_policy_sha256,self.preview_release_sha256,self.context_sha256,self.watermark_proof_sha256)),
            'Private draft proof requires independent authorization/policy/context/watermark bindings.')
        from .private_preview import validate_intent
        intent=validate_intent(job.snapshot.get('processing_plan',{}).get('preview_contract'))
        require(intent['preview_policy_sha256']==self.preview_policy_sha256 and intent['preview_release_sha256']==self.preview_release_sha256,
                'Private draft proof differs from independently frozen preview intent.')
        return True


class FirestorePreviewEvidence(_FirestoreBoundary):
    """Immutable input and actual decoded-watermark output evidence, never final."""
    def _ref(self,owner,job_id,collection):
        require(isinstance(job_id,str) and re.fullmatch(r'[0-9a-f]{32}',job_id),'Private preview job required.')
        return self._doc(self._owner(owner)+'/'+collection+'/'+job_id)
    def commit(self,owner,job,report,clip_receipts,*,preview_context=None,continuation_context=None):
        require((preview_context is None)!=(continuation_context is None),'Exactly one preview context required.')
        preview_context=preview_context if preview_context is not None else continuation_context
        subject=owner.subject if isinstance(owner,Principal) else owner
        require(subject==job.owner_id and fingerprint(job.snapshot)==job.snapshot_sha256,'Preview evidence belongs to another job.')
        require(preview_context.get('status')=='PRIVATE_DRAFT_PREVIEW_CONTEXT' and preview_context.get('schema_version')==2
            and preview_context.get('owner_id')==job.owner_id and preview_context.get('job_id')==job.id
            and preview_context.get('snapshot_sha256')==job.snapshot_sha256,'Distinct owner-bound preview context required.')
        require(report.get('exact_text')==job.snapshot['narrated_text'] and report.get('profile_sha256')==fingerprint(job.snapshot['processing_plan']['narration_profile'])
            and preview_context.get('narration_report_sha256')==fingerprint(report),'Preview native narration changed.')
        require([x.get('image_id') for x in clip_receipts]==job.snapshot['input_order']
            and all(x.get('schema')=='byron-private-preview-clip-v1' and x.get('human_approved') is False
                    and 'no_invented_action_reviewed' not in x for x in clip_receipts),'Preview evidence must not contain fake human review.')
        value=dict(owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,report=report,report_sha256=fingerprint(report),
            clip_receipts=clip_receipts,preview_context=preview_context,preview_context_sha256=fingerprint(preview_context))
        require(len(canonical(value))<=800000,'Preview evidence exceeds its bound.')
        return self._immutable(self._ref(owner,job.id,'preview_worker_evidence'),value)
    def _immutable(self,ref,value):
        value=json.loads(canonical(value))
        def transaction(tx):
            old=self._read(ref,tx);require(old is None or old==value,'Private preview evidence is immutable.')
            if old is None:tx.create(ref,value)
            return value
        return self._run(transaction)
    def load(self,owner,job):
        value=self._read(self._ref(owner,job.id,'preview_worker_evidence'))
        require(isinstance(value,dict) and value.get('owner_id')==job.owner_id and value.get('job_id')==job.id
            and value.get('snapshot_sha256')==job.snapshot_sha256 and fingerprint(value['report'])==value['report_sha256']
            and fingerprint(value['preview_context'])==value['preview_context_sha256'],'Preview input evidence changed.')
        require((owner.subject if isinstance(owner,Principal) else owner)==job.owner_id,'Private preview owner differs.')
        return copy.deepcopy(value)
    def reserve_attempt(self,owner,job,execution_id):
        require((owner.subject if isinstance(owner,Principal) else owner)==job.owner_id,'Private preview attempt owner differs.')
        require(job.receipt is not None and job.receipt.execution_id==execution_id,'Committed preview execution required.')
        ref=self._ref(owner,job.id,'preview_worker_attempts')
        value=dict(owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,execution_id=execution_id,
                   status='started_unknown_until_private_draft_commit')
        def transaction(tx):
            require(self._read(ref,tx) is None,'Private preview rendering was already attempted; reconcile without rerendering.')
            tx.create(ref,value);return value
        return self._run(transaction)
    def record_output(self,owner,job,proof,watermark):
        require(type(proof) is PreviewArtifact,'Only a distinct private draft output belongs here.');proof.validate(job)
        value=self.load(owner,job);context=value['preview_context']
        require(proof.context_sha256==value['preview_context_sha256'] and proof.authorization_sha256==context['authorization_sha256']
            and proof.preview_release_sha256==context['preview_release_sha256'] and proof.preview_policy_sha256==context['authorization']['preview_policy_sha256'],'Private draft proof differs from committed authorization/context.')
        require(watermark.get('artifact_sha256')==proof.sha256 and watermark.get('all_frames_verified') is True
            and watermark.get('method')=='all-decoded-frame-glyph-template-v1'
            and watermark.get('frames_verified')==round(proof.duration_seconds*24)
            and fingerprint(watermark)==proof.watermark_proof_sha256,'Actual whole-film decoded watermark evidence required.')
        output=dict(owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,execution_id=proof.execution_id,
                    proof_sha256=fingerprint(asdict(proof)),proof=asdict(proof),watermark=watermark)
        return self._immutable(self._ref(owner,job.id,'preview_output_evidence'),output)
    def load_output(self,job):
        value=self._read(self._ref(job.owner_id,job.id,'preview_output_evidence'))
        require(isinstance(value,dict) and value.get('owner_id')==job.owner_id and value.get('job_id')==job.id
            and value.get('snapshot_sha256')==job.snapshot_sha256 and fingerprint(value['proof'])==value['proof_sha256'],
            'Private draft output evidence missing or changed.')
        proof=PreviewArtifact(**value['proof']);proof.validate(job)
        require(fingerprint(value['watermark'])==proof.watermark_proof_sha256 and value['watermark']['artifact_sha256']==proof.sha256
            and value['watermark']['all_frames_verified'] is True,'Private draft watermark output binding changed.')
        return copy.deepcopy(value)
    def verify(self,job,proof):
        require(type(proof) is PreviewArtifact,'Only private draft evidence is accepted.')
        output=self.load_output(job);inputs=self.load(job.owner_id,job)
        require(output['proof_sha256']==fingerprint(asdict(proof)) and inputs['preview_context_sha256']==proof.context_sha256
            and inputs['preview_context']['authorization_sha256']==proof.authorization_sha256
            and inputs['preview_context']['preview_release_sha256']==proof.preview_release_sha256
            and inputs['preview_context']['authorization']['preview_policy_sha256']==proof.preview_policy_sha256,'Private draft proof or authorization changed.')
        return True


def execute_preview_worker(*,target,actual_target,manifest,manifest_ref,job,execution_id,reader,root,compositor,
                           semantic_verifier,artifact_writer,artifact_registry,evidence,expected_authorization_sha256,
                           expected_preview_release_sha256,expected_context_sha256,expected_ffmpeg_version,
                           activation_check,enabled=False):
    """One private draft render; no provider calls, no final artifact mutation."""
    def active():require(enabled is True and callable(activation_check) and activation_check() is True,'Private preview worker is disabled/expired.')
    require(getattr(artifact_writer,'prefix',None)=='byron-private-preview-v1','Draft output must use its separate private storage prefix.')
    active();require(expected_preview_release_sha256==release_hash(),'Preview release differs from the approved binding.')
    require(job.receipt is not None and job.receipt.execution_id==execution_id,'Preview execution receipt changed.')
    trusted=dict(job_id=job.id,owner_id=job.owner_id,snapshot_sha256=job.snapshot_sha256,frozen_job=job.snapshot)
    staged=stage_bound_inputs(target=target,actual_target=actual_target,manifest=manifest,manifest_ref=manifest_ref,
        trusted_job=trusted,reader=reader,root=root)
    owner=Principal(job.owner_id);inputs=evidence.load(owner,job)
    require(inputs['preview_context_sha256']==expected_context_sha256
        and inputs['preview_context']['authorization_sha256']==expected_authorization_sha256
        and inputs['preview_context']['preview_release_sha256']==expected_preview_release_sha256,'Committed preview evidence differs from independent bindings.')
    active();evidence.reserve_attempt(owner,job,execution_id)
    output=Path(staged['root'])/'private-draft-preview.mp4';active();compositor(staged,job.snapshot,output)
    from .candidate_media_evidence import RetainedMediaVerifier
    fidelity=RetainedMediaVerifier(compositor=compositor,staged=staged,job=job)
    media=verify_artifact(output,job=job,artifact_id=uuid.uuid4().hex,semantic_verifier=fidelity,expected_ffmpeg_version=expected_ffmpeg_version,allow_candidate=True)
    # Independently decode the actual final file AGAIN; ignore compositor metadata.
    watermark=verify_watermark(output,expected_frames=round(media.duration_seconds*24))
    proof=PreviewArtifact(**asdict(media),candidate_media_evidence_json=fidelity.evidence_json(),authorization_sha256=expected_authorization_sha256,
        preview_policy_sha256=inputs['preview_context']['authorization']['preview_policy_sha256'],preview_release_sha256=expected_preview_release_sha256,context_sha256=expected_context_sha256,
        watermark_proof_sha256=fingerprint(watermark));proof.validate(job)
    active();evidence.record_output(owner,job,proof,watermark)
    active();reference=artifact_writer(owner,job,proof.artifact_id,output,proof)
    require(hashlib.sha256(output.read_bytes()).hexdigest()==proof.sha256,'Preview changed during private publication.')
    active();completed=artifact_registry.commit_verified(owner,job,proof,reference)
    return dict(artifact=proof,private_reference=reference,completion_reconciled=True,committed_job=completed,
                artifact_kind=KIND,human_approved=False,live_release=False)
