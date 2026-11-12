"""Separate ElevenLabs portrait composition DRAFT; never a legacy preset override.

Neutral decoder/crop/probe/frame-composition algorithm recovered from the private
refinement source; only generic code and visual settings are reused. No private
browser receipts, Library IDs, account IDs, prompts or source media are included.
"""
from __future__ import annotations
import bisect, hashlib, importlib.metadata, json, math, re, subprocess, tempfile, time
from dataclasses import asdict
from pathlib import Path
from PIL import Image
from .engine import Word, _colour, _font, draw_caption, make_captions, wrap_caption, write_srt
from .elevenlabs_adapter import fingerprint, plan_requests

SHA=re.compile(r'[0-9a-f]{64}\Z')
PRESENTATION_SHA='8ed4dad7ae848fa9bd97ecd0e4ef5f6c9f79c10ef98ab451f894598a4a6b5542'
BASE_SHA=PRESENTATION_SHA
HELPER_ENGINE_SHA='a4c8a76b3921309a3d202a7f578353e123d84a395222deab884ce56fafa934e8'

def require(ok,message):
    if not ok:raise ValueError(message)
def digest(data):return hashlib.sha256(data).hexdigest()
def checked_file(root,value,expected,max_bytes=50_000_000):
    require(isinstance(value,str) and Path(value).is_absolute(),'A private absolute path is required.')
    path=Path(value);require(not path.is_symlink(),'Symlink inputs are forbidden.')
    base,actual=Path(root).resolve(strict=True),path.resolve(strict=True)
    require(actual.is_relative_to(base) and actual!=base and actual.is_file(),'Input escaped the private job root.')
    require(0<actual.stat().st_size<=max_bytes,'File exceeds its technical size limit.')
    require(isinstance(expected,str) and SHA.fullmatch(expected) and digest(actual.read_bytes())==expected,'Input file fingerprint mismatch.')
    return actual


def run(command,timeout=120):return subprocess.run(command,check=True,capture_output=True,timeout=timeout)


def run_bounded(command,timeout=60,stdout_limit=262144):
    """Bound metadata while reading it; capture_output would allocate first."""
    import threading
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    outputs=[bytearray(),bytearray()];exceeded=threading.Event()
    def drain(pipe,index,limit):
        try:
            while True:
                chunk=pipe.read(4096)
                if not chunk:break
                if len(outputs[index])+len(chunk)>limit:
                    exceeded.set();process.kill();break
                outputs[index].extend(chunk)
        finally:pipe.close()
    threads=[threading.Thread(target=drain,args=(process.stdout,0,stdout_limit),daemon=True),
             threading.Thread(target=drain,args=(process.stderr,1,65536),daemon=True)]
    for thread in threads:thread.start()
    try:process.wait(timeout=timeout)
    except BaseException:
        process.kill();process.wait()
        raise
    finally:
        for thread in threads:thread.join(timeout=2)
    require(not exceeded.is_set() and not any(t.is_alive() for t in threads),'Media metadata exceeds its bounded output limit.')
    if process.returncode:raise subprocess.CalledProcessError(process.returncode,command,bytes(outputs[0]),bytes(outputs[1]))
    return subprocess.CompletedProcess(command,process.returncode,bytes(outputs[0]),bytes(outputs[1]))


def input_options(path,*,video):
    # Never autodetect disguised ffconcat/HLS playlists or external MOV references.
    if not video and Path(path).suffix.lower()=='.wav':return ['-protocol_whitelist','file,pipe','-f','wav']
    require(Path(path).suffix.lower() in ('.mp4','.m4a'),'Only MP4 clips or WAV/MP4 narration are supported.')
    return ['-protocol_whitelist','file,pipe','-f','mov','-enable_drefs','0','-use_absolute_path','0']


def validate_presentation_crop(crop,w,h):
    require(isinstance(crop,dict) and set(crop)=={'left','top','width','height'} and all(type(v) is int for v in crop.values()),'Explicit integer crop geometry required.')
    x,y,cw,ch=(crop[k] for k in ('left','top','width','height'))
    require(w*16!=h*9,'Exact portrait media does not need a rounding crop.')
    require(0<=x and 0<=y and cw>0 and ch>0 and x+cw<=w and y+ch<=h and cw*16==ch*9,'Crop must remain inside native frames and be exact 9:16.')
    require(2*x+cw==w and 2*y+ch==h and cw%2==0 and ch%2==0,'Only centered even-pixel presentation crops are permitted.')
    require(0<(w*h-cw*ch)/(w*h)<=.02,'Provider rounding crop exceeds the 2 percent removed-area limit.')
    return crop


def probe(path,*,video,presentation_crop=None):
    options=input_options(path,video=video)
    info=json.loads(run_bounded(['ffprobe','-v','error',*options,'-show_format','-show_streams','-of','json',str(path)],30,65536).stdout)
    streams=[s for s in info['streams'] if s['codec_type']==('video' if video else 'audio')]
    require(len(streams)==1,'Exactly one requested media stream is required.')
    stream=streams[0];duration=float(stream.get('duration',info['format'].get('duration',0)))
    require(math.isfinite(duration) and duration>0,'Invalid media duration.')
    if video:
        require('mp4' in info['format']['format_name'] and stream['codec_name'] in ('h264','hevc','av1'),'Unsupported MP4 video codec.')
        w,h=stream['width'],stream['height']
        require(64<=w<=2160 and 64<=h<=3840,'Clip dimensions exceed the technical limit.')
        if presentation_crop is None:require(w*16==h*9,'Use upright exact 9:16 clips within the size limit.')
        else:validate_presentation_crop(presentation_crop,w,h)
        require(stream.get('sample_aspect_ratio','1:1') in ('1:1','N/A'),'Non-square pixels are forbidden.')
        require(not stream.get('tags',{}).get('rotate') and not any(x.get('rotation',0) for x in stream.get('side_data_list',[])),'Unexpected rotation.')
        require(duration<=60.05,'A continuous scene exceeds the 60-second technical limit.')
        for key in ('avg_frame_rate','r_frame_rate'):
            a,b=map(int,stream[key].split('/'));require(b>0 and abs(a/b-24)<1e-8,'Native constant 24 fps required; retiming is forbidden.')
        frames=json.loads(run_bounded(['ffprobe','-v','error',*options,'-select_streams','v:0','-show_entries','frame=best_effort_timestamp_time','-of','json',str(path)],60,262144).stdout)['frames']
        require(isinstance(frames,list) and 0<len(frames)<=60*24,'Decoded frame count exceeds the 60-second scene bound.')
        require(all(abs(float(f['best_effort_timestamp_time'])-i/24)<.0001 for i,f in enumerate(frames)),'Clip timestamps must be zero-based and constant-rate.')
        require(abs(duration-len(frames)/24)<=1/24+.001,'Declared duration disagrees with decoded native frame count.')
        duration=len(frames)/24;stream['verified_frames']=len(frames)
    run(['ffmpeg','-nostdin','-v','error','-xerror',*options,'-i',str(path),'-map','0:v:0' if video else '0:a:0','-an' if video else '-vn','-f','null','-'])
    return {'duration':duration,'stream':stream}


def validate_words(text,entries,duration):
    require(entries,'Narration word timings are required.');words=[];cursor=0;previous=0.
    for entry in entries:
        word=Word(**entry)
        require(type(word.char_start) is int and type(word.char_end) is int and cursor<=word.char_start<word.char_end<=len(text) and not text[cursor:word.char_start].strip() and word.text==text[word.char_start:word.char_end],'Timing text differs from exact script.')
        require(all(type(t) in (int,float) and math.isfinite(t) for t in (word.start,word.end)) and 0<=previous<=word.start<=word.end<=duration,'Word times are invalid or overlapping.')
        require(not any(ch.isspace() for ch in word.text),'A timing token cannot span whitespace.')
        require(not re.search(r'\w',word.text) or word.end>word.start,'A lexical timing token must have positive duration.')
        require(not (word.char_start>0 and text[word.char_start-1].isalnum() and word.text[0].isalnum())
                and not (word.char_end<len(text) and word.text[-1].isalnum() and text[word.char_end].isalnum()),'A timing token cannot split an alphanumeric word.')
        words.append(word);cursor=word.char_end;previous=word.end
    require(not text[cursor:].strip(),'Word timings omit script text.')
    return words


class Decoder:
    """Sequential native-rate decode; EOF is failure, never a held frame."""
    def __init__(self,path,width,height,*,presentation_crop=None):
        self.size=width*height*3;self.dimensions=(width,height);self.index=-1;self.frame=None;self.errors=tempfile.TemporaryFile()
        filters=[]
        if presentation_crop is not None:
            c=presentation_crop;filters.append(f"crop={c['width']}:{c['height']}:{c['left']}:{c['top']}:exact=1")
        filters.append(f'scale={width}:{height}:flags=lanczos')
        self.process=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-xerror','-threads','1','-filter_threads','1',*input_options(path,video=True),'-i',str(path),'-map','0:v:0','-an','-vf',','.join(filters),'-fps_mode','passthrough','-f','rawvideo','-pix_fmt','rgb24','-threads','1','pipe:1'],stdout=subprocess.PIPE,stderr=self.errors)
    def at(self,number):
        require(number>=self.index,'Decoder cannot rewind or loop footage.')
        while self.index<number:
            raw=self.process.stdout.read(self.size);require(len(raw)==self.size,'Footage ended early; no frozen frame substituted.')
            self.frame=Image.frombytes('RGB',self.dimensions,raw);self.index+=1
        return self.frame.copy()
    def close(self):
        if self.process.poll() is None:self.process.terminate()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        self.process.stdout.close();self.errors.close()


def _compose_validated(manifest,private_root,output,validated,provenance):
    """Single shared compositor; use a provenance-validating public entry point."""
    started=time.monotonic();style,audio,clips,captions,starts,duration=validated
    root,output=Path(private_root).resolve(strict=True),Path(output).resolve()
    require(output.is_relative_to(root) and output.suffix=='.mp4','Output must be a private MP4 in job root.')
    require(not any(output.with_suffix(x).exists() for x in ('.mp4','.json','.srt')),'Output exists; never overwrite a recording.')
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    w,h,fps=(style['canvas'][k] for k in ('width','height','fps'));font=_font(round(h*style['captions']['font_height_fraction']),style)
    capstarts=[c.start for c in captions];overlap=style['transitions']['crossfade_seconds'];decoders={};enc=style['encoding']
    with tempfile.TemporaryDirectory(prefix='motion-',dir=output.parent) as temp:
        encoded=Path(temp)/'film.mp4'
        with (Path(temp)/'encoder.log').open('wb') as errors:
            command=['ffmpeg','-nostdin','-v','error','-filter_threads','1','-y','-f','rawvideo','-pix_fmt','rgb24','-s',f'{w}x{h}','-r',str(fps),'-i','pipe:0',*input_options(audio,video=False),'-i',str(audio),'-map','0:v:0','-map','1:a:0','-c:v',enc['video_codec'],'-preset',enc['video_preset'],'-crf',str(enc['crf']),'-threads',str(enc['encoder_threads']),'-pix_fmt',enc['pixel_format'],'-colorspace',style['canvas']['colour_space'],'-color_primaries',style['canvas']['colour_space'],'-color_trc',style['canvas']['colour_space'],'-c:a',enc['audio_codec'],'-b:a',enc['audio_bitrate'],'-movflags','+faststart','-t',f'{duration:.9f}',str(encoded)]
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=errors)
            try:
                for number in range(math.ceil(duration*fps)):
                    t=number/fps;scene=bisect.bisect_right(starts,t)-1
                    active=[scene-1,scene] if scene and t<starts[scene]+overlap else [scene]
                    for old in set(decoders)-set(active):decoders.pop(old).close()
                    frames=[]
                    for index in active:
                        if index not in decoders:decoders[index]=Decoder(clips[index]['path'],w,h,presentation_crop=clips[index].get('presentation_crop'))
                        frames.append(decoders[index].at(math.floor((t-starts[index])*fps+1e-7)))
                    frame=frames[-1]
                    if len(frames)==2:
                        mix=(t-starts[scene])/overlap;frame=Image.blend(frames[0],frames[1],mix*mix*(3-2*mix))
                    fade=min(1,t/style['transitions']['opening_fade_seconds'],max(0,(duration-t)/style['transitions']['closing_fade_seconds']))
                    if fade<1:frame=Image.blend(Image.new('RGB',(w,h),_colour(style,'charcoal')),frame,fade)
                    ci=bisect.bisect_right(capstarts,t)-1;draw_caption(frame,captions[ci] if ci>=0 and t<=captions[ci].end else None,font,style)
                    process.stdin.write(frame.tobytes())
                process.stdin.close();require(process.wait(timeout=180)==0,'Encoding failed; incomplete output discarded.')
            except BaseException:process.kill();process.wait();raise
            finally:
                for decoder in decoders.values():decoder.close()
        info=json.loads(run(['ffprobe','-v','error',*input_options(encoded,video=True),'-show_format','-show_streams','-of','json',str(encoded)],30).stdout)
        videos=[x for x in info['streams'] if x['codec_type']=='video'];sounds=[x for x in info['streams'] if x['codec_type']=='audio']
        require(len(videos)==len(sounds)==1 and videos[0]['codec_name']=='h264' and sounds[0]['codec_name']=='aac' and (videos[0]['width'],videos[0]['height'])==(w,h) and abs(float(info['format']['duration'])-duration)<=1/fps+.02,'Export verification failed.')
        run(['ffmpeg','-nostdin','-v','error','-xerror',*input_options(encoded,video=True),'-i',str(encoded),'-f','null','-'],180)
        checked_file(root,str(audio),manifest['narration']['sha256'],100_000_000)
        for clip in clips:checked_file(root,str(clip['path']),clip['sha256'])
        encoded.replace(output)
    write_srt(output.with_suffix('.srt'),captions)
    result={'local_path':str(output),'sha256':digest(output.read_bytes()),'duration_seconds':duration,'encoded_duration_seconds':float(info['format']['duration']),'width':w,'height':h,'fps':fps,'synthetic':True,'provider_live_verified':False,'approval':'DRAFT','reconstructed_candidate':True,'motion_type':'synthetic moving-video integration, not live-provider animation proof','base_style_sha256':BASE_SHA,'motion_style_sha256':manifest['motion_contract']['compiled_sha256'],'compositor_sha256':digest(Path(__file__).read_bytes()),'script_sha256':manifest['script_sha256'],'narration_sha256':manifest['narration']['sha256'],'narration_text':manifest['narration']['exactText'],'synthesis_passes_this_composition':0,'narration_reused':True,'provider_audio_muted':True,'loop':False,'freeze_extension':False,'retime':False,'source_order_preserved':True,'scene_starts':starts,'crossfade_seconds':overlap,'clip_sha256':[c['sha256'] for c in clips],'input_manifest':manifest,'captions':[asdict(c) for c in captions],'elapsed_seconds':round(time.monotonic()-started,2),'source_semantic_motion_review':'Synthetic plumbing does not establish human acceptance of real provider motion.'}
    result['presentation_crops']=[c.get('presentation_crop') for c in clips]
    result.update(provenance)
    output.with_suffix('.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8');return result



def presentation():
    path=Path(__file__).resolve().parent.parent/'style-guide/portrait-candidate/presentation.json'
    require(digest(path.read_bytes())==PRESENTATION_SHA,'Portrait candidate presentation changed.')
    require(digest(Path(__file__).with_name('engine.py').read_bytes())==HELPER_ENGINE_SHA,'Shared exact-caption implementation changed.')
    release=json.loads(path.with_name('release.json').read_text())
    require(release.get('status')=='DRAFT_RELEASE_CANDIDATE' and release.get('human_approved') is False
            and release.get('presentation_sha256')==PRESENTATION_SHA
            and release.get('compositor_sha256')==digest(Path(__file__).read_bytes())
            and release.get('helper_engine_sha256')==HELPER_ENGINE_SHA
            and release.get('continuation_worker_sha256')==digest(Path(__file__).with_name('continuation_worker.py').read_bytes()),'Portrait release source lock changed.')
    result=json.loads(path.read_text())
    require(result['canvas']=={'aspect':'9:16','colour_space':'bt709','fps':24,'height':1920,'width':1080},'Portrait geometry changed.')
    return result


def release_bindings():
    """Independent hashes to freeze in the trusted plan before accepting a job."""
    presentation()  # Verify source/presentation/helper byte locks first.
    release_path=Path(__file__).resolve().parent.parent/'style-guide/portrait-candidate/release.json'
    from .motion_originals import load_policy
    return {'visual_style_sha256':fingerprint(json.loads(release_path.read_text())),
            'motion_policy_sha256':load_policy()['source_sha256']}


def validate_candidate(staged,frozen,*,report,expected_report_sha256,clip_receipts,expected_ffmpeg_version,continuation_context=None,expected_continuation_context_sha256=None):
    """Inputs are independently persisted server receipts, not browser claims.

    expected_report_sha256 and clip_receipts must be loaded from the committed
    operation journal. This validator cannot create that provenance itself.
    """
    style=presentation();root=Path(staged['root'])
    bindings=release_bindings()
    require(all(frozen.get('processing_plan',{}).get(k)==v for k,v in bindings.items()),
            'Frozen visual/motion release differs from this portrait candidate.')
    require(staged['snapshot_sha256']==fingerprint(frozen),'Candidate differs from the frozen job.')
    require(fingerprint(report)==expected_report_sha256 and report.get('status')=='DRAFT_RELEASE_CANDIDATE'
            and report.get('provider')=='elevenlabs' and report.get('media_verified') is True
            and report.get('provider_receipts_verified') is True and report.get('live_release') is False
            and report.get('human_approved') is False,'Verified distinct ElevenLabs release report required.')
    if report.get('synthetic') is not True:
        require(importlib.metadata.version('pillow')=='13.0.0' and importlib.metadata.version('numpy')=='2.5.4',
                'Real media candidate requires the recovered pinned Pillow/numpy runtime.')
    text=report['exact_text'];duration=report['duration_seconds']
    provenance=report.get('provenance',{})
    require(provenance.get('owner_job_sha256')==digest((staged['owner_id']+'\0'+staged['job_id']).encode())
            and provenance.get('exact_text_sha256')==frozen['narrated_text_sha256']
            and provenance.get('profile_sha256')==fingerprint(frozen['processing_plan']['narration_profile'])
            and provenance.get('plan_sha256')==fingerprint(plan_requests(text,frozen['processing_plan']['narration_profile'])),
            'Narration provenance belongs to another owner, job or deterministic request plan.')
    require(text==frozen['narrated_text'] and digest(text.encode())==frozen['narrated_text_sha256'],'Exact narration differs from frozen wording.')
    require(report['profile_sha256']==fingerprint(frozen['processing_plan']['narration_profile'])
            and report['profile']==frozen['processing_plan']['narration_profile'],'Narration profile differs from selected frozen voice.')
    require(report['ffmpeg_version']==expected_ffmpeg_version
            and run(['ffmpeg','-version']).stdout.decode().splitlines()[0].split()[2]==expected_ffmpeg_version,'FFmpeg differs from the reviewed release.')
    require(type(duration) in (int,float) and math.isfinite(duration) and 0<duration<=900,'Invalid narration duration.')
    audio=checked_file(root,str(staged['files']['narration.wav']),report['audio_sha256'],100_000_000)
    measured=probe(audio,video=False)
    require(abs(measured['duration']-duration)<=.001,'Narration duration changed; do not trim or extend.')
    words=validate_words(text,report['words'],duration)
    composition=json.loads(Path(staged['files']['composition.json']).read_text())
    require(set(composition)=={'schema_version','status','snapshot_sha256','narration_report_sha256','clips'}
            and composition['schema_version']==1 and composition['status']=='DRAFT_RELEASE_CANDIDATE'
            and composition['snapshot_sha256']==staged['snapshot_sha256']
            and composition['narration_report_sha256']==expected_report_sha256,'Composition is not bound to the server narration/job release.')
    planned=composition['clips'];images=frozen['images'];order=frozen['input_order']
    require(4<=len(images)<=5 and len(planned)==len(images)==len(clip_receipts)
            and order==[x['id'] for x in images],'Four or five exact ordered images are required.')
    expected_models=['minimax/h3-max-turbo/image-to-video']*len(images)
    if continuation_context is not None:
        from .continuation_worker import validate_continuation_context
        expected_models=validate_continuation_context(continuation_context,expected_continuation_context_sha256,
            staged=staged,frozen=frozen,clip_receipts=clip_receipts,expected_report_sha256=expected_report_sha256)
    else:
        require(expected_continuation_context_sha256 is None,'Continuation hash requires its independently loaded full context.')
    checked=[];starts=[0.];seen=set();overlap=style['transitions']['crossfade_seconds']
    for index,(item,image,receipt) in enumerate(zip(planned,images,clip_receipts)):
        require(set(item)=={'image_id','trim_length','presentation_crop'} and item['image_id']==image['id'],'Composition changed numbered image order.')
        require(isinstance(receipt,dict) and receipt.get('image_id')==image['id']
                and receipt.get('source_image_sha256')==image['sha256']
                and receipt.get('provider')=='fal' and receipt.get('model')==expected_models[index]
                and receipt.get('media_verified') is True and receipt.get('no_invented_action_reviewed') is True
                and type(receipt.get('synthetic')) is bool and receipt['synthetic'] is report.get('synthetic')
                and receipt.get('presentation_crop')==item['presentation_crop'],'Independent source-bound clip receipt/review required.')
        require(all(isinstance(receipt.get(k),str) and receipt[k] for k in ('request_id','operation_id')),'Exact operation provenance required.')
        sha=receipt['sha256'];require(sha not in seen,'Never loop/reuse the same clip bytes.');seen.add(sha)
        path=checked_file(root,str(staged['files'][f'clip-{index+1:02}.mp4']),sha)
        media=probe(path,video=True,presentation_crop=item['presentation_crop'])
        length=item['trim_length']
        require(type(length) in (int,float) and math.isfinite(length) and overlap*2<length<=media['duration']+.0001,'Insufficient continuous native footage; no freeze/loop/retime.')
        checked.append(dict(path=path,sha256=sha,trim_start=0,trim_length=length,presentation_crop=item['presentation_crop']))
        if index<len(images)-1:starts.append(starts[-1]+length-overlap)
    require(abs(starts[-1]+checked[-1]['trim_length']-duration)<.001,'Unique footage does not match narration duration.')
    captions=make_captions(text,words,style=style)
    require(re.sub(r'\s+','',''.join(c.text for c in captions))==re.sub(r'\s+','',text),'Caption wording changed.')
    font=_font(round(style['canvas']['height']*style['captions']['font_height_fraction']),style)
    for c in captions:wrap_caption(c.text,font,round(style['canvas']['width']*style['captions']['safe_width_fraction']))
    return style,audio,checked,captions,starts,duration


def compose_candidate(staged,frozen,output,*,report,expected_report_sha256,clip_receipts,expected_ffmpeg_version,continuation_context=None,expected_continuation_context_sha256=None):
    validated=validate_candidate(staged,frozen,report=report,expected_report_sha256=expected_report_sha256,
        clip_receipts=clip_receipts,expected_ffmpeg_version=expected_ffmpeg_version,continuation_context=continuation_context,
        expected_continuation_context_sha256=expected_continuation_context_sha256)
    manifest={'motion_contract':{'compiled_sha256':fingerprint({'presentation_sha256':PRESENTATION_SHA,
                  'compositor_sha256':digest(Path(__file__).read_bytes()),'narration_report_sha256':expected_report_sha256})},
              'script_sha256':frozen['narrated_text_sha256'],
              'narration':{'sha256':report['audio_sha256'],'exactText':report['exact_text']},
              'source_order':list(frozen['input_order'])}
    return _compose_validated(manifest,staged['root'],output,validated,{
        'status':'DRAFT_RELEASE_CANDIDATE','approval':'DRAFT','human_approved':False,
        'synthetic':report.get('synthetic') is True,'live_provider_verified':False,
        'provider_api_integration_verified':False,'public_api_activated':False,
        'narration_provider':'elevenlabs','narration_profile_sha256':report['profile_sha256'],
        'runtime_versions':{'pillow':importlib.metadata.version('pillow'),'numpy':importlib.metadata.version('numpy'),'ffmpeg':expected_ffmpeg_version},
        'runtime_matches_recovered_reference':importlib.metadata.version('pillow')=='13.0.0' and importlib.metadata.version('numpy')=='2.5.4',
        'narration_report_sha256':expected_report_sha256,'source_order':list(frozen['input_order']),
        'continuation_context_sha256':expected_continuation_context_sha256,
        'motion_type':'Recovered neutral portrait compositor with a distinct ElevenLabs narration release candidate',
        'source_semantic_motion_review':'Per-clip review remains mandatory; this candidate is not final user style approval.'})


class PortraitCandidateCompositor:
    """Bind only independently loaded operation-journal receipts; no defaults."""
    def __init__(self,release_loader,clip_receipt_loader,expected_ffmpeg_version,continuation_context_loader=None):
        self.release_loader,self.clip_receipt_loader=release_loader,clip_receipt_loader
        self.expected_ffmpeg_version=expected_ffmpeg_version
        self.continuation_context_loader=continuation_context_loader
    def __call__(self,staged,frozen,output):
        report,expected=self.release_loader(staged['snapshot_sha256'])
        receipts=self.clip_receipt_loader(staged['snapshot_sha256'])
        context,context_sha=(self.continuation_context_loader(staged['snapshot_sha256']) if self.continuation_context_loader else (None,None))
        return compose_candidate(staged,frozen,output,report=report,expected_report_sha256=expected,
                                 clip_receipts=receipts,expected_ffmpeg_version=self.expected_ffmpeg_version,continuation_context=context,
                                 expected_continuation_context_sha256=context_sha)
