"""Official-resource-shaped readback fixtures, not live Cloud Run evidence."""
import copy
from types import SimpleNamespace
import unittest
from byron_video.cloud_run_readback import CloudRunReadback,CloudRunReadTransport,normalized_task
from byron_video.cloud_render import build_execution_request
from byron_video.elevenlabs_adapter import fingerprint
from test_worker_binding_stdlib import fixture


class CloudReadbackChecks(unittest.TestCase):
    def setUp(self):
        self.target,self.manifest,self.ref,trusted,_,_=fixture()
        self.job=SimpleNamespace(id=trusted['job_id'],owner_id=trusted['owner_id'],snapshot_sha256=trusted['snapshot_sha256'],snapshot=trusted['frozen_job'])
        t=self.target;self.name=f"projects/{t['project_id']}/locations/{t['region']}/jobs/{t['job_name']}"
        self.operation=f"projects/{t['project_id']}/locations/{t['region']}/operations/fixture_operation"
        self.execution=self.name+'/executions/fixture_execution'
        self.account='fixture-worker@synthetic-project.iam.gserviceaccount.com'
        self.task=dict(containers=[dict(name='renderer',image=t['container_image'],env=[],resources=dict(limits={'cpu':'2','memory':'4Gi'}))],serviceAccount=self.account,maxRetries=0,timeout='1800s')
        self.resources={self.name:dict(name=self.name,reconciling=False,terminalCondition={'state':'CONDITION_SUCCEEDED'},template=dict(taskCount=1,parallelism=1,template=self.task)),
            self.operation:dict(name=self.operation,done=False,metadata={'@type':'type.googleapis.com/google.cloud.run.v2.Execution','name':self.execution,'uid':'fixture-uid'})}
        task=copy.deepcopy(self.task);task['containers'][0]['env']=build_execution_request(t,self.manifest,self.ref,trusted_job=trusted)['json']['overrides']['containerOverrides'][0]['env']
        self.resources[self.execution]=dict(name=self.execution,job=self.name,uid='fixture-uid',taskCount=1,parallelism=1,template=task)
        self.calls=[]
    def adapter(self,enabled=True):
        def read(url):
            self.calls.append(url);return copy.deepcopy(self.resources[url.removeprefix('https://run.googleapis.com/v2/')])
        return CloudRunReadback(target=self.target,read=read,expected_task_template_sha256=fingerprint(normalized_task(self.task)),expected_service_account=self.account,handoff_loader=lambda job:(self.manifest,self.ref),enabled=enabled)
    def test_default_disabled_does_not_read(self):
        with self.assertRaises(ValueError):self.adapter(False).target_provider()
        self.assertEqual(self.calls,[])
    def test_job_readback_matches_exact_reviewed_template(self):
        self.assertEqual(self.adapter().target_provider(),self.target)
    def test_omitted_reconciling_uses_false_default_when_ready(self):
        del self.resources[self.name]['reconciling']
        self.assertEqual(self.adapter().target_provider(),self.target)
        self.resources[self.name]['terminalCondition']['state']='CONDITION_PENDING'
        with self.assertRaises(ValueError):self.adapter().target_provider()
    def test_omitted_max_retries_is_not_zero(self):
        del self.resources[self.name]['template']['template']['maxRetries']
        # Even a reviewed digest of this task cannot authorize the provider's
        # omitted-retries default of three rather than explicit no retries.
        with self.assertRaises(ValueError):self.adapter().target_provider()
    def test_running_operation_binds_real_execution_without_waiting_for_completion(self):
        result=self.adapter().execution_resolver(self.operation,self.target,self.job)
        self.assertEqual(result['execution_name'],self.execution)
        self.assertEqual(result['snapshot_sha256'],self.job.snapshot_sha256)
        self.assertEqual(len(self.calls),3)
    def test_changed_environment_or_resources_rejected(self):
        self.resources[self.name]['template']['template']['containers'][0]['env']=[{'name':'UNREVIEWED','value':'fixture'}]
        with self.assertRaises(ValueError):self.adapter_with_original_digest().target_provider()
    def adapter_with_original_digest(self):
        original=copy.deepcopy(self.task);original['containers'][0]['env']=[]
        adapter=self.adapter();adapter.template_sha=fingerprint(normalized_task(original));return adapter
    def test_wrong_job_override_rejected(self):
        self.resources[self.execution]['template']['containers'][0]['env'][0]['value']='changed'
        with self.assertRaises(ValueError):self.adapter().execution_resolver(self.operation,self.target,self.job)
    def test_operation_metadata_cannot_select_other_job(self):
        self.resources[self.operation]['metadata']['name']='projects/other/locations/us-central1/jobs/other/executions/fixture_execution'
        with self.assertRaises(ValueError):self.adapter().execution_resolver(self.operation,self.target,self.job)
        self.assertEqual(len(self.calls),2)
    def test_reconciling_target_and_duplicate_env_fail_closed(self):
        self.resources[self.name]['reconciling']=True
        with self.assertRaises(ValueError):self.adapter().target_provider()
        with self.assertRaises(ValueError):normalized_task({'containers':[{'name':'renderer','env':[{'name':'X'},{'name':'X'}]}]})
    def test_explicit_original_request_loader_cannot_escape_target(self):
        adapter=self.adapter();adapter.handoff_loader=None
        trusted={'job_id':self.job.id,'owner_id':self.job.owner_id,'snapshot_sha256':self.job.snapshot_sha256,'frozen_job':self.job.snapshot}
        request=build_execution_request(self.target,self.manifest,self.ref,trusted_job=trusted)
        adapter.expected_request_loader=lambda job,target:copy.deepcopy(request)
        self.assertEqual(adapter.execution_resolver(self.operation,self.target,self.job)['execution_name'],self.execution)
        request['url']='https://run.googleapis.com/v2/projects/other/locations/us-central1/jobs/other:run'
        with self.assertRaises(ValueError):adapter.execution_resolver(self.operation,self.target,self.job)

    def test_separate_receipt_preserves_original_request_identity(self):
        from byron_video.hosted_control import DispatchReceipt
        original_receipt=SimpleNamespace(execution_id='original_preview_execution')
        self.job.receipt=original_receipt;self.job.state='draft_preview_ready';self.job.artifact=object()
        trusted=dict(job_id=self.job.id,owner_id=self.job.owner_id,snapshot_sha256=self.job.snapshot_sha256,frozen_job=self.job.snapshot)
        expected=build_execution_request(self.target,self.manifest,self.ref,trusted_job=trusted)
        adapter=self.adapter();adapter.handoff_loader=None
        def load(original,target):
            self.assertIs(original,self.job);self.assertIs(original.receipt,original_receipt)
            self.assertEqual(original.state,'draft_preview_ready');self.assertIsNotNone(original.artifact)
            return copy.deepcopy(expected)
        adapter.expected_request_loader=load
        receipt=DispatchReceipt(self.job.id,self.job.snapshot_sha256,'fixture_execution')
        self.assertFalse(adapter.terminal_execution_with_receipt(self.job,receipt))
        self.resources[self.execution].update(completionTime='2026-01-01T00:00:00Z',runningCount=0,failedCount=1,
            conditions=[{'type':'Completed','state':'CONDITION_FAILED'}])
        self.assertTrue(adapter.terminal_execution_with_receipt(self.job,receipt))
        self.assertIs(self.job.receipt,original_receipt);self.assertIs(adapter.expected_request_loader,load)
    def test_terminal_execution_requires_quiescent_exact_completed_resource(self):
        self.job.receipt=SimpleNamespace(execution_id='fixture_execution')
        adapter=self.adapter()
        self.assertFalse(adapter.terminal_execution(self.job))
        e=self.resources[self.execution];e.update(completionTime='2026-01-01T00:00:00Z',succeededCount=1,
            conditions=[{'type':'Completed','state':'CONDITION_SUCCEEDED'}])
        self.assertTrue(adapter.terminal_execution(self.job))
        e['runningCount']=1;self.assertFalse(adapter.terminal_execution(self.job));e['runningCount']=0
        e['reconciling']=True;self.assertFalse(adapter.terminal_execution(self.job));e['reconciling']=False
        e['completionTime']='2999-01-01T00:00:00Z';self.assertFalse(adapter.terminal_execution(self.job))
        e['job']='other-job'
        with self.assertRaises(ValueError):adapter.terminal_execution(self.job)

    def test_read_transport_wall_deadline_and_encoded_response_rejected(self):
        class Response:
            status_code=200
            def __init__(self,headers):self.headers=headers
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_bytes(self):yield b'{}'
        class Client:
            def __init__(self,reply,calls):self.reply,self.calls=reply,calls
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def stream(self,*args,**kwargs):self.calls.append((args,kwargs));return self.reply
        for headers,clock in [({},iter([0,0,31]).__next__),({'content-encoding':'gzip'},lambda:0)]:
            calls=[]
            transport=CloudRunReadTransport(lambda:'synthetic-token-only',enabled=True,clock=clock,
                client_factory=lambda **kwargs:Client(Response(headers),calls))
            with self.assertRaises(ValueError):transport('https://run.googleapis.com/v2/'+self.name)
            self.assertEqual(len(calls),1)
            self.assertEqual(calls[0][0][0],'GET')

    def test_read_transport_disabled_before_token_or_network(self):
        calls=[];transport=CloudRunReadTransport(lambda:calls.append(1))
        with self.assertRaises(ValueError):transport('https://run.googleapis.com/v2/'+self.name)
        self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
