# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Vista -> MCC builders for the shader-template family in CACHE form: `stem` (shader_template), `spas` (shader_pass) and
`vrtx` (vertex_shader).

Why this module exists
----------------------
Both games keep only the COMPILED runtime structure of these three groups in a cache (authoring blocks are empty, 4796/4796 spas, 5029/5029
stems, 3048/3048 vrtx of the 560 Vista maps).  The Vista code is Direct3D 9 byte code (vs_2_0, ps_2_0, ps_3_0); MCC runs DXBC (vs_5_0 / ps_5_0).
A template that exists in no MCC map (the 39 community stems, 12 spas, 1 vrtx of the community maps measured) therefore has to be
PORTED: its structure is rebuilt in the MCC layout from the Vista tag and the code blobs are replaced by DXBC compiled from the Vista blobs
(the compiler is NOT part of this module).  Everything here is a deterministic, field-by-field rebuild - no guessing, no substitute templates.

Public API (all take a RESOLVER `res` = h2conv.rev.transcode.RevCtx / a test session ctx / NameResolver, see below)
-------------------------------------------------------------------------------------------------------------
build_mcc_vrtx(res, vista_root, dxbc)            -> MCC vrtx root Node        dxbc: [bytes per Vista classification] | callable(i, vista_blob)
build_mcc_spas(res, vista_root, dxbc)            -> MCC spas root Node        dxbc: {(impl, ps): (b0, b1, b2)} | {(impl, ps, slot): bytes} | callable(impl, ps, slot, vista_blob)
build_mcc_stem(res, vista_root, lods=None)       -> MCC stem root Node        lods=n: the cut-down form the H2EK tool writes (never needed for a port)
port_vista_tag(res, group, vista_root, dxbc)     dispatcher
needed_blobs(group, vista_root)                  -> [BlobJob] the Direct3D 9 blobs a compiler has to translate (and where the result goes)
vista_pixel_blobs / vista_vertex_blobs           the D3D9 blobs of a decoded Vista spas / vrtx;  mcc_pixel_blobs / mcc_vertex_blobs: the DXBC blobs of a
                                                 decoded MCC tag (tests feed MCC's own code back in)
swizzle_to_mcc(v)                                vrtx Output Swizzle rule (docs section 3.3)
check_dxbc(blob, kind)                           structural sanity check of a DXBC container (the 'ps' / 'vs' profile stock MCC tags carry)
stem_signature / stem_structure_diff / truncate_signature / stem_is_cut_down_of / spas_structure_signature / classify_spas
                                                 datum-space independent structure comparison (Vista tag vs MCC tag of the same name)
validate_stem / validate_spas / validate_vrtx    internal consistency of a built tag (packed index ranges, blobs, handles, padding)
MCC_RESTRUCTURED_STEMS / MCC_EDITED_SPAS / MCC_REAUTHORED_VRTX    the names whose stock MCC tag is not the Vista tag (measured, re-checked by the test)
SEMANTIC_WATER_CHAINS / is_water_chain / water_chain_reason       the water stems / passes / vertex shaders whose MCC namesake is another SHADER (measured reasons;
                                                                 the planner ports them privately as '<name>__vista')

Resolver protocol (duck typed; RevCtx and the test session ctxs satisfy it):
    res.lay_mcc[group].fields / .base_size   MCC plugin layouts (optional: loaded from h2conv.rev.transcode when missing)
    res.remap_datum(vista_datum) -> MCC datum or 0xFFFFFFFF      Vista tag datum (relative to the map being converted) -> MCC output datum
    res.remap_sid(vista_sid) -> MCC string id                    by text
    res.warn(code, msg)                                          optional; codes shader_tags_ref_unresolved / shader_tags_empty_pixel_slot
"""
import ctypes
try:                                                    # pragma: no cover
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:                                       # noqa: BLE001
    pass

import collections
import struct

from .. import tagtree as T

NULL = 0xFFFFFFFF
GROUPS = ('stem', 'spas', 'vrtx')
MAX_BLOB_BYTES = 32768           # DataSize(32768) of the compiled-shader datarefs (xlive/Blam/Engine/shaders/pixel_shader.h); stock MCC maximum: ps 4948, vs 6588

# vertex type of a geometry classification (index in the vrtx classification lists); xlive/Blam/Engine/rasterizer/dx11/vertex_shaders_dx11/
# preprocessed_hlsl_from_tool/_vertex_shader_dump.py:21 - a shader implements a prefix of them (1, 2, 3, 7, 9 or 12 in the stock maps)
VRTX_CLASSIFICATION_NAMES = ('world', 'rigid', 'rigid_boned', 'skinned_1_bone', 'skinned_2_bone', 'skinned_3_bone', 'skinned_4_bone', 'screen',
                             'parallel', 'perpendicular', 'vertical', 'horizontal')
# DXBC output signature of EVERY stock MCC vertex shader (573 blobs): (semantic, index, register, mask); FOG (reg 11, mask 1) / PSIZE are appended iff
# the D3D9 shader writes oFog / oPts
VS_OSGN_FIXED = (('SV_POSITION', 0, 0, 15), ('COLOR', 0, 1, 15), ('COLOR', 1, 2, 15), ('TEXCOORD', 0, 3, 15), ('TEXCOORD', 1, 4, 15), ('TEXCOORD', 2, 5, 15),
                 ('TEXCOORD', 3, 6, 15), ('TEXCOORD', 4, 7, 15), ('TEXCOORD', 5, 8, 15), ('TEXCOORD', 6, 9, 15), ('TEXCOORD', 7, 10, 15))

_U32 = struct.Struct('<I')
_U32x2 = struct.Struct('<II')


class ShaderTagError(ValueError):
    """A tag cannot be ported faithfully (the message says why); the caller reports it, never approximates."""


# ================================================================================================
# field offsets (Assembly plugin XML, Halo2 vs Halo2MCC; asserted against the layouts by selftest())
# ================================================================================================
# vrtx  Vista 0x10:  +0 Platform enum16, +2 Unknown int16, +4 Geometry Classifications (0x14: +0 undefined, +4 Compiled Shader, +0xC Code), +0xC Output Swizzles
#       MCC   0x24:  +0 DX11 block, +8 DX9 block, +0x10 Xbox1 block (each 0x18: +0 u64 Pointer, +8 Compiled Shader, +0x10 Code), +0x18/+0x1C/+0x20 Output Swizzles DX11/DX9/Xbox1
V_VRTX_PLATFORM, V_VRTX_UNK, V_VRTX_BLOCK, V_VRTX_SWZ = 0x00, 0x02, 0x04, 0x0C
V_VRTX_EL = 0x14
V_VRTX_EL_BLOB, V_VRTX_EL_CODE = 0x04, 0x0C
M_VRTX_DX11, M_VRTX_DX9, M_VRTX_XBOX = 0x00, 0x08, 0x10
M_VRTX_SWZ_DX11, M_VRTX_SWZ_DX9, M_VRTX_SWZ_XBOX = 0x18, 0x1C, 0x20
M_VRTX_EL = 0x18
M_VRTX_EL_PTR, M_VRTX_EL_BLOB, M_VRTX_EL_CODE = 0x00, 0x08, 0x10

# spas  root 0x24 in both games; Postprocess Definition @0x1C (0x58); its Implementations @0 (0x14A); Pixel Shader block @0x132 in the implementation:
#       Vista element 0x2C: +0/+4/+8 u32 handles, +0xC Constant Register Defaults, +0x14/+0x1C/+0x24 Compiled Shader
#       MCC   element 0x38: +0/+8/+0x10 u64 handles, +0x18 Constant Register Defaults, +0x20/+0x28/+0x30 Compiled Shader
SPAS_PP = 0x1C
SPAS_PP_IMPL = 0x00
SPAS_IMPL_PS = 0x132
SPAS_IMPL_VERTEX_SHADER = 0xFC
V_PS_DEFAULTS, V_PS_BLOBS, V_PS_EL = 0x0C, (0x14, 0x1C, 0x24), 0x2C
M_PS_DEFAULTS, M_PS_BLOBS, M_PS_EL = 0x18, (0x20, 0x28, 0x30), 0x38
M_PS_HANDLES = (0x00, 0x08, 0x10)

# stem  root 0x60 in both games, identical layout; Postprocess Definition @0x58 (0x28)
STEM_PP = 0x58


# ================================================================================================
# where the stock MCC tag of the SAME NAME is not the Vista tag (measured over the 43 stock map pairs).  For these names MCC re-authored the tag; a donor from MCC is NOT interchangeable with the
# Vista tag, a port from Vista is what the Vista shaders were compiled against.
# ================================================================================================
# stems whose MCC structure differs (all use MCC-only passes or MCC-edited ones, docs section 2)
MCC_RESTRUCTURED_STEMS = (
    'shaders\\shader_templates\\hud\\shield_test',
    'shaders\\shader_templates\\opaque\\prt_simple',
    'shaders\\shader_templates\\opaque\\tex_bump_active_camo',
    'shaders\\shader_templates\\opaque\\tex_bump_alpha_test',
    'shaders\\shader_templates\\opaque\\tex_bump_alpha_test_single_pass',
    'shaders\\shader_templates\\opaque\\tex_bump_detail_blend',
    'shaders\\shader_templates\\opaque\\tex_bump_env',
    'shaders\\shader_templates\\opaque\\tex_bump_env_clamped',
    'shaders\\shader_templates\\opaque\\tex_bump_env_clamped_combined',
    'shaders\\shader_templates\\opaque\\tex_bump_env_illum',
    'shaders\\shader_templates\\opaque\\tex_bump_env_illum_combined',
    'shaders\\shader_templates\\opaque\\tex_bump_env_two_change_color_multiply_map_self_illum',
    'shaders\\shader_templates\\opaque\\tex_bump_one_change_color',
    'shaders\\shader_templates\\opaque\\tex_env',
)
# passes whose MCC structure differs from the Vista pass of the same name: name -> (stock stems identical to their Vista twin that reference the
# MCC pass = precedent that a VISTA-structured stem works with MCC's version, what MCC changed)
MCC_EDITED_SPAS = {
    'effects\\screen_effects\\copy_fb_to_texaccum': (1, 'vertex shader screen_xform_4 -> screen_xform_4_dx11'),
    'effects\\screen_effects\\copy_texaccum_to_fb': (1, 'vertex shader screen_2_stage -> screen_4_stage_dx11'),
    'effects\\screen_effects\\desaturate': (1, 'vertex shader screen_xform_4 -> screen_xform_4_dx11'),
    'shaders\\shader_passes\\dynamic_light_gels\\plasma_light_gel': (1, 'vertex shader screen_xform_4 -> screen_xform_4_dx11'),
    'shaders\\shader_passes\\hud\\shield_test': (0, 'Render States value 7 -> 0xF'),
    'shaders\\shader_passes\\lightmap\\lightmap_emblem_flag': (1, 'Texture States byte 0x0404 -> 0x0303'),
    'shaders\\shader_passes\\lightmap\\lightmap_tex_detail_alpha_test': (0, 'Textures 20 -> 16 (fifth texture of every implementation removed), Render States 28 -> 36, Externs[0]'),
    'shaders\\shader_passes\\prt\\prt_simple': (0, 'Textures 5 -> 4'),
    'shaders\\shader_passes\\shadow\\shadow_generate': (68, 'Render States 21 -> 19 (state-0x16 entries 4 -> 2)'),
    'shaders\\shader_passes\\transparent\\default_transparent': (2, 'Render States 7 -> 9'),
    'shaders\\shader_passes\\transparent\\sky_one_add_two_plus_two': (1, 'Render States value 2 -> 6'),
    'shaders\\shader_passes\\water\\water_pool': (1, 'Texture States byte 3 -> 1'),
    'shaders\\shader_passes\\water\\water_pool_static': (1, 'Render States value 2 -> 6'),
}
# vertex shaders whose Output Swizzles differ from the 0 -> F rule (MCC re-authored the vertex shader source)
MCC_REAUTHORED_VRTX = (
    'rasterizer\\vertex_shaders_dx9\\effect',
    'rasterizer\\vertex_shaders_dx9\\prt',
    'rasterizer\\vertex_shaders_dx9\\water_pool',
    'rasterizer\\vertex_shaders_dx9\\water_pool_static',
)

# ================================================================================================
# the water chains whose MCC namesakes are other SHADERS
# ================================================================================================
# MCC_EDITED_SPAS / MCC_REAUTHORED_VRTX record what 343 changed in the STRUCTURE of a tag.  For the three water families below, MCC's stem + pass + vertex shader of the same
# names are different SHADERS, not the same shader with other art: 343 re-authored the vertex shaders so that they divide the screen-space reflection / refraction coordinate PER
# VERTEX (exact only on a finely tessellated mesh) and the pass samples the reflection as a Texture2D at that coordinate instead of a cube at the reflection vector, then
# compensated per shader, by hand, for the water of its stock maps (template switch to *_cube, cube art replaced by 2D, fresnel art swap, vertex-constant phase shift).  Nothing
# replays that for a map authored against the Vista chain, so the planner (option water_chain, default 'vista') does NOT map such a shader onto MCC's chain: stem, pass and vertex
# shader are ported from the Vista byte code under the private names '<name>__vista' (shader_port.private_water_name).  Not on the registry (pixel and vertex shaders equal on
# the lanes the pixel shader reads): water_edge_blend, ocean_static, ocean_static_opaque, ocean_static_opaque_blend, waterfall; the already-private chain of
# water_pool_static_transparent_tint stays as it is.  Every reason below is a measurement.
_WATER_STEM_WHY = ('its Pass is the pass of the same name, which is another shader in MCC (see the pass entry); MCC\'s stock water shaders were re-authored by hand for the MCC chain '
                   '(template switch to *_cube, cube art replaced by 2D, fresnel art swap, vertex-constant phase shift) and none of that exists for a map authored against the Vista '
                   'chain')
_WATER_PS_WHY = ('pixel shader: MCC samples the reflection as a Texture2D at the interpolated screen UV (v4.xy) where the Vista pass samples a CUBE (dcl_cube s1) at the reflection '
                 'vector (t1.xyz) - equal only after folding cube -> 2D')
_WATER_PAIR_WHY = ('the converter\'s own vs_ps_composite rejects the donor pair (MCC vertex shader driving the Vista pixel shaders) in 6 of 6 combinations, with random vertex constants AND with '
                   'the constants the engine pins (c4.w = 1, c5.w = 0.5, c6.w = 2, c15.y = 0, c21 = (1, -1, -1, 0.5); vs_ps_composite(pin_engine=True))')
# water_static (vertex shader `water`) is different: the pointwise pairing test rejects the donor pair only because it draws c21 at random; at the engine's constants the pair is exact AT
# THE VERTICES and the whole defect is the interpolation between them (TEXCOORD2 pre-divided, w = 1) - measured with the engine's pinned constants and by an
# interpolation-aware check
_WATER_STATIC_PAIR_WHY = ('the pointwise pairing test (vs_ps_composite) rejects the donor pair in 6 of 6 combinations only because it draws c21 at random; with the constants the engine pins '
                          '(pin_engine=True) the donor `water` vertex shader drives the Vista pixel shaders exactly AT THE VERTICES (6 of 6 ok) - the defect is the interpolation between '
                          'vertices, which only an interpolation-aware check sees')
_WATER_COARSE = ('the interpolated per-vertex quotient is exact only on a finely tessellated mesh (stock pools: 0.2 - 0.9 wu edges); on a coarse custom mesh (Holidays Grave: very large triangles, '
                 '100 - 193 wu edges) the lookup collapses to a near-constant screen location that slides with the view')
SEMANTIC_WATER_CHAINS = {
    'stem': {
        'shaders\\shader_templates\\water\\water_pool': _WATER_STEM_WHY,
        'shaders\\shader_templates\\water\\water_pool_static': _WATER_STEM_WHY,
        'shaders\\shader_templates\\water\\water_static': _WATER_STEM_WHY,
    },
    'spas': {
        'shaders\\shader_passes\\water\\water_pool': (
            _WATER_PS_WHY + '; Texture States byte 3 -> 1 (MCC_EDITED_SPAS); it names MCC\'s re-authored vertex shader water_pool (see there); ' + _WATER_PAIR_WHY
            + '; 343\'s own water_pool_cube pass is the Vista pass (tables and pixel shaders, cube kept) with MCC\'s vertex shader'),
        'shaders\\shader_passes\\water\\water_pool_static': (
            _WATER_PS_WHY + '; Render States DESTBLEND ONE (2) -> INVSRCALPHA (6) (MCC_EDITED_SPAS); it names MCC\'s re-authored vertex shader water_pool_static (see there); '
            + _WATER_PAIR_WHY + '; 343\'s own water_pool_static_cube pass is the Vista pass (tables and pixel shaders, cube kept) with MCC\'s vertex shader'),
        'shaders\\shader_passes\\water\\water_static': (
            'pixel shaders equal on every lane they read, but the pass names MCC\'s re-authored vertex shader '
            '`water` (see there); ' + _WATER_STATIC_PAIR_WHY),
    },
    'vrtx': {
        'rasterizer\\vertex_shaders_dx9\\water_pool': (
            'MCC re-authored it: no NORMAL input; TEXCOORD1 = the per-vertex divided screen UV (u, v, 0, 1) where Vista writes the reflection vector 2(e.n)n - e; TEXCOORD2 = the same UV clamped to '
            '[c15.y, c21.x] with zw = (0, 1) where Vista writes the projective (0.5(x+w), 0.5(w-y), w, w) that its pixel shader divides per pixel; ' + _WATER_COARSE),
        'rasterizer\\vertex_shaders_dx9\\water_pool_static': (
            'MCC re-authored it: TEXCOORD1.xyz = the per-vertex divided screen position (c21 = (1, -1, -1, 0.5): (x+w, w-y, z+w) / 2w, w = 1) where Vista writes the reflection vector '
            '2(e.n)n - e (the cube lookup); TEXCOORD2 (bump UV) is equal; ' + _WATER_COARSE),
        'rasterizer\\vertex_shaders_dx9\\water': (
            'MCC re-authored it: TEXCOORD1 is equal but TEXCOORD2 is a copy of the per-vertex divided TEXCOORD1 (w = 1) where Vista writes the projective (0.5(x+w), 0.5(w-y), w, w) that its '
            'pixel shader divides per pixel (the raw lanes differ, but after the pixel shader\'s division the two are the same number at a vertex: this is an INTERPOLATION defect only); '
            + _WATER_COARSE + ' (TEXCOORD2 differs in every lane; ' + _WATER_STATIC_PAIR_WHY + ')'),
    },
}
# the reflection map of a shader on these stems: the pixel shader's `dcl_cube s1`, i.e. Bitmaps[1] (stem Remappings, bitmap registers [(0,0), (1,1), (2,3)]; the tests
# re-derive it from the Vista tags)
WATER_REFLECTION_BITMAP_SLOT = 1
# ... but only for the POOL passes.  The cube sampler of each registry pass and the Bitmaps[] slot that feeds it (stem Remappings (register, slot)); None = the pass declares NO
# cube sampler: water_static's four samplers are all dcl_2d and its register 1 is fed by slot 3 (Remappings [(0, 0), (1, 3)]), so a 2D bitmap there is right, not a mismatch.
# The tests re-derive both columns from the Vista tags of beavercreek / gemini / needle.
WATER_CUBE_SAMPLER_SLOT = {'water_pool': 1, 'water_pool_static': 1, 'water_static': None}


def water_cube_slot(stem_name):
    """The Bitmaps[] slot that feeds the cube sampler (`dcl_cube s1`) of the pass of the registry stem `stem_name` (the Vista name or its private '<name>__vista'), or None when the
    pass declares no cube sampler (water_static) or the stem is not on the registry."""
    n = (stem_name or '').lower()
    if n.endswith('__vista'):
        n = n[:-len('__vista')]
    return WATER_CUBE_SAMPLER_SLOT.get(n.rsplit('\\', 1)[-1])


def water_chain_reason(group, name):
    """The measured reason why MCC's tag (group, name) is another SHADER than the Vista tag of that name (SEMANTIC_WATER_CHAINS), else None.  Case-insensitive."""
    return SEMANTIC_WATER_CHAINS.get(group, {}).get((name or '').lower())


def is_water_chain(group, name):
    """True when the Vista stem / pass / vertex shader `name` is on the registry of semantically different water chains."""
    return water_chain_reason(group, name) is not None


# ================================================================================================
# the sky vertex shaders whose Vista clip depth is PINNED to the far plane
# ================================================================================================
# Vista's two sky vertex shaders end `add r0.zw, r1.x, c3.w ; mov oPos, r0`: clip z == clip w EXACTLY (forward Z depth 1.0 = the far plane), and Vista draws the sky FIRST with
# ZENABLE off, so a sky polygon can never occlude geometry.  MCC draws the sky LATE with the depth test ON (reverse Z, GREATEREQUAL, ties pass) and its own sky vertex shaders (the
# same code as the ordinary transparent_generic_reflection) write REAL depth z = w * c10.w + c10.z, so a Vista sky polygon inside the far plane that is at or above the BSP wins:
# cbsp's flat ground cap at z = 0 painted flat blue over the coplanar floor.  The faithful MCC image of Vista's pinned depth is z_mcc = w - z_D3D9 = w - w = 0 (reverse-Z far plane);
# translate_vs emits it ('pin', d3d9_to_psh.vs_clip_z_equals_w) and the planner (option sky_depth, default 'vista') takes these two vertex shaders from the Vista tag of the map under
# the SAME names; MCC's sky stems / passes / pixel shaders stay.  The two names are referenced only by sky passes (146 / 3 of the 153 package maps).
# Every reason is a measurement.
_SKY_PIN_WHY = ('Vista: every geometry classification ends with clip z == clip w (max |z - w| = 0 over 7 classes x 128 random constant sets; the blobs are md5-identical in 169 of 169 Vista '
                'source maps); MCC: the same-name vertex shader is MCC\'s ordinary world-space one (DXBC class 0 md5 4ec7f146..., all 7 classes byte-identical to transparent_generic_reflection), '
                'z = w * c10.w + c10.z = real reverse-Z depth, drawn late with the depth test on')
SKY_DEPTH_PINNED_VRTX = {
    'rasterizer\\vertex_shaders_dx9\\transparent_generic_reflection_sky': _SKY_PIN_WHY,
    'rasterizer\\vertex_shaders_dx9\\transparent_generic_default_sky': _SKY_PIN_WHY,
}


def sky_pin_reason(name):
    """The measured reason why the Vista vertex shader `name` is taken from the Vista tag (z pinned to the far plane) instead of MCC's same-name tag (SKY_DEPTH_PINNED_VRTX), else
    None.  Case-insensitive."""
    return SKY_DEPTH_PINNED_VRTX.get((name or '').lower())


def is_sky_pinned_vrtx(name):
    """True when the vertex shader `name` is on the registry of Vista sky vertex shaders that pin the clip depth to the far plane."""
    return sky_pin_reason(name) is not None


# ================================================================================================
# resolvers
# ================================================================================================
def _mcc_layouts(res):
    lay = getattr(res, 'lay_mcc', None)
    if lay:
        return lay
    from . import transcode as X                         # lazy: this module must stay importable from a handler without a cycle
    return X.layouts('mcc')


def _vista_layouts(res):
    lay = getattr(res, 'lay_vista', None)
    if lay:
        return lay
    from . import transcode as X
    return X.layouts('vista')


class NameResolver(object):
    """Stand-alone resolver: Vista datum -> (group, name) of the Vista map -> MCC datum via `mcc_datum(group, name)`; string ids by text
    via `mcc_sid(text)`.  `vista_cache` is the VistaCache the datums / string ids belong to (shared placeholders resolve by name too)."""

    def __init__(self, vista_cache, mcc_datum, mcc_sid, lay_mcc=None, lay_vista=None):
        self.vista_cache = vista_cache
        self.mcc_datum = mcc_datum
        self.mcc_sid = mcc_sid
        self.lay_mcc = lay_mcc
        self.lay_vista = lay_vista
        self.warnings = []

    def remap_datum(self, d):
        if d in (None, NULL, 0):
            return NULL
        i = d & 0xFFFF
        tags = self.vista_cache.tags
        if i >= len(tags) or tags[i].datum != d or not tags[i].name:
            return NULL
        r = self.mcc_datum(tags[i].group, tags[i].name)
        return NULL if r is None else r

    def remap_sid(self, sid):
        if not sid or sid == NULL:
            return sid or 0
        return self.mcc_sid(self.vista_cache.string_id(sid))

    def warn(self, code, msg, tag=None):
        self.warnings.append((code, msg))


def _warn(res, code, msg):
    fn = getattr(res, 'warn', None)
    if fn is not None:
        try:
            fn(code, msg)
        except Exception:                                # noqa: BLE001
            pass


# ================================================================================================
# generic same-layout clone (stem, spas, and the identical sub-structures of both games)
# ================================================================================================
def _fields_equal(sf, df, ssize, dsize, path, skip=()):
    """The two layouts must pair field for field (offset, type, size, name) except the named block fields in `skip`; raises otherwise."""
    if ssize != dsize:
        raise ShaderTagError('layout size mismatch at %r: Vista 0x%X vs MCC 0x%X' % (path, ssize, dsize))
    sm = {f.offset: f for f in sf}
    dm = {f.offset: f for f in df}
    if set(sm) != set(dm):
        raise ShaderTagError('layout field offsets differ at %r' % path)
    for off, a in sm.items():
        b = dm[off]
        if (a.type, a.size) != (b.type, b.size):
            raise ShaderTagError('layout field %r@0x%X differs at %r: %s/%d vs %s/%d' % (a.name, off, path, a.type, a.size, b.type, b.size))
        if a.type in T.BLOCK_TYPES and a.name not in skip:
            _fields_equal(a.children, b.children, T.element_size(a), T.element_size(b), path + '/' + a.name, skip)


class _Cloner(object):
    """Rebuilds a Vista Node tree as an MCC Node tree of identical layout: bytes copied, tag references through res.remap_datum, string ids
    through res.remap_sid, blocks / datarefs recursively.  A Vista list or payload referenced twice stays shared (the encoder then emits it once,
    like the cache builder).  `overrides[(mcc path, field name)](cloner, snode, dnode, dfield)` replaces the clone of one block field."""

    def __init__(self, res, group, overrides=None, ref_groups=None):
        self.res = res
        self.group = group
        self.overrides = overrides or {}
        self.ref_groups = ref_groups or {}
        self.memo = {}
        self.missing_refs = []                           # [(path, field, vista datum)]
        self.stats = collections.Counter()

    # ---- leaves --------------------------------------------------------------------------------
    def _ref(self, s, d, f, path):
        so = f.offset
        if f.size == 8:
            g, dat = _U32x2.unpack_from(s.data, so)
        else:
            g, dat = None, _U32.unpack_from(s.data, so)[0]
        if dat == NULL or (f.size == 4 and dat == 0) or (f.size == 8 and dat == 0 and g == 0):
            nd = dat
        else:
            nd = self.res.remap_datum(dat)
            self.stats['ref_remapped'] += 1
            if nd == NULL:
                self.missing_refs.append((path, f.name, dat))
                self.stats['ref_dropped'] += 1
        if f.size == 8:
            hint = self.ref_groups.get((path, f.name))
            if g == NULL and nd != NULL and hint:
                g = struct.unpack('<I', hint.encode('latin-1')[::-1])[0]
            _U32x2.pack_into(d.data, so, g, nd)
        else:
            _U32.pack_into(d.data, so, nd)

    def clone(self, s, dfields, dsize, dpath):
        d = T.Node(self.group, dpath, dfields, dsize, s.data)
        for f in dfields:
            if f.type == 'tagref':
                self._ref(s, d, f, dpath)
            elif f.type == 'stringid':
                v = _U32.unpack_from(s.data, f.offset)[0]
                if v and v != NULL:
                    _U32.pack_into(d.data, f.offset, self.res.remap_sid(v))
            elif f.type in T.BLOCK_TYPES:
                self.clone_block(s, d, f, dpath)
            elif f.type in T.DATA_TYPES:
                payload = s.datas.get(f.offset) or b''
                d.datas[f.offset] = payload
                _U32.pack_into(d.data, f.offset, len(payload))
                _U32.pack_into(d.data, f.offset + 4, 0)
        return d

    def clone_block(self, s, d, f, dpath):
        ov = self.overrides.get((dpath, f.name))
        if ov is not None:
            ov(self, s, d, f)
            return
        kids = s.blocks.get(f.offset) or []
        key = (id(kids), id(f.children))
        out = self.memo.get(key)
        if out is None:
            cp = dpath + '/' + f.name
            es = T.element_size(f)
            out = [self.clone(k, f.children, es, cp) for k in kids]
            if kids:
                self.memo[key] = out
        d.blocks[f.offset] = out
        _U32.pack_into(d.data, f.offset, len(out))
        _U32.pack_into(d.data, f.offset + 4, 0)

    def clone_elements(self, kids, f, dpath):
        """Clone a list of source elements into the destination block field `f` (used by overrides)."""
        cp = dpath + '/' + f.name
        es = T.element_size(f)
        key = (id(kids), id(f.children))
        out = self.memo.get(key)
        if out is None:
            out = [self.clone(k, f.children, es, cp) for k in kids]
            if kids:
                self.memo[key] = out
        return out


def pad_postprocess(root, count=3):
    """The MCC tool writes THREE 'Postprocess Definition' elements of stem / spas / shad / prt3: [0] real, [1] and [2] all zero (DX9 / Xbox1
    slots; 93/93 custom, 16/16 stock stems, 98/98 + 16/16 spas)."""
    f = root.field('Postprocess Definition')
    kids = root.blocks.get(f.offset) or []
    if len(kids) != 1 and len(kids) != count:
        raise ShaderTagError('%s: %d postprocess definitions (a cache tag carries exactly 1; 4796 spas / 5029 stems checked)' % (root.group, len(kids)))
    es = T.element_size(f)
    kids = list(kids)
    while len(kids) < count:
        kids.append(T.Node(root.group, '/' + f.name, f.children, es))
    root.blocks[f.offset] = kids
    _U32.pack_into(root.data, f.offset, len(kids))


# ================================================================================================
# DXBC
# ================================================================================================
def dxbc_chunks(blob):
    """{fourcc: [payload bytes]} of a DXBC container ({} when the magic is wrong)."""
    if len(blob) < 32 or blob[:4] != b'DXBC':
        return {}
    n = _U32.unpack_from(blob, 28)[0]
    out = {}
    for i in range(n):
        o = _U32.unpack_from(blob, 32 + 4 * i)[0]
        if o + 8 > len(blob):
            return {}
        tag = blob[o:o + 4].decode('latin-1')
        sz = _U32.unpack_from(blob, o + 4)[0]
        out.setdefault(tag, []).append(blob[o + 8:o + 8 + sz])
    return out


def dxbc_program(blob):
    """(program type, major, minor) from the SHEX / SHDR chunk: type 0 = pixel, 1 = vertex (D3D10+ version token), None when missing."""
    cs = dxbc_chunks(blob)
    for tag in ('SHEX', 'SHDR'):
        if tag in cs and len(cs[tag][0]) >= 4:
            tok = _U32.unpack_from(cs[tag][0], 0)[0]
            return ((tok >> 16) & 0xFFFF, (tok >> 4) & 0xF, tok & 0xF)
    return None


def dxbc_signature(blob, chunk='ISGN'):
    """[(semantic, index, register, mask)] of the ISGN (inputs) / OSGN (outputs) chunk of a DXBC blob ([] when absent)."""
    cs = dxbc_chunks(blob)
    if chunk not in cs:
        return []
    r = cs[chunk][0]
    n = _U32.unpack_from(r, 0)[0]
    out = []
    for i in range(n):
        name_off, idx, sv, ct, reg, mask = struct.unpack_from('<IIIIII', r, 8 + 24 * i)
        end = r.index(b'\0', name_off)
        out.append((r[name_off:end].decode('latin-1'), idx, reg, mask & 0xFF))
    return out


def check_dxbc(blob, kind):
    """Problems of a DXBC blob meant for a Pixel Shader slot (kind 'ps') or a vertex shader classification ('vs'): [] when it looks like what the
    stock MCC tags carry (container size, chunk table in range, SHEX program type, shader model 5.0, the chunks RDEF ISGN OSGN SHEX STAT)."""
    probs = []
    if not blob:
        return ['empty blob']
    if len(blob) > MAX_BLOB_BYTES:
        return ['%d bytes: over the %d-byte data size limit of the tag definition' % (len(blob), MAX_BLOB_BYTES)]
    if blob[:4] != b'DXBC':
        return ['no DXBC magic (first bytes %s)' % blob[:4].hex()]
    if len(blob) < 36:
        return ['truncated container']
    if _U32.unpack_from(blob, 24)[0] != len(blob):
        probs.append('container size field %d != blob length %d' % (_U32.unpack_from(blob, 24)[0], len(blob)))
    cs = dxbc_chunks(blob)
    if not cs:
        return probs + ['chunk table out of range']
    for need in ('RDEF', 'ISGN', 'OSGN', 'SHEX'):
        if need not in cs:
            probs.append('missing chunk %s' % need)
    prog = dxbc_program(blob)
    want = 0 if kind == 'ps' else 1
    if prog is None:
        probs.append('no SHEX/SHDR program token')
    else:
        if prog[0] != want:
            probs.append('program type %d, expected %d (%s)' % (prog[0], want, kind))
        if (prog[1], prog[2]) != (5, 0):
            probs.append('shader model %d.%d, stock MCC tags carry 5.0' % (prog[1], prog[2]))
    if kind == 'vs' and 'OSGN' in cs:
        osg = tuple(dxbc_signature(blob, 'OSGN'))
        if osg[:len(VS_OSGN_FIXED)] != VS_OSGN_FIXED or osg[len(VS_OSGN_FIXED):] not in ((), (('FOG', 0, 11, 1),), (('PSIZE', 0, 11, 1),)):
            probs.append('output signature differs from the one of all 573 stock MCC vertex shaders (SV_POSITION, COLOR0-1, TEXCOORD0-7 [+FOG|PSIZE], mask F)')
    return probs


# ================================================================================================
# vrtx
# ================================================================================================
def swizzle_to_mcc(v):
    """vrtx 'Output Swizzles' of the MCC DX11 / Xbox1 slots from the Vista value: 6 nibbles (one per vertex-shader output register used by the
    pixel stage), every nibble 0 (= unused) becomes 0xF; the DX9 slot keeps the Vista value.  93 of 97 distinct stock vrtx names obey it exactly; the 4 exceptions (effect, prt, water_pool, water_pool_static) are MCC re-authored vertex shaders."""
    v &= 0xFFFFFFFF
    if v >> 24:
        raise ShaderTagError('vrtx Output Swizzles 0x%08X is not a 6-nibble value (never seen in 560 Vista maps)' % v)
    out = 0
    for k in range(6):
        x = (v >> (4 * k)) & 0xF
        out |= (0xF if x == 0 else x) << (4 * k)
    return out


def _intern():
    """Per-tag blob pool: equal contents -> the same bytes object (the encoder emits a shared payload once, like the cache builder)."""
    pool = {}

    def get(b):
        b = bytes(b)
        if len(b) <= 1:
            return b
        return pool.setdefault(b, b)
    return get


def vista_vertex_blobs(vista_root):
    """[D3D9 blob per Geometry Classification] (b'' = the tool wrote an empty entry)."""
    return [bytes(e.datas.get(V_VRTX_EL_BLOB) or b'') for e in (vista_root.blocks.get(V_VRTX_BLOCK) or [])]


def mcc_vertex_blobs(mcc_root, api='DX11'):
    off = {'DX11': M_VRTX_DX11, 'DX9': M_VRTX_DX9, 'Xbox1': M_VRTX_XBOX}[api]
    return [bytes(e.datas.get(M_VRTX_EL_BLOB) or b'') for e in (mcc_root.blocks.get(off) or [])]


def _vertex_provider(dxbc):
    if callable(dxbc):
        return dxbc
    if dxbc is None:
        return lambda i, vb: b''
    seq = dxbc
    if isinstance(seq, (list, tuple)):
        return lambda i, vb: seq[i] if i < len(seq) else b''
    return lambda i, vb: seq.get(i, b'')


def build_mcc_vrtx(res, vista_root, dxbc, allow_empty=False, check=True):
    """MCC vrtx (0x24) from a decoded Vista vrtx (0x10) and the DXBC of every geometry classification.

    Rules (every one verified on the 97 distinct stock names / 365+ stock tags, docs 3):
      * Vista Platform (+0) and Unknown (+2) must be 0 (PC; 3048/3048 Vista vrtx) - an Xbox vertex shader raises ShaderTagError.
      * the Vista classification list becomes the MCC DX11 list element for element (same count, same order); each element = u64 Pointer 0,
        Compiled Shader = the DXBC, Code = the Vista Code (always empty).  The DX9 and Xbox1 lists stay EMPTY (the MCC tool leaves them empty).
      * a classification the Vista tool left empty (621 of 16,801 elements) stays empty (no DXBC is asked for); a non-empty one needs a DXBC blob.
      * Output Swizzles: DX11 = Xbox1 = swizzle_to_mcc(Vista), DX9 = Vista value.
    `dxbc`: list/dict indexed by classification index, or callable(index, vista_blob) -> bytes.  allow_empty=True accepts missing blobs (structure
    only builds for planning/tests, never for a map).  check=True runs check_dxbc on every blob."""
    lay = _mcc_layouts(res)['vrtx']
    if vista_root.u16(V_VRTX_PLATFORM) != 0 or vista_root.u16(V_VRTX_UNK) != 0:
        raise ShaderTagError('vrtx Platform=%d Unknown=%d: only the PC platform (0, 0) is portable' % (vista_root.u16(V_VRTX_PLATFORM), vista_root.u16(V_VRTX_UNK)))
    root = T.Node('vrtx', '', lay.fields, lay.base_size or lay.computed_size)
    f11 = root.field('Geometry Classifications DX11')
    f9 = root.field('Geometry Classifications DX9')
    fx = root.field('Geometry Classifications Xbox1')
    if (f11.offset, f9.offset, fx.offset) != (M_VRTX_DX11, M_VRTX_DX9, M_VRTX_XBOX):
        raise ShaderTagError('unexpected MCC vrtx layout')
    prov = _vertex_provider(dxbc)
    intern = _intern()
    kids = []
    es = T.element_size(f11)
    for i, e in enumerate(vista_root.blocks.get(V_VRTX_BLOCK) or []):
        vb = bytes(e.datas.get(V_VRTX_EL_BLOB) or b'')
        code = bytes(e.datas.get(V_VRTX_EL_CODE) or b'')
        if e.data[0:4] != b'\0\0\0\0':
            raise ShaderTagError('vrtx classification %d: Vista Unknown dword != 0' % i)
        if vb:
            mb = prov(i, vb)
            mb = bytes(mb) if mb is not None else b''
            if not mb:
                if not allow_empty:
                    raise ShaderTagError('vrtx classification %d has a %d-byte D3D9 shader but no DXBC was supplied' % (i, len(vb)))
            elif check:
                probs = check_dxbc(mb, 'vs')
                if probs:
                    raise ShaderTagError('vrtx classification %d: bad DXBC: %s' % (i, '; '.join(probs)))
        else:
            mb = b''
        n = T.Node('vrtx', '/' + f11.name, f11.children, es)
        n.datas[M_VRTX_EL_BLOB] = intern(mb)
        n.set_u32(M_VRTX_EL_BLOB, len(mb))
        n.datas[M_VRTX_EL_CODE] = code
        n.set_u32(M_VRTX_EL_CODE, len(code))
        kids.append(n)
    root.blocks[M_VRTX_DX11] = kids
    root.set_u32(M_VRTX_DX11, len(kids))
    root.blocks[M_VRTX_DX9] = []
    root.blocks[M_VRTX_XBOX] = []
    sw = vista_root.u32(V_VRTX_SWZ)
    root.set_u32(M_VRTX_SWZ_DX11, swizzle_to_mcc(sw))
    root.set_u32(M_VRTX_SWZ_DX9, sw)
    root.set_u32(M_VRTX_SWZ_XBOX, swizzle_to_mcc(sw))
    return root


# ================================================================================================
# spas
# ================================================================================================
def vista_pixel_blobs(vista_root):
    """{(impl, ps): [b0, b1, b2]} D3D9 blobs of a decoded Vista spas (slot 0 = ps_2_0, 1 = ps_2_0, 2 = ps_3_0)."""
    out = {}
    pps = vista_root.blocks.get(SPAS_PP) or []
    if not pps:
        return out
    for i, impl in enumerate(pps[0].blocks.get(SPAS_PP_IMPL) or []):
        for j, p in enumerate(impl.blocks.get(SPAS_IMPL_PS) or []):
            out[(i, j)] = [bytes(p.datas.get(o) or b'') for o in V_PS_BLOBS]
    return out


def mcc_pixel_blobs(mcc_root):
    """{(impl, ps): [b0, b1, b2]} DXBC blobs of a decoded MCC spas."""
    out = {}
    pps = mcc_root.blocks.get(SPAS_PP) or []
    if not pps:
        return out
    for i, impl in enumerate(pps[0].blocks.get(SPAS_PP_IMPL) or []):
        for j, p in enumerate(impl.blocks.get(SPAS_IMPL_PS) or []):
            out[(i, j)] = [bytes(p.datas.get(o) or b'') for o in M_PS_BLOBS]
    return out


def _pixel_provider(dxbc):
    if callable(dxbc):
        return dxbc
    if dxbc is None:
        return lambda i, j, k, vb: b''
    m = dxbc

    def get(i, j, k, vb):
        v = m.get((i, j, k))
        if v is not None:
            return v
        t = m.get((i, j))
        if t is not None:
            return t[k]
        return b''
    return get


def build_mcc_spas(res, vista_root, dxbc, allow_empty=False, check=True):
    """MCC spas from a decoded Vista spas and the DXBC of every Pixel Shader slot.

    Rules (docs section 4): the whole tag is the Vista tag cloned in the MCC layout (the two layouts are IDENTICAL except the Pixel Shader element,
    asserted at run time), with
      * Implementations[].Vertex Shader (tagref @0xFC) -> the MCC vrtx datum of the same (group, name) through res.remap_datum;
      * String ids (Constants / Constant Info parameter names) -> res.remap_sid;
      * Postprocess Definition padded to 3 elements ([1], [2] zero);
      * every Pixel Shader element 0x2C -> 0x38: three u64 runtime handles 0 (Vista: three u32, all 0 in 100 %), Constant Register Defaults cloned
        (32 x int32, all 0), and the three Compiled Shader datarefs replaced by DXBC blob k for Vista blob k (k = 0 ps_2_0, 1 ps_2_0, 2 ps_3_0).
    The Vista authoring blocks (Documentation, Parameters, Implementations) are empty in all 4796 Vista spas; they are cloned as they are.
    `dxbc`: {(impl, ps): (b0, b1, b2)} | {(impl, ps, slot): bytes} | callable(impl, ps, slot, vista_blob) -> bytes."""
    lays = _mcc_layouts(res)
    lay = lays['spas']
    vlay = _vista_layouts(res)['spas']
    _fields_equal(vlay.fields, lay.fields, vlay.base_size or vlay.computed_size, lay.base_size or lay.computed_size, '', skip=('Pixel Shader',))
    prov = _pixel_provider(dxbc)
    intern = _intern()
    ctr = collections.Counter()

    def ps_override(cl, s, d, f):
        # s = Vista Implementations element (Node), d = MCC Implementations element being built, f = MCC 'Pixel Shader' field
        impl_idx = ctr['impl']
        kids = s.blocks.get(SPAS_IMPL_PS) or []
        es = T.element_size(f)
        cp = d.path + '/' + f.name
        out = []
        dfld = next(c for c in f.children if c.name == 'Constant Register Defaults')
        for j, p in enumerate(kids):
            q = T.Node('spas', cp, f.children, es)
            # handles stay 0 (the engine writes them at load)
            if any(p.data[0:12]):
                _warn(res, 'shader_tags_handles_dropped', 'spas Pixel Shader handles non-zero in the Vista tag (impl %d ps %d)' % (impl_idx, j))
            q.blocks[M_PS_DEFAULTS] = cl.clone_elements(p.blocks.get(V_PS_DEFAULTS) or [], dfld, cp)
            q.set_u32(M_PS_DEFAULTS, len(q.blocks[M_PS_DEFAULTS]))
            for k in range(3):
                vb = bytes(p.datas.get(V_PS_BLOBS[k]) or b'')
                if not vb:
                    _warn(res, 'shader_tags_empty_pixel_slot',
                          'spas impl %d ps %d slot %d: the Vista tag has NO D3D9 shader there (1 of 909 ps elements of the 168 distinct Vista passes: lightmap_zoe); left empty, not approximated' % (impl_idx, j, k))
                mb = prov(impl_idx, j, k, vb) if vb else b''
                mb = bytes(mb) if mb is not None else b''
                if vb and not mb:
                    if not allow_empty:
                        raise ShaderTagError('spas impl %d ps %d slot %d: %d-byte D3D9 shader but no DXBC supplied' % (impl_idx, j, k, len(vb)))
                elif mb and check:
                    probs = check_dxbc(mb, 'ps')
                    if probs:
                        raise ShaderTagError('spas impl %d ps %d slot %d: bad DXBC: %s' % (impl_idx, j, k, '; '.join(probs)))
                q.datas[M_PS_BLOBS[k]] = intern(mb)
                q.set_u32(M_PS_BLOBS[k], len(mb))
            out.append(q)
        d.blocks[f.offset] = out
        d.set_u32(f.offset, len(out))
        ctr['impl'] += 1

    cl = _Cloner(res, 'spas', overrides={('/Postprocess Definition/Implementations', 'Pixel Shader'): ps_override},
                 ref_groups={('/Postprocess Definition/Implementations', 'Vertex Shader'): 'vrtx'})
    root = cl.clone(vista_root, lay.fields, lay.base_size or lay.computed_size, '')
    pad_postprocess(root)
    _report_missing(res, cl, 'spas')
    return root


# ================================================================================================
# stem
# ================================================================================================
def build_mcc_stem(res, vista_root, lods=None):
    """MCC stem (shader_template) from a decoded Vista stem.  The layouts are identical (asserted); the tag is cloned with Postprocess
    Definition.Passes[].Pass (tagref @0, 8 bytes) -> the MCC spas datum of the same name, String ids by text, and Postprocess Definition padded to
    3 elements.  The STRUCTURE (LODs, Layers, Passes, Implementations, Remappings) stays the Vista one: docs section 2.4 gives the evidence.
    `lods=n` (NOT the default, never needed for a faithful port) applies cut_down_stem(.., n) - the form the H2EK tool writes."""
    lay = _mcc_layouts(res)['stem']
    vlay = _vista_layouts(res)['stem']
    _fields_equal(vlay.fields, lay.fields, vlay.base_size or vlay.computed_size, lay.base_size or lay.computed_size, '')
    cl = _Cloner(res, 'stem', ref_groups={('/Postprocess Definition/Passes', 'Pass'): 'spas'})
    root = cl.clone(vista_root, lay.fields, lay.base_size or lay.computed_size, '')
    pad_postprocess(root)
    if lods is not None:
        root = cut_down_stem(root, lods)
    _report_missing(res, cl, 'stem')
    return root


def _copy_node(n):
    d = T.Node(n.group, n.path, n.fields, len(n.data), n.data)
    d.blocks = {o: list(v) for o, v in n.blocks.items()}
    d.datas = dict(n.datas)
    d.patches = dict(n.patches)
    return d


def cut_down_stem(root, lods=1):
    """The cut-down form of a stem that the H2EK tool writes (the kit's .shader_template tags hold every LOD, tool.exe compiles only the first
    one into the map): keep the first `lods` LODs, set the Projected Height of the last kept LOD to 0.0 (it then covers every distance), and keep
    the prefix of Layers / Passes / Implementations / Remappings those LODs reach.  Reproduces every distinct cut-down stem of the 24 H2EK-built
    maps that has a multi-LOD source: 50 Node-exact from the Vista tag, 9 (signature) from the restructured stock MCC tag (docs 2.3).  Returns a
    new root (the input is not modified)."""
    f = root.field('Postprocess Definition')
    pps = root.blocks.get(f.offset) or []
    if not pps:
        raise ShaderTagError('stem without a postprocess definition')
    out = _copy_node(root)
    p = _copy_node(pps[0])
    ll, ly, ps, im, rm = (p.blocks.get(o) or [] for o in (0x00, 0x08, 0x10, 0x18, 0x20))
    if lods < 1 or lods > len(ll):
        raise ShaderTagError('cut_down_stem: %d LODs requested, the stem has %d' % (lods, len(ll)))
    ll = [_copy_node(k) for k in ll[:lods]]
    ll[-1].set_f32(6, 0.0)

    def reach(vals, n):
        m = 0
        for v in vals:
            s, c = unpack_range(v)
            m = max(m, s + c)
        if m > n:
            raise ShaderTagError('cut_down_stem: index range beyond its block')
        return m
    ly = ly[:reach([k.u16(0) for k in ll], len(ly))]
    ps = ps[:reach([k.u16(0) for k in ly], len(ps))]
    im = im[:reach([k.u16(8) for k in ps], len(im))]
    rm = rm[:reach([k.u16(o) for k in im for o in (0, 2, 4)], len(rm))]
    for off, lst in ((0x00, ll), (0x08, ly), (0x10, ps), (0x18, im), (0x20, rm)):
        p.blocks[off] = list(lst)
        p.set_u32(off, len(lst))
    out.blocks[f.offset] = [p] + list(pps[1:])
    return out


def _report_missing(res, cl, group):
    if cl.missing_refs:
        for path, name, dat in cl.missing_refs[:25]:
            _warn(res, 'shader_tags_ref_unresolved', '%s%s %s: Vista datum 0x%08X has no MCC counterpart' % (group, path, name, dat))
        res_stats = getattr(res, 'stats', None)
        if res_stats is not None:
            try:
                res_stats['shader_tags_ref_unresolved'] += len(cl.missing_refs)
            except Exception:                            # noqa: BLE001
                pass


def port_vista_tag(res, group, vista_root, dxbc=None, **kw):
    if group == 'stem':
        return build_mcc_stem(res, vista_root)
    if group == 'spas':
        return build_mcc_spas(res, vista_root, dxbc, **kw)
    if group == 'vrtx':
        return build_mcc_vrtx(res, vista_root, dxbc, **kw)
    raise ShaderTagError('not a shader-template-family group: %r' % (group,))


# ================================================================================================
# what a compiler has to translate
# ================================================================================================
BlobJob = collections.namedtuple('BlobJob', 'group key kind slot profile vista_blob')


def needed_blobs(group, vista_root):
    """The Direct3D 9 blobs of a Vista tag that need a DXBC counterpart, as BlobJob(group, key, kind, slot, profile, vista_blob):
    spas key = (impl, ps, slot), kind 'ps', profile 'ps_2_0' / 'ps_2_0' / 'ps_3_0' for slots 0/1/2 (the Vista blobs' own version tokens);
    vrtx key = classification index, kind 'vs', profile 'vs_2_0'.  Empty Vista blobs are not listed."""
    jobs = []
    if group == 'spas':
        for (i, j), bl in sorted(vista_pixel_blobs(vista_root).items()):
            for k, b in enumerate(bl):
                if b:
                    jobs.append(BlobJob('spas', (i, j, k), 'ps', k, ('ps_2_0', 'ps_2_0', 'ps_3_0')[k], b))
    elif group == 'vrtx':
        for i, b in enumerate(vista_vertex_blobs(vista_root)):
            if b:
                jobs.append(BlobJob('vrtx', i, 'vs', 0, 'vs_2_0', b))
    return jobs


# ================================================================================================
# verification helpers
# ================================================================================================
def unpack_range(v):
    """Packed block range of the stem postprocess index fields: value = start + 512 * count (9-bit start, 7-bit count).  0x0800 = 4 elements from
    0, 0x3219 = 25 elements from 25, 0x0707 = 3 elements from 263 (the two stems with more than 256 Remappings need bit 8 of the start; the
    forward module shader_params._rng reads only 8 start bits).  -> (start, count)."""
    return v & 0x1FF, v >> 9


def pack_range(start, count):
    return (start & 0x1FF) | (count << 9)


def _range_ok(v, n):
    """A packed block range must lie inside a block of n elements."""
    s, c = unpack_range(v)
    return s + c <= n


def validate_stem(node):
    """Internal consistency of a (built or stock) stem: every packed index range stays inside the block it indexes (LOD -> Layers -> Passes ->
    Implementations -> Remappings), Pass references are set, three postprocess elements for an MCC tag.  Returns a list of problems."""
    probs = []
    pps = node.blocks.get(STEM_PP) or []
    if len(pps) not in (1, 3):
        probs.append('postprocess definitions: %d' % len(pps))
    if not pps:
        return probs
    if len(pps) == 3 and any(any(p.data) for p in pps[1:]):
        probs.append('postprocess [1]/[2] not zero')
    p = pps[0]
    lods, layers, passes, impls, rem = (p.blocks.get(o) or [] for o in (0x00, 0x08, 0x10, 0x18, 0x20))
    for i, k in enumerate(lods):
        if not _range_ok(k.u16(0), len(layers)):
            probs.append('LOD %d layers range 0x%04X > %d layers' % (i, k.u16(0), len(layers)))
    for i, k in enumerate(layers):
        if not _range_ok(k.u16(0), len(passes)):
            probs.append('layer %d passes range 0x%04X > %d passes' % (i, k.u16(0), len(passes)))
    for i, k in enumerate(passes):
        if not _range_ok(k.u16(8), len(impls)):
            probs.append('pass %d implementations range 0x%04X > %d' % (i, k.u16(8), len(impls)))
        g, d = _U32x2.unpack_from(k.data, 0)
        if d in (NULL, 0):
            probs.append('pass %d has a null pass reference' % i)
    for i, k in enumerate(impls):
        for off in (0, 2, 4):
            if not _range_ok(k.u16(off), len(rem)):
                probs.append('implementation %d range@%d 0x%04X > %d remappings' % (i, off, k.u16(off), len(rem)))
    return probs


def check_stem_pass_coupling(stem_node, impl_count_of):
    """The one verified stem <-> pass coupling: the implementation range of every `Passes[k]` of a stem has exactly as many elements as the referenced
    spas has Implementations (8630 / 8630 MCC and 8084 / 8084 Vista local pairs, 0 exceptions).  `impl_count_of(mcc_datum)` -> that count for the spas
    the (already remapped) reference points to, or None when unknown.  Returns [(pass index, variants in the stem, implementations of the pass)] of
    the mismatches - a stem that is paired with a re-authored pass (MCC_EDITED_SPAS) can be checked this way before it is written."""
    out = []
    pps = stem_node.blocks.get(STEM_PP) or []
    if not pps:
        return out
    for i, k in enumerate(pps[0].blocks.get(0x10) or []):
        g, d = _U32x2.unpack_from(k.data, 0)
        if d in (NULL, 0):
            continue
        n = impl_count_of(d)
        if n is not None and unpack_range(k.u16(8))[1] != n:
            out.append((i, unpack_range(k.u16(8))[1], n))
    return out


def validate_spas(node, allow_null_vs=False):
    """Consistency of a built spas: 3 postprocess elements ([1], [2] zero), every Pixel Shader element carries three DXBC blobs, handles zero,
    a Vertex Shader reference per implementation (allow_null_vs=True accepts none: the hud passes text_flash / shield_test have none in stock MCC
    and Vista), the Pixel Shader Index Block as long as the Pixel Shader block (stock rule)."""
    probs = []
    pps = node.blocks.get(SPAS_PP) or []
    if len(pps) != 3:
        probs.append('postprocess definitions: %d' % len(pps))
        if not pps:
            return probs
    elif any(any(p.data) for p in pps[1:]):
        probs.append('postprocess [1]/[2] not zero')
    for i, impl in enumerate(pps[0].blocks.get(SPAS_PP_IMPL) or []):
        g, d = _U32x2.unpack_from(impl.data, SPAS_IMPL_VERTEX_SHADER)
        if d in (NULL, 0) and not allow_null_vs:
            probs.append('implementation %d has no vertex shader reference' % i)
        ps = impl.blocks.get(SPAS_IMPL_PS) or []
        idx = impl.blocks.get(0x142) or []
        if idx and len(idx) != len(ps):
            probs.append('implementation %d: %d pixel shaders but a %d-entry index block' % (i, len(ps), len(idx)))
        for j, q in enumerate(ps):
            if any(q.data[o:o + 8] != b'\0' * 8 for o in M_PS_HANDLES):
                probs.append('impl %d ps %d: runtime handle not zero' % (i, j))
            for k in range(3):
                b = q.datas.get(M_PS_BLOBS[k]) or b''
                pr = check_dxbc(b, 'ps') if b else ['empty']
                if pr:
                    probs.append('impl %d ps %d slot %d: %s' % (i, j, k, '; '.join(pr)))
    return probs


def validate_vrtx(node):
    probs = []
    if node.blocks.get(M_VRTX_DX9) or node.blocks.get(M_VRTX_XBOX):
        probs.append('DX9 / Xbox1 lists must stay empty')
    for i, e in enumerate(node.blocks.get(M_VRTX_DX11) or []):
        b = e.datas.get(M_VRTX_EL_BLOB) or b''
        if b:
            pr = check_dxbc(b, 'vs')
            if pr:
                probs.append('classification %d: %s' % (i, '; '.join(pr)))
        if any(e.data[0:8]):
            probs.append('classification %d: Pointer not zero' % i)
    return probs


def stem_signature(node, name_of):
    """Datum-space independent structure of a stem (Vista or MCC Node): (LODs, Layers, Passes by NAME, Implementations, Remappings).
    name_of(datum) -> tag name (lower case) or None."""
    pps = node.blocks.get(STEM_PP) or []
    if not pps:
        return None
    p = pps[0]
    lods = tuple((k.i16(0), k.i32(2), round(k.f32(6), 6)) for k in (p.blocks.get(0) or []))
    layers = tuple(k.i16(0) for k in (p.blocks.get(8) or []))
    passes = tuple((name_of(k.u32(4)), k.i16(8)) for k in (p.blocks.get(0x10) or []))
    impls = tuple((k.i16(0), k.i16(2), k.i16(4)) for k in (p.blocks.get(0x18) or []))
    rem = tuple(bytes(k.data) for k in (p.blocks.get(0x20) or []))
    return (lods, layers, passes, impls, rem)


def stem_structure_diff(sig_a, sig_b):
    """[] when two stem signatures are equal, else the names of the differing parts (LODs, Layers, Passes, Implementations, Remappings)."""
    names = ('LODs', 'Layers', 'Passes', 'Implementations', 'Remappings')
    return [n for n, a, b in zip(names, sig_a, sig_b) if a != b]


def truncate_signature(sig, nlods):
    """stem_signature() of cut_down_stem(): the first `nlods` LODs (last one's projected height 0.0) and the prefix of the other parts they reach."""
    lods, layers, passes, impls, rem = sig
    lods = tuple(lods[:nlods])
    if not lods:
        return None
    lods = lods[:-1] + ((lods[-1][0], lods[-1][1], 0.0),)

    def reach(vals):
        m = 0
        for v in vals:
            s, c = unpack_range(v & 0xFFFF)
            m = max(m, s + c)
        return m
    layers = layers[:reach(l[0] for l in lods)]
    passes = passes[:reach(layers)]
    impls = impls[:reach(p[1] for p in passes)]
    rem = rem[:reach(v for i in impls for v in i)]
    return (lods, tuple(layers), tuple(passes), tuple(impls), tuple(rem))


def stem_is_cut_down_of(sig_cut, sig_full):
    """True when `sig_cut` is exactly the cut-down form (truncate_signature) of `sig_full` with fewer LODs - what the H2EK tool writes."""
    n = len(sig_cut[0])
    return n < len(sig_full[0]) and truncate_signature(sig_full, n) == sig_cut


def classify_spas(vista_node, vista_name_of, vista_text_of, mcc_node=None, mcc_name_of=None, mcc_text_of=None):
    """How a Vista pass relates to MCC's pass of the same name: 'absent' (mcc_node None: it must be ported), 'identical' (the MCC tag is the
    Vista tag with DXBC code: use the MCC donor), or 'differs' (MCC re-authored it; MCC_EDITED_SPAS says what changed and how many stock Vista-
    structured stems are known to work with it)."""
    if mcc_node is None:
        return 'absent'
    a = spas_structure_signature(vista_node, vista_name_of, vista_text_of)
    b = spas_structure_signature(mcc_node, mcc_name_of, mcc_text_of)
    return 'identical' if a == b else 'differs'


def spas_structure_signature(node, name_of, text_of):
    """Layout-agnostic structure of a spas (Vista or MCC Node), everything the stem and the engine depend on EXCEPT the code: per struct the
    bytes with pointer words / runtime handles cleared, tag references by NAME (name_of(datum) -> lower-case name), string ids by TEXT
    (text_of(sid)), and for a Pixel Shader element only its Constant Register Defaults.  Only Postprocess Definition [0] is looked at (the MCC
    padding elements are zero).  Two spas with equal signatures are interchangeable for a stem."""
    def rec(n, first_only):
        if n.path.endswith('/Pixel Shader'):
            f = n.field('Constant Register Defaults')
            return ('PS', tuple(k.i32(0) for k in (n.blocks.get(f.offset) or [])))
        d = bytearray(n.data)
        extra = []
        for f in n.fields:
            if f.type in T.BLOCK_TYPES:
                d[f.offset + 4:f.offset + 8] = b'\0\0\0\0'
                if first_only and f.name == 'Postprocess Definition':
                    d[f.offset:f.offset + 4] = b'\0\0\0\0'
            elif f.type in T.DATA_TYPES:
                d[f.offset + 4:f.offset + 8] = b'\0\0\0\0'
                extra.append(('data', f.offset, bytes(n.datas.get(f.offset) or b'')))
            elif f.type == 'tagref' and f.size == 8:
                dat = _U32.unpack_from(d, f.offset + 4)[0]
                extra.append(('ref', f.offset, name_of(dat) if dat not in (NULL, 0) else None))
                d[f.offset + 4:f.offset + 8] = b'\0\0\0\0'
            elif f.type == 'stringid':
                sid = _U32.unpack_from(d, f.offset)[0]
                extra.append(('sid', f.offset, text_of(sid) if sid else ''))
                d[f.offset:f.offset + 4] = b'\0\0\0\0'
        kids = []
        for off in sorted(n.blocks):
            fl = next((f for f in n.fields if f.offset == off), None)
            lst = n.blocks[off]
            if first_only and fl is not None and fl.name == 'Postprocess Definition':
                lst = lst[:1]
            kids.append((off, tuple(rec(k, False) for k in lst)))
        return (n.path, bytes(d), tuple(extra), tuple(kids))
    return rec(node, True)


def selftest(res=None):
    """Layout assertions behind the offset constants above.  Raises on mismatch; returns True."""
    if res is None:
        from . import transcode as X
        lm, lv = X.layouts('mcc'), X.layouts('vista')
    else:
        lm, lv = _mcc_layouts(res), _vista_layouts(res)

    def find(fields, name, off=None):
        for f in fields:
            if f.name == name and (off is None or f.offset == off):
                return f
        raise AssertionError('field %r@%r not found' % (name, off))
    v, m = lv['vrtx'], lm['vrtx']
    assert (v.base_size, m.base_size) == (0x10, 0x24)
    assert find(v.fields, 'Geometry Classifications').offset == V_VRTX_BLOCK and T.element_size(find(v.fields, 'Geometry Classifications')) == V_VRTX_EL
    assert find(m.fields, 'Geometry Classifications DX11').offset == M_VRTX_DX11 and T.element_size(find(m.fields, 'Geometry Classifications DX11')) == M_VRTX_EL
    assert find(m.fields, 'Output Swizzles Xbox1').offset == M_VRTX_SWZ_XBOX and find(v.fields, 'Output Swizzles').offset == V_VRTX_SWZ
    sv, sm = lv['spas'], lm['spas']
    pv = find(find(find(sv.fields, 'Postprocess Definition').children, 'Implementations').children, 'Pixel Shader')
    pm = find(find(find(sm.fields, 'Postprocess Definition').children, 'Implementations').children, 'Pixel Shader')
    assert (pv.offset, pm.offset) == (SPAS_IMPL_PS, SPAS_IMPL_PS)
    assert (T.element_size(pv), T.element_size(pm)) == (V_PS_EL, M_PS_EL)
    assert [c.offset for c in pv.children if c.type == 'dataref'] == list(V_PS_BLOBS)
    assert [c.offset for c in pm.children if c.type == 'dataref'] == list(M_PS_BLOBS)
    assert find(pv.children, 'Constant Register Defaults').offset == V_PS_DEFAULTS and find(pm.children, 'Constant Register Defaults').offset == M_PS_DEFAULTS
    assert find(find(sv.fields, 'Postprocess Definition').children, 'Implementations').children is not None
    impl = find(find(sm.fields, 'Postprocess Definition').children, 'Implementations')
    assert find(impl.children, 'Vertex Shader').offset == SPAS_IMPL_VERTEX_SHADER
    assert (lv['stem'].base_size, lm['stem'].base_size) == (0x60, 0x60)
    return True
