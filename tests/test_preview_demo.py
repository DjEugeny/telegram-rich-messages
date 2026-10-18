"""Временная демонстрация не меняет рабочие данные и не использует токен."""

import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from core import Store
import tool


class PreviewDemoTests(unittest.TestCase):
    def test_preview_is_isolated_and_cleans_up_after_server_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root)
            try:
                personal = store.create('Мой пост', [{'type': 'paragraph', 'text': 'Мой текст'}], {})
            finally:
                store.conn.close()
            demo_roots = []

            def observe(demo_root, port):
                demo_roots.append(demo_root)
                self.assertNotEqual(demo_root, root)
                self.assertFalse(demo_root.is_relative_to(root))
                self.assertEqual(port, 8795)
                demo_store = Store(demo_root)
                try:
                    posts = demo_store.list()
                    self.assertEqual(len(posts), 5)
                    self.assertNotIn(personal['id'], {post['id'] for post in posts})
                finally:
                    demo_store.conn.close()
                page = (demo_root / 'index.html').read_text(encoding='utf-8')
                self.assertIn('Демонстрация', page)
                self.assertIn('В личной истории они не сохраняются', page)

            with patch('tool.load_api', side_effect=AssertionError('Токен не нужен')) as loader:
                with patch('tool.dashboard.serve', side_effect=observe), redirect_stdout(io.StringIO()):
                    code = tool.main(['--root', str(root), 'preview-demo', '--port', '8795'])
                loader.assert_not_called()
            self.assertEqual(code, 0)
            self.assertEqual(len(demo_roots), 1)
            self.assertFalse(demo_roots[0].exists())
            store = Store(root)
            try:
                self.assertEqual(store.list(), [personal])
            finally:
                store.conn.close()

    def test_server_failure_leaves_fresh_workspace_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seen = []

            def fail(demo_root, port):
                seen.append(demo_root)
                raise OSError('Порт занят')

            with patch('tool.load_api', side_effect=AssertionError('Токен не нужен')) as loader:
                with patch('tool.dashboard.serve', side_effect=fail):
                    with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                        code = tool.main(['--root', str(root), 'preview-demo'])
                loader.assert_not_called()
            self.assertEqual(code, 2)
            self.assertFalse(seen[0].exists())
            self.assertEqual(list(root.iterdir()), [])
