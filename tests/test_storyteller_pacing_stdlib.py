"""Storyteller pacing: one request per sentence or line, exact text, pauses only in 'sentences' mode."""
import unittest

from byron_video.elevenlabs_adapter import plan_requests, sentence_spans, validate_profile
from byron_video.elevenlabs_release import PARAGRAPH_PAUSE, SENTENCE_PAUSE, SKIP_SECONDS_PER_CHAR, pause_after
from byron_video.narration_media import RATE
from test_provider_execution_stdlib import profile

STORY = ('At thirty-six, Leila Mehra was considered the most beautiful woman in Bombay. She had been married twice.\n\n'
         '“You are extraordinarily beautiful.”\n\n“I’ve been told.”\n\nMr. Mehra smiled… Then he left!')


class PacingChecks(unittest.TestCase):
    def test_sentence_spans_are_exact_and_keep_abbreviations(self):
        spans = sentence_spans(STORY, 10_000)
        self.assertEqual(''.join(s['text'] for s in spans), STORY)
        self.assertEqual([s['text'].strip() for s in spans], [
            'At thirty-six, Leila Mehra was considered the most beautiful woman in Bombay.', 'She had been married twice.',
            '“You are extraordinarily beautiful.”', '“I’ve been told.”', 'Mr. Mehra smiled…', 'Then he left!'])

    def test_pacing_profile_plans_one_request_per_sentence_with_story_context(self):
        paced = {**profile(), 'pacing': 'sentences'}
        self.assertEqual(validate_profile(paced)['pacing'], 'sentences')
        requests = plan_requests(STORY, paced)
        self.assertEqual(len(requests), 6); self.assertEqual(''.join(r['body']['text'] for r in requests), STORY)
        self.assertTrue(requests[2]['body']['previous_text'].endswith('married twice.\n\n'))  # Context is the story, not one span.
        self.assertNotIn('pacing', requests[0]['body'])
        self.assertEqual(len(plan_requests(STORY, profile())), 1)  # Without pacing: unchanged single request.
        with self.assertRaises(ValueError): validate_profile({**profile(), 'pacing': 'fast'})

    def test_studio_story_limits_are_generous_and_engine_is_untouched(self):
        from byron_video import engine
        from byron_video.story_limits import STORY_MAX_CHARS, STORY_MAX_WORDS, validate_story
        self.assertEqual((engine.MAX_SCRIPT_CHARS, engine.MAX_SCRIPT_WORDS), (16_000, 2_000))  # Locked; older paths only.
        long_story = ' '.join(['Sentence.'] * 15_000)  # 15,000 words, ~150,000 characters... trimmed below.
        long_story = long_story[:STORY_MAX_CHARS]
        self.assertEqual(validate_story(long_story), long_story)
        self.assertGreater(len(plan_requests(long_story, {**profile(), 'pacing': 'sentences'})), 0)
        with self.assertRaises(ValueError): validate_story('x' * (STORY_MAX_CHARS + 1))
        with self.assertRaises(ValueError): validate_story('word ' * (STORY_MAX_WORDS + 1))

    def test_very_long_story_groups_sentences_to_fit_and_keeps_exact_text(self):
        story = ' '.join(f'Line {i} ends.' for i in range(1500))  # 1,500 sentences.
        spans = sentence_spans(story, 10_000)
        self.assertLessEqual(len(spans), 400); self.assertEqual(''.join(s['text'] for s in spans), story)

    def test_pauses_follow_sentences_and_paragraphs(self):
        self.assertEqual(pause_after('A sentence. ', False, 'sentences'), round(RATE * SENTENCE_PAUSE))
        self.assertEqual(pause_after('“I’ve been told.”\n\n', False, 'sentences'), round(RATE * PARAGRAPH_PAUSE))
        self.assertEqual(pause_after('Then he left!', True, 'sentences'), 0)
        self.assertEqual(pause_after('A sentence. ', False, None), 0)
        self.assertLess(SKIP_SECONDS_PER_CHAR, 0.07)  # Well below calm narration, so normal sentences are never flagged.

    def test_joins_are_softened_without_changing_length(self):
        from array import array
        from byron_video.elevenlabs_release import soften_edges
        loud = array('h', [20000] * RATE).tobytes()
        soft = array('h', soften_edges(loud))
        self.assertEqual(len(soft), RATE)
        self.assertEqual(soft[0], 0); self.assertEqual(soft[-1], 0)  # No click at either join.
        self.assertEqual(soft[RATE // 2], 20000)  # The speech itself is untouched.


if __name__ == '__main__': unittest.main()
