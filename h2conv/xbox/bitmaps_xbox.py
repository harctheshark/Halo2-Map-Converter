# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Xbox (cache v8, build 02.09.27.09809) bitmap pixels -> Halo 2 Vista pixel layout and element fields.

PUBLIC API
    xbox_element_pixels(xc, el, palette=None) -> bytes     pixels of one Xbox bitmap_data element in the exact Vista layout
                                                            (len == bitmaps.bitmap_raw_size() of the Vista element values)
    vista_element_fields(el)                  -> dict      the Vista values of every field that differs from the Xbox element
    vista_pixel_offsets(bitm_node)            -> [int]     Vista pixels_offset (+0x18) of every element of a tag
    vista_element_bytes(el, parent_datum, pixel_ptr, pixel_size, pixels_offset) -> bytes (0x74)   complete Vista bitmap_data element
    LightmapPalettes(xc)                                    ltmp Section Palettes of the P8 lightmap pages (needed for format 18)
    convert_bitmap_tag(xc, bitm_node, bitm_datum, palettes) -> [(fields, pixels), ...] one entry per element
    The caller zlib-compresses `pixels` (bitmaps.vista_pack_pixels), stores them 0x200-aligned and writes pointer / size.

RULES (each was derived from / checked against the stock Vista maps, the oracle; numbers = distinct (tag name, element) pairs over
the 45 Phantom maps + the pack's mainmenu / shared)

 1. LOD1/2/3.  LOD1 (+0x1C ptr, +0x34 size) is the COMPLETE chain; LOD2 (+0x20 / +0x38) is the same chain starting at mip 1, LOD3
    (+0x24 / +0x3C) starting at mip 2 (823 of 832 LOD2 blobs of bulldog.map are byte-identical to LOD1[level 1 ...]; sizes: 83,680
    of 84,084 LOD1, 32,114 of 32,285 LOD2, 25,500 of 25,802 LOD3 equal pad128(chain)).  Only LOD1 is needed: Vista has ONE blob.
    Pointers carry the location bits (0 map, 1 mainmenu, 2 shared, 3 single_player_shared - 331 elements of the pack point into a
    single_player_shared.map that the pack does not ship; xc.raw() raises for them).
 2. Blob layout.  Levels top mip first, tightly packed; Xbox pads every FACE to a multiple of 128 bytes (non-cube: the blob end,
    cube: 6 equal faces, stride = size / 6).  Vista = no padding.
    CUBE MAPS: Xbox is face-major (face 0 all levels, face 1 all levels ...); Vista is LEVEL-major (level 0 of the 6 faces, then
    level 1 ...), and Vista faces 1 and 2 are the Xbox faces 2 and 1 (CUBE_FACE_MAP).  Evidence: cube_probe (every Vista block
    found in the Xbox blob), 82 / 84 DXT1 cubes, 7 / 7 X8R8G8B8, 3 / 3 Y8 cubes byte-identical to the oracle.
    3-D textures: depth halves per level, a level is one w*h*d block (4x4x4 X8R8G8B8, 32x32x32 A8Y8: identical to the oracle).
 3. Swizzle.  Flag 0x0008 (set on every non-DXT, non-linear Xbox bitmap) = Morton order PER MIP LEVEL: bit order x0 y0 z0 x1 y1 z1 ...
    while a dimension still has bits, the left-over bits of the longer dimensions follow linearly (XGSwizzleRect).  Texel size =
    BITS_PER_PIXEL/8.  Bit-exact vs the oracle for X8R8G8B8, A8R8G8B8, A8Y8, A8, Y8, V8U8, R5G6B5, A1R5G5B5, A4R4G4B4 (2-D, cube, 3-D).
 4. DXT1/3/5 are never swizzled (even where a damaged mod flag says so): 4x4 blocks in raster order, level = ceil(w/4)*ceil(h/4)
    blocks (min 1).  756 DXT5 + 623 DXT1 + 140 DXT3 + 51 DXT1-cube names identical to the oracle.
 5. Linear surfaces (flag 0x0010; non-power-of-two sizes and interface bitmaps, one level): row pitch rounded up to 64 bytes in
    Xbox, tight in Vista (A4R4G4B4 10x10 640 -> 200 bytes; A8R8G8B8 123x121 61,952 -> 59,532): 164 / 164 A8R8G8B8 + 8 / 8 A4R4G4B4.
 6. DXT mip count.  A FULL chain (mipmap_count == floor(log2(max(w, h)))) loses its two smallest levels in Vista: mips - 2
    (256x256 8 -> 6, 4x4 2 -> 0, 1024x8 10 -> 8).  1,959 / 1,959 full-chain DXT elements (2-D 1,875, cube 84) obey it; 275 partial
    chains keep their count; non-DXT formats never change.  Non-square Xbox chains do not even STORE the two cut levels.
    A few mod bitmaps declare more levels than their blob holds (64x64 DXT3 'mips 6', 4096 bytes): the count is clamped to what is stored.
 7. Flags +0x0E: Vista = (xbox & ~(0x0008 | 0x0020 | 0x1000)) | 0x2000, and also & ~0x0004 when P8 / P8-bump are converted.
    (2,235 / 2,235 DXT 0x203 -> 0x2203; 285 / 286 0x1209 -> 0x2201; 172 / 172 0x290 -> 0x2290; 0x223 -> 0x2203; 0x120d -> 0x2201; 0x1289 -> 0x2281 2 / 3.)
    28 of 6,290 compared stock elements lack the 0x2000 tool bit: not a function of anything we have.
 8. Formats the stock Vista maps never contain (none of 17, 18, 2, 23 occurs in 50,025 Vista elements):
      17 P8-Bump -> X8R8G8B8 (10): 8-bit index into the FIXED 256-entry table h2conv/data/p8_bump_lut.json (alpha 0xFF); registration
         point -> (width/2, height/2) (590 / 590); flags lose the palettized bit.  X8R8G8B8 is what Vista used for 90 % of its bump maps
         (A8R8G8B8 10 % - source plates with alpha; DXT1 in a few).  APPROXIMATE: Vista built its bumps from the full-precision source, the
         Xbox only kept the palette index: 49 % of the texels are exact, 70 % within +-2, 82 % within +-4, 90 % within +-8 per channel.
      18 P8 -> A8R8G8B8 (11).  P8 occurs ONLY as BSP lightmap pages (26 tags).  Each page is drawn with exactly ONE Section Palette of the
         ltmp Lightmap Group that references its bitm tag (Cluster / Instance Render Info {i16 bitmap index, i8 palette index}; no page
         uses two palettes in the 47 maps): pixel = palette[index] (ARGB dword, 256 per 0x400-byte Section Palette element).
         bulldog.map: 137 / 137 pages = 2,356,864 / 2,356,864 texels identical to the stock Vista headlong lightmap pages (1,003 names
         identical in all; the rest are the mods' own re-baked lightmaps).  Pages whose bitm tag no ltmp of the map refers to (1,698
         elements: leftover campaign / other-map lightmaps) get a grey palette (convert_bitmap_tag(missing_palette='gray')).
       2 AY8 -> A8Y8 (3): both bytes = the AY8 byte.  Y channel identical to the oracle, alpha 81 % (Vista re-imports it).
      23 G8B8 is kept (51 elements, 2 tags, none in the oracle).  Y8, A8Y8, A8, V8U8, R5G6B5, A1R5G5B5, A4R4G4B4 are kept as they are
         (identical to the oracle).
 9. Other element fields.  Unchanged: width, height, depth, more_flags, type, cache_usage (2,700 / 2,700), lod_adjust (a tool-time
    byte, ~50 % differ either way, FACTS [tfr]), +0x28..+0x30 (0xFFFFFFFF), +0x40..+0x48 (0).  Vista: signature bytes 'mtib' (all 50,025
    stock elements; some mod bitmaps carry 'bitm'), +0x20 / +0x24 / +0x38 / +0x3C (LOD2/3) 0, +0x4C = datum of the new bitm tag,
    +0x50..+0x73 = 0 (the Xbox puts the low-detail block there; ~6 % of the stock elements hold junk in +0x58 / +0x68 that the engine
    does not read).  vista_element_bytes() equals the stock element in 5,881 of 6,290 compared
    elements (ignoring ptr, size, datum, lod_adjust and +0x70; the other 409 differ only by that junk or the 0x2000 bit).
    pixels_offset +0x18 = running sum over the tag's elements of bitmap_raw_size(VISTA format, XBOX mip count): converted formats
    count with their new size (P8 / P8-bump x4) while DXT chains keep the two cut levels (no mismatch left in the 6,290 compared elements).
Not handled: damaged elements (mips field 29805, LOD1 shorter than level 0) raise XboxBitmapError.  Re-authored textures cannot match
(see the report: 1,013 names have other dimensions in Vista, DXT names differ by re-encoding).
"""
import json
import os
import struct

import numpy as np

from .. import paths as _paths
from ..bitmaps import BITS_PER_PIXEL, FORMAT_NAMES, bitmap_raw_size

NULL = 0xFFFFFFFF
FMT_A8, FMT_Y8, FMT_AY8, FMT_A8Y8 = 0, 1, 2, 3
FMT_X8R8G8B8, FMT_A8R8G8B8 = 10, 11
FMT_DXT1, FMT_DXT3, FMT_DXT5 = 14, 15, 16
FMT_P8_BUMP, FMT_P8, FMT_G8B8 = 17, 18, 23
DXT_FORMATS = (FMT_DXT1, FMT_DXT3, FMT_DXT5)

# bitmap_data flags (u16 @ +0x0E)
F_POW2, F_COMPRESSED, F_PALETTIZED, F_SWIZZLED, F_LINEAR = 0x1, 0x2, 0x4, 0x8, 0x10
F_XBOX_BIT5, F_XBOX_BIT12, F_VISTA_BIT13 = 0x20, 0x1000, 0x2000

LOD_ALIGN = 128            # every Xbox LOD (and every cube face inside it) is padded up to a multiple of 128 bytes
LINEAR_PITCH_ALIGN = 64    # linear surfaces: row pitch rounded up to 64 bytes
VISTA_ELEMENT_SIZE = 0x74
CUBE_FACE_MAP = (0, 2, 1, 3, 4, 5)   # Vista cube face i = Xbox cube face CUBE_FACE_MAP[i]  (faces 1 and 2 are swapped)


class XboxBitmapError(Exception):
    pass


# ------------------------------------------------------------------------------------------------------------------
#  Morton ("swizzle") addressing
# ------------------------------------------------------------------------------------------------------------------
def _deposit(vals, mask):
    """parallel bit deposit: spread the low bits of `vals` over the set bits of `mask`."""
    out = np.zeros(len(vals), dtype=np.int64)
    k = 0
    for b in range(32):
        if mask >> b & 1:
            out |= ((vals >> k) & 1) << b
            k += 1
    return out


_MORTON = {}


def morton_index(w, h, d):
    """int32 array of w*h*d entries: for the raster position i = (z*h + y)*w + x the position of that texel in the swizzled level."""
    key = (w, h, d)
    idx = _MORTON.get(key)
    if idx is None:
        mx = my = mz = 0
        bit = 1
        ww, hh, dd = w, h, d
        while ww > 1 or hh > 1 or dd > 1:
            if ww > 1:
                mx |= bit
                bit <<= 1
                ww >>= 1
            if hh > 1:
                my |= bit
                bit <<= 1
                hh >>= 1
            if dd > 1:
                mz |= bit
                bit <<= 1
                dd >>= 1
        x = _deposit(np.arange(w, dtype=np.int64), mx)
        y = _deposit(np.arange(h, dtype=np.int64), my)
        z = _deposit(np.arange(d, dtype=np.int64), mz)
        idx = (z[:, None, None] + y[None, :, None] + x[None, None, :]).reshape(-1).astype(np.int32)
        if len(_MORTON) > 24:
            _MORTON.clear()
        _MORTON[key] = idx
    return idx


def unswizzle(data, w, h, d, bytes_per_pixel):
    """Xbox swizzled level (w*h*d texels) -> raster order."""
    n = w * h * d
    a = np.frombuffer(data, dtype=np.uint8, count=n * bytes_per_pixel).reshape(n, bytes_per_pixel)
    return a[morton_index(w, h, d)].tobytes()


# ------------------------------------------------------------------------------------------------------------------
#  level geometry
# ------------------------------------------------------------------------------------------------------------------
def _is_dxt(fmt):
    return fmt in DXT_FORMATS


def _level_dims(width, height, depth, mips):
    return [(max(1, width >> l), max(1, height >> l), max(1, depth >> l)) for l in range(mips + 1)]


def _tight_level_bytes(w, h, d, fmt):
    """bytes of one level, no pitch (the Vista layout; DXT rounds w/h up to 4 per level, as bitmap_raw_size does)."""
    bpp = BITS_PER_PIXEL[fmt]
    if _is_dxt(fmt):
        w += -w & 3
        h += -h & 3
    return w * h * d * bpp // 8


def full_mip_count(width, height):
    """mipmap_count of a complete chain down to 1x1: floor(log2(max(w, h)))."""
    return max(width, height).bit_length() - 1


def vista_mip_count(width, height, bitmap_type, fmt, mips):
    """Vista mipmap_count: a full DXT chain loses its two smallest levels (rule 6)."""
    full = full_mip_count(width, height)
    if _is_dxt(fmt) and mips == full and full >= 2:
        return mips - 2
    return mips


def xbox_lod_layout(width, height, depth, bitmap_type, fmt, flags, mips):
    """-> (levels[(xbox_bytes, vista_bytes, w, h, d)], faces, face_stride) of the Xbox LOD1 blob for the declared Xbox `mips`."""
    levels = []
    linear = not _is_dxt(fmt) and not (flags & F_SWIZZLED)
    xbox_face = 0
    for (w, h, d) in _level_dims(width, height, depth, mips):
        vb = _tight_level_bytes(w, h, d, fmt)
        if linear:
            pitch = -(-(w * BITS_PER_PIXEL[fmt] // 8) // LINEAR_PITCH_ALIGN) * LINEAR_PITCH_ALIGN
            xb = pitch * h * d
        else:
            xb = vb
        levels.append((xb, vb, w, h, d))
        xbox_face += xb
    faces = 6 if bitmap_type == 2 else 1
    stride = -(-xbox_face // LOD_ALIGN) * LOD_ALIGN
    return levels, faces, stride


def decode_xbox_lod(blob, width, height, depth, bitmap_type, fmt, flags, mips, out_mips=None):
    """Xbox LOD1 blob -> raw Vista-layout pixels (raster order, no padding, top mip first, cube faces in file order),
    keeping the first `out_mips`+1 levels of every face (default: all `mips`+1)."""
    if fmt not in BITS_PER_PIXEL:
        raise XboxBitmapError('unsupported bitmap format %d' % fmt)
    if not 0 <= mips <= 16:
        raise XboxBitmapError('implausible mipmap count %d' % mips)
    out_mips = mips if out_mips is None else out_mips
    levels, faces, stride = xbox_lod_layout(width, height, depth, bitmap_type, fmt, flags, mips)
    keep = levels[:out_mips + 1]
    if faces == 6 and len(blob) % 6 == 0 and len(blob) // 6 >= sum(l[0] for l in keep):
        stride = len(blob) // 6              # LOD1 = 6 equal faces (stock: pad128(chain) each; unpadded when a foreign tool wrote it)
    need = stride * (faces - 1) + sum(l[0] for l in keep)
    if len(blob) < need:
        raise XboxBitmapError('LOD1 has %d bytes, %dx%dx%d %s type %d mips %d needs %d' % (
            len(blob), width, height, depth, FORMAT_NAMES.get(fmt, fmt), bitmap_type, mips, need))
    dxt = _is_dxt(fmt)
    swz = bool(flags & F_SWIZZLED) and not dxt
    bpp8 = BITS_PER_PIXEL[fmt] // 8
    offs, p = [], 0
    for lev in keep:
        offs.append(p)
        p += lev[0]
    out = []
    for li, (xb, vb, w, h, d) in enumerate(keep):            # Vista cube maps are LEVEL-major: level 0 of all 6 faces, then level 1 ...
        for f in range(faces):
            q = CUBE_FACE_MAP[f] * stride + offs[li] if faces == 6 else offs[li]
            lv = blob[q:q + xb]
            if swz:
                lv = unswizzle(lv, w, h, d, bpp8)
            elif not dxt:                                    # linear surface: drop the row pitch padding
                rowb = w * bpp8
                pitch = xb // (h * d)
                if pitch != rowb:
                    lv = b''.join(lv[r * pitch:r * pitch + rowb] for r in range(h * d))
            out.append(lv)
    return b''.join(out)


# ------------------------------------------------------------------------------------------------------------------
#  palettes / format conversions
# ------------------------------------------------------------------------------------------------------------------
P8_BUMP_LUT_FILE = 'p8_bump_lut.json'
_P8_BUMP_LUT = []


def p8_bump_lut():
    """numpy uint8 array (256, 4): P8-bump index -> B, G, R, A of the Vista pixel (h2conv/data/p8_bump_lut.json, 'bgra_hex')."""
    if not _P8_BUMP_LUT:
        try:
            with open(os.path.join(_paths.data_dir(), P8_BUMP_LUT_FILE), 'r', encoding='utf-8') as fh:
                raw = bytes.fromhex(json.load(fh)['bgra_hex'])
        except (OSError, ValueError, KeyError) as ex:
            # the bitmap then falls back like any bitmap that cannot convert (the converter replaces it by gray_50_percent)
            raise XboxBitmapError('the P8 bump table %s cannot be read (%s)' % (P8_BUMP_LUT_FILE, ex))
        _P8_BUMP_LUT.append(np.frombuffer(raw, np.uint8).reshape(256, 4).copy())
    return _P8_BUMP_LUT[0]


def p8_bump_to_x8r8g8b8(p8):
    """P8-bump index bytes -> 32-bit pixels through the fixed table (alpha 0xFF, bytes B G R A)."""
    return p8_bump_lut()[np.frombuffer(p8, np.uint8)].tobytes()


def ay8_to_a8y8(ay8):
    """AY8 (one byte, alpha == intensity) -> A8Y8 (two bytes)."""
    return np.repeat(np.frombuffer(ay8, np.uint8), 2).tobytes()


def p8_to_a8r8g8b8(p8, palette):
    """P8 indices + 256 ARGB dwords (1024 bytes, little-endian dwords == B G R A bytes) -> A8R8G8B8 bytes."""
    pal = np.frombuffer(bytes(palette), dtype='<u4', count=256)
    return pal[np.frombuffer(p8, np.uint8)].astype('<u4').tobytes()


class LightmapPalettes(object):
    """ltmp Lightmap Groups of an Xbox map: which Section Palette draws which P8 lightmap page.
    palette(bitm_datum, page) -> 1024 bytes or None;  .conflicts = [(datum, page, {palette indices})] for pages used with several."""

    def __init__(self, xc):
        from .. import tagtree as T
        from . import layouts as XL
        lay = XL.xbox_layouts()['ltmp']
        self._pal = {}
        self.conflicts = []
        for t in xc.find('ltmp'):
            if not t.addr:
                continue
            node = T.decode_tag(xc.read, lay, t.addr, 'ltmp')
            for g in node.blocks.get(0x80, []):
                datum = g.u32(0x1C)                        # Bitmap Group tagref (group, datum)
                pals = g.blocks.get(0x8, [])               # Section Palette, 0x400 bytes = 256 ARGB dwords each
                used = {}
                for blk in (0x28, 0x48):                   # Cluster / Instance Render Info {i16 bitmap index, i8 palette index}
                    for e in g.blocks.get(blk, []):
                        bi, pi = e.i16(0), e.data[2]
                        if bi >= 0:
                            used.setdefault(bi, set()).add(pi)
                for bi, ps in used.items():
                    p = min(ps)
                    if len(ps) > 1:
                        self.conflicts.append((datum, bi, set(ps)))
                    if p < len(pals):
                        self._pal[(datum, bi)] = bytes(pals[p].data)

    def palette(self, bitm_datum, page):
        return self._pal.get((bitm_datum, page))


# ------------------------------------------------------------------------------------------------------------------
#  public API
# ------------------------------------------------------------------------------------------------------------------
def element_params(el):
    """(width, height, depth, type, format, flags, mipmap_count) of an Xbox bitmap_data element Node (0x74 bytes)."""
    d = el.data
    return (el.i16(4), el.i16(6), d[8], el.u16(0xA), el.u16(0xC), el.u16(0xE), el.i16(0x14))


def vista_format(fmt):
    """Xbox pixel format -> format Vista stores (rule 8)."""
    return {FMT_P8_BUMP: FMT_X8R8G8B8, FMT_P8: FMT_A8R8G8B8, FMT_AY8: FMT_A8Y8}.get(fmt, fmt)


def vista_element_fields(el):
    """Vista values of the fields that differ from the Xbox element (keys: format, flags, mipmap_count, reg_x, reg_y);
    width / height / depth / type / more_flags / cache_usage / lod_adjust / pixels_offset are unchanged."""
    w, h, d, typ, fmt, flags, mips = element_params(el)
    nf = vista_format(fmt)
    nflags = (flags & ~(F_SWIZZLED | F_XBOX_BIT12 | F_XBOX_BIT5)) | F_VISTA_BIT13
    if nf != fmt and fmt in (FMT_P8, FMT_P8_BUMP):
        nflags &= ~F_PALETTIZED
    vm = vista_mip_count(w, h, typ, fmt, mips)
    lod_size = el.u32(0x34)
    if fmt in BITS_PER_PIXEL and 0 <= mips <= 16 and lod_size:
        # a few mod bitmaps declare more mip levels than their LOD1 blob holds (64x64 DXT3 'mips 6', 4096 bytes = level 0 only):
        # keep what is stored
        levels, faces, stride = xbox_lod_layout(w, h, d, typ, fmt, flags, mips)
        if faces == 6 and lod_size % 6 == 0:
            stride = lod_size // 6
        while vm > 0 and stride * (faces - 1) + sum(l[0] for l in levels[:vm + 1]) > lod_size:
            vm -= 1
    out = {'format': nf, 'flags': nflags, 'mipmap_count': vm}
    if fmt == FMT_P8_BUMP:
        out['reg_x'], out['reg_y'] = w // 2, h // 2           # converted bump maps get the centre as registration point (Xbox: 0)
    return out


def vista_pixel_offsets(bitm_node):
    """pixels_offset (+0x18) of every element of a bitm tag in Vista = running sum of bitmap_raw_size(VISTA format, XBOX mip count)
    over the earlier elements (the tool's offsets keep the two cut DXT levels but count converted formats with their new size:
    P8 lightmap pages and P8-bump x4).  The Xbox value is the same sum over the Xbox formats."""
    out, off = [], 0
    for el in bitm_node.blocks.get(0x44, []):
        w, h, d, typ, fmt, flags, mips = element_params(el)
        vf = vista_element_fields(el)
        out.append(off)
        off += bitmap_raw_size(w, h, d, vf['format'], mips if 0 <= mips <= 16 else 0, typ) if fmt in BITS_PER_PIXEL else 0      # DXT: the UNCUT Xbox chain (the tool's offsets keep the 2 cut levels)
    return out


def xbox_element_pixels(xc, el, palette=None):
    """Pixels of one Xbox bitmap_data element in the exact layout (and length) the Vista element expects.
    `palette` (1024 bytes, see LightmapPalettes.palette) is required for P8 (format 18)."""
    w, h, d, typ, fmt, flags, mips = element_params(el)
    ptr, size = el.u32(0x1C), el.u32(0x34)
    if ptr == NULL or not size:
        raise XboxBitmapError('bitmap element has no LOD1 data')
    vf = vista_element_fields(el)
    blob = xc.raw(ptr, size)
    px = decode_xbox_lod(blob, w, h, d, typ, fmt, flags, mips, out_mips=vf['mipmap_count'])
    if fmt == FMT_P8_BUMP:
        px = p8_bump_to_x8r8g8b8(px)
    elif fmt == FMT_P8:
        if palette is None:
            raise XboxBitmapError('P8 bitmap needs its ltmp Section Palette (pass palette=)')
        px = p8_to_a8r8g8b8(px, palette)
    elif fmt == FMT_AY8:
        px = ay8_to_a8y8(px)
    want = bitmap_raw_size(w, h, d, vf['format'], vf['mipmap_count'], typ)
    if len(px) != want:
        raise XboxBitmapError('converted %d bytes, Vista expects %d' % (len(px), want))
    return px


def vista_element_bytes(el, parent_datum, pixel_ptr, pixel_size, pixels_offset=None, fmt=None):
    """Complete Vista bitmap_data element (0x74 bytes).  pixel_ptr = +0x1C raw pointer (top 2 bits 0 = this map),
    pixel_size = byte length of the zlib blob, parent_datum = datum of the new bitm tag (+0x4C),
    pixels_offset = vista_pixel_offsets(bitm_node)[index] (default: the Xbox value, right only when no earlier element changed size),
    fmt = the format convert_bitmap_tag decided from the pixels (P8-bump with transparent texels -> A8R8G8B8)."""
    vf = vista_element_fields(el)
    if fmt is not None:
        vf['format'] = fmt
    v = bytearray(VISTA_ELEMENT_SIZE)
    v[0:0x1C] = bytes(el.data[0:0x1C])
    v[0:4] = b'mtib'                                          # all 50,025 stock Vista elements; a few mod bitmaps carry 'bitm'
    if pixels_offset is not None:
        struct.pack_into('<I', v, 0x18, pixels_offset)
    struct.pack_into('<H', v, 0xC, vf['format'])
    struct.pack_into('<H', v, 0xE, vf['flags'])
    struct.pack_into('<h', v, 0x14, vf['mipmap_count'])
    if 'reg_x' in vf:
        struct.pack_into('<hh', v, 0x10, vf['reg_x'], vf['reg_y'])
    struct.pack_into('<I', v, 0x1C, pixel_ptr)
    struct.pack_into('<III', v, 0x28, NULL, NULL, NULL)
    struct.pack_into('<I', v, 0x34, pixel_size)
    struct.pack_into('<I', v, 0x4C, parent_datum)
    return bytes(v)


def gray_palette():
    """identity palette (index -> opaque grey) for P8 pages no ltmp refers to (stale lightmap tags the map keeps but never draws)."""
    return b''.join(bytes((i, i, i, 255)) for i in range(256))


def convert_bitmap_tag(xc, bitm_node, bitm_datum, palettes=None, missing_palette='gray'):
    """All elements of one Xbox bitm tag -> [(vista_fields, pixels)]; vista_fields also carries 'pixels_offset' (vista_pixel_offsets).
    `palettes` = LightmapPalettes(xc) (needed only for P8 lightmap pages; built on demand when omitted).  `bitm_datum` = the tag's
    datum (the key the ltmp Bitmap Group reference uses).  A P8 page without palette (the bitmap tag is not referenced by any ltmp
    group of the map: 1,698 such elements in the pack, all leftover campaign lightmaps) becomes grey if missing_palette == 'gray',
    else XboxBitmapError.  The element's fields['palette_missing'] is then True."""
    out = []
    offs = vista_pixel_offsets(bitm_node)
    shift = 0                                                  # bytes later elements move by (an element decoded out of DXT, below)
    for i, el in enumerate(bitm_node.blocks.get(0x44, [])):
        pal, missing = None, False
        if el.u16(0xC) == FMT_P8:
            if palettes is None:
                palettes = LightmapPalettes(xc)
            pal = palettes.palette(bitm_datum, i)
            if pal is None and missing_palette == 'gray':
                pal, missing = gray_palette(), True
        f = vista_element_fields(el)
        f['pixels_offset'] = offs[i] + shift
        if missing:
            f['palette_missing'] = True
        px = xbox_element_pixels(xc, el, pal)
        if el.u16(0xC) == FMT_P8_BUMP and 0 in px[3::4]:
            # P8-bump index 255 = a TRANSPARENT texel (table alpha 0): alpha-tested bump maps (headlong crane girders girders_a_bump,
            # tex_bump_alpha_test*) need the alpha, so they become A8R8G8B8 (stock Vista girders_a_bump is A8R8G8B8: all 16,129
            # index-255 texels alpha 0, every other index 255).  X8R8G8B8 made the GPU ignore it: solid girders.  Same bytes / size.
            f['format'] = FMT_A8R8G8B8
        w, h, d, typ, fmt, flags, mips = element_params(el)
        if _is_dxt(f['format']) and (w % 4 or h % 4) and typ == 0 and d == 1:
            # D3D9 cannot create a DXT texture whose size is not a multiple of 4 (no stock Vista bitmap has one, 0 of 617): the map-load
            # bitmap pass then calls UpdateTexture on a NULL texture and crashes in d3d9.dll (california 104x19 DXT1, 2026-10-26).  The
            # Xbox draws it, so it is decoded to A8R8G8B8 - the same texels, no padding or stretch.
            px = dxt_to_argb(px, w, h, f['format'], f['mipmap_count'])
            old = bitmap_raw_size(w, h, d, f['format'], mips if 0 <= mips <= 16 else 0, typ)
            f['format'] = FMT_A8R8G8B8
            f['dxt_decoded'] = True
            shift += bitmap_raw_size(w, h, d, FMT_A8R8G8B8, mips if 0 <= mips <= 16 else 0, typ) - old
        out.append((f, px))
    return out


def dxt_to_argb(px, w, h, fmt, mips):
    """A 2D DXT1/3/5 mip chain (levels 0..mips, each rounded up to whole 4x4 blocks) -> the A8R8G8B8 chain of the same levels."""
    out, p = [], 0
    for lw, lh, _ in _level_dims(w, h, 1, mips):
        n = _tight_level_bytes(lw, lh, 1, fmt)
        out.append(_decode_dxt(fmt, lw, lh, bytes(px[p:p + n])))
        p += n
    return b''.join(out)


def _rgb565(v):
    r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
    return (r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2)


def _dxt_color_block(blk, opaque_only):
    c0, c1 = struct.unpack_from('<HH', blk, 0)
    p0, p1 = _rgb565(c0), _rgb565(c1)
    pal = [p0 + (255,), p1 + (255,)]
    if c0 > c1 or not opaque_only:                       # 4-colour mode (always for DXT3 / DXT5)
        pal.append(tuple((2 * a + b) // 3 for a, b in zip(p0, p1)) + (255,))
        pal.append(tuple((a + 2 * b) // 3 for a, b in zip(p0, p1)) + (255,))
    else:                                                # DXT1 3-colour mode + transparent black
        pal.append(tuple((a + b) // 2 for a, b in zip(p0, p1)) + (255,))
        pal.append((0, 0, 0, 0))
    idx = struct.unpack_from('<I', blk, 4)[0]
    return pal, idx


def _decode_dxt(fmt, w, h, data):
    """DXT1 (14) / DXT3 (15) / DXT5 (16) blocks -> BGRA bytes (w*h*4)."""
    out = bytearray(w * h * 4)
    bw, bh = (w + 3) // 4, (h + 3) // 4
    bsz = 8 if fmt == 14 else 16
    p = 0
    for by in range(bh):
        for bx in range(bw):
            blk = data[p:p + bsz]
            p += bsz
            if fmt == 14:
                pal, idx = _dxt_color_block(blk, True)
                alpha = None
            else:
                pal, idx = _dxt_color_block(blk[8:], False)
                if fmt == 15:                            # explicit 4-bit alpha
                    a64 = struct.unpack_from('<Q', blk, 0)[0]
                    alpha = [((a64 >> (4 * i)) & 15) * 17 for i in range(16)]
                else:                                    # interpolated alpha
                    a0, a1 = blk[0], blk[1]
                    bits = int.from_bytes(blk[2:8], 'little')
                    if a0 > a1:
                        tab = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
                    else:
                        tab = [a0, a1] + [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] + [0, 255]
                    alpha = [tab[(bits >> (3 * i)) & 7] for i in range(16)]
            for py in range(4):
                y = by * 4 + py
                if y >= h:
                    break
                for px in range(4):
                    x = bx * 4 + px
                    if x >= w:
                        break
                    k = py * 4 + px
                    r, g, b, a = pal[(idx >> (2 * k)) & 3]
                    if alpha is not None:
                        a = alpha[k]
                    o = (y * w + x) * 4
                    out[o:o + 4] = bytes((b, g, r, a))
    return bytes(out)
