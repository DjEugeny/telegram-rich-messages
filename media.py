"""Структурная проверка nonfragmented ISO-BMFF с AVC/AAC, без декодирования."""

import struct

MAX_VIDEO_BYTES = 50 * 1024 * 1024


def _fail(message):
    raise ValueError("MP4: " + message)


def _boxes(data, start, end):
    result = []
    while start < end:
        if end - start < 8:
            _fail("оборванный box")
        size, kind = struct.unpack_from(">I4s", data, start)
        header = 8
        if size == 1:
            if end - start < 16:
                _fail("оборванный extended box")
            size = struct.unpack_from(">Q", data, start + 8)[0]
            header = 16
        elif size == 0:
            size = end - start
        if size < header or size > end - start:
            _fail("границы box повреждены")
        result.append((kind, start + header, start + size))
        start += size
        if len(result) > 100000:
            _fail("слишком много boxes")
    return result


def _one(boxes, kind):
    found = [box for box in boxes if box[0] == kind]
    if len(found) != 1:
        _fail("нужен единственный " + kind.decode("ascii"))
    return found[0]


def _payload(data, box, minimum=0):
    value = data[box[1]:box[2]]
    if len(value) < minimum:
        _fail("короткий " + box[0].decode("ascii", "replace"))
    return value


def _table(data, boxes, kind, columns):
    value = _payload(data, _one(boxes, kind), 8)
    count = struct.unpack_from(">I", value, 4)[0]
    if value[:4] != b"\0\0\0\0" or len(value) != 8 + count * 4 * columns:
        _fail("неверная таблица " + kind.decode())
    return [struct.unpack_from(">" + "I" * columns, value, offset) for offset in range(8, len(value), 4 * columns)]


def _avcc(value):
    if len(value) < 7 or value[0] != 1 or value[4] & 3 == 2:
        _fail("неверный avcC")
    length_size, cursor = (value[4] & 3) + 1, 6
    sps_count = value[5] & 31
    if not sps_count:
        _fail("отсутствует SPS")
    for expected, count in ((7, sps_count), (8, None)):
        if count is None:
            if cursor >= len(value):
                _fail("отсутствует PPS")
            count, cursor = value[cursor], cursor + 1
            if not count:
                _fail("отсутствует PPS")
        for _ in range(count):
            if cursor + 2 > len(value):
                _fail("оборвана AVC configuration")
            size = int.from_bytes(value[cursor:cursor + 2], "big")
            cursor += 2
            if size < 2 or cursor + size > len(value) or value[cursor] & 128 or value[cursor] & 31 != expected:
                _fail("неверный SPS/PPS")
            cursor += size
    # High-profile расширения avcC допустимы; декодер кадров здесь не реализован.
    return length_size


def _aac(value):
    """esds DecoderConfigDescriptor должен явно объявлять MPEG-4 Audio AAC."""
    def descriptor(cursor, end):
        if cursor >= end:
            _fail("оборван esds")
        tag, cursor, size = value[cursor], cursor + 1, 0
        for _ in range(4):
            if cursor >= end:
                _fail("оборвана длина esds")
            byte, cursor = value[cursor], cursor + 1
            size = size * 128 + (byte & 127)
            if not byte & 128:
                break
        else:
            _fail("неверная длина esds")
        if cursor + size > end:
            _fail("границы esds повреждены")
        return tag, cursor, cursor + size

    if len(value) < 4 or value[:4] != b"\0\0\0\0":
        _fail("неверный esds")
    tag, begin, end = descriptor(4, len(value))
    if tag != 3 or end != len(value) or end - begin < 3:
        _fail("неверный ES descriptor")
    flags, cursor = value[begin + 2], begin + 3
    if flags & 128:
        cursor += 2
    if flags & 64:
        if cursor >= end:
            _fail("неверный ES URL")
        cursor += 1 + value[cursor]
    if flags & 32:
        cursor += 2
    tag, begin, config_end = descriptor(cursor, end)
    if tag != 4 or config_end - begin < 13 or value[begin] != 0x40 or value[begin + 1] >> 2 != 5:
        _fail("поддерживается только AAC audio")
    tag, begin, audio_end = descriptor(begin + 13, config_end)
    if tag != 5 or audio_end - begin < 2 or value[begin] >> 3 not in (1, 2, 3, 4):
        _fail("неподдерживаемая AAC configuration")


def mp4_info(data):
    """Возвращает (width, height, duration); проверяет таблицы и каждый AVC NAL envelope."""
    if not data or len(data) > MAX_VIDEO_BYTES:
        _fail("размер должен быть от 1 байта до 50 MB")
    top = _boxes(data, 0, len(data))
    ftyp = _payload(data, _one(top, b"ftyp"), 8)
    brands = [ftyp[:4]] + [ftyp[i:i + 4] for i in range(8, len(ftyp), 4)]
    if len(ftyp) % 4 or not set(brands) & {b"isom", b"iso2", b"mp41", b"mp42", b"avc1"}:
        _fail("неподдерживаемый brand")
    if any(kind in (b"moof", b"mfra") for kind, _, _ in top):
        _fail("fragmented MP4 не поддерживается")
    mdats = [(start, end) for kind, start, end in top if kind == b"mdat" and end > start]
    if not mdats:
        _fail("отсутствует mdat")
    moov = _one(top, b"moov")
    movie = _boxes(data, moov[1], moov[2])
    if any(kind == b"mvex" for kind, _, _ in movie):
        _fail("fragmented MP4 не поддерживается")
    mvhd = _payload(data, _one(movie, b"mvhd"), 100)
    if mvhd[0] not in (0, 1):
        _fail("неверный mvhd")
    movie_offset = 20 if mvhd[0] else 12
    if mvhd[0] and len(mvhd) < 112:
        _fail("оборван mvhd v1")
    movie_scale = int.from_bytes(mvhd[movie_offset:movie_offset + 4], "big")
    movie_duration = int.from_bytes(mvhd[movie_offset + 4:movie_offset + (12 if mvhd[0] else 8)], "big")
    if not movie_scale or not movie_duration:
        _fail("неверная movie timescale/duration")
    video, intervals, track_ids = [], [], set()
    tracks = [box for box in movie if box[0] == b"trak"]
    if not 1 <= len(tracks) <= 2:
        _fail("нужен один video track и необязательный AAC audio track")
    for track in tracks:
        children = _boxes(data, track[1], track[2])
        tkhd = _payload(data, _one(children, b"tkhd"), 84)
        if tkhd[0] not in (0, 1):
            _fail("неверный tkhd")
        if tkhd[0] and len(tkhd) < 96:
            _fail("оборван tkhd v1")
        tkhd_offset = 20 if tkhd[0] else 12
        track_id = int.from_bytes(tkhd[tkhd_offset:tkhd_offset + 4], "big")
        if not track_id or track_id in track_ids:
            _fail("неверный track ID")
        track_ids.add(track_id)
        mdia = _one(children, b"mdia")
        metadata = _boxes(data, mdia[1], mdia[2])
        mdhd = _payload(data, _one(metadata, b"mdhd"), 24)
        if mdhd[0] == 0:
            timescale, duration = struct.unpack_from(">II", mdhd, 12)
        elif mdhd[0] == 1 and len(mdhd) >= 36:
            timescale, duration = struct.unpack_from(">IQ", mdhd, 20)
        else:
            _fail("неверный mdhd")
        if not timescale or not duration or duration == (2**64 - 1 if mdhd[0] else 2**32 - 1):
            _fail("неверная длительность")
        handler = _payload(data, _one(metadata, b"hdlr"), 12)[8:12]
        if handler not in (b"vide", b"soun"):
            _fail("поддерживаются только video/audio tracks")
        minf = _one(metadata, b"minf")
        info = _boxes(data, minf[1], minf[2])
        dinf = _one(info, b"dinf")
        dref = _payload(data, _one(_boxes(data, dinf[1], dinf[2]), b"dref"), 8)
        refs = _boxes(dref, 8, len(dref))
        if int.from_bytes(dref[4:8], "big") != 1 or len(refs) != 1 or refs[0][0] != b"url " or _payload(dref, refs[0]) != b"\0\0\0\1":
            _fail("внешние media references запрещены")
        stbl = _one(info, b"stbl")
        tables = _boxes(data, stbl[1], stbl[2])
        stsd = _one(tables, b"stsd")
        value = _payload(data, stsd, 8)
        entries = _boxes(data, stsd[1] + 8, stsd[2])
        if value[:8] != b"\0\0\0\0\0\0\0\1" or len(entries) != 1:
            _fail("нужен единственный sample description")
        entry = entries[0]
        value = _payload(data, entry, 78 if handler == b"vide" else 28)
        if value[:6] != b"\0" * 6 or value[6:8] != b"\0\1":
            _fail("неверный data reference")
        if handler == b"vide":
            if entry[0] != b"avc1":
                _fail("поддерживается только H.264/AVC avc1")
            width, height = struct.unpack_from(">HH", value, 24)
            if not 1 <= width <= 8192 or not 1 <= height <= 8192:
                _fail("неверные размеры video")
            codec_boxes = _boxes(data, entry[1] + 78, entry[2])
            nal_size = _avcc(_payload(data, _one(codec_boxes, b"avcC")))
            video.append((width, height, duration / timescale))
        else:
            if entry[0] != b"mp4a" or value[8:10] != b"\0\0":
                _fail("поддерживается только AAC mp4a v0")
            codec_boxes = _boxes(data, entry[1] + 28, entry[2])
            _aac(_payload(data, _one(codec_boxes, b"esds")))
        stsz = _payload(data, _one(tables, b"stsz"), 12)
        sample_size, count = struct.unpack_from(">II", stsz, 4)
        if stsz[:4] != b"\0" * 4 or not 1 <= count <= 2000000 or len(stsz) != (12 if sample_size else 12 + 4 * count):
            _fail("неверный stsz")
        sizes = [sample_size] * count if sample_size else list(struct.unpack_from(">" + "I" * count, stsz, 12))
        if not all(sizes):
            _fail("пустой sample")
        timings = _table(data, tables, b"stts", 2)
        # Некоторые muxer округляют mdhd и сумму stts по-разному. Разрешаем
        # только один тик исходной timescale (и не больше 1 ms), не один кадр.
        timing_duration = sum(n * delta for n, delta in timings)
        difference = abs(timing_duration - duration)
        if (not timings or any(not n or not delta for n, delta in timings)
                or sum(n for n, _ in timings) != count
                or difference > 1 or difference * 1000 > timescale):
            _fail("sample timing не совпадает с длительностью")
        for optional in (b"stss", b"ctts"):
            if any(box[0] == optional for box in tables):
                if optional == b"stss":
                    sync = _table(data, tables, optional, 1)
                    if not sync or any(not 1 <= n <= count for n, in sync) or sync != sorted(set(sync)):
                        _fail("неверный sync sample")
                else:
                    ctts = _payload(data, _one(tables, optional), 8)
                    if ctts[0] not in (0, 1) or ctts[1:4] != b"\0" * 3 or len(ctts) != 8 + int.from_bytes(ctts[4:8], "big") * 8 or sum(int.from_bytes(ctts[i:i + 4], "big") for i in range(8, len(ctts), 8)) != count:
                        _fail("неверный composition timing")
        stsc = _table(data, tables, b"stsc", 3)
        offsets_kind = b"co64" if any(box[0] == b"co64" for box in tables) else b"stco"
        if sum(box[0] in (b"co64", b"stco") for box in tables) != 1:
            _fail("нужна единственная chunk offset table")
        offsets_value = _payload(data, _one(tables, offsets_kind), 8)
        offset_size = 8 if offsets_kind == b"co64" else 4
        chunks = int.from_bytes(offsets_value[4:8], "big")
        if not chunks or offsets_value[:4] != b"\0" * 4 or len(offsets_value) != 8 + chunks * offset_size:
            _fail("неверные chunk offsets")
        if not stsc or stsc[0][0] != 1 or any(not 1 <= first <= chunks or not per or desc != 1 for first, per, desc in stsc) or any(a[0] >= b[0] for a, b in zip(stsc, stsc[1:])):
            _fail("неверный sample-to-chunk")
        sample_index, mapping = 0, 0
        for chunk in range(1, chunks + 1):
            while mapping + 1 < len(stsc) and stsc[mapping + 1][0] <= chunk:
                mapping += 1
            per = stsc[mapping][1]
            if sample_index + per > count:
                _fail("лишние samples в chunk")
            start = int.from_bytes(offsets_value[8 + (chunk - 1) * offset_size:8 + chunk * offset_size], "big")
            end = start + sum(sizes[sample_index:sample_index + per])
            if not any(begin <= start < end <= limit for begin, limit in mdats):
                _fail("sample offset за пределами mdat")
            intervals.append((start, end))
            cursor = start
            for size in sizes[sample_index:sample_index + per]:
                sample_end = cursor + size
                if handler == b"vide":
                    vcl = False
                    while cursor < sample_end:
                        if cursor + nal_size > sample_end:
                            _fail("оборван размер NAL")
                        length = int.from_bytes(data[cursor:cursor + nal_size], "big")
                        cursor += nal_size
                        if length < 1 or cursor + length > sample_end or data[cursor] & 128 or not 1 <= data[cursor] & 31 <= 23:
                            _fail("повреждённый AVC NAL sample")
                        vcl |= data[cursor] & 31 in (1, 2, 3, 4, 5)
                        cursor += length
                    if not vcl:
                        _fail("sample без AVC кадра")
                else:
                    cursor = sample_end
            sample_index += per
        if sample_index != count:
            _fail("не все samples адресованы")
    intervals.sort()
    if any(first[1] > second[0] for first, second in zip(intervals, intervals[1:])):
        _fail("перекрывающиеся chunks")
    if len(video) != 1:
        _fail("нужен единственный video track")
    return video[0]
