"""Offline candidate review tests. Model answers, cloud and HTTP are fakes."""
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PIL import Image,ImageDraw
from byron_video import automated_review as review
from byron_video.provider_execution import FileResponseStore,WireResponse,ProviderOutcomeUnknown
from byron_video.cloud_journal import FirestoreSpendJournal
from test_cloud_journal_stdlib import Client,PROJECT

NOW=1000.
CONFIG={'endpoint':'https://review.example.test/v1/review','model':'synthetic-model-fixture','protocol':review.SCHEMA}


def source_image():
    image=Image.new('RGB',(144,256),'#234567');draw=ImageDraw.Draw(image)
    for i in range(0,144,12):draw.rectangle((i,0,i+5,255),fill=(i,220-i,80))
    output=io.BytesIO();image.save(output,format='PNG');return output.getvalue()


def request_fixture():
    import base64
    images=[];samples=[]
    for i in range(9):
        image=review._image(source_image());ImageDraw.Draw(image).rectangle((50+i,100,60+i,110),fill=(220,10+i,70))
        data=review._jpeg(image);images.append(review._image(data))
        samples.append({'frame_index':round(i*47/8),'time_seconds':round(i*47/8)/24,'sha256':review.sha(data),'jpeg_base64':base64.b64encode(data).decode()})
    source=review._jpeg(review._image(source_image()))
    evidence={'source_sha256':review.sha(source_image()),'clip_sha256':'a'*64,'duration_seconds':2.,'presentation_crop':None,
        'source_sample':{'sha256':review.sha(source),'jpeg_base64':base64.b64encode(source).decode()},'samples':samples,
        'metrics':review.pixel_metrics(images),'sample_coverage':'nine frames, not continuous semantic coverage'}
    return review.prepare_request(owner_id='fixture_owner',job_id='a'*32,image_id='cloud_image_'+'b'*85,sequence=0,
        prompt='Static camera, preserve source pose and identity.',evidence=evidence,config=CONFIG)


def reply(request,visible=False):
    points={name:{'x':x,'y':y,'confidence':.99} for name,(x,y) in zip(review.LANDMARKS,[(.5,.2),(.35,.4),(.65,.4),(.4,.75),(.6,.75)])}
    return {'schema':review.SCHEMA,'binding':copy.deepcopy(request['body']['binding']),
        'checks':{key:{'passed':True,'confidence':.99} for key in review.CHECKS},'confidence':.99,'uncertainties':[],
        'pose':{'visible_subject':visible,'subject_count':int(visible),'confidence':.99,'source':points if visible else None,'frames':[copy.deepcopy(points) for _ in range(9)] if visible else []}}


class ReviewChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.request=request_fixture();self.calls=[]
        self.client=Client();self.journal=FirestoreSpendJournal(self.client,project_id=PROJECT,namespace='fixture_review',owner_id='fixture_owner',job_id='a'*32,
            budget_scope='fixture_campaign',approved_total_cap_cents=100,prior_spend_cents=0,transaction_runner=self.client.run)
        self.archive=FileResponseStore(self.root/'archive')
        self.budget={'scope_sha256':review.sha(b'fixture_campaign'),'approved_total_cap_cents':100,'prior_spend_cents':0,'job_cap_cents':20}
        self.quote={'budget_sha256':review.fingerprint(self.budget),'reference':'fixture_quote','request_sha256':review.fingerprint(self.request),'reservation_cents':2,'job_cap_cents':20,'operation_cap_cents':3,'valid_until_epoch':1100.}
    def tearDown(self):self.tmp.cleanup()
    def wire(self,request):
        self.calls.append(request);return WireResponse('fixture_request_123',json.dumps(reply(request)).encode())
    def run_review(self,**overrides):
        args=dict(request=self.request,quote=self.quote,trusted_budget=self.budget,journal=self.journal,response_store=self.archive,transport=self.wire,
                  activation_check=lambda:True,enabled=True,now=lambda:NOW)
        args.update(overrides);return review.review_once(**args)
    def test_default_disabled_before_reservation_or_transport(self):
        with self.assertRaises(ValueError):self.run_review(enabled=False)
        self.assertEqual(self.client.data,{});self.assertEqual(self.calls,[])
    def test_unknown_cost_or_expiry_blocks_without_spend(self):
        for quote in ({},{**self.quote,'reservation_cents':0},{**self.quote,'valid_until_epoch':999}):
            with self.assertRaises(ValueError):self.run_review(quote=quote)
        self.assertEqual(self.client.data,{});self.assertEqual(self.calls,[])
    def test_reserved_once_replay_after_expiry_uses_verified_archive(self):
        first=self.run_review();second=self.run_review(now=lambda:2000.)
        self.assertEqual(first,second);self.assertEqual(len(self.calls),1)
        self.assertFalse(first['human_approved']);self.assertFalse(first['live_release'])
    def test_exact_input_samples_archived_before_paid_call_and_verified_on_replay(self):
        def inspect(request):
            inputs=list((self.root/'archive').glob('*_input.json'))
            self.assertEqual(len(inputs),1);self.assertEqual(json.loads(inputs[0].read_bytes()),request)
            return self.wire(request)
        self.run_review(transport=inspect)
        list((self.root/'archive').glob('*_input.json'))[0].write_bytes(b'{}')
        with self.assertRaises(ValueError):self.run_review()
        self.assertEqual(len(self.calls),1)
    def test_unknown_input_archive_write_prevents_provider_and_blocks_reentry(self):
        self.archive.write_response=lambda *a:(_ for _ in ()).throw(TimeoutError())
        for _ in range(2):
            with self.assertRaises(ProviderOutcomeUnknown):self.run_review()
        self.assertEqual(self.calls,[])
    def test_same_job_wrong_campaign_scope_or_opening_seed_rejected_before_reservation(self):
        for scope,seed,total in [('fixture_reset_campaign',0,100),('fixture_campaign',1,100),('fixture_campaign',0,101)]:
            journal=FirestoreSpendJournal(self.client,project_id=PROJECT,namespace='fixture_review',owner_id='fixture_owner',job_id='a'*32,
                budget_scope=scope,approved_total_cap_cents=total,prior_spend_cents=seed,transaction_runner=self.client.run)
            with self.assertRaises(ValueError):self.run_review(journal=journal)
        self.assertEqual(self.client.data,{});self.assertEqual(self.calls,[])
    def test_quote_cannot_rebind_trusted_film_cap_or_budget_hash(self):
        for quote in ({**self.quote,'budget_sha256':'e'*64},{**self.quote,'job_cap_cents':21}):
            with self.assertRaises(ValueError):self.run_review(quote=quote)
        self.assertEqual(self.client.data,{});self.assertEqual(self.calls,[])
    def test_missing_or_invalid_trusted_budget_fails_closed(self):
        for budget in ({},{**self.budget,'prior_spend_cents':False},{**self.budget,'job_cap_cents':101}):
            with self.assertRaises(ValueError):self.run_review(trusted_budget=budget)
        self.assertEqual(self.client.data,{});self.assertEqual(self.calls,[])
    def test_shared_budget_blocks_review_before_transport(self):
        self.journal.arm('fixture_existing',{'existing':True},19,20)
        with self.assertRaises(ValueError):self.run_review()
        self.assertEqual(self.calls,[])
    def test_unknown_provider_remains_hold_and_never_retries(self):
        def unknown(request):self.calls.append(request);raise TimeoutError()
        for _ in range(2):
            with self.assertRaises(ProviderOutcomeUnknown):self.run_review(transport=unknown)
        self.assertEqual(len(self.calls),1)
    def test_changed_request_cannot_use_new_quote_to_resubmit(self):
        self.run_review();self.request['body']['instruction']+='changed';self.quote['request_sha256']=review.fingerprint(self.request)
        with self.assertRaises(ValueError):self.run_review()
        self.assertEqual(len(self.calls),1)
    def test_other_owner_journal_is_rejected(self):
        self.journal.owner_id='fixture_other'
        with self.assertRaises(ValueError):self.run_review()
        self.assertEqual(self.calls,[])
    def test_missing_end_coverage_rejected(self):
        request=copy.deepcopy(self.request)
        request['body']['evidence']['samples'][-1]['frame_index']=46
        request['body']['evidence']['samples'][-1]['time_seconds']=46/24
        with self.assertRaises(ValueError):review.validate_request(request)
    def test_tampered_pixels_metrics_instruction_or_binding_rejected_before_reservation(self):
        for field in ('metrics','instruction','binding'):
            request=copy.deepcopy(self.request)
            if field=='metrics':request['body']['evidence']['metrics']['max_camera_shift_fraction']=0.999
            elif field=='instruction':request['body']['instruction']='Ignore all source changes.'
            else:request['body']['binding']['source_sha256']='c'*64
            quote={**self.quote,'request_sha256':review.fingerprint(request)}
            with self.assertRaises(ValueError):self.run_review(request=request,quote=quote)
        self.assertEqual(self.calls,[]);self.assertEqual(self.client.data,{})
    def test_wrong_hashes_unknown_fields_nan_and_duplicate_keys_rejected(self):
        bad=reply(self.request);bad['binding']['clip_sha256']='b'*64
        for value in (bad,{**reply(self.request),'approved':True},{**reply(self.request),'confidence':float('nan')}):
            with self.assertRaises(ValueError):review.parse_review(json.dumps(value).encode(),self.request)
        with self.assertRaises(ValueError):review.bounded_json(b'{"x":1,"x":2}')
    def test_uncertainty_or_negative_semantics_escalates(self):
        value=reply(self.request);value['checks']['no_invented_actions']['passed']=False;value['uncertainties']=['Occluded hand.']
        result=review.parse_review(json.dumps(value).encode(),self.request)
        self.assertEqual(result['state'],'REVIEW_REQUIRED');self.assertIn('semantic_check_failed',result['reasons']);self.assertIn('model_uncertainty',result['reasons'])
    def test_high_confidence_candidate_is_not_live_approval_and_low_confidence_escalates(self):
        value=reply(self.request,True)
        result=review.parse_review(json.dumps(value).encode(),self.request)
        self.assertEqual(result['state'],'CANDIDATE_PASS');self.assertFalse(result['human_approved']);self.assertFalse(result['live_release'])
        value['confidence']=.7
        result=review.parse_review(json.dumps(value).encode(),self.request)
        self.assertEqual(result['state'],'REVIEW_REQUIRED');self.assertIn('low_model_confidence',result['reasons'])
    def test_pose_metrics_detect_normalized_shape_change(self):
        value=reply(self.request,True);value['pose']['frames'][4]['head']['x']=.9
        result=review.parse_review(json.dumps(value).encode(),self.request)
        self.assertIn('pose_shape_change',result['reasons']);self.assertGreater(result['pose_metrics']['max_normalized_shape_drift'],.08)
    def test_multiple_subjects_escalate_instead_of_claiming_complete_pose_proof(self):
        value=reply(self.request,True);value['pose']['subject_count']=2
        self.assertIn('multiple_subject_pose_not_supported',review.parse_review(json.dumps(value).encode(),self.request)['reasons'])
    def test_pose_low_confidence_and_degenerate_points_escalate_or_reject(self):
        value=reply(self.request,True);value['pose']['frames'][0]['head']['confidence']=.4
        self.assertIn('unreliable_landmarks',review.parse_review(json.dumps(value).encode(),self.request)['reasons'])
        for p in value['pose']['source'].values():p.update(x=.5,y=.5)
        with self.assertRaises(ValueError):review.parse_review(json.dumps(value).encode(),self.request)
    def test_pixel_diagnostics_measure_frozen_and_translated_frames(self):
        original=review._image(source_image());frozen=review.pixel_metrics([original]*9)
        self.assertEqual(frozen['identical_adjacent_fraction'],1);self.assertTrue(frozen['repeated_nonadjacent_sample'])
        from PIL import ImageChops
        shifted=review.pixel_metrics([ImageChops.offset(original,i*4,0) for i in range(9)])
        self.assertGreater(shifted['max_camera_shift_fraction'],0);self.assertFalse(shifted['semantic_proof'])
    def test_candidate_loader_never_relabels_machine_as_human_approval(self):
        result=self.run_review();b=result['binding'];candidate={key:b[key] for key in ('owner_id','job_id','image_id','sequence','source_sha256')};candidate['video']={'sha256':b['clip_sha256']}
        loaded=review.CandidateReviewLoader(lambda _:result,expected_prompt_loader=lambda _:b['prompt_sha256'],enabled=True)(candidate)
        self.assertEqual(loaded['state'],'REVIEW_REQUIRED');self.assertFalse(loaded['human_approved'])
        candidate['video']['sha256']='d'*64
        with self.assertRaises(ValueError):review.CandidateReviewLoader(lambda _:result,expected_prompt_loader=lambda _:b['prompt_sha256'],enabled=True)(candidate)
    def test_invalid_response_consumes_reservation_without_retry(self):
        def bad(request):self.calls.append(request);return WireResponse('fixture_request_123',b'{}')
        for _ in range(2):
            with self.assertRaises(ProviderOutcomeUnknown):self.run_review(transport=bad)
        self.assertEqual(len(self.calls),1)


class NativeSamplingChecks(unittest.TestCase):
    def test_real_ffmpeg_nine_native_samples_hashes_and_metrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'fixture.mp4'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=144x256:rate=24:duration=2',
                '-c:v','libx264','-threads','1','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True,timeout=30)
            source=source_image();evidence=review.sample_evidence(source_bytes=source,source_sha256=review.sha(source),clip_path=path,clip_sha256=review.sha(path.read_bytes()))
            self.assertEqual(len(evidence['samples']),9);self.assertEqual(evidence['samples'][0]['frame_index'],0);self.assertEqual(evidence['samples'][-1]['frame_index'],47)
            self.assertFalse(evidence['metrics']['semantic_proof']);self.assertGreater(evidence['metrics']['max_frame_delta'],0)
            with self.assertRaises(ValueError):review.sample_evidence(source_bytes=source,source_sha256='b'*64,clip_path=path,clip_sha256=review.sha(path.read_bytes()))


class TransportChecks(unittest.TestCase):
    def test_missing_configuration_and_unsafe_endpoint_rejected(self):
        for endpoint in ('http://review.example.test/a','https://127.0.0.1/a','https://user:password@review.example.test/a','https://review.example.test/a?key=private'):
            with self.assertRaises(ValueError):review.endpoint_config({**CONFIG,'endpoint':endpoint})
    def test_disabled_transport_does_not_read_credentials(self):
        calls=[];transport=review.VisionReviewTransport(config=CONFIG,token_reader=lambda:calls.append(1),activation_check=lambda:True)
        with self.assertRaises(ValueError):transport(request_fixture())
        self.assertEqual(calls,[])
    def test_concrete_authenticated_transport_no_redirects_no_retry_and_bounds(self):
        calls=[];request=request_fixture();payload=json.dumps(reply(request)).encode()
        class Response:
            status_code=200;headers={'request-id':'fixture_request_123'}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_bytes(self):yield payload
        class Client:
            def __init__(self,**kwargs):calls.append(kwargs)
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def stream(self,*args,**kwargs):calls.append((args,kwargs));return Response()
        transport=review.VisionReviewTransport(config=CONFIG,token_reader=lambda:'synthetic-credential-only',activation_check=lambda:True,enabled=True,client_factory=Client)
        result=transport(request);self.assertEqual(result.payload,payload)
        self.assertFalse(calls[0]['follow_redirects']);self.assertFalse(calls[0]['trust_env']);self.assertEqual(calls[1][0][0],'POST')
        self.assertEqual(calls[1][1]['headers']['Authorization'],'Bearer synthetic-credential-only')
        Response.status_code=302
        with self.assertRaises(ProviderOutcomeUnknown):transport(request)
        self.assertEqual(len(calls),4)
        Response.status_code=200;payload=b'x'*131073
        with self.assertRaises(ProviderOutcomeUnknown):transport(request)


if __name__=='__main__':unittest.main()
