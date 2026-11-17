"""Bounded receipt-schema continuation candidate. Disabled; no implicit requests.

The planner reuses exact narration timing and existing source-supported motion
policy. Extension always consumes the prior FULL decoded clip, never a stack of
partial prefixes. This module does not grant provider/disclosure/spend approval.
"""
from dataclasses import dataclass, asdict
import hashlib
import json
import math
from pathlib import Path
import re
import time

from .elevenlabs_adapter import fingerprint
from . import motion_planning as historical_motion
from . import motion_originals as original_motion


def validate_motion_plan(plan,images,text_sha,timing):
    # Support historical fixtures without weakening either independently locked policy.
    candidate=original_motion.load_policy()
    if plan.get('policy_sha256')==candidate['source_sha256']:
        return original_motion.validate_plan(plan,images,text_sha,timing)
    return historical_motion.validate_plan(plan,images,text_sha,timing)

from .provider_execution import (
    FalQueueTransport, FAL_QUEUE_ORIGIN, FAL_MAX_MEDIA_BYTES, FAL_HEADERS,
    ProviderOutcomeUnknown, _fal_media_url, REQUEST_ID, _wall_deadline,
)

EXTENSION_ENDPOINT='minimax/h3-max-turbo/extend-video'
IMAGE_ENDPOINT='minimax/h3-max-turbo/image-to-video'
ID=re.compile(r'[A-Za-z0-9_-]{8,128}\Z')
SHA=re.compile(r'[0-9a-f]{64}\Z')
MAX_EXTENSIONS_PER_SCENE=10


def require(ok,message):
    if not ok:raise ValueError(message)


def _identifier(value):
    return isinstance(value,str) and bool(ID.fullmatch(value))


def _sha(value):
    return isinstance(value,str) and bool(SHA.fullmatch(value))


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _quote(quote,kind,now):
    endpoint,duration=(IMAGE_ENDPOINT,10) if kind=='image' else (EXTENSION_ENDPOINT,5)
    require(isinstance(quote,dict) and set(quote)=={'kind','endpoint','duration_seconds','resolution','max_cost_cents','valid_until_epoch','reference'},
            'An explicit complete provider quote is required; unknown cost blocks generation.')
    require(quote['kind']==kind and quote['endpoint']==endpoint and quote['duration_seconds']==duration
            and quote['resolution']=='768P' and type(quote['max_cost_cents']) is int and quote['max_cost_cents']>0
            and _number(quote['valid_until_epoch']) and quote['valid_until_epoch']>now and _identifier(quote['reference']),
            'A current independently reviewed quote for this exact endpoint/duration is required.')
    return json.loads(json.dumps(quote))


def plan_continuation(*,owner_id,job_id,images,motion_plan,timing,narration_report,expected_narration_report_sha256,
                      approved_prompts,image_quote,extension_quote,budget,now=None):
    """Preflight complete 4–5 scene coverage, finite extensions, prompts and cost.

    `images`, timing, report fingerprint and approved_prompts must be independently
    loaded trusted records, not browser-supplied approval claims. Each prompt is
    exactly the existing source-supported motion prompt; no new story action is
    inferred. An explicit allowance is held for possible separately approved
    retries but this candidate never executes a retry.
    """
    now=time.time() if now is None else now
    require(_number(now) and _identifier(owner_id) and _identifier(job_id),'Verified owner/job and current time required.')
    require(isinstance(images,list) and len(images) in (4,5),'Continuation requires the same four or five ordered paintings.')
    require(_sha(expected_narration_report_sha256) and fingerprint(narration_report)==expected_narration_report_sha256,
            'Narration differs from its independently recorded media report.')
    require(narration_report.get('provider')=='elevenlabs' and narration_report.get('media_verified') is True
            and narration_report.get('provider_receipts_verified') is True
            and narration_report.get('status')=='DRAFT_RELEASE_CANDIDATE',
            'Decoded exact ElevenLabs narration provenance is required before motion planning.')
    text=narration_report.get('exact_text');require(isinstance(text,str) and bool(text),'Exact narration is required.')
    text_sha=hashlib.sha256(text.encode()).hexdigest()
    checked=validate_motion_plan(motion_plan,images,text_sha,timing)
    require(timing['audio_sha256']==narration_report.get('audio_sha256')
            and timing['word_timing_sha256']==fingerprint(narration_report.get('words'))
            and abs(timing['duration_seconds']-narration_report.get('duration_seconds',-1))<1e-6,
            'Scene coverage must bind decoded audio, exact words and actual narration duration.')
    # Existing policy covers full narrator duration including 0.7-second overlaps.
    require(len(approved_prompts)==len(images),'Every scene requires its independently approved prompt.')
    image_quote=_quote(image_quote,'image',now);extension_quote=_quote(extension_quote,'extend',now)
    require(image_quote['reference']!=extension_quote['reference'],'Extension quote must be distinct from the image-generation quote.')
    require(isinstance(budget,dict) and set(budget)=={'scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents','retry_allowance_cents','approval_reference'},
            'Explicit shared campaign budget with its prior spending is required.')
    require(_identifier(budget['scope']) and _identifier(budget['approval_reference'])
            and all(type(budget[k]) is int for k in ('approved_total_cap_cents','prior_spend_cents','job_cap_cents','retry_allowance_cents'))
            and 0<=budget['prior_spend_cents']<budget['approved_total_cap_cents']
            and 0<budget['job_cap_cents']<=budget['approved_total_cap_cents']
            and 0<=budget['retry_allowance_cents']<=budget['job_cap_cents'], 'Invalid fixed budget or prior-spending seed.')
    scenes=[]
    for image,scene,approval in zip(images,checked['scenes'],approved_prompts):
        require(isinstance(approval,dict) and set(approval)=={'image_id','source_sha256','scene_sha256','prompt','reference','approved'},
                'Complete independently approved source-bound prompt required.')
        require(approval['approved'] is True and approval['image_id']==image['id'] and approval['source_sha256']==image['sha256']
                and approval['scene_sha256']==fingerprint(scene) and approval['prompt']==scene['prompt'] and _identifier(approval['reference']),
                'Prompt approval must match the source-supported scene exactly; no new story actions.')
        required=scene['duration_seconds']
        count=max(0,math.ceil((required-10-1e-9)/5))
        require(count<=MAX_EXTENSIONS_PER_SCENE and 10+5*count<=60,'Scene exceeds the finite 60-second continuation ceiling.')
        segments=[{'sequence':0,'kind':'image','duration_seconds':10,'expected_full_duration_seconds':10,
                   'max_cost_cents':image_quote['max_cost_cents']}]
        for index in range(1,count+1):
            segments.append({'sequence':index,'kind':'extend','duration_seconds':5,'expected_prior_duration_seconds':10+5*(index-1),
                             'expected_full_duration_seconds':10+5*index,'max_cost_cents':extension_quote['max_cost_cents']})
        scenes.append({'image_id':image['id'],'position':image['position'],'source_sha256':image['sha256'],
                       'required_seconds':required,'approved_prompt':approval['prompt'],'prompt_approval_reference':approval['reference'],
                       'segments':segments,'assembly':{'use_only_final_full_clip':True,'trim_start_seconds':0,'trim_duration_seconds':required,
                                                       'provider_audio':'mute','loop':False,'freeze':False,'retime':False}})
    quote_total=sum(segment['max_cost_cents'] for scene in scenes for segment in scene['segments'])
    total=quote_total+budget['retry_allowance_cents']
    require(total<=budget['job_cap_cents'] and total+budget['prior_spend_cents']<=budget['approved_total_cap_cents'],
            'Quoted image/extension costs plus explicit retry allowance exceed the approved remaining budget.')
    result={'schema_version':1,'status':'DRAFT_NOT_ACTIVATED','owner_id':owner_id,'job_id':job_id,
            'narration_report_sha256':expected_narration_report_sha256,'narration_text_sha256':text_sha,
            'timing_plan_sha256':fingerprint(timing),'motion_plan_sha256':fingerprint(checked),
            'duration_seconds':timing['duration_seconds'],'crossfade_seconds':timing['crossfade_seconds'],
            'input_order':[i['id'] for i in images],'scenes':scenes,'image_quote':image_quote,'extension_quote':extension_quote,
            'budget':json.loads(json.dumps(budget)),'quoted_generation_cents':quote_total,'reserved_plan_ceiling_cents':total,
            'automatic_retries':0,'max_extensions_per_scene':MAX_EXTENSIONS_PER_SCENE,'live_release':False}
    result['sha256']=fingerprint(result)
    return result


def scene_prompt(scene):
    if 'preview_prompt' in scene:
        require('approved_prompt' not in scene and _sha(scene.get('preview_authorization_sha256')),'Distinct preview prompt provenance required.')
        return scene['preview_prompt']
    return scene['approved_prompt']


def _checked_plan(plan,expected_sha256,now,require_current_quotes=True):
    require(isinstance(plan,dict) and plan.get('status')=='DRAFT_NOT_ACTIVATED' and plan.get('live_release') is False
            and _sha(expected_sha256) and fingerprint(plan)==expected_sha256
            and plan.get('sha256')==fingerprint({k:v for k,v in plan.items() if k!='sha256'}),
            'Continuation plan differs from the independently frozen job.')
    if plan.get('preview_schema'):
        from .private_preview import preview_plan
        preview_plan(plan,now=now,require_current=require_current_quotes)
    quote_time=now if require_current_quotes else 0
    _quote(plan['image_quote'],'image',quote_time);_quote(plan['extension_quote'],'extend',quote_time)
    return plan


@dataclass(frozen=True)
class VerifiedPriorClip:
    """Output of independently trusted full-decode verifier, never browser JSON."""
    owner_id: str
    job_id: str
    image_id: str
    source_sha256: str
    sequence: int
    clip_sha256: str
    byte_count: int
    duration_seconds: float
    provider_request_id: str
    provider_url: str
    metadata_sha256: str
    full_decode_verified: bool


def verify_prior_clip(*,private_root,record,expected_record_sha256):
    """Decode the exact full clip bound to an independently stored record.

    The stored record must derive from private provider receipt/download staging;
    a caller cannot establish ownership by providing an email or arbitrary URL.
    """
    from .portrait_composition import probe
    require(isinstance(record,dict) and set(record)=={'owner_id','job_id','image_id','source_sha256','sequence','path','clip_sha256',
            'byte_count','duration_seconds','provider_request_id','provider_url','presentation_crop'}, 'Complete prior-clip record required.')
    require(_sha(expected_record_sha256) and fingerprint(record)==expected_record_sha256,'Prior clip metadata changed after durable verification.')
    require(all(_identifier(record[k]) for k in ('owner_id','job_id','image_id')) and _sha(record['source_sha256'])
            and _sha(record['clip_sha256']) and type(record['sequence']) is int and 0<=record['sequence']<=MAX_EXTENSIONS_PER_SCENE
            and type(record['byte_count']) is int and 0<record['byte_count']<=FAL_MAX_MEDIA_BYTES
            and _number(record['duration_seconds']) and 1.625<=record['duration_seconds']<=60
            and isinstance(record['provider_request_id'],str) and REQUEST_ID.fullmatch(record['provider_request_id'])
            and _fal_media_url(record['provider_url']), 'Invalid prior clip owner, duration, provider or immutable bytes.')
    root=Path(private_root)
    path=Path(record['path'])
    require(root.is_absolute() and path.is_absolute() and not any(p.is_symlink() for p in (path,*path.parents)), 'Private non-symlink prior clip required.')
    require(path.resolve().is_relative_to(root.resolve()) and path.is_file() and path.suffix.lower()=='.mp4','Clip escaped the private workspace.')
    require(path.stat().st_size==record['byte_count'] and hashlib.sha256(path.read_bytes()).hexdigest()==record['clip_sha256'],'Prior full-clip bytes changed.')
    actual=probe(path,video=True,presentation_crop=record['presentation_crop'])
    require(abs(actual['duration']-record['duration_seconds'])<=1/24,'Prior clip duration differs from the independently decoded receipt.')
    return VerifiedPriorClip(record['owner_id'],record['job_id'],record['image_id'],record['source_sha256'],record['sequence'],
        record['clip_sha256'],record['byte_count'],actual['duration'],record['provider_request_id'],record['provider_url'],expected_record_sha256,True)


def extension_request(*,plan,expected_plan_sha256,image_id,sequence,prior_clip,now=None,enforce_current_quote=True):
    now=time.time() if now is None else now;_checked_plan(plan,expected_plan_sha256,now,enforce_current_quote)
    scene=next((s for s in plan['scenes'] if s['image_id']==image_id),None)
    require(scene is not None and type(sequence) is int and 1<=sequence<len(scene['segments']),'Only a preplanned finite extension may execute.')
    segment=scene['segments'][sequence]
    require(isinstance(prior_clip,VerifiedPriorClip) and prior_clip.full_decode_verified is True
            and (prior_clip.owner_id,prior_clip.job_id,prior_clip.image_id,prior_clip.source_sha256,prior_clip.sequence)==
                (plan['owner_id'],plan['job_id'],image_id,scene['source_sha256'],sequence-1)
            and _sha(prior_clip.clip_sha256) and _sha(prior_clip.metadata_sha256)
            and type(prior_clip.byte_count) is int and 0<prior_clip.byte_count<=FAL_MAX_MEDIA_BYTES
            and _number(prior_clip.duration_seconds) and 1.625<=prior_clip.duration_seconds<=55
            and abs(prior_clip.duration_seconds-segment['expected_prior_duration_seconds'])<=1/24
            and _fal_media_url(prior_clip.provider_url), 'Extension must use the decoded prior FULL clip from the same owner/source/sequence.')
    body={'video_url':prior_clip.provider_url,'prompt':scene_prompt(scene),'duration':5,'resolution':'768P',
          'enable_safety_checker':True,'enable_prompt_expansion':False,'aspect_ratio':'9:16','output':'extended'}
    result={'kind':'extend','endpoint':EXTENSION_ENDPOINT,'owner_id':plan['owner_id'],'job_id':plan['job_id'],
            'image_id':image_id,'sequence':sequence,'plan_sha256':expected_plan_sha256,'prior_clip':asdict(prior_clip),
            'body':body,'max_cost_cents':segment['max_cost_cents'],'quote_reference':plan['extension_quote']['reference']}
    result['sha256']=fingerprint(result)
    return result


def _extension_wire(request):
    require(isinstance(request,dict) and set(request)=={'kind','endpoint','owner_id','job_id','image_id','sequence','plan_sha256',
        'prior_clip','body','max_cost_cents','quote_reference','sha256'} and request['kind']=='extend' and request['endpoint']==EXTENSION_ENDPOINT
        and request['sha256']==fingerprint({k:v for k,v in request.items() if k!='sha256'}),'Extension request changed.')
    body=request['body'];prior=request['prior_clip']
    require(isinstance(body,dict) and set(body)=={'video_url','prompt','duration','resolution','enable_safety_checker','enable_prompt_expansion','aspect_ratio','output'}
            and _fal_media_url(body['video_url']) and isinstance(body['prompt'],str) and 0<len(body['prompt'].strip())<=2000
            and not any(ord(c)<32 and c not in '\n\r\t' for c in body['prompt'])
            and body['duration']==5 and body['resolution']=='768P' and body['enable_safety_checker'] is True
            and body['enable_prompt_expansion'] is False and body['aspect_ratio']=='9:16' and body['output']=='extended',
            'Only the recovered five-second FULL extended-output schema is supported.')
    require(isinstance(prior,dict) and prior.get('full_decode_verified') is True
            and prior.get('provider_url')==body['video_url'] and _sha(prior.get('clip_sha256'))
            and _sha(prior.get('metadata_sha256')) and _number(prior.get('duration_seconds'))
            and 1.625<=prior['duration_seconds']<=55 and type(prior.get('byte_count')) is int and 0<prior['byte_count']<=FAL_MAX_MEDIA_BYTES
            and all(_identifier(request[k]) for k in ('owner_id','job_id','image_id','quote_reference'))
            and _sha(request['plan_sha256']) and type(request['sequence']) is int and 1<=request['sequence']<=MAX_EXTENSIONS_PER_SCENE
            and (prior.get('owner_id'),prior.get('job_id'),prior.get('image_id'),prior.get('sequence'))==
                (request['owner_id'],request['job_id'],request['image_id'],request['sequence']-1)
            and type(request['max_cost_cents']) is int and request['max_cost_cents']>0,
            'Independent decoded prior clip and current bounded quote are required.')
    return request


def validate_extension_handle(handle):
    require(isinstance(handle,dict) and set(handle)=={'endpoint','request_id','status_url','response_url'}
            and handle['endpoint']==EXTENSION_ENDPOINT and isinstance(handle['request_id'],str)
            and bool(REQUEST_ID.fullmatch(handle['request_id'])),'Invalid extension queue receipt.')
    base=FAL_QUEUE_ORIGIN+'/minimax/h3-max-turbo/requests/'+handle['request_id']
    require(handle['response_url']==base and handle['status_url']==base+'/status','Untrusted extension queue handle.')
    return dict(handle)


class FalExtensionTransport(FalQueueTransport):
    """Separate disabled transport; existing anchored-image whitelist unchanged."""
    def submit(self,request):
        return self._submit(request,None)
    def submit_authorized(self,request,submission_check):
        require(callable(submission_check),'Final extension authorization required.')
        return self._submit(request,submission_check)
    def _submit(self,request,submission_check):
        request=_extension_wire(request)
        if not self._enabled:
            raise RuntimeError('Extension transport is disabled; explicit reviewed activation is required.')
        try:
            factory=self._factory
            if factory is None:
                import httpx
                factory=httpx.Client
            key=self._key_reader()
            require(isinstance(key,str) and 8<=len(key)<=512 and key.isascii() and not any(c.isspace() for c in key),'Invalid key.')
            with factory(timeout=10,follow_redirects=False,trust_env=False) as client, _wall_deadline(client,60,self._clock) as check_deadline:
                if submission_check is not None:require(submission_check() is True,'Final extension authorization unavailable.')
                with client.stream('POST',FAL_QUEUE_ORIGIN+'/'+EXTENSION_ENDPOINT,
                    headers={**FAL_HEADERS,'Authorization':'Key '+key,'Accept':'application/json','Accept-Encoding':'identity'},json=request['body']) as response:
                    check_deadline()
                    require(response.status_code in {200,202},'Unverified provider response.')
                    require(response.headers.get('content-encoding','identity').lower() in {'identity',''},'Encoded streaming response is not accepted.')
                    length=response.headers.get('content-length')
                    require(length is None or length.isdigit() and int(length)<=1024*1024,'Provider response too large.')
                    data=bytearray()
                    for chunk in response.iter_bytes():
                        check_deadline()
                        require(len(data)+len(chunk)<=1024*1024,'Provider response too large.')
                        data.extend(chunk)
                    check_deadline()
                    value=json.loads(data,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
                    handle={'endpoint':EXTENSION_ENDPOINT,**{k:value[k] for k in ('request_id','status_url','response_url')}}
                    return validate_extension_handle(handle)
        except Exception:
            raise ProviderOutcomeUnknown('Extension outcome is unknown. Keep the reservation; do not resubmit.') from None

    def status(self,handle):
        handle=validate_extension_handle(handle)
        value=self._request(handle['status_url'],'GET')
        require(value.get('request_id')==handle['request_id'] and value.get('status') in {'IN_QUEUE','IN_PROGRESS','COMPLETED'},'Unverified extension status.')
        return value['status']

    def result(self,handle):
        handle=validate_extension_handle(handle)
        value=self._request(handle['response_url'],'GET');video=value.get('video')
        require(isinstance(video,dict) and _fal_media_url(video.get('url')) and type(video.get('file_size')) is int
                and 0<video['file_size']<=FAL_MAX_MEDIA_BYTES and video.get('content_type') in {'video/mp4','application/octet-stream'},
                'Unverified extension media receipt.')
        return {'url':video['url'],'bytes':video['file_size'],'request_id':handle['request_id'],'media_verified':False}


def _journal_scope(journal,plan):
    budget=plan['budget']
    require(getattr(journal,'owner_id',None)==plan['owner_id'] and getattr(journal,'job_id',None)==plan['job_id']
            and getattr(journal,'scope_hash',None)==hashlib.sha256(budget['scope'].encode()).hexdigest()
            and getattr(journal,'total_cap',None)==budget['approved_total_cap_cents']
            and getattr(journal,'prior_spend',None)==budget['prior_spend_cents'],
            'A matching durable shared-budget journal is required; never reset prior spending with a fresh job ledger.')


class PlanReservationJournal:
    """Adapt funded allocations to existing once-only operation semantics."""
    def __init__(self,journal,plan,plan_sha256):
        self.journal=journal;self.plan=plan;self.plan_sha256=plan_sha256
        self.plan_id='continuation_'+hashlib.sha256((plan['owner_id']+'\0'+plan['job_id']).encode()).hexdigest()
        for key in ('owner_id','job_id','scope_hash','total_cap','prior_spend'):
            setattr(self,key,getattr(journal,key))
    def allocation(self,operation_id):
        return self.journal.get_reserved(operation_id,self.plan_id,self.plan_sha256)
    def get(self,operation_id):
        value=self.allocation(operation_id)
        return None if value['state']=='reserved' else value
    def arm(self,operation_id,request,reservation_cents,job_cap_cents):
        return self.journal.claim_reserved(operation_id,request,reservation_cents,job_cap_cents,self.plan_id,self.plan_sha256)
    def record(self,*args):return self.journal.record(*args)


def bind_plan_budget(*,plan,expected_plan_sha256,journal,now=None):
    """Atomically hold the COMPLETE plan before the first generation request.

    Journal transaction includes existing narration/job and other campaign funds.
    Claims use these allocations without charging them a second time.
    """
    now=time.time() if now is None else now;_checked_plan(plan,expected_plan_sha256,now,False);_journal_scope(journal,plan)
    if isinstance(journal,PlanReservationJournal):
        require(journal.plan_sha256==expected_plan_sha256,'Different reserved plan wrapper.')
        return journal
    require(all(callable(getattr(journal,name,None)) for name in ('reserve_plan','claim_reserved','get_reserved','get_plan_reservation')),
            'Atomic complete-plan budget reservation is required before any segment.')
    wrapped=PlanReservationJournal(journal,plan,expected_plan_sha256)
    allocations={}
    for scene in plan['scenes']:
        for segment in scene['segments']:
            prefix='motion_' if segment['sequence']==0 else 'extension_'
            identity=plan['owner_id']+'\0'+plan['job_id']+'\0'+scene['image_id']
            if segment['sequence']>0:identity+='\0'+str(segment['sequence'])
            allocations[prefix+hashlib.sha256(identity.encode()).hexdigest()]=segment['max_cost_cents']
    allowance=plan['budget']['retry_allowance_cents']
    if allowance:
        op='motion_allowance_'+hashlib.sha256((plan['owner_id']+'\0'+plan['job_id']).encode()).hexdigest()
        allocations[op]=allowance
    require(sum(allocations.values())==plan['reserved_plan_ceiling_cents'],'Complete plan reservation differs from its quote.')
    if journal.get_plan_reservation(wrapped.plan_id,expected_plan_sha256) is None:
        _checked_plan(plan,expected_plan_sha256,now)
    journal.reserve_plan(wrapped.plan_id,expected_plan_sha256,allocations,plan['budget']['job_cap_cents'])
    return wrapped


def reserve_retry_allowance(*,plan,expected_plan_sha256,journal,now=None):
    """Return the explicit reserved contingency; never claim or spend it."""
    wrapped=bind_plan_budget(plan=plan,expected_plan_sha256=expected_plan_sha256,journal=journal,now=now)
    amount=plan['budget']['retry_allowance_cents']
    if amount==0:return {'reserved_cents':0,'automatic_retries':0}
    op='motion_allowance_'+hashlib.sha256((plan['owner_id']+'\0'+plan['job_id']).encode()).hexdigest()
    allocation=wrapped.allocation(op)
    require(allocation['state']=='reserved' and allocation['reservation_cents']==amount and allocation.get('receipt') is None,
            'Retry contingency is not an untouched reserved hold.')
    return {'operation_id':op,'reserved_cents':amount,'state':'reserved','automatic_retries':0}


def submit_extension_once(*,plan,expected_plan_sha256,image_id,sequence,prior_clip,journal,transport,now=None):
    """One explicit segment only. No polling loop, paid retry or new story prompt.

    Caller must first execute/reconcile preceding segments and independently decode
    their full output. A queue handle alone cannot authorize the next extension.
    Image generation and narration must use this same campaign/job budget scope.
    """
    now=time.time() if now is None else now
    request=extension_request(plan=plan,expected_plan_sha256=expected_plan_sha256,image_id=image_id,sequence=sequence,prior_clip=prior_clip,now=now,enforce_current_quote=False)
    journal=bind_plan_budget(plan=plan,expected_plan_sha256=expected_plan_sha256,journal=journal,now=now)
    op='extension_'+hashlib.sha256((plan['owner_id']+'\0'+plan['job_id']+'\0'+image_id+'\0'+str(sequence)).encode()).hexdigest()
    binding={'request':request,'plan_sha256':expected_plan_sha256,'budget_scope_sha256':journal.scope_hash,
             'budget_approval_reference':plan['budget']['approval_reference'],'automatic_retries':0}
    existing=journal.get(op)
    if existing is not None:
        require(existing['plan_sha256']==fingerprint(binding) and existing['reservation_cents']==request['max_cost_cents'],
                'The immutable extension operation changed; do not repeat it.')
        if existing['state']!='received':raise ProviderOutcomeUnknown('This extension may already have been submitted. Reconcile; do not retry.')
        return validate_extension_handle(existing['receipt'])
    _checked_plan(plan,expected_plan_sha256,now)
    reserve_retry_allowance(plan=plan,expected_plan_sha256=expected_plan_sha256,journal=journal,now=now)
    journal.arm(op,binding,request['max_cost_cents'],plan['budget']['job_cap_cents'])
    try:
        receipt=validate_extension_handle(_preview_transport(transport,plan,expected_plan_sha256).submit(request))
        journal.record(op,binding,receipt)
        return receipt
    except Exception:
        raise ProviderOutcomeUnknown('Extension result is not durably confirmed. Funds stay reserved; no automatic retry.') from None


def select_final_clips(*,plan,expected_plan_sha256,verified_clips,now=None):
    """Return final FULL clip references only, never concatenate extension prefixes."""
    now=time.time() if now is None else now;_checked_plan(plan,expected_plan_sha256,now,False)
    require(isinstance(verified_clips,list) and len(verified_clips)==len(plan['scenes']),'Every ordered scene needs one final full clip.')
    result=[]
    for scene,clip in zip(plan['scenes'],verified_clips):
        sequence=len(scene['segments'])-1;expected=scene['segments'][-1]['expected_full_duration_seconds']
        require(isinstance(clip,VerifiedPriorClip) and clip.full_decode_verified is True
                and (clip.owner_id,clip.job_id,clip.image_id,clip.source_sha256,clip.sequence)==
                    (plan['owner_id'],plan['job_id'],scene['image_id'],scene['source_sha256'],sequence)
                and _sha(clip.clip_sha256) and _sha(clip.metadata_sha256) and _number(clip.duration_seconds)
                and scene['required_seconds']<=clip.duration_seconds<=60
                and abs(clip.duration_seconds-expected)<=1/24,
                'Final scene must use its verified full extension at original playback speed; no loop/freeze/retime.')
        result.append({'image_id':scene['image_id'],'position':scene['position'],'clip_sha256':clip.clip_sha256,
                       'metadata_sha256':clip.metadata_sha256,'trim_start_seconds':0,'trim_duration_seconds':scene['required_seconds'],
                       'source_sha256':clip.source_sha256,'sequence':sequence,'provider_audio':'mute'})
    return result


def submit_initial_image_once(*,plan,expected_plan_sha256,image_id,request,journal,transport,now=None):
    """Bridge the approved base-image quote into existing once-only image transport.

    Holds the explicit retry allowance before the first image POST as well as
    before extensions, so a later segment cannot silently omit the contingency.
    Narration and all image/extension operations must share this same journal cap.
    """
    from .provider_execution import _motion_request,SpendApproval,submit_motion_once
    now=time.time() if now is None else now
    journal=bind_plan_budget(plan=plan,expected_plan_sha256=expected_plan_sha256,journal=journal,now=now)
    request=_motion_request(request)
    scene=next((s for s in plan['scenes'] if s['image_id']==image_id),None)
    require(scene is not None and request['image_id']==image_id and request['original_sha256']==scene['source_sha256']
            and request['body']['prompt']==scene_prompt(scene),'Initial image differs from the approved original and source-supported prompt.')
    op='motion_'+hashlib.sha256((plan['owner_id']+'\0'+plan['job_id']+'\0'+image_id).encode()).hexdigest()
    if journal.get(op) is None:
        _checked_plan(plan,expected_plan_sha256,now)
        reserve_retry_allowance(plan=plan,expected_plan_sha256=expected_plan_sha256,journal=journal,now=now)
    cost=plan['image_quote']['max_cost_cents']
    reference='approval_'+hashlib.sha256((plan['budget']['approval_reference']+expected_plan_sha256).encode()).hexdigest()
    approval=SpendApproval(reference,fingerprint([request]),(cost,),plan['budget']['job_cap_cents'],cost)
    return submit_motion_once(owner_id=plan['owner_id'],job_id=plan['job_id'],request=request,approval=approval,journal=journal,transport=_preview_transport(transport,plan,expected_plan_sha256))


def _preview_transport(transport,plan,expected_plan_sha256):
    if not plan.get('preview_schema'):return transport
    class Checked:
        def submit(self,request):
            require(callable(getattr(transport,'submit_authorized',None)),'Private preview requires final-post authorization-aware motion transport.')
            def check():
                _checked_plan(plan,expected_plan_sha256,time.time(),True)
                return True
            return transport.submit_authorized(request,check)
    return Checked()
