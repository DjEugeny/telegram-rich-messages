import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import base64

from core import Store
import delivery
import tool


# Синтетический TEST/FAKE/CANARY для тестов redaction; не реальный Telegram token.
SYNTHETIC_CANARY_TOKEN = '123456:TEST_FAKE_CANARY_abcdefghijklmnopqrstuvwxyz'


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.bot_id = 101
        self.mode = 'success'

    def call(self, method, payload=None, files=None):
        self.calls.append((method, payload or {}, files or {}))
        if method == 'getMe':
            return {'id': self.bot_id, 'is_bot': True, 'username': 'fictional_bot'}
        if method == 'getChat':
            return {'id': -100123, 'type': 'channel', 'title': 'Вымышленный канал', 'username': 'fictional'}
        if method == 'getChatMember':
            return {'status': 'administrator', 'can_post_messages': True, 'can_edit_messages': True}
        if method == 'getWebhookInfo':
            return {'url': ''}
        if method == 'getUpdates':
            return []
        if self.mode == 'timeout':
            raise TimeoutError('synthetic token URL must not escape')
        if self.mode == 'reject':
            raise delivery.APIRejected(403)
        return {'message_id': 77, 'chat': {'id': -100123, 'type': 'channel'}, 'date': 1792252800}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.api = FakeAPI()
        self.manager = delivery.Delivery(self.store, self.api)
        self.heading = {'type': 'heading', 'level': 2, 'text': 'Черновик'}
        self.post = self.store.create('Черновик', [self.heading, {'type': 'paragraph', 'text': 'Согласованный текст'}], {'chat_id': '@fictional'})

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def test_cli_ordered_mixed_media_defaults_and_update_preserves_blocks(self):
        from core import create_demo
        image = next(b for b in create_demo(self.root)['blocks'] if b['type'] == 'image')
        video = self.root / 'fixture.mp4'
        video.write_bytes(base64.b64decode((Path(__file__).parent / 'fixtures' / 'avc-96x160.mp4.base64').read_text()))
        output = io.StringIO()
        with patch('tool.load_api') as loader, contextlib.redirect_stdout(output):
            self.assertEqual(tool.main(['--root', str(self.root), 'create', '--title', 'Медиа', '--text', 'Текст', '--video', str(video), '--image', str(self.root / image['path']), '--media', str(video)]), 0)
            created = json.loads(output.getvalue())
            self.assertEqual([b['type'] for b in created['blocks']], ['media_group', 'paragraph'])
            self.assertEqual(created['blocks'][0]['layout'], 'collage')
            self.assertEqual([b['type'] for b in created['blocks'][0]['items']], ['video', 'image', 'video'])
            output.seek(0)
            output.truncate()
            self.assertEqual(tool.main(['--root', str(self.root), 'update', created['id'], '--media', str(video), '--media-position', 'after']), 0)
            updated = json.loads(output.getvalue())
            self.assertEqual(updated['blocks'][:-1], created['blocks'])
            self.assertEqual(updated['blocks'][-1]['type'], 'video')
            self.assertEqual(self.store.get(created['id'], 1)['blocks'], created['blocks'])
            for layout in ('slideshow', 'separate'):
                explicit = [{'type': 'paragraph', 'text': 'Первый'}, {'type': 'heading', 'text': 'Второй'}]
                blocks_path = self.root / 'explicit-blocks.json'
                blocks_path.write_text(json.dumps(explicit), encoding='utf-8')
                output.seek(0)
                output.truncate()
                self.assertEqual(tool.main(['--root', str(self.root), 'import', '--title', 'Явный порядок', '--blocks', str(blocks_path), '--media', str(video), '--image', str(self.root / image['path']), '--media-layout', layout, '--media-position', 'after']), 0)
                saved = json.loads(output.getvalue())
                self.assertEqual(saved['blocks'][:2], explicit)
                if layout == 'separate':
                    self.assertEqual([b['type'] for b in saved['blocks'][2:]], ['video', 'image'])
                else:
                    self.assertEqual(saved['blocks'][2]['layout'], 'slideshow')
                    self.assertEqual([b['type'] for b in saved['blocks'][2]['items']], ['video', 'image'])
            loader.assert_not_called()

    def test_review_uses_actual_video_and_photo_permissions(self):
        from core import import_video, create_demo
        source = self.root / 'fixture.mp4'
        source.write_bytes(base64.b64decode((Path(__file__).parent / 'fixtures' / 'avc-96x160.mp4.base64').read_text()))
        video = dict(import_video(self.root, source), type='video')
        image = next(b for b in create_demo(self.root)['blocks'] if b['type'] == 'image')
        original = self.api.call
        permissions = {'can_send_messages': True, 'can_send_photos': False, 'can_send_videos': True}
        def restricted(method, payload=None, files=None):
            if method == 'getChat':
                return {'id': -100123, 'type': 'supergroup', 'username': 'fictional'}
            if method == 'getChatMember':
                return dict(permissions, status='restricted', is_member=True)
            return original(method, payload, files)
        self.api.call = restricted
        post = self.store.update(self.post['id'], blocks=[video])
        operation = self.manager.review(post['id'])  # video alone does not need photo permission
        permissions['can_send_videos'] = False
        with self.assertRaises(delivery.DeliveryError):
            self.manager.execute(operation['id'], operation['fingerprint'])
        permissions['can_send_videos'] = True
        post = self.store.update(post['id'], blocks=[{'type': 'media_group', 'layout': 'collage', 'items': [video, image]}])
        with self.assertRaises(delivery.DeliveryError):
            self.manager.review(post['id'])
        permissions['can_send_photos'] = True
        self.manager.review(post['id'])
        permissions.pop('can_send_videos')
        with self.assertRaises(delivery.DeliveryError):
            self.manager.review(post['id'])
        self.assertFalse(any(c[0] == 'sendRichMessage' for c in self.api.calls))

    def test_review_memory_is_bounded_for_many_video_attachments(self):
        from core import import_video
        import struct
        import tracemalloc
        source = self.root / 'padded-fixture.mp4'
        fixture = base64.b64decode((Path(__file__).parent / 'fixtures' / 'avc-96x160.mp4.base64').read_text())
        # ISO-BMFF free box leaves the known AVC sample data/offsets intact.
        source.write_bytes(fixture + struct.pack('>I4s', 2 * 1024 * 1024 + 8, b'free') + b'\0' * (2 * 1024 * 1024))
        video = dict(import_video(self.root, source), type='video')
        post = self.store.update(self.post['id'], blocks=[{'type': 'media_group', 'layout': 'collage', 'items': [video] * 20}])
        tracemalloc.start()
        try:
            operation = self.manager.review(post['id'])
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(operation['snapshot']['media']), 20)
        # Includes headroom for the 50 MB per-file read limit, but cannot hold
        # twenty 2 MB payloads alongside another maximum-sized read buffer.
        self.assertLess(peak, 64 * 1024 * 1024)
        self.assertFalse(any(c[0] == 'sendRichMessage' for c in self.api.calls))

    def test_mixed_delivery_strips_metadata_and_multipart_uses_mp4(self):
        from core import import_video, create_demo
        source = self.root / 'unsafe-name.mp4'
        data = base64.b64decode((Path(__file__).parent / 'fixtures' / 'avc-96x160.mp4.base64').read_text())
        source.write_bytes(data)
        video = dict(import_video(self.root, source), type='video')
        image = next(b for b in create_demo(self.root)['blocks'] if b['type'] == 'image')
        post = self.store.update(self.post['id'], blocks=[{'type': 'media_group', 'layout': 'slideshow', 'items': [video, image]}])
        operation = self.manager.review(post['id'])
        self.manager.execute(operation['id'], operation['fingerprint'])
        request = next(c for c in self.api.calls if c[0] == 'sendRichMessage')
        self.assertEqual(request[1]['rich_message']['media'], [
            {'id': '1', 'media': {'type': 'video', 'media': 'attach://video_1'}},
            {'id': '2', 'media': {'type': 'photo', 'media': 'attach://photo_2'}}])
        self.assertIn('<tg-slideshow>', request[1]['rich_message']['markdown'])
        self.assertEqual(request[2]['video_1'][1:], ('video/mp4', data))
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok":true,"result":{"message_id":77}}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch('urllib.request.build_opener', return_value=opener):
            delivery.TelegramAPI(SYNTHETIC_CANARY_TOKEN).call('sendRichMessage', request[1], request[2])
        multipart = opener.open.call_args.args[0].data
        self.assertIn(b'filename="video_1.mp4"\r\nContent-Type: video/mp4\r\n', multipart)
        self.assertIn(data, multipart)
        for local in (b'unsafe-name', b'"sha256"', b'"path"', b'"duration"', b'"source"'):
            self.assertNotIn(local, multipart)

    def test_demo_media_is_autonomous_idempotent_and_keeps_existing_versions(self):
        from core import create_demo, compile_post, validate_media
        import shutil
        old_demo = create_demo(self.root)
        output = io.StringIO()
        with patch('tool.load_api') as loader, contextlib.redirect_stdout(output):
            self.assertEqual(tool.main(['--root', str(self.root), 'demo-media']), 0)
            first = json.loads(output.getvalue())
            self.assertEqual(len(first), 4)
            self.assertEqual([len(compile_post(post)['media']) for post in first], [6, 1, 4, 1])
            self.assertEqual([post['blocks'][0]['type'] for post in first], ['media_group', 'image', 'media_group', 'image'])
            for post in first:
                self.assertIn('Вымышленный', post['title'])
                self.assertEqual(post['version'], 1)
                self.assertEqual(post['status'], 'draft')
                validate_media(self.root, post)
            self.assertEqual([(b['width'], b['height']) for b in first[0]['blocks'][0]['items']], [(320, 400)] * 6)
            self.assertEqual((first[1]['blocks'][0]['width'], first[1]['blocks'][0]['height']), (300, 400))
            mixed = first[2]['blocks'][0]['items']
            self.assertEqual([b['type'] for b in mixed], ['video', 'image', 'image', 'image'])
            self.assertEqual([(b['width'], b['height']) for b in mixed[1:]], [(225, 400)] * 3)
            self.assertIn('tg://emoji?id=', compile_post(first[2])['markdown'])
            self.assertTrue(any(b['type'] == 'quote' for b in first[2]['blocks']))
            # Изменённый человеком demo сохраняется; повтор не возвращает v1.
            edited = self.store.update(first[0]['id'], title='Сохранённая правка человека')
            media_names = sorted(path.name for path in (self.root / 'media').iterdir())
            output.seek(0)
            output.truncate()
            self.assertEqual(tool.main(['--root', str(self.root), 'demo-media']), 0)
            second = json.loads(output.getvalue())
            self.assertEqual([p['id'] for p in first], [p['id'] for p in second])
            self.assertEqual(second[0], edited)
            self.assertEqual(sorted(path.name for path in (self.root / 'media').iterdir()), media_names)
            self.assertEqual(self.store.get(old_demo['id']), old_demo)
            self.assertEqual(self.store.get(self.post['id']), self.post)
            loader.assert_not_called()
        self.store.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        with tempfile.TemporaryDirectory() as parent:
            moved = Path(parent) / 'Перенос'
            shutil.copytree(self.root, moved)
            portable = Store(moved)
            try:
                for post in second:
                    validate_media(moved, portable.get(post['id']))
            finally:
                portable.conn.close()

    def test_review_is_persisted_and_execute_is_once(self):
        operation = self.manager.review(self.post['id'])
        self.assertEqual(operation['status'], 'reviewed')
        self.assertEqual(operation['snapshot']['version'], 1)
        reopened = delivery.Delivery(self.store, self.api)
        result = reopened.execute(operation['id'], operation['fingerprint'])
        self.assertEqual(result['message_id'], 77)
        reopened.execute(operation['id'], operation['fingerprint'])
        self.assertEqual(sum(c[0] == 'sendRichMessage' for c in self.api.calls), 1)
        self.assertEqual(self.store.get(self.post['id'])['status'], 'published')

    def test_plain_or_bold_title_is_draft_only_and_cli_never_loads_secret(self):
        for text in ('Заголовок', '**Заголовок**', '# Заголовок'):
            with self.subTest(text=text):
                post = self.store.create('Название в истории', [{'type': 'paragraph', 'text': text}], {'chat_id': '@fictional'})
                before = len(self.api.calls)
                with self.assertRaisesRegex(delivery.DeliveryError, 'настоящего блока heading'):
                    self.manager.review(post['id'])
                self.assertEqual(len(self.api.calls), before)
                with patch('tool.load_api') as loader, contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(tool.main(['--root', str(self.root), 'review', post['id'], '--action', 'send']), 2)
                loader.assert_not_called()
                self.assertIn('настоящего блока heading', err.getvalue())
                self.assertEqual(self.store.get(post['id'])['version'], 1)
                self.assertEqual(self.store.conn.execute('SELECT count(*) FROM operations WHERE post_id=?', (post['id'],)).fetchone()[0], 0)

    def test_rich_structure_survives_review_send_and_edit(self):
        from core import compile_post
        blocks = [
            {'type': 'heading', 'level': 1, 'text': 'Тема'},
            {'type': 'paragraph', 'text': [{'type': 'bold', 'text': 'Важно'}, ' и ', {'type': 'link', 'text': 'источник', 'url': 'https://example.com'}]},
            {'type': 'heading', 'level': 2, 'text': 'Раздел'},
            {'type': 'list', 'items': ['Первый', 'Второй']},
            {'type': 'heading', 'level': 3, 'text': 'Шаги'},
            {'type': 'list', 'ordered': True, 'items': ['Начало', 'Конец']},
            {'type': 'quote', 'text': 'Предоставленная цитата'},
            {'type': 'table', 'headers': ['Вариант', 'Цена'], 'rows': [['А', '100']]},
            {'type': 'details', 'summary': 'Подробности', 'blocks': [
                {'type': 'heading', 'level': 4, 'text': 'Деталь'},
                {'type': 'paragraph', 'text': [{'type': 'code', 'text': 'x < 2'}]}]},
            {'type': 'heading', 'level': 5, 'text': 'Примечание'},
            {'type': 'heading', 'level': 6, 'text': 'Источник'},
            {'type': 'button', 'text': 'Открыть', 'url': 'https://example.com'},
        ]
        post = self.store.update(self.post['id'], blocks=blocks)
        compiled = compile_post(post)
        operation = self.manager.review(post['id'])
        formatting = operation['snapshot']['formatting']
        self.assertEqual(formatting['format'], 'rich_markdown')
        self.assertEqual([h['level'] for h in formatting['headings']], [1, 2, 3, 4, 5, 6])
        self.assertEqual(formatting['blocks']['list'], 2)
        for kind in ('quote', 'table', 'details', 'button'):
            self.assertEqual(formatting['blocks'][kind], 1)
        self.assertEqual(formatting['inline'], {'bold': 1, 'link': 1, 'code': 1})
        self.manager.execute(operation['id'], operation['fingerprint'])
        sent = next(c for c in self.api.calls if c[0] == 'sendRichMessage')[1]
        self.assertEqual(sent['rich_message']['markdown'], compiled['markdown'])
        self.assertNotIn('parse_mode', sent)
        for marker in ('# Тема', '## Раздел', '### Шаги', '#### Деталь', '##### Примечание', '###### Источник', '- Первый', '1. Начало', '> Предоставленная цитата', '| Вариант | Цена |', '<details>', '<tg-button'):
            self.assertIn(marker, sent['rich_message']['markdown'])
        for tag in ('<h1>', '<h6>', '<ul>', '<ol>', '<blockquote>', '<table>', '<details>'):
            self.assertIn(tag, compiled['html'])
        self.store.update(post['id'], blocks=blocks + [{'type': 'paragraph', 'text': 'Уточнение'}])
        edit = self.manager.review(post['id'], action='edit')
        self.manager.execute(edit['id'], edit['fingerprint'])
        edited = next(c for c in self.api.calls if c[0] == 'editMessageText')[1]
        self.assertEqual(edited['message_id'], 77)
        self.assertEqual(edited['rich_message']['markdown'], edit['snapshot']['markdown'])
        self.assertFalse(any(c[0] == 'sendMessage' for c in self.api.calls))

    def test_legacy_review_without_structure_is_blocked_before_network(self):
        plain = self.store.create('Старый черновик', [{'type': 'paragraph', 'text': '**Жирный заголовок**'}], {'chat_id': '@fictional'})
        for post in (plain, self.post):
            with self.subTest(post_id=post['id']):
                # Воссоздать review прежней версии сервиса, не включая обход в продукт.
                with patch('delivery.publication_format', return_value={}):
                    operation = self.manager.review(post['id'])
                snapshot = dict(operation['snapshot'])
                snapshot.pop('formatting')
                fingerprint = delivery._digest({'legacy': operation['id'], 'snapshot': snapshot})
                with self.store.conn:
                    self.store.conn.execute('UPDATE operations SET snapshot=?,fingerprint=? WHERE id=?',
                                            (delivery._json(snapshot), fingerprint, operation['id']))
                before = len(self.api.calls)
                with self.assertRaises(delivery.DeliveryError):
                    self.manager.execute(operation['id'], fingerprint)
                self.assertEqual(len(self.api.calls), before)
                with patch('tool.load_api') as loader, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(tool.main(['--root', str(self.root), 'execute', operation['id'], '--fingerprint', fingerprint]), 2)
                loader.assert_not_called()
                self.assertEqual(self.manager.operation(operation['id'])['status'], 'reviewed')

    def test_compiler_change_after_review_requires_fresh_approval(self):
        from core import compile_post
        operation = self.manager.review(self.post['id'])
        changed = compile_post(self.post)
        changed['markdown'] += '\n\nДругой результат новой версии compiler'
        before = len(self.api.calls)
        with patch('delivery.compile_post', return_value=changed):
            with self.assertRaisesRegex(delivery.DeliveryError, 'результат компиляции изменились'):
                self.manager.execute(operation['id'], operation['fingerprint'])
        self.assertEqual(len(self.api.calls), before)
        self.assertEqual(self.manager.operation(operation['id'])['status'], 'reviewed')

    def test_edit_cannot_remove_structure(self):
        operation = self.manager.review(self.post['id'])
        self.manager.execute(operation['id'], operation['fingerprint'])
        self.store.update(self.post['id'], blocks=[{'type': 'paragraph', 'text': '**Плоская правка**'}])
        before = len(self.api.calls)
        with self.assertRaisesRegex(delivery.DeliveryError, 'настоящего блока heading'):
            self.manager.review(self.post['id'], action='edit')
        self.assertEqual(len(self.api.calls), before)
        self.assertEqual(self.store.get(self.post['id'])['publication']['message_id'], 77)

    def test_transport_hides_token_in_network_exception(self):
        token = SYNTHETIC_CANARY_TOKEN
        transport = delivery.TelegramAPI(token)
        with patch('urllib.request.OpenerDirector.open', side_effect=OSError('https://api.telegram.org/bot' + token)):
            with self.assertRaises(delivery.DeliveryError) as caught:
                transport.call('getMe')
        self.assertNotIn(token, str(caught.exception))
        self.assertNotIn(token, repr(caught.exception))

    def test_explicit_restricted_photo_permission_is_required(self):
        original = self.api.call
        def restricted(method, payload=None, files=None):
            if method == 'getChat':
                return {'id': -100123, 'type': 'supergroup', 'username': 'fictional'}
            if method == 'getChatMember':
                return {'status': 'restricted', 'is_member': True, 'can_send_messages': True}
            return original(method, payload, files)
        self.api.call = restricted
        with self.assertRaises(delivery.DeliveryError):
            self.manager.check('@fictional', media=True)

    def test_prepared_operations_cannot_send_same_post_twice(self):
        first = self.manager.review(self.post['id'])
        second = self.manager.review(self.post['id'])
        self.manager.execute(first['id'], first['fingerprint'])
        with self.assertRaises(delivery.DeliveryError):
            self.manager.execute(second['id'], second['fingerprint'])
        self.assertEqual(sum(c[0] == 'sendRichMessage' for c in self.api.calls), 1)
        with self.assertRaises(delivery.DeliveryError):
            self.manager.review(self.post['id'])
        deliberate = self.manager.review(self.post['id'], new_publication=True)
        self.manager.execute(deliberate['id'], deliberate['fingerprint'])
        self.assertEqual(sum(c[0] == 'sendRichMessage' for c in self.api.calls), 2)

    def test_unknown_blocks_every_retry_and_manual_resolution_is_offline(self):
        operation = self.manager.review(self.post['id'])
        self.api.mode = 'timeout'
        with self.assertRaises(delivery.DeliveryError):
            self.manager.execute(operation['id'], operation['fingerprint'])
        self.assertEqual(self.manager.operation(operation['id'])['status'], 'unknown')
        calls = len(self.api.calls)
        for action in (lambda: self.manager.execute(operation['id'], operation['fingerprint']),
                       lambda: self.manager.review(self.post['id'], new_publication=True)):
            with self.assertRaises(delivery.DeliveryError):
                action()
        self.manager.api = None
        self.manager.recover()
        self.manager.resolve(operation['id'], 77, 'Человек сверил точный пост в Telegram')
        self.assertEqual(len(self.api.calls), calls)
        self.assertEqual(self.store.get(self.post['id'])['publication']['message_id'], 77)

    def test_version_target_bot_and_confirmation_changes_invalidate_review(self):
        operation = self.manager.review(self.post['id'])
        with self.assertRaises(delivery.DeliveryError):
            self.manager.execute(operation['id'], 'wrong fingerprint')
        self.api.bot_id = 202
        with self.assertRaises(delivery.DeliveryError):
            self.manager.execute(operation['id'], operation['fingerprint'])
        self.api.bot_id = 101
        self.store.update(self.post['id'], destination={'chat_id': '-100999'})
        with self.assertRaises(delivery.DeliveryError):
            self.manager.execute(operation['id'], operation['fingerprint'])
        self.assertFalse(any(c[0] == 'sendRichMessage' for c in self.api.calls))
        # Новые rich-типы сохраняют прежний контракт approval для порядка и ID.
        from core import import_video, create_demo
        source = self.root / 'fixture.mp4'
        source.write_bytes(base64.b64decode((Path(__file__).parent / 'fixtures' / 'avc-96x160.mp4.base64').read_text()))
        video = dict(import_video(self.root, source), type='video')
        image = next(b for b in create_demo(self.root)['blocks'] if b['type'] == 'image')
        initial = [{'type': 'media_group', 'layout': 'collage', 'items': [video, image]},
                   {'type': 'paragraph', 'text': [{'type': 'custom_emoji', 'text': '⭐', 'custom_emoji_id': '123456'}]},
                   {'type': 'quote', 'text': 'Первая цитата'}, self.heading]
        for changed in ([dict(initial[0], items=[image, video])] + initial[1:],
                        [dict(initial[0], layout='slideshow')] + initial[1:],
                        [initial[0], {'type': 'paragraph', 'text': [{'type': 'custom_emoji', 'text': '⭐', 'custom_emoji_id': '987654'}]}, initial[2], self.heading],
                        initial[:2] + [{'type': 'quote', 'text': 'Изменённая цитата'}, self.heading]):
            post = self.store.create('Вымышленный approval', initial, {'chat_id': '@fictional'})
            approved = self.manager.review(post['id'])
            self.store.update(post['id'], blocks=changed)
            with self.assertRaises(delivery.DeliveryError):
                self.manager.execute(approved['id'], approved['fingerprint'])
        self.assertFalse(any(c[0] == 'sendRichMessage' for c in self.api.calls))

    def test_api_rejection_is_error_and_a_fresh_review_is_required(self):
        operation = self.manager.review(self.post['id'])
        self.api.mode = 'reject'
        with self.assertRaises(delivery.APIRejected):
            self.manager.execute(operation['id'], operation['fingerprint'])
        self.assertEqual(self.manager.operation(operation['id'])['status'], 'error')
        with self.assertRaises(delivery.DeliveryError):
            self.manager.execute(operation['id'], operation['fingerprint'])
        self.api.mode = 'success'
        fresh = self.manager.review(self.post['id'])
        self.manager.execute(fresh['id'], fresh['fingerprint'])
        self.assertEqual(self.store.get(self.post['id'])['status'], 'published')

    def test_edit_keeps_message_id_and_visual_confirmation_is_separate(self):
        operation = self.manager.review(self.post['id'])
        self.manager.execute(operation['id'], operation['fingerprint'])
        self.assertFalse(self.store.get(self.post['id'])['visual_verified'])
        with self.assertRaises(delivery.DeliveryError):
            self.manager.verify_visual(self.post['id'])
        self.manager.verify_visual(self.post['id'], confirmed=True)
        self.store.update(self.post['id'], blocks=[self.heading, {'type': 'paragraph', 'text': 'Уточнено человеком'}])
        self.assertFalse(self.store.get(self.post['id'])['visual_verified'])
        operation = self.manager.review(self.post['id'], action='edit')
        result = self.manager.execute(operation['id'], operation['fingerprint'])
        self.assertEqual(result['message_id'], 77)
        edits = [c for c in self.api.calls if c[0] == 'editMessageText']
        self.assertEqual(edits[0][1]['message_id'], 77)
        self.assertEqual(sum(c[0] == 'sendRichMessage' for c in self.api.calls), 1)
        self.assertFalse(self.store.get(self.post['id'])['visual_verified'])

    def test_local_html_failure_does_not_allow_republication(self):
        operation = self.manager.review(self.post['id'])
        def fail(_root):
            raise OSError('fixture disk failure')
        result = self.manager.execute(operation['id'], operation['fingerprint'], rebuild=fail)
        self.assertEqual(result['message_id'], 77)
        self.assertIn('local_warning', result)
        self.assertEqual(self.manager.operation(operation['id'])['status'], 'published')
        self.manager.execute(operation['id'], operation['fingerprint'])
        self.assertEqual(sum(c[0] == 'sendRichMessage' for c in self.api.calls), 1)

    def test_sqlite_projection_failure_recovers_fsynced_receipt_without_network(self):
        operation = self.manager.review(self.post['id'])
        with patch.object(self.manager, '_apply_receipt', side_effect=OSError('fixture projection failed')):
            with self.assertRaises(delivery.DeliveryError):
                self.manager.execute(operation['id'], operation['fingerprint'])
        receipt = json.loads((self.root / 'data' / 'receipts' / (operation['id'] + '.json')).read_text())
        self.assertEqual(receipt['message_id'], 77)
        calls = len(self.api.calls)
        self.manager.api = None
        recovered = self.manager.recover()
        self.assertEqual(recovered[0]['status'], 'published')
        self.assertEqual(len(self.api.calls), calls)
        self.assertEqual(self.store.get(self.post['id'])['publication']['message_id'], 77)

    def test_receipt_file_failure_retains_id_in_sqlite_without_retry(self):
        operation = self.manager.review(self.post['id'])
        with patch('delivery._atomic_json', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(delivery.DeliveryError):
                self.manager.execute(operation['id'], operation['fingerprint'])
        self.manager.api = None
        self.manager.recover()
        self.assertEqual(self.store.get(self.post['id'])['publication']['message_id'], 77)
        self.assertEqual(sum(c[0] == 'sendRichMessage' for c in self.api.calls), 1)

    def test_missing_and_mutated_media_block_network_after_review(self):
        from core import create_demo
        post = create_demo(self.root)
        post = self.store.update(post['id'], destination={'chat_id': '@fictional'})
        operation = self.manager.review(post['id'])
        image = next(b for b in post['blocks'] if b['type'] == 'image')
        path = self.root / image['path']
        original = path.read_bytes()
        path.write_bytes(b'mutated fixture')
        calls = len(self.api.calls)
        with self.assertRaises(delivery.DeliveryError):
            self.manager.execute(operation['id'], operation['fingerprint'])
        path.unlink()
        with self.assertRaises(delivery.DeliveryError):
            self.manager.execute(operation['id'], operation['fingerprint'])
        self.assertEqual(len(self.api.calls), calls)
        path.write_bytes(original)
        self.manager.execute(operation['id'], operation['fingerprint'])
        request = next(c for c in self.api.calls if c[0] == 'sendRichMessage')
        self.assertEqual(request[1]['rich_message']['media'][0]['media']['media'], 'attach://photo_1')
        self.assertEqual(request[2]['photo_1'][2], original)

    def test_wrong_response_chat_or_edit_id_is_unknown(self):
        operation = self.manager.review(self.post['id'])
        original = self.api.call
        def wrong(method, payload=None, files=None):
            if method == 'sendRichMessage':
                return {'message_id': 77, 'chat': {'id': -100999}}
            return original(method, payload, files)
        self.api.call = wrong
        with self.assertRaises(delivery.DeliveryError):
            self.manager.execute(operation['id'], operation['fingerprint'])
        self.assertEqual(self.manager.operation(operation['id'])['status'], 'unknown')

    def _worker(self, operation, mode):
        code = '''
import os, sys, time
from pathlib import Path
from core import Store
from delivery import Delivery
root, oid, fingerprint, mode = sys.argv[1:]
class API:
 def call(self, method, payload=None, files=None):
  if method == 'getMe': return {'id':101,'is_bot':True,'username':'fictional_bot'}
  if method == 'getChat': return {'id':-100123,'type':'channel','title':'Вымышленный канал','username':'fictional'}
  if method == 'getChatMember': return {'status':'administrator','can_post_messages':True,'can_edit_messages':True}
  with (Path(root)/'network-marker').open('ab') as f:
   f.write(b'network-request\\n'); f.flush(); os.fsync(f.fileno())
  if mode == 'crash': os._exit(9)
  time.sleep(0.8)
  return {'message_id':77,'chat':{'id':-100123,'type':'channel'}}
store=Store(root)
try:
 Delivery(store, API()).execute(oid,fingerprint)
except Exception:
 sys.exit(2)
finally:
 store.conn.close()
'''
        return subprocess.Popen([sys.executable, '-c', code, str(self.root), operation['id'], operation['fingerprint'], mode],
                                cwd=Path(delivery.__file__).parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _wait_marker(self, process):
        end = time.monotonic() + 5
        while not (self.root / 'network-marker').exists() and time.monotonic() < end and process.poll() is None:
            time.sleep(0.01)
        self.assertTrue((self.root / 'network-marker').exists(), 'worker did not reach fake network')

    def test_crossprocess_lock_blocks_parallel_execution_and_active_recovery(self):
        first = self.manager.review(self.post['id'])
        second = self.manager.review(self.post['id'])
        process = self._worker(first, 'success')
        try:
            self._wait_marker(process)
            other = self._worker(second, 'success')
            self.assertEqual(other.wait(timeout=5), 2)
            with self.assertRaises(delivery.DeliveryError):
                self.manager.recover()
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        self.assertEqual((self.root / 'network-marker').read_bytes(), b'network-request\n')
        self.assertEqual(self.manager.operation(first['id'])['status'], 'published')

    def test_process_crash_mid_request_becomes_unknown_and_never_retries(self):
        operation = self.manager.review(self.post['id'])
        process = self._worker(operation, 'crash')
        self.assertEqual(process.wait(timeout=5), 9)
        self.assertEqual(self.manager.operation(operation['id'])['status'], 'sending')
        self.manager.api = None
        self.manager.recover()
        self.assertEqual(self.manager.operation(operation['id'])['status'], 'unknown')
        with self.assertRaises(delivery.DeliveryError):
            self.manager.review(self.post['id'], new_publication=True)
        self.assertEqual((self.root / 'network-marker').read_bytes(), b'network-request\n')

    def test_cli_discovery_consent_and_webhook_are_not_bypassed(self):
        with patch('tool.load_api', return_value=self.api) as loader, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tool.main(['--root', str(self.root), 'discover']), 2)
            loader.assert_not_called()
        original = self.api.call
        def webhook(method, payload=None, files=None):
            if method == 'getWebhookInfo':
                return {'url': 'https://fictional.invalid/hook'}
            return original(method, payload, files)
        self.api.call = webhook
        with patch('tool.load_api', return_value=self.api), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tool.main(['--root', str(self.root), 'discover', '--isolated-bot-consent']), 2)
        self.assertFalse(any(c[0] == 'getUpdates' for c in self.api.calls))

    def test_token_set_without_hidden_terminal_never_reads_token(self):
        with patch('sys.stdin.isatty', return_value=False), patch('getpass.getpass') as hidden:
            with self.assertRaises(delivery.DeliveryError):
                delivery.set_token(self.root)
            hidden.assert_not_called()

    def test_keychain_uses_secret_only_on_stdin_and_checks_persistence(self):
        token = SYNTHETIC_CANARY_TOKEN
        from types import SimpleNamespace
        fake_results = [SimpleNamespace(returncode=0, stdout='', stderr=''),
                        SimpleNamespace(returncode=0, stdout=token + '\n', stderr='')]
        with patch('sys.platform', 'darwin'), patch('sys.stdin.isatty', return_value=True), patch('getpass.getpass', return_value=token), patch('subprocess.run', side_effect=fake_results) as run:
            result = delivery.set_token(self.root)
        self.assertNotIn(token, json.dumps(result))
        self.assertIn(token, run.call_args_list[0].kwargs['input'])
        for call in run.call_args_list:
            self.assertNotIn(token, ' '.join(call.args[0]))
        self.assertEqual(run.call_args_list[0].args[0], ['security', '-i'])

    def test_transport_rich_multipart_and_response_redaction(self):
        token = SYNTHETIC_CANARY_TOKEN
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({'ok': True, 'result': {'message_id': 77, 'echo': token}}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch('urllib.request.build_opener', return_value=opener):
            result = delivery.TelegramAPI(token).call('sendRichMessage',
                       {'chat_id': -100123, 'rich_message': {'markdown': '![x](tg://photo?id=1)', 'media': [{'id': '1', 'media': {'type': 'photo', 'media': 'attach://photo_1'}}]}},
                       {'photo_1': ('unsafe"\r\nname.png', 'image/png', b'fixture bytes')})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.telegram.org/bot' + token + '/sendRichMessage')
        self.assertIn(b'name="photo_1"; filename="photo_1.png"', request.data)
        self.assertIn(b'attach://photo_1', request.data)
        self.assertIn(b'fixture bytes', request.data)
        self.assertNotIn(b'unsafe', request.data)
        self.assertNotIn(token, json.dumps(result))

    def test_failed_keychain_repl_is_not_reported_as_saved(self):
        from types import SimpleNamespace
        token = SYNTHETIC_CANARY_TOKEN
        responses = [SimpleNamespace(returncode=0, stdout='', stderr='failure'),
                     SimpleNamespace(returncode=44, stdout='', stderr='not found')]
        with patch('sys.platform', 'darwin'), patch('sys.stdin.isatty', return_value=True), patch('getpass.getpass', return_value=token), patch('subprocess.run', side_effect=responses):
            with self.assertRaises(delivery.DeliveryError):
                delivery.set_token(self.root)

    def test_bad_settings_and_no_post_permission_fail_before_sending(self):
        post = self.store.create('Без назначения', [self.heading, {'type': 'paragraph', 'text': 'Локально'}])
        (self.root / 'data' / 'settings.json').write_text('{broken', encoding='utf-8')
        with self.assertRaises(delivery.DeliveryError):
            self.manager.review(post['id'])
        original = self.api.call
        def denied(method, payload=None, files=None):
            if method == 'getChatMember':
                return {'status': 'administrator', 'can_post_messages': False}
            return original(method, payload, files)
        self.api.call = denied
        with self.assertRaises(delivery.DeliveryError):
            self.manager.review(self.post['id'])
        self.assertFalse(any(c[0] == 'sendRichMessage' for c in self.api.calls))

    def test_cli_local_roundtrip_and_published_retry_do_not_load_secrets(self):
        out = io.StringIO()
        with patch('tool.load_api') as loader, contextlib.redirect_stdout(out):
            self.assertEqual(tool.main(['--root', str(self.root), 'create', '--title', 'Через CLI', '--text', 'Без сети']), 0)
            created = json.loads(out.getvalue())
            out.seek(0)
            out.truncate()
            self.assertEqual(tool.main(['--root', str(self.root), 'update', created['id'], '--text', 'Версия 2']), 0)
            self.assertEqual(json.loads(out.getvalue())['version'], 2)
            loader.assert_not_called()
        operation = self.manager.review(self.post['id'])
        self.manager.execute(operation['id'], operation['fingerprint'])
        with patch('tool.load_api') as loader, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(['--root', str(self.root), 'execute', operation['id'], '--fingerprint', operation['fingerprint']]), 0)
            loader.assert_not_called()

    def test_known_receipt_cannot_be_overridden_as_absent(self):
        operation = self.manager.review(self.post['id'])
        with patch('delivery._atomic_json', side_effect=OSError('fixture failed file save')):
            with self.assertRaises(delivery.DeliveryError):
                self.manager.execute(operation['id'], operation['fingerprint'])
        self.assertEqual(self.manager.operation(operation['id'])['status'], 'unknown')
        with self.assertRaises(delivery.DeliveryError):
            self.manager.resolve(operation['id'], absent=True, evidence='Человек ошибочно считает что результата нет')

    def test_private_chat_username_does_not_produce_guessed_message_link(self):
        self.assertIsNone(delivery.Delivery._message_url({'chat_id': 123, 'chat_type': 'private', 'chat_username': 'fictional_user'}, 77))

    def test_visual_confirmation_is_atomic_against_parallel_version_update(self):
        """Writer пробует BEGIN между чтением v1 и установкой visual flag."""
        import sqlite3
        import threading
        operation = self.manager.review(self.post['id'])
        self.manager.execute(operation['id'], operation['fingerprint'])
        read = self.store.get
        writer_ready = threading.Event()
        attempted = threading.Event()
        attempt_finished = threading.Event()
        retry = threading.Event()
        errors = []
        outcome = []

        def write_new_version():
            writer = Store(self.root)
            writer.conn.execute('PRAGMA busy_timeout=50')
            writer_ready.set()
            try:
                attempted.wait(5)
                try:
                    writer.update(self.post['id'], blocks=[{'type': 'paragraph', 'text': 'Новая непубликованная версия'}])
                    outcome.append('updated_between_read_and_flag')
                except sqlite3.OperationalError as error:
                    if 'locked' not in str(error):
                        raise
                    outcome.append('blocked_by_atomic_visual_transaction')
                    attempt_finished.set()
                    if not retry.wait(5):
                        raise AssertionError('visual did not finish')
                    writer.update(self.post['id'], blocks=[{'type': 'paragraph', 'text': 'Новая непубликованная версия'}])
            except BaseException as error:
                errors.append(error)
            finally:
                attempt_finished.set()
                writer.conn.close()

        worker = threading.Thread(target=write_new_version)
        worker.start()
        self.assertTrue(writer_ready.wait(5), 'writer initialization stalled')
        intercepted = False
        def read_with_interleave(post_id, version=None):
            nonlocal intercepted
            post = read(post_id, version)
            if not intercepted:
                intercepted = True
                attempted.set()
                self.assertTrue(attempt_finished.wait(5), 'writer attempt stalled')
            return post
        try:
            with patch.object(self.store, 'get', side_effect=read_with_interleave):
                self.manager.verify_visual(self.post['id'], confirmed=True)
        finally:
            retry.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(read(self.post['id'])['version'], 2)
        self.assertFalse(read(self.post['id'])['visual_verified'], outcome)

    def test_destination_pinned_bot_cannot_silently_change_during_review(self):
        self.store.update(self.post['id'], destination={'chat_id': '@fictional', 'bot_id': 999})
        with self.assertRaises(delivery.DeliveryError):
            self.manager.review(self.post['id'])
        self.assertFalse(any(c[0] == 'sendRichMessage' for c in self.api.calls))
        # Смену бота человек/агент отражает явной новой версией назначения.
        post = self.store.update(self.post['id'], destination={'chat_id': '@fictional', 'bot_id': 101})
        operation = self.manager.review(post['id'])
        self.assertEqual(operation['snapshot']['destination']['bot_id'], 101)

    def test_prepared_new_publications_are_bound_to_previous_publication(self):
        for already_published in (False, True):
            with self.subTest(already_published=already_published):
                post = self.store.create('Отдельно согласуемая публикация', [self.heading, {'type': 'paragraph', 'text': 'Точный текст'}], {'chat_id': '@fictional'})
                if already_published:
                    initial = self.manager.review(post['id'])
                    self.manager.execute(initial['id'], initial['fingerprint'])
                first = self.manager.review(post['id'], new_publication=True)
                stale = self.manager.review(post['id'], new_publication=True)
                self.manager.execute(first['id'], first['fingerprint'])
                sent = sum(c[0] == 'sendRichMessage' for c in self.api.calls)
                with self.assertRaises(delivery.DeliveryError):
                    self.manager.execute(stale['id'], stale['fingerprint'])
                self.assertEqual(sum(c[0] == 'sendRichMessage' for c in self.api.calls), sent)
                # Новое отдельное разрешение после смены публикации остаётся допустимым.
                fresh = self.manager.review(post['id'], new_publication=True)
                self.manager.execute(fresh['id'], fresh['fingerprint'])
                self.assertEqual(sum(c[0] == 'sendRichMessage' for c in self.api.calls), sent + 1)

    def test_edit_response_with_foreign_message_id_remains_unknown(self):
        initial = self.manager.review(self.post['id'])
        self.manager.execute(initial['id'], initial['fingerprint'])
        self.store.update(self.post['id'], blocks=[self.heading, {'type': 'paragraph', 'text': 'Согласованная правка'}])
        edit = self.manager.review(self.post['id'], action='edit')
        original = self.api.call
        def foreign_message(method, payload=None, files=None):
            if method == 'editMessageText':
                self.api.calls.append((method, payload, files))
                return {'message_id': 999, 'chat': {'id': -100123}}
            return original(method, payload, files)
        self.api.call = foreign_message
        with self.assertRaises(delivery.DeliveryError):
            self.manager.execute(edit['id'], edit['fingerprint'])
        self.assertEqual(self.manager.operation(edit['id'])['status'], 'unknown')
        self.assertEqual(self.store.get(self.post['id'])['publication']['message_id'], 77)
        request = next(c for c in self.api.calls if c[0] == 'editMessageText')
        self.assertEqual(request[1]['message_id'], 77)
        self.assertFalse((self.root / 'data' / 'receipts' / (edit['id'] + '.json')).exists())
        count = len(self.api.calls)
        with self.assertRaises(delivery.DeliveryError):
            self.manager.execute(edit['id'], edit['fingerprint'])
        self.assertEqual(len(self.api.calls), count)

    def test_cli_image_source_and_dimensions_survive_versions_and_project_move(self):
        import shutil
        import struct
        import zlib
        from core import validate_media

        def chunk(kind, payload):
            return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)
        png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(b'\x00\x10\x20\x30')) + chunk(b'IEND', b''))
        with tempfile.TemporaryDirectory() as external, tempfile.TemporaryDirectory() as moved_parent:
            original = Path(external) / 'Внешняя картинка с пробелами.png'
            original.write_bytes(png)
            expected_source = {'kind': 'file', 'value': str(original), 'original_name': original.name}
            output = io.StringIO()
            with patch('tool.load_api') as loader, contextlib.redirect_stdout(output):
                self.assertEqual(tool.main(['--root', str(self.root), 'create', '--title', 'Импорт с происхождением', '--text', 'Без переписывания', '--image', str(original)]), 0)
                created = json.loads(output.getvalue())
                first_image = next(b for b in created['blocks'] if b['type'] == 'image')
                self.assertEqual(first_image.get('source'), expected_source)
                self.assertEqual((first_image['width'], first_image['height']), (1, 1))
                output.seek(0)
                output.truncate()
                self.assertEqual(tool.main(['--root', str(self.root), 'update', created['id'], '--image', str(original)]), 0)
                updated = json.loads(output.getvalue())
                self.assertEqual(updated['version'], 2)
                updated_images = [b for b in updated['blocks'] if b['type'] == 'image']
                self.assertEqual(updated_images[0]['source'], expected_source)
                self.assertEqual(updated_images[1], first_image)
                self.assertEqual(original.read_bytes(), png)
                self.assertNotEqual(updated_images[0]['path'], first_image['path'])
                loader.assert_not_called()
            reopened = Store(self.root)
            try:
                for version in (1, 2):
                    saved = reopened.get(created['id'], version)
                    exported = json.loads((self.root / 'posts' / created['id'] / f'v{version:04d}.json').read_text(encoding='utf-8'))
                    for image in [block for block in saved['blocks'] if block['type'] == 'image']:
                        self.assertEqual(image['source'], expected_source)
                        self.assertEqual((image['width'], image['height']), (1, 1))
                        self.assertEqual((self.root / image['path']).read_bytes(), png)
                    self.assertEqual(exported['blocks'], saved['blocks'])
            finally:
                reopened.conn.close()
            self.store.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            moved = Path(moved_parent) / 'Перенесённый проект'
            shutil.copytree(self.root, moved)
            original.unlink()  # Перенос не должен зависеть от существования исходника.
            portable = Store(moved)
            try:
                saved = portable.get(created['id'])
                validate_media(moved, saved)
                saved_image = next(b for b in saved['blocks'] if b['type'] == 'image')
                self.assertEqual(saved_image['source'], expected_source)
                self.assertEqual((moved / saved_image['path']).read_bytes(), png)
            finally:
                portable.conn.close()


if __name__ == '__main__':
    unittest.main()
