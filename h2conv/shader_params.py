# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Shader parameter maps: remap MCC compiled shader arrays -> Vista compiled arrays BY PARAMETER NAME.

Learned data: h2conv/data/shader_param_maps.json (tables learned by analysing the games' own files and stock maps).

Why
  A compiled `shad` postprocess is  Bitmaps[], Pixel Constants[] (4 bytes b,g,r,a), Vertex Constants[] (4 floats) + Properties arrays,
  laid out by the shader TEMPLATE (stem) version it was compiled against.  The MCC editing kit's templates are cut-down 1-LOD
  versions of the Vista ones (stock MCC: full 2-3 LOD versions), so the same slot index means something else in the two games
  (tex_bump_detail_blend: MCC VC[0] is the detail transform, Vista VC[0] the bump transform).  The old per-template edit table
  (shaders.ARRAY_EDITS) was learned from twin shaders whose values were often EQUAL, so some edits were ambiguous and wrong.

What this module does
  * learned maps: for every (template, stem version) which parameter component feeds each slot;
  * decode(MCC arrays) -> {feature: value} where feature = ('bm'|'v'|'r'|'g'|'b'|'sx'|'sy'|'tx'|'ty'|'su'..., parameter name);
  * encode(features, Vista map, reference arrays) -> Vista arrays;
  * remap() = decode with the MCC map of the shader's stem version + encode with the Vista map.
  * ENGINE LEVEL (end of the file): RemapTab / stem_corr = MCC array slot -> Vista array slot from the two stems' pass Remappings (no samples);
    slot_roles = parameter component of a slot from the learned map or the cross-template register-role table (data['code_roles']);
    engine_apply = copy every paired MCC slot to its Vista slot and fill the Vista-only slots of the same parameters.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:                                         # noqa: BLE001
    pass

import hashlib
import json
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, 'data', 'shader_param_maps.json')

# Properties arrays: element k holds the parameter the template's Properties block names for this property id
BITMAP_PROPERTY_ORDER = (1, 2, 8, 9, 12)       # Diffuse Map, Lightmap Emissive Map, Alpha Test Map, Translucent Map, Active Camo Map
COLOR_PROPERTY_ORDER = (3, 10)                 # Lightmap Emissive Color, Lightmap Translucent Color
VALUE_PROPERTY_ORDER = (4, 5, 6, 7, 11, 13)    # Emissive Power, Resolution Scale, Half Life, Diffuse Scale, Translucent Alpha, Foliage Scale

NULL = 0xFFFFFFFF


# ================================================================================================
# decoding compiled shaders (works on tagtree Nodes of either game)
# ================================================================================================
def _blk(node, name):
    f = node.field(name)
    return node.blocks.get(f.offset, []) if f is not None else []


def stem_fingerprint(stem_root, name_of):
    """10-hex fingerprint of a compiled stem (shader template) postprocess: LOD, Layers, Passes (pass NAMES), Implementations, Remappings.
    Two stems with the same fingerprint lay their shaders' arrays out identically.  name_of(datum) -> tag name or None."""
    pps = _blk(stem_root, 'Postprocess Definition')
    if not pps:
        return None
    pp = pps[0]
    h = hashlib.md5()
    for nm in ('Levels Of Detail', 'Layers', 'Passes', 'Implementations', 'Remappings'):
        for e in _blk(pp, nm):
            d = bytearray(e.data)
            if nm == 'Passes':
                h.update((name_of(e.u32(4)) or '').lower().encode())
                d[0:8] = b'\0' * 8
            h.update(bytes(d))
        h.update(b'|')
    return h.hexdigest()[:10]


def decode_compiled(root, name_of):
    """shad root Node -> plain dict of the compiled arrays ('pp' = the postprocess Node)."""
    pps = _blk(root, 'Postprocess Definition')
    out = {'template': name_of(root.u32(4)), 'pp': None}
    if not pps:
        return out
    pp = pps[0]
    out['bitmaps'] = [(name_of(e.u32(0)), e.i32(4), struct.unpack_from('<f', e.data, 8)[0]) for e in _blk(pp, 'Bitmaps')]
    out['pc'] = [bytes(e.data) for e in _blk(pp, 'Pixel Constants')]
    out['vc'] = [struct.unpack_from('<4f', e.data) for e in _blk(pp, 'Vertex Constants')]
    out['bprops'] = [(e.u16(0), e.u16(2)) for e in _blk(pp, 'Bitmap Properties')]
    out['cprops'] = [struct.unpack_from('<3f', e.data) for e in _blk(pp, 'Color Properties')]
    out['vprops'] = [struct.unpack_from('<f', e.data)[0] for e in _blk(pp, 'Value Properties')]
    # skeleton + animation arrays
    out['lods'] = [(e.u32(0), e.u16(4)) for e in _blk(pp, 'Levels Of Detail')]
    out['layers'] = [e.u16(0) for e in _blk(pp, 'Layers')]
    out['passes'] = [e.u16(0) for e in _blk(pp, 'Passes')]
    out['impls'] = [tuple(e.u16(2 * i) for i in range(5)) for e in _blk(pp, 'Implementations')]
    out['overlays'] = len(_blk(pp, 'Overlays'))
    out['ovrefs'] = [(e.u16(0), e.u16(2)) for e in _blk(pp, 'Overlay References')]
    out['aparams'] = [e.u16(0) for e in _blk(pp, 'Animated Parameters')]
    out['aprefs'] = [(e.u16(0), e.data[2], e.data[3]) for e in _blk(pp, 'Animated Parameter References')]
    out['pp'] = pp
    return out


# ================================================================================================
# learned maps
# ================================================================================================
_DATA = None


def load_data(path=None, reload=False):
    global _DATA
    if _DATA is None or reload or path:
        with open(path or DATA_PATH, 'r', encoding='utf-8') as fh:
            _DATA = json.load(fh)
    return _DATA


def template_entry(template):
    return load_data()['templates'].get((template or '').lower())


def vista_map(template):
    e = template_entry(template)
    return e.get('vista') if e else None


def mcc_map(template, fp):
    """the map of the MCC stem version `fp` of `template` (None when that version was never seen with enough samples)"""
    e = template_entry(template)
    if not e:
        return None
    return (e.get('mcc') or {}).get(str(fp))


def param_defs(template, game):
    e = template_entry(template)
    return (e or {}).get('params_' + game) or []


def _kind(x):
    return x[0]


# feature value helpers --------------------------------------------------------------------------
def _f32(v):
    return struct.unpack('<f', struct.pack('<f', v))[0]


def _byte(v):
    return int(round(max(0.0, min(1.0, v)) * 255))


def decode_features(m, bitmaps, pc, vc):
    """Decode compiled arrays with the map m -> {feature: value}.
    feature = ('bm', param) -> the bitmaps element (whatever the caller stores in `bitmaps`),
              ('r'|'g'|'b'|'v'|'sx'|'sy'|'tx'|'ty'|'su'.., param) -> float.
    Colour / value features read from a Pixel Constant are quantised to 8 bits; the Vertex Constant floats are exact and win."""
    feats = {}
    for i, s in enumerate(m['bitmaps']):
        if i < len(bitmaps):
            if s[0] == 'p':
                feats[('bm', s[1])] = bitmaps[i]
            elif s[0] == 'w':
                feats[('bm', s[2])] = bitmaps[i]
    for j, slot in enumerate(m['pc']):
        if j >= len(pc):
            break
        for b, x in enumerate(slot):
            if x[0] in ('f', 'w') and x[1] != 'bm':
                feats[(x[1], x[2])] = pc[j][b] / 255.0
    for k, slot in enumerate(m['vc']):
        if k >= len(vc):
            break
        for c, x in enumerate(slot):
            if x[0] in ('f', 'w') and x[1] != 'bm':
                feats[(x[1], x[2])] = vc[k][c]
    return feats


def encode_arrays(m, feats, base_bitmaps, base_pc, base_vc):
    """Fill the arrays laid out by map m from feats; slots/components without a feature keep the base value.
    -> (bitmaps, pc, vc, n_written, n_missing)  (lengths follow the base arrays, which must match the map's shape)"""
    bitmaps = list(base_bitmaps)
    pc = [bytearray(x) for x in base_pc]
    vc = [list(x) for x in base_vc]
    wrote = missing = 0
    for i, s in enumerate(m['bitmaps']):
        if i >= len(bitmaps):
            break
        name = s[1] if s[0] == 'p' else (s[2] if s[0] == 'w' else None)
        if name is not None:
            if ('bm', name) in feats:
                bitmaps[i] = feats[('bm', name)]
                wrote += 1
            else:
                missing += 1
    for j, slot in enumerate(m['pc']):
        if j >= len(pc):
            break
        for b, x in enumerate(slot):
            if x[0] in ('f', 'w') and x[1] != 'bm':
                if (x[1], x[2]) in feats and feats[(x[1], x[2])] is not None:
                    pc[j][b] = _byte(feats[(x[1], x[2])])
                    wrote += 1
                else:
                    missing += 1
    for k, slot in enumerate(m['vc']):
        if k >= len(vc):
            break
        for c, x in enumerate(slot):
            if x[0] in ('f', 'w') and x[1] != 'bm':
                if (x[1], x[2]) in feats and feats[(x[1], x[2])] is not None:
                    vc[k][c] = _f32(feats[(x[1], x[2])])
                    wrote += 1
                else:
                    missing += 1
    return bitmaps, [bytes(x) for x in pc], [tuple(x) for x in vc], wrote, missing


# ================================================================================================
# the remap
# ================================================================================================
MIN_SAMPLES = 4          # a learned map built from fewer samples is not trusted


def usable(template, mcc_fp):
    """-> (mcc map, vista map) when both learned maps exist, were built from enough samples and were not disabled by the build-time
    evaluation; else None (the caller falls back to the edit table / twin)."""
    e = template_entry(template)
    if not e or 'vista' not in e:
        return None
    vm = e['vista']
    mm = (e.get('mcc') or {}).get(str(mcc_fp))
    if mm is None:
        return None
    for m in (mm, vm):
        if m.get('n', 0) < MIN_SAMPLES or m.get('use') is False:
            return None
    return mm, vm


def default_features(template, game='vista'):
    """{feature: value} from the template's parameter defaults (colours r/g/b, values v, bitmap transform components, bitmap names 'bm')"""
    feats = {}
    for p in param_defs(template, game):
        nm = p['name']
        if p['type'] == 0:
            feats[('bm', nm)] = (p.get('bitmap') or '').lower()
            sc = p.get('scale', 0.0)
            fl = p.get('anim_flags', 0)
            for comp, ok, val in (('su', fl & 1, sc), ('sx', fl & 2, sc), ('sy', fl & 2, sc), ('sz', fl & 2, sc), ('tx', fl & 4, 0.0), ('ty', fl & 4, 0.0), ('tz', fl & 4, 0.0)):
                if ok:
                    feats[(comp, nm)] = val
        elif p['type'] == 1:
            feats[('v', nm)] = p.get('value', 0.0)
        elif p['type'] == 2:
            c = p.get('color', [0.0, 0.0, 0.0])
            feats[('r', nm)], feats[('g', nm)], feats[('b', nm)] = c
    return feats


def _slot_identity(src):
    """cross-game identity of a Bitmaps slot source"""
    if src[0] == 'p':
        return ('p', src[1])
    if src[0] == 'w':
        return ('p', src[2])
    if src[0] == 'g':
        return ('g', src[1])
    return (src[0],)


def _slot_feats(slot):
    return frozenset((x[1], x[2]) for x in slot if x[0] in ('f', 'w'))


def _comp_compat(x, y):
    """can a Vista component with source x sit at the place of an MCC component with source y?  Features must be the same feature,
    constants the same value; unexplained ('?') and 'a constant in one map / a never-varying parameter in the other' are compatible."""
    kx, ky = x[0], y[0]
    if kx in ('f', 'w') and ky in ('f', 'w'):
        return (x[1], x[2]) == (y[1], y[2])
    if kx == 'c' and ky == 'c':
        return abs(float(x[1]) - float(y[1])) < 1e-4
    if kx == '?' or ky == '?':
        return not (kx == 'c' or ky == 'c')          # unexplained fits unexplained or a parameter, never a constant
    return False                                       # a constant never stands for a parameter (that pairing misplaced bump / base transforms)


def _slot_compat(sx, sy):
    return all(_comp_compat(a, b) for a, b in zip(sx, sy)) and len(sx) == len(sy)


def align_slots(mslots, vslots):
    """{Vista slot: MCC slot}: longest common subsequence of the two slot sequences under _slot_compat (identical layouts align one to one;
    inserted / removed / reordered slots are skipped)."""
    n, m = len(vslots), len(mslots)
    ok = [[_slot_compat(vslots[j], mslots[i]) for i in range(m)] for j in range(n)]
    L = [[0] * (m + 1) for _ in range(n + 1)]
    for j in range(n - 1, -1, -1):
        for i in range(m - 1, -1, -1):
            L[j][i] = L[j + 1][i + 1] + 1 if ok[j][i] else max(L[j + 1][i], L[j][i + 1])
    pairs = {}
    j = i = 0
    while j < n and i < m:
        if ok[j][i]:
            pairs[j] = i
            j += 1
            i += 1
        elif L[j + 1][i] >= L[j][i + 1]:
            j += 1
        else:
            i += 1
    return pairs


def _bm_compat(x, y):
    if x[0] == '?' or y[0] == '?':
        return True
    ix, iy = _slot_identity(x), _slot_identity(y)
    if ix[0] == 'p' and iy[0] == 'p':
        return ix == iy
    if ix[0] == 'g' and iy[0] == 'g':
        return ix == iy
    if ix[0] in ('p', 'g') and iy[0] in ('p', 'g'):
        return False
    return True


def align_bitmaps(mslots, vslots):
    n, m = len(vslots), len(mslots)
    ok = [[_bm_compat(vslots[j], mslots[i]) for i in range(m)] for j in range(n)]
    L = [[0] * (m + 1) for _ in range(n + 1)]
    for j in range(n - 1, -1, -1):
        for i in range(m - 1, -1, -1):
            L[j][i] = L[j + 1][i + 1] + 1 if ok[j][i] else max(L[j + 1][i], L[j][i + 1])
    pairs = {}
    j = i = 0
    while j < n and i < m:
        if ok[j][i]:
            pairs[j] = i
            j += 1
            i += 1
        elif L[j + 1][i] >= L[j][i + 1]:
            j += 1
        else:
            i += 1
    return pairs


def remap_arrays(mm, vm, mcc, ref, template=None, ref_is_twin=False, animated_from_ref=False, is_null=None):
    """Remap MCC compiled arrays to the Vista layout.
      mm, vm : learned maps (MCC stem version, Vista stem)
      mcc    : {'bitmaps': [elem...], 'pc': [bytes4...], 'vc': [4-tuple...], 'bprops': [(bitmap index, animated index)...]}
      ref    : same keys, in the VISTA layout (a Vista twin or representative shader of the stem) - provides every value the MCC shader
               cannot (constants, global bitmaps, parameters the MCC stem does not use, '?' slots)
      ref_is_twin : the reference is the same shader in Vista (its values for unknown parameters are the right ones); otherwise the template
               defaults replace the reference's values for parameters absent from the MCC arrays.
      animated_from_ref : the output skeleton/animation is the reference's, so the 'animated parameter index' of every Bitmap Property comes
               from the reference (the MCC index points into the MCC animation arrays).
      is_null : callable(bitmaps element) -> True for a null / default_2d placeholder bitmap.  When the MCC value of a bitmap slot AND the
               reference's value are both null-like the reference's is kept (the two games spell 'no bitmap' differently: null versus
               rasterizer/default_2d; both render the same).
    -> dict(bitmaps, pc, vc, bprops, stats) or None when the reference does not have the Vista layout."""
    vs = vm['shape']
    if ref is None or len(ref['bitmaps']) != vs[0] or len(ref['pc']) != vs[1] or len(ref['vc']) != vs[2]:
        return None
    feats = decode_features(mm, mcc['bitmaps'], mcc['pc'], mcc['vc'])
    decoded = frozenset(feats)                     # before the template defaults are overlaid
    stats = {'decoded': len(feats)}
    if not ref_is_twin and template:
        d = default_features(template, 'vista')
        added = 0
        for k, v in d.items():
            if k[0] != 'bm' and k not in feats:
                feats[k] = v
                added += 1
        stats['defaults'] = added
    bitmaps, pc, vc, wrote, missing = encode_arrays(vm, feats, ref['bitmaps'], ref['pc'], ref['vc'])
    # Positional carry for slots the maps cannot name: when the two layouts line up (>= 75% of the Vista slots align under _slot_compat), a Vista
    # component that is unexplained ('?') - or a parameter component the MCC arrays do not decode while the aligned MCC component is '?' - takes
    # the MCC slot's value.
    n_pair = 0
    for arr_name, outarr in (('pc', pc), ('vc', vc)):
        al = align_slots(mm[arr_name], vm[arr_name])
        if len(al) < 0.75 * max(len(vm[arr_name]), 1):
            continue
        for j, i in al.items():
            if i >= len(mcc[arr_name]) or j >= len(outarr):
                continue
            for c, x in enumerate(vm[arr_name][j]):
                y = mm[arr_name][i][c]
                if x[0] == '?' or (x[0] in ('f', 'w') and (x[1], x[2]) not in decoded and y[0] == '?'):
                    if arr_name == 'pc':
                        b_ = bytearray(outarr[j])
                        b_[c] = mcc['pc'][i][c]
                        outarr[j] = bytes(b_)
                    else:
                        v_ = list(outarr[j])
                        v_[c] = mcc['vc'][i][c]
                        outarr[j] = tuple(v_)
                    n_pair += 1
    alb = align_bitmaps(mm['bitmaps'], vm['bitmaps'])
    if len(alb) >= 0.75 * max(len(vm['bitmaps']), 1):
        for j, i in alb.items():
            if vm['bitmaps'][j][0] == '?' and i < len(mcc['bitmaps']):
                bitmaps[j] = mcc['bitmaps'][i]
                n_pair += 1
    stats['paired_unexplained'] = n_pair
    if is_null is not None:
        for i, sl in enumerate(vm['bitmaps']):
            if i < len(bitmaps) and sl[0] in ('p', 'w') and bitmaps[i] is not ref['bitmaps'][i] and is_null(bitmaps[i]) and is_null(ref['bitmaps'][i]):
                bitmaps[i] = ref['bitmaps'][i]
    stats['wrote'] = wrote
    stats['missing'] = missing
    # bitmap properties: same bitmap IDENTITY, Vista slot index
    vslots = {}
    for i, s in enumerate(vm['bitmaps']):
        vslots.setdefault(_slot_identity(s), []).append(i)
    bprops = []
    nbad = 0
    for k, (bi, ai) in enumerate(mcc['bprops']):
        refp = ref['bprops'][k] if k < len(ref['bprops']) else (0xFFFF, 0xFFFF)
        if animated_from_ref:
            ai = refp[1]
        if bi == 0xFFFF:
            bprops.append((0xFFFF, ai))
            continue
        if bi >= len(mm['bitmaps']):
            bprops.append(refp)
            nbad += 1
            continue
        cand = vslots.get(_slot_identity(mm['bitmaps'][bi]))
        if cand and len(cand) == 1:
            bprops.append((cand[0], ai))
        else:
            bprops.append(refp)
            nbad += 1
    stats['bprops_unmapped'] = nbad
    return {'bitmaps': bitmaps, 'pc': pc, 'vc': vc, 'bprops': bprops, 'stats': stats}


# ================================================================================================
# animation arrays (shader Implementations ranges + Animated Parameter References) for a Vista stem
# ================================================================================================
# A shader's animation is stored as  Overlays (functions), Overlay References, Animated Parameters (per animated parameter: range of overlay
# references) - these do not depend on the stem - plus, per stem IMPLEMENTATION, five (start, count) ranges into Animated Parameter References
# (one list per register kind).  The ranges are packed as  start | (2 * count) << 8.  The Vista tool builds the list by walking the
# implementations in order and, for every implementation, appending the lists of the fields in the order 3, 4, 1, 2, 0 (an empty field
# points at the current end of the list).  Passes are the same code in both games, so the references of a Vista implementation are those of
# the MCC implementation of the same pass (name, n-th occurrence, variant); Vista passes the MCC stem does not have get none.
# Verified against 1,085 animated shaders that exist in both games (all 3 stem families): 1,073 reproduce the Vista shader's arrays exactly
# (the others differ by art edits of the animation itself).
ANIM_FIELD_ORDER = (3, 4, 1, 2, 0)


def _rng(v):
    return v & 0xFF, (v >> 8) // 2


def _pack_rng(start, count):
    return (start & 0xFF) | ((count * 2) << 8)


class StemTab(object):
    """pass table of a compiled stem: passes [(name, first implementation, implementation count)], n_impl, impl_pass {impl: (pass, variant)}"""
    __slots__ = ('passes', 'n_impl', 'impl_pass')

    def __init__(self, pp, name_of):
        self.passes = []
        for e in _blk(pp, 'Passes'):
            s, c = _rng(e.u16(8))
            self.passes.append(((name_of(e.u32(4)) or '').lower(), s, c))
        self.n_impl = len(_blk(pp, 'Implementations'))
        self.impl_pass = {}
        for pi, (nm, s, c) in enumerate(self.passes):
            for k in range(s, s + c):
                self.impl_pass[k] = (pi, k - s)


def _impl_map(vs, ms):
    """Vista implementation -> MCC implementation of the same pass (name, occurrence capped at the MCC count, variant) or None"""
    import collections
    seen = collections.Counter()
    vocc = []
    for nm, _s, _c in vs.passes:
        vocc.append(seen[nm])
        seen[nm] += 1
    mby = collections.defaultdict(list)
    for i, (nm, _s, _c) in enumerate(ms.passes):
        mby[nm].append(i)
    out = {}
    for j in range(vs.n_impl):
        pi, var = vs.impl_pass.get(j, (None, 0))
        if pi is None:
            out[j] = None
            continue
        cands = mby.get(vs.passes[pi][0])
        if not cands:
            out[j] = None
            continue
        mp = cands[min(vocc[pi], len(cands) - 1)]
        ms_, mc_ = ms.passes[mp][1], ms.passes[mp][2]
        out[j] = ms_ + min(var, mc_ - 1) if mc_ else None
    return out


def predict_animation(entries, aprefs, vs, ms):
    """entries: the MCC shader's Implementations animation ranges [(5 x u16)]; aprefs: its Animated Parameter References [(u16, u8, u8)];
    vs / ms: StemTab of the Vista / MCC stem.  -> (Vista entries [(5 x u16)], Vista references [(u16, u8, u8)]) or None when the shapes disagree."""
    if len(entries) != ms.n_impl:
        return None
    refs = []
    for e in entries:
        lists = []
        for v in e:
            s, n = _rng(v)
            if s + n > len(aprefs):
                return None
            lists.append(list(aprefs[s:s + n]))
        refs.append(lists)
    m = _impl_map(vs, ms)
    out_refs = []
    out_entries = []
    for j in range(vs.n_impl):
        i = m.get(j)
        lists = refs[i] if i is not None else [[], [], [], [], []]
        vals = [0] * 5
        for f in ANIM_FIELD_ORDER:
            if lists[f]:
                vals[f] = _pack_rng(len(out_refs), len(lists[f]))
                out_refs.extend(lists[f])
            else:
                vals[f] = _pack_rng(len(out_refs), 0)
        out_entries.append(tuple(vals))
    if len(out_refs) > 255:                  # 8-bit start field
        return None
    return out_entries, out_refs


# ================================================================================================
# ENGINE-LEVEL slot correspondence (stem Remappings) + cross-template register roles
# ================================================================================================
# Why this exists
#   A compiled stem lists, per pass implementation, Remappings (register code, array index): "register `code` of this pass reads Bitmaps /
#   Pixel Constants / Vertex Constants element `index`".  The same pass tag (same occurrence in the stem, same implementation variant) with the
#   same code in the MCC stem and in the Vista stem therefore names the SAME logical constant, so MCC array index -> Vista array index is known
#   from the two stems alone - no learned data, no source tags (an independent verifier found 6,272 such slot pairs over 487 MCC stems vs the
#   Vista stems with 0 conflicts and 0 non-injective maps).  The learned parameter maps (above) name the PARAMETER of a slot from samples; a slot
#   whose parameter never varied in a game's samples is a 'constant' there, so a custom shader that sets it lost the value (the Vista
#   reference's was kept: windownew's environment map, detail tiling, specular colours ...).  The correspondence does not depend on samples.
#   Vista-only slots (constants of passes the cut-down H2EK stems lack, e.g. the pixel-shader environment tints PC[4..7] of the tex_bump_env
#   family) have no MCC counterpart; their parameter is known through the REGISTER ROLE table: the same (pass, variant, code, component) feeds
#   the same parameter in every template (learned once from all templates with trusted maps;
#   measured leave-one-template-out).
class RemapTab(object):
    """pass table of a compiled stem Postprocess Definition: passes = [(pass tag name lower, [implementation | None])] where an implementation is
    {'bm'|'pc'|'vc': [(register code, shader array index)]}.  pp = the stem's 'Postprocess Definition' [0] Node, name_of(datum) -> tag name."""
    __slots__ = ('passes', '_keys')

    def __init__(self, pp, name_of):
        impls = _blk(pp, 'Implementations')
        rem = _blk(pp, 'Remappings')
        self.passes = []
        self._keys = None
        for e in _blk(pp, 'Passes'):
            nm = (name_of(e.u32(4)) or '').lower()
            s, c = _rng(e.u16(8))
            lst = []
            for k in range(s, s + c):
                if k >= len(impls):
                    lst.append(None)
                    continue
                d = {}
                for kind, off in (('bm', 0), ('pc', 2), ('vc', 4)):
                    rs, rc = _rng(impls[k].u16(off))
                    d[kind] = [(rem[j].u16(0), rem[j].data[3]) for j in range(rs, min(rs + rc, len(rem)))]
                lst.append(d)
            self.passes.append((nm, lst))

    def slot_keys(self):
        """{(kind, array index): [(pass name, variant, code)]}"""
        if self._keys is None:
            d = {}
            for nm, impls in self.passes:
                for k, im in enumerate(impls):
                    if im is None:
                        continue
                    for kind in ('bm', 'pc', 'vc'):
                        for code, idx in im[kind]:
                            d.setdefault((kind, idx), []).append((nm, k, code))
            self._keys = d
        return self._keys


def stem_corr(mt, vt):
    """Engine-level slot correspondence between an MCC stem (RemapTab mt) and the Vista stem of the same template (RemapTab vt).
    -> {'bm'|'pc'|'vc': {MCC index: Vista index}, 'conflicts': n}: pairs from the same pass (n-th occurrence) and implementation variant with the
    same register code; an MCC index with several Vista targets, or two MCC indices for one Vista index, are dropped (counted in 'conflicts')."""
    import collections
    pm = collections.defaultdict(list)
    pv = collections.defaultdict(list)
    for nm, impls in mt.passes:
        pm[nm].append(impls)
    for nm, impls in vt.passes:
        pv[nm].append(impls)
    cand = {k: collections.defaultdict(set) for k in ('bm', 'pc', 'vc')}
    for nm, ml in pm.items():
        vl = pv.get(nm)
        if not vl:
            continue
        for occ in range(min(len(ml), len(vl))):
            for a, b in zip(ml[occ], vl[occ]):
                if a is None or b is None:
                    continue
                for kind in cand:
                    da = collections.defaultdict(list)
                    db = collections.defaultdict(list)
                    for code, src in a[kind]:
                        da[code].append(src)
                    for code, src in b[kind]:
                        db[code].append(src)
                    for code, xs in da.items():
                        ys = db.get(code)
                        if ys is not None and len(xs) == len(ys):
                            for x, y in zip(xs, ys):
                                cand[kind][x].add(y)
    out = {}
    conflicts = 0
    for kind, d in cand.items():
        one = {iM: next(iter(s)) for iM, s in d.items() if len(s) == 1}
        inv = collections.Counter(one.values())
        good = {iM: iV for iM, iV in one.items() if inv[iV] == 1}
        conflicts += (len(d) - len(one)) + (len(one) - len(good))
        out[kind] = good
    out['conflicts'] = conflicts
    return out


def role_key(pass_name, variant, kind, code, comp):
    return '%s|%d|%s|%d|%d' % (pass_name, variant, kind, code, comp)


def code_roles():
    """the learned register-role table {role_key: [feature letter, parameter name, voters]} ({} when the data has none)"""
    return load_data().get('code_roles') or {}


def slot_roles(learned, tab, shape, roles=None):
    """Roles (which parameter component) of the Pixel / Vertex Constant components of ONE stem.
      learned : the learned map body that belongs to exactly this stem version (or None)
      tab     : its RemapTab (or None)
      shape   : (number of Pixel Constants, number of Vertex Constants) of the arrays that are interpreted
    -> {'pc': [[role | None] * 4], 'vc': [...]} where role = ((feature letter, parameter), how), how = 'learned' (a solid learned component),
    'table' (the register-role table: every (pass, variant, code) feeding the slot agrees) or 'weak' (a weakly learned component the table does not contradict)."""
    roles = code_roles() if roles is None else roles
    keys = tab.slot_keys() if tab is not None else {}
    out = {}
    for kind, n in (('pc', shape[0]), ('vc', shape[1])):
        rows = []
        for idx in range(n):
            row = []
            for comp in range(4):
                lrn = None
                if learned is not None and idx < len(learned[kind]) and comp < len(learned[kind][idx]):
                    lrn = learned[kind][idx][comp]
                if lrn is not None and lrn[0] == 'f':
                    row.append(((lrn[1], lrn[2]), 'learned'))
                    continue
                preds = set()
                for ps, var, code in keys.get((kind, idx), ()):
                    r = roles.get(role_key(ps, var, kind, code, comp))
                    if r:
                        preds.add((r[0], r[1]))
                if len(preds) == 1:
                    row.append((next(iter(preds)), 'table'))
                elif not preds and lrn is not None and lrn[0] == 'w':
                    row.append(((lrn[1], lrn[2]), 'weak'))
                else:
                    row.append(None)
            rows.append(row)
        out[kind] = rows
    return out


def engine_apply(corr, mroles, vroles, mcc, out, is_null=None):
    """Engine-level pass over finished Vista arrays.  mcc / out = {'bitmaps': [(datum, index, log dim)], 'pc': [bytes4], 'vc': [4-tuple]} (out is
    modified in place; pc entries are replaced by new bytes objects).
      1. every MCC slot that the stems pair with a Vista slot (corr) is COPIED to it - the same register of the same pass holds the same constant
         whatever the learned parameter maps say about the Vista slot (the maps call a slot that never varied in the Vista samples a constant and
         keep the reference shader's value).  A null / default_2d MCC bitmap is not copied (the output keeps the spelling the compiler uses).
      2. the parameter components found in the copied slots (role of the MCC slot, else of the Vista slot) are written to every OTHER Vista
         slot component whose role is that parameter (register roles: the pixel-shader copies of the environment tints PC[4..7] ...).
    -> Counter: bm_changed pc_changed vc_changed pc_filled vc_filled (values that differ from what `out` held before);
       pc_fill_agrees_<learned|table>, vc_fill_agrees_<..> = Vista-only components whose role value equals what `out` already held (diagnostics:
       the roles reproduce the learned maps wherever both know a slot)."""
    import collections
    st = collections.Counter()
    for iM, iV in corr['bm'].items():
        if iM >= len(mcc['bitmaps']) or iV >= len(out['bitmaps']):
            continue
        m = mcc['bitmaps'][iM]
        if is_null is not None and is_null(m):
            st['bm_null_kept'] += 1
            continue
        if tuple(out['bitmaps'][iV][:2]) != tuple(m[:2]):
            st['bm_changed'] += 1
            out['bitmaps'][iV] = m
    covered = {'pc': set(), 'vc': set()}
    known = {}                                           # feature -> (value, exact float from a Vertex Constant)
    for kind in ('pc', 'vc'):
        for iM, iV in corr[kind].items():
            if iM >= len(mcc[kind]) or iV >= len(out[kind]):
                continue
            covered[kind].add(iV)
            src = mcc[kind][iM]
            old = out[kind][iV]
            if kind == 'pc':
                src = bytes(src[:4])
                if bytes(old[:4]) != src:
                    st['pc_changed'] += 1
                    out['pc'][iV] = src
            else:
                src = tuple(src)
                if any(abs(a - b) > 1e-6 * max(1.0, abs(b)) for a, b in zip(old, src)):
                    st['vc_changed'] += 1
                    out['vc'][iV] = src
            for comp in range(4):
                f = None
                if mroles is not None and iM < len(mroles[kind]):
                    f = mroles[kind][iM][comp]
                if f is None and vroles is not None and iV < len(vroles[kind]):
                    f = vroles[kind][iV][comp]
                if f is None or f[1] == 'weak':
                    continue
                val = src[comp] / 255.0 if kind == 'pc' else src[comp]
                prev = known.get(f[0])
                if prev is None or (kind == 'vc' and not prev[1]):
                    known[f[0]] = (val, kind == 'vc')
    if vroles is not None:
        for kind in ('pc', 'vc'):
            for iV in range(min(len(out[kind]), len(vroles[kind]))):
                if iV in covered[kind]:
                    continue
                for comp in range(4):
                    f = vroles[kind][iV][comp]
                    if f is None or f[1] == 'weak':
                        continue
                    k = known.get(f[0])
                    if k is None:
                        continue                      # the MCC arrays do not carry this parameter (its pass is missing from the cut-down stem): keep the reference
                    if kind == 'pc':
                        b = _byte(k[0])
                        if abs(out['pc'][iV][comp] - b) > 1:
                            nb = bytearray(out['pc'][iV])
                            nb[comp] = b
                            out['pc'][iV] = bytes(nb)
                            st['pc_filled'] += 1
                        else:
                            st['pc_fill_agrees_' + f[1]] += 1
                    else:
                        v = _f32(k[0])
                        if abs(out['vc'][iV][comp] - v) > 1e-3 * max(1.0, abs(v)):
                            nv = list(out['vc'][iV])
                            nv[comp] = v
                            out['vc'][iV] = tuple(nv)
                            st['vc_filled'] += 1
                        else:
                            st['vc_fill_agrees_' + f[1]] += 1
    return st
