"""Separate release-candidate tests; generated tones, not actual provider speech."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from byron_video.elevenlabs_adapter import fingerprint,plan_requests
from byron_video.elevenlabs_release import build_narration_candidate
from byron_video.provider_execution import FileResponseStore,SpendApproval,WireResponse,execute_narration_once
from byron_video.worker_journal import Journal
from test_provider_execution_stdlib import profile


class NarrationReleaseChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();root=Path(cls.temp.name)
        cls.version=subprocess.check_output(['ffmpeg','-version']).decode().splitlines()[0].split()[2]
        target=root/'tone.mp3'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=44100:duration=4',
            '-c:a','libmp3lame','-b:a','128k',str(target)],check=True,capture_output=True)
        cls.audio=target.read_bytes()
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def setUp(self):
        self.temp_job=tempfile.TemporaryDirectory();self.root=Path(self.temp_job.name)
        self.journal=Journal(self.root/'journal.sqlite');self.archive=FileResponseStore(self.root/'responses')
        self.text='Synthetic words, unchanged.';self.profile=profile()
        requests=plan_requests(self.text,self.profile)
        self.approval=SpendApproval('synthetic_approval',fingerprint(requests),(2,),5,2)
        self.calls=[]
    def tearDown(self):self.journal.close();self.temp_job.cleanup()
    def prepare(self,end=3):
        def transport(request):
            self.calls.append(request)
            text=request['body']['text'];n=len(text)
            payload=json.dumps({'audio_base64':base64.b64encode(self.audio).decode(),'alignment':{
                'characters':list(text),'character_start_times_seconds':[i*end/n for i in range(n)],
                'character_end_times_seconds':[(i+1)*end/n for i in range(n)]}}).encode()
            return WireResponse(f'synthetic_request_{len(self.calls)}',payload)  # Unique per request, as ElevenLabs gives.
        execute_narration_once(owner_id='fixture_owner',job_id='fixture_job_1',text=self.text,profile=self.profile,
            approval=self.approval,journal=self.journal,transport=transport,response_store=self.archive)
    def build(self,**changes):
        args=dict(owner_id='fixture_owner',job_id='fixture_job_1',text=self.text,profile=self.profile,
            approval=self.approval,journal=self.journal,response_store=self.archive,
            expected_profile_sha256=fingerprint(self.profile),expected_text_sha256=hashlib.sha256(self.text.encode()).hexdigest(),
            job_root=self.root/'candidate',expected_ffmpeg_version=self.version,
            audio_settings={'integrated_lufs':-16,'true_peak_dbfs':-1,'lra':11},source_kind='synthetic_test')
        args.update(changes);return build_narration_candidate(**args)

    def test_actual_tone_decode_candidate_is_separate_and_draft(self):
        self.prepare();report=self.build()
        self.assertEqual(report['provider'],'elevenlabs');self.assertEqual(report['status'],'DRAFT_RELEASE_CANDIDATE')
        self.assertTrue(report['synthetic']);self.assertTrue(report['media_verified']);self.assertTrue(report['provider_receipts_verified'])
        self.assertFalse(report['live_provider_verified']);self.assertFalse(report['human_approved']);self.assertFalse(report['live_release'])
        self.assertEqual(report['exact_text'],self.text);self.assertEqual(report['duration_seconds'],4)
        self.assertEqual(report['frames'],96000);self.assertEqual(report['profile_sha256'],fingerprint(self.profile))
        self.assertEqual(report['synthesis_calls_in_this_media_stage'],0);self.assertEqual(len(self.calls),1)
        self.assertTrue((self.root/'candidate/narration.wav').exists())
        self.assertEqual((self.root/'candidate/provider-chunk-1.mp3').read_bytes(),self.audio)
        with self.assertRaises(ValueError):self.build()

    def test_final_word_rounding_overrun_is_clamped_but_real_overrun_fails(self):
        self.prepare(end=4.001)  # Seen live: the last word ended 1 ms after the decoded 4 s audio.
        report=self.build()
        self.assertEqual(report['words'][-1]['end'],4); self.assertEqual(report['exact_text'],self.text)
        self.journal.close(); self.temp_job.cleanup(); self.setUp()
        self.prepare(end=4.2)
        with self.assertRaisesRegex(ValueError,'exceed decoded audio'):self.build()

    def test_full_length_sentence_paced_story_assembles_every_sentence_with_pauses(self):
        # Live failure: a 75-sentence story hit an old two-piece limit here. Use the real assembly on 40 sentences.
        self.journal.close(); self.temp_job.cleanup(); self.setUp()
        self.text = '\n\n'.join(' '.join(f'Sentence {p}-{s} is here.' for s in range(4)) for p in range(10))
        self.profile = {**profile(), 'pacing': 'sentences'}
        requests = plan_requests(self.text, self.profile)
        self.assertEqual(len(requests), 40)
        self.approval = SpendApproval('synthetic_approval', fingerprint(requests), tuple([1] * 40), 100, 2)
        self.prepare(end=0.6)
        report = self.build()
        self.assertEqual(report['exact_text'], self.text); self.assertEqual(len(report['parts']), 40)
        self.assertEqual(len(self.calls), 40)
        pauses = [p['pause_frames'] for p in report['parts']]
        self.assertEqual(pauses[-1], 0); self.assertTrue(all(p > 0 for p in pauses[:-1]))
        self.assertGreater(pauses[3], pauses[0])  # Paragraph pause after the 4th sentence is longer than a sentence pause.
        starts = [w['start'] for w in report['words']]
        self.assertEqual(starts, sorted(starts)); self.assertLessEqual(report['words'][-1]['end'], report['duration_seconds'])

    def test_frozen_voice_and_text_fingerprints_required_before_media(self):
        self.prepare()
        for kw in [dict(expected_profile_sha256='a'*64),dict(expected_text_sha256='b'*64)]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):self.build(**kw)
        self.assertFalse((self.root/'candidate').exists());self.assertEqual(len(self.calls),1)

    def test_missing_or_wrong_owner_provenance_cannot_trigger_synthesis(self):
        with self.assertRaises(ValueError):self.build()
        self.assertEqual(self.calls,[])
        self.prepare()
        with self.assertRaises(ValueError):self.build(owner_id='other_fixture')
        self.assertEqual(len(self.calls),1)

    def test_archive_tamper_rejected_before_media(self):
        self.prepare();next((self.root/'responses').glob('*.json')).write_bytes(b'changed')
        with self.assertRaises(ValueError):self.build()
        self.assertFalse((self.root/'candidate').exists());self.assertEqual(len(self.calls),1)

    def test_historical_live_media_gate_is_unchanged(self):
        from byron_video.narration_media import assemble_narration
        with self.assertRaisesRegex(ValueError,'not activated'):
            assemble_narration([],[],self.root/'old',expected_ffmpeg_version=self.version,profile=self.profile,
                audio_settings={'integrated_lufs':-16,'true_peak_dbfs':-1,'lra':11},synthetic_fixture=False)


if __name__=='__main__':unittest.main(verbosity=2)
