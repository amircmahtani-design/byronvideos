"""Native Developer wire/receipt contracts with fake HTTP/cloud. No Google calls."""
import base64,copy,json,tempfile,unittest
from pathlib import Path
from byron_video import automated_review as review
from byron_video import gemini_developer as vertex
from byron_video.provider_execution import FileResponseStore,WireResponse,ProviderOutcomeUnknown
from byron_video.cloud_journal import FirestoreSpendJournal
from test_cloud_journal_stdlib import Client,PROJECT
from test_automated_review_stdlib import request_fixture,reply

NOW=1000.
CONTRACT='fixture_token_limit_contract'


def logical():
    old=request_fixture();body=old['body'];binding=body['binding']
    return review.prepare_request(owner_id=binding['owner_id'],job_id=binding['job_id'],image_id=binding['image_id'],sequence=0,
        prompt=body['approved_prompt_as_untrusted_data'],evidence=body['evidence'],config=vertex.config(PROJECT))


def native_response(request):
    return {'responseId':'fixture-native-id/with=encoding','modelVersion':vertex.MODEL,
        'usageMetadata':{'promptTokenCount':1000,'candidatesTokenCount':500,'thoughtsTokenCount':10,'totalTokenCount':1510},
        'candidates':[{'index':0,'finishReason':'STOP','content':{'role':'model','parts':[{'text':json.dumps(reply(request))}]}}]}


def input_proof(native):
    return {'reference':'fixture_exact_input_bound','native_request_sha256':review.fingerprint(native),'max_input_tokens':2000,
        'max_output_tokens':8192,'image_count':10,'expires_at':1500.,'method':'reviewed_count_tokens_bound',
        'evidence_sha256':'c'*64,'token_limit_contract_reference':CONTRACT,
        'billing_bound':{'reference':'synthetic_billing_evidence','evidence_sha256':'d'*64,'native_request_sha256':review.fingerprint(native),
            'generation_config_sha256':review.fingerprint(native['body']['generationConfig']),'model':vertex.MODEL,'endpoint':native['url'],
            'max_billable_output_tokens':8192,'includes_thought_tokens':True,'token_limit_contract_reference':CONTRACT,'expires_at':1500.}}


class DeveloperChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.request=logical();self.native=vertex.build_native_request(self.request,project=PROJECT)
        self.db=Client();self.owner=self.request['body']['binding']['owner_id'];self.job=self.request['body']['binding']['job_id']
        self.records=vertex.FirestoreDeveloperAuditRecords(self.db,project_id=PROJECT,namespace='fixture_vertex',owner_id=self.owner,job_id=self.job,transaction_runner=self.db.run)
        self.native_archive=FileResponseStore(self.root/'native');self.evidence=vertex.DeveloperNativeEvidence(archive=self.native_archive,records=self.records,project=PROJECT)
        self.store=vertex.DeveloperVerifiedReviewResponseStore(FileResponseStore(self.root/'semantic'),self.evidence)
        self.calls=[];self.token_calls=[];self.raw=vertex.encoded(native_response(self.request));self.status=200;self.headers={}
    def tearDown(self):self.temp.cleanup()
    def transport(self,**overrides):
        parent=self
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            @property
            def status_code(self):return parent.status
            @property
            def headers(self):return parent.headers
            def iter_bytes(self):yield parent.raw
        class HTTP:
            def __init__(self,**kwargs):parent.calls.append(('client',kwargs))
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def stream(self,*args,**kwargs):parent.calls.append(('request',args,kwargs));return Response()
        def token():self.token_calls.append(1);return 'synthetic-private-api-key'
        args=dict(project=PROJECT,key_reader=token,input_proof_loader=lambda request,native:input_proof(native),max_input_tokens=2000,
            token_limit_contract_reference=CONTRACT,native_evidence=self.evidence,activation_check=lambda:True,
            enabled=True,submission_check=lambda *args:True,client_factory=HTTP,now=lambda:NOW)
        args.update(overrides);return vertex.DeveloperReviewTransport(**args)
    def test_exact_developer_mapping_source_plus_nine_images(self):
        self.assertEqual(self.native['url'],vertex.endpoint(PROJECT));body=self.native['body'];cfg=body['generationConfig']
        self.assertEqual(cfg['thinkingConfig'],{'thinkingLevel':'MINIMAL','includeThoughts':False});self.assertEqual(cfg['mediaResolution'],'MEDIA_RESOLUTION_HIGH')
        self.assertEqual(cfg['maxOutputTokens'],8192);self.assertEqual(cfg['candidateCount'],1)
        self.assertEqual(cfg['responseMimeType'],'application/json');self.assertNotIn('additionalProperties',json.dumps(cfg['responseSchema']))
        parts=body['contents'][0]['parts'];images=[p['inlineData'] for p in parts if 'inlineData' in p]
        self.assertEqual(len(images),10);self.assertTrue(all(x['mimeType']=='image/jpeg' for x in images))
        self.assertEqual(images[0]['data'],self.request['body']['evidence']['source_sample']['jpeg_base64'])
        self.assertEqual([x['data'] for x in images[1:]],[s['jpeg_base64'] for s in self.request['body']['evidence']['samples']])
        self.assertIn('untrusted evidence',body['systemInstruction']['parts'][0]['text']);self.assertNotIn('tools',body)
    def test_count_tokens_is_pure_and_maps_official_direct_fields(self):
        count=vertex.build_count_tokens_request(self.request,project=PROJECT)
        self.assertTrue(count['url'].endswith(':countTokens'));self.assertEqual(count['body'],{'generateContentRequest':{'model':'models/'+vertex.MODEL,**self.native['body']}})
        result=vertex.parse_count_tokens(b'{"totalTokens":1200}',request=self.request,project=PROJECT)
        self.assertEqual(result['native_request_sha256'],review.fingerprint(self.native));self.assertFalse(result['is_guaranteed_upper_bound'])
        self.assertEqual(self.calls,[])
        for raw in (b'{"totalTokens":true}',b'{"totalTokens":0}',b'{"totalTokens":1,"totalTokens":2}'):
            with self.assertRaises(ValueError):vertex.parse_count_tokens(raw,request=self.request,project=PROJECT)
    def test_no_other_endpoint_or_model_fallback(self):
        changed=copy.deepcopy(self.request);changed['url']=changed['url'].replace('generativelanguage.googleapis.com','other.googleapis.com')
        with self.assertRaises(ValueError):vertex.build_native_request(changed,project=PROJECT)
        changed=copy.deepcopy(self.request);changed['body']['model']='gemini-other'
        with self.assertRaises(ValueError):vertex.build_native_request(changed,project=PROJECT)
    def test_disabled_before_token_proof_or_network(self):
        calls=[];transport=self.transport(enabled=False,input_proof_loader=lambda *a:calls.append(1))
        with self.assertRaises(ValueError):transport(self.request)
        self.assertEqual(calls+self.token_calls+self.calls,[])
    def test_missing_changed_expired_or_unfunded_input_proof_blocks_before_token(self):
        proof=input_proof(self.native)
        for bad in ({},{**proof,'native_request_sha256':'a'*64},{**proof,'expires_at':NOW},
                    {**proof,'image_count':6},{**proof,'max_input_tokens':2001},{**proof,'max_output_tokens':2000},
                    {**proof,'token_limit_contract_reference':'different_contract'}):
            with self.assertRaises(ValueError):self.transport(input_proof_loader=lambda *a,b=bad:b)(self.request)
        self.assertEqual(self.token_calls+self.calls,[])
    def test_missing_unscoped_or_expired_combined_billing_proof_blocks_before_token(self):
        proof=input_proof(self.native)
        for bound in ({},{**proof['billing_bound'],'includes_thought_tokens':False},{**proof['billing_bound'],'max_billable_output_tokens':2000},
                      {**proof['billing_bound'],'generation_config_sha256':'e'*64},{**proof['billing_bound'],'expires_at':999}):
            with self.assertRaises(ValueError):self.transport(input_proof_loader=lambda *a,b=bound:{**proof,'billing_bound':b})(self.request)
        self.assertEqual(self.calls+self.token_calls,[])
    def test_billing_expiry_during_token_refresh_blocks_before_post(self):
        ticks=[NOW]
        def token():ticks[0]=NOW+20;return 'synthetic-private-api-key'
        proof=input_proof(self.native);proof['billing_bound']['expires_at']=NOW+10
        transport=self.transport(key_reader=token,now=lambda:ticks[0],input_proof_loader=lambda *a:proof)
        with self.assertRaises(ProviderOutcomeUnknown):transport(self.request)
        self.assertFalse(any(row[0]=='request' for row in self.calls))
    def test_final_funded_quote_verifier_rechecks_after_token_before_post(self):
        calls=[]
        def token():calls.append('token');return 'synthetic-private-api-key'
        def funded(*args):calls.append('funding');return False
        with self.assertRaises(ProviderOutcomeUnknown):self.transport(key_reader=token,submission_check=funded)(self.request)
        self.assertEqual(calls,['token','funding']);self.assertFalse(any(row[0]=='request' for row in self.calls))
        with self.assertRaises(ValueError):self.transport(submission_check=None)(self.request)
    def test_only_explicit_bounded_private_key_accepted(self):
        for token in ('short','bad key with whitespace and spaces',object(),'x'*513):
            with self.assertRaises(ValueError):self.transport(key_reader=lambda t=token:t)(self.request)
        self.assertEqual(self.calls,[])
    def test_native_transport_archives_exact_bytes_usage_and_derived_id_before_return(self):
        wire=self.transport()(self.request)
        self.assertTrue(wire.request_id.startswith(vertex.ID_PREFIX));self.assertNotEqual(wire.request_id,native_response(self.request)['responseId'])
        row=self.evidence.verify_for_wire(wire,expected_request=self.request)
        self.assertEqual(row['metadata']['native_response_id'],native_response(self.request)['responseId'])
        self.assertEqual(row['metadata']['model_version'],vertex.MODEL);self.assertEqual(row['metadata']['usage_metadata']['totalTokenCount'],1510)
        archived=self.native_archive.read_response(row['archive_receipt']);audit=json.loads(archived.payload)
        self.assertEqual(base64.b64decode(audit['native_response_base64']),self.raw)
        self.assertEqual(base64.b64decode(audit['native_body_base64']),self.calls[1][2]['content'])
        self.assertEqual(self.calls[1][1],('POST',vertex.endpoint(PROJECT)))
        self.assertFalse(self.calls[0][1]['follow_redirects']);self.assertFalse(self.calls[0][1]['trust_env'])
        self.assertNotIn('synthetic-private-api-key',archived.payload.decode())
        self.assertEqual(self.calls[1][2]['headers']['x-goog-api-key'],'synthetic-private-api-key')
        self.assertNotIn('Authorization',self.calls[1][2]['headers'])
    def test_native_thought_text_never_selected_as_semantic_output(self):
        value=native_response(self.request);value['candidates'][0]['content']['parts'].insert(0,{'text':'not semantic JSON','thought':True})
        wire,metadata=vertex.parse_native_response(vertex.encoded(value),self.request,input_proof=input_proof(self.native))
        self.assertEqual(json.loads(wire.payload)['binding'],self.request['body']['binding'])
        value['candidates'][0]['content']['parts']=value['candidates'][0]['content']['parts'][:1]
        with self.assertRaises(ValueError):vertex.parse_native_response(vertex.encoded(value),self.request,input_proof=input_proof(self.native))
    def test_signature_only_metadata_is_preserved_but_not_semantic_text(self):
        value=native_response(self.request);value['candidates'][0]['content']['parts'].insert(0,{'thoughtSignature':'opaque_fixture_signature'})
        payload,metadata=vertex.parse_developer_response(vertex.encoded(value),max_input_tokens=2000)
        self.assertEqual(json.loads(payload)['schema'],review.SCHEMA)
    def test_missing_id_version_usage_blocking_finish_and_extra_candidates_fail_closed(self):
        cases=[]
        for key in ('responseId','modelVersion','usageMetadata'):
            value=native_response(self.request);del value[key];cases.append(value)
        value=native_response(self.request);value['modelVersion']='gemini-different';cases.append(value)
        value=native_response(self.request);value['promptFeedback']={'blockReason':'SAFETY'};cases.append(value)
        value=native_response(self.request);value['candidates'][0]['finishReason']='MAX_TOKENS';cases.append(value)
        value=native_response(self.request);value['candidates']*=2;cases.append(value)
        value=native_response(self.request);value['candidates'][0]['content']['parts']=[{'functionCall':{'name':'escape'}}];cases.append(value)
        for value in cases:
            with self.assertRaises(ValueError):vertex.parse_native_response(vertex.encoded(value),self.request,input_proof=input_proof(self.native))
    def test_usage_is_additive_thoughts_bounded_tools_unpriced_and_cache_subset(self):
        for change in ({'totalTokenCount':1000},{'promptTokenCount':2001,'totalTokenCount':2511},
                       {'thoughtsTokenCount':8192,'totalTokenCount':9692},{'toolUsePromptTokenCount':1},
                       {'cachedContentTokenCount':1001}):
            value=native_response(self.request);value['usageMetadata'].update(change)
            with self.assertRaises(ValueError):vertex.parse_native_response(vertex.encoded(value),self.request,input_proof=input_proof(self.native))
    def test_non_native_wire_cannot_spoof_input_archive_role(self):
        payload=json.dumps(reply(self.request)).encode()
        for identifier in ('review_input_forged','review_input_'+review.sha(payload)):
            with self.assertRaises(ValueError):self.store.write_response('fixture_output',WireResponse(identifier,payload))
        data=vertex.encoded(self.request);wire=WireResponse('review_input_'+review.sha(data),data)
        with self.assertRaises(ValueError):self.store.write_response('fixture_output',wire)
        self.store.write_response('fixture_input',wire)
    def test_review_once_replay_independently_verifies_native_archive_without_new_call(self):
        journal=FirestoreSpendJournal(self.db,project_id=PROJECT,namespace='fixture_vertex',owner_id=self.owner,job_id=self.job,
            budget_scope='fixture_budget',approved_total_cap_cents=100,prior_spend_cents=0,transaction_runner=self.db.run)
        budget={'scope_sha256':review.sha(b'fixture_budget'),'approved_total_cap_cents':100,'prior_spend_cents':0,'job_cap_cents':20}
        quote={'reference':'fixture_quote','request_sha256':review.fingerprint(self.request),'budget_sha256':review.fingerprint(budget),
            'reservation_cents':2,'job_cap_cents':20,'operation_cap_cents':3,'valid_until_epoch':1500.}
        args=dict(request=self.request,quote=quote,trusted_budget=budget,journal=journal,response_store=self.store,transport=self.transport(),activation_check=lambda:True,enabled=True)
        first=review.review_once(**args,now=lambda:NOW);calls=len(self.calls)
        self.assertEqual(review.review_once(**args,now=lambda:2000.),first);self.assertEqual(len(self.calls),calls)
        next((self.root/'native').glob('*.json')).write_bytes(b'{}')
        with self.assertRaises(ValueError):review.review_once(**args,now=lambda:2000.)
        self.assertEqual(len(self.calls),calls)
    def test_native_audit_failure_or_http_error_never_returns_acceptance(self):
        self.native_archive.write_response=lambda *a:(_ for _ in ()).throw(TimeoutError())
        with self.assertRaises(ProviderOutcomeUnknown):self.transport()(self.request)
        self.assertEqual(len(self.calls),2)
        self.status=302
        with self.assertRaises(ProviderOutcomeUnknown):self.transport()(self.request)
        self.assertEqual(len(self.calls),4)
    def test_native_bounds_and_encoded_response_fail_without_retry(self):
        self.headers={'content-encoding':'gzip'}
        with self.assertRaises(ProviderOutcomeUnknown):self.transport()(self.request)
        self.headers={};self.raw=b'x'*(vertex.MAX_NATIVE_BYTES+1)
        with self.assertRaises(ProviderOutcomeUnknown):self.transport()(self.request)
        self.assertEqual(len(self.calls),4)
    def test_native_generation_config_cannot_add_tools_or_change_output_cap(self):
        transport=self.transport();proof=input_proof(self.native)
        for field in ('tools','output','thinking'):
            native=copy.deepcopy(self.native)
            if field=='tools':native['body']['tools']=[{'googleSearch':{}}]
            elif field=='output':native['body']['generationConfig']['maxOutputTokens']=10000
            else:native['body']['generationConfig']['thinkingConfig']['thinkingLevel']='HIGH'
            proof['native_request_sha256']=review.fingerprint(native)
            with self.assertRaises(ValueError):transport.http(native,proof,image_count=10)
        self.assertEqual(self.calls+self.token_calls,[])
    def test_per_call_funding_callback_is_not_transport_global_state(self):
        transport=self.transport(submission_check=None);checks=[]
        def funded():checks.append('checked');return True
        wire=transport.submit_funded(self.request,funded)
        self.assertTrue(wire.request_id.startswith(vertex.ID_PREFIX));self.assertEqual(checks,['checked'])
        self.assertIsNone(transport.submission_check)
    def test_native_cost_properties_cannot_be_assigned(self):
        transport=self.transport()
        for field in ('max_input_tokens','max_output_tokens','token_limit_contract_reference'):
            with self.assertRaises(AttributeError):setattr(transport,field,1)
        self.assertEqual(transport.max_output_tokens,8192)



class DeveloperCountChecks(unittest.TestCase):
    def test_complete_count_wrapper_and_key_only_header(self):
        native=vertex.build_native_request(logical(),project=PROJECT);calls=[]
        class Response:
            status_code=200;headers={}
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def iter_bytes(self):yield b'{"totalTokens":1200}'
        class HTTP:
            def __init__(self,**kw):calls.append(kw)
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def close(self):pass
            def stream(self,*a,**kw):calls.append((a,kw));return Response()
        transport=vertex.DeveloperCountHTTP(project=PROJECT,key_reader=lambda:'synthetic-private-api-key',activation_check=lambda:True,enabled=True,client_factory=HTTP)
        raw=transport(native,submission_check=lambda:True);self.assertEqual(vertex.parse_count(raw)['totalTokens'],1200)
        args,kw=calls[1];self.assertEqual(args,('POST',vertex.endpoint(PROJECT).replace(':generateContent',':countTokens')))
        self.assertEqual(json.loads(kw['content']),{'generateContentRequest':{'model':'models/'+vertex.MODEL,**native['body']}})
        self.assertEqual(kw['headers']['x-goog-api-key'],'synthetic-private-api-key');self.assertNotIn('Authorization',kw['headers'])
        self.assertFalse(calls[0]['trust_env']);self.assertFalse(calls[0]['follow_redirects'])
    def test_count_disabled_unknown_fields_and_malformed_count(self):
        native=vertex.build_native_request(logical(),project=PROJECT);calls=[]
        transport=vertex.DeveloperCountHTTP(project=PROJECT,key_reader=lambda:calls.append('key'),activation_check=lambda:True)
        with self.assertRaises(ValueError):transport(native,submission_check=lambda:True)
        self.assertFalse(calls)
        for raw in (b'{"totalTokens":true}',b'{"totalTokens":2,"unknown":1}',b'{"totalTokens":2,"cachedContentTokenCount":1}',b'{"totalTokens":2,"totalTokens":3}'):
            with self.assertRaises(ValueError):vertex.parse_count(raw)
    def test_count_rejects_vertex_endpoint_and_mutated_controls(self):
        native=vertex.build_native_request(logical(),project=PROJECT)
        for change in ('endpoint','controls'):
            n=copy.deepcopy(native)
            if change=='endpoint':n['url']='https://aiplatform.eu.rep.googleapis.com/v1/fake'
            else:n['body']['generationConfig']['thinkingConfig']['thinkingLevel']='HIGH'
            with self.assertRaises(ValueError):vertex.count_request(n,PROJECT)

if __name__=="__main__":unittest.main()
