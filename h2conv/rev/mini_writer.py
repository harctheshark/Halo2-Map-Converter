# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Minimal MCC (cache v13) writer: the fallback of h2conv/rev/convert.py when h2conv/mcc_writer.py is absent.

Same names and call order as the real writer's MccBuilder but only what a SELF-CONTAINED, uncompressed,
H2EK-style map needs (header flags 0x10, store mask 0, mask 0, every tag local, raw data inside the map):

    b = MiniBuilder(header, groups)           header = new_header(name, scenario_path, map_type) ; groups = groups_from_stock(<any MCC map>)
    off = b.add_sound(blob) / b.add_geometry(blob)              absolute file offsets (0x80 aligned, identical blobs stored once)
    b.set_strings(list) ; b.set_tag_names(list)                  -> b.locale_base
    b.set_locale(blob, None)                                     (optional)
    off = b.add_bitmap(blob)
    b.set_tags(instances, tag_data, scenario_datum, globals_datum) ; b.write(path)

Layout (verified on the H2EK-built maps): header | sounds | geometry | string index | string data |
tag names | tag name index | locale | bitmaps | meta ('tags' header, 120 group links, 16-byte instances, pad to 4 KiB, tag data, pad to 4 KiB);
every region starts and ends on a 0x80 boundary.  Header 0x2F8 = XOR32 of all dwords of [0x380, EOF); 0x2FC (running CRC of the
tool's writes) is left 0 - the engine does not read it.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import os
import struct
import time

HEADER_SIZE = 0x380
PAGE = 0x80
META_ALIGN = 0x1000
NONE32 = 0xFFFFFFFF
MCC_VERSION = 13


def align_up(x, a=PAGE):
    return (x + a - 1) // a * a


def fourcc_int(text):
    if text is None:
        return NONE32
    return struct.unpack('<I', text.encode('latin-1')[::-1])[0]


class MiniHeader(object):
    """Just enough of the 0x380 header: a bytearray with named accessors for the fields the builder derives."""

    def __init__(self, raw=None):
        self.raw = bytearray(raw) if raw is not None else bytearray(HEADER_SIZE)

    def u32(self, off):
        return struct.unpack_from('<I', self.raw, off)[0]

    def set_u32(self, off, v):
        struct.pack_into('<I', self.raw, off, v & 0xFFFFFFFF)

    def set_u16(self, off, v):
        struct.pack_into('<H', self.raw, off, v & 0xFFFF)

    def set_str(self, off, size, text):
        b = text.encode('latin-1')
        if len(b) >= size:
            raise ValueError('string too long for the header field (%d >= %d)' % (len(b), size))
        self.raw[off:off + size] = b.ljust(size, b'\0')

    def pack(self):
        return bytes(self.raw)


def new_header(name, scenario_path, map_type=1, creation_time=None, flags=0x10):
    if len(name.encode('latin-1')) >= 32:
        raise ValueError('map name must be < 32 characters')
    h = MiniHeader()
    h.raw[0:4] = b'daeh'
    h.raw[0x37C:0x380] = b'toof'
    h.set_u32(0x04, MCC_VERSION)
    h.set_u16(0x0C, 0x0201)
    h.set_u16(0x18, map_type)
    h.set_u16(0x1C, flags)
    h.set_u16(0x1E, 0)
    h.set_str(0xB0, 0x20, name)
    h.set_str(0xD0, 0x100, scenario_path)
    struct.pack_into('<Q', h.raw, 0x48, creation_time if creation_time is not None else int(time.time() * 10000000) + 116444736000000000)
    h.set_u32(0x2DC, NONE32)
    h.set_u32(0x2E4, NONE32)
    h.set_u32(0x2EC, NONE32)
    return h


class GroupLink(object):
    def __init__(self, group, parent, grandparent):
        self.group, self.parent, self.grandparent = group, parent, grandparent

    def pack(self):
        return struct.pack('<III', self.group, self.parent, self.grandparent)


def groups_from_stock(path):
    """The 120-entry tag group table (identical in every v13 map)."""
    from ..mcc_cache import MccCache
    c = MccCache(path, load_names=False)
    try:
        mh = c.meta_header
        return [GroupLink(*struct.unpack_from('<III', c.mm, c.meta_offset + mh['tag_group_table_offset'] + 12 * i))
                for i in range(mh['number_of_tag_groups'])]
    finally:
        c.close()


class MccTagInstance(object):
    __slots__ = ('index', 'group', 'datum', 'addr', 'size', 'name')

    def __init__(self, index, group, datum, addr, size, name=None):
        self.index, self.group, self.datum, self.addr, self.size, self.name = index, group, datum, addr, size, name

    def pack(self):
        return struct.pack('<IIII', fourcc_int(self.group), self.datum, self.addr, self.size)

    @staticmethod
    def null(index):
        return MccTagInstance(index, None, NONE32, 0, 0)


def make_datum(index, salt=None):
    if salt is None:
        salt = 0x8000 | ((0x6373 + index) & 0x7FFF)
    return ((salt & 0xFFFF) << 16) | (index & 0xFFFF)


class _Region(object):
    """Sequential 0x80-padded payloads with identical-blob de-duplication."""

    def __init__(self):
        self.parts = []
        self.size = 0
        self._seen = {}

    def add(self, data, dedupe=True):
        data = bytes(data)
        if dedupe and data in self._seen:
            return self._seen[data]
        off = self.size
        pad = align_up(len(data)) - len(data)
        self.parts.append(data + b'\0' * pad)
        self.size += len(data) + pad
        if dedupe:
            self._seen[data] = off
        return off


class MiniBuilder(object):
    def __init__(self, header, groups, tag_mask=0):
        self.header = header
        self.groups = list(groups)
        self.mask = tag_mask
        self._sounds = _Region()
        self._geometry = _Region()
        self._bitmaps = _Region()
        self._stage = 1
        self.strings = []
        self.tag_names = []
        self._locale = b''
        self._locale_globals = None
        self.tags = []
        self.tag_data = b''
        self.scenario_datum = self.globals_datum = NONE32

    # stages 1/2: sounds then geometry --------------------------------------------------------------------
    def add_sound(self, data, dedupe=True):
        if self._stage > 1:
            raise RuntimeError('sound region already sealed')
        return HEADER_SIZE + self._sounds.add(data, dedupe)

    def add_geometry(self, data, dedupe=True):
        self._stage = max(self._stage, 2)
        if self._stage > 2:
            raise RuntimeError('geometry region already sealed')
        return HEADER_SIZE + self._sounds.size + self._geometry.add(data, dedupe)

    # stage 3: tables -----------------------------------------------------------------------------------
    @staticmethod
    def _pack_strings(strings):
        offs, parts, cur = [], [], 0
        for s in strings:
            b = s.encode('utf-8') + b'\0'
            offs.append(cur)
            parts.append(b)
            cur += len(b)
        return struct.pack('<%dI' % len(offs), *offs), b''.join(parts)

    @staticmethod
    def _pack_names(names):
        offs, parts, cur = [], [], 0
        for s in names:
            if s is None:
                offs.append(NONE32)
                continue
            b = s.encode('latin-1') + b'\0'
            offs.append(cur)
            parts.append(b)
            cur += len(b)
        return struct.pack('<%dI' % len(offs), *offs), b''.join(parts)

    def set_strings(self, strings):
        self._stage = max(self._stage, 3)
        self.strings = list(strings)

    def set_tag_names(self, names, order=None):
        self._stage = max(self._stage, 3)
        self.tag_names = list(names)

    def _tables(self):
        sidx, sdat = self._pack_strings(self.strings)
        nidx, ndat = self._pack_names(self.tag_names)
        return sidx, sdat, ndat, nidx

    @property
    def locale_base(self):
        sidx, sdat, ndat, nidx = self._tables()
        return (HEADER_SIZE + self._sounds.size + self._geometry.size + align_up(len(sidx)) + align_up(len(sdat)) +
                align_up(len(ndat)) + align_up(len(nidx)))

    def set_locale(self, locale_data, locale_globals=None):
        if len(locale_data) % PAGE:
            raise ValueError('locale_data must be a multiple of 0x80')
        self._locale = bytes(locale_data)
        self._locale_globals = None if locale_globals is None else bytes(locale_globals)

    # stage 4: bitmaps --------------------------------------------------------------------------------------
    @property
    def bitmap_base(self):
        return self.locale_base + len(self._locale) + (align_up(len(self._locale_globals)) if self._locale_globals else 0)

    def add_bitmap(self, data, dedupe=True):
        self._stage = max(self._stage, 4)
        return self.bitmap_base + self._bitmaps.add(data, dedupe)

    # stage 5: tags -----------------------------------------------------------------------------------------
    def tag_region_data_offset(self, tag_count):
        return align_up(0x20 + 12 * len(self.groups) + 16 * tag_count, META_ALIGN)

    def set_tags(self, tags, tag_data, scenario_datum, globals_datum, unk14=None):
        self._stage = 5
        self.tags = list(tags)
        self.tag_data = bytes(tag_data)
        self.scenario_datum, self.globals_datum = scenario_datum, globals_datum
        self.unk14 = unk14 or 0

    def build_image(self):
        return self

    def write(self, path, compress=False, workers=1):
        if compress:
            raise NotImplementedError('the minimal writer does not compress')
        if self._stage < 5:
            raise RuntimeError('set_tags() not called')
        h = MiniHeader(self.header.pack())
        sidx, sdat, ndat, nidx = self._tables()
        body = bytearray()                                           # file[0x380:]
        pos = HEADER_SIZE

        def emit(b, pad=True):
            nonlocal pos
            b = bytes(b)
            body.extend(b)
            if pad:
                p = align_up(len(b)) - len(b)
                body.extend(b'\0' * p)
                pos += len(b) + p
            else:
                pos += len(b)

        for part in self._sounds.parts:
            body.extend(part)
        pos += self._sounds.size
        raw_off = pos
        for part in self._geometry.parts:
            body.extend(part)
        pos += self._geometry.size
        h.set_u32(0x2F0, raw_off)
        h.set_u32(0x2F4, self._geometry.size)
        h.set_u32(0x3C, pos); emit(sidx)
        h.set_u32(0x34, pos); h.set_u32(0x38, len(sdat)); h.set_u32(0x30, len(self.strings)); emit(sdat)
        h.set_u32(0x24, pos); h.set_u32(0x28, len(ndat)); emit(ndat)
        h.set_u32(0x2C, pos); h.set_u32(0x20, len(self.tag_names)); emit(nidx)
        if self._locale:
            emit(self._locale, pad=False)
        if self._locale_globals:
            h.set_u32(0x2E4, pos)
            h.set_u32(0x2E8, len(self._locale_globals))
            emit(self._locale_globals)
        for part in self._bitmaps.parts:
            body.extend(part)
        pos += self._bitmaps.size
        # meta
        n = len(self.tags)
        data_off = self.tag_region_data_offset(n)
        meta = bytearray(struct.pack('<iiiIIIi', 0x20, len(self.groups), 0x20 + 12 * len(self.groups), self.scenario_datum,
                                     self.globals_datum, self.unk14, n))
        meta += struct.pack('<I', 0x74616773)
        for g in self.groups:
            meta += g.pack()
        for t in self.tags:
            meta += t.pack()
        meta += b'\0' * (data_off - len(meta))
        meta += self.tag_data
        meta += b'\0' * (align_up(len(meta), META_ALIGN) - len(meta))
        h.set_u32(0x10, pos)
        h.set_u32(0x14, len(meta))
        h.set_u32(0x2D0, self.mask)
        h.set_u32(0x2D4, data_off)
        h.set_u32(0x2D8, len(meta) - data_off)
        body.extend(meta)
        pos += len(meta)
        h.set_u32(0x08, pos)
        # XOR32 of every dword of [0x380, EOF)
        x = 0
        try:
            import numpy as np
            a = np.frombuffer(bytes(body), dtype='<u4')
            x = int(np.bitwise_xor.reduce(a)) if len(a) else 0
        except Exception:                                            # noqa: BLE001
            for (v,) in struct.iter_unpack('<I', bytes(body)):
                x ^= v
        h.set_u32(0x2F8, x)
        tmp = path + '.part'
        with open(tmp, 'wb') as fh:
            fh.write(h.pack())
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def write_image(img, path, **kw):
    img.write(path)


def validate_file(path, deep=False, check_prefix=False):
    """Structural checks of an uncompressed file with MccCache (it raises on a bad tag table / names)."""
    from ..mcc_cache import MccCache
    problems = []
    try:
        c = MccCache(path)
        if c.header['file_size'] != c.file_size:
            problems.append('header file size != real size')
        c.close()
    except Exception as ex:                                          # noqa: BLE001
        problems.append('MccCache: %r' % (ex,))
    return problems
