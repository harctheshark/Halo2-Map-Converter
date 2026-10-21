# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Source-to-source: the DX11 branch of a H2EK ``.psh`` pixel shader (the text the MCC tool compiled to the DXBC in a map) -> HLSL for Direct3D 9.

Only the sampling API and the (unused) ``SV_POSITION`` input change (``SamplerState sN`` + ``Texture2D sNtex`` + ``sNtex.Sample(sN, c)``  ->  ``sampler2D sN`` +
``tex2D(sN, c)``; ``TextureCube`` -> ``samplerCUBE`` / ``texCUBE``; ``Texture3D`` -> ``sampler3D`` / ``tex3D``); every arithmetic statement is kept character for
character.  Compile the result with ``-DDX11`` (so the DX11 branches are the ones that count) after ``DX11_PRELUDE_D3D9``; the three variants add ``ZPRIME`` / ``MRT``.

Why not the source's own ``#ifndef DX11`` branch?  It is the original Halo 2 Vista program, and in the Halo-CE port passes it differs from the DX11 branch
(``tex3Dproj`` divides a volume coordinate by w, the DX11 branch does not: ``cann\\shaders\\passes\\light_2cc_diff`` implementation 1 evaluates differently,
measured with the converter's evaluators).  The DX11 branch is what the map's DXBC was compiled from, so it is the faithful one.
"""
import re

DX11_PRELUDE_D3D9 = (
    "#define DX11_sampler2D(i)          sampler2D s##i : register(S##i)\n"
    "#define DX11_samplerCUBE(i)        samplerCUBE s##i : register(S##i)\n"
    "#define DX11_tex2D(si, tci)        tex2D(si, tci.xy)\n"
    "#define DX11_tex2Dproj(si, tci)    tex2D(si, tci.xy/tci.w)\n"
    "#define DX11_texCUBE(si, tci)      texCUBE(si, tci.xyz)\n")

_SAMP = re.compile(r'^[ \t]*SamplerState[ \t]+s(\d+)[ \t]*:[ \t]*register\([sS]\d+\)[ \t]*;[ \t]*$', re.M)
_TEX = re.compile(r'^([ \t]*)(Texture2D|TextureCube|Texture3D)[ \t]+s(\d+)tex[ \t]*:[ \t]*register\([tT]\d+\)[ \t]*;', re.M)
_POS = re.compile(r'^[ \t]*float4[ \t]+pos_unused[ \t]*:[ \t]*SV_POSITION[ \t]*,[ \t]*$', re.M)
_KIND = {'Texture2D': ('sampler2D', 'tex2D'), 'TextureCube': ('samplerCUBE', 'texCUBE'), 'Texture3D': ('sampler3D', 'tex3D')}
_CRLF = '\r' + '\n'


def to_d3d9(text):
    """DX11 chunk text -> D3D9 HLSL text.  Raises ValueError on a Sample() the declarations do not explain (a texture type is never guessed)."""
    kinds = {}
    text = text.replace(_CRLF, '\n')

    def tex_decl(m):
        kinds[int(m.group(3))] = m.group(2)
        return '%s%s s%s : register(s%s);' % (m.group(1), _KIND[m.group(2)][0], m.group(3), m.group(3))
    out = _SAMP.sub('', text)
    out = _TEX.sub(tex_decl, out)
    out = _POS.sub('', out)

    def sample(m):
        k = int(m.group(1))
        if k not in kinds:
            raise ValueError('Sample() on s%d without a Texture declaration' % k)
        return '%s(s%d,' % (_KIND[kinds[k]][1], k)
    out = re.sub(r'\bs(\d+)tex\.Sample\(s\1,', sample, out)
    if re.search(r'\.Sample\s*\(', out):
        raise ValueError('unconverted .Sample( left in the shader text')
    return out


# ---------------------------------------------------------------------------------------------------------------------------------------------------
# Vertex shaders.  The H2EK ``.vertex_shader`` tag keeps, next to every vs_5_0 DXBC blob, the HLSL text the MCC tool compiled (the Halo 2 vertex shader wrapper:
# ``c[N]`` constants, ``r0..r11`` temporaries, ``oT0..oT7`` / ``oD0`` / ``oD1`` / ``oPos`` outputs, the dp3 / dp4 / hsge helpers).  The program is already written in
# Direct3D 9 register terms (it was a vs_2_0 program in Halo 2), so the Direct3D 9 version of it differs from the text MCC compiled in exactly two places:
#   * the output position semantic (``SV_POSITION`` in the DX11 text, ``POSITION`` for Direct3D 9)
#   * the DX11 wrapper's final statement ``output.oPos = lerp(oPos, float4(oPos.x, oPos.y, c[10].z + c[10].w * oPos.w, oPos.w), useW);`` - MCC's reverse-Z remap of the
#     clip depth (a function of w and the per-view constants c10.z / c10.w).  Every Vista vertex shader writes the plain clip position (``mov oPos, r0``), so the
#     statement is removed and ``output.oPos = oPos`` (already in the text) stands.  ``output.oT7.xyzw = oPos`` precedes it in the text: TEXCOORD7 carries the clip
#     position BEFORE the remap in MCC as in Vista, which the pixel shaders' depth stripes read.
# Every other statement is kept character for character (the 'wookiewookie' hsge block, the pow / MAXPOWER block, the r10 / r11 temporaries).
# ---------------------------------------------------------------------------------------------------------------------------------------------------
_VS_POSDECL = 'float4 oPos : SV_POSITION;'
_VS_REMAP = re.compile(r'^[ \t]*output\.oPos[ \t]*=[ \t]*lerp\(oPos,[ \t]*float4\(oPos\.x,[ \t]*oPos\.y,[ \t]*c\[10\]\.z[ \t]*\+[ \t]*c\[10\]\.w[ \t]*\*[ \t]*oPos\.w,[ \t]*oPos\.w\),[ \t]*useW\);[ \t]*$', re.M)


def vs_to_d3d9(text):
    """DX11 vertex shader text of the H2EK vertex_shader tag -> HLSL text for vs_2_0 (see the comment above).  Raises ValueError when the text is not the
    expected wrapper form (exactly one SV_POSITION declaration and exactly one z-remap statement); nothing else is touched."""
    t = text.replace(_CRLF, '\n')
    if t.count(_VS_POSDECL) != 1 or t.count('SV_POSITION') != 1:
        raise ValueError('vertex shader text: expected exactly one "%s"' % _VS_POSDECL)
    hits = _VS_REMAP.findall(t)
    if len(hits) != 1 or t.count('lerp(') != 1:
        raise ValueError('vertex shader text: expected exactly one DX11 z-remap statement (lerp(oPos, ..., useW))')
    if 'output.oPos=oPos;' not in t or 'output.oT7.xyzw = oPos;' not in t:
        raise ValueError('vertex shader text: the plain oPos / oT7 statements are missing')
    t = t.replace(_VS_POSDECL, 'float4 oPos : POSITION;')
    return _VS_REMAP.sub('', t)
