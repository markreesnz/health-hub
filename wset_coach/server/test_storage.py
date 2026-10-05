import os
import tempfile
import unittest
from pathlib import Path


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'state').mkdir()
        (root / 'quizzes').mkdir()
        (root / 'state' / 'progress.md').write_text('resume here\n')
        (root / 'AGENTS.md').write_text('coach rules\n')
        os.environ['WSET_ROOT'] = str(root)
        os.environ.pop('WSET_CONNECTOR_CONFIG', None)

        import importlib
        import storage
        importlib.reload(storage)
        self.storage = storage
        self.root = root.resolve()  # macOS /var is a symlink to /private/var; storage.ROOT resolves it

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop('WSET_ROOT', None)

    def test_read_target_returns_content_and_hash(self):
        result = self.storage.read_target('progress')
        self.assertEqual(result['content'], 'resume here\n')
        self.assertEqual(result['sha256'], self.storage.sha256('resume here\n'))

    def test_read_target_missing_file_is_empty_not_error(self):
        result = self.storage.read_target('weak_areas')
        self.assertEqual(result['content'], '')

    def test_read_target_unknown_name_raises(self):
        with self.assertRaises(self.storage.WsetError):
            self.storage.read_target('nope')

    def test_coaching_contract_is_read_only_not_writable(self):
        self.assertIn('coaching_contract', self.storage.READ_TARGETS)
        self.assertNotIn('coaching_contract', self.storage.WRITE_TARGETS)

    def test_session_path_rejects_bad_slug(self):
        with self.assertRaises(self.storage.WsetError):
            self.storage.session_path('../../etc/passwd')
        with self.assertRaises(self.storage.WsetError):
            self.storage.session_path('Has Spaces')

    def test_session_path_accepts_good_slug(self):
        path = self.storage.session_path('loire-quickfire')
        self.assertTrue(str(path).startswith(str(self.root / 'quizzes' / 'session-')))
        self.assertTrue(str(path).endswith('-loire-quickfire.md'))

    def test_atomic_write_then_read_roundtrip(self):
        target = self.root / 'state' / 'weak-areas.md'
        self.storage.atomic_write(target, 'a new weakness\n')
        self.assertEqual(target.read_text(), 'a new weakness\n')


if __name__ == '__main__':
    unittest.main()
