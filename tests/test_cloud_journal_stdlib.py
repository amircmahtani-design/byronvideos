"""Offline Firestore adapter contract tests; no SDK/service or cloud claims."""
import copy
import builtins
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import threading
import unittest
from types import SimpleNamespace
from byron_video.cloud_journal import FirestoreJobStore, FirestoreSpendJournal, FirestoreDispatchJournal, FirestoreArtifactRegistry
from byron_video.hosted_control import DispatchReceipt, VerifiedArtifact, IdempotencyConflict

PROJECT='synthetic-project'
OWNER='fixture_owner'
OTHER='fixture_other'
KEY='fixture_request_000001'


class Ref:
    def __init__(self,client,path): self.client,self.path=client,path
    def get(self,transaction=None):
        data=(transaction.data if transaction else self.client.data).get(self.path)
        if transaction and transaction.writes:
            raise AssertionError('Firestore requires reads before writes')
        return SimpleNamespace(exists=data is not None,to_dict=lambda:copy.deepcopy(data))


class Transaction:
    def __init__(self,data): self.data,self.writes=data,False
    def create(self,ref,data):
        if ref.path in self.data: raise ValueError('Already exists')
        self.data[ref.path]=copy.deepcopy(data);self.writes=True
    def update(self,ref,data):
        if ref.path not in self.data: raise ValueError('Missing document')
        self.data[ref.path].update(copy.deepcopy(data));self.writes=True


class Client:
    project=PROJECT
    def __init__(self): self.data={};self.lock=threading.RLock();self.unknown_commit=False
    def document(self,path): return Ref(self,path)
    def run(self,fn):
        with self.lock:
            tx=Transaction(copy.deepcopy(self.data))
            result=fn(tx)
            self.data=tx.data
            if self.unknown_commit:
                self.unknown_commit=False
                raise TimeoutError('synthetic uncertain commit')
            return result


class CloudJournalChecks(unittest.TestCase):
    def setUp(self):
        self.client=Client()
        self.store=FirestoreJobStore(self.client,project_id=PROJECT,namespace='fixture_namespace',transaction_runner=self.client.run)
        self.snapshot={'input_order':['painting_001','painting_002','painting_003','painting_004'],'narrated_text_sha256':'a'*64}
    def reserve(self,owner=OWNER,key=KEY,sha='b'*64):
        return self.store.reserve(owner,key,sha,self.snapshot)
    def spend(self,owner=OWNER,job='c'*32):
        return FirestoreSpendJournal(self.client,project_id=PROJECT,namespace='fixture_namespace',owner_id=owner,job_id=job,budget_scope='synthetic_shared_scope',approved_total_cap_cents=1000,prior_spend_cents=0,transaction_runner=self.client.run)
    def dispatch(self,job,execution='fixture_execution_001'):
        return self.store.record_dispatch(job.owner_id,job.id,DispatchReceipt(job.id,job.snapshot_sha256,execution))
    def proof(self,job):
        return VerifiedArtifact('fixture_artifact_001',job.id,job.owner_id,job.snapshot_sha256,job.receipt.execution_id,
            'd'*64,1000,30,1080,1920,'h264','aac',24,tuple(self.snapshot['input_order']),'a'*64,'a'*64,True,'ffmpeg-full-decode-v1')

    def test_worker_hold_preserves_receipt_and_cannot_redispatch(self):
        job,_=self.reserve();job=self.dispatch(job)
        held=self.store.record_worker_hold(OWNER,job.id,job.receipt.execution_id,'queue_deadline')
        self.assertEqual(held.state,'HOLD');self.assertEqual(held.receipt,job.receipt)
        self.assertEqual(self.store.get(OWNER,job.id),held)
        self.assertEqual(self.store.record_dispatch(OWNER,job.id,job.receipt),held)
        with self.assertRaises(ValueError):self.store.record_worker_hold(OTHER,job.id,job.receipt.execution_id,'queue_deadline')
        with self.assertRaises(ValueError):self.store.record_worker_hold(OWNER,job.id,'other_execution','queue_deadline')
        with self.assertRaises(ValueError):self.store.record_worker_hold(OWNER,job.id,job.receipt.execution_id,'unbounded private error')
        started=self.store.record_started(OWNER,job.id,job.receipt.execution_id)
        self.assertEqual(started.state,'running');self.assertEqual(started.receipt,job.receipt)

    def test_concurrent_http_worker_receipt_reconciliation_is_idempotent(self):
        job,_=self.reserve();receipt=DispatchReceipt(job.id,job.snapshot_sha256,'fixture_execution_001')
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.store.record_dispatch(OWNER,job.id,receipt),range(16)))
        self.assertTrue(all(result.receipt==receipt and result.state=='queued' for result in results))
        with self.assertRaises(ValueError):self.store.record_dispatch(OWNER,job.id,replace(receipt,execution_id='different_execution'))

    def test_private_config_must_match_client(self):
        with self.assertRaises(ValueError):
            FirestoreJobStore(self.client,project_id='different-project',namespace='fixture')
        with self.assertRaises(ValueError):
            FirestoreJobStore(self.client,project_id=PROJECT,namespace='../public')
        self.assertEqual(self.client.data,{})

    def test_missing_firestore_runtime_fails_before_any_write(self):
        store=FirestoreJobStore(self.client,project_id=PROJECT,namespace='fixture_namespace')
        original=builtins.__import__
        def unavailable(name,*args,**kwargs):
            if name.startswith('google'):raise ImportError('synthetic missing SDK')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=unavailable),self.assertRaisesRegex(RuntimeError,'runtime is unavailable'):
            store.reserve(OWNER,KEY,'b'*64,self.snapshot)
        self.assertEqual(self.client.data,{})

    def test_atomic_reservation_replay_conflict_and_owner_scoping(self):
        job,created=self.reserve();self.assertTrue(created);self.assertEqual(job.state,'HOLD')
        same,created=self.reserve();self.assertFalse(created);self.assertEqual(job,same)
        with self.assertRaises(IdempotencyConflict): self.reserve(sha='c'*64)
        other,created=self.reserve(owner=OTHER);self.assertTrue(created);self.assertNotEqual(other.id,job.id)
        self.assertIsNone(self.store.get(OTHER,job.id))
        self.assertEqual(self.store.find_submission(OWNER,KEY),job)
        self.assertEqual(self.store.find_request(OWNER,KEY,'b'*64),job)

    def test_concurrent_reserve_has_one_creator(self):
        with ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(lambda _:self.reserve(),range(20)))
        self.assertEqual(sum(created for job,created in results),1)
        self.assertEqual(len({job.id for job,created in results}),1)

    def test_unknown_commit_remains_recoverable_without_duplicate_job(self):
        self.client.unknown_commit=True
        with self.assertRaises(TimeoutError): self.reserve()
        job=self.store.find_submission(OWNER,KEY)
        self.assertEqual(job.state,'HOLD')
        self.assertEqual(self.reserve(),(job,False))

    def test_snapshots_are_detached_and_tamper_rejected(self):
        job,_=self.reserve();self.snapshot['input_order'].reverse()
        self.assertNotEqual(job.snapshot,self.snapshot)
        path=self.store._job_ref(OWNER,job.id).path
        self.client.data[path]['snapshot_json']='{}'
        with self.assertRaises(ValueError): self.store.get(OWNER,job.id)

    def test_dispatch_execution_is_globally_unique(self):
        a,_=self.reserve();b,_=self.reserve(owner=OTHER)
        a=self.dispatch(a)
        with self.assertRaises(ValueError): self.dispatch(b)
        self.assertEqual(self.store.get(OTHER,b.id).state,'HOLD')
        with self.assertRaises(ValueError): self.dispatch(a,'another_execution')

    def test_start_and_verified_completion_bind_exact_owner_execution(self):
        job=self.dispatch(self.reserve()[0])
        with self.assertRaises(ValueError): self.store.record_started(OWNER,job.id,'wrong_execution')
        job=self.store.record_started(OWNER,job.id,job.receipt.execution_id)
        with self.assertRaises(ValueError): self.store.record_verified_completion(OWNER,job.id,replace(self.proof(job),owner_id=OTHER))
        job=self.store.record_verified_completion(OWNER,job.id,self.proof(job))
        self.assertEqual(job.state,'complete');self.assertEqual(self.store.get(OWNER,job.id),job)
        with self.assertRaises(ValueError): self.store.record_verified_completion(OWNER,job.id,self.proof(job))

    def test_spend_arm_freezes_cap_and_rejects_duplicates(self):
        journal=self.spend();request={'text':'synthetic request'}
        sha=journal.arm('operation_001',request,4,10)
        self.assertEqual(journal.get('operation_001'),dict(plan_sha256=sha,reservation_cents=4,state='may_have_submitted',receipt=None))
        with self.assertRaises(ValueError): journal.arm('operation_001',{'other':1},1,10)
        with self.assertRaises(ValueError): journal.arm('operation_002',request,1,10)
        with self.assertRaises(ValueError): journal.arm('operation_002',{'other':2},1,20)
        with self.assertRaises(ValueError): journal.arm('operation_002',{'other':2},7,10)
        self.assertIsNone(journal.get('operation_002'))

    def test_spend_concurrent_budget_never_overdraws(self):
        journal=self.spend()
        def reserve(i):
            try: journal.arm(f'operation_{i:03}',{'sequence':i},3,10);return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(reserve,range(20)))
        self.assertEqual(sum(results),3)

    def test_spend_unknown_commit_is_not_rearmed(self):
        journal=self.spend();self.client.unknown_commit=True
        with self.assertRaises(TimeoutError):journal.arm('operation_001',{'x':1},2,10)
        self.assertEqual(journal.get('operation_001')['state'],'may_have_submitted')
        with self.assertRaises(ValueError):journal.arm('operation_001',{'x':1},2,10)

    def test_spend_receipt_exact_request_global_uniqueness_and_no_secrets(self):
        journal=self.spend();journal.arm('operation_001',{'x':1},2,10)
        receipt={'request_id':'synthetic_request_001','output_sha256':'a'*64}
        with self.assertRaises(ValueError):journal.record('operation_001',{'x':2},receipt)
        with self.assertRaises(ValueError):journal.record('operation_001',{'x':1},{**receipt,'api_key':'not-real'})
        journal.record('operation_001',{'x':1},receipt)
        self.assertEqual(journal.get('operation_001')['state'],'received')
        other=self.spend(owner=OTHER);other.arm('operation_001',{'x':1},2,10)
        with self.assertRaises(ValueError):other.record('operation_001',{'x':1},receipt)
        self.assertEqual(other.get('operation_001')['state'],'may_have_submitted')

    def test_shared_budget_cannot_reset_across_jobs_or_owners(self):
        def journal(owner,job,total=10,prior=4):
            return FirestoreSpendJournal(self.client,project_id=PROJECT,namespace='shared_budget_fixture',owner_id=owner,job_id=job,
                budget_scope='synthetic_shared_scope',approved_total_cap_cents=total,prior_spend_cents=prior,transaction_runner=self.client.run)
        journal(OWNER,'a'*32).arm('operation_001',{'part':1},3,10)
        with self.assertRaises(ValueError):journal(OTHER,'b'*32).arm('operation_002',{'part':2},4,10)
        journal(OTHER,'b'*32).arm('operation_002',{'part':2},3,10)
        with self.assertRaises(ValueError):journal(OWNER,'c'*32).arm('operation_003',{'part':3},1,10)
        with self.assertRaises(ValueError):journal(OWNER,'d'*32,total=20).arm('operation_004',{'part':4},1,10)
        with self.assertRaises(ValueError):journal(OWNER,'d'*32,prior=0).arm('operation_004',{'part':4},1,10)

    def test_receipts_use_firestore_compatible_canonical_text(self):
        journal=self.spend();request={'x':1};journal.arm('operation_001',request,2,10)
        receipt={'request_id':'synthetic_request_001','nested':[[1,2],[3,4]]}
        journal.record('operation_001',request,receipt)
        self.assertEqual(journal.get('operation_001')['receipt'],receipt)
        stored=self.client.data[journal._operation_ref('operation_001').path]
        self.assertIsInstance(stored['receipt'],str)

    def test_dispatch_journal_reserves_once_and_recovers_operation(self):
        journal=FirestoreDispatchJournal(self.client,project_id=PROJECT,namespace='fixture_namespace',transaction_runner=self.client.run)
        job,_=self.reserve()
        self.assertTrue(journal.reserve_dispatch(OWNER,job.id,job.snapshot_sha256,'f'*64))
        self.assertFalse(journal.reserve_dispatch(OWNER,job.id,job.snapshot_sha256,'f'*64))
        with self.assertRaises(ValueError):journal.reserve_dispatch(OWNER,job.id,job.snapshot_sha256,'e'*64)
        self.assertIsNone(journal.get_dispatch_operation(OWNER,job.id,job.snapshot_sha256))
        operation='projects/synthetic-project/locations/us-central1/operations/synthetic_operation_001'
        journal.record_dispatch_operation(OWNER,job.id,job.snapshot_sha256,operation)
        self.assertEqual(journal.get_dispatch_operation(OWNER,job.id,job.snapshot_sha256),operation)
        self.assertIsNone(journal.get_dispatch_operation(OTHER,job.id,job.snapshot_sha256))
        other,_=self.reserve(key='fixture_request_000002')
        journal.reserve_dispatch(OWNER,other.id,other.snapshot_sha256,'e'*64)
        with self.assertRaises(ValueError):journal.record_dispatch_operation(OWNER,other.id,other.snapshot_sha256,operation)

    def test_dispatch_unknown_reservation_commit_never_allows_second_submit(self):
        journal=FirestoreDispatchJournal(self.client,project_id=PROJECT,namespace='fixture_namespace',transaction_runner=self.client.run)
        job,_=self.reserve();self.client.unknown_commit=True
        with self.assertRaises(TimeoutError):journal.reserve_dispatch(OWNER,job.id,job.snapshot_sha256,'f'*64)
        self.assertFalse(journal.reserve_dispatch(OWNER,job.id,job.snapshot_sha256,'f'*64))

    def test_artifact_registry_atomically_completes_and_binds_generation(self):
        registry=FirestoreArtifactRegistry(self.client,project_id=PROJECT,namespace='fixture_namespace',transaction_runner=self.client.run)
        job=self.dispatch(self.reserve()[0]);proof=self.proof(job)
        reference=dict(generation='42',sha256=proof.sha256,byte_count=proof.byte_count,content_type='video/mp4')
        completed=registry.commit_verified(OWNER,job,proof,reference)
        self.assertEqual(completed.state,'complete')
        self.assertEqual(registry(OWNER,completed,proof.artifact_id),reference)
        self.assertEqual(registry.verify(OWNER,completed,proof.artifact_id),proof)
        self.assertEqual(registry.commit_verified(OWNER,completed,proof,reference),completed)
        with self.assertRaises(ValueError):registry.commit_verified(OWNER,completed,proof,{**reference,'generation':'43'})
        with self.assertRaises(ValueError):registry(OTHER,completed,proof.artifact_id)

    def test_artifact_registry_rejects_unverified_storage_reference(self):
        registry=FirestoreArtifactRegistry(self.client,project_id=PROJECT,namespace='fixture_namespace',transaction_runner=self.client.run)
        job=self.dispatch(self.reserve()[0]);proof=self.proof(job)
        for reference in [dict(generation='latest',sha256=proof.sha256,byte_count=proof.byte_count,content_type='video/mp4'),
                          dict(generation='42',sha256='f'*64,byte_count=proof.byte_count,content_type='video/mp4')]:
            with self.assertRaises(ValueError):registry.commit_verified(OWNER,job,proof,reference)
        self.assertEqual(self.store.get(OWNER,job.id).state,'queued')

    def test_spend_cap_inputs_are_strict_positive_integers(self):
        journal=self.spend()
        for reservation,cap in [(True,10),(0,10),(1,False),(2,1),(1,1_000_000_001)]:
            with self.subTest(reservation=reservation,cap=cap),self.assertRaises(ValueError):
                journal.arm('operation_001',{'x':1},reservation,cap)
        self.assertEqual(self.client.data,{})


if __name__=='__main__':unittest.main()
