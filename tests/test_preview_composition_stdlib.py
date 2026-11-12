"""Actual decoded-frame watermark tests. Synthetic media only, no provider calls."""
import copy,hashlib,json,subprocess,tempfile,unittest
from pathlib import Path
from PIL import Image
from byron_video import preview_composition as p

class WatermarkChecks(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def video(self,watermark=True,wrong=False,missing_last=False):
        template,_=p.watermark_template()
        if wrong:
            from PIL import ImageDraw
            ImageDraw.Draw(template).rectangle((120,24,200,155),fill='black')
        png=self.root/'mark.png';template.save(png)
        output=self.root/'film.mp4'
        command=['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','color=c=red:s=1080x1920:r=24:d=0.25',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=24000:duration=0.25']
        if watermark:
            command+=['-i',str(png),'-filter_complex',"[0:v][2:v]overlay=0:0"+(":enable='lt(t,0.20)'" if missing_last else '')+'[v]', '-map','[v]','-map','1:a:0']
        command+=['-c:v','libx264','-preset','ultrafast','-crf','18','-threads','2','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(output)]
        subprocess.run(command,check=True,capture_output=True);return output
    def test_exact_burned_label_verified_on_every_native_frame(self):
        output=self.video();proof=p.verify_watermark(output,expected_frames=6)
        self.assertEqual(proof['frames_verified'],6);self.assertTrue(proof['all_frames_verified'])
        self.assertEqual(proof['artifact_sha256'],hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(proof['text'],'DRAFT PRIVATE PREVIEW')
    def test_metadata_or_unmarked_film_cannot_claim_draft(self):
        with self.assertRaises(ValueError):p.verify_watermark(self.video(watermark=False))
    def test_missing_or_changed_watermark_glyph_fails(self):
        with self.assertRaises(ValueError):p.verify_watermark(self.video(wrong=True))
    def test_last_frame_missing_watermark_is_not_hidden_by_sampling(self):
        with self.assertRaises(ValueError):p.verify_watermark(self.video(missing_last=True))
    def test_wrong_expected_frame_count_fails(self):
        with self.assertRaises(ValueError):p.verify_watermark(self.video(),expected_frames=5)
    def test_separate_preview_release_does_not_replace_historical_compositor(self):
        release=json.loads(p.RELEASE.read_text());self.assertFalse(release['human_style_accepted']);self.assertFalse(release['live_release'])
        self.assertEqual(p.release_hash(),p.fingerprint(release))
        historical=json.loads((p.ROOT/'style-guide/portrait-candidate/release.json').read_text())
        self.assertEqual(release['neutral_compositor_sha256'],historical['compositor_sha256'])

    def test_real_native_preview_composition_keeps_captions_and_burns_every_frame(self):
        # Semantic QC is separately unit-tested against native archives. Here only
        # that boundary is doubled; actual audio/video/render/watermark are native.
        import math,wave,array
        from unittest.mock import patch
        from test_portrait_candidate_stdlib import PortraitCandidateChecks
        from byron_video.elevenlabs_adapter import fingerprint
        fixture=PortraitCandidateChecks();fixture.setUp();self.addCleanup(fixture.tearDown)
        staged,frozen,report,_,old_receipts=fixture.fixture();root=Path(staged['root'])
        rate=24000;audio=root/'narration.wav'
        samples=array.array('h',(round(4000*math.sin(2*math.pi*440*n/rate)) for n in range(round(5.9*rate))))
        with wave.open(str(audio),'wb') as stream:stream.setparams((1,2,rate,len(samples),'NONE','not compressed'));stream.writeframes(samples.tobytes())
        report['audio_sha256']=hashlib.sha256(audio.read_bytes()).hexdigest()
        version=subprocess.check_output(['ffmpeg','-version']).decode().splitlines()[0].split()[2];report['ffmpeg_version']=version
        report_sha=fingerprint(report);receipts=[]
        for index,(old,colour) in enumerate(zip(old_receipts,['red','blue','green','yellow']),1):
            path=root/f'clip-{index:02}.mp4'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',f'color=c={colour}:s=270x480:r=24:d=2',
                '-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True)
            receipt={k:v for k,v in old.items() if k!='no_invented_action_reviewed'}
            receipt.update(schema='byron-private-preview-clip-v1',human_approved=False,preview_qc_sha256='a'*64,sha256=hashlib.sha256(path.read_bytes()).hexdigest());receipts.append(receipt)
        composition=json.loads(Path(staged['files']['composition.json']).read_text());composition['status']='PRIVATE_DRAFT_PREVIEW';composition['narration_report_sha256']=report_sha
        Path(staged['files']['composition.json']).write_text(json.dumps(composition))
        output=root/'preview.mp4'
        with patch('byron_video.private_preview.validate_preview_context',return_value=[x['model'] for x in receipts]) as checked:
            result=p.compose_preview(staged,frozen,output,report=report,expected_report_sha256=report_sha,clip_receipts=receipts,
                context={'explicit_synthetic_qc_boundary':True},expected_context_sha256='b'*64,expected_authorization_sha256='c'*64,
                expected_preview_release_sha256=p.release_hash(),expected_ffmpeg_version=version,preview_evidence_verifier=lambda value:None,preview_source_verifier=lambda value:True)
        self.assertEqual(checked.call_count,1);self.assertEqual(result['status'],'PRIVATE_DRAFT_PREVIEW');self.assertFalse(result['human_approved'])
        self.assertEqual(result['watermark_proof']['frames_verified'],142)
        self.assertEqual(p.verify_watermark(output,expected_frames=142)['artifact_sha256'],result['sha256'])
        self.assertIn('One two three four.',output.with_suffix('.srt').read_text())
        self.assertFalse(any(root.glob('private-preview-*/neutral.mp4')))
