"""Exact-film approval flow: real local media, synthetic services only.

The retained preview is built through the complete API/native-planner/native-QC
fixture. The service responses and identity/SDK admission are synthetic, not live
acceptance. Finalization must reuse retained media without any provider call.
"""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone
from contextlib import ExitStack
from unittest.mock import patch
from test_hosted_uploads_stdlib import plan as base_plan
from test_private_preview_flow_stdlib import PreviewFlowFixture
from fresh_app_fixture import TOKEN
from byron_video.hosted_control import Principal


class RetainedPreviewFixture:
    """Fresh draft with a reversible fake-cloud checkpoint for isolated tests."""
    def __init__(self,root):
        self.stack=ExitStack();self.flow=None
        try:self._initialize(root)
        except BaseException:
            if self.flow is not None:self.flow.close()
            self.stack.close()
            raise
    def _initialize(self,root):
        self.root=Path(root);self.created_times={}
        from test_cloud_journal_stdlib import Transaction,Ref
        original_create,original_get=Transaction.create,Ref.get
        def create(tx,ref,value):
            result=original_create(tx,ref,value)
            self.created_times.setdefault(ref.path,datetime.now(timezone.utc))
            return result
        def get(ref,transaction=None):
            result=original_get(ref,transaction=transaction)
            result.create_time=self.created_times.get(ref.path)
            return result
        self.stack.enter_context(patch.object(Transaction,'create',create))
        self.stack.enter_context(patch.object(Ref,'get',get))
        # New assembly imports these primitives at construction. Forward only to
        # the already injected local fake HTTP closures, never to a network client.
        from byron_video import runtime_assembly
        self.stack.enter_context(patch('byron_video.worker_binding.HttpxCloudRunTransport',side_effect=lambda *a,**kw:runtime_assembly.HttpxCloudRunTransport(*a,**kw)))
        self.stack.enter_context(patch('byron_video.cloud_run_readback.CloudRunReadTransport',side_effect=lambda *a,**kw:runtime_assembly.CloudRunReadTransport(*a,**kw)))
        from byron_video.exact_film_runtime import contract
        processing={**base_plan(),'exact_film_contract':contract()}
        with patch('test_private_preview_flow_stdlib.plan',lambda:processing):
            self.flow=PreviewFlowFixture(self.root)
        self.job=self.flow.submit();self.worker,self.invocation=self.flow.worker()
        result=self.worker.run_original(self.invocation,self.root/'initial-preview')
        assert result['completion_reconciled'],result
        self.api=self.flow.api
        self.job=self.api.runtime.jobs.get(self.api.owner,self.job.id)
        assert self.job.state=='draft_preview_ready'
        downloaded=self.api.client.get('/api/jobs/'+self.job.id+'/video')
        assert downloaded.status_code==200,downloaded.text[:100]
        self.preview_bytes=downloaded.content
        assert hashlib.sha256(self.preview_bytes).hexdigest()==self.job.artifact.sha256
        self.checkpoint=dict(db=copy.deepcopy(self.api.firestore.data),objects=copy.deepcopy(self.api.bucket.objects),
            serial=self.api.bucket.serial,writes=list(self.api.bucket.writes),resources=copy.deepcopy(self.api.resources),
            runs=copy.deepcopy(self.api.run_requests),posts=list(self.flow.posts),calls=list(self.flow.media.calls),created_times=copy.deepcopy(self.created_times))
    def reset(self):
        saved=self.checkpoint
        self.created_times.clear();self.created_times.update(copy.deepcopy(saved['created_times']))
        self.api.firestore.data=copy.deepcopy(saved['db']);self.api.firestore.unknown_commit=False
        self.api.bucket.objects=copy.deepcopy(saved['objects']);self.api.bucket.serial=saved['serial']
        self.api.bucket.writes=list(saved['writes']);self.api.bucket.fail_after=None
        self.api.resources.clear();self.api.resources.update(copy.deepcopy(saved['resources']))
        self.api.run_requests[:]=copy.deepcopy(saved['runs']);self.flow.posts[:]=saved['posts'];self.flow.media.calls[:]=saved['calls']
        self.api.client.cookies.clear();self.api.client.cookies.set('__Host-byron_session',TOKEN)
    def provider_trace(self):return (list(self.flow.posts),list(self.flow.media.calls))
    def close(self):
        self.flow.close();self.stack.close()


class ExactFilmFlowChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.addClassCleanup(cls.temp.cleanup)
        cls.retained=RetainedPreviewFixture(Path(cls.temp.name));cls.addClassCleanup(cls.retained.close)
    def setUp(self):
        self.f=self.retained;self.f.reset();self.api=self.f.api;self.graph=self.api.runtime.exact_films
        import time
        self.graph.actions.now=time.time;self.graph.lineage.now=time.time
        self.api.runtime.previews.authorization_verifier.primary.authorizations.now=time.time
        self.root=self.f.root/self._testMethodName;self.root.mkdir()
        self.before=self.f.provider_trace()
    def headers(self,key):return {**self.api.headers,'Idempotency-Key':key}
    def challenge(self,*,renew=False,artifact_id=None):
        route='preview-view-renewal' if renew else 'film-approval'
        field='artifact_id' if renew else 'preview_id'
        response=self.api.client.post('/api/jobs/'+self.f.job.id+'/'+route+'/challenge',json={field:artifact_id or self.f.job.artifact.artifact_id},headers=self.api.headers)
        self.assertEqual(response.status_code,200,response.text)
        value=response.json();return {field:artifact_id or self.f.job.artifact.artifact_id,'challenge_id':value['challenge_id'],'challenge_sha256':value['challenge_sha256']}
    def approve(self):
        body=self.challenge();headers=self.headers('exact_film_owner_approval_001')
        result=self.api.client.post('/api/jobs/'+self.f.job.id+'/film-approval',json=body,headers=headers)
        self.assertEqual(result.status_code,202,result.text);self.assertFalse(result.json()['reused'])
        self.assertEqual(len(self.api.run_requests),2)
        pending=self.api.client.get('/api/jobs/'+self.f.job.id+'/exact-film')
        self.assertEqual(pending.status_code,200,pending.text)
        self.assertEqual(pending.json()['status'],'queued')
        return body,headers
    def invocation(self):
        from byron_video.exact_film_dispatch import invocation_from_environment
        env={item['name']:item['value'] for item in self.api.run_requests[-1]['overrides']['containerOverrides'][0]['env']}
        env['CLOUD_RUN_EXECUTION']='renderer-fixture-2'
        return invocation_from_environment(env,self.api.config)
    def assert_no_provider_calls(self):self.assertEqual(self.f.provider_trace(),self.before)
    def test_current_owner_approval_same_media_final_private_download_and_replay(self):
        from byron_video.preview_composition import verify_watermark
        body,headers=self.approve();invocation=self.invocation()
        result=self.f.worker.run_exact_film(invocation,self.root/'final-worker')
        self.assertTrue(result['completion_reconciled'],result);self.assert_no_provider_calls()
        status=self.api.client.get('/api/jobs/'+self.f.job.id+'/exact-film')
        self.assertEqual(status.status_code,200,status.text);self.assertEqual(status.json()['status'],'ready')
        response=self.api.client.get(status.json()['download_url'])
        self.assertEqual(response.status_code,200,response.text[:100]);self.assertEqual(response.headers['x-byron-artifact-kind'],'OWNER_APPROVED_EXACT_FILM')
        proof,_,_,_=self.graph.registry.load(Principal(self.api.owner),self.f.job)
        self.assertEqual(hashlib.sha256(response.content).hexdigest(),proof.sha256)
        self.assertNotEqual(response.content,self.f.preview_bytes)
        final=self.root/'final.mp4';final.write_bytes(response.content)
        with self.assertRaises(ValueError):verify_watermark(final)
        self.assertEqual(self.api.runtime.jobs.get(self.api.owner,self.f.job.id),self.f.job)
        self.assertEqual(self.api.client.get('/api/jobs/'+self.f.job.id+'/video').content,self.f.preview_bytes)
        repeated=self.api.client.post('/api/jobs/'+self.f.job.id+'/film-approval',json=body,headers=headers)
        self.assertEqual(repeated.status_code,202,repeated.text);self.assertTrue(repeated.json()['reused'])
        self.assertEqual(len(self.api.run_requests),2)
        again=self.f.worker.run_exact_film(invocation,self.root/'do-not-render')
        self.assertTrue(again['already_complete']);self.assert_no_provider_calls()
        import os,json
        if os.environ.get('BYRON_EXACT_FILM_PROOF_DIR'):
            preserved=Path(os.environ['BYRON_EXACT_FILM_PROOF_DIR']);preserved.mkdir(parents=True,exist_ok=True)
            (preserved/'final.mp4').write_bytes(response.content);(preserved/'preview.mp4').write_bytes(self.f.preview_bytes)
            (preserved/'SYNTHETIC-ONLY.json').write_text(json.dumps(dict(final_sha256=proof.sha256,preview_sha256=self.f.job.artifact.sha256,provider_calls='FAKE ONLY',live_acceptance=False),indent=2))
        self.api.client.cookies.clear();self.assertEqual(self.api.client.get(status.json()['download_url']).status_code,401)
    def test_csrf_cross_owner_and_challenge_action_confusion_fail_closed(self):
        from byron_video.hosted_control import Principal
        from fastapi import HTTPException
        target='/api/jobs/'+self.f.job.id+'/film-approval/challenge'
        result=self.api.client.post(target,json={'preview_id':self.f.job.artifact.artifact_id})
        self.assertIn(result.status_code,(401,403))
        with self.assertRaises(HTTPException) as failure:self.graph.service.challenge(Principal('another_owner'),self.f.job.id,self.f.job.artifact.artifact_id,'RENDER_THIS_FILM_PRIVATELY_WITHOUT_DRAFT_WATERMARK')
        self.assertEqual(failure.exception.status_code,404)
        view=self.challenge(renew=True)
        forged={'preview_id':view['artifact_id'],'challenge_id':view['challenge_id'],'challenge_sha256':view['challenge_sha256']}
        result=self.api.client.post('/api/jobs/'+self.f.job.id+'/film-approval',json=forged,headers=self.headers('wrong_action_fixture_001'))
        self.assertEqual(result.status_code,409);self.assertEqual(len(self.api.run_requests),1);self.assert_no_provider_calls()
    def test_expired_generation_auth_only_renews_view_for_exact_retained_preview(self):
        future=self.f.flow.expires+1
        self.graph.actions.now=lambda:future;self.graph.lineage.now=lambda:future
        self.api.runtime.previews.authorization_verifier.primary.authorizations.now=lambda:future
        url='/api/jobs/'+self.f.job.id+'/video'
        self.assertEqual(self.api.client.get(url).status_code,503)
        body=self.challenge(renew=True)
        route='/api/jobs/'+self.f.job.id+'/preview-view-renewal';headers=self.headers('renew_retained_view_fixture_001')
        response=self.api.client.post(route,json=body,headers=headers)
        self.assertEqual(response.status_code,200,response.text)
        download=self.api.client.get(url);self.assertEqual(download.status_code,200,download.text[:100]);self.assertEqual(download.content,self.f.preview_bytes)
        self.assertTrue(self.api.client.post(route,json=body,headers=headers).json()['reused'])
        self.assertIsNone(self.graph.actions.latest_approval(Principal(self.api.owner),self.f.job))
        with self.assertRaises(ValueError):self.api.runtime.previews.authorization_verifier.primary.authorizations.load(self.f.job)
        self.assertEqual(len(self.api.run_requests),1);self.assert_no_provider_calls()
    def test_tampered_retained_video_blocks_clean_output(self):
        self.approve();invocation=self.invocation()
        approval=self.graph.actions.get_approval(self.f.job)
        from byron_video.hosted_control import Principal
        lineage=self.graph.actions.approval_lineage(Principal(self.api.owner),self.f.job,approval['approval_id'])
        name=lineage['retained_manifest']['clips'][0]['video']['object']
        generation,data,mime=self.api.bucket.objects[name]
        self.api.bucket.objects[name]=(generation,b'corrupt retained media',mime)
        with self.assertRaises(Exception):self.f.worker.run_exact_film(invocation,self.root/'corrupt')
        self.assertNotEqual(self.api.client.get('/api/jobs/'+self.f.job.id+'/approved-video').status_code,200)
        self.assert_no_provider_calls();self.assertEqual(len(self.api.run_requests),2)
    def test_unknown_render_attempt_is_hold_and_never_automatically_renders_again(self):
        from byron_video.exact_film_composition import ExactFilmCompositor
        self.approve();invocation=self.invocation()
        with patch.object(ExactFilmCompositor,'__call__',side_effect=TimeoutError('synthetic unknown local render')) as render:
            with self.assertRaises(Exception):self.f.worker.run_exact_film(invocation,self.root/'unknown')
            self.assertEqual(render.call_count,1)
            with self.assertRaises(Exception):self.f.worker.run_exact_film(invocation,self.root/'no-retry')
            self.assertEqual(render.call_count,1)
        # Independent fake Cloud Run readback now proves this process stopped.
        execution=self.api.execution+'-2'
        self.api.resources[execution].update(completionTime=datetime.now(timezone.utc).isoformat(),runningCount=0,
            succeededCount=0,failedCount=1,conditions=[dict(type='Completed',state='CONDITION_FAILED')])
        status=self.api.client.get('/api/jobs/'+self.f.job.id+'/exact-film')
        self.assertEqual(status.status_code,200,status.text);self.assertEqual(status.json()['status'],'HOLD')
        self.assertNotEqual(self.api.client.get('/api/jobs/'+self.f.job.id+'/approved-video').status_code,200)
        self.assertEqual(len(self.api.run_requests),2);self.assert_no_provider_calls()

if __name__=='__main__':unittest.main()
