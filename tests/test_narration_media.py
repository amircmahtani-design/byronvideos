"""Actual local FFmpeg decoding with synthetic tones, NOT ElevenLabs speech."""
import base64
import copy
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
import pytest
from byron_video.elevenlabs_adapter import parse_response, plan_requests
from byron_video.narration_media import assemble_narration

SETTINGS = {'integrated_lufs':-16, 'true_peak_dbfs':-1, 'lra':11}


def profile():
    return {'schema_version':1, 'version':'synthetic-media-1','status':'DRAFT','provider':'elevenlabs',
            'voice_id':'SYNTHETIC_TEST_VOICE','model_id':'eleven_multilingual_v2',
            'voice_settings':{'stability':.5,'similarity_boost':.75,'style':0,'use_speaker_boost':True,'speed':.93},
            'output_format':'mp3_44100_128','seed':1,'apply_text_normalization':'off'}


@pytest.fixture(scope='module')
def tone(tmp_path_factory):
    if not shutil.which('ffmpeg'):
        if os.environ.get('BYRON_REQUIRE_MEDIA_TESTS') == '1':
            pytest.fail('FFmpeg is required by this release gate; media tests must not be skipped.')
        pytest.skip('FFmpeg unavailable: synthetic media decoding not run.')
    root = tmp_path_factory.mktemp('synthetic-tone'); path=root/'tone.mp3'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=44100:duration=4',
                    '-c:a','libmp3lame','-b:a','128k',str(path)],check=True,capture_output=True)
    version=subprocess.check_output(['ffmpeg','-version']).decode().splitlines()[0].split()[2]
    return path.read_bytes(),version


def inputs(tone, text='Synthetic words, unchanged.'):
    p=profile(); requests=plan_requests(text,p); responses=[]
    for i,r in enumerate(requests):
        text=r['body']['text']; n=len(text)
        alignment={'characters':list(text),'character_start_times_seconds':[j*3/n for j in range(n)],
                   'character_end_times_seconds':[(j+1)*3/n for j in range(n)]}
        responses.append(parse_response(text,json.dumps({'audio_base64':base64.b64encode(tone[0]).decode(),
                          'alignment':alignment}).encode(),f'synthetic-request-{i}'))
    return p,requests,responses


def test_real_decode_sample_exact_join_and_loudness(tone,tmp_path):
    p,requests,responses=inputs(tone,'abcdefg '*2000)
    report=assemble_narration(requests,responses,tmp_path/'job',expected_ffmpeg_version=tone[1],profile=p,audio_settings=SETTINGS,synthetic_fixture=True)
    assert report['exact_text']=='abcdefg '*2000
    assert report['duration_seconds']==8 and report['frames']==192000
    assert report['parts'][1]['offset_frames']==96000
    assert report['words'][1250]['start']==4
    assert report['words'][1250]['char_start']==10000
    assert report['media_verified'] is True and report['live_provider_verified'] is False
    assert report['synthesis_calls']==0 and report['normalization_passes']==1
    assert abs(report['measured_integrated_lufs']+16)<.5
    assert report['measured_true_peak_dbfs']<=-.95
    assert hashlib.sha256((tmp_path/'job/provider-chunk-1.mp3').read_bytes()).digest()==hashlib.sha256(tone[0]).digest()
    assert (tmp_path/'job/narration.json').is_file()
    with pytest.raises(ValueError,match='overwrite'):
        assemble_narration(requests,responses,tmp_path/'job',expected_ffmpeg_version=tone[1],profile=p,audio_settings=SETTINGS,synthetic_fixture=True)


@pytest.mark.parametrize('change', ['duration','order','words','audiohash','profile','native_live'])
def test_media_boundary_failures(tone,tmp_path,change):
    p,requests,responses=inputs(tone)
    live=False
    if change=='duration':responses[0]['words'][-1]['end']=5
    if change=='order':requests[0]['sequence']=2
    if change=='words':responses[0]['words'][0]['text']='Rewritten'
    if change=='audiohash':responses[0]['audio_sha256']='a'*64
    if change=='profile':p['voice_settings']['speed']=1.1
    if change=='native_live':live=True
    with pytest.raises(ValueError):
        assemble_narration(requests,responses,tmp_path/'job',expected_ffmpeg_version=tone[1],profile=p,audio_settings=SETTINGS,synthetic_fixture=not live)
    assert not (tmp_path/'job/narration.json').exists()


def test_disguised_playlist_cannot_be_decoded_as_audio(tone,tmp_path):
    p,requests,responses=inputs(tone)
    data=b"ffconcat version 1.0\nfile '/etc/passwd'\n"
    responses[0]['audio_bytes']=data;responses[0]['audio_sha256']=hashlib.sha256(data).hexdigest()
    with pytest.raises(subprocess.CalledProcessError):
        assemble_narration(requests,responses,tmp_path/'job',expected_ffmpeg_version=tone[1],profile=p,audio_settings=SETTINGS,synthetic_fixture=True)
    assert not (tmp_path/'job/narration.json').exists()


@pytest.mark.parametrize('bad_id', [None, True, '', 'bad/path', 'duplicate'])
def test_request_ids_revalidated_at_media_handoff(tone,tmp_path,bad_id):
    p,requests,responses=inputs(tone,'abcdefg '*2000)
    responses[1]['request_id']=responses[0]['request_id'] if bad_id=='duplicate' else bad_id
    with pytest.raises(ValueError,match='provider request ID'):
        assemble_narration(requests,responses,tmp_path/'job',expected_ffmpeg_version=tone[1],profile=p,audio_settings=SETTINGS,synthetic_fixture=True)
    assert not (tmp_path/'job/narration.json').exists()
