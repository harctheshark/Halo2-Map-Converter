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
    """The decompressed stock MCC shared.map (<work folder>/mcc_decompressed, or inflated from the MCC install); None if unavailable."""
    try:
        from . import paths as _paths
        from .mcc_cache import MccCache, DECOMP_DIR
        for d in (DECOMP_DIR, _paths.mcc_maps_dir(required=False)):
            if not d:
                continue
            p = os.path.join(d, 'shared.map')
            if os.path.isfile(p):
                return MccCache(p)
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
                 'reason', 'twin', 'src_size', 'parent', 'redirect_key')

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
        return {
            'summary': self.summary(), 'stats': dict(self.stats), 'by_action': {a: dict(c) for a, c in self.counts().items()},
            'reasons': dict(reasons.most_common()),
            'modified_kept': modified[:max_items], 'modified_kept_count': len(modified),
            'modified_but_dropped': lost[:max_items], 'modified_but_dropped_count': len(lost),
            'tainted_count': len(self.tainted),
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

    def __init__(self, src, shared, payload=True, dat_dir=None):
        self.a = src
        self.b = shared
        self.lay = layouts('mcc')
        self.payload = payload
        self.dat_dir = dat_dir
        self._dats = None
        self._maps = {}
        self._memo = {}
        self._all = False

    # ---- helpers ------------------------------------------------------------------------------------
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
            self._cmp(na, nb, self.a, self.b, GROUP_RULES.get(ta.group, {}), why)
        finally:
            self._all = False
        return why[:limit]

    # ---- payloads ------------------------------------------------------------------------------------------
    def _dat(self):
        if self._dats is None:
            from . import raw_mcc as R
            self._dats = R.DatStore(self.dat_dir)
        return self._dats

    def _bitmap_hashes(self, cache, tag):
        from . import raw_mcc as R
        if not tag.addr:
            return []
        external = bool(cache.header['flags'] & R.HDR_FLAG_EXTERNAL_DAT)
        buf = self._dat().get(R.TEXTURES_DAT) if external else cache.mm
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
            m = R.MccMap(cache.path, dat_dir=self.dat_dir, dats=self._dat())
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
        rules = GROUP_RULES.get(g, {})
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
            mode = opts.get('matg', 'link')
            vt = self.vsh_name.get(nk) or next((t for t in self.vsh.tags if not t.is_empty and t.group == 'matg' and t.addr), None)
            if mode == 'auto':           # local globals only when the input's globals differ from the stock MCC ones
                mode = 'convert' if self.plan.notes.get('matg', ('',))[0] == 'modified' else 'link'
            elif mode == 'hud':          # DEFAULT: local globals only when the HUD text under matg is customised (see prepass)
                mode = 'convert' if self.plan.notes.get('hud', ('',))[0] == 'custom' else 'link'
            if mode == 'link' and self.is_mp and vt is not None:
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
        return self._register(self._new(key, tag, 'drop', why + ': dropped, references become null'), key)

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
    def prepass(self):
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
        for t in cands:
            key = (SRC, t.index)
            cset.add(key)
            mt = self.msh.tag_by_name(t.group, t.name)
            if mt is None or not mt.addr:
                self.status[key] = ('unverifiable', 'no stock MCC shared twin')
                self.plan.stats['status:unverifiable'] += 1
                continue
            self.status[key] = cmp_.compare(t, mt)
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
            elif e.action == 'donor':
                for f2, i2, off in self.donors.refs(*e.donor):
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
        # decisions for every local tag of the input map, in source order
        local_keys = [(SRC, t.index) for t in src.tags if not t.is_null and not t.external and t.name]
        for k in local_keys:
            self.ensure_key(k)
        roots = self.roots()
        self.expand(roots)
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
            while stack:
                i = stack.pop()
                if i in ph:
                    continue
                ph.add(i)
                stack.extend(kids(i))
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

