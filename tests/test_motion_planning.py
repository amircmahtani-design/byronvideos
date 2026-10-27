"""Synthetic local feature/timing records only; no vision or provider calls."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import pytest
from byron_video import motion_planning as motion
from byron_video.elevenlabs_adapter import fingerprint
from byron_video.hosted_contract import freeze_job

TEXT='Synthetic wording, unchanged.'
TEXT_SHA=hashlib.sha256(TEXT.encode()).hexdigest()


def images(capabilities):
    return [{'id':f'image-{i}','position':i,'source_sha256':str(i)*64,'duration_seconds':10,
             'source_review':{'provenance':'local_human_source_review','source_sha256':str(i)*64,
                              'reference_id':f'synthetic-local-review-{i}','capabilities':list(caps)}}
            for i,caps in enumerate(capabilities,1)]


def timing(records):
    scenes=[];start=0
    for image in records:
        end=start+image['duration_seconds'];scenes.append({'image_id':image['id'],'start_seconds':start,'end_seconds':end});start=end-.7
    return {'schema_version':1,'source':'exact_narration_word_times','narration_text_sha256':TEXT_SHA,
            'audio_sha256':'a'*64,'word_timing_sha256':'b'*64,'duration_seconds':scenes[-1]['end_seconds'],
            'crossfade_seconds':.7,'scenes':scenes}


def build(records):
    return motion.plan_motion(records,TEXT_SHA,timing_plan=timing(records))


def test_runtime_is_compiled_draft_not_final_approval():
    result=motion.compile_policy(check=True)
    assert result['source_sha256']==fingerprint(result['policy'])
    assert result['policy']['status']=='DRAFT' and result['policy']['final_style_approved'] is False
    assert result==motion.load_policy()


def test_diversity_uses_visible_features_and_does_not_reorder_or_retime():
    records=images([['foliage','hair'],['fabric','eyes'],['existing_light'],['stable_person_pose'],['safe_camera_framing']])
    original=copy.deepcopy(records);plan=build(records)
    assert [s['primary_category'] for s in plan['scenes']]==['environment','fabric','light','posture','camera']
    assert plan['input_order']==[i['id'] for i in records]
    assert [s['duration_seconds'] for s in plan['scenes']]==[10]*5
    assert records==original and plan['diversity_state']=='varied'
    assert all(s['semantic_action']=='none' and not s['loop'] and not s['freeze'] and not s['retime'] for s in plan['scenes'])
    assert plan['provider_submission_authorized'] is False and plan['external_vision_calls']==0
    assert plan['status']=='DRAFT_REVIEW_REQUIRED' and plan['final_style_approved'] is False
    assert all('Do not add a kiss' in s['prompt'] and 'prolonged eye closure' in s['prompt'] for s in plan['scenes'])
    assert plan['policy_snapshot']==motion.load_policy()


def test_planner_looks_ahead_instead_of_forcing_adjacent_repetition():
    plan=build(images([['foliage','fabric'],['foliage']]))
    assert [s['primary_category'] for s in plan['scenes']]==['fabric','environment']
    assert plan==build(images([['foliage','fabric'],['foliage']]))


def test_hair_blink_only_fails_instead_of_becoming_repeated_template():
    with pytest.raises(ValueError,match='primary motion'):
        build(images([['hair','eyes']]))


def test_limited_source_variety_is_flagged_not_invented():
    plan=build(images([['fabric'],['fabric'],['fabric']]))
    assert plan['diversity_state']=='source_limited_review_required'
    assert {s['primary_category'] for s in plan['scenes']}=={'fabric'}
    assert all(s['secondary_details']==[] for s in plan['scenes'])
    assert all(s['requires_actual_clip_review'] for s in plan['scenes'])


def test_source_review_copy_is_not_mutable_from_caller():
    records=images([['fabric','hair']]);plan=build(records);records[0]['source_review']['capabilities'].append('water')
    assert plan['source_inputs'][0]['source_review']['capabilities']==['fabric','hair']


@pytest.mark.parametrize('mutate',[
 lambda x:x[0].update(position=True),lambda x:x[0].update(position=2),
 lambda x:x[0].update(source_sha256='c'*64),
 lambda x:x[0]['source_review'].update(provenance='external_vision_service'),
 lambda x:x[0]['source_review'].update(reference_id='https://example.test/private'),
 lambda x:x[0]['source_review'].update(capabilities=['new_kiss']),
 lambda x:x[0]['source_review'].update(capabilities=['fabric','fabric']),
 lambda x:x[0]['source_review'].update(capabilities=[{}]),
 lambda x:x[0].update(duration_seconds=True),lambda x:x[0].update(duration_seconds=61),
 lambda x:x[0].update(duration_seconds=3),lambda x:x[0].update(id='../escape'),
])
def test_invalid_scene_inputs_fail_before_any_generation(mutate):
    records=images([['fabric']]);t=timing(records);mutate(records)
    with pytest.raises(ValueError):motion.plan_motion(records,TEXT_SHA,timing_plan=t)


@pytest.mark.parametrize('mutate',[
 lambda p:p.update(status='APPROVED'),lambda p:p.update(final_style_approved=True),
 lambda p:p['temporal'].update(loop=True),lambda p:p['semantic'].update(invented_story_actions=True),
 lambda p:p['source_review'].update(external_vision_upload=True),lambda p:p.pop('camera'),
 lambda p:p.update(secondary_only=['hair']),lambda p:p['canvas'].update(fit='contain'),
])
def test_policy_cannot_weaken_direction_guards(mutate):
    runtime=motion.load_policy();mutate(runtime['policy']);runtime['source_sha256']=fingerprint(runtime['policy'])
    records=images([['fabric']])
    with pytest.raises(ValueError):motion.plan_motion(records,TEXT_SHA,timing_plan=timing(records),policy=runtime)


def test_tampered_protocol_and_stale_runtime_fail(tmp_path):
    for name in ('PROTOCOL.md','motion.source.json','motion.runtime.json'):
        shutil.copyfile(motion.ROOT/name,tmp_path/name)
    assert motion.compile_policy(tmp_path,check=True)
    (tmp_path/'PROTOCOL.md').write_text('Changed prose without a new lock.')
    with pytest.raises(ValueError,match='fingerprint'):motion.compile_policy(tmp_path,check=True)
    shutil.copyfile(motion.ROOT/'PROTOCOL.md',tmp_path/'PROTOCOL.md')
    (tmp_path/'motion.runtime.json').write_text('{}')
    with pytest.raises(ValueError,match='stale'):motion.compile_policy(tmp_path,check=True)


def test_modified_planner_implementation_invalidates_policy(tmp_path,monkeypatch):
    path=tmp_path/'changed.py';path.write_text('Changed implementation')
    monkeypatch.setattr(motion,'__file__',str(path))
    with pytest.raises(ValueError,match='fingerprint'):motion.load_policy()


def test_independent_timing_snapshot_prevents_replanned_duration_drift():
    records=images([['fabric'],['foliage']]);trusted=timing(records);plan=build(records)
    ordered=[{'id':x['id'],'position':x['position'],'sha256':x['source_sha256']} for x in records]
    assert motion.validate_plan(plan,ordered,TEXT_SHA,trusted)==plan
    changed=copy.deepcopy(records);changed[0]['duration_seconds']=12
    changed_plan=build(changed)
    with pytest.raises(ValueError,match='independently supplied'):motion.validate_plan(changed_plan,ordered,TEXT_SHA,trusted)


@pytest.mark.parametrize('mutate',[
 lambda t:t.update(narration_text_sha256='c'*64),lambda t:t.update(word_timing_sha256='missing'),
 lambda t:t.update(crossfade_seconds=True),lambda t:t['scenes'][0].update(start_seconds=1),
 lambda t:t['scenes'][1].update(start_seconds=10),lambda t:t.update(duration_seconds=100),
])
def test_bad_timing_snapshot(mutate):
    records=images([['fabric'],['foliage']]);t=timing(records);mutate(t)
    with pytest.raises(ValueError):motion.plan_motion(records,TEXT_SHA,timing_plan=t)


def test_hosted_freeze_binds_optional_motion_exact_source_and_timing():
    records=images([['fabric'],['foliage'],['existing_light'],['stable_person_pose']]);plan=build(records);t=timing(records)
    ordered=[{'id':x['id'],'position':x['position'],'sha256':x['source_sha256']} for x in records]
    profile={'schema_version':1,'version':'synthetic-voice-1','status':'DRAFT','provider':'elevenlabs','voice_id':'SYNTHETIC_VOICE',
             'model_id':'eleven_multilingual_v2','voice_settings':{'stability':.5,'similarity_boost':.75,'style':0,'use_speaker_boost':True,'speed':.93},
             'output_format':'mp3_44100_128','seed':1,'apply_text_normalization':'off'}
    job=freeze_job(TEXT,ordered,profile,'d'*64,motion_plan=plan,narration_timing_plan=t)
    assert job['motion_plan']==plan and job['status']=='DRAFT_NOT_ACTIVATED'
    plan['scenes'][0]['prompt']='invent a kiss'
    with pytest.raises(ValueError,match='changed after'):freeze_job(TEXT,ordered,profile,'d'*64,motion_plan=plan,narration_timing_plan=t)
    with pytest.raises(ValueError):freeze_job(TEXT,ordered,profile,'d'*64,motion_plan=build(records))
