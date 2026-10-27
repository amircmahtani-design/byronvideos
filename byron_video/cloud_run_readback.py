"""Read-only Cloud Run target and operation→execution verification adapter.

Uses documented v2 Job/Execution resources and RunJob's Execution metadata type.
No credentials are discovered, no network runs on import, and no write method is
provided. Construction/transport are disabled by default pending real acceptance.
"""
import copy
import json
import re
import time
from .provider_execution import _wall_deadline
from .cloud_render import validate_target,build_execution_request
from .elevenlabs_adapter import fingerprint


def need(ok,message):
    if not ok:raise ValueError(message)


def normalized_task(value):
    need(isinstance(value,dict),'Task template required.')
    result=copy.deepcopy(value)
    containers=result.get('containers')
    need(isinstance(containers,list) and len(containers)==1 and containers[0].get('name')=='renderer','Exactly the reviewed renderer container is required.')
    env=containers[0].get('env',[])
    need(isinstance(env,list) and all(isinstance(x,dict) and isinstance(x.get('name'),str) for x in env)
         and len({x['name'] for x in env})==len(env),'Duplicate or malformed environment variables.')
    containers[0]['env']=sorted(env,key=lambda x:x['name'])
    return result


class CloudRunReadTransport:
    def __init__(self,token_reader,*,enabled=False,client_factory=None,clock=time.monotonic):
        self.clock=clock
        self.token_reader,self.enabled,self.factory=token_reader,enabled is True,client_factory
    def __call__(self,url):
        need(self.enabled,'Cloud Run readback is not activated.')
        need(isinstance(url,str) and re.fullmatch(r'https://run\.googleapis\.com/v2/projects/[a-z][a-z0-9-]+/locations/[a-z0-9-]+/(?:jobs/[a-z0-9-]+(?:/executions/[A-Za-z0-9_-]+)?|operations/[A-Za-z0-9_-]+)',url),'Unexpected read-only Cloud Run resource.')
        try:
            token=self.token_reader()
            need(isinstance(token,str) and 16<=len(token)<=8192 and token.isascii() and not any(c.isspace() for c in token),'Workload token required.')
            factory=self.factory
            if factory is None:
                import httpx
                factory=httpx.Client
            with factory(timeout=10,follow_redirects=False,trust_env=False) as client, _wall_deadline(client,30,self.clock) as check_deadline:
                with client.stream('GET',url,headers={'Authorization':'Bearer '+token,'Accept-Encoding':'identity'}) as response:
                    need(response.headers.get('content-encoding','identity').lower()=='identity','Encoded readback rejected.')
                    need(response.status_code==200,'Readback unavailable.')
                    raw=bytearray()
                    for part in response.iter_bytes():
                        check_deadline()
                        need(len(raw)+len(part)<=256*1024,'Readback exceeded metadata bound.')
                        raw.extend(part)
                    result=json.loads(raw,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
                    need(isinstance(result,dict),'Object response required.')
                    return result
        except Exception:
            raise ValueError('Cloud Run readback could not be verified; no write or retry was performed.') from None


class CloudRunReadback:
    def __init__(self,*,target,read,expected_task_template_sha256,expected_service_account,handoff_loader=None,expected_request_loader=None,enabled=False):
        self.target=validate_target(target);self.read=read;self.enabled=enabled is True
        need(isinstance(expected_task_template_sha256,str) and re.fullmatch(r'[0-9a-f]{64}',expected_task_template_sha256),'Independently reviewed complete task-template digest required.')
        need(isinstance(expected_service_account,str) and re.fullmatch(r'[A-Za-z0-9._-]+@[a-z][a-z0-9-]+\.iam\.gserviceaccount\.com',expected_service_account),'Explicit reviewed workload identity required.')
        self.template_sha,self.service_account=expected_task_template_sha256,expected_service_account
        need(callable(handoff_loader) != callable(expected_request_loader),'Exactly one trusted execution-request loader is required.')
        self.handoff_loader,self.expected_request_loader=handoff_loader,expected_request_loader
        t=self.target;self.job_name=f"projects/{t['project_id']}/locations/{t['region']}/jobs/{t['job_name']}"
        self.operation_prefix=f"projects/{t['project_id']}/locations/{t['region']}/operations/"

    def _baseline(self):
        need(self.enabled,'Cloud Run readback is not activated.')
        resource=self.read('https://run.googleapis.com/v2/'+self.job_name)
        # Plain proto3 bool: omitted reconciling has the documented False default.
        # maxRetries is different (oneof retries, omission defaults to 3):
        # retain the explicit integer zero requirement below.
        need(resource.get('name')==self.job_name and not resource.get('deleteTime') and resource.get('reconciling',False) is False,
             'Deployed job is missing, changing or deleted.')
        need(resource.get('terminalCondition',{}).get('state')=='CONDITION_SUCCEEDED','Deployed job is not ready.')
        template=resource.get('template',{});task=normalized_task(template.get('template'))
        t=self.target;container=task['containers'][0]
        need(template.get('taskCount')==t['task_count'] and type(template.get('taskCount')) is int
             and template.get('parallelism')==t['parallelism'] and type(template.get('parallelism')) is int
             and task.get('maxRetries')==0 and type(task.get('maxRetries')) is int
             and task.get('timeout')==str(t['task_timeout_seconds'])+'s'
             and task.get('serviceAccount')==self.service_account
             and container.get('image')==t['container_image']
             and container.get('resources',{}).get('limits')=={'cpu':t['cpu'],'memory':t['memory']}
             and fingerprint(task)==self.template_sha,'Deployed task differs from the reviewed complete template.')
        return task

    def target_provider(self):
        self._baseline()
        return copy.deepcopy(self.target)

    def terminal_execution_with_receipt(self,job,receipt):
        """Probe a separate execution without replacing the original job proof.

        The trusted request loader is closed over the independently loaded
        original job. Only the execution identity comes from the clean receipt.
        No shared reader/loader is temporarily mutated.
        """
        from types import SimpleNamespace
        receipt.validate(job)
        need(callable(self.expected_request_loader),'Exact trusted execution request loader required.')
        request=self.expected_request_loader(job,self.target)
        probe=CloudRunReadback(target=self.target,read=self.read,
            expected_task_template_sha256=self.template_sha,expected_service_account=self.service_account,
            expected_request_loader=lambda ignored,target:copy.deepcopy(request),enabled=self.enabled)
        view=SimpleNamespace(id=job.id,owner_id=job.owner_id,snapshot_sha256=job.snapshot_sha256,
            snapshot=job.snapshot,receipt=receipt)
        return probe.terminal_execution(view)

    def terminal_execution(self,job):
        """Read-only prior-worker quiescence proof before explicitly authorized resume."""
        baseline=self._baseline()
        need(job.receipt is not None and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',job.receipt.execution_id),
             'An independently committed prior execution is required.')
        name=self.job_name+'/executions/'+job.receipt.execution_id
        execution=self.read('https://run.googleapis.com/v2/'+name)
        need(execution.get('name')==name and execution.get('job')==self.job_name and isinstance(execution.get('uid'),str)
             and bool(execution['uid']) and not execution.get('deleteTime'),'Prior execution identity is unavailable.')
        expected=self._expected_task(job,self.target,baseline)
        need(type(execution.get('taskCount')) is int and execution['taskCount']==1
             and type(execution.get('parallelism')) is int and execution['parallelism']==1
             and normalized_task(execution.get('template'))==expected,'Prior execution differs from approved job/configuration.')
        running=execution.get('runningCount',0)
        need(type(running) is int and running>=0,'Invalid prior execution task count.')
        counts=[execution.get(k,0) for k in ('succeededCount','failedCount','cancelledCount')]
        need(all(type(n) is int and n>=0 for n in counts),'Invalid terminal task counts.')
        if execution.get('reconciling',False) is not False or running!=0 or sum(counts)!=1:return False
        completed=execution.get('completionTime')
        if not isinstance(completed,str):return False
        from datetime import datetime,timezone
        try:when=datetime.fromisoformat(completed.replace('Z','+00:00'))
        except ValueError:return False
        if when.tzinfo is None or when>datetime.now(timezone.utc):return False
        conditions=execution.get('conditions',[])
        return isinstance(conditions,list) and any(isinstance(c,dict) and c.get('type')=='Completed'
            and c.get('state') in {'CONDITION_SUCCEEDED','CONDITION_FAILED'} for c in conditions)

    def _expected_task(self,job,target,baseline):
        trusted={'job_id':job.id,'owner_id':job.owner_id,'snapshot_sha256':job.snapshot_sha256,'frozen_job':job.snapshot}
        if self.expected_request_loader is not None:
            request=self.expected_request_loader(job,target)
            need(isinstance(request,dict) and request.get('method')=='POST'
                 and request.get('url')=='https://run.googleapis.com/v2/'+self.job_name+':run',
                 'Trusted original-input request escaped the exact target.')
        else:
            manifest,reference=self.handoff_loader(job)
            request=build_execution_request(target,manifest,reference,trusted_job=trusted)
        override=request['json']['overrides'];expected=copy.deepcopy(baseline)
        env={item['name']:item for item in expected['containers'][0].get('env',[])}
        for item in override['containerOverrides'][0]['env']:env[item['name']]=dict(item)
        expected['containers'][0]['env']=sorted(env.values(),key=lambda x:x['name'])
        expected['timeout']=override['timeout']
        return expected

    def execution_resolver(self,operation,target,job):
        need(target==self.target,'Target changed during execution recovery.')
        baseline=self._baseline()
        need(isinstance(operation,str) and operation.startswith(self.operation_prefix)
             and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',operation[len(self.operation_prefix):]),'Operation escaped configured target.')
        op=self.read('https://run.googleapis.com/v2/'+operation)
        need(op.get('name')==operation and 'error' not in op,'Operation is missing or failed.')
        # Official RunJob proto declares both metadata and response as Execution.
        candidate=op.get('response') if op.get('done') is True and isinstance(op.get('response'),dict) else op.get('metadata')
        need(isinstance(candidate,dict) and candidate.get('@type')=='type.googleapis.com/google.cloud.run.v2.Execution','Execution metadata is not available yet.')
        name=candidate.get('name');prefix=self.job_name+'/executions/'
        need(isinstance(name,str) and name.startswith(prefix) and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',name[len(prefix):]),'Operation refers to another execution/job.')
        execution=self.read('https://run.googleapis.com/v2/'+name)
        need(execution.get('name')==name and execution.get('job')==self.job_name and isinstance(execution.get('uid'),str)
             and bool(execution['uid']) and not execution.get('deleteTime'),'Execution identity is unavailable.')
        if candidate.get('uid') is not None:need(candidate['uid']==execution['uid'],'Execution UID changed.')
        expected=self._expected_task(job,target,baseline)
        need(execution.get('taskCount')==1 and type(execution.get('taskCount')) is int
             and execution.get('parallelism')==1 and type(execution.get('parallelism')) is int
             and normalized_task(execution.get('template'))==expected,'Execution does not bind exact target, job and manifest overrides.')
        return dict(operation_name=operation,execution_name=name,target_sha256=fingerprint(target),snapshot_sha256=job.snapshot_sha256)
