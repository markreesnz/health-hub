import os
import tempfile
import unittest
from pathlib import Path


class TextbookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'study').mkdir()
        text = (
            '===== PDF p3 | book p1 =====\n'
            'Riesling is a white grape variety prized for high acidity.\n\n'
            '===== PDF p4 | book p2 =====\n'
            'Chablis is made from Chardonnay in a cool climate, unoaked style.\n'
        )
        (root / 'study' / 'textbook_fulltext.txt').write_text(text)
        os.environ['WSET_ROOT'] = str(root)
        os.environ.pop('WSET_CONNECTOR_CONFIG', None)

        import importlib
        import storage
        import textbook
        importlib.reload(storage)
        importlib.reload(textbook)
        self.textbook = textbook
        self.storage = storage

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop('WSET_ROOT', None)

    def test_search_finds_term_with_book_page(self):
        results = self.textbook.search_textbook('Riesling')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['book_page'], '1')
        self.assertIn('Riesling', results[0]['excerpt'])

    def test_search_is_case_insensitive(self):
        results = self.textbook.search_textbook('chablis')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['book_page'], '2')

    def test_search_no_match_returns_empty(self):
        self.assertEqual(self.textbook.search_textbook('Nebbiolo'), [])

    def test_search_empty_query_raises(self):
        with self.assertRaises(self.storage.WsetError):
            self.textbook.search_textbook('   ')


if __name__ == '__main__':
    unittest.main()
