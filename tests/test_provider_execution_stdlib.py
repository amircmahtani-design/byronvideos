"""Offline transport/journal tests. Synthetic bytes are NOT valid narrated MP3."""
import base64
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from byron_video.elevenlabs_adapter import fingerprint, plan_requests
from byron_video.provider_execution import (
    ElevenLabsHTTPTransport, FileResponseStore, ProviderOutcomeUnknown,
    SpendApproval, WireResponse, execute_narration_once,
)
from byron_video.worker_journal import Journal


def profile():
    return dict(schema_version=1,version='synthetic-provider-1',status='DRAFT',provider='elevenlabs',
        voice_id='SYNTHETIC_VOICE',model_id='eleven_multilingual_v2',
        voice_settings=dict(stability=.5,similarity_boost=.75,style=0,use_speaker_boost=True,speed=.93),
        output_format='mp3_44100_128',seed=1,apply_text_normalization='off')


def response(text, request_id='synthetic_request_1'):
    n=len(text)
    payload=json.dumps(dict(audio_base64=base64.b64encode(b'NOT-REAL-MP3-FIXTURE').decode(),alignment=dict(
        characters=list(text),character_start_times_seconds=[i/n for i in range(n)],
        character_end_times_seconds=[(i+1)/n for i in range(n)]))).encode()
    return WireResponse(request_id,payload)


class FakeResponse:
    def __init__(self, wire=None, status=200, chunks=None, headers=None):
        wire=wire or response('Synthetic words.')
        self.status_code=status
        self.headers={'request-id':wire.request_id,**(headers or {})}
        self.chunks=chunks if chunks is not None else [wire.payload]
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_bytes(self):yield from self.chunks


class FakeClient:
    def __init__(self,calls,reply):self.calls,self.reply=calls,reply
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def stream(self,*args,**kwargs):
        self.calls.append((args,kwargs))
        if isinstance(self.reply,Exception):raise self.reply
        return self.reply


class ProviderExecutionChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.journal=Journal(self.root/'journal.sqlite')
        self.archive=FileResponseStore(self.root/'responses')
        self.text='Synthetic words.'
        self.requests=plan_requests(self.text,profile())
        self.approval=SpendApproval('synthetic_approval',fingerprint(self.requests),(3,),5,3)
        self.calls=[]
    def tearDown(self):self.journal.close();self.temp.cleanup()
    def transport(self,request):
        self.calls.append(request)
        return response(request['body']['text'],'synthetic_request_'+str(request['sequence']))
    def execute(self,**overrides):
        args=dict(owner_id='fixture_owner',job_id='fixture_job_1',text=self.text,profile=profile(),
                  approval=self.approval,journal=self.journal,transport=self.transport,response_store=self.archive)
        args.update(overrides);return execute_narration_once(**args)

    def test_reserved_before_transport_archived_and_reentry_never_repeats(self):
        def transport(request):
            rows=self.journal.db.execute('SELECT state,reservation FROM operation').fetchall()
            self.assertEqual(rows,[('may_have_submitted',3)])
            return self.transport(request)
        first=self.execute(transport=transport)
        second=self.execute()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(first,second)
        self.assertFalse(first['media_verified']);self.assertFalse(first['live_release'])
        receipt=self.journal.db.execute('SELECT receipt FROM operation').fetchone()[0]
        self.assertNotIn(self.text,receipt)
        self.assertEqual(len(list((self.root/'responses').glob('*.json'))),1)

    def test_unknown_timeout_never_retries_even_after_restart(self):
        def fail(request):self.calls.append(request);raise TimeoutError('synthetic-secret')
        with self.assertRaises(ProviderOutcomeUnknown) as raised:self.execute(transport=fail)
        self.assertNotIn('synthetic-secret',str(raised.exception))
        self.journal.close();self.journal=Journal(self.root/'journal.sqlite')
        with self.assertRaises(ProviderOutcomeUnknown):self.execute()
        self.assertEqual(len(self.calls),1)

    def test_invalid_response_remains_reserved_and_is_not_repeated(self):
        def invalid(request):self.calls.append(request);return WireResponse('synthetic_request_1',b'{}')
        with self.assertRaises(ProviderOutcomeUnknown):self.execute(transport=invalid)
        with self.assertRaises(ProviderOutcomeUnknown):self.execute()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.journal.db.execute('SELECT state FROM operation').fetchone()[0],'may_have_submitted')

    def test_archive_failure_retains_unknown_without_retransmission(self):
        class Broken:
            def write_response(self,*args):raise OSError('private details')
        with self.assertRaises(ProviderOutcomeUnknown):self.execute(response_store=Broken())
        with self.assertRaises(ProviderOutcomeUnknown):self.execute()
        self.assertEqual(len(self.calls),1)

    def test_spend_plan_and_caps_are_preflighted_before_any_call(self):
        cases=[replace(self.approval,plan_sha256='a'*64),replace(self.approval,reservations_cents=(True,)),
               replace(self.approval,reservations_cents=(4,)),replace(self.approval,job_cap_cents=2),
               replace(self.approval,reservations_cents=(3,3))]
        for approval in cases:
            with self.subTest(approval=approval),self.assertRaises(ValueError):self.execute(approval=approval)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.journal.db.execute('SELECT count(*) FROM operation').fetchone()[0],0)

    def test_changed_cap_or_text_cannot_bypass_completed_operation(self):
        self.execute()
        with self.assertRaises(ValueError):self.execute(approval=replace(self.approval,job_cap_cents=6))
        changed='Other synthetic words.'
        approval=replace(self.approval,plan_sha256=fingerprint(plan_requests(changed,profile())))
        with self.assertRaises(ValueError):self.execute(text=changed,approval=approval)
        self.assertEqual(len(self.calls),1)

    def test_two_span_plan_preserves_exact_text_and_per_operation_caps(self):
        text='abcdefg '*1800
        requests=plan_requests(text,profile())
        approval=SpendApproval('synthetic_approval',fingerprint(requests),(2,3),5,3)
        result=self.execute(text=text,approval=approval)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(''.join(x['exact_text'] for x in result['responses']),text)
        self.assertEqual(self.journal.db.execute('SELECT sum(reservation) FROM operation').fetchone()[0],5)

    def test_archived_response_tamper_fails_without_network(self):
        self.execute()
        next((self.root/'responses').glob('*.json')).write_bytes(b'bad')
        with self.assertRaises(ValueError):self.execute()
        self.assertEqual(len(self.calls),1)

    def test_http_default_disabled_never_reads_key(self):
        seen=[]
        transport=ElevenLabsHTTPTransport(lambda:seen.append(True))
        with self.assertRaises(RuntimeError):transport(self.requests[0])
        self.assertEqual(seen,[])

    def test_http_exact_endpoint_no_redirects_no_proxy_bounded_client(self):
        calls=[];options=[]
        def factory(**kwargs):options.append(kwargs);return FakeClient(calls,FakeResponse())
        transport=ElevenLabsHTTPTransport(lambda:'synthetic-key-never-live',enabled=True,client_factory=factory)
        wire=transport(self.requests[0])
        self.assertEqual(wire,response(self.text))
        self.assertEqual(options,[dict(timeout=90,follow_redirects=False,trust_env=False)])  # Whole-span wait, not a 10 s idle limit.
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][1]['json'],self.requests[0]['body'])
        self.assertNotIn('synthetic-key-never-live',repr(transport))

    def test_http_total_deadline_or_encoding_never_retries(self):
        for reply, clock in [(FakeResponse(),iter([0,0,1000]).__next__),
                             (FakeResponse(headers={'content-encoding':'gzip'}),lambda:0)]:
            calls=[]
            transport=ElevenLabsHTTPTransport(lambda:'synthetic-key-never-live',enabled=True,
                client_factory=lambda **kwargs:FakeClient(calls,reply),clock=clock)
            with self.assertRaises(ProviderOutcomeUnknown):transport(self.requests[0])
            self.assertEqual(len(calls),1)

    def test_http_error_redirect_timeout_or_missing_receipt_never_retries(self):
        for reply in [FakeResponse(status=429),FakeResponse(status=302),FakeResponse(headers={'request-id':''}),
                      FakeResponse(headers={'content-length':'999999999'}),TimeoutError('synthetic-key-never-live')]:
            calls=[]
            factory=lambda **kwargs:FakeClient(calls,reply)
            transport=ElevenLabsHTTPTransport(lambda:'synthetic-key-never-live',enabled=True,client_factory=factory)
            with self.subTest(reply=type(reply)),self.assertRaises(ProviderOutcomeUnknown) as error:transport(self.requests[0])
            self.assertEqual(len(calls),1)
            self.assertNotIn('synthetic-key-never-live',str(error.exception))

    def test_mutated_provider_endpoint_is_rejected_before_client(self):
        request={**self.requests[0],'url':'https://attacker.example/steal'}
        request['sha256']=fingerprint({k:v for k,v in request.items() if k!='sha256'})
        calls=[]
        transport=ElevenLabsHTTPTransport(lambda:'synthetic-key-never-live',enabled=True,client_factory=lambda **kwargs:calls.append(kwargs))
        with self.assertRaises(ValueError):transport(request)
        self.assertEqual(calls,[])

    def test_archive_is_exclusive_private_and_rejects_symlink(self):
        receipt=self.archive.write_response('synthetic_operation',response(self.text))
        path=self.root/'responses/synthetic_operation.json'
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError):self.archive.write_response('synthetic_operation',response(self.text))
        path.unlink();path.symlink_to(self.root/'outside')
        with self.assertRaises(ValueError):self.archive.read_response(receipt)




class FalExecutionChecks(unittest.TestCase):
    def setUp(self):
        import io
        from PIL import Image
        from byron_video.provider_execution import plan_anchored_motion,FAL_IMAGE_ENDPOINT
        self.temp=tempfile.TemporaryDirectory();self.journal=Journal(Path(self.temp.name)/'journal.sqlite')
        out=io.BytesIO();Image.new('RGB',(64,64),'blue').save(out,format='PNG');self.image=out.getvalue()
        import hashlib
        self.request=plan_anchored_motion(image_id='fixture_image_1',original_bytes=self.image,content_type='image/png',
            original_sha256=hashlib.sha256(self.image).hexdigest(),prompt='Synthetic gentle motion; preserve composition.')
        self.approval=SpendApproval('synthetic_approval',fingerprint([self.request]),(2,),5,2)
        base='https://queue.fal.run/minimax/h3-max-turbo/requests/synthetic_request_1'
        self.handle=dict(endpoint=FAL_IMAGE_ENDPOINT,request_id='synthetic_request_1',response_url=base,status_url=base+'/status')
    def tearDown(self):self.journal.close();self.temp.cleanup()
    def execute(self,transport,**overrides):
        from byron_video.provider_execution import submit_motion_once
        args=dict(owner_id='fixture_owner',job_id='fixture_job_1',request=self.request,approval=self.approval,
            journal=self.journal,transport=transport);args.update(overrides);return submit_motion_once(**args)

    def test_exact_anchored_schema_and_no_private_url(self):
        import base64
        body=self.request['body']
        self.assertEqual(body['image_url'],body['end_image_url'])
        self.assertEqual(base64.b64decode(body['image_url'].split(',')[1]),self.image)
        self.assertEqual(body['duration'],10);self.assertEqual(body['resolution'],'768P')
        self.assertEqual(body['prompt_expansion_mode'],'disabled');self.assertTrue(body['enable_safety_checker'])

    def test_submit_once_replay_reads_receipt_without_paid_repeat(self):
        from types import SimpleNamespace
        calls=[]
        def submit(request):
            self.assertEqual(self.journal.db.execute('SELECT state FROM operation').fetchone()[0],'may_have_submitted')
            calls.append(request);return self.handle
        transport=SimpleNamespace(submit=submit)
        self.assertEqual(self.execute(transport),self.handle)
        self.assertEqual(self.execute(transport),self.handle)
        self.assertEqual(len(calls),1)

    def test_unknown_motion_never_reposts(self):
        from types import SimpleNamespace
        calls=[]
        def fail(request):calls.append(request);raise TimeoutError('private input')
        transport=SimpleNamespace(submit=fail)
        with self.assertRaises(ProviderOutcomeUnknown):self.execute(transport)
        with self.assertRaises(ProviderOutcomeUnknown):self.execute(transport)
        self.assertEqual(len(calls),1)

    def test_default_disabled_and_untrusted_handles_do_not_read_keys(self):
        from byron_video.provider_execution import FalQueueTransport
        calls=[];transport=FalQueueTransport(lambda:calls.append(True))
        with self.assertRaises(RuntimeError):transport.submit(self.request)
        with self.assertRaises(ValueError):transport.status({**self.handle,'status_url':'https://attacker.example'})
        self.assertEqual(calls,[])

    def test_no_retry_queue_headers_and_no_key_on_cdn(self):
        from byron_video.provider_execution import FalQueueTransport
        calls=[];key_reads=[]
        replies=[FakeResponse(WireResponse('synthetic_request_1',json.dumps(self.handle).encode()),status=202),
                 FakeResponse(WireResponse('synthetic_request_1',json.dumps(dict(request_id='synthetic_request_1',status='COMPLETED')).encode())),
                 FakeResponse(WireResponse('synthetic_request_1',json.dumps(dict(video=dict(url='https://fal.media/files/synthetic.mp4',file_size=3,content_type='video/mp4'))).encode())),
                 FakeResponse(chunks=[b'abc'])]
        def factory(**kwargs):
            self.assertFalse(kwargs['follow_redirects']);self.assertFalse(kwargs['trust_env'])
            return FakeClient(calls,replies.pop(0))
        def key():key_reads.append(True);return 'synthetic-fal-key'
        transport=FalQueueTransport(key,enabled=True,client_factory=factory)
        handle=transport.submit(self.request);self.assertEqual(transport.status(handle),'COMPLETED')
        result=transport.result(handle);data=transport.download(result)
        self.assertEqual(data['bytes'],b'abc');self.assertFalse(data['media_verified'])
        self.assertEqual(len(key_reads),3)
        self.assertEqual(calls[0][1]['headers']['X-Fal-No-Retry'],'1')
        self.assertEqual(calls[0][1]['headers']['x-app-fal-disable-fallback'],'true')
        self.assertEqual(calls[-1][1]['headers'],{'Accept-Encoding':'identity'})
        self.assertEqual([call[0][0] for call in calls],['POST','GET','GET','GET'])

    def test_changed_duration_or_anchor_fails_before_transport(self):
        from byron_video.provider_execution import FalQueueTransport
        import copy
        for change in ['duration','end_image_url']:
            request=copy.deepcopy(self.request)
            request['body'][change]=5 if change=='duration' else 'https://fal.media/files/not-original.jpg'
            request['sha256']=fingerprint({k:v for k,v in request.items() if k!='sha256'})
            calls=[];transport=FalQueueTransport(lambda:calls.append(True),enabled=True)
            with self.assertRaises(ValueError):transport.submit(request)
            self.assertEqual(calls,[])

    def test_malformed_queue_receipt_remains_unknown(self):
        from types import SimpleNamespace
        calls=[]
        def submit(request):calls.append(request);return {**self.handle,'response_url':'https://attacker.example'}
        with self.assertRaises(ProviderOutcomeUnknown):self.execute(SimpleNamespace(submit=submit))
        with self.assertRaises(ProviderOutcomeUnknown):self.execute(SimpleNamespace(submit=submit))
        self.assertEqual(len(calls),1)


if __name__=='__main__':unittest.main(verbosity=2)
