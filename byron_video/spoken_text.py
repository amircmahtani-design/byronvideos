"""What the narrator reads: the story without its title line or web links.

  - title: the first non-empty line when it starts with '#', or when it is short (at most
    15 words), has no sentence-ending punctuation and is followed by a blank line;
  - links: Markdown links keep their visible label; bare web addresses are removed, and a
    line left with no words (or only a short label such as "Read more:") is dropped.
Runs of blank lines become one paragraph break. What was set aside is reported back so the
page can show it; story wording is otherwise unchanged. web/core.mjs mirrors these rules
for the preview shown before Convert.
"""
import re

from .engine import narration_text

URL = re.compile(r'(?:https?://|www\.)\S+', re.I)
SENTENCE_END = re.compile(r'[.!?…:;,"\'”’)]$')


def is_title(line, following):
    line = line.strip()
    if line.startswith('#'):
        return True
    return len(line.split()) <= 15 and not SENTENCE_END.search(line) and not following.strip()


def prepare_story(script):
    """Return (narrated text, skipped) where skipped = {'title': str or None, 'links': int}."""
    lines = narration_text(script).replace('\r\n', '\n').replace('\r', '\n').split('\n')
    skipped = {'title': None, 'links': 0}
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None and is_title(lines[first], lines[first + 1] if first + 1 < len(lines) else ''):
        skipped['title'] = lines[first].strip().lstrip('#').strip()
        lines[first] = ''
    out, separator = [], None
    for line in lines:
        found = URL.findall(line)
        if found:
            skipped['links'] += len(found)
            line = URL.sub('', line)
            if not re.search(r'[^\W\d_]', line) or (len(line.split()) <= 4 and line.rstrip().endswith(':')):
                continue
        line = line.rstrip()
        if not line.strip():
            if separator:
                separator = '\n\n'
            continue
        if separator:
            out.append(separator)
        out.append(line); separator = '\n'
    narrated = ''.join(out)
    if not narrated.strip():
        raise ValueError('There is no story left to read once the title and links are set aside.')
    return narrated, skipped
