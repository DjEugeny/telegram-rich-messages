"""Автономные вымышленные Rich-примеры; не изменяют существующие записи."""

import base64
from pathlib import Path
import sqlite3
import struct
import tempfile
import uuid
import zlib

from core import Store, import_image, import_video


def _png(width, height, variant):
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)

    palette = [(204, 215, 225), (224, 207, 187), (199, 215, 195), (215, 202, 218), (228, 213, 173), (192, 214, 215)]
    background = palette[variant % len(palette)]
    foreground = tuple(max(0, channel - 65) for channel in background)
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            shape = width // 5 < x < width * 4 // 5 and height // 4 < y < height * 3 // 4
            rows.extend(foreground if shape and (x + y + variant * 19) % 90 < 60 else background)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')


def create_demo_media(root):
    """Возвращает четыре текущих черновика; повтор не добавляет файлы/версии."""
    store = Store(root)
    result = []
    try:
        for index, number in enumerate((884, 890, 891, 895)):
            post_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'local:telegram-rich-media-demo-v1:{number}'))
            try:
                result.append(store.get(post_id))
                continue
            except KeyError:
                pass
            with tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                def picture(width, height, variant):
                    name = f'portrait-{variant}.png'
                    source = directory / name
                    source.write_bytes(_png(width, height, variant))
                    item = import_image(root, source)
                    item['source'] = {'kind': 'generated', 'value': 'stdlib rich-media demo v1', 'original_name': name}
                    return dict(item, type='image', alt=f'Вымышленная геометрическая иллюстрация {variant + 1}')

                if index == 0:
                    media = {'type': 'media_group', 'layout': 'collage', 'items': [picture(320, 400, i) for i in range(6)]}
                    title = 'Вымышленный пример: шесть портретов'
                elif index == 1:
                    media = picture(300, 400, 6)
                    title = 'Вымышленный пример: одиночный портрет'
                elif index == 2:
                    source = directory / 'synthetic-avc.mp4'
                    source.write_bytes(base64.b64decode((Path(__file__).parent / 'examples' / 'fixtures' / 'avc-96x160.mp4.base64').read_text(encoding='ascii')))
                    video = import_video(root, source)
                    video['source'] = {'kind': 'generated', 'value': 'bundled synthetic AVC fixture', 'original_name': source.name}
                    media = {'type': 'media_group', 'layout': 'collage', 'items': [dict(video, type='video', alt='Синтетическое немое видео, одна секунда')] + [picture(225, 400, i + 7) for i in range(3)]}
                    title = 'Вымышленный пример: видео и три изображения'
                else:
                    media = picture(320, 400, 10)
                    title = 'Вымышленный пример: портрет и цитата'
            blocks = [media, {'type': 'heading', 'level': 2, 'text': title},
                      {'type': 'paragraph', 'text': 'Это вымышленный локальный черновик. Иллюстрации и текст созданы для проверки оформления.'}]
            if index == 1:
                blocks.append({'type': 'paragraph', 'text': [{'type': 'underline', 'text': 'Пропорция 3:4 сохранена без обрезки.'}]})
            if index in (2, 3):
                blocks.append({'type': 'quote', 'text': 'У вымышленной мастерской появился новый эскиз.\n**Сначала посмотрим**, затем обсудим детали.'})
            if index == 2:
                blocks.append({'type': 'paragraph', 'text': ['Unicode: 👩🏽‍💻 ✨ ❤️. Демонстрационный custom ID: ', {'type': 'custom_emoji', 'text': '⭐', 'custom_emoji_id': '1234567890123456789'}, '. Его доступность в Telegram не проверялась.']})
            try:
                result.append(store._save(post_id, title, blocks, {}, True, 'generated-rich-media-demo-v1'))
            except sqlite3.IntegrityError:
                result.append(store.get(post_id))
        return result
    finally:
        store.conn.close()
