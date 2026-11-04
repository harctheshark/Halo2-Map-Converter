# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Halo 2 Xbox (v8, 02.09.27.09809) multiplayer map -> Halo 2 Vista (v8) multiplayer map.

    py -3 -m h2conv.xbox.convert_xbox <xbox map> <out.map> [--vista-maps DIR] [--pack-dir DIR]

Design (probes of the original Xbox maps, 2026-10-21):
* The output is laid out like a stock Vista MP map: the map's own tags local (0..N-1), every Vista shared.map tag as a placeholder at its
  shared index (10000+), the map's own sound gestalt as the SECONDARY ugh! (header 0x2E4), local unic strings in the header language pack.
* plan_xbox decides per Xbox tag: LINK to the Vista shared copy (equal to the pack's Xbox shared.map copy, taint-free), CONVERT, or (shader
  family) take the Vista tag.  Tags are emitted lazily from the roots (scenario, globals, gestalt) through their references, so Xbox-only
  tags nothing reaches (vertex_shaders_xbox ...) never appear.
* shad: Xbox shaders cannot be used (their postprocess mirrors the XBOX template skeletons: 219 of 364 differ from Vista's).  A shader
  whose name exists in a stock Vista map is taken from there (donor copy, Vista closure) - only when its Xbox content IS a stock Xbox
  copy of that name and the plan found no modification (shader_donor_allowed, 2026-10-26); every other one is built (build_shader).
* Converted Xbox tags: the 54 groups without an Xbox plugin override decode straight into the Vista layout; the overrides are re-laid out
  by field name (layouts.py), jmad animations move from the Tag Resource Groups to the inline Resource dataref (759/759 byte-identical,
  size = sum of the 7 size fields at +0x3C), raw data (bitmaps, geometry, sound chunks) is pulled out of the map and the pack's
  mainmenu/shared.map and placed in the output.
* Custom shaders (build_shader): a Vista skeleton on the same template, its static arrays filled from the Xbox shader through a slot map
  learned from Bungie's own Xbox -> Vista ports - the map's pairs plus every stock map's (data\\xbox_shader_slot_maps.json: which Xbox slot
  matches which Vista slot, no values; learn_slot_map).  The skeleton is Bungie's own Vista port of the shader when a stock map
  has one, with only the slots the mod changed; the Xbox overlays when the routing matches; the template from the root or (root NONE)
  the postprocess, clone names ('Copy of X', 'X (1)') by their base; unreadable blocks emptied, never a failed model (decode_shader).
  A layer the map's sky draws with goes on the sky_ template Bungie moved that template's stock sky layers to for PC (output_stem,
  learned 'sky_retemplate': two_alpha_clouds -> sky_two_alpha_clouds ...).
* Gestalt: snd! playback / custom playback indices past the end of their blocks get the elements the Xbox reads there appended
  (extend_past_end_playbacks / extend_past_end_custom_playbacks: the Xbox accessor has no count check).
* A tag whose own layout cannot be read (junk / stale pointers, junk counts, data past the file end) is salvaged block by block
  (salvage_xbox: shift, the stock original's bytes, count clamp, a synthesised collision permutation, zeros, wild pointers emptied in
  place; salvage_tag) instead of being replaced by the Vista tag of its name or killed - unless the salvaged tag is
  implausible (an enum outside its options, a non-finite float, an absurd count: salvage_xbox.plausibility_problems).
* Stock tags take Bungie's Vista port (decision of 2026-10-27): a tag converted ONLY by taint whose own content equals the stock copy is the
  Vista shared.map tag with this map's references (vista_port_of_tainted: Spartan LOD distances); a stock-named tag converted only by
  RC8 whose geometry / sound is broken links to the Vista tag of its name (stock_named_broken).
* Accepted input: a multiplayer map - header scenario type 1, or (24 Phantom maps, 2026-10-26) header 0 with the scenario tag's own
  Type 1 (warning header_scenario_type); a stale tags-header scenario / globals salt resolves by index (warning header_datum_salt).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass

import collections
import glob
import os
import re
import struct
import sys
import time
import zlib

from .. import tagtree as T
from .. import lightmap_gen as LG
from .. import prt_gen as PG
from ..objtype import OBJECT_TYPE_GROUPS, layout_group, pad_alias_root
from .. import paths as _paths
from .. import vista_writer as W
from ..strings import StringTable
from ..vista_cache import VistaCache, VistaTag
from ..handlers import scripts as HS
from ..handlers import locale as LOC
from ..handlers import havok as HK
from . import layouts as XL
from . import plan_xbox as PX
from . import sound_xbox as SX
from . import name_recovery as NR
from . import stock_index as SI
from . import salvage_xbox as SV
from . import packs as PK

# The original Xbox maps (optional reference: paths.stock_xbox_maps_dir, option --stock-xbox-maps) and the cache of their index
# (paths.xbox_index_dir, a sub-folder of the work folder) are looked up when a conversion starts - there is no fixed folder.
# h2conv\data: which Xbox slot matches which Vista slot in Bungie's own Xbox -> Vista shader ports, over all the original maps (mapping
# evidence only: no constant values, see slot_evidence), and the content-keyed widget drops (xbox_widget_drops.json)
SLOT_MAP_FILE = 'xbox_shader_slot_maps.json'
WIDGET_DROPS_FILE = 'xbox_widget_drops.json'
from .xbox_cache import XboxCache, NULL, XboxFormatError, NotHalo2MapError, TruncatedMapError, short_bytes, xbox_kind, scenario_tag_type

# exit codes of the command line (main)
EXIT_OK, EXIT_FAILED, EXIT_SKIPPED, EXIT_BAD_INPUT = 0, 1, 3, 4


class NotMultiplayerError(ValueError):
    """A Halo 2 Xbox map that is not a multiplayer map (campaign, main menu, shared data): multiplayer maps only."""

MP_MASK = 0x1400000
VISTA_BUILD = '11081.07.04.30.0934.main'


def _norm(n):
    return re.sub(r'[^a-z0-9]', '', (n or '').lower())


# the neutral light of radiance-transfer objects (220-byte Lighting Environment) lives with the lightmap generator
no_lightmap_environment = LG.no_lightmap_environment
NO_LIGHTMAP_ENVIRONMENT = no_lightmap_environment()


# ==================================================================================================================
# script tables: Xbox opcodes -> Vista opcodes
# ==================================================================================================================
class XboxHsTables(HS.HsTables):
    """HsTables with the Xbox scripting XML in the 'mcc' slot.  Additions: special forms (begin, if, sleep_until ...) map by NAME (the XMLs
    annotate sleep_until's return type differently: void on Xbox, boolean on Vista, same opcode 0x15); external globals map by name
    (1052 of 1053 Xbox globals have a Vista global of the same name)."""

    def __init__(self):
        x = os.path.join(_paths.assembly_layout().formats_root, 'Halo2Xbox', 'H2X_Scripting.xml')
        super(XboxHsTables, self).__init__(x, HS.default_xml_vista())
        vby = {}
        for op, f in self.vista['fn'].items():
            vby.setdefault(f['name'], op)
        for op, f in self.mcc['fn'].items():
            p = self.fn_plan.get(op)
            if f['special'] and (p is None or p.kind != 'map') and f['name'] in vby:
                self.fn_plan[op] = HS.FnPlan('map', vby[f['name']], None, f['name'], f['ret'], f['args'])
        vg = {g['name']: i for i, g in self.vista['gl'].items()}
        for i, g in self.mcc['gl'].items():
            j = vg.get(g['name'])
            if j is not None and self.vista['gl'][j]['type'] == g['type']:
                self.gl_map[i] = j
                self.gl_verified.add(i)


# ==================================================================================================================
# re-layout of the override groups (by field name; Vista-only fields default to zero / NONE references / empty blocks)
# ==================================================================================================================
def relayout(xnode, vfields, vsize):
    out = T.Node(xnode.group, xnode.path, vfields, vsize)
    xf = {}
    occ = collections.Counter()
    for f in sorted(xnode.fields, key=lambda f: f.offset):
        k = (_norm(f.name), f.type.lower())
        xf[(k, occ[k])] = f
        occ[k] += 1
    occ = collections.Counter()
    for f in sorted(vfields, key=lambda f: f.offset):
        k = (_norm(f.name), f.type.lower())
        src = xf.get((k, occ[k]))
        occ[k] += 1
        if f.type in T.BLOCK_TYPES:
            kids = xnode.blocks.get(src.offset, []) if src is not None and src.type in T.BLOCK_TYPES else []
            es = T.element_size(f)
            out.blocks[f.offset] = [relayout(k_, f.children, es) for k_ in kids]
        elif f.type in T.DATA_TYPES:
            out.datas[f.offset] = (xnode.datas.get(src.offset) or b'') if src is not None and src.type in T.DATA_TYPES else b''
        elif src is not None:
            n = min(f.size, src.size)
            out.data[f.offset:f.offset + n] = xnode.data[src.offset:src.offset + n]
        elif f.type.lower() == 'tagref':
            out.data[f.offset + (4 if f.size == 8 else 0):f.offset + f.size] = b'\xff\xff\xff\xff'
            if f.size == 8:
                out.data[f.offset:f.offset + 4] = b'\xff\xff\xff\xff'
    out.src_addr = xnode.src_addr
    return out


def jmad_animations(xroot, vroot, xc):
    """Xbox Animations element (0x6C) -> Vista (0x60): loop/parent/next from +0x2E/+0x30/+0x32 to +0x20/+0x22/+0x24, +0x3C..+0x4C to
    +0x30..+0x40, Frame Events etc. blocks by name, and the animation bytes from Tag Resource Groups[+0x24].blob[+0x28 : +size]."""
    xa = xroot.block('Animations')
    va = vroot.block('Animations')
    groups = xroot.blocks.get(next(f.offset for f in xroot.fields if f.name == 'Tag Resource Groups'), [])
    blobs = {}
    for k, (x, v) in enumerate(zip(xa, va)):
        v.data[0x20:0x26] = x.data[0x2E:0x34]
        v.data[0x30:0x40] = x.data[0x3C:0x4C]
        gi, go = x.i16(0x24), x.i32(0x28)
        b0, b1, h0, h1, h2, w0, w1 = struct.unpack_from('<BBHHHII', x.data, 0x3C)
        size = b0 + b1 + h0 + h1 + h2 + w0 + w1
        if 0 <= gi < len(groups) and size > 0:
            if gi not in blobs:
                g = groups[gi]
                blobs[gi] = xc.raw(g.u32(8), g.i32(4))
            v.datas[0x28] = blobs[gi][go:go + size]
        else:
            v.datas[0x28] = b''


# ==================================================================================================================
# the converter
# ==================================================================================================================
class OutTag(object):
    __slots__ = ('kind', 'group', 'name', 'src', 'index', 'datum', 'root', 'xindex')

    def __init__(self, kind, group, name, src, index):
        self.kind = kind                 # 'x' (converted Xbox tag) | 'd' (Vista donor copy)
        self.group = group
        self.name = name
        self.src = src                   # Xbox: XboxTag ; donor: (VistaCache, VistaTag)
        self.index = index
        self.datum = W.make_datum(index)
        self.root = None
        self.xindex = src.index if kind == 'x' else None


GUIDE_STOCK = ('Give the folder with your original Halo 2 Xbox maps (the one with lockout.map, ascension.map and the campaign maps): in the '
               'window program in section "4. Games and layout files", row "OG Xbox maps folder", on the command line as --stock-xbox-maps <folder>, or as '
               '"stock_xbox_maps" in settings.json (the environment variable H2CONV_STOCK_XBOX_MAPS works too). The README section '
               '"Converting original Xbox maps" explains it.')
STOCK_MISSING_TEXT = (
    'The map was converted without the original Xbox maps, which costs some quality: '
    '(1) tags the map shares with the original game cannot be compared with it, so an edited tag can pass as unchanged and be replaced by '
    'the standard Halo 2 Vista version (re-textured or edited shaders, models and sounds are lost), and the Spartan model can fade out '
    'at a short distance; '
    '(2) re-skinned menu bitmaps are not recognised and show the standard picture; '
    '(3) a map whose tag names were removed gets fewer of them back (more tags are named generically); '
    '(4) damaged tags cannot be repaired from the original copy and fall back to the standard Vista tag of that name; '
    '(5) textures and models stored in the original single_player_shared.map (a few Killtrocity maps) cannot be read. '
    + GUIDE_STOCK)
STOCK_MISSING_FILES_EFFECT = ('without shared.map the tags the map shares with the original cannot be checked; without mainmenu.map '
                              're-skinned menu bitmaps are not recognised; without single_player_shared.map the textures and models '
                              'stored in it (a few Killtrocity maps) cannot be read. ' + GUIDE_STOCK)


class XboxToVista(object):
    def __init__(self, src, out, vista_maps=None, pack_dir=None, opts=None, log=None):
        self.src = os.path.abspath(src)
        self.out = os.path.abspath(out)
        self.vm = _paths.vista_maps_dir(vista_maps)
        self.pack_dir = pack_dir
        self.opts = dict(opts or {})
        self.log = log or (lambda m: print(m, flush=True))
        # the original Xbox maps folder: opts['stock_xbox_maps'] > --stock-xbox-maps > H2CONV_STOCK_XBOX_MAPS > settings.json "stock_xbox_maps"
        # (None = none given); the index of those maps lives in the work folder (opts['name_index_dir'] overrides it for tests)
        self.stock_dir = _paths.stock_xbox_maps_dir(self.opts.get('stock_xbox_maps'))
        self.index_dir = self.opts.get('name_index_dir') or _paths.xbox_index_dir()
        self.stats = collections.Counter()
        self.warnings = collections.defaultdict(list)
        self.data_twin, self.reindex_as = {}, {}        # find_data_twins
        self.object_nonobject_twins = set()              # find_data_twins (resolve_x alias_ok)
        self.tagref_groups = {}                        # load_tagref_groups (setup)
        self.salvaged = []                               # salvage_tag: one row per salvaged Xbox tag (report 'salvage')

    # ---- small services (also used by the script converter) ----------------------------------------------------------
    def warn(self, code, msg, tag=None):
        self.stats['warn:' + code] += 1
        if len(self.warnings[code]) < 20:
            self.warnings[code].append(msg + (' [%s]' % tag if tag else ''))

    def remap_datum(self, d):
        return self.resolve_x(d)

    def remap_sid(self, sid):
        return self.sid(sid)

    def sid(self, sid):
        if sid in (0, NULL):
            return sid
        text = self.xc.string_id(sid)
        if text is None:
            self.stats['sid_unknown'] += 1
            return 0
        return self.strs.sid(self.out_sid_text(text))

    # the HUD / multiplayer message lists whose mod-edited strings get their own string id names (--unique-mod-strings)
    UNIQUE_STRING_LISTS = ('ui\\hud\\hud_messages', 'multiplayer\\in_game_multiplayer_messages', 'multiplayer\\global_multiplayer_messages')

    def out_sid_text(self, text):
        """The output name of a string id: its own, or '<name>__<map>' for a string the mod edited (opts['unique_mod_strings']).
        MCC's UI draws Halo 2's HUD / multiplayer messages from ITS OWN localization by (string list path, string id NAME): halo2.dll
        sub_1806BEEC0 hands every unic string to the host (qword_181E8CE68 vtable +448) as names only, never the text.  A mod's edit
        under Bungie's name shows MCC's stock text (Tortuga: "Welcome" for "!Ahoy!", 2026-11-02); under a name MCC does not know, the
        host has no entry of its own.  Every reference (unic entries, mulg events, item messages) goes through here, so they stay equal."""
        ren = self.__dict__.get('_sid_renames')
        if ren is None:
            ren = self._sid_renames = self._mod_edited_string_names() if self.opts.get('unique_mod_strings') else {}
        return ren.get(text, text)

    def _mod_edited_string_names(self):
        """{string id name: new name} for the strings of UNIQUE_STRING_LISTS whose text the mod changed (vs the pack shared.map's copy of
        the list, any language).  Names that also appear in another unic tag of the map are left alone (menus share 'header' ...)."""
        if self.ps is None:
            return {}
        suffix = '__' + re.sub(r'[^a-z0-9_]', '_', os.path.splitext(os.path.basename(self.out))[0].lower())[:20]
        tx, tp = SX.locale_tables(self.xc), SX.locale_tables(self.ps)
        pn = {(t.name or '').lower(): t for t in self.ps.tags if t.group == 'unic' and t.addr}
        edited, elsewhere = set(), collections.Counter()
        for t in self.xc.tags:
            if t.group != 'unic' or not t.addr:
                continue
            mine = SX.unic_strings(self.xc, t, tx)
            names = {sid for L in range(9) for sid, _ in mine[L]}
            if (t.name or '').lower() not in self.UNIQUE_STRING_LISTS:
                elsewhere.update(names)
                continue
            p = pn.get((t.name or '').lower())
            if p is None:
                continue
            stock = SX.unic_strings(self.ps, p, tp)
            for L in range(9):
                old = dict(stock[L])
                edited.update(sid for sid, txt in mine[L] if sid and sid in old and old[sid] != txt)
        out = {n: n + suffix for n in sorted(edited) if not elsewhere[n]}
        self.stats['unique_mod_strings'] = len(out)
        if out:
            self.warn('unique_mod_strings', '%d mod-edited HUD / multiplayer string id(s) renamed with %r for MCC (e.g. %s)' % (
                len(out), suffix, ', '.join(list(out)[:4])))
        return out

    def find_cross_group_aliases(self):
        """Old Xbox mods repurposed a tag by pointing its index entry at ANOTHER tag's data (a mod author's "took the pointer data of
        another tag").  aerial: weap ghost_gun / cannon_turret / cannon_turret_mp / minigun_turret share their meta with the deltatemple
        bridge, gondola_launch(2) and sentinelhq detail_keyplug MACHINES; item collections (bridge_big_way ...) spawn them through
        netgame equipment and the Xbox engine builds a machine from the definition.  Decoded as weapons the data is junk, and the
        stock Vista weapon replaced them: the bridges were gone and vehicles fell off the map (2026-10-26).
        The data says what it is: every object definition starts with 'Object Type' (+0), and the four aerial 'weapons' say 7 =
        machine.  alias_of[tag index] = the object group the data's Object Type names, for object tags whose own group differs (same-group
        sharing - identical effects, one vehicle under two names - converts as it is)."""
        self.alias_of = {}
        for t in self.xc.tags:
            if t.is_empty or not t.addr or t.group not in OBJECT_TYPE_GROUPS:
                continue
            want = layout_group(t.group, self.xc.read, t.addr, self.lx)    # only when the data decodes as that group
            if want != t.group:
                self.alias_of[t.index] = want
        if self.alias_of:
            self.warn('cross_group_alias', '%d object tag(s) whose data is another object type, built as that group: %s'
                      % (len(self.alias_of), ', '.join('%s %s = %s' % (self.xc.tags[i].group, self.xc.tags[i].name, g)
                                                       for i, g in list(self.alias_of.items())[:6])))
        self.find_data_twins()

    def family(self, g):
        """{group, parent, grandparent} of a group fourcc (as written, e.g. 'weap' -> {'weap', 'item', 'obje'})."""
        return {x[::-1].decode('latin-1') for x in self.group_family(g.encode('latin-1')[::-1])}

    def find_data_twins(self):
        """Index entries whose data is ANOTHER group's (RC7 / RC2, 2026-10-26).  The Xbox engine
        reads a reference by index alone: default.xbe inlines tag_get as tag_instances[datum & 0xFFFF].address at 1,089 sites with no
        salt and no group check (e.g. 0x143E6-0x143EF: and edx,0FFFFh / shl edx,4 / mov esi,[edx+ebp+8], base dword_4E3B44; read in the
        disassembly), so whatever consumer reads such an entry reads the other tag's bytes.  Vista's
        tag_get 0x4316C3 returns NULL unless the entry's group family contains the requested group, so the output must name the group
        the data really is.
        * data_twin[i] = j: entry i shares its address with entry j of another group whose layout decodes the data STRICTLY, and either
          i's own layout does not, or every 8-byte reference to i declares j's group (none i's).  resolve_x redirects i to j, as the
          Xbox read it: Cable_Room <fx> flak_cannon_drop_soft / flak_cannon_fire_animation = the snd! rocket_launcher_drop_soft /
          banshee_cannon_fire data (foot / jmad Sound References), Satans_Crypt <fx> 'Soul Reaper ' = bitm effects\\bitmaps\\fires\\fire
          (the Soul Reaper handle's Diffuse Map), Biohazard / biohazard_I weap ghost_gun = itmc energy_sword2, Echo_Station weap
          banshee_gun = jpt! ss_exterior_hinge_hammer_shake.  Before, the alias was built from its own layout (a 28-byte junk <fx> in a
          sound / bitmap slot, or convert_failed -> a dead, silent sound) or replaced by the stock Vista weapon.  A field that cannot hold
          the twin's group (a vehicle's Weapon slot -> an itmc) is set NONE by remap_xbox (ref_group_allowed); the Xbox could not have
          built a weapon from item-collection data either (Object Type +0 = 0 / 1).  Object / object pairs are alias_of's (objtype.py).
        * reindex_as[i] = g: no twin, every 8-byte reference to i declares one group g of REINDEX_GROUPS (the model groups, snd!), and
          the data decodes as g with its main blocks (a sound: its gestalt pitch ranges exist): the entry is rebuilt and WRITTEN as g.
          Infiltration <fx> 5131 'panel_truck' is the truck's render model (Regions 1, Sections 4, Nodes 1, Materials 2 as mode; one
          junk Platform Effect Template as <fx>); its only reference, hlmt panel_truck Render Model, declares 'mode' - the Vista hlmt
          pointed at a '<fx>' tag and the truck had no model.  343_Delta_Ruins <fx> flak_cannon_drop_soft / _fire_animation hold
          their own snd! bodies (no twin), referenced as snd! by the flak cannon's foot / jmad Sound References."""
        self.data_twin, self.reindex_as = {}, {}
        xc, lx = self.xc, self.lx
        by_addr = collections.defaultdict(list)
        for t in xc.tags:
            if not t.is_empty and t.addr:
                by_addr[t.addr].append(t)
        decodes = {}

        def strict(g, addr):
            k = (g, addr)
            if k not in decodes:
                try:
                    decodes[k] = T.decode_tag(xc.read, lx[g], addr, g) if g in lx else None
                except Exception:                                        # noqa: BLE001
                    decodes[k] = None
            return decodes[k]
        # declared groups of the 8-byte references to each entry (junk fourccs - 0x00000000 / 0xFFFFFFFF or no known group - left out)
        known = set(lx) | {x for g in lx for x in self.family(g)}
        decl = collections.defaultdict(set)
        for t in xc.tags:
            if t.is_empty or not t.addr or t.group not in lx:
                continue
            g = self.alias_of.get(t.index, t.group)
            try:
                root = T.decode_tag(xc.read, lx[g], t.addr, g, dropped=[])
            except Exception:                                            # noqa: BLE001
                continue
            for n in root.iter_nodes():
                for f in n.fields:
                    if f.size != 8 or f.type.lower() != 'tagref':
                        continue
                    dg = bytes(n.data[f.offset:f.offset + 4])[::-1].decode('latin-1')
                    if dg in known:
                        rt = xc.tag_by_datum(struct.unpack_from('<I', n.data, f.offset + 4)[0])
                        if rt is not None:
                            decl[rt.index].add(dg)
        for addr, same in by_addr.items():
            if len({t.group for t in same}) < 2:
                continue
            for t in same:
                if t.index in self.alias_of:
                    continue
                others = [u for u in same if u.group != t.group and not (t.group in OBJECT_TYPE_GROUPS and u.group in OBJECT_TYPE_GROUPS)
                          and xc.tag_by_datum(u.datum) is u and strict(u.group, addr) is not None]
                if not others:
                    continue
                d = decl.get(t.index, set())
                if strict(t.group, addr) is not None:
                    # both layouts read the bytes: the references decide (Cable_Room <fx> 4598 and snd! 2651 both decode; every
                    # reference to 4598 declares snd!)
                    if not d or any(g in self.family(t.group) for g in d):
                        continue
                    others = [u for u in others if all(g in self.family(u.group) for g in d)]
                else:
                    others = [u for u in others if any(g in self.family(u.group) for g in d)] or others
                if len({u.group for u in others}) != 1:
                    if others:
                        self.warn('data_twin_ambiguous', '%s %s: its data decodes as %s' % (t.group, t.name, sorted({u.group for u in others})))
                    continue
                u = min(others, key=lambda u: (u.size != t.size, u.index))
                self.data_twin[t.index] = u.index
        for i in [i for i, j in self.data_twin.items() if self.data_twin.get(j) == i]:
            self.data_twin.pop(i, None)                                  # mutual: neither is the other's alias
        for i in list(self.data_twin):
            # a longer cycle (i -> j -> k -> i, three groups at one address) would recurse in resolve_x until RecursionError: none of
            # its entries is the others' alias either
            seen, j = {i}, self.data_twin.get(i)
            while j is not None and j not in seen:
                seen.add(j)
                j = self.data_twin.get(j)
            if j is not None:
                for k in seen:
                    self.data_twin.pop(k, None)
        # object entries whose data is a NON-object tag (Biohazard weap ghost_gun = itmc energy_sword2): only remap_xbox may follow them,
        # after ref_group_allowed has checked the field (resolve_x alias_ok)
        self.object_nonobject_twins = {i for i, j in self.data_twin.items() if xc.tags[i].group in OBJECT_TYPE_GROUPS
                                       and xc.tags[j].group not in OBJECT_TYPE_GROUPS}
        for i, d in decl.items():
            t = xc.tags[i]
            if len(d) != 1 or i in self.data_twin or i in self.alias_of or t.group in OBJECT_TYPE_GROUPS or not t.addr:
                continue
            g = next(iter(d))
            if g not in REINDEX_GROUPS or g in self.family(t.group) or t.group in self.family(g) or len(by_addr[t.addr]) > 1:
                continue
            r = strict(g, t.addr)
            if r is None or not all(r.block(b) for b in REINDEX_GROUPS[g]):
                continue
            if g == 'snd!':
                if t.group in TAGREF_EFFECT_GROUPS:
                    continue                    # an effe / lsnd in a sound slot is the mod's choice (ref_group_allowed), not another tag's data
                # a sound body indexes the map's gestalt: its pitch ranges must exist there
                gs = self.__dict__.get('_twin_gestalt') or self.__dict__.setdefault('_twin_gestalt', SX.Gestalt(xc))
                b = xc.read(t.addr, 0x14)
                pr, prc = struct.unpack_from('<h', b, 8)[0], b[0xA]
                if not (prc and 0 <= pr and pr + prc <= len(gs.pitch_ranges)):
                    continue
            self.reindex_as[i] = self.alias_of[i] = g
        if self.data_twin:
            self.stats['data_twins'] = len(self.data_twin)
            self.warn('data_twin', '%d index entr%s whose data is another group\'s tag: references go to that tag (%s)' % (
                len(self.data_twin), 'y' if len(self.data_twin) == 1 else 'ies', ', '.join(
                    '%s %s = %s %s' % (xc.tags[i].group, xc.tags[i].name, xc.tags[j].group, xc.tags[j].name)
                    for i, j in list(self.data_twin.items())[:6])))
        if self.reindex_as:
            self.stats['reindexed'] = len(self.reindex_as)
            self.warn('reindexed_as', '%d tag(s) whose data is the group their references declare, written as that group: %s' % (
                len(self.reindex_as), ', '.join('%s %s = %s' % (xc.tags[i].group, xc.tags[i].name, g) for i, g in self.reindex_as.items())))

    def dedupe_names(self):
        """Two different tags of one group under one name in the source (duffworld: two crate_multi models, umbubble: two dark_gray
        bitmaps) - every by-name lookup (the plan, the Vista links, donors) would treat them as one tag.  The lowest index keeps the
        name; the others become '<name>_dup<n>' and convert as the map's own tags."""
        seen = set()
        n = 0
        for t in self.xc.tags:
            if t.is_empty or not t.name:
                continue
            k = (t.group, t.name.lower())
            if k not in seen:
                seen.add(k)
                continue
            i = 2
            while (t.group, ('%s_dup%d' % (t.name, i)).lower()) in seen:
                i += 1
            t.name = '%s_dup%d' % (t.name, i)
            seen.add((t.group, t.name.lower()))
            n += 1
        if n:
            self.warn('duplicate_names', '%d tag(s) share group + name with an earlier tag: renamed <name>_dup<n>' % n)

    # ---- setup -------------------------------------------------------------------------------------------------------
    def pack_files_dir(self):
        """The folder of the pack's own mainmenu.map / shared.map: --pack-dir when given, else the map's folder, else (a map that was moved
        out of its pack's 'maps' folder, or one kept apart from it) the folder above it, a 'maps' folder inside it, or a sibling 'maps'
        folder - the first of those that holds a shared.map.  -> (folder or None to use the map's own folder, [folders looked in])."""
        here = os.path.dirname(self.src)
        if self.pack_dir:
            return self.pack_dir, [self.pack_dir]
        up = os.path.dirname(here)
        cands = [here, up, os.path.join(here, 'maps'), os.path.join(up, 'maps')]
        seen, looked = set(), []
        for c in cands:
            k = os.path.normcase(os.path.abspath(c))
            if k in seen or not os.path.isdir(c):
                continue
            seen.add(k)
            looked.append(c)
            if os.path.isfile(os.path.join(c, 'shared.map')):
                return (None if k == os.path.normcase(os.path.abspath(here)) else c), looked
        return None, looked

    def stock_folder_warning(self):
        """Tell once, when the conversion runs without (a usable) OG Xbox maps folder, what that costs and how to supply it."""
        info = SI.stock_status(self.stock_dir)
        self.stock_info = info
        if info['maps']:
            miss = [n for n, k in (('single_player_shared.map', 'has_sp_shared'), ('mainmenu.map', 'has_mainmenu'), ('shared.map', 'has_shared'))
                    if not info[k]]
            if miss:
                self.warn('stock_xbox_maps_incomplete', 'the OG Xbox maps folder %s has no %s: %s' % (
                    self.stock_dir, ' / '.join(miss), STOCK_MISSING_FILES_EFFECT))
                self.log('WARNING: ' + self.warnings['stock_xbox_maps_incomplete'][-1])
            return
        if not self.stock_dir:
            why = 'No OG Xbox maps folder was given.'
        elif not info['exists']:
            why = 'The OG Xbox maps folder %s does not exist.' % self.stock_dir
        else:
            why = 'The OG Xbox maps folder %s holds no Halo 2 Xbox maps.' % self.stock_dir
        msg = why + ' ' + STOCK_MISSING_TEXT
        self.warn('stock_xbox_maps_missing', msg)
        self.log('WARNING: ' + msg)

    def setup(self):
        shared_dir, looked = self.pack_files_dir()
        self.pack_dir = shared_dir
        self.xc = XboxCache(self.src, shared_dir=shared_dir, fallback_dirs=(self.stock_dir,))
        if shared_dir:
            self.log('pack files: %s' % self.xc.shared_dir)
        for m in self.xc.repairs:
            self.warn('header_datum_salt', m)
        short = short_bytes(self.xc.m[:0x10], len(self.xc.m))
        if short:
            self.warn('source_slightly_short', 'the file is %d bytes shorter than its header says (the end of the last tag is missing; '
                      'the map converts, but check the download)' % short)
        st = self.xc.header['scenario_type']
        if st != 1:
            # 24 Phantom maps (ageoldwar, metro1, desertedmetro ...) have header 0x140 = 0 (a mod / resign tool zeroed it) while their
            # scenario tag's own Type (+0x10) says multiplayer; stock campaign maps say 0 in both places, stock MP maps 1 in both.  The
            # output header is always written as multiplayer (map_type=1)
            stype = scenario_tag_type(self.xc)
            if xbox_kind(st, stype) != 'multiplayer':
                kind = {0: 'a campaign (single-player) map', 2: 'the main menu', 3: 'a shared data file', 4: 'a shared data file'}.get(
                    stype if stype in (0, 2, 3, 4) else st, 'not a multiplayer map')
                raise NotMultiplayerError('%s is %s. Only multiplayer Xbox maps can be converted (header scenario type %d, scenario Type %s)'
                                          % (os.path.basename(self.src), kind, st, stype))
            self.warn('header_scenario_type', 'header scenario type %d, but the scenario tag\'s Type says multiplayer: converted as '
                      'multiplayer' % st)
        self.vsh = VistaCache(os.path.join(self.vm, 'shared.map'))
        ps = os.path.join(self.xc.shared_dir, 'shared.map')
        self.ps = XboxCache(ps, shared_dir=self.xc.shared_dir) if os.path.isfile(ps) else None
        if self.ps is None:
            # the plan cannot verify any tag against the pack: it LINKS what it cannot verify to Halo 2 Vista's copy of that name (non-strict),
            # tags whose raw data sits in shared.map cannot be read, and a map with a blanked tag-name table keeps no names
            msg = ('no Xbox shared.map found for this map (looked in: %s). Without the pack\'s shared.map the converter cannot tell which tags '
                   'the map changed: tags it cannot check are replaced by the standard Halo 2 Vista tag of the same name, bitmaps, models and '
                   'sounds stored in shared.map cannot be read, and a map whose tag names were removed stays unnamed. Put the pack\'s '
                   'shared.map (and mainmenu.map) in the map\'s folder or pass --pack-dir <folder>.' % '; '.join(looked or [os.path.dirname(self.src)]))
            self.warn('pack_shared_missing', msg)
            self.log('WARNING: ' + msg)
        elif not os.path.isfile(os.path.join(self.xc.shared_dir, 'mainmenu.map')):
            self.warn('pack_mainmenu_missing', 'no mainmenu.map next to the pack\'s shared.map (%s): bitmaps stored in it cannot be read and '
                      're-skinned menu bitmaps cannot be recognised' % self.xc.shared_dir)
        self.stock_folder_warning()
        stock_usable = self.stock_dir if self.stock_info['maps'] else None
        if self.ps is not None and NR.nameless_tags(self.xc):
            # a "protected" map with a blanked tag-name table: recover the names by content (h2conv/xbox/name_recovery.py)
            st = NR.recover_names(self.xc, self.ps, self.log, stock_dir=stock_usable, cache_dir=self.index_dir)
            for k, v in st.items():
                self.stats['names:%s' % k] += v
            if st.get('labelled'):
                self.warn('names_labelled', '%d tag(s) have no name anywhere (the map\'s own content): labelled recovered\\...' % st['labelled'])
        self.dedupe_names()
        # content digests of every stock Xbox copy, cached (stock_index): the plan's stock check and shader identity read it
        self.stock = SI.StockIndex(stock_usable, self.index_dir, self.log)
        self.plan = PX.XboxPlan(self.xc, self.ps, self.vsh, self.log, strict=self.opts.get('strict', False), stock=self.stock).build()
        self.lx, self.lv = XL.xbox_layouts(), XL.vista_layouts()
        self.tagref_groups = load_tagref_groups(self.opts.get('tagref_groups'))       # ref_group_allowed
        self.find_cross_group_aliases()
        self.align = T.AlignTable.load(os.path.join(_paths.data_dir(), 'block_alignment_vista.json'))
        self.strs = StringTable.from_vista_shared(os.path.join(self.vm, 'shared.map'))
        self.hs_tables = XboxHsTables()
        self.vs_name = {}
        for t in self.vsh.tags:
            if t.addr and t.name:
                self.vs_name.setdefault((t.group, t.name.lower()), t)
        self.placeholders = [VistaTag(t.index, t.group, t.datum, 0, 0, t.name) for t in self.vsh.tags
                             if t.index >= 10000 and not t.is_empty]
        self._donor_index()
        self.tags = []                   # OutTag in index order
        self.by_x = {}                   # xbox tag index -> OutTag
        self.by_d = {}                   # (donor path, vista tag index) -> OutTag
        self.full_ltmps = set()          # output indices of donor ltmps used COMPLETE (the BSP is the unmodified stock one)
        self.lightmap_style = self.opts.get('lightmap') or 'auto'      # --lightmap: auto | native | neutral-valid (h2conv.lightmap_gen)
        if self.lightmap_style not in LG.CHOICES:
            raise ValueError('lightmap must be one of %s, not %r' % (', '.join(LG.CHOICES), self.lightmap_style))
        self.by_name = {}                # (group, lower name) -> OutTag (local)
        self.queue = collections.deque()
        self.raws = []                   # (kind, bytes); node.patches tokens = ('raw', i, location bits)
        self._xdatum_cache = {}

    def _donor_index(self):
        """(group, lower name) -> [(path, tag index)] over the stock MULTIPLAYER Vista maps (+ mainmenu): their local tags reference shared.map
        placeholders with shared.map's own datums, so a copy keeps every shared reference valid."""
        self.donor = {}
        self._donor_caches = {}
        for f in sorted(glob.glob(os.path.join(self.vm, '*.map'))):
            b = os.path.basename(f).lower()
            if b in ('shared.map', 'single_player_shared.map'):
                continue
            try:
                c = VistaCache(f)
            except Exception:                                            # noqa: BLE001
                continue
            if c.header.type not in (1, 2):
                c.close()
                continue
            for t in c.tags:
                if t.addr and t.name:
                    self.donor.setdefault((t.group, t.name.lower()), []).append((f, t.index))
            c.close()

    def donor_cache(self, path):
        c = self._donor_caches.get(path)
        if c is None:
            c = self._donor_caches[path] = VistaCache(path)
        return c

    # ---- tag creation ------------------------------------------------------------------------------------------------
    def _new(self, kind, group, name, src):
        o = OutTag(kind, group, name, src, len(self.tags))
        if o.index >= 10000:
            raise RuntimeError('more than 10000 local tags')
        self.tags.append(o)
        if name:
            self.by_name.setdefault((group, name.lower()), o)
        self.queue.append(o)
        return o

    def donor_tag(self, group, name):
        hits = self.donor.get((group, (name or '').lower()))
        if not hits:
            return None
        path, idx = hits[0]
        key = (path, idx)
        o = self.by_d.get(key)
        if o is None:                         # already brought in under this name from another stock map (resolve_d): that one
            prev = self.by_name.get((group, (name or '').lower()))
            if prev is not None and prev.kind == 'd':
                return prev
        if o is None:
            c = self.donor_cache(path)
            o = self.by_d[key] = self._new('d', group, name, (c, c.tags[idx]))
            self.stats['donor:%s' % group] += 1
        return o

    # Xbox shader-family tags Vista dropped -> the Vista tag that takes their place.  A NONE particle Shader Template is not survivable: the
    # particle renderer follows it unchecked (halo2.exe+0xC49D1).  particle_alpha_test_no_lighting -> its glass twin (same layer/pass shape,
    # one implementation); a stock particle that used it takes Bungie's port instead (family_gone).
    FAMILY_SUBSTITUTES = {
        ('stem', 'shaders\\shader_templates\\transparent\\particle_alpha_test_no_lighting'):
            'shaders\\shader_templates\\transparent\\particle_alpha_test_no_lighting_fixed_for_glass',
    }

    def resolve_x(self, d, alias_ok=False):
        """Xbox datum -> output datum.  0 is NONE (Xbox datums never have salt 0; e.g. scnr Scenario Cluster Data/BSP stores 0 where
        stock Vista stores 0xFFFFFFFF).  An object entry whose data is a non-object tag (find_data_twins: Biohazard weap ghost_gun = itmc
        energy_sword2, Echo_Station banshee_gun = jpt!) resolves to that tag only with alias_ok - remap_xbox, after ref_group_allowed
        checked the field; every other path (the script converter's remap_datum, merge_with_vista's references, a donor name lookup)
        gets NONE, as remap_xbox gives an object field (weapon_alias_disarmed)."""
        if d in (0, NULL):
            return NULL
        if not alias_ok and self.object_nonobject_twins:
            t = self.xc.tag_by_datum(d)
            if t is not None and t.index in self.object_nonobject_twins:
                self.stats['data_twin_object_refs_none'] += 1
                return NULL
        r = self._xdatum_cache.get(d)
        if r is not None:
            return r
        t = self.xc.tag_by_datum(d)
        r = NULL
        if t is not None and t.index in self.data_twin and t.group != 'shad':
            # the entry's data is another tag's (find_data_twins): the Xbox read THAT tag's bytes, so the reference goes to it.  A shader
            # entry is never redirected: its consumers want a shader, and decode_shader refuses to build one from the other tag's bytes,
            # so it gets fallback_node's Bungie port / skeleton (never a NONE material)
            self.stats['data_twin_refs'] += 1
            r = self._xdatum_cache[d] = self.resolve_x(self.xc.tags[self.data_twin[t.index]].datum)
            return r
        if t is None:
            self.stats['ref_unknown'] += 1
        elif t.index in self.reindex_as:
            o = self.by_x.get(t.index)
            if o is None:
                o = self.by_x[t.index] = self._new('x', self.reindex_as[t.index], t.name, t)
            r = o.datum
        else:
            e = self.plan.entries.get(t.index)
            if e is None:
                self.stats['ref_no_entry:%s' % t.group] += 1
            elif e.action in ('link', 'vista'):
                r = e.vista_datum
                self.stats['linked'] += 1 if d not in self._xdatum_cache else 0
            elif e.action == 'vista_donor':
                vn = PX.family_name(t.group, t.name, lambda g, n: bool(self.donor.get((g, (n or '').lower())))
                                    or self.campaign_tag(g, n) is not None)          # a clone name: its base's (audit-tag-tricks RC6)
                o = self.donor_tag(t.group, vn)
                cr = self.campaign_tag(t.group, vn) if o is None else None
                sub = self.FAMILY_SUBSTITUTES.get((t.group, (vn or '').lower()))
                sv = None
                if o is None and cr is None and sub:
                    sv = self.vs_name.get((t.group, sub))                # shared.map's (linked, as a 'vista' entry would be)
                    o = self.donor_tag(t.group, sub) if sv is None else None
                    cr = self.campaign_tag(t.group, sub) if sv is None and o is None else None
                    if sv is not None or o is not None or cr is not None:
                        self.__dict__.setdefault('_family_substituted', set()).add(t.index)
                        self.warn('shader_family_substituted', '%s %s: Vista has no tag of this name - its closest Vista twin %s' % (
                            t.group, t.name, sub))
                if sv is not None:
                    r = sv.datum
                elif o is not None:
                    r = o.datum
                elif cr is not None:                                   # only a campaign map has it (prt_lightmap): its copy
                    r = self.resolve_d(cr[0], cr[1].datum)
                else:
                    self.warn('shader_family_missing', '%s %s: no Vista tag of this name - reference set to NONE' % (t.group, t.name))
            elif t.group == 'shad':
                o = self.by_name.get(('shad', (t.name or '').lower()))
                if o is None and self.shader_donor_allowed(t, e):
                    o = self.donor_tag('shad', t.name)
                if o is not None:
                    r = o.datum
                else:
                    r = self.custom_shader(t)
            else:
                o = self.by_x.get(t.index)
                vb = self.stock_named_broken(t, e) if o is None else None
                if vb is not None:
                    r = vb
                else:
                    if o is None:
                        o = self.by_x[t.index] = self._new('x', t.group, t.name, t)
                    r = o.datum
        self._xdatum_cache[d] = r
        return r

    RC8_WHY = 'modified vs every stock Xbox copy'

    def stock_named_broken(self, t, e):
        """The Vista shared.map datum for a stock-NAMED tag that converts only because the plan found it modified vs every stock Xbox
        copy (RC8) when its own conversion is broken - geometry that does not convert (Deep_darkness lights_underwater / crate_space:
        no geometry block, 'no fklb trailer', empty meshes) or a sound with invalid Xbox ADPCM, codec values or a scale / promotion /
        extra-info / pitch-range index past its block (Gate air_tank_*, Deep_darkness container_melee, Southside / UMtitan1 air_tank_loop).
        Before this rule those linked unseen and showed Bungie's models / played Bungie's sounds; decision of 2026-10-27:
        that is kept for broken content, a genuine edit that converts cleanly keeps converting.  None = convert as planned."""
        if e is None or not (e.why or '').startswith(self.RC8_WHY) or t.index in self.alias_of or t.index in self.data_twin:
            return None
        if t.group != 'snd!' and t.group not in GEOMETRY_GROUPS:
            return None
        vt = self.vs_name.get((t.group, (PX.vista_name(t.group, t.name) or '').lower()))
        if vt is None:
            return None
        why = None
        if t.group == 'snd!':
            gs = self.__dict__.get('_twin_gestalt') or self.__dict__.setdefault('_twin_gestalt', SX.Gestalt(self.xc))
            bad = gs.defects(self.xc.read(t.addr, 0x14))
            why = '; '.join(bad[:2]) if bad else None
        else:
            why = self.geometry_defect(t)
        if why is None:
            return None
        self.stats['stock_name_broken_linked:%s' % t.group] += 1
        self.warn('stock_name_broken_linked', '%s %s: modified vs every stock Xbox copy, but %s - linked to the Vista tag of that name' % (
            t.group, t.name, why[:140]))
        return vt.datum

    def geometry_defect(self, t):
        """Why a geometry tag's Xbox geometry does not convert (geometry_xbox.convert_block raises for one of its blocks, as geometry()
        would report geometry_failed), else None.  A tag that does not decode strictly is left to build_node (salvage / fallback)."""
        from . import geometry_xbox as GX
        try:
            root = T.decode_tag(self.xc.read, self.lx[t.group], t.addr, t.group)
        except Exception:                                                # noqa: BLE001
            return None
        o = OutTag('x', t.group, t.name, t, 0)
        sec_comp = self.mode_section_compounds(o, root) if t.group == 'mode' else {}
        for n in root.iter_nodes():
            for f in n.fields:
                if f.name not in ('Block Offset', 'Resource Block Offset') or f.size != 4:
                    continue
                fo = f.offset
                ptr, bsize, sds = n.u32(fo), n.u32(fo + 4), n.u32(fo + 8)
                if ptr in (0, NULL) or not bsize:
                    continue
                fr = next((x for x in n.fields if x.offset == fo + 0x10 and x.type in T.BLOCK_TYPES), None)
                res_nodes = n.blocks.get(fr.offset, []) if fr is not None else []
                label = n.path.split('/')[-1] or t.group
                try:
                    GX.convert_block(self.xc.raw(ptr, bsize), res_nodes, sds, t.group, label, warn=lambda m: None,
                                     compound=sec_comp.get(id(n)), stats=collections.Counter())
                except Exception as ex:                                  # noqa: BLE001
                    return 'its geometry does not convert (%s: %r)' % (label, ex)
        return None

    def resolve_d(self, cache, d):
        """Datum inside a donor Vista map -> output datum."""
        if d in (0, NULL):
            return d
        i = d & 0xFFFF
        if i >= len(cache.tags):
            return NULL
        t = cache.tags[i]
        if t.is_empty:
            return NULL
        if not t.addr:                                   # a placeholder of the donor map = the same shared.map tag
            return t.datum
        k = (t.group, (t.name or '').lower())
        if getattr(self, '_x_by_name', None) is None:
            self._x_by_name = {}
            for xt in self.xc.tags:
                if not xt.is_empty and xt.name:
                    self._x_by_name.setdefault((xt.group, xt.name.lower()), xt)
        xt = self._x_by_name.get(k)
        pe = self.plan.entries.get(xt.index) if xt is not None else None
        if t.group == 'bitm' and pe is not None and self.xc.tag_by_datum(xt.datum) is xt and self.bitmap_modified_in_place(xt.datum):
            # a stock-named bitmap the map redrew in place (its OWN content differs: plan 'modified ...', not 'depends on ...', and its
            # LOD1 pixels are no stock copy's, bitmap_modified_in_place): a donor shader / effect that references the name draws the map's
            # pixels, as the Xbox did, not Vista's stock texture (audit-tag-tricks RC4 as the verifier restricted it, after the RC3 LOD
            # fix).  By the plan's verdict alone, 10 of the 16 smoke maps (stock zanzibar too) swapped in their Xbox copy of Bungie's MP
            # build of detail_maps\tech\wacky_globs (element byte 0x16 LOD Adjust differs, the same pixels) for Vista's texture (2026-10-26)
            self.stats['donor_ref_own_bitmap'] += 1
            return self.resolve_x(xt.datum)
        vt = self.vs_name.get(k)
        if vt is not None:
            return vt.datum
        o = self.by_name.get(k)
        if o is not None:
            return o.datum
        # the map has its own tag of that name (not built yet): that one, not a second copy from the donor.  duffworld: hlmt crate_multi
        # fell back to the Vista tag, whose mode / coll / phmo came in again from a stock map - a duplicate crate_multi whose donor
        # geometry pointed outside this map's geometry region
        # shader-family donors keep their own Vista references; an entry that carries another entry's datum (a clone, see
        # XboxCache._by_datum) cannot be reached by its datum - the donor's own copy then, never the other tag
        if pe is not None and pe.action in ('convert', 'link') and self.xc.tag_by_datum(xt.datum) is xt:
            return self.resolve_x(xt.datum)
        key = (cache.path, t.index)
        o = self.by_d.get(key)
        if o is None:
            o = self.by_d[key] = self._new('d', t.group, t.name, (cache, t))
            self.stats['donor:%s' % t.group] += 1
        return o.datum

    def custom_shader(self, t):
        """A shader built from the Xbox data (no usable Vista copy of that name): converted onto the Vista template (see build_shader).
        Its template comes from xbox_shader_stem, which reads only the template references, so a shader with unreadable blocks never
        fails the MODEL being remapped (H3ascension flame_thrower: the mode died through its material, audit-geometry RC1).  When the
        template has no Vista shader at all: the stock Vista shader of the same name if a stock map has one (a modified stock-named
        shader renders as the stock one, as before shader_donor_allowed), else NONE (reported)."""
        stem = self.xbox_shader_stem(self.xc, t)
        if self.shader_reference(self.output_stem(t, stem)) is not None:
            o = self.by_x.get(t.index)
            if o is None:
                o = self.by_x[t.index] = self._new('x', t.group, t.name, t)
            return o.datum
        o = self.donor_tag('shad', t.name)
        if o is not None:
            self.warn('custom_shader_donor', '%s: template %s has no Vista shader - the stock Vista shader of that name' % (t.name, stem))
            return o.datum
        self.warn('custom_shader_replaced', '%s: template %s has no Vista shader - reference set to NONE' % (t.name, stem))
        self.stats['custom_shader_replaced'] += 1
        return NULL

    # ---- shader identity: which Xbox shaders ARE the stock shader of their name ------------------------------------------
    STOCK_CHECKED = ('shad',)            # groups without raw data: plan_xbox._same is exact across maps

    def shader_donor_allowed(self, t, e):
        """May a 'convert' shader be replaced by the stock Vista shader of its NAME (donor_tag)?  Only when its Xbox content is a stock
        Xbox copy of that name.  Old Xbox tools retextured stock tags in place under their stock names: aerial2's
        headlong_horiz_glass carries flat_dark_scratchy + alphagasgiant_sky where stock headlong has nm_horiz_glass + earthcity_sky,
        and the by-name donor put the glass back on every detail_keyplug floor (aerial2-glass RC1, 2026-10-26).  Refused too when the
        plan already found a modification - its own ('modified vs the pack shared.map') or in a tag it references ('depends on
        converted': ditw2 battle_rifle 'tens' -> its re-drawn numbers_plate); stock_xbox_equal compares references by name only.
        A shader that IS a stock Xbox copy keeps the donor unless one of its bitmaps was redrawn in place (bitmap_modified_in_place, by
        pixels): Bungie's own copies differ between levels (battle_rifle tens / ones: 3 bitmaps in shared.map, 4 in backwash, gemini
        ... and in Vista mainmenu, so 'modified vs the pack shared.map' on every map that carries them), and the plan's bitmap verdict
        counts moved LOD pointers as modifications (audit-tag-tricks RC3: 'depends on converted' refused 51 shaders of 343_Delta_Ruins
        whose bitmaps have Bungie's pixels).  A map-local bitmap the donor resolves to the map's own copy anyway (resolve_d)."""
        if not self.donor.get(('shad', (t.name or '').lower())):
            return True                                  # no donor: nothing to refuse
        why = e.why or ''
        eq = self.stock_xbox_equal(t)
        if eq is False:
            reason = 'differs from every stock Xbox copy of its name'
        elif eq is True and self.shader_bitmap_redrawn(t):
            reason = 'a stock Xbox copy, but a bitmap it uses was redrawn in place'
        elif eq is None and why.startswith(('depends on converted', 'modified vs the pack')):
            reason = why
        else:
            return True
        self.stats['shader_donor_refused'] += 1
        self.warn('stock_name_modified', '%s: %s - built from the Xbox data on Bungie\'s Vista port, not taken from the stock map'
                  % (t.name, reason[:120]))
        return False

    def shader_bitmap_redrawn(self, t):
        """Does a Postprocess Bitmaps slot of the Xbox shader t reference a stock-named bitmap the map redrew in place?"""
        try:
            pp = self.decode_shader(self.xc, t).block('Postprocess Definition')
        except Exception:                                                # noqa: BLE001
            return True                                  # cannot tell: build it (the Xbox data decides)
        return any(self.bitmap_modified_in_place(k.u32(0)) for k in (pp[0].block('Bitmaps') if pp else []))

    def stock_xbox_copies(self, group, name):
        """[(XboxCache, XboxTag)] of group + name in the pack's shared.map and every stock Xbox v8 map (opts stock_xbox_maps /
        the OG Xbox maps folder; the Halo 3 / Reach construct, shrine and campaign.map do not open and are skipped).  Only the stock maps whose
        content index (stock_index) lists that name are opened: opening and listing ~26 maps per conversion cost +32 % time."""
        memo = self.__dict__.setdefault('_stock_x', {})
        k = (group, (name or '').lower())
        if k not in memo:
            out = []
            pt = self.plan._ps_name.get(k)
            if pt is not None and not pt.is_empty:
                out.append((self.ps, pt))
            out.extend(self.stock.tags(group, name))
            memo[k] = out
        return memo[k]

    def stock_xbox_equal(self, t):
        """True = the Xbox tag t IS a stock Xbox copy of its name (plan_xbox._same: references by name, string ids by text); False =
        copies exist and none is equal (a stock name over the mod's own content); None = nothing to compare with (no copy, or t does
        not decode: a shader with unreadable blocks keeps today's donor).  Bungie's per-level variants count as different (a leave-one-out
        run over the stock maps found 23 of 381): those are built on Bungie's own port with only the differing slots (build_shader), so
        they stay faithful.  The stock maps are compared through their cached content digests (stock_index; digest equality is
        _same's), the pack's shared.map copy directly."""
        memo = self.__dict__.setdefault('_stock_eq', {})
        if t.index not in memo:
            r = None
            if t.group in self.STOCK_CHECKED:
                pk = [st for c, st in self.stock_xbox_copies(t.group, t.name) if c is self.ps]
                if pk or self.stock.has(t.group, t.name):
                    try:
                        xr = T.decode_tag(self.xc.read, self.lx[t.group], t.addr, t.group)
                    except Exception:                                    # noqa: BLE001
                        xr = None
                    if xr is not None:
                        r = False
                        for st in pk:
                            try:
                                if PX._same(xr, T.decode_tag(self.ps.read, self.lx[st.group], st.addr, st.group), self.xc, self.ps, []):
                                    r = True
                                    break
                            except Exception:                            # noqa: BLE001
                                continue
                        if not r and self.stock.has(t.group, t.name):
                            r = self.stock.match(t.group, t.name, SI.Digester(self.xc).tag(t, xr)) is not None
            memo[t.index] = r
        return memo[t.index]

    def closest_stock_arrays(self, t, xa, stem):
        """(_pp_arrays, XboxCache, [Bitmaps datums]) of the stock Xbox copy of t's name on the same template with the most slots equal
        to the mod's (xa), or None."""
        best, out = -1, None
        for c, st in self.stock_xbox_copies('shad', t.name):
            try:
                sr = T.decode_tag(c.read, self.lx['shad'], st.addr, 'shad')
            except Exception:                                            # noqa: BLE001
                continue
            if self.shader_stem(c, sr) != stem:
                continue
            sa = _pp_arrays(sr, lambda d, cc=c: _xname(cc, d))
            eq = sum(1 for a in self.SHADER_ARRAYS for u, v in zip(xa.get(a, []), sa.get(a, [])) if u == v)
            if eq > best:
                pp = sr.block('Postprocess Definition')
                best, out = eq, (sa, c, [k.u32(0) for k in pp[0].block('Bitmaps')] if pp else [])
        return out

    def bitmap_pixels(self, c, d):
        """((width, height bytes, LOD1 size, LOD1 crc32) per element) of the bitmap of datum d in Xbox cache c; None when it is no
        readable bitmap (memo)."""
        memo = self.__dict__.setdefault('_bitm_px', {})
        k = (c.path, d)
        if k not in memo:
            r = None
            t = c.tag_by_datum(d) if d not in (0, NULL) else None
            if t is not None and t.group == 'bitm' and t.addr:
                try:
                    els = T.decode_tag(c.read, self.lx['bitm'], t.addr, 'bitm').block('Bitmaps')
                    fo = els[0].field('LOD1 Offset') if els else None
                    fs = els[0].field('LOD1 Size') if els else None
                    r = tuple((bytes(e.data[4:8]), e.u32(fs.offset), zlib.crc32(c.raw(e.u32(fo.offset), e.u32(fs.offset))))
                              for e in els) if fo is not None and fs is not None else ()
                except Exception:                                        # noqa: BLE001
                    r = None
            memo[k] = r
        return memo[k]

    def bitmap_modified_in_place(self, d):
        """Is the Xbox bitmap of datum d the map's own content under a stock name that Vista's shared.map also has (plan 'modified vs
        the pack shared.map')?  The plan's verdict alone over-reports (it calls a moved LOD1 / unused LOD2-3 pointer a modification:
        450 of H3ascension's 481 'modified' stock bitmaps have byte-identical pixels, audit-tag-tricks RC3), so the LOD1 pixels of every
        element are compared with the pack shared.map copy (bitmap_pixels) - or, for a name the pack's shared.map lacks (the plan's
        'modified vs every stock Xbox copy'), with every stock Xbox copy: the same pixels as one of them is no redraw.  A name the pack's
        shared.map HAS is compared with that copy only (as in shader identity): pixels equal to a campaign level's variant are
        still not what the pack's shared.map - and so Vista's shared texture - holds."""
        xt = self.xc.tag_by_datum(d) if d not in (0, NULL) else None
        e = self.plan.entries.get(xt.index) if xt is not None else None
        if e is None or e.action != 'convert' or not (e.why or '').startswith('modified') or self.ps is None:
            return False
        a = self.bitmap_pixels(self.xc, d)
        if a is None:
            return True
        p = self.plan._ps_name.get(PX._nk('bitm', xt.name))
        cands = [(self.ps, p)] if p is not None and not p.is_empty else self.stock_xbox_copies('bitm', xt.name)
        return not cands or all(a != self.bitmap_pixels(c, st.datum) for c, st in cands)

    def stock_slot_kept(self, a, i, xa, xd, near):
        """build_shader delta: does the mod's slot i of array a equal the closest stock Xbox copy's (near = closest_stock_arrays)?  Then
        Bungie's port keeps its own value there.  Bitmaps: the same name and not retextured in place, or the same pixels under another
        name (a protected map whose bitmap names could not be recovered: Funbox's masterchief shader references 'recovered\\...'
        copies with stock masterchief pixels)."""
        sa, sc, sbm = near
        if i >= len(sa.get(a, [])) or i >= len(xa.get(a, [])):
            return False
        if a != 'Bitmaps':
            return xa[a][i] == sa[a][i]
        if xa[a][i] == sa[a][i] and not self.bitmap_modified_in_place(xd):
            return True
        px = self.bitmap_pixels(self.xc, xd) if xd not in (0, NULL) else None
        return px is not None and i < len(sbm) and px == self.bitmap_pixels(sc, sbm[i])

    # ---- shaderguard + template of an Xbox shader ----------------------------------------------------------------------
    def decode_shader(self, cache, t, emptied=None):
        """An Xbox shad decoded for building: strictly, else with every block / dataref whose own data cannot be read emptied IN PLACE
        (tagtree emptied=).  Injected shaders in 21 maps carry the bytes of a neighbouring bitmap reference in root 'Postprocess
        Properties' (count 0x746D0000 'mt', pointer 0x12BD6269 / 0xFFFF6269: flame_thrower, Qbert glass_box / white_bloom, the
        Lockpaintball marine); the strict decode raised inside remap_xbox of the MODE using the shader and 51 modes died or became the
        stock Vista model (audit-geometry RC1 / audit-tag-tricks RC2 'shaderguard', 2026-10-26).  build_shader takes only the static
        arrays from the Xbox postprocess, so an emptied block costs at most the skeleton's value in those slots.  Raises only when the
        root itself is unreadable.  emptied: a list -> the emptied paths are appended (shader_slot_map)."""
        try:
            return T.decode_tag(cache.read, self.lx['shad'], t.addr, 'shad')
        except Exception:                                                # noqa: BLE001
            if cache is self.xc and t.index in self.data_twin:
                # the bytes are another group's tag that decodes strictly as itself (find_data_twins): emptying what a shader layout
                # cannot read would build a junk shader from them (the lenient path lacked this alias guard)
                u = self.xc.tags[self.data_twin[t.index]]
                raise ValueError('the data is %s %s (same address), not a shader' % (u.group, u.name))
        quiet = emptied is not None                    # the caller reports (shader_slot_map: evidence only, maybe never built)
        emptied = [] if emptied is None else emptied
        if cache is not self.xc:
            return T.decode_tag(cache.read, self.lx['shad'], t.addr, 'shad', emptied=emptied)
        # the salvage ladder (salvage_tag): a stale pointer reads its stock original (Sanctus 0x8073E930 = 07a_highcharity
        # Animated Parameter References, Reconciliation antigrav_effects Pixel Constants = 01b_spacestation's), data past the file end
        # inside the tag region reads as zeros (Cassiopeia Pixel Constants 0x80591984), a wild pointer is emptied (the root
        # Postprocess Properties above, as before)
        root, notes = self.salvage_tag(t, 'shad', report=not quiet, root_wild=True)
        emptied.extend('%s: %s %s' % (p, c, d) for c, p, d in notes if c in SV.UNRELIABLE)
        seen = self.__dict__.setdefault('_shad_emptied', set())
        if not quiet and t.index not in seen and any(c == 'emptied' for c, _, _ in notes):
            seen.add(t.index)
            self.stats['shader_blocks_emptied'] += 1
            self.warn('shader_blocks_emptied', '%s: unreadable %s - emptied, the shader is built from the rest'
                      % (t.name, '; '.join(p + ': ' + d for c, p, d in notes if c == 'emptied')[:160]))
        return root

    def salvage_tag(self, t, g, report=True, root_wild=False):
        """(root, notes): Xbox tag t, whose strict decode as g failed, through salvage_xbox's repair ladder (shift, stale-pointer restore
        from the stock original, junk-count clamp, a synthesised collision permutation, zeros past the file end, wild pointers emptied in
        place; 2026-10-26).  Before, one unreadable nested block replaced the whole tag
        by the stock Vista tag of its name or killed it: Skull_Island's ancient trees and broken arches, the Reconciliation walkway, the
        Burnt_Blood creep, the soccer ball, 343_Delta_Ruins' ghost / banshee / flak damage effects ...  Never for an entry whose bytes
        another group's layout reads strictly: the bytes are that tag's, not this one's with junk (the alias guard; Biohazard weap
        ghost_gun = itmc energy_sword2, Skull_Island <fx> sword_impact_env = two snd!).  Raises the error that refused it; report=False:
        not recorded yet (shader_slot_map evidence)."""
        by_addr = self.__dict__.get('_by_addr')
        if by_addr is None:
            by_addr = self._by_addr = collections.defaultdict(list)
            for u in self.xc.tags:
                if not u.is_empty and u.addr:
                    by_addr[u.addr].append(u)
        for u in by_addr.get(t.addr, ()):
            if u is t or u.group == g or u.group not in self.lx:
                continue
            try:
                T.decode_tag(self.xc.read, self.lx[u.group], u.addr, u.group)
            except Exception:                                            # noqa: BLE001
                continue
            raise SV.Unsalvageable('%s %s: its bytes are %s %s (same address, decodes strictly)' % (g, t.name, u.group, u.name))
        sv = self.__dict__.get('_salvage')
        if sv is None:
            sv = self._salvage = SV.TagSalvage(self.xc, self.lx, self.stock)
        root, notes = sv.decode(t, g, root_wild=root_wild)
        done = self.__dict__.setdefault('_salvage_done', set())
        if report and t.index not in done:
            done.add(t.index)
            cls = sorted({c for c, _, _ in notes}, key=SV.CLASSES.index)
            self.stats['salvaged_tags'] += 1
            for c in cls:
                self.stats['salvage:%s' % c] += 1                       # tags recovered (per class)
            for c, _, _ in notes:
                self.stats['salvage_blocks:%s' % c] += 1                # blocks / datarefs repaired (per class)
            self.salvaged.append({'group': g, 'name': t.name, 'index': t.index, 'classes': cls,
                                  'notes': ['%s %s %s' % n for n in notes]})
            self.warn('tag_salvaged', '%s %s: %s' % (g, t.name, '; '.join('%s %s %s' % n for n in notes)[:220]))
        return root, notes

    def stem_family(self, name):
        """Lower template name as the Vista shader index knows it: the name, else its clone base ('Copy of X', 'X (1)', 'X_dupN' ->
        X, plan_xbox.base_name) when only that has Vista shaders (UMconst / tramp tex_bump_env (1): audit-tag-tricks RC6)."""
        n = (name or '').lower()
        if not n or self._vista_shaders_by_stem().get(n) or self._campaign_shaders_by_stem().get(n):
            return n
        b = PX.base_name(n).lower()
        return b if b != n and (self._vista_shaders_by_stem().get(b) or self._campaign_shaders_by_stem().get(b)) else n

    def shader_stem(self, cache, root):
        """stem_of(root): the lower (family) name of an Xbox shader's template - root 'Template', else Postprocess Definition[0] 'Shader
        Template', which is what the Xbox renders with: Slaughterhouse's warthog chassis and Qbert's over_shield have a NONE root
        template over a valid tex_bump_shiny / plasma_shield postprocess and were set to NONE (audit-tag-tricks RC6, 2026-10-26)."""
        d = root.u32(4)
        if d in (0, NULL):
            pp = root.blocks.get(root.field('Postprocess Definition').offset) or []
            f = pp[0].field('Shader Template') if pp else None
            d = pp[0].u32(f.offset + (4 if f.size == 8 else 0)) if f is not None else NULL
        st = cache.tag_by_datum(d) if d not in (0, NULL) else None
        return self.stem_family(st.name) if st is not None else ''

    def xbox_shader_stem(self, cache, t):
        """shader_stem of a tag that may not decode at all: decode_shader, else the root template reference read directly, else ''."""
        try:
            return self.shader_stem(cache, self.decode_shader(cache, t))
        except Exception:                                                # noqa: BLE001
            pass
        try:
            st = cache.tag_by_datum(struct.unpack('<I', cache.read(t.addr + 4, 4))[0])
            return self.stem_family(st.name) if st is not None else ''
        except Exception:                                                # noqa: BLE001
            return ''

    # ---- custom shaders ----------------------------------------------------------------------------------------------
    SHADER_ARRAYS = ('Bitmaps', 'Pixel Constants', 'Vertex Constants', 'Value Properties', 'Color Properties')

    def _vista_shaders_by_stem(self):
        m = getattr(self, '_vss', None)
        if m is None:
            m = self._vss = collections.defaultdict(list)       # lower stem name -> [(cache, tag)]
            caches = [self.vsh] + [self.donor_cache(p) for p in sorted({h[0] for hits in self.donor.values() for h in hits})]
            for c in caches:
                for vt in c.tags:
                    if vt.group == 'shad' and vt.addr:
                        d = struct.unpack('<I', c.read_addr(vt.addr + 4, 4))[0]
                        st = c.tags[d & 0xFFFF] if d not in (0, NULL) and (d & 0xFFFF) < len(c.tags) else None
                        if st is not None and st.name:
                            m[st.name.lower()].append((c, vt))
        return m

    def _campaign_shaders_by_stem(self):
        """Like _vista_shaders_by_stem over the stock Vista CAMPAIGN maps, for templates no multiplayer map has (prt_lightmap: only
        04b_floodlab, which holds Bungie's own Vista port of the alphagasgiant cable_room_whole shaders).  A campaign map has no
        placeholders, so a copy brings its whole closure (template, passes, vertex / pixel shaders) along as local tags."""
        m = getattr(self, '_css', None)
        if m is None:
            m = self._css = collections.defaultdict(list)
            for f in sorted(glob.glob(os.path.join(self.vm, '*.map'))):
                if os.path.basename(f).lower() in ('shared.map', 'single_player_shared.map', 'mainmenu.map'):
                    continue
                try:
                    c = self.donor_cache(f)
                except Exception:                                        # noqa: BLE001
                    continue
                if c.header.type in (1, 2):                              # multiplayer / mainmenu: already in _vista_shaders_by_stem
                    continue
                for vt in c.tags:
                    if vt.group == 'shad' and vt.addr:
                        d = struct.unpack('<I', c.read_addr(vt.addr + 4, 4))[0]
                        st = c.tags[d & 0xFFFF] if d not in (0, NULL) and (d & 0xFFFF) < len(c.tags) else None
                        if st is not None and st.name and st.addr:
                            m[st.name.lower()].append((c, vt))
        return m

    def campaign_tag(self, group, name):
        """(cache, tag) of a shader-family tag that only the stock Vista campaign maps have (via the shaders using it), else None."""
        if group != 'stem':
            return None
        for c, vt in self._campaign_shaders_by_stem().get((name or '').lower(), [])[:1]:
            d = struct.unpack('<I', c.read_addr(vt.addr + 4, 4))[0]
            return c, c.tags[d & 0xFFFF]
        return None

    def shaders_on_stem(self, stem):
        """Vista shaders on this template: the multiplayer ones, else (no multiplayer map has the template) the campaign ones."""
        return self._vista_shaders_by_stem().get(stem) or self._campaign_shaders_by_stem().get(stem) or []

    def shader_reference(self, stem):
        lst = self.shaders_on_stem(stem)
        return lst[0] if lst else None

    def same_name_shader(self, name, stem, prefer=None):
        """Bungie's own Vista port of THIS shader: (cache, tag) of the stock Vista shader with this name on this template - shared /
        multiplayer / mainmenu first, then campaign (aerial2's injected Sacred Icon sky = 06a_sentinelwalls' sky_shq_*: its layers took
        an arbitrary multiplayer skeleton's 10 overlays and scrolled, aerial2-sky RC2, 2026-10-26) - or None.  A campaign copy only when
        its template lays the arrays out like the Vista template the output references (stem_fingerprint; mandatory).
        prefer: a map file name ('backwash.map') whose port wins - the level of the stock Xbox copy the build compares with, because
        Bungie's copies of one name differ between levels (battle_rifle tens: 3 bitmaps in shared.map, 4 in backwash / mainmenu)."""
        n = (name or '').lower()
        if not n:
            return None
        mp = [(c, vt) for c, vt in self._vista_shaders_by_stem().get(stem, []) if (vt.name or '').lower() == n]
        camp = [(c, vt) for c, vt in self._campaign_shaders_by_stem().get(stem, []) if (vt.name or '').lower() == n]
        first = mp[0] if mp else None
        for c, vt in mp + camp:
            if (c, vt) not in mp and not self.campaign_stem_matches(c, vt, stem):
                continue
            if first is None:
                first = c, vt
            if not prefer or os.path.basename(c.path).lower() == prefer.lower():
                return c, vt
        return first

    def stem_print(self, c, st):
        """shader_params.stem_fingerprint of the Vista stem tag st in cache c (memo)."""
        memo = self.__dict__.setdefault('_stem_prints', {})
        k = (c.path, st.index)
        if k not in memo:
            from .. import shader_params as SP
            try:
                memo[k] = SP.stem_fingerprint(T.decode_tag(c.read_addr, self.lv['stem'], st.addr, 'stem'), lambda d: _vname(c, d))
            except Exception:                                            # noqa: BLE001
                memo[k] = None
        return memo[k]

    def campaign_stem_matches(self, c, vt, stem):
        """The campaign shader vt's template has the fingerprint of the stock copy the output links (shared.map, else the first stock
        multiplayer map's); True when no multiplayer copy exists (the campaign template itself comes along, as for prt_lightmap)."""
        d = struct.unpack('<I', c.read_addr(vt.addr + 4, 4))[0]
        st = c.tags[d & 0xFFFF] if d not in (0, NULL) and (d & 0xFFFF) < len(c.tags) else None
        if st is None or not st.addr:
            return False
        ref = self.vs_name.get(('stem', stem))
        rc = self.vsh
        if ref is None:
            hits = self.donor.get(('stem', stem))
            if not hits:
                return True
            rc = self.donor_cache(hits[0][0])
            ref = rc.tags[hits[0][1]]
        a, b = self.stem_print(c, st), self.stem_print(rc, ref)
        ok = a is not None and a == b
        if not ok:
            self.stats['same_name_campaign_stem_differs'] += 1
        return ok

    def sky_shader_indices(self):
        """Xbox tag indices of the shaders this map's skies draw with (sky_shader_names: sky -> Render Model -> Materials; memo)."""
        s = getattr(self, '_sky_shaders', None)
        if s is None:
            s = self._sky_shaders = sky_shader_names(self.xc, self.xc.read, self.lx, self.xc.tag_by_datum, indices=True)
        return s

    def output_stem(self, t, stem):
        """The Vista template an Xbox shader is built on: its own (stem), or - for a shader this map's sky draws with - the sky_
        re-template Bungie gave that template's sky layers for PC (data\\xbox_shader_slot_maps.json 'sky_retemplate', learned
        from every stock sky: two_alpha_clouds -> sky_two_alpha_clouds 8 of 8, one_alpha_env(_clamped)
        -> sky_ 3 + 3, two_add_detail_masked 2, one_add_illum(_detail) -> sky_one_add_illum_detail 7 of 8; only containment's
        sky_shq_mtdoombeam_day kept its plain template).  aerial2's injected Sacred Icon sky_shq_stormcloudjungle was built on a
        multiplayer two_alpha_clouds skeleton with 3 bitmaps instead of 06a_sentinelwalls' sky_two_alpha_clouds port with 2
        (2026-10-26).  Only when a stock Vista shader on the re-template exists, and never against Bungie's own choice for THIS shader:
        a Vista port of the name on the Xbox template and none on the re-template keeps the Xbox template (the mods' copies of
        containment's sky_shq_mtdoombeam_day, which Bungie left on one_add_illum_detail: 10 corpus maps)."""
        v = load_sky_retemplate(self.opts.get('slot_pairs')).get(stem)
        if not v or t is None or t.index not in self.sky_shader_indices() or not self.shaders_on_stem(v):
            return stem
        memo = self.__dict__.setdefault('_out_stem', {})
        if (t.index, stem) not in memo:
            if self.same_name_shader(t.name, v) is None and self.same_name_shader(t.name, stem) is not None:
                self.stats['shader_sky_retemplate_port_kept'] += 1
                memo[(t.index, stem)] = stem
            else:
                memo[(t.index, stem)] = v
        return memo[(t.index, stem)]

    def shader_slot_map(self, stem, vstem=None):
        """Learned per template: {array: [xbox slot for each Vista slot, or None = keep the Vista reference value]} (learn_slot_map).
        Pairs = shaders with the same name on this template in the Xbox map / pack shared.map and in a stock Vista map (the map's own copy
        first), then Bungie's own ports from EVERY stock map (data\\xbox_shader_slot_maps.json) under the
        names the map does not have: one map has 0-4 pairs for most templates, too few to tell a permutation from a coincidence (aerial2's
        sky_one_alpha_env: 4), and none for Deep_sea / Lakeside / Waterworld's sky_one_alpha_env or any map's sky_one_alpha_env_clamped,
        whose sky layers then took identity and drew black or not at all (2026-10-26).  Without any pair: None (build_shader: identity
        when the lengths agree).
        Self test: a stock pair that the stock pairs' own map reproduces and this map does not (the map's pairs outvoted Bungie's) is
        reported as shader_slot_selftest.
        vstem: the Vista template when it is not the Xbox one (output_stem's sky re-template): Xbox shaders on stem pair with Vista
        shaders on vstem, and the table's pairs are keyed 'stem>vstem' (two_alpha_clouds Bitmaps [tile, mask, color_white] ->
        sky_two_alpha_clouds [tile, mask])."""
        cache = getattr(self, '_slot_maps', None)
        if cache is None:
            cache = self._slot_maps = {}
        key = stem if not vstem or vstem == stem else stem + '>' + vstem
        if key in cache:
            return cache[key]
        vby = {vt.name.lower(): (c, vt) for c, vt in self.shaders_on_stem(vstem or stem)}
        own, names = [], set()
        for xcache in [c for c in (self.xc, self.ps) if c is not None]:
            for t in xcache.tags:
                n = (t.name or '').lower()
                if t.group != 'shad' or not t.addr or n not in vby or n in names:
                    continue
                if xcache is self.xc and self.stock_xbox_equal(t) is False:
                    # a stock name over the mod's own content is no evidence of the slot order (aerial2 headlong_horiz_glass); the
                    # stock table's pair of that name stands in for it
                    self.stats['slot_pair_modified_skipped'] += 1
                    continue
                emptied = []
                try:
                    xr = self.decode_shader(xcache, t, emptied)
                except Exception:                                         # noqa: BLE001
                    xr = None
                if xr is None or any('Postprocess Definition' in p for p in emptied):
                    # a malformed shader is no evidence and must not fail every custom shader on its template (Sanctus 0x8073E930;
                    # UMconst tex_bump only got through because the old 24-pair cap stopped before its broken stock-named shader);
                    # one whose emptied blocks lie outside the postprocess (Postprocess Properties) still has whole arrays
                    self.stats['slot_pair_undecodable'] += 1
                    continue
                if self.shader_stem(xcache, xr) != stem:
                    continue
                vc, vt = vby[n]
                vr = T.decode_tag(vc.read_addr, self.lv['shad'], vt.addr, 'shad')
                own.append(slot_evidence(_pp_arrays(xr, lambda d, c=xcache: _xname(c, d)), _pp_arrays(vr, lambda d, c=vc: _vname(c, d))))
                names.add(n)
        table = load_slot_evidence(self.opts.get('slot_pairs'))
        if not table and not self.stats['slot_pairs_missing']:
            self.stats['slot_pairs_missing'] += 1
            self.warn('slot_pairs_missing', 'no stock shader slot table (%s): custom shaders learn their slot maps from the map alone'
                      % SLOT_MAP_FILE)
        stock = [ev for n, ev in table.get(key, [])]
        pairs = own + [ev for n, ev in table.get(key, []) if n not in names]
        resolve = self.__dict__.get('_vista_slot_values')
        if resolve is None:
            resolve = self._vista_slot_values = VistaSlotValues(self.vm, self.lv)
        out = {a: learn_slot_map(pairs, a, resolve) for a in self.SHADER_ARRAYS}
        for a in self.SHADER_ARRAYS:
            if out[a] is None or not stock:
                continue
            ref = learn_slot_map(stock, a, resolve)
            ref_missed = {p for p, _j in slot_map_misses(ref, stock, a)}         # Bungie's own port edits: no slot map reproduces them
            lost = [(p, j) for p, j in slot_map_misses(out[a], stock, a) if p not in ref_missed]
            if lost:
                p, j = lost[0]
                self.warn('shader_slot_selftest', '%s %s: slot map %s misses %d of %d stock pair(s) that the stock map %s reproduces '
                          '(e.g. %s Vista slot %d)' % (stem.split('\\')[-1], a, out[a], len({q for q, _j in lost}), len(stock), ref,
                                                       table[key][p][0].split('\\')[-1], j))
        cache[key] = out
        return out

    # postprocess blocks that route the animated parameters: equal in the skeleton and the Xbox shader = the Xbox Overlays fit it
    SHADER_ROUTING = ('Levels Of Detail', 'Layers', 'Passes', 'Implementations', 'Overlay References', 'Animated Parameters',
                      'Animated Parameter References')

    def build_shader(self, o, conv):
        """Shader built from the Xbox data -> Vista: the converted Xbox root fields (runtime properties, flags, ...) + the Postprocess
        Definition of a Vista SKELETON on the same template, whose static arrays get the Xbox values through shader_slot_map.
        * Skeleton = Bungie's own Vista port of this shader when a stock map has one (same_name_shader; the port of the level whose stock
          Xbox copy is closest, ditw2 cyclotron dark_metal: cyclotron's), and then only the slots the mod changed against that copy are
          taken (aerial2 headlong_horiz_glass: Bitmaps 2 / 4 = flat_dark_scratchy / alphagasgiant_sky, everything else Bungie's port;
          bitmaps compare by name and pixels, stock_slot_kept).  Else the template's first Vista shader (shader_reference) with every
          mapped slot.
        * The Xbox Overlays (functions, periods) replace the skeleton's when both route their animated parameters identically
          (SHADER_ROUTING): aerial2's sky_shq_stormcloudjungle scrolled at the skeleton ascension_clouds' 50 s instead of 180 / 60 s.
        * Only what the output keeps is resolved: the Xbox postprocess / predicted resources are not remapped, and skeleton slots that
          get Xbox values are freed before the skeleton is remapped, so neither side's unused bitmaps come along as orphan tags
          (aerial2: the stale nm_horiz_glass / earthcity_sky hints, the skeleton's nm_horiz_glass).
        * A non-bitmap tag in a bitmap slot (protection junk, a cross-group alias) keeps the skeleton's bitmap."""
        xr = self.decode_shader(self.xc, o.src)
        stem = self.shader_stem(self.xc, xr)
        vstem = self.output_stem(o.src, stem)                             # a sky layer: Bungie's PC sky_ re-template (output_stem)
        xa = _pp_arrays(xr, lambda d: _xname(self.xc, d))
        ref = self.same_name_shader(o.name, vstem)
        same = ref is not None
        sarr = self.closest_stock_arrays(o.src, xa, stem) if same else None
        if sarr is not None:
            ref = self.same_name_shader(o.name, vstem, prefer=os.path.basename(sarr[1].path)) or ref
        if ref is None:
            ref = self.shader_reference(vstem)
        if ref is None:
            self.remap_xbox(conv)
            return conv
        f_pp, f_pr = conv.field('Postprocess Definition'), conv.field('Predicted Resources')
        cpp = conv.blocks.get(f_pp.offset) or []
        conv.blocks[f_pp.offset] = []
        if f_pr is not None:
            conv.blocks[f_pr.offset] = []
        self.remap_xbox(conv)                                             # the root fields only (see above)
        rc, rt = ref
        base = T.decode_tag(rc.read_addr, self.lv['shad'], rt.addr, 'shad')
        smap = self.shader_slot_map(stem, vstem)
        if vstem != stem:
            self.stats['shader_sky_retemplate'] += 1
            ov0 = ', sky re-template %s -> %s' % (stem.split('\\')[-1], vstem.split('\\')[-1])
        else:
            ov0 = ''
        bpp = base.block('Postprocess Definition')
        take = []                                                         # (array, Vista slot, Xbox slot)
        if bpp and cpp:
            for a in self.SHADER_ARRAYS:
                bv, cv = bpp[0].block(a), cpp[0].block(a)
                m = smap.get(a)
                if m is None:
                    m = list(range(len(bv))) if len(bv) == len(cv) else []
                for j, i in enumerate(m):
                    if i is None or j >= len(bv) or i >= len(cv):
                        continue
                    d = cv[i].u32(0) if a == 'Bitmaps' else None
                    if d not in (None, 0, NULL):
                        xt = self.xc.tag_by_datum(d)
                        if xt is not None and xt.index in self.data_twin:
                            xt = self.xc.tags[self.data_twin[xt.index]]   # what the Xbox drew (Satans_Crypt 'Soul Reaper ' = bitm fire)
                        if xt is not None and xt.group != 'bitm':
                            self.stats['shader_slot_not_bitmap'] += 1
                            continue
                    if sarr is not None and self.stock_slot_kept(a, i, xa, d, sarr):
                        continue                                          # untouched by the mod: Bungie's port keeps its own value
                    take.append((a, j, i))
        if bpp:
            # free the skeleton bitmaps the Xbox values replace (and their predicted-resource hints) before the skeleton is remapped
            bv = bpp[0].block('Bitmaps')
            freed = set()
            for a, j, i in take:
                if a == 'Bitmaps':
                    freed.add(bv[j].u32(0))
                    bv[j].set_u32(0, NULL)
            gone = freed - {k.u32(0) for k in bv} - {0, NULL}
            fb = base.field('Predicted Resources')
            if gone and fb is not None:
                base.blocks[fb.offset] = [k for k in base.blocks.get(fb.offset, []) if k.u32(4) not in gone]
            self.remap_donor(bpp[0], rc)
        preds = base.blocks.get(base.field('Predicted Resources').offset, [])
        for k in preds:
            self.remap_donor(k, rc)
        for a, j, i in take:
            n = bpp[0].block(a)[j]
            n.data[:] = cpp[0].block(a)[i].data
            self.remap_xbox(n)
        ov = ov0
        if bpp and cpp:
            same_routing = all(len(bpp[0].block(r)) == len(cpp[0].block(r)) and
                               all(bytes(x.data) == bytes(y.data) for x, y in zip(bpp[0].block(r), cpp[0].block(r)))
                               for r in self.SHADER_ROUTING)
            bo, co = bpp[0].block('Overlays'), cpp[0].block('Overlays')
            if same_routing and len(bo) == len(co) and co and any(
                    bytes(x.data[8:12]) != bytes(y.data[8:12]) or x.datas.get(0xC, b'') != y.datas.get(0xC, b'') for x, y in zip(bo, co)):
                for n in co:
                    self.remap_xbox(n)
                bpp[0].blocks[bpp[0].field('Overlays').offset] = co
                self.stats['shader_overlays_from_xbox'] += 1
                ov += ', Xbox overlays'
        self.remap_donor_ref(base, rc, 4)                                 # the template
        conv.blocks[f_pp.offset] = bpp
        if f_pr is not None:
            conv.blocks[f_pr.offset] = preds
            try:
                from ..handlers import shaders as HSH
                HSH.sync_predicted_resources(self, conv)
            except Exception as ex:                                       # noqa: BLE001
                self.warn('shader_predicted', repr(ex))
        conv.set_u32(4, base.u32(4))
        conv.data[0:4] = base.data[0:4]               # and its group: the Xbox root Template may hold junk (ref_group_allowed -> NONE)
        if same:
            # root fields only Vista has (Added Depth Bias Offset / Slope Scale; relayout leaves them 0): Bungie's values for this shader
            xn = {f.name for f in self.lx['shad'].fields}
            for f in conv.fields:
                if f.name not in xn and f.type not in T.BLOCK_TYPES + T.DATA_TYPES:
                    conv.data[f.offset:f.offset + f.size] = base.data[f.offset:f.offset + f.size]
        self.stats['custom_shader_converted'] += 1
        if same:
            self.stats['custom_shader_same_name'] += 1
            if sarr is not None and not take:
                self.stats['custom_shader_same_name_no_delta'] += 1
        self.warn('custom_shader_converted', '%s: Vista skeleton of %s%s + %d Xbox slots%s' % (
            o.name, rt.name, ' (its own port, %s)' % os.path.basename(rc.path) if same else '', len(take), ov))
        return conv

    def remap_donor_ref(self, node, cache, off):
        """One reference word of a donor-map node -> output datum (remap_donor for a single field)."""
        if cache is not self.vsh:
            node.set_u32(off, self.resolve_d(cache, node.u32(off)))

    # ---- node building -----------------------------------------------------------------------------------------------
    def first_person_bodies(self):
        """Xbox tag indices of the modes the map's matg Player Representation uses as 'First Person Body Model' (content, not names:
        scrambled / recovered maps call them mode_2770 ...).  Stock Vista sets mode Flags bit 0 'Render Model Force Third Person' on
        exactly these (shared.map: masterchief fp_body 0x9, elite and dervish fp_body 0xD); no Xbox mode has it."""
        fp = getattr(self, '_fp_bodies', None)
        if fp is not None:
            return fp
        fp = self._fp_bodies = set()
        gt, lay = self.xc.globals_tag, self.lx['matg']
        pr = next((f for f in lay.fields if f.name == 'Player Representation' and f.type in T.BLOCK_TYPES), None)
        fb = next((c for c in pr.children if c.name == 'First Person Body Model' and c.type.lower() == 'tagref'), None) if pr else None
        if gt is None or not gt.addr or fb is None:
            return fp
        try:
            root = T.decode_tag(self.xc.read, lay, gt.addr, 'matg', dropped=[])
        except Exception as ex:                                           # noqa: BLE001
            self.warn('fp_body_flag', 'matg %s: Player Representation unreadable (%r): first-person body flags left as they are'
                      % (gt.name, ex))
            return fp
        for e in root.blocks.get(pr.offset, []):
            t = self.xc.tag_by_datum(e.u32(fb.offset + (4 if fb.size == 8 else 0)))
            if t is not None and t.group == 'mode':
                fp.add(t.index)
        return fp

    def build_node(self, o):
        if o.kind == 'd':
            cache, vt = o.src
            root = T.decode_tag(cache.read_addr, self.lv[o.group], vt.addr, o.group)
            owners = []
            if o.group == 'ltmp' and o.index in self.full_ltmps:
                owners = self.donor_geometry(o, root, cache)
            elif o.group == 'ltmp':
                # a donor ltmp that carries a generated lightmap (stub_lightmaps, finish_lightmaps): only the root settings are kept
                # and ONE emptied group stays - the engine reads the first Lightmap Group's block counts through the block pointer, so an
                # empty Lightmap Groups block is a null read at map start (halo2.exe 0x6657D6).  The generated group replaces it
                # once the output BSP exists.  No donor bitmap comes along (Bitmap Group NONE).
                for f in root.fields:
                    if f.type in T.BLOCK_TYPES:
                        els = root.blocks.get(f.offset, [])
                        root.blocks[f.offset] = els[:1] if f.name == 'Lightmap Groups' else []
                    elif f.type in T.DATA_TYPES:
                        root.datas[f.offset] = b''
                for g in root.blocks.get(next((f.offset for f in root.fields if f.name == 'Lightmap Groups'), -1), []):
                    for ff in g.fields:
                        if ff.type in T.BLOCK_TYPES:
                            g.blocks[ff.offset] = []
                        elif ff.type in T.DATA_TYPES:
                            g.datas[ff.offset] = b''
                    g.set_u32(0x18, NULL)
                    g.set_u32(0x1C, NULL)
            if o.group in HK.OWNED:
                self.havok(root)              # a stock map's copy carries ITS tool's heap pointers in User Data (duffworld phmo crate_multi)
            self.remap_donor(root, cache)
            for nd, off in owners:
                nd.set_u32(off, o.datum)                                  # geometry_block_info owner (after remap_donor: a tagref field)
            return root
        t = o.src
        g = t.group
        if t.index in self.alias_of:
            # the tag's data IS another group's (aerial: weap ghost_gun = the deltatemple bridge MACHINE): built as that group, written
            # under this tag's own group / name - the netgame item collection spawns it on Xbox and the engine makes a machine of it
            g = self.alias_of[t.index]
            self.stats['aliased_as:%s' % g] += 1
        self._owner_fix = []
        self._after_remap = []                # (node, offset, output datum): references set to OUTPUT datums before remap_xbox
        pe = self.plan.entries.get(t.index)
        if pe is not None and pe.tainted_only and g == t.group and g not in TAINT_PORT_SKIP and t.index not in self.alias_of:
            port = self.vista_port_of_tainted(o)
            if port is not None:
                return port
        if g in MERGE_GROUPS:
            merged = self.merge_with_vista(o)
            if merged is not None:
                return merged
        lay_x = self.lx[g]
        try:
            root = self.decode_shader(self.xc, t) if g == 'shad' else T.decode_tag(self.xc.read, lay_x, t.addr, g)
        except Exception as ex:                                           # noqa: BLE001
            if g == 'shad':
                raise                                                     # decode_shader salvaged already: fallback_node's skeleton
            if g not in ('scnr', 'sbsp', 'ltmp', 'matg', 'ugh!'):
                # the salvage ladder (salvage_tag); a tag it refuses falls back to the Vista tag (fallback_node) as before
                try:
                    root = self.salvage_tag(t, g)[0]
                except Exception as sx:                                   # noqa: BLE001
                    self.stats['salvage_refused'] += 1
                    self.warn('salvage_refused', '%s %s: %s' % (g, t.name, str(sx)[:200]))
                    raise ex
            else:
                # the map's own tag: iceking / umbubble / ricochet scenarios carry 0xBABABABA / 0xCDCDCDCD heap fill (and stale pointers)
                # in Scenario Resources[0] and Scenario Cluster Data[0], editor-side data a mod tool left behind; those elements are
                # left out
                dropped = []
                root = T.decode_tag(self.xc.read, lay_x, t.addr, g, dropped=dropped)
                for d in dropped:
                    self.warn('junk_elements_dropped', '%s %s%s' % (g, t.name, d[:200]))
        xroot = root
        if lay_x is not self.lv[g]:
            root = relayout(xroot, self.lv[g].fields, self.lv[g].base_size or self.lv[g].computed_size)
            if g == 'jmad':
                jmad_animations(xroot, root, self.xc)
        if g == 'bitm':
            self.bitmap(o, xroot, root)
        self.convert_raw(o, root)
        if g == 'mode':
            self.repair_prt(o, root)                                      # damaged radiance transfer (prt_gen)
        if g == 'sbsp':
            self.ensure_device_mapping(o, root)                           # the engine reads element 0 of Portal=>Device Mapping unguarded
        if g == 'mode' and t.index in self.first_person_bodies() and not root.u16(4) & 1:
            # Flags bit 0 'Render Model Force Third Person', as the PC tool sets it on every first-person body (first_person_bodies).
            # Without it halo2.exe sub_5A2E8E (0x5A2EFC: mode +4 & 1) queues the body's sections (entry flag 0x400) to the
            # first-person list (dword_8F8098), drawn by sub_5A0FB4 in its own near 1/64 / far 1.0 pass instead of with the world
            # (verify-foundry-spartan, 2026-10-26: 108 local fp_body tags in 63 batch_v2 maps lacked it)
            root.set_u16(4, root.u16(4) | 1)
            self.stats['fp_body_force_third_person'] += 1
        if g == 'scnr':
            self.scenario(root)
        if g in HK.OWNED:
            self.havok(root)
        if g == 'MGS2':
            # light volume 'Fuzzy' (Volumes/Flags bit 3, the soft z-buffer blend) renders as a full-screen flickering striped wedge on
            # Vista; with it off the bulldog headlight is the white forward beam it is on Xbox (observed 2026-10-22)
            for v in root.block('Volumes'):
                fl = v.field('Flags')
                if fl is not None and v.u32(fl.offset) & 0x8:
                    v.set_u32(fl.offset, v.u32(fl.offset) & ~0x8)
                    self.stats['light_volume_fuzzy_cleared'] += 1
        if g in ('vehi', 'bipd', 'weap', 'scen', 'bloc', 'eqip', 'mach', 'ctrl'):
            self.drop_widgets(o, root)
        if g == 'shad':
            return self.build_shader(o, root)                             # resolves only the references the output keeps
        if g == 'hudg':
            f = root.field('Player Training Data')
            if f is not None and root.blocks.get(f.offset):
                root.blocks[f.offset] = []
                self.stats['hudg_training_emptied'] += 1
        if g == 'nhdt' and not self.opts.get('no_hud_upscale'):
            self.hud_offsets(root)
            self.hud_reticle_sprites(root)
        self.remap_xbox(root)
        for n, off, d in self._after_remap:
            n.set_u32(off, d)
        self._after_remap = []
        for n, off in self._owner_fix:
            n.set_u32(off, o.datum)                                       # geometry_block_info owner = the owning output tag
        self._owner_fix = []
        self.sort_engine_blocks(root)
        return root

    def hud_offsets(self, root):
        """nhdt built from Xbox data: Fullscreen Offset of every Bitmap / Text Widget x HUD_SCALE (Vista's doubled HUD, see HUD_SCALE).
        A Vista port (vista_port_of_tainted) never gets here: it already holds Bungie's Vista values."""
        for f in root.fields:
            if f.type not in T.BLOCK_TYPES or '/' + f.name not in HUD_OFFSET_BLOCKS:
                continue
            for w in root.blocks.get(f.offset, []):
                fo = w.field('Fullscreen Offset')
                if fo is None or fo.offset + 4 > len(w.data):
                    continue
                x, y = struct.unpack_from('<hh', w.data, fo.offset)
                struct.pack_into('<hh', w.data, fo.offset, *(max(-32768, min(32767, v * HUD_SCALE)) for v in (x, y)))
                self.stats['hud_offsets_scaled'] += 1

    RETICLE_SHEET = 'ui\\hud\\bitmaps\\new_hud\\crosshairs\\hud_reticles'

    def reticle_full_of_half(self):
        """{halfscreen sequence index: fullscreen sequence index} of the reticle sheet as Bungie pairs them in the stock Vista nhdt tags
        (shared.map; only indices with ONE fullscreen partner and never used fullscreen themselves), learned at run time."""
        got = self.__dict__.get('_ret_pairs')
        if got is not None:
            return got
        pairs, full = collections.defaultdict(set), set()
        sheet = self.vs_name.get(('bitm', self.RETICLE_SHEET))
        for t in self.vsh.tags:
            if t.group != 'nhdt' or not t.addr or sheet is None:
                continue
            try:
                r = T.decode_tag(self.vsh.read_addr, self.lv['nhdt'], t.addr, 'nhdt')
            except Exception:                                            # noqa: BLE001
                continue
            for w in r.block('Bitmap Widgets'):
                f = w.field('Bitmap')
                if w.u32(f.offset + (4 if f.size == 8 else 0)) != sheet.datum:
                    continue
                fs, hs = w.u8(w.field('Fullscreen Sequence Index').offset), w.u8(w.field('Halfscreen Sequence Index').offset)
                pairs[hs].add(fs)
                full.add(fs)
        got = self._ret_pairs = {h: next(iter(v)) for h, v in pairs.items() if len(v) == 1 and h not in full}
        return got

    def hud_reticle_sprites(self, root):
        """A mod's nhdt that draws the reticle sheet FULLSCREEN with a sprite Bungie only uses for split screen (the small Xbox-size
        copy: Tortuga's hand cannon = rocket_launcher on 23 = smg's split-screen reticle, crossbow = battle_rifle on 29 = needler's,
        cannon h_turret on 25 = chaingun_test's) gets that sprite's fullscreen twin (23 -> 0, 29 -> 6, 25 -> 2): the same reticle at
        PC resolution.  Halfscreen / quarterscreen indices stay."""
        pairs = self.reticle_full_of_half()
        if not pairs:
            return
        for w in root.block('Bitmap Widgets') if root.field('Bitmap Widgets') else []:
            f = w.field('Bitmap')
            b = self.xc.tag_by_datum(w.u32(f.offset + (4 if f.size == 8 else 0)))
            if b is None or (b.name or '').lower() != self.RETICLE_SHEET:
                continue
            fo = w.field('Fullscreen Sequence Index').offset
            v = pairs.get(w.u8(fo))
            if v is not None:
                w.data[fo] = v
                self.stats['hud_reticle_fullscreen_sprite'] += 1

    def hud_widget_bitmaps(self):
        """Xbox datums of the bitm tags a Bitmap / Screen Effect Widget of any nhdt of the map draws (bitmap: drawn x HUD_SCALE)."""
        got = self.__dict__.get('_hud_bitmaps')
        if got is None:
            got = self._hud_bitmaps = set()
            for t in self.xc.tags:
                if t.is_empty or t.group != 'nhdt' or not t.addr:
                    continue
                try:
                    root = T.decode_tag(self.xc.read, self.lx['nhdt'], t.addr, 'nhdt')
                except Exception:                                         # noqa: BLE001
                    continue                                              # its own build warns (salvage ladder)
                for n in root.iter_nodes():
                    if n.path not in HUD_BITMAP_BLOCKS:
                        continue
                    for f in n.fields:
                        if f.name == 'Bitmap' and f.type.lower() == 'tagref' and f.offset + f.size <= len(n.data):
                            d = struct.unpack_from('<I', n.data, f.offset + (4 if f.size == 8 else 0))[0]
                            if d != NULL:
                                got.add(d)
        return got

    def hud_upscale(self, o, xel, conv):
        """conv (convert_bitmap_tag) of a HUD widget bitmap -> the same elements HUD_SCALE x bigger (nearest neighbour: each Xbox texel
        becomes a HUD_SCALE x HUD_SCALE square, so the HUD looks as it did on Xbox at Vista's doubled size; DXT is decoded to A8R8G8B8
        first; every mip level is scaled, the level count stays), with pixels_offset recomputed.  None (and a warning) when an element
        is not a 2D bitmap: the tag stays as is."""
        import numpy as np
        from . import bitmaps_xbox as BX
        out, off = [], 0
        for i, (f, px) in enumerate(conv):
            w, h, d, typ = BX.element_params(xel[i])[:4]
            fmt, mips = f['format'], f['mipmap_count']
            if typ != 0 or d != 1 or fmt not in BX.BITS_PER_PIXEL or w <= 0 or h <= 0 or not 0 <= mips <= 16:
                self.warn('hud_upscale_skipped', '%s[%d]: type %d depth %d mips %d format %d (only 2D bitmaps)' % (
                    o.name, i, typ, d, mips, fmt), o.name)
                return None
            f = dict(f)
            if BX._is_dxt(fmt):
                px = BX.dxt_to_argb(px, w, h, fmt, mips)
                fmt = f['format'] = BX.FMT_A8R8G8B8
                f['dxt_decoded'] = True
            bpp = BX.BITS_PER_PIXEL[fmt] // 8
            levels = list(BX._level_dims(w, h, 1, mips))
            if bpp < 1 or len(px) != sum(lw * lh * bpp for lw, lh, _ in levels):
                self.warn('hud_upscale_skipped', '%s[%d]: %d pixel bytes for %dx%d mips %d format %d' % (
                    o.name, i, len(px), w, h, mips, fmt), o.name)
                return None
            # each new level has the size the engine derives from the new width / height (max(1, w * s >> l): not always s x the old
            # level, 103 -> 51 but 206 -> 103) and takes the nearest texel of the old level (level 0: every texel an s x s square)
            parts, p = [], 0
            for (lw, lh, _), (nw, nh, _) in zip(levels, BX._level_dims(w * HUD_SCALE, h * HUD_SCALE, 1, mips)):
                a = np.frombuffer(px, dtype=np.uint8, count=lw * lh * bpp, offset=p).reshape(lh, lw, bpp)
                parts.append(a[np.arange(nh) * lh // nh][:, np.arange(nw) * lw // nw].tobytes())
                p += lw * lh * bpp
            px = b''.join(parts)
            f['pixels_offset'] = off
            off += len(px)
            out.append((f, px))
        self.stats['hud_bitmaps_upscaled'] += 1
        return out

    def sort_engine_blocks(self, root):
        """Blocks the engine binary-searches by their first dword (a string id label; halo2.exe sub_4F5B8F) must be re-sorted after the
        by-text string id remap changed the numbers (same rule as the MCC path: h2conv.transcode._sort_offsets / sort_block_elements)."""
        from .. import transcode as X
        for n in root.iter_nodes():
            for off in X._sort_offsets(n.fields, n.group, n.path):
                kids = n.blocks.get(off)
                if kids and len(kids) > 1:
                    out = X.sort_block_elements(kids)
                    if out is not kids:
                        n.blocks[off] = out
                        self.stats['sorted_blocks'] += 1

    def drop_empty_weather(self):
        """A weather system whose Particle Systems block (+0) is EMPTY: the Xbox draws no weather, but Vista's map-load pre-cache
        (halo2.exe sub_408AF0 -> 0x408D52) takes Particle Systems[0]'s geometry_block_info (+0x40) of EVERY weat tag with no count check,
        reads tag-data garbage as the block info and crashes in sub_499F10 (+0x99F29, Tortuga 2026-11-02: the mod emptied
        deltatemple misty_rain).  Such a tag is dropped like a dead tag (references NONE, empty slot) = the same "no weather"."""
        n = 0
        for o in self.tags:
            if o.group == 'weat' and o.root is not None and not o.root.blocks.get(0):
                o.root = None
                n += 1
                self.warn('weather_empty_dropped', '%s: no particle systems (no weather on Xbox either) - dropped, references NONE '
                          '(Vista pre-caches Particle Systems[0] of every weather tag unchecked)' % o.name)
        self.stats['weather_empty_dropped'] = n

    def trim_mulg_weapons(self):
        """A converted multiplayer globals keeps Vista's Runtime > Weapons COUNT: the stock Xbox list has 18 entries, Vista's 17 (the
        same order; Vista's last one is the Xbox's 17th, the Xbox's 18th is NONE).  Trailing NONE entries past Vista's count are dropped
        (Tortuga's modded mulg, 2026-11-02); a real weapon there is kept and reported."""
        vt = next((t for t in self.vsh.tags if t.group == 'mulg' and t.addr), None)
        if vt is None:
            return
        vroot = T.decode_tag(self.vsh.read_addr, self.lv['mulg'], vt.addr, 'mulg')
        want = len(vroot.block('Runtime')[0].block('Weapons')) if vroot.block('Runtime') else None
        for o in self.tags:
            if o.group != 'mulg' or o.kind != 'x' or o.root is None or want is None or not o.root.block('Runtime'):
                continue
            rt = o.root.block('Runtime')[0]
            f = rt.field('Weapons')
            ws = rt.blocks.get(f.offset) or []
            ref = ws[0].fields[0] if ws else None
            while len(ws) > want and ref is not None and ws[-1].u32(ref.offset + 4) in (0, NULL):
                ws.pop()
                self.stats['mulg_weapons_trimmed'] += 1
            if len(ws) > want:
                self.warn('mulg_weapons_extra', '%s: %d Runtime weapons, Vista has %d - the extra ones are real weapons, kept' % (
                    o.name, len(ws), want))

    def set_object_radii(self):
        """--object-radius OBJECT=R: the user's own Bounding Radius (+0x4) for a LOCAL object tag (a per-map request, never automatic:
        Tortuga's boat - the mod's warthog - culled too early with its stock 1.08, 2026-11-02).  A linked (shared) tag is not changed."""
        want = {n.lower(): r for n, r in (self.opts.get('object_radius') or ())}
        if not want:
            return
        done = set()
        for o in self.tags:
            if o.root is None or o.group not in OBJECT_TYPE_GROUPS or (o.name or '').lower() not in want:
                continue
            r = want[o.name.lower()]
            old = struct.unpack_from('<f', o.root.data, 4)[0]
            struct.pack_into('<f', o.root.data, 4, r)
            done.add(o.name.lower())
            self.stats['object_radius_set'] += 1
            self.warn('object_radius_set', '%s %s: Bounding Radius %.3f -> %.3f (--object-radius)' % (o.group, o.name, old, r))
        for n in sorted(set(want) - done):
            self.warn('object_radius_not_local', '%s: no local object tag of that name - --object-radius not applied' % n)

    def hide_invisible_instances(self):
        """An instanced geometry definition whose EVERY render part uses a material with a NONE shader draws nothing, but each of its
        instances still takes one entry of the per-frame render budget (render_visible_sections[850]; Vista halo2.exe sub_59C1BC
        `cmp edi,352h`), filled clusters -> instances -> objects.  Tortuga (05a_deltaapproach bsp_01 with 166 / 178 materials nulled to
        make the temple invisible; collision kept) has 374 such instances of 397: they ate the budget and the ship / castle / cannon
        were cut at some views (2026-11-02; HCM's uncap made them stay).  The render pass only takes an instance whose definition has
        a non-zero Total Subpart Count (+0x24; sub_4BBFAA): that count is set to 0 here - the same picture, the collision (BSP nodes,
        physics) untouched, the budget freed."""
        n = 0
        for o in self.tags:
            if o.group != 'sbsp' or o.kind != 'x' or o.root is None or o.src is None or not o.src.addr:
                continue
            try:
                xr = T.decode_tag(self.xc.read, self.lx['sbsp'], o.src.addr, 'sbsp')
            except Exception:                                            # noqa: BLE001
                continue
            visible = []
            for m in xr.block('Materials'):
                f = m.field('Shader')
                visible.append(m.u32(f.offset + (4 if f.size == 8 else 0)) not in (0, NULL))
            xdefs = xr.block('Instanced Geometry Definitions')
            odefs = o.root.block('Instanced Geometry Definitions')
            if len(xdefs) != len(odefs) or not xdefs:
                continue
            for xd, od in zip(xdefs, odefs):
                mats = self._section_part_materials(xd)
                if not mats or any(0 <= m < len(visible) and visible[m] for m in mats) or any(not 0 <= m < len(visible) for m in mats):
                    continue
                f = od.field('Total Subpart Count')
                if f is not None and od.u16(f.offset):
                    struct.pack_into('<H', od.data, f.offset, 0)
                    n += 1
            if n:
                self.warn('bsp_invisible_instances_hidden', '%s: %d instanced geometry definition(s) whose every part has a NONE shader '
                          '(they draw nothing): Total Subpart Count 0, so their instances no longer take render-budget slots (collision '
                          'unchanged)' % (o.name, n))
        self.stats['bsp_invisible_instance_defs'] = n

    def _section_part_materials(self, node):
        """Material indices of the render Parts of an Xbox geometry section (instance definition / cluster): the type-0 resource of
        72-byte elements at section-data locator 0 (the Parts block), material index int16 at +4.  None when it cannot be read."""
        try:
            ptr = node.u32(node.field('Resource Block Offset').offset)
            size = node.u32(node.field('Resource Block Size').offset)
            sds = node.u32(node.field('Section Data Size').offset)
            if ptr in (0, NULL) or not size:
                return None
            blk = self.xc.raw(ptr, size)
            count = struct.unpack_from('<i', blk, 8)[0]
            rdata = blk[8 + sds:]
            for r in node.block('Resources'):
                if r.u8(0) == 0 and r.u16(4) == 0 and r.i16(6) == 72:
                    roff, rsz = r.u32(12), r.u32(8)
                    if count < 0 or 72 * count > rsz:
                        return None
                    return [struct.unpack_from('<h', rdata, roff + 72 * k + 4)[0] for k in range(count)]
        except Exception:                                                # noqa: BLE001
            return None
        return None

    def drop_dead_tags(self):
        """Tags that failed with no fallback have no data: every reference to them becomes NONE (a reference to an empty tag is a null
        pointer in the engine) and their slot is written empty."""
        dead = {o.datum for o in self.tags if o.root is None}
        if not dead:
            return
        n = 0
        for o in self.tags:
            if o.root is None:
                continue
            for node in o.root.iter_nodes():
                for f in node.fields:
                    if f.type.lower() != 'tagref':
                        continue
                    off = f.offset + (4 if f.size == 8 else 0)
                    if struct.unpack_from('<I', node.data, off)[0] in dead:
                        struct.pack_into('<I', node.data, off, NULL)
                        if f.size == 8:
                            node.data[f.offset:f.offset + 4] = b'\xff\xff\xff\xff'
                        n += 1
        self.stats['dead_tags'] = len(dead)
        self.stats['dead_tag_refs_nulled'] = n
        self.warn('dead_tags', '%d tag(s) could not be converted and have no Vista fallback: %d reference(s) set to NONE' % (len(dead), n))

    FALLBACK = {'bitm': 'shaders\\default_bitmaps\\bitmaps\\gray_50_percent'}

    def fallback_node(self, o):
        """A tag that failed to convert must not be written empty (a referenced bitm with no data = a null bitmap element: halo2.exe
        0x6659A3, dkong 2026-10-22).  Copy the Vista tag of the same name (shared.map or a stock map) under this tag's own datum,
        else the group's default (bitm: Vista's gray_50_percent); None (empty) only when neither exists."""
        if o.group in ('scnr', 'sbsp', 'ltmp', 'matg', 'ugh!'):
            raise RuntimeError('the map\'s own %s %s cannot be converted' % (o.group, o.name))
        cands = [(self.vsh, self.vs_name.get((o.group, (o.name or '').lower())))]
        base = PX.base_name(o.name or '')
        if base.lower() != (o.name or '').lower():
            # a clone ('flaming_death (1)') Vista has no tag of: Bungie's tag of the BASE name, the same clone rule as the shader family
            # (RC6) - a dead clone left 26 projectiles of Chastebeaver with no detonation damage at all (2026-10-27)
            cands.append((self.vsh, self.vs_name.get((o.group, base.lower()))))
        hits = self.donor.get((o.group, (o.name or '').lower())) or []
        if hits:                                                  # a stock map's geometry blocks are copied along (donor_geometry)
            c = self.donor_cache(hits[0][0])
            cands.append((c, c.tags[hits[0][1]]))
        dn = self.FALLBACK.get(o.group)
        if dn:
            cands.append((self.vsh, self.vs_name.get((o.group, dn))))
        if o.group == 'shad' and o.kind == 'x':
            # last resort for a shader that cannot be built: Bungie's port of it, else a Vista shader on its template - it renders with
            # the skeleton's textures, never as a NONE material (a NONE mode material is untested on the Vista renderer)
            stem = self.output_stem(o.src, self.xbox_shader_stem(self.xc, o.src))
            ref = self.same_name_shader(o.name, stem) or self.shader_reference(stem)
            if ref is not None:
                cands.append(ref)
        for c, vt in cands:
            if vt is None or not vt.addr:
                continue
            try:
                root = T.decode_tag(c.read_addr, self.lv[o.group], vt.addr, o.group)
                # a stock map's model: its geometry blocks live in THAT map (soccer bm_rock_sm pointed at burial_mounds' 0xA96E00 in
                # this file, 2026-10-26); shared.map tags keep their shared raw pointers
                owners = self.donor_geometry(o, root, c) if c is not self.vsh and o.group in GEOMETRY_GROUPS else []
                self.remap_donor(root, c)
                for nd, off in owners:
                    nd.set_u32(off, o.datum)
                if o.group == 'bitm':
                    # element +0x4C = the OWNING tag's datum; remap_donor resolved it to the Vista shared tag (validate bitmap.parent_datum)
                    for e in root.block('Bitmaps'):
                        struct.pack_into('<I', e.data, 0x4C, o.datum)
                self.stats['fallback:%s' % o.group] += 1
                self.warn('convert_fallback', '%s %s: replaced by the Vista tag %s' % (o.group, o.name, vt.name))
                return root
            except Exception as ex:                                       # noqa: BLE001
                self.warn('convert_fallback_failed', '%s %s via %s: %r' % (o.group, o.name, vt.name, ex))
        return None

    def vista_port_of_tainted(self, o):
        """Bungie's Vista port (the Vista shared.map tag of that name) for an Xbox tag the plan converts ONLY by taint - its own content
        was verified equal to the pack shared.map / a stock Xbox copy (plan_xbox Entry.tainted_only) - with every reference set to what
        this map's own reference at the same place becomes (this map's converted tag where the Xbox tag points at one).  Policy (decision of
        2026-10-27): stock tags take Bungie's Vista port.  Converting the Xbox bytes instead kept the Xbox values Bungie changed for PC:
        hlmt masterchief_mp, tainted by mode masterchief's genuine LOD trick, kept disappear 40 / fade 30 / L1-L4 10, 5, 2.8, 1.8 where
        the Vista port has 150 / 130 / 30, 20, 15, 8, and Spartans vanished at 40 wu on PC in 145 batch_v4 maps (Foundry included;
        audit oracle c).  Only when the port has the SAME structure as the Xbox tag (layout, every block count), the same reference
        targets (group + name, or NONE, at every reference) and the same string ids (by text): otherwise None and the tag converts as before (stat
        vista_port_refused:<why>).  The prediction hints (PORT_OWN_BLOCKS) are not compared: the port keeps Bungie's (references
        to shared.map placeholders, valid in every output).  Not for TAINT_PORT_SKIP groups (raw data / geometry / sound / built shaders have their own path)
        nor for a tag with a per-map widget drop."""
        t = o.src
        g = t.group
        vt = self.vs_name.get((g, (PX.vista_name(g, t.name) or '').lower()))
        if vt is None or not vt.addr:
            self.stats['vista_port_refused:no_port'] += 1
            return None
        if self.widgets_to_drop(t):
            return None
        lay_x, lay_v = self.lx[g], self.lv[g]
        try:
            xroot = T.decode_tag(self.xc.read, lay_x, t.addr, g)
            vroot = T.decode_tag(self.vsh.read_addr, lay_v, vt.addr, g)
        except Exception:                                                # noqa: BLE001
            self.stats['vista_port_refused:decode'] += 1
            return None
        conv = xroot if lay_x is lay_v else relayout(xroot, lay_v.fields, lay_v.base_size or lay_v.computed_size)
        why = self._port_mismatch(conv, vroot)
        if why and g in PORT_BY_NAME_GROUPS:
            # Bungie reworked this tag for PC (hudg: training strings): the port stands, its references go to the map's own tags of
            # their names - the mod's hud_messages (weapon pickup texts) through Tortuga's tainted hudg default (2026-11-02)
            self.remap_donor(vroot, self.vsh)
            self.prefer_own_tags(vroot)
            self.stats['vista_port_by_name:%s' % g] += 1
            self.warn('tainted_from_vista_port', '%s %s: Bungie\'s Vista port (%s), references by name to this map\'s tags' % (
                g, t.name, why[1][:80]))
            return vroot
        if why:
            self.stats['vista_port_refused:%s' % why[0]] += 1
            self.warn('vista_port_refused', '%s %s: %s - converted from the Xbox data' % (g, t.name, why[1][:120]))
            return None
        for n in list(conv.iter_nodes()):
            for f in n.fields:
                if f.type in T.BLOCK_TYPES and '/' + f.name in PORT_OWN_BLOCKS:
                    n.blocks[f.offset] = []      # the port keeps its own hints: the Xbox ones must not bring tags in (resolve_x)
        self.remap_xbox(conv)                    # what each Xbox reference becomes (the same targets as the port's, by the check above)
        self.remap_donor(vroot, self.vsh)        # the port's string ids
        self._copy_refs(conv, vroot)
        if g in HK.OWNED:
            self.havok(vroot)
        self.stats['vista_port_tainted:%s' % g] += 1
        self.warn('tainted_from_vista_port', '%s %s (%s): Bungie\'s Vista port, references to this map\'s tags' % (
            g, t.name, (self.plan.entries[t.index].why or '')[:80]))
        return vroot

    def _port_mismatch(self, a, b):
        """None when the Xbox tag a (in the Vista layout) and the Vista port b have the same structure, reference targets and string ids, else
        (stat key, detail)."""
        if a.fields is not b.fields and [(f.offset, f.type, f.name) for f in a.fields] != [(f.offset, f.type, f.name) for f in b.fields]:
            return 'layout', '%s layout' % (a.path or '/')
        if len(a.data) != len(b.data):
            return 'layout', '%s size' % (a.path or '/')
        for f in a.fields:
            tl = f.type.lower()
            if f.type in T.BLOCK_TYPES and '/' + f.name in PORT_OWN_BLOCKS:
                continue                         # prediction hints: the port keeps its own (shared.map placeholders stay valid)
            if f.type in T.BLOCK_TYPES:
                ka, kb = a.blocks.get(f.offset) or [], b.blocks.get(f.offset) or []
                if len(ka) != len(kb):
                    return 'count', '%s/%s count %d vs %d' % (a.path, f.name, len(ka), len(kb))
                for x, y in zip(ka, kb):
                    w = self._port_mismatch(x, y)
                    if w:
                        return w
            elif tl == 'tagref':
                off = f.offset + (4 if f.size == 8 else 0)
                xd, vd = a.u32(off), b.u32(off)
                xt = self.xc.tag_by_datum(xd) if xd not in (0, NULL) else None
                vi = vd & 0xFFFF if vd not in (0, NULL) else None
                vv = self.vsh.tags[vi] if vi is not None and vi < len(self.vsh.tags) and self.vsh.tags[vi].datum == vd else None
                la = (xt.group, (PX.vista_name(xt.group, xt.name) or '').lower()) if xt is not None else None
                lb = (vv.group, (vv.name or '').lower()) if vv is not None else None
                if la != lb:
                    if xt is not None and vv is not None and xt.group == vv.group and self.family_gone(xt):
                        self.__dict__.setdefault('_keep_port_ref', set()).add((id(a), f.offset))
                        continue                 # Bungie replaced a shader-family tag Vista dropped: the port's (_copy_refs keeps it)
                    return 'reference', '%s/%s %s vs %s' % (a.path, f.name, la, lb)
            elif tl == 'stringid':
                # names tie the tag to the models / animations it references (hlmt region / permutation / damage section names):
                # a port that renamed one would not fit this map's own (Xbox) model
                xs, vs = a.u32(f.offset), b.u32(f.offset)
                xa = (self.xc.string_id(xs) or '') if xs not in (0, NULL) else ''
                vb = (self.vsh.string_by_index(vs & 0xFFFFFF) or '') if vs not in (0, NULL) else ''
                if xa != vb:
                    return 'string_id', '%s/%s %r vs %r' % (a.path, f.name, xa, vb)
        return None

    def _copy_refs(self, a, b):
        """The (remapped) reference words of a into b, field by field, block element by element (same structure: _port_mismatch)."""
        for f in a.fields:
            if f.type in T.BLOCK_TYPES and '/' + f.name not in PORT_OWN_BLOCKS:
                for x, y in zip(a.blocks.get(f.offset) or [], b.blocks.get(f.offset) or []):
                    self._copy_refs(x, y)
            elif f.type.lower() == 'tagref':
                if (id(a), f.offset) in self.__dict__.get('_keep_port_ref', ()):
                    continue                     # the Xbox target has no Vista tag (family_gone): Bungie's replacement stays
                b.data[f.offset:f.offset + f.size] = a.data[f.offset:f.offset + f.size]

    def family_gone(self, t):
        """True for an Xbox shader-family tag (plan action vista_donor) that has no Vista tag at all, so resolve_x makes it NONE.  Stock Xbox
        particle effects\\generic\\solids\\glass_breakable used stem particle_alpha_test_no_lighting, which Vista dropped (its port uses
        particle_alpha_multiply_add): converted from the Xbox bytes it kept a NONE Shader Template and the particle renderer read a garbage
        postprocess pointer through it - halo2.exe+0xC49D1 the moment a projectile hit a glass material (Tortuga ship, 2026-11-02)."""
        e = self.plan.entries.get(t.index)
        if e is None or e.action != 'vista_donor':
            return False
        return self.resolve_x(t.datum) == NULL or t.index in self.__dict__.get('_family_substituted', ())

    def widgets_to_drop(self, t):
        """Lower-case widget tag names to remove from the Widgets of object tag `t` (an XboxTag): the --drop-widget entries
        (opts['drop_widgets'] = [(object tag name, widget tag name)]) plus the shipped rules (data\\xbox_widget_drops.json), which are keyed
        by CONTENT, not by map: a rule names the object tag, the widget and the content digest of the object's model (hlmt).  The warthog
        of the Phantom pack's bulldog carries the stock antenna widget but a different model (the bulldog motorcycle, which has no antenna
        marker): the Xbox never shows the antenna, Vista would draw it at the model origin.  The same warthog tag over any other model
        keeps its antenna."""
        memo = self.__dict__.setdefault('_widget_drops', {})
        if t.index in memo:
            return memo[t.index]
        name = (t.name or '').lower()
        want = {w.lower() for obj, w in (self.opts.get('drop_widgets') or ()) if obj.lower() == name}
        rules = [r for r in load_widget_drops(self.opts.get('widget_drops')) if r['object'] == name]
        if rules and t.addr and t.group in self.lx:
            try:
                digest = self.model_digest(t)
            except Exception:                                            # noqa: BLE001 - an unreadable tag simply has no rule
                digest = None
            want |= {r['widget'] for r in rules if digest is not None and r['model_digest'] == digest}
        memo[t.index] = want
        if want and any(r['object'] == name for r in rules):
            self.stats['widget_drop_rule'] += 1
        return want

    def model_digest(self, t):
        """Hex content digest (stock_index.Digester) of the Model (hlmt) tag an object tag points at, or None."""
        root = T.decode_tag(self.xc.read, self.lx[t.group], t.addr, t.group)
        f = root.field('Model')
        if f is None:
            return None
        h = self.xc.tag_by_datum(struct.unpack_from('<I', root.data, f.offset + (4 if f.size == 8 else 0))[0])
        if h is None or h.group != 'hlmt' or not h.addr:
            return None
        d = SI.Digester(self.xc).tag(h)
        return d.hex() if d else None

    def drop_widgets(self, o, root):
        """Remove the Widgets entries widgets_to_drop names (per-map --drop-widget entries and the shipped content-keyed rules)."""
        f = root.field('Widgets')
        if f is None or o.kind != 'x':
            return
        want = self.widgets_to_drop(o.src)
        if not want:
            return
        keep = []
        for e in root.blocks.get(f.offset, []):
            d = struct.unpack_from('<I', e.data, 4)[0]
            t = self.xc.tag_by_datum(d)
            if t is not None and (t.name or '').lower() in want:
                self.stats['widgets_dropped'] += 1
                continue
            keep.append(e)
        root.blocks[f.offset] = keep

    def havok(self, root):
        """Xbox Havok records are already the 32-bit image Vista uses; only the build's runtime values change (handlers/havok.py rules):
        Runtime Code Pointer -> the Vista per-class constant (RCP_VISTA), other runtime pointers -> 0, User Data >= 0x10000 (a tool heap
        pointer) -> 0.  The engine rewrites them at tag load anyway (0x47B844 / 0x47BE5C); this keeps the output identical in kind to stock."""
        paths = set(HK.OWNED.get(root.group, ()))
        for n in root.iter_nodes():
            if n.path not in paths:
                continue
            for off in HK.JUNK_DWORDS.get((root.group, n.path), ()):
                if off + 4 <= len(n.data) and n.u32(off):
                    n.set_u32(off, 0)                                     # tool-heap garbage dwords (stock Vista: 0)
                    self.stats['havok_fields_fixed'] += 1
            if root.group == 'vehi' and n.path == '/Phantom Shapes':
                for f in n.fields:                                        # child slots: the Vista 'unused' pattern (the engine rewrites
                    if HK._VEHI_SLOT.match(f.name):                       # used ones from the multi-sphere count, 0x511C8E)
                        if f.name.startswith('Shape Index'):
                            struct.pack_into('<H', n.data, f.offset, 0xFFFF)
                        else:
                            n.data[f.offset:f.offset + f.size] = b'\0' * f.size
            for f in n.fields:
                kind = HK._kind(f)
                if kind is None or f.size != 4:
                    continue
                v = n.u32(f.offset)
                if kind == 'rcp' and v:
                    n.set_u32(f.offset, HK.RCP_VISTA.get((root.group, n.path, f.offset), 0))
                elif kind == 'rtptr' and v:
                    n.set_u32(f.offset, 0)
                elif kind == 'user' and v >= 0x10000:
                    n.set_u32(f.offset, 0)
                else:
                    continue
                self.stats['havok_fields_fixed'] += 1

    def finish_decorators(self):
        """Decorator sets that cannot draw (2026-10-25, Killtrocity Aerial).  A palette entry whose DECR geometry has no render data (Xbox
        block rds 0: 'hklb' + zeros + 'fklb') draws nothing on Xbox, and a BSP decorator group / decal whose set index is past the end of
        the palette draws nothing either (Aerial: the author replaced the 5-set palette with 3 sets and kept the BSP decorator data: 131
        placements of the empty set 0, 64 of the missing set 4 - the BSP shows no decorators at all on Xbox).  Vista crashes on both: an
        empty set is registered twice at map load (halo2.exe 0x499F29, index 0xFFFF) and an out-of-range set index reads past the palette.
        Lossless clean-up: groups / decals of such sets are removed from every Decorators block (scenario + BSPs; each cell's contiguous
        group range is rebuilt) and the sets themselves leave the palette (remaining indices renumbered)."""
        sc = self.tags[0].root if self.tags and self.tags[0].group == 'scnr' else None
        if sc is None:
            return
        fp = sc.field('Decorator Palette')
        pal = sc.blocks.get(fp.offset) or []
        xsc = T.decode_tag(self.xc.read, self.lx['scnr'], self.xc.scenario.addr, 'scnr', dropped=[])
        xpal = xsc.block('Decorator Palette')
        drawable = []
        for xe in xpal:
            d = struct.unpack_from('<I', xe.data, 4)[0]
            t = self.xc.tag_by_datum(d) if d not in (0, NULL) else None
            ok = False
            if t is not None and t.addr:
                try:
                    ok = struct.unpack_from('<I', self.xc.read(self.xc.tag_addr(t), 0x48), 0x44)[0] > 0
                except Exception:                                         # noqa: BLE001
                    ok = False
            drawable.append(ok)
        if len(drawable) != len(pal):
            return
        keep = [i for i, ok in enumerate(drawable) if ok]
        renum = {old: new for new, old in enumerate(keep)}
        holders = [sc] + [o.root for o in self.tags if o.group == 'sbsp' and o.root is not None]
        dropped_groups = dropped_decals = 0
        for h in holders:
            for f in h.fields:
                if f.name != 'Decorators' or f.type not in T.BLOCK_TYPES:
                    continue
                for de in h.blocks.get(f.offset) or []:
                    fg, fc, fd = de.field('Groups'), de.field('Cells'), de.field('Decals')
                    groups = de.blocks.get(fg.offset) or []
                    new_groups, newpos = [], {}
                    for gi, g in enumerate(groups):
                        s = struct.unpack_from('<b', g.data, 0)[0]
                        if s in renum:
                            struct.pack_into('<b', g.data, 0, renum[s])
                            newpos[gi] = len(new_groups)
                            new_groups.append(g)
                        else:
                            dropped_groups += 1
                    if len(new_groups) != len(groups):
                        for c in de.blocks.get(fc.offset) or []:
                            cnt, start = struct.unpack_from('<h', c.data, 0x12)[0], struct.unpack_from('<i', c.data, 0x14)[0]
                            kept = [newpos[i] for i in range(start, start + max(cnt, 0)) if i in newpos]
                            struct.pack_into('<h', c.data, 0x12, len(kept))
                            struct.pack_into('<i', c.data, 0x14, kept[0] if kept else 0)
                        de.blocks[fg.offset] = new_groups
                    decals = de.blocks.get(fd.offset) or []
                    new_decals = []
                    for dc in decals:
                        s = struct.unpack_from('<b', dc.data, 0)[0]
                        if s in renum:
                            struct.pack_into('<b', dc.data, 0, renum[s])
                            new_decals.append(dc)
                        else:
                            dropped_decals += 1
                    de.blocks[fd.offset] = new_decals
        if len(keep) != len(pal):
            sc.blocks[fp.offset] = [pal[i] for i in keep]
        if dropped_groups or dropped_decals or len(keep) != len(pal):
            self.stats['decorator_sets_dropped'] += len(pal) - len(keep)
            self.warn('decorators_cleaned', '%d decorator set(s) without geometry dropped from the palette, %d group(s) / %d decal(s) of '
                      'empty or missing sets removed (they draw nothing on Xbox)' % (len(pal) - len(keep), dropped_groups, dropped_decals))

    def finish_dead_decorator_blocks(self):
        """Decorator Cache Blocks whose geometry could not be converted (aerial: 118 of 132 are not geometry blocks in the source, no
        'fklb' trailer).  geometry() zeroes their offset / size but the section data size, resources and the cells pointing at them
        stayed, so Vista loaded 'something' for each visible cell and freed a garbage runtime pointer from it when the map unloaded
        (halo2.exe 0x78202E via the pc geometry cache, quitting aerial to the menu, 2026-10-26).  A dead block becomes fully empty, every
        cell on it gets Cache Block Index -1 (the stock 'no cache block' value: 27 of deltatap's 69 cells) and the groups drawn from it
        - nothing valid to draw on any platform - are removed (each cell's group range rebuilt)."""
        holders = [o.root for o in self.tags if o.group in ('sbsp', 'scnr') and o.root is not None]
        blocks = cells_cleared = groups_dropped = 0
        for h in holders:
            for f in h.fields:
                if f.name != 'Decorators' or f.type not in T.BLOCK_TYPES:
                    continue
                for de in h.blocks.get(f.offset) or []:
                    fb, fg, fc = de.field('Cache Blocks'), de.field('Groups'), de.field('Cells')
                    cbs = de.blocks.get(fb.offset) or []
                    dead = set()
                    for i, cb in enumerate(cbs):
                        if cb.u32(4) == 0 and cb.u32(0) in (0, NULL):         # Resource Block Size 0: no geometry at all
                            dead.add(i)
                            cb.set_u32(8, 0)                                  # Section / Resource Data Size
                            cb.set_u32(12, 0)
                            for ff in cb.fields:
                                if ff.type in T.BLOCK_TYPES:
                                    cb.blocks[ff.offset] = []
                    if not dead:
                        continue
                    blocks += len(dead)
                    groups = de.blocks.get(fg.offset) or []
                    new_groups, newpos = [], {}
                    for gi, g in enumerate(groups):
                        if g.i16(6) in dead:                                  # Cache Block Index
                            groups_dropped += 1
                            continue
                        newpos[gi] = len(new_groups)
                        new_groups.append(g)
                    for c in de.blocks.get(fc.offset) or []:
                        if c.i16(0x10) in dead:
                            c.set_u16(0x10, 0xFFFF)
                            cells_cleared += 1
                        if len(new_groups) != len(groups):
                            cnt, start = c.i16(0x12), struct.unpack_from('<i', c.data, 0x14)[0]
                            kept = [newpos[i] for i in range(start, start + max(cnt, 0)) if i in newpos]
                            struct.pack_into('<h', c.data, 0x12, len(kept))
                            struct.pack_into('<i', c.data, 0x14, kept[0] if kept else 0)
                    de.blocks[fg.offset] = new_groups
        if blocks:
            self.warn('decorator_blocks_dead', '%d decorator cache block(s) without convertible geometry emptied: %d cell(s) set to no cache '
                      'block, %d group(s) drawn from them removed' % (blocks, cells_cleared, groups_dropped))

    def finish_scenario(self):
        """After every reference is final: scnr Simulation Definition Table sorted ascending as int32 (the engine binary-searches it,
        sub_5F43BB) plus the shared placeholder 'twin' of every local entry that shadows a shared tag of the same (group, name);
        respawn-zone Relevant Games masked to the 8 Vista game bits."""
        root = self.tags[0].root if self.tags and self.tags[0].group == 'scnr' else None
        if root is None:
            return
        f = root.field('Simulation Definition Table')
        els = root.blocks.get(f.offset) or []
        have = [e.u32(0) for e in els if e.u32(0) not in (0, NULL)]
        extra = []
        for d in have:
            i = d & 0xFFFF
            if i < len(self.tags) and self.tags[i].datum == d and self.tags[i].name:
                vt = self.vs_name.get((self.tags[i].group, self.tags[i].name.lower()))
                if vt is not None:
                    extra.append(vt.datum)
        tab = sorted(set(have) | set(extra), key=lambda v: struct.unpack('<i', struct.pack('<I', v))[0])
        es = T.element_size(f)
        path = T.child_path(root.path, f)
        root.blocks[f.offset] = [T.Node('scnr', path, f.children, es, struct.pack('<I', d) + b'\0' * (es - 4)) for d in tab]
        self.stats['sim_table'] = len(tab)
        self.stats['sim_table_twins'] = len(set(extra) - set(have))
        sd = root.block('Spawn Data')
        for zname in ('Static Respawn Zones', 'Static Initial Spawn Zones'):
            for z in (sd[0].block(zname) if sd else []):
                fz = z.field('Relevant Games')
                if fz is not None and z.u32(fz.offset) & 0xFFFFFF00:
                    z.set_u32(fz.offset, z.u32(fz.offset) & 0xFF)
                    self.stats['zone_games_masked'] += 1
                for f in z.fields:                       # a mod's broken zone can carry NaN coordinates (bulldog: initial zone 7)
                    if f.type.lower() in ('float32', 'point3', 'vector3', 'point2', 'rangef', 'degree'):
                        for o in range(f.offset, f.offset + f.size, 4):
                            v = struct.unpack_from('<f', z.data, o)[0]
                            if v != v or v in (float('inf'), float('-inf')):
                                struct.pack_into('<f', z.data, o, 0.0)
                                self.stats['zone_nan_zeroed'] += 1

    def merge_with_vista(self, o):
        """Three-way merge for engine-level tags (matg): the Vista shared.map tag is the base; every TOP-LEVEL field where the Xbox map
        differs from the pack's Xbox shared.map copy (the mod's change) is taken from the converted Xbox tag.  Keeps Vista's engine data
        (rasterizer vertex shaders, language packs, sound globals ...) that an Xbox tag cannot provide.  None when no base exists."""
        t = o.src
        vt = self.vs_name.get((t.group, (t.name or '').lower()))
        pt = None
        if self.ps is not None:
            pt = next((x for x in self.ps.tags if x.addr and x.group == t.group and (x.name or '').lower() == (t.name or '').lower()), None)
        if vt is None or pt is None:
            return None
        lay_x, lay_v = self.lx[t.group], self.lv[t.group]
        xr = T.decode_tag(self.xc.read, lay_x, t.addr, t.group)
        pr = T.decode_tag(self.ps.read, lay_x, pt.addr, t.group)
        base = T.decode_tag(self.vsh.read_addr, lay_v, vt.addr, t.group)
        self.remap_donor(base, self.vsh)
        conv = xr if lay_x is lay_v else relayout(xr, lay_v.fields, lay_v.base_size or lay_v.computed_size)
        keep = MERGE_KEEP_VISTA.get(t.group, ())
        taken = []
        for f in lay_x.fields:
            vf = next((g for g in lay_v.fields if g.offset == f.offset and g.name == f.name), None)
            if vf is None or any(lo <= f.offset < hi for lo, hi in keep):
                continue
            if _field_same(xr, pr, f, self.xc, self.ps):
                continue
            if f.type in T.BLOCK_TYPES:
                kids = conv.blocks.get(vf.offset, [])
                for k in kids:
                    self.remap_xbox(k)
                base.blocks[vf.offset] = kids
            elif f.type in T.DATA_TYPES:
                base.datas[vf.offset] = conv.datas.get(vf.offset, b'')
            else:
                base.data[vf.offset:vf.offset + vf.size] = conv.data[vf.offset:vf.offset + vf.size]
                tl = vf.type.lower()
                if tl == 'tagref':
                    off = vf.offset + (4 if vf.size == 8 else 0)
                    struct.pack_into('<I', base.data, off, self.resolve_x(struct.unpack_from('<I', base.data, off)[0]))
                elif tl == 'stringid':
                    struct.pack_into('<I', base.data, vf.offset, self.sid(struct.unpack_from('<I', base.data, vf.offset)[0]))
            taken.append(f.name)
        if self.prefer_own_tags(base):
            taken.append('own tags of %d reference(s)' % self.stats['merge_own_tag_refs'])
        self.stats['merged:%s' % t.group] += 1
        self.warn('merged_with_vista', '%s %s: Vista shared.map base + the mod\'s %s' % (t.group, t.name, ', '.join(taken) or 'nothing'))
        return base

    def prefer_own_tags(self, root):
        """Every reference of a merge base (Vista shared.map tag, remap_donor'd) to a shared.map tag whose name this map CONVERTS goes to the
        map's own tag, as the Xbox tag's datum did.  The mod's field may equal the pack's (so merge_with_vista keeps the base) while the
        tag it names was rebuilt in place: Tortuga's matg Grenades named frag_grenade / plasma_grenade, unchanged, but both grenades are
        the mod's own - Vista threw Bungie's (2026-11-02).  Returns the number of references redirected."""
        if getattr(self, '_x_by_name', None) is None:
            self._x_by_name = {}
            for xt in self.xc.tags:
                if not xt.is_empty and xt.name:
                    self._x_by_name.setdefault((xt.group, xt.name.lower()), xt)
        n = 0
        for nd in root.iter_nodes():
            for f in nd.fields:
                if f.type.lower() != 'tagref':
                    continue
                off = f.offset + (4 if f.size == 8 else 0)
                d = nd.u32(off)
                if d in (0, NULL):
                    continue
                i = d & 0xFFFF
                vt = self.vsh.tags[i] if i < len(self.vsh.tags) else None
                if vt is None or vt.datum != d or not vt.name:
                    continue
                if vt.group in OWN_TAG_SKIP:
                    continue
                xt = self._x_by_name.get((vt.group, vt.name.lower()))
                pe = self.plan.entries.get(xt.index) if xt is not None else None
                if pe is None or pe.action != 'convert' or self.xc.tag_by_datum(xt.datum) is not xt:
                    continue
                r = self.resolve_x(xt.datum)
                if r not in (NULL, d):
                    nd.set_u32(off, r)
                    n += 1
        self.stats['merge_own_tag_refs'] += n
        return n

    def group_family(self, gb):
        """{stored-form group, parent, grandparent} of a stored-form (reversed) group fourcc, from Vista shared.map's group links."""
        fam = getattr(self, '_fam', None)
        if fam is None:
            fam = self._fam = {}
            for gl in self.vsh.groups:
                key = gl.group.encode('latin-1')[::-1]
                fam[key] = {key} | {x.encode('latin-1')[::-1] for x in (gl.parent, gl.grandparent) if x}
        return fam.get(gb, {gb})

    def group_of_out(self, d):
        """Group fourcc (as stored, reversed) of an output datum: local tag or shared placeholder."""
        if d in (0, NULL):
            return None
        i = d & 0xFFFF
        if i < len(self.tags) and self.tags[i].datum == d:
            g = self.tags[i].group
        elif i < len(self.vsh.tags) and self.vsh.tags[i].datum == d:
            g = self.vsh.tags[i].group
        else:
            return None
        return g.encode('latin-1')[::-1]

    def ref_group_allowed(self, n, f, xd):
        """False when the reference's target (Xbox datum xd, as resolve_x will build it) is of a group the field never holds in stock data
        (load_tagref_groups; 2026-10-26): waterworld's sbsp Background Sound Palette 'Background Sound'
        -> the sbsp itself, Satans_Crypt's hlmt warthog Instant Responses 'Transition Effect' -> itmc battle_rifle and wraith 'Looping
        Effect' -> vehc SoulRyd, a vehicle 'Weapon' -> a weapon entry whose data is an itmc / jpt! (find_data_twins).  Vista's tag_get
        0x4316C3 returns NULL for such a target (its group family lacks the requested group); the Xbox, which reads by index only, read
        the other group's bytes as the field's.  Object targets in an object or item / vehicle collection field stay (the engine builds
        what the definition's Object Type says: objtype.py; Outflod's netgame equipment names 12 eqip directly), as do targets whose
        group family meets the field's and effe / snd! / lsnd in an effect-or-sound field (TAGREF_EFFECT_GROUPS); owner back-pointers,
        prediction hints and the simulation table are not checked (TAGREF_UNCHECKED_*) unless the target is an object entry whose data
        is a non-object (Biohazard's ghost_gun leaves the simulation table instead of becoming an itmc row there).  A field the table
        does not know is allowed
        (today's fourcc correction), except a non-object twin of an object entry.  Not group-checked: the stored fourcc, which the Xbox
        never read (bulldog's mode chaingun Materials 'Shader' stores 'wigl' for a shad: allowed, its fourcc is corrected).
        Decided before resolve_x, so a refused target is not built: the output group of an Xbox entry is its own (a link / donor is the
        Vista tag of that group), its twin's, or its reindex_as group."""
        xt = self.xc.tag_by_datum(xd) if xd not in (0, NULL) else None
        if xt is None:
            return True
        alias = xt.group in OBJECT_TYPE_GROUPS and xt.index in self.data_twin
        unchecked = f.name in TAGREF_UNCHECKED_FIELDS or any(p in n.path for p in TAGREF_UNCHECKED_BLOCKS)
        if unchecked and not alias:
            return True
        u = self.xc.tags[self.data_twin[xt.index]] if xt.index in self.data_twin and xt.group != 'shad' else xt
        gt = self.reindex_as.get(u.index, u.group)
        # an object entry whose data is a non-object: never an entity in the simulation table (Biohazard's ghost_gun = itmc)
        allowed = None if unchecked else self.tagref_groups.get('%s|%s|%s' % (n.group, n.path, f.name))
        if allowed is None:
            ok = not alias or gt in OBJECT_TYPE_GROUPS
        else:
            ok = gt in allowed or bool(self.family(gt) & allowed) or (
                gt in OBJECT_TYPE_GROUPS and any(a in OBJECT_TYPE_GROUPS or a in ('itmc', 'vehc') for a in allowed)) or (
                gt in TAGREF_EFFECT_GROUPS and bool(allowed & TAGREF_EFFECT_GROUPS))
        if not ok:
            code = 'weapon_alias_disarmed' if alias else 'tagref_group_incompatible'
            self.stats['tagref_none:%s' % gt] += 1
            self.warn(code, '%s|%s/%s -> %s %s: %s, set to NONE' % (
                n.group, n.path, f.name, xt.group, xt.name, 'the entry\'s data is %s %s' % (u.group, u.name) if alias
                else 'the field holds %s in stock data' % '/'.join(sorted(allowed or ()))))
        return ok

    def zero_group_ref_allowed(self, n, f, xd):
        """An 8-byte reference whose stored group word is 0 (never a group fourcc) but whose datum is not NONE: kept only when the field's
        learned allowed-group table (load_tagref_groups) holds the group the target would be built as.  Blockfort's scen
        high_shipping_crate is garb data (Object Type 4) whose garb 'Unknown' reference stores 00000000 + a junk datum that lands on bitm
        rasterizer\\linear_corner_fade: remap_xbox rewrote the fourcc to 'bitm' and the Vista -> MCC writer's verify refused the map
        (tagref_group; w3, 2026-10-27).  No stock data stores a group-0 reference (build_tagref_groups leaves 0x00000000 out), so the
        word says the reference is unset."""
        xt = self.xc.tag_by_datum(xd) if xd not in (0, NULL) else None
        if xt is None:
            return True                          # no target: resolve_x gives NONE anyway (Qbert hlmt Shield Depleted Effect 00000000/00000001)
        allowed = self.tagref_groups.get('%s|%s|%s' % (n.group, n.path, f.name))
        if not allowed:
            return False
        u = self.xc.tags[self.data_twin[xt.index]] if xt.index in self.data_twin and xt.group != 'shad' else xt
        gt = self.reindex_as.get(u.index, u.group)
        return gt in allowed or bool(self.family(gt) & allowed)

    def remap_xbox(self, root):
        for n in root.iter_nodes():
            for f in n.fields:
                tl = f.type.lower()
                if tl == 'tagref':
                    o = f.offset + (4 if f.size == 8 else 0)
                    xd = struct.unpack_from('<I', n.data, o)[0]
                    if f.size == 8 and n.data[f.offset:f.offset + 4] == b'\0\0\0\0' and not self.zero_group_ref_allowed(n, f, xd):
                        self.stats['tagref_group_zero_none'] += 1
                        self.warn('tagref_group_zero', '%s|%s/%s: stored group 00000000 with datum 0x%08X - set to NONE' % (
                            n.group, n.path, f.name, xd))
                        nd = NULL
                        n.data[f.offset:f.offset + 4] = b'\xff\xff\xff\xff'
                    elif self.ref_group_allowed(n, f, xd):
                        nd = self.resolve_x(xd, alias_ok=True)
                    else:
                        nd = NULL
                        if f.size == 8:
                            n.data[f.offset:f.offset + 4] = b'\xff\xff\xff\xff'
                    struct.pack_into('<I', n.data, o, nd)
                    if f.size == 8:
                        # the stored group must be the target's group or one of its parents (some Xbox mod tags carry junk there, e.g.
                        # 'wigl' on a mode material shader): anything else is replaced by the target's group
                        gb = self.group_of_out(nd)
                        cur = bytes(n.data[f.offset:f.offset + 4])
                        if gb is not None and cur != b'\xff\xff\xff\xff' and cur not in self.group_family(gb):
                            n.data[f.offset:f.offset + 4] = gb
                            self.stats['tagref_group_fixed'] += 1
                elif tl == 'stringid':
                    struct.pack_into('<I', n.data, f.offset, self.sid(struct.unpack_from('<I', n.data, f.offset)[0]))

    def remap_donor(self, root, cache):
        if cache is self.vsh:                     # Vista shared.map itself: its references ARE the placeholders; only string ids move
            for n in root.iter_nodes():
                for f in n.fields:
                    if f.type.lower() == 'stringid':
                        v = struct.unpack_from('<I', n.data, f.offset)[0]
                        if v:
                            text = cache.string_by_index(v & 0xFFFFFF)
                            struct.pack_into('<I', n.data, f.offset, self.strs.sid(self.out_sid_text(text)) if text else 0)
            return
        for n in root.iter_nodes():
            for f in n.fields:
                tl = f.type.lower()
                if tl == 'tagref':
                    o = f.offset + (4 if f.size == 8 else 0)
                    struct.pack_into('<I', n.data, o, self.resolve_d(cache, struct.unpack_from('<I', n.data, o)[0]))
                elif tl == 'stringid':
                    v = struct.unpack_from('<I', n.data, f.offset)[0]
                    if v:
                        idx = v & 0xFFFFFF
                        text = cache.string_by_index(idx) if idx < cache.header.string_table_count else None
                        struct.pack_into('<I', n.data, f.offset, self.strs.sid(self.out_sid_text(text)) if text else 0)
            if n.group == 'bitm' and n.path == '/Bitmaps':
                p, sz = n.u32(0x1C), n.u32(0x34)
                if p >> 30 == 0 and sz and p not in (0, NULL):
                    self.add_raw_patch(n, 0x1C, 'bitmap', cache.raw(p & 0x3FFFFFFF, sz))
                elif p >> 30 == 3 and sz and p != NULL:
                    # a CAMPAIGN donor (same-name shader port) keeps its pixels in single_player_shared.map, which a multiplayer map
                    # never loads (validate raw.mp_sp_shared; Funbox grav_beam clouds_a/b, the tower_pool water bitmaps, 2026-10-26):
                    # copy them into this map like location-0 pixels
                    sp = self.donor_cache(os.path.join(self.vm, 'single_player_shared.map'))
                    self.add_raw_patch(n, 0x1C, 'bitmap', sp.raw(p & 0x3FFFFFFF, sz))
                    self.stats['donor_sp_shared_pixels_localised'] += 1

    # ---- raw data ------------------------------------------------------------------------------------------------
    def add_raw_patch(self, node, off, kind, data):
        self.raws.append((kind, bytes(data)))
        node.patches[off] = ('raw', len(self.raws) - 1)

    def convert_raw(self, o, root):
        g = o.group
        if g in GEOMETRY_GROUPS:
            self.geometry(o, root)
        if g == 'ugh!':
            self._gestalt_root = root          # finished after every local snd! is known (finish_gestalt)
            self._gestalt_datum = o.datum

    def bitmap(self, o, xroot, vroot):
        """Xbox bitm -> Vista (h2conv.xbox.bitmaps_xbox: unswizzle, unpad, cube face order, DXT mip trim, P8 / P8-bump / AY8 formats).
        Root: the first 0x4C bytes, flags +0x06 |= 0x20, the 4 compression bytes 0F 0F 01 03 (3,260 of 3,275 same-name stock tags).
        Elements: vista_element_bytes with the +0x4C parent datum left as the XBOX datum (remap_xbox turns it into the output datum)."""
        from . import bitmaps_xbox as BX
        if getattr(self, '_palettes', None) is None:
            self._palettes = BX.LightmapPalettes(self.xc)
        conv = BX.convert_bitmap_tag(self.xc, xroot, o.src.datum, self._palettes)
        vroot.data[0:0x4C] = xroot.data[0:0x4C]
        vroot.set_u16(6, vroot.u16(6) | 0x20)
        vroot.data[0x4C:0x50] = b'\x0f\x0f\x01\x03'
        xel = xroot.blocks.get(0x44, [])
        vel = vroot.blocks.get(0x44, [])
        scaled = None
        if conv and o.src.datum in self.hud_widget_bitmaps() and not self.opts.get('no_hud_upscale'):
            scaled = self.hud_upscale(o, xel, conv)
            if scaled is not None:
                conv = scaled
                s = HUD_SCALE
                cw, ch = struct.unpack_from('<hh', vroot.data, 0x18)                  # colour plate size (tool side)
                struct.pack_into('<hh', vroot.data, 0x18, cw * s, ch * s)
                sf = vroot.field('Sequences')
                for seq in vroot.blocks.get(sf.offset if sf is not None else 0x3C, []):   # sprite registration points are pixels
                    for kids in seq.blocks.values():
                        for sp in kids:
                            if len(sp.data) >= 0x20:
                                rx, ry = struct.unpack_from('<ff', sp.data, 0x18)
                                struct.pack_into('<ff', sp.data, 0x18, rx * s, ry * s)
        for i, (f, px) in enumerate(conv):
            blob = zlib.compress(px, 9)
            vel[i].data[:] = BX.vista_element_bytes(xel[i], o.src.datum, 0, len(blob), f['pixels_offset'], fmt=f['format'])
            if scaled is not None:
                w, h = struct.unpack_from('<hh', vel[i].data, 4)
                rx, ry = struct.unpack_from('<hh', vel[i].data, 0x10)
                struct.pack_into('<hh', vel[i].data, 4, w * HUD_SCALE, h * HUD_SCALE)
                struct.pack_into('<hh', vel[i].data, 0x10, rx * HUD_SCALE, ry * HUD_SCALE)
            self.add_raw_patch(vel[i], 0x1C, 'bitmap', blob)
            self.stats['bitmap_elements'] += 1
            if f.get('palette_missing'):
                self.stats['bitmap_p8_without_palette'] += 1

    def mode_section_compounds(self, o, root):
        """mode: id(Sections element) -> (len(Nodes), Compound Nodes of the Section Group the section is drawn with) for
        geometry_xbox.expand_compound_nodes.  At LOD l a permutation draws section L1..L6 Section Index (+0x4 + 2 l) with Section Group
        root +0x3C + l (int8); the palette builder appends THAT group's Compound Nodes after the model nodes (halo2.exe sub_77DEBD).
        Node bytes are unsigned (the engine's movzx / cmp 0FFh at 0x77E007).  A section no permutation draws takes the compound list
        when only one exists.  A section that could be drawn with different compound lists gets None (its compound references are left
        as they are and reported, see expand_compound_nodes).  {} when no group has compound nodes (in the 230 Xbox sources only
        fp_body, fp_beam_rifle and a few hunter cannon / beam rifle / fp_krieg variants have any)."""
        try:
            secs, groups = root.block('Sections'), root.block('Section Groups')
            lists = [tuple((tuple(struct.unpack_from('<4B', c.data, 0)), (c.f32(4), c.f32(8), c.f32(12)))
                           for c in g.block('Compound Nodes')) for g in groups]
            if not any(lists):
                return {}
            drawn = collections.defaultdict(set)              # section index -> compound lists it is drawn with
            seen = set()                                      # sections some permutation draws (with or without compound nodes)
            for r in root.block('Regions'):
                for p in r.block('Permutations'):
                    for lod in range(6):
                        s, g = p.i16(4 + 2 * lod), struct.unpack_from('<b', root.data, 0x3C + lod)[0]
                        if 0 <= s < len(secs) and 0 <= g < len(lists):
                            seen.add(s)
                            if lists[g]:
                                drawn[s].add(lists[g])
            nn = len(root.block('Nodes'))
        except Exception as ex:                                           # noqa: BLE001
            self.warn('geometry', '%s: compound nodes unreadable, left as they are: %r' % (o.name, ex))
            return {}
        only = set(l for l in lists if l)
        out = {}
        for i, sec in enumerate(secs):
            if i in seen and not drawn.get(i):
                out[id(sec)] = (nn, [])                       # drawn only with groups that have no compound nodes: nothing to expand
                continue
            cand = drawn.get(i) or only
            out[id(sec)] = (nn, list(next(iter(cand))) if len(cand) == 1 else None)
        return out

    def geometry(self, o, root):
        """Every geometry_block_info of the tag (a 'Block Offset' / 'Resource Block Offset' word followed by size, section data size,
        resource data size, the Resources fixup block at +0x10 and the owner datum at +0x18): the Xbox block is converted by
        h2conv.xbox.geometry_xbox (vertex buffers to Vista floats, water quads to triangles, fixups re-packed) and placed in the output's
        geometry region.  The owner datum is set after the reference remap (build_node)."""
        from . import geometry_xbox as GX
        if o.group == 'DECR':
            root.data[0x6C:0x70] = b'\xff\xff\xff\xff'               # Vista-only root dword: ffffffff in stock DECR
        sec_comp = self.mode_section_compounds(o, root) if o.group == 'mode' else {}
        for n in root.iter_nodes():
            for f in n.fields:
                if f.name not in ('Block Offset', 'Resource Block Offset') or f.size != 4:
                    continue
                fo = f.offset
                ptr, bsize, sds = n.u32(fo), n.u32(fo + 4), n.u32(fo + 8)
                if ptr in (0, NULL) or not bsize:
                    continue
                fr = next((x for x in n.fields if x.offset == fo + 0x10 and x.type in T.BLOCK_TYPES), None)
                res_nodes = n.blocks.get(fr.offset, []) if fr is not None else []
                label = n.path.split('/')[-1] or o.group
                try:
                    blk = self.xc.raw(ptr, bsize)
                    vblk, vres, vsds, vrds = GX.convert_block(blk, res_nodes, sds, o.group, label,
                                                               warn=lambda m, _n=o.name: self.warn('geometry', '%s: %s' % (_n, m)),
                                                               compound=sec_comp.get(id(n)), stats=self.stats)
                except Exception as ex:                                   # noqa: BLE001
                    self.warn('geometry_failed', '%s %s %s: %r' % (o.group, o.name, label, ex))
                    # FULLY empty (offset, size, section / resource data size, resources): a half-empty info still made Vista load
                    # 'something' and free a garbage pointer from it on map unload (aerial decorator cache blocks, 2026-10-26)
                    for k in (0, 4, 8, 12):
                        n.set_u32(fo + k, 0)
                    if fr is not None:
                        n.blocks[fr.offset] = []
                    continue
                GX.apply_to_nodes(res_nodes, vres)
                n.set_u32(fo + 4, len(vblk))
                n.set_u32(fo + 8, vsds)
                n.set_u32(fo + 12, vrds)
                if (o.group, n.path) in GEOMETRY_FLAG_0800 and fo + 0x20 <= len(n.data):
                    # stock Vista sets bit 0x800 (0x0900) on the sites whose section struct lives in a block of the OWNING tag element
                    # (owner section offset -8 / +36); Xbox has 0x0100 everywhere.  Without it the water renderer walked an empty
                    # 'Section' block and crashed at map load (halo2.exe 0x77D764 via sub_5A0673, bulldog 2026-10-21).
                    n.set_u16(fo + 0x1E, n.u16(fo + 0x1E) | 0x0800)
                    self.stats['geometry_flag_0800'] += 1
                self.add_raw_patch(n, fo, 'geometry', vblk)
                self._owner_fix.append((n, fo + 0x18))
                self.stats['geometry_blocks'] += 1

    # ---- radiance transfer (PRT) of render models: h2conv.prt_gen ------------------------------------------------------------------
    def raw_of(self, node, off):
        """The converted raw geometry block stored at a geometry_block_info of an output node, or None."""
        tok = node.patches.get(off)
        return self.raws[tok[1]][1] if tok is not None and tok[0] == 'raw' and self.raws[tok[1]][0] != 'skipped' else None

    def put_block(self, node, off, data):
        """Store a geometry block at a geometry_block_info of an output node; the block it replaces is not written."""
        tok = node.patches.get(off)
        if tok is not None and tok[0] == 'raw':
            self.raws[tok[1]] = ('skipped', b'')
        self.add_raw_patch(node, off, 'geometry', data)

    def campaign_prt_models(self):
        """[(cache, tag)] of the render models with PRT Info in the stock Vista campaign maps (Bungie's data for the campaign objects)."""
        m = self.__dict__.get('_campaign_prt')
        if m is None:
            m = self._campaign_prt = []
            for f in sorted(glob.glob(os.path.join(self.vm, '*.map'))):
                if os.path.basename(f).lower() in ('shared.map', 'mainmenu.map'):
                    continue
                try:
                    c = self.donor_cache(f)
                except Exception:                                        # noqa: BLE001
                    continue
                if c.header.type in (1, 2):                              # multiplayer / main menu maps have no campaign objects
                    continue
                for vt in c.tags:
                    if vt.group == 'mode' and vt.addr and vt.name:
                        try:
                            if struct.unpack('<i', c.read_addr(vt.addr + PG.PRT_INFO_BLOCK, 4))[0] > 0:
                                m.append((c, vt))
                        except Exception:                                # noqa: BLE001
                            continue
        return m

    @staticmethod
    def vista_raw_of(cache):
        def raw_of(n, off):
            ptr, size = n.u32(off), n.u32(off + 4)
            if not size or ptr in (0, NULL) or ptr >> 30:                # 0 = none; the top bits name another database
                return None
            return cache.raw(ptr & 0x3FFFFFFF, size)
        return raw_of

    def stock_prt_model(self, o, root, secs):
        """Bungie's copy of this render model among the stock Vista campaign models, proven by its geometry (never by its name): the
        (verdict, stock root, stock sections, cache) of the best match, else None.  An 'exact' match - every section readable and equal -
        wins over a 'struct' one (sections of this model unreadable; same name, same checksum, same counts and bounds)."""
        best = None
        memo = self.__dict__.setdefault('_stock_prt_cache', {})
        name = (o.name or '').lower()
        for c, vt in sorted(self.campaign_prt_models(), key=lambda ct: (ct[1].name or '').lower() != name):
            key = (c.path, vt.index)
            ent = memo.get(key)
            if ent is None:
                try:
                    sroot = T.decode_tag(c.read_addr, self.lv['mode'], vt.addr, 'mode')
                    ssecs = PG.read_sections(sroot, self.vista_raw_of(c))
                except Exception:                                        # noqa: BLE001
                    sroot = ssecs = None
                ent = memo[key] = (sroot, ssecs)
            sroot, ssecs = ent
            if sroot is None or len(ssecs) != len(secs):
                continue
            v = PG.compare_sections(secs, ssecs)
            if v.kind == 'exact':
                return v, sroot, ssecs, c, vt
            if v.kind == 'struct' and best is None and (vt.name or '').lower() == name and sroot.i32(8) == root.i32(8):
                best = (v, sroot, ssecs, c, vt)
        return best

    def repair_prt(self, o, root):
        """A render model whose PRT Info is damaged (header zeroed, or the stream block destroyed): Bungie's PRT Info when the model IS a
        stock campaign model, else a neutral PRT generated from its vertex normals (prt_gen).  Option prt_repair:
          faithful (default)  a damaged model keeps the Xbox's look: its lighting is black.  Header zeroed: the header is restored (so the
                              game uploads the constants of the model's own clusters instead of reading stale registers) and the Cluster Basis
                              is all zero, so the vertex shader gives black every frame.  Stream destroyed (noise on the Xbox, nothing to
                              be faithful to): a valid structure (Bungie's, else generated) with the same all-zero Cluster Basis = black.
                              Sections destroyed beyond the PRT block are restored from the stock model either way.
          repaired            the above, but Bungie's / the generated transfer is kept: the model is lit by the map's SH light (green
                              where the lightmap has no Lighting Environments, see h2-object-lightmap-sampling)
          neutral             never take Bungie's data (generate)
        There is NO 'off': a damaged PRT Info left as the mod tool wrote it is a hazard in game (header zeroed: the vertex shader reads
        stale constant registers; stream destroyed: the model is marked PRT but has no vertex stream block), so 'off' (and anything
        unknown) means faithful."""
        mode = self.opts.get('prt_repair') or 'faithful'
        if mode not in ('faithful', 'repaired', 'neutral'):
            mode = 'faithful'
        if not root.block('PRT Info'):
            return
        self.stats['prt_models'] += 1
        secs = PG.read_sections(root, self.raw_of)
        rep = PG.check_prt(root, self.raw_of, secs)
        if rep.status == PG.OK:
            self.stats['prt_valid'] += 1
            return
        self.stats['prt_damaged'] += 1
        p = root.block('PRT Info')[0]
        why = '; '.join(rep.problems[:2])[:150]
        black = mode == 'faithful'                                       # zeroed header OR destroyed stream: both end black (docstring)
        hit = self.stock_prt_model(o, root, secs) if mode in ('repaired', 'faithful') else None
        if hit is not None:
            v, sroot, ssecs, c, vt = hit
            sp = sroot.block('PRT Info')[0]
            sblk = self.vista_raw_of(c)(sp, PG.PRT_GEOMETRY_INFO)
            if sblk is not None:
                PG.adopt_prt(p, sp, sblk, self.put_block)
                self._owner_fix.append((p, PG.PRT_GEOMETRY_INFO + 0x18))
                nodes, snodes = root.block('Sections'), sroot.block('Sections')
                for i in v.unreadable:
                    blk = self.vista_raw_of(c)(snodes[i], PG.SECTION_GEOMETRY_INFO)
                    if blk is not None:
                        PG.adopt_section(nodes[i], snodes[i], blk, self.put_block)
                        self._owner_fix.append((nodes[i], PG.SECTION_GEOMETRY_INFO + 0x18))
                self.stats['prt_from_stock'] += 1
                if black:
                    self.prt_black(o, p)
                if v.unreadable:
                    self.stats['prt_from_stock_geometry'] += 1
                self.warn('prt_from_stock', '%s: PRT Info damaged (%s) - Bungie\'s PRT Info of the stock model %s in %s%s' % (
                    o.name, why, vt.name, os.path.basename(c.path),
                    '; its %d unreadable section(s) restored from it as well' % len(v.unreadable) if v.unreadable else
                    ' (the geometry is the same: largest difference %.1e)' % (v.max_diff or 0.0)))
                return
        try:
            normals, counts, missing = PG.model_normals(secs)
            data = PG.neutral_prt(normals, PG.DEFAULT_CLUSTERS)
            PG.write_neutral(p, secs, data, self.put_block)
        except Exception as ex:                                          # noqa: BLE001
            self.stats['prt_unrepaired'] += 1
            self.warn('prt_unrepaired', '%s: PRT Info damaged (%s) and a neutral one could not be generated (%r)' % (o.name, why, ex))
            if self.opts.get('debug'):
                raise
            return
        self._owner_fix.append((p, PG.PRT_GEOMETRY_INFO + 0x18))
        self.stats['prt_generated'] += 1
        if black:
            self.prt_black(o, p)
        self.warn('prt_generated', '%s: PRT Info damaged (%s) and %s - neutral PRT generated from its %d vertex normals (%d clusters)%s' % (
            o.name, why, 'Bungie\'s data was not asked for' if mode == 'neutral' else 'the model matches no stock campaign model',
            data.vertices, data.clusters, '; %d section(s) without readable normals use a flat default' % missing if missing else ''))

    def ensure_device_mapping(self, o, root):
        """A structure bsp whose 'Portal=>Device Mapping' block is empty gets the one empty element every stock Vista bsp has (all 60+ stock
        bsps: exactly 1).  halo2.exe sub_49F554 (0x49F554, called from sub_49F635 for every bsp at map load) reads element 0 and its
        'Device Portal Associations' count WITHOUT checking the block count: an empty block has address 0, so it read the first dword of
        the whole tag data as a count and walked junk -> crash at load, halo2.exe+0x1318E1 (sub_5318E1 compare on a junk element
        pointer; wake 2026-10-29: ebx == dword_882290, the tag data base).  Modded bsps re-exported without the block (wake, aquatica)
        carry it empty."""
        try:
            if root.block('Portal=>Device Mapping'):
                return
            f = root.field('Portal=>Device Mapping')
            el = T.Node(root.group, T.child_path(root.path, f), f.children, T.element_size(f))
        except Exception as ex:                                           # noqa: BLE001
            self.warn('device_mapping', '%s: Portal=>Device Mapping could not be completed (%r)' % (o.name, ex))
            return
        root.blocks[f.offset] = [el]
        self.stats['device_mapping_added'] += 1
        self.warn('device_mapping_added', '%s: Portal=>Device Mapping was empty - one empty element added (the engine reads element 0 unguarded)' % o.name)

    def prt_black(self, o, p):
        """--prt-repair faithful: the model's lighting is black, as on the Xbox (prt_gen.zero_basis)."""
        PG.zero_basis(p)
        self.stats['prt_faithful_black'] += 1
        self.warn('prt_faithful_black', '%s: PRT header was zeroed - kept black as on the original Xbox (Cluster Basis zero, header and stream valid)' % o.name)

    def finish_gestalt(self):
        """The map's gestalt becomes the SECONDARY ugh! (stock Vista MP layout).  Only the LOCAL snd! tags index it (the linked ones play
        from shared.map's primary gestalt), so only the chunks their pitch ranges / permutations reach are copied; every other chunk record
        points at the first copied chunk with its size (never played).  The Extra Info lip-sync blocks are copied as they are."""
        root = getattr(self, '_gestalt_root', None)
        if root is None:
            return
        g = SX.Gestalt(self.xc)
        reach = set()
        for o in self.tags:
            if o.group != 'snd!' or o.kind != 'x':
                continue
            b = self.xc.read(o.src.addr, 0x14)
            pr, prc = struct.unpack_from('<h', b, 8)[0], b[0xA]
            for i in range(prc):
                if not 0 <= pr + i < len(g.pitch_ranges):
                    continue
                e = g.pitch_ranges[pr + i]
                for p in range(e.i16(8), e.i16(8) + max(e.i16(0xA), 0)):
                    if 0 <= p < len(g.perms):
                        pe = g.perms[p]
                        reach.update(range(pe.i16(0xC), pe.i16(0xC) + max(pe.i16(0xE), 0)))
        chunks = root.block('Permutation Chunks')
        seen = {}
        first = None
        for k, c in enumerate(chunks):
            if k not in reach:
                continue
            ptr, szw = c.u32(0), c.u32(4)
            key = (ptr, szw & 0x3FFFFFFF)
            if key in seen:
                c.patches[0], sw = seen[key]
                c.set_u32(4, sw)
                continue
            data = self.xc.raw(ptr, szw & 0x3FFFFFFF)
            if SX.is_asf(data):
                # Xbox WMA = a whole ASF file; Vista wants media type + WAVEFORMATEX + packet payloads (sound_xbox.xbox_wma_to_vista)
                try:
                    tr = SX._TRUNCATED[0]
                    data = SX.xbox_wma_to_vista(data)
                    if SX._TRUNCATED[0] != tr:
                        self.stats['sound_wma_truncated'] += 1
                        self.warn('sound_wma_truncated', 'gestalt chunk %d: the ASF declares more packets than the chunk holds - the '
                                  'whole packets inside it kept (what the Xbox plays)' % k)
                    szw = (szw & ~0x3FFFFFFF & 0xFFFFFFFF) | len(data)
                    c.set_u32(4, szw)
                    self.stats['sound_wma_reframed'] += 1
                except SX.WmaError as ex:
                    self.stats['sound_wma_unconverted'] += 1
                    self.warn('sound_wma_unconverted', 'gestalt chunk %d: Xbox WMA not converted (%s) - copied as is, Vista may not play it' % (k, ex))
            self.add_raw_patch(c, 0, 'sound', data)
            seen[key] = (c.patches[0], szw)
            if first is None:
                first = (c.patches[0], szw)
            self.stats['sound_chunks_copied'] += 1
        for k, c in enumerate(chunks):
            if k in reach:
                continue
            if first is None:
                c.set_u32(0, 0)
                c.set_u32(4, 0)
            else:
                c.patches[0] = first[0]
                c.set_u32(4, first[1])
            self.stats['sound_chunks_unused'] += 1
        # Sample Size = the bytes of its chunks (Vista: every stock permutation; Xbox stores the decoded size for some)
        for p in root.block('Permutations'):
            first, cnt = p.i16(0xC), p.i16(0xE)
            tot = sum(chunks[k].u32(4) & 0x3FFFFFFF for k in range(first, first + max(cnt, 0)) if 0 <= k < len(chunks))
            if p.u32(8) != tot:
                p.set_u32(8, tot)
                self.stats['sound_sample_size_fixed'] += 1
        for e in root.block('Extra Info'):
            f = e.field('Resource Block Offset')
            if f is None:
                for sub in e.iter_nodes():
                    f = sub.field('Resource Block Offset')
                    if f is not None:
                        e = sub
                        break
            if f is None:
                continue
            p, s = e.u32(f.offset), e.u32(f.offset + 4)
            if p not in (0, NULL) and s:
                self.add_raw_patch(e, f.offset, 'geometry', self.xc.raw(p, s))
                if f.offset + 0x1C <= len(e.data):
                    e.set_u32(f.offset + 0x18, self._gestalt_datum)   # geometry_block_info owner = the output ugh! (lip-sync maps: aerial)
                self.stats['gestalt_extra_info_blocks'] += 1

    def extend_gestalt(self):
        """Both past-end extensions of the map's gestalt (below), up to the indices of the snd! tags built so far; the number of elements
        appended (run() builds what they reference and calls again until nothing more is appended)."""
        root = getattr(self, '_gestalt_root', None)
        if root is None:
            return 0
        g = self.__dict__.get('_ext_gestalt') or self.__dict__.setdefault('_ext_gestalt', SX.Gestalt(self.xc))
        return self.extend_past_end_playbacks(root, g) + self.extend_past_end_custom_playbacks(root, g)

    def past_end_element(self, f, ptr, i, es, emptied=None):
        """Element i of the gestalt block field f (pointer ptr) read past the block's end as the Xbox read it, or None when its bytes lie
        past the file end (then not even zeros inside the declared tag region make it: salvage_xbox.TagSalvage.zero_fill)."""
        cp = T.child_path('', f)
        try:
            return T.decode_struct(self.xc.read, 'ugh!', cp, f.children, es, ptr + i * es, emptied=emptied)
        except Exception:                                                # noqa: BLE001
            sv = self.__dict__.get('_salvage') or self.__dict__.setdefault('_salvage', SV.TagSalvage(self.xc, self.lx, self.stock))
            blob = sv.zero_fill(ptr + i * es, es)
            if blob is None:
                return None
            try:
                return T.decode_struct(self.xc.read, 'ugh!', cp, f.children, es, ptr + i * es, raw=blob, emptied=emptied)
            except Exception:                                            # noqa: BLE001
                return None

    def extend_past_end_playbacks(self, root, g):
        """Injected sounds whose playback index is past the end of the map's Playback Parameters (aerial: the pelican and hunter cannon
        loops index 132..142 of 123) read the bytes after the block on Xbox (the Scales / Import Names data).  The block is extended with
        exactly those bytes, so the sounds behave as they did on Xbox and the index stays in range.  An element past the file end stops
        the extension (past_end_element; the sound keeps its out-of-range index, as before).  Returns the elements appended."""
        f = g.root.field('Playback Parameters')
        cnt, ptr = struct.unpack_from('<iI', g.root.data, f.offset)
        top = -1
        for o in self.tags:
            if o.group == 'snd!' and o.kind == 'x':
                top = max(top, struct.unpack_from('<h', self.xc.read(o.src.addr, 8), 6)[0])
        pb = root.block('Playback Parameters')
        if top < len(pb) or cnt <= 0:
            return 0
        es = T.element_size(f)
        n0 = len(pb)
        for i in range(n0, top + 1):
            e = self.past_end_element(f, ptr, i, es)
            if e is None:
                self.warn('sound_playback_past_eof', 'Playback Parameters element %d lies past the file end: not appended' % i)
                break
            pb.append(e)
        if len(pb) > n0:
            self.warn('sound_playback_past_end', '%d Playback Parameters element(s) appended: snd! indices up to %d past the %d in the block: the block is extended '
                      'with the bytes Xbox read there' % (len(pb) - n0, top, cnt))
        return len(pb) - n0

    def extend_past_end_custom_playbacks(self, root, g):
        """snd! Custom Playback indices (int8 at +0xD) past the end of the map's gestalt Custom Playbacks block: 80 sounds in 14 maps,
        campaign ambience injected with its campaign index (Invocation's dopplerwind_for_04a = 5 of 2, Fissure / Pelican /
        Desertedmetro loops 5..11 of 5 / 8; validate snd.index).  The Xbox reads them anyway: sub_18D170 / sub_18D090 (default.xbe,
        2026-10-26) return gestalt+0x34 pointer + 0x34 * index with no check
        against the count at +0x30, and sub_21D110 / sub_21DD30 / sub_222770 / sub_2226B0 then use its Flags (+8 bit 0), Filter,
        Pitch LFO, Filter LFO and Sound Effect blocks.  So the block is extended with exactly the elements the Xbox read there (as
        for the playback parameters above); a block in them whose pointer is NULL or outside the file (a float read as a pointer,
        0x45FA0000) is empty: the Xbox's null-checked readers (sub_21DD30, sub_222770, sub_2228D0, sub_222930) skip a NULL one, and
        a wild one is not data.  Never the campaign map's custom playback: the Xbox never read that.  An element whose own bytes lie past
        the file end (outside the declared tag region too) stops the extension instead of failing the whole map (past_end_element), and
        the references of the appended elements are built before the gestalt is finished (run: extend_gestalt / build_queue).  Returns the elements appended."""
        f = g.root.field('Custom Playbacks')
        cnt, ptr = struct.unpack_from('<iI', g.root.data, f.offset)
        top = -1
        for o in self.tags:
            if o.group == 'snd!' and o.kind == 'x':
                top = max(top, struct.unpack_from('<b', self.xc.read(o.src.addr + 0xD, 1), 0)[0])
        cps = root.block('Custom Playbacks')
        if top < max(cnt, len(cps)) or not ptr or ptr == NULL:
            return 0
        es = T.element_size(f)
        emptied = []
        n0 = len(cps)
        for i in range(n0, top + 1):
            n = self.past_end_element(f, ptr, i, es, emptied=emptied)
            if n is None:
                self.warn('sound_custom_playback_past_eof', 'Custom Playbacks element %d lies past the file end: not appended' % i)
                break
            self.remap_xbox(n)
            cps.append(n)
        if len(cps) > n0:
            self.stats['sound_custom_playbacks_appended'] += len(cps) - n0
            self.warn('sound_custom_playback_past_end', '%d Custom Playbacks element(s) appended: snd! indices up to %d past the %d in the '
                      'block; the block is extended with the bytes Xbox read there (%d wild block pointer(s) in them read as empty)'
                      % (len(cps) - n0, top, cnt, len(emptied)))
        return len(cps) - n0

    # ---- scenario --------------------------------------------------------------------------------------------------
    def stub_lightmaps(self, root, f):
        """A Structure BSP whose 'Structure Lightmap' is NONE (aerial / aerial2: the mod stripped the lightmap; its ltmp tag is empty)
        crashes Vista at map start (halo2.exe 0x6657D6, the tag table lookup of the NONE reference, 2026-10-22).  It gets an ltmp tag:
          * the unmodified stock BSP of a stock map (infection: lockout, BSP Checksum = stock lockout's) with --lightmap auto: the
            COMPLETE stock lightmap, which is its real one;
          * otherwise a stock Vista ltmp (the Xbox ltmp's name if a stock map has it, else the first stock one) that keeps only its
            root settings; finish_lightmaps puts the lightmap generated from the output BSP into it (h2conv.lightmap_gen)."""
        for e in root.blocks.get(f.offset, []):
            fl = e.field('Structure Lightmap')
            if fl is None or e.u32(fl.offset + 4) not in (0, NULL):
                continue
            names = [t.name for t in self.xc.find('ltmp') if t.name] + sorted(k[1] for k in self.donor if k[0] == 'ltmp')
            fb = e.field('Structure BSP')
            xt = self.xc.tag_by_datum(e.u32(fb.offset + 4)) if fb is not None else None
            ck = struct.unpack_from('<I', self.xc.read(self.xc.tag_addr(xt), 0xC), 8)[0] if xt is not None and not xt.is_empty else None
            # an unmodified stock BSP: the COMPLETE stock lightmap is its real one (--lightmap auto only)
            full = next((n for n in names if ck is not None and self.donor_ltmp_checksum(n) == ck), None) if self.lightmap_style == 'auto' else None
            o = self.donor_tag('ltmp', full) if full else next((x for x in (self.donor_tag('ltmp', n) for n in names) if x is not None), None)
            if o is None:
                self.warn('lightmap_stub_missing', 'no stock Vista ltmp to carry the generated lightmap of a BSP without one')
                continue
            e.set_u32(fl.offset, 0x6C746D70)                       # 'ltmp'
            # an OUTPUT datum: written after remap_xbox (which reads every reference as an Xbox datum and turned it back into NONE -
            # the stub never reached the map, aerial / aerial2 still crashed at 0x6657D6 on 2026-10-26)
            self._after_remap.append((e, fl.offset + 4, o.datum))
            if full:
                self.full_ltmps.add(o.index)
                self.stats['lightmap_stock'] += 1
                self.warn('lightmap_stock', 'Structure BSP without lightmap is the unmodified stock BSP: its stock lightmap %s' % o.name)
                continue
            self.stats['lightmap_stub'] += 1
            self.warn('lightmap_stub', 'Structure BSP without lightmap: ltmp %s gets a generated lightmap' % o.name)

    def _out_by_datum(self, d):
        """The OutTag of an OUTPUT datum, or None (NONE, a shared.map placeholder, a datum with a stale salt)."""
        if d in (0, NULL) or (d & 0xFFFF) >= len(self.tags):
            return None
        o = self.tags[d & 0xFFFF]
        return o if o.datum == d else None

    def grey_bitmap(self):
        """(datum, page count) of rasterizer\\default_2d in Vista's shared.map: the grey bitmap of the neutral-valid lightmap style."""
        t = self.vs_name.get(('bitm', 'rasterizer\\default_2d'))
        if t is None:
            raise LG.LightmapGenError('shared.map has no rasterizer\\default_2d')
        pages = len(T.decode_tag(self.vsh.read_addr, self.lv['bitm'], t.addr, 'bitm').block('Bitmaps'))
        return t.datum, pages

    def finish_lightmaps(self):
        """Every Structure BSP of the scenario against its lightmap, once all output tags are built (the lightmap is generated from the
        OUTPUT BSP - its converted vertex counts - never the Xbox one):
          * a lightmap that is a donor shell (stub_lightmaps) gets the generated lightmap (--lightmap native | neutral-valid; auto = native);
          * a real (converted) lightmap whose tables the engine would read outside of (a count below the BSP's cluster / instance count, a
            bucket index or slice outside its stream, no group: covenant_shaft's tables are one instance short) is replaced by the same;
          * any other real lightmap stays as it is, including the ones the engine rates 'not valid' (Bitmap Group NONE or a checksum that
            is not the BSP's: the Xbox itself draws those without lightmap) whose tables are complete.
        The complete stock lightmap of an unmodified stock BSP (full_ltmps) is left alone."""
        scn = self.tags[0] if self.tags else None
        if scn is None or scn.root is None:
            return
        fs = scn.root.field('Structure BSPs')
        if fs is None:
            return
        style = LG.STYLE_NATIVE if self.lightmap_style == 'auto' else self.lightmap_style
        seen = set()
        for e in scn.root.blocks.get(fs.offset, []):
            fb, fl = e.field('Structure BSP'), e.field('Structure Lightmap')
            if fb is None or fl is None:
                continue
            sb, lt = self._out_by_datum(e.u32(fb.offset + 4)), self._out_by_datum(e.u32(fl.offset + 4))
            if sb is None or lt is None or sb.group != 'sbsp' or lt.group != 'ltmp' or sb.root is None or lt.root is None:
                continue
            if lt.index in self.full_ltmps:
                continue
            if lt.index in seen:
                self.warn('lightmap_shared', '%s: two Structure BSPs use the same lightmap tag; only the first gets a generated lightmap' % lt.name)
                continue
            seen.add(lt.index)
            shell = lt.kind == 'd'
            why = 'no lightmap in the source'
            if not shell:
                try:
                    probs = LG.check(lt.root, sb.root)
                except Exception as ex:                                           # noqa: BLE001
                    self.warn('lightmap_check_failed', '%s: lightmap not checked (%r)' % (lt.name, ex))
                    continue
                eng = LG.engine_problems(probs)
                for p in probs:
                    if not p.engine:
                        self.stats['lightmap_note:%s' % p.code] += 1
                if not eng:
                    # the section sizes of a converted lightmap's Clusters differ from the converted BSP's in every map: not worth a line
                    told = sorted({p.code for p in probs} - {'section_sizes'})
                    if told:
                        self.warn('lightmap_not_generated', '%s: kept as it is, its tables are complete (%s)' % (lt.name, '; '.join(told)))
                    continue
                why = '; '.join(p.message for p in eng[:3]) + (' ...' if len(eng) > 3 else '')
            try:
                kw = {}
                if style == LG.STYLE_NEUTRAL_VALID:
                    kw['bitmap'], kw['bitmap_pages'] = self.grey_bitmap()
                info = LG.generate(lt.root, sb.root, style, lambda n, off, data: self.add_raw_patch(n, off, 'geometry', data), lt.datum, **kw)
            except Exception as ex:                                               # noqa: BLE001
                self.warn('lightmap_generate_failed', '%s: %r' % (lt.name, ex))
                if self.opts.get('debug'):
                    raise
                continue
            for og in info['old_groups']:                                         # the replaced lightmap's geometry blocks are not written
                for nd in og.iter_nodes():
                    for tok in nd.patches.values():
                        if tok[0] == 'raw':
                            self.raws[tok[1]] = ('skipped', b'')
            if style == LG.STYLE_NEUTRAL_VALID:
                self.drop_lightmap_predictions(sb, sb.root)
            self.stats['lightmap_generated:%s' % style] += 1
            self.warn('lightmap_generated', '%s: %s lightmap generated for the BSP %s (%s): %d cluster(s), %d instance(s), %d bucket(s) of %d vertices'
                      % (lt.name, style, sb.name, why, info['clusters'], info['instances'], len(info['buckets']), info['bucket_vertices']))
            probs = LG.check(lt.root, sb.root, style)
            if probs:
                self.warn('lightmap_generated_check', '%s: %s' % (lt.name, '; '.join(p.message for p in probs[:3])))

    def donor_geometry(self, o, root, cache):
        """Geometry blocks of a donor tag used complete (a stock lightmap): their bytes live in the stock map's geometry region, so they
        are copied into this map's (raw patch).  -> [(node, owner offset)] to set to this tag's datum after remap_donor."""
        owners = []
        for nd in root.iter_nodes():
            for f in nd.fields:
                if f.name not in ('Block Offset', 'Resource Block Offset') or f.size != 4:
                    continue
                ptr, size = nd.u32(f.offset), nd.u32(f.offset + 4)
                if ptr in (0, NULL) or not size:
                    continue
                if ptr >> 30:
                    raise RuntimeError('%s: geometry block %08X is not in the donor map' % (o.name, ptr))
                self.add_raw_patch(nd, f.offset, 'geometry', cache.raw(ptr & 0x3FFFFFFF, size))
                if f.offset + 0x1C <= len(nd.data):
                    owners.append((nd, f.offset + 0x18))
        self.stats['donor_geometry_blocks'] += len(owners)
        return owners

    def donor_ltmp_checksum(self, name):
        """Lightmap Groups[0] 'Structure Checksum' of the stock Vista ltmp of that name (None if there is none)."""
        hits = self.donor.get(('ltmp', (name or '').lower()))
        if not hits:
            return None
        c = self.donor_cache(hits[0][0])
        t = c.tags[hits[0][1]]
        g = T.decode_tag(c.read_addr, self.lv['ltmp'], t.addr, 'ltmp').block('Lightmap Groups')
        return g[0].u32(4) if g else None

    def drop_lightmap_predictions(self, o, root):
        """A BSP whose lightmap was stripped (stub ltmp): its Predicted Resources of types 5..8 (lightmap geometry object / instance
        buckets, lightmap cluster / instance bitmaps) index the lightmap that is gone.  Against the empty stub they read past empty
        blocks: aerial crashed in game at halo2.exe 0x665A2A (sub_49F966, type 8 index 57, 2026-10-26).  On Xbox the lightmap was NONE."""
        n = 0
        for nd in root.iter_nodes():
            for f in nd.fields:
                if f.name != 'Predicted Resources' or f.type not in T.BLOCK_TYPES:
                    continue
                els = nd.blocks.get(f.offset, [])
                keep = [e for e in els if not 5 <= e.i16(0) <= 8]
                if len(keep) != len(els):
                    n += len(els) - len(keep)
                    nd.blocks[f.offset] = keep
        if n:
            self.warn('lightmap_predictions_dropped', '%s: %d predicted resource(s) of the stripped lightmap removed' % (o.name, n))

    def scenario(self, root):
        f = root.field('Structure BSPs')
        for e in root.blocks.get(f.offset, []):
            e.data[0:0xC] = b'\0' * 0xC          # Vista: sbsp / ltmp are ordinary tags (offset/size/address 0)
        self.stub_lightmaps(root, f)
        # Scenery 'Valid Multiplayer Games': one bit per game type, 7 on Xbox and Vista (stock Xbox: 0x20 / 0x21 / 0x61).  funbox sets
        # 0xFEEB; bits 7..15 mean nothing to either engine, so masking them off keeps every game-type test identical.
        for e in root.blocks.get(root.field('Scenery').offset, []):
            fv = e.field('Valid Multiplayer Games')
            if fv is not None and e.u16(fv.offset) & 0xFF80:
                e.set_u16(fv.offset, e.u16(fv.offset) & 0x7F)
                self.stats['scenery_game_bits_masked'] += 1
        # Player Starting Locations 'Game Type 1..4': an enum 0..14 (0 = none).  ricochet has junk (4772, 62505) that matches no game
        # type on Xbox - exactly what none (0) does
        for e in root.blocks.get(root.field('Player Starting Locations').offset, []):
            for i in range(1, 5):
                fg = e.field('Game Type %d' % i)
                if fg is not None and fg.size == 2 and e.u16(fg.offset) > 14:
                    e.set_u16(fg.offset, 0)
                    self.stats['start_game_type_junk_cleared'] += 1
        try:
            xroot = T.decode_tag(self.xc.read, self.lx['scnr'], self.xc.scenario.addr, 'scnr', dropped=[])
            HS.convert_scenario_scripts(self, xroot, root, self.hs_tables)
        except Exception as ex:                                           # noqa: BLE001
            self.warn('scripts_failed', repr(ex))

    # ---- driver ------------------------------------------------------------------------------------------------------
    def run(self):
        t0 = time.time()
        self.setup()
        xc = self.xc
        self.log('input  : %s (%d tags, %s), plan: %s' % (os.path.basename(self.src), len(xc.tags), xc.header['scenario_path'],
                                                         {k: v for k, v in self.plan.stats.items() if ':' not in k}))
        # roots: scenario (index 0), globals, gestalt
        self.resolve_x(xc.scenario_datum)
        glob_out = self.resolve_x(xc.globals_datum)
        ugh = xc.find('ugh!')
        ugh_out = self.resolve_x(ugh[0].datum) if ugh else NULL
        self.build_queue()
        # the gestalt's past-end elements reference tags too (a custom playback's Sound Effect Template / Components): what they need
        # is built before the gestalt is finished, and a sound that brings in can extend the blocks again (the
        # appended references were remapped after the queue had drained, so a new tag was queued but never built)
        while self.extend_gestalt():
            self.build_queue()
        self.finish_gestalt()
        self.finish_globals(glob_out, ugh_out)
        self.finish_lightmaps()
        self.drop_empty_weather()
        self.hide_invisible_instances()
        self.set_object_radii()
        self.trim_mulg_weapons()
        self.drop_dead_tags()
        self.finish_decorators()
        self.finish_dead_decorator_blocks()
        self.finish_scenario()
        return self.write(glob_out, ugh_out, t0)

    def pad_object_type_root(self, o):
        """An object tag whose Object Type (+0) names a LARGER object group whose layout its data does not decode as (so it is no alias:
        find_cross_group_aliases / objtype.layout_group) is padded like an alias (objtype.pad_alias_root: zeros, references NONE) to
        that group's main struct size.  Every engine builds the object the Object Type names and reads that group's fields past the
        tag's own root - in the output, the bytes of the tag's own blocks.  Blockfort's scen high_shipping_crate is a 196-byte scenery
        with Object Type 4 = garb (468 bytes): read as garb (halo2 MCC conversion, h2conv.rev: layout_group finds it decodes there), its
        Predicted Resources element {0, 0, bitm rasterizer\\linear_corner_fade} became garb 'Unknown' = group 00000000 + a bitm datum
        and the Vista -> MCC verify refused the map (tagref_group; w3, 2026-10-27).  The tag's own bytes, Object Type included, stay.
        Only when that group's view of the tag's own bytes is the tag's structure (every block / dataref / reference field of it inside
        the root is one of the tag's own, at the same offset: scen -> garb share the object header); otherwise no reader can take the
        bytes for that group (Infiltration's mach house_gate, Object Type 2 = weap: weap Predicted Bitmaps at 0x104 lies in the
        machine's own fields) and padding would only leave an unreferenced gap (validate tile.gap)."""
        r = o.root
        if r is None or r.group not in OBJECT_TYPE_GROUPS or len(r.data) < 2 or r.group not in self.lv:
            return
        ot = struct.unpack_from('<h', r.data, 0)[0]
        g = OBJECT_TYPE_GROUPS[ot] if 0 <= ot < len(OBJECT_TYPE_GROUPS) else None
        if g is None or g == r.group or g not in self.lv:
            return
        n0 = len(r.data)
        kinds = T.BLOCK_TYPES + T.DATA_TYPES + ('tagref',)
        own = {(f.offset, f.type, f.size) for f in self.lv[r.group].fields if f.type in kinds}
        if any((f.offset, f.type, f.size) not in own for f in self.lv[g].fields if f.type in kinds and f.offset + f.size <= n0):
            return
        pad_alias_root(r, g, self.lv)
        if len(r.data) > n0:
            self.stats['object_type_root_padded'] += 1
            self.warn('object_type_root_padded', '%s %s: Object Type %d = %s, data of %d bytes padded to %d (zeros, references NONE)' % (
                o.group, o.name, ot, g, n0, len(r.data)))

    def build_queue(self):
        """Build every queued output tag (references found while building queue more); a failed one gets fallback_node."""
        while self.queue:
            o = self.queue.popleft()
            try:
                o.root = self.build_node(o)
            except Exception as ex:                                       # noqa: BLE001
                self.warn('convert_failed', '%s %s: %r' % (o.group, o.name, ex))
                for r in self.salvaged:
                    if o.kind == 'x' and r['index'] == o.src.index:
                        r['converted'] = False                            # salvaged, but a later step failed (fallback_node)
                if self.opts.get('debug'):
                    raise
                o.root = self.fallback_node(o)

    def finish_globals(self, glob_out, ugh_out):
        """A LOCAL matg in a multiplayer map: its 9 language packs must stay shared.map's (the shared unic tags index them; the engine
        merges the header pack after them), and its Sound Globals gestalt is shared.map's (the map's own ugh! is the secondary one)."""
        o = next((x for x in self.tags if x.datum == glob_out), None)
        if o is None or o.root is None:
            return
        vm = [t for t in self.vsh.tags if t.group == 'matg' and t.addr][0]
        raw = self.vsh.read_addr(vm.addr + LOC.MATG_LANG_OFF, LOC.PACK_SIZE)
        o.root.data[LOC.MATG_LANG_OFF:LOC.MATG_LANG_OFF + LOC.PACK_SIZE] = raw
        shared_ugh = next(t for t in self.vsh.tags if t.group == 'ugh!' and t.addr)
        for n in o.root.iter_nodes():
            if n.path.startswith('/Sound Globals'):
                for f in n.fields:
                    if f.type.lower() == 'tagref':
                        off = f.offset + (4 if f.size == 8 else 0)
                        if struct.unpack_from('<I', n.data, off)[0] == ugh_out and ugh_out != NULL:
                            struct.pack_into('<I', n.data, off, shared_ugh.datum)
                            self.stats['matg_gestalt_shared'] += 1

    def locale_strings(self):
        """Header pack = the strings of every local unic tag (ranges from 0 per language; the engine shifts them past the matg strings).
        Gathered BEFORE the string table is written: a unic string id whose text nothing else uses (funbox, dawn_of_warfare) is added to
        the table here; added after set_strings it was an index past the end of the table."""
        tables = SX.locale_tables(self.xc)
        langs = [[] for _ in range(9)]
        for o in self.tags:
            if o.group != 'unic' or o.root is None:
                continue
            if o.kind == 'x':
                per = SX.unic_strings(self.xc, o.src, tables)
            else:
                per = [[] for _ in range(9)]
                self.warn('unic_donor', 'donor unic %s keeps no strings' % o.name)
            for L in range(9):
                start = len(langs[L])
                for text_sid, data in per[L]:
                    langs[L].append((self.strs.sid(self.out_sid_text(text_sid)) if text_sid else 0, data))
                struct.pack_into('<HH', o.root.data, 0x10 + 4 * L, start if per[L] else 0, len(per[L]))
            for f in o.root.fields:
                if f.type in T.BLOCK_TYPES:
                    o.root.blocks[f.offset] = []
                elif f.type in T.DATA_TYPES:
                    o.root.datas[f.offset] = b''
        return langs

    def locale(self, b, langs):
        base = b.locale_base
        blob, entries, pack = LOC.build_blob(langs, base)
        b.set_locale(blob, pack if any(langs) else None)

    def resolve_token(self, tok):
        return self._resolved[tok[1]]

    def write(self, glob_out, ugh_out, t0):
        xc = self.xc
        name = os.path.splitext(os.path.basename(self.out))[0][:31]
        # header name = what Cartographer's list shows for a local-matg map: --display-name, else the words of the SOURCE file's name
        # (packs.display_name: 'Dawn_of_Warfare' -> 'Dawn Of Warfare', ' OG' after the original maps; an Xbox mod keeps the stock
        # internal name, e.g. bulldog.map says 'headlong', which would read as a second stock Headlong), else the output file stem
        hdr = W.new_header(self.opts.get('display_name') or PK.display_name(self.src, self.stock_dir) or name, xc.header['scenario_path'], map_type=1,
                           build=VISTA_BUILD, uses_shared=(1, 1, 0), shared_creation_time=W.read_shared_creation_times(self.vm),
                           secondary_ugh_tag_index=ugh_out)
        b = W.VistaBuilder(hdr, W.groups_from_stock(os.path.join(self.vm, 'shared.map')), tag_offset_mask=MP_MASK)
        ptr = [None] * len(self.raws)
        for kind in ('sound', 'geometry'):
            for i, (k, data) in enumerate(self.raws):
                if k == kind:
                    ptr[i] = b.add_sound(data) if kind == 'sound' else b.add_geometry(data)
        tag_count = len(self.vsh.tags)
        names = [None] * tag_count
        for o in self.tags:
            names[o.index] = o.name
        for p in self.placeholders:
            names[p.index] = p.name
        langs = self.locale_strings()
        b.set_strings(self.strs.strings)
        b.set_tag_names(names)
        self.locale(b, langs)
        for i, (k, data) in enumerate(self.raws):
            if k == 'bitmap':
                ptr[i] = b.add_bitmap_pixels(data)
        self._resolved = ptr
        inst = [VistaTag(i, '', NULL, 0, 0) for i in range(tag_count)]
        for p in self.placeholders:
            inst[p.index] = p
        cursor = b.tag_region_data_offset(tag_count)
        chunks = []
        for o in self.tags:
            data = b''
            if o.root is not None:
                try:
                    # the layout of the group the data was BUILT as: a cross-group alias (find_cross_group_aliases) is a machine
                    # under a weapon entry; for every other tag root.group == o.group
                    pad_alias_root(o.root, o.group, self.lv)
                    self.pad_object_type_root(o)
                    data = T.encode_tag(o.root, self.lv[o.root.group if o.root.group in self.lv else o.group], MP_MASK + cursor, self.align,
                                        resolve=self.resolve_token)
                except Exception as ex:                                   # noqa: BLE001
                    self.warn('encode_failed', '%s %s: %r' % (o.group, o.name, ex))
                    data = b''
            if len(data) % 4:
                data += b'\0' * (4 - len(data) % 4)
            if not data:
                chunks.append(b'')                          # dead tag (drop_dead_tags): empty slot, nothing references it
                continue
            inst[o.index] = VistaTag(o.index, o.group, o.datum, (MP_MASK + cursor) if data else 0, len(data), o.name)
            chunks.append(data)
            cursor += len(data)
        scn = self.tags[0].datum
        b.set_tags(inst, b''.join(chunks), scn, glob_out, 0)
        img = b.build_image()
        img.mopp_checksum = 'compute'
        os.makedirs(os.path.dirname(self.out), exist_ok=True)
        W.write_image(img, self.out)
        problems = W.validate_file(self.out)
        rep = {'output': self.out, 'local_tags': len(self.tags), 'raws': collections.Counter(k for k, _ in self.raws if k != 'skipped'),
               'raw_bytes': sum(len(d) for _, d in self.raws), 'size': os.path.getsize(self.out), 'seconds': round(time.time() - t0, 1),
               'validate_file': problems, 'stats': dict(self.stats), 'warnings': dict(self.warnings),
               'plan': {k: v for k, v in self.plan.stats.items() if ':' not in k}, 'salvage': self.salvaged}
        self.log('output : %s (%d local tags, %.1f MB, %.1fs) validate_file: %s' % (self.out, len(self.tags), rep['size'] / 1048576.0,
                                                                                 rep['seconds'], problems or 'OK'))
        for code, lst in self.warnings.items():
            self.log('  %-28s %4d  e.g. %s' % (code, self.stats.get('warn:' + code, 0), lst[0][:160]))
        return rep


GEOMETRY_GROUPS = ('mode', 'sbsp', 'ltmp', 'DECR', 'PRTM', 'weat', 'scnr')
MERGE_GROUPS = ('matg',)
# never taken from Bungie's Vista port by vista_port_of_tainted: raw data, geometry and the gestalt-indexed sounds (their own converters
# place the data), shaders (build_shader already starts from Bungie's port), the engine-merged globals and the map's own tags
# blocks a Vista port keeps as Bungie wrote them (vista_port_of_tainted): the prediction hints, which list what the tool found
# reachable at cache-build time (Foundry: 14 of 19 refused ports differed only there, e.g. rocket_launcher 18 vs 19 entries)
PORT_OWN_BLOCKS = ('/Predicted Resources', '/Predicted Bitmaps')
TAINT_PORT_SKIP = frozenset(GEOMETRY_GROUPS + MERGE_GROUPS + ('bitm', 'snd!', 'jmad', 'shad', 'ugh!', 'unic', 'sbsp', 'ltmp', 'scnr',
                                                             'MGS2'))    # MGS2: build_node clears 'Fuzzy' (2026-10-22), a port would skip it
# geometry_block_info flags 0x0900 in every stock Vista map (shared, headlong, 03a_oldmombasa, beavercreek);
# mode Sections, sbsp Clusters / Instanced Geometry and ltmp Geometry Buckets stay 0x0100
GEOMETRY_FLAG_0800 = {('DECR', ''), ('PRTM', ''), ('sbsp', '/Decorators/Cache Blocks'), ('sbsp', '/Water Definitions'),
                      ('mode', '/PRT Info'), ('scnr', '/Decorators/Cache Blocks'),
                      # weather particle systems: 0x0900 in every stock Vista weat (4 of 4).  Without the bit halo2.exe sub_781F5E (the geometry
                      # cache evict, sub_66506E) frees the dword at section data + 0x40 on unload; this block's section data is 48 bytes, so
                      # that dword is vertex data (a float like 0.006) -> crash on quit to menu, halo2.exe+0x38202E, in EVERY converted map
                      # that holds a weat tag (facility, glasswall, high_tide, aerial2, deep_darkness ... 2026-10-29)
                      ('weat', '/Particle System')}
# byte ranges of a merged tag that are always Vista's: matg's 9 language packs (+0x188, 252 B: per-map string-table placement)
# matg +0x108 Rasterizer Data: always Vista's (DX9 global vertex shaders, default bitmaps).  Dawn of Warfare's shifted name table made the
# block compare as "modified"; resolve_x then pointed Global Vertex Shaders 24-31 (screen_0_stage..screen_7_stage) at debug_screenspace and
# MCC drew nothing but the HUD (2026-11-02).  No other of the 180 pack maps took the mod's block.
MERGE_KEEP_VISTA = {'matg': ((0x108, 0x110), (0x188, 0x188 + 252))}
# prefer_own_tags leaves these on the merge base's Vista tag: the menu / widget tree is Bungie's PC one (keyboard, video and audio settings
# screens the Xbox tree does not have); a mod's menu text / widget edits are not carried over
OWN_TAG_SKIP = frozenset(('wgtz', 'wigl', 'wgit', 'skin'))
# vista_port_of_tainted: a port that differs structurally is still taken for these (references by name: prefer_own_tags)
PORT_BY_NAME_GROUPS = frozenset(('hudg',))
# a model-family tag (or a sound) stored under another group's index entry (find_data_twins): rebuilt under the group its references
# declare only when the data decodes as that group with these blocks non-empty (an all-zero or junk root never qualifies)
REINDEX_GROUPS = {'mode': ('Nodes', 'Sections'), 'coll': ('Regions', 'Nodes'), 'phmo': ('Rigid Bodies',), 'jmad': ('Skeleton Nodes',),
                  'hlmt': ('Variants',), 'snd!': ()}       # snd!: its gestalt pitch ranges must exist (find_data_twins)
TAGREF_GROUPS_FILE = 'xbox_tagref_groups.json'
_TAGREF_GROUPS = {}
# ref_group_allowed never refuses: owner back-pointers the converter rewrites itself (geometry block info / ugh! Extra Info 'Owner Tag',
# bitm element / jmad resource group 'Datum'), prediction hints and the scenario's simulation table (today's output kept for those)
TAGREF_UNCHECKED_FIELDS = ('Owner Tag', 'Datum')
TAGREF_UNCHECKED_BLOCKS = ('/Predicted Resources', '/Predicted Bitmaps', '/Simulation Definition Table')
# the "effect or sound" fields take any of these (stock: mach 'Delay Effect' snd!, weapon 'Empty Effect' effe + snd!, foot Sounds
# snd! + lsnd); the corpus scan's small-sample fields (mach Delay Effect = 18 snd! only) must not refuse a mod's effe there
TAGREF_EFFECT_GROUPS = frozenset(('effe', 'snd!', 'lsnd'))
# Vista redrew the HUD at 2x the Xbox pixels: every bitmap a stock nhdt widget draws is ~2x its Xbox twin (39 of 48 exactly, 8 redrawn
# a few pixels off, territory_hud_meters 4x) and every Fullscreen Offset of the Bitmap / Text Widgets is doubled (249 of 263 non-zero
# offsets in the 30 stock nhdt exactly 2x, the rest moved a pixel or two by hand); every other nhdt field is identical, Halfscreen /
# Quarterscreen included.  Vista draws a bitmap at its pixel size, so a mod's own HUD kept at Xbox values drew at half size and out of
# place next to the linked stock pieces (Satans_Crypt custom weapons, 2026-11-01).  opts['no_hud_upscale'] keeps the Xbox values.
HUD_SCALE = 2
HUD_OFFSET_BLOCKS = ('/Bitmap Widgets', '/Text Widgets')
HUD_BITMAP_BLOCKS = ('/Bitmap Widgets', '/Screen Effect Widgets')


_WIDGET_DROPS = {}


def load_widget_drops(path=None):
    """[{'object': lower object tag name, 'widget': lower widget tag name, 'model_digest': hex digest}] of data\\xbox_widget_drops.json
    (content-keyed widget drops, see XboxToVista.widgets_to_drop); [] when the table is missing."""
    path = path or os.path.join(_paths.data_dir(), WIDGET_DROPS_FILE)
    got = _WIDGET_DROPS.get(path)
    if got is None:
        import json
        try:
            with open(path, encoding='utf-8') as fh:
                got = [{'object': r['object'].lower(), 'widget': r['widget'].lower(), 'model_digest': r['model_digest'].lower()}
                       for r in json.load(fh)['rules']]
        except (OSError, ValueError, KeyError):
            got = []
        _WIDGET_DROPS[path] = got
    return got


def load_tagref_groups(path=None):
    """'owner group|block path|field name' -> frozenset of the target groups stock data stores in that tag reference field (stock Vista
    maps + stock Xbox maps).  {} when the table is missing (every reference then keeps today's
    fourcc correction)."""
    path = path or os.path.join(_paths.data_dir(), TAGREF_GROUPS_FILE)
    got = _TAGREF_GROUPS.get(path)
    if got is None:
        import json
        try:
            with open(path, encoding='utf-8') as fh:
                got = {k: frozenset(v) for k, v in json.load(fh)['fields'].items()}
        except (OSError, ValueError, KeyError):
            got = {}
        _TAGREF_GROUPS[path] = got
    return got


def _xname(c, d):
    t = c.tag_by_datum(d) if d not in (0, NULL) else None
    return (t.name or '').lower() if t else None


def _vname(c, d):
    if d in (0, NULL) or (d & 0xFFFF) >= len(c.tags):
        return None
    return (c.tags[d & 0xFFFF].name or '').lower()


def _ref_datum(node, name):
    f = node.field(name)
    return node.u32(f.offset + (4 if f.size == 8 else 0)) if f is not None else NULL


def sky_shader_names(c, read, layouts, tag_of, indices=False):
    """Lower names (indices=True: tag indices) of the shaders the skies of cache c draw with: every sky tag's Render Model ->
    Materials -> Shader.  read(addr, n) reads the cache, layouts is its group -> layout table, tag_of(datum) -> tag or None.  A sky or
    render model that does not decode is skipped (build_shader: sky_retemplate)."""
    out = set()
    for t in c.tags:
        if t.group != 'sky ' or not t.addr:
            continue
        try:
            mt = tag_of(_ref_datum(T.decode_tag(read, layouts['sky '], t.addr, 'sky '), 'Render Model'))
            if mt is None or not mt.addr or mt.group != 'mode':
                continue
            for m in T.decode_tag(read, layouts['mode'], mt.addr, 'mode').block('Materials'):
                sh = tag_of(_ref_datum(m, 'Shader'))
                if sh is not None and sh.group == 'shad':
                    out.add(sh.index if indices else (sh.name or '').lower())
        except Exception:                                                # noqa: BLE001
            continue
    out.discard('')
    return out


def _pp_arrays(root, name_of):
    pps = root.block('Postprocess Definition')
    if not pps:
        return {}
    out = {}
    for a in XboxToVista.SHADER_ARRAYS:
        try:
            kids = pps[0].block(a)
        except KeyError:
            kids = []
        out[a] = [name_of(k.u32(0)) for k in kids] if a == 'Bitmaps' else [bytes(k.data) for k in kids]
    return out


StockVista = collections.namedtuple('StockVista', 'map name')     # a stock Vista shader: its map file name (lower case) and lower tag name


def slot_evidence(xarr, varr):
    """The slot-map evidence of ONE (Xbox shader, Vista shader) pair from their arrays (_pp_arrays): per array
    (Xbox length, Vista length, {(i, j): Xbox slot i holds the same value as Vista slot j}, the Vista slot values as a tuple).
    The shipped table holds the same for the stock pairs WITHOUT the values: the last item is a StockVista (stock map + tag name), whose
    slot values VistaSlotValues reads from the user's own Vista maps when learn_slot_map needs them."""
    out = {}
    for a in XboxToVista.SHADER_ARRAYS:
        xs, vs = xarr.get(a, []), varr.get(a, [])
        if not xs and not vs:
            continue
        eq = frozenset((i, j) for i, xv in enumerate(xs) for j, vv in enumerate(vs) if xv == vv)
        out[a] = (len(xs), len(vs), eq, tuple(vs))
    return out


class VistaSlotValues(object):
    """resolver(StockVista, array) -> the slot values of that stock Vista shader (_pp_arrays form), read from the user's own stock Vista
    maps in `vista_dir`; None when the map or shader is not there.  Maps are opened on demand and each shader is decoded once."""

    def __init__(self, vista_dir, layouts=None):
        self.dir = vista_dir
        self.lv = layouts
        self._files = None
        self._maps = {}
        self._memo = {}

    def _open(self, mapname):
        got = self._maps.get(mapname)
        if got is None:
            got = False
            if self._files is None:
                self._files = {n.lower(): n for n in os.listdir(self.dir)} if self.dir and os.path.isdir(self.dir) else {}
            real = self._files.get(mapname)
            if real:
                try:
                    vc = VistaCache(os.path.join(self.dir, real))
                    by = {}
                    for t in vc.tags:
                        if t.group == 'shad' and t.addr and t.name:
                            by.setdefault(t.name.lower(), t)
                    got = (vc, by)
                except Exception:                                         # noqa: BLE001 - an unreadable map: its shaders stay unresolved
                    got = False
            self._maps[mapname] = got
        return got

    def __call__(self, ref, array):
        arrs = self._memo.get(ref, 0)
        if arrs == 0:
            arrs = None
            m = self._open(ref.map)
            t = m[1].get(ref.name) if m else None
            if t is not None:
                try:
                    lv = self.lv or XL.vista_layouts()
                    vr = T.decode_tag(m[0].read_addr, lv['shad'], t.addr, 'shad')
                    arrs = _pp_arrays(vr, lambda d, c=m[0]: _vname(c, d))
                except Exception:                                         # noqa: BLE001
                    arrs = None
            self._memo[ref] = arrs
        return None if arrs is None else arrs.get(array, [])


_SLOT_EVIDENCE = {}


def load_slot_evidence(path=None):
    """lower template name -> [(lower shader name, evidence)] from the shipped table (data\\xbox_shader_slot_maps.json; evidence as
    slot_evidence builds it, one entry per Bungie Xbox -> Vista shader port, its Vista shader named by StockVista).  {} when the table is
    missing."""
    path = path or os.path.join(_paths.data_dir(), SLOT_MAP_FILE)
    got = _SLOT_EVIDENCE.get(path)
    if got is None:
        import json
        got = {}
        try:
            with open(path, encoding='utf-8') as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            doc = None
        if doc:
            arr = doc['arrays']
            for stem, prs in doc['templates'].items():
                got[stem] = [(n, {a: (r[0], r[1], frozenset((v >> 8, v & 255) for v in r[2]), StockVista(vmap, n))
                                  for a, r in zip(arr, rows) if r is not None}) for n, vmap, rows in prs]
        _SKY_RETEMPLATE[path] = {k: v[0] for k, v in (doc or {}).get('sky_retemplate', {}).items()}
        _SLOT_EVIDENCE[path] = got
    return got


_SKY_RETEMPLATE = {}


def load_sky_retemplate(path=None):
    """lower Xbox template name -> lower Vista sky_ template name Bungie moved that template's stock sky layers to (the pair table's
    'sky_retemplate'; XboxToVista.output_stem).  {} when the table is missing."""
    path = path or os.path.join(_paths.data_dir(), SLOT_MAP_FILE)
    load_slot_evidence(path)
    return _SKY_RETEMPLATE.get(path, {})


_EMPTY_EVIDENCE = (0, 0, frozenset(), ())


def _same_vista_value(have, j, a, resolve):
    first = _vista_value(have[0], j, a, resolve)
    return all(_vista_value(e, j, a, resolve) == first for e in have)


def _vista_value(e, j, a, resolve):
    """The value of Vista slot j of array a in the pair with evidence e: stored (the map's own pairs) or read through `resolve` (stock pairs).
    A value that cannot be read is never equal to anything."""
    vd = e[3]
    if isinstance(vd, StockVista):
        vals = resolve(vd, a) if resolve else None
        if vals is None or j >= len(vals):
            return object()
        return vals[j]
    return vd[j]


def learn_slot_map(pairs, a, resolve=None):
    """Xbox slot for each Vista slot of array a ([.., None = keep the skeleton's value]) from the evidence (slot_evidence) of the pairs of one
    template, or None without pairs.  resolve(StockVista, array) -> slot values: see VistaSlotValues.  Vista slot j scores Xbox slot i once per pair holding equal values there.
    * The best Xbox slot(s) decide when they hold in a strict majority of the pairs (unanimity is not needed: one retextured stock pair
      is enough to break it - aerial2 tex_bump_env_combined base map / cube map kept the skeleton plasma_cannon_shiny's bitmaps over the
      author's forerunner plates, 2026-10-26).  The same slot wins among them, else a single one.
    * Several best slots without j are a permutation of the two templates' orders, never identity: the one no other Vista slot took,
      slots with a single free candidate first; none free = None.  sky_one_alpha_env Pixel Constants are Xbox [environment, opacity,
      alpha blend color, 1] and Vista [environment, alpha blend color, opacity, 1]: Vista 1 matched Xbox 0 / 2 / 3 in every pair and
      the old identity fallback gave it Xbox 1 (000000ff), so aerial2's six Sacred Icon sky layers drew black (2026-10-26); tex_bump_env
      Vertex Constants took the detail-map scale (16,16) as the base-map scale the same way.
    * A Vista slot with ONE value in every pair (at least 4) while its best Xbox slot differs in two or more of them is a constant of the Vista
      template, which the skeleton (same template) carries: None.  tex_bump_illum_3_channel Pixel Constants 8-10 are ffffff00 in all
      18 stock pairs and Xbox 0 (specular color) is white in 15 of them; tex_bump_illum_alpha_test Vertex Constants 5-7 match no Xbox
      slot in any pair.
    * No majority: the same slot when both layouts have the same length - but never a slot that matched nothing while another one did
      (None keeps the skeleton's value) - else None."""
    if not pairs:
        return None
    ev = [p.get(a, _EMPTY_EVIDENCE) for p in pairs]
    nv = max(e[1] for e in ev)
    nx = max(e[0] for e in ev)
    m, ties = [], {}
    for j in range(nv):
        have = [e for e in ev if j < e[1]]
        score = [sum(1 for e in have if i < e[0] and (i, j) in e[2]) for i in range(nx)]
        best = max(score, default=0)
        top = [i for i in range(nx) if score[i] == best] if best * 2 > len(have) else []      # == the unanimous slots when there are any
        # (>= 4 pairs: on 2 pairs, tex_bump_env_tiling_specular PC 4 env_midrange_tint_color was nulled - a mod's tint lost - review 2026-10-26)
        if len(have) >= 4 and len(have) - best >= 2 and _same_vista_value(have, j, a, resolve):
            m.append(None)
        elif j in top or len(top) == 1:
            m.append(j if j in top else top[0])
        elif top:
            m.append(None)
            ties[j] = top
        elif nv == nx and (score[j] or not best):
            m.append(j)
        else:
            m.append(None)
    claimed = {i for i in m if i is not None}
    while ties:
        free = {j: [i for i in c if i not in claimed] for j, c in ties.items()}
        j = next((j for j in sorted(free) if len(free[j]) == 1), None)
        if j is None:
            j = min(free)
        if free[j]:
            m[j] = free[j][0]
            claimed.add(m[j])
        del ties[j]
    return m


def slot_map_misses(m, pairs, a):
    """[(pair index, Vista slot j)] where slot map m does not reproduce a pair: j mapped to Xbox slot i and the pair's values differ."""
    out = []
    for k, p in enumerate(pairs):
        nx, nv, eq, _vd = p.get(a, _EMPTY_EVIDENCE)
        for j, i in enumerate(m or []):
            if i is not None and j < nv and i < nx and (i, j) not in eq:
                out.append((k, j))
    return out


def _field_same(a, b, f, ca, cb):
    """Is top-level field f equal in the Xbox tag a and the pack-shared tag b (references by name, string ids by text)?"""
    if f.type in T.BLOCK_TYPES:
        ka, kb = a.blocks.get(f.offset) or [], b.blocks.get(f.offset) or []
        return len(ka) == len(kb) and all(PX._same(x, y, ca, cb, []) for x, y in zip(ka, kb))
    if f.type in T.DATA_TYPES:
        return (a.datas.get(f.offset) or b'') == (b.datas.get(f.offset) or b'')
    tl = f.type.lower()
    if tl == 'tagref':
        o = f.offset + (4 if f.size == 8 else 0)
        return PX._label(ca, struct.unpack_from('<I', a.data, o)[0]) == PX._label(cb, struct.unpack_from('<I', b.data, o)[0])
    if tl == 'stringid':
        return ca.string_id(struct.unpack_from('<I', a.data, f.offset)[0]) == cb.string_id(struct.unpack_from('<I', b.data, f.offset)[0])
    return a.data[f.offset:f.offset + f.size] == b.data[f.offset:f.offset + f.size]


def _write_report(path, rep):
    import json
    if path:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(rep, fh, indent=1, default=str)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description='Halo 2 Xbox multiplayer map -> Halo 2 Vista.  Exit codes: 0 converted, 1 converted with validation problems or failed, '
                    '3 skipped (not a Halo 2 Xbox map, or not a multiplayer map), 4 unusable input (for example a truncated download).')
    ap.add_argument('src', help='the Xbox .map file')
    ap.add_argument('out', help='the output .map file, or a folder: the file is then named <Map>_<PackSuffix>.map after the map\'s pack')
    ap.add_argument('--vista-maps', help='folder with the stock Halo 2 Vista maps (default: H2CONV_VISTA_MAPS, settings.json, a search)')
    ap.add_argument('--stock-xbox-maps', metavar='DIR', help='folder with your ORIGINAL Halo 2 Xbox maps (optional, but the conversion is better '
                    'with it; default: H2CONV_STOCK_XBOX_MAPS, then "stock_xbox_maps" in settings.json)')
    ap.add_argument('--pack-dir', help='folder of the pack\'s mainmenu.map / shared.map (default: the map\'s folder, else the folder above it, '
                    'a maps folder in or beside it)')
    ap.add_argument('--display-name', help='the name the game lists (default: the words of the source file\'s name, ' +
                    '"Dawn_of_Warfare.map" -> "Dawn Of Warfare")')
    ap.add_argument('--strict', action='store_true', help='convert tags that cannot be verified against the pack shared.map')
    ap.add_argument('--lightmap', choices=LG.CHOICES, default='auto', help='what a map gets whose BSP has no usable lightmap (the mod stripped it, or it does not fit the BSP): auto (default) = the complete stock lightmap when the BSP is the unmodified BSP of a stock map, else native; native = a complete lightmap generated from the BSP that the game treats as not valid, so the BSP is drawn as on the original Xbox (no lightmap); neutral-valid = a generated valid lightmap with constant grey lighting. Maps that carry a working lightmap keep it.')
    ap.add_argument('--prt-repair', choices=('faithful', 'repaired', 'neutral'), default='faithful', help='render models whose precomputed radiance transfer was zeroed or damaged: faithful (default) = black as on the original Xbox, but stable (valid data, zero transfer, no stale lighting); repaired = Bungie\'s transfer when the model is a stock campaign model, else a neutral one generated from the model (lit by the map\'s SH light, which is green where the lightmap has no Lighting Environments); neutral = always generate')
    ap.add_argument('--report', help='write a JSON report here (also for a skipped or failed input: "status")')
    ap.add_argument('--drop-widget', action='append', default=[], metavar='OBJECT=WIDGET',
                    help='advanced: remove a widget (antenna ...) from an object tag, e.g. objects\\vehicles\\warthog\\warthog=objects\\vehicles\\warthog\\attachments\\warthog_antenna'
                         ' (the known case needs no flag: it is a built-in rule)')
    ap.add_argument('--object-radius', action='append', default=[], metavar='OBJECT=RADIUS',
                    help='advanced: set an object tag\'s Bounding Radius (+0x4), e.g. objects\\vehicles\\warthog\\warthog=5 (Tortuga\'s boat)')
    ap.add_argument('--unique-mod-strings', action='store_true',
                    help='for an MCC build: the HUD / multiplayer messages the mod edited get their own string id names, so MCC\'s UI cannot '
                         'replace them with its stock localization')
    ap.add_argument('--debug', action='store_true')
    a = ap.parse_args(argv)
    _paths.configure(stock_xbox_maps=a.stock_xbox_maps)
    out = a.out
    if os.path.isdir(out) or out.endswith(('\\', '/')):
        info = PK.detect(a.src, _paths.stock_xbox_maps_dir())
        out = os.path.join(out, PK.file_stem(os.path.splitext(os.path.basename(a.src))[0], info.suffix) + '.map')
    try:
        conv = XboxToVista(a.src, out, a.vista_maps, a.pack_dir, opts={
            'display_name': a.display_name, 'strict': a.strict, 'debug': a.debug, 'lightmap': a.lightmap, 'prt_repair': a.prt_repair,
            'drop_widgets': [tuple(s.split('=', 1)) for s in a.drop_widget if '=' in s],
            'object_radius': [(s.split('=', 1)[0], float(s.split('=', 1)[1])) for s in a.object_radius if '=' in s],
            'unique_mod_strings': a.unique_mod_strings})
        stem = os.path.basename(out).split('.')[0]                 # the engine's own rule: the text between the last '\' and the first '.'
        if len(stem) > PK.MAX_STEM:
            # halo2.exe sub_44CE2E (0x44CE2E, called by the map-list scan sub_44D021 with a 32-wchar buffer) refuses a stem of 32+ characters and
            # the scan then drops the file: the map is valid ("ok vista multiplayer" in mapinfo) but NEVER APPEARS in the map list
            # (Deep_darkness_PRTv2_KilltrocityV2.map, 2026-10-29).  An explicit output name is the caller's, so warn instead of renaming.
            msg = ('the output file stem %r is %d characters long; the game lists only stems of at most %d - this map will NOT appear in the map '
                   'list.  Use a shorter name (e.g. %r), or give a folder as the output so the name is made for you' % (
                       stem, len(stem), PK.MAX_STEM, stem[:PK.MAX_STEM]))
            print('WARNING: %s' % msg, flush=True)
            conv.warn('file_stem_too_long', msg)
        rep = conv.run()
    except (NotHalo2MapError, NotMultiplayerError) as ex:
        print('SKIPPED: %s' % ex, flush=True)
        _write_report(a.report, {'status': 'skipped', 'code': 'not_halo2' if isinstance(ex, NotHalo2MapError) else 'not_multiplayer',
                                 'message': str(ex), 'input': os.path.basename(a.src)})
        return EXIT_SKIPPED
    except TruncatedMapError as ex:
        print('ERROR: %s' % ex, flush=True)
        _write_report(a.report, {'status': 'failed', 'code': 'truncated', 'message': str(ex), 'input': os.path.basename(a.src)})
        return EXIT_BAD_INPUT
    rep['status'] = 'ok' if not rep['validate_file'] else 'failed'
    rep['display_name'] = a.display_name or PK.display_name(a.src, conv.stock_dir)
    _write_report(a.report, rep)
    return EXIT_OK if not rep['validate_file'] else EXIT_FAILED


if __name__ == '__main__':
    sys.exit(main())
