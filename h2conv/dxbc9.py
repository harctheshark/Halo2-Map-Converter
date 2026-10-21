# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""DXBC (ps_5_0, as D3DCompile writes it for the H2EK ``.psh`` shaders) -> HLSL for Direct3D 9 (ps_2_0 / ps_3_0).

Why this exists.  The Halo-CE port maps (workshop mod 3583572796) ship shader code ONLY as DXBC.  For almost every pass the H2EK ``.psh`` source is
available and the DXBC is reproduced from it byte for byte, so the D3D9 code is compiled from that source (``ce_port_build``).  A handful of passes
have no source on this machine (``h1\\shaders\\passes\\scopeblur``): their D3D9 code is recovered from the DXBC by this module - a statement-by-statement
translation of the shader the MCC tool compiled, never a re-implementation - and then PROVEN against the DXBC with the converter's two evaluators
(``rev.d3d9_to_psh.D3D9Machine`` / ``DxbcMachine``) before it is used.

Scope (what D3DCompile emits for these pixel shaders; anything else raises ``Unsupported`` - nothing is approximated):
  * float arithmetic  mov add mul mad div dp2 dp3 dp4 min max sqrt rsq exp log frc round_ni round_pi round_z round_ne, _sat, source neg / abs
  * movc, lt ge eq ne (as 0 / 1 masks only when consumed by movc)
  * sample / sample_l / sample_b on texture2d / texturecube / texture3d  (the resource swizzle is applied to the destination lanes)
  * ONE structured idiom for integer loops: ``mov rK.c, 0 ; loop ; ige t, rK.c, N ; breakc_nz t ; ... itof f, rK.c ... ; iadd rK.c, rK.c, 1 ; endloop`` (what fxc
    writes for ``for (int i = 0; i < N; i++)``), emitted as ``[unroll] for``
  * discard / clip is not needed here (alpha test is a render state in Direct3D 9)
Inputs are the interpolants of the H2EK pixel shader convention (COLOR0/1, TEXCOORD0..7 -> ``color0/1`` / ``tc0..7``), constants ``cb0[k]`` -> ``c<k>`` with
``register(c<k>)``, samplers ``t<n>`` -> ``s<n>``.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:                                                     # noqa: BLE001  pragma: no cover
    pass

import struct

from .rev import d3d9_to_psh as D

Unsupported = D.Unsupported


def _lit(bits):
    """immediate dword -> HLSL float literal that round-trips the float32 bit pattern exactly"""
    f = struct.unpack('<f', struct.pack('<I', bits & 0xFFFFFFFF))[0]
    if f != f or f in (float('inf'), float('-inf')):
        raise Unsupported('non-finite immediate 0x%08X' % bits)
    s = '%.9g' % f
    if 'e' not in s and '.' not in s:
        s += '.0'
    return s


def _is_int_imm(bits):
    bits &= 0xFFFFFFFF
    return bits != 0 and ((bits >> 23) & 0xFF) == 0


class _Tr(object):
    LANES = 'xyzw'

    def __init__(self, dx):
        self.dx = dx
        self.out = []
        self.ind = '    '
        self.sem_of_reg = {}
        for (nm, sidx, sysv, reg, mask) in dx.isgn:
            if nm == 'SV_POSITION':
                continue
            self.sem_of_reg[reg] = ('color%d' % sidx) if nm == 'COLOR' else ('tc%d' % sidx) if nm == 'TEXCOORD' else None
        self.consts = set()
        self.samplers = {}
        self.tex_kind = {}
        for ins in dx.ins:
            if ins.name == 'dcl_resource':
                self.tex_kind[ins.ops[0].idx[0][0]] = D.DXBC_RES_DIMS.get((ins.ctrl >> 11) & 0x1F, '?')
        self.temps = int(next((i.extra for i in dx.ins if i.name == 'dcl_temps'), 0) or 0)
        self.outs = sorted(set(reg for (nm, sidx, sysv, reg, mask) in dx.osgn if nm == 'SV_TARGET'))
        self.loops = []                   # stack of (reg, lane) counters
        self.loop_vars = {}               # (reg, lane) -> HLSL int name

    # ---- operands --------------------------------------------------------------------------------------------------------
    def _swz(self, o, lanes):
        """swizzle letters of operand o for the destination lanes"""
        if o.ncomp == 2:
            return ''.join(self.LANES[o.swz[l]] for l in lanes)
        return ''.join(self.LANES[l] for l in lanes)

    def src(self, o, lanes, as_int=False):
        t = o.otype
        n = len(lanes)
        if t == D.DO_IMM32:
            if o.ncomp == 1 or len(o.imm) == 1:
                vals = [o.imm[0]] * n
            else:
                vals = [o.imm[o.swz[l]] if o.ncomp == 2 else o.imm[l] for l in lanes]
            if as_int:
                return str(struct.unpack('<i', struct.pack('<I', vals[0] & 0xFFFFFFFF))[0])
            parts = [_lit(v) for v in vals]
            e = parts[0] if n == 1 else 'float%d(%s)' % (n, ', '.join(parts))
        else:
            sw = self._swz(o, lanes)
            if t == D.DO_TEMP:
                key = (o.idx[0][0], None)
                e = 'r%d.%s' % (o.idx[0][0], sw)
            elif t == D.DO_INPUT:
                nm = self.sem_of_reg.get(o.idx[0][0])
                if nm is None:
                    raise Unsupported('input v%d' % o.idx[0][0])
                e = '%s.%s' % (nm, sw)
            elif t == D.DO_CBUF:
                if o.idx[0][0] != 0 or o.idx[1][1] is not None:
                    raise Unsupported('constant buffer form')
                k = o.idx[1][0]
                self.consts.add(k)
                e = 'c%d.%s' % (k, sw)
            else:
                raise Unsupported('operand type %d' % t)
        if o.mod & 2:
            e = 'abs(%s)' % e
        if o.mod & 1:
            e = '-' + e if not e.startswith('-') else '(%s)' % ('-' + e)
        return e

    def dst(self, o):
        t = o.otype
        lanes = [l for l in range(4) if o.mask >> l & 1]
        if t == D.DO_TEMP:
            return 'r%d' % o.idx[0][0], lanes
        if t == D.DO_OUTPUT:
            return 'o%d' % o.idx[0][0], lanes
        raise Unsupported('destination operand type %d' % t)

    # ---- statement emission -----------------------------------------------------------------------------------------------
    def emit(self, s):
        self.out.append(self.ind * (1 + len(self.loops)) + s)

    def assign(self, dname, lanes, expr, sat):
        n = len(lanes)
        if sat:
            expr = 'saturate(%s)' % expr
        self.emit('%s.%s = %s;' % (dname, ''.join(self.LANES[l] for l in lanes), expr))

    def run(self):
        ins_list = [i for i in self.dx.ins if not i.name.startswith('dcl_') and i.name not in ('customdata', 'nop')]
        pc = 0
        while pc < len(ins_list):
            ins = ins_list[pc]
            nm = ins.name
            nd = D.DXBC_N_DST.get(nm, 1)
            dst, src = ins.ops[:nd], ins.ops[nd:]
            if nm == 'ret':
                break
            if nm == 'mov' and not self.loops and pc + 1 < len(ins_list) and ins_list[pc + 1].name == 'loop' and dst[0].otype == D.DO_TEMP \
                    and src[0].otype == D.DO_IMM32:
                # loop idiom: counter initialised just before the loop
                pc = self._loop(ins_list, pc)
                continue
            if nm == 'loop':
                raise Unsupported('loop without the counter initialisation idiom')
            self._ins(ins, nm, dst, src)
            pc += 1

    def _loop(self, ins_list, pc):
        init = ins_list[pc]
        d, lanes = self.dst(init.ops[0])
        if len(lanes) != 1 or init.ops[1].imm[0] != 0:
            raise Unsupported('loop counter init')
        reg, lane = init.ops[0].idx[0][0], lanes[0]
        ge, br = ins_list[pc + 2], ins_list[pc + 3]
        if not (ge.name == 'ige' and br.name == 'breakc' and br.nz):
            raise Unsupported('loop header is not ige / breakc_nz')
        tdst = ge.ops[0]
        if not (ge.ops[1].otype == D.DO_TEMP and ge.ops[1].idx[0][0] == reg and len(self.dst(ge.ops[0])[1]) == 1):
            raise Unsupported('loop compare')
        if ge.ops[2].otype != D.DO_IMM32 or not _is_int_imm(ge.ops[2].imm[0]):
            raise Unsupported('loop bound')
        bound = ge.ops[2].imm[0]
        if not (br.ops[0].otype == D.DO_TEMP and br.ops[0].idx[0][0] == tdst.idx[0][0]):
            raise Unsupported('loop break condition')
        var = 'i%d_%s' % (reg, self.LANES[lane])
        self.emit('[unroll] for (int %s = 0; %s < %d; %s++) {' % (var, var, bound, var))
        self.loops.append((reg, lane))
        self.loop_vars[(reg, lane)] = var
        k = pc + 4
        while True:
            ins = ins_list[k]
            nm = ins.name
            if nm == 'iadd':
                dd = ins.ops[0]
                if dd.otype == D.DO_TEMP and dd.idx[0][0] == reg and dd.mask == 1 << lane and ins.ops[1].otype == D.DO_TEMP \
                        and ins.ops[1].idx[0][0] == reg and ins.ops[2].otype == D.DO_IMM32 and ins.ops[2].imm[0] == 1:
                    k += 1
                    if ins_list[k].name != 'endloop':
                        raise Unsupported('statements after the counter increment')
                    break
            if nm in ('loop', 'endloop', 'ret'):
                raise Unsupported('nested loop / early end inside the loop idiom')
            nd = D.DXBC_N_DST.get(nm, 1)
            self._ins(ins, nm, ins.ops[:nd], ins.ops[nd:])
            k += 1
        self.loops.pop()
        del self.loop_vars[(reg, lane)]
        self.out.append(self.ind * (1 + len(self.loops)) + '}')
        return k + 1

    def _ins(self, ins, nm, dst, src):
        sat = ins.sat
        if nm == 'itof':
            d, lanes = self.dst(dst[0])
            o = src[0]
            if len(lanes) != 1 or o.otype != D.DO_TEMP:
                raise Unsupported('itof form')
            key = (o.idx[0][0], o.swz[lanes[0]] if o.ncomp == 2 else lanes[0])
            if key not in self.loop_vars:
                raise Unsupported('itof of something other than a loop counter')
            self.assign(d, lanes, '(float)%s' % self.loop_vars[key], sat)
            return
        d, lanes = self.dst(dst[0])
        n = len(lanes)
        S = lambda i: self.src(src[i], lanes)
        if nm == 'mov':
            e = S(0)
        elif nm == 'add':
            e = '%s + %s' % (S(0), S(1))
        elif nm == 'mul':
            e = '%s * %s' % (S(0), S(1))
        elif nm == 'mad':
            e = '%s * %s + %s' % (S(0), S(1), S(2))
        elif nm == 'div':
            e = '%s / %s' % (S(0), S(1))
        elif nm in ('dp2', 'dp3', 'dp4'):
            k = int(nm[2])
            full = list(range(k))
            e = 'dot(%s, %s)' % (self.src(src[0], full), self.src(src[1], full))
        elif nm == 'min':
            e = 'min(%s, %s)' % (S(0), S(1))
        elif nm == 'max':
            e = 'max(%s, %s)' % (S(0), S(1))
        elif nm == 'sqrt':
            e = 'sqrt(%s)' % S(0)
        elif nm == 'rsq':
            e = 'rsqrt(%s)' % S(0)
        elif nm == 'exp':
            e = 'exp2(%s)' % S(0)
        elif nm == 'log':
            e = 'log2(%s)' % S(0)
        elif nm == 'frc':
            e = 'frac(%s)' % S(0)
        elif nm == 'round_ni':
            e = 'floor(%s)' % S(0)
        elif nm == 'round_pi':
            e = 'ceil(%s)' % S(0)
        elif nm == 'round_z':
            e = 'trunc(%s)' % S(0)
        elif nm == 'round_ne':
            e = 'round(%s)' % S(0)
        elif nm in ('sample', 'sample_l', 'sample_b'):
            tex, smp = src[1], src[2]
            slot = tex.idx[0][0]
            kind = self.tex_kind.get(slot, '2d')
            self.samplers[slot] = kind
            if kind == '2d':
                cl, fn = [0, 1], 'tex2D'
            elif kind == 'cube':
                cl, fn = [0, 1, 2], 'texCUBE'
            elif kind == '3d':
                cl, fn = [0, 1, 2], 'tex3D'
            else:
                raise Unsupported('texture kind %s' % kind)
            coord = self.src(src[0], cl)
            if nm == 'sample':
                call = '%s(s%d, %s)' % (fn, slot, coord)
            elif nm == 'sample_l' and kind == '2d':
                call = 'tex2Dlod(s%d, float4(%s, 0, %s))' % (slot, coord, self.src(src[3], [0]))
            elif nm == 'sample_b' and kind == '2d':
                call = 'tex2Dbias(s%d, float4(%s, 0, %s))' % (slot, coord, self.src(src[3], [0]))
            else:
                raise Unsupported('%s on %s' % (nm, kind))
            sw = ''.join(self.LANES[tex.swz[l]] for l in lanes)
            e = '%s.%s' % (call, sw)
        else:
            raise Unsupported('DXBC opcode %s' % nm)
        self.assign(d, lanes, e, sat)


def dxbc_to_hlsl(dxbc, variant_mrt=None):
    """DXBC pixel shader -> HLSL text for Direct3D 9 (entry ``main``).  The text is meant for ``D3DCompile`` with ps_2_0 / ps_3_0."""
    dx = D.parse_dxbc(dxbc)
    if dx.kind != 'ps':
        raise Unsupported('not a pixel shader')
    tr = _Tr(dx)
    tr.run()
    mrt = len(tr.outs) > 1 if variant_mrt is None else variant_mrt
    lines = []
    for slot in sorted(tr.samplers):
        lines.append({'2d': 'sampler2D', 'cube': 'samplerCUBE', '3d': 'sampler3D'}[tr.samplers[slot]] + ' s%d : register(s%d);' % (slot, slot))
    for k in sorted(tr.consts):
        lines.append('float4 c%d : register(c%d);' % (k, k))
    if mrt:
        lines += ['struct MRT_return', '{', '    float4 color : COLOR0;', '    float4 depth : COLOR1;', '};']
        lines.append('MRT_return main(')
    else:
        lines.append('float4 main(')
    used_sem = set(tr.sem_of_reg.values()) - {None}
    params = ['    float4 color0 : COLOR0', '    float4 color1 : COLOR1'] + ['    float4 tc%d : TEXCOORD%d' % (i, i) for i in range(8)]
    lines.append(',\n'.join(params))
    lines.append(')' + ('' if mrt else ' : COLOR'))
    lines.append('{')
    for r in range(tr.temps):
        lines.append('    float4 r%d = float4(0, 0, 0, 0);' % r)
    for o in tr.outs:
        lines.append('    float4 o%d = float4(0, 0, 0, 0);' % o)
    lines += tr.out
    if mrt:
        lines += ['    MRT_return retval;', '    retval.color = o0;', '    retval.depth = o1;', '    return retval;']
    else:
        lines.append('    return o0;')
    lines.append('}')
    return '\n'.join(lines) + '\n'
