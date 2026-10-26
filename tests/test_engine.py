from pathlib import Path
from types import SimpleNamespace
import pytest
from PIL import Image
from byron_video.engine import (validate_script, validate_images, _align_tokens, make_captions, scene_boundaries, artwork_frame, wrap_caption, _font, Word)


def test_script_exact():
    text = '  “Paintings,” he said.\n\nNothing rewritten!  '
    assert validate_script(text, True) == text


@pytest.mark.parametrize('text', ['', 'short', 'word '*451, 'a'*4001, 'A\x00bad text', '[name](/pronunciation/)'])
def test_reject_bad_scripts(text):
    with pytest.raises(ValueError):
        validate_script(text)


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
