"""Lean 5/2/3 timing math and join metric; the real render runs only where ffmpeg exists."""
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

from byron_video.lean_composition import (FULL_SCENE, MIDDLE_RAMP, RAMP, TWO_PART_SCENE, camera, drift_at, frame_difference,
                                          scene_plan, state_at, still_frame, still_windows)


def parts(plan):
    return [(round(s.at, 6), round(s.clip_from, 6), round(s.clip_to, 6)) for s in plan['segments']]


class SceneRuleChecks(unittest.TestCase):
    def assert_forward_once(self, plan):
        previous_end, previous_clip = 0., 0.
        for s in plan['segments']:
            self.assertGreaterEqual(s.at, previous_end - 1e-9)  # No overlap between parts.
            self.assertGreaterEqual(s.clip_from, previous_clip - 1e-9)  # Clip frames forward only, used once.
            times = [s.clip_time(s.length * i / 200) for i in range(201)]
            self.assertTrue(all(b >= a - 1e-9 for a, b in zip(times, times[1:])))  # Never backwards.
            self.assertAlmostEqual(times[0], s.clip_from); self.assertAlmostEqual(times[-1], s.clip_to)
            previous_end, previous_clip = s.end, s.clip_to
        self.assertLessEqual(previous_end, plan['duration'] + 1e-9)

    def test_full_rule_places_5_2_3_at_start_middle_and_end(self):
        plan = scene_plan(20, 10)
        self.assertEqual(plan['rule'], 'full')
        m = min(RAMP, MIDDLE_RAMP)
        self.assertEqual(parts(plan), [(0, 0, 5), (10 - (2 + m) / 2, 5, 7), (20 - (3 + RAMP / 2), 7, 10)])
        first, middle, last = plan['segments']
        self.assertEqual((first.ramp_in, first.ramp_out), (0, RAMP))  # Direct start: clip t=0 is the original.
        self.assertEqual((middle.ramp_in, middle.ramp_out), (m, m))
        self.assertEqual((last.ramp_in, last.ramp_out), (RAMP, 0))  # Clip t=L is the original; scene crossfade follows.
        self.assertAlmostEqual(last.end, 20); self.assertAlmostEqual(middle.at + middle.length / 2, 10)
        self.assert_forward_once(plan)
        self.assertEqual(scene_plan(FULL_SCENE, 10)['rule'], 'full')

    def test_long_scene_spreads_four_new_moments_of_motion(self):
        plan = scene_plan(45, 10)
        self.assertEqual(plan['rule'], 'spread')
        self.assertEqual([(s.clip_from, s.clip_to) for s in plan['segments']], [(0, 4), (4, 6), (6, 8), (8, 10)])  # No repeats.
        centres = [s.at + s.length / 2 for s in plan['segments'][1:3]]
        self.assertAlmostEqual(centres[0], 15); self.assertAlmostEqual(centres[1], 30)
        self.assertAlmostEqual(plan['segments'][-1].end, 45)
        self.assert_forward_once(plan)
        self.assertEqual(scene_plan(29.9, 10)['rule'], 'full')

    def test_short_scene_rules(self):
        plan = scene_plan(12, 10)
        self.assertEqual(plan['rule'], 'two_part'); self.assertEqual(parts(plan), [(0, 0, 5), (12 - (3 + RAMP / 2), 7, 10)])
        self.assertEqual(scene_plan(FULL_SCENE - 0.01, 10)['rule'], 'two_part'); self.assertEqual(scene_plan(TWO_PART_SCENE, 10)['rule'], 'two_part')
        plan = scene_plan(6, 10)
        self.assertEqual(plan['rule'], 'opening_only'); self.assertAlmostEqual(plan['segments'][0].ramp_out, min(RAMP, 2.4))
        self.assertLessEqual(plan['segments'][0].end, 6)
        for duration in (0.5, 3, 6, 7.99, 8, 9.3, 10.49, 10.5, 11.5, 12, 14.99, 15, 16.99, 17, 30, 61):
            self.assert_forward_once(scene_plan(duration, 10))
            self.assert_forward_once(scene_plan(duration, 12))

    def test_longer_clip_never_overlaps_or_rewinds(self):
        plan = scene_plan(TWO_PART_SCENE, 12)
        self.assertGreater(plan['segments'][1].clip_from, 7)  # Starts later in the clip instead of overlapping.
        self.assert_forward_once(plan)
        with self.assertRaises(ValueError): scene_plan(10, 6)
        with self.assertRaises(ValueError): scene_plan(float('nan'), 10)

    def test_motion_eases_to_rest_while_dissolving_into_the_still(self):
        plan = scene_plan(20, 10)
        self.assertEqual(state_at(plan, 0), (0, 0, 1.))
        first = plan['segments'][0]
        part, clip, alpha = state_at(plan, first.end - RAMP / 2)
        self.assertEqual(part, 0); self.assertTrue(4.5 < clip < 5 and 0 < alpha < 1)
        step = 1 / 24  # Playback speed (clip seconds per output second) falls smoothly towards zero.
        speeds = [(first.clip_time(u + step) - first.clip_time(u)) / step for u in (first.length - RAMP, first.length - RAMP / 2, first.length - step)]
        self.assertAlmostEqual(speeds[0], 1, places=2); self.assertTrue(speeds[0] > speeds[1] > speeds[2]); self.assertLess(speeds[2], 0.05)
        self.assertEqual(state_at(plan, 7), (None, None, 0.))  # Perfectly still painting between parts.
        middle = plan['segments'][1]
        self.assertEqual(state_at(plan, middle.at)[2], 0.)  # Middle part wakes up from the still.
        self.assertAlmostEqual(state_at(plan, 19.999)[1], 10, places=2)
        self.assertEqual(len(still_windows(plan)), 2)

    def test_camera_breath_takes_over_as_motion_settles_and_hands_back_at_rest(self):
        plan = scene_plan(30, 10)
        for start, stop in still_windows(plan):
            for value, expected in zip(drift_at(plan, start, 0), (1., 0., 0.)):  # Begins exactly as the motion settles.
                self.assertAlmostEqual(value, expected)
            zoom, x, y = drift_at(plan, stop, 0)
            self.assertAlmostEqual(zoom, 1); self.assertAlmostEqual(x, 0); self.assertAlmostEqual(y, 0)  # Back at the exact framing.
            zoom, x, y = drift_at(plan, (start + stop) / 2, 0)
            self.assertTrue(1 < zoom <= 1.05 and x != 0 and y != 0)
            self.assertLess(drift_at(plan, start + 0.01, 0)[0] - 1, 1e-5)  # Eased from rest: no jolt.
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
            self.assertEqual((manifest['width'], manifest['height']), (1080, 1920))
            self.assertEqual(manifest['source_order'], ['painting_0001', 'painting_0002'])
            self.assertEqual([s['rule'] for s in manifest['scenes']], ['opening_only', 'opening_only'])
            self.assertTrue(manifest['joins'] and all(isinstance(j['difference'], float) for j in manifest['joins']))
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
