"""Lean "5/2/3" portrait composition: animated clip parts around the original still.

Scene rule (scene length D from the narration split, clip length L ≈ 10 s, ramp R = 1.5 s):
  D >= 15 s         full:         clip 0–5 s at the scene start, clip 5–7 s centred on D/2,
                                  clip 7–L s ending exactly at the scene end.
  10.5 <= D < 15 s  two_part:     clip 0–5 s at the start and clip 7–L s ending at the scene end.
  D < 10.5 s        opening_only: clip 0–a s at the start, then the still until the scene end.
Wherever a part meets the still, the motion never stops or starts abruptly: over R seconds
its playback speed eases smoothly to zero (or up from zero) while it dissolves into (or out
of) the still, so the subject settles into the painting. A ramp of R output seconds uses
R/2 clip seconds, so a part lasts (clip span) + (ramps)/2. Where a part's clip end is the
original painting (clip t=0 starts the scene; clip t=L ends it) no ramp is needed.
Between parts the ORIGINAL painting is shown perfectly still: no zoom, pan or drift.
A later part never overlaps an earlier one; if it would, it starts later in the clip.
Clip frames are read forward only (no rewind or loop); slowed frames are blended between
neighbouring source frames instead of repeated. Every clip/still join is measured (mean
absolute grey difference, 0–255) and recorded; joins above the threshold are flagged.
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

RAMP = 1.5
FULL_SCENE = 15.0
TWO_PART_SCENE = 10.5
JOIN_SIZE = (135, 240)


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
    at: float
    clip_from: float
    clip_to: float
    ramp_in: float
    ramp_out: float

    @property
    def length(self):
        return self.clip_to - self.clip_from + (self.ramp_in + self.ramp_out) / 2

    @property
    def end(self):
        return self.at + self.length

    def clip_time(self, local):
        """Clip seconds shown at `local` output seconds into the part: eased in and out, never backwards."""
        if self.ramp_in and local < self.ramp_in:
            return self.clip_from + self.ramp_in * eased_distance(local / self.ramp_in)
        left = self.length - local
        if self.ramp_out and left < self.ramp_out:
            return self.clip_to - self.ramp_out * eased_distance(left / self.ramp_out)
        return self.clip_from + self.ramp_in / 2 + (local - self.ramp_in)


def scene_plan(duration, clip_seconds=10.0, ramp=RAMP):
    D, L, R = float(duration), float(clip_seconds), float(ramp)
    require(math.isfinite(D) and D > 0 and math.isfinite(L) and 7.5 <= L <= 12 and 0 < R <= 3, 'Invalid scene or clip timing.')
    tail = (L - 7) + R / 2  # Output length of the final part (clip 7–L, eased in only).
    if D >= FULL_SCENE:
        rule = 'full'
        parts = [(0., 0., 5., 0., R), (D / 2 - (2 + R) / 2, 5., 7., R, R), (D - tail, 7., L, R, 0.)]
    elif D >= TWO_PART_SCENE:
        rule, parts = 'two_part', [(0., 0., 5., 0., R), (D - tail, 7., L, R, 0.)]
    else:
        r = min(R, 0.4 * D)
        a = max(r / 2, min(5., D - r / 2 - min(1., 0.2 * D)))
        rule, parts = 'opening_only', [(0., 0., a, 0., r)]
    segments, previous = [], 0.
    for at, start, stop, ramp_in, ramp_out in parts:
        if at < previous:  # Start later in the clip instead of overlapping; order is kept.
            start, at = start + previous - at, previous
        require(stop - start >= (ramp_in + ramp_out) / 2 - 1e-9 and stop > start, 'The scene is too short for its clip part.')
        segment = Segment(at, start, stop, ramp_in, ramp_out)
        segments.append(segment)
        previous = segment.end
    require(previous <= D + 1e-6, 'Clip parts exceed the scene.')
    return {'rule': rule, 'duration': D, 'clip_seconds': L, 'ramp': R, 'segments': segments}


def still_windows(plan):
    segments = plan['segments']
    windows = [(a.end - a.ramp_out, b.at + b.ramp_in) for a, b in zip(segments, segments[1:])]
    last = segments[-1]
    if last.end < plan['duration'] - 1e-6:
        windows.append((last.end - last.ramp_out, plan['duration']))
    return windows


def state_at(plan, t):
    """(part index or None, clip seconds, clip opacity over the still) at scene time t."""
    for index, s in enumerate(plan['segments']):
        if s.at - 1e-9 <= t < s.end:
            local, alpha = t - s.at, 1.
            if s.ramp_in:
                alpha = min(alpha, ease(local / s.ramp_in))
            if s.ramp_out:
                alpha = min(alpha, ease((s.end - t) / s.ramp_out))
            return index, s.clip_time(local), alpha
    return None, None, 0.


def still_frame(path, size):
    """The original painting, cover-cropped (centre protected); shown perfectly still."""
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(.5, .5))


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
        self.size, self.dimensions, self.index, self.frame, self.previous = width * height * 3, (width, height), -1, None, None
        self.fps = fps
        self.errors = tempfile.TemporaryFile()
        vf = f'fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},setsar=1'
        self.process = subprocess.Popen(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', *input_options(path, video=True),
            '-i', str(path), '-map', '0:v:0', '-an', '-vf', vf, '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=self.errors)

    def at(self, number):
        if number == self.index - 1 and self.previous is not None:
            return self.previous.copy()  # Kept only for blending slowed motion; never a rewind.
        require(number >= self.index, 'Clip frames are used once and in order; no rewind or loop.')
        while self.index < number:
            raw = self.process.stdout.read(self.size)
            if len(raw) != self.size:
                require(self.frame is not None and number - self.index <= 2, 'The clip ended early; no frozen frame substituted.')
                break
            self.previous, self.frame = self.frame, Image.frombytes('RGB', self.dimensions, raw); self.index += 1
        return self.frame.copy()

    def sample(self, seconds):
        """Frame at a (possibly slowed) clip time: blended between the two nearest source frames."""
        x = max(0., seconds * self.fps)
        number = math.floor(x + 1e-7); weight = x - number
        first = self.at(number)
        if weight < 0.02:
            return first
        return Image.blend(first, self.at(number + 1), min(1., weight))

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait()
        self.process.stdout.close(); self.errors.close()


def compose_film(*, root, narration_wav, report, timing, scenes, output, ramp=RAMP, join_threshold=18.0, style=None):
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
                      'plan': scene_plan(scene['end_seconds'] - scene['start_seconds'], length, ramp),
                      'still': still_frame(item['original_path'], (w, h)), 'clip': item['clip_path']})
    readers, joins = {}, []

    def scene_frame(index, local):
        item = items[index]
        part, clip_time, alpha = state_at(item['plan'], local)
        if part is None:
            return item['still'].copy()
        if index not in readers:
            readers[index] = ClipReader(item['clip'], w, h, fps)
        clip = readers[index].sample(clip_time)
        if alpha >= 1:
            return clip
        background = item['still']
        s = item['plan']['segments'][part]
        into = s.ramp_out and s.end - local <= 1 / fps + 1e-9
        if into or (s.ramp_in and local - s.at < 1 / fps - 1e-9):
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
    return {'schema_version': 1, 'composition': 'lean-5-2-3-v2-eased', 'width': w, 'height': h, 'fps': fps,
            'duration_seconds': duration, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
            'crossfade_seconds': overlap, 'ramp_seconds': ramp, 'still_motion': 'none', 'caption_count': len(captions),
            'scenes': [{'image_id': i['image_id'], 'start_seconds': i['start'], 'end_seconds': i['end'], 'rule': i['plan']['rule'],
                        'clip_seconds': i['plan']['clip_seconds'], 'parts': [asdict(s) for s in i['plan']['segments']]} for i in items],
            'joins': joins, 'join_threshold': join_threshold, 'flagged_joins': sum(j['flagged'] for j in joins),
            'source_order': [i['image_id'] for i in items], 'loop': False, 'rewind': False, 'letterbox': False,
            'elapsed_seconds': round(time.monotonic() - started, 2)}
