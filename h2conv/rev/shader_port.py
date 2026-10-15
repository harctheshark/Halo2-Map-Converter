# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""FAITHFUL PORT of the Vista shader code MCC lacks: policy + translation glue (the integrator of the four shader modules).

Binding rule: custom / missing shader templates are ported FAITHFULLY from their compiled Vista code,
only when the equivalence proof passes; whatever cannot be ported and verified is reported as UNPORTABLE (with the exact reason) and the
shaders that depend on it point at MCC's shaders\\invalid.  No substitute template, no guess.

What is ported (always by NAME: a map holds ONE tag per (group, name), so a name MCC already has stays MCC's tag):
  vrtx  Vista vs_2_0 byte code -> MCC vs_5_0 DXBC           h2conv.rev.d3d9_to_psh.translate_vs  (proof: random-input evaluation of both)
  spas  Vista ps_2_0 / ps_2_0 / ps_3_0 -> three ps_5_0 DXBC per Pixel Shader element, compiled with the H2EK tool recipe (+ the pass's
        alpha test from its render states)                  h2conv.rev.d3d9_to_psh.translate_ps  (proof per variant, alpha path included)
  stem  the Vista tag with every LOD / layer / pass / implementation / remapping UNCHANGED (the shaders' arrays are a function of exactly this
        structure), the pass references re-pointed        h2conv.rev.shader_tags.build_mcc_stem
A stem is portable iff every pass it names is either MCC's own pass of that name (identical structure, or a re-authored one with a verified
precedent) or itself ported; a pass iff all its pixel shader elements are proven and its vertex shaders are MCC's or ported.

Water: the names on shader_tags.SEMANTIC_WATER_CHAINS (stems water_pool / water_pool_static / water_static, their passes and vertex
shaders) exist in MCC as OTHER shaders.  With the planner option water_chain='vista' (default) the Vista stem, pass and vertex shader of a shader authored against them are
ported although MCC has the names - under the private names '<name>__vista' (private_water_name): the private stem names the private pass, the private pass the private vertex
shader.  PortPolicy.water_member() is the one test; 'mcc' restores the donor behaviour.

Sky depth: Vista's two sky vertex shaders (shader_tags.SKY_DEPTH_PINNED_VRTX) pin the clip depth to the far plane (clip z == clip w, drawn first
with the depth test off) while MCC draws the sky late with the depth test on and its same-name vertex shaders write real depth.  With the planner option sky_depth='vista'
(default) the two names are ported from the Vista tag UNDER THE SAME NAMES (translate_vs detects the pin and emits z = 0, the reverse-Z far plane); PortPolicy.sky_pin_member()
is the test, _decide_vrtx refuses a half-pinned tag (every non-empty classification must translate with fixup 'pin'); 'mcc' keeps MCC's donor.

Public API
    PortPolicy(env, resolve, cache_of, opts)   .decide(group, name, label, vtag) -> PortDecision       (names MCC does NOT have, and the private water chain)
                                               .audit_donor(group, name, label, vtag, donor) -> [flag text]   (names MCC has: custom content?)
    port_spas(node, name) -> SpasPort          port_vrtx(node, name) -> VrtxPort            (memoised by content)
    spas_dxbc_map(port) / vrtx_dxbc_list(port) the dxbc arguments of shader_tags.build_mcc_spas / build_mcc_vrtx
    validate_ported_spas(root, vista_node)     structural validation of a built spas (an empty Vista slot is allowed to stay empty)
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import collections
import hashlib
import struct

from .. import tagtree as T
from . import shader_tags as ST

NULL = 0xFFFFFFFF
GROUPS = ('stem', 'spas', 'vrtx')
PORT_VERSION = 2                  # 2 (sky depth pin): translate_vs can return fixup 'pin' (z = 0); cached translations of version 1 must never be served
PRIVATE_SUFFIX = '__vista'        # private copies of Vista shader-family tags whose MCC namesake is another shader (vertex shaders, the water chains)

# engine behaviour that was READ in the engine's code (it used to be the first ENGINE_ASSUMPTION; every report lists it as verified)
ENGINE_VERIFIED = (
    'MCC supplies its z-remap constants (c10.z / c10.w) to a ported vertex shader exactly as to the stock MCC vertex shaders: VERIFIED in the engine code - the per-view '
    'constants c0..c11 are written by sub_180955590 (c10 = (.., .., near*far/(far-near), 1-far/(far-near))) and sub_180954ED0 (c0..c3 / c10), neither of them by shader name, '
    'and sub_180964F80 uploads the whole shadow array before every DrawIndexed (read independently by two lenses and the tint verifier)',
)

# the one-line statements of the report: engine behaviour that cannot be verified without running the game
ENGINE_ASSUMPTIONS = (
    'MCC selects one of the three pixel-shader blobs of an element at run time (plain / z-prime / MRT); which one is not verified',
    'nothing was run in the game: every statement is about compiled code, tag structure and register tables',
)


class PortError(Exception):
    pass


class PortDecision(object):
    """What the integrator does with a Vista shader-family tag whose name MCC does not have.
    kind 'port' | 'unportable'.  reason: why unportable (the exact chain, e.g. 'pass X: pixel shader impl 2 ps 1 ...').  notes: facts for the report
    (strategies, MCC passes used, counts).  flags: things that are ported but carry an unverifiable statement (shown in the report)."""
    __slots__ = ('group', 'name', 'kind', 'reason', 'notes', 'flags', 'info')

    def __init__(self, group, name, kind, reason='', notes=None, flags=None, info=None):
        self.group = group
        self.name = name
        self.kind = kind
        self.reason = reason
        self.notes = list(notes or [])
        self.flags = list(flags or [])
        self.info = dict(info or {})

    @property
    def ok(self):
        return self.kind == 'port'

    def as_dict(self):
        info = {k: v for k, v in self.info.items() if k not in ('private_vrtx', 'private_spas') and isinstance(v, (int, float, str, list, dict, tuple, bool, type(None)))}
        for k in ('private_vrtx', 'private_spas'):                      # {vista name (lower): (name, (label, VistaTag), decision)} -> the private tag names
            if self.info.get(k):
                info[k] = sorted(private_water_name(v[0]) for v in self.info[k].values())
        return {'group': self.group, 'name': self.name, 'kind': self.kind, 'reason': self.reason, 'notes': list(self.notes), 'flags': list(self.flags), 'info': info}

    def __repr__(self):
        return '<PortDecision %s %s %s%s>' % (self.kind, self.group, self.name, (' (%s)' % self.reason[:80]) if self.reason else '')


# ================================================================================================
# translation results (memoised by content: the same blobs are translated once per process)
# ================================================================================================
class SpasPort(object):
    """Result of porting one decoded Vista spas.  ok: every Pixel Shader element proven (all present variants) and compiled.
    dxbc {(impl, ps): (blob0, blob1, blob2)} (b'' = the variant is absent in the Vista pass), elements [per-element facts], problems [str], flags [str]"""
    __slots__ = ('ok', 'dxbc', 'elements', 'problems', 'flags', 'impls')

    def __init__(self):
        self.ok = False
        self.dxbc = {}
        self.elements = []
        self.problems = []
        self.flags = []
        self.impls = 0


class VrtxPort(object):
    """Result of porting one decoded Vista vrtx.  dxbc [bytes per Geometry Classification] (b'' = empty slot in the Vista tag)."""
    __slots__ = ('ok', 'dxbc', 'elements', 'problems', 'flags')

    def __init__(self):
        self.ok = False
        self.dxbc = []
        self.elements = []
        self.problems = []
        self.flags = []


_PS_CACHE = {}        # (blob hashes, alpha) -> element result dict
_VS_CACHE = {}        # (PORT_VERSION, blob hash) -> VsResult facts
_SPAS_CACHE = {}
_VRTX_CACHE = {}
_COMPILER = {}


def compiler_status():
    """(ok, text): does the in-process D3DCompiler_47 write the creator string of the MCC tool's compiler (byte-identical DXBC)?"""
    if 'v' not in _COMPILER:
        try:
            from . import shader_compile as SC
            _COMPILER['v'] = SC.check_compiler()
        except Exception as ex:                                       # noqa: BLE001
            _COMPILER['v'] = (False, 'check_compiler failed: %r' % (ex,))
    return _COMPILER['v']


def _h(*parts):
    h = hashlib.sha1()
    for p in parts:
        b = p if isinstance(p, (bytes, bytearray)) else repr(p).encode()
        h.update(len(b).to_bytes(4, 'little'))
        h.update(b)
    return h.hexdigest()


def _alpha_key(alpha):
    return None if alpha is None else tuple(sorted(alpha.items()))


def _ps_element(blobs, alpha, name):
    """Translate + prove + compile one Vista Pixel Shader element.  -> dict(ok, dxbc (3 blobs), strategy, present, notes, err)"""
    key = (_h(*blobs), _alpha_key(alpha))
    hit = _PS_CACHE.get(key)
    if hit is not None:
        return hit
    from . import d3d9_to_psh as D
    out = {'ok': False, 'dxbc': None, 'strategy': '', 'present': (False, False, False), 'notes': [], 'err': None}
    try:
        r = D.translate_ps(list(blobs), name=name)
        out['strategy'] = r.strategy
        out['present'] = tuple(bool(x) for x in r.present)
        out['notes'] = list(r.notes)
        if not r.ok:
            out['err'] = 'translation not proven: %s' % (r.summary(),)
        elif alpha is None:
            out['err'] = 'the alpha-test function of the pass is not supported by the MCC tool (greater / not-equal / less only)'
        else:
            dx = []
            for v in range(3):
                if not r.present[v]:
                    dx.append(b'')
                    continue
                blob = r.compile(v, alpha)
                dx.append(blob)
                if alpha:                                              # prove the alpha-test path as well (render state emulated on the D3D9 side)
                    rep = D.compare_machines(D.D3D9Machine(blobs[v]), D.DxbcMachine(blob), n=48, seed=17 + v, alpha_a=alpha)
                    if not rep.ok:
                        raise PortError('alpha-test path of variant %d not proven: %s' % (v, '; '.join(rep.notes[:2])))
            out['dxbc'] = tuple(dx)
            out['ok'] = True
    except PortError as ex:
        out['err'] = str(ex)
    except Exception as ex:                                            # noqa: BLE001  CompileError / Unsupported / anything: never approximate
        out['err'] = '%s: %s' % (type(ex).__name__, str(ex).strip().split('\n')[0][:200])
    _PS_CACHE[key] = out
    return out


def port_spas(node, name=''):
    """Port every Pixel Shader element of a decoded Vista spas root Node.  Memoised by content (blobs + render-state alpha test)."""
    from . import d3d9_to_psh as D
    els = D.spas_pixel_shaders(node)
    pps = node.blocks.get(ST.SPAS_PP) or []
    n_impl = len(pps[0].blocks.get(ST.SPAS_PP_IMPL) or []) if pps else 0
    key = _h(PORT_VERSION, *[(e['impl'], e['ps'], _h(*e['blobs']), _alpha_key(e['alpha'])) for e in els])
    hit = _SPAS_CACHE.get(key)
    if hit is not None:
        return hit
    sp = SpasPort()
    sp.impls = n_impl
    ok = True
    for el in els:
        i, j = el['impl'], el['ps']
        r = _ps_element(el['blobs'], el['alpha'], name)
        info = {'impl': i, 'ps': j, 'strategy': r['strategy'], 'present': r['present'], 'alpha': el['alpha'], 'ok': r['ok']}
        sp.elements.append(info)
        if not r['ok']:
            ok = False
            sp.problems.append('pixel shader impl %d ps %d: %s' % (i, j, r['err']))
            continue
        sp.dxbc[(i, j)] = r['dxbc']
        if not all(r['present']):
            absent = [k for k in range(3) if not r['present'][k]]
            sp.flags.append('impl %d ps %d: the Vista tag has NO shader for variant slot(s) %s (Bungie\'s compile of that variant failed); left empty exactly as in Vista - '
                            'what the MCC engine does with an empty slot is not verified' % (i, j, absent))
    sp.ok = ok and bool(els)
    if not els:
        sp.problems.append('the pass has no Pixel Shader element')
    _SPAS_CACHE[key] = sp
    return sp


def port_vrtx(node, name=''):
    """Port every geometry classification of a decoded Vista vrtx root Node (D3D9 vs_2_0 -> vs_5_0 DXBC, proven)."""
    from . import d3d9_to_psh as D
    blobs = ST.vista_vertex_blobs(node)
    key = _h(PORT_VERSION, *blobs)
    hit = _VRTX_CACHE.get(key)
    if hit is not None:
        return hit
    vp = VrtxPort()
    ok = True
    for k, b in enumerate(blobs):
        if not b:
            vp.dxbc.append(b'')
            vp.elements.append({'index': k, 'empty': True})
            continue
        fk = _h(PORT_VERSION, b)
        r = _VS_CACHE.get(fk)
        if r is None:
            try:
                res = D.translate_vs(b, name)
                r = {'ok': bool(res.ok and res.dxbc), 'dxbc': res.dxbc if res.ok else b'', 'fixup': res.fixup,
                     'err': None if res.ok else '; '.join(res.notes[:2]) or res.unsupported or 'not proven'}
            except Exception as ex:                                    # noqa: BLE001
                r = {'ok': False, 'dxbc': b'', 'fixup': None, 'err': '%s: %s' % (type(ex).__name__, str(ex).strip().split('\n')[0][:200])}
            _VS_CACHE[fk] = r
        vp.elements.append({'index': k, 'empty': False, 'ok': r['ok'], 'fixup': r['fixup']})
        if not r['ok']:
            ok = False
            vp.problems.append('vertex shader classification %d: %s' % (k, r['err']))
            vp.dxbc.append(b'')
            continue
        vp.dxbc.append(r['dxbc'])
    vp.ok = ok and bool(blobs)
    if not blobs:
        vp.problems.append('the vertex shader has no geometry classification')
    _VRTX_CACHE[key] = vp
    return vp


def private_vrtx_name(vista_vrtx_name):
    """Tag name of the PRIVATE port of a Vista vertex shader whose MCC namesake behaves differently (a map holds one tag per (group, name), so the copy
    gets its own name; the engine reaches vertex shaders through the pass's tag reference, never by name)."""
    return vista_vrtx_name + PRIVATE_SUFFIX


def strip_private_suffix(name):
    """Inverse of private_water_name / private_vrtx_name: the Vista tag name a private port stands for (None stays None).  A stem fingerprint hashes the pass tag NAMES, so a
    private water stem (its passes are '<pass>__vista') equals its Vista stem under the stripped names."""
    return name[:-len(PRIVATE_SUFFIX)] if name and name.lower().endswith(PRIVATE_SUFFIX) else name


def private_water_name(vista_name):
    """Tag name of the PRIVATE port of a Vista stem / pass / vertex shader of a semantically different water chain (shader_tags.SEMANTIC_WATER_CHAINS): '<name>__vista'.
    The same rule as the private vertex shaders (the water_pool_static__vista of the tint chain IS the water chain's vertex shader of that name, one tag per map).  The
    custom-stem names are '<name>__<map file stem>'; the planner never gives them this suffix (_Builder.custom_stem_entry)."""
    return vista_name + PRIVATE_SUFFIX


_PAIR_CACHE = {}
# The vertex constants the ENGINE pins for every draw (not shader data): c4.w = 1, c5.w = 0.5, c6.w = 2 (sub_180955590), c15.y = 0 and the Water Definition constant c21 = (1, -1, -1, 0.5)
# (xmmword_180B3F160, sub_1807F4140).  vs_ps_composite draws every constant at random by default (a pairing that holds for any constants); pin_engine=True fixes these to the engine values
# (with them the donor `water` vertex shader is exact at the vertices, the water_pool / water_pool_static donor pairs are not).
ENGINE_PINNED_VS_CONSTANTS = (((4, 3), 1.0), ((5, 3), 0.5), ((6, 3), 2.0), ((15, 1), 0.0), ((21, 0), 1.0), ((21, 1), -1.0), ((21, 2), -1.0), ((21, 3), 0.5))
PAIR_MAX_BAD_FRACTION = 0.05      # at most this fraction of the random trials may differ ...
PAIR_MAX_DIFF = 0.008             # ... and only by about one step of the 8-bit depth-stripe / alpha quantisation (a float rounding at a frc / compare edge)


def _composite_verdict(pa, pb, n):
    """Compare the pixel-shader results of the two vertex-shader paths: -> (ok, why).  A real behavioural difference changes a large share of the trials
    (water_pool_static: every trial, active camo: 17 % by up to 0.9); a rounding difference of 1 ulp in the vertex shader that a `frc` / `slt` of the pixel
    shader turns into one quantisation step hits at most a trial or two."""
    import numpy as np
    bad = np.zeros(n, dtype=bool)
    killed_diff = pa.killed != pb.killed
    bad |= killed_diff
    live = ~(pa.killed | pb.killed)
    worst = 0.0
    where = ''
    for sem in sorted(set(pa.outputs) & set(pb.outputs)):
        w = pa.written.get(sem, 0) & pb.written.get(sem, 0)
        for lane in range(4):
            if not (w >> lane) & 1:
                continue
            a, b = pa.outputs[sem][:, lane], pb.outputs[sem][:, lane]
            with np.errstate(all='ignore'):
                err = np.abs(a - b)
                bad_l = ~((err <= 1e-5 + 1e-4 * np.maximum(np.abs(a), np.abs(b))) | (a == b) | (~np.isfinite(a) & ~np.isfinite(b))) & live
            bad |= bad_l
            if bad_l.any():
                e = float(np.nanmax(np.where(bad_l & np.isfinite(err), err, 0.0)))
                if e > worst:
                    worst, where = e, '%s lane %s' % (sem, 'xyzw'[lane])
    nb = int(bad.sum())
    if nb == 0:
        return True, ''
    if nb <= max(1, int(PAIR_MAX_BAD_FRACTION * n)) and worst <= PAIR_MAX_DIFF and int(killed_diff.sum()) <= 1:
        return True, 'rounding-level difference in %d of %d trials (max %.4g at %s)' % (nb, n, worst, where)
    return False, 'output %s differs in %d of %d trials (max |diff| %.4g)' % (where or 'kill flags', nb, n, worst)


def vs_ps_composite(vs_vista_blob, vs_mcc_dxbc, ps_blob, n=48, seed=5, pin_engine=False):
    """Does MCC's vertex shader drive the VISTA pixel shader like the Vista vertex shader does?  Random vertex attributes / constants go through the Vista
    vs_2_0 byte code (wrapper model: unwritten lanes COLOR 1,1,1,1 / TEXCOORD 0,0,0,1, like the MCC wrapper) and through MCC's vs_5_0 DXBC; both sets of interpolants
    feed the Vista ps byte code with identical constants, and the pixel outputs / kill flags are compared.  -> (ok, note).  Only lanes the pixel shader reads
    matter (a vertex shader that differs elsewhere, e.g. in the z remap of the position or in an unread texcoord, passes).  pin_engine=True: the vertex constants the engine
    pins (ENGINE_PINNED_VS_CONSTANTS) take their engine values instead of random ones.  This test is POINTWISE (at the vertices): it cannot see a per-vertex division that is only
    wrong between the vertices - only an interpolation-aware check sees that."""
    key = (_h(vs_vista_blob), _h(vs_mcc_dxbc), _h(ps_blob), n, seed, bool(pin_engine))
    hit = _PAIR_CACHE.get(key)
    if hit is not None:
        return hit
    from . import d3d9_to_psh as D
    try:
        a, b, p = D.D3D9Machine(vs_vista_blob), D.DxbcMachine(vs_mcc_dxbc), D.D3D9Machine(ps_blob)
        samp = D.SampleFn()
        sems = set(a.input_semantics()) | set(b.input_semantics())
        cvs, vin = D.random_trials(n, seed, sems, ncon=256)
        if pin_engine:
            for (reg, lane), val in ENGINE_PINNED_VS_CONSTANTS:
                cvs[:, reg, lane] = val
        ra, rb = a.run(cvs, vin, samp), b.run(cvs, vin, samp)
        exp = D.vs_expected(ra, cvs, False)
        cps, _unused = D.random_trials(n, seed + 1, set(), ncon=64)
        pin_a, pin_b = {}, {}
        extra = D.random_trials(n, seed + 2, set(p.input_semantics()), ncon=1)[1]
        for sem in p.input_semantics():
            if sem[0] in ('COLOR', 'TEXCOORD'):
                pin_a[sem] = exp[sem]
                pin_b[sem] = rb.outputs.get(sem, exp[sem])
            else:
                pin_a[sem] = pin_b[sem] = extra[sem]
        pa, pb = p.run(cps, pin_a, samp), p.run(cps, pin_b, samp)
        out = _composite_verdict(pa, pb, n)
    except Exception as ex:                                            # noqa: BLE001  anything unevaluable is NOT verified
        out = (False, 'not evaluable: %s: %s' % (type(ex).__name__, str(ex)[:160]))
    _PAIR_CACHE[key] = out
    return out


def validate_ported_spas(root, vista_node):
    """ShaderTagError-free validation of a built MCC spas: ST.validate_spas minus the slots that are empty in the Vista tag as well."""
    probs = ST.validate_spas(root, allow_null_vs=True)
    empty = set()
    for (i, j), bl in ST.vista_pixel_blobs(vista_node).items():
        for k, b in enumerate(bl):
            if not b:
                empty.add('impl %d ps %d slot %d: empty' % (i, j, k))
    return [p for p in probs if p not in empty]


# ================================================================================================
# name helpers
# ================================================================================================
def vista_name_of(cache):
    def name_of(d):
        if d in (NULL, 0):
            return None
        i = d & 0xFFFF
        t = cache.tags[i] if i < len(cache.tags) else None
        return (t.name or '').lower() if t is not None and t.datum == d and t.name else None
    return name_of


def mcc_name_of(cache):
    def name_of(d):
        t = cache.tag_by_datum(d)
        return (t.name or '').lower() if t and t.name else None
    return name_of


def _text_of(cache):
    def text_of(sid):
        try:
            return cache.string_id(sid)
        except Exception:                                              # noqa: BLE001
            return None
    return text_of


# ================================================================================================
# the policy
# ================================================================================================
class PortPolicy(object):
    """Decides, for a Vista stem / spas / vrtx tag whose NAME MCC does not have, whether it is ported or unportable.

        env       planner RevEnv (env.mcc = MccSources; env.lay_vista / env.lay_mcc)
        resolve   callable(label, datum) -> (label', VistaTag) | None     (RevPlan.resolve_vista: shared placeholders resolve to shared.map)
        cache_of  callable(label) -> VistaCache                            (RevPlan.vista_cache)

    Decisions are memoised per (group, name).  A decision is a DRY RUN of the real build: the tag is built with the shader_tags builders against
    synthetic datums, validated and encoded/decoded, so a 'port' decision means the convert-time handler will succeed."""

    def __init__(self, env, resolve, cache_of, opts=None):
        self.env = env
        self.mcc = env.mcc.build() if hasattr(env.mcc, 'build') else env.mcc
        self.resolve = resolve
        self.cache_of = cache_of
        self.opts = dict(opts or {})
        self.memo = {}
        self.stack = set()
        self._mcc_nodes = {}
        self._mcc_cls = {}
        from . import transcode as X
        self.lay_v = X.layouts('vista')
        self.lay_m = X.layouts('mcc')
        self.align = X.mcc_align_table()
        self.notes = {'ported': {g: [] for g in GROUPS}, 'unportable': [], 'flags': [], 'donor_audit': [], 'mcc_pass_use': []}

    # ---- the water chains whose MCC namesakes are other shaders ---------------------------------------------------------------
    def water_member(self, group, name):
        """True when the Vista tag (group, name) is on the registry of semantically different water chains (shader_tags.SEMANTIC_WATER_CHAINS) and the option water_chain is
        'vista' (default): MCC's tag of that name is another SHADER, so the Vista tag gets a PRIVATE port ('<name>__vista') instead of MCC's tag."""
        return self.opts.get('water_chain', 'vista') == 'vista' and ST.is_water_chain(group, name)

    # ---- the sky vertex shaders that pin the clip depth to the far plane ------------------------------------------------------------
    def sky_pin_member(self, name):
        """True when the vertex shader `name` is on shader_tags.SKY_DEPTH_PINNED_VRTX and the option sky_depth is 'vista' (default): MCC's same-name tag writes real depth where the
        Vista one pins it to the far plane, so the Vista tag is ported under the SAME name (every non-empty classification must translate with the pin, else the decision is
        'unportable' and the planner keeps MCC's donor with the warning sky_pin_fallback).  'mcc' keeps MCC's donor."""
        return str(self.opts.get('sky_depth', 'vista')).lower() == 'vista' and ST.is_sky_pinned_vrtx(name)

    # ---- MCC side -----------------------------------------------------------------------------------
    def mcc_find(self, group, name):
        return self.mcc.find(group, name)

    def mcc_node(self, group, name):
        """(Node, MccCache) of MCC's tag, or (None, None)."""
        k = (group, name.lower())
        if k not in self._mcc_nodes:
            d = self.mcc.find(group, name)
            hit = (None, None)
            if d is not None:
                cache, tag = self.mcc.tag(d)
                hit = (T.decode_tag(lambda a, n, c=cache: c.read_at_addr(a, n), self.lay_m[group], tag.addr, group), cache)
            self._mcc_nodes[k] = hit
        return self._mcc_nodes[k]

    def vista_node(self, group, label, vtag):
        cache = self.cache_of(label)
        return T.decode_tag(cache.read_addr, self.lay_v[group], vtag.addr, group), cache

    # ---- decisions ----------------------------------------------------------------------------------------
    def decide(self, group, name, label, vtag):
        """Port decision for the Vista tag (label = the Vista map holding its data: 'src' | 'shared' | ...)."""
        k = (group, name.lower())
        hit = self.memo.get(k)
        if hit is not None:
            return hit
        if k in self.stack:
            return PortDecision(group, name, 'unportable', 'cyclic reference between shader tags')
        self.stack.add(k)
        try:
            try:
                if group == 'vrtx':
                    d = self._decide_vrtx(name, label, vtag)
                elif group == 'spas':
                    d = self._decide_spas(name, label, vtag)
                elif group == 'stem':
                    d = self._decide_stem(name, label, vtag)
                else:
                    d = PortDecision(group, name, 'unportable', 'not a shader-template-family group')
            except Exception as ex:                                    # noqa: BLE001  a decision never aborts the plan
                d = PortDecision(group, name, 'unportable', 'port analysis failed: %s: %s' % (type(ex).__name__, str(ex)[:200]))
        finally:
            self.stack.discard(k)
        self.memo[k] = d
        if d.kind == 'port':
            self.notes['ported'][group].append(name)
            for f in d.flags:
                self.notes['flags'].append({'group': group, 'name': name, 'flag': f})
        else:
            self.notes['unportable'].append({'group': group, 'name': name, 'reason': d.reason})
        return d

    def _synth(self, cache):
        keys = {}

        def mdatum(g, n):
            return keys.setdefault((g, n.lower()), 0xE1000000 | (len(keys) + 1))

        def msid(text):
            return (len(text) << 24) | (len(keys) + 1000)
        res = ST.NameResolver(cache, mdatum, msid, self.lay_m, self.lay_v)
        return res, keys

    def _dry_encode(self, group, root):
        """encode -> decode round trip of a built tag with the MCC alignment table; '' when the tree survives"""
        out = T.encode_tag(root, self.lay_m[group], 0x1000, self.align)
        back = T.decode_tag(lambda a, n: out[a - 0x1000:a - 0x1000 + n], self.lay_m[group], 0x1000, group)
        ok, why = T.same_tree(root, back)
        return '' if ok else 'encode/decode round trip differs: %s' % (why,)

    def _decide_vrtx(self, name, label, vtag):
        node, cache = self.vista_node('vrtx', label, vtag)
        vp = port_vrtx(node, name)
        if not vp.ok:
            return PortDecision('vrtx', name, 'unportable', '; '.join(vp.problems[:3]))
        live = [e for e in vp.elements if not e.get('empty')]
        remap = sum(1 for e in live if e.get('fixup') is True)               # fixup is tri-state: False (none) | True (z = w * c10.w + c10.z) | 'pin' (z = 0)
        pinned = sum(1 for e in live if e.get('fixup') == 'pin')
        sky = self.sky_pin_member(name)
        if sky and pinned != len(live):                                       # never a half-pinned tag: a sky tag with one classification at real depth is the bug itself
            return PortDecision('vrtx', name, 'unportable', 'sky vertex shader %s: %d of %d non-empty geometry classification(s) translate to the far-plane depth pin (Vista clip z == '
                                'clip w); a partly pinned tag is never built' % (name.split('\\')[-1], pinned, len(live)))
        res, _keys = self._synth(cache)
        root = ST.build_mcc_vrtx(res, node, vp.dxbc)
        probs = ST.validate_vrtx(root)
        if not probs:
            w = self._dry_encode('vrtx', root)
            probs = [w] if w else []
        if probs:
            return PortDecision('vrtx', name, 'unportable', 'built tag does not validate: %s' % '; '.join(probs[:3]))
        info = {'classifications': len(vp.elements), 'z_remap': remap, 'z_pin': pinned}
        if sky:
            info['sky_pin'] = True
        return PortDecision('vrtx', name, 'port', notes=['%d geometry classification(s), %d translated (z-remap on %d, z pinned to the far plane on %d), %d empty in the Vista tag; '
                                                         'all proven by evaluation' % (len(vp.elements), len(live), remap, pinned, len(vp.elements) - len(live))],
                            flags=[], info=info)                  # the engine supplies c10 to ported vertex shaders: verified (ENGINE_VERIFIED), not a flag

    def _vs_refs(self, node, cache, label):
        """[(implementation index, vrtx name | None, (label', VistaTag) | None)] of every implementation's Vertex Shader reference of a Vista spas"""
        out = []
        pps = node.blocks.get(ST.SPAS_PP) or []
        if not pps:
            return out
        for i, impl in enumerate(pps[0].blocks.get(ST.SPAS_PP_IMPL) or []):
            d = impl.u32(ST.SPAS_IMPL_VERTEX_SHADER + 4)
            if d in (NULL, 0):
                continue
            r = self.resolve(label, d)
            if r is None or not r[1].name:
                out.append((i, None, None))
            else:
                out.append((i, r[1].name, r))
        return out

    def _pairing(self, vname, r, ps_blobs):
        """Does MCC's vertex shader `vname` drive the Vista pixel shaders `ps_blobs` (D3D9) like the Vista vertex shader does, classification by
        classification?  -> (ok, why)"""
        vnode, _vc = self.vista_node('vrtx', r[0], r[1])
        mnode, _mc = self.mcc_node('vrtx', vname)
        vb, mb = ST.vista_vertex_blobs(vnode), ST.mcc_vertex_blobs(mnode, 'DX11')
        if len(vb) != len(mb):
            return False, 'MCC\'s vertex shader has %d geometry classifications, the Vista one %d' % (len(mb), len(vb))
        seen = set()
        n = 0
        for k, (a, b) in enumerate(zip(vb, mb)):
            if bool(a) != bool(b):
                return False, 'classification %d is %s in MCC and %s in Vista' % (k, 'empty' if not b else 'present', 'empty' if not a else 'present')
            if not a:
                continue
            for pb in ps_blobs:
                key = (k, _h(pb))
                if key in seen:
                    continue
                seen.add(key)
                ok, why = vs_ps_composite(a, b, pb)
                n += 1
                if not ok:
                    return False, 'classification %d drives a Vista pixel shader differently (%s)' % (k, why)
        return True, '%d vertex-shader/pixel-shader combinations evaluated equal' % n

    def _private_vrtx(self, vname, r):
        """A PRIVATE port of the Vista vertex shader `vname` for a ported pass, used when MCC's tag of the same name behaves differently (the name is taken
        by MCC's tag, which MCC's own passes use).  -> PortDecision (kind 'port' = usable; info['dxbc_ok'])"""
        k = ('private', vname.lower())
        hit = self.memo.get(k)
        if hit is None:
            hit = self.memo[k] = self._decide_vrtx(vname, r[0], r[1])
        return hit

    def _decide_spas(self, name, label, vtag):
        from . import d3d9_to_psh as D
        node, cache = self.vista_node('spas', label, vtag)
        water_pass = self.water_member('spas', vtag.name or name)      # the private port of a Vista water pass (MCC's pass of that name is another shader)
        sp = port_spas(node, name)
        if not sp.ok:
            return PortDecision('spas', name, 'unportable', '; '.join(sp.problems[:3]) + (' (+%d more)' % (len(sp.problems) - 3) if len(sp.problems) > 3 else ''))
        notes = []
        flags = list(sp.flags)
        ps_by_impl = collections.defaultdict(list)
        for el in D.spas_pixel_shaders(node):
            ps_by_impl[el['impl']] += [b for b in el['blobs'] if b]
        uses = collections.OrderedDict()                                  # vrtx name (lower) -> [name, r, [D3D9 pixel shader blobs of the implementations using it]]
        for i, vname, r in self._vs_refs(node, cache, label):
            if vname is None:
                return PortDecision('spas', name, 'unportable', 'a Vertex Shader reference of the pass does not resolve to a tag')
            u = uses.setdefault(vname.lower(), [vname, r, []])
            u[2] += ps_by_impl.get(i, [])
        private = {}
        for vl, (vname, r, ps_blobs) in uses.items():
            short = vname.split('\\')[-1]
            if self.mcc_find('vrtx', vname) is not None:
                ok, why = self._pairing(vname, r, ps_blobs)
                registry = self.water_member('vrtx', vname)
                if ok and not registry:
                    notes.append('vertex shader %s: MCC\'s own tag, drives the Vista pixel shaders like the Vista one (%s)' % (short, why))
                    continue
                if ok:                                                  # on the registry although the sampled pairing test found no difference: the registry (a measured reason) wins
                    why = 'the sampled pairing test finds no difference, but MCC\'s vertex shader is on the registry of semantically different water chains: %s' % (
                        ST.water_chain_reason('vrtx', vname)[:160],)
                pv = self._private_vrtx(vname, r)
                if not pv.ok:
                    return PortDecision('spas', name, 'unportable', 'vertex shader %s exists in MCC but behaves differently from the Vista one (%s) and the Vista vertex '
                                        'shader cannot be ported either: %s' % (short, why, pv.reason))
                private[vl] = (vname, r, pv)
                notes.append('vertex shader %s: MCC\'s tag of that name differs from the Vista one (%s) -> PRIVATE port of the Vista vertex shader for this pass' % (short, why))
                flags += pv.flags
                continue
            sub = self.decide('vrtx', vname, r[0], r[1])
            if not sub.ok:
                return PortDecision('spas', name, 'unportable', 'vertex shader %s: %s' % (short, sub.reason))
            notes.append('vertex shader %s: ported' % short)
            flags += sub.flags
        res, _keys = self._synth(cache)
        root = ST.build_mcc_spas(res, node, sp.dxbc)
        probs = validate_ported_spas(root, node)
        if not probs:
            w = self._dry_encode('spas', root)
            probs = [w] if w else []
        if probs:
            return PortDecision('spas', name, 'unportable', 'built tag does not validate: %s' % '; '.join(probs[:3]))
        strat = collections.Counter(e['strategy'] for e in sp.elements)
        if water_pass:
            notes.insert(0, 'PRIVATE port of the Vista water pass (Texture States / Render States / Externs / implementation flags / pixel shaders with the cube sampler kept, all verbatim): '
                            'MCC\'s pass of that name is another shader (%s)' % (ST.water_chain_reason('spas', vtag.name or name) or '')[:200])
        notes.insert(0, '%d implementation(s), %d pixel shader element(s) x up to 3 variants proven (%s)%s' % (
            sp.impls, len(sp.elements), ', '.join('%s %d' % kv for kv in sorted(strat.items())),
            '; alpha test on %d' % sum(1 for e in sp.elements if e['alpha']) if any(e['alpha'] for e in sp.elements) else ''))
        info = {'impls': sp.impls, 'elements': len(sp.elements), 'private_vrtx': {vl: (v[0], v[1], v[2]) for vl, v in private.items()}}
        if water_pass:
            info['water_chain'] = True
        return PortDecision('spas', name, 'port', notes=notes, flags=flags, info=info)

    def _mcc_pass_class(self, vnode, vcache, pname):
        """('identical'|'differs', MCC implementation count, precedent count) of MCC's pass of that name against the Vista pass `vnode`"""
        mnode, mcache = self.mcc_node('spas', pname)
        a = ST.classify_spas(vnode, vista_name_of(vcache), _text_of(vcache), mnode, mcc_name_of(mcache), _text_of(mcache))
        pps = mnode.blocks.get(ST.SPAS_PP) or []
        n_impl = len(pps[0].blocks.get(ST.SPAS_PP_IMPL) or []) if pps else 0
        prec = ST.MCC_EDITED_SPAS.get(pname.lower(), (0, ''))
        return a, n_impl, prec[0], prec[1]

    def _decide_stem(self, name, label, vtag):
        node, cache = self.vista_node('stem', label, vtag)
        pps = node.blocks.get(ST.STEM_PP) or []
        if not pps:
            return PortDecision('stem', name, 'unportable', 'the stem has no Postprocess Definition')
        notes = []
        flags = []
        pass_impls = {}                                                 # pass name (lower) -> implementation count
        names = []
        for e in pps[0].blocks.get(0x10) or []:
            d = e.u32(4)
            if d in (NULL, 0):
                return PortDecision('stem', name, 'unportable', 'a Pass reference of the stem is null')
            r = self.resolve(label, d)
            if r is None or not r[1].name:
                return PortDecision('stem', name, 'unportable', 'a Pass reference of the stem does not resolve to a tag')
            names.append((r[1].name, r))
        private_spas = {}                                               # Vista pass name (lower) -> (Vista pass name, (label, VistaTag), PortDecision of the PRIVATE pass)
        for pname, (plabel, ptag) in names:
            pl = pname.lower()
            if pl in pass_impls:
                continue
            if self.water_member('spas', pname):
                # MCC's pass of this name is another SHADER (shader_tags.SEMANTIC_WATER_CHAINS): the Vista pass is ported under a private name, never replaced by MCC's
                pn = private_water_name(pname)
                sub = self.decide('spas', pn, plabel, ptag)
                if not sub.ok:
                    return PortDecision('stem', name, 'unportable', 'pass %s (private Vista port %s): %s' % (pname.split('\\')[-1], pn.split('\\')[-1], sub.reason))
                pass_impls[pl] = sub.info.get('impls')
                private_spas[pl] = (pname, (plabel, ptag), sub)
                notes.append('pass %s: PRIVATE port of the Vista pass as %s - MCC\'s pass of that name is another shader' % (pname.split('\\')[-1], pn.split('\\')[-1]))
                flags += sub.flags
                continue
            if self.mcc_find('spas', pname) is not None:
                vp_node, vp_cache = self.vista_node('spas', plabel, ptag)
                cls, n_impl, prec, what = self._mcc_pass_class(vp_node, vp_cache, pname)
                pass_impls[pl] = n_impl
                if cls == 'identical':
                    notes.append('pass %s: MCC\'s own pass (identical structure)' % pname.split('\\')[-1])
                elif prec >= 1:
                    notes.append('pass %s: MCC\'s re-authored pass (%s); %d stock Vista-structured stem(s) identical to their Vista twin use it' % (pname.split('\\')[-1], what, prec))
                    flags.append('uses MCC\'s re-authored pass %s (%s), verified by %d stock precedent(s)' % (pname.split('\\')[-1], what, prec))
                else:
                    return PortDecision('stem', name, 'unportable',
                                        'pass %s exists in MCC with a DIFFERENT structure%s and no stock stem that equals its Vista twin is known to work with MCC\'s version '
                                        '(0 precedents); porting the Vista pass under the same name would replace the tag every MCC donor of the map uses' % (
                                            pname.split('\\')[-1], (' (' + what + ')') if what else ''))
            else:
                sub = self.decide('spas', pname, plabel, ptag)
                if not sub.ok:
                    return PortDecision('stem', name, 'unportable', 'pass %s: %s' % (pname.split('\\')[-1], sub.reason))
                pass_impls[pl] = sub.info.get('impls')
                notes.append('pass %s: ported' % pname.split('\\')[-1])
                flags += sub.flags
        res, keys = self._synth(cache)
        root = ST.build_mcc_stem(res, node)
        probs = ST.validate_stem(root)
        rev = {v: k[1] for k, v in keys.items() if k[0] == 'spas'}
        bad = ST.check_stem_pass_coupling(root, lambda d: pass_impls.get(rev.get(d, '')))
        if bad:
            probs.append('implementation counts of the stem and its passes disagree: %s' % (bad[:3],))
        if not probs:
            w = self._dry_encode('stem', root)
            probs = [w] if w else []
        if probs:
            return PortDecision('stem', name, 'unportable', 'built tag does not validate: %s' % '; '.join(probs[:3]))
        lods = len(pps[0].blocks.get(0x00) or [])
        # drop duplicate flags, keep order
        seen = set()
        flags = [f for f in flags if not (f in seen or seen.add(f))]
        info = {'lods': lods, 'passes': len(names)}
        if private_spas:
            info['private_spas'] = private_spas
            info['water_chain'] = True
        return PortDecision('stem', name, 'port', notes=notes, flags=flags, info=info)

    # ---- names MCC has: is the Vista tag really the stock template? ---------------------------------------
    def audit_donor(self, group, name, label, vtag):
        """The planner uses MCC's tag of this name.  Returns flag texts when the Vista tag is NOT interchangeable with it by a measured reason
        or an unknown one (never raises)."""
        out = []
        try:
            if group == 'stem':
                node, cache = self.vista_node('stem', label, vtag)
                mnode, mcache = self.mcc_node('stem', name)
                if mnode is None:
                    return out
                from .handlers import shaders as RS
                vsd = RS.stem_data(node, RS._vista_name_of(cache), name)
                msd = RS.stem_data(mnode, RS._mcc_name_of(mcache), name)
                if vsd is None or msd is None or vsd.fp == msd.fp:
                    return out
                if name.lower() in ST.MCC_RESTRUCTURED_STEMS:
                    out.append('stem %s: MCC ships another revision of this template (measured restructure); the shaders are re-mapped onto it (engine register pairing, template-order bitmap names, register structure)' % name.split('\\')[-1])
                else:
                    out.append('stem %s: the Vista stem differs from MCC\'s stem of the same name and the name is NOT a known MCC re-authoring - custom content under a stock '
                               'name; a name MCC has cannot be ported (one tag per name, MCC donors use it); the shaders are mapped onto MCC\'s stem and the Vista features '
                               'it has no slot for are reported as dropped' % name.split('\\')[-1])
            elif group == 'spas':
                node, cache = self.vista_node('spas', label, vtag)
                cls, n_impl, prec, what = self._mcc_pass_class(node, cache, name)
                if cls == 'differs' and name.lower() not in ST.MCC_EDITED_SPAS:
                    out.append('pass %s: differs from MCC\'s pass of the same name and the name is NOT a known MCC re-authoring - custom content under a stock name; MCC\'s pass is used' % name.split('\\')[-1])
        except Exception as ex:                                        # noqa: BLE001
            out.append('%s %s: donor audit failed: %r' % (group, name, ex))
        for t in out:
            self.notes['donor_audit'].append({'group': group, 'name': name, 'text': t})
        return out
