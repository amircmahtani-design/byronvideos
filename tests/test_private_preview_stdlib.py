"""Private-preview policy/chain tests; all paid/native responses are local fakes."""
import copy,hashlib,io,json,re,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image,ImageDraw
from byron_video import private_preview as preview,source_planning,automated_review,vertex_review
from byron_video.elevenlabs_adapter import fingerprint
from byron_video.motion_continuation import bind_plan_budget,scene_prompt,_checked_plan
from byron_video.continuation_worker import stage_committed_continuation,validate_continuation_context,_operation
from byron_video.review_budget import FilmReviewFunding,FundedReviewExecutor
import test_source_planning_stdlib as sources
import test_vertex_review_stdlib as native_tests
from test_automated_review_stdlib import request_fixture
from test_review_budget_stdlib import quote as review_quote

NOW=1000.;RELEASE='f'*64

class PreviewChecks(unittest.TestCase):
    def setUp(self):
        self.source=sources.PlanningChecks();self.source.setUp();self.addCleanup(self.source.tearDown)
        self.native=native_tests.VertexChecks();self.native.setUp();self.addCleanup(self.native.tearDown)
        self.job=self.source.job;self.root=self.source.root;self.report=self.source.report
        for index,image in enumerate(self.job.snapshot['images']):
            canvas=Image.new('RGB',(512,512),'navy');draw=ImageDraw.Draw(canvas)
            for x in range(0,512,8):draw.line((x,0,x,511),fill=(x%256,200,index*30),width=3)
            stream=io.BytesIO();canvas.save(stream,format='PNG');body=stream.getvalue()
            image['sha256']=hashlib.sha256(body).hexdigest();self.source.originals[image['id']]['bytes']=body
        contract=dict(mode='PRIVATE_DRAFT_PREVIEW',preview_policy_sha256=fingerprint(preview.policy()),preview_release_sha256=RELEASE,
            source_policy_sha256=fingerprint(source_planning.policy()),review_policy_sha256=fingerprint(automated_review.candidate_policy()))
        self.job.snapshot['processing_plan']['preview_contract']=contract;self.job.snapshot_sha256=fingerprint(self.job.snapshot)
        self.audio=b'synthetic exact audio';words=[]
        for index,match in enumerate(re.finditer(r'\S+',self.report['exact_text'])):
            words.append(dict(text=match.group(),char_start=match.start(),char_end=match.end(),start=index*9.3,end=index*9.3+1))
        self.report.update(provider='elevenlabs',status='DRAFT_RELEASE_CANDIDATE',duration_seconds=37.9,words=words,synthetic=True,audio_sha256=hashlib.sha256(self.audio).hexdigest())
        self.source.prepared=source_planning.prepare_source_request(job=self.job,originals=self.source.originals,narration_report=self.report,config=sources.CONFIG)
        self.source.quote,self.source.proof=self.source.approvals(self.source.prepared);self.candidate=self.source.run_plan()
        self.budget=dict(scope='fixture_campaign',approved_total_cap_cents=100,prior_spend_cents=10,job_cap_cents=20,retry_allowance_cents=0,approval_reference='fixture_spend_approval')
        self.auth=dict(schema=preview.AUTH_SCHEMA,reference='fixture_private_preview',owner_id=self.job.owner_id,job_id=self.job.id,snapshot_sha256=self.job.snapshot_sha256,
            **{k:v for k,v in contract.items() if k!='mode'},budget=self.budget,expires_at=1100.,private_preview_approved=True,owner_only=True,public_release=False,human_style_accepted=False)
        self.timing=dict(schema_version=1,source='exact_narration_word_times',narration_text_sha256=self.job.snapshot['narrated_text_sha256'],audio_sha256=self.report['audio_sha256'],word_timing_sha256=fingerprint(words),duration_seconds=37.9,crossfade_seconds=.7,
            scenes=[dict(image_id=image['id'],start_seconds=round(i*9.3,3),end_seconds=round(i*9.3+10,3)) for i,image in enumerate(self.job.snapshot['images'])])
        self.plan=self.build();self.journal=self.source.journal;self.objects={};self.records={};self.qc={}
    def build(self,**overrides):
        args=dict(job=self.job,source_candidate=self.candidate,timing=self.timing,narration_report=self.report,expected_report_sha256=fingerprint(self.report),authorization=self.auth,expected_authorization_sha256=fingerprint(self.auth),preview_release_sha256=RELEASE,budget=self.budget,now=NOW,
            image_quote=dict(kind='image',endpoint='minimax/h3-max-turbo/image-to-video',duration_seconds=10,resolution='768P',max_cost_cents=1,valid_until_epoch=1100,reference='fixture_image_quote'),
            extension_quote=dict(kind='extend',endpoint='minimax/h3-max-turbo/extend-video',duration_seconds=5,resolution='768P',max_cost_cents=1,valid_until_epoch=1100,reference='fixture_extend_quote'))
        args.update(overrides);return preview.plan_private_preview_continuation(**args)
    def native_qc(self,record):
        if not hasattr(self,'funding'):
            q=review_quote();q.update(config=vertex_review.config(native_tests.PROJECT),max_output_tokens=8192,approved_review_cap_cents=20,token_limit_contract_reference=native_tests.CONTRACT)
            self.funding=FilmReviewFunding(quote_loader=lambda j,p:q,enabled=True,now=lambda:NOW)(job=self.job,plan=self.plan,journal=self.journal)
        evidence=copy.deepcopy(request_fixture()['body']['evidence']);evidence.update(source_sha256=record['source_sha256'],clip_sha256=record['video']['sha256'],duration_seconds=10)
        for index,sample in enumerate(evidence['samples']):sample['frame_index']=round(index*239/8);sample['time_seconds']=sample['frame_index']/24
        scene=next(s for s in self.plan['scenes'] if s['image_id']==record['image_id'])
        request=automated_review.prepare_request(owner_id=self.job.owner_id,job_id=self.job.id,image_id=record['image_id'],sequence=record['sequence'],prompt=scene_prompt(scene),evidence=evidence,config=self.funding['review_plan']['config'])
        response=native_tests.native_response(request);response['responseId']='fixture_native_'+record['image_id'];self.native.raw=vertex_review.encoded(response)
        result=FundedReviewExecutor(funding=self.funding,transport=self.native.transport(),response_store=self.native.store,activation_check=lambda:True,enabled=True,now=lambda:NOW)(request)
        self.assertEqual(result['state'],'CANDIDATE_PASS')
        verified=self.verify(result)
        return preview.make_preview_qc(candidate=record,result=result,plan=self.plan,expected_plan_sha256=fingerprint(self.plan),authorization=self.auth,expected_authorization_sha256=fingerprint(self.auth),job=self.job,preview_release_sha256=RELEASE,archive_verification=verified,now=NOW)
    def verify(self,result):return preview.verify_preview_review(result,funding=self.funding,response_store=self.native.store)
    def verify_source(self,candidate):
        self.assertEqual(self.source.run_plan(),candidate);return True
    def record(self,index):
        scene=self.plan['scenes'][index];body=('synthetic-full-'+str(index)).encode();sha=hashlib.sha256(body).hexdigest()
        prefix=f"owners/{hashlib.sha256(self.job.owner_id.encode()).hexdigest()}/jobs/{self.job.id}/"
        name=prefix+f"segments/{scene['image_id']}/00.mp4";self.objects[name]=body
        video=dict(bucket='fixture-private-bucket',object=name,generation='1',sha256=sha,bytes=len(body),content_type='video/mp4')
        rid=f'fixture_motion_{index}';url='https://queue.fal.run/minimax/h3-max-turbo/requests/'+rid
        handle=dict(endpoint='minimax/h3-max-turbo/image-to-video',request_id=rid,response_url=url,status_url=url+'/status')
        op=_operation(self.job.owner_id,self.job.id,scene['image_id'],0);binding={'fixture_native_request':op}
        wrapped=bind_plan_budget(plan=self.plan,expected_plan_sha256=fingerprint(self.plan),journal=self.journal,now=NOW)
        wrapped.arm(op,binding,1,20);wrapped.record(op,binding,handle)
        record=dict(schema_version=2,owner_id=self.job.owner_id,job_id=self.job.id,snapshot_sha256=self.job.snapshot_sha256,continuation_plan_sha256=fingerprint(self.plan),image_id=scene['image_id'],source_sha256=scene['source_sha256'],sequence=0,
            operation_id=op,journal_binding_sha256=fingerprint(binding),provider_handle=handle,provider_url='https://fal.media/files/'+rid+'.mp4',video=video,duration_seconds=10,presentation_crop=None,prior_record_sha256=None,prior_clip_sha256=None,synthetic=True,full_decode_verified=True)
        record['review']=self.native_qc(record);self.records[(scene['image_id'],0)]=(record,fingerprint(record));return record
    def test_separate_intent_and_source_prompts_never_forge_approval(self):
        self.assertEqual(self.plan['preview_schema'],preview.PLAN_SCHEMA)
        self.assertTrue(all('approved_prompt' not in s and s['preview_prompt'] for s in self.plan['scenes']))
        self.assertTrue(all(p['approved'] is False for p in self.plan['source_candidate']['proposals']))
        self.assertFalse(self.plan['human_approved']);self.assertEqual(self.plan['automatic_retries'],0)
    def test_auth_owner_policy_expiry_and_public_mode_fail_closed(self):
        for change in ({'owner_id':'other_owner'},{'review_policy_sha256':'a'*64},{'preview_policy_sha256':'a'*64},{'expires_at':999},{'public_release':True}):
            auth={**self.auth,**change}
            with self.assertRaises(ValueError):self.build(authorization=auth,expected_authorization_sha256=fingerprint(auth))
    def test_native_qc_archive_reread_and_tamper_rejection(self):
        record=self.record(0);preview.validate_preview_qc(record['review'],candidate=record,plan=self.plan,job=self.job,archive_verifier=self.verify,now=NOW)
        for path in self.native.native_archive.root.glob('*.json'):path.write_bytes(b'changed-native-archive')
        with self.assertRaises(ValueError):preview.validate_preview_qc(record['review'],candidate=record,plan=self.plan,job=self.job,archive_verifier=self.verify,now=NOW)
    def test_forged_clean_native_result_cannot_satisfy_archive_proof(self):
        record=self.record(0);result=copy.deepcopy(record['review']['native_review']);result['provider_request_id']=vertex_review.ID_PREFIX+'b'*64
        with self.assertRaises(ValueError):self.verify(result)
    def test_context_preserves_preview_schema_and_historical_validator_rejects(self):
        for index in range(4):self.record(index)
        prefix=f"owners/{hashlib.sha256(self.job.owner_id.encode()).hexdigest()}/jobs/{self.job.id}/";name=prefix+'narration.wav';self.objects[name]=self.audio
        audio=dict(bucket='fixture-private-bucket',object=name,generation='1',sha256=self.report['audio_sha256'],bytes=len(self.audio),content_type='audio/wav')
        def reader(ref):return dict(generation='1',content_type=ref['content_type'],stream=io.BytesIO(self.objects[ref['object']]))
        with patch('byron_video.portrait_composition.probe',return_value={'duration':10}):
            staged=stage_committed_continuation(job=self.job,plan=self.plan,expected_plan_sha256=fingerprint(self.plan),narration_report=self.report,expected_report_sha256=fingerprint(self.report),narration_ref=audio,
                segment_loader=lambda o,j,i,s:self.records[(i,s)],reader=reader,spend_journal=self.journal,root=self.root/'preview-stage',expected_bucket='fixture-private-bucket',enabled=True,now=NOW,preview_evidence_verifier=self.verify,preview_source_verifier=self.verify_source)
        kwargs=dict(staged=staged['staged'],frozen=self.job.snapshot,clip_receipts=staged['clip_receipts'],expected_report_sha256=fingerprint(self.report))
        self.assertEqual(len(preview.validate_preview_context(staged['continuation_context'],staged['continuation_context_sha256'],**kwargs,expected_authorization_sha256=fingerprint(self.auth),preview_release_sha256=RELEASE,preview_evidence_verifier=self.verify,preview_source_verifier=self.verify_source,now=NOW)),4)
        self.assertTrue(all('no_invented_action_reviewed' not in receipt for receipt in staged['clip_receipts']))
        with self.assertRaises(ValueError):validate_continuation_context(staged['continuation_context'],staged['continuation_context_sha256'],**kwargs)
    def test_capture_advances_only_preview_ready_with_native_archive_verifier(self):
        from byron_video.segment_capture import capture_segment,CloudSegmentObjects,FirestoreSegmentStore
        from test_cloud_storage_stdlib import FakeBucket
        from test_cloud_journal_stdlib import Client
        record=self.record(0);body=self.objects[record['video']['object']];bucket=FakeBucket();db=Client()
        store=FirestoreSegmentStore(db,project_id=native_tests.PROJECT,namespace='fixture_preview_capture',transaction_runner=db.run,
            objects=CloudSegmentObjects(SimpleNamespace(bucket=lambda name:bucket),bucket='fixture-private-bucket'))
        transport=SimpleNamespace(status=lambda handle:'COMPLETED',result=lambda handle:dict(url=record['provider_url'],bytes=len(body),request_id=handle['request_id']),download=lambda result:dict(bytes=body,sha256=hashlib.sha256(body).hexdigest()))
        parent=self
        class Review:
            def __call__(self,candidate):return parent.native_qc(candidate)
            def verify_preview_evidence(self,result):return parent.verify(result)
        with patch('byron_video.portrait_composition.probe',return_value={'duration':10}):
            captured=capture_segment(job=self.job,plan=self.plan,expected_plan_sha256=fingerprint(self.plan),image_id=record['image_id'],sequence=0,
                spend_journal=self.journal,transport=transport,segment_store=store,review_loader=Review(),root=self.root/'preview-capture',narration_report=self.report,enabled=True,now=NOW)
        self.assertEqual(captured['state'],'READY');self.assertEqual(captured['record']['schema_version'],2)
        self.assertEqual(store.get_capture(self.job,record['image_id'],0)['state'],'PREVIEW_READY')
        self.assertNotIn('no_invented_action_reviewed',captured['record']['review']);self.assertFalse(captured['record']['review']['human_approved'])
    def test_paid_transport_final_authorization_after_key_read_blocks_post(self):
        from byron_video.provider_execution import ElevenLabsHTTPTransport,FalQueueTransport,ProviderOutcomeUnknown,plan_anchored_motion
        from byron_video.elevenlabs_adapter import plan_requests
        from test_provider_execution_stdlib import FakeClient,FakeResponse,profile
        calls=[];current=[NOW]
        def key():current[0]=2000;return 'synthetic-secret-key'
        def check():preview.preview_plan(self.plan,now=current[0]);return True
        factory=lambda **kw:FakeClient(calls,FakeResponse())
        narration=ElevenLabsHTTPTransport(key,enabled=True,client_factory=factory)
        with self.assertRaises(ProviderOutcomeUnknown):narration.submit_authorized(plan_requests('Synthetic words.',profile())[0],check)
        image=self.job.snapshot['images'][0];original=self.source.originals[image['id']]
        request=plan_anchored_motion(image_id=image['id'],original_bytes=original['bytes'],content_type='image/png',original_sha256=image['sha256'],prompt=scene_prompt(self.plan['scenes'][0]))
        current[0]=NOW
        with self.assertRaises(ProviderOutcomeUnknown):FalQueueTransport(key,enabled=True,client_factory=factory).submit_authorized(request,check)
        self.assertEqual(calls,[])
    def test_uncertain_source_proposal_and_low_confidence_review_stay_hold(self):
        raw=sources.semantic(self.source.prepared);raw['scenes'][0]['uncertainties']=['Source feature uncertain.']
        base=source_planning.parse_proposals(json.dumps(raw).encode(),evidence=self.source.prepared['evidence'])
        changed={**self.candidate,**base,'candidate_sha256':fingerprint(base)}
        with self.assertRaises(ValueError):self.build(source_candidate=changed)
        record=self.record(0);result=copy.deepcopy(record['review']['native_review']);result['semantic_assessment']['confidence']=.5
        base={k:v for k,v in result.items() if k not in {'operation_id','provider_request_id','automatic_retries','policy_status','review_evidence_sha256'}}
        result['review_evidence_sha256']=fingerprint(base)
        proof={**record['review']['archive_verification'],'review_result_sha256':fingerprint(result)}
        with self.assertRaises(ValueError):preview.make_preview_qc(candidate=record,result=result,plan=self.plan,expected_plan_sha256=fingerprint(self.plan),authorization=self.auth,expected_authorization_sha256=fingerprint(self.auth),job=self.job,preview_release_sha256=RELEASE,archive_verification=proof,now=NOW)
    def test_extension_final_authorization_after_key_read_blocks_post(self):
        from test_motion_continuation_stdlib import inputs as old_inputs
        from byron_video.motion_continuation import plan_continuation,extension_request,VerifiedPriorClip,FalExtensionTransport
        from byron_video.provider_execution import ProviderOutcomeUnknown
        from test_provider_execution_stdlib import FakeClient,FakeResponse
        plan=plan_continuation(**old_inputs());scene=plan['scenes'][0]
        prior=VerifiedPriorClip(plan['owner_id'],plan['job_id'],scene['image_id'],scene['source_sha256'],0,'b'*64,123,10,'fixture_prior_request','https://fal.media/files/prior.mp4','c'*64,True)
        request=extension_request(plan=plan,expected_plan_sha256=fingerprint(plan),image_id=scene['image_id'],sequence=1,prior_clip=prior,now=NOW)
        calls=[];state=[True]
        def key():state[0]=False;return 'fixture-secret'
        transport=FalExtensionTransport(key,enabled=True,client_factory=lambda **kw:FakeClient(calls,FakeResponse()))
        with self.assertRaises(ProviderOutcomeUnknown):transport.submit_authorized(request,lambda:state[0])
        self.assertEqual(calls,[])
    def test_private_authorization_store_rechecks_current_expiry(self):
        clock=[NOW]
        store=preview.FirestorePreviewAuthorizations(self.source.client,project_id=native_tests.PROJECT,namespace='fixture_preview',budget_loader=lambda job:self.budget,preview_release_sha256=RELEASE,now=lambda:clock[0],transaction_runner=self.source.client.run)
        self.assertTrue(store.commit(self.job,self.auth,fingerprint(self.auth)));self.assertFalse(store.commit(self.job,self.auth,fingerprint(self.auth)))
        self.assertEqual(store.load(self.job),(self.auth,fingerprint(self.auth)));clock[0]=2000
        with self.assertRaises(ValueError):store.load(self.job)

if __name__=='__main__':unittest.main()
