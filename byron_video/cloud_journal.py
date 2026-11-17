"""Firestore transactional persistence adapters; no default client or activation.

Construct with an already-authenticated client and explicit private deployment
configuration. Importing this module never creates credentials or makes calls.
Transaction callbacks contain database reads/writes ONLY, never provider requests.
"""
from dataclasses import asdict, replace
import hashlib
import json
import re
import secrets

from .hosted_control import (JobRecord, DispatchReceipt, VerifiedArtifact,
    IdempotencyConflict, canonical, digest, require, SAFE_ID, JOB_ID, SHA256)

MAX_ALLOCATIONS = 480


def _safe_owner(owner):
    value = owner.subject if hasattr(owner, 'subject') else owner
    require(isinstance(value, str) and bool(SAFE_ID.fullmatch(value)), 'Invalid owner.')
    return value


def _key(value):
    require(isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{16,128}', value)), 'Invalid submission reference.')
    return value


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class _FirestoreBoundary:
    def __init__(self, client, *, project_id, namespace, transaction_runner=None):
        require(isinstance(project_id, str) and bool(re.fullmatch(r'[a-z][a-z0-9-]{4,61}[a-z0-9]', project_id)), 'Explicit project required.')
        require(isinstance(namespace, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', namespace)), 'Explicit private namespace required.')
        require(getattr(client, 'project', None) == project_id, 'Firestore client project differs from private configuration.')
        self.client = client
        self.root = 'byron_control/' + namespace
        self._runner = transaction_runner

    def _doc(self, path):
        return self.client.document(self.root + '/' + path)

    def _owner(self, owner):
        return 'owners/' + _hash(_safe_owner(owner))

    @staticmethod
    def _read(ref, transaction=None):
        snap = ref.get(transaction=transaction)
        return snap.to_dict() if snap.exists else None

    def _run(self, callback):
        # Test runner injection must emulate atomic read-before-write transactions.
        if self._runner is not None:
            return self._runner(callback)
        try:
            from google.cloud.firestore_v1 import transactional
        except ImportError:
            raise RuntimeError('Google Cloud Firestore runtime is unavailable; no transaction was attempted.') from None
        # An uncertain commit is surfaced, never converted into an external retry.
        return transactional(callback)(self.client.transaction(max_attempts=1))


class FirestoreJobStore(_FirestoreBoundary):
    """DurableJobStore implemented using atomic document/index transactions.

    A transactional request index prevents duplicate jobs. A global execution
    index prevents the same worker execution attaching to two jobs or owners.
    Snapshots are canonical strings and integrity checked on every loaded record.
    """
    def _job_ref(self, owner, job_id):
        require(isinstance(job_id, str) and bool(JOB_ID.fullmatch(job_id)), 'Invalid job ID.')
        return self._doc(self._owner(owner) + '/jobs/' + job_id)

    def _request_ref(self, owner, key):
        return self._doc(self._owner(owner) + '/requests/' + _hash(_key(key)))

    @staticmethod
    def _record(data, owner, job_id=None):
        if data is None:
            return None
        require(isinstance(data, dict) and set(data) == set(JobRecord.__dataclass_fields__), 'Invalid stored job.')
        job = JobRecord(**data)
        require(job.owner_id == _safe_owner(owner) and bool(JOB_ID.fullmatch(job.id)), 'Job ownership mismatch.')
        require(job_id is None or job.id == job_id, 'Job identity mismatch.')
        require(bool(SHA256.fullmatch(job.request_sha256)) and job.state in {'HOLD','queued','running','complete','draft_preview_ready'})
        _key(job.idempotency_key)
        job.snapshot
        if job.receipt is not None:
            job.receipt.validate(job)
        require(job.receipt is not None or job.state == 'HOLD', 'Accepted state requires a dispatch receipt.')
        if job.artifact is not None:
            job.artifact.validate(job)
        require((job.state in {'complete','draft_preview_ready'}) == (job.artifact is not None), 'Stored state and verified artifact disagree.')
        require((job.state=='draft_preview_ready') == (getattr(job.artifact,'artifact_kind',None)=='PRIVATE_DRAFT_PREVIEW'), 'Draft state and proof disagree.')
        return job

    def get(self, owner_id, job_id):
        return self._record(self._read(self._job_ref(owner_id,job_id)),owner_id,job_id)

    def _find(self, owner, key, transaction=None):
        index = self._read(self._request_ref(owner,key),transaction)
        if index is None:
            return None
        require(set(index) == {'job_id','request_sha256','owner_id','key_sha256'})
        require(index['owner_id'] == _safe_owner(owner) and index['key_sha256'] == _hash(key))
        job = self._record(self._read(self._job_ref(owner,index['job_id']),transaction),owner,index['job_id'])
        require(job is not None and job.idempotency_key == key and job.request_sha256 == index['request_sha256'], 'Submission index integrity failed.')
        return job

    def find_submission(self, owner_id, key):
        # Read together to avoid returning an index/job pair from different commits.
        return self._run(lambda transaction: self._find(owner_id,key,transaction))

    def find_request(self, owner_id, key, request_sha256):
        job = self.find_submission(owner_id,key)
        if job is not None and job.request_sha256 != request_sha256:
            raise IdempotencyConflict('This submission key already belongs to different input.')
        return job

    def reserve(self, owner_id, key, request_sha256, snapshot):
        owner_id, key = _safe_owner(owner_id), _key(key)
        require(isinstance(request_sha256,str) and bool(SHA256.fullmatch(request_sha256)))
        encoded = canonical(snapshot)
        require(len(encoded.encode()) < 700_000, 'Snapshot exceeds private document bound.')
        candidate = JobRecord(secrets.token_hex(16),owner_id,key,request_sha256,encoded,digest(snapshot))
        job_ref, request_ref = self._job_ref(owner_id,candidate.id), self._request_ref(owner_id,key)
        def transaction(tx):
            existing = self._find(owner_id,key,tx)
            if existing is not None:
                if existing.request_sha256 != request_sha256:
                    raise IdempotencyConflict('This submission key already belongs to different input.')
                return existing, False
            tx.create(job_ref,asdict(candidate))
            tx.create(request_ref,dict(job_id=candidate.id,request_sha256=request_sha256,owner_id=owner_id,key_sha256=_hash(key)))
            return candidate, True
        return self._run(transaction)

    def record_dispatch(self, owner_id, job_id, receipt):
        require(isinstance(receipt,DispatchReceipt))
        ref = self._job_ref(owner_id,job_id)
        execution_ref = self._doc('executions/' + _hash(receipt.execution_id))
        def transaction(tx):
            job = self._record(self._read(ref,tx),owner_id,job_id)
            require(job is not None)
            receipt.validate(job)
            execution=self._read(execution_ref,tx)
            expected=dict(owner_id=job.owner_id,job_id=job.id,execution_id=receipt.execution_id)
            if job.receipt is not None:
                require(job.receipt==receipt and execution==expected,'A different backend execution is already attached to this job.')
                return job  # HTTP and worker may reconcile the exact same accepted execution concurrently.
            require(job.state == 'HOLD' and execution is None, 'Backend execution is already attached to a job.')
            updated = replace(job,state='queued',receipt_json=canonical(asdict(receipt)))
            tx.create(execution_ref,dict(owner_id=job.owner_id,job_id=job.id,execution_id=receipt.execution_id))
            tx.update(ref,dict(state=updated.state,receipt_json=updated.receipt_json))
            return updated
        return self._run(transaction)

    def record_worker_hold(self, owner_id, job_id, execution_id, reason):
        """Truthful worker HOLD preserves its immutable execution; never redispatch."""
        require(reason in {'provider_outcome_unknown','queue_deadline','source_review','prompt_review',
            'motion_capture','narration_outcome_unknown','generation_validation','output_review'},'Bounded worker HOLD reason required.')
        ref=self._job_ref(owner_id,job_id)
        hold_ref=self._doc(self._owner(owner_id)+'/worker_holds/'+job_id)
        def hold(tx):
            job=self._record(self._read(ref,tx),owner_id,job_id)
            require(job is not None and job.state in {'queued','running','HOLD'} and job.artifact is None
                    and job.receipt is not None and job.receipt.execution_id==execution_id,'HOLD must preserve the accepted worker identity.')
            prior=self._read(hold_ref,tx)
            value=dict(owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,execution_id=execution_id,reason=reason)
            if prior is not None:
                require(all(prior.get(k)==value[k] for k in ('owner_id','job_id','snapshot_sha256')),'Worker HOLD identity changed.')
            (tx.create if prior is None else tx.update)(hold_ref,value)
            tx.update(ref,{'state':'HOLD'})
            return replace(job,state='HOLD')
        return self._run(hold)

    def record_started(self, owner_id, job_id, execution_id):
        ref = self._job_ref(owner_id,job_id)
        def transaction(tx):
            job = self._record(self._read(ref,tx),owner_id,job_id)
            require(job is not None and job.state in {'queued','HOLD'} and job.receipt is not None and job.receipt.execution_id == execution_id, 'Task is not queued for this execution.')
            tx.update(ref,dict(state='running'))
            return replace(job,state='running')
        return self._run(transaction)

    def record_verified_completion(self, owner_id, job_id, proof):
        require(type(proof) is VerifiedArtifact)
        ref = self._job_ref(owner_id,job_id)
        def transaction(tx):
            job = self._record(self._read(ref,tx),owner_id,job_id)
            require(job is not None and job.state in {'queued','running'})
            proof.validate(job)
            encoded = canonical(asdict(proof))
            tx.update(ref,dict(state='complete',artifact_json=encoded))
            return replace(job,state='complete',artifact_json=encoded)
        return self._run(transaction)


class FirestoreSpendJournal(_FirestoreBoundary):
    """Per-owner/job fixed-budget journal with durable unknown-outcome holds.

    Implements worker_journal.Journal's arm/record/get protocol. Reservation is
    committed BEFORE a provider call. No reservation is refunded automatically;
    a timeout leaves may_have_submitted and duplicate arm always rejects.
    """
    def __init__(self, client, *, project_id, namespace, owner_id, job_id, budget_scope, approved_total_cap_cents, prior_spend_cents, transaction_runner=None):
        super().__init__(client,project_id=project_id,namespace=namespace,transaction_runner=transaction_runner)
        require(isinstance(job_id,str) and bool(JOB_ID.fullmatch(job_id)), 'Explicit frozen job required.')
        self.owner_id = _safe_owner(owner_id)
        self.job_id = job_id
        self.path = self._owner(owner_id) + '/spend_jobs/' + job_id
        require(isinstance(budget_scope,str) and bool(SAFE_ID.fullmatch(budget_scope)), 'Explicit shared budget scope required.')
        require(type(approved_total_cap_cents) is int and type(prior_spend_cents) is int
                and 0 <= prior_spend_cents < approved_total_cap_cents <= 1_000_000_000,
                'Explicit total cap and already-spent opening balance required.')
        self.scope_hash = _hash(budget_scope)
        self.total_cap = approved_total_cap_cents
        self.prior_spend = prior_spend_cents
        self.shared_ref = self._doc('spend_scopes/' + self.scope_hash)

    def _operation_ref(self, operation_id):
        require(isinstance(operation_id,str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{8,100}',operation_id)), 'Invalid operation ID.')
        return self._doc(self.path + '/operations/' + operation_id)

    def arm(self, operation_id, request, reservation_cents, job_cap_cents):
        require(type(reservation_cents) is int and type(job_cap_cents) is int
                and 0 < reservation_cents <= job_cap_cents <= 1_000_000_000,
                'Explicit positive bounded reservation required.')
        request_hash = digest(request)
        operation_ref = self._operation_ref(operation_id)
        budget_ref = self._doc(self.path)
        plan_ref = self._doc(self.path + '/request_hashes/' + request_hash)
        def transaction(tx):
            budget = self._read(budget_ref,tx)
            existing = self._read(operation_ref,tx)
            prior_plan = self._read(plan_ref,tx)
            shared = self._read(self.shared_ref,tx)
            if shared is None:
                shared_used = self.prior_spend
            else:
                require(set(shared) == {'scope_sha256','cap_cents','prior_spend_cents','reserved_cents'}
                        and shared['scope_sha256'] == self.scope_hash and shared['cap_cents'] == self.total_cap
                        and shared['prior_spend_cents'] == self.prior_spend
                        and type(shared['reserved_cents']) is int
                        and self.prior_spend <= shared['reserved_cents'] <= self.total_cap,
                        'Shared budget configuration or integrity changed.')
                shared_used = shared['reserved_cents']
            require(shared_used + reservation_cents <= self.total_cap, 'Shared reservation would exceed its fixed total budget.')
            require(existing is None, 'Operation already exists; reconcile it, never resend.')
            require(prior_plan is None, 'This exact request is already reserved; do not submit under a new operation ID.')
            if budget is None:
                used = 0
            else:
                require(set(budget) == {'owner_id','job_id','cap_cents','reserved_cents'}
                        and budget['owner_id'] == self.owner_id and budget['job_id'] == self.job_id
                        and type(budget['reserved_cents']) is int and 0 <= budget['reserved_cents'] <= budget['cap_cents'], 'Stored budget failed integrity checks.')
                require(budget['cap_cents'] == job_cap_cents, 'The job budget cannot change after its first reservation.')
                used = budget['reserved_cents']
            require(used + reservation_cents <= job_cap_cents, 'Job reservation would exceed its fixed budget.')
            record = dict(plan_sha256=request_hash,reservation_cents=reservation_cents,state='may_have_submitted',receipt=None)
            updated = dict(owner_id=self.owner_id,job_id=self.job_id,cap_cents=job_cap_cents,reserved_cents=used+reservation_cents)
            if budget is None:
                tx.create(budget_ref,updated)
            else:
                tx.update(budget_ref,updated)
            shared_updated = dict(scope_sha256=self.scope_hash,cap_cents=self.total_cap,
                                  prior_spend_cents=self.prior_spend,reserved_cents=shared_used+reservation_cents)
            if shared is None:
                tx.create(self.shared_ref,shared_updated)
            else:
                tx.update(self.shared_ref,shared_updated)
            tx.create(operation_ref,record)
            tx.create(plan_ref,dict(operation_id=operation_id))
            return request_hash
        return self._run(transaction)

    def reserve_plan(self, plan_id, plan_sha256, allocations, job_cap_cents):
        """Atomically fund ALL remaining motion work, including existing job spend.

        Allocations are non-spend holds, not provider submissions. Claiming one
        later moves its funds to an operation without incrementing either budget.
        No automatic refund or replacement-plan release exists.
        """
        require(isinstance(plan_id,str) and bool(SAFE_ID.fullmatch(plan_id))
                and isinstance(plan_sha256,str) and bool(SHA256.fullmatch(plan_sha256)), 'Invalid plan identity.')
        # Sentence-paced narration makes one operation per sentence: a 16,000-character story can need a
        # few hundred. 480 keeps the whole plan (one document each, plus 3) inside one 500-write transaction.
        require(isinstance(allocations,dict) and 0 < len(allocations) <= MAX_ALLOCATIONS
                and all(isinstance(k,str) and re.fullmatch(r'[A-Za-z0-9_-]{8,100}',k)
                        and type(v) is int and v>0 for k,v in allocations.items()), 'Explicit finite positive allocations required.')
        total=sum(allocations.values())
        require(type(job_cap_cents) is int and 0<total<=job_cap_cents<=1_000_000_000, 'Plan exceeds its fixed film cap.')
        encoded=canonical(allocations)
        plan_ref=self._doc(self.path+'/reserved_plans/'+plan_id)
        budget_ref=self._doc(self.path)
        refs={op:self._doc(self.path+'/plan_allocations/'+op) for op in allocations}
        def transaction(tx):
            existing=self._read(plan_ref,tx)
            budget=self._read(budget_ref,tx);shared=self._read(self.shared_ref,tx)
            expected=dict(owner_id=self.owner_id,job_id=self.job_id,plan_sha256=plan_sha256,
                          allocations_json=encoded,job_cap_cents=job_cap_cents,total_cents=total)
            if existing is not None:
                require(existing==expected,'Reserved continuation plan changed; never silently release or reset it.')
                require(budget is not None and budget['cap_cents']==job_cap_cents,'Reserved job budget changed.')
                require(shared is not None and shared['scope_sha256']==self.scope_hash and shared['cap_cents']==self.total_cap
                        and shared['prior_spend_cents']==self.prior_spend,'Reserved shared budget changed.')
                return False
            existing_allocations={op:self._read(ref,tx) for op,ref in refs.items()}
            existing_operations={op:self._read(self._operation_ref(op),tx) for op in allocations}
            require(all(v is None for v in existing_allocations.values()) and all(v is None for v in existing_operations.values()),
                    'Plan operation was already reserved or submitted; reconcile rather than refund/reallocate.')
            used=0 if budget is None else budget.get('reserved_cents')
            require(type(used) is int and 0<=used<=job_cap_cents and (budget is None or
                set(budget)=={'owner_id','job_id','cap_cents','reserved_cents'} and budget['owner_id']==self.owner_id
                and budget['job_id']==self.job_id and budget['cap_cents']==job_cap_cents),'Existing film budget failed integrity checks.')
            shared_used=self.prior_spend if shared is None else shared.get('reserved_cents')
            require(type(shared_used) is int and self.prior_spend<=shared_used<=self.total_cap and (shared is None or
                set(shared)=={'scope_sha256','cap_cents','prior_spend_cents','reserved_cents'}
                and shared['scope_sha256']==self.scope_hash and shared['cap_cents']==self.total_cap
                and shared['prior_spend_cents']==self.prior_spend),'Existing shared budget failed integrity checks.')
            require(used+total<=job_cap_cents and shared_used+total<=self.total_cap,
                    'Complete motion plan cannot be funded after existing narration/job/campaign reservations; no segment was armed.')
            next_budget=dict(owner_id=self.owner_id,job_id=self.job_id,cap_cents=job_cap_cents,reserved_cents=used+total)
            next_shared=dict(scope_sha256=self.scope_hash,cap_cents=self.total_cap,prior_spend_cents=self.prior_spend,reserved_cents=shared_used+total)
            (tx.create if budget is None else tx.update)(budget_ref,next_budget)
            (tx.create if shared is None else tx.update)(self.shared_ref,next_shared)
            tx.create(plan_ref,expected)
            for op,amount in allocations.items():
                tx.create(refs[op],dict(plan_id=plan_id,plan_sha256=plan_sha256,reservation_cents=amount,state='reserved'))
            return True
        return self._run(transaction)

    def get_plan_reservation(self, plan_id, plan_sha256):
        require(isinstance(plan_id,str) and bool(SAFE_ID.fullmatch(plan_id))
                and isinstance(plan_sha256,str) and bool(SHA256.fullmatch(plan_sha256)), 'Invalid plan identity.')
        result=self._read(self._doc(self.path+'/reserved_plans/'+plan_id))
        if result is not None:
            require(result.get('owner_id')==self.owner_id and result.get('job_id')==self.job_id
                    and result.get('plan_sha256')==plan_sha256,'Reserved plan identity changed.')
        return result

    def get_reserved(self, operation_id, plan_id, plan_sha256):
        self._operation_ref(operation_id)
        require(isinstance(plan_id,str) and bool(SAFE_ID.fullmatch(plan_id)) and isinstance(plan_sha256,str) and bool(SHA256.fullmatch(plan_sha256)), 'Invalid plan identity.')
        allocation=self._read(self._doc(self.path+'/plan_allocations/'+operation_id))
        require(isinstance(allocation,dict) and set(allocation)=={'plan_id','plan_sha256','reservation_cents','state'}
                and allocation['plan_id']==plan_id and allocation['plan_sha256']==plan_sha256
                and type(allocation['reservation_cents']) is int and allocation['reservation_cents']>0
                and allocation['state'] in {'reserved','claimed'},'Operation is not allocated to this frozen plan.')
        if allocation['state']=='reserved':
            return dict(plan_sha256=plan_sha256,reservation_cents=allocation['reservation_cents'],state='reserved',receipt=None)
        operation=self.get(operation_id)
        require(operation is not None and operation['reservation_cents']==allocation['reservation_cents'], 'Claimed operation is missing or changed.')
        return operation

    def claim_reserved(self, operation_id, request, reservation_cents, job_cap_cents, plan_id, plan_sha256):
        """Single transactional claim; no additional spend reservation or retries."""
        require(isinstance(plan_id,str) and bool(SAFE_ID.fullmatch(plan_id)) and isinstance(plan_sha256,str) and bool(SHA256.fullmatch(plan_sha256)), 'Invalid plan identity.')
        require(type(reservation_cents) is int and reservation_cents>0 and type(job_cap_cents) is int, 'Invalid reserved claim.')
        operation_ref=self._operation_ref(operation_id)
        allocation_ref=self._doc(self.path+'/plan_allocations/'+operation_id)
        plan_ref=self._doc(self.path+'/reserved_plans/'+plan_id)
        request_hash=digest(request);hash_ref=self._doc(self.path+'/request_hashes/'+request_hash)
        def transaction(tx):
            plan=self._read(plan_ref,tx);allocation=self._read(allocation_ref,tx)
            operation=self._read(operation_ref,tx);prior_request=self._read(hash_ref,tx)
            require(plan is not None and plan['owner_id']==self.owner_id and plan['job_id']==self.job_id
                    and plan['plan_sha256']==plan_sha256 and plan['job_cap_cents']==job_cap_cents
                    and json.loads(plan['allocations_json']).get(operation_id)==reservation_cents,'Claim differs from funded plan.')
            require(allocation==dict(plan_id=plan_id,plan_sha256=plan_sha256,reservation_cents=reservation_cents,state='reserved')
                    and operation is None and prior_request is None,'Operation already claimed; reconcile unknown submission instead of retrying.')
            tx.update(allocation_ref,dict(state='claimed'))
            tx.create(operation_ref,dict(plan_sha256=request_hash,reservation_cents=reservation_cents,state='may_have_submitted',receipt=None))
            tx.create(hash_ref,dict(operation_id=operation_id))
            return request_hash
        return self._run(transaction)

    def record(self, operation_id, request, receipt):
        require(isinstance(receipt,dict) and isinstance(receipt.get('request_id'),str)
                and bool(re.fullmatch(r'[A-Za-z0-9_-]{8,160}',receipt['request_id'])), 'Verified provider request ID required.')
        # This journal is private, but credentials still have no place in receipts.
        def validate(item):
            if isinstance(item,dict):
                require(all(isinstance(k,str) and k.lower() not in {'api_key','apikey','secret','password','token','authorization','credentials'} for k in item), 'Credentials must not enter receipts.')
                for child in item.values(): validate(child)
            elif isinstance(item,list):
                for child in item: validate(child)
        validate(receipt)
        encoded = canonical(receipt)
        require(len(encoded.encode()) <= 250_000, 'Provider receipt exceeds journal bound.')
        operation_ref = self._operation_ref(operation_id)
        receipt_ref = self._doc('provider_receipts/' + _hash(receipt['request_id']))
        request_hash = digest(request)
        def transaction(tx):
            operation = self._read(operation_ref,tx)
            claimed = self._read(receipt_ref,tx)
            require(operation is not None and operation['state'] == 'may_have_submitted'
                    and operation['plan_sha256'] == request_hash, 'Operation is absent, changed or already reconciled.')
            require(claimed is None, 'Provider request ID already belongs to an operation.')
            tx.create(receipt_ref,dict(owner_id=self.owner_id,job_id=self.job_id,operation_id=operation_id))
            tx.update(operation_ref,dict(state='received',receipt=encoded))
        self._run(transaction)

    def get(self, operation_id):
        result = self._read(self._operation_ref(operation_id))
        if result is None:
            return None
        require(isinstance(result,dict) and set(result) == {'plan_sha256','reservation_cents','state','receipt'}
                and isinstance(result['plan_sha256'],str) and bool(SHA256.fullmatch(result['plan_sha256']))
                and type(result['reservation_cents']) is int and result['reservation_cents'] > 0
                and result['state'] in {'may_have_submitted','received'}, 'Stored operation failed integrity checks.')
        if result['state'] == 'received':
            require(isinstance(result['receipt'],str), 'Stored receipt must be canonical JSON text.')
            receipt = json.loads(result['receipt'])
            require(isinstance(receipt,dict) and canonical(receipt) == result['receipt']
                    and isinstance(receipt.get('request_id'),str)
                    and bool(re.fullmatch(r'[A-Za-z0-9_-]{8,160}',receipt['request_id'])), 'Stored receipt failed verification.')
            result = {**result,'receipt':receipt}
        else:
            require(result['receipt'] is None, 'Unknown operation cannot claim a receipt.')
        return result


class FirestoreDispatchJournal(_FirestoreBoundary):
    """Reserve Cloud Run submission before POST; operation recovery never resends."""
    def _ref(self, owner_id, job_id):
        require(isinstance(job_id,str) and bool(JOB_ID.fullmatch(job_id)), 'Invalid job ID.')
        return self._doc(self._owner(owner_id) + '/dispatches/' + job_id)

    def reserve_dispatch(self, owner_id, job_id, snapshot_sha256, request_sha256):
        require(all(isinstance(x,str) and bool(SHA256.fullmatch(x)) for x in (snapshot_sha256,request_sha256)))
        ref=self._ref(owner_id,job_id)
        job_ref=self._doc(self._owner(owner_id)+'/jobs/'+job_id)
        def transaction(tx):
            prior=self._read(ref,tx)
            job=FirestoreJobStore._record(self._read(job_ref,tx),owner_id,job_id)
            require(job is not None and job.state=='HOLD' and job.snapshot_sha256==snapshot_sha256, 'Committed matching HOLD job required.')
            if prior is not None:
                require(prior['owner_id']==owner_id and prior['job_id']==job_id
                        and prior['snapshot_sha256']==snapshot_sha256 and prior['request_sha256']==request_sha256,
                        'Dispatch reservation changed.')
                return False
            require(job.receipt is None,'A held accepted worker cannot reserve a new ordinary dispatch.')
            tx.create(ref,dict(owner_id=owner_id,job_id=job_id,snapshot_sha256=snapshot_sha256,
                request_sha256=request_sha256,state='may_have_submitted',operation_name=None))
            return True
        return self._run(transaction)

    def record_dispatch_operation(self, owner_id, job_id, snapshot_sha256, operation):
        require(isinstance(operation,str) and bool(re.fullmatch(r'projects/[a-z][a-z0-9-]+/locations/[a-z0-9-]+/operations/[A-Za-z0-9_-]{8,128}',operation)), 'Verified operation name required.')
        ref=self._ref(owner_id,job_id);claim=self._doc('dispatch_operations/'+_hash(operation))
        def transaction(tx):
            prior=self._read(ref,tx);claimed=self._read(claim,tx)
            require(prior is not None and prior['owner_id']==owner_id and prior['job_id']==job_id
                    and prior['snapshot_sha256']==snapshot_sha256 and prior['state']=='may_have_submitted'
                    and prior['operation_name'] is None, 'Dispatch is absent, changed or already recorded.')
            require(claimed is None, 'Cloud operation already belongs to another dispatch.')
            tx.create(claim,dict(owner_id=owner_id,job_id=job_id))
            tx.update(ref,dict(state='operation_recorded',operation_name=operation))
        self._run(transaction)

    def get_dispatch_operation(self, owner_id, job_id, snapshot_sha256):
        data=self._read(self._ref(owner_id,job_id))
        if data is None:return None
        require(data['owner_id']==owner_id and data['job_id']==job_id and data['snapshot_sha256']==snapshot_sha256, 'Dispatch ownership or snapshot changed.')
        require(data['state'] in {'may_have_submitted','operation_recorded'})
        if data['state']=='may_have_submitted':
            require(data['operation_name'] is None)
            return None
        operation=data['operation_name']
        require(isinstance(operation,str) and bool(re.fullmatch(r'projects/[a-z][a-z0-9-]+/locations/[a-z0-9-]+/operations/[A-Za-z0-9_-]{8,128}',operation)))
        return operation


class FirestoreArtifactRegistry(_FirestoreBoundary):
    """Atomically bind verified private object generation and complete the job.

    Called only AFTER full media/semantic verification and immutable storage
    readback succeed. This records that trusted proof; it does not infer media
    correctness from a file extension or a creator-supplied completion claim.
    """
    def _refs(self, owner, job_id, artifact_id):
        require(isinstance(job_id,str) and bool(JOB_ID.fullmatch(job_id)))
        require(isinstance(artifact_id,str) and bool(SAFE_ID.fullmatch(artifact_id)))
        base=self._owner(owner)
        return self._doc(base+'/jobs/'+job_id), self._doc(base+'/artifacts/'+artifact_id)

    @staticmethod
    def _reference(reference, proof):
        require(isinstance(reference,dict) and set(reference)=={'generation','sha256','byte_count','content_type'}
                and isinstance(reference['generation'],str) and bool(re.fullmatch(r'[1-9][0-9]{0,19}',reference['generation']))
                and reference['sha256']==proof.sha256 and reference['byte_count']==proof.byte_count
                and type(reference['byte_count']) is int and reference['content_type']=='video/mp4', 'Private artifact reference differs from verified bytes.')
        return canonical(reference)

    def commit_verified(self, owner, job, proof, reference):
        require(type(proof) is VerifiedArtifact and job.owner_id==_safe_owner(owner))
        proof.validate(job)
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
                require(prior==data and current.state=='complete' and current.artifact==proof, 'Artifact registry cannot be replaced.')
                return current
            require(current.state in {'queued','running'}, 'Only a dispatched job can complete.')
            tx.create(artifact_ref,data)
            tx.update(job_ref,dict(state='complete',artifact_json=encoded_proof))
            return replace(current,state='complete',artifact_json=encoded_proof)
        return self._run(transaction)

    def _load(self, owner, job, artifact_id):
        require(job.owner_id==_safe_owner(owner) and job.state=='complete')
        _,ref=self._refs(owner,job.id,artifact_id)
        data=self._read(ref)
        require(isinstance(data,dict) and set(data)=={'owner_id','job_id','snapshot_sha256','proof_json','reference_json'}
                and data['owner_id']==job.owner_id and data['job_id']==job.id and data['snapshot_sha256']==job.snapshot_sha256,
                'Private verified artifact is unavailable.')
        raw=json.loads(data['proof_json']);raw['source_order']=tuple(raw['source_order'])
        proof=VerifiedArtifact(**raw);proof.validate(job)
        require(proof==job.artifact and proof.artifact_id==artifact_id, 'Recorded artifact proof changed.')
        reference=json.loads(data['reference_json']);self._reference(reference,proof)
        return proof,reference

    def __call__(self, owner, job, artifact_id):
        return self._load(owner,job,artifact_id)[1]

    def verify(self, owner, job, artifact_id):
        return self._load(owner,job,artifact_id)[0]
