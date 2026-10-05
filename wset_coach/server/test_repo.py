import importlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


class RepoApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / 'repo'
        (root / 'state').mkdir(parents=True)
        (root / 'quizzes').mkdir()
        (root / 'state' / 'progress.md').write_text('old resume point\n')
        git(root, 'init', '-q', '-b', 'main')
        git(root, 'config', 'user.name', 't')
        git(root, 'config', 'user.email', 't@example.invalid')
        git(root, 'add', '-A')
        git(root, 'commit', '-q', '-m', 'seed')
        os.environ['WSET_ROOT'] = str(root)
        os.environ['WSET_CONNECTOR_RUNTIME'] = str(Path(self.tmp.name) / 'runtime')
        import storage, previews, repo
        importlib.reload(storage)
        importlib.reload(repo)
        importlib.reload(previews)
        self.previews, self.storage, self.repo = previews, storage, repo
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()
        for key in ('WSET_ROOT', 'WSET_CONNECTOR_RUNTIME'):
            os.environ.pop(key, None)

    def test_apply_commits_the_file(self):
        preview = self.previews.preview_write('session_log', 'Q1 raw answer', session_slug='scratch')
        receipt = self.previews.apply_write(preview['preview_id'])
        self.assertEqual(receipt['commit'], git(self.root, 'rev-parse', 'HEAD'))
        self.assertIn('session scratch', git(self.root, 'log', '-1', '--format=%s'))
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_identical_replace_is_not_an_error(self):
        preview = self.previews.preview_write('progress', 'old resume point\n', mode='replace')
        receipt = self.previews.apply_write(preview['preview_id'])
        self.assertEqual(receipt['commit'], git(self.root, 'rev-parse', 'HEAD'))

    def test_stale_preview_rejected_and_nothing_committed(self):
        head = git(self.root, 'rev-parse', 'HEAD')
        preview = self.previews.preview_write('progress', 'mine')
        (self.root / 'state' / 'progress.md').write_text('pushed from the Mac\n')
        with self.assertRaises(self.storage.WsetError):
            self.previews.apply_write(preview['preview_id'])
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual((self.root / 'state' / 'progress.md').read_text(), 'pushed from the Mac\n')

    def test_failed_commit_rolls_back_the_write(self):
        preview = self.previews.preview_write('progress', 'new paragraph')
        original = self.repo.commit_file

        def boom(*a, **k):
            raise RuntimeError('disk full')
        self.repo.commit_file = boom
        try:
            with self.assertRaises(self.storage.WsetError):
                self.previews.apply_write(preview['preview_id'])
        finally:
            self.repo.commit_file = original
        self.assertEqual((self.root / 'state' / 'progress.md').read_text(), 'old resume point\n')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_failed_commit_removes_a_new_session_file(self):
        preview = self.previews.preview_write('session_log', 'Q1', session_slug='new-one')
        original = self.repo.commit_file
        self.repo.commit_file = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x'))
        try:
            with self.assertRaises(self.storage.WsetError):
                self.previews.apply_write(preview['preview_id'])
        finally:
            self.repo.commit_file = original
        self.assertEqual(list((self.root / 'quizzes').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
