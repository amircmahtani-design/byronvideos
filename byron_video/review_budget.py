"""Finite whole-film review funding; no provider rates, approval or calls implied.

The quote is an independently approved maximum-token contract, not an estimate.
Its input ceiling includes source plus nine frames and all instruction/prompt JSON.
A production gateway must enforce those ceilings, including reasoning output.
"""
from decimal import Decimal,InvalidOperation,ROUND_CEILING,localcontext
import copy
import hashlib
import time
from .elevenlabs_adapter import fingerprint
from .motion_continuation import _checked_plan,_journal_scope,scene_prompt
from .automated_review import candidate_policy,validate_budget_context,validate_request,endpoint_config,need,exact,ID,SHA


def _rate(value):
    need(isinstance(value,str) and 0<len(value)<=40,'Rates must be explicit decimal USD strings, never binary floats.')
    try:rate=Decimal(value)
    except InvalidOperation:raise ValueError('Invalid decimal rate.') from None
    need(rate.is_finite() and 0<rate<=100000,'Positive known rate required; unknown/free guesses are blocked.')
    return rate


def call_ceiling_cents(*,max_input_tokens,max_output_tokens,input_usd_per_million,output_usd_per_million):
    need(type(max_input_tokens) is int and 0<max_input_tokens<=2_000_000
         and type(max_output_tokens) is int and 0<max_output_tokens<=1_000_000,'Explicit bounded per-call token ceilings required.')
    with localcontext() as ctx:
        ctx.prec=80
        usd=(Decimal(max_input_tokens)*_rate(input_usd_per_million)+Decimal(max_output_tokens)*_rate(output_usd_per_million))/Decimal(1000000)
        cents=int((usd*100).to_integral_value(rounding=ROUND_CEILING))
    need(0<cents<=1_000_000_000,'Review cost exceeds budget bounds.')
    return cents


def _operation(owner,job,image,sequence):
    return 'vision_review_'+hashlib.sha256((owner+'\0'+job+'\0'+image+'\0'+str(sequence)).encode()).hexdigest()


def plan_review_budget(*,motion_plan,expected_motion_plan_sha256,quote,now=None):
    """Freeze all intermediate and final reviews, before the next paid phase.

    Rates are USD/million billable tokens. Taxes, regional uplift and gateway fees
    must already be covered by this approved all-in ceiling quote. No current
    provider/model price or approval is inferred from a candidate model name.
    """
    now=time.time() if now is None else now
    quote=copy.deepcopy(quote)
    _checked_plan(motion_plan,expected_motion_plan_sha256,0,False)
    exact(quote,{'reference','approved','config','policy_sha256','input_usd_per_million','output_usd_per_million',
        'max_input_tokens','max_output_tokens','output_includes_thought_tokens','image_tokens_ceiling_each','text_tokens_ceiling',
        'sample_count','source_count','all_in_usd_ceiling','token_limit_contract_reference','valid_until_epoch',
        'source_planning','review_retry_allowance_calls','approved_review_cap_cents'},'Complete independently approved review quote required.')
    policy=candidate_policy();config=endpoint_config(quote['config'])
    need(quote['approved'] is True and quote['all_in_usd_ceiling'] is True and quote['output_includes_thought_tokens'] is True
         and isinstance(quote['reference'],str) and ID.fullmatch(quote['reference'])
         and isinstance(quote['token_limit_contract_reference'],str) and ID.fullmatch(quote['token_limit_contract_reference']),
         'Unapproved quote or unenforced token/cost contract blocks review.')
    need(quote['policy_sha256']==fingerprint(policy) and quote['sample_count']==policy['sample_count']==9 and quote['source_count']==1,
         'Current review policy is source plus nine frames, not five or final-clips only.')
    need(type(quote['image_tokens_ceiling_each']) is int and 0<quote['image_tokens_ceiling_each']<=100000
         and type(quote['text_tokens_ceiling']) is int and 0<quote['text_tokens_ceiling']<=1_000_000
         and quote['max_input_tokens']>=10*quote['image_tokens_ceiling_each']+quote['text_tokens_ceiling'],
         'Input cap must cover ten images and the complete text/schema/prompt ceiling.')
    need(type(now) in (int,float) and type(quote['valid_until_epoch']) in (int,float)
         and now<quote['valid_until_epoch']<=now+86400,'A fresh bounded review quote is required to create a plan.')
    per_call=call_ceiling_cents(max_input_tokens=quote['max_input_tokens'],max_output_tokens=quote['max_output_tokens'],
        input_usd_per_million=quote['input_usd_per_million'],output_usd_per_million=quote['output_usd_per_million'])
    operations=[]
    for scene in motion_plan['scenes']:
        for segment in scene['segments']:
            operations.append(dict(operation_id=_operation(motion_plan['owner_id'],motion_plan['job_id'],scene['image_id'],segment['sequence']),
                image_id=scene['image_id'],source_sha256=scene['source_sha256'],sequence=segment['sequence'],
                prompt_sha256=hashlib.sha256(scene_prompt(scene).encode()).hexdigest(),reservation_cents=per_call))
    need(4<=len(operations)<=55 and len({x['operation_id'] for x in operations})==len(operations),'Finite unique complete review operations required.')
    planning=quote['source_planning']
    exact(planning,{'image_count','max_input_tokens','max_output_tokens','text_tokens_ceiling','execution_enabled','funded_operation_id'},'Source-planning cost declaration is mandatory, including explicit zero.')
    need(planning['execution_enabled'] is False,'This funding module never executes source planning; its separate stage must supply an existing operation proof.')
    need(type(planning['image_count']) is int and planning['image_count'] in (0,len(motion_plan['scenes'])), 'Source planning must cover every original painting or declare zero.')
    expected_planning_operation='source_planning_'+hashlib.sha256((motion_plan['owner_id']+'\0'+motion_plan['job_id']).encode()).hexdigest()
    need(planning['funded_operation_id'] in (None,expected_planning_operation),'Source-planning operation differs from exact owner/job.')
    if planning['image_count']==0:
        need(planning['funded_operation_id'] is None,'Zero planning cannot reference paid operation.')
        need(all(type(planning[k]) is int and planning[k]==0 for k in ('max_input_tokens','max_output_tokens','text_tokens_ceiling')),'Disabled source-planning cost must be explicit zero.')
        planning_cents=0
    else:
        need(type(planning['text_tokens_ceiling']) is int and planning['text_tokens_ceiling']>0
             and planning['max_input_tokens']>=planning['image_count']*quote['image_tokens_ceiling_each']+planning['text_tokens_ceiling'], 'Source-planning input cap omits images/text.')
        planning_cents=call_ceiling_cents(max_input_tokens=planning['max_input_tokens'],max_output_tokens=planning['max_output_tokens'],input_usd_per_million=quote['input_usd_per_million'],output_usd_per_million=quote['output_usd_per_million'])
    need(type(quote['review_retry_allowance_calls']) is int and 0<=quote['review_retry_allowance_calls']<=len(operations),'Finite explicit review retry contingency, including zero, required.')
    contingency=quote['review_retry_allowance_calls']*per_call
    total=per_call*len(operations)+planning_cents+contingency
    need(type(quote['approved_review_cap_cents']) is int and total<=quote['approved_review_cap_cents']<=motion_plan['budget']['job_cap_cents'], 'Whole-review budget exceeds its independently approved cap.')
    result=dict(schema_version=1,status='DRAFT_NOT_ACTIVATED',owner_id=motion_plan['owner_id'],job_id=motion_plan['job_id'],
        motion_plan_sha256=expected_motion_plan_sha256,quote=quote,config=config,policy_sha256=fingerprint(policy),
        segment_count=len(operations),frames_per_segment=9,source_images_per_segment=1,operations=operations,
        per_call_ceiling_cents=per_call,total_review_ceiling_cents=total,source_planning_ceiling_cents=planning_cents,review_retry_contingency_cents=contingency,
        new_reservation_cents=total-(planning_cents if planning['funded_operation_id'] else 0),source_planning_operation_id=planning['funded_operation_id'],
        motion_ceiling_cents=motion_plan['reserved_plan_ceiling_cents'],budget=copy.deepcopy(motion_plan['budget']),automatic_retries=0)
    need(result['total_review_ceiling_cents']+result['motion_ceiling_cents']<=motion_plan['budget']['job_cap_cents'],
         'Motion plus review exceeds the film cap before existing narration; do not submit.')
    return result


class ReviewAllocationJournal:
    """review_once-compatible claims, no double charging or fresh retry identity."""
    def __init__(self,journal,plan):
        self.journal=journal;self.plan=copy.deepcopy(plan);self.plan_sha256=fingerprint(plan)
        self.plan_id='review_film_'+hashlib.sha256((plan['owner_id']+'\0'+plan['job_id']).encode()).hexdigest()
        self.allowed={x['operation_id']:x['reservation_cents'] for x in plan['operations']}
        self.allocations=dict(self.allowed)
        for key in ('source_planning_ceiling_cents','review_retry_contingency_cents'):
            if key=='source_planning_ceiling_cents' and plan['source_planning_operation_id']:continue
            if plan[key]:self.allocations['review_hold_'+hashlib.sha256((self.plan_id+'\0'+key).encode()).hexdigest()]=plan[key]
        for name in ('owner_id','job_id','scope_hash','total_cap','prior_spend'):setattr(self,name,getattr(journal,name))
    def get(self,operation):
        need(operation in self.allowed,'Unallocated review operation.')
        value=self.journal.get_reserved(operation,self.plan_id,self.plan_sha256)
        return None if value['state']=='reserved' else value
    def arm(self,operation,request,reservation_cents,job_cap_cents):
        need(self.allowed.get(operation)==reservation_cents and job_cap_cents==self.plan['budget']['job_cap_cents'],'Review claim changed its funded cap.')
        self.journal.claim_reserved(operation,request,reservation_cents,job_cap_cents,self.plan_id,self.plan_sha256)
    def record(self,*args):return self.journal.record(*args)


def bind_review_budget(*,plan,expected_plan_sha256,motion_plan,journal,now=None):
    """Atomically hold every review against existing narration/motion/shared spend.

    Call before any new paid motion. Existing funded plans can be loaded after
    expiry; new claims still require review_once's current quote gate.
    """
    need(fingerprint(plan)==expected_plan_sha256,'Review plan changed.')
    # Reconstruct shape/cost deterministically using a time inside original validity.
    rebuilt=plan_review_budget(motion_plan=motion_plan,expected_motion_plan_sha256=plan['motion_plan_sha256'],quote=plan['quote'],now=plan['quote']['valid_until_epoch']-1)
    need(rebuilt==plan,'Review allocation plan differs from exact approved contract.')
    _journal_scope(journal,motion_plan)
    if plan['source_planning_operation_id']:
        planning_entry=journal.get(plan['source_planning_operation_id'])
        need(planning_entry is not None and planning_entry['state']=='received' and planning_entry['reservation_cents']==plan['source_planning_ceiling_cents'], 'Source planning must have exact received shared-ledger cost, not an unproven discount.')
    wrapped=ReviewAllocationJournal(journal,plan)
    existing=journal.get_plan_reservation(wrapped.plan_id,expected_plan_sha256)
    now=time.time() if now is None else now
    if existing is None:need(now<plan['quote']['valid_until_epoch']<=now+86400,'Fresh review quote required before whole-film reservation.')
    journal.reserve_plan(wrapped.plan_id,expected_plan_sha256,wrapped.allocations,plan['budget']['job_cap_cents'])
    return wrapped


def quote_for_request(*,plan,expected_plan_sha256,request,trusted_budget):
    """Adapt frozen allocation to review_once quote, including replay after expiry.

    No transport call occurs. The gateway's independently approved token contract
    must enforce the maximums; this function cannot make a gateway honor them.
    """
    need(fingerprint(plan)==expected_plan_sha256,'Review plan changed.')
    validate_request(request);binding=request['body']['binding']
    matches=[op for op in plan['operations'] if op['image_id']==binding['image_id'] and op['sequence']==binding['sequence']]
    need(len(matches)==1,'Request not allocated in whole film.')
    operation=matches[0]
    need(binding['owner_id']==plan['owner_id'] and binding['job_id']==plan['job_id']
         and binding['source_sha256']==operation['source_sha256'] and binding['prompt_sha256']==operation['prompt_sha256']
         and binding['policy_sha256']==plan['policy_sha256'] and binding['model']==plan['config']['model']
         and request['url']==plan['config']['endpoint'],'Review request changed funded scope/model/endpoint/policy.')
    budget=plan['budget']
    expected=dict(scope_sha256=hashlib.sha256(budget['scope'].encode()).hexdigest(),approved_total_cap_cents=budget['approved_total_cap_cents'],
                  prior_spend_cents=budget['prior_spend_cents'],job_cap_cents=budget['job_cap_cents'])
    need(trusted_budget==expected,'Trusted campaign/film budget differs from funded review.')
    return dict(reference=plan['quote']['reference'],request_sha256=fingerprint(request),budget_sha256=fingerprint(trusted_budget),
        reservation_cents=operation['reservation_cents'],job_cap_cents=budget['job_cap_cents'],operation_cap_cents=operation['reservation_cents'],
        valid_until_epoch=plan['quote']['valid_until_epoch'])


class FilmReviewFunding:
    """Coordinator hook; trusted quote loader must retain the ORIGINAL approval.

    Returning a new/extended quote on recovery conflicts with the immutable hold.
    No provider calls or cached-result guessing occur in this funder.
    """
    def __init__(self,*,quote_loader,enabled=False,now=None):
        self.quote_loader=quote_loader;self.enabled=enabled;self.now=now or time.time
    def __call__(self,*,job,plan,journal):
        need(self.enabled is True,'Whole-film review funding is disabled.')
        need(job.owner_id==plan['owner_id'] and job.id==plan['job_id'],'Funding owner/job changed.')
        quote=self.quote_loader(job,plan)
        need(isinstance(quote,dict),'Independent approved review quote missing.')
        review_plan=plan_review_budget(motion_plan=plan,expected_motion_plan_sha256=fingerprint(plan),quote=quote,
                                      now=quote['valid_until_epoch']-1)
        digest=fingerprint(review_plan)
        wrapped=bind_review_budget(plan=review_plan,expected_plan_sha256=digest,motion_plan=plan,journal=journal,now=self.now())
        budget=plan['budget']
        trusted=dict(scope_sha256=hashlib.sha256(budget['scope'].encode()).hexdigest(),approved_total_cap_cents=budget['approved_total_cap_cents'],
                     prior_spend_cents=budget['prior_spend_cents'],job_cap_cents=budget['job_cap_cents'])
        def make_quote(request):return quote_for_request(plan=review_plan,expected_plan_sha256=digest,request=request,trusted_budget=trusted)
        return dict(state='FUNDED',owner_id=job.owner_id,job_id=job.id,continuation_plan_sha256=fingerprint(plan),review_plan_sha256=digest,
            review_plan_id=wrapped.plan_id,total_cents=review_plan['new_reservation_cents'],source_planning_operation=journal.get(review_plan['source_planning_operation_id']) if review_plan['source_planning_operation_id'] else None,review_plan=review_plan,allocation_evidence=journal.get_plan_reservation(wrapped.plan_id,digest),
            journal=wrapped,quote_maker=make_quote,trusted_budget=trusted)


def validate_funding(result,*,job,plan=None,continuation_plan=None,journal):
    plan=continuation_plan if continuation_plan is not None else plan
    need(isinstance(result,dict) and result.get('state')=='FUNDED'
         and (result.get('owner_id'),result.get('job_id'),result.get('continuation_plan_sha256'))==
             (job.owner_id,job.id,fingerprint(plan)) and fingerprint(result.get('review_plan'))==result.get('review_plan_sha256'),
         'Funding summary changed owner/job/continuation plan.')
    review_plan=result['review_plan'];quote=review_plan['quote']
    rebuilt=plan_review_budget(motion_plan=plan,expected_motion_plan_sha256=fingerprint(plan),quote=quote,now=quote['valid_until_epoch']-1)
    need(rebuilt==review_plan,'Funding summary does not cover exact complete film and declared allowances.')
    _journal_scope(journal,plan)
    wrapped=ReviewAllocationJournal(journal,review_plan)
    actual=journal.get_plan_reservation(wrapped.plan_id,result['review_plan_sha256'])
    from .worker_binding import canonical
    need(actual is not None and actual==result.get('allocation_evidence') and actual['total_cents']==sum(wrapped.allocations.values())==result.get('total_cents')
         and actual['allocations_json']==canonical(wrapped.allocations).decode() and actual['owner_id']==job.owner_id and actual['job_id']==job.id
         and actual['job_cap_cents']==plan['budget']['job_cap_cents'] and result.get('review_plan_id')==wrapped.plan_id,
         'Whole-film review funding is not authoritatively reserved with complete exact allocations.')
    if review_plan['source_planning_operation_id']:
        entry=journal.get(review_plan['source_planning_operation_id'])
        need(entry is not None and entry==result.get('source_planning_operation') and entry['state']=='received'
             and entry['reservation_cents']==review_plan['source_planning_ceiling_cents'],'Existing source-planning cost proof changed.')
    return True


class FundedReviewTransport:
    """Require the concrete native adapter's enforced caps to fit this funding.

    This wrapper does not implement token counting or pretend arbitrary gateways
    enforce caps. Only a separately reviewed native adapter may expose these
    limits; its exact-request input proof remains mandatory.
    """
    def __init__(self,*,funding,transport,now=None,activation_check=None):
        self.funding=funding;self.transport=transport;self.now=now or time.time;self.activation_check=activation_check
    def __call__(self,request):
        plan=self.funding['review_plan'];need(fingerprint(plan)==self.funding['review_plan_sha256'],'Funded token contract changed.')
        self.funding['quote_maker'](request)
        need(getattr(self.transport,'token_limit_contract_reference',None)==plan['quote']['token_limit_contract_reference'], 'Native token proof differs from funded contract reference.')
        for key in ('max_input_tokens','max_output_tokens'):
            actual=getattr(self.transport,key,None)
            need(type(actual) is int and 0<actual<=plan['quote'][key],'Native adapter token ceiling exceeds independently funded quote.')
        def final_check():
            if self.activation_check is not None:need(self.activation_check() is True,'Preview/native review authorization expired before final POST.')
            need(fingerprint(plan)==self.funding['review_plan_sha256'],'Funded token contract changed before transmission.')
            quote=self.funding['quote_maker'](request)
            need(self.now()<quote['valid_until_epoch'],'Review price approval expired before native transmission.')
            for key in ('max_input_tokens','max_output_tokens'):
                need(getattr(self.transport,key,None)<=plan['quote'][key],'Native token cap changed before transmission.')
            need(getattr(self.transport,'token_limit_contract_reference',None)==plan['quote']['token_limit_contract_reference'],'Native token contract changed before transmission.')
            return True
        submit=getattr(self.transport,'submit_funded',None)
        if callable(submit):return submit(request,final_check)
        return self.transport(request)


class FundedReviewExecutor:
    """Concrete fresh/cached review_once bridge using only pre-funded claims.

    Caller constructs/samples the exact logical request locally. This executor
    cannot approve content; it returns the existing unapproved candidate result.
    """
    def __init__(self,*,funding,transport,response_store,activation_check,enabled=False,now=None):
        from .vertex_review import VertexReviewTransport,VertexVerifiedReviewResponseStore
        if isinstance(transport,VertexReviewTransport):
            need(isinstance(response_store,VertexVerifiedReviewResponseStore) and response_store.evidence is transport.evidence, 'Native Vertex review requires the same verified native evidence store on fresh output and replay.')
        self.funding=funding;self.transport=FundedReviewTransport(funding=funding,transport=transport,now=now,activation_check=activation_check)
        self.responses=response_store;self.activation_check=activation_check;self.enabled=enabled;self.now=now
    def __call__(self,request):
        from .automated_review import review_once
        need(self.enabled is True,'Funded review execution is disabled.')
        return review_once(request=request,quote=self.funding['quote_maker'](request),trusted_budget=self.funding['trusted_budget'],
            journal=self.funding['journal'],response_store=self.responses,transport=self.transport,
            activation_check=self.activation_check,enabled=True,now=self.now)
