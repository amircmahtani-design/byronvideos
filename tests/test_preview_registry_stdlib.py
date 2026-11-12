from candidate_media_fixture import synthetic_evidence
"""Offline namespace/API tests; injected approval is NOT real authorization or QC."""
import hashlib,io,json,unittest
from dataclasses import asdict,replace
from fastapi.testclient import TestClient
from byron_video.hosted_control import Principal,DispatchReceipt,VerifiedArtifact,canonical,create_app
from byron_video.cloud_journal import FirestoreJobStore,FirestoreArtifactRegistry
from byron_video.preview_registry import FirestorePreviewRegistry,PREVIEW_STATE
from byron_video.cloud_storage import artifact_object_name
from test_cloud_journal_stdlib import Client,PROJECT,OWNER,OTHER,KEY

class PreviewRegistryChecks(unittest.TestCase):
    def setUp(self):
        from byron_video.preview_worker import PreviewArtifact
        self.db=Client();self.args=dict(project_id=PROJECT,namespace='fixture_namespace',transaction_runner=self.db.run)
        self.jobs=FirestoreJobStore(self.db,**self.args)
        from byron_video.private_preview import policy
        from byron_video.source_planning import policy as source_policy
        from byron_video.automated_review import candidate_policy
        from byron_video.hosted_control import digest
        intent=dict(mode='PRIVATE_DRAFT_PREVIEW',preview_policy_sha256=digest(policy()),preview_release_sha256='f'*64,
                    source_policy_sha256=digest(source_policy()),review_policy_sha256=digest(candidate_policy()))
        snapshot=dict(input_order=['painting_001','painting_002','painting_003','painting_004'],narrated_text_sha256='a'*64,
                      processing_plan=dict(preview_contract=intent))
        self.job,_=self.jobs.reserve(OWNER,KEY,'b'*64,snapshot)
        self.job=self.jobs.record_dispatch(OWNER,self.job.id,DispatchReceipt(self.job.id,self.job.snapshot_sha256,'fixture_execution'))
        self.payload=b'synthetic mp4 boundary bytes, NOT native QC';sha=hashlib.sha256(self.payload).hexdigest()
        self.proof=PreviewArtifact(candidate_media_evidence_json=synthetic_evidence(self.job,sha,30),artifact_id='fixture_draft_artifact',job_id=self.job.id,owner_id=OWNER,
            snapshot_sha256=self.job.snapshot_sha256,execution_id=self.job.receipt.execution_id,sha256=sha,byte_count=len(self.payload),
            duration_seconds=30,width=1080,height=1920,video_codec='h264',audio_codec='aac',fps=24,
            source_order=tuple(snapshot['input_order']),narrated_text_sha256='a'*64,caption_text_sha256='a'*64,
            full_decode_verified=True,verification_method='ffmpeg-full-decode-v1',artifact_kind='PRIVATE_DRAFT_PREVIEW',
            authorization_sha256='b'*64,preview_policy_sha256=intent['preview_policy_sha256'],preview_release_sha256=intent['preview_release_sha256'],context_sha256='c'*64,watermark_proof_sha256='d'*64)
        self.authorized=True
        self.registry=FirestorePreviewRegistry(self.db,authorization_verifier=lambda job,proof:self.authorized,**self.args)
        self.reference=dict(generation='42',sha256=sha,byte_count=len(self.payload),content_type='video/mp4')
    def commit(self):return self.registry.commit_verified(OWNER,self.job,self.proof,self.reference)
    def test_namespace_and_no_final_promotion_or_overwrite(self):
        final=FirestoreArtifactRegistry(self.db,**self.args)
        with self.assertRaises(ValueError):final.commit_verified(OWNER,self.job,self.proof,self.reference)
        with self.assertRaises(ValueError):self.jobs.record_verified_completion(OWNER,self.job.id,self.proof)
        result=self.commit();self.assertEqual(result.state,PREVIEW_STATE)
        self.assertEqual(self.commit(),result)
        self.assertTrue(any('/preview_artifacts/' in p for p in self.db.data))
        self.assertFalse(any('/artifacts/' in p for p in self.db.data))
        self.assertNotEqual(artifact_object_name('byron-private',Principal(OWNER),result.id,self.proof.artifact_id),artifact_object_name('byron-private-preview-v1',Principal(OWNER),result.id,self.proof.artifact_id))
        with self.assertRaises(ValueError):self.registry.commit_verified(OWNER,self.job,replace(self.proof,artifact_id='replacement_artifact'),self.reference)
        plain=VerifiedArtifact(**{k:v for k,v in asdict(self.proof).items() if k in VerifiedArtifact.__dataclass_fields__})
        with self.assertRaises(ValueError):final.commit_verified(OWNER,result,plain,self.reference)
    def test_absent_or_expired_authorization_denies_commit_and_read(self):
        blocked=FirestorePreviewRegistry(self.db,**self.args)
        with self.assertRaises(ValueError):blocked.commit_verified(OWNER,self.job,self.proof,self.reference)
        result=self.commit();self.authorized=False
        with self.assertRaises(ValueError):self.registry.verify(OWNER,result,self.proof.artifact_id)
    def test_owner_only_download_draft_labels_headers_and_tamper_denial(self):
        result=self.commit()
        def resolve(request):
            value=request.headers.get('x-fixture-owner');return Principal(value) if value else None
        app=create_app(store=self.jobs,principal_resolver=resolve,artifact_verifier=self.registry.verify,
            artifact_reader=lambda owner,job,proof:io.BytesIO(self.payload))
        with TestClient(app) as client:
            path='/api/jobs/'+result.id
            self.assertEqual(client.get(path+'/video').status_code,401)
            self.assertEqual(client.get(path+'/video',headers={'x-fixture-owner':OTHER}).status_code,404)
            headers={'x-fixture-owner':OWNER}
            status=client.get(path,headers=headers).json()
            self.assertEqual(status['status'],'draft_preview_ready');self.assertIs(status['draft'],True)
            self.assertFalse(status['live_release']);self.assertNotIn('owner_id',status)
            download=client.get(path+'/video',headers=headers)
            self.assertEqual(download.status_code,200);self.assertEqual(download.content,self.payload)
            self.assertIn('DRAFT',download.headers['content-disposition']);self.assertIn('no-store',download.headers['cache-control'])
            self.assertEqual(download.headers['x-byron-artifact-kind'],'PRIVATE_DRAFT_PREVIEW')
            self.authorized=False;self.assertEqual(client.get(path+'/video',headers=headers).status_code,503)
    def test_state_cannot_launder_draft_as_final(self):
        result=self.commit();bad=replace(result,state='complete')
        with self.assertRaises(ValueError):bad.public()
        with self.assertRaises(ValueError):FirestoreJobStore._record(asdict(bad),OWNER,result.id)
    def test_only_exact_server_frozen_preview_intent_is_accepted(self):
        from test_hosted_uploads_stdlib import plan
        from byron_video.hosted_control import validate_private_plan
        value=plan();intent=self.job.snapshot['processing_plan']['preview_contract']
        self.assertNotIn('preview_contract',validate_private_plan(value))
        value['preview_contract']=intent
        self.assertEqual(validate_private_plan(value)['preview_contract'],intent)
        for update in [{'mode':'FINAL'},{'public_release':True},{'preview_policy_sha256':'0'*64}]:
            with self.subTest(update=update),self.assertRaises(ValueError):validate_private_plan({**value,'preview_contract':{**intent,**update}})
    def test_concrete_verifier_rechecks_authorization_and_native_evidence_bindings(self):
        from byron_video.private_preview import FirestorePreviewAuthorizations,AUTH_SCHEMA
        from byron_video.preview_worker import FirestorePreviewEvidence
        from byron_video.preview_registry import PreviewAuthorizationVerifier
        from byron_video.hosted_control import digest
        intent=self.job.snapshot['processing_plan']['preview_contract'];budget={'fixture':'private trusted budget'}
        authorization=dict(schema=AUTH_SCHEMA,reference='fixture_private_preview_approval',owner_id=OWNER,job_id=self.job.id,
            snapshot_sha256=self.job.snapshot_sha256,source_policy_sha256=intent['source_policy_sha256'],review_policy_sha256=intent['review_policy_sha256'],
            preview_release_sha256=intent['preview_release_sha256'],preview_policy_sha256=intent['preview_policy_sha256'],budget=budget,
            expires_at=1100,private_preview_approved=True,owner_only=True,public_release=False,human_style_accepted=False)
        auth=FirestorePreviewAuthorizations(self.db,budget_loader=lambda job:budget,preview_release_sha256=intent['preview_release_sha256'],now=lambda:1000,**self.args)
        auth.commit(self.job,authorization,digest(authorization))
        evidence=FirestorePreviewEvidence(self.db,**self.args)
        context=dict(authorization=authorization,authorization_sha256=digest(authorization),preview_release_sha256=intent['preview_release_sha256'])
        # Synthetic store fixture only: no assertion that these are native media.
        evidence._immutable(evidence._ref(OWNER,self.job.id,'preview_worker_evidence'),dict(owner_id=OWNER,job_id=self.job.id,
            snapshot_sha256=self.job.snapshot_sha256,report={},report_sha256=digest({}),preview_context=context,preview_context_sha256=digest(context)))
        watermark=dict(artifact_sha256=self.proof.sha256,all_frames_verified=True,method='all-decoded-frame-glyph-template-v1',frames_verified=720)
        proof=replace(self.proof,authorization_sha256=digest(authorization),context_sha256=digest(context),watermark_proof_sha256=digest(watermark))
        evidence.record_output(OWNER,self.job,proof,watermark)
        verifier=PreviewAuthorizationVerifier(auth,evidence)
        self.assertTrue(verifier(self.job,proof))
        with self.assertRaises(ValueError):verifier(self.job,replace(proof,sha256='0'*64))
        with self.assertRaises(ValueError):verifier(self.job,replace(proof,watermark_proof_sha256='0'*64))
        auth.now=lambda:1101
        with self.assertRaises(ValueError):verifier(self.job,proof)
    def test_capability_distinguishes_explicit_preview_from_live_release(self):
        from fastapi import FastAPI
        from types import SimpleNamespace
        from byron_video.hosted_uploads import install_creator_routes
        from test_hosted_uploads_stdlib import plan
        value=plan();value['preview_contract']=self.job.snapshot['processing_plan']['preview_contract']
        async def authorize(request):return SimpleNamespace(principal=Principal(OWNER),csrf_token='c'*32)
        app=FastAPI()
        # Injected boundary fixture; production create_app independently binds signed runtime.
        install_creator_routes(app,access=SimpleNamespace(authorize=authorize),storage=None,enabled=True,
            runtime_binding=SimpleNamespace(check=lambda:True,release_sha256='d'*64),plan_provider=lambda owner:value)
        with TestClient(app) as client:
            capability=client.get('/api/creator-capabilities').json()
            self.assertEqual(capability['mode'],'hosted_private_preview');self.assertIs(capability['private_preview'],True)
            self.assertFalse(capability['live_release']);self.assertFalse(capability['synthetic_fixture'])
