# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Geometry ('blkh' raw block) conversion Vista (v8) -> MCC (v13).  The inverse of
h2conv/handlers/geometry.py.

Every user of the 36-byte `geometry_block_info` struct (forward `geometry.SITES`, identical in both plugin sets) points at a
raw block outside the tag data:

    'blkh'  u32 resource_data_size  |  section data (sds)  |  resource data (rds)  |  'fklb'

The resource data is a list of payloads described by the 16-byte `geometry_block_resource` ("fixup") entries stored in the
tag (`Resources` block of the info struct).  Both games lay the payloads out with ONE rule ('crsr' marker before every
payload, payload start aligned to 1 << align_bits relative to the block start), so going from
Vista to MCC only needs:

  * the section data struct grows: render-model sections 0x70 -> 0x78 and sbsp cluster / instanced geometry / water
    sections 0x44 -> 0x48 (MCC turned a 4-byte pad into the 64-bit `Index Buffer Pointer`, plus an 8-byte tail pad in
    render-model sections).  The inserted bytes are ZERO (MCC cache files always carry 0 there - a runtime pointer);
    the Vista pad dwords that become the low half of the pointer are zeroed too;
  * the fixup entries' primary locators that lie after an inserted range move up by 4;
  * the resource data is laid out again with the common rule (the 16-aligned `Tag Data` MOPP payloads keep their
    absolute offset, so their padding SHRINKS);
  * the info numbers (Resource Block Size / Section Data Size / Resource Data Size) and the raw pointer are rewritten.
The vertex / index / part payload bytes are copied untouched (vertex declarations and strides are identical in both games).

Public API (pure, no ctx)
    convert_section_rev(kind, sd)                     Vista section struct -> MCC section struct
    convert_block_rev(kind, blk, entries, sds, rds)   Vista block -> (MCC block, MCC entries, MCC sds, MCC rds)
    estimate_empty_numbers_rev(kind, sds, rds, ents)  numbers of a block-less info (ltmp clusters / poop definitions)
Ctx API
    read_vista_block(ctx, ptr, size, sds, rds)        the Vista block behind a (location<<30 | offset) dword
    convert_info_rev(ctx, kind, info36, entries)      info struct + block -> MCC info bytes + raw token
    convert_site_node(ctx, vista_node, mcc_node, info_off, kind)   node handler body (registered for every SITE)
    ugh_extra_info_node(ctx, vista_node, mcc_node)    ugh! 'Extra Info' (1 Vista slot -> MCC slot 0 of 8); NOT registered
    geometry_post_pass(ctx, converted)                exact numbers for block-less ltmp infos
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import hashlib
import struct

from . import node_handler, post_pass
from ...handlers import geometry as G          # forward module: the pure layout functions are direction independent

GeometryError = G.GeometryError
Entry = G.Entry
decode_entry = G.decode_entry
encode_entry = G.encode_entry
check_block = G.check_block
split_block = G.split_block
layout_block = G.layout_block
verify_block = G.verify_block
SITES = G.SITES
site_for = G.site_for
UGH_SITE = G.UGH_SITE
INFO_SIZE = G.INFO_SIZE
NO_BLOCK = G.NO_BLOCK
RES_VERTEX_BUFFER = G.RES_VERTEX_BUFFER
MCC_ALIGN_HINT = 0x80                 # MCC raw table blocks are 0x80-aligned (the H2EK tool; stock MCC maps too)

_INFO_HEAD = G._INFO_HEAD
_LOC_FILE = {1: 'mainmenu', 2: 'shared', 3: 'single_player_shared'}

# kind -> (vista section size, mcc section size, ((offset in MCC space, length) ...))   (the forward G.SECTION_DROPS, reordered)
SECTION_GROWTH = {
    'mode': (0x70, 0x78, G.SECTION_DROPS['mode'][2]),
    'bsp': (0x44, 0x48, G.SECTION_DROPS['bsp'][2]),
    'plain': (None, None, ()),
}
# MCC section offsets whose bytes are runtime-only; the Vista bytes that land in the LOW half of the Index Buffer Pointer
# (Vista pad dword at +0x40) are cleared as well so that the pointer is 0 like in every MCC cache file.
RUNTIME_ZERO = {'mode': ((0x40, 4),), 'bsp': ((0x40, 4),), 'plain': ()}


# ---------------------------------------------------------------------------------------------------------------------
# pure block conversion
# ---------------------------------------------------------------------------------------------------------------------
def convert_section_rev(kind, sd, strict=False):
    """Vista section struct bytes -> MCC section struct bytes (zero bytes inserted at the MCC drop positions).
    Returns (bytes, inserts) with inserts = [(offset in MCC space, length)].  `strict` raises when a Vista pad dword that is
    being cleared (the low half of the Index Buffer Pointer) is not zero."""
    vsz, msz, drops = SECTION_GROWTH[kind]
    if not drops:
        return bytes(sd), ()
    if len(sd) != vsz:
        raise GeometryError('%s section data is 0x%X bytes, expected Vista size 0x%X' % (kind, len(sd), vsz))
    out = bytearray()
    pos = 0
    for o, n in drops:
        take = o - len(out)
        out += sd[pos:pos + take]
        pos += take
        out += bytes(n)
    out += sd[pos:]
    if len(out) != msz:
        raise GeometryError('internal: %s MCC section is 0x%X bytes, expected 0x%X' % (kind, len(out), msz))
    for o, n in RUNTIME_ZERO[kind]:
        if strict and any(out[o:o + n]):
            raise GeometryError('non-zero Vista pad %s at section +0x%X' % (bytes(out[o:o + n]).hex(), o))
        out[o:o + n] = bytes(n)
    return bytes(out), drops


def _shift_locator_up(loc, drops):
    """Vista locator (offset inside the section struct) -> MCC locator after the zero bytes were inserted."""
    m = loc
    for o, n in drops:
        if m >= o:
            m += n
    return m


def convert_block_rev(kind, blk, entries, sds, rds, strict=False, stats=None):
    """Convert one Vista raw block to the MCC block.

    kind    'mode' | 'bsp' | 'plain'
    blk     the Vista 'blkh'...'fklb' bytes;   entries  list of Entry (decode_entry of the Vista Resources block)
    sds/rds the Vista info numbers
    Returns (MCC block bytes, MCC Entry list, MCC sds, MCC rds)."""
    sd, pays = split_block(blk, sds, rds, entries)
    nsd, drops = convert_section_rev(kind, sd, strict)
    if drops:
        entries = [e._replace(primary=_shift_locator_up(e.primary, drops), dup=_shift_locator_up(e.dup, drops)) for e in entries]
    nblk, nent, nrds = layout_block(nsd, pays, entries)
    tail = _vista_tail(blk, sds, rds, entries)
    if tail:
        # a block that breaks the layout rule (1 of 54,934 infos of the 516 community maps: an all-zero sbsp water definition with
        # no Resources but 0x1148C bytes of zero resource data) keeps the bytes the entries do not account for
        nrds += len(tail)
        grown = bytearray(nblk[:-4] + tail + G.FOOT)
        struct.pack_into('<I', grown, 4, nrds)
        nblk = bytes(grown)
        if stats is not None:
            stats['geometry_rev_tail_kept'] += 1
    if stats is not None:
        stats['geometry_rev_blocks'] += 1
    return nblk, nent, len(nsd), nrds


def _vista_tail(blk, sds, rds, entries):
    """Bytes of the Vista resource data behind the layout rule's end (align4 of the last payload end); b'' for every conforming block."""
    last = max((e.offset + e.size for e in entries), default=0)
    rule = (last + 3) & ~3 if entries else 0
    if rds > rule:
        return bytes(blk[8 + sds + rule:8 + sds + rds])
    return b''


def estimate_empty_numbers_rev(kind, sds, rds, entries):
    """Size numbers of an info that has no block (ltmp 'Lightmap Groups / Clusters' and 'Poop Definitions': copies of the numbers of
    the sbsp Clusters / Instanced Geometry Definitions).  sds grows by the 4 pointer bytes; rds is the common case (MCC = Vista - 4,
    80 % of the sbsp blocks).  The post pass replaces the estimate by the exact value of the sbsp block with the same Vista numbers when
    that is unambiguous.  Returns (sds, rds, entries)."""
    vsz, msz, drops = SECTION_GROWTH[kind]
    if not drops or sds != vsz:
        return sds, rds, entries
    if entries:                                   # fixup list but no block: re-lay it out
        ents = [e._replace(primary=_shift_locator_up(e.primary, drops), dup=_shift_locator_up(e.dup, drops)) for e in entries]
        base = 8 + msz
        cur = base
        new = []
        for e in ents:
            al = 1 << e.align_bits
            start = (cur + 4 + al - 1) & ~(al - 1)
            new.append(e._replace(offset=start - base))
            cur = start + e.size
        return msz, ((cur + 3) & ~3) - base, new
    if kind == 'bsp' and rds >= 4:
        return msz, rds - 4, entries
    return msz, rds, entries


# ---------------------------------------------------------------------------------------------------------------------
# reading the Vista block from the right file
# ---------------------------------------------------------------------------------------------------------------------
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
    cur = None
    try:
        cur = ctx.cur_cache
    except Exception:                                       # noqa: BLE001
        cur = None
    if loc == 0:
        if fresh(cur):
            yield cur
        if fresh(ctx.src):
            yield ctx.src
    else:
        c = ref(_LOC_FILE.get(loc))
        if fresh(c):
            yield c
        if fresh(cur):
            yield cur
        if fresh(ctx.src):
            yield ctx.src


def read_vista_block(ctx, ptr, size, sds, rds):
    """Raw bytes of the Vista block: ptr = (location << 30) | file offset (location 0 = the map the tag comes from, 1 mainmenu.map,
    2 shared.map, 3 single_player_shared.map).  The file is chosen by location and validated by framing + sizes."""
    ptr &= 0xFFFFFFFF
    loc, off = ptr >> 30, ptr & 0x3FFFFFFF
    last = None
    for c in _candidate_caches(ctx, loc):
        try:
            blk = c.raw(off, size)
            check_block(blk, sds, rds, size)
            return blk, c
        except Exception as ex:                             # noqa: BLE001
            last = ex
    raise GeometryError('Vista geometry block @0x%08X size 0x%X not found in any candidate map (%s)' % (ptr, size, last))


# ---------------------------------------------------------------------------------------------------------------------
# info struct conversion
# ---------------------------------------------------------------------------------------------------------------------
def _store(ctx, name):
    d = getattr(ctx, name, None)
    if d is None:
        d = {}
        try:
            setattr(ctx, name, d)
        except Exception:                                   # noqa: BLE001
            pass
    return d


def _stat(ctx, key, n=1):
    st = getattr(ctx, 'stats', None)
    if st is not None:
        st[key] += n


def _add_raw(ctx, block):
    try:
        return ctx.add_raw('geometry', block, MCC_ALIGN_HINT)
    except TypeError:
        return ctx.add_raw('geometry', block)


def convert_info_rev(ctx, kind, vista_info36, vista_entry_bytes, owner_datum=None, strict=False):
    """Convert one geometry_block_info.

    vista_info36       the 36 info bytes of the Vista struct;  vista_entry_bytes  list of the Vista `Resources` elements (16 B each)
    Returns (mcc_info36 with the block offset word left 0, [16-byte MCC entries], token or None).
    An info WITHOUT a block (offset 0 / -1, size 0) returns token None and keeps the offset word: only the size numbers are converted."""
    bo, bs, sds, rds = _INFO_HEAD.unpack_from(vista_info36, 0)
    tail = bytes(vista_info36[0x18:0x24])
    entries = [decode_entry(e) for e in vista_entry_bytes]
    if bs == 0 or bo in (0, NO_BLOCK, -1):
        nsds, nrds, nent = estimate_empty_numbers_rev(kind, sds, rds, entries)
        info = bytearray(vista_info36)
        _INFO_HEAD.pack_into(info, 0, bo, bs, nsds, nrds)
        return bytes(info), [encode_entry(e) for e in nent], None
    blk, cache = read_vista_block(ctx, bo, bs, sds, rds)
    nblk, nent, nsds, nrds = convert_block_rev(kind, blk, entries, sds, rds, strict, getattr(ctx, 'stats', None))
    if kind == 'bsp':
        _store(ctx, '_geometry_rev_numbers').setdefault((sds, rds), set()).add((nsds, nrds))
    toks = _store(ctx, '_geometry_rev_tokens')
    tkey = (id(cache), bo, bs, tuple(vista_entry_bytes), kind)
    token = toks.get(tkey)
    if token is None:
        # blocks that are byte-identical after conversion are stored once (the Vista maps share blocks between infos, 4,604 of 33,078)
        bkey = ('blk', hashlib.sha1(nblk).digest(), len(nblk))
        token = toks.get(bkey)
        if token is None:
            token = _add_raw(ctx, nblk)
            toks[bkey] = token
            _stat(ctx, 'geometry_blocks')
            _stat(ctx, 'geometry_bytes', len(nblk))
        toks[tkey] = token
    info = bytearray(INFO_SIZE)
    _INFO_HEAD.pack_into(info, 0, 0, len(nblk), nsds, nrds)
    info[0x18:0x24] = tail
    if owner_datum is not None:
        struct.pack_into('<I', info, 0x18, owner_datum)
    return bytes(info), [encode_entry(e) for e in nent], token


# ---------------------------------------------------------------------------------------------------------------------
# node handlers  (reverse registry: fn(ctx, vista_node, mcc_node))
# ---------------------------------------------------------------------------------------------------------------------
def _resources_field(node, info_off):
    for f in node.fields:
        if f.offset == info_off + 0x10 and f.type in ('tagblock', 'reflexive'):
            return f
    raise GeometryError('%s|%s has no Resources block at +0x%X' % (node.group, node.path, info_off + 0x10))


def _entry_bytes(node, f):
    return [bytes(k.data) for k in (node.blocks.get(f.offset) or [])]


def convert_site_node(ctx, vista_node, mcc_node, info_off, kind, vista_info_off=None):
    """Node handler body: convert the geometry_block_info at vista_info_off (default: info_off) of the Vista struct into the one at
    info_off of the MCC struct (idempotent)."""
    from ... import tagtree as T
    voff = info_off if vista_info_off is None else vista_info_off
    f_v = _resources_field(vista_node, voff)
    f_m = _resources_field(mcc_node, info_off)
    v_info = bytes(vista_node.data[voff:voff + INFO_SIZE])
    ventries = _entry_bytes(vista_node, f_v)
    try:
        info, entries, token = convert_info_rev(ctx, kind, v_info, ventries, strict=False)
    except GeometryError as ex:
        if not (getattr(ctx, 'opts', None) or {}).get('geometry_lenient'):
            raise GeometryError('%s|%s: %s' % (mcc_node.group, mcc_node.path, ex))
        ctx.warn('geometry_block_dropped', '%s|%s: %s' % (mcc_node.group, mcc_node.path, ex))
        info = bytearray(v_info)
        _INFO_HEAD.pack_into(info, 0, -1, 0, 0, 0)
        info, entries, token = bytes(info), [], None
    mcc_node.data[info_off + 4:info_off + 0x10] = info[4:0x10]
    if token is None:
        mcc_node.data[info_off:info_off + 4] = info[0:4]
        mcc_node.patches.pop(info_off, None)
        sd, rd = struct.unpack_from('<2I', info, 8)
        if kind == 'bsp' and not entries and sd and rd:
            _store_list(ctx, '_geometry_rev_pending').append((mcc_node, info_off, struct.unpack_from('<2I', v_info, 8)))
    else:
        mcc_node.data[info_off:info_off + 4] = b'\0\0\0\0'
        mcc_node.patches[info_off] = token
    owner = struct.unpack_from('<I', v_info, 0x18)[0]
    if owner not in (0, NO_BLOCK) and hasattr(ctx, 'remap_datum'):
        struct.pack_into('<I', mcc_node.data, info_off + 0x18, ctx.remap_datum(owner))
    kids = mcc_node.blocks.get(f_m.offset) or []
    if len(kids) != len(entries):
        es = T.element_size(f_m)
        kids = [T.Node(mcc_node.group, T.child_path(mcc_node.path, f_m), f_m.children, es) for _ in entries]
        mcc_node.blocks[f_m.offset] = kids
    for k, e in zip(kids, entries):
        k.data[:] = e
    struct.pack_into('<i', mcc_node.data, f_m.offset, len(entries))


def _store_list(ctx, name):
    d = getattr(ctx, name, None)
    if d is None:
        d = []
        try:
            setattr(ctx, name, d)
        except Exception:                                   # noqa: BLE001
            pass
    return d


@post_pass()
def geometry_post_pass(ctx, converted=None):
    """Replace the estimated size numbers of block-less infos (ltmp Clusters / Poop Definitions) by the exact numbers of the sbsp
    block they were copied from, when the Vista (sds, rds) pair maps to ONE MCC pair."""
    nums = getattr(ctx, '_geometry_rev_numbers', None) or {}
    fixed = 0
    for node, off, (vsds, vrds) in list(getattr(ctx, '_geometry_rev_pending', None) or []):
        got = nums.get((vsds, vrds))
        if got and len(got) == 1:
            nsds, nrds = next(iter(got))
            struct.pack_into('<II', node.data, off + 8, nsds, nrds)
            fixed += 1
    _stat(ctx, 'geometry_empty_numbers_fixed', fixed)
    try:
        ctx._geometry_rev_pending = []
    except Exception:                                       # noqa: BLE001
        pass


def ugh_extra_info_node(ctx, vista_node, mcc_node):
    """ugh! 'Extra Info' element (Vista 0x2C with ONE geometry_block_info at +0x08 -> MCC 0x14C with 8 per-language slots at
    +0x08 + 0x24 * k).  The H2EK tool populates slot 0 (English) only and leaves the other seven all-zero (23/23 and 26/26 elements of
    the two self-contained maps probed), so the converted block goes to slot 0.  The Encoded Permutation Section block is the sound
    converter's business; the ugh! tag itself is built by the sound handler, which may call this for the geometry part."""
    convert_site_node(ctx, vista_node, mcc_node, G.UGH_EXTRA_INFO_MCC_OFFSETS[0], UGH_SITE.kind,
                      vista_info_off=G.UGH_EXTRA_INFO_VISTA_OFFSET)


def _make_handler(site):
    def handler(ctx, vista_node, mcc_node):
        convert_site_node(ctx, vista_node, mcc_node, site.info_off, site.kind)
    handler.__name__ = 'rev_geometry_%s_%s' % (site.group.replace('*', 'x').replace('!', 'x'),
                                              site.label.replace(' ', '_').replace('/', '_'))
    return handler


for _s in SITES:
    node_handler(_s.group, _s.path)(_make_handler(_s))
# ugh! is NOT registered: the sound converter builds the tag (see ugh_extra_info_node).
