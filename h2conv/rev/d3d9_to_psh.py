# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Direct3D 9 shader bytecode (vs_2_0 / vs_3_0 / ps_2_0 / ps_2_x / ps_3_0) -> HLSL in the style of the H2EK ``.psh`` sources.

Why this exists.  A Halo 2 Vista cache keeps only the COMPILED pass code (``spas`` Pixel Shader blocks: three D3D9 blobs per pixel
shader, ``vrtx``: one D3D9 vs_2_0 blob).  MCC needs DXBC.  The 39 stems / 12 passes / 1 vertex shader that exist in no MCC map and in no
H2EK tag set therefore have to be ported FAITHFULLY from the Vista bytecode: bytecode -> HLSL (this module) -> DXBC (D3DCompiler_47, same
profile / flags / defines as the H2EK tool) with an equivalence proof (the two evaluators below).

Sections
--------
1. D3D9 token stream parser + CTAB reader + text disassembler        (``parse``, ``disassemble``)     == D3DDisassemble on 2095/2095 blobs
2. D3DCompiler_47 bindings, in-process (ctypes)                       (``compile_hlsl``, ``d3d_disassemble``)
3. evaluator of the D3D9 instruction subset used by Halo 2            (``D3D9Machine``)
4. DXBC (SM4/SM5) container parser + evaluator                        (``parse_dxbc``, ``DxbcMachine``)
5. pixel shaders: translator, .psh assembly, compile recipe, proof    (``translate_ps``, ``compile_psh``, ``PshResult``)
6. vertex shaders: vs_2_0 -> MCC style vs_5_0, proof                  (``translate_vs``, ``compile_vs``, ``VsResult``)
7. equivalence harness (random trials, two machines, compare)         (``random_trials``, ``compare_machines``)
8. tag helpers (decoded spas / vrtx nodes) and a small CLI

No GUI, no external process: everything is pure Python + numpy + ctypes.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL
except Exception:                                                                                    # pragma: no cover
    pass

import math
import os
import re
import struct
import sys
from collections import OrderedDict, namedtuple

import numpy as np

# =====================================================================================================================
# 1. D3D9 token stream
# =====================================================================================================================
# register types (D3DSHADER_PARAM_REGISTER_TYPE)
R_TEMP, R_INPUT, R_CONST, R_ADDR, R_RASTOUT, R_ATTROUT, R_TEXCRDOUT, R_CONSTINT, R_COLOROUT, R_DEPTHOUT, R_SAMPLER, \
    R_CONST2, R_CONST3, R_CONST4, R_CONSTBOOL, R_LOOP, R_TEMPFLOAT16, R_MISC, R_LABEL, R_PREDICATE = range(20)
R_TEXTURE = R_ADDR            # same value 3: 't#' in a pixel shader, 'a0' in a vertex shader
R_OUTPUT = R_TEXCRDOUT        # same value 6: 'oT#' in vs_2_0, 'o#' in vs_3_0

OPCODES = OrderedDict([
    (0, 'nop'), (1, 'mov'), (2, 'add'), (3, 'sub'), (4, 'mad'), (5, 'mul'), (6, 'rcp'), (7, 'rsq'), (8, 'dp3'), (9, 'dp4'),
    (10, 'min'), (11, 'max'), (12, 'slt'), (13, 'sge'), (14, 'exp'), (15, 'log'), (16, 'lit'), (17, 'dst'), (18, 'lrp'),
    (19, 'frc'), (20, 'm4x4'), (21, 'm4x3'), (22, 'm3x4'), (23, 'm3x3'), (24, 'm3x2'), (25, 'call'), (26, 'callnz'),
    (27, 'loop'), (28, 'ret'), (29, 'endloop'), (30, 'label'), (31, 'dcl'), (32, 'pow'), (33, 'crs'), (34, 'sgn'),
    (35, 'abs'), (36, 'nrm'), (37, 'sincos'), (38, 'rep'), (39, 'endrep'), (40, 'if'), (41, 'ifc'), (42, 'else'),
    (43, 'endif'), (44, 'break'), (45, 'breakc'), (46, 'mova'), (47, 'defb'), (48, 'defi'),
    (64, 'texcoord'), (65, 'texkill'), (66, 'texld'), (67, 'texbem'), (68, 'texbeml'), (69, 'texreg2ar'),
    (70, 'texreg2gb'), (71, 'texm3x2pad'), (72, 'texm3x2tex'), (73, 'texm3x3pad'), (74, 'texm3x3tex'),
    (76, 'texm3x3spec'), (77, 'texm3x3vspec'), (78, 'expp'), (79, 'logp'), (80, 'cnd'), (81, 'def'),
    (82, 'texreg2rgb'), (83, 'texdp3tex'), (84, 'texm3x2depth'), (85, 'texdp3'), (86, 'texm3x3'), (87, 'texdepth'),
    (88, 'cmp'), (89, 'bem'), (90, 'dp2add'), (91, 'dsx'), (92, 'dsy'), (93, 'texldd'), (94, 'setp'), (95, 'texldl'),
    (96, 'breakp'),
    (0xFFFD, 'phase'), (0xFFFE, 'comment'), (0xFFFF, 'end'),
])
OP_DCL, OP_DEF, OP_DEFI, OP_DEFB, OP_COMMENT, OP_END, OP_TEXLD, OP_TEXKILL = 31, 81, 48, 47, 0xFFFE, 0xFFFF, 66, 65
# number of SOURCE operands (the destination, when present, is operand 0); None = no destination
#         op : (has_dst, n_src)
OP_SHAPE = {
    'nop': (False, 0), 'mov': (True, 1), 'add': (True, 2), 'sub': (True, 2), 'mad': (True, 3), 'mul': (True, 2), 'rcp': (True, 1),
    'rsq': (True, 1), 'dp3': (True, 2), 'dp4': (True, 2), 'min': (True, 2), 'max': (True, 2), 'slt': (True, 2), 'sge': (True, 2),
    'exp': (True, 1), 'log': (True, 1), 'lit': (True, 1), 'dst': (True, 2), 'lrp': (True, 3), 'frc': (True, 1),
    'm4x4': (True, 2), 'm4x3': (True, 2), 'm3x4': (True, 2), 'm3x3': (True, 2), 'm3x2': (True, 2),
    'call': (False, 1), 'callnz': (False, 2), 'loop': (False, 2), 'ret': (False, 0), 'endloop': (False, 0), 'label': (False, 1),
    'pow': (True, 2), 'crs': (True, 2), 'sgn': (True, 3), 'abs': (True, 1), 'nrm': (True, 1), 'sincos': (True, 1),
    'rep': (False, 1), 'endrep': (False, 0), 'if': (False, 1), 'ifc': (False, 2), 'else': (False, 0), 'endif': (False, 0),
    'break': (False, 0), 'breakc': (False, 2), 'mova': (True, 1), 'texkill': (True, 0), 'texld': (True, 2),
    'texcoord': (True, 0), 'expp': (True, 1), 'logp': (True, 1), 'cnd': (True, 3), 'cmp': (True, 3), 'dp2add': (True, 3),
    'dsx': (True, 1), 'dsy': (True, 1), 'texldd': (True, 4), 'setp': (True, 2), 'texldl': (True, 2), 'breakp': (False, 1),
    'phase': (False, 0),
}
USAGE_NAMES = ['position', 'blendweight', 'blendindices', 'normal', 'psize', 'texcoord', 'tangent', 'binormal', 'tessfactor',
               'positiont', 'color', 'fog', 'depth', 'sample']
SAMPLER_TYPES = {0: '', 1: '_1d', 2: '_2d', 3: '_cube', 4: '_volume'}
CMP_NAMES = {1: 'gt', 2: 'eq', 3: 'ge', 4: 'lt', 5: 'ne', 6: 'le'}
SRCMOD_NAMES = {0: '', 1: 'neg', 2: 'bias', 3: 'bias_neg', 4: 'bx2', 5: 'bx2_neg', 6: 'comp', 7: 'x2', 8: 'x2_neg', 9: 'dz', 10: 'dw',
                11: 'abs', 12: 'abs_neg', 13: 'not'}
SM_NEG, SM_BIAS, SM_BIASNEG, SM_SIGN, SM_SIGNNEG, SM_COMP, SM_X2, SM_X2NEG, SM_DZ, SM_DW, SM_ABS, SM_ABSNEG, SM_NOT = range(1, 14)


class D3D9Error(Exception):
    pass


class Param(object):
    """A parameter token (+ relative-address token).  For a destination ``mask`` is the 4-bit write mask, for a source ``swz`` is the
    4-tuple of component indices and ``mod`` the source modifier."""
    __slots__ = ('rtype', 'num', 'rel', 'mask', 'mod_bits', 'shift', 'swz', 'srcmod', 'raw')

    def __init__(self, tok, rel=None):
        self.raw = tok
        self.rtype = ((tok >> 28) & 7) | ((tok >> 8) & 0x18)
        self.num = tok & 0x7FF
        self.rel = rel                        # Param (the address register, with swizzle) or None
        self.mask = (tok >> 16) & 0xF
        self.mod_bits = (tok >> 20) & 0xF     # dst: 1 saturate, 2 partial precision, 4 centroid
        sh = (tok >> 24) & 0xF
        self.shift = sh - 16 if sh >= 8 else sh
        s = (tok >> 16) & 0xFF
        self.swz = (s & 3, (s >> 2) & 3, (s >> 4) & 3, (s >> 6) & 3)
        self.srcmod = (tok >> 24) & 0xF

    @property
    def reg_key(self):
        return (self.rtype, self.num)


class Ins(object):
    __slots__ = ('op', 'name', 'ctrl', 'dst', 'srcs', 'coissue', 'pred', 'extra', 'ntok', 'index', 'dcl_usage', 'dcl_index', 'dcl_type')

    def __init__(self, op, name):
        self.op = op
        self.name = name
        self.ctrl = 0
        self.dst = None
        self.srcs = []
        self.coissue = False
        self.pred = None
        self.extra = None
        self.ntok = 0
        self.index = -1
        self.dcl_usage = self.dcl_index = self.dcl_type = None


ConstInfo = namedtuple('ConstInfo', 'name rset index count cls typ rows cols elements default')
REG_SET_NAMES = {0: 'bool', 1: 'int4', 2: 'float4', 3: 'sampler'}


class CTab(object):
    def __init__(self):
        self.creator = ''
        self.target = ''
        self.version = 0
        self.flags = 0
        self.consts = []

    def by_reg(self, rset, index):
        for c in self.consts:
            if c.rset == rset and c.index <= index < c.index + max(c.count, 1):
                return c
        return None


class Shader(object):
    def __init__(self):
        self.kind = 'ps'
        self.major = 2
        self.minor = 0
        self.sub = 0            # 0xFF => 'x' (ps_2_x style: minor 1)
        self.ins = []
        self.ctab = None
        self.comments = []
        self.raw = b''

    @property
    def profile(self):
        return '%s_%d_%s' % (self.kind, self.major, 'x' if (self.major == 2 and self.minor == 1) else self.minor)

    def defs(self):
        out = OrderedDict()
        for i in self.ins:
            if i.name == 'def':
                out[i.dst.num] = i.extra
        return out


def _parse_ctab(data):
    """data = comment payload after the 'CTAB' fourcc (offsets are relative to the start of the D3DXSHADER_CONSTANTTABLE)."""
    ct = CTab()
    buf = data                                  # every CTAB offset is relative to the start of this structure (after the 'CTAB' fourcc)
    (size, creator, version, nconst, cinfo, flags, target) = struct.unpack_from('<7I', buf, 0)
    if size != 28:
        raise D3D9Error('CTAB size %d' % size)

    def cstr(off):
        if not off or off >= len(buf):
            return ''
        e = buf.find(b'\0', off)
        return buf[off:e].decode('latin-1')
    ct.creator, ct.version, ct.flags, ct.target = cstr(creator), version, flags, cstr(target)
    for i in range(nconst):
        o = cinfo + 20 * i
        nm, rset, ridx, rcnt, _res, tinfo, dflt = struct.unpack_from('<IHHHHII', buf, o)
        cls = typ = rows = cols = elems = 0
        if tinfo:
            cls, typ, rows, cols, elems, _m, _mi = struct.unpack_from('<6HI', buf, tinfo)
        dv = None
        if dflt:
            n = max(rows * cols, 1) * max(elems, 1) * 4
            dv = bytes(buf[dflt:dflt + n])
        ct.consts.append(ConstInfo(cstr(nm), rset, ridx, rcnt, cls, typ, rows, cols, elems, dv))
    return ct


def parse(code):
    """Parse a D3D9 shader blob -> Shader.  Raises D3D9Error on anything it cannot decode exactly."""
    code = bytes(code)
    if len(code) < 8 or len(code) % 4:
        raise D3D9Error('bad length %d' % len(code))
    toks = struct.unpack('<%dI' % (len(code) // 4), code)
    ver = toks[0]
    hi = ver >> 16
    if hi == 0xFFFF:
        kind = 'ps'
    elif hi == 0xFFFE:
        kind = 'vs'
    else:
        raise D3D9Error('not a D3D9 shader (version token %08X)' % ver)
    sh = Shader()
    sh.kind = kind
    sh.major, sh.minor = (ver >> 8) & 0xFF, ver & 0xFF
    sh.raw = code
    if sh.major < 2:
        raise D3D9Error('%s_%d_%d is not supported (only 2.x / 3.0)' % (kind, sh.major, sh.minor))
    has_rel = (kind == 'vs') or (sh.major >= 3)       # relative addressing tokens exist for vs_2_0+ and ps_3_0
    p = 1
    n = len(toks)
    idx = 0
    while p < n:
        t = toks[p]
        op = t & 0xFFFF
        if op == OP_END:
            break
        if op == OP_COMMENT:
            ln = (t >> 16) & 0x7FFF
            payload = struct.pack('<%dI' % ln, *toks[p + 1:p + 1 + ln])
            if payload[:4] == b'CTAB':
                try:
                    sh.ctab = _parse_ctab(payload[4:])
                except Exception as e:                       # a malformed CTAB must not hide the code
                    sh.comments.append(('CTAB-error', str(e)))
            else:
                sh.comments.append(('comment', payload))
            p += 1 + ln
            continue
        name = OPCODES.get(op)
        if name is None:
            raise D3D9Error('unknown opcode %d at token %d' % (op, p))
        ln = (t >> 24) & 0xF
        ins = Ins(op, name)
        ins.ctrl = (t >> 16) & 0xFF
        ins.coissue = bool(t & 0x40000000)
        ins.index = idx
        idx += 1
        body = toks[p + 1:p + 1 + ln]
        pos = [0]

        def take():
            v = body[pos[0]]
            pos[0] += 1
            return v

        def take_param():
            tk = take()
            rel = None
            if tk & 0x2000 and has_rel:
                rel = Param(take())
            return Param(tk, rel)

        if op == OP_DCL:
            d = take()
            ins.dcl_usage = d & 0x1F
            ins.dcl_index = (d >> 16) & 0xF
            ins.dcl_type = (d >> 27) & 0xF
            ins.dst = take_param()
        elif op == OP_DEF:
            ins.dst = take_param()
            ins.extra = struct.unpack('<4f', struct.pack('<4I', *[take() for _ in range(4)]))
        elif op == OP_DEFI:
            ins.dst = take_param()
            ins.extra = struct.unpack('<4i', struct.pack('<4I', *[take() for _ in range(4)]))
        elif op == OP_DEFB:
            ins.dst = take_param()
            ins.extra = (take(),)
        else:
            shape = OP_SHAPE.get(name)
            if shape is None:
                raise D3D9Error('opcode %s has no operand shape' % name)
            has_dst, nsrc = shape
            if name == 'sincos' and sh.major < 3:
                nsrc = 3                      # sincos dst, src, taylor-constant, taylor-constant (SM2)
            if has_dst:
                ins.dst = take_param()
            srcs = []
            while pos[0] < len(body):
                srcs.append(take_param())
            # a predicated instruction (bit 28) carries one trailing predicate source (p0 with swizzle / modifier)
            if t & 0x10000000 and srcs:
                ins.pred = srcs.pop()
            ins.srcs = srcs
            if len(srcs) != nsrc:
                raise D3D9Error('%s: %d source operands, expected %d' % (name, len(srcs), nsrc))
        ins.ntok = ln
        sh.ins.append(ins)
        p += 1 + ln
    else:
        raise D3D9Error('missing END token')
    return sh


# ------------------------------------------------------------------------------------------------------------------
# text disassembler (D3DX style)
# ------------------------------------------------------------------------------------------------------------------
def _fmt_float(f):
    if f != f:
        return 'nan'
    if f in (float('inf'), float('-inf')):
        return 'inf' if f > 0 else '-inf'
    s = '%.9g' % f
    if 'e' not in s and '.' in s:
        s = s.rstrip('0').rstrip('.')
    return re.sub(r'e([+-])(\d\d)$', r'e\g<1>0\g<2>', s)           # D3DX prints three exponent digits (MSVC runtime): 1.5e-006


def reg_name(sh, rtype, num, as_dst=False):
    k, ps = sh.kind, sh.kind == 'ps'
    if rtype == R_TEMP:
        return 'r%d' % num
    if rtype == R_INPUT:
        return 'v%d' % num
    if rtype == R_CONST:
        return 'c%d' % num
    if rtype == R_ADDR:
        return ('t%d' % num) if ps else ('a%d' % num)
    if rtype == R_RASTOUT:
        return ('oPos', 'oFog', 'oPts')[num] if num < 3 else 'oRast%d' % num
    if rtype == R_ATTROUT:
        return 'oD%d' % num
    if rtype == R_TEXCRDOUT:
        return ('o%d' % num) if (sh.major >= 3) else ('oT%d' % num)
    if rtype == R_CONSTINT:
        return 'i%d' % num
    if rtype == R_COLOROUT:
        return 'oC%d' % num
    if rtype == R_DEPTHOUT:
        return 'oDepth'
    if rtype == R_SAMPLER:
        return 's%d' % num
    if rtype == R_CONST2:
        return 'c%d' % (2048 + num)
    if rtype == R_CONST3:
        return 'c%d' % (4096 + num)
    if rtype == R_CONST4:
        return 'c%d' % (6144 + num)
    if rtype == R_CONSTBOOL:
        return 'b%d' % num
    if rtype == R_LOOP:
        return 'aL'
    if rtype == R_TEMPFLOAT16:
        return 'h%d' % num
    if rtype == R_MISC:
        return ('vPos', 'vFace')[num] if num < 2 else 'misc%d' % num
    if rtype == R_LABEL:
        return 'l%d' % num
    if rtype == R_PREDICATE:
        return 'p%d' % num
    return 'reg%d_%d' % (rtype, num)


def _swz_str(swz):
    if swz == (0, 1, 2, 3):
        return ''
    if len(set(swz)) == 1:
        return '.' + 'xyzw'[swz[0]]
    return '.' + ''.join('xyzw'[i] for i in swz)


def src_str(sh, prm):
    nm = reg_name(sh, prm.rtype, prm.num)
    if prm.rel is not None:
        r = prm.rel
        rn = reg_name(sh, r.rtype, r.num)
        if r.rtype == R_LOOP:
            idx = 'aL'
        else:
            idx = rn + '.' + 'xyzw'[r.swz[0]]
        nm = '%s[%s]' % (nm, idx)                      # D3DX: c50[a0.x] / v0[aL]
    s = nm + _swz_str(prm.swz)
    m = prm.srcmod
    if m == 0:
        return s
    if m == SM_NEG:
        return '-' + s
    if m == SM_COMP:
        return '1-' + s
    if m == SM_NOT:
        return '!' + s
    if m == SM_ABS:
        return '%s_abs' % s
    if m == SM_ABSNEG:
        return '-%s_abs' % s
    base = {SM_BIAS: '_bias', SM_BIASNEG: '_bias', SM_SIGN: '_bx2', SM_SIGNNEG: '_bx2', SM_X2: '_x2', SM_X2NEG: '_x2',
            SM_DZ: '_dz', SM_DW: '_dw'}[m]
    neg = '-' if m in (SM_BIASNEG, SM_SIGNNEG, SM_X2NEG) else ''
    return neg + s + base


def dst_str(sh, prm, no_mods=False):
    nm = reg_name(sh, prm.rtype, prm.num, True)
    if prm.rel is not None:
        r = prm.rel
        nm = '%s[%s.%s]' % (nm[0], reg_name(sh, r.rtype, r.num), 'xyzw'[r.swz[0]])
    if prm.mask != 0xF:
        nm += '.' + ''.join(c for i, c in enumerate('xyzw') if prm.mask >> i & 1)
    return nm


def ins_suffix(sh, ins):
    s = ''
    if ins.dst is not None and ins.name not in ('dcl', 'def', 'defi', 'defb'):
        sc = ins.dst.shift
        if sc:
            s += {1: '_x2', 2: '_x4', 3: '_x8', -1: '_d2', -2: '_d4', -3: '_d8'}.get(sc, '_s%d' % sc)
        mb = ins.dst.mod_bits
        if mb & 1:
            s += '_sat'
        if mb & 2:
            s += '_pp'
        if mb & 4:
            s += '_centroid'
    return s


def disassemble(sh, with_ctab=True):
    """Text in the style of D3DXDisassembleShader (one instruction per line)."""
    if isinstance(sh, (bytes, bytearray)):
        sh = parse(sh)
    out = []
    if with_ctab and sh.ctab is not None:
        out.append('// Creator: %s   Target: %s' % (sh.ctab.creator, sh.ctab.target))
        for c in sh.ctab.consts:
            out.append('//   %-24s %-8s c%-3d %d' % (c.name, REG_SET_NAMES.get(c.rset, str(c.rset)), c.index, c.count))
    out.append('    %s' % sh.profile)
    for i in sh.ins:
        nm = i.name
        if nm == 'dcl':
            if sh.kind == 'ps' and i.dst.rtype == R_SAMPLER:
                nm = 'dcl' + SAMPLER_TYPES.get(i.dcl_type, '_t%d' % i.dcl_type)
                out.append('    %s %s' % (nm, reg_name(sh, i.dst.rtype, i.dst.num)))
                continue
            cen = '_centroid' if i.dst.mod_bits & 4 else ''
            if (sh.major >= 3 or sh.kind == 'vs') and i.dst.rtype != R_MISC:
                u = USAGE_NAMES[i.dcl_usage] if i.dcl_usage < len(USAGE_NAMES) else 'usage%d' % i.dcl_usage
                ui = str(i.dcl_index) if i.dcl_index else ''
                nm = 'dcl_' + u + ui + cen
            else:
                nm = 'dcl' + cen
            out.append('    %s %s' % (nm, dst_str(sh, i.dst, no_mods=True)))
            continue
        if nm == 'def':
            out.append('    def %s, %s' % (reg_name(sh, i.dst.rtype, i.dst.num), ', '.join(_fmt_float(f) for f in i.extra)))
            continue
        if nm == 'defi':
            out.append('    defi %s, %s' % (reg_name(sh, i.dst.rtype, i.dst.num), ', '.join(str(f) for f in i.extra)))
            continue
        if nm == 'defb':
            out.append('    defb %s, %s' % (reg_name(sh, i.dst.rtype, i.dst.num), 'true' if i.extra[0] else 'false'))
            continue
        if nm == 'texld':
            nm = {0: 'texld', 1: 'texldp', 2: 'texldb'}.get(i.ctrl & 3, 'texld?')
        if nm in ('ifc', 'breakc', 'setp'):
            nm += '_' + CMP_NAMES.get(i.ctrl & 7, '?')
        nm += ins_suffix(sh, i)
        ops = []
        if i.dst is not None:
            ops.append(dst_str(sh, i.dst))
        ops += [src_str(sh, s) for s in i.srcs]
        out.append('    %s%s' % (nm, (' ' + ', '.join(ops)) if ops else ''))
    return '\n'.join(out) + '\n'


# =====================================================================================================================
# 2. D3DCompiler_47 bindings (ctypes, in-process).  The H2EK tool uses the same DLL (tool.exe strings: '\tools\dx11\D3DCompiler_47.dll',
#    '_D3DCompileX86@44', fxc command line '/nologo /T <profile> /E main /Gpp /Gec ...').
# =====================================================================================================================
_D3DC = None
D3DCOMPILE_DEBUG = 1 << 0
D3DCOMPILE_SKIP_VALIDATION = 1 << 1
D3DCOMPILE_SKIP_OPTIMIZATION = 1 << 2
D3DCOMPILE_ENABLE_BACKWARDS_COMPATIBILITY = 1 << 12     # fxc /Gec
# Verified recipe: profile ps_5_0, entry 'main', flags = ENABLE_BACKWARDS_COMPATIBILITY ONLY, defines
# DX11 (+ ZPRIME | + MRT) reproduces the three MCC DXBC blobs of a stock pass BYTE FOR BYTE.  (fxc /Gpp has no D3DCompile flag: bit 19 is
# D3DCOMPILE_RESOURCES_MAY_ALIAS in D3DCompiler_47 and is rejected for ps_5_0.)
H2EK_FLAGS = D3DCOMPILE_ENABLE_BACKWARDS_COMPATIBILITY


def _d3dc():
    global _D3DC
    if _D3DC is None:
        _D3DC = ctypes.WinDLL(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'D3DCompiler_47.dll'))
        _D3DC.D3DCompile.restype = ctypes.c_long
        _D3DC.D3DDisassemble.restype = ctypes.c_long
    return _D3DC


def _blob_take(pblob):
    """Copy the bytes out of an ID3DBlob* and Release() it."""
    if not pblob:
        return b''
    pv = ctypes.c_void_p(pblob)
    vtbl = ctypes.cast(ctypes.cast(pv, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    get_ptr = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(vtbl[3])
    get_size = ctypes.WINFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(vtbl[4])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
    data = ctypes.string_at(get_ptr(pv), get_size(pv))
    release(pv)
    return data


class _Macro(ctypes.Structure):
    _fields_ = [('Name', ctypes.c_char_p), ('Definition', ctypes.c_char_p)]


class CompileError(Exception):
    pass


def compile_hlsl(source, profile='ps_5_0', entry='main', defines=None, flags=None, name='shader.psh'):
    """In-process D3DCompile.  ``defines`` = {name: value-or-None}.  Returns the DXBC bytes; raises CompileError with the compiler log.
    Default flags = the verified H2EK recipe (/Gec only, see H2EK_FLAGS)."""
    if flags is None:
        flags = H2EK_FLAGS
    src = source.encode('latin-1') if isinstance(source, str) else bytes(source)
    items = []
    for k, v in (defines or {}).items():
        items.append(_Macro(k.encode(), None if v is None else str(v).encode()))
        if v is None:
            items[-1].Definition = b'1'
    arr = (_Macro * (len(items) + 1))(*(items + [_Macro(None, None)]))
    pcode, perr = ctypes.c_void_p(0), ctypes.c_void_p(0)
    hr = _d3dc().D3DCompile(ctypes.c_char_p(src), ctypes.c_size_t(len(src)), ctypes.c_char_p(name.encode()), arr, None,
                            ctypes.c_char_p(entry.encode()), ctypes.c_char_p(profile.encode()), ctypes.c_uint(flags), ctypes.c_uint(0),
                            ctypes.byref(pcode), ctypes.byref(perr))
    log = _blob_take(perr.value).decode('latin-1', 'replace').rstrip('\x00')
    code = _blob_take(pcode.value)
    if hr < 0 or not code:
        raise CompileError('D3DCompile failed (hr=%08X):\n%s' % (hr & 0xFFFFFFFF, log))
    return code


def d3d_disassemble(code, flags=0):
    """Text disassembly through D3DCompiler_47's D3DDisassemble (works for D3D9 token streams and for DXBC)."""
    pb = ctypes.c_void_p(0)
    buf = ctypes.create_string_buffer(bytes(code), len(code))
    hr = _d3dc().D3DDisassemble(buf, ctypes.c_size_t(len(code)), ctypes.c_uint(flags), None, ctypes.byref(pb))
    if hr < 0:
        raise CompileError('D3DDisassemble failed hr=%08X' % (hr & 0xFFFFFFFF))
    return _blob_take(pb.value).decode('latin-1', 'replace').rstrip('\x00')


# =====================================================================================================================
# 3. Evaluator of the D3D9 instruction subset used by Halo 2 (vectorised over N random trials; float64 by default)
# =====================================================================================================================
class Unsupported(Exception):
    """The evaluator / translator met an instruction or operand form it does not implement.  NEVER approximated: the caller must report the
    shader as 'not proven'."""


def _sat(x):
    return np.clip(np.nan_to_num(x, nan=0.0), 0.0, 1.0)


def usage_semantic(usage, index):
    name = USAGE_NAMES[usage].upper() if usage < len(USAGE_NAMES) else 'USAGE%d' % usage
    return (name, index)


class SampleFn(object):
    """Deterministic texture stand-in shared by both evaluators: sample(kind, stage, coords(N,4), lod_bias(N) or None) -> (N,4).
    Depends on the stage, the dimensionality and EVERY coordinate that the dimensionality uses (so a wrong coordinate or a wrong stage
    changes the answer); channel values lie in [0,1] with high-frequency content."""
    FREQ = np.array([[3.1, 5.7, 2.3], [4.3, 2.9, 6.1], [5.3, 3.7, 4.1], [2.7, 6.3, 3.3]])
    PHASE = np.array([0.31, 1.17, 2.03, 2.89])

    def __call__(self, kind, stage, coords, bias=None):
        n = 3 if kind in ('cube', '3d', 'volume') else 2
        c = np.asarray(coords, dtype=np.float64)[:, :n]
        out = np.empty((c.shape[0], 4))
        for ch in range(4):
            f = self.FREQ[ch][:n] * (1.0 + 0.173 * stage)
            ph = self.PHASE[ch] + 0.61 * stage
            out[:, ch] = 0.5 + 0.5 * np.sin(c @ f + ph + 0.2 * np.sin(c @ f[::-1] * 1.7))
        return out


class Result(object):
    """outputs {semantic -> (N,4)}, written {semantic -> lane bit mask}, killed (N,) bool."""
    __slots__ = ('outputs', 'written', 'killed')

    def __init__(self, outputs, written, killed):
        self.outputs, self.written, self.killed = outputs, written, killed


class D3D9Machine(object):
    """Executes a parsed D3D9 Shader for N trials at once.
    run(consts, inputs, sampler, int_consts=None) -> Result
    consts: (N, C, 4) float array (register c# = consts[:, #]); `def` constants override the registers they name.
    inputs: {semantic: (N,4)}; semantics: ('COLOR',n) ('TEXCOORD',n) ('POSITION',0) ('NORMAL',0) ... (see input_semantics())."""

    def __init__(self, sh, clamp_colors=True):
        if isinstance(sh, (bytes, bytearray)):
            sh = parse(sh)
        self.sh = sh
        self.clamp_colors = clamp_colors
        self.in_map = {}      # (rtype, num) -> semantic
        self.out_map = {}     # (rtype, num) -> semantic
        self._build_maps()

    # ---- register <-> semantic maps ------------------------------------------------------------------------------------
    def _build_maps(self):
        sh = self.sh
        if sh.kind == 'ps':
            if sh.major < 3:
                for n in range(2):
                    self.in_map[(R_INPUT, n)] = ('COLOR', n)
                for n in range(8):
                    self.in_map[(R_TEXTURE, n)] = ('TEXCOORD', n)
            else:
                for i in sh.ins:
                    if i.name == 'dcl' and i.dst.rtype == R_INPUT:
                        self.in_map[(R_INPUT, i.dst.num)] = usage_semantic(i.dcl_usage, i.dcl_index)
                self.in_map[(R_MISC, 0)] = ('POSITION', 0)
            for n in range(4):
                self.out_map[(R_COLOROUT, n)] = ('COLOR', n)
            self.out_map[(R_DEPTHOUT, 0)] = ('DEPTH', 0)
        else:
            for i in sh.ins:
                if i.name == 'dcl' and i.dst.rtype == R_INPUT:
                    self.in_map[(R_INPUT, i.dst.num)] = usage_semantic(i.dcl_usage, i.dcl_index)
            if sh.major < 3:
                self.out_map[(R_RASTOUT, 0)] = ('POSITION', 0)
                self.out_map[(R_RASTOUT, 1)] = ('FOG', 0)
                self.out_map[(R_RASTOUT, 2)] = ('PSIZE', 0)
                for n in range(2):
                    self.out_map[(R_ATTROUT, n)] = ('COLOR', n)
                for n in range(8):
                    self.out_map[(R_TEXCRDOUT, n)] = ('TEXCOORD', n)
            else:
                for i in sh.ins:
                    if i.name == 'dcl' and i.dst.rtype == R_OUTPUT:
                        self.out_map[(R_OUTPUT, i.dst.num)] = usage_semantic(i.dcl_usage, i.dcl_index)

    def input_semantics(self):
        return sorted(set(self.in_map.values()))

    def used_input_semantics(self):
        """Semantics whose register is READ by at least one instruction (components are not tracked)."""
        used = set()
        for i in self.sh.ins:
            for s in i.srcs:
                if s.rtype in (R_INPUT, R_TEXTURE, R_MISC) and (s.rtype, s.num) in self.in_map:
                    used.add(self.in_map[(s.rtype, s.num)])
            if i.name == 'texkill' and i.dst is not None and (i.dst.rtype, i.dst.num) in self.in_map:
                used.add(self.in_map[(i.dst.rtype, i.dst.num)])
        return used

    def sampler_kinds(self):
        out = {}
        for i in self.sh.ins:
            if i.name == 'dcl' and i.dst.rtype == R_SAMPLER:
                out[i.dst.num] = {2: '2d', 3: 'cube', 4: '3d', 1: '1d'}.get(i.dcl_type, '2d')
        return out

    # ---- execution ---------------------------------------------------------------------------------------------------------
    def run(self, consts, inputs, sampler, int_consts=None, bool_consts=None):
        sh = self.sh
        n = consts.shape[0]
        zero = np.zeros((n, 4))
        temps = {}
        outs = {}
        wmask = {}
        killed = np.zeros(n, dtype=bool)
        a0 = np.zeros((n, 4))
        defs = {}
        ints = dict(int_consts or {})
        bools = dict(bool_consts or {})
        for i in sh.ins:
            if i.name == 'def':
                defs[i.dst.num] = np.broadcast_to(np.array(i.extra, dtype=np.float32).astype(np.float64), (n, 4))
            elif i.name == 'defi':
                ints[i.dst.num] = tuple(i.extra)
            elif i.name == 'defb':
                bools[i.dst.num] = bool(i.extra[0])
        samplers = self.sampler_kinds()
        loopv = [0]

        def read_reg(p):
            t, k = p.rtype, p.num
            if p.rel is not None:
                r = p.rel
                if r.rtype == R_LOOP:
                    idx = k + loopv[0]
                    if t == R_INPUT:
                        key = (R_INPUT, idx)
                        if key not in self.in_map:
                            raise Unsupported('v[aL] out of range (v%d)' % idx)
                        return inputs[self.in_map[key]]
                    if t == R_CONST:
                        return defs[idx] if idx in defs else consts[:, idx]
                    raise Unsupported('relative %d via aL' % t)
                if r.rtype == R_ADDR and sh.kind == 'vs':
                    idxv = a0[:, r.swz[0]].astype(np.int64) + k
                    if t == R_CONST:
                        idxv = np.clip(idxv, 0, consts.shape[1] - 1)
                        return consts[np.arange(n), idxv]
                    raise Unsupported('relative addressing of type %d' % t)
                raise Unsupported('relative addressing via %s' % reg_name(sh, r.rtype, r.num))
            if t == R_TEMP:
                return temps.get(k, zero)
            if t == R_CONST:
                return defs[k] if k in defs else consts[:, k]
            if t in (R_INPUT, R_TEXTURE, R_MISC):
                key = (t, k)
                if key not in self.in_map:
                    raise Unsupported('input register %s has no semantic' % reg_name(sh, t, k))
                sem = self.in_map[key]
                if sem not in inputs:
                    raise Unsupported('input %s not supplied' % (sem,))
                if sem[0] == 'COLOR' and sh.kind == 'ps' and self.clamp_colors:
                    return np.clip(inputs[sem], 0.0, 1.0)          # D3D9: colour interpolants reach the pixel shader in [0,1] (vs_2_0 oD0/oD1 are saturated)
                return inputs[sem]
            if t == R_ADDR and sh.kind == 'vs':
                return a0
            if t in (R_COLOROUT, R_DEPTHOUT, R_RASTOUT, R_ATTROUT, R_TEXCRDOUT):
                return outs.get((t, k), zero)
            raise Unsupported('source register type %d' % t)

        def read_src(p):
            v = read_reg(p)[:, list(p.swz)]
            m = p.srcmod
            if m == 0:
                return v
            if m == SM_NEG:
                return -v
            if m == SM_BIAS:
                return v - 0.5
            if m == SM_BIASNEG:
                return -(v - 0.5)
            if m == SM_SIGN:
                return 2.0 * (v - 0.5)
            if m == SM_SIGNNEG:
                return -2.0 * (v - 0.5)
            if m == SM_COMP:
                return 1.0 - v
            if m == SM_X2:
                return 2.0 * v
            if m == SM_X2NEG:
                return -2.0 * v
            if m == SM_ABS:
                return np.abs(v)
            if m == SM_ABSNEG:
                return -np.abs(v)
            raise Unsupported('source modifier %s' % SRCMOD_NAMES.get(m, m))

        def merge(cur, val, mask):
            cur = np.zeros((n, 4)) if cur is None else cur.copy()
            for lane in range(4):
                if mask >> lane & 1:
                    cur[:, lane] = val[:, lane]
            return cur

        def write_dst(p, val):
            if p.shift:
                val = val * (2.0 ** p.shift)
            if p.mod_bits & 1:
                val = _sat(val)
            t, k = p.rtype, p.num
            if p.rel is not None:
                raise Unsupported('relative destination')
            if t == R_TEMP:
                temps[k] = merge(temps.get(k), val, p.mask)
            elif t == R_ADDR and sh.kind == 'vs':
                a0[:] = merge(a0, val, p.mask)
            elif t in (R_COLOROUT, R_DEPTHOUT, R_RASTOUT, R_ATTROUT, R_TEXCRDOUT):
                outs[(t, k)] = merge(outs.get((t, k)), val, p.mask)
                wmask[(t, k)] = wmask.get((t, k), 0) | p.mask
            else:
                raise Unsupported('destination register type %d' % t)

        def rep(v):
            return np.repeat(v[:, :1], 4, axis=1)

        ins_list = sh.ins
        pc = 0
        stack = []            # [pc_of_loop, count, start, step, iteration]
        while pc < len(ins_list):
            ins = ins_list[pc]
            nm = ins.name
            if nm in ('dcl', 'def', 'defi', 'defb', 'nop', 'phase'):
                pc += 1
                continue
            if nm == 'loop':
                cnt = ints[ins.srcs[1].num]
                if ins.srcs[0].rtype != R_LOOP:
                    raise Unsupported('loop counter operand')
                if cnt[0] <= 0:
                    depth, q = 1, pc + 1
                    while depth:
                        depth += {'loop': 1, 'endloop': -1}.get(ins_list[q].name, 0)
                        q += 1
                    pc = q
                    continue
                stack.append([pc, cnt[0], cnt[1], cnt[2], 0])
                loopv[0] = cnt[1]
                pc += 1
                continue
            if nm == 'endloop':
                top = stack[-1]
                top[4] += 1
                if top[4] < top[1]:
                    loopv[0] = top[2] + top[4] * top[3]
                    pc = top[0] + 1
                    continue
                stack.pop()
                loopv[0] = (stack[-1][2] + stack[-1][4] * stack[-1][3]) if stack else 0
                pc += 1
                continue
            if nm in ('rep', 'endrep', 'if', 'ifc', 'else', 'endif', 'call', 'callnz', 'ret', 'label', 'break', 'breakc', 'breakp', 'setp'):
                raise Unsupported('flow control instruction %s' % nm)
            if ins.pred is not None:
                raise Unsupported('predicated instruction')
            if nm == 'texkill':
                v = read_reg(ins.dst)
                killed |= np.any(v[:, :3] < 0.0, axis=1)
                pc += 1
                continue
            S = [read_src(s) for s in (ins.srcs[:1] if nm == 'texld' else ins.srcs)]
            if nm == 'mov':
                r = S[0]
            elif nm == 'add':
                r = S[0] + S[1]
            elif nm == 'sub':
                r = S[0] - S[1]
            elif nm == 'mul':
                r = S[0] * S[1]
            elif nm == 'mad':
                r = S[0] * S[1] + S[2]
            elif nm == 'dp3':
                r = rep(np.sum(S[0][:, :3] * S[1][:, :3], axis=1, keepdims=True))
            elif nm == 'dp4':
                r = rep(np.sum(S[0] * S[1], axis=1, keepdims=True))
            elif nm == 'dp2add':
                r = rep(S[0][:, :1] * S[1][:, :1] + S[0][:, 1:2] * S[1][:, 1:2] + S[2][:, :1])
            elif nm == 'rcp':
                with np.errstate(all='ignore'):
                    r = rep(1.0 / S[0][:, :1])
            elif nm == 'rsq':
                with np.errstate(all='ignore'):
                    r = rep(1.0 / np.sqrt(np.abs(S[0][:, :1])))
            elif nm in ('exp', 'expp'):
                r = rep(np.exp2(S[0][:, :1]))
            elif nm in ('log', 'logp'):
                with np.errstate(all='ignore'):
                    r = rep(np.log2(np.abs(S[0][:, :1])))
            elif nm == 'pow':
                with np.errstate(all='ignore'):
                    r = rep(np.power(np.abs(S[0][:, :1]), S[1][:, :1]))
            elif nm == 'min':
                r = np.minimum(S[0], S[1])
            elif nm == 'max':
                r = np.maximum(S[0], S[1])
            elif nm == 'slt':
                r = (S[0] < S[1]).astype(np.float64)
            elif nm == 'sge':
                r = (S[0] >= S[1]).astype(np.float64)
            elif nm == 'frc':
                r = S[0] - np.floor(S[0])
            elif nm == 'lrp':
                r = S[0] * (S[1] - S[2]) + S[2]
            elif nm == 'cmp':
                r = np.where(S[0] >= 0.0, S[1], S[2])
            elif nm == 'abs':
                r = np.abs(S[0])
            elif nm == 'nrm':
                with np.errstate(all='ignore'):
                    r = S[0] / np.sqrt(np.sum(S[0][:, :3] ** 2, axis=1, keepdims=True))
            elif nm == 'crs':
                a, b = S[0], S[1]
                r = np.zeros((n, 4))
                r[:, 0] = a[:, 1] * b[:, 2] - a[:, 2] * b[:, 1]
                r[:, 1] = a[:, 2] * b[:, 0] - a[:, 0] * b[:, 2]
                r[:, 2] = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
            elif nm == 'sincos':
                x = S[0][:, :1]
                r = np.concatenate([np.cos(x), np.sin(x), np.zeros((n, 2))], axis=1)
            elif nm == 'mova':
                r = np.floor(S[0] + 0.5)
            elif nm == 'texld':
                mode = ins.ctrl & 3
                sidx = ins.srcs[1].num
                kind = samplers.get(sidx, '2d')
                c = S[0]
                bias = None
                if mode == 1:           # texldp
                    with np.errstate(all='ignore'):
                        c = c / c[:, 3:4]
                elif mode == 2:         # texldb
                    bias = S[0][:, 3]
                r = sampler(kind, sidx, c, bias)
            else:
                raise Unsupported('opcode %s' % nm)
            write_dst(ins.dst, r)
            pc += 1
        outputs, written = {}, {}
        for key, val in outs.items():
            sem = self.out_map.get(key)
            if sem is None:
                raise Unsupported('output register %s without semantic' % reg_name(sh, *key))
            outputs[sem] = val
            written[sem] = wmask.get(key, 0)
        return Result(outputs, written, killed)


# =====================================================================================================================
# 4. DXBC (SM4 / SM5) container parser + evaluator of the subset D3DCompile emits for Halo 2 shaders
# =====================================================================================================================
DXBC_OPS = {
    0: 'add', 1: 'and', 2: 'break', 3: 'breakc', 4: 'call', 5: 'callc', 6: 'case', 7: 'continue', 8: 'continuec', 9: 'cut', 10: 'default',
    11: 'deriv_rtx', 12: 'deriv_rty', 13: 'discard', 14: 'div', 15: 'dp2', 16: 'dp3', 17: 'dp4', 18: 'else', 19: 'emit', 20: 'emitthencut',
    21: 'endif', 22: 'endloop', 23: 'endswitch', 24: 'eq', 25: 'exp', 26: 'frc', 27: 'ftoi', 28: 'ftou', 29: 'ge', 30: 'iadd', 31: 'if',
    32: 'ieq', 33: 'ige', 34: 'ilt', 35: 'imad', 36: 'imax', 37: 'imin', 38: 'imul', 39: 'ine', 40: 'ineg', 41: 'ishl', 42: 'ishr',
    43: 'itof', 44: 'label', 45: 'ld', 46: 'ld_ms', 47: 'log', 48: 'loop', 49: 'lt', 50: 'mad', 51: 'min', 52: 'max', 53: 'customdata',
    54: 'mov', 55: 'movc', 56: 'mul', 57: 'ne', 58: 'nop', 59: 'not', 60: 'or', 61: 'resinfo', 62: 'ret', 63: 'retc', 64: 'round_ne',
    65: 'round_ni', 66: 'round_pi', 67: 'round_z', 68: 'rsq', 69: 'sample', 70: 'sample_c', 71: 'sample_c_lz', 72: 'sample_l',
    73: 'sample_d', 74: 'sample_b', 75: 'sqrt', 76: 'switch', 77: 'sincos', 78: 'udiv', 79: 'ult', 80: 'uge', 81: 'umul', 82: 'umad',
    83: 'umax', 84: 'umin', 85: 'ushr', 86: 'utof', 87: 'xor', 88: 'dcl_resource', 89: 'dcl_constantbuffer', 90: 'dcl_sampler',
    91: 'dcl_indexrange', 92: 'dcl_gs_output_primitive_topology', 93: 'dcl_gs_input_primitive', 94: 'dcl_maxout', 95: 'dcl_input',
    96: 'dcl_input_sgv', 97: 'dcl_input_siv', 98: 'dcl_input_ps', 99: 'dcl_input_ps_sgv', 100: 'dcl_input_ps_siv', 101: 'dcl_output',
    102: 'dcl_output_sgv', 103: 'dcl_output_siv', 104: 'dcl_temps', 105: 'dcl_indexabletemp', 106: 'dcl_globalflags',
    108: 'lod', 109: 'gather4', 110: 'samplepos', 111: 'sampleinfo', 135: 'bfrev', 136: 'bfi', 137: 'bfrev',
    138: 'ibfe', 139: 'ubfe', 140: 'bfi', 141: 'bfrev',
}
DXBC_N_DST = {'discard': 0, 'if': 0, 'breakc': 0, 'callc': 0, 'continuec': 0, 'retc': 0, 'ret': 0, 'else': 0, 'endif': 0, 'endloop': 0,
              'loop': 0, 'break': 0, 'continue': 0, 'switch': 0, 'case': 0, 'default': 0, 'endswitch': 0, 'nop': 0, 'label': 0,
              'call': 0, 'emit': 0, 'cut': 0, 'emitthencut': 0, 'sincos': 2}
# operand types
DO_TEMP, DO_INPUT, DO_OUTPUT, DO_INDEXABLE, DO_IMM32, DO_IMM64, DO_SAMPLER, DO_RESOURCE, DO_CBUF, DO_ICB, DO_LABEL, DO_PRIMID, \
    DO_DEPTH, DO_NULL = range(14)
DXBC_RES_DIMS = {1: 'buffer', 2: 'texture1d', 3: '2d', 4: '2dms', 5: '3d', 6: 'cube', 7: '1darray', 8: '2darray', 9: '2dmsarray',
                 10: '3dms', 11: 'cubearray'}
SV_NAMES = {0: '', 1: 'POSITION', 2: 'CLIPDIST', 3: 'CULLDIST', 4: 'RTINDEX', 5: 'VPINDEX', 6: 'VERTEXID', 7: 'PRIMID', 8: 'INSTID',
            9: 'ISFRONT', 10: 'SAMPLEIDX', 64: 'TARGET', 65: 'DEPTH', 66: 'COVERAGE'}


class DOperand(object):
    __slots__ = ('otype', 'ncomp', 'mode', 'mask', 'swz', 'sel1', 'idx', 'imm', 'mod')

    def __init__(self):
        self.otype = 0
        self.ncomp = 0
        self.mode = 0
        self.mask = 0xF
        self.swz = (0, 1, 2, 3)
        self.sel1 = 0
        self.idx = []        # list of (imm_value, rel DOperand or None)
        self.imm = None
        self.mod = 0         # 0 none, 1 neg, 2 abs, 3 absneg


class DInst(object):
    __slots__ = ('op', 'name', 'sat', 'nz', 'ctrl', 'ops', 'ext', 'extra')

    def __init__(self):
        self.ext = []
        self.extra = None


class Dxbc(object):
    def __init__(self):
        self.chunks = OrderedDict()
        self.kind = 'ps'
        self.major = 5
        self.minor = 0
        self.isgn = []      # (name, sem_index, sysval, reg, mask)
        self.osgn = []
        self.ins = []
        self.cbuffers = []  # (name, [(var, offset, size)], size)
        self.resources = []   # (name, type, dim, bind)
        self.icb = None
        self.creator = ''

    @property
    def profile(self):
        return '%s_%d_%d' % (self.kind, self.major, self.minor)


def _read_signature(buf, fmt):
    cnt = struct.unpack_from('<I', buf, 0)[0]
    esz = {'ISGN': 24, 'OSGN': 24, 'ISG1': 32, 'OSG1': 32, 'OSG5': 40}[fmt]
    out = []
    for i in range(cnt):
        o = 8 + esz * i
        if fmt == 'OSG5':
            _stream, nameoff, sidx, sysv, ctype, reg, mask = struct.unpack_from('<IIIIIIB', buf, o)
        else:
            nameoff, sidx, sysv, ctype, reg, mask = struct.unpack_from('<IIIIIB', buf, o)
        e = buf.find(b'\0', nameoff)
        out.append((buf[nameoff:e].decode('latin-1').upper(), sidx, sysv, reg, mask))
    return out


def _parse_rdef(buf, dx):
    try:
        (ncb, cb_off, nres, res_off, ver, flags, creator_off) = struct.unpack_from('<IIIIIII', buf, 0)
        e = buf.find(b'\0', creator_off)
        dx.creator = buf[creator_off:e].decode('latin-1')
        for i in range(nres):
            nm, typ, rtype, dim, ns, bind, bcnt, fl = struct.unpack_from('<IIIIIIII', buf, res_off + 32 * i)
            e = buf.find(b'\0', nm)
            dx.resources.append((buf[nm:e].decode('latin-1'), typ, dim, bind))
        for i in range(ncb):
            nm, nvar, voff, size, fl, ctype = struct.unpack_from('<IIIIII', buf, cb_off + 24 * i)
            e = buf.find(b'\0', nm)
            vs = []
            for j in range(nvar):
                vn, vo, vsz, vfl, ti, dv, a, b, c, d = struct.unpack_from('<IIIIIIIIII', buf, voff + 40 * j)
                e = buf.find(b'\0', vn)
                vs.append((buf[vn:e].decode('latin-1'), vo, vsz))
            dx.cbuffers.append((buf[nm:buf.find(b'\0', nm)].decode('latin-1'), vs, size))
    except Exception:
        pass


def parse_dxbc(data):
    data = bytes(data)
    if data[:4] != b'DXBC':
        raise D3D9Error('not a DXBC container')
    total, nchunks = struct.unpack_from('<II', data, 24)[0], struct.unpack_from('<I', data, 28)[0]
    offs = struct.unpack_from('<%dI' % nchunks, data, 32)
    dx = Dxbc()
    for o in offs:
        fc = data[o:o + 4].decode('latin-1')
        sz = struct.unpack_from('<I', data, o + 4)[0]
        dx.chunks[fc] = data[o + 8:o + 8 + sz]
    for fmt in ('ISGN', 'ISG1'):
        if fmt in dx.chunks:
            dx.isgn = _read_signature(dx.chunks[fmt], fmt)
    for fmt in ('OSGN', 'OSG1', 'OSG5'):
        if fmt in dx.chunks:
            dx.osgn = _read_signature(dx.chunks[fmt], fmt)
    if 'RDEF' in dx.chunks:
        _parse_rdef(dx.chunks['RDEF'], dx)
    sh = dx.chunks.get('SHEX') or dx.chunks.get('SHDR')
    if sh is None:
        raise D3D9Error('DXBC without SHEX/SHDR')
    toks = struct.unpack('<%dI' % (len(sh) // 4), sh)
    ver, length = toks[0], toks[1]
    dx.kind = {0: 'ps', 1: 'vs', 2: 'gs', 3: 'hs', 4: 'ds', 5: 'cs'}.get(ver >> 16, '?')
    dx.major, dx.minor = (ver >> 4) & 0xF, ver & 0xF
    p = 2
    end = min(length, len(toks))
    while p < end:
        t = toks[p]
        op = t & 0x7FF
        ln = (t >> 24) & 0x7F
        name = DXBC_OPS.get(op, 'op%d' % op)
        if op == 53:                                       # customdata: length in the next dword
            ln = toks[p + 1]
            cls = (t >> 11) & 0x1FFFFF
            ins = DInst()
            ins.op, ins.name, ins.sat, ins.nz, ins.ctrl, ins.ops = op, name, False, False, t, []
            if cls == 3:                                    # immediate constant buffer
                n4 = (ln - 2) // 4
                dx.icb = np.array(struct.unpack('<%dI' % (n4 * 4), struct.pack('<%dI' % (n4 * 4), *toks[p + 2:p + 2 + n4 * 4]))).reshape(n4, 4)
            ins.extra = cls
            dx.ins.append(ins)
            p += ln
            continue
        if ln == 0:
            raise D3D9Error('zero length DXBC instruction at %d' % p)
        ins = DInst()
        ins.op, ins.name = op, name
        ins.sat = bool(t >> 13 & 1)
        ins.nz = bool(t >> 18 & 1)
        ins.ctrl = t
        q = p + 1
        ext = t >> 31
        while ext:
            et = toks[q]
            q += 1
            ins.ext.append(et)
            ext = et >> 31
        body = toks[q:p + ln]
        ins.ops = []
        if name.startswith('dcl_'):
            ins.extra = list(body)
            k = 0
            if name in ('dcl_temps',):
                ins.extra = body[0]
            elif name in ('dcl_globalflags',):
                ins.extra = (t >> 11) & 0xFFF
            else:
                o, k = _parse_operand(body, 0)
                ins.ops.append(o)
                ins.extra = list(body[k:])
        else:
            k = 0
            while k < len(body):
                o, k = _parse_operand(body, k)
                ins.ops.append(o)
        dx.ins.append(ins)
        p += ln
    return dx


def _parse_operand(toks, p):
    t = toks[p]
    p += 1
    o = DOperand()
    o.ncomp = t & 3
    o.mode = (t >> 2) & 3
    o.otype = (t >> 12) & 0xFF
    dim = (t >> 20) & 3
    sel = (t >> 4) & 0xFF
    if o.ncomp == 2:
        if o.mode == 0:
            o.mask = sel & 0xF
        elif o.mode == 1:
            o.swz = (sel & 3, (sel >> 2) & 3, (sel >> 4) & 3, (sel >> 6) & 3)
        else:
            o.sel1 = sel & 3
            o.swz = (o.sel1,) * 4
    if t >> 31:
        ext = True
        while ext:
            et = toks[p]
            p += 1
            if (et & 0x3F) == 1:
                o.mod = (et >> 6) & 0xFF
            ext = bool(et >> 31)
    for d in range(dim):
        rep = (t >> (22 + 3 * d)) & 7
        if rep == 0:
            o.idx.append((toks[p], None))
            p += 1
        elif rep == 2:
            rel, p = _parse_operand(toks, p)
            o.idx.append((0, rel))
        elif rep == 3:
            v = toks[p]
            p += 1
            rel, p = _parse_operand(toks, p)
            o.idx.append((v, rel))
        else:
            raise Unsupported('DXBC operand index representation %d' % rep)
    if o.otype == DO_IMM32:
        if o.ncomp == 1:
            o.imm = (toks[p],)
            p += 1
        else:
            o.imm = tuple(toks[p:p + 4])
            p += 4
    elif o.otype == DO_IMM64:
        raise Unsupported('64-bit immediates')
    return o, p


def _imm_value(bits):
    """Immediate dword -> float64.  Normal floats and +-0.0 stay floats; 0xFFFFFFFF is the TRUE mask (NaN sentinel); denormal patterns
    (small positive integers) and the NaN/Inf range (negative integers) are taken as int32."""
    bits &= 0xFFFFFFFF
    if bits == 0xFFFFFFFF:
        return float('nan')
    exp = (bits >> 23) & 0xFF
    if exp == 0 and (bits & 0x7FFFFF) == 0:
        return -0.0 if bits >> 31 else 0.0
    if exp not in (0, 0xFF):
        return float(np.array([bits], dtype=np.uint32).view(np.float32)[0])
    return float(np.array([bits], dtype=np.uint32).view(np.int32)[0])


def _truthy(v):
    return np.isnan(v) | (v != 0.0)


def _mask_of(cond):
    return np.where(cond, np.nan, 0.0)


def _f32bits(v):
    v = np.asarray(v, dtype=np.float64)
    b = v.astype(np.float32).view(np.uint32).astype(np.uint64)
    return np.where(np.isnan(v), np.uint64(0xFFFFFFFF), b)


def _bits_to_val(b):
    b = np.asarray(b, dtype=np.uint64) & 0xFFFFFFFF
    out = np.where(b == 0xFFFFFFFF, np.nan, b.astype(np.uint32).view(np.float32).astype(np.float64))
    return out


class DxbcMachine(object):
    """Evaluates a parsed Dxbc for N trials at once (float64 lanes).  Same call signature as D3D9Machine.run().  consts: (N,C,4)
    (register c# of the HLSL source = cb0[#])."""

    def __init__(self, dx):
        if isinstance(dx, (bytes, bytearray)):
            dx = parse_dxbc(dx)
        self.dx = dx
        self.in_map = {}
        self.out_map = {}
        for (nm, sidx, sysv, reg, mask) in dx.isgn:
            key = ('POSITION', 0) if nm == 'SV_POSITION' else (nm, sidx)
            self.in_map[reg] = key
        for (nm, sidx, sysv, reg, mask) in dx.osgn:
            if nm == 'SV_TARGET':
                key = ('COLOR', sidx)
            elif nm == 'SV_DEPTH':
                key = ('DEPTH', 0)
            elif nm == 'SV_POSITION':
                key = ('POSITION', 0)
            else:
                key = (nm, sidx)
            self.out_map[reg] = key
        self.res_kind = {}
        for ins in dx.ins:
            if ins.name == 'dcl_resource':
                self.res_kind[ins.ops[0].idx[0][0]] = DXBC_RES_DIMS.get((ins.ctrl >> 11) & 0x1F, '?')

    def input_semantics(self):
        return sorted(set(self.in_map.values()))

    def run(self, consts, inputs, sampler, int_consts=None, bool_consts=None):
        dx = self.dx
        n = consts.shape[0]
        zero = np.zeros((n, 4))
        regs = {'r': {}, 'o': {}, 'x': {}}
        wmask = {}
        killed = np.zeros(n, dtype=bool)
        active = [np.ones(n, dtype=bool)]
        if_stack = []
        loops = []            # [pc after the loop instruction, broken mask]

        def cur_mask():
            m = active[-1]
            for lp in loops:
                m = m & ~lp[1]
            return m

        def get_idx(o, d):
            v, rel = o.idx[d]
            if rel is None:
                return v
            rv = read(rel)[:, 0]
            return v + np.nan_to_num(rv).astype(np.int64)

        def raw_read(o):
            t = o.otype
            if t == DO_IMM32:
                vals = [_imm_value(b) for b in o.imm]
                if len(vals) == 1:
                    vals = vals * 4
                return np.broadcast_to(np.array(vals), (n, 4))
            if t == DO_TEMP:
                return regs['r'].get(o.idx[0][0], zero)
            if t == DO_INPUT:
                r = o.idx[0][0]
                if r not in self.in_map:
                    raise Unsupported('input v%d not in signature' % r)
                sem = self.in_map[r]
                if sem not in inputs:
                    raise Unsupported('input %s not supplied' % (sem,))
                return inputs[sem]
            if t == DO_CBUF:
                slot = o.idx[0][0]
                if slot != 0:
                    raise Unsupported('constant buffer %d' % slot)
                i1 = get_idx(o, 1)
                if isinstance(i1, np.ndarray):
                    i1 = np.clip(i1, 0, consts.shape[1] - 1)
                    return consts[np.arange(n), i1]
                return consts[:, i1]
            if t == DO_ICB:
                if dx.icb is None:
                    raise Unsupported('immediate constant buffer missing')
                i1 = get_idx(o, 0)
                tab = dx.icb.astype(np.uint32)
                if isinstance(i1, np.ndarray):
                    rows = tab[np.clip(i1, 0, len(tab) - 1)]
                else:
                    rows = np.broadcast_to(tab[i1], (n, 4))
                return np.vectorize(_imm_value)(rows) if rows.size else rows
            if t == DO_INDEXABLE:
                x = regs['x'].get(o.idx[0][0])
                if x is None:
                    raise Unsupported('indexable temp x%d' % o.idx[0][0])
                i1 = get_idx(o, 1)
                return x[np.arange(n), i1] if isinstance(i1, np.ndarray) else x[:, i1]
            if t == DO_OUTPUT:
                return regs['o'].get(o.idx[0][0], zero)
            raise Unsupported('operand type %d' % t)

        def read(o):
            v = raw_read(o)[:, list(o.swz)] if o.ncomp == 2 else raw_read(o)
            if o.mod:
                if o.mod & 2:
                    v = np.abs(v)
                if o.mod & 1:
                    v = -v
            return v

        def write(o, val, sat=False):
            if sat:
                val = _sat(val)
            t = o.otype
            mask = o.mask
            if t == DO_TEMP:
                store, key = regs['r'], o.idx[0][0]
            elif t == DO_OUTPUT:
                store, key = regs['o'], o.idx[0][0]
            elif t == DO_DEPTH:
                store, key = regs['o'], 'depth'
                mask = 1
            elif t == DO_INDEXABLE:
                raise Unsupported('write to indexable temp')
            else:
                raise Unsupported('write to operand type %d' % t)
            cur = store.get(key)
            cur = np.zeros((n, 4)) if cur is None else cur.copy()
            act = cur_mask()
            for lane in range(4):
                if mask >> lane & 1:
                    cur[act, lane] = val[act, lane]
            store[key] = cur
            if store is regs['o']:
                wmask[key] = wmask.get(key, 0) | mask

        def rep(v):
            return np.repeat(v[:, :1], 4, axis=1)

        ins_list = dx.ins
        pc = 0
        steps = 0
        while pc < len(ins_list):
            ins = ins_list[pc]
            pc += 1
            steps += 1
            if steps > 200000:
                raise Unsupported('DXBC program did not terminate')
            nm = ins.name
            if nm.startswith('dcl_') or nm in ('customdata', 'nop'):
                continue
            nd = DXBC_N_DST.get(nm, 1)
            dst = ins.ops[:nd]
            src = ins.ops[nd:]
            if nm == 'ret':
                break
            if nm == 'loop':
                loops.append([pc, np.zeros(n, dtype=bool)])
                continue
            if nm == 'break':
                loops[-1][1] = loops[-1][1] | active[-1]
                continue
            if nm == 'breakc':
                c = (_truthy(read(src[0])[:, 0]) == ins.nz) & active[-1]
                loops[-1][1] = loops[-1][1] | c
                continue
            if nm == 'endloop':
                top = loops[-1]
                alive = active[-1] & ~top[1]
                for lp in loops[:-1]:
                    alive = alive & ~lp[1]
                if alive.any():
                    pc = top[0]
                else:
                    loops.pop()
                continue
            if nm == 'discard':
                c = _truthy(read(src[0])[:, 0])
                killed |= (c == ins.nz) & cur_mask()
                continue
            if nm == 'if':
                c = _truthy(read(src[0])[:, 0]) == ins.nz
                if_stack.append(active[-1])
                active.append(active[-1] & c)
                continue
            if nm == 'else':
                prev = if_stack[-1]
                cond = active[-1]
                active[-1] = prev & ~cond
                continue
            if nm == 'endif':
                active.pop()
                if_stack.pop()
                continue
            if nm in ('continue', 'continuec', 'switch', 'case', 'default', 'endswitch', 'call', 'callc', 'label', 'retc'):
                raise Unsupported('flow control %s' % nm)
            if nm in ('sample', 'sample_l', 'sample_b'):
                S = [read(src[0])] + ([read(src[3])] if len(src) > 3 else [])
            else:
                S = [read(s) for s in src]
            if nm == 'mov':
                r = S[0]
            elif nm == 'add':
                r = S[0] + S[1]
            elif nm == 'mul':
                r = S[0] * S[1]
            elif nm == 'mad':
                r = S[0] * S[1] + S[2]
            elif nm == 'div':
                with np.errstate(all='ignore'):
                    r = S[0] / S[1]
            elif nm == 'dp2':
                r = rep(np.sum(S[0][:, :2] * S[1][:, :2], axis=1, keepdims=True))
            elif nm == 'dp3':
                r = rep(np.sum(S[0][:, :3] * S[1][:, :3], axis=1, keepdims=True))
            elif nm == 'dp4':
                r = rep(np.sum(S[0] * S[1], axis=1, keepdims=True))
            elif nm == 'min':
                r = np.minimum(S[0], S[1])
            elif nm == 'max':
                r = np.maximum(S[0], S[1])
            elif nm == 'rsq':
                with np.errstate(all='ignore'):
                    r = 1.0 / np.sqrt(S[0])
            elif nm == 'sqrt':
                with np.errstate(all='ignore'):
                    r = np.sqrt(S[0])
            elif nm == 'exp':
                r = np.exp2(S[0])
            elif nm == 'log':
                with np.errstate(all='ignore'):
                    r = np.log2(S[0])
            elif nm == 'frc':
                r = S[0] - np.floor(S[0])
            elif nm == 'round_ni':
                r = np.floor(S[0])
            elif nm == 'round_pi':
                r = np.ceil(S[0])
            elif nm == 'round_z':
                r = np.trunc(S[0])
            elif nm == 'round_ne':
                r = np.rint(S[0])
            elif nm == 'lt':
                r = _mask_of(S[0] < S[1])
            elif nm == 'ge':
                r = _mask_of(S[0] >= S[1])
            elif nm == 'eq':
                r = _mask_of(S[0] == S[1])
            elif nm == 'ne':
                r = _mask_of(S[0] != S[1])
            elif nm == 'movc':
                r = np.where(_truthy(S[0]), S[1], S[2])
            elif nm == 'and':
                a, b = S
                fa = np.isnan(a) | (a == 0)
                fb = np.isnan(b) | (b == 0)
                if np.all(fa):
                    r = np.where(np.isnan(a), b, 0.0)
                elif np.all(fb):
                    r = np.where(np.isnan(b), a, 0.0)
                else:
                    r = _bits_to_val(_f32bits(a) & _f32bits(b))
            elif nm == 'or':
                a, b = S
                r = np.where(np.isnan(a) | np.isnan(b), np.nan, np.where(_truthy(a), a, b))
            elif nm == 'not':
                r = np.where(np.isnan(S[0]), 0.0, np.where(S[0] == 0, np.nan, S[0]))
            elif nm in ('ftoi', 'ftou'):
                r = np.trunc(np.nan_to_num(S[0]))
            elif nm in ('itof', 'utof'):
                r = S[0]
            elif nm == 'iadd':
                r = S[0] + S[1]
            elif nm == 'imax':
                r = np.maximum(S[0], S[1])
            elif nm == 'imin':
                r = np.minimum(S[0], S[1])
            elif nm == 'ineg':
                r = -S[0]
            elif nm == 'ilt':
                r = _mask_of(S[0] < S[1])
            elif nm == 'ige':
                r = _mask_of(S[0] >= S[1])
            elif nm == 'ieq':
                r = _mask_of(S[0] == S[1])
            elif nm == 'ine':
                r = _mask_of(S[0] != S[1])
            elif nm == 'imul':
                r = S[0] * S[1]
            elif nm == 'imad':
                r = S[0] * S[1] + S[2]
            elif nm == 'ishl':
                r = S[0] * np.exp2(S[1])
            elif nm in ('sample', 'sample_l', 'sample_b'):
                tex = src[1]
                slot = tex.idx[0][0]
                kind = self.res_kind.get(slot, '2d')
                coords = S[0]
                bias = S[1][:, 0] if len(S) > 1 else None
                res = sampler(kind, slot, coords, bias)
                r = res[:, list(tex.swz)]
            elif nm == 'sincos':
                x = S[0][:, :1]
                if dst[0].otype != DO_NULL:
                    write(dst[0], np.repeat(np.sin(x), 4, axis=1))
                if dst[1].otype != DO_NULL:
                    write(dst[1], np.repeat(np.cos(x), 4, axis=1))
                continue
            else:
                raise Unsupported('DXBC opcode %s' % nm)
            write(dst[0], r, ins.sat)
        outputs, written = {}, {}
        for key, val in regs['o'].items():
            if key == 'depth':
                sem = ('DEPTH', 0)
            else:
                sem = self.out_map.get(key)
                if sem is None:
                    raise Unsupported('output o%s not in signature' % key)
            outputs[sem] = val
            written[sem] = wmask.get(key, 0)
        return Result(outputs, written, killed)


# =====================================================================================================================
# 5. Pixel shader translator: D3D9 bytecode -> HLSL in the style of the H2EK ``.psh`` sources
# =====================================================================================================================
_PSH_MACROS_VOLUME = """\
#ifdef DX11
#define DX11_sampler3D(i)          SamplerState s##i : register(S##i); Texture3D s##i##tex : register(t##i)
#define DX11_tex3D(si, tci)        si##tex.Sample(si, tci.xyz)
#else
#define DX11_sampler3D(i)          sampler3D s##i : register(S##i)
#define DX11_tex3D(si, tci)        tex3D(si, tci)
#endif
"""

# the standard "fog zvalue" footer of the generated .psh files (kept verbatim, including the ALPHA_TEST block)
PSH_FOOTER = """\
//fog zvalue
    float linear_depth = tc7.w / zwriteout.z;
    float3 depth_stripe;

    depth_stripe.r = floor(linear_depth*256)/256;
    depth_stripe.g = frac(floor(linear_depth*65536)/256);
    depth_stripe.b = frac(floor(linear_depth*65536*256)/256);

#ifdef ZPRIME
    //r0.a has alpha kill value
    r0.rgb = depth_stripe.rgb;
#endif

#ifdef ALPHA_TEST
#ifdef ALPHA_TEST_NOT_EQUAL
    clip(any(r0.a - ALPHA_TEST) - 0.5f);
#elif ALPHA_TEST_GREATER
    clip(r0.a - ALPHA_TEST);
    clip(any(r0.a - ALPHA_TEST) - 0.5f);
#elif ALPHA_TEST_LESS
    clip(ALPHA_TEST - r0.a);
    clip(any(r0.a - ALPHA_TEST) - 0.5f);
#endif
#endif // ALPHA_TEST

#ifndef MRT
    return r0;
#else
    MRT_return retval;
    retval.color = r0;
    retval.depth.rgb = depth_stripe.rgb;
    retval.depth.a = r0.a;
    return retval;
#endif
}
"""

# footer of a body that already is the complete shader (no depth stripe): only the alpha-test clip and the return
PSH_FOOTER_PLAIN = """\
#ifdef ALPHA_TEST
#ifdef ALPHA_TEST_NOT_EQUAL
    clip(any(r0.a - ALPHA_TEST) - 0.5f);
#elif ALPHA_TEST_GREATER
    clip(r0.a - ALPHA_TEST);
    clip(any(r0.a - ALPHA_TEST) - 0.5f);
#elif ALPHA_TEST_LESS
    clip(ALPHA_TEST - r0.a);
    clip(any(r0.a - ALPHA_TEST) - 0.5f);
#endif
#endif // ALPHA_TEST
    return r0;
}
"""

_ALPHA_BLOCK_OC0 = PSH_FOOTER_PLAIN.split('    return r0;')[0].replace('r0.a', 'oC0.a')
_NUM_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
# names the generated body uses itself (a CTAB constant must not shadow them)
_RESERVED_RE = re.compile(r'^(r\d+|tc\d+|tcx\d+|color\d|oC\d|oDepth|a0|s\d+|retval|linear_depth|depth_stripe|main|pos_unused|float\d*|int\d*)$')


def _fnum(f):
    """Float literal that survives a round trip through the HLSL parser to the same float32."""
    f = float(f)
    if f != f:
        return '(0.0/0.0)'
    if math.isinf(f):
        return '(1.0/0.0)' if f > 0 else '(-1.0/0.0)'
    s = '%.9g' % f
    if f == 0.0 and math.copysign(1.0, f) < 0:
        return '-0.0'
    if 'e' in s or '.' in s:
        return s
    return s + '.0'


class _Xlat(object):
    """One D3D9 Shader -> list of HLSL statements.  Pure data flow: nothing is guessed, anything unknown raises Unsupported."""

    def __init__(self, sh, clamp_colors=False, color_names=('color0', 'color1'), tc_name='tc'):
        self.sh = sh
        self.clamp_colors = clamp_colors
        self.defs = sh.defs()
        self.ints = {}
        self.lines = []
        self.used_temps = set()
        self.used_consts = set()
        self.used_samplers = {}
        self.used_inputs = set()
        self.written_outputs = set()
        self.tmpn = 0
        self.indent = '    '
        self.uses_a0 = False
        self.rel_const = False
        self.color_names = color_names
        self.tc_name = tc_name
        m = D3D9Machine(sh)
        self.in_names = {}
        for key, sem in m.in_map.items():
            self.in_names[key] = ('i.v%d' % key[1]) if sh.kind == 'vs' else self._sem_name(sem)
        self.sampler_kind = m.sampler_kinds()
        self.ctab_names = {}
        if sh.ctab is not None:
            seen = set()
            for c in sh.ctab.consts:
                nm = c.name or ''
                if c.rset == 2 and c.count == 1 and _NUM_RE.match(nm) and nm not in seen and not _RESERVED_RE.match(nm)                         and (nm != 'c%d' % c.index and not re.match(r'^c\d+$', nm) or nm == 'c%d' % c.index):
                    self.ctab_names[c.index] = nm
                    seen.add(nm)

    @staticmethod
    def _sem_name(sem):
        nm, idx = sem
        if nm == 'COLOR':
            return 'color%d' % idx
        if nm == 'TEXCOORD':
            return 'tc%d' % idx
        if nm == 'POSITION':
            return 'pos_unused'
        return '%s%d' % (nm.lower(), idx)

    # ---- helpers --------------------------------------------------------------------------------------------------
    def emit(self, s):
        self.lines.append(self.indent + s)

    def new_tmp(self):
        self.tmpn += 1
        return 'tcx%d' % (self.tmpn - 1)

    def const_name(self, num):
        return self.ctab_names.get(num, 'c%d' % num)

    def reg_text(self, p, loopv):
        t, k = p.rtype, p.num
        if p.rel is not None:
            r = p.rel
            if r.rtype == R_LOOP:
                idx = k + loopv
                if t == R_INPUT:
                    key = (R_INPUT, idx)
                    if key not in self.in_names:
                        raise Unsupported('v[aL] resolves to missing v%d' % idx)
                    self.used_inputs.add(key)
                    return self.in_names[key]
                if t == R_CONST:
                    return self.const_reg(idx)
                raise Unsupported('relative %d' % t)
            if r.rtype == R_ADDR and self.sh.kind == 'vs' and t == R_CONST:
                self.uses_a0 = True
                self.rel_const = True
                return 'c[a0.%s%s]' % ('xyzw'[r.swz[0]], ('+%d' % k) if k else '')
            raise Unsupported('relative addressing')
        if t == R_TEMP:
            self.used_temps.add(k)
            return 'r%d' % k
        if t == R_CONST:
            return self.const_reg(k)
        if t in (R_INPUT, R_TEXTURE, R_MISC):
            key = (t, k)
            if key not in self.in_names:
                raise Unsupported('input register %s without semantic' % reg_name(self.sh, t, k))
            self.used_inputs.add(key)
            return self.in_names[key]
        if t == R_ADDR and self.sh.kind == 'vs':
            self.uses_a0 = True
            return 'a0'
        raise Unsupported('source register type %d' % t)

    def const_reg(self, k):
        if k in self.defs:
            return None            # literal, handled by src()
        self.used_consts.add(k)
        if self.sh.kind == 'vs':
            return 'c[%d]' % k
        return self.const_name(k)

    def src(self, p, lanes, loopv=0):
        """Text of source operand p restricted to the destination lanes (list of lane indices), with swizzle and modifier applied."""
        comps = [p.swz[l] for l in lanes]
        if p.rtype == R_CONST and p.rel is None and p.num in self.defs:
            vals = [self.defs[p.num][c] for c in comps]
            body = _fnum(vals[0]) if len(vals) == 1 else 'float%d(%s)' % (len(vals), ', '.join(_fnum(v) for v in vals))
            if body.startswith('-'):
                body = '(%s)' % body          # never emit '--x'
        else:
            base = self.reg_text(p, loopv)
            body = '%s.%s' % (base, ''.join('xyzw'[c] for c in comps))
            if self.clamp_colors and p.rtype == R_INPUT and self.sh.kind == 'ps' and base.startswith('color'):
                body = 'saturate(%s)' % body
        m = p.srcmod
        if m == 0:
            return body
        if m == SM_NEG:
            return '(-%s)' % body
        if m == SM_BIAS:
            return '(%s - 0.5)' % body
        if m == SM_BIASNEG:
            return '(-(%s - 0.5))' % body
        if m == SM_SIGN:
            return '((%s - 0.5) * 2.0)' % body
        if m == SM_SIGNNEG:
            return '(-((%s - 0.5) * 2.0))' % body
        if m == SM_COMP:
            return '(1.0 - %s)' % body
        if m == SM_X2:
            return '(%s * 2.0)' % body
        if m == SM_X2NEG:
            return '(-(%s * 2.0))' % body
        if m == SM_ABS:
            return 'abs(%s)' % body
        if m == SM_ABSNEG:
            return '(-abs(%s))' % body
        raise Unsupported('source modifier %s' % SRCMOD_NAMES.get(m, m))

    def dst_lanes(self, p):
        return [l for l in range(4) if p.mask >> l & 1]

    def dst_ref(self, p, lanes):
        t, k = p.rtype, p.num
        if p.rel is not None:
            raise Unsupported('relative destination')
        sw = ''.join('xyzw'[l] for l in lanes)
        if t == R_TEMP:
            self.used_temps.add(k)
            return 'r%d.%s' % (k, sw)
        if t == R_COLOROUT:
            self.written_outputs.add(('oC', k))
            return 'oC%d.%s' % (k, sw)
        if t == R_DEPTHOUT:
            self.written_outputs.add(('oDepth', 0))
            return 'oDepth'
        if t == R_ADDR and self.sh.kind == 'vs':
            self.uses_a0 = True
            return 'a0.%s' % sw
        if self.sh.kind == 'vs' and t in (R_RASTOUT, R_ATTROUT, R_TEXCRDOUT):
            self.written_outputs.add((t, k))
            if t == R_RASTOUT and k > 0:
                return self.vs_out_name(t, k)               # oFog / oPts are scalar (FOG0 / PSIZE0 are single-component outputs in MCC)
            return '%s.%s' % (self.vs_out_name(t, k), sw)
        raise Unsupported('destination register type %d' % t)

    def vs_out_name(self, t, k):
        if t == R_RASTOUT:
            return ('oPos', 'o.oFog', 'o.oPts')[k]
        if t == R_ATTROUT:
            return 'o.oD%d' % k
        if t == R_TEXCRDOUT:
            if self.sh.major >= 3:
                raise Unsupported('vs_3_0 outputs')
            return 'o.oT%d' % k
        raise Unsupported('vs output register')

    # ---- instruction emission -------------------------------------------------------------------------------------
    def finish_value(self, p, rhs):
        if p.shift:
            rhs = '(%s) * %s' % (rhs, _fnum(2.0 ** p.shift))
        if p.mod_bits & 1:
            rhs = 'saturate(%s)' % rhs
        return rhs

    def assign(self, ins, rhs, lanes):
        p = ins.dst
        self.emit('%s = %s;' % (self.dst_ref(p, lanes), self.finish_value(p, rhs)))

    def run(self):
        sh = self.sh
        ins_list = sh.ins
        pc = 0
        while pc < len(ins_list):
            ins = ins_list[pc]
            nm = ins.name
            if nm in ('dcl', 'def', 'defi', 'defb', 'nop', 'phase'):
                if nm == 'defi':
                    self.ints[ins.dst.num] = tuple(ins.extra)
                pc += 1
                continue
            if nm == 'loop':
                cnt = self.ints.get(ins.srcs[1].num)
                if cnt is None or ins.srcs[0].rtype != R_LOOP:
                    raise Unsupported('loop with non-constant count')
                # unroll: find the matching endloop
                depth, q = 1, pc + 1
                while depth:
                    depth += {'loop': 1, 'endloop': -1}.get(ins_list[q].name, 0)
                    q += 1
                body = ins_list[pc + 1:q - 1]
                count, start, step = cnt[0], cnt[1], cnt[2]
                self.emit('// unrolled: loop aL, i%d  (count %d, start %d, step %d)' % (ins.srcs[1].num, count, start, step))
                for it in range(max(count, 0)):
                    loopv = start + it * step
                    for b in body:
                        self.one(b, loopv)
                pc = q
                continue
            if nm == 'endloop':
                raise Unsupported('stray endloop')
            self.one(ins, 0)
            pc += 1
        return self.lines

    def one(self, ins, loopv):
        nm = ins.name
        if nm in ('dcl', 'def', 'defi', 'defb', 'nop', 'phase'):
            return
        if nm in ('rep', 'endrep', 'if', 'ifc', 'else', 'endif', 'call', 'callnz', 'ret', 'label', 'break', 'breakc', 'breakp', 'setp',
                  'loop', 'endloop'):
            raise Unsupported('flow control instruction %s' % nm)
        if ins.pred is not None:
            raise Unsupported('predicated instruction')
        if nm == 'texkill':
            self.emit('clip(%s.xyz);' % self.reg_text(ins.dst, loopv))
            return
        p = ins.dst
        lanes = self.dst_lanes(p)
        if self.sh.kind == 'vs' and p.rtype == R_RASTOUT and p.num > 0:
            lanes = [0] if p.mask & 1 else []               # oFog / oPts only have an x component
        n = len(lanes)
        if not lanes and nm != 'mova':
            return
        S = lambda i, ls=None: self.src(ins.srcs[i], lanes if ls is None else ls, loopv)

        def scalar(i):
            return self.src(ins.srcs[i], [0], loopv)

        if nm == 'mov':
            rhs = S(0)
        elif nm == 'add':
            rhs = '%s + %s' % (S(0), S(1))
        elif nm == 'sub':
            rhs = '%s - %s' % (S(0), S(1))
        elif nm == 'mul':
            rhs = '%s * %s' % (S(0), S(1))
        elif nm == 'mad':
            rhs = '%s * %s + %s' % (S(0), S(1), S(2))
        elif nm == 'dp3':
            rhs = 'dot(%s, %s)' % (S(0, [0, 1, 2]), S(1, [0, 1, 2]))
        elif nm == 'dp4':
            rhs = 'dot(%s, %s)' % (S(0, [0, 1, 2, 3]), S(1, [0, 1, 2, 3]))
        elif nm == 'dp2add':
            rhs = 'dot(%s, %s) + %s' % (S(0, [0, 1]), S(1, [0, 1]), scalar(2))
        elif nm == 'rcp':
            rhs = '1.0 / %s' % scalar(0)
        elif nm == 'rsq':
            rhs = 'rsqrt(abs(%s))' % scalar(0)
        elif nm in ('exp', 'expp'):
            rhs = 'exp2(%s)' % scalar(0)
        elif nm in ('log', 'logp'):
            rhs = 'log2(abs(%s))' % scalar(0)
        elif nm == 'pow':
            rhs = 'pow(abs(%s), %s)' % (scalar(0), scalar(1))
        elif nm == 'min':
            rhs = 'min(%s, %s)' % (S(0), S(1))
        elif nm == 'max':
            rhs = 'max(%s, %s)' % (S(0), S(1))
        elif nm == 'slt':
            rhs = '(%s < %s) ? 1.0 : 0.0' % (S(0), S(1)) if n == 1 else '(float%d)(%s < %s)' % (n, S(0), S(1))
        elif nm == 'sge':
            rhs = '(%s >= %s) ? 1.0 : 0.0' % (S(0), S(1)) if n == 1 else '(float%d)(%s >= %s)' % (n, S(0), S(1))
        elif nm == 'frc':
            rhs = 'frac(%s)' % S(0)
        elif nm == 'lrp':
            rhs = 'lerp(%s, %s, %s)' % (S(2), S(1), S(0))
        elif nm == 'cmp':
            rhs = '(%s >= 0.0) ? %s : %s' % (S(0), S(1), S(2))
        elif nm == 'abs':
            rhs = 'abs(%s)' % S(0)
        elif nm == 'nrm':
            a3 = S(0, [0, 1, 2])
            rhs = '%s * rsqrt(dot(%s, %s))' % (S(0), a3, a3)
        elif nm == 'crs':
            if any(l > 2 for l in lanes):
                raise Unsupported('crs writing w')
            rhs = 'cross(%s, %s).%s' % (S(0, [0, 1, 2]), S(1, [0, 1, 2]), ''.join('xyzw'[l] for l in lanes))
        elif nm == 'sincos':
            x = scalar(0)
            parts = []
            for l in lanes:
                if l == 0:
                    parts.append('cos(%s)' % x)
                elif l == 1:
                    parts.append('sin(%s)' % x)
                else:
                    parts.append('0.0')
            rhs = parts[0] if n == 1 else 'float%d(%s)' % (n, ', '.join(parts))
        elif nm == 'mova':
            self.uses_a0 = True
            lanes = [l for l in range(4) if p.mask >> l & 1]
            self.emit('a0.%s = (int%d)floor(%s + 0.5);' % (''.join('xyzw'[l] for l in lanes), len(lanes), S(0)))
            return
        elif nm == 'dst':
            a, b = ins.srcs[0], ins.srcs[1]
            parts = []
            for l in lanes:
                parts.append({0: '1.0', 1: '%s * %s' % (self.src(a, [1], loopv), self.src(b, [1], loopv)),
                              2: self.src(a, [2], loopv), 3: self.src(b, [3], loopv)}[l])
            rhs = parts[0] if n == 1 else 'float%d(%s)' % (n, ', '.join('(%s)' % x for x in parts))
        elif nm == 'lit':
            raise Unsupported('lit')
        elif nm == 'texld':
            mode = ins.ctrl & 3
            sidx = ins.srcs[1].num
            kind = self.sampler_kind.get(sidx, '2d')
            self.used_samplers[sidx] = kind
            if mode == 2:
                raise Unsupported('texldb')
            if kind == '2d':
                if mode == 1:
                    coord = self.coord(ins.srcs[0], [0, 1, 2, 3], loopv)
                    call = 'DX11_tex2Dproj(s%d, %s)' % (sidx, coord)
                else:
                    coord = self.coord(ins.srcs[0], [0, 1], loopv)
                    call = 'DX11_tex2D(s%d, %s)' % (sidx, coord)
            elif kind == 'cube':
                if mode == 1:
                    raise Unsupported('projective cube sample')
                coord = self.coord(ins.srcs[0], [0, 1, 2], loopv)
                call = 'DX11_texCUBE(s%d, %s)' % (sidx, coord)
            elif kind == '3d':
                if mode == 1:
                    coord = self.coord(ins.srcs[0], [0, 1, 2, 3], loopv)
                    t = self.new_tmp()
                    self.emit('float4 %s = %s;' % (t, coord))
                    self.emit('%s.xyz = %s.xyz / %s.w;' % (t, t, t))
                    coord = t
                else:
                    coord = self.coord(ins.srcs[0], [0, 1, 2], loopv)
                call = 'DX11_tex3D(s%d, %s)' % (sidx, coord)
            else:
                raise Unsupported('sampler kind %s' % kind)
            # the sample is float4; take the destination lanes in order
            sw = ''.join('xyzw'[l] for l in lanes)
            rhs = '%s.%s' % (call, sw)
        else:
            raise Unsupported('opcode %s' % nm)
        self.assign(ins, rhs, lanes)

    def coord(self, p, lanes, loopv):
        """Texture-coordinate operand for the DX11_tex* macros: they append .xy / .xyz / .xy/.w to it, so it has to be a plain (swizzled)
        register; anything else (modifiers, literals) goes through a temporary."""
        txt = self.src(p, lanes, loopv)
        if re.match(r'^[A-Za-z_][A-Za-z0-9_]*\.[xyzw]{%d}$' % len(lanes), txt) or (re.match(r'^saturate\(', txt) is None and False):
            return txt
        t = self.new_tmp()
        self.emit('float%d %s = %s;' % (len(lanes), t, txt))
        return t


# ---------------------------------------------------------------------------------------------------------------------
# 5b. .psh text assembly, H2EK compile recipe, equivalence proof for a pixel shader
# ---------------------------------------------------------------------------------------------------------------------
# macro headers that tool.exe prepends before fxc (extracted from tool.exe at 0xA5E988 / 0xA5EAB0; DX9 and DX11 flavours)
H2EK_MACROS_DX9 = (
    "#define DX11_sampler2D(i)          sampler2D s##i : register(S##i)\n"
    "#define DX11_samplerCUBE(i)        samplerCUBE s##i : register(S##i)\n"
    "#define DX11_tex2D(si, tci)        tex2D(si, tci)\n"
    "#define DX11_tex2Dproj(si, tci)    tex2Dproj(si, tci)\n"
    "#define DX11_texCUBE(si, tci)      texCUBE(si, tci)\n")
H2EK_MACROS_DX11 = (
    "#define DX11_sampler2D(i)          SamplerState s##i : register(S##i); Texture2D s##i##tex : register(t##i)\n"
    "#define DX11_samplerCUBE(i)        SamplerState s##i : register(S##i); TextureCube s##i##tex : register(t##i)\n"
    "#define DX11_tex2D(si, tci)        si##tex.Sample(si, tci.xy)\n"
    "#define DX11_tex2Dproj(si, tci)    si##tex.Sample(si, tci.xy/tci.w)\n"
    "#define DX11_texCUBE(si, tci)      si##tex.Sample(si, tci.xyz)\n")

# the three compiled variants of one pixel shader element, in spas order: [0] normal, [1] z-prime, [2] MRT (ps_3_0 on D3D9)
PS_VARIANTS = (('DX11', {'DX11': 1}), ('ZPRIME', {'DX11': 1, 'ZPRIME': 1}), ('MRT', {'DX11': 1, 'MRT': 1}))
D3D9_PS_PROFILES = ('ps_2_0', 'ps_2_0', 'ps_3_0')


def prepare_psh_source(text, dx11=True):
    """tool.exe preprocessing of a .psh chunk: ';' comment lines and '#const' lines are consumed by the tool (they feed the pass tag), the
    macro header of the target API is prepended.  Line numbers shift by the length of the header only."""
    out = []
    for l in text.replace('\r\n', '\n').split('\n'):
        s = l.lstrip()
        out.append('' if (s.startswith(';') or s.startswith('#const')) else l)
    return (H2EK_MACROS_DX11 if dx11 else H2EK_MACROS_DX9) + '\n'.join(out)


def split_psh_cases(text):
    """A .psh with '#case $a=0 $b=1' lines holds one complete shader text per permutation (the pixel shader block elements, in order).
    Returns [(case_line, chunk)]; a file without #case is one chunk."""
    lines = text.replace('\r\n', '\n').split('\n')
    idx = [i for i, l in enumerate(lines) if l.startswith('#case')]
    if not idx:
        return [('', text)]
    out = []
    for n, i in enumerate(idx):
        j = idx[n + 1] if n + 1 < len(idx) else len(lines)
        out.append((lines[i], '\n'.join(lines[i + 1:j])))
    return out


def alpha_test_defines(alpha_ref, func):
    """MCC tool: ALPHA_TEST=<ref/255 as %f> plus one of ALPHA_TEST_GREATER / _NOT_EQUAL / _LESS (tool.exe strings; verified byte exact against
    the stock MCC 'lightmap\\decorator' blob: ALPHA_TEST=0.501961 + ALPHA_TEST_GREATER)."""
    name = {'greater': 'ALPHA_TEST_GREATER', 'not_equal': 'ALPHA_TEST_NOT_EQUAL', 'less': 'ALPHA_TEST_LESS'}[func]
    return {'ALPHA_TEST': '%f' % (alpha_ref / 255.0), name: 1}


# D3DRS ids used in the Vista pass Render States block (state index u8, value u32): ALPHATESTENABLE, ALPHAREF, ALPHAFUNC
D3DRS_ALPHATESTENABLE, D3DRS_ALPHAREF, D3DRS_ALPHAFUNC = 15, 24, 25
_D3DCMP_TO_FUNC = {2: 'less', 5: 'greater', 6: 'not_equal'}      # the only compare functions the MCC tool accepts (tool.exe assertion text)


def alpha_test_from_render_states(states):
    """Active render states of one pass implementation ([(state index, value)] = Render States[rs_start : default_start])
    -> {'ALPHA_TEST': '0.501961', 'ALPHA_TEST_GREATER': 1} or {} (no alpha test).  The LAST write of each state wins, like the device."""
    cur = {}
    for idx, val in states:
        cur[idx] = val
    if not cur.get(D3DRS_ALPHATESTENABLE):
        return {}
    func = _D3DCMP_TO_FUNC.get(cur.get(D3DRS_ALPHAFUNC))
    if func is None:
        raise Unsupported('alpha function %s is not supported by the MCC tool' % cur.get(D3DRS_ALPHAFUNC))
    return alpha_test_defines(cur.get(D3DRS_ALPHAREF, 0), func)


def compile_psh(text, variant=0, extra_defines=None, dx11=True, profile=None):
    """Compile one .psh chunk exactly like the H2EK tool does for MCC: ps_5_0, entry main, /Gec, -DDX11 [-DZPRIME | -DMRT] (variant 0/1/2)."""
    defs = dict(PS_VARIANTS[variant][1]) if dx11 else ({'ZPRIME': 1} if variant == 1 else ({'MRT': 1} if variant == 2 else {}))
    defs.update(extra_defines or {})
    prof = profile or ('ps_5_0' if dx11 else D3D9_PS_PROFILES[variant])
    return compile_hlsl(prepare_psh_source(text, dx11), prof, 'main', defs)


class PshResult(object):
    """Result of translate_ps()."""

    def __init__(self):
        self.source = ''
        self.strategy = ''            # 'footer' | 'plain' | 'variant'
        self.proof = []               # [CompareReport per variant] (D3D9 original vs compile(translated .psh))
        self.ok = False
        self.notes = []
        self.dxbc = []                # compiled DXBC of the translated source, per variant (b'' for an absent variant)
        self.present = [True, True, True]   # variants that exist in the Vista pass
        self.unsupported = None

    def summary(self):
        return '%s strategy=%s %s' % ('PROVEN' if self.ok else 'NOT PROVEN', self.strategy,
                                      '; '.join(self.notes[:3]))

    def compile(self, variant, extra_defines=None):
        """DXBC of one MCC variant (0 normal / 1 z-prime / 2 MRT) with optional extra defines (the pass implementation's alpha test)."""
        if not self.present[variant]:
            raise CompileError('variant %d does not exist in the Vista pass' % variant)
        return compile_psh(self.source, variant, extra_defines)


def _decl_block(samplers, consts, ctab_names, extra_consts=()):
    lines = []
    need_volume = any(k == '3d' for k in samplers.values())
    if need_volume:
        lines.append(_PSH_MACROS_VOLUME.rstrip('\n'))
    for s in sorted(samplers):
        k = samplers[s]
        lines.append({'2d': 'DX11_sampler2D(%d);', 'cube': 'DX11_samplerCUBE(%d);', '3d': 'DX11_sampler3D(%d);'}[k] % s)
    names = {}
    decl = {}
    if 0 not in consts:
        decl[0] = ('bumpenv', '// .r=m00 .g=m01 .b=m10 .a=m11')
    for k in sorted(consts):
        decl[k] = (ctab_names.get(k, 'c%d' % k), '')
    for k in sorted(decl):
        nm, cm = decl[k]
        names[nm] = k
        # lower case on purpose: D3DCompile honours register(cN) as an exact cbuffer slot, but register(CN) (upper case) only as a hint
        # (sequential packing) - the H2EK sources get away with C0..Cn because they declare EVERY register in order
        lines.append('float4 %s : register(c%d);%s' % (nm, k, ('    ' + cm) if cm else ''))
    for nm, k in extra_consts:
        if k not in consts:
            lines.append('float4 %s : register(c%d);' % (nm, k))
    return lines


_MRT_STRUCT = """\
struct MRT_return
{
    float4 color :COLOR0;
    float4 depth :COLOR1;
};
"""


def _main_head(used_tc, mrt_struct=True):
    """The H2EK entry point head (verbatim from the generated .psh files); DX9 branch lists the used texture coordinate registers."""
    out = []
    if mrt_struct:
        out.append("#ifndef MRT\nfloat4 main(\n#else\nMRT_return main(\n#endif")
    else:
        out.append("float4 main(")
    out.append("#ifdef DX11\n    float4 pos_unused : SV_POSITION,\n#endif\n    float4 color0 : COLOR0,\n    float4 color1 : COLOR1,")
    out.append("#ifdef DX11\n" + ''.join('    float4 tc%d : TEXCOORD%d%s\n' % (i, i, ',' if i < 7 else '') for i in range(8)) + "#else")
    for i in sorted(set(used_tc) - {7}):
        out.append("    float4 tc%d : TEXCOORD%d," % (i, i))
    out.append("    float4 tc7:TEXCOORD7\n#endif // DX11\n)")
    if mrt_struct:
        out.append("#ifndef MRT\n    : COLOR\n#endif")
    else:
        out.append("    : COLOR")
    out.append("{")
    return '\n'.join(out)


def _temps_decl(used_temps, extra=()):
    names = ['r%d = 0' % k for k in sorted(used_temps)]
    if not names:
        names = ['r0 = 0']
    return '    float4 %s;' % ', '.join(names)




def _build_footer_source(name, xl, ctab_names, note=''):
    samplers = dict(xl.used_samplers)
    decls = _decl_block(samplers, xl.used_consts, ctab_names, extra_consts=[('zwriteout', 16)])
    tcs = {int(xl.in_names[key][2:]) for key in xl.used_inputs if xl.in_names[key].startswith('tc')}
    txt = ['// translated from Vista D3D9 bytecode by h2conv.rev.d3d9_to_psh%s' % (' - ' + name if name else ''), '// ' + note if note else '//']
    txt += decls
    txt.append(_MRT_STRUCT.rstrip('\n'))
    txt.append(_main_head(tcs | {7}, True))
    txt.append(_temps_decl(xl.used_temps | {0}))
    txt.append('    float4 oC0 = 0;')
    txt += xl.lines
    txt.append('    r0 = oC0;')
    txt.append('')
    txt.append(PSH_FOOTER.rstrip('\n'))
    return '\n'.join(txt) + '\n'


def _build_plain_source(name, xl, ctab_names, note=''):
    samplers = dict(xl.used_samplers)
    decls = _decl_block(samplers, xl.used_consts, ctab_names)
    tcs = {int(xl.in_names[key][2:]) for key in xl.used_inputs if xl.in_names[key].startswith('tc')}
    txt = ['// translated from Vista D3D9 bytecode by h2conv.rev.d3d9_to_psh%s' % (' - ' + name if name else ''), '// ' + note if note else '//']
    txt += decls
    txt.append(_main_head(tcs | {7}, False))
    txt.append(_temps_decl(xl.used_temps | {0}))
    txt.append('    float4 oC0 = 0;')
    txt += xl.lines
    txt.append('    r0 = oC0;')
    txt.append(PSH_FOOTER_PLAIN.rstrip('\n'))
    return '\n'.join(txt) + '\n'


def _build_variant_source(name, xls, ctab_names, note=''):
    """One self-contained body per variant: #if MRT / #elif ZPRIME / #else.  xls = [xl0, xl1, xl2]; a None entry is a variant that the Vista
    pass does not have (empty compiled shader): its branch is an #error so nobody compiles it by accident."""
    samplers, consts, tcs = {}, set(), {7}
    for xl in xls:
        if xl is None:
            continue
        samplers.update(xl.used_samplers)
        consts |= xl.used_consts
        tcs |= {int(xl.in_names[key][2:]) for key in xl.used_inputs if xl.in_names[key].startswith('tc')}
    two_out = xls[2] is not None and ('oC', 1) in xls[2].written_outputs
    decls = _decl_block(samplers, consts, ctab_names)
    txt = ['// translated from Vista D3D9 bytecode by h2conv.rev.d3d9_to_psh%s' % (' - ' + name if name else ''), '// ' + note if note else '//']
    txt += decls
    if two_out:
        txt.append(_MRT_STRUCT.rstrip('\n'))
    txt.append(_main_head(tcs, two_out))
    for vi, (kw, xl) in enumerate((('#if defined(MRT)', xls[2]), ('#elif defined(ZPRIME)', xls[1]), ('#else', xls[0]))):
        txt.append(kw)
        if xl is None:
            txt.append('#error "the Vista pass has no compiled shader for this variant (empty ps blob)"')
            txt.append('    return 0;')
            continue
        txt.append(_temps_decl(xl.used_temps | {0}))
        txt.append('    float4 oC0 = 0, oC1 = 0;')
        txt += xl.lines
        txt.append(_ALPHA_BLOCK_OC0.rstrip('\n'))
        if kw.startswith('#if') and two_out:
            txt.append('    MRT_return retval;\n    retval.color = oC0;\n    retval.depth = oC1;\n    return retval;')
        else:
            txt.append('    return oC0;')
    txt.append('#endif')
    txt.append('}')
    return '\n'.join(txt) + '\n'



def verify_psh_text(text, blobs, n=48, seed=11, rtol=2e-6, atol=2e-7, sampler=None, case_extra=None, stress=False):
    """Compile `text` in the 3 MCC variants and compare each with the matching D3D9 blob by random-input evaluation.
    Returns [CompareReport] (one per variant; a failed compile yields a failed report with the compiler message)."""
    reps = []
    for vi in range(3):
        rep = CompareReport()
        if not blobs[vi]:
            rep.notes.append('variant %d is absent in the Vista pass (empty blob): nothing to compare' % vi)
            reps.append(rep)
            continue
        try:
            dxbc = compile_psh(text, vi, case_extra)
            ma, mb = D3D9Machine(blobs[vi]), DxbcMachine(dxbc)
            rep = compare_machines(ma, mb, n=n, seed=seed + vi, rtol=rtol, atol=atol, sampler=sampler, stress=stress)
        except CompileError as e:
            rep.fail('compile error: ' + str(e).strip().split('\n')[0][-160:])
        except Unsupported as e:
            rep.fail('evaluator unsupported: %s' % e)
        reps.append(rep)
    return reps


def translate_ps(blobs, name='', strategies=None, clamp_colors=True, n=48, seed=11, verify=True):
    """Translate the three D3D9 blobs of one pixel shader element ([ps_2_0 normal, ps_2_0 z-prime, ps_3_0 MRT]) into ONE H2EK-style .psh
    text and PROVE it: the text is compiled with the H2EK recipe in the three MCC variants and every compiled variant is evaluated against
    the original bytecode on random inputs.  The first strategy whose variants all agree is returned ('footer' = body of
    the normal variant + the standard fog/z-prime/MRT footer, 'plain' = body only, 'variant' = one body per variant); if none does, the best
    attempt is returned with ok=False (never silently accepted).  An empty blob (a variant the Vista build never compiled) is reported in
    `present` and left out of the proof; only the 'variant' strategy can express it."""
    res = PshResult()
    blobs = [bytes(b) for b in blobs]
    if len(blobs) != 3:
        raise ValueError('expected the three compiled variants of the pixel shader element')
    res.present = [len(b) > 0 for b in blobs]
    if not any(res.present):
        res.unsupported = 'all three variants are empty'
        res.notes.append(res.unsupported)
        return res
    if strategies is None:
        strategies = ('footer', 'plain', 'variant') if all(res.present) else ('variant',)
    try:
        shs = [parse(b) if b else None for b in blobs]
    except D3D9Error as e:
        res.unsupported = 'parse: %s' % e
        res.notes.append(res.unsupported)
        return res
    best = None
    for strat in strategies:
        try:
            if strat in ('footer', 'plain'):
                if shs[0] is None:
                    raise Unsupported('the %s strategy needs the normal variant' % strat)
                xl = _Xlat(shs[0], clamp_colors=clamp_colors)
                xl.run()
                src = (_build_footer_source if strat == 'footer' else _build_plain_source)(name, xl, xl.ctab_names, 'strategy ' + strat)
            else:
                xls = []
                ctab = {}
                for vi in (2, 1, 0):
                    pass
                for vi, sh in enumerate(shs):
                    if sh is None:
                        xls.append(None)
                        continue
                    xl = _Xlat(sh, clamp_colors=clamp_colors)
                    xl.run()
                    xls.append(xl)
                for vi in (2, 1, 0):
                    if xls[vi] is not None:
                        ctab.update(xls[vi].ctab_names)
                src = _build_variant_source(name, xls, ctab, 'strategy variant')
        except Unsupported as e:
            res.notes.append('%s: unsupported (%s)' % (strat, e))
            res.unsupported = str(e)
            continue
        if not verify:
            res.source, res.strategy, res.ok = src, strat, False
            return res
        reps = verify_psh_text(src, blobs, n=n, seed=seed)
        if all(r.ok for r in reps):
            res.source, res.strategy, res.proof, res.ok = src, strat, reps, True
            res.unsupported = None
            res.dxbc = [compile_psh(src, vi) if res.present[vi] else b'' for vi in range(3)]
            return res
        res.notes.append('%s: %s' % (strat, ' | '.join('v%d: %s' % (i, '; '.join(r.notes[:1]) or 'ok') for i, r in enumerate(reps) if not r.ok)))
        if best is None:
            best = (src, strat, reps)
    if best is not None:
        res.source, res.strategy, res.proof = best
    return res


# ---------------------------------------------------------------------------------------------------------------------
# 6. Vertex shaders: vs_2_0 -> MCC style vs_5_0 HLSL
#
# MCC's vertex shaders (Geometry Classifications DX11 of a vrtx tag) are fxc products of a generated wrapper (the editing kit generates
# 'temp\vertex_shaders_dx9\<name>__<class>.vsh.fx' with _vertex_shader_import.py; its source is not shipped).  What can be MEASURED on the 580
# stock MCC blobs:
#   * vs_5_0, 'Microsoft (R) HLSL Shader Compiler 10.1', flags = D3DCOMPILE_IEEE_STRICTNESS only  ->  the `[precise]` instruction tags
#   * one constant buffer `$Globals { float4 c[208]; }` (register(c0)): D3D9 constant register N is cb0[N]
#   * inputs: float4 per D3D9 `dcl` (POSITION BLENDINDICES BLENDWEIGHT NORMAL TANGENT BINORMAL TEXCOORDn COLORn)
#   * outputs: SV_POSITION, COLOR0, COLOR1, TEXCOORD0..7 (+ FOG0 / PSIZE0 as a 12th element when the Vista code writes oFog / oPts);
#     lanes the Vista code does not write are COLOR0/1 = 1,1,1,1 and TEXCOORDn = 0,0,0,1  (123/187/31..443 stock elements agree, 0 exceptions)
#   * the position is z-remapped:  o0 = p + (p' - p) * K,  p'.z = p.w * c10.w + c10.z   (K = 1 for 455 geometry shaders, 0 for the 10 screen-space ones)
#     MCC's remap is z_mcc = w - z_D3D9 (reverse Z, d_mcc = 1 - d_vista); for a standard projection that IS w * c10.w + c10.z.  A Vista shader that PINS the clip depth to
#     the far plane (oPos.z == oPos.w exactly: the two sky vertex shaders, vs_clip_z_equals_w) has z_mcc = w - w = 0, the reverse-Z far plane: fixup 'pin', p'.z = 0
#     (the closed form would give REAL depth, and MCC draws the sky late with the depth test on)
#   * stock `screen_1_stage` is reproduced BYTE FOR BYTE by the HLSL this module emits for it.
# ---------------------------------------------------------------------------------------------------------------------
VS_IEEE_FLAGS = 1 << 13        # D3DCOMPILE_IEEE_STRICTNESS (fxc /Gis)
VS_CONST_COUNT = 208
VS_DEFAULT_COLOR = (1.0, 1.0, 1.0, 1.0)
VS_DEFAULT_TEXCOORD = (0.0, 0.0, 0.0, 1.0)


def compile_vs(source, flags=None):
    return compile_hlsl(source, 'vs_5_0', 'main', {}, VS_IEEE_FLAGS if flags is None else flags, name='shader.vsh')


def vs_position_depends_on_constants(sh):
    """True when oPos depends (through data flow) on a non-literal constant register: world/view-projection shaders.  False for screen-space
    shaders that forward the position input (stock: convolution, lens_flare, screen_N_stage...)."""
    taint = {}                 # (reg type, num) -> set of lanes that depend on a constant
    defs = sh.defs()

    def src_taint(p, lanes):
        if p.rtype == R_CONST:
            return not (p.rel is None and p.num in defs)
        if p.rel is not None:
            return True
        if p.rtype == R_TEMP:
            t = taint.get((R_TEMP, p.num), set())
            return any(p.swz[l] in t for l in lanes)
        if p.rtype == R_ADDR:
            return True
        return False

    for ins in sh.ins:
        if ins.dst is None or ins.name in ('dcl', 'def', 'defi', 'defb'):
            continue
        lanes = [l for l in range(4) if ins.dst.mask >> l & 1]
        if ins.name in ('dp3', 'dp4', 'dp2add', 'rcp', 'rsq', 'exp', 'log', 'pow', 'expp', 'logp', 'nrm', 'sincos', 'dst', 'lit', 'crs',
                        'm3x2', 'm3x3', 'm3x4', 'm4x3', 'm4x4', 'mova'):
            use_lanes = [0, 1, 2, 3]
            dep = any(src_taint(s, use_lanes) for s in ins.srcs)
            per_lane = {l: dep for l in lanes}
        else:
            per_lane = {l: any(src_taint(s, [l]) for s in ins.srcs) for l in lanes}
        key = (ins.dst.rtype, ins.dst.num)
        cur = taint.setdefault(key, set())
        for l, d in per_lane.items():
            if d:
                cur.add(l)
            else:
                cur.discard(l)
    pos = taint.get((R_RASTOUT, 0), set())
    return bool(pos & {2, 3})


VS_PIN = 'pin'                  # VsResult.fixup of a vertex shader whose Vista clip z is pinned to clip w (the sky): MCC z = 0
VS_PIN_TRIALS = 32              # random constant / vertex sets per sample kind (plain + stress) that must ALL give z == w exactly


def vs_clip_z_equals_w(blob, n=VS_PIN_TRIALS, seed=33):
    """True when a Vista vs_2_0 shader (byte code or a parsed Shader) writes oPos.z == oPos.w EXACTLY for every one of n random constant / vertex sets (and n stress sets: zeros, +-1, 0.5 injected),
    i.e. the shader pins the clip depth to the far plane (D3D9 depth z/w = 1.0).  The two Vista sky vertex shaders do this (`add r0.zw, r1.x, c3.w ; mov oPos, r0`);
    no projection that carries a depth (a row 2 of the matrix that is not row 3) can.  Evaluated with the converter's own D3D9 interpreter, never from a name.  Only
    meaningful for a shader whose position depends on constants (vs_position_depends_on_constants): a screen-space shader that forwards its position input is not a pin."""
    try:
        m = D3D9Machine(blob)
        sems = set(m.input_semantics())
        sampler = SampleFn()
        for stress in (False, True):
            consts, inputs = random_trials(n, seed + (1 if stress else 0), sems, ncon=256, stress=stress)
            pos = m.run(consts, inputs, sampler).outputs[('POSITION', 0)]
            z, w = pos[:, 2], pos[:, 3]
            if not (np.all(np.isfinite(z)) and np.all(np.isfinite(w)) and np.array_equal(z, w)):
                return False
        return True
    except (D3D9Error, Unsupported, KeyError):
        return False


def vrtx_output_swizzle_dx11(vista_value):
    """vrtx 'Output Swizzles' (nibble per output register = lanes written): the MCC DX11 slot (+0x18) is the Vista value (+0xC) with every ZERO
    nibble 0..5 replaced by 0xF, because the wrapper defines all lanes of COLOR0/COLOR1/TEXCOORD0..3.  Holds for 93 of the 97 stock pairs
    (the 4 exceptions - effect, prt, water_pool, water_pool_static - are vertex shaders MCC edited); the MCC DX9 slot (+0x1C) is the Vista
    value unchanged (97/97) and the Xbox1 slot (+0x20) equals the DX11 slot."""
    v = vista_value & 0xFFFFFFFF
    for i in range(6):
        if (v >> (4 * i)) & 0xF == 0:
            v |= 0xF << (4 * i)
    return v


class VsResult(object):
    def __init__(self):
        self.source = ''
        self.dxbc = b''
        self.ok = False
        self.report = None
        self.notes = []
        self.fixup = None            # None (not decided) | False (no z remap: screen space) | True (z = w * c10.w + c10.z) | 'pin' (z = 0: Vista clip z == clip w, the sky)
        self.unsupported = None

    def summary(self):
        return '%s K=%s %s' % ('PROVEN' if self.ok else 'NOT PROVEN', self.fixup, '; '.join(self.notes[:2]))


def _vs_source(name, sh, xl, fixup, note=''):
    # every D3D9 `dcl` becomes a VS_IN member (the engine builds the input layout from the vertex declaration, so the struct lists all of them)
    decl = [(i.dst.num, usage_semantic(i.dcl_usage, i.dcl_index)) for i in sh.ins if i.name == 'dcl' and i.dst.rtype == R_INPUT]
    decl = sorted(set(decl))
    txt = ['// translated from Vista D3D9 vertex shader bytecode (vs_2_0) by h2conv.rev.d3d9_to_psh%s' % (' - ' + name if name else ''),
           '// MCC wrapper conventions: see h2conv/rev/d3d9_to_psh.py section 6; compile with vs_5_0 and D3DCOMPILE_IEEE_STRICTNESS' + (' (' + note + ')' if note else ''),
           'float4 c[%d] : register(c0);' % VS_CONST_COUNT,
           'struct VS_IN',
           '{']
    for num, (sn, si) in decl:
        txt.append('    float4 v%d : %s%s;' % (num, sn, si if si else ''))
    txt.append('};')
    txt.append('struct VS_OUT')
    txt.append('{')
    txt.append('    float4 pos : SV_POSITION;')
    txt.append('    float4 oD0 : COLOR0;')
    txt.append('    float4 oD1 : COLOR1;')
    for i in range(8):
        txt.append('    float4 oT%d : TEXCOORD%d;' % (i, i))
    written = xl.written_outputs
    if (R_RASTOUT, 1) in written:
        txt.append('    float oFog : FOG;')
    if (R_RASTOUT, 2) in written:
        txt.append('    float oPts : PSIZE;')
    txt.append('};')
    txt.append('VS_OUT main(VS_IN i)')
    txt.append('{')
    txt.append('    VS_OUT o;')
    names = ['r%d = 0' % k for k in sorted(xl.used_temps)] or ['r0 = 0']
    txt.append('    float4 %s;' % ', '.join(names))
    if xl.uses_a0:
        txt.append('    int4 a0 = 0;')
    txt.append('    float4 oPos = 0;')
    txt.append('    o.oD0 = float4(1.0, 1.0, 1.0, 1.0);')
    txt.append('    o.oD1 = float4(1.0, 1.0, 1.0, 1.0);')
    for i in range(8):
        txt.append('    o.oT%d = float4(0.0, 0.0, 0.0, 1.0);' % i)
    if (R_RASTOUT, 1) in written:
        txt.append('    o.oFog = 0;')
    if (R_RASTOUT, 2) in written:
        txt.append('    o.oPts = 0;')
    txt += xl.lines
    txt.append('    float4 fixed_p = oPos;')
    if fixup == VS_PIN:                                  # Vista clip z == clip w (far plane, forward Z): the reverse-Z image w - z = 0
        txt.append('    fixed_p.z = 0.0f;')
    else:
        txt.append('    fixed_p.z = oPos.w * c[10].w + c[10].z;')
    txt.append('    o.pos = oPos + (fixed_p - oPos) * %s;' % ('1.0f' if fixup else '0.0f'))
    txt.append('    return o;')
    txt.append('}')
    return '\n'.join(txt) + '\n'


def vs_expected(res, consts, fixup):
    """The MCC-wrapper model of a Vista vertex shader run: {semantic: (N,4)} with the wrapper defaults and the z remap applied (fixup True: w * c10.w + c10.z;
    fixup 'pin' (VS_PIN): z = 0, the reverse-Z image w - z of Vista's z == w; False: none)."""
    n = consts.shape[0]
    out = {}
    pos = res.outputs[('POSITION', 0)].copy()
    if fixup == VS_PIN:
        pos[:, 2] = 0.0
    elif fixup:
        pos[:, 2] = pos[:, 3] * consts[:, 10, 3] + consts[:, 10, 2]
    out[('POSITION', 0)] = pos
    for sem, dflt in ((('COLOR', 0), VS_DEFAULT_COLOR), (('COLOR', 1), VS_DEFAULT_COLOR)) + tuple(
            (('TEXCOORD', i), VS_DEFAULT_TEXCOORD) for i in range(8)):
        arr = np.tile(np.array(dflt), (n, 1))
        w = res.written.get(sem, 0)
        if sem in res.outputs:
            for lane in range(4):
                if w >> lane & 1:
                    arr[:, lane] = res.outputs[sem][:, lane]
        out[sem] = arr
    for sem in (('FOG', 0), ('PSIZE', 0)):
        if sem in res.outputs:
            out[sem] = res.outputs[sem]
    return out


def verify_vs(orig_blob, dxbc, fixup, n=40, seed=21, rtol=1e-5, atol=1e-6):
    """Original vs_2_0 (with the wrapper model) against the compiled vs_5_0: every lane of every output, random constants (256 registers),
    random vertex attributes."""
    rep = CompareReport()
    try:
        a = D3D9Machine(orig_blob)
        b = DxbcMachine(dxbc)
        sems = set(a.input_semantics()) | set(b.input_semantics())
        consts, inputs = random_trials(n, seed, sems, ncon=256)
        sampler = SampleFn()
        ra = a.run(consts, inputs, sampler)
        rb = b.run(consts, inputs, sampler)
        exp = vs_expected(ra, consts, fixup)
    except (Unsupported, CompileError) as e:
        rep.fail('evaluator: %s' % e)
        return rep
    if fixup == VS_PIN:                              # the premise of the pin, re-checked on the proof trials themselves: Vista's clip z IS its clip w (exactly)
        vp = ra.outputs[('POSITION', 0)]
        if not np.array_equal(vp[:, 2], vp[:, 3]):
            rep.fail('pin: the Vista clip z differs from the clip w in %d of %d trials' % (int((vp[:, 2] != vp[:, 3]).sum()), len(vp)))
    for sem, arr in sorted(exp.items()):
        if sem not in rb.outputs:
            if sem[0] in ('FOG', 'PSIZE'):
                rep.fail('output %s missing in the compiled shader' % (sem,))
            else:
                rep.fail('output %s missing in the compiled shader' % (sem,))
            continue
        got = rb.outputs[sem]
        lanes = range(4) if sem[0] not in ('FOG', 'PSIZE') else [0]
        for lane in lanes:
            x, y = arr[:, lane], got[:, lane]
            with np.errstate(all='ignore'):
                err = np.abs(x - y)
                lim = atol + rtol * np.maximum(np.abs(x), np.abs(y))
                bad = ~((err <= lim) | (x == y) | (~np.isfinite(x) & ~np.isfinite(y)))
            rep.compared_lanes += len(x)
            fin = err[np.isfinite(err)]
            if len(fin):
                rep.max_err = max(rep.max_err, float(fin.max()))
            if bad.any():
                rep.fail('output %s lane %s differs in %d of %d trials (max |diff| %.4g)' % (
                    sem, 'xyzw'[lane], int(bad.sum()), len(x), float(np.nanmax(err[bad])) if np.isfinite(err[bad]).any() else float('nan')))
    return rep


def translate_vs(blob, name='', fixup='auto', n=40, seed=21, verify=True):
    """vs_2_0 bytecode -> MCC style HLSL + DXBC, proven by evaluation (see the section header).  fixup: 'auto' (z remap iff oPos depends on a
    constant register; 'pin' instead of the closed-form remap when that position's clip z == clip w exactly, vs_clip_z_equals_w), True, False or 'pin'
    (VS_PIN).  VsResult.fixup is False | True | 'pin'."""
    res = VsResult()
    try:
        sh = parse(blob)
        if sh.kind != 'vs' or sh.major != 2:
            raise Unsupported('only vs_2_x is supported (got %s)' % sh.profile)
        xl = _Xlat(sh, clamp_colors=False)
        xl.run()
    except (D3D9Error, Unsupported) as e:
        res.unsupported = str(e)
        res.notes.append('unsupported: %s' % e)
        return res
    if fixup == 'auto':
        fixup = vs_position_depends_on_constants(sh)
        if fixup and vs_clip_z_equals_w(sh):
            fixup = VS_PIN
    res.fixup = VS_PIN if fixup == VS_PIN else bool(fixup)
    res.source = _vs_source(name, sh, xl, res.fixup)
    try:
        res.dxbc = compile_vs(res.source)
    except CompileError as e:
        res.notes.append('compile error: ' + str(e).strip().split('\n')[0][-200:] + ' | ' + str(e).strip().split('\n')[1][-200:] if '\n' in str(e).strip() else str(e)[:300])
        return res
    if verify:
        res.report = verify_vs(blob, res.dxbc, res.fixup, n=n, seed=seed)
        res.ok = res.report.ok
        res.notes += res.report.notes[:3]
    return res


# =====================================================================================================================
# 7. Equivalence harness: random trials -> two machines -> compare
# =====================================================================================================================
def random_trials(n, seed, semantics, ncon=64, stress=False):
    """Random but well-conditioned inputs: constants magnitude in [0.25, 2] with random sign, colours in [0,1], texcoords xyz in
    [-1.5,1.5] / w in [0.5,2], other vertex attributes in [-1,1].  stress=True: constants in [-3,3] with 0 / +-1 / 0.5 injected, colours
    in [-1,2] (the clamp must matter), texcoords in [-3,3] with w in [0.2,3]."""
    rng = np.random.RandomState(seed)
    if stress:
        consts = rng.uniform(-3.0, 3.0, (n, ncon, 4))
        pick = rng.rand(n, ncon, 4)
        consts = np.where(pick < 0.10, 0.0, consts)
        consts = np.where((pick >= 0.10) & (pick < 0.18), 1.0, consts)
        consts = np.where((pick >= 0.18) & (pick < 0.24), -1.0, consts)
        consts = np.where((pick >= 0.24) & (pick < 0.28), 0.5, consts)
    else:
        mag = rng.uniform(0.25, 2.0, (n, ncon, 4))
        sign = np.where(rng.rand(n, ncon, 4) < 0.35, -1.0, 1.0)
        consts = mag * sign
    inputs = {}
    for sem in sorted(semantics):
        nm = sem[0]
        if nm == 'COLOR':
            inputs[sem] = rng.uniform(-1.0, 2.0, (n, 4)) if stress else rng.uniform(0.0, 1.0, (n, 4))
        elif nm == 'TEXCOORD':
            v = rng.uniform(-3.0, 3.0, (n, 4)) if stress else rng.uniform(-1.5, 1.5, (n, 4))
            v[:, 3] = rng.uniform(0.2, 3.0, n) if stress else rng.uniform(0.5, 2.0, n)
            inputs[sem] = v
        elif nm == 'POSITION':
            v = rng.uniform(-1.5, 1.5, (n, 4))
            v[:, 3] = 1.0
            inputs[sem] = v
        else:
            inputs[sem] = rng.uniform(-1.0, 1.0, (n, 4))
    return consts, inputs


class CompareReport(object):
    def __init__(self):
        self.ok = True
        self.max_err = 0.0
        self.notes = []
        self.n = 0
        self.killed_equal = True
        self.compared_lanes = 0

    def fail(self, msg):
        self.ok = False
        if msg not in self.notes:
            self.notes.append(msg)

    def __repr__(self):
        return '<CompareReport ok=%s max_err=%.3g lanes=%d %s>' % (self.ok, self.max_err, self.compared_lanes, '; '.join(self.notes[:3]))


def compare_results(ra, rb, rtol=1e-6, atol=1e-7, require_same_written=True):
    rep = CompareReport()
    rep.n = len(ra.killed)
    if not np.array_equal(ra.killed, rb.killed):
        rep.killed_equal = False
        rep.fail('killed flags differ in %d of %d trials' % (int(np.sum(ra.killed != rb.killed)), rep.n))
    live = ~(ra.killed | rb.killed)
    sems = sorted(set(ra.outputs) | set(rb.outputs))
    for sem in sems:
        if sem not in ra.outputs or sem not in rb.outputs:
            rep.fail('output %s only on one side' % (sem,))
            continue
        wa, wb = ra.written.get(sem, 0), rb.written.get(sem, 0)
        if wa != wb and require_same_written:
            rep.fail('output %s written lanes differ (%X vs %X)' % (sem, wa, wb))
        mask = wa & wb
        for lane in range(4):
            if not mask >> lane & 1:
                continue
            a, b = ra.outputs[sem][live, lane], rb.outputs[sem][live, lane]
            if not len(a):
                continue
            with np.errstate(all='ignore'):
                err = np.abs(a - b)
                lim = atol + rtol * np.maximum(np.abs(a), np.abs(b))
                bad = ~((err <= lim) | (a == b) | (~np.isfinite(a) & ~np.isfinite(b)))      # inf / NaN on both sides = same undefined result
            rep.compared_lanes += int(live.sum())
            fin = err[np.isfinite(err)]
            if len(fin):
                rep.max_err = max(rep.max_err, float(fin.max()))
            if bad.any():
                rep.fail('output %s lane %s differs in %d of %d trials (max |diff| %.4g)' % (
                    sem, 'xyzw'[lane], int(bad.sum()), len(a), float(np.nanmax(err[bad])) if np.isfinite(err[bad]).any() else float('nan')))
    return rep


def apply_alpha_test(res, defines):
    """Emulate the fixed-function alpha test of a D3D9 render state on a machine Result: `defines` is the dict that
    alpha_test_from_render_states() returns ({'ALPHA_TEST': '0.501961', 'ALPHA_TEST_GREATER': 1}); the pixel is killed when the alpha written to
    COLOR0 fails the compare (float compare, like the MCC clip() code)."""
    if not defines:
        return res
    ref = float(defines['ALPHA_TEST'])
    a = res.outputs[('COLOR', 0)][:, 3]
    if defines.get('ALPHA_TEST_GREATER'):
        ok = a > ref
    elif defines.get('ALPHA_TEST_NOT_EQUAL'):
        ok = a != ref
    elif defines.get('ALPHA_TEST_LESS'):
        ok = a < ref
    else:
        raise Unsupported('alpha test function')
    res.killed = res.killed | ~ok
    return res


def compare_machines(ma, mb, n=48, seed=1, rtol=1e-6, atol=1e-7, sampler=None, ncon=64, require_same_written=True, stress=False,
                     alpha_a=None, alpha_b=None):
    """Run two machines on identical random trials and compare every output lane both sides write.  alpha_a / alpha_b: alpha-test define
    dicts emulated on that side's result (use alpha_a for a D3D9 machine whose pass has an alpha-test render state)."""
    sems = set(ma.input_semantics()) | set(mb.input_semantics())
    consts, inputs = random_trials(n, seed, sems, ncon, stress)
    sampler = sampler or SampleFn()
    ra = apply_alpha_test(ma.run(consts, inputs, sampler), alpha_a)
    rb = apply_alpha_test(mb.run(consts, inputs, sampler), alpha_b)
    return compare_results(ra, rb, rtol, atol, require_same_written)


# =====================================================================================================================
# 8. Tag helpers (decoded 'spas' / 'vrtx' nodes from h2conv.tagtree) and a small command line
# =====================================================================================================================
def _field_off(node, name):
    f = node.field(name)
    if f is None:
        raise KeyError('%s|%s has no field %r' % (node.group, node.path, name))
    return f.offset


def spas_pixel_shaders(root):
    """Decoded Vista (or MCC) 'spas' root Node -> list of dicts, one per Pixel Shader element:
    {'impl': i, 'ps': j, 'blobs': [normal, zprime, mrt] (bytes, b'' when absent), 'rs_active': [(D3DRS id, value)], 'alpha': {} | defines}.
    'alpha' is the ALPHA_TEST define set the MCC tool would compile this implementation with."""
    pp = root.blocks.get(_field_off(root, 'Postprocess Definition'), [])
    if not pp:
        return []
    p0 = pp[0]
    states = [(n.u8(0), n.u32(1)) for n in p0.blocks.get(_field_off(p0, 'Render States'), [])]
    out = []
    for i, im in enumerate(p0.blocks.get(_field_off(p0, 'Implementations'), [])):
        rs_start = im.u16(_field_off(im, 'Render States Block Index Data')) & 0xFF
        df_start = im.u16(_field_off(im, 'Default Render States Block Index Data')) & 0xFF
        active = states[rs_start:df_start] if df_start >= rs_start else states[rs_start:]
        try:
            alpha = alpha_test_from_render_states(active)
        except Unsupported:
            alpha = None
        for j, ps in enumerate(im.blocks.get(_field_off(im, 'Pixel Shader'), [])):
            blobs = [bytes(ps.datas[o]) for o in sorted(ps.datas)]
            out.append({'impl': i, 'ps': j, 'blobs': blobs, 'rs_active': active, 'alpha': alpha})
    return out


def vrtx_elements(root):
    """Decoded Vista 'vrtx' root Node -> list of compiled D3D9 vertex shader blobs, one per Geometry Classification element."""
    off = _field_off(root, 'Geometry Classifications')
    out = []
    for el in root.blocks.get(off, []):
        out.append(bytes(el.datas.get(_field_off(el, 'Compiled Shader'), b'')))
    return out


def translate_spas(root, name='', **kw):
    """Translate every pixel shader element of a decoded Vista 'spas' root Node: {(impl, ps): (PshResult, element dict)}."""
    res = OrderedDict()
    for el in spas_pixel_shaders(root):
        res[(el['impl'], el['ps'])] = (translate_ps(el['blobs'], name=name, **kw), el)
    return res


def _read_blob_file(path):
    return open(path, 'rb').read()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='d3d9_to_psh', description='D3D9 shader bytecode tools')
    sub = ap.add_subparsers(dest='cmd')
    a = sub.add_parser('disasm', help='disassemble a raw D3D9 shader blob (or a DXBC container) file')
    a.add_argument('file')
    a = sub.add_parser('selftest', help='quick self check (compiler binding, parser, evaluator)')
    args = ap.parse_args(argv)
    if args.cmd == 'disasm':
        data = _read_blob_file(args.file)
        if data[:4] == b'DXBC':
            print(d3d_disassemble(data))
        else:
            print(disassemble(data))
        return 0
    if args.cmd == 'selftest':
        src = 'float4 c0 : register(c0);\nfloat4 main(float4 p : SV_POSITION, float4 t : TEXCOORD0) : SV_Target { return c0 * t; }\n'
        dx = compile_hlsl(src, 'ps_5_0', 'main', {}, H2EK_FLAGS)
        m = DxbcMachine(dx)
        consts, inputs = random_trials(4, 1, m.input_semantics(), 8)
        r = m.run(consts, inputs, SampleFn())
        assert np.allclose(r.outputs[('COLOR', 0)], consts[:, 0] * inputs[('TEXCOORD', 0)]), 'DXBC evaluator mismatch'
        print('selftest ok (D3DCompiler_47 binding, DXBC parser and evaluator)')
        return 0
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
