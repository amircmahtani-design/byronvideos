"""Current exact-film compute consent, separate from paid-generation permission."""
import copy,time
from .declarative_policy import verify_signed,ApprovedProductionPolicy
from .hosted_control import digest,require
from .automated_review import exact,ID

PURPOSE='byron-exact-film-infrastructure-authorization-v1'


def validate_exact_infrastructure(envelope,public_key,config,processing_plan,job=None,*,now=time.time):
    value=verify_signed(envelope,public_key)
    exact(value,{'purpose','reference','config_sha256','processing_plan_sha256','owner_ids','infrastructure',
                 'issued_at','expires_at','paid_generation_allowed','publication_allowed'},'Complete separate render-only infrastructure authorization required.')
    require(value['purpose']==PURPOSE and isinstance(value['reference'],str) and ID.fullmatch(value['reference'])
        and value['config_sha256']==digest(config) and value['processing_plan_sha256']==digest(processing_plan)
        and value['paid_generation_allowed'] is False and value['publication_allowed'] is False,
        'Infrastructure consent cannot authorize generation/publication or another configuration.')
    require(type(value['issued_at']) is int and type(value['expires_at']) is int
        and value['issued_at']<=now()<value['expires_at'] and 0<value['expires_at']-value['issued_at']<=7*86400,
        'Current bounded render-only infrastructure consent required.')
    owners=value['owner_ids']
    require(isinstance(owners,list) and 0<len(owners)<=32 and len(set(owners))==len(owners)
        and all(isinstance(owner,str) and ID.fullmatch(owner) for owner in owners),'Exact approved owner identities required.')
    # Reuse the pure bounded resource schema, never validate/refresh a generation policy.
    scope=ApprovedProductionPolicy._infrastructure(None,{'infrastructure':value['infrastructure']})
    target=config['target']
    require(scope['target_sha256']==digest(target) and all(scope[k]==target[k] for k in
        ('cpu','memory','task_timeout_seconds','task_count','parallelism','max_retries')),
        'Render-only infrastructure differs from the signed target.')
    require(scope['authorization_mode']=='DOCUMENTED_BOUNDED_USE_CONSENT',
        'Quoted infrastructure requires a separate reservation adapter; no implied dollar cap.')
    if job is not None:
        require(job.owner_id in owners and digest(job.snapshot)==job.snapshot_sha256
            and job.snapshot.get('runtime_config_sha256')==digest(config),'Render-only infrastructure belongs to another owner/configuration.')
        from .exact_film_runtime import require_job_contract
        require_job_contract(job)
        normalized=copy.deepcopy(job.snapshot['processing_plan'])
        # Original generation already froze the selected voice; no new voice,
        # synthesis or policy is selected by this render-only permission.
        normalized['narration_profile']=processing_plan['narration_profile']
        require(normalized==processing_plan,'Retained film differs from signed processing configuration.')
    return copy.deepcopy(value)
