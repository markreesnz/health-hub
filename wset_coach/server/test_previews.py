import os
import tempfile
import unittest
from pathlib import Path


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / 'wset'
        runtime = Path(self.tmp.name) / 'runtime'
        (root / 'state').mkdir(parents=True)
        (root / 'quizzes').mkdir()
        (root / 'state' / 'progress.md').write_text('old resume point\n')
        os.environ['WSET_ROOT'] = str(root)
        os.environ['WSET_CONNECTOR_RUNTIME'] = str(runtime)
        os.environ.pop('WSET_CONNECTOR_CONFIG', None)

        import importlib
        import storage
        import previews
        importlib.reload(storage)
        importlib.reload(previews)
        self.storage = storage
        self.previews = previews
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()
        for key in ('WSET_ROOT', 'WSET_CONNECTOR_RUNTIME', 'WSET_CONNECTOR_CONFIG'):
            os.environ.pop(key, None)

    def test_preview_then_apply_appends_content(self):
        preview = self.previews.preview_write('progress', 'new paragraph', mode='append')
        receipt = self.previews.apply_write(preview['preview_id'])
        content = (self.root / 'state' / 'progress.md').read_text()
        self.assertIn('old resume point', content)
        self.assertIn('new paragraph', content)
        self.assertEqual(receipt['target'], 'progress')

    def test_replace_mode_overwrites_whole_file(self):
        preview = self.previews.preview_write('progress', 'brand new resume point', mode='replace')
        self.previews.apply_write(preview['preview_id'])
        content = (self.root / 'state' / 'progress.md').read_text()
        self.assertEqual(content, 'brand new resume point')

    def test_apply_rejects_stale_preview_after_concurrent_edit(self):
        preview = self.previews.preview_write('progress', 'my addition', mode='append')
        # Simulate a concurrent edit, e.g. from a Codex session.
        (self.root / 'state' / 'progress.md').write_text('someone else edited this\n')
        with self.assertRaises(self.storage.WsetError):
            self.previews.apply_write(preview['preview_id'])
        # The file must be untouched by the failed apply.
        self.assertEqual((self.root / 'state' / 'progress.md').read_text(), 'someone else edited this\n')

    def test_apply_unknown_preview_id_raises(self):
        with self.assertRaises(self.storage.WsetError):
            self.previews.apply_write('does-not-exist')

    def test_apply_is_single_use(self):
        preview = self.previews.preview_write('weak_areas', 'a gap', mode='append')
        self.previews.apply_write(preview['preview_id'])
        with self.assertRaises(self.storage.WsetError):
            self.previews.apply_write(preview['preview_id'])

    def test_session_log_target_requires_slug(self):
        with self.assertRaises(self.storage.WsetError):
            self.previews.preview_write('session_log', 'Q1: ...', mode='append', session_slug=None)

    def test_session_log_target_creates_new_file(self):
        preview = self.previews.preview_write('session_log', 'Q1: ...', mode='append', session_slug='loire-quickfire')
        self.previews.apply_write(preview['preview_id'])
        matches = list((self.root / 'quizzes').glob('session-*-loire-quickfire.md'))
        self.assertEqual(len(matches), 1)
        self.assertIn('Q1: ...', matches[0].read_text())


if __name__ == '__main__':
    unittest.main()
