# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Conversion planner: which tags of the MCC input become local Vista tags, which are LINKED to the Vista shared.map,
which are copied from a Vista DONOR map, and which are DROPPED.

    plan = build_plan(src, mcc_shared, vista_refs, opts)            # -> Plan

Everything here is derived from the stock Vista MP maps:

* local tags are indices 0..N-1 (scnr #0, the map's own ugh! LAST); slots up to 9999 are empty; indices 10000..15064 are the
  shared.map placeholders (group+datum of the shared.map tag, addr/size 0); tag_count is 15065 in all 25 MP maps.
* local datum = (salt << 16) | index with salt = 0x8000 | ((0x6178 + k) % 0x7FFF): 0xE178 for the first tag of every MP map and
  an ascending counter wrapping into 0x8000 in the big SP maps (the stock tool leaves gaps for the shared tags it loaded; any
  salt works because the engine compares the full datum).
* the placeholder set is the CLOSURE (over shared.map's own tagrefs) of the shared tags the local tags reference plus the
  globals tag - for 25/25 stock MP maps closure is a subset of the real set, and the real set adds only 14-21 tags that are
  referenced from authoring data the cache strips (11 of them in all 25 maps: COMMON_SHARED_EXTRAS).
* local tag order follows the cache builder's depth-first walk from scnr; the MCC cache builder produced the SAME relative order
  (457 common tags of headlong: 1 swap), so the planner keeps the SOURCE order and inserts tags it adds after their referrer.

Actions per PlanEntry:  convert  the tag is converted from MCC data (src_map 'src', or 'shared' = pulled from the MCC shared.map)
                        link     no local copy; the output refers to the Vista shared.map tag with the same (group, name)
                        donor    the whole tag is copied from a Vista map (vrtx/spas/stem: MCC only has DXBC code)
                        drop     no output tag (MCC-only sound_remastered\\* tags, superseded / orphan tags); `redirect` says where
                                 references to it should go instead (the legacy twin), None = null reference
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass

import collections
import hashlib
import os
import struct
import sys
import time

from . import paths as _paths
from . import plugins as P
from . import tagtree as T
from .vista_cache import VistaCache, VistaTag, FIRST_SHARED_TAG_INSTANCE_INDEX

NULL = 0xFFFFFFFF
MP_TAG_COUNT = 15065                 # tag_count of every stock MP map (and shared.map)
LOCAL_LIMIT = FIRST_SHARED_TAG_INSTANCE_INDEX          # local indices must be < 10000
SHADER_DONOR_GROUPS = ('vrtx', 'spas', 'stem')
REMASTERED_PREFIX = 'sound_remastered\\'
LEGACY_PREFIX = 'sound\\'
SALT_BASE = 0x6178                   # first local salt 0xE178 (all 25 stock MP maps)
SRC, SHARED = 'src', 'shared'        # PlanEntry.src_map values used by the planner ('single_player_shared'/'mainmenu' reserved)

# Shared tags that ALL 25 stock MP maps list as placeholders although nothing in their cache data references them
# (referenced from stripped authoring data).  Name -> looked up in shared.map; absent names are ignored.
COMMON_SHARED_EXTRAS = (
    ('bitm', 'effects\\bitmaps\\energy\\light_ring_expand'),
    ('prt3', 'effects\\generic\\solids\\blood_generic_impact_burst'),
    ('pmov', 'effects\\particle_physics\\lingering_smoke'),
    ('bitm', 'incompetent\\bitmaps\\jackal_shield_noise'),
    ('bitm', 'incompetent\\bitmaps\\jackal_shield_mask'),
    ('prt3', 'effects\\objects\\vehicles\\ghost\\ghost_plasma_vent'),
    ('pmov', 'effects\\objects\\vehicles\\ghost\\ghost_plasma_vent'),
    ('prt3', 'effects\\generic\\smoke\\smoke_puff'),
    ('deca', 'effects\\decals\\ripples\\water_ripples'),
    ('bitm', 'effects\\decals\\ripples\\bitmaps\\water_ripples'),
    ('bitm', 'objects\\bitmaps\\detail_maps\\covenant_detail'),
)

# drop reasons whose references become null on purpose (Plan.audit): remastered-only sounds without a legacy twin, MCC 'empty' tags
# (size 0: nothing to convert), MCC-only shader code with no Vista twin, MCC-only settings
BY_DESIGN_NULL = ('sound_remastered', 'empty in MCC', 'NO_DONOR', 'MCC-only:')

# Tags whose "modified" status must NOT taint their dependents.  H2EK kit tags are re-exported at
# a higher resolution than what MCC shipped; `rasterizer\linear_corner_fade` is the same 0..255 ramp, 16x16 in the shipped MCC
# shared.map and in Vista (A8Y8) but 256x256 in the H2EK tag set the custom maps are built from, so it is always "modified" and, through
# the taint rule, forced 306 of 1,190 converted tags of the custom headlong (shaders, effects ... that merely REFERENCE the bitmap) to be
# copied locally instead of linked, creating 306 local tags that shadow shared tags of the same name (stock Vista maps: none).
# The bitmap itself is still converted when a kept local tag references it; only the cascade is cut.  Pass opts['taint_ignore'] =
# [(group, name), ...] to choose another set, () to restore the old behaviour (CLI: --no-taint-ignore).
DEFAULT_TAINT_IGNORE = (('bitm', 'rasterizer\\linear_corner_fade'),)
# Tags whose copy in an H2EK-built map is the editing kit's DEFAULT placeholder, not the shipped tag: `rasterizer\invalid` is the
# kit's 512x512 red/green "INVALID TEXTURE" checkerboard (1 bitmap, 0 sequences) in every one of the 24 H2EK maps, while the shipped MCC
# shared.map and the Vista shared.map carry the 14-sequence / 13-bitmap atlas (same name; the Vista one is what the stock Vista
# hudg 'Icon Bitmap' / 'Damage Indicator Bitmap' and matg 'Interface Goo Map 2' point to).  Compared by content it is "modified", so a
# converted map with local globals (--matg hud / auto / local) copied the kit checkerboard as a LOCAL tag that shadows the shared atlas
# and pointed hudg / matg at it.  Vista-kit maps (native Vista chronopolis, lockout, ...) never carry it.  These tags are therefore
# forced to 'equal' (= linked to Vista's shared.map tag) in the standalone prepass.  opts['kit_placeholder_link'] = False restores the
# old behaviour.
KIT_PLACEHOLDER_LINK = (('bitm', 'rasterizer\\invalid'),)
# Custom-shared support (workshop mods with their own shared.map): groups that are never compared with the stock MCC shared.map.  matg is handled
# on its own (Vista globals clone / converted local globals, _matg_mode), scnr is the map's own, ugh! is rebuilt by the sound handler.
# mulg is NOT special any more (2026-10-20 uncuthud): the mod's multiplayer_globals Runtime Weapons / Vehicles lists are the weapons the game
# variants select (Uncut/DW3 points them at its own dw3\...\magnum_dw3 etc. whose HUDs sit at the Weapon HUD anchor = top-left; Vista's
# shared mulg holds the stock weapons whose HUDs sit at anchor 0 = top-right, on top of the Uncut shield bar), so a MODIFIED mulg is converted
# locally (handlers/gameplay.mulg_runtime still cuts the lists to the Vista lengths); an unmodified one still links to Vista's.
CUSTOM_SHARED_SPECIAL = ('matg', 'scnr', 'ugh!')
# groups that are compared with the stock MCC tag but never become local merely because a tag they reference is modified (taint): the mulg
# of a mod that edits e.g. one weapon sound must keep linking to Vista's mulg (the pre-uncuthud behaviour of every such map)
CUSTOM_SHARED_NO_TAINT = ('mulg',)
# matg 'Language' descriptors carry the offsets of the shared.map's own string/locale tables (String Reference/Data Cache Offset): they differ between any two
# shared.map builds with different string counts (3252880894: this is the ONLY matg difference) and say nothing about the globals' content
CUSTOM_SHARED_RULES = {'matg': {'skip_names': ('String Reference Cache Offset', 'String Data Cache Offset')}}
# Baseline noise of a NEWER MCC build than the installed one: every workshop shared.map built on it differs from the installed stock one in exactly
# these fields (Rampancy 3164362918, which edits nothing, shows precisely these 4 tags) and nowhere else.
# A tag listed here is 'equal' when every difference found starts with one of the listed field paths; any other difference keeps it 'modified'.
CUSTOM_SHARED_NOISE = {
    ('vehi', 'objects\\vehicles\\warthog\\warthog'): ('/Phantom Shapes/Shape Type ', '/Phantom Shapes/Shape Index '),
    ('vehi', 'objects\\vehicles\\scorpion\\scorpion'): ('/Phantom Shapes/Shape Type ', '/Phantom Shapes/Shape Index '),
    ('vehi', 'objects\\vehicles\\mongoose\\mongoose'): ('/Phantom Shapes/Shape Type ', '/Phantom Shapes/Shape Index '),
    ('coll', 'scenarios\\objects\\solo\\alphagasgiant\\lights\\light_ground_up\\light_ground_up'): ('/Regions/Permutations/BSP Physics/Unknown',),
}
# shared shaders whose Vista copy differs from the stock MCC one (the forward shader checker over chronopolis / lockout / headlong,
# 2026-10-20): reported when the output lists them as placeholders.  Cartographer (tag_fixes.cpp) reverts fp_arms / masterchief_visor /
# smg_painted_metal at load; the others stay as Vista shipped them.
VISTA_STOCK_DIFFERS_FROM_MCC = (
    ('shad', 'objects\\characters\\elite\\shaders\\elite_honor_inset_lights'),
    ('shad', 'objects\\characters\\elite\\shaders\\inset_lights'),
    ('shad', 'objects\\characters\\masterchief\\fp\\shaders\\fp_arms'),
    ('shad', 'objects\\characters\\masterchief\\shaders\\masterchief_visor'),
    ('shad', 'objects\\gear\\human\\military\\shaders\\fusion_coil_bloom'),
    ('shad', 'objects\\powerups\\shotgun_ammo\\shaders\\sg_ammo_box'),
    ('shad', 'objects\\powerups\\sniper_rifle_ammo\\shaders\\sr_ammo_metal'),
    ('shad', 'objects\\vehicles\\warthog\\shaders\\metal'),
    ('shad', 'objects\\vehicles\\warthog\\turrets\\chaingun\\shaders\\chaingun'),
    ('shad', 'objects\\weapons\\rifle\\covenant_carbine\\shaders\\carbine_display'),
    ('shad', 'objects\\weapons\\rifle\\covenant_carbine\\shaders\\carbine_switch'),
    ('shad', 'objects\\weapons\\rifle\\smg\\shaders\\smg_painted_metal'),
)

OWN_GESTALT_NAME = "i've got custom multiplayer coconuts, and i don't know why!"


class PlannerError(Exception):
    pass


def local_datum(index):
    """Datum of the k-th local tag (see module doc)."""
    return ((0x8000 | ((SALT_BASE + index) % 0x7FFF)) << 16) | (index & 0xFFFF)


def _nk(group, name):
    return (group, (name or '').lower())


_LAY = {}


def layouts(game):
    if game not in _LAY:
        _LAY[game] = P.load_layouts(game, corrected=True)
    return _LAY[game]


def _find_stock_mcc_shared():
    """The decompressed stock MCC shared.map (<work folder>/mcc_decompressed, or inflated from the MCC install); None if unavailable.
    Custom-shared support: a candidate counts as THE stock shared.map only when its creation time is a stock one (mapinfo.stock_mcc_times) -
    a workshop mod's shared.map that ended up under the stock name in the work folder must never be taken for it.  When no candidate has the
    stock time the first existing file is returned as before (unknown stock build)."""
    try:
        from . import paths as _paths
        from . import mapinfo as _MI
        from .mcc_cache import MccCache, DECOMP_DIR, file_creation_time
        stock_dir = _paths.stock_mcc_maps_dir()
        want = _MI.stock_mcc_times(stock_dir).get('shared')
        first = None
        for d in (DECOMP_DIR, stock_dir, _paths.mcc_maps_dir(required=False)):
            if not d:
                continue
            p = os.path.join(d, 'shared.map')
            if os.path.isfile(p):
                if first is None:
                    first = p
                if file_creation_time(p) == want:
                    return MccCache(p)
        if first is not None:
            return MccCache(first)
    except Exception:
        pass
    return None


# ======================================================================================================
# tagref scanner
# ======================================================================================================
class _Sched(object):
    __slots__ = ('items', 'size')


class RefScanner(object):
    """Fast extraction of every tagref (8-byte {group, datum} and 4-byte datum-only) of a tag, in field order (pre-order:
    a tagblock's elements are visited at the position of the block field).  Only blocks that can contain refs are read."""

    def __init__(self, lay):
        self.lay = lay
        self._memo = {}
        self._top = {}

    def _compile(self, fields, size):
        key = id(fields)
        s = self._memo.get(key)
        if s is not None:
            return s
        s = _Sched()
        s.size = size
        s.items = []
        self._memo[key] = s
        for f in sorted(fields, key=lambda x: x.offset):
            if f.type == 'tagref':
                s.items.append(('r', f.offset, f.size))
            elif f.type in T.BLOCK_TYPES:
                es = T.element_size(f)
                child = self._compile(f.children, es)
                if child.items:
                    s.items.append(('b', f.offset, es, child))
        return s

    def sched(self, group):
        s = self._top.get(group)
        if s is None:
            lay = self.lay.get(group)
            if lay is None:
                return None
            s = self._compile(lay.fields, lay.base_size or lay.computed_size)
            self._top[group] = s
        return s

    def scan(self, read, group, addr):
        """-> [(group_int|None, datum, field_offset)] ; read(addr, size) -> bytes."""
        s = self.sched(group)
        if s is None or not s.items:
            return []
        out = []
        self._scan(read, s, read(addr, s.size), out)
        return out

    def _scan(self, read, sched, data, out):
        for it in sched.items:
            if it[0] == 'r':
                off = it[1]
                if it[2] == 8:
                    g, d = struct.unpack_from('<II', data, off)
                else:
                    g = None
                    d = struct.unpack_from('<I', data, off)[0]
                out.append((g, d, off))
            else:
                off, es, child = it[1], it[2], it[3]
                cnt, ptr = struct.unpack_from('<iI', data, off)
                if cnt > 0 and ptr:
                    blob = read(ptr, cnt * es)
                    for i in range(cnt):
                        self._scan(read, child, blob[i * es:(i + 1) * es], out)


# ======================================================================================================
# Vista donor index
# ======================================================================================================
class DonorIndex(object):
    """(group, name) -> [(map file name, tag index)] over every Vista map in a folder (tags that carry DATA in that map).
    Search order: shared.map, mainmenu.map, the multiplayer maps (alphabetical), single_player_shared.map, then the campaign maps.
    closure(map, tag) follows Vista-layout tagrefs; a ref to a placeholder (index >= 10000, addr 0) of an MP map is resolved to
    the shared.map tag with the same index."""

    FIRST = ('shared.map', 'mainmenu.map')

    def __init__(self, maps_dir, opener=None):
        self.maps_dir = maps_dir
        _paths.warn_extra_vista_maps(maps_dir)               # every .map of the folder is indexed as a donor
        self._opener = opener
        self._caches = {}
        self._index = None
        self._files = None
        self.scanner = RefScanner(layouts('vista'))
        self._refs = {}

    # ---- maps ------------------------------------------------------------------------------------
    def cache(self, fname):
        c = self._caches.get(fname)
        if c is None:
            c = self._opener(fname) if self._opener else VistaCache(os.path.join(self.maps_dir, fname))
            self._caches[fname] = c
        return c

    def files(self):
        if self._files is None:
            names = sorted(f for f in os.listdir(self.maps_dir) if f.lower().endswith('.map'))
            first = [f for f in self.FIRST if f in names]
            rest = [f for f in names if f not in first]
            typ = {}
            for f in rest:
                typ[f] = self.cache(f).header.type
            mp = [f for f in rest if typ[f] == 1]
            spsh = [f for f in rest if f == 'single_player_shared.map']
            sp = [f for f in rest if typ[f] != 1 and f != 'single_player_shared.map']
            self._files = first + mp + spsh + sp
        return self._files

    def _build(self):
        idx = collections.OrderedDict()
        for f in self.files():
            c = self.cache(f)
            for t in c.tags:
                if t.is_empty or not t.addr or not t.name:
                    continue
                idx.setdefault((t.group, t.name.lower()), []).append((f, t.index))
        self._index = idx

    # ---- queries ---------------------------------------------------------------------------------
    def find(self, group, name):
        if self._index is None:
            self._build()
        return list(self._index.get((group, (name or '').lower()), ()))

    def tag(self, fname, index):
        return self.cache(fname).tags[index]

    def target(self, fname, datum):
        """Resolve a datum found in a tag of map `fname` to the (map file, index) that owns the data; None if unresolvable."""
        if datum in (0, NULL):
            return None
        c = self.cache(fname)
        i = datum & 0xFFFF
        if i >= len(c.tags) or c.tags[i].datum != datum:
            return None
        t = c.tags[i]
        if t.addr == 0 and i >= FIRST_SHARED_TAG_INSTANCE_INDEX:
            sh = self.cache('shared.map')
            if i < len(sh.tags) and sh.tags[i].datum == datum and sh.tags[i].addr:
                return ('shared.map', i)
            return None
        if not t.addr:
            return None
        return (fname, i)

    def refs(self, fname, index):
        """[(map file, index, field_offset)] of the Vista tag, in field order (targets resolved, nulls skipped)."""
        key = (fname, index)
        r = self._refs.get(key)
        if r is None:
            c = self.cache(fname)
            t = c.tags[index]
            r = []
            for g, d, off in self.scanner.scan(c.read_addr, t.group, t.addr):
                tg = self.target(fname, d)
                if tg is not None:
                    r.append((tg[0], tg[1], off))
            self._refs[key] = r
        return r

    def closure(self, fname, index):
        """Pre-order list of (map file, index) reachable from the tag (including itself)."""
        seen, order, stack = set(), [], [(fname, index)]
        while stack:
            k = stack.pop()
            if k in seen:
                continue
            seen.add(k)
            order.append(k)
            for f2, i2, _ in reversed(self.refs(*k)):
                stack.append((f2, i2))
        return order


# ======================================================================================================
# Plan objects
# ======================================================================================================
class PlanEntry(object):
    """One decision.  src_map/src_index identify the MCC tag ('src' = the input map, 'shared' = the MCC shared.map, None =
    a tag that only exists on the Vista side, e.g. a donor-closure addition).  out_index/out_datum: local index/datum for
    convert/donor; the Vista shared.map index/datum for link; None/0xFFFFFFFF for drop."""
    __slots__ = ('src_map', 'src_index', 'group', 'name', 'action', 'out_index', 'out_datum', 'donor', 'redirect',
                 'reason', 'twin', 'src_size', 'parent', 'redirect_key', 'shared_refs', 'no_closure')

    def __init__(self, src_map, src_index, group, name, action, reason='', donor=None, redirect=None):
        self.src_map = src_map
        self.src_index = src_index
        self.group = group
        self.name = name
        self.action = action
        self.out_index = None
        self.out_datum = NULL
        self.donor = donor            # (vista map file name, tag index) for action 'donor'
        self.redirect = redirect      # PlanEntry for dropped tags (None = null)
        self.reason = reason
        self.twin = None              # (vista map file, index) of a Vista tag with this name when the entry is converted anyway
        self.src_size = 0
        self.parent = None
        self.redirect_key = None
        self.no_closure = False       # donor whose references are NOT followed (targets absent from the output become null)
        self.shared_refs = False      # donor clone of a Vista SHARED tag (training-off globals): every reference stays on the shared slot

    def __repr__(self):
        return '<PlanEntry %s %s %r %s out=%s>' % (self.action, self.group, self.name, (self.src_map, self.src_index), self.out_index)


class Plan(object):
    def __init__(self):
        self.entries = []              # local output tags (convert + donor), output index order
        self.all_entries = []          # every decision, creation order
        self.by_src = {}               # (src_map, mcc_index) -> PlanEntry
        self.by_name = {}              # (group, name.lower()) -> PlanEntry
        self.shared_placeholders = []  # [VistaTag] (indices 10000.., copied from Vista shared.map)
        self.tag_count = 0
        self.map_type = 1
        self.opts = {}
        self.stats = collections.Counter()
        self.warnings = []
        self.src = None
        self.mcc_shared_cache = None
        self.own_gestalt = None        # PlanEntry of the map's own ugh!
        self.scenario = None           # PlanEntry of scnr
        self.globals_entry = None      # PlanEntry of matg (usually a link)
        self.self_contained = False
        self.src_kind = 'stock'
        self.elapsed = 0.0
        self.status = {}               # (src_map, index) -> ('equal'|'modified'|'unverifiable', why)  (standalone inputs)
        self.tainted = {}              # (src_map, index) -> name of the modified dependency
        self.notes = {}                # free-form findings, e.g. 'matg': ('modified', first difference)
        self._builder = None

    # ---- sources -----------------------------------------------------------------------------------------
    def source_of(self, entry):
        """(MccCache, MccTag) the entry is converted from ('src' = input map, 'shared' = MCC shared.map); None if it has none."""
        if entry.src_map is None:
            return None
        cache = self.src if entry.src_map == SRC else self.mcc_shared_cache
        return cache, cache.tags[entry.src_index]

    def donor_source_of(self, entry):
        """(VistaCache, VistaTag) of a donor entry (or of a convert entry's Vista twin); None if it has none."""
        d = entry.donor or entry.twin
        if d is None:
            return None
        c = self._builder.donors.cache(d[0])
        return c, c.tags[d[1]]

    # ---- datum remap -----------------------------------------------------------------------------
    def _key_of_datum(self, src_map, datum):
        if src_map == SRC:
            t = self.src.tag_by_datum(datum)
            if t is None:
                return None
            return (SHARED, t.index) if t.external else (SRC, t.index)
        if src_map == SHARED and self.mcc_shared_cache is not None:
            t = self.mcc_shared_cache.tag_by_datum(datum)
            return None if t is None else (SHARED, t.index)
        return None

    @staticmethod
    def _out(entry):
        seen = 0
        while entry is not None and seen < 8:
            if entry.action in ('convert', 'donor', 'link'):
                return entry.out_datum
            entry = entry.redirect
            seen += 1
        return NULL

    def entry_of_datum(self, src_map, mcc_datum):
        """The PlanEntry an MCC datum (of the input map or of the MCC shared.map) belongs to, or None."""
        if mcc_datum in (NULL, 0, None):
            return None
        k = self._key_of_datum(src_map, mcc_datum)
        return None if k is None else self.by_src.get(k)

    def remap_datum(self, src_map, mcc_datum):
        """Output datum for an MCC datum (src_map: 'src' = datum of the input map, 'shared' = datum of the MCC shared.map);
        0xFFFFFFFF for null, unknown or dropped-without-redirect references."""
        e = self.entry_of_datum(src_map, mcc_datum)
        return self._out(e) if e is not None else NULL

    def remap_name(self, group, name):
        """Output datum of the plan entry with this (group, name), NULL if the plan has none (used for donor tags)."""
        e = self.by_name.get((group, (name or '').lower()))
        return self._out(e) if e is not None else NULL

    def shared_clone_datum(self, group, name):
        """Local datum of the Vista-shared CLONE (PlanEntry.shared_refs: the training-off matg / hudg) named (group, name), else None."""
        e = self.by_name.get((group, (name or '').lower()))
        if e is not None and e.shared_refs and e.action == 'donor' and e.out_datum != NULL:
            return e.out_datum
        return None

    def shared_listed(self, index):
        """True when shared.map slot `index` is listed as a shared placeholder of the output."""
        s = getattr(self, '_listed_idx', None)
        if s is None or len(s) != len(self.shared_placeholders):
            s = self._listed_idx = {p.index for p in self.shared_placeholders}
        return index in s

    def remap_vista_datum(self, vista_cache, datum):
        """Output datum for a datum read from a tag of a Vista donor map (resolved by (group, name))."""
        if datum in (0, NULL):
            return NULL
        i = datum & 0xFFFF
        if i >= len(vista_cache.tags) or vista_cache.tags[i].datum != datum:
            return NULL
        t = vista_cache.tags[i]
        if t.is_empty or not t.name:
            return NULL
        return self.remap_name(t.group, t.name)

    # ---- tables for the writer ----------------------------------------------------------------------
    @property
    def scenario_datum(self):
        return self.scenario.out_datum if self.scenario else NULL

    @property
    def globals_datum(self):
        return self.globals_entry.out_datum if self.globals_entry else NULL

    @property
    def secondary_ugh_datum(self):
        """Value of header 0x2E4.  Multiplayer output: the shared Vista ugh! stays the primary gestalt and the map's own ugh! is
        the secondary one named here.  Solo / main-menu output: ONE local ugh! referenced by the local matg and header 0x2E4 = -1
        (all 15 stock solo maps; with the datum the engine would merge the gestalt with itself)."""
        if self.map_type != 1:
            return NULL
        return self.own_gestalt.out_datum if self.own_gestalt else NULL

    def instance_table(self):
        """Full instance table (tag_count VistaTag): local entries carry group+datum with addr/size 0 (the writer fills
        them), placeholders copy the shared.map tag, everything else is the empty slot {FFFFFFFF, FFFFFFFF, 0, 0}."""
        tab = [VistaTag(i, '', NULL, 0, 0) for i in range(self.tag_count)]
        for e in self.entries:
            tab[e.out_index] = VistaTag(e.out_index, e.group, e.out_datum, 0, 0, e.name)
        for p in self.shared_placeholders:
            tab[p.index] = VistaTag(p.index, p.group, p.datum, 0, 0, p.name)
        return tab

    def tag_names(self):
        names = [None] * self.tag_count
        for e in self.entries:
            names[e.out_index] = e.name
        for p in self.shared_placeholders:
            names[p.index] = p.name
        return names

    # ---- validation ------------------------------------------------------------------------------------
    def audit(self):
        """Check that every reference of every kept tag resolves.  Returns (reference count, [problem tuples]) with
        problem = (kind, from group, from name, target group, target name, why) for references that would become NULL:
        'no-entry' (target without plan entry), 'null' (dropped target without redirect) and 'null-by-design' (a
        sound_remastered tag without legacy twin: the Vista field has no such sound; an MCC 'empty' tag; MCC-only shader code
        or settings - BY_DESIGN_NULL)."""
        b = self._builder
        n = 0
        bad = []
        for e in self.entries:
            if e.action == 'convert' and e.src_map is not None:
                for tk, off in b.mcc_refs((e.src_map, e.src_index)):
                    n += 1
                    te = self.by_src.get(tk)
                    tag = b.tag_of(tk)
                    if te is None:
                        bad.append(('no-entry', e.group, e.name, tag.group, tag.name, ''))
                    elif self._out(te) == NULL:
                        by_design = te.action == 'drop' and any(k in te.reason for k in BY_DESIGN_NULL)
                        bad.append(('null-by-design' if by_design else 'null', e.group, e.name, te.group, te.name, te.reason))
            elif e.action == 'donor':
                for f2, i2, off in b.donors.refs(*e.donor):
                    n += 1
                    t2 = b.donors.tag(f2, i2)
                    if e.shared_refs:             # a clone of a shared tag: its targets are the shared slots (or the clone hudg)
                        if self.shared_clone_datum(t2.group, t2.name) is None and not self.shared_listed(i2):
                            bad.append(('no-entry', e.group, e.name, t2.group, t2.name, 'shared slot %d not listed' % i2))
                        continue
                    if self.remap_name(t2.group, t2.name) == NULL:
                        bad.append(('no-entry', e.group, e.name, t2.group, t2.name, 'donor ref'))
        return n, bad

    def estimate_output(self, count_sounds=True):
        """Rough size of the converted map in bytes: {'tags', 'bitmaps', 'geometry', 'sounds', 'total', ...}.  Tag data uses the
        MCC tag sizes (donor tags: the Vista size), bitmaps the MCC blob sizes (Vista recompresses to ONE zlib stream),
        geometry the blkh block sizes, sounds the ADPCM chunk bytes (Opus: decoded PCM re-encoded as XADPCM at 44.1 kHz)."""
        return _estimate_output(self, count_sounds)

    # ---- reporting ------------------------------------------------------------------------------------
    def report(self, max_items=200):
        """JSON-able conversion report: counts, decisions by reason, content losses worth knowing about."""
        reasons = collections.Counter()
        for e in self.all_entries:
            reasons['%s | %s' % (e.action, e.reason.split(':')[0].split(' [')[0][:90])] += 1
        by_key = {k: e for k, e in self.by_src.items()}
        modified, lost = [], []
        for k, (st, why) in self.status.items():
            if st != 'modified':
                continue
            e = by_key.get(k)
            row = {'group': e.group, 'name': e.name, 'why': why} if e else {'why': why}
            (modified if e is not None and e.action in ('convert', 'donor', 'link') else lost).append(row)
        nulls = [e for e in self.all_entries if e.action == 'drop' and e.redirect is None and 'sound_remastered' in e.reason]
        unverified, differs = self._unverified_links(), self._vista_stock_differs()
        return {
            'unverified_links': unverified[:max_items], 'unverified_links_count': len(unverified),
            'mcc_differs_from_vista_stock': differs[:max_items], 'mcc_differs_from_vista_stock_count': len(differs),
            'summary': self.summary(), 'stats': dict(self.stats), 'by_action': {a: dict(c) for a, c in self.counts().items()},
            'reasons': dict(reasons.most_common()),
            'modified_kept': modified[:max_items], 'modified_kept_count': len(modified),
            'modified_but_dropped': lost[:max_items], 'modified_but_dropped_count': len(lost),
            'tainted_count': len(self.tainted),
            'custom_shared': (dict(self.notes['custom_shared'],
                                   candidates=self.stats.get('cs_candidates', 0), candidates_input_local=self.stats.get('cs_candidates_src', 0),
                                   modified=self.stats.get('cs_modified', 0), modified_input_local=self.stats.get('cs_modified_src', 0),
                                   tainted=self.stats.get('cs_tainted', 0), noise_ignored=self.stats.get('cs_noise_ignored', 0),
                                   compare_seconds=self.stats.get('cs_seconds', 0), modified_tags=list(self.notes.get('custom_shared_modified', []))[:max_items],
                                   globals=self.notes.get('matg', ('?',))[0], hud=self.notes.get('hud', ('?',))[0])
                              if 'custom_shared' in self.notes else None),
            'taint_ignored': list(self.notes.get('taint_ignored', [])),
            'taint_ignored_dependents_linked': self.stats.get('taint_ignored_dependents_linked', 0),
            'matg': {'status': self.notes['matg'][0], 'differences': self.notes['matg'][1]} if 'matg' in self.notes else None,
            'remastered_without_legacy_twin': [e.name for e in nulls][:max_items], 'remastered_without_legacy_twin_count': len(nulls),
            'no_donor': [e.name for e in self.all_entries if 'NO_DONOR' in e.reason][:max_items],
            'empty_mcc_tags': [{'group': e.group, 'name': e.name, 'action': e.action, 'reason': e.reason[:120]}
                               for e in self.all_entries if 'empty in MCC' in e.reason][:max_items],
            'empty_mcc_tags_count': sum(1 for e in self.all_entries if 'empty in MCC' in e.reason),
            'pulled_from_mcc_shared': sorted(e.name for e in self.entries if e.src_map == SHARED)[:max_items],
            'pulled_from_mcc_shared_count': sum(1 for e in self.entries if e.src_map == SHARED),
        }

    def _unverified_links(self):
        """Shared placeholders of the output whose (group, name) is a tag of the INPUT map with no tag of that name in the stock MCC
        shared maps: the Vista twin was linked by name and could not be compared with anything (report only)."""
        b = self._builder
        if b is None or b.msh is None:
            return []
        out = []
        for t in self.shared_placeholders:
            nk = _nk(t.group, t.name)
            k = b.src_name.get(nk)
            if k is not None and k[0] == SRC and nk not in b.msh_name:
                out.append({'group': t.group, 'name': t.name, 'shared_index': t.index})
        return out

    def _vista_stock_differs(self):
        """Shared placeholders that are known to differ between the stock MCC shared.map and the Vista shared.map (shader constants,
        animation functions, template, bitmaps: the forward shader checker, 2026-10-20).  Vista's own copy is used (report only)."""
        have = {_nk(t.group, t.name) for t in self.shared_placeholders}
        return [{'group': g, 'name': n} for g, n in VISTA_STOCK_DIFFERS_FROM_MCC if _nk(g, n) in have]

    def counts(self):
        """{action: Counter(group)}"""
        out = collections.defaultdict(collections.Counter)
        for e in self.all_entries:
            out[e.action][e.group] += 1
        return out

    def summary(self):
        c = collections.Counter(e.action for e in self.all_entries)
        return {'local': len(self.entries), 'convert': c['convert'], 'donor': c['donor'], 'link': c['link'], 'drop': c['drop'],
                'placeholders': len(self.shared_placeholders), 'tag_count': self.tag_count, 'src_kind': self.src_kind,
                'self_contained': self.self_contained, 'map_type': self.map_type, 'elapsed_s': round(self.elapsed, 1)}


# ======================================================================================================
# content comparison (Phase A: "is this tag unmodified relative to the stock MCC shared.map?")
# ======================================================================================================
_SCALAR_SKIP_PREFIX = ('Runtime',)

# per-group comparison rules.  skip_fields: (block path, field name); skip_blocks: block path ('/A/B').
# Everything not listed is compared byte-exactly (after the generic normalisation: pointer words ignored, tagrefs by target
# (group, name), string ids by text, 'Runtime*'/'User Data' scalars ignored).
GROUP_RULES = {
    'shad': {
        'skip_blocks': ('/Postprocess Definition/Levels Of Detail', '/Postprocess Definition/Layers',
                        '/Postprocess Definition/Passes', '/Postprocess Definition/Implementations'),
        'skip_fields': (('/Postprocess Definition/Bitmaps', 'Log Bitmap Dimension'),),
    },
    'bitm': {
        # offsets/sizes address the pixel blob (compared through the decoded-pixel hash); the colour-plate size/data words
        # (root +0x18..+0x28) are authoring metadata of the import step and LOD Adjust is a streaming hint - none changes what
        # is drawn (a false 'modified' on linear_corner_fade alone tainted 293 dependent tags).
        'skip_fields': (('/Bitmaps', 'LOD1 Offset'), ('/Bitmaps', 'LOD2 Offset'), ('/Bitmaps', 'LOD3 Offset'),
                        ('/Bitmaps', 'LOD1 Size'), ('/Bitmaps', 'LOD2 Size'), ('/Bitmaps', 'LOD3 Size'),
                        ('', 'Color Plate Width'), ('', 'Color Plate Height'), ('', 'Unknown'), ('/Bitmaps', 'LOD Adjust')),
    },
    'unic': {
        'skip_fields': (('', 'Strings'),),
        'skip_blocks': ('/Import String References',),
        'skip_datas': ('Import String Data',),
    },
    'snd!': {
        'skip_names': ('[ugh!] ',),                 # every gestalt-relative index of the snd! struct
    },
    'mode': {'skip_names': ('Model Checksum',)},
}
_SKIP_ANY_NAME = ('Resource Block Offset',)         # geometry_block_info offset words (compared through the blob hash)

# geometry_block_info sites: (group, ((block offset, element size), ...), info offset in innermost element)
_GEOM_SITES = (
    ('mode', ((0x24, 0x5C),), 0x38), ('mode', ((0x74, 0x58),), 0x34),
    ('sbsp', ((0x9C, 0xB0),), 0x28), ('sbsp', ((0x138, 0xC8),), 0x28), ('sbsp', ((0x214, 0xAC),), 0x10),
    ('sbsp', ((0x234, 0x30), (0x10, 0x34)), 0x00),
    ('ltmp', ((0x80, 0x68), (0x20, 0x54)), 0x28), ('ltmp', ((0x80, 0x68), (0x30, 0x54)), 0x28),
    ('ltmp', ((0x80, 0x68), (0x40, 0x38)), 0x0C),
    ('scnr', ((0x360, 0x30), (0x10, 0x34)), 0x00),
    ('DECR', (), 0x38), ('PRTM', (), 0xA0), ('dc*s', ((0x00, 0x30), (0x10, 0x34)), 0x00), ('weat', ((0x00, 0x8C),), 0x40),
)


class ContentComparer(object):
    """Normalised comparison of two MCC tags (usually: the input map's tag vs the stock MCC shared.map tag of the same name).
    compare() -> (status, why)  status 'equal' | 'modified'.  With payload=True the raw resources are compared too:
    decoded bitmap pixels (textures.dat or in-map), geometry blkh blobs and, for snd!, the gestalt records + audio chunks."""

    def __init__(self, src, shared, payload=True, dat_dir=None, dat_dir_a=None, dat_dir_b=None):
        """dat_dir: textures.dat / sounds_*.dat folder of BOTH sides (the default, stock inputs and H2EK maps).  Custom-shared support compares a
        workshop mod's tag with the stock one: dat_dir_a (the input side `src`) and dat_dir_b (the `shared` side) name one folder per side."""
        self.a = src
        self.b = shared
        self.lay = layouts('mcc')
        self.payload = payload
        self.dat_dir = dat_dir
        self.dat_dir_a = dat_dir_a
        self.dat_dir_b = dat_dir_b
        self._dats = None
        self._dats_ab = {}
        self._maps = {}
        self._memo = {}
        self._all = False

    # ---- helpers ------------------------------------------------------------------------------------
    extra_rules = None            # {group: rules dict} merged over GROUP_RULES by the caller (custom-shared planning: matg language-table offsets)

    def _rules(self, group):
        r = GROUP_RULES.get(group, {})
        x = self.extra_rules.get(group) if self.extra_rules else None
        if not x:
            return r
        m = dict(r)
        for k, v in x.items():
            m[k] = tuple(m.get(k, ())) + tuple(v)
        return m

    def _decode(self, cache, tag):
        g = tag.group
        lay = self.lay.get(g)
        if lay is None:
            return None
        return T.decode_tag(cache.read_at_addr, lay, tag.addr, g)

    @staticmethod
    def _tname(cache, datum):
        if datum in (0, NULL):
            return None
        t = cache.tag_by_datum(datum)
        if t is None:
            return ('?', datum)
        if t.external:
            return (t.group, (t.name or '').lower())
        return (t.group, (t.name or '').lower())

    @staticmethod
    def _stext(cache, sid):
        if sid in (0, NULL):
            return ''
        try:
            return cache.string_id(sid)
        except Exception:
            return '?%08X' % sid

    # ---- structural compare ----------------------------------------------------------------------------
    def _cmp(self, a, b, ca, cb, rules, why):
        """Walk two decoded trees; append a message per difference to `why`.  Stops at the first difference unless
        self._all is set (diff_all)."""
        if len(a.data) != len(b.data):
            why.append('%s: struct size' % (a.path or '/'))
            return False
        path = a.path
        skip_f = rules.get('skip_fields', ())
        skip_b = rules.get('skip_blocks', ())
        skip_d = rules.get('skip_datas', ())
        skip_n = rules.get('skip_names', ())
        ok = True
        for f in a.fields:
            o, s, name = f.offset, f.size, f.name
            tp = f.type
            if (path, name) in skip_f:
                continue
            if any(name.startswith(p) for p in skip_n):
                continue
            msg = None
            if tp in T.BLOCK_TYPES:
                if (path + '/' + name) in skip_b:
                    continue
                ka, kb = a.blocks.get(o, []), b.blocks.get(o, [])
                if len(ka) != len(kb):
                    msg = '%s/%s: count %d vs %d' % (path, name, len(ka), len(kb))
                else:
                    for x, y in zip(ka, kb):
                        if not self._cmp(x, y, ca, cb, rules, why):
                            ok = False
                            if not self._all:
                                return False
            elif tp in T.DATA_TYPES:
                if name in skip_d:
                    continue
                if (a.datas.get(o) or b'') != (b.datas.get(o) or b''):
                    msg = '%s/%s: data differs' % (path, name)
            elif tp == 'tagref':
                if s == 8:
                    ga, da = struct.unpack_from('<II', a.data, o)
                    gb, db = struct.unpack_from('<II', b.data, o)
                    na, nb = self._tname(ca, da), self._tname(cb, db)
                    if na != nb or (na is not None and ga != gb):
                        msg = '%s/%s: tagref %s vs %s' % (path, name, na, nb)
                else:
                    da = struct.unpack_from('<I', a.data, o)[0]
                    db = struct.unpack_from('<I', b.data, o)[0]
                    na, nb = self._tname(ca, da), self._tname(cb, db)
                    if na != nb:
                        msg = '%s/%s: tagref %s vs %s' % (path, name, na, nb)
            elif tp == 'stringid':
                sa = self._stext(ca, struct.unpack_from('<I', a.data, o)[0])
                sb = self._stext(cb, struct.unpack_from('<I', b.data, o)[0])
                if sa != sb:
                    msg = '%s/%s: string %r vs %r' % (path, name, sa, sb)
            else:
                if name.startswith(_SCALAR_SKIP_PREFIX) or name == 'User Data' or name in _SKIP_ANY_NAME:
                    continue
                if a.data[o:o + s] != b.data[o:o + s]:
                    msg = '%s/%s@0x%X: value differs' % (path, name, o)
            if msg is not None:
                why.append(msg)
                ok = False
                if not self._all:
                    return False
        return ok

    def diff_all(self, ta, tb, limit=200):
        """Every structural difference between two tags (list of strings; payloads not compared)."""
        na, nb = self._decode(self.a, ta), self._decode(self.b, tb)
        why = []
        self._all = True
        try:
            self._cmp(na, nb, self.a, self.b, self._rules(ta.group), why)
        finally:
            self._all = False
        return why[:limit]

    # ---- payloads ------------------------------------------------------------------------------------------
    def _dat(self, cache=None):
        """DatStore of a side: dat_dir_a / dat_dir_b when given (custom-shared), else the one dat_dir."""
        side_dir = None
        if cache is not None:
            side_dir = self.dat_dir_a if cache is self.a else self.dat_dir_b if cache is self.b else None
        if side_dir:
            ds = self._dats_ab.get(side_dir)
            if ds is None:
                from . import raw_mcc as R
                ds = self._dats_ab[side_dir] = R.DatStore(side_dir)
            return ds
        if self._dats is None:
            from . import raw_mcc as R
            self._dats = R.DatStore(self.dat_dir)
        return self._dats

    def _bitmap_hashes(self, cache, tag):
        from . import raw_mcc as R
        if not tag.addr:
            return []
        external = bool(cache.header['flags'] & R.HDR_FLAG_EXTERNAL_DAT)
        buf = self._dat(cache).get(R.TEXTURES_DAT) if external else cache.mm
        data = cache.read_at_addr(tag.addr, 0x50)
        cnt, ptr = struct.unpack_from('<iI', data, 0x44)
        out = []
        if cnt <= 0 or not ptr:
            return out
        blob = cache.read_at_addr(ptr, cnt * R.BITMAP_ELEMENT_SIZE)
        for i in range(cnt):
            el = blob[i * R.BITMAP_ELEMENT_SIZE:(i + 1) * R.BITMAP_ELEMENT_SIZE]
            off, size = struct.unpack_from('<I', el, 0x38)[0], struct.unpack_from('<I', el, 0x50)[0]
            if not size:
                out.append(b'')
                continue
            try:
                px, _ = R.decode_texture_blob(buf, off, size)
                out.append(hashlib.md5(px).digest())
            except Exception as ex:                      # unreadable payload = treat as different
                out.append(('ERR %s' % ex).encode())
        return out

    def _geometry_hashes(self, cache, tag):
        sites = [s for s in _GEOM_SITES if s[0] == tag.group]
        if not sites or not tag.addr:
            return []
        out = []
        lay = self.lay.get(tag.group)
        base = cache.read_at_addr(tag.addr, lay.base_size or lay.computed_size)

        def walk(blob, path, depth, info_off):
            if depth == len(path):
                inf = blob[info_off:info_off + 8]
                if len(inf) < 8:
                    return
                bo, bs = struct.unpack('<II', inf)
                if bo in (0, NULL) or not bs:
                    out.append(b'')
                    return
                try:
                    out.append(hashlib.md5(cache.raw(bo & 0x3FFFFFFF, bs)).digest())
                except Exception as ex:
                    out.append(('ERR %s' % ex).encode())
                return
            boff, esz = path[depth]
            cnt, ptr = struct.unpack_from('<iI', blob, boff)
            if cnt <= 0 or not ptr:
                return
            bl = cache.read_at_addr(ptr, cnt * esz)
            for k in range(cnt):
                walk(bl[k * esz:(k + 1) * esz], path, depth + 1, info_off)

        for g, path, info_off in sites:
            walk(base, path, 0, info_off)
        return out

    def _mapobj(self, cache):
        from . import raw_mcc as R
        m = self._maps.get(cache.path)
        if m is None:
            ds = self._dat(cache)
            m = R.MccMap(cache.path, dat_dir=ds.directory if (self.dat_dir_a or self.dat_dir_b) else self.dat_dir, dats=ds)
            self._maps[cache.path] = m
        return m

    def _sound_fingerprint(self, cache, tag):
        """Digest of everything the Vista snd! + its gestalt entries are built from: playback / scale records, pitch-range
        parameters, per-permutation records (names by text) and the audio chunk bytes."""
        mm = self._mapobj(cache)
        g = mm.sound_gestalt()
        m = mm.m
        o = mm.a2o(tag.addr)
        info = bytes(m[o:o + 0x1C])
        flags = struct.unpack_from('<H', info, 0)[0]
        ppi, pri = struct.unpack_from('<hh', info, 6)
        prc, scale = info[0xA], info[0xB]
        go = g.o
        h = hashlib.md5()
        h.update(info[:3])                                       # flags, sound class, (codec index excluded: gestalt-relative)
        h.update(info[0x10:0x18])                                # max play time, inner silence distance
        h.update(struct.pack('<B', info[0xD] and 1 or 0))

        def blk(off):
            n, p = struct.unpack_from('<iI', m, go + off)
            return n, (mm.a2o(p) if p else 0)

        def rec(off, esz, idx):
            n, po = blk(off)
            if idx < 0 or idx >= n or not po:
                return b''
            return bytes(m[po + idx * esz:po + (idx + 1) * esz])

        def imp_text(idx):
            r = rec(0x20 if False else 0x18, 4, idx)
            if len(r) < 4:
                return ''
            return self._stext(cache, struct.unpack('<I', r)[0])

        h.update(rec(0x08, 0x38, ppi) if ppi != 0xFFFF and ppi >= 0 else b'')            # playback parameters
        h.update(rec(0x10, 0x14, scale) if scale != 0xFF and scale >= 0 else b'')       # scales
        for r in range(pri, pri + prc):
            pr = rec(0x28, 12, r)
            if len(pr) < 12:
                continue
            imp, par, enc, rtf, first, npm = struct.unpack('<hhhhhh', pr)
            h.update(imp_text(imp).encode('latin-1', 'replace'))
            h.update(rec(0x20, 0xA, par))                                                # pitch range parameters
            h.update(struct.pack('<h', npm))
            for pi in range(first, first + npm):
                pm = rec(0x30, 0x10, pi)
                if len(pm) < 16:
                    continue
                imp2, skip, gain, pinfo, lnt = struct.unpack_from('<hhbbh', pm, 0)
                h.update(imp_text(imp2).encode('latin-1', 'replace'))
                h.update(struct.pack('<hbbh', skip, gain, pinfo, lnt))
                if self.payload:
                    try:
                        h.update(hashlib.md5(g.read_permutation(pi, flags)).digest())
                    except Exception as ex:
                        h.update(('ERR %s' % ex).encode())
        return h.digest()

    # ---- public --------------------------------------------------------------------------------------------
    def compare(self, ta, tb):
        """ta: tag of the input map, tb: tag of the MCC shared.map.  Returns ('equal'|'modified', why)."""
        key = (ta.index, tb.index)
        r = self._memo.get(key)
        if r is not None:
            return r
        r = self._compare(ta, tb)
        self._memo[key] = r
        return r

    def _compare(self, ta, tb):
        g = ta.group
        if g != tb.group:
            return ('modified', 'group differs')
        try:
            na, nb = self._decode(self.a, ta), self._decode(self.b, tb)
        except Exception as ex:
            return ('modified', 'decode error %s' % ex)
        if na is None:
            return ('modified', 'no layout for %s' % g)
        why = []
        rules = self._rules(g)
        if not self._cmp(na, nb, self.a, self.b, rules, why):
            return ('modified', why[0] if why else 'differs')
        if g == 'unic':
            # the text is not in the tag (it is in the map's language pack): compare the effective strings of the 8 languages
            # Cheap, so done even with payload=False.
            try:
                from .handlers import locale as LOC
                if not LOC.unic_same_content(self.a, ta, self.b, tb):
                    return ('modified', 'unic strings differ')
            except Exception as ex:                                      # noqa: BLE001
                return ('modified', 'unic compare error %s: %s' % (type(ex).__name__, ex))
        if self.payload:
            try:
                if g == 'bitm':
                    ha, hb = self._bitmap_hashes(self.a, ta), self._bitmap_hashes(self.b, tb)
                    if ha != hb:
                        return ('modified', 'bitmap pixel data differs')
                elif g in ('mode', 'DECR', 'PRTM', 'weat', 'dc*s'):
                    ha, hb = self._geometry_hashes(self.a, ta), self._geometry_hashes(self.b, tb)
                    if ha != hb:
                        return ('modified', 'geometry blob differs')
                elif g == 'snd!':
                    if self._sound_fingerprint(self.a, ta) != self._sound_fingerprint(self.b, tb):
                        return ('modified', 'sound data/gestalt differs')
            except Exception as ex:
                return ('modified', 'payload compare error %s: %s' % (type(ex).__name__, ex))
        return ('equal', '')


# ======================================================================================================
# the builder
# ======================================================================================================
class _Builder(object):
    def __init__(self, src, mcc_shared, vista, opts):
        self.src = src
        self.vista = vista
        self.opts = dict(opts or {})
        self.mcc_shared = mcc_shared
        self.msh = None
        if mcc_shared is not None:
            self.msh = mcc_shared.get('shared') if hasattr(mcc_shared, 'get') else mcc_shared
        self.lay_m = layouts('mcc')
        self.scan_m = RefScanner(self.lay_m)
        self.map_type = self.opts.get('map_type', src.header['scenario_type'])
        self.is_mp = self.map_type == 1
        self.self_contained = bool(self.opts.get('self_contained', False)) or not self.is_mp
        self.linking = not self.self_contained
        self.has_ext = any(t.external for t in src.tags)
        self.src_kind = 'stock' if self.has_ext else 'standalone'
        if self.msh is None:
            self.msh = src.open_shared() if self.has_ext else _find_stock_mcc_shared()
            if self.msh is None and self.has_ext:
                raise PlannerError('input map has external (shared.map) tags but no MCC shared.map was supplied')
        if self.msh is None and not self.opts.get('self_contained') and self.is_mp:
            # a standalone (H2EK-built) input cannot be compared with the stock shared.map: converting is the safe choice
            self.opts['strict_link'] = True
        self.vsh = vista.shared
        self.vsh_name = {}
        for t in self.vsh.tags:
            if not t.is_empty and t.name and t.addr:
                self.vsh_name.setdefault(_nk(t.group, t.name), t)
        self.donors = DonorIndex(vista.maps_dir, opener=getattr(vista, 'open', None))
        self.plan = Plan()
        self.plan.src = src
        self.plan.mcc_shared_cache = self.msh
        self.plan.map_type = self.map_type
        self.plan.opts = self.opts
        self.plan.self_contained = self.self_contained
        self.plan.src_kind = self.src_kind
        self.by_src = self.plan.by_src
        self.by_name = self.plan.by_name
        self.all = self.plan.all_entries
        # MCC name indexes
        self.src_name = {}                 # nk -> key  (externals resolve to ('shared', idx))
        for t in src.tags:
            if t.is_null or not t.name:
                continue
            self.src_name.setdefault(_nk(t.group, t.name), (SHARED, t.index) if t.external else (SRC, t.index))
        self.msh_name = {}
        if self.msh is not None:
            for t in self.msh.tags:
                if t.is_null or not t.name:
                    continue
                self.msh_name.setdefault(_nk(t.group, t.name), (SHARED, t.index))
        self._refs = {}
        self.status = {}                   # key -> ('equal'|'modified'|'unverifiable', why)
        self.tainted = {}                  # key -> name of the modified dependency
        self.used_links = set()            # Vista shared indices referenced by kept tags
        self.reached = set()               # id(entry)
        self.children = collections.defaultdict(list)     # id(parent entry) -> [entries first reached through it]
        self.own_key = None
        self.t0 = time.time()
        self.comparer = None
        self.train_hudg = None             # training-off globals (see _setup_training_globals)
        self.clone_globals = False
        self.custom_shared = False         # the externals resolve against a CUSTOM (workshop mod) shared.map: see _detect_custom_shared
        self.stock = None                  # the stock MCC shared.map (custom-shared only)
        self.cmp_shared = self.cmp_src = None
        self._detect_custom_shared()

    # ---- custom shared.map (workshop mods), step 2 ------------------------------------------------------------------------------------
    def _detect_custom_shared(self):
        """custom_shared = a MULTIPLAYER input with external tags whose shared.map is NOT the stock MCC one (creation time / size differ).  Then every
        shared tag of the mod is compared with the stock MCC shared.map exactly like the tags of an H2EK-built map (prepass): changed ones, and
        every tag that references a changed one, become LOCAL tags converted from the mod; the rest links to Vista's shared.map as for a stock map.
        Gated: a map built against the stock shared.map plans exactly as before.  opts['custom_shared'] = False switches it off; opts
        'stock_shared' (path) / 'stock_dat_dir' name the stock files (default: the installed game, paths.stock_mcc_maps_dir)."""
        o = self.opts
        if o.get('custom_shared') is False or not (self.has_ext and self.is_mp and self.linking and self.msh is not None):
            return
        stock = None
        p = o.get('stock_shared')
        if p and os.path.isfile(p):
            from .mcc_cache import MccCache
            stock = MccCache(p)
        if stock is None:
            stock = _find_stock_mcc_shared()
        if stock is None:
            return
        same = (stock.header['creation_time'] == self.msh.header['creation_time'] and stock.file_size == self.msh.file_size)
        if same:
            return
        # the externals of the input must resolve in THIS shared.map (same index, group and datum): a wrong file would silently map every
        # external tag to some other tag
        bad = [t for t in self.src.tags if t.external and (t.index >= len(self.msh.tags) or self.msh.tags[t.index].datum != t.datum
                                                         or self.msh.tags[t.index].group != t.group)]
        if bad:
            raise PlannerError('the shared.map %s is not the one this map was built against: %d of its %d external tags do not match it '
                               '(e.g. tag #%d %s)' % (self.msh.source_path, len(bad), sum(1 for t in self.src.tags if t.external), bad[0].index, bad[0].group))
        self.custom_shared = True
        self.stock = stock
        self.plan.notes['custom_shared'] = {'shared_map': self.msh.source_path, 'creation_time': self.msh.header['creation_time'],
                                            'stock_shared_map': stock.source_path, 'stock_creation_time': stock.header['creation_time']}

    # ------------------------------------------------------------------------------------------------------
    def warn(self, code, msg):
        self.plan.stats['warn:' + code] += 1
        if len(self.plan.warnings) < 2000:
            self.plan.warnings.append((code, msg))

    def tag_of(self, key):
        return (self.src if key[0] == SRC else self.msh).tags[key[1]]

    def cache_of(self, key):
        return self.src if key[0] == SRC else self.msh

    # ---- MCC refs -----------------------------------------------------------------------------------------
    def target_key(self, src_map, datum):
        if datum in (0, NULL):
            return None
        if src_map == SRC:
            t = self.src.tag_by_datum(datum)
            if t is None:
                return None
            if t.external:
                return (SHARED, t.index)
            return (SRC, t.index)
        t = self.msh.tag_by_datum(datum)
        return None if t is None else (SHARED, t.index)

    def mcc_refs(self, key):
        r = self._refs.get(key)
        if r is not None:
            return r
        cache = self.cache_of(key)
        t = cache.tags[key[1]]
        r = []
        # an MCC 'empty' tag (size 0, address aliasing another tag's data) has no content: scanning its address would read
        # the references of whatever tag lives there
        if t.addr and t.size and t.group in self.lay_m:
            try:
                for g, d, off in self.scan_m.scan(cache.read_at_addr, t.group, t.addr):
                    tk = self.target_key(key[0], d)
                    if tk is not None:
                        r.append((tk, off))
            except Exception as ex:
                self.warn('ref-scan', '%s %s: %s' % (t.group, t.name, ex))
        self._refs[key] = r
        return r

    # ---- policy -----------------------------------------------------------------------------------------------
    def _new(self, key, tag, action, reason, donor=None):
        e = PlanEntry(key[0], key[1], tag.group, tag.name, action, reason, donor=donor)
        e.src_size = tag.size
        return e

    def _register(self, e, key=None, by_name=True):
        self.all.append(e)
        if key is not None:
            self.by_src[key] = e
        if by_name and e.name:
            self.by_name.setdefault(_nk(e.group, e.name), e)
        return e

    def _link_entry(self, vt, key, tag, reason):
        e = self._new(key, tag, 'link', reason) if key is not None else PlanEntry(None, None, vt.group, vt.name, 'link', reason)
        e.out_index = vt.index
        e.out_datum = vt.datum
        return e

    def _vista_twin(self, group, name):
        return self.vsh_name.get(_nk(group, name)) if self.linking else None

    def _donor_for(self, group, name):
        hits = self.donors.find(group, name)
        return hits[0] if hits else None

    # ---- MCC-only stems: structurally identical Vista stem (see decide(), group 'stem') ----------------------------------------------
    @staticmethod
    def _stem_signature(root):
        """Skeleton of a stem's Postprocess Definition[0] without its pass tag references: LOD (layer index, available layers, height),
        Layers, the Implementation index of every Pass, Implementations and Remappings.  The same bytes in MCC and Vista layouts."""
        pps = root.block('Postprocess Definition')
        if not pps:
            return None
        pp = pps[0]
        return (tuple(bytes(k.data).hex() for k in pp.block('Levels Of Detail')),
                tuple(bytes(k.data).hex() for k in pp.block('Layers')),
                tuple(bytes(k.data[8:10]).hex() for k in pp.block('Passes')),
                tuple(bytes(k.data).hex() for k in pp.block('Implementations')),
                tuple(bytes(k.data).hex() for k in pp.block('Remappings')))

    def _vista_stem_signatures(self):
        """{signature: [(map file, tag index, name)]} over the Vista stems of the donor maps (shared.map first), built once."""
        m = getattr(self, '_vsig', None)
        if m is None:
            m = {}
            dx = self.donors
            if dx._index is None:
                dx._build()
            lay_v = layouts('vista')
            for (g, _nm), lst in dx._index.items():
                if g != 'stem':
                    continue
                f, i = lst[0]
                c = dx.cache(f)
                t = c.tags[i]
                try:
                    sig = self._stem_signature(T.decode_tag(c.read_addr, lay_v['stem'], t.addr, 'stem'))
                except Exception:                                    # noqa: BLE001
                    continue
                if sig is not None:
                    m.setdefault(sig, []).append((f, i, t.name))
            self._vsig = m
        return m

    def _stem_substitute(self, key, tag):
        """(map file, tag index, name) of the Vista stem that best replaces an MCC stem with no Vista twin by name, or None.  Only stems with an IDENTICAL
        postprocess skeleton qualify (shaders keep their array semantics); among them: same template folder, then the longest common
        word prefix of the names (sky_solid -> sky_two_alpha_clouds, water_pool_cube -> water_pool), then alphabetical."""
        try:
            cache = self.cache_of(key)
            sig = self._stem_signature(T.decode_tag(lambda a, n: cache.read_at_addr(a, n), self.lay_m['stem'], tag.addr, 'stem'))
        except Exception as ex:                                        # noqa: BLE001
            self.warn('stem-substitute', '%s: %s' % (tag.name, ex))
            return None
        cands = self._vista_stem_signatures().get(sig) if sig is not None else None
        if not cands:
            return None
        folder, _, leaf = (tag.name or '').lower().replace('/', '\\').rpartition('\\')
        words = leaf.split('_')

        def score(d):
            f, _, l = d[2].lower().rpartition('\\')
            w = l.split('_')
            common = 0
            for a, b in zip(words, w):
                if a != b:
                    break
                common += 1
            return (-(f == folder), -common, d[2].lower())
        return sorted(cands, key=score)[0]

    # ---- training-off globals (2026-10-20) ---------------------------------------------------------------------------------------------
    def _matg_mode(self):
        """'convert' (local globals converted from the input) or 'link' (Vista's globals) after resolving hud / auto."""
        mode = self.opts.get('matg', 'link')
        if self.custom_shared and mode in ('hud', 'auto'):
            # custom shared.map: the mod's OWN globals (custom player models / representations, damage table, HUD, ...) are converted locally
            # when they differ from the stock MCC ones in any way, or when the HUD text is customised; unchanged globals take the Vista path
            return 'convert' if (self.plan.notes.get('matg', ('',))[0] == 'modified' or self.plan.notes.get('hud', ('',))[0] == 'custom') else 'link'
        if mode == 'auto':           # local globals only when the input's globals differ from the stock MCC ones
            return 'convert' if self.plan.notes.get('matg', ('',))[0] == 'modified' else 'link'
        if mode == 'hud':            # DEFAULT: local globals only when the HUD text under matg is customised (see prepass)
            return 'convert' if self.plan.notes.get('hud', ('',))[0] == 'custom' else 'link'
        return mode

    def _setup_training_globals(self):
        """Training is OFF in every conversion (convert.convert sets opts['training_off']; opts keep_training / env H2CONV_KEEP_TRAINING keep it;
        a direct planner.build_plan call without 'training_off' behaves as before, which keeps the planner oracles unchanged).  The game's tutorial prompts draw as an empty
        square on converted maps, and Vista's SHARED hudg (the one the shared matg references) cannot be edited per map.  So an MP map whose
        globals would be LINKED gets its OWN globals instead: clone_globals = a local copy of the Vista shared matg and of the hudg it
        references, byte-faithful to Vista (cloned from shared.map, not from the MCC input) except that the hudg Player Training Data
        block is emptied (donor.import_donor); every other reference of both tags stays on the shared slots (PlanEntry.shared_refs).
        A map whose globals are converted locally keeps its converted matg (handlers/gameplay.hudg_root empties its hudg); only a hudg
        that would still be LINKED there (stock HUD, --matg local) is replaced by the same clone."""
        self.train_hudg = None            # (name key, Vista shared hudg tag) of the hudg that Vista's shared matg references
        self.clone_globals = False        # the matg itself is a clone too
        o = self.opts
        if not o.get('training_off') or o.get('keep_training') or os.environ.get('H2CONV_KEEP_TRAINING') or not self.is_mp:
            return
        vm = next((t for t in self.vsh.tags if not t.is_empty and t.group == 'matg' and t.addr), None)
        if vm is None:
            return
        hud = []
        for f2, i2, _off in self.donors.refs('shared.map', vm.index):
            t2 = self.donors.tag(f2, i2)
            if t2.group == 'hudg':
                hud.append(t2)
        if not hud:
            self.warn('training-globals', 'the Vista shared matg references no hudg: training stays as it is')
            return
        self.train_hudg = (_nk(hud[0].group, hud[0].name), hud[0])
        self.clone_globals = self._matg_mode() != 'convert'
        self.plan.notes['training_off'] = {'matg': 'clone of the Vista shared matg' if self.clone_globals else 'converted from the input',
                                           'hudg': hud[0].name}

    def _globals_clone(self, key, tag, vt, why):
        """Donor entry that clones the Vista shared tag `vt` (matg / hudg) as a LOCAL tag; key None = a tag that exists on the Vista side only."""
        reason = 'training off: local clone of the Vista shared %s (%s); every reference stays on the shared slots' % (vt.group, why)
        if key is None:
            e = PlanEntry(None, None, vt.group, vt.name, 'donor', reason, donor=('shared.map', vt.index))
        else:
            e = self._new(key, tag, 'donor', reason, donor=('shared.map', vt.index))
        e.shared_refs = True
        self.plan.stats['globals_cloned'] += 1
        return e

    def decide(self, key):
        """Create (and register) the PlanEntry for an MCC tag."""
        tag = self.tag_of(key)
        g, n = tag.group, tag.name or ''
        nk = _nk(g, n)
        lo = n.lower()
        opts = self.opts
        # --- the input map's own sound gestalt ------------------------------------------------------------------
        if g == 'ugh!':
            if key == self.own_key:
                e = self._new(key, tag, 'convert', 'own sound gestalt (last tag; header secondary ugh!)')
                if self.linking and nk in self.vsh_name:
                    e.name = OWN_GESTALT_NAME             # the shared gestalt owns the original name
                self.plan.own_gestalt = e
                return self._register(e, key, by_name=False)
            vt = self.vsh_name.get(nk) or next((t for t in self.vsh.tags if not t.is_empty and t.group == 'ugh!' and t.addr), None)
            if self.linking and vt is not None:
                return self._register(self._link_entry(vt, key, tag, 'shared sound gestalt -> Vista shared ugh!'), key)
            return self._register(self._new(key, tag, 'drop', 'extra sound gestalt (single gestalt output)'), key)
        # --- globals ---------------------------------------------------------------------------------------------
        if g == 'matg':
            vt = self.vsh_name.get(nk) or next((t for t in self.vsh.tags if not t.is_empty and t.group == 'matg' and t.addr), None)
            mode = self._matg_mode()
            if mode == 'link' and self.is_mp and vt is not None and self.clone_globals:
                # training off (default): the map's OWN copy of Vista's shared globals, minus the hudg training block
                e = self._register(self._globals_clone(key, tag, vt, 'matg'), key)
            elif mode == 'link' and self.is_mp and vt is not None:
                e = self._register(self._link_entry(vt, key, tag, 'shared Vista globals tag (the custom-map list validator wants a '
                                                                  'shared matg; input matg changes are not carried over)'), key)
            else:
                e = self._new(key, tag, 'convert', 'globals converted locally (opts matg=%s): the map-list entry falls back to the '
                                                    'header name' % opts.get('matg', 'link'))
                if vt is not None:
                    e.twin = ('shared.map', vt.index)         # the Vista shared matg is the template for the converted tag
                self._register(e, key)
            if self.plan.globals_entry is None:
                self.plan.globals_entry = e
            return e
        # --- the hudg of Vista's shared matg when the globals are a training-off clone ------------------------------------------
        if g == 'hudg' and self.clone_globals and self.train_hudg is not None and nk == self.train_hudg[0]:
            return self._register(self._globals_clone(key, tag, self.train_hudg[1], 'hudg'), key)
        # --- MCC-only remastered sounds ------------------------------------------------------------------------------
        if g in ('snd!', 'lsnd') and lo.startswith(REMASTERED_PREFIX):
            e = self._register(self._new(key, tag, 'drop', 'MCC-only sound_remastered tag'), key)
            twin = LEGACY_PREFIX + lo[len(REMASTERED_PREFIX):]
            tk = self.src_name.get((g, twin)) or self.msh_name.get((g, twin))
            if tk is not None:
                e.redirect_key = tk
            else:
                vt = self.vsh_name.get((g, twin)) if self.linking else None
                if vt is not None:
                    e.redirect = self._ensure_vista_link(vt)
                else:
                    e.reason += ' (no legacy twin: references become null)'
            return e
        # --- MCC 'empty' tags (size 0, address aliasing another tag) -------------------------------------------------------
        if tag.empty:
            return self._decide_empty(key, tag)
        # --- scenario ------------------------------------------------------------------------------------------------
        if g == 'scnr':
            e = self._register(self._new(key, tag, 'convert', 'scenario (root)'), key)
            if self.plan.scenario is None:
                self.plan.scenario = e
            return e
        # --- MCC-only game-engine setting tags (sily Parameter > 0x6F): no Vista meaning ----------------
        if g == 'sily':
            why = self._sily_unrepresentable(key, tag)
            if why:
                self.plan.stats['sily_dropped'] += 1
                return self._register(self._new(key, tag, 'drop', 'MCC-only: ' + why), key)
        # --- shader family: MCC only has DXBC code ------------------------------------------------------------------------
        if g in SHADER_DONOR_GROUPS:
            vt = self._vista_twin(g, n)
            if vt is not None:
                return self._register(self._link_entry(vt, key, tag, 'shader family: Vista shared.map copy'), key)
            hit = self._donor_for(g, n)
            if hit is not None:
                e = self._new(key, tag, 'donor', 'shader family: MCC has DXBC only; donor map %s' % hit[0], donor=hit)
                return self._register(e, key)
            # A user-made MCC stem (cyberdyne_h2 `sky_solid`, sweet `water_pool_cube`, ...) has no Vista twin by name.  A shader whose Template is
            # NONE crashes the engine's sky / model draw (halo2.exe sub_67FE30 reads stem+0x58 without a null check; cyberdyne_h2 crash
            # 2026-10-20), so references are sent to a Vista stem whose postprocess skeleton is IDENTICAL (LOD, Layers, Pass implementation,
            # Implementations, Remappings): the shader's own arrays then keep their meaning (handlers.shaders.reconcile, tier 'sub').
            if g == 'stem':
                sub = self._stem_substitute(key, tag)
                if sub is not None:
                    e = self._new(key, tag, 'drop', 'STEM_SUBSTITUTE: MCC-only stem; references go to the structurally identical Vista stem %s' % sub[2])
                    self._register(e, key)
                    e.redirect = self.ensure_vista(sub[0], sub[1], e)
                    self.plan.stats['stem_substituted'] += 1
                    return e
            # MCC-only shader code (DXBC) with no Vista twin (e.g. vrtx debug_screenspace, only listed in the MCC globals): a converted copy
            # would be an empty shell, so the tag is dropped and references become null (design decision; 0 occurrences in
            # the studied stock inputs, 1 with --matg auto on the custom map)
            e = self._new(key, tag, 'drop', 'NO_DONOR: shader family tag with no Vista twin (MCC code is DXBC only): dropped, references become null')
            self.plan.stats['no_donor'] += 1
            return self._register(e, key)
        # --- everything else: link when the Vista shared.map has the tag and it is unmodified ------------------------------
        vt = self._vista_twin(g, n)
        if vt is not None:
            act, why = self._link_verdict(key, tag)
            if act == 'link':
                if g == 'hudg' and self.train_hudg is not None and nk == self.train_hudg[0]:
                    # local (converted) globals whose hudg is unmodified: a linked hudg would keep Vista's training prompts
                    return self._register(self._globals_clone(key, tag, self.train_hudg[1], 'hudg, unmodified in the input'), key)
                return self._register(self._link_entry(vt, key, tag, why), key)
            e = self._new(key, tag, 'convert', why)
            if g == 'shad':
                e.twin = self._donor_for(g, n)
            return self._register(e, key)
        if key[0] == SHARED:
            if self.linking and not opts.get('pull_mcc_only', True):
                return self._register(self._new(key, tag, 'drop', 'MCC-only shared tag (absent from Vista shared.map); '
                                                                  'references become null (opts pull_mcc_only=False)'), key)
            e = self._new(key, tag, 'convert', 'MCC shared.map tag absent from Vista shared.map: pulled into the map'
                          if self.linking else 'MCC shared.map tag converted locally (no linking)')
            return self._register(e, key)
        e = self._new(key, tag, 'convert', 'local tag, no Vista shared twin')
        if g == 'shad':
            e.twin = self._donor_for(g, n)
        return self._register(e, key)

    def _sily_unrepresentable(self, key, tag):
        """Reason string when an MCC `sily` tag sets a game-engine parameter that Vista does not have, else None."""
        try:
            from .handlers.gameplay import unrepresentable_tag
            cache = self.cache_of(key)
            node = T.decode_tag(lambda a, n: cache.read_at_addr(a, n), self.lay_m['sily'], tag.addr, 'sily')
            return unrepresentable_tag('sily', node)
        except Exception as ex:                                        # noqa: BLE001
            self.warn('sily-check', '%s: %s' % (tag.name, ex))
            return None

    def _decide_empty(self, key, tag):
        """An MCC tag with size 0 but a non-zero address (31 in the custom headlong, 33 in shared.map, 4 `shad` in lockout) holds no
        data - the address aliases another tag - so nothing can be converted from it.
        References are sent to the best real tag instead of a synthesised zero struct:
          1. a Vista shared.map tag with the same (group, name)                -> link
          2. a `shad` that a Vista map defines under that name (lockout props)  -> donor copy (the Vista original)
          3. a `shad`: the Vista shared fallback `shaders\\invalid`               -> drop, redirect
          4. anything else                                                      -> drop (references become null)."""
        g, n = tag.group, tag.name or ''
        self.plan.stats['empty_tags'] += 1
        why = 'empty in MCC (size 0, aliased address)'
        vt = self._vista_twin(g, n)
        if vt is not None:
            return self._register(self._link_entry(vt, key, tag, why + ': Vista shared.map tag of the same name'), key)
        if g == 'shad':
            hit = self._donor_for(g, n)
            if hit is not None:
                e = self._new(key, tag, 'donor', why + ': Vista original from donor map %s' % hit[0], donor=hit)
                return self._register(e, key)
            fb = self.vsh_name.get(_nk('shad', 'shaders\\invalid')) if self.linking else None
            e = self._new(key, tag, 'drop', why + ': dropped')
            if fb is not None:
                e.redirect = self._ensure_vista_link(fb)
                e.reason += '; references go to shaders\\invalid'
            return self._register(e, key)
        if g == 'mulg' and self.map_type != 1:
            # A campaign matg keeps its Multiplayer Globals reference, but the MCC (and stock Vista) campaign mulg is an empty aliased tag.  The
            # engine's multiplayer starting-weapon mapping (halo2.exe sub_45C52E: weapon type -> matg+0x168 -> mulg -> weapon table) dereferences it
            # when a campaign scenario is launched as a multiplayer game; NONE reads the garbage tag instance 0xFFFF (level1, 2026-10-20).  The Vista
            # mulg is copied WITHOUT following its references: every target that exists in the output (the weapons of the map) keeps its link,
            # the others (MP vehicles, sounds, names) become NONE.
            hit = self._donor_for(g, n)
            if hit is not None:
                e = self._new(key, tag, 'donor', why + ': campaign launched as multiplayer needs a mulg; Vista copy from %s without closure' % hit[0], donor=hit)
                e.no_closure = True
                self.plan.stats['solo_mulg_donor'] += 1
                return self._register(e, key)
        return self._register(self._new(key, tag, 'drop', why + ': dropped, references become null'), key)

    # ---- campaign maps launched as multiplayer: the scenario's own starting weapons as the "map default" loadout -----------------------
    def _profile_weapon_names(self):
        """Lower-case tag names of Player Starting Profile[0].Primary / Secondary Weapon of the MCC scenario ([] when it has none)."""
        scn = self.src.scenario
        lay = self.lay_m['scnr']
        fld = next(f for f in lay.fields if f.name == 'Player Starting Profile')
        cache = self.src
        raw = cache.read_at_addr(scn.addr, scn.size)
        cnt, ptr = struct.unpack_from('<II', raw, fld.offset)
        if cnt < 1:
            return []
        es = T.element_size(fld)
        el = cache.read_at_addr(ptr, es)
        out = []
        for nm in ('Primary Weapon', 'Secondary Weapon'):
            c = next((x for x in fld.children if x.name == nm), None)
            if c is None:
                continue
            datum = struct.unpack_from('<I', el, c.offset + 4)[0]
            t = cache.tag_by_datum(datum) if datum not in (0, NULL) else None
            if t is not None and t.name:
                out.append(t.name.lower())
        return out

    def _itmc_by_weapon(self):
        """{weapon tag name (lower): (map file, index, itmc name)} for the Vista item collections that hold exactly one weapon (multiplayer\single_weapons\*)."""
        m = getattr(self, '_itmc_map', None)
        if m is None:
            m = {}
            dx = self.donors
            if dx._index is None:
                dx._build()
            for (g, nm), lst in dx._index.items():
                if g != 'itmc':
                    continue
                f, i = lst[0]
                c = dx.cache(f)
                t = c.tags[i]
                try:
                    data = c.read_tag(t)
                    n, _p = struct.unpack_from('<II', data, 0)
                    d = struct.unpack_from('<I', data, 20)[0]
                    wt = c.tags[d & 0xFFFF]
                except Exception:                                    # noqa: BLE001
                    continue
                if n == 1 and wt.group == 'weap' and wt.name:
                    m.setdefault(wt.name.lower(), (f, i, t.name))
            self._itmc_map = m
        return m

    def _solo_starting_itmc(self):
        """A solo scenario carries its loadout in Player Starting Profile (campaign only).  Launched as a multiplayer game the engine takes the
        starting weapons from the scenario's Starting Equipment block (6 item collections, halo2.exe sub_45D5xx), which campaign scenarios leave
        empty - players spawn unarmed.  The Vista item collection that wraps exactly the profile's primary / secondary weapon is added to the plan
        (a donor copy; its weapon reference resolves to the map's own weapon tag); handlers.gameplay fills Starting Equipment[0] with it."""
        try:
            names = self._profile_weapon_names()
        except Exception as ex:                                        # noqa: BLE001
            self.warn('solo-start', 'Player Starting Profile unreadable: %s' % ex)
            return
        picks = []
        by_weapon = self._itmc_by_weapon()
        for wn in names:
            hit = by_weapon.get(wn)
            if hit is None:
                self.warn('solo-start', 'no Vista item collection wraps the starting weapon %s' % wn)
                continue
            if hit[2] in picks:
                continue
            e = self.ensure_vista(hit[0], hit[1], self.plan.scenario)
            self.expand([e])
            picks.append(hit[2])
        if picks:
            self.plan.notes['solo_starting_itmc'] = picks
            self.plan.stats['solo_starting_itmc'] += len(picks)

    def _ensure_vista_link(self, vt):
        nk = _nk(vt.group, vt.name)
        e = self.by_name.get(nk)
        if e is not None:
            return e
        e = PlanEntry(None, None, vt.group, vt.name, 'link', 'Vista shared.map tag')
        e.out_index, e.out_datum = vt.index, vt.datum
        return self._register(e)

    def _link_verdict(self, key, tag):
        """Link or convert a tag that exists in the Vista shared.map."""
        strict = self.opts.get('strict_link')
        if self.custom_shared:
            # custom shared.map: the shared tags AND the input's own tags are judged by their content status against the stock MCC shared.map
            st = self.status.get(key)
            if st is not None and st[0] == 'modified':
                return 'convert', 'MODIFIED vs stock MCC shared.map: %s' % st[1]
            cause = self.tainted.get(key)
            if cause:
                return 'convert', 'depends on modified tag %s' % cause
            if st is None:
                return 'link', 'MCC shared tag -> Vista shared.map tag of the same name' if key[0] == SHARED else \
                    'in Vista shared.map (not compared with the stock MCC shared.map)'
            if st[0] == 'unverifiable':
                if strict:
                    return 'convert', 'strict_link: cannot verify against the stock MCC shared.map'
                return 'link', 'in Vista shared.map; unverifiable (no stock MCC shared twin)'
            return 'link', 'unmodified (equal to the stock MCC shared.map tag)'
        if key[0] == SHARED:
            return 'link', 'MCC shared tag -> Vista shared.map tag of the same name'
        if self.src_kind == 'stock':
            if strict:
                return 'convert', 'strict_link: MCC local copy has no shared twin to compare with'
            return 'link', 'in Vista shared.map (MCC local copy has no shared twin to compare with)'
        st = self.status.get(key)
        if st is None or st[0] == 'unverifiable':
            if strict:
                return 'convert', 'strict_link: cannot verify against the stock MCC shared.map'
            cause = self.tainted.get(key)
            if cause:
                return 'convert', 'depends on modified tag %s' % cause
            return 'link', 'in Vista shared.map; unverifiable (no stock MCC shared twin)'
        if st[0] == 'modified':
            return 'convert', 'MODIFIED vs stock MCC shared.map: %s' % st[1]
        cause = self.tainted.get(key)
        if cause:
            return 'convert', 'depends on modified tag %s' % cause
        return 'link', 'unmodified (equal to the stock MCC shared.map tag)'

    # ---- Phase A: content status + taint ------------------------------------------------------------------------------------
    def _cs_candidate(self, t):
        """Is this tag of the input / of the mod's shared.map a candidate for 'link to the Vista shared twin unless modified'?"""
        if t.is_null or t.external or not t.name or not t.addr or t.empty:
            return False
        g, lo = t.group, t.name.lower()
        if g in SHADER_DONOR_GROUPS or g in CUSTOM_SHARED_SPECIAL:
            return False
        if g in ('snd!', 'lsnd') and lo.startswith(REMASTERED_PREFIX):
            return False
        return _nk(g, t.name) in self.vsh_name

    def _cs_compare(self, cmp_, t, mt):
        """compare() with the known baseline noise of a newer MCC build removed (CUSTOM_SHARED_NOISE): such a tag is 'equal' when EVERY
        difference found is one of the listed fields."""
        st = cmp_.compare(t, mt)
        if st[0] == 'modified':
            allowed = CUSTOM_SHARED_NOISE.get(_nk(t.group, t.name))
            if allowed:
                diffs = cmp_.diff_all(t, mt, 400)
                if diffs and all(any(d.startswith(a) for a in allowed) for d in diffs):
                    self.plan.stats['cs_noise_ignored'] += 1
                    return ('equal', 'build noise only (%s)' % '; '.join(sorted(set(d.split(':')[0] for d in diffs)))[:80])
        return st

    def _prepass_custom_shared(self):
        """Content status + taint for a map built against a CUSTOM shared.map (see _detect_custom_shared).  Candidates = the tags of the mod's
        shared.map AND the input's own (non-external) tags that have a same-name tag in Vista's shared.map; each is compared with the stock MCC
        shared.map tag of that name (textures.dat / sounds_*.dat of the MOD for the mod side, of the stock install for the stock side).  A candidate
        that is modified, or new in the mod although a Vista tag has its name, is converted; so is every candidate that (transitively) references one."""
        t_start = time.time()
        stock = self.stock
        o = self.opts
        payload = o.get('compare_payload', True)
        mod_dat = o.get('mcc_dat_dir') or o.get('dat_dir')
        stock_dat = o.get('stock_dat_dir') or _paths.stock_mcc_maps_dir()
        if payload and not (stock_dat and os.path.isfile(os.path.join(stock_dat, 'textures.dat'))):
            raise PlannerError('custom shared.map: the stock MCC textures.dat / sounds_*.dat folder was not found (%s); it is needed to compare the bitmaps and '
                               'sounds of the mod with the stock ones (opts stock_dat_dir / --mcc-maps of the installed game)' % stock_dat)
        if payload and mod_dat and not os.path.isfile(os.path.join(mod_dat, 'textures.dat')):
            raise PlannerError('custom shared.map: the mod\'s textures.dat was not found in %s' % mod_dat)
        self.cmp_shared = ContentComparer(self.msh, stock, payload=payload, dat_dir=mod_dat, dat_dir_a=mod_dat, dat_dir_b=stock_dat)
        self.cmp_src = ContentComparer(self.src, stock, payload=payload, dat_dir=mod_dat, dat_dir_a=mod_dat, dat_dir_b=stock_dat)
        for c in (self.cmp_shared, self.cmp_src):
            c.extra_rules = CUSTOM_SHARED_RULES
        self.comparer = self.cmp_shared
        stock_by = {}
        for t in stock.tags:
            if not t.is_null and t.name and t.addr:
                stock_by.setdefault((t.group, t.name.lower()), t)
        kit_link = set(_nk(g, n) for g, n in KIT_PLACEHOLDER_LINK) if o.get('kit_placeholder_link', True) else set()
        cset = set()
        for which, cache, cmp_ in ((SHARED, self.msh, self.cmp_shared), (SRC, self.src, self.cmp_src)):
            for t in cache.tags:
                if not self._cs_candidate(t):
                    continue
                key = (which, t.index)
                cset.add(key)
                mt = stock_by.get((t.group, t.name.lower()))
                if mt is None:
                    if which == SHARED:
                        # a tag the mod added, which happens to carry the name of a Vista shared tag: it cannot be that tag
                        self.status[key] = ('modified', 'not in the stock MCC shared.map (new tag of the mod)')
                    else:
                        self.status[key] = ('unverifiable', 'no stock MCC shared twin')
                    self.plan.stats['status:' + self.status[key][0]] += 1
                    continue
                self.status[key] = self._cs_compare(cmp_, t, mt)
                if which == SRC and self.status[key][0] == 'modified' and _nk(t.group, t.name) in kit_link:
                    self.plan.notes.setdefault('kit_placeholder_linked', []).append(t.group + ' ' + t.name + ' (' + self.status[key][1] + ')')
                    self.status[key] = ('equal', 'H2EK kit default placeholder; the shipped tag (Vista shared.map) is used')
                    self.plan.stats['kit_placeholder_linked'] += 1
                self.plan.stats['status:' + self.status[key][0]] += 1
        self.plan.stats['cs_candidates'] = len(cset)
        self.plan.stats['cs_candidates_src'] = sum(1 for k in cset if k[0] == SRC)
        # taint: a candidate that references a modified candidate must be converted too, or the modification would vanish
        rev = collections.defaultdict(list)
        for key in cset:
            for tk, _off in self.mcc_refs(key):
                if tk in cset:
                    rev[tk].append(key)
        ign = self.opts.get('taint_ignore')
        ign = DEFAULT_TAINT_IGNORE if ign is None else ign
        ign = set(_nk(g, n) for g, n in ign)
        seeds = [k for k in cset if self.status[k][0] == 'modified']
        ignored = [k for k in seeds if _nk(self.tag_of(k).group, self.tag_of(k).name) in ign]
        ign_keys = set(ignored)
        work = collections.deque(k for k in seeds if k not in ign_keys) if self.opts.get('taint', True) else collections.deque()
        cause = {k: self.tag_of(k).name for k in work}
        while work:
            k = work.popleft()
            for p in rev.get(k, ()):
                if p not in cause:
                    cause[p] = cause[k]
                    work.append(p)
        for k in cset:
            if self.status[k][0] != 'modified' and k in cause:
                if self.tag_of(k).group in CUSTOM_SHARED_NO_TAINT:
                    continue                  # uncuthud: mulg is local only when its OWN content differs (see CUSTOM_SHARED_SPECIAL)
                self.tainted[k] = cause[k]
        self.plan.stats['cs_modified'] = len(seeds)
        self.plan.stats['cs_modified_src'] = sum(1 for k in seeds if k[0] == SRC)
        self.plan.stats['cs_tainted'] = len(self.tainted)
        self.plan.stats['tainted'] = len(self.tainted)
        if ignored and self.opts.get('taint', True):
            self.plan.stats['taint_ignored'] = len(ignored)
            self.plan.notes['taint_ignored'] = sorted(self.tag_of(k).group + ' ' + self.tag_of(k).name for k in ignored)
        self.plan.notes['custom_shared_modified'] = sorted('%s %s :: %s' % (self.tag_of(k).group, self.tag_of(k).name, self.status[k][1])
                                                           for k in seeds)[:400]
        # the globals: compared with the stock MCC matg (drives _matg_mode); the HUD text chain below matg
        mg = self.src.globals_tag
        mg_key = None
        if mg is not None:
            mg_key = (SHARED, mg.index) if mg.external else (SRC, mg.index)
            mt = self.tag_of(mg_key)
            smg = stock.tag_by_name('matg', mt.name)
            if smg is not None and smg.addr and mt.addr:
                cmp_ = self.cmp_shared if mg_key[0] == SHARED else self.cmp_src
                st = cmp_.compare(mt, smg)
                self.plan.notes['matg'] = (st[0], cmp_.diff_all(mt, smg, 60) if st[0] == 'modified' else [])
        hud_mod = []
        if mg_key is not None:
            seen = set()
            frontier = [mg_key]
            for _depth in range(2):                      # matg -> hudg/unic, then hudg -> unic
                nxt = []
                for k in frontier:
                    for tk, _off in self.mcc_refs(k):
                        if tk in seen:
                            continue
                        seen.add(tk)
                        t2 = self.tag_of(tk)
                        if t2.group not in ('hudg', 'unic'):
                            continue
                        st = self.status.get(tk, ('',))[0]
                        if st == 'modified':
                            hud_mod.append('%s %s (modified)' % (t2.group, t2.name))
                        elif tk in self.tainted:
                            hud_mod.append('%s %s (references modified %s)' % (t2.group, t2.name, self.tainted[tk]))
                        elif _nk(t2.group, t2.name) not in self.vsh_name:
                            hud_mod.append('%s %s (custom tag, not in Vista shared.map)' % (t2.group, t2.name))
                        if t2.group == 'hudg':
                            nxt.append(tk)
                frontier = nxt
        self.plan.notes['hud'] = ('custom' if hud_mod else 'stock', hud_mod)
        self.plan.stats['hud_custom'] = len(hud_mod)
        self.plan.stats['cs_seconds'] = round(time.time() - t_start, 1)

    def prepass(self):
        if self.custom_shared:
            return self._prepass_custom_shared()
        if not self.linking or self.src_kind != 'standalone' or self.msh is None:
            return
        payload = self.opts.get('compare_payload', True)
        cmp_ = self.comparer = ContentComparer(self.src, self.msh, payload=payload, dat_dir=self.opts.get('dat_dir') or self.opts.get('mcc_dat_dir'))
        cands = []
        t_start = time.time()
        for t in self.src.tags:
            if t.is_null or t.external or not t.name or not t.addr or t.empty:
                continue
            g, lo = t.group, t.name.lower()
            if g in SHADER_DONOR_GROUPS or g in ('matg', 'ugh!', 'scnr'):
                continue
            if g in ('snd!', 'lsnd') and lo.startswith(REMASTERED_PREFIX):
                continue
            if _nk(g, t.name) not in self.vsh_name:
                continue
            cands.append(t)
        self.plan.stats['candidates'] = len(cands)
        mg = self.src.globals_tag                      # matg is always linked; record how far it is from the stock one
        if mg is not None and not mg.external and mg.addr:
            smg = self.msh.tag_by_name('matg', mg.name)
            if smg is not None and smg.addr:
                st = cmp_.compare(mg, smg)
                self.plan.notes['matg'] = (st[0], cmp_.diff_all(mg, smg, 60) if st[0] == 'modified' else [])
        cset = set()
        kit_link = set(_nk(g, n) for g, n in KIT_PLACEHOLDER_LINK) if self.opts.get('kit_placeholder_link', True) else set()
        for t in cands:
            key = (SRC, t.index)
            cset.add(key)
            mt = self.msh.tag_by_name(t.group, t.name)
            if mt is None or not mt.addr:
                self.status[key] = ('unverifiable', 'no stock MCC shared twin')
                self.plan.stats['status:unverifiable'] += 1
                continue
            self.status[key] = cmp_.compare(t, mt)
            if self.status[key][0] == 'modified' and kit_link and _nk(t.group, t.name) in kit_link:
                self.plan.notes.setdefault('kit_placeholder_linked', []).append(t.group + ' ' + t.name + ' (' + self.status[key][1] + ')')
                self.status[key] = ('equal', 'H2EK kit default placeholder; the shipped tag (Vista shared.map) is used')
                self.plan.stats['kit_placeholder_linked'] += 1
            self.plan.stats['status:' + self.status[key][0]] += 1
        # taint: a candidate that references a modified candidate must be converted too, or the modification would vanish
        rev = collections.defaultdict(list)
        for key in cset:
            for tk, off in self.mcc_refs(key):
                if tk in cset:
                    rev[tk].append(key)
        ign = self.opts.get('taint_ignore')
        ign = DEFAULT_TAINT_IGNORE if ign is None else ign
        ign = set(_nk(g, n) for g, n in ign)
        taint_on = self.opts.get('taint', True)
        seeds = [k for k in cset if self.status[k][0] == 'modified']
        ignored = [k for k in seeds if _nk(self.tag_of(k).group, self.tag_of(k).name) in ign]
        ign_keys = set(ignored)
        work = collections.deque(k for k in seeds if k not in ign_keys) if taint_on else collections.deque()
        cause = {k: self.tag_of(k).name for k in work}
        while work:
            k = work.popleft()
            for p in rev.get(k, ()):
                if p not in cause:
                    cause[p] = cause[k]
                    work.append(p)
        for k in cset:
            if self.status[k][0] != 'modified' and k in cause:
                self.tainted[k] = cause[k]
        self.plan.stats['tainted'] = len(self.tainted)
        if ignored and taint_on:
            # how many dependents the ignored tags would have tainted on their own (reported, so the effect is visible)
            reach, stack = set(), list(ignored)
            while stack:
                k = stack.pop()
                for p in rev.get(k, ()):
                    if p not in reach:
                        reach.add(p)
                        stack.append(p)
            saved = [p for p in reach if self.status[p][0] != 'modified' and p not in self.tainted]
            self.plan.stats['taint_ignored'] = len(ignored)
            self.plan.stats['taint_ignored_dependents_linked'] = len(saved)
            self.plan.notes['taint_ignored'] = sorted(self.tag_of(k).group + ' ' + self.tag_of(k).name for k in ignored)
        self.plan.stats['compare_s'] = round(time.time() - t_start, 1)
        # custom HUD text (confirmed in game on a custom chronopolis): pickup prompts etc. live in the unic tags
        # under matg -> HUD Globals (hudg) -> HUD Text.  A LINKED (shared) matg points at Vista's own hudg/unic, so custom strings
        # are never looked up.  --matg hud (the default) converts matg locally exactly when one of those tags is modified.
        hud_mod = []
        if mg is not None and not mg.external and mg.addr:
            seen = set()
            frontier = [(SRC, mg.index)]
            for _depth in range(2):                      # matg -> hudg/unic, then hudg -> unic
                nxt = []
                for k in frontier:
                    for tk, _off in self.mcc_refs(k):
                        if tk in seen:
                            continue
                        seen.add(tk)
                        t2 = self.tag_of(tk)
                        if t2.group not in ('hudg', 'unic'):
                            continue
                        st = self.status.get(tk, ('',))[0]
                        if st == 'modified':
                            hud_mod.append('%s %s (modified)' % (t2.group, t2.name))
                        elif tk in self.tainted:
                            hud_mod.append('%s %s (references modified %s)' % (t2.group, t2.name, self.tainted[tk]))
                        elif tk[0] == SRC and _nk(t2.group, t2.name) not in self.vsh_name:
                            hud_mod.append('%s %s (custom tag, not in Vista shared.map)' % (t2.group, t2.name))
                        if t2.group == 'hudg':
                            nxt.append(tk)
                frontier = nxt
        self.plan.notes['hud'] = ('custom' if hud_mod else 'stock', hud_mod)
        self.plan.stats['hud_custom'] = len(hud_mod)

    # ---- Phase B: decisions + closure ---------------------------------------------------------------------------------------------
    def ensure_key(self, key, parent=None):
        e = self.by_src.get(key)
        if e is None:
            tag = self.tag_of(key)
            nk = _nk(tag.group, tag.name)
            existing = self.by_name.get(nk)
            if existing is not None and tag.group != 'ugh!' and existing.src_map is None:
                # a Vista-side addition got there first: attribute it to this MCC tag
                existing.src_map, existing.src_index = key
                existing.src_size = tag.size
                self.by_src[key] = existing
                e = existing
            else:
                e = self.decide(key)
            if parent is not None and e.parent is None:
                e.parent = parent
                self.children[id(parent)].append(e)
        return e

    def ensure_vista(self, vfile, vidx, parent):
        """Plan entry for a tag found by following a Vista-layout ref of a donor / twin tag."""
        vt = self.donors.tag(vfile, vidx)
        nk = _nk(vt.group, vt.name)
        e = self.by_name.get(nk)
        if e is not None:
            return e
        shared_vt = self._vista_twin(vt.group, vt.name)
        if shared_vt is not None:
            e = self._ensure_vista_link(shared_vt)
        else:
            mk = self.src_name.get(nk) or self.msh_name.get(nk)
            if mk is not None:
                return self.ensure_key(mk, parent)
            e = PlanEntry(None, None, vt.group, vt.name, 'donor', 'closure of a Vista donor tag (%s)' % vfile, donor=(vfile, vidx))
            self._register(e)
        if e.parent is None:
            e.parent = parent
            self.children[id(parent)].append(e)
        return e

    def _ensure_hudg_clone(self, parent):
        """The (single) plan entry of the clone hudg: the input's own hudg of that name when it was decided, else a Vista-side entry."""
        h = self.train_hudg[1]
        e = self.by_name.get(self.train_hudg[0])
        if e is None:
            e = self._register(self._globals_clone(None, None, h, 'hudg'))
        if e.parent is None and parent is not None:
            e.parent = parent
            self.children[id(parent)].append(e)
        return e

    def expand(self, roots):
        stack = list(reversed(roots))
        while stack:
            e = stack.pop()
            if id(e) in self.reached:
                continue
            self.reached.add(id(e))
            nxt = []
            if e.action == 'convert' and e.src_map is not None:
                key = (e.src_map, e.src_index)
                for tk, off in self.mcc_refs(key):
                    nxt.append(self.ensure_key(tk, e))
                if e.twin is not None and self.opts.get('shader_twin_closure', True):
                    # the shader handler may emit the Vista donor shad: make sure its stem (and the stem's passes) are planned
                    for f2, i2, off in self.donors.refs(*e.twin):
                        g2 = self.donors.tag(f2, i2).group
                        # also the bitmaps of a converted SHADER's twin: tier T / array edits copy Bitmaps elements from the twin
                        # and name their bitmaps; a twin-only bitmap that is not planned would become a null reference (01a_tutorial:
                        # alpha_white x8).  Linked mode: a placeholder; self-contained: the MCC shared bitmap or a Vista donor copy.
                        if g2 == 'stem' or (g2 == 'bitm' and e.group == 'shad'):
                            nxt.append(self.ensure_vista(f2, i2, e))
            elif e.action == 'donor' and e.shared_refs:
                # a clone of a Vista shared tag: everything it references is a shared slot (listed through used_links / the matg closure),
                # except the clone hudg, which is local.  Never resolved by name: a local tag that shadows a shared one must not capture them.
                for f2, i2, off in self.donors.refs(*e.donor):
                    t2 = self.donors.tag(f2, i2)
                    if t2.group == 'hudg' and self.train_hudg is not None and _nk(t2.group, t2.name) == self.train_hudg[0]:
                        nxt.append(self._ensure_hudg_clone(e))
                    else:
                        self.used_links.add(i2)
            elif e.action == 'donor':
                for f2, i2, off in ([] if e.no_closure else self.donors.refs(*e.donor)):
                    nxt.append(self.ensure_vista(f2, i2, e))
            elif e.action == 'link':
                if e.out_index is not None and e.out_index >= LOCAL_LIMIT:
                    self.used_links.add(e.out_index)
            elif e.action == 'drop':
                if e.redirect is None and e.redirect_key is not None:
                    e.redirect = self.ensure_key(e.redirect_key, e)
                if e.redirect is not None:
                    nxt.append(e.redirect)
            for x in reversed(nxt):
                stack.append(x)

    def roots(self):
        out = []
        scn = self.src.scenario
        if scn is None:
            raise PlannerError('input map has no scenario tag')
        out.append(self.ensure_key((SRC, scn.index)))
        if self.own_key is not None:
            out.append(self.ensure_key(self.own_key))
        g = self.src.globals_tag
        if g is not None:
            k = (SHARED, g.index) if g.external else (SRC, g.index)
            out.append(self.ensure_key(k))                # link (default) or local conversion; sets plan.globals_entry
        return out

    # ---- reachability helpers -------------------------------------------------------------------------------------------------------
    def plain_reach(self, start_keys):
        seen, stack = set(), list(start_keys)
        while stack:
            k = stack.pop()
            if k in seen:
                continue
            seen.add(k)
            if k[0] == SRC or self.opts.get('follow_shared_in_reach', False):
                for tk, off in self.mcc_refs(k):
                    stack.append(tk)
        return seen

    def run(self):
        plan = self.plan
        src = self.src
        # own gestalt
        og = src.sound_gestalt
        if og is not None and og.has_data and not og.external:
            self.own_key = (SRC, og.index)
        self.prepass()
        self._setup_training_globals()
        # decisions for every local tag of the input map, in source order
        local_keys = [(SRC, t.index) for t in src.tags if not t.is_null and not t.external and t.name]
        for k in local_keys:
            self.ensure_key(k)
        roots = self.roots()
        self.expand(roots)
        if self.map_type != 1:
            self._solo_starting_itmc()
        # --- orphans ------------------------------------------------------------------------------------------------------------------
        scn_key = (SRC, src.scenario.index)
        r_plain = self.plain_reach([scn_key] + ([self.own_key] if self.own_key else []))
        mg = src.globals_tag
        r_matg = set()
        if mg is not None and not mg.external:
            r_matg = self.plain_reach([(SRC, mg.index)]) - r_plain
        orphans = []
        kept_orphans = []
        for k in local_keys:
            e = self.by_src[k]
            if id(e) in self.reached or e.action == 'drop':
                continue
            orphans.append((k, e))
            if k in r_plain:
                continue                          # superseded: reachable in MCC space only through replaced tags
            if k in r_matg and not self.opts.get('keep_matg_closure', False):
                continue                          # only the (replaced) globals tag refers to it
            kept_orphans.append(e)
            e.reason += ' [orphan kept: referenced only by stripped authoring data]'
            self.plan.stats['orphans_kept'] += 1
        if kept_orphans:
            self.expand(kept_orphans)             # may reach further orphans, which then stay too
        for k, e in orphans:
            if id(e) in self.reached:
                continue
            if k in r_plain:
                self._drop_orphan(e, 'superseded: only reachable through tags replaced by link/donor/drop')
            else:
                self._drop_orphan(e, 'only referenced via the globals tag (matg is replaced by the Vista shared one)')
        plan.stats['orphans_dropped'] = sum(1 for e in self.all if e.action == 'drop' and e.src_map == SRC and 'orphan' in e.reason)
        self.finish()
        plan.elapsed = time.time() - self.t0
        plan._builder = self
        plan.status = self.status
        plan.tainted = self.tainted
        return plan

    def _drop_orphan(self, e, why):
        e.action = 'drop'
        e.reason = 'orphan: ' + why
        e.out_index, e.out_datum = None, NULL
        e.donor = None

    # ---- numbering + placeholders --------------------------------------------------------------------------------------------------------------------
    def finish(self):
        plan = self.plan
        reached_local = [e for e in self.all if id(e) in self.reached and e.action in ('convert', 'donor')]
        src_part = sorted((e for e in reached_local if e.src_map == SRC), key=lambda e: e.src_index)
        emitted = set()
        order = []

        def emit(e0):
            stack = [e0]
            while stack:
                e = stack.pop()
                if id(e) in emitted or id(e) not in self.reached or e.action not in ('convert', 'donor'):
                    continue
                emitted.add(id(e))
                order.append(e)
                for c in reversed(self.children.get(id(e), ())):
                    if c.src_map != SRC:              # tags of the input map keep their own source position
                        stack.append(c)

        first = plan.scenario
        if first is not None:
            emit(first)
        for e in src_part:
            if e is not plan.own_gestalt:
                emit(e)
        for e in reached_local:                       # anything not hanging below a src tag (defensive)
            if e is not plan.own_gestalt:
                emit(e)
        if plan.own_gestalt is not None and id(plan.own_gestalt) in self.reached:
            emitted.add(id(plan.own_gestalt))
            order.append(plan.own_gestalt)
        if len(order) >= LOCAL_LIMIT:
            raise PlannerError('%d local tags: the Vista engine allows < %d' % (len(order), LOCAL_LIMIT))
        for i, e in enumerate(order):
            e.out_index = i
            e.out_datum = local_datum(i)
        plan.entries = order
        for e in self.all:                            # resolve redirects of dropped tags (no new entries are created here)
            if e.action == 'drop' and e.redirect is None and e.redirect_key is not None:
                e.redirect = self.by_src.get(e.redirect_key)
        plan.tag_count = MP_TAG_COUNT if (self.is_mp and (self.linking or self.opts.get('mp_layout', True))) else len(order)
        # unreached non-local entries keep their state; dropped / unreached converts are reported as drops
        for e in self.all:
            if e.action in ('convert', 'donor') and e.out_index is None:
                e.action = 'drop'
                e.reason = 'unreachable after link/donor decisions: ' + e.reason
        self._placeholders()

    def _placeholders(self):
        plan = self.plan
        if plan.tag_count != MP_TAG_COUNT:
            return
        sh = self.vsh
        graph = {}

        def kids(i):
            r = graph.get(i)
            if r is None:
                t = sh.tags[i]
                r = []
                if t.addr and t.group in self.donors.scanner.lay:
                    try:
                        for g, d, off in self.donors.scanner.scan(sh.read_addr, t.group, t.addr):
                            tg = self.donors.target('shared.map', d)
                            if tg is not None:
                                r.append(tg[1])
                    except Exception as ex:
                        self.warn('shared-scan', '%s: %s' % (t.name, ex))
                graph[i] = r
            return r

        roots = set(self.used_links)
        mode = self.opts.get('placeholders', 'closure')
        ugh_vt = next((t for t in sh.tags if not t.is_empty and t.group == 'ugh!' and t.addr), None)
        if ugh_vt is not None:
            roots.add(ugh_vt.index)               # every MP map lists the shared gestalt (first shared ugh! must match)
        if plan.globals_entry is not None and plan.globals_entry.action == 'link':
            roots.add(plan.globals_entry.out_index)
        if self.linking or self.clone_globals:
            # EVERY stock MP map lists the closure of the Vista shared matg (25/25), also when the output matg is LOCAL: Cartographer
            # injects content (the Halloween 'skeleton' biped, flood, event props ...) whose tags reach shared slots by INDEX, e.g.
            # jmad masterchief 10231 and ligh flashlight_3p 10435 through masterchief_skeleton.  A slot that is not listed stays
            # empty, the reference becomes NONE, the biped gets no animation manager and the player is never created (black
            # screen, name X-ed out; debug.txt "OBJECT CREATION FAILED: cannot create masterchief_skeleton").  The root is the
            # shared matg by its shared.map index, not globals_entry.out_index (a local matg has no shared index).
            vmatg = next((t for t in sh.tags if not t.is_empty and t.group == 'matg' and t.addr), None)
            if vmatg is not None:
                roots.add(vmatg.index)
        # any link entry that a local tag refers to was added to used_links by expand(); kept orphans too
        if mode == 'all':
            ph = {t.index for t in sh.tags if not t.is_empty and t.addr}
        else:
            if self.opts.get('common_extras', True):
                for g, n in COMMON_SHARED_EXTRAS:
                    t = self.vsh_name.get(_nk(g, n))
                    if t is not None:
                        roots.add(t.index)
            ph, stack = set(), list(roots)

            def grow():
                while stack:
                    i = stack.pop()
                    if i in ph:
                        continue
                    ph.add(i)
                    stack.extend(kids(i))
            grow()
            # Cartographer's injected content (mods\maps\carto_shared.map) reaches host shared slots BY INDEX that no stock-closure tag
            # needs, e.g. the reindeer ghost shader -> bitm shaders\default_bitmaps\bitmaps\gray_50_percent (#10010).  When the map has a
            # LOCAL tag of the same name nothing links to the shared one, so the slot stayed empty (katabatic / outpost / rain / sweet /
            # stone_walls: invariants spawn.carto_inject).  The slot is listed with shared.map's own group + datum; it does not touch the
            # local tag (names resolve to the local entries first: Plan.remap_name).  Same exceptions as invariants.CARTO_STOCK_MISSES.
            if self.linking and self.opts.get('carto_shared', True):
                try:
                    from . import invariants as INV
                    cp = INV.carto_shared_path(self.donors.maps_dir)
                    added = []
                    if cp:
                        for g, nm, idx, _via in INV.carto_injection_misses(cp):
                            if (g, (nm or '').lower()) in INV.CARTO_STOCK_MISSES or idx in ph:
                                continue
                            t = sh.tags[idx] if idx < len(sh.tags) else None
                            if t is None or t.is_empty or not t.addr or t.group != g:
                                continue
                            added.append(t.name)
                            stack.append(idx)
                        grow()
                    plan.stats['carto_slots_added'] = len(added)
                    if added:
                        plan.notes['carto_slots_added'] = added[:50]
                except Exception as ex:                            # noqa: BLE001  (carto_shared.map unreadable: the invariants gate still reports)
                    self.warn('carto-shared', '%s: %s' % (type(ex).__name__, ex))
        plan.shared_placeholders = [VistaTag(i, sh.tags[i].group, sh.tags[i].datum, 0, 0, sh.tags[i].name) for i in sorted(ph)]
        plan.stats['placeholders'] = len(ph)


def build_plan(src, mcc_shared, vista_refs, opts=None):
    """Plan the conversion of the MCC map `src` (MccCache).

    mcc_shared: {'shared': MccCache|None, ...} (decompressed stock MCC shared maps; only 'shared' is used)
    vista_refs: VistaRefs-like (.shared VistaCache of shared.map, .maps_dir, .open(name))
    opts (all optional):
        self_contained  bool  no links (single-file map); shader family becomes donor copies, matg stays linked only for MP
        map_type        int   override the scenario type of the input (1 = multiplayer)
        matg            'link' (shared Vista matg; the planner's own default) | 'convert' (local matg) | 'auto' (local only if the
                              input's matg differs from the stock MCC shared matg) | 'hud' (convert.py's DEFAULT:
                              local only if the HUD text under matg -> hudg -> unic is customised; notes['hud'])
        placeholders    'closure' (default) | 'all'
        common_extras   bool  add the 11 shared tags every stock MP map lists though nothing references them (default True)
        compare_payload bool  compare bitmap pixels / geometry blobs / sound data when deciding 'unmodified' (default True)
        taint           bool  (default True) a tag that references a MODIFIED tag is converted too (otherwise linking it to the
                              Vista shared.map would silently drop the modification)
        taint_ignore    iterable of (group, name)  modified tags that do NOT taint their dependents (default DEFAULT_TAINT_IGNORE =
                              the bitm 'rasterizer' + backslash + 'linear_corner_fade', a kit-vs-shipped resolution difference);
                              () = no exception
        strict_link     bool  convert tags whose Vista twin cannot be verified against the MCC shared.map
        pull_mcc_only   bool  (default True) an MCC shared.map tag that the Vista shared.map lacks is pulled into the map
                              (converted + its closure); False = references to it become null
        keep_matg_closure bool  keep tags that are only referenced through the (replaced) globals tag
        shader_twin_closure bool  also plan the Vista stem of a converted shader's donor twin (default True)
        dat_dir         str   folder with textures.dat / sounds_*.dat of the MCC install (also read from mcc_dat_dir, which h2conv.convert always sets)
    """
    return _Builder(src, mcc_shared, vista_refs, opts).run()


# ======================================================================================================
# output size estimate
# ======================================================================================================
def _estimate_output(plan, count_sounds=True):
    from . import raw_mcc as R
    b = plan._builder
    est = collections.Counter()
    by_group = collections.Counter()
    mm_cache = {}

    def mapobj(cache):
        m = mm_cache.get(cache.path)
        if m is None:
            m = mm_cache[cache.path] = R.MccMap(cache.path)
        return m

    for e in plan.entries:
        if e.action == 'donor':
            vt = b.donors.tag(*e.donor)
            est['tags'] += vt.size
            by_group[e.group] += vt.size
            continue
        if e.src_map is None:
            continue
        cache = b.cache_of((e.src_map, e.src_index))
        tag = cache.tags[e.src_index]
        est['tags'] += tag.size
        by_group[e.group] += tag.size
        g = e.group
        try:
            if g == 'bitm' and tag.addr:
                data = cache.read_at_addr(tag.addr, 0x50)
                cnt, ptr = struct.unpack_from('<iI', data, 0x44)
                if cnt > 0 and ptr:
                    blob = cache.read_at_addr(ptr, cnt * R.BITMAP_ELEMENT_SIZE)
                    for i in range(cnt):
                        sz = struct.unpack_from('<I', blob, i * R.BITMAP_ELEMENT_SIZE + 0x50)[0]
                        est['bitmaps'] += sz
                        by_group['bitm(pixels)'] += sz
            elif g in ('mode', 'sbsp', 'ltmp', 'DECR', 'PRTM', 'weat', 'dc*s') and tag.addr:
                lay = b.lay_m.get(g)
                base = cache.read_at_addr(tag.addr, lay.base_size or lay.computed_size)
                for site_g, path, info_off in _GEOM_SITES:
                    if site_g != g:
                        continue
                    stack = [(base, 0)]
                    while stack:
                        blob, depth = stack.pop()
                        if depth == len(path):
                            bo, bs = struct.unpack_from('<II', blob, info_off)
                            if bo not in (0, NULL) and bs:
                                est['geometry'] += bs
                                by_group['geometry(' + g + ')'] += bs
                            continue
                        boff, esz = path[depth]
                        cnt, ptr = struct.unpack_from('<iI', blob, boff)
                        if cnt > 0 and ptr:
                            bl = cache.read_at_addr(ptr, cnt * esz)
                            for k in range(cnt):
                                stack.append((bl[k * esz:(k + 1) * esz], depth + 1))
            elif g == 'snd!' and count_sounds and e.src_map == SRC:
                sb = _sound_bytes(mapobj(cache), tag)
                est['sounds'] += sb
                by_group['snd!(audio)'] += sb
        except Exception:                            # an estimate must never abort a plan report
            est['errors'] += 1
    est['total'] = est['tags'] + est['bitmaps'] + est['geometry'] + est['sounds']
    est['by_group'] = dict(by_group)
    return dict(est)


def _sound_bytes(mm, tag):
    """Vista bytes of one snd!'s audio: Xbox ADPCM / WMA chunks are copied, Opus is re-encoded to Xbox ADPCM at 44.1 kHz."""
    g = mm.sound_gestalt()
    o = mm.a2o(tag.addr)
    info = bytes(mm.m[o:o + 0x1C])
    codec = info[3]
    pri, prc = struct.unpack_from('<h', info, 8)[0], info[0xA]
    kind = g.codec_kind(codec) if codec < len(g.codecs) else 'adpcm_mono'
    total = 0
    for r in range(pri, pri + prc):
        pr = g.pitch_range(r)
        for pi in range(pr['first_permutation'], pr['first_permutation'] + pr['permutation_count']):
            chunks = g.permutation_chunks(pi)
            if kind.startswith('opus'):
                pcm = sum(c.uncompressed_size for c in chunks)
                total += int(pcm * (44100.0 / 48000.0) * (36.0 / 128.0))
            else:
                total += sum(c.size for c in chunks)
    return total


# ======================================================================================================
# helpers of the tests and of the analysis scripts
# ======================================================================================================
def local_set(plan):
    return {(e.group, e.name.lower()) for e in plan.entries}


def vista_local_set(vcache):
    return {(t.group, t.name.lower()) for t in vcache.tags if not t.is_empty and t.index < LOCAL_LIMIT}

