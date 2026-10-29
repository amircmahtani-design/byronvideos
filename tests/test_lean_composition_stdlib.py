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

from byron_video.lean_composition import (DEPTH, FADE, ONE_PASS_SCENE, RAMP, TURN, TWO_MOMENTS_SCENE, camera, drift_at,
                                          frame_difference, scene_plan, state_at, still_frame, still_windows)

ANCHOR_TOLERANCE = 1e-6


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

    def test_join_difference_metric_and_still_is_the_original(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'painting.png'
            base = Image.linear_gradient('L').convert('RGB').resize((270, 480)); base.save(path)
            self.assertEqual(frame_difference(base, base.copy()), 0)
            self.assertGreater(frame_difference(base, Image.new('RGB', (270, 480), 'white')), 20)
            frame = still_frame(path, (135, 240))
            self.assertEqual(frame.size, (135, 240)); self.assertLess(frame_difference(frame, base), 2)


def tool(name):
    return shutil.which(name) is not None


@unittest.skipUnless(tool('ffmpeg') and tool('ffprobe'), 'ffmpeg/ffprobe are required for the real lean render.')
class LeanRenderChecks(unittest.TestCase):
    def test_render_fills_portrait_records_joins_and_keeps_order(self):
        from byron_video.lean_composition import compose_film
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            scenes, text = [], 'A quiet harbour at dawn. The boats rest. Then the hills turn gold at dusk, and the day ends.'
            for index, colour in enumerate(('navy', 'darkgreen'), 1):
                original = root / f'painting-{index}.png'
                Image.new('RGB', (900, 1200), colour).save(original)
                clip = root / f'clip-{index}.mp4'
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', f'color=c={colour}:s=768x1344:r=24:d=10',
                                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip)], check=True, timeout=120)
                scenes.append({'image_id': f'painting_{index:04}', 'original_path': original, 'clip_path': clip})
            duration = 14.0
            audio = root / 'narration.wav'
            with wave.open(str(audio), 'wb') as sound:
                sound.setnchannels(1); sound.setsampwidth(2); sound.setframerate(24000)
                sound.writeframes(b'\x00\x00' * int(24000 * duration))
            tokens = list(re.finditer(r'\S+', text)); step = (duration - 1) / len(tokens)
            words = [{'text': m.group(), 'start': round(i * step, 3), 'end': round(i * step + step * .8, 3),
                      'char_start': m.start(), 'char_end': m.end()} for i, m in enumerate(tokens)]
            report = {'exact_text': text, 'words': words, 'duration_seconds': duration}
            timing = {'duration_seconds': duration, 'crossfade_seconds': .7, 'scenes': [
                {'image_id': 'painting_0001', 'start_seconds': 0, 'end_seconds': 7.35},
                {'image_id': 'painting_0002', 'start_seconds': 6.65, 'end_seconds': duration}]}
            manifest = compose_film(root=root, narration_wav=audio, report=report, timing=timing, scenes=scenes,
                                    output=root / 'film' / 'film.mp4', join_threshold=5)
            self.assertEqual(manifest['composition'], 'lean-anchored-v3')
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


if __name__ == '__main__': unittest.main()
