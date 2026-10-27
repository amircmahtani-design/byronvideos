"""Disabled exact-native countTokens preflight and independently signed billing bounds.

Google v1 CountTokensRequest supports contents, system_instruction and generation_config:
https://github.com/googleapis/googleapis/blob/master/google/cloud/aiplatform/v1/prediction_service.proto
Routing: google/cloud/aiplatform/v1/llm_utility_service.proto. No response-output
billing guarantee follows from that schema; a separate reviewed contract is mandatory.
"""
import copy,hashlib,json,time
from .hosted_control import require,canonical
from .elevenlabs_adapter import fingerprint
from .automated_review import exact,SHA,ID
from .vertex_review import endpoint,MODEL,WorkloadToken,encoded,build_vertex_request,validate_input_proof,_json
from .provider_execution import _wall_deadline,ProviderOutcomeUnknown,WireResponse
from .cloud_journal import _FirestoreBoundary
from .declarative_policy import verify_signed


def count_request(native,project):
    exact(native,{'method','url','body'},'Exact native request required.')
    require(native['method']=='POST' and native['url']==endpoint(project),'CountTokens cannot redirect native scope.')
    b=native['body'];exact(b,{'systemInstruction','contents','generationConfig'},'Complete native body required.')
    require(len(b['contents'])==1 and b['contents'][0]['role']=='user' and len(b['systemInstruction']['parts'])==1,'Fixed single-turn native input required.')
    rebuilt=build_vertex_request(project=project,system_instruction=b['systemInstruction']['parts'][0]['text'],parts=b['contents'][0]['parts'],response_json_schema=b['generationConfig']['responseSchema'],max_output_tokens=b['generationConfig']['maxOutputTokens'])
    require(native==rebuilt,'Unreviewed native generation settings.')
    return dict(method='POST',url=endpoint(project).removesuffix(':generateContent')+':countTokens',body=copy.deepcopy(b))


def validate_native_job_scope(job,native):
    """Parse untrusted native data only; never execute image/model instructions."""
    from . import source_planning,automated_review
    parts=native['body']['contents'][0]['parts'];require(parts and set(parts[0])=={'text'},'Native first part must bind its exact private request.')
    data=_json(parts[0]['text'].encode(),1_000_000);binding=data.get('binding')
    require(isinstance(binding,dict) and (binding.get('owner_id'),binding.get('job_id'))==(job.owner_id,job.id),'Count disclosure belongs to another owner/job.')
    images=job.snapshot['images']
    if data.get('schema')==source_planning.SCHEMA:
        require(binding.get('snapshot_sha256')==job.snapshot_sha256 and binding.get('input_order')==job.snapshot['input_order']
            and binding.get('profile_sha256')==fingerprint(job.snapshot['processing_plan']['narration_profile'])
            and binding.get('narrated_text_sha256')==hashlib.sha256(job.snapshot['narrated_text'].encode()).hexdigest()
            and data.get('exact_story_as_untrusted_data')==job.snapshot['narrated_text']
            and binding.get('policy_sha256')==fingerprint(source_planning.policy())
            and data.get('binding_sha256')==fingerprint(binding),'Native source count changed frozen story/profile/order/policy.')
        sources=binding.get('sources');require(isinstance(sources,list) and len(sources)==len(images) and len(parts)==1+2*len(images),'Native count must cover all original sources.')
        for index,(source,image) in enumerate(zip(sources,images)):
            require((source.get('image_id'),source.get('source_sha256'),source.get('position'))==(image['id'],image['sha256'],index+1),'Source count changed original identity/order.')
            label=_json(parts[1+2*index]['text'].encode(),100000);require(label==source,'Native source label changed.')
            import base64
            raw=base64.b64decode(parts[2+2*index]['inlineData']['data'],validate=True)
            require(hashlib.sha256(raw).hexdigest()==source['derivative']['sha256'],'Native source derivative bytes changed.')
    elif data.get('schema')==automated_review.SCHEMA:
        image=next((i for i in images if i['id']==binding.get('image_id')),None)
        require(image is not None and binding.get('source_sha256')==image['sha256'] and binding.get('policy_sha256')==fingerprint(automated_review.candidate_policy())
            and binding.get('model')==MODEL and type(binding.get('sequence')) is int and 0<=binding['sequence']<=10 and len(parts)==21,'Native review count changed source/policy/finite sequence.')
    else:raise ValueError('Only exact source-planning or nine-frame review native families may be disclosed.')
    return True


def parse_count(raw):
    value=_json(raw,65536)
    require(isinstance(value,dict) and set(value)<={'totalTokens','totalBillableCharacters','promptTokensDetails'}
        and type(value.get('totalTokens')) is int and 0<value['totalTokens']<=1_048_576,'Invalid bounded native token count.')
    if 'totalBillableCharacters' in value:require(type(value['totalBillableCharacters']) is int and value['totalBillableCharacters']>=0,'Invalid billable character count.')
    if 'promptTokensDetails' in value:
        details=value['promptTokensDetails'];require(isinstance(details,list) and 0<len(details)<=2
            and all(isinstance(d,dict) and set(d)=={'modality','tokenCount'} and d['modality'] in {'TEXT','IMAGE'} and type(d['tokenCount']) is int and d['tokenCount']>=0 for d in details)
            and len({d['modality'] for d in details})==len(details) and sum(d['tokenCount'] for d in details)==value['totalTokens'],'Unexpected or inconsistent counted modality.')
    return value


class VertexCountHTTP:
    def __init__(self,*,project,token_reader,activation_check,enabled=False,client_factory=None,now=time.time,clock=time.monotonic):
        endpoint(project);self.project,self.token_reader,self.active=project,token_reader,activation_check
        self.enabled,self.factory,self.now,self.clock=enabled is True,client_factory,now,clock
    def __call__(self,native,*,submission_check):
        require(self.enabled and self.active() is True and callable(submission_check),'CountTokens disclosure is disabled.')
        request=count_request(native,self.project);body=encoded(request['body']);require(len(body)<=12_000_000,'Bounded native count request required.')
        token=self.token_reader();require(type(token) is WorkloadToken and isinstance(token.value,str) and 16<=len(token.value)<=8192
            and token.value.isascii() and not any(c.isspace() for c in token.value) and self.now()+30<token.expires_at<=self.now()+3600,'Explicit short-lived workload token required.')
        factory=self.factory
        if factory is None:
            import httpx
            factory=httpx.Client
        try:
            with factory(timeout=10,follow_redirects=False,trust_env=False) as client,_wall_deadline(client,60,self.clock) as check:
                require(submission_check() is True and self.active() is True and self.now()+5<token.expires_at,'Count policy expired before disclosure.')
                with client.stream('POST',request['url'],headers={'Authorization':'Bearer '+token.value,'Content-Type':'application/json','Accept':'application/json','Accept-Encoding':'identity'},content=body) as response:
                    require(response.status_code==200 and response.headers.get('content-encoding','identity').lower()=='identity','Count response unavailable.')
                    raw=bytearray()
                    for chunk in response.iter_bytes():check();require(len(raw)+len(chunk)<=65536,'Count response exceeds bound.');raw.extend(chunk)
            parse_count(bytes(raw));return bytes(raw)
        except Exception:raise ProviderOutcomeUnknown('CountTokens outcome unknown; no automatic retry or generation fallback.') from None


class FirestoreCountRecords(_FirestoreBoundary):
    def __init__(self,*args,owner_id,job_id,**kwargs):
        super().__init__(*args,**kwargs);require(ID.fullmatch(owner_id) and ID.fullmatch(job_id),'Count owner/job required.');self.owner_id,self.job_id=owner_id,job_id
    def _base(self):return self._owner(self.owner_id)+'/token_preflight/'+self.job_id
    def get(self,key):
        require(SHA.fullmatch(key),'Exact native request hash required.');return self._read(self._doc(self._base()+'/requests/'+key))
    def reserve(self,key,binding,max_calls):
        require(SHA.fullmatch(key) and type(max_calls) is int and 1<=max_calls<=56,'Finite exact-request count scope required.')
        root=self._doc(self._base());ref=self._doc(self._base()+'/requests/'+key)
        def txn(tx):
            existing=self._read(ref,tx);state=self._read(root,tx)
            require(existing is None,'Count already claimed; recover read-only, never resubmit.')
            if state is None:state=dict(owner_id=self.owner_id,job_id=self.job_id,policy_sha256=binding['policy_sha256'],max_calls=max_calls,claimed=0)
            require(state['owner_id']==self.owner_id and state['job_id']==self.job_id and state['policy_sha256']==binding['policy_sha256'] and state['max_calls']==max_calls and state['claimed']<max_calls,'Count policy/call cap changed or exhausted.')
            updated={**state,'claimed':state['claimed']+1}
            if self._read(root,tx) is None:tx.create(root,updated)
            else:tx.update(root,updated)
            tx.create(ref,dict(state='may_have_submitted',binding=binding,receipt=None,slot=state['claimed']))
            return state['claimed']
        return self._run(txn)
    def record(self,key,binding,receipt):
        ref=self._doc(self._base()+'/requests/'+key)
        def txn(tx):
            previous=self._read(ref,tx);require(previous is not None and previous['binding']==binding,'Count receipt scope changed.')
            if previous['state']=='received':require(previous['receipt']==receipt,'Immutable count receipt changed.');return
            require(previous['state']=='may_have_submitted','Unknown count state.');tx.update(ref,dict(state='received',receipt=receipt))
        self._run(txn)


def validate_billing_contract(policy,envelope,public_key,job=None,*,now=time.time):
    p=policy.validate() if job is None else policy._job(job);c=verify_signed(envelope,public_key);t=p['token_preflight']
    exact(c,{'purpose','reference','policy_reference','endpoint','model','billed_controls','compiler_sha256s','includes_thought_tokens','token_limit_contract_reference','evidence_sha256','evidence_url','issued_at','expires_at'},'Independent documented billing contract required.')
    from . import vertex_review,source_planning
    from pathlib import Path
    compilers={name:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for name,module in (('vertex_review',vertex_review),('source_planning',source_planning))}
    require(fingerprint(c)==t['billing_contract_sha256'] and c['purpose']=='byron-vertex-billable-output-contract-v1' and c['policy_reference']==p['reference']
        and isinstance(c['reference'],str) and ID.fullmatch(c['reference']) and c['endpoint']==endpoint(p['project']) and c['model']==MODEL and c['compiler_sha256s']==compilers
        and c['includes_thought_tokens'] is True and c['token_limit_contract_reference']==p['source_config']['token_limit_contract_reference']
        and isinstance(c['evidence_sha256'],str) and SHA.fullmatch(c['evidence_sha256']) and isinstance(c['evidence_url'],str) and c['evidence_url'].startswith('https://')
        and type(c['issued_at']) is int and type(c['expires_at']) is int and c['issued_at']<=now()<c['expires_at']<=p['expires_at'], 'Missing independently documented exact billing contract; generation blocked.')
    expected=[]
    for output in sorted({p['source_config']['max_output_tokens'],p['review_quote']['max_output_tokens']}):
        generation=build_vertex_request(project=p['project'],system_instruction='bound',parts=[{'text':'bound'}],response_json_schema={},max_output_tokens=output)['body']['generationConfig']
        del generation['responseSchema'];expected.append(dict(generation_controls=generation,max_billable_output_tokens=output))
    require(c['billed_controls']==expected,'Billing evidence must explicitly bound each source/review response-plus-thought ceiling, not just configure maxOutputTokens.')
    return c

def fund_count_budget(policy,job,journal):
    p=policy._job(job);t=p['token_preflight'];budget=policy.trusted_budget(job)
    from .automated_review import validate_budget_context
    validate_budget_context(journal,budget)
    require(journal.owner_id==job.owner_id and journal.job_id==job.id,'Count funding belongs to another job.')
    plan=dict(policy_sha256=policy.sha256,owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,max_calls=t['max_calls_per_job'],max_cost_cents=t['max_cost_cents'],budget=budget)
    plan_id='count_plan_'+fingerprint(dict(owner=job.owner_id,job=job.id));sha=fingerprint(plan)
    allocations={'count_slot_'+fingerprint(dict(owner=job.owner_id,job=job.id,slot=i)):t['max_cost_cents'] for i in range(t['max_calls_per_job'])}
    if t['max_cost_cents']:journal.reserve_plan(plan_id,sha,allocations,budget['job_cap_cents'])
    return dict(plan_id=plan_id,plan_sha256=sha,allocations=allocations,total_cents=t['max_calls_per_job']*t['max_cost_cents'])


class CountedNativeProofs:
    """Fresh automatic exact-input proof; output bound remains independent evidence."""
    def __init__(self,*,policy,billing_contract,public_key,records,archive,journal,transport,enabled=False,now=time.time):
        from .declarative_policy import ApprovedProductionPolicy
        require(type(policy) is ApprovedProductionPolicy and type(records) is FirestoreCountRecords and type(transport) is VertexCountHTTP,'Concrete signed-policy/count adapters required.')
        self.policy,self.policy_sha256=policy,policy.sha256;self.contract=copy.deepcopy(billing_contract);self.key=public_key
        self.records,self.archive,self.journal,self.transport=records,archive,journal,transport;self.enabled,self.now=enabled is True,now
    def prevalidate(self,job):return validate_billing_contract(self.policy,self.contract,self.key,job,now=self.now)
    def _contract(self,p,native,max_output_tokens,token_limit_contract_reference,job):
        c=self.prevalidate(job);controls=copy.deepcopy(native['body']['generationConfig']);schema=controls.pop('responseSchema')
        require(dict(generation_controls=controls,max_billable_output_tokens=max_output_tokens) in c['billed_controls']
            and token_limit_contract_reference==c['token_limit_contract_reference'] and native['url']==c['endpoint'],'Native billed controls differ from independent contract.')
        from . import vertex_review,source_planning
        def vertex_types(value):
            if isinstance(value,list):return [vertex_types(x) for x in value]
            if not isinstance(value,dict):return value
            return {key:(item.upper() if key=='type' else str(item) if key in ('minItems','maxItems') else vertex_types(item)) for key,item in value.items()}
        if schema!=vertex_types(source_planning.response_schema()):
            properties=schema.get('properties',{}).get('binding',{}).get('properties',{})
            binding={k:(v.get('minimum') if v.get('type')=='INTEGER' and v.get('minimum')==v.get('maximum') else (v.get('enum') or [None])[0]) for k,v in properties.items()}
            # Dynamic identities are accepted only under the pinned schema compiler.
            require(schema==vertex_review.response_schema(binding),'Unreviewed native response-schema compiler output.')
        return c
    def load(self,job,native,*,max_input_tokens,max_output_tokens,image_count,token_limit_contract_reference):
        require(self.enabled,'CountTokens proof compiler remains disabled.')
        p=self.policy._job(job);t=p['token_preflight'];c=self._contract(p,native,max_output_tokens,token_limit_contract_reference,job)
        require(t['disclosure_authorized'] is True and self.records.owner_id==self.journal.owner_id==job.owner_id and self.records.job_id==self.journal.job_id==job.id,'Count disclosure owner/job changed.')
        request=count_request(native,p['project']);validate_native_job_scope(job,native);key=fingerprint(native);budget=self.policy.trusted_budget(job)
        from .automated_review import validate_budget_context
        validate_budget_context(self.journal,budget)
        binding=dict(owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,policy_sha256=self.policy_sha256,native_request_sha256=key,count_request_sha256=fingerprint(request),budget_sha256=fingerprint(budget))
        funding=fund_count_budget(self.policy,job,self.journal)
        old=self.records.get(key)
        if old is not None:
            require(old['binding']==binding,'Cached count belongs to another scope.')
            if old['state']!='received':raise ProviderOutcomeUnknown('Count preflight unresolved; no automatic resubmission.')
            wire=self.archive.read_response(old['receipt'])
        else:
            slot=self.records.reserve(key,binding,t['max_calls_per_job']);op='count_slot_'+fingerprint(dict(owner=job.owner_id,job=job.id,slot=slot))
            if t['max_cost_cents']:
                self.journal.claim_reserved(op,dict(request=request,binding=binding),t['max_cost_cents'],budget['job_cap_cents'],funding['plan_id'],funding['plan_sha256'])
            def gate():
                current=self.policy._job(job);self._contract(current,native,max_output_tokens,token_limit_contract_reference,job);return True
            raw=self.transport(native,submission_check=gate)
            wire=WireResponse('token_count_'+fingerprint(dict(owner_id=job.owner_id,job_id=job.id,native_request_sha256=key,response_sha256=hashlib.sha256(raw).hexdigest())),raw);receipt=self.archive.write_response(op,wire)
            require(self.archive.read_response(receipt)==wire,'Count response immutable readback failed.')
            if t['max_cost_cents']:self.journal.record(op,dict(request=request,binding=binding),receipt)
            self.records.record(key,binding,receipt)
        count=parse_count(wire.payload);require(wire.request_id=='token_count_'+fingerprint(dict(owner_id=job.owner_id,job_id=job.id,native_request_sha256=key,response_sha256=hashlib.sha256(wire.payload).hexdigest())),'Count archive identity changed.')
        bounded=count['totalTokens']+t['input_margin_tokens'];require(bounded<=max_input_tokens,'Actual count plus independently approved margin exceeds funded ceiling.')
        evidence=fingerprint(dict(binding=binding,response_sha256=hashlib.sha256(wire.payload).hexdigest(),count=count,input_margin_tokens=t['input_margin_tokens'],count_bound_evidence_sha256=t['count_bound_evidence_sha256']))
        expiry=min(p['expires_at'],c['expires_at'])
        billing=dict(reference=c['reference'],evidence_sha256=c['evidence_sha256'],native_request_sha256=key,generation_config_sha256=fingerprint(native['body']['generationConfig']),model=MODEL,endpoint=native['url'],max_billable_output_tokens=max_output_tokens,includes_thought_tokens=True,token_limit_contract_reference=token_limit_contract_reference,expires_at=expiry)
        proof=dict(reference='count_proof_'+key,native_request_sha256=key,max_input_tokens=bounded,max_output_tokens=max_output_tokens,image_count=image_count,expires_at=expiry,method='reviewed_count_tokens_bound',evidence_sha256=evidence,token_limit_contract_reference=token_limit_contract_reference,billing_bound=billing)
        return validate_input_proof(proof,native,max_input_tokens=max_input_tokens,max_output_tokens=max_output_tokens,image_count=image_count,token_limit_contract_reference=token_limit_contract_reference,now=self.now())


class ConfiguredNativeProofs:
    """Configuration-driven production loader; no arbitrary operator Python module."""
    def __init__(self,*,policy,billing_envelope,billing_public_key,token_reader,firestore_client,storage_client,config,activation_check,enabled=False,transaction_runner=None,client_factory=None,now=time.time):
        self.policy,self.policy_sha256=policy,policy.sha256;self.contract,self.key=copy.deepcopy(billing_envelope),billing_public_key
        self.token_reader,self.firestore,self.storage=token_reader,firestore_client,storage_client
        self.config,self.active,self.enabled=copy.deepcopy(config),activation_check,enabled is True
        self.tx,self.factory,self.now=transaction_runner,client_factory,now
    def prevalidate(self,job):
        p=self.policy._job(job)
        require(p['project']==self.config['target']['project_id'] and all(p['budget'][k]==self.config['budget'][k] for k in ('scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents','operation_cap_cents')),'Configured native proof campaign/project differs from signed policy.')
        return validate_billing_contract(self.policy,self.contract,self.key,job,now=self.now)
    def reserve_budget(self,job):
        require(self.enabled and self.active() is True,'Count funding remains disabled.')
        self.prevalidate(job)
        from .cloud_journal import FirestoreSpendJournal
        b=self.config['budget'];target=self.config['target']
        journal=FirestoreSpendJournal(self.firestore,project_id=target['project_id'],namespace=self.config['firestore']['namespace'],transaction_runner=self.tx,owner_id=job.owner_id,job_id=job.id,budget_scope=b['scope'],approved_total_cap_cents=b['approved_total_cap_cents'],prior_spend_cents=b['prior_spend_cents'])
        return fund_count_budget(self.policy,job,journal)
    def load(self,job,native,**caps):
        require(self.enabled and self.active() is True,'Configured count disclosure remains disabled.')
        self.prevalidate(job)
        from .cloud_journal import FirestoreSpendJournal
        from .cloud_storage import CloudProviderResponseStore
        from .hosted_control import Principal
        b=self.config['budget'];target=self.config['target'];common=dict(project_id=target['project_id'],namespace=self.config['firestore']['namespace'],transaction_runner=self.tx)
        records=FirestoreCountRecords(self.firestore,**common,owner_id=job.owner_id,job_id=job.id)
        journal=FirestoreSpendJournal(self.firestore,**common,owner_id=job.owner_id,job_id=job.id,budget_scope=b['scope'],approved_total_cap_cents=b['approved_total_cap_cents'],prior_spend_cents=b['prior_spend_cents'])
        archive=CloudProviderResponseStore(self.storage,bucket=target['bucket'],owner=Principal(job.owner_id),job_id=job.id)
        def gate():self.prevalidate(job);require(self.active() is True,'Native count activation expired.');return True
        transport=VertexCountHTTP(project=target['project_id'],token_reader=lambda:self.token_reader(job),activation_check=gate,enabled=True,client_factory=self.factory,now=self.now)
        compiler=CountedNativeProofs(policy=self.policy,billing_contract=self.contract,public_key=self.key,records=records,archive=archive,journal=journal,transport=transport,enabled=True,now=self.now)
        return compiler.load(job,native,**caps)
