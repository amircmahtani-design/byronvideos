"""Lean worker step resume and no-double-spend with synthetic providers; no network or ffmpeg."""
import base64
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from byron_video.elevenlabs_adapter import fingerprint
from byron_video.hosted_control import require
from byron_video.lean_film import LeanJobStore, LeanLedger
from byron_video.lean_worker import LeanPipeline, explain, lean_timing, motion_anchor, story_title, with_bounds
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
        self.assertTrue(failed.message.startswith('Stopped while putting the film together: a studio check failed: “synthetic encoder failure”.'),
                        failed.message)
        self.assertIn('(Code: RuntimeError at test_lean_worker_stdlib.py:', failed.message)
        self.assertIn(' in compose)', failed.message)
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

    def test_full_length_story_runs_sentence_by_sentence_end_to_end(self):
        # Full length, as live: one narration request per sentence, every step of the film after it.
        config = lean_config(); config['narration_profile'] = {**config['narration_profile'], 'pacing': 'sentences'}
        story = '\n\n'.join(' '.join(f'Sentence {p}-{s} is here.' for s in range(4)) for p in range(30))  # 120 sentences.
        self.config = config
        self.store.fail(OWNER, self.job.id, 'Set aside: one film at a time.')  # setUp's film would otherwise hold the lock.
        self.job, _ = self.store.reserve(OWNER, 'lean_request_full_length_01', 'c' * 64, inputs_builder(config, text=story))
        eleven, fal = ElevenLabs(), Fal(); self.compose_fails = False
        pipeline = self.pipeline(eleven, fal); heard = []
        pipeline.checker = lambda **kw: (heard.append(kw['text']) or True, {'missing_words': 0})
        done = pipeline.run(self.job.id)
        self.assertEqual(done.status, 'complete', done.message)
        self.assertEqual(eleven.calls, 120); self.assertEqual(len(fal.submitted), 4); self.assertEqual(heard, [story])

    def test_skipped_narration_stops_before_any_painting_is_animated(self):
        eleven, fal = ElevenLabs(), Fal()
        pipeline = self.pipeline(eleven, fal)
        pipeline.checker = lambda **kw: (False, {'missing_words': 9, 'total_words': 20, 'runs': [{'near': 'she became unavailable', 'words': 9}]})
        job = pipeline.run(self.job.id)
        self.assertEqual(job.status, 'failed'); self.assertEqual(fal.submitted, [])  # No animation was paid for.
        self.assertIn('narration check', job.message); self.assertIn('she became unavailable', job.message)

    def test_placement_bounds_keep_crossfades_and_order(self):
        timing = {'duration_seconds': 40.0, 'crossfade_seconds': .7, 'scenes': [
            {'image_id': f'painting_{i}', 'start_seconds': 0, 'end_seconds': 0} for i in range(1, 5)]}
        placed = with_bounds(timing, [0.0, 6.0, 20.0, 31.0, 40.0])
        self.assertEqual([(s['start_seconds'], s['end_seconds']) for s in placed['scenes']],
                         [(0, 6.35), (5.65, 20.35), (19.65, 31.35), (30.65, 40.0)])
        self.assertEqual([s['image_id'] for s in placed['scenes']], [s['image_id'] for s in timing['scenes']])

    def test_own_check_failure_names_the_check_and_the_line_that_raised(self):
        try:
            require(False, 'Narration word timings required.')
        except ValueError as error:
            message = explain('assembly', error)
        self.assertTrue(message.startswith('Stopped while putting the film together: a studio check failed: “Narration word timings required.”'))
        self.assertRegex(message, r'\(Code: ValueError at test_lean_worker_stdlib\.py:\d+ in test_own_check_failure_names_the_check_and_the_line_that_raised\)$')
        long = explain('assembly', ValueError('x' * 2000))
        self.assertLessEqual(len(long), 500); self.assertTrue(long.endswith(')')); self.assertIn('(Code: ValueError at ', long)

    def timing_job(self, text, words, duration):
        snapshot = {'images': [{'id': f'painting_{i}'} for i in range(1, 5)], 'narrated_text': text}
        report = {'exact_text': text, 'media_verified': True, 'duration_seconds': duration, 'words': words, 'audio_sha256': 'a' * 64}
        return SimpleNamespace(snapshot=snapshot), report

    def test_timing_accepts_silent_punctuation_but_names_a_word_without_time(self):
        text = 'One night — the lake was still … and he wrote. Then dawn came.'
        words = [{'text': m.group(), 'start': i * .5, 'end': i * .5 + (0 if m.group() in ('—', '…') else .4),
                  'char_start': m.start(), 'char_end': m.end()} for i, m in enumerate(re.finditer(r'\S+', text))]
        job, report = self.timing_job(text, words, 8.0)
        timing, _ = lean_timing(job, report)  # Zero-length "—" and "…" were what stopped the film on 30 Oct 2026.
        self.assertEqual(len(timing['scenes']), 4)
        broken = [dict(w) for w in words]; broken[4]['end'] = broken[4]['start']  # "lake" with no duration
        with self.assertRaisesRegex(ValueError, 'Narration word timing is invalid: “lake” at 2.0–2.0 s'):
            lean_timing(*self.timing_job(text, broken, 8.0))

    def test_title_line_goes_on_the_card_not_the_byline(self):
        self.assertEqual(story_title('# The Prisoner of Chillon\nby Lord Byron\n\nMy hair is grey, but not with years.'), 'The Prisoner of Chillon')
        self.assertEqual(story_title('Written by Lord Byron\n\nThe Prisoner of Chillon\n\nMy hair is grey, but not with years.'), 'The Prisoner of Chillon')
        self.assertIsNone(story_title('My hair is grey, but not with years. Nor grew it white in a single night.'))

    def test_small_original_is_the_exact_anchor(self):
        data = image_bytes(1)
        self.assertEqual(motion_anchor(data, 'image/png'), (data, 'image/png'))


if __name__ == '__main__': unittest.main()
