"""Actual packaged Developer graph with synthetic external SDK/HTTP boundaries.

Exercises fresh API upload/Generate, worker construction, count preflight and
native source planning. Does not repeat the unchanged whole-film renderer.
"""
import copy,hashlib,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_production_fresh_flow_stdlib import ProductionFixture,base_config,textured,VOICE
from byron_video import gemini_developer as native,source_planning as sp
from byron_video.elevenlabs_adapter import fingerprint
from byron_video.hosted_control import canonical

class DeveloperFixture(ProductionFixture):
    def configure_policy(self,config,processing_plan):
        super().configure_policy(config,processing_plan)
        p=copy.deepcopy(self.policy_envelope['payload']);p['native_api_mode']='gemini_developer'
        p['native_disclosure']=dict(paid_service=True,non_eu_exclusive_processing=True,abuse_retention_days=55,data_disclosure_authorized=True)
        p['source_config'].update(native_api_mode='gemini_developer',max_output_tokens=3072)
        p['review_quote'].update(config=native.config(p['project']),max_output_tokens=8192,input_usd_per_million='0.25',output_usd_per_million='1.50')
        p['review_quote']['source_planning']['max_output_tokens']=3072
        billing=copy.deepcopy(self.billing_envelope['payload']);billing.update(purpose='byron-gemini-developer-billable-output-contract-v1',endpoint=native.endpoint(p['project']),compiler_sha256s={name:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for name,module in [('gemini_developer',native),('source_planning',sp)]},billed_controls=[])
        billing['evidence_url']='https://ai.google.dev/gemini-api/docs/generate-content/thinking#token-limits-and-max_output_tokens'
        for n in (3072,8192):
            generation=native.build_vertex_request(project=p['project'],system_instruction='bound',parts=[{'text':'bound'}],response_json_schema={},max_output_tokens=n)['body']['generationConfig'];generation.pop('responseSchema')
            billing['billed_controls'].append(dict(generation_controls=generation,max_billable_output_tokens=n))
        p['token_preflight']['billing_contract_sha256']=fingerprint(billing)
        self.policy_envelope=self.sign(p);self.billing_envelope=self.sign(billing)
    def submit(self):
        api=self.api;cap=api.client.get('/api/creator-capabilities');assert cap.status_code==200,cap.text
        api.headers={'Origin':api.config['identity']['origin'],'X-CSRF-Token':cap.json()['csrf_token'],'Idempotency-Key':'developer_fresh_generate_001'}
        originals=[]
        for i in range(4):
            b=io.BytesIO();textured(i).save(b,format='PNG');originals.append(b.getvalue())
        uploaded=api.client.post('/api/uploads',files=[('images',(f'{i}.png',data,'image/png')) for i,data in enumerate(originals)],headers=api.headers);assert uploaded.status_code==201,uploaded.text
        api.body=dict(script=self.media.job.snapshot['raw_script'],images=uploaded.json()['images'],voice_id=VOICE)
        submitted=api.client.post('/api/jobs',json=api.body,headers=api.headers);assert submitted.status_code==202,submitted.text
        self.job=api.runtime.jobs.get(api.owner,submitted.json()['id']);return self.job

class DeveloperPackagedTests(unittest.TestCase):
    def test_fresh_api_worker_source_uses_selected_developer_count_and_native_transport(self):
        cfg=base_config();cfg['native_api_mode']='gemini_developer';cfg['secret_resources']['gemini']='projects/synthetic-project/secrets/synthetic_gemini'
        with tempfile.TemporaryDirectory() as d,patch('test_production_fresh_flow_stdlib.base_config',lambda:copy.deepcopy(cfg)):
            f=DeveloperFixture(Path(d))
            try:
                job=f.submit();worker,_=f.worker();coordinator=worker.bindings.generation_factory(job,worker)
                self.assertIs(type(coordinator.source_planner.transport),native.NativeDeveloperHTTP)
                originals={image['id']:coordinator.originals(job,image) for image in job.snapshot['images']}
                root=Path(d)/'developer-stage';root.mkdir()
                narration=coordinator.narration_stage(job,root/'narration')
                result=coordinator.source_planner(job=job,originals=originals,narration_report=narration['report'],root=root/'source')
                self.assertEqual(result['diversity_state'],'varied_proposal')
                external=[url for _,url in f.http_calls if url.endswith((':generateContent',':countTokens'))]
                self.assertEqual(len(external),2)
                self.assertTrue(all(url.startswith('https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:') for url in external))
                self.assertFalse(any('aiplatform' in url for _,url in f.http_calls))
                before=list(f.http_calls)
                again=coordinator.source_planner(job=job,originals=originals,narration_report=narration['report'],root=root/'source-again')
                self.assertEqual(result,again);self.assertEqual(f.http_calls,before)
                # Concrete packaged QC factory and exact-count proof path, using
                # labelled synthetic sampled evidence; no extra film render.
                from test_automated_review_stdlib import request_fixture
                from byron_video.automated_review import prepare_request,validate_request
                reviewer=coordinator.review_loader
                self.assertIs(type(reviewer.transport),native.DeveloperReviewTransport)
                self.assertIs(type(reviewer.transport.http),native.NativeDeveloperHTTP)
                self.assertIs(type(reviewer.responses),native.DeveloperVerifiedReviewResponseStore)
                self.assertIs(type(reviewer.transport.evidence),native.DeveloperNativeEvidence)
                self.assertIs(reviewer.responses.evidence,reviewer.transport.evidence)
                evidence=copy.deepcopy(request_fixture()['body']['evidence'])
                image=job.snapshot['images'][0];evidence['source_sha256']=image['sha256']
                logical=prepare_request(owner_id=job.owner_id,job_id=job.id,image_id=image['id'],sequence=0,
                    prompt='Static camera, preserve source pose and identity.',evidence=evidence,config=native.config(cfg['target']['project_id']))
                validate_request(logical)
                request=native.build_native_request(logical,project=cfg['target']['project_id'])
                proof=reviewer.transport.proof_loader(logical,request)
                self.assertEqual(proof['image_count'],10)
                self.assertEqual(proof['max_output_tokens'],8192)
                self.assertEqual(proof['billing_bound']['endpoint'],native.endpoint(cfg['target']['project_id']))
                self.assertEqual(f.http_calls[-1][1],native.endpoint(cfg['target']['project_id']).replace(':generateContent',':countTokens'))
                after=list(f.http_calls)
                self.assertEqual(proof,reviewer.transport.proof_loader(logical,request))
                self.assertEqual(f.http_calls,after)
                # Input proof is not permission to bypass whole-film funding.
                with self.assertRaises(ValueError):reviewer.transport(logical)
                self.assertEqual(f.http_calls,after)
            finally:f.close()

if __name__=='__main__':unittest.main()
