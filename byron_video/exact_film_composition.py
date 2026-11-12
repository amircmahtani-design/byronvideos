"""This-film-only neutral rerender; no provider submission or historical approval."""
from dataclasses import asdict
import hashlib,json,math,subprocess,tempfile,threading
from pathlib import Path
import numpy as np
from .hosted_control import require
from .elevenlabs_adapter import fingerprint
from . import preview_composition as preview
from . import portrait_composition as neutral

RELEASE=Path(__file__).resolve().parent.parent/'style-guide'/'exact-film'/'release.json'
EQUIVALENCE=dict(method='all-frame-retained-film-v1',crop_top=168,width=270,height=438,
                 max_frame_mean_error=3.0,max_tile_mean_error=8.0,max_changed_pixel_fraction=.02,
                 changed_pixel_threshold=32,decoded_audio_exact=True)

def sha(data):return hashlib.sha256(data).hexdigest()

def release_hash():
    value=json.loads(RELEASE.read_text())
    require(value.get('schema_version')==1 and value.get('scope')=='OWNER_APPROVED_EXACT_FILM'
        and value.get('future_style_approval') is False and value.get('automatic_publication') is False
        and value.get('neutral_compositor_sha256')==sha(Path(neutral.__file__).read_bytes())
        and value.get('preview_validator_sha256')==sha(Path(preview.__file__).read_bytes())
        and value.get('exact_compositor_sha256')==sha(Path(__file__).read_bytes())
        and value.get('equivalence')==EQUIVALENCE,'Exact-film composition release differs.')
    return fingerprint(value)

def _regular(path):
    path=Path(path)
    require(path.is_absolute() and path.is_file() and not any(p.is_symlink() for p in (path,*path.parents)),
            'Private regular exact-film media required.')
    return path

def _audio_digest(path,timeout):
    # Bounded full decode; exact copied AAC content must decode identically.
    command=['ffmpeg','-nostdin','-v','error','-xerror',*neutral.input_options(path,video=True),'-i',str(path),
             '-map','0:a:0','-vn','-ac','1','-ar','24000','-f','s16le','pipe:1']
    with tempfile.TemporaryFile() as errors:
        proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=errors)
        timer=threading.Timer(timeout,proc.kill);timer.daemon=True;timer.start();digest=hashlib.sha256();size=0
        try:
            while True:
                block=proc.stdout.read(65536)
                if not block:break
                size+=len(block);require(size<=44_000_000,'Exact-film audio exceeds bound.');digest.update(block)
            require(proc.wait(timeout=10)==0 and size>0,'Exact-film audio decode failed.')
        finally:
            timer.cancel()
            if proc.poll() is None:proc.kill();proc.wait()
            proc.stdout.close()
    return dict(sha256=digest.hexdigest(),bytes=size)

def verify_equivalence(preview_path,final_path,*,expected_frames,timeout=1800):
    """Compare every decoded frame below the label, plus exact decoded audio.

    This supplements (does not replace) exact immutable input/timeline provenance.
    Lossy second-generation preview encoding needs bounded pixel tolerance.
    """
    paths=[_regular(preview_path),_regular(final_path)]
    require(type(expected_frames) is int and 0<expected_frames<=21600,'Bounded exact-film frame count required.')
    before=[sha(p.read_bytes()) for p in paths];processes=[];timers=[];errors=[];count=0;maxmean=0.;maxtile=0.;maxchanged=0.
    try:
        for path in paths:
            error=tempfile.TemporaryFile();errors.append(error)
            proc=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-xerror','-threads','1',*neutral.input_options(path,video=True),
                '-i',str(path),'-map','0:v:0','-an','-vf','crop=1080:1752:0:168:exact=1,scale=270:438:flags=area,format=rgb24',
                '-fps_mode','passthrough','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],stdout=subprocess.PIPE,stderr=error)
            processes.append(proc);timer=threading.Timer(timeout,proc.kill);timer.daemon=True;timer.start();timers.append(timer)
        while True:
            blocks=[p.stdout.read(270*438*3) for p in processes]
            if not any(blocks):break
            require(all(len(b)==270*438*3 for b in blocks),'Exact-film frame count or decoded geometry differs.')
            count+=1;require(count<=expected_frames,'Exact-film added frames.')
            arrays=[np.frombuffer(b,dtype=np.uint8).reshape(438,270,3).astype(np.int16) for b in blocks]
            delta=np.abs(arrays[0]-arrays[1]);mean=float(delta.mean());changed=float((delta.max(axis=2)>32).mean())
            tile=max(float(t.mean()) for rows in np.array_split(delta,6,axis=0) for t in np.array_split(rows,6,axis=1))
            require(mean<=3 and tile<=8 and changed<=.02,'Exact-film content differs from the viewed preview outside its label.')
            maxmean=max(maxmean,mean);maxtile=max(maxtile,tile);maxchanged=max(maxchanged,changed)
        require(count==expected_frames and all(p.wait(timeout=10)==0 for p in processes),'Exact-film full comparison decode failed.')
    finally:
        for timer in timers:timer.cancel()
        for proc in processes:
            if proc.poll() is None:proc.kill();proc.wait()
            proc.stdout.close()
        for error in errors:error.close()
    audio=[_audio_digest(p,timeout) for p in paths]
    require(audio[0]==audio[1],'Exact-film narration audio differs from the owner-viewed preview.')
    require([sha(p.read_bytes()) for p in paths]==before,'Exact-film bytes changed during equivalence verification.')
    return dict(schema_version=1,method=EQUIVALENCE['method'],policy_sha256=fingerprint(EQUIVALENCE),
        preview_sha256=before[0],final_sha256=before[1],frames_verified=count,all_frames_verified=True,
        maximum_frame_mean_error=round(maxmean,6),maximum_tile_mean_error=round(maxtile,6),
        maximum_changed_pixel_fraction=round(maxchanged,6),decoded_audio=audio[0],decoded_audio_equal=True)

def validate_exact_inputs(staged,frozen,*,job,lineage,preview_evidence_verifier,preview_source_verifier,
                       expected_ffmpeg_version,now=None):
    """Independent archived lineage plus actual retained media validation."""
    from .exact_film_policy import validate_exact_film_lineage
    historical=validate_exact_film_lineage(job,lineage,expected_final_release_sha256=release_hash(),now=now)
    require(staged['manifest_sha256']==lineage['manifest_sha256'] and frozen==job.snapshot,
            'Exact-film retained immutable manifest differs from the owner approval.')
    value=lineage['inputs'];context=value['preview_context']
    validated=preview.validate_preview(staged,frozen,report=value['report'],expected_report_sha256=value['report_sha256'],
        clip_receipts=value['clip_receipts'],context=context,expected_context_sha256=value['preview_context_sha256'],
        expected_authorization_sha256=context['authorization_sha256'],expected_preview_release_sha256=context['preview_release_sha256'],
        expected_ffmpeg_version=expected_ffmpeg_version,preview_evidence_verifier=preview_evidence_verifier,
        preview_source_verifier=preview_source_verifier,now=historical)
    return validated

def compose_exact_film(staged,frozen,output,*,job,lineage,preview_evidence_verifier,preview_source_verifier,
                       expected_ffmpeg_version,now=None):
    validated=validate_exact_inputs(staged,frozen,job=job,lineage=lineage,preview_evidence_verifier=preview_evidence_verifier,
        preview_source_verifier=preview_source_verifier,expected_ffmpeg_version=expected_ffmpeg_version,now=now)
    value=lineage['inputs']
    style,audio,clips,captions,starts,duration=validated
    render_proof=dict(schema_version=1,method='exact-retained-neutral-timeline-v1',
        approval_sha256=lineage['approval_sha256'],retained_manifest_sha256=staged['manifest_sha256'],
        context_sha256=value['preview_context_sha256'],final_release_sha256=release_hash(),
        preview_proof_sha256=lineage['preview_proof_sha256'],preview_artifact_sha256=lineage['preview_proof']['sha256'],
        narration_sha256=value['report']['audio_sha256'],narration_report_sha256=value['report_sha256'],
        source_order=frozen['input_order'],clips=[{k:v for k,v in clip.items() if k!='path'} for clip in clips],
        captions=[asdict(c) for c in captions],scene_starts=starts,duration_seconds=duration,
        presentation_sha256=fingerprint(style),ffmpeg_version=expected_ffmpeg_version,
        neutral_compositor_sha256=sha(Path(neutral.__file__).read_bytes()),watermark_omitted=True,
        paid_generation_calls=0,future_style_approval=False,automatic_publication=False)
    manifest=dict(motion_contract={'compiled_sha256':release_hash()},script_sha256=frozen['narrated_text_sha256'],
        narration={'sha256':value['report']['audio_sha256'],'exactText':frozen['narrated_text']},source_order=frozen['input_order'])
    result=neutral._compose_validated(manifest,staged['root'],output,validated,
        dict(status='OWNER_APPROVED_EXACT_FILM',this_film_owner_approved=True,human_style_accepted=False,
             live_release=False,synthetic=value['report'].get('synthetic') is True,
             source_semantic_motion_review='Archived bounded candidate QC; explicit owner approval of this exact viewed film only.'))
    result['render_proof']=render_proof
    Path(output).with_suffix('.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result

class ExactFilmCompositor:
    def __init__(self,*,job,lineage,preview_evidence_verifier,preview_source_verifier,expected_ffmpeg_version,now=None):
        self.job=job;self.lineage=lineage;self.qc=preview_evidence_verifier;self.source=preview_source_verifier
        self.version=expected_ffmpeg_version;self.now=now
    def preflight(self,staged,frozen):
        return validate_exact_inputs(staged,frozen,job=self.job,lineage=self.lineage,preview_evidence_verifier=self.qc,
            preview_source_verifier=self.source,expected_ffmpeg_version=self.version,now=self.now)
    def __call__(self,staged,frozen,output):
        return compose_exact_film(staged,frozen,output,job=self.job,lineage=self.lineage,
            preview_evidence_verifier=self.qc,preview_source_verifier=self.source,expected_ffmpeg_version=self.version,now=self.now)
