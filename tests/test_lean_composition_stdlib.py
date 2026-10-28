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

from byron_video.lean_composition import (FADE, drift_at, frame_difference, scene_plan, state_at, still_frame, still_windows)


def parts(plan):
    return [(round(s.at, 6), round(s.clip_from, 6), round(s.clip_to, 6)) for s in plan['segments']]


class SceneRuleChecks(unittest.TestCase):
    def assert_forward_once(self, plan):
        previous_end, previous_clip = 0., 0.
        for s in plan['segments']:
            self.assertGreaterEqual(s.at, previous_end - 1e-9)  # No overlap between parts.
            self.assertGreaterEqual(s.clip_from, previous_clip - 1e-9)  # Clip frames forward only, used once.
            previous_end, previous_clip = s.end, s.clip_to
        self.assertLessEqual(previous_end, plan['duration'] + 1e-9)

    def test_full_rule_places_5_2_3_at_start_middle_and_end(self):
        plan = scene_plan(20, 10)
        self.assertEqual(plan['rule'], 'full')
        self.assertEqual(parts(plan), [(0, 0, 5), (9, 5, 7), (17, 7, 10)])
        first, middle, last = plan['segments']
        self.assertEqual((first.fade_in, first.fade_out), (0, FADE))  # Direct start: clip t=0 is the original.
        self.assertEqual((middle.fade_in, middle.fade_out), (FADE, FADE))
        self.assertEqual((last.fade_in, last.fade_out), (FADE, 0))  # Ends with the scene into the 0.7 s crossfade.
        self.assertAlmostEqual(last.end, 20)
        self.assert_forward_once(plan)
        self.assertEqual(scene_plan(12, 10)['rule'], 'full')

    def test_short_scene_rules(self):
        plan = scene_plan(10, 10)
        self.assertEqual(plan['rule'], 'two_part'); self.assertEqual(parts(plan), [(0, 0, 5), (7, 7, 10)])
        self.assertEqual(scene_plan(11.99, 10)['rule'], 'two_part'); self.assertEqual(scene_plan(8, 10)['rule'], 'two_part')
        plan = scene_plan(6, 10)
        self.assertEqual(plan['rule'], 'opening_only'); self.assertEqual(parts(plan), [(0, 0, 4.4)])
        self.assertEqual(plan['segments'][0].fade_out, FADE)
        self.assertEqual([(round(a, 6), round(b, 6)) for a, b in still_windows(plan)], [(3.6, 6)])
        self.assertEqual(parts(scene_plan(7.99, 10)), [(0, 0, 5)])
        tiny = scene_plan(0.5, 10)
        self.assertEqual(parts(tiny), [(0, 0, 0.5)]); self.assertEqual(tiny['segments'][0].fade_out, 0)
        for duration in (0.5, 3, 6, 7.99, 8, 9.3, 11.99, 12, 15, 30, 61):
            self.assert_forward_once(scene_plan(duration, 10))

    def test_longer_clip_never_overlaps_or_rewinds(self):
        plan = scene_plan(8, 10.2)
        self.assertEqual(parts(plan)[1][0], 5.0)
        self.assertAlmostEqual(plan['segments'][1].clip_from, 7.2)
        self.assert_forward_once(plan)
        with self.assertRaises(ValueError): scene_plan(10, 6)
        with self.assertRaises(ValueError): scene_plan(float('nan'), 10)

    def test_crossfades_and_eased_drift(self):
        plan = scene_plan(20, 10)
        self.assertEqual(state_at(plan, 0), (0, 0, 1.))
        part, clip, alpha = state_at(plan, 5 - FADE / 2)
        self.assertEqual(part, 0); self.assertAlmostEqual(clip, 4.6); self.assertTrue(0 < alpha < 1)
        self.assertEqual(state_at(plan, 6), (None, None, 0.))
        self.assertEqual(state_at(plan, 9)[2], 0.)  # Middle part fades in from the still.
        self.assertAlmostEqual(state_at(plan, 19.99)[1], 9.99)
        for start, stop in still_windows(plan):
            self.assertEqual(drift_at(plan, start, 0), (1., 0., 0.))
            zoom, x, y = drift_at(plan, stop, 0)
            self.assertAlmostEqual(zoom, 1); self.assertAlmostEqual(x, 0); self.assertAlmostEqual(y, 0)
            zoom, x, y = drift_at(plan, (start + stop) / 2, 0)
            self.assertTrue(1 < zoom <= 1.05 and x != 0 and y != 0)
            near = drift_at(plan, start + 0.01, 0)[0] - 1
            self.assertLess(near, 1e-5)  # Eased: no abrupt start of motion.

    def test_join_difference_metric(self):
        base = Image.linear_gradient('L').convert('RGB').resize((270, 480))
        self.assertEqual(frame_difference(base, base.copy()), 0)
        self.assertGreater(frame_difference(base, Image.new('RGB', (270, 480), 'white')), 20)
        frame = still_frame(base, (135, 240))
        self.assertEqual(frame.size, (135, 240)); self.assertLess(frame_difference(frame, base), 2)
        self.assertGreater(frame_difference(still_frame(base, (135, 240), 1.05, 1, -1), frame), 0)


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
            self.assertTrue(math.isclose(manifest['duration_seconds'], duration))


if __name__ == '__main__': unittest.main()
