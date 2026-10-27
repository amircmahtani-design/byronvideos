"""Explicitly constructed provider worker bridge; no implicit network activation.

Preserves the existing narration planner and parser. No fallback voice, endpoint,
retry, price lookup or credential discovery. Reservation ceilings are supplied by
an authorized server-side quote/policy, not computed from unverified pricing.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import time
import threading
from contextlib import contextmanager
from .elevenlabs_adapter import (
    MAX_RESPONSE_BYTES, MODEL_LIMITS, fingerprint, parse_response, plan_requests, validate_profile,
)

ID = re.compile(r'[A-Za-z0-9_-]{8,128}\Z')
REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{8,160}\Z')


@contextmanager
def _wall_deadline(client, seconds, clock):
    """Stop slow-trickle responses; socket inactivity timeouts alone are not total."""
    deadline=clock()+seconds
    expired=threading.Event()
    def abort():
        expired.set()
        try:
            close=getattr(client,'close',None)
            if callable(close):close()
        except Exception:pass
    timer=threading.Timer(seconds,abort);timer.daemon=True;timer.start()
    def check():
        if expired.is_set() or clock()>=deadline:raise TimeoutError('Provider wall-clock deadline exceeded.')
    try:
        check();yield check;check()
    finally:timer.cancel()


class ProviderOutcomeUnknown(RuntimeError):
    """Reservation remains consumed; reconcile, never automatically submit again."""


@dataclass(frozen=True)
class WireResponse:
    request_id: str
    payload: bytes = field(repr=False)


@dataclass(frozen=True)
class SpendApproval:
    """Trusted per-film ceiling, explicitly bound to the exact narration plan.

    This is not billing authorization by itself. The injected server policy must
    verify authorization and bound reservations using a reviewed provider quote.
    No caller-provided browser number or live price assumption belongs here.
    """
    reference: str
    plan_sha256: str
    reservations_cents: tuple[int, ...]
    job_cap_cents: int
    operation_cap_cents: int

    def validate(self, requests):
        if (not isinstance(self.reference, str) or not ID.fullmatch(self.reference)
                or self.plan_sha256 != fingerprint(requests)
                or not isinstance(self.reservations_cents, tuple)
                or len(self.reservations_cents) != len(requests)
                or type(self.job_cap_cents) is not int or type(self.operation_cap_cents) is not int
                or not 0 < self.operation_cap_cents <= self.job_cap_cents
                or any(type(x) is not int or not 0 < x <= self.operation_cap_cents for x in self.reservations_cents)
                or sum(self.reservations_cents) > self.job_cap_cents):
            raise ValueError('An explicit exact-plan bounded spend approval is required.')


def _wire_request(request):
    """Defensive endpoint/body validation even if transport is called directly."""
    if not isinstance(request, dict) or set(request) != {'method','url','body','char_start','char_end','profile_sha256','sequence','sha256'}:
        raise ValueError('Use the complete frozen narration request.')
    raw = {k:v for k,v in request.items() if k != 'sha256'}
    if fingerprint(raw) != request['sha256']:
        raise ValueError('Narration request changed.')
    if (request['method'] != 'POST' or not isinstance(request['url'],str)
            or not re.fullmatch(r'https://api\.elevenlabs\.io/v1/text-to-speech/[A-Za-z0-9_-]{8,80}/with-timestamps\?output_format=mp3_44100_128',request['url'])):
        raise ValueError('Only the pinned ElevenLabs narration endpoint is permitted.')
    body = request['body']
    required = {'text','model_id','voice_settings','seed','apply_text_normalization','apply_language_text_normalization'}
    if (not isinstance(body,dict) or not required <= set(body) or not set(body) <= required | {'previous_text','next_text'}
            or body.get('model_id') not in MODEL_LIMITS or not isinstance(body.get('text'),str)
            or not 0 < len(body['text']) <= MODEL_LIMITS[body['model_id']]
            or body.get('apply_text_normalization') != 'off' or body.get('apply_language_text_normalization') is not False):
        raise ValueError('Use the exact preplanned narration body.')
    voice_id = request['url'].split('/text-to-speech/',1)[1].split('/',1)[0]
    validate_profile({'schema_version':1,'version':'transport-validation','status':'DRAFT','provider':'elevenlabs',
        'voice_id':voice_id,'model_id':body['model_id'],'voice_settings':body['voice_settings'],
        'output_format':'mp3_44100_128','seed':body['seed'],'apply_text_normalization':'off'})
    if (any(not isinstance(body[k],str) or len(body[k]) > 1000 for k in ('previous_text','next_text') if k in body)
            or type(request['sequence']) is not int or request['sequence'] < 1
            or type(request['char_start']) is not int or request['char_start'] < 0
            or type(request['char_end']) is not int or request['char_end'] != request['char_start']+len(body['text'])):
        raise ValueError('Narration span/context changed.')
    return json.loads(json.dumps(request,allow_nan=False))


class ElevenLabsHTTPTransport:
    """Bounded non-retrying HTTP implementation; disabled unless explicitly armed.

    client_factory is injectable for deterministic offline tests. The default
    lazily imports httpx only when invoked. No ambient proxies or redirects are
    accepted, and no key is retained on this object. An SDK/client import failure
    prevents transmission. A timeout/error after invocation is always unknown.
    """
    def __init__(self, key_reader, *, enabled=False, client_factory=None, timeout_seconds=90, clock=time.monotonic):
        if type(timeout_seconds) not in (int,float) or not 1 <= timeout_seconds <= 180:
            raise ValueError('A bounded provider timeout is required.')
        self._key_reader = key_reader
        self._enabled = enabled is True
        self._factory = client_factory
        self._timeout = timeout_seconds
        self._clock = clock

    def __call__(self, request):
        return self._submit(request,None)

    def submit_authorized(self,request,submission_check):
        if not callable(submission_check):raise ValueError('Final narration authorization required.')
        return self._submit(request,submission_check)

    def _submit(self,request,submission_check):
        request = _wire_request(request)
        if not self._enabled:
            raise RuntimeError('Provider transport is disabled; explicit reviewed activation is required.')
        try:
            factory = self._factory
            if factory is None:
                import httpx
                factory = httpx.Client
            key = self._key_reader()
            if not isinstance(key,str) or not 16 <= len(key) <= 512 or not key.isascii() or any(c.isspace() for c in key):
                raise ValueError()
            with factory(timeout=min(10,self._timeout), follow_redirects=False, trust_env=False) as client, _wall_deadline(client,self._timeout,self._clock) as check_deadline:
                if submission_check is not None and submission_check() is not True:raise ValueError('Final narration authorization unavailable.')
                with client.stream('POST',request['url'],headers={'xi-api-key':key,'Accept':'application/json','Accept-Encoding':'identity'},json=request['body']) as response:
                    # No automatic retry, even for 429/5xx or ambiguous redirect.
                    if response.headers.get('content-encoding','identity').lower() != 'identity':raise ValueError()
                    if response.status_code != 200:
                        raise ValueError()
                    request_id = response.headers.get('request-id')
                    if not isinstance(request_id,str) or not REQUEST_ID.fullmatch(request_id):
                        raise ValueError()
                    length = response.headers.get('content-length')
                    if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                        raise ValueError()
                    payload = bytearray()
                    for chunk in response.iter_bytes():
                        check_deadline()
                        if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ValueError()
                        payload.extend(chunk)
                    if not payload:
                        raise ValueError()
                    return WireResponse(request_id,bytes(payload))
        except Exception:
            raise ProviderOutcomeUnknown('Provider outcome could not be verified. Keep the reservation and reconcile; do not retry.') from None


class FileResponseStore:
    """Private persistent-worker response archive, not a cloud filesystem claim.

    Cloud workers must inject an immutable generation-bound private object store
    with the same write_response/read_response methods. Raw provider JSON is
    retained before receipt reconciliation; exclusive creation never overwrites.
    """
    def __init__(self, root):
        self.root = Path(root)
        if not self.root.is_absolute() or any(x.is_symlink() for x in (self.root,*self.root.parents)):
            raise ValueError('An absolute non-symlink private response directory is required.')
        public = Path(__file__).resolve().parent.parent / 'web'
        if self.root.resolve().is_relative_to(public):
            raise ValueError('Provider responses must not be stored publicly.')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)

    def _path(self, operation_id):
        if not isinstance(operation_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',operation_id):
            raise ValueError('Invalid operation reference.')
        return self.root / (operation_id + '.json')

    def write_response(self, operation_id, response):
        if (not isinstance(response,WireResponse) or not REQUEST_ID.fullmatch(response.request_id)
                or not isinstance(response.payload,bytes) or not 0 < len(response.payload) <= MAX_RESPONSE_BYTES):
            raise ValueError('A bounded provider response is required.')
        path = self._path(operation_id)
        descriptor = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(descriptor,'wb') as destination:
            destination.write(response.payload)
            destination.flush()
            os.fsync(destination.fileno())
        directory = os.open(self.root,os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {'request_id':response.request_id,'response_ref':operation_id,
                'response_sha256':hashlib.sha256(response.payload).hexdigest(),'response_bytes':len(response.payload)}

    def read_response(self, receipt):
        path = self._path(receipt['response_ref'])
        if path.is_symlink():
            raise ValueError('Response symlinks are forbidden.')
        with path.open('rb') as source:
            payload = source.read(MAX_RESPONSE_BYTES+1)
        if (len(payload) != receipt['response_bytes'] or len(payload) > MAX_RESPONSE_BYTES
                or hashlib.sha256(payload).hexdigest() != receipt['response_sha256']):
            raise ValueError('Stored response bytes changed.')
        return WireResponse(receipt['request_id'],payload)


def _narration_operations(owner_id, job_id, requests, approval):
    if any(not isinstance(x,str) or not ID.fullmatch(x) for x in (owner_id,job_id)):
        raise ValueError('Verified owner and job references are required.')
    approval.validate(requests)
    scope = hashlib.sha256((owner_id+'\0'+job_id).encode()).hexdigest()
    operations = []
    for request,reservation in zip(requests,approval.reservations_cents):
        request = _wire_request(request)
        operation_id = 'narration_' + hashlib.sha256((scope+':'+str(request['sequence'])).encode()).hexdigest()
        binding = {'owner_job_sha256':scope,'request':request,'approval_ref':approval.reference,
                   'approved_plan_sha256':approval.plan_sha256,'reservation_cents':reservation,
                   'job_cap_cents':approval.job_cap_cents,'operation_cap_cents':approval.operation_cap_cents}
        operations.append((operation_id,binding,request,reservation))
    return operations


def load_recorded_narration(*, owner_id, job_id, text, profile, approval, journal, response_store):
    """Read and verify durable receipts only; cannot reserve or submit anything."""
    requests = plan_requests(text,profile)
    operations = _narration_operations(owner_id,job_id,requests,approval)
    responses,proofs = [],[]
    for op,binding,request,reservation in operations:
        entry = journal.get(op)
        if (entry is None or entry.get('state') != 'received' or entry.get('plan_sha256') != fingerprint(binding)
                or entry.get('reservation_cents') != reservation or not isinstance(entry.get('receipt'),dict)):
            raise ValueError('Every exact narration span needs an independently recorded durable provider receipt.')
        receipt = entry['receipt']
        wire = response_store.read_response(receipt)
        if (wire.request_id != receipt.get('request_id') or hashlib.sha256(wire.payload).hexdigest() != receipt.get('response_sha256')
                or len(wire.payload) != receipt.get('response_bytes')):
            raise ValueError('Archived provider response does not match durable provenance.')
        parsed = parse_response(request['body']['text'],wire.payload,wire.request_id)
        if (parsed['audio_sha256'] != receipt.get('audio_sha256')
                or hashlib.sha256(parsed['exact_text'].encode()).hexdigest() != receipt.get('exact_text_sha256')
                or fingerprint(parsed['words']) != receipt.get('words_sha256')):
            raise ValueError('Narration receipt text, alignment or audio changed.')
        responses.append(parsed)
        proofs.append({'operation_id':op,'request_id':wire.request_id,'receipt_sha256':fingerprint(receipt)})
    return {'requests':requests,'responses':responses,'receipts':proofs,
            'owner_job_sha256':hashlib.sha256((owner_id+'\0'+job_id).encode()).hexdigest(),
            'plan_sha256':fingerprint(requests),'profile_sha256':fingerprint(profile),
            'exact_text_sha256':hashlib.sha256(text.encode()).hexdigest()}


def execute_narration_once(*, owner_id, job_id, text, profile, approval, journal, transport, response_store):
    """Execute each exact span at most once after durable reservation.

    Re-entry never calls transport for an existing operation. Completed receipts
    can be read back and revalidated; unknown operations block the whole plan.
    The journal MUST be scoped to this owner+job and preserve an immutable cap,
    unique operation/plan identities and reserve-before-transmission semantics.
    Metadata returned here does NOT establish decoded media quality or release.
    """
    requests = plan_requests(text,profile)
    operations = _narration_operations(owner_id,job_id,requests,approval)
    # Preflight ALL prior entries before transmitting any remaining span.
    existing = {}
    for op,binding,request,reservation in operations:
        entry = journal.get(op)
        if entry is not None:
            if entry['plan_sha256'] != fingerprint(binding) or entry['reservation_cents'] != reservation:
                raise ValueError('The frozen provider operation changed; nothing was submitted.')
            if entry['state'] != 'received' or not isinstance(entry.get('receipt'),dict):
                raise ProviderOutcomeUnknown('A provider operation may already have been submitted. Reconcile it; never retry.')
            existing[op] = entry['receipt']
    results = []
    for op,binding,request,reservation in operations:
        if op in existing:
            receipt = existing[op]
            wire = response_store.read_response(receipt)
            parsed = parse_response(request['body']['text'],wire.payload,wire.request_id)
            if (parsed['audio_sha256'] != receipt['audio_sha256']
                    or hashlib.sha256(parsed['exact_text'].encode()).hexdigest() != receipt['exact_text_sha256']
                    or fingerprint(parsed['words']) != receipt['words_sha256']):
                raise ValueError('Archived provider audio changed.')
        else:
            journal.arm(op,binding,reservation,approval.job_cap_cents)
            try:
                wire = transport(request)  # Exactly one call. Any exception retains reservation.
                receipt = response_store.write_response(op,wire)
                parsed = parse_response(request['body']['text'],wire.payload,wire.request_id)
                if (receipt.get('request_id') != wire.request_id or receipt.get('response_sha256') != hashlib.sha256(wire.payload).hexdigest()
                        or receipt.get('response_bytes') != len(wire.payload)):
                    raise ValueError('Response store receipt changed.')
                # Verify durable read-back before marking a provider operation received.
                stored = response_store.read_response(receipt)
                if stored != wire:
                    raise ValueError('Stored provider response differs.')
                receipt = {**receipt,'audio_sha256':parsed['audio_sha256'],
                           'exact_text_sha256':hashlib.sha256(parsed['exact_text'].encode()).hexdigest(),
                           'words_sha256':fingerprint(parsed['words']),'media_verified':False}
                journal.record(op,binding,receipt)
            except Exception:
                raise ProviderOutcomeUnknown('Provider result is not durably verified. Keep the reservation and reconcile; do not retry.') from None
        results.append(parsed)
    return {'requests':requests,'responses':results,'plan_sha256':fingerprint(requests),
            'reservation_cents':sum(approval.reservations_cents),'media_verified':False,'live_release':False}

# Receipt-derived refinement schema; not an approved general film preset.
FAL_IMAGE_ENDPOINT = 'minimax/h3-max-turbo/image-to-video'
FAL_QUEUE_ORIGIN = 'https://queue.fal.run'
FAL_MAX_MEDIA_BYTES = 50 * 1024 * 1024
FAL_HEADERS = {'X-Fal-No-Retry':'1','x-app-fal-disable-fallback':'true','X-Fal-Store-IO':'0',
               'X-Fal-Object-Lifecycle-Preference':'{"expiration_duration_seconds":7200}'}


def plan_anchored_motion(*, image_id, original_bytes, content_type, original_sha256, prompt):
    """One exact 10-second original-first/original-last anchored candidate.

    Only generic schema/settings come from recovered evidence; no private input,
    provider URL, prompt or request ID is embedded. No crop, inferred prompt,
    vision service, extension, loop, retime or provider audio is requested.
    """
    import base64
    from .hosted_uploads import prepare_image
    if (not isinstance(image_id,str) or not ID.fullmatch(image_id) or not isinstance(original_bytes,bytes)
            or len(original_bytes)>4*1024*1024 or hashlib.sha256(original_bytes).hexdigest()!=original_sha256
            or not isinstance(prompt,str) or not prompt.strip() or len(prompt)>2000
            or any(ord(c)<32 and c not in '\n\r\t' for c in prompt)):
        raise ValueError('Explicit verified original bytes and approved bounded prompt are required.')
    prepare_image(original_bytes,content_type)
    anchor='data:'+content_type+';base64,'+base64.b64encode(original_bytes).decode()
    value={'kind':'anchored_image','status':'DRAFT_NOT_ACTIVATED','endpoint':FAL_IMAGE_ENDPOINT,
           'image_id':image_id,'original_sha256':original_sha256,
           'body':{'prompt':prompt,'duration':10,'resolution':'768P','enable_safety_checker':True,
                   'prompt_expansion_mode':'disabled','image_url':anchor,'end_image_url':anchor}}
    value['sha256']=fingerprint(value)
    return value


def _motion_request(value):
    import base64
    if (not isinstance(value,dict) or set(value)!={'kind','status','endpoint','image_id','original_sha256','body','sha256'}
            or value['kind']!='anchored_image' or value['status']!='DRAFT_NOT_ACTIVATED'
            or value['endpoint']!=FAL_IMAGE_ENDPOINT
            or fingerprint({k:v for k,v in value.items() if k!='sha256'})!=value['sha256']):
        raise ValueError('Only the frozen receipt-derived anchored motion plan is supported.')
    body=value['body']
    if (not isinstance(body,dict) or set(body)!={'prompt','duration','resolution','enable_safety_checker','prompt_expansion_mode','image_url','end_image_url'}
            or body['image_url']!=body['end_image_url'] or not isinstance(body['image_url'],str)
            or len(body['image_url'])>6*1024*1024):
        raise ValueError('Both anchors must use the exact same verified original.')
    match=re.fullmatch(r'data:(image/(?:png|jpeg));base64,([A-Za-z0-9+/]+={0,2})',body['image_url'])
    if not match:
        raise ValueError('Inline original image bytes are required; no provider URL substitution.')
    try:
        image=base64.b64decode(match[2],validate=True)
        expected=plan_anchored_motion(image_id=value['image_id'],original_bytes=image,content_type=match[1],
            original_sha256=value['original_sha256'],prompt=body['prompt'])
    except Exception:
        raise ValueError('Invalid anchored motion request.') from None
    if fingerprint(expected)!=fingerprint(value):
        raise ValueError('Anchored motion settings changed.')
    return expected


def validate_fal_handle(handle):
    if (not isinstance(handle,dict) or set(handle)!={'endpoint','request_id','response_url','status_url'}
            or handle['endpoint']!=FAL_IMAGE_ENDPOINT or not isinstance(handle['request_id'],str)
            or not REQUEST_ID.fullmatch(handle['request_id'])):
        raise ValueError('Invalid queue receipt.')
    base=FAL_QUEUE_ORIGIN+'/minimax/h3-max-turbo/requests/'+handle['request_id']
    if handle['response_url']!=base or handle['status_url']!=base+'/status':
        raise ValueError('Untrusted queue status/result URL.')
    return dict(handle)


def _fal_media_url(value):
    from urllib.parse import urlsplit
    try:
        u=urlsplit(value)
        return (isinstance(value,str) and len(value)<=4096 and u.scheme=='https' and not u.username
                and not u.password and not u.port and not u.fragment
                and (u.hostname=='fal.media' or re.fullmatch(r'[a-z0-9-]+\.fal\.media',u.hostname or ''))
                and u.path.startswith('/files/') and '..' not in u.path)
    except (ValueError,TypeError):
        return False


class FalQueueTransport:
    """Pinned queue submit/status/result and unauthenticated CDN download.

    Default disabled; no auto retries, fallback, redirects, environment proxy,
    key logging or ambient credentials. Payload-retention headers are requests,
    not a guarantee of zero provider retention.
    """
    def __init__(self,key_reader,*,enabled=False,client_factory=None,clock=time.monotonic):
        self._key_reader,self._enabled,self._factory=key_reader,enabled is True,client_factory
        self._clock=clock

    def _request(self,url,method,body=None,*,media=False,submission_check=None):
        if not self._enabled:
            raise RuntimeError('fal transport is disabled; reviewed activation is required.')
        if media:
            if method!='GET' or not _fal_media_url(url):raise ValueError('Invalid media URL.')
        elif not (url==FAL_QUEUE_ORIGIN+'/'+FAL_IMAGE_ENDPOINT and method=='POST' or
                  method=='GET' and re.fullmatch(re.escape(FAL_QUEUE_ORIGIN)+r'/minimax/h3-max-turbo/requests/[A-Za-z0-9_-]{8,160}(?:/status)?',url)):
            raise ValueError('Untrusted queue endpoint.')
        try:
            factory=self._factory
            if factory is None:
                import httpx
                factory=httpx.Client
            headers={'Accept-Encoding':'identity'}
            if not media:
                key=self._key_reader()
                if not isinstance(key,str) or not 8<=len(key)<=512 or not key.isascii() or any(c.isspace() for c in key):raise ValueError()
                headers={**headers,**FAL_HEADERS,'Authorization':'Key '+key,'Accept':'application/json'}
            limit=FAL_MAX_MEDIA_BYTES if media else 1024*1024
            with factory(timeout=10,follow_redirects=False,trust_env=False) as client, _wall_deadline(client,120 if media else 60,self._clock) as check_deadline:
                options={'headers':headers}
                if body is not None:options['json']=body
                if method=='POST' and submission_check is not None:
                    if not callable(submission_check) or submission_check() is not True:raise ValueError('Final motion authorization unavailable.')
                with client.stream(method,url,**options) as result:
                    if result.headers.get('content-encoding','identity').lower() != 'identity':raise ValueError()
                    if result.status_code not in ({200} if media or method=='GET' else {200,202}):raise ValueError()
                    length=result.headers.get('content-length')
                    if length is not None and (not length.isdigit() or int(length)>limit):raise ValueError()
                    data=bytearray()
                    for chunk in result.iter_bytes():
                        check_deadline()
                        if len(data)+len(chunk)>limit:raise ValueError()
                        data.extend(chunk)
                    if not data:raise ValueError()
                    if media:return bytes(data)
                    value=json.loads(data,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
                    if not isinstance(value,dict):raise ValueError()
                    return value
        except Exception:
            raise ProviderOutcomeUnknown('fal outcome could not be verified. Reconcile the original reference; do not resubmit.') from None

    def submit(self,request):
        request=_motion_request(request)
        result=self._request(FAL_QUEUE_ORIGIN+'/'+FAL_IMAGE_ENDPOINT,'POST',request['body'])
        try:
            return validate_fal_handle({'endpoint':FAL_IMAGE_ENDPOINT,**{k:result[k] for k in ('request_id','response_url','status_url')}})
        except Exception:
            raise ProviderOutcomeUnknown('fal submission receipt could not be verified; do not resubmit.') from None

    def submit_authorized(self,request,submission_check):
        request=_motion_request(request)
        if not callable(submission_check):raise ValueError('Final motion authorization required.')
        result=self._request(FAL_QUEUE_ORIGIN+'/'+FAL_IMAGE_ENDPOINT,'POST',request['body'],submission_check=submission_check)
        return validate_fal_handle({'endpoint':FAL_IMAGE_ENDPOINT,**{k:result[k] for k in ('request_id','response_url','status_url')}})

    def status(self,handle):
        handle=validate_fal_handle(handle)
        result=self._request(handle['status_url'],'GET')
        if result.get('request_id')!=handle['request_id'] or result.get('status') not in {'IN_QUEUE','IN_PROGRESS','COMPLETED'}:
            raise ProviderOutcomeUnknown('Queue status was not verified; no new submission was made.')
        return result['status']

    def result(self,handle):
        handle=validate_fal_handle(handle)
        result=self._request(handle['response_url'],'GET')
        video=result.get('video')
        if (not isinstance(video,dict) or not _fal_media_url(video.get('url')) or type(video.get('file_size')) is not int
                or not 0<video['file_size']<=FAL_MAX_MEDIA_BYTES or video.get('content_type') not in {'video/mp4','application/octet-stream'}):
            raise ProviderOutcomeUnknown('Queue result was not verified; no new submission was made.')
        return {'url':video['url'],'bytes':video['file_size'],'request_id':handle['request_id'],'media_verified':False}

    def download(self,result):
        if not isinstance(result,dict) or type(result.get('bytes')) is not int or not 0<result['bytes']<=FAL_MAX_MEDIA_BYTES:
            raise ValueError('A verified bounded result receipt is required.')
        data=self._request(result.get('url'),'GET',media=True)
        if len(data)!=result['bytes']:raise ValueError('Provider media byte count differs.')
        return {'bytes':data,'sha256':hashlib.sha256(data).hexdigest(),'media_verified':False}


def submit_motion_once(*,owner_id,job_id,request,approval,journal,transport):
    """Reserve an anchored operation once; queue polling never repeats POST.

    The journal must include any prior campaign spending; a fresh per-job ledger
    is not permission to reset a preexisting overall budget. No price is inferred.
    """
    if any(not isinstance(x,str) or not ID.fullmatch(x) for x in (owner_id,job_id)):
        raise ValueError('Verified owner/job references required.')
    request=_motion_request(request)
    approval.validate([request])
    operation='motion_'+hashlib.sha256((owner_id+'\0'+job_id+'\0'+request['image_id']).encode()).hexdigest()
    binding={'owner_job_sha256':hashlib.sha256((owner_id+'\0'+job_id).encode()).hexdigest(),
             'request':request,'approval_ref':approval.reference,'reservation_cents':approval.reservations_cents[0],
             'job_cap_cents':approval.job_cap_cents,'operation_cap_cents':approval.operation_cap_cents}
    existing=journal.get(operation)
    if existing is not None:
        if existing['plan_sha256']!=fingerprint(binding) or existing['reservation_cents']!=approval.reservations_cents[0]:
            raise ValueError('Frozen motion operation changed.')
        if existing['state']!='received':raise ProviderOutcomeUnknown('Motion may already have been submitted; do not resubmit.')
        return validate_fal_handle(existing['receipt'])
    journal.arm(operation,binding,approval.reservations_cents[0],approval.job_cap_cents)
    try:
        handle=validate_fal_handle(transport.submit(request))
        journal.record(operation,binding,handle)
        return handle
    except Exception:
        raise ProviderOutcomeUnknown('Motion submission is uncertain. Keep its reservation; do not resubmit.') from None
