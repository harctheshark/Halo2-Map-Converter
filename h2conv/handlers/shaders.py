# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""shad handlers (+ vrtx/spas/stem donor fallbacks): MCC shader family -> Halo 2 Vista.

Background (measured on the stock maps)
  * MCC has no D3D9 bytecode: vrtx/spas/stem come from Vista tags of the same name (donor.py / planner link).
  * A shad's postprocess [0] holds (a) a SKELETON - Levels Of Detail / Layers / Passes / Implementations - that is
    a function of its stem (Vista corpus: 2807/2807 shaders have LOD layer ranges, Layers and Passes byte-equal to
    the stem's, and one Implementations entry per stem implementation) plus the shader's per-LOD flags and animation
    ranges, and (b) VALUE arrays (Bitmaps, Pixel/Vertex Constants, Overlays ..., Bitmap/Color/Value Properties) whose
    static part (Bitmaps, Pixel, Vertex, BitmapProps, ColorProps, ValueProps lengths) is a function of the stem.
  * The H2EK-built MCC stems are CUT-DOWN versions of the Vista stems (custom map: 24 of 93 are a pure prefix of Vista's -
    LOD >= 1 removed - and 8 more differ in passes), so an MCC skeleton cannot be paired with the Vista stem the output
    references, and the static arrays lost the constants of the removed passes.

What the 'shad' node handler does (after the generic conversion copied the struct and remapped ids)
  1. Shader LOD Bias 7 ('Lowest', MCC only) -> 4.
  2. Postprocess Definition: keep only element [0] (MCC elements [1]/[2] are the empty DX9/Xbox slots).
  3. Reconcile the postprocess with the Vista stem named by Template (tier code -> ctx.stats['shad_tier:<code>']):
        D  stem identical in both games                -> MCC postprocess unchanged
        K  MCC skeleton already mirrors the Vista stem -> kept (animation ranges stay valid), static arrays fixed by the edits below
        X  stem differs, not animated                  -> RE-SKELETON: LOD/Layers/Passes/Implementations rebuilt from the Vista stem
                                                          (LOD flags from MCC masked by the stem's availability, zero animation
                                                          ranges); MCC value arrays kept and aligned with the Vista stem's by the
                                                          verified per-stem edits in ARRAY_EDITS (drop / insert / tail-pad / swap)
        S  as X, animated, no Vista twin               -> animation arrays dropped (static shader)
        T  arrays do not fit (no verified edit), or an animated shader on a changed stem
                                                       -> postprocess [0] taken from the Vista twin (same name AND Template), else from
                                                          the stem's representative Vista shader (T-rep)
  3b. ENGINE PASS (_engine_fix, after every path above that produced Vista arrays): the stems' Remappings pair MCC array slots with Vista slots
      (same pass / implementation variant / register code); every paired MCC Bitmaps / Pixel / Vertex Constant is copied to its Vista slot
      whatever the learned maps or ARRAY_EDITS wrote there, and Vista-only slots whose parameter is a register role of the copied values
      (pixel-shader copies of the environment tints ...) get the same value.  Fixes the windownew / matte_grey_dlc_object / turf_door /
      pedestrian / roadsign / basic_metal_* defects.
  4. Bitmaps[].Log Bitmap Dimension recomputed where the OUTPUT bitmap's size differs from the one the stored value was computed for.
  5. Predicted Resources rebuilt (Vista rule) when the final Bitmaps[] names other tags than the hint list (non-D tiers).
Every decision is explained by ctx.warn codes 'shad_*'.  check_consistency() verifies a finished shad against its Vista stem.
"""
import math
import os
import struct

from . import node_handler, tag_handler
from .. import bitmaps as B
from .. import donor as D
from .. import shader_params as SPM
from .. import tagtree as T

NULL = 0xFFFFFFFF

# ---- shad layout (identical in MCC and Vista) --------------------------------------------
OFF_TEMPLATE = 0x00          # tagref (group, datum)
OFF_PP = 0x20                # tagblock Postprocess Definition (element 0x7C)
OFF_LOD_BIAS = 0x3C          # enum16 Shader LOD Bias
MCC_LOD_LOWEST = 7
VISTA_LOD_FALLBACK = 4       # '1/4 Size'

# ---- Added Depth Bias Offset (shad +0x54, float) : MCC unit -> Vista unit (pitscreen, 2026-10-20) --------------------------------------
# Both engines feed the tag value straight into the D3D9 render state DEPTHBIAS (id 195): Vista halo2.exe sub_669E9A
# `SetRenderState(195, shad+0x54 + extra)`, MCC halo2.dll sub_18095AB30 (same sum), and MCC sub_180961440 NEGATES it (reverse Z).  But the
# depth buffers differ: Vista = D24 forward Z, bias = absolute depth units (b); MCC = DXGI D32_FLOAT_S8X24 (dword_1819948A8 = 0x14, set only in
# sub_18095E8E0) reverse Z, D3D11 rasterizer DepthBias = int(b * (2^24 - 1)) (handler sub_180960AE0), and for a FLOAT depth buffer D3D11 scales it
# by 2^(exponent(depth) - 23), i.e. it is RELATIVE to the depth value d' = 1 - d_vista ~ n/z.  Pull toward the camera, world units, b = 0.001
# (cyberdyne_h2 monitor overlays), near 0.06 / far 1024 wu (Vista defaults dword_868150 / dword_868154):
#     z = 3 wu: MCC 0.005 wu, Vista 0.15 wu      z = 10: 0.013 vs 1.67      z = 30: 0.029 vs 15.0 wu (a different room's wall is no occluder).
# No single Vista value reproduces a distance-relative bias.  The rule used: the Vista value b*d'(Z_REF) never pulls farther than MCC does at any
# distance up to Z_REF (MCC's absolute depth bias is >= b*d' everywhere: 2*b*2^floor(log2 d') >= b*d').  Z_REF = 32 wu.  Only CONVERTED shader tags
# of H2EK-built inputs pass through here (tags linked to Vista's shared.map keep Vista's own values; stock Aspyr-built maps, header flag 0x08, are not
# touched so their conversions stay byte-identical).  Slope-scaled bias (+0x58) has the same meaning in both and is kept.
# H2CONV_DEPTH_BIAS_REF=<wu> overrides Z_REF, 0 keeps the MCC value unchanged (A/B builds).
# FLOOR: a converted magnitude never drops below 1e-6 (the smallest value stock Vista content uses on world surfaces, 17 capital_ship shaders: >= 16 D24
# steps at any distance) and never grows: tiny MCC decal biases (-5e-5 ammo decals) keep a usable separation instead of vanishing to ~1e-7.
OFF_DEPTH_BIAS = 0x54
DEPTH_NEAR, DEPTH_FAR, DEPTH_BIAS_Z_REF = 0.06, 1024.0, 32.0
DEPTH_BIAS_FLOOR = 1e-6


def depth_bias_vista(bias, fac):
    """MCC Added Depth Bias Offset -> Vista value: bias * fac, but not below DEPTH_BIAS_FLOOR in magnitude and never larger than the MCC value."""
    mag = min(abs(bias), max(abs(bias) * fac, DEPTH_BIAS_FLOOR))
    return math.copysign(mag, bias)


def _stock_built_input(ctx):
    """True for a map built by Aspyr's pipeline (header flag bit3 = textures.dat / sounds_*.dat, i.e. the stock MCC maps).  Their shader values are left
    exactly as before (stock conversions stay byte-identical: the lockout goldens); the rescale is for maps authored with the H2EK, whose values were
    tuned in the MCC engine."""
    try:
        return bool(ctx.src.header['flags'] & 0x0008)
    except Exception:                                              # noqa: BLE001
        return False


def depth_bias_factor(z_ref=None):
    """d'(z_ref) = 1 - d_vista(z_ref): the factor that turns an MCC Added Depth Bias Offset into the Vista value (1.0 = unchanged)."""
    if z_ref is None:
        v = os.environ.get('H2CONV_DEPTH_BIAS_REF', '').strip()
        z_ref = float(v) if v else DEPTH_BIAS_Z_REF
    if z_ref <= 0:
        return 1.0
    n, f = DEPTH_NEAR, DEPTH_FAR
    return n * f / ((f - n) * z_ref) - n / (f - n)

PP_STATIC = ('Bitmaps', 'Pixel Constants', 'Vertex Constants', 'Bitmap Properties', 'Color Properties', 'Value Properties')
PP_ANIM = ('Overlays', 'Overlay References', 'Animated Parameters', 'Animated Parameter References')
PP_SKELETON = ('Levels Of Detail', 'Layers', 'Passes', 'Implementations')


# ================================================================================================
# small node helpers
# ================================================================================================
def _blk(node, name):
    f = node.field(name)
    return node.blocks.get(f.offset, []) if f is not None else []


def _set_blk(node, name, kids):
    f = node.field(name)
    node.blocks[f.offset] = kids
    node.set_u32(f.offset, len(kids))                 # count word; the encoder rewrites the address word


def _new_child(parent, block_name, data):
    f = parent.field(block_name)
    return T.Node(parent.group, parent.path + '/' + block_name, f.children, len(data), data)


def _clone(node):
    n = T.Node(node.group, node.path, node.fields, len(node.data), node.data)
    n.src_addr = node.src_addr
    for off, kids in node.blocks.items():
        n.blocks[off] = [_clone(k) for k in kids]
    for off, payload in node.datas.items():
        n.datas[off] = payload
    n.patches = dict(node.patches)
    return n


def _shape(pp, names):
    return tuple(len(_blk(pp, n)) for n in names)


def _impl_entries(pp):
    return [tuple(k.u16(2 * i) for i in range(5)) for k in _blk(pp, 'Implementations')]


def _memo(ctx):
    ex = getattr(ctx, 'extra', None)
    if ex is None:
        ex = getattr(ctx, '_shaders_memo', None)
        if ex is None:
            ex = {}
            try:
                ctx._shaders_memo = ex
            except Exception:                                      # noqa: BLE001
                pass
    return ex.setdefault('shaders', {})


# ================================================================================================
# canonical forms (compare MCC and Vista trees by tag NAME and string TEXT)
# ================================================================================================
def canon(node, name_of, sid_of):
    d = bytearray(node.data)
    refs = []
    for f in node.fields:
        if f.type == 'tagref':
            off = f.offset + (4 if f.size == 8 else 0)
            refs.append((f.offset, name_of(struct.unpack_from('<I', d, off)[0])))
            d[f.offset:f.offset + f.size] = b'\0' * f.size
        elif f.type == 'stringid':
            refs.append((f.offset, sid_of(struct.unpack_from('<I', d, f.offset)[0])))
            d[f.offset:f.offset + 4] = b'\0\0\0\0'
        elif f.type in T.BLOCK_TYPES or f.type in T.DATA_TYPES:
            d[f.offset + 4:f.offset + 8] = b'\0\0\0\0'
    blocks = tuple((off, tuple(canon(k, name_of, sid_of) for k in kids)) for off, kids in sorted(node.blocks.items()))
    datas = tuple((off, bytes(p)) for off, p in sorted(node.datas.items()))
    return (bytes(d), tuple(refs), blocks, datas)


def _mcc_funcs(ctx):
    def name_of(datum):
        if datum in (NULL, 0):
            return None
        t = ctx.mcc_tag(datum)
        return (t.group, (t.name or '').lower()) if t is not None else ('?', datum)

    def sid_of(s):
        if s in (0, NULL):
            return ''
        try:
            return ctx.mcc_string(s)
        except Exception:                                          # noqa: BLE001
            return '<bad %08X>' % s
    return name_of, sid_of


def _vista_funcs(cache):
    def name_of(datum):
        lab = D.tag_label(cache, datum)
        if lab is None:
            return None if datum in (NULL, 0) else ('?', datum)
        return (lab[0], lab[1].lower())

    def sid_of(s):
        if s in (0, NULL):
            return ''
        try:
            return cache.string_id(s)
        except Exception:                                          # noqa: BLE001
            return '<bad %08X>' % s
    return name_of, sid_of


# ================================================================================================
# stems
# ================================================================================================
class StemInfo(object):
    __slots__ = ('name', 'donor', 'root', 'pp', 'same', 'm_missing', 'mcc_fp', 'vista_fp', 'mcc_tab', 'vista_tab', 'mcc_remap', 'vista_remap', 'corr')

    def __init__(self, name):
        self.name = name
        self.donor = None        # donor.Donor of the Vista stem
        self.root = None         # decoded Vista stem root Node
        self.pp = None           # its Postprocess Definition [0]
        self.same = False        # MCC stem == Vista stem (names/text-normalised)
        self.m_missing = False   # MCC stem not readable
        self.mcc_fp = None       # shader_params.stem_fingerprint of the MCC stem (selects the learned MCC parameter map)
        self.vista_fp = None     # ... and of the Vista stem
        self.mcc_tab = None      # shader_params.StemTab (pass table) of the MCC / Vista stem: animation arrays of a shader without Vista twin
        self.vista_tab = None
        self.mcc_remap = None    # shader_params.RemapTab (pass -> implementation -> register code -> array index) of the MCC / Vista stem
        self.vista_remap = None
        self.corr = None         # shader_params.stem_corr(mcc_remap, vista_remap): engine-level MCC array index -> Vista array index


def stem_info(ctx, m_template_datum, name):
    """-> StemInfo (cached per source map + stem name) or None when Vista has no stem of that name."""
    memo = _memo(ctx)
    key = ('stem', getattr(ctx, 'cur_src', 'src'), name.lower())
    if key in memo:
        return memo[key]
    dn = D.find(ctx, 'stem', name)
    if dn is None:
        memo[key] = None
        return None
    si = StemInfo(name)
    si.donor = dn
    si.root = ctx.decode_vista(dn.cache, dn.tag)
    vpps = _blk(si.root, 'Postprocess Definition')
    si.pp = vpps[0] if vpps else None
    try:
        mtag = ctx.mcc_tag(m_template_datum)
        mroot = ctx.decode_mcc(mtag, ctx.cur_cache) if hasattr(ctx, 'cur_cache') else ctx.decode_mcc(mtag)
        mpps = _blk(mroot, 'Postprocess Definition')
        mn, ms = _mcc_funcs(ctx)
        vn, vs = _vista_funcs(dn.cache)
        si.same = bool(mpps and vpps and canon(mpps[0], mn, ms) == canon(vpps[0], vn, vs))
        # stem version fingerprints (name based: pass tag names), used to pick the learned parameter maps (shader_params.py)
        si.mcc_fp = SPM.stem_fingerprint(mroot, lambda d: (mn(d) or ('', ''))[1])
        si.vista_fp = SPM.stem_fingerprint(si.root, lambda d: (vn(d) or ('', ''))[1])
        if mpps and vpps:
            si.mcc_tab = SPM.StemTab(mpps[0], lambda d: (mn(d) or ('', ''))[1])
            si.vista_tab = SPM.StemTab(vpps[0], lambda d: (vn(d) or ('', ''))[1])
            si.mcc_remap = SPM.RemapTab(mpps[0], lambda d: (mn(d) or ('', ''))[1])
            si.vista_remap = SPM.RemapTab(vpps[0], lambda d: (vn(d) or ('', ''))[1])
            si.corr = SPM.stem_corr(si.mcc_remap, si.vista_remap)
    except Exception as ex:                                        # noqa: BLE001
        si.m_missing = True
        ctx.warn('shad_mcc_stem_unreadable', '%s: %r' % (name, ex))
    memo[key] = si
    return si


# ================================================================================================
# Vista reference shaders: the twin (same name) and the representative of a stem
# ================================================================================================
def _stem_shaders(ctx):
    """{stem name.lower(): [Donor of every Vista shad using it]} (cached on the donor index)."""
    ix = D.get_index(ctx)
    m = getattr(ix, '_stem_shaders', None)
    if m is None:
        m = {}
        for d in ix.by_group.get('shad', ()):
            try:
                raw = d.cache.read_addr(d.tag.addr, 8)
            except Exception:                                      # noqa: BLE001
                continue
            _g, datum = struct.unpack('<II', raw)
            lab = D.tag_label(d.cache, datum)
            if lab is not None and lab[0] == 'stem':
                m.setdefault(lab[1].lower(), []).append(d)
        ix._stem_shaders = m
    return m


def _template_name(cache, tag):
    raw = cache.read_addr(tag.addr, 8)
    lab = D.tag_label(cache, struct.unpack('<II', raw)[1])
    return lab[1].lower() if lab and lab[0] == 'stem' else None


def representative(ctx, stem_name, limit=40):
    """The Vista shader that best represents a stem: first (shared-map-first order) shader with the majority static
    shape among up to `limit` shaders using it.  -> (Donor, pp Node) or None."""
    memo = _memo(ctx)
    key = ('rep', stem_name.lower())
    if key in memo:
        return memo[key]
    lst = _stem_shaders(ctx).get(stem_name.lower(), [])
    best = None
    if lst:
        import collections
        shapes = collections.OrderedDict()
        for d in lst[:limit]:
            root = ctx.decode_vista(d.cache, d.tag)
            pps = _blk(root, 'Postprocess Definition')
            if len(pps) != 1:
                continue
            sh = _shape(pps[0], PP_STATIC)
            shapes.setdefault(sh, []).append((d, pps[0]))
        if shapes:
            top = max(shapes.values(), key=len)
            best = top[0]
    memo[key] = best
    return best


def reference_for(ctx, stem_name, shad_name):
    """-> (Donor, pp Node, is_twin) or None.  The Vista twin (same shader name AND same Template) when there is one."""
    if shad_name:
        tw = None
        spec = getattr(getattr(ctx, 'cur_entry', None), 'twin', None)      # planner.PlanEntry.twin = (Vista map file, tag index)
        if spec:
            try:
                tw = D.open_donor(ctx, spec)
            except Exception:                                              # noqa: BLE001
                tw = None
        if tw is None:
            tw = D.find(ctx, 'shad', shad_name)
        if tw is not None and _template_name(tw.cache, tw.tag) == stem_name.lower():
            root = ctx.decode_vista(tw.cache, tw.tag)
            pps = _blk(root, 'Postprocess Definition')
            if len(pps) == 1:
                return tw, pps[0], True
    rep = representative(ctx, stem_name)
    if rep is not None:
        return rep[0], rep[1], False
    return None


# ================================================================================================
# re-skeleton
# ================================================================================================
# Learned edits between the MCC and the Vista static value arrays of a stem.  Source: a scan over the 387 custom +
# 400 stock MCC shaders that have a Vista twin with the same Template; only alignments that were EXACT (every other element
# bit-equal to the twin after the edit) are listed.  Anything not listed is NOT guessed (the shader takes tier T).
#   ('drop', k)    the MCC array has one element too many, at index k (tex_bump_env family: Pixel Constants[0]; 82 shaders)
#   ('insert', k)  the MCC array lacks the element the Vista stem's removed LOD needs, at index k (copied from the reference shader)
#   ('tail',)      the MCC array lacks trailing elements (the cut LODs' constants), copied from the reference shader
#   ('swap', i, j) / ('perm', p)   same length but reordered: tex_bump_env family Vertex Constants[0] <-> [2] (82 shaders, 0 counter-examples)
# MCC's tool cut LOD 1+ out of the stems (and the constants only those passes use); the stems listed with 'tail' are the ones
# where nothing else moved.  tex_bump_alpha_test / tex_env showed inconsistent positions (weak) and are deliberately absent.
# The table only decides LENGTHS and tiers (and the arrays when no stem correspondence exists); the VALUES of every slot that the MCC and Vista stems
# pair by register code are finally taken from the shader's own MCC arrays by the engine pass (_engine_fix), so a missing rule here (tex_detail_blend,
# 6 -> 8 Vertex Constants) no longer hands the shader a Vista reference shader's constants.
_P, _V, _B = 'Pixel Constants', 'Vertex Constants', 'Bitmaps'
_OP = 'shaders\\shader_templates\\opaque\\'
ARRAY_EDITS = {
    _OP + 'tex_bump_env': {_P: ('drop', 0), _V: ('swap', 0, 2)},
    _OP + 'tex_bump_env_clamped': {_P: ('drop', 0), _V: ('swap', 0, 2)},
    _OP + 'tex_bump_env_illum': {_P: ('drop', 0), _V: ('swap', 0, 2)},
    _OP + 'tex_bump_env_illum_combined': {_P: ('drop', 0), _V: ('swap', 0, 2)},
    # was (0, 2, 3, 1, 6, 7, 4, 5): learned from ONE shader with equal values and WRONG (it put the 16x16 detail tiling on the bump transform -
    # the chronopolis `corrugated` ramp lost its bump).  MCC VC = [detail, secondary, base, bump, c, c, spec, spec_gl], Vista VC =
    # [bump, base, detail, secondary, spec, spec_gl, c, c]; only reached when the learned maps are unusable.
    _OP + 'tex_bump_detail_blend': {_V: ('perm', (3, 2, 0, 1, 6, 7, 4, 5))},
    _OP + 'tex_bump_alpha_test_single_pass': {_B: ('insert', 4), _V: ('insert', 3)},
    _OP + 'tex_bump_dprs_env_illum': {_P: ('tail',), _V: ('tail',)},
    _OP + 'tex_bump_env_four_change_color': {_P: ('tail',), _V: ('tail',)},
    _OP + 'tex_bump_env_illum_3_channel': {_P: ('tail',), _V: ('tail',)},
    _OP + 'tex_bump_env_illum_two_change_color': {_P: ('tail',), _V: ('tail',)},
    _OP + 'tex_bump_env_two_change_color': {_P: ('tail',), _V: ('tail',)},
    _OP + 'tex_bump_env_two_change_color_indexed': {_P: ('tail',), _V: ('tail',)},
    _OP + 'tex_bump_four_change_color': {_V: ('tail',)},
    _OP + 'tex_bump_illum': {_V: ('tail',)},
    _OP + 'tex_bump_illum_bloom': {_V: ('tail',)},
    _OP + 'tex_bump_meter_illum': {_B: ('tail',), _P: ('tail',), _V: ('tail',)},
    _OP + 'tex_bump_plasma_one_channel_illum': {_V: ('tail',)},
    _OP + 'tex_bump_detail_keep': {_V: ('tail',)},
}


def static_fit(m_pp, r_pp, stem_name=None):
    """Can the MCC value arrays be used with the Vista stem whose shader R has the arrays r_pp?
    -> list of ops [('drop', array, k) | ('insert', array, k) | ('pad', array, n) | ('perm', array, p)] (empty = already equal) or None when they
    do not fit (a length difference that ARRAY_EDITS has no verified rule for)."""
    edits = ARRAY_EDITS.get((stem_name or '').lower(), {})
    ops = []
    for name in PP_STATIC:
        a, b = len(_blk(m_pp, name)), len(_blk(r_pp, name))
        ed = edits.get(name)
        if ed is not None and ed[0] in ('swap', 'perm'):
            perm = _perm_of(ed, a)
            if a == b and perm is not None:
                ops.append(('perm', name, perm))          # reordered constants (same length): applied before any other check
                continue
            if a != b or perm is None:
                return None
        if a == b:
            continue
        if ed is None:
            return None
        op = ed[0]
        if op == 'drop' and a == b + 1 and ed[1] < a:
            ops.append(('drop', name, ed[1]))
        elif op == 'insert' and a + 1 == b and ed[1] <= a:
            ops.append(('insert', name, ed[1]))
        elif op == 'tail' and a < b:
            ops.append(('pad', name, b - a))
        else:
            return None
    return ops


def _perm_of(ed, n):
    """('swap', i, j) / ('perm', (p0, p1, ...)) -> permutation list of length n (new[i] = old[perm[i]]) or None."""
    if ed[0] == 'swap':
        i, j = ed[1], ed[2]
        if not (0 <= i < n and 0 <= j < n):
            return None
        p = list(range(n))
        p[i], p[j] = p[j], p[i]
        return p
    p = list(ed[1])
    return p if len(p) == n and sorted(p) == list(range(n)) else None


def is_animated(m_pp):
    return any(any(e) for e in _impl_entries(m_pp))


def derive_skeleton(vpp, m_pp, v_stem_pp, animated_entries=None):
    """Rebuild vpp's LOD / Layers / Passes / Implementations from the Vista stem; LOD flags come from the MCC shader
    (masked with the stem's available layers; extra LODs inherit the previous LOD's flags & their own mask).
    Implementations: the MCC entries (animated_entries) or zeros, one per Vista stem implementation."""
    s_lod = _blk(v_stem_pp, 'Levels Of Detail')
    s_lay = _blk(v_stem_pp, 'Layers')
    s_pass = _blk(v_stem_pp, 'Passes')
    s_impl = _blk(v_stem_pp, 'Implementations')
    m_flags = [k.u32(0) for k in _blk(m_pp, 'Levels Of Detail')]
    lods = []
    flags = []
    for i, k in enumerate(s_lod):
        avail = k.u32(2)
        f = m_flags[i] if i < len(m_flags) else (flags[-1] if flags else 0)
        f &= avail
        flags.append(f)
        lods.append(_new_child(vpp, 'Levels Of Detail', struct.pack('<IH', f, k.u16(0))))
    _set_blk(vpp, 'Levels Of Detail', lods)
    _set_blk(vpp, 'Layers', [_new_child(vpp, 'Layers', struct.pack('<H', k.u16(0))) for k in s_lay])
    _set_blk(vpp, 'Passes', [_new_child(vpp, 'Passes', struct.pack('<H', k.u16(8))) for k in s_pass])
    ents = animated_entries or []
    impls = []
    for j in range(len(s_impl)):
        e = ents[j] if j < len(ents) else (0, 0, 0, 0, 0)
        impls.append(_new_child(vpp, 'Implementations', struct.pack('<5H', *e)))
    _set_blk(vpp, 'Implementations', impls)


def skeleton_matches(m_pp, v_stem_pp):
    """True when the MCC shader's own skeleton already mirrors the Vista stem (same LOD layer ranges, Layers, Passes and
    one Implementations entry per stem implementation): nothing needs rebuilding and animation ranges stay valid."""
    s_lod, s_lay, s_pass, s_impl = (_blk(v_stem_pp, n) for n in PP_SKELETON)
    lod, lay, pas, imp = (_blk(m_pp, n) for n in PP_SKELETON)
    return (len(lod) == len(s_lod) and all(a.u16(4) == b.u16(0) for a, b in zip(lod, s_lod))
            and [k.u16(0) for k in lay] == [k.u16(0) for k in s_lay]
            and [k.u16(0) for k in pas] == [k.u16(8) for k in s_pass]
            and len(imp) == len(s_impl))


def apply_array_ops(ctx, vpp, r_pp, ref_cache, ops, label=None):
    """Execute static_fit's ops on vpp.  Elements taken from the reference shader are cloned and, if they reference tags
    (Bitmaps), remapped to the output by name."""
    for op, name, x in ops:
        cur = list(_blk(vpp, name))
        if op == 'perm':
            cur = [cur[i] for i in x]
        elif op == 'drop':
            del cur[x]
        elif op == 'insert':
            new = _clone(_blk(r_pp, name)[x])
            D.remap_tree(ctx, new, ref_cache, label)
            cur.insert(x, new)
        else:                                               # pad: append the reference's trailing elements
            tail = [_clone(k) for k in _blk(r_pp, name)[len(cur):]]
            for t in tail:
                D.remap_tree(ctx, t, ref_cache, label)
            cur.extend(tail)
        _set_blk(vpp, name, cur)


def drop_animation(vpp):
    """Make the shader static: empty animation arrays, no animated-parameter index on any bitmap property."""
    for name in PP_ANIM:
        _set_blk(vpp, name, [])
    for k in _blk(vpp, 'Bitmap Properties'):
        k.set_u16(2, 0xFFFF)                               # Animated Parameter Index = none


def make_static(pp):
    """drop_animation + the five per-implementation animation ranges back to 0 (the state of the 2199 / 2807 non-animated Vista shaders)"""
    drop_animation(pp)
    for k in _blk(pp, 'Implementations'):
        for i in range(5):
            k.set_u16(2 * i, 0)


# ================================================================================================
# Log Bitmap Dimension
# ================================================================================================
# the formula lives in h2conv/bitmaps.py:
log_bitmap_dimension = B.log_bitmap_dimension          # ln(max(w, h)) * ln(2)
bitmap_log_dimension = B.bitmap_log_dimension          # + the rasterizer\default_2d quirk (0.0)


def _plan_entry(ctx, group, name):
    m = _memo(ctx).get('plan_entries')
    if m is None:
        m = {}
        plan = getattr(ctx, 'plan', None)
        for e in (getattr(plan, 'entries', None) or ()):
            if e.name:
                m.setdefault((e.group, e.name.lower()), e)
        _memo(ctx)['plan_entries'] = m
    return m.get((group, name.lower()))


def _vista_bitmap_dims(cache, tag):
    root = cache.read_addr(tag.addr, 0x50)
    cnt, addr = struct.unpack_from('<iI', root, 0x44)
    out = []
    for i in range(max(cnt, 0)):
        w, h = struct.unpack('<hh', cache.read_addr(addr + i * 0x74 + 4, 4))
        out.append((w, h))
    return out


def _mcc_bitmap_dims(cache, tag):
    root = cache.read_at_addr(tag.addr, 0x50)
    cnt, addr = struct.unpack_from('<iI', root, 0x44)
    out = []
    for i in range(max(cnt, 0)):
        w, h = struct.unpack('<hh', cache.read_at_addr(addr + i * 0xA8 + 4, 4))
        out.append((w, h))
    return out


def bitmap_dims(ctx, name):
    """[(w, h)] of the bitm tag the OUTPUT will contain under this name: converted from MCC -> MCC sizes, linked or donated
    from Vista -> Vista sizes.  None when unknown."""
    memo = _memo(ctx)
    key = ('dims', name.lower())
    if key in memo:
        return memo[key]
    res = None
    try:
        e = _plan_entry(ctx, 'bitm', name)
        use_mcc = e is not None and e.action == 'convert'
        if use_mcc:
            cache = ctx.cache_for(e.src_map)
            res = _mcc_bitmap_dims(cache, cache.tags[e.src_index])
        else:
            dn = D.find(ctx, 'bitm', name)
            if dn is not None:
                res = _vista_bitmap_dims(dn.cache, dn.tag)
            elif e is None:
                t = ctx.src.tag_by_name('bitm', name) if hasattr(ctx, 'src') else None
                if t is not None and t.has_data:
                    res = _mcc_bitmap_dims(ctx.src, t)
    except Exception as ex:                                        # noqa: BLE001
        ctx.warn('shad_bitmap_dims', '%s: %r' % (name, ex))
    memo[key] = res
    return res


def mcc_bitmap_dims(ctx, name):
    """[(w, h)] of the MCC bitm tag called `name` (input map first, then the MCC shared maps), None when absent."""
    memo = _memo(ctx)
    key = ('mdims', name.lower())
    if key in memo:
        return memo[key]
    res = None
    caches = [getattr(ctx, 'src', None)]
    ms = getattr(ctx, 'mcc_shared', None)
    for k in ('shared', 'single_player_shared', 'mainmenu'):
        caches.append(ms.get(k) if ms is not None else None)
    for c in caches:
        if c is None:
            continue
        try:
            t = c.tag_by_name('bitm', name)
            if t is not None and t.has_data:
                res = _mcc_bitmap_dims(c, t)
                break
        except Exception:                                          # noqa: BLE001
            continue
    memo[key] = res
    return res


def _baseline_dims(ctx, name, donor_cache):
    """The bitmap size the STORED Log Bitmap Dimension was computed for: the donor's own bitmap (donor import), else the MCC
    bitmap of that name, else the Vista one."""
    if donor_cache is not None:
        dn = D.get_index(ctx).find('bitm', name, prefer=os.path.basename(donor_cache.path))
        return _vista_bitmap_dims(dn.cache, dn.tag) if dn is not None else None
    m = mcc_bitmap_dims(ctx, name)
    if m:
        return m
    dn = D.find(ctx, 'bitm', name)
    return _vista_bitmap_dims(dn.cache, dn.tag) if dn is not None else None


DEFAULT_2D_NAME = 'rasterizer\\default_2d'
LOG_DIM_TOLERANCE = 5e-3        # a kept Log Bitmap Dimension within this of ln(max(w,h))*ln(2) is left alone


def fix_log_dims(ctx, shad_root, donor_cache=None):
    """Bitmaps[].Log Bitmap Dimension must describe the bitmap the OUTPUT contains.  A stored value is kept when that bitmap
    has the size the value was computed for (MCC's own bitmap, or the donor's) and recomputed (ln(max(w,h))*ln(2)) when the
    output bitmap differs - e.g. the shader links a Vista bitmap of another resolution.  Keeping unchanged values preserves the
    tool's rasterizer/default_2d quirk (0.0 mostly, 0.9609 in 27 Vista shaders).
    A kept value is still checked against the formula for the OUTPUT bitmap (tolerance LOG_DIM_TOLERANCE): the Vista twin's value can
    be stale for the author's bitmap - e.g. rasterizer\\linear_corner_fade is 16x16 in every shipped map (1.922) but 256x256 in the
    author's kit tags (3.844), and the twin keeps 1.922 although the baseline (the author's own bitmap) equals the output.
    The formula holds for 2,190 of 2,190 MCC and 1,435 of 1,435 Vista shared references; default_2d and null refs are never touched."""
    n = 0
    for pp in _blk(shad_root, 'Postprocess Definition'):
        for k in _blk(pp, 'Bitmaps'):
            lab = D.out_label(ctx, k.u32(0))
            if lab is None or lab[0] != 'bitm':
                continue
            dims = bitmap_dims(ctx, lab[1])
            idx = k.i32(4)
            if not dims or not (0 <= idx < len(dims)):
                continue
            base = _baseline_dims(ctx, lab[1], donor_cache)
            if base and idx < len(base) and base[idx] == dims[idx]:
                if lab[1].lower() == DEFAULT_2D_NAME:
                    continue                                   # the tool's null bitmap keeps whatever the twin stores (0.0 / 0.9609)
                want = bitmap_log_dimension(lab[1], *dims[idx])
                if abs(k.f32(8) - want) <= LOG_DIM_TOLERANCE:
                    continue                                   # consistent already (float noise only)
            k.set_f32(8, bitmap_log_dimension(lab[1], *dims[idx]))
            n += 1
    ctx.stats['shad_log_dims_recomputed'] += n
    return n


@D.post_hook('shad')
def _donor_shad_hook(ctx, root, cache, entry):
    """Donor-imported shad: Vista data is consistent already; only the bitmap sizes may differ in the output."""
    fix_log_dims(ctx, root, cache)


# ================================================================================================
# the node handler
# ================================================================================================
def _shad_name(ctx, mcc_node):
    ent = getattr(ctx, 'cur_entry', None)
    if ent is not None and getattr(ent, 'name', None):
        return ent.name
    cache = getattr(ctx, 'cur_cache', None) or getattr(ctx, 'src', None)
    a = getattr(mcc_node, 'src_addr', None)
    if cache is not None and a:
        try:
            t = cache.owner_of(a)
            if t is not None and t.addr == a:
                return t.name
        except Exception:                                          # noqa: BLE001
            pass
    return None


def _import_pp_from(ctx, donor, pp, label):
    """Copy a Vista pp Node from a reference shader into the output: ids remapped by name / text, bitmap sizes fixed later."""
    new = _clone(pp)
    D.remap_tree(ctx, new, donor.cache, label)
    return new


# ================================================================================================
# tier T-sib: animated shader without a Vista twin, but with an MCC SIBLING that has one
# ================================================================================================
# Found in game (test of a custom headlong): scenarios\objects\solo\earthcity\shaders\
# adprojector_bldgfacade_new (the projected ad on the headlong BSP facade) has no Vista twin, so tier S dropped its
# overlays / animated parameters and the ad rendered as missing.  Its MCC sibling adprojector_bldgfacade (same stem
# tex_bump_env_illum_3_channel_combined) HAS a Vista twin, and the two MCC shaders are identical in skeleton, animation and
# static shapes - only Color Properties / Value Properties values differ.  So: take the sibling's Vista postprocess (a valid
# animated D3D9 skeleton for the output stem) and transfer OUR differing elements index-by-index.
_SIB_SAME = PP_SKELETON + PP_ANIM


def _sibling_candidates(ctx, t_datum):
    memo = _memo(ctx)
    cache = getattr(ctx, 'cur_cache', None) or ctx.src
    key = ('sib', id(cache), t_datum)
    if key in memo:
        return memo[key]
    out = []
    for t in cache.tags:
        if t.group != 'shad' or not getattr(t, 'has_data', False) or not t.name:
            continue
        try:
            root = ctx.decode_mcc(t, cache)
        except Exception:                                                  # noqa: BLE001
            continue
        if root.u32(OFF_TEMPLATE + 4) != t_datum:
            continue
        pps = _blk(root, 'Postprocess Definition')
        if pps:
            out.append((t.name, pps[0]))
    memo[key] = out
    return out


def sibling_twin(ctx, m_pp, t_datum, stem_name, name):
    """-> (Donor, Vista pp of the sibling's twin, sibling MCC pp, sibling name) or None."""
    best = None
    for sib_name, s_mpp in _sibling_candidates(ctx, t_datum):
        if name and sib_name.lower() == name.lower():
            continue
        if _shape(s_mpp, PP_STATIC) != _shape(m_pp, PP_STATIC):
            continue
        ok = True
        for nm in _SIB_SAME:
            a, b = _blk(m_pp, nm), _blk(s_mpp, nm)
            if len(a) != len(b) or not all(T.same_tree(x, y)[0] for x, y in zip(a, b)):
                ok = False
                break
        if not ok:
            continue
        tw = D.find(ctx, 'shad', sib_name)
        if tw is None or _template_name(tw.cache, tw.tag) != stem_name.lower():
            continue
        r_pps = _blk(ctx.decode_vista(tw.cache, tw.tag), 'Postprocess Definition')
        if len(r_pps) != 1:
            continue
        ndiff = sum(1 for nm in PP_STATIC for x, y in zip(_blk(m_pp, nm), _blk(s_mpp, nm)) if not T.same_tree(x, y)[0])
        if best is None or ndiff < best[0]:
            best = (ndiff, tw, r_pps[0], s_mpp, sib_name)
    return best[1:] if best else None


def transfer_sibling_values(new_pp, our_vpp, m_pp, s_mpp):
    """In place on new_pp (the imported Vista pp of the sibling's twin): every static-array element where OUR MCC shader differs
    from the sibling's is replaced by our generic-converted element (ids already remapped).  Returns the number of elements
    transferred, or None when an array cannot be mapped index-by-index (lengths differ between MCC and the Vista twin)."""
    moved = 0
    for nm in PP_STATIC:
        ours_m, sib_m = _blk(m_pp, nm), _blk(s_mpp, nm)
        diff = [i for i, (x, y) in enumerate(zip(ours_m, sib_m)) if not T.same_tree(x, y)[0]]
        if not diff:
            continue
        tw, ours_v = list(_blk(new_pp, nm)), _blk(our_vpp, nm)
        if len(tw) != len(ours_m) or len(ours_v) != len(ours_m):
            return None
        for i in diff:
            if len(ours_v[i].data) != len(tw[i].data):
                return None
            tw[i] = _clone(ours_v[i])
            moved += 1
        _set_blk(new_pp, nm, tw)
    return moved


# ================================================================================================
# by-name array remap (shader_params.py): MCC arrays -> Vista arrays through the learned parameter maps
# ================================================================================================
def _short(stem_name):
    return (stem_name or '').replace('shaders\\shader_templates\\', '')


def _record_path(ctx, name, path, fp):
    """remember which array path a shader took (ctx.extra['shad_params'][name] = (path, MCC stem fingerprint)); read by the tests and the reports"""
    ex = getattr(ctx, 'extra', None)
    if ex is not None and name:
        ex.setdefault('shad_params', {})[name.lower()] = (path, fp)


def byname_plan(ctx, si, stem_name, name):
    """-> (mcc map, vista map) when the by-name path may be used for this shader, else None (ctx.stats / ctx.warn say why)."""
    if si is None or si.mcc_fp is None:
        ctx.stats['shad_params:none:no_mcc_fingerprint'] += 1
        return None
    e = SPM.template_entry(stem_name)
    why = None
    if not e or 'vista' not in e:
        why = 'no learned Vista map'
    else:
        mm = (e.get('mcc') or {}).get(str(si.mcc_fp))
        if mm is None:
            why = 'MCC stem version %s was never seen by the learner' % si.mcc_fp
        elif e['vista'].get('fp') != si.vista_fp:
            why = 'Vista stem version %s differs from the learned %s' % (si.vista_fp, e['vista'].get('fp'))
        elif SPM.usable(stem_name, si.mcc_fp) is None:
            why = mm.get('use_reason') or 'learned from too few samples (%d MCC / %d Vista)' % (mm.get('n', 0), e['vista'].get('n', 0))
    if why:
        ctx.stats['shad_params:fallback'] += 1
        ctx.stats['shad_params:fallback:' + _short(stem_name)] += 1
        ctx.warn('shad_params_fallback', '%s: %s - using the edit table / Vista twin' % (_short(stem_name), why), name)
        _record_path(ctx, name, 'edits', si.mcc_fp)
        return None
    return SPM.usable(stem_name, si.mcc_fp)


def _pp_arrays(pp):
    return {'bitmaps': [(k.u32(0), k.i32(4), struct.unpack_from('<f', k.data, 8)[0]) for k in _blk(pp, 'Bitmaps')],
            'pc': [bytes(k.data[:4]) for k in _blk(pp, 'Pixel Constants')],
            'vc': [struct.unpack_from('<4f', k.data) for k in _blk(pp, 'Vertex Constants')],
            'bprops': [(k.u16(0), k.u16(2)) for k in _blk(pp, 'Bitmap Properties')]}


class _RefBitmap(object):
    """placeholder for a reference-shader Bitmaps element; resolved (tag id remapped into the output) only if it survives the remap, so
    reference bitmaps that the MCC shader replaces do not produce 'donor_ref_dropped' warnings"""
    __slots__ = ('i',)

    def __init__(self, i):
        self.i = i


def _ref_arrays(ctx, r_pp, ref_cache, label):
    """the reference shader's arrays; Bitmaps are _RefBitmap placeholders unless ref_cache is None (postprocess already in the output space)"""
    arr = _pp_arrays(r_pp)
    if ref_cache is not None:
        arr['bitmaps'] = [_RefBitmap(i) for i in range(len(arr['bitmaps']))]
    return arr


def _resolve_refs(ctx, out, r_pp, ref_cache, label):
    bm = []
    for x in out['bitmaps']:
        if isinstance(x, _RefBitmap):
            c = _clone(_blk(r_pp, 'Bitmaps')[x.i])
            D.remap_tree(ctx, c, ref_cache, label)
            x = (c.u32(0), c.i32(4), struct.unpack_from('<f', c.data, 8)[0])
        bm.append(x)
    out['bitmaps'] = bm


def _write_arrays(pp, out):
    _set_blk(pp, 'Bitmaps', [_new_child(pp, 'Bitmaps', struct.pack('<IiI', d, i, struct.unpack('<I', struct.pack('<f', ld))[0])) for d, i, ld in out['bitmaps']])
    _set_blk(pp, 'Pixel Constants', [_new_child(pp, 'Pixel Constants', bytes(x)) for x in out['pc']])
    _set_blk(pp, 'Vertex Constants', [_new_child(pp, 'Vertex Constants', struct.pack('<4f', *x)) for x in out['vc']])
    _set_blk(pp, 'Bitmap Properties', [_new_child(pp, 'Bitmap Properties', struct.pack('<HH', a, b)) for a, b in out['bprops']])


def apply_byname(ctx, plan, tgt_pp, mcc_pp, r_pp, ref_cache, stem_name, is_twin, animated, label):
    """Compute the Vista arrays from the MCC shader's arrays (mcc_pp = the generic-converted postprocess, ids already in the output space)
    and the reference shader's (r_pp), write Bitmaps / Pixel / Vertex Constants / Bitmap Properties into tgt_pp (and, for a static shader
    whose target is not mcc_pp itself, the template-level Color / Value Properties).  -> stats dict or None (nothing was changed)."""
    mm, vm = plan
    ref_bm = _blk(r_pp, 'Bitmaps')

    def is_null(el):
        """null / rasterizer\\default_2d placeholder bitmap (the games spell 'no bitmap' differently; both render the same)"""
        if isinstance(el, _RefBitmap):
            datum, cache = ref_bm[el.i].u32(0), ref_cache
            lab = D.tag_label(cache, datum) if cache is not None else D.out_label(ctx, datum)
        else:
            datum = el[0]
            lab = D.out_label(ctx, datum)
        return datum in (NULL, 0) or lab is None or (lab[1] or '').lower() == 'rasterizer\\default_2d'

    out = SPM.remap_arrays(mm, vm, _pp_arrays(mcc_pp), _ref_arrays(ctx, r_pp, ref_cache, label), template=stem_name, ref_is_twin=is_twin,
                           animated_from_ref=animated, is_null=is_null)
    if out is None:
        ctx.stats['shad_params:fallback'] += 1
        ctx.stats['shad_params:fallback:' + _short(stem_name)] += 1
        ctx.warn('shad_params_fallback', '%s: the reference shader does not have the learned Vista array shape %s' % (_short(stem_name), vm['shape'][:3]), label)
        return None
    if ref_cache is not None:
        _resolve_refs(ctx, out, r_pp, ref_cache, label)
    _write_arrays(tgt_pp, out)
    if tgt_pp is not mcc_pp and not animated and mm.get('properties') == vm.get('properties'):
        for nm in ('Color Properties', 'Value Properties'):
            _set_blk(tgt_pp, nm, [_clone(k) for k in _blk(mcc_pp, nm)])
    ctx.stats['shad_params:byname'] += 1
    ctx.stats['shad_params:byname:' + _short(stem_name)] += 1
    _record_path(ctx, label, 'byname', mm.get('fp'))
    ctx.stats['shad_params:written'] += out['stats'].get('wrote', 0)
    ctx.stats['shad_params:kept_reference'] += out['stats'].get('missing', 0)
    return out['stats']


# ================================================================================================
# engine-level pass (shader_params.stem_corr / engine_apply): runs after EVERY path that produced Vista arrays
# ================================================================================================
def _roles(ctx, si, side, stem_name, shape):
    """slot roles (shader_params.slot_roles) of the MCC ('m') or Vista ('v') side of a stem, cached per stem and array shape"""
    memo = _memo(ctx)
    key = ('roles', side, getattr(ctx, 'cur_src', 'src'), stem_name.lower(), tuple(shape))
    if key in memo:
        return memo[key]
    e = SPM.template_entry(stem_name) or {}
    if side == 'v':
        body, tab, fp = e.get('vista'), si.vista_remap, si.vista_fp
        if body is not None and body.get('fp') != fp:
            body = None
    else:
        body, tab, fp = (e.get('mcc') or {}).get(str(si.mcc_fp)), si.mcc_remap, si.mcc_fp
    if body is not None and body.get('n', 0) < SPM.MIN_SAMPLES:
        body = None
    res = SPM.slot_roles(body, tab, shape)
    memo[key] = res
    return res


def _is_null_el(ctx, el):
    """Bitmaps element (datum, index, log dim) of the OUTPUT that means 'no bitmap': null or rasterizer\\default_2d"""
    datum = el[0]
    lab = D.out_label(ctx, datum)
    return datum in (NULL, 0) or lab is None or (lab[1] or '').lower() == 'rasterizer\\default_2d'


def _engine_fix(ctx, si, stem_name, tgt_pp, mcc_arr, label):
    """Final pass on the Vista arrays in tgt_pp: every MCC Bitmaps / Pixel / Vertex Constant that the stems pair with a Vista slot (same pass,
    same implementation variant, same register code) is copied there, whatever the learned maps or the edit table did; Vista slots without an
    MCC counterpart that carry a parameter the copied slots hold (register roles) get its value.  Fixes the 'learned constant' holes
    and the arrays of the shaders on the fallback path (lengths that ARRAY_EDITS has no rule for)."""
    corr = getattr(si, 'corr', None)
    if corr is None or mcc_arr is None or si.vista_remap is None:
        ctx.stats['shad_engine:skipped'] += 1
        return None
    try:
        out = _pp_arrays(tgt_pp)
        mroles = _roles(ctx, si, 'm', stem_name, (len(mcc_arr['pc']), len(mcc_arr['vc'])))
        vroles = _roles(ctx, si, 'v', stem_name, (len(out['pc']), len(out['vc'])))
        st = SPM.engine_apply(corr, mroles, vroles, mcc_arr, out, is_null=lambda el: _is_null_el(ctx, el))
        changed = sum(st.get(k, 0) for k in ('bm_changed', 'pc_changed', 'vc_changed', 'pc_filled', 'vc_filled'))
        if changed:
            _write_arrays(tgt_pp, out)
            ctx.stats['shad_engine:shaders_changed'] += 1
            ex = getattr(ctx, 'extra', None)
            if ex is not None and label:
                ex.setdefault('shad_engine', {})[label.lower()] = dict(st)
        ctx.stats['shad_engine:shaders'] += 1
        for k, v in st.items():
            ctx.stats['shad_engine:' + k] += v
        # diagnostic: a Bitmap Property that points at a slot the stems say is another one
        bad = 0
        for (mbi, _ma), (obi, _oa) in zip(mcc_arr['bprops'], out['bprops']):
            if mbi != 0xFFFF and obi != 0xFFFF and mbi in corr['bm'] and corr['bm'][mbi] != obi:
                bad += 1
        if bad:
            ctx.stats['shad_engine:bprop_differs_from_stems'] += bad
            ctx.warn('shad_engine_bprop', '%d Bitmap Properties point at another bitmap slot than the stem correspondence says' % bad, label)
        return st
    except Exception as ex:                                        # noqa: BLE001  (a polish pass must never lose the shader: keep the arrays as they are)
        ctx.stats['shad_engine:failed'] += 1
        ctx.warn('shad_engine_failed', '%s: %r - arrays left as the previous steps produced them' % (_short(stem_name), ex), label)
        return None


def reconcile(ctx, mcc_root, vis_root, name):
    """Steps 2 and 3 of the module docstring.  Returns the tier code."""
    mpps = _blk(mcc_root, 'Postprocess Definition')
    vpps = _blk(vis_root, 'Postprocess Definition')
    if not mpps or not vpps:
        return 'none'
    if any(any(p.data) for p in mpps[1:]):
        ctx.warn('shad_extra_pp', 'MCC postprocess definitions [1]/[2] are not empty - dropped', name)
    vpp = vpps[0]
    _set_blk(vis_root, 'Postprocess Definition', [vpp])
    m_pp = mpps[0]
    mcc_arr = _pp_arrays(vpp)         # the shader's own MCC arrays (generic conversion: ids already in the output space), before any edit
    t_datum = mcc_root.u32(OFF_TEMPLATE + 4)
    if t_datum in (NULL, 0):
        return 'notemplate'
    mt = ctx.mcc_tag(t_datum)
    if mt is None or not mt.name:
        ctx.warn('shad_template_unresolved', 'Template datum 0x%08X' % t_datum, name)
        return 'unresolved'
    stem_name = mt.name
    si = stem_info(ctx, t_datum, stem_name)
    # make sure both Template references point at the output stem (the generic remap may have dropped it)
    od = D.out_datum(ctx, 'stem', stem_name)
    if od != NULL:
        if vis_root.u32(OFF_TEMPLATE + 4) == NULL:
            vis_root.set_u32(OFF_TEMPLATE + 4, od)
        if vpp.u32(0) == NULL:
            vpp.set_u32(0, od)
    if si is None:
        # A Template of NONE crashes the engine when the shader is drawn (halo2.exe sub_67FE30, cyberdyne_h2 sky, 2026-10-20), so a shader
        # on a user-made stem must always end up on a real Vista stem:
        if od != NULL:
            # 1. the planner redirected the MCC-only stem to a Vista stem with an IDENTICAL skeleton (planner._stem_substitute): the
            #    shader's own postprocess keeps its meaning (same Layers, Passes, Implementations and Remappings)
            lab = D.out_label(ctx, od)
            ctx.stats['shad_stem_substituted'] += 1
            ctx.warn('shad_stem_substituted', 'MCC-only stem %s: the Template is the structurally identical Vista stem %s; the MCC postprocess is kept' % (
                stem_name, lab[1] if lab else '0x%08X' % od), name)
            return 'sub'
        # 2. no identical Vista stem: take the postprocess (and Template) of the Vista shader of the same name, if the Vista maps have one
        tw = None
        spec = getattr(getattr(ctx, 'cur_entry', None), 'twin', None)
        if spec:
            try:
                tw = D.open_donor(ctx, spec)
            except Exception:                                              # noqa: BLE001
                tw = None
        if tw is None:
            tw = D.find(ctx, 'shad', name) if name else None
        if tw is not None:
            tname = _template_name(tw.cache, tw.tag)
            td = D.out_datum(ctx, 'stem', tname) if tname else NULL
            tpps = _blk(ctx.decode_vista(tw.cache, tw.tag), 'Postprocess Definition')
            if td != NULL and len(tpps) == 1:
                new_pp = _import_pp_from(ctx, tw, tpps[0], name)
                new_pp.set_u32(0, td)
                vis_root.blocks[OFF_PP] = [new_pp]
                vis_root.set_u32(OFF_TEMPLATE + 4, td)
                ctx.stats['shad_stem_from_twin'] += 1
                ctx.warn('shad_stem_from_twin', 'MCC-only stem %s has no identical Vista stem: postprocess and Template taken from the Vista shader %s '
                         '(stem %s); the MCC parameters of this shader are lost' % (stem_name, tw.tag.name, tname), name)
                return 'T-twin'
        ctx.stats['shad_stem_unresolved'] += 1
        ctx.warn('shad_no_vista_stem', 'no Vista stem named %s, none with the same skeleton and no Vista twin shader: the Template is NONE and the engine '
                 'crashes when this shader is drawn (invariants sh.null_template)' % stem_name, name)
        return 'nostem'
    if si.same:
        return 'D'
    ref = reference_for(ctx, stem_name, name)
    animated = is_animated(m_pp)
    if ref is None:
        ctx.warn('shad_no_reference', 'no Vista shader uses stem %s: skeleton rebuilt without a shape check' % stem_name, name)
        derive_skeleton(vpp, m_pp, si.pp)
        if animated:
            drop_animation(vpp)
        return 'X?'
    donor, r_pp, is_twin = ref
    # learned parameter maps (shader_params.py): the arrays are remapped BY PARAMETER NAME whenever both stems have a trusted map and the
    # reference has the learned Vista array shape; otherwise the edit table (ARRAY_EDITS) / tier T below decide as before
    plan = byname_plan(ctx, si, stem_name, name)
    if plan is not None and _shape(r_pp, PP_STATIC[:3]) != tuple(plan[1]['shape'][:3]):
        ctx.stats['shad_params:fallback'] += 1
        ctx.stats['shad_params:fallback:' + _short(stem_name)] += 1
        ctx.warn('shad_params_fallback', '%s: the reference shader %s does not have the learned Vista array shape' % (_short(stem_name), donor.tag.name), name)
        plan = None
    if animated and not is_twin:
        sib = sibling_twin(ctx, m_pp, t_datum, stem_name, name)
        if sib is not None:
            s_donor, s_rpp, s_mpp, sib_name = sib
            new_pp = _import_pp_from(ctx, s_donor, s_rpp, name)
            splan = plan if plan is not None and _shape(s_rpp, PP_STATIC[:3]) == tuple(plan[1]['shape'][:3]) else None
            if splan is not None:
                moved = 1 if apply_byname(ctx, splan, new_pp, vpp, new_pp, None, stem_name, False, True, name) is not None else None
            else:
                moved = transfer_sibling_values(new_pp, vpp, m_pp, s_mpp)
            if moved is not None:
                vis_root.blocks[OFF_PP] = [new_pp]
                _engine_fix(ctx, si, stem_name, new_pp, mcc_arr, name)
                ctx.warn('shad_postprocess_from_sibling', 'animated shader without a Vista twin: postprocess taken from the Vista '
                         'twin of its MCC sibling %s (identical skeleton+animation), %s' % (sib_name, 'values remapped by parameter name'
                                                                                           if splan is not None else '%d differing value elements transferred' % moved), name)
                return 'T-sib'
    ops = [] if plan is not None else static_fit(m_pp, r_pp, stem_name)
    # a drop / insert shifts array indices that animation ranges and animated-parameter references use: not for animated shaders
    structural = ops is not None and any(o[0] != 'pad' for o in ops)
    tier = None
    if ops is not None and not (animated and structural):
        if skeleton_matches(m_pp, si.pp):
            tier = 'K'                                  # the MCC skeleton (and animation ranges) already fit this Vista stem
        elif not animated:
            derive_skeleton(vpp, m_pp, si.pp)
            tier = 'X'
        elif not is_twin:
            pred = None
            if plan is not None and si.mcc_tab is not None and si.vista_tab is not None:
                # animation of a shader without Vista twin: overlays / overlay references / animated parameters are stem independent;
                # the per-implementation ranges and the animated parameter references are rebuilt for the Vista stem (shader_params.py)
                pred = SPM.predict_animation(_impl_entries(m_pp), [(k.u16(0), k.data[2], k.data[3]) for k in _blk(m_pp, 'Animated Parameter References')],
                                             si.vista_tab, si.mcc_tab)
            if pred is not None:
                derive_skeleton(vpp, m_pp, si.pp, animated_entries=pred[0])
                _set_blk(vpp, 'Animated Parameter References', [_new_child(vpp, 'Animated Parameter References', struct.pack('<HBB', *r)) for r in pred[1]])
                tier = 'A'
                ctx.stats['shad_params:animation_remapped'] += 1
                ctx.warn('shad_animation_remapped', 'animated shader on a re-skeletoned stem without a Vista twin: animation kept, implementation ranges and '
                         'animated parameter references rebuilt for the Vista stem (%d references)' % len(pred[1]), name)
            else:
                derive_skeleton(vpp, m_pp, si.pp)
                drop_animation(vpp)
                tier = 'S'
                ctx.warn('shad_animation_dropped', 'animated shader on a re-skeletoned stem without a Vista twin: animation arrays dropped', name)
    if tier is not None:
        if plan is not None:
            apply_byname(ctx, plan, vpp, vpp, r_pp, donor.cache, stem_name, is_twin, False, name)
        else:
            ctx.stats['shad_params:edits'] += 1
            apply_array_ops(ctx, vpp, r_pp, donor.cache, ops, name)
        _engine_fix(ctx, si, stem_name, vpp, mcc_arr, name)
        return tier
    # tier T: take the reference shader's postprocess (a valid skeleton for the Vista stem) ...
    new_pp = _import_pp_from(ctx, donor, r_pp, name)
    # ... then put OUR values back wherever an array lines up (tier T used to keep the reference's arrays wholesale, which gave
    # custom shaders another shader's textures - found on a custom chronopolis: parking_level*, windownew, turf_door ...)
    if plan is not None and apply_byname(ctx, plan, new_pp, vpp, new_pp, None, stem_name, is_twin, True, name) is not None:
        moved = ['by parameter name']
    else:
        ctx.stats['shad_params:edits'] += 1
        moved = merge_own_arrays(ctx, new_pp, vpp, stem_name, animated)
    vis_root.blocks[OFF_PP] = [new_pp]
    if not animated and is_animated(new_pp):
        # the MCC shader is static but the imported reference (typically the stem's representative shader) is animated: its overlays / animated
        # parameters would make the converted shader flicker with another shader's functions (chronopolis `pedestrian` got honor_guard's tint animation)
        make_static(new_pp)
        ctx.stats['shad_reference_animation_dropped'] += 1
        ctx.warn('shad_reference_animation_dropped', 'the Vista %s %s is animated, this shader is not: animation arrays and ranges dropped' % (
            'twin' if is_twin else 'representative shader', donor.tag.name), name)
    _engine_fix(ctx, si, stem_name, new_pp, mcc_arr, name)
    ctx.warn('shad_postprocess_from_vista', 'postprocess skeleton taken from the Vista %s %s (%s); own arrays restored: %s' % (
        'twin' if is_twin else 'representative shader', donor.tag.name,
        'arrays do not fit' if ops is None else 'animated shader on a changed stem', ', '.join(moved) or 'none'), name)
    return 'T' if is_twin else 'T-rep'


def merge_own_arrays(ctx, new_pp, our_vpp, stem_name, animated):
    """In place on new_pp (an imported reference postprocess): replace static value arrays by the shader's OWN converted arrays
    (our_vpp = the generic-converted MCC postprocess, ids already remapped to the output) where they line up:
      * same length                      -> ours (after a verified 'swap'/'perm' edit for that array, if any)
      * a verified per-array edit fits   -> ours with that edit (drop / insert from the reference / tail from the reference)
      * Bitmaps shorter than the ref's   -> our elements + the reference's tail (the cut LODs' extra bitmaps come last)
      * anything else                    -> the reference's array is kept
    Animated shaders keep the reference's Bitmap/Color/Value Properties (they index the reference's animation arrays); only
    Bitmaps, Pixel and Vertex Constants are restored for them.  Returns the names of the restored arrays."""
    edits = ARRAY_EDITS.get((stem_name or '').lower(), {})
    names = ('Bitmaps', 'Pixel Constants', 'Vertex Constants') if animated else PP_STATIC
    moved = []
    for nm in names:
        mine = [_clone(k) for k in _blk(our_vpp, nm)]
        ref = list(_blk(new_pp, nm))
        if not mine or not ref:
            continue
        if any(len(a.data) != len(ref[0].data) for a in mine):
            continue
        ed = edits.get(nm)
        if ed is not None and ed[0] in ('swap', 'perm'):
            p = _perm_of(ed, len(mine))
            if p is None or len(mine) != len(ref):
                continue
            mine = [mine[i] for i in p]
        out = None
        if len(mine) == len(ref):
            out = mine
        elif ed is not None and ed[0] == 'drop' and len(mine) == len(ref) + 1 and ed[1] < len(mine):
            out = mine[:ed[1]] + mine[ed[1] + 1:]
        elif ed is not None and ed[0] == 'insert' and len(mine) + 1 == len(ref) and ed[1] <= len(mine):
            out = mine[:ed[1]] + [ref[ed[1]]] + mine[ed[1]:]
        elif len(mine) < len(ref) and ((ed is not None and ed[0] == 'tail') or nm == 'Bitmaps'):
            out = mine + ref[len(mine):]
        if out is not None:
            _set_blk(new_pp, nm, out)
            moved.append(nm)
            ctx.stats['shad_array_restored:%s' % nm] += 1
        else:
            ctx.stats['shad_array_kept_ref:%s' % nm] += 1
    return moved


def sync_predicted_resources(ctx, vis_root):
    """root Predicted Resources {type, resource index, tag datum}: prefetch hints for the shader's bitmaps.  Rule (Vista corpus: the
    SET of hinted bitmap tags equals the set of Bitmaps[] groups in 2807/2807 shaders; the exact list = the unique non-null
    (group, Bitmap Index) pairs of Bitmaps[] in order in 2305/2807, the rest add further indices of animated bitmaps).
    When the final Bitmaps[] (after re-skeleton / padding / twin import) names other tags than the list hints, rebuild the list
    by that rule; otherwise leave it alone.  Returns True when rebuilt."""
    pps = _blk(vis_root, 'Postprocess Definition')
    pred = _blk(vis_root, 'Predicted Resources')
    if len(pps) != 1:
        return False
    want = []
    for k in _blk(pps[0], 'Bitmaps'):
        d, i = k.u32(0), k.i32(4)
        if d not in (NULL, 0) and (0, i, d) not in want:
            want.append((0, i, d))
    have = set(k.u32(4) for k in pred if k.u32(4) not in (NULL, 0))
    if have == set(d for _t, _i, d in want):
        return False
    kids = [_new_child(vis_root, 'Predicted Resources', struct.pack('<HHI', t, i & 0xFFFF, d)) for t, i, d in want]
    _set_blk(vis_root, 'Predicted Resources', kids)
    ctx.stats['shad_predicted_rebuilt'] += 1
    return True


@node_handler('shad', '', prio=150)
def shad_root(ctx, mcc_node, vis_node):
    # 1. Shader LOD Bias
    if vis_node.u16(OFF_LOD_BIAS) == MCC_LOD_LOWEST:
        vis_node.set_u16(OFF_LOD_BIAS, VISTA_LOD_FALLBACK)
        ctx.stats['shad_lod_bias_7_to_4'] += 1
    name = _shad_name(ctx, mcc_node)
    # 1b. Added Depth Bias Offset: MCC float-depth (relative) unit -> Vista D24 (absolute) unit, see depth_bias_factor()
    bias = vis_node.f32(OFF_DEPTH_BIAS)
    fac = depth_bias_factor()
    if bias != 0.0 and fac != 1.0 and not _stock_built_input(ctx):
        nb = depth_bias_vista(bias, fac)
        if nb != bias:
            vis_node.set_f32(OFF_DEPTH_BIAS, nb)
            ctx.stats['shad_depth_bias_rescaled'] += 1
            ctx.warn('shad_depth_bias_rescaled', 'Added Depth Bias Offset %.3g (MCC float-depth unit) -> %.3g (Vista D24 unit, x%.3g, floor %g)' % (
                bias, nb, fac, DEPTH_BIAS_FLOOR), name)
    tier = reconcile(ctx, mcc_node, vis_node, name)
    ctx.stats['shad_tier:%s' % tier] += 1
    fix_log_dims(ctx, vis_node)
    if tier not in ('D', 'none', 'notemplate', 'unresolved', 'nostem', 'sub'):
        sync_predicted_resources(ctx, vis_node)


# ================================================================================================
# consistency check (used by tests and validate)
# ================================================================================================
def check_consistency(shad_root, stem_root, ref_pp=None):
    """-> list of problems of a Vista shad against its Vista stem: skeleton lists must mirror the stem, LOD flags must be
    a subset of the stem's available layers, implementation count must match; optionally the static shape of ref_pp."""
    probs = []
    pps = _blk(shad_root, 'Postprocess Definition')
    if len(pps) != 1:
        return ['postprocess count %d != 1' % len(pps)]
    pp = pps[0]
    sp = _blk(stem_root, 'Postprocess Definition')
    if len(sp) != 1:
        return ['stem has %d postprocess definitions' % len(sp)]
    sp = sp[0]
    s_lod, s_lay, s_pass, s_impl = (_blk(sp, n) for n in PP_SKELETON)
    lod, lay, pas, imp = (_blk(pp, n) for n in PP_SKELETON)
    if len(lod) != len(s_lod):
        probs.append('LOD count %d != stem %d' % (len(lod), len(s_lod)))
    else:
        for i, (a, b) in enumerate(zip(lod, s_lod)):
            if a.u16(4) != b.u16(0):
                probs.append('LOD %d layers index differs' % i)
            if a.u32(0) & ~b.u32(2):
                probs.append('LOD %d flags 0x%X outside the stem availability 0x%X' % (i, a.u32(0), b.u32(2)))
    if [k.u16(0) for k in lay] != [k.u16(0) for k in s_lay]:
        probs.append('Layers differ from the stem')
    if [k.u16(0) for k in pas] != [k.u16(8) for k in s_pass]:
        probs.append('Passes differ from the stem')
    if len(imp) != len(s_impl):
        probs.append('Implementations %d != stem %d' % (len(imp), len(s_impl)))
    if ref_pp is not None:
        for n in PP_STATIC:
            if len(_blk(pp, n)) != len(_blk(ref_pp, n)):
                probs.append('%s %d != reference %d' % (n, len(_blk(pp, n)), len(_blk(ref_pp, n))))
    return probs


# ================================================================================================
# vrtx / spas / stem: always the Vista tag of the same name
# ================================================================================================
def _donor_or_empty(ctx, entry):
    d = D.find(ctx, entry.group, entry.name)
    if d is not None:
        ctx.stats['shaderfam_donor:%s' % entry.group] += 1
        return D.import_donor(ctx, d, entry)
    ctx.warn('shaderfam_no_donor', '%s %s: no Vista tag of this name - emitting an empty tag (MCC carries DXBC only)' % (
        entry.group, entry.name), entry.name)
    from .. import transcode as X
    return X.empty_tag_node(ctx, entry.group)


@tag_handler('vrtx')
def vrtx_from_donor(ctx, entry):
    return _donor_or_empty(ctx, entry)


@tag_handler('spas')
def spas_from_donor(ctx, entry):
    return _donor_or_empty(ctx, entry)


@tag_handler('stem')
def stem_from_donor(ctx, entry):
    return _donor_or_empty(ctx, entry)
