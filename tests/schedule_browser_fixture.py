"""Вымышленные safe list projections для проверки расписания без сети.

python3 tests/schedule_browser_fixture.py --root /tmp/telegram-schedule-qa
python3 -c 'import dashboard; dashboard.serve("/tmp/telegram-schedule-qa", 8767)'
Не использовать папку с пользовательскими материалами.
"""

import argparse
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import Store, import_image
from dashboard import build


def create_fixture(root, deadline=None):
    """Создать реальные immutable версии; подставить только публичный list seam."""
    from dashboard_fixture import _fixture_png

    root = Path(root).resolve()
    store = Store(root)
    projections = []
    try:
        if store.list():
            raise ValueError("Нужна пустая папка; существующие посты не заменены.")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.png"
            source.write_bytes(_fixture_png())
            image = import_image(root, source)
        states = ["scheduled", "reviewed", "sending", "proposed", "approving", "unknown", "error", "cancelled", "published"]
        for index, status in enumerate(states):
            send_at = deadline if status == "scheduled" and deadline else "2026-10-17T18:00:00+07:00" if status == "scheduled" else "2026-10-20T18:00:00+07:00"
            blocks = [{"type": "heading", "level": 2, "text": "Согласованный текст " + status},
                      {"type": "quote", "text": "Вымышленная цитата 😀"},
                      dict(image, type="image", alt="Тестовые полосы"),
                      {"type": "paragraph", "text": "Материал старой сохранённой версии."}]
            post = store.create("Синтетика: " + status, blocks)
            if status == "scheduled":
                store.update(post["id"], blocks=[{"type": "paragraph", "text": "Новый текст текущей версии"}])
            projections.append({"id": "synthetic-" + status, "post_id": post["id"], "version": 1,
                "status": status, "channel": -100555,
                "channel_title": '</script><script>window.XSS=1</script>' if status == "unknown" else "Вымышленный канал",
                "send_at": send_at,
                "send_date": int(datetime.fromisoformat(send_at).timestamp()),
                "timezone": "Asia/Novosibirsk", "proposal_message_id": 55 if status != "reviewed" else None,
                "published_message_id": 777 if status == "published" else None,
                "source": "schedule-review", "experimental": True, "live_verified": False,
                "evidence": "Человек сверил вымышленный пост" if status == "published" else None,
                "last_error": '<img src=x onerror="window.XSS=2">' if status == "error" else None})
        store.create("Без расписания", [{"type": "paragraph", "text": "Обычный локальный черновик"}])
    finally:
        store.conn.close()
    with patch("scheduling.Scheduling.list", return_value=projections):
        return build(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Синтетическое расписание для браузерной проверки")
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    print(create_fixture(args.root))
