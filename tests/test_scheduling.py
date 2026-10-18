"""Первые тесты experimental-адаптера расписания (только stdlib, синтетика)."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


class FakeScheduleAPI:
    """Синтетический DM/preflight: канал -100555, DM -200555, topic 7."""

    def __init__(self, bot_id=101, mode="success"):
        self.calls = []
        self.bot_id = bot_id
        self.mode = mode

    def call(self, method, payload=None, files=None):
        self.calls.append((method, payload or {}, files or {}))
        if method == "getMe":
            return {"id": self.bot_id, "is_bot": True, "username": "fictional_bot"}
        if method == "getChat":
            return {"id": -200555, "type": "channel", "title": "Вымышленные DM",
                    "is_direct_messages": True, "parent_chat": {"id": -100555}}
        if method == "getChatMember":
            return {"status": "administrator", "can_post_messages": True,
                    "can_manage_direct_messages": True}
        if method == "sendRichMessage":
            if self.mode == "timeout":
                raise TimeoutError("synthetic timeout")
            if self.mode == "reject":
                from scheduling import SchedulingError
                raise SchedulingError("Telegram отклонил запрос (код 403).")
            message = dict(payload or {})
            rich = message.get("rich_message") or {}
            params = message.get("suggested_post_parameters") or {}
            if ("price" in params or "paid" in str(message)):
                raise AssertionError("paid path must not be used")
            return {"message_id": 55, "chat": {"id": -200555},
                    "suggested_post_info": {"state": "pending", "send_date": params.get("send_date")}}
        if method == "approveSuggestedPost":
            if self.mode == "approve-timeout":
                raise TimeoutError("synthetic timeout")
            if self.mode == "approve-false":
                return {"ok": False}
            return True
        raise AssertionError("unexpected method " + method)


def _manager(directory, api=None):
    from core import Store
    from scheduling import Scheduling
    store = Store(directory)
    manager = Scheduling(store, api or FakeScheduleAPI())
    manager.configure(channel=-100555, dm_chat_id=-200555, topic_id=7,
                      timezone_name="Europe/Moscow")
    return manager


NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
WHEN = "2026-10-19T18:00:00+03:00"


class SchedulingTimeTests(unittest.TestCase):
    def test_iso_naive_interpreted_in_zone(self):
        from scheduling import parse_schedule_time
        now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
        result = parse_schedule_time("2026-10-19T18:00:00", "Europe/Moscow", now=now)
        # 18:00 MSK = 15:00Z 19.10.2026
        self.assertEqual(result["send_date"], int(datetime(2026, 10, 19, 15, 0, 0, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(result["timezone"], "Europe/Moscow")
        self.assertIn("+03:00", result["iso"])

    def test_russian_tomorrow_at_time(self):
        from scheduling import parse_schedule_time
        now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
        result = parse_schedule_time("завтра в 18:00", "Europe/Moscow", now=now)
        # 19.10.2026 18:00 MSK = 15:00Z
        self.assertEqual(result["send_date"], int(datetime(2026, 10, 19, 15, 0, 0, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(result["timezone"], "Europe/Moscow")

    def test_rejects_past_and_far_future(self):
        from scheduling import parse_schedule_time
        now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            parse_schedule_time("2026-10-18T14:00:00+03:00", "Europe/Moscow", now=now)
        with self.assertRaises(ValueError):
            parse_schedule_time("2026-11-20T18:00:00+03:00", "Europe/Moscow", now=now)

    def test_rejects_dst_gap_and_fold(self):
        from scheduling import parse_schedule_time
        # 2026-03-29 02:30 не существует в Europe/Berlin (gap).
        now = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            parse_schedule_time("2026-03-29T02:30:00", "Europe/Berlin", now=now)
        # 2026-10-25 02:30 встречается дважды в Europe/Berlin (fold).
        now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            parse_schedule_time("2026-10-25T02:30:00", "Europe/Berlin", now=now)

    def test_rejects_weekday_without_time(self):
        from scheduling import parse_schedule_time
        now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            parse_schedule_time("в пятницу", "Europe/Moscow", now=now)
        with self.assertRaises(ValueError):
            parse_schedule_time("2026-10-19", "Europe/Moscow", now=now)


class SchedulingReviewTests(unittest.TestCase):
    def _make_post(self, root):
        from core import Store
        store = Store(root)
        return store, store.create("План", [{"type": "paragraph", "text": "Текст"}], {})

    def test_review_rejects_without_experimental(self):
        from core import Store
        from scheduling import Scheduling
        with tempfile.TemporaryDirectory() as directory:
            store, post = self._make_post(directory)
            manager = Scheduling(store)
            manager.configure(channel="@fictional", dm_chat_id=-200123,
                              topic_id=7, timezone_name="Europe/Moscow")
            with self.assertRaises(ValueError):
                manager.review(post["id"], "завтра в 18:00")
            store.conn.close()


class SchedulingGuardTests(unittest.TestCase):
    def test_delivery_review_blocked_by_active_schedule(self):
        import delivery
        from test_delivery import FakeAPI
        with tempfile.TemporaryDirectory() as directory:
            manager = _manager(directory)
            post = manager.store.create("План", [{"type": "paragraph", "text": "Текст"}], {})
            manager.review(post["id"], WHEN, experimental=True, now=NOW)
            courier = delivery.Delivery(manager.store, FakeAPI())
            with self.assertRaises(delivery.DeliveryError):
                courier.review(post["id"])
            manager.store.conn.close()

class SchedulingExecuteTests(unittest.TestCase):
    def test_success_sends_dm_topic_and_strict_true(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = _manager(directory)
            post = manager.store.create("План", [{"type": "paragraph", "text": "Текст"}], {})
            operation = manager.review(post["id"], WHEN, experimental=True, now=NOW)
            self.assertEqual(operation["status"], "reviewed")
            self.assertIn("fingerprint", operation)
            receipt = manager.execute(operation["id"], operation["fingerprint"],
                                      experimental=True, now=NOW)
            self.assertEqual(receipt["proposal_message_id"], 55)
            self.assertTrue(list((Path(directory) / "data" / "schedule-receipts").glob("*.json")))
            schedule_file = (Path(directory) / "data" / "schedule-receipts" /
                             (operation["id"] + ".schedule.json"))
            proposal_file = (Path(directory) / "data" / "schedule-receipts" /
                             (operation["id"] + ".proposal.json"))
            self.assertTrue(schedule_file.is_file())
            self.assertTrue(proposal_file.is_file())
            self.assertEqual(manager.conn.execute(
                "SELECT COUNT(*) FROM schedule_receipts WHERE operation_id=?",
                (operation["id"],)).fetchone()[0], 2)
            methods = [call[0] for call in manager.api.calls]
            self.assertIn("sendRichMessage", methods)
            self.assertIn("approveSuggestedPost", methods)
            send = next(call[1] for call in manager.api.calls if call[0] == "sendRichMessage")
            self.assertEqual(send["chat_id"], -200555)
            self.assertEqual(send["direct_messages_topic_id"], 7)
            self.assertIn("suggested_post_parameters", send)
            self.assertNotIn("price", str(send))
            approve = next(call[1] for call in manager.api.calls if call[0] == "approveSuggestedPost")
            self.assertEqual(approve, {"chat_id": -200555, "message_id": 55,
                                       "send_date": operation["snapshot"]["send_date"]})
            self.assertEqual(approve["chat_id"], -200555)
            self.assertEqual(approve["message_id"], receipt["proposal_message_id"])
            self.assertEqual(approve["send_date"], operation["snapshot"]["send_date"])
            self.assertNotEqual(approve["chat_id"], approve["message_id"])
            done = manager.operation(operation["id"])
            self.assertEqual(done["status"], "scheduled")
            listed = manager.list()[0]
            self.assertEqual(listed["proposal_message_id"], 55)
            self.assertFalse(listed["live_verified"])
            self.assertFalse(listed["experimental"] is False)
            self.assertNotIn("post_id", listed)
            self.assertEqual(
                set(listed),
                {"id", "status", "version", "channel", "channel_title",
                 "send_at", "send_date", "timezone", "proposal_message_id",
                 "published_message_id", "source", "evidence", "last_error",
                 "experimental", "live_verified"})
            manager.store.conn.close()

    def test_recover_moves_interrupted_sending_to_unknown_and_restores_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = _manager(directory)
            post = manager.store.create("План", [{"type": "paragraph", "text": "Текст"}], {})
            operation = manager.review(post["id"], WHEN, experimental=True, now=NOW)
            manager.conn.execute("UPDATE schedule_operations SET status='sending' WHERE id=?",
                                 (operation["id"],))
            manager.conn.commit()
            recovered = manager.recover()
            self.assertEqual(recovered[0]["status"], "unknown")
            self.assertEqual(manager.operation(operation["id"])["status"], "unknown")
            with self.assertRaises(ValueError):
                manager.execute(operation["id"], operation["fingerprint"],
                                experimental=True, now=NOW)
            manager.store.conn.close()

    def test_recover_restores_receipt_after_crash_before_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = _manager(directory)
            post = manager.store.create("План", [{"type": "paragraph", "text": "Текст"}], {})
            operation = manager.review(post["id"], WHEN, experimental=True, now=NOW)
            receipt = manager.execute(operation["id"], operation["fingerprint"],
                                      experimental=True, now=NOW)
            self.assertEqual(receipt["proposal_message_id"], 55)
            manager.conn.execute("UPDATE schedule_operations SET status='approving' WHERE id=?",
                                 (operation["id"],))
            manager.conn.commit()
            recovered = manager.recover()
            self.assertEqual(recovered[0]["status"], "scheduled")
            calls_before = len(manager.api.calls)
            manager.recover()
            self.assertEqual(len(manager.api.calls), calls_before)
            manager.store.conn.close()

    def test_reconcile_verdicts_keep_receipt_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = _manager(directory)
            post = manager.store.create("План", [{"type": "paragraph", "text": "Текст"}], {})
            operation = manager.review(post["id"], WHEN, experimental=True, now=NOW)
            with self.assertRaises(ValueError):
                manager.reconcile(operation["id"], "bogus", "проверено человеком")
            with self.assertRaises(ValueError):
                manager.reconcile(operation["id"], "published", "коротко", message_id=7)
            done = manager.execute(operation["id"], operation["fingerprint"],
                                   experimental=True, now=NOW)
            self.assertEqual(done["proposal_message_id"], 55)
            with self.assertRaises(ValueError):
                manager.reconcile(operation["id"], "absent", "проверено человеком явно")
            published = manager.reconcile(operation["id"], "published",
                                          "проверено человеком в клиенте",
                                          message_id=777)
            self.assertEqual(published["status"], "published")
            again = manager.reconcile(operation["id"], "published",
                                      "проверено человеком в клиенте",
                                      message_id=777)
            self.assertEqual(again["status"], "published")
            with self.assertRaises(ValueError):
                manager.reconcile(operation["id"], "published",
                                  "проверено человеком в клиенте",
                                  message_id=778)
            manager.store.conn.close()

    def test_timeout_becomes_unknown_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = _manager(directory, FakeScheduleAPI(mode="timeout"))
            post = manager.store.create("План", [{"type": "paragraph", "text": "Текст"}], {})
            operation = manager.review(post["id"], WHEN, experimental=True, now=NOW)
            with self.assertRaises(ValueError):
                manager.execute(operation["id"], operation["fingerprint"], experimental=True, now=NOW)
            self.assertEqual(manager.operation(operation["id"])["status"], "unknown")
            with self.assertRaises(ValueError):
                manager.execute(operation["id"], operation["fingerprint"], experimental=True, now=NOW)
            recovered = manager.recover()
            self.assertEqual(recovered[0]["status"], "unknown")
            manager.store.conn.close()

    def test_concurrent_execute_has_single_winner_and_single_send(self):
        import threading
        from core import Store
        from scheduling import Scheduling
        with tempfile.TemporaryDirectory() as directory:
            api = FakeScheduleAPI()
            first = Scheduling(Store(directory), api)
            first.configure(channel=-100555, dm_chat_id=-200555, topic_id=7,
                            timezone_name="Europe/Moscow")
            post = first.store.create("План", [{"type": "paragraph", "text": "Текст"}], {})
            operation = first.review(post["id"], WHEN, experimental=True, now=NOW)
            first.store.conn.close()
            barrier = threading.Barrier(2)
            outcomes, errors = [], []

            def attempt():
                from scheduling import Scheduling as Manager
                from core import Store as LocalStore
                manager = Manager(LocalStore(directory), api)
                try:
                    barrier.wait(timeout=5)
                    manager.execute(operation["id"], operation["fingerprint"],
                                    experimental=True, now=NOW)
                    outcomes.append("scheduled")
                except Exception as error:  # noqa: BLE001 - фиксируем итог гонки
                    errors.append(error)
                finally:
                    manager.store.conn.close()

            workers = [threading.Thread(target=attempt) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(len(outcomes), 1, (outcomes, [str(error) for error in errors]))
            self.assertEqual(len(errors), 1)
            sends = [call for call in api.calls if call[0] == "sendRichMessage"]
            self.assertEqual(len(sends), 1)
            checker = Scheduling(Store(directory), FakeScheduleAPI())
            try:
                self.assertEqual(checker.operation(operation["id"])["status"], "scheduled")
            finally:
                checker.store.conn.close()

    def test_media_payload_keeps_order_and_rejects_paid_broadcast(self):
        import base64
        from core import create_demo, import_video
        with tempfile.TemporaryDirectory() as directory:
            api = FakeScheduleAPI()
            manager = _manager(directory, api)
            fixture = base64.b64decode((Path(__file__).parent / "fixtures" /
                                        "avc-96x160.mp4.base64").read_text(encoding="ascii"))
            source = Path(directory) / "synthetic-avc.mp4"
            source.write_bytes(fixture)
            video = dict(import_video(manager.store.root, source), type="video",
                         alt="Синтетическое немое видео")
            image = next(block for block in create_demo(manager.store.root)["blocks"]
                         if block["type"] == "image")
            blocks = [{"type": "quote", "text": "Вымышленная цитата"},
                      {"type": "media_group", "layout": "collage",
                       "items": [dict(video), dict(image, type="image")]},
                      {"type": "paragraph",
                       "text": ["Unicode: 👩🏽‍💻 ✨ ❤️ ",
                                {"type": "custom_emoji", "text": "⭐",
                                 "custom_emoji_id": "1234567890123456789"}]}]
            post = manager.store.create("План", blocks, {})
            operation = manager.review(post["id"], WHEN, experimental=True, now=NOW)
            snapshot = operation["snapshot"]
            self.assertTrue(snapshot["markdown"].startswith("> Вымышленная цитата"))
            self.assertIn("<tg-collage>", snapshot["markdown"])
            self.assertEqual([item["media"]["type"] for item in snapshot["media"]],
                             ["video", "photo"])
            manager.execute(operation["id"], operation["fingerprint"], experimental=True, now=NOW)
            send = next(call for call in api.calls if call[0] == "sendRichMessage")
            self.assertNotIn("price", str(send))
            self.assertNotIn("\"paid\"", str(send))
            self.assertIn("tg://emoji?id=1234567890123456789",
                          send[1]["rich_message"]["markdown"])
            self.assertIn("<tg-collage>", send[1]["rich_message"]["markdown"])
            self.assertEqual([item["media"]["type"] for item in send[1]["rich_message"]["media"]],
                             [item["media"]["type"] for item in snapshot["media"]])
            manager.store.conn.close()

    def test_crash_between_receipt_and_projection_recovers_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = _manager(directory)
            post = manager.store.create("План", [{"type": "paragraph", "text": "Текст"}], {})
            operation = manager.review(post["id"], WHEN, experimental=True, now=NOW)
            done = manager.execute(operation["id"], operation["fingerprint"],
                                   experimental=True, now=NOW)
            manager.conn.execute("UPDATE schedule_operations SET status='sending',result=NULL WHERE id=?",
                                 (operation["id"],))
            manager.conn.commit()
            calls_before = len(manager.api.calls)
            recovered = manager.recover()
            by_id = {item["id"]: item for item in recovered}
            self.assertEqual(by_id[operation["id"]]["status"], "scheduled")
            self.assertEqual(len(manager.api.calls), calls_before)
            self.assertEqual(manager.operation(operation["id"])["status"], "scheduled")
            self.assertEqual(done["proposal_message_id"], 55)
            manager.store.conn.close()

    def test_missing_delivery_lock_is_explicit_error(self):
        import importlib
        import importlib.abc
        import sys
        with tempfile.TemporaryDirectory() as directory:
            manager = _manager(directory)
            post = manager.store.create("План", [{"type": "paragraph", "text": "Текст"}], {})
            import scheduling

            class BlockDelivery(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name == "delivery":
                        raise ImportError("synthetic missing delivery lock")
                    return None

            hook = BlockDelivery()
            blocked = sys.modules.pop("delivery", None)
            scheduling_snapshot = sys.modules.pop("scheduling", None)
            sys.meta_path.insert(0, hook)
            try:
                reloaded = importlib.import_module("scheduling")
                with self.assertRaises(ValueError) as caught:
                    reloaded._schedule_lock(manager.root)
                self.assertIn("lock", str(caught.exception).lower())
                self.assertFalse(manager.api.calls)
            finally:
                sys.meta_path.remove(hook)
                sys.modules.pop("scheduling", None)
                if scheduling_snapshot is not None:
                    sys.modules["scheduling"] = scheduling_snapshot
                if blocked is not None:
                    sys.modules["delivery"] = blocked
            manager.store.conn.close()

    def test_stale_version_requires_new_review(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = _manager(directory)
            post = manager.store.create("План", [{"type": "paragraph", "text": "Текст"}], {})
            operation = manager.review(post["id"], WHEN, experimental=True, now=NOW)
            manager.store.update(post["id"], blocks=[{"type": "paragraph", "text": "Другой"}])
            with self.assertRaises(ValueError):
                manager.execute(operation["id"], operation["fingerprint"], experimental=True, now=NOW)
            manager.store.conn.close()

    def test_second_review_blocked_and_unsupported_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = _manager(directory)
            post = manager.store.create("План", [{"type": "paragraph", "text": "Текст"}], {})
            manager.review(post["id"], WHEN, experimental=True, now=NOW)
            with self.assertRaises(ValueError):
                manager.review(post["id"], WHEN, experimental=True, now=NOW)
            with self.assertRaises(ValueError):
                manager.cancel()
            with self.assertRaises(ValueError):
                manager.reschedule()
            self.assertFalse([c for c in manager.api.calls if c[0] in ("cancel", "reschedule")])
            manager.store.conn.close()



class SchedulingSettingsTests(unittest.TestCase):
    def test_configure_and_read_settings_channel(self):
        from core import Store
        from scheduling import Scheduling
        with tempfile.TemporaryDirectory() as directory:
            store = Scheduling(Store(directory))
            saved = store.configure(channel="@fictional", dm_chat_id=-200123,
                                    topic_id=7, timezone_name="Europe/Moscow")
            self.assertEqual(saved["channel"], "@fictional")
            self.assertEqual(saved["topic_id"], 7)
            fetched = store.get_settings()
            self.assertEqual(fetched, saved)
            self.assertNotIn("token", fetched)

    def test_rejects_bad_zone_and_topic_without_dm(self):
        from core import Store
        from scheduling import Scheduling
        with tempfile.TemporaryDirectory() as directory:
            store = Scheduling(Store(directory))
            with self.assertRaises((ValueError, Exception)):
                store.configure(channel="@fictional", timezone_name="Mars/Olympus")
            with self.assertRaises(ValueError):
                store.configure(channel="@fictional", topic_id=7, timezone_name="Europe/Moscow")
            with self.assertRaises(ValueError):
                store.configure(timezone_name="Europe/Moscow")
            store.store.conn.close()
            store.store.conn.close()
