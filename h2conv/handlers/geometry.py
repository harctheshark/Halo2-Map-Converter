# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Geometry ('blkh' raw block) conversion MCC -> Vista.

Every user of the 36-byte `geometry_block_info` struct (SITES below) points at a raw block outside the tag data:

    'blkh'  u32 resource_data_size  |  section data (sds)  |  resource data (rds)  |  'fklb'          (little-endian words,
    the bytes on disk read  68 6B 6C 62 ... 66 6B 6C 62)

The resource data is a list of payloads described by the 16-byte `geometry_block_resource` ("fixup") entries stored in
the tag (`Resources` block of the info struct).  Every payload is preceded by a 'crsr' marker; zero padding goes BEFORE
the marker so that the payload start is aligned to 1 << entry.align_bits relative to the block start (identical rule in
both games, proven on 61,134 Vista blocks of 43 maps and 38,450 MCC blocks of 48 maps).

MCC -> Vista differences (all measured):
  * the section data struct of mode Sections (0x78 -> 0x70) and of sbsp Clusters / Instanced Geometry / Water
    (0x48 -> 0x44) carries MCC's 64-bit `Index Buffer Pointer` (mode: plus an 8-byte tail) - always zero in a cache file;
    the extra bytes are dropped and the primary locators of the fixup entries after them move down by 4;
  * because the section data shrinks, the resource data is laid out again with the Vista rule (the 16-aligned `Tag Data`
    payloads - MOPP / reorder code - land on the same absolute block offset, so their padding grows);
  * every other site (ltmp Geometry Buckets, sbsp Decorators, DECR, PRTM, weat, mode PRT Info, scnr / dc*s decorators,
    ugh! lipsync Extra Info) has the same section struct, so its block is bit-identical apart from float noise.
The vertex / index / part payload bytes are copied untouched.

Public API
    SITES                                  the table of all geometry_block_info users
    convert_block(kind, blk, entries)      pure bytes -> (new block, new entries, new sds, new rds)
    convert_info(ctx, ...)                 info struct + block -> Vista info struct bytes + raw token (also for ugh! Extra Info)
    node handlers                          registered for every SITE (idempotent); not for ugh! (sound converter owns it)
    post pass                              exact size numbers for block-less ltmp cluster infos
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import struct
from collections import namedtuple

from . import node_handler, post_pass

MAGIC = b'hklb'          # the 'blkh' header magic as it appears on disk
FOOT = b'fklb'
CRSR = b'crsr'
INFO_SIZE = 0x24
ENTRY_SIZE = 0x10
NO_BLOCK = 0xFFFFFFFF

# resource entry types (engine: e_geometry_resource_type)
RES_TAG_BLOCK, RES_TAG_DATA, RES_VERTEX_BUFFER = 0, 1, 2

Site = namedtuple('Site', 'group path info_off label kind')

# kind: 'mode'      render-model section   MCC sds 0x78 -> Vista 0x70   drop [0x44,0x48) and [0x74,0x78)
#       'bsp'       cluster / instance / water section, MCC 0x48 -> 0x44  drop [0x44,0x48)
#       'plain'     identical section struct
SITES = (
    Site('mode', '/Sections', 0x38, 'Sections', 'mode'),
    Site('mode', '/PRT Info', 0x34, 'PRT Info', 'plain'),
    Site('sbsp', '/Clusters', 0x28, 'Clusters', 'bsp'),
    Site('sbsp', '/Instanced Geometry Definitions', 0x28, 'Instanced Geometry Definitions', 'bsp'),
    Site('sbsp', '/Water Definitions', 0x10, 'Water Definitions', 'bsp'),
    Site('sbsp', '/Decorators/Cache Blocks', 0x00, 'Decorators / Cache Blocks', 'plain'),
    Site('ltmp', '/Lightmap Groups/Clusters', 0x28, 'Lightmap Groups / Clusters', 'bsp'),
    Site('ltmp', '/Lightmap Groups/Poop Definitions', 0x28, 'Lightmap Groups / Poop Definitions', 'bsp'),
    Site('ltmp', '/Lightmap Groups/Geometry Buckets', 0x0C, 'Lightmap Groups / Geometry Buckets', 'plain'),
    Site('scnr', '/Decorators/Cache Blocks', 0x00, 'Decorators / Cache Blocks', 'plain'),
    Site('DECR', '', 0x38, 'Decorator set (root)', 'plain'),
    Site('PRTM', '', 0xA0, 'Particle model (root)', 'plain'),
    Site('dc*s', '/Decorators/Cache Blocks', 0x00, 'Decorators / Cache Blocks', 'plain'),
    Site('weat', '/Particle System', 0x40, 'Particle System', 'plain'),
)
# ugh! 'Extra Info' (lipsync): Vista element has ONE info struct at +0x08, MCC has 8 (one per language) at
# +0x08 + 0x24*k.  The sound converter owns the ugh! tag (it copies the blob verbatim, which a 'plain' conversion
# reproduces); convert_info() / ugh_extra_info_node() are available if it wants this module to do the work.
UGH_EXTRA_INFO_VISTA_OFFSET = 0x08
UGH_EXTRA_INFO_MCC_OFFSETS = (0x08, 0x2C, 0x50, 0x74, 0x98, 0xBC, 0xE0, 0x104)
UGH_SITE = Site('ugh!', '/Extra Info', 0x08, 'Extra Info (lipsync)', 'plain')

# bytes of the MCC section struct that Vista does not have: kind -> (mcc_sds, vista_sds, [(offset, length)])
SECTION_DROPS = {
    'mode': (0x78, 0x70, ((0x44, 4), (0x74, 4))),
    'bsp': (0x48, 0x44, ((0x44, 4),)),
    'plain': (None, None, ()),
}

_ENTRY = struct.Struct('<BBHhhII')       # type, align_bits, primary(dup), primary, secondary, size, offset
_INFO_HEAD = struct.Struct('<iIII')      # block_offset, block_size, section_data_size, resource_data_size


class GeometryError(Exception):
    pass


Entry = namedtuple('Entry', 'type align_bits dup primary secondary size offset')


def decode_entry(b):
    return Entry(*_ENTRY.unpack(bytes(b)))


def encode_entry(e):
    return _ENTRY.pack(*e)


def site_for(group, path):
    for s in SITES:
        if s.group == group and s.path == path:
            return s
    return None


# ---------------------------------------------------------------------------------------------------------------------
# pure block conversion
# ---------------------------------------------------------------------------------------------------------------------
def check_block(blk, sds, rds, block_size=None):
    """Framing check of a raw block against its info numbers; raises GeometryError."""
    if blk[:4] != MAGIC or blk[-4:] != FOOT:
        raise GeometryError('bad blkh/fklb framing (head %r tail %r)' % (bytes(blk[:4]), bytes(blk[-4:])))
    if len(blk) != 8 + sds + rds + 4 or (block_size is not None and block_size != len(blk)):
        raise GeometryError('block size %d != 8+sds 0x%X+rds 0x%X+4' % (len(blk), sds, rds))
    if struct.unpack_from('<I', blk, 4)[0] != rds:
        raise GeometryError('header resource size 0x%X != info rds 0x%X' % (struct.unpack_from('<I', blk, 4)[0], rds))


def split_block(blk, sds, rds, entries):
    """-> (section_data bytes, [payload bytes per entry]).  Payloads are cut by the entries' explicit offsets."""
    check_block(blk, sds, rds)
    sd = bytes(blk[8:8 + sds])
    rd = memoryview(blk)[8 + sds:8 + sds + rds]
    pays = []
    for e in entries:
        if e.offset < 4 or e.offset + e.size > rds:
            raise GeometryError('resource entry offset 0x%X size 0x%X outside resource data 0x%X' % (e.offset, e.size, rds))
        pays.append(bytes(rd[e.offset:e.offset + e.size]))
    return sd, pays


def layout_block(sd, pays, entries):
    """Build a block from section data + payloads with the Vista layout rule (module docstring).
    Returns (block bytes, new Entry list, rds)."""
    base = 8 + len(sd)
    out = bytearray(MAGIC + b'\0\0\0\0' + sd)
    new = []
    for e, p in zip(entries, pays):
        al = 1 << e.align_bits
        start = (len(out) + 4 + al - 1) & ~(al - 1)
        out.extend(b'\0' * (start - 4 - len(out)))
        out.extend(CRSR)
        out.extend(p)
        new.append(e._replace(offset=start - base))
    out.extend(b'\0' * (((len(out) + 3) & ~3) - len(out)))
    rds = len(out) - base
    struct.pack_into('<I', out, 4, rds)
    out.extend(FOOT)
    return bytes(out), new, rds


def verify_block(blk, sds, rds, entries):
    """Check a block against the layout rule shared by both games.  Returns a list of problem strings (empty = valid).
    Proven to return [] for all 61,000+ Vista blocks of the 43 stock Vista maps."""
    pr = []
    try:
        check_block(blk, sds, rds)
    except GeometryError as ex:
        return [str(ex)]
    base = 8 + sds
    cur = base
    for i, e in enumerate(entries):
        if e.dup != e.primary:
            pr.append('entry %d: +2 word 0x%X != primary locator 0x%X' % (i, e.dup, e.primary))
        al = 1 << e.align_bits
        start = (cur + 4 + al - 1) & ~(al - 1)
        if start - base != e.offset:
            pr.append('entry %d: offset 0x%X, layout rule says 0x%X' % (i, e.offset, start - base))
            return pr
        if blk[start - 4:start] != CRSR:
            pr.append('entry %d: no crsr marker' % i)
        if any(blk[cur:start - 4]):
            pr.append('entry %d: non-zero padding' % i)
        cur = start + e.size
    if ((cur + 3) & ~3) - base != rds:
        pr.append('resource data size 0x%X, layout rule says 0x%X' % (rds, ((cur + 3) & ~3) - base))
    if any(blk[cur:len(blk) - 4]):
        pr.append('non-zero tail padding')
    return pr


def convert_section(kind, sd, strict=False):
    """MCC section struct bytes -> Vista section struct bytes.  Returns (bytes, drops) with drops = list of
    (offset, length) removed (offsets in MCC space).  Raises GeometryError for an unexpected size."""
    msz, vsz, drops = SECTION_DROPS[kind]
    if not drops:
        return bytes(sd), ()
    if len(sd) != msz:
        raise GeometryError("%s section data is 0x%X bytes, expected MCC size 0x%X" % (kind, len(sd), msz))
    out = bytearray()
    pos = 0
    for o, n in drops:
        out += sd[pos:o]
        if strict and any(sd[o:o + n]):
            raise GeometryError('non-zero runtime bytes %s at section +0x%X' % (bytes(sd[o:o + n]).hex(), o))
        pos = o + n
    out += sd[pos:]
    return bytes(out), drops


def _shift_locator(loc, drops):
    new = loc
    for o, n in drops:
        if loc >= o + n:
            new -= n
        elif loc >= o:
            raise GeometryError('fixup locator 0x%X points into a dropped section range' % loc)
    return new


CD_FILL = bytes([0xCD])
CD_TAIL_MIN = 64          # bytes: shorter trailing 0xCD runs are left alone (could be real data)


def scrub_payload(entry, payload):
    """MCC leaves the unused tail of a vertex buffer as MSVC debug-heap fill (0xCD); Vista has zeros there
    (sbsp Water Definitions, 42,948 of 60,000 bytes in headlong).  Only a long TRAILING run of a vertex-buffer payload
    is zeroed - 0xCDCDCDCD also occurs inside genuine Tag Data (MOPP code, identical in both games)."""
    if entry.type != RES_VERTEX_BUFFER or len(payload) < CD_TAIL_MIN:
        return payload, 0
    n = len(payload) - len(payload.rstrip(CD_FILL))
    n &= ~3
    if n < CD_TAIL_MIN:
        return payload, 0
    return payload[:len(payload) - n] + bytes(n), n


VB_DEF_SIZE = 0x20            # rasterizer_vertex_buffer: int8 declaration, u8 stride, u16 count, ... (32 bytes)
DECL_PAD4 = 0x2E              # declaration whose 4th byte of every 4-byte vertex is padding


def scrub_vertex_padding(entries, pays):
    """Vertex declaration 0x2E (stride 4, only in render-model sections: 32 of 2,234 mode buffers in the corpus) keeps
    three data bytes and one PADDING byte per vertex.  MCC leaves the padding byte as 0xCD (uninitialised heap) in
    13,469 / 13,469 vertices; Vista has 0 in 12,503 of them (the rest are stale-heap junk).  Writing 0 is what
    Vista ships and cannot hurt (the byte is never meaningful).  Other uninitialised-padding bytes (0xCC / 0xCD / 0xCE
    fill in both games) are left alone.  Returns the new payload list and the number of bytes changed."""
    vbdefs = {}
    for e, p in zip(entries, pays):
        if e.type == RES_TAG_BLOCK and e.secondary == VB_DEF_SIZE:
            vbdefs[e.primary] = p
    out = list(pays)
    changed = 0
    for i, e in enumerate(entries):
        if e.type != RES_VERTEX_BUFFER or e.secondary < 0:
            continue
        d = vbdefs.get(e.primary)
        if d is None or (e.secondary + 1) * VB_DEF_SIZE > len(d):
            continue
        decl, stride = d[e.secondary * VB_DEF_SIZE], d[e.secondary * VB_DEF_SIZE + 1]
        if decl != DECL_PAD4 or stride != 4:
            continue
        b = bytearray(pays[i])
        for j in range(3, len(b), 4):
            if b[j] == 0xCD:
                b[j] = 0
                changed += 1
        out[i] = bytes(b)
    return out, changed


def convert_block(kind, blk, entries, sds, rds, strict=False, stats=None):
    """Convert one MCC raw block to the Vista block.

    kind    'mode' | 'bsp' | 'plain'
    blk     the MCC 'blkh'...'fklb' bytes;   entries  list of Entry (decode_entry of the MCC Resources block)
    sds/rds the MCC info numbers;   stats  optional Counter receiving the scrub counters
    Returns (vista block bytes, vista Entry list, vista sds, vista rds)."""
    sd, pays = split_block(blk, sds, rds, entries)
    nsd, drops = convert_section(kind, sd, strict)
    scr = [scrub_payload(e, p) for e, p in zip(entries, pays)]
    pays = [p for p, _n in scr]
    pays, pad = scrub_vertex_padding(entries, pays)
    if stats is not None:
        stats['geometry_cd_tail_bytes_zeroed'] += sum(n for _p, n in scr)
        stats['geometry_vertex_padding_bytes_zeroed'] += pad
    if drops:
        entries = [e._replace(primary=_shift_locator(e.primary, drops), dup=_shift_locator(e.dup, drops)) for e in entries]
    nblk, nent, nrds = layout_block(nsd, pays, entries)
    return nblk, nent, len(nsd), nrds


# ---------------------------------------------------------------------------------------------------------------------
# reading the MCC block from the right file
# ---------------------------------------------------------------------------------------------------------------------
_LOC_NAME = {1: 'mainmenu', 2: 'shared', 3: 'single_player_shared'}


def _candidate_caches(ctx, loc):
    """Generator of the MCC caches that may hold a block whose offset dword has location bits `loc`, most likely first
    (lazy: the MCC shared maps are only opened when the cheaper candidates failed)."""
    seen = []

    def fresh(c):
        if c is None or any(c is o for o in seen):
            return False
        seen.append(c)
        return True
    cur = None
    try:
        if hasattr(ctx, 'cache_for'):
            cur = ctx.cur_cache            # transcode.Ctx: the MCC map the tag being converted comes from
    except Exception:                      # map not available (KeyError) - fall back to the search below
        cur = None
    shared = lambda nm: _safe_get(ctx.mcc_shared, nm)
    if loc == 0:
        if fresh(cur):
            yield cur
        if fresh(ctx.src):
            yield ctx.src
        for nm in ('shared', 'single_player_shared', 'mainmenu'):
            c = shared(nm)
            if fresh(c):
                yield c
    else:
        # location 2 inside shared.map means the file itself: the current cache is the first guess
        if fresh(cur):
            yield cur
        c = shared(_LOC_NAME.get(loc))
        if fresh(c):
            yield c
        if fresh(ctx.src):
            yield ctx.src


def _safe_get(d, key):
    try:
        return d.get(key)
    except Exception:
        return None


def read_mcc_block(ctx, block_offset, block_size, sds, rds):
    """Raw bytes of the MCC block.  MCC offsets are plain file offsets (location bits only appear in shared.map where
    location 2 = the map itself).  The cache is found by validation (framing + sizes), the current source map first."""
    loc = (block_offset & 0xFFFFFFFF) >> 30
    off = block_offset & 0x3FFFFFFF
    last = None
    for c in _candidate_caches(ctx, loc):
        try:
            blk = c.raw(off, block_size)
            check_block(blk, sds, rds, block_size)
            return blk, c
        except Exception as ex:
            last = ex
    raise GeometryError('MCC geometry block @0x%X size 0x%X not found in any candidate map (%s)' % (block_offset, block_size, last))


# ---------------------------------------------------------------------------------------------------------------------
# info struct conversion
# ---------------------------------------------------------------------------------------------------------------------
def _tokens(ctx):
    d = getattr(ctx, '_geometry_tokens', None)
    if d is None:
        d = {}
        try:
            ctx._geometry_tokens = d
        except Exception:
            pass
    return d


def _stat(ctx, key, n=1):
    st = getattr(ctx, 'stats', None)
    if st is not None:
        st[key] += n


def _numbers(ctx):
    """{(mcc sds, mcc rds): {(vista sds, vista rds)}} of every converted 'bsp' block (for the post pass)."""
    d = getattr(ctx, '_geometry_numbers', None)
    if d is None:
        d = {}
        try:
            ctx._geometry_numbers = d
        except Exception:
            pass
    return d


def estimate_empty_numbers(kind, sds, rds, entries):
    """Size numbers of an info that has no block.  Used by ltmp 'Lightmap Groups / Clusters' and 'Poop Definitions': their
    infos are copies of the numbers of the sbsp Clusters / Instanced Geometry Definitions (block offset -1, size 0, no
    Resources) - 65 + 32 in headlong, 0 real blocks in any of the 47+43 stock maps.  The engine never reads a block
    for them; the numbers are only converted for fidelity: sds drops the 4 pointer bytes and rds is the common
    case (+4, 78% of the sbsp blocks); the post pass replaces the estimate by the exact value of the sbsp block with
    the same MCC numbers when that is unambiguous.  Returns (sds, rds, entries)."""
    msz, vsz, drops = SECTION_DROPS[kind]
    if not drops or sds != msz:
        return sds, rds, entries
    if entries:                                   # fixup list but no block: re-lay it out (offsets / rds from the sizes)
        ents = [e._replace(primary=_shift_locator(e.primary, drops), dup=_shift_locator(e.dup, drops)) for e in entries]
        base = 8 + vsz
        cur = base
        new = []
        for e in ents:
            al = 1 << e.align_bits
            start = (cur + 4 + al - 1) & ~(al - 1)
            new.append(e._replace(offset=start - base))
            cur = start + e.size
        return vsz, ((cur + 3) & ~3) - base, new
    if kind == 'bsp' and rds:
        return vsz, rds + 4, entries
    return vsz, rds, entries


def convert_info(ctx, kind, mcc_info36, mcc_entry_bytes, owner_datum=None, strict=False):
    """Convert one geometry_block_info.

    mcc_info36        the 36 info bytes of the MCC struct
    mcc_entry_bytes   list of the MCC `Resources` elements (16 bytes each)
    Returns (vista_info36 with the block offset word left 0, [16-byte Vista entries], token or None).
    The caller patches the block-offset word with the token.  An info WITHOUT a block (offset 0 / -1, size 0) returns
    token None and the info with the offset word kept: only the section / resource size numbers are converted
    (they are leftovers copied from the sbsp cluster the lightmap cluster belongs to, see estimate_empty_numbers)."""
    bo, bs, sds, rds = _INFO_HEAD.unpack_from(mcc_info36, 0)
    tail = bytes(mcc_info36[0x18:0x24])
    if bs == 0 or bo in (0, NO_BLOCK):
        entries = [decode_entry(e) for e in mcc_entry_bytes]
        nsds, nrds, nent = estimate_empty_numbers(kind, sds, rds, entries)
        info = bytearray(mcc_info36)
        _INFO_HEAD.pack_into(info, 0, bo, bs, nsds, nrds)
        return bytes(info), [encode_entry(e) for e in nent], None
    entries = [decode_entry(e) for e in mcc_entry_bytes]
    key = (bo, bs, sds, rds, tuple(mcc_entry_bytes), kind)
    blk, cache = read_mcc_block(ctx, bo, bs, sds, rds)
    toks = _tokens(ctx)
    tkey = (id(cache), key)
    nblk, nent, nsds, nrds = convert_block(kind, blk, entries, sds, rds, strict, getattr(ctx, "stats", None))
    if kind == 'bsp':
        _numbers(ctx).setdefault((sds, rds), set()).add((nsds, nrds))
    token = toks.get(tkey)
    if token is None:
        token = ctx.add_raw('geometry', nblk)
        toks[tkey] = token
        _stat(ctx, 'geometry_blocks')
        _stat(ctx, 'geometry_bytes', len(nblk))
    info = bytearray(INFO_SIZE)
    _INFO_HEAD.pack_into(info, 0, 0, len(nblk), nsds, nrds)
    info[0x18:0x24] = tail
    if owner_datum is not None:
        struct.pack_into('<I', info, 0x18, owner_datum)
    return bytes(info), [encode_entry(e) for e in nent], token


# ---------------------------------------------------------------------------------------------------------------------
# node handlers
# ---------------------------------------------------------------------------------------------------------------------
def _resources_field(node, info_off):
    for f in node.fields:
        if f.offset == info_off + 0x10 and f.type in ('tagblock', 'reflexive'):
            return f
    raise GeometryError('%s|%s has no Resources block at +0x%X' % (node.group, node.path, info_off + 0x10))


def _entry_bytes(node, f):
    return [bytes(k.data) for k in (node.blocks.get(f.offset) or [])]


def convert_site_node(ctx, mcc_node, vista_node, info_off, kind, mcc_info_off=None):
    """Node handler body: convert the geometry_block_info at mcc_info_off (default: info_off) of the MCC struct into the
    one at info_off of the Vista struct (idempotent).  Differing offsets only for ugh! Extra Info (language slots)."""
    from .. import tagtree as T
    moff = info_off if mcc_info_off is None else mcc_info_off
    f_m = _resources_field(mcc_node, moff)
    f_v = _resources_field(vista_node, info_off)
    mcc_info = bytes(mcc_node.data[moff:moff + INFO_SIZE])
    mentries = _entry_bytes(mcc_node, f_m)
    try:
        info, entries, token = convert_info(ctx, kind, mcc_info, mentries, strict=False)
    except GeometryError as ex:
        if not (getattr(ctx, 'opts', None) or {}).get('geometry_lenient'):
            raise GeometryError('%s|%s: %s' % (vista_node.group, vista_node.path, ex))
        # lenient mode: drop the geometry (an info without a block) and report it
        ctx.warn('geometry_block_dropped', '%s|%s: %s' % (vista_node.group, vista_node.path, ex))
        info = bytearray(mcc_info)
        _INFO_HEAD.pack_into(info, 0, -1, 0, 0, 0)
        info, entries, token = bytes(info), [], None
    # block numbers + untouched tail (owner datum is remapped by the generic converter; make sure of it)
    vista_node.data[info_off + 4:info_off + 0x10] = info[4:0x10]
    if token is None:
        # no block: the offset word keeps its MCC value (0 / -1) and nothing is patched
        vista_node.data[info_off:info_off + 4] = info[0:4]
        vista_node.patches.pop(info_off, None)
        if kind == 'bsp' and not entries and struct.unpack_from('<2I', info, 8)[0] and struct.unpack_from('<2I', info, 8)[1]:
            pend = getattr(ctx, '_geometry_pending', None)
            if pend is None:
                pend = []
                try:
                    ctx._geometry_pending = pend
                except Exception:
                    pass
            pend.append((vista_node, info_off, struct.unpack_from('<2I', mcc_info, 8)))
    else:
        vista_node.data[info_off:info_off + 4] = b'\0\0\0\0'
        vista_node.patches[info_off] = token
    owner = struct.unpack_from('<I', mcc_info, 0x18)[0]
    if owner not in (0, NO_BLOCK) and hasattr(ctx, 'remap_datum'):
        struct.pack_into('<I', vista_node.data, info_off + 0x18, ctx.remap_datum(owner))
    # Resources block: reuse the existing element Nodes where the count matches, else build new ones
    kids = vista_node.blocks.get(f_v.offset) or []
    if len(kids) != len(entries):
        es = T.element_size(f_v)
        kids = [T.Node(vista_node.group, T.child_path(vista_node.path, f_v), f_v.children, es) for _ in entries]
        vista_node.blocks[f_v.offset] = kids
    for k, e in zip(kids, entries):
        k.data[:] = e
    struct.pack_into('<i', vista_node.data, f_v.offset, len(entries))


@post_pass()
def geometry_post_pass(ctx, converted=None):
    """Replace the estimated size numbers of block-less infos (ltmp Clusters / Poop Definitions) by the exact numbers
    of the sbsp block they were copied from, when the MCC (sds, rds) pair maps to ONE Vista pair."""
    nums = getattr(ctx, '_geometry_numbers', None) or {}
    fixed = 0
    for node, off, (msds, mrds) in list(getattr(ctx, '_geometry_pending', None) or []):
        got = nums.get((msds, mrds))
        if got and len(got) == 1:
            nsds, nrds = next(iter(got))
            struct.pack_into('<II', node.data, off + 8, nsds, nrds)
            fixed += 1
    _stat(ctx, 'geometry_empty_numbers_fixed', fixed)
    try:
        ctx._geometry_pending = []
    except Exception:
        pass


def ugh_extra_info_node(ctx, mcc_node, vista_node):
    """ugh! 'Extra Info' element (MCC 0x14C with 8 per-language geometry_block_info slots -> Vista 0x2C with one slot at
    +0x08): keep the first non-empty slot (slot 0 = English).  The Encoded Permutation Section block is the sound
    converter's business."""
    pick = UGH_EXTRA_INFO_MCC_OFFSETS[0]
    for off in UGH_EXTRA_INFO_MCC_OFFSETS:
        bo, bs = struct.unpack_from('<iI', mcc_node.data, off)
        if bs and bo not in (0, NO_BLOCK):
            pick = off
            break
    convert_site_node(ctx, mcc_node, vista_node, UGH_EXTRA_INFO_VISTA_OFFSET, UGH_SITE.kind, mcc_info_off=pick)


def _make_handler(site):
    def handler(ctx, mcc_node, vista_node):
        convert_site_node(ctx, mcc_node, vista_node, site.info_off, site.kind)
    handler.__name__ = 'geometry_%s_%s' % (site.group.replace('*', 'x').replace('!', 'x'), site.label.replace(' ', '_').replace('/', '_'))
    return handler


for _s in SITES:
    node_handler(_s.group, _s.path)(_make_handler(_s))
# NOT registered as a node handler: the ugh! tag is built by the sound converter (sound_bundle.py), which copies the lip-sync
# blob verbatim (byte-identical, a 'plain' block is an identity conversion) and may find it in sounds_*.dat, not in the
# map.  ugh_extra_info_node() stays available for a pipeline that converts the Extra Info block through the generic pass.
