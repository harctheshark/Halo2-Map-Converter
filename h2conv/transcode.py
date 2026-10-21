# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Generic MCC -> Vista tag transcoder + the Ctx object every handler codes against.

Public API
----------
Ctx(src, mcc_shared, vista, plan, strings, opts=None, ...)   the conversion context (ARCHITECTURE section 5)
convert_node(ctx, mcc_node, vista_fields, vista_size, vista_path, ...) -> tagtree.Node
        generic, layout-driven conversion of ONE struct and (recursively) all its blocks/datarefs; then runs the
        registered node handlers (handlers.node_handlers(group, vista_path)).
convert_tag(ctx, entry) -> tagtree.Node          tag handler if registered else decode + convert_node
convert_mcc_tag(ctx, mcc_tag, cache=None) -> Node generic conversion of one MCC tag (no tag handler) - used by tests
empty_tag_node(ctx, group) -> Node               zero-filled Vista struct for MCC "empty" (size 0) tags
default_enum_policy() / reload_enum_policy()     h2conv/data/enum_policy.json loader

How a pair of structs is converted
----------------------------------------------------------------
The MCC and Vista field lists are paired with plugins.diff_fields (+ plugins.SEMANTIC_PAIRS) and turned into a
plan by plugins.make_plan.  The plan is compiled ONCE per (id(mcc_fields), id(vista_fields), group, path) into a flat
op list:   raw byte copies | tagref/tag-datum remap | stringid remap | dataref copy | tagblock recursion |
           numeric resize | enum/flag option clean-up | null-fill of Vista-only tagrefs.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import collections
import json
import os
import re
import struct
import time

from . import paths as _paths
from . import plugins as P
from . import tagtree as T
from . import handlers as H

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NULL = 0xFFFFFFFF
MCC_DECOMP = os.path.join(_paths.work_dir(), 'mcc_decompressed')
ENUM_POLICY_PATH = os.path.join(_paths.data_dir(), 'enum_policy.json')
ALIGN_VISTA_PATH = os.path.join(_paths.data_dir(), 'block_alignment_vista.json')


def __getattr__(name):
    """VISTA_MAPS_DEFAULT / MCC_STOCK_DIR: folders on one PC, now resolved by h2conv.paths; kept as attributes for the developer tools."""
    if name == 'VISTA_MAPS_DEFAULT':
        return _paths.vista_maps_dir()
    if name == 'MCC_STOCK_DIR':
        return _paths.mcc_maps_dir()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))

_U32 = struct.Struct('<I')
_U32x2 = struct.Struct('<II')
_INT_TYPES = frozenset(['int8', 'int16', 'int32', 'uint8', 'uint16', 'uint32', 'uint64', 'enum8', 'enum16', 'enum32',
                        'flags8', 'flags16', 'flags32', 'stringid', 'datum', 'undefined'])
_SIGNED = frozenset(['int8', 'int16', 'int32'])


# ================================================================================================
# enum / flags policy  (h2conv/data/enum_policy.json + built-ins)
# ================================================================================================
_POLICY = None

# Built-in fallback policy.  Keys are (group | '*', block path | '*', lower-case field name [+ '@<mcc offset>']).
# h2conv/data/enum_policy.json is authoritative: its entries override these per key.
# Evidence for the built-ins: the cross-map comparison of the stock MCC / Vista map pairs.
_DMG_MAP = {'value_map': {42: 36, 43: 36}}           # 'Warthog Gunner Gauss/Rocket' -> Vista's 'Warthog Gunner' (proj 42->36 x7)
BUILTIN_POLICY = {
    ('*', '*', 'damage reporting type'): _DMG_MAP,
    ('*', '*', 'melee damage reporting type'): _DMG_MAP,
    ('*', '*', 'damage effect reporting type'): _DMG_MAP,
    ('*', '*', 'collision damage reporting type'): _DMG_MAP,
    ('*', '*', 'response damage reporting type'): _DMG_MAP,
    # jpt! Flags@0xC: MCC data carries a bit-2 flag that neither plugin defines; Vista has it clear in 1422/1422 pairs
    ('jpt!', '', 'flags'): {'clear_bits': [2], '_only_offset': 0x0C},
}


def reload_enum_policy(path=ENUM_POLICY_PATH):
    """Load h2conv/data/enum_policy.json: {group: {block path: {field name: spec}}} (field key 'name' or 'name@<mcc offset>').
    spec keys (all optional; a spec is the COMPLETE decision for that field - it replaces the default behaviour, which is
    "clear the MCC-only flag bits / map MCC-only enum values to 0"):
         clear_bits  [bit, ...]       flag bits to clear
         clear_mask  int              same as an OR-ed mask
         move_bits   {from: to}       flag bits that move (read from the ORIGINAL value, applied after clearing)
         set_bits    [bit, ...]       bits to force on
         value_map   {mcc: vista}     enum value translation
         default     int              value for MCC-only enum values that are not in value_map (default 0)
         keep        true             leave the field untouched
       Keys starting with '_' are comments.  Block path = tagtree path ('' main struct, '/Block/Sub'); group and path
       may be '*' in the built-ins.  Unknown keys (type, note, usage_*, mcc_offset, vista_offset) are ignored."""
    global _POLICY
    pol = dict(BUILTIN_POLICY)
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                raw = json.load(fh)
            for g, blocks in raw.items():
                if g.startswith('_') or not isinstance(blocks, dict):
                    continue
                for bp, fields in blocks.items():
                    if bp.startswith('_') or not isinstance(fields, dict):
                        continue
                    for fname, spec in fields.items():
                        if fname.startswith('_') or not isinstance(spec, dict):
                            continue
                        pol[(g, bp, fname.lower())] = spec
        except Exception as ex:  # a broken policy file must not break conversion
            print('WARNING: could not read %s: %s' % (path, ex))
    _POLICY = pol
    return pol


def default_enum_policy():
    if _POLICY is None:
        reload_enum_policy()
    return _POLICY


def _policy_for(group, vpath, fname, mcc_offset=None, vista_offset=None, dpath=None):
    pol = default_enum_policy()
    if not pol:
        return None
    names = []
    if mcc_offset is not None:
        names.append('%s@%d' % (fname.lower(), mcc_offset))
    names.append(fname.lower())
    for key in names:
        # h2conv/data/enum_policy.json keys duplicate sibling blocks as '/Settings#1' (the MCC diff path = dpath)
        cands = [(group, bp, key) for bp in ((dpath,) if dpath and dpath != vpath else ()) + (vpath, vpath.lstrip('/'), vpath.rstrip('/'))]
        cands += [(group, '*', key), ('*', vpath, key), ('*', '*', key)]
        for k in cands:
            s = pol.get(k)
            if s is not None:
                if vista_offset is not None and '_only_offset' in s and s['_only_offset'] != vista_offset:
                    continue
                return s
    return None


# ================================================================================================
# pair plans
# ================================================================================================
# op codes
OP_REF = 1       # (op, mo, vo, msz, vsz, name, null_word) tagref / 4-byte datum-only tagref
OP_SID = 2       # (op, mo, vo)
OP_DATA = 3      # (op, mo, vo)
OP_BLOCK = 4     # (op, mo, vo, mf, vf, dname)             dname = MCC block name with '#k' duplicate suffix
OP_COPY = 5      # (op, mo, vo, n)                         extra raw copy (unic ranges, 'datum' ids)
OP_RESIZE = 6    # (op, mo, vo, msz, vsz, signed, name)
OP_ENUM = 7      # (op, mo, vo, size, is_flags, clear_mask, set_mask, value_map, default, only_values, name, moves)
OP_NULLFILL = 8  # (op, vo, size)                          Vista-only tagref: write a null reference
OP_CSTR = 9      # (op, vo, size, wide)                    string field: zero everything after the terminating NUL


class Pair(object):
    __slots__ = ('raw', 'ops', 'identity', 'msize', 'vsize', 'group', 'vpath', 'dpath', 'notes', 'has_children')


_PAIR_CACHE = {}
_PAIR_KEEP = []     # keep the field lists alive so id() keys stay unique

# Group word of a NULL 8-byte tag reference.  The engine only tests the datum; the word is cosmetic.
# Over the 43-map cross-map oracle 182,319 of 185,063 null refs of non-specialist groups carry the same word in both games, so the
# MCC word is copied.  The few FIELDS where the two tools differ systematically are listed here (word Vista writes; evidence
# = pairs that follow the rule / pairs of that MCC word):
def _fcc(text):
    return struct.unpack('<I', text.encode('latin-1')[::-1])[0]


NULL_REF_WORD = {
    ('sky ', '/Cube Map', 'Cube Map Reference'): _fcc('bitm'),      # 84/84 (MCC writes -1)
    ('hudg', '', 'Item Message Text'): _fcc('unic'),                # 18/18
    ('hudg', '', 'Alternate Icon Text'): _fcc('unic'),              # 18/18
    ('effe', '', 'Looping Sound'): _fcc('lsnd'),                    # 335/364 of the MCC -1 words
    ('foot', '/Effects/Sounds', 'Secondary Tag'): 0xFFFFFFFF,       # 2240/2240 of the MCC 'snd!' words (Vista writes -1)
    ('PRTM', '', 'Collision Effect'): 0xFFFFFFFF,                   # 12/12
}


def get_pair(ctx, mfields, vfields, msize, vsize, group, dpath, vpath):
    key = (id(mfields), id(vfields), group, dpath, vpath)
    pr = _PAIR_CACHE.get(key)
    if pr is None:
        pr = _build_pair(mfields, vfields, msize, vsize, group, dpath, vpath)
        _PAIR_CACHE[key] = pr
        _PAIR_KEEP.append((mfields, vfields))
    return pr


def _build_pair(mfields, vfields, msize, vsize, group, dpath, vpath):
    ents = P.diff_fields(mfields, vfields)
    for (sg, sp, smn, svn, snote) in P.SEMANTIC_PAIRS:
        if sg == group and sp == dpath:
            ents = P._apply_semantic(ents, smn, svn, snote)
    plan = P.make_plan(ents)
    mby = {}
    for f in mfields:
        mby.setdefault(f.offset, f)
    vby = {}
    for f in vfields:
        vby.setdefault(f.offset, f)
    pr = Pair()
    pr.msize, pr.vsize, pr.group, pr.vpath, pr.dpath = msize, vsize, group, vpath, dpath
    pr.notes = []
    ops = []
    # ---- raw runs ----
    pr.raw = [tuple(r) for r in plan['raw']]
    # ---- fixups ----
    seen_block = {}
    for e in ents:
        m, v = e['_m'], e['_v']
        if m is None or v is None:
            continue
        if m.children is not None and v.children is not None:
            k = seen_block.get(m.name, 0)
            seen_block[m.name] = k + 1
            ops.append((OP_BLOCK, m.offset, v.offset, m, v, m.name + ('#%d' % k if k else '')))
    for fx in plan['fixup']:
        mf = mby.get(fx['mcc_offset'])
        vf = vby.get(fx['vista_offset'])
        if mf is None or vf is None:
            continue
        mt, vt = mf.type, vf.type
        if mt == 'tagblock' and vt == 'tagblock':
            continue                                    # handled above (needs the duplicate-name counter)
        if mt == 'dataref' and vt == 'dataref':
            ops.append((OP_DATA, mf.offset, vf.offset))
        elif mt == 'stringid' and vt == 'stringid':
            ops.append((OP_SID, mf.offset, vf.offset))
        elif mt == 'unicList' and vt == 'unicList':
            ops.append((OP_COPY, mf.offset, vf.offset, min(mf.size, vf.size)))
        elif mt == 'datum' and vt == 'datum':
            # 'datum' fields are object Unique IDs / script-expression datums, NOT tag datums
            ops.append((OP_COPY, mf.offset, vf.offset, min(mf.size, vf.size)))
        elif mt in ('tagref', 'datum') and vt in ('tagref', 'datum'):
            ops.append((OP_REF, mf.offset, vf.offset, mf.size, vf.size, mf.name,
                        NULL_REF_WORD.get((group, vpath, vf.name)) if vf.size == 8 else None))
        elif mf.size != vf.size or mt in _INT_TYPES:
            ops.append((OP_RESIZE, mf.offset, vf.offset, mf.size, vf.size, mt in _SIGNED, mf.name))
        else:
            pr.notes.append('unhandled fixup %s->%s %s' % (mt, vt, mf.name))
            ops.append((OP_COPY, mf.offset, vf.offset, min(mf.size, vf.size)))
    # ---- enum / flags ----
    for e in ents:
        mf, vf = e['_m'], e['_v']
        if mf is None or vf is None or mf.size != vf.size:
            continue
        if not (mf.type.startswith(('flags', 'enum')) and vf.type[:5] == mf.type[:5]):
            continue
        is_flags = mf.type.startswith('flags')
        pol = _policy_for(group, vpath, mf.name, mf.offset, vf.offset, dpath)
        if pol is not None and pol.get('keep'):
            continue
        clear = setm = 0
        moves = None
        vmap = None
        default = 0
        only_vals = set()
        if pol is None:
            # default: clear the flag bits / zero the enum values that exist only in the MCC option list
            if is_flags:
                for _n, bit in e.get('options_only_mcc', ()):
                    clear |= 1 << bit
            else:
                only_vals = set(val for _n, val in e.get('options_only_mcc', ()))
        else:
            for b_ in pol.get('clear_bits', ()):
                clear |= 1 << int(b_)
            if pol.get('clear_mask'):
                clear |= int(pol['clear_mask'])
            for b_ in pol.get('set_bits', ()):
                setm |= 1 << int(b_)
            if pol.get('move_bits'):
                moves = tuple((int(k), int(v)) for k, v in pol['move_bits'].items())
            if pol.get('value_map'):
                vmap = {int(k): int(v) for k, v in pol['value_map'].items()}
                only_vals |= set(vmap)
            default = int(pol.get('default', 0))
            if not is_flags:
                only_vals |= set(val for _n, val in e.get('options_only_mcc', ()))     # unmapped MCC-only values -> default
        if is_flags and not clear and not setm and not moves:
            continue
        if not is_flags and not only_vals:
            continue
        ops.append((OP_ENUM, mf.offset, vf.offset, mf.size, is_flags, clear, setm, vmap, default, only_vals, mf.name, moves))
    # ---- fixed-size strings: MCC pads with 0xFE junk after the NUL, Vista with zeros ----
    for e in ents:
        mf, vf = e['_m'], e['_v']
        if mf is None or vf is None or mf.size != vf.size:
            continue
        if mf.type in ('ascii', 'utf16') and vf.type == mf.type:
            ops.append((OP_CSTR, vf.offset, vf.size, mf.type == 'utf16'))
    # ---- Vista-only null references ----
    for off, size, name, typ in plan['vista_fill']:
        if typ == 'tagref':
            ops.append((OP_NULLFILL, off, size))
    pr.ops = ops
    pr.has_children = any(o[0] in (OP_BLOCK, OP_DATA) for o in ops)
    pr.identity = (msize == vsize and not ops and len(pr.raw) == 1 and pr.raw[0] == (0, 0, msize))
    return pr


# ================================================================================================
# generic conversion
# ================================================================================================
def _int_resize(md, mo, vd, vo, msz, vsz, signed):
    if msz == vsz:
        vd[vo:vo + vsz] = md[mo:mo + msz]
    elif msz > vsz:
        vd[vo:vo + vsz] = md[mo:mo + vsz]                 # low bytes (little endian)
    else:
        vd[vo:vo + msz] = md[mo:mo + msz]
        if signed and md[mo + msz - 1] & 0x80:
            vd[vo + msz:vo + vsz] = b'\xff' * (vsz - msz)


def _clean_cstr(vd, off, size, wide):
    """Fixed-size string field: bytes after the terminating NUL that are 0xFE (the MSVC debug 'uninitialised heap' fill
    the MCC tool leaves there, e.g. hudg 'Waypoint Arrows Name') become 0.  Any other leftover (stale text from the
    tool's string buffer, e.g. styl 'blind_panic\\0-----') is copied unchanged: Vista carries the very same leftovers
    (styl 369/369 and colo 72/72 pairs are byte-identical only if they are kept)."""
    end = off + size
    if not wide:
        i = vd.find(0, off, end)
        if i >= 0 and i + 1 < end and b'\xfe' in vd[i + 1:end]:
            seg = bytes(vd[i + 1:end])
            vd[i + 1:end] = seg.replace(b'\xfe', b'\x00')
    else:
        for i in range(off, end - 1, 2):
            if vd[i] == 0 and vd[i + 1] == 0:
                for j in range(i + 2, end - 1, 2):
                    if vd[j] == 0xFE and vd[j + 1] == 0xFE:
                        vd[j] = vd[j + 1] = 0
                break


def _conv_ref(ctx, md, vd, mo, vo, msz, vsz, name, null_word, node):
    if msz == 8:
        g, d = _U32x2.unpack_from(md, mo)
    else:
        g = None
        d = _U32.unpack_from(md, mo)[0]
    if d == NULL or (d == 0 and msz == 4) or (d == 0 and g == 0):
        nd = d
    else:
        nd = ctx.remap_datum(d)
        ctx.stats['tagref_remapped'] += 1
        if nd == NULL:
            ctx.stats['tagref_dropped'] += 1
            ctx.note_dropped_ref(node, name, d)
    if vsz == 8:
        if g is None:
            g = ctx.mcc_group_word(d) if d not in (NULL, 0) else NULL
        elif g == NULL and d not in (NULL, 0):
            g = ctx.mcc_group_word(d)
        if nd == NULL and null_word is not None:
            g = null_word
        _U32x2.pack_into(vd, vo, g, nd)
    else:
        _U32.pack_into(vd, vo, nd)


def _conv_enum(ctx, md, vd, op, node):
    (_o, mo, vo, size, is_flags, clear, setm, vmap, default, only_vals, name, moves) = op
    val = int.from_bytes(md[mo:mo + size], 'little')
    if is_flags:
        orig = val
        if moves:
            for src, dst in moves:
                clear |= 1 << src
        val = (val & ~clear) | setm
        if moves:
            for src, dst in moves:
                if (orig >> src) & 1:
                    val |= 1 << dst
        if val != orig:
            ctx.stats['flag_bits_changed'] += 1
    else:
        if val in only_vals:
            val = vmap[val] if (vmap is not None and val in vmap) else default
            ctx.stats['enum_value_remapped'] += 1
    vd[vo:vo + size] = (val & ((1 << (8 * size)) - 1)).to_bytes(size, 'little')


def convert_node(ctx, mnode, vfields, vsize, vpath, group=None, dpath=None, _memo=None, _handlers=True):
    """Convert one MCC struct (Node) to a Vista Node with layout (vfields, vsize) at tagtree path `vpath`."""
    group = group or mnode.group
    if dpath is None:
        dpath = vpath
    if _memo is None:
        _memo = {'__handlers__': _handlers}
    msize = len(mnode.data)
    pr = get_pair(ctx, mnode.fields, vfields, msize, vsize, group, dpath, vpath)
    md = mnode.data
    if pr.identity:
        vnode = T.Node(group, vpath, vfields, vsize, md)
    else:
        vnode = T.Node(group, vpath, vfields, vsize)
        vd = vnode.data
        for mo, vo, n in pr.raw:
            vd[vo:vo + n] = md[mo:mo + n]
        if pr.ops:
            vd = vnode.data
            for op in pr.ops:
                c = op[0]
                if c == OP_REF:
                    _conv_ref(ctx, md, vd, op[1], op[2], op[3], op[4], op[5], op[6], mnode)
                elif c == OP_SID:
                    mo, vo = op[1], op[2]
                    s = _U32.unpack_from(md, mo)[0]
                    if s != 0 and s != NULL:
                        try:
                            s = ctx.remap_sid(s)
                        except Exception as ex:             # junk id in an unused/opaque MCC field
                            ctx.warn('bad_stringid', '%s|%s 0x%X: %s' % (group, vpath, s, ex))
                            s = 0
                    _U32.pack_into(vd, vo, s)
                elif c == OP_DATA:
                    payload = mnode.datas.get(op[1]) or b''
                    vnode.datas[op[2]] = payload
                    _U32.pack_into(vd, op[2], len(payload))      # size word (the encoder rewrites the address word)
                elif c == OP_BLOCK:
                    _conv_block(ctx, mnode, vnode, op, vpath, dpath, group, _memo)
                elif c == OP_COPY:
                    vd[op[2]:op[2] + op[3]] = md[op[1]:op[1] + op[3]]
                elif c == OP_RESIZE:
                    _int_resize(md, op[1], vd, op[2], op[3], op[4], op[5])
                    rk = 'resize:%s|%s|%s' % (group, vpath, op[6])
                    ctx.stats[rk] += 1
                    if ctx.stats[rk] == 1 and not (_handlers and any(getattr(fn, 'owns_resize', False)
                                                                     for fn in H.node_handlers(group, vpath))):
                        ctx.warn('resize_default', '%s|%s %s: %d -> %d bytes, low bytes kept (add a handler if the value matters)' % (
                            group, vpath or '/', op[6], op[3], op[4]))
                elif c == OP_ENUM:
                    _conv_enum(ctx, md, vd, op, mnode)
                elif c == OP_CSTR:
                    _clean_cstr(vd, op[1], op[2], op[3])
                elif c == OP_NULLFILL:
                    vd[op[1]:op[1] + op[2]] = b'\xff' * op[2]
    bf = BUILTIN_FIXES.get((group, vpath))
    if bf is not None:
        for fn in bf:
            fn(ctx, mnode, vnode)
    if _handlers:
        hs = H.node_handlers(group, vpath)
        if hs:
            for fn in hs:
                fn(ctx, mnode, vnode)
    so = _sort_offsets(vfields, group, vpath)
    if so:
        _sort_blocks(ctx, vnode, so, group, vpath)
    return vnode


# ------------------------------------------------------------------------------------------------
# blocks the ENGINE binary-searches
# ------------------------------------------------------------------------------------------------
# halo2.exe sub_4F5B8F is a signed 32-bit binary search over a tag block on the FIRST DWORD of its elements (16 call sites in the
# animation accessors).  The Vista tool keeps such blocks sorted ascending by that dword (a string_id label); the Assembly plugin
# marks them sort="true".  MCC sorts them by ITS string ids; after the by-text remap to Vista's string table the numeric order
# changes, so the converted blocks must be re-sorted or labels are not found (emulated: 135 of 5,037 lookups missed in the custom
# map's masterchief / dervish / elite graphs, 0 of 6,183 in stock shared.map).  The exe also searches two jmad blocks the plugin does
# not flag (sub_4F5F25 / sub_4F5F3B); they are sorted in every stock tag with >= 2 elements.  Children travel with their element, the
# sort is STABLE (equal labels = animation variants keep their MCC order) and keys compare as signed int32 like the engine's `cmp`.
ENGINE_SORTED_BLOCKS = frozenset([('jmad', '', 'Vehicle Suspension'), ('jmad', '', 'Object Overlays')])
_I32 = struct.Struct('<i')
_SORT_CACHE = {}


def _sort_offsets(vfields, group, vpath):
    """Offsets of the tagblock fields of this struct that must stay sorted by their first dword (cached per layout path)."""
    key = (id(vfields), group, vpath)
    r = _SORT_CACHE.get(key)
    if r is None:
        r = tuple(f.offset for f in vfields if f.type in T.BLOCK_TYPES and (
            str(f.attrs.get('sort', '')).lower() == 'true' or (group, vpath, f.name) in ENGINE_SORTED_BLOCKS))
        _SORT_CACHE[key] = r
        _PAIR_KEEP.append(vfields)                       # id() keys must stay unique
    return r


def sort_block_elements(kids):
    """The elements ordered ascending by the signed int32 in their first dword (stable).  Returns `kids` itself when already sorted."""
    keys = [_I32.unpack_from(k.data, 0)[0] for k in kids]
    if all(keys[i] <= keys[i + 1] for i in range(len(keys) - 1)):
        return kids
    return [kids[i] for i in sorted(range(len(kids)), key=keys.__getitem__)]


def _sort_blocks(ctx, vnode, offsets, group, vpath):
    for off in offsets:
        kids = vnode.blocks.get(off)
        if not kids or len(kids) < 2:
            continue
        out = sort_block_elements(kids)
        if out is not kids:
            vnode.blocks[off] = out
            ctx.stats['sorted_blocks'] += 1
            ctx.stats['sorted_block_elements'] += len(out)


def _element_is_empty(k):
    return not any(k.data) and not any(k.blocks.values()) and not any(k.datas.values())


def _fix_prt3_postprocess(ctx, mnode, vnode):
    """prt3 (particle shader): MCC keeps THREE 'Postprocess Definition' elements, [1] and [2] always empty (same as
    shad/stem/spas); Vista has exactly one.  Cross-map oracle: 2929/2929 prt3 pairs."""
    f = vnode.field('Postprocess Definition')
    if f is None:
        return
    kids = vnode.blocks.get(f.offset) or []
    if len(kids) > 1:
        if all(_element_is_empty(k) for k in kids[1:]):
            vnode.blocks[f.offset] = kids[:1]
            _U32.pack_into(vnode.data, f.offset, 1)
            ctx.stats['prt3_postprocess_trimmed'] += 1
        else:
            ctx.warn('prt3_postprocess_nonempty', 'prt3 Postprocess Definition has %d elements and [1..] are not empty' % len(kids))


# built-in, always-on struct fixes of the generic pass: {(group, vista path): [fn(ctx, mcc_node, vista_node)]}
BUILTIN_FIXES = {('prt3', ''): [_fix_prt3_postprocess]}


def _conv_block(ctx, mnode, vnode, op, vpath, dpath, group, memo):
    _o, mo, vo, mf, vf, dname = op
    kids = mnode.blocks.get(mo)
    if not kids:
        vnode.blocks[vo] = []
        return
    key = (id(kids), id(vf.children), vpath)
    out = memo.get(key)
    if out is None:
        cv = vpath + '/' + vf.name
        cd = dpath + '/' + dname
        es = T.element_size(vf)
        vfc = vf.children
        hnd = memo.get('__handlers__', True)
        out = [convert_node(ctx, k, vfc, es, cv, group=group, dpath=cd, _memo=memo, _handlers=hnd) for k in kids]
        if vf.name == 'Predicted Resources':
            # {type, resource index, tag index}: an entry whose tag no longer exists is useless (Vista never stores a
            # null Tag Index: 0 of 8k+ entries in 01a_tutorial) - drop it instead of leaving a dangling prediction
            tf = next((c for c in vfc if c.name == 'Tag Index' and c.type == 'tagref' and c.size == 4), None)
            if tf is not None:
                keep = [k for k in out if _U32.unpack_from(k.data, tf.offset)[0] != NULL]
                if len(keep) != len(out):
                    ctx.stats['predicted_resources_dropped'] += len(out) - len(keep)
                    out = keep
        memo[key] = out
    vnode.blocks[vo] = out
    _U32.pack_into(vnode.data, vo, len(out))                     # count word (the encoder rewrites the address word)


def empty_tag_node(ctx, group):
    """A zero-filled Vista struct of the group's main size (blocks empty, tagrefs left at 0).  Used for MCC
    'empty' tags (size 0 with an aliasing address) so the output slot holds a valid, harmless tag."""
    lay = ctx.lay_vista[group]
    node = T.Node(group, '', lay.fields, lay.base_size or lay.computed_size)
    for f in lay.fields:
        if f.type == 'tagref':
            node.data[f.offset:f.offset + f.size] = b'\xff' * f.size
    return node


def convert_mcc_tag(ctx, mtag, cache=None, handlers=True):
    """Generic conversion of one MCC tag: decode with MCC layouts, convert_node with the Vista layout."""
    group = mtag.group
    vlay = ctx.lay_vista.get(group)
    if vlay is None:
        raise KeyError('no Vista layout for group %r' % group)
    if mtag.size == 0 and not mtag.external:
        ctx.warn('empty_tag_synth', '%s %s: MCC tag has size 0, emitting a zero-filled Vista struct' % (group, mtag.name))
        return empty_tag_node(ctx, group)
    mroot = ctx.decode_mcc(mtag, cache)
    return convert_node(ctx, mroot, vlay.fields, vlay.base_size or vlay.computed_size, '', group=group,
                        dpath='', _handlers=handlers)


# groups whose MCC data is DXBC-only shader code: never converted generically
NO_GENERIC_FALLBACK = ('vrtx', 'spas', 'stem')


def convert_tag(ctx, entry):
    """Convert the plan entry's source tag to a Vista root Node: the registered tag handler if there is one, else the
    generic path.  Sets ctx.cur_src for the duration (datum/string-id remapping is relative to the entry's source map)."""
    prev = ctx.cur_src
    ctx.cur_src = entry.src_map
    ctx.cur_entry = entry
    strict = ctx.opts.get('strict')
    try:
        fn = H.tag_handler_for(entry.group)
        if fn is not None and not ctx.opts.get('no_tag_handlers'):
            try:
                root = fn(ctx, entry)
                if root is None:
                    raise ValueError('tag handler returned None')
                ctx.stats['tag_handler:' + entry.group] += 1
                return root
            except Exception as ex:                                  # noqa: BLE001
                if strict:
                    raise
                if entry.group in NO_GENERIC_FALLBACK:
                    raise                                           # generic conversion of DXBC-only shader code would be garbage
                ctx.warn('tag_handler_failed', '%s %s: %r - falling back to the generic conversion' % (entry.group, entry.name, ex))
                ctx.cur_src = entry.src_map                         # the handler may have changed it
        cache = ctx.cache_for(entry.src_map)
        mtag = cache.tags[entry.src_index]
        node_handlers_on = not ctx.opts.get('no_node_handlers')
        try:
            root = convert_mcc_tag(ctx, mtag, cache, handlers=node_handlers_on)
            ctx.stats['generic:' + entry.group] += 1
            return root
        except Exception as ex:                                      # noqa: BLE001
            if strict or not node_handlers_on or entry.group in NO_GENERIC_FALLBACK:
                raise
            ctx.warn('node_handler_failed', '%s %s: %r - converted without node handlers' % (entry.group, entry.name, ex))
            ctx.stats['generic_without_handlers:' + entry.group] += 1
            return convert_mcc_tag(ctx, mtag, cache, handlers=False)
    finally:
        ctx.cur_src = prev
        ctx.cur_entry = None


# ================================================================================================
# Ctx
# ================================================================================================
_LAYOUTS = {}
_ALIGN = []


def layouts(game):
    """Corrected plugin layouts, parsed once per process ('mcc' | 'vista'); the compiled pair plans key on their ids."""
    if game not in _LAYOUTS:
        _LAYOUTS[game] = P.load_layouts(game, corrected=True)
    return _LAYOUTS[game]


def align_table():
    if not _ALIGN:
        _ALIGN.append(T.AlignTable.load(ALIGN_VISTA_PATH))
    return _ALIGN[0]


class VistaRefs(object):
    """The Vista maps used as references/donors."""

    def __init__(self, maps_dir=None):
        from .vista_cache import VistaCache
        self._cls = VistaCache
        self.maps_dir = maps_dir or _paths.vista_maps_dir()
        self._open = {}

    def open(self, name):
        if not name.lower().endswith('.map'):
            name += '.map'
        if name not in self._open:
            self._open[name] = self._cls(os.path.join(self.maps_dir, name))
        return self._open[name]

    @property
    def shared(self):
        return self.open('shared.map')

    @property
    def mainmenu(self):
        return self.open('mainmenu.map')

    @property
    def sp_shared(self):
        return self.open('single_player_shared.map')


class MccShared(dict):
    """{'shared','single_player_shared','mainmenu'} -> MccCache|None, opened lazily (decompressed stock MCC maps).
    Search order: explicit dir, <work folder>/mcc_decompressed, directory of the source map, stock MCC folder.
    custom-shared support: `shared_map` (an explicit shared.map path, the one a workshop mod's map was built against) wins for 'shared';
    `want_times` = the creation FILETIMEs the map recorded ([mainmenu, shared, single_player_shared], header 0x50..0x60): among the existing
    candidates the file with that creation time is preferred (a mod's folder may hold a shared.map next to a map that was built against the
    stock one, or the other way round); nothing recorded / nothing matching = the first existing file as before."""
    NAMES = {'shared': 'shared.map', 'single_player_shared': 'single_player_shared.map', 'mainmenu': 'mainmenu.map'}
    SLOT = {'mainmenu': 0, 'shared': 1, 'single_player_shared': 2}

    def __init__(self, dirs=(), shared_map=None, want_times=None):
        dict.__init__(self)
        self.dirs = [d for d in dirs if d] + [d for d in (MCC_DECOMP, _paths.mcc_maps_dir(required=False)) if d]
        self.shared_map = shared_map
        self.want_times = tuple(want_times) if want_times else ()

    def get(self, key, default=None):
        if key not in self:
            from .mcc_cache import MccCache, pick_shared_map
            cache = None
            if key == 'shared' and self.shared_map and os.path.isfile(self.shared_map):
                try:
                    cache = MccCache(self.shared_map)
                except Exception:
                    cache = None
            if cache is None:
                want = self.want_times[self.SLOT[key]] if len(self.want_times) == 3 else 0
                cands = [os.path.join(d, self.NAMES[key]) for d in self.dirs]
                if want:
                    # best match first; a candidate that fails to open falls through to the next one (as before)
                    from .mcc_cache import file_creation_time
                    cands = [p for p in cands if os.path.isfile(p) and file_creation_time(p) == want] + cands
                for p in cands:
                    if os.path.isfile(p):
                        try:
                            cache = MccCache(p)
                        except Exception:
                            cache = None
                        if cache is not None:
                            break
            self[key] = cache
        return dict.get(self, key, default)

    def __getitem__(self, key):
        return self.get(key)


class Ctx(object):
    """Conversion context.

    Attributes: src, mcc_shared, vista, plan, strings, lay_mcc, lay_vista, align, opts, map_type, stats, warnings,
    raws, cur_src ('src' | 'shared' | 'single_player_shared' | 'mainmenu': the MCC map the tag being converted
    comes from), locale_blob/language_pack (hook for handlers/locale.py), extra (free-form dict for handlers)."""

    MAX_WARN_PER_CODE = 25

    def __init__(self, src, mcc_shared=None, vista=None, plan=None, strings=None, opts=None,
                 lay_mcc=None, lay_vista=None, align=None, map_type=None):
        self.src = src
        self.mcc_shared = mcc_shared if mcc_shared is not None else MccShared([os.path.dirname(src.source_path)])
        self.vista = vista if vista is not None else VistaRefs()
        self.plan = plan
        self.strings = strings
        self.opts = dict(opts or {})
        self.lay_mcc = lay_mcc if lay_mcc is not None else layouts('mcc')
        self.lay_vista = lay_vista if lay_vista is not None else layouts('vista')
        self.align = align if align is not None else align_table()
        self.map_type = map_type if map_type is not None else src.header['scenario_type']
        self.stats = collections.Counter()
        self.warnings = collections.OrderedDict()      # code -> [(msg, tag)]
        self.raws = []                                 # [(kind, bytes)]
        self.cur_src = 'src'
        self.cur_entry = None
        self.locale_langs = None                       # preferred locale hook: 9 lists of (vista string id, utf8 bytes)
        self.locale_blob = None                        # alt. hook: bytes of the locale area ...
        self.language_pack = None                      # ... + 252-byte language pack whose offsets are RELATIVE to the blob
        self.extra = {}
        self._dcache = {}
        self._scache = {}
        self._resolved = None

    # ---- source maps -----------------------------------------------------------------------------------
    def cache_for(self, src_map):
        if src_map in (None, 'src'):
            return self.src
        c = self.mcc_shared.get(src_map)
        if c is None:
            raise KeyError('MCC %s not available' % src_map)
        return c

    @property
    def cur_cache(self):
        return self.cache_for(self.cur_src)

    # ---- ids ---------------------------------------------------------------------------------------------
    def remap_datum(self, mcc_datum):
        if mcc_datum is None or mcc_datum == NULL:
            return NULL
        key = (self.cur_src, mcc_datum)
        r = self._dcache.get(key)
        if r is None:
            r = self.plan.remap_datum(self.cur_src, mcc_datum)
            if r is None:
                r = NULL
            self._dcache[key] = r
        return r

    def remap_name(self, group, name):
        """Output datum of the planned tag with this (group, name) (donor tags are resolved by name), NULL if none."""
        return self.plan.remap_name(group, name)

    def mcc_tag(self, mcc_datum, src_map=None):
        c = self.cache_for(src_map or self.cur_src)
        return c.tag_by_datum(mcc_datum)

    def mcc_group_word(self, mcc_datum):
        """The 'group' dword (as stored in tag references) of the MCC tag with this datum, 0xFFFFFFFF if unknown."""
        t = self.cur_cache.tag_by_datum(mcc_datum)
        return t.group_int if t is not None else NULL

    def mcc_tag_label(self, mcc_datum):
        t = self.cur_cache.tag_by_datum(mcc_datum)
        if t is None:
            return '<datum 0x%08X not in table>' % mcc_datum
        return '%s %s' % (t.group, t.name)

    def mcc_string(self, mcc_sid):
        if not mcc_sid or mcc_sid == NULL:
            return ''
        return self.cur_cache.string_id(mcc_sid)

    def sid(self, text):
        if not text:
            return 0
        return self.strings.sid(text)

    def remap_sid(self, mcc_sid):
        if not mcc_sid or mcc_sid == NULL:
            return mcc_sid or 0
        key = (self.cur_src, mcc_sid)
        r = self._scache.get(key)
        if r is None:
            try:
                r = self.sid(self.mcc_string(mcc_sid))
            except Exception as ex:                                  # noqa: BLE001  junk id in an unused/opaque MCC field
                self.warn('bad_stringid', '%s 0x%08X: %s' % (self.cur_src, mcc_sid, ex))
                r = 0
            self._scache[key] = r
        return r

    # ---- decode ------------------------------------------------------------------------------------------
    def decode_mcc(self, tag, cache=None):
        c = cache or self.cur_cache
        if tag.external:
            for nm in ('shared', 'single_player_shared', 'mainmenu'):
                sh = self.mcc_shared.get(nm)
                if sh is None or tag.index >= len(sh.tags):
                    continue
                st = sh.tags[tag.index]
                if st.datum == tag.datum and st.group == tag.group and st.has_data:
                    tag, c = st, sh
                    break
            else:
                raise KeyError('external MCC tag #%d %s %s not found in any shared map' % (tag.index, tag.group, tag.name))
        lay = self.lay_mcc[tag.group]
        return T.decode_tag(lambda a, n: c.read_at_addr(a, n), lay, tag.addr, tag.group)

    def decode_vista(self, cache, tag):
        return T.decode_tag(cache.read_addr, self.lay_vista[tag.group], tag.addr, tag.group)

    # ---- raw resources ------------------------------------------------------------------------------------
    def add_raw(self, kind, data, align=0x200):
        """Register a raw payload ('sound' | 'geometry' | 'bitmap') and return a token for node.patches[...]."""
        if kind not in ('sound', 'geometry', 'bitmap'):
            raise ValueError('unknown raw kind %r' % kind)
        self.raws.append((kind, data if isinstance(data, bytes) else bytes(data)))
        return ('raw', len(self.raws) - 1)

    def resolve(self, token):
        """Final Vista raw pointer of a token (valid once the driver placed the raw regions)."""
        if self._resolved is None:
            raise RuntimeError('raw regions are not placed yet')
        return self._resolved[token[1]]

    # ---- reporting ----------------------------------------------------------------------------------------
    def warn(self, code, msg, tag=None):
        self.stats['warn:' + code] += 1
        lst = self.warnings.get(code)
        if lst is None:
            lst = self.warnings[code] = []
        if len(lst) < self.MAX_WARN_PER_CODE:
            lst.append((msg, tag if tag is not None else (self.cur_entry.name if self.cur_entry is not None else None)))

    def note_dropped_ref(self, node, fname, mcc_datum):
        # the label lookup is only worth doing while the warning list still has room
        if len(self.warnings.get('tagref_dropped', ())) < self.MAX_WARN_PER_CODE:
            self.warn('tagref_dropped', '%s|%s %s -> %s' % (node.group, node.path, fname, self.mcc_tag_label(mcc_datum)))
        else:
            self.stats['warn:tagref_dropped'] += 1
