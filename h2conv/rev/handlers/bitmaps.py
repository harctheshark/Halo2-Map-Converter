# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""bitm handlers: Halo 2 Vista bitmap group (v8) -> MCC bitmap group (v13).  The inverse of
h2conv/handlers/bitmaps.py.

Pixel data
----------
Vista keeps ONE plain zlib stream (78 DA, level 9) of the whole mip chain / all six cube faces per bitmap element, at an
offset whose top two bits select the cache file (0 this map, 1 mainmenu.map, 2 shared.map, 3 single_player_shared.map).
MCC stores a *chunked resource* (`u32 n; i32 size[n]; chunk data...`): the decompressed bytes are cut into pieces of 0x40000 bytes
(the last one is shorter), every piece is deflated with the MCC tool's exact parameters and stored raw (negative size) when
deflate would not make it smaller.  The parameters were recovered from the H2EK-built maps:

    zlib.compressobj(level 9, DEFLATED, wbits 10, memLevel 8, strategy Z_FIXED)        -> header bytes 28 15
    reproduces 2,518 / 2,518 chunks (1,948 / 1,948 blobs, byte for byte) of the custom headlong; the raw rule is `len(z) < len(raw)`.

The blob is written INSIDE the map (header flags 0x10, the target container style of the H2EK maps): the element's
LOD1 offset (+0x38) is `node.patches[0x38] = ctx.add_raw('bitmap', blob, 0x80)` (the driver writes the final file offset; the
H2EK tool aligns blobs to 0x80 and stores identical blobs once - done here too) and LOD1 size (+0x50) = len(blob).

Element 0x74 (Vista) -> 0xA8 (MCC), measured against 49,536 same-name element pairs of the 43 stock map pairs:
    +0x00..+0x17 copy;  more_flags &= 3;  flags16 = (v & ~0x2000) | 0x0200, minus 0x80 when 'linear' (0x10)   (18,489 / 18,489 equal)
    +0x18 pixels_offset copy;  +0x1C parent datum = datum of the OUTPUT bitm tag;  +0x20..+0x37 native mipmap info / sizes / tile mode /
    runtime pointer = 0;  +0x38 LOD1 offset (patch);  +0x3C/+0x40 = 0;  +0x44/+0x48/+0x4C = 0xFFFFFFFF;  +0x50 LOD1 size;  everything
    else (LOD2/3 sizes, runtime pointers, unknown dwords) = 0  (census over 9,392 MCC elements of 5 maps: every one of these is constant).
Formats MCC never stores (A8Y8 / R5G6B5 / A1R5G5B5 / A4R4G4B4: the MCC tool turns a "16-bit color" or "monochrome" group into
X8R8G8B8 / A8R8G8B8 elements, 0 of 25,769 MCC elements use them) are expanded to 32 bit (REENCODE below).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import hashlib
import struct
import zlib

from . import node_handler, pre_pass
from ... import bitmaps as B            # forward helpers: bitmap_raw_size, BITS_PER_PIXEL (direction independent)

MCC_ELEM = B.MCC_BITMAP_ELEM            # 0xA8
VISTA_ELEM = B.VISTA_BITMAP_ELEM        # 0x74
NULL = 0xFFFFFFFF
CHUNK = 0x40000                         # decompressed bytes per MCC chunk (all chunks but the last)
MCC_ALIGN_HINT = 0x80                   # the H2EK tool aligns bitmap blobs to 0x80
MCC_FORMATS = frozenset((0, 1, 10, 11, 14, 15, 16, 22, 23))     # element formats seen in 25,769 stock + H2EK MCC elements
DXT_FORMATS = (14, 15, 16)

# bitm root: offsets identical in both games
ROOT_FLAGS = 0x06
FLAG_OG_XBOX_MIPMAP = 0x2000            # MCC group flag bit 13 (never set by Vista tools)

# Vista element format -> MCC element format for formats the MCC tool never produces
REENCODE = {3: 11, 6: 10, 8: 11, 9: 11}

_LOC_FILE = {1: 'mainmenu', 2: 'shared', 3: 'single_player_shared'}


# ------------------------------------------------------------------------------------------------
# chunked MCC resource
# ------------------------------------------------------------------------------------------------
def deflate_fixed(raw):
    """zlib stream exactly as the MCC tool writes it: level 9, 1 KiB window (header 28 15), memLevel 8, fixed Huffman codes."""
    co = zlib.compressobj(9, zlib.DEFLATED, 10, 8, zlib.Z_FIXED)
    return co.compress(raw) + co.flush()


def mcc_pack_pixels(raw):
    """Decompressed pixel bytes -> MCC chunked resource blob (`u32 n; i32 size[n]; chunks`).  Empty input -> b''."""
    raw = bytes(raw)
    if not raw:
        return b''
    sizes = []
    parts = []
    for k in range(0, len(raw), CHUNK):
        piece = raw[k:k + CHUNK]
        z = deflate_fixed(piece)
        if len(z) < len(piece):
            sizes.append(len(z))
            parts.append(z)
        else:
            sizes.append(-len(piece))
            parts.append(piece)
    return struct.pack('<I', len(sizes)) + struct.pack('<%di' % len(sizes), *sizes) + b''.join(parts)


def mcc_unpack_pixels(blob):
    """MCC chunked resource blob -> decompressed bytes (inverse of mcc_pack_pixels)."""
    raw, used = B.mcc_read_resource(blob, 0)
    if used != len(blob):
        raise ValueError('chunked resource is %d bytes, header says %d' % (len(blob), used))
    return raw


def vista_unpack_pixels(blob):
    """Vista one-stream resource -> decompressed bytes.  Raises ValueError unless the stream is complete (size exact)."""
    d = zlib.decompressobj()
    raw = d.decompress(bytes(blob))
    if not d.eof:
        raise ValueError('truncated zlib stream')
    return raw


def salvage_pixels(blob):
    """Best effort for a corrupt / truncated Vista stream (1 of 63,391 elements of the 516 community maps scanned: pulau.map, 148,767 of
    699,048 bytes decode before 'invalid code lengths set'): the bytes that inflate before the error, b'' if none."""
    blob = bytes(blob)
    d = zlib.decompressobj()
    acc = bytearray()
    try:
        for k in range(0, len(blob), 64):
            acc += d.decompress(blob[k:k + 64])
            if d.eof:
                break
    except zlib.error:
        pass
    return bytes(acc)


# ------------------------------------------------------------------------------------------------
# pixel formats MCC does not store
# ------------------------------------------------------------------------------------------------
def reencode_pixels(fmt, raw):
    """Expand a whole mip chain of a legacy format to the 32-bit format REENCODE names (BGRA byte order = D3D X8R8G8B8 / A8R8G8B8).
    A8Y8 -> (Y, Y, Y, A) (470 / 628 stock pairs byte-equal, the rest is different art); 5-bit and 4-bit channels use bit replication."""
    import numpy as np
    if fmt == 3:
        a = np.frombuffer(raw, dtype='u1').reshape(-1, 2)
        y, al = a[:, 0], a[:, 1]
        return np.stack([y, y, y, al], axis=1).astype('u1').tobytes()
    v = np.frombuffer(raw, dtype='<u2').astype(np.uint32)
    if fmt == 6:
        r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
        out = np.stack([(b << 3) | (b >> 2), (g << 2) | (g >> 4), (r << 3) | (r >> 2), np.full_like(b, 255)], axis=1)
    elif fmt == 8:
        al, r, g, b = (v >> 15) & 1, (v >> 10) & 31, (v >> 5) & 31, v & 31
        out = np.stack([(b << 3) | (b >> 2), (g << 3) | (g >> 2), (r << 3) | (r >> 2), al * 255], axis=1)
    elif fmt == 9:
        al, r, g, b = (v >> 12) & 15, (v >> 8) & 15, (v >> 4) & 15, v & 15
        out = np.stack([b * 17, g * 17, r * 17, al * 17], axis=1)
    else:
        raise ValueError('format %d is not re-encoded' % fmt)
    return out.astype('u1').tobytes()


# ------------------------------------------------------------------------------------------------
# element conversion (pure)
# ------------------------------------------------------------------------------------------------
def mcc_flags(vflags, fmt_changed_to=None):
    """Vista element flags16 -> MCC: bit 13 (set by the Vista tool on 97 % of bitmaps) is cleared, 'always on' bit 9 is set, the
    Vista-only 0x80 that Vista adds to every linear bitmap is dropped.  A re-encoded format also drops the 'compressed' bit."""
    f = (vflags & ~0x2000) | 0x0200
    if f & 0x10:
        f &= ~0x80
    if fmt_changed_to is not None and fmt_changed_to not in DXT_FORMATS:
        f &= ~0x0002
    return f


def vista_to_mcc_element(v, parent_datum, fmt=None, lod1_size=0):
    """Vista bitmap_data (0x74) -> MCC bitmap_data (0xA8) as a bytearray.  `fmt` overrides the format word (re-encoded pixels);
    +0x38 (LOD1 offset) is left 0 for the caller (patch token) and +0x50 = lod1_size."""
    assert len(v) >= VISTA_ELEM
    m = bytearray(MCC_ELEM)
    m[0:0x18] = v[0:0x18]                                          # sig,w,h,depth,flags8,type,format,flags,reg,mips,lod_adjust,cache_usage
    m[9] = v[9] & 0x03                                              # MCC adds 'recreate allowed' / 'sampling allowed' (bits 2/3): tool-time, not derivable
    vf = struct.unpack_from('<H', v, 0xE)[0]
    if fmt is not None:
        struct.pack_into('<H', m, 0xC, fmt)
    struct.pack_into('<H', m, 0xE, mcc_flags(vf, fmt))
    m[0x18:0x1C] = v[0x18:0x1C]                                    # pixels_offset
    struct.pack_into('<I', m, 0x1C, parent_datum)
    struct.pack_into('<III', m, 0x44, NULL, NULL, NULL)
    struct.pack_into('<I', m, 0x50, lod1_size)
    return m


def fix_group_header(vista_data, mcc_data):
    """Root 0x50 bytes are identical in layout: copy everything before the first block, clear the MCC-only flag bit 13 (never set by a
    Vista tag, so this only repairs a half-converted node).  Block count / pointer words (+0x3C, +0x44) belong to the encoder."""
    mcc_data[0:0x3C] = vista_data[0:0x3C]
    fl = struct.unpack_from('<H', mcc_data, ROOT_FLAGS)[0] & ~FLAG_OG_XBOX_MIPMAP
    struct.pack_into('<H', mcc_data, ROOT_FLAGS, fl)
    mcc_data[0x4C:0x50] = vista_data[0x4C:0x50]


# ------------------------------------------------------------------------------------------------
# reading the Vista pixel stream
# ------------------------------------------------------------------------------------------------
def _candidate_caches(ctx, loc):
    seen = []

    def fresh(c):
        if c is None or any(c is o for o in seen):
            return False
        seen.append(c)
        return True

    def ref(name):
        try:
            return ctx.vista_refs.get(name)
        except Exception:                                   # noqa: BLE001  map not on disk
            return None
    try:
        cur = ctx.cur_cache
    except Exception:                                       # noqa: BLE001
        cur = None
    if loc == 0:
        if fresh(cur):
            yield cur
        if fresh(getattr(ctx, 'src', None)):
            yield ctx.src
    else:
        # absolute file identity: the shared maps are never the map being converted, except when the shared map IS the source
        c = ref(_LOC_FILE.get(loc))
        if fresh(c):
            yield c
        if fresh(cur):
            yield cur
        if fresh(getattr(ctx, 'src', None)):
            yield ctx.src


def read_vista_pixels(ctx, ptr, size):
    """Decompressed bytes behind a Vista (location << 30 | offset, size) pair.  The cache file is chosen by the location bits and
    VALIDATED: the stream must inflate completely from exactly `size` bytes.  Raises ValueError when no candidate map holds it."""
    ptr &= 0xFFFFFFFF
    loc, off = ptr >> 30, ptr & 0x3FFFFFFF
    last = None
    for c in _candidate_caches(ctx, loc):
        try:
            return vista_unpack_pixels(c.raw(off, size))
        except Exception as ex:                             # noqa: BLE001
            last = ex
    raise ValueError('pixel stream @0x%08X size 0x%X not found/valid in any candidate map (%s)' % (ptr, size, last))


def read_vista_pixels_salvage(ctx, ptr, size):
    """Like read_vista_pixels for a stream that does not inflate cleanly: the partial output of the first candidate file whose bytes
    start with a zlib header (78 xx).  b'' when nothing can be recovered."""
    ptr &= 0xFFFFFFFF
    loc, off = ptr >> 30, ptr & 0x3FFFFFFF
    for c in _candidate_caches(ctx, loc):
        try:
            blob = c.raw(off, size)
        except Exception:                                   # noqa: BLE001
            continue
        if len(blob) >= 2 and blob[0] == 0x78 and ((blob[0] << 8) | blob[1]) % 31 == 0:
            got = salvage_pixels(blob)
            if got:
                return got
    return b''


def decode_pixels(ctx, vnode):
    """-> (raw pixel bytes, problem) for one Vista bitmap element node; problem is None or a short string."""
    e = vnode.data
    w, h, d = struct.unpack_from('<hhB', e, 4)
    typ = struct.unpack_from('<h', e, 0xA)[0]
    fmt = struct.unpack_from('<H', e, 0xC)[0]
    mips = struct.unpack_from('<h', e, 0x14)[0]
    ptr = struct.unpack_from('<I', e, 0x1C)[0]
    size = struct.unpack_from('<I', e, 0x34)[0]
    if size == 0:
        return b'', 'no-pixels'
    try:
        raw = read_vista_pixels(ctx, ptr, size)
    except ValueError as ex:
        part = read_vista_pixels_salvage(ctx, ptr, size) if fmt in B.BITS_PER_PIXEL else b''
        if not part:
            return b'', 'store-not-found %s' % ex
        want = B.bitmap_raw_size(w, h, d, fmt, mips, typ)
        return part[:want], 'stream-corrupt salvaged %d of %d bytes (the rest is zero-filled)' % (min(len(part), want), want)
    if fmt not in B.BITS_PER_PIXEL:
        return raw, 'format %d unknown to the size formula' % fmt
    want = B.bitmap_raw_size(w, h, d, fmt, mips, typ)
    if len(raw) != want:
        return raw, 'pixel-size %d != expected %d' % (len(raw), want)
    return raw, None


# ------------------------------------------------------------------------------------------------
# element handler
# ------------------------------------------------------------------------------------------------
def _own_datum(ctx, vnode):
    ent = getattr(ctx, 'cur_entry', None)
    d = getattr(ent, 'out_datum', None) if ent is not None else None
    if d not in (None, NULL):
        return d
    return ctx.remap_datum(vnode.u32(0x4C))


def _raw_token(ctx, blob):
    """One token per distinct blob (the H2EK tool stores identical blobs once: default_2d / default_multiplicative share an offset)."""
    toks = getattr(ctx, '_rev_bitmap_tokens', None)
    if toks is None:
        toks = {}
        try:
            ctx._rev_bitmap_tokens = toks
        except Exception:                                   # noqa: BLE001
            pass
    key = (len(blob), hashlib.sha1(blob).digest())
    tok = toks.get(key)
    if tok is None:
        try:
            tok = ctx.add_raw('bitmap', blob, MCC_ALIGN_HINT)
        except TypeError:
            tok = ctx.add_raw('bitmap', blob)
        toks[key] = tok
    else:
        ctx.stats['bitm_blobs_shared'] += 1
    return tok


def build_element(ctx, vnode, mnode, parent_datum=None):
    """Rebuild the MCC element (0xA8) in mnode from the Vista element node.  Returns the problem string or None."""
    raw, problem = decode_pixels(ctx, vnode)
    e = bytes(vnode.data)
    if parent_datum is None:
        parent_datum = _own_datum(ctx, vnode)
    w, h, d = struct.unpack_from('<hhB', e, 4)
    typ = struct.unpack_from('<h', e, 0xA)[0]
    fmt = struct.unpack_from('<H', e, 0xC)[0]
    mips = struct.unpack_from('<h', e, 0x14)[0]
    if problem and problem.startswith(('pixel-size', 'stream-corrupt')) and raw and fmt in B.BITS_PER_PIXEL:
        # a wrong byte count would make the engine inflate into a mis-sized buffer: pad / truncate to the engine's formula
        want = B.bitmap_raw_size(w, h, d, fmt, mips, typ)
        raw = (raw + b'\0' * want)[:want]
    usable = bool(raw) and (problem is None or problem.startswith(('pixel-size', 'format', 'stream-corrupt')))
    new_fmt = None
    # opts['keep_legacy_formats'] = True stores A8Y8 / 16-bit pixels unchanged (only useful if MCC turns out to render them)
    if usable and fmt in REENCODE and not (getattr(ctx, 'opts', None) or {}).get('keep_legacy_formats'):
        raw = reencode_pixels(fmt, raw)
        new_fmt = REENCODE[fmt]
        ctx.stats['bitm_elements_reencoded'] += 1
    if usable and (new_fmt if new_fmt is not None else fmt) not in MCC_FORMATS:
        ctx.warn('bitm_format_unseen_in_mcc', '%s[%s]: format %d never occurs in an MCC map (kept as is)' % (
            getattr(vnode, 'path', ''), '', new_fmt if new_fmt is not None else fmt), getattr(getattr(ctx, 'cur_entry', None), 'name', None))
    blob = mcc_pack_pixels(raw) if usable else b''
    mnode.data[:] = vista_to_mcc_element(e, parent_datum if parent_datum is not None else NULL, new_fmt, len(blob))
    mnode.blocks.clear()
    mnode.datas.clear()
    mnode.patches.clear()
    if blob:
        mnode.patches[0x38] = _raw_token(ctx, blob)
        ctx.stats['bitm_elements'] += 1
        ctx.stats['bitm_pixel_bytes'] += len(blob)
    else:
        mnode.set_u32(0x38, 0)
        mnode.set_u32(0x50, 0)
        ctx.stats['bitm_elements_no_pixels'] += 1
    if problem and problem != 'no-pixels':
        ctx.warn('bitm_pixels', '%s: %s' % (getattr(vnode, 'path', ''), problem),
                 getattr(getattr(ctx, 'cur_entry', None), 'name', None))
    return problem


@node_handler('bitm', '/Bitmaps')
def bitm_element(ctx, vista_node, mcc_node):
    build_element(ctx, vista_node, mcc_node)


def _element_raw_size(e):
    w, h, d = struct.unpack_from('<hhB', e.data, 4)
    typ = struct.unpack_from('<h', e.data, 0xA)[0]
    fmt = struct.unpack_from('<H', e.data, 0xC)[0]
    mips = struct.unpack_from('<h', e.data, 0x14)[0]
    if fmt not in B.BITS_PER_PIXEL:
        return None
    return B.bitmap_raw_size(w, h, d, fmt, mips, typ)


def fix_pixels_offsets(vista_node, mcc_node):
    """+0x18 'pixels_offset' is, in 92 % of the multi-bitmap groups of both games, the cumulative decompressed size of the previous
    elements.  When a format was expanded (REENCODE) the sizes change, so such a group's offsets are recomputed from the new sizes
    (groups whose Vista offsets are not cumulative keep them).  Returns the number of elements changed."""
    fv, fm = vista_node.field('Bitmaps'), mcc_node.field('Bitmaps')
    if fv is None or fm is None:
        return 0
    ve, me = vista_node.blocks.get(fv.offset) or [], mcc_node.blocks.get(fm.offset) or []
    if len(ve) != len(me) or len(ve) < 2:
        return 0
    if all(v.u16(0xC) == m.u16(0xC) for v, m in zip(ve, me)):
        return 0
    cum = 0
    for v in ve:
        sz = _element_raw_size(v)
        if sz is None or v.u32(0x18) != cum:
            return 0
        cum += sz
    cum, n = 0, 0
    for m in me:
        sz = _element_raw_size(m)
        if sz is None:
            return n
        if m.u32(0x18) != cum:
            m.set_u32(0x18, cum)
            n += 1
        cum += sz
    return n


@node_handler('bitm', '', prio=200)
def bitm_group(ctx, vista_node, mcc_node):
    fix_group_header(vista_node.data, mcc_node.data)
    ctx.stats['bitm_pixels_offsets_fixed'] += fix_pixels_offsets(vista_node, mcc_node)
    ctx.stats['bitm_groups'] += 1


# ------------------------------------------------------------------------------------------------
# standalone conversion (tests / tools): Vista bitm root Node -> MCC bitm root Node without rev/transcode.py
# ------------------------------------------------------------------------------------------------
def convert_standalone(ctx, vista_root):
    """Build the MCC Node tree for one Vista bitm tag the way rev/transcode.py + the handlers do: identical layout for the root /
    Sequences / Sprites, element rebuilt by bitm_element, then the group handler."""
    from ... import tagtree as T
    lay = ctx.lay_mcc['bitm']
    root = T.Node('bitm', '', lay.fields, len(vista_root.data), vista_root.data)

    def clone(src_children):
        out = []
        for k in src_children:
            n = T.Node('bitm', k.path, k.fields, len(k.data), k.data)
            for off, kids in k.blocks.items():
                n.blocks[off] = clone(kids)
            for off, payload in k.datas.items():
                n.datas[off] = payload
            out.append(n)
        return out

    seq = root.field('Sequences')
    if seq is not None:
        root.blocks[seq.offset] = clone(vista_root.blocks.get(seq.offset, []))
    bf = vista_root.field('Bitmaps')
    mf = [f for f in lay.fields if f.name == 'Bitmaps'][0]
    kids = []
    for v in vista_root.blocks.get(bf.offset, []):
        m = T.Node('bitm', '/Bitmaps', mf.children, MCC_ELEM)
        bitm_element(ctx, v, m)
        kids.append(m)
    root.blocks[mf.offset] = kids
    for off, payload in vista_root.datas.items():
        root.datas[off] = payload
    bitm_group(ctx, vista_root, root)
    return root
