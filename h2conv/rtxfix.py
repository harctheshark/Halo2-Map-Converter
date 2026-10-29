# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""ON by default (``--no-rtx-foliage-fix`` turns it off): the alpha-tested foliage of Halo 2 Vista on NVIDIA RTX (Turing and newer) cards (rtxfix, 2026-10-21).

THE DEFECT (reproduced on an RTX 4090 with the real Vista pixel shaders through the real Direct3D 9 driver).
  Vista's pass ``shaders\\shader_passes\\lightmap\\lightmap_tex_detail_alpha_test`` (and ``..._clamped``) multiplies the bump map's alpha by a second alpha mask,
  texture stage 4 (``alpha_white``, a 4x4 DXT1 bitmap; the placeholder of the templates tex_bump_alpha_test*).  In implementations 0, 2 and 3 that stage is a 2D sampler
  (``tex2Dproj``).  In implementation 1 - the PER-VERTEX lightmap implementation, used by every object lit through vertex colours: palm trees, bushes, scenery - the original
  tool source declares it ``samplerCUBE s4`` and reads ``texCUBE(s4, tc4)`` (the pass source ``..._lightmap_tex_detail_alpha_test_1.psh``,
  ``#texture 4 mode=2``: copied from the environment-map pass, whose stage 4 really is a cube), while the template binds the 2D alpha_white bitmap there.
  A 2D texture read through a cube sampler is undefined in Direct3D 9.  Old GPUs happened to return the texel; RTX / GTX 16 return (0, 0, 0, 0) for ANY 2D format:
  alpha = bump.a * 0 = 0 for every pixel, so the alpha test discards the whole object.  (GTX 10 cards returned alpha 1 for a texture without an alpha channel,
  which is why Project Cartographer's NONE workaround - the X8R8G8B8 rasterizer\\default_2d bitmap - helped there and not on RTX.)  Measured on the RTX 4090, alpha-test coverage of a half
  transparent test quad, implementation 1: 2D DXT1 placeholder 0.000, X8R8G8B8 default_2d 0.000, A8R8G8B8 white 0.000, CUBE white 0.500; implementations 0 and 2 with the same bindings 0.500.
  MCC's DX11 version of the pass has no fifth texture at all, which is why MCC renders it.

THE FIX (Vista's own byte code, nothing is ported).  Every pixel shader blob of the affected passes that declares ``s4`` as a cube sampler gets the declaration of the
bound bitmap's type: ``dcl_cube s4`` -> ``dcl_2d s4``, ONE dword (the plain ``texld rK, t4, s4`` then reads the xy of the coordinate, which the pixel shader already declares - the cube
read used xyz).  All three variants (ps_2_0, ps_2_0 z-prime, ps_3_0 MRT) of every element.  Nothing else changes: the same 4x4 DXT1 white bitmap is read, so on a GPU
that handled the old declaration the result is identical, and on RTX the stage now returns the white texel.  The passes are Vista's shared.map tags (a linked tag cannot change), so with
the fix on the planner does NOT link them nor the templates that use them: local donor copies under their own names carry the edit (like the vsfix reflection chain).  Default ON; confirmed
in game on an RTX card.

Public: ``patch_ps_blob``, ``patch_spas_root``, ``RTX_PASSES``, ``affected_pass``.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:                                   # noqa: BLE001  pragma: no cover
    pass

import struct

RTX_PASSES = ('shaders\\shader_passes\\lightmap\\lightmap_tex_detail_alpha_test', 'shaders\\shader_passes\\lightmap\\lightmap_tex_detail_alpha_test_clamped')
_PASS_SET = {p.lower() for p in RTX_PASSES}

OP_DCL, OP_TEXLD = 0x1F, 0x42
R_SAMPLER = 10
TEX_2D, TEX_CUBE = 2, 3                 # D3DSAMPLER_TEXTURE_TYPE values (dcl usage token bits 27..30)
TEXLD_PROJECT = 1 << 16                 # texld control: texldp


def affected_pass(name):
    """True for the Vista passes this fix edits."""
    return (name or '').lower() in _PASS_SET


def _u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def _reg(tok):
    return (((tok >> 28) & 7) | ((tok >> 8) & 0x18)), tok & 0x7FF


def patch_ps_blob(blob):
    """Pixel shader blob whose stage-4 sampler is declared as cube -> the same blob with the 2D declaration, or None when stage 4 is not a cube sampler.
    Edits exactly one dword (the dcl usage token); the blob keeps its length.  ValueError for anything unexpected (a cube s4 that is not read by exactly
    one texld, a malformed stream): nothing is guessed."""
    blob = bytearray(bytes(blob))
    if len(blob) < 8 or len(blob) % 4:
        raise ValueError('not a pixel shader blob')
    ver = _u32(blob, 0)
    if (ver >> 16) != 0xFFFF or ((ver >> 8) & 0xFF) not in (2, 3):
        raise ValueError('not a ps_2_x / ps_3_0 blob')
    o = 4
    dcl_at = None
    reads = []
    while o + 4 <= len(blob):
        t = _u32(blob, o)
        if t == 0x0000FFFF:
            break
        op = t & 0xFFFF
        if op == 0xFFFE:                                     # comment block (constant table): skip
            o += 4 * (1 + ((t >> 16) & 0x7FFF))
            continue
        n = (t >> 24) & 0xF
        if op == OP_DCL and n == 2 and _reg(_u32(blob, o + 8)) == (R_SAMPLER, 4):
            ut = _u32(blob, o + 4)
            if ((ut >> 27) & 0xF) == TEX_CUBE:
                dcl_at = o + 4
        elif op == OP_TEXLD and n == 3 and _reg(_u32(blob, o + 12)) == (R_SAMPLER, 4):
            reads.append(o)
        o += 4 + 4 * n
    else:
        raise ValueError('blob without END token')
    if dcl_at is None:
        return None
    if len(reads) != 1:
        raise ValueError('a cube s4 read by %d texld instructions' % len(reads))
    ut = _u32(blob, dcl_at)
    struct.pack_into('<I', blob, dcl_at, (ut & ~(0xF << 27)) | (TEX_2D << 27))
    t = _u32(blob, reads[0])
    if t & (3 << 16):
        raise ValueError('the stage-4 texld is not a plain texld (control 0x%X)' % ((t >> 16) & 0xFF))
    return bytes(blob)


def patch_spas_root(root):
    """Patch every pixel shader blob of a decoded Vista ``spas`` Node (in place).  -> dict(patched=, unchanged=); the Node is untouched when a blob raises."""
    pps = root.block('Postprocess Definition')
    if len(pps) != 1:
        raise ValueError('Postprocess Definition count %d' % len(pps))
    edits = []
    unchanged = 0
    for im in pps[0].block('Implementations'):
        for ps in im.block('Pixel Shader'):
            for off in sorted(ps.datas):
                b = bytes(ps.datas[off])
                if not b:
                    continue
                nb = patch_ps_blob(b)
                if nb is None:
                    unchanged += 1
                else:
                    edits.append((ps, off, nb))
    for ps, off, nb in edits:
        ps.datas[off] = bytearray(nb) if isinstance(ps.datas.get(off), bytearray) else nb
    return {'patched': len(edits), 'unchanged': unchanged}
