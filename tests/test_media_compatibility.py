"""Реалистичные медиа без внешних зависимостей и без публикации."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from core import Store, _image_info, import_media, validate_media
from media import mp4_info
import tool


FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(name):
    return base64.b64decode((FIXTURES / name).read_text())


def segment(marker, data):
    return bytes((255, marker)) + struct.pack('>H', len(data) + 2) + data


def progressive(scans, width=8, restart=0):
    # DC category 0: код 0; AC: 01 -> 00, EOB -> 01, EOB1 -> 10.
    tables = b'\0\1' + b'\0' * 15 + b'\0'
    tables += b'\x10\0\3' + b'\0' * 14 + b'\x01\0\x10'
    data = b'\xff\xd8' + segment(0xdb, b'\0' + b'\1' * 64)
    data += segment(0xc2, struct.pack('>BHHB', 8, 8, width, 1) + b'\1\x11\0')
    data += segment(0xc4, tables)
    if restart:
        data += segment(0xdd, struct.pack('>H', restart))
    for spectral, entropy in scans:
        data += segment(0xda, b'\1\1\0' + spectral) + entropy
    return data + b'\xff\xd9'


class MediaCompatibilityTests(unittest.TestCase):
    def test_encoder_generated_progressive_files_import_and_revalidate_unchanged(self):
        cases = [('progressive-rgb-65x39.jpg.base64', (65, 39)),
                 ('progressive-gray-17x19.jpg.base64', (17, 19))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'service'
            leaves = []
            for name, size in cases:
                raw = fixture(name)
                self.assertEqual(_image_info(raw), ('image/jpeg', 'jpg', *size))
                source = Path(directory) / name.removesuffix('.base64')
                source.write_bytes(raw)
                item = import_media(root, source)
                self.assertEqual(source.read_bytes(), raw)
                self.assertEqual((root / item['path']).read_bytes(), raw)
                leaves.append(dict(item, type='image', alt='Синтетический тест'))
            store = Store(root)
            try:
                post = store.create('Мои фото', [{'type': 'media_group', 'layout': 'collage', 'items': leaves}])
                self.assertEqual(len(validate_media(root, post)), 2)
            finally:
                store.conn.close()

    def test_progressive_spectral_refinement_eob_and_restart(self):
        scans = [(b'\0\0\1', b'\x7f'), (b'\1\x3f\1', b'\x2f'),
                 (b'\0\0\x10', b'\xff\0'), (b'\1\x3f\x10', b'\x37')]
        self.assertEqual(_image_info(progressive(scans))[2:], (8, 8))
        # Два нулевых блока, EOB run=2 в AC, а также restart с reset EOB.
        self.assertEqual(_image_info(progressive([(b'\0\0\0', b'\x3f'),
                                                  (b'\1\x3f\0', b'\x9f')], width=16))[2:], (16, 8))
        self.assertEqual(_image_info(progressive([(b'\0\0\0', b'\x7f\xff\xd0\x7f'),
                                                  (b'\1\x3f\0', b'\x7f\xff\xd0\x7f')],
                                                 width=16, restart=1))[2:], (16, 8))

    def test_progressive_rejects_damage_and_impossible_scan_sequences(self):
        dc = (b'\0\0\0', b'\x7f')
        invalid = [
            progressive([(b'\1\x3f\0', b'\x7f')]),  # AC до DC
            progressive([dc, dc]),  # Повтор первого прохода
            progressive([dc, (b'\1\x3f\x10', b'\x7f')]),  # Уточнение без первого AC
            progressive([dc, (b'\1\x3f\0', b'\x9f')]),  # EOB за границей кадра
            progressive([dc, (b'\1\x3f\0', b'\x40')]),  # Padding не all-ones
            progressive([dc, (b'\x3f\1\0', b'\x7f')]),  # Неверная полоса
            progressive([dc, (b'\1\x3f\0', b'\x7f\0')]),  # Лишний entropy byte
            progressive([(b'\0\0\0', b'\x7f\xff\xd1\x7f')], width=16, restart=1),
        ]
        raw = fixture('progressive-rgb-65x39.jpg.base64')
        invalid += [raw[:-2], raw[:-50] + b'\xff\xd9', raw + b'junk', raw.replace(b'\xff\xc2', b'\xff\xca', 1)]
        for index, raw in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(ValueError):
                _image_info(raw)

    def test_mp4_tolerates_only_one_rounding_tick_with_submillisecond_bound(self):
        raw = fixture('avc-96x160.mp4.base64')
        offset = raw.index(b'mdhd') + 4
        duration = struct.unpack_from('>I', raw, offset + 16)[0]
        expected = mp4_info(raw)
        for delta in (-1, 1):
            data = bytearray(raw)
            struct.pack_into('>I', data, offset + 16, duration + delta)
            result = mp4_info(bytes(data))
            self.assertEqual(result[:2], expected[:2])
            self.assertAlmostEqual(result[2], expected[2], places=3)
        for delta in (-2, 2, 1000):
            data = bytearray(raw)
            struct.pack_into('>I', data, offset + 16, duration + delta)
            with self.subTest(delta=delta), self.assertRaisesRegex(ValueError, 'sample timing'):
                mp4_info(bytes(data))
        data = bytearray(raw)
        struct.pack_into('>II', data, offset + 12, 1, duration + 1)
        with self.assertRaisesRegex(ValueError, 'sample timing'):
            mp4_info(bytes(data))

    def test_cli_reports_safe_media_diagnostic_and_saves_no_post(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'PRIVATE_NAME.jpg'
            source.write_bytes(fixture('progressive-rgb-65x39.jpg.base64')[:-50])
            root = Path(directory) / 'service'
            with patch('tool.load_api', side_effect=AssertionError('Без токена')):
                with redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
                    self.assertEqual(tool.main(['--root', str(root), 'create', '--title', 'Тест',
                                                '--text', 'Текст', '--image', str(source)]), 2)
            self.assertIn('JPEG:', errors.getvalue())
            self.assertIn('Черновик не сохранён', errors.getvalue())
            self.assertNotIn('PRIVATE_NAME', errors.getvalue())
            store = Store(root)
            try:
                self.assertEqual(store.list(), [])
            finally:
                store.conn.close()
