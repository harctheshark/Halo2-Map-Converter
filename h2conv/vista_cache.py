# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Halo 2 Vista (cache version 8) cache-file container: reader + low-level helpers.

Everything in here is derived from (and cross-checked against) real bytes of the stock Vista
``headlong/shared/single_player_shared/mainmenu`` maps, the Project Cartographer engine source
(``Blam/Engine/cache/cache_files.h/.cpp``), disassembly of ``halo2.exe`` (cache_header_verify @0x4310E9,
cache_file_blocking_read @0x464D01, shared-database pointer decode @0x430C8B/0x430CC4) and the decompiled
H2Tool cache builder (``build_cache_file`` @0x8352F0 in H2ToolSymbols).

Public API
----------
    VistaCache(path)            -- memory-mapped, read-only view of a Vista .map
        .header                 -- VistaHeader (all 0x800 bytes decoded; .raw keeps the original bytes)
        .groups                 -- list[TagGroupLink]  (group, parent, grandparent four-CC strings)
        .tags                   -- list[VistaTag]      (index/group/name/datum/addr/size/file_offset ...)
        .read_tag(tag_or_index) -- bytes of the tag's data
        .addr_to_offset(addr)   -- tag-data memory address -> file offset
        .offset_to_addr(off)
        .string_id(sid) / .string_by_index(i)
        .raw(offset, size)      -- bytes from the file (local pointers only)
        .resolve_raw_pointer(p) -- (database, offset)  see decode_raw_pointer()
        .sections()             -- ordered list of (name, offset, size) covering the whole file

Module-level helpers: ``decode_raw_pointer``, ``encode_raw_pointer``, ``xor32``, ``jamcrc_update``,
``align_up``, ``fourcc``.
"""
from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple, Union

# --------------------------------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------------------------------
HEADER_SIZE = 0x800
CACHE_FILE_PAGE_SIZE = 0x200           # CACHE_FILE_PAGE_SIZE = 512; halo2.exe @0x4647DA rounds up to 0x200
TAG_REGION_ALIGN = 0x1000              # H2Tool sub_840440(x, 12): tag header size / tag region size
TAG_DATA_ALIGN = 4                     # H2Tool sub_8403F0(x, 2): every tag's data starts 4-aligned
FIRST_SHARED_TAG_INSTANCE_INDEX = 10000
MAX_SHARED_DB = 3
HEADER_SIGNATURE = b"daeh"             # 'head' stored as little-endian dword 0x68656164
FOOTER_SIGNATURE = b"toof"             # 'foot' stored as little-endian dword 0x666f6f74
TAGS_SIGNATURE = b"sgat"               # 'tags' stored as little-endian dword 0x74616773
VISTA_VERSION = 8
BUILD_VISTA = "11081.07.04.30.0934.main"
COMPATIBLE_BUILDS = ("11028.07.03.23.1927.main", "11081.07.04.30.0934.main", "11122.07.08.24.1808.main")

SCENARIO_TYPES = {0: "solo", 1: "multiplayer", 2: "mainmenu", 3: "multiplayer_shared", 4: "single_player_shared"}
# raw-pointer database selector (top 2 bits) -> logical database
DB_LOCAL, DB_MAINMENU, DB_SHARED, DB_SP_SHARED = -1, 0, 1, 2
DB_NAMES = {DB_LOCAL: "local", DB_MAINMENU: "mainmenu.map", DB_SHARED: "shared.map", DB_SP_SHARED: "single_player_shared.map"}


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def fourcc(s: str) -> bytes:
    """'bitm' -> the 4 bytes as they appear in the file (little-endian dword of the big-endian four-cc)."""
    return s.encode("latin-1")[::-1]


def fourcc_str(b: bytes) -> str:
    """4 bytes from the file -> 'bitm'.  0xFFFFFFFF -> ''."""
    if b == b"\xff\xff\xff\xff":
        return ""
    return b[::-1].decode("latin-1")


# --------------------------------------------------------------------------------------------------
# checksums
# --------------------------------------------------------------------------------------------------
def xor32(data: Union[bytes, bytearray, memoryview, mmap.mmap], start: int = 0, end: Optional[int] = None,
          chunk: int = 1 << 22) -> int:
    """XOR of all little-endian uint32 in data[start:end] (end-start must be a multiple of 4).

    This is exactly the header ``checksum`` (0x2F0): the H2Tool finalizer (sub_9B2B30) seeks to 0x800 and XORs
    every 1 MiB chunk (sub_8337C0 = XOR-fold of 8-byte lanes); the engine's map-copy state machine
    (halo2.exe 0x465963 case 12) recomputes it and compares with header+0x2F0.
    """
    import numpy as np  # local import: numpy is available in this project, keeps import light for callers
    if end is None:
        end = len(data)
    acc = 0
    pos = start
    while pos < end:
        n = min(chunk, end - pos)
        n -= n % 4
        if n == 0:
            break
        acc ^= int(np.bitwise_xor.reduce(np.frombuffer(data[pos:pos + n], dtype="<u4")))
        pos += n
    return acc


_CRC_TABLE: Optional[List[int]] = None


def _crc_table() -> List[int]:
    global _CRC_TABLE
    if _CRC_TABLE is None:
        t = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ 0xEDB88320 if (c & 1) else (c >> 1)
            t.append(c)
        _CRC_TABLE = t
    return _CRC_TABLE


def jamcrc_update(crc: int, data) -> int:
    """H2Tool ``crc_checksum_buffer`` (sub_7768A0): reflected CRC-32, poly 0xEDB88320, init 0xFFFFFFFF, NO final xor.

    ``crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)`` per byte.  Equivalent to ``zlib.crc32(data) ^ 0xFFFFFFFF``
    for a fresh start; continuation: ``jamcrc_update(c, d) == zlib.crc32(d, c ^ 0xFFFFFFFF) ^ 0xFFFFFFFF``.
    """
    import zlib
    return (zlib.crc32(bytes(data) if not isinstance(data, (bytes, bytearray, memoryview)) else data,
                       crc ^ 0xFFFFFFFF) & 0xFFFFFFFF) ^ 0xFFFFFFFF


# --------------------------------------------------------------------------------------------------
# raw (resource) pointers
# --------------------------------------------------------------------------------------------------
def decode_raw_pointer(p: int) -> Tuple[int, int]:
    """Split a 32-bit resource pointer (bitmap lod offset, sound chunk file_offset, geometry ...) into (db, offset).

    halo2.exe sub_430C8B / sub_430CC4: ``switch (p >> 30)`` 0 -> this map (-1), 1 -> mainmenu.map (0),
    2 -> shared.map (1), 3 -> single_player_shared.map (2); offset = ``p & 0x3FFFFFFF``.
    """
    p &= 0xFFFFFFFF
    return (p >> 30) - 1, p & 0x3FFFFFFF


def encode_raw_pointer(db: int, offset: int) -> int:
    if not 0 <= offset <= 0x3FFFFFFF:
        raise ValueError("raw offset out of 30-bit range: %#x" % offset)
    return ((db + 1) << 30) | offset


# --------------------------------------------------------------------------------------------------
# header
# --------------------------------------------------------------------------------------------------
# (name, offset, struct-format or 'bytes:N', description)
_HEADER_FIELDS: List[Tuple[str, int, str]] = [
    ("header_signature", 0x000, "4s"),
    ("version", 0x004, "i"),
    ("file_size", 0x008, "I"),
    ("field_C", 0x00C, "i"),
    ("tag_offset", 0x010, "I"),            # file offset of the tag region (tags header + tag data)  [Assembly: meta offset]
    ("data_offset", 0x014, "I"),           # size of the tags-header part of the region (4 KiB aligned) [tag data offset]
    ("data_size", 0x018, "I"),             # size of the tag-data part (4 KiB aligned)                   [tag data size]
    ("tag_size", 0x01C, "I"),              # data_offset + data_size (+ shared prefix)                   [meta size]
    ("tag_offset_mask", 0x020, "I"),       # virtual-address base of the tag region (0x1400000 MP, 0 otherwise)
    ("shared_tag_dependency_offset", 0x024, "I"),
    ("shared_tag_dependency_count", 0x028, "I"),
    ("path", 0x02C, "256s"),
    ("version_string", 0x12C, "32s"),      # build string
    ("type", 0x14C, "h"),                  # scenario type
    ("pad_14E", 0x14E, "h"),
    ("crc", 0x150, "I"),
    ("crc_valid", 0x154, "B"),
    ("pad_155", 0x155, "3s"),
    ("field_158", 0x158, "B"),
    ("tracked", 0x159, "B"),
    ("field_15A", 0x15A, "B"),
    ("field_15B", 0x15B, "B"),
    ("field_15C", 0x15C, "I"),
    ("low_detail_bitmap_count", 0x160, "I"),   # Assembly names; engine: field_160
    ("low_detail_bitmap_offset", 0x164, "I"),
    ("low_detail_bitmap_size", 0x168, "I"),
    ("string_block_offset", 0x16C, "I"),   # array of string_table_count 128-byte slots (debug string names)
    ("string_table_count", 0x170, "I"),
    ("string_table_size", 0x174, "I"),
    ("string_idx_offset", 0x178, "I"),
    ("string_table_offset", 0x17C, "I"),
    ("uses_shared_map", 0x180, "3B"),      # [mainmenu, shared(mp), single_player_shared]
    ("pad_183", 0x183, "B"),
    ("creation_time", 0x184, "Q"),         # FILETIME
    ("shared_creation_time", 0x18C, "3Q"),  # creation_time of the databases this map was built against
    ("name", 0x1A4, "32s"),
    ("language", 0x1C4, "i"),
    ("scenario_path", 0x1C8, "256s"),
    ("minor_version", 0x2C8, "i"),
    ("debug_tag_name_count", 0x2CC, "I"),
    ("tag_name_buffer_offset", 0x2D0, "I"),
    ("tag_name_buffer_size", 0x2D4, "I"),
    ("tag_name_offset", 0x2D8, "I"),       # file offset of the uint32[debug_tag_name_count] name-offset index
    ("language_pack_offset", 0x2DC, "I"),
    ("language_pack_size", 0x2E0, "I"),
    ("secondary_ugh_tag_index", 0x2E4, "I"),
    ("geometry_data_offset", 0x2E8, "I"),  # "raw table": region preloaded into RAM ("fast geometry load")
    ("geometry_data_size", 0x2EC, "I"),
    ("checksum", 0x2F0, "I"),
    ("mopp_checksum", 0x2F4, "I"),
    ("footer_signature", 0x7FC, "4s"),
]

_STR_FIELDS = {"path", "version_string", "name", "scenario_path"}


class VistaHeader:
    """The 0x800-byte header.  Attribute access for every field; ``raw`` keeps the untouched original bytes
    (including the 1284 reserved bytes at 0x2F8) so a parsed header can be re-serialised identically."""

    FIELDS = _HEADER_FIELDS

    def __init__(self, raw: Optional[bytes] = None):
        if raw is None:
            raw = bytearray(HEADER_SIZE)
            raw[0:4] = HEADER_SIGNATURE
            raw[0x7FC:0x800] = FOOTER_SIGNATURE
        if len(raw) != HEADER_SIZE:
            raise ValueError("header must be 0x800 bytes")
        object.__setattr__(self, "raw", bytearray(raw))

    # --- generic field access ------------------------------------------------------------------
    @classmethod
    def _spec(cls, name: str):
        for n, off, fmt in _HEADER_FIELDS:
            if n == name:
                return off, fmt
        raise AttributeError(name)

    def __getattr__(self, name: str):
        if name.startswith("_") or name == "raw":
            raise AttributeError(name)
        off, fmt = self._spec(name)
        v = struct.unpack_from("<" + fmt, self.raw, off)
        if name in _STR_FIELDS:
            return v[0].split(b"\0")[0].decode("latin-1")
        return v[0] if len(v) == 1 else tuple(v)

    def __setattr__(self, name: str, value):
        if name == "raw":
            object.__setattr__(self, name, value)
            return
        off, fmt = self._spec(name)
        if name in _STR_FIELDS:
            n = int(fmt[:-1])
            b = value.encode("latin-1") if isinstance(value, str) else bytes(value)
            if len(b) >= n:
                raise ValueError("%s too long (%d >= %d)" % (name, len(b), n))
            struct.pack_into("<" + fmt, self.raw, off, b.ljust(n, b"\0"))
        elif isinstance(value, (tuple, list)):
            struct.pack_into("<" + fmt, self.raw, off, *value)
        else:
            struct.pack_into("<" + fmt, self.raw, off, value)

    def pack(self) -> bytes:
        return bytes(self.raw)

    def as_dict(self) -> Dict[str, object]:
        return {n: getattr(self, n) for n, _, _ in _HEADER_FIELDS if not n.startswith("pad_")}

    def copy(self) -> "VistaHeader":
        return VistaHeader(bytes(self.raw))

    # --- derived -----------------------------------------------------------------------------
    @property
    def scenario_type_name(self) -> str:
        return SCENARIO_TYPES.get(self.type, str(self.type))

    def verify(self) -> List[str]:
        """Replicates halo2.exe cache_header_verify (0x4310E9) + the build-string check; returns problems."""
        problems = []
        if bytes(self.raw[0:4]) != HEADER_SIGNATURE:
            problems.append("bad header signature")
        if bytes(self.raw[0x7FC:0x800]) != FOOTER_SIGNATURE:
            problems.append("bad footer signature")
        if struct.unpack_from("<i", self.raw, 8)[0] < 0:
            problems.append("file_size negative")
        t = struct.unpack_from("<h", self.raw, 0x14C)[0]
        if not 0 <= t < 5:
            problems.append("scenario type %d outside [0,5)" % t)
        if b"\0" not in bytes(self.raw[0x1A4:0x1C4]):
            problems.append("name not NUL-terminated within 32 bytes")
        if struct.unpack_from("<i", self.raw, 4)[0] != VISTA_VERSION:
            problems.append("version != 8")
        if b"\0" not in bytes(self.raw[0x12C:0x14C]):
            problems.append("build string not NUL-terminated")
        elif self.version_string.lower() not in COMPATIBLE_BUILDS:
            problems.append("incompatible build string %r" % self.version_string)
        return problems


# --------------------------------------------------------------------------------------------------
# tag index structures
# --------------------------------------------------------------------------------------------------
@dataclass
class TagGroupLink:
    group: str            # 'bitm'
    parent: str           # '' when none
    grandparent: str      # '' when none
    raw: bytes = b""      # the 12 bytes as stored

    def pack(self) -> bytes:
        if self.raw:
            return self.raw
        g = fourcc(self.group) if self.group else b"\xff\xff\xff\xff"
        p = fourcc(self.parent) if self.parent else b"\xff\xff\xff\xff"
        gp = fourcc(self.grandparent) if self.grandparent else b"\xff\xff\xff\xff"
        return g + p + gp


@dataclass
class VistaTag:
    index: int                    # absolute tag index (0..tag_count-1)
    group: str                    # 'bitm' ('' for an empty slot)
    datum: int                    # full datum (salt<<16 | index), 0xFFFFFFFF for an empty slot
    addr: int                     # virtual address of the tag data (0 for shared placeholders / empty slots)
    size: int                     # size in bytes (4-byte padded, as stored)
    name: Optional[str] = None
    file_offset: Optional[int] = None   # file offset of the tag data (None if addr == 0)

    @property
    def is_empty(self) -> bool:
        return self.datum == 0xFFFFFFFF

    @property
    def is_shared(self) -> bool:
        """Instance whose data lives in a shared database (index >= 10000, addr == 0 in this file)."""
        return (not self.is_empty) and self.index >= FIRST_SHARED_TAG_INSTANCE_INDEX and self.addr == 0

    def pack(self) -> bytes:
        g = fourcc(self.group) if self.group else b"\xff\xff\xff\xff"
        return g + struct.pack("<III", self.datum, self.addr, self.size)


# --------------------------------------------------------------------------------------------------
# the reader
# --------------------------------------------------------------------------------------------------
class VistaCache:
    """Read-only view of a Halo 2 Vista (v8) .map file."""

    def __init__(self, path: str):
        self.path = path
        self._f = open(path, "rb")
        self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        self.size = len(self._mm)
        if self.size < HEADER_SIZE:
            raise ValueError("file smaller than a cache header")
        self.header = VistaHeader(bytes(self._mm[0:HEADER_SIZE]))
        h = self.header
        if bytes(h.raw[0:4]) != HEADER_SIGNATURE or h.version != VISTA_VERSION:
            raise ValueError("%s is not a Halo 2 Vista (v8) cache file" % path)
        # tag region
        self.tag_region_offset = h.tag_offset
        self.tag_mask = h.tag_offset_mask
        th = bytes(self._mm[h.tag_offset:h.tag_offset + h.data_offset])
        self.tags_header_bytes = th
        (self.group_table_off, self.group_count, self.instance_table_off, self.scenario_datum, self.globals_datum,
         self.tags_field_14, self.tag_count, sig) = struct.unpack_from("<IIIIIIII", th, 0)
        if struct.pack("<I", sig) != TAGS_SIGNATURE:
            raise ValueError("tags header signature mismatch")
        self.groups: List[TagGroupLink] = []
        for i in range(self.group_count):
            g, p, gp = struct.unpack_from("<4s4s4s", th, self.group_table_off + 12 * i)
            self.groups.append(TagGroupLink(fourcc_str(g), fourcc_str(p), fourcc_str(gp), th[self.group_table_off + 12 * i:self.group_table_off + 12 * i + 12]))
        self._names = self._load_tag_names()
        self.tags: List[VistaTag] = []
        for i in range(self.tag_count):
            g, datum, addr, sz = struct.unpack_from("<4sIII", th, self.instance_table_off + 16 * i)
            t = VistaTag(i, fourcc_str(g), datum, addr, sz)
            if datum != 0xFFFFFFFF and i < len(self._names):
                t.name = self._names[i]
            if addr:
                t.file_offset = self.addr_to_offset(addr)
            self.tags.append(t)
        self._string_idx: Optional[List[int]] = None
        self._string_tbl: Optional[bytes] = None

    # --- lifecycle -------------------------------------------------------------------------------
    def close(self):
        try:
            self._mm.close()
        finally:
            self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # --- addresses ---------------------------------------------------------------------------------
    def addr_to_offset(self, addr: int) -> int:
        """Tag-data virtual address -> file offset.  addr = tag_offset_mask + (offset - tag_offset)."""
        return self.header.tag_offset + (addr - self.header.tag_offset_mask)

    def offset_to_addr(self, offset: int) -> int:
        return self.header.tag_offset_mask + (offset - self.header.tag_offset)

    # --- raw ---------------------------------------------------------------------------------------
    def raw(self, offset: int, size: int) -> bytes:
        return bytes(self._mm[offset:offset + size])

    def resolve_raw_pointer(self, p: int) -> Tuple[int, int]:
        return decode_raw_pointer(p)

    # --- tags --------------------------------------------------------------------------------------
    def get_tag(self, key: Union[int, str, VistaTag]) -> VistaTag:
        if isinstance(key, VistaTag):
            return key
        if isinstance(key, int):
            return self.tags[key & 0xFFFF] if key > 0xFFFF else self.tags[key]   # full datum or plain index
        for t in self.tags:
            if t.name == key:
                return t
        raise KeyError(key)

    def find(self, group: Optional[str] = None, name: Optional[str] = None) -> List[VistaTag]:
        return [t for t in self.tags if not t.is_empty and (group is None or t.group == group) and (name is None or t.name == name)]

    def read_tag(self, tag: Union[int, VistaTag]) -> bytes:
        t = self.get_tag(tag)
        if not t.addr:
            raise ValueError("tag %d has no data in this file (shared placeholder or empty)" % t.index)
        return bytes(self._mm[t.file_offset:t.file_offset + t.size])

    def read_addr(self, addr: int, size: int) -> bytes:
        o = self.addr_to_offset(addr)
        return bytes(self._mm[o:o + size])

    # --- tag names ---------------------------------------------------------------------------------
    def _load_tag_names(self) -> List[str]:
        h = self.header
        cnt = h.debug_tag_name_count
        if cnt == 0 or h.tag_name_buffer_size == 0:
            return []
        idx = struct.unpack_from("<%dI" % cnt, self._mm, h.tag_name_offset)
        buf = bytes(self._mm[h.tag_name_buffer_offset:h.tag_name_buffer_offset + h.tag_name_buffer_size])
        out = []
        for o in idx:
            if o == 0xFFFFFFFF or o >= len(buf):
                out.append(None)
                continue
            e = buf.find(b"\0", o)
            out.append(buf[o:e].decode("latin-1"))
        return out

    # --- string ids --------------------------------------------------------------------------------
    def _load_strings(self):
        if self._string_idx is None:
            h = self.header
            self._string_idx = list(struct.unpack_from("<%dI" % h.string_table_count, self._mm, h.string_idx_offset))
            self._string_tbl = bytes(self._mm[h.string_table_offset:h.string_table_offset + h.string_table_size])

    def string_by_index(self, i: int) -> str:
        self._load_strings()
        o = self._string_idx[i]
        e = self._string_tbl.find(b"\0", o)
        return self._string_tbl[o:e].decode("latin-1")

    def string_id(self, sid: int) -> str:
        """string_id (high 8 bits = length, low 24 bits = index) -> text."""
        return self.string_by_index(sid & 0xFFFFFF)

    def all_strings(self) -> List[str]:
        self._load_strings()
        return [self.string_by_index(i) for i in range(self.header.string_table_count)]

    # --- layout ------------------------------------------------------------------------------------
    def sections(self) -> List[Tuple[str, int, int]]:
        """Named file regions in file order as described by the header (sizes are the *payload* sizes; each region
        starts on a 0x200 boundary).  The un-described regions are reported as 'raw_sounds' (between the header and
        the geometry region) and 'bitmap_pixels' (between the language pack and the tag region)."""
        h = self.header
        s: List[Tuple[str, int, int]] = [("header", 0, HEADER_SIZE)]
        marks = []
        if h.geometry_data_offset not in (0, 0xFFFFFFFF):
            marks.append(("geometry_blocks", h.geometry_data_offset, h.geometry_data_size))
        marks.append(("string_block", h.string_block_offset, h.string_table_count * 128))
        marks.append(("string_idx", h.string_idx_offset, 4 * h.string_table_count))
        marks.append(("string_table", h.string_table_offset, h.string_table_size))
        marks.append(("tag_names", h.tag_name_buffer_offset, h.tag_name_buffer_size))
        marks.append(("tag_name_idx", h.tag_name_offset, 4 * h.debug_tag_name_count))
        if h.language_pack_offset != 0xFFFFFFFF:
            marks.append(("language_pack", h.language_pack_offset, h.language_pack_size))
        if h.shared_tag_dependency_offset != 0xFFFFFFFF:
            marks.append(("shared_dependency_graph", h.shared_tag_dependency_offset, h.shared_tag_dependency_count))
        marks.append(("tag_region", h.tag_offset, h.tag_size))
        marks.sort(key=lambda m: m[1])
        cur = HEADER_SIZE
        for name, off, size in marks:
            if off > cur:
                gap_name = "raw_sounds" if cur == HEADER_SIZE else ("locale_data" if name == "language_pack" else
                                                                     ("bitmap_pixels" if name == "tag_region" else "gap"))
                s.append((gap_name, cur, off - cur))
            s.append((name, off, size))
            cur = max(cur, align_up(off + size, CACHE_FILE_PAGE_SIZE)) if name != "tag_region" else off + size
        return s


def reconstruct_write_items(c: "VistaCache") -> List[Tuple[int, int, str]]:
    """Recover the exact payload extents of every ``sub_9B27E0`` write H2Tool made for a stock *multiplayer* map
    (list of (file offset, payload size, label), sorted by offset).  This is what the header ``crc`` (0x150) runs over:
    JAMCRC of the payload bytes only (the zero padding to 0x200 after each write is NOT part of it).

    Sources of the item boundaries:
      * sound chunks   - the map's own ``ugh!`` chunk table (size = low 16 bits of the dword at +4)
      * geometry       - every (offset,size) dword pair in the tag data that points 0x200-aligned into the geometry region;
                         the unique chain that tiles the region exactly is the block list
      * strings/names  - sizes from the header
      * locale         - per language index table (8*count) + data (size) from the 252-byte language pack
      * bitmap pixels  - the local lod1..3 (offset,size) pairs of every bitmap
      * tag region     - one write of the whole 4 KiB padded region
    Verified by recomputing headlong's crc."""
    import numpy as np
    h = c.header
    items: List[Tuple[int, int, str]] = []
    R = {n: (o, s) for n, o, s in c.sections()}
    # sounds
    for t in c.find("ugh!"):
        if not t.addr or t.index >= FIRST_SHARED_TAG_INSTANCE_INDEX:
            continue
        d = c.read_tag(t)
        cnt, a = struct.unpack_from("<II", d, 0x40)
        base = c.addr_to_offset(a)
        for i in range(cnt):
            off, sz, _ = struct.unpack_from("<III", c.raw(base + 12 * i, 12), 0)
            db, o = decode_raw_pointer(off)
            if db == DB_LOCAL:
                items.append((o, sz & 0xFFFF, "snd"))
    # geometry
    if "geometry_blocks" in R:
        go, gs = R["geometry_blocks"]
        ge = go + gs
        td = c.raw(h.tag_offset + h.data_offset, h.data_size)
        arr = np.frombuffer(td[: len(td) // 4 * 4], dtype="<u4")
        cand: Dict[int, set] = {}
        m = (arr[:-1] >= go) & (arr[:-1] < ge) & ((arr[:-1] & 0x1FF) == 0) & (arr[1:] > 0) & (arr[1:].astype(np.uint64) + arr[:-1] <= ge)
        for i in np.nonzero(m)[0]:
            cand.setdefault(int(arr[i]), set()).add(int(arr[i + 1]))
        cset = set(cand)
        pos = go
        while pos < ge:
            opts = [s for s in sorted(cand.get(pos, ())) if align_up(pos + s, CACHE_FILE_PAGE_SIZE) in cset or align_up(pos + s, CACHE_FILE_PAGE_SIZE) == ge]
            if not opts:
                raise ValueError("geometry block chain broke at %#x" % pos)
            items.append((pos, opts[-1], "geom"))
            pos = align_up(pos + opts[-1], CACHE_FILE_PAGE_SIZE)
    # string/name tables
    items.append((h.string_block_offset, 128 * h.string_table_count, "strblock"))
    items.append((h.string_idx_offset, 4 * h.string_table_count, "stridx"))
    items.append((h.string_table_offset, h.string_table_size, "strtbl"))
    items.append((h.tag_name_buffer_offset, h.tag_name_buffer_size, "names"))
    items.append((h.tag_name_offset, 4 * h.debug_tag_name_count, "nameidx"))
    # locale
    if h.language_pack_offset != 0xFFFFFFFF:
        lp = c.raw(h.language_pack_offset, h.language_pack_size)
        for i in range(9):
            cnt, size, ioff, doff = struct.unpack_from("<IIII", lp, 28 * i + 8)
            if cnt or size:
                items.append((ioff, 8 * cnt, "loc_idx%d" % i))
                items.append((doff, size, "loc_dat%d" % i))
        items.append((h.language_pack_offset, h.language_pack_size, "langpack"))
    # bitmaps
    bm: Dict[int, int] = {}
    for t in c.find("bitm"):
        if not t.addr:
            continue
        d = c.read_tag(t)
        n, a = struct.unpack_from("<II", d, 0x44)
        b = c.addr_to_offset(a)
        for i in range(n):
            e = c.raw(b + 0x74 * i, 0x74)
            for lo, sz in ((0x1C, 0x34), (0x20, 0x38), (0x24, 0x3C)):
                p, s = struct.unpack_from("<I", e, lo)[0], struct.unpack_from("<I", e, sz)[0]
                db, o = decode_raw_pointer(p)
                if db == DB_LOCAL and p and s:
                    bm[o] = max(bm.get(o, 0), s)
    items += [(o, s, "bitmap") for o, s in sorted(bm.items())]
    if h.shared_tag_dependency_offset != 0xFFFFFFFF:
        items.append((h.shared_tag_dependency_offset, h.shared_tag_dependency_count, "shared_graph"))
    items.append((h.tag_offset, h.tag_size, "tags"))
    items.sort()
    return items


def mopp_checksum_of(c: "VistaCache", tag_blobs: Optional[Dict[int, bytes]] = None) -> int:
    """Header ``mopp_checksum`` (0x2F4) = XOR32 of the bytes of the Havok MOPP-code data references of every *local*
    tag (H2Tool sub_844620 / sub_844C80; XOR is order independent), cache-layout offsets from the Assembly plugins:

        sbsp  root  MOPP Codes @0x1E0, Breakable Surface MOPP Codes @0x204
        sbsp  Clusters (block @0x9C, 0xB0 each)  Collision MOPP Codes @+0xA8
        phmo  root  MOPP Codes @0xA8
        coll  Regions @0x1C (0xC) -> Permutations @+4 (0x14) -> BSP Physics @+0xC (0x70) -> MOPP Code Data @+0x64

    (the visibility-MOPP / lightmap / model entries H2Tool also lists are empty in cache files).  Verified equal to the
    stored value for 24 of 25 stock multiplayer maps (tests).  Shared databases store 0."""
    import numpy as np

    def x32b(b: bytes) -> int:
        n = len(b) // 4
        return int(np.bitwise_xor.reduce(np.frombuffer(b[:n * 4], dtype="<u4"))) if n else 0

    def dref(blob: bytes, off: int) -> int:
        size, addr = struct.unpack_from("<II", blob, off)
        if size <= 0 or size > 0x1000000 or not addr:
            return 0
        return x32b(c.read_addr(addr, size))

    def block(blob: bytes, off: int, esize: int):
        cnt, addr = struct.unpack_from("<II", blob, off)
        if cnt <= 0 or cnt > 200000 or not addr:
            return []
        raw = c.read_addr(addr, cnt * esize)
        return [raw[i * esize:(i + 1) * esize] for i in range(cnt)]

    acc = 0
    for t in c.tags:
        if t.is_empty or not t.addr or t.index >= FIRST_SHARED_TAG_INSTANCE_INDEX:
            continue
        if t.group == "sbsp":
            d = c.read_tag(t)
            acc ^= dref(d, 0x1E0) ^ dref(d, 0x204)
            for cl in block(d, 0x9C, 0xB0):
                acc ^= dref(cl, 0xA8)
        elif t.group == "phmo":
            acc ^= dref(c.read_tag(t), 0xA8)
        elif t.group == "coll":
            d = c.read_tag(t)
            for reg in block(d, 0x1C, 0xC):
                for perm in block(reg, 4, 0x14):
                    for ph in block(perm, 0xC, 0x70):
                        acc ^= dref(ph, 0x64)
    return acc


def crc_from_items(c: "VistaCache", items: Iterable[Tuple[int, int, str]]) -> Tuple[int, List[str]]:
    """JAMCRC over the payload bytes of ``items`` in file order; also returns problems (overlap, non-zero padding or
    non-zero bytes between items).  For a stock multiplayer map the result equals ``header.crc``."""
    crc = 0xFFFFFFFF
    prev_end = HEADER_SIZE
    problems: List[str] = []
    for off, size, label in sorted(items):
        if off < prev_end:
            problems.append("overlap at %#x (%s)" % (off, label))
            continue
        if off > prev_end and any(c.raw(prev_end, off - prev_end)):
            problems.append("non-zero bytes outside any item in [%#x, %#x) before %s" % (prev_end, off, label))
        crc = jamcrc_update(crc, c.raw(off, size))
        end = off + size if label == "tags" else align_up(off + size, CACHE_FILE_PAGE_SIZE)
        if any(c.raw(off + size, end - off - size)):
            problems.append("non-zero padding after %s item at %#x" % (label, off))
        prev_end = end
    return crc, problems
