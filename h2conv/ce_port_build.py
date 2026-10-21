# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""DEVELOPER tool (the converter itself never calls it; it is included so the data can be rebuilt and checked): builds ``h2conv/data/ce_port/`` for ``h2conv.ce_port``.

    py -3 -m h2conv.ce_port_build build  --mod DIR --kit DIR [--vista-maps DIR] [--out DIR] [--report FILE] [--only substring ...]

Inputs (all read-only): the workshop mod folder (``maps\\*.map`` + ``halo2\\h2_maps_win64_dx11\\shared.map``), the H2EK folder (``tags\\rasterizer\\pixel_shaders_dx9\\*.psh``
and ``tags\\...\\*.shader_pass``), the stock Vista maps.  Needs D3DCompiler_47.dll (Windows).  See ``h2conv/ce_port.py`` for the route and ``docs/HOW_IT_WORKS.md`` (section 10.3).

Per target stem: every pass it names is
  * a Vista pass of that name whose structure the stem can use (equal, or equal but for render / texture states, which belong to the engine's pass, e.g. the
    MCC re-authorings of shadow_generate / default_transparent) -> nothing to do, the Vista donor is used;
  * a Vista pass of that name with another structure (``lightmap_tex_detail_alpha_test``: MCC dropped a texture) -> ported under ``<name>__ce``;
  * a pass Vista does not have -> ported.
A ported pass is entered into the table only when every pixel shader blob of every element is proven and every vertex shader it names is a Vista vertex shader
that drives the ported pixel shaders like MCC's does.  A stem is registered only when all its passes are available.

Lane reconciliation.  MCC's vertex-shader wrapper defines every lane a shader leaves unwritten as a constant (TEXCOORD z = 0, w = 1) where the Vista shader of the
same name writes the constant 1 into the z lane of a 2-D texture coordinate (``rev_d3d9`` section 5).  A pixel shader that READS such a lane (a cube map lookup
with a 2-D coordinate: ``lightmap_tex_detail_alpha_test_detail`` implementation 1, ``tc4.z``) sees 0 in MCC and 1 with Vista's vertex shader.  When - and only when -
every lane the pixel shader reads that differs is a constant in BOTH vertex shaders and the constants agree over all vertex classes, the Vista pixel shader is compiled
with that lane pinned to MCC's constant (``tc4.z = 0``), so that the pair (Vista vertex shader, ported pixel shader) computes what MCC's pair computes.  Recorded in the
registry (``lane_fixes``) and in the report; anything else that differs makes the pass unportable.

Private vertex shaders.  When the Vista vertex shader of a pass's name does NOT drive the ported pixel shaders like MCC's (the composite test fails and no constant lane
explains it: ``water_pool_static`` of beavercreek, MCC computes a per-vertex divided screen uv where Vista writes the reflection vector) the MCC vertex shader itself is
ported to ``vs_2_0`` under the private name ``<name>__ce`` (``Builder.port_vrtx``): the H2EK ``.vertex_shader`` tag holds the HLSL text of every vs_5_0 blob, the text must
reproduce the map's DXBC BYTE FOR BYTE (compiled with /Gis), and its Direct3D 9 form (``dx11to9.vs_to_d3d9``: the SV_POSITION semantic and MCC's z-remap statement differ,
nothing else) is compiled for vs_2_0 and proven lane for lane against the DXBC (``prove_vs``) and, as a chain with every ported pixel shader, against MCC's chain
(``chain_check``).  The registry gets a ``vrtx`` table; the pass entry lists ``private_vrtx``.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:                                                     # noqa: BLE001  pragma: no cover
    pass

import argparse
import collections
import hashlib
import json
import os
import re
import struct
import sys
import time

import numpy as np

from . import ce_port as CP
from . import tagtree as T
from . import dxbc9
from .mcc_cache import MccCache
from .vista_cache import VistaCache
from .rev import d3d9_to_psh as D
from .rev import shader_compile as SC
from .rev import shader_port as SP
from .transcode import layouts

# ---- what to port: the 30 stems that the batch of 2026-10-20 dropped (plan.no_donor, tag type stem) in the 12 Halo CE maps; their passes come from the closure ------------------
MAPS = ('beavercreek', 'bloodgulch', 'chillout', 'gephyrophobia', 'hangemhigh', 'icefields', 'longest', 'prisoner', 'putput', 'ratrace', 'sidewinder', 'wizard')
TARGET_STEMS = (
    'cann\\shaders\\add_generic', 'cann\\shaders\\add_generic_single', 'cann\\shaders\\add_generic~multi', 'cann\\shaders\\add_plasma~offset',
    'cann\\shaders\\add_plasma~shield', 'cann\\shaders\\alpha_generic', 'cann\\shaders\\alpha_generic~illum', 'cann\\shaders\\alpha_multiblend',
    'cann\\shaders\\opaque_2cc~illum', 'cann\\shaders\\opaque_2cc~mp', 'cann\\shaders\\opaque_2cc~mp_illum', 'cann\\shaders\\opaque_generic',
    'cann\\shaders\\opaque_generic~illum', 'cann\\shaders\\opaque_generic~plasma', 'cann\\shaders\\opaque_strata', 'cann\\shaders\\opaque_unlit',
    'cann\\shaders\\transp_generic', 'cann\\shaders\\transp_glass', 'cann\\shaders\\transp_glass~dynamic', 'cann\\shaders\\terrain_generic',
    'cann\\shaders\\lightmap', 'cann\\shaders\\water_generic~masked', 'cann\\templates\\opaque_unlit', 'cann\\templates\\opaque~plasma', 'cann\\templates\\reflect',
    'h1\\shaders\\scopeblur', 'shaders\\shader_templates\\opaque\\overlay_stacked', 'shaders\\shader_templates\\transparent\\meter_plasma_rifle',
    'shaders\\shader_templates\\transparent\\one_alpha_env_illum_plasma', 'shaders\\shader_templates\\transparent\\plasma_mask_offset_teleporter')
# render / texture state fields: a pass that differs from Vista's ONLY here keeps the Vista pass (the stem's Remappings address textures / constants, not states)
STATE_FIELDS = ('Render States', 'Texture States', 'Render States Block Index Data', 'Texture States Block Index Data', 'Default Render States Block Index Data',
                'Render State Info Block Index Data', 'Texture State Info Block Index Data', 'Render State Externs Block Index Data', 'State Externs Block Index Data')
D3D9_PROFILES = ('ps_2_0', 'ps_2_0', 'ps_3_0')
D3D9_FLAGS = 1 << 12                  # D3DCOMPILE_ENABLE_BACKWARDS_COMPATIBILITY, like the H2EK tool (/Gec)
VS_PROFILE = 'vs_2_0'
VS_DX11_FLAGS = 1 << 13               # D3DCOMPILE_IEEE_STRICTNESS (fxc /Gis): reproduces the MCC tool's vs_5_0 DXBC byte for byte
VS_D3D9_FLAGS = 1 << 13               # the same flag for vs_2_0: the evaluation order of the DX11 text is kept (IEEE strictness)
VS_OUTPUTS = (('POSITION', 0), ('COLOR', 0), ('COLOR', 1)) + tuple(('TEXCOORD', i) for i in range(8))
LANES = 'xyzw'


def _log(msg):
    print(msg, flush=True)


# ================================================================================================
# helpers: names, nodes, Vista index
# ================================================================================================
def mcc_funcs(cache):
    def name_of(d):
        if d in (0xFFFFFFFF, 0):
            return None
        t = cache.tag_by_datum(d)
        return (t.group, (t.name or '').lower()) if t is not None else ('?', d)

    def sid_of(s):
        if s in (0, 0xFFFFFFFF):
            return ''
        try:
            return cache.string_id(s)
        except Exception:                                            # noqa: BLE001
            return '<bad %08X>' % s
    return name_of, sid_of


def vista_funcs(cache):
    def name_of(d):
        if d in (0xFFFFFFFF, 0):
            return None
        i = d & 0xFFFF
        t = cache.tags[i] if i < len(cache.tags) else None
        return (t.group, (t.name or '').lower()) if t is not None and t.datum == d else ('?', d)
    return name_of


def blk(node, name):
    f = node.field(name)
    return node.blocks.get(f.offset, []) if f is not None else []


class Env(object):
    def __init__(self, mod, kit, vista_maps, maps=MAPS):
        self.mod, self.kit, self.vista_dir = mod, kit, vista_maps
        self.notes = []
        self.lm, self.lv = layouts('mcc'), layouts('vista')
        self.shared = MccCache(os.path.join(mod, 'halo2', 'h2_maps_win64_dx11', 'shared.map'))
        self.maps = collections.OrderedDict((m, MccCache(os.path.join(mod, 'maps', m + '.map'))) for m in maps)
        self.psh_dir = os.path.join(kit, 'tags', 'rasterizer', 'pixel_shaders_dx9')
        self.vista = {}
        self.vidx = collections.OrderedDict()
        for fn in sorted(os.listdir(vista_maps), key=lambda f: (f.lower() != 'shared.map', f.lower())):
            if not fn.lower().endswith('.map'):
                continue
            try:
                c = VistaCache(os.path.join(vista_maps, fn))
            except Exception:                                        # noqa: BLE001
                continue
            self.vista[fn] = c
            for t in c.tags:
                if t.group in ('stem', 'spas', 'vrtx') and t.addr and t.name:
                    self.vidx.setdefault((t.group, t.name.lower()), (fn, t))

    def find_mcc(self, group, name):
        """-> (tag, cache, where) of the MCC tag with data: the mod's shared.map first, then the maps"""
        for where, c in [('shared', self.shared)] + list(self.maps.items()):
            t = c.tag_by_name(group, name)
            if t is not None and not t.is_null and t.has_data:
                return t, c, where
        return None, None, None

    def find_all(self, group, name):
        """every copy of the MCC tag with data: [(tag, cache, where)] - a template that lives in the maps (not in the mod's shared.map) exists once per map"""
        out = []
        for where, c in [('shared', self.shared)] + list(self.maps.items()):
            t = c.tag_by_name(group, name)
            if t is not None and not t.is_null and t.has_data:
                out.append((t, c, where))
        return out

    def digests(self, group, name):
        """digests (sorted, unique) of the copies of the tag that carry the SAME content as the first copy - for a stem the same pass names, for a pass the same
        DXBC blobs in the same order; the registry accepts any of them (a copy that differs is reported and not registered)"""
        out, ref = set(), None
        for t, c, where in self.find_all(group, name):
            root = self.dec_mcc(c, t)
            nm, sd = mcc_funcs(c)
            if group == 'stem':
                pp = blk(root, 'Postprocess Definition')[0]
                content = tuple((self.resolve(c, k.u32(k.field('Pass').offset + 4))[0] or t).name for k in blk(pp, 'Passes'))
            elif group == 'vrtx':
                content = tuple(hashlib.md5(bytes(el.datas.get(el.field('Compiled Shader').offset, b''))).hexdigest() for el in blk(root, 'Geometry Classifications DX11'))
            else:
                content = tuple(hashlib.md5(b).hexdigest() if b else '' for e in D.spas_pixel_shaders(root) for b in e['blobs'])
            if ref is None:
                ref = content
            if content != ref:
                self.notes.append('%s %s: the copy in %s differs in content and is not registered' % (group, name, where))
                continue
            out.add(CP.digest(root, nm, sd))
        return sorted(out)

    def dec_mcc(self, cache, tag):
        return T.decode_tag(lambda a, n: cache.read_at_addr(a, n), self.lm[tag.group], tag.addr, tag.group)

    def vista_tag(self, group, name):
        hit = self.vidx.get((group, name.lower()))
        if hit is None:
            return None, None
        fn, t = hit
        c = self.vista[fn]
        return (c, t), T.decode_tag(c.read_addr, self.lv[group], t.addr, group)

    def resolve(self, cache, datum):
        """MCC tag (following an external tag into the mod's shared.map) and the cache that holds it"""
        t = cache.tag_by_datum(datum)
        if t is not None and t.external:
            return self.shared.tags[t.index], self.shared
        return t, cache


# ================================================================================================
# structure comparison of a pass that exists in both games
# ================================================================================================
def _flat(node, nm, path=''):
    out = {}
    d = bytes(node.data)
    seen = collections.Counter()
    for f in node.fields:
        if f.name and 'Runtime' in f.name:
            continue
        seen[f.name] += 1
        key = (path, f.name, seen[f.name])             # by NAME + ordinal: the two games lay a struct out differently (Pixel Shader element 0x38 vs 0x2C)
        if f.type == 'tagref':
            off = f.offset + (4 if f.size == 8 else 0)
            out[key] = ('ref', nm(int.from_bytes(d[off:off + 4], 'little')))
        elif f.type in T.BLOCK_TYPES:
            kids = node.blocks.get(f.offset, [])
            out[key] = ('count', len(kids))
            for i, k in enumerate(kids):
                out.update(_flat(k, nm, path + '/%s[%d]' % (f.name, i)))
        elif f.type in T.DATA_TYPES:
            out[key] = ('code', len(node.datas.get(f.offset, b'')) > 0) if f.name == 'Compiled Shader' else ('data', bytes(node.datas.get(f.offset, b'')))
        else:
            out[key] = ('v', d[f.offset:f.offset + f.size])
    return out


def pass_compat(mnode, mname, vnode, vname):
    """('equal'|'state-only'|'incompatible', [differing fields]) of an MCC and a Vista spas (Postprocess Definition [0] only, code ignored)."""
    a = _flat(blk(mnode, 'Postprocess Definition')[0], mname)
    b = _flat(blk(vnode, 'Postprocess Definition')[0], vname)
    diff = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
    if not diff:
        return 'equal', []

    def is_state(k):
        path, nm, _n = k
        return nm in STATE_FIELDS or any(('/%s[' % s) in path or path.endswith('/' + s) for s in ('Render States', 'Texture States'))
    # states may change the length of their arrays, which moves the 'Block Index Data' words of the implementations that follow: only the state fields themselves
    # and the index words of state blocks are allowed to differ
    hard = [k for k in diff if not is_state(k)]
    return ('state-only' if not hard else 'incompatible'), sorted(hard or diff, key=str)[:12]


# ================================================================================================
# sources: kit .psh byte-exactness, D3D9 compile, DXBC translation
# ================================================================================================
def load_pass_sources(env, name, mroot):
    """-> ({(impl, ps): chunk text}, {impl: reason}): the implementations of pass `name` whose kit .psh recompiles (DX11, MCC recipe) to EXACTLY the DXBC of every
    element of the map; the chunk is the DX11 text of that element."""
    out, why = {}, {}
    tagp = os.path.join(env.kit, 'tags', name + '.shader_pass')
    tag = SC.read_tag_file(tagp, 'spas') if os.path.exists(tagp) else None
    els = D.spas_pixel_shaders(mroot)
    nimpl = len(blk(blk(mroot, 'Postprocess Definition')[0], 'Implementations'))
    for ii in range(nimpl):
        p = os.path.join(env.psh_dir, SC.psh_file_name(name, ii))
        if not os.path.exists(p):
            why[ii] = 'no .psh'
            continue
        if tag is None:
            why[ii] = 'no .shader_pass tag'
            continue
        timpl = tag['Implementations'][ii]
        at = SC.alpha_test_from_tag_impl(timpl)
        centroid = tuple(i for i, t in enumerate(timpl['Textures']) if SC.num(t, 'Source Extern') == SC.SOURCE_EXTERN_LIGHTMAP)
        data = open(p, 'rb').read()
        res = SC.compile_psh(data, at, centroid, None, source_name=os.path.basename(p))
        mine = [e for e in els if e['impl'] == ii]
        if len(mine) != len(res.blobs):
            why[ii] = 'element count %d != %d cases' % (len(mine), len(res.blobs))
            continue
        bad = [(e['ps'], v) for e, bl in zip(mine, res.blobs) for v in range(3) if bytes(bl[v]) != e['blobs'][v]]
        if bad:
            why[ii] = 'DXBC differs from the .psh compile in %d blobs (e.g. element %d variant %d)' % (len(bad), bad[0][0], bad[0][1])
            continue
        for e, case in zip(mine, res.cases):
            out[(ii, e['ps'])] = case.text.decode('latin-1') if isinstance(case.text, bytes) else case.text
    return out, why


def patch_chunk(text, fixes):
    """pin interpolant lanes at the top of main(): fixes = {(semantic name, index): {lane: value}}  ->  text with ``tcN.z = value;`` lines after the opening brace"""
    if not fixes:
        return text
    lines = []
    for (nm, idx), lanes in sorted(fixes.items()):
        var = ('tc%d' if nm == 'TEXCOORD' else 'color%d') % idx
        for lane, val in sorted(lanes.items()):
            lines.append('    %s.%s = %s;   // MCC vertex shader wrapper default (ce_port_build lane reconciliation)' % (var, LANES[lane], repr(float(val))))
    t = text.replace('\r\n', '\n')
    m = re.search(r'\bmain\s*\(', t)
    if m is None:
        raise ValueError('no main() in the pixel shader text')
    b = t.find('\n{', m.end())
    if b < 0:
        raise ValueError('no opening brace after main()')
    b += 2
    return t[:b] + '\n'.join(lines) + '\n' + t[b:]


def compile_d3d9(chunk_text, variant, fixes=None):
    """DX11 chunk text -> (blob, profile, note).  ps_2_0 for variants 0 / 1 and ps_3_0 for variant 2 (Vista's three profiles); a variant that does not fit ps_2_0
    (instruction slots) is compiled as ps_3_0 - the engine creates the shader from whatever model the blob declares."""
    from .dx11to9 import DX11_PRELUDE_D3D9, to_d3d9
    src = DX11_PRELUDE_D3D9 + patch_chunk(to_d3d9(chunk_text), fixes)
    defs = {'DX11': 1}
    if variant == 1:
        defs['ZPRIME'] = 1
    elif variant == 2:
        defs['MRT'] = 1
    note = ''
    prof = D3D9_PROFILES[variant]
    try:
        return D.compile_hlsl(src, prof, 'main', defs, D3D9_FLAGS), prof, note
    except D.CompileError as ex:
        if prof != 'ps_2_0':
            raise
        note = '%s: %s' % (prof, str(ex).strip().split('\n')[-1][:140])
        return D.compile_hlsl(src, 'ps_3_0', 'main', defs, D3D9_FLAGS), 'ps_3_0', note


def dxbc_route(dxbc, variant):
    src = dxbc9.dxbc_to_hlsl(dxbc, variant_mrt=(variant == 2))
    note = ''
    prof = D3D9_PROFILES[variant]
    try:
        return D.compile_hlsl(src, prof, 'main', {}, D3D9_FLAGS), prof, note
    except D.CompileError as ex:
        if prof != 'ps_2_0':
            raise
        note = '%s: %s' % (prof, str(ex).strip().split('\n')[-1][:140])
        return D.compile_hlsl(src, 'ps_3_0', 'main', {}, D3D9_FLAGS), 'ps_3_0', note


def prove(blob, dxbc, alpha, pins=None, n=32):
    """(ok, max_err, notes): D3D9 blob against the DXBC of the map on two normal trial sets and one stress set (constants -3..3 with 0 / +-1 / 0.5 injected, colours
    0..1 (the Direct3D 9 colour interpolant range), texcoord w 0.2..3); every output lane both sides write is compared (rtol 2e-6 / atol 2e-7, stress 1e-5 / 1e-6), written-lane sets and kill flags must
    agree.  `alpha`: the pass implementation's alpha-test define dict (a render state in D3D9, compiled into the DXBC: emulated on the D3D9 side).  `pins`: the
    interpolant lanes pinned by a lane fix ({(semantic, index): {lane: value}}): both machines see exactly that value there (the domain the MCC vertex shader supplies)."""
    ma, mb = D.D3D9Machine(blob), D.DxbcMachine(dxbc)
    sems = set(ma.input_semantics()) | set(mb.input_semantics())
    samp = D.SampleFn()
    ok, worst, notes = True, 0.0, []
    for k, seed in enumerate((7, 8, 9)):
        stress = (k == 2)
        consts, inputs = D.random_trials(n, seed, sems, 64, stress)
        z = consts[:, 16, 2]                    # zwriteout.z (fog depth divisor): the engine never supplies 0, the stress trials inject exact zeros
        consts[:, 16, 2] = np.where(np.abs(z) < 0.25, 1.0, z)
        for sem in inputs:                      # Direct3D 9 colour interpolants are in [0, 1] (vs_2_0 oD0 / oD1 are saturated by the hardware): both machines see that domain
            if sem[0] == 'COLOR':
                inputs[sem] = np.clip(inputs[sem], 0.0, 1.0)
        for sem, lanes in (pins or {}).items():
            for lane, val in lanes.items():
                if sem in inputs:
                    inputs[sem][:, lane] = val
        ra = D.apply_alpha_test(ma.run(consts, inputs, samp), alpha or None)
        rb = mb.run(consts, inputs, samp)
        rep = D.compare_results(ra, rb, 1e-5 if stress else 2e-6, 1e-6 if stress else 2e-7)
        worst = max(worst, rep.max_err)
        if not rep.ok:
            ok = False
            notes += rep.notes[:2]
    return ok, worst, notes


def ps_read_masks(dxbc):
    """{(semantic, index): lane mask} of the pixel shader interpolants that are actually READ (ISGN read/write mask byte)"""
    dx = D.parse_dxbc(dxbc)
    buf = dx.chunks.get('ISGN') or dx.chunks.get('ISG1')
    out = {}
    cnt = struct.unpack_from('<I', buf, 0)[0]
    esz = 24 if 'ISGN' in dx.chunks else 32
    for i in range(cnt):
        o = 8 + esz * i
        nameoff, sidx, sysv, ctype, reg, mask, rw = struct.unpack_from('<IIIIIBB', buf, o)
        nm = buf[nameoff:buf.find(b'\0', nameoff)].decode('latin-1').upper()
        if nm in ('COLOR', 'TEXCOORD') and rw:
            out[(nm, sidx)] = rw
    return out


# ================================================================================================
# vertex shaders: source, proof
# ================================================================================================
def kit_vrtx_texts(kit, name):
    """{MD5 of a vs_5_0 DXBC blob: its HLSL text} from the H2EK ``tags\\<name>.vertex_shader`` file, which stores the text the MCC tool compiled right behind every blob
    (the DX11 and the Xbox blocks; the DX9 block holds the original Halo 2 program with a vs_2_0 blob)."""
    p = os.path.join(kit, 'tags', name + '.vertex_shader')
    out = {}
    if not os.path.isfile(p):
        return out
    d = open(p, 'rb').read()
    for m in re.finditer(b'DXBC', d):
        o = m.start()
        if o + 32 > len(d):
            continue
        sz = struct.unpack_from('<I', d, o + 24)[0]
        if sz < 64 or o + sz > len(d):
            continue
        e = o + sz
        while e < len(d) and (d[e] >= 32 or d[e] in (9, 10, 13)):
            e += 1
        t = d[o + sz:e]
        k = t.rfind(b'}')
        if k < 0 or b'VS_OUTPUT' not in t:
            continue
        out.setdefault(hashlib.md5(d[o:o + sz]).hexdigest(), t[:k + 1].decode('latin-1'))
    return out


def explicit_lane_mask(text):
    """The 'Output Swizzles' word of a vertex shader text as the MCC tool writes it: nibble i = the lanes the PROGRAM assigns explicitly (``oT1.w = ...`` or the whole
    register ``oT1 = ...``) of oD0, oD1, oT0, oT1, oT2, oT3 (i = 0..5).  Lanes that only keep the wrapper's initial value do not count.  Reproduces the DX11 slot of
    beavercreek's water_pool_static (0x0033F3FF; Vista's original program 0x003373FF)."""
    regs = {'oD0': 0, 'oD1': 1, 'oT0': 2, 'oT1': 3, 'oT2': 4, 'oT3': 5}
    mask = [0] * 6
    for m in re.finditer(r'^[ \t]*(o[DT][0-3])(?:\.([xyzw]+))?[ \t]*=[^=]', text.replace('\r\n', '\n'), re.M):
        i = regs[m.group(1)]
        for ch in (m.group(2) or 'xyzw'):
            mask[i] |= 1 << 'xyzw'.index(ch)
    return sum(v << (4 * i) for i, v in enumerate(mask))


def prove_vs(blob, dxbc, n=48):
    """(ok, max_err, notes): a vs_2_0 byte code against the vs_5_0 DXBC of the map, lane for lane.  Four trial sets (two with the constants the engine pins, two stress sets:
    constants +-3 with 0 / +-1 / 0.5 injected); EVERY lane of POSITION, COLOR0/1 and TEXCOORD0..7 must be written by BOTH shaders (no reliance on the MCC wrapper's defaults
    for the Direct3D 9 side - an unwritten lane of a vs_2_0 output is undefined) and agree (rtol 1e-6 / atol 1e-7, stress 1e-5 / 1e-6).  The position is compared after MCC's
    z remap (z = w * c10.w + c10.z) is applied to the Direct3D 9 clip position: the Vista shader writes the plain clip position, MCC's wrapper remaps the depth."""
    notes = []
    sh = D.parse(blob)
    if (sh.kind, sh.major, sh.minor) != ('vs', 2, 0):
        return False, 0.0, ['profile is %s_%d_%d, not vs_2_0' % (sh.kind, sh.major, sh.minor)]
    ninst = len([x for x in sh.ins if x.name not in ('dcl', 'def', 'defi', 'defb', 'comment')])
    if ninst > 256:
        return False, 0.0, ['%d instructions exceed the vs_2_0 limit' % ninst]
    ma, mb = D.D3D9Machine(blob), D.DxbcMachine(dxbc)
    sems = set(ma.input_semantics()) | set(mb.input_semantics())
    samp = D.SampleFn()
    ok, worst = True, 0.0
    for k, seed in enumerate((31, 32, 33, 34)):
        stress = k >= 2
        consts, inputs = D.random_trials(n, seed, sems, 256, stress)
        if k % 2:
            for (reg, lane), val in SP.ENGINE_PINNED_VS_CONSTANTS:
                consts[:, reg, lane] = val
        ra, rb = ma.run(consts, inputs, samp), mb.run(consts, inputs, samp)
        if k == 0:
            for sem in VS_OUTPUTS:
                wa, wb = ra.written.get(sem, 0), rb.written.get(sem, 0)
                if wa != 0xF or wb != 0xF:
                    ok = False
                    notes.append('output %s: lanes written %X (vs_2_0) / %X (DXBC), all four expected' % (sem, wa, wb))
            extra = (set(ra.outputs) | set(rb.outputs)) - set(VS_OUTPUTS)
            if extra:
                ok = False
                notes.append('unexpected outputs %s' % sorted(extra))
        for sem in VS_OUTPUTS:
            if sem not in ra.outputs or sem not in rb.outputs:
                continue
            x = ra.outputs[sem].copy()
            y = rb.outputs[sem]
            if sem[0] == 'POSITION':
                x[:, 2] = x[:, 3] * consts[:, 10, 3] + consts[:, 10, 2]
            rtol, atol = (1e-5, 1e-6) if stress else (1e-6, 1e-7)
            with np.errstate(all='ignore'):
                err = np.abs(x - y)
                bad = ~((err <= atol + rtol * np.maximum(np.abs(x), np.abs(y))) | (x == y) | (~np.isfinite(x) & ~np.isfinite(y)))
            fin = err[np.isfinite(err)]
            if len(fin):
                worst = max(worst, float(fin.max()))
            if bad.any():
                ok = False
                notes.append('%s differs in %d lane values (set %d, max %.4g)' % (sem, int(bad.sum()), k, float(np.nanmax(err[bad])) if np.isfinite(err[bad]).any() else float('nan')))
    return ok, worst, notes[:4]


def chain_check(vs_blob, vs_dxbc, ps_blob, ps_dxbc, alpha, n=32, seed=5, stress=False):
    """(ok, note): the CHAIN (ported vs_2_0 -> ported D3D9 pixel shader) against MCC's chain (vs_5_0 -> ps_5_0): same random vertex constants / attributes (the engine-pinned
    ones fixed), same pixel constants; every interpolant the pixel shader reads comes from what each vertex shader WROTE (colours clamped to [0,1], the Direct3D 9 colour
    interpolant range, on both sides); the pixel outputs, kill flags and the alpha test (a render state in Direct3D 9) are compared like ``prove`` does."""
    va, vb = D.D3D9Machine(vs_blob), D.DxbcMachine(vs_dxbc)
    pa, pb = D.D3D9Machine(ps_blob), D.DxbcMachine(ps_dxbc)
    samp = D.SampleFn()
    sems = set(va.input_semantics()) | set(vb.input_semantics())
    cvs, vin = D.random_trials(n, seed, sems, 256, stress)
    for (reg, lane), val in SP.ENGINE_PINNED_VS_CONSTANTS:
        cvs[:, reg, lane] = val
    ra, rb = va.run(cvs, vin, samp), vb.run(cvs, vin, samp)
    cps, _unused = D.random_trials(n, seed + 1, set(), 64, stress)
    z = cps[:, 16, 2]
    cps[:, 16, 2] = np.where(np.abs(z) < 0.25, 1.0, z)
    psems = set(pa.input_semantics()) | set(pb.input_semantics())
    extra = D.random_trials(n, seed + 2, psems, 1)[1]
    ia, ib = {}, {}
    for sem in psems:
        if sem[0] in ('COLOR', 'TEXCOORD'):
            if ra.written.get(sem, 0) != 0xF:
                return False, 'the vs_2_0 does not write every lane of %s' % (sem,)
            a, b = ra.outputs[sem], rb.outputs[sem]
            if sem[0] == 'COLOR':
                a, b = np.clip(a, 0.0, 1.0), np.clip(b, 0.0, 1.0)
            ia[sem], ib[sem] = a, b
        else:
            ia[sem] = ib[sem] = extra[sem]
    res_a = D.apply_alpha_test(pa.run(cps, {s: ia[s] for s in pa.input_semantics() if s in ia}, samp), alpha or None)
    res_b = pb.run(cps, {s: ib[s] for s in pb.input_semantics() if s in ib}, samp)
    rep = D.compare_results(res_a, res_b, 1e-5 if stress else 2e-6, 1e-6 if stress else 2e-7)
    return rep.ok, '; '.join(rep.notes[:2])


# ================================================================================================
# the build
# ================================================================================================
class Builder(object):
    def __init__(self, env, log=_log):
        self.env = env
        self.log = log
        self.d3d9 = collections.OrderedDict()           # md5(D3D9 blob) -> {'d3d9': bytes, 'profile', 'source', ...}
        self.cache = {}                                 # (md5 dxbc, variant, fixkey) -> result dict   (a compiled + proven blob, reused by aliases)
        self.chunk_index = {}                           # md5 of a verified element's first non-empty DXBC -> chunk text (any pass of the mod)
        self.passes = collections.OrderedDict()         # lower name -> registry entry
        self.stems = collections.OrderedDict()
        self.vrtx = collections.OrderedDict()           # lower name -> registry entry of a private vertex shader (port_vrtx)
        self.vrtx_why = {}                              # lower name -> why a vertex shader could not be ported
        self._vrtx_dxbc = {}                            # lower name -> [MCC DXBC per class]
        self.unportable = []
        self.report = {'passes': {}, 'stems': {}, 'notes': []}

    # ---- chunk index over EVERY spas of the mod whose .psh reproduces its DXBC (aliases: opaque_unlit == add_detail_masked ...) --------------------------------
    def index_chunks(self):
        env = self.env
        seen = set()
        n = 0
        for where, c in [('shared', env.shared)] + list(env.maps.items()):
            for t in c.tags:
                if t.group != 'spas' or not t.has_data or not t.name or t.name in seen:
                    continue
                seen.add(t.name)
                root = env.dec_mcc(c, t)
                src, why = load_pass_sources(env, t.name, root)
                els = {(e['impl'], e['ps']): e for e in D.spas_pixel_shaders(root)}
                for k, txt in src.items():
                    for b in els[k]['blobs']:
                        if b:
                            self.chunk_index.setdefault(hashlib.md5(b).hexdigest(), txt)
                            break
                    n += 1
        self.log('chunk index: %d elements with a reproducing .psh over %d passes' % (n, len(seen)))

    # ---- one DXBC blob -> D3D9 ------------------------------------------------------------------------------------------------------------------------------------
    def port_element(self, e, chunk, fixes=None):
        """-> list of 3 result dicts (None for an empty variant): {'md5': md5 of the D3D9 blob, 'profile', 'source', 'max_err', 'note', 'ok'}"""
        fixkey = repr(sorted((k, sorted(v.items())) for k, v in (fixes or {}).items()))
        res = []
        for v in range(3):
            dxbc = e['blobs'][v]
            if not dxbc:
                res.append(None)
                continue
            ckey = (hashlib.md5(dxbc).hexdigest(), v, fixkey, bool(e['alpha']))
            hit = self.cache.get(ckey)
            if hit is not None:
                res.append(hit)
                continue
            blob = prof = None
            note, source = '', None
            if chunk is not None:
                try:
                    blob, prof, note = compile_d3d9(chunk, v, fixes)
                    source = 'psh'
                except Exception as ex:                              # noqa: BLE001
                    note = 'psh route failed: %s' % str(ex)[:160]
            if blob is None and not fixes:
                try:
                    blob, prof, note2 = dxbc_route(dxbc, v)
                    source = 'dxbc9'
                    note = (note + ' ' + note2).strip()
                except Exception as ex:                              # noqa: BLE001
                    r = {'error': '%s; dxbc9: %s' % (note, str(ex)[:160]), 'ok': False}
                    self.cache[ckey] = r
                    res.append(r)
                    continue
            if blob is None:
                r = {'error': note or 'no source', 'ok': False}
                self.cache[ckey] = r
                res.append(r)
                continue
            ok, err, notes = prove(blob, dxbc, e['alpha'], fixes)
            h = hashlib.md5(blob).hexdigest()
            r = {'md5': h, 'profile': prof, 'source': source, 'max_err': err, 'note': note, 'ok': ok, 'proof_notes': notes}
            if ok:
                self.d3d9.setdefault(h, {'d3d9': blob, 'profile': prof, 'source': source})
            self.cache[ckey] = r
            res.append(r)
        return res

    # ---- vertex shaders ------------------------------------------------------------------------------------------------------------------------------------------
    def port_vrtx(self, name):
        """Port the MCC vertex shader `name` to vs_2_0 from the HLSL text of the H2EK vertex_shader tag.  -> the registry entry (also kept in self.vrtx) or None (the reason is in
        self.vrtx_why).  Every non-empty class must (1) have a text that reproduces its DXBC byte for byte, (2) compile for vs_2_0 and (3) be proven lane for lane."""
        from .dx11to9 import vs_to_d3d9
        env = self.env
        low = name.lower()
        if low in self.vrtx:
            return self.vrtx[low]
        if low in self.vrtx_why:
            return None

        def fail(why):
            self.vrtx_why[low] = why
            self.log('    vrtx %s: NOT ported: %s' % (name, why))
            return None
        t, c, where = env.find_mcc('vrtx', name)
        if t is None:
            return fail('not in the mod')
        mroot = env.dec_mcc(c, t)
        moff = mroot.field('Geometry Classifications DX11').offset
        dxbcs = [bytes(el.datas.get(el.field('Compiled Shader').offset, b'')) for el in mroot.blocks.get(moff, [])]
        texts = kit_vrtx_texts(env.kit, name)
        classes, info, masks = [], [], set()
        for ci, dxbc in enumerate(dxbcs):
            if not dxbc:
                classes.append('')
                continue
            text = texts.get(hashlib.md5(dxbc).hexdigest())
            if text is None:
                return fail('class %d: the kit has no text for this DXBC' % ci)
            try:
                rebuilt = D.compile_hlsl(text, 'vs_5_0', 'main', {}, VS_DX11_FLAGS, name='shader.vsh')
            except D.CompileError as ex:
                return fail('class %d: the kit text does not compile for vs_5_0: %s' % (ci, str(ex).strip().split('\n')[-1][:120]))
            if rebuilt != dxbc:
                return fail('class %d: the kit text does not reproduce the DXBC of the map (%d vs %d bytes)' % (ci, len(rebuilt), len(dxbc)))
            try:
                blob = D.compile_hlsl(vs_to_d3d9(text), VS_PROFILE, 'main', {}, VS_D3D9_FLAGS, name='shader.vsh')
            except (D.CompileError, ValueError) as ex:
                return fail('class %d: vs_2_0 compile failed: %s' % (ci, str(ex).strip().split('\n')[-1][:120]))
            ok, err, notes = prove_vs(blob, dxbc)
            if not ok:
                return fail('class %d: not proven: %s' % (ci, notes[:2]))
            h = hashlib.md5(blob).hexdigest()
            self.d3d9.setdefault(h, {'d3d9': blob, 'profile': VS_PROFILE, 'source': 'vsh'})
            classes.append(h)
            masks.add(explicit_lane_mask(text))
            info.append({'class': ci, 'dxbc_bytes': len(dxbc), 'vs_2_0_bytes': len(blob), 'max_err': err})
        if not any(classes):
            return fail('no compiled class')
        swz_mcc = mroot.u32(mroot.field('Output Swizzles DX11').offset)
        if len(masks) != 1:
            return fail('the classes assign different output lanes: %s' % sorted(hex(m) for m in masks))
        swz = masks.pop()
        nm, sd = mcc_funcs(c)
        ent = {'digest': CP.digest(mroot, nm, sd), 'digests': env.digests('vrtx', name), 'private': name + CP.PRIVATE_SUFFIX, 'classes': classes, 'output_swizzles': swz,
               'profile': VS_PROFILE, 'source': 'H2EK vertex_shader tag text (DX11 program), reproduces the map DXBC byte for byte; vs_to_d3d9'}
        if swz != swz_mcc:
            ent['note'] = 'Output Swizzles from the program text 0x%08X differs from the MCC DX11 slot 0x%08X' % (swz, swz_mcc)
        self.vrtx[low] = ent
        self._vrtx_dxbc[low] = dxbcs
        self.report.setdefault('vrtx', {})[name] = {'where': where, 'classes': info, 'output_swizzles': '0x%08X' % swz, 'mcc_dx11_slot': '0x%08X' % swz_mcc}
        self.log('    vrtx %s: ported to %s (%d classes, Output Swizzles 0x%08X)' % (name, VS_PROFILE, len([x for x in classes if x]), swz))
        return ent

    def _pair_tests(self, pairs, mine, results, ii, chain=False):
        """composite (pointwise, vs_ps_composite) over every class x ported pixel shader of the implementation; chain=True adds chain_check (normal + stress)
        against MCC's pixel shader -> (tests, failures, first notes)"""
        ntest = nbad = 0
        first = []
        for ci, a, b in pairs:
            for e in mine:
                for v, r in enumerate(results[(ii, e['ps'])]):
                    if r is None or 'md5' not in r:
                        continue
                    ps = self.d3d9[r['md5']]['d3d9']
                    runs = [('composite', lambda: SP.vs_ps_composite(a, b, ps, n=24, seed=5, pin_engine=True))]
                    if chain:
                        runs += [('chain', lambda: chain_check(a, b, ps, e['blobs'][v], e['alpha'])),
                                 ('chain stress', lambda: chain_check(a, b, ps, e['blobs'][v], e['alpha'], seed=9, stress=True))]
                    for label, fn in runs:
                        ntest += 1
                        okc, note = fn()
                        if not okc:
                            nbad += 1
                            if len(first) < 2:
                                first.append('class %d %s: %s' % (ci, label, note))
        return ntest, nbad, first

    def vs_pairs(self, im):
        """-> (vrtx name, [(class index, vista blob, mcc dxbc)], error) of one pass implementation"""
        env = self.env
        vo = im.field('Vertex Shader').offset
        vdatum = im.u32(vo + 4)
        vt = vc = None
        for c in [env.shared] + list(env.maps.values()):
            vt = c.tag_by_datum(vdatum)
            if vt is not None:
                vc = c
                break
        if vt is None or vt.group != 'vrtx':
            return None, [], 'vertex shader reference not found'
        vt, vc = env.resolve(vc, vdatum)
        vname = vt.name
        _vv, vroot = env.vista_tag('vrtx', vname)
        if vroot is None:
            return vname, [], 'no Vista vertex shader of that name'
        mv = env.dec_mcc(vc, vt)
        moff = mv.field('Geometry Classifications DX11').offset
        mcls = [bytes(el.datas.get(el.field('Compiled Shader').offset, b'')) for el in mv.blocks.get(moff, [])]
        vcls = D.vrtx_elements(vroot)
        pairs, err = [], None
        for ci in range(max(len(mcls), len(vcls))):
            a = vcls[ci] if ci < len(vcls) else b''
            b = mcls[ci] if ci < len(mcls) else b''
            if a and b:
                pairs.append((ci, a, b))
            elif bool(a) != bool(b):
                err = 'class %d: only the %s vertex shader has code' % (ci, 'Vista' if a else 'MCC')
        return vname, pairs, err

    @staticmethod
    def lane_diffs(a_blob, b_dxbc, read, n=24, seed=5):
        """interpolant lanes the pixel shader reads that differ between the Vista vertex shader (wrapper model) and MCC's: {(sem, lane): (vista value | None, mcc value | None)};
        None = not a constant"""
        a, b = D.D3D9Machine(a_blob), D.DxbcMachine(b_dxbc)
        sems = set(a.input_semantics()) | set(b.input_semantics())
        cvs, vin = D.random_trials(n, seed, sems, ncon=256)
        for (reg, lane), val in SP.ENGINE_PINNED_VS_CONSTANTS:
            cvs[:, reg, lane] = val
        samp = D.SampleFn()
        ra, rb = a.run(cvs, vin, samp), b.run(cvs, vin, samp)
        exp = D.vs_expected(ra, cvs, False)
        out = {}
        for sem, mask in read.items():
            x, y = exp.get(sem), rb.outputs.get(sem)
            if x is None or y is None:
                continue
            for lane in range(4):
                if not mask >> lane & 1:
                    continue
                with np.errstate(all='ignore'):
                    bad = np.abs(x[:, lane] - y[:, lane]) > 1e-5 + 1e-4 * np.abs(y[:, lane])
                if bad.any():
                    cx = float(x[0, lane]) if np.ptp(x[:, lane]) == 0 else None
                    cy = float(y[0, lane]) if np.ptp(y[:, lane]) == 0 else None
                    out[(sem, lane)] = (cx, cy)
        return out

    def vs_check(self, mroot, results, fixes_by_impl):
        """For every implementation: do the Vista vertex shader (all classes) and the ported pixel shaders (variants of every element) reproduce MCC's pair?
        -> (ok, rows, fixable {impl: fixes})"""
        env = self.env
        pp = blk(mroot, 'Postprocess Definition')[0]
        els = D.spas_pixel_shaders(mroot)
        rows, ok, fixable = [], True, {}
        for ii, im in enumerate(blk(pp, 'Implementations')):
            vname, pairs, err = self.vs_pairs(im)
            row = {'impl': ii, 'vrtx': vname, 'classes': len(pairs)}
            if err and not pairs:
                row['error'] = err
                rows.append(row)
                ok = False
                continue
            if err:
                row['note'] = err
            mine = [e for e in els if e['impl'] == ii]
            ntest, nbad, first = self._pair_tests(pairs, mine, results, ii)
            row.update({'tests': ntest, 'failures': nbad, 'first': first})
            if nbad and vname and not self.derive_fixes(pairs, mine):
                # the Vista vertex shader of that name does not drive the ported pixel shaders like MCC's AND constant lanes (lane_fixes) do not explain it:
                # port MCC's own vertex shader (private vrtx).  Only a failure the lane pins cannot repair gets here, so every pass that was proven with the
                # Vista vertex shader (with or without lane fixes) is unchanged.
                pv = self.port_vrtx(vname)
                if pv is not None:
                    dxbcs = self._vrtx_dxbc[vname.lower()]
                    ppairs = [(ci, self.d3d9[h]['d3d9'], dxbcs[ci]) for ci, h in enumerate(pv['classes']) if h]
                    ptests, pbad, pfirst = self._pair_tests(ppairs, mine, results, ii, chain=True)
                    row['vista_vertex_shader'] = {'tests': ntest, 'failures': nbad, 'first': first}
                    row.update({'tests': ptests, 'failures': pbad, 'first': pfirst, 'classes': len(ppairs)})
                    if not pbad:
                        row['private_vrtx'] = vname
                        row.pop('error', None)
                        nbad = 0
                    else:
                        nbad = pbad
                else:
                    row['private_vrtx_why'] = self.vrtx_why.get(vname.lower())
            if fixes_by_impl.get(ii):
                row['lane_fixes'] = {'%s%d.%s' % (k[0], k[1], LANES[ln]): v for k, lanes in fixes_by_impl[ii].items() for ln, v in lanes.items()}
            rows.append(row)
            if nbad:
                ok = False
                # can the failure be explained by constant lanes alone?
                fx = self.derive_fixes(pairs, mine)
                if fx:
                    fixable[ii] = fx
        return ok, rows, fixable

    def derive_fixes(self, pairs, mine):
        read = collections.defaultdict(int)
        for e in mine:
            for b in e['blobs']:
                if b:
                    for k, m in ps_read_masks(b).items():
                        read[k] |= m
        merged, bad = {}, False
        for ci, a, b in pairs:
            for (sem, lane), (cx, cy) in self.lane_diffs(a, b, dict(read)).items():
                if cy is None or cx is None:
                    bad = True
                    continue
                prev = merged.get((sem, lane))
                if prev is not None and prev != (cx, cy):
                    bad = True
                merged[(sem, lane)] = (cx, cy)
        if bad or not merged:
            return None
        # every class must differ in exactly these lanes (a lane that matches in another class would be broken by the pin)
        for ci, a, b in pairs:
            d = self.lane_diffs(a, b, dict(read))
            if set(d) != set(merged):
                return None
        fx = collections.defaultdict(dict)
        for (sem, lane), (cx, cy) in merged.items():
            fx[sem][lane] = cy
        return dict(fx)

    # ---- one pass -----------------------------------------------------------------------------------------------------------------------------------------------------
    def build_pass(self, name, private):
        env = self.env
        t, c, where = env.find_mcc('spas', name)
        if t is None:
            return False, 'pass not found in the mod'
        mroot = env.dec_mcc(c, t)
        srcs, why = load_pass_sources(env, name, mroot)
        els = D.spas_pixel_shaders(mroot)
        fixes_by_impl = {}
        info = {'where': where, 'elements': len(els), 'rejected_psh': {str(k): v for k, v in why.items()}}
        for attempt in (0, 1):
            results, bad = {}, []
            nsrc = collections.Counter()
            for e in els:
                chunk = srcs.get((e['impl'], e['ps']))
                if chunk is None:
                    for b in e['blobs']:               # an alias of a source-backed element of another pass (same DXBC)
                        if b:
                            chunk = self.chunk_index.get(hashlib.md5(b).hexdigest())
                            break
                res = self.port_element(e, chunk, fixes_by_impl.get(e['impl']))
                results[(e['impl'], e['ps'])] = res
                for v, r in enumerate(res):
                    if r is None:
                        continue
                    if not r['ok']:
                        bad.append((e['impl'], e['ps'], v, r.get('error') or r.get('proof_notes')))
                    else:
                        nsrc[r['source']] += 1
            info['sources'] = dict(nsrc)
            info['bad_count'] = len(bad)
            info['bad'] = [list(map(str, b)) for b in bad[:6]]
            if bad:
                self.report['passes'][name] = info
                return False, 'unproven blobs: %d (e.g. %s)' % (len(bad), str(bad[0])[:200])
            ok, vrows, fixable = self.vs_check(mroot, results, fixes_by_impl)
            info['vertex_shaders'] = vrows
            if ok:
                break
            new = {ii: fx for ii, fx in fixable.items() if ii not in fixes_by_impl}
            if attempt == 1 or not new:
                self.report['passes'][name] = info
                return False, 'vertex shader check failed: %s' % [r for r in vrows if r.get('failures') or r.get('error')][:2]
            fixes_by_impl.update(new)
            self.log('    %s: lane fixes for implementations %s: %s' % (name, sorted(new), {i: {k: v for k, v in fx.items()} for i, fx in new.items()}))
        self.report['passes'][name] = info
        nm, sd = mcc_funcs(c)
        flat = []
        for e in els:
            for r in results[(e['impl'], e['ps'])]:
                flat.append(r['md5'] if r else '')
        ent = {'digest': CP.digest(mroot, nm, sd), 'digests': env.digests('spas', name), 'private': (name + CP.PRIVATE_SUFFIX) if private else None, 'elements': len(els), 'd3d9': flat,
               'vertex_shaders': sorted(set(r['vrtx'] for r in vrows if r.get('vrtx')))}
        pvs = sorted(set(r['private_vrtx'] for r in vrows if r.get('private_vrtx')))
        if pvs:
            ent['private_vrtx'] = pvs                  # the planner ports the pass only together with these (digest-matched) private vertex shaders
        if fixes_by_impl:
            ent['lane_fixes'] = {str(ii): {'%s%d.%s' % (k[0], k[1], LANES[ln]): v for k, lanes in fx.items() for ln, v in lanes.items()} for ii, fx in fixes_by_impl.items()}
        self.passes[name.lower()] = ent
        return True, 'ok'

    # ---- the whole table ------------------------------------------------------------------------------------------------------------------------------------------
    def run(self, only=()):
        env = self.env
        self.index_chunks()
        pass_state = {}                    # lower name -> ('vista'|'port'|'private'|'unportable', why)

        def classify(pname):
            low = pname.lower()
            if low in pass_state:
                return pass_state[low]
            t, c, where = env.find_mcc('spas', pname)
            vtag, vroot = env.vista_tag('spas', pname)
            if t is None:
                st = ('unportable', 'not in the mod')
            elif vtag is None:
                ok, why = self.build_pass(pname, False)
                st = ('port', why) if ok else ('unportable', why)
            else:
                mroot = env.dec_mcc(c, t)
                nm, sd = mcc_funcs(c)
                kind, diff = pass_compat(mroot, nm, vroot, vista_funcs(vtag[0]))
                if kind in ('equal', 'state-only'):
                    st = ('vista', kind)
                else:
                    ok, why = self.build_pass(pname, True)
                    st = ('private', why) if ok else ('unportable', '%s; structure differs from Vista (%s)' % (why, diff[:2]))
            pass_state[low] = st
            self.log('  pass %-80s %s' % (pname, st[0] + ('' if st[0] in ('vista', 'port', 'private') else ': ' + str(st[1])[:150])))
            return st

        for sname in TARGET_STEMS:
            if only and not any(o in sname for o in only):
                continue
            t, c, where = env.find_mcc('stem', sname)
            if t is None:
                self.unportable.append(('stem', sname, 'not found'))
                continue
            root = env.dec_mcc(c, t)
            nm, sd = mcc_funcs(c)
            pp = blk(root, 'Postprocess Definition')[0]
            pnames = []
            for k in blk(pp, 'Passes'):
                po = k.field('Pass').offset
                pt, _pc = env.resolve(c, k.u32(po + 4))
                pnames.append(pt.name if pt is not None else None)
            uniq = list(dict.fromkeys(pnames))
            states = [classify(p) if p else ('unportable', 'null pass') for p in uniq]
            bad = [(p, s) for p, s in zip(uniq, states) if s[0] == 'unportable']
            if bad:
                self.unportable.append(('stem', sname, 'passes unavailable: %s' % bad[:2]))
                self.log('STEM %-60s UNPORTABLE' % sname)
                continue
            self.stems[sname.lower()] = {'digest': CP.digest(root, nm, sd), 'digests': env.digests('stem', sname), 'passes': [p.lower() for p in uniq]}
            self.log('STEM %-60s ok (%d passes)' % (sname, len(uniq)))
        for g, n, why in self.unportable:
            self.log('UNPORTABLE %s %s: %s' % (g, n, str(why)[:260]))
        self.report['unportable'] = [list(map(str, x)) for x in self.unportable]
        self.report['pass_state'] = {k: list(map(str, v)) for k, v in pass_state.items()}

    # ---- output -----------------------------------------------------------------------------------------------------------------------------------------------------------
    def write(self, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        used = set()
        for ent in self.passes.values():
            used.update(h for h in ent['d3d9'] if h)
        for ent in self.vrtx.values():
            used.update(h for h in ent['classes'] if h)
        blobs, off = collections.OrderedDict(), 0
        with open(os.path.join(out_dir, CP.BLOBS_NAME), 'wb') as fh:
            for key, ent in self.d3d9.items():
                if key not in used:
                    continue
                fh.write(ent['d3d9'])
                blobs[key] = [off, len(ent['d3d9']), ent['profile'], ent['source']]
                off += len(ent['d3d9'])
        man = {'format': CP.FORMAT, 'built_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               'recipe': {'profiles': list(D3D9_PROFILES), 'flags': D3D9_FLAGS, 'compiler': SC.compiler_info()},
               'stems': self.stems, 'passes': self.passes, 'blobs': blobs}
        if self.vrtx:
            man['vrtx'] = self.vrtx
        with open(os.path.join(out_dir, CP.MANIFEST_NAME), 'w', encoding='utf-8') as fh:
            json.dump(man, fh, indent=1)
        return man


def build(mod, kit, vista_maps, out_dir, report=None, only=(), log=_log):
    env = Env(mod, kit, vista_maps)
    b = Builder(env, log)
    b.run(only)
    man = b.write(out_dir)
    log('wrote %s: %d stems, %d passes, %d blobs (%d bytes)' % (out_dir, len(man['stems']), len(man['passes']), len(man['blobs']),
                                                                sum(v[1] for v in man['blobs'].values())))
    if report:
        rep = dict(b.report)
        rep['notes'] = list(env.notes)
        rep['summary'] = {'stems': len(man['stems']), 'passes': len(man['passes']), 'blobs': len(man['blobs']), 'unportable': len(b.unportable)}
        with open(report, 'w', encoding='utf-8') as fh:
            json.dump(rep, fh, indent=1, default=str)
    return b


def main(argv=None):
    ap = argparse.ArgumentParser(prog='h2conv.ce_port_build', description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd')
    a = sub.add_parser('build')
    a.add_argument('--mod', required=True, help='the workshop mod folder (Steam workshop item 3583572796: maps\\*.map + halo2\\h2_maps_win64_dx11\\shared.map)')
    a.add_argument('--kit', required=True, help='the Halo 2 editing kit (H2EK) folder that holds tags\\rasterizer\\pixel_shaders_dx9\\*.psh')
    a.add_argument('--vista-maps', default=None)
    a.add_argument('--out', default=CP.DATA_DIR)
    a.add_argument('--report')
    a.add_argument('--only', nargs='*', default=[])
    args = ap.parse_args(argv)
    if args.cmd == 'build':
        from . import paths as _paths
        vm = args.vista_maps or _paths.vista_maps_dir()
        build(args.mod, args.kit, vm, args.out, args.report, tuple(args.only))
        return 0
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
