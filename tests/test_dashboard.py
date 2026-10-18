"""Проверки публичных границ статического дашборда."""

import json
import http.client
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from core import Store
from dashboard import build, make_server
from dashboard_fixture import create_fixture


class DashboardTests(unittest.TestCase):
    def test_schedule_fixture_covers_states_and_separate_immutable_content(self):
        from schedule_browser_fixture import create_fixture as schedule_fixture
        with tempfile.TemporaryDirectory() as directory:
            page = schedule_fixture(directory).read_text(encoding="utf-8")
            encoded = re.search(r'<script id="post-data" type="application/json">(.*?)</script>', page, re.S).group(1)
            posts = json.loads(encoded)
            states = {schedule["status"] for post in posts for schedule in post["schedule"]}
            self.assertEqual(states, {"reviewed", "sending", "proposed", "approving", "scheduled", "unknown", "error", "cancelled", "published"})
            self.assertTrue(any(not post["schedule"] for post in posts))
            changed = next(post for post in posts if post["version"] == 2)
            self.assertIn("Согласованный текст", changed["schedule"][0]["html"])
            self.assertNotIn("Новый текст", changed["schedule"][0]["html"])
            self.assertIn("Новый текст", changed["html"])
            self.assertNotIn("</script", encoded)
            self.assertTrue(all(not schedule["live_verified"] for post in posts for schedule in post["schedule"]))

    def test_mp4_invalid_or_multiple_ranges_are_416_without_file_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "media").mkdir()
            (root / "media" / "clip.mp4").write_bytes(b"0123456789")
            server = make_server(root)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                cases = [[value] for value in ["bytes=10-", "bytes=7-2", "bytes=-0", "bytes=-", "bytes=0-1,4-5", "items=0-1", "bytes=word-", "bytes=" + "9" * 5000 + "-"]]
                cases.append(["bytes=0-1", "bytes=4-5"])
                for values in cases:
                    with self.subTest(ranges=[value[:30] for value in values]):
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                        try:
                            connection.putrequest("GET", "/media/clip.mp4")
                            for value in values:
                                connection.putheader("Range", value)
                            connection.endheaders()
                            response = connection.getresponse()
                            self.assertEqual(response.status, 416)
                            self.assertEqual(response.headers["Content-Range"], "bytes */10")
                            self.assertEqual(response.read(), b"")
                        finally:
                            connection.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    def test_mp4_full_head_and_single_byte_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "media").mkdir()
            (root / "media" / "clip.mp4").write_bytes(b"0123456789")
            server = make_server(root)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            url = f"http://127.0.0.1:{server.server_port}/media/clip.mp4"
            try:
                with urlopen(url) as response:
                    self.assertEqual(response.read(), b"0123456789")
                    self.assertEqual(response.headers["Content-Type"], "video/mp4")
                    self.assertEqual(response.headers["Accept-Ranges"], "bytes")
                    self.assertIn("media-src 'self'", response.headers["Content-Security-Policy"])
                    self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                with urlopen(Request(url, method="HEAD")) as response:
                    self.assertEqual(response.headers["Content-Length"], "10")
                    self.assertEqual(response.read(), b"")
                for value, expected, content_range in [
                    ("bytes=2-5", b"2345", "bytes 2-5/10"),
                    ("bytes=7-", b"789", "bytes 7-9/10"),
                    ("bytes=-3", b"789", "bytes 7-9/10"),
                    ("bytes=8-99", b"89", "bytes 8-9/10"),
                ]:
                    with self.subTest(range=value), urlopen(Request(url, headers={"Range": value})) as response:
                        self.assertEqual(response.status, 206)
                        self.assertEqual(response.headers["Content-Range"], content_range)
                        self.assertEqual(response.headers["Content-Length"], str(len(expected)))
                        self.assertEqual(response.read(), expected)
                with urlopen(Request(url, method="HEAD", headers={"Range": "bytes=2-5"})) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers["Content-Length"], "4")
                    self.assertEqual(response.read(), b"")
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    def test_browser_fixture_keeps_version_history_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            page = create_fixture(directory)
            self.assertTrue(page.is_file())
            store = Store(directory)
            try:
                posts = store.list()
                self.assertEqual(len(posts), 130)
                self.assertEqual({post["status"] for post in posts}, {"draft", "sending", "published", "error", "unknown"})
                self.assertEqual(posts[0]["updated_at"], "2026-10-17T12:30:00+07:00")
                self.assertEqual(posts[-1]["updated_at"], "2026-06-10T12:30:00+07:00")
                changed = next(post for post in posts if post["version"] == 2)
                self.assertEqual(changed["publication"]["published_version"], 1)
                self.assertEqual(store.get(changed["id"], 1)["updated_at"], "2026-10-15T12:30:00+07:00")
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute("UPDATE versions SET created_at='1900-01-01' WHERE post_id=?", (posts[0]["id"],))
            finally:
                store.conn.rollback()
                store.conn.close()

    def test_template_markers_in_user_text_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.create("__CSP__", [{"type": "paragraph", "text": "__POST_DATA__ __CSP__"}])
            store.conn.close()
            page = build(directory).read_text(encoding="utf-8")
            encoded = re.search(r'<script id="post-data" type="application/json">(.*?)</script>', page, re.S).group(1)
            self.assertEqual(json.loads(encoded)[0]["title"], "__CSP__")

    def test_embedded_export_preserves_safe_content_without_copying_server_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root)
            payload = '</script><script>window.XSS=1</script> &\u2028'
            post = store.create(payload, [{"type": "paragraph", "text": payload}])
            store.conn.close()
            output = build(root)
            page = output.read_text(encoding="utf-8")
            encoded = re.search(r'<script id="post-data" type="application/json">(.*?)</script>', page, re.S).group(1)
            exported = json.loads(encoded)
            self.assertEqual(exported[0]["id"], post["id"])
            self.assertEqual(exported[0]["title"], payload)
            self.assertNotIn("</script", encoded.lower())
            self.assertNotIn("<script>", exported[0]["html"])
            self.assertNotIn("blocks", exported[0])
            self.assertTrue((root / "web" / "app.js").is_file())
            self.assertFalse((root / "web" / "shell.html").exists())

    def test_server_only_serves_public_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("PUBLIC", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "state.sqlite3").write_text("PRIVATE", encoding="utf-8")
            (root / "data" / "private.mp4").write_bytes(b"PRIVATE")
            (root / "media").mkdir()
            (root / "media" / "escape.mp4").symlink_to(root / "data" / "private.mp4")
            (root / "media" / "linked").symlink_to(root / "data", target_is_directory=True)
            server = make_server(root)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(base + "/") as response:
                    self.assertEqual(response.read(), b"PUBLIC")
                for route in ["/data/state.sqlite3", "/core.py", "/web/shell.html", "/media/%2e%2e/data/private.mp4", "/media/escape.mp4", "/media/linked/private.mp4", "/media/%00.mp4"]:
                    with self.subTest(route=route), self.assertRaises(HTTPError) as caught:
                        urlopen(base + route)
                    self.assertEqual(caught.exception.code, 404)
                    caught.exception.close()
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(base + "/", headers={"Host": "evil.example"}))
                self.assertEqual(caught.exception.code, 403)
                caught.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    def test_embedded_schedule_uses_allowlist_and_snapshot_version(self):
        from scheduling import Scheduling
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                with patch("core._now", return_value="2026-10-18T10:00:00+00:00"):
                    post = store.create("Запланированный <пост>", [{"type": "paragraph", "text": "Старая версия"}], {})
                from test_scheduling import FakeScheduleAPI
                manager = Scheduling(store, FakeScheduleAPI())
                manager.configure(channel=-100555, dm_chat_id=-200555, topic_id=7, timezone_name="UTC")
                operation = manager.review(post["id"], "2026-10-19T10:00:00+00:00", experimental=True,
                                            now=datetime(2026, 10, 18, 10, tzinfo=timezone.utc))
                with patch("core._now", return_value="2026-10-18T11:00:00+00:00"):
                    store.update(post["id"], title="Текущая версия", blocks=[{"type": "paragraph", "text": "Новая версия"}])
                page = build(directory).read_text(encoding="utf-8")
                encoded = re.search(r'<script id="post-data" type="application/json">(.*?)</script>', page, re.S).group(1)
                item = next(value for value in json.loads(encoded) if value["id"] == post["id"])
                schedule = item["schedule"][0]
                self.assertEqual(schedule["post_id"], post["id"])
                self.assertEqual(schedule["version"], 1)
                self.assertEqual(schedule["status"], "reviewed")
                self.assertEqual(schedule["timezone"], "UTC")
                self.assertNotIn("snapshot", schedule)
                self.assertNotIn("fingerprint", schedule)
                self.assertNotIn("receipt", schedule)
                self.assertIn("Старая версия", schedule["html"])
                self.assertNotIn("Новая версия", schedule["html"])
                self.assertEqual(item["version"], 2)
                self.assertEqual(operation["id"], schedule["id"])
            finally:
                store.conn.close()

    def test_schedule_projection_drops_private_fields_and_never_substitutes_current_version(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            post = store.create("Текущий", [{"type": "paragraph", "text": "НЕ ПОДМЕНЯТЬ"}])
            store.conn.close()
            unsafe = {"id": "synthetic", "post_id": post["id"], "version": None,
                      "status": "unknown", "channel_title": "</script><script>window.XSS=1</script>",
                      "snapshot": {"secret": "PRIVATE_SENTINEL"}, "receipts": "PRIVATE_SENTINEL",
                      "fingerprint": "PRIVATE_SENTINEL"}
            with patch("scheduling.Scheduling.list", return_value=[unsafe]):
                page = build(directory).read_text(encoding="utf-8")
            encoded = re.search(r'<script id="post-data" type="application/json">(.*?)</script>', page, re.S).group(1)
            schedule = json.loads(encoded)[0]["schedule"][0]
            self.assertEqual(schedule["html"], "")
            self.assertIn("недоступна", schedule["content_error"])
            self.assertNotIn("PRIVATE_SENTINEL", page)
            self.assertNotIn("</script", encoded)
            self.assertEqual(schedule["channel_title"], unsafe["channel_title"])

    def test_server_cannot_serve_private_file_through_index_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            private = root / "data" / "state.sqlite3"
            private.write_bytes(b"PRIVATE")
            (root / "index.html").symlink_to(private)
            server = make_server(root)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with self.assertRaises(HTTPError) as caught:
                    urlopen(f"http://127.0.0.1:{server.server_port}/")
                self.assertEqual(caught.exception.code, 404)
                caught.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
