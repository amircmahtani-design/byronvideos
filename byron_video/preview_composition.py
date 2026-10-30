"""Separate private draft compositor. Historical portrait source/locks unchanged.

Reuses the recovered neutral renderer, then burns an opaque conspicuous watermark
and verifies its decoded pixels on EVERY frame. Clean automated candidate QC is
not relabelled as human review or final style acceptance.
"""
from __future__ import annotations
import hashlib,importlib.metadata,json,math,re,subprocess,tempfile,threading
from pathlib import Path
from PIL import Image,ImageDraw
import numpy as np
from . import portrait_composition as base
from .elevenlabs_adapter import fingerprint,plan_requests

ROOT=Path(__file__).resolve().parent.parent
RELEASE=ROOT/'style-guide/portrait-preview/release.json'
WATERMARK={'text':'DRAFT PRIVATE PREVIEW','width':1080,'height':168,'font_size':58,
           'background':'#000000','foreground':'#FFFFFF','stripe':'#FFD400','stripe_height':12,
           'minimum_glyph_iou':.97,'minimum_global_iou':.985,'all_frames':True}
require=base.require
sha=lambda data:hashlib.sha256(data).hexdigest()


def release_hash():
    release=json.loads(RELEASE.read_text())
    require(release['schema_version']==1 and release['status']=='PRIVATE_DRAFT_PREVIEW_ONLY'
            and release['human_style_accepted'] is False and release['live_release'] is False
            and release['neutral_compositor_sha256']==sha(Path(base.__file__).read_bytes())
            and release['preview_compositor_sha256']==sha(Path(__file__).read_bytes())
            and release['presentation_sha256']==base.PRESENTATION_SHA and release['watermark']==WATERMARK,
            'Private preview release changed or attempts to claim acceptance.')
    return fingerprint(release)


def watermark_template():
    style=base.presentation();font=base._font(WATERMARK['font_size'],style)
    image=Image.new('RGB',(1080,168),WATERMARK['background']);draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,1079,11),fill=WATERMARK['stripe'])
    text=WATERMARK['text'];width=draw.textlength(text,font=font);x=(1080-width)/2;y=49
    require(width<1000,'Watermark font no longer fits its independently fixed band.')
    draw.text((x,y),text,font=font,fill=WATERMARK['foreground'])
    boxes=[]
    for index,char in enumerate(text):
        if char==' ':continue
        left=max(0,math.floor(x+draw.textlength(text[:index],font=font)))
        right=min(1080,math.ceil(x+draw.textlength(text[:index+1],font=font)))
        boxes.append((left,24,right,155))
    return image,boxes


def verify_watermark(path,*,expected_frames=None,timeout=1800):
    """Decode all watermark pixels, compare each glyph, and bind exact MP4 bytes.

    No sidecar/metadata bit or sampled-frame substitute can pass this verifier.
    Lossy codec tolerance is bounded per glyph as well as across the full text.
    """
    path=Path(path);require(path.is_absolute() and path.is_file() and not any(p.is_symlink() for p in (path,*path.parents)),
                           'Private regular preview output required.')
    before=sha(path.read_bytes());template,boxes=watermark_template()
    expected=np.asarray(template.convert('L'))>128;expected[:24,:]=False
    expected[155:,:]=False;count=0;minimum=1.;frame_digest=hashlib.sha256()
    with tempfile.TemporaryFile() as errors:
        process=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-xerror','-threads','1',*base.input_options(path,video=True),
            '-i',str(path),'-map','0:v:0','-an','-vf','crop=1080:168:0:0:exact=1,format=gray','-fps_mode','passthrough',
            '-f','rawvideo','-pix_fmt','gray','pipe:1'],stdout=subprocess.PIPE,stderr=errors)
        timer=threading.Timer(timeout,process.kill);timer.daemon=True;timer.start()
        try:
            while True:
                raw=process.stdout.read(1080*168)
                if not raw:break
                require(len(raw)==1080*168,'Truncated preview watermark frame.')
                count+=1;require(count<=21600,'Preview watermark frame count exceeds bound.')
                pixels=np.frombuffer(raw,dtype=np.uint8).reshape((168,1080));actual=pixels>128
                actual[:24,:]=False;actual[155:,:]=False
                union=np.count_nonzero(actual|expected);intersection=np.count_nonzero(actual&expected)
                score=intersection/union if union else 0.
                require(score>=WATERMARK['minimum_global_iou'],'A decoded frame lacks the exact visible PRIVATE DRAFT watermark.')
                for left,top,right,bottom in boxes:
                    a=actual[top:bottom,left:right];e=expected[top:bottom,left:right]
                    union=np.count_nonzero(a|e);iou=np.count_nonzero(a&e)/union if union else 0.
                    require(iou>=WATERMARK['minimum_glyph_iou'],'A decoded frame changed or obscured a required watermark glyph.')
                    minimum=min(minimum,iou)
                # Opaque stripe/background is also observable, not alpha metadata.
                require(float(pixels[16:23,:].mean())<8 and float(pixels[:10,:].mean())>150,
                        'Private draft watermark banner is absent or translucent.')
                frame_digest.update(raw)
            require(process.wait(timeout=10)==0 and count>0,'Preview watermark decoding failed.')
        finally:
            timer.cancel()
            if process.poll() is None:process.kill();process.wait()
            process.stdout.close()
    require(expected_frames is None or count==expected_frames,'Preview watermark coverage omitted or added frames.')
    require(sha(path.read_bytes())==before,'Preview bytes changed during watermark verification.')
    return dict(schema_version=1,method='all-decoded-frame-glyph-template-v1',text=WATERMARK['text'],
        watermark_spec_sha256=fingerprint(WATERMARK),artifact_sha256=before,frames_verified=count,
        decoded_banner_sha256=frame_digest.hexdigest(),minimum_glyph_iou=round(minimum,6),
        expected_template_sha256=sha(template.tobytes()),all_frames_verified=True)


def validate_preview(staged,frozen,*,report,expected_report_sha256,clip_receipts,context,expected_context_sha256,
                     expected_authorization_sha256,expected_preview_release_sha256,expected_ffmpeg_version,preview_evidence_verifier,preview_source_verifier,now=None):
    from .private_preview import validate_preview_context
    require(expected_preview_release_sha256==release_hash(),'Independently approved private preview release differs.')
    style=base.presentation();root=Path(staged['root'])
    require(staged['snapshot_sha256']==fingerprint(frozen),'Private preview snapshot changed.')
    require(fingerprint(report)==expected_report_sha256 and report.get('status')=='DRAFT_RELEASE_CANDIDATE'
            and report.get('provider')=='elevenlabs' and report.get('media_verified') is True
            and report.get('provider_receipts_verified') is True and report.get('human_approved') is False
            and report.get('live_release') is False,'Distinct verified native narration report required.')
    if report.get('synthetic') is not True:
        require(importlib.metadata.version('pillow')=='13.0.0' and importlib.metadata.version('numpy')=='2.5.4',
                'Real private preview still requires the pinned media runtime.')
    text=frozen['narrated_text'];profile=frozen['processing_plan']['narration_profile'];provenance=report.get('provenance',{})
    require(report.get('exact_text')==text and sha(text.encode())==frozen['narrated_text_sha256']
        and report.get('profile')==profile and report.get('profile_sha256')==fingerprint(profile)
        and provenance.get('owner_job_sha256')==sha((staged['owner_id']+'\0'+staged['job_id']).encode())
        and provenance.get('exact_text_sha256')==frozen['narrated_text_sha256'] and provenance.get('profile_sha256')==fingerprint(profile)
        and provenance.get('plan_sha256')==fingerprint(plan_requests(text,profile)),'Preview narration differs from frozen owner/text/voice.')
    require(report['ffmpeg_version']==expected_ffmpeg_version and base.run(['ffmpeg','-version']).stdout.decode().splitlines()[0].split()[2]==expected_ffmpeg_version,
            'Private preview FFmpeg differs from reviewed runtime.')
    duration=report['duration_seconds'];require(type(duration) in (int,float) and math.isfinite(duration) and 0<duration<=900,'Bounded native narration duration required.')
    audio=base.checked_file(root,str(staged['files']['narration.wav']),report['audio_sha256'],100_000_000)
    require(abs(base.probe(audio,video=False)['duration']-duration)<=.001,'Preview narration duration changed.')
    words=base.validate_words(text,report['words'],duration)
    composition=json.loads(Path(staged['files']['composition.json']).read_text())
    require(set(composition)=={'schema_version','status','snapshot_sha256','narration_report_sha256','clips'}
        and composition['schema_version']==1 and composition['status']=='PRIVATE_DRAFT_PREVIEW'
        and composition['snapshot_sha256']==staged['snapshot_sha256'] and composition['narration_report_sha256']==expected_report_sha256,
        'Preview composition must have its distinct source-bound status.')
    models=validate_preview_context(context,expected_context_sha256,staged=staged,frozen=frozen,clip_receipts=clip_receipts,
        expected_report_sha256=expected_report_sha256,expected_authorization_sha256=expected_authorization_sha256,
        preview_release_sha256=expected_preview_release_sha256,preview_evidence_verifier=preview_evidence_verifier,preview_source_verifier=preview_source_verifier,now=now)
    planned=composition['clips'];images=frozen['images'];require(len(images) in (4,5) and len(planned)==len(images)==len(clip_receipts)
        and frozen['input_order']==[x['id'] for x in images],'Preview source order changed.')
    checked=[];starts=[0.];seen=set();overlap=style['transitions']['crossfade_seconds']
    for index,(item,image,receipt) in enumerate(zip(planned,images,clip_receipts)):
        require(set(item)=={'image_id','trim_length','presentation_crop'} and item['image_id']==image['id'],'Preview reordered an original.')
        require(receipt.get('schema')=='byron-private-preview-clip-v1' and receipt.get('image_id')==image['id']
            and receipt.get('source_image_sha256')==image['sha256'] and receipt.get('provider')=='fal' and receipt.get('model')==models[index]
            and receipt.get('media_verified') is True and receipt.get('human_approved') is False
            and 'no_invented_action_reviewed' not in receipt and receipt.get('synthetic') is report.get('synthetic')
            and receipt.get('presentation_crop')==item['presentation_crop'],'Preview QC must not masquerade as a human clip review.')
        digest=receipt['sha256'];require(digest not in seen,'Private preview cannot repeat footage.');seen.add(digest)
        path=base.checked_file(root,str(staged['files'][f'clip-{index+1:02}.mp4']),digest)
        media=base.probe(path,video=True,presentation_crop=item['presentation_crop']);length=item['trim_length']
        require(type(length) in (int,float) and math.isfinite(length) and overlap*2<length<=media['duration']+.0001,'Insufficient continuous preview footage.')
        checked.append(dict(path=path,sha256=digest,trim_start=0,trim_length=length,presentation_crop=item['presentation_crop']))
        if index<len(images)-1:starts.append(starts[-1]+length-overlap)
    require(abs(starts[-1]+checked[-1]['trim_length']-duration)<.001,'Preview omitted native narration coverage.')
    captions=base.make_captions(text,words,style=style)
    require(re.sub(r'\s+','',''.join(c.text for c in captions))==re.sub(r'\s+','',text),'Preview caption words changed.')
    font=base._font(round(style['canvas']['height']*style['captions']['font_height_fraction']),style)
    for caption in captions:base.wrap_caption(caption.text,font,round(style['canvas']['width']*style['captions']['safe_width_fraction']))
    return style,audio,checked,captions,starts,duration


def compose_preview(staged,frozen,output,**bindings):
    validated=validate_preview(staged,frozen,**bindings);report=bindings['report'];output=Path(output);root=Path(staged['root']).resolve()
    require(output.is_absolute() and output.resolve().is_relative_to(root) and not output.exists(),'New private preview path required.')
    manifest=dict(motion_contract={'compiled_sha256':release_hash()},script_sha256=frozen['narrated_text_sha256'],
        narration={'sha256':report['audio_sha256'],'exactText':report['exact_text']},source_order=frozen['input_order'])
    with tempfile.TemporaryDirectory(prefix='private-preview-',dir=root) as scratch:
        scratch=Path(scratch);neutral=scratch/'neutral.mp4';watermark=scratch/'watermark.png';encoded=scratch/'draft.mp4'
        provenance=dict(status='PRIVATE_DRAFT_PREVIEW',human_approved=False,human_style_accepted=False,live_release=False,
            synthetic=report.get('synthetic') is True,source_semantic_motion_review='Bounded automated candidate QC; no human approval.')
        result=base._compose_validated(manifest,root,neutral,validated,provenance)
        template,_=watermark_template();template.save(watermark,format='PNG')
        base.run(['ffmpeg','-nostdin','-v','error','-n','-threads','1',*base.input_options(neutral,video=True),'-i',str(neutral),
            '-i',str(watermark),'-filter_complex','[0:v][1:v]overlay=0:0:format=auto[v]','-map','[v]','-map','0:a:0',
            '-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p','-threads','2','-c:a','copy','-movflags','+faststart',str(encoded)],1800)
        proof=verify_watermark(encoded,expected_frames=math.ceil(validated[-1]*24))
        encoded.replace(output)
        # Sidecars retain only truthful draft provenance, never an unmarked path.
        result.update(local_path=str(output),sha256=proof['artifact_sha256'],watermark_proof=proof,
            preview_release_sha256=bindings['expected_preview_release_sha256'],authorization_sha256=bindings['expected_authorization_sha256'],
            preview_context_sha256=bindings['expected_context_sha256'])
        output.with_suffix('.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        output.with_suffix('.srt').write_bytes(neutral.with_suffix('.srt').read_bytes())
    return result


class PrivatePreviewCompositor:
    def __init__(self,evidence_loader,*,expected_authorization_sha256,expected_preview_release_sha256,expected_ffmpeg_version,preview_evidence_verifier,preview_source_verifier,now=None):
        self.loader=evidence_loader;self.auth=expected_authorization_sha256;self.release=expected_preview_release_sha256;self.version=expected_ffmpeg_version;self.now=now;self.verifier=preview_evidence_verifier;self.source_verifier=preview_source_verifier
    def __call__(self,staged,frozen,output):
        value=self.loader(staged['snapshot_sha256'])
        return compose_preview(staged,frozen,output,report=value['report'],expected_report_sha256=value['report_sha256'],
            clip_receipts=value['clip_receipts'],context=value['preview_context'],expected_context_sha256=value['preview_context_sha256'],
            expected_authorization_sha256=self.auth,expected_preview_release_sha256=self.release,expected_ffmpeg_version=self.version,preview_evidence_verifier=self.verifier,preview_source_verifier=self.source_verifier,now=self.now)
