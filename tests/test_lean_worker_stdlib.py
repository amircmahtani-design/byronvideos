"""Lean worker step resume and no-double-spend with synthetic providers; no network or ffmpeg."""
import base64
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from byron_video.elevenlabs_adapter import fingerprint
from byron_video.lean_film import LeanJobStore, LeanLedger
from byron_video.lean_worker import LeanPipeline, explain, motion_anchor
from byron_video.provider_execution import FileResponseStore, WireResponse, execute_narration_once
from test_cloud_journal_stdlib import Client, PROJECT
from test_hosted_uploads_stdlib import image_bytes
from test_lean_film_stdlib import KEY, NS, OWNER, handle, inputs_builder, lean_config


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Objects:
    def __init__(self): self.data = {}
    def put(self, owner, job_id, role, data, mime):
        ref = dict(object=f'{owner}/{job_id}/{role}-{sha(data)[:16]}', generation='1', sha256=sha(data), bytes=len(data), content_type=mime)
        self.data[ref['object']] = data
        return ref
    def put_file(self, owner, job_id, role, path, mime): return self.put(owner, job_id, role, Path(path).read_bytes(), mime)
    def read(self, owner, job_id, ref):
        data = self.data[ref['object']]; assert sha(data) == ref['sha256'] and ref['object'].startswith(f'{owner}/{job_id}/')
        return data
    def open(self, owner, job_id, ref): return io.BytesIO(self.read(owner, job_id, ref))
    def save(self, owner, job_id, ref, path): Path(path).write_bytes(self.read(owner, job_id, ref)); return Path(path)


class Uploads:
    def read_original(self, principal, record):
        assert principal.subject == OWNER
        return io.BytesIO(image_bytes(int(record.id[-4:])))


class ElevenLabs:
    def __init__(self, fail=False): self.calls, self.fail = 0, fail
    def __call__(self, request):
        self.calls += 1
        if self.fail: raise RuntimeError('synthetic lost response')
        text = request['body']['text']
        payload = {'audio_base64': base64.b64encode(b'ID3 synthetic mp3').decode(), 'alignment': {'characters': list(text),
                   'character_start_times_seconds': [i * .05 for i in range(len(text))],
                   'character_end_times_seconds': [(i + 1) * .05 for i in range(len(text))]}}
        return WireResponse(f'el_request_{self.calls:04}', json.dumps(payload).encode())


class Fal:
    def __init__(self): self.submitted, self.polls = [], 0
    def submit(self, request): self.submitted.append(request['image_id']); return handle(len(self.submitted))
    def status(self, value):
        self.polls += 1
        return 'IN_PROGRESS' if self.polls <= 2 else 'COMPLETED'
    def result(self, value):
        data = b'clip-' + value['request_id'].encode()
        return {'url': 'https://v3.fal.media/files/clip.mp4', 'bytes': len(data), 'request_id': value['request_id'], 'media_verified': False}
    def download(self, result):
        data = b'clip-' + result['request_id'].encode()
        return {'bytes': data, 'sha256': sha(data), 'media_verified': False}


class LeanWorkerChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.client, self.config = Client(), lean_config()
        self.store = LeanJobStore(self.client, project_id=PROJECT, namespace=NS, transaction_runner=self.client.run)
        self.job, _ = self.store.reserve(OWNER, KEY, 'b' * 64, inputs_builder(self.config))
        self.objects, self.responses = Objects(), FileResponseStore(self.root / 'responses')
        self.calls, self.compose_fails = [], True
    def tearDown(self): self.temp.cleanup()

    def narrate(self, **kw):
        self.calls.append('narrate')
        result = execute_narration_once(owner_id=kw['owner_id'], job_id=kw['job_id'], text=kw['text'], profile=kw['profile'],
            approval=kw['approval'], journal=kw['journal'], transport=kw['transport'], response_store=kw['response_store'])
        words = [w for r in result['responses'] for w in r['words']]
        folder = Path(kw['job_root']); folder.mkdir(parents=True, exist_ok=True)
        audio = b'RIFF synthetic narration'; (folder / 'narration.wav').write_bytes(audio)
        return {'exact_text': kw['text'], 'words': words, 'duration_seconds': words[-1]['end'] + .5, 'media_verified': True,
                'profile_sha256': fingerprint(kw['profile']), 'audio_sha256': sha(audio)}

    def compose(self, **kw):
        self.calls.append('compose')
        if self.compose_fails: raise RuntimeError('synthetic encoder failure')
        self.assertEqual([s['image_id'] for s in kw['scenes']], self.job.inputs['input_order'])
        for scene in kw['scenes']:
            self.assertTrue(Path(scene['clip_path']).read_bytes().startswith(b'clip-') and Path(scene['original_path']).is_file())
        output = Path(kw['output']); output.parent.mkdir(parents=True, exist_ok=True); output.write_bytes(b'synthetic film')
        return {'duration_seconds': kw['timing']['duration_seconds'], 'joins': [], 'flagged_joins': 0}

    def pipeline(self, eleven, fal):
        return LeanPipeline(owner_id=OWNER, jobs=self.store, uploads=Uploads(), objects=self.objects,
            ledger_factory=lambda job_id: LeanLedger(self.client, project_id=PROJECT, namespace=NS, owner_id=OWNER, job_id=job_id,
                                                     budget=self.config['budget'], transaction_runner=self.client.run),
            response_factory=lambda job_id: self.responses, narration_transport=eleven, motion_transport=fal,
            ffmpeg_version='synthetic-version', workspace=self.root / 'work', narrate=self.narrate, compose=self.compose,
            sleep=lambda seconds: None)

    def test_rerun_skips_verified_steps_and_never_pays_twice(self):
        eleven, fal = ElevenLabs(), Fal()
        failed = self.pipeline(eleven, fal).run(self.job.id)
        self.assertEqual(failed.status, 'failed')
        self.assertTrue(failed.message.startswith('Stopped while putting the film together: something went wrong inside the studio.'))
        self.assertIn('(Code: RuntimeError)', failed.message)
        self.assertEqual(eleven.calls, 1); self.assertEqual(fal.submitted, self.job.inputs['input_order'])
        steps = self.store.get(OWNER, self.job.id).steps
        self.assertTrue({'narration', 'clip_1', 'clip_4', 'anchor_1'} <= set(steps))
        polls = fal.polls; self.compose_fails = False
        done = self.pipeline(eleven, fal).run(self.job.id)
        self.assertEqual(done.status, 'complete'); self.assertEqual(self.calls, ['narrate', 'compose', 'compose'])
        self.assertEqual((eleven.calls, len(fal.submitted), fal.polls), (1, 4, polls))  # Saved outputs reused.
        public = done.public()
        self.assertEqual(public['download_url'], f'/api/jobs/{self.job.id}/video')
        self.assertEqual(public['actual_cost_cents'], public['expected_cost_cents'])
        self.assertEqual(self.objects.read(OWNER, self.job.id, done.result['video']), b'synthetic film')
        self.assertEqual(self.pipeline(eleven, fal).run(self.job.id).status, 'complete'); self.assertEqual(self.calls.count('compose'), 2)

    def test_uncertain_paid_step_stops_and_is_not_retried(self):
        eleven = ElevenLabs(fail=True); fal = Fal()
        for _ in range(2):
            job = self.pipeline(eleven, fal).run(self.job.id)
            self.assertEqual(job.status, 'failed')
            self.assertTrue(job.message.startswith('Stopped at the narration (ElevenLabs):'), job.message)
        self.assertEqual(eleven.calls, 1); self.assertEqual(fal.submitted, [])
        self.assertGreater(self.store.get(OWNER, self.job.id).public()['uncertain_cost_cents'], 0)

    def test_failure_message_names_step_service_and_cause(self):
        from byron_video.provider_execution import ProviderOutcomeUnknown, _outcome, _Refused
        cases = [(_Refused(401, b'{"detail":{"status":"quota_exceeded","message":"x"}}'), 'elevenlabs', 'out of credit', '401 quota_exceeded'),
                 (_Refused(403, b'{"detail":"User is locked. Reason: Exhausted balance."}'), 'fal', 'out of credit', '403'),
                 (_Refused(401, b'{"detail":{"status":"missing_permissions"}}'), 'elevenlabs', 'did not accept the saved key', 'missing_permissions'),
                 (_Refused(404, b'{"detail":{"status":"voice_not_found"}}'), 'elevenlabs', 'narrator voice', 'voice_not_found'),
                 (_Refused(429, b'{}'), 'fal', 'busy', '429'), (_Refused(503, b''), 'fal', 'on their side', '503'),
                 (TimeoutError(), 'elevenlabs', 'too long', 'timeout')]
        for error, provider, words, code in cases:
            with self.subTest(code=code):
                outcome = _outcome(error, provider, 'x')
                self.assertIsInstance(outcome, ProviderOutcomeUnknown)
                message = explain('narration' if provider == 'elevenlabs' else 'animation', outcome)
                self.assertIn(words, message); self.assertIn(code, message); self.assertLessEqual(len(message), 300)
                self.assertIn('ElevenLabs' if provider == 'elevenlabs' else '(fal)', message)

    def test_small_original_is_the_exact_anchor(self):
        data = image_bytes(1)
        self.assertEqual(motion_anchor(data, 'image/png'), (data, 'image/png'))


if __name__ == '__main__': unittest.main()
