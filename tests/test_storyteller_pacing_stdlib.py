"""Storyteller pacing: one request per sentence or line, exact text, pauses only in 'sentences' mode."""
import unittest

from byron_video.elevenlabs_adapter import ENDING_SPEED, closing_cut, ending_roles, plan_requests, sentence_spans, validate_profile
from byron_video.elevenlabs_release import (CLOSING_JOIN, ENDING_GAP, PARAGRAPH_PAUSE, SENTENCE_PAUSE, SKIP_SECONDS_PER_CHAR,
                                            ending_pause, pause_after)
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

    def test_final_sentence_is_slower_and_its_closing_words_are_recorded_on_their_own(self):
        story = ('Three days later, her telephone rang.\n\n'
                 'For the first time in her life, Leila answered without waiting to see if it would ring again.')
        paced = {**profile(), 'pacing': 'sentences'}
        requests = plan_requests(story, paced)
        self.assertEqual([r['body']['text'] for r in requests], [
            'Three days later, her telephone rang.\n\n',
            'For the first time in her life, Leila answered without waiting to see ',
            'if it would ring again.'])
        self.assertEqual(''.join(r['body']['text'] for r in requests), story)  # Exact text, nothing rewritten.
        normal = paced['voice_settings']
        self.assertEqual(requests[0]['body']['voice_settings'], normal)
        for r in requests[1:]:  # Same voice; only the speed of the final sentence changes.
            self.assertEqual(r['body']['voice_settings'], {**normal, 'speed': ENDING_SPEED})
        self.assertEqual(requests[1]['body']['next_text'], 'if it would ring again.')
        self.assertNotIn('next_text', requests[2]['body'])
        self.assertEqual(ending_roles(story, paced), {'final': 1, 'closing': 2})
        self.assertEqual(len(plan_requests(story, profile())), 1)  # Without pacing: unchanged single request.
        self.assertIsNone(ending_roles(story, profile()))

    def test_closing_words_split_only_at_a_natural_break(self):
        self.assertEqual(closing_cut('For the first time in her life, Leila answered without waiting to see if it would ring again.'),
                         len('For the first time in her life, Leila answered without waiting to see '))
        self.assertIsNone(closing_cut('Then he left!'))  # Too short to split.
        # Live film, 24 Nov 2026: no joining word or comma, so a subject pronoun starts the closing words.
        sentence = 'There was nobody to tell him he was being foolish.'
        self.assertEqual(sentence[closing_cut(sentence):], 'he was being foolish.')
        # No break at all: the last four words are still landed on their own.
        sentence = 'Seven long years passed slowly over the quiet grey northern town.'
        self.assertEqual(sentence[closing_cut(sentence):], 'quiet grey northern town.')
        cut = closing_cut('She closed the old blue door behind her, quietly and for good.')
        self.assertEqual('She closed the old blue door behind her, quietly and for good.'[cut:], 'quietly and for good.')

    def test_short_final_sentence_is_slowed_but_not_split(self):
        requests = plan_requests(STORY, {**profile(), 'pacing': 'sentences'})
        self.assertEqual(len(requests), 6); self.assertEqual(requests[-1]['body']['text'], 'Then he left!')
        self.assertEqual(requests[-1]['body']['voice_settings']['speed'], ENDING_SPEED)
        self.assertEqual(requests[-2]['body']['voice_settings']['speed'], profile()['voice_settings']['speed'])

    def test_ending_pauses(self):
        roles = {'final': 1, 'closing': 2}
        self.assertEqual(ending_pause('Three days later, her telephone rang.\n\n', 0, 3, 'sentences', roles), round(RATE * ENDING_GAP))
        self.assertEqual(ending_pause('For the first time ... to see ', 1, 3, 'sentences', roles), round(RATE * CLOSING_JOIN))
        self.assertEqual(ending_pause('if it would ring again.', 2, 3, 'sentences', roles), 0)
        self.assertEqual(ending_pause('A sentence. ', 0, 3, 'sentences', roles), round(RATE * SENTENCE_PAUSE))  # Never longer.
        self.assertLess(ENDING_GAP, PARAGRAPH_PAUSE)

    def test_grouped_long_story_still_gets_its_own_ending(self):
        story = ' '.join(f'Line {i} ends.' for i in range(1500)) + ' She left the room, and the door closed behind her.'
        requests = plan_requests(story, {**profile(), 'pacing': 'sentences'})
        self.assertEqual(''.join(r['body']['text'] for r in requests), story)
        self.assertEqual(requests[-2]['body']['text'], 'She left the room, ')
        self.assertEqual(requests[-1]['body']['text'], 'and the door closed behind her.')
        self.assertLessEqual(len(requests), 402)

    def test_joins_are_softened_without_changing_length(self):
        from array import array
        from byron_video.elevenlabs_release import soften_edges
        loud = array('h', [20000] * RATE).tobytes()
        soft = array('h', soften_edges(loud))
        self.assertEqual(len(soft), RATE)
        self.assertEqual(soft[0], 0); self.assertEqual(soft[-1], 0)  # No click at either join.
        self.assertEqual(soft[RATE // 2], 20000)  # The speech itself is untouched.


if __name__ == '__main__': unittest.main()
