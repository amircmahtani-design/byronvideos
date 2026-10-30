"""Pure configuration validation; no command execution/cloud side effects."""
import copy
import json
from pathlib import Path
import unittest
from byron_video.deployment_plan import plan,validate_config
from test_worker_binding_stdlib import fixture
from test_hosted_uploads_stdlib import plan as processing_plan


def config():
    target=fixture()[0]
    return dict(schema_version=1,status='DRAFT_NOT_ACTIVATED',target=target,
        firestore=dict(database='(default)',namespace='synthetic_namespace'),
        identity=dict(origin='https://studio.example',oauth_audience='synthetic-client',creator_subjects=['100000000001'],admin_subjects=['100000000001'],subject_emails={'100000000001':'synthetic-admin@example.test'}),
        secret_resources={k:'projects/synthetic-project/secrets/synthetic_'+k for k in ('elevenlabs','fal','default_voice','csrf','google_oauth')},
        budget=dict(scope='synthetic_scope',approved_total_cap_cents=100,prior_spend_cents=20,job_cap_cents=50,operation_cap_cents=10),
        narration_profile=processing_plan()['narration_profile'],
        runtime=dict(base_image='registry.example/runtime/base@sha256:'+'a'*64,ffmpeg_version='synthetic-version',sdk_lock_sha256='b'*64,portrait_candidate_sha256='c'*64,cloud_run_task_template_sha256='d'*64,worker_service_account='synthetic-worker@synthetic-project.iam.gserviceaccount.com'))


class DeploymentPlanChecks(unittest.TestCase):
    def test_valid_config_returns_nonexecuting_read_only_plan(self):
        result=plan(config())
        self.assertEqual(result['status'],'PLAN_ONLY_NOT_EXECUTED')
        self.assertFalse(result['live_ready'])
        self.assertEqual(len(result['read_only_commands']),8)
        self.assertTrue(all('describe' in command for command in result['read_only_commands']))

    def test_empty_template_cannot_claim_ready(self):
        path=Path(__file__).resolve().parent.parent/'hosted/deployment.private.template.json'
        with self.assertRaises(ValueError):validate_config(json.loads(path.read_text()))

    def test_secrets_must_be_resource_references_not_literal_keys(self):
        value=config();value['secret_resources']['fal']='synthetic-literal-key'
        with self.assertRaises(ValueError):validate_config(value)

    def test_budget_preserves_prior_spending(self):
        value=config();value['budget']['prior_spend_cents']=80
        with self.assertRaises(ValueError):validate_config(value)

    def test_identity_rejects_email_and_insecure_origin(self):
        for key,value in [('origin','http://studio.example'),('creator_subjects',['person@example.test'])]:
            candidate=config();candidate['identity'][key]=value
            with self.assertRaises(ValueError):validate_config(candidate)

    def test_unreviewed_template_or_other_project_identity_rejected(self):
        for key,value in [('cloud_run_task_template_sha256',''),('worker_service_account','worker@other-project.iam.gserviceaccount.com')]:
            candidate=config();candidate['runtime'][key]=value
            with self.assertRaises(ValueError):validate_config(candidate)

    def test_live_switch_and_mutable_image_are_rejected(self):
        value=config();value['target']['live_release']=True
        with self.assertRaises(ValueError):validate_config(value)
        value=config();value['runtime']['base_image']='python:latest'
        with self.assertRaises(ValueError):validate_config(value)


if __name__=='__main__':unittest.main()
