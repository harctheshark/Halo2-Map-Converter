# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Gameplay handlers: char, goof, scnr (non-script / non-sound parts), matg (non-sound parts), nhdt, hudg, mulg, sily,
ssce and the option-only changes of weap, proj, eqip, bloc, ctrl, mach, scen, garb, lifi.

The generic transcoder (transcode.convert_node) already pairs the MCC and Vista layouts, copies matched fields, remaps
ids, drops MCC-only fields/blocks and applies h2conv/data/enum_policy.json (MCC-only flag bits cleared, MCC-only enum values
mapped).  What it cannot know - and what the handlers below add - is how to *downgrade* MCC-only content so that the
Vista engine neither misreads nor crashes on it:

  * MCC-only game types (Medic, VIP, Infection; Headhunter / Race in MCC-only masks) have no Vista meaning.  Placed
    content that is ONLY for such games is neutralised (None game-type slots, Not Automatically, Race-bound spawn
    zones - an EMPTY mask means "all games" in Vista); mixed content keeps its Vista games.
  * Indexed lists whose positions are hard-wired in the engine (mulg Weapons/Vehicles, matg Global Vertex Shaders)
    are truncated to the Vista length, not re-ordered.
  * Elements that exist only for an MCC-only enum value are DROPPED rather than re-pointed (mulg/matg 'Pre Game Over'
    event, goof default variants / settings of MCC-only categories, scnr 'Medic' spawn overloads).
  * The scnr Simulation Definition Table (cache-builder list) never holds a null entry in Vista: unresolved entries
    are dropped.  The engine BINARY-SEARCHES it (sub_5F43BB), so the converted table is sorted ascending (signed int32,
    duplicates kept like the Vista tool) and gets the SHARED twin datum of every local entry whose (group, name) also exists as a shared
    placeholder (see scnr_simulation_table below).  (Predicted Resources are handlers/computed.py's.)
  * Values the generic pass can only zero get their nearest Vista equivalent (damage reporting 42/43 -> 36, oracle:
    gauss_bullet 7/7); the matg damage groups get Vista's order; matg 'Anniversary Globals' must stay empty.
  * MCC pads fixed ASCII fields with 0xFE after the terminator; Vista pads with 0 (hudg/matg/scnr).
  * hudg: MCC has no button icons / icon bitmap / damage indicator bitmap (all null / empty); Vista needs them ->
    taken from the Vista donor hudg of the same name.

Every handler is idempotent (running it twice gives the same node), recomputes its result from the MCC node and works on
(ctx, mcc_node, vista_node) where the Vista node was produced by the generic conversion.  Handlers never import
transcode.  Registered at priority 110 (after the 100-priority handlers of scripts / locale / sound).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import os
import struct

from . import node_handler
from .. import tagtree as T

NULL = 0xFFFFFFFF
BLOCK_TYPES = ('tagblock', 'reflexive')

# ------------------------------------------------------------------------------------------------
# constants (every number is measured against the stock maps)
# ------------------------------------------------------------------------------------------------
VISTA_DAMAGE_WARTHOG_GUNNER = 36        # Vista 'Warthog Gunner'; MCC 42/43 = Warthog Gunner Gauss / Rocket
VISTA_MULG_WEAPONS = 17                 # stock Vista mulg 'Weapons' count (MCC: 20, appended smg_silenced, scarab gun, gpmg)
VISTA_MULG_VEHICLES = 11                # stock Vista mulg 'Vehicles' count (MCC: 14, appended creep, falcon, gravity throne)
VISTA_GLOBAL_VERTEX_SHADERS = 32        # stock Vista matg Rasterizer Data 'Global Vertex Shaders' (MCC 33: + debug_screenspace)
PRE_GAME_OVER_EVENT = 0x31              # MCC-only multiplayer event enum value
GAME_OVER_EVENT = 19                    # Vista 'general_game_over' (same announcer sound)
MCC_ONLY_GAME_MASK_SCNR_SCENERY = 0x0780  # Medic, VIP, Infection, Headhunter (bits 7..10) in Valid Multiplayer Games
NOT_AUTOMATICALLY = 1                   # scnr object Placement Flags bit 0
ZONE_NEVER_USED_MASK = 1 << 4           # Relevant Games bit 4 'Race': no Vista game type -> zone never used
CROSSHAIR_ANCHOR = 4                    # Vista nhdt Anchor 'Crosshair' (screen centre) - fallback for MCC anchors 6..9
SILY_VISTA_MAX_PARAMETER = 0x6F

# Registry of every MCC-vs-Vista option difference this module answers (checked against
# plugins.diff_layouts): (group, vista path, field name) -> policy summary.
HANDLED_OPTION_DIFFS = {}


def _handled(group, path, field, policy):
    HANDLED_OPTION_DIFFS[(group, path, field)] = policy


# ------------------------------------------------------------------------------------------------
# small helpers
# ------------------------------------------------------------------------------------------------
def _f(node, name, nth=0):
    """nth field named `name` (case-insensitive) of the node, or None."""
    n = name.lower()
    k = 0
    for f in node.fields:
        if f.name and f.name.lower() == n:
            if k == nth:
                return f
            k += 1
    return None


def _fat(node, name, offset):
    """field named `name` located at `offset` (disambiguates repeated names), or None."""
    n = name.lower()
    for f in node.fields:
        if f.offset == offset and f.name and f.name.lower() == n:
            return f
    return None


def _rd(node, f):
    return int.from_bytes(node.data[f.offset:f.offset + f.size], 'little')


def _wr(node, f, val):
    node.data[f.offset:f.offset + f.size] = (val & ((1 << (8 * f.size)) - 1)).to_bytes(f.size, 'little')


def _opt_values(f):
    return set(int(v) for _n, v in (f.options or ()))


def _mcc_only_values(mf, vf):
    """Option values (enum values / flag bit indices) that exist only in the MCC definition of the field."""
    if mf is None or vf is None:
        return set()
    return _opt_values(mf) - _opt_values(vf)


def _stat(ctx, key, n=1):
    st = getattr(ctx, 'stats', None)
    if st is not None:
        st[key] += n


def _warn(ctx, code, msg):
    w = getattr(ctx, 'warn', None)
    if w is not None:
        w(code, msg)


def _block_fields(node, name):
    n = name.lower()
    return [f for f in node.fields if f.type in BLOCK_TYPES and f.name and f.name.lower() == n]


def _set_kids(node, f, kids):
    node.blocks[f.offset] = kids
    node.set_i32(f.offset, len(kids))


def _filter_block(m, v, name, drop_pred):
    """Drop the Vista elements of the block(s) called `name` whose MCC counterpart satisfies drop_pred(mcc_elem).
    MCC and Vista element lists are parallel (the generic pass converts element by element); a list whose length
    differs from the MCC one was already filtered (idempotence) and is left alone.  Returns the number dropped."""
    dropped = 0
    for mf, vf in zip(_block_fields(m, name), _block_fields(v, name)):
        mk = m.blocks.get(mf.offset) or []
        vk = v.blocks.get(vf.offset) or []
        if len(mk) != len(vk) or not vk:
            continue
        keep = [vk[i] for i in range(len(vk)) if not drop_pred(mk[i])]
        if len(keep) != len(vk):
            dropped += len(vk) - len(keep)
            _set_kids(v, vf, keep)
    return dropped


def _truncate_block(v, name, limit):
    n = 0
    for vf in _block_fields(v, name):
        vk = v.blocks.get(vf.offset) or []
        if len(vk) > limit:
            n += len(vk) - len(vk[:limit])
            _set_kids(v, vf, vk[:limit])
    return n


def _drop_null_refs(v, block_name, ref_name):
    """Drop elements of block `block_name` whose tag reference `ref_name` is null.  Returns the count dropped."""
    dropped = 0
    for vf in _block_fields(v, block_name):
        vk = v.blocks.get(vf.offset) or []
        if not vk:
            continue
        keep = []
        for e in vk:
            rf = _f(e, ref_name)
            if rf is not None and _ref_is_null(e, rf):
                continue
            keep.append(e)
        if len(keep) != len(vk):
            dropped += len(vk) - len(keep)
            _set_kids(v, vf, keep)
    return dropped


def _ref_is_null(node, rf):
    """True when a tagref field (8 B group+datum, or 4 B datum-only) holds the null datum."""
    if rf.size == 8:
        return struct.unpack_from('<I', node.data, rf.offset + 4)[0] == NULL
    return struct.unpack_from('<I', node.data, rf.offset)[0] == NULL


def _clone_node(node):
    """Copy of a Node tree that shares the (immutable) plugin Field lists."""
    n = T.Node(node.group, node.path, node.fields, len(node.data), node.data)
    n.src_addr = node.src_addr
    n.datas = dict(node.datas)
    n.patches = dict(node.patches)
    for off, kids in node.blocks.items():
        n.blocks[off] = [_clone_node(k) for k in kids]
    return n


def _scrub_ascii(node):
    """Zero every byte after the first NUL of the fixed ASCII fields of one struct (MCC pads with 0xFE)."""
    n = 0
    for f in node.fields:
        if f.type == 'ascii' and f.size > 4:
            b = node.data[f.offset:f.offset + f.size]
            z = b.find(0)
            if z >= 0 and any(b[z:]):
                node.data[f.offset + z:f.offset + f.size] = b'\0' * (f.size - z)
                n += 1
    return n


def _scrub_tree(ctx, node):
    n = 0
    for nd in node.iter_nodes():
        n += _scrub_ascii(nd)
    if n:
        _stat(ctx, 'gp_ascii_padding_scrubbed', n)
    return n


def _clean(ctx, m, v, name, nth=0, vmap=None, default=0, offset=None, label=None):
    """Make field `name` of the Vista node valid for the Vista option set: MCC-only flag bits are cleared, MCC-only
    enum values are replaced by vmap[value] (else `default`).  The value is recomputed from the MCC node, so the
    result does not depend on what the generic pass wrote.  Returns (mcc value, new value) when something changed."""
    if offset is None:
        vf, mf = _f(v, name, nth), _f(m, name, nth)
    else:
        vf, mf = _fat(v, name, offset), _fat(m, name, offset)
    if vf is None or mf is None:
        return None
    only = _mcc_only_values(mf, vf)
    if not only:
        return None
    val = _rd(m, mf)
    if vf.type.startswith('flags'):
        mask = 0
        for b in only:
            mask |= 1 << b
        new = val & ~mask
    elif val in only:
        new = (vmap or {}).get(val, default)
    else:
        new = val
    _wr(v, vf, new)
    if new != val:
        _stat(ctx, 'gp_option_downgraded:%s' % (label or '%s|%s|%s' % (v.group, v.path or '/', vf.name)))
        return val, new
    return None


# ------------------------------------------------------------------------------------------------
# object groups: bit 14 of the root Flags, damage reporting types, weapon flags, Predicted Resources
# ------------------------------------------------------------------------------------------------
OBJECT_GROUPS = ('weap', 'proj', 'eqip', 'bloc', 'ctrl', 'mach', 'scen', 'garb', 'lifi', 'ssce')
for _g in OBJECT_GROUPS:
    _handled(_g, '', 'Flags', 'MCC bit 14 (Prophet Is Not Displayed In Pegasus Builds) cleared')
_handled('weap', '', 'Flags', 'MCC bits 30/31 (Prevents Crouching, Uses 3rd Person Camera) cleared')
_handled('weap', '/Barrels', 'Flags', 'MCC bit 14 (Can Fire At Maximum Age) cleared')
_handled('weap', '', 'Melee Damage Reporting Type', '42/43 -> 36 Warthog Gunner')
_handled('weap', '/Barrels', 'Damage Effect Reporting Type', '42/43 -> 36 Warthog Gunner')
_handled('proj', '', 'Damage Reporting Type', '42/43 -> 36 Warthog Gunner')

_DAMAGE_MAP = {42: VISTA_DAMAGE_WARTHOG_GUNNER, 43: VISTA_DAMAGE_WARTHOG_GUNNER}


def _object_root(ctx, m, v):
    g = v.group
    # root Flags (flags16 at +2): MCC bit 14 only
    _clean(ctx, m, v, 'Flags', offset=0x2, label='%s|Flags' % g)
    if g == 'weap':
        _clean(ctx, m, v, 'Flags', offset=0x12C, label='weap|Flags32')
        _clean(ctx, m, v, 'Melee Damage Reporting Type', vmap=_DAMAGE_MAP, default=0)
    elif g == 'proj':
        _clean(ctx, m, v, 'Damage Reporting Type', vmap=_DAMAGE_MAP, default=0)
    # Predicted Resources are rebuilt / cleaned by handlers/computed.py (post pass); nothing to do here.


for _g in OBJECT_GROUPS:
    node_handler(_g, '', prio=110)(_object_root)


@node_handler('weap', '/Barrels', prio=110)
def weap_barrel(ctx, m, v):
    _clean(ctx, m, v, 'Flags', offset=0x0, label='weap|Barrels|Flags')
    _clean(ctx, m, v, 'Damage Effect Reporting Type', vmap=_DAMAGE_MAP, default=0)


# ------------------------------------------------------------------------------------------------
# char
# ------------------------------------------------------------------------------------------------
@node_handler('char', '/General Properties', prio=110)
def char_general(ctx, m, v):
    """MCC enum8 Metagame Type / Classification (+6/+7, deprecated campaign scoring metadata) occupy Vista's unnamed
    int16 at +6, which is 0 in 130/130 stock Vista characters -> write 0 (never invent values Vista never has)."""
    f = _fat(v, 'Unknown', 6)
    if f is None:
        return
    if m.data[6] or m.data[7]:
        _stat(ctx, 'gp_char_metagame_dropped')
    v.data[f.offset:f.offset + f.size] = b'\0' * f.size


_handled('char', '/General Properties', 'Metagame Type/Classification', 'MCC-only enum8 pair dropped (Vista int16 = 0)')


@node_handler('char', '', prio=110)
def char_root(ctx, m, v):
    """MCC appends 3 root tagblocks (Fake Empty Morph / Equipment Definitions, Campaign Metagame Bucket): no Vista
    counterpart, the generic pass drops them; count what is lost."""
    for name in ('Campaign Metagame Bucket',):
        mf = _f(m, name)
        if mf is not None and m.blocks.get(mf.offset):
            _stat(ctx, 'gp_char_metagame_bucket_dropped', len(m.blocks[mf.offset]))
    _scrub_ascii(v)


# ------------------------------------------------------------------------------------------------
# goof (multiplayer variant settings interface)
# ------------------------------------------------------------------------------------------------
_handled('goof', '', 'Variant Type', 'enum_policy value map (7..11 -> 0); the 3 MCC-only records are dropped by the layout pairing')
_handled('goof', '/Default Variants', 'Variant Type', 'default variants of MCC-only game types dropped')
_handled('goof', '/Game Engine Settings', 'Setting Category', 'elements with MCC-only categories dropped')
_handled('goof', '/Default Variants/Settings', 'Setting Category', 'elements with MCC-only categories dropped')
_handled('goof', '/Settings', 'Setting Category', 'elements with MCC-only categories dropped')


def _drop_by_enum(ctx, m, v, block, enum_name, label):
    """Drop Vista elements of `block` whose MCC enum `enum_name` is outside the Vista option set."""
    mfs, vfs = _block_fields(m, block), _block_fields(v, block)
    if not mfs or not vfs:
        return 0
    mc = next((c for c in (mfs[0].children or ()) if c.name == enum_name), None)
    vc = next((c for c in (vfs[0].children or ()) if c.name == enum_name), None)
    only = _mcc_only_values(mc, vc)
    if not only or mc is None:
        return 0
    off, size = mc.offset, mc.size

    def pred(e):
        return int.from_bytes(e.data[off:off + size], 'little') in only

    n = _filter_block(m, v, block, pred)
    if n:
        _stat(ctx, 'gp_goof_dropped:%s' % label, n)
    return n


@node_handler('goof', '/Default Variants', prio=110)
def goof_default_variant(ctx, m, v):
    _drop_by_enum(ctx, m, v, 'Settings', 'Setting Category', 'settings')


@node_handler('goof', '', prio=110)
def goof_root(ctx, m, v):
    # 1. (the 9 stock variant records' Variant Type enums are mapped by h2conv/data/enum_policy.json)
    # 2. block elements bound to MCC-only enum values are dropped
    _drop_by_enum(ctx, m, v, 'Game Engine Settings', 'Setting Category', 'engine settings')
    _drop_by_enum(ctx, m, v, 'Default Variants', 'Variant Type', 'default variants')
    _drop_by_enum(ctx, m, v, 'Settings', 'Setting Category', 'record settings')
    # 3. MCC-only variant records (Medic, VIP, Infection @ +0xE4..+0x120) have no Vista fields.  Vista keeps 35
    #    opaque dwords at +0xE4..+0x170 (zero in every stock tag): the generic pass pairs MCC's trailing 20 dwords
    #    (+0x120..+0x170) with the FIRST 20 Vista ones; the plugin sources place them at the SAME offsets -> copy
    #    by offset, zero the rest.
    lo, hi = 0xE4, 0x170
    if len(v.data) >= hi and len(m.data) >= hi:
        v.data[lo:0x120] = b'\0' * (0x120 - lo)
        v.data[0x120:hi] = m.data[0x120:hi]
    mrec = 0
    for off in (0xE4, 0xF8, 0x10C):
        if len(m.data) >= off + 0x14 and any(m.data[off:off + 0x14]) :
            mrec += 1
    if mrec:
        _stat(ctx, 'gp_goof_mcc_only_records_dropped', mrec)
        _warn(ctx, 'gp_goof_records', 'goof: %d MCC-only variant record(s) (Medic/VIP/Infection) dropped' % mrec)


# ------------------------------------------------------------------------------------------------
# mulg / matg: events, indexed lists, damage table, vertex shaders
# ------------------------------------------------------------------------------------------------
_handled('mulg', '/Runtime/General Events', 'Event', "'Pre Game Over' (0x31) events dropped")
_handled('matg', '/Multiplayer Information/General Events', 'Event', "'Pre Game Over' (0x31) events dropped")


def _drop_pre_game_over(ctx, m, v):
    """'Pre Game Over' (MCC event 0x31) has no Vista event.  The enum policy (caveat 1): DROP it when the list already
    holds the 'game over' event (19, announces the same sound -> it would play twice), else keep ONE element remapped to
    19.  Returns the number of dropped elements."""
    mfs, vfs = _block_fields(m, 'General Events'), _block_fields(v, 'General Events')
    if not mfs or not vfs:
        return 0
    ef = next((c for c in (mfs[0].children or ()) if c.name == 'Event'), None)
    mk = m.blocks.get(mfs[0].offset) or []
    vk = v.blocks.get(vfs[0].offset) or []
    if ef is None or len(mk) != len(vk):
        return 0

    def ev(e):
        return struct.unpack_from('<H', e.data, ef.offset)[0]

    bad = set(i for i, e in enumerate(mk) if ev(e) == PRE_GAME_OVER_EVENT)
    if not bad:
        return 0
    has_game_over = any(ev(e) == GAME_OVER_EVENT for i, e in enumerate(mk) if i not in bad)
    keep = []
    remapped = False
    for i, e in enumerate(vk):
        if i in bad:
            if not has_game_over and not remapped:
                vef = _f(e, 'Event')
                _wr(e, vef, GAME_OVER_EVENT)
                remapped = True
                keep.append(e)
                _stat(ctx, 'gp_pre_game_over_remapped')
            continue
        keep.append(e)
    if len(keep) != len(vk):
        _set_kids(v, vfs[0], keep)
        _stat(ctx, 'gp_pre_game_over_dropped', len(vk) - len(keep))
    return len(vk) - len(keep)


@node_handler('mulg', '/Runtime', prio=110)
def mulg_runtime(ctx, m, v):
    _drop_pre_game_over(ctx, m, v)
    # Weapons / Vehicles are indexed by position (gametype 'weapon set' / vehicle set indices, engine enums): MCC
    # appended entries at the END, Vista has no slot for them.
    n = _truncate_block(v, 'Weapons', VISTA_MULG_WEAPONS)
    n2 = _truncate_block(v, 'Vehicles', VISTA_MULG_VEHICLES)
    if n or n2:
        _stat(ctx, 'gp_mulg_list_entries_dropped', n + n2)
    # Vista's 'Null Block' words (count/pointer pairs of blocks the Vista tag has no definition for) must be 0, as for scnr.  The stock MCC mulg
    # has 0 in all of them; the Uncut/DW3 mod's mulg (the first one converted LOCALLY, uncuthud 2026-10-20) carries stale count/pointer pairs
    # (0xC0: 4 / 0x0173DE10, 0xD0, 0xF0, 0xF8) left by the cache builder, which Vista would read as blocks.
    z = 0
    for nf in v.fields:
        if nf.name == 'Null Block' and any(v.data[nf.offset:nf.offset + nf.size]):
            _wr(v, nf, 0)
            z += 1
    if z:
        _stat(ctx, 'gp_mulg_null_block_words_zeroed', z)


_handled('mulg', '/Runtime', 'Weapons/Vehicles counts', 'truncated to the Vista lengths 17 / 11')


@node_handler('matg', '/Multiplayer Information', prio=110)
def matg_mp_info(ctx, m, v):
    _drop_pre_game_over(ctx, m, v)


@node_handler('matg', '/Damage Table', prio=110)
def matg_damage_table(ctx, m, v):
    """MCC lists the damage groups ('', emp, all, melee, ...) where Vista has ('', all, emp, melee, ...): each group
    keeps its own armor multipliers (216/216 residual multiplier diffs in the oracle were exactly this swap)."""
    mfs, vfs = _block_fields(m, 'Damage Groups'), _block_fields(v, 'Damage Groups')
    if not mfs or not vfs:
        return
    mk = m.blocks.get(mfs[0].offset) or []
    vk = v.blocks.get(vfs[0].offset) or []
    if len(mk) != len(vk) or len(vk) < 3:
        return
    nf = _f(mk[0], 'Name')
    if nf is None:
        return
    msids = [struct.unpack_from('<I', e.data, nf.offset)[0] for e in mk]
    try:
        names = [ctx.mcc_string(x) if x else '' for x in msids]
    except Exception:
        return
    if 'emp' in names and 'all' in names:
        i, j = names.index('emp'), names.index('all')
        if i < j:
            # idempotence: only swap while the Vista list still has the MCC order (names compared as Vista string ids)
            vf_name = _f(vk[0], 'Name')
            cur = [struct.unpack_from('<I', e.data, vf_name.offset)[0] for e in vk]
            exp = [ctx.remap_sid(x) if x else 0 for x in msids]
            if cur == exp:
                out = list(vk)
                out[i], out[j] = out[j], out[i]
                _set_kids(v, vfs[0], out)
                _stat(ctx, 'gp_damage_groups_swapped')


@node_handler('matg', '/Rasterizer Data', prio=110)
def matg_rasterizer(ctx, m, v):
    """'Global Vertex Shaders' is an array indexed by an engine enum (stock Vista: 32 slots); MCC appended
    debug_screenspace as slot 32."""
    n = _truncate_block(v, 'Global Vertex Shaders', VISTA_GLOBAL_VERTEX_SHADERS)
    if n:
        _stat(ctx, 'gp_global_vertex_shaders_dropped', n)


_handled('matg', '/Rasterizer Data', 'Global Vertex Shaders count', 'truncated to 32')


@node_handler('matg', '', prio=110)
def matg_root(ctx, m, v):
    # 'Anniversary Globals' (8 skull / birthday party tagrefs) has no Vista data: Vista's slot is a hidden block of
    # element size 0 that must be EMPTY (the generic pass pairs the two by position and would emit 8 empty elements).
    af = _f(v, 'Anniversary Globals') or _fat(v, 'Unknown', 0xD8)
    if af is not None and af.type in BLOCK_TYPES:
        if v.blocks.get(af.offset):
            _stat(ctx, 'gp_matg_anniversary_dropped', len(v.blocks[af.offset]))
        _set_kids(v, af, [])
    _solo_representation_guard(ctx, v)
    _solo_ui_globals_guard(ctx, v)
    _scrub_tree(ctx, v)


# Player Representation element: First Person Hands tagref @0, First Person Body @8, ..., Third Person Unit tagref @0xB0, variant @0xB8 (188 bytes)
_REP_UNIT_DATUM = 0xB4


def _solo_representation_guard(ctx, v):
    """Solo / campaign globals leave Player Representation[2] (spartan) and [3] (elite) NONE in every stock Vista campaign map (and in the
    MCC ones).  Cartographer's multiplayer tag patches (game_globals_prepare_lmao_representation, run from OnMapLoad when game_mode ==
    multiplayer; xlive.dll+0x45100) dereference [spartan].Third Person Unit unconditionally: a campaign-type map picked from the custom
    map list therefore dies in OnMapLoad (C0000005 at xlive.dll+0x445AC, 'Map Active False'; level1, arcology_playground 2026-10-20).
    Elements 2 and 3 are copied from the first populated element (campaign never reads them), so that code reads a real unit."""
    if getattr(ctx, 'map_type', 1) == 1:
        return
    f = _f(v, 'Player Representation')
    els = v.blocks.get(f.offset) if f is not None else None
    if not els or len(els) < 4 or any(len(e.data) < _REP_UNIT_DATUM + 4 for e in els):
        return
    donor = next((e for e in els[:2] if e.u32(_REP_UNIT_DATUM) not in (0, 0xFFFFFFFF)), None)
    if donor is None:
        _warn(ctx, 'gp_matg_no_representation', 'solo globals: no populated Player Representation to copy into [2]/[3]')
        return
    for i in (2, 3):
        if els[i].u32(_REP_UNIT_DATUM) in (0, 0xFFFFFFFF):
            els[i].data[:] = donor.data
            _stat(ctx, 'gp_matg_solo_representation_filled')


# Interface Tags element: Main Menu UI Globals @0x80, Single Player UI Globals @0x88, Multiplayer UI Globals @0x90 (tagref = group word + datum)
_UI_SP, _UI_MP = 0x88, 0x90


def _solo_ui_globals_guard(ctx, v):
    """Same family as the representation guard: a campaign matg leaves Interface Tags[0].Multiplayer UI Globals NONE (stock Vista and MCC alike).
    Cartographer injects 'multiplayer\\game_variant_settings\\multiplayer_game_settings' on every map load and stores it in
    tag_get_fast(Interface Tags[0].Multiplayer UI Globals)+0x10/+0x14 (xlive.dll+0xAD962..0xAD96D, a WRITE); tag_get_fast(NONE) is a stray
    heap address -> C0000005 at xlive.dll+0xACD6A, 'Map Active False' (m1_assault, 01a_tutorial_coop 2026-10-20).  The Single Player UI Globals tag
    has the same wigl layout (32 bytes), so the multiplayer slot is pointed at it; a solo map never opens the multiplayer variant UI."""
    if getattr(ctx, 'map_type', 1) == 1:
        return
    f = _f(v, 'Interface Tags')
    els = v.blocks.get(f.offset) if f is not None else None
    if not els or len(els[0].data) < _UI_MP + 8:
        return
    e = els[0]
    if e.u32(_UI_MP + 4) not in (0, 0xFFFFFFFF):
        return
    if e.u32(_UI_SP + 4) in (0, 0xFFFFFFFF):
        _warn(ctx, 'gp_matg_no_ui_globals', 'solo globals: no Single Player UI Globals to copy into the Multiplayer UI Globals slot')
        return
    e.data[_UI_MP:_UI_MP + 8] = e.data[_UI_SP:_UI_SP + 8]
    _stat(ctx, 'gp_matg_solo_ui_globals_filled')


# ------------------------------------------------------------------------------------------------
# nhdt: MCC-only anchors
# ------------------------------------------------------------------------------------------------
for _p in ('/Bitmap Widgets', '/Text Widgets', '/Screen Effect Widgets'):
    _handled('nhdt', _p, 'Anchor', 'MCC anchors 6..9 (top/bottom/left/right centre) -> the enum policy value (0); warned')


def _nhdt_widget(ctx, m, v):
    """The value mapping (6..9 -> 0, Health And Shield) belongs to h2conv/data/enum_policy.json and is applied by the generic
    pass; this handler only reports a widget that used an MCC-only anchor (0 uses in 301 stock nhdt tags)."""
    mf, vf = _f(m, 'Anchor'), _f(v, 'Anchor')
    if mf is None or vf is None:
        return
    val = _rd(m, mf)
    if val in _mcc_only_values(mf, vf):
        _stat(ctx, 'gp_nhdt_anchor_downgraded')
        _warn(ctx, 'gp_nhdt_anchor', 'nhdt widget anchor %d has no Vista equivalent -> %d (position not adjusted)'
              % (val, _rd(v, vf)))


for _p in ('/Bitmap Widgets', '/Text Widgets', '/Screen Effect Widgets'):
    node_handler('nhdt', _p, prio=110)(_nhdt_widget)


# ------------------------------------------------------------------------------------------------
# sily: parameter enum
# ------------------------------------------------------------------------------------------------
_handled('sily', '', 'Parameter', 'values 0x70..0x9D are MCC-only: tag is unrepresentable (planner should drop it); enum_policy value kept')


def unrepresentable_tag(group, node):
    """Reason string when an MCC tag (decoded Node with MCC layout) can not exist in a Vista map, else None.
    The planner should drop such a tag (no Vista UI/engine code can reach it, and a Parameter outside the Vista
    enum would index past the engine's per-parameter table).  Pure function."""
    if group == 'sily':
        f = _f(node, 'Parameter')
        if f is not None and _rd(node, f) > SILY_VISTA_MAX_PARAMETER:
            return 'sily Parameter 0x%X is an MCC-only game-engine setting' % _rd(node, f)
    return None


@node_handler('sily', '', prio=110)
def sily_root(ctx, m, v):
    """Value mapping (0x70..0x9D -> 111, a Vista no-op placeholder) comes from h2conv/data/enum_policy.json; the planner should
    have dropped the tag (two sily tags with the same Parameter would collide in the engine's per-parameter table)."""
    why = unrepresentable_tag('sily', m)
    if why:
        _stat(ctx, 'gp_sily_mcc_only_parameter')
        _warn(ctx, 'gp_sily_parameter', '%s: converted with Parameter %d; the planner should drop this tag'
              % (why, _rd(v, _f(v, 'Parameter'))))


# ------------------------------------------------------------------------------------------------
# scnr (non-script parts)
# ------------------------------------------------------------------------------------------------
_handled('scnr', '', 'Flags', "bit 8 'Do Not Apply Bungie MP Tag Patches' cleared")
_handled('scnr', '/Scenery', 'Valid Multiplayer Games',
         'MCC-only bits cleared; placements valid ONLY for MCC-only games get Placement Flags bit 0 (Not Automatically)')
for _p in ('/Player Starting Locations', '/Netgame Equipment', '/Starting Equipment'):
    for _i in range(1, 5):
        _handled('scnr', _p, 'Game Type %d' % _i, 'MCC-only game types removed, slots compacted')
_handled('scnr', '/Spawn Data/Dynamic Spawn Overloads', 'Overload Type', "'Medic' (12) overloads dropped")
_handled('scnr', '/Spawn Data/Static Respawn Zones', 'Relevant Games', 'MCC-only bits cleared; MCC-only-games zones bound to Race (never used)')
_handled('scnr', '/Spawn Data/Static Initial Spawn Zones', 'Relevant Games', 'MCC-only bits cleared; MCC-only-games zones bound to Race (never used)')


@node_handler('scnr', '/Scenery', prio=110)
def scnr_scenery(ctx, m, v):
    vf, mf = _f(v, 'Valid Multiplayer Games'), _f(m, 'Valid Multiplayer Games')
    if vf is None or mf is None:
        return
    mask = _rd(m, mf)
    only_bits = _mcc_only_values(mf, vf)
    only_mask = 0
    for b in only_bits:
        only_mask |= 1 << b
    if not (mask & only_mask):
        return
    cleaned = mask & ~only_mask
    _wr(v, vf, cleaned)
    _stat(ctx, 'gp_scenery_game_mask_cleaned')
    if cleaned == 0:
        # mask 0 means "every game" in Vista (95/100 stock placements): an object that was ONLY for MCC-only games
        # must not turn into an always-present one -> never place it automatically.
        pf = _f(v, 'Placement Flags')
        if pf is not None:
            _wr(v, pf, _rd(v, pf) | NOT_AUTOMATICALLY)
            _stat(ctx, 'gp_scenery_exclusive_to_mcc_games_disabled')
            _warn(ctx, 'gp_scenery_mcc_game_only',
                  'scenery placement valid only for MCC-only games (mask 0x%X): set Not Automatically' % mask)


def _compact_game_types(ctx, m, v):
    mf1 = _f(m, 'Game Type 1')
    vf1 = _f(v, 'Game Type 1')
    if mf1 is None or vf1 is None:
        return
    only = _mcc_only_values(mf1, vf1)
    if not only:
        return
    mfs = [_f(m, 'Game Type %d' % i) for i in range(1, 5)]
    vfs = [_f(v, 'Game Type %d' % i) for i in range(1, 5)]
    if any(f is None for f in mfs + vfs):
        return
    vals = [_rd(m, f) for f in mfs]
    if not any(x in only for x in vals):
        return
    kept = [x for x in vals if x not in only and x != 0]
    kept += [0] * (4 - len(kept))
    for f, x in zip(vfs, kept):
        _wr(v, f, x)
    _stat(ctx, 'gp_gametype_slots_cleaned')
    if not any(kept):
        _stat(ctx, 'gp_gametype_slots_now_empty')


for _p in ('/Player Starting Locations', '/Netgame Equipment', '/Starting Equipment'):
    node_handler('scnr', _p, prio=110)(_compact_game_types)


def _zone_games(ctx, m, v):
    """Static spawn zones: Relevant Games has the MCC-only bits Medic/VIP/Infection (8..10).  Engine tooltip
    (scenario spawn zones, Cartographer source game_engine_spawning.h): 'leaving all games empty means all games' - so a
    zone whose mask was ONLY MCC-only games must not become an all-games zone: it is bound to Race (bit 4), a game
    type Vista cannot start (retail Vista has no Race / Headhunter variants), i.e. it is never used."""
    mf, vf = _f(m, 'Relevant Games'), _f(v, 'Relevant Games')
    if mf is None or vf is None:
        return
    mask = _rd(m, mf)
    only_mask = 0
    for b in _mcc_only_values(mf, vf):
        only_mask |= 1 << b
    if not (mask & only_mask):
        return
    cleaned = mask & ~only_mask
    if cleaned == 0:
        cleaned = ZONE_NEVER_USED_MASK
        _stat(ctx, 'gp_spawn_zone_exclusive_to_mcc_games_disabled')
        _warn(ctx, 'gp_spawn_zone_mcc_game_only', 'spawn zone relevant only to MCC-only games (mask 0x%X): bound to Race (never used)' % mask)
    _wr(v, vf, cleaned)
    _stat(ctx, 'gp_option_downgraded:scnr|Relevant Games')


for _p in ('/Spawn Data/Static Respawn Zones', '/Spawn Data/Static Initial Spawn Zones'):
    node_handler('scnr', _p, prio=110)(_zone_games)


# Starting Equipment element (0x9C bytes): Flags @0, Game Type 1..4 (enum16) @4/@6/@8/@0xA, Item Collection 1..6 tagrefs @0x3C + 8*k
_SE_SIZE, _SE_ITMC = 0x9C, 0x3C
_SE_GAME_TYPE_ALL = 0x0C        # Game Type 1 of the stock multiplayer element (zanzibar, lockout ...): applies to every game type


@node_handler('scnr', '', prio=130)
def scnr_solo_starting_equipment(ctx, m, v):
    """A solo scenario takes its loadout from Player Starting Profile (campaign only).  Launched as a multiplayer game the engine builds the
    starting weapons from Starting Equipment[0].Item Collection 1/2 ('map default' primary / secondary, halo2.exe sub_45D5xx); campaign scenarios
    leave the block empty, so players spawn unarmed.  The planner added the Vista item collections that wrap the profile's own primary / secondary
    weapon (plan.notes['solo_starting_itmc']); they become Item Collection 1 / 2 of one element.  A block the author filled is left alone."""
    if getattr(ctx, 'map_type', 1) == 1:
        return
    plan = getattr(ctx, 'plan', None)
    picks = (getattr(plan, 'notes', None) or {}).get('solo_starting_itmc')
    if not picks:
        return
    from .. import donor as D
    f = _f(v, 'Starting Equipment')
    if f is None:
        return
    kids = v.blocks.get(f.offset) or []
    if kids and any(struct.unpack_from('<I', k.data, _SE_ITMC + 8 * i + 4)[0] not in (0, 0xFFFFFFFF) for k in kids for i in range(6)):
        return
    data = bytearray(_SE_SIZE)
    struct.pack_into('<H', data, 4, _SE_GAME_TYPE_ALL)
    n = 0
    for i in range(6):
        dat = 0xFFFFFFFF
        if i < len(picks):
            dat = D.out_datum(ctx, 'itmc', picks[i])
            if dat != 0xFFFFFFFF:
                n += 1
        struct.pack_into('<4sI', data, _SE_ITMC + 8 * i, b'cmti', dat)
    if not n:
        return
    el = T.Node(v.group, T.child_path(v.path, f), f.children, _SE_SIZE, data)
    _set_kids(v, f, [el])
    _stat(ctx, 'gp_solo_starting_equipment', n)


@node_handler('scnr', '/Spawn Data', prio=110)
def scnr_spawn_data(ctx, m, v):
    mfs, vfs = _block_fields(m, 'Dynamic Spawn Overloads'), _block_fields(v, 'Dynamic Spawn Overloads')
    if not mfs or not vfs:
        return
    mc = next((c for c in (mfs[0].children or ()) if c.name == 'Overload Type'), None)
    vc = next((c for c in (vfs[0].children or ()) if c.name == 'Overload Type'), None)
    only = _mcc_only_values(mc, vc)
    if not only:
        return
    off = mc.offset
    n = _filter_block(m, v, 'Dynamic Spawn Overloads',
                      lambda e: struct.unpack_from('<H', e.data, off)[0] in only)
    if n:
        _stat(ctx, 'gp_spawn_overloads_dropped', n)


@node_handler('scnr', '', prio=110)
def scnr_root(ctx, m, v):
    _clean(ctx, m, v, 'Flags', offset=0x12, label='scnr|Flags')
    # Scavenger Hunt Objects (campaign skulls) have no Vista field; Vista's two 'Null Block' words must be 0.
    sf = _f(m, 'Scavenger Hunt Objects')
    if sf is not None:
        k = len(m.blocks.get(sf.offset) or [])
        if k:
            _stat(ctx, 'gp_scavenger_hunt_dropped', k)
    for nf_off in (0x288, 0x28C):
        nf = _fat(v, 'Null Block', nf_off)
        if nf is not None:
            _wr(v, nf, 0)
    # cache-builder lists: unresolved entries do not exist in Vista maps
    n = _drop_null_refs(v, 'Simulation Definition Table', 'Tag')
    if n:
        _stat(ctx, 'gp_simulation_entries_dropped', n)
    for name in ('Comments', 'Object Names', 'Editor Folders'):
        for bf in _block_fields(v, name):
            for e in v.blocks.get(bf.offset) or []:
                _scrub_ascii(e)
    for bf in _block_fields(v, 'Level Data'):
        for e in v.blocks.get(bf.offset) or []:
            for mb in _block_fields(e, 'Multiplayer'):
                for x in e.blocks.get(mb.offset) or []:
                    _scrub_ascii(x)


# ------------------------------------------------------------------------------------------------
# scnr Simulation Definition Table: SORTED + shared twins
# ------------------------------------------------------------------------------------------------
# halo2.exe sub_5F43BB(datum) is a bsearch (sub_48CAC3, comparator sub_5F4116 = `*a - *b` as int32) over scnr+0x3D8/+0x3DC; the result+1 is
# written in 9 bits by the 12 object-replication encoders (sub_5F4CC0) and the decoder (sub_5F4CE9) returns table[index-1].  A datum the
# search does not find is encoded as 0 = "no definition", and the object-creation decoder (sub_5F3BDD) fails for it.  All 40 stock Vista
# maps with a table have it sorted ascending, the MCC order is arbitrary: the converted custom
# map had 243 of 280 datums unfindable.  Also, objects created from SHARED tags (matg player representations, mulg runtime weapons /
# vehicles, grenades ...) use the SHARED datum; when the map carries a LOCAL copy of the same (group, name) the MCC table lists only the
# local datum (91 of them in the custom map), so the shared twin is added.  Duplicates (the Vista tool keeps them: 108 of 367 in headlong)
# are kept unless the table would exceed the 9-bit limit.
SIM_TABLE_MAX_ENTRIES = 508      # 9-bit index+1 <= 511, minus the <= 3 entries Cartographer appends at load (game_globals.cpp)
LOCAL_INDEX_LIMIT = 10000


def _s32(x):
    return x - (1 << 32) if x & 0x80000000 else x


def sorted_simulation_datums(datums, twin_datums=(), unique=False):
    """The table content, ascending as signed int32 (the engine's comparator): the non-null datums (the MCC multiplicity is kept, as the
    Vista tool keeps duplicates too - 108 of the 367 entries of stock headlong - unless unique=True) plus each shared twin once."""
    ds = [int(d) & 0xFFFFFFFF for d in datums if d is not None]
    ds = [d for d in ds if d != NULL]
    present = set(ds)
    extra = [t for t in dict.fromkeys(int(x) & 0xFFFFFFFF for x in twin_datums) if t != NULL and t not in present]
    out = ds + extra
    if unique:
        out = list(dict.fromkeys(out))
    return sorted(out, key=_s32)


def engine_sim_lookup(table, datum):
    """halo2.exe sub_48CAC3 + sub_5F4116 emulated: index of `datum` in `table` or -1 (what sub_5F43BB returns)."""
    base, n = 0, len(table)
    if n == 0:
        return -1
    while True:
        mid = base + (n >> 1)
        diff = _s32(datum) - _s32(table[mid])
        diff = (diff + (1 << 31)) % (1 << 32) - (1 << 31)         # int32 wrap-around of `*a - *b`
        if diff == 0:
            return mid
        if diff > 0:
            base = mid + 1
            n -= 1
        n >>= 1
        if n == 0:
            return -1


def _sim_twin_maps(ctx):
    """({local out_datum: (group, lower name)}, {(group, lower name): [shared placeholder datum]}) of the plan (cached)."""
    ex = getattr(ctx, 'extra', None)
    if ex is not None and 'gp_sim_twin_maps' in ex:
        return ex['gp_sim_twin_maps']
    plan = getattr(ctx, 'plan', None)
    local, shared = {}, {}
    for e in getattr(plan, 'entries', ()) or ():
        if getattr(e, 'action', None) in ('convert', 'donor') and getattr(e, 'name', None) and \
                getattr(e, 'out_datum', NULL) not in (None, NULL):
            local[e.out_datum] = (e.group, e.name.lower())
    for t in getattr(plan, 'shared_placeholders', ()) or ():
        if getattr(t, 'name', None) and getattr(t, 'datum', NULL) not in (None, NULL):
            shared.setdefault((t.group, t.name.lower()), []).append(t.datum)
    if ex is not None:
        ex['gp_sim_twin_maps'] = (local, shared)
    return local, shared


@node_handler('scnr', '', prio=120)
def scnr_simulation_table(ctx, m, v):
    for vf in _block_fields(v, 'Simulation Definition Table'):
        kids = v.blocks.get(vf.offset) or []
        if not kids:
            continue
        rf = next((c for c in (vf.children or ()) if c.name == 'Tag'), None)
        if rf is None or rf.size != 4:
            continue
        pool = {}
        for e in kids:
            pool.setdefault(struct.unpack_from('<I', e.data, rf.offset)[0], []).append(e)
        datums = [struct.unpack_from('<I', e.data, rf.offset)[0] for e in kids]
        local, shared = _sim_twin_maps(ctx)
        twins = []
        for d in pool:
            info = local.get(d)
            if info is not None:
                twins += [t for t in shared.get(info, ()) if t not in pool]
        final = sorted_simulation_datums(datums, twins)
        if len(final) > SIM_TABLE_MAX_ENTRIES:
            final = sorted_simulation_datums(datums, twins, unique=True)
            _warn(ctx, 'gp_simtable_deduplicated', 'Simulation Definition Table had more than %d entries: duplicates merged (%d left)' % (
                SIM_TABLE_MAX_ENTRIES, len(final)))
        if len(final) > SIM_TABLE_MAX_ENTRIES:
            _warn(ctx, 'gp_simtable_overflow', 'Simulation Definition Table has %d distinct entries; the 9-bit replication index allows %d '
                  "(+ Cartographer's appended ones)" % (len(final), SIM_TABLE_MAX_ENTRIES))
        spread = _s32(final[-1]) - _s32(final[0]) if final else 0
        if spread >= 1 << 31:
            _warn(ctx, 'gp_simtable_comparator_overflow', 'Simulation Definition Table datums span %d (>= 2^31): the engine comparator '
                  '`*a - *b` overflows, a binary search cannot be trusted' % spread)
        new, added = [], 0
        for d in final:
            lst = pool.get(d)
            if lst:
                el = lst.pop(0)
            else:
                el = T.Node(v.group, T.child_path(v.path, vf), vf.children, len(kids[0].data))
                struct.pack_into('<I', el.data, rf.offset, d)
                added += 1
            new.append(el)
        if added:
            _stat(ctx, 'gp_simulation_shared_twins_added', added)
        if len(kids) - (len(new) - added) > 0:
            _stat(ctx, 'gp_simulation_duplicates_dropped', len(kids) - (len(new) - added))
        _set_kids(v, vf, new)
        _stat(ctx, 'gp_simulation_entries', len(new))


# ------------------------------------------------------------------------------------------------
# hudg: button icons / icon bitmap / damage indicator bitmap from the Vista donor
# ------------------------------------------------------------------------------------------------
def _name_index(ctx):
    """{(group, lower name): output datum} from the planner's output entries and the shared placeholders."""
    ex = getattr(ctx, 'extra', None)
    if ex is not None and 'gp_name_index' in ex:
        return ex['gp_name_index']
    idx = {}
    plan = getattr(ctx, 'plan', None)
    if plan is not None:
        for t in getattr(plan, 'shared_placeholders', ()) or ():
            if getattr(t, 'name', None) and getattr(t, 'datum', NULL) != NULL:
                idx[(t.group, t.name.lower())] = t.datum
        for e in getattr(plan, 'entries', ()) or ():
            if getattr(e, 'action', None) in ('convert', 'link', 'donor') and getattr(e, 'name', None) \
                    and getattr(e, 'out_datum', None) not in (None, NULL):
                idx[(e.group, e.name.lower())] = e.out_datum
    if ex is not None:
        ex['gp_name_index'] = idx
    return idx


def out_datum_by_name(ctx, group, name):
    """Output-map datum of the tag (group, name) or 0xFFFFFFFF.  Uses the planner's Plan.remap_name; the test
    OraclePlan's ref_by_name; else the planner's entries / shared placeholders."""
    plan = getattr(ctx, 'plan', None)
    if plan is None or not name:
        return NULL
    fn = getattr(plan, 'remap_name', None) or getattr(plan, 'out_datum_by_name', None)
    if fn is not None:
        r = fn(group, name)
        return NULL if r is None else r
    rb = getattr(plan, 'ref_by_name', None)
    if rb is not None:
        t = rb.get((group, name.lower()))
        return t.datum if t is not None else NULL
    return _name_index(ctx).get((group, name.lower()), NULL)


def _donor_hudg(ctx, name):
    """(VistaCache, VistaTag) of the hudg to take Vista-only values from: same tag name in the Vista map type that
    matches the output map type first, then the other Vista maps; any hudg of that map when the name is unknown."""
    vref = getattr(ctx, 'vista', None)
    if vref is None:
        return None, None
    mt = getattr(ctx, 'map_type', 1)
    order = {1: ('shared', 'mainmenu', 'single_player_shared'), 0: ('single_player_shared', 'shared', 'mainmenu'),
             2: ('mainmenu', 'shared', 'single_player_shared')}.get(mt, ('shared', 'mainmenu', 'single_player_shared'))
    want = (name or '').lower()
    first_any = None
    for mp in order:
        try:
            c = vref.open(mp + '.map')
        except Exception:
            continue
        for t in c.tags:
            if t.group == 'hudg' and t.addr and not t.is_empty:
                if want and (t.name or '').lower() == want:
                    return c, t
                if first_any is None:
                    first_any = (c, t)
    return first_any if first_any else (None, None)


def _donor_ref_to_output(ctx, vcache, group_word, datum):
    """Map a tag reference of a Vista donor tag into the OUTPUT map's datum space (by tag name)."""
    if datum == NULL:
        return NULL
    plan = getattr(ctx, 'plan', None)
    fn = getattr(plan, 'remap_vista_datum', None)
    if fn is not None:
        d = fn(vcache, datum)
        if d != NULL:
            return d
    try:
        t = vcache.tags[datum & 0xFFFF]
    except Exception:
        return NULL
    d = out_datum_by_name(ctx, t.group, t.name)
    if d != NULL:
        return d
    # shared.map's datums are valid in every multiplayer output map (shared placeholders keep them)
    if vcache is getattr(getattr(ctx, 'vista', None), 'shared', None) and getattr(ctx, 'map_type', 1) == 1:
        return datum
    return NULL


@node_handler('hudg', '', prio=110)
def hudg_root(ctx, m, v):
    _scrub_tree(ctx, v)
    # DEFAULT ON since 2026-10-20 (user: "turn training off by default for all conversions"): empty the LOCAL hudg's Player Training
    # Data.  The game's tutorial prompts ('hold X to ...') draw a frame around the text; on converted maps they showed as an empty
    # square (2026-10-20, chronopolis; the training-less build fixed it in game).  Nothing in the map data differs from Vista's
    # (docs/FACTS [hudmsg3]), so this is a workaround, not a fix.  Keep the prompts with opts['keep_training'] (convert
    # --keep-training) or env H2CONV_KEEP_TRAINING=1.  A map with LINKED globals uses Vista's own hudg, which cannot be changed here.
    _o = getattr(ctx, 'opts', None) or {}
    if not (_o.get('keep_training') or os.environ.get('H2CONV_KEEP_TRAINING')):
        _tf = _f(v, 'Player Training Data')
        if _tf is not None and (v.blocks.get(_tf.offset) or []):
            _n = len(v.blocks.get(_tf.offset) or [])
            _set_kids(v, _tf, [])
            _stat(ctx, 'gp_hudg_training_entries_removed', _n)
            _warn(ctx, 'gp_hudg_training_disabled', 'hudg Player Training Data emptied (%d entries; the game shows no tutorial prompts; '
                                                    '--keep-training keeps them)' % _n)
    icon = _f(v, 'Icon Bitmap')
    dmg = _f(v, 'Damage Indicator Bitmap')
    btn = _f(v, 'Button Icons')
    if icon is None or dmg is None or btn is None:
        return
    need_icon = _ref_is_null(v, icon)
    need_dmg = _ref_is_null(v, dmg)
    need_btn = not (v.blocks.get(btn.offset) or [])
    if not (need_icon or need_dmg or need_btn):
        return
    entry = getattr(ctx, 'cur_entry', None)
    c, dt = _donor_hudg(ctx, getattr(entry, 'name', None) or 'ui\\hud\\default')
    if c is None:
        _warn(ctx, 'gp_hudg_no_donor', 'hudg: no Vista donor hudg found; button icons / icon bitmap left empty')
        return
    donor = ctx.decode_vista(c, dt)
    if _f(donor, 'Icon Bitmap') is None or _f(donor, 'Damage Indicator Bitmap') is None or _f(donor, 'Button Icons') is None:
        _warn(ctx, 'gp_hudg_no_donor', 'hudg: donor %s has an unexpected layout' % (getattr(dt, 'name', None),))
        return
    if need_icon:
        gd = _f(donor, 'Icon Bitmap')
        g, d = struct.unpack_from('<II', donor.data, gd.offset)
        nd = _donor_ref_to_output(ctx, c, g, d)
        struct.pack_into('<II', v.data, icon.offset, g if nd != NULL else NULL, nd)
        if nd == NULL:
            _warn(ctx, 'gp_hudg_donor_ref', 'hudg Icon Bitmap donor bitmap not present in the output map')
        _stat(ctx, 'gp_hudg_icon_bitmap_from_donor')
    if need_dmg:
        gd = _f(donor, 'Damage Indicator Bitmap')
        g, d = struct.unpack_from('<II', donor.data, gd.offset)
        nd = _donor_ref_to_output(ctx, c, g, d)
        struct.pack_into('<II', v.data, dmg.offset, g if nd != NULL else NULL, nd)
        if nd == NULL:
            _warn(ctx, 'gp_hudg_donor_ref', 'hudg Damage Indicator Bitmap donor bitmap not present in the output map')
        _stat(ctx, 'gp_hudg_damage_bitmap_from_donor')
    if need_btn:
        bd = _f(donor, 'Button Icons')
        kids = [_clone_node(e) for e in (donor.blocks.get(bd.offset) or [])]
        _set_kids(v, btn, kids)
        _stat(ctx, 'gp_hudg_button_icons_from_donor', len(kids))


_handled('hudg', '', 'Button Icons / Icon Bitmap / Damage Indicator Bitmap', 'empty in MCC -> Vista donor values')


def selftest():
    """Offline check (no map IO): the handler registry and the pure helpers."""
    from . import NODE
    need = [('char', '/General Properties'), ('goof', ''), ('scnr', '/Scenery'), ('matg', '/Damage Table'),
            ('mulg', '/Runtime'), ('hudg', ''), ('sily', ''), ('weap', '/Barrels'), ('proj', '')]
    assert all(k in NODE for k in need), [k for k in need if k not in NODE]
    assert len(HANDLED_OPTION_DIFFS) >= 45
    return True


if __name__ == '__main__':
    print('selftest', selftest())
