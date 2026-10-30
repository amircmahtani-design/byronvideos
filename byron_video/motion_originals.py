"""Original-upload ID-compatible motion candidate. Historical policy is unchanged."""
from __future__ import annotations
import argparse
import copy
import hashlib
import itertools
import json
import math
from pathlib import Path
import re
from .elevenlabs_adapter import fingerprint

ROOT = Path(__file__).resolve().parent.parent / 'style-guide' / 'motion-originals'
CATEGORIES = ('environment', 'fabric', 'light', 'posture', 'camera')
CAPABILITIES = ('foliage', 'curtain', 'water', 'clouds', 'fabric', 'existing_light', 'stable_person_pose', 'safe_camera_framing', 'hair', 'eyes')
SUPPORT = {'environment': ('foliage','curtain','water','clouds'), 'fabric': ('fabric',),
           'light': ('existing_light',), 'posture': ('stable_person_pose',), 'camera': ('safe_camera_framing',)}
SHA = re.compile(r'[0-9a-f]{64}\Z')


def require(ok, message):
    if not ok: raise ValueError(message)


def validate_policy(p, root=ROOT):
    require(isinstance(p, dict) and set(p) == {'schema_version','version','status','direction_feedback','final_style_approved',
        'canvas','source_order','primary_categories','secondary_only','diversity','camera','temporal','semantic',
        'source_review','anchors','approval','protocol_sha256','implementation_sha256'}, 'Motion policy settings are missing or unknown.')
    require(type(p['schema_version']) is int and p['schema_version']==1 and p['status']=='DRAFT'
            and p['final_style_approved'] is False and isinstance(p['version'],str) and re.fullmatch(r'motion-direction-draft-\d+\.\d+\.\d+',p['version']), 'Only an explicitly unapproved direction draft is supported.')
    require(p['direction_feedback']=='varied_source_supported_motion_encouraged_not_final_style_approval', 'Do not infer final approval from direction feedback.')
    require(p['canvas']=={'aspect':'9:16','fit':'full_bleed','preserve_original_bytes':True}
            and p['canvas']['preserve_original_bytes'] is True and p['source_order']=='explicit_numbered_upload_order', 'Portrait canvas and original numbered order must be preserved.')
    require(p['primary_categories']==list(CATEGORIES) and p['secondary_only']==['hair','natural_blink'], 'Unsupported primary/secondary motion catalogue.')
    require(p['diversity']=={'policy':'minimize_adjacent_repetition_then_maximize_categories','source_limited':'flag_for_review_never_invent'}, 'Unsupported diversity rule.')
    require(p['camera']=={'maximum_scale_change_fraction':.03,'guarantee':'measure_generated_output_not_prompt'}
            and type(p['camera']['maximum_scale_change_fraction']) in (int,float), 'Camera bound must remain explicit and measured.')
    t=p['temporal'];require(isinstance(t,dict) and set(t)=={'min_scene_seconds','max_scene_seconds','loop','freeze','retime','timing_source'}, 'Incomplete temporal policy.')
    require(type(t['min_scene_seconds']) in (int,float) and type(t['max_scene_seconds']) in (int,float)
            and t['min_scene_seconds']==4 and t['max_scene_seconds']==60 and all(t[x] is False for x in ('loop','freeze','retime'))
            and t['timing_source']=='existing_exact_narration_story_beat_plan', 'No loops, freezes, retiming or arbitrary scene duration.')
    require(p['semantic']=={'invented_story_actions':False,'pose_preserving_only':True,'repeated_blink_cycle':False,'prolonged_eye_closure':False}
            and all(type(v) is bool for v in p['semantic'].values()), 'Semantic and eye-behavior restrictions are required.')
    require(p['source_review']=={'provenance':'local_human_source_review','external_vision_upload':False,'missing_capabilities':'fail_before_generation'}
            and p['source_review']['external_vision_upload'] is False, 'No implicit vision disclosure or guessed capabilities.')
    require(p['anchors']=='same_original_start_end_where_provider_supports' and p['approval']=='full_clip_and_film_human_review_required', 'Anchors and actual-output review are required.')
    root=Path(root)
    for key,path in [('protocol_sha256',root/'PROTOCOL.md'),('implementation_sha256',Path(__file__))]:
        require(isinstance(p[key],str) and SHA.fullmatch(p[key]) and hashlib.sha256(path.read_bytes()).hexdigest()==p[key], 'Motion protocol or implementation fingerprint changed.')
    return p


def compile_policy(root=ROOT, *, check=False):
    root=Path(root);p=validate_policy(json.loads((root/'motion.source.json').read_text()),root)
    result={'schema_version':1,'source_sha256':fingerprint(p),'policy':p}
    data=json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()+b'\n'
    path=root/'motion.runtime.json'
    if check:require(path.is_file() and path.read_bytes()==data, 'Compiled motion policy is stale.')
    else:path.write_bytes(data)
    return result


def load_policy(root=ROOT):
    return compile_policy(root,check=True)


def plan_motion(images, narration_text_sha256, *, timing_plan, policy=None):
    runtime=load_policy() if policy is None else copy.deepcopy(policy)
    require(isinstance(runtime,dict) and set(runtime)=={'schema_version','source_sha256','policy'}
            and type(runtime['schema_version']) is int and runtime['schema_version']==1, 'Invalid runtime motion snapshot.')
    p=validate_policy(runtime['policy']);require(runtime['source_sha256']==fingerprint(p),'Motion snapshot hash mismatch.')
    require(isinstance(narration_text_sha256,str) and SHA.fullmatch(narration_text_sha256),'Exact narration-text fingerprint required.')
    require(isinstance(images,list) and 1<=len(images)<=5,'Use one to five existing ordered scenes (short previews may use fewer than four).')
    timing = validate_timing_plan(timing_plan, images, narration_text_sha256)
    ids=set();options=[]
    for position,item in enumerate(images,1):
        require(isinstance(item,dict) and set(item)=={'id','position','source_sha256','duration_seconds','source_review'}, 'Each scene needs its original hash, unchanged duration and reviewed source features.')
        require(isinstance(item['id'],str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',item['id']) and item['id'] not in ids,'Unique safe image IDs required.');ids.add(item['id'])
        require(type(item['position']) is int and item['position']==position,'Never reorder the numbered source images.')
        require(isinstance(item['source_sha256'],str) and SHA.fullmatch(item['source_sha256']),'Original image hash required.')
        d=item['duration_seconds'];require(type(d) in (int,float) and math.isfinite(d) and p['temporal']['min_scene_seconds']<=d<=p['temporal']['max_scene_seconds'],'Scene duration is outside the separate technical range; do not loop or retime.')
        review=item['source_review']
        require(isinstance(review,dict) and set(review)=={'provenance','source_sha256','reference_id','capabilities'},'Source-visible feature review is missing.')
        require(review['provenance']==p['source_review']['provenance'] and review['source_sha256']==item['source_sha256']
                and isinstance(review['reference_id'],str) and re.fullmatch(r'[A-Za-z0-9_-]{3,160}',review['reference_id']),'Feature review must bind the exact source; never fetch a URL.')
        caps=review['capabilities'];require(isinstance(caps,list) and all(isinstance(c,str) and c in CAPABILITIES for c in caps) and len(caps)==len(set(caps)),'Unknown or duplicate source capability.')
        available=[category for category in CATEGORIES if set(SUPPORT[category]) & set(caps)]
        require(available,'No source-supported primary motion; hair/blink alone is not an adequate plan.')
        options.append(available)
    # At most5^5 alternatives. Source ordering and durations are never changed.
    def score(choices):
        return (sum(a==b for a,b in zip(choices,choices[1:])), -len(set(choices)), tuple(CATEGORIES.index(x) for x in choices))
    selected=min(itertools.product(*options),key=score)
    scenes=[]
    for item,category in zip(images,selected):
        caps=item['source_review']['capabilities'];support=next(c for c in SUPPORT[category] if c in caps)
        secondary=[]
        if 'hair' in caps:secondary.append('hair')
        if 'eyes' in caps:secondary.append('natural_blink')
        scenes.append({'image_id':item['id'],'position':item['position'],'source_sha256':item['source_sha256'],
                       'duration_seconds':item['duration_seconds'],'primary_category':category,'visible_support':support,
                       'secondary_details':secondary,'intensity':'subtle_pose_preserving','semantic_action':'none',
                       'loop':False,'freeze':False,'retime':False,'requires_actual_clip_review':True})
    for scene in scenes:
        scene['prompt'] = motion_prompt(scene, p)
    result={'schema_version':1,'status':'DRAFT_REVIEW_REQUIRED','final_style_approved':False,
            'policy_version':p['version'],'policy_sha256':runtime['source_sha256'],'policy_snapshot':copy.deepcopy(runtime),'narration_text_sha256':narration_text_sha256,
            'timing_plan_snapshot':copy.deepcopy(timing),'timing_plan_sha256':fingerprint(timing),
            'timing_verification':'upstream_snapshot_bound_not_audio_remeasured_here',
            'source_inputs':copy.deepcopy(images),'input_order':[i['id'] for i in images],'scenes':scenes,
            'diversity_state':'varied' if len(set(selected))>1 else ('single_scene' if len(images)==1 else 'source_limited_review_required'),
            'provider_submission_authorized':False,'external_vision_calls':0}
    result['sha256']=fingerprint(result)
    return result


def validate_timing_plan(timing, images, text_sha256):
    """Bind upstream timing, never claim to have decoded the referenced audio here."""
    require(isinstance(timing,dict) and set(timing)=={'schema_version','source','narration_text_sha256','audio_sha256',
        'word_timing_sha256','duration_seconds','crossfade_seconds','scenes'}, 'Complete upstream narration timing snapshot required.')
    require(type(timing['schema_version']) is int and timing['schema_version']==1 and timing['source']=='exact_narration_word_times'
            and timing['narration_text_sha256']==text_sha256,'Timing source or exact text differs.')
    require(all(isinstance(timing[k],str) and SHA.fullmatch(timing[k]) for k in ('audio_sha256','word_timing_sha256')), 'Timing must bind original audio and word-time fingerprints.')
    duration=timing['duration_seconds'];overlap=timing['crossfade_seconds']
    require(type(duration) in (int,float) and math.isfinite(duration) and 0<duration<=900
            and type(overlap) in (int,float) and overlap==.7, 'Invalid duration or transition in upstream timing.')
    scenes=timing['scenes'];require(isinstance(scenes,list) and len(scenes)==len(images),'Timing scenes do not match the selected photos.')
    previous_end=None
    for image,scene in zip(images,scenes):
        require(isinstance(image,dict) and isinstance(scene,dict) and set(scene)=={'image_id','start_seconds','end_seconds'}
                and scene['image_id']==image.get('id'),'Timing changed image order.')
        a,b=scene['start_seconds'],scene['end_seconds']
        require(all(type(x) in (int,float) and math.isfinite(x) for x in (a,b)) and 0<=a<b<=duration
                and (a==0 if previous_end is None else abs(a-(previous_end-overlap))<1e-6), 'Scene timing has a gap, wrong overlap or reordered interval.')
        require(type(image.get('duration_seconds')) in (int,float) and abs((b-a)-image['duration_seconds'])<1e-6,
                'Motion scene duration differs from trusted narration timing.')
        previous_end=b
    require(previous_end is not None and abs(previous_end-duration)<1e-6,'Timing omits the end of narration.')
    return copy.deepcopy(timing)


def motion_prompt(scene, policy):
    """Only catalogue phrases enter provider prompts; never script/URL instructions."""
    descriptions={
        'foliage':'a faint natural movement of foliage already visible',
        'curtain':'a small drift of the existing visible curtain',
        'water':'gentle movement of the existing visible water surface',
        'clouds':'a slow natural drift of clouds already visible',
        'fabric':'a restrained settling of visible fabric without changing the pose',
        'existing_light':'a very slight natural fluctuation from the existing visible light, preserving palette and exposure',
        'stable_person_pose':'subtle breathing or a tiny natural posture settling, retaining the original silhouette and contact',
        'safe_camera_framing':'one restrained continuous cinematic camera drift within the safe framing, with at most 3 percent scale change',
    }
    primary=descriptions[scene['visible_support']]
    secondary=[]
    if 'hair' in scene['secondary_details']:secondary.append('hair may move faintly as a secondary detail, not the main action')
    if 'natural_blink' in scene['secondary_details']:secondary.append('any blink is brief and incidental, never repeated cyclically or held closed')
    camera='Keep the camera stable; no zoom or reframing.' if scene['primary_category']!='camera' else 'Protect faces and hands; verify the actual camera bound after generation.'
    return ('Create subtle continuous motion from the original image: '+primary+'. '+camera+' '
            +' '.join(secondary)+'. Preserve identity, expression, existing pose, relationships and original composition. '
            +'Do not add a kiss, embrace, reaching, walking, speaking, objects or other story action. '
            +'No repetitive blink, prolonged eye closure, repeated motion cycle, loop, freeze or speed change. '
            +'Only animate what is already visible; no generative repaint of unrelated details.')


def validate_plan(plan, images, narration_text_sha256, timing_plan):
    require(isinstance(plan,dict),'Motion plan is required.')
    require(fingerprint(timing_plan)==plan['timing_plan_sha256'], 'Motion plan differs from the independently supplied narration timing snapshot.')
    expected=plan_motion(plan['source_inputs'],narration_text_sha256,timing_plan=timing_plan,policy=plan['policy_snapshot'])
    require(fingerprint(plan)==fingerprint(expected),'Motion plan changed after compilation.')
    require([(x['id'],x['position'],x['sha256']) for x in images]==
            [(x['id'],x['position'],x['source_sha256']) for x in plan['source_inputs']], 'Motion plan changed selected image order or bytes.')
    return copy.deepcopy(plan)


def main():
    parser=argparse.ArgumentParser(description='Compile/check unapproved local motion direction; no provider work.')
    parser.add_argument('--check',action='store_true');args=parser.parse_args()
    r=compile_policy(check=args.check);print(r['policy']['version'],r['policy']['status'],r['source_sha256'])

if __name__=='__main__':main()
