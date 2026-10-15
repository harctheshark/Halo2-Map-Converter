# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Gameplay handlers, REVERSE direction: Halo 2 Vista (cache v8) -> Halo 2 MCC (cache v13).

Inverse of the forward handlers/gameplay.py.

The reverse generic pass pairs the two layouts, copies matched fields and remaps ids.  Vista content is (almost) a subset of
what MCC understands, so there is no downgrading; the work is the *upgrade* to what MCC's engine and tools expect:

  * Engine-sorted tables: the scnr Simulation Definition Table (halo2.dll looks datums up by binary search; stock MCC tables
    are sorted ascending as signed int32 in 43/43 maps - r03_simtable.py) and the jmad label blocks (stock MCC: 100% sorted
    by the string id).  Datums and string ids are renumbered by the remap, so both are re-sorted with the MCC ids.
  * Indexed lists whose positions are engine enums and that MCC extended at the END: mulg Runtime Weapons 17 -> 20,
    Vehicles 11 -> 14, matg Global Vertex Shaders 32 -> 33; the new slots are filled from the stock MCC tag of the same name
    (the "donor": shared.map / single_player_shared.map / mainmenu.map, resolved BY NAME into the output) and are NULL
    references when no donor or output tag exists.
  * MCC-only content defaulted: the matg Anniversary Globals block (8 refs), the mulg / matg 'Pre Game Over' event (49),
    char campaign metagame data - all from the donor; empty / zero otherwise.  The three MCC-only goof variant records stay
    zero (they ARE zero in the stock MCC goof).
  * matg Damage Table groups back to MCC's order ('', emp, all, ...).
  * hudg: stock MCC hudg tags carry no Icon Bitmap / Damage Indicator Bitmap and no Button Icons (18/18); Vista's are dropped
    (opts['rev_hudg_keep_vista'] keeps them).

Every handler is idempotent, recomputes from the Vista node, and registers at priority 110 (after the 100-priority handlers
of the other topics).  Handlers receive (ctx, vista_node, mcc_node) - mcc_node is the generic result in the MCC layout.
"""
import struct

from . import node_handler, post_pass
from . import _revmisc_common as C
from ... import tagtree as T

NULL = 0xFFFFFFFF
BLOCK_TYPES = ('tagblock', 'reflexive')

MCC_MULG_WEAPONS = 20                    # stock MCC mulg Runtime Weapons (Vista 17)
MCC_MULG_VEHICLES = 14                   # stock MCC mulg Runtime Vehicles (Vista 11)
MCC_GLOBAL_VERTEX_SHADERS = 33           # stock MCC matg Rasterizer Data Global Vertex Shaders (Vista 32)
MCC_ANNIVERSARY_REFS = 8                 # stock MCC matg Anniversary Globals
PRE_GAME_OVER_EVENT = 0x31
DEBUG_SCREENSPACE_VRTX = 'rasterizer\\vertex_shaders_dx9\\debug_screenspace'
SIM_TABLE_MAX_ENTRIES = 508
# object-definition groups (+ damage effects) that the cache builder lists in the Simulation Definition Table (r13_simtable_rule.py)
SIM_GROUPS = ('weap', 'proj', 'bloc', 'eqip', 'vehi', 'scen', 'ssce', 'bipd', 'garb', 'mach', 'ctrl', 'crea', 'lifi', 'jpt!')


# ------------------------------------------------------------------------------------------------
# small helpers
# ------------------------------------------------------------------------------------------------
def _null_ref_element(parent, field):
    e = C.new_element(parent, field)
    for f in e.fields:
        if f.type == 'tagref':
            e.data[f.offset:f.offset + f.size] = b'\xff' * f.size
    return e


# ------------------------------------------------------------------------------------------------
# char
# ------------------------------------------------------------------------------------------------
@node_handler('char', '', prio=110)
def char_root(ctx, vista_node, mcc_node):
    """MCC adds Fake Empty Morph / Equipment Definitions and Campaign Metagame Bucket (3 root blocks) and the Metagame
    Type / Classification bytes of General Properties; Vista has none of it (its int16 at +6 is 0 in 130/130 stock chars).
    Left empty / zero; a stock MCC char of the same name donates the campaign scoring data."""
    donor, cache = C.mcc_donor(ctx, 'char', C.cur_name(ctx))
    if donor is None:
        return
    gp_m, gp_d = C.blk(mcc_node, 'General Properties'), C.blk(donor, 'General Properties')
    if gp_m and gp_d:
        gp_m[0].data[6:8] = gp_d[0].data[6:8]
        C.stat(ctx, 'rev_gp_char_metagame_from_donor')
    for name in ('Fake Empty Morph Properties', 'Fake Empty Equipment Definitions', 'Campaign Metagame Bucket'):
        mf = mcc_node.field(name)
        df = donor.field(name)
        if mf is None or df is None or C.blk(mcc_node, name):
            continue
        kids = [C.clone_donor_tree(ctx, cache, k) for k in C.blk(donor, name)]
        if kids:
            C.set_block(mcc_node, mf, kids)
            C.stat(ctx, 'rev_gp_char_blocks_from_donor', len(kids))


def _unit_metagame(ctx, vista_node, mcc_node):
    """bipd / crea / vehi: MCC turned Vista's unnamed int16 (0 in every stock Vista unit) into two enum8 fields, Metagame Type
    and Metagame Classification (campaign scoring metadata; non-zero in the stock MCC units: vehi 31/31, bipd 26/28).  Zero
    unless the stock MCC unit of the same name donates them."""
    if mcc_node.group == 'crea':
        # crea Flags is flags32 in Vista, flags16 in MCC (the upper two bytes became the metagame pair); stock Vista values
        # are < 0x100 (r27), so the 4 -> 2 byte resize of the generic pass loses nothing - check it
        vf = vista_node.field('Flags')
        if vf is not None and vf.size == 4 and (struct.unpack_from('<I', vista_node.data, vf.offset)[0] >> 16):
            C.warn(ctx, 'rev_gp_crea_flags_dropped', 'crea Flags has bits above 15 set: dropped')
    donor, _cache = C.mcc_donor(ctx, mcc_node.group, C.cur_name(ctx))
    if donor is None:
        return
    n = 0
    for name in ('Metagame Type', 'Metagame Classification'):
        v = C.rd(donor, name)
        if v is not None and C.wr(mcc_node, name, v):
            n += 1
    if n:
        C.stat(ctx, 'rev_gp_unit_metagame_from_donor')


_unit_metagame.owns_resize = True          # crea Flags flags32 -> flags16 is checked above (generic 'resize_default' warning is noise)
for _g in ('bipd', 'crea', 'vehi'):
    node_handler(_g, '', prio=110)(_unit_metagame)


# ------------------------------------------------------------------------------------------------
# goof
# ------------------------------------------------------------------------------------------------
@node_handler('goof', '', prio=110)
def goof_root(ctx, vista_node, mcc_node):
    """MCC inserts three variant records (Medic, VIP, Infection) at +0xE4..+0x120 - ALL ZERO in the stock MCC goof - and keeps
    Vista's opaque dwords at the same offsets +0x120..+0x170.  Copy those by offset, zero the three records."""
    lo, mid, hi = 0xE4, 0x120, 0x170
    if len(mcc_node.data) >= hi and len(vista_node.data) >= hi:
        mcc_node.data[lo:mid] = b'\0' * (mid - lo)
        mcc_node.data[mid:hi] = vista_node.data[mid:hi]


# ------------------------------------------------------------------------------------------------
# mulg / matg lists
# ------------------------------------------------------------------------------------------------
def _extend_list(ctx, mcc_node, name, target, donor_kids, cache):
    """Grow block `name` of mcc_node to `target` elements (positions are engine enums), filling NULL slots and the new tail
    from the donor list; unresolvable slots stay NULL references.  Returns the number of elements added."""
    f = mcc_node.field(name)
    if f is None or f.children is None:
        return 0
    kids = list(mcc_node.blocks.get(f.offset, []))
    added = 0
    for i in range(target):
        is_new = i >= len(kids)
        if is_new:
            e = _null_ref_element(mcc_node, f)
            kids.append(e)
            added += 1
        else:
            e = kids[i]
            tf = next((x for x in e.fields if x.type == 'tagref'), None)
            if tf is None or struct.unpack_from('<I', e.data, tf.offset + 4)[0] != NULL:
                continue                                  # a real reference stays
        if i < len(donor_kids):
            tf = next((x for x in e.fields if x.type == 'tagref'), None)
            dtf = next((x for x in donor_kids[i].fields if x.type == 'tagref'), None)
            if tf is not None and dtf is not None:
                g, d = struct.unpack_from('<II', donor_kids[i].data, dtf.offset)
                g2, d2 = C.donor_ref(ctx, cache, g, d)
                if d2 != NULL:
                    struct.pack_into('<II', e.data, tf.offset, g2, d2)
                    C.stat(ctx, 'rev_gp_%s_slot_from_donor' % name.lower())
    if added:
        C.set_block(mcc_node, f, kids)
    return added


def _add_pre_game_over(ctx, mcc_node, donor_parent, cache):
    """MCC lists the event 'Pre Game Over' (49) last in General Events; Vista has no such event.  Added from the donor tag
    (its sound re-pointed by name); without a donor nothing is added (the event simply never fires)."""
    f = mcc_node.field('General Events')
    if f is None or f.children is None or donor_parent is None:
        return
    ef = next((c for c in f.children if c.name == 'Event'), None)
    if ef is None:
        return
    kids = list(mcc_node.blocks.get(f.offset, []))
    if any(struct.unpack_from('<H', k.data, ef.offset)[0] == PRE_GAME_OVER_EVENT for k in kids):
        return
    for dk in C.blk(donor_parent, 'General Events'):
        if struct.unpack_from('<H', dk.data, ef.offset)[0] == PRE_GAME_OVER_EVENT:
            kids.append(C.clone_donor_tree(ctx, cache, dk))
            C.set_block(mcc_node, f, kids)
            C.stat(ctx, 'rev_gp_pre_game_over_from_donor')
            return


@node_handler('mulg', '/Runtime', prio=110)
def mulg_runtime(ctx, vista_node, mcc_node):
    donor, cache = C.mcc_donor(ctx, 'mulg', C.cur_name(ctx))
    drt = C.blk(donor, 'Runtime') if donor is not None else []
    drt = drt[0] if drt else None
    dw = C.blk(drt, 'Weapons') if drt is not None else []
    dv = C.blk(drt, 'Vehicles') if drt is not None else []
    n = _extend_list(ctx, mcc_node, 'Weapons', MCC_MULG_WEAPONS, dw, cache)
    n += _extend_list(ctx, mcc_node, 'Vehicles', MCC_MULG_VEHICLES, dv, cache)
    if n:
        C.stat(ctx, 'rev_gp_mulg_list_entries_added', n)
    _add_pre_game_over(ctx, mcc_node, drt, cache)


@node_handler('matg', '/Multiplayer Information', prio=110)
def matg_mp_info(ctx, vista_node, mcc_node):
    donor, cache = C.mcc_donor(ctx, 'matg', C.cur_name(ctx))
    di = C.blk(donor, 'Multiplayer Information') if donor is not None else []
    _add_pre_game_over(ctx, mcc_node, di[0] if di else None, cache)


@node_handler('matg', '/Damage Table', prio=110)
def matg_damage_table(ctx, vista_node, mcc_node):
    """Vista lists the damage groups ('', all, emp, melee, ...), MCC ('', emp, all, melee, ...): every group keeps its own armor
    multipliers, so the two elements swap places."""
    vf = vista_node.field('Damage Groups')
    mf = mcc_node.field('Damage Groups')
    if vf is None or mf is None:
        return
    vk = vista_node.blocks.get(vf.offset) or []
    mk = list(mcc_node.blocks.get(mf.offset) or [])
    if len(vk) != len(mk) or len(mk) < 3:
        return
    nf = next((c for c in mf.children if c.name == 'Name'), None)
    if nf is None:
        return
    vnf = next((c for c in vf.children if c.name == 'Name'), None)
    try:
        names = [C.src_string(ctx, struct.unpack_from('<I', e.data, vnf.offset)[0]) for e in vk]
    except Exception:  # noqa: BLE001
        return
    if 'emp' in names and 'all' in names:
        i, j = names.index('all'), names.index('emp')
        if i < j:
            exp = [ctx.remap_sid(struct.unpack_from('<I', e.data, vnf.offset)[0]) if struct.unpack_from('<I', e.data, vnf.offset)[0] else 0 for e in vk]
            cur = [struct.unpack_from('<I', e.data, nf.offset)[0] for e in mk]
            if cur == exp:                       # idempotence: only while the list still has the Vista order
                mk[i], mk[j] = mk[j], mk[i]
                C.set_block(mcc_node, mf, mk)
                C.stat(ctx, 'rev_gp_damage_groups_swapped')


@node_handler('matg', '/Rasterizer Data', prio=110)
def matg_rasterizer(ctx, vista_node, mcc_node):
    """'Global Vertex Shaders' is indexed by an engine enum; MCC appended debug_screenspace as slot 32."""
    f = mcc_node.field('Global Vertex Shaders')
    if f is None or f.children is None:
        return
    kids = list(mcc_node.blocks.get(f.offset, []))
    if len(kids) >= MCC_GLOBAL_VERTEX_SHADERS or not kids:
        return
    while len(kids) < MCC_GLOBAL_VERTEX_SHADERS:
        e = _null_ref_element(mcc_node, f)
        tf = next(x for x in e.fields if x.type == 'tagref')
        d = C.remap_name(ctx, 'vrtx', DEBUG_SCREENSPACE_VRTX)
        if d != NULL:
            struct.pack_into('<II', e.data, tf.offset, C.fcc('vrtx'), d)
            C.stat(ctx, 'rev_gp_debug_screenspace_resolved')
        else:
            # a NULL entry in a table the renderer walks at init could be dereferenced: duplicate the last real slot instead
            e.data[:] = kids[-1].data
            C.warn(ctx, 'rev_gp_debug_screenspace', 'matg Global Vertex Shaders[%d] (debug_screenspace) not in the output map; '
                   'the previous slot is repeated' % len(kids))
        kids.append(e)
    C.set_block(mcc_node, f, kids)


@node_handler('matg', '', prio=110)
def matg_root(ctx, vista_node, mcc_node):
    """Anniversary Globals (8 refs: the scarab hand gun + 7 remastered sounds): Vista's slot is a hidden element-size-0
    block.  Filled from the donor matg (by name); eight NULL references otherwise (indexed access stays in bounds)."""
    af = mcc_node.field('Anniversary Globals')
    if af is None or af.children is None:
        return
    donor, cache = C.mcc_donor(ctx, 'matg', C.cur_name(ctx))
    kids = []
    if donor is not None:
        kids = [C.clone_donor_tree(ctx, cache, k) for k in C.blk(donor, 'Anniversary Globals')]
        if kids:
            C.stat(ctx, 'rev_gp_anniversary_from_donor', len(kids))
    if not kids:
        kids = [_null_ref_element(mcc_node, af) for _ in range(MCC_ANNIVERSARY_REFS)]
        C.stat(ctx, 'rev_gp_anniversary_null_refs')
    C.set_block(mcc_node, af, kids)


# ------------------------------------------------------------------------------------------------
# hudg
# ------------------------------------------------------------------------------------------------
@node_handler('hudg', '', prio=110)
def hudg_root(ctx, vista_node, mcc_node):
    """Stock MCC ui\\hud\\default has Icon Bitmap + Damage Indicator Bitmap null and Button Icons empty in 18/18 maps."""
    if C.opt(ctx, 'rev_hudg_keep_vista', False):
        return
    for name in ('Icon Bitmap', 'Damage Indicator Bitmap'):
        f = mcc_node.field(name)
        if f is not None and f.type == 'tagref' and f.size == 8:
            g = struct.unpack_from('<I', mcc_node.data, f.offset)[0]
            struct.pack_into('<II', mcc_node.data, f.offset, g, NULL)
    bf = mcc_node.field('Button Icons')
    if bf is not None and bf.children is not None and mcc_node.blocks.get(bf.offset):
        C.set_block(mcc_node, bf, [])


# ------------------------------------------------------------------------------------------------
# engine-sorted tables
# ------------------------------------------------------------------------------------------------
_JMAD_SORTED = {
    '': ('Modes', 'Weapon List', 'Vehicle Suspension', 'Object Overlays'),
    '/Modes': ('Weapon Class',),
    '/Modes/Weapon Class': ('Weapon Type',),
    '/Modes/Weapon Class/Weapon Type': ('Actions', 'Death and Damage', 'Overlays', 'Transitions'),
}
_I32 = struct.Struct('<i')


def sort_block_elements(kids):
    """Elements ascending by the signed int32 in their first dword (stable); `kids` itself when already sorted."""
    keys = [_I32.unpack_from(k.data, 0)[0] for k in kids]
    if all(keys[i] <= keys[i + 1] for i in range(len(keys) - 1)):
        return kids
    return [kids[i] for i in sorted(range(len(kids)), key=keys.__getitem__)]


def _make_jmad_sorter(path):
    names = _JMAD_SORTED[path]

    def jmad_sort(ctx, vista_node, mcc_node):
        for nm in names:
            f = mcc_node.field(nm)
            if f is None or f.children is None:
                continue
            kids = mcc_node.blocks.get(f.offset)
            if not kids or len(kids) < 2:
                continue
            out = sort_block_elements(kids)
            if out is not kids:
                mcc_node.blocks[f.offset] = out
                C.stat(ctx, 'rev_gp_jmad_sorted_blocks')
    jmad_sort.__name__ = 'jmad_sort_%s' % (path.replace('/', '_').replace(' ', '') or 'root')
    return jmad_sort


for _p in _JMAD_SORTED:
    node_handler('jmad', _p, prio=150)(_make_jmad_sorter(_p))


# ------------------------------------------------------------------------------------------------
# scnr: Simulation Definition Table
# ------------------------------------------------------------------------------------------------
def _fg():
    from ...handlers import gameplay as FG            # forward module: pure helpers sorted_simulation_datums / _sim_twin_maps
    return FG


def _sim_field(node):
    for vf in C.block_fields(node, 'Simulation Definition Table'):
        rf = next((c for c in (vf.children or ()) if c.name == 'Tag'), None)
        if rf is not None and rf.size == 4:
            return vf, rf
    return None, None


def _write_sim_table(ctx, mcc_node, vf, rf, final):
    kids = mcc_node.blocks.get(vf.offset) or []
    pool = {}
    for e in kids:
        pool.setdefault(struct.unpack_from('<I', e.data, rf.offset)[0], []).append(e)
    esize = T.element_size(vf)
    new = []
    for d in final:
        lst = pool.get(d)
        if lst:
            el = lst.pop(0)
        else:
            el = T.Node(mcc_node.group, T.child_path(mcc_node.path, vf), vf.children, esize)
            struct.pack_into('<I', el.data, rf.offset, d)
        new.append(el)
    C.set_block(mcc_node, vf, new)


@node_handler('scnr', '', prio=120)
def scnr_simulation_table(ctx, vista_node, mcc_node):
    """Stock MCC tables are sorted ascending as signed int32 (43/43 maps, duplicates kept).  The remap changed every datum: drop
    the entries whose tag does not exist in the output, add the shared twin of every local entry, sort."""
    FG = _fg()
    vf, rf = _sim_field(mcc_node)
    if vf is None:
        return
    kids = mcc_node.blocks.get(vf.offset) or []
    if not kids:
        return
    datums = [struct.unpack_from('<I', e.data, rf.offset)[0] for e in kids]
    pool = set(datums)
    local, shared = FG._sim_twin_maps(ctx)
    twins = []
    for d in pool:
        info = local.get(d)
        if info is not None:
            twins += [t for t in shared.get(info, ()) if t not in pool]
    final = FG.sorted_simulation_datums(datums, twins)
    if len(final) > SIM_TABLE_MAX_ENTRIES:
        final = FG.sorted_simulation_datums(datums, twins, unique=True)
        C.warn(ctx, 'rev_gp_simtable_deduplicated', 'Simulation Definition Table had more than %d entries: duplicates merged (%d left)' % (
            SIM_TABLE_MAX_ENTRIES, len(final)))
    if len(final) > SIM_TABLE_MAX_ENTRIES:
        C.warn(ctx, 'rev_gp_simtable_overflow', 'Simulation Definition Table has %d distinct entries (limit %d)' % (len(final), SIM_TABLE_MAX_ENTRIES))
    _write_sim_table(ctx, mcc_node, vf, rf, final)
    C.stat(ctx, 'rev_gp_simulation_entries', len(final))


@post_pass(prio=220)
def scnr_simulation_closure(ctx, converted):
    """Every object-definition tag (and damage effect) of the OUTPUT map must be findable by the engine's binary search, also
    those the Vista table did not list because the planner added them (MCC-only objects, donors).  Entries are added to the
    already sorted table; the table stays sorted."""
    plan = getattr(ctx, 'plan', None)
    entries = getattr(plan, 'entries', None)
    if not entries:
        return
    FG = _fg()
    want = []
    for e in entries:
        if getattr(e, 'group', None) in SIM_GROUPS and getattr(e, 'action', None) in ('convert', 'donor', 'mcc_donor', 'link'):
            d = getattr(e, 'out_datum', None)
            if d not in (None, NULL):
                want.append(d)
    for node in converted.values():
        if node is None or node.group != 'scnr':
            continue
        vf, rf = _sim_field(node)
        if vf is None:
            continue
        kids = node.blocks.get(vf.offset) or []
        have = [struct.unpack_from('<I', e.data, rf.offset)[0] for e in kids]
        missing = [d for d in dict.fromkeys(want) if d not in set(have)]
        if not missing:
            continue
        if len(have) + len(missing) > SIM_TABLE_MAX_ENTRIES:
            C.warn(ctx, 'rev_gp_simtable_closure_skipped', '%d object tags missing from the Simulation Definition Table (%d entries, limit %d)' % (
                len(missing), len(have), SIM_TABLE_MAX_ENTRIES))
            continue
        _write_sim_table(ctx, node, vf, rf, FG.sorted_simulation_datums(have + missing))
        C.stat(ctx, 'rev_gp_simulation_closure_added', len(missing))
