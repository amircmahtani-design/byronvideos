"""Storyteller pacing: one request per sentence or line, exact text, pauses only in 'sentences' mode."""
import unittest

from byron_video.elevenlabs_adapter import plan_requests, sentence_spans, validate_profile
from byron_video.elevenlabs_release import PARAGRAPH_PAUSE, SENTENCE_PAUSE, pause_after
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

    def test_pauses_follow_sentences_and_paragraphs(self):
        self.assertEqual(pause_after('A sentence. ', False, 'sentences'), round(RATE * SENTENCE_PAUSE))
        self.assertEqual(pause_after('“I’ve been told.”\n\n', False, 'sentences'), round(RATE * PARAGRAPH_PAUSE))
        self.assertEqual(pause_after('Then he left!', True, 'sentences'), 0)
        self.assertEqual(pause_after('A sentence. ', False, None), 0)


if __name__ == '__main__': unittest.main()
