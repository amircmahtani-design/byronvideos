"""The listen-back guard: missing story words stop a film; misheard words do not."""
import unittest

from byron_video.narration_check import judge

STORY = ('Leila found this almost indecent. She became unavailable, then cold, then distant. '
         'He spoke of her very fondly, years later, to anyone who asked.')


class NarrationCheckChecks(unittest.TestCase):
    def test_complete_reading_passes_even_with_misheard_names(self):
        heard = STORY.replace('Leila', 'Layla').replace('indecent', 'in decent')
        ok, detail = judge(STORY, heard)
        self.assertTrue(ok, detail); self.assertEqual(detail['runs'], [])

    def test_the_live_skip_is_caught_and_located(self):
        # Seen live: after "almost indecent" the voice jumped straight to "very fondly".
        heard = 'Leila found this almost indecent. He spoke of her very fondly, years later, to anyone who asked.'
        ok, detail = judge(STORY, heard)
        self.assertFalse(ok)
        self.assertTrue(detail['runs'][0]['near'].startswith('she became unavailable'))
        self.assertGreaterEqual(detail['missing_words'], 7)

    def test_a_swallowed_first_word_of_a_sentence_is_caught(self):
        heard = STORY.replace('She became unavailable', 'became unavailable')
        ok, detail = judge(STORY, heard)
        self.assertFalse(ok); self.assertTrue(detail['runs'][0]['near'].startswith('she became'))
        self.assertTrue(judge(STORY, STORY.replace('She became', 'Shi became'))[0])  # Misheard, not missing.

    def test_two_missing_words_are_tolerated_but_three_are_not(self):
        self.assertTrue(judge(STORY, STORY.replace('then cold, ', ''))[0])
        self.assertFalse(judge(STORY, STORY.replace('then cold, then ', ''))[0])


if __name__ == '__main__': unittest.main()
