# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""bitm handlers: MCC bitmap group (v13) -> Halo 2 Vista bitmap group (v8).

Registered handlers:
    @node_handler('bitm', '')           group header: flags |= 0x20, compression-quality constants, height-map fixes
    @node_handler('bitm', '/Bitmaps')   one bitmap element 0xA8 -> 0x74, pixel data -> ctx.add_raw('bitmap', zlib stream)

Pixel data
----------
MCC stores each element's mip chain as a chunked resource (u32 n; i32 size[n]; chunks) either in textures.dat
(stock maps: map header flags@0x1C bit3) or inside the map itself (H2EK-built maps).  Vista wants ONE plain zlib
stream per element.  The handler decodes the MCC resource, checks the byte count against the engine's size formula
(bitmaps.bitmap_raw_size), recompresses (level 9) and hands the unpadded stream to ctx.add_raw('bitmap', blob); the
Vista element gets lod1_size = len(blob) and node.patches[0x1C] = token (the driver writes the final raw pointer).

Which store?  The handler never trusts a flag alone: it validates the blob header (u32 n, 4+4n+sum|size| == lod1_size)
against every candidate store (the entry's own cache first, then ctx.src, then the MCC shared maps) and remembers
the winner.  See _locate().

Everything is idempotent and rebuilds the Vista element from the MCC element, so it does not matter what the generic
conversion put into vista_node.
"""
import struct
import zlib

from . import node_handler, pre_pass
from .. import bitmaps as B
from .. import raw_mcc as R

MCC_ELEM = B.MCC_BITMAP_ELEM        # 0xA8
VISTA_ELEM = B.VISTA_BITMAP_ELEM    # 0x74
NULL = 0xFFFFFFFF
SHARED_KEYS = ('shared', 'single_player_shared', 'mainmenu')

# bitm root: offsets (identical in both games)
ROOT_USAGE = 0x04
ROOT_FLAGS = 0x06
ROOT_QUALITY = 0x4C            # color quality, alpha quality, overlap, subsampling (4 x int8)
VISTA_QUALITY = bytes((0x0F, 0x0F, 0x01, 0x03))
USAGE_HEIGHT_MAP = 2
FLAG_WDP = 0x0020              # Vista: always set ('WDP compression'); MCC clears it for height-map groups
FLAG_OG_XBOX_MIPMAP = 0x2000   # MCC-only root flag bit 13 (no Vista meaning)

# per-ctx state (store cache, counters).  Kept off the Ctx object's slots by using an id()-keyed dict.
_STATE = {}


class _State(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.dats = {}          # directory -> raw_mcc.DatStore
        self.hint = None        # label of the store that matched last
        self.stores = {}        # label -> pixel buffer (mmap) or None when unavailable


def _state(ctx):
    st = _STATE.get(id(ctx))
    if st is None or st.ctx is not ctx:
        st = _STATE[id(ctx)] = _State(ctx)
    return st


def reset_state(ctx=None):
    """Forget cached stores (tests)."""
    if ctx is None:
        _STATE.clear()
    else:
        _STATE.pop(id(ctx), None)


# ------------------------------------------------------------------------------------------------
# pixel store lookup
# ------------------------------------------------------------------------------------------------
def _textures_dat(ctx, st, cache):
    """mmap of textures.dat for a stock cache (directory: ctx.opts['mcc_maps_dir'] > the cache's own folder > the
    installed MCC maps folder)."""
    cands = []
    opts = getattr(ctx, 'opts', None) or {}
    for k in ('mcc_maps_dir', 'mcc_dir', 'dat_dir'):
        if opts.get(k):
            cands.append(opts[k])
    sp = getattr(cache, 'source_path', None)
    if sp:
        import os
        cands.append(os.path.dirname(sp))
    from .. import paths as _paths
    inst = _paths.mcc_maps_dir(required=False)          # --mcc-maps / H2CONV_MCC_MAPS / settings / your Steam library
    if inst:
        cands.append(inst)
    import os
    for d in cands:
        if os.path.isfile(os.path.join(d, R.TEXTURES_DAT)):
            ds = st.dats.get(d)
            if ds is None:
                ds = st.dats[d] = R.DatStore(d)
            return ds.get(R.TEXTURES_DAT)
    raise FileNotFoundError('textures.dat not found (tried %s)' % ', '.join(cands))


def _cache_for(ctx, label):
    """The MccCache a store label names ('src' = the input map, else an MCC shared map opened on demand), or None."""
    if label == 'src':
        return getattr(ctx, 'src', None)
    ms = getattr(ctx, 'mcc_shared', None)
    return ms.get(label) if ms is not None else None


def _buffer_for(ctx, st, label, cache):
    if cache.header['flags'] & R.HDR_FLAG_EXTERNAL_DAT:
        return _textures_dat(ctx, st, cache)
    return cache.mm


def _blob_ok(buf, off, size):
    """True when buf[off:] starts a chunked resource whose length equals `size` (4 + 4n + sum|chunk size|)."""
    if size < 8 or off < 0 or off + 8 > len(buf):
        return False
    n = struct.unpack_from('<I', buf, off)[0]
    if not 0 < n <= 4096 or off + 4 + 4 * n > len(buf):
        return False
    tot = 4 + 4 * n
    for s in struct.unpack_from('<%di' % n, buf, off + 4):
        tot += abs(s)
    return tot == size and off + size <= len(buf)


def _locate(ctx, mcc_node, off, size):
    """-> pixel buffer holding the element's resource (validated against the blob header), or None.
    Candidate order: the store of the entry being converted (ctx.cur_entry.src_map), then - by address - the input map or a
    shared map, then the store that matched last, then everything else.  MCC shared maps are only opened when needed."""
    st = _state(ctx)
    order = ['src'] + list(SHARED_KEYS)
    ent = getattr(ctx, 'cur_entry', None)
    first = getattr(ent, 'src_map', None) if ent is not None else None
    if first in order:
        order.remove(first)
        order.insert(0, first)
    else:
        src = getattr(ctx, 'src', None)
        a = getattr(mcc_node, 'src_addr', None)
        if src is not None and a is not None and src.mask and a < src.mask:
            order.remove('src')                    # tag data below the input map's mask lives in a shared map
            order.append('src')
        elif st.hint in order:
            order.remove(st.hint)
            order.insert(0, st.hint)
    for lab in order:
        entry = st.stores.get(lab)
        if entry is None:
            cache = _cache_for(ctx, lab)
            buf = None
            if cache is not None:
                try:
                    buf = _buffer_for(ctx, st, lab, cache)
                except FileNotFoundError:
                    buf = None
            entry = st.stores[lab] = buf
        if entry is None:
            continue
        if _blob_ok(entry, off, size):
            st.hint = lab
            return entry
    return None


@pre_pass()
def bitm_prepare(ctx):
    """Fail early and loudly (one warning) when the input map keeps its bitmaps in textures.dat and the file cannot be found;
    otherwise every element would be reported separately and come out without pixels."""
    src = getattr(ctx, 'src', None)
    if src is None or not (src.header['flags'] & R.HDR_FLAG_EXTERNAL_DAT):
        return
    try:
        _textures_dat(ctx, _state(ctx), src)
    except FileNotFoundError as ex:
        ctx.warn('bitm_no_textures_dat', 'bitmap pixels live in textures.dat but it was not found: %s (set opts mcc_maps_dir)' % ex)


# ------------------------------------------------------------------------------------------------
# element conversion
# ------------------------------------------------------------------------------------------------
def _own_datum(ctx, mcc_node):
    """Output datum of the bitm tag that owns this element."""
    ent = getattr(ctx, 'cur_entry', None)
    d = getattr(ent, 'out_datum', None) if ent is not None else None
    if d not in (None, NULL):
        return d
    return ctx.remap_datum(mcc_node.u32(0x1C))


def decode_pixels(ctx, mcc_node):
    """-> (raw pixel bytes, problem) for one MCC bitmap element node; problem is None or a short string."""
    e = mcc_node.data
    w, h, d = struct.unpack_from('<hhB', e, 4)
    typ = struct.unpack_from('<h', e, 0xA)[0]
    fmt = struct.unpack_from('<H', e, 0xC)[0]
    mips = struct.unpack_from('<h', e, 0x14)[0]
    off = struct.unpack_from('<I', e, 0x38)[0]
    size = struct.unpack_from('<I', e, 0x50)[0]
    if size == 0:
        return b'', 'no-pixels'
    buf = _locate(ctx, mcc_node, off, size)
    if buf is None:
        return b'', 'store-not-found off=0x%X size=0x%X' % (off, size)
    raw, used = B.mcc_read_resource(buf, off)
    if used != size:
        return b'', 'resource-length %d != %d' % (used, size)
    if fmt not in B.BITS_PER_PIXEL:
        return raw, 'format %d unknown to the Vista size formula' % fmt
    want = B.bitmap_raw_size(w, h, d, fmt, mips, typ)
    if len(raw) != want:
        return raw, 'pixel-size %d != expected %d' % (len(raw), want)
    return raw, None


def build_element(ctx, mcc_node, vista_node, parent_datum=None):
    """Rebuild the Vista element (0x74) in vista_node from the MCC element node.  Returns the problem string or None."""
    raw, problem = decode_pixels(ctx, mcc_node)
    e = bytes(mcc_node.data)
    if parent_datum is None:
        parent_datum = _own_datum(ctx, mcc_node)
    w, h, d = struct.unpack_from('<hhB', e, 4)
    typ = struct.unpack_from('<h', e, 0xA)[0]
    fmt = struct.unpack_from('<H', e, 0xC)[0]
    mips = struct.unpack_from('<h', e, 0x14)[0]
    if problem and problem.startswith('pixel-size') and raw and fmt in B.BITS_PER_PIXEL:
        # A wrong byte count would make the engine inflate into a mis-sized buffer: pad / truncate to the engine's formula.
        want = B.bitmap_raw_size(w, h, d, fmt, mips, typ)
        raw = (raw + b'\0' * want)[:want]
    vista_node.data[:] = B.convert_bitmap_element(e, parent_datum if parent_datum is not None else NULL, 0, 0)
    vista_node.blocks.clear()
    vista_node.datas.clear()
    vista_node.patches.clear()
    if raw and (problem is None or problem.startswith('pixel-size') or problem.startswith('format')):
        blob = B.vista_pack_pixels(raw)
        tok = ctx.add_raw('bitmap', blob)
        vista_node.set_u32(0x34, len(blob))
        vista_node.patches[0x1C] = tok
        ctx.stats['bitm_elements'] += 1
        ctx.stats['bitm_pixel_bytes'] += len(blob)
    else:
        vista_node.set_u32(0x1C, 0)
        vista_node.set_u32(0x34, 0)
        ctx.stats['bitm_elements_no_pixels'] += 1
    if problem and problem != 'no-pixels':
        ctx.warn('bitm_pixels', '%s: %s' % (getattr(mcc_node, 'path', ''), problem),
                 getattr(getattr(ctx, 'cur_entry', None), 'name', None))
    return problem


@node_handler('bitm', '/Bitmaps')
def bitm_element(ctx, mcc_node, vista_node):
    build_element(ctx, mcc_node, vista_node)


# ------------------------------------------------------------------------------------------------
# group header
# ------------------------------------------------------------------------------------------------
def fix_header(mcc_node, vista_node):
    """Root 0x50 bytes: identical layout; apply the Vista tool's constants (verified 174/174 on stock Vista headlong).
    Block count / pointer words (+0x3C, +0x44) are left alone (the encoder owns them)."""
    m = bytes(mcc_node.data)
    d = vista_node.data
    # fields before the first block (0x00..0x3B) are a verbatim copy (also repairs a half-converted node)
    d[0:0x3C] = m[0:0x3C]
    flags = struct.unpack_from('<H', m, ROOT_FLAGS)[0]
    flags &= ~FLAG_OG_XBOX_MIPMAP          # MCC-only bit, Vista's flags16 has no bit 13
    flags |= FLAG_WDP                       # Vista always sets it
    struct.pack_into('<H', d, ROOT_FLAGS, flags)
    q = m[ROOT_QUALITY:ROOT_QUALITY + 4]
    d[ROOT_QUALITY:ROOT_QUALITY + 4] = VISTA_QUALITY if q == b'\0\0\0\0' else q


def _bitmaps_off(node):
    f = node.field('Bitmaps')
    return f.offset if f is not None else 0x44


@node_handler('bitm', '', prio=200)
def bitm_group(ctx, mcc_node, vista_node):
    fix_header(mcc_node, vista_node)
    usage = mcc_node.u16(ROOT_USAGE)
    kids = vista_node.blocks.get(_bitmaps_off(vista_node), [])
    if usage == USAGE_HEIGHT_MAP:
        # MCC re-encodes height maps to A8R8G8B8 and leaves cache_usage = lod_adjust = 0; Vista has 1/2/5 there
        for v in kids:
            if v.u8(0x17) == 0:
                v.set_u8(0x17, B.guess_bump_cache_usage(0))
    ctx.stats['bitm_groups'] += 1


# ------------------------------------------------------------------------------------------------
# standalone conversion (tests / tools): MCC bitm root Node -> Vista bitm root Node without transcode.py
# ------------------------------------------------------------------------------------------------
def convert_standalone(ctx, mcc_root):
    """Build the Vista Node tree for one MCC bitm tag the way transcode.py + the handlers do: identical layout for the
    root / Sequences / Sprites, element rebuilt by bitm_element, then the group handler."""
    from .. import tagtree as T
    lay = ctx.lay_vista['bitm']
    root = T.Node('bitm', '', lay.fields, len(mcc_root.data), mcc_root.data)

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
        root.blocks[seq.offset] = clone(mcc_root.blocks.get(seq.offset, []))
    bf = root.field('Bitmaps')
    vf = [f for f in lay.fields if f.name == 'Bitmaps'][0]
    kids = []
    for m in mcc_root.blocks.get(bf.offset, []):
        v = T.Node('bitm', '/Bitmaps', vf.children, VISTA_ELEM)
        bitm_element(ctx, m, v)
        kids.append(v)
    root.blocks[bf.offset] = kids
    for off, payload in mcc_root.datas.items():
        root.datas[off] = payload
    bitm_group(ctx, mcc_root, root)
    return root
