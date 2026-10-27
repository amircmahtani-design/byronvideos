"""Exact retained-film owner actions; never generation, sharing or global style.

HTTP callers must already verify signed identity, owner scope, CSRF and origin.
All media/policy hashes are loaded privately; client JSON supplies opaque handles
only. Generation validity uses Firestore snapshot.create_time, never client time.
"""
import copy
from dataclasses import asdict
from datetime import datetime,timezone
import hashlib
import json
import math
import re
import secrets
import time
from .cloud_journal import _FirestoreBoundary,_key
from .hosted_control import Principal,canonical,require
from .elevenlabs_adapter import fingerprint
from .private_preview import validate_authorization,validate_intent

RENDER='RENDER_THIS_FILM_PRIVATELY_WITHOUT_DRAFT_WATERMARK'
VIEW='VIEW_THIS_PRIVATE_ARTIFACT'
APPROVAL_SCHEMA='byron-exact-film-approval-v1'
VIEW_SCHEMA='byron-owner-artifact-view-v1'
CHALLENGE_SCHEMA='byron-exact-film-challenge-v1'


def _owner(owner,job):
    require(type(owner) is Principal and owner.subject==job.owner_id,'Current authenticated owner principal required; never an email assertion.')
    require(fingerprint(job.snapshot)==job.snapshot_sha256,'Frozen original job changed.')
    from .exact_film_runtime import require_job_contract
    require_job_contract(job)
    return owner.subject


def _hash(value):return hashlib.sha256(value.encode()).hexdigest()


def _commit(snapshot,*,document_path,proof_sha256,now):
    value=getattr(snapshot,'create_time',None)
    require(isinstance(value,datetime) and value.tzinfo is not None,'Authoritative Firestore create_time is required; no caller/default timestamp fallback.')
    epoch=value.timestamp()
    require(math.isfinite(epoch) and 0<epoch<=now+60,'Invalid authoritative artifact commit time.')
    return dict(document_path=document_path,created_at_epoch=epoch,proof_sha256=proof_sha256,
                timestamp_source='firestore_document_create_time')


def _scope(lineage,final_release_sha256):
    proof=lineage['preview_proof'];context=lineage['inputs']['preview_context'];auth=lineage['authorization']
    target=lineage.get('target_proof',proof)
    return dict(owner_id=proof['owner_id'],job_id=proof['job_id'],snapshot_sha256=proof['snapshot_sha256'],
        artifact_id=target['artifact_id'],artifact_sha256=target['sha256'],artifact_proof_sha256=fingerprint(target),artifact_kind=target['artifact_kind'],
        preview_artifact_id=proof['artifact_id'],preview_artifact_sha256=proof['sha256'],preview_proof_sha256=fingerprint(proof),
        generation_authorization_sha256=proof['authorization_sha256'],generation_commit_sha256=fingerprint(lineage['generation_commit']),
        context_sha256=lineage['inputs']['preview_context_sha256'],retained_manifest_sha256=lineage['manifest_sha256'],
        retained_manifest_reference_sha256=fingerprint(lineage['manifest_reference']),narration_report_sha256=lineage['inputs']['report_sha256'],
        clip_receipts_sha256=fingerprint(lineage['inputs']['clip_receipts']),preview_policy_sha256=auth['preview_policy_sha256'],
        source_policy_sha256=auth['source_policy_sha256'],review_policy_sha256=auth['review_policy_sha256'],preview_release_sha256=auth['preview_release_sha256'],
        final_release_sha256=final_release_sha256)


class FirestoreRetainedPreviewLineage(_FirestoreBoundary):
    """Actual immutable retained evidence and authoritative generation commit read."""
    def __init__(self,*args,budget_loader,target,preview_evidence,now=None,**kwargs):
        super().__init__(*args,**kwargs)
        from .preview_worker import FirestorePreviewEvidence
        from .cloud_render import validate_target
        require(type(preview_evidence) is FirestorePreviewEvidence,'Concrete retained preview evidence store required.')
        self.budget_loader=budget_loader;self.target=validate_target(target);self.evidence=preview_evidence;self.now=now or time.time
    def _snapshot(self,path):
        ref=self._doc(path);snap=ref.get();require(snap.exists,'Retained private evidence is unavailable.')
        return snap,snap.to_dict(),ref
    def load(self,owner,job,artifact_id):
        _owner(owner,job)
        from .preview_worker import PreviewArtifact
        from .continuation_worker import FirestorePreparedContinuation
        from .cloud_render import validate_manifest,validate_object
        base=self._owner(job.owner_id)
        snap,data,ref=self._snapshot(base+'/preview_artifacts/'+artifact_id)
        require(data.get('owner_id')==job.owner_id and data.get('job_id')==job.id and data.get('snapshot_sha256')==job.snapshot_sha256,'Retained preview belongs to another owner/job.')
        proof_dict=json.loads(data['proof_json']);proof=PreviewArtifact(**proof_dict);proof.validate(job)
        require(proof.artifact_id==artifact_id and job.artifact==proof,'Only the exact retained preview may be acted on.')
        commit=_commit(snap,document_path=getattr(ref,'path',self.root+'/'+base+'/preview_artifacts/'+artifact_id),proof_sha256=fingerprint(proof_dict),now=self.now())
        reference=json.loads(data['reference_json'])
        from .cloud_journal import FirestoreArtifactRegistry
        FirestoreArtifactRegistry._reference(reference,proof)
        _,stored_auth,_=self._snapshot(base+'/preview_authorizations/'+job.id)
        authorization=json.loads(stored_auth['authorization_json'])
        require(stored_auth['authorization_sha256']==proof.authorization_sha256,'Historical generation authorization changed.')
        validate_authorization(authorization,proof.authorization_sha256,job=job,budget=self.budget_loader(job),preview_release_sha256=proof.preview_release_sha256,
            now=commit['created_at_epoch'])
        self.evidence.verify(job,proof);inputs=self.evidence.load(owner,job)
        require(inputs['preview_context_sha256']==proof.context_sha256 and inputs['preview_context']['authorization']==authorization,'Retained context/authorization changed.')
        registry=FirestorePreparedContinuation(self.client,project_id=self.client.project,namespace=self.root.split('/',1)[1],transaction_runner=self._runner)
        manifest,manifest_ref=registry.load(job)
        trusted=dict(job_id=job.id,owner_id=job.owner_id,snapshot_sha256=job.snapshot_sha256,frozen_job=job.snapshot)
        validate_manifest(manifest,self.target,trusted_job=trusted)
        validate_object(manifest_ref,bucket=self.target['bucket'],prefix=f'owners/{_hash(job.owner_id)}/jobs/{job.id}/',content_type='application/json',max_bytes=2_000_000)
        require(manifest_ref['sha256']==_hash(canonical(manifest)) and manifest_ref['bytes']==len(canonical(manifest).encode()),'Retained manifest reference changed exact bytes.')
        return dict(preview_proof=proof_dict,preview_proof_sha256=fingerprint(proof_dict),preview_reference=reference,
            preview_committed_at=commit['created_at_epoch'],generation_commit=commit,inputs=inputs,context=inputs['preview_context'],context_sha256=inputs['preview_context_sha256'],
            retained_manifest=manifest,manifest_reference=manifest_ref,manifest_sha256=manifest['sha256'],authorization=authorization)
    def load_artifact(self,owner,job,artifact_id):
        """View-only lookup of a preview OR already completed exact clean film."""
        _owner(owner,job)
        if artifact_id==job.artifact.artifact_id:return self.load(owner,job,artifact_id)
        _,row,_=self._snapshot(self._owner(job.owner_id)+'/exact_film_artifacts/'+artifact_id)
        require((row.get('owner_id'),row.get('job_id'),row.get('snapshot_sha256'))==(job.owner_id,job.id,job.snapshot_sha256),'Clean retained film belongs to another owner/job.')
        approval_ref=self._doc(self._owner(job.owner_id)+'/exact_film_approvals/'+row['approval_id'])
        approval=self._read(approval_ref);require(isinstance(approval,dict),'Original exact-film approval is unavailable.')
        _validate_record(approval,job=job,action=RENDER,now=self.now(),require_current=False)
        proof=json.loads(row['proof_json']);reference=json.loads(row['reference_json'])
        from .exact_film_worker import ExactFilmArtifact
        parsed=ExactFilmArtifact(**proof)
        from .cloud_journal import FirestoreArtifactRegistry
        FirestoreArtifactRegistry._reference(reference,parsed)
        # Worker/final registry owns execution proof validation; bind all retained lineage here too.
        require(proof['artifact_id']==artifact_id and proof['owner_id']==job.owner_id and proof['job_id']==job.id
                and proof['snapshot_sha256']==job.snapshot_sha256 and proof['artifact_kind']=='OWNER_APPROVED_EXACT_FILM'
                and proof['approval_sha256']==fingerprint(approval),'Clean film differs from exact immutable approval.')
        lineage=self.load(owner,job,approval['scope']['preview_artifact_id'])
        require(approval['scope']==_scope(lineage,approval['scope']['final_release_sha256'])
                and proof['preview_artifact_sha256']==lineage['preview_proof']['sha256']
                and proof['preview_proof_sha256']==lineage['preview_proof_sha256'] and proof['context_sha256']==lineage['context_sha256']
                and proof['retained_manifest_sha256']==lineage['manifest_sha256'] and proof['final_release_sha256']==approval['scope']['final_release_sha256'],
                'Clean film changed approved retained media/context/release.')
        require(reference['sha256']==proof['sha256'] and reference['byte_count']==proof['byte_count'],'Clean retained reference changed.')
        return {**lineage,'target_proof':proof,'target_reference':reference,'original_film_approval':approval}


def _validate_record(record,*,job,action,now,require_current=True):
    expected_schema=APPROVAL_SCHEMA if action==RENDER else VIEW_SCHEMA
    fields={'schema','action','owner_id','actor_subject','job_id','snapshot_sha256','scope','scope_sha256','challenge_id','issued_at','expires_at','owner_only','paid_generation_allowed','publication_allowed','future_style_approved', 'approval_id' if action==RENDER else 'lease_id'}
    require(isinstance(record,dict) and set(record)==fields,'Unexpected owner-action grant fields.')
    require(isinstance(record,dict) and record.get('schema')==expected_schema and record.get('action')==action
            and record.get('owner_id')==job.owner_id and record.get('job_id')==job.id and record.get('snapshot_sha256')==job.snapshot_sha256
            and record.get('actor_subject')==job.owner_id and record.get('scope_sha256')==fingerprint(record.get('scope'))
            and record.get('paid_generation_allowed') is False and record.get('publication_allowed') is False
            and record.get('future_style_approved') is False and record.get('owner_only') is True,'Exact owner action proof changed scope/rights.')
    require(type(record.get('issued_at')) in (int,float) and type(record.get('expires_at')) in (int,float)
            and math.isfinite(record['issued_at']) and math.isfinite(record['expires_at']) and 0<record['issued_at']<record['expires_at'],
            'Finite server-issued owner action interval required.')
    if require_current:require(record['issued_at']<=now<record['expires_at'],'Exact owner action expired; no generation permit is renewed.')
    return record


class FirestoreExactFilmActions(_FirestoreBoundary):
    def __init__(self,*args,lineage_loader,final_release_sha256,now=None,challenge_ttl_seconds=300,render_ttl_seconds=3600,view_ttl_seconds=3600,**kwargs):
        super().__init__(*args,**kwargs)
        require(type(lineage_loader) is FirestoreRetainedPreviewLineage,'Concrete retained-lineage loader required.')
        from .automated_review import SHA
        require(isinstance(final_release_sha256,str) and SHA.fullmatch(final_release_sha256),'Independently configured exact final release required.')
        require(all(type(v) is int and 30<=v<=86400 for v in (challenge_ttl_seconds,render_ttl_seconds,view_ttl_seconds)),'Bounded explicit owner-action lifetimes required.')
        self.lineage=lineage_loader;self.release=final_release_sha256;self.now=now or time.time
        self.challenge_ttl=challenge_ttl_seconds;self.render_ttl=render_ttl_seconds;self.view_ttl=view_ttl_seconds
    def _base(self,owner,job):_owner(owner,job);return self._owner(job.owner_id)
    def _request_ref(self,owner,job,key):return self._doc(self._base(owner,job)+'/exact_film_requests/'+_hash(job.id+'\0'+_key(key)))
    def create_challenge(self,owner,job,artifact_id,action):
        base=self._base(owner,job);require(action in {RENDER,VIEW},'Only exact-film rendering or owner viewing may be requested.')
        lineage=self.lineage.load(owner,job,artifact_id) if action==RENDER else self.lineage.load_artifact(owner,job,artifact_id)
        scope=_scope(lineage,self.release if action==RENDER else None);now=self.now();challenge_id=secrets.token_hex(24)
        payload=dict(schema=CHALLENGE_SCHEMA,challenge_id=challenge_id,owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,
            action=action,scope=scope,scope_sha256=fingerprint(scope),issued_at=now,expires_at=now+self.challenge_ttl)
        digest=fingerprint(payload);ref=self._doc(base+'/exact_film_challenges/'+challenge_id)
        self._run(lambda tx:tx.create(ref,dict(payload=payload,challenge_sha256=digest,consumed_by=None)))
        return {**payload,'challenge_sha256':digest}
    def approve(self,owner,job,challenge_id,challenge_sha256,idempotency_key,*,expected_action,expected_artifact_id):
        base=self._base(owner,job);request_ref=self._request_ref(owner,job,idempotency_key)
        require(isinstance(challenge_id,str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{8,128}',challenge_id)) and isinstance(challenge_sha256,str) and bool(re.fullmatch(r'[0-9a-f]{64}',challenge_sha256)),'Invalid opaque challenge handle.')
        challenge_ref=self._doc(base+'/exact_film_challenges/'+challenge_id);pre=self._read(challenge_ref)
        require(isinstance(pre,dict) and pre.get('challenge_sha256')==challenge_sha256 and fingerprint(pre.get('payload'))==challenge_sha256,'Exact one-time challenge is missing or changed.')
        payload=pre['payload'];action=payload['action'];artifact_id=payload['scope']['artifact_id']
        require(action==expected_action and artifact_id==expected_artifact_id and expected_action in {RENDER,VIEW},'Owner-action route cannot consume a challenge for another action or artifact.')
        require((payload['owner_id'],payload['job_id'],payload['snapshot_sha256'])==(job.owner_id,job.id,job.snapshot_sha256),'Challenge belongs to another owner/job.')
        lineage=self.lineage.load(owner,job,artifact_id) if action==RENDER else self.lineage.load_artifact(owner,job,artifact_id)
        scope=_scope(lineage,self.release if action==RENDER else None);require(scope==payload['scope'],'Retained media changed after owner challenge.')
        now=self.now();request_sha=fingerprint(dict(challenge_id=challenge_id,challenge_sha256=challenge_sha256,scope_sha256=fingerprint(scope),action=action))
        # One render approval per exact scope. View leases are explicitly renewable.
        grant_id=('film_' if action==RENDER else 'view_')+_hash(job.id+'\0'+(fingerprint(scope) if action==RENDER else idempotency_key))
        collection='exact_film_approvals' if action==RENDER else 'owner_view_leases'
        grant_ref=self._doc(base+'/'+collection+'/'+grant_id)
        latest_ref=self._doc(base+'/exact_film_latest/'+job.id+('_render' if action==RENDER else '_view_'+_hash(artifact_id)))
        def transaction(tx):
            existing_request=self._read(request_ref,tx);challenge=self._read(challenge_ref,tx);existing_grant=self._read(grant_ref,tx)
            current_latest=self._read(latest_ref,tx)
            require(challenge==pre,'Challenge changed during owner approval.')
            if existing_request is not None:
                require(existing_request['request_sha256']==request_sha and existing_request['grant_id']==grant_id and existing_grant is not None,'Idempotency key changed exact owner action.')
                return dict(record=existing_grant,created=False)
            require(payload['issued_at']<=now<payload['expires_at'] and challenge['consumed_by'] is None,'Challenge expired or was consumed by another request.')
            if existing_grant is not None:
                require(action==RENDER and existing_grant['scope']==scope,'Existing owner grant differs from exact film.')
                record=existing_grant;created=False
            else:
                record=dict(schema=APPROVAL_SCHEMA if action==RENDER else VIEW_SCHEMA,action=action,owner_id=job.owner_id,actor_subject=owner.subject,
                    job_id=job.id,snapshot_sha256=job.snapshot_sha256,scope=scope,scope_sha256=fingerprint(scope),challenge_id=challenge_id,
                    issued_at=now,expires_at=now+(self.render_ttl if action==RENDER else self.view_ttl),owner_only=True,
                    paid_generation_allowed=False,publication_allowed=False,future_style_approved=False)
                record['approval_id' if action==RENDER else 'lease_id']=grant_id
                tx.create(grant_ref,record);created=True
            tx.create(request_ref,dict(request_sha256=request_sha,grant_id=grant_id,collection=collection,owner_id=job.owner_id,job_id=job.id))
            tx.update(challenge_ref,dict(consumed_by=_hash(idempotency_key)))
            pointer=dict(grant_id=grant_id,collection=collection,owner_id=job.owner_id,job_id=job.id)
            (tx.create if current_latest is None else tx.update)(latest_ref,pointer)
            return dict(record=record,created=created)
        return self._run(transaction)
    def _read_grant(self,owner,job,grant_id,action,require_current=True):
        base=self._base(owner,job);require(isinstance(grant_id,str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{8,128}',grant_id)),'Invalid owner-action reference.')
        collection='exact_film_approvals' if action==RENDER else 'owner_view_leases'
        record=self._read(self._doc(base+'/'+collection+'/'+grant_id));_validate_record(record,job=job,action=action,now=self.now(),require_current=require_current)
        lineage=self.lineage.load(owner,job,record['scope']['preview_artifact_id']) if action==RENDER else self.lineage.load_artifact(owner,job,record['scope']['artifact_id'])
        require(record['scope']==_scope(lineage,record['scope']['final_release_sha256'] if action==RENDER else None),'Owner grant no longer matches exact retained artifact.')
        if action==RENDER:require(record['scope']['final_release_sha256']==self.release,'Final rendering release differs from approved version.')
        return copy.deepcopy(record)
    def read_approval(self,owner,job,approval_id,*,require_current=True):return self._read_grant(owner,job,approval_id,RENDER,require_current)
    def read_view_lease(self,owner,job,lease_id):return self._read_grant(owner,job,lease_id,VIEW,True)
    def _latest(self,owner,job,action,artifact_id=None,require_current=True):
        base=self._base(owner,job);suffix='_render' if action==RENDER else '_view_'+_hash(artifact_id)
        row=self._read(self._doc(base+'/exact_film_latest/'+job.id+suffix))
        if row is None:return None
        return self._read_grant(owner,job,row['grant_id'],action,require_current)
    def latest_approval(self,owner,job):return self._latest(owner,job,RENDER,require_current=False)
    def latest_view_lease(self,owner,job,artifact_id):return self._latest(owner,job,VIEW,artifact_id,True)
    def get_approval(self,job):return self._latest(Principal(job.owner_id),job,RENDER,require_current=True)
    def find_request(self,owner,job,key):
        row=self._read(self._request_ref(owner,job,key))
        if row is None:return None
        record=self._read(self._doc(self._base(owner,job)+'/'+row['collection']+'/'+row['grant_id']))
        _validate_record(record,job=job,action=record['action'],now=self.now(),require_current=False)
        return dict(record=copy.deepcopy(record),created=False)
    def authorize_view(self,owner,job,artifact_id,artifact_sha256,proof_sha256):
        lease=self.latest_view_lease(owner,job,artifact_id)
        require(lease is not None and (lease['scope']['artifact_id'],lease['scope']['artifact_sha256'],lease['scope']['artifact_proof_sha256'])==(artifact_id,artifact_sha256,proof_sha256),'No current owner-only lease for this exact artifact.')
        return True
    def approval_lineage(self,owner,job,approval_id):
        approval=self.read_approval(owner,job,approval_id)
        lineage=self.lineage.load(owner,job,approval['scope']['preview_artifact_id'])
        return {**lineage,'approval':approval,'approval_sha256':fingerprint(approval)}


def validate_exact_film_lineage(job,lineage,expected_final_release_sha256,*,now=None):
    """Validate independently loaded immutable lineage + CURRENT owner render scope.

    The returned historical timestamp is for read-only validation ONLY. No paid
    transport or generation authorization may consume it.
    """
    now=time.time() if now is None else now
    from .exact_film_runtime import require_job_contract
    contract=require_job_contract(job)
    require(contract['final_release_sha256']==expected_final_release_sha256,'Owner action differs from independently frozen final renderer.')
    approval=lineage['approval'];_validate_record(approval,job=job,action=RENDER,now=now)
    require(fingerprint(approval)==lineage['approval_sha256'] and approval['scope']==_scope(lineage,expected_final_release_sha256),
            'Exact-film approval changed retained media, context or renderer.')
    commit=lineage['generation_commit'];require(commit['timestamp_source']=='firestore_document_create_time'
        and commit['created_at_epoch']==lineage['preview_committed_at'] and commit['proof_sha256']==fingerprint(lineage['preview_proof'])
        and fingerprint(commit)==approval['scope']['generation_commit_sha256'],'Authoritative historical preview commit proof changed.')
    require(lineage['manifest_sha256']==lineage['retained_manifest']['sha256'] and fingerprint(lineage['inputs']['preview_context'])==lineage['context_sha256'],
            'Retained render manifest or source context changed.')
    auth=lineage['authorization'];validate_authorization(auth,lineage['preview_proof']['authorization_sha256'],job=job,budget=auth['budget'],
        preview_release_sha256=lineage['preview_proof']['preview_release_sha256'],now=commit['created_at_epoch'])
    return commit['created_at_epoch']


class ReadOnlyNativeReplay:
    """Retained source/QC archive verification with NO provider transport object.

    Missing or unknown receipts fail closed. This class cannot call planning_once,
    submission factories, generation authorization, credentials or provider HTTP.
    """
    def __init__(self,*,journal,source_response_store,review_response_store,original_reader,trusted_budget):
        self.journal=journal;self.source_store=source_response_store;self.review_store=review_response_store
        self.original_reader=original_reader;self.budget=copy.deepcopy(trusted_budget)
    def review(self,result):
        from .private_preview import verify_preview_review
        return verify_preview_review(result,journal=self.journal,trusted_budget=self.budget,response_store=self.review_store)
    def source(self,candidate,*,job,narration_report):
        from . import source_planning as source
        from .automated_review import validate_budget_context
        from .vertex_review import encoded,parse_vertex_response
        budget_sha=validate_budget_context(self.journal,self.budget)
        require(self.journal.owner_id==job.owner_id and self.journal.job_id==job.id,'Source replay journal belongs to another owner/job.')
        operation=source.source_operation(job.owner_id,job.id);entry=self.journal.get(operation)
        require(entry is not None and entry['state']=='received','Retained source planning is unresolved; no paid fallback is allowed.')
        receipt=entry['receipt'];snapshot=self.source_store.read_response(receipt['request_archive']);request=json.loads(snapshot.payload)
        require(snapshot.request_id=='source_input_'+hashlib.sha256(snapshot.payload).hexdigest() and snapshot.payload==encoded(request),
                'Original native source request archive changed.')
        originals={}
        for image in job.snapshot['images']:
            value=self.original_reader(job,image)
            if isinstance(value,bytes):value=dict(bytes=value,content_type=image['content_type'])
            require(isinstance(value,dict) and isinstance(value.get('bytes'),bytes)
                    and hashlib.sha256(value['bytes']).hexdigest()==image['sha256'],'Retained original bytes changed.')
            originals[image['id']]=value
        prepared=source.prepare_source_request(job=job,originals=originals,narration_report=narration_report,config=request['config'])
        expected=dict(native=prepared['native'],binding=prepared['binding'],evidence_sha256=fingerprint(prepared['evidence']),config=prepared['config'])
        require(request==expected,'Read-only source replay differs from original exact text/images/config/native request.')
        wire=self.source_store.read_response(receipt)
        require(wire.request_id==receipt['request_id'] and hashlib.sha256(wire.payload).hexdigest()==receipt['response_sha256']
                and len(wire.payload)==receipt['response_bytes'],'Retained native source response changed.')
        text,metadata=parse_vertex_response(wire.payload,max_input_tokens=prepared['config']['max_input_tokens'],max_output_tokens=prepared['config']['max_output_tokens'])
        base=source.parse_proposals(text,evidence=prepared['evidence'])
        actual={**base,'operation_id':operation,'reservation_cents':entry['reservation_cents'],'budget_sha256':budget_sha,
                'native_provenance':metadata,'automatic_retries':0,'candidate_sha256':fingerprint(base)}
        require(actual==candidate,'Retained source proposal differs from independently reread native archive.')
        source.validate_candidate_result(actual,job=job,narration_report=narration_report)
        return True


class ExactFilmNativeReplay:
    """Concrete worker archive readers, deliberately no coordinator/transport."""
    def __init__(self,runtime,job,lineage):
        from .cloud_journal import FirestoreSpendJournal
        from .cloud_storage import CloudProviderResponseStore
        from .funded_review import CloudOriginalReviewReader
        from .vertex_review import FirestoreVertexAuditRecords,VertexNativeEvidence,VertexVerifiedReviewResponseStore
        config=runtime.config;bindings=runtime.bindings;budget=config['budget'];target=config['target']
        require(lineage['preview_proof']['owner_id']==job.owner_id and lineage['preview_proof']['job_id']==job.id,'Exact replay belongs to another retained job.')
        auth_budget=lineage['authorization']['budget']
        require(all(auth_budget[k]==budget[k] for k in ('scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents')),'Retained native ledger differs from independently configured scope/caps.')
        common=dict(project_id=target['project_id'],namespace=config['firestore']['namespace'],transaction_runner=bindings.transaction_runner)
        journal=FirestoreSpendJournal(bindings.firestore_client,**common,owner_id=job.owner_id,job_id=job.id,budget_scope=budget['scope'],approved_total_cap_cents=budget['approved_total_cap_cents'],prior_spend_cents=budget['prior_spend_cents'])
        archive=CloudProviderResponseStore(bindings.storage_client,bucket=target['bucket'],owner=Principal(job.owner_id),job_id=job.id)
        records=FirestoreVertexAuditRecords(bindings.firestore_client,**common,owner_id=job.owner_id,job_id=job.id)
        native=VertexNativeEvidence(archive=archive,records=records,project=target['project_id'])
        verified=VertexVerifiedReviewResponseStore(archive,native)
        trusted=dict(scope_sha256=_hash(budget['scope']),approved_total_cap_cents=budget['approved_total_cap_cents'],prior_spend_cents=budget['prior_spend_cents'],job_cap_cents=budget['job_cap_cents'])
        self.reader=ReadOnlyNativeReplay(journal=journal,source_response_store=archive,review_response_store=verified,
            original_reader=CloudOriginalReviewReader(bindings.storage_client,bucket=target['bucket']),trusted_budget=trusted)
        self.job=job;self.report=copy.deepcopy(lineage['inputs']['report'])
    def verify_source(self,candidate):return self.reader.source(candidate,job=self.job,narration_report=self.report)
    def verify_review(self,result):return self.reader.review(result)
