"""Тесты агентных команд расписания: dispatch, время, offline, unsupported (stdlib)."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


def _run(root, *argv):
    import tool
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = tool.main(["--root", str(root), *argv])
    text = stdout.getvalue()
    try:
        output = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        output = {"raw": text}
    return code, output, stderr.getvalue()


def _future_iso(zone="Europe/Moscow"):
    from scheduling import parse_schedule_time
    from zoneinfo import ZoneInfo
    moment = datetime.now(timezone.utc) + timedelta(days=2)
    local = moment.astimezone(ZoneInfo(zone))
    candidate = local.replace(hour=18, minute=0, second=0, microsecond=0)
    if (candidate.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() < 400:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def _reviewed(root):
    from core import Store
    from scheduling import Scheduling
    from test_scheduling import FakeScheduleAPI
    store = Store(root)
    scheduler = Scheduling(store, FakeScheduleAPI())
    scheduler.configure(channel=-100555, dm_chat_id=-200555, topic_id=7,
                        timezone_name='Europe/Moscow')
    post = store.create('План', [{'type': 'paragraph', 'text': 'Текст'}], {})
    operation = scheduler.review(post['id'], _future_iso(), experimental=True)
    return scheduler, operation


class ScheduleCliDispatchTests(unittest.TestCase):
    def test_deferred_network_commands_preserve_reviewed_operation_without_token(self):
        from core import Store
        from scheduling import Scheduling
        with tempfile.TemporaryDirectory(prefix='CLI пробел ') as directory:
            scheduler, operation = _reviewed(directory)
            before = scheduler.operation(operation['id'])
            scheduler.store.conn.close()
            commands = (
                ('schedule-review', operation['post_id'], _future_iso(), '--experimental'),
                ('schedule-execute', operation['id'], '--fingerprint',
                 operation['fingerprint'], '--experimental'),
            )
            with patch('tool.load_api', side_effect=AssertionError('token forbidden')) as loader:
                for command in commands:
                    code, output, error = _run(directory, *command)
                    self.assertEqual(code, 2)
                    self.assertIsNone(output)
                    self.assertIn('отключена', error)
                    self.assertIn('docs/ROADMAP.md', error)
                loader.assert_not_called()
            store = Store(directory)
            try:
                restored = Scheduling(store).operation(operation['id'])
                self.assertEqual(restored, before)
            finally:
                store.conn.close()

    def test_execute_requires_experimental_and_matching_fingerprint_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            scheduler, operation = _reviewed(directory)
            scheduler.store.conn.close()
            with patch('tool.load_api', side_effect=AssertionError('token forbidden')) as loader:
                code, _, error = _run(directory, 'schedule-execute', operation['id'],
                                      '--fingerprint', operation['fingerprint'])
                self.assertEqual(code, 2)
                self.assertIn('--experimental', error)
                code, _, error = _run(directory, 'schedule-execute', operation['id'],
                                      '--fingerprint', 'wrong', '--experimental')
                self.assertEqual(code, 2)
                self.assertIn('Fingerprint', error)
            loader.assert_not_called()

    def test_reconcile_dispatch_requires_evidence_and_rejects_conflicting_id_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            scheduler, operation = _reviewed(directory)
            scheduler.execute(operation['id'], operation['fingerprint'], experimental=True)
            scheduler.store.conn.close()
            with patch('tool.load_api', side_effect=AssertionError('token forbidden')) as loader:
                code, _, error = _run(directory, 'schedule-reconcile', operation['id'],
                                      '--verdict', 'published', '--evidence', ' ', '--message-id', '777')
                self.assertEqual(code, 2)
                self.assertTrue(error.strip())
                command = ('schedule-reconcile', operation['id'], '--verdict', 'published',
                           '--evidence', 'Проверено человеком в клиенте Telegram', '--message-id')
                code, published, error = _run(directory, *command, '777')
                self.assertEqual((code, error), (0, ''))
                self.assertEqual(published['status'], 'published')
                self.assertEqual(published['result']['message_id'], 777)
                self.assertEqual(published['result']['channel'], -100555)
                code, repeated, error = _run(directory, *command, '777')
                self.assertEqual((code, error, repeated['status']), (0, '', 'published'))
                code, _, error = _run(directory, *command, '778')
                self.assertEqual(code, 2)
                self.assertTrue(error.strip())
                code, receipt, error = _run(directory, 'schedule-execute', operation['id'],
                                            '--fingerprint', operation['fingerprint'], '--experimental')
                self.assertEqual((code, error, receipt['proposal_message_id']), (0, '', 55))
            loader.assert_not_called()

    def test_forbidden_execute_states_fail_before_token_loading(self):
        for status in ('unknown', 'error', 'sending', 'proposed', 'approving', 'cancelled'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                scheduler, operation = _reviewed(directory)
                # Синтетический сохранённый итог отказа/прерывания процесса.
                scheduler.conn.execute('UPDATE schedule_operations SET status=? WHERE id=?',
                                       (status, operation['id']))
                scheduler.conn.commit()
                scheduler.store.conn.close()
                with patch('tool.load_api', side_effect=AssertionError('token forbidden')) as loader:
                    code, _, error = _run(directory, 'schedule-execute', operation['id'],
                                          '--fingerprint', operation['fingerprint'], '--experimental')
                self.assertEqual(code, 2)
                self.assertIn('Повтор заблокирован', error)
                loader.assert_not_called()

    def test_overdue_list_keeps_scheduled_and_explains_unconfirmed_publication(self):
        from core import Store
        from scheduling import Scheduling
        from test_scheduling import FakeScheduleAPI
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            scheduler = Scheduling(store, FakeScheduleAPI())
            scheduler.configure(channel=-100555, dm_chat_id=-200555, topic_id=7,
                                timezone_name='Europe/Moscow')
            post = store.create('План', [{'type': 'paragraph', 'text': 'Текст'}], {})
            now = datetime(2020, 1, 1, tzinfo=timezone.utc)
            operation = scheduler.review(post['id'], '2020-01-02T18:00:00+03:00',
                                         experimental=True, now=now)
            scheduler.execute(operation['id'], operation['fingerprint'], experimental=True, now=now)
            store.conn.close()
            with patch('tool.load_api') as loader:
                code, items, error = _run(directory, 'schedule-list')
            self.assertEqual((code, error), (0, ''))
            self.assertEqual(items[0]['status'], 'scheduled')
            self.assertEqual(items[0]['status_note'], 'Время наступило; публикация не подтверждена')
            self.assertNotIn('snapshot', items[0])
            loader.assert_not_called()

    def test_config_time_list_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            code, saved, _ = _run(directory, "schedule-config", "--channel", "@fictional",
                               "--dm-chat-id", "-200555", "--topic-id", "7",
                               "--timezone", "Europe/Moscow")
            self.assertEqual(code, 0)
            self.assertEqual(saved["channel"], "@fictional")
            when = _future_iso()
            code, parsed, _ = _run(directory, "schedule-time", when, "--timezone", "Europe/Moscow")
            self.assertEqual(code, 0)
            self.assertIn("+03:00", parsed["iso"])
            self.assertEqual(parsed["timezone"], "Europe/Moscow")
            code, items, _ = _run(directory, "schedule-list")
            self.assertEqual(code, 0)
            self.assertEqual(items, [])

    def test_schedule_time_requires_configured_zone_and_rejects_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('tool.load_api') as loader:
                code, _, error = _run(directory, 'schedule-time', 'завтра в 18:00')
                self.assertEqual(code, 2)
                self.assertIn('schedule-config', error)
                code, _, error = _run(directory, 'schedule-config', '--channel', '@fictional',
                                      '--timezone', 'Europe/Moscow')
                self.assertEqual(code, 0)
                code, _, error = _run(directory, 'schedule-time', 'в пятницу')
                self.assertEqual(code, 2)
                self.assertIn('Нужно точное время', error)
            loader.assert_not_called()

    def test_offline_commands_never_load_token_and_unsupported_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('tool.load_api') as loader:
                code, recovered, error = _run(directory, 'schedule-recover')
                self.assertEqual(code, 0)
                self.assertEqual(recovered, [])
                self.assertEqual(error, '')
                code, _, error = _run(directory, 'schedule-cancel', 'not-an-operation')
                self.assertEqual(code, 2)
                self.assertIn('unsupported', error)
                code, _, error = _run(directory, 'schedule-reschedule', 'not-an-operation', 'завтра в 18:00')
                self.assertEqual(code, 2)
                self.assertIn('unsupported', error)
            loader.assert_not_called()

    def test_schedule_review_requires_experimental_before_loading_token(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('tool.load_api') as loader:
                code, _, error = _run(directory, 'schedule-review', 'post-id', _future_iso())
            self.assertEqual(code, 2)
            self.assertIn('--experimental', error)
            loader.assert_not_called()
