"""Public-safe synthetic fixtures only. No provider calls or real voice IDs."""
import base64
import copy
import json
import pytest
from byron_video.elevenlabs_adapter import (alignment_words, fingerprint, live_transport,
    parse_response, plan_requests, validate_profile)
from byron_video.hosted_contract import freeze_job, hosted_readiness
from byron_video.worker_journal import Journal, execute_fixture_once


def profile():
    return {'schema_version': 1, 'version': 'synthetic-profile-1', 'status': 'DRAFT',
            'provider': 'elevenlabs', 'voice_id': 'SYNTHETIC_TEST_VOICE', 'model_id': 'eleven_multilingual_v2',
            'voice_settings': {'stability': .5, 'similarity_boost': .75, 'style': 0,
                               'use_speaker_boost': True, 'speed': .93},
            'output_format': 'mp3_44100_128', 'seed': 1, 'apply_text_normalization': 'off'}


def alignment(text):
    return {'characters': list(text), 'character_start_times_seconds': [i/10 for i in range(len(text))],
            'character_end_times_seconds': [(i+1)/10 for i in range(len(text))]}


def test_ordered_whole_paste_and_five_images_no_automatic_sort():
    raw = 'Synthetic title\nBy Example Writer\n[example.test](https://example.test/a)\n\n' + 'Exact words. ' * 250
    images = [{'id': name, 'position': i, 'sha256': str(i)*64} for i, name in enumerate(['z','a','x','c','b'], 1)]
    p = profile()
    plan = freeze_job(raw, images, p, 'a'*64)
    assert plan['input_order'] == ['z','a','x','c','b']
    assert plan['raw_script'] == raw and len(raw.split()) > 449
    assert 'https://example.test/a' not in plan['narrated_text']
    assert 'example.test' in plan['narrated_text']
    images[0]['id'] = 'mutated'; p['voice_settings']['speed'] = 1
    assert plan['input_order'][0] == 'z' and plan['narration_profile']['voice_settings']['speed'] == .93
    result_hash = plan.pop('sha256')
    assert result_hash == fingerprint(plan)


@pytest.mark.parametrize('count', [0,3,6])
def test_image_count_failures(count):
    with pytest.raises(ValueError):
        freeze_job('Exact text.', [{'id':str(i),'position':i+1,'sha256':'a'*64} for i in range(count)], profile(), 'a'*64)


def test_order_and_bool_positions_fail_closed():
    images = [{'id':str(i),'position':i+1,'sha256':'a'*64} for i in range(5)]
    images[0]['position'] = True
    with pytest.raises(ValueError, match='order'):
        freeze_job('Exact text.', images, profile(), 'a'*64)


def test_exact_16000_characters_two_preplanned_requests_not_retry():
    text = 'abcdefg ' * 2000
    p = profile(); before = copy.deepcopy(p)
    requests = plan_requests(text, p)
    assert len(text) == 16000 and len(requests) == 2
    assert ''.join(r['body']['text'] for r in requests) == text
    assert all(len(r['body']['text']) <= 10000 for r in requests)
    assert p == before and all(r['body']['voice_settings']['speed'] == .93 for r in requests)
    assert requests[0]['char_end'] == requests[1]['char_start']
    assert requests[0]['body']['next_text'] == requests[1]['body']['text'][:1000]


def test_short_script_single_request_and_no_alternate_voice():
    reqs = plan_requests('“Keep every word,” she said.\n', profile())
    assert len(reqs) == 1 and reqs[0]['body']['text'] == '“Keep every word,” she said.\n'
    assert reqs[0]['url'].startswith('https://api.elevenlabs.io/v1/text-to-speech/SYNTHETIC_TEST_VOICE/with-timestamps?')
    req = copy.deepcopy(reqs[0]); digest = req.pop('sha256')
    assert digest == fingerprint(req)


@pytest.mark.parametrize('mutate', [lambda p:p.update(voice_id=None), lambda p:p.update(status='APPROVED'),
 lambda p:p.update(schema_version=True), lambda p:p['voice_settings'].update(speed=True),
 lambda p:p['voice_settings'].update(stability=float('nan')), lambda p:p.update(apply_text_normalization='auto'),
 lambda p:p.update(voice_id='https://evil.test'), lambda p:p.update(seed=True), lambda p:p.update(model_id='unknown')])
def test_invalid_profiles(mutate):
    p = profile(); mutate(p)
    with pytest.raises(ValueError): validate_profile(p)


def test_voice_must_be_selected_template_is_not_ready():
    with open('hosted/narration-profile.draft.json') as file:
        p = json.load(file)
    with pytest.raises(ValueError, match='voice ID'):
        validate_profile(p)
    assert hosted_readiness()['ready'] is False
    with pytest.raises(RuntimeError, match='not activated'):
        live_transport()


def test_original_character_alignment_preserves_unicode_punctuation_spans():
    text = ' “Café,” he said.\n'
    words = alignment_words(text, alignment(text))
    assert [w['text'] for w in words] == ['“Café,”', 'he', 'said.']
    assert all(text[w['char_start']:w['char_end']] == w['text'] for w in words)


@pytest.mark.parametrize('mutate', [lambda a:a['characters'].pop(),
 lambda a:a['character_start_times_seconds'].__setitem__(0, True),
 lambda a:a['character_end_times_seconds'].__setitem__(1, float('nan')),
 lambda a:a['character_end_times_seconds'].__setitem__(0, -1),
 lambda a:a['character_start_times_seconds'].__setitem__(3, 0)])
def test_malformed_alignment(mutate):
    a = alignment('Exact text.'); mutate(a)
    with pytest.raises(ValueError): alignment_words('Exact text.', a)


def test_normalized_alignment_not_substituted_for_missing_original():
    data = {'audio_base64': base64.b64encode(b'not-real-mp3-fixture').decode(), 'normalized_alignment': alignment('Changed words.')}
    with pytest.raises(ValueError, match='Original-text'):
        parse_response('Exact words.', json.dumps(data).encode(), 'synthetic-request-1')


def test_response_bytes_remain_unverified_until_real_decode():
    text = 'Exact words.'
    data = {'audio_base64': base64.b64encode(b'not-real-mp3-fixture').decode(), 'alignment': alignment(text)}
    result = parse_response(text, json.dumps(data).encode(), 'synthetic-request-1')
    assert result['media_verified'] is False and result['exact_text'] == text
    assert result['audio_bytes'] == b'not-real-mp3-fixture'


def test_journal_restart_unknown_never_resends_and_cap_cannot_change(tmp_path):
    request = plan_requests('Exact text.', profile())[0]
    p = tmp_path/'job'/'journal.sqlite'
    journal = Journal(p)
    journal.arm('operation-1', request, 10, 20); journal.close()
    journal = Journal(p)
    assert journal.get('operation-1')['state'] == 'may_have_submitted'
    with pytest.raises(ValueError, match='already exists'): journal.arm('operation-1', request, 10, 20)
    with pytest.raises(ValueError, match='cannot change'): journal.arm('operation-2', request, 10, 100)
    journal.arm('operation-2', {**request, 'sequence':2}, 10, 20)
    with pytest.raises(ValueError, match='exceed'): journal.arm('operation-3', {**request, 'sequence':3}, 1, 20)
    journal.close()


def test_journal_one_fixture_transport_call_and_no_second_submission(tmp_path):
    journal = Journal(tmp_path/'job.sqlite'); calls = []
    def fixture(request):
        calls.append(request)
        return {'request_id':'synthetic-request-123', 'synthetic':True}
    fixture.synthetic_fixture = True
    request = plan_requests('Exact text.', profile())[0]
    execute_fixture_once(journal, 'operation-1', request, 5, 10, fixture)
    assert journal.get('operation-1')['state'] == 'received'
    with pytest.raises(ValueError): execute_fixture_once(journal, 'operation-1', request, 5, 10, fixture)
    assert len(calls) == 1
    with pytest.raises(ValueError, match='synthetic'): execute_fixture_once(journal, 'operation-2', request, 5, 10, lambda _: {})
    journal.close()


def test_transport_interruption_remains_unknown(tmp_path):
    journal = Journal(tmp_path/'job.sqlite')
    def fixture(_): raise TimeoutError('synthetic uncertain outcome')
    fixture.synthetic_fixture = True
    with pytest.raises(TimeoutError): execute_fixture_once(journal, 'operation-1', {}, 5, 10, fixture)
    assert journal.get('operation-1')['state'] == 'may_have_submitted'
    journal.close()


def test_journal_receipt_plan_binding(tmp_path):
    journal = Journal(tmp_path/'job.sqlite'); journal.arm('operation-1', {'a':1}, 1, 1)
    with pytest.raises(ValueError): journal.record('operation-1', {'a':2}, {'request_id':'synthetic-request'})
    with pytest.raises(ValueError): journal.record('operation-1', {'a':1}, {'request_id':True})
    assert journal.get('operation-1')['state'] == 'may_have_submitted'
    journal.close()


def test_same_request_different_id_and_duplicate_provider_id_rejected(tmp_path):
    j = Journal(tmp_path/'job.sqlite')
    j.arm('operation-1', {'sequence':1}, 1, 3)
    with pytest.raises(ValueError, match='exact request'): j.arm('operation-2', {'sequence':1}, 1, 3)
    j.record('operation-1', {'sequence':1}, {'request_id':'synthetic-request-1'})
    j.arm('operation-2', {'sequence':2}, 1, 3)
    with pytest.raises(ValueError, match='already belongs'): j.record('operation-2', {'sequence':2}, {'request_id':'synthetic-request-1'})
    assert j.get('operation-2')['state'] == 'may_have_submitted'
    j.close()


def test_whitespace_only_chunk_rejected_before_any_submission():
    with pytest.raises(ValueError, match='all-whitespace'):
        plan_requests('word' + ' '*12000, profile())
