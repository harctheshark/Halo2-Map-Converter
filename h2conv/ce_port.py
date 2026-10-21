# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Halo-CE port templates (Steam workshop mod 3583572796 and maps built on its shared.map): faithful D3D9 ports of DXBC-only shader templates.

The problem.  MCC keeps pixel shader code only as DXBC.  The mod's maps use ~37 templates (``stem`` = shader_template, ``spas`` = shader_pass, ``vrtx``) that exist in
no Halo 2 Vista map, so their shaders ended up with no Template (``shad.null_template``).  Vista needs the same programs as Direct3D 9 byte code.

The route (``h2conv.ce_port_build`` makes the data once, this module only uses it; nothing is compiled while a map is converted):
  1. The H2EK ``.psh`` source of every pass is recompiled with the MCC tool's recipe and must reproduce the DXBC the mod shipped BYTE FOR BYTE (that proves the
     source is the program the map holds).  The DX11 branch of that source - the text MCC compiled - is then compiled for Direct3D 9 (ps_2_0 / ps_2_0 / ps_3_0, the
     three variants of a Vista pixel shader); only the sampling API changes (``dx11to9``), every arithmetic statement is kept.
  2. A pass with no source on this machine (``h1\\shaders\\passes\\scopeblur``) is recovered from its DXBC by ``h2conv.dxbc9`` (statement by statement).
  3. Every D3D9 blob is PROVEN against the DXBC of the map with the converter's two evaluators (random + stress inputs, the alpha test of the pass emulated on the
     D3D9 side, the vertex shader of the pass checked against its MCC namesake) before it is entered into the table.  Anything not proven is not in the table.
  4. A vertex shader MCC re-authored (``water_pool_static`` of beavercreek: a per-vertex divided screen uv where Vista writes the reflection vector) is ported as a
     PRIVATE vrtx tag ``<name>__ce``: the H2EK ``.vertex_shader`` tag holds its HLSL text, which reproduces the map's vs_5_0 DXBC byte for byte and is compiled for
     vs_2_0 (``dx11to9.vs_to_d3d9``: two statements differ) and proven lane for lane against the DXBC.

The data (``h2conv/data/ce_port/``): the D3D9 blobs (by MD5) and a registry of the tags that may be ported, each with the SHA-256 of its canonical form (tag
references by NAME, string ids by text, DXBC included) and, for a pass, the blob of every compiled-shader slot in tag order.  A tag whose canonical form differs from the registered one is
never ported, so a look-alike from another mod falls back to the old behaviour (dropped, ``shad.null_template``).

Gate (all must hold, otherwise nothing here runs and every other conversion is byte-identical to before):
  * ``opts['ce_port']`` is not False;  * the input has a custom shared.map (``planner._Builder.custom_shared``);  * the registry data files exist;
  * the MCC tag matches the registry entry (name + digest).
The planner calls ``decision()``; the tag handlers (``handlers/shaders.py``) call ``build_tag()``.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:                                                     # noqa: BLE001  pragma: no cover
    pass

import hashlib
import json
import os

from . import tagtree as T

FORMAT = 1
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'ce_port')
MANIFEST_NAME = 'ce_port_v1.json'
BLOBS_NAME = 'ce_port_v1.bin'
PRIVATE_SUFFIX = '__ce'          # a stock-named pass whose MCC structure differs from Vista's is ported under <name>__ce (only the ported stems reference it)
GROUPS = ('stem', 'spas', 'vrtx')


class CEPortError(Exception):
    pass


# ================================================================================================
# canonical form / digest  (independent of datums, string-id values and file offsets)
# ================================================================================================
def digest(node, name_of, sid_of):
    """SHA-256 (hex) of the canonical form of a decoded MCC tag Node: struct bytes with tag references replaced by (group, lower-case name), string ids by their text,
    block / data pointer words and runtime handles zeroed, block counts and data payloads (compiled code included) in field order."""
    h = hashlib.sha256()
    _feed(h, node, name_of, sid_of)
    return h.hexdigest()


def _feed(h, node, name_of, sid_of):
    d = bytearray(node.data)
    parts = []
    for f in node.fields:
        if f.type == 'tagref':
            off = f.offset + (4 if f.size == 8 else 0)
            parts.append(('ref', f.offset, name_of(int.from_bytes(d[off:off + 4], 'little'))))
            d[f.offset:f.offset + f.size] = b'\0' * f.size
        elif f.type == 'stringid':
            parts.append(('sid', f.offset, sid_of(int.from_bytes(d[f.offset:f.offset + 4], 'little'))))
            d[f.offset:f.offset + 4] = b'\0\0\0\0'
        elif f.type in T.BLOCK_TYPES or f.type in T.DATA_TYPES:
            d[f.offset + 4:f.offset + 8] = b'\0\0\0\0'
        elif f.name and 'Runtime' in f.name:
            d[f.offset:f.offset + f.size] = b'\0' * f.size
    h.update(bytes(d))
    h.update(repr(parts).encode('utf-8', 'replace'))
    for f in node.fields:
        if f.type in T.BLOCK_TYPES:
            kids = node.blocks.get(f.offset, [])
            h.update(b'B%d:%d;' % (f.offset, len(kids)))
            for k in kids:
                _feed(h, k, name_of, sid_of)
        elif f.type in T.DATA_TYPES:
            p = bytes(node.datas.get(f.offset, b''))
            h.update(b'D%d:%d;' % (f.offset, len(p)))
            h.update(p)


# ================================================================================================
# registry (the data files)
# ================================================================================================
class Registry(object):
    def __init__(self, manifest, blob_path):
        self.manifest = manifest
        self.stems = manifest.get('stems', {})
        self.passes = manifest.get('passes', {})
        self.vrtx = manifest.get('vrtx', {})           # private vertex shaders (vs_2_0), see build_tag
        self.blobs = manifest.get('blobs', {})
        self._blob_path = blob_path
        self._fh = None
        self._cache = {}

    def table(self, group):
        return {'stem': self.stems, 'spas': self.passes, 'vrtx': self.vrtx}[group]

    def blob(self, md5_hex):
        """D3D9 blob for the DXBC blob with this MD5.  One bytes object per MD5 (identical blobs share a payload, like the cache builder dedupes)."""
        b = self._cache.get(md5_hex)
        if b is not None:
            return b
        ent = self.blobs.get(md5_hex)
        if ent is None:
            raise CEPortError('no D3D9 port for the DXBC blob %s' % md5_hex)
        if self._fh is None:
            self._fh = open(self._blob_path, 'rb')
        self._fh.seek(ent[0])
        b = self._fh.read(ent[1])
        if len(b) != ent[1]:
            raise CEPortError('%s is truncated' % BLOBS_NAME)
        self._cache[md5_hex] = b
        return b

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


_STATE = {'loaded': False, 'registry': None}


def registry(path=None):
    """The registry, loaded once.  None when the data files are missing or of another format (the feature is then off).  `path` (a folder) is for tests."""
    if path is not None:
        return _load(path)
    if not _STATE['loaded']:
        _STATE['registry'] = _load(DATA_DIR)
        _STATE['loaded'] = True
    return _STATE['registry']


def reset():
    """forget the loaded registry (tests)"""
    if _STATE['registry'] is not None:
        _STATE['registry'].close()
    _STATE['loaded'], _STATE['registry'] = False, None


def _load(folder):
    mp, bp = os.path.join(folder, MANIFEST_NAME), os.path.join(folder, BLOBS_NAME)
    if not (os.path.isfile(mp) and os.path.isfile(bp)):
        return None
    with open(mp, 'r', encoding='utf-8') as fh:
        man = json.load(fh)
    if man.get('format') != FORMAT:
        return None
    return Registry(man, bp)


def enabled(opts):
    return (opts or {}).get('ce_port', True) is not False and registry() is not None


# ================================================================================================
# the planner side
# ================================================================================================
def decision(group, name, node, name_of, sid_of, reg=None):
    """-> dict (the registry entry, plus 'kind') when the MCC tag (group, name; decoded `node`) is registered AND its canonical form matches, else None.
    The caller (planner) has already checked the gate."""
    reg = reg or registry()
    if reg is None or group not in GROUPS or not name:
        return None
    ent = reg.table(group).get(name.lower())
    if ent is None:
        return None
    if digest(node, name_of, sid_of) not in ent.get('digests', (ent['digest'],)):
        return None
    out = dict(ent)
    out['kind'] = group
    out['name'] = name
    return out


# ================================================================================================
# the conversion side
# ================================================================================================
def _trim_postprocess(root):
    """MCC stems / passes hold three Postprocess Definition elements ([1] / [2] are the empty DX9 / Xbox slots); Vista holds one."""
    f = root.field('Postprocess Definition')
    kids = root.blocks.get(f.offset, [])
    if len(kids) > 1:
        for k in kids[1:]:                    # the empty slots hold zeroed struct bytes and empty blocks only; anything else would be lost silently
            if any(k.data) or any(v for v in k.blocks.values()) or any(v for v in k.datas.values()):
                raise CEPortError('Postprocess Definition [1]/[2] are not empty')
        root.blocks[f.offset] = kids[:1]
        root.set_u32(f.offset, 1)


def _build_vrtx(ctx, entry, reg, mtag, cache):
    """Vista vrtx root Node of a CE-ported vertex shader.  One Vista 'Geometry Classifications' element per MCC 'Geometry Classifications DX11' element (same index = same
    geometry class), holding the proven vs_2_0 byte code of the registry (an empty MCC class stays empty); 'Output Swizzles' = the registry value (the lanes the
    program writes, as the MCC tool computed it from the same source).  The DX9 / Xbox1 blocks of the MCC tag are empty in every cache."""
    from . import transcode as X
    ent = entry.ce_port
    mroot = ctx.decode_mcc(mtag, cache)
    mcls = mroot.block('Geometry Classifications DX11')
    classes = ent['classes']
    if len(mcls) != len(classes):
        raise CEPortError('%s: %d geometry classifications, the registry lists %d' % (entry.name, len(mcls), len(classes)))
    root = X.empty_tag_node(ctx, 'vrtx')
    gf = root.field('Geometry Classifications')
    kids, n = [], 0
    for el, want in zip(mcls, classes):
        child = T.Node('vrtx', T.child_path('', gf), gf.children, T.element_size(gf))
        cf, kf = child.field('Compiled Shader'), child.field('Code')
        has = bool(bytes(el.datas.get(el.field('Compiled Shader').offset, b'')))
        if has != bool(want):
            raise CEPortError('%s: a geometry classification is %s in the tag and %s in the registry' % (entry.name, 'filled' if has else 'empty', 'empty' if has else 'filled'))
        blob = reg.blob(want) if want else b''
        n += bool(want)
        child.datas[cf.offset] = blob
        child.set_u32(cf.offset, len(blob))
        child.datas[kf.offset] = b''
        kids.append(child)
    root.blocks[gf.offset] = kids
    root.set_u32(gf.offset, len(kids))
    root.set_u32(root.field('Output Swizzles').offset, ent['output_swizzles'])
    ctx.stats['ce_port:vrtx'] += 1
    ctx.stats['ce_port:vrtx_blobs'] += n
    return root


def build_tag(ctx, entry):
    """Vista root Node of a CE-ported stem / spas: the generic field-by-field conversion of the MCC tag, the empty DX9 / Xbox postprocess slots dropped,
    and (spas) every Pixel Shader blob replaced by its proven D3D9 port from the registry table."""
    from . import transcode as X
    reg = registry()
    if reg is None:
        raise CEPortError('the CE port data files are missing')
    cache = ctx.cache_for(entry.src_map)
    mtag = cache.tags[entry.src_index]
    if entry.group == 'vrtx':
        return _build_vrtx(ctx, entry, reg, mtag, cache)
    root = X.convert_mcc_tag(ctx, mtag, cache, handlers=not ctx.opts.get('no_node_handlers'))
    _trim_postprocess(root)
    ctx.stats['ce_port:%s' % entry.group] += 1
    if entry.group != 'spas':
        return root
    # the registry lists the D3D9 blob (by MD5) of every compiled-shader slot of the pass in tag order: implementation, Pixel Shader element, variant 0 / 1 / 2
    flat = entry.ce_port['d3d9']
    mroot = ctx.decode_mcc(mtag, cache)
    mpp = mroot.block('Postprocess Definition')[0]
    vpp = root.block('Postprocess Definition')[0]
    n = 0
    for mi, vi in zip(mpp.block('Implementations'), vpp.block('Implementations')):
        for mps, vps in zip(mi.block('Pixel Shader'), vi.block('Pixel Shader')):
            moffs, voffs = sorted(mps.datas), sorted(vps.datas)
            if len(moffs) != len(voffs):
                raise CEPortError('%s: Pixel Shader element has %d / %d compiled shader slots' % (entry.name, len(moffs), len(voffs)))
            for mo, vo in zip(moffs, voffs):
                want = flat[n]
                n += 1
                if not bytes(mps.datas[mo]):
                    if want:
                        raise CEPortError('%s: registry has a D3D9 blob for an empty DXBC slot' % entry.name)
                    vps.datas[vo] = b''
                    continue
                if not want:
                    raise CEPortError('%s: no D3D9 blob registered for a DXBC slot' % entry.name)
                vps.datas[vo] = reg.blob(want)
    if n != len(flat):
        raise CEPortError('%s: registry lists %d slots, the tag has %d' % (entry.name, len(flat), n))
    ctx.stats['ce_port:blobs'] += n
    return root


def summary(plan):
    """report section: the tags the plan ported (None when none)"""
    rows = [(e.group, e.name, (e.ce_port or {}).get('private')) for e in plan.all_entries if getattr(e, 'ce_port', None) and e.action == 'convert']
    if not rows:
        return None
    out = {'stems': sorted(n for g, n, p in rows if g == 'stem'), 'passes': sorted(n for g, n, p in rows if g == 'spas'),
           'private_passes': sorted(n for g, n, p in rows if p and g == 'spas')}
    if any(g == 'vrtx' for g, n, p in rows):                    # only when there is one: the report of every other CE map stays as it was
        out['vertex_shaders'] = sorted(p for g, n, p in rows if g == 'vrtx')
    return out
