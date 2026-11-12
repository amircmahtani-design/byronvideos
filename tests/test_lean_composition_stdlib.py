"""Lean anchored-motion timing math and join metric; the real render runs only where ffmpeg exists."""
import json
import math
import re
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from PIL import Image

from byron_video.lean_composition import (DEPTH, FADE, ONE_PASS_SCENE, RAMP, TITLE_REST, TITLE_SECONDS, TURN, TWO_MOMENTS_SCENE,
                                          align_still, camera, drift_at, frame_difference, scene_plan, state_at, still_frame,
                                          still_windows, title_card, title_level)

ANCHOR_TOLERANCE = 1e-6


def still_frame_from(image, size):
    from PIL import ImageOps
    return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(.5, .5))


def samples(s, count=200):
    return [s.clip_time(s.length * i / count) for i in range(count + 1)]


class SceneRuleChecks(unittest.TestCase):
    def assert_anchored(self, plan):
        """Motion only meets the still at clip anchors 0 or L (both the original painting); legs never overlap."""
        L, previous_end = plan['clip_seconds'], 0.
        for s in plan['segments']:
            self.assertGreaterEqual(s.at, previous_end - 1e-9)
            times = samples(s)
            step = (lambda a, b: a - b) if s.reverse else (lambda a, b: b - a)
            self.assertTrue(all(step(a, b) >= -1e-9 for a, b in zip(times, times[1:])))  # Monotonic within a leg.
            self.assertTrue(all(-1e-9 <= t <= L + 1e-9 for t in times))
            for edge, fade in ((times[0], s.fade_in), (times[-1], s.fade_out)):
                if fade:  # Wherever the clip dissolves with the still, it shows an anchor frame.
                    self.assertTrue(min(abs(edge), abs(edge - L)) < ANCHOR_TOLERANCE, (edge, plan['rule']))
            previous_end = s.end
        for a, b in zip(plan['segments'], plan['segments'][1:]):  # A turn joins two legs seamlessly.
            if not a.fade_out:
                self.assertAlmostEqual(a.end, b.at); self.assertAlmostEqual(samples(a)[-1], samples(b)[0])
        self.assertLessEqual(previous_end, plan['duration'] + 1e-9)

    def test_long_scene_has_two_different_moments_that_return_to_the_painting(self):
        plan = scene_plan(40, 10)
        self.assertEqual(plan['rule'], 'two_moments')
        out, back, late_out, late_back = plan['segments']
        self.assertEqual((out.clip_from, out.clip_to, out.reverse), (0, DEPTH, False))
        self.assertEqual((back.clip_from, back.clip_to, back.reverse), (0, DEPTH, True))
        self.assertEqual((late_out.clip_from, late_out.clip_to, late_out.reverse), (10 - DEPTH, 10, True))
        self.assertEqual((late_back.clip_from, late_back.clip_to, late_back.reverse), (10 - DEPTH, 10, False))
        self.assertAlmostEqual(out.at, 0); self.assertAlmostEqual(late_back.end, 40)
        self.assertEqual((out.ramp_in, out.ramp_out, back.ramp_in, back.ramp_out), (RAMP, TURN, TURN, RAMP))
        self.assertEqual(len(still_windows(plan)), 1)  # One long rest between the two moments.
        self.assert_anchored(plan)
        self.assertEqual(scene_plan(TWO_MOMENTS_SCENE, 10)['rule'], 'two_moments')
        self.assertEqual(scene_plan(TWO_MOMENTS_SCENE - .01, 10)['rule'], 'one_pass')

    def test_medium_scene_plays_the_clip_once_from_painting_to_painting(self):
        plan = scene_plan(18, 10)
        self.assertEqual(plan['rule'], 'one_pass')
        (s,) = plan['segments']
        self.assertEqual((s.clip_from, s.clip_to, s.reverse, s.fade_in, s.fade_out), (0, 10, False, FADE, FADE))
        self.assertAlmostEqual(s.at + s.length / 2, 9)  # Centred in the scene.
        self.assert_anchored(plan)
        self.assertEqual(scene_plan(ONE_PASS_SCENE, 10)['rule'], 'one_pass')
        self.assertEqual(scene_plan(ONE_PASS_SCENE, 11)['rule'], 'short')  # A longer clip never overruns the scene.

    def test_every_duration_is_anchored_and_fits(self):
        for length in (7.5, 10, 10.04, 12):
            for duration in (0.5, 1.5, 3, 6, 9.35, 11, 12.99, 13, 15, 20, 23.99, 24, 30, 61, 300):
                plan = scene_plan(duration, length)
                self.assert_anchored(plan)
        self.assertEqual(scene_plan(1, 10)['segments'], [])
        with self.assertRaises(ValueError): scene_plan(10, 6)
        with self.assertRaises(ValueError): scene_plan(float('nan'), 10)

    def test_motion_settles_slowly_into_the_painting(self):
        plan = scene_plan(40, 10)
        out, back = plan['segments'][:2]
        self.assertEqual(state_at(plan, 0)[:2], (0, 0))
        self.assertEqual(state_at(plan, 0)[2], 0.)  # Wakes up from the still.
        step = 1 / 24  # Playback speed falls smoothly to zero as the subject returns to its pose.
        speeds = [abs(back.clip_time(u + step) - back.clip_time(u)) / step
                  for u in (back.length - RAMP, back.length - RAMP / 2, back.length - step)]
        self.assertAlmostEqual(speeds[0], 1, places=2); self.assertTrue(speeds[0] > speeds[1] > speeds[2]); self.assertLess(speeds[2], 0.05)
        turn = [abs(out.clip_time(u + step) - out.clip_time(u)) / step for u in (out.length - 2 * step,)]
        self.assertLess(turn[0], 0.1)  # Slows gently into the turn, never snaps back.
        part, clip, alpha = state_at(plan, back.end - FADE / 2)
        self.assertEqual(part, 1); self.assertLess(clip, 0.02); self.assertTrue(0 < alpha < 1)  # Dissolve only at the pose.
        self.assertEqual(state_at(plan, 20), (None, None, 0.))
        self.assertAlmostEqual(state_at(plan, 39.999)[1], 10, places=2)

    def test_camera_breath_fills_the_rest_and_hands_back_at_rest(self):
        plan = scene_plan(30, 10)
        for start, stop in still_windows(plan):
            for value, expected in zip(drift_at(plan, start, 0), (1., 0., 0.)):
                self.assertAlmostEqual(value, expected)
            zoom, x, y = drift_at(plan, stop, 0)
            self.assertAlmostEqual(zoom, 1); self.assertAlmostEqual(x, 0); self.assertAlmostEqual(y, 0)
            zoom, x, y = drift_at(plan, (start + stop) / 2, 0)
            self.assertTrue(1 < zoom <= 1.05 and x != 0 and y != 0)
            self.assertLess(drift_at(plan, start + 0.01, 0)[0] - 1, 1e-5)
        self.assertEqual(drift_at(plan, 1.0, 0), (1., 0., 0.))  # Camera holds still while the painting moves.
        base = Image.linear_gradient('L').convert('RGB').resize((270, 480))
        self.assertIs(camera(base), base); self.assertEqual(camera(base, 1.05, 1, -1).size, base.size)
        self.assertGreater(frame_difference(camera(base, 1.05, 1, -1), base), 0)

    def test_still_is_framed_like_the_clip_anchor(self):
        import random
        rng = random.Random(7)
        painting = Image.new('RGB', (900, 1600), 'black')
        from PIL import ImageDraw
        draw = ImageDraw.Draw(painting)
        for _ in range(120):
            x, y, r = rng.randrange(900), rng.randrange(1600), rng.randrange(10, 80)
            draw.ellipse((x - r, y - r, x + r, y + r), fill=tuple(rng.randrange(256) for _ in range(3)))
        # The video model enlarges the painting ~1.5% and nudges it: the clip anchor shows that framing.
        bw, bh = 900 / 1.015, 1600 / 1.015
        cx, cy = 450 + 6, 800 - 4
        anchor = painting.transform((270, 480), Image.Transform.EXTENT, (cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2), Image.Resampling.BICUBIC)
        still, info = align_still(painting, anchor)
        self.assertEqual(info['used'], 'aligned'); self.assertEqual(still.size, (270, 480))
        self.assertAlmostEqual(info['scale'], 1.015, delta=0.004)
        self.assertLess(info['after'], info['before'] / 3)
        same, info = align_still(painting, still_frame_from(painting, (270, 480)))
        self.assertLessEqual(info['after'], info['before'])  # Never worse than the plain crop.

    def test_join_difference_metric_and_still_is_the_original(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'painting.png'
            base = Image.linear_gradient('L').convert('RGB').resize((270, 480)); base.save(path)
            self.assertEqual(frame_difference(base, base.copy()), 0)
            self.assertGreater(frame_difference(base, Image.new('RGB', (270, 480), 'white')), 20)
            frame = still_frame(path, (135, 240))
            self.assertEqual(frame.size, (135, 240)); self.assertLess(frame_difference(frame, base), 2)


class FramingChecks(unittest.TestCase):
    def test_whole_painting_is_shown_and_only_near_portrait_fills_the_frame(self):
        from byron_video.lean_composition import art_box
        self.assertEqual(art_box(1280, 720, 1080, 1920), (1080, 608, 0, 656, 'whole'))  # Landscape: full width, centred.
        self.assertEqual(art_box(720, 720, 1080, 1920), (1080, 1080, 0, 420, 'whole'))   # Square: centred, clear of the captions.
        self.assertEqual(art_box(768, 1344, 1080, 1920)[4], 'fill')                      # Already ~9:16: hides under 6%.
        w, h, left, top, framing = art_box(1000, 1500, 1080, 1920)                        # 2:3 portrait would lose 16%.
        self.assertEqual(framing, 'whole'); self.assertEqual(h, 1620); self.assertLessEqual(top + h, 1920)


class TitleCardChecks(unittest.TestCase):
    def test_card_shows_title_and_byline_and_fits_long_titles(self):
        from byron_video.portrait_composition import presentation
        style = presentation()
        plain = Image.new('RGB', (1080, 1920), (0x14, 0x14, 0x16))
        for title in ('Darkness', 'The Prisoner of Chillon and Other Tales of Captivity on the Shores of a Quiet Mountain Lake'):
            with self.subTest(title=title[:20]):
                card = title_card(1080, 1920, title, 'Lord Byron', style)
                self.assertEqual(card.size, (1080, 1920)); self.assertGreater(frame_difference(card, plain), 0.2)
                self.assertEqual(card.getpixel((0, 0)), (0x14, 0x14, 0x16))  # Charcoal edges; text stays inside the safe width.
                self.assertEqual(card.crop((0, 0, 60, 1920)).getextrema(), ((0x14, 0x14), (0x14, 0x14), (0x16, 0x16)))

    def test_card_fades_in_holds_and_rests_on_charcoal_before_the_story(self):
        self.assertEqual(TITLE_SECONDS, 2.0)  # Default length.
        for length in (1, 2, 3, 4, 5):
            with self.subTest(length=length):
                self.assertEqual(title_level(0, length), 0)
                self.assertEqual(title_level(length * .45, length), 1)  # Fully readable mid-card at every length.
                self.assertEqual(title_level(length - min(.4, length * TITLE_REST), length), 0)
                self.assertEqual(title_level(length - .01, length), 0)  # Charcoal just before the story fades in.


def tool(name):
    return shutil.which(name) is not None


@unittest.skipUnless(tool('ffmpeg') and tool('ffprobe'), 'ffmpeg/ffprobe are required for the real lean render.')
class LeanRenderChecks(unittest.TestCase):
    def test_render_fills_portrait_records_joins_and_keeps_order(self):
        from byron_video.lean_composition import compose_film
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            scenes, text = [], 'A quiet harbour at dawn. The boats rest. Then the hills turn gold at dusk, and the day ends.'
            # A near-portrait painting fills the frame; a landscape one is shown whole on charcoal.
            for index, (colour, painting, clip_size_) in enumerate((('navy', (900, 1200), '768x1344'), ('darkgreen', (1600, 900), '1344x768')), 1):
                original = root / f'painting-{index}.png'
                Image.new('RGB', painting, colour).save(original)
                clip = root / f'clip-{index}.mp4'
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', f'color=c={colour}:s={clip_size_}:r=24:d=10',
                                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip)], check=True, timeout=120)
                scenes.append({'image_id': f'painting_{index:04}', 'original_path': original, 'clip_path': clip})
            duration = 14.0
            audio = root / 'narration.wav'
            with wave.open(str(audio), 'wb') as sound:
                sound.setnchannels(1); sound.setsampwidth(2); sound.setframerate(24000)
                sound.writeframes(b''.join(int(6000 * math.sin(2 * math.pi * 440 * i / 24000)).to_bytes(2, 'little', signed=True)
                                           for i in range(int(24000 * duration))))  # Audible tone: the title card must stay silent.
            tokens = list(re.finditer(r'\S+', text)); step = (duration - 1) / len(tokens)
            words = [{'text': m.group(), 'start': round(i * step, 3), 'end': round(i * step + step * .8, 3),
                      'char_start': m.start(), 'char_end': m.end()} for i, m in enumerate(tokens)]
            report = {'exact_text': text, 'words': words, 'duration_seconds': duration}
            timing = {'duration_seconds': duration, 'crossfade_seconds': .7, 'scenes': [
                {'image_id': 'painting_0001', 'start_seconds': 0, 'end_seconds': 7.35},
                {'image_id': 'painting_0002', 'start_seconds': 6.65, 'end_seconds': duration}]}
            manifest = compose_film(root=root, narration_wav=audio, report=report, timing=timing, scenes=scenes,
                                    output=root / 'film' / 'film.mp4', join_threshold=5)
            self.assertEqual(manifest['composition'], 'lean-anchored-v4-aligned')
            self.assertTrue(all(s['alignment']['used'] in ('aligned', 'cover_crop') for s in manifest['scenes']))
            self.assertEqual((manifest['width'], manifest['height']), (1080, 1920))
            self.assertEqual(manifest['source_order'], ['painting_0001', 'painting_0002'])
            self.assertEqual([s['rule'] for s in manifest['scenes']], ['short', 'short'])
            self.assertTrue(manifest['joins'] and all(isinstance(j['difference'], float) for j in manifest['joins']))
            self.assertTrue(any(j['join'] == 'clip_to_still' for j in manifest['joins']))
            self.assertEqual(manifest['flagged_joins'], sum(j['flagged'] for j in manifest['joins']))
            probe = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(root / 'film' / 'film.mp4')],
                                              check=True, capture_output=True).stdout)
            video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
            self.assertEqual((video['width'], video['height']), (1080, 1920))
            self.assertTrue(math.isclose(manifest['duration_seconds'], duration + 2.0))  # Last painting lingers 2 s after the voice.
            self.assertTrue(math.isclose(manifest['voice_seconds'], duration))
            audio_stream = next(s for s in probe['streams'] if s['codec_type'] == 'audio')
            self.assertGreater(float(audio_stream['duration']), duration + 1.8)  # Silence padded, not cut at the last word.
            self.assertIsNone(manifest['title_card'])
            self.assertEqual([s['framing'] for s in manifest['scenes']], ['fill', 'whole']); self.assertTrue(manifest['letterbox'])
            frame = root / 'landscape.png'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-ss', '12', '-i', str(root / 'film' / 'film.mp4'), '-frames:v', '1', str(frame)],
                           check=True, timeout=60)
            with Image.open(frame) as still:
                still = still.convert('RGB')
                self.assertLess(max(still.getpixel((540, 150))), 40)       # Charcoal above the landscape painting.
                r, g, b = still.getpixel((20, 960)); self.assertGreater(g, 60); self.assertLess(r, 40)  # Painting reaches the left edge: nothing cut.

            # A title with length 0 means no card at all.
            none = compose_film(root=root, narration_wav=audio, report=report, timing=timing, scenes=scenes,
                                output=root / 'none' / 'film.mp4', join_threshold=5, title='A Harbour at Dawn', title_seconds=0)
            self.assertIsNone(none['title_card']); self.assertTrue(math.isclose(none['duration_seconds'], duration + 2.0))

            # With a title: a silent card first (default 2 s), then the same film shifted later by TITLE_SECONDS.
            titled = compose_film(root=root, narration_wav=audio, report=report, timing=timing, scenes=scenes,
                                  output=root / 'titled' / 'film.mp4', join_threshold=5, title='A Harbour at Dawn')
            self.assertEqual(titled['title_card'], {'title': 'A Harbour at Dawn', 'byline': 'Lord Byron', 'seconds': TITLE_SECONDS})
            self.assertTrue(math.isclose(titled['duration_seconds'], TITLE_SECONDS + duration + 2.0))
            srt = (root / 'titled' / 'film.srt').read_text(encoding='utf-8')
            first = re.search(r'(\d\d):(\d\d):(\d\d),(\d{3}) -->', srt).groups()
            self.assertAlmostEqual(int(first[2]) + int(first[3]) / 1000, TITLE_SECONDS + words[0]['start'], places=2)
            frames = root / 'titled' / 'frames'; frames.mkdir()
            for name, at in (('card', TITLE_SECONDS / 2), ('story', TITLE_SECONDS + 3)):
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-ss', str(at), '-i', str(root / 'titled' / 'film.mp4'),
                                '-frames:v', '1', str(frames / f'{name}.png')], check=True, timeout=60)
            with Image.open(frames / 'card.png') as card, Image.open(frames / 'story.png') as story:
                r, g, b = card.convert('RGB').getpixel((540, 200))
                self.assertLess(max(r, g, b), 40)  # Charcoal card (#141416), not the navy painting.
                r, g, b = story.convert('RGB').getpixel((540, 200))
                self.assertGreater(b, 80); self.assertLess(r, 40)  # The navy painting (0, 0, 128) once the story starts.
            loud = subprocess.run(['ffmpeg', '-nostdin', '-v', 'info', '-t', str(TITLE_SECONDS - .2), '-i', str(root / 'titled' / 'film.mp4'),
                                   '-af', 'volumedetect', '-f', 'null', '-'], capture_output=True, text=True, timeout=60).stderr
            self.assertRegex(loud, r'max_volume: -(9[0-9]|inf)')  # Nothing is read aloud over the card.


if __name__ == '__main__': unittest.main()
