# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""shad handler of the REVERSE direction: Halo 2 Vista shader (cache v8) -> MCC shader (cache v13) on a given MCC stem.

Why this module exists
  A compiled `shad` is a SKELETON (Levels Of Detail / Layers / Passes / Implementations + the five animation ranges per implementation)
  plus VALUE ARRAYS (Bitmaps, Pixel Constants, Vertex Constants, Bitmap/Color/Value Properties) that are laid out by the compiled stem
  (shader template) it was built against.  The generic reverse pass copies the Vista arrays onto the MCC struct position by position, which is
  right only where the MCC stem has the same layout as the Vista one (7,648 of 9,312 stock pairs).  Everywhere else a slot index means another
  parameter: the H2EK/MCC stems are cut-down Vista stems (tex_bump_env* moved two Vertex Constants and dropped Pixel Constant [0], illum*
  stems lost the lightmap passes ...), and on a SUBSTITUTED stem bitmaps land in the wrong sampler (the bump map in the albedo slot).

The mapping (nothing is guessed; every inference class is measured against the stock twins)
  * IDENTICAL STEMS (equal stem fingerprint): the postprocess is kept unchanged (tier D).  A stem the integrator PORTED from the Vista stem is
    identical by construction (register_ported_stem), so its shaders take this path with no learned data at all.
  * ENGINE PAIRING (shader_params.stem_corr, the function the forward direction uses): a compiled stem lists per pass implementation `Remappings`
    (register code -> array index).  The same pass tag (n-th occurrence), the same implementation variant and the same register code in the
    MCC stem and in the Vista stem are the same logical sampler / constant.  Reverse application: MCC.array[i] = Vista.array[corr[i]].
  * UNPAIRED MCC slots are filled in this order, each step only from verified evidence, and EVERY one is REPORTED (Report):
        bitmaps   learned parameter name on both sides (n >= 4 samples), then the trailing placeholder slots no register reads, in order;
        constants a register ROLE (pass, variant, code, component -> parameter; shader_params.code_roles + the learned solid components) that names
                  a parameter the Vista shader holds in a role-named slot -> that value ('role'; 'weak' when the Vista role was only weakly learned);
                  else a component the learned MCC map calls a constant (identical in every sample of that stem version, n >= 4) -> 'literal';
                  else the MCC template's parameter default for a parameter the Vista compile does not store -> 'default' (flagged);
                  else UNRESOLVED: the slot keeps a null / zero placeholder and is listed in the report - never a guess.
  * SKELETON from the target MCC stem (LOD layer ranges, Layers, Passes, Implementations); the LOD availability flags are the Vista shader's,
    masked with the MCC stem's availability; the animation ranges of an animated shader are rebuilt for the MCC implementations by
    shader_params.predict_animation and dropped - reported - when that is impossible.
  * Bitmap Properties are re-pointed through the same slot correspondence; Color / Value Properties are copied (identical property blocks).

Registered: @node_handler('shad', '') (the reverse registry; runs after the generic conversion, which already copied the struct and remapped
tag ids and string ids).  Nothing is registered for stem / spas / vrtx: those are the integrator's (MCC donors, or the port).
Integrator API:  register_ported_stem(ctx, vista_stem_name)   target_stem(ctx, vista_stem_name)   assess_pair(vsd, msd)   reverse_arrays(...)
Options (ctx.opts): rev_shad_disable (skip the handler: the previous, positional-copy behaviour, for A/B tests);
                    rev_shad_defaults (default True: a parameter the Vista compile does not store takes the MCC template default and is FLAGGED;
                    False leaves such components zero, flagged as unresolved).
                    rev_shad_stale_keep (default False: see "batchfix" below).
Reports: ctx.extra['rev_shad_report'][shader name lower] = Report.as_dict() + vista_stem / mcc_stem / stem_kind; ctx.stats['rev_shad_*'];
         warning codes rev_shad_unresolved / rev_shad_default / rev_shad_substitute_stem / rev_shad_substitute_same_structure / rev_shad_no_target_stem /
         rev_shad_no_vista_stem / rev_shad_animation_dropped / rev_shad_name_conflict / rev_shad_failed / rev_shad_stale_skeleton / rev_shad_stale_skeleton_kept.

Custom stems (a stem the input carries under a stock name):
  * target_stem(ctx, vname, vsd, vdatum): the shader's Template datum is resolved through the plan, so a shader on a stem the INPUT carries under a stock name
    (planner.custom_stem_entry: private port '<name>__<map>') takes that port as its target stem (kind 'ported', tier D) instead of MCC's namesake.
  * stale_skeleton(): a shader whose own skeleton is not its stem's (compiled against another revision of the template than the one in the map: cideral and
    nuclear, `tex_bump`) gets the stem's skeleton, its value arrays are copied verbatim - only when they have the stem's lengths and the shader is static.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import collections
import struct

from . import node_handler
from ... import bitmaps as B            # direction independent helpers (bitmap_log_dimension)
from ... import shader_params as SPM
from .. import shader_structure as SS     # register structure of the compiled stems
from ... import tagtree as T

NULL = 0xFFFFFFFF
NONE16 = 0xFFFF

# ---- shad layout (identical in both games) -------------------------------------------------------------
OFF_TEMPLATE = 0x00          # tagref (group, datum)
NULLISH = ('', 'rasterizer\\default_2d')
NULL_BITMAP = (NULL, 0, 0.0)


# ================================================================================================
# small node helpers (self-contained: the forward module is not imported)
# ================================================================================================
def _blk(node, name):
    f = node.field(name)
    return node.blocks.get(f.offset, []) if f is not None else []


def _set_blk(node, name, kids):
    f = node.field(name)
    node.blocks[f.offset] = kids
    node.set_u32(f.offset, len(kids))                 # count word; the encoder rewrites the address word


def _new_child(parent, block_name, payload):
    """a new element of block `block_name` of `parent`, its fixed struct = payload padded / cut to the layout's element size"""
    f = parent.field(block_name)
    size = T.element_size(f)
    data = bytearray(size)
    n = min(size, len(payload))
    data[:n] = payload[:n]
    return T.Node(parent.group, parent.path + '/' + block_name, f.children, size, data)


# ================================================================================================
# PART 1: pure data model (no Nodes beyond the two builders, no ctx): compiled stems and shaders as plain Python data
# ================================================================================================
class StemData(object):
    """Plain-data model of one compiled stem (shader template) of either game.  Picklable.
    name    template name (lower case)          fp     shader_params.stem_fingerprint (LOD, Layers, Passes by NAME, Implementations, Remappings)
    tab     shader_params.RemapTab (pass -> implementation -> (register code, array index) for Bitmaps / Pixel / Vertex Constants)
    stab    shader_params.StemTab (pass table for the animation ranges)
    lods    [(layer range u16, available layer flags u32)]   layers [u16]   passes [u16 implementation range]   n_impl
    """
    __slots__ = ('name', 'fp', 'tab', 'stab', 'lods', 'layers', 'passes', 'n_impl')

    def formula_shape(self):
        """(Bitmaps, Pixel Constants, Vertex Constants) array lengths implied by the registers: 1 + the highest index any register reads.  Equals the
        real array length of 97 % of the compiled shaders of both games; templates with a bitmap property placeholder (illum, two_alpha_env_illum,
        two_alpha_clouds, water_pool*, roadsign_glass ...) have ONE extra trailing Bitmaps slot that no register reads."""
        mx = {'bm': -1, 'pc': -1, 'vc': -1}
        for _nm, impls in self.tab.passes:
            for im in impls:
                if im is None:
                    continue
                for k in mx:
                    for _code, idx in im[k]:
                        mx[k] = max(mx[k], idx)
        return mx['bm'] + 1, mx['pc'] + 1, mx['vc'] + 1

    def fed(self, kind):
        """set of array indices some register of some pass implementation reads"""
        s = set()
        for _nm, impls in self.tab.passes:
            for im in impls:
                if im is not None:
                    for _code, idx in im[kind]:
                        s.add(idx)
        return s


def stem_data(stem_root, name_of, name=''):
    """stem root Node (either game) -> StemData, or None when the stem has no Postprocess Definition.  name_of(datum) -> tag name or None."""
    pps = _blk(stem_root, 'Postprocess Definition')
    if not pps:
        return None
    pp = pps[0]
    sd = StemData()
    sd.name = (name or '').lower()
    sd.fp = SPM.stem_fingerprint(stem_root, name_of)
    sd.tab = SPM.RemapTab(pp, name_of)
    sd.stab = SPM.StemTab(pp, name_of)
    sd.lods = [(k.u16(0), k.u32(2)) for k in _blk(pp, 'Levels Of Detail')]
    sd.layers = [k.u16(0) for k in _blk(pp, 'Layers')]
    sd.passes = [k.u16(8) for k in _blk(pp, 'Passes')]
    sd.n_impl = len(_blk(pp, 'Implementations'))
    return sd


def shader_arrays(pp):
    """shad Postprocess Definition [0] Node -> plain dict (the shape of shader_params.decode_compiled, without the Node).  Bitmaps elements are
    (tag datum, bitmap index, log dimension)."""
    return {
        'bitmaps': [(k.u32(0), k.i32(4), struct.unpack_from('<f', k.data, 8)[0]) for k in _blk(pp, 'Bitmaps')],
        'pc': [bytes(k.data[:4]) for k in _blk(pp, 'Pixel Constants')],
        'vc': [struct.unpack_from('<4f', k.data) for k in _blk(pp, 'Vertex Constants')],
        'bprops': [(k.u16(0), k.u16(2)) for k in _blk(pp, 'Bitmap Properties')],
        'cprops': [struct.unpack_from('<3f', k.data) for k in _blk(pp, 'Color Properties')],
        'vprops': [struct.unpack_from('<f', k.data)[0] for k in _blk(pp, 'Value Properties')],
        'lods': [(k.u32(0), k.u16(4)) for k in _blk(pp, 'Levels Of Detail')],
        'layers': [k.u16(0) for k in _blk(pp, 'Layers')],
        'passes': [k.u16(0) for k in _blk(pp, 'Passes')],
        'impls': [tuple(k.u16(2 * i) for i in range(5)) for k in _blk(pp, 'Implementations')],
        'aprefs': [(k.u16(0), k.data[2], k.data[3]) for k in _blk(pp, 'Animated Parameter References')],
    }


def is_animated(arr):
    return any(any(e) for e in arr['impls'])


def skeleton_fits(arr, sd):
    """True when the skeleton of a shader (shader_arrays() dict) is its stem's own: LOD layer ranges, Layers, Passes (implementation ranges), one
    Implementations entry per stem implementation.  Holds for 2807 / 2807 shaders of the 43 stock Vista maps."""
    return (len(arr['lods']) == len(sd.lods) and all(a[1] == b[0] for a, b in zip(arr['lods'], sd.lods)) and list(arr['layers']) == list(sd.layers)
            and list(arr['passes']) == list(sd.passes) and len(arr['impls']) == sd.n_impl)


def stale_skeleton(src, vsd, msd, vlearn=None, mlearn=None):
    """A shader whose OWN skeleton is not its stem's (it was compiled against another revision of the template than the one the map / MCC carries:
    cideral, nuclear - `tex_bump`; no stock shader does this, 2807 / 2807 fit) while its identical-stem tier would copy that skeleton verbatim.
    -> None when the skeleton fits (nothing to do), else (rebuild, text): rebuild=True when the arrays can safely be read through the stem's own registers -
    the Vista engine can only do so, the Remappings exist in the stem alone - i.e. the array lengths are those of the stem (learned shape of the stem, else
    the register formula) and the shader is not animated (no per-implementation animation ranges that belong to the old pass layout); the skeleton is then
    the stem's (derive_skeleton), the arrays stay verbatim.  rebuild=False (kept as found, flagged) otherwise."""
    if skeleton_fits(src, msd):
        return None
    n = (len(src['bitmaps']), len(src['pc']), len(src['vc']))
    full = n + (len(src['bprops']), len(src['cprops']), len(src['vprops']))
    def _rng(vals):
        return [(v & 0x1FF, v >> 9) for v in vals]                  # packed block range = start + 512 * count (shader_tags.unpack_range)
    nl_diff = sum(1 for a, b in zip(src['layers'], msd.layers) if a != b) + abs(len(src['layers']) - len(msd.layers))
    why = ['the shader skeleton is not the stem\'s: passes (start, count) %s against the stem\'s %s, %d of %d layer entries differ, %d / %d implementations' % (
        _rng(src['passes']), _rng(msd.passes), nl_diff, len(msd.layers), len(src['impls']), msd.n_impl)]
    if is_animated(src) or src['aprefs']:
        return False, why[0] + '; the shader is animated, the animation ranges belong to the old pass layout: kept as found'
    shape = None
    for lm in (vlearn, mlearn):
        if lm is not None and lm.get('shape'):
            shape = tuple(int(x) for x in lm['shape'])
            break
    if shape is not None:
        ok = (n == shape[:3]) and (len(shape) < 6 or full == shape[:6])
        ref = 'learned shape %s' % (list(shape),)
    else:
        fm = msd.formula_shape()
        ok = n in (fm, (fm[0] + 1, fm[1], fm[2]))
        ref = 'register formula %s' % (list(fm),)
    if not ok:
        return False, why[0] + '; the array lengths %s do not fit the stem (%s): kept as found' % (list(full), ref)
    return True, why[0] + '; the array lengths %s are the stem\'s (%s) and the shader is static: skeleton rebuilt from the stem, arrays copied verbatim' % (list(full), ref)


# ================================================================================================
# the mapping core (pure)
# ================================================================================================
class Report(object):
    """What reverse_arrays did to one shader (counts are slots for 'bm', components for 'pc' / 'vc')."""
    __slots__ = ('tier', 'paired', 'by_name', 'by_role', 'by_weak', 'by_literal', 'by_default', 'extras', 'unresolved', 'dropped', 'name_conflicts',
                 'shape', 'shape_src', 'animation', 'notes', 'prov', 'conflicts', 'by_tname', 'by_struct', 'by_layout', 'by_renum')

    def __init__(self):
        self.tier = None                           # 'D' identical stems, 'M' mapped
        self.paired = collections.Counter()        # 'bm' / 'pc' / 'vc' -> slots copied through the engine pairing
        self.by_name = 0                           # bitmap slots paired through the learned parameter name (n >= 4 on both sides)
        self.by_tname = 0                          # bitmap slots paired through the TEMPLATE-ORDER parameter name (slot i = bitmap parameter i of the template)
        self.by_struct = collections.Counter()     # components filled through the STRUCTURAL role of a bitmap-transform register (rank -> bitmap, mask -> component)
        self.by_layout = collections.Counter()     # components fixed by the register structure (pc byte no register delivers = 0; corpus-constant vc components)
        self.by_renum = collections.Counter()      # slots paired through a RENUMBERED register (one register per kind of a pass implementation changed its number)
        self.extras = 0                            # trailing placeholder bitmap slots paired in order
        self.by_role = collections.Counter()       # components filled through a register role / learned solid role
        self.by_weak = collections.Counter()       # ... through a weakly learned role (parameter never varied in the samples)
        self.by_literal = collections.Counter()    # components filled with a constant the learned MCC map calls constant (n >= 4)
        self.by_default = collections.Counter()    # components of a parameter the Vista compile does not store: MCC template default (FLAGGED)
        self.unresolved = []                       # [('bm', i) | ('pc', i, comp) | ('vc', i, comp)] with no evidence (FLAGGED)
        self.dropped = []                          # Vista parameter features / bitmap slots the MCC stem has no slot for
        self.name_conflicts = []                   # engine pairing contradicted by the learned parameter names (0 expected)
        self.conflicts = 0                         # stem_corr ambiguities (0 expected)
        self.shape = None
        self.shape_src = None
        self.animation = None                      # None | 'copied' | 'predicted' | 'dropped'
        self.notes = []
        self.prov = {}                             # ('bm', i) / ('pc'|'vc', i, comp) -> 'pair' | 'name' | 'tname' | 'extra' | 'role' | 'struct' | 'weak' | 'literal' | 'layout' | 'default' | 'unresolved'

    @property
    def exact(self):
        """no slot had to be inferred below the evidence of the stems themselves: nothing unresolved, no template default, animation kept"""
        return not self.unresolved and not self.by_default and self.animation != 'dropped' and not self.name_conflicts

    def as_dict(self):
        return {'tier': self.tier, 'paired': dict(self.paired), 'by_name': self.by_name, 'by_tname': self.by_tname, 'extras': self.extras, 'by_role': dict(self.by_role),
                'by_struct': dict(self.by_struct), 'by_layout': dict(self.by_layout), 'by_renum': dict(self.by_renum),
                'by_weak': dict(self.by_weak), 'by_literal': dict(self.by_literal), 'by_default': dict(self.by_default),
                'unresolved': list(self.unresolved), 'dropped': list(self.dropped), 'name_conflicts': list(self.name_conflicts),
                'shape': self.shape, 'shape_src': self.shape_src, 'animation': self.animation, 'notes': list(self.notes), 'exact': self.exact}


def _f32(v):
    return struct.unpack('<f', struct.pack('<f', v))[0]


def _byte(v):
    return int(round(max(0.0, min(1.0, v)) * 255))


def mcc_shape(msd, vsd, src, mlearn):
    """Array lengths of a shader compiled against the MCC stem `msd`.  The learned map of that exact stem version (n >= MIN_SAMPLES) states it;
    otherwise the register formula of the MCC stem plus the number of trailing placeholder slots the Vista shader has beyond the Vista formula.
    -> (bm, pc, vc, source label)"""
    if mlearn is not None and mlearn.get('n', 0) >= SPM.MIN_SAMPLES and mlearn.get('shape'):
        s = mlearn['shape']
        return int(s[0]), int(s[1]), int(s[2]), 'learned'
    fm = msd.formula_shape()
    fv = vsd.formula_shape()
    act = (len(src['bitmaps']), len(src['pc']), len(src['vc']))
    ex = tuple(max(0, a - f) for a, f in zip(act, fv))
    return fm[0] + ex[0], fm[1] + ex[1], fm[2] + ex[2], 'formula+extras'


def _comp_value(kind, arr, i, c):
    return arr[i][c] / 255.0 if kind == 'pc' else arr[i][c]


def _vista_known(src, vroles):
    """{feature: (value, exact, solid)} of every Vista pixel / vertex component that has a role (feature = (letter, parameter)).  Vertex Constant
    floats are exact, Pixel Constant bytes are quantised: the exact value wins when both exist; a SOLID role (learned from varying samples, or the
    register-role table) beats a WEAK one (a parameter that never varied in the learned samples: the Vista slot value is still the shader's value
    and equals the template default unless the shader overrides it)."""
    known = {}
    for kind in ('pc', 'vc'):
        rows = vroles[kind] if vroles else []
        for i, row in enumerate(rows):
            if i >= len(src[kind]):
                break
            for c, r in enumerate(row):
                if r is None:
                    continue
                f = r[0]
                solid = r[1] != 'weak'
                val = _comp_value(kind, src[kind], i, c)
                prev = known.get(f)
                if prev is None or (solid and not prev[2]) or (solid == prev[2] and kind == 'vc' and not prev[1]):
                    known[f] = (val, kind == 'vc', solid)
    return known


def derive_skeleton(src, vsd, msd, out, rep):
    """Skeleton of the MCC shader from the MCC stem: LOD layer ranges, Layers, Passes and one Implementations entry per stem implementation.
    The LOD availability flags are the Vista shader's, masked with the MCC stem's availability (an extra MCC LOD inherits the previous one's);
    the five animation ranges per implementation + the Animated Parameter References of an animated shader are rebuilt for the MCC
    implementations by shader_params.predict_animation, or dropped (reported) when that is impossible."""
    lods = []
    prev = 0
    for i, (lidx, avail) in enumerate(msd.lods):
        f = src['lods'][i][0] if i < len(src['lods']) else prev
        f &= avail
        prev = f
        lods.append((f, lidx))
    out['lods'] = lods
    out['layers'] = list(msd.layers)
    out['passes'] = list(msd.passes)
    out['aprefs'] = []
    if not is_animated(src):
        out['impls'] = [(0, 0, 0, 0, 0)] * msd.n_impl
        return True
    pred = SPM.predict_animation(src['impls'], src['aprefs'], msd.stab, vsd.stab)
    if pred is None:
        out['impls'] = [(0, 0, 0, 0, 0)] * msd.n_impl
        rep.animation = 'dropped'
        rep.notes.append('animation cannot be rebuilt for this stem: dropped')
        return False
    out['impls'] = [tuple(e) for e in pred[0]]
    out['aprefs'] = [tuple(r) for r in pred[1]]
    rep.animation = 'predicted'
    return True


def _bm_param(learned, i):
    """('p', parameter) / ('g', global bitmap path) of bitmap slot i of a learned map, else None"""
    if learned is None or i >= len(learned['bitmaps']):
        return None
    s = learned['bitmaps'][i]
    if s[0] == 'p':
        return ('p', s[1])
    if s[0] == 'w':
        return ('p', s[2])
    if s[0] == 'g':
        return ('g', s[1])
    return None


def _default_null(el):
    if el is None:
        return True
    n = el[0]
    if n is None or n == NULL or n == 0:
        return True
    return isinstance(n, str) and n.lower() in NULLISH


def _bm_names(params, n, learned=None):
    """Bitmaps[] slot -> template parameter name for a compiled shader of `n` bitmap slots: slot i is the i-th bitmap parameter of its template, the
    trailing placeholder slots have no name (None).  None when the template list does not fit the stem (more parameters than slots, or a TRUSTED learned
    map names a slot differently)."""
    if not params or len(params) > n:
        return None
    if learned is not None:
        for i in range(min(len(params), len(learned['bitmaps']))):
            p = _bm_param(learned, i)
            if p is not None and p[0] == 'p' and p[1] != params[i]:
                return None
    return list(params) + [None] * (n - len(params))


def _reg_class(kind, code):
    """what a register code says besides its number: pixel (mask index, function); vertex (bitmap-transform, component mask) / (other, code >> 4)"""
    if kind == 'pc':
        return ((code >> 5) & 7, code >> 8)
    k, _rank, mask, _reg = SS.decode_vc_code(code)
    return (k, mask if k == 'tex' else code >> 4)


def renumbered_pairs(mtab, vtab, corr):
    """{'pc': {MCC index: Vista index}, 'vc': {...}}: inside a pass (same tag, n-th occurrence) and implementation variant that both stems list, after the
    registers with equal codes are paired, exactly ONE register of a kind is left on each side and both belong to the same register class -> the pass
    renumbered that register (hud\\shield_test, remapping code 0x67 -> 0x66).  Ambiguous or conflicting proposals are dropped.
    Not part of stem_corr (the forward direction does not use it)."""
    pm = collections.defaultdict(list)
    pv = collections.defaultdict(list)
    for nm, impls in mtab.passes:
        pm[nm].append(impls)
    for nm, impls in vtab.passes:
        pv[nm].append(impls)
    cand = {'pc': collections.defaultdict(set), 'vc': collections.defaultdict(set)}
    for nm, ml in pm.items():
        vl = pv.get(nm)
        if not vl:
            continue
        for occ in range(min(len(ml), len(vl))):
            for a, b in zip(ml[occ], vl[occ]):
                if a is None or b is None:
                    continue
                for kind in ('pc', 'vc'):
                    ca = {c for c, _ in a[kind]}
                    cb = {c for c, _ in b[kind]}
                    ua = [(c, i) for c, i in a[kind] if c not in cb]
                    ub = [(c, i) for c, i in b[kind] if c not in ca]
                    if len(ua) == 1 and len(ub) == 1 and _reg_class(kind, ua[0][0]) == _reg_class(kind, ub[0][0]):
                        cand[kind][ua[0][1]].add(ub[0][1])
    out = {'pc': {}, 'vc': {}}
    for kind in ('pc', 'vc'):
        taken = set(corr[kind].values())
        for iM, s in cand[kind].items():
            if len(s) == 1 and iM not in corr[kind]:
                iV = next(iter(s))
                if iV not in taken:
                    out[kind][iM] = iV
        inv = collections.Counter(out[kind].values())
        out[kind] = {a: b for a, b in out[kind].items() if inv[b] == 1}
    return out


def reverse_arrays(src, vsd, msd, vlearn=None, mlearn=None, roles=None, base=None, mkbitmap=None, defaults=None, null_test=None, cache=None,
                   vparams=None, mparams=None, layout=None, synthetic=False):
    """Map the compiled arrays of a Vista shader onto the MCC stem `msd`.
      src      shader_arrays() dict of the Vista shader      vsd / msd   StemData of the Vista stem / the target MCC stem
      vlearn / mlearn   learned maps (shader_params.vista_map / mcc_map bodies; callers pass only maps with n >= MIN_SAMPLES) or None
      roles    the register-role table (shader_params.code_roles()) or None for the packaged one
      base     optional {'bitmaps','pc','vc'} reference arrays in the MCC layout whose values fill UNRESOLVED slots (default: null / zero)
      mkbitmap callable(global path) -> Bitmaps element for a learned global-bitmap slot (default: (path, 0, 0.0))
      defaults {(letter, parameter): value} template parameter defaults of the MCC template (shader_params.default_features(t, 'mcc')): the last
               resort for a component whose parameter the Vista compile does not store; always reported (Report.by_default)
      null_test callable(Bitmaps element) -> True for a null / rasterizer\\default_2d element (only used for the 'dropped' report)
      cache    optional dict reused across the shaders of one stem pair (stem_corr and the register roles)
      vparams / mparams  bitmap parameter names of the Vista / MCC TEMPLATE in template order (shader_structure.bitmap_param_names): with both, a bitmap slot the
               registers and the learned names do not pair is paired by its template-order name (provenance 'tname')
      layout   the register-layout tables (shader_structure.register_layout()): with it, a Vertex Constant that a bitmap-transform register names gets its
               structural role ('struct'), and a component the register structure fixes is filled with that constant ('layout'; pc byte no register
               delivers = 0).  None (default) keeps the previous behaviour.
      synthetic  True when `src` holds placeholder values (assess_pair): the value-dependent safety check of the code constants is skipped
    -> (out dict in the shape of shader_arrays(), Report)"""
    rep = Report()
    if msd.fp == vsd.fp:
        rep.tier = 'D'
        out = {k: (list(v) if isinstance(v, list) else v) for k, v in src.items()}
        rep.shape = (len(src['bitmaps']), len(src['pc']), len(src['vc']))
        rep.shape_src = 'identical stem'
        rep.animation = 'copied' if is_animated(src) else None
        return out, rep
    rep.tier = 'M'
    cache = {} if cache is None else cache
    roles = SPM.code_roles() if roles is None else roles
    mkbitmap = mkbitmap or (lambda path: (path, 0, 0.0))
    null_test = null_test or _default_null
    corr = cache.get('corr')
    if corr is None:
        corr = cache['corr'] = SPM.stem_corr(msd.tab, vsd.tab)
    rep.conflicts = corr.get('conflicts', 0)
    n_bm, n_pc, n_vc, rep.shape_src = mcc_shape(msd, vsd, src, mlearn)
    rep.shape = (n_bm, n_pc, n_vc)
    nv = {'bm': len(src['bitmaps']), 'pc': len(src['pc']), 'vc': len(src['vc'])}
    nm = {'bm': n_bm, 'pc': n_pc, 'vc': n_vc}
    base = base or {}
    prov = rep.prov
    out = {'bitmaps': [None] * n_bm, 'pc': [None] * n_pc, 'vc': [None] * n_vc}
    used_v = {'bm': set(), 'pc': set(), 'vc': set()}
    v2m = {}                                                       # Vista bitmap slot -> MCC bitmap slot

    # ---- 1. engine pairing: MCC index -> Vista index
    for kind, key in (('bm', 'bitmaps'), ('pc', 'pc'), ('vc', 'vc')):
        for iM, iV in sorted(corr[kind].items()):
            if iM < nm[kind] and iV < nv[kind]:
                out[key][iM] = src[key][iV]
                used_v[kind].add(iV)
                rep.paired[kind] += 1
                if kind == 'bm':
                    v2m[iV] = iM
                    prov[('bm', iM)] = 'pair'
                else:
                    for c in range(4):
                        prov[(kind, iM, c)] = 'pair'

    # ---- 1b. RENUMBERED registers (structure switch): the same pass implementation reads the same registers in both stems except ONE register per kind whose
    #          number changed (class = mask / function unchanged): the two are the same constant
    if layout is not None:
        rn = cache.get('renum')
        if rn is None:
            rn = cache['renum'] = renumbered_pairs(msd.tab, vsd.tab, corr)
        for kind in ('pc', 'vc'):
            for iM, iV in sorted(rn.get(kind, {}).items()):
                if iM < nm[kind] and iV < nv[kind] and out[kind][iM] is None and iV not in used_v[kind]:
                    out[kind][iM] = src[kind][iV]
                    used_v[kind].add(iV)
                    rep.by_renum[kind] += 1
                    for c in range(4):
                        prov[(kind, iM, c)] = 'renum'

    # ---- 2. bitmap slots the registers do not pair
    vparam = {}
    if vlearn is not None:
        for j in range(len(vlearn['bitmaps'])):
            p = _bm_param(vlearn, j)
            if p is not None:
                vparam.setdefault(p, []).append(j)
    for iM, iV in sorted(corr['bm'].items()):                      # learned names must agree with the engine pairing (informational)
        a, b = _bm_param(mlearn, iM), _bm_param(vlearn, iV)
        if a is not None and b is not None and a != b and a[0] == b[0] and iM < nm['bm'] and iV < nv['bm']:
            rep.name_conflicts.append(('bm', iM, iV, a, b))
    fed_m, fed_v = msd.fed('bm'), vsd.fed('bm')
    todo_m = [i for i in range(n_bm) if out['bitmaps'][i] is None]
    for i in list(todo_m):                                         # 2a. the learned parameter name (n >= 4 on both sides)
        p = _bm_param(mlearn, i)
        if p is None:
            continue
        if p[0] == 'g':
            out['bitmaps'][i] = mkbitmap(p[1])
            prov[('bm', i)] = 'name'
            rep.by_name += 1
            todo_m.remove(i)
            continue
        cands = [j for j in vparam.get(p, ()) if j < nv['bm'] and j not in used_v['bm']]
        if len(cands) == 1:
            out['bitmaps'][i] = src['bitmaps'][cands[0]]
            used_v['bm'].add(cands[0])
            v2m[cands[0]] = i
            prov[('bm', i)] = 'name'
            rep.by_name += 1
            todo_m.remove(i)
    mnames = _bm_names(mparams, n_bm, mlearn)                      # 2a'. the TEMPLATE-ORDER name: slot i of a compiled shader is the i-th bitmap parameter of
    vnames = _bm_names(vparams, nv['bm'], vlearn)                  #      its template (3,090 / 3,102 Vista and 3,205 / 3,218 MCC stock shaders)
    if mnames is not None and vnames is not None and todo_m:
        by_pname = {}
        for j, nme in enumerate(vnames):
            if nme is not None:
                by_pname.setdefault(nme, []).append(j)
        for i in list(todo_m):
            nme = mnames[i] if i < len(mnames) else None
            if nme is None:
                continue
            cands = [j for j in by_pname.get(nme, ()) if j not in used_v['bm']]
            if len(cands) == 1:
                out['bitmaps'][i] = src['bitmaps'][cands[0]]
                used_v['bm'].add(cands[0])
                v2m[cands[0]] = i
                prov[('bm', i)] = 'tname'
                rep.by_tname += 1
                todo_m.remove(i)
    ex_m = [i for i in todo_m if i not in fed_m]                   # 2b. trailing placeholder slots no register reads: in order, same count
    ex_v = [j for j in range(nv['bm']) if j not in fed_v and j not in used_v['bm']]
    if ex_m and len(ex_m) == len(ex_v):
        for i, j in zip(ex_m, ex_v):
            out['bitmaps'][i] = src['bitmaps'][j]
            used_v['bm'].add(j)
            v2m[j] = i
            prov[('bm', i)] = 'extra'
            rep.extras += 1
            todo_m.remove(i)
    if mnames is not None and defaults:                            # 2c. a bitmap parameter the Vista template does not have: the MCC template's default bitmap (FLAGGED placeholder)
        for i in list(todo_m):
            nme = mnames[i] if i < len(mnames) else None
            dp = defaults.get(('bm', nme)) if nme else None
            if dp:
                out['bitmaps'][i] = mkbitmap(dp)
                prov[('bm', i)] = 'default'
                rep.by_default['bm'] += 1
                todo_m.remove(i)
    for i in todo_m:                                               # unresolved bitmap slots: null placeholder (or the caller's base element)
        b = base.get('bitmaps')
        out['bitmaps'][i] = b[i] if b is not None and i < len(b) else NULL_BITMAP
        prov[('bm', i)] = 'unresolved'
        rep.unresolved.append(('bm', i))

    # ---- 3. pixel / vertex components the registers do not pair
    key_roles = ('roles', n_pc, n_vc, nv['pc'], nv['vc'], layout is not None, mnames is not None, vnames is not None)
    rr = cache.get(key_roles)
    if rr is None:
        rr = cache[key_roles] = (SS.slot_roles(mlearn, msd.tab, (n_pc, n_vc), roles, mnames, layout), SS.slot_roles(vlearn, vsd.tab, (nv['pc'], nv['vc']), roles, vnames, layout))
    mroles, vroles = rr
    known = _vista_known(src, vroles)
    vcodes = None
    if layout is not None:
        vcodes = cache.get('vcodes')                                # register code -> Vista Vertex Constant indices (the safety check of a code constant)
        if vcodes is None:
            vcodes = cache['vcodes'] = {}
            for (kd, ix), ks in vsd.tab.slot_keys().items():
                if kd == 'vc':
                    for _ps, _var, code in ks:
                        vcodes.setdefault(code, set()).add(ix)
    consumed = set()
    for kind in ('pc', 'vc'):
        for i in range(nm[kind]):
            if out[kind][i] is not None:
                continue
            row = []
            for c in range(4):
                r = mroles[kind][i][c] if i < len(mroles[kind]) else None
                lrn = mlearn[kind][i][c] if (mlearn is not None and i < len(mlearn[kind]) and c < len(mlearn[kind][i])) else None
                val = how = None
                if r is not None and r[0] in known:
                    val = known[r[0]][0]
                    how = 'struct' if r[1] == 'struct' else ('role' if known[r[0]][2] and r[1] != 'weak' else 'weak')
                    consumed.add(r[0])
                elif lrn is not None and lrn[0] == 'c':
                    val, how = float(lrn[1]) / (255.0 if kind == 'pc' else 1.0), 'literal'      # learned Pixel Constants are stored in byte units
                elif lrn is not None and lrn[0] == 'w' and (lrn[1], lrn[2]) in known:
                    val, how = known[(lrn[1], lrn[2])][0], 'weak'
                    consumed.add((lrn[1], lrn[2]))
                elif lrn is not None and lrn[0] == 'w' and len(lrn) > 3:
                    val, how = float(lrn[3]) / (255.0 if kind == 'pc' else 1.0), 'weak'
                if val is None and layout is not None:
                    lv = SS.slot_const(msd.tab, kind, i, c, layout)
                    if lv is not None and kind == 'vc' and vcodes and not synthetic:
                        for _ps, _var, code in msd.tab.slot_keys().get(('vc', i), ()):          # a code constant must not contradict the Vista shader's own slot of that code
                            for vj in vcodes.get(code, ()):
                                if vj < nv['vc'] and abs(src['vc'][vj][c] - lv) > 1e-3 * max(1.0, abs(lv)):
                                    lv = None
                    if lv is not None:
                        val, how = lv, 'layout'
                if val is None and defaults:
                    for feat in ((r[0],) if r is not None else ()) + (((lrn[1], lrn[2]),) if (lrn is not None and lrn[0] in ('f', 'w')) else ()):
                        if defaults.get(feat) is not None:
                            val, how = float(defaults[feat]), 'default'
                            break
                if val is None:
                    b = base.get(kind)
                    bv = 0.0
                    if b is not None and i < len(b):
                        bv = b[i][c] / 255.0 if kind == 'pc' else b[i][c]
                    val, how = bv, 'unresolved'
                    rep.unresolved.append((kind, i, c))
                else:
                    (rep.by_role if how == 'role' else rep.by_literal if how == 'literal' else rep.by_default if how == 'default' else
                     rep.by_struct if how == 'struct' else rep.by_layout if how == 'layout' else rep.by_weak)[kind] += 1
                prov[(kind, i, c)] = how
                row.append(val)
            out[kind][i] = bytes(_byte(v) for v in row) if kind == 'pc' else tuple(_f32(v) for v in row)

    # ---- 4. what the MCC stem has no slot for: Vista parameter features nobody received, Vista bitmaps nobody received
    delivered = set()
    for kind in ('pc', 'vc'):
        for j in used_v[kind]:
            if vroles and j < len(vroles[kind]):
                for r in vroles[kind][j]:
                    if r is not None:
                        delivered.add(r[0])
    for f in sorted(set(known) - delivered - consumed):
        if layout is not None and f[0] in ('tx', 'ty', 'tz') and abs(known[f][0]) < 1e-12:
            continue                                               # a zero translation: the MCC slot (no translation positions) draws it identically - nothing is lost
        rep.dropped.append(('param', f[0], f[1]))
    for j in range(nv['bm']):
        if j not in used_v['bm'] and not null_test(src['bitmaps'][j]):
            p = _bm_param(vlearn, j)
            rep.dropped.append(('bitmap', j, p[1] if p else None))

    # ---- 5. Bitmap Properties: bitmap slot of the Vista shader -> bitmap slot of the MCC shader
    bprops = []
    for k, (bi, ai) in enumerate(src['bprops']):
        if bi == NONE16:
            bprops.append((NONE16, ai))
            continue
        tgt = v2m.get(bi)
        bprops.append((tgt if tgt is not None else NONE16, ai))
        if tgt is None:
            rep.notes.append('bitmap property %d: Vista bitmap slot %d has no MCC slot' % (k, bi))
    out['bprops'] = bprops
    out['cprops'] = list(src['cprops'])
    out['vprops'] = list(src['vprops'])
    derive_skeleton(src, vsd, msd, out, rep)
    return out, rep


# ================================================================================================
# stem pair assessment (stems only, no shader): the integrator's guide to which stems map exactly
# ================================================================================================
def assess_pair(vsd, msd, vlearn=None, mlearn=None, roles=None, defaults=None, vparams=None, mparams=None, layout=None):
    """Class of the Vista stem -> MCC stem mapping, judged from the stems (and the learned maps) alone, with a synthetic Vista shader of the stem's
    shape (values do not matter, only which MCC components find evidence).
      'identical'  equal fingerprints: shaders are copied unchanged
      'exact'      every MCC slot is paired by the engine, a learned name, a placeholder order, a register role or a learned constant
      'partial'    some MCC component has no evidence (reverse_arrays reports it unresolved) or only a flagged template default
    -> (class, detail dict)"""
    if msd.fp == vsd.fp:
        return 'identical', {}
    vshape = tuple(vlearn['shape'][:3]) if vlearn is not None and vlearn.get('shape') else vsd.formula_shape()
    src = {'bitmaps': [('x%d' % j, 0, 0.0) for j in range(vshape[0])], 'pc': [bytes(4)] * vshape[1], 'vc': [(0.0,) * 4] * vshape[2],
           'bprops': [], 'cprops': [], 'vprops': [], 'lods': [(0, 0)] * len(vsd.lods), 'layers': [], 'passes': [], 'impls': [(0,) * 5] * vsd.n_impl, 'aprefs': []}
    out, rep = reverse_arrays(src, vsd, msd, vlearn, mlearn, roles, defaults=defaults, vparams=vparams, mparams=mparams, layout=layout, synthetic=True)
    det = {'paired': dict(rep.paired), 'by_name': rep.by_name, 'by_tname': rep.by_tname, 'extras': rep.extras, 'by_role': dict(rep.by_role), 'by_weak': dict(rep.by_weak),
           'by_struct': dict(rep.by_struct), 'by_layout': dict(rep.by_layout), 'by_renum': dict(rep.by_renum),
           'by_literal': dict(rep.by_literal), 'by_default': dict(rep.by_default), 'unresolved': list(rep.unresolved), 'name_conflicts': list(rep.name_conflicts)}
    return ('exact' if rep.exact else 'partial'), det


# ================================================================================================
# PART 2: the node layer (RevCtx)
# ================================================================================================
def _memo(ctx):
    ex = getattr(ctx, 'extra', None)
    if ex is None:
        ex = {}
        try:
            ctx.extra = ex
        except Exception:                                          # noqa: BLE001
            ex = getattr(ctx, '_rev_shad_extra', None) or {}
            try:
                ctx._rev_shad_extra = ex
            except Exception:                                      # noqa: BLE001
                pass
    return ex


def _vista_name_of(cache):
    def name_of(d):
        if d in (NULL, 0):
            return None
        i = d & 0xFFFF
        t = cache.tags[i] if i < len(cache.tags) else None
        return t.name if t is not None and t.datum == d else None
    return name_of


def _mcc_name_of(cache):
    def name_of(d):
        t = cache.tag_by_datum(d)
        return t.name if t else None
    return name_of


def _resolve_vista(ctx, tag):
    """(VistaTag, VistaCache) that hold the DATA of a tag of the current source map (a shared placeholder resolves in the shared maps)"""
    cache = ctx.cur_cache
    if tag.addr:
        return tag, cache
    for nm in ('shared', 'single_player_shared', 'mainmenu'):
        try:
            sh = ctx.vista_refs.get(nm)
        except Exception:                                          # noqa: BLE001  map not on disk
            continue
        if tag.index < len(sh.tags):
            st = sh.tags[tag.index]
            if st.datum == tag.datum and st.group == tag.group and st.addr:
                return st, sh
    return None, None


def vista_stem(ctx, datum):
    """-> (stem name, StemData) of the Vista stem tag with this datum in the map being converted; (None, None) if it cannot be read."""
    if datum in (NULL, 0):
        return None, None
    t = ctx.vista_tag(datum)
    if t is None or t.group != 'stem':
        return None, None
    memo = _memo(ctx).setdefault('rev_shad_vstem', {})
    rt, cache = _resolve_vista(ctx, t)
    if rt is None:
        return None, None
    key = (cache.path, rt.index)
    hit = memo.get(key)
    if hit is None:
        root = ctx.decode_vista(rt, cache)
        sd = stem_data(root, _vista_name_of(cache), rt.name or '')
        hit = memo[key] = ((rt.name or '').lower(), sd)
    return hit


def register_ported_stem(ctx, vista_stem_name, msd=None):
    """The integrator ported the Vista stem `vista_stem_name` to MCC (DXBC) with the SAME compiled structure (passes, implementations, remappings):
    its shaders are copied unchanged (tier D).  `msd` may carry a StemData of a port whose structure differs on purpose."""
    _memo(ctx).setdefault('rev_ported_stems', {})[vista_stem_name.lower()] = msd


def _mcc_real(ctx, cache, tag):
    """(MccCache, MccTag) that hold the data of a donor tag (an external tag of a stock MP map lives in an MCC shared map)"""
    if not getattr(tag, 'external', False):
        return cache, tag
    refs = getattr(ctx, 'mcc_refs', None) or getattr(ctx, 'mcc_shared', None)
    for nm in ('shared', 'single_player_shared', 'mainmenu'):
        sh = refs.get(nm) if refs is not None else None
        if sh is None or tag.index >= len(sh.tags):
            continue
        st = sh.tags[tag.index]
        if st.datum == tag.datum and st.group == tag.group and st.has_data:
            return sh, st
    return None, None


def target_stem(ctx, vname, vsd, vdatum=None):
    """The stem the output shader will be compiled against.  -> (StemData, kind, label) or (None, reason, label)
      kind 'ported'      the integrator ported the Vista stem (register_ported_stem, or a plan entry 'convert' of that stem, also the PRIVATE port
                         '<name>__<map>' of a stem the input carries under a stock name - found through `vdatum`, the shader's Template datum): identical structure
           'donor'       MCC's own stem of the same name (plan entry 'mcc_donor')
           'substitute'  the plan aliased the missing stem to ANOTHER MCC stem (opts stem_fallback): NOT faithful, mapped by the engine pairing anyway"""
    memo = _memo(ctx)
    ported = memo.get('rev_ported_stems') or {}
    if vname.lower() in ported:
        return (ported[vname.lower()] or vsd), 'ported', vname
    plan = getattr(ctx, 'plan', None)
    entry = None
    private = False
    if plan is not None and vdatum not in (None, NULL, 0) and getattr(plan, 'private_stems', None):
        try:
            entry = plan.entry_of_datum(getattr(ctx, 'cur_src', 'src'), vdatum)
        except Exception:                                          # noqa: BLE001
            entry = None
        private = entry is not None and entry.group == 'stem' and bool(getattr(entry, 'private_of', None))
        if not private:
            entry = None
    if entry is None:
        entry = plan.entry_of_name('stem', vname) if plan is not None else None
    if entry is None or entry.action == 'drop':
        return None, 'no target stem in the plan', vname
    sub = entry.name.lower() != vname.lower() and not private
    if entry.action == 'convert':
        return (None, 'plan converts a substitute stem from Vista', entry.name) if sub else (vsd, 'ported', entry.name)
    key = ('mstem', entry.name.lower())
    hit = memo.setdefault('rev_shad_mstem', {}).get(key)
    if hit is None:
        src = plan.donor_source_of(entry)
        if src is None:
            return None, 'donor not readable', entry.name
        cache, tag = _mcc_real(ctx, *src)
        if cache is None:
            return None, 'external donor stem not found', entry.name
        root = ctx.decode_mcc(cache, tag)
        hit = memo['rev_shad_mstem'][key] = stem_data(root, _mcc_name_of(cache), entry.name)
    if hit is None:
        return None, 'stem has no postprocess', entry.name
    return hit, ('substitute' if sub else 'donor'), entry.name


def _learned_for(vname, vsd, mname, msd):
    vl = SPM.vista_map(vname)
    if not (vl and vl.get('fp') == vsd.fp and vl.get('n', 0) >= SPM.MIN_SAMPLES and vl.get('use') is not False):
        vl = None
    ml = SPM.mcc_map(mname, msd.fp)
    if not (ml and ml.get('n', 0) >= SPM.MIN_SAMPLES and ml.get('use') is not False):
        ml = None
    return vl, ml


# ---- bitmap sizes (Log Bitmap Dimension) --------------------------------------------------------------------------------------------
def _vista_dims(cache, tag):
    root = cache.read_addr(tag.addr, 0x50)
    cnt, addr = struct.unpack_from('<iI', root, 0x44)
    return [struct.unpack('<hh', cache.read_addr(addr + i * 0x74 + 4, 4)) for i in range(max(cnt, 0))]


def _mcc_dims(cache, tag):
    root = cache.read_at_addr(tag.addr, 0x50)
    cnt, addr = struct.unpack_from('<iI', root, 0x44)
    return [struct.unpack('<hh', cache.read_at_addr(addr + i * 0xA8 + 4, 4)) for i in range(max(cnt, 0))]


def _out_entry(ctx, datum):
    m = _memo(ctx).get('rev_shad_byout')
    if m is None:
        m = {}
        for e in (getattr(getattr(ctx, 'plan', None), 'entries', None) or ()):
            m[e.out_datum] = e
        _memo(ctx)['rev_shad_byout'] = m
    return m.get(datum)


def _out_dims(ctx, datum):
    """([(w, h)], name) of the bitmap tag the OUTPUT holds under this MCC datum, None when unknown"""
    memo = _memo(ctx).setdefault('rev_shad_dims', {})
    if datum in memo:
        return memo[datum]
    res = None
    try:
        e = _out_entry(ctx, datum)
        if e is not None and e.group == 'bitm':
            if e.action == 'convert':
                s = ctx.plan.source_of(e)
                if s is not None:
                    res = (_vista_dims(*s), e.name)
            elif e.action == 'mcc_donor':
                s = ctx.plan.donor_source_of(e)
                if s is not None:
                    c, t = _mcc_real(ctx, *s)
                    if c is not None:
                        res = (_mcc_dims(c, t), e.name)
    except Exception as ex:                                        # noqa: BLE001
        ctx.warn('rev_shad_bitmap_dims', '0x%08X: %r' % (datum, ex))
    memo[datum] = res
    return res


def _vista_dims_of(ctx, vdatum):
    t = ctx.vista_tag(vdatum)
    if t is None:
        return None
    rt, cache = _resolve_vista(ctx, t)
    if rt is None or rt.group != 'bitm':
        return None
    try:
        return _vista_dims(cache, rt)
    except Exception:                                              # noqa: BLE001
        return None


def fix_log_dims(ctx, vista_pp, out):
    """Bitmaps[].Log Bitmap Dimension must describe the bitmap the OUTPUT holds.  The Vista value is kept when the output bitmap has the size the
    Vista bitmap had (a bitmap converted from the same Vista tag; preserves the tool's default_2d quirk) and recomputed (ln(max(w,h)) * ln 2) when
    the output bitmap is another one (an MCC donor of the same name with another size) or the slot is new.  -> number recomputed"""
    base = {}
    for k in _blk(vista_pp, 'Bitmaps'):
        vd = k.u32(0)
        if vd in (NULL, 0):
            continue
        md = ctx.remap_datum(vd)
        if md not in (NULL, 0) and md not in base:
            dims = _vista_dims_of(ctx, vd)
            if dims:
                base[md] = dims
    n = 0
    for i, (d, idx, ld) in enumerate(out['bitmaps']):
        if d in (NULL, 0):
            continue
        od = _out_dims(ctx, d)
        if od is None or not (0 <= idx < len(od[0])):
            continue
        b = base.get(d)
        if b is not None and idx < len(b) and tuple(b[idx]) == tuple(od[0][idx]):
            continue
        new = B.bitmap_log_dimension(od[1], *od[0][idx])
        if abs(new - ld) > 1e-6:
            out['bitmaps'][i] = (d, idx, new)
            n += 1
    ctx.stats['rev_shad_log_dims_recomputed'] += n
    return n


def sync_predicted_resources(ctx, root, out):
    """Predicted Resources = prefetch hints for the shader's bitmaps; the SET of hinted tags equals the Bitmaps[] tags (Vista corpus 2807 / 2807).
    When the final Bitmaps[] names other tags than the hint list, rebuild it by the Vista rule: the unique non-null (0, Bitmap Index, tag) in order."""
    pred = _blk(root, 'Predicted Resources')
    want = []
    for d, idx, _ld in out['bitmaps']:
        if d not in (NULL, 0) and (0, idx, d) not in want:
            want.append((0, idx, d))
    have = set(k.u32(4) for k in pred if k.u32(4) not in (NULL, 0))
    if have == set(d for _t, _i, d in want):
        return False
    _set_blk(root, 'Predicted Resources', [_new_child(root, 'Predicted Resources', struct.pack('<HHI', t, i & 0xFFFF, d)) for t, i, d in want])
    ctx.stats['rev_shad_predicted_rebuilt'] += 1
    return True


def _write_bitmaps(pp, out):
    _set_blk(pp, 'Bitmaps', [_new_child(pp, 'Bitmaps', struct.pack('<IiI', d if d is not None else NULL, idx, struct.unpack('<I', struct.pack('<f', ld))[0]))
                              for d, idx, ld in out['bitmaps']])


def _write_skeleton(pp, out):
    """only the skeleton blocks of a shader (Levels Of Detail, Layers, Passes, Implementations, Animated Parameter References)"""
    _set_blk(pp, 'Levels Of Detail', [_new_child(pp, 'Levels Of Detail', struct.pack('<IH', f, l)) for f, l in out['lods']])
    _set_blk(pp, 'Layers', [_new_child(pp, 'Layers', struct.pack('<H', v)) for v in out['layers']])
    _set_blk(pp, 'Passes', [_new_child(pp, 'Passes', struct.pack('<H', v)) for v in out['passes']])
    _set_blk(pp, 'Implementations', [_new_child(pp, 'Implementations', struct.pack('<5H', *e)) for e in out['impls']])
    _set_blk(pp, 'Animated Parameter References', [_new_child(pp, 'Animated Parameter References', struct.pack('<HBB', *r)) for r in out['aprefs']])


def _write_arrays(pp, out):
    _write_bitmaps(pp, out)
    _set_blk(pp, 'Pixel Constants', [_new_child(pp, 'Pixel Constants', bytes(x)) for x in out['pc']])
    _set_blk(pp, 'Vertex Constants', [_new_child(pp, 'Vertex Constants', struct.pack('<4f', *x)) for x in out['vc']])
    _set_blk(pp, 'Bitmap Properties', [_new_child(pp, 'Bitmap Properties', struct.pack('<HH', a, b)) for a, b in out['bprops']])
    _set_blk(pp, 'Levels Of Detail', [_new_child(pp, 'Levels Of Detail', struct.pack('<IH', f, l)) for f, l in out['lods']])
    _set_blk(pp, 'Layers', [_new_child(pp, 'Layers', struct.pack('<H', v)) for v in out['layers']])
    _set_blk(pp, 'Passes', [_new_child(pp, 'Passes', struct.pack('<H', v)) for v in out['passes']])
    _set_blk(pp, 'Implementations', [_new_child(pp, 'Implementations', struct.pack('<5H', *e)) for e in out['impls']])
    _set_blk(pp, 'Animated Parameter References', [_new_child(pp, 'Animated Parameter References', struct.pack('<HBB', *r)) for r in out['aprefs']])


def _shad_name(ctx):
    ent = getattr(ctx, 'cur_entry', None)
    return getattr(ent, 'name', None) if ent is not None else None


def reverse_shad(ctx, vista_node, mcc_node, name=None):
    """The node handler body.  Returns the Report (None when nothing could be done; the generic output stays)."""
    name = name or _shad_name(ctx) or '?'
    mpps = _blk(mcc_node, 'Postprocess Definition')
    if not mpps:
        ctx.stats['rev_shad_tier:no_postprocess'] += 1
        return None
    vdatum = vista_node.u32(OFF_TEMPLATE + 4)
    vname, vsd = vista_stem(ctx, vdatum)
    if vsd is None:
        ctx.stats['rev_shad_tier:no_vista_stem'] += 1
        ctx.warn('rev_shad_no_vista_stem', 'Template datum 0x%08X is not a readable Vista stem: arrays left as the generic pass copied them' % vdatum, name)
        return None
    msd, kind, mname = target_stem(ctx, vname, vsd, vdatum)
    if msd is None:
        ctx.stats['rev_shad_tier:no_target_stem'] += 1
        ctx.warn('rev_shad_no_target_stem', '%s: %s (%s) - arrays left as the generic pass copied them' % (vname.split('\\')[-1], kind, mname), name)
        return None
    pp = mpps[0]
    src = shader_arrays(pp)                    # the generic pass copied the Vista arrays: tag ids are MCC datums already
    memo = _memo(ctx)
    pc = memo.setdefault('rev_shad_pair_cache', {})
    cache = pc.setdefault((vsd.fp, msd.fp, mname.lower()), {})
    vl, ml = _learned_for(vname, vsd, mname, msd)

    def mkbitmap(path):
        d = ctx.remap_name('bitm', path)
        if d in (NULL, 0, None):
            ctx.warn('rev_shad_global_bitmap_missing', 'bitmap %s is not in the output map' % path, name)
            return NULL_BITMAP
        return (d, 0, 0.0)

    opts = getattr(ctx, 'opts', None) or {}
    use_defaults = opts.get('rev_shad_defaults', True)
    structural = not opts.get('rev_shad_no_structure')            # template-order names + register structure; off = the previous behaviour
    out, rep = reverse_arrays(src, vsd, msd, vl, ml, defaults=SPM.default_features(mname, 'mcc') if use_defaults else None, mkbitmap=mkbitmap,
                              null_test=lambda el: el[0] in (NULL, 0), cache=cache,
                              vparams=SS.bitmap_param_names(vname, 'vista') if structural else None,
                              mparams=SS.bitmap_param_names(mname, 'mcc') if structural else None,
                              layout=SS.register_layout() if structural else None)
    short = vname.replace('shaders\\shader_templates\\', '')
    ctx.stats['rev_shad_tier:%s' % rep.tier] += 1
    ctx.stats['rev_shad_stem_kind:%s' % kind] += 1
    # the Template references point at the stem the shader was compiled against (the generic pass remapped them through the plan already)
    od = ctx.remap_datum(vdatum)
    if od in (NULL, None, 0):
        od = ctx.remap_name('stem', vname)
    if od not in (NULL, None):
        if mcc_node.u32(OFF_TEMPLATE + 4) in (NULL, 0):
            mcc_node.set_u32(OFF_TEMPLATE + 4, od)
        if pp.u32(0) in (NULL, 0):
            pp.set_u32(0, od)
    vpps = _blk(vista_node, 'Postprocess Definition')
    stale = None
    if rep.tier == 'D':
        # identical stems: the arrays stay; only a Log Bitmap Dimension may need a new value (the output bitmap is MCC's own, of another size)
        if vpps and fix_log_dims(ctx, vpps[0], out):
            _write_bitmaps(pp, out)
        stale = stale_skeleton(src, vsd, msd, vl, ml)
        if stale is not None:
            if stale[0] and (getattr(ctx, 'opts', None) or {}).get('rev_shad_stale_keep'):      # A/B switch: keep the skeleton exactly as the Vista shader has it
                stale = (False, stale[1] + ' [rev_shad_stale_keep]')
            if stale[0]:
                skel = dict(out)
                derive_skeleton(src, vsd, msd, skel, rep)
                _write_skeleton(pp, skel)
                ctx.stats['rev_shad_stale_skeleton'] += 1
                rep.notes.append('stale skeleton: ' + stale[1])
                ctx.warn('rev_shad_stale_skeleton', '%s on %s: %s' % (short, mname.split('\\')[-1], stale[1]), name)
            else:
                ctx.stats['rev_shad_stale_skeleton_kept'] += 1
                rep.notes.append('stale skeleton KEPT: ' + stale[1])
                ctx.warn('rev_shad_stale_skeleton_kept', '%s on %s: %s' % (short, mname.split('\\')[-1], stale[1]), name)
    else:
        if vpps:
            fix_log_dims(ctx, vpps[0], out)
        _write_arrays(pp, out)
        sync_predicted_resources(ctx, mcc_node, out)
        ctx.stats['rev_shad_paired_slots'] += sum(rep.paired.values())
        ctx.stats['rev_shad_by_name'] += rep.by_name + rep.extras
        ctx.stats['rev_shad_by_tname'] += rep.by_tname
        ctx.stats['rev_shad_by_struct'] += sum(rep.by_struct.values())
        ctx.stats['rev_shad_by_layout'] += sum(rep.by_layout.values())
        ctx.stats['rev_shad_by_renum'] += sum(rep.by_renum.values())
        ctx.stats['rev_shad_by_role'] += sum(rep.by_role.values())
        ctx.stats['rev_shad_by_weak'] += sum(rep.by_weak.values())
        ctx.stats['rev_shad_by_literal'] += sum(rep.by_literal.values())
        ctx.stats['rev_shad_by_default'] += sum(rep.by_default.values())
        ctx.stats['rev_shad_unresolved'] += len(rep.unresolved)
        ctx.stats['rev_shad_dropped_features'] += len(rep.dropped)
    if kind == 'substitute' and rep.tier == 'D':
        # equal stem fingerprint: the compiled passes / implementations / remappings are the Vista stem's own (the templates differ only in authoring data,
        # which the cache does not carry), so the runtime data of the shader are exactly what the Vista engine ran
        ctx.stats['rev_shad_substitute_same_structure'] += 1
        ctx.warn('rev_shad_substitute_same_structure', 'Vista template %s has no MCC stem; the substitute %s has the identical compiled structure (fingerprint equal): '
                 'arrays copied unchanged' % (short, mname.replace('shaders\\shader_templates\\', '')), name)
    elif kind == 'substitute':
        ctx.stats['rev_shad_substitute'] += 1
        ctx.warn('rev_shad_substitute_stem', 'Vista template %s has no MCC stem; compiled against the SUBSTITUTE %s (opt-in stem_fallback, NOT faithful): '
                 'registers paired by the engine, %d slot(s) unresolved' % (short, mname.replace('shaders\\shader_templates\\', ''), len(rep.unresolved)), name)
    if rep.unresolved:
        ctx.stats['rev_shad_shaders_unresolved'] += 1
        ctx.warn('rev_shad_unresolved', '%s on %s: no evidence for %d slot component(s) %s - left null / zero' % (
            short, mname.replace('shaders\\shader_templates\\', ''), len(rep.unresolved), rep.unresolved[:6]), name)
    if rep.by_default:
        ctx.stats['rev_shad_shaders_default'] += 1
        ctx.warn('rev_shad_default', '%s: %d component(s) take the MCC template default (parameter not stored in the Vista compile)' % (
            short, sum(rep.by_default.values())), name)
    if rep.animation == 'dropped':
        ctx.stats['rev_shad_animation_dropped'] += 1
        ctx.warn('rev_shad_animation_dropped', '%s: animation cannot be rebuilt for the MCC stem %s' % (short, mname), name)
    if rep.name_conflicts:
        ctx.warn('rev_shad_name_conflict', '%s: engine pairing contradicts the learned parameter names %s' % (short, rep.name_conflicts[:3]), name)
    # exact = the mapping used only the evidence of the stems (no unresolved slot, no template default) AND the stem is the Vista one or MCC's own
    # of the same name; a re-structured substitute stem is never called exact
    faithful = rep.exact and not (kind == 'substitute' and rep.tier != 'D')
    if faithful:
        ctx.stats['rev_shad_exact'] += 1
    d = rep.as_dict()
    d.update({'vista_stem': vname, 'mcc_stem': mname, 'stem_kind': kind, 'exact': faithful, 'stale_skeleton': (None if stale is None else ('rebuilt' if stale[0] else 'kept'))})
    memo.setdefault('rev_shad_report', {})[name.lower()] = d
    return rep


@node_handler('shad', '', prio=150)
def shad_root(ctx, vista_node, mcc_node):
    if (getattr(ctx, 'opts', None) or {}).get('rev_shad_disable'):
        ctx.stats['rev_shad_disabled'] += 1
        return
    try:
        reverse_shad(ctx, vista_node, mcc_node)
    except Exception as ex:                                        # noqa: BLE001  one odd shader must not lose the map: the generic arrays stay
        if (getattr(ctx, 'opts', None) or {}).get('strict'):
            raise
        ctx.stats['rev_shad_failed'] += 1
        ctx.warn('rev_shad_failed', '%r - the generic (positional) arrays stay' % (ex,))
