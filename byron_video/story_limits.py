"""Story checks for the lean film studio, with generous limits: the owner's only intended cap is money.

Same rules as engine.validate_script (never rewrites or shortens the text; no control characters, no
leftover markup, no absurdly long tokens) but with far larger size limits. engine.py itself is
fingerprint-locked by the caption and style code, so its older 16,000-character limit stays there for
the older paths.
"""
import re

from .engine import narration_text

STORY_MAX_CHARS = 100_000
STORY_MAX_WORDS = 20_000


def validate_story(script):
    if not isinstance(script, str) or not script.strip():
        raise ValueError('Paste the story you want narrated.')
    if len(script) > STORY_MAX_CHARS:
        raise ValueError(f'The studio accepts up to {STORY_MAX_CHARS:,} characters. Nothing was shortened.')
    if any(ord(ch) < 32 and ch not in '\n\r\t' for ch in script):
        raise ValueError('The story contains unsupported control characters.')
    visible = narration_text(script)
    if re.search(r'\[[^\]]+\]\([^)]*\)', visible):
        raise ValueError('Use plain text or standard web links; other markup is not supported.')
    if len(re.findall(r'\S+', script)) > STORY_MAX_WORDS:
        raise ValueError(f'The studio accepts up to {STORY_MAX_WORDS:,} words per film. Nothing was shortened.')
    if any(len(word) > 80 for word in re.findall(r'\S+', visible)):
        raise ValueError('The story contains an unusually long word or address. Please use readable plain text.')
    return script
