"""Disabled, one-shot provider result capture with independent output review.

No POST/retry exists here. Unknown capture writes HOLD; explicit read-only
reconciliation can recover immutable bytes but never recreate a paid request.
"""
import hashlib
import json
from pathlib import Path
from .elevenlabs_adapter import fingerprint
from .motion_continuation import (_checked_plan,_journal_scope,_identifier,_sha,require,
    validate_extension_handle,VerifiedPriorClip)
from .provider_execution import validate_fal_handle,_fal_media_url
from .continuation_worker import _operation
from .cloud_journal import _FirestoreBoundary
from .cloud_storage import _PrivateBucket
from .worker_binding import canonical,safe_private_root,copy_verified_object


class ReviewedProviderGeometry:
    """Exact previously reviewed provider-rounding geometry, frozen by release hash."""
    def __call__(self,job,path,video,scene):
        from .portrait_composition import release_bindings,run_bounded,input_options,validate_presentation_crop
        root=Path(__file__).resolve().parent.parent/'style-guide/portrait-candidate'
        bindings=release_bindings()
        require(job.snapshot['processing_plan'].get('visual_style_sha256')==bindings['visual_style_sha256'],
                'Rounding crop requires the exact independently frozen release policy.')
        policy=json.loads((root/'release.json').read_text()).get('provider_rounding_geometry')
        expected={'source_width':768,'source_height':1344,'crop':{'left':6,'top':0,'width':756,'height':1344},'maximum_removed_area_fraction':0.02}
        require(policy==expected,'Only the reviewed provider rounding geometry may be applied.')
        original=next((i for i in job.snapshot['images'] if i['id']==scene['image_id']),None)
        require(original is not None and original['sha256']==scene['source_sha256']
                and hashlib.sha256(Path(path).read_bytes()).hexdigest()==video['sha256'],
                'Crop geometry must bind this immutable source/clip.')
        metadata=json.loads(run_bounded(['ffprobe','-v','error',*input_options(path,video=True),'-select_streams','v:0',
            '-show_entries','stream=width,height','-of','json',str(path)],30,65536).stdout)
        streams=metadata.get('streams');require(isinstance(streams,list) and len(streams)==1,'One native video stream required.')
        w,h=streams[0]['width'],streams[0]['height']
        if w*16==h*9:return None
        require((w,h)==(policy['source_width'],policy['source_height']),'Unreviewed provider dimensions require review, not an invented crop.')
        return validate_presentation_crop(dict(policy['crop']),w,h)


class CloudSegmentObjects(_PrivateBucket):
    def __init__(self,client,*,bucket,timeout=30):
        super().__init__(client,bucket=bucket,timeout=timeout);self.bucket_name=bucket
    def name(self,job,image,sequence):
        require(_identifier(job.owner_id) and _identifier(job.id) and _identifier(image)
                and type(sequence) is int and 0<=sequence<=10,'Invalid segment identity.')
        return f'owners/{hashlib.sha256(job.owner_id.encode()).hexdigest()}/jobs/{job.id}/segments/{image}/{sequence:02}.mp4'
    def write(self,job,image,sequence,data):
        require(isinstance(data,bytes) and 0<len(data)<=50_000_000,'Bounded MP4 bytes required.')
        name=self.name(job,image,sequence);generation=self._write(name,data,'video/mp4')
        ref=dict(bucket=self.bucket_name,object=name,generation=generation,sha256=hashlib.sha256(data).hexdigest(),bytes=len(data),content_type='video/mp4')
        response=self.read(ref);response['stream'].close();return ref
    def read(self,ref):
        require(ref.get('bucket')==self.bucket_name,'Wrong private segment bucket.')
        stream,generation,_=self._download(ref['object'],generation=ref['generation'],maximum=50_000_000,mime='video/mp4',expected_sha=ref['sha256'],expected_size=ref['bytes'])
        return dict(generation=generation,content_type='video/mp4',stream=stream)
    def recover(self,job,image,sequence,expected_sha,expected_bytes):
        """Read-only current generation discovery, constrained by pre-upload hash."""
        name=self.name(job,image,sequence)
        stream,generation,_=self._download(name,maximum=50_000_000,mime='video/mp4',expected_sha=expected_sha,expected_size=expected_bytes)
        stream.close();return dict(bucket=self.bucket_name,object=name,generation=generation,sha256=expected_sha,bytes=expected_bytes,content_type='video/mp4')


class FirestoreSegmentStore(_FirestoreBoundary):
    def __init__(self,*args,objects,**kwargs):
        super().__init__(*args,**kwargs);self.objects=objects
    def _ref(self,job,image,sequence):
        self.objects.name(job,image,sequence)
        return self._doc(self._owner(job.owner_id)+'/jobs/'+job.id+'/segments/'+image+'_'+str(sequence))
    def get_capture(self,job,image,sequence):return self._read(self._ref(job,image,sequence))
    def claim_capture(self,job,image,sequence,binding):
        ref=self._ref(job,image,sequence)
        payload=dict(state='CAPTURING',binding=binding,binding_sha256=fingerprint(binding))
        require(len(canonical(payload))<100_000,'Bounded segment binding required.')
        def transaction(tx):
            existing=self._read(ref,tx)
            if existing is not None:
                require(existing['binding_sha256']==payload['binding_sha256'],'Immutable capture binding changed.')
                return False
            tx.create(ref,payload);return True
        return self._run(transaction)
    def record_media(self,job,image,sequence,binding,video):
        ref=self._ref(job,image,sequence)
        def transaction(tx):
            existing=self._read(ref,tx)
            require(existing and existing['binding_sha256']==fingerprint(binding),'Capture binding changed.')
            if existing['state']!='CAPTURING':
                require(existing.get('video')==video,'Captured immutable media changed.');return False
            value={**existing,'state':'MEDIA','video':video};tx.update(ref,value);return True
        return self._run(transaction)
    def accept(self,job,image,sequence,record):
        ref=self._ref(job,image,sequence);digest=fingerprint(record)
        require(len(canonical(record))<100_000,'Bounded segment record required.')
        def transaction(tx):
            existing=self._read(ref,tx)
            require(existing and existing['state'] in {'MEDIA','READY','PREVIEW_READY'} and existing['video']==record['video'],'Only captured immutable media may be accepted.')
            if existing['state'] in {'READY','PREVIEW_READY'}:
                require(existing['record_sha256']==digest,'Accepted output review changed.');return False
            tx.update(ref,{**existing,'state':'PREVIEW_READY' if record.get('schema_version')==2 else 'READY','record_json':canonical(record).decode(),'record_sha256':digest});return True
        self._run(transaction);return record,digest
    def get_record(self,job,image,sequence):
        value=self.get_capture(job,image,sequence)
        require(value and value['state'] in {'READY','PREVIEW_READY'},'Previous full clip lacks committed independent review.')
        record=json.loads(value['record_json']);require(fingerprint(record)==value['record_sha256'],'Stored segment record changed.')
        return record,value['record_sha256']
    def reconcile(self,job,image,sequence):
        """Explicit read-only object recovery; never upload or submit again."""
        value=self.get_capture(job,image,sequence)
        require(value and value['state']=='CAPTURING','Only uncertain capture can be reconciled.')
        binding=value['binding'];video=self.objects.recover(job,image,sequence,binding['clip_sha256'],binding['byte_count'])
        self.record_media(job,image,sequence,binding,video)
        return {'state':'MEDIA','provider_requests':0}


def capture_segment(*,job,plan,expected_plan_sha256,image_id,sequence,spend_journal,transport,
                    segment_store,review_loader,root,narration_report,crop_resolver=None,enabled=False,now=None):
    """One bounded read-only queue observation/capture, never provider submission.

    review_loader(candidate_record) -> exact five-field review dict or None.
    Its implementation must load trusted independent output review, not produce
    semantic acceptance from the decoder. A missing review returns REVIEW_REQUIRED.
    Every READY return independently re-reads and fully decodes private bytes.
    """
    require(enabled is True,'Segment capture is disabled until explicitly configured.')
    _checked_plan(plan,expected_plan_sha256,0,False);_journal_scope(spend_journal,plan)
    is_preview=bool(plan.get('preview_schema'))
    if is_preview:
        from .private_preview import preview_plan
        preview_plan(plan,now=now,require_current=True)
    require(job.owner_id==plan['owner_id'] and job.id==plan['job_id'] and fingerprint(job.snapshot)==job.snapshot_sha256
            and job.snapshot['input_order']==plan['input_order'],'Capture differs from frozen job.')
    scene=next((s for s in plan['scenes'] if s['image_id']==image_id),None)
    require(scene is not None and type(sequence) is int and 0<=sequence<len(scene['segments']),'Unplanned segment.')
    require([(x['id'],x['position'],x['sha256']) for x in job.snapshot['images']]==[(x['image_id'],x['position'],x['source_sha256']) for x in plan['scenes']], 'Frozen image/source order changed.')
    require(fingerprint(narration_report)==plan['narration_report_sha256'] and type(narration_report.get('synthetic')) is bool
            and narration_report.get('exact_text')==job.snapshot['narrated_text']
            and narration_report.get('profile_sha256')==fingerprint(job.snapshot['processing_plan']['narration_profile']), 'Independent frozen narration provenance required.')
    previous=segment_store.get_record(job,image_id,sequence-1) if sequence else None
    if previous:
        prior=previous[0]
        require((prior['owner_id'],prior['job_id'],prior['snapshot_sha256'],prior['continuation_plan_sha256'],prior['image_id'],prior['source_sha256'],prior['sequence'])==
                (job.owner_id,job.id,job.snapshot_sha256,expected_plan_sha256,image_id,scene['source_sha256'],sequence-1),'Prior accepted record differs from continuation scope.')
    operation=_operation(job.owner_id,job.id,image_id,sequence)
    plan_id='continuation_'+hashlib.sha256((job.owner_id+'\0'+job.id).encode()).hexdigest()
    allocation=getattr(spend_journal,'allocation',None)
    entry=allocation(operation) if callable(allocation) else spend_journal.get_reserved(operation,plan_id,expected_plan_sha256)
    require(entry.get('reservation_cents')==scene['segments'][sequence]['max_cost_cents'],'Paid operation differs from the fully funded plan.')
    require(entry and entry.get('state')=='received' and _sha(entry.get('plan_sha256')),'No verified paid-operation receipt; retain HOLD and never resubmit.')
    handle=(validate_extension_handle if sequence else validate_fal_handle)(entry['receipt'])
    read_requests=0
    captured=segment_store.get_capture(job,image_id,sequence)
    if captured is not None and captured['state']=='CAPTURING':return {'state':'HOLD','reason':'capture_write_outcome_unknown','provider_requests':0}
    if captured is None:
        read_requests+=1
        status=transport.status(handle)
        require(status in {'IN_QUEUE','IN_PROGRESS','COMPLETED'},'Unverified queue status.')
        if status!='COMPLETED':return {'state':status,'provider_requests':read_requests,'paid_submissions':0}
        read_requests+=1
        result=transport.result(handle)
        require(result.get('request_id')==handle['request_id'] and _fal_media_url(result.get('url')),'Queue output changed its request identity.')
        read_requests+=1
        download=transport.download(result);body=download.get('bytes')
        require(isinstance(body,bytes) and 0<len(body)<=50_000_000 and len(body)==result['bytes']
                and hashlib.sha256(body).hexdigest()==download.get('sha256'),'Downloaded full clip changed.')
        binding=dict(owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,continuation_plan_sha256=expected_plan_sha256,
            image_id=image_id,source_sha256=scene['source_sha256'],sequence=sequence,operation_id=operation,journal_binding_sha256=entry['plan_sha256'],
            provider_handle=handle,provider_url=result['url'],clip_sha256=download['sha256'],byte_count=len(body),
            prior_record_sha256=previous[1] if previous else None,prior_clip_sha256=previous[0]['video']['sha256'] if previous else None)
        if not segment_store.claim_capture(job,image_id,sequence,binding):return {'state':'HOLD','reason':'concurrent_capture','provider_requests':read_requests,'paid_submissions':0}
        # The reservation precedes the object write. Unknown write/commit remains HOLD.
        video=segment_store.objects.write(job,image_id,sequence,body)
        segment_store.record_media(job,image_id,sequence,binding,video)
        captured=segment_store.get_capture(job,image_id,sequence)
    binding=captured['binding'];video=captured['video']
    require((binding['owner_id'],binding['job_id'],binding['image_id'],binding['source_sha256'],binding['sequence'],binding['operation_id'])==
            (job.owner_id,job.id,image_id,scene['source_sha256'],sequence,operation),'Captured operation scope changed.')
    require(video['object']==segment_store.objects.name(job,image_id,sequence) and video['bucket']==segment_store.objects.bucket_name
            and video['sha256']==binding['clip_sha256'] and video['bytes']==binding['byte_count'],'Captured private object changed.')
    require(fingerprint(binding)==captured['binding_sha256'] and binding['continuation_plan_sha256']==expected_plan_sha256
            and binding['snapshot_sha256']==job.snapshot_sha256 and binding['provider_handle']==handle
            and binding['journal_binding_sha256']==entry['plan_sha256'] and binding['prior_record_sha256']==(previous[1] if previous else None),
            'Captured media binding differs from immutable paid operation.')
    root=safe_private_root(root);path=copy_verified_object(segment_store.objects.read,video,root/'full.mp4')
    from .portrait_composition import probe
    crop=crop_resolver(job,path,video,scene) if crop_resolver is not None else None
    decoded=probe(path,video=True,presentation_crop=crop)
    require(abs(decoded['duration']-scene['segments'][sequence]['expected_full_duration_seconds'])<=1/24,
            'Provider did not return expected full continuous clip.')
    require(hashlib.sha256(path.read_bytes()).hexdigest()==video['sha256'],'Clip changed during full decode.')
    record={key:binding[key] for key in ('owner_id','job_id','snapshot_sha256','continuation_plan_sha256','image_id','source_sha256','sequence',
        'operation_id','journal_binding_sha256','provider_handle','provider_url','prior_record_sha256','prior_clip_sha256')}
    record.update(schema_version=1,video=video,duration_seconds=decoded['duration'],presentation_crop=crop,
                  synthetic=narration_report['synthetic'],full_decode_verified=True)
    if is_preview:record['schema_version']=2
    if captured['state'] in {'READY','PREVIEW_READY'}:
        accepted,digest=segment_store.get_record(job,image_id,sequence)
        require(all(accepted.get(k)==v for k,v in record.items()),'Accepted segment changed after immutable capture.')
        record=accepted
        if is_preview:
            from .private_preview import validate_preview_qc
            validate_preview_qc(record['review'],candidate=record,plan=plan,job=job,now=now,archive_verifier=getattr(review_loader,'verify_preview_evidence',None))
    else:
        review=review_loader(dict(record))
        if review is None:return {'state':'REVIEW_REQUIRED','candidate':record,'candidate_sha256':fingerprint(record),'provider_requests':read_requests,'paid_submissions':0}
        if isinstance(review,dict) and review.get('state')=='REVIEW_REQUIRED':
            require(review.get('human_approved') is False and review.get('live_release',False) is False,
                    'Candidate review cannot imply acceptance.')
            return {'state':'REVIEW_REQUIRED','review_protocol':'unapproved_automated_candidate',
                'candidate':record,'candidate_sha256':fingerprint(record),'provider_requests':read_requests,'paid_submissions':0}
        if is_preview:
            from .private_preview import validate_preview_qc
            validate_preview_qc(review,candidate=record,plan=plan,job=job,now=now,archive_verifier=getattr(review_loader,'verify_preview_evidence',None))
        else:
            require(isinstance(review,dict) and set(review)=={'reference','clip_sha256','source_sha256','prompt_sha256','no_invented_action_reviewed'}
            and _identifier(review['reference']) and review['clip_sha256']==video['sha256'] and review['source_sha256']==scene['source_sha256']
            and review['prompt_sha256']==hashlib.sha256(scene['approved_prompt'].encode()).hexdigest() and review['no_invented_action_reviewed'] is True,
            'Explicit independent exact-output semantic review is required.')
        record['review']=review;record,digest=segment_store.accept(job,image_id,sequence,record)
    proof=VerifiedPriorClip(job.owner_id,job.id,image_id,scene['source_sha256'],sequence,video['sha256'],video['bytes'],decoded['duration'],handle['request_id'],binding['provider_url'],digest,True)
    return {'state':'READY','record':record,'record_sha256':digest,'verified_clip':proof,'provider_requests':read_requests,'paid_submissions':0}


class MotionTransportRouter:
    """Route only the two independently pinned transports; disabled by default."""
    def __init__(self,*,image_transport,extension_transport,activation_check,enabled=False):
        self.image=image_transport;self.extension=extension_transport
        self.activation_check=activation_check;self.enabled=enabled
    def _route(self,endpoint):
        from .motion_continuation import IMAGE_ENDPOINT,EXTENSION_ENDPOINT
        require(self.enabled is True and callable(self.activation_check) and self.activation_check() is True,
                'Motion transport routing is disabled or its acceptance expired.')
        require(endpoint in {IMAGE_ENDPOINT,EXTENSION_ENDPOINT},'Only reviewed image/extension endpoints may be routed.')
        return self.image if endpoint==IMAGE_ENDPOINT else self.extension
    def submit(self,request):return self._route(request.get('endpoint')).submit(request)
    def submit_authorized(self,request,submission_check):
        target=self._route(request.get('endpoint'))
        require(callable(getattr(target,'submit_authorized',None)),'Exact pinned transport lacks final preview authorization gate.')
        return target.submit_authorized(request,submission_check)
    def status(self,handle):return self._route(handle.get('endpoint')).status(handle)
    def result(self,handle):
        value=self._route(handle.get('endpoint')).result(handle)
        require(isinstance(value,dict) and 'route_endpoint' not in value,'Unexpected transport result shape.')
        return {**value,'route_endpoint':handle['endpoint']}
    def download(self,result):
        require(isinstance(result,dict),'Routed media result required.')
        value=dict(result);endpoint=value.pop('route_endpoint',None)
        return self._route(endpoint).download(value)
