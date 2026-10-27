"""Offline configuration validator/read-only deployment plan; never runs commands."""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .cloud_render import validate_target
from .elevenlabs_adapter import validate_profile
from .worker_binding import validate_container_base


def need(value,message):
    if not value:raise ValueError(message)


def validate_config(value):
    base_keys={'schema_version','status','target','firestore','identity','secret_resources','budget','narration_profile','runtime'}
    need(isinstance(value,dict) and set(value) in (base_keys,base_keys|{'native_api_mode'}),'Use the exact private deployment configuration schema.')
    mode=value.get('native_api_mode','vertex_eu')
    need(mode in ('vertex_eu','gemini_developer'),'Choose one explicit native API mode; fallback/auto routing is forbidden.')
    need(type(value['schema_version']) is int and value['schema_version']==1 and value['status']=='DRAFT_NOT_ACTIVATED','This validator cannot activate a deployment.')
    target=validate_target(value['target']);project=target['project_id']
    db=value['firestore'];need(isinstance(db,dict) and set(db)=={'database','namespace'},'Explicit database/namespace required.')
    need(isinstance(db['database'],str) and bool(re.fullmatch(r'\(default\)|[a-z][a-z0-9-]{2,62}',db['database'])),'Invalid database identifier.')
    need(isinstance(db['namespace'],str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,80}',db['namespace'])),'Explicit private control namespace required.')
    auth=value['identity'];need(isinstance(auth,dict) and set(auth)=={'origin','oauth_audience','creator_subjects','admin_subjects','subject_emails'},'Explicit identity configuration required.')
    origin=urlsplit(auth['origin'] or '')
    need(origin.scheme=='https' and bool(origin.netloc) and not origin.username and not origin.password and not origin.path and not origin.query and not origin.fragment,'Exact HTTPS origin required.')
    need(isinstance(auth['oauth_audience'],str) and bool(auth['oauth_audience']) and len(auth['oauth_audience'])<=512,'OAuth audience required.')
    need(isinstance(auth['creator_subjects'],list) and bool(auth['creator_subjects']) and all(isinstance(s,str) and re.fullmatch(r'[0-9]{8,128}',s) for s in auth['creator_subjects']),'Verified Google subject allowlist required; emails are not identities.')
    need(isinstance(auth['admin_subjects'],list) and bool(auth['admin_subjects']) and set(auth['admin_subjects'])<=set(auth['creator_subjects']),'Explicit administrator subjects must be permitted creators.')
    emails=auth['subject_emails'];need(isinstance(emails,dict) and set(emails)==set(auth['creator_subjects']) and all(isinstance(v,str) and len(v)<=254 and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',v) for v in emails.values()),'Private verified-email mapping must match the pinned numeric subjects.')
    refs=value['secret_resources'];secret_keys={'elevenlabs','fal','default_voice','csrf','google_oauth'}
    need(isinstance(refs,dict) and set(refs) in (secret_keys,secret_keys|{'gemini'}),'Use private secret resource references, never literal keys.')
    need(mode!='gemini_developer' or 'gemini' in refs,'Explicit Developer API mode requires its private Gemini secret resource.')
    need(all(isinstance(s,str) and re.fullmatch(r'projects/'+re.escape(project)+r'/secrets/[A-Za-z0-9_-]+',s) for s in refs.values()),'Secrets must reference existing resources in the configured project.')
    budget=value['budget'];need(isinstance(budget,dict) and set(budget)=={'scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents','operation_cap_cents'},'Explicit total/prior/job/operation budget required.')
    need(isinstance(budget['scope'],str) and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',budget['scope']),'Private shared budget scope required.')
    total,prior,job,operation=(budget[k] for k in ('approved_total_cap_cents','prior_spend_cents','job_cap_cents','operation_cap_cents'))
    need(all(type(x) is int for x in (total,prior,job,operation)) and 0<=prior<total<=1_000_000_000 and 0<operation<=job<=total-prior,'Configured budget cannot discard prior spend or overallocate remaining cap.')
    validate_profile(value['narration_profile'])
    runtime=value['runtime'];base_runtime={'base_image','ffmpeg_version','sdk_lock_sha256','portrait_candidate_sha256','cloud_run_task_template_sha256','worker_service_account'}
    need(isinstance(runtime,dict) and set(runtime) in (base_runtime,base_runtime|{'service_service_account','csrf_secret_version'}),'Explicit pinned runtime/release bindings required.')
    if 'service_service_account' in runtime:
        need(isinstance(runtime['service_service_account'],str) and re.fullmatch(r'[A-Za-z0-9._-]+@'+re.escape(project)+r'\.iam\.gserviceaccount\.com',runtime['service_service_account'])
            and runtime['service_service_account']!=runtime['worker_service_account'],'Use a distinct reviewed service workload identity.')
        need(isinstance(runtime['csrf_secret_version'],str) and re.fullmatch(r'[1-9][0-9]{0,19}',runtime['csrf_secret_version']),'Pin an explicit numeric CSRF secret version.')
    validate_container_base(runtime['base_image'])
    need(isinstance(runtime['ffmpeg_version'],str) and bool(re.fullmatch(r'[A-Za-z0-9.+~:_-]{1,100}',runtime['ffmpeg_version'])),'Exact verified FFmpeg version required.')
    need(all(isinstance(runtime[k],str) and re.fullmatch(r'[0-9a-f]{64}',runtime[k]) for k in ('sdk_lock_sha256','portrait_candidate_sha256','cloud_run_task_template_sha256')),'Runtime and portrait candidate fingerprints required.')
    need(isinstance(runtime['worker_service_account'],str) and re.fullmatch(r'[A-Za-z0-9._-]+@'+re.escape(project)+r'\.iam\.gserviceaccount\.com',runtime['worker_service_account']),'Explicit reviewed workload identity in configured project required.')
    return json.loads(json.dumps(value))


def plan(value):
    config=validate_config(value);t=config['target'];p=t['project_id'];db=config['firestore']
    checks=[['gcloud','run','jobs','describe',t['job_name'],'--project',p,'--region',t['region'],'--format=json'],
            ['gcloud','storage','buckets','describe','gs://'+t['bucket'],'--format=json'],
            ['gcloud','firestore','databases','describe','--project',p,'--database',db['database'],'--format=json']]
    for resource in config['secret_resources'].values():
        checks.append(['gcloud','secrets','describe',resource.rsplit('/',1)[1],'--project',p,'--format=json'])
    return dict(status='PLAN_ONLY_NOT_EXECUTED',configuration_valid=True,live_ready=False,read_only_commands=checks,
        required_acceptance=['Verify existing resource ownership, region, private bucket policy and least-privilege workload identity.',
            'Provide a digest-pinned runtime with tested SDK lock; build/test candidate image without altering historical locks.',
            'Configure reviewed login callback/state/nonce/cookie/revocation flow and subject allowlists.',
            'Mount explicit runtime assembly only after independently signed, configuration-bound acceptance; default stays disabled.',
            'Verify real provider schema/voice/alignment/clip timing, spending reservations and unknown-outcome recovery with separately authorized capped test.',
            'Complete browser/iPad, full pinned tests and real private film/export acceptance before enabling live generation.'],
        approval_boundaries=['Creating/changing identity, IAM, bucket security or secret resources requires explicit authorization.',
            'Billing/resource creation, provider disclosure and paid acceptance tests require explicit authorization.',
            'Routine deployment may use standing permission only after existing target/resource scope is verified.'])


def main():
    parser=argparse.ArgumentParser(description='Validate a private config and print a read-only plan; no cloud calls.')
    parser.add_argument('config',type=Path);args=parser.parse_args()
    try:
        result=plan(json.loads(args.config.read_text()))
    except Exception as error:
        parser.exit(2,'Configuration is incomplete or invalid: '+str(error)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
