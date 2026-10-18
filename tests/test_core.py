import tempfile
import unittest
from pathlib import Path
import struct
import zlib
import sqlite3
import base64
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from core import Store, compile_post, import_image, validate_media, create_demo, load_text


def png():
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b"")


def jpeg(entropy=b"\x3f"):
    def segment(marker, value):
        return bytes([255, marker]) + struct.pack(">H", len(value) + 2) + value
    tables = b"\0" + b"\1" + b"\0" * 15 + b"\0" + b"\x10" + b"\1" + b"\0" * 15 + b"\0"
    return b"\xff\xd8" + segment(0xdb, b"\0" + b"\1" * 64) + segment(0xc0, b"\x08\0\x01\0\x01\x01\x01\x11\0") + segment(0xc4, tables) + segment(0xda, b"\x01\x01\0\0\x3f\0") + entropy + b"\xff\xd9"


class StoreTests(unittest.TestCase):
    def test_mp4_rejects_damage_unsupported_codec_and_inconsistent_tables(self):
        from core import import_media, import_video
        raw = base64.b64decode((Path(__file__).parent / "fixtures" / "avc-96x160.mp4.base64").read_text())
        def altered(box, relative, replacement):
            data = bytearray(raw)
            offset = data.index(box) + 4 + relative
            data[offset:offset + len(replacement)] = replacement
            return bytes(data)
        invalid = [
            ("zero movie timescale", altered(b"mvhd", 12, b"\0" * 4)),
            ("truncation", raw[:-1]),
            ("junk container", struct.pack(">I4s4sI", 16, b"ftyp", b"isom", 0) + b"junk"),
            ("unsupported codec", raw.replace(b"avc1", b"hvc1")),
            ("chunk outside mdat", altered(b"stco", 8, b"\xff" * 4)),
            ("zero timing", altered(b"stts", 12, b"\0" * 4)),
            ("no AVC configuration", raw.replace(b"avcC", b"junk")),
            ("fragmented", raw + struct.pack(">I4s", 8, b"moof")),
            ("bad top box size", b"\xff\xff\xff\xff" + raw[4:]),
            ("oversize", raw + b"\0" * (50 * 1024 * 1024)),
        ]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source.mp4"
            for label, content in invalid:
                with self.subTest(label=label):
                    source.write_bytes(content)
                    with self.assertRaises(ValueError):
                        import_media(base / "project", source)
            source.write_bytes(png())
            with self.assertRaises(ValueError):
                import_video(base / "project", source)

    def test_real_mp4_import_is_portable_and_preserves_original(self):
        import core
        self.assertTrue(callable(getattr(core, "import_media", None)), "Нужен публичный import_media")
        raw = base64.b64decode((Path(__file__).parent / "fixtures" / "avc-96x160.mp4.base64").read_text())
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "оригинал.mp4"
            source.write_bytes(raw)
            root = base / "project"
            first, second = core.import_media(root, source), core.import_media(root, source)
            self.assertEqual((first["mime"], first["width"], first["height"]), ("video/mp4", 96, 160))
            self.assertAlmostEqual(first["duration"], 1.0, places=2)
            self.assertNotEqual(first["path"], second["path"])
            self.assertEqual(source.read_bytes(), raw)
            store = Store(root)
            post = store.create("Вымышленное видео", [dict(first, type="video", alt="Цветной кадр")])
            store.conn.close()
            moved = base / "перенос"
            root.rename(moved)
            reopened = Store(moved)
            self.assertEqual(validate_media(moved, reopened.get(post["id"]))[0]["mime"], "video/mp4")
            reopened.conn.close()
            self.assertEqual((moved / first["path"]).read_bytes(), raw)
            # Один сквозной шов: импорт → снимок → компиляция → повторная проверка.
            image_source = base / "portrait.png"
            image_source.write_bytes(png())
            image = dict(core.import_media(moved, image_source), type="image", alt="Фото")
            video = dict(first, type="video", alt="Видео")
            for layout in ("collage", "slideshow"):
                for leaves in ([image] * 6, [video, image, image, image]):
                    result = compile_post({"blocks": [{"type": "media_group", "layout": layout, "items": leaves}]})
                    self.assertEqual([item["path"] for item in result["media"]], [item["path"] for item in leaves])
                    self.assertEqual([item["media"]["type"] for item in result["media"]], ["video" if item["type"] == "video" else "photo" for item in leaves])
                    self.assertIn(f'<tg-{layout}>', result["markdown"])
                    self.assertEqual(result["html"].count('class="rich-media-item"'), len(leaves))
                    self.assertIn(f'data-layout="{layout}"', result["html"])
            self.assertEqual(compile_post({"blocks": [video]})["markdown"], '![Видео](tg://video?id=1)')
            unicode_text = "👩🏽‍💻 ❤️ ☀️ 🏳️‍🌈"
            inline = [unicode_text, {"type": "underline", "text": "линия"}, {"type": "strikethrough", "text": "старое"}, {"type": "bold", "text": [{"type": "italic", "text": "важно"}]}, {"type": "code", "text": "<x>`"}, {"type": "custom_emoji", "text": "🔥", "custom_emoji_id": "12345"}, {"type": "link", "text": "ссылка", "url": "https://example.com/a(b)"}, "<script>bad</script>"]
            rich = compile_post({"blocks": [{"type": "quote", "text": "**Первая**\n\nВторая"}, {"type": "heading", "text": inline}, {"type": "paragraph", "text": inline}, {"type": "list", "items": [inline]}]})
            self.assertTrue(rich["markdown"].startswith("> **Первая**\n> \n> Вторая"))
            self.assertIn("<blockquote><strong>Первая</strong><br><br>Вторая</blockquote>", rich["html"])
            self.assertIn(unicode_text, rich["html"])
            self.assertIn(unicode_text, rich["markdown"])
            self.assertIn("<u>линия</u>", rich["markdown"])
            self.assertIn("~~старое~~", rich["markdown"])
            self.assertIn("![🔥](tg://emoji?id=12345)", rich["markdown"])
            self.assertIn('class="custom-emoji" role="img"', rich["html"])
            self.assertIn("<strong><em>важно</em></strong>", rich["html"])
            self.assertIn("<code>&lt;x&gt;`</code>", rich["html"])
            self.assertNotIn("<script>", rich["html"])
            bad_blocks = [
                {"type": "media_group", "layout": "collage", "items": [image]},
                {"type": "media_group", "layout": "collage", "items": [image] * 51},
                {"type": "media_group", "layout": "other", "items": [image, video]},
                {"type": "media_group", "layout": "slideshow", "items": [image, {"type": "paragraph", "text": "x"}]},
                {"type": "paragraph", "text": [{"type": "link", "url": "javascript:alert(1)", "text": [{"type": "bold", "text": "x"}]}]},
                {"type": "paragraph", "text": [{"type": "bold", "text": [{"type": "link", "url": "data:text/html,x", "text": "x"}]}]},
                {"type": "paragraph", "text": [{"type": "custom_emoji", "text": "🔥", "custom_emoji_id": "x"}]},
                {"type": "paragraph", "text": [{"type": "custom_emoji", "text": "", "custom_emoji_id": "123"}]},
            ]
            for bad in bad_blocks:
                with self.assertRaises(ValueError):
                    compile_post({"blocks": [bad]})
            nested_inline = "x"
            for _ in range(17):
                nested_inline = [{"type": "bold", "text": nested_inline}]
            with self.assertRaises(ValueError):
                compile_post({"blocks": [{"type": "paragraph", "text": nested_inline}]})
            with self.assertRaises(ValueError):
                compile_post({"blocks": [{"type": "media_group", "layout": "collage", "items": [image] * 50}, video]})
            for bad_metadata in (dict(video, width=100), dict(video, duration=2.0)):
                with self.assertRaises(ValueError):
                    validate_media(moved, {"blocks": [bad_metadata]})
            target = moved / first["path"]
            target.write_bytes(raw[:-1])
            with self.assertRaises(ValueError):
                validate_media(moved, {"blocks": [video]})
            target.unlink()
            target.symlink_to(source)
            with self.assertRaises(ValueError):
                validate_media(moved, {"blocks": [video]})
            alias = base / "alias.mp4"
            alias.symlink_to(source)
            with self.assertRaises(ValueError):
                core.import_media(moved, alias)
            private = base / "secret.env"
            private.write_bytes(raw)
            with self.assertRaises(ValueError):
                core.import_media(moved, private)

            requests = []
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    requests.append(self.path)
                    self.send_response(302 if self.path == "/redirect" else 200)
                    if self.path == "/redirect":
                        self.send_header("Location", "/video")
                    self.end_headers()
                    self.wfile.write(raw if self.path == "/video" else b"<html>Page</html>")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}"
                self.assertEqual(core.import_media(moved, url + "/video")["sha256"], first["sha256"])
                for route in ("/redirect", "/page"):
                    with self.assertRaises(ValueError):
                        core.import_media(moved, url + route)
                self.assertEqual(requests, ["/video", "/redirect", "/page"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_multiline_list_items_keep_continuations_inside_same_item(self):
        unordered = compile_post({"blocks": [{"type": "list", "items": ["Первая\nПродолжение", "Вторая"]}]})
        self.assertEqual(unordered["markdown"], "- Первая\n  Продолжение\n- Вторая")
        self.assertEqual(unordered["html"], "<ul><li>Первая<br>Продолжение</li><li>Вторая</li></ul>")
        ordered = compile_post({"blocks": [{"type": "list", "ordered": True, "items": ["Строка"] * 9 + ["Десятая\nПродолжение"]}]})
        self.assertTrue(ordered["markdown"].endswith("10. Десятая\n    Продолжение"))
        self.assertIn("<li>Десятая<br>Продолжение</li>", ordered["html"])

    def test_third_export_failure_removes_uncommitted_export_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            post = store.create("Исходный", [{"type": "paragraph", "text": "Текст"}])
            export_dir = Path(directory) / "posts" / post["id"]
            before = {path.name: path.read_bytes() for path in export_dir.iterdir()}
            original_replace = os.replace
            replacements = []

            def fail_third(source, destination):
                replacements.append(Path(destination).suffix)
                if len(replacements) == 3:
                    raise OSError("third export failed")
                return original_replace(source, destination)

            with patch("os.replace", side_effect=fail_third):
                with self.assertRaisesRegex(OSError, "third export failed"):
                    store.update(post["id"], title="Несохранённый")
            self.assertEqual(replacements, [".json", ".md", ".html"])
            self.assertEqual(store.get(post["id"])["version"], 1)
            self.assertEqual({path.name: path.read_bytes() for path in export_dir.iterdir()}, before)
            saved = store.update(post["id"], title="Сохранённый")
            self.assertEqual(saved["version"], 2)
            self.assertEqual(len(list(export_dir.glob("v0002.*"))), 3)

    def test_jpeg_rejects_complete_huffman_tree_with_all_ones_code(self):
        # Два кода длины 1 занимают всё дерево; код 1 зарезервирован JPEG padding.
        invalid = jpeg().replace(
            b"\xff\xc4\0\x26\0\x01" + b"\0" * 15 + b"\0",
            b"\xff\xc4\0\x27\0\x02" + b"\0" * 15 + b"\0\x01",
            1,
        )
        self.assertNotEqual(invalid, jpeg())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.jpg"
            fixture = base64.b64decode((Path(__file__).resolve().parents[1] / "examples" / "invalid-huffman-all-ones.jpg.base64").read_text())
            for content in (invalid, fixture):
                path.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "Хаффмана"):
                    import_image(Path(directory) / "project", path)

    def test_invalid_destination_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            for destination in ([], "", False, {"bot_id": True}, {"chat_id": "javascript:1"}):
                with self.assertRaises(ValueError):
                    store.create("Пост", [{"type": "paragraph", "text": "Текст"}], destination=destination)
            self.assertEqual(store.list(), [])
    def test_versions_are_append_only_even_with_shared_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            post = store.create("Неизменяемый", [{"type": "paragraph", "text": "Снимок"}])
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE versions SET title='Изменён' WHERE post_id=?", (post["id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("DELETE FROM versions WHERE post_id=?", (post["id"],))
            self.assertEqual(store.get(post["id"], 1)["title"], "Неизменяемый")
    def test_demo_is_idempotent_and_text_input_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            first, second = create_demo(root), create_demo(root)
            self.assertEqual((first["id"], first["version"]), (second["id"], second["version"]))
            self.assertEqual(len(Store(root).list()), 1)
            self.assertEqual(len(validate_media(root, first)), 1)
            self.assertIn("<details>", compile_post(first)["html"])
            file = Path(directory) / "текст.txt"
            file.write_text("Мой текст\nВторая строка", encoding="utf-8")
            self.assertEqual(load_text(file), "Мой текст\nВторая строка")
            self.assertEqual(load_text(str(file)), str(file))
            self.assertEqual(load_text(str(file), from_file=True), "Мой текст\nВторая строка")
    def test_saved_image_hash_and_containment_are_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            path = Path(directory) / "source.png"
            path.write_bytes(png())
            image = dict(import_image(root, path), type="image", alt="Пример")
            store = Store(root)
            post = store.create("Картинка", [image])
            self.assertEqual(validate_media(root, post)[0]["id"], "1")
            (root / image["path"]).write_bytes(png() + b"corrupt")
            with self.assertRaises(ValueError):
                validate_media(root, post)
            with self.assertRaises(ValueError):
                store.update(post["id"], blocks=[image])
            self.assertEqual(store.get(post["id"])["version"], 1)
            imported = root / image["path"]
            imported.unlink()
            imported.symlink_to(path)
            with self.assertRaises(ValueError):
                validate_media(root, post)
            with self.assertRaises(ValueError):
                import_image(root, 123)
    def test_jpeg_rejects_corrupt_entropy_and_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.jpg"
            path.write_bytes(jpeg())
            result = import_image(Path(directory) / "project", path)
            self.assertEqual((result["mime"], result["width"], result["height"]), ("image/jpeg", 1, 1))
            for content in (jpeg(b"\xff\0"), jpeg()[:-1]):
                path.write_bytes(content)
                with self.assertRaises(ValueError):
                    import_image(Path(directory) / "project", path)
            real = base64.b64decode((Path(__file__).resolve().parents[1] / "examples" / "baseline-16x16.jpg.base64").read_text())
            path.write_bytes(real)
            real_result = import_image(Path(directory) / "project", path)
            self.assertEqual((real_result["width"], real_result["height"]), (16, 16))
            for content in (real[:-8], png()[:-5], png() + b"extra", b"<html>not a photo</html>", b"x" * (10 * 1024 * 1024 + 1)):
                path.write_bytes(content)
                with self.assertRaises(ValueError):
                    import_image(Path(directory) / "project", path)
            damaged = bytearray(png())
            damaged[-15] ^= 1
            path.write_bytes(damaged)
            with self.assertRaises(ValueError):
                import_image(Path(directory) / "project", path)
            interlace = bytearray(png())
            interlace[28] = 1
            interlace[29:33] = struct.pack(">I", zlib.crc32(interlace[12:29]) & 0xffffffff)
            path.write_bytes(interlace)
            with self.assertRaisesRegex(ValueError, "interlace"):
                import_image(Path(directory) / "project", path)
    def test_image_import_copies_unique_bytes_and_survives_project_move(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "исходник.png"
            source.write_bytes(png())
            project = base / "project"
            first = import_image(project, source)
            second = import_image(project, source)
            self.assertNotEqual(first["path"], second["path"])
            self.assertEqual((first["mime"], first["width"], first["height"]), ("image/png", 1, 1))
            self.assertEqual((project / first["path"]).read_bytes(), png())
            self.assertEqual(source.read_bytes(), png())
            moved = base / "moved"
            project.rename(moved)
            self.assertEqual((moved / first["path"]).read_bytes(), png())
            requests = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    requests.append(self.path)
                    if self.path == "/redirect":
                        self.send_response(302)
                        self.send_header("Location", "/photo")
                        self.end_headers()
                    else:
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(png() if self.path == "/photo" else b"<html>Page</html>")

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}"
                downloaded = import_image(moved, url + "/photo")
                self.assertEqual(downloaded["sha256"], first["sha256"])
                with self.assertRaises(ValueError):
                    import_image(moved, url + "/redirect")
                with self.assertRaises(ValueError):
                    import_image(moved, url + "/page")
                self.assertEqual(requests, ["/photo", "/redirect", "/page"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
    def test_compiler_escapes_source_and_renders_supported_blocks(self):
        result = compile_post({"blocks": [
            {"type": "heading", "level": 2, "text": "Заголовок"},
            {"type": "paragraph", "text": '**Важно** <script>alert(1)</script> [ссылка](https://example.com)'},
            {"type": "list", "items": ["Один", "Два"], "ordered": True},
            {"type": "details", "summary": "Детали", "blocks": [{"type": "paragraph", "text": "Внутри"}]},
            {"type": "table", "headers": ["Имя"], "rows": [["Анна"]]},
            {"type": "button", "text": "Открыть", "url": "https://example.com"},
        ]})
        self.assertIn("<strong>Важно</strong>", result["html"])
        self.assertNotIn("<script>", result["html"])
        self.assertIn("&lt;script&gt;", result["html"])
        self.assertIn('<details><summary>Детали</summary>', result["markdown"])
        self.assertIn('<tg-button type="url" url="https://example.com">Открыть</tg-button>', result["markdown"])
        self.assertIn("<ol>", result["html"])
        self.assertIn("<table>", result["html"])
        for block in (
            {"type": "paragraph", "text": "[Клик](javascript:alert)"},
            {"type": "button", "text": "Открыть", "url": "data:text/html,bad"},
            {"type": "image", "path": "media/../outside.png", "sha256": "0" * 64, "mime": "image/png"},
            {"type": "table", "headers": ["x"] * 21, "rows": []},
            {"type": "paragraph", "text": "x" * 32769},
            {"type": "list", "items": ["x"] * 501},
        ):
            with self.assertRaises(ValueError):
                compile_post({"blocks": [block]})
        nested = {"type": "paragraph", "text": "x"}
        for _ in range(16):
            nested = {"type": "details", "summary": "x", "blocks": [nested]}
        with self.assertRaises(ValueError):
            compile_post({"blocks": [nested]})
    def test_versions_survive_reopen_and_exports_match_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            first = store.create("Первый", [{"type": "paragraph", "text": "Исходный текст"}])
            second = store.update(first["id"], title="Второй")
            self.assertEqual((first["version"], second["version"]), (1, 2))
            store.conn.close()
            reopened = Store(directory)
            self.assertEqual(reopened.get(first["id"], 1)["title"], "Первый")
            self.assertEqual(reopened.get(first["id"])["title"], "Второй")
            self.assertEqual(len(reopened.list()), 1)
            self.assertTrue((Path(directory) / "posts" / first["id"] / "v0001.json").is_file())
            self.assertEqual(reopened.get(first["id"])["status"], "draft")
            with patch("os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    reopened.update(first["id"], title="Не сохранён")
            self.assertEqual(reopened.get(first["id"])["version"], 2)


if __name__ == "__main__":
    unittest.main()
