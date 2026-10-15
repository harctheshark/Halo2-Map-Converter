# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""MCC Halo 2 cache-file (version 13) container reader.

Pure stdlib (struct, mmap, zlib, json, os).

Everything here is derived from, and verified against, the real MCC maps.  Short version of the format:

* 0x380-byte header ('head' stored little-endian => bytes "daeh", version 13, 'foot' at 0x37C).
* Compressed MCC maps (flags u16 @0x1C bit0): header is kept verbatim, then the body is split into
  chunks of header[0x308] bytes (0x40000).  Chunk table (header[0x310], count header[0x314]) = array of
  {u32 compressed_size, u32 file_offset}; every chunk is a standard zlib stream (RFC1950).
  Decompressed file == header (with flag bit0 cleared) + concatenated chunks.
* Uncompressed layout:  header | raw table [0x2F0,+0x2F4) | string-id index | string-id data |
  file-name data | file-name index | locale area | meta (= tag groups, tag table, tag data) at header[0x10].
* Memory addresses inside tag data:  file_offset = meta_offset + (address - header[0x2D0])
  (header[0x2D0] = "meta offset mask" = base address of the meta, 0x3200000 for MP maps that rely on
  shared.map, 0 otherwise).  Tags whose data lives in shared.map have address 0 / size 0 in the table.
* There are NO separate structure-bsp sections in v13: scenario.structure_bsps[i] has offset/size/address
  all 0 and the sbsp / ltmp tags are ordinary entries of the tag table.

Public API:  MccCache(path), decompress_map(src, dst), fourcc(), MccTag.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # not Windows / no kernel32
    pass

import hashlib
import json
import mmap
import os
import struct
import sys
import zlib

try:
    from . import paths as _paths
except ImportError:      # run as a script
    import paths as _paths

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
WORK_DIR = _paths.work_dir()                                        # H2CONV_WORK / settings.json "work_dir" / %LOCALAPPDATA%\H2MapConverter\work
DECOMP_DIR = os.path.join(WORK_DIR, 'mcc_decompressed')


def __getattr__(name):
    """STOCK_MAPS_DIR (the installed MCC maps folder) is resolved by h2conv.paths; kept as an attribute for the developer tools."""
    if name == 'STOCK_MAPS_DIR':
        return _paths.mcc_maps_dir()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))

HEADER_SIZE = 0x380
NULL_DATUM = 0xFFFFFFFF
NULL_GROUP = 0xFFFFFFFF
MCC_VERSION = 13

SCENARIO_TYPES = {0: 'solo', 1: 'multiplayer', 2: 'mainmenu', 3: 'multiplayer_shared', 4: 'single_player_shared'}
LANGUAGE_NAMES = ['english', 'japanese', 'german', 'french', 'spanish', 'italian', 'korean', 'chinese',
                  'portuguese']   # 9 entries; slot 8 is the "neutral"/button-glyph table in practice (see docs)


class MccFormatError(Exception):
    pass


class SharedAddressError(MccFormatError):
    """Address lies below the meta offset mask: it belongs to the shared map, not this file."""


# --------------------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------------------
def fourcc(value):
    """int (as stored little-endian) -> 'scnr'.  0xFFFFFFFF -> None."""
    if value == NULL_GROUP:
        return None
    return struct.pack('<I', value)[::-1].decode('latin-1')


def fourcc_int(text):
    """'scnr' -> int as stored in the file."""
    if text is None:
        return NULL_GROUP
    return struct.unpack('<I', text.encode('latin-1')[::-1])[0]


def _cstr(buf, off=0, enc='utf-8'):
    end = buf.find(b'\0', off)
    if end < 0:
        end = len(buf)
    raw = bytes(buf[off:end])
    try:
        return raw.decode(enc)
    except UnicodeDecodeError:
        return raw.decode('latin-1')


def filetime_to_iso(ft):
    if not ft:
        return None
    import datetime
    try:
        return (datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=ft // 10)).isoformat() + 'Z'
    except OverflowError:
        return None


# --------------------------------------------------------------------------------------------------
# header
# --------------------------------------------------------------------------------------------------
def parse_header(buf):
    """Parse the 0x380-byte v13 header.  Field names follow Assembly's H2MCC_LayoutsU1_Core.xml
    where it exists; offsets that Assembly does not know are named after the offset (unk_XXXX)."""
    if len(buf) < HEADER_SIZE:
        raise MccFormatError('header truncated')
    u32 = lambda o: struct.unpack_from('<I', buf, o)[0]
    i32 = lambda o: struct.unpack_from('<i', buf, o)[0]
    u16 = lambda o: struct.unpack_from('<H', buf, o)[0]
    u64 = lambda o: struct.unpack_from('<Q', buf, o)[0]
    sig = bytes(buf[0:4])[::-1].decode('latin-1')
    h = {
        'signature': sig,                               # 'head'
        'version': u32(0x04),                           # 13
        'file_size': u32(0x08),
        'unk_000C': u32(0x0C),                          # 0x201 in every MCC map seen
        'meta_offset': u32(0x10),
        'meta_size': u32(0x14),
        'scenario_type': u16(0x18),                     # 0 solo 1 mp 2 mainmenu 3 mp-shared 4 sp-shared
        'unk_001A': u16(0x1A),
        'flags': u16(0x1C),                             # bit0 = body is chunk-compressed (zlib)
        'unk_001E': u16(0x1E),                          # 0x7A/0x7E/0x78 in stock maps, 0 in H2EK-built
        'file_table_count': i32(0x20),                  # tag-name table (indexed by tag index)
        'file_table_offset': u32(0x24),
        'file_table_size': i32(0x28),
        'file_index_table_offset': u32(0x2C),
        'string_table_count': i32(0x30),                # string_id table
        'string_table_offset': u32(0x34),
        'string_table_size': i32(0x38),
        'string_index_table_offset': u32(0x3C),
        'creation_time': u64(0x48),                     # FILETIME
        'shared_creation_time': [u64(0x50), u64(0x58), u64(0x60)],   # [mainmenu, mp-shared, sp-shared]
        'build_string': _cstr(buf, 0x90) if bytes(buf[0x90:0xB0]).strip(b'\0') else '',
        'internal_name': _cstr(buf, 0xB0),
        'scenario_name': _cstr(buf, 0xD0),
        'meta_offset_mask': u32(0x2D0),                 # base *address* of the meta in memory
        'tag_data_offset': u32(0x2D4),                  # first tag-data byte, relative to meta start
        'tag_data_size': i32(0x2D8),
        'unk_02DC': u32(0x2DC),                         # -1 except shared.map (0x1635680)
        'unk_02E0': u32(0x2E0),                         # 0  except shared.map (0x22180)
        'locale_globals_offset': u32(0x2E4),            # -1 when absent (SP maps / H2EK-built)
        'locale_globals_size': u32(0x2E8),              # 0xFC when present
        'sound_gestalt_datum': u32(0x2EC),
        'raw_table_offset': u32(0x2F0),
        'raw_table_size': i32(0x2F4),
        'checksum': u32(0x2F8),
        'unk_02FC': u32(0x2FC),
        'unk_0300': u32(0x300),
        'compression_chunk_size': u32(0x308),
        'compression_data_offset': u32(0x30C),
        'compression_chunk_table_offset': u32(0x310),
        'compression_chunk_count': u32(0x314),
        'footer': bytes(buf[0x37C:0x380])[::-1].decode('latin-1'),   # 'foot'
    }
    h['compressed'] = bool(h['flags'] & 1)
    h['scenario_type_name'] = SCENARIO_TYPES.get(h['scenario_type'], 'unknown(%d)' % h['scenario_type'])
    h['meta_end_address'] = h['meta_offset_mask'] + h['meta_size']
    h['creation_time_iso'] = filetime_to_iso(h['creation_time'])
    h['raw'] = bytes(buf[:HEADER_SIZE])
    return h


def read_header(path):
    with open(path, 'rb') as f:
        return parse_header(f.read(HEADER_SIZE))


# --------------------------------------------------------------------------------------------------
# decompression
# --------------------------------------------------------------------------------------------------
def read_chunk_table(f, header):
    """-> list of (size, file_offset).  *size* is a signed int32: > 0 = zlib stream of that many bytes,
    < 0 = chunk stored UNcompressed with length -size (seen in 06b_floodzone chunk 223: 0xFFFC0000)."""
    n = header['compression_chunk_count']
    off = header['compression_chunk_table_offset']
    f.seek(off)
    tbl = f.read(n * 8)
    if len(tbl) != n * 8:
        raise MccFormatError('chunk table truncated')
    return list(struct.iter_unpack('<iI', tbl))


def iter_decompressed_chunks(f, header):
    """Yield the decompressed bytes of every chunk of a compressed MCC map, in order."""
    chunk_size = header['compression_chunk_size']
    for i, (csize, coff) in enumerate(read_chunk_table(f, header)):
        f.seek(coff)
        if csize < 0:                    # negative size => stored raw, length = -size
            blob = f.read(-csize)
            if len(blob) != -csize:
                raise MccFormatError('chunk %d truncated' % i)
            yield blob
            continue
        blob = f.read(csize)
        if len(blob) != csize:
            raise MccFormatError('chunk %d truncated' % i)
        d = zlib.decompressobj()
        out = d.decompress(blob) + d.flush()
        if not d.eof or d.unused_data:
            raise MccFormatError('chunk %d: zlib stream not cleanly terminated' % i)
        if len(out) > chunk_size:
            raise MccFormatError('chunk %d inflates to 0x%X > chunk size' % (i, len(out)))
        yield out


def decompress_map(src, dst):
    """Decompress a compressed MCC .map to *dst*.  Result is byte-identical to what Bungie/343's tool wrote
    for an untouched stock copy: header with flag bit0 cleared (everything else, including the stale
    compression fields 0x308..0x314, untouched) + all chunks.  Returns a stats dict."""
    tmp = '%s.%d.part' % (dst, os.getpid())
    with open(src, 'rb') as f:
        hdr_bytes = f.read(HEADER_SIZE)
        h = parse_header(hdr_bytes)
        if h['version'] != MCC_VERSION or h['signature'] != 'head':
            raise MccFormatError('%s: not an MCC v13 cache (sig=%r ver=%d)' % (src, h['signature'], h['version']))
        if not h['compressed']:
            raise MccFormatError('%s: flags bit0 clear - not compressed' % src)
        new_hdr = bytearray(hdr_bytes)
        struct.pack_into('<H', new_hdr, 0x1C, h['flags'] & ~1)
        total = 0
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        try:
            with open(tmp, 'wb') as out:
                out.write(new_hdr)
                total += len(new_hdr)
                nchunks = 0
                for data in iter_decompressed_chunks(f, h):
                    out.write(data)
                    total += len(data)
                    nchunks += 1
        except BaseException:                      # a truncated / damaged input: do not leave our half-written '<dst>.<pid>.part' in the cache folder
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    if total != h['file_size']:
        os.remove(tmp)
        raise MccFormatError('%s: decompressed %d bytes, header says %d' % (src, total, h['file_size']))
    if os.path.exists(dst):
        os.remove(dst)
    os.replace(tmp, dst)
    return {'src': src, 'dst': dst, 'chunks': nchunks, 'size': total, 'compressed_size': os.path.getsize(src)}


def decompress_to_bytes(src):
    """Same result as decompress_map() but returned in memory (bytes) - fine for maps up to a few hundred MB."""
    with open(src, 'rb') as f:
        hb = f.read(HEADER_SIZE)
        h = parse_header(hb)
        if h['version'] != MCC_VERSION or h['signature'] != 'head' or not h['compressed']:
            raise MccFormatError('%s: not a compressed MCC v13 cache' % src)
        out = bytearray(hb)
        struct.pack_into('<H', out, 0x1C, h['flags'] & ~1)
        for data in iter_decompressed_chunks(f, h):
            out += data
    if len(out) != h['file_size']:
        raise MccFormatError('%s: decompressed %d bytes, header says %d' % (src, len(out), h['file_size']))
    return bytes(out)


def _same_decompressed(src_hdr_bytes, dst):
    """Is *dst* an existing complete decompression of a map with header *src_hdr_bytes*?"""
    try:
        with open(dst, 'rb') as f:
            d = f.read(HEADER_SIZE)
        if len(d) < HEADER_SIZE:
            return False
        a = bytearray(src_hdr_bytes)
        struct.pack_into('<H', a, 0x1C, struct.unpack_from('<H', a, 0x1C)[0] & ~1)
        return bytes(a) == d and os.path.getsize(dst) == struct.unpack_from('<I', d, 8)[0]
    except OSError:
        return False


def ensure_decompressed(path, out_dir=None):
    """Return the path of an uncompressed copy of *path* (path itself if it is not compressed).
    Cached under <work folder>/mcc_decompressed/<basename>; a different map with the same basename gets a hash suffix."""
    with open(path, 'rb') as f:
        hb = f.read(HEADER_SIZE)
    h = parse_header(hb)
    if not h['compressed']:
        return path
    out_dir = out_dir or DECOMP_DIR
    base = os.path.basename(path)
    dst = os.path.join(out_dir, base)
    if _same_decompressed(hb, dst):
        return dst
    if os.path.exists(dst):   # basename taken by a different map -> hashed name
        stem, ext = os.path.splitext(base)
        dst = os.path.join(out_dir, '%s.%s%s' % (stem, hashlib.md5(hb).hexdigest()[:8], ext))
        if _same_decompressed(hb, dst):
            return dst
    decompress_map(path, dst)
    return dst


# --------------------------------------------------------------------------------------------------
# tags
# --------------------------------------------------------------------------------------------------
class MccTag(object):
    __slots__ = ('index', 'group_int', 'datum', 'addr', 'size', 'file_offset', 'name', 'group')

    def __init__(self, index, group_int, datum, addr, size, file_offset, name):
        self.index = index
        self.group_int = group_int
        self.group = fourcc(group_int)        # None for the unused (0xFFFFFFFF) slots
        self.datum = datum
        self.addr = addr
        self.size = size
        self.file_offset = file_offset        # None when the data is not in this file
        self.name = name

    @property
    def salt(self):
        return self.datum >> 16

    @property
    def is_null(self):
        return self.group_int == NULL_GROUP

    @property
    def has_data(self):
        """tag data (size > 0) is stored in this file."""
        return self.file_offset is not None and self.size > 0

    @property
    def external(self):
        """valid tag with addr == 0 and size == 0: its data lives in shared.map / single_player_shared.map
        (same tag index there)."""
        return (not self.is_null) and self.addr == 0 and self.size == 0

    @property
    def empty(self):
        """valid tag with a non-zero address but size 0 (31 effe/pmov/jpt!/colo/pphy tags in the H2EK-built
        headlong; the address aliases a neighbouring tag's data)."""
        return (not self.is_null) and self.addr != 0 and self.size == 0

    def to_dict(self):
        return {
            'index': self.index, 'group': self.group, 'name': self.name,
            'datum': self.datum, 'datum_hex': '0x%08X' % self.datum,
            'addr': self.addr, 'addr_hex': '0x%X' % self.addr,
            'size': self.size,
            'file_offset': self.file_offset,
            'file_offset_hex': None if self.file_offset is None else '0x%X' % self.file_offset,
            'external': self.external,
            'empty': self.empty,
        }

    def __repr__(self):
        return '<MccTag #%d %s %r datum=0x%08X addr=0x%X size=0x%X>' % (
            self.index, self.group, self.name, self.datum, self.addr, self.size)


# --------------------------------------------------------------------------------------------------
# the cache
# --------------------------------------------------------------------------------------------------
class MccCache(object):
    """Reader for an MCC Halo 2 (v13) .map.  Compressed inputs are transparently decompressed to
    <work folder>/mcc_decompressed/<name> (cached) and that file is memory-mapped."""

    def __init__(self, path, decompress_dir=None, load_names=True, in_memory=False):
        """path: any MCC v13 .map (compressed or not).  Compressed input is inflated to
        <decompress_dir or <work folder>/mcc_decompressed>/<basename> once and mmap'ed, or - with in_memory=True -
        inflated into a bytes object and nothing is written to disk."""
        self.source_path = os.path.abspath(path)
        with open(self.source_path, 'rb') as f:
            src_hdr = parse_header(f.read(HEADER_SIZE))
        if src_hdr['signature'] != 'head' or src_hdr['footer'] != 'foot':
            raise MccFormatError('%s: bad head/foot signature' % path)
        if src_hdr['version'] != MCC_VERSION:
            raise MccFormatError('%s: cache version %d, expected 13' % (path, src_hdr['version']))
        self.was_compressed = src_hdr['compressed']
        self._fh = None
        if in_memory and src_hdr['compressed']:
            self.path = self.source_path
            self._mm = decompress_to_bytes(self.source_path)
        else:
            self.path = ensure_decompressed(self.source_path, decompress_dir)
            self._fh = open(self.path, 'rb')
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        self.file_size = len(self._mm)
        self.header = parse_header(self._mm[:HEADER_SIZE])
        if self.header['compressed']:
            raise MccFormatError('%s still has the compressed flag set' % self.path)
        if self.header['file_size'] != self.file_size:
            raise MccFormatError('%s: header file size %d != real size %d' %
                                 (self.path, self.header['file_size'], self.file_size))
        self.mask = self.header['meta_offset_mask']
        self.meta_offset = self.header['meta_offset']
        self.meta_size = self.header['meta_size']
        self._parse_meta()
        self._names = None
        self._string_ids = None
        self._sid_lookup = None
        if load_names:
            self._load_names()

    # ----------------------------------------------------------------------------------------------
    def close(self):
        try:
            if hasattr(self._mm, 'close'):
                self._mm.close()
        finally:
            if self._fh is not None:
                self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---- meta / groups / tags ------------------------------------------------------------------
    def _parse_meta(self):
        mm, mo = self._mm, self.meta_offset
        (gto, ng, tto, scn, mg, x14, ntags, sig) = struct.unpack_from('<iiiIIIii', mm, mo)
        if sig != 0x74616773:      # 'tags'
            raise MccFormatError('meta header signature 0x%08X != "tags"' % sig)
        self.meta_header = {
            'tag_group_table_offset': gto, 'number_of_tag_groups': ng, 'tag_table_offset': tto,
            'scenario_datum': scn, 'globals_datum': mg, 'unk_14': x14, 'number_of_tags': ntags,
            'signature': 'tags',
        }
        self.scenario_datum = scn
        self.globals_datum = mg
        # tag groups: (group, parent, grandparent) fourcc ints; group table is relative to meta start
        self.groups = {}
        for i in range(ng):
            g, p, gp = struct.unpack_from('<III', mm, mo + gto + 12 * i)
            self.groups[fourcc(g)] = (fourcc(p), fourcc(gp))
        # tag table
        tags = []
        data_lo = mo + self.header['tag_data_offset']
        data_hi = mo + self.meta_size
        for i, (g, d, a, s) in enumerate(struct.iter_unpack(
                '<IIII', mm[mo + tto: mo + tto + 16 * ntags])):
            fo = None
            if g != NULL_GROUP and a != 0:
                fo = mo + (a - self.mask)
                if not (data_lo <= fo and fo + s <= data_hi):
                    raise MccFormatError('tag #%d %s addr 0x%X size 0x%X outside tag data' % (i, fourcc(g), a, s))
            tags.append(MccTag(i, g, d, a, s, fo, None))
        self.tags = tags
        self._by_datum = None

    def _load_names(self):
        h, mm = self.header, self._mm
        n = h['file_table_count']
        if n != len(self.tags):
            raise MccFormatError('file table count %d != tag count %d' % (n, len(self.tags)))
        fo, fs = h['file_table_offset'], h['file_table_size']
        idx = struct.unpack_from('<%dI' % n, mm, h['file_index_table_offset'])
        blob = mm[fo:fo + fs]
        for t in self.tags:
            o = idx[t.index]
            if o != 0xFFFFFFFF and o < fs:
                t.name = _cstr(blob, o, 'latin-1')
        self._names = True

    # ---- lookups ---------------------------------------------------------------------------------
    @property
    def valid_tags(self):
        return [t for t in self.tags if not t.is_null]

    def tag_by_datum(self, datum):
        if datum in (NULL_DATUM, None):
            return None
        i = datum & 0xFFFF
        if i < len(self.tags) and self.tags[i].datum == datum:
            return self.tags[i]
        return None

    def tag_by_name(self, group, name=None):
        """tag_by_name('bitm', 'levels\\...') or tag_by_name('levels\\...') (group optional).
        Name compare is case-insensitive.  Returns None when absent."""
        if name is None:
            group, name = None, group
        name_l = name.lower()
        if self._by_name_map is None:
            d = {}
            for t in self.tags:
                if t.name is not None and not t.is_null:
                    d.setdefault((t.group, t.name.lower()), t)
                    d.setdefault((None, t.name.lower()), t)
            self._by_name_map = d
        return self._by_name_map.get((group, name_l))

    _by_name_map = None   # lazily built; see tag_by_name

    def tags_in_group(self, group):
        return [t for t in self.tags if t.group == group]

    @property
    def scenario(self):
        return self.tag_by_datum(self.scenario_datum)

    @property
    def globals_tag(self):
        return self.tag_by_datum(self.globals_datum)

    @property
    def sound_gestalt(self):
        """The map's own sound gestalt (ugh!) tag: header[0x2EC] datum, else (H2EK-built self-contained maps
        store -1 there) the first ugh! tag that has data.  MP maps ALSO reference shared.map's gestalt
        (the last tag slot, index 23130) - see tags_in_group('ugh!')."""
        t = self.tag_by_datum(self.header['sound_gestalt_datum'])
        if t is None:
            t = next((x for x in self.tags if x.group == 'ugh!' and x.has_data), None)
        return t

    # ---- shared.map linkage ---------------------------------------------------------------------
    def open_shared(self, maps_dir=None):
        """Open the map that holds this map's external tags (shared.map for multiplayer maps) or None.
        Search order: maps_dir, the directory of this map, <work folder>/mcc_decompressed, the stock MCC folder."""
        if not any(t.external for t in self.tags):
            return None
        cands = [d for d in (maps_dir, os.path.dirname(self.source_path), DECOMP_DIR, _paths.mcc_maps_dir(required=False)) if d]
        for d in cands:
            p = os.path.join(d, 'shared.map')
            if os.path.isfile(p):
                return MccCache(p)
        return None

    def resolve_external(self, tag, shared):
        """For an external tag (addr == size == 0) return the same tag in *shared* (same index, verified by
        datum/group/name), else the tag itself."""
        if not tag.external:
            return tag
        st = shared.tags[tag.index] if tag.index < len(shared.tags) else None
        if st is None or st.datum != tag.datum or st.group != tag.group:
            raise MccFormatError('tag #%d %s not found at the same index in %s' % (tag.index, tag.group, shared.path))
        return st

    # ---- memory model ----------------------------------------------------------------------------
    @property
    def tag_data_start_offset(self):
        return self.meta_offset + self.header['tag_data_offset']

    def addr_to_offset(self, addr, tag=None):
        """Translate an in-tag memory address to an absolute file offset.
        file_offset = meta_offset + (addr - meta_offset_mask).  Raises SharedAddressError for addresses
        below the mask (shared-map data).  If *tag* is given the address must lie in that tag's own data."""
        if addr in (0, 0xFFFFFFFF):
            raise MccFormatError('null pointer')
        if addr < self.mask:
            raise SharedAddressError('address 0x%X < mask 0x%X (shared map region)' % (addr, self.mask))
        rel = addr - self.mask
        if rel >= self.meta_size:
            raise MccFormatError('address 0x%X beyond meta end 0x%X' % (addr, self.mask + self.meta_size))
        if tag is not None and not (tag.addr <= addr < tag.addr + tag.size):
            raise MccFormatError('address 0x%X outside tag #%d [0x%X,0x%X)' % (addr, tag.index, tag.addr, tag.addr + tag.size))
        return self.meta_offset + rel

    def owner_of(self, addr):
        """The tag whose [addr, addr+size) range contains *addr* (None if none / shared region).
        NOTE: sub-blocks are de-duplicated across tags in v13 (and Vista) caches - a tag_block/data_reference
        pointer may legitimately point into an EARLIER tag's data."""
        if self._own_sorted is None:
            self._own_sorted = sorted((t for t in self.tags if t.has_data), key=lambda t: t.addr)
            self._own_starts = [t.addr for t in self._own_sorted]
        import bisect
        i = bisect.bisect_right(self._own_starts, addr) - 1
        if i >= 0:
            t = self._own_sorted[i]
            if t.addr <= addr < t.addr + t.size:
                return t
        return None

    _own_sorted = None
    _own_starts = None

    def offset_to_addr(self, off):
        rel = off - self.meta_offset
        if not (0 <= rel < self.meta_size):
            raise MccFormatError('file offset 0x%X outside meta' % off)
        return self.mask + rel

    # ---- reading ---------------------------------------------------------------------------------
    @property
    def mm(self):
        """The read-only mmap of the (decompressed) file, for zero-copy slicing / struct.unpack_from."""
        return self._mm

    def raw(self, offset, size):
        """Raw bytes at an absolute file offset."""
        if offset < 0 or offset + size > self.file_size:
            raise MccFormatError('raw read 0x%X+0x%X outside file (0x%X)' % (offset, size, self.file_size))
        return bytes(self._mm[offset:offset + size])

    def read_tag(self, tag):
        """The tag's whole data block (main struct + all child blocks, contiguous).  b'' for tags whose
        data lives in a shared map."""
        if isinstance(tag, int):
            tag = self.tags[tag & 0xFFFF] if tag > 0xFFFF else self.tags[tag]
        if not tag.has_data:
            return b''
        return bytes(self._mm[tag.file_offset: tag.file_offset + tag.size])

    def read_at_addr(self, addr, size, tag=None):
        return self.raw(self.addr_to_offset(addr, tag), size)

    @property
    def raw_table(self):
        """(absolute offset, size) of the raw-resource area (header 0x2F0/0x2F4)."""
        return self.header['raw_table_offset'], self.header['raw_table_size']

    # ---- tag blocks ------------------------------------------------------------------------------
    def read_block(self, tag_data_bytes_or_tag, field_offset, elem_size):
        """Convenience: read the tag_block at *field_offset* of a tag -> list of element bytes."""
        t = tag_data_bytes_or_tag
        data = self.read_tag(t) if isinstance(t, (MccTag, int)) else t
        count, addr = struct.unpack_from('<iI', data, field_offset)
        if count <= 0 or addr == 0:
            return []
        off = self.addr_to_offset(addr)
        blob = self.raw(off, count * elem_size)
        return [blob[i * elem_size:(i + 1) * elem_size] for i in range(count)]

    # ---- structure bsps --------------------------------------------------------------------------
    def structure_bsps(self):
        """scenario.structure_bsps (block @0x210, element 0x44).  In v13 the offset/size/address triple is
        0/0/0 and sbsp/ltmp are ordinary tags."""
        scn = self.scenario
        if scn is None or not scn.has_data:
            return []
        out = []
        for i, e in enumerate(self.read_block(scn, 0x210, 0x44)):
            off, size, addr = struct.unpack_from('<III', e, 0)
            sg, sd, lg, ld = struct.unpack_from('<IIII', e, 0x10)
            out.append({'index': i, 'bsp_offset': off, 'bsp_size': size, 'bsp_address': addr,
                        'sbsp_datum': sd, 'ltmp_datum': ld,
                        'sbsp': self.tag_by_datum(sd), 'ltmp': self.tag_by_datum(ld),
                        'flags': struct.unpack_from('<H', e, 0x3C)[0],
                        'default_sky': struct.unpack_from('<h', e, 0x40)[0]})
        return out

    # ---- string ids ------------------------------------------------------------------------------
    def _load_strings(self):
        h, mm = self.header, self._mm
        n = h['string_table_count']
        idx = struct.unpack_from('<%dI' % n, mm, h['string_index_table_offset'])
        blob = bytes(mm[h['string_table_offset']: h['string_table_offset'] + h['string_table_size']])
        self._string_ids = [(_cstr(blob, o) if o < len(blob) else None) for o in idx]

    @property
    def string_ids(self):
        """list of strings; list index == string_id & 0xFFFFFF."""
        if self._string_ids is None:
            self._load_strings()
        return self._string_ids

    def string_id(self, value):
        """string_id value (u32) -> string.  Encoding: (len(string) << 24) | index  (verified, see docs).
        0 / 0xFFFFFFFF -> ''."""
        if value in (0, NULL_DATUM):
            return ''
        sids = self.string_ids
        i = value & 0xFFFFFF
        if i >= len(sids):
            raise MccFormatError('string_id 0x%08X index %d >= %d' % (value, i, len(sids)))
        s = sids[i]
        if (value >> 24) != len(s.encode('utf-8')):
            raise MccFormatError('string_id 0x%08X length byte %d != len(%r)' % (value, value >> 24, s))
        return s

    def string_id_value(self, text):
        """string -> string_id value (None if the string is not in the table)."""
        if self._sid_lookup is None:
            d = {}
            for i, s in enumerate(self.string_ids):
                if s is not None:
                    d.setdefault(s, (len(s.encode('utf-8')) << 24) | i)
            self._sid_lookup = d
        return self._sid_lookup.get(text)

    # ---- locale ----------------------------------------------------------------------------------
    def locale_globals(self):
        """Header locale block (header 0x2E4/0x2E8): [8 bytes zero][9 x language entry (0x1C each; the 9th is
        truncated to 0x14 bytes)].  Entry = {count, table_size, index_table_offset(abs), data_offset(abs), ...}.
        [] when the map has none (SP maps, H2EK-built maps)."""
        off, size = self.header['locale_globals_offset'], self.header['locale_globals_size']
        if off == 0xFFFFFFFF or size == 0:
            return []
        out = []
        for i in range(9):
            o = off + 8 + 0x1C * i
            if o + 16 > off + size:
                break
            cnt, tsz, ito, dto = struct.unpack_from('<IIII', self._mm, o)
            out.append({'language': LANGUAGE_NAMES[i], 'string_count': cnt, 'table_size': tsz,
                        'index_table_offset': ito, 'data_offset': dto})
        return out

    def locale_strings(self, entry):
        """Decode one language entry (from locale_globals() or the matg 'languages' array at +0x190) ->
        list of (string_id_value, text).  Bit 31 of the two offsets is a flag set in shared.map's matg
        (0x8157C980 = flag | file offset 0x157C980); it is masked off here."""
        cnt, tsz = entry['string_count'], entry['table_size']
        if cnt == 0:
            return []
        ito = entry['index_table_offset'] & 0x7FFFFFFF
        dto = entry['data_offset'] & 0x7FFFFFFF
        idx = list(struct.iter_unpack('<II', self.raw(ito, cnt * 8)))
        blob = self.raw(dto, tsz)
        return [(sid, _cstr(blob, o)) for sid, o in idx]

    def matg_languages(self):
        """The 9 language entries stored inside the matg tag at +0x190 (empty list when matg is external)."""
        g = self.globals_tag
        if g is None or not g.has_data:
            return []
        d = self.read_tag(g)
        out = []
        for i in range(9):
            cnt, tsz, ito, dto = struct.unpack_from('<IIII', d, 0x190 + 0x1C * i)
            out.append({'language': LANGUAGE_NAMES[i], 'string_count': cnt, 'table_size': tsz,
                        'index_table_offset': ito, 'data_offset': dto})
        return out

    # ---- dumps -----------------------------------------------------------------------------------
    def tag_table(self, include_null=False):
        return [t.to_dict() for t in self.tags if include_null or not t.is_null]

    def dump_tags_json(self, path, include_null=False):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.tag_table(include_null), f, indent=0)

    def summary(self):
        h = self.header
        valid = [t for t in self.tags if not t.is_null]
        return {
            'path': self.path, 'source': self.source_path, 'was_compressed': self.was_compressed,
            'internal_name': h['internal_name'], 'scenario_name': h['scenario_name'],
            'type': h['scenario_type_name'], 'file_size': self.file_size,
            'mask': hex(self.mask), 'meta_offset': hex(self.meta_offset), 'meta_size': hex(self.meta_size),
            'tag_slots': len(self.tags), 'valid_tags': len(valid),
            'with_data': sum(1 for t in valid if t.has_data), 'external': sum(1 for t in valid if t.external),
            'empty': sum(1 for t in valid if t.empty),
            'groups': len(self.groups), 'string_ids': h['string_table_count'],
        }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print('usage: py -3 -m h2conv.mcc_cache <map> [--tags-json out.json] | decompress <src> <dst>')
        return 2
    if argv[0] == 'decompress':
        print(decompress_map(argv[1], argv[2]))
        return 0
    c = MccCache(argv[0])
    print(json.dumps(c.summary(), indent=1))
    if '--tags-json' in argv:
        c.dump_tags_json(argv[argv.index('--tags-json') + 1])
    return 0


if __name__ == '__main__':
    sys.exit(main())
