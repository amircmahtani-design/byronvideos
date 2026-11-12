"""Heading lines and web links are never narrated; story wording is otherwise unchanged."""
import unittest

from byron_video.spoken_text import prepare_story


class SpokenTextChecks(unittest.TestCase):
    def test_live_story_header_is_set_aside(self):
        # The shape of the real trial paste: title, byline, bare site name, then the story.
        script = ('The Woman Who Waited\n\nby Lord Byron\n\nbadmaddangerous.com\n\n'
                  'At thirty-six, Leila Mehra was considered the most beautiful woman in Bombay.\n\nShe was married twice.')
        text, skipped = prepare_story(script)
        self.assertTrue(text.startswith('At thirty-six, Leila Mehra'))
        self.assertEqual(skipped, {'heading': ['The Woman Who Waited', 'by Lord Byron'], 'links': 1})

    def test_title_and_links_are_set_aside(self):
        script = ('The Old Couple on the Bench\n\nThey sat down slowly. The park was quiet.\n\n'
                  'Read more: https://badmaddangerous.com/old-couple\n\nShe smiled at him, see www.example.com/x for more.')
        text, skipped = prepare_story(script)
        self.assertEqual(text, 'They sat down slowly. The park was quiet.\n\nShe smiled at him, see  for more.')
        self.assertEqual(skipped, {'heading': ['The Old Couple on the Bench'], 'links': 2})

    def test_plain_domain_inside_a_sentence_is_story_wording(self):
        text, skipped = prepare_story('She ordered the frame from Amazon.com and waited.')
        self.assertEqual(text, 'She ordered the frame from Amazon.com and waited.'); self.assertEqual(skipped['links'], 0)

    def test_markdown_link_keeps_label_and_heading_title(self):
        text, skipped = prepare_story('# A Title, With Comma\nThey met at [the gallery](https://example.com/g).')
        self.assertEqual(text, 'They met at the gallery.'); self.assertEqual(skipped['heading'], ['A Title, With Comma'])

    def test_story_without_heading_is_unchanged(self):
        story = 'They sat down slowly.\nThe park was quiet.\n\n\n\nShe smiled.'
        text, skipped = prepare_story(story)
        self.assertEqual(text, 'They sat down slowly.\nThe park was quiet.\n\nShe smiled.')
        self.assertEqual(skipped, {'heading': [], 'links': 0})
        self.assertEqual(prepare_story('One short opening line\nstill the story.')[1]['heading'], [])  # No blank line after.

    def test_nothing_left_is_refused(self):
        with self.assertRaises(ValueError): prepare_story('Just A Title\n\nhttps://example.com')


if __name__ == '__main__': unittest.main()
