"""Привязка канала по целой ссылке, без ручного извлечения чисел."""

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from delivery import DeliveryError, normalize_chat_reference
import tool


class ChatReferenceTests(unittest.TestCase):
    def test_public_private_links_and_existing_ids(self):
        cases = (
            ('https://t.me/channel_name', '@channel_name'),
            ('t.me/channel_name/42', '@channel_name'),
            ('https://telegram.me/channel_name/42?single', '@channel_name'),
            ('https://t.me/c/3922251090/4', -1003922251090),
            ('https://t.me/c/1/2?t=3', -1000000000001),
            ('https://t.me/c/997852516352/2', -1997852516352),
            (' @channel_name ', '@channel_name'),
            ('-1003922251090', '-1003922251090'),
            (-1003922251090, -1003922251090),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(normalize_chat_reference(value), expected)

    def test_invalid_invite_comment_and_topic_links_never_load_token(self):
        values = (
            'https://t.me/+PRIVATE_INVITE', 'https://t.me/joinchat/PRIVATE_INVITE',
            'https://t.me/c/123/4?comment=9', 'https://t.me/c/123/4?thread=9',
            'https://t.me/c/123/9/4', 'https://t.me/c/0/1',
            'https://t.me/c/997852516353/1', 'https://t.me/c/123/0',
            'https://t.me.evil.example/channel_name',
            'https://name:PRIVATE_PASSWORD@t.me/channel_name',
            'https://t.me/channel_name?start=PRIVATE_VALUE',
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch('tool.load_api', side_effect=AssertionError('Токен запрещён')) as loader:
                for value in values:
                    with self.subTest(value=value), redirect_stderr(io.StringIO()) as stderr:
                        self.assertEqual(tool.main(['--root', directory, 'check', '--chat', value]), 2)
                        self.assertNotIn('PRIVATE_', stderr.getvalue())
                loader.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])
        for value in (True, None, [], {}):
            with self.assertRaises(DeliveryError):
                normalize_chat_reference(value)

    def test_check_private_link_verifies_and_saves_normalized_channel(self):
        class API:
            def __init__(self):
                self.calls = []

            def call(self, method, payload=None, files=None):
                self.calls.append((method, payload))
                if method == 'getMe':
                    return {'id': 123, 'is_bot': True, 'username': 'selected_bot'}
                if method == 'getChat':
                    self.assert_target(payload)
                    return {'id': -1000000000456, 'title': 'Мой канал', 'type': 'channel'}
                if method == 'getChatMember':
                    self.assert_target(payload)
                    return {'status': 'administrator', 'can_post_messages': True}
                raise AssertionError(method)

            @staticmethod
            def assert_target(payload):
                if payload['chat_id'] != -1000000000456:
                    raise AssertionError(payload)

        api = API()
        with tempfile.TemporaryDirectory() as directory:
            with patch('tool.load_api', return_value=api):
                with redirect_stdout(io.StringIO()) as output:
                    code = tool.main(['--root', directory, 'check', '--chat', 'https://t.me/c/456/7'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())['bot_username'], 'selected_bot')
            saved = json.loads((Path(directory) / 'data/settings.json').read_text())
            self.assertEqual(saved['destination']['chat_id'], -1000000000456)
            self.assertEqual([name for name, _ in api.calls], ['getMe', 'getChat', 'getChatMember'])
