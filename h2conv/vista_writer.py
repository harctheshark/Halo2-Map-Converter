# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Halo 2 Vista (cache v8) writer.

Lays out a complete, engine-loadable ``.map`` from an in-memory model (``VistaImage``).  The order, alignment and
header fields follow what H2Tool's ``build_cache_file`` does (decompiled from H2ToolSymbols.exe) and were verified
by re-serialising the stock Vista ``headlong/shared/single_player_shared/mainmenu`` maps byte-for-byte.

File order (every region starts on a 0x200 boundary and is zero-padded to 0x200 after its payload):

    0x000  header (0x800)                                        <- written LAST by H2Tool, here too
           raw_sounds       sound sample chunks                  (pointers = absolute file offsets)
           geometry         "fast geometry load" blocks          header 0x2E8/0x2EC (-1/0 when empty)
           string_block     string_table_count * 128-byte slots  header 0x16C/0x170 (debug names, unused at runtime)
           string_idx       uint32[string_table_count]           header 0x178
           string_table     NUL-terminated strings               header 0x17C, size 0x174
           tag_names        NUL-terminated tag names             header 0x2D0, size 0x2D4
           tag_name_idx     uint32[debug_tag_name_count]         header 0x2D8, count 0x2CC (-1 = unnamed)
           locale_data      per-language string-reference tables + utf-8 data (referenced by the language pack)
           language_pack    9 x c_language_pack (28 B) = 252 B   header 0x2DC/0x2E0 (-1/0 when absent)
           bitmap_pixels    bitmap pixel data (lod offsets = absolute file offsets)
           shared_graph     shared tag dependency graph          header 0x24/0x28 (-1/0 when absent)
           tag region       tags header + group links + instances (4 KiB aligned) + tag data (4 KiB aligned)
                            header 0x10 (offset) 0x14 (data_offset) 0x18 (data_size) 0x1C (tag_size) 0x20 (mask)

Header fields derived here: file_size, tag region fields, string/tag-name/locale/geometry/shared-graph offsets,
``checksum`` (XOR32 of everything after the header) and ``crc`` (running JAMCRC of the written payloads).
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

try:
    from .vista_cache import (CACHE_FILE_PAGE_SIZE, FIRST_SHARED_TAG_INSTANCE_INDEX, HEADER_SIZE, TAG_DATA_ALIGN,
                              TAG_REGION_ALIGN, TAGS_SIGNATURE, TagGroupLink, VistaCache, VistaHeader, VistaTag,
                              align_up, fourcc, jamcrc_update, mopp_checksum_of, reconstruct_write_items, xor32)
except ImportError:  # run as a plain script
    from vista_cache import (CACHE_FILE_PAGE_SIZE, FIRST_SHARED_TAG_INSTANCE_INDEX, HEADER_SIZE, TAG_DATA_ALIGN,  # type: ignore
                             TAG_REGION_ALIGN, TAGS_SIGNATURE, TagGroupLink, VistaCache, VistaHeader, VistaTag,
                             align_up, fourcc, jamcrc_update, mopp_checksum_of, reconstruct_write_items, xor32)

PAGE = CACHE_FILE_PAGE_SIZE
NONE32 = 0xFFFFFFFF
MAX_TAG_NAME_LEN = 255          # H2Tool copies min(len+1, 0x100) bytes per tag name
MAX_STRING_ID_STORAGE = 0x60000  # k_maximum_string_id_storage (engine global buffer)
MAX_STRING_IDS = 24576           # k_cache_file_max_string_ids (g_string_id_index_buffer[0x6000])
MAX_TAG_NAME_BUFFER = 5120000    # k_cache_file_debug_name_buffer_size
MAX_MP_FILE_SIZE = 0xFA00000     # custom-map list validation: 0xC00 <= file_size <= 0xFA00000 (halo2.exe 0x44F779)


# --------------------------------------------------------------------------------------------------
# region builder: sequential, 0x200-padded writes that remember their payload (for the CRC) -----------
# --------------------------------------------------------------------------------------------------
class RegionBuilder:
    """Collects blobs the way H2Tool's ``sub_9B27E0`` writes them: every blob is followed by zero padding up to the
    next 0x200 boundary.  ``add`` returns the offset *relative to the start of the region* (0x200-aligned), so with
    a known region base the absolute file offset is ``base + rel``."""

    def __init__(self):
        self._parts: List[bytes] = []
        self.payload_sizes: List[int] = []
        self.size = 0

    def add(self, data: Union[bytes, bytearray, memoryview]) -> int:
        rel = self.size
        data = bytes(data)
        pad = (-len(data)) % PAGE
        self._parts.append(data)
        if pad:
            self._parts.append(b"\0" * pad)
        self.payload_sizes.append(len(data))
        self.size += len(data) + pad
        return rel

    def data(self) -> bytes:
        return b"".join(self._parts)

    def __len__(self):
        return self.size

    @classmethod
    def from_blob(cls, blob: bytes, base: int, items: Sequence[Tuple[int, int]]) -> "RegionBuilder":
        """Wrap an existing 0x200-padded region that starts at file offset ``base``; ``items`` = (offset, payload size)
        of the writes it is made of.  They must tile the blob exactly (each starts where the previous padded one ended)."""
        rb = cls()
        pos = base
        for off, size in sorted(items):
            if off != pos:
                raise ValueError("items do not tile the region at %#x (next item at %#x)" % (pos, off))
            rb.payload_sizes.append(size)
            pos = align_up(off + size, PAGE)
        if pos - base != len(blob):
            raise ValueError("items cover %#x bytes but the region has %#x" % (pos - base, len(blob)))
        rb._parts = [bytes(blob)]
        rb.size = len(blob)
        return rb


# --------------------------------------------------------------------------------------------------
# image ---------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
@dataclass
class VistaImage:
    """Complete content of a Vista cache file, as separable regions.  ``header`` carries all *non-derived* fields
    (type, build string, name, scenario path, creation times, uses_shared_map, secondary_ugh_tag_index, mopp_checksum,
    reserved bytes ...).  Offsets/sizes/checksum are recomputed by ``write_image``."""

    header: VistaHeader
    raw_sounds: Union[bytes, RegionBuilder] = b""
    geometry: Optional[Union[bytes, RegionBuilder]] = None       # None -> header 0x2E8/0x2EC = (-1, 0)
    strings: List[str] = field(default_factory=list)              # string-id table, in index order
    string_block: Optional[bytes] = None                          # payload of count*128 bytes; None -> generated
    tag_names: List[Optional[str]] = field(default_factory=list)  # indexed by tag index; len = debug_tag_name_count
    tag_name_order: Optional[List[int]] = None                    # tag indices in buffer order (None -> ascending)
    locale_data: Union[bytes, RegionBuilder] = b""
    language_pack: Optional[bytes] = None                         # 252 bytes (9 x c_language_pack) or None
    bitmap_pixels: Union[bytes, RegionBuilder] = b""
    shared_graph: Optional[bytes] = None                          # exactly shared_tag_dependency_count bytes
    # tag region
    groups: List[TagGroupLink] = field(default_factory=list)
    tags: List[VistaTag] = field(default_factory=list)            # full instance table (addr/size explicit)
    tag_data: bytes = b""                                         # bytes at region offset data_offset .. (incl. tail pad)
    scenario_datum: int = NONE32
    globals_datum: int = NONE32
    tags_field_14: int = 0
    pad_after_instances: Optional[bytes] = None                   # bytes between instance table and data_offset (stock: stale junk)
    tag_offset_mask: int = 0
    crc: Optional[int] = None                                     # None -> compute from payloads; else written as is
    mopp_checksum: Optional[Union[int, str]] = None               # None -> keep header value; 'compute' -> XOR32 of MOPP data refs

    # --- construction from a parsed stock file -------------------------------------------------
    @classmethod
    def from_cache(cls, c: VistaCache, reconstruct_items: bool = False) -> "VistaImage":
        """Split a parsed stock file into regions.  With ``reconstruct_items=True`` (stock *multiplayer* maps) the
        sound/geometry/locale/bitmap regions are wrapped in RegionBuilders carrying the exact write boundaries, so
        ``serialize_image(img, 'compute')`` reproduces the header ``crc`` (see vista_cache.reconstruct_write_items)."""
        h = c.header.copy()
        regions = {n: (o, s) for n, o, s in c.sections()}
        mm = c._mm

        def blob(off, size):
            return bytes(mm[off:off + size])

        img = cls(header=h)
        # raw sounds: [0x800, first described region)
        o, s = regions["raw_sounds"] if "raw_sounds" in regions else (HEADER_SIZE, 0)
        img.raw_sounds = blob(o, s)
        if "geometry_blocks" in regions:
            o, s = regions["geometry_blocks"]
            img.geometry = blob(o, s)
        img.strings = c.all_strings()
        o, s = regions["string_block"]
        img.string_block = blob(o, s)
        img.tag_names = list(c._names)
        _hh = c.header
        _idx = struct.unpack_from('<%dI' % _hh.debug_tag_name_count, c._mm, _hh.tag_name_offset)
        img.tag_name_order = [i for i, _o in sorted(((i, o) for i, o in enumerate(_idx) if o != NONE32), key=lambda x: x[1])]
        # what lies between tag_name_idx and the language pack / bitmap area
        if "language_pack" in regions:
            lo, ls = regions["language_pack"]
            if "locale_data" in regions:
                o2, s2 = regions["locale_data"]
                img.locale_data = blob(o2, s2)
            img.language_pack = blob(lo, ls)
            bm_start = align_up(lo + ls, PAGE)
        else:
            idx_o, idx_s = regions["tag_name_idx"]
            bm_start = align_up(idx_o + idx_s, PAGE)
        end = regions["shared_dependency_graph"][0] if "shared_dependency_graph" in regions else h.tag_offset
        img.bitmap_pixels = blob(bm_start, end - bm_start)
        if "shared_dependency_graph" in regions:
            o, s = regions["shared_dependency_graph"]
            img.shared_graph = blob(o, h.shared_tag_dependency_count)
        # tag region
        img.groups = [TagGroupLink(g.group, g.parent, g.grandparent, g.raw) for g in c.groups]
        img.tags = [VistaTag(t.index, t.group, t.datum, t.addr, t.size, t.name, t.file_offset) for t in c.tags]
        inst_end = c.instance_table_off + 16 * c.tag_count
        img.pad_after_instances = c.tags_header_bytes[inst_end:h.data_offset]
        img.tag_data = blob(h.tag_offset + h.data_offset, h.data_size)
        img.scenario_datum, img.globals_datum, img.tags_field_14 = c.scenario_datum, c.globals_datum, c.tags_field_14
        img.tag_offset_mask = h.tag_offset_mask
        img.crc = h.crc
        img.mopp_checksum = h.mopp_checksum
        if reconstruct_items:
            items = reconstruct_write_items(c)

            snd_start = HEADER_SIZE
            img.raw_sounds = RegionBuilder.from_blob(img.raw_sounds, snd_start, [(o, s) for o, s, lab in items if lab == "snd"])
            if img.geometry is not None:
                img.geometry = RegionBuilder.from_blob(img.geometry, h.geometry_data_offset, [(o, s) for o, s, lab in items if lab == "geom"])
            if img.locale_data:
                img.locale_data = RegionBuilder.from_blob(img.locale_data, regions["locale_data"][0],
                                                          [(o, s) for o, s, lab in items if lab.startswith("loc_")])
            img.bitmap_pixels = RegionBuilder.from_blob(img.bitmap_pixels, bm_start, [(o, s) for o, s, lab in items if lab == "bitmap"])
            img.crc = None            # force recomputation from the reconstructed payload boundaries
        return img


class _ImageView:
    """Minimal VistaCache look-alike over an in-memory image (for mopp_checksum_of)."""

    def __init__(self, img: "VistaImage", data_offset: int):
        self.tags = img.tags
        self._d = img.tag_data
        self._base = img.tag_offset_mask + data_offset

    def read_addr(self, addr: int, size: int) -> bytes:
        o = addr - self._base
        return bytes(self._d[o:o + size])

    def read_tag(self, t: VistaTag) -> bytes:
        return self.read_addr(t.addr, t.size)


# --------------------------------------------------------------------------------------------------
# helpers for building sections --------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
def pack_string_table(strings: Sequence[str]) -> Tuple[bytes, bytes]:
    """-> (index bytes uint32[n], table bytes).  Strings are NUL-terminated and stored in index order
    (H2Tool sub_83DA10: idx[j] = running offset)."""
    if len(strings) > MAX_STRING_IDS:
        raise ValueError("too many string ids (%d > %d)" % (len(strings), MAX_STRING_IDS))
    offs, parts, cur = [], [], 0
    for s in strings:
        b = s.encode("latin-1") + b"\0"
        offs.append(cur)
        parts.append(b)
        cur += len(b)
    if cur >= MAX_STRING_ID_STORAGE:
        raise ValueError("string table too large (%#x >= %#x)" % (cur, MAX_STRING_ID_STORAGE))
    return struct.pack("<%dI" % len(offs), *offs), b"".join(parts)


def pack_string_block(strings: Sequence[str]) -> bytes:
    """string_block payload: one 128-byte slot per string (NUL padded, truncated to 127 chars)."""
    out = bytearray(128 * len(strings))
    for i, s in enumerate(strings):
        b = s.encode("latin-1")[:127]
        out[128 * i:128 * i + len(b)] = b
    return bytes(out)


def pack_tag_names(names: Sequence[Optional[str]], order: Optional[Sequence[int]] = None) -> Tuple[bytes, bytes]:
    """-> (name buffer, index bytes uint32[len(names)]).

    ``order`` = tag indices in the order their names appear in the buffer.  Stock maps write local tags in ascending
    index order and then the shared tags, but 14 of the shared names in headlong are out of ascending order (the old
    tool build 11081 appends some late), so exact reproduction needs the explicit order.  Default: ascending index.
    Unnamed indices hold 0xFFFFFFFF.  Names are truncated to 255 chars (H2Tool copies at most 0x100 bytes)."""
    buf = bytearray()
    offs: Dict[int, int] = {}
    seq = order if order is not None else [i for i, n in enumerate(names) if n is not None]
    for i in seq:
        n = names[i]
        if n is None:
            raise ValueError("tag_name_order lists unnamed tag %d" % i)
        b = n.encode("latin-1")[:MAX_TAG_NAME_LEN]
        offs[i] = len(buf)
        buf += b + b"\0"
    idx = [offs.get(i, NONE32) for i in range(len(names))]
    if len(buf) > MAX_TAG_NAME_BUFFER:
        raise ValueError("tag name buffer too large")
    return bytes(buf), struct.pack("<%dI" % len(idx), *idx)


def pack_tag_instance(t: VistaTag) -> bytes:
    return t.pack()


def build_tag_region_header(groups: Sequence[TagGroupLink], tags: Sequence[VistaTag], scenario_datum: int,
                            globals_datum: int, field_14: int, pad_after_instances: Optional[bytes] = None) -> Tuple[bytes, int]:
    """Tags header (0x20) + group links + instance table, padded to a 0x1000 multiple (= header ``data_offset``).
    Returns (bytes, data_offset)."""
    n_groups = len(groups)
    group_off = 0x20
    inst_off = group_off + 12 * n_groups
    body = bytearray()
    body += struct.pack("<IIIIIII", group_off, n_groups, inst_off, scenario_datum, globals_datum, field_14, len(tags))
    body += TAGS_SIGNATURE
    for g in groups:
        body += g.pack()
    for t in tags:
        body += t.pack()
    data_offset = align_up(len(body), TAG_REGION_ALIGN)
    pad = data_offset - len(body)
    if pad_after_instances is not None:
        if len(pad_after_instances) != pad:
            raise ValueError("pad_after_instances is %d bytes, layout needs %d" % (len(pad_after_instances), pad))
        body += pad_after_instances
    else:
        body += b"\0" * pad
    return bytes(body), data_offset


@dataclass
class NewTag:
    """A tag to be placed by ``assign_tag_addresses``/``build_tag_data``.

    ``data`` holds the tag's bytes with every internal pointer (tag_block address, data_reference address) written as
    an offset *relative to the start of this tag's data*; ``relocs`` lists the byte offsets of those u32 fields.  The
    writer adds the tag's final virtual address to each non-zero pointer."""
    group: str
    name: Optional[str]
    data: bytes
    relocs: Sequence[int] = ()
    index: Optional[int] = None       # requested absolute index (None -> next free local index)
    datum: Optional[int] = None       # full datum; None -> derived from index and salt


def make_datum(index: int, salt: Optional[int] = None) -> int:
    """datum = (salt << 16) | index.  Any salt works as long as references use the same full datum; default is
    0x4000 + index (stays clear of 0xFFFF/NONE for index < 10000)."""
    if salt is None:
        salt = (0x4000 + index) & 0xFFFE
        salt |= 0x0001
    return ((salt & 0xFFFF) << 16) | (index & 0xFFFF)


def assign_tag_addresses(tags: Sequence[NewTag], mask: int, data_offset: int) -> List[Tuple[VistaTag, bytes]]:
    """Place local tags sequentially (4-byte aligned, in list order = ascending index) starting at region offset
    ``data_offset``; apply relocations; return [(VistaTag, final_bytes)].  The tag region offset of the first tag's data is
    ``data_offset`` -> virtual address ``mask + data_offset``."""
    out = []
    cursor = data_offset
    for nt in tags:
        addr = mask + cursor
        data = bytearray(nt.data)
        if len(data) % TAG_DATA_ALIGN:
            data += b"\0" * (TAG_DATA_ALIGN - len(data) % TAG_DATA_ALIGN)
        for r in nt.relocs:
            v = struct.unpack_from("<I", data, r)[0]
            if v:
                struct.pack_into("<I", data, r, (v + addr) & 0xFFFFFFFF)
        idx = nt.index
        datum = nt.datum if nt.datum is not None else (make_datum(idx) if idx is not None else NONE32)
        t = VistaTag(idx if idx is not None else -1, nt.group, datum, addr, len(data), nt.name)
        out.append((t, bytes(data)))
        cursor += len(data)
    return out


# --------------------------------------------------------------------------------------------------
# layout planning + serialisation --------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
def _size(x) -> int:
    return len(x) if x is not None else 0


@dataclass
class VistaLayout:
    """Offsets of every region for a given set of region sizes (use before the final bytes exist: bitmap pixel
    offsets depend on everything before them)."""
    raw_sounds: int = HEADER_SIZE
    geometry: int = 0
    string_block: int = 0
    string_idx: int = 0
    string_table: int = 0
    tag_names: int = 0
    tag_name_idx: int = 0
    locale_data: int = 0
    language_pack: int = 0
    bitmap_pixels: int = 0
    shared_graph: int = 0
    tag_region: int = 0
    file_size: int = 0


def plan_layout(raw_sounds_size: int, geometry_size: int, string_count: int, string_table_size: int,
                tag_names_size: int, tag_name_count: int, locale_size: int, language_pack_size: int,
                bitmap_pixels_size: int, shared_graph_count: int, tag_region_size: int) -> VistaLayout:
    """Offsets for the given payload sizes (raw_sounds/geometry/locale/bitmap sizes must already be 0x200 multiples)."""
    L = VistaLayout()
    cur = HEADER_SIZE
    L.raw_sounds = cur; cur += align_up(raw_sounds_size, PAGE)
    L.geometry = cur; cur += align_up(geometry_size, PAGE)
    L.string_block = cur; cur += align_up(128 * string_count, PAGE)
    L.string_idx = cur; cur += align_up(4 * string_count, PAGE)
    L.string_table = cur; cur += align_up(string_table_size, PAGE)
    L.tag_names = cur; cur += align_up(tag_names_size, PAGE)
    L.tag_name_idx = cur; cur += align_up(4 * tag_name_count, PAGE)
    L.locale_data = cur; cur += align_up(locale_size, PAGE)
    L.language_pack = cur; cur += align_up(language_pack_size, PAGE)
    L.bitmap_pixels = cur; cur += align_up(bitmap_pixels_size, PAGE)
    L.shared_graph = cur; cur += align_up(shared_graph_count, PAGE)
    L.tag_region = cur
    L.file_size = cur + tag_region_size
    return L


def _bytes_of(x) -> bytes:
    if x is None:
        return b""
    if isinstance(x, RegionBuilder):
        return x.data()
    return bytes(x)


def _payload_sizes(x) -> List[int]:
    if isinstance(x, RegionBuilder):
        return list(x.payload_sizes)
    return [len(x)] if x else []


def serialize_image(img: VistaImage, crc_mode: str = "auto") -> Tuple[VistaHeader, List[bytes]]:
    """-> (final header, ordered list of byte chunks making up the file after the header).  ``crc_mode``: 'auto'
    (use img.crc if set else compute), 'compute', or 'keep'."""
    h = img.header.copy()
    sounds, geom, loc, bmp = (_bytes_of(img.raw_sounds), _bytes_of(img.geometry) if img.geometry is not None else None,
                              _bytes_of(img.locale_data), _bytes_of(img.bitmap_pixels))
    for nm, b in (("raw_sounds", sounds), ("geometry", geom or b""), ("locale_data", loc), ("bitmap_pixels", bmp)):
        if len(b) % PAGE:
            raise ValueError("%s size %#x is not a multiple of 0x200" % (nm, len(b)))
    nstr = len(img.strings)
    idx_b, tbl_b = pack_string_table(img.strings)
    sblock = img.string_block if img.string_block is not None else pack_string_block(img.strings)
    if len(sblock) != 128 * nstr:
        raise ValueError("string_block must be 128 * string_count bytes")
    names_b, nidx_b = pack_tag_names(img.tag_names, img.tag_name_order)
    th, data_offset = build_tag_region_header(img.groups, img.tags, img.scenario_datum, img.globals_datum,
                                              img.tags_field_14, img.pad_after_instances)
    tag_data = bytes(img.tag_data)
    if len(tag_data) % TAG_REGION_ALIGN:
        tag_data += b"\0" * (TAG_REGION_ALIGN - len(tag_data) % TAG_REGION_ALIGN)
    data_size = len(tag_data)
    region_size = data_offset + data_size
    # --- sections in order ---------------------------------------------------------------------------------------
    chunks: List[bytes] = []
    payload_blobs: List[bytes] = []   # payload (non-pad) bytes in write order, for the H2Tool-style CRC

    def emit(blob: bytes, pieces: Optional[List[int]] = None):
        """Append blob + zero pad to 0x200 as ONE chunk.  ``pieces`` = payload sizes of the sub-writes the blob is made of."""
        pad = (-len(blob)) % PAGE
        chunks.append(blob + b"\0" * pad if pad else blob)
        if pieces is None:
            payload_blobs.append(blob)
        else:
            pos = 0
            for s_ in pieces:
                payload_blobs.append(blob[pos:pos + s_])
                pos += align_up(s_, PAGE)

    cur = HEADER_SIZE
    emit(sounds, _payload_sizes(img.raw_sounds) if isinstance(img.raw_sounds, RegionBuilder) else None)
    cur += len(sounds)
    if geom is None:
        h.geometry_data_offset, h.geometry_data_size = NONE32, 0
    else:
        h.geometry_data_offset, h.geometry_data_size = cur, len(geom)
        emit(geom, _payload_sizes(img.geometry) if isinstance(img.geometry, RegionBuilder) else None)
        cur += len(geom)
    h.string_block_offset, h.string_table_count = cur, nstr
    emit(sblock); cur += align_up(len(sblock), PAGE)
    h.string_idx_offset = cur
    emit(idx_b); cur += align_up(len(idx_b), PAGE)
    h.string_table_offset, h.string_table_size = cur, len(tbl_b)
    emit(tbl_b); cur += align_up(len(tbl_b), PAGE)
    h.tag_name_buffer_offset, h.tag_name_buffer_size, h.debug_tag_name_count = cur, len(names_b), len(img.tag_names)
    emit(names_b); cur += align_up(len(names_b), PAGE)
    h.tag_name_offset = cur
    emit(nidx_b); cur += align_up(len(nidx_b), PAGE)
    if loc:
        emit(loc, _payload_sizes(img.locale_data) if isinstance(img.locale_data, RegionBuilder) else None)
        cur += len(loc)
    if img.language_pack is None:
        h.language_pack_offset, h.language_pack_size = NONE32, 0
    else:
        h.language_pack_offset, h.language_pack_size = cur, len(img.language_pack)
        emit(bytes(img.language_pack)); cur += align_up(len(img.language_pack), PAGE)
    if bmp:
        emit(bmp, _payload_sizes(img.bitmap_pixels) if isinstance(img.bitmap_pixels, RegionBuilder) else None)
        cur += len(bmp)
    if img.shared_graph is None:
        h.shared_tag_dependency_offset, h.shared_tag_dependency_count = NONE32, 0
    else:
        h.shared_tag_dependency_offset, h.shared_tag_dependency_count = cur, len(img.shared_graph)
        emit(bytes(img.shared_graph)); cur += align_up(len(img.shared_graph), PAGE)
    # tag region (single write; the CRC covers the whole 4 KiB padded region)
    h.tag_offset, h.data_offset, h.data_size = cur, data_offset, data_size
    h.tag_size = region_size
    h.tag_offset_mask = img.tag_offset_mask
    chunks.append(th)
    chunks.append(tag_data)
    payload_blobs.append(th + tag_data)
    cur += region_size
    h.file_size = cur
    # --- checksums ---------------------------------------------------------------------------------------------
    acc = 0
    for ch in chunks:
        acc ^= xor32(ch)
    h.checksum = acc
    if img.mopp_checksum == 'compute':
        h.mopp_checksum = mopp_checksum_of(_ImageView(img, data_offset))
    elif img.mopp_checksum is not None:
        h.mopp_checksum = img.mopp_checksum
    use_given = (crc_mode == "keep") or (crc_mode == "auto" and img.crc is not None)
    if use_given:
        h.crc = img.crc if img.crc is not None else h.crc
    else:
        c = 0xFFFFFFFF
        for b in payload_blobs:
            c = jamcrc_update(c, b)
        h.crc = c
    h.crc_valid = 1
    return h, chunks


def write_image(img: VistaImage, path: Optional[str] = None, crc_mode: str = "auto") -> bytes:
    """Serialise ``img``; if ``path`` is given the file is written there (streaming).  Always returns the header
    bytes (0x800) -- the full file only if ``path`` is None (then it returns the whole file)."""
    h, chunks = serialize_image(img, crc_mode)
    hdr = h.pack()
    if path is None:
        return hdr + b"".join(chunks)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(hdr)
        for c in chunks:
            f.write(c)
    os.replace(tmp, path)
    return hdr


# --------------------------------------------------------------------------------------------------
# verification of a finished file --------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
def validate_file(path: str) -> List[str]:
    """Checks every invariant halo2.exe / Cartographer test: header verify, size, checksum, region layout, tag table."""
    import mmap
    problems: List[str] = []
    c = VistaCache(path)
    h = c.header
    problems += h.verify()
    if h.file_size != c.size:
        problems.append("header file_size %#x != actual %#x" % (h.file_size, c.size))
    if c.size % PAGE:
        problems.append("file size not a multiple of 0x200")
    if c.size > MAX_MP_FILE_SIZE and h.type in (0, 1):
        problems.append("file larger than 0xFA00000: not accepted by the custom map list")
    if xor32(c._mm, HEADER_SIZE, c.size) != h.checksum:
        problems.append("checksum (0x2F0) != XOR32 of [0x800, EOF)")
    if h.tag_offset + h.tag_size != c.size:
        problems.append("tag region does not end at EOF")
    if h.data_offset + h.data_size != h.tag_size:
        problems.append("data_offset + data_size != tag_size")
    for nm in ("tag_offset", "string_block_offset", "string_idx_offset", "string_table_offset", "tag_name_buffer_offset", "tag_name_offset"):
        if getattr(h, nm) % PAGE:
            problems.append("%s not 0x200-aligned" % nm)
    if h.data_offset % TAG_REGION_ALIGN or h.data_size % TAG_REGION_ALIGN:
        problems.append("tag region parts not 4 KiB aligned")
    live = [t for t in c.tags if not t.is_empty and t.addr]
    lo = h.tag_offset_mask + h.data_offset
    hi = h.tag_offset_mask + h.tag_size
    for t in live:
        if not (lo <= t.addr and t.addr + t.size <= hi):
            problems.append("tag %d (%s) data outside tag region" % (t.index, t.group))
            break
        if t.addr % 4 or t.size % 4:
            problems.append("tag %d not 4-byte aligned" % t.index)
            break
    sc = c.tags[c.scenario_datum & 0xFFFF]
    if sc.group != "scnr":
        problems.append("scenario datum does not reference an scnr")
    if c.globals_datum != NONE32 and c.tags[c.globals_datum & 0xFFFF].group != "matg":
        problems.append("globals datum does not reference a matg")
    c.close()
    return problems


# --------------------------------------------------------------------------------------------------
# new-map helpers --------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
def filetime_now() -> int:
    """Windows FILETIME (100 ns ticks since 1601-01-01) for the header ``creation_time``."""
    import time
    return int(time.time() * 10_000_000) + 116444736000000000


def read_shared_creation_times(maps_dir: str) -> Tuple[int, int, int]:
    """creation_time of (mainmenu.map, shared.map, single_player_shared.map): the values a map built against those
    databases stores in header ``shared_creation_time[0..2]`` (verified equal for all 43 stock Vista maps)."""
    out = []
    for n in ("mainmenu.map", "shared.map", "single_player_shared.map"):
        with open(os.path.join(maps_dir, n), "rb") as f:
            out.append(VistaHeader(f.read(HEADER_SIZE)).creation_time)
    return tuple(out)  # type: ignore


def new_header(name: str, scenario_path: str, map_type: int = 1, build: str = "11081.07.04.30.0934.main",
               uses_shared: Tuple[int, int, int] = (1, 1, 0), creation_time: Optional[int] = None,
               shared_creation_time: Tuple[int, int, int] = (0, 0, 0), secondary_ugh_tag_index: int = NONE32,
               mopp_checksum: int = 0, tag_offset_mask: Optional[int] = None) -> VistaHeader:
    """Header for a new map with every constant stock field (all verified identical across the 43 stock maps) set:
    version 8, empty ``path``, all reserved/pad fields zero, ``crc_valid`` = 1.  Derived fields are filled in later by
    ``write_image``.  For a custom multiplayer map: ``map_type=1``, ``uses_shared=(1,1,0)`` (mainmenu + shared) and
    ``shared_creation_time=read_shared_creation_times(maps_dir)`` masked by ``uses_shared``."""
    if len(name) >= 32:
        raise ValueError("name must be < 32 chars")
    h = VistaHeader()
    h.version = 8
    h.version_string = build
    h.type = map_type
    h.name = name
    h.scenario_path = scenario_path
    h.uses_shared_map = tuple(uses_shared)
    h.creation_time = creation_time if creation_time is not None else filetime_now()
    h.shared_creation_time = tuple(t if u else 0 for t, u in zip(shared_creation_time, uses_shared))
    h.secondary_ugh_tag_index = secondary_ugh_tag_index
    h.mopp_checksum = mopp_checksum
    h.crc_valid = 1
    return h


def groups_from_stock(path: str) -> List[TagGroupLink]:
    """The 120-entry tag group link table (identical in every stock Vista map; it describes the engine build)."""
    c = VistaCache(path)
    g = [TagGroupLink(x.group, x.parent, x.grandparent, x.raw) for x in c.groups]
    c.close()
    return g


class VistaBuilder:
    """Staged builder for a new map.  Each stage's absolute file offsets are only known once all earlier regions have
    their final size, so the stages must be driven in this order (a later stage seals the earlier ones):

        1. ``add_sound(blob)``                  -> absolute file offset (for sound chunk ``file_offset``)
        2. ``add_geometry(blob)``               -> absolute file offset (geometry_block_info.block_offset)
        3. ``set_strings`` / ``set_tag_names``  (sizes only), then ``locale_base`` is known
           ``set_locale(locale_blob, language_pack)``
        4. ``add_bitmap_pixels(blob)``          -> absolute file offset (bitmap lod offsets)
        5. ``set_tags(...)``; ``build_image()`` / ``write(path)``

    Offsets returned by ``add_*`` are plain file offsets: store them directly in the tag (or OR in
    ``encode_raw_pointer(db, off)`` bits for a shared-database reference)."""

    def __init__(self, header: VistaHeader, groups: Sequence[TagGroupLink], tag_offset_mask: int = 0x1400000):
        self.header = header
        self.groups = list(groups)
        self.mask = tag_offset_mask
        self._sounds = RegionBuilder()
        self._geometry: Optional[RegionBuilder] = None
        self._bitmaps: Optional[RegionBuilder] = None
        self._stage = 1
        self.strings: List[str] = []
        self.tag_names: List[Optional[str]] = []
        self.tag_name_order: Optional[List[int]] = None
        self._locale: bytes = b""
        self._langpack: Optional[bytes] = None
        self._geom_base = self._bitmap_base = None
        self._tables_set = [False, False]
        self.tags: List[VistaTag] = []
        self.tag_data = b""
        self.scenario_datum = self.globals_datum = NONE32

    # stage 1 -------------------------------------------------------------------------------------------
    def add_sound(self, data) -> int:
        if self._stage > 1:
            raise RuntimeError("sound region already sealed")
        return HEADER_SIZE + self._sounds.add(data)

    # stage 2 -------------------------------------------------------------------------------------------
    def _seal_sounds(self):
        if self._stage < 2:
            self._stage = 2
            self._geom_base = HEADER_SIZE + len(self._sounds)
            self._geometry = RegionBuilder()

    def add_geometry(self, data) -> int:
        self._seal_sounds()
        if self._stage > 2:
            raise RuntimeError("geometry region already sealed")
        return self._geom_base + self._geometry.add(data)

    # stage 3 -------------------------------------------------------------------------------------------
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
        """Absolute file offset where the locale data blob will start (valid after set_strings + set_tag_names)."""
        if not all(self._tables_set):
            raise RuntimeError("call set_strings() and set_tag_names() first")
        geom = len(self._geometry) if self._geometry is not None else 0
        names_b, _ = pack_tag_names(self.tag_names, self.tag_name_order)
        _, tbl = pack_string_table(self.strings)
        n = len(self.strings)
        cur = (HEADER_SIZE + len(self._sounds) + geom + align_up(128 * n, PAGE) + align_up(4 * n, PAGE) +
               align_up(len(tbl), PAGE) + align_up(len(names_b), PAGE) + align_up(4 * len(self.tag_names), PAGE))
        return cur

    @property
    def language_pack_offset(self) -> int:
        return self.locale_base + len(self._locale)

    def set_locale(self, locale_data: bytes, language_pack: Optional[bytes]):
        """``locale_data``: concatenated per-language blobs, each padded to 0x200 (so len % 0x200 == 0);
        ``language_pack``: 252 bytes (9 x c_language_pack) whose index/data offsets are absolute file offsets
        (``locale_base`` + position), or None for maps without a pack."""
        if not all(self._tables_set):
            raise RuntimeError("call set_strings() and set_tag_names() first")
        if len(locale_data) % PAGE:
            raise ValueError("locale_data must be a multiple of 0x200")
        if language_pack is not None and len(language_pack) != 252:
            raise ValueError("language pack must be 252 bytes")
        self._locale = bytes(locale_data)
        self._langpack = None if language_pack is None else bytes(language_pack)

    # stage 4 -------------------------------------------------------------------------------------------
    def _seal_locale(self):
        if not all(self._tables_set):
            raise RuntimeError("call set_strings() and set_tag_names() before adding bitmap pixels")
        if self._stage < 4:
            self._stage = 4
            self._bitmap_base = self.language_pack_offset + (align_up(len(self._langpack), PAGE) if self._langpack else 0)
            self._bitmaps = RegionBuilder()

    @property
    def bitmap_base(self) -> int:
        self._seal_locale()
        return self._bitmap_base

    def add_bitmap_pixels(self, data) -> int:
        self._seal_locale()
        if self._stage > 4:
            raise RuntimeError("bitmap region already sealed")
        return self._bitmap_base + self._bitmaps.add(data)

    # stage 5 -------------------------------------------------------------------------------------------
    def set_tags(self, tags: Sequence[VistaTag], tag_data: bytes, scenario_datum: int, globals_datum: int,
                 tags_field_14: int = 0):
        """``tags``: the complete instance table (len = tag_count; empty slots = VistaTag(i,'',0xFFFFFFFF,0,0); shared
        placeholders = group + shared.map datum with addr 0/size 0; local tags with final addr/size);
        ``tag_data``: the bytes from region offset data_offset on (use ``assign_tag_addresses`` + concatenate)."""
        self._seal_locale()
        self._stage = 5
        self.tags = list(tags)
        self.tag_data = bytes(tag_data)
        self.scenario_datum, self.globals_datum, self.tags_field_14 = scenario_datum, globals_datum, tags_field_14

    def tag_region_data_offset(self, tag_count: int) -> int:
        """``data_offset`` (first tag's offset inside the tag region) for a given instance-table length."""
        return align_up(0x20 + 12 * len(self.groups) + 16 * tag_count, TAG_REGION_ALIGN)

    def build_image(self) -> VistaImage:
        if self._stage < 5:
            raise RuntimeError("set_tags() not called")
        return VistaImage(
            header=self.header, raw_sounds=self._sounds, geometry=self._geometry if (self._geometry and len(self._geometry)) else None,
            strings=self.strings, tag_names=self.tag_names, tag_name_order=self.tag_name_order, locale_data=self._locale,
            language_pack=self._langpack, bitmap_pixels=self._bitmaps if self._bitmaps is not None else b"",
            groups=self.groups, tags=self.tags, tag_data=self.tag_data, scenario_datum=self.scenario_datum,
            globals_datum=self.globals_datum, tags_field_14=self.tags_field_14, tag_offset_mask=self.mask, crc=None)

    def write(self, path: str) -> bytes:
        return write_image(self.build_image(), path)
