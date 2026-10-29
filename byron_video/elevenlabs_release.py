"""Separate ElevenLabs narration release candidate; historical gates untouched.

No TTS, network or budget mutation occurs here. It loads independently persisted
provider receipts, decodes every audio span and validates exact wording/timing.
The result is DRAFT and requires real provider + human acceptance before release.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
import wave
from .elevenlabs_adapter import MAX_AUDIO_BYTES, fingerprint, plan_requests, require, validate_profile
from .narration_media import RATE, MAX_SECONDS, run, wav_info, loudness_json
from .provider_execution import load_recorded_narration

END_TOLERANCE = 0.05  # Seconds a final word may overrun the decoded audio before it is a real mismatch.
# Storyteller pacing (profile 'pacing': 'sentences'): silence added after each spoken sentence/line.
SENTENCE_PAUSE = 0.45
PARAGRAPH_PAUSE = 1.1


def pause_after(chunk, last, pacing):
    if last or pacing != 'sentences':
        return 0
    return round(RATE * (PARAGRAPH_PAUSE if '\n' in chunk[len(chunk.rstrip()):] else SENTENCE_PAUSE))


def build_narration_candidate(*, owner_id, job_id, text, profile, approval, journal, response_store,
                              expected_profile_sha256, expected_text_sha256, job_root,
                              expected_ffmpeg_version, audio_settings, source_kind):
    """Independent frozen-job fingerprints are mandatory, not derived defaults.

    source_kind must be explicitly synthetic_test for offline tests or
    provider_receipts for independently trusted production receipts. The latter
    does not assert human pronunciation, voice approval or final live readiness.
    """
    require(source_kind in {'synthetic_test','provider_receipts'}, 'Explicit narration provenance kind required.')
    require(fingerprint(validate_profile(profile)) == expected_profile_sha256
            and hashlib.sha256(text.encode()).hexdigest() == expected_text_sha256,
            'Selected voice/profile or exact text differs from the independently frozen job.')
    recorded = load_recorded_narration(owner_id=owner_id,job_id=job_id,text=text,profile=profile,
                                      approval=approval,journal=journal,response_store=response_store)
    requests,responses = recorded['requests'],recorded['responses']
    provenance = {k:v for k,v in recorded.items() if k not in {'requests','responses'}}
    provenance['source_kind'] = source_kind
    require(audio_settings == {'integrated_lufs': -16, 'true_peak_dbfs': -1, 'lra': 11}, 'This draft requires its explicit loudness settings.')
    require(isinstance(requests, list) and isinstance(responses, list) and 0 < len(requests) == len(responses) <= 2, 'All planned narration responses are required.')
    profile = validate_profile(profile)
    planned_text = ''.join(request['body']['text'] for request in requests)
    require(fingerprint(requests) == fingerprint(plan_requests(planned_text, profile)), 'Narration requests differ from the frozen deterministic plan.')
    root = Path(job_root)
    require(root.is_absolute() and not any(p.is_symlink() for p in (root,*root.parents)), 'Private absolute non-symlink job directory required.')
    require(not root.resolve().is_relative_to(Path(__file__).resolve().parent.parent / 'web'), 'Narration must remain outside the public web directory.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = root.resolve(strict=True)
    target = root/'narration.wav'; report_path = root/'narration.json'
    require(not target.exists() and not report_path.exists(), 'Do not overwrite a prior narration.')
    version = run(['ffmpeg', '-version']).stdout.decode().splitlines()[0].split()[2]
    require(version == expected_ffmpeg_version, 'FFmpeg runtime differs from the recorded draft.')
    words, total_frames, cursor, text, parts = [], 0, 0, '', []
    request_ids = set()
    with tempfile.TemporaryDirectory(prefix='narration-', dir=root) as scratch:
        temp = Path(scratch)
        for index, (request, response) in enumerate(zip(requests, responses), 1):
            req = {k:v for k,v in request.items() if k != 'sha256'}
            require(fingerprint(req) == request['sha256'] and request['profile_sha256'] == fingerprint(profile), 'Request or voice profile changed.')
            require(request['sequence'] == index and request['char_start'] == cursor, 'Narration chunk order or offsets changed.')
            provider_id = response.get('request_id')
            require(isinstance(provider_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{8,160}', provider_id)
                    and provider_id not in request_ids, 'Each planned chunk requires a distinct valid provider request ID.')
            request_ids.add(provider_id)
            chunk = request['body']['text']
            require(request['char_end'] == cursor+len(chunk) and response['exact_text'] == chunk, 'Narration omitted or rewrote a source span.')
            require(response.get('provider') == 'elevenlabs' and response.get('media_verified') is False, 'Use the unverified provider response boundary once.')
            audio = response['audio_bytes']
            require(isinstance(audio, bytes) and 0 < len(audio) <= MAX_AUDIO_BYTES and hashlib.sha256(audio).hexdigest() == response['audio_sha256'], 'Provider audio bytes changed.')
            original = temp/f'chunk-{index}.mp3'; original.write_bytes(audio)
            decoded = temp/f'chunk-{index}.wav'
            # MP3 demux is explicit: a disguised playlist can never open other files.
            run(['ffmpeg','-nostdin','-v','error','-xerror','-threads','1','-protocol_whitelist','file,pipe',
                 '-f','mp3','-i',str(original),'-t',str(MAX_SECONDS+1),'-map','0:a:0','-vn','-ac','1','-ar',str(RATE),'-c:a','pcm_s16le','-f','wav',str(decoded)])
            count, pcm = wav_info(decoded)
            duration = count/RATE
            require([(w['text'], w['char_start'], w['char_end']) for w in response['words']] ==
                    [(m.group(), m.start(), m.end()) for m in re.finditer(r'\S+', chunk)], 'Word boundaries differ from exact source tokens.')
            previous, last_char = 0., 0
            for word in response['words']:
                a,b,ca,cb = word['start'],word['end'],word['char_start'],word['char_end']
                # Provider timings and the decoded MP3 can differ by rounding at the very end (seen: 1 ms).
                # Clamp only that tiny edge; a word starting past the audio, or ending well past it, still fails.
                if type(b) in (int,float) and math.isfinite(b) and duration < b <= duration + END_TOLERANCE:
                    b = duration; word = {**word, 'end': b}
                require(type(ca) is int and type(cb) is int and last_char <= ca < cb <= len(chunk)
                        and not chunk[last_char:ca].strip() and chunk[ca:cb] == word['text'], 'Word text or spans changed before media handoff.')
                require(all(type(t) in (int,float) and math.isfinite(t) for t in (a,b))
                        and 0 <= previous <= a <= b <= duration and (b>a or not re.search(r'\w',word['text'])), 'Word timings exceed decoded audio or overlap.')
                require(not any(c.isspace() for c in word['text']), 'Word timing spans whitespace.')
                words.append({**word, 'start':a+total_frames/RATE, 'end':b+total_frames/RATE,
                              'char_start':ca+cursor, 'char_end':cb+cursor})
                previous, last_char = b, cb
            require(response['words'] and not chunk[last_char:].strip(), 'Word timings omit source wording.')
            pause = pause_after(chunk, index == len(requests), profile.get('pacing'))
            parts.append({'request_sha256': request['sha256'], 'request_id': response['request_id'],
                          'source_audio_sha256': response['audio_sha256'], 'decoded_frames': count,
                          'offset_frames':total_frames, 'char_start':cursor, 'char_end':cursor+len(chunk), 'pause_frames': pause})
            total_frames += count + pause; cursor += len(chunk); text += chunk
            require(total_frames <= RATE*MAX_SECONDS, 'Complete narration exceeds the 900-second technical ceiling; no truncation.')
        joined = temp/'joined.wav'
        with wave.open(str(joined), 'wb') as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(RATE)
            for index in range(1,len(parts)+1):
                output.writeframes(wav_info(temp/f'chunk-{index}.wav')[1])
                output.writeframes(b'\x00\x00' * parts[index-1]['pause_frames'])  # Storyteller pause (mono 16-bit).
        require(wav_info(joined)[0] == total_frames, 'Audio join changed sample count.')
        loud = 'loudnorm=I=-16:TP=-1:LRA=11'
        measure = run(['ffmpeg','-nostdin','-hide_banner','-f','wav','-i',str(joined),'-af',loud+':print_format=json','-f','null','-'])
        values = loudness_json(measure.stderr)
        normalization = loud + f":measured_I={values['input_i']}:measured_TP={values['input_tp']}:measured_LRA={values['input_lra']}:measured_thresh={values['input_thresh']}:offset={values['target_offset']}:linear=true:print_format=json"
        normalized = temp/'normalized.wav'
        second = run(['ffmpeg','-nostdin','-hide_banner','-f','wav','-i',str(joined),'-af',normalization,
                      '-ac','1','-ar',str(RATE),'-c:a','pcm_s16le','-f','wav',str(normalized)])
        require(wav_info(normalized)[0] == total_frames, 'Normalization changed duration; no silent audio retiming.')
        final = loudness_json(run(['ffmpeg','-nostdin','-hide_banner','-f','wav','-i',str(normalized),'-af',loud+':print_format=json','-f','null','-']).stderr)
        require(abs(float(final['input_i'])+16)<=.5 and float(final['input_tp'])<=-.95, 'Normalized audio missed its loudness/peak target.')
        report = {'schema_version':1,'synthetic':source_kind == 'synthetic_test','live_provider_verified':False,'media_verified':True,
                  'status':'DRAFT_RELEASE_CANDIDATE','live_release':False,'human_approved':False,
                  'provider_receipts_verified':True,'provenance':provenance,
                  'provider':'elevenlabs','exact_text':text,'words':words,
                  'duration_seconds':total_frames/RATE,'sample_rate':RATE,'frames':total_frames,
                  'profile_sha256':fingerprint(profile),'profile':profile,'audio_settings':audio_settings,
                  'parts':parts,'ffmpeg_version':version,'synthesis_calls_in_this_media_stage':0,'automatic_retries':0,
                  'audio_sha256':hashlib.sha256(normalized.read_bytes()).hexdigest(),
                  'measured_integrated_lufs':float(final['input_i']),'measured_true_peak_dbfs':float(final['input_tp']),
                  'normalization_passes':1,'timing_origin':'synthetic-character-alignment-fixture_not_real_provider_evidence' if source_kind == 'synthetic_test' else 'elevenlabs-original-character-alignment'}
        # Preserve raw responses as files, never log base64 or source text.
        for index in range(1,len(parts)+1):
            destination = root/f'provider-chunk-{index}.mp3'
            require(not destination.exists(), 'Original provider audio already exists.')
            with destination.open('xb') as output: output.write((temp/f'chunk-{index}.mp3').read_bytes())
            destination.chmod(0o600)
        with target.open('xb') as output: output.write(normalized.read_bytes())
        target.chmod(0o600)
        with report_path.open('x') as output: json.dump(report,output,ensure_ascii=False,indent=2,allow_nan=False)
        report_path.chmod(0o600)
    return report
