"""Offline lean ledger, job store and Cloud Run start contract; no SDK, network or provider."""
import hashlib
import unittest

from byron_video.deployment_plan import validate_config
from byron_video.elevenlabs_adapter import plan_requests
from byron_video.hosted_control import IdempotencyConflict
from byron_video.lean_film import (LeanBudget, LeanBudgetExceeded, LeanJobStore, LeanLedger, LeanStarter, allocations,
    estimate_cents, freeze_inputs, lean_settings, motion_approval, motion_cents, motion_operation, narration_profile, owner_for)
from byron_video.provider_execution import (FAL_IMAGE_ENDPOINT, FAL_QUEUE_ORIGIN, ProviderOutcomeUnknown,
    _narration_operations, plan_anchored_motion, submit_motion_once)
from byron_video.lean_film import narration_approval
from byron_video.worker_binding import DispatchPending, HttpxCloudRunTransport
from test_cloud_journal_stdlib import Client, PROJECT
from test_deployment_plan_stdlib import config as base_config
from test_hosted_uploads_stdlib import image_bytes

OWNER = 'google_' + 'a' * 64
OTHER = 'google_' + 'b' * 64
KEY = 'lean_request_000000001'
NS = 'lean_fixture'
JOB = 'c' * 32
TEXT = 'The first painting shows a quiet harbour.\n\nThe second shows the hills at dusk.'


def lean_config(**budget):
    c = base_config(); c['firestore']['database'] = 'synthetic-setup'
    c['budget'] = dict(scope='lean_trial_scope', approved_total_cap_cents=1000, prior_spend_cents=0,
                       job_cap_cents=1000, operation_cap_cents=500)
    c['budget'].update(budget)
    c['lean_generation'] = {'schema_version': 1}
    return c


def images(count=4):
    return [dict(id=f'painting_{i:04}', owner_id=OWNER, sha256=hashlib.sha256(image_bytes(i)).hexdigest(), byte_count=len(image_bytes(i)),
                 width=64, height=64, content_type='image/png', storage_id=f'painting_{i:04}', storage_version='1',
                 original_bytes_verified=True, position=i) for i in range(1, count + 1)]


def inputs_builder(config, text=TEXT, count=4, request_sha='b' * 64):
    profile = narration_profile(config, 'SYNTHETIC_TEST_VOICE')
    requests = plan_requests(text, profile)
    return lambda job_id: freeze_inputs(owner_id=OWNER, job_id=job_id, script=text, narrated_text=text, images=images(count),
        voice={'id': 'SYNTHETIC_TEST_VOICE', 'name': 'Narrator'}, profile=profile, requests=requests,
        settings=lean_settings(config), budget=config['budget'], request_sha256=request_sha)


def handle(number):
    request_id = f'fal_request_{number:04}'
    base = FAL_QUEUE_ORIGIN + '/minimax/h3-max-turbo/requests/' + request_id
    return {'endpoint': FAL_IMAGE_ENDPOINT, 'request_id': request_id, 'response_url': base, 'status_url': base + '/status'}


class FakeMotion:
    def __init__(self, fail=False): self.submitted, self.fail = [], fail
    def submit(self, request):
        self.submitted.append(request['image_id'])
        if self.fail: raise RuntimeError('synthetic timeout after transmission')
        return handle(len(self.submitted))


class LeanFilmChecks(unittest.TestCase):
    def setUp(self):
        self.client = Client(); self.config = lean_config(); self.settings = lean_settings(self.config)
    def ledger(self, job=JOB, **budget):
        value = {**self.config['budget'], **budget}
        return LeanLedger(self.client, project_id=PROJECT, namespace=NS, owner_id=OWNER, job_id=job, budget=value,
                          transaction_runner=self.client.run)
    def store(self):
        return LeanJobStore(self.client, project_id=PROJECT, namespace=NS, transaction_runner=self.client.run)

    def test_settings_defaults_are_signed_config_and_strict(self):
        self.assertEqual(self.settings['motion_cents_per_second'], 4); self.assertEqual(motion_cents(self.settings), 40)
        self.assertEqual(validate_config(self.config)['lean_generation'], {'schema_version': 1})
        self.assertTrue(owner_for(self.config).startswith('google_'))
        for bad in [{'schema_version': 1, 'motion_cents_per_second': 0}, {'schema_version': 1, 'gemini': True},
                    {'motion_cents_per_second': 4}, {'schema_version': 1, 'motion_prompt': ''}]:
            value = lean_config(); value['lean_generation'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError): validate_config(value)
        value = lean_config(); del value['lean_generation']
        with self.assertRaises(ValueError): lean_settings(value)
        validate_config(value)  # Default configuration is unchanged.

    def test_quote_matches_paid_call_operation_identities(self):
        profile = narration_profile(self.config, 'SYNTHETIC_TEST_VOICE')
        self.assertEqual(profile['model_id'], 'eleven_multilingual_v2')
        requests = plan_requests(TEXT, profile)
        self.assertEqual(''.join(r['body']['text'] for r in requests), TEXT)  # Exact, never shortened.
        funded = allocations(OWNER, JOB, requests, ['painting_0001'], self.settings, self.config['budget'])
        approval = narration_approval(JOB, requests, self.settings, self.config['budget'])
        for op, _b, _r, cents in _narration_operations(OWNER, JOB, requests, approval):
            self.assertEqual(funded[op], cents)
        armed = []
        class Recorder:
            def get(self, op): return None
            def arm(self, op, binding, cents, cap): armed.append((op, cents))
            def record(self, *args): pass
        data = image_bytes(1)
        request = plan_anchored_motion(image_id='painting_0001', original_bytes=data, content_type='image/png',
            original_sha256=hashlib.sha256(data).hexdigest(), prompt=self.settings['motion_prompt'])
        self.assertEqual(request['body']['duration'], 10); self.assertEqual(request['body']['resolution'], '768P')
        self.assertEqual(request['body']['image_url'], request['body']['end_image_url'])
        submit_motion_once(owner_id=OWNER, job_id=JOB, request=request, approval=motion_approval(JOB, request, self.settings, self.config['budget']),
                           journal=Recorder(), transport=FakeMotion())
        self.assertEqual(armed, [(motion_operation(OWNER, JOB, 'painting_0001'), 40)])
        self.assertEqual(funded[armed[0][0]], 40)
        self.assertEqual(estimate_cents(requests, 1, self.settings, self.config['budget']), sum(funded.values()))

    def test_estimate_refuses_over_film_or_step_cap_before_anything(self):
        requests = plan_requests(TEXT, narration_profile(self.config, 'SYNTHETIC_TEST_VOICE'))
        with self.assertRaisesRegex(LeanBudgetExceeded, r'\$'):
            estimate_cents(requests, 5, self.settings, {**self.config['budget'], 'job_cap_cents': 100})
        with self.assertRaises(LeanBudgetExceeded):
            estimate_cents(requests, 5, self.settings, {**self.config['budget'], 'operation_cap_cents': 39})
        self.assertEqual(self.client.data, {})

    def test_hard_total_cap_is_shared_across_films_and_funding_is_idempotent(self):
        a, b = self.ledger('a' * 32), self.ledger('b' * 32)
        self.assertTrue(a.fund('lean_' + 'a' * 32, 'f' * 64, {'motion_' + '1' * 64: 300, 'motion_' + '2' * 64: 300}))
        self.assertEqual(a.remaining_cents(), 400)
        with self.assertRaisesRegex(LeanBudgetExceeded, r'\$4\.00'):
            b.fund('lean_' + 'b' * 32, 'e' * 64, {'motion_' + '3' * 64: 300, 'motion_' + '4' * 64: 200})
        self.assertFalse(a.fund('lean_' + 'a' * 32, 'f' * 64, {'motion_' + '1' * 64: 300, 'motion_' + '2' * 64: 300}))
        budget = LeanBudget(self.client, project_id=PROJECT, namespace=NS, transaction_runner=self.client.run)
        self.assertEqual(budget.remaining_cents(self.config['budget']), 400)
        self.assertTrue(b.fund('lean_' + 'b' * 32, 'e' * 64, {'motion_' + '3' * 64: 300, 'motion_' + '4' * 64: 100}))
        self.assertEqual(budget.remaining_cents(self.config['budget']), 0)

    def test_each_paid_call_is_claimed_once_and_uncertain_is_never_resent(self):
        data = image_bytes(1); settings, budget = self.settings, self.config['budget']
        requests = []
        for image_id in ('painting_0001', 'painting_0002'):
            requests.append(plan_anchored_motion(image_id=image_id, original_bytes=data, content_type='image/png',
                original_sha256=hashlib.sha256(data).hexdigest(), prompt=settings['motion_prompt']))
        funded = {motion_operation(OWNER, JOB, r['image_id']): 40 for r in requests}
        ledger = self.ledger(); ledger.fund('lean_' + JOB, 'd' * 64, funded)
        journal = ledger.operations('lean_' + JOB, 'd' * 64, lambda: True)
        ok = FakeMotion()
        first = submit_motion_once(owner_id=OWNER, job_id=JOB, request=requests[0], approval=motion_approval(JOB, requests[0], settings, budget), journal=journal, transport=ok)
        again = submit_motion_once(owner_id=OWNER, job_id=JOB, request=requests[0], approval=motion_approval(JOB, requests[0], settings, budget), journal=journal, transport=ok)
        self.assertEqual(first, again); self.assertEqual(ok.submitted, ['painting_0001'])
        lost = FakeMotion(fail=True)
        for _ in range(2):
            with self.assertRaises(ProviderOutcomeUnknown):
                submit_motion_once(owner_id=OWNER, job_id=JOB, request=requests[1], approval=motion_approval(JOB, requests[1], settings, budget), journal=journal, transport=lost)
        self.assertEqual(lost.submitted, ['painting_0002'])
        self.assertEqual(ledger.summary('lean_' + JOB, 'd' * 64, funded), {'expected_cents': 80, 'spent_cents': 40, 'uncertain_cents': 40})
        self.assertEqual(ledger.remaining_cents(), 920)  # Reserved once; never refunded or double counted.

    def test_idempotent_submit_owner_scope_and_write_once_steps(self):
        store = self.store(); build = inputs_builder(self.config)
        job, created = store.reserve(OWNER, KEY, 'b' * 64, build)
        self.assertTrue(created); self.assertEqual(job.status, 'queued')
        same, created = store.reserve(OWNER, KEY, 'b' * 64, build)
        self.assertFalse(created); self.assertEqual(same.id, job.id)
        with self.assertRaises(IdempotencyConflict): store.reserve(OWNER, KEY, 'c' * 64, build)
        self.assertIsNone(store.get(OTHER, job.id)); self.assertIsNone(store.find(OTHER, KEY))
        self.assertEqual(store.find(OWNER, KEY).id, job.id)
        self.assertEqual(job.inputs['input_order'], [f'painting_{i:04}' for i in range(1, 5)])
        self.assertTrue(store.claim_start(OWNER, job.id)); self.assertFalse(store.claim_start(OWNER, job.id))
        store.record_start(OWNER, job.id, 'projects/synthetic-project/locations/us-central1/operations/operation_0001')
        public = store.get(OWNER, job.id).public()
        self.assertEqual(public['expected_cost_cents'], job.inputs['estimate_cents'])
        self.assertNotIn('script', str(public)); self.assertNotIn('painting_0001', str(public))
        store.begin(OWNER, job.id)
        store.record_step(OWNER, job.id, 'narration', {'x': 1}); store.record_step(OWNER, job.id, 'narration', {'x': 1})
        with self.assertRaises(ValueError): store.record_step(OWNER, job.id, 'narration', {'x': 2})
        ref = dict(object='o', generation='1', sha256='a' * 64, bytes=1, content_type='video/mp4')
        done = store.complete(OWNER, job.id, {'video': ref, 'duration_seconds': 12.5})
        self.assertEqual(done.public()['download_url'], f'/api/jobs/{job.id}/video')
        self.assertEqual(store.fail(OWNER, job.id, 'late failure').status, 'complete')

    def test_cloud_run_start_request_shape_and_uncertain_outcome(self):
        calls = []
        operation = 'projects/synthetic-project/locations/us-central1/operations/operation_0001'
        starter = LeanStarter(self.config['target'], lambda m, u, b: calls.append((m, u, b)) or {'name': operation})
        self.assertEqual(starter(JOB), operation)
        method, url, body = calls[0]
        self.assertEqual((method, url), ('POST', 'https://run.googleapis.com/v2/projects/synthetic-project/locations/us-central1/jobs/byron-renderer:run'))
        self.assertEqual(body, {'overrides': {'taskCount': 1, 'timeout': '1800s', 'containerOverrides': [
            {'name': 'renderer', 'env': [{'name': 'BYRON_LEAN_JOB_ID', 'value': JOB}]}]}})
        with self.assertRaises(ValueError): LeanStarter(self.config['target'], lambda *a: {'name': 'elsewhere'})(JOB)
        with self.assertRaises(ValueError): starter('not-a-job')
        def unreachable(**kwargs): raise OSError('synthetic network failure')
        transport = HttpxCloudRunTransport(lambda: 't' * 32, client_factory=unreachable)
        with self.assertRaises(DispatchPending): LeanStarter(self.config['target'], transport)(JOB)


if __name__ == '__main__': unittest.main()
