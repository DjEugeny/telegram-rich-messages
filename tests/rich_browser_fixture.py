"""Автономные вымышленные медиа для браузерной проверки через HTTP.

python3 tests/rich_browser_fixture.py --root /tmp/telegram-rich-qa
python3 tests/rich_browser_fixture.py --root /tmp/telegram-rich-storage-qa --storage-denied
python3 -c 'import dashboard; dashboard.serve("/tmp/telegram-rich-qa", 8767)'
Нужна новая папка. ffmpeg, сеть и токен при запуске не используются.
"""

import argparse
import base64
import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import Store, import_media
from dashboard import build


def _png(width, height, index):
    """Тестовые цветовые поля и круг: искажение пропорций заметно визуально."""
    palette = [(217, 236, 255), (251, 225, 241), (200, 246, 249), (248, 231, 201), (220, 229, 211), (226, 218, 245)]
    background = palette[index % len(palette)]
    radius = min(width, height) // 4
    rows = []
    for y in range(height):
        row = bytearray(b"\0")
        for x in range(width):
            circle = (x - width // 2) ** 2 + (y - height // 2) ** 2 < radius ** 2
            color = (65, 84, 110) if circle else background
            if x < 8 or y < 8 or x >= width - 8 or y >= height - 8:
                color = (91, 110, 132)
            row.extend(color)
        rows.append(row)

    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def create_fixture(root, storage_denied=False):
    """Создать 4 аналога, slideshow и явную ошибку; вернуть index.html."""
    root = Path(root).resolve()
    store = Store(root)
    try:
        if store.list():
            raise ValueError("Нужна пустая папка; существующие посты не изменены.")
        with tempfile.TemporaryDirectory() as source_dir:
            source_dir = Path(source_dir)

            def image(width, height, index):
                source = source_dir / f"image-{width}-{height}-{index}.png"
                source.write_bytes(_png(width, height, index))
                return dict(import_media(root, source), type="image", alt=f"Вымышленная иллюстрация {index + 1}, {width} × {height}: круг и цветовое поле")

            video_source = source_dir / "synthetic-4s.mp4"
            encoded = Path(__file__).with_name("fixtures") / "browser-avc-144x256.mp4.base64"
            video_source.write_bytes(base64.b64decode(encoded.read_bytes()))
            video = dict(import_media(root, video_source), type="video", alt="Вымышленное тестовое видео, 4 секунды, 144 × 256")

            def create(title, media, extra=None):
                blocks = [media, {"type": "heading", "level": 2, "text": title}, {"type": "paragraph", "text": "Вымышленный материал для проверки. Круг остаётся круглым, края изображения полностью видны. Медиа идёт перед текстом."}]
                blocks.extend(extra or [])
                blocks.append({"type": "details", "summary": "Подробности проверки", "blocks": [{"type": "paragraph", "text": "Проверьте порядок, пропорции и доступность с клавиатуры. Все файлы локальные."}]})
                store.create(title, blocks)

            create("01 · Шесть иллюстраций 4:5", {"type": "media_group", "layout": "collage", "items": [image(240, 300, index) for index in range(6)]})
            create("02 · Одиночная иллюстрация 3:4", image(240, 320, 1), [{"type": "paragraph", "text": [{"type": "underline", "text": "Подчёркнутый фрагмент"}, " и обычный текст."]}])
            create("03 · Видео и три иллюстрации 9:16", {"type": "media_group", "layout": "collage", "items": [video] + [image(180, 320, index) for index in range(3)]}, [
                {"type": "quote", "text": "**Вымышленная цитата.**\nВторая строка цитаты."},
                {"type": "paragraph", "text": ["Unicode: 👩🏽‍💻 ❤️ 🏳️‍🌈. Custom: ", {"type": "custom_emoji", "text": "⭐", "custom_emoji_id": "123456789"}]},
            ])
            create("04 · Иллюстрация 4:5 и цитата", image(240, 300, 3), [{"type": "quote", "text": "Вымышленная цитата отделяется от основного текста.\n\nВторой абзац сохраняется."}])
            create("05 · Слайд-шоу: видео, пейзаж, квадрат", {"type": "media_group", "layout": "slideshow", "items": [video, image(480, 270, 4), image(300, 300, 5)]})
            broken = dict(import_media(root, video_source), type="video", alt="Намеренно отсутствующий файл для проверки ошибки")
            create("06 · Ошибка видео: отсутствующий файл", broken)
            (root / broken["path"]).unlink()
    finally:
        store.conn.close()
    page = build(root)
    if storage_denied:
        # Только копия JS в отдельном fixture: inline script блокируется CSP.
        # Override выполняется до приложения, не требуя browser evaluate.
        script = root / "web" / "app.js"
        override = "Object.defineProperty(window, 'localStorage', {get() {throw new DOMException('Storage unavailable', 'SecurityError');}});\n"
        script.write_text(override + script.read_text(encoding="utf-8"), encoding="utf-8")
    return page


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Вымышленные Rich Media для браузерной проверки")
    parser.add_argument("--root", required=True)
    parser.add_argument("--storage-denied", action="store_true", help="Запретить localStorage только в копии тестового дашборда")
    arguments = parser.parse_args()
    print(create_fixture(arguments.root, storage_denied=arguments.storage_denied))
