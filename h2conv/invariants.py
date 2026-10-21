# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Semantic invariants of a CONVERTED Vista map that the format-level validator (validate.py) does not know.

The topic handlers (gameplay, havok, scripts, shaders) establish them; this module re-checks the WRITTEN file, so an encoder or
planner mistake that undoes a handler is caught too.  Every rule was measured on the 43 stock Vista maps (calibration:
0 findings on all of them).

    check_map(path, maps_dir=None) -> Findings   (.items = [(level, code, message)], .counts = {name: checked})
        gameplay  matg Global Vertex Shaders <= 32 and an empty hidden block at +0xD8; mulg Runtime Weapons <= 17 / Vehicles <= 11 and no
                  General Event 49; goof Variant Type <= 6, Setting Category <= 0x6F / 0x27; scnr Game Type 1..4 <= 14, scenery Valid
                  Multiplayer Games <= 0x7F, spawn zone Relevant Games <= 0xFF, no overload type 12, no null Simulation Definition entry;
                  hudg Button Icons present; scnr Simulation Definition Table sorted ascending (the engine binary-searches it) and holding
                  the shared twin of every local entry that shadows a shared placeholder; jmad sort blocks (Modes ... Weapon List,
                  Vehicle Suspension, Object Overlays) findable by the engine's binary search (halo2.exe sub_4F5B8F)
        havok     every MOPP program decodes with the Vista VM grammar; runtime pointers / User Data are 0 (small ints where the fixup
                  keeps them); Runtime Code Pointer is 0 or a stock constant
        scripts   the scnr syntax-node table passes handlers.scripts.validate_program (datums, salts, arity, argument types) and the
                  engine limits (36,864 nodes, 1024 scripts, 256 globals)
        shaders   every local shad has a Template that resolves to a stem (ERROR sh.null_template / sh.stem_unresolved: a NONE Template crashes
                  the engine when the shader is drawn) and mirrors the skeleton of that stem (handlers.shaders.check_consistency)
        spawn     multiplayer maps: the closure of the Vista shared matg is listed as shared placeholders (all 25 stock MP maps do), and, when
                  mods/maps/carto_shared.map exists, every shared slot that Cartographer-injected content refers to is listed (stock maps'
                  own misses allowed).  Missing slots (jmad masterchief 10231, ligh flashlight_3p 10435) leave the player uncreatable
                  under Cartographer's seasonal / skeleton biped (chronopolis, 2026-10-20)
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import os
import struct
import sys
import time

from . import paths as _paths
from . import plugins as P
from . import tagtree as T
from .vista_cache import VistaCache

NULL = 0xFFFFFFFF
_LAY = {}


def __getattr__(name):                  # the former module constant, for the developer tools that still read it
    if name == 'VISTA_MAPS_DEFAULT':
        return _paths.vista_maps_dir()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


def _layouts():
    if 'v' not in _LAY:
        _LAY['v'] = P.load_layouts('vista', corrected=True)
    return _LAY['v']


class Findings(object):
    def __init__(self, path, stock=False):
        self.path = path
        self.stock = stock               # True = a map written by the Vista tool (calibration runs): skip the runtime-pointer rules
        self.items = []
        self.counts = {}

    def add(self, level, code, msg):
        if len([1 for i in self.items if i[1] == code]) < 8:
            self.items.append((level, code, msg))
        self.counts['finding:' + code] = self.counts.get('finding:' + code, 0) + 1

    def count(self, name, n=1):
        self.counts[name] = self.counts.get(name, 0) + n

    @property
    def errors(self):
        return sum(v for k, v in self.counts.items() if k.startswith('finding:') and self._lvl(k[8:]) == 'error')

    @property
    def warnings(self):
        return sum(v for k, v in self.counts.items() if k.startswith('finding:') and self._lvl(k[8:]) == 'warning')

    def _lvl(self, code):
        for lv, c, _m in self.items:
            if c == code:
                return lv
        return 'warning'

    def to_dict(self):
        return {'errors': self.errors, 'warnings': self.warnings, 'items': [list(i) for i in self.items], 'counts': dict(self.counts)}


def _val(node, name):
    f = node.field(name)
    if f is None:
        return None
    return int.from_bytes(bytes(node.data[f.offset:f.offset + f.size]), 'little')


def _where(tag, node, extra=''):
    return '%s %s%s%s' % (tag.group, tag.name, node.path or '', extra)


# --------------------------------------------------------------------------------------------------
# order-sensitive tables the engine binary-searches
# --------------------------------------------------------------------------------------------------
JMAD_EXTRA_SORTED = {('', 'Vehicle Suspension'), ('', 'Object Overlays')}      # searched by sub_4F5F25 / sub_4F5F3B, not flagged sort="true"


def _s32(x):
    return x - (1 << 32) if x & 0x80000000 else x


def engine_sim_lookup(table, datum):
    """halo2.exe sub_48CAC3 + comparator sub_5F4116 (`*a - *b` as int32): index of `datum` in `table`, or -1 (= "no definition")."""
    base, n = 0, len(table)
    if n == 0:
        return -1
    while True:
        mid = base + (n >> 1)
        diff = (_s32(datum) - _s32(table[mid]) + (1 << 31)) % (1 << 32) - (1 << 31)
        if diff == 0:
            return mid
        if diff > 0:
            base = mid + 1
            n -= 1
        n >>= 1
        if n == 0:
            return -1


def engine_block_find(keys, key):
    """halo2.exe sub_4F5B8F: signed 32-bit binary search over the first dword of a tag block's elements; index or -1."""
    lo, hi = 0, len(keys) - 1
    while lo <= hi:
        mid = (lo + hi) >> 1
        e = keys[mid]
        if e == key:
            return mid
        if e > key:
            hi = mid - 1
        else:
            lo = mid + 1
    return -1


def _check_sim_table(F, tag, root):
    f = next((x for x in root.fields if x.name == 'Simulation Definition Table' and x.type in T.BLOCK_TYPES), None)
    if f is None:
        return
    els = root.blocks.get(f.offset) or []
    if not els:
        return
    tab = [e.u32(0) for e in els]
    F.count('scnr simulation tables')
    lost = [d for d in set(tab) if d != NULL and engine_sim_lookup(tab, d) < 0]
    if lost:
        F.add('error', 'gp.scnr_simulation_unsorted',
              "%s: the engine's binary search (sub_5F43BB) cannot find %d of %d distinct Simulation Definition entries (table not sorted "
              "ascending as int32; e.g. 0x%08X) - replicated objects of those types get definition index 0" % (
                  _where(tag, root), len(lost), len(set(tab)), sorted(lost)[0]))
    twin_of = getattr(F, 'sim_names', None)
    ph = getattr(F, 'sim_placeholders', None)
    if twin_of and ph:
        have = set(tab)
        miss = []
        for d in have:
            info = twin_of.get(d)
            if info is None:
                continue
            miss += [t for t in ph.get(info, ()) if t not in have]
        F.count('scnr simulation shadow twins checked')
        if miss:
            F.add('error', 'gp.scnr_simulation_twin_missing',
                  '%s: %d shared placeholder(s) with the (group, name) of a LOCAL Simulation Definition entry are missing from the table '
                  '(objects created from the shared tag could not be replicated; e.g. 0x%08X)' % (_where(tag, root), len(miss), miss[0]))


def _check_jmad_sorted(F, tag, root):
    for n in root.iter_nodes():
        for f in n.fields:
            if f.type not in T.BLOCK_TYPES:
                continue
            if str(f.attrs.get('sort', '')).lower() != 'true' and (n.path, f.name) not in JMAD_EXTRA_SORTED:
                continue
            els = n.blocks.get(f.offset) or []
            if len(els) < 2:
                continue
            keys = [_s32(e.u32(0)) for e in els]
            F.count('jmad sort blocks')
            F.count('jmad label lookups', len(keys))
            lost = sum(1 for k in keys if engine_block_find(keys, k) < 0)
            if lost:
                F.add('error', 'gp.jmad_unsorted',
                      '%s: block %s/%s (%d elements) is not sorted by label: %d lookups of sub_4F5B8F would miss' % (
                          _where(tag, root), n.path, f.name, len(keys), lost))


# --------------------------------------------------------------------------------------------------
# gameplay
# --------------------------------------------------------------------------------------------------
def _check_gameplay(F, tag, root):
    g = tag.group
    if g == 'scnr':
        _check_sim_table(F, tag, root)
    elif g == 'jmad':
        _check_jmad_sorted(F, tag, root)
        return
    for n in root.iter_nodes():
        p = n.path
        if g == 'matg':
            if p == '':
                f = n.fields and next((x for x in n.fields if x.offset == 0xD8 and x.type == 'tagblock'), None)
                if f is not None:
                    cnt = n.u32(f.offset)
                    F.count('matg hidden block')
                    if cnt:
                        F.add('error', 'gp.matg_hidden_block', '%s: hidden block at +0xD8 has %d elements (Vista: element size 0, count 0)' % (_where(tag, n), cnt))
            elif p == '/Rasterizer Data':
                f = n.field('Global Vertex Shaders')
                if f is not None:
                    F.count('matg global vertex shaders')
                    if n.u32(f.offset) > 32:
                        F.add('error', 'gp.matg_vertex_shaders', '%s: Global Vertex Shaders %d > 32 (engine enum)' % (_where(tag, n), n.u32(f.offset)))
        elif g == 'mulg':
            if p == '/Runtime':
                for nm, lim in (('Weapons', 17), ('Vehicles', 11)):
                    f = n.field(nm)
                    if f is not None:
                        F.count('mulg runtime lists')
                        if n.u32(f.offset) > lim:
                            F.add('error', 'gp.mulg_list', '%s: %s %d > %d (positions are engine enums)' % (_where(tag, n), nm, n.u32(f.offset), lim))
            elif p.endswith('/General Events'):
                ev = _val(n, 'Event')
                if ev is not None:
                    F.count('mulg events')
                    if ev == 49:
                        F.add('error', 'gp.mulg_event49', '%s: General Event 49 (Pre Game Over) does not exist in Vista' % _where(tag, n))
        elif g == 'goof':
            vt = _val(n, 'Variant Type')
            if vt is not None and p in ('', '/Default Variants'):
                F.count('goof variant types')
                if vt > 6:
                    F.add('error', 'gp.goof_variant_type', '%s: Variant Type %d > 6' % (_where(tag, n), vt))
            sc = _val(n, 'Setting Category')
            if sc is not None:
                F.count('goof setting categories')
                lim = 0x27 if p.startswith('/Game Engine Settings') else 0x6F
                if sc > lim:
                    F.add('error', 'gp.goof_setting_category', '%s: Setting Category 0x%X > 0x%X' % (_where(tag, n), sc, lim))
        elif g == 'scnr':
            if p in ('/Player Starting Locations', '/Netgame Equipment', '/Starting Equipment'):
                for i in range(1, 5):
                    v = _val(n, 'Game Type %d' % i)
                    if v is not None:
                        F.count('scnr game types')
                        if v > 14:
                            F.add('error', 'gp.scnr_game_type', '%s: Game Type %d = %d > 14 (MCC-only game)' % (_where(tag, n), i, v))
            elif p == '/Scenery':
                v = _val(n, 'Valid Multiplayer Games')
                if v is not None:
                    F.count('scnr scenery masks')
                    if v & 0xFF80:
                        F.add('error', 'gp.scnr_scenery_games', '%s: Valid Multiplayer Games 0x%X has MCC-only bits' % (_where(tag, n), v))
            elif p in ('/Spawn Data/Static Respawn Zones', '/Spawn Data/Static Initial Spawn Zones'):
                v = _val(n, 'Relevant Games')
                if v is not None:
                    F.count('scnr zone masks')
                    if v & 0xFFFFFF00:
                        F.add('error', 'gp.scnr_zone_games', '%s: Relevant Games 0x%X has MCC-only bits' % (_where(tag, n), v))
            elif p == '/Spawn Data/Dynamic Spawn Overloads':
                v = _val(n, 'Overload Type')
                if v is not None:
                    F.count('scnr overloads')
                    if v >= 12:
                        F.add('error', 'gp.scnr_overload', '%s: Overload Type %d (Medic is MCC-only)' % (_where(tag, n), v))
            elif p == '/Simulation Definition Table':
                f = n.field('Tag')
                if f is not None:
                    F.count('scnr simulation entries')
                    if n.u32(f.offset) == NULL:
                        F.add('error', 'gp.scnr_simulation_null', '%s: null Simulation Definition entry (Vista never has one)' % _where(tag, n))
        elif g == 'hudg':
            if p == '':
                f = n.field('Button Icons')
                if f is not None:
                    F.count('hudg button icons')
                    if n.u32(f.offset) == 0:
                        F.add('warning', 'gp.hudg_button_icons', '%s: Button Icons is empty (Vista: 18 entries)' % _where(tag, n))


# --------------------------------------------------------------------------------------------------
# havok
# --------------------------------------------------------------------------------------------------
def _check_havok(F, tag, root):
    from .handlers import havok as HK
    paths = set(HK.OWNED.get(tag.group, ()))
    rcp_ok = set(HK.RCP_VISTA.values()) | {0}
    for n in root.iter_nodes():
        if n.path in paths:
            for f in n.fields:
                kind = HK._kind(f)
                if kind is None or f.size != 4:
                    continue
                v = n.u32(f.offset)
                F.count('havok runtime fields')
                if F.stock:                    # stock Vista maps carry the tool's heap pointers there; we write 0
                    continue
                if kind == 'rtptr' and v:
                    F.add('error', 'hk.runtime_pointer', '%s: %s = 0x%X (must be 0, the engine rewrites it)' % (_where(tag, n), f.name, v))
                elif kind == 'user' and v >= 0x10000:
                    F.add('error', 'hk.user_data', '%s: User Data 0x%X looks like a tool heap pointer' % (_where(tag, n), v))
                elif kind == 'rcp' and v not in rcp_ok:
                    F.add('warning', 'hk.code_pointer', '%s: Runtime Code Pointer 0x%X is not a stock constant' % (_where(tag, n), v))
        if tag.group in ('phmo', 'coll', 'sbsp'):
            for f in n.fields:
                if f.type == 'dataref' and 'mopp' in f.name.lower():
                    payload = n.datas.get(f.offset) or b''
                    if not payload:
                        continue
                    secs = HK.mopp_sections(payload) if tag.group == 'phmo' else [payload]
                    for s in secs:
                        F.count('havok mopp programs')
                        rep = HK.mopp_validate(s)
                        if not rep.ok:
                            F.add('error', 'hk.mopp_invalid', '%s %s: %s' % (_where(tag, n), f.name, rep.reason))
        if tag.group == 'coll' and n.path == '/Regions/Permutations/BSP Physics':
            f = next((x for x in n.fields if x.type == 'dataref' and 'mopp' in x.name.lower()), None)
            if f is not None and n.datas.get(f.offset):
                F.count('havok mopp programs')
                rep = HK.mopp_validate(n.datas[f.offset])
                if not rep.ok:
                    F.add('error', 'hk.mopp_invalid', '%s %s: %s' % (_where(tag, n), f.name, rep.reason))


# --------------------------------------------------------------------------------------------------
# scripts
# --------------------------------------------------------------------------------------------------
def _check_scripts(F, tag, root):
    from .handlers import scripts as S
    f_expr = root.field('Script Expressions')
    f_pool = root.field('Script String Data')
    if f_expr is None or f_pool is None:
        return
    recs = [S.Rec.unpack(bytes(e.data)) for e in root.blocks.get(f_expr.offset, [])]
    n_scripts = len(root.blocks.get(root.field('Scripts').offset, []))
    n_globals = len(root.blocks.get(root.field('Globals').offset, []))
    roots = [e.u32(0x24) for nm in ('Scripts', 'Globals') for e in root.blocks.get(root.field(nm).offset, [])]
    probs, ex = S.validate_program(recs, roots, root.datas.get(f_pool.offset, b''), S.get_tables().vista, n_scripts=n_scripts)
    F.count('script nodes', len(recs))
    for code, n in sorted(probs.items()):
        F.add('error', 'hs.' + str(code), 'scnr %s: %d x %s %s' % (tag.name, n, code, [e[2] for e in ex if e[0] == code][:1]))
    if len(recs) > 36864 or n_scripts > 1024 or n_globals > 256:
        F.add('error', 'hs.limits', 'scnr %s: %d syntax nodes / %d scripts / %d globals exceed the engine tables (36864 / 1024 / 256)' % (
            tag.name, len(recs), n_scripts, n_globals))


# --------------------------------------------------------------------------------------------------
# shaders
# --------------------------------------------------------------------------------------------------
def _check_shaders(F, c, shared, tag, root, decode):
    from .handlers import shaders as SH
    d = root.u32(SH.OFF_TEMPLATE + 4)
    if d in (NULL, 0):
        # ERROR: the engine's sky / model draw (halo2.exe sub_67FE30) reads Template -> stem+0x58 without a null check; a drawn shader with
        # no stem crashes the game (cyberdyne_h2 `kilimanjaro`, stone_walls `battle_rifle_plastic`, sweet `water_pool`; 0 in the 44 stock maps)
        F.add('error', 'sh.null_template', 'shad %s: the Template (stem) is NONE - the engine crashes when this shader is drawn' % tag.name)
        return
    idx = d & 0xFFFF
    src, st = c, (c.tags[idx] if idx < len(c.tags) else None)
    if st is not None and not st.addr and shared is not None and idx < len(shared.tags):
        src, st = shared, shared.tags[idx]
    if st is None or not st.addr or st.group != 'stem' or st.datum != d:
        F.add('error', 'sh.stem_unresolved', 'shad %s: Template 0x%08X does not resolve to a stem' % (tag.name, d))
        return
    stem_root = decode(src, st)
    probs = SH.check_consistency(root, stem_root)
    F.count('shaders vs stem')
    for p in probs[:2]:
        F.add('error', 'sh.inconsistent', 'shad %s vs stem %s: %s' % (tag.name, st.name, p))


# --------------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
# shared slots that Cartographer content reaches BY INDEX (chronopolis no-spawn, 2026-10-20)
# --------------------------------------------------------------------------------------------------
# Cartographer injects tags from mods\maps\carto_shared.map (the Halloween 'skeleton' biped, flood, event props ...).  An injected tag
# that points at a NON-injected tag points at a host SHARED slot (index >= 10000); a slot the map does not list is empty, the reference
# becomes NONE and e.g. masterchief_skeleton's hlmt loses its jmad (shared 10231): the unit gets no animation manager, object_new fails
# and the player is never created (black screen, name X-ed out).  Every stock MP map lists the closure of the shared matg (25 of 25).
CARTO_SHARED_REL = ('mods', 'maps', 'carto_shared.map')
CARTO_ROOT_GROUPS = ('bipd', 'scen', 'crat', 'eqip', 'weap', 'hlmt', 'vehi', 'mode', 'jmad', 'coll', 'phmo', 'obje', 'ligh', 'item')
# (group, lower-case name) of the carto_shared references that the stock maps (lockout, headlong, ...) leave empty too: not a defect
CARTO_STOCK_MISSES = frozenset({
    ('bitm', 'scenarios\\bitmaps\\solo\\sentinelhq\\shq_gel_fan'),
    ('bitm', 'scenarios\\bitmaps\\detail_maps\\metal\\scratches'),
    ('bitm', 'scenarios\\bitmaps\\reflection_maps\\forerunner_interiors'),
    ('bitm', 'scenarios\\bitmaps\\detail_maps\\metal\\metal_crackle'),           # ascension, beavercreek, burial_mounds, coagulation, elongation, foundation
    ('hlmt', 'scenarios\\objects\\special\\null_up\\null_up'),
    ('mode', 'scenarios\\objects\\special\\null_up\\null_up'),
})
_SPAWN_MEMO = {}


def carto_shared_path(maps_dir=None):
    """mods\\maps\\carto_shared.map of the Cartographer install that owns `maps_dir` (the stock Vista maps folder), or None."""
    d = os.path.dirname(os.path.normpath(maps_dir or _paths.vista_maps_dir()))
    p = os.path.join(d, *CARTO_SHARED_REL)
    return p if os.path.isfile(p) else None


def _ref_scanner():
    from . import planner as PL
    return PL.RefScanner(PL.layouts('vista'))


def shared_matg_closure(shared):
    """Set of shared.map tag indexes: the shared matg and every tag it reaches through tagrefs (the set each stock MP map lists)."""
    key = ('closure', getattr(shared, 'path', id(shared)))
    hit = _SPAWN_MEMO.get(key)
    if hit is not None:
        return hit
    sc = _ref_scanner()
    root = next((t for t in shared.tags if not t.is_empty and t.group == 'matg' and t.addr), None)
    seen, stack = set(), ([root.index] if root is not None else [])
    while stack:
        i = stack.pop()
        if i in seen:
            continue
        seen.add(i)
        t = shared.tags[i]
        if not t.addr or t.group not in sc.lay:
            continue
        for _g, d, _off in sc.scan(shared.read_addr, t.group, t.addr):
            if d in (0, NULL):
                continue
            j = d & 0xFFFF
            if j < len(shared.tags) and shared.tags[j].datum == d and shared.tags[j].addr:
                stack.append(j)
    _SPAWN_MEMO[key] = seen
    return seen


class InjectionMisses(list):
    """[(group, name, host slot index, (referrer group, referrer name))] of carto_shared references to non-injected tags."""
    roots = 0


def carto_injection_misses(carto_path):
    """Every host shared slot that the data-bearing carto_shared tags (and what they inject) refer to without carrying the tag
    themselves; sounds are skipped (they resolve through the gestalt)."""
    key = ('carto', carto_path, os.path.getmtime(carto_path))
    hit = _SPAWN_MEMO.get(key)
    if hit is not None:
        return hit
    sc = _ref_scanner()
    cs = VistaCache(carto_path)
    out = InjectionMisses()
    try:
        by_datum = {t.datum: t for t in cs.tags if not t.is_empty}
        roots = [t for t in cs.tags if not t.is_empty and t.addr and t.name and t.group in CARTO_ROOT_GROUPS]
        out.roots = len(roots)
        done, stack, found = set(), list(roots), {}
        while stack:
            t = stack.pop()
            if t.index in done:
                continue
            done.add(t.index)
            if t.group not in sc.lay:
                continue
            for _g, d, _off in sc.scan(cs.read_addr, t.group, t.addr):
                u = by_datum.get(d)
                if u is None or u.group in ('snd!', 'lsnd'):
                    continue
                if u.addr:
                    stack.append(u)
                elif u.index >= 10000:
                    found.setdefault(u.index, (u.group, u.name or '', u.index, (t.group, t.name or '')))
        out.extend(found[i] for i in sorted(found))
    finally:
        cs.close()
    _SPAWN_MEMO[key] = out
    return out


def _check_shared_closure(F, listed, shared):
    """`listed` = shared indexes the map lists as placeholders.  ERROR when part of the Vista shared matg closure is missing."""
    closure = shared_matg_closure(shared)
    F.count('shared matg closure tags', len(closure))
    missing = sorted(closure - set(listed))
    if not missing:
        return
    # the animation graph of the player bipeds first: it is the slot whose absence stops the player from being created
    missing.sort(key=lambda i: (0 if (shared.tags[i].group == 'jmad' and (shared.tags[i].name or '').lower().endswith('\\masterchief\\masterchief')) else 1, i))
    first = ['%s %s (#%d)' % (shared.tags[i].group, shared.tags[i].name, i) for i in missing[:4]]
    F.add('error', 'spawn.matg_closure',
          '%d of the %d tags in the Vista shared matg closure are not listed as shared placeholders (first: %s); every stock MP map lists '
          'them and Cartographer content (seasonal events, the skeleton biped) reaches them by slot index: the reference becomes NONE and '
          'the player cannot be created' % (len(missing), len(closure), '; '.join(first)))


def _check_carto_injection(F, listed, misses):
    """ERROR when a carto_shared reference lands on a host slot this map leaves empty, except the references stock maps leave empty."""
    listed = set(listed)
    bad = [m for m in misses if m[2] not in listed and (m[0], m[1].lower()) not in CARTO_STOCK_MISSES]
    F.count('carto_shared host slots', len(misses))
    if not bad:
        return
    first = ['%s %s (#%d, from %s %s)' % (g, n, i, via[0], via[1]) for g, n, i, via in bad[:4]]
    F.add('error', 'spawn.carto_inject',
          '%d Cartographer-injected reference(s) land on shared slots this map leaves empty (first: %s)' % (len(bad), '; '.join(first)))


def _check_spawn(F, c, shared, maps_dir=None):
    listed = {t.index for t in c.tags if t.index >= 10000 and not t.is_empty}
    _check_shared_closure(F, listed, shared)
    cp = carto_shared_path(maps_dir)
    if cp is None:
        F.count('carto_shared.map absent (injection gate skipped)')
        return
    _check_carto_injection(F, listed, carto_injection_misses(cp))


GROUP_CHECKS = {
    'matg': 'gameplay', 'mulg': 'gameplay', 'goof': 'gameplay', 'scnr': 'gameplay', 'hudg': 'gameplay', 'jmad': 'gameplay',
    'bipd': 'havok', 'crea': 'havok', 'coll': 'havok', 'phmo': 'havok', 'sbsp': 'havok', 'vehi': 'havok', 'phys': 'havok',
}


def check_map(path, maps_dir=None, checks=('gameplay', 'havok', 'scripts', 'shaders', 'spawn'), stock=False):
    F = Findings(path, stock)
    lay = _layouts()
    c = VistaCache(path)
    shared = None
    try:
        if ('shaders' in checks or 'spawn' in checks) and c.tag_count >= 10000:
            shared = VistaCache(os.path.join(maps_dir or _paths.vista_maps_dir(), 'shared.map'))
        decode = lambda cache, t: T.decode_tag(cache.read_addr, lay[t.group], t.addr, t.group)   # noqa: E731
        if 'gameplay' in checks and c.tag_count >= 10000:
            # (group, name) of every LOCAL tag and the shared placeholders by (group, name): the Simulation Definition twin rule
            F.sim_names = {t.datum: (t.group, t.name.lower()) for t in c.tags if t.index < 10000 and t.datum != NULL and t.name}
            F.sim_placeholders = {}
            for t in c.tags:
                if t.index >= 10000 and t.datum != NULL and t.name:
                    F.sim_placeholders.setdefault((t.group, t.name.lower()), []).append(t.datum)
        t0 = time.time()
        for t in c.tags:
            if not t.addr or t.datum == NULL or t.index >= 10000 and c.tag_count >= 10000 or t.group not in lay:
                continue
            g = t.group
            want = []
            if GROUP_CHECKS.get(g) in checks:
                want.append(GROUP_CHECKS[g])
            if g == 'scnr' and 'scripts' in checks:
                want.append('scripts')
            if g == 'shad' and 'shaders' in checks:
                want.append('shaders')
            if not want:
                continue
            try:
                root = decode(c, t)
            except Exception as ex:                                  # noqa: BLE001
                F.add('warning', 'inv.decode', '%s %s: cannot decode (%r)' % (g, t.name, ex))
                continue
            F.count('tags checked')
            for w in want:
                if w == 'gameplay':
                    _check_gameplay(F, t, root)
                elif w == 'havok':
                    _check_havok(F, t, root)
                elif w == 'scripts':
                    _check_scripts(F, t, root)
                elif w == 'shaders':
                    _check_shaders(F, c, shared, t, root, decode)
        if 'spawn' in checks and shared is not None and c.header.type == 1:
            _check_spawn(F, c, shared, maps_dir)
        F.counts['seconds'] = round(time.time() - t0, 1)
    finally:
        c.close()
        if shared is not None:
            shared.close()
    return F


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = _paths.consume_common_options(argv)          # --assembly-dir / --vista-maps / --mcc-maps DIR (also H2CONV_ASSEMBLY, H2CONV_VISTA_MAPS)
    if not argv or argv[0] in ('-h', '--help'):
        print('usage: py -3 -m h2conv.invariants [--stock] [--assembly-dir DIR] [--vista-maps DIR] <map.map> [...]   (--stock: maps written by the Vista tool)')
        return 0 if argv else 2
    bad = 0
    stock = '--stock' in argv
    for p in [a for a in argv if not a.startswith('--')]:
        try:
            F = check_map(p, stock=stock)
        except _paths.PathsError as ex:
            print('ERROR: %s' % ex)
            return 2
        print('%s: %d error(s), %d warning(s)  %s' % (os.path.basename(p), F.errors, F.warnings, {k: v for k, v in F.counts.items() if not k.startswith('finding:')}))
        for lv, code, msg in F.items:
            print('   %-7s %-24s %s' % (lv, code, msg))
        bad += F.errors
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
