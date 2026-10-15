# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""REVERSE planner: which tags of a Halo 2 VISTA map become tags of a SELF-CONTAINED Halo 2 MCC map.

    env  = RevEnv()                                           # Vista maps folder + MCC stock maps + the H2EK maps
    plan = build_rev_plan(r'...\\maps\\headlong.map', env, opts)  # -> RevPlan

Actions per RevEntry
    convert    a local MCC tag transcoded from a Vista tag (src_map 'src' = the input map, 'shared' = Vista shared.map)
    mcc_donor  no transcoding: MCC's OWN tag of the same (group, name) is copied (MccCache + tag index in entry.donor).
               MANDATORY for the shader code groups vrtx / spas / stem (MCC runs DXBC, Vista tags carry D3D9 bytecode);
               default for tags that came from Vista's shared.map when MCC's shared.map has the same name (opts shared_policy)
    drop       no output tag: Vista scenario-resource tags (*cen *eap ... hsc* - MCC has none, its scnr keeps the same inline
               blocks with null references), shader code without an MCC twin (NO_MCC_DONOR), unresolved references

Output topology (24/24 maps built with the MCC H2EK): ALL tags local, no null slots (tag_count ==
number of tags), matg #0, sncl 'sound_remastered\\sound_classes' #1, sncl 'sound\\sound_classes' #2, spk! #3, scnr #4, the map's
single merged ugh! LAST, salt of tag k = 0x8000 + (0xE175 - 0x8000 + k + (k >= 4)) % 0x7FFF, header 0x2EC (sound gestalt datum) = -1,
meta mask 0.  Tags are identified by (group, lower-case name): a Vista local tag overrides any tag of the same name that came
from Vista's shared.map or from an MCC donor.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:                                                   # pragma: no cover
    pass

import collections
import glob
import json
import os
import pickle
import struct
import sys
import time

from .. import plugins as P
from .. import tagtree as T
from .. import paths as _paths
from ..mcc_cache import MccCache
from ..planner import RefScanner                                     # read-only reuse of the forward tagref scanner
from ..vista_cache import VistaCache, FIRST_SHARED_TAG_INSTANCE_INDEX
from . import shader_port as SPORT                                  # faithful port of the Vista shader code MCC lacks
from . import custom_stems as CST                                   # stems carried under a STOCK name with custom content
from . import shader_tags as ST                                     # SEMANTIC_WATER_CHAINS: the water chains MCC re-authored as other shaders

NULL = 0xFFFFFFFF
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = os.path.join(_paths.work_dir(), 'rev_planner')


def __getattr__(name):
    """VISTA_MAPS / H2EK_MAPS / H2EK_TAGS: folders on one PC, now resolved by h2conv.paths; kept as attributes for the developer tools
    (H2EK_MAPS / H2EK_TAGS are None unless an H2EK folder is configured: --h2ek-maps, H2CONV_H2EK_MAPS, settings.json "h2ek_maps_dir")."""
    if name == 'VISTA_MAPS':
        return _paths.vista_maps_dir()
    if name == 'H2EK_MAPS':
        return _paths.h2ek_maps_dir()
    if name == 'H2EK_TAGS':
        return _paths.h2ek_tags_dir()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


# Vista-only scenario resource groups (exist in 0 of 45 MCC maps; Vista: campaign maps + 16 of 25 MP maps).  The cache scnr of both
# games has the same inline blocks (Squads, Zones, Scripts ... identical counts) and a 'Scenario Resources' block whose references MCC
# leaves NULL (group word kept, datum 0xFFFFFFFF; 01b_spacestation/03a_oldmombasa/ascension, stock and H2EK) -> drop + null.
RESOURCE_GROUPS = ('*cen', '*eap', '*ehi', '*igh', '*ipd', '*qip', '*rea', '*sce', '/**/', 'ai**', 'cin*', 'clu*', 'dc*s', 'dec*',
                   'dgr*', 'hsc*', 'sslt', 'trg*')
DONOR_REQUIRED = ('vrtx', 'spas', 'stem')                           # shader code: MCC runs DXBC, Vista tags hold D3D9 bytecode
SHARED_KINDS = ('shared', 'single_player_shared', 'mainmenu')       # MCC databases that mirror Vista's three shared maps
GESTALT_NAME = "i've got a lovely bunch of coconuts"                # name of the single ugh! of every H2EK-built map
GESTALT_KEY = ('ugh!', GESTALT_NAME.lower())
PREFIX_TAGS = (('matg', 'globals\\globals'), ('sncl', 'sound_remastered\\sound_classes'),
               ('sncl', 'sound\\sound_classes'), ('spk!', 'sound\\combat_dialogue_constants'))
MATG_KEY = ('matg', 'globals\\globals')
INVALID_SHADER = ('shad', 'shaders\\invalid')
AUTO_STEM_MIN_SCORE = 0.5                                           # suggest_stem_fallbacks() score needed by stem_fallback='auto'
MAX_TAGS = 16999                                                   # the engine loads shared.map's table when tag_count >= 17000 (rev_container.md s1)
SALT_BASE = 0xE175                                                  # salt of tag 0 in all 24 H2EK maps (01b_spacestation wraps at tag 7816)
BY_DESIGN_NULL = ('scenario resource', 'NO_MCC_DONOR', 'stem without MCC', 'UNPORTABLE')

DEFAULT_OPTS = {
    'shared_policy': 'mcc',           # tags that came through Vista's shared.map: 'mcc' = MCC's own tag of that name when it exists,
                                      # else convert the Vista one; 'vista' = always convert the Vista shared tag
    'matg': 'auto',                   # 'auto' local Vista globals -> convert (MCC's matg is the twin), shared globals -> MCC donor;
                                      # 'mcc' always MCC's globals; 'convert' always convert Vista's
    'orphans': 'keep',                # local Vista tags nothing reaches: 'keep' | 'drop'
    'gestalt_reflections': True,      # add the sound_remastered\reflections\* snd! tags the MCC gestalt's Reflections block names (+136 tags,
                                      # +17 MiB); off = the sound handler drops the Reflections entries whose target is not planned
    'remastered_twins': True,         # add sound_remastered\X for every sound\X donor (+467 tags, +34 MiB): MCC marks classic sounds that have
                                      # a twin 'play only in legacy mode' (flag 0x400), so without the twin they are silent in Remastered
                                      # audio mode; the sound handler links the twins through the gestalt's Remastered Sound block
    'placeholders_as_roots': True,    # tags the input lists as shared placeholders that nothing references (stripped authoring data,
                                      # 11 in all 25 stock MP maps) are kept when MCC has a same-name tag
    'shader_port': True,              # FAITHFUL PORT: a Vista stem / pass / vertex shader MCC does not have is rebuilt in the MCC
                                      # format from its compiled Vista code (D3D9 -> HLSL -> DXBC, proven by evaluation); what cannot be ported and
                                      # verified is UNPORTABLE and the shaders that need it go to MCC's shaders\invalid.  False = the previous
                                      # donor-only behaviour (A/B tests only)
    'stem_fallback': None,            # NON-FAITHFUL OPT-IN (default OFF, never used by default): {vista stem name: MCC stem name} or 'auto'
                                      # (the UNVERIFIED suggest_stem_fallbacks() guesses) substitutes, used ONLY for a stem the port cannot handle;
                                      # every use is reported under shader_port.non_faithful
    'shader_twin_fallback': False,    # NON-FAITHFUL OPT-IN (default OFF): a Vista shader whose stem is unportable uses MCC's shader of the same name
                                      # instead of shaders\invalid
    'custom_stem_port': True,         # a stem the input carries under a STOCK name whose content differs from MCC's stem AND from every stock Vista stem
                                      # of that name (fingerprints, h2conv/rev/custom_stems.py) is PORTED under the private name '<name>__<map>' and the
                                      # map's shaders on it are redirected to it; False = MCC's donor stem replaces it (the
                                      # previous behaviour, A/B tests only)
    'water_chain': 'vista',           # the water shader chains MCC re-authored as OTHER shaders (shader_tags.SEMANTIC_WATER_CHAINS: stems water_pool / water_pool_static /
                                      # water_static with their passes and vertex shaders): 'vista' = a shader the input (or Vista's shared.map) authored against them keeps the
                                      # Vista chain, ported faithfully under the private names '<name>__vista'; 'mcc' = MCC's same-name donor
                                      # chain (the previous behaviour; stock-oracle conversions and A/B maps only)
    'sky_depth': 'vista',             # the two sky vertex shaders (shader_tags.SKY_DEPTH_PINNED_VRTX: transparent_generic_reflection_sky / transparent_generic_default_sky): Vista pins their clip
                                      # depth to the far plane (clip z == clip w; the sky is drawn first, depth test off) where MCC's same-name tags write REAL depth and MCC draws the sky
                                      # late with the depth test on.  'vista' = the Vista tag of the map is ported under the SAME name with z = 0 (the reverse-Z far plane; translate_vs
                                      # 'pin'); MCC's sky stems / passes / pixel shaders stay.  If the pin cannot be ported and proven for EVERY classification (or the input carries no such
                                      # tag) MCC's donor is used and the plan warns sky_pin_fallback.  'mcc' = MCC's donor vertex shaders (A/B builds and negative controls only)
    'use_h2ek_donors': True,          # the H2EK maps folder as the last tier for donors - only when one is configured (--h2ek-maps / H2CONV_H2EK_MAPS / settings.json h2ek_maps_dir); never auto-detected
    'use_stock_locals': True,         # local tags of stock MCC campaign / MP maps as donor tier 3
}


class RevPlannerError(Exception):
    pass


def rev_datum(k):
    """Datum of the k-th output tag (H2EK rule: salt counter 0xE175.. with one skipped value after tag 3, wrapping 0xFFFE -> 0x8000)."""
    salt = 0x8000 + ((SALT_BASE - 0x8000) + k + (1 if k >= 4 else 0)) % 0x7FFF
    return (salt << 16) | (k & 0xFFFF)


def _nk(group, name):
    return (group, (name or '').lower())


def synthetic_name(label, t):
    """Name for a Vista tag the map's name table does not name (2 of 517 community maps have a few): a path-like, unique, deterministic one."""
    g = ''.join(ch if ch.isalnum() else '_' for ch in (t.group or 'none'))
    return 'unnamed\\%s\\%s_%05d' % (label, g, t.index)


_LAY = {}


def layouts(game):
    if game not in _LAY:
        _LAY[game] = P.load_layouts(game, corrected=True)
    return _LAY[game]


# ======================================================================================================
# MCC donor sources
# ======================================================================================================
class McDonor(collections.namedtuple('McDonor', 'label index group name datum size')):
    """A tag of an MCC map that carries data.  label: 'shared' | 'single_player_shared' | 'mainmenu' | 'stock:<map>' | 'h2ek:<map>'."""
    __slots__ = ()

    @property
    def kind(self):
        return self.label.split(':')[0]

    @property
    def spec(self):
        return (self.label, self.index)


class MccSources(object):
    """Index of the data-carrying tags of the MCC maps usable as donors, tiered:
         0 shared.map   1 single_player_shared.map   2 mainmenu.map   3 the other stock maps ('stock:')   4 the H2EK maps ('h2ek:')
    find(group, name) returns the first tier that has the (group, name); `prefer` = a label tried first."""

    ROWS_VERSION = 2

    def __init__(self, stock_dir=None, h2ek_dir=None, use_h2ek=True, use_stock_locals=True, cache_file=None, quiet=True):
        if stock_dir is None:
            # ALWAYS the installed MCC maps folder (--mcc-maps / H2CONV_MCC_MAPS / settings / your Steam library), never the decompression cache:
            # the cache holds only the maps an earlier run happened to open, so indexing it made the donor set depend on the order of earlier
            # runs.  MccCache inflates a compressed map into the cache by itself when it is opened.
            stock_dir = _paths.mcc_maps_dir()
        self.stock_dir = stock_dir
        if h2ek_dir is None and use_h2ek:
            h2ek_dir = _paths.h2ek_maps_dir()                  # opt-in: None unless a folder was configured
        self.h2ek_dir = h2ek_dir if (use_h2ek and h2ek_dir and os.path.isdir(h2ek_dir)) else None
        self.use_stock_locals = use_stock_locals
        self.cache_file = cache_file if cache_file is not None else os.path.join(WORK, 'mcc_index_v%d.pkl' % self.ROWS_VERSION)
        self.quiet = quiet
        self.paths = collections.OrderedDict()
        for lab in SHARED_KINDS:
            p = os.path.join(stock_dir, lab + '.map')
            if os.path.isfile(p):
                self.paths[lab] = p
        if use_stock_locals:
            for p in sorted(glob.glob(os.path.join(stock_dir, '*.map'))):
                b = os.path.splitext(os.path.basename(p))[0]
                if b.lower() not in SHARED_KINDS:
                    self.paths['stock:' + b] = p
        if self.h2ek_dir:
            for p in sorted(glob.glob(os.path.join(self.h2ek_dir, '*.map'))):
                self.paths['h2ek:' + os.path.splitext(os.path.basename(p))[0]] = p
        self._open = {}
        self.by_key = None                  # (group, name_l) -> [McDonor] in tier order
        self.by_group = None
        self.rows = {}
        self.types = {}                     # label -> scenario type

    # ---- caches ------------------------------------------------------------------------------------------------------
    def open(self, label):
        c = self._open.get(label)
        if c is None:
            c = self._open[label] = MccCache(self.paths[label])
        return c

    def close(self):
        for c in self._open.values():
            try:
                c.close()
            except Exception:                                          # noqa: BLE001
                pass
        self._open = {}

    def _sig(self, p):
        st = os.stat(p)
        return (os.path.abspath(p), st.st_size, st.st_mtime_ns)

    def build(self):
        """Build (or load from the pickle cache) the name index."""
        if self.by_key is not None:
            return self
        if 'shared' not in self.paths:
            raise RevPlannerError('the stock MCC shared.map was not found in %s (the MCC maps folder: pass --mcc-maps or set H2CONV_MCC_MAPS)' % self.stock_dir)
        cached = {}
        if self.cache_file and os.path.isfile(self.cache_file):
            try:
                with open(self.cache_file, 'rb') as f:
                    cached = pickle.load(f)
            except Exception:                                          # noqa: BLE001
                cached = {}
        out = {}
        dirty = False
        for lab, p in self.paths.items():
            sig = self._sig(p)
            hit = cached.get(lab)
            if hit is not None and hit[0] == sig:
                out[lab] = hit
                continue
            dirty = True
            c = MccCache(p)
            rows = []
            for t in c.tags:
                if t.is_null or not t.has_data or t.size <= 0:
                    continue
                rows.append((t.index, t.group, t.datum, t.size, t.name or ''))
            typ = c.header['scenario_type']
            c.close()
            out[lab] = (sig, rows, typ)
        if dirty and self.cache_file:
            try:
                os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
                tmp = self.cache_file + '.tmp'
                with open(tmp, 'wb') as f:
                    pickle.dump(out, f, protocol=4)
                os.replace(tmp, self.cache_file)
            except Exception:                                          # noqa: BLE001
                pass
        self.by_key = {}
        self.by_group = collections.defaultdict(list)
        for lab in self.paths:
            sig, rows, typ = out[lab]
            self.types[lab] = typ
            self.rows[lab] = rows
            for idx, g, datum, size, name in rows:
                if not name:
                    continue
                d = McDonor(lab, idx, g, name, datum, size)
                self.by_key.setdefault((g, name.lower()), []).append(d)
                self.by_group[g].append(d)
        return self

    # ---- queries -----------------------------------------------------------------------------------------------------
    def find(self, group, name, prefer=None, kinds=None):
        """First donor holding (group, name) in tier order (optionally limited to `kinds`), `prefer` (a label) wins when it has it."""
        self.build()
        lst = self.by_key.get((group, (name or '').lower()))
        if not lst:
            return None
        if kinds is None and group not in DONOR_REQUIRED:
            # the H2EK maps carry their author's own edits: they are a donor of last resort for shader code only
            kinds = ('shared', 'single_player_shared', 'mainmenu', 'stock')
        if prefer is not None:
            for d in lst:
                if d.label == prefer and (kinds is None or d.kind in kinds):
                    return d
        for d in lst:
            if kinds is None or d.kind in kinds:
                return d
        return None

    def find_all(self, group, name):
        self.build()
        return list(self.by_key.get((group, (name or '').lower()), ()))

    def has_name(self, group, name):
        self.build()
        return (group, (name or '').lower()) in self.by_key

    def tier_of(self, d):
        return {'shared': 0, 'single_player_shared': 1, 'mainmenu': 2, 'stock': 3, 'h2ek': 4}[d.kind]

    def tag(self, d):
        """(MccCache, MccTag) of a donor."""
        c = self.open(d.label)
        return c, c.tags[d.index]


# ======================================================================================================
# environment
# ======================================================================================================
class RevEnv(object):
    """Everything a reverse plan needs besides the input map: the Vista maps folder (shared.map ...), the MCC donor sources, layouts."""

    def __init__(self, vista_maps=None, mcc=None, **mcc_kw):
        self.vista_maps = vista_maps or _paths.vista_maps_dir()
        self.mcc = mcc if mcc is not None else MccSources(**mcc_kw)
        self._vc = {}
        self.lay_vista = layouts('vista')
        self.lay_mcc = layouts('mcc')
        self.scan_vista = RefScanner(self.lay_vista)
        self.scan_mcc = RefScanner(self.lay_mcc)
        self._shared_index = None
        self._refl = None

    def vista(self, name):
        """VistaCache of a map in the Vista maps folder ('shared', 'shared.map', ...)."""
        if not name.lower().endswith('.map'):
            name += '.map'
        c = self._vc.get(name.lower())
        if c is None:
            p = os.path.join(self.vista_maps, name)
            if not os.path.isfile(p):
                raise RevPlannerError('Vista map %s not found in %s (the reverse planner needs the stock Vista shared.map there)' % (name, self.vista_maps))
            c = self._vc[name.lower()] = VistaCache(p)
        return c

    def get(self, key):
        """Same lookup as the reverse RevCtx.vista_refs.get: 'shared' | 'mainmenu' | 'single_player_shared'."""
        return self.vista(key)

    @property
    def shared(self):
        return self.vista('shared.map')

    @property
    def mainmenu(self):
        return self.vista('mainmenu.map')

    @property
    def sp_shared(self):
        return self.vista('single_player_shared.map')

    def shared_index(self):
        """{(group, name_l): VistaTag} of the tags that carry data in Vista's shared.map."""
        if self._shared_index is None:
            d = {}
            for t in self.shared.tags:
                if not t.is_empty and t.addr and t.name:
                    d.setdefault(_nk(t.group, t.name), t)
            self._shared_index = d
        return self._shared_index

    def shared_ugh_index(self):
        for t in self.shared.tags:
            if not t.is_empty and t.addr and t.group == 'ugh!':
                return t.index
        return None

    def gestalt_reflection_names(self):
        """Names of the snd! tags the MCC shared gestalt's Reflections block references (cached)."""
        if self._refl is None:
            names = []
            d = self.mcc.find('ugh!', GESTALT_NAME, kinds=('shared',))
            if d is not None:
                c, t = self.mcc.tag(d)
                node = T.decode_tag(lambda a, n: c.read_at_addr(a, n), self.lay_mcc['ugh!'], t.addr, 'ugh!')
                for n in node.iter_nodes():
                    if n.path != '/Reflections':
                        continue
                    for f in n.fields:
                        if f.type == 'tagref' and f.size == 8:
                            g, dat = struct.unpack_from('<II', n.data, f.offset)
                            x = c.tag_by_datum(dat)
                            if x is not None and x.name:
                                names.append(x.name)
            self._refl = names
        return self._refl

    def close(self):
        self.mcc.close()
        for c in self._vc.values():
            try:
                c.close()
            except Exception:                                          # noqa: BLE001
                pass
        self._vc = {}


# ======================================================================================================
# plan objects
# ======================================================================================================
class RevEntry(object):
    """One output decision, identified by key = (group, lower-case name)."""
    __slots__ = ('key', 'group', 'name', 'action', 'src_map', 'src_index', 'donor', 'twin', 'out_index', 'out_datum', 'reason',
                 'redirect', 'src_size', 'children', 'merge_sources', 'origin', 'refs', 'port', 'private_of')

    def __init__(self, group, name, action, reason=''):
        self.key = _nk(group, name)
        self.group = group
        self.name = name
        self.action = action
        self.src_map = None             # 'src' | 'shared' | ... : the Vista map a 'convert' entry is transcoded from
        self.src_index = None           # tag index in that map
        self.donor = None               # McDonor for 'mcc_donor'
        self.twin = None                # McDonor of the MCC tag with this name, for 'convert' entries (a template for MCC-only fields)
        self.out_index = None
        self.out_datum = NULL
        self.reason = reason
        self.redirect = None            # RevEntry that references to a DROPPED tag go to (None = null)
        self.src_size = 0
        self.children = []              # RevEntry list in reference order (graph edges, unique)
        self.merge_sources = None       # ugh! only: [{'side': 'vista'|'mcc', 'map'|'label': ..., 'index': ...}]
        self.origin = None              # how the closure reached it ('root', 'vista-local', 'vista-shared', 'mcc-ref', ...)
        self.refs = (0, 0, 0)           # (references scanned, dangling, to dropped)
        self.port = None                # shader_port.PortDecision of a ported ('convert') or UNPORTABLE ('drop') stem / spas / vrtx
        self.private_of = None          # the STOCK / MCC name this entry is the private port of: stem = the input's custom stem '<name>' or a Vista-authored water stem; spas / vrtx =
                                        # the Vista pass / vertex shader whose MCC namesake is another shader (private water chain, private vertex shader); else None

    def __repr__(self):
        return '<RevEntry %s %s %r out=%s>' % (self.action, self.group, self.name, self.out_index)


class RevPlan(object):
    def __init__(self, vista, env, opts):
        self.vista = vista              # input VistaCache
        self.env = env
        self.opts = opts
        self.entries = []               # kept entries (convert + mcc_donor) in output index order
        self.all_entries = []           # every decision in creation order
        self.by_name = {}               # (group, name_l) -> RevEntry
        self.by_src = {}                # (src_map, vista_index) -> RevEntry   (convert entries)
        self.by_donor = {}              # (label, mcc_index) -> RevEntry       (mcc_donor entries)
        self.scenario = None
        self.globals_entry = None
        self.gestalt = None
        self.map_type = vista.header.type
        self.stats = collections.Counter()
        self.warnings = []
        self.notes = {}
        self.elapsed = 0.0
        self.private_stems = {}         # (src_map, vista tag index) -> RevEntry: the PRIVATE port of a stem the input carries under a stock name (see entry_of_datum)

    # ---- helpers -----------------------------------------------------------------------------------------------------
    @staticmethod
    def _out(entry):
        n = 0
        while entry is not None and n < 8:
            if entry.action in ('convert', 'mcc_donor'):
                return entry.out_datum
            entry = entry.redirect
            n += 1
        return NULL

    def warn(self, code, msg):
        self.stats['warn:' + code] += 1
        if len(self.warnings) < 2000:
            self.warnings.append((code, msg))

    def close(self):
        """Close the input VistaCache when build_rev_plan opened it from a path (the env's shared caches stay open)."""
        if getattr(self, '_own_cache', False):
            try:
                self.vista.close()
            except Exception:                                          # noqa: BLE001
                pass
            self._own_cache = False

    def vista_cache(self, src_map):
        """VistaCache named by an entry's src_map / ctx.cur_src ('src' = the input map)."""
        if src_map in (None, 'src'):
            return self.vista
        return self.env.vista(src_map)

    def resolve_vista(self, src_map, datum):
        """(src_map', VistaTag) the datum of a tag of Vista map `src_map` names; a shared placeholder (index >= 10000, no data) is
        resolved to Vista's shared.map.  None for null / dangling references."""
        if datum in (NULL, 0, None):
            return None
        cache = self.vista_cache(src_map)
        i = datum & 0xFFFF
        if i >= len(cache.tags):
            return None
        t = cache.tags[i]
        if t.datum != datum or t.is_empty:
            return None
        if t.addr:
            return (src_map or 'src', t)
        if i >= FIRST_SHARED_TAG_INSTANCE_INDEX:
            sh = self.env.shared
            if i < len(sh.tags) and sh.tags[i].datum == datum and sh.tags[i].addr:
                return ('shared', sh.tags[i])
        return None

    @staticmethod
    def _key_for(group, name):
        return GESTALT_KEY if group == 'ugh!' else _nk(group, name)

    # ---- datum remap (the API the reverse transcoder codes against) ----------------------------------------------------
    def entry_of_datum(self, src_map, vista_datum):
        r = self.resolve_vista(src_map, vista_datum)
        if r is None:
            return None
        t = r[1]
        if self.private_stems and t.group == 'stem':
            pe = self.private_stems.get((r[0], t.index))              # the input's custom stem under a stock name -> its private port, not MCC's namesake
            if pe is not None:
                return pe
        return self.by_name.get(self._key_for(t.group, t.name or synthetic_name(r[0], t)))

    def remap_datum(self, src_map, vista_datum):
        """MCC output datum for a Vista datum read from a tag of Vista map `src_map` ('src' = the input map, 'shared', 'mainmenu',
        'single_player_shared'); 0xFFFFFFFF for null, dangling, dropped-without-redirect."""
        e = self.entry_of_datum(src_map, vista_datum)
        return self._out(e) if e is not None else NULL

    def remap_vista_datum(self, vista_cache, vista_datum):
        """Same, with the VistaCache instead of the map label."""
        if vista_cache is self.vista:
            return self.remap_datum('src', vista_datum)
        label = os.path.splitext(os.path.basename(vista_cache.path))[0]
        return self.remap_datum(label, vista_datum)

    def remap_mcc_datum(self, mcc_cache, mcc_datum):
        """Output datum for an MCC datum read from a donor tag of `mcc_cache` (external tags of stock MP maps resolve by name)."""
        if mcc_datum in (NULL, 0, None):
            return NULL
        t = mcc_cache.tag_by_datum(mcc_datum)
        if t is None or not t.name:
            return NULL
        return self.remap_name(t.group, t.name)

    def remap_name(self, group, name):
        """Output datum of the planned tag with this (group, name); NULL if the plan has none."""
        e = self.by_name.get(self._key_for(group, name))
        return self._out(e) if e is not None else NULL

    def entry_of_name(self, group, name):
        return self.by_name.get(self._key_for(group, name))

    # ---- sources -----------------------------------------------------------------------------------------------------
    def source_of(self, entry):
        """(VistaCache, VistaTag) a 'convert' entry is transcoded from, else None."""
        if entry.action != 'convert' or entry.src_map is None:
            return None
        c = self.vista_cache(entry.src_map)
        return c, c.tags[entry.src_index]

    def donor_source_of(self, entry):
        """(MccCache, MccTag) of an 'mcc_donor' entry, else None."""
        if entry.donor is None:
            return None
        return self.env.mcc.tag(entry.donor)

    def twin_source_of(self, entry):
        """(MccCache, MccTag) of the MCC tag with the same name (convert entries), else None."""
        if entry.twin is None:
            return None
        return self.env.mcc.tag(entry.twin)

    # ---- header / tables for the container writer -------------------------------------------------------------------------
    @property
    def tag_count(self):
        return len(self.entries)

    @property
    def scenario_datum(self):
        return self.scenario.out_datum if self.scenario else NULL

    @property
    def globals_datum(self):
        return self.globals_entry.out_datum if self.globals_entry else NULL

    @property
    def sound_gestalt_header_datum(self):
        """Header 0x2EC.  Every H2EK-built map (24/24, MP and solo) stores 0xFFFFFFFF: ONE merged gestalt, found through matg."""
        return NULL

    def header_values(self):
        return {'scenario_type': self.map_type, 'scenario_datum': self.scenario_datum, 'globals_datum': self.globals_datum,
                'sound_gestalt_datum': NULL, 'tag_count': self.tag_count, 'meta_offset_mask': 0,
                'gestalt_datum': self.gestalt.out_datum if self.gestalt else NULL}

    def instance_table(self):
        """[(group, datum, name)] in output index order (no null slots, no placeholders)."""
        return [(e.group, e.out_datum, e.name) for e in self.entries]

    def tag_names(self):
        return [e.name for e in self.entries]

    # ---- validation --------------------------------------------------------------------------------------------------
    def audit(self):
        """Every reference of every kept entry (recorded while the closure was built) must resolve to a kept entry or be a
        deliberate null.  -> (references, [problems]); problem = (kind, from group, from name, to group, to name, why)."""
        n = 0
        bad = []
        for e in self.entries:
            for c in e.children:
                n += 1
                if self._out(c) == NULL:
                    by_design = c.action == 'drop' and any(k in c.reason for k in BY_DESIGN_NULL)
                    bad.append(('null-by-design' if by_design else 'null', e.group, e.name, c.group, c.name, c.reason))
        return n, bad

    def estimate_output(self, count_sounds=True, lang=0):
        """Rough size in bytes of the self-contained MCC map: {'tags', 'bitmaps', 'geometry', 'sounds', 'total', 'donor_*', 'vista_*'}.
        Donor tags: the MCC bitmap blobs (element +0x50), geometry blocks and sound chunks (language list `lang`) are copied as they are.
        Converted tags: the Vista map's own raw regions (raw_sounds / geometry_blocks / bitmap_pixels - they hold the LOCAL content only;
        MCC chunk-deflates bitmaps with a 1 KiB window, so bitmaps come out a few percent larger).  Tags converted from Vista's
        shared.map are not counted (few).  Checked against the H2EK headlong (336,633,088 B)."""
        from .. import raw_mcc as R
        from ..planner import _GEOM_SITES
        est = collections.Counter()
        by_group = collections.Counter()
        maps = {}
        env = self.env

        def mapobj(label):
            m = maps.get(label)
            if m is None:
                m = maps[label] = R.MccMap(env.mcc.paths[label])
            return m

        secs = dict((n, sz) for n, o, sz in self.vista.sections())
        est['vista_sounds'] = secs.get('raw_sounds', 0)
        est['vista_geometry'] = secs.get('geometry_blocks', 0)
        est['vista_bitmaps'] = secs.get('bitmap_pixels', 0)
        for e in self.entries:
            est['tags'] += e.src_size
            by_group[e.group] += e.src_size
            if e.action != 'mcc_donor':
                continue
            cache, tag = env.mcc.tag(e.donor)
            g = e.group
            try:
                if g == 'bitm':
                    data = cache.read_at_addr(tag.addr, 0x50)
                    cnt, ptr = struct.unpack_from('<iI', data, 0x44)
                    if cnt > 0 and ptr:
                        blob = cache.read_at_addr(ptr, cnt * R.BITMAP_ELEMENT_SIZE)
                        for i in range(cnt):
                            est['donor_bitmaps'] += struct.unpack_from('<I', blob, i * R.BITMAP_ELEMENT_SIZE + 0x50)[0]
                elif g in ('mode', 'sbsp', 'ltmp', 'DECR', 'PRTM', 'weat', 'dc*s'):
                    lay = env.lay_mcc.get(g)
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
                                    est['donor_geometry'] += bs
                                continue
                            boff, esz = path[depth]
                            cnt, ptr = struct.unpack_from('<iI', blob, boff)
                            if cnt > 0 and ptr:
                                bl = cache.read_at_addr(ptr, cnt * esz)
                                for k in range(cnt):
                                    stack.append((bl[k * esz:(k + 1) * esz], depth + 1))
                elif g == 'snd!' and count_sounds:
                    mm = mapobj(e.donor.label)
                    gest = mm.sound_gestalt()
                    o = mm.a2o(tag.addr)
                    info = bytes(mm.m[o:o + 0x1C])
                    pri, prc = struct.unpack_from('<h', info, 8)[0], info[0xA]
                    total = 0
                    for r in range(pri, pri + prc):
                        pr = gest.pitch_range(r)
                        for pi in range(pr['first_permutation'], pr['first_permutation'] + pr['permutation_count']):
                            total += sum(c.size for c in gest.permutation_chunks(pi, lang))
                    est['donor_sounds'] += total
            except Exception:                                          # noqa: BLE001  (an estimate never aborts)
                est['errors'] += 1
        est['bitmaps'] = est['donor_bitmaps'] + est['vista_bitmaps']
        est['geometry'] = est['donor_geometry'] + est['vista_geometry']
        est['sounds'] = est['donor_sounds'] + est['vista_sounds']
        est['total'] = est['tags'] + est['bitmaps'] + est['geometry'] + est['sounds']
        est['by_group'] = dict(by_group)
        return dict(est)

    def counts(self):
        out = collections.defaultdict(collections.Counter)
        for e in self.all_entries:
            out[e.action][e.group] += 1
        return out

    def summary(self):
        c = collections.Counter(e.action for e in self.all_entries)
        src = collections.Counter(e.src_map for e in self.entries if e.action == 'convert')
        return {'tags': len(self.entries), 'convert': c['convert'], 'mcc_donor': c['mcc_donor'], 'drop': c['drop'],
                'convert_from': dict(src), 'map_type': self.map_type, 'name': self.vista.header.name,
                'scenario_datum': '0x%08X' % self.scenario_datum, 'globals_datum': '0x%08X' % self.globals_datum,
                'elapsed_s': round(self.elapsed, 1)}

    def report(self, max_items=100):
        reasons = collections.Counter()
        for e in self.all_entries:
            reasons['%s | %s' % (e.action, e.reason.split(':')[0][:90])] += 1
        shader = {}
        for g in DONOR_REQUIRED:
            es = [e for e in self.all_entries if e.group == g]
            shader[g] = {'names': len(es), 'donor': sum(1 for e in es if e.action == 'mcc_donor'),
                         'ported': sorted(e.name for e in es if e.action == 'convert'),
                         'unportable': sorted(e.name for e in es if e.action == 'drop' and e.reason.startswith('UNPORTABLE'))[:max_items],
                         'no_donor': sorted(e.name for e in es if e.action == 'drop' and not e.reason.startswith('UNPORTABLE'))[:max_items],
                         'no_donor_count': sum(1 for e in es if e.action == 'drop' and not e.reason.startswith('UNPORTABLE')),
                         'by_tier': dict(collections.Counter(e.donor.kind for e in es if e.donor is not None))}
        shadow = self.notes.get('shadowed', [])
        n, bad = self.audit()
        by_design = [b for b in bad if b[0] == 'null-by-design']
        real = [b for b in bad if b[0] == 'null']
        return {'summary': self.summary(), 'stats': dict(self.stats), 'by_action': {a: dict(c) for a, c in self.counts().items()},
                'reasons': dict(reasons.most_common()), 'shader_code': shader,
                'resources_dropped': dict(collections.Counter(e.group for e in self.all_entries if e.group in RESOURCE_GROUPS)),
                'shadowed_shared_count': len(shadow), 'shadowed_shared': shadow[:max_items],
                'twins': sum(1 for e in self.entries if e.twin is not None and e.action == 'convert'),
                'shaders': {'dropped_to_invalid': sum(1 for e in self.all_entries if e.group == 'shad' and e.action == 'drop'),
                            'to_mcc_twin': self.stats.get('shad_to_mcc_twin', 0), 'stem_fallback_refs': self.stats.get('stem_fallback', 0)},
                'shader_port': self.notes.get('shader_port'),
                'orphans': self.notes.get('orphans', {}), 'refs': n, 'null_refs_by_design': len(by_design), 'null_refs_unexplained': len(real),
                'unexplained_examples': real[:20], 'gestalt_sources': self.gestalt.merge_sources if self.gestalt else None,
                'bytes_vista': sum(e.src_size for e in self.entries if e.action == 'convert'),
                'bytes_mcc_donor': sum(e.src_size for e in self.entries if e.action == 'mcc_donor'),
                'warnings': self.warnings[:max_items]}


# ======================================================================================================
# builder
# ======================================================================================================
class _Builder(object):
    def __init__(self, vista, env, opts):
        self.vista = vista
        self.env = env
        self.opts = opts
        self.mcc = env.mcc.build()
        self.plan = RevPlan(vista, env, opts)
        self.by_name = self.plan.by_name
        self.queue = collections.deque()
        self.local = {}                 # (group, name_l) -> VistaTag : data-carrying tags of the input map
        self.local_names = {}           # vista index -> synthetic key name for unnamed tags
        self.map_type = vista.header.type
        self.shared_ix = env.shared_index()
        self.invalid_entry = None
        sf = opts.get('stem_fallback') or {}
        self.stem_auto = isinstance(sf, str) and sf.lower() == 'auto'     # opts['stem_fallback'] = 'auto': suggest_stem_fallbacks() on demand
        if isinstance(sf, str):
            sf = {}
        self.stem_fallback = dict((str(a).lower(), b) for a, b in sf.items())
        # the faithful port: decides, for every stem / pass / vertex shader MCC does not have, port or UNPORTABLE
        self.port = SPORT.PortPolicy(env, self.plan.resolve_vista, self.plan.vista_cache, opts) if opts.get('shader_port', True) else None
        self.unportable_shaders = []     # [{'shader', 'stem', 'reason'}] shaders sent to shaders\\invalid because their stem cannot be ported
        self.non_faithful = []           # [{'what', 'name', 'detail'}] uses of the non-faithful opt-ins (stem_fallback / shader_twin_fallback)
        self._stem_verdicts = {}         # vista tag index -> custom_stems.Verdict of a local stem
        self._custom_entries = {}        # vista tag index -> RevEntry (private port) | None  (decided once per local stem)
        self.custom_pairs = []           # [(report row, RevEntry)] of the stems ported under a private name (report shader_port.custom_stems)
        self.water_mode = str(opts.get('water_chain', 'vista')).lower()
        if self.water_mode not in ('vista', 'mcc'):
            raise RevPlannerError("planner option water_chain must be 'vista' or 'mcc', not %r" % (opts.get('water_chain'),))
        self._water_entries = {}         # (src_map, vista tag index) -> RevEntry (the private water stem) | None  (decided once per stem tag)
        self.water_pairs = []            # [(report row, RevEntry | None)] of the water stems of the input (report shader_port.water_chain.chains)
        self.sky_mode = str(opts.get('sky_depth', 'vista')).lower()
        if self.sky_mode not in ('vista', 'mcc'):
            raise RevPlannerError("planner option sky_depth must be 'vista' or 'mcc', not %r" % (opts.get('sky_depth'),))
        self.sky_rows = []               # [(report row, RevEntry | None)] of the sky vertex shaders of the plan (report shader_port.sky_pin.vertex_shaders)

    # ---- stems carried under a STOCK name with custom content --------------------------------
    def custom_verdict(self, name, vtag):
        """custom_stems.Verdict of a stem tag of the input map that carries data (memoised)."""
        v = self._stem_verdicts.get(vtag.index)
        if v is None:
            v = self._stem_verdicts[vtag.index] = CST.classify_local_stem(self.env, self.vista, vtag, name)
        return v

    def is_custom_stem(self, group, name, k):
        """True when the input's own stem `name` is custom content under a stock name (and the private port is on)."""
        if group != 'stem' or self.port is None or not self.opts.get('custom_stem_port', True):
            return False
        t = self.local.get(k)
        return t is not None and bool(t.addr) and self.custom_verdict(name, t).kind == 'custom'

    def custom_stem_entry(self, name, vtag, origin=None):
        """The entry the input's stem `name` (a tag of the INPUT map, referenced by a shader) stands for when it is custom content under a STOCK name:
        a PRIVATE port `<name>__<map stem>` ('convert', built by handlers/shaderfam.py from the Vista tag - structure unchanged, passes re-pointed), or an
        UNPORTABLE 'drop' with the exact reason.  None when the stem is interchangeable with MCC's (or MCC has no stem of that name: the ordinary port
        under the same name applies).  Every reference to this Vista tag is redirected to the entry (RevPlan.entry_of_datum); MCC's own tag of the stock
        name stays the donor for MCC's tags, which never saw the community stem."""
        if self.port is None or vtag is None or not vtag.addr or not self.opts.get('custom_stem_port', True):
            return None
        hit = self._custom_entries.get(vtag.index, 0)
        if hit != 0:
            return hit
        self._custom_entries[vtag.index] = None
        v = self.custom_verdict(name, vtag)
        if v.kind != 'custom':
            return None

        def taken(n):
            kk = RevPlan._key_for('stem', n)
            # (a name ending in the water suffix '__vista' belongs to the private water chain, whatever the map is called)
            return (kk in self.by_name or kk in self.local or self.mcc.find('stem', n) is not None or self.shared_ix.get(kk) is not None
                    or n.endswith(SPORT.PRIVATE_SUFFIX))

        pname = CST.private_name(name, self.vista, taken)
        dec = self.port.decide('stem', pname, 'src', vtag)
        row = {'name': name, 'private': pname, 'fp_local': v.fp_local, 'fp_mcc': v.fp_mcc, 'fps_vista_stock': list(v.fps_vista), 'mcc_label': v.mcc_label,
               'decision': dec.kind, 'reason': dec.reason, 'notes': list(dec.notes), 'flags': list(dec.flags)}
        if dec.ok:
            e = self.mk_convert('stem', pname, 'src', vtag, 'PORT (private): the input carries its own stem under the stock name %s (fingerprint %s; MCC %s, '
                                'stock Vista %s); faithful port of the compiled Vista tag under a private name (%s)' % (
                                    name.split('\\')[-1], v.fp_local, v.fp_mcc, ','.join(v.fps_vista) or '-', '; '.join(dec.notes[:2])), origin or 'custom-stem')
            self.plan.stats['port:stem'] += 1
            self.plan.stats['port:stem_private'] += 1
        else:
            e = self.mk_drop('stem', pname, 'UNPORTABLE: %s' % dec.reason, origin or 'custom-stem')
            self.plan.stats['unportable:stem'] += 1
        e.port = dec
        e.private_of = name
        self.plan.private_stems[('src', vtag.index)] = e
        self._custom_entries[vtag.index] = e
        self.custom_pairs.append((row, e))
        self.port.notes['donor_audit'].append({'group': 'stem', 'name': name, 'text': (
            'stem %s: custom content under a stock name (fingerprint %s; MCC\'s stem %s, stock Vista %s) -> %s; MCC\'s own stem of that name stays the donor for MCC\'s tags' % (
                name.split('\\')[-1], v.fp_local, v.fp_mcc, ','.join(v.fps_vista) or 'none',
                ('PRIVATE faithful port ' + pname) if dec.ok else ('UNPORTABLE: ' + dec.reason)))})
        return e

    # ---- the Vista-authored water chains ---------------------------------------------------------------------
    def name_taken(self, group, n):
        """Is (group, n) already a tag name of the plan, of the input, of MCC or of Vista's shared.map?"""
        kk = RevPlan._key_for(group, n)
        return kk in self.by_name or kk in self.local or self.mcc.find(group, n) is not None or self.shared_ix.get(kk) is not None

    def private_stem_entry(self, name, vtag, vlabel, origin=None):
        """The private port a stem reference of a Vista tag stands for: the input's custom stem under a stock name (custom_stem_entry) or the stem of a Vista-authored water chain
        (water_stem_entry).  None when MCC's stem of that name is the right one (a donor)."""
        if vtag is None or not vtag.addr or vlabel not in ('src', 'shared'):
            return None
        pe = self.custom_stem_entry(name, vtag, origin) if vlabel == 'src' else None
        return pe if pe is not None else self.water_stem_entry(name, vtag, vlabel, origin)

    def water_stem_entry(self, name, vtag, vlabel, origin=None):
        """A Vista stem (a tag of the input, or of Vista's shared.map) on the registry of semantically different water chains (shader_tags.SEMANTIC_WATER_CHAINS): MCC's stem /
        pass / vertex shader of those names are other SHADERS, so the shaders the Vista data was authored against are not mapped onto them.  The entry is the PRIVATE port
        '<name>__vista' ('convert', built by handlers/shaderfam.py from the Vista tag - structure unchanged); its Pass reference goes to the private port of the Vista pass, that
        one's Vertex Shader to the private port of the Vista vertex shader (private_child).  Every reference to the Vista stem tag is redirected to it (RevPlan.private_stems,
        entry_of_datum); MCC's own chain stays the donor for MCC's tags.  If the chain cannot be ported and proven (never seen) the donor chain is used and the plan reports it
        under water_chain.fallbacks.  None when the option water_chain is 'mcc', the stem is not on the registry, or the chain falls back."""
        if self.port is None or self.water_mode != 'vista' or not ST.is_water_chain('stem', name):
            return None
        key = (vlabel, vtag.index)
        hit = self._water_entries.get(key, 0)
        if hit != 0:
            return hit
        self._water_entries[key] = None
        pname = SPORT.private_water_name(name)
        row = {'name': name, 'private': pname, 'source': vlabel, 'reason': ST.water_chain_reason('stem', name)}
        if name.lower().endswith('\\water_pool_static'):
            row['caveat'] = ('the private pass keeps the Vista Render States (DESTBLEND = ONE); both MCC passes of this family (water_pool_static and water_pool_static_cube) carry INVSRCALPHA '
                             '(343 edited it), so no stock MCC pass proves ONE with this pixel shader in MCC: include a water_pool_static map (gemini) in the in-game A/B')
        if self.name_taken('stem', pname):
            row.update(decision='fallback_donor', detail='the private name %s is already a tag name' % pname)
            self.water_pairs.append((row, None))
            self.plan.warn('water_chain_fallback', '%s: %s' % (name, row['detail']))
            return None
        dec = self.port.decide('stem', pname, vlabel, vtag)
        row.update(notes=list(dec.notes), flags=list(dec.flags))
        if not dec.ok:
            row.update(decision='fallback_donor', detail='UNPORTABLE: %s' % dec.reason)
            self.water_pairs.append((row, None))
            self.plan.warn('water_chain_fallback', '%s: the private Vista chain cannot be ported (%s); MCC\'s chain of that name is used' % (name, dec.reason))
            return None
        e = self.mk_convert('stem', pname, vlabel, vtag, 'PORT (private water chain): the input authored its water shaders against the Vista chain; MCC\'s %s is another shader; '
                            'faithful port of the compiled Vista tag under a private name (%s)' % (name.split('\\')[-1], '; '.join(dec.notes[:2])), origin or 'water-chain')
        e.port = dec
        e.private_of = name
        self.plan.private_stems[(vlabel, vtag.index)] = e
        self._water_entries[key] = e
        self.plan.stats['port:stem'] += 1
        self.plan.stats['port:stem_water'] += 1
        row['decision'] = 'port'
        self.water_pairs.append((row, e))
        self.port.notes['donor_audit'].append({'group': 'stem', 'name': name, 'text': (
            'stem %s: Vista-authored water chain (%s tag); MCC\'s stem / pass / vertex shader of these names are other shaders (shader_tags.SEMANTIC_WATER_CHAINS) -> PRIVATE faithful '
            'port %s; MCC\'s own chain stays the donor for MCC\'s tags' % (name.split('\\')[-1], vlabel, pname))})
        return e

    def private_spas_entry(self, pname, r, dec, user):
        """The PRIVATE port of a Vista water pass for a private water stem: one 'convert' entry '<pass>__vista', shared by every private stem that names the pass."""
        priv = SPORT.private_water_name(pname)
        k = RevPlan._key_for('spas', priv)
        pe = self.by_name.get(k)
        if pe is None:
            pe = self._new('spas', priv, 'convert', 'PORT (private water chain): MCC\'s pass %s is another shader; faithful port of the Vista pass the stem %s names' % (
                pname.split('\\')[-1], user.split('\\')[-1]), 'port')
            label, vt = r
            pe.src_map = label
            pe.src_index = vt.index
            pe.src_size = vt.size
            pe.port = dec
            pe.private_of = pname
            self.queue.append(pe)
            self.plan.stats['port:spas'] += 1
            self.plan.stats['port:spas_water'] += 1
        return pe

    def private_child(self, e, g, nm, vt, lb):
        """The PRIVATE water-chain entry that stands for a Vista pass / vertex shader reference of a private water stem / pass (the MCC donor of the same name is NOT a child of
        it); None for every other reference."""
        if e.action != 'convert' or e.port is None or not e.port.info.get('water_chain'):
            return None
        if e.group == 'stem' and g == 'spas':
            hit = (e.port.info.get('private_spas') or {}).get((nm or '').lower())
            return self.private_spas_entry(hit[0], hit[1], hit[2], e.name) if hit is not None else None
        if e.group == 'spas' and g == 'vrtx':
            hit = (e.port.info.get('private_vrtx') or {}).get((nm or '').lower())
            return self.private_vrtx_entry(hit[0], hit[1], hit[2], e.name) if hit is not None else None
        return None

    def port_source(self, k, vtag, vlabel):
        """(label, VistaTag) of the Vista tag a shader-code port would start from: the input map's own tag of that name, else the tag the reference
        resolved to, else Vista's shared.map tag; (None, None) if there is none."""
        loc = self.local.get(k)
        if loc is not None:
            return 'src', loc
        if vtag is not None and vtag.addr and vlabel:
            return vlabel, vtag
        vs = self.shared_ix.get(k)
        if vs is not None:
            return 'shared', vs
        return None, None

    def portable(self, group, name, vtag=None, vlabel=None):
        """PortDecision for a shader-code name MCC does not have (None when the port is off or no Vista tag exists)."""
        if self.port is None or self.mcc.find(group, name) is not None:
            return None
        label, src = self.port_source(RevPlan._key_for(group, name), vtag, vlabel)
        if src is None:
            return None
        return self.port.decide(group, name, label, src)

    def auto_stem(self, name):
        """stem_fallback='auto': register (and return) the suggested MCC substitute of a Vista stem MCC lacks, else None."""
        k = name.lower()
        if k in self.stem_fallback:
            return self.stem_fallback[k]
        if not self.stem_auto or self.mcc.find('stem', name) is not None:
            return None
        sug = suggest_stem_fallbacks([name], self.env).get(name)
        if sug is None or sug[1] < AUTO_STEM_MIN_SCORE:
            return None
        self.stem_fallback[k] = sug[0]
        return sug[0]

    # ---- naming -------------------------------------------------------------------------------------------------------
    def tag_name(self, label, t):
        if t.name:
            return t.name
        return synthetic_name(label, t)

    def index_local(self):
        for t in self.vista.tags:
            if t.is_empty or not t.addr:
                continue
            name = self.tag_name('src', t)
            k = RevPlan._key_for(t.group, name)
            if k in self.local:
                self.plan.warn('duplicate_local_name', '%s %s (#%d and #%d)' % (t.group, name, self.local[k].index, t.index))
                continue
            self.local[k] = t

    # ---- entry creation -----------------------------------------------------------------------------------------------
    def _new(self, group, name, action, reason, origin):
        e = RevEntry(group, name, action, reason)
        e.origin = origin
        self.plan.all_entries.append(e)
        k = RevPlan._key_for(group, name)
        e.key = k
        self.by_name[k] = e
        return e

    def mk_drop(self, group, name, reason, origin, redirect=None):
        e = self._new(group, name, 'drop', reason, origin)
        e.redirect = redirect
        return e

    def mk_convert(self, group, name, label, vt, reason, origin):
        e = self._new(group, name, 'convert', reason, origin)
        e.src_map = label
        e.src_index = vt.index
        e.src_size = vt.size
        self.plan.by_src[(label, vt.index)] = e
        e.twin = self.mcc.find(group, name, kinds=None) if group != 'ugh!' else None
        self.queue.append(e)
        return e

    def mk_donor(self, group, name, d, reason, origin):
        e = self._new(group, name, 'mcc_donor', reason, origin)
        e.donor = d
        e.src_size = d.size
        self.plan.by_donor[(d.label, d.index)] = e
        self.queue.append(e)
        return e

    def require(self, group, name, vtag=None, vlabel=None, mcc_label=None, origin=None):
        """The entry for (group, name), deciding it on first use.  vtag/vlabel: the Vista tag the reference resolved to (if the
        reference comes from a Vista tag); mcc_label: the MCC map of the donor tag that holds the reference."""
        if group == 'stem' and vtag is not None and vlabel in ('src', 'shared'):   # the input's own stem with custom content under a stock name, or a Vista-authored water chain -> private port
            pe = self.private_stem_entry(name, vtag, vlabel, origin)
            if pe is not None:
                return pe
        k = RevPlan._key_for(group, name)
        e = self.by_name.get(k)
        if e is not None:
            return e
        if group == 'stem' and (self.stem_fallback or self.stem_auto) and self.mcc.find('stem', name) is None:
            dec = self.portable('stem', name, vtag, vlabel)
            if dec is not None and dec.ok:
                if self.stem_fallback.get(k[1]) or self.stem_auto:
                    self.plan.stats['stem_fallback_ignored_port_available'] += 1       # the faithful port wins over a substitute
            elif self.auto_stem(name):
                sub = self.stem_fallback[k[1]]
                e = self.require('stem', sub, mcc_label=mcc_label, origin=origin)
                self.by_name[k] = e                                     # alias: remap_name('stem', <vista name>) -> the substitute
                self.plan.stats['stem_fallback'] += 1
                self.non_faithful.append({'what': 'stem_fallback', 'name': name, 'detail': 'NON-FAITHFUL substitute stem %s (%s)' % (
                    sub, dec.reason if dec is not None else 'the port is off')})
                return e
        return self.decide(k, group, name, vtag, vlabel, mcc_label, origin)

    def decide(self, k, group, name, vtag, vlabel, mcc_label, origin):
        opts = self.opts
        if group in RESOURCE_GROUPS:
            return self.mk_drop(group, name, 'scenario resource: MCC has no such tag (its scnr keeps the same inline blocks, '
                                'references null)', origin)
        if group == 'ugh!':
            return self.mk_gestalt()
        if group == 'vrtx' and self.port is not None and self.sky_mode == 'vista' and ST.is_sky_pinned_vrtx(name):
            se = self.sky_pin_entry(k, name, vtag, vlabel, origin)      # Vista pins the sky to the far plane, MCC's same-name tag writes real depth: the Vista tag, z = 0
            if se is not None:
                return se
        if group in DONOR_REQUIRED:
            d = self.mcc.find(group, name, prefer=mcc_label)
            if d is not None:
                if self.port is not None and group in ('stem', 'spas') and k in self.local and not self.is_custom_stem(group, name, k):
                    self.port.audit_donor(group, name, 'src', self.local[k])   # custom content under a stock name? (report only; a custom STEM is ported privately)
                return self.mk_donor(group, name, d, 'shader code exists as DXBC only in MCC: same-name tag of %s' % d.label, origin)
            return self.decide_missing_shader_code(k, group, name, vtag, vlabel, origin)
        loc = self.local.get(k)
        if group == 'matg' and k == MATG_KEY:
            return self.decide_matg(k, group, name, loc, vtag, vlabel, origin)
        if loc is not None:                                             # a Vista local tag overrides any same-named shared / donor tag
            return self.mk_convert(group, name, 'src', loc, 'local tag of the input map', origin)
        if vlabel == 'shared' and vtag is not None:                    # a Vista reference that resolved to a shared.map tag
            if opts['shared_policy'] == 'mcc':
                d = self.mcc.find(group, name, kinds=SHARED_KINDS)
                if d is not None:
                    return self.mk_donor(group, name, d, 'Vista shared.map tag -> MCC %s tag of the same name' % d.label, origin)
                return self.mk_convert(group, name, 'shared', vtag, 'Vista shared.map tag without an MCC twin', origin)
            return self.mk_convert(group, name, 'shared', vtag, 'Vista shared.map tag (shared_policy=vista)', origin)
        if vlabel not in (None, 'src', 'shared') and vtag is not None:  # tag of another Vista map (never for stock inputs)
            return self.mk_convert(group, name, vlabel, vtag, 'tag of Vista %s' % vlabel, origin)
        # a reference found inside an MCC donor tag: MCC's own tag of that name (same map first), else Vista's shared.map
        d = self.mcc.find(group, name, prefer=mcc_label)
        if d is not None:
            return self.mk_donor(group, name, d, 'referenced by an MCC donor tag: %s' % d.label, origin)
        vs = self.shared_ix.get(k)
        if vs is not None:
            return self.mk_convert(group, name, 'shared', vs, 'referenced by an MCC donor tag; only Vista shared.map has it', origin)
        self.plan.warn('unresolved_mcc_ref', '%s %s' % (group, name))
        return self.mk_drop(group, name, 'UNRESOLVED: referenced by an MCC donor tag but no source has it', origin)

    def sky_pin_entry(self, k, name, vtag, vlabel, origin=None):
        """The entry for a sky vertex shader on shader_tags.SKY_DEPTH_PINNED_VRTX (option sky_depth='vista'): MCC has a tag of this name, but it writes REAL depth (z = w * c10.w + c10.z)
        where the Vista tag pins the clip depth to the far plane (clip z == clip w), and MCC draws the sky late with the depth test on - a sky polygon inside the far plane that is at or
        above the BSP then wins (cbsp's flat ground cap at z = 0 painted flat blue over the coplanar floor).  The entry is a 'convert' of the Vista tag under the SAME name (the input
        map's own tag, else the tag the reference resolved to, else Vista's shared.map), built by handlers/shaderfam.py: translate_vs gives z = 0 (the reverse-Z far plane) in every
        classification; MCC's own sky stems / passes / pixel shaders, which reference the vertex shader by name, stay and pick the entry up through by_name.  Decided once per name.
        None (-> MCC's donor, plan warning sky_pin_fallback, report row 'fallback_donor') if the input carries no such tag or the PortPolicy refuses it (every non-empty classification
        must translate with the pin: a partly pinned tag is never built)."""
        label, src = self.port_source(k, vtag, vlabel)
        row = {'name': name, 'source': label, 'reason': ST.sky_pin_reason(name)}
        if src is None:
            row.update(decision='fallback_donor', detail='neither the input map nor Vista\'s shared.map holds a Vista tag of that name')
            self.sky_rows.append((row, None))
            self.plan.warn('sky_pin_fallback', '%s: %s; MCC\'s vertex shader of that name (real depth) is used' % (name, row['detail']))
            return None
        dec = self.port.decide('vrtx', name, label, src)
        row.update(notes=list(dec.notes), flags=list(dec.flags))
        if not dec.ok:
            row.update(decision='fallback_donor', detail='UNPORTABLE: %s' % dec.reason)
            self.sky_rows.append((row, None))
            self.plan.warn('sky_pin_fallback', '%s: the Vista vertex shader cannot be ported with the far-plane pin (%s); MCC\'s vertex shader of that name (real depth) is used' % (name, dec.reason))
            return None
        e = self.mk_convert('vrtx', name, label, src, 'PORT (sky depth pin): Vista pins the sky to the far plane (clip z == clip w) where MCC\'s %s writes real depth and draws the sky late with '
                            'the depth test on; faithful port of the compiled Vista tag under the same name, z = 0 in every classification (%s)' % (
                                name.split('\\')[-1], '; '.join(dec.notes[:1])), origin or 'sky-pin')
        e.port = dec
        self.plan.stats['port:vrtx'] += 1
        self.plan.stats['port:vrtx_sky_pin'] += 1
        row['decision'] = 'port'
        self.sky_rows.append((row, e))
        self.port.notes['donor_audit'].append({'group': 'vrtx', 'name': name, 'text': (
            'vertex shader %s: Vista pins the clip depth to the far plane (shader_tags.SKY_DEPTH_PINNED_VRTX); MCC\'s tag of that name writes real depth -> faithful port of the %s tag '
            'under the same name, z = 0' % (name.split('\\')[-1], label))})
        return e

    def decide_missing_shader_code(self, k, group, name, vtag, vlabel, origin):
        """A stem / pass / vertex shader whose NAME no MCC map carries.  With the port on (default) the Vista tag is rebuilt from its compiled code
        ('convert' entry, handled by handlers/shaderfam.py) when the whole closure is proven; otherwise the name is UNPORTABLE (drop; the exact reason
        is in the entry and in the report).  With the port off: the previous NO_MCC_DONOR drop."""
        dec = self.portable(group, name, vtag, vlabel)
        if dec is None:
            return self.mk_drop(group, name, 'NO_MCC_DONOR: no MCC map carries %s %s' % (group, name), origin)
        label, src = self.port_source(k, vtag, vlabel)
        if dec.ok:
            e = self.mk_convert(group, name, label, src, 'PORT: no MCC map carries this %s; faithful port of the compiled Vista code (%s)' % (
                group, '; '.join(dec.notes[:2])), origin)
            e.port = dec
            self.plan.stats['port:' + group] += 1
            for vl, (vname, vr, pv) in sorted((dec.info.get('private_vrtx') or {}).items()):
                e.children.append(self.private_vrtx_entry(vname, vr, pv, e.name))
            return e
        e = self.mk_drop(group, name, 'UNPORTABLE: %s' % dec.reason, origin)
        e.port = dec
        self.plan.stats['unportable:' + group] += 1
        return e

    def private_vrtx_entry(self, vname, vr, pv, user):
        """The PRIVATE port of a Vista vertex shader (a ported pass whose MCC namesake drives the Vista pixel shaders differently, e.g. water_pool_static):
        a 'convert' entry under its own name (shader_port.private_vrtx_name); one per vertex shader, shared by all ported passes that need it."""
        pname = SPORT.private_vrtx_name(vname)
        k = RevPlan._key_for('vrtx', pname)
        pe = self.by_name.get(k)
        if pe is None:
            pe = self._new('vrtx', pname, 'convert', 'PORT (private copy): MCC\'s vertex shader %s behaves differently from the Vista one for the ported pass %s' % (
                vname.split('\\')[-1], user.split('\\')[-1]), 'port')
            label, vt = vr
            pe.src_map = label
            pe.src_index = vt.index
            pe.src_size = vt.size
            pe.port = pv
            pe.private_of = vname
            self.queue.append(pe)
            self.plan.stats['port:vrtx'] += 1
            self.plan.stats['port:vrtx_private'] += 1
        return pe

    def decide_matg(self, k, group, name, loc, vtag, vlabel, origin):
        mode = self.opts['matg']
        tier = {0: 'single_player_shared', 1: 'shared', 2: 'mainmenu'}.get(self.map_type, 'shared')
        # the MCC globals of the SAME map (stock campaign / H2EK map of that name) is the best template, else the shared database
        # of the map's type
        # (only for solo / main-menu inputs: an MP map's globals are the shared ones; never the H2EK maps - their globals carry
        # their author's own edits)
        same = self.vista.header.name.lower()
        twin = None
        if self.map_type != 1:
            for d in self.mcc.find_all('matg', name):
                if d.label == 'stock:' + same:
                    twin = d
                    break
        if twin is None:
            twin = self.mcc.find('matg', name, prefer=tier, kinds=(tier,)) or self.mcc.find('matg', name)
        if mode == 'mcc' or (mode == 'auto' and loc is None and twin is not None):
            if twin is not None:
                return self.mk_donor(group, name, twin, 'globals: MCC %s globals (%s)' % (tier, 'forced' if mode == 'mcc' else
                                                                                         'input uses the shared Vista globals'), origin)
        src = loc
        label = 'src'
        if src is None:
            src = self.shared_ix.get(k)
            label = 'shared'
        if src is None:
            self.plan.warn('no_matg', 'no Vista globals tag found')
            if twin is not None:
                return self.mk_donor(group, name, twin, 'globals: no Vista globals, MCC tag', origin)
            return self.mk_drop(group, name, 'UNRESOLVED: no globals tag', origin)
        e = self.mk_convert(group, name, label, src, 'globals converted from the Vista %s globals' % ('local' if loc else 'shared'), origin)
        e.twin = twin
        return e

    def mk_gestalt(self):
        loc = self.local.get(GESTALT_KEY)
        e = self._new('ugh!', GESTALT_NAME, 'convert', 'ONE merged sound gestalt (all H2EK maps: single ugh!, last, header 0x2EC = -1)',
                      'gestalt')
        t = None
        for tt in self.vista.tags:
            if not tt.is_empty and tt.addr and tt.group == 'ugh!':
                t = tt
                break
        if t is not None:
            e.src_map = 'src'
            e.src_index = t.index
            e.src_size = t.size
            self.plan.by_src[('src', t.index)] = e
        self.plan.gestalt = e
        self.queue.append(e)
        return e

    # ---- closure ------------------------------------------------------------------------------------------------------
    def scan_vista(self, e):
        cache = self.plan.vista_cache(e.src_map)
        t = cache.tags[e.src_index]
        return cache, self.env.scan_vista.scan(cache.read_addr, t.group, t.addr)

    def mcc_targets(self, donor):
        """[(group, name, None, None, label)] of the tagrefs of an MCC donor tag, and the number of dangling ones."""
        c, t = self.mcc.tag(donor)
        raw = self.env.scan_mcc.scan(lambda a, n: c.read_at_addr(a, n), t.group, t.addr)
        targets = []
        dangling = 0
        for g, d, off in raw:
            if d in (NULL, 0):
                continue
            x = c.tag_by_datum(d)
            if x is None or not x.name:
                dangling += 1
                continue
            targets.append((x.group, x.name, None, None, donor.label))
        return targets, dangling

    def process(self, e):
        if e.group == 'ugh!':
            return                                                      # its children are the explicit gestalt edges (gestalt_edges)
        if e.action == 'convert':
            cache, raw = self.scan_vista(e)
            targets = []
            dangling = 0
            for g, d, off in raw:
                if d in (NULL, 0):
                    continue
                r = self.plan.resolve_vista(e.src_map, d)
                if r is None:
                    dangling += 1
                    continue
                label, vt = r
                targets.append((vt.group, self.tag_name(label, vt), vt, label))
            if e.group == 'shad':
                miss = []
                for g, nm, vt, lb in targets:
                    if g != 'stem':
                        continue
                    ok, why = self.stem_usable(nm, vt, lb)
                    if not ok:
                        miss.append((nm, why))
                if miss:
                    self.demote_shader(e, miss[0][0], miss[0][1])
                    return
            self.add_children(e, [(g, nm, vt, lb, None) for g, nm, vt, lb in targets], dangling)
            if e.group == 'matg' and e.twin is not None:
                # a converted globals tag gets the MCC-only fields (remastered sound classes, anniversary globals ...) from its MCC twin:
                # the twin's references must exist in the output as well
                tt, dd = self.mcc_targets(e.twin)
                self.add_children(e, tt, dd, extend=True)
        else:
            targets, dangling = self.mcc_targets(e.donor)
            self.add_children(e, targets, dangling)

    def add_children(self, e, targets, dangling, extend=False):
        seen = set(id(c) for c in e.children)
        to_drop = 0
        for g, nm, vt, lb, ml in targets:
            ch = self.private_child(e, g, nm, vt, lb)                       # a private water stem / pass names the private pass / vertex shader, not MCC's namesake
            if ch is None:
                ch = self.require(g, nm, vtag=vt, vlabel=lb, mcc_label=ml, origin=e.name)
            if ch is e or id(ch) in seen:
                continue
            seen.add(id(ch))
            e.children.append(ch)
            if ch.action == 'drop':
                to_drop += 1
        if extend:
            e.refs = (e.refs[0] + len(targets), e.refs[1] + dangling, e.refs[2] + to_drop)
        else:
            e.refs = (len(targets), dangling, to_drop)

    def stem_usable(self, nm, vt, lb):
        """(ok, reason): can a converted shader be compiled against the stem `nm`?  Yes when the plan already holds a kept entry for it, MCC has the
        stem, the faithful port can rebuild it, or (opt-in, NON-FAITHFUL) a stem_fallback substitute exists."""
        if vt is not None and lb in ('src', 'shared'):
            pe = self.private_stem_entry(nm, vt, lb)                     # the input's custom stem under a stock name / a Vista-authored water chain: its private port (or UNPORTABLE)
            if pe is not None:
                return pe.action != 'drop', pe.reason
        k = RevPlan._key_for('stem', nm)
        e = self.by_name.get(k)
        if e is not None:
            return e.action != 'drop', e.reason
        if self.mcc.find('stem', nm) is not None:
            return True, ''
        dec = self.portable('stem', nm, vt, lb)
        if dec is not None and dec.ok:
            return True, ''
        if self.auto_stem(nm):
            return True, ''
        return False, ('UNPORTABLE: %s' % dec.reason) if dec is not None else 'NO_MCC_DONOR: no MCC map carries this stem and the port is off'

    def demote_shader(self, e, stem, why=''):
        """A shader whose stem can be neither taken from MCC nor ported cannot be converted: it is UNPORTABLE.  The references to it go to MCC's
        shaders\\invalid (no substitute).  Only with the explicit NON-FAITHFUL opt-in shader_twin_fallback is MCC's shader of the same name used."""
        self.plan.by_src.pop((e.src_map, e.src_index), None)
        self.unportable_shaders.append({'shader': e.name, 'stem': stem, 'reason': why})
        twin = self.mcc.find('shad', e.name)
        if twin is not None and self.opts.get('shader_twin_fallback', False):
            self.non_faithful.append({'what': 'shader_twin_fallback', 'name': e.name, 'detail': 'NON-FAITHFUL: MCC shader of the same name replaces the Vista shader (%s)' % why})
            e.action = 'mcc_donor'
            e.donor = twin
            e.reason = 'Vista stem %s has no MCC twin: MCC %s shader of the same name instead' % (stem.split('\\')[-1], twin.label)
            e.src_map = None
            e.src_index = None
            e.twin = None
            e.src_size = twin.size
            self.plan.by_donor[(twin.label, twin.index)] = e
            self.plan.stats['shad_to_mcc_twin'] += 1
            self.queue.append(e)
            return
        e.action = 'drop'
        e.reason = 'UNPORTABLE shader: its stem %s cannot be used%s' % (stem.split('\\')[-1], (' (' + why + ')') if why else '')
        self.plan.stats['unportable:shad'] += 1
        if self.invalid_entry is None:
            d = self.mcc.find(*INVALID_SHADER)
            k = _nk(*INVALID_SHADER)
            ie = self.by_name.get(k)
            if ie is None and d is not None:
                ie = self.mk_donor(INVALID_SHADER[0], d.name, d, 'redirect target of the UNPORTABLE shaders (their stem is neither in MCC nor portable)', 'shader-fallback')
            self.invalid_entry = ie
        e.redirect = self.invalid_entry
        e.src_map = None
        e.src_index = None
        e.twin = None

    def run_queue(self):
        while self.queue:
            self.process(self.queue.popleft())

    # ---- driver -------------------------------------------------------------------------------------------------------
    def run(self):
        t0 = time.time()
        v = self.vista
        if v.header.type not in (0, 1, 2):
            raise RevPlannerError('input map type %d (%s) is not a playable map' % (v.header.type, v.header.scenario_type_name))
        self.index_local()
        # roots: scnr, matg (header datums), the gestalt (always)
        scn = self.plan.resolve_vista('src', v.scenario_datum)
        if scn is None:
            raise RevPlannerError('scenario datum 0x%08X does not resolve' % v.scenario_datum)
        self.plan.scenario = self.require('scnr', self.tag_name('src', scn[1]), vtag=scn[1], vlabel='src', origin='root')
        glb = self.plan.resolve_vista('src', v.globals_datum)
        if glb is None:
            raise RevPlannerError('globals datum 0x%08X does not resolve' % v.globals_datum)
        self.plan.globals_entry = self.require('matg', self.tag_name(glb[0], glb[1]), vtag=glb[1], vlabel=glb[0], origin='root')
        self.require('ugh!', GESTALT_NAME, origin='root')
        self.run_queue()
        # the prefix tags every H2EK map starts with (matg's sound classes) and the gestalt's implicit references
        self.placeholder_roots()
        self.run_queue()
        self.gestalt_edges()
        self.run_queue()
        self.handle_orphans()
        self.run_queue()
        self.plan.notes['shadowed'] = self.shadowed()
        self.plan.notes['shader_port'] = self.shader_port_notes()
        self.finish()
        self.plan.elapsed = time.time() - t0
        return self.plan

    def shader_port_notes(self):
        """The shader-port section of the plan report: what was ported (with the facts behind each), what is UNPORTABLE and why, the shaders that
        therefore point at shaders\\invalid, flags that cannot be verified without the engine, donor audits, NON-FAITHFUL opt-ins that were used."""
        ported = {g: [] for g in DONOR_REQUIRED}
        unportable = []
        flags = []
        for e in self.plan.all_entries:
            if e.group not in DONOR_REQUIRED or e.port is None:
                continue
            if e.action == 'convert':
                ported[e.group].append({'name': e.name, 'notes': list(e.port.notes), 'flags': list(e.port.flags)})
                for f in e.port.flags:
                    if f not in flags:
                        flags.append(f)
            else:
                unportable.append({'group': e.group, 'name': e.name, 'reason': e.port.reason})
        for g in ported:
            ported[g].sort(key=lambda r: r['name'])
        pol = self.port
        if pol is not None:                                         # decisions the closure never turned into an entry (a stem whose shaders were demoted)
            have = set((u['group'], u['name'].lower()) for u in unportable)
            for u in pol.notes['unportable']:
                if (u['group'], u['name'].lower()) not in have:
                    unportable.append(dict(u))
                    have.add((u['group'], u['name'].lower()))
        custom = []
        for row, pe in self.custom_pairs:                           # stems ported under a private name: how many shaders of the input use each
            r = dict(row)
            r['action'] = pe.action
            r['shaders'] = sum(1 for e in self.plan.all_entries if e.group == 'shad' and any(c is pe for c in e.children))
            custom.append(r)
        return {'enabled': pol is not None, 'ported': ported, 'unportable': unportable, 'unportable_shaders': list(self.unportable_shaders),
                'flags': flags, 'engine_assumptions': list(SPORT.ENGINE_ASSUMPTIONS), 'engine_verified': list(SPORT.ENGINE_VERIFIED),
                'donor_audit': list(pol.notes['donor_audit']) if pol is not None else [],
                'custom_stems': custom, 'water_chain': self.water_report(), 'sky_pin': self.sky_report(),
                'non_faithful': list(self.non_faithful), 'compiler': SPORT.compiler_status() if (pol is not None and any(ported.values())) else None}

    BITMAP_TYPES = {0: '2D', 1: '3D', 2: 'CUBE', 3: 'WHITE', 4: 'BLACK'}

    @staticmethod
    def _bitmap_type(read, lay, addr):
        """Type of Bitmaps[0] of a bitm tag (read(addr, n) -> bytes; lay = the game's bitm layout) as an int, None when the tag has no bitmap."""
        bf = next(f for f in lay.fields if f.name == 'Bitmaps')
        tf = next(c for c in bf.children if c.name == 'Type')
        cnt, ptr = struct.unpack_from('<iI', read(addr + bf.offset, 8))
        if cnt <= 0 or not ptr:
            return None
        return struct.unpack_from('<H', read(ptr + tf.offset, 2))[0]

    def reflection_bitmap(self, e, slot=None):
        """(bitmap tag name, type name) of Bitmaps[slot] (default ST.WATER_REFLECTION_BITMAP_SLOT; the slot that feeds the pass's cube sampler: ST.water_cube_slot) of the converted
        Vista shader `e`, as the OUTPUT will hold it (the converted Vista bitmap, or the MCC donor bitmap of that name); type name '2D' / '3D' / 'CUBE' / ... or 'none' (null slot) /
        'unresolved' / 'dropped' / 'unreadable'."""
        from .handlers import shaders as RS
        try:
            cache, vt = self.plan.source_of(e)
            node = T.decode_tag(cache.read_addr, self.env.lay_vista['shad'], vt.addr, 'shad')
            pp = RS._blk(node, 'Postprocess Definition')
            bm = RS._blk(pp[0], 'Bitmaps') if pp else []
            slot = ST.WATER_REFLECTION_BITMAP_SLOT if slot is None else slot
            if slot >= len(bm):
                return '', 'none'
            d = bm[slot].u32(0)
            if d in (NULL, 0):
                return '', 'none'
            be = self.plan.entry_of_datum(e.src_map, d)
            if be is None:
                return '', 'unresolved'
            if be.action == 'convert':
                c, bt = self.plan.source_of(be)
                t = self._bitmap_type(c.read_addr, self.env.lay_vista['bitm'], bt.addr)
            elif be.action == 'mcc_donor':
                c, bt = self.plan.donor_source_of(be)
                t = self._bitmap_type(lambda a, n, c=c: c.read_at_addr(a, n), self.env.lay_mcc['bitm'], bt.addr)
            else:
                return be.name, 'dropped'
            return be.name, (self.BITMAP_TYPES.get(t, str(t)) if t is not None else 'unreadable')
        except Exception as ex:                                        # noqa: BLE001  a report row never aborts the plan
            return '', 'unreadable (%s)' % (type(ex).__name__,)

    def sky_report(self):
        """Section `sky_pin` of the shader-port report:
             mode                  the option sky_depth ('vista' = the Vista sky vertex shaders pin the depth to the far plane, 'mcc' = MCC's donors with real depth)
             registry              shader_tags.SKY_DEPTH_PINNED_VRTX (name -> measured reason)
             vertex_shaders        one row per sky vertex shader the plan decided: decision 'port' (action, classifications, z_pin / z_remap per classification) or 'fallback_donor' (detail)
             fallbacks             the rows that fell back to MCC's donor (each also warns sky_pin_fallback)
             donor_sky_vrtx        sky vertex shaders of the plan that are MCC's donors (option 'mcc', a fallback, or the shader port off): they write REAL depth
             sky_passes            passes of the plan that reference a sky vertex shader, each with the action of that vertex shader (the passes themselves are MCC's own donors)"""
        plan = self.plan
        rep = {'mode': self.sky_mode, 'registry': {n: r for n, r in sorted(ST.SKY_DEPTH_PINNED_VRTX.items())}, 'vertex_shaders': [], 'fallbacks': [], 'donor_sky_vrtx': [], 'sky_passes': []}
        for row, pe in self.sky_rows:
            r = dict(row)
            if pe is None:
                rep['fallbacks'].append(r)
            else:
                r['action'] = pe.action
                r['classifications'] = pe.port.info.get('classifications') if pe.port is not None else None
                r['z_pin'] = pe.port.info.get('z_pin') if pe.port is not None else None
                r['z_remap'] = pe.port.info.get('z_remap') if pe.port is not None else None
            rep['vertex_shaders'].append(r)
        sky = {}
        for e in plan.all_entries:
            if e.group == 'vrtx' and ST.is_sky_pinned_vrtx(e.name):
                sky[id(e)] = e
                if e.action == 'mcc_donor':
                    rep['donor_sky_vrtx'].append(e.name)
        for e in plan.all_entries:
            if e.group != 'spas':
                continue
            vs = [c for c in e.children if id(c) in sky]
            if vs:
                rep['sky_passes'].append({'pass': e.name, 'action': e.action, 'vertex_shaders': [{'name': c.name, 'action': c.action} for c in vs]})
        rep['sky_passes'].sort(key=lambda r: r['pass'])
        return rep

    def water_report(self):
        """Section `water_chain` of the shader-port report:
             chains                      one row per Vista water stem of the input: the private ports (decision 'port') or the fallback to MCC's chain, the shaders on it
             donor_chain_shaders         shaders of the input that sit on MCC's chain of a registry stem (option water_chain=mcc, or a fallback)
             reflection_bitmap_type_mismatch   a shader on the private chain whose reflection slot (Bitmaps[1], the pixel shader's `dcl_cube s1`) holds a bitmap that is not a cube (the
                                         Vista-literal pairing, kept; undefined on D3D9 / D3D11), or a shader on MCC's 2D chain that holds a cube
             no_water_definition         BSPs that use a water-chain shader but have no Water Definition (the engine then never writes the water pixel-shader constants c0..c2)
             coarse_mesh                 not computed here (needs the BSP part geometry)"""
        plan = self.plan
        rep = {'mode': self.water_mode, 'registry': {g: sorted(d) for g, d in ST.SEMANTIC_WATER_CHAINS.items()}, 'chains': [], 'fallbacks': [], 'donor_chain_shaders': [],
               'reflection_bitmap_type_mismatch': [], 'no_water_definition': [], 'sbsp_water_definitions': {},
               'coarse_mesh': 'not computed by the planner (needs the BSP part geometry)'}
        private = {}
        for row, pe in self.water_pairs:
            r = dict(row)
            if pe is None:
                rep['fallbacks'].append(r)
            else:
                private[id(pe)] = r
                r['action'] = pe.action
                passes = [c for c in pe.children if c.group == 'spas']
                r['passes'] = sorted(c.name for c in passes)
                r['vertex_shaders'] = sorted(set(v.name for c in passes for v in c.children if v.group == 'vrtx' and v.private_of))
                r['shaders'] = sum(1 for x in plan.all_entries if x.group == 'shad' and any(c is pe for c in x.children))
            rep['chains'].append(r)
        water_shads = set()
        for x in plan.all_entries:
            if x.group != 'shad' or x.action != 'convert' or x.src_map is None:
                continue
            st = next((c for c in x.children if c.group == 'stem' and (id(c) in private or (c.action == 'mcc_donor' and ST.is_water_chain('stem', c.name)))), None)
            if st is None:
                continue
            on_private = id(st) in private
            water_shads.add(id(x))
            if not on_private:
                rep['donor_chain_shaders'].append(x.name)
            slot = ST.water_cube_slot(st.name)
            if slot is None:                                          # the pass declares no cube sampler (water_static: all four samplers are dcl_2d): nothing can mismatch by dimension
                continue
            bname, btype = self.reflection_bitmap(x, slot)
            if (on_private and btype != 'CUBE') or (not on_private and btype == 'CUBE'):
                rep['reflection_bitmap_type_mismatch'].append({
                    'shader': x.name, 'stem': st.name, 'chain': 'private (Vista)' if on_private else 'MCC donor', 'bitmap': bname, 'bitmap_type': btype,
                    'kind': (('%s_bitmap_in_cube_sampler' % btype.lower()) if btype in self.BITMAP_TYPES.values() else 'no_usable_bitmap_in_cube_sampler') if on_private
                            else 'cube_bitmap_in_2d_sampler',
                    'note': ('Vista-literal pairing kept: the Vista pixel shader samples a CUBE (dcl_cube s1) and the shader holds a %s bitmap there; neither engine type-checks a shader\'s '
                             'parameter bitmaps against the sampler (Vista halo2.exe sub_6679D9 -> sub_65F600 -> SetTexture; MCC sub_18095BA90 -> sub_1809548A0), so this is the state the '
                             'author saw in Vista; what the engine draws for a sampler/bitmap dimension mismatch is undefined (D3D9 and D3D11) and was not game-tested.  The reflection '
                             'sample only enters through the fresnel weight (lrp(fresnel alpha, reflection, refraction)): the refraction term does not depend on it' % btype) if on_private else
                            ('MCC\'s pass samples this slot as a Texture2D and the shader holds a cube bitmap there (343 moved such shaders to the *_cube templates by hand)')})
        if water_shads:
            lay = self.env.lay_vista['sbsp']
            wf = next((f for f in lay.fields if f.name == 'Water Definitions'), None)
            for x in plan.all_entries:
                if x.group != 'sbsp' or x.action != 'convert' or x.src_map is None or wf is None or not any(id(c) in water_shads for c in x.children):
                    continue
                try:
                    cache, vt = plan.source_of(x)
                    n = struct.unpack_from('<i', cache.read_addr(vt.addr + wf.offset, 4))[0]
                except Exception:                                      # noqa: BLE001
                    continue
                rep['sbsp_water_definitions'][x.name] = n
                if n <= 0:
                    rep['no_water_definition'].append({'sbsp': x.name, 'water_definitions': n})
        return rep

    def placeholder_roots(self):
        """The input's instance table lists shared tags nothing references (authoring data the cache strips); keep those MCC has."""
        n = 0
        if not self.opts['placeholders_as_roots'] or self.vista.header.type != 1:
            self.plan.notes['placeholder_roots'] = 0
            return
        sh = self.env.shared
        for t in self.vista.tags:
            if t.is_empty or t.addr or t.index < FIRST_SHARED_TAG_INSTANCE_INDEX or t.index >= len(sh.tags):
                continue
            st = sh.tags[t.index]
            if st.datum != t.datum or not st.addr or not st.name:
                continue
            if RevPlan._key_for(st.group, st.name) in self.by_name or st.group in RESOURCE_GROUPS or st.group in DONOR_REQUIRED:
                continue
            if self.mcc.find(st.group, st.name, kinds=SHARED_KINDS) is None:
                continue
            self.require(st.group, st.name, vtag=st, vlabel='shared', origin='placeholder')
            n += 1
        self.plan.notes['placeholder_roots'] = n

    def handle_orphans(self):
        keep = self.opts['orphans'] == 'keep'
        orphans = []
        for t in self.vista.tags:
            if t.is_empty or not t.addr or t.group in RESOURCE_GROUPS or t.group in DONOR_REQUIRED:
                continue                       # shader code of the input is never carried over: MCC's stems bring their own passes
            k = RevPlan._key_for(t.group, self.tag_name('src', t))
            if k in self.by_name:
                continue
            orphans.append(t)
        # reachable only through dropped scenario-resource tags?
        res_reach = set()
        for t in self.vista.tags:
            if t.is_empty or not t.addr or t.group not in RESOURCE_GROUPS:
                continue
            for g, d, off in self.env.scan_vista.scan(self.vista.read_addr, t.group, t.addr):
                r = self.plan.resolve_vista('src', d)
                if r is not None and r[0] == 'src':
                    res_reach.add(r[1].index)
        kinds = collections.Counter()
        for t in orphans:
            via_res = t.index in res_reach
            kinds['resource-only' if via_res else 'unreferenced'] += 1
            if keep:
                self.require(t.group, self.tag_name('src', t), vtag=t, vlabel='src', origin='orphan')
        self.plan.notes['orphans'] = {'count': len(orphans), 'kept': keep, 'kinds': dict(kinds),
                                      'groups': dict(collections.Counter(t.group for t in orphans).most_common(12))}

    def gestalt_edges(self):
        """Implicit references of the merged gestalt: its Reflections block (snd! tags) and, optionally, the remastered twin of every
        donor sound.  The gestalt entry gets them as children so the closure keeps them."""
        g = self.plan.gestalt
        if g is None:
            return
        add = []
        if self.opts['gestalt_reflections']:
            for nm in self.env.gestalt_reflection_names():
                add.append(('snd!', nm))
        if self.opts['remastered_twins']:
            for e in list(self.plan.all_entries):
                if e.action == 'mcc_donor' and e.group in ('snd!', 'lsnd') and e.name.lower().startswith('sound\\'):
                    nm = 'sound_remastered\\' + e.name[len('sound\\'):]
                    if self.mcc.has_name(e.group, nm):
                        add.append((e.group, nm))
        seen = set(id(c) for c in g.children)
        for grp, nm in add:
            ch = self.require(grp, nm, mcc_label='shared', origin='gestalt')
            if id(ch) not in seen and ch is not g:
                g.children.append(ch)
                seen.add(id(ch))

    def shadowed(self):
        out = []
        for k, t in self.local.items():
            if t.group in RESOURCE_GROUPS or t.group == 'ugh!' or k not in self.shared_ix:
                continue
            e = self.by_name.get(k)
            out.append({'group': t.group, 'name': t.name, 'action': e.action if e else None})
        return out

    # ---- numbering ----------------------------------------------------------------------------------------------------
    def finish(self):
        plan = self.plan
        kept = [e for e in plan.all_entries if e.action in ('convert', 'mcc_donor')]
        kept_set = set(id(e) for e in kept)
        order = []
        placed = set()

        def place(e):
            if id(e) not in placed and id(e) in kept_set and e is not plan.gestalt:
                placed.add(id(e))
                order.append(e)

        visited = set()

        def dfs(root):
            stack = [root]
            while stack:
                e = stack.pop()
                if id(e) in visited or e is plan.gestalt:
                    continue
                visited.add(id(e))
                if e.action == 'drop':
                    if e.redirect is not None:
                        stack.append(e.redirect)
                    continue
                place(e)
                for c in reversed(e.children):
                    stack.append(c)

        for g, nm in PREFIX_TAGS:
            e = self.by_name.get(_nk(g, nm))
            if e is not None:
                place(e)
        place(plan.scenario)
        dfs(plan.scenario)
        if plan.globals_entry is not None:
            dfs(plan.globals_entry)
        for e in kept:
            place(e)
        if plan.gestalt is not None:
            placed.add(id(plan.gestalt))
            order.append(plan.gestalt)
        if len(order) > MAX_TAGS:
            raise RevPlannerError('%d output tags exceed the engine limit of %d (a tag count >= 17000 makes MCC load shared.map tables)' % (
                len(order), MAX_TAGS))
        if len(order) > 12000:
            plan.warn('many_tags', '%d tags (the largest H2EK-built map has 10177)' % len(order))
        for k, e in enumerate(order):
            e.out_index = k
            e.out_datum = rev_datum(k)
        plan.entries = order
        if plan.gestalt is not None:
            plan.gestalt.merge_sources = self.gestalt_sources()
        plan.stats['tags'] = len(order)

    def gestalt_sources(self):
        """Gestalts that hold the sounds of the plan: the sound handler merges them into the single output ugh!.  Primary first."""
        plan = self.plan
        out = []
        seen = set()

        def add(rec, key):
            if key not in seen:
                seen.add(key)
                out.append(rec)

        sh_idx = self.env.shared_ugh_index() if self.map_type == 1 else None
        src_ugh = next((t.index for t in self.vista.tags if not t.is_empty and t.addr and t.group == 'ugh!'), None)
        snds = [e for e in plan.entries if e.group == 'snd!']
        if any(e.action == 'convert' and e.src_map == 'shared' for e in snds) and sh_idx is not None:
            add({'side': 'vista', 'map': 'shared', 'index': sh_idx}, ('v', 'shared'))
        if any(e.action == 'convert' and e.src_map == 'src' for e in snds) and src_ugh is not None:
            add({'side': 'vista', 'map': 'src', 'index': src_ugh}, ('v', 'src'))
        for e in snds:
            if e.action == 'mcc_donor':
                c = self.mcc.open(e.donor.label)
                g = c.sound_gestalt
                if g is not None:
                    add({'side': 'mcc', 'label': e.donor.label, 'index': g.index}, ('m', e.donor.label))
        return out


def build_rev_plan(vista, env=None, opts=None):
    """vista: VistaCache or a path.  env: RevEnv (shared between maps; default RevEnv()).  opts: see DEFAULT_OPTS."""
    o = dict(DEFAULT_OPTS)
    o.update(opts or {})
    own_cache = False
    if env is None:
        env = RevEnv(use_h2ek=o['use_h2ek_donors'], use_stock_locals=o['use_stock_locals'])
    if isinstance(vista, str):
        if not os.path.isfile(vista):                                   # a bare map name: look in the Vista maps folder
            vista = os.path.join(env.vista_maps, vista if vista.lower().endswith('.map') else vista + '.map')
        vista = VistaCache(vista)
        own_cache = True
    b = _Builder(vista, env, o)
    plan = b.run()
    plan._own_cache = own_cache
    return plan


# ======================================================================================================
# coverage helpers
# ======================================================================================================
def shader_family_names(vista):
    """{group: {name_l: (name, [Vista tag indices])}} of the vrtx / spas / stem tags that carry data in a Vista map."""
    out = {g: {} for g in DONOR_REQUIRED}
    for t in vista.tags:
        if t.is_empty or not t.addr or t.group not in out or not t.name:
            continue
        out[t.group].setdefault(t.name.lower(), (t.name, []))[1].append(t.index)
    return out


def h2ek_has_tag(group, name, tags_dir=None):
    """Does the H2EK tag set hold the tag file for a shader-family cache tag name?  (Diagnostics only; False unless an H2EK folder is configured.)"""
    ext = {'vrtx': '.vertex_shader', 'spas': '.shader_pass', 'stem': '.shader_template'}.get(group)
    tags_dir = tags_dir or _paths.h2ek_tags_dir()
    if ext is None or not tags_dir or not os.path.isdir(tags_dir):
        return False
    return os.path.isfile(os.path.join(tags_dir, name + ext))


def donor_coverage(names_by_group, env):
    """Coverage of shader-family names by MCC tiers.  -> {group: {'total': n, 'by_tier': {kind: n}, 'kit_only': [...], 'missing': [...]}}"""
    mcc = env.mcc.build()
    res = {}
    for g, names in names_by_group.items():
        by_tier = collections.Counter()
        kit_only, missing = [], []
        for nl, (name, _) in names.items():
            d = mcc.find(g, name)
            if d is not None:
                by_tier[d.kind] += 1
            elif h2ek_has_tag(g, name):
                kit_only.append(name)
            else:
                missing.append(name)
        res[g] = {'total': len(names), 'by_tier': dict(by_tier), 'kit_only': sorted(kit_only), 'missing': sorted(missing)}
    return res


def suggest_stem_fallbacks(missing_stems, env):
    """UNVERIFIED suggestions {vista stem name: mcc stem name} for Vista shader templates MCC does not have (opts['stem_fallback']).
    Heuristic: among the MCC stems, prefer the same directory and the best token overlap (tokens of the file name split at '_'); a
    candidate whose tokens are a subset of the source's ('tex_bump' for 'tex_bump_no_specular') wins over a larger one.  Whether the
    shader parameters of such a substitute fit is for the shader handler / the game to decide."""
    mcc = env.mcc.build()
    cands = sorted(set(d.name for d in mcc.by_group['stem']))
    out = {}
    for nm in missing_stems:
        parts = nm.split('\\')
        d, base = '\\'.join(parts[:-1]), parts[-1]
        toks = set(base.split('_'))
        best, best_score = None, 0.0
        for c in cands:
            cp = c.split('\\')
            ct = set(cp[-1].split('_'))
            inter = len(toks & ct)
            if not inter:
                continue
            score = inter / float(len(toks | ct))
            if ct <= toks:
                score += 0.25                                       # a less featured template of the same family
            if '\\'.join(cp[:-1]) == d:
                score += 0.5
            if score > best_score:
                best, best_score = c, score
        if best is not None:
            out[nm] = (best, round(best_score, 2))
    return out


def mcc_closure_names(mcc_cache, env, roots=None):
    """Names {(group, name_l)} reachable from the scnr + matg of an MCC map in MCC space (externals resolved through the stock
    shared.map by name) - the oracle set a reverse plan of the same map has to cover."""
    mcc = env.mcc.build()
    scan = env.scan_mcc
    seen = {}
    stack = []
    for d in (mcc_cache.scenario_datum, mcc_cache.globals_datum):
        t = mcc_cache.tag_by_datum(d)
        if t is not None:
            stack.append((mcc_cache, t))
    shared = None
    while stack:
        c, t = stack.pop()
        k = _nk(t.group, t.name)
        if k in seen:
            continue
        seen[k] = (c.source_path, t.index)
        cc, tt = c, t
        if t.external:                                              # stock MP maps: the data is in shared.map (same name)
            d = mcc.find(t.group, t.name, kinds=('shared',))
            if d is None:
                continue
            cc, tt = mcc.tag(d)
        elif not t.has_data:                                        # MCC 'empty' tag (size 0, aliased address): nothing to follow
            continue
        if tt.group not in env.lay_mcc:
            continue
        for g, dat, off in scan.scan(lambda a, n, cc=cc: cc.read_at_addr(a, n), tt.group, tt.addr):
            x = cc.tag_by_datum(dat)
            if x is not None and x.name:
                stack.append((cc, x))
    return seen


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print('usage: py -3 -m h2conv.rev.planner <vista map (path or name in the Vista maps folder)> [key=value ...] [--report out.json]')
        return 2
    path = argv[0]
    if not os.path.isfile(path):
        path = os.path.join(_paths.vista_maps_dir(), path if path.lower().endswith('.map') else path + '.map')
    opts = {}
    rep = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == '--report':
            rep = argv[i + 1]
            i += 2
            continue
        if '=' in a:
            k, v = a.split('=', 1)
            opts[k] = {'true': True, 'false': False}.get(v.lower(), v)
        i += 1
    plan = build_rev_plan(path, None, opts)
    r = plan.report()
    print(json.dumps(r['summary'], indent=1))
    print('by action:', {a: sum(c.values()) for a, c in r['by_action'].items()})
    print('shader code:', {g: (v['names'], v['donor'], v['no_donor_count']) for g, v in r['shader_code'].items()})
    print('refs %d, null by design %d, unexplained %d' % (r['refs'], r['null_refs_by_design'], r['null_refs_unexplained']))
    if rep:
        with open(rep, 'w', encoding='utf-8') as f:
            json.dump(r, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    sys.exit(main())
