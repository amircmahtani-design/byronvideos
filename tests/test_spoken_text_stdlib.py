"""Title and web links are never narrated; story wording is otherwise unchanged."""
import unittest

from byron_video.spoken_text import prepare_story


class SpokenTextChecks(unittest.TestCase):
    def test_title_and_links_are_set_aside(self):
        script = ('The Old Couple on the Bench\n\nThey sat down slowly. The park was quiet.\n\n'
                  'Read more: https://badmaddangerous.com/old-couple\n\nShe smiled at him, see www.example.com/x for more.')
        text, skipped = prepare_story(script)
        self.assertEqual(text, 'They sat down slowly. The park was quiet.\n\nShe smiled at him, see  for more.'.replace('  ', ' ', 0))
        self.assertEqual(skipped, {'title': 'The Old Couple on the Bench', 'links': 2})
        self.assertNotIn('http', text); self.assertNotIn('Old Couple on the Bench', text)

    def test_markdown_link_keeps_label_and_heading_title(self):
        text, skipped = prepare_story('# A Title, With Comma\nThey met at [the gallery](https://example.com/g).')
        self.assertEqual(text, 'They met at the gallery.'); self.assertEqual(skipped['title'], 'A Title, With Comma')

    def test_story_without_title_is_unchanged(self):
        story = 'They sat down slowly.\nThe park was quiet.\n\n\n\nShe smiled.'
        text, skipped = prepare_story(story)
        self.assertEqual(text, 'They sat down slowly.\nThe park was quiet.\n\nShe smiled.')
        self.assertEqual(skipped, {'title': None, 'links': 0})
        self.assertEqual(prepare_story('One short opening line\nstill the story.')[1]['title'], None)  # No blank line after.

    def test_nothing_left_is_refused(self):
        with self.assertRaises(ValueError): prepare_story('Just A Title\n\nhttps://example.com')


if __name__ == '__main__': unittest.main()
