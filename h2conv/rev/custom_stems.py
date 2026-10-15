# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Custom shader templates (stem) under a STOCK name: detection by fingerprint, exactly.

Used by h2conv/rev/planner.py (`_Builder.custom_stem_entry`).

A community map can carry its OWN stem tag under the name of a stock template (e.g. `dreamervar1.map`: `tex_bump` whose 7th pass is the custom pass
`lightmap_tex_detail_custom`).  The reverse planner takes MCC's stem for every name MCC has (one tag per (group, name); MCC's donor shaders use it), so
the community stem was replaced and its shaders were mapped onto a stem they were not compiled against.  The fix (planner) ports such a stem under a
private, collision-free name; this module decides WHICH local stems that applies to:

    kind 'custom'          the name exists in MCC, the local stem's fingerprint equals neither MCC's stem of that name NOR any stock Vista stem of that
                           name  -> private port
    kind 'mcc_equal'       fingerprint == MCC's  -> MCC's stem is interchangeable (donor)
    kind 'mcc_reauthored'  fingerprint == a stock Vista stem of that name but != MCC's: MCC re-authored the template (the 14 measured names of
                           shader_tags.MCC_RESTRUCTURED_STEMS + any other name the data shows) -> donor + the exact reverse mapping of handlers/shaders.py
    kind 'no_mcc_name'     MCC has no stem of that name -> the existing faithful port under the SAME name (shader_port.PortPolicy)
    kind 'unreadable'      no Postprocess Definition

The fingerprint is shader_params.stem_fingerprint: LODs, Layers, Passes by pass tag NAME, Implementations, Remappings - the data every shader array is a
function of.  Stock Vista corpus = the 40 stock playable maps + shared / single_player_shared / mainmenu of the Vista maps folder (cached in
<work folder>/rev_planner/stock_vista_stem_fps_v1.json, keyed by file size + mtime).

CLI:  py -3 -m h2conv.rev.custom_stems scan <folder-with-vista-maps> [--json out.json] [--limit N]
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import collections
import glob
import json
import os
import re
import sys

from .. import paths as _paths
from .. import tagtree as T

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_FILE = os.path.join(_paths.work_dir(), 'rev_planner', 'stock_vista_stem_fps_v1.json')
CACHE_VERSION = 1

# the stock playable Vista maps (h2conv/batch.py STOCK_MAP_NAMES) + the three shared databases
STOCK_VISTA_MAPS = ('00a_introduction', '01a_tutorial', '01b_spacestation', '03a_oldmombasa', '03b_newmombasa', '04a_gasgiant', '04b_floodlab',
                    '05a_deltaapproach', '05b_deltatowers', '06a_sentinelwalls', '06b_floodzone', '07a_highcharity', '07b_forerunnership',
                    '08a_deltacliffs', '08b_deltacontrol', 'zanzibar', 'ascension', 'backwash', 'beavercreek', 'burial_mounds', 'coagulation',
                    'colossus', 'foundation', 'headlong', 'cyclotron', 'lockout', 'midship', 'waterworks', 'containment', 'warlock', 'deltatap',
                    'turf', 'elongation', 'gemini', 'dune', 'triplicate', 'street_sweeper', 'needle', 'derelict', 'highplains',
                    'shared', 'single_player_shared', 'mainmenu')

KINDS = ('custom', 'mcc_equal', 'mcc_reauthored', 'no_mcc_name', 'unreadable')


def private_suffix(vista_cache):
    """The map's file stem as an identifier ([0-9a-z_]): the suffix of the private names (`<stock name>__<suffix>`)."""
    stem = os.path.splitext(os.path.basename(getattr(vista_cache, 'path', '') or ''))[0] or getattr(vista_cache.header, 'name', '') or 'map'
    s = re.sub(r'[^0-9A-Za-z_]+', '_', stem).strip('_').lower()
    return s or 'map'


def private_name(stock_name, vista_cache, taken=lambda n: False):
    """`<stock name>__<map stem>`, made unique with `_2`, `_3` ... while taken(candidate) says the (group, name) exists."""
    base = '%s__%s' % (stock_name, private_suffix(vista_cache))
    cand = base
    n = 1
    while taken(cand):
        n += 1
        cand = '%s_%d' % (base, n)
    return cand


# ================================================================================================
# fingerprints
# ================================================================================================
def _shaders_mod():
    from .handlers import shaders as RS
    return RS


def vista_stem_fp(cache, tag, lay_vista):
    """Stem fingerprint of a Vista stem tag that carries data (None when it has no Postprocess Definition)."""
    RS = _shaders_mod()
    root = T.decode_tag(cache.read_addr, lay_vista['stem'], tag.addr, 'stem')
    sd = RS.stem_data(root, RS._vista_name_of(cache), tag.name or '')
    return sd.fp if sd is not None else None


def mcc_stem_fp(env, donor):
    """Stem fingerprint of an MCC donor stem (McDonor)."""
    RS = _shaders_mod()
    c, t = env.mcc.tag(donor)
    root = T.decode_tag(lambda a, n: c.read_at_addr(a, n), env.lay_mcc['stem'], t.addr, 'stem')
    sd = RS.stem_data(root, RS._mcc_name_of(c), donor.name)
    return sd.fp if sd is not None else None


def _sig(p):
    st = os.stat(p)
    return [os.path.basename(p), st.st_size, st.st_mtime_ns]


def stock_vista_stem_fps(env):
    """{stem name (lower): sorted [fingerprints]} over the stock Vista corpus (STOCK_VISTA_MAPS of env.vista_maps).  Cached per env and on disk."""
    hit = getattr(env, '_stock_stem_fps', None)
    if hit is not None:
        return hit
    from ..vista_cache import VistaCache
    paths = []
    for nm in STOCK_VISTA_MAPS:
        p = os.path.join(env.vista_maps, nm + '.map')
        if os.path.isfile(p):
            paths.append(p)
    sigs = [_sig(p) for p in paths]
    disk = None
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            disk = json.load(f)
        if disk.get('version') != CACHE_VERSION or disk.get('sigs') != sigs:
            disk = None
    except Exception:                                                  # noqa: BLE001
        disk = None
    if disk is not None:
        out = {k: list(v) for k, v in disk['fps'].items()}
    else:
        acc = collections.defaultdict(set)
        for p in paths:
            c = VistaCache(p)
            try:
                for t in c.tags:
                    if t.group != 'stem' or not t.addr or t.is_empty or not t.name:
                        continue
                    try:
                        fp = vista_stem_fp(c, t, env.lay_vista)
                    except Exception:                                  # noqa: BLE001  one odd tag never aborts the corpus
                        fp = None
                    if fp:
                        acc[t.name.lower()].add(fp)
            finally:
                c.close()
        out = {k: sorted(v) for k, v in acc.items()}
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            tmp = CACHE_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'version': CACHE_VERSION, 'sigs': sigs, 'fps': out}, f)
            os.replace(tmp, CACHE_FILE)
        except Exception:                                              # noqa: BLE001
            pass
    env._stock_stem_fps = out
    return out


def mcc_stem_fp_by_name(env, name):
    """(fingerprint, McDonor) of MCC's stem of that name (the planner's own donor choice), (None, None) if MCC has none.  Memoised on env."""
    memo = getattr(env, '_mcc_stem_fps', None)
    if memo is None:
        memo = env._mcc_stem_fps = {}
    k = (name or '').lower()
    if k not in memo:
        d = env.mcc.find('stem', name)
        memo[k] = (mcc_stem_fp(env, d) if d is not None else None, d)
    return memo[k]


# ================================================================================================
# classification
# ================================================================================================
Verdict = collections.namedtuple('Verdict', 'kind name fp_local fp_mcc fps_vista mcc_label')


def classify_local_stem(env, vista_cache, tag, name=None):
    """Verdict for a stem tag that carries data in `vista_cache` (the input map)."""
    name = name or tag.name or ''
    try:
        fl = vista_stem_fp(vista_cache, tag, env.lay_vista)
    except Exception:                                                  # noqa: BLE001
        fl = None
    fm, d = mcc_stem_fp_by_name(env, name)
    fv = stock_vista_stem_fps(env).get(name.lower(), [])
    label = d.label if d is not None else None
    if fl is None:
        return Verdict('unreadable', name, None, fm, fv, label)
    if d is None:
        return Verdict('no_mcc_name', name, fl, None, fv, None)
    if fl == fm:
        return Verdict('mcc_equal', name, fl, fm, fv, label)
    if fl in fv:
        return Verdict('mcc_reauthored', name, fl, fm, fv, label)
    return Verdict('custom', name, fl, fm, fv, label)


def scan_map(env, vista_cache):
    """[Verdict] of every local stem of a Vista map that is NOT interchangeable with MCC's (kinds 'custom' and 'unreadable'), plus counts of every kind."""
    counts = collections.Counter()
    custom = []
    for t in vista_cache.tags:
        if t.group != 'stem' or not t.addr or t.is_empty or not t.name:
            continue
        v = classify_local_stem(env, vista_cache, t)
        counts[v.kind] += 1
        if v.kind in ('custom', 'unreadable'):
            custom.append(v)
    return custom, counts


def stale_shaders(env, vista_cache, memo=None):
    """[(shader name, stem name)] of the shaders of a Vista map whose own skeleton (LOD layer ranges, Layers, Passes, Implementations count) does not fit the
    stem they reference IN THE SAME MAP (local tag or Vista shared.map): a shader compiled against another revision of its template than the one in the map."""
    RS = _shaders_mod()
    out = []
    memo = {} if memo is None else memo
    lay = env.lay_vista
    for t in vista_cache.tags:
        if t.group != 'shad' or not t.addr or t.is_empty:
            continue
        try:
            node = T.decode_tag(vista_cache.read_addr, lay['shad'], t.addr, 'shad')
            d = node.u32(4)
            pps = RS._blk(node, 'Postprocess Definition')
            if not pps or d in (0xFFFFFFFF, 0):
                continue
            st = vista_cache.tags[d & 0xFFFF]
            if st.datum != d or st.group != 'stem':
                continue
            rt, rc = st, vista_cache
            if not st.addr:
                rt, rc = env.shared.tags[st.index], env.shared
                if rt.datum != st.datum or not rt.addr:
                    continue
            key = (rc.path, rt.index)
            sd = memo.get(key)
            if sd is None:
                root = T.decode_tag(rc.read_addr, lay['stem'], rt.addr, 'stem')
                sd = memo[key] = RS.stem_data(root, RS._vista_name_of(rc), st.name or '') or False
            if sd and not RS.skeleton_fits(RS.shader_arrays(pps[0]), sd):
                out.append((t.name, st.name))
        except Exception:                                              # noqa: BLE001
            continue
    return out


# ================================================================================================
# CLI
# ================================================================================================
def _scan_cli(folder, json_out=None, limit=None):
    from . import planner as RP
    from ..vista_cache import VistaCache
    env = RP.RevEnv()
    rows = {}
    n = 0
    print('stock Vista corpus: %d stem names' % len(stock_vista_stem_fps(env)))
    for p in sorted(glob.glob(os.path.join(folder, '*.map'))):
        if limit and n >= limit:
            break
        name = os.path.splitext(os.path.basename(p))[0]
        try:
            c = VistaCache(p)
        except Exception as ex:                                        # noqa: BLE001
            print('%-44s UNREADABLE %r' % (name, ex))
            continue
        try:
            if c.header.type not in (0, 1, 2):
                continue
            n += 1
            custom, counts = scan_map(env, c)
            stale = stale_shaders(env, c)
        except Exception as ex:                                        # noqa: BLE001
            print('%-44s SCAN FAILED %r' % (name, ex))
            c.close()
            continue
        c.close()
        if custom or stale:
            rows[name] = {'custom_stems': [{'name': v.name, 'kind': v.kind, 'fp_local': v.fp_local, 'fp_mcc': v.fp_mcc, 'fps_vista_stock': v.fps_vista,
                                            'mcc_label': v.mcc_label} for v in custom],
                          'kinds': dict(counts), 'stale_shaders': [list(x) for x in stale]}
            print('%-44s custom-under-stock-name: %s%s' % (name, [v.name.split('\\')[-1] for v in custom],
                                                           ('   stale-skeleton shaders: %d' % len(stale)) if stale else ''))
    print('%d playable maps scanned, %d with custom stems or stale shaders' % (n, len(rows)))
    if json_out:
        with open(json_out, 'w', encoding='utf-8') as f:
            json.dump(rows, f, indent=1)
    return rows


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == 'scan':
        out = None
        limit = None
        if '--json' in argv:
            out = argv[argv.index('--json') + 1]
        if '--limit' in argv:
            limit = int(argv[argv.index('--limit') + 1])
        _scan_cli(argv[1], out, limit)
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
