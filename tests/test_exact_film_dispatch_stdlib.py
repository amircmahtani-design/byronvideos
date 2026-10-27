"""Offline exact-film dispatch contracts; fake authorization and cloud I/O only."""
import copy,unittest
from types import SimpleNamespace
from byron_video.hosted_control import digest
from byron_video.exact_film_dispatch import *
from test_cloud_journal_stdlib import Client,PROJECT,OWNER
from test_deployment_plan_stdlib import config

class ExactDispatchChecks(unittest.TestCase):
    def setUp(self):
        self.config=config();self.target=self.config['target'];self.approval={'fixture_this_film_only':True}
        self.job=SimpleNamespace(id='1'*32,owner_id=OWNER,state='draft_preview_ready',artifact=object(),
            snapshot={'runtime_config_sha256':digest(self.config)},snapshot_sha256='2'*64)
        self.db=Client();self.journal=FirestoreExactFilmDispatchJournal(self.db,project_id=PROJECT,namespace='fixture',transaction_runner=self.db.run)
        self.calls=[];self.active=True;self.valid=True;self.uncertain=False
        prefix=f"projects/{self.target['project_id']}/locations/{self.target['region']}"
        self.operation=prefix+'/operations/fixture_operation'
        def transport(*args):
            self.calls.append(args)
            if self.uncertain:raise TimeoutError()
            return {'name':self.operation}
        self.reads=[]
        def resolve(operation,target,job):
            self.reads.append(operation)
            return dict(operation_name=operation,target_sha256=digest(target),snapshot_sha256=job.snapshot_sha256,
                execution_name=prefix+'/jobs/'+target['job_name']+'/executions/fixture_clean_execution')
        self.readback=SimpleNamespace(target_provider=lambda:copy.deepcopy(self.target),execution_resolver=resolve)
        self.dispatch=ExactFilmDispatcher(target=self.target,config_sha256=digest(self.config),journal=self.journal,
            approval_loader=lambda job:copy.deepcopy(self.approval),approval_validator=lambda job,a:self.valid,
            readback=self.readback,transport=transport,activation_check=lambda:self.active,enabled=True)
    def test_exact_protocol_and_no_generation_env(self):
        request=build_request(self.target,self.job,digest(self.config),digest(self.approval))
        env={x['name']:x['value'] for x in request['json']['overrides']['containerOverrides'][0]['env']}
        self.assertNotIn('BYRON_INPUT_PROTOCOL',env)
        self.assertEqual(env['BYRON_EXACT_FILM_PROTOCOL'],'exact-film-v1')
        env['CLOUD_RUN_EXECUTION']='fixture_clean_execution'
        self.assertEqual(invocation_from_environment(env,self.config)['approval_sha256'],digest(self.approval))
        env['BYRON_CONFIG_SHA256']='f'*64
        with self.assertRaises(ValueError):invocation_from_environment(env,self.config)
    def test_once_then_readback_only_original_job_unchanged(self):
        first=self.dispatch(self.job,self.approval);again=self.dispatch(self.job,self.approval)
        self.assertEqual(first,again);self.assertEqual(len(self.calls),1);self.assertEqual(len(self.reads),2)
        self.assertEqual(self.job.state,'draft_preview_ready')
        self.assertEqual(self.journal.get_receipt(self.job,self.approval),first)
    def test_unknown_post_is_never_resubmitted(self):
        self.uncertain=True
        with self.assertRaises(DispatchPending):self.dispatch(self.job,self.approval)
        self.uncertain=False
        with self.assertRaises(DispatchPending):self.dispatch(self.job,self.approval)
        self.assertEqual(len(self.calls),1)
    def test_disabled_expired_or_changed_approval_no_post(self):
        self.active=False
        with self.assertRaises(ValueError):self.dispatch(self.job,self.approval)
        self.active=True;self.valid=False
        with self.assertRaises(ValueError):self.dispatch(self.job,self.approval)
        self.valid=True
        with self.assertRaises(ValueError):self.dispatch(self.job,{'changed':True})
        self.assertEqual(self.calls,[])
    def test_readback_other_snapshot_cannot_commit(self):
        self.readback.execution_resolver=lambda *a:dict(operation_name=self.operation,target_sha256=digest(self.target),snapshot_sha256='f'*64,
            execution_name='unrelated')
        with self.assertRaises(DispatchPending):self.dispatch(self.job,self.approval)
        self.assertIsNone(self.journal.get_receipt(self.job,self.approval))
        with self.assertRaises(ValueError):self.dispatch(self.job,self.approval)
        self.assertEqual(len(self.calls),1)
    def test_terminal_clean_execution_holds_without_resubmission(self):
        receipt=self.dispatch(self.job,self.approval)
        self.readback.terminal_execution_with_receipt=lambda original,clean:False
        self.assertEqual(self.dispatch.pending_status(self.job,self.approval,receipt),'queued')
        def terminal(original,clean):
            self.assertIs(original,self.job)
            self.assertEqual(clean,receipt)
            return True
        self.readback.terminal_execution_with_receipt=terminal
        self.assertEqual(self.dispatch.pending_status(self.job,self.approval,receipt),'HOLD')
        self.assertEqual(len(self.calls),1)
    def test_final_gate_runs_after_token_read_before_cloud_post(self):
        from byron_video.worker_binding import HttpxCloudRunTransport
        posted=[]
        class Client:
            def __init__(self,**kwargs):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def stream(self,*args,**kwargs):posted.append(args);raise AssertionError('must not POST')
        def token():self.valid=False;return 'fixture_short_lived_token'
        self.dispatch.transport=HttpxCloudRunTransport(token,client_factory=Client)
        with self.assertRaises(DispatchPending):self.dispatch(self.job,self.approval)
        self.assertEqual(posted,[])
    def test_new_approval_cannot_reset_one_clean_attempt(self):
        self.dispatch(self.job,self.approval)
        self.approval={'fixture_this_film_only':True,'renewal':1}
        with self.assertRaises(ValueError):self.dispatch(self.job,self.approval)
        self.assertEqual(len(self.calls),1)
    def test_second_owner_cannot_reuse_execution(self):
        self.dispatch(self.job,self.approval)
        other=copy.copy(self.job);other.owner_id='fixture_other'
        with self.assertRaises(DispatchPending):self.dispatch(other,self.approval)
        self.assertIsNone(self.journal.get_receipt(other,self.approval))

if __name__=='__main__':unittest.main()
