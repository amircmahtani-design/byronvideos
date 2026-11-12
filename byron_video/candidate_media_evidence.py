"""Retained-input provenance/output integrity, NOT speech or human review.

Default checks revalidate immutable native archives, retained inputs, caption and
timeline construction; decode/hash actual video and compare actual audio to a
retained-narration audio-only encode. It does NOT independently prove visible
caption correctness, spoken-word correctness, or source semantics. Optional
reference_mode reconstructs a second film and compares every decoded pixel/sample
for calibration, never enabled implicitly. No provider calls/publication occur.
"""
from dataclasses import dataclass,asdict
import hashlib,json,math,subprocess,tempfile,threading
from pathlib import Path
from .hosted_control import require,canonical,VerifiedArtifact
from .elevenlabs_adapter import fingerprint

METHOD='retained-input-provenance-output-integrity-v1'

class CandidateVerifiedMedia(VerifiedArtifact):
    """Intermediate candidate only; historical exact-type registries reject it."""


@dataclass(frozen=True)
class CandidateMediaEvidence:
    output_sha256:str
    snapshot_sha256:str
    source_order:tuple
    narrated_text_sha256:str
    caption_text_sha256:str
    reference_sha256:str|None
    retained_inputs_sha256:str
    audio_reference_sha256:str
    independent_render_verified:bool
    decoded_video_sha256:str
    decoded_audio_sha256:str
    timeline_sha256:str
    video_bytes:int
    audio_bytes:int
    frames_verified:int
    compositor_sha256:str
    implementation_sha256:str
    method:str=METHOD
    spoken_words_independently_verified:bool=False
    visible_caption_words_independently_verified:bool=False
    visual_source_order_independently_verified:bool=False
    human_semantics_verified:bool=False
    human_style_approved:bool=False


def file_sha(path):
    path=Path(path)
    require(path.is_absolute() and path.is_file() and not any(p.is_symlink() for p in (path,*path.parents)), 'Private nonsymlink media required.')
    h=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(1024*1024):h.update(block)
    return h.hexdigest()


def decoded_digest(path,*,video,maximum,timeout=1800):
    """Bounded sequential entire-stream decode, no sampling/resizing/downmix."""
    from .portrait_composition import input_options
    args=['ffmpeg','-nostdin','-v','error','-xerror','-threads','1',*input_options(path,video=True),'-i',str(path)]
    args+=['-map','0:v:0','-an','-fps_mode','passthrough','-f','rawvideo','-pix_fmt','rgb24','-threads','1','pipe:1'] if video else ['-map','0:a:0','-vn','-f','s32le','-acodec','pcm_s32le','pipe:1']
    with tempfile.TemporaryFile() as errors:
        proc=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=errors)
        timer=threading.Timer(timeout,proc.kill);timer.daemon=True;timer.start();count=0;h=hashlib.sha256()
        try:
            while block:=proc.stdout.read(1024*1024):
                count+=len(block);require(count<=maximum,'Decoded candidate exceeds exact media bound.');h.update(block)
            require(proc.wait(timeout=10)==0 and count>0,'Full candidate media decode failed.')
        finally:
            timer.cancel()
            if proc.poll() is None:proc.kill();proc.wait()
            proc.stdout.close()
    return count,h.hexdigest()


def compare_decoded(reference,actual,*,frames,timeout=1800):
    require(type(frames) is int and 0<frames<=21600,'Bounded expected frame count required.')
    from .portrait_composition import run_bounded,input_options
    before=(file_sha(reference),file_sha(actual));video=[];audio=[];timelines=[]
    for path in (reference,actual):
        timeline=json.loads(run_bounded(['ffprobe','-v','error',*input_options(path,video=True),
            '-show_entries','stream=index,codec_type,sample_rate,channels,channel_layout,time_base,start_pts,duration_ts:packet=stream_index,pts,dts,duration',
            '-of','json',str(path)],timeout=120,stdout_limit=16_000_000).stdout)
        timelines.append(fingerprint(timeline))
        video.append(decoded_digest(path,video=True,maximum=frames*1080*1920*3,timeout=timeout))
        audio.append(decoded_digest(path,video=False,maximum=900*192000*8*4,timeout=timeout))
    require(video[0]==video[1] and video[0][0]==frames*1080*1920*3,
            'Actual output pixels/captions/order differ from deterministic retained-input reference.')
    require(timelines[0]==timelines[1],'Candidate timestamps/audio layout differ from retained-input reference.')
    require(audio[0]==audio[1],'Actual output audio differs from retained native narration render.')
    require(before==(file_sha(reference),file_sha(actual)),'Media changed during candidate fidelity verification.')
    return dict(reference_sha256=before[0],output_sha256=before[1],decoded_video_sha256=video[0][1],
                decoded_audio_sha256=audio[0][1],timeline_sha256=timelines[0],video_bytes=video[0][0],audio_bytes=audio[0][0],frames_verified=frames)


def validate_evidence_json(value,*,artifact,job):
    require(isinstance(value,str) and len(value)<=8192,'Bounded candidate media integrity evidence required.')
    record=json.loads(value)
    require(canonical(record)==value,'Candidate evidence must use exact canonical encoding.')
    evidence=CandidateMediaEvidence(**record)
    require(evidence.method==METHOD and evidence.output_sha256==artifact.sha256
        and evidence.snapshot_sha256==job.snapshot_sha256 and tuple(evidence.source_order)==tuple(job.snapshot['input_order'])
        and evidence.narrated_text_sha256==evidence.caption_text_sha256==job.snapshot['narrated_text_sha256']
        and evidence.spoken_words_independently_verified is False and evidence.human_semantics_verified is False
        and evidence.visible_caption_words_independently_verified is False and evidence.visual_source_order_independently_verified is False
        and evidence.human_style_approved is False,'Candidate integrity must not claim independent speech/human approval.')
    import re
    require(all(isinstance(getattr(evidence,k),str) and re.fullmatch('[a-f0-9]{64}',getattr(evidence,k)) for k in
        ('retained_inputs_sha256','audio_reference_sha256','decoded_video_sha256','decoded_audio_sha256','timeline_sha256','compositor_sha256','implementation_sha256')),
        'Complete candidate media digest lineage required.')
    require(type(evidence.independent_render_verified) is bool and ((evidence.reference_sha256 is None and evidence.independent_render_verified is False) or (isinstance(evidence.reference_sha256,str) and re.fullmatch('[a-f0-9]{64}',evidence.reference_sha256) and evidence.independent_render_verified is True)), 'Optional reference verification must be explicit, never fabricated.')
    require(type(evidence.frames_verified) is int and evidence.frames_verified==round(artifact.duration_seconds*24)
        and type(evidence.video_bytes) is int and evidence.video_bytes==evidence.frames_verified*1080*1920*3
        and type(evidence.audio_bytes) is int and 0<evidence.audio_bytes<=900*192000*8*4,'Candidate full-media counts differ.')
    return evidence


class RetainedMediaVerifier:
    """Concrete provenance/integrity verifier; optional full reference is OFF."""
    def __init__(self,*,compositor,staged,job,reference_mode=False):
        from .preview_composition import PrivatePreviewCompositor
        from .exact_film_composition import ExactFilmCompositor
        require(type(compositor) in (PrivatePreviewCompositor,ExactFilmCompositor),'Concrete retained-input compositor required.')
        require(staged['owner_id']==job.owner_id and staged['job_id']==job.id
            and staged['snapshot_sha256']==job.snapshot_sha256,'Candidate verifier belongs to another job.')
        require(type(reference_mode) is bool,'Explicit optional reference mode required.')
        self.reference_mode=reference_mode
        self.compositor,self.staged,self.job=compositor,staged,job
        self.evidence=None
    def __call__(self,path,job):
        require((job.id,job.owner_id,job.snapshot_sha256)==(self.job.id,self.job.owner_id,self.job.snapshot_sha256)
            and fingerprint(job.snapshot)==job.snapshot_sha256,'Candidate verification scope changed.')
        root=Path(self.staged['root']);before=file_sha(path)
        from .preview_composition import PrivatePreviewCompositor,validate_preview
        from .portrait_composition import input_options,run_bounded,checked_file
        if type(self.compositor) is PrivatePreviewCompositor:
            c=self.compositor;value=c.loader(job.snapshot_sha256)
            expected_audio_sha=value['report']['audio_sha256']
            validated=validate_preview(self.staged,job.snapshot,report=value['report'],expected_report_sha256=value['report_sha256'],
                clip_receipts=value['clip_receipts'],context=value['preview_context'],expected_context_sha256=value['preview_context_sha256'],
                expected_authorization_sha256=c.auth,expected_preview_release_sha256=c.release,expected_ffmpeg_version=c.version,
                preview_evidence_verifier=c.verifier,preview_source_verifier=c.source_verifier,now=c.now)
        else:
            validated=self.compositor.preflight(self.staged,job.snapshot)
            expected_audio_sha=self.compositor.lineage['inputs']['report']['audio_sha256']
        style,narration,clips,captions,starts,duration=validated
        frames=math.ceil(duration*24)
        require(file_sha(narration)==expected_audio_sha,'Retained narration differs from independently validated native report.')
        # Bind validated actual retained bytes/timeline/caption construction. This
        # is provenance, NOT an independent reading of visible caption pixels.
        inputs=dict(narration_sha256=file_sha(narration),clips=[{k:v for k,v in clip.items() if k!='path'} for clip in clips],
            caption_plan=[asdict(c) for c in captions],scene_starts=starts,duration_seconds=duration,presentation_sha256=fingerprint(style))
        with tempfile.TemporaryDirectory(prefix='candidate-check-',dir=root) as temporary:
            audio_reference=Path(temporary)/'native-narration.m4a'
            # Audio-only deterministic encoding is inexpensive; never synthesize
            # speech or render a second film in default production mode.
            run_bounded(['ffmpeg','-nostdin','-v','error','-n',*input_options(narration,video=False),'-i',str(narration),
                '-map','0:a:0','-c:a',style['encoding']['audio_codec'],'-b:a',style['encoding']['audio_bitrate'],
                '-t',f'{duration:.9f}',str(audio_reference)],timeout=180,stdout_limit=65536)
            actual_audio=decoded_digest(path,video=False,maximum=900*192000*8*4)
            reference_audio=decoded_digest(audio_reference,video=False,maximum=900*192000*8*4)
            require(actual_audio==reference_audio,'Output audio differs from retained native narration encoding.')
            actual_video=decoded_digest(path,video=True,maximum=frames*1080*1920*3)
            require(actual_video[0]==frames*1080*1920*3,'Actual output frame count differs from validated timeline.')
            timeline=json.loads(run_bounded(['ffprobe','-v','error',*input_options(path,video=True),
                '-show_entries','stream=index,codec_type,sample_rate,channels,channel_layout,time_base,start_pts,duration_ts:packet=stream_index,pts,dts,duration',
                '-of','json',str(path)],timeout=120,stdout_limit=16_000_000).stdout)
            from fractions import Fraction
            streams=[stream for stream in timeline['streams'] if stream['codec_type']=='video']
            require(len(streams)==1,'One actual video stream required.')
            video_stream=streams[0];timebase=Fraction(video_stream['time_base'])
            packets=[packet for packet in timeline['packets'] if packet['stream_index']==video_stream['index']]
            require(len(packets)==frames and sorted(Fraction(packet['pts'])*timebase for packet in packets)==[Fraction(n,24) for n in range(frames)]
                and all(Fraction(packet['duration'])*timebase==Fraction(1,24) for packet in packets),
                'Actual frame playback timeline differs from the fixed retained 24fps plan.')
            audio_timeline=json.loads(run_bounded(['ffprobe','-v','error',*input_options(audio_reference,video=True),
                '-show_entries','stream=index,codec_type,sample_rate,channels,channel_layout,time_base,start_pts,duration_ts:packet=stream_index,pts,dts,duration',
                '-of','json',str(audio_reference)],timeout=120,stdout_limit=16_000_000).stdout)
            def audio_clock(value):
                streams=[s for s in value['streams'] if s['codec_type']=='audio']
                require(len(streams)==1,'Exactly one actual narration stream required.')
                stream=streams[0];index=stream['index']
                return ({k:v for k,v in stream.items() if k!='index'},
                        [{k:v for k,v in p.items() if k!='stream_index'} for p in value['packets'] if p['stream_index']==index])
            require(audio_clock(timeline)==audio_clock(audio_timeline),'Actual narration playback timing/layout differs from retained encoding.')
            compared=dict(output_sha256=before,reference_sha256=None,retained_inputs_sha256=fingerprint(inputs),
                audio_reference_sha256=file_sha(audio_reference),independent_render_verified=False,
                decoded_video_sha256=actual_video[1],decoded_audio_sha256=actual_audio[1],timeline_sha256=fingerprint(timeline),
                video_bytes=actual_video[0],audio_bytes=actual_audio[0],frames_verified=frames)
            if self.reference_mode:
                reference=Path(temporary)/'reference.mp4'
                self.compositor(self.staged,job.snapshot,reference)
                compared.update(compare_decoded(reference,path,frames=frames));compared['independent_render_verified']=True
            require(file_sha(path)==before,'Output changed during candidate verification.')
            require(file_sha(narration)==inputs['narration_sha256'],'Retained narration changed during verification.')
            for clip in clips:checked_file(root,str(clip['path']),clip['sha256'])
        import inspect
        source=Path(inspect.getfile(type(self.compositor)))
        self.evidence=CandidateMediaEvidence(snapshot_sha256=job.snapshot_sha256,source_order=tuple(job.snapshot['input_order']),
            narrated_text_sha256=job.snapshot['narrated_text_sha256'],caption_text_sha256=job.snapshot['narrated_text_sha256'],
            compositor_sha256=file_sha(source),implementation_sha256=file_sha(Path(__file__).resolve()),**compared)
        return self.evidence
    def evidence_json(self):
        require(type(self.evidence) is CandidateMediaEvidence,'Actual candidate verification has not completed.')
        return canonical(asdict(self.evidence))
