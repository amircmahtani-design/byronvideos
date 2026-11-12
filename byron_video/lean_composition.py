"""Lean portrait composition: living paintings that always return to the painting's own pose.

Each painting's animated clip (length L ≈ 10 s) starts AND ends on the original painting
(both anchors are the original). Motion only ever meets the still at those anchors, so the
moving frame and the still always show the same pose: no ghosting, no visible cut.

Scene rule (scene length D from the narration split; R = 3 s settle, T = 1.5 s turn):
  D >= 24 s        two_moments: early, the clip plays 0 → 3 s and eases back to 0 (out and back);
                                late, it plays L → L−3 s backwards and eases forward to L, ending
                                exactly at the scene end. Two different movements, never a repeat.
  13 <= D < 24 s   one_pass:    the whole clip 0 → L once, centred, easing in and out.
  D < 13 s         short:       an out-and-back from 0 as deep as the scene allows.
Every leg eases its playback speed: from rest to full over R at an anchor, and down to a gentle
turn over T where the motion reverses, so the subject settles back into the painting slowly.
A short dissolve (FADE) at each anchor covers only rendering differences, never a pose change.
Between moments the camera "breathes": a slow drift in and back out (zoom + pan, sin² size)
from rest to rest, holding still while the painting itself moves. Clip frames are kept in
memory (compressed) so a movement can play back toward its anchor; slowed frames are blended
between neighbouring source frames. Every clip/still join is measured (mean absolute grey
difference, 0–255) and recorded; joins above the threshold are flagged.
"""
import bisect
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageStat

from .engine import Caption, _colour, _font, draw_caption, make_captions, wrap_caption, write_srt
from .portrait_composition import input_options, presentation, run, run_bounded, validate_words

RAMP = 3.0  # Ease from rest / into rest at an anchor (the original pose).
TURN = 1.5  # Ease into and out of the turning point of an out-and-back movement.
FADE = 0.8  # Dissolve at an anchor; same pose on both sides, so it only hides rendering differences.
DEPTH = 3.0  # Clip seconds an out-and-back movement travels before turning back.
TAIL = 2.0  # Seconds the last painting lingers after the final word.
CLOSING_FADE = 1.5  # Gentle fade to charcoal within that tail.
CAPTION_HOLD = 1.3  # A caption stays up across a pause up to this long (sentence and paragraph pauses).
TITLE_SECONDS = 2.0  # Default silent opening card (story title + byline); the narration starts after it.
TITLE_FADE = 0.2  # Card fade in and out, as a share of its length (at most 0.8 s each).
TITLE_REST = 0.1  # Charcoal between the card and the first painting's own fade, as a share (at most 0.4 s).
TITLE_BYLINE = 'Lord Byron'
TITLE_FONT_FILE = Path(__file__).resolve().parent.parent / 'style-guide/fonts/Italianno-Regular.ttf'
TITLE_FONT_SHA256 = 'f6ae96dea0da46c73370eb0575848ab0eda190315bdfda3f5b252bba3dc9173c'
TITLE_WEIGHT = 0.022  # Outline as a share of the letter size: the approved "larger + bolder" handwriting.
COVER_LOSS = 0.06  # A painting may fill the frame edge to edge only if that hides at most 6% of it.
CAPTION_TOP = 0.80  # Short (landscape) paintings sit above this line, leaving the captions their own space.
TWO_MOMENTS_SCENE = 24.0
ONE_PASS_SCENE = 13.0
JOIN_SIZE = (135, 240)
JPEG_QUALITY = 93  # In-memory clip frames; visually lossless at this size.
ZOOM_RATE = 0.004  # Zoom amplitude per second of still window: slow by construction.
MAX_ZOOM = 0.05
PAN = 0.8  # Fraction of the zoom margin used for panning; keeps the frame inside the painting.
DIRECTIONS = ((1, -1), (-1, -1), (1, 1), (-1, 1))


def require(ok, message):
    if not ok:
        raise ValueError(message)


def ease(p):
    p = min(1., max(0., p))
    return p * p * (3 - 2 * p)


def eased_distance(p):
    """Clip seconds covered (per ramp second) after p of a speed ramp 0→1 that follows ease()."""
    p = min(1., max(0., p))
    return p ** 3 - p ** 4 / 2  # Integral of 3p² − 2p³; reaches 0.5 at p = 1.


@dataclass(frozen=True)
class Segment:
    """One leg of motion over clip seconds [clip_from, clip_to]; reverse plays it from clip_to back to clip_from."""
    at: float
    clip_from: float
    clip_to: float
    ramp_in: float
    ramp_out: float
    reverse: bool = False
    fade_in: float = 0.
    fade_out: float = 0.

    @property
    def length(self):
        return self.clip_to - self.clip_from + (self.ramp_in + self.ramp_out) / 2

    @property
    def end(self):
        return self.at + self.length

    def clip_time(self, local):
        """Clip seconds shown at `local` output seconds into the leg: eased in and out, monotonic."""
        if self.ramp_in and local < self.ramp_in:
            travelled = self.ramp_in * eased_distance(local / self.ramp_in)
        elif self.ramp_out and self.length - local < self.ramp_out:
            travelled = (self.clip_to - self.clip_from) - self.ramp_out * eased_distance((self.length - local) / self.ramp_out)
        else:
            travelled = self.ramp_in / 2 + (local - self.ramp_in)
        return self.clip_to - travelled if self.reverse else self.clip_from + travelled


def out_and_back(at, start_anchor, clip_seconds, depth, ramp, turn):
    """Two legs leaving an anchor (0 or L), turning gently, and settling back on exactly the same anchor."""
    L = clip_seconds
    if start_anchor == 0:
        out = Segment(at, 0., depth, ramp, turn, False, FADE, 0.)
        back = Segment(out.end, 0., depth, turn, ramp, True, 0., FADE)
    else:
        out = Segment(at, L - depth, L, ramp, turn, True, FADE, 0.)
        back = Segment(out.end, L - depth, L, turn, ramp, False, 0., FADE)
    return [out, back]


def scene_plan(duration, clip_seconds=10.0, ramp=RAMP, turn=TURN, depth=DEPTH):
    D, L, R, T = float(duration), float(clip_seconds), float(ramp), float(turn)
    require(math.isfinite(D) and D > 0 and math.isfinite(L) and 7.5 <= L <= 12 and 0 < R <= 4 and 0 < T <= R,
            'Invalid scene or clip timing.')
    moment = 2 * (depth + (R + T) / 2)  # Output length of one out-and-back movement.
    if D >= TWO_MOMENTS_SCENE:
        rule = 'two_moments'
        segments = out_and_back(0., 0, L, depth, R, T) + out_and_back(D - moment, L, L, depth, R, T)
    elif D >= max(ONE_PASS_SCENE, L + R):
        rule = 'one_pass'
        segments = [Segment(max(0., (D - (L + R)) / 2), 0., L, R, R, False, FADE, FADE)]
    else:
        rule = 'short'
        scale = min(1., D / (moment + 0.5))  # Shorter movement and gentler, shorter eases; never longer than the scene.
        segments = out_and_back(0., 0, L, max(0.2, depth * scale), R * scale, T * scale) if D >= 1.5 else []
    previous = 0.
    for s in segments:
        require(s.at >= previous - 1e-6 and s.clip_to > s.clip_from and s.clip_to - s.clip_from >= (s.ramp_in + s.ramp_out) / 2 - 1e-9,
                'Motion legs must be ordered and fit their clip span.')
        previous = s.end
    require(previous <= D + 1e-6, 'Motion exceeds the scene.')
    return {'rule': rule, 'duration': D, 'clip_seconds': L, 'ramp': R, 'turn': T, 'segments': segments}


def still_windows(plan):
    """Stretches where the original painting rests (the camera may breathe), from rest to rest."""
    edges = [0.] + [x for s in plan['segments'] for x in (s.at, s.end)] + [plan['duration']]
    return [(a, b) for a, b in zip(edges[::2], edges[1::2]) if b - a > 1e-6]


def state_at(plan, t):
    """(leg index or None, clip seconds, clip opacity over the still) at scene time t."""
    for index, s in enumerate(plan['segments']):
        if s.at - 1e-9 <= t < s.end:
            local, alpha = t - s.at, 1.
            if s.fade_in:
                alpha = min(alpha, ease(local / s.fade_in))
            if s.fade_out:
                alpha = min(alpha, ease((s.end - t) / s.fade_out))
            return index, s.clip_time(local), alpha
    return None, None, 0.


def drift_at(plan, t, index):
    """(zoom, x, y) camera breath; exactly (1, 0, 0) with zero speed at both ends of each still window."""
    for start, stop in still_windows(plan):
        if stop > start and start <= t <= stop:
            e = math.sin(math.pi * (t - start) / (stop - start)) ** 2
            dx, dy = DIRECTIONS[index % len(DIRECTIONS)]
            return 1 + min(MAX_ZOOM, ZOOM_RATE * (stop - start)) * e, dx * e, dy * e
    return 1., 0., 0.


def still_frame(path, size):
    """The original painting, cover-cropped (centre protected)."""
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(.5, .5))


def _render(source, box, size, resample=Image.Resampling.BILINEAR):
    return source.transform(size, Image.Transform.EXTENT, box, resample)


def _cost(a, b):
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


def align_still(original, target, max_shift=0.12, scales=(0.8, 1.25)):
    """The original painting framed to match the animated clip's anchor frame.

    The video model re-frames the painting slightly (scale and position), so a plain cover-crop
    of the original sits a few pixels off the clip: dissolving between them shows every outline
    twice. This searches the crop box (scale + offset; coarse grid, then refinement) that best
    matches the anchor frame, and renders the still from the full-resolution original with it.
    Returns (still, info). Boxes stay inside the painting; no borders are invented.
    """
    w, h = target.size
    source = ImageOps.exif_transpose(original).convert('RGB') if isinstance(original, Image.Image) else original
    sw, sh = source.size
    base = min(sw / w, sh / h) * w  # Cover-crop box width (ImageOps.fit, centred).

    def box(s, dx, dy):
        bw = base / s; bh = bw * h / w
        cx, cy = sw / 2 + dx * base, sh / 2 + dy * base
        return (cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2)

    def inside(b):
        return b[0] >= -0.5 and b[1] >= -0.5 and b[2] <= sw + 0.5 and b[3] <= sh + 0.5

    levels = []
    for small in ((68, 120), (135, 240), (270, 480)):
        factor = small[0] / w
        levels.append((small, factor, source.resize((max(1, round(sw * factor * 2)), max(1, round(sh * factor * 2))), Image.Resampling.BOX),
                       target.convert('L').resize(small, Image.Resampling.BOX)))

    def cost(level, p):
        small, factor, src, goal = levels[level]
        b = box(*p)
        if p[0] < scales[0] or p[0] > scales[1] or abs(p[1]) > max_shift or abs(p[2]) > max_shift or not inside(b):
            return math.inf
        k = src.size[0] / sw
        return _cost(_render(src, tuple(v * k for v in b), small).convert('L'), goal)

    best = min(((s / 100, dx / 100, dy / 100) for s in range(round(scales[0] * 100), round(scales[1] * 100) + 1, 2)
                for dx in range(-12, 13, 2) for dy in range(-12, 13, 2)), key=lambda p: cost(0, p))
    for level, steps in ((1, (0.01, 0.005)), (2, (0.004, 0.002, 0.001, 0.0005))):
        current = cost(level, best)
        for step in steps:
            improved = True
            while improved:
                improved = False
                for axis in range(3):
                    for sign in (-1, 1):
                        trial = list(best); trial[axis] += sign * step; trial = tuple(trial)
                        value = cost(level, trial)
                        if value < current - 1e-9:
                            best, current, improved = trial, value, True
    still = _render(source, box(*best), (w, h), Image.Resampling.BICUBIC)
    plain = ImageOps.fit(source, (w, h), method=Image.Resampling.LANCZOS, centering=(.5, .5))
    info = {'scale': round(best[0], 4), 'dx': round(best[1], 4), 'dy': round(best[2], 4),
            'before': frame_difference(plain, target), 'after': frame_difference(still, target)}
    if info['after'] >= info['before']:  # Alignment never makes the match worse.
        return plain, {**info, 'scale': 1., 'dx': 0., 'dy': 0., 'after': info['before'], 'used': 'cover_crop'}
    return still, {**info, 'used': 'aligned'}


def camera(frame, zoom=1., x=0., y=0.):
    """Apply the camera framing to a whole scene frame (still, moving or a dissolve of both)."""
    if zoom == 1 and x == 0 and y == 0:
        return frame
    w, h = frame.size
    cw, ch = w / zoom, h / zoom
    left, top = (w - cw) / 2 * (1 + PAN * x), (h - ch) / 2 * (1 + PAN * y)
    return frame.resize((w, h), Image.Resampling.BICUBIC, box=(left, top, left + cw, top + ch))


def frame_difference(a, b, size=JOIN_SIZE):
    """Mean absolute grey difference (0 identical … 255) on a small proxy."""
    x, y = (i.convert('L').resize(size, Image.Resampling.BILINEAR) for i in (a, b))
    return round(ImageStat.Stat(ImageChops.difference(x, y)).mean[0], 3)


def clip_seconds(path):
    info = json.loads(run_bounded(['ffprobe', '-v', 'error', *input_options(path, video=True), '-select_streams', 'v:0',
        '-show_entries', 'stream=duration:format=duration', '-of', 'json', str(path)], 30, 65536).stdout)
    streams = info.get('streams') or [{}]
    value = float(streams[0].get('duration') or info.get('format', {}).get('duration') or 0)
    require(math.isfinite(value) and 7.5 <= value <= 12, 'The animated clip is not the expected 10 seconds.')
    return value


def native_fps(path):
    info = json.loads(run_bounded(['ffprobe', '-v', 'error', *input_options(path, video=True), '-select_streams', 'v:0',
        '-show_entries', 'stream=avg_frame_rate,r_frame_rate', '-of', 'json', str(path)], 30, 65536).stdout)
    stream = (info.get('streams') or [{}])[0]
    for key in ('avg_frame_rate', 'r_frame_rate'):
        try:
            top, _, bottom = str(stream.get(key, '')).partition('/')
            value = float(top) / float(bottom or 1)
            if 10 <= value <= 120:
                return value
        except (ValueError, ZeroDivisionError):
            pass
    return None


def clip_size(path):
    info = json.loads(run_bounded(['ffprobe', '-v', 'error', *input_options(path, video=True), '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height', '-of', 'json', str(path)], 30, 65536).stdout)
    stream = (info.get('streams') or [{}])[0]
    width, height = stream.get('width'), stream.get('height')
    require(type(width) is int and type(height) is int and width > 0 and height > 0, 'The animated clip could not be measured.')
    return width, height

def art_box(clip_width, clip_height, width, height):
    """Where the painting sits in the frame: (art_width, art_height, left, top, framing).

    The whole painting is always shown. Only a painting already close to the frame's shape fills it edge
    to edge, losing at most COVER_LOSS of its area; any other shape is scaled to fit on the charcoal ground,
    kept above the caption lines where it is short enough to leave room."""
    cover = max(width / clip_width, height / clip_height)
    if 1 - (width * height) / (clip_width * cover * clip_height * cover) <= COVER_LOSS:
        return width, height, 0, 0, 'fill'
    scale = min(width / clip_width, height / clip_height)
    art_width, art_height = (max(2, round(v * scale / 2) * 2) for v in (clip_width, clip_height))
    top = min((height - art_height) // 2, max(0, round(height * CAPTION_TOP) - art_height))
    return art_width, art_height, (width - art_width) // 2, top, 'whole'

def anchor_frame(path, width, height):
    """The clip's first frame (its anchor), cover-cropped exactly like ClipFrames."""
    vf = f'scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},setsar=1'
    raw = run_bounded(['ffmpeg', '-nostdin', '-v', 'error', *input_options(path, video=True), '-i', str(path), '-map', '0:v:0',
        '-frames:v', '1', '-an', '-vf', vf, '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'], 60, width * height * 3 + 1).stdout
    require(len(raw) == width * height * 3, 'The animated clip could not be decoded.')
    return Image.frombytes('RGB', (width, height), raw)


class ClipFrames:
    """Every clip frame, cover-cropped at the canvas rate, kept compressed in memory for random access
    (a movement may play back toward its anchor). Slowed motion blends the two nearest source frames."""
    def __init__(self, path, width, height, fps):
        # Native frame rate: forcing a 25/30 fps clip to the canvas rate drops frames and makes motion hitch;
        # sampling by time at the native rate (blending neighbours) keeps it smooth.
        self.fps, self.frames, self.cache = native_fps(path) or fps, [], {}
        size, dimensions = width * height * 3, (width, height)
        vf = f'scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},setsar=1'
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', *input_options(path, video=True),
                '-i', str(path), '-map', '0:v:0', '-an', '-vf', vf, '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                stdout=subprocess.PIPE, stderr=errors)
            try:
                while len(raw := process.stdout.read(size)) == size:
                    buffer = io.BytesIO()
                    Image.frombytes('RGB', dimensions, raw).save(buffer, format='JPEG', quality=JPEG_QUALITY, subsampling=0)
                    self.frames.append(buffer.getvalue())
            finally:
                process.stdout.close()
                require(process.wait(timeout=120) == 0 and self.frames, 'The animated clip could not be decoded.')

    def at(self, number):
        number = min(max(0, number), len(self.frames) - 1)
        if number not in self.cache:
            if len(self.cache) >= 4:
                self.cache.pop(next(iter(self.cache)))
            self.cache[number] = Image.open(io.BytesIO(self.frames[number])).convert('RGB')
        return self.cache[number].copy()

    def sample(self, seconds):
        """Frame at a (possibly slowed or reversed) clip time: blended between the two nearest source frames."""
        x = max(0., seconds * self.fps)
        number = math.floor(x + 1e-7); weight = x - number
        first = self.at(number)
        if weight < 0.02 or number + 1 >= len(self.frames):
            return first
        return Image.blend(first, self.at(number + 1), min(1., weight))

    def close(self):
        self.frames, self.cache = [], {}


def wrap_title(text, font, max_width, max_lines=3):
    """Balanced greedy wrap; None when it needs more than max_lines at this size."""
    lines, line = [], ''
    for word in text.split():
        trial = f'{line} {word}'.strip()
        if line and font.getlength(trial) > max_width:
            lines.append(line); line = word
        else:
            line = trial
    lines.append(line)
    return lines if len(lines) <= max_lines and all(font.getlength(x) <= max_width for x in lines) else None

def title_font(size):
    """Italianno (SIL OFL, bundled and fingerprinted) for the handwritten opening card only."""
    data = TITLE_FONT_FILE.read_bytes()
    require(hashlib.sha256(data).hexdigest() == TITLE_FONT_SHA256, 'The title card font file changed.')
    return ImageFont.truetype(io.BytesIO(data), size)

def title_card(width, height, title, byline, style):
    """Charcoal card: the story title handwritten in parchment, a short gold rule, the byline handwritten in gold.
    Never narrated. Italianno has one weight, so a same-colour outline (TITLE_WEIGHT of the size) makes it bolder."""
    card = Image.new('RGB', (width, height), _colour(style, 'charcoal'))
    draw = ImageDraw.Draw(card)
    parchment, gold = _colour(style, 'parchment'), _colour(style, 'gold')
    safe = round(width * 0.86)
    for fraction in (0.095, 0.085, 0.075, 0.065, 0.055, 0.047, 0.04):  # Long titles step down in size rather than overflow.
        font = title_font(round(height * fraction))
        lines = wrap_title(title, font, safe)
        if lines:
            break
    require(lines is not None, 'The story title is too long for the opening card.')
    byline_font = title_font(round(height * 0.052))
    line_height, gap = font.size, round(height * 0.016)
    y = height / 2 - line_height * len(lines) / 2 - byline_font.size * 0.4
    for line in lines:
        draw.text((width / 2, y + line_height / 2), line, fill=parchment, font=font, anchor='mm',
                  stroke_width=max(1, round(font.size * TITLE_WEIGHT)), stroke_fill=parchment)
        y += line_height
    rule_y = y + gap
    draw.line((width / 2 - width * 0.07, rule_y, width / 2 + width * 0.07, rule_y), fill=gold, width=max(1, round(height / 640)))
    draw.text((width / 2, rule_y + gap + byline_font.size / 2), byline, fill=gold, font=byline_font, anchor='mm',
              stroke_width=max(1, round(byline_font.size * TITLE_WEIGHT)), stroke_fill=gold)
    return card

def title_level(t, length):
    """Card opacity over charcoal at t seconds into the opening: fade in, hold, fade out, short rest.
    Fades scale with the chosen length, so even a 1 s title holds fully readable for half its time."""
    fade, rest = min(0.8, length * TITLE_FADE), min(0.4, length * TITLE_REST)
    return min(ease(t / fade), ease((length - rest - t) / fade))

def compose_film(*, root, narration_wav, report, timing, scenes, output, ramp=RAMP, turn=TURN, join_threshold=18.0, style=None,
                 title=None, byline=TITLE_BYLINE, title_seconds=TITLE_SECONDS, cancel=None):
    """scenes: [{'image_id','original_path','clip_path'}] in the frozen numbered order.
    title: shown silently for title_seconds on an opening card with the byline (0 or no title: no card);
    the narration and every scene start after it."""
    require(type(title_seconds) in (int, float) and 0 <= title_seconds <= 5, 'The opening title lasts 0 to 5 seconds.')
    started = time.monotonic()
    style = style or presentation()
    w, h, fps = (style['canvas'][k] for k in ('width', 'height', 'fps'))
    root, output = Path(root).resolve(strict=True), Path(output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = output.resolve()
    require(output.suffix == '.mp4' and output.is_relative_to(root) and not output.exists(), 'Output must be a new private MP4.')
    text, duration, overlap = report['exact_text'], timing['duration_seconds'], style['transitions']['crossfade_seconds']
    require(abs(report['duration_seconds'] - duration) <= .001 and abs(timing['crossfade_seconds'] - overlap) < 1e-9
            and len(scenes) == len(timing['scenes']), 'Scene timing differs from the narration or portrait style.')
    words = validate_words(text, report['words'], duration)
    captions = make_captions(text, words, style=style)
    require(re.sub(r'\s+', '', ''.join(c.text for c in captions)) == re.sub(r'\s+', '', text), 'Caption wording changed.')
    font = _font(round(h * style['captions']['font_height_fraction']), style)
    for caption in captions:
        wrap_caption(caption.text, font, round(w * style['captions']['safe_width_fraction']))
    # The film does not end on the last word: the final painting lingers in silence, then fades out.
    voice, duration = duration, duration + TAIL
    # Optional silent opening card; everything after it (pictures, voice, captions) moves later by `intro`.
    title = ' '.join((title or '').split()) if title_seconds else ''
    intro = float(title_seconds) if title else 0.
    card = title_card(w, h, title, byline, style) if title else None
    items = []
    for number, (scene, item) in enumerate(zip(timing['scenes'], scenes), 1):
        require(scene['image_id'] == item['image_id'], 'Scene order differs from the numbered paintings.')
        length = clip_seconds(item['clip_path'])
        end = scene['end_seconds'] + (TAIL if number == len(scenes) else 0)
        # Frame the still exactly like the clip's anchor so the dissolve never shows outlines twice.
        box = art_box(*clip_size(item['clip_path']), w, h)  # The whole painting, never half of it.
        with Image.open(item['original_path']) as original:
            still, alignment = align_still(ImageOps.exif_transpose(original).convert('RGB'), anchor_frame(item['clip_path'], box[0], box[1]))
        items.append({'image_id': item['image_id'], 'start': scene['start_seconds'], 'end': end,
                      'plan': scene_plan(end - scene['start_seconds'], length, ramp, turn),
                      'still': still, 'alignment': alignment, 'clip': item['clip_path'], 'box': box})
    readers, joins = {}, []
    ground = Image.new('RGB', (w, h), _colour(style, 'charcoal'))

    def scene_frame(index, local):
        art = camera(scene_picture(index, local), *drift_at(items[index]['plan'], local, index))
        art_width, art_height, left, top, framing = items[index]['box']
        if framing == 'fill':
            return art
        frame = ground.copy(); frame.paste(art, (left, top))
        return frame

    def scene_picture(index, local):
        item = items[index]
        part, clip_time, alpha = state_at(item['plan'], local)
        if part is None:
            return item['still'].copy()
        if index not in readers:
            readers[index] = ClipFrames(item['clip'], item['box'][0], item['box'][1], fps)
        clip = readers[index].sample(clip_time)
        if alpha >= 1:
            return clip
        background = item['still']
        s = item['plan']['segments'][part]
        into = s.fade_out and s.end - local <= 1 / fps + 1e-9
        if into or (s.fade_in and local - s.at < 1 / fps - 1e-9):
            difference = frame_difference(clip, background)
            joins.append({'scene': index + 1, 'image_id': item['image_id'], 'part': part + 1,
                          'join': 'clip_to_still' if into else 'still_to_clip', 'scene_seconds': round(local, 3),
                          'clip_seconds': round(clip_time, 3), 'difference': difference, 'flagged': difference > join_threshold})
        return Image.blend(background, clip, alpha)

    enc, capstarts = style['encoding'], [c.start for c in captions]
    total, charcoal = math.ceil((intro + duration) * fps - 1e-9), Image.new('RGB', (w, h), _colour(style, 'charcoal'))
    with tempfile.TemporaryDirectory(prefix='lean-', dir=output.parent) as temp:
        encoded = Path(temp) / 'film.mp4'
        with (Path(temp) / 'encoder.log').open('wb') as errors:
            command = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', str(fps),
                '-i', 'pipe:0', *input_options(narration_wav, video=False), '-i', str(narration_wav), '-map', '0:v:0', '-map', '1:a:0',
                '-c:v', enc['video_codec'], '-preset', enc['video_preset'], '-crf', str(enc['crf']), '-threads', str(enc['encoder_threads']),
                '-pix_fmt', enc['pixel_format'], '-colorspace', style['canvas']['colour_space'], '-color_primaries', style['canvas']['colour_space'],
                '-color_trc', style['canvas']['colour_space'],
                '-af', (f'adelay={round(intro * 1000)}:all=1,' if intro else '') + f'apad=pad_dur={TAIL}', '-c:a', enc['audio_codec'], '-b:a', enc['audio_bitrate'],
                '-movflags', '+faststart', '-t', f'{intro + duration:.9f}', str(encoded)]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                for number in range(total):
                    if cancel is not None and number % (fps * 2) == 0:
                        cancel()  # Owner's Stop: raises, and the encoder and partial output are discarded below.
                    t = number / fps - intro
                    if t < 0:  # Opening card: silent, no captions; ends on charcoal before the first painting fades in.
                        level = title_level(number / fps, intro)
                        process.stdin.write((Image.blend(charcoal, card, level) if level > 0 else charcoal).tobytes())
                        continue
                    active = [i for i, item in enumerate(items) if item['start'] <= t < item['end']][-2:] or [len(items) - 1]
                    for old in set(readers) - set(active):
                        readers.pop(old).close()
                    frames = [scene_frame(i, t - items[i]['start']) for i in active]
                    frame = frames[-1]
                    if len(frames) == 2:  # Unchanged 0.7 s scene crossfade.
                        frame = Image.blend(frames[0], frames[1], ease((t - items[active[1]]['start']) / overlap))
                    level = min(1, t / style['transitions']['opening_fade_seconds'],
                                max(0, ease((duration - t) / max(CLOSING_FADE, style['transitions']['closing_fade_seconds']))))
                    if level < 1:
                        frame = Image.blend(charcoal, frame, level)
                    ci = bisect.bisect_right(capstarts, t) - 1
                    # Hold a caption through a storyteller pause rather than flickering off between sentences.
                    held = ci >= 0 and (t <= captions[ci].end or (ci + 1 < len(captions) and captions[ci + 1].start - captions[ci].end <= CAPTION_HOLD))
                    draw_caption(frame, captions[ci] if held else None, font, style)
                    process.stdin.write(frame.tobytes())
                process.stdin.close()
                require(process.wait(timeout=600) == 0, 'Encoding failed; incomplete output discarded.')
            except BaseException:
                process.kill(); process.wait()
                raise
            finally:
                for reader in readers.values():
                    reader.close()
        info = json.loads(run(['ffprobe', '-v', 'error', *input_options(encoded, video=True), '-show_format', '-show_streams', '-of', 'json', str(encoded)], 30).stdout)
        videos = [x for x in info['streams'] if x['codec_type'] == 'video']; sounds = [x for x in info['streams'] if x['codec_type'] == 'audio']
        require(len(videos) == len(sounds) == 1 and videos[0]['codec_name'] == 'h264' and sounds[0]['codec_name'] == 'aac'
                and (videos[0]['width'], videos[0]['height']) == (w, h) and abs(float(info['format']['duration']) - (intro + duration)) <= 1 / fps + .05,
                'Export verification failed.')
        run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', *input_options(encoded, video=True), '-i', str(encoded), '-f', 'null', '-'], 600)
        encoded.replace(output)
    write_srt(output.with_suffix('.srt'), [Caption(c.text, c.start + intro, c.end + intro) for c in captions])
    data = output.read_bytes()
    return {'schema_version': 1, 'composition': 'lean-anchored-v4-aligned', 'width': w, 'height': h, 'fps': fps,
            'duration_seconds': intro + duration, 'voice_seconds': voice, 'tail_seconds': TAIL,
            'title_card': {'title': title, 'byline': byline, 'seconds': intro} if title else None, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
            'crossfade_seconds': overlap, 'ramp_seconds': ramp, 'turn_seconds': turn, 'still_motion': 'eased camera breath (max 5%)', 'caption_count': len(captions),
            'scenes': [{'image_id': i['image_id'], 'start_seconds': i['start'], 'end_seconds': i['end'], 'rule': i['plan']['rule'],
                        'clip_seconds': i['plan']['clip_seconds'], 'alignment': i['alignment'], 'framing': i['box'][4],
                        'art_box': list(i['box'][:4]), 'parts': [asdict(s) for s in i['plan']['segments']]} for i in items],
            'joins': joins, 'join_threshold': join_threshold, 'flagged_joins': sum(j['flagged'] for j in joins),
            'source_order': [i['image_id'] for i in items], 'loop': False, 'rewind': 'returns to anchor', 'letterbox': any(i['box'][4] == 'whole' for i in items),
            'elapsed_seconds': round(time.monotonic() - started, 2)}
