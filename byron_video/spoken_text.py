"""What the narrator reads: the story without its heading lines or web links.

  - heading: at the top, before the story begins, the title and byline lines are set aside:
    a line starting with '#', a "by …" / "written by …" line, or a short line (at most 15
    words) with no sentence-ending punctuation that is followed by a blank line;
  - links: Markdown links keep their visible label; http(s)/www addresses are removed anywhere;
    a line that is only web addresses (including plain ones such as badmaddangerous.com),
    optionally with a short label such as "Read more:", is dropped. A plain domain name
    inside a sentence stays, because it is part of the story's wording.
Runs of blank lines become one paragraph break. What was set aside is reported back so the
page can show it; story wording is otherwise unchanged. web/core.mjs mirrors these rules
for the preview shown before Convert.
"""
import re

from .engine import narration_text

URL = re.compile(r'(?:https?://|www\.)\S+', re.I)
DOMAIN = re.compile(r'\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|co|uk|io|info|blog|me|art|eu|es|ae)\b(?:/\S*)?', re.I)


def link_only(line):
    """(link count, True) when the line is only web addresses, optionally with a short label."""
    count = len(URL.findall(line)) + len(DOMAIN.findall(URL.sub('', line)))
    rest = DOMAIN.sub('', URL.sub('', line)).strip(' \t:-–—|·')
    label = len(rest.split()) <= 4 and DOMAIN.sub('', URL.sub('', line)).rstrip(' \t').endswith(':')
    return count, bool(count) and (not re.search(r'[^\W\d_]', rest) or label)
SENTENCE_END = re.compile(r'[.!?…:;,"\'”’)]$')
BYLINE = re.compile(r'(?:written\s+|words\s+)?by\s+\S', re.I)
HEADING_LINES = 4


def is_heading(line, following):
    line = line.strip()
    if line.startswith('#') or (BYLINE.match(line) and len(line.split()) <= 8):
        return True
    return len(line.split()) <= 15 and not SENTENCE_END.search(line) and not following.strip()


def prepare_story(script):
    """Return (narrated text, skipped) where skipped = {'heading': [lines], 'links': int}."""
    lines = narration_text(script).replace('\r\n', '\n').replace('\r', '\n').split('\n')
    skipped = {'heading': [], 'links': 0}
    seen = 0
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if seen == HEADING_LINES:
            break
        seen += 1
        if link_only(line)[1]:
            continue  # A link-only line; removed with the other links below.
        if not is_heading(line, lines[index + 1] if index + 1 < len(lines) else ''):
            break  # The story has begun.
        skipped['heading'].append(line.strip().lstrip('#').strip())
        lines[index] = ''
    out, separator = [], None
    for line in lines:
        count, only = link_only(line)
        if only:
            skipped['links'] += count
            continue
        found = URL.findall(line)  # Full web addresses inside a sentence; plain domain names there stay as wording.
        if found:
            skipped['links'] += len(found)
            line = URL.sub('', line)
            if not re.search(r'[^\W\d_]', line):
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
        raise ValueError('There is no story left to read once the heading and links are set aside.')
    return narrated, skipped
