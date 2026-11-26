"""Explicit synthetic rate/token approvals; no price discovery or provider calls."""
import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from byron_video.review_budget import *
from byron_video.motion_continuation import plan_continuation,bind_plan_budget
from byron_video import automated_review as review
from byron_video.provider_execution import FileResponseStore,WireResponse,ProviderOutcomeUnknown
from test_motion_continuation_stdlib import inputs,ScopedJournal,NOW
from test_automated_review_stdlib import CONFIG,request_fixture,reply


def quote():
    return dict(reference='fixture_approved_rate',approved=True,config=copy.deepcopy(CONFIG),policy_sha256=fingerprint(candidate_policy()),
        input_usd_per_million='0.275',output_usd_per_million='1.65',max_input_tokens=15000,max_output_tokens=2000,
        output_includes_thought_tokens=True,image_tokens_ceiling_each=1200,text_tokens_ceiling=3000,sample_count=9,source_count=1,
        all_in_usd_ceiling=True,token_limit_contract_reference='fixture_enforced_token_contract',valid_until_epoch=1100,
        source_planning=dict(image_count=0,max_input_tokens=0,max_output_tokens=0,text_tokens_ceiling=0,execution_enabled=False,funded_operation_id=None),
        review_retry_allowance_calls=0,approved_review_cap_cents=100)


class ReviewBudgetChecks(unittest.TestCase):
    def setUp(self):
        self.motion=plan_continuation(**inputs());self.q=quote();self.plan=self.make();self.journal=ScopedJournal(None,self.motion)
        self.job=SimpleNamespace(owner_id=self.motion['owner_id'],id=self.motion['job_id'])
    def make(self,**changes):
        args=dict(motion_plan=self.motion,expected_motion_plan_sha256=fingerprint(self.motion),quote=self.q,now=NOW);args.update(changes)
        return plan_review_budget(**args)
    def bind(self,now=NOW):return bind_review_budget(plan=self.plan,expected_plan_sha256=fingerprint(self.plan),motion_plan=self.motion,journal=self.journal,now=now)
    def test_decimal_rounding_per_operation_not_total(self):
        self.assertEqual(call_ceiling_cents(max_input_tokens=15000,max_output_tokens=2000,input_usd_per_million='.275',output_usd_per_million='1.65'),1)
        self.assertEqual(call_ceiling_cents(max_input_tokens=20000,max_output_tokens=4000,input_usd_per_million='.275',output_usd_per_million='1.65'),2)
        self.assertEqual(self.plan['total_review_ceiling_cents'],14)
    def test_all_10_19_27_segments_not_only_final_clips(self):
        for durations,count in (((10,15,20,25),10),((25,25,30,35),19),((40,40,40,35),27)):
            motion=plan_continuation(**inputs(durations));p=self.make(motion_plan=motion,expected_motion_plan_sha256=fingerprint(motion))
            self.assertEqual(p['segment_count'],count);self.assertEqual(p['total_review_ceiling_cents'],count)
            self.assertEqual(p['frames_per_segment'],9)
    def test_unknown_unapproved_rates_and_5frames_block(self):
        for field,value in [('approved',False),('input_usd_per_million',None),('output_usd_per_million','NaN'),('sample_count',5),('output_includes_thought_tokens',False),('max_input_tokens',14000)]:
            q={**self.q,field:value}
            with self.subTest(field=field),self.assertRaises(ValueError):self.make(quote=q)
    def test_source_planning_and_retry_contingency_declared_and_held(self):
        self.q['source_planning']=dict(image_count=4,max_input_tokens=7800,max_output_tokens=2000,text_tokens_ceiling=3000,execution_enabled=False,funded_operation_id=None)
        self.q['review_retry_allowance_calls']=2;self.plan=self.make();wrapped=self.bind()
        self.assertEqual(self.plan['source_planning_ceiling_cents'],1);self.assertEqual(self.plan['total_review_ceiling_cents'],17)
        self.assertEqual(len(wrapped.allocations),16);self.assertEqual(len(wrapped.allowed),14)
        hold=next(op for op in wrapped.allocations if op.startswith('review_hold_'))
        with self.assertRaises(ValueError):wrapped.get(hold)
    def test_existing_narration_and_motion_share_authoritative_cap(self):
        self.journal.arm('fixture_narration_operation',{'fixture':1},150,200)
        bind_plan_budget(plan=self.motion,expected_plan_sha256=fingerprint(self.motion),journal=self.journal,now=NOW)
        with self.assertRaises(ValueError):self.bind()
        self.assertEqual(self.journal.reserved(),193)
    def test_full_review_reservation_then_motion_cannot_strand_paid_motion(self):
        self.journal.arm('fixture_narration_operation',{'fixture':1},150,200)
        self.bind()
        with self.assertRaises(ValueError):bind_plan_budget(plan=self.motion,expected_plan_sha256=fingerprint(self.motion),journal=self.journal,now=NOW)
        self.assertEqual(self.journal.reserved(),164)
    def test_hold_replay_no_double_charge_and_expired_new_plan_blocks(self):
        self.bind();self.bind(now=2000);self.assertEqual(self.journal.reserved(),14)
        self.journal=ScopedJournal(None,self.motion)
        with self.assertRaises(ValueError):self.bind(now=2000)
    def test_claim_unknown_is_not_retry_and_no_double_count(self):
        wrapped=self.bind();op=self.plan['operations'][0]['operation_id'];wrapped.arm(op,{'request':'fixture'},1,200)
        self.assertEqual(self.journal.reserved(),14);self.assertEqual(wrapped.get(op)['state'],'may_have_submitted')
        with self.assertRaises(ValueError):wrapped.arm(op,{'request':'fixture'},1,200)
    def test_funder_hook_and_changed_scope_rejected(self):
        funder=FilmReviewFunding(quote_loader=lambda job,plan:self.q,enabled=True,now=lambda:NOW)
        result=funder(job=self.job,plan=self.motion,journal=self.journal);self.assertTrue(validate_funding(result,job=self.job,plan=self.motion,journal=self.journal))
        result['owner_id']='other'
        with self.assertRaises(ValueError):validate_funding(result,job=self.job,plan=self.motion,journal=self.journal)
    def test_default_disabled_hook_no_reservation(self):
        with self.assertRaises(ValueError):FilmReviewFunding(quote_loader=lambda j,p:self.q)(job=self.job,plan=self.motion,journal=self.journal)
        self.assertEqual(self.journal.reserved(),0)
    def test_review_once_cached_after_expiry_no_second_charge_or_transport(self):
        funder=FilmReviewFunding(quote_loader=lambda job,plan:self.q,enabled=True,now=lambda:NOW)
        funded=funder(job=self.job,plan=self.motion,journal=self.journal)
        scene=self.motion['scenes'][0];evidence=copy.deepcopy(request_fixture()['body']['evidence']);evidence['source_sha256']=scene['source_sha256']
        request=review.prepare_request(owner_id=self.job.owner_id,job_id=self.job.id,image_id=scene['image_id'],sequence=0,prompt=scene['approved_prompt'],evidence=evidence,config=CONFIG)
        q=funded['quote_maker'](request);calls=[]
        def transmit(req):calls.append(req);return WireResponse('fixture_request_0001',json.dumps(reply(req)).encode())
        with tempfile.TemporaryDirectory() as folder:
            args=dict(request=request,quote=q,trusted_budget=funded['trusted_budget'],journal=funded['journal'],response_store=FileResponseStore(Path(folder)/'archive'),transport=transmit,activation_check=lambda:True,enabled=True)
            first=review.review_once(**args,now=lambda:NOW)
            second=review.review_once(**args,now=lambda:2000)
            self.assertEqual(first,second);self.assertEqual(len(calls),1);self.assertEqual(self.journal.reserved(),14)
            changed=copy.deepcopy(request);changed['body']['binding']['clip_sha256']='f'*64
            with self.assertRaises(ValueError):review.review_once(**{**args,'request':changed},now=lambda:2000)
    def test_vertex_8192_output_token_case_rounds_each_call(self):
        q={**self.q,'max_output_tokens':8192}
        p=self.make(quote=q);self.assertEqual(p['per_call_ceiling_cents'],2)
        motion=plan_continuation(**inputs((40,40,40,35)))
        p=self.make(motion_plan=motion,expected_motion_plan_sha256=fingerprint(motion),quote=q)
        self.assertEqual(p['segment_count'],27);self.assertEqual(p['total_review_ceiling_cents'],54)
    def test_approval_input_mutation_does_not_change_frozen_plan(self):
        before=fingerprint(self.plan);self.q['config']['model']='changed';self.q['source_planning']['image_count']=4
        self.assertEqual(fingerprint(self.plan),before)
    def test_existing_source_planning_is_counted_not_reserved_twice(self):
        op='source_planning_'+hashlib.sha256((self.job.owner_id+'\0'+self.job.id).encode()).hexdigest()
        self.q['source_planning']=dict(image_count=4,max_input_tokens=7800,max_output_tokens=8192,text_tokens_ceiling=3000,execution_enabled=False,funded_operation_id=op)
        self.journal.arm(op,{'native_planner':'fixture'},2,200);self.journal.record(op,{'native_planner':'fixture'},{'request_id':'fixture_planner_request'})
        funded=FilmReviewFunding(quote_loader=lambda j,p:self.q,enabled=True,now=lambda:NOW)(job=self.job,plan=self.motion,journal=self.journal)
        self.assertEqual(funded['review_plan']['total_review_ceiling_cents'],16);self.assertEqual(funded['total_cents'],14)
        self.assertEqual(self.journal.reserved(),16);self.assertTrue(validate_funding(funded,job=self.job,continuation_plan=self.motion,journal=self.journal))
    def test_missing_existing_planner_proof_blocks_discount(self):
        self.q['source_planning']=dict(image_count=4,max_input_tokens=7800,max_output_tokens=8192,text_tokens_ceiling=3000,execution_enabled=False,
            funded_operation_id='source_planning_'+hashlib.sha256((self.job.owner_id+'\0'+self.job.id).encode()).hexdigest())
        self.plan=self.make()
        with self.assertRaises(ValueError):self.bind()
        self.assertEqual(self.journal.reserved(),0)
    def test_funding_validator_rejects_partial_operation_plan(self):
        funded=FilmReviewFunding(quote_loader=lambda j,p:self.q,enabled=True,now=lambda:NOW)(job=self.job,plan=self.motion,journal=self.journal)
        funded['review_plan']['operations'].pop();funded['review_plan_sha256']=fingerprint(funded['review_plan'])
        with self.assertRaises(ValueError):validate_funding(funded,job=self.job,continuation_plan=self.motion,journal=self.journal)
    def test_native_adapter_caps_cannot_exceed_funding(self):
        funded=FilmReviewFunding(quote_loader=lambda j,p:self.q,enabled=True,now=lambda:NOW)(job=self.job,plan=self.motion,journal=self.journal)
        funded['quote_maker']=lambda r:None
        class Adapter:
            max_input_tokens=15000;max_output_tokens=8192
            token_limit_contract_reference='fixture_enforced_token_contract'
            def __call__(self,r):raise AssertionError('Must not transmit beyond funded cap')
        with self.assertRaises(ValueError):FundedReviewTransport(funding=funded,transport=Adapter())({'fixture':'request'})
    def test_funded_native_matching_caps_delegate_once(self):
        self.q['max_output_tokens']=8192
        funded=FilmReviewFunding(quote_loader=lambda j,p:self.q,enabled=True,now=lambda:NOW)(job=self.job,plan=self.motion,journal=self.journal)
        scene=self.motion['scenes'][0];evidence=copy.deepcopy(request_fixture()['body']['evidence']);evidence['source_sha256']=scene['source_sha256']
        request=review.prepare_request(owner_id=self.job.owner_id,job_id=self.job.id,image_id=scene['image_id'],sequence=0,prompt=scene['approved_prompt'],evidence=evidence,config=CONFIG)
        calls=[]
        class Adapter:
            max_input_tokens=15000;max_output_tokens=8192
            token_limit_contract_reference='fixture_enforced_token_contract'
            def __call__(self,r):calls.append(r);return 'synthetic-result'
        self.assertEqual(FundedReviewTransport(funding=funded,transport=Adapter())(request),'synthetic-result');self.assertEqual(len(calls),1)
    def test_funded_executor_claims_once_and_reuses_cached_expired_result(self):
        self.q['max_output_tokens']=8192
        funding=FilmReviewFunding(quote_loader=lambda j,p:self.q,enabled=True,now=lambda:NOW)(job=self.job,plan=self.motion,journal=self.journal)
        scene=self.motion['scenes'][0];evidence=copy.deepcopy(request_fixture()['body']['evidence']);evidence['source_sha256']=scene['source_sha256']
        request=review.prepare_request(owner_id=self.job.owner_id,job_id=self.job.id,image_id=scene['image_id'],sequence=0,prompt=scene['approved_prompt'],evidence=evidence,config=CONFIG)
        calls=[];now=[NOW]
        class Native:
            max_input_tokens=15000;max_output_tokens=8192;token_limit_contract_reference='fixture_enforced_token_contract'
            def __call__(self,r):calls.append(r);return WireResponse('fixture_native_id',json.dumps(reply(r)).encode())
        with tempfile.TemporaryDirectory() as folder:
            executor=FundedReviewExecutor(funding=funding,transport=Native(),response_store=FileResponseStore(Path(folder)/'archive'),activation_check=lambda:True,enabled=True,now=lambda:now[0])
            first=executor(request);now[0]=2000;self.assertEqual(executor(request),first)
        self.assertEqual(len(calls),1);self.assertEqual(self.journal.reserved(),28)
    def test_funding_expiry_during_credentials_blocks_final_native_send(self):
        self.q['max_output_tokens']=8192;now=[NOW];sent=[]
        funded=FilmReviewFunding(quote_loader=lambda j,p:self.q,enabled=True,now=lambda:NOW)(job=self.job,plan=self.motion,journal=self.journal)
        scene=self.motion['scenes'][0];evidence=copy.deepcopy(request_fixture()['body']['evidence']);evidence['source_sha256']=scene['source_sha256']
        request=review.prepare_request(owner_id=self.job.owner_id,job_id=self.job.id,image_id=scene['image_id'],sequence=0,prompt=scene['approved_prompt'],evidence=evidence,config=CONFIG)
        class Native:
            max_input_tokens=15000;max_output_tokens=8192;token_limit_contract_reference='fixture_enforced_token_contract'
            def submit_funded(self,r,submission_check):
                now[0]=2000;submission_check();sent.append(r)
        with self.assertRaises(ValueError):FundedReviewTransport(funding=funded,transport=Native(),now=lambda:now[0])(request)
        self.assertEqual(sent,[])
    def test_per_call_rounding_cannot_exceed_approved_total(self):
        with self.assertRaises(ValueError):self.make(quote={**self.q,'approved_review_cap_cents':13})

if __name__=='__main__':unittest.main()
