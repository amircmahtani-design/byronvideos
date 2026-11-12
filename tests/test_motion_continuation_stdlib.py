"""Offline bounded-continuation tests; quotes, identities and clips are synthetic."""
from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from byron_video.elevenlabs_adapter import fingerprint
from byron_video.motion_planning import plan_motion
from byron_video.motion_continuation import (
    EXTENSION_ENDPOINT, VerifiedPriorClip, FalExtensionTransport, plan_continuation,
    extension_request, reserve_retry_allowance, submit_extension_once, select_final_clips, verify_prior_clip,
)
from byron_video.provider_execution import ProviderOutcomeUnknown,WireResponse
from byron_video.worker_journal import Journal
from test_provider_execution_stdlib import FakeClient,FakeResponse

NOW=1000


def inputs(durations=(12,17,22,27)):
    text='Synthetic words unchanged.';text_sha=hashlib.sha256(text.encode()).hexdigest()
    source=[];images=[];timed=[];cursor=0
    for index,duration in enumerate(durations,1):
        image_id=f'fixture_image_{index}'
        sha=hashlib.sha256(str(index).encode()).hexdigest()
        source.append({'id':image_id,'position':index,'source_sha256':sha,'duration_seconds':duration,
            'source_review':{'provenance':'local_human_source_review','source_sha256':sha,'reference_id':f'fixture_review_{index}',
                             'capabilities':['water','safe_camera_framing']}})
        images.append({'id':image_id,'position':index,'sha256':sha})
        start=0 if index==1 else cursor-.7;cursor=start+duration
        timed.append({'image_id':image_id,'start_seconds':start,'end_seconds':cursor})
    words=[{'text':'Synthetic','start':0,'end':1,'char_start':0,'char_end':9},
           {'text':'words','start':1,'end':2,'char_start':10,'char_end':15},
           {'text':'unchanged.','start':2,'end':3,'char_start':16,'char_end':26}]
    report={'provider':'elevenlabs','media_verified':True,'provider_receipts_verified':True,'status':'DRAFT_RELEASE_CANDIDATE',
            'exact_text':text,'audio_sha256':'a'*64,'words':words,'duration_seconds':cursor,'synthetic':True}
    timing={'schema_version':1,'source':'exact_narration_word_times','narration_text_sha256':text_sha,
        'audio_sha256':'a'*64,'word_timing_sha256':fingerprint(words),'duration_seconds':cursor,'crossfade_seconds':.7,'scenes':timed}
    motion=plan_motion(source,text_sha,timing_plan=timing)
    approved=[{'image_id':s['image_id'],'source_sha256':s['source_sha256'],'scene_sha256':fingerprint(s),
        'prompt':s['prompt'],'reference':f'fixture_prompt_{i}','approved':True} for i,s in enumerate(motion['scenes'],1)]
    quote=lambda kind,endpoint,duration,cost:{'kind':kind,'endpoint':endpoint,'duration_seconds':duration,'resolution':'768P',
        'max_cost_cents':cost,'valid_until_epoch':NOW+100,'reference':'fixture_quote_'+kind}
    return dict(owner_id='fixture_owner',job_id='f'*32,images=images,motion_plan=motion,timing=timing,narration_report=report,
        expected_narration_report_sha256=fingerprint(report),approved_prompts=approved,
        image_quote=quote('image','minimax/h3-max-turbo/image-to-video',10,2),
        extension_quote=quote('extend',EXTENSION_ENDPOINT,5,3),
        budget={'scope':'fixture_campaign','approved_total_cap_cents':300,'prior_spend_cents':7,'job_cap_cents':200,
                'retry_allowance_cents':5,'approval_reference':'fixture_spend_approval'},now=NOW)


class ScopedJournal:
    """Production journal methods against an atomic in-memory Firestore fake."""
    def __init__(self,path,plan):
        from test_cloud_journal_stdlib import Client
        from byron_video.cloud_journal import FirestoreSpendJournal
        self.client=Client()
        self.delegate=FirestoreSpendJournal(self.client,project_id='synthetic-project',namespace='fixture_namespace',
            owner_id=plan['owner_id'],job_id=plan['job_id'],budget_scope=plan['budget']['scope'],
            approved_total_cap_cents=plan['budget']['approved_total_cap_cents'],prior_spend_cents=plan['budget']['prior_spend_cents'],
            transaction_runner=self.client.run)
        for key in ('owner_id','job_id','scope_hash','total_cap','prior_spend'):setattr(self,key,getattr(self.delegate,key))
    def __getattr__(self,name):return getattr(self.delegate,name)
    def close(self):pass
    def operations(self):return [v for k,v in self.client.data.items() if '/operations/' in k]
    def allocations(self):return [v for k,v in self.client.data.items() if '/plan_allocations/' in k]
    def reserved(self):return self.client.data.get(self.delegate.root+'/'+self.delegate.path,{}).get('reserved_cents',0)


class ContinuationChecks(unittest.TestCase):
    def setUp(self):
        self.data=inputs();self.plan=plan_continuation(**self.data);self.plan_sha=fingerprint(self.plan)
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.journal=ScopedJournal(self.root/'journal.sqlite',self.plan)
        self.calls=[]
    def tearDown(self):self.journal.close();self.temp.cleanup()
    def prior(self,scene=0,sequence=0):
        selected=self.plan['scenes'][scene]
        return VerifiedPriorClip(self.plan['owner_id'],self.plan['job_id'],selected['image_id'],selected['source_sha256'],sequence,
            'b'*64,1234,10+sequence*5,'fixture_request_1','https://fal.media/files/fixture.mp4','c'*64,True)
    def handle(self,number=1):
        base='https://queue.fal.run/minimax/h3-max-turbo/requests/fixture_request_'+str(number)
        return {'endpoint':EXTENSION_ENDPOINT,'request_id':'fixture_request_'+str(number),'response_url':base,'status_url':base+'/status'}
    def transport(self):
        def submit(request):self.calls.append(request);return self.handle(len(self.calls))
        return SimpleNamespace(submit=submit)
    def submit(self,**changes):
        args=dict(plan=self.plan,expected_plan_sha256=self.plan_sha,image_id=self.plan['input_order'][0],sequence=1,
            prior_clip=self.prior(),journal=self.journal,transport=self.transport(),now=NOW)
        args.update(changes);return submit_extension_once(**args)

    def test_complete_narration_coverage_overlap_and_finite_segments(self):
        self.assertEqual([len(s['segments'])-1 for s in self.plan['scenes']],[1,2,3,4])
        self.assertEqual(self.plan['input_order'],[i['id'] for i in self.data['images']])
        self.assertAlmostEqual(sum(s['required_seconds'] for s in self.plan['scenes'])-.7*3,self.plan['duration_seconds'])
        self.assertEqual(self.plan['quoted_generation_cents'],38)
        self.assertEqual(self.plan['reserved_plan_ceiling_cents'],43)
        self.assertEqual(self.plan['automatic_retries'],0)
        for scene in self.plan['scenes']:
            self.assertTrue(scene['assembly']['use_only_final_full_clip'])
            self.assertFalse(scene['assembly']['loop']);self.assertFalse(scene['assembly']['freeze']);self.assertFalse(scene['assembly']['retime'])

    def test_five_sixty_second_scenes_have_ten_extensions_each(self):
        data=inputs((60,60,60,60,60));data['budget'].update(job_cap_cents=200,approved_total_cap_cents=300)
        plan=plan_continuation(**data)
        self.assertEqual([len(s['segments']) for s in plan['scenes']],[11]*5)
        self.assertAlmostEqual(plan['duration_seconds'],297.2)
        self.assertTrue(all(s['segments'][-1]['expected_full_duration_seconds']==60 for s in plan['scenes']))

    def test_missing_unknown_expired_or_reused_extension_quote_blocks(self):
        for change in [None,{},dict(self.data['extension_quote'],max_cost_cents=None),
                       dict(self.data['extension_quote'],valid_until_epoch=NOW),self.data['image_quote'],
                       dict(self.data['extension_quote'],reference=self.data['image_quote']['reference'])]:
            with self.subTest(change=change),self.assertRaises(ValueError):plan_continuation(**{**self.data,'extension_quote':change})

    def test_narration_or_prompt_changes_fail_before_work(self):
        for field,change in [('narration_report',{**self.data['narration_report'],'duration_seconds':1}),
                             ('expected_narration_report_sha256','f'*64)]:
            with self.assertRaises(ValueError):plan_continuation(**{**self.data,field:change})
        changed=copy.deepcopy(self.data);changed['approved_prompts'][0]['prompt']='Invent a new story action.'
        with self.assertRaises(ValueError):plan_continuation(**changed)
        changed=copy.deepcopy(self.data);changed['approved_prompts'][0]['approved']=False
        with self.assertRaises(ValueError):plan_continuation(**changed)

    def test_budget_includes_previous_spend_and_explicit_allowance(self):
        for budget in [dict(self.data['budget'],job_cap_cents=42),dict(self.data['budget'],approved_total_cap_cents=49,job_cap_cents=49),
                       dict(self.data['budget'],retry_allowance_cents=None)]:
            with self.assertRaises(ValueError):plan_continuation(**{**self.data,'budget':budget})

    def test_fewer_scenes_or_overlong_scene_rejected(self):
        with self.assertRaises(ValueError):plan_continuation(**inputs((12,17,22)))
        with self.assertRaises(ValueError):inputs((61,17,22,27))

    def test_recovered_extension_schema_uses_full_prior_clip(self):
        request=extension_request(plan=self.plan,expected_plan_sha256=self.plan_sha,image_id=self.plan['input_order'][0],sequence=1,prior_clip=self.prior(),now=NOW)
        self.assertEqual(request['body'],dict(video_url=self.prior().provider_url,prompt=self.plan['scenes'][0]['approved_prompt'],duration=5,
            resolution='768P',enable_safety_checker=True,enable_prompt_expansion=False,aspect_ratio='9:16',output='extended'))
        self.assertEqual(request['prior_clip']['clip_sha256'],'b'*64)
        self.assertEqual(request['max_cost_cents'],3)

    def test_wrong_owner_source_sequence_or_short_prior_blocks(self):
        for change in [dict(owner_id='other_owner'),dict(source_sha256='d'*64),dict(sequence=1),dict(duration_seconds=9),dict(full_decode_verified=False)]:
            with self.subTest(change=change),self.assertRaises(ValueError):self.submit(prior_clip=replace(self.prior(),**change))
        self.assertEqual(self.calls,[])
        self.assertEqual(len(self.journal.operations()),0)

    def test_allowance_reserved_once_before_segment_no_automatic_retry(self):
        def submit(request):
            rows=[(r['reservation_cents'],r['state']) for r in self.journal.operations()]
            self.assertEqual(rows,[(3,'may_have_submitted')])
            self.assertEqual(self.journal.reserved(),43)
            self.assertTrue(any(r['reservation_cents']==5 and r['state']=='reserved' for r in self.journal.allocations()))
            self.calls.append(request);return self.handle()
        first=self.submit(transport=SimpleNamespace(submit=submit));second=self.submit()
        self.assertEqual(first,second);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.journal.reserved(),43)
        self.assertEqual(reserve_retry_allowance(plan=self.plan,expected_plan_sha256=self.plan_sha,journal=self.journal,now=NOW)['reserved_cents'],5)

    def test_unknown_extension_retains_every_reservation_and_blocks_reentry(self):
        def fail(request):self.calls.append(request);raise TimeoutError('private input')
        with self.assertRaises(ProviderOutcomeUnknown) as error:self.submit(transport=SimpleNamespace(submit=fail))
        self.assertNotIn('private input',str(error.exception))
        with self.assertRaises(ProviderOutcomeUnknown):self.submit()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.journal.reserved(),43)

    def test_matching_shared_budget_journal_is_mandatory(self):
        self.journal.scope_hash='d'*64
        with self.assertRaises(ValueError):self.submit()
        self.assertEqual(self.calls,[])

    def test_changed_frozen_plan_or_expired_quote_blocks_new_post(self):
        changed=copy.deepcopy(self.plan);changed['scenes'][0]['approved_prompt']='Changed'
        with self.assertRaises(ValueError):self.submit(plan=changed)
        with self.assertRaises(ValueError):self.submit(now=NOW+200)
        self.assertEqual(self.calls,[])

    def test_received_replay_and_final_selection_do_not_need_fresh_quote(self):
        first=self.submit()
        self.assertEqual(self.submit(now=NOW+200),first);self.assertEqual(len(self.calls),1)
        clips=[self.prior(i,len(s['segments'])-1) for i,s in enumerate(self.plan['scenes'])]
        chosen=select_final_clips(plan=self.plan,expected_plan_sha256=self.plan_sha,verified_clips=clips,now=NOW+200)
        self.assertEqual([r['sequence'] for r in chosen],[1,2,3,4]);self.assertTrue(all(r['trim_start_seconds']==0 for r in chosen))
        with self.assertRaises(ValueError):select_final_clips(plan=self.plan,expected_plan_sha256=self.plan_sha,verified_clips=[self.prior()]+clips[1:],now=NOW)

    def test_default_transport_disabled_and_no_key_read(self):
        calls=[];transport=FalExtensionTransport(lambda:calls.append(True))
        request=extension_request(plan=self.plan,expected_plan_sha256=self.plan_sha,image_id=self.plan['input_order'][0],sequence=1,prior_clip=self.prior(),now=NOW)
        with self.assertRaises(RuntimeError):transport.submit(request)
        self.assertEqual(calls,[])

    def test_queue_transport_pinned_and_read_only_status_download(self):
        calls=[];keys=[]
        replies=[FakeResponse(WireResponse('fixture_request_1',json.dumps(self.handle()).encode()),status=202),
                 FakeResponse(WireResponse('fixture_request_1',json.dumps({'request_id':'fixture_request_1','status':'COMPLETED'}).encode())),
                 FakeResponse(WireResponse('fixture_request_1',json.dumps({'video':{'url':'https://fal.media/files/fixture.mp4','file_size':3,'content_type':'video/mp4'}}).encode())),
                 FakeResponse(chunks=[b'abc'])]
        def factory(**kw):
            self.assertFalse(kw['follow_redirects']);self.assertFalse(kw['trust_env']);return FakeClient(calls,replies.pop(0))
        def key():keys.append(True);return 'synthetic-provider-key'
        transport=FalExtensionTransport(key,enabled=True,client_factory=factory)
        handle=self.submit(transport=transport)
        self.assertEqual(transport.status(handle),'COMPLETED')
        self.assertEqual(transport.download(transport.result(handle))['bytes'],b'abc')
        self.assertEqual([x[0][0] for x in calls],['POST','GET','GET','GET'])
        self.assertTrue(calls[0][0][1].endswith('/extend-video'))
        self.assertEqual(calls[0][1]['headers']['X-Fal-No-Retry'],'1')
        self.assertEqual(calls[-1][1]['headers'],{'Accept-Encoding':'identity'});self.assertEqual(len(keys),3)

    def test_initial_image_bridge_uses_distinct_quote_and_reserves_allowance(self):
        import io
        from PIL import Image
        from byron_video.provider_execution import plan_anchored_motion,FAL_IMAGE_ENDPOINT
        from byron_video.motion_continuation import submit_initial_image_once
        stream=io.BytesIO();Image.new('RGB',(64,64),'blue').save(stream,format='PNG');raw=stream.getvalue()
        sha=hashlib.sha256(raw).hexdigest();data=inputs()
        data['images'][0]['sha256']=sha
        sources=data['motion_plan']['source_inputs'];sources[0]['source_sha256']=sha;sources[0]['source_review']['source_sha256']=sha
        motion=plan_motion(sources,hashlib.sha256(data['narration_report']['exact_text'].encode()).hexdigest(),timing_plan=data['timing'])
        data['motion_plan']=motion
        data['approved_prompts']=[{'image_id':scene['image_id'],'source_sha256':scene['source_sha256'],'scene_sha256':fingerprint(scene),
            'prompt':scene['prompt'],'reference':f'fixture_prompt_{i}','approved':True} for i,scene in enumerate(motion['scenes'],1)]
        plan=plan_continuation(**data)
        request=plan_anchored_motion(image_id=plan['input_order'][0],original_bytes=raw,content_type='image/png',original_sha256=sha,
            prompt=plan['scenes'][0]['approved_prompt'])
        calls=[]
        def submit(request):calls.append(request);return {**self.handle(),'endpoint':FAL_IMAGE_ENDPOINT}
        with_journal=ScopedJournal(self.root/'image-journal.sqlite',plan)
        try:
            args=dict(plan=plan,expected_plan_sha256=fingerprint(plan),image_id=plan['input_order'][0],request=request,journal=with_journal,
                      transport=SimpleNamespace(submit=submit),now=NOW)
            first=submit_initial_image_once(**args)
            self.assertEqual(first,submit_initial_image_once(**{**args,'now':NOW+200}))
            self.assertEqual(len(calls),1)
            self.assertEqual([r['reservation_cents'] for r in with_journal.operations()],[2])
        finally:with_journal.close()

    def test_transport_total_deadline_blocks_slow_trickle_without_retry(self):
        from unittest.mock import patch
        calls=[]
        response=FakeResponse(WireResponse('fixture_request_1',json.dumps(self.handle()).encode()))
        clock=iter([0,1,2,61]).__next__
        transport=FalExtensionTransport(lambda:'synthetic-provider-key',enabled=True,client_factory=lambda **kwargs:FakeClient(calls,response),clock=clock)
        with self.assertRaises(ProviderOutcomeUnknown):self.submit(transport=transport)
        self.assertEqual(len(calls),1)
        with self.assertRaises(ProviderOutcomeUnknown):self.submit()
        self.assertEqual(self.journal.reserved(),43)

    def test_independent_full_decode_and_immutable_clip_record(self):
        path=self.root/'prior.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=blue:s=72x128:r=24:d=10',
                        '-c:v','libx264','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True)
        data=path.read_bytes();prior=self.prior()
        record={'owner_id':prior.owner_id,'job_id':prior.job_id,'image_id':prior.image_id,'source_sha256':prior.source_sha256,
                'sequence':0,'path':str(path),'clip_sha256':hashlib.sha256(data).hexdigest(),'byte_count':len(data),'duration_seconds':10,
                'provider_request_id':'fixture_request_1','provider_url':prior.provider_url,'presentation_crop':None}
        verified=verify_prior_clip(private_root=self.root,record=record,expected_record_sha256=fingerprint(record))
        self.assertTrue(verified.full_decode_verified);self.assertEqual(verified.duration_seconds,10)
        with self.assertRaises(ValueError):verify_prior_clip(private_root=self.root,record={**record,'byte_count':1},expected_record_sha256=fingerprint(record))
        path.write_bytes(b'changed')
        with self.assertRaises(ValueError):verify_prior_clip(private_root=self.root,record=record,expected_record_sha256=fingerprint(record))


if __name__=='__main__':unittest.main(verbosity=2)
