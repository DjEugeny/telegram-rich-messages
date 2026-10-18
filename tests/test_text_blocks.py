"""Готовый текст сохраняет структуру при подготовке Telegram-поста."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import compile_post
from text_blocks import parse_text_blocks
import tool


class TextBlocksTests(unittest.TestCase):
    def test_blank_lines_lists_and_author_dashes(self):
        source = (
            "Вступление e\u0301\n\n"
            "- Первый пункт\n\n- Второй пункт\n\n"
            "1. Начало\n2. Конец\n\n"
            "— рынок огромен\n— конкурентов много\n\nФинал"
        )
        blocks = parse_text_blocks(source)
        self.assertEqual(blocks, [
            {"type": "paragraph", "text": "Вступление e\u0301"},
            {"type": "list", "items": ["Первый пункт", "Второй пункт"], "ordered": False},
            {"type": "list", "items": ["Начало", "Конец"], "ordered": True},
            {"type": "paragraph", "text": "— рынок огромен\n— конкурентов много"},
            {"type": "paragraph", "text": "Финал"},
        ])
        markdown = compile_post({"blocks": blocks})["markdown"]
        self.assertIn("- Первый пункт\n- Второй пункт", markdown)
        self.assertIn("1. Начало\n2. Конец", markdown)
        self.assertIn("— рынок огромен<br>— конкурентов много", markdown)
        self.assertIn("e\u0301", markdown)

    def test_nonsequential_numbers_and_plain_dashes_keep_literal_text(self):
        blocks = parse_text_blocks("3. Не начало\n— диалог\n-не маркер\n\n1. Один\n3. Не два")
        self.assertEqual(blocks, [
            {"type": "paragraph", "text": "3. Не начало\n— диалог\n-не маркер"},
            {"type": "list", "items": ["Один"], "ordered": True},
            {"type": "paragraph", "text": "3. Не два"},
        ])

    def test_create_from_file_and_update_from_text_export_exact_markdown(self):
        def run(root, *arguments):
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(tool.main(["--root", str(root), *arguments]), 0, errors.getvalue())
            return json.loads(output.getvalue())

        with tempfile.TemporaryDirectory() as directory, patch("tool.load_api", side_effect=AssertionError("Сеть не нужна")):
            root = Path(directory)
            source = root / "материал.txt"
            source.write_text("Вступление\n\n- Первый\n- Второй\n\n— авторский текст", encoding="utf-8")
            post = run(root, "create", "--title", "Проверка", "--file", str(source))
            self.assertEqual([block["type"] for block in post["blocks"]], ["paragraph", "list", "paragraph"])
            export = root / "posts" / post["id"] / "v0001.md"
            self.assertEqual(export.read_text(encoding="utf-8"), compile_post(post)["markdown"])
            self.assertIn("- Первый\n- Второй", export.read_text(encoding="utf-8"))

            updated = run(root, "update", post["id"], "--text", "Новая версия\n\n1. Первый\n2. Второй")
            self.assertEqual(updated["version"], 2)
            self.assertEqual(updated["blocks"][1]["items"], ["Первый", "Второй"])
            self.assertEqual(export.read_text(encoding="utf-8"), compile_post(post)["markdown"])
            self.assertEqual((root / "posts" / post["id"] / "v0002.md").read_text(encoding="utf-8"),
                             compile_post(updated)["markdown"])


if __name__ == "__main__":
    unittest.main()
