# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Prototype bitmap/shader helpers: minimal tag-index reader for MCC (v13) and Vista (v8) H2 cache files.

Pure python + stdlib.  READ-ONLY on all maps.

Address model (verified):
    meta_off  = header 0x10 (both versions)  -> 0x20-byte meta header at that file offset
    mask      = MCC header 0x2D0 / Vista header 0x20  (tag-data virtual base)
    file_off(addr) = meta_off + (addr - mask)
    meta header: +0 group-table off, +4 group count, +8 tag-table off (both relative to meta_off),
                 +0xC scnr datum, +0x10 matg datum, +0x18 tag count, +0x1C 'tags'
    tag entry (16 B): group fourcc (reversed on disk), datum, addr, size
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass
import mmap
import struct
import os

from . import paths as _paths


def __getattr__(name):
    """MAPS / VISTA_DIR / MCC_DIR: the sample maps and the stock folders of the developer tools and tests, resolved by h2conv.paths when somebody asks for them
    (--vista-maps / H2CONV_VISTA_MAPS / settings ...).  The converter does not use them."""
    if name == 'MAPS':
        return _paths.sample_maps()
    if name == 'VISTA_DIR':
        return _paths.vista_maps_dir()
    if name == 'MCC_DIR':
        return _paths.mcc_maps_dir()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


class H2Map:
    """Minimal read-only map reader.  kind = 'mcc' (v13, header 0x380) or 'vista' (v8, header 0x800)."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        self.m = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        m = self.m
        self.version = struct.unpack_from('<I', m, 4)[0]
        self.file_size = struct.unpack_from('<I', m, 8)[0]
        if self.version == 13:
            self.kind = 'mcc'
            self.meta_off, self.meta_size = struct.unpack_from('<II', m, 0x10)
            self.mask = struct.unpack_from('<I', m, 0x2D0)[0]
            self.tag_data_off, self.tag_data_size = struct.unpack_from('<II', m, 0x2D4)
            self.file_cnt, self.file_off, self.file_tbl_size, self.file_idx_off = struct.unpack_from('<IIII', m, 0x20)
            self.raw_off, self.raw_size = struct.unpack_from('<II', m, 0x2F0)
        elif self.version == 8:
            self.kind = 'vista'
            self.meta_off = struct.unpack_from('<I', m, 0x10)[0]
            self.tag_data_off, self.tag_data_size, self.meta_size, self.mask = struct.unpack_from('<IIII', m, 0x14)
            self.file_cnt, self.file_off, self.file_tbl_size, self.file_idx_off = struct.unpack_from('<IIII', m, 0x2CC)
            self.raw_off, self.raw_size = struct.unpack_from('<II', m, 0x2E8)
        else:
            raise ValueError('unknown version %d' % self.version)
        self._parse_index()

    def close(self):
        self.m.close()
        self.f.close()

    def a2o(self, addr):
        """tag-data virtual address -> file offset"""
        return self.meta_off + (addr - self.mask)

    def _parse_index(self):
        m = self.m
        (self.grp_off, self.grp_cnt, self.tag_tbl_off, self.scnr_datum, self.matg_datum,
         _unk, self.tag_cnt, _sig) = struct.unpack_from('<iiiIIiii', m, self.meta_off)
        tt = self.meta_off + self.tag_tbl_off
        self.tags = []
        for i in range(self.tag_cnt):
            g, d, a, s = struct.unpack_from('<4sIII', m, tt + i * 16)
            grp = None if g == b'\xff\xff\xff\xff' else g[::-1].decode('latin1')
            self.tags.append({'group': grp, 'datum': d, 'addr': a, 'size': s, 'idx': i})
        self._names()
        self.by_datum = {t['datum']: t for t in self.tags}
        self.by_name = {}
        for t in self.tags:
            if t['name'] is not None and t['group'] is not None:
                self.by_name[(t['name'], t['group'])] = t

    def _names(self):
        m = self.m
        for i, t in enumerate(self.tags):
            t['name'] = None
            if self.file_cnt and i < self.file_cnt:
                o = struct.unpack_from('<i', m, self.file_idx_off + 4 * i)[0]
                if o >= 0:
                    e = m.find(b'\0', self.file_off + o)
                    t['name'] = m[self.file_off + o:e].decode('latin1')

    def strings(self):
        """string_id table -> list of str (list index == string_id & 0xFFFFFF; id>>24 == byte length)."""
        if getattr(self, '_strs', None) is None:
            m = self.m
            if self.kind == 'mcc':
                n, off, size, ioff = struct.unpack_from('<iIiI', m, 0x30)
            else:
                n = struct.unpack_from('<i', m, 0x170)[0]
                size = struct.unpack_from('<i', m, 0x174)[0]
                ioff, off = struct.unpack_from('<II', m, 0x178)
            idx = struct.unpack_from('<%dI' % n, m, ioff) if n > 0 else ()
            blob = bytes(m[off:off + size])
            out = []
            for o in idx:
                if o < len(blob):
                    e = blob.find(b'\0', o)
                    out.append(blob[o:e].decode('latin1'))
                else:
                    out.append(None)
            self._strs = out
        return self._strs

    def sid(self, v):
        if v in (0, 0xFFFFFFFF):
            return ''
        i = v & 0xFFFFFF
        st = self.strings()
        return st[i] if i < len(st) else '<sid %08X>' % v

    def is_local(self, t):
        return t['addr'] not in (0, 0xFFFFFFFF) and t['size'] != 0

    def tag_bytes(self, t):
        if not self.is_local(t):
            return b''
        o = self.a2o(t['addr'])
        return bytes(self.m[o:o + t['size']])

    def tag_off(self, t):
        return self.a2o(t['addr'])

    def find(self, group=None, name_sub=None):
        for t in self.tags:
            if group and t['group'] != group:
                continue
            if name_sub and (t['name'] is None or name_sub not in t['name']):
                continue
            yield t


def open_map(key_or_path):
    return H2Map(_paths.sample_maps().get(key_or_path, key_or_path))


# ======================================================================================================
#  BITMAP CONVERSION  (MCC 'bitm' -> Vista 'bitm')
# ======================================================================================================
import zlib

MCC_BITMAP_ELEM = 0xA8
VISTA_BITMAP_ELEM = 0x74
GROUP_HDR = 0x50
SECTOR = 0x200            # Vista reads (size rounded up to 0x200) bytes at every resource offset (sub_4647DA)

# e_bitmap_data_format -> bits per pixel (engine table word_7E11D4, same as Blam\Engine\bitmaps\bitmaps.cpp k_bits_per_pixel)
BITS_PER_PIXEL = {0: 8, 1: 8, 2: 8, 3: 16, 6: 16, 8: 16, 9: 16, 10: 32, 11: 32, 14: 4, 15: 8, 16: 8,
                  17: 8, 18: 8, 19: 128, 20: 96, 21: 48, 22: 16, 23: 16}
FORMAT_NAMES = {0: 'A8', 1: 'Y8', 2: 'AY8', 3: 'A8Y8', 6: 'R5G6B5', 8: 'A1R5G5B5', 9: 'A4R4G4B4', 10: 'X8R8G8B8',
                11: 'A8R8G8B8', 14: 'DXT1', 15: 'DXT3', 16: 'DXT5', 17: 'P8-Bump', 18: 'P8', 19: 'ARGBFP32',
                20: 'RGBFP32', 21: 'RGBFP16', 22: 'V8U8', 23: 'G8B8'}


def bitmap_raw_size(width, height, depth, fmt, mip_count, bitmap_type):
    """Decompressed byte size of a bitmap resource == what Vista's sub_67A3D4/sub_674EFA compute and what
    uncompress() must produce.  mip_count = bitmap_data.mipmap_count (extra levels).  Cube maps (type 2): x6.
    DXT formats round width/height up to a multiple of 4 *per level*; depth halves per level (3D)."""
    total = 0
    for lvl in range(mip_count + 1):
        w = max(1, width >> lvl)
        h = max(1, height >> lvl)
        d = max(1, depth >> lvl)
        if fmt in (14, 15, 16):
            w += -w & 3
            h += -h & 3
        total += w * h * d * BITS_PER_PIXEL[fmt] // 8
    return total * (6 if bitmap_type == 2 else 1)


def mcc_read_resource(buf, offset):
    """Read one MCC bitmap pixel resource (textures.dat, or the custom map itself).
    Layout: u32 chunk_count; i32 chunk_size[count]; chunk data...  size>0 = zlib stream (RFC1950, header 0x28 0x15
    in the files seen), size<0 = stored raw (-size bytes).  Returns (decompressed bytes, bytes consumed)."""
    n = struct.unpack_from('<I', buf, offset)[0]
    sizes = struct.unpack_from('<%di' % n, buf, offset + 4)
    p = offset + 4 + 4 * n
    parts = []
    for s in sizes:
        if s < 0:
            parts.append(bytes(buf[p:p - s]))
            p += -s
        else:
            d = zlib.decompressobj()
            parts.append(d.decompress(bytes(buf[p:p + s])))
            if not d.eof:
                raise ValueError('truncated zlib chunk')
            p += s
    return b''.join(parts), p - offset


def vista_pack_pixels(raw):
    """Vista resource = ONE zlib stream (level 9, header 78 DA) of the whole mip chain / all cube faces."""
    return zlib.compress(raw, 9)


def vista_read_pixels(map_buf, offset_field, size, shared_bufs=None):
    """Read+decompress a Vista pixel resource.  offset_field top 2 bits = 0 this map, 1 mainmenu.map, 2 shared.map,
    3 single_player_shared.map (engine sub_430C8B/sub_430CC4); low 30 bits = byte offset in that file."""
    which = offset_field >> 30
    buf = map_buf if which == 0 else shared_bufs[which]
    off = offset_field & 0x3FFFFFFF
    return zlib.decompress(bytes(buf[off:off + size]))


def pad_sector(blob):
    return blob + b'\0' * (-len(blob) % SECTOR)


def convert_bitmap_group_header(h):
    """MCC bitm root (0x50 bytes) -> Vista root (0x50 bytes).  Block count/pointer fields (+0x3C, +0x44) are returned
    UNCHANGED - the tag writer must patch the pointers.  Rules (verified 174/174 against stock Vista headlong):
      +0x06 flags |= 0x20   (Vista always sets 'WDP compression'; MCC clears it for usage==height-map groups)
      +0x4C..0x4F = 0F 0F 01 03 (Vista always has color/alpha quality 15, overlap 1, subsampling 3; MCC has 0 for 52 groups)
    everything else is byte-identical (usage, plate size, color-plate data_reference size, sprite budget...)"""
    h = bytearray(h[:GROUP_HDR])
    fl = struct.unpack_from('<H', h, 6)[0] | 0x20
    struct.pack_into('<H', h, 6, fl)
    h[0x4C:0x50] = bytes((0x0F, 0x0F, 0x01, 0x03))
    return bytes(h)


def convert_bitmap_element(e, parent_datum, lod1_offset, lod1_size, *, cache_usage=None, lod_adjust=None):
    """MCC bitmap_data (0xA8) -> Vista bitmap_data (0x74).
    e              MCC element bytes
    parent_datum   datum of the *output* bitm tag (Vista +0x4C 'parent_bitmap_tag'); MCC +0x1C holds the old datum
    lod1_offset    new value for Vista +0x1C (top 2 bits 0 = this map; offset must be 0x200-aligned)
    lod1_size      length in bytes of the zlib blob written at that offset
    cache_usage    override for +0x17 (needed for MCC bump maps: MCC writes 0)
    lod_adjust     override for +0x16
    Field map (MCC -> Vista):  +0x00..+0x17 same (see tweaks) | +0x18 pixels_offset same |
      MCC +0x1C datum, +0x20 native-mipmap block, +0x28 native size, +0x2C tile mode, +0x30 ptr -> dropped
      MCC +0x38/3C/40 lod1/2/3 offset -> Vista +0x1C/20/24 (lod2/3 = 0) | MCC +0x44..+0x4C (FFFFFFFF x3) -> Vista +0x28..+0x30
      MCC +0x50/54/58 lod sizes -> Vista +0x34/38/3C | Vista +0x40..+0x48 = 0 | Vista +0x4C = parent datum
      Vista +0x50..+0x70 (runtime pointers, low-detail fields) = 0."""
    assert len(e) >= MCC_BITMAP_ELEM
    v = bytearray(VISTA_BITMAP_ELEM)
    v[0:0x1C] = e[0:0x1C]                                    # sig,w,h,depth,flags8,type,format,flags,reg,mips,lod_adj,cache_usage,pixels_offset
    more = v[9] & 0x03                                       # keep 'delete from cache' / 'create attempted'; drop MCC 0x04/0x08
    v[9] = more
    fl = struct.unpack_from('<H', e, 0xE)[0] | 0x2000        # Vista tool sets bit 13 on 97% of bitmaps (MCC never does); not required by the engine
    if fl & 0x10:                                            # 'linear' (interface bitmaps): Vista always adds 0x80 'prefer low detail'
        fl |= 0x80                                           #   (9500+1306 linear bitmaps in 43 Vista maps, 0 exceptions)
    struct.pack_into('<H', v, 0xE, fl)
    if lod_adjust is not None:
        v[0x16] = lod_adjust & 0xFF
    if cache_usage is not None:
        v[0x17] = cache_usage & 0xFF
    struct.pack_into('<I', v, 0x1C, lod1_offset)             # lod1 offset (top bits 00 => this map)
    struct.pack_into('<III', v, 0x28, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
    struct.pack_into('<I', v, 0x34, lod1_size)
    struct.pack_into('<I', v, 0x4C, parent_datum)
    return bytes(v)


def guess_bump_cache_usage(own_cache_usage, sibling_cache_usage=None):
    """MCC leaves cache_usage=0 and lod_adjust=0 on height-map (usage 2) bitmaps that it re-encodes to A8R8G8B8.
    Vista values seen: 2 (environment bump) 36x+, 1 (environment) 2x, 5 (scenery bump) 3x.
    If the sibling diffuse bitmap's cache_usage is a non-bump value n in {1,4,6,8,10,12} use n+1, else 2."""
    if own_cache_usage:
        return own_cache_usage
    if sibling_cache_usage in (1, 4, 6, 8, 10, 12):
        return sibling_cache_usage + 1
    return 2


def convert_bitmap_group(src_map, tag, pixel_store, parent_datum_for_output, alloc):
    """Convert one MCC bitm tag.  Returns dict(header=bytes(0x50), elements=[bytes(0x74)...], blobs=[zlib bytes, one per element],
    sequences=[{'data': 0x34 bytes, 'sprite_count': n, 'sprites': n*0x20 bytes}]).  `alloc(blob_len)->offset` supplies the
    (0x200-aligned) file offset for each pixel blob (the writer must store pad_sector(blob) there); `pixel_store` is the buffer
    holding MCC resources (textures.dat mmap, or src_map.m for H2EK maps); `parent_datum_for_output` = datum of the output tag.
    header[0x3C/0x40/0x44/0x48] (block counts+pointers) are returned as in MCC and must be patched by the writer."""
    b = src_map.tag_bytes(tag)
    hdr = convert_bitmap_group_header(b)
    bc, ba = struct.unpack_from('<iI', b, 0x44)
    base = src_map.a2o(ba) - src_map.a2o(tag['addr'])
    elems, blobs = [], []
    for i in range(bc):
        e = b[base + i * MCC_BITMAP_ELEM: base + (i + 1) * MCC_BITMAP_ELEM]
        w, h, d = struct.unpack_from('<hhB', e, 4)
        typ = struct.unpack_from('<h', e, 0xA)[0]
        fmt = struct.unpack_from('<H', e, 0xC)[0]
        mips = struct.unpack_from('<h', e, 0x14)[0]
        l1 = struct.unpack_from('<I', e, 0x38)[0]
        s1 = struct.unpack_from('<I', e, 0x50)[0]
        raw, used = mcc_read_resource(pixel_store, l1)
        if used != s1:
            raise ValueError('%s[%d]: resource length %d != lod1_size %d' % (tag['name'], i, used, s1))
        if len(raw) != bitmap_raw_size(w, h, d, fmt, mips, typ):
            raise ValueError('%s[%d]: decoded %d bytes, engine expects %d' % (
                tag['name'], i, len(raw), bitmap_raw_size(w, h, d, fmt, mips, typ)))
        blob = vista_pack_pixels(raw)
        off = alloc(len(blob))
        elems.append(convert_bitmap_element(e, parent_datum_for_output, off, len(blob)))
        blobs.append(blob)
    # sequences (0x3C each; first 0x34 bytes are plain data, +0x34 = sprites block {count, addr}) and sprites (0x20 each) have the
    # IDENTICAL layout in both games -> returned raw; the writer must re-point the sprites block of every sequence.
    seqs = []
    sc, sa = struct.unpack_from('<iI', b, 0x3C)
    if sc > 0:
        so = src_map.a2o(sa)
        for i in range(sc):
            se = bytes(src_map.m[so + i * 0x3C: so + (i + 1) * 0x3C])
            spc, spa = struct.unpack_from('<iI', se, 0x34)
            sp = bytes(src_map.m[src_map.a2o(spa): src_map.a2o(spa) + spc * 0x20]) if spc > 0 else b''
            seqs.append({'data': se[:0x34], 'sprite_count': spc, 'sprites': sp})
    return {'header': hdr, 'elements': elems, 'blobs': blobs, 'sequences': seqs}


# ======================================================================================================
#  SHADER FAMILY / DONOR LOOKUP
# ======================================================================================================
import glob, math

SHADER_GROUPS = ('shad', 'stem', 'spas', 'vrtx')          # 'pixl' exists in the group table but NO map has a pixl tag
DONOR_ORDER = ('shared.map', 'single_player_shared.map', 'mainmenu.map')    # prefer the shared maps, then any other Vista map


class VistaDonors:
    """Index of LOCAL (data-carrying) tags of every map in a Vista maps folder, keyed (name.lower(), group).
    Tags that a map takes from a shared map have addr 0/size 0 and are NOT indexed (see H2Map.is_local)."""

    def __init__(self, folder=None, groups=SHADER_GROUPS):
        folder = folder or _paths.vista_maps_dir()
        self.maps = {}
        self.idx = {}
        files = [os.path.join(folder, n) for n in DONOR_ORDER]
        files += [p for p in sorted(glob.glob(os.path.join(folder, '*.map'))) if os.path.basename(p) not in DONOR_ORDER]
        for p in files:
            if not os.path.exists(p):
                continue
            mp = H2Map(p)
            self.maps[p] = mp
            for t in mp.tags:
                if t['name'] and t['group'] in groups and mp.is_local(t):
                    self.idx.setdefault((t['name'].lower(), t['group']), (p, t))

    def find(self, name, group):
        """-> (H2Map, tag dict) or None"""
        hit = self.idx.get((name.lower(), group))
        return (self.maps[hit[0]], hit[1]) if hit else None


def log_bitmap_dimension(width, height):
    """shad Postprocess Definition[0]/Bitmaps[]/'Log Bitmap Dimension' = ln(max(w,h)) * ln(2)
    (fits 40,307 of 41,089 samples over all 43 Vista maps; the 782 exceptions are NOT
    "4x4 bitmaps" - other 4x4 bitmaps (default_detail, default_vector, ...) give ln(4)*ln(2) = 0.9609 - they are all references to
    the tool's null bitmap rasterizer\\default_2d, whose value is 0.0 in Vista AND in MCC: see bitmap_log_dimension()).
    MUST be recomputed whenever a shader (or a donor shader) is paired with a bitmap whose size differs."""
    return math.log(max(width, height)) * math.log(2)


def bitmap_log_dimension(name, width, height):
    """log_bitmap_dimension with the rasterizer\\default_2d quirk (0.0)."""
    if (name or '').lower() == 'rasterizer\\default_2d':
        return 0.0
    return log_bitmap_dimension(width, height)


def map_shader_lod_bias(v):
    """shad +0x3C 'Shader LOD Bias': MCC adds 0x07 'Lowest' that Vista lacks (Vista 0..6, 0x6 = Cinematic).
    Nearest Vista value: 4 ('1/4 Size').
    NOTE: value 7 does not occur in the custom map (it uses 0,2,3,4,5,6) nor in stock MCC headlong (0,2,5)."""
    return 4 if v == 7 else v
