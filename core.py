"""Каноническое локальное состояние и безопасная компиляция постов.

Только стандартная библиотека. Модуль не обращается к Telegram и не читает секреты.
"""

from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid
import zlib
from media import MAX_VIDEO_BYTES, mp4_info


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


def load_text(source, from_file=False):
    """Строка — буквальный текст; Path или from_file=True — явный UTF-8 файл."""
    if isinstance(source, os.PathLike) or from_file:
        path = Path(source).expanduser()
        _check_source_path(path)
        try:
            with path.open("rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("Текстовый файл больше 2 MB")
            return _text(raw.decode("utf-8"))
        except (OSError, UnicodeError):
            raise ValueError("Нужен доступный текстовый файл UTF-8") from None
    return _text(source)


def create_demo(root):
    """Локальная вымышленная демонстрация; повторный вызов не создаёт копий."""
    store = Store(root)
    demo_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "local:telegram-rich-messages-demo-v1"))
    try:
        return store.get(demo_id)
    except KeyError:
        pass
    finally:
        store.conn.close()

    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)

    width, height = 640, 320
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):
            accent = 100 < x < 540 and 100 < y < 220
            pixels.extend((216, 233, 247) if accent else (242, 245, 249))
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(pixels))) + chunk(b"IEND", b"")
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "demo.png"
        source.write_bytes(data)
        image = import_image(root, source)
    image["source"] = {"kind": "generated", "value": "stdlib demo", "original_name": "demo.png"}
    blocks = [
        {"type": "heading", "level": 2, "text": "Пробный пост"},
        {"type": "paragraph", "text": "Это **вымышленный пример**. Его можно изменить перед публикацией."},
        dict(image, type="image", alt="Светлая демонстрационная иллюстрация с голубым прямоугольником"),
        {"type": "list", "items": ["Подготовить материал", "Проверить оформление", "Выбрать чат и бота"]},
        {"type": "details", "summary": "Что внутри", "blocks": [{"type": "paragraph", "text": "Локальный черновик. Сообщение ещё не отправлено."}]},
        {"type": "button", "text": "Документация Telegram", "url": "https://core.telegram.org/bots/api"},
    ]
    store = Store(root)
    try:
        # Несколько процессов demo могут стартовать одновременно; уникальный ID защищает БД.
        try:
            return store._save(demo_id, "Демонстрационный пост", blocks, {}, True, "generated-demo-v1")
        except sqlite3.IntegrityError:
            return store.get(demo_id)
    finally:
        store.conn.close()


def _check_source_path(path):
    for candidate in (path, path.resolve()):
        if candidate.name.lower().endswith((".env", ".pem")) or "secrets" in candidate.parts:
            raise ValueError("Чтение хранилища секретов запрещено")


MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _dimensions(width, height):
    if min(width, height) < 1 or width + height > 10000 or max(width, height) / min(width, height) > 20:
        raise ValueError("Размер изображения: сумма сторон до 10000, пропорции до 20:1")


def _png(data):
    cursor, compressed, seen, palette = 8, bytearray(), [], None
    width = height = channels = bits = color = None
    while cursor < len(data):
        if cursor + 12 > len(data):
            raise ValueError("PNG: оборванный chunk")
        length = struct.unpack_from(">I", data, cursor)[0]
        kind = data[cursor + 4:cursor + 8]
        if length > MAX_IMAGE_BYTES or cursor + length + 12 > len(data):
            raise ValueError("PNG: неверная длина chunk")
        value = data[cursor + 8:cursor + 8 + length]
        crc = struct.unpack_from(">I", data, cursor + length + 8)[0]
        if zlib.crc32(kind + value) & 0xffffffff != crc:
            raise ValueError("PNG: повреждена контрольная сумма")
        if not seen and kind != b"IHDR":
            raise ValueError("PNG: отсутствует IHDR")
        if kind == b"IHDR":
            if seen or length != 13:
                raise ValueError("PNG: неверный IHDR")
            width, height, bits, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", value)
            _dimensions(width, height)
            allowed = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if color not in allowed or bits not in allowed[color] or compression or filtering or interlace:
                raise ValueError("PNG: неподдерживаемый формат; v1 принимает PNG без interlace")
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
        elif kind == b"PLTE":
            if palette is not None or b"IDAT" in seen or not length or length % 3 or length > 768 or color in (0, 4):
                raise ValueError("PNG: неверная палитра")
            palette = length // 3
        elif kind == b"IDAT":
            if b"IDAT" in seen and seen[-1] != b"IDAT" or color == 3 and palette is None:
                raise ValueError("PNG: неверный порядок IDAT")
            compressed.extend(value)
        elif kind == b"IEND":
            if length or b"IDAT" not in seen or cursor + 12 != len(data):
                raise ValueError("PNG: неверный конец")
        elif not kind or not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind) or not kind[0] & 32:
            raise ValueError("PNG: неизвестный обязательный chunk")
        seen.append(kind)
        cursor += length + 12
    if not seen or seen[-1] != b"IEND":
        raise ValueError("PNG: отсутствует IEND")
    stride = (width * channels * bits + 7) // 8
    expected = (stride + 1) * height
    if expected > 100 * 1024 * 1024:
        raise ValueError("PNG: слишком большой распакованный размер")
    decoder = zlib.decompressobj()
    try:
        pixels = decoder.decompress(bytes(compressed), expected + 1)
    except zlib.error as error:
        raise ValueError("PNG: повреждены пиксели") from error
    if len(pixels) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("PNG: неверный поток пикселей")
    previous = bytearray(stride)
    step = max(1, (channels * bits + 7) // 8)
    for offset in range(0, expected, stride + 1):
        filter_kind = pixels[offset]
        if filter_kind > 4:
            raise ValueError("PNG: неверный фильтр")
        row = bytearray(pixels[offset + 1:offset + 1 + stride])
        for index in range(stride):
            left, up = (row[index - step] if index >= step else 0), previous[index]
            upper_left = previous[index - step] if index >= step else 0
            if filter_kind == 1:
                predictor = left
            elif filter_kind == 2:
                predictor = up
            elif filter_kind == 3:
                predictor = (left + up) // 2
            elif filter_kind == 4:
                p = left + up - upper_left
                distances = (abs(p - left), abs(p - up), abs(p - upper_left))
                predictor = (left, up, upper_left)[distances.index(min(distances))]
            else:
                predictor = 0
            row[index] = (row[index] + predictor) & 255
        if color == 3:
            mask = (1 << bits) - 1
            if any((row[(index * bits) // 8] >> (8 - bits - (index * bits) % 8)) & mask >= palette for index in range(width)):
                raise ValueError("PNG: пиксель за пределами палитры")
        previous = row
    return width, height


def _jpeg_scan(raw, frame, selected, huffman, restart_interval):
    """Проверяет все Huffman-коды и число DCT-блоков baseline JPEG."""
    components = frame[2]
    maximum_h = max(item[1] for item in components.values())
    maximum_v = max(item[2] for item in components.values())
    if len(selected) == 1:
        _, component, _ = selected[0]
        columns = (frame[0] * component[1] + 8 * maximum_h - 1) // (8 * maximum_h)
        rows = (frame[1] * component[2] + 8 * maximum_v - 1) // (8 * maximum_v)
        blocks = [(selected[0][2], 1)]
    else:
        columns = (frame[0] + 8 * maximum_h - 1) // (8 * maximum_h)
        rows = (frame[1] + 8 * maximum_v - 1) // (8 * maximum_v)
        blocks = [(tables, component[1] * component[2]) for _, component, tables in selected]
    chunks, current, cursor, restart = [], bytearray(), 0, 0
    while cursor < len(raw):
        value = raw[cursor]
        cursor += 1
        if value != 255:
            current.append(value)
            continue
        if cursor >= len(raw):
            raise ValueError("JPEG: оборванный entropy stream")
        marker = raw[cursor]
        cursor += 1
        if marker == 0:
            current.append(255)
        elif 0xD0 <= marker <= 0xD7 and restart_interval and marker == 0xD0 + restart % 8:
            chunks.append(bytes(current))
            current.clear()
            restart += 1
        else:
            raise ValueError("JPEG: неверный restart marker")
    chunks.append(bytes(current))
    total = columns * rows
    expected_chunks = (total + restart_interval - 1) // restart_interval if restart_interval else 1
    if len(chunks) != expected_chunks:
        raise ValueError("JPEG: неверное число restart markers")
    completed = 0
    for chunk in chunks:
        bit_offset = 0

        def read_bits(count):
            nonlocal bit_offset
            if bit_offset + count > len(chunk) * 8:
                raise ValueError("JPEG: оборваны коэффициенты DCT")
            value = 0
            for _ in range(count):
                value = (value << 1) | ((chunk[bit_offset // 8] >> (7 - bit_offset % 8)) & 1)
                bit_offset += 1
            return value

        def symbol(table):
            value = 0
            for length in range(1, 17):
                value = (value << 1) | read_bits(1)
                if (length, value) in table:
                    return table[(length, value)]
            raise ValueError("JPEG: неверный Huffman-код")

        count = min(restart_interval or total, total - completed)
        for _ in range(count):
            for tables, block_count in blocks:
                for _ in range(block_count):
                    dc = symbol(huffman[(0, tables >> 4)])
                    if dc > 11:
                        raise ValueError("JPEG: неверная DC category")
                    read_bits(dc)
                    coefficient = 1
                    while coefficient < 64:
                        ac = symbol(huffman[(1, tables & 15)])
                        if ac == 0:
                            break
                        run, size = ac >> 4, ac & 15
                        if ac == 0xF0:
                            coefficient += 16
                            if coefficient > 64:
                                raise ValueError("JPEG: неверный zero run")
                            continue
                        coefficient += run
                        if not 1 <= size <= 10 or coefficient >= 64:
                            raise ValueError("JPEG: неверная AC category")
                        read_bits(size)
                        coefficient += 1
        remaining = len(chunk) * 8 - bit_offset
        if remaining >= 8 or read_bits(remaining) != (1 << remaining) - 1:
            raise ValueError("JPEG: лишние данные или повреждённый padding")
        completed += count


def _jpeg(data):
    from jpeg_progressive import Progressive

    cursor, frame, scans, quant, huffman = 2, None, 0, set(), {}
    restart_interval, scanned = 0, set()
    progressive = None
    while cursor < len(data):
        if data[cursor] != 255:
            raise ValueError("JPEG: отсутствует маркер")
        while cursor < len(data) and data[cursor] == 255:
            cursor += 1
        if cursor >= len(data):
            raise ValueError("JPEG: оборванный маркер")
        marker = data[cursor]
        cursor += 1
        if marker == 0xD9:
            if cursor != len(data) or frame is None or not scans or scanned != set(frame[2]):
                raise ValueError("JPEG: неверный конец")
            return frame[0], frame[1]
        if marker in (0, 0xD8) or 0xD0 <= marker <= 0xD7 or cursor + 2 > len(data):
            raise ValueError("JPEG: неверный маркер")
        length = struct.unpack_from(">H", data, cursor)[0]
        if length < 2 or cursor + length > len(data):
            raise ValueError("JPEG: оборванный сегмент")
        value = data[cursor + 2:cursor + length]
        cursor += length
        if marker in (0xC0, 0xC2):
            if frame is not None or len(value) < 6:
                raise ValueError("JPEG: неверный кадр")
            precision, height, width, count = struct.unpack_from(">BHHB", value)
            _dimensions(width, height)
            if precision != 8 or count not in (1, 3) or len(value) != 6 + 3 * count:
                raise ValueError("JPEG: поддерживаются 8-bit grayscale/RGB baseline и progressive")
            components = {}
            for offset in range(6, len(value), 3):
                identifier, sampling, table = value[offset:offset + 3]
                if identifier in components or not 1 <= sampling >> 4 <= 4 or not 1 <= sampling & 15 <= 4 or table > 3:
                    raise ValueError("JPEG: неверные компоненты")
                components[identifier] = (table, sampling >> 4, sampling & 15)
            frame = (width, height, components, marker)
            if marker == 0xC2:
                progressive = Progressive(frame)
        elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xCC):
            raise ValueError("JPEG: неподдерживаемое кодирование")
        elif marker == 0xDB:
            index = 0
            while index < len(value):
                info = value[index]
                size = 64 * (1 + (info >> 4))
                if info >> 4 > 1 or info & 15 > 3 or index + 1 + size > len(value):
                    raise ValueError("JPEG: неверная таблица квантования")
                quant_values = value[index + 1:index + 1 + size]
                if not all(quant_values if size == 64 else struct.unpack(">64H", quant_values)):
                    raise ValueError("JPEG: нулевая таблица квантования")
                quant.add(info & 15)
                index += 1 + size
        elif marker == 0xC4:
            index = 0
            while index < len(value):
                if index + 17 > len(value):
                    raise ValueError("JPEG: оборвана таблица Хаффмана")
                info = value[index]
                counts = value[index + 1:index + 17]
                total = sum(counts)
                slots = 1
                for count in counts:
                    slots = slots * 2 - count
                    # JPEG резервирует all-ones код под padding: полное дерево
                    # недопустимо даже без переполнения числа кодов.
                    if slots <= 0:
                        raise ValueError("JPEG: переполнена или замкнута таблица Хаффмана")
                if info >> 4 > 1 or info & 15 > 3 or not 0 < total <= 256 or index + 17 + total > len(value):
                    raise ValueError("JPEG: неверная таблица Хаффмана")
                codes, code, symbol_index = {}, 0, index + 17
                for code_length, count in enumerate(counts, 1):
                    for _ in range(count):
                        codes[(code_length, code)] = value[symbol_index]
                        symbol_index += 1
                        code += 1
                    code <<= 1
                huffman[(info >> 4, info & 15)] = codes
                index += 17 + total
        elif marker == 0xDD:
            if len(value) != 2:
                raise ValueError("JPEG: неверный DRI")
            restart_interval = struct.unpack(">H", value)[0]
        elif marker == 0xDA:
            if frame is None or not value or len(value) != 1 + 2 * value[0] + 3 or not 1 <= value[0] <= len(frame[2]):
                raise ValueError("JPEG: неверный scan")
            selected = []
            for offset in range(1, len(value) - 3, 2):
                component, tables = value[offset:offset + 2]
                if (component not in frame[2] or frame[2][component][0] not in quant
                        or (not progressive and component in scanned)
                        or any(item[0] == component for item in selected)
                        or tables >> 4 > 3 or tables & 15 > 3):
                    raise ValueError("JPEG: отсутствует таблица компонента")
                if not progressive and ((0, tables >> 4) not in huffman or (1, tables & 15) not in huffman):
                    raise ValueError("JPEG: отсутствует таблица Хаффмана")
                selected.append((component, frame[2][component], tables))
                if not progressive or value[-3] == 0:
                    scanned.add(component)
            if not progressive and value[-3:] != b"\0\x3f\0":
                raise ValueError("JPEG: неверный baseline scan")
            start = cursor
            while cursor < len(data):
                if data[cursor] != 255:
                    cursor += 1
                    continue
                if cursor + 1 >= len(data):
                    raise ValueError("JPEG: оборван entropy stream")
                following = data[cursor + 1]
                if following == 0 or 0xD0 <= following <= 0xD7:
                    cursor += 2
                    continue
                break
            if cursor == start:
                raise ValueError("JPEG: пустой entropy stream")
            if progressive:
                progressive.scan(data[start:cursor], selected, huffman, restart_interval, value[-3:])
            else:
                _jpeg_scan(data[start:cursor], frame, selected, huffman, restart_interval)
            scans += 1
    raise ValueError("JPEG: отсутствует EOI")


def _image_info(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Изображение пустое или больше 10 MB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png", *_png(data)
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg", "jpg", *_jpeg(data)
    raise ValueError("Принимаются только PNG и JPEG, HTML-страницы не подходят")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise ValueError("Укажите прямой URL изображения без перенаправления")


def import_image(root, source):
    """Совместимый импорт только PNG/JPEG."""
    return _import_media(root, source, image_only=True)


def import_media(root, source):
    """Определяет PNG/JPEG/MP4 по содержимому и сохраняет уникальную копию."""
    return _import_media(root, source)


def import_video(root, source):
    """Импорт только MP4 AVC/AAC."""
    return _import_media(root, source, video_only=True)


class MediaValidationError(ValueError):
    """Безопасная диагностика валидатора: без путей, URL и содержимого файла."""


def _media_info(data):
    try:
        if len(data) >= 8 and data[4:8] == b"ftyp":
            width, height, duration = mp4_info(data)
            return {"mime": "video/mp4", "extension": "mp4", "width": width, "height": height, "duration": duration}
        mime, extension, width, height = _image_info(data)
        return {"mime": mime, "extension": extension, "width": width, "height": height}
    except ValueError as error:
        raise MediaValidationError(str(error)) from None


def _import_media(root, source, image_only=False, video_only=False):
    """Копирует доступный путь или прямой URL; оригинал остаётся нетронутым."""
    if not isinstance(source, (str, os.PathLike)):
        raise ValueError("Нужен доступный путь к вложению или прямой HTTP(S) URL")
    source_string = os.fspath(source)
    if isinstance(source, str) and urlsplit(source).scheme.lower() in ("http", "https"):
        target = _url(source)
        try:
            with build_opener(_NoRedirect).open(Request(target, headers={"User-Agent": "TelegramRichMessages/1"}), timeout=15) as response:
                data = response.read((MAX_IMAGE_BYTES if image_only else MAX_VIDEO_BYTES) + 1)
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("Не удалось получить изображение по указанному URL") from None
        origin = {"kind": "url", "value": source_string, "original_name": Path(urlsplit(source_string).path).name}
    else:
        path = Path(source).expanduser()
        _check_source_path(path)
        system_aliases = {Path("/var"), Path("/tmp"), Path("/etc")} if sys.platform == "darwin" else set()
        if path.is_symlink() or any(parent.is_symlink() and parent not in system_aliases for parent in path.parents):
            raise ValueError("Символические ссылки источника запрещены")
        try:
            with path.open("rb") as stream:
                data = stream.read((MAX_IMAGE_BYTES if image_only else MAX_VIDEO_BYTES) + 1)
        except OSError:
            raise ValueError("Файл недоступен; нужен доступный локальный путь к вложению") from None
        origin = {"kind": "file", "value": str(path.absolute()), "original_name": path.name}
    metadata = _media_info(data)
    if image_only and metadata["mime"] == "video/mp4":
        raise MediaValidationError("Нужно изображение PNG или JPEG")
    if video_only and metadata["mime"] != "video/mp4":
        raise MediaValidationError("Нужно видео MP4 H.264/AVC")
    media_dir = Path(root).expanduser().resolve() / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    if media_dir.is_symlink():
        raise ValueError("media/ не должна быть символической ссылкой")
    prefix = "video" if metadata["mime"] == "video/mp4" else "image"
    name = f"{prefix}-{uuid.uuid4().hex}.{metadata.pop('extension')}"
    target = media_dir / name
    # link публикует полностью записанный файл атомарно и никогда не заменяет имя.
    descriptor, temporary = tempfile.mkstemp(prefix=".import-", dir=media_dir)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        os.unlink(temporary)
    return {"path": "media/" + name, "sha256": hashlib.sha256(data).hexdigest(), **metadata, "source": origin}


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _text(value, label="Текст"):
    if not isinstance(value, str) or any(ord(char) < 32 and char not in "\n\t\r" for char in value):
        raise ValueError(f"{label}: требуется строка без управляющих символов")
    return value


def _url(value):
    value = _text(value, "URL")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or any(char.isspace() for char in value) or "\\" in value:
        raise ValueError("Допустим только полный HTTP(S) URL без пароля")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("Неверный порт URL") from error
    return value


def _media_path(value):
    value = _text(value, "Путь изображения")
    if not re.fullmatch(r"media/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.(?:png|jpg|jpeg|mp4)", value, re.I) or any(part in (".", "..") for part in value.split("/")):
        raise ValueError("Изображение должно находиться внутри media/")
    return value


def _md_literal(value):
    escaped = re.sub(r"([\\`*_\[\]()#>|~{}.!+\-=])", r"\\\1", value)
    return escaped.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md_text(value):
    # Rich Markdown сворачивает обычный перевод строки внутри абзаца в пробел.
    return _md_literal(value).replace("\n", "<br>")


_INLINE = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^\s)]+\))")


def _inline(value, depth=1, literal=False, in_link=False):
    if depth > 16:
        raise ValueError("Вложенность inline больше 16")
    if isinstance(value, list):
        if len(value) > 500:
            raise ValueError("Слишком много inline узлов")
        pairs = []
        for node in value:
            if isinstance(node, str):
                pairs.append(_inline(node, depth + 1, literal=literal, in_link=in_link))
                continue
            if not isinstance(node, dict):
                raise ValueError("Inline узел должен быть строкой или объектом")
            kind = node.get("type")
            text = node.get("text")
            if kind in ("text", "custom_emoji"):
                text = _text(text)
                md, rendered = _md_text(text), html.escape(text).replace("\n", "<br>")
                if kind == "custom_emoji":
                    identifier = node.get("custom_emoji_id")
                    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9]{1,32}", identifier) or not text.strip() or len(text) > 64 or "\n" in text or "\r" in text:
                        raise ValueError("Custom emoji: нужны fallback и явный числовой ID")
                    md = f'![{_md_literal(text)}](tg://emoji?id={identifier})'
                    rendered = f'<span class="custom-emoji" role="img" aria-label="{html.escape(text, quote=True)} — приблизительный вид custom emoji">{html.escape(text)}</span>'
            elif kind in ("bold", "italic", "underline", "strikethrough", "code", "link"):
                if kind == "link" and in_link:
                    raise ValueError("Вложенные ссылки запрещены")
                md, rendered = _inline(text, depth + 1, literal=True, in_link=in_link or kind == "link")
                if kind == "link":
                    target = _url(node.get("url"))
                    md = f'[{md}]({target.replace("(", "%28").replace(")", "%29")})'
                    rendered = f'<a href="{html.escape(target, quote=True)}" target="_blank" rel="noopener noreferrer">{rendered}</a>'
                elif kind == "code":
                    # HTML code гарантирует буквальное содержимое даже с backticks.
                    md = f"<code>{html.escape(_inline_plain(text))}</code>"
                    rendered = f"<code>{html.escape(_inline_plain(text))}</code>"
                else:
                    marker, tag = {"bold": ("**", "strong"), "italic": ("*", "em"), "underline": ("<u>", "u"), "strikethrough": ("~~", "s")}[kind]
                    md = f"{marker}{md}{'</u>' if kind == 'underline' else marker}"
                    rendered = f"<{tag}>{rendered}</{tag}>"
            else:
                raise ValueError("Неподдерживаемый inline узел")
            pairs.append((md, rendered))
        return "".join(pair[0] for pair in pairs), "".join(pair[1] for pair in pairs)
    value = _text(value)
    if literal:
        return _md_text(value), html.escape(value).replace("\n", "<br>")
    markdown, rendered = [], []
    cursor = 0
    for match in _INLINE.finditer(value):
        literal = value[cursor:match.start()]
        markdown.append(_md_text(literal))
        rendered.append(html.escape(literal).replace("\n", "<br>"))
        token = match.group()
        if token.startswith("["):
            label, target = token[1:].split("](", 1)
            target = _url(target[:-1])
            markdown.append(f'[{_md_literal(label)}]({target})')
            rendered.append(f'<a href="{html.escape(target, quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(label)}</a>')
        elif token.startswith("`"):
            markdown.append(token)
            rendered.append("<code>" + html.escape(token[1:-1]) + "</code>")
        else:
            size, tag = (2, "strong") if token.startswith("**") else (1, "em")
            inner = token[size:-size]
            markdown.append(token[:size] + _md_literal(inner) + token[-size:])
            rendered.append(f"<{tag}>" + html.escape(inner) + f"</{tag}>")
        cursor = match.end()
    literal = value[cursor:]
    markdown.append(_md_text(literal))
    rendered.append(html.escape(literal).replace("\n", "<br>"))
    return "".join(markdown), "".join(rendered)


def _inline_plain(value):
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, list):
        return "".join(_inline_plain(node if isinstance(node, str) else node["text"]) for node in value)
    raise ValueError("Code: требуется текст")


def compile_post(post):
    """Компиляция Rich Markdown и HTML из одного снимка; неверный ввод отклоняется."""
    if not isinstance(post, dict):
        raise ValueError("Пост должен быть объектом")
    media = []
    counter = [0]

    def render_media(block):
        kind = block.get("type")
        if kind not in ("image", "video"):
            raise ValueError("Группа содержит только image/video")
        path = _media_path(block.get("path"))
        checksum = block.get("sha256")
        expected_mimes = ("video/mp4",) if kind == "video" else ("image/png", "image/jpeg")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum) or block.get("mime") not in expected_mimes:
            raise ValueError("Для медиа нужны корректные SHA256 и MIME")
        if (kind == "video") != path.lower().endswith(".mp4"):
            raise ValueError("Расширение пути не совпадает с типом медиа")
        if kind == "video" and (type(block.get("width")) is not int or type(block.get("height")) is not int or not 1 <= block["width"] <= 8192 or not 1 <= block["height"] <= 8192 or type(block.get("duration")) not in (int, float) or not 0 < block["duration"] < float("inf")):
            raise ValueError("Видео требует размеры и конечную положительную длительность")
        alt = _text(block.get("alt", ""))
        media_type = "video" if kind == "video" else "photo"
        attachment = f"{media_type}_{len(media) + 1}"
        media_id = str(len(media) + 1)
        item = {"id": media_id, "media": {"type": media_type, "media": "attach://" + attachment}, "attachment": attachment, "path": path, "mime": block["mime"], "sha256": checksum}
        item.update({key: block[key] for key in ("width", "height", "duration") if key in block})
        media.append(item)
        md = f'![{_md_literal(alt)}](tg://{media_type}?id={media_id})'
        safe_path, safe_alt = html.escape(path, quote=True), html.escape(alt, quote=True)
        if kind == "video":
            content = f'<video controls preload="metadata" src="{safe_path}" aria-label="{safe_alt}" width="{block["width"]}" height="{block["height"]}"></video><p class="media-error" hidden>Видео не удалось воспроизвести. Откройте исходный файл.</p><a class="media-original" href="{safe_path}" target="_blank" rel="noopener noreferrer">Открыть исходный файл</a>'
        else:
            content = f'<a class="media-original" href="{safe_path}" target="_blank" rel="noopener noreferrer"><img src="{safe_path}" alt="{safe_alt}" loading="lazy"></a>'
        return md, f'<figure class="rich-media-item">{content}</figure>'

    def render(blocks, depth=1):
        if not isinstance(blocks, list) or depth > 16:
            raise ValueError("Неверные блоки или вложенность больше 16")
        md_parts, html_parts = [], []
        previous_kind = None
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError("Блок должен быть объектом")
            counter[0] += 1
            kind = block.get("type")
            if kind in ("heading", "paragraph", "quote"):
                md, rendered = _inline(block.get("text"))
                if kind == "heading":
                    level = block.get("level", 2)
                    if type(level) is not int or not 1 <= level <= 6:
                        raise ValueError("Уровень заголовка от 1 до 6")
                    md, rendered = "#" * level + " " + md, f"<h{level}>{rendered}</h{level}>"
                elif kind == "quote":
                    md = "\n".join("> " + line for line in md.split("\n"))
                    rendered = f"<blockquote>{rendered}</blockquote>"
                else:
                    rendered = f"<p>{rendered}</p>"
            elif kind == "list":
                items = block.get("items")
                if not isinstance(items, list) or not items or type(block.get("ordered", False)) is not bool:
                    raise ValueError("Список должен содержать строки")
                counter[0] += len(items)
                pairs = [_inline(item) for item in items]
                ordered = block.get("ordered", False)
                md_items = []
                for index, pair in enumerate(pairs, 1):
                    prefix = f"{index}. " if ordered else "- "
                    # Продолжение выравнивается по тексту пункта, включая 10. и далее.
                    md_items.append(prefix + pair[0].replace("\n", "\n" + " " * len(prefix)))
                md = "\n".join(md_items)
                tag = "ol" if ordered else "ul"
                rendered = f"<{tag}>" + "".join(f"<li>{pair[1]}</li>" for pair in pairs) + f"</{tag}>"
            elif kind == "details":
                summary_md, summary_html = _inline(block.get("summary"))
                inner_md, inner_html = render(block.get("blocks"), depth + 1)
                md = f"<details><summary>{summary_md}</summary>\n\n{inner_md}\n\n</details>"
                rendered = f"<details><summary>{summary_html}</summary>{inner_html}</details>"
            elif kind == "table":
                headers, rows = block.get("headers"), block.get("rows")
                if not isinstance(headers, list) or not 1 <= len(headers) <= 20 or not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != len(headers) for row in rows):
                    raise ValueError("Таблица: от 1 до 20 колонок, одинаковая длина строк")
                counter[0] += len(rows) + 1
                pairs = [[_inline(cell) for cell in row] for row in [headers] + rows]
                md_rows = ["| " + " | ".join(pair[0].replace("|", "\\|").replace("\n", " ") for pair in row) + " |" for row in pairs]
                md_rows.insert(1, "| " + " | ".join("---" for _ in headers) + " |")
                md = "\n".join(md_rows)
                rendered = '<div class="table-scroll"><table><thead><tr>' + "".join(f"<th>{pair[1]}</th>" for pair in pairs[0]) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{pair[1]}</td>" for pair in row) + "</tr>" for row in pairs[1:]) + "</tbody></table></div>"
            elif kind in ("button", "url-button"):
                target = html.escape(_url(block.get("url")), quote=True)
                text = _text(block.get("text"))
                md = f'<tg-button type="url" url="{target}">{html.escape(text)}</tg-button>'
                rendered = f'<a class="url-button" href="{target}" target="_blank" rel="noopener noreferrer">{html.escape(text)}</a>'
            elif kind in ("image", "video"):
                md, rendered = render_media(block)
            elif kind == "media_group":
                items, layout = block.get("items"), block.get("layout")
                if depth >= 16 or layout not in ("collage", "slideshow") or not isinstance(items, list) or not 2 <= len(items) <= 50 or any(not isinstance(item, dict) for item in items):
                    raise ValueError("Группа: collage/slideshow, от 2 до 50 image/video")
                counter[0] += len(items)
                pairs = [render_media(item) for item in items]
                md = f'<tg-{layout}>\n\n' + "\n\n".join(pair[0] for pair in pairs) + f'\n\n</tg-{layout}>'
                rendered = f'<figure class="rich-media-group" data-layout="{layout}">' + "".join(pair[1] for pair in pairs) + '</figure>'
            else:
                raise ValueError(f"Неподдерживаемый тип блока: {kind!r}")
            if counter[0] > 500 or len(media) > 50:
                raise ValueError("Превышен лимит 500 блоков или 50 медиа")
            if previous_kind == kind == "paragraph":
                # Клиент Telegram рисует соседние paragraph без промежутка.
                # Содержательный пустой абзац сохраняет видимый интервал.
                md_parts.append("\u00a0")
                counter[0] += 1
                if counter[0] > 500:
                    raise ValueError("Превышен лимит 500 блоков или 50 медиа")
            md_parts.append(md)
            html_parts.append(rendered)
            previous_kind = kind
        return "\n\n".join(md_parts), "".join(html_parts)

    markdown, rendered = render(post.get("blocks"))
    if len(markdown.encode("utf-16-le")) // 2 > 32768:
        raise ValueError("Rich Markdown превышает 32768 символов UTF-16")
    return {"markdown": markdown, "html": rendered, "media": media}


def validate_publication_structure(post, compiled=None):
    """Минимум структуры перед доставкой и сводка фактического оформления.

    Черновики остаются свободными. Текстовая публикация требует непустого
    заголовка верхнего уровня блоков; жирный абзац его не заменяет.
    Сводка не оценивает смысл и не добавляет блоки за автора.
    """
    if compiled is None:
        compiled = compile_post(post)
    counts, headings = {}, []
    top_heading = False

    def visit(blocks, depth=0):
        nonlocal top_heading
        for block in blocks:
            kind = block["type"]
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "heading":
                rendered = _inline(block["text"])[1]
                text = html.unescape(re.sub(r"<[^>]*>", "", rendered)).strip()
                if not text:
                    raise ValueError("Rich Markdown: заголовок не может быть пустым.")
                headings.append({"level": block.get("level", 2), "text": text})
                top_heading = top_heading or depth == 0
            elif kind == "details":
                visit(block["blocks"], depth + 1)
            elif kind == "media_group":
                layout = block["layout"]
                counts[layout] = counts.get(layout, 0) + 1
                visit(block["items"], depth + 1)

    visit(post["blocks"])
    media_kinds = {"image", "video", "media_group"}
    if any(block["type"] not in media_kinds for block in post["blocks"]) and not top_heading:
        raise ValueError(
            "Rich Markdown: текстовая публикация требует настоящего блока heading. "
            "Жирный paragraph и название в истории не заменяют заголовок. "
            "Оформи заголовки и остальную структуру по смыслу через --blocks, "
            "покажи новое превью и выполни новый review."
        )
    tags = {"bold": "strong", "italic": "em", "underline": "u",
            "strikethrough": "s", "code": "code"}
    inline = {kind: len(re.findall(r"<" + tag + r"(?:\s|>)", compiled["html"]))
              for kind, tag in tags.items()}
    # Служебные ссылки на оригиналы медиа и кнопки не являются inline-ссылками.
    inline["link"] = len(re.findall(r'<a href=', compiled["html"]))
    inline["custom_emoji"] = compiled["html"].count('class="custom-emoji"')
    return {"format": "rich_markdown", "blocks": counts, "headings": headings,
            "inline": {kind: count for kind, count in inline.items() if count}}


def validate_media(root, post):
    """Проверка перед сохранением/доставкой: путь, SHA256 и содержимое медиа."""
    root = Path(root).expanduser().resolve()
    media = compile_post(post)["media"]
    for item in media:
        path = root / item["path"]
        try:
            path.resolve(strict=True).relative_to(root / "media")
            cursor = path
            while cursor != root:
                if cursor.is_symlink():
                    raise ValueError("Символические ссылки в media/ запрещены")
                cursor = cursor.parent
            with path.open("rb") as stream:
                data = stream.read((MAX_VIDEO_BYTES if item["mime"] == "video/mp4" else MAX_IMAGE_BYTES) + 1)
        except (OSError, ValueError):
            raise ValueError("Медиа отсутствует или выходит за пределы media/") from None
        info = _media_info(data)
        if info["mime"] != item["mime"] or hashlib.sha256(data).hexdigest() != item["sha256"] or any(key in item and item[key] != info.get(key) for key in ("width", "height", "duration")):
            raise ValueError("Медиа изменилось после импорта; нужен повторный review")
    return media


def _validate_destination(destination):
    if not isinstance(destination, dict):
        raise ValueError("Назначение должно быть объектом")
    allowed = {"chat_id", "chat_title", "bot_id", "bot_username", "message_thread_id"}
    if set(destination) - allowed:
        raise ValueError("Неподдерживаемое поле назначения")
    for key, value in destination.items():
        if value is None:
            continue
        if key in ("chat_title", "bot_username"):
            _text(value, key)
        elif key == "chat_id":
            if type(value) is not int and not (isinstance(value, str) and (re.fullmatch(r"-?[0-9]+", value) or re.fullmatch(r"@[A-Za-z0-9_]{5,32}", value))):
                raise ValueError("chat_id должен быть числом или точным @username")
        elif type(value) is not int or value < 1:
            raise ValueError(f"{key}: нужен положительный числовой ID")


class Store:
    """SQLite — канонический источник; номера версий неизменяемы."""

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        for name in ("data", "posts", "media"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.root / "data" / "state.sqlite3", timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY, current_version INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS versions (
                post_id TEXT NOT NULL REFERENCES posts(id), version INTEGER NOT NULL,
                title TEXT NOT NULL, blocks TEXT NOT NULL, destination TEXT NOT NULL,
                created_at TEXT NOT NULL, fingerprint TEXT NOT NULL,
                PRIMARY KEY(post_id, version)
            );
            CREATE TABLE IF NOT EXISTS post_state (
                post_id TEXT PRIMARY KEY REFERENCES posts(id), status TEXT NOT NULL DEFAULT 'draft',
                visual_verified INTEGER NOT NULL DEFAULT 0, publication TEXT
            );
            CREATE TRIGGER IF NOT EXISTS versions_no_update BEFORE UPDATE ON versions
            BEGIN SELECT RAISE(ABORT, 'versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON versions
            BEGIN SELECT RAISE(ABORT, 'versions are immutable'); END;
        """)
        self.conn.commit()

    def create(self, title, blocks, destination=None, source=None):
        post_id = str(uuid.uuid4())
        return self._save(post_id, title, blocks, {} if destination is None else destination, True, source)

    def update(self, post_id, title=None, blocks=None, destination=None):
        current = self.get(post_id)
        return self._save(post_id, current["title"] if title is None else title,
                          current["blocks"] if blocks is None else blocks,
                          current["destination"] if destination is None else destination, False)

    def _save(self, post_id, title, blocks, destination, creating, source=None):
        if not _text(title, "Название").strip() or len(title) > 500:
            raise ValueError("Название: от 1 до 500 символов")
        _validate_destination(destination)
        # JSON round-trip detaches caller-owned lists and rejects non-JSON values.
        snapshot = json.loads(_json({"title": title, "blocks": blocks, "destination": destination}))
        compiled = compile_post(snapshot)
        validate_media(self.root, snapshot)
        stamp = _now()
        fingerprint = hashlib.sha256(_json(snapshot).encode()).hexdigest()
        export_paths = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if creating:
                version = 1
                self.conn.execute("INSERT INTO posts VALUES (?, ?, ?)", (post_id, version, stamp))
                self.conn.execute("INSERT INTO post_state(post_id) VALUES (?)", (post_id,))
            else:
                row = self.conn.execute("SELECT current_version FROM posts WHERE id=?", (post_id,)).fetchone()
                if row is None:
                    raise KeyError(post_id)
                version = row[0] + 1
                self.conn.execute("UPDATE posts SET current_version=? WHERE id=?", (version, post_id))
                self.conn.execute("UPDATE post_state SET visual_verified=0 WHERE post_id=?", (post_id,))
            self.conn.execute("INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (post_id, version, title, _json(blocks), _json(destination), stamp, fingerprint))
            exported = dict(snapshot, id=post_id, version=version, created_at=stamp,
                            fingerprint=fingerprint, source=source)
            base = self.root / "posts" / post_id / f"v{version:04d}"
            for suffix, content in ((".json", _json(exported)), (".md", compiled["markdown"]), (".html", compiled["html"])):
                export_path = base.with_suffix(suffix)
                export_paths.append(export_path)
                _atomic_write(export_path, content.encode("utf-8"))
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            # Откат относится ко всему комплекту версии, а не только к SQLite.
            # Старые зафиксированные версии не входят в export_paths.
            for export_path in reversed(export_paths):
                export_path.unlink(missing_ok=True)
            raise
        return self.get(post_id)

    def get(self, post_id, version=None):
        row = self.conn.execute("""
            SELECT p.id, p.created_at AS post_created_at, v.*, s.status,
                   s.visual_verified, s.publication
            FROM posts p JOIN versions v ON v.post_id=p.id
            JOIN post_state s ON s.post_id=p.id
            WHERE p.id=? AND v.version=COALESCE(?,p.current_version)
        """, (post_id, version)).fetchone()
        if row is None:
            raise KeyError((post_id, version))
        return {"id": row["id"], "version": row["version"], "title": row["title"],
                "blocks": json.loads(row["blocks"]), "destination": json.loads(row["destination"]),
                "created_at": row["post_created_at"], "updated_at": row["created_at"],
                "status": row["status"], "visual_verified": bool(row["visual_verified"]),
                "publication": json.loads(row["publication"]) if row["publication"] else None,
                "fingerprint": row["fingerprint"]}

    def list(self):
        ids = self.conn.execute("SELECT p.id FROM posts p JOIN versions v ON v.post_id=p.id AND v.version=p.current_version ORDER BY v.created_at DESC,p.id").fetchall()
        return [self.get(row["id"]) for row in ids]
