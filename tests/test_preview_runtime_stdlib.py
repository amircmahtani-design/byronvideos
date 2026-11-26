"""Ephemeral synthetic preview signatures only; no real acceptance or keys saved."""
import base64,copy,time,unittest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from byron_video.hosted_control import canonical,digest
from byron_video.preview_runtime import verify_runtime_authorization,PREVIEW_EVIDENCE,PURPOSE
from byron_video.runtime_assembly import assemble_runtime,verify_acceptance
from byron_video.original_input import OriginalInputRequestLoader
from test_deployment_plan_stdlib import config
from test_hosted_uploads_stdlib import plan
import test_runtime_assembly_stdlib as historical


def preview_plan():
    from byron_video.private_preview import policy
    from byron_video.source_planning import policy as source_policy
    from byron_video.automated_review import candidate_policy
    from byron_video.preview_composition import release_hash
    value=plan()
    value['preview_contract']=dict(mode='PRIVATE_DRAFT_PREVIEW',preview_policy_sha256=digest(policy()),
        preview_release_sha256=release_hash(),source_policy_sha256=digest(source_policy()),review_policy_sha256=digest(candidate_policy()))
    return value


def signed_preview(config_value,plan_value,*,now=None):
    now=int(time.time()) if now is None else now
    private=Ed25519PrivateKey.generate()  # Ephemeral FIXTURE, never persisted.
    public=private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
    payload=dict(schema_version=1,purpose=PURPOSE,status='PRIVATE_PREVIEW_AUTHORIZED',config_sha256=digest(config_value),
        plan_sha256=digest(plan_value),preview_contract_sha256=digest(plan_value['preview_contract']),issued_at=now-10,expires_at=now+600,
        evidence={key:OriginalInputRequestLoader(digest(config_value)).release_sha256 if key=='handoff_adapter' else 'e'*64 for key in PREVIEW_EVIDENCE},
        owner_only=True,public_release=False)
    envelope=dict(payload=payload,signature=base64.b64encode(private.sign(canonical(payload).encode())).decode())
    return envelope,public,private


class PreviewRuntimeChecks(unittest.TestCase):
    def setUp(self):
        self.config,self.plan=config(),preview_plan()
        self.envelope,self.public,self.private=signed_preview(self.config,self.plan,now=1000)
    def verify(self,envelope=None,public=None,**changes):
        args=dict(preview_contract=self.plan['preview_contract'],now=1000);args.update(changes)
        return verify_runtime_authorization(envelope or self.envelope,public or self.public,digest(self.config),digest(self.plan),**args)
    def test_distinct_signed_preview_purpose_is_not_final_acceptance(self):
        self.assertEqual(self.verify()['status'],'PRIVATE_PREVIEW_AUTHORIZED')
        with self.assertRaises(ValueError):verify_acceptance(self.envelope,self.public,digest(self.config),digest(self.plan),now=1000)
        final,key,_=historical.signed(self.config,self.plan,now=1000)
        with self.assertRaises(ValueError):self.verify(final,key)
    def test_wrong_key_config_plan_and_intent_deny(self):
        _,other,_=signed_preview(self.config,self.plan,now=1000)
        with self.assertRaises(ValueError):self.verify(public=other)
        for cfg,pl in [('0'*64,digest(self.plan)),(digest(self.config),'0'*64)]:
            with self.assertRaises(ValueError):verify_runtime_authorization(self.envelope,self.public,cfg,pl,preview_contract=self.plan['preview_contract'],now=1000)
        with self.assertRaises(ValueError):self.verify(preview_contract={**self.plan['preview_contract'],'preview_release_sha256':'0'*64})
    def test_expired_future_and_overlong_authorization_deny(self):
        for now in [980,1600,1700]:
            with self.subTest(now=now),self.assertRaises(ValueError):self.verify(now=now)
        value=copy.deepcopy(self.envelope);value['payload']['expires_at']=1000+8*86400
        value['signature']=base64.b64encode(self.private.sign(canonical(value['payload']).encode())).decode()
        with self.assertRaises(ValueError):self.verify(value)
    def test_signed_public_or_nonowner_flags_and_tampering_deny(self):
        for change in [{'public_release':True},{'owner_only':False},{'status':'ACCEPTED'},{'purpose':'byron-runtime-acceptance-v1'}]:
            value=copy.deepcopy(self.envelope);value['payload'].update(change)
            value['signature']=base64.b64encode(self.private.sign(canonical(value['payload']).encode())).decode()
            with self.subTest(change=change),self.assertRaises(ValueError):self.verify(value)
        value=copy.deepcopy(self.envelope);value['payload']['evidence']['draft_policy_review']='0'*64
        with self.assertRaises(ValueError):self.verify(value)
    def test_exact_signed_preview_assembly_has_concrete_verifier_without_io(self):
        from byron_video.preview_registry import PreviewAuthorizationVerifier
        args=historical.RuntimeAssemblyChecks().arguments(processing_plan=self.plan)
        envelope,key,_=signed_preview(args['config'],self.plan)
        runtime=assemble_runtime(**args,enabled=True,acceptance=envelope,reviewer_public_key=key)
        self.assertTrue(runtime.enabled)
        self.assertIs(type(runtime.previews.authorization_verifier),PreviewAuthorizationVerifier)
        self.assertEqual(runtime.previews.authorization_verifier.authorizations.release,self.plan['preview_contract']['preview_release_sha256'])
    def test_preview_disabled_by_default_and_needs_separate_signed_acceptance(self):
        args=historical.RuntimeAssemblyChecks().arguments(processing_plan=self.plan)
        runtime=assemble_runtime(**args);self.assertFalse(runtime.enabled)
        final,key,_=historical.signed(args['config'],self.plan)
        with self.assertRaises(ValueError):assemble_runtime(**args,enabled=True,acceptance=final,reviewer_public_key=key)
        with self.assertRaises(ValueError):assemble_runtime(**args,enabled=True)

    def test_preview_queued_running_and_hold_never_report_live_release(self):
        from unittest.mock import patch
        from fresh_app_fixture import FreshApplicationFixture
        with patch('fresh_app_fixture.signed',signed_preview):
            fixture=FreshApplicationFixture(processing_plan=preview_plan())
        try:
            job=fixture.submit()
            for state in ['queued','running','HOLD']:
                if state=='running':fixture.runtime.jobs.record_started(job.owner_id,job.id,job.receipt.execution_id)
                if state=='HOLD':fixture.runtime.jobs.record_worker_hold(job.owner_id,job.id,job.receipt.execution_id,'queue_deadline')
                response=fixture.client.get('/api/jobs/'+job.id)
                self.assertEqual(response.status_code,200)
                body=response.json();self.assertEqual(body['status'],state)
                self.assertEqual(body['mode'],'hosted_private_preview')
                self.assertIs(body['live_release'],False);self.assertIs(body['private_preview'],True)
                self.assertNotIn('download_url',body)
        finally:fixture.close()
