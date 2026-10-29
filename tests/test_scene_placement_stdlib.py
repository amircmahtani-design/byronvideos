"""Paintings appear where the story reaches them; any doubt falls back without stopping the film."""
import io
import json
import re
import unittest

from PIL import Image

from byron_video.scene_placement import cue_bounds, place, placement_request, sentences

TEXT = ('The day began in the harbour. Boats rested on the water. Later they walked up the hill. '
        'The town lay below them. At last they sat down on the old bench. They were old again, and happy. '
        'Night came softly. The stars were out.')


def png(colour):
    out = io.BytesIO(); Image.new('RGB', (900, 1200), colour).save(out, format='PNG'); return out.getvalue()


def timed_words(text, seconds_per_word=0.6):
    return [{'text': m.group(), 'start': i * seconds_per_word, 'end': i * seconds_per_word + 0.4, 'char_start': m.start()}
            for i, m in enumerate(re.finditer(r'\S+', text))]


class Response:
    def __init__(self, status, payload): self.status_code, self.content = status, json.dumps(payload).encode()


class Client:
    def __init__(self, response): self.response, self.calls = response, []
    def __call__(self, **kwargs): self.kwargs = kwargs; return self
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, url, json=None, headers=None): self.calls.append((url, json, headers)); return self.response


def answer(starts):
    return {'candidates': [{'content': {'parts': [{'text': json.dumps({'starts': starts})}]}}]}


class PlacementChecks(unittest.TestCase):
    def setUp(self):
        self.paintings = [png(c) for c in ('navy', 'green', 'brown', 'grey')]
        self.words = timed_words(TEXT); self.duration = self.words[-1]['end'] + 0.5

    def run_place(self, response):
        client = Client(response)
        result = place(project='badmad-dangerous', text=TEXT, paintings=self.paintings, words=self.words,
                       duration=self.duration, token=lambda: 'synthetic-token', client_factory=client)
        return result, client

    def test_sentences_and_request_keep_painting_order(self):
        numbered = sentences(TEXT)
        self.assertEqual(len(numbered), 8); self.assertEqual(numbered[4][1], 'At last they sat down on the old bench.')
        request = placement_request('badmad-dangerous', TEXT, self.paintings)
        labels = [p['text'] for p in request['body']['contents'][0]['parts'] if p.get('text', '').startswith('Painting')]
        self.assertEqual(labels, ['Painting 1:', 'Painting 2:', 'Painting 3:', 'Painting 4:'])
        self.assertIn('aiplatform.eu.rep.googleapis.com', request['url'])

    def test_chosen_sentences_become_scene_bounds(self):
        (bounds, detail), client = self.run_place(Response(200, answer([3, 5, 7])))
        numbered = sentences(TEXT)
        expected = [next(w['start'] for w in self.words if w['char_start'] >= numbered[n - 1][0]) for n in (3, 5, 7)]
        self.assertEqual(bounds, [0.0] + expected + [self.duration])
        self.assertEqual(detail['sentences'], [3, 5, 7])
        self.assertEqual(client.calls[0][2]['Authorization'], 'Bearer synthetic-token')

    def test_bad_answers_fall_back_and_never_raise(self):
        for response in (Response(403, {}), Response(200, answer([5, 3, 7])), Response(200, answer([3, 5])),
                         Response(200, answer([1, 5, 7])), Response(200, {'candidates': []}), Response(200, answer([2, 3, 4]))):
            with self.subTest(response=response.content[:60]):
                (bounds, reason), _ = self.run_place(response)
                self.assertIsNone(bounds); self.assertTrue(reason)

    def test_too_brief_scene_is_rejected(self):
        numbered = sentences(TEXT)
        self.assertIsNone(cue_bounds([2, 3, 4], numbered, self.words, self.duration, 4))


if __name__ == '__main__': unittest.main()
