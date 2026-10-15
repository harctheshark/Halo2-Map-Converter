# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Havok / physics handlers, REVERSE direction: Halo 2 Vista (cache v8, 32-bit Havok images) -> Halo 2 MCC (cache v13, 64-bit).

Inverse of the forward handlers/havok.py.

What changes between the games
------------------------------
Every Havok shape record is the in-memory image of a C++ object.  Vista stores 32-bit pointers (uint32 vtable, uint32 user
data), MCC stores the 64-bit image (uint64 vtable, uint64 user data, and a hidden `Runtime Shape Pointer Pad` dword after
every (shape type, shape index) pair that the engine overwrites in place with the shape POINTER).  Element sizes therefore
GROW from Vista to MCC: phmo Rigid Bodies 0x90 -> 0xA0, Spheres 0x80 -> 0xA0, Polyhedra 0x100 -> 0x120, Lists 0x38 -> 0x68,
coll/sbsp BSP Physics 0x70 -> 0xA0, vehi Phantom Shapes 0xF0 -> 0x130, phys root 0x74 -> 0x80 ...

The MCC engine rewrites every one of those runtime words at tag load, exactly like halo2.exe does:
`havok_component_build_shape_from_model` (halo2.dll 0x18070CBC0) dereferences the qword at rigid body +0x38 as a hkShape pointer
and calls vtable slot +0x28 (getType) on it, while the tag file holds (type u16, index u16, pad u32) there - so a load-time pass
must replace it.  The constants a tag carries are addresses inside the TOOL process that wrote it; they only matter for byte
equality with a stock map.

Per-field policy (the inverse of the forward table; pairing from plugins.diff_fields on the two corrected layouts):

    matched, equal size            byte copy (stringid -> ctx.remap_sid, tagref/datum -> ctx.remap_datum)
    'Runtime Code Pointer' u32->u64   the per-class constant of the MCC tool (RCP_H2EK, default; RCP_STOCK = the stock 343
                                      build; 0 with opts['rev_havok_rcp']='zero') when the Vista pointer is non-zero, else 0
    'User Data' u32 -> u64         zero-extended when the Vista value is a small integer (< 0x10000), else 0
    other runtime pointers         0
    tool-heap garbage dwords       0   (JUNK_DWORDS, Vista offsets)
    MCC-only fields (pads, hidden Runtime Shape Pointer Pad, phys Runtime Pointer)   0
    vehi Phantom Shapes child slots  all zero (MCC unused pattern: type 0, index 0, filter 0; used slots are rewritten by the engine)

MOPP code is copied verbatim (the Vista and MCC MOPP VMs share one grammar and one 0x30-byte header).

Public: repack(ctx, vista_node, mcc_node) (registered as node handler for every owned path), RCP_H2EK, RCP_STOCK, OWNED,
JUNK_DWORDS, describe_plan().
"""
import collections
import re
import struct

from . import node_handler
from ... import plugins as P
from . import _revmisc_common as C

NULL = 0xFFFFFFFF
BLOCK_TYPES = ('tagblock', 'reflexive')

# which blocks are ours (same names and paths in both layouts)
_UNIT_SHAPES = ['/Dead Sphere Shapes', '/Pill Shapes', '/Sphere Shapes']
OWNED = collections.OrderedDict([
    ('bipd', list(_UNIT_SHAPES)),
    ('crea', list(_UNIT_SHAPES)),
    ('coll', ['/Regions/Permutations/BSP Physics']),
    ('phmo', ['/Rigid Bodies', '/Spheres', '/Multi Spheres', '/Pills', '/Boxes', '/Triangles', '/Polyhedra', '/Lists',
              '/List Shapes', '/MOPPs', '/Phantoms']),
    ('sbsp', ['/Instanced Geometry Definitions/BSP Physics']),
    ('vehi', ['/Phantom Shapes']),
    ('phys', ['']),
])

# ------------------------------------------------------------------------------------------------
# MCC constants.  Key = (group, path, MCC field offset).  Measured over the 43 stock MCC maps + the H2EK-built map.
# Two tool builds, both accepted by halo2.dll:
#   stock  0x00DBE...  (343's internal tool that built the shipped maps; 100% of the stock maps)
#   h2ek   0x00D7B...  (the public H2EK tool.exe: every Havok tag of an H2EK-built custom headlong)
# 0xDBE3AC/0xDBE3F0 are sphere / translate wrapper, 0xDBE368 pill, 0xDBE41C box, 0xDBE4A4 polyhedron, 0xDBE4E8 list,
# 0xDBE530 MOPP, 0xDBE308/0xDBE2D4 phantom, 0xDEFB9C / 0xE60EA0 BSP physics; the H2EK values are the same classes at -0x42748
# (BSP: 0xDAD4F0 / 0xE1E940).
# ------------------------------------------------------------------------------------------------
_S = dict(sphere=0xDBE3AC, wrap=0xDBE3F0, pill=0xDBE368, box=0xDBE41C, poly=0xDBE4A4, lst=0xDBE4E8, mopp=0xDBE530,
          ph_a=0xDBE308, ph_b=0xDBE2D4, bsp_a=0xDEFB9C, bsp_b=0xE60EA0, veh_ms=0xDBE334)
_H = dict(sphere=0xD7BC64, wrap=0xD7BCA8, pill=0xD7BC20, box=0xD7BCD4, poly=0xD7BD5C, lst=0xD7BDA0, mopp=0xD7BDE8,
          ph_a=0xD7BBC0, ph_b=0xD7BB8C, bsp_a=0xDAD4F0, bsp_b=0xE1E940, veh_ms=0xD7BBEC)


def _rcp_table(K):
    t = {}
    for g in ('bipd', 'crea'):
        t[(g, '/Dead Sphere Shapes', 0x20)] = K['sphere']
        t[(g, '/Dead Sphere Shapes', 0x40)] = K['wrap']
        t[(g, '/Sphere Shapes', 0x20)] = K['sphere']
        t[(g, '/Sphere Shapes', 0x40)] = K['wrap']
        t[(g, '/Pill Shapes', 0x20)] = K['pill']
    t.update({
        ('phmo', '/Spheres', 0x20): K['sphere'],
        ('phmo', '/Spheres', 0x40): K['wrap'],
        ('phmo', '/Pills', 0x20): K['pill'],
        ('phmo', '/Boxes', 0x20): K['box'],
        ('phmo', '/Boxes', 0x50): K['wrap'],
        ('phmo', '/Polyhedra', 0x20): K['poly'],
        ('phmo', '/Lists', 0x00): K['lst'],
        ('phmo', '/MOPPs', 0x00): K['mopp'],
        ('phmo', '/Phantoms', 0x00): K['ph_a'],
        ('phmo', '/Phantoms', 0x28): K['ph_b'],
        ('coll', '/Regions/Permutations/BSP Physics', 0x00): K['bsp_a'],
        ('coll', '/Regions/Permutations/BSP Physics', 0x50): K['bsp_b'],
        ('coll', '/Regions/Permutations/BSP Physics', 0x70): K['mopp'],
        ('sbsp', '/Instanced Geometry Definitions/BSP Physics', 0x00): K['bsp_a'],
        ('sbsp', '/Instanced Geometry Definitions/BSP Physics', 0x50): K['bsp_b'],
        ('sbsp', '/Instanced Geometry Definitions/BSP Physics', 0x70): K['mopp'],
        ('vehi', '/Phantom Shapes', 0x00): K['lst'],
        ('vehi', '/Phantom Shapes', 0x90): K['veh_ms'],
    })
    return t


RCP_STOCK = _rcp_table(_S)
RCP_H2EK = _rcp_table(_H)

# dwords (VISTA offsets) that hold tool-heap pointers in both games; the engine's constructors write them -> 0
_BSP_JUNK = frozenset([0x0C, 0x4C, 0x5C, 0x60])
JUNK_DWORDS = {
    ('coll', '/Regions/Permutations/BSP Physics'): _BSP_JUNK,
    ('sbsp', '/Instanced Geometry Definitions/BSP Physics'): _BSP_JUNK,
    ('phmo', '/Phantoms'): frozenset([0x10]),
}

# vehi Phantom Shapes: child slots (type 0 index 0 filter 0 when unused; the engine writes pointers into used ones)
_VEHI_SLOT = re.compile(r'^(Shape Type|Shape Index|Collision Filter) [0-3]$')

_RE_RCP = re.compile(r'^runtime code pointer$', re.I)
_RE_USER = re.compile(r'^user data$', re.I)
_RE_RTPTR = re.compile(r'runtime .*pointer$|^runtime pointer$', re.I)

_PLAN_CACHE = {}


def _entries(mfields, vfields):
    k = (id(mfields), id(vfields))
    hit = _PLAN_CACHE.get(k)
    if hit is None or hit[0] is not mfields or hit[1] is not vfields:
        hit = (mfields, vfields, P.diff_fields(mfields, vfields))
        _PLAN_CACHE[k] = hit
    return hit[2]


def _kind(field):
    n = field.name or ''
    if _RE_RCP.match(n):
        return 'rcp'
    if _RE_USER.match(n):
        return 'user'
    if _RE_RTPTR.search(n):
        return 'rtptr'
    return None


def _small_user_data(v):
    return v if v < 0x10000 else 0


def rcp_table(ctx):
    mode = C.opt(ctx, 'rev_havok_rcp', 'h2ek')
    if mode == 'zero':
        return {}
    return RCP_STOCK if mode == 'stock' else RCP_H2EK


def repack(ctx, vista_node, mcc_node):
    """Rebuild mcc_node.data (the MCC element/struct) from vista_node.data.  Blocks are left to the generic converter
    (their count/pointer words are kept); datarefs are carried over from the Vista node (MOPP code is copied verbatim)."""
    group, path = mcc_node.group, mcc_node.path
    rcp = rcp_table(ctx)
    junk = JUNK_DWORDS.get((group, path), ())
    ents = _entries(mcc_node.fields, vista_node.fields)
    old = bytes(mcc_node.data)
    new = bytearray(len(mcc_node.data))
    vd = vista_node.data
    for e in ents:
        m, v = e['_m'], e['_v']
        if m is None:
            continue                                     # Vista-only field: dropped
        mo = m.offset
        if v is None:
            continue                                     # MCC-only field (pad / hidden pointer): stays zero
        vo = v.offset
        if m.children is not None or m.type in BLOCK_TYPES:
            new[mo:mo + 8] = old[mo:mo + 8]              # block header: the encoder rewrites the pointer word
            continue
        if m.type == 'dataref':
            new[mo:mo + 8] = old[mo:mo + 8]
            mcc_node.datas[mo] = vista_node.datas.get(vo) or b''
            continue
        kind = _kind(m)
        if m.size == 8 and v.size == 4 and (kind is not None or m.type == 'uint64'):
            vval = struct.unpack_from('<I', vd, vo)[0]
            if kind == 'rcp':
                val = rcp.get((group, path, mo), 0) if vval else 0
            elif kind == 'user':
                val = _small_user_data(vval)
            else:
                val = 0
            struct.pack_into('<Q', new, mo, val)
            continue
        if m.size != v.size:
            C.warn(ctx, 'rev_havok_size_mismatch', '%s%s %s: Vista %d B vs MCC %d B' % (group, path, m.name, v.size, m.size))
            continue
        if vo in junk:
            continue                                     # tool-heap garbage -> 0
        if m.type == 'stringid':
            struct.pack_into('<I', new, mo, ctx.remap_sid(struct.unpack_from('<I', vd, vo)[0]))
        elif m.type == 'tagref' and m.size == 8:
            g, d = struct.unpack_from('<II', vd, vo)
            nd = ctx.remap_datum(d) if d != NULL else NULL
            if nd == NULL:
                g = NULL
            struct.pack_into('<II', new, mo, g, nd)
        elif m.type in ('tagref', 'datum') and m.size == 4:
            d = struct.unpack_from('<I', vd, vo)[0]
            struct.pack_into('<I', new, mo, ctx.remap_datum(d) if d != NULL else NULL)
        else:
            new[mo:mo + m.size] = vd[vo:vo + v.size]
    if group == 'vehi' and path == '/Phantom Shapes':
        for f in mcc_node.fields:
            if f.name and _VEHI_SLOT.match(f.name):
                new[f.offset:f.offset + f.size] = b'\0' * f.size
    mcc_node.data[:] = new
    C.stat(ctx, 'rev_havok_repacked')
    if C.opt(ctx, 'havok_validate_mopp', True):
        for off, payload in mcc_node.datas.items():
            f = next((x for x in mcc_node.fields if x.offset == off), None)
            if f is not None and 'mopp' in (f.name or '').lower() and payload:
                _check_mopp(ctx, payload, '%s%s %s' % (group, path, f.name))


def describe_plan(mfields, vfields):
    """Action table of the pairing repack() uses: (status, vista_offset, vista_size, name, type, mcc_offset)."""
    out = []
    for e in _entries(mfields, vfields):
        m, v = e['_m'], e['_v']
        if v is None:
            out.append(('zero', None, None, m.name, m.type, m.offset))
        elif m is None:
            out.append(('drop', v.offset, v.size, v.name, v.type, None))
        else:
            out.append((e['status'], v.offset, v.size, v.name, v.type, m.offset))
    return out


# repack() writes every runtime-pointer / User Data field of its paths itself: the generic pass's `resize_default`
# warning (uint32 -> uint64, zero-extended) is noise there (same attribute the forward owner uses).
repack.owns_resize = True
for _g, _paths in OWNED.items():
    for _p in _paths:
        node_handler(_g, _p)(repack)


# ------------------------------------------------------------------------------------------------
# MOPP validation (the grammar is the forward module's; both games use it)
# ------------------------------------------------------------------------------------------------
def _mopp_lib():
    from ...handlers import havok as FH           # forward module: pure functions mopp_validate / mopp_sections
    return FH


def _check_mopp(ctx, payload, where):
    FH = _mopp_lib()
    secs = FH.mopp_sections(payload) if where.startswith('phmo') else [payload]
    for s in secs:
        rep = FH.mopp_validate(s)
        C.stat(ctx, 'rev_havok_mopp_blobs')
        if not rep.ok:
            C.stat(ctx, 'rev_havok_mopp_invalid')
            C.warn(ctx, 'rev_havok_mopp_invalid', '%s: %s' % (where, rep.reason))


def _check_mopp_fields(*names):
    low = [n.lower() for n in names]

    def check(ctx, vista_node, mcc_node):
        if not C.opt(ctx, 'havok_validate_mopp', True):
            return
        for f in mcc_node.fields:
            if f.type == 'dataref' and (f.name or '').lower() in low:
                payload = mcc_node.datas.get(f.offset) or b''
                if payload:
                    _check_mopp(ctx, payload, '%s%s %s' % (mcc_node.group, mcc_node.path, f.name))
    check.__name__ = 'mopp_check_' + '_'.join(n.replace(' ', '') for n in names)
    return check


node_handler('phmo', '', prio=200)(_check_mopp_fields('MOPP Codes'))
node_handler('sbsp', '', prio=200)(_check_mopp_fields('MOPP Codes', 'Breakable Surface MOPP Codes'))
node_handler('sbsp', '/Clusters', prio=200)(_check_mopp_fields('Collision MOPP Codes'))
