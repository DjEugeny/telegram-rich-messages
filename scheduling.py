"""Устойчивый experimental-адаптер расписания.

Только стандартная библиотека. Без сети и секретов: разбор времени и
настройки выполняются локально. Live-проверка самоодобрения не пройдена,
итог — experimental / live-required.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import uuid
from pathlib import Path

MIN_DELAY_SECONDS = 300
MAX_DELAY_SECONDS = 2678400

ACTIVE_STATUSES = ("reviewed", "sending", "proposed", "approving", "scheduled", "unknown")
TERMINAL_OK = ("scheduled", "published")


class SchedulingError(ValueError):
    """Безопасная для показа человеку ошибка расписания без данных transport."""


def _schedule_lock(root):
    """Общая delivery.lock; недоступность — явная ошибка без silent fallback."""
    try:
        from delivery import execution_lock
    except Exception:
        raise SchedulingError("Межпроцессный lock недоступен; расписание заблокировано.") from None
    return execution_lock(root)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat()


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".schedule-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Scheduling:
    """Настройки и время расписания; сеть и секреты не затрагиваются."""

    def __init__(self, store, api=None):
        self.store = store
        self.conn = store.conn
        self.root = store.root
        self.api = api
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schedule_settings ("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schedule_operations ("
            " id TEXT PRIMARY KEY, post_id TEXT NOT NULL, status TEXT NOT NULL,"
            " snapshot TEXT NOT NULL, fingerprint TEXT NOT NULL,"
            " created_at TEXT NOT NULL, result TEXT, error TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schedule_events ("
            " seq INTEGER PRIMARY KEY, operation_id TEXT NOT NULL,"
            " status TEXT NOT NULL, created_at TEXT NOT NULL, detail TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schedule_receipts ("
            " operation_id TEXT NOT NULL, kind TEXT NOT NULL, receipt TEXT NOT NULL,"
            " PRIMARY KEY (operation_id, kind))"
        )
        try:
            columns = [row[1] for row in self.conn.execute(
                "PRAGMA table_info(schedule_receipts)").fetchall()]
            if columns and columns[:2] != ["operation_id", "kind"]:
                pass
            else:
                sql = (self.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='schedule_receipts'"
                ).fetchone() or (None,))[0] or ""
                if "PRIMARY KEY (operation_id, kind)" not in sql.replace(
                        " ", "").replace("\n", "") and "operation_idTEXTPRIMARYKEY" in sql.replace(
                            " ", "").replace("\n", ""):
                    self.conn.execute(
                        "CREATE TABLE IF NOT EXISTS schedule_receipts_new ("
                        " operation_id TEXT NOT NULL, kind TEXT NOT NULL,"
                        " receipt TEXT NOT NULL,"
                        " PRIMARY KEY (operation_id, kind))")
                    self.conn.execute(
                        "INSERT OR IGNORE INTO schedule_receipts_new"
                        "(operation_id, kind, receipt)"
                        " SELECT operation_id, kind, receipt FROM schedule_receipts")
                    self.conn.execute("DROP TABLE schedule_receipts")
                    self.conn.execute(
                        "ALTER TABLE schedule_receipts_new RENAME TO schedule_receipts")
        except Exception:
            pass
        self.conn.commit()

    def _write_setting(self, key, raw):
        self.conn.execute(
            "INSERT INTO schedule_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(raw, ensure_ascii=False)),
        )

    def configure(self, channel=None, dm_chat_id=None, topic_id=None, timezone_name=None):
        if timezone_name is None:
            raise ValueError("Нужна явная IANA-зона, например Europe/Moscow")
        zone = _load_zone(timezone_name)
        if channel is None and dm_chat_id is None:
            raise ValueError("Нужно назначение: channel или dm/topic")
        cleaned = {"timezone": zone.key, "channel": None, "dm_chat_id": None, "topic_id": None}
        if channel is not None:
            if isinstance(channel, str) and re.fullmatch(r"@[A-Za-z0-9_]{5,32}", channel):
                cleaned["channel"] = channel
            elif isinstance(channel, int) and channel != 0:
                cleaned["channel"] = channel
            else:
                raise ValueError("channel: точный @username или числовой ID")
        if dm_chat_id is not None:
            if type(dm_chat_id) is not int:
                raise ValueError("dm_chat_id должен быть числом")
            cleaned["dm_chat_id"] = dm_chat_id
        if topic_id is not None:
            if type(topic_id) is not int or topic_id < 1:
                raise ValueError("topic_id: положительный числовой ID")
            cleaned["topic_id"] = topic_id
        if cleaned["topic_id"] is not None and cleaned["dm_chat_id"] is None:
            raise ValueError("topic_id требует dm_chat_id")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for key, raw in cleaned.items():
                self._write_setting(key, raw)
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return self.get_settings()

    def get_settings(self):
        rows = {row[0]: json.loads(row[1]) for row in self.conn.execute("SELECT key, value FROM schedule_settings")}
        if not rows:
            raise KeyError("Настройки расписания не заданы")
        return {
            "timezone": rows.get("timezone"),
            "channel": rows.get("channel"),
            "dm_chat_id": rows.get("dm_chat_id"),
            "topic_id": rows.get("topic_id"),
        }
    # ---- review/execute (шаг 4) ----

    def _call(self, method, payload=None, files=None):
        if self.api is None:
            raise SchedulingError("Сетевой transport не задан.")
        try:
            return self.api.call(method, payload, files)
        except SchedulingError:
            raise
        except Exception:
            raise SchedulingError("Нет подтверждённого ответа Telegram.") from None

    def _event(self, operation_id, status, detail=""):
        self.conn.execute(
            "INSERT INTO schedule_events(operation_id,status,created_at,detail) VALUES (?,?,?,?)",
            (operation_id, status, _now(), detail),
        )

    def operation(self, operation_id):
        row = self.conn.execute("SELECT * FROM schedule_operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise SchedulingError("Операция расписания не найдена.")
        result = dict(row)
        result["snapshot"] = json.loads(result["snapshot"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def _receipt_path(self, operation_id, kind):
        return self.root / "data" / "schedule-receipts" / (str(uuid.UUID(operation_id)) + "." + kind + ".json")

    def _schedule_blocked(self, post_id, exclude_id=None):
        query = "SELECT id FROM schedule_operations WHERE post_id=? AND status IN ({})".format(
            ",".join("?" for _ in ACTIVE_STATUSES))
        params = [post_id, *ACTIVE_STATUSES]
        if exclude_id is not None:
            query += " AND id!=?"
            params.append(exclude_id)
        return self.conn.execute(query, params).fetchone()

    def _delivery_blocked(self, post_id):
        try:
            return self.conn.execute(
                "SELECT id FROM operations WHERE post_id=? AND status IN ('reviewed','sending','unknown') LIMIT 1",
                (post_id,)).fetchone()
        except Exception:
            return None

    def _store_receipt(self, operation_id, kind, receipt):
        try:
            _atomic_json(self._receipt_path(operation_id, kind), receipt)
            file_ok = True
        except OSError:
            file_ok = False
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO schedule_receipts VALUES (?,?,?)",
                              (operation_id, kind, _json(receipt)))
        return file_ok

    def _known_receipt(self, operation_id, kind):
        path = self._receipt_path(operation_id, kind)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        row = self.conn.execute("SELECT receipt FROM schedule_receipts WHERE operation_id=? AND kind=?",
                                (operation_id, kind)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def _mark(self, operation, status, error):
        with self.conn:
            self.conn.execute("UPDATE schedule_operations SET status=?,error=? WHERE id=?",
                              (status, error, operation["id"]))
            self._event(operation["id"], status, error or "")


    def _media_files(self, compiled):
        from core import MAX_IMAGE_BYTES
        from media import MAX_VIDEO_BYTES
        import hashlib as _hashlib
        files = {}
        for item in compiled["media"]:
            original = self.root / item["path"]
            target = original.resolve()
            base = (self.root / "media").resolve()
            if target != base and base not in target.parents:
                raise SchedulingError("Вложение вне media/.")
            cursor = original
            while cursor != self.root:
                if cursor.is_symlink():
                    raise SchedulingError("Символические ссылки запрещены.")
                cursor = cursor.parent
            try:
                limit = MAX_VIDEO_BYTES if item["mime"] == "video/mp4" else MAX_IMAGE_BYTES
                with target.open("rb") as stream:
                    data = stream.read(limit + 1)
            except OSError:
                raise SchedulingError("Вложение недоступно.") from None
            if len(data) > limit or _hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise SchedulingError("Вложение изменилось: SHA256 не совпадает.")
            files[item["attachment"]] = (target.name, item["mime"], data)
        return files

    def _preflight(self, dm_chat_id, topic_id, media_types):
        bot = self._call("getMe")
        target = self._call("getChat", {"chat_id": dm_chat_id})
        if not bot.get("is_bot") or type(bot.get("id")) is not int:
            raise SchedulingError("Не удалось проверить бота.")
        if type(target.get("id")) is not int or int(target["id"]) != int(dm_chat_id):
            raise SchedulingError("Telegram вернул другой DM chat_id.")
        if target.get("is_direct_messages") is not True:
            raise SchedulingError("Чат не является direct messages чатом.")
        parent = target.get("parent_chat") or {}
        parent_id = parent.get("id") if isinstance(parent, dict) else None
        member = self._call("getChatMember", {"chat_id": target["id"], "user_id": bot["id"]})
        checked = member
        if parent_id is not None:
            checked = self._call("getChatMember", {"chat_id": parent_id, "user_id": bot["id"]})
        status = checked.get("status")
        if status != "creator" and (status != "administrator"
                or not checked.get("can_post_messages")
                or not checked.get("can_manage_direct_messages")):
            raise SchedulingError("Нет прав can_post_messages/can_manage_direct_messages.")
        return {"bot_id": bot["id"], "bot_username": bot.get("username", ""),
                "dm_chat_id": target["id"], "parent_chat_id": parent_id, "topic_id": topic_id,
                "media_types": sorted(media_types),
                "topic_check": "Права темы подтвердит только отправка."}


    def review(self, post_id, when, experimental=False, now=None):
        from core import compile_post, validate_media
        if experimental is not True:
            raise SchedulingError("Нужен явный experimental opt-in.")
        with _schedule_lock(self.root):
            try:
                settings = self.get_settings()
            except KeyError:
                raise SchedulingError("Настройки расписания не заданы.") from None
            if settings.get("dm_chat_id") is None or settings.get("topic_id") is None:
                raise SchedulingError("Нужны явные dm_chat_id и topic_id.")
            try:
                moment = parse_schedule_time(when, settings.get("timezone"), now=now)
            except ValueError as error:
                raise SchedulingError(str(error)) from None
            post = self.store.get(post_id)
            if self._schedule_blocked(post_id):
                raise SchedulingError("Активное расписание уже есть.")
            if self._delivery_blocked(post_id):
                raise SchedulingError("Активна доставка. Сначала recover/resolve.")
            try:
                compiled = compile_post(post)
            except ValueError:
                raise SchedulingError("Пост не компилируется.") from None
            try:
                validate_media(self.root, post)
            except ValueError:
                raise SchedulingError("Медиа повреждено; нужен повторный импорт.") from None
            media_types = {item["media"]["type"] for item in compiled["media"]}
            if media_types - {"photo", "video"}:
                raise SchedulingError("Неизвестный тип медиа.")
            preflight = self._preflight(settings["dm_chat_id"], settings["topic_id"], media_types)
            channel = settings.get("channel")
            if preflight["parent_chat_id"] is not None and isinstance(channel, int):
                if int(channel) != int(preflight["parent_chat_id"]):
                    raise SchedulingError("DM-родитель не совпадает с каналом.")
            snapshot = {"post_id": post_id, "version": post["version"],
                        "version_hash": post["fingerprint"], "channel": channel,
                        "settings_source": dict(settings), "bot_id": preflight["bot_id"],
                        "bot_username": preflight["bot_username"], "dm_chat_id": preflight["dm_chat_id"],
                        "topic_id": preflight["topic_id"], "parent_chat_id": preflight["parent_chat_id"],
                        "send_date": moment["send_date"], "iso": moment["iso"],
                        "timezone": moment["timezone"], "publication_anchor": _digest(post.get("publication")),
                        "markdown": compiled["markdown"], "media": compiled["media"],
                        "experimental": True, "live_verified": False}
            operation_id = str(uuid.uuid4())
            fingerprint = _digest({"operation_id": operation_id, "snapshot": snapshot,
                                   "nonce": secrets.token_hex(24)})
            with self.conn:
                self.conn.execute("INSERT INTO schedule_operations VALUES (?,?,?,?,?,?,NULL,NULL)",
                                  (operation_id, post_id, "reviewed", _json(snapshot), fingerprint, _now()))
                self._event(operation_id, "reviewed")
            return self.operation(operation_id)
    def execute(self, operation_id, fingerprint, experimental=False, now=None):
        from core import compile_post, validate_media
        if experimental is not True:
            raise SchedulingError("Нужен явный experimental opt-in.")
        with _schedule_lock(self.root):
            operation = self.operation(operation_id)
            if not isinstance(fingerprint, str) or not hmac.compare_digest(fingerprint, operation["fingerprint"]):
                raise SchedulingError("Подтверждение не соответствует операции.")
            if operation["status"] in ("scheduled", "published"):
                known = self._known_receipt(operation_id, "schedule")
                return known if known is not None else operation["result"]
            if operation["status"] != "reviewed":
                raise SchedulingError("Повтор заблокирован. Используйте recover.")
            if self._schedule_blocked(operation["post_id"], exclude_id=operation_id):
                raise SchedulingError("Повтор заблокирован: есть активное расписание.")
            if self._delivery_blocked(operation["post_id"]):
                raise SchedulingError("Активна доставка. Сначала recover/resolve.")
            snapshot = operation["snapshot"]
            if snapshot.get("experimental") is not True:
                raise SchedulingError("Snapshot без opt-in недействителен.")
            post = self.store.get(snapshot["post_id"])
            if post["version"] != snapshot["version"] or post["fingerprint"] != snapshot["version_hash"]:
                raise SchedulingError("Версия изменилась; нужен новый review.")
            if _digest(post.get("publication")) != snapshot.get("publication_anchor"):
                raise SchedulingError("Публикация изменилась; нужен новый review.")
            try:
                settings = self.get_settings()
            except KeyError:
                raise SchedulingError("Настройки изменились; нужен новый review.") from None
            if (settings.get("dm_chat_id") != snapshot["dm_chat_id"]
                    or settings.get("topic_id") != snapshot["topic_id"]
                    or settings.get("channel") != snapshot["channel"]
                    or settings.get("timezone") != snapshot["timezone"]):
                raise SchedulingError("Настройки изменились; нужен новый review.")
            try:
                current = parse_schedule_time(snapshot["iso"], snapshot["timezone"], now=_coerce_now(now))
            except ValueError as error:
                raise SchedulingError(str(error)) from None
            if current["send_date"] != snapshot["send_date"]:
                raise SchedulingError("Дата изменилась; нужен новый review.")
            try:
                compiled = compile_post(post)
                validate_media(self.root, post)
            except ValueError:
                raise SchedulingError("Медиа изменилось; нужен новый review.") from None
            if compiled["markdown"] != snapshot["markdown"] or compiled["media"] != snapshot["media"]:
                raise SchedulingError("Содержимое изменилось; нужен новый review.")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                changed = self.conn.execute(
                    "UPDATE schedule_operations SET status='sending' WHERE id=? AND status='reviewed'",
                    (operation_id,)).rowcount
                if changed != 1:
                    raise SchedulingError("Операция уже занята.")
                self._event(operation_id, "sending")
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            media_types = {item["media"]["type"] for item in snapshot["media"]}
            try:
                preflight = self._preflight(snapshot["dm_chat_id"], snapshot["topic_id"], media_types)
            except SchedulingError as error:
                self._mark(operation, "error", str(error))
                raise
            except BaseException:
                self._mark(operation, "unknown", "Ответ не подтверждён. Повтор запрещён.")
                raise SchedulingError("Результат неизвестен; выполните recover.") from None
            if preflight["bot_id"] != snapshot["bot_id"]:
                self._mark(operation, "error", "Бот изменился; нужен новый review.")
                raise SchedulingError("Бот изменился; нужен новый review.")
            files = self._media_files({"media": snapshot["media"]})
            payload = {"chat_id": snapshot["dm_chat_id"],
                       "direct_messages_topic_id": snapshot["topic_id"],
                       "rich_message": {"markdown": snapshot["markdown"]},
                       "suggested_post_parameters": {"send_date": snapshot["send_date"]}}
            if snapshot["media"]:
                payload["rich_message"]["media"] = [
                    {"id": item["id"], "media": item["media"]} for item in snapshot["media"]]
            try:
                proposal = self._call("sendRichMessage", payload, files)
            except BaseException:
                self._mark(operation, "unknown", "sendRichMessage без подтверждения.")
                raise SchedulingError("Результат неизвестен; выполните recover.") from None
            if (not isinstance(proposal, dict) or type(proposal.get("message_id")) is not int
                    or proposal["message_id"] < 1
                    or not isinstance(proposal.get("chat"), dict)
                    or proposal["chat"].get("id") != snapshot["dm_chat_id"]
                    or not isinstance(proposal.get("suggested_post_info"), dict)
                    or proposal["suggested_post_info"].get("state") != "pending"):
                self._mark(operation, "unknown", "Некорректное подтверждение.")
                raise SchedulingError("Результат неизвестен: некорректное подтверждение.")
            proposal_receipt = {"operation_id": operation_id, "kind": "proposal",
                                "snapshot_hash": _digest(snapshot),
                                "proposal_message_id": proposal["message_id"],
                                "dm_chat_id": snapshot["dm_chat_id"], "topic_id": snapshot["topic_id"],
                                "send_date": snapshot["send_date"],
                                "suggested_post_info": proposal["suggested_post_info"],
                                "created_at": _now(), "experimental": True, "live_verified": False}
            file_ok = self._store_receipt(operation_id, "proposal", proposal_receipt)
            with self.conn:
                self.conn.execute("UPDATE schedule_operations SET status='proposed',error=NULL WHERE id=?",
                                  (operation_id,))
                self._event(operation_id, "proposed")
            if not file_ok:
                self._mark(operation, "unknown", "Proposal receipt в SQLite, нужен recover.")
                raise SchedulingError("Предложение принято; нужен recover. Не повторяйте отправку.")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                changed = self.conn.execute(
                    "UPDATE schedule_operations SET status='approving' WHERE id=? AND status='proposed'",
                    (operation_id,)).rowcount
                if changed != 1:
                    raise SchedulingError("Операция уже занята.")
                self._event(operation_id, "approving")
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            try:
                approved = self._call("approveSuggestedPost",
                                      {"chat_id": snapshot["dm_chat_id"],
                                       "message_id": proposal["message_id"],
                                       "send_date": snapshot["send_date"]})
            except BaseException:
                self._mark(operation, "unknown", "approve без подтверждения. Повтор запрещён.")
                raise SchedulingError("Результат неизвестен; выполните recover.") from None
            if approved is not True:
                self._mark(operation, "unknown", "Approve не вернул строго True.")
                raise SchedulingError("Результат неизвестен: approve не вернул True.")
            schedule_receipt = {"operation_id": operation_id, "kind": "schedule",
                                "snapshot_hash": _digest(snapshot),
                                "proposal_message_id": proposal["message_id"],
                                "dm_chat_id": snapshot["dm_chat_id"], "topic_id": snapshot["topic_id"],
                                "send_date": snapshot["send_date"], "iso": snapshot["iso"],
                                "timezone": snapshot["timezone"], "scheduled_at": _now(),
                                "experimental": True, "live_verified": False}
            file_ok = self._store_receipt(operation_id, "schedule", schedule_receipt)
            if not file_ok:
                with self.conn:
                    self.conn.execute("UPDATE schedule_operations SET status=?,result=?,error=? WHERE id=?",
                                      ("unknown", _json(schedule_receipt),
                                       "Schedule receipt в SQLite, нужен recover.", operation_id))
                    self._event(operation_id, "unknown", "Schedule receipt в SQLite.")
                raise SchedulingError("Расписание подтверждено; нужен recover. Не повторяйте отправку.")
            with self.conn:
                self.conn.execute("UPDATE schedule_operations SET status='scheduled',result=?,error=NULL WHERE id=?",
                                  (_json(schedule_receipt), operation_id))
                self._event(operation_id, "scheduled")
            return schedule_receipt
    def recover(self):
        with _schedule_lock(self.root):
            recovered = []
            rows = self.conn.execute(
                "SELECT id FROM schedule_operations WHERE status IN ('sending','approving','unknown')").fetchall()
            for row in rows:
                operation = self.operation(row["id"])
                digest = _digest(operation["snapshot"])
                schedule = self._known_receipt(operation["id"], "schedule")
                proposal = self._known_receipt(operation["id"], "proposal")
                if operation["status"] in ("sending", "approving"):
                    expected = None
                    if schedule is not None and schedule.get("snapshot_hash") == digest:
                        expected = "scheduled"
                    elif proposal is not None and proposal.get("snapshot_hash") == digest:
                        expected = "proposed"
                    if expected is None:
                        with self.conn:
                            self.conn.execute(
                                "UPDATE schedule_operations SET status='unknown',error=? WHERE id=?",
                                ("Перебито без receipt; результат неизвестен.",
                                 operation["id"]))
                            self._event(operation["id"], "unknown",
                                        "Перебито без receipt.")
                    elif operation["status"] != expected:
                        with self.conn:
                            self.conn.execute(
                                "UPDATE schedule_operations SET status=?,result=?,error=NULL WHERE id=?",
                                (expected, _json(schedule if expected == "scheduled" else proposal),
                                 operation["id"]))
                            self._event(operation["id"], expected)
                elif schedule is not None and schedule.get("snapshot_hash") == digest:
                    with self.conn:
                        self.conn.execute(
                            "UPDATE schedule_operations SET status='scheduled',result=?,error=NULL WHERE id=?",
                            (_json(schedule), operation["id"]))
                        self._event(operation["id"], "scheduled")
                elif proposal is not None and proposal.get("snapshot_hash") == digest:
                    with self.conn:
                        self.conn.execute(
                            "UPDATE schedule_operations SET status='proposed',error=? WHERE id=?",
                            ("Известное предложение без расписания.", operation["id"]))
                        self._event(operation["id"], "proposed")
                else:
                    self._mark(operation, "unknown", "Без устойчивого receipt. Повтор запрещён.")
                recovered.append(self.operation(operation["id"]))
            return recovered

    def list(self):
        rows = self.conn.execute("SELECT id FROM schedule_operations ORDER BY created_at DESC,id").fetchall()
        items = []
        for row in rows:
            operation = self.operation(row["id"])
            snapshot = operation["snapshot"]
            schedule = self._known_receipt(operation["id"], "schedule")
            published = operation["result"] if operation["status"] == "published" else None
            published_id = published.get("message_id") if isinstance(published, dict) else None
            evidence = published.get("evidence") if isinstance(published, dict) else None
            items.append({"id": operation["id"], "status": operation["status"],
                          "version": snapshot.get("version"),
                          "channel": snapshot.get("channel"), "channel_title": snapshot.get("channel"),
                          "send_at": snapshot.get("iso"), "send_date": snapshot.get("send_date"),
                          "timezone": snapshot.get("timezone"),
                          "proposal_message_id": (schedule or {}).get("proposal_message_id"),
                          "published_message_id": published_id, "source": "schedule-review",
                          "evidence": evidence, "last_error": operation.get("error"),
                          "experimental": True, "live_verified": False})
        return items


    def reconcile(self, operation_id, verdict, evidence, message_id=None):
        if verdict not in ("published", "absent", "cancelled"):
            raise SchedulingError("verdict должен быть published, absent или cancelled.")
        if not isinstance(evidence, str) or len(evidence.strip()) < 8:
            raise SchedulingError("Укажите проверяемое основание (минимум 8 символов).")
        with _schedule_lock(self.root):
            operation = self.operation(operation_id)
            snapshot = operation["snapshot"]
            if verdict == "cancelled":
                if operation["status"] not in ("scheduled", "proposed", "unknown", "error"):
                    raise SchedulingError("Отмена допустима только для известного предложения/расписания.")
                if self._known_receipt(operation_id, "schedule") and operation["status"] == "scheduled":
                    raise SchedulingError("Известное расписание нельзя отменить без факта отмены.")
                with self.conn:
                    self.conn.execute("UPDATE schedule_operations SET status='cancelled',result=?,error=NULL WHERE id=?",
                                      (_json({"evidence": evidence.strip(), "cancelled_at": _now()}), operation_id))
                    self._event(operation_id, "cancelled", evidence.strip())
                return self.operation(operation_id)
            if verdict == "absent":
                if message_id is not None:
                    raise SchedulingError("Укажите message_id либо отсутствие.")
                if self._known_receipt(operation_id, "schedule") or self._known_receipt(operation_id, "proposal"):
                    raise SchedulingError("Есть receipt; отсутствие заявить нельзя.")
                if operation["status"] != "unknown":
                    raise SchedulingError("Отсутствие допустимо только для unknown.")
                with self.conn:
                    self.conn.execute("UPDATE schedule_operations SET status='error',error=? WHERE id=?",
                                      ("Отсутствие подтверждено: " + evidence.strip(), operation_id))
                    self._event(operation_id, "error", evidence.strip())
                return self.operation(operation_id)
            if type(message_id) is not int or message_id < 1:
                raise SchedulingError("Укажите channel message_id и основание.")
            schedule = self._known_receipt(operation_id, "schedule")
            if schedule is None:
                raise SchedulingError("Публикация без расписания запрещена.")
            if operation["status"] not in ("scheduled", "unknown", "proposed", "error", "published"):
                raise SchedulingError("Конфликтующий переход отклонён.")
            known = operation["result"] if operation["status"] == "published" else None
            if isinstance(known, dict) and known.get("message_id") != message_id:
                raise SchedulingError("Конфликтующий message_id отклонён.")
            receipt = {"operation_id": operation_id, "snapshot_hash": _digest(snapshot),
                       "message_id": message_id, "channel": snapshot.get("channel"),
                       "send_date": snapshot.get("send_date"), "evidence": evidence.strip(),
                       "confirmed_at": _now(), "experimental": True, "live_verified": False}
            with self.conn:
                self.conn.execute("UPDATE schedule_operations SET status='published',result=?,error=NULL WHERE id=?",
                                  (_json(receipt), operation_id))
                self._event(operation_id, "published", evidence.strip())
            return self.operation(operation_id)

    def cancel(self, operation_id=None):
        raise SchedulingError("Отмена approved unsupported: метод не документирован; без сети.")

    def reschedule(self, operation_id=None, when=None):
        raise SchedulingError("Перенос approved unsupported: метод не документирован; без сети.")


def _load_zone(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Нужна явная IANA-зона, например Europe/Moscow")
    return ZoneInfo(name.strip())


def _coerce_now(now):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise ValueError("now должен быть datetime")
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _parse_iso(text):
    candidate = text.strip()
    if not isinstance(text, str) or not candidate:
        raise ValueError("Нужна дата и время: ISO или «завтра в 18:00»")
    normalized = candidate[:-1] + "+00:00" if candidate[-1:] in ("Z", "z") else candidate
    if ":" not in normalized:
        raise ValueError("Нужно точное время ЧЧ:ММ, дата без времени отклонена")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError("Неизвестный формат: нужен ISO или «завтра в 18:00»") from None
    return parsed


def _parse_russian(text, zone, moment):
    pattern = re.compile(
        r"^\s*(сегодня|завтра|послезавтра)\s+(?:в\s+)?([01][0-9]|2[0-3]):([0-5][0-9])\s*$",
        re.IGNORECASE,
    )
    match = pattern.match(text)
    if not match:
        return None
    word, hour, minute = match.group(1).lower(), int(match.group(2)), int(match.group(3))
    local_today = moment.astimezone(zone).date()
    from datetime import timedelta as _delta
    offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[word]
    day = local_today + _delta(days=offset)
    return datetime(day.year, day.month, day.day, hour, minute)


def _resolve_wall(naive, zone):
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("Неоднозначное время (переход DST): уточните момент")
    instant = first.astimezone(timezone.utc)
    back = instant.astimezone(zone)
    if back.replace(tzinfo=None) != naive:
        raise ValueError("Несуществующее время (переход DST): выберите другое")
    return first
def parse_schedule_time(value, timezone_name, now=None):
    if not isinstance(value, str):
        raise ValueError("Нужна дата и время: ISO или «завтра в 18:00»")
    zone = _load_zone(timezone_name)
    moment = _coerce_now(now)
    russian = _parse_russian(value, zone, moment)
    if russian is not None:
        aware = _resolve_wall(russian, zone)
    else:
        parsed = _parse_iso(value)
        if parsed.tzinfo is None:
            aware = _resolve_wall(parsed, zone)
        else:
            aware = parsed
    instant = aware.astimezone(timezone.utc)
    delta = (instant - moment).total_seconds()
    if delta < MIN_DELAY_SECONDS:
        raise ValueError("Дата должна быть минимум на 300 секунд в будущем")
    if delta > MAX_DELAY_SECONDS:
        raise ValueError("Дата должна быть не позже 2678400 секунд (31 сутки)")
    return {
        "iso": aware.astimezone(zone).isoformat(),
        "send_date": int(instant.timestamp()),
        "timezone": zone.key,
    }
