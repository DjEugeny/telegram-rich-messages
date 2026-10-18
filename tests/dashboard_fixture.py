"""Только вымышленные материалы для воспроизводимой браузерной проверки.

python3 tests/dashboard_fixture.py --root /tmp/telegram-dashboard-qa
python3 -c 'import dashboard; dashboard.serve("/tmp/telegram-dashboard-qa", 8766)'
Папка должна быть новой: существующие посты не заменяются.
"""

import argparse
import json
import struct
import sys
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import Store, import_image
from dashboard import build


def _fixture_png():
    width, height = 900, 320
    colors = [(217, 236, 255), (251, 225, 241), (200, 246, 249)]
    rows = b"".join(b"\0" + bytes(colors[min(y * 3 // height, 2)]) * width for y in range(height))

    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def create_fixture(root):
    """Создать 130 постов разных дат/состояний и вернуть index.html."""
    root = Path(root).resolve()
    store = Store(root)
    try:
        if store.list():
            raise ValueError("Для браузерной проверки нужна пустая папка; посты не заменены.")
        with tempfile.TemporaryDirectory() as source_dir:
            source = Path(source_dir) / "fixture.png"
            source.write_bytes(_fixture_png())
            image = import_image(root, source)
        start = datetime(2026, 10, 17, 12, 30, tzinfo=timezone(timedelta(hours=7)))
        statuses = ["draft", "sending", "published", "error", "unknown"]
        for index in range(130):
            title = f"Вымышленный пост {index + 1:03d}"
            blocks = [
                {"type": "heading", "level": 2, "text": title},
                {"type": "paragraph", "text": f"Материал {index + 1} для проверки истории. **Выделение**, *курсив*, `пример кода` и [ссылка](https://example.com/). Все сведения вымышлены."},
                {"type": "list", "items": ["Проверить смысл и факты", "Посмотреть изображение", "Разрешить конкретную версию в чате"]},
                dict(image, type="image", alt="Вымышленное тестовое изображение: три цветные полосы"),
                {"type": "details", "summary": "Подробности тестового материала", "blocks": [{"type": "paragraph", "text": "Раскрывающийся блок работает и без сети."}]},
                {"type": "button", "text": "Открыть пример", "url": "https://example.com/"},
            ]
            if index == 0:
                title = "Проверка разметки и широкой таблицы"
                blocks.append({"type": "table", "headers": [f"Колонка {n}" for n in range(1, 21)], "rows": [[f"Длинное значение {n}" for n in range(1, 21)]]})
            if index == 1:
                title = '</script><script>window.XSS=1</script> & "Тест"'
                blocks.append({"type": "paragraph", "text": '<img src=x onerror="window.XSS=2"> <script>window.XSS=3</script>'})
            stamp = (start - timedelta(days=index)).isoformat()
            # Даты фиксируются на публичном шве создания версии: уже записанные
            # версии остаются неизменяемыми, включая их JSON/Markdown/HTML экспорты.
            with patch("core._now", return_value=stamp):
                post = store.create(title, blocks, {"chat_id": -1009999999999, "chat_title": "Вымышленный канал", "bot_id": 999999999, "bot_username": "fictional_preview_bot", "message_thread_id": 123})
            status = statuses[index % len(statuses)]
            publication = None
            if status == "published":
                publication = json.dumps({"message_id": 1000 + index, "version": 1, "published_version": 1, "published_at": stamp, "url": "https://t.me/example/1000"})
                if index == 2:
                    with patch("core._now", return_value=stamp):
                        store.update(post["id"], title=title + " — с изменениями")
            with store.conn:
                store.conn.execute("UPDATE post_state SET status=?,visual_verified=?,publication=? WHERE post_id=?", (status, int(status == "published" and index % 2 == 0), publication, post["id"]))
    finally:
        store.conn.close()
    return build(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Вымышленная выборка для браузерной проверки")
    parser.add_argument("--root", required=True)
    arguments = parser.parse_args()
    print(create_fixture(arguments.root))
