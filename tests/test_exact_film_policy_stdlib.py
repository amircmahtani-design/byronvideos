from candidate_media_fixture import synthetic_evidence
"""Control/lineage contracts with explicitly timestamped fake Firestore only.

Media/evidence rows are synthetic boundary fixtures, not real semantic approval.
Separate exact-film flow tests exercise actual local rendering/native archives.
"""
import copy,hashlib,json,unittest
from dataclasses import asdict
from datetime import datetime,timezone
from types import SimpleNamespace
from byron_video.exact_film_policy import *
from byron_video.hosted_control import Principal,DispatchReceipt
from byron_video.cloud_journal import FirestoreJobStore
from byron_video.preview_worker import PreviewArtifact,FirestorePreviewEvidence
from byron_video.preview_registry import FirestorePreviewRegistry
from byron_video.continuation_worker import FirestorePreparedContinuation
from byron_video.private_preview import FirestorePreviewAuthorizations,AUTH_SCHEMA,policy
from byron_video.source_planning import policy as source_policy
from byron_video.automated_review import candidate_policy
from byron_video.preview_composition import release_hash as preview_release
from byron_video.exact_film_composition import release_hash
from byron_video.exact_film_runtime import contract
from test_cloud_journal_stdlib import Client,Ref,PROJECT,OWNER,KEY
from test_worker_binding_stdlib import fixture


class TimestampedRef(Ref):
    def get(self,transaction=None):
        snap=super().get(transaction)
        snap.create_time=self.client.timestamps.get(self.path)
        return snap


class TimestampedClient(Client):
    def __init__(self,clock):super().__init__();self.timestamps={};self.clock=clock
    def document(self,path):return TimestampedRef(self,path)
    def run(self,fn):
        before=set(self.data)
        try:return super().run(fn)
        finally:
            for path in set(self.data)-before:self.timestamps[path]=datetime.fromtimestamp(self.clock[0],timezone.utc)


class ExactPolicyChecks(unittest.TestCase):
    def setUp(self):
        self.clock=[1000.];self.db=TimestampedClient(self.clock);self.owner=Principal(OWNER)
        self.args=dict(project_id=PROJECT,namespace='fixture_exact_policy',transaction_runner=self.db.run)
        self.target=fixture()[0];images=copy.deepcopy(fixture()[3]['frozen_job']['images']);text='Exact synthetic narration.'
        self.budget=dict(scope='fixture_campaign',approved_total_cap_cents=100,prior_spend_cents=10,job_cap_cents=20,retry_allowance_cents=0,approval_reference='fixture_spend_approval')
        intent=dict(mode='PRIVATE_DRAFT_PREVIEW',preview_policy_sha256=fingerprint(policy()),preview_release_sha256=preview_release(),source_policy_sha256=fingerprint(source_policy()),review_policy_sha256=fingerprint(candidate_policy()))
        snapshot=dict(images=images,input_order=[i['id'] for i in images],narrated_text=text,narrated_text_sha256=_sha(text.encode()),processing_plan=dict(narration_profile={'fixture':'voice'},preview_contract=intent,exact_film_contract=contract()))
        jobs=FirestoreJobStore(self.db,**self.args);job,_=jobs.reserve(OWNER,KEY,'b'*64,snapshot)
        job=jobs.record_dispatch(OWNER,job.id,DispatchReceipt(job.id,job.snapshot_sha256,'fixture_preview_execution'))
        self.auth=dict(schema=AUTH_SCHEMA,reference='fixture_private_preview',owner_id=OWNER,job_id=job.id,snapshot_sha256=job.snapshot_sha256,
            **{k:v for k,v in intent.items() if k!='mode'},budget=self.budget,expires_at=1100.,private_preview_approved=True,owner_only=True,public_release=False,human_style_accepted=False)
        self.auths=FirestorePreviewAuthorizations(self.db,**self.args,budget_loader=lambda j:self.budget,preview_release_sha256=preview_release(),now=lambda:self.clock[0])
        self.auths.commit(job,self.auth,fingerprint(self.auth))
        report=dict(exact_text=text,profile_sha256=fingerprint(snapshot['processing_plan']['narration_profile']))
        receipts=[dict(schema='byron-private-preview-clip-v1',image_id=i['id'],human_approved=False) for i in images]
        context=dict(schema_version=2,status='PRIVATE_DRAFT_PREVIEW_CONTEXT',owner_id=OWNER,job_id=job.id,snapshot_sha256=job.snapshot_sha256,
            narration_report_sha256=fingerprint(report),authorization=self.auth,authorization_sha256=fingerprint(self.auth),preview_release_sha256=preview_release())
        self.evidence=FirestorePreviewEvidence(self.db,**self.args);self.evidence.commit(self.owner,job,report,receipts,preview_context=context)
        payload=b'synthetic retained preview';watermark=dict(artifact_sha256=_sha(payload),all_frames_verified=True,method='all-decoded-frame-glyph-template-v1',frames_verified=720)
        self.proof=PreviewArtifact(candidate_media_evidence_json=synthetic_evidence(job,_sha(payload),30),artifact_id='fixture_preview_artifact',job_id=job.id,owner_id=OWNER,snapshot_sha256=job.snapshot_sha256,execution_id=job.receipt.execution_id,
            sha256=_sha(payload),byte_count=len(payload),duration_seconds=30,width=1080,height=1920,video_codec='h264',audio_codec='aac',fps=24,source_order=tuple(snapshot['input_order']),narrated_text_sha256=snapshot['narrated_text_sha256'],caption_text_sha256=snapshot['narrated_text_sha256'],full_decode_verified=True,verification_method='ffmpeg-full-decode-v1',
            authorization_sha256=fingerprint(self.auth),preview_policy_sha256=intent['preview_policy_sha256'],preview_release_sha256=preview_release(),context_sha256=fingerprint(context),watermark_proof_sha256=fingerprint(watermark))
        self.evidence.record_output(self.owner,job,self.proof,watermark)
        registry=FirestorePreviewRegistry(self.db,**self.args,authorization_verifier=lambda j,p:True)
        self.job=registry.commit_verified(self.owner,job,self.proof,dict(generation='1',sha256=self.proof.sha256,byte_count=self.proof.byte_count,content_type='video/mp4'))
        prefix=f'owners/{_sha(OWNER.encode())}/jobs/{job.id}/'
        def ref(tail,data,mime):return dict(bucket=self.target['bucket'],object=prefix+tail,generation='1',sha256=_sha(data),bytes=len(data),content_type=mime)
        manifest=dict(schema_version=1,status='DRAFT_NOT_ACTIVATED',job_id=job.id,owner_namespace=_sha(OWNER.encode()),source_job_sha256=job.snapshot_sha256,source_order=snapshot['input_order'],
            composition=ref('inputs/composition.json',b'{}','application/json'),narration=ref('inputs/narration.wav',b'wave','audio/wav'),
            clips=[dict(image_id=image['id'],position=i,original_sha256=image['sha256'],video=ref(f'inputs/clip-{i:02}.mp4',str(i).encode(),'video/mp4')) for i,image in enumerate(images,1)])
        manifest['sha256']=fingerprint(manifest);reference=ref('render.json',canonical(manifest).encode(),'application/json')
        FirestorePreparedContinuation(self.db,**self.args).commit(self.job,manifest,reference,fingerprint(context))
        self.loader=FirestoreRetainedPreviewLineage(self.db,**self.args,budget_loader=lambda j:self.budget,target=self.target,preview_evidence=self.evidence,now=lambda:self.clock[0])
        self.actions=FirestoreExactFilmActions(self.db,**self.args,lineage_loader=self.loader,final_release_sha256=release_hash(),now=lambda:self.clock[0])
        self.clock[0]=1200. # Original generation authorization is now expired.
    def challenge(self,action=RENDER):return self.actions.create_challenge(self.owner,self.job,self.proof.artifact_id,action)
    def approve(self,challenge,key='fixture_action_key_0001',action=RENDER):return self.actions.approve(self.owner,self.job,challenge['challenge_id'],challenge['challenge_sha256'],key,expected_action=action,expected_artifact_id=self.proof.artifact_id)
    def test_expired_generation_has_valid_server_committed_lineage_only(self):
        with self.assertRaises(ValueError):self.auths.load(self.job)
        lineage=self.loader.load(self.owner,self.job,self.proof.artifact_id);self.assertEqual(lineage['preview_committed_at'],1000.)
        result=self.approve(self.challenge());approval=result['record'];self.assertTrue(result['created'])
        bundle=self.actions.approval_lineage(self.owner,self.job,approval['approval_id'])
        self.assertEqual(validate_exact_film_lineage(self.job,bundle,release_hash(),now=1200),1000)
        self.assertFalse(approval['paid_generation_allowed']);self.assertFalse(approval['future_style_approved'])
        with self.assertRaises(ValueError):self.auths.load(self.job)
    def test_same_key_replay_and_new_challenge_reuse_one_film_approval(self):
        challenge=self.challenge();first=self.approve(challenge);second=self.approve(challenge)
        self.assertFalse(second['created']);self.assertEqual(first['record'],second['record'])
        third=self.approve(self.challenge(),'fixture_action_key_0002');self.assertFalse(third['created']);self.assertEqual(third['record'],first['record'])
        self.assertEqual(self.actions.find_request(self.owner,self.job,'fixture_action_key_0001')['record'],first['record'])
        self.assertEqual(len([p for p in self.db.data if '/exact_film_approvals/' in p]),1)
    def test_challenge_wrong_route_consumption_and_replay_key_conflict(self):
        challenge=self.challenge();before=copy.deepcopy(self.db.data)
        with self.assertRaises(ValueError):self.approve(challenge,action=VIEW)
        self.assertEqual(before,self.db.data)
        self.approve(challenge)
        with self.assertRaises(ValueError):self.approve(challenge,'fixture_action_key_other')
        with self.assertRaises(ValueError):self.approve(self.challenge(),'fixture_action_key_0001')
    def test_view_renewal_grants_only_exact_view_and_not_generation(self):
        first=self.approve(self.challenge(VIEW),action=VIEW)['record']
        self.assertTrue(self.actions.authorize_view(self.owner,self.job,self.proof.artifact_id,self.proof.sha256,fingerprint(asdict(self.proof))))
        with self.assertRaises(ValueError):self.actions.authorize_view(self.owner,self.job,self.proof.artifact_id,'e'*64,fingerprint(asdict(self.proof)))
        self.clock[0]=first['expires_at']+1
        with self.assertRaises(ValueError):self.actions.read_view_lease(self.owner,self.job,first['lease_id'])
        newer=self.approve(self.challenge(VIEW),'fixture_view_new_0001',VIEW)['record'];self.assertNotEqual(first['lease_id'],newer['lease_id'])
        self.assertIsNone(self.actions.get_approval(self.job))
        with self.assertRaises(ValueError):self.auths.load(self.job)
    def test_missing_or_forged_server_commit_time_fails_closed(self):
        path=next(p for p in self.db.data if '/preview_artifacts/' in p)
        del self.db.timestamps[path]
        with self.assertRaises(ValueError):self.challenge()
        self.db.timestamps[path]=datetime.fromtimestamp(1150,timezone.utc)
        with self.assertRaises(ValueError):self.challenge()
    def test_cross_owner_raw_email_and_expired_challenge_denied(self):
        for actor in (Principal('other_owner'),OWNER):
            with self.assertRaises(ValueError):self.actions.create_challenge(actor,self.job,self.proof.artifact_id,RENDER)
        challenge=self.challenge();self.clock[0]=challenge['expires_at']+1
        with self.assertRaises(ValueError):self.approve(challenge)
    def test_unknown_transaction_commit_recovers_without_new_approval(self):
        challenge=self.challenge();self.db.unknown_commit=True
        with self.assertRaises(TimeoutError):self.approve(challenge)
        recovered=self.actions.find_request(self.owner,self.job,'fixture_action_key_0001');self.assertIsNotNone(recovered)
        self.assertFalse(self.approve(challenge)['created'])
    def test_expired_render_approval_never_refreshes_with_new_challenge(self):
        first=self.approve(self.challenge())['record'];self.clock[0]=first['expires_at']+1
        with self.assertRaises(ValueError):self.actions.get_approval(self.job)
        replay=self.approve(self.challenge(),'fixture_render_second_key')['record'];self.assertEqual(replay,first)
        self.assertEqual(self.actions.read_approval(self.owner,self.job,first['approval_id'],require_current=False),first)


class ExactReadOnlyReplayChecks(unittest.TestCase):
    def setUp(self):
        import test_private_preview_stdlib as preview_tests
        self.fixture=preview_tests.PreviewChecks();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;self.record=f.record(0)
        self.replay=ReadOnlyNativeReplay(journal=f.journal,source_response_store=f.source.archive,review_response_store=f.native.store,
            original_reader=lambda job,image:f.source.originals[image['id']],trusted_budget=f.source.budget)
    def test_expired_disabled_generation_does_not_block_readonly_native_archives(self):
        f=self.fixture;f.source.active=False
        before=(list(f.source.calls),list(f.native.calls))
        self.assertTrue(self.replay.source(f.candidate,job=f.job,narration_report=f.report))
        self.assertEqual(self.replay.review(self.record['review']['native_review']),self.record['review']['archive_verification'])
        self.assertEqual((f.source.calls,f.native.calls),before)
    def test_missing_or_tampered_archive_never_falls_back_to_provider(self):
        f=self.fixture;before=list(f.source.calls)
        for path in f.source.archive.root.glob('*.json'):path.write_bytes(b'changed-source-archive')
        with self.assertRaises(ValueError):self.replay.source(f.candidate,job=f.job,narration_report=f.report)
        self.assertEqual(f.source.calls,before)

def _sha(data):return hashlib.sha256(data).hexdigest()

if __name__=='__main__':unittest.main()
