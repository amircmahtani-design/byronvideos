"""Fresh funded reviewer integration, native answers/transport synthetic only."""
import copy,hashlib,io,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from byron_video.funded_review import FundedCandidateReview,PrivateReviewEvidence
from byron_video.review_budget import FilmReviewFunding
from byron_video.elevenlabs_adapter import fingerprint
from byron_video.provider_execution import WireResponse,FileResponseStore
from test_motion_continuation_stdlib import inputs,ScopedJournal,NOW
from byron_video.motion_continuation import plan_continuation
from test_review_budget_stdlib import quote
from test_automated_review_stdlib import request_fixture,reply,source_image


class FundedReviewChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.plan=plan_continuation(**inputs());snapshot={'images':inputs()['images'],'input_order':self.plan['input_order']}
        self.job=SimpleNamespace(owner_id=self.plan['owner_id'],id=self.plan['job_id'],snapshot=snapshot,snapshot_sha256=fingerprint(snapshot))
        self.journal=ScopedJournal(None,self.plan);q=quote();q['max_output_tokens']=8192
        self.funding=FilmReviewFunding(quote_loader=lambda j,p:q,enabled=True,now=lambda:NOW)(job=self.job,plan=self.plan,journal=self.journal)
        self.calls=[];self.time=[NOW];self.active=[True];calls=self.calls
        class Native:
            max_input_tokens=15000;max_output_tokens=8192;token_limit_contract_reference='fixture_enforced_token_contract'
            def __call__(self,request):calls.append(request);return WireResponse('fixture_native_review',json.dumps(reply(request)).encode())
        self.native=Native();scene=self.plan['scenes'][0]
        self.candidate=dict(owner_id=self.job.owner_id,job_id=self.job.id,snapshot_sha256=self.job.snapshot_sha256,continuation_plan_sha256=fingerprint(self.plan),
            image_id=scene['image_id'],source_sha256=scene['source_sha256'],sequence=0,full_decode_verified=True,duration_seconds=10,presentation_crop=None,video={'sha256':'a'*64})
        self.service=FundedCandidateReview(journal=self.journal,transport=self.native,response_store=FileResponseStore(self.root/'archive'),evidence_loader=self.evidence,
            activation_check=lambda:self.active[0],enabled=True,now=lambda:self.time[0])
    def tearDown(self):self.temp.cleanup()
    def evidence(self,*,job,image,candidate):
        evidence=copy.deepcopy(request_fixture()['body']['evidence']);evidence['source_sha256']=image['sha256'];evidence['duration_seconds']=10
        for i,sample in enumerate(evidence['samples']):sample['frame_index']=round(i*239/8);sample['time_seconds']=sample['frame_index']/24
        return evidence
    def bind(self):self.service.bind_funding(self.funding,job=self.job,plan=self.plan)
    def test_fresh_candidate_and_expired_cache_never_human_approval_or_double_charge(self):
        self.bind();first=self.service(self.candidate);self.time[0]=2000;second=self.service(self.candidate)
        self.assertEqual(first,second);self.assertEqual(first['state'],'REVIEW_REQUIRED');self.assertFalse(first['human_approved'])
        self.assertEqual(len(self.calls),1);self.assertEqual(self.journal.reserved(),28)
    def test_unfunded_disabled_and_expired_activation_fail_before_transmit(self):
        with self.assertRaises(ValueError):self.service(self.candidate)
        self.bind();self.active[0]=False
        with self.assertRaises(ValueError):self.service(self.candidate)
        self.assertEqual(self.calls,[])
    def test_cross_owner_or_changed_clip_evidence_rejected(self):
        self.bind()
        for change in ({'owner_id':'other'},{'video':{'sha256':'b'*64}},{'sequence':10}):
            with self.assertRaises(ValueError):self.service({**self.candidate,**change})
        self.assertEqual(self.calls,[])
    def test_changed_funding_or_snapshot_rejected(self):
        self.funding['review_plan']['operations'].pop()
        with self.assertRaises(ValueError):self.bind()
        self.assertEqual(self.calls,[])
    def test_concrete_vertex_native_archive_and_funded_service_cached_replay(self):
        import test_vertex_review_stdlib as native_tests
        from byron_video import vertex_review as vertex
        from byron_video.automated_review import prepare_request
        native=native_tests.VertexChecks();native.setUp()
        try:
            data=inputs();data['job_id']='a'*32;plan=plan_continuation(**data)
            job=SimpleNamespace(**{**vars(self.job),'id':plan['job_id']});journal=ScopedJournal(None,plan)
            q=quote();q.update(config=vertex.config(native_tests.PROJECT),max_output_tokens=8192,token_limit_contract_reference=native_tests.CONTRACT)
            funded=FilmReviewFunding(quote_loader=lambda j,p:q,enabled=True,now=lambda:NOW)(job=job,plan=plan,journal=journal)
            candidate={**self.candidate,'job_id':job.id,'continuation_plan_sha256':fingerprint(plan)}
            scene=plan['scenes'][0];evidence=self.evidence(job=job,image=job.snapshot['images'][0],candidate=candidate)
            expected=prepare_request(owner_id=job.owner_id,job_id=job.id,image_id=scene['image_id'],sequence=0,prompt=scene['approved_prompt'],evidence=evidence,config=q['config'])
            native.raw=vertex.encoded(native_tests.native_response(expected));time=[NOW]
            service=FundedCandidateReview(journal=journal,transport=native.transport(),response_store=native.store,evidence_loader=self.evidence,activation_check=lambda:True,enabled=True,now=lambda:time[0])
            service.bind_funding(funded,job=job,plan=plan);first=service(candidate);time[0]=2000;self.assertEqual(service(candidate),first)
            self.assertEqual(first['state'],'REVIEW_REQUIRED');self.assertEqual(len([c for c in native.calls if c[0]=='request']),1)
            self.assertEqual(journal.reserved(),28)
            for archive in native.native_archive.root.glob('*.json'):archive.write_bytes(b'tampered-native-archive')
            with self.assertRaises(ValueError):service(candidate)
            self.assertEqual(len([c for c in native.calls if c[0]=='request']),1)
            unverified=FundedCandidateReview(journal=journal,transport=native.transport(),response_store=FileResponseStore(self.root/'bad-archive'),evidence_loader=self.evidence,activation_check=lambda:True,enabled=True,now=lambda:NOW)
            with self.assertRaises(ValueError):unverified.bind_funding(funded,job=job,plan=plan)
        finally:native.tearDown()
    def test_per_job_vertex_factory_constructs_scoped_services_without_network(self):
        from byron_video.funded_review import build_vertex_review_factory
        from test_cloud_storage_stdlib import FakeBucket
        from test_cloud_journal_stdlib import Client,PROJECT
        bucket=FakeBucket();db=Client();runtime=SimpleNamespace(enabled=True,config={'target':{'project_id':PROJECT,'bucket':'fixture-private-bucket'},'firestore':{'namespace':'fixture_namespace'},'budget':copy.deepcopy(self.plan['budget'])},
            bindings=SimpleNamespace(storage_client=SimpleNamespace(bucket=lambda name:bucket),firestore_client=db,transaction_runner=db.run))
        args=dict(token_reader=lambda:(_ for _ in ()).throw(AssertionError('No token during construction')),input_proof_loader=lambda *a:None,
            max_input_tokens=15000,token_limit_contract_reference='fixture_enforced_token_contract',workspace=self.root,activation_check=lambda:True)
        with self.assertRaises(ValueError):build_vertex_review_factory(**args)(job=self.job,runtime=runtime,journal=self.journal)
        factory=build_vertex_review_factory(**args,enabled=True);first=factory(job=self.job,runtime=runtime,journal=self.journal)
        other=SimpleNamespace(**{**vars(self.job),'id':'b'*32})
        with self.assertRaises(ValueError):factory(job=other,runtime=runtime,journal=self.journal)
        second=factory(job=other,runtime=runtime,journal=ScopedJournal(None,{**self.plan,'job_id':other.id}))
        self.assertIsNot(first,second);self.assertEqual(first.transport.evidence.records.job_id,self.job.id)
        self.assertEqual(second.transport.evidence.records.job_id,other.id);self.assertEqual(bucket.writes,[]);self.assertEqual(db.data,{})
    def test_reader_backed_evidence_copies_exact_immutable_private_generation(self):
        source=source_image();sha=hashlib.sha256(source).hexdigest();image={'id':'fixture_image_1','sha256':sha}
        body=b'synthetic fullclip';prefix=f"owners/{hashlib.sha256(self.job.owner_id.encode()).hexdigest()}/jobs/{self.job.id}/segments/{image['id']}/"
        ref=dict(bucket='fixture-private-bucket',object=prefix+'00.mp4',generation='1',content_type='video/mp4',bytes=len(body),sha256=hashlib.sha256(body).hexdigest())
        candidate={**self.candidate,'source_sha256':sha,'video':ref};reads=[]
        def reader(ref):reads.append(ref);return dict(generation='1',content_type='video/mp4',stream=io.BytesIO(body))
        evidence=PrivateReviewEvidence(original_reader=lambda j,i:source,clip_reader=reader,bucket='fixture-private-bucket',workspace=self.root)
        def sample(**args):self.assertEqual(args['clip_path'].read_bytes(),body);self.assertEqual(args['source_bytes'],source);return {'fixture':'sampled'}
        with patch('byron_video.funded_review.sample_evidence',side_effect=sample):self.assertEqual(evidence(job=self.job,image=image,candidate=candidate),{'fixture':'sampled'})
        self.assertEqual(len(reads),1);self.assertFalse(list(self.root.glob('review-*')))
        candidate['video']={**ref,'object':'other-owner/clip.mp4'}
        with self.assertRaises(ValueError):evidence(job=self.job,image=image,candidate=candidate)

if __name__=='__main__':unittest.main()
