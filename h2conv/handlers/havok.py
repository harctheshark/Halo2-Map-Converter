# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Havok / physics handlers: MCC (v13, 64-bit Havok) -> Halo 2 Vista (v8, 32-bit Havok).

What this module does
---------------------
Every Havok shape record in an MCC tag is a 64-bit image of the object (uint64 vtable pointer, uint64 user data,
hidden pad dwords after the 'shape type/index' pairs).  Vista stores the 32-bit image.  The records appear in

    phmo   Rigid Bodies Spheres 'Multi Spheres' Pills Boxes Triangles Polyhedra Lists 'List Shapes' MOPPs Phantoms
    coll   Regions/Permutations/BSP Physics
    sbsp   Instanced Geometry Definitions/BSP Physics
    bipd   Dead Sphere Shapes, Pill Shapes, Sphere Shapes           (same for crea)
    vehi   Phantom Shapes
    phys   root (one extra uint64 'Runtime Pointer')

One node handler per path rebuilds the Vista element from the MCC element (idempotent: the result depends only on the
MCC node, never on what the generic conversion put into the Vista node).  Field pairing comes from
plugins.diff_fields() on the two corrected layouts, so offsets are never hand-typed.  Per-field policy:

    matched, equal size            byte copy (stringid -> ctx.remap_sid, tagref/datum -> ctx.remap_datum)
    'Runtime Code Pointer'         Vista per-class constant (RCP_VISTA) when the MCC pointer is non-zero, else 0
    'User Data'                    low dword when the MCC value is a small integer (< 0x10000), else 0
    other runtime pointers         0
    tool-heap garbage dwords       0   (JUNK_DWORDS)
    MCC-only fields (pads, hidden) dropped;  Vista-only fields  0
    vehi Phantom Shapes child slots  (type 0, index 0xFFFF, filter 0)  = the Vista 'unused' pattern

MOPP code (phmo 'MOPP Codes', coll/sbsp 'MOPP Code Data', 'Collision MOPP Codes', ...) is copied verbatim.  The
engine's MOPP interpreter (halo2.exe 0x6DF589) accepts every MCC blob; mopp_validate() re-checks that on the way
through and reports problems with ctx.warn('havok_mopp_invalid', ...).

Public helpers: repack(), RCP_VISTA, JUNK_DWORDS, mopp_validate(), mopp_terminal_ids(), mopp_sections(),
describe_plan().
"""
import collections
import re
import struct

from . import node_handler
from .. import plugins as P

NULL = 0xFFFFFFFF
BLOCK_TYPES = ('tagblock', 'reflexive')

# ------------------------------------------------------------------------------------------------
# which blocks are ours
# ------------------------------------------------------------------------------------------------
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
# Vista constants (all 43 stock Vista maps).  Key = (group, path, VISTA field offset).
# The values are addresses inside the TOOL process (H2Tool) that built the stock maps; halo2.exe overwrites every one
# of them at tag load (physics_model_definitions_fixup 0x47B844, collision_model_definitions_fixup 0x47BE5C,
# vehicle/biped/creature_definitions_fixup 0x53895A/0x5389B0/0x538985, sbsp collision fixup), so they only matter for
# byte-equality with the stock maps.  'Multi Spheres' and 'Triangles' never occur in a stock Vista map -> no constant.
# ------------------------------------------------------------------------------------------------
RCP_SPHERE = 0x00C3045C
RCP_TRANSFORM = 0x00C304A0       # the translate/rotate wrapper shape that follows a sphere / box
RCP_PILL = 0x00C30414
RCP_BOX = 0x00C304CC
RCP_POLYHEDRON = 0x00C30894
RCP_LIST = 0x00C308DC
RCP_MOPP = 0x00C30558
RCP_PHANTOM_A = 0x00C303B4
RCP_PHANTOM_B = 0x00C30380
RCP_BSP_A = 0x00C7529C
RCP_BSP_B = 0x00CE0F3C
RCP_VEH_MULTISPHERE = 0x00C303E0

RCP_VISTA = {}
for _g in ('bipd', 'crea'):
    RCP_VISTA[(_g, '/Dead Sphere Shapes', 0x20)] = RCP_SPHERE
    RCP_VISTA[(_g, '/Dead Sphere Shapes', 0x30)] = RCP_TRANSFORM
    RCP_VISTA[(_g, '/Sphere Shapes', 0x20)] = RCP_SPHERE
    RCP_VISTA[(_g, '/Sphere Shapes', 0x30)] = RCP_TRANSFORM
    RCP_VISTA[(_g, '/Pill Shapes', 0x20)] = RCP_PILL
RCP_VISTA.update({
    ('phmo', '/Spheres', 0x20): RCP_SPHERE,
    ('phmo', '/Spheres', 0x30): RCP_TRANSFORM,
    ('phmo', '/Pills', 0x20): RCP_PILL,
    ('phmo', '/Boxes', 0x20): RCP_BOX,
    ('phmo', '/Boxes', 0x40): RCP_TRANSFORM,
    ('phmo', '/Polyhedra', 0x20): RCP_POLYHEDRON,
    ('phmo', '/Lists', 0x00): RCP_LIST,
    ('phmo', '/MOPPs', 0x00): RCP_MOPP,
    ('phmo', '/Phantoms', 0x00): RCP_PHANTOM_A,
    ('phmo', '/Phantoms', 0x14): RCP_PHANTOM_B,
    ('coll', '/Regions/Permutations/BSP Physics', 0x00): RCP_BSP_A,
    ('coll', '/Regions/Permutations/BSP Physics', 0x40): RCP_BSP_B,
    ('coll', '/Regions/Permutations/BSP Physics', 0x50): RCP_MOPP,
    ('sbsp', '/Instanced Geometry Definitions/BSP Physics', 0x00): RCP_BSP_A,
    ('sbsp', '/Instanced Geometry Definitions/BSP Physics', 0x40): RCP_BSP_B,
    ('sbsp', '/Instanced Geometry Definitions/BSP Physics', 0x50): RCP_MOPP,
    ('vehi', '/Phantom Shapes', 0x00): RCP_LIST,
    ('vehi', '/Phantom Shapes', 0x60): RCP_VEH_MULTISPHERE,
})

# dwords that hold tool-heap pointers in BOTH games (distinct value in every element, all 0x0C000000..0x2F000000);
# the engine's constructors write them (4C5036 -> 4F9293 / 47B642 / 4C4F9D).  Vista offsets.
_BSP_JUNK = frozenset([0x0C, 0x4C, 0x5C, 0x60])
JUNK_DWORDS = {
    ('coll', '/Regions/Permutations/BSP Physics'): _BSP_JUNK,
    ('sbsp', '/Instanced Geometry Definitions/BSP Physics'): _BSP_JUNK,
    ('phmo', '/Phantoms'): frozenset([0x10]),                 # 'Collision Filter' slot after the shape type/index
}

# vehi Phantom Shapes: the four child slots are (type 0, index 0xFFFF, filter 0) when unused; for used elements the
# engine (0x511C8E) rewrites them from the multi-sphere count.  MCC writes index 0 for unused slots.
_VEHI_SLOT = re.compile(r'^(Shape Type|Shape Index|Collision Filter) [0-3]$')

_RE_RCP = re.compile(r'^runtime code pointer$', re.I)
_RE_USER = re.compile(r'^user data$', re.I)
_RE_RTPTR = re.compile(r'runtime .*pointer$|^runtime pointer$', re.I)

# ------------------------------------------------------------------------------------------------
# field pairing (cached)
# ------------------------------------------------------------------------------------------------
_PLAN_CACHE = {}


def _entries(mfields, vfields):
    k = (id(mfields), id(vfields))
    hit = _PLAN_CACHE.get(k)
    if hit is None or hit[0] is not mfields or hit[1] is not vfields:
        hit = (mfields, vfields, P.diff_fields(mfields, vfields))
        _PLAN_CACHE[k] = hit
    return hit[2]


def _opt(ctx, key, default):
    opts = getattr(ctx, 'opts', None) or {}
    return opts.get(key, default)


def _small_user_data(mval):
    return mval & 0xFFFFFFFF if mval < 0x10000 else 0


def _kind(vfield):
    n = vfield.name
    if _RE_RCP.match(n):
        return 'rcp'
    if _RE_USER.match(n):
        return 'user'
    if _RE_RTPTR.search(n):
        return 'rtptr'
    return None


def repack(ctx, mnode, vnode):
    """Rebuild vnode.data (the Vista element/struct) from mnode.data.  Blocks are left to the generic converter
    (pointer words are kept); datarefs are carried over from the MCC node (MOPP code is copied verbatim)."""
    group, path = vnode.group, vnode.path
    rcp_mode = _opt(ctx, 'havok_rcp', 'vista')
    junk = JUNK_DWORDS.get((group, path), ())
    ents = _entries(mnode.fields, vnode.fields)
    old = bytes(vnode.data)
    new = bytearray(len(vnode.data))
    md = mnode.data
    for e in ents:
        m, v = e['_m'], e['_v']
        if v is None or m is None:
            continue                                     # MCC-only: dropped.  Vista-only: stays zero
        vo, mo = v.offset, m.offset
        if v.children is not None or v.type in BLOCK_TYPES:
            new[vo:vo + 8] = old[vo:vo + 8]              # block header: pointer word is rewritten by the encoder
            continue
        if v.type == 'dataref':
            new[vo:vo + 8] = old[vo:vo + 8]
            vnode.datas[vo] = mnode.datas.get(mo) or b''
            continue
        kind = _kind(v)
        if m.size == 8 and v.size == 4:
            mval = struct.unpack_from('<Q', md, mo)[0]
            if kind == 'rcp':
                val = 0
                if mval and rcp_mode != 'zero':
                    val = RCP_VISTA.get((group, path, vo), 0)
            elif kind == 'user':
                val = _small_user_data(mval)
            else:
                val = 0
            struct.pack_into('<I', new, vo, val)
            continue
        if m.size != v.size:
            ctx.warn('havok_size_mismatch', '%s%s %s: MCC %d B vs Vista %d B' % (group, path, v.name, m.size, v.size))
            continue
        if vo in junk:
            continue                                     # tool-heap garbage -> 0
        if v.type == 'stringid':
            struct.pack_into('<I', new, vo, ctx.remap_sid(struct.unpack_from('<I', md, mo)[0]))
        elif v.type == 'tagref' and v.size == 8:
            g, d = struct.unpack_from('<II', md, mo)
            nd = ctx.remap_datum(d) if d != NULL else NULL
            if nd == NULL:
                g = NULL
            struct.pack_into('<II', new, vo, g, nd)
        elif v.type in ('tagref', 'datum') and v.size == 4:
            d = struct.unpack_from('<I', md, mo)[0]
            struct.pack_into('<I', new, vo, ctx.remap_datum(d) if d != NULL else NULL)
        else:
            new[vo:vo + v.size] = md[mo:mo + v.size]
    if group == 'vehi' and path == '/Phantom Shapes':
        for f in vnode.fields:
            if _VEHI_SLOT.match(f.name):
                if f.name.startswith('Shape Index'):
                    struct.pack_into('<H', new, f.offset, 0xFFFF)
                else:
                    new[f.offset:f.offset + f.size] = b'\0' * f.size
    vnode.data[:] = new
    ctx.stats['havok_repacked'] += 1
    if _opt(ctx, 'havok_validate_mopp', True):
        for off, payload in vnode.datas.items():
            f = next((x for x in vnode.fields if x.offset == off), None)
            if f is not None and 'mopp' in f.name.lower() and payload:
                _check_mopp(ctx, payload, '%s%s %s' % (group, path, f.name))


def describe_plan(mfields, vfields):
    """Human-readable action table of the pairing repack() uses (docs/tests)."""
    out = []
    for e in _entries(mfields, vfields):
        m, v = e['_m'], e['_v']
        if v is None:
            out.append(('drop', m.offset, m.size, m.name, m.type, None))
        elif m is None:
            out.append(('zero', None, None, v.name, v.type, v.offset))
        else:
            out.append((e['status'], m.offset, m.size, m.name, m.type, v.offset))
    return out


# ------------------------------------------------------------------------------------------------
# registration
# ------------------------------------------------------------------------------------------------
# repack() writes every Havok runtime-pointer / User Data field of the paths it owns itself, so the generic pass's
# 'resize_default' warning (uint64 -> uint32, low bytes kept) is noise there: transcode.convert_node skips it for handlers
# that carry this attribute (refined from "any node handler" so other groups still warn).
repack.owns_resize = True
for _g, _paths in OWNED.items():
    for _p in _paths:
        node_handler(_g, _p)(repack)


# ------------------------------------------------------------------------------------------------
# MOPP
# ------------------------------------------------------------------------------------------------
# Grammar read from the Vista engine's MOPP interpreter (halo2.exe 0x6DF589: opcode -> handler byte table at
# 0x6DFBDC, handler table at 0x6DFB5C; AABB-query virtual machine).  Header = 0x30 bytes (hkMoppCode image: offset
# vector, build info, u32 @0x20 = real byte length, u32 @0x24 = build type); the program starts at +0x30; blobs are
# padded to a multiple of 16 with 0xCD.
MOPP_START = 0x30


def mopp_op_length(op):
    """Length in bytes of MOPP instruction `op` in the Vista VM, 0 if the VM rejects the opcode."""
    if op == 0x00: return 1                                  # RETURN
    if 0x01 <= op <= 0x04: return 4                          # SCALE1..4 (+3 offset bytes)
    if op == 0x05: return 2                                  # JUMP8
    if op == 0x06: return 3                                  # JUMP16
    if op == 0x07: return 4                                  # JUMP24
    if op == 0x09: return 2                                  # DATA_OFFSET8  (add)
    if op == 0x0A: return 3                                  # DATA_OFFSET16 (add)
    if op == 0x0B: return 5                                  # DATA_OFFSET32 (set)
    if 0x10 <= op <= 0x1C: return 4                          # SPLIT_x / combos  (a, b, jump8)
    if 0x20 <= op <= 0x22: return 3                          # SINGLE_SPLIT     (v, jump8)
    if 0x23 <= op <= 0x25: return 7                          # DOUBLE_SPLIT     (a, b, jump16, jump16)
    if 0x26 <= op <= 0x28: return 3                          # CUT8             (lo, hi)
    if 0x29 <= op <= 0x2B: return 7                          # CUT24            (lo24, hi24)
    if 0x30 <= op <= 0x4F: return 1                          # TERM5
    if 0x50 <= op <= 0x53: return 2 + (op - 0x50)            # TERM8/16/24/32
    if 0x60 <= op <= 0x63: return 2                          # PROPERTY8
    if 0x64 <= op <= 0x67: return 3                          # PROPERTY16
    if 0x68 <= op <= 0x6B: return 5                          # PROPERTY32
    return 0


class MoppReport(object):
    __slots__ = ('ok', 'reason', 'ops', 'coverage', 'real_length')

    def __init__(self, ok, reason='', ops=None, coverage=0.0, real_length=0):
        self.ok, self.reason, self.ops, self.coverage, self.real_length = ok, reason, ops, coverage, real_length

    def __repr__(self):
        return '<MoppReport ok=%s %s cover=%.4f len=%d>' % (self.ok, self.reason, self.coverage, self.real_length)


def mopp_validate(b):
    """Walk every reachable instruction of one MOPP blob.  ok == all opcodes are accepted by the Vista VM, every
    jump target is inside the program, the walk never falls off the end, and (coverage) every program byte belongs to
    a reachable instruction (true for 100% of the 11,000+ stock blobs of both games)."""
    if len(b) <= MOPP_START:
        return MoppReport(False, 'blob shorter than header')
    real = struct.unpack_from('<I', b, 0x20)[0]
    if real > len(b) or real <= MOPP_START or (real + 15) // 16 * 16 != len(b):
        return MoppReport(False, 'length %d vs header %d' % (len(b), real), real_length=real)
    ops = collections.Counter()
    seen = set()
    cover = bytearray(real)
    stack = [MOPP_START]
    while stack:
        ip = stack.pop()
        while ip not in seen:
            if ip < MOPP_START or ip >= real:
                return MoppReport(False, 'target %d outside program (len %d)' % (ip, real), ops, real_length=real)
            seen.add(ip)
            op = b[ip]
            n = mopp_op_length(op)
            if n == 0:
                return MoppReport(False, 'opcode %02X at %d rejected by the VM' % (op, ip), ops, real_length=real)
            if ip + n > real:
                return MoppReport(False, 'operands of %02X at %d overrun' % (op, ip), ops, real_length=real)
            ops[op] += 1
            cover[ip:ip + n] = b'\1' * n
            if op == 0x00 or 0x30 <= op <= 0x53:
                break
            if op == 0x05:
                ip = ip + 2 + b[ip + 1]
            elif op == 0x06:
                ip = ip + 3 + (b[ip + 1] << 8 | b[ip + 2])
            elif op == 0x07:
                ip = ip + 4 + (b[ip + 1] << 16 | b[ip + 2] << 8 | b[ip + 3])
            elif 0x10 <= op <= 0x1C:
                stack.append(ip + 4 + b[ip + 3])
                ip += 4
            elif 0x20 <= op <= 0x22:
                stack.append(ip + 3 + b[ip + 2])
                ip += 3
            elif 0x23 <= op <= 0x25:
                stack.append(ip + 7 + (b[ip + 3] << 8 | b[ip + 4]))
                ip = ip + 7 + (b[ip + 5] << 8 | b[ip + 6])
            else:
                ip += n
    cov = sum(cover[MOPP_START:]) / float(real - MOPP_START)
    return MoppReport(True, '', ops, cov, real)


def mopp_terminal_ids(b):
    """Set of primitive ids a MOPP program can return (DATA_OFFSET scoped to its sub-tree, as in the VM)."""
    real = struct.unpack_from('<I', b, 0x20)[0]
    ids = set()
    seen = set()
    stack = [(MOPP_START, 0)]
    while stack:
        ip, off = stack.pop()
        while (ip, off) not in seen:
            if ip < MOPP_START or ip >= real:
                break
            seen.add((ip, off))
            op = b[ip]
            n = mopp_op_length(op)
            if n == 0 or op == 0x00:
                break
            if 0x30 <= op <= 0x4F:
                ids.add(off + op - 0x30)
                break
            if 0x50 <= op <= 0x53:
                v = 0
                for k in range(1, n):
                    v = v << 8 | b[ip + k]
                ids.add(off + v)
                break
            if op == 0x05:
                ip = ip + 2 + b[ip + 1]
                continue
            if op == 0x06:
                ip = ip + 3 + (b[ip + 1] << 8 | b[ip + 2])
                continue
            if op == 0x07:
                ip = ip + 4 + (b[ip + 1] << 16 | b[ip + 2] << 8 | b[ip + 3])
                continue
            if op == 0x09:
                off += b[ip + 1]
            elif op == 0x0A:
                off += b[ip + 1] << 8 | b[ip + 2]
            elif op == 0x0B:
                off = b[ip + 1] << 24 | b[ip + 2] << 16 | b[ip + 3] << 8 | b[ip + 4]
            if 0x10 <= op <= 0x1C:
                stack.append((ip + 4 + b[ip + 3], off))
                ip += 4
            elif 0x20 <= op <= 0x22:
                stack.append((ip + 3 + b[ip + 2], off))
                ip += 3
            elif 0x23 <= op <= 0x25:
                stack.append((ip + 7 + (b[ip + 3] << 8 | b[ip + 4]), off))
                ip = ip + 7 + (b[ip + 5] << 8 | b[ip + 6])
            else:
                ip += n
    return ids


def mopp_sections(b):
    """Split a dataref that holds several MOPP programs back to back (phmo 'MOPP Codes': one program per MOPPs
    element, each padded to 16 bytes)."""
    out = []
    pos = 0
    while pos < len(b):
        if pos + 0x28 > len(b):
            out.append(bytes(b[pos:]))
            break
        real = struct.unpack_from('<I', b, pos + 0x20)[0]
        ln = (real + 15) // 16 * 16
        if real <= MOPP_START or pos + ln > len(b):
            out.append(bytes(b[pos:]))
            break
        out.append(bytes(b[pos:pos + ln]))
        pos += ln
    return out


def _check_mopp(ctx, payload, where):
    secs = mopp_sections(payload) if where.startswith('phmo') else [payload]
    for s in secs:
        rep = mopp_validate(s)
        ctx.stats['havok_mopp_blobs'] += 1
        if not rep.ok:
            ctx.stats['havok_mopp_invalid'] += 1
            ctx.warn('havok_mopp_invalid', '%s: %s' % (where, rep.reason))


def _check_mopp_fields(*offsets_by_name):
    """node handler factory: validate the named MOPP datarefs of a struct without touching anything."""
    names = [n.lower() for n in offsets_by_name]

    def check(ctx, mnode, vnode):
        if not _opt(ctx, 'havok_validate_mopp', True):
            return
        for f in vnode.fields:
            if f.type == 'dataref' and f.name.lower() in names:
                payload = vnode.datas.get(f.offset) or b''
                if payload:
                    _check_mopp(ctx, payload, '%s%s %s' % (vnode.group, vnode.path, f.name))
    return check


node_handler('phmo', '', prio=200)(_check_mopp_fields('MOPP Codes'))
node_handler('sbsp', '', prio=200)(_check_mopp_fields('MOPP Codes', 'Breakable Surface MOPP Codes'))
node_handler('sbsp', '/Clusters', prio=200)(_check_mopp_fields('Collision MOPP Codes'))
