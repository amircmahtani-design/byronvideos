"""EXPLICIT SYNTHETIC report for schema/registry tests, never media proof."""
from dataclasses import asdict
from byron_video.candidate_media_evidence import CandidateMediaEvidence
from byron_video.hosted_control import canonical

def synthetic_evidence(job,sha,duration):
    return canonical(asdict(CandidateMediaEvidence(output_sha256=sha,snapshot_sha256=job.snapshot_sha256,
        source_order=tuple(job.snapshot['input_order']),narrated_text_sha256=job.snapshot['narrated_text_sha256'],
        caption_text_sha256=job.snapshot['narrated_text_sha256'],reference_sha256=None,retained_inputs_sha256='0'*64,audio_reference_sha256='0'*64,independent_render_verified=False,decoded_video_sha256='0'*64,
        decoded_audio_sha256='0'*64,timeline_sha256='0'*64,video_bytes=round(duration*24)*1080*1920*3,audio_bytes=1024,
        frames_verified=round(duration*24),compositor_sha256='0'*64,implementation_sha256='0'*64)))
