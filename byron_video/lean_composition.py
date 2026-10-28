"""Lean "5/2/3" portrait composition: animated clip parts around the original still.

Scene rule (scene length D from the narration split, clip length L ≈ 10 s, fade X = 0.8 s):
  D >= 12 s       full:         clip 0–5 s at the scene start, clip 5–7 s centred on D/2,
                                clip 7–L s ending exactly at the scene end.
  8 <= D < 12 s   two_part:     clip 0–5 s at the start and clip 7–L s ending at the scene end.
  D < 8 s         opening_only: clip 0–a s at the start, a = min(5, max(D − 2X, min(D, X))),
                                then the still until the scene end.
Between clip parts the ORIGINAL painting is shown with a slow drift (zoom + pan) whose
size follows sin², so it starts and ends at the exact original framing with zero speed.
Clip parts fade X seconds into and out of the still, except the scene's first part (clip
t=0 is the original, so it starts directly) and a part that ends with the scene (clip
t=L is the original again, and the 0.7 s scene crossfade follows). A later part never
overlaps an earlier one; if it would, it starts later in the clip. Clip frames are read
once, forward only: no rewind, loop or retime beyond conversion to the 24 fps canvas.
Every clip/still join is measured (mean absolute grey difference, 0–255) and recorded;
joins above the threshold are flagged, never failed.
"""
import bisect
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time

from PIL import Image, ImageChops, ImageOps, ImageStat

from .engine import _colour, _font, draw_caption, make_captions, wrap_caption, write_srt
from .portrait_composition import input_options, presentation, run, run_bounded, validate_words

FADE = 0.8
FULL_SCENE = 12.0
TWO_PART_SCENE = 8.0
ZOOM_RATE = 0.004  # Zoom amplitude per second of still window: slow by construction.
MAX_ZOOM = 0.05
PAN = 0.8  # Fraction of the zoom margin used for panning; keeps the frame inside the painting.
DIRECTIONS = ((1, -1), (-1, -1), (1, 1), (-1, 1))
JOIN_SIZE = (135, 240)
STILL_SCALE = 1.5


def require(ok, message):
    if not ok:
        raise ValueError(message)


def ease(p):
    p = min(1., max(0., p))
    return p * p * (3 - 2 * p)


@dataclass(frozen=True)
class Segment:
    at: float
    clip_from: float
    clip_to: float
    fade_in: float
    fade_out: float

    @property
    def end(self):
        return self.at + self.clip_to - self.clip_from


def scene_plan(duration, clip_seconds=10.0, fade=FADE):
    D, L, X = float(duration), float(clip_seconds), float(fade)
    require(math.isfinite(D) and D > 0 and math.isfinite(L) and 7.5 <= L <= 12 and 0 < X <= 2, 'Invalid scene or clip timing.')
    if D >= FULL_SCENE:
        rule, parts = 'full', [(0., 0., 5.), (D / 2 - 1, 5., 7.), (D - (L - 7), 7., L)]
    elif D >= TWO_PART_SCENE:
        rule, parts = 'two_part', [(0., 0., 5.), (D - (L - 7), 7., L)]
    else:
        rule, parts = 'opening_only', [(0., 0., min(5., max(D - 2 * X, min(D, X))))]
    segments, previous = [], 0.
    for index, (at, start, stop) in enumerate(parts):
        if at < previous:  # Start later in the clip instead of overlapping; order is kept.
            start, at = start + previous - at, previous
        length = stop - start
        require(length > 1e-6, 'The scene is too short for its clip part.')
        ends_scene = at + length >= D - 1e-6
        fade_in = 0. if index == 0 else min(X, length / 2)
        fade_out = 0. if ends_scene else min(X, length if index == 0 else length / 2)
        segments.append(Segment(at, start, stop, fade_in, fade_out))
        previous = at + length
    require(previous <= D + 1e-6, 'Clip parts exceed the scene.')
    return {'rule': rule, 'duration': D, 'clip_seconds': L, 'fade': X, 'segments': segments}


def still_windows(plan):
    segments = plan['segments']
    windows = [(a.end - a.fade_out, b.at + b.fade_in) for a, b in zip(segments, segments[1:])]
    last = segments[-1]
    if last.end < plan['duration'] - 1e-6:
        windows.append((last.end - last.fade_out, plan['duration']))
    return windows


def state_at(plan, t):
    """(part index or None, clip seconds, clip opacity over the still) at scene time t."""
    for index, s in enumerate(plan['segments']):
        if s.at - 1e-9 <= t < s.end:
            local, alpha = t - s.at, 1.
            if s.fade_in:
                alpha = min(alpha, ease(local / s.fade_in))
            if s.fade_out:
                alpha = min(alpha, ease((s.end - t) / s.fade_out))
            return index, s.clip_from + local, alpha
    return None, None, 0.


def drift_at(plan, t, index):
    """(zoom, x, y) for the still; exactly (1, 0, 0) at both ends of each window."""
    for start, stop in still_windows(plan):
        if stop > start and start <= t <= stop:
            e = math.sin(math.pi * (t - start) / (stop - start)) ** 2
            dx, dy = DIRECTIONS[index % len(DIRECTIONS)]
            return 1 + min(MAX_ZOOM, ZOOM_RATE * (stop - start)) * e, dx * e, dy * e
    return 1., 0., 0.


def still_base(path, width, height, scale=STILL_SCALE):
    """Cover-crop (centre protected) the original, oversampled for a sharp drift."""
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        return ImageOps.fit(image, (round(width * scale), round(height * scale)), method=Image.Resampling.LANCZOS, centering=(.5, .5))


def still_frame(base, size, zoom=1., x=0., y=0.):
    bw, bh = base.size
    cw, ch = bw / zoom, bh / zoom
    left, top = (bw - cw) / 2 * (1 + PAN * x), (bh - ch) / 2 * (1 + PAN * y)
    return base.resize(size, Image.Resampling.BICUBIC, box=(left, top, left + cw, top + ch))


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


class ClipReader:
    """Forward-only cover-cropped decode at the canvas rate; EOF holds at most 2 frames."""
    def __init__(self, path, width, height, fps):
        self.size, self.dimensions, self.index, self.frame = width * height * 3, (width, height), -1, None
        self.errors = tempfile.TemporaryFile()
        vf = f'fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},setsar=1'
        self.process = subprocess.Popen(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', *input_options(path, video=True),
            '-i', str(path), '-map', '0:v:0', '-an', '-vf', vf, '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=self.errors)

    def at(self, number):
        require(number >= self.index, 'Clip frames are used once and in order; no rewind or loop.')
        while self.index < number:
            raw = self.process.stdout.read(self.size)
            if len(raw) != self.size:
                require(self.frame is not None and number - self.index <= 2, 'The clip ended early; no frozen frame substituted.')
                break
            self.frame = Image.frombytes('RGB', self.dimensions, raw); self.index += 1
        return self.frame.copy()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait()
        self.process.stdout.close(); self.errors.close()


def compose_film(*, root, narration_wav, report, timing, scenes, output, fade=FADE, join_threshold=18.0, style=None):
    """scenes: [{'image_id','original_path','clip_path'}] in the frozen numbered order."""
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
    items = []
    for scene, item in zip(timing['scenes'], scenes):
        require(scene['image_id'] == item['image_id'], 'Scene order differs from the numbered paintings.')
        length = clip_seconds(item['clip_path'])
        items.append({'image_id': item['image_id'], 'start': scene['start_seconds'], 'end': scene['end_seconds'],
                      'plan': scene_plan(scene['end_seconds'] - scene['start_seconds'], length, fade),
                      'base': still_base(item['original_path'], w, h), 'clip': item['clip_path'], 'identity': None})
    readers, joins = {}, []

    def still(index, local):
        item = items[index]
        zoom, x, y = drift_at(item['plan'], local, index)
        if zoom == 1 and x == 0 and y == 0:
            if item['identity'] is None:
                item['identity'] = still_frame(item['base'], (w, h))
            return item['identity'].copy()
        return still_frame(item['base'], (w, h), zoom, x, y)

    def scene_frame(index, local):
        item = items[index]
        part, clip_time, alpha = state_at(item['plan'], local)
        if part is None:
            return still(index, local)
        if index not in readers:
            readers[index] = ClipReader(item['clip'], w, h, fps)
        clip = readers[index].at(math.floor(clip_time * fps + 1e-7))
        if alpha >= 1:
            return clip
        background = still(index, local)
        s = item['plan']['segments'][part]
        into = s.fade_out and s.end - local <= 1 / fps + 1e-9
        if into or (s.fade_in and local - s.at < 1 / fps - 1e-9):
            difference = frame_difference(clip, background)
            joins.append({'scene': index + 1, 'image_id': item['image_id'], 'part': part + 1,
                          'join': 'clip_to_still' if into else 'still_to_clip', 'scene_seconds': round(local, 3),
                          'clip_seconds': round(clip_time, 3), 'difference': difference, 'flagged': difference > join_threshold})
        return Image.blend(background, clip, alpha)

    enc, capstarts = style['encoding'], [c.start for c in captions]
    total = math.ceil(duration * fps - 1e-9)
    with tempfile.TemporaryDirectory(prefix='lean-', dir=output.parent) as temp:
        encoded = Path(temp) / 'film.mp4'
        with (Path(temp) / 'encoder.log').open('wb') as errors:
            command = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', str(fps),
                '-i', 'pipe:0', *input_options(narration_wav, video=False), '-i', str(narration_wav), '-map', '0:v:0', '-map', '1:a:0',
                '-c:v', enc['video_codec'], '-preset', enc['video_preset'], '-crf', str(enc['crf']), '-threads', str(enc['encoder_threads']),
                '-pix_fmt', enc['pixel_format'], '-colorspace', style['canvas']['colour_space'], '-color_primaries', style['canvas']['colour_space'],
                '-color_trc', style['canvas']['colour_space'], '-c:a', enc['audio_codec'], '-b:a', enc['audio_bitrate'],
                '-movflags', '+faststart', '-t', f'{duration:.9f}', str(encoded)]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                for number in range(total):
                    t = number / fps
                    active = [i for i, item in enumerate(items) if item['start'] <= t < item['end']][-2:] or [len(items) - 1]
                    for old in set(readers) - set(active):
                        readers.pop(old).close()
                    frames = [scene_frame(i, t - items[i]['start']) for i in active]
                    frame = frames[-1]
                    if len(frames) == 2:  # Unchanged 0.7 s scene crossfade.
                        frame = Image.blend(frames[0], frames[1], ease((t - items[active[1]]['start']) / overlap))
                    level = min(1, t / style['transitions']['opening_fade_seconds'],
                                max(0, (duration - t) / style['transitions']['closing_fade_seconds']))
                    if level < 1:
                        frame = Image.blend(Image.new('RGB', (w, h), _colour(style, 'charcoal')), frame, level)
                    ci = bisect.bisect_right(capstarts, t) - 1
                    draw_caption(frame, captions[ci] if ci >= 0 and t <= captions[ci].end else None, font, style)
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
                and (videos[0]['width'], videos[0]['height']) == (w, h) and abs(float(info['format']['duration']) - duration) <= 1 / fps + .05,
                'Export verification failed.')
        run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', *input_options(encoded, video=True), '-i', str(encoded), '-f', 'null', '-'], 600)
        encoded.replace(output)
    write_srt(output.with_suffix('.srt'), captions)
    data = output.read_bytes()
    return {'schema_version': 1, 'composition': 'lean-5-2-3-v1', 'width': w, 'height': h, 'fps': fps,
            'duration_seconds': duration, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
            'crossfade_seconds': overlap, 'fade_seconds': fade, 'caption_count': len(captions),
            'scenes': [{'image_id': i['image_id'], 'start_seconds': i['start'], 'end_seconds': i['end'], 'rule': i['plan']['rule'],
                        'clip_seconds': i['plan']['clip_seconds'], 'parts': [asdict(s) for s in i['plan']['segments']]} for i in items],
            'joins': joins, 'join_threshold': join_threshold, 'flagged_joins': sum(j['flagged'] for j in joins),
            'source_order': [i['image_id'] for i in items], 'loop': False, 'rewind': False, 'letterbox': False,
            'elapsed_seconds': round(time.monotonic() - started, 2)}
