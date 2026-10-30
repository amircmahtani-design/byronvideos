"""Signed private production policy -> deterministic bounded job receipts.

No key, consent, price or native billing proof is supplied by this package.
Missing independent signatures/proofs fail closed. No provider/client discovery.
"""
import base64,copy,hashlib,json,time
from decimal import Decimal,ROUND_CEILING,localcontext
from pathlib import Path
from .hosted_control import canonical,require
from .elevenlabs_adapter import fingerprint,validate_profile,plan_requests
from .provider_execution import SpendApproval
from .review_budget import call_ceiling_cents,_rate
from .automated_review import exact,ID

PURPOSE='byron-production-policy-v1'


def verify_signed(envelope,public_key):
    try:
        require(isinstance(envelope,dict) and set(envelope)=={'payload','signature'},'Signed envelope required.')
        require(isinstance(public_key,bytes) and len(public_key)==32,'Independent trusted reviewer key required.')
        signature=base64.b64decode(envelope['signature'],validate=True)
        require(len(signature)==64,'Ed25519 signature required.')
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature,canonical(envelope['payload']).encode())
        return json.loads(canonical(envelope['payload']))
    except Exception:raise ValueError('Missing or invalid independently signed policy/proof; unsigned fallback forbidden.') from None


def read_private_json(path,maximum=1_000_000):
    path=Path(path)
    require(path.is_absolute() and path.is_file() and not any(p.is_symlink() for p in (path,*path.parents)), 'Private regular configuration file required.')
    raw=path.read_bytes();require(0<len(raw)<=maximum,'Configuration exceeds bounded size.')
    def pairs(items):
        result={}
        for k,v in items:require(k not in result,'Duplicate configuration key.');result[k]=v
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda v:(_ for _ in ()).throw(ValueError('Nonfinite configuration.')))


class SignedNativeProofs:
    """Read-only exact-request proof archive. Does NOT manufacture token evidence."""
    def __init__(self,root,*,public_key,policy_sha256,now=time.time):
        self.root=Path(root);self.key=public_key;self.policy_sha256=policy_sha256;self.now=now
    def load(self,job,native,*,max_input_tokens,max_output_tokens,image_count,token_limit_contract_reference):
        from .vertex_review import validate_input_proof
        sha=fingerprint(native)
        value=verify_signed(read_private_json(self.root/(sha+'.json')),self.key)
        exact(value,{'purpose','policy_sha256','owner_id','job_id','snapshot_sha256','proof'},'Exact signed native proof record required.')
        require(value['purpose']=='byron-native-input-billing-proof-v1' and value['policy_sha256']==self.policy_sha256
            and (value['owner_id'],value['job_id'],value['snapshot_sha256'])==(job.owner_id,job.id,job.snapshot_sha256),'Native proof scope changed.')
        return validate_input_proof(value['proof'],native,max_input_tokens=max_input_tokens,max_output_tokens=max_output_tokens,
            image_count=image_count,token_limit_contract_reference=token_limit_contract_reference,now=self.now())


class ApprovedProductionPolicy:
    """Fresh signed prior administrator authorization, never browser consent.

    Policy signatures authorize ONLY the declared private draft pipeline. Human
    acceptance, public release and native billing facts cannot be inferred.
    """
    def __init__(self,envelope,*,public_key,config_sha256,processing_plan_sha256,now=time.time):
        self._envelope=copy.deepcopy(envelope);self.key=public_key;self.now=now
        self.config_sha256=config_sha256;self.processing_plan_sha256=processing_plan_sha256
        self.payload=verify_signed(envelope,public_key);self.sha256=fingerprint(self.payload);self.validate()
    @classmethod
    def from_file(cls,path,**kwargs):return cls(read_private_json(path),**kwargs)
    def validate(self):
        p=verify_signed(self._envelope,self.key)
        require(p==self.payload,'Signed policy mutated.')
        mode=p.get('native_api_mode','vertex_eu')
        require(mode in ('vertex_eu','gemini_developer'),'Unknown native mode; fallback forbidden.')
        optional=set(p)&{'native_api_mode','native_disclosure'}
        if mode=='gemini_developer':
            require(optional=={'native_api_mode','native_disclosure'},'Explicit selected Developer API disclosure required.')
            disclosure=p['native_disclosure'];exact(disclosure,{'paid_service','non_eu_exclusive_processing','abuse_retention_days','data_disclosure_authorized'},'Exact Developer API disclosure required.')
            require(all(disclosure[k] is True for k in ('paid_service','non_eu_exclusive_processing','data_disclosure_authorized')) and type(disclosure['abuse_retention_days']) is int and disclosure['abuse_retention_days']==55,'Developer API requires explicit paid, international-processing and retention consent.')
        else:require('native_disclosure' not in p,'Developer disclosure cannot select or authorize Vertex.')
        exact(p,{'purpose','reference','issued_at','expires_at','owner_ids','config_sha256','processing_plan_sha256','project',
            'private_preview_authorized','owner_only','public_release','human_style_accepted','preview_intent','narration_profile','allowed_voice_profiles',
            'budget','infrastructure','token_preflight','narration_usd_per_character','all_in_usd_ceiling','image_quote','extension_quote','review_quote','source_config'}|optional,'Complete versioned production policy required.')
        require(p['purpose']==PURPOSE and isinstance(p['reference'],str) and ID.fullmatch(p['reference'])
            and type(p['issued_at']) is int and type(p['expires_at']) is int and p['issued_at']<=self.now()<p['expires_at']
            and 0<p['expires_at']-p['issued_at']<=86400,'Production policy expired or unbounded.')
        require(p['config_sha256']==self.config_sha256 and p['processing_plan_sha256']==self.processing_plan_sha256,'Signed policy differs from private configuration/processing plan.')
        require(p['private_preview_authorized'] is True and p['owner_only'] is True and p['public_release'] is False
            and p['human_style_accepted'] is False and p['all_in_usd_ceiling'] is True,'Explicit private-only prior approval and all-in prices required.')
        require(isinstance(p['owner_ids'],list) and 0<len(p['owner_ids'])<=32 and len(set(p['owner_ids']))==len(p['owner_ids'])
            and all(isinstance(x,str) and ID.fullmatch(x) for x in p['owner_ids']),'Exact approved owner identities required.')
        from .private_preview import validate_intent
        from .preview_composition import release_hash
        from .motion_continuation import _quote
        from .vertex_review import config
        if mode=='gemini_developer':
            from .gemini_developer import config
        validate_intent(p['preview_intent']);require(p['preview_intent']['preview_release_sha256']==release_hash(),'Preview renderer changed.')
        validate_profile(p['narration_profile'])
        voices=p['allowed_voice_profiles'];require(isinstance(voices,list) and 1<=len(voices)<=32 and p['narration_profile'] in voices and len({fingerprint(v) for v in voices})==len(voices),'Finite explicit approved voice profiles required.')
        for voice in voices:
            validate_profile(voice);require({k:v for k,v in voice.items() if k!='voice_id'}=={k:v for k,v in p['narration_profile'].items() if k!='voice_id'},'Voice allowlist cannot silently alter model/settings or pricing basis.')
        _rate(p['narration_usd_per_character'])
        self._infrastructure(p)
        b=p['budget'];exact(b,{'scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents','operation_cap_cents','retry_allowance_cents','approval_reference'},'Explicit complete cumulative budget required.')
        require(isinstance(b['scope'],str) and ID.fullmatch(b['scope']) and isinstance(b['approval_reference'],str) and ID.fullmatch(b['approval_reference'])
            and all(type(b[k]) is int for k in ('approved_total_cap_cents','prior_spend_cents','job_cap_cents','operation_cap_cents','retry_allowance_cents'))
            and 0<=b['prior_spend_cents']<b['approved_total_cap_cents'] and 0<b['operation_cap_cents']<=b['job_cap_cents']<=b['approved_total_cap_cents']
            and 0<=b['retry_allowance_cents']<=b['job_cap_cents'],'Invalid campaign/job/operation/retry scope.')
        for key,kind in (('image_quote','image'),('extension_quote','extend')):
            quote=_quote(p[key],kind,self.now());require(quote['valid_until_epoch']<=p['expires_at'] and quote['max_cost_cents']<=b['operation_cap_cents'],'Motion quote exceeds signed policy.')
        t=p['token_preflight'];exact(t,{'disclosure_authorized','max_calls_per_job','max_cost_cents','all_in_usd_ceiling','price_evidence_sha256','input_margin_tokens','count_bound_evidence_sha256','billing_contract_sha256'},'Explicit count disclosure, cost and evidence required.')
        from .automated_review import SHA
        require(t['disclosure_authorized'] is True and t['all_in_usd_ceiling'] is True and type(t['max_calls_per_job']) is int and 1<=t['max_calls_per_job']<=56
            and type(t['max_cost_cents']) is int and 0<=t['max_cost_cents']<=b['operation_cap_cents'] and type(t['input_margin_tokens']) is int and 0<=t['input_margin_tokens']<=100000
            and all(isinstance(t[k],str) and SHA.fullmatch(t[k]) for k in ('price_evidence_sha256','count_bound_evidence_sha256','billing_contract_sha256')), 'Unknown count price, bounds or permission blocks preflight.')
        q=p['review_quote'];require(q['config']==config(p['project']) and q['valid_until_epoch']<=p['expires_at']
            and q['valid_until_epoch']>self.now() and q['approved'] is True and q['all_in_usd_ceiling'] is True
            and q['output_includes_thought_tokens'] is True and q['sample_count']==9 and q['source_count']==1,'Native review quote scope changed.')
        require(q['max_output_tokens']==8192,'Native review output ceiling must match selected pinned transport.')
        c=p['source_config'];exact(c,{'project','max_input_tokens','max_output_tokens','token_limit_contract_reference'}|({'native_api_mode'} if mode=='gemini_developer' else set()),'Exact native planning configuration required.')
        require(c.get('native_api_mode','vertex_eu')==mode,'Planner native mode differs from signed policy.')
        if mode=='gemini_developer':require(c['max_output_tokens']==3072,'Developer planner output cap must be 3072.')
        require(c['project']==p['project'] and c['token_limit_contract_reference']==q['token_limit_contract_reference'],'Planner/review token enforcement differs.')
        planning=q['source_planning']
        require(planning['execution_enabled'] is False and all(planning[k]==c[k] for k in ('max_input_tokens','max_output_tokens')), 'Source planning accounting differs from native token ceiling.')
        for caps in (q,c):
            cost=call_ceiling_cents(max_input_tokens=caps['max_input_tokens'],max_output_tokens=caps['max_output_tokens'],input_usd_per_million=q['input_usd_per_million'],output_usd_per_million=q['output_usd_per_million'])
            require(cost<=b['operation_cap_cents'],'Token ceiling exceeds approved operation cap.')
        return copy.deepcopy(p)
    def _infrastructure(self,p):
        from .automated_review import SHA
        i=p['infrastructure']
        exact(i,{'purpose','reference','target_sha256','cpu','memory','task_timeout_seconds','task_count','parallelism','max_retries',
            'original_execution_limit','explicit_resume_limit','exact_film_execution_limit','authorization_mode','bounded_use_approved',
            'storage_scope','storage_retention','evidence_sha256','evidence_reference','per_execution_compute_allowance_cents',
            'all_job_storage_allowance_cents','all_job_infrastructure_allowance_cents','independent_budget_scope'},'Explicit compute/storage authorization is separate from provider costs.')
        require(i['purpose']=='byron-private-infrastructure-authorization-v1' and isinstance(i['reference'],str) and ID.fullmatch(i['reference'])
            and isinstance(i['target_sha256'],str) and SHA.fullmatch(i['target_sha256']) and i['cpu']=='2' and i['memory']=='4Gi'
            and type(i['task_timeout_seconds']) is int and 60<=i['task_timeout_seconds']<=3600
            and all(type(i[k]) is int for k in ('task_count','parallelism','max_retries','original_execution_limit','explicit_resume_limit','exact_film_execution_limit'))
            and (i['task_count'],i['parallelism'],i['max_retries'],i['original_execution_limit'],i['explicit_resume_limit'],i['exact_film_execution_limit'])==(1,1,0,1,3,1)
            and i['bounded_use_approved'] is True and i['storage_scope']=='PRIVATE_JOB_INPUTS_INTERMEDIATES_PROOFS_OUTPUTS'
            and i['storage_retention']=='UNTIL_EXPLICIT_OWNER_DELETION' and isinstance(i['evidence_sha256'],str) and SHA.fullmatch(i['evidence_sha256'])
            and isinstance(i['evidence_reference'],str) and 8<=len(i['evidence_reference'])<=512,'Unapproved/changed infrastructure resources, finite attempts or retention disclosure.')
        amounts=('per_execution_compute_allowance_cents','all_job_storage_allowance_cents','all_job_infrastructure_allowance_cents')
        if i['authorization_mode']=='DOCUMENTED_BOUNDED_USE_CONSENT':
            require(all(i[k] is None for k in amounts) and i['independent_budget_scope'] is None,'Nonmonetary consent cannot invent guaranteed dollar ceilings.')
        elif i['authorization_mode']=='ALL_IN_QUOTED_ALLOWANCE':
            require(all(type(i[k]) is int and 0<i[k]<=1_000_000_000 for k in amounts)
                and i['all_job_infrastructure_allowance_cents']>=5*i['per_execution_compute_allowance_cents']+i['all_job_storage_allowance_cents']
                and isinstance(i['independent_budget_scope'],str) and ID.fullmatch(i['independent_budget_scope']), 'Complete independent compute/storage allowance required.')
        else:raise ValueError('Missing documented infrastructure allowance or bounded-use consent.')
        return copy.deepcopy(i)
    def validate_infrastructure(self,config):
        p=self.validate();i=self._infrastructure(p);target=config['target']
        require(fingerprint(config)==p['config_sha256'] and fingerprint(target)==i['target_sha256']
            and all(i[k]==target[k] for k in ('cpu','memory','task_timeout_seconds','task_count','parallelism','max_retries')), 'Infrastructure authorization differs from actual private target/resources.')
        return {**i,'requires_separate_reservation':i['authorization_mode']=='ALL_IN_QUOTED_ALLOWANCE'}
    def _job(self,job):
        p=self.validate();normalized=copy.deepcopy(job.snapshot['processing_plan']);selected=normalized.get('narration_profile');normalized['narration_profile']=p['narration_profile']
        require(job.owner_id in p['owner_ids'] and fingerprint(job.snapshot)==job.snapshot_sha256
            and fingerprint(normalized)==p['processing_plan_sha256']
            and selected in p['allowed_voice_profiles']
            and job.snapshot['processing_plan']['preview_contract']==p['preview_intent'],'Job differs from approved owner/frozen profile/processing plan.')
        return p
    def _reference(self,job,kind):return 'policy_'+fingerprint(dict(policy=self.sha256,owner=job.owner_id,job=job.id,snapshot=job.snapshot_sha256,kind=kind))
    def preview_budget_loader(self,job):
        b=self._job(job)['budget'];return {k:v for k,v in b.items() if k!='operation_cap_cents'}
    def preview_authorization_loader(self,job):
        from .private_preview import AUTH_SCHEMA,validate_authorization
        p=self._job(job);auth=dict(schema=AUTH_SCHEMA,reference=self._reference(job,'preview'),owner_id=job.owner_id,job_id=job.id,
            snapshot_sha256=job.snapshot_sha256,**{k:v for k,v in p['preview_intent'].items() if k!='mode'},budget=self.preview_budget_loader(job),
            expires_at=p['expires_at'],private_preview_approved=True,owner_only=True,public_release=False,human_style_accepted=False)
        sha=fingerprint(auth);validate_authorization(auth,sha,job=job,budget=auth['budget'],preview_release_sha256=p['preview_intent']['preview_release_sha256'],now=self.now())
        return dict(authorization=auth,expected_authorization_sha256=sha)
    def narration_approval_loader(self,job):
        p=self._job(job);requests=plan_requests(job.snapshot['narrated_text'],job.snapshot['processing_plan']['narration_profile'])
        with localcontext() as ctx:
            ctx.prec=80;costs=tuple(int((Decimal(len(r['body']['text']))*_rate(p['narration_usd_per_character'])*100).to_integral_value(rounding=ROUND_CEILING)) for r in requests)
        b=p['budget'];approval=SpendApproval(self._reference(job,'narration'),fingerprint(requests),costs,b['job_cap_cents'],b['operation_cap_cents']);approval.validate(requests)
        return dict(approval=approval,quote=dict(reference=approval.reference,owner_id=job.owner_id,job_id=job.id,plan_sha256=approval.plan_sha256,reservations_cents=list(costs),valid_until_epoch=p['expires_at']))
    def preview_quote_loader(self,job,candidate,timing):
        p=self._job(job);return {k:copy.deepcopy(p[k]) for k in ('image_quote','extension_quote')}
    def review_quote_loader(self,job,plan):
        from .source_planning import source_operation
        p=self._job(job);require(plan['owner_id']==job.owner_id and plan['job_id']==job.id and plan['snapshot_sha256']==job.snapshot_sha256,'Review plan scope changed.')
        q=copy.deepcopy(p['review_quote']);q['reference']=self._reference(job,'review');s=q['source_planning'];s['image_count']=len(job.snapshot['images']);s['funded_operation_id']=source_operation(job.owner_id,job.id)
        return q
    def trusted_budget(self,job):
        b=self._job(job)['budget'];return dict(scope_sha256=hashlib.sha256(b['scope'].encode()).hexdigest(),**{k:b[k] for k in ('approved_total_cap_cents','prior_spend_cents','job_cap_cents')})
    def source_quote_loader(self,proofs):
        from .vertex_token_preflight import CountedNativeProofs,ConfiguredNativeProofs
        require(type(proofs) in (SignedNativeProofs,CountedNativeProofs,ConfiguredNativeProofs) and proofs.policy_sha256==self.sha256,'Independent exact signed native proof archive required.')
        def load(job,prepared):
            p=self._job(job);c=p['source_config'];require(prepared['config']==c,'Source config changed.')
            q=p['review_quote'];b=p['budget'];request={k:prepared[k] for k in ('native','binding','config')};request['evidence_sha256']=fingerprint(prepared['evidence'])
            cost=call_ceiling_cents(max_input_tokens=c['max_input_tokens'],max_output_tokens=c['max_output_tokens'],input_usd_per_million=q['input_usd_per_million'],output_usd_per_million=q['output_usd_per_million'])
            quote=dict(reference=self._reference(job,'source'),request_sha256=fingerprint(request),budget_sha256=fingerprint(self.trusted_budget(job)),reservation_cents=cost,job_cap_cents=b['job_cap_cents'],operation_cap_cents=b['operation_cap_cents'],
                **{k:q[k] for k in ('input_usd_per_million','output_usd_per_million','all_in_usd_ceiling','output_includes_thought_tokens')},**{k:c[k] for k in ('max_input_tokens','max_output_tokens','token_limit_contract_reference')},valid_until_epoch=min(p['expires_at'],q['valid_until_epoch']))
            proof=proofs.load(job,prepared['native'],max_input_tokens=c['max_input_tokens'],max_output_tokens=c['max_output_tokens'],image_count=len(job.snapshot['images']),token_limit_contract_reference=c['token_limit_contract_reference'])
            return dict(quote=quote,input_proof=proof)
        return load
    def review_input_proof_loader(self,proofs):
        from .vertex_token_preflight import CountedNativeProofs,ConfiguredNativeProofs
        require(type(proofs) in (SignedNativeProofs,CountedNativeProofs,ConfiguredNativeProofs) and proofs.policy_sha256==self.sha256,'Independent native proof archive required.')
        def load(job,logical,native):
            from .vertex_review import build_native_request
            p=self._job(job)
            if p.get('native_api_mode','vertex_eu')=='gemini_developer':
                from .gemini_developer import build_native_request
            q=p['review_quote'];require(native==build_native_request(logical,project=p['project']),'Count proof native request differs from exact logical review.')
            return proofs.load(job,native,max_input_tokens=q['max_input_tokens'],max_output_tokens=q['max_output_tokens'],image_count=10,token_limit_contract_reference=q['token_limit_contract_reference'])
        return load
