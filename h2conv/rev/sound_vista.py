# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Readers for the Vista (cache v8) sound cache-file gestalt ('ugh!') and 'snd!' tags  (REVERSE direction).

The inverse of `h2conv.sound_bundle.McGestalt`: it reads ONE Vista `ugh!` tag (0x58 bytes, 11 tag blocks) on top of
`h2conv.vista_cache.VistaCache` and resolves the sound chunks it lists through the Vista raw-pointer scheme
(`(location << 30) | file_offset`, location 0 = the map itself, 1 = mainmenu.map, 2 = shared.map, 3 = single_player_shared.map).

Vista `ugh!` root (sound_cache_file_definitions.h):
    playbacks@0x00 scales@0x08 import_names@0x10 pitch_range_params@0x18 pitch_ranges@0x20 permutations@0x28
    custom_playbacks@0x30 runtime_perm_flags@0x38 chunks@0x40 promotions@0x48 extra_info@0x50
Vista `snd!` body = 20 bytes (sound_definitions.h `sound_definition_v1`).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass
import struct

from h2conv import tagtree as T

NULL = 0xFFFFFFFF
VISTA_UGH = dict(playbacks=0x00, scales=0x08, import_names=0x10, pitch_range_params=0x18, pitch_ranges=0x20,
                 permutations=0x28, custom_playbacks=0x30, runtime_perm_flags=0x38, chunks=0x40, promotions=0x48,
                 extra_info=0x50)
VELEM = dict(playbacks=0x38, scales=0x14, import_names=4, pitch_range_params=0xA, pitch_ranges=0xC, permutations=0x10,
             custom_playbacks=0x34, runtime_perm_flags=1, chunks=0xC, promotions=0x1C, extra_info=0x2C)
NODE_FIELD = {'custom_playbacks': 'Custom Playbacks', 'promotions': 'Promotions', 'extra_info': 'Extra Info'}
SND_SIZE = 0x14


def _s8(v):
    return v - 256 if v > 127 else v


def parse_vista_snd(body):
    """Vista snd! tag body (20 bytes) -> dict (field names as in the MCC parse: McGestalt.parse_snd)."""
    if len(body) < SND_SIZE:
        raise ValueError('Vista snd! body is %d bytes, expected %d' % (len(body), SND_SIZE))
    flags, cls, rate, enc, comp = struct.unpack_from('<HBBBB', body, 0)
    pb, pr = struct.unpack_from('<hh', body, 6)
    return dict(flags=flags, cls=cls, rate=rate, encoding=enc, compression=comp, playback=pb, pitch_range=pr,
                pitch_range_count=body[0xA], scale=_s8(body[0xB]), promotion=_s8(body[0xC]),
                custom_playback=_s8(body[0xD]), extra_info=struct.unpack_from('<h', body, 0xE)[0],
                max_play=struct.unpack_from('<I', body, 0x10)[0])


class VistaGestalt(object):
    """Reader of one Vista `ugh!` tag.

    `cache`   the VistaCache that holds the tag
    `tag`     its VistaTag (must have data)
    `layout`  ctx.lay_vista['ugh!'] (only needed for node())
    `maps`    {location: VistaCache} used to resolve chunk / lip-sync pointers; location 0 defaults to `cache`.
              Missing locations are looked up through `opener(location)` when given (a callable returning a VistaCache or None).
    """

    def __init__(self, cache, tag, layout=None, maps=None, opener=None):
        self.cache = cache
        self.tag = tag
        self.layout = layout
        self.maps = dict(maps or {})
        self.maps.setdefault(0, cache)
        self.opener = opener
        self.blocks = {}
        base = tag.file_offset if tag.file_offset is not None else cache.addr_to_offset(tag.addr)
        raw = cache.raw(base, 0x58)
        for name, off in VISTA_UGH.items():
            cnt, addr = struct.unpack_from('<iI', raw, off)
            self.blocks[name] = (max(cnt, 0), cache.addr_to_offset(addr) if cnt > 0 and addr else None)
        self._data = {}
        self.fields = {f.name: f for f in layout.fields} if layout is not None else {}

    # ---- raw block access -----------------------------------------------------------------------
    def count(self, name):
        return self.blocks[name][0]

    def _bytes(self, name):
        b = self._data.get(name)
        if b is None:
            cnt, off = self.blocks[name]
            b = self.cache.raw(off, cnt * VELEM[name]) if cnt else b''
            self._data[name] = b
        return b

    def elem_bytes(self, name, i):
        cnt = self.blocks[name][0]
        if not 0 <= i < cnt:
            raise IndexError('%s[%d] outside 0..%d' % (name, i, cnt))
        es = VELEM[name]
        return self._bytes(name)[i * es:(i + 1) * es]

    # ---- typed accessors -------------------------------------------------------------------------
    def pitch_range(self, i):
        imp, par, enc, rtf, first, n = struct.unpack('<6h', self.elem_bytes('pitch_ranges', i))
        return dict(import_name=imp, params=par, encoded_data=enc, runtime_flag=rtf, first_perm=first, perm_count=n)

    def permutation(self, i):
        imp, skip, gain, info, lnt, ssz, first, cnt = struct.unpack('<hHbbHIhh', self.elem_bytes('permutations', i))
        return dict(import_name=imp, skip=skip, gain=gain, info=info, lnt=lnt, sample_size=ssz, first_chunk=first,
                    chunk_count=cnt)

    def chunk(self, i):
        """-> (file_offset_with_location_bits, size, f31, runtime).  size = bits 0..30 of the size dword, f31 = bit 31
        (sound.md 4.1: the plugin's 16+16 bit split of that dword is wrong)."""
        fo, sd, rt = struct.unpack('<IIi', self.elem_bytes('chunks', i))
        return fo, sd & 0x7FFFFFFF, bool(sd & 0x80000000), rt

    def node(self, name, i):
        """Decode one element of a verbatim block (custom_playbacks / promotions / extra_info) with its children (Vista layout)."""
        if self.layout is None:
            raise RuntimeError('VistaGestalt needs the Vista ugh! layout for node()')
        cnt, off = self.blocks[name]
        if not 0 <= i < cnt:
            raise IndexError('%s[%d] outside 0..%d' % (name, i, cnt))
        f = self.fields[NODE_FIELD[name]]
        es = VELEM[name]
        raw = self._bytes(name)[i * es:(i + 1) * es]
        cache = self.cache
        return T.decode_struct(lambda a, n: cache.read_addr(a, n), 'ugh!', T.child_path('', f), f.children, es,
                               cache.offset_to_addr(off + i * es), raw=raw)

    # ---- raw payloads ----------------------------------------------------------------------------
    def map_at(self, location):
        m = self.maps.get(location)
        if m is None and self.opener is not None:
            m = self.opener(location)
            if m is not None:
                self.maps[location] = m
        return m

    def read_raw(self, pointer, size):
        """Bytes at a Vista raw pointer ((location << 30) | offset)."""
        loc, off = pointer >> 30, pointer & 0x3FFFFFFF
        m = self.map_at(loc)
        if m is None:
            raise ValueError('raw pointer %#x refers to location %d, which is not available' % (pointer, loc))
        if off + size > m.size:
            raise ValueError('raw pointer %#x + %d is outside %s' % (pointer, size, getattr(m, 'path', '?')))
        return m.raw(off, size)

    def perm_chunks(self, perm):
        """[(payload_bytes, size, f31)] of one permutation, in chunk order."""
        out = []
        for c in range(perm['first_chunk'], perm['first_chunk'] + perm['chunk_count']):
            fo, size, f31, rt = self.chunk(c)
            out.append((self.read_raw(fo, size), size, f31))
        return out

    def perm_payload(self, perm):
        """The whole encoded payload of a permutation (chunks concatenated)."""
        return b''.join(p for p, _, _ in self.perm_chunks(perm))


def vista_maps_for(ctx):
    """Best-effort `opener(location)` for a reverse ctx: location 1 mainmenu.map, 2 shared.map, 3 single_player_shared.map.
    Looks at `ctx.vista` (VistaRefs-like: .mainmenu .shared .sp_shared) / `ctx.vista_refs`."""
    refs = getattr(ctx, 'vista', None) or getattr(ctx, 'vista_refs', None) or getattr(ctx, 'vref', None)

    def opener(location):
        if refs is None:
            return None
        try:
            if location == 1:
                return refs.mainmenu
            if location == 2:
                return refs.shared
            if location == 3:
                return refs.sp_shared
        except Exception:
            return None
        return None
    return opener
