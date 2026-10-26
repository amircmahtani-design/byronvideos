"""Synthetic draft baselines: regression evidence, never aesthetic approval."""
import hashlib
import json
from pathlib import Path
from PIL import Image, ImageDraw
from byron_video.engine import artwork_frame, draw_caption, Caption, _font
from byron_video.style import load_style

ROOT = Path(__file__).resolve().parent.parent
BASELINES = ROOT / 'style-guide/references/synthetic-baselines'


def reference_frame(style, portrait=False):
    size = (700, 1000) if portrait else (1000, 700)
    art = Image.new('RGB', size, '#5B0E14')
    draw = ImageDraw.Draw(art)
    draw.rectangle((4,4,size[0]-5,size[1]-5), outline='#C8A96A', width=10)
    draw.ellipse((size[0]*.22,size[1]*.22,size[0]*.78,size[1]*.78), fill='#C8A96A')
    draw.text((size[0]/2,60), 'SYNTHETIC TEST — NOT USER ART', font=_font(26,style), anchor='mm', fill='#EFE6D2')
    canvas = (style['canvas']['width'], style['canvas']['height'])
    frame = artwork_frame(art, .5, 1 if portrait else 0, canvas, style)
    draw_caption(frame, Caption('Draft regression reference. This visual style is not approved.',0,5), _font(round(canvas[1]*style['captions']['font_height_fraction']),style), style)
    return frame


def test_synthetic_draft_reference_pixels_match():
    style = load_style()
    manifest = json.loads((BASELINES/'pixels.json').read_text())
    assert manifest['status'] == 'DRAFT_SYNTHETIC_ONLY'
    for portrait, name in ((False,'landscape'),(True,'portrait')):
        actual = reference_frame(style,portrait)
        baseline = Image.open(BASELINES/f'{name}.png').convert('RGB')
        assert actual.size == baseline.size
        assert hashlib.sha256(actual.tobytes()).hexdigest() == manifest[name]['rgb_sha256']
        assert actual.tobytes() == baseline.tobytes()
