"""Native exact-film comparison tests with synthetic pixels/audio, no providers."""
import subprocess,tempfile,unittest
from pathlib import Path
from byron_video import exact_film_composition as e
from byron_video.preview_composition import watermark_template

class ExactFilmComparisonChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.final=self.root/'final.mp4';self.preview=self.root/'preview.mp4'
        self.make(self.final,'red',440)
        mark=self.root/'mark.png';watermark_template()[0].save(mark)
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',str(self.final),'-i',str(mark),
            '-filter_complex','[0:v][1:v]overlay=0:0[v]','-map','[v]','-map','0:a:0','-c:v','libx264',
            '-preset','fast','-crf','18','-threads','2','-pix_fmt','yuv420p','-c:a','copy',str(self.preview)],check=True,capture_output=True)
    def tearDown(self):self.temp.cleanup()
    def make(self,path,color,tone):
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',f'color=c={color}:s=1080x1920:r=24:d=0.25',
            '-f','lavfi','-i',f'sine=frequency={tone}:sample_rate=24000:duration=0.25','-c:v','libx264','-preset','fast',
            '-crf','18','-threads','2','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(path)],check=True,capture_output=True)
    def test_all_frames_same_retained_content_and_audio_without_label(self):
        proof=e.verify_equivalence(self.preview,self.final,expected_frames=6)
        self.assertEqual(proof['frames_verified'],6);self.assertTrue(proof['decoded_audio_equal'])
        self.assertNotEqual(proof['preview_sha256'],proof['final_sha256'])
    def test_changed_visual_content_fails(self):
        self.make(self.final,'blue',440)
        with self.assertRaises(ValueError):e.verify_equivalence(self.preview,self.final,expected_frames=6)
    def test_changed_audio_fails(self):
        self.make(self.final,'red',880)
        with self.assertRaises(ValueError):e.verify_equivalence(self.preview,self.final,expected_frames=6)
    def test_omitted_or_added_frames_fail(self):
        with self.assertRaises(ValueError):e.verify_equivalence(self.preview,self.final,expected_frames=5)

if __name__=='__main__':unittest.main()
