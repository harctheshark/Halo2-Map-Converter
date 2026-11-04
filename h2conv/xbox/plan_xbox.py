# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Planner for Xbox -> Vista: which tags of the Xbox map LINK to the Vista shared.map copy and which are CONVERTED into the map.

Xbox multiplayer maps carry every tag locally; stock Vista multiplayer maps keep only their own content local and link the rest
to shared.map (stock headlong: 458 local, 4,680 shared).  bulldog.map: 4,269 of its 4,975 tags have the same (group, name) in the
pack's Xbox shared.map AND in Vista's shared.map.  The rule, as for MCC inputs (h2conv.planner):

  link     the tag equals the pack's Xbox shared.map copy (same content: struct bytes, references by (group, name), string ids by
           text, raw data by bytes; LOD2/3, Havok User Data and placement words ignored) - or, through stock_index, a stock Xbox copy
           of its name (a name the pack lacks; a jmad's MP cache-build trim: equal_to_pack) - Vista's shared.map has a tag of that
           name, and every tag it references links too (taint);
  convert  everything else (map-specific, modified, or depending on a converted tag; a tag converted only through that taint whose own
           content was verified equal is marked tainted_only, and convert_xbox builds it from Bungie's Vista port, w3);
  vista    shader family (stem / spas / vrtx): never from the Xbox (Xbox GPU code); always the Vista tag of the same name (a clone
           'Copy of X' / 'X (1)' / 'X_dupN': X's, family_name);
  drop     shader family with no Vista tag of that name (references become NONE; reported).

The Xbox shared.map of the PACK is the "stock" reference: the mod's own map is compared with the mod's own shared files.
"""
import collections
import hashlib
import os
import re
import struct

from .. import tagtree as T
from ..vista_cache import VistaCache
from . import layouts as XL
from . import sound_xbox as SX
from .xbox_cache import XboxCache, NULL, LOC_FILES

SHADER_FAMILY = ('stem', 'spas', 'vrtx', 'pixl')
NEVER_LINK = ('scnr', 'sbsp', 'ltmp', 'ugh!')  # the map's own content (ugh! = the map's secondary gestalt, as in stock Vista MP maps)
# groups whose stock MULTIPLAYER copies are a cache-build TRIM of the shared.map tag (equal_to_pack): every stock Xbox MP map's jmad
# masterchief / elite / dervish has 654 / 893 / 139 of shared.map's 662 / 963 / 140 animations (the missing ones: combat:missile:fc:*,
# h_turret_mp_d:*, corner_cover_*), each kept one with the same name, checksums, frame / node counts and data (a probe, foundry
# + stock lockout, 2026-10-26).  Bungie's Vista MP maps link the full tag (0 of the 7,700 local tags of the 25 stock Vista MP maps
# share a name with Vista's shared.map, a probe); converting the trim tainted every hlmt masterchief_mp, which then kept
# the Xbox LOD / fade distances (verify-foundry-spartan)
TRIMMED_GROUPS = ('jmad',)


def _nk(group, name):
    return (group, (name or '').lower())


XBOX_VS_PREFIX = 'rasterizer\\vertex_shaders_xbox\\'
DX9_VS_PREFIX = 'rasterizer\\vertex_shaders_dx9\\'


def vista_name(group, name):
    """The name the Vista tag of an Xbox tag has: the engine vertex shaders live under vertex_shaders_dx9 on Vista."""
    if group == 'vrtx' and name and name.lower().startswith(XBOX_VS_PREFIX):
        return DX9_VS_PREFIX + name[len(XBOX_VS_PREFIX):]
    return name


# clone marks: old Xbox tools copied a template under 'Copy of <name>' or '<name> (1)', and dedupe_names renames a second tag of one name
# '<name>_dup<n>'.  Echo_Station's 'Copy of shaders\shader_templates\opaque\prt_simple' (+ 42 'Copy of' spas, 73 vrtx) and UMconst /
# tramp's 'shaders\shader_templates\opaque\tex_bump_env (1)' hold the stock template's content, but no Vista tag has that name, so
# their shaders' references became NONE (audit-tag-tricks RC6, 2026-10-26)
_CLONE_PREFIX = re.compile(r'^copy of\s+', re.I)
_CLONE_SUFFIX = re.compile(r'(\s*\(\d+\)|_dup\d+)$', re.I)


_TEMPLATE_DIR = 'shaders\\shader_templates\\'


def base_name(name):
    """name without its clone marks ('Copy of ', ' (N)', '_dupN', repeated), and a shader template without a folder put in front of
    shaders\\shader_templates\\: Tortuga's water_development\\shaders\\shader_templates\\water\\water_static is Bungie's water_static
    (every postprocess block equal but the per-map pass datum), and its WaterZ BSP water was set to NONE - no water plane (2026-11-02)."""
    n, prev = name or '', None
    while n != prev:
        prev, n = n, _CLONE_SUFFIX.sub('', _CLONE_PREFIX.sub('', n))
    i = n.lower().find(_TEMPLATE_DIR)
    return n[i:] if i > 0 else n


def family_name(group, name, has):
    """The Vista name of a shader-family tag (stem / spas / vrtx / pixl): vista_name, else - when has(group, that name) is False - the
    clone's base name if has() knows it.  has(group, name) = a Vista tag of that name exists."""
    n = vista_name(group, name)
    if has(group, n):
        return n
    b = vista_name(group, base_name(name))
    return b if b.lower() != (n or '').lower() and has(group, b) else n


class Entry(object):
    __slots__ = ('index', 'group', 'name', 'action', 'why', 'vista_index', 'vista_datum', 'out_index', 'out_datum', 'verified',
                 'tainted_only')

    def __init__(self, t):
        self.index = t.index
        self.group = t.group
        self.name = t.name
        self.action = None
        self.why = ''
        self.verified = False            # linked because its content EQUALS the pack shared.map / a stock Xbox copy (not unverifiable)
        self.tainted_only = False        # converted only by taint, its own content verified equal (convert_xbox.vista_port_of_tainted)
        self.vista_index = None
        self.vista_datum = None
        self.out_index = None
        self.out_datum = None


class XboxPlan(object):
    def __init__(self, xc, pack_shared, vista_shared, log=None, strict=False, stock=None):
        self.strict = strict              # True: unverifiable tags are converted instead of linked
        self.xc = xc
        self.ps = pack_shared
        self.vs = vista_shared
        self.stock = stock                # stock_index.StockIndex of the stock Xbox maps (None: the pack shared.map only)
        self._digester = None
        self.log = log or (lambda m: None)
        self.lay = XL.xbox_layouts()
        self.entries = {}
        self.stats = collections.Counter()
        self._ps_name = {_nk(t.group, t.name): t for t in self.ps.tags if t.addr} if self.ps else {}
        self._vs_name = {}
        for t in self.vs.tags:
            if t.addr and t.name:
                self._vs_name.setdefault(_nk(t.group, t.name), t)
        self._refs = {}
        self._gestalt_x = self._gestalt_p = None
        self._loc_x = self._loc_p = None

    # ---- references of a tag (datums) -----------------------------------------------------------------------------
    def refs(self, cache, t):
        key = (id(cache), t.index)
        r = self._refs.get(key)
        if r is None:
            root = T.decode_tag(cache.read, self.lay[t.group], t.addr, t.group)
            r = self._refs[key] = (root, _tag_refs(root))
        return r

    # ---- equality with the pack's shared copy ------------------------------------------------------------------------
    def digest(self, t):
        """stock_index content digest of the map's tag t (None: no layout / undecodable)."""
        from .stock_index import Digester
        if self._digester is None:
            self._digester = Digester(self.xc)
        try:
            root = self.refs(self.xc, t)[0] if t.group in self.lay else None
            return self._digester.tag(t, root)
        except Exception:                                                # noqa: BLE001
            return None

    def equal_to_stock(self, t, mp_only):
        """(True|False|None, stock map): t against the stock Xbox copies of its name (stock_index).  None = no copy / no digest."""
        if self.stock is None or not self.stock.has(t.group, t.name):
            return None, None
        d = self.digest(t)
        if d is None:
            return None, None
        m = self.stock.match(t.group, t.name, d, mp_only=mp_only)
        return (True, m) if m else (False, None)

    def equal_to_pack(self, t):
        """(True|False|None, why): None = unverifiable (no copy of that name in the pack's shared.map nor in a stock Xbox map).
        True also for a tag equal to a stock Xbox copy of its name (why says which), in two cases:
          * the pack's shared.map has no tag of that name: equal to ANY stock copy (campaign maps included) is stock content; unequal to
            every one is the mod's own content under a stock name ('modified', converted).  Before, every such tag linked unseen: 760
            of the batch's 6,738 differed from every stock copy (audit-tag-tricks RC8);
          * a TRIMMED_GROUPS tag (jmad) whose pack copy differs, but a stock MULTIPLAYER map's copy is equal: the MP cache build's
            animation trim (TRIMMED_GROUPS), which Bungie's Vista port of that map links.  A campaign map's variant is not accepted
            there.  Other groups' MP variants are real per-level content (foot material effects / sounds, the battle_rifle tens / ones
            shaders with 4 bitmaps vs 3, sncl, unic, effe Particle Systems: a probe, 2026-10-26) and keep the Xbox content."""
        p = self._ps_name.get(_nk(t.group, t.name))
        if p is None:
            eq, m = self.equal_to_stock(t, mp_only=False)
            if eq:
                self.stats['stock_equal_unverifiable'] += 1
                return True, 'equal to the stock Xbox %s copy' % m
            if eq is False:
                self.stats['stock_differs_unverifiable'] += 1
                return False, 'modified vs every stock Xbox copy (not in the pack shared.map)'
            return None, 'not in the pack shared.map (unverifiable)'
        eq, why = self._equal_to_pack(t, p)
        if eq is False and t.group in TRIMMED_GROUPS:
            s, m = self.equal_to_stock(t, mp_only=True)
            if s:
                self.stats['stock_mp_variant'] += 1
                return True, 'equal to the stock Xbox %s copy (the pack copy differs: %s)' % (m, why[:80])
        return eq, why

    def _equal_to_pack(self, t, p):
        if t.group not in self.lay:
            return False, 'no layout'
        try:
            if t.group == 'snd!':
                # the 20-byte body indexes each map's own gestalt: compare the resolved content
                if self._gestalt_x is None:
                    self._gestalt_x, self._gestalt_p = SX.Gestalt(self.xc), SX.Gestalt(self.ps)
                fa = self._gestalt_x.fingerprint(self.xc.read(t.addr, 0x14))
                fb = self._gestalt_p.fingerprint(self.ps.read(p.addr, 0x14))
                if fa == fb:
                    return True, ''
                # same audio and parameters, only the import names differ (dawn_of_warfare's re-merged gestalt): the stock sound, linked,
                # rather than a local copy (its Xbox WMA would be copied as ASF, which Vista cannot play)
                if self._gestalt_x.fingerprint(self.xc.read(t.addr, 0x14), names=False) == \
                        self._gestalt_p.fingerprint(self.ps.read(p.addr, 0x14), names=False):
                    self.stats['sound_import_names_differ'] += 1
                    return True, ''
                return False, 'sound content differs'
            if t.group == 'unic':
                # the Strings ranges point into each map's own locale tables: compare the strings
                if self._loc_x is None:
                    self._loc_x, self._loc_p = SX.locale_tables(self.xc), SX.locale_tables(self.ps)
                same = SX.unic_strings(self.xc, t, self._loc_x) == SX.unic_strings(self.ps, p, self._loc_p)
                return (True, '') if same else (False, 'unic strings differ')
        except Exception as ex:                                          # noqa: BLE001
            return False, 'compare error %s' % ex
        try:
            ra, _ = self.refs(self.xc, t)
            rb, _ = self.refs(self.ps, p)
        except Exception as ex:                                          # noqa: BLE001
            return False, 'decode error %s' % ex
        why = []
        if not _same(ra, rb, self.xc, self.ps, why):
            return False, why[0] if why else 'differs'
        return True, ''

    # ---- the plan ------------------------------------------------------------------------------------------------
    def build(self):
        xc = self.xc
        tags = [t for t in xc.tags if not t.is_empty and t.addr]
        for t in tags:
            e = self.entries[t.index] = Entry(t)
            vt = self._vs_name.get(_nk(t.group, vista_name(t.group, t.name)))
            if t.group in SHADER_FAMILY:
                vt = self._vs_name.get(_nk(t.group, family_name(t.group, t.name, lambda g, n: _nk(g, n) in self._vs_name)))
                if vt is not None:
                    e.action, e.why, e.vista_index, e.vista_datum = 'vista', 'shader family: Vista shared.map tag', vt.index, vt.datum
                else:
                    e.action, e.why = 'vista_donor', 'shader family: no Vista shared.map tag of this name (donor or drop)'
                continue
            if t.group in NEVER_LINK or vt is None:
                e.action, e.why = 'convert', 'the map\'s own tag' if t.group in NEVER_LINK else 'not in Vista shared.map'
                continue
            eq, why = self.equal_to_pack(t)
            if eq is None and not self.strict:
                e.action, e.why, e.vista_index, e.vista_datum = 'link', 'in Vista shared.map; ' + why, vt.index, vt.datum
            elif eq:
                e.action, e.why, e.vista_index, e.vista_datum = 'link', why or 'equal to the pack shared.map copy', vt.index, vt.datum
                e.verified = True
            else:
                e.action, e.why = 'convert', why if why.startswith('modified') else 'modified vs the pack shared.map: ' + why
        # taint: a linked tag whose reference is converted must be converted too (a shared tag cannot point at a local one)
        changed = True
        while changed:
            changed = False
            for e in self.entries.values():
                if e.action != 'link':
                    continue
                try:
                    _, refs = self.refs(xc, xc.tags[e.index])
                except Exception:                                        # noqa: BLE001
                    # junk block pointers (protected maps: skull_island, umconst, duffworld): the plan decodes strictly, so this tag stays
                    # linked (the Vista tag of its name) and its references taint nothing.  It is never salvaged: build_node's salvage
                    # ladder only sees tags the plan converts - a pack-verifiable undecodable tag converts ('decode error') and IS
                    # salvaged, an unverifiable one is not (6 link_undecodable in batch_v4)
                    if e.why.endswith('(unverifiable)'):
                        e.why += '; undecodable (junk pointers)'
                        self.stats['link_undecodable'] += 1
                    refs = ()
                for d in refs:
                    tt = xc.tag_by_datum(d)
                    if tt is None:
                        continue
                    r = self.entries.get(tt.index)
                    # shader-family references never taint: the linked Vista tag carries its own Vista shader references
                    if r is not None and r.action == 'convert':
                        # its own content was verified equal (not an unverifiable link): Bungie's Vista port of it can stand in
                        # (convert_xbox.vista_port_of_tainted, w3)
                        e.tainted_only = e.verified
                        e.action, e.why = 'convert', 'depends on converted %s %s' % (r.group, r.name)
                        e.vista_index = e.vista_datum = None
                        changed = True
                        break
        for e in self.entries.values():
            self.stats['%s' % e.action] += 1
            self.stats['%s:%s' % (e.action, e.group)] += 1
            if e.tainted_only:
                self.stats['tainted_only'] += 1
                self.stats['tainted_only:%s' % e.group] += 1
        return self


def _tag_refs(root):
    out = set()
    for n in root.iter_nodes():
        for f in n.fields:
            if f.type.lower() == 'tagref':
                d = struct.unpack_from('<I', n.data, f.offset + (4 if f.size == 8 else 0))[0]
                if d not in (0, NULL):
                    out.add(d)
    return out


def _label(cache, d):
    if d in (0, NULL):
        return None
    t = cache.tag_by_datum(d)
    return (t.group, (t.name or '').lower()) if t is not None else ('?', d)


RAW_FIELD_NAMES = {'Resource Block Offset', 'LOD1 Offset', 'LOD2 Offset', 'LOD3 Offset', 'File Offset', 'Offset'}
# raw pointer field -> the field holding the byte count of its data (jmad Tag Resource Groups: {+4 Size, +8 Offset}).  ugh! File Offset
# (size word with flags) has none: compared by pointer only (the gestalt is the map's own, NEVER_LINK)
RAW_SIZE_FIELD = {'LOD1 Offset': 'LOD1 Size', 'Resource Block Offset': 'Resource Block Size', 'Offset': 'Size'}
# per-map placement, not content: the bitmap's slot in the map's own low-detail atlas (header 0x154..0x15C)
IGNORED_FIELD_NAMES = {'Low-Detail Offset', 'Low-Detail Size'}
# bitm: the converter reads LOD1 only - the complete mip chain; LOD2 / LOD3 are the same chain from mip 1 / 2 and are written 0
# (bitmaps_xbox 'Only LOD1 is needed').  Campaign-built and MP-built copies of one stock bitmap differ there (LOD2 present or not), and
# H3ascension's 481 'modified' stock bitmaps were 450 moved / dropped LOD2-3 pointers over byte-identical pixels (audit-tag-tricks RC3,
# 2026-10-26)
IGNORED_GROUP_FIELDS = {'bitm': {'LOD2 Offset', 'LOD3 Offset', 'LOD2 Size', 'LOD3 Size'}}
# Havok shape 'User Data' (phmo Spheres / Pills / Boxes / Lists / MOPPs / Phantoms ..., bipd / crea Sphere / Pill Shapes, vehi Phantom
# Shapes, coll / sbsp BSP Physics): a pointer into the TOOL's heap at cache-build time, not content.  phmo masterchief Pills +0x28 reads
# 0x0C1F2B90, +0x50 per element in Foundry and stock gemini, 0x0B5C58C0.. in shared.map, 0x0BD9AEA0.. in lockout, 0x0E241AB0.. in Vista
# shared.map (a probe, 2026-10-26).  Foundry's stock bipd / phmo masterchief_mp were 'modified' only there, which tainted
# hlmt masterchief_mp: it converted with the Xbox LOD / fade distances (disappear 40, fade 30, L1-L4 10/5/2.8/1.8 vs Vista's 150/130,
# 30/20/15/8) and Spartans vanished at 40 wu on PC (verify-foundry-spartan, 2026-10-26).  Not sufficient on its own: hlmt masterchief_mp
# stayed tainted through mode masterchief's genuine LOD trick in 145 batch_v4 maps (batch_v4 audit); the converter
# now builds such a tainted-only tag from Bungie's Vista port (Entry.tainted_only, convert_xbox.vista_port_of_tainted, w3 2026-10-27)
RUNTIME_FIELD_NAMES = {'User Data'}
# matg: every multiplayer map nulls the campaign UI globals that shared.map's matg points at (stock MP difference, not a mod)
IGNORED_REFS = {('matg', '/Interface Tags', 'Single Player UI Globals')}


def field_kind(n, f):
    """How _same / content_digest treat field f of node n: 'block', 'data', 'ref', 'sid', 'raw', 'skip' (placement / runtime / never
    emitted) or 'plain' (compared byte for byte with the rest of the struct)."""
    t = f.type.lower()
    name = f.name or ''
    if f.type in T.BLOCK_TYPES:
        return 'block'
    if f.type in T.DATA_TYPES:
        return 'data'
    if t == 'tagref':
        return 'skip' if (n.group, n.path, name) in IGNORED_REFS else 'ref'
    if t == 'stringid':
        return 'sid'
    if name in IGNORED_FIELD_NAMES or 'Runtime' in name or name.endswith('Pointer') or name in RUNTIME_FIELD_NAMES or \
            name in IGNORED_GROUP_FIELDS.get(n.group, ()):
        return 'skip'
    if name in RAW_FIELD_NAMES and f.size == 4:
        return 'raw'
    return 'plain'


def raw_size(n, f):
    """Byte count of the raw data behind raw pointer field f of node n (None: unknown)."""
    sf = n.field(RAW_SIZE_FIELD[f.name]) if f.name in RAW_SIZE_FIELD else None
    if sf is None or sf.size != 4:
        return None
    return n.u32(sf.offset) & 0x3FFFFFFF


def _raw_none(p):
    return p in (0, NULL)


def raw_equal(ca, pa, sa, cb, pb, sb):
    """Raw data equality across two caches: the same file and offset (an Xbox map's location-2 pointer and the pack shared.map's own
    location-0 pointer to that offset), else the same BYTES (size sa / sb).  A stock map's level-local copy keeps its raw in THAT map
    (location 0) while the mod points into shared.map, and rebuilt maps moved LOD1 / geometry / jmad data without changing it: by
    pointer value those were 'modified' (audit-tag-tricks RC3, a probe same_content).  The one exception to 'same file +
    offset': a pixel edit the pack made in place in its own mainmenu.map (mainmenu_edited_in_place, RC10)."""
    if _raw_none(pa) or _raw_none(pb):
        return _raw_none(pa) and _raw_none(pb)
    if (pa & 0x3FFFFFFF) == (pb & 0x3FFFFFFF) and os.path.normcase(ca.loc_path(pa >> 30)) == os.path.normcase(cb.loc_path(pb >> 30)):
        return not mainmenu_edited_in_place(ca, pa, sa, cb.fallback_dirs)
    if sa is None or sa != sb:
        return False
    try:
        return ca.raw(pa, sa) == cb.raw(pb, sb)
    except Exception:                                                    # noqa: BLE001
        return False


_MAINMENU_SLOTS = {}


def mainmenu_edited_in_place(c, p, size, more_dirs=()):
    """Is the location-1 raw data at pointer p (size bytes) a PIXEL EDIT the pack made in its own mainmenu.map?  Every pack ships a
    modified mainmenu.map while its shared.map is the stock one (md5), and the modders re-skinned UI bitmaps there in place (fs_dialog,
    the dialog / virtual keyboard buttons, list skins, loading_screen_spinners, map_shots): an Xbox tag and the pack shared.map copy then
    point at the same offset of the same file, raw_equal called them equal and 1,758 re-skinned bitmaps were linked to the stock Vista
    ones (audit-tag-tricks RC10, a probe, 2026-10-26).  True only when the bytes differ from the STOCK Xbox mainmenu.map
    (c's fallback folders: the stock maps) at the same offset AND an element of a pack-mainmenu bitmap has its own data at exactly that
    offset and size - an in-place edit, never a rebuilt / extended mainmenu whose offsets moved (the verifier's guard)."""
    if p >> 30 != 1 or not size:
        return False
    pack = os.path.normcase(os.path.abspath(c.loc_path(1)))
    stock = next((os.path.abspath(os.path.join(d, LOC_FILES[1])) for d in tuple(c.fallback_dirs) + tuple(more_dirs)
                  if os.path.isfile(os.path.join(d, LOC_FILES[1]))), None)
    if stock is None or os.path.normcase(stock) == pack or not os.path.isfile(pack):
        return False
    slots = _MAINMENU_SLOTS.get((pack, os.path.normcase(stock)))
    if slots is None:
        slots = set()
        try:
            mc = XboxCache(pack, shared_dir=os.path.dirname(pack))
            try:
                lb = XL.xbox_layouts()['bitm']
                for t in mc.tags:
                    if t.group != 'bitm' or not t.addr:
                        continue
                    try:
                        r = T.decode_tag(mc.read, lb, t.addr, 'bitm')
                    except Exception:                                    # noqa: BLE001
                        continue
                    for e in r.block('Bitmaps'):
                        for k in ('LOD1', 'LOD2', 'LOD3'):
                            fo, fs = e.field(k + ' Offset'), e.field(k + ' Size')
                            if fo is not None and fs is not None and e.u32(fo.offset) not in (0, NULL) and e.u32(fo.offset) >> 30 == 0:
                                slots.add((e.u32(fo.offset) & 0x3FFFFFFF, e.u32(fs.offset) & 0x3FFFFFFF))
            finally:
                mc.close()
        except Exception:                                                # noqa: BLE001
            slots = set()
        _MAINMENU_SLOTS[(pack, os.path.normcase(stock))] = slots
    o = p & 0x3FFFFFFF
    if (o, size) not in slots:
        return False
    try:
        with open(stock, 'rb') as fh:
            fh.seek(o)
            ref = fh.read(size)
        return len(ref) == size and c.raw(p, size) != ref
    except Exception:                                                    # noqa: BLE001
        return False


def _same(a, b, ca, cb, why):
    if len(a.data) != len(b.data):
        why.append('%s size' % a.path)
        return False
    skip = set()
    for f in a.fields:
        k = field_kind(a, f)
        if k == 'block':
            ka, kb = a.blocks.get(f.offset) or [], b.blocks.get(f.offset) or []
            if len(ka) != len(kb):
                why.append('%s/%s count %d vs %d' % (a.path, f.name, len(ka), len(kb)))
                return False
            for x, y in zip(ka, kb):
                if not _same(x, y, ca, cb, why):
                    return False
            skip.update(range(f.offset, f.offset + 8))
        elif k == 'data':
            if (a.datas.get(f.offset) or b'') != (b.datas.get(f.offset) or b''):
                why.append('%s/%s data' % (a.path, f.name))
                return False
            skip.update(range(f.offset, f.offset + 8))
        elif k == 'ref':
            o = f.offset + (4 if f.size == 8 else 0)
            if _label(ca, struct.unpack_from('<I', a.data, o)[0]) != _label(cb, struct.unpack_from('<I', b.data, o)[0]):
                why.append('%s/%s reference' % (a.path, f.name))
                return False
            skip.update(range(o, o + 4))
        elif k == 'sid':
            if ca.string_id(struct.unpack_from('<I', a.data, f.offset)[0]) != cb.string_id(struct.unpack_from('<I', b.data, f.offset)[0]):
                why.append('%s/%s string id' % (a.path, f.name))
                return False
            skip.update(range(f.offset, f.offset + 4))
        elif k == 'skip':
            skip.update(range(f.offset, f.offset + f.size))
        elif k == 'raw':
            # raw pointers: the same data (by file + offset, or by bytes) is equal
            if not raw_equal(ca, a.u32(f.offset), raw_size(a, f), cb, b.u32(f.offset), raw_size(b, f)):
                why.append('%s/%s raw data' % (a.path, f.name))
                return False
            skip.update(range(f.offset, f.offset + 4))
    for i in range(len(a.data)):
        if i not in skip and a.data[i] != b.data[i]:
            why.append('%s byte 0x%X' % (a.path or '/', i))
            return False
    return True


def content_digest(root, cache):
    """sha1 (20 bytes) of a decoded tag that two tags share exactly when _same says they are equal (up to hash collisions): the same
    field rules, references as (group, lower name) of their targets in `cache`, string ids as text, raw data as the sha1 of its bytes.
    Lets a tag be compared with every stock Xbox copy of its name from a cached index (stock_index) instead of opening and decoding
    each map."""
    h = hashlib.sha1()
    _digest_node(root, cache, h)
    return h.digest()


def _digest_node(n, c, h):
    skip = set()
    h.update(b'N%d|' % len(n.data))
    for f in n.fields:
        k = field_kind(n, f)
        if k == 'block':
            kids = n.blocks.get(f.offset) or []
            h.update(b'B%d|' % len(kids))
            for x in kids:
                _digest_node(x, c, h)
            skip.update(range(f.offset, f.offset + 8))
        elif k == 'data':
            d = bytes(n.datas.get(f.offset) or b'')
            h.update(b'D%d|' % len(d) + d)
            skip.update(range(f.offset, f.offset + 8))
        elif k == 'ref':
            o = f.offset + (4 if f.size == 8 else 0)
            h.update(b'R' + repr(_label(c, n.u32(o))).encode('latin1', 'replace') + b'|')
            skip.update(range(o, o + 4))
        elif k == 'sid':
            h.update(b'S' + repr(c.string_id(n.u32(f.offset))).encode('latin1', 'replace') + b'|')
            skip.update(range(f.offset, f.offset + 4))
        elif k == 'skip':
            skip.update(range(f.offset, f.offset + f.size))
        elif k == 'raw':
            h.update(_raw_token(c, n.u32(f.offset), raw_size(n, f)))
            skip.update(range(f.offset, f.offset + 4))
    h.update(bytes(b for i, b in enumerate(n.data) if i not in skip))


def _raw_token(c, p, size):
    """content_digest's view of one raw pointer (raw_equal's rule): none, the bytes' sha1, or - unknown size / unreadable - the file + offset."""
    if _raw_none(p):
        return b'Pnone|'
    if size is not None:
        try:
            return b'P%d:' % size + hashlib.sha1(c.raw(p, size)).digest() + b'|'
        except Exception:                                                # noqa: BLE001
            pass
    return b'P@' + os.path.normcase(c.loc_path(p >> 30)).encode('utf-8', 'replace') + b':%d|' % (p & 0x3FFFFFFF)


def plan(map_path, vista_maps, pack_dir=None, log=None):
    xc = XboxCache(map_path, shared_dir=pack_dir)
    ps_path = os.path.join(xc.shared_dir, 'shared.map')
    ps = XboxCache(ps_path, shared_dir=xc.shared_dir) if os.path.isfile(ps_path) else None
    vs = VistaCache(os.path.join(vista_maps, 'shared.map'))
    return XboxPlan(xc, ps, vs, log).build()
