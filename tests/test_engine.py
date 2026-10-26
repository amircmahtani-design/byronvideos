from pathlib import Path
from types import SimpleNamespace
import pytest
from PIL import Image
from byron_video.engine import (validate_script, validate_images, _align_tokens, make_captions, scene_boundaries, artwork_frame, wrap_caption, _font, Word)


def test_script_exact():
    text = '  “Paintings,” he said.\n\nNothing rewritten!  '
    assert validate_script(text, True) == text


@pytest.mark.parametrize('text', ['', 'word '*451, 'a'*4001, 'A\x00bad text', '[name](/pronunciation/)'])
def test_reject_bad_scripts(text):
    with pytest.raises(ValueError):
        validate_script(text)


def test_short_script_is_not_an_editorial_error():
    text = '  A short, exact essay.  '
    assert validate_script(text) == text


@pytest.mark.parametrize('seconds,has_advisory', [(89,True),(90,False),(180,False),(181,True)])
def test_duration_guidance_synthesizes_once_at_frozen_speed(monkeypatch, seconds, has_advisory):
    import copy
    import numpy as np
    from byron_video import engine
    from byron_video.style import load_style
    style = load_style()
    original_style = copy.deepcopy(style)
    exact_script = '  “Keep every word,” he said.\n\nNo automatic retiming.  '
    calls = []
    audio = np.ones(seconds * engine.SAMPLE_RATE, dtype=np.float32) * .1
    tokens, paragraphs = [object()], [.5]
    def fake_synthesis(script, voice, *, speed, progress, style):
        calls.append((script,voice,speed))
        return audio, tokens, paragraphs
    monkeypatch.setattr(engine, 'synthesize', fake_synthesis)
    returned = engine.prepare_narration(exact_script, style)
    assert len(calls) == 1
    assert calls[0] == (exact_script, style['narration']['voice'], style['narration']['speed'])
    assert style == original_style
    assert returned[0] is audio and returned[1] is tokens and returned[2] is paragraphs
    assert returned[3] == seconds and bool(returned[4]) is has_advisory


def test_technical_duration_limit_is_separate_from_editorial_guidance():
    from byron_video.engine import duration_advisory
    from byron_video.style import load_style
    style = load_style()
    assert duration_advisory(190,style)
    assert duration_advisory(300,style)
    with pytest.raises(ValueError,match='technical processing limit'):
        duration_advisory(301,style)


def test_valid_images_and_source_untouched(tmp_path):
    paths = []
    for i in range(4):
        p = tmp_path / f'{i}.png'
        Image.new('RGB', (100, 200), 'red').save(p)
        paths.append(p)
    before = [p.read_bytes() for p in paths]
    assert len(validate_images(paths)) == 4
    assert before == [p.read_bytes() for p in paths]
    with pytest.raises(ValueError):
        validate_images(paths[:3])
    paths[0].write_text('not image')
    with pytest.raises(ValueError):
        validate_images(paths)


def test_alignment_preserves_punctuation():
    script = 'Hello, “world”.'
    tokens = [SimpleNamespace(text=t, start_ts=i*.2, end_ts=(i+1)*.2) for i,t in enumerate(['Hello', ',', '“', 'world', '”', '.'])]
    words, cursor = _align_tokens(script,tokens,.5,0,2)
    captions = make_captions(script, words)
    assert ''.join(c.text for c in captions) == script
    assert cursor == len(script)
    assert words[0].start == .5


def test_changed_or_unaligned_words_rejected():
    with pytest.raises(ValueError):
        _align_tokens('Original', [SimpleNamespace(text='Changed',start_ts=0,end_ts=1)],0,0,1)
    with pytest.raises(ValueError):
        _align_tokens('Original', [SimpleNamespace(text='Original',start_ts=None,end_ts=None)],0,0,1)


def test_scene_exact_paragraph_mapping():
    assert scene_boundaries(30,4,[.5,7,15,22],[]) == [0,7,15,22,30]


def test_scene_default_order():
    boundaries = scene_boundaries(30,5,[.5],[])
    assert boundaries == [0,6,12,18,24,30]


@pytest.mark.parametrize('size',[(100,400),(400,100),(400,400)])
@pytest.mark.parametrize('p',[0,.5,1])
def test_motion_never_clips_painting(size,p):
    image = Image.new('RGB',size,'white')
    frame = artwork_frame(image,p,0,(960,540))
    import numpy as np
    ys,xs = np.where(np.array(frame)[:,:,0] > 100)
    assert min(xs)>0 and max(xs)<959
    assert min(ys)>0 and max(ys)<450  # independent caption safe area


def test_captions_max_two_lines():
    font = _font(38)
    lines = wrap_caption('The complete painting remains visible while the captions breathe beneath it.',font,1650)
    assert len(lines) <= 2
    assert ' '.join(lines) == 'The complete painting remains visible while the captions breathe beneath it.'
