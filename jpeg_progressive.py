"""Проверка Huffman progressive JPEG (ITU-T T.81, Annex G), без перекодирования.

Храним только маски ненулевых AC: для проверки уточняющих проходов пиксели,
IDCT и сами значения коэффициентов не нужны. Память и объём работы ограничены.
"""

from array import array


def _fail(message):
    raise ValueError('JPEG: ' + message)


def _chunks(raw, interval):
    chunks, current, cursor, restart = [], bytearray(), 0, 0
    while cursor < len(raw):
        byte = raw[cursor]
        cursor += 1
        if byte != 255:
            current.append(byte)
            continue
        if cursor == len(raw):
            _fail('оборван entropy stream')
        marker = raw[cursor]
        cursor += 1
        if marker == 0:
            current.append(255)
        elif interval and marker == 0xD0 + restart % 8:
            chunks.append(bytes(current))
            current.clear()
            restart += 1
        else:
            _fail('неверный restart marker')
    chunks.append(bytes(current))
    return chunks


class _Bits:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def read(self, count):
        if self.offset + count > len(self.data) * 8:
            _fail('оборваны коэффициенты DCT')
        value = 0
        for _ in range(count):
            value = (value << 1) | ((self.data[self.offset // 8] >> (7 - self.offset % 8)) & 1)
            self.offset += 1
        return value

    def symbol(self, table):
        code = 0
        for length in range(1, 17):
            code = (code << 1) | self.read(1)
            if (length, code) in table:
                return table[length, code]
        _fail('неверный Huffman-код')

    def finish(self):
        remaining = len(self.data) * 8 - self.offset
        if remaining >= 8 or self.read(remaining) != (1 << remaining) - 1:
            _fail('лишние данные или повреждённый padding')


class Progressive:
    def __init__(self, frame):
        self.frame = frame
        self.levels = {key: [-1] * 64 for key in frame[2]}
        self.masks, self.geometry = {}, {}
        maximum_h = max(item[1] for item in frame[2].values())
        maximum_v = max(item[2] for item in frame[2].values())
        self.columns = (frame[0] + 8 * maximum_h - 1) // (8 * maximum_h)
        self.rows = (frame[1] + 8 * maximum_v - 1) // (8 * maximum_v)
        if sum(h * v for _, h, v in frame[2].values()) > 10:
            _fail('слишком много блоков в MCU')
        for key, (_, h, v) in frame[2].items():
            columns = (frame[0] * h + 8 * maximum_h - 1) // (8 * maximum_h)
            rows = (frame[1] * v + 8 * maximum_v - 1) // (8 * maximum_v)
            self.geometry[key] = (columns, rows)
        self.scans, self.work = 0, 0

    def scan(self, raw, selected, huffman, interval, spectral):
        start, end, approximation = spectral
        high, low = approximation >> 4, approximation & 15
        if not 0 <= start <= end <= 63 or (start == 0 and end != 0) or (start and len(selected) != 1):
            _fail('неверный progressive spectral selection')
        if low > 13 or (high and high != low + 1):
            _fail('неверное progressive уточнение')
        identifiers = [key for key, _, _ in selected]
        if len(set(identifiers)) != len(identifiers):
            _fail('повтор компонента в scan')
        for key, _, tables in selected:
            levels = self.levels[key]
            if start and levels[0] < 0:
                _fail('AC scan до DC scan')
            if any(levels[k] != (high if high else -1) for k in range(start, end + 1)):
                _fail('неверный порядок progressive scans')
            table = (1, tables & 15) if start else (0, tables >> 4)
            if (start or not high) and table not in huffman:
                _fail('отсутствует таблица Хаффмана')
        key = identifiers[0]
        columns, rows = self.geometry[key] if len(selected) == 1 else (self.columns, self.rows)
        total = columns * rows
        blocks_per_mcu = 1 if len(selected) == 1 else sum(c[1] * c[2] for _, c, _ in selected)
        self.scans += 1
        self.work += total * blocks_per_mcu * (end - start + 1)
        if self.scans > 128 or self.work > 100_000_000:
            _fail('превышен лимит сложности progressive JPEG')
        if start and key not in self.masks:
            self.masks[key] = array('Q', [0]) * total
        chunks = _chunks(raw, interval)
        if len(chunks) != ((total + interval - 1) // interval if interval else 1):
            _fail('неверное число restart markers')
        completed = 0
        for chunk in chunks:
            bits, eob = _Bits(chunk), 0
            count = min(interval or total, total - completed)
            for index in range(completed, completed + count):
                if not start:
                    for _, component, tables in selected:
                        for _ in range(1 if len(selected) == 1 else component[1] * component[2]):
                            if high:
                                bits.read(1)
                            else:
                                category = bits.symbol(huffman[0, tables >> 4])
                                if category > 11:
                                    _fail('неверная DC category')
                                bits.read(category)
                    continue
                mask = self.masks[key][index]
                table = huffman[1, selected[0][2] & 15]
                k = start
                if not high:
                    if eob:
                        eob -= 1
                        continue
                    while k <= end:
                        symbol = bits.symbol(table)
                        run, size = symbol >> 4, symbol & 15
                        if size:
                            k += run
                            if size > 10 or k > end:
                                _fail('неверная progressive AC category/run')
                            bits.read(size)
                            mask |= 1 << k
                            k += 1
                        elif run == 15:
                            k += 16
                            if k > end + 1:
                                _fail('неверный progressive zero run')
                        else:
                            eob = (1 << run) + bits.read(run) - 1
                            break
                else:
                    if not eob:
                        while k <= end:
                            symbol = bits.symbol(table)
                            run, size = symbol >> 4, symbol & 15
                            if size:
                                if size != 1:
                                    _fail('неверная refinement AC category')
                                bits.read(1)  # Знак нового коэффициента.
                            elif run != 15:
                                eob = (1 << run) + bits.read(run)
                                break
                            else:
                                run = 16
                            while k <= end:
                                if mask & (1 << k):
                                    bits.read(1)
                                elif run:
                                    run -= 1
                                else:
                                    break
                                k += 1
                                if not size and not run:
                                    break
                            if run or (size and k > end):
                                _fail('refinement zero run за пределами спектра')
                            if size:
                                mask |= 1 << k
                                k += 1
                    if eob:
                        while k <= end:
                            if mask & (1 << k):
                                bits.read(1)
                            k += 1
                        eob -= 1
                self.masks[key][index] = mask
            if eob:
                _fail('EOB run за пределами scan/restart')
            bits.finish()
            completed += count
        for key in identifiers:
            self.levels[key][start:end + 1] = [low] * (end - start + 1)
