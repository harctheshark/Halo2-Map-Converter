# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Two legacy Direct3D 9 rendering defects of Halo 2 Vista, fixed by default in the MCC -> Vista converter (vsfix, 2026-10-21).

1. ``transparent_generic_reflection`` VERTEX SHADER (``rasterizer\\vertex_shaders_dx9\\transparent_generic_reflection``)
   Every vs_2_0 class of the stock Vista tag computes a fade/cull factor (COLOR1, ``oD1``) from the vertex position expressed in three
   normalised axes (rows c13, c14, c12): per axis ``u = min(max(1 - p, 0)^2, 1)``, then the distance term is the SUM of the axis terms.  The stock
   program adds only TWO of them::

        min r0.xyz, r0, c4.w
        add r0.x, r0.y, r0.x          <- x + y        (the z axis never takes part in the test)
        min r0.w, r0.x, c4.w

   so the test is applied on two axes and the third is ignored (the transparency of such surfaces dies / pops when the
   camera crosses cluster portals).  The fix, taken from a corrected editing-kit tag of this vertex shader, replaces that ONE instruction in the DX9
   blobs by ``dp3 r0.x, r0, c4.w`` (= x + y + z, c4.w is the engine constant 1.0 the program already uses as its 'one').  Same instruction length
   (4 dwords): the blob keeps its size and every other token.  The converter applies the SAME token edit to the stock blobs of the Vista donor
   (`patch_blob`): outside those four dwords the program is byte-identical to what Vista ships.

   NOTE (measured): in the corrected kit tag the HLSL TEXT is not the fix - the fix lives in the compiled DX9 blobs only (the text of
   all three groups still reads ``r8.xxxx + r8.yyyy``, the pristine program).  The converter therefore edits blobs, and proves the edit against the
   corrected compiled blobs (byte-equal on the kit's pristine blobs) and against an independent D3DCompiler_47 build of the three-axis text.

2. NVIDIA: the ``alpha_white`` / ``gray_50_percent`` PLACEHOLDER bitmaps of the alpha-test shader templates
   Vista's pass ``lightmap_tex_detail_alpha_test`` (templates ``tex_bump_alpha_test_single_pass`` / ``tex_bump_alpha_test``) samples a fifth texture
   (stage 4, ``texldp r1, t4, s4``; the alpha-test multiplier) from a 4x4 DXT1 placeholder bitmap.  MCC's DX11 version of the pass has NO fifth
   texture (Textures 16 vs 20); Project Cartographer ships a load-time workaround for exactly these two templates (tag_fixes.cpp
   `shader_templates_fix_nvidia`, xlive.dll sub_10005030): the placeholder bitmap reference is set to NONE (the engine then binds
   ``rasterizer\\default_2d``, a 4x4 X8R8G8B8 texture whose alpha is 1, halo2.exe sub_65F600).  Its tag lookup is by name, first match, so it silently skips
   shaders that reference another copy of the bitmap (audit N3).  The converter applies the SAME neutralisation to the converted tags
   (`foliage_fix`): in the written map those slots are NONE, whatever the runtime does.

Public: ``patch_blob``, ``find_sum_instruction``, ``patch_vrtx_root``, ``NVIDIA_PLACEHOLDERS``, ``REFLECTION_VRTX``.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:                                   # noqa: BLE001
    pass

import struct

NULL = 0xFFFFFFFF

# ---------------------------------------------------------------------------------------------------------------------------------------------
# 1. transparent_generic_reflection vertex shader
# ---------------------------------------------------------------------------------------------------------------------------------------------
REFLECTION_VRTX = 'rasterizer\\vertex_shaders_dx9\\transparent_generic_reflection'
OP_ADD, OP_DP3, OP_MIN = 0x02, 0x08, 0x0A
R_TEMP, R_CONST = 0, 2
SWZ_IDENT, SWZ_X, SWZ_Y, SWZ_W = 0xE4, 0x00, 0x55, 0xFF
SRC_C4_WWWW = 0xA0000000 | (SWZ_W << 16) | 4          # source token: constant register c4, swizzle .wwww, no modifier


def _u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def _reg(tok):
    """(register type, number) of a vs_2_0 register token"""
    return (((tok >> 28) & 7) | ((tok >> 8) & 0x18)), tok & 0x7FF


def _walk(blob):
    """[(token offset, opcode, number of operand tokens)] of the instructions of a vs_2_x blob (comment blocks skipped, END excluded)."""
    if len(blob) < 8 or _u32(blob, 0) != 0xFFFE0200:
        raise ValueError('not a vs_2_0 blob')
    out = []
    o = 4
    while o + 4 <= len(blob):
        t = _u32(blob, o)
        if t == 0x0000FFFF:
            return out
        if (t & 0xFFFF) == 0xFFFE:                           # comment (constant table): size in dwords in bits 16..30
            o += 4 + 4 * ((t >> 16) & 0x7FFF)
            continue
        n = (t >> 24) & 0xF
        out.append((o, t & 0xFFFF, n))
        o += 4 + 4 * n
    raise ValueError('vs_2_0 blob without END token')


def _src_is(tok, rtype, num, swz, mod=0):
    return (not tok >> 13 & 1) and _reg(tok) == (rtype, num) and ((tok >> 16) & 0xFF) == swz and ((tok >> 24) & 0xF) == mod


def find_sum_instruction(blob):
    """Token offset of THE distance-sum instruction of a stock transparent_generic_reflection class blob, or None.  The stock sequence is::

        min rK.xyz, rK, c4.w          (3 operands: dst temp K mask xyz, src rK, src c4.wwww)
        add rD.m,   rK.y, rK.x        (the x + y sum; m = one lane)
        min rK.w,   rD.m(replicated), c4.w

    Exactly one such triple must exist in the blob (else None; a blob that already has `dp3` there is not 'stock')."""
    ins = _walk(blob)
    hits = []
    for i in range(len(ins) - 2):
        (o1, op1, n1), (o2, op2, n2), (o3, op3, n3) = ins[i], ins[i + 1], ins[i + 2]
        if not (op1 == OP_MIN and op2 == OP_ADD and op3 == OP_MIN and n1 == n2 == n3 == 3):
            continue
        d1, a1, b1 = (_u32(blob, o1 + 4 + 4 * k) for k in range(3))
        d2, a2, b2 = (_u32(blob, o2 + 4 + 4 * k) for k in range(3))
        d3, a3, b3 = (_u32(blob, o3 + 4 + 4 * k) for k in range(3))
        rt, k = _reg(d1)
        if rt != R_TEMP or ((d1 >> 16) & 0xF) != 0x7 or not _src_is(a1, R_TEMP, k, SWZ_IDENT) or b1 != SRC_C4_WWWW:
            continue
        dt, dnum = _reg(d2)
        mask2 = (d2 >> 16) & 0xF
        if dt != R_TEMP or mask2 not in (1, 2, 4, 8) or (d2 >> 20) & 0xF:
            continue
        if not _src_is(a2, R_TEMP, k, SWZ_Y) or not _src_is(b2, R_TEMP, k, SWZ_X):
            continue
        lane = {1: 0, 2: 1, 4: 2, 8: 3}[mask2]
        if _reg(d3) != (R_TEMP, k) or ((d3 >> 16) & 0xF) != 0x8 or not _src_is(a3, R_TEMP, dnum, lane * 0x55) or b3 != SRC_C4_WWWW:
            continue
        hits.append(o2)
    return hits[0] if len(hits) == 1 else None


def patch_blob(blob):
    """Stock class blob -> fixed class blob (`add rD.m, rK.y, rK.x` -> `dp3 rD.m, rK, c4.w`), or None when the blob is not the stock program
    (no / several matches).  The result has the same length; only the 3 changed dwords (opcode, src0 swizzle, src1) differ."""
    blob = bytes(blob)
    o = find_sum_instruction(blob)
    if o is None:
        return None
    b = bytearray(blob)
    struct.pack_into('<I', b, o, 0x03000000 | OP_DP3)                                  # dp3 (opcode, length 3)
    a0 = _u32(blob, o + 8)
    struct.pack_into('<I', b, o + 8, (a0 & ~0x00FF0000) | (SWZ_IDENT << 16))           # src0: rK, identity swizzle (xyz are read by dp3)
    struct.pack_into('<I', b, o + 12, SRC_C4_WWWW)                                     # src1: c4.wwww
    return bytes(b)


def is_fixed_blob(blob):
    """True when the blob has the fixed instruction sequence (`dp3 rD.m, rK, c4.w` where the stock program has the add)."""
    ins = _walk(bytes(blob))
    for i in range(len(ins) - 2):
        (o1, op1, n1), (o2, op2, n2), (o3, op3, n3) = ins[i], ins[i + 1], ins[i + 2]
        if not (op1 == OP_MIN and op2 == OP_DP3 and op3 == OP_MIN and n1 == n2 == n3 == 3):
            continue
        d1, a1, b1 = (_u32(blob, o1 + 4 + 4 * k) for k in range(3))
        d2, a2, b2 = (_u32(blob, o2 + 4 + 4 * k) for k in range(3))
        rt, k = _reg(d1)
        if rt == R_TEMP and ((d1 >> 16) & 0xF) == 7 and b1 == SRC_C4_WWWW and _src_is(a2, R_TEMP, k, SWZ_IDENT) and b2 == SRC_C4_WWWW:
            return True
    return False


# Vista vrtx layout: root +0x4 'Geometry Classifications' block, element: +0x4 'Compiled Shader' dataref (the vs_2_0 blob), +0xC 'Code' dataref
OFF_CLASSES = 0x4
OFF_COMPILED = 0x4


def patch_vrtx_root(root):
    """Patch every class blob of a decoded Vista vrtx Node (tagtree) in place.  -> (patched, already_fixed, other, details): counts of
    non-empty class blobs that were stock and are now fixed / were fixed already / are neither (left untouched)."""
    patched = fixed = other = 0
    det = []
    for ci, el in enumerate(root.blocks.get(OFF_CLASSES, [])):
        blob = bytes(el.datas.get(OFF_COMPILED, b''))
        if not blob:
            det.append((ci, 'empty'))
            continue
        try:
            nb = patch_blob(blob)
            if nb is None:
                if is_fixed_blob(blob):
                    fixed += 1
                    det.append((ci, 'already fixed'))
                else:
                    other += 1
                    det.append((ci, 'not the stock program: left untouched'))
                continue
        except ValueError:
            other += 1
            det.append((ci, 'not a vs_2_0 blob: left untouched'))
            continue
        el.datas[OFF_COMPILED] = bytearray(nb) if isinstance(el.datas.get(OFF_COMPILED), bytearray) else nb
        patched += 1
        det.append((ci, 'patched'))
    return patched, fixed, other, det


# ---------------------------------------------------------------------------------------------------------------------------------------------
# 2. NVIDIA placeholder bitmaps
# ---------------------------------------------------------------------------------------------------------------------------------------------
# (shader template, Bitmaps[] slot, placeholder bitmap) - EXACTLY the two entries of Project Cartographer's shader_templates_fix_nvidia()
NVIDIA_PLACEHOLDERS = (
    ('shaders\\shader_templates\\opaque\\tex_bump_alpha_test_single_pass', 4, 'shaders\\default_bitmaps\\bitmaps\\alpha_white'),
    ('shaders\\shader_templates\\opaque\\tex_bump_alpha_test', 1, 'shaders\\default_bitmaps\\bitmaps\\gray_50_percent'),
)
NVIDIA_TABLE = {t.lower(): (slot, bm.lower()) for t, slot, bm in NVIDIA_PLACEHOLDERS}
