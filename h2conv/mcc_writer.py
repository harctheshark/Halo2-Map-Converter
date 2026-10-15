# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Halo 2 MCC (cache v13) writer - the mirror image of ``vista_writer`` for the Vista -> MCC (reverse) direction.

The layout, the alignment rules and every derived header field were verified by re-serialising stock MCC maps (all 43 stock
maps, decompressed) and the H2EK-built self-contained maps byte-for-byte (all of them), by reproducing
the stock *compressed* files from their decompressed form, and by decompiling MCC ``tool.exe`` (cache_file_builder.cpp,
the writer) and ``halo2.dll`` (the loader).

File order (every region starts on a 0x80 boundary and is zero padded to 0x80 after its payload):

    0x000  header (0x380)                                         written LAST by the tool, here too
           sounds        in-map sound chunks (H2EK style)         [0x380, header 0x2F0)       empty for stock-style maps
           geometry      'blkh' blocks ("raw table")              header 0x2F0 / 0x2F4
           string_idx    uint32[string_count]                     header 0x3C (count 0x30)
           string_data   NUL-terminated strings                   header 0x34 / 0x38
           tag_names     NUL-terminated tag names                 header 0x24 / 0x28
           tag_name_idx  uint32[tag_count]  (-1 = unnamed)        header 0x2C (count 0x20)
           locale        per-language index table + data blobs    (offsets live in matg+0x188 (+0x1C per language) or in locale_globals)
           locale_glob.  252-byte table of 9 x 28-byte entries    header 0x2E4 / 0x2E8   (stock MP maps only, else -1 / 0)
           bitmaps       in-map bitmap blobs (H2EK style)         (offsets live in the bitm elements)
           shared_graph  shared tag dependency graph              header 0x2DC / 0x2E0   (stock shared.map only, else -1 / 0)
           meta          'tags' header + 120 group links + 16-byte instances, pad to 4 KiB, tag data, pad to 4 KiB
                         header 0x10 (offset) 0x14 (size) 0x2D0 (mask) 0x2D4 (tag data offset) 0x2D8 (tag data size)

Header fields derived here: file_size, meta/tag-data fields, string / tag-name / locale / raw-table offsets, ``checksum``
(0x2F8 = XOR32 of every dword of [0x380, EOF)) and ``crc`` (0x2FC = running CRC, poly 0xEDB88320, init 0xFFFFFFFF, no final
xor, over the *payload* bytes of every write the tool makes, padding excluded).  Everything else is a plain input of
``MccHeader`` (very few of them are read by halo2.dll at all).

``write_image(..., compress=True)`` / ``compress_file`` produce the h2mcc compressed form: the body [0x380, EOF) cut into
0x40000-byte chunks, each deflated with ``zlib.compressobj(9, DEFLATED, 10, 8, Z_FIXED)`` (reproduces the stock files bit
for bit) or stored raw (negative size) when that does not shrink it, chunk table {int32 size, u32 offset} at EOF.
"""
from __future__ import annotations

import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
except Exception:  # not Windows
    pass

import hashlib
import os
import struct
import sys
import zlib
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

try:
    import numpy as np
except Exception:  # numpy is installed on the project machine; keep a pure-python fallback anyway
    np = None

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
STRING_PREFIX_FILE = os.path.join(HERE, "rev", "data", "mcc_engine_string_ids.txt")

HEADER_SIZE = 0x380
PAGE = 0x80                       # every region start and every payload is padded to 0x80 (CACHE_FILE_PAGE_SIZE of the MCC tool)
META_ALIGN = 0x1000               # tag region parts (data_offset, data_size) are 4 KiB aligned
MCC_VERSION = 13
NONE32 = 0xFFFFFFFF
HEAD_MAGIC = b"daeh"              # 'head' stored little endian
FOOT_MAGIC = b"toof"
TAGS_SIGNATURE = struct.pack("<I", 0x74616773)    # 'tags'
CHUNK_SIZE = 0x40000              # h2mcc compression chunk size (header 0x308)

#: first N strings of EVERY MCC string table are the engine's hard-coded string ids (halo2.dll uses 947 distinct ids as code
#: immediates, all with index <= 2462; identical in all 43 stock maps and all 24 H2EK-built maps).
ENGINE_STRING_COUNT = 2463
MAX_STRING_IDS = 24576            # static index buffer 0x18000 bytes in halo2.dll
MAX_STRING_BYTES = 0x60000        # static text buffer (>= 0x76D20 bytes exist in halo2.dll; 0x60000 is the Vista/engine value)

SCENARIO_TYPES = {0: "solo", 1: "multiplayer", 2: "mainmenu", 3: "multiplayer_shared", 4: "single_player_shared"}

# header flags (u16 @0x1C)
FLAG_COMPRESSED = 0x01            # body is chunk-compressed (engine: sub_1806C95C0 / 18067C4D0)
FLAG_SHARED = 0x04                # tool global byte_11C6ADD (set only while building shared.map: 0x1D); not read by the engine
FLAG_EXTERNAL_DAT = 0x08          # tool option 8 "external resources"; stock maps carry it (0x19/0x09); the engine decides by the store mask instead
FLAG_NOT_OPT10 = 0x10             # set when tool option 0x10 is CLEAR (H2EK default); engine: sub_18067BDC0 "game_is_tool_or_editor_mode" == (flags & 0x10)
# store mask (u16 @0x1E): bit k = resource store k is EXTERNAL.  0 = everything is inside the map (H2EK style).
STORE_MAINMENU, STORE_SHARED, STORE_SP_SHARED, STORE_TEXTURES, STORE_SOUNDS_NEUTRAL, STORE_SOUNDS_LANG, STORE_SOUNDS_REMASTERED = range(7)


class MccWriterError(ValueError):
    pass


# --------------------------------------------------------------------------------------------------
# small helpers ---------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
def align_up(x: int, a: int = PAGE) -> int:
    return (x + a - 1) // a * a


def fourcc_int(text: Optional[str]) -> int:
    """'scnr' -> the u32 stored in the file ('rncs' bytes)."""
    if text is None:
        return NONE32
    return struct.unpack("<I", text.encode("latin-1")[::-1])[0]


def fourcc(value: int) -> Optional[str]:
    if value == NONE32:
        return None
    return struct.pack("<I", value)[::-1].decode("latin-1")


class Xor32:
    """Streaming XOR of little-endian dwords (the tool's 'realtime data checksum', header 0x2F8).  Accepts pieces of any
    length (a partial trailing dword is carried into the next piece)."""

    def __init__(self):
        self.acc = 0
        self._tail = b""

    def feed(self, data) -> None:
        mv = memoryview(data).cast("B")
        if self._tail:
            need = 4 - len(self._tail)
            if len(mv) < need:
                self._tail += bytes(mv)
                return
            self.acc ^= int.from_bytes(self._tail + bytes(mv[:need]), "little")
            mv = mv[need:]
            self._tail = b""
        n = len(mv) // 4
        if n:
            step = 1 << 22
            for s in range(0, n, step):
                e = min(n, s + step)
                seg = mv[s * 4:e * 4]
                if np is not None:
                    self.acc ^= int(np.bitwise_xor.reduce(np.frombuffer(seg, dtype="<u4")))
                else:
                    for (v,) in struct.iter_unpack("<I", seg):
                        self.acc ^= v
        if len(mv) - n * 4:
            self._tail = bytes(mv[n * 4:])

    def value(self) -> int:
        acc = self.acc
        if self._tail:
            acc ^= int.from_bytes(self._tail + b"\0" * (4 - len(self._tail)), "little")
        return acc & 0xFFFFFFFF


def xor32(data) -> int:
    x = Xor32()
    x.feed(data)
    return x.value()


def crc_update(state: int, data) -> int:
    """Running CRC state update of the MCC tool (sub_664270: init 0xFFFFFFFF, reflected poly 0xEDB88320, NO final xor)."""
    mv = memoryview(data).cast("B")
    step = 1 << 24
    for s in range(0, len(mv), step):
        state = (zlib.crc32(mv[s:s + step], state ^ 0xFFFFFFFF) & 0xFFFFFFFF) ^ 0xFFFFFFFF
    return state


# --------------------------------------------------------------------------------------------------
# header ---------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
def _u(off, fmt):
    size = struct.calcsize(fmt)

    def get(self):
        return struct.unpack_from("<" + fmt, self.raw, off)[0]

    def set_(self, v):
        struct.pack_into("<" + fmt, self.raw, off, v & ((1 << (8 * size)) - 1) if fmt in "BHIQ" else v)
    return property(get, set_)


def _cs(off, size):
    def get(self):
        b = bytes(self.raw[off:off + size])
        n = b.find(b"\0")
        return (b if n < 0 else b[:n]).decode("latin-1")

    def set_(self, v):
        b = v.encode("latin-1")
        if len(b) >= size:
            raise MccWriterError("string %r too long for a %d-byte header field (needs a NUL)" % (v, size))
        self.raw[off:off + size] = b + b"\0" * (size - len(b))
    return property(get, set_)


class MccHeader:
    """The 0x380-byte v13 header.  ``raw`` is the authoritative bytearray; properties read / write it.
    """

    signature = property(lambda self: bytes(self.raw[0:4])[::-1].decode("latin-1"))
    version = _u(0x04, "I")
    file_size = _u(0x08, "I")            # total size of the UNcompressed file (also stored in compressed files)
    minor = _u(0x0C, "H")                # 0x0201 in every map; the engine only checks the low byte == 1
    meta_offset = _u(0x10, "I")
    meta_size = _u(0x14, "I")
    type = _u(0x18, "H")                 # 0 solo, 1 multiplayer, 2 mainmenu, 3 mp shared, 4 sp shared  (engine: <= 4)
    flags = _u(0x1C, "H")
    store_mask = _u(0x1E, "H")           # bit k set = resource store k external (see STORE_*); 0 for self-contained maps
    file_table_count = _u(0x20, "i")     # number of tag slots == number of tag-name index entries
    file_table_offset = _u(0x24, "I")
    file_table_size = _u(0x28, "i")
    file_index_offset = _u(0x2C, "I")
    string_count = _u(0x30, "i")
    string_offset = _u(0x34, "I")
    string_size = _u(0x38, "i")
    string_index_offset = _u(0x3C, "I")
    word_40 = _u(0x40, "I")              # tool writes language / minor-version values here; 0 in every map seen
    word_44 = _u(0x44, "I")
    creation_time = _u(0x48, "Q")        # FILETIME; 0 when the tool ran without timestamps; never read by the engine
    shared_creation_time0 = _u(0x50, "Q")   # [mainmenu, mp-shared, sp-shared]; all 0 for self-contained maps
    shared_creation_time1 = _u(0x58, "Q")
    shared_creation_time2 = _u(0x60, "Q")
    build_string = _cs(0x90, 0x20)       # empty in every map; engine: strlen < 32
    name = _cs(0xB0, 0x20)               # internal map name; engine: strlen < 32
    scenario_path = _cs(0xD0, 0x100)     # copied by the engine into saved-game headers
    mask = _u(0x2D0, "I")                # tag memory base address (0x3200000 for MP maps linked to shared.map, else 0)
    tag_data_offset = _u(0x2D4, "I")     # relative to the meta start
    tag_data_size = _u(0x2D8, "i")
    shared_graph_offset = _u(0x2DC, "I")  # -1 unless this is shared.map
    shared_graph_size = _u(0x2E0, "I")
    locale_offset = _u(0x2E4, "I")       # -1 = absent (every self-contained map)
    locale_size = _u(0x2E8, "I")         # 0xFC when present
    gestalt_datum = _u(0x2EC, "I")       # datum of the map's secondary ugh!, -1 = primary gestalt only
    raw_offset = _u(0x2F0, "I")
    raw_size = _u(0x2F4, "i")
    checksum = _u(0x2F8, "I")
    crc = _u(0x2FC, "I")
    mopp_checksum = _u(0x300, "I")       # only tool runs that build against shared.map; 0 for self-contained maps
    compression_chunk_size = _u(0x308, "I")
    compression_data_offset = _u(0x30C, "I")
    compression_table_offset = _u(0x310, "I")
    compression_chunk_count = _u(0x314, "I")

    def __init__(self, raw: Optional[bytes] = None):
        self.raw = bytearray(raw[:HEADER_SIZE]) if raw is not None else bytearray(HEADER_SIZE)
        if len(self.raw) != HEADER_SIZE:
            raise MccWriterError("header must be 0x380 bytes")

    def copy(self) -> "MccHeader":
        return MccHeader(bytes(self.raw))

    def pack(self) -> bytes:
        return bytes(self.raw)

    @property
    def compressed(self) -> bool:
        return bool(self.flags & FLAG_COMPRESSED)

    @property
    def shared_creation_time(self) -> Tuple[int, int, int]:
        return (self.shared_creation_time0, self.shared_creation_time1, self.shared_creation_time2)

    @shared_creation_time.setter
    def shared_creation_time(self, v):
        self.shared_creation_time0, self.shared_creation_time1, self.shared_creation_time2 = v

    def engine_problems(self) -> List[str]:
        """The checks halo2.dll sub_1806C9970 makes on the header (and nothing else): MCC accepts any header that passes these."""
        r, p = self.raw, []
        if bytes(r[0:4]) != HEAD_MAGIC:
            p.append("signature != 'head'")
        if bytes(r[0x37C:0x380]) != FOOT_MAGIC:
            p.append("footer != 'foot'")
        if r[0x0C] != 1:
            p.append("byte @0x0C != 1")
        if self.file_size >= 0x80000000:
            p.append("file_size is negative as int32")
        if self.type > 4:
            p.append("scenario type > 4")
        if self.version != MCC_VERSION:
            p.append("version != 13")
        for off, nm in ((0xB0, "name"), (0x90, "build string")):
            if 0 not in bytes(r[off:off + 32]):
                p.append("%s not NUL terminated within 32 bytes" % nm)
        return p


def new_header(name: str, scenario_path: str, map_type: int = 1, creation_time: Optional[int] = None,
               flags: int = FLAG_NOT_OPT10) -> MccHeader:
    """Header of a new SELF-CONTAINED map with the H2EK conventions (verified on 25 H2EK-built maps): flags 0x10, store mask 0,
    no shared creation times, mopp checksum 0, locale / shared graph / gestalt fields -1, mask 0 until ``MccImage`` sets it."""
    if len(name.encode("latin-1")) >= 32:
        raise MccWriterError("map name must be < 32 characters")
    h = MccHeader()
    h.raw[0:4] = HEAD_MAGIC
    h.raw[0x37C:0x380] = FOOT_MAGIC
    h.version = MCC_VERSION
    h.minor = 0x0201
    h.type = map_type
    h.flags = flags
    h.store_mask = 0
    h.name = name
    h.scenario_path = scenario_path
    h.creation_time = creation_time if creation_time is not None else filetime_now()
    h.shared_graph_offset = NONE32
    h.locale_offset = NONE32
    h.gestalt_datum = NONE32
    return h


def filetime_now() -> int:
    import time
    return int(time.time() * 10_000_000) + 116444736000000000


# --------------------------------------------------------------------------------------------------
# region builder ---------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
class RegionBuilder:
    """Sequential 0x80-padded writes that remember their payload sizes (for the CRC).  ``add`` returns the offset relative
    to the region start; with a known region base the absolute file offset is ``base + rel``.  Parts may be bytes or
    memoryviews (zero-copy slices of a mapped file)."""

    def __init__(self):
        self.parts: List[object] = []
        self.payload_sizes: List[int] = []
        self.size = 0
        self._dedupe: Dict[bytes, int] = {}

    def add(self, data, dedupe: bool = False) -> int:
        n = len(data)
        if dedupe:
            key = hashlib.md5(data).digest() + n.to_bytes(4, "little")
            hit = self._dedupe.get(key)
            if hit is not None:
                return hit
        rel = self.size
        self.parts.append(data)
        pad = (-n) % PAGE
        if pad:
            self.parts.append(b"\0" * pad)
        self.payload_sizes.append(n)
        self.size += n + pad
        if dedupe:
            self._dedupe[key] = rel
        return rel

    def __len__(self):
        return self.size

    def pieces(self) -> List[object]:
        return self.parts

    def data(self) -> bytes:
        return b"".join(bytes(p) for p in self.parts)

    def payloads(self) -> Iterator[object]:
        """The payload byte ranges in write order (padding excluded)."""
        # parts alternate payload / optional pad part; walk by payload sizes
        idx = 0
        for n in self.payload_sizes:
            blob = self.parts[idx]
            idx += 1
            if (-n) % PAGE:
                idx += 1
            yield blob

    @classmethod
    def from_view(cls, mv, base: int, items: Sequence[Tuple[int, int]]) -> "RegionBuilder":
        """Wrap an existing padded region (a memoryview over exactly the region bytes) that starts at absolute file offset
        ``base``; ``items`` = (absolute offset, payload size) of the writes it is made of.  They must tile the region."""
        rb = cls()
        pos = base
        for off, size in sorted(items):
            if off != pos:
                raise MccWriterError("items do not tile the region at %#x (next item at %#x)" % (pos, off))
            rb.payload_sizes.append(size)
            pos = align_up(off + size)
        if pos - base != len(mv):
            raise MccWriterError("items cover %#x bytes but the region has %#x" % (pos - base, len(mv)))
        rb.parts = [mv]
        rb.size = len(mv)
        return rb


Region = Union[bytes, bytearray, memoryview, RegionBuilder, None]


def _region_len(r: Region) -> int:
    return 0 if r is None else len(r)


def _region_pieces(r: Region) -> List[object]:
    if r is None:
        return []
    if isinstance(r, RegionBuilder):
        return r.pieces()
    return [r] if len(r) else []


def _region_payloads(r: Region) -> List[object]:
    """CRC payload items of a region: the individual writes of a RegionBuilder, else the whole blob as ONE write."""
    if r is None or not len(r):
        return []
    if isinstance(r, RegionBuilder):
        # a wrapped view holds many payloads in one part: split by payload sizes
        out, pos = [], 0
        if len(r.parts) == 1 and len(r.payload_sizes) > 1:
            mv = memoryview(r.parts[0])
            for n in r.payload_sizes:
                out.append(mv[pos:pos + n])
                pos += align_up(n)
            return out
        return list(r.payloads())
    return [r]


# --------------------------------------------------------------------------------------------------
# packers ---------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
def pack_string_table(strings: Sequence[str]) -> Tuple[bytes, bytes]:
    """-> (index bytes uint32[n], text bytes).  Strings NUL-terminated in index order; idx[j] = running offset (verified)."""
    if len(strings) > MAX_STRING_IDS:
        raise MccWriterError("too many string ids (%d > %d)" % (len(strings), MAX_STRING_IDS))
    offs, parts, cur = [], [], 0
    for s in strings:
        b = s.encode("latin-1") + b"\0"
        offs.append(cur)
        parts.append(b)
        cur += len(b)
    if cur >= MAX_STRING_BYTES:
        raise MccWriterError("string table too large (%#x >= %#x)" % (cur, MAX_STRING_BYTES))
    return struct.pack("<%dI" % len(offs), *offs), b"".join(parts)


def pack_tag_names(names: Sequence[Optional[str]], order: Optional[Sequence[int]] = None) -> Tuple[bytes, bytes]:
    """-> (name buffer, index bytes uint32[len(names)]).  ``order`` = tag indices in buffer order (default ascending index);
    unnamed tags hold 0xFFFFFFFF."""
    buf = bytearray()
    offs: Dict[int, int] = {}
    seq = order if order is not None else [i for i, n in enumerate(names) if n is not None]
    for i in seq:
        n = names[i]
        if n is None:
            raise MccWriterError("tag_name_order lists unnamed tag %d" % i)
        offs[i] = len(buf)
        buf += n.encode("latin-1") + b"\0"
    idx = [offs.get(i, NONE32) for i in range(len(names))]
    return bytes(buf), struct.pack("<%dI" % len(idx), *idx)


def pack_language(entries: Sequence[Tuple[int, str]], index_offset: int) -> Tuple[Dict[str, int], bytes]:
    """One language of the locale area: ``entries`` = [(string_id, utf-8 text)] -> (descriptor, blob).  The blob is
    ``index table (count x {string_id, offset into data}) | pad to 0x80 | data (NUL terminated UTF-8) | pad to 0x80``;
    ``index_offset`` is the ABSOLUTE file offset the blob will be placed at (0x80 aligned).  Empty language: descriptor with
    count 0 and both offsets = ``index_offset``, blob b''.  descriptor keys: string_count, table_size, index_table_offset,
    data_offset (the 4 fields of the 28-byte entry)."""
    d, idx, data = pack_language_parts(entries, index_offset)
    blob = idx + b"\0" * ((-len(idx)) % PAGE) + data + b"\0" * ((-len(data)) % PAGE)
    return d, blob


def pack_language_parts(entries: Sequence[Tuple[int, str]], index_offset: int) -> Tuple[Dict[str, int], bytes, bytes]:
    """-> (descriptor, index table bytes, data bytes) of one language; the tool writes the two as SEPARATE 0x80-padded writes
    (this matters for the header CRC: see ``pack_locale_region``)."""
    if index_offset % PAGE:
        raise MccWriterError("language blobs must start 0x80 aligned")
    if not entries:
        return ({"string_count": 0, "table_size": 0, "index_table_offset": index_offset, "data_offset": index_offset}, b"", b"")
    idx, data, cur = bytearray(), bytearray(), 0
    for sid, text in entries:
        idx += struct.pack("<II", sid, cur)
        b = text.encode("utf-8") + b"\0"
        data += b
        cur += len(b)
    d = {"string_count": len(entries), "table_size": len(data), "index_table_offset": index_offset,
         "data_offset": index_offset + align_up(len(idx))}
    return d, bytes(idx), bytes(data)


def language_entry_bytes(d: Dict[str, int]) -> bytes:
    """The 28-byte language entry {ptr, ptr, count, table_size, index_offset, data_offset, 0xCDCDCD00} (runtime pointers 0)."""
    return struct.pack("<IIIIIII", 0, 0, d["string_count"], d["table_size"], d["index_table_offset"], d["data_offset"], 0xCDCDCD00)


def pack_locale_area(languages: Sequence[Sequence[Tuple[int, str]]], base_offset: int) -> Tuple[bytes, List[Dict[str, int]]]:
    """All 9 languages back to back starting at the absolute, 0x80-aligned file offset ``base_offset`` (= ``MccBuilder.locale_base``).
    -> (blob, [9 descriptors]).  An empty language gets count 0 and index = data offset = the running offset (verified on the
    stock ninth language and on every language the H2EK tool leaves empty)."""
    if len(languages) != 9:
        raise MccWriterError("the locale area needs 9 languages (empty lists for absent ones)")
    cur, blobs, descs = base_offset, [], []
    for entries in languages:
        d, blob = pack_language(entries, cur)
        descs.append(d)
        blobs.append(blob)
        cur += len(blob)
    return b"".join(blobs), descs


def pack_locale_region(languages: Sequence[Sequence[Tuple[int, str]]], base_offset: int) -> Tuple["RegionBuilder", List[Dict[str, int]]]:
    """Like ``pack_locale_area`` but returns a ``RegionBuilder`` whose writes are the index table and the data blob of every
    non-empty language, in the tool's order - hand THIS to ``MccBuilder.set_locale`` so the header CRC (0x2FC) comes out as the
    tool's.  -> (region, [9 descriptors])."""
    if len(languages) != 9:
        raise MccWriterError("the locale area needs 9 languages (empty lists for absent ones)")
    rb = RegionBuilder()
    cur, descs = base_offset, []
    for entries in languages:
        d, idx, data = pack_language_parts(entries, cur)
        descs.append(d)
        if idx:
            rb.add(idx)
            rb.add(data)
        cur = base_offset + len(rb)
    return rb, descs


def pack_locale_globals(descs: Sequence[Dict[str, int]]) -> bytes:
    """The 252-byte header locale block (header 0x2E4 / 0x2E8, stock MP maps): 9 language entries of 28 bytes."""
    if len(descs) != 9:
        raise MccWriterError("locale globals need 9 language entries")
    return b"".join(language_entry_bytes(d) for d in descs)


# --------------------------------------------------------------------------------------------------
# string-id table (rules for a self-contained MCC map) ---------------------------------------------------
# --------------------------------------------------------------------------------------------------
def engine_string_prefix() -> List[str]:
    """The 2463 engine string ids (indices 0..2462, id = (len << 24) | index) every MCC table must start with.  Read from
    h2conv/rev/data/mcc_engine_string_ids.txt (one string per line, line i = index i; line 0 is the empty string); if the data
    file is missing it is extracted from the stock MCC shared.map."""
    try:
        with open(STRING_PREFIX_FILE, "r", encoding="latin-1", newline="") as f:
            lines = f.read().split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if len(lines) == ENGINE_STRING_COUNT:
            return lines
    except OSError:
        pass
    return extract_engine_string_prefix()


def extract_engine_string_prefix(shared_map: Optional[str] = None) -> List[str]:
    from . import mcc_cache, paths
    path = shared_map or os.path.join(paths.mcc_maps_dir(), "shared.map")
    c = mcc_cache.MccCache(path, load_names=False)
    try:
        return list(c.string_ids[:ENGINE_STRING_COUNT])
    finally:
        c.close()


def write_engine_string_prefix(path: str = STRING_PREFIX_FILE, strings: Optional[Sequence[str]] = None) -> int:
    strings = list(strings) if strings is not None else extract_engine_string_prefix()
    if len(strings) != ENGINE_STRING_COUNT:
        raise MccWriterError("expected %d strings" % ENGINE_STRING_COUNT)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="latin-1", newline="") as f:
        f.write("\n".join(strings) + "\n")
    return len(strings)


class StringTableError(MccWriterError):
    pass


class StringTable:
    """String-id table of a self-contained MCC map: the engine prefix (frozen) + the map's own strings.

        id = (len(text) << 24) | index;  limits: <= 24576 ids, text < 0x60000 bytes (static buffers of halo2.dll).
    """

    def __init__(self, base_strings: Optional[Sequence[str]] = None, max_ids: int = MAX_STRING_IDS, max_bytes: int = MAX_STRING_BYTES):
        self.max_ids, self.max_bytes = max_ids, max_bytes
        self._strings = list(base_strings) if base_strings is not None else engine_string_prefix()
        self.base_count = len(self._strings)
        self._index: Dict[str, int] = {}
        self._bytes = 0
        for i, s in enumerate(self._strings):
            self._index.setdefault(s, i)
            self._bytes += self._blen(s) + 1
        self._check()

    @staticmethod
    def _blen(text: str) -> int:
        try:
            n = len(text.encode("latin-1"))
        except UnicodeEncodeError:
            raise StringTableError("string %r is not latin-1 encodable" % text)
        if n > 255:
            raise StringTableError("string of %d bytes does not fit the 8-bit length of a string id" % n)
        return n

    def _check(self):
        if len(self._strings) > self.max_ids:
            raise StringTableError("too many string ids: %d > %d" % (len(self._strings), self.max_ids))
        if self._bytes >= self.max_bytes:
            raise StringTableError("string text %#x bytes >= %#x" % (self._bytes, self.max_bytes))

    @property
    def strings(self) -> List[str]:
        return self._strings

    def __len__(self):
        return len(self._strings)

    def has(self, text: str) -> bool:
        return text in self._index

    def sid(self, text: str) -> int:
        """Full 32-bit string id of ``text`` (appended when missing)."""
        if text == "":
            return 0
        i = self._index.get(text)
        if i is None:
            n = self._blen(text)
            i = len(self._strings)
            self._strings.append(text)
            self._index[text] = i
            self._bytes += n + 1
            self._check()
        return (self._blen(text) << 24) | i

    def text(self, sid: int) -> str:
        if sid in (0, NONE32):
            return ""
        i = sid & 0xFFFFFF
        if i >= len(self._strings):
            raise StringTableError("string id %#x index out of range" % sid)
        s = self._strings[i]
        if (sid >> 24) != self._blen(s):
            raise StringTableError("string id %#x length byte != len(%r)" % (sid, s))
        return s

    def is_prefix_ok(self) -> bool:
        return self._strings[:ENGINE_STRING_COUNT] == engine_string_prefix()


# --------------------------------------------------------------------------------------------------
# tag region -----------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
@dataclass
class MccTagInstance:
    """One 16-byte tag instance {group, datum, addr, size}; ``group`` None / datum 0xFFFFFFFF = null slot."""
    index: int
    group: Optional[str]
    datum: int
    addr: int
    size: int
    name: Optional[str] = None

    def pack(self) -> bytes:
        return struct.pack("<IIII", fourcc_int(self.group), self.datum, self.addr, self.size)

    @staticmethod
    def null(index: int) -> "MccTagInstance":
        return MccTagInstance(index, None, NONE32, 0, 0)


@dataclass
class GroupLink:
    group: int
    parent: int
    grandparent: int

    def pack(self) -> bytes:
        return struct.pack("<III", self.group, self.parent, self.grandparent)


GROUPS_FILE = os.path.join(HERE, "rev", "data", "mcc_tag_groups.json")


def default_groups() -> List[GroupLink]:
    """The 120-entry tag group table.  It is identical in all 67 MCC maps examined (43 stock + 24 H2EK-built) and equals the Vista
    table; loaded from h2conv/rev/data/mcc_tag_groups.json."""
    import json
    with open(GROUPS_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)
    return [GroupLink(*g) for g in d["groups"]]


def groups_from_stock(path: str) -> List[GroupLink]:
    """The 120-entry tag group table (identical in every v13 map)."""
    from . import mcc_cache
    c = mcc_cache.MccCache(path, load_names=False)
    try:
        mm = c.mm
        mo = c.meta_header["tag_group_table_offset"]
        n = c.meta_header["number_of_tag_groups"]
        return [GroupLink(*struct.unpack_from("<III", mm, c.meta_offset + mo + 12 * i)) for i in range(n)]
    finally:
        c.close()


def build_meta_header(groups: Sequence[GroupLink], tags: Sequence[MccTagInstance], scenario_datum: int, globals_datum: int,
                      unk_14: int, pad_after_instances: Optional[bytes] = None) -> Tuple[bytes, int]:
    """'tags' header (0x20) + group links + instance table, zero padded to 4 KiB (= header ``tag_data_offset``)."""
    n_groups = len(groups)
    inst_off = 0x20 + 12 * n_groups
    body = bytearray(struct.pack("<iiiIIIi", 0x20, n_groups, inst_off, scenario_datum, globals_datum, unk_14, len(tags)))
    body += TAGS_SIGNATURE
    for g in groups:
        body += g.pack()
    for t in tags:
        body += t.pack()
    data_offset = align_up(len(body), META_ALIGN)
    pad = data_offset - len(body)
    if pad_after_instances is not None:
        if len(pad_after_instances) != pad:
            raise MccWriterError("pad_after_instances is %d bytes, layout needs %d" % (len(pad_after_instances), pad))
        body += pad_after_instances
    else:
        body += b"\0" * pad
    return bytes(body), data_offset


def tag_region_data_offset(n_groups: int, tag_count: int) -> int:
    return align_up(0x20 + 12 * n_groups + 16 * tag_count, META_ALIGN)


def make_datum(index: int, salt: Optional[int] = None) -> int:
    """datum = (salt << 16) | index; any salt with the top bit set works as long as every reference uses the same datum.  The
    default reproduces the H2EK convention (tag 0 = 0xE1750000, salt +1 per tag)."""
    if salt is None:
        salt = 0x8000 | ((0x6175 + index) & 0x7FFF)
    return ((salt & 0xFFFF) << 16) | (index & 0xFFFF)


@dataclass
class NewTag:
    """A tag to place.  ``data`` holds the bytes with every internal pointer (tag_block / data_reference address) written as an
    offset relative to the start of THIS tag's data; ``relocs`` lists the byte offsets of those u32 fields."""
    group: str
    name: Optional[str]
    data: bytes
    relocs: Sequence[int] = ()
    index: Optional[int] = None
    datum: Optional[int] = None


def assign_tag_addresses(tags: Sequence[NewTag], mask: int, data_offset: int) -> List[Tuple[MccTagInstance, bytes]]:
    """Place the tags back to back (4-byte aligned, in list order = ascending index) from region offset ``data_offset`` (virtual
    address ``mask + data_offset``), apply the relocations, return [(instance, final bytes)]."""
    out, cursor = [], data_offset
    for nt in tags:
        addr = mask + cursor
        data = bytearray(nt.data)
        if len(data) % 4:
            data += b"\0" * (4 - len(data) % 4)
        for r in nt.relocs:
            v = struct.unpack_from("<I", data, r)[0]
            if v:
                struct.pack_into("<I", data, r, (v + addr) & 0xFFFFFFFF)
        idx = nt.index if nt.index is not None else -1
        datum = nt.datum if nt.datum is not None else (make_datum(idx) if idx >= 0 else NONE32)
        out.append((MccTagInstance(idx, nt.group, datum, addr, len(data), nt.name), bytes(data)))
        cursor += len(data)
    return out


def place_tags(tags: Sequence[NewTag], tag_count: Optional[int] = None, mask: int = 0, n_groups: int = 120
               ) -> Tuple[List[MccTagInstance], bytes, int]:
    """Place a set of tags in the tag region: tags with an explicit ``index`` keep it, the others take the next free index;
    unused slots below ``tag_count`` (default: highest index + 1) become null slots.  Tag data is packed in ascending index
    order.  -> (full instance table, concatenated tag data (WITHOUT the 4 KiB tail pad), data_offset)."""
    used: Dict[int, NewTag] = {}
    nxt = 0
    for nt in tags:
        if nt.index is None:
            while nxt in used:
                nxt += 1
            idx = nxt
        else:
            idx = nt.index
        if idx in used:
            raise MccWriterError("two tags want index %d" % idx)
        used[idx] = nt
    n = max(tag_count if tag_count is not None else 0, (max(used) + 1) if used else 0)
    data_offset = tag_region_data_offset(n_groups, n)
    ordered = []
    for i in sorted(used):
        nt = used[i]
        ordered.append(NewTag(nt.group, nt.name, nt.data, nt.relocs, i, nt.datum))
    placed = {inst.index: (inst, blob) for inst, blob in assign_tag_addresses(ordered, mask, data_offset)}
    instances, chunks = [], []
    for i in range(n):
        if i in placed:
            instances.append(placed[i][0])
            chunks.append(placed[i][1])
        else:
            instances.append(MccTagInstance.null(i))
    return instances, b"".join(chunks), data_offset


def tag_checksum_crc(tag_blobs: Iterable[bytes]) -> int:
    """Stand-in for the meta header word at +0x14.  The tool computes it as a running CRC (init -1, no final xor) over the
    4-byte checksum of every loaded tag *source file* (sub_63E3F0) - not reproducible from a cache and never read by the
    engine.  Here: running CRC over crc32 of every tag's data, in index order (deterministic)."""
    state = 0xFFFFFFFF
    for b in tag_blobs:
        state = crc_update(state, struct.pack("<I", zlib.crc32(b) & 0xFFFFFFFF))
    return state


# --------------------------------------------------------------------------------------------------
# image ------------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
@dataclass
class MccImage:
    """Complete content of a v13 cache file as separable regions.  ``header`` carries the non-derived fields; offsets, sizes,
    ``checksum`` and (unless given) ``crc`` are recomputed by ``serialize_image``."""

    header: MccHeader
    sounds: Region = None                 # in-map sound chunks (H2EK style) at 0x380, or None
    geometry: Region = None               # raw table
    strings: List[str] = field(default_factory=list)
    string_raw: Optional[Tuple[bytes, bytes]] = None     # (index bytes, text bytes) overriding ``strings`` (non-canonical tables)
    tag_names: List[Optional[str]] = field(default_factory=list)
    tag_name_order: Optional[List[int]] = None
    locale: Region = None                 # per-language blobs
    locale_globals: Optional[bytes] = None    # 252-byte block or None
    bitmaps: Region = None                # in-map bitmap blobs (H2EK style)
    shared_graph: Optional[bytes] = None
    groups: List[GroupLink] = field(default_factory=list)
    tags: List[MccTagInstance] = field(default_factory=list)
    tag_data: Region = b""                # bytes from region offset data_offset on (incl. the tail pad)
    scenario_datum: int = NONE32
    globals_datum: int = NONE32
    tags_unk14: int = 0
    pad_after_instances: Optional[bytes] = None
    tag_mask: int = 0
    crc: Optional[int] = None             # None -> compute from the payload boundaries, else written as is

    # ---- construction from a parsed file ------------------------------------------------------------------
    @classmethod
    def from_cache(cls, c, reconstruct_items: bool = True) -> "MccImage":
        """Split a parsed (uncompressed) file into regions.  The big regions are zero-copy memoryviews of the mapped file, so
        keep ``c`` open while using the image.  With ``reconstruct_items`` the sound / geometry / locale / bitmap regions carry
        the write boundaries of the original tool run, so ``serialize_image(img, crc_mode='compute')`` reproduces header 0x2FC."""
        h = MccHeader(c.header["raw"])
        mv = memoryview(c.mm)
        mm = c.mm
        u32 = lambda o: struct.unpack_from("<I", mm, o)[0]
        img = cls(header=h)
        raw_off, raw_size = h.raw_offset, h.raw_size
        if raw_off < HEADER_SIZE or (raw_off % PAGE):
            raise MccWriterError("raw table offset %#x" % raw_off)
        # sounds
        img.sounds = mv[HEADER_SIZE:raw_off] if raw_off > HEADER_SIZE else None
        img.geometry = mv[raw_off:raw_off + raw_size]
        # strings
        n = h.string_count
        sidx = bytes(mm[h.string_index_offset:h.string_index_offset + 4 * n])
        stext = bytes(mm[h.string_offset:h.string_offset + h.string_size])
        offs = struct.unpack("<%dI" % n, sidx)
        strings, canonical, cur = [], True, 0
        for o in offs:
            e = stext.find(b"\0", o)
            strings.append(stext[o:e].decode("latin-1"))
            if o != cur:
                canonical = False
            cur = e + 1
        if cur != len(stext):
            canonical = False
        img.strings = strings
        if not canonical:
            img.string_raw = (sidx, stext)
        # tag names
        nt = h.file_table_count
        nidx = struct.unpack_from("<%dI" % nt, mm, h.file_index_offset)
        nblob = bytes(mm[h.file_table_offset:h.file_table_offset + h.file_table_size])
        names: List[Optional[str]] = []
        for o in nidx:
            if o == NONE32:
                names.append(None)
            else:
                names.append(nblob[o:nblob.find(b"\0", o)].decode("latin-1"))
        img.tag_names = names
        img.tag_name_order = [i for i, _o in sorted(((i, o) for i, o in enumerate(nidx) if o != NONE32), key=lambda x: x[1])]
        # locale area, bitmaps, shared graph
        nameidx_end = align_up(h.file_index_offset + 4 * nt)
        meta = h.meta_offset
        graph_start = h.shared_graph_offset if h.shared_graph_offset != NONE32 else None
        after = graph_start if graph_start is not None else meta
        if h.locale_offset != NONE32 and h.locale_size:
            loc_end = h.locale_offset
            img.locale = mv[nameidx_end:loc_end] if loc_end > nameidx_end else None
            img.locale_globals = bytes(mm[h.locale_offset:h.locale_offset + h.locale_size])
            bm_start = align_up(h.locale_offset + h.locale_size)
        else:
            loc_end = nameidx_end
            for e in c.matg_languages():
                if e["string_count"]:
                    loc_end = max(loc_end, align_up((e["index_table_offset"] & 0x7FFFFFFF) + 8 * e["string_count"]),
                                  align_up((e["data_offset"] & 0x7FFFFFFF) + e["table_size"]))
            img.locale = mv[nameidx_end:loc_end] if loc_end > nameidx_end else None
            bm_start = loc_end
        img.bitmaps = mv[bm_start:after] if after > bm_start else None
        if graph_start is not None:
            img.shared_graph = bytes(mm[graph_start:graph_start + h.shared_graph_size])
        # tag region
        gto, ng = c.meta_header["tag_group_table_offset"], c.meta_header["number_of_tag_groups"]
        img.groups = [GroupLink(*struct.unpack_from("<III", mm, meta + gto + 12 * i)) for i in range(ng)]
        img.tags = [MccTagInstance(t.index, t.group, t.datum, t.addr, t.size, t.name) for t in c.tags]
        inst_end = c.meta_header["tag_table_offset"] + 16 * len(c.tags)
        img.pad_after_instances = bytes(mm[meta + inst_end:meta + h.tag_data_offset])
        img.tag_data = mv[meta + h.tag_data_offset:meta + h.meta_size]
        img.scenario_datum, img.globals_datum = c.meta_header["scenario_datum"], c.meta_header["globals_datum"]
        img.tags_unk14 = c.meta_header["unk_14"]
        img.tag_mask = h.mask
        img.crc = h.crc
        if reconstruct_items:
            items = reconstruct_write_items(c)
            if img.sounds is not None:
                img.sounds = RegionBuilder.from_view(img.sounds, HEADER_SIZE, items["sounds"])
            img.geometry = RegionBuilder.from_view(img.geometry, raw_off, items["geometry"])
            if img.locale is not None:
                img.locale = RegionBuilder.from_view(img.locale, nameidx_end, items["locale"])
            if img.bitmaps is not None:
                img.bitmaps = RegionBuilder.from_view(img.bitmaps, bm_start, items["bitmaps"])
            img.crc = None
        return img


def reconstruct_write_items(c) -> Dict[str, List[Tuple[int, int]]]:
    """Recover the tool's write boundaries (absolute offset, payload size) of the variable regions of an uncompressed file:
    sounds (chunk records of every local ugh!), geometry ('hklb' ... 'fklb' blocks), locale (index table + data per language,
    from the header block or the matg entries), bitmaps (blob offset / size of every local bitm element).  Identical payloads
    were de-duplicated by the tool, so offsets are unique.  (The CRC was proven the same way.)"""
    h, mm = c.header, c.mm
    u32 = lambda o: struct.unpack_from("<I", mm, o)[0]
    i32 = lambda o: struct.unpack_from("<i", mm, o)[0]
    out: Dict[str, List[Tuple[int, int]]] = {"sounds": [], "geometry": [], "locale": [], "bitmaps": []}
    raw_off, raw_size = h["raw_table_offset"], h["raw_table_size"]
    # geometry
    pos, end = raw_off, raw_off + raw_size
    while True:
        p = mm.find(b"hklb", pos, end)
        if p < 0:
            break
        q = mm.find(b"fklb", p + 8, end)
        if q < 0:
            raise MccWriterError("geometry block at %#x has no footer" % p)
        out["geometry"].append((p, q + 4 - p))
        pos = p + align_up(q + 4 - p)
    # sounds: every chunk record of every local ugh!
    store_mask = h["unk_001E"]
    snd: Dict[int, int] = {}
    if raw_off > HEADER_SIZE and not (store_mask & ((1 << STORE_SOUNDS_NEUTRAL) | (1 << STORE_SOUNDS_LANG) | (1 << STORE_SOUNDS_REMASTERED))):
        for t in c.tags:
            if t.group != "ugh!" or not t.has_data:
                continue
            base = t.file_offset
            n9 = i32(base + 0x60)
            p9 = u32(base + 0x64)
            if n9 <= 0 or not p9:
                continue
            lo = c.addr_to_offset(p9)
            for L in range(n9):
                n = i32(lo + 8 * L)
                ptr = u32(lo + 8 * L + 4)
                if n > 0 and ptr:
                    ro = c.addr_to_offset(ptr)
                    for q in range(n):
                        fo, sf = struct.unpack_from("<II", mm, ro + 16 * q)
                        if HEADER_SIZE <= fo < raw_off:
                            snd[fo] = max(snd.get(fo, 0), sf & 0x0FFFFFFF)
        out["sounds"] = sorted(snd.items())
    # locale
    if h["locale_globals_offset"] != NONE32 and h["locale_globals_size"]:
        ents = c.locale_globals()
    else:
        ents = c.matg_languages()
    for e in ents:
        if e["string_count"]:
            out["locale"].append((e["index_table_offset"] & 0x7FFFFFFF, e["string_count"] * 8))
            out["locale"].append((e["data_offset"] & 0x7FFFFFFF, e["table_size"]))
    out["locale"].sort()
    # bitmaps (in-map blobs only)
    bm: Dict[int, int] = {}
    meta = h["meta_offset"]
    bm_lo = align_up(h["file_index_table_offset"] + 4 * h["file_table_count"])
    for t in c.tags:
        if t.group != "bitm" or not t.has_data or (store_mask & (1 << STORE_TEXTURES)):
            continue
        base = t.file_offset
        n, ptr = i32(base + 0x44), u32(base + 0x48)
        if n <= 0 or not ptr:
            continue
        bo = c.addr_to_offset(ptr)
        for j in range(n):
            lod_off, = struct.unpack_from("<I", mm, bo + 0xA8 * j + 0x38)
            lod_size, = struct.unpack_from("<I", mm, bo + 0xA8 * j + 0x50)
            if lod_size and bm_lo <= lod_off < meta:
                bm[lod_off] = max(bm.get(lod_off, 0), lod_size)
    out["bitmaps"] = sorted(bm.items())
    return out


# --------------------------------------------------------------------------------------------------
# layout + serialisation --------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
@dataclass
class MccLayout:
    sounds: int = HEADER_SIZE
    geometry: int = 0
    string_idx: int = 0
    string_data: int = 0
    tag_names: int = 0
    tag_name_idx: int = 0
    locale: int = 0
    locale_globals: int = 0
    bitmaps: int = 0
    shared_graph: int = 0
    meta: int = 0
    file_size: int = 0


def plan_layout(sounds_size: int, geometry_size: int, string_count: int, string_data_size: int, names_size: int,
                tag_count: int, locale_size: int, locale_globals_size: int, bitmap_size: int, shared_graph_size: int,
                meta_size: int) -> MccLayout:
    """Offsets for the given payload sizes (sounds / geometry / locale / bitmap sizes must already be 0x80 multiples)."""
    L = MccLayout()
    cur = HEADER_SIZE
    L.sounds = cur; cur += align_up(sounds_size)
    L.geometry = cur; cur += align_up(geometry_size)
    L.string_idx = cur; cur += align_up(4 * string_count)
    L.string_data = cur; cur += align_up(string_data_size)
    L.tag_names = cur; cur += align_up(names_size)
    L.tag_name_idx = cur; cur += align_up(4 * tag_count)
    L.locale = cur; cur += align_up(locale_size)
    L.locale_globals = cur; cur += align_up(locale_globals_size)
    L.bitmaps = cur; cur += align_up(bitmap_size)
    L.shared_graph = cur; cur += align_up(shared_graph_size)
    L.meta = cur
    L.file_size = cur + meta_size
    return L


def serialize_image(img: MccImage, crc_mode: str = "auto"):
    """-> (final MccHeader, list of body pieces (bytes / memoryview) that follow the header, in file order).
    ``crc_mode``: 'auto' (img.crc if set else compute), 'compute', 'keep' (header value untouched)."""
    h = img.header.copy()
    for nm, r in (("sounds", img.sounds), ("geometry", img.geometry), ("locale", img.locale), ("bitmaps", img.bitmaps)):
        if _region_len(r) % PAGE:
            raise MccWriterError("%s size %#x is not a multiple of 0x80" % (nm, _region_len(r)))
    if len(img.tag_names) != len(img.tags):
        raise MccWriterError("%d tag names for %d tag slots (header 0x20 is both counts)" % (len(img.tag_names), len(img.tags)))
    if img.scenario_datum == NONE32 or img.globals_datum == NONE32:
        raise MccWriterError("scenario / globals datum missing (the engine locates both through the meta header)")
    nstr = len(img.strings)
    idx_b, tbl_b = img.string_raw if img.string_raw is not None else pack_string_table(img.strings)
    if img.string_raw is not None:
        nstr = len(idx_b) // 4
    names_b, nidx_b = pack_tag_names(img.tag_names, img.tag_name_order)
    th, data_offset = build_meta_header(img.groups, img.tags, img.scenario_datum, img.globals_datum, img.tags_unk14,
                                        img.pad_after_instances)
    tag_data = img.tag_data
    td_len = len(tag_data)
    td_pad = (-td_len) % META_ALIGN
    data_size = td_len + td_pad
    meta_size = data_offset + data_size

    pieces: List[object] = []
    payloads: List[object] = []
    xs = Xor32()

    def emit(blob, pay=None):
        """Append ``blob`` + zero pad to 0x80 (one write); ``pay`` = list of payload items for the CRC (default: blob itself)."""
        n = len(blob)
        pieces.append(blob)
        xs.feed(blob)
        pad = (-n) % PAGE
        if pad:
            z = b"\0" * pad
            pieces.append(z)
            xs.feed(z)
        payloads.extend([blob] if pay is None else pay)
        return n + pad

    def emit_region(r: Region):
        if r is None or not len(r):
            return 0
        n = 0
        for p in _region_pieces(r):
            pieces.append(p)
            xs.feed(p)
            n += len(p)
        payloads.extend(_region_payloads(r))
        return n

    cur = HEADER_SIZE
    cur += emit_region(img.sounds)
    h.raw_offset = cur
    g = emit_region(img.geometry)
    h.raw_size = g
    cur += g
    h.string_index_offset, h.string_count = cur, nstr
    cur += emit(idx_b)
    h.string_offset, h.string_size = cur, len(tbl_b)
    cur += emit(tbl_b)
    h.file_table_offset, h.file_table_size, h.file_table_count = cur, len(names_b), len(img.tag_names)
    cur += emit(names_b)
    h.file_index_offset = cur
    cur += emit(nidx_b)
    cur += emit_region(img.locale)
    if img.locale_globals is None:
        h.locale_offset, h.locale_size = NONE32, 0
    else:
        h.locale_offset, h.locale_size = cur, len(img.locale_globals)
        cur += emit(bytes(img.locale_globals))
    cur += emit_region(img.bitmaps)
    if img.shared_graph is None:
        h.shared_graph_offset, h.shared_graph_size = NONE32, 0
    else:
        h.shared_graph_offset, h.shared_graph_size = cur, len(img.shared_graph)
        cur += emit(bytes(img.shared_graph))
    # meta: one write (header part + data part, 4 KiB padded)
    h.meta_offset, h.meta_size = cur, meta_size
    h.mask, h.tag_data_offset, h.tag_data_size = img.tag_mask, data_offset, data_size
    meta_pieces = [th, tag_data] + ([b"\0" * td_pad] if td_pad else [])
    for p in meta_pieces:
        pieces.append(p)
        xs.feed(p)
    payloads.append(("meta", meta_pieces))
    cur += meta_size
    h.file_size = cur
    h.checksum = xs.value()
    h.flags = h.flags & ~FLAG_COMPRESSED
    if (crc_mode == "keep") or (crc_mode == "auto" and img.crc is not None):
        h.crc = img.crc if img.crc is not None else h.crc
    else:
        st = 0xFFFFFFFF
        for p in payloads:
            if isinstance(p, tuple) and p[0] == "meta":
                for q in p[1]:
                    st = crc_update(st, q)
            else:
                st = crc_update(st, p)
        h.crc = st
    return h, pieces


def write_image(img: MccImage, path: Optional[str] = None, crc_mode: str = "auto", compress: bool = False,
                workers: int = 1) -> Optional[bytes]:
    """Serialise ``img``.  With ``path`` the file is streamed to ``path + '.part'`` and renamed (atomic); returns None.
    Without a path the whole uncompressed file is returned as bytes (small maps only).  ``compress=True`` writes the h2mcc
    compressed form (``workers`` <= 4 deflate threads)."""
    h, pieces = serialize_image(img, crc_mode)
    if path is None:
        if compress:
            raise MccWriterError("compress needs a path")
        return h.pack() + b"".join(bytes(p) for p in pieces)
    tmp = path + ".part"
    try:
        if compress:
            _write_compressed(tmp, h, pieces, workers)
        else:
            with open(tmp, "wb") as f:
                f.write(h.pack())
                for p in pieces:
                    f.write(p)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return None


# --------------------------------------------------------------------------------------------------
# compression --------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
def deflate_chunk(raw) -> Tuple[bytes, bool]:
    """-> (blob, stored_raw).  The stock recipe: zlib level 9, 1 KB window (header 28 15), memLevel 8, Z_FIXED; a chunk that
    does not shrink is stored raw (chunk table size < 0)."""
    c = zlib.compressobj(9, zlib.DEFLATED, 10, 8, zlib.Z_FIXED)
    z = c.compress(raw) + c.flush()
    if len(z) >= len(raw):
        return bytes(raw), True
    return z, False


def _iter_chunks(pieces: Iterable[object], chunk: int = CHUNK_SIZE) -> Iterator[object]:
    buf = bytearray()
    for p in pieces:
        mv = memoryview(p).cast("B")
        pos = 0
        if buf:
            take = min(chunk - len(buf), len(mv))
            buf += mv[:take]
            pos = take
            if len(buf) == chunk:
                yield bytes(buf)
                buf = bytearray()
        while len(mv) - pos >= chunk:
            yield mv[pos:pos + chunk]
            pos += chunk
        if pos < len(mv):
            buf += mv[pos:]
    if buf:
        yield bytes(buf)


def _write_compressed(path: str, h: MccHeader, pieces: Iterable[object], workers: int = 1) -> None:
    """Stream the body pieces through the chunk compressor into ``path`` (header written last)."""
    workers = max(1, min(4, int(workers)))
    h = h.copy()
    table: List[Tuple[int, int]] = []
    with open(path, "wb") as f:
        f.write(b"\0" * HEADER_SIZE)
        pos = HEADER_SIZE
        chunks = _iter_chunks(pieces)
        if workers == 1:
            results = (deflate_chunk(c) for c in chunks)
            for blob, raw in results:
                pos = _emit_chunk(f, pos, blob, raw, table)
        else:
            from concurrent.futures import ThreadPoolExecutor
            import collections
            with ThreadPoolExecutor(max_workers=workers) as ex:
                window = collections.deque()
                for c in chunks:
                    window.append(ex.submit(deflate_chunk, bytes(c) if isinstance(c, memoryview) else c))
                    if len(window) >= workers * 3:
                        blob, raw = window.popleft().result()
                        pos = _emit_chunk(f, pos, blob, raw, table)
                while window:
                    blob, raw = window.popleft().result()
                    pos = _emit_chunk(f, pos, blob, raw, table)
        tbl_off = pos
        f.write(b"".join(struct.pack("<iI", s, o) for s, o in table))
        end = tbl_off + 8 * len(table)
        pad = (-end) % PAGE
        if pad:
            f.write(b"\0" * pad)
        h.flags = h.flags | FLAG_COMPRESSED
        h.compression_chunk_size = CHUNK_SIZE
        h.compression_data_offset = HEADER_SIZE
        h.compression_table_offset = tbl_off
        h.compression_chunk_count = len(table)
        f.seek(0)
        f.write(h.pack())


def _emit_chunk(f, pos: int, blob, raw: bool, table: List[Tuple[int, int]]) -> int:
    size = -len(blob) if raw else len(blob)
    table.append((size, pos))
    f.write(blob)
    pos += len(blob)
    pad = (-pos) % PAGE
    if pad:
        f.write(b"\0" * pad)
        pos += pad
    return pos


def compress_file(src: str, dst: str, workers: int = 1) -> Dict[str, int]:
    """h2mcc-compress an UNcompressed v13 file (any origin): header with flag bit0 set + chunks + chunk table.  Header
    0x2F8 / 0x2FC / everything else is kept; 0x08 stays the uncompressed size."""
    import mmap
    with open(src, "rb") as fh:
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            h = MccHeader(mm[:HEADER_SIZE])
            if h.compressed:
                raise MccWriterError("%s is already compressed" % src)
            if h.file_size != len(mm):
                raise MccWriterError("header file size %d != real size %d" % (h.file_size, len(mm)))
            tmp = dst + ".part"
            mv = memoryview(mm)
            body = mv[HEADER_SIZE:]
            try:
                _write_compressed(tmp, h, [body], workers)
                os.replace(tmp, dst)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
                body.release()
                mv.release()
        finally:
            try:
                mm.close()
            except BufferError:
                import gc
                gc.collect()
                mm.close()
    return {"src_size": os.path.getsize(src), "dst_size": os.path.getsize(dst)}


# --------------------------------------------------------------------------------------------------
# staged builder (mirror of VistaBuilder) -------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
class MccBuilder:
    """Staged builder for a NEW self-contained map.  Each stage's absolute file offsets are only known once all earlier regions
    have their final size, so the stages must be driven in this order (a later stage seals the earlier ones):

        1. ``add_sound(blob)``                   -> absolute file offset of the chunk (ugh! chunk record file_offset)
        2. ``add_geometry(blob)``                -> absolute file offset of the 'blkh' block (geometry_block_info.block_offset)
        3. ``set_strings`` / ``set_tag_names``   (sizes only), then ``locale_base`` is known
           ``set_locale(blob, locale_globals)``  (blob = concatenated language blobs, 0x80 padded)
        4. ``add_bitmap(blob)``                  -> absolute file offset of the bitmap blob (bitm element lod offset)
        5. ``set_tags(...)``; ``build_image()`` / ``write(path)``

    Offsets returned by the ``add_*`` methods are plain file offsets (MCC has no location bits for in-map resources: header
    store mask 0).  Blobs: sound chunk = codec payload as in the source ugh! chunk list; bitmap blob = ``u32 n | i32 size[n] |
    chunks`` (negative size = stored raw); geometry = a complete 'blkh' ... 'fklb' block."""

    def __init__(self, header: MccHeader, groups: Sequence[GroupLink], tag_mask: int = 0):
        self.header = header
        self.groups = list(groups)
        self.mask = tag_mask
        self._sounds = RegionBuilder()
        self._geometry: Optional[RegionBuilder] = None
        self._bitmaps: Optional[RegionBuilder] = None
        self._stage = 1
        self.strings: List[str] = []
        self.tag_names: List[Optional[str]] = []
        self.tag_name_order: Optional[List[int]] = None
        self._locale: Union[bytes, RegionBuilder] = b""
        self._locale_globals: Optional[bytes] = None
        self._geom_base = self._bitmap_base = None
        self._tables_set = [False, False]
        self.tags: List[MccTagInstance] = []
        self.tag_data: Region = b""
        self.scenario_datum = self.globals_datum = NONE32
        self.tags_unk14 = 0
        self.shared_graph: Optional[bytes] = None
        self.pad_after_instances: Optional[bytes] = None

    # stage 1 ---------------------------------------------------------------------------------------
    def add_sound(self, data, dedupe: bool = True) -> int:
        if self._stage > 1:
            raise RuntimeError("sound region already sealed")
        return HEADER_SIZE + self._sounds.add(data, dedupe)

    # stage 2 ---------------------------------------------------------------------------------------
    def _seal_sounds(self):
        if self._stage < 2:
            self._stage = 2
            self._geom_base = HEADER_SIZE + len(self._sounds)
            self._geometry = RegionBuilder()

    def add_geometry(self, data, dedupe: bool = True) -> int:
        self._seal_sounds()
        if self._stage > 2:
            raise RuntimeError("geometry region already sealed")
        return self._geom_base + self._geometry.add(data, dedupe)

    # stage 3 ---------------------------------------------------------------------------------------
    def _seal_geometry(self):
        self._seal_sounds()
        if self._stage < 3:
            self._stage = 3

    def set_strings(self, strings: Sequence[str]):
        self._seal_geometry()
        self.strings = list(strings)
        self._tables_set[0] = True

    def set_tag_names(self, names: Sequence[Optional[str]], order: Optional[Sequence[int]] = None):
        self._seal_geometry()
        self.tag_names = list(names)
        self.tag_name_order = list(order) if order is not None else None
        self._tables_set[1] = True

    @property
    def locale_base(self) -> int:
        """Absolute file offset where the locale blobs start (valid after set_strings + set_tag_names)."""
        if not all(self._tables_set):
            raise RuntimeError("call set_strings() and set_tag_names() first")
        geom = len(self._geometry) if self._geometry is not None else 0
        names_b, _ = pack_tag_names(self.tag_names, self.tag_name_order)
        idx_b, tbl_b = pack_string_table(self.strings)
        return (HEADER_SIZE + len(self._sounds) + geom + align_up(len(idx_b)) + align_up(len(tbl_b)) +
                align_up(len(names_b)) + align_up(4 * len(self.tag_names)))

    @property
    def locale_end(self) -> int:
        return self.locale_base + len(self._locale)

    def set_locale(self, locale_data, locale_globals: Optional[bytes] = None):
        """``locale_data``: concatenated language blobs (each ``pack_language`` blob, 0x80 padded; bytes or RegionBuilder);
        ``locale_globals``: 252-byte header block (stock MP style; its offsets are absolute file offsets starting at
        ``locale_base``) or None (self-contained maps: the matg tag carries the entries)."""
        if not all(self._tables_set):
            raise RuntimeError("call set_strings() and set_tag_names() first")
        if len(locale_data) % PAGE:
            raise ValueError("locale_data must be a multiple of 0x80")
        if locale_globals is not None and len(locale_globals) != 252:
            raise ValueError("locale globals must be 252 bytes")
        self._locale = locale_data
        self._locale_globals = None if locale_globals is None else bytes(locale_globals)

    # stage 4 ---------------------------------------------------------------------------------------
    def _seal_locale(self):
        if not all(self._tables_set):
            raise RuntimeError("call set_strings() and set_tag_names() before adding bitmaps")
        if self._stage < 4:
            self._stage = 4
            self._bitmap_base = self.locale_end + (align_up(len(self._locale_globals)) if self._locale_globals else 0)
            self._bitmaps = RegionBuilder()

    @property
    def bitmap_base(self) -> int:
        self._seal_locale()
        return self._bitmap_base

    def add_bitmap(self, data, dedupe: bool = True) -> int:
        self._seal_locale()
        if self._stage > 4:
            raise RuntimeError("bitmap region already sealed")
        return self._bitmap_base + self._bitmaps.add(data, dedupe)

    # stage 5 ---------------------------------------------------------------------------------------
    def set_shared_graph(self, data: Optional[bytes]) -> None:
        """Shared tag dependency graph (header 0x2DC / 0x2E0): only the stock shared.map has one; call before ``set_tags``."""
        self.shared_graph = None if data is None else bytes(data)

    def set_tags(self, tags: Sequence[MccTagInstance], tag_data, scenario_datum: int, globals_datum: int, unk14: Optional[int] = None,
                 pad_after_instances: Optional[bytes] = None):
        """``tags``: the complete instance table (null slots = MccTagInstance.null(i)); ``tag_data``: the bytes from region offset
        ``tag_region_data_offset(len(tags))`` on (concatenate the ``assign_tag_addresses`` results).  ``unk14`` None -> derived."""
        self._seal_locale()
        if len(tags) >= 17000 and self.mask == 0 and self.header.type != 3:
            raise MccWriterError("%d tag slots: halo2.dll treats tag tables of 17000+ entries as linked to shared.map" % len(tags))
        self._stage = 5
        self.tags = list(tags)
        self.tag_data = tag_data
        self.scenario_datum, self.globals_datum = scenario_datum, globals_datum
        self.pad_after_instances = pad_after_instances
        if unk14 is None:
            base = self.mask + tag_region_data_offset(len(self.groups), len(self.tags))
            mvd = memoryview(tag_data)
            blobs = []
            for t in self.tags:
                if t.size and t.addr >= base:
                    blobs.append(mvd[t.addr - base:t.addr - base + t.size])
            unk14 = tag_checksum_crc(blobs)
        self.tags_unk14 = unk14

    def tag_region_data_offset(self, tag_count: int) -> int:
        return tag_region_data_offset(len(self.groups), tag_count)

    def build_image(self) -> MccImage:
        if self._stage < 5:
            raise RuntimeError("set_tags() not called")
        return MccImage(
            header=self.header, sounds=self._sounds if len(self._sounds) else None,
            geometry=self._geometry if (self._geometry is not None and len(self._geometry)) else None,
            strings=self.strings, tag_names=self.tag_names, tag_name_order=self.tag_name_order,
            locale=self._locale if len(self._locale) else None, locale_globals=self._locale_globals,
            bitmaps=self._bitmaps if (self._bitmaps is not None and len(self._bitmaps)) else None,
            groups=self.groups, tags=self.tags, tag_data=self.tag_data, scenario_datum=self.scenario_datum,
            shared_graph=self.shared_graph, pad_after_instances=self.pad_after_instances,
            globals_datum=self.globals_datum, tags_unk14=self.tags_unk14, tag_mask=self.mask, crc=None)

    def write(self, path: str, compress: bool = False, workers: int = 1) -> None:
        write_image(self.build_image(), path, compress=compress, workers=workers)


# --------------------------------------------------------------------------------------------------
# verification of a finished file ------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
def validate_file(path: str, deep: bool = False, check_prefix: bool = True) -> List[str]:
    """Everything halo2.dll tests at load (``MccHeader.engine_problems`` + meta header) plus the invariants the tool guarantees
    (region order / alignment, tag table inside the tag data, XOR checksum, engine string prefix; with ``deep`` also the CRC
    recomputed from the write boundaries recovered out of the file).  Works on uncompressed files; for a compressed file only the
    chunk table structure is checked (decompress it for the full check).  Never raises: a file the checks cannot even open
    yields a problem string.  Returns [] for a clean file."""
    try:
        return _validate_file(path, deep, check_prefix)
    except Exception as e:  # noqa - a corrupt file must produce a report, not a traceback
        return ["validation aborted: %s: %s" % (type(e).__name__, e)]


def _validate_file(path: str, deep: bool, check_prefix: bool) -> List[str]:
    problems: List[str] = []
    with open(path, "rb") as fh:
        raw_h = fh.read(HEADER_SIZE)
    if len(raw_h) < HEADER_SIZE:
        return ["file shorter than the 0x380 header"]
    h = MccHeader(raw_h)
    problems += h.engine_problems()
    if h.compressed:
        return problems + _validate_compressed(path, h)
    from . import mcc_cache
    try:
        c = mcc_cache.MccCache(path)
    except Exception as e:  # noqa
        return problems + ["unreadable: %s" % e]
    try:
        mm = c.mm
        size = len(mm)
        if h.file_size != size:
            problems.append("header file_size %#x != actual %#x" % (h.file_size, size))
        if size % PAGE:
            problems.append("file size not a multiple of 0x80")
        # engine meta checks
        mo = h.meta_offset
        tto = struct.unpack_from("<i", mm, mo + 8)[0]
        nt = struct.unpack_from("<i", mm, mo + 0x18)[0]
        if tto == 0 or nt <= 0 or struct.unpack_from("<I", mm, mo + 0x1C)[0] != 0x74616773:
            problems.append("meta header rejected by the engine (tag table offset / count / 'tags')")
        if h.meta_offset + h.meta_size != size:
            problems.append("meta does not end at EOF")
        if h.tag_data_offset + h.tag_data_size != h.meta_size:
            problems.append("tag_data_offset + tag_data_size != meta_size (engine places the tag data at the END of the meta)")
        if h.tag_data_offset % META_ALIGN or h.tag_data_size % META_ALIGN:
            problems.append("tag data parts not 4 KiB aligned")
        # region order
        order = [("raw", h.raw_offset, h.raw_size), ("string_idx", h.string_index_offset, 4 * h.string_count),
                 ("string_data", h.string_offset, h.string_size), ("tag_names", h.file_table_offset, h.file_table_size),
                 ("tag_name_idx", h.file_index_offset, 4 * h.file_table_count)]
        pos = h.raw_offset
        if h.raw_offset < HEADER_SIZE or h.raw_offset % PAGE:
            problems.append("raw table offset %#x" % h.raw_offset)
        for nm, off, sz in order:
            if off != pos:
                problems.append("%s at %#x, expected %#x" % (nm, off, pos))
            if off % PAGE:
                problems.append("%s not 0x80 aligned" % nm)
            pos = align_up(off + sz)
        if h.locale_offset != NONE32 and h.locale_offset < pos:
            problems.append("locale globals before the end of the name index")
        if h.mask != 0 and (h.mask % 0x10000):
            problems.append("odd tag mask %#x" % h.mask)
        if len(c.tags) >= 17000 and h.mask == 0 and h.type != 3:
            problems.append("%d tag slots with mask 0: the engine will try to link the table to shared.map" % len(c.tags))
        # tags
        lo, hi = h.mask + h.tag_data_offset, h.mask + h.meta_size
        for t in c.tags:
            if t.is_null:
                if t.datum != NONE32 or t.addr or t.size:
                    problems.append("null slot %d has data" % t.index)
                    break
                continue
            if t.size:
                if not (lo <= t.addr and t.addr + t.size <= hi):
                    problems.append("tag %d (%s) data outside the tag region" % (t.index, t.group))
                    break
                if t.addr % 4 or t.size % 4:
                    problems.append("tag %d not 4-byte aligned" % t.index)
                    break
            if (t.datum & 0xFFFF) != t.index:
                problems.append("tag %d datum %#x does not carry its index" % (t.index, t.datum))
                break
        sc = c.tag_by_datum(c.scenario_datum)
        if sc is None or sc.group != "scnr":
            problems.append("scenario datum does not reference an scnr")
        gl = c.tag_by_datum(c.globals_datum)
        if gl is None or gl.group != "matg":
            problems.append("globals datum does not reference a matg")
        # strings
        if check_prefix:
            try:
                pre = engine_string_prefix()
                if c.string_ids[:ENGINE_STRING_COUNT] != pre:
                    problems.append("string table does not start with the %d engine string ids" % ENGINE_STRING_COUNT)
            except Exception as e:  # noqa
                problems.append("cannot check the string prefix: %s" % e)
        if len(c.string_ids) > MAX_STRING_IDS:
            problems.append("more than %d string ids" % MAX_STRING_IDS)
        # checksum
        x = Xor32()
        mv = memoryview(mm)
        for s in range(HEADER_SIZE, size, 1 << 26):
            x.feed(mv[s:min(size, s + (1 << 26))])
        if x.value() != h.checksum:
            problems.append("checksum (0x2F8) %#x != XOR32 of [0x380, EOF) %#x" % (h.checksum, x.value()))
        if deep:
            try:
                items = reconstruct_write_items(c)
                allitems = list(items["sounds"]) + list(items["geometry"]) + list(items["locale"]) + list(items["bitmaps"])
                allitems += [(h.string_index_offset, 4 * h.string_count), (h.string_offset, h.string_size),
                             (h.file_table_offset, h.file_table_size), (h.file_index_offset, 4 * h.file_table_count)]
                if h.locale_offset != NONE32:
                    allitems.append((h.locale_offset, h.locale_size))
                if h.shared_graph_offset != NONE32:
                    allitems.append((h.shared_graph_offset, h.shared_graph_size))
                allitems.append((h.meta_offset, h.meta_size))
                allitems.sort()
                st = 0xFFFFFFFF
                for off, sz in allitems:
                    st = crc_update(st, mv[off:off + sz])
                if st != h.crc:
                    problems.append("crc (0x2FC) %#x != running CRC of the recovered write items %#x" % (h.crc, st))
            except Exception as e:  # noqa
                problems.append("deep crc check failed: %s" % e)
        mv.release()
    finally:
        close_cache(c)
    return problems


def _validate_compressed(path: str, h: MccHeader) -> List[str]:
    p: List[str] = []
    size = os.path.getsize(path)
    n, tbl = h.compression_chunk_count, h.compression_table_offset
    if h.compression_chunk_size != CHUNK_SIZE:
        p.append("compression chunk size %#x != 0x40000" % h.compression_chunk_size)
    if h.compression_data_offset != HEADER_SIZE:
        p.append("compression data offset != 0x380")
    if tbl < HEADER_SIZE or tbl + 8 * n > size:
        p.append("chunk table outside the file")
        return p
    if align_up(tbl + 8 * n) != size:
        p.append("chunk table does not end at EOF")
    with open(path, "rb") as f:
        f.seek(tbl)
        entries = list(struct.iter_unpack("<iI", f.read(8 * n)))
        expect = HEADER_SIZE
        total = 0
        for i, (s, o) in enumerate(entries):
            if o != expect:
                p.append("chunk %d at %#x, expected %#x" % (i, o, expect))
                break
            expect = align_up(o + abs(s))
            total += CHUNK_SIZE if i < n - 1 else 0
        if expect != tbl:
            p.append("chunk table not directly after the last chunk")
    return p


# --------------------------------------------------------------------------------------------------
# round-trip helper + CLI ----------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
def close_cache(c) -> None:
    """Close an MccCache whose mmap may still be referenced by memoryview slices (images built with ``from_cache``)."""
    try:
        c.close()
    except BufferError:
        import gc
        gc.collect()
        c.close()


def _same(a, b, block: int = 1 << 24) -> bool:
    """Fast equality of two byte buffers (memcmp on bytes blocks; memoryview == is element-wise slow)."""
    if len(a) != len(b):
        return False
    for i in range(0, len(a), block):
        if bytes(a[i:i + block]) != bytes(b[i:i + block]):
            return False
    return True


def _roundtrip_body(c, crc_mode: str) -> Dict[str, object]:
    img = MccImage.from_cache(c, reconstruct_items=True)
    h, pieces = serialize_image(img, crc_mode=crc_mode)
    mm = c.mm
    diffs: List[Tuple[int, object, object]] = []
    hdr = h.pack()
    for i in range(HEADER_SIZE):
        if hdr[i] != mm[i]:
            diffs.append((i, mm[i], hdr[i]))
    pos = HEADER_SIZE
    mv = memoryview(mm)
    for blob in pieces:
        n = len(blob)
        if pos + n > len(mm):
            diffs.append((pos, "file shorter", len(mm)))
            break
        if not _same(mv[pos:pos + n], memoryview(blob).cast("B")):
            a, b = bytes(mv[pos:pos + n]), bytes(blob)
            for i in range(n):
                if a[i] != b[i]:
                    diffs.append((pos + i, a[i], b[i]))
                    if len(diffs) >= 20:
                        break
        pos += n
        if len(diffs) >= 20:
            break
    if not diffs and pos != len(mm):
        diffs.append((pos, "length mismatch", len(mm)))
    return {"identical": not diffs, "diffs": diffs, "size": len(mm), "crc_header": c.header["unk_02FC"], "crc_computed": h.crc}


def roundtrip_compare(path: str, crc_mode: str = "compute") -> Dict[str, object]:
    """Parse ``path`` (uncompressed v13), re-serialise it in memory (nothing is written) and compare with the original.
    Returns {'identical': bool, 'diffs': [(offset, expected, got)], 'size': n, 'crc_header', 'crc_computed'}."""
    from . import mcc_cache
    c = mcc_cache.MccCache(path)
    try:
        return _roundtrip_body(c, crc_mode)
    finally:
        close_cache(c)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: py -3 -m h2conv.mcc_writer roundtrip <map> | compress <src> <dst> [--workers N] | validate <map> [--deep] | "
              "strings-prefix [--write]")
        return 2
    cmd = argv[0]
    if cmd == "roundtrip":
        r = roundtrip_compare(argv[1])
        print(r)
        return 0 if r["identical"] else 1
    if cmd == "compress":
        w = int(argv[argv.index("--workers") + 1]) if "--workers" in argv else 1
        print(compress_file(argv[1], argv[2], workers=w))
        return 0
    if cmd == "validate":
        probs = validate_file(argv[1], deep="--deep" in argv)
        print("\n".join(probs) if probs else "OK")
        return 1 if probs else 0
    if cmd == "strings-prefix":
        if "--write" in argv:
            print("wrote", write_engine_string_prefix(), "strings to", STRING_PREFIX_FILE)
        else:
            pre = engine_string_prefix()
            print(len(pre), pre[:5], "...", pre[-3:])
        return 0
    print("unknown command", cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main())
