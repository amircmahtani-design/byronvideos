"""Actual native decode fidelity tests; synthetic pictures/audio, no services."""
import json,subprocess,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from byron_video.candidate_media_evidence import compare_decoded,RetainedMediaVerifier,validate_evidence_json
from byron_video.hosted_control import canonical
from candidate_media_fixture import synthetic_evidence

class FidelityChecks(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def film(self,name,colour='red',frequency=440,overlay=False):
        path=self.root/name
        cmd=['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',f'color=c={colour}:s=1080x1920:r=24:d=0.25',
             '-f','lavfi','-i',f'sine=frequency={frequency}:sample_rate=24000:duration=0.25']
        if overlay:cmd+=['-vf',"drawbox=x=500:y=1700:w=4:h=8:color=white:t=fill:enable='eq(n,5)'"]
        cmd+=['-c:v','libx264','-preset','ultrafast','-crf','18','-threads','2','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(path)]
        subprocess.run(cmd,check=True,capture_output=True);return path
    def test_reconstructed_native_media_exact_match(self):
        a=self.film('a.mp4');b=self.film('b.mp4');value=compare_decoded(a,b,frames=6)
        self.assertEqual(value['frames_verified'],6);self.assertEqual(value['video_bytes'],6*1080*1920*3)
        self.assertGreater(value['audio_bytes'],0)
    def test_last_frame_tiny_caption_glyph_mutation_rejected(self):
        with self.assertRaises(ValueError):compare_decoded(self.film('a.mp4'),self.film('b.mp4',overlay=True),frames=6)
    def test_changed_source_frame_or_order_rejected(self):
        with self.assertRaises(ValueError):compare_decoded(self.film('a.mp4'),self.film('b.mp4',colour='blue'),frames=6)
    def test_changed_narration_rejected(self):
        with self.assertRaises(ValueError):compare_decoded(self.film('a.mp4'),self.film('b.mp4',frequency=880),frames=6)
    def test_wrong_frame_bound_rejected(self):
        a=self.film('a.mp4')
        with self.assertRaises(ValueError):compare_decoded(a,a,frames=5)
    def test_arbitrary_truth_compositor_rejected(self):
        with self.assertRaises(ValueError):RetainedMediaVerifier(compositor=lambda *a:True,staged={},job=None)
    def test_default_checks_actual_audio_without_second_video_render(self):
        import array,math,wave
        from unittest.mock import patch
        from byron_video.preview_composition import PrivatePreviewCompositor
        from byron_video.portrait_composition import presentation
        from byron_video.elevenlabs_adapter import fingerprint
        style=presentation();wav=self.root/'native.wav'
        samples=array.array('h',(round(4000*math.sin(2*math.pi*440*n/24000)) for n in range(6000)))
        with wave.open(str(wav),'wb') as stream:
            stream.setparams((1,2,24000,6000,'NONE','not compressed'));stream.writeframes(samples.tobytes())
        output=self.root/'actual.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','color=c=red:s=1080x1920:r=24:d=0.25',
            '-i',str(wav),'-map','0:v:0','-map','1:a:0','-c:v','libx264','-threads','2','-preset','ultrafast',
            '-c:a',style['encoding']['audio_codec'],'-b:a',style['encoding']['audio_bitrate'],'-t','0.250000000',str(output)],check=True,capture_output=True)
        snapshot={'input_order':['one'],'narrated_text_sha256':'a'*64}
        job=SimpleNamespace(owner_id='owner',id='job',snapshot=snapshot,snapshot_sha256=fingerprint(snapshot))
        staged=dict(owner_id='owner',job_id='job',snapshot_sha256=job.snapshot_sha256,root=self.root)
        value=dict(report={'audio_sha256':__import__('hashlib').sha256(wav.read_bytes()).hexdigest()},report_sha256='a'*64,clip_receipts=[],preview_context={},preview_context_sha256='b'*64)
        compositor=PrivatePreviewCompositor(lambda ignored:value,expected_authorization_sha256='a'*64,
            expected_preview_release_sha256='b'*64,expected_ffmpeg_version='fixture',preview_evidence_verifier=lambda x:None,
            preview_source_verifier=lambda x:None)
        validated=(style,wav,[],[],[0.],.25)
        # Only native archive/input admission is doubled here. Audio/video decode,
        # hashes, audio-only encode and timeline checks are real local processes.
        with patch('byron_video.preview_composition.validate_preview',return_value=validated),patch.object(PrivatePreviewCompositor,'__call__',side_effect=AssertionError('default must not rerender')):
            verifier=RetainedMediaVerifier(compositor=compositor,staged=staged,job=job)
            evidence=verifier(output,job)
        self.assertFalse(evidence.independent_render_verified);self.assertIsNone(evidence.reference_sha256)
        self.assertFalse(evidence.spoken_words_independently_verified);self.assertFalse(evidence.human_semantics_verified)
        self.assertEqual(evidence.frames_verified,6)

    def test_candidate_intermediate_cannot_commit_as_historical_final(self):
        from dataclasses import asdict
        from byron_video.candidate_media_evidence import CandidateVerifiedMedia
        from byron_video.hosted_control import VerifiedArtifact
        from byron_video.cloud_journal import FirestoreArtifactRegistry
        from test_preview_registry_stdlib import PreviewRegistryChecks
        fixture=PreviewRegistryChecks();fixture.setUp();self.addCleanup(fixture.tearDown)
        fields={key:value for key,value in asdict(fixture.proof).items() if key in VerifiedArtifact.__dataclass_fields__}
        candidate=CandidateVerifiedMedia(**fields)
        self.assertIsNot(type(candidate),VerifiedArtifact)
        registry=FirestoreArtifactRegistry(fixture.db,**fixture.args)
        with self.assertRaises(ValueError):registry.commit_verified(fixture.job.owner_id,fixture.job,candidate,fixture.reference)

    def test_evidence_never_claims_spoken_word_or_human_truth(self):
        job=SimpleNamespace(snapshot_sha256='a'*64,snapshot={'input_order':['one'],'narrated_text_sha256':'b'*64})
        artifact=SimpleNamespace(sha256='c'*64,duration_seconds=1)
        encoded=synthetic_evidence(job,artifact.sha256,1);validate_evidence_json(encoded,artifact=artifact,job=job)
        for key in ('spoken_words_independently_verified','visible_caption_words_independently_verified','visual_source_order_independently_verified','human_semantics_verified','human_style_approved'):
            data=json.loads(encoded);data[key]=True
            with self.assertRaises(ValueError):validate_evidence_json(canonical(data),artifact=artifact,job=job)
        data=json.loads(encoded);data['output_sha256']='d'*64
        with self.assertRaises(ValueError):validate_evidence_json(canonical(data),artifact=artifact,job=job)

if __name__=='__main__':unittest.main()
