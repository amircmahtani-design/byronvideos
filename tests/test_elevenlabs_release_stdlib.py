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
    def prepare(self):
        def transport(request):
            self.calls.append(request)
            text=request['body']['text'];n=len(text)
            payload=json.dumps({'audio_base64':base64.b64encode(self.audio).decode(),'alignment':{
                'characters':list(text),'character_start_times_seconds':[i*3/n for i in range(n)],
                'character_end_times_seconds':[(i+1)*3/n for i in range(n)]}}).encode()
            return WireResponse('synthetic_request_1',payload)
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
