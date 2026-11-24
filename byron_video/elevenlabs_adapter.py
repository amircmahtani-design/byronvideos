"""Server-side ElevenLabs request/alignment boundary. No live transport or secrets.

This additive module is not called by the local Kokoro renderer or public website.
A production worker must supply reviewed transport, authorization and spend journal.
"""
from __future__ import annotations
import base64
import hashlib
import json
import math
import re

API_ORIGIN = 'https://api.elevenlabs.io'
MODEL_LIMITS = {'eleven_multilingual_v2': 10_000, 'eleven_flash_v2_5': 40_000}
MAX_AUDIO_BYTES = 40_000_000
MAX_RESPONSE_BYTES = 60_000_000
LIVE_RELEASE = False


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


PROFILE_KEYS = {'schema_version', 'version', 'status', 'provider', 'voice_id', 'model_id',
                'voice_settings', 'output_format', 'seed', 'apply_text_normalization'}
# Optional storyteller pacing: one request per sentence, with silences added between them.
PACING = ('sentences',)
SENTENCE_END = re.compile(r'[.!?…]+["\'”’)\]]*(?=\s|$)')
MAX_PACED_REQUESTS = 400  # Leaves room for the paintings within the ledger's 480 operations per film.
ABBREVIATIONS = {'mr', 'mrs', 'ms', 'dr', 'st', 'mt', 'jr', 'sr', 'prof', 'rev', 'col', 'gen', 'capt', 'lt', 'vs', 'etc', 'no'}
# Storyteller ending (sentence pacing only), chosen by ear with the owner: the final sentence is read slower in
# the same voice (only speed changes), and its closing words are recorded on their own so the voice lands them
# as the end of the story instead of carrying on.
ENDING_SPEED = 0.75
CLOSING_MIN_WORDS, CLOSING_MAX_WORDS = 3, 7  # Closing words split off a final sentence of at least 8 words.
CLOSING_PRONOUNS = {'he', 'she', 'they', 'i', 'we', 'you', 'it'}  # Second choice: a new clause's subject.
CLOSING_FALLBACK_WORDS = 4  # Last resort, so every long final sentence still lands its closing words.
CLOSING_STARTERS = {'if', 'when', 'whether', 'until', 'till', 'before', 'after', 'while', 'as', 'because', 'though',
                    'although', 'and', 'but', 'or', 'so', 'that', 'where', 'which', 'who', 'unless', 'since', 'once'}


def validate_profile(profile):
    require(isinstance(profile, dict) and set(profile) in (PROFILE_KEYS, PROFILE_KEYS | {'pacing'}), 'Incomplete narration profile.')
    require(profile.get('pacing', PACING[0]) in PACING, 'Unknown narration pacing.')
    require(type(profile['schema_version']) is int and profile['schema_version'] == 1, 'Invalid narration schema.')
    require(profile['status'] == 'DRAFT' and profile['provider'] == 'elevenlabs', 'This adapter is an unapproved ElevenLabs draft only.')
    require(isinstance(profile['version'], str) and re.fullmatch(r'[a-z0-9.-]{1,80}', profile['version']), 'Invalid profile version.')
    require(isinstance(profile['voice_id'], str) and re.fullmatch(r'[A-Za-z0-9_-]{8,80}', profile['voice_id']), 'Choose and review an ElevenLabs voice ID first.')
    require(profile['model_id'] in MODEL_LIMITS, 'Choose a supported explicitly pinned model.')
    settings = profile['voice_settings']
    require(isinstance(settings, dict) and set(settings) == {'stability', 'similarity_boost', 'style', 'use_speaker_boost', 'speed'}, 'All voice settings must be explicit.')
    for name in ('stability', 'similarity_boost', 'style'):
        require(type(settings[name]) in (float, int) and math.isfinite(settings[name]) and 0 <= settings[name] <= 1, 'Invalid voice setting.')
    require(type(settings['use_speaker_boost']) is bool, 'Speaker boost must be a boolean.')
    require(type(settings['speed']) in (int, float) and math.isfinite(settings['speed']) and .7 <= settings['speed'] <= 1.2, 'Invalid fixed voice speed.')
    require(profile['output_format'] == 'mp3_44100_128', 'This draft accepts only the explicit MP3 format.')
    require(type(profile['seed']) is int and 0 <= profile['seed'] <= 4294967295, 'Explicit bounded seed required; it does not guarantee determinism.')
    require(profile['apply_text_normalization'] == 'off', 'No hidden script normalization.')
    return json.loads(json.dumps(profile, allow_nan=False))


def plan_requests(text, profile):
    """Plan exact contiguous spans once; no duration-driven synthesis or rewrite.

    >10k characters on Multilingual v2 uses preplanned chunks at whitespace.
    This is not a second attempt at the same text. No paid transport is invoked.
    """
    from .story_limits import validate_story
    validate_story(text)
    p = validate_profile(profile)
    limit = MODEL_LIMITS[p['model_id']]
    spans, start = ([], 0) if p.get('pacing') != 'sentences' else (sentence_spans(text, limit), len(text))
    ending = None
    if p.get('pacing') == 'sentences':
        spans, ending = ending_spans(text, spans)
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            positions = [m.end() for m in re.finditer(r'\s+', text[start:end])]
            require(positions, 'A text token exceeds the provider request limit; nothing was cut.')
            end = start + positions[-1]
        spans.append({'char_start': start, 'char_end': end, 'text': text[start:end]})
        start = end
    require(''.join(s['text'] for s in spans) == text, 'Request plan omitted text.')
    requests = []
    for i, span in enumerate(spans):
        require(bool(span['text'].strip()), 'Formatting creates an all-whitespace provider span; adjust whitespace before any provider calls. Nothing was cut.')
        settings = p['voice_settings']
        if ending and i >= ending['final']:  # The final sentence: same voice, slower.
            settings = {**settings, 'speed': min(settings['speed'], ENDING_SPEED)}
        body = {'text': span['text'], 'model_id': p['model_id'], 'voice_settings': settings,
                'seed': p['seed'], 'apply_text_normalization': 'off', 'apply_language_text_normalization': False}
        # Context is source text, not another requested narration or a prompt. Sentence pacing gives each
        # sentence the surrounding story so its tone carries on naturally instead of restarting.
        if i:
            body['previous_text'] = (text[max(0, span['char_start'] - 1000):span['char_start']] if p.get('pacing')
                                     else spans[i-1]['text'][-1000:])
        if i + 1 < len(spans):
            body['next_text'] = (text[span['char_end']:span['char_end'] + 1000] if p.get('pacing')
                                 else spans[i+1]['text'][:1000])
        request = {'method': 'POST', 'url': f"{API_ORIGIN}/v1/text-to-speech/{p['voice_id']}/with-timestamps?output_format={p['output_format']}",
                   'body': body, 'char_start': span['char_start'], 'char_end': span['char_end'],
                   'profile_sha256': fingerprint(p), 'sequence': i + 1}
        request['sha256'] = fingerprint(request)
        requests.append(request)
    return requests


def sentence_cuts(text):
    """Positions where a sentence or line ends (after its trailing whitespace)."""
    cuts = set()
    for match in SENTENCE_END.finditer(text):
        word = re.search(r'([A-Za-z]+)\W*$', text[max(0, match.start() - 24):match.start() + 1])  # Local look-back only.
        if word and word.group(1).lower() in ABBREVIATIONS and match.group().startswith('.'):
            continue  # "Mr. Mehra" is one sentence.
        cuts.add(re.match(r'\s*', text[match.end():]).end() + match.end())
    cuts |= {m.end() for m in re.finditer(r'\n\s*', text)}  # A line break always ends a spoken unit.
    return cuts


def closing_cut(sentence):
    """Offset where a final sentence's closing words begin, or None to read it whole (under 8 words).

    The closing words are the last 3-7 words. They start, in order of preference, at the earliest joining
    word ("if", "when", "and"...) or right after a comma/semicolon/colon/dash in that window; else at the
    earliest subject pronoun ("he", "she", "they"...); else they are simply the last 4 words, so every
    film's final sentence lands its ending (chosen by ear with the owner).
    """
    words = list(re.finditer(r'\S+', sentence))
    if len(words) < CLOSING_MIN_WORDS + 5:
        return None
    window = range(len(words) - CLOSING_MAX_WORDS, len(words) - CLOSING_MIN_WORDS + 1)
    bare = [re.sub(r'^\W+|\W+$', '', w.group()).lower() for w in words]
    for j in window:
        if bare[j] in CLOSING_STARTERS or re.search(r'[,;:–—]$', words[j - 1].group()):
            return words[j].start()
    for j in window:
        if bare[j] in CLOSING_PRONOUNS:
            return words[j].start()
    return words[len(words) - CLOSING_FALLBACK_WORDS].start()


def ending_spans(text, spans):
    """Give the final sentence its own spans: (spans, {'final': index of its first span, 'closing': index or None}).

    Exact text is kept: spans stay contiguous and cover the story unchanged.
    """
    if not spans:
        return spans, None
    last = spans[-1]
    inside = [c for c in sentence_cuts(text) if last['char_start'] < c < last['char_end']]
    start = max(inside) if inside else last['char_start']  # A grouped last span: split off its final sentence.
    if not re.search(r'\w', text[start:last['char_end']]):
        return spans, None
    head = spans[:-1] + ([{'char_start': last['char_start'], 'char_end': start, 'text': text[last['char_start']:start]}]
                         if start > last['char_start'] else [])
    final, cut = len(head), closing_cut(text[start:last['char_end']])
    if cut is None:
        tail = [{'char_start': start, 'char_end': last['char_end'], 'text': text[start:last['char_end']]}]
    else:
        tail = [{'char_start': start, 'char_end': start + cut, 'text': text[start:start + cut]},
                {'char_start': start + cut, 'char_end': last['char_end'], 'text': text[start + cut:last['char_end']]}]
    return head + tail, {'final': final, 'closing': final + 1 if cut is not None else None}


def ending_roles(text, profile):
    """The ending plan of plan_requests for this exact text/profile, by request index (0-based), or None."""
    p = validate_profile(profile)
    if p.get('pacing') != 'sentences':
        return None
    return ending_spans(text, sentence_spans(text, MODEL_LIMITS[p['model_id']]))[1]


def sentence_spans(text, limit):
    """Contiguous spans, one per sentence (or line), each keeping its trailing whitespace."""
    cuts = sentence_cuts(text)
    spans, start = [], 0
    for end in sorted(c for c in cuts if 0 < c < len(text)) + [len(text)]:
        if end > start and text[start:end].strip():
            require(end - start <= limit, 'A sentence exceeds the provider request limit; nothing was cut.')
            spans.append({'char_start': start, 'char_end': end, 'text': text[start:end]})
            start = end
    if start < len(text):  # Trailing whitespace joins the last span.
        spans[-1] = {**spans[-1], 'char_end': len(text), 'text': text[spans[-1]['char_start']:]}
    if len(spans) > MAX_PACED_REQUESTS:  # A very long story: read a few sentences per request so it always fits.
        size = -(-len(spans) // MAX_PACED_REQUESTS)
        groups = [spans[i:i + size] for i in range(0, len(spans), size)]
        spans = [{'char_start': g[0]['char_start'], 'char_end': g[-1]['char_end'],
                  'text': text[g[0]['char_start']:g[-1]['char_end']]} for g in groups]
        require(all(s['char_end'] - s['char_start'] <= limit for s in spans), 'A sentence group exceeds the provider request limit; nothing was cut.')
    return spans


def alignment_words(exact_text, alignment):
    require(isinstance(alignment, dict), 'Original-text character alignment is required.')
    chars = alignment.get('characters')
    starts = alignment.get('character_start_times_seconds')
    ends = alignment.get('character_end_times_seconds')
    require(isinstance(chars, list) and all(isinstance(c, str) and len(c) == 1 for c in chars), 'Invalid character alignment.')
    require(''.join(chars) == exact_text, 'Provider alignment changed or omitted original text.')
    require(isinstance(starts, list) and isinstance(ends, list) and len(chars) == len(starts) == len(ends), 'Alignment lengths differ.')
    previous_start, previous_end = 0., 0.
    for a, b in zip(starts, ends):
        require(type(a) in (int, float) and type(b) in (int, float) and math.isfinite(a) and math.isfinite(b)
                and 0 <= previous_start <= a <= b <= 900 and previous_end <= b, 'Invalid character timing.')
        previous_start, previous_end = a, b
    words, previous = [], 0.
    for match in re.finditer(r'\S+', exact_text):
        a, b = starts[match.start()], ends[match.end()-1]
        require(a >= previous and (b > a or not re.search(r'\w', match.group())), 'Word timing is zero-duration or overlapping.')
        words.append({'text': match.group(), 'start': a, 'end': b,
                      'char_start': match.start(), 'char_end': match.end()})
        previous = b
    require(words, 'No narrated words in response.')
    return words


def parse_response(exact_text, payload_bytes, request_id):
    require(isinstance(payload_bytes, bytes) and 0 < len(payload_bytes) <= MAX_RESPONSE_BYTES, 'Provider response exceeds the bounded JSON limit.')
    require(isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{8,160}', request_id), 'Provider request ID is required for reconciliation.')
    payload = json.loads(payload_bytes, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite response JSON.')))
    require(isinstance(payload, dict) and isinstance(payload.get('audio_base64'), str), 'Missing narration audio.')
    words = alignment_words(exact_text, payload.get('alignment'))
    try:
        audio = base64.b64decode(payload['audio_base64'], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid audio encoding.') from exc
    require(0 < len(audio) <= MAX_AUDIO_BYTES, 'Audio exceeds the technical byte limit.')
    # Bytes are still untrusted: force MP3 demux, decode/probe, normalize once and
    # verify timing against decoded duration before allowing a compositor handoff.
    return {'provider': 'elevenlabs', 'request_id': request_id, 'audio_bytes': audio,
            'audio_sha256': hashlib.sha256(audio).hexdigest(), 'exact_text': exact_text,
            'words': words, 'timing_origin': 'elevenlabs-original-character-alignment',
            'media_verified': False}


def live_transport(*_args, **_kwargs):
    raise RuntimeError('Hosted narration is not activated: reviewed credentials, budget, privacy, profile and worker deployment are required.')
