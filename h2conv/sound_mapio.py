# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Minimal read-only H2 cache-map reader used by the sound tooling.

Supports Vista (v8, 0x800 header) and MCC (v13, 0x380 header; uncompressed maps only).
Only what is needed to locate tags, follow tag blocks and read raw file regions.
"""
import ctypes
ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
import mmap
import struct

from . import paths as _paths


def __getattr__(name):
    """MAPS: the sample maps of the developer tools and tests, resolved by h2conv.paths when somebody asks for them.  The converter does not use it."""
    if name == 'MAPS':
        return _paths.sample_maps()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))

U32 = struct.Struct('<I')


def fourcc(b):
    """tag group code as a string (stored big-endian-looking in file as reversed?)."""
    return b[::-1].decode('latin1') if len(b) == 4 else '????'


class H2Map:
    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        self.m = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        m = self.m
        self.version = U32.unpack_from(m, 4)[0]
        self.file_size = U32.unpack_from(m, 8)[0]
        if self.version == 8:
            self.kind = 'vista'
            self.hdr_size = 0x800
            self.meta_off = U32.unpack_from(m, 0x10)[0]
            self.tagdata_off = U32.unpack_from(m, 0x14)[0]
            self.tagdata_size = U32.unpack_from(m, 0x18)[0]
            self.meta_size = U32.unpack_from(m, 0x1C)[0]
            self.meta_mask = U32.unpack_from(m, 0x20)[0]
            self.gestalt_datum = U32.unpack_from(m, 0x2E4)[0]
            self.raw_off = U32.unpack_from(m, 0x2E8)[0]
            self.raw_size = U32.unpack_from(m, 0x2EC)[0]
            self.build = m[0x12C:0x12C + 32].split(b'\0')[0].decode('latin1')
        elif self.version == 13:
            self.kind = 'mcc'
            self.hdr_size = 0x380
            self.meta_off = U32.unpack_from(m, 0x10)[0]
            self.meta_size = U32.unpack_from(m, 0x14)[0]
            self.meta_mask = U32.unpack_from(m, 0x2D0)[0]
            self.tagdata_off = U32.unpack_from(m, 0x2D4)[0]
            self.tagdata_size = U32.unpack_from(m, 0x2D8)[0]
            self.gestalt_datum = U32.unpack_from(m, 0x2EC)[0]
            self.raw_off = U32.unpack_from(m, 0x2F0)[0]
            self.raw_size = U32.unpack_from(m, 0x2F4)[0]
            self.build = m[0x90:0x90 + 32].split(b'\0')[0].decode('latin1')
            self.hdr_flags = struct.unpack_from('<H', m, 0x1C)[0]
            # bit3 (0x08): bitmap/sound raw data lives in textures.dat / sounds_*.dat (stock maps); clear = inside the map
            self.external_raw = bool(self.hdr_flags & 0x08)
        else:
            raise ValueError('unsupported version %d' % self.version)
        # meta header at meta_off
        mo = self.meta_off
        (self.grp_tbl, self.grp_cnt, self.tag_tbl, self.scnr_datum, self.matg_datum,
         self.rand_id, self.tag_cnt, self.tags_fourcc) = struct.unpack_from('<iiiIIIii', m, mo)
        # grp_tbl / tag_tbl are RELATIVE to the meta header (0x20 / 0x5C0).
        # tag memory addresses: file offset = addr - meta_mask + meta_off   (header meta-offset-mask)
        self._tags = None

    # address helpers: tag-block pointers are virtual addresses; file = addr - magic + meta_off
    def a2f(self, addr):
        return addr - self.meta_mask + self.meta_off

    def tags(self):
        if self._tags is None:
            out = []
            base = self.meta_off + self.tag_tbl
            for i in range(self.tag_cnt):
                g, d, addr, sz = struct.unpack_from('<4sIII', self.m, base + i * 16)
                out.append({'group': g[::-1].decode('latin1'), 'datum': d, 'addr': addr, 'size': sz,
                            'foff': (self.a2f(addr) if addr and sz else None), 'idx': i})
            self._tags = out
        return self._tags

    def names(self):
        """tag index -> tag path string (from the header file table), {} if absent"""
        if getattr(self, '_names', None) is not None:
            return self._names
        m = self.m
        if self.kind == 'vista':
            cnt, off, size, idx = (U32.unpack_from(m, o)[0] for o in (0x2CC, 0x2D0, 0x2D4, 0x2D8))
        else:
            cnt, off, size, idx = (U32.unpack_from(m, o)[0] for o in (0x20, 0x24, 0x28, 0x2C))
        out = {}
        for i in range(self.tag_cnt):
            if idx + i * 4 + 4 > len(m):
                break
            o = U32.unpack_from(m, idx + i * 4)[0]
            if o == 0xFFFFFFFF or o >= size:
                continue
            e = m.find(bytes([0]), off + o, off + size)
            out[i] = bytes(m[off + o:e]).decode('latin1')
        self._names = out
        return out

    def tag_by_datum(self, datum):
        # datum index low 16 bits = index
        return self.tags()[datum & 0xFFFF]

    def tags_of(self, grp):
        return [t for t in self.tags() if t['group'] == grp]

    def block(self, foff, count_off):
        """read a tag block header (count,addr) at file offset foff+count_off, return (count, file offset of element 0)"""
        cnt, addr = struct.unpack_from('<iI', self.m, foff + count_off)
        if cnt <= 0 or addr == 0:
            return 0, None
        return cnt, self.a2f(addr)

    def read(self, off, n):
        return bytes(self.m[off:off + n])

    def close(self):
        self.m.close()
        self.f.close()
