# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Generic Halo 2 Vista -> MCC tag transcoder + the RevCtx object every reverse handler codes against.

Mirror of h2conv/transcode.py with the roles swapped: the SOURCE is a Vista (cache v8) tag decoded with the Vista
layouts, the TARGET is an MCC (cache v13) tag in the MCC layouts.

Public API
----------
RevCtx(src, vista_refs, mcc_refs, plan, strings, opts=None, ...)   the conversion context (same names as Ctx)
convert_node(ctx, vnode, mfields, msize, mpath, ...) -> tagtree.Node
        generic, layout-driven conversion of ONE Vista struct and (recursively) all its blocks/datarefs into the MCC
        layout; then the registered REVERSE node handlers (h2conv.rev.handlers.node_handlers(group, mpath)).
convert_tag(ctx, entry) -> tagtree.Node          tag handler if registered else decode + convert_node
convert_vista_tag(ctx, vtag, cache=None) -> Node generic conversion of one Vista tag (no tag handler) - used by tests
empty_tag_node(ctx, group) -> Node               zero-filled MCC struct (null tagrefs)
McStringTable                                    output string table (MCC shared.map strings as an exact prefix)

How a pair of structs is converted
----------------------------------
plugins.diff_fields(vista_fields, mcc_fields) (+ plugins.SEMANTIC_PAIRS with the names swapped) + plugins.make_plan are
compiled ONCE per (id(vista_fields), id(mcc_fields), group, path) into a flat op list:
    raw byte copies | tagref remap | stringid remap | dataref copy | tagblock recursion | numeric resize |
    reverse enum/flag policy | learned defaults and null-fill for the MCC-only fields.
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

from .. import paths as _paths
from .. import plugins as P
from .. import tagtree as T
from . import handlers as H

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
ROOT = os.path.dirname(PKG)
DATA_DIR = os.path.join(HERE, 'data')
NULL = 0xFFFFFFFF
MCC_DECOMP = os.path.join(_paths.work_dir(), 'mcc_decompressed')
ENUM_POLICY_PATH = os.path.join(_paths.data_dir(), 'enum_policy.json')          # MCC -> Vista (inverted here)
MCC_ALIGN_PATH = os.path.join(_paths.data_dir(), 'block_alignment_mcc.json')


def __getattr__(name):
    """VISTA_MAPS_DEFAULT / MCC_STOCK_DIR: folders on one PC, now resolved by h2conv.paths; kept as attributes for the developer tools."""
    if name == 'VISTA_MAPS_DEFAULT':
        return _paths.vista_maps_dir()
    if name == 'MCC_STOCK_DIR':
        return _paths.mcc_maps_dir()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


DEFAULTS_PATH = os.path.join(DATA_DIR, 'rev_defaults.json')
NULL_WORDS_PATH = os.path.join(DATA_DIR, 'rev_null_words.json')

_U32 = struct.Struct('<I')
_U32x2 = struct.Struct('<II')
_INT_TYPES = frozenset(['int8', 'int16', 'int32', 'uint8', 'uint16', 'uint32', 'uint64', 'enum8', 'enum16', 'enum32',
                        'flags8', 'flags16', 'flags32', 'stringid', 'datum', 'undefined'])
_SIGNED = frozenset(['int8', 'int16', 'int32'])

# groups that exist in Vista campaign maps only (scenario-resource tags; 0 of 43 MCC maps carry one): MCC has no layout
# of its own for them, so the (corrected) Vista layout is used for both sides and the tag is copied 1:1.  The planner
# normally drops them.
VISTA_ONLY_GROUPS = ('*cen', '*eap', '*ehi', '*igh', '*ipd', '*qip', '*rea', '*sce', '/**/', 'ai**', 'cin*', 'clu*',
                     'dc*s', 'dec*', 'dgr*', 'hsc*', 'sslt', 'trg*')

# (group, MCC path, SOURCE field name) pairings of plugins.diff_fields that are accidents of equal offset/size and must
# not be converted as a pair.  snd!: Vista enum8 'Sample Rate'@3 is NOT the MCC int8 'Codec Index'@3.
UNPAIR = frozenset([('snd!', '', 'Sample Rate')])

# size changes that need no handler (no warning): crea Flags flags32 -> flags16 (the plugin names bits 0-6 only); spas Pixel Shader Handle
# (Runtime) uint32 -> uint64 (a runtime handle, written 0)
BENIGN_RESIZE = frozenset([('crea', '', 'Flags'), ('spas', '/Postprocess Definition/Implementations/Pixel Shader', 'Pixel Shader Handle (Runtime)')])


# ================================================================================================
# enum / flags policy: reverse of h2conv/data/enum_policy.json
# ================================================================================================
_POLICY = None


def _fcc(text):
    return struct.unpack('<I', text.encode('latin-1')[::-1])[0]


# Built-in reverse policy.
# jpt! Flags@0xC: the MCC tool sets bit 2 (undefined in both plugins) in 1332 of 1416 same-name pairs (151 of 159 distinct names; the
# 8 exceptions are charging / overheat style near-empty damage effects, no rule found); Vista never does (the forward converter
# clears it).
BUILTIN_POLICY = {
    ('jpt!', '', 'flags'): {'set_bits': [2], '_only_offset': 0x0C},
}


def reload_enum_policy(path=ENUM_POLICY_PATH):
    """The reverse enum policy.  h2conv/data/enum_policy.json decides what the forward converter does with MCC-only options;
    every VISTA option exists in MCC with the same value/bit (checked by plugins.diff_fields: the only SRC_ONLY options are
    label spellings), so the reverse default is IDENTITY.  The one exception is a `move_bits` rule (snd! Flags: MCC
    bits 12..15 are Vista bits 10..13): inverted here ({to: from}) and the vacated Vista positions are cleared.
    Keys: (group, block path, lower-case field name[@offset]) -> spec (clear_bits/move_bits/set_bits/keep, as in transcode.py;
    '_only_offset' restricts a spec to one MCC field offset)."""
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
                        if fname.startswith('_') or not isinstance(spec, dict) or not spec.get('move_bits'):
                            continue
                        inv = {int(v): int(k) for k, v in spec['move_bits'].items()}
                        pol[(g, bp, fname.lower())] = {'move_bits': inv, 'clear_bits': sorted(inv)}
        except Exception as ex:  # a broken policy file must not break conversion
            print('WARNING: could not read %s: %s' % (path, ex))
    _POLICY = pol
    return pol


def default_enum_policy():
    if _POLICY is None:
        reload_enum_policy()
    return _POLICY


def _policy_for(group, mpath, fname, s_off=None, d_off=None, dpath=None):
    pol = default_enum_policy()
    if not pol:
        return None
    names = []
    if d_off is not None:
        names.append('%s@%d' % (fname.lower(), d_off))
    names.append(fname.lower())
    for key in names:
        # forward keys use the MCC path (= our target path), duplicates are '/Settings#1'
        for bp in ((dpath,) if dpath and dpath != mpath else ()) + (mpath, mpath.lstrip('/'), mpath.rstrip('/'), '*'):
            s = pol.get((group, bp, key))
            if s is not None and not ('_only_offset' in s and d_off is not None and s['_only_offset'] != d_off):
                return s
    return None


# ================================================================================================
# learned data: defaults of MCC-only fields, null-reference group words
# ================================================================================================
_DEFAULTS = None
_NULL_WORDS = None


def load_defaults(path=DEFAULTS_PATH):
    """{group: {block path (MCC): {offset: bytes}}}: values the MCC tool writes into fields Vista does not have
    (modal value over the stock MCC maps and the H2EK-built maps)."""
    global _DEFAULTS
    d = {}
    if os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as fh:
            raw = json.load(fh)
        for g, blocks in raw.items():
            if g.startswith('_'):
                continue
            for bp, offs in blocks.items():
                for off, hx in offs.items():
                    d.setdefault((g, bp), {})[int(off)] = bytes.fromhex(hx)
    _DEFAULTS = d
    return d


def defaults_for(group, mpath):
    if _DEFAULTS is None:
        load_defaults()
    return _DEFAULTS.get((group, mpath)) or {}


def load_null_words(path=NULL_WORDS_PATH):
    """{(group, MCC path, field name): group word an MCC NULL reference carries where it differs from Vista's}."""
    global _NULL_WORDS
    d = {}
    if os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as fh:
            raw = json.load(fh)
        for k, v in raw.items():
            if k.startswith('_'):
                continue
            g, bp, fn = k.split('|', 2)
            d[(g, bp, fn)] = int(v, 16) if isinstance(v, str) else int(v)
    _NULL_WORDS = d
    return d


def null_word_for(group, mpath, fname):
    if _NULL_WORDS is None:
        load_null_words()
    return _NULL_WORDS.get((group, mpath, fname))


# ================================================================================================
# pair plans
# ================================================================================================
OP_REF = 1       # (op, so, do, ssz, dsz, name, null_word)         tagref / 4-byte datum-only tagref
OP_SID = 2       # (op, so, do)
OP_DATA = 3      # (op, so, do)
OP_BLOCK = 4     # (op, so, do, sf, df, dname)                     dname = SOURCE block name with '#k' duplicate suffix
OP_COPY = 5      # (op, so, do, n)
OP_RESIZE = 6    # (op, so, do, ssz, dsz, signed, name, zero)
OP_ENUM = 7      # (op, so, do, size, is_flags, clear_mask, set_mask, value_map, default, only_values, name, moves)
OP_NULLFILL = 8  # (op, do, size, null_word)                         MCC-only tagref: write a null reference
OP_DEFAULT = 9   # (op, do, bytes)                                 learned default of an MCC-only field
OP_ZERO = 10     # (op, do, size)                                  target field fed by a source of another kind


class RPair(object):
    __slots__ = ('raw', 'ops', 'identity', 'ssize', 'dsize', 'group', 'mpath', 'dpath', 'notes', 'has_children')


_PAIR_CACHE = {}
_PAIR_KEEP = []     # keep the field lists alive so id() keys stay unique

_RUNTIME_RE = re.compile(r'runtime|handle \(runtime\)|pointer', re.I)


def get_pair(sfields, dfields, ssize, dsize, group, spath, mpath):
    key = (id(sfields), id(dfields), group, spath, mpath)
    pr = _PAIR_CACHE.get(key)
    if pr is None:
        pr = _build_pair(sfields, dfields, ssize, dsize, group, spath, mpath)
        _PAIR_CACHE[key] = pr
        _PAIR_KEEP.append((sfields, dfields))
    return pr


def _split_unpaired(ents, group, mpath):
    out = []
    for e in ents:
        s, d = e['_m'], e['_v']
        if s is not None and d is not None and (group, mpath, s.name) in UNPAIR:
            out.append(P._entry(s, None, 'only_mcc'))
            out.append(P._entry(None, d, 'only_vista'))
        else:
            out.append(e)
    return out


def _build_pair(sfields, dfields, ssize, dsize, group, spath, mpath):
    """sfields = Vista (source) fields, dfields = MCC (target) fields.  NOTE: in the plugins.diff_fields entries '_m' is the
    FIRST argument (here the Vista/source field) and '_v' the second (the MCC/target field); the 'mcc_*'/'vista_*' keys of
    make_plan() are therefore source/target in this module: raw (src_off, dst_off, n), fixup mcc_offset=src vista_offset=dst,
    vista_fill = target-only fields, mcc_drop = source-only fields."""
    ents = P.diff_fields(sfields, dfields)
    for (sg, sp, smn, svn, snote) in P.SEMANTIC_PAIRS:
        if sg == group and sp == spath:
            ents = P._apply_semantic(ents, svn, smn, snote)         # swapped: first arg = Vista name
    ents = _split_unpaired(ents, group, mpath)
    plan = P.make_plan(ents)
    sby = {}
    for f in sfields:
        sby.setdefault(f.offset, f)
    dby = {}
    for f in dfields:
        dby.setdefault(f.offset, f)
    pr = RPair()
    pr.ssize, pr.dsize, pr.group, pr.mpath, pr.dpath = ssize, dsize, group, mpath, spath
    pr.notes = []
    ops = []
    pr.raw = [tuple(r) for r in plan['raw']]
    seen_block = {}
    for e in ents:
        s, d = e['_m'], e['_v']
        if s is None or d is None:
            continue
        if s.children is not None and d.children is not None:
            k = seen_block.get(s.name, 0)
            seen_block[s.name] = k + 1
            ops.append((OP_BLOCK, s.offset, d.offset, s, d, s.name + ('#%d' % k if k else '')))
    for fx in plan['fixup']:
        sf = sby.get(fx['mcc_offset'])
        df = dby.get(fx['vista_offset'])
        if sf is None or df is None:
            continue
        st, dt = sf.type, df.type
        if st == 'tagblock' and dt == 'tagblock':
            continue
        if st == 'dataref' and dt == 'dataref':
            ops.append((OP_DATA, sf.offset, df.offset))
        elif st == 'stringid' and dt == 'stringid':
            ops.append((OP_SID, sf.offset, df.offset))
        elif dt == 'stringid':                                         # fed by an opaque/int source: no valid id to keep
            ops.append((OP_ZERO, df.offset, df.size))
        elif st == 'unicList' and dt == 'unicList':
            ops.append((OP_COPY, sf.offset, df.offset, min(sf.size, df.size)))
        elif st == 'datum' and dt == 'datum':
            ops.append((OP_COPY, sf.offset, df.offset, min(sf.size, df.size)))      # object Unique IDs / expression datums
        elif st in ('tagref', 'datum') and dt in ('tagref', 'datum'):
            ops.append((OP_REF, sf.offset, df.offset, sf.size, df.size, sf.name,
                        null_word_for(group, mpath, df.name) if df.size == 8 else None))
        elif dt == 'tagref':
            ops.append((OP_NULLFILL, df.offset, df.size, null_word_for(group, mpath, df.name)))
        elif sf.size != df.size or st in _INT_TYPES:
            ops.append((OP_RESIZE, sf.offset, df.offset, sf.size, df.size, st in _SIGNED, sf.name,
                        bool(_RUNTIME_RE.search(df.name)) and df.size == 8))
        else:
            pr.notes.append('unhandled fixup %s->%s %s' % (st, dt, sf.name))
            ops.append((OP_COPY, sf.offset, df.offset, min(sf.size, df.size)))
    # ---- reverse enum / flags policy (identity unless the policy file moves bits) ----
    for e in ents:
        sf, df = e['_m'], e['_v']
        if sf is None or df is None or sf.size != df.size:
            continue
        if not (sf.type.startswith(('flags', 'enum')) and df.type[:5] == sf.type[:5]):
            continue
        is_flags = sf.type.startswith('flags')
        pol = _policy_for(group, mpath, df.name, sf.offset, df.offset, spath)
        if pol is None or pol.get('keep'):
            continue
        clear = setm = 0
        moves = None
        for b_ in pol.get('clear_bits', ()):
            clear |= 1 << int(b_)
        for b_ in pol.get('set_bits', ()):
            setm |= 1 << int(b_)
        if pol.get('move_bits'):
            moves = tuple((int(k), int(v)) for k, v in pol['move_bits'].items())
        if not is_flags or not (clear or setm or moves):
            continue
        ops.append((OP_ENUM, sf.offset, df.offset, sf.size, True, clear, setm, None, 0, set(), sf.name, moves))
    # ---- MCC-only fields: null references, learned defaults ----
    dflt = defaults_for(group, mpath)
    for off, size, name, typ in plan['vista_fill']:
        if typ == 'tagref':
            ops.append((OP_NULLFILL, off, size, null_word_for(group, mpath, name) if size == 8 else None))
        elif off in dflt and len(dflt[off]) == size and typ not in ('tagblock', 'dataref'):
            ops.append((OP_DEFAULT, off, dflt[off]))
    pr.ops = ops
    pr.has_children = any(o[0] in (OP_BLOCK, OP_DATA) for o in ops)
    pr.identity = (ssize == dsize and not ops and len(pr.raw) == 1 and pr.raw[0] == (0, 0, ssize))
    return pr


# ================================================================================================
# generic conversion
# ================================================================================================
def _int_resize(sd, so, dd, do, ssz, dsz, signed):
    if ssz == dsz:
        dd[do:do + dsz] = sd[so:so + ssz]
    elif ssz > dsz:
        dd[do:do + dsz] = sd[so:so + dsz]                  # low bytes (little endian)
    else:
        dd[do:do + ssz] = sd[so:so + ssz]
        if signed and sd[so + ssz - 1] & 0x80:
            dd[do + ssz:do + dsz] = b'\xff' * (dsz - ssz)


def _conv_ref(ctx, sd, dd, so, do, ssz, dsz, name, null_word, node):
    if ssz == 8:
        g, d = _U32x2.unpack_from(sd, so)
    else:
        g = None
        d = _U32.unpack_from(sd, so)[0]
    if d == NULL or (d == 0 and ssz == 4) or (d == 0 and g == 0):
        nd = d
    else:
        nd = ctx.remap_datum(d)
        ctx.stats['tagref_remapped'] += 1
        if nd == NULL:
            ctx.stats['tagref_dropped'] += 1
            ctx.note_dropped_ref(node, name, d)
    if dsz == 8:
        if g is None:
            g = ctx.vista_group_word(d) if d not in (NULL, 0) else NULL
        elif g == NULL and d not in (NULL, 0):
            g = ctx.vista_group_word(d)
        if nd == NULL and null_word is not None:
            g = null_word
        _U32x2.pack_into(dd, do, g, nd)
    else:
        _U32.pack_into(dd, do, nd)


def _conv_enum(ctx, sd, dd, op, node):
    (_o, so, do, size, is_flags, clear, setm, vmap, default, only_vals, name, moves) = op
    val = int.from_bytes(sd[so:so + size], 'little')
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
    dd[do:do + size] = (val & ((1 << (8 * size)) - 1)).to_bytes(size, 'little')


def convert_node(ctx, snode, dfields, dsize, mpath, group=None, spath=None, _memo=None, _handlers=True):
    """Convert one Vista struct (Node) to an MCC Node with layout (dfields, dsize) at tagtree path `mpath` (MCC names)."""
    group = group or snode.group
    if spath is None:
        spath = mpath
    if _memo is None:
        _memo = {'__handlers__': _handlers}
    ssize = len(snode.data)
    pr = get_pair(snode.fields, dfields, ssize, dsize, group, spath, mpath)
    sd = snode.data
    if pr.identity:
        dnode = T.Node(group, mpath, dfields, dsize, sd)
    else:
        dnode = T.Node(group, mpath, dfields, dsize)
        dd = dnode.data
        for so, do, n in pr.raw:
            dd[do:do + n] = sd[so:so + n]
        if pr.ops:
            for op in pr.ops:
                c = op[0]
                if c == OP_REF:
                    _conv_ref(ctx, sd, dd, op[1], op[2], op[3], op[4], op[5], op[6], snode)
                elif c == OP_SID:
                    so, do = op[1], op[2]
                    s = _U32.unpack_from(sd, so)[0]
                    if s != 0 and s != NULL:
                        try:
                            s = ctx.remap_sid(s)
                        except Exception as ex:             # junk id in an unused/opaque Vista field
                            ctx.warn('bad_stringid', '%s|%s 0x%X: %s' % (group, mpath, s, ex))
                            s = 0
                    _U32.pack_into(dd, do, s)
                elif c == OP_DATA:
                    payload = snode.datas.get(op[1]) or b''
                    dnode.datas[op[2]] = payload
                    _U32.pack_into(dd, op[2], len(payload))
                elif c == OP_BLOCK:
                    _conv_block(ctx, snode, dnode, op, mpath, spath, group, _memo)
                elif c == OP_COPY:
                    dd[op[2]:op[2] + op[3]] = sd[op[1]:op[1] + op[3]]
                elif c == OP_RESIZE:
                    if op[7]:                                # runtime pointer widened to 64 bit: the engine rewrites it
                        dd[op[2]:op[2] + op[4]] = b'\0' * op[4]
                    else:
                        _int_resize(sd, op[1], dd, op[2], op[3], op[4], op[5])
                    rk = 'resize:%s|%s|%s' % (group, mpath, op[6])
                    ctx.stats[rk] += 1
                    if ctx.stats[rk] == 1 and (group, mpath, op[6]) not in BENIGN_RESIZE and not (
                            _handlers and any(getattr(fn, 'owns_resize', False) for fn in H.node_handlers(group, mpath))):
                        ctx.warn('resize_default', '%s|%s %s: %d -> %d bytes, low bytes kept/zero-extended (add a handler if the value matters)' % (
                            group, mpath or '/', op[6], op[3], op[4]))
                elif c == OP_ENUM:
                    _conv_enum(ctx, sd, dd, op, snode)
                elif c == OP_NULLFILL:
                    dd[op[1]:op[1] + op[2]] = b'\xff' * op[2]
                    if op[3] is not None:
                        _U32.pack_into(dd, op[1], op[3])
                elif c == OP_DEFAULT:
                    dd[op[1]:op[1] + len(op[2])] = op[2]
                elif c == OP_ZERO:
                    dd[op[1]:op[1] + op[2]] = b'\0' * op[2]
    bf = BUILTIN_FIXES.get((group, mpath))
    if bf is not None:
        for fn in bf:
            fn(ctx, snode, dnode)
    if _handlers:
        hs = H.node_handlers(group, mpath)
        if hs:
            for fn in hs:
                fn(ctx, snode, dnode)
    so_ = _sort_offsets(dfields, group, mpath)
    if so_:
        _sort_blocks(ctx, dnode, so_, group, mpath)
    return dnode


# ------------------------------------------------------------------------------------------------
# blocks the ENGINE binary-searches (halo2.exe sub_4F5B8F; the MCC build keeps the same table convention): after the
# by-text string-id remap the numeric order changes, so they are re-sorted ascending by the signed first dword.
# ------------------------------------------------------------------------------------------------
ENGINE_SORTED_BLOCKS = frozenset([('jmad', '', 'Vehicle Suspension'), ('jmad', '', 'Object Overlays')])
_I32 = struct.Struct('<i')
_SORT_CACHE = {}


def _sort_offsets(dfields, group, mpath):
    key = (id(dfields), group, mpath)
    r = _SORT_CACHE.get(key)
    if r is None:
        r = tuple(f.offset for f in dfields if f.type in T.BLOCK_TYPES and (
            str(f.attrs.get('sort', '')).lower() == 'true' or (group, mpath, f.name) in ENGINE_SORTED_BLOCKS))
        _SORT_CACHE[key] = r
        _PAIR_KEEP.append(dfields)
    return r


def sort_block_elements(kids):
    keys = [_I32.unpack_from(k.data, 0)[0] for k in kids]
    if all(keys[i] <= keys[i + 1] for i in range(len(keys) - 1)):
        return kids
    return [kids[i] for i in sorted(range(len(kids)), key=keys.__getitem__)]


def _sort_blocks(ctx, dnode, offsets, group, mpath):
    for off in offsets:
        kids = dnode.blocks.get(off)
        if not kids or len(kids) < 2:
            continue
        out = sort_block_elements(kids)
        if out is not kids:
            dnode.blocks[off] = out
            ctx.stats['sorted_blocks'] += 1
            ctx.stats['sorted_block_elements'] += len(out)


def resort_engine_sorted(ctx, root):
    """An MCC tree that did NOT go through convert_node (an 'mcc_donor' tag): after its string ids were remapped BY TEXT into the
    output string table their numeric order is not MCC's any more, so every engine-binary-searched block (same set as the
    converted path: plugin sort="true" + ENGINE_SORTED_BLOCKS) is re-sorted IN PLACE (decode_tag shares one list between identical
    block intervals; every sharer wants the same order).  Unsorted donor jmad Modes made halo2.dll's binary search miss
    ghost_d / spectre_d / warthog_d / warthog_g ...: the player could not enter those driver / gunner seats.
    Returns the number of blocks reordered."""
    seen = set()
    stack = [root]
    n_sorted = 0
    while stack:
        n = stack.pop()
        if id(n) in seen:
            continue
        seen.add(id(n))
        for off in _sort_offsets(n.fields, n.group, n.path):
            kids = n.blocks.get(off)
            if not kids or len(kids) < 2:
                continue
            out = sort_block_elements(kids)
            if out is not kids:
                kids[:] = out
                n_sorted += 1
                ctx.stats['donor_sorted_blocks'] += 1
                ctx.stats['donor_sorted_block_elements'] += len(out)
        for kids in n.blocks.values():
            stack.extend(kids)
    return n_sorted


# ------------------------------------------------------------------------------------------------
# built-in structural fixes of the generic pass
# ------------------------------------------------------------------------------------------------
def _pad_postprocess(ctx, snode, dnode):
    """shad / stem / spas / prt3: the MCC tool writes THREE 'Postprocess Definition' elements, [1] and [2] always all-zero
    (also their 4-byte tag references: 0, not null); Vista has exactly one."""
    f = dnode.field('Postprocess Definition')
    if f is None:
        return
    kids = dnode.blocks.get(f.offset) or []
    if len(kids) == 1:
        es = T.element_size(f)
        kids = list(kids) + [T.Node(dnode.group, dnode.path + '/' + f.name, f.children, es) for _ in range(2)]
        dnode.blocks[f.offset] = kids
        _U32.pack_into(dnode.data, f.offset, 3)
        ctx.stats['postprocess_padded'] += 1


def _hlmt_computed_zero(ctx, snode, dnode):
    """hlmt Render-Only Node Flags / Render-Only Section Flags / Runtime Flags (+0xA4..+0xE7) are filled by the Vista tool
    and left ZERO by the MCC tool in 90 percent of the stock models (2554/2825; 109/125 in the H2EK map):
    over the 43-map oracle 2645/2818 pairs differ only because we copied the Vista values."""
    for f in dnode.fields:
        n = f.name
        if n.startswith('Render-Only Node Flags') or n.startswith('Render-Only Section Flags') or n == 'Runtime Flags':
            dnode.data[f.offset:f.offset + f.size] = bytes(f.size)
    ctx.stats['hlmt_computed_flags_zeroed'] += 1


def _hudg_mcc_style(ctx, snode, dnode):
    """hudg: the MCC tool leaves Icon Bitmap / Damage Indicator Bitmap null and Button Icons empty in 18/18 stock maps (Vista:
    rasterizer\\invalid and 18 icons in 18/18; the forward converter fills them from a Vista donor)."""
    for nm in ('Icon Bitmap', 'Damage Indicator Bitmap'):
        f = dnode.field(nm)
        if f is not None and f.type == 'tagref':
            dnode.data[f.offset:f.offset + f.size] = b'\xff' * f.size
    f = dnode.field('Button Icons')
    if f is not None and f.type in T.BLOCK_TYPES:
        dnode.blocks[f.offset] = []
        _U32.pack_into(dnode.data, f.offset, 0)
    ctx.stats['hudg_mcc_style'] += 1


BUILTIN_FIXES = {('shad', ''): [_pad_postprocess], ('stem', ''): [_pad_postprocess], ('spas', ''): [_pad_postprocess],
                 ('prt3', ''): [_pad_postprocess], ('hlmt', ''): [_hlmt_computed_zero], ('hudg', ''): [_hudg_mcc_style]}


def _conv_block(ctx, snode, dnode, op, mpath, spath, group, memo):
    _o, so, do, sf, df, sname = op
    kids = snode.blocks.get(so)
    if not kids:
        dnode.blocks[do] = []
        return
    key = (id(kids), id(df.children), mpath)
    out = memo.get(key)
    if out is None:
        cm = mpath + '/' + df.name
        cs = spath + '/' + sname
        es = T.element_size(df)
        dfc = df.children
        hnd = memo.get('__handlers__', True)
        out = [convert_node(ctx, k, dfc, es, cm, group=group, spath=cs, _memo=memo, _handlers=hnd) for k in kids]
        if df.name == 'Predicted Resources':
            tf = next((c for c in dfc if c.name == 'Tag Index' and c.type == 'tagref' and c.size == 4), None)
            if tf is not None:
                keep = [k for k in out if _U32.unpack_from(k.data, tf.offset)[0] != NULL]
                if len(keep) != len(out):
                    ctx.stats['predicted_resources_dropped'] += len(out) - len(keep)
                    out = keep
        memo[key] = out
    dnode.blocks[do] = out
    _U32.pack_into(dnode.data, do, len(out))


def empty_tag_node(ctx, group):
    """A zero-filled MCC struct of the group's main size (blocks empty, tagrefs null)."""
    lay = ctx.lay_mcc[group]
    node = T.Node(group, '', lay.fields, lay.base_size or lay.computed_size)
    for f in lay.fields:
        if f.type == 'tagref':
            node.data[f.offset:f.offset + f.size] = b'\xff' * f.size
    return node


def convert_vista_tag(ctx, vtag, cache=None, handlers=True):
    """Generic conversion of one Vista tag: decode with the Vista layouts, convert_node into the MCC layout."""
    group = vtag.group
    mlay = ctx.lay_mcc.get(group)
    if mlay is None:
        raise KeyError('no MCC layout for group %r' % group)
    if not vtag.addr:
        ctx.warn('empty_tag_synth', '%s %s: Vista tag has no data (shared placeholder), emitting a zero-filled MCC struct' % (group, vtag.name))
        return empty_tag_node(ctx, group)
    if not vtag.size:
        # Vista 'empty' tag (size 0, address != 0): the tool stores IDENTICAL tag contents once and points the twins' instances at the
        # first copy (shared.map: impact effects soft_organic -> soft_inorganic, jpt! rocket_launcher_trigger -> flak_cannon_trigger ...;
        # every such address lies inside an earlier tag of the same group).  The struct at that address IS the tag's content.
        ctx.stats['vista_empty_alias_decoded'] += 1
    sroot = ctx.decode_vista(vtag, cache)
    return convert_node(ctx, sroot, mlay.fields, mlay.base_size or mlay.computed_size, '', group=group,
                        spath='', _handlers=handlers)


# groups whose Vista data is D3D9 shader byte code only: never converted generically (MCC wants DXBC)
NO_GENERIC_FALLBACK = ('vrtx', 'spas', 'stem')


def convert_tag(ctx, entry):
    """Convert the plan entry's source tag to an MCC root Node: the registered reverse tag handler if there is one, else
    the generic path.  Sets ctx.cur_src for the duration (datum/string-id remapping is relative to the entry's source map)."""
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
                    raise
                ctx.warn('tag_handler_failed', '%s %s: %r - falling back to the generic conversion' % (entry.group, entry.name, ex))
                ctx.cur_src = entry.src_map
        cache = ctx.cache_for(entry.src_map)
        vtag = cache.tags[entry.src_index]
        node_handlers_on = not ctx.opts.get('no_node_handlers')
        try:
            root = convert_vista_tag(ctx, vtag, cache, handlers=node_handlers_on)
            ctx.stats['generic:' + entry.group] += 1
            return root
        except Exception as ex:                                      # noqa: BLE001
            if strict or not node_handlers_on or entry.group in NO_GENERIC_FALLBACK:
                raise
            ctx.warn('node_handler_failed', '%s %s: %r - converted without node handlers' % (entry.group, entry.name, ex))
            ctx.stats['generic_without_handlers:' + entry.group] += 1
            return convert_vista_tag(ctx, vtag, cache, handlers=False)
    finally:
        ctx.cur_src = prev
        ctx.cur_entry = None


# ================================================================================================
# layouts, alignment, reference maps
# ================================================================================================
_LAYOUTS = {}
_ALIGN = []


def layouts(game):
    """Corrected plugin layouts, parsed once per process ('mcc' | 'vista')."""
    if game not in _LAYOUTS:
        lay = P.load_layouts(game, corrected=True)
        if game == 'mcc':
            vl = layouts('vista')
            for g in VISTA_ONLY_GROUPS:
                if g in vl:
                    lay[g] = vl[g]
        _LAYOUTS[game] = lay
    return _LAYOUTS[game]


VISTA_ALIGN_PATH = os.path.join(_paths.data_dir(), 'block_alignment_vista.json')


def merged_alignment():
    """The MCC block/data alignment table: {key: alignment}.  block_alignment_mcc.json (h2conv/data) was measured on only 2-5 instances per
    path (it reports the LARGEST alignment all instances happen to satisfy: ltmp Section Palette 128 although the builder uses 64), so
    wherever the Vista table (43 maps, up to thousands of instances per path; the same builder rule) is consistent with EVERY MCC
    instance, the Vista value wins.  Where the MCC data contradict it (matg AI Globals / Grenades / Sounds / Camera / Gravemind
    Properties, clwd Strip Indices: 9 paths) or the path exists only in MCC, the MCC value stays; a path with no MCC data takes the
    Vista value; unknown paths and the 3 jmad paths without a consistent rule default to 4."""
    with open(MCC_ALIGN_PATH, 'r', encoding='utf-8') as fh:
        M = json.load(fh)
    with open(VISTA_ALIGN_PATH, 'r', encoding='utf-8') as fh:
        V = json.load(fh)
    out = {}
    for k, m in M.items():
        v = V.get(k)
        va = v.get('align') if v else None
        ma = m.get('align')
        if va and m.get('consistent', {}).get(str(va)) == m.get('n'):
            out[k] = va
        elif ma:
            out[k] = ma
    for k, v in V.items():
        if k not in out and v.get('align') and k not in M:
            out[k] = v['align']
    return out


def mcc_align_table():
    if not _ALIGN:
        _ALIGN.append(T.AlignTable(merged_alignment()))
    return _ALIGN[0]


class VistaRefs(object):
    """The Vista maps used as sources (shared databases) and donors."""

    def __init__(self, maps_dir=None):
        from ..vista_cache import VistaCache
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

    def get(self, key):
        return {'shared': self.shared, 'mainmenu': self.mainmenu, 'single_player_shared': self.sp_shared}[key]


class MccRefs(dict):
    """{'shared','single_player_shared','mainmenu'} -> MccCache|None, opened lazily (decompressed stock MCC maps).
    Search order: explicit dirs, <work folder>/mcc_decompressed, the stock MCC folder."""
    NAMES = {'shared': 'shared.map', 'single_player_shared': 'single_player_shared.map', 'mainmenu': 'mainmenu.map'}

    def __init__(self, dirs=()):
        dict.__init__(self)
        self.dirs = [d for d in dirs if d] + [d for d in (MCC_DECOMP, _paths.mcc_maps_dir(required=False)) if d]

    def get(self, key, default=None):
        if key not in self:
            from ..mcc_cache import MccCache
            cache = None
            for d in self.dirs:
                p = os.path.join(d, self.NAMES[key])
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


# ================================================================================================
# MCC output string table
# ================================================================================================
class McStringTable(object):
    """Output string-id table of the MCC map.  MCC string_id = (len(utf8 text) << 24) | index (FACTS [container]); the MCC
    shared.map table is an exact PREFIX of every stock MP map's table (headlong: 12864 + 455), so a map that links to
    shared.map must keep it frozen.  Self-contained maps may use any table; the prefix is kept anyway (harmless)."""

    MAX_IDS = 0x8000

    def __init__(self, base_strings=(), max_ids=MAX_IDS):
        self.max_ids = max_ids
        self._strings = list(base_strings)
        self.base_count = len(self._strings)
        self._index = {}
        for i, s in enumerate(self._strings):
            self._index.setdefault(s, i)

    @classmethod
    def from_mcc_shared(cls, shared, **kw):
        """shared: a path or an MccCache of shared.map."""
        if isinstance(shared, str):
            from ..mcc_cache import MccCache
            c = MccCache(shared)
            return cls([s if s is not None else '' for s in c.string_ids], **kw)
        return cls([s if s is not None else '' for s in shared.string_ids], **kw)

    @property
    def strings(self):
        return self._strings

    def __len__(self):
        return len(self._strings)

    @property
    def appended(self):
        return self._strings[self.base_count:]

    def has(self, text):
        return text in self._index

    def sid(self, text):
        if not text:
            return 0
        i = self._index.get(text)
        if i is None:
            n = len(text.encode('utf-8'))
            if n > 255:
                raise ValueError('string of %d bytes does not fit the 8-bit length of a string id: %r' % (n, text[:40]))
            if len(self._strings) + 1 > self.max_ids:
                raise ValueError('too many string ids (%d)' % self.max_ids)
            i = len(self._strings)
            self._strings.append(text)
            self._index[text] = i
        return (len(text.encode('utf-8')) << 24) | i

    def text(self, sid):
        if sid in (0, NULL):
            return ''
        return self._strings[sid & 0xFFFFFF]


# ================================================================================================
# RevCtx
# ================================================================================================
class RevCtx(object):
    """Reverse conversion context (the names mirror transcode.Ctx; 'mcc'/'vista' roles are swapped).

    src            VistaCache of the input map
    vista_refs     VistaRefs: .shared .mainmenu .sp_shared .maps_dir .open(name) .get(key)
    mcc_refs       MccRefs: {'shared', 'single_player_shared', 'mainmenu'} -> MccCache|None  (decompressed stock MCC maps)
    plan           rev planner Plan (entries, by_src, remap_datum(src_map, vista_datum) -> MCC datum, ...)
    strings        McStringTable (output MCC string table)
    lay_vista / lay_mcc   {fourcc: Layout} (corrected=True)       align = MCC AlignTable
    opts, map_type (0 solo | 1 multiplayer | 2 mainmenu ...), stats, warnings, raws
    cur_src        'src' | 'shared' | 'single_player_shared' | 'mainmenu': the Vista map the tag being converted comes
                   from; datum / string-id remapping is relative to it
    cur_entry      the plan entry being converted (None outside convert_tag)
    """

    MAX_WARN_PER_CODE = 25

    def __init__(self, src, vista_refs=None, mcc_refs=None, plan=None, strings=None, opts=None,
                 lay_vista=None, lay_mcc=None, align=None, map_type=None):
        self.src = src
        self.vista_refs = vista_refs if vista_refs is not None else VistaRefs()
        self.vista = self.vista_refs                      # alias: same attribute name as the forward Ctx
        self.mcc_refs = mcc_refs if mcc_refs is not None else MccRefs()
        self.plan = plan
        self.strings = strings
        self.opts = dict(opts or {})
        self.lay_vista = lay_vista if lay_vista is not None else layouts('vista')
        self.lay_mcc = lay_mcc if lay_mcc is not None else layouts('mcc')
        self.align = align if align is not None else mcc_align_table()
        self.map_type = map_type if map_type is not None else src.header.type
        self.stats = collections.Counter()
        self.warnings = collections.OrderedDict()        # code -> [(msg, tag)]
        self.raws = []                                   # [(kind, bytes)]
        self.cur_src = 'src'
        self.cur_entry = None
        self.extra = {}
        self._dcache = {}
        self._scache = {}
        self._donor_cache = {}
        self._resolved = None

    # ---- names the handler helpers are written against (h2conv/rev/handlers/_revmisc_common.py) -----------
    @property
    def mcc_shared(self):
        return self.mcc_refs

    def src_string(self, vista_sid):
        """Text of a Vista string id of the map being converted ('' for 0 / null)."""
        return self.vista_string(vista_sid)

    def mcc_donor(self, group, name):
        """(Node, MccCache) of MCC's own tag with this (group, name) - the planner's donor tiers (shared.map, single_player_shared,
        mainmenu, stock maps, the H2EK maps for shader code) - decoded with the MCC layouts; (None, None) if there is none.
        `opts['rev_no_donor']` disables the lookup."""
        if self.opts.get('rev_no_donor') or not name:
            return None, None
        key = (group, name.lower())
        hit = self._donor_cache.get(key)
        if hit is None:
            hit = (None, None)
            env = getattr(self.plan, 'env', None)
            try:
                d = env.mcc.find(group, name) if env is not None else None
                if d is not None:
                    cache, tag = env.mcc.tag(d)
                    hit = (self.decode_mcc(cache, tag), cache)
            except Exception as ex:                                  # noqa: BLE001
                self.warn('mcc_donor_failed', '%s %s: %r' % (group, name, ex))
            self._donor_cache[key] = hit
        return hit

    # ---- source maps ------------------------------------------------------------------------------------
    def cache_for(self, src_map):
        """VistaCache of a source map name ('src' = the input map)."""
        if src_map in (None, 'src'):
            return self.src
        return self.vista_refs.get(src_map)

    @property
    def cur_cache(self):
        return self.cache_for(self.cur_src)

    # ---- ids ----------------------------------------------------------------------------------------------
    def remap_datum(self, vista_datum):
        """MCC output datum of a Vista datum (relative to ctx.cur_src); 0xFFFFFFFF if null / dropped."""
        if vista_datum is None or vista_datum == NULL:
            return NULL
        key = (self.cur_src, vista_datum)
        r = self._dcache.get(key)
        if r is None:
            r = self.plan.remap_datum(self.cur_src, vista_datum)
            if r is None:
                r = NULL
            self._dcache[key] = r
        return r

    def remap_name(self, group, name):
        """Output datum of the planned tag with this (group, name), NULL if none."""
        return self.plan.remap_name(group, name)

    def vista_tag(self, vista_datum, src_map=None):
        c = self.cache_for(src_map or self.cur_src)
        i = vista_datum & 0xFFFF
        if i < len(c.tags) and c.tags[i].datum == vista_datum:
            return c.tags[i]
        return None

    def vista_group_word(self, vista_datum):
        """The 'group' dword (as stored in tag references) of the Vista tag with this datum, 0xFFFFFFFF if unknown."""
        t = self.vista_tag(vista_datum)
        if t is None or not t.group:
            return NULL
        return _fcc(t.group)

    def vista_tag_label(self, vista_datum):
        t = self.vista_tag(vista_datum)
        if t is None:
            return '<datum 0x%08X not in table>' % vista_datum
        return '%s %s' % (t.group, t.name)

    def vista_string(self, vista_sid):
        if not vista_sid or vista_sid == NULL:
            return ''
        return self.cur_cache.string_id(vista_sid)

    def sid(self, text):
        """MCC string id for `text` (appended to the output table when missing)."""
        if not text:
            return 0
        return self.strings.sid(text)

    def remap_sid(self, vista_sid):
        if not vista_sid or vista_sid == NULL:
            return vista_sid or 0
        key = (self.cur_src, vista_sid)
        r = self._scache.get(key)
        if r is None:
            try:
                r = self.sid(self.vista_string(vista_sid))
            except Exception as ex:                                  # noqa: BLE001  junk id in an unused/opaque field
                self.warn('bad_stringid', '%s 0x%08X: %s' % (self.cur_src, vista_sid, ex))
                r = 0
            self._scache[key] = r
        return r

    # ---- decode -------------------------------------------------------------------------------------------
    def decode_vista(self, tag, cache=None):
        """Decode a Vista tag with the Vista layouts.  A shared placeholder (addr == 0, index >= 10000) is resolved in the
        Vista shared databases by (index, datum, group)."""
        c = cache or self.cur_cache
        if not tag.addr:
            for nm in ('shared', 'single_player_shared', 'mainmenu'):
                try:
                    sh = self.vista_refs.get(nm)
                except Exception:                                    # noqa: BLE001
                    continue
                if tag.index >= len(sh.tags):
                    continue
                st = sh.tags[tag.index]
                if st.datum == tag.datum and st.group == tag.group and st.addr:
                    tag, c = st, sh
                    break
            else:
                raise KeyError('Vista placeholder #%d %s %s not found in any shared map' % (tag.index, tag.group, tag.name))
        return T.decode_tag(c.read_addr, self.lay_vista[tag.group], tag.addr, tag.group)

    def decode_mcc(self, cache, tag):
        """Decode an MCC tag of an MCC reference map (stock shared maps / donors) with the MCC layouts."""
        c = cache
        if tag.external:
            for nm in ('shared', 'single_player_shared', 'mainmenu'):
                sh = self.mcc_refs.get(nm)
                if sh is None or tag.index >= len(sh.tags):
                    continue
                st = sh.tags[tag.index]
                if st.datum == tag.datum and st.group == tag.group and st.has_data:
                    tag, c = st, sh
                    break
            else:
                raise KeyError('external MCC tag #%d %s %s not found in any shared map' % (tag.index, tag.group, tag.name))
        return T.decode_tag(lambda a, n: c.read_at_addr(a, n), self.lay_mcc[tag.group], tag.addr, tag.group)

    # ---- raw resources --------------------------------------------------------------------------------------
    def add_raw(self, kind, data, align=0x80):
        """Register a raw payload ('sound' | 'geometry' | 'bitmap') and return a token for node.patches[...]."""
        if kind not in ('sound', 'geometry', 'bitmap'):
            raise ValueError('unknown raw kind %r' % kind)
        self.raws.append((kind, data if isinstance(data, bytes) else bytes(data)))
        return ('raw', len(self.raws) - 1)

    def resolve(self, token):
        """Final MCC raw pointer of a token (valid once the driver placed the raw regions)."""
        if self._resolved is None:
            raise RuntimeError('raw regions are not placed yet')
        return self._resolved[token[1]]

    # ---- reporting ------------------------------------------------------------------------------------------
    def warn(self, code, msg, tag=None):
        self.stats['warn:' + code] += 1
        lst = self.warnings.get(code)
        if lst is None:
            lst = self.warnings[code] = []
        if len(lst) < self.MAX_WARN_PER_CODE:
            lst.append((msg, tag if tag is not None else (self.cur_entry.name if self.cur_entry is not None else None)))

    def note_dropped_ref(self, node, fname, vista_datum):
        if len(self.warnings.get('tagref_dropped', ())) < self.MAX_WARN_PER_CODE:
            self.warn('tagref_dropped', '%s|%s %s -> %s' % (node.group, node.path, fname, self.vista_tag_label(vista_datum)))
        else:
            self.stats['warn:tagref_dropped'] += 1
