# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Salvage of Xbox tags whose own layout cannot be read: junk block pointers, junk counts, data past the end of the file
(2026-10-26).

Before, one unreadable nested block or dataref killed the whole tag: the converter replaced it by the stock Vista tag of that NAME or set
every reference to it NONE (227 tags in 67 batch maps: Skull_Island's ancient trees / broken arches, Outflod's flood garbage, the
Burnt_Blood creep, the Reconciliation walkway, the soccer ball, Cassiopeia's PLAYER biped model, the H3 spikers, custom weapons,
projectiles, damage effects ...).  The Xbox read those tags without trouble - it either never touches the bad field or reads whatever
lies at the address.  TagSalvage.decode reproduces what the Xbox most likely read, rung by rung, per failing block:

  (i)   shift: an injector moved the whole tag body by a constant S without relocating it (Soccer / Soccermounds soccer_ball (1): hlmt
        +264, mode +672).  Every root pointer lies in [addr + S, addr + size + S) and the first one is S bytes past the root; decoded
        with the reader moved back by S the tag reads strictly (0 bytes differ from the original soccer_ball hlmt: probes).
  (ii)  stale restore: the bad pointer is the ORIGINAL pointer of the stock (campaign) map the tag was extracted from - the extractor
        left a block the cache compiler had shared outside the tag unrelocated.  When a stock Xbox copy of the same name has that exact
        pointer and count at the same block path, its bytes are used (string ids by text, references by name into this map): 23 of 29
        hlmt Collision Regions/Permutations (Skull_Island 0x806AF064 = 05a_deltaapproach, control_walkway 0x806899FC =
        08b_deltacontrol), shaders (Sanctus 0x8073E930 = 07a_highcharity), projectiles (probes).  Also
        through another tag of this map that stores the very same stale pointer at that path and whose stock original has it
        (Sanctus ghost / banshee damage effects: 0x8075609C = 07a_highcharity hunter_particle_cannon_shake).
  (iii) clamp: a count that holds a string id a remap tool wrote over it, or that exceeds the room up to the next block of the tag, is
        cut to that room (Burnt_Blood creep Marker Groups[3]/Markers 0x14002600 = 'passenger_lb_enter01' -> 1, Reconciliation
        control_walkway Compression Info 0x070025D7 = 'walkway' -> 1: both stock originals have 1; probes).
  (iv)  hlmt Collision Regions/Permutations are never left empty: an EMPTY block disables collision for that region (sapien
        sub_6766E0 / sub_6768A0, default xmmword_C5C1B0 = 16 x 0xFF; 0 of 211 stock blocks are empty).  One element is synthesised:
        the render model region's first permutation name (else 'default'), collision / physics permutation 0.
  (v)   zero-fill: a pointer inside the header-declared tag region (tag offset .. tag offset + tag size) but past the end of the file -
        the Xbox loads the declared region, so the part the file lacks reads as zeros (343_Delta_Ruins ghost_bolt / banshee_bomb
        0x80620BE0, the H3 spikers): the declared count / size of zeroed elements (function type 0 = identity; an empty function
        evaluates to 0.0 on Vista, 0x4E79DA, and never occurs in stock: 0 of 15,813 effe emitter functions).  Only a wild pointer (a
        float, a small int, another struct's bytes: Cassiopeia hlmt States 0xFFFF0000) is emptied in place.
An element is never dropped (that shifts every index after it).  The result is validated (node trees: indices in range, no self-loops,
no cycles - Soccer's hlmt as stored has first-child 0 on node 0); a tag that does not validate raises the strict decode's error, and
the converter falls back as before.  Not salvaged at all (corpus scan over the 208 batch maps): bytes that are no definition of the
group (an object whose Object Type names another type: Soccer bloc bm_rock_sm = 36), an index entry whose declared size cannot hold
its root, and a wild pointer in the ROOT struct other than a prediction hint (the main struct itself is damaged: Darkness_mist's
impact effects with Locations 0x42480000 at 0xA, Waterworld's damage effects).  The caller guards aliases: an entry whose bytes another group decodes strictly is not salvaged
(convert_xbox.salvage_tag; data twins).  A salvaged tag must finally be PLAUSIBLE (plausibility_problems: no enum outside its
options, no non-finite float in a named field, no absurd count; decision of 2026-10-27): junk read through a readable but wrong
pointer is not the tag, and the Vista tag of its name stays the fallback (batch_v4 audit R1: 13 float.nonfinite in 7 maps)."""
import math
import re
import struct

from .. import tagtree as T
from ..objtype import OBJECT_TYPE_GROUPS
from .xbox_cache import NULL

CLASSES = ('shift', 'stale_restore', 'count_clamped', 'collision_permutation', 'zero_filled', 'emptied')
UNRELIABLE = ('count_clamped', 'zero_filled', 'emptied')      # the salvaged data is not the tag's own (shader slot-map evidence)
MAX_SHIFT = 0x10000
# plausibility gate (decision of 2026-10-27, batch_v4 audit R1): a block count above this is no Halo 2 block (the engine's block
# indices are int16) - and zero-fill never serves a region larger than ZERO_FILL_MAX bytes or reaching more than ZERO_FILL_SLACK past
# the file end (a junk count with a pointer inside an inflated header region would allocate and decode millions of zero elements)
MAX_BLOCK_COUNT = 0x7FFF
ZERO_FILL_MAX = 0x400000
ZERO_FILL_SLACK = 0x400000
# float32 lanes per plugin field type (validate._FLOAT_LANES)
FLOAT_LANES = {'float32': 1, 'degree': 1, 'point2': 2, 'point3': 3, 'vector2': 2, 'vector3': 3, 'vector4': 4, 'degree2': 2,
               'degree3': 3, 'plane2': 3, 'plane3': 4, 'rangef': 2, 'colorf': 4}
ENUM_TYPES = ('enum8', 'enum16', 'enum32')
# root blocks that may be emptied: prediction hints (the engine works without them)
ROOT_HINTS = ('Predicted Resources', 'Predicted Bitmaps')
# raw pointers into a map's own file: a stock block carrying one cannot be restored into another map
RAW_FIELDS = ('Resource Block Offset', 'LOD1 Offset', 'LOD2 Offset', 'LOD3 Offset', 'File Offset', 'Offset')
COLLISION_PERMUTATIONS = ('hlmt', '/Collision Regions/Permutations')
_CLONE = (re.compile(r' \(\d+\)$'), re.compile(r'_dup\d+$'), re.compile(r'^copy of ', re.I))


class Unsalvageable(Exception):
    pass


def clone_bases(name):
    """The name and its clone bases ('X (1)', 'X_dup2', 'Copy of X' -> 'X'), in that order."""
    out = [name] if name else []
    for rx in _CLONE:
        n = rx.sub('', name or '')
        if n and n not in out:
            out.append(n)
    return out


def tree_problems(root):
    """Node-tree blocks (an int16 'parent' field plus a 'child' / 'sibling' one: mode / hlmt / coll / phmo nodes) whose indices leave the
    block, point at the element itself, or loop.  [] = sane."""
    out = []
    for n in root.iter_nodes():
        for f in n.fields:
            if f.type not in T.BLOCK_TYPES:
                continue
            kids = n.blocks.get(f.offset) or []
            if not kids:
                continue
            i16 = [c for c in f.children if c.type == 'int16' and c.name]
            par = next((c for c in i16 if 'parent' in c.name.lower()), None)
            ch = next((c for c in i16 if 'child' in c.name.lower()), None)
            sib = next((c for c in i16 if 'sibling' in c.name.lower()), None)
            if par is None or (ch is None and sib is None):
                continue
            m = len(kids)
            bad = False
            for i, k in enumerate(kids):
                for c in (par, ch, sib):
                    if c is None:
                        continue
                    v = k.i16(c.offset)
                    if v == i or (v != -1 and not 0 <= v < m):
                        out.append('%s/%s[%d] %s = %d' % (n.path, f.name, i, c.name, v))
                        bad = True
            if bad:
                continue
            # a cycle over the child / sibling links (several roots chained as siblings are fine: bulldog_motorcycle)
            links = [[k.i16(c.offset) for c in (ch, sib) if c is not None and k.i16(c.offset) != -1] for k in kids]
            state = [0] * m                                              # 0 new, 1 on the path, 2 done
            loop = None
            for s in range(m):
                if state[s] or loop is not None:
                    continue
                stack = [(s, iter(links[s]))]
                state[s] = 1
                while stack and loop is None:
                    i, it = stack[-1]
                    j = next(it, None)
                    if j is None:
                        state[i] = 2
                        stack.pop()
                    elif state[j] == 1:
                        loop = j
                    elif state[j] == 0:
                        state[j] = 1
                        stack.append((j, iter(links[j])))
            if loop is not None:
                out.append('%s/%s: the node tree loops at %d' % (n.path, f.name, loop))
    return out


def _learned_enums():
    """Enum values genuine Vista maps use beyond the plugin's options (validate's learned table; {} when it cannot be loaded)."""
    try:
        from .. import validate as V
        return V._learned_enums()
    except Exception:                                                    # noqa: BLE001
        return {}


def plausibility_problems(root):
    """What makes a SALVAGED tag implausible as the tag's own data (plausibility gate, decision of 2026-10-27; batch_v4 audit R1):
    an enum outside its plugin options (and the values stock Vista data uses, validate._learned_enums), a non-finite float in a named
    field (not 'Unknown' / padding), a block of more than MAX_BLOCK_COUNT elements.  Salvage reads junk through a readable but wrong
    pointer as faithfully as a real block, and the 13 float.nonfinite of batch_v4 came from exactly such elements: Chastebeaver /
    Chastewarlock / Kraken / Mines jpt! flaming_death (1) Player Responses[0] = Response Type 10, Maximum Intensity 0x80670BE4 (a tag
    address), Ricochet's boarding_melee_response Player Responses (vertex data), the Evermore plasma_rifle's Autofire Throw NaN - each of
    them replaced a working Vista fallback.  [] = sane.  Only salvaged tags are gated: a tag that decodes strictly keeps its bytes, NaN
    included (the Xbox read them)."""
    out = []
    learned = None
    for n in root.iter_nodes():
        for f in n.fields:
            t = f.type
            if t in T.BLOCK_TYPES:
                k = n.blocks.get(f.offset) or []
                if len(k) > MAX_BLOCK_COUNT:
                    out.append('%s/%s: %d elements' % (n.path, f.name, len(k)))
                continue
            nm = (f.name or '').strip().lower()
            if not nm or nm.startswith(('unknown', 'padding', 'pad ')):
                continue
            lanes = FLOAT_LANES.get(t)
            if lanes:
                if f.offset + 4 * lanes <= len(n.data):
                    v = struct.unpack_from('<%df' % lanes, n.data, f.offset)
                    if not all(math.isfinite(x) for x in v):
                        out.append('%s/%s = %s (non-finite)' % (n.path, f.name, ', '.join('%g' % x for x in v)))
            elif t in ENUM_TYPES and f.options and f.offset + f.size <= len(n.data):
                mask = (1 << (8 * f.size)) - 1
                v = int.from_bytes(bytes(n.data[f.offset:f.offset + f.size]), 'little')
                allowed = {o & mask for _, o in f.options} | {mask}               # all ones = NONE
                if v not in allowed:
                    if learned is None:
                        learned = _learned_enums()
                    allowed |= {int(x) & mask for x in learned.get('%s|%s|%s@%x' % (n.group, n.path, f.name, f.offset), {}).get('v', ())}
                    if v not in allowed:
                        out.append('%s/%s = %d (no option)' % (n.path, f.name, v))
    return out


class TagSalvage(object):
    """Per-map salvage service.  xc: XboxCache, lx: Xbox layouts, stock: stock_index.StockIndex (rung ii; None skips it)."""

    def __init__(self, xc, lx, stock=None):
        self.xc, self.lx, self.stock = xc, lx, stock
        h = xc.header
        self.region = (h['tag_offset'] + h['tag_index_size'] + xc.magic, h['tag_offset'] + h['tag_size'] + xc.magic)
        self.eof = len(xc.m) + xc.magic
        self._stock_roots = {}
        self._by_name = None

    # ---- the ladder ----------------------------------------------------------------------------------------------------
    def decode(self, t, g, root_wild=False, gate=True):
        """(root, notes) of tag t decoded as group g; notes = [(class, block path, detail)].  Raises Unsalvageable when no rung gives
        a tag that validates (or the root itself cannot be read).  A wild pointer in the ROOT struct (not a prediction hint) means the
        main struct itself is damaged, not a rarely-read sub-structure: refused unless root_wild (convert_xbox.decode_shader: the
        emptied 'Postprocess Properties' of 21 maps' injected shaders).  gate: a salvaged tag must also pass plausibility_problems
        (decision of 2026-10-27), else Unsalvageable and the converter keeps its fallback (the Vista tag of that name); decode_shader
        (root_wild) is not gated: a shader's fallback is a skeleton, not the tag, and those shaders were always built from the same bytes."""
        lay = self.lx[g]
        rs = lay.base_size or lay.computed_size
        raw = self.xc.read(t.addr, rs)
        if 0 < t.size < rs:
            # an index entry too small for its own root: its address lies on another tag's bytes (Outflod effe legs_destroyed, size 4:
            # Locations / Events of ASCII) - no tag body to salvage.  Size 0 says nothing: protected maps zero every size (Cassiopeia)
            # and stock maps have size-0 entries (04b_floodlab legs_destroyed).
            # EXCEPTION, a SHORT OBJECT ROOT: an injection tool wrote an object definition in a shorter layout and sized the entry to it
            # (Tortuga 2026-11-02: 9 custom scen "moon (N)" of 172 bytes, the 196-byte root's last 24 = the next tag, their own hlmt).
            # The object type must name this group and the declared bytes must end on a field boundary at or past every tagref; the
            # bytes past the declared size are taken as zero (empty blocks, default enums) - what the tool wrote, not the next tag.
            ot = struct.unpack_from('<h', raw, 0)[0] if g in OBJECT_TYPE_GROUPS else -1
            last_ref = max((f.offset + f.size for f in lay.fields if f.type.lower() == 'tagref'), default=0)
            if (g in OBJECT_TYPE_GROUPS and 0 <= ot < len(OBJECT_TYPE_GROUPS) and OBJECT_TYPE_GROUPS[ot] == g and t.size >= last_ref
                    and t.size % 4 == 0 and any(f.offset == t.size for f in lay.fields)):
                raw = bytes(raw[:t.size]) + bytes(rs - t.size)
                self.short_roots = getattr(self, 'short_roots', 0) + 1
            else:
                raise Unsalvageable('%s %s: the index entry declares %d bytes, less than its %d-byte root' % (g, t.name, t.size, rs))
        if g in OBJECT_TYPE_GROUPS:
            # an object definition starts with its Object Type, which the engine builds from (objtype.py): bytes that do not name this
            # group are not this tag's definition (Soccer bloc bm_rock_sm: type 36 and six root blocks of junk - not salvaged)
            ot = struct.unpack_from('<h', raw, 0)[0]
            if not 0 <= ot < len(OBJECT_TYPE_GROUPS) or OBJECT_TYPE_GROUPS[ot] != g:
                raise Unsalvageable('%s %s: Object Type %d is not a %s definition' % (g, t.name, ot, g))
        r = self.shifted(t, g, lay, rs, raw)
        if r is not None:
            bad = plausibility_problems(r[0]) if gate and not root_wild else []
            if bad:
                raise Unsalvageable('shifted %s %s is implausible: %s' % (g, t.name, '; '.join(bad[:3])))
            return r
        ctx = _Tag(self, t, g)
        root = T.decode_struct(self.xc.read, g, '', lay.fields, rs, t.addr, raw=raw, salvage=ctx)
        bad = tree_problems(root)
        if bad:
            raise Unsalvageable('salvaged %s %s does not validate: %s' % (g, t.name, '; '.join(bad[:3])))
        if not root_wild:
            # corpus scan (208 maps): Echo_Station's goof placements (Default Variants / Settings of floats),
            # Moon_Stronghold rocket_detonation (Locations 0x01FB8810, Events 0x0210FCA4), Biohazard energy_blade_useless (AI Properties /
            # First Person 0x88572CD8): emptied they would be an effect without locations / events (Events index Locations) or a weapon
            # without its first-person data - the Vista tag of that name stays the fallback.  Prediction hints are never relied on
            # (Nightmare smg, Skull_Island cannon_turret_mp: Predicted Resources / Bitmaps)
            wild = [p for c, p, _ in ctx.notes if c == 'emptied' and p.count('/') == 1 and p[1:] not in ROOT_HINTS]
            if wild:
                raise Unsalvageable('%s %s: wild pointer in the root struct (%s)' % (g, t.name, ', '.join(wild[:4])))
        if gate and not root_wild:
            bad = plausibility_problems(root)
            if bad:
                raise Unsalvageable('salvaged %s %s is implausible: %s' % (g, t.name, '; '.join(bad[:3])))
        return root, ctx.notes

    def shifted(self, t, g, lay, rs, raw):
        """Rung (i): the decode with every pointer of the tag's window moved back by S, or None."""
        ptrs = []
        for f in lay.fields:
            if f.type in T.BLOCK_TYPES or f.type in T.DATA_TYPES:
                c, p = struct.unpack_from('<iI', raw, f.offset)
                if c > 0 and p not in (0, NULL):
                    ptrs.append(p)
        if not ptrs:
            return None
        s = min(ptrs) - (t.addr + rs)
        lo, hi = t.addr + s, t.addr + t.size + s
        if s == 0 or abs(s) > MAX_SHIFT or not all(lo <= p < hi for p in ptrs):
            return None
        xc = self.xc
        ends = []

        def rd(a, n):
            if lo <= a and a + n <= hi:
                ends.append(a + n - s)
                return xc.read(a - s, n)
            return xc.read(a, n)
        try:
            root = T.decode_struct(rd, g, '', lay.fields, rs, t.addr, raw=raw)
        except Exception:                                                # noqa: BLE001
            return None
        # a moved body ends where the tag ends (its last block within alignment of addr + size); alignment padding between the root and
        # the first block, or a junk pointer that happens to make the window, does not
        if not ends or not t.addr + t.size - 16 <= max(ends) <= t.addr + t.size or tree_problems(root):
            return None
        return root, [('shift', '', 'every pointer %+d bytes past its data (an unrelocated move): decoded at %+d' % (s, -s))]

    # ---- rung (ii): the stock copy whose pointer this is ----------------------------------------------------------------------
    def stock_root(self, c, st, g):
        k = (c.path, st.index, g)
        if k not in self._stock_roots:
            try:
                self._stock_roots[k] = T.decode_tag(c.read, self.lx[g], st.addr, g)
            except Exception:                                            # noqa: BLE001
                self._stock_roots[k] = None
        return self._stock_roots[k]

    def stock_match(self, t, g, node, f, cnt, ptr):
        """(stock cache, stock tag, via) whose copy of t's name has this exact (count, pointer) in field f of a struct at node's path;
        via = None.  Else the same for the name of another tag of this map that stores the very same (path, count, pointer) - one
        extractor run left one stale pointer in every tag whose block the cache compiler had shared: Sanctus jpt! ghost_bolt_trigger /
        banshee_bomb_* carry 0x8075609C, which 07a_highcharity has in hunter_particle_cannon_shake and plasma_cannon_bolt but not in
        its own ghost / banshee damage effects; via = that tag."""
        if self.stock is None:
            return None
        mine = [(t, None)] + [(u, u) for u in self.mod_pointer_index(g).get((node.path, f.offset, cnt, ptr), ()) if u is not t]
        for x, via in mine:
            for name in clone_bases(x.name):
                for c, st in self.stock.tags(g, name):
                    sr = self.stock_root(c, st, g)
                    if sr is None:
                        continue
                    for n in sr.iter_nodes():
                        if n.path == node.path and len(n.data) == len(node.data) and \
                                struct.unpack_from('<iI', n.data, f.offset) == (cnt, ptr):
                            return c, st, via
        return None

    def mod_pointer_index(self, g):
        """(struct path, field offset, count, pointer) -> [this map's tags of group g storing it] (a lenient pass over the group, once)."""
        memo = self.__dict__.setdefault('_mod_ptrs', {})
        if g not in memo:
            ix = memo[g] = {}
            for u in self.xc.tags:
                if u.is_empty or not u.addr or u.group != g or self.xc.tag_by_datum(u.datum) is not u:
                    continue
                try:
                    r = T.decode_tag(self.xc.read, self.lx[g], u.addr, g, emptied=[])
                except Exception:                                        # noqa: BLE001
                    continue
                for n in r.iter_nodes():
                    for fl in n.fields:
                        if fl.type in T.BLOCK_TYPES or fl.type in T.DATA_TYPES:
                            c, p = struct.unpack_from('<iI', n.data, fl.offset)
                            if c > 0 and p not in (0, NULL):
                                ix.setdefault((n.path, fl.offset, c, p), []).append(u)
        return memo[g]

    def mod_tag(self, group, name):
        if self._by_name is None:
            self._by_name = {}
            for x in self.xc.tags:
                if not x.is_empty and x.name and (self.xc.tag_by_datum(x.datum) is x):
                    self._by_name.setdefault((x.group, x.name.lower()), x)
        return self._by_name.get((group, (name or '').lower()))

    def translate(self, kids, c):
        """Stock-map string ids / references in restored nodes -> this map's (string ids by text, references by group + name; a target
        the map lacks becomes NONE).  Returns the number of such NONE references, or None when a node holds a raw pointer into the
        stock map's file (not restorable)."""
        lost = 0
        for k in kids:
            for n in k.iter_nodes():
                for f in n.fields:
                    tl = f.type.lower()
                    if f.name in RAW_FIELDS and tl in ('uint32', 'int32') and n.u32(f.offset) not in (0, NULL):
                        return None
                    if tl == 'stringid':
                        n.set_u32(f.offset, self.xc.sid_of(c.string_id(n.u32(f.offset)) or '', add=True))
                    elif tl == 'tagref':
                        o = f.offset + (4 if f.size == 8 else 0)
                        d = n.u32(o)
                        st = c.tag_by_datum(d) if d not in (0, NULL) else None
                        mt = self.mod_tag(st.group, st.name) if st is not None else None
                        n.set_u32(o, mt.datum if mt is not None else NULL)
                        if mt is None and st is not None:
                            lost += 1
                        if f.size == 8 and mt is None:
                            n.data[f.offset:f.offset + 4] = b'\xff\xff\xff\xff'
        return lost

    # ---- rung (v): the declared tag region past the end of the file ---------------------------------------------------------
    def zero_fill(self, ptr, n):
        """The bytes the Xbox loaded for [ptr, ptr + n) when that lies in the header's tag region and runs past the file end (the file's
        part, then zeros), else None.  Bounded: never more than ZERO_FILL_MAX bytes nor more than ZERO_FILL_SLACK past
        the file end - a junk count (a string id such as 0x14002600) with a pointer inside an inflated header region would otherwise
        allocate and decode millions of zero elements; such a block is emptied instead."""
        lo, hi = self.region
        if not (lo <= ptr and ptr + n <= hi and ptr + n > self.eof):
            return None
        if n > ZERO_FILL_MAX or ptr + n > self.eof + ZERO_FILL_SLACK:
            return None
        o = ptr - self.xc.magic
        part = bytes(self.xc.m[o:len(self.xc.m)]) if o < len(self.xc.m) else b''
        return part[:n] + b'\0' * (n - len(part[:n]))


class _Tag(object):
    """The salvage hook of one tag decode (tagtree decode_struct salvage=)."""

    def __init__(self, sv, t, g):
        self.sv, self.t, self.g = sv, t, g
        self.notes = []
        self._ptrs = None
        # tagtree reads this after each block(): True = the result depends on the parent struct (a synthesised collision permutation
        # is named after ITS region), so it is not cached for other fields holding the same bad (pointer, count)
        self.per_parent = False

    def note(self, cls, path, detail):
        self.notes.append((cls, path, detail))

    def elements(self, read, node, f, n, ptr, es, blob, _seen):
        cp = T.child_path(node.path, f)
        return [T.decode_struct(read, node.group, cp, f.children, es, ptr + i * es, raw=blob[i * es:(i + 1) * es], _seen=_seen, salvage=self)
                for i in range(n)]

    def pointers(self):
        """Sorted block / dataref pointers of the tag (as stored; a lenient pass) - the room of a junk count ends at the next one."""
        if self._ptrs is None:
            ps = set()
            try:
                root = T.decode_tag(self.sv.xc.read, self.sv.lx[self.g], self.t.addr, self.g, emptied=[])
                for n in root.iter_nodes():
                    for f in n.fields:
                        if f.type in T.BLOCK_TYPES or f.type in T.DATA_TYPES:
                            c, p = struct.unpack_from('<iI', n.data, f.offset)
                            if c > 0 and p not in (0, NULL):
                                ps.add(p)
            except Exception:                                            # noqa: BLE001
                pass
            self._ptrs = sorted(ps)
        return self._ptrs

    def room(self, ptr, es):
        end = self.t.addr + self.t.size
        if not self.t.addr <= ptr < end:
            return 0
        nxt = min([p for p in self.pointers() if p > ptr] + [end])
        return (min(nxt, end) - ptr) // es

    def block(self, read, node, f, cnt, ptr, es, ex, _seen):
        kids = self._block(read, node, f, cnt, ptr, es, ex, _seen)
        self.per_parent = (node.group, T.child_path(node.path, f)) == COLLISION_PERMUTATIONS and es == 8 and \
            bool(self.notes) and self.notes[-1][0] == 'collision_permutation'
        return kids

    def _block(self, read, node, f, cnt, ptr, es, ex, _seen):
        sv, cp = self.sv, T.child_path(node.path, f)
        # (ii) the stock original's bytes
        m = sv.stock_match(self.t, self.g, node, f, cnt, ptr)
        if m is not None:
            c, st, via = m
            kids = [T.decode_struct(c.read, node.group, cp, f.children, es, ptr + i * es) for i in range(cnt)]
            lost = sv.translate(kids, c)
            if lost is not None:
                self.note('stale_restore', cp, '0x%08X x%d = %s #%d %s%s%s' % (
                    ptr, cnt, c.path.rsplit('\\', 1)[-1], st.index, st.name, ' (the pointer of this map\'s %s)' % via.name if via else '',
                    ' (%d reference(s) the map lacks: NONE)' % lost if lost else ''))
                return kids
        # (iii) a junk count: the room up to the next block
        room = self.room(ptr, es)
        if 0 < room < cnt:
            try:
                blob = read(ptr, room * es)
            except Exception:                                            # noqa: BLE001
                blob = None
            if blob is not None:
                txt = sv.xc.string_id(cnt)
                sid = ' = string id %r' % txt if txt and (cnt >> 24) == len(txt) else ''
                self.note('count_clamped', cp, 'count 0x%X%s -> %d (the room up to the next block)' % (cnt, sid, room))
                return self.elements(read, node, f, room, ptr, es, blob, _seen)
        # (iv) a collision region always keeps one permutation
        if (node.group, cp) == COLLISION_PERMUTATIONS and es == 8:
            name = self.collision_permutation_name(node)
            e = T.Node(node.group, cp, f.children, es)
            e.set_u32(0, name)
            self.note('collision_permutation', cp, 'unreadable (0x%08X x%d): one permutation %r, collision / physics 0'
                      % (ptr, cnt, sv.xc.string_id(name)))
            return [e]
        # (v) zeros where the file ends inside the declared region, else nothing
        blob = sv.zero_fill(ptr, cnt * es) if cnt <= MAX_BLOCK_COUNT else None
        if blob is not None:
            self.note('zero_filled', cp, '0x%08X x%d: past the file end inside the tag region' % (ptr, cnt))
            return self.elements(read, node, f, cnt, ptr, es, blob, _seen)
        self.note('emptied', cp, '0x%08X x%d: %s' % (ptr, cnt, ex))
        return []

    def data(self, read, node, f, sz, ptr, ex):
        sv, cp = self.sv, T.child_path(node.path, f)
        m = sv.stock_match(self.t, self.g, node, f, sz, ptr)
        if m is not None:
            c, st, via = m
            try:
                b = c.read(ptr, sz)
                self.note('stale_restore', cp, '0x%08X +%d = %s #%d %s%s' % (ptr, sz, c.path.rsplit('\\', 1)[-1], st.index, st.name,
                                                                          ' (the pointer of this map\'s %s)' % via.name if via else ''))
                return b
            except Exception:                                            # noqa: BLE001
                pass
        b = sv.zero_fill(ptr, sz)
        if b is not None:
            self.note('zero_filled', cp, '0x%08X +%d: past the file end inside the tag region' % (ptr, sz))
            return b
        self.note('emptied', cp, '0x%08X +%d: %s' % (ptr, sz, ex))
        return b''

    def collision_permutation_name(self, node):
        """Xbox string id for a synthesised collision permutation: the render model's region of the collision region's name, its first
        permutation's name; else 'default'."""
        xc = self.sv.xc
        try:
            lay = self.sv.lx['hlmt']
            f = next(x for x in lay.fields if x.name == 'Render Model')
            mt = xc.tag_by_datum(struct.unpack_from('<I', xc.read(self.t.addr + f.offset + 4, 4))[0])
            if mt is not None and mt.group == 'mode' and mt.addr:
                try:
                    mr = T.decode_tag(xc.read, self.sv.lx['mode'], mt.addr, 'mode')
                except Exception:                                        # noqa: BLE001
                    mr = T.decode_tag(xc.read, self.sv.lx['mode'], mt.addr, 'mode', emptied=[])
                for r in mr.block('Regions'):
                    if r.u32(0) == node.u32(0) and r.block('Permutations'):
                        return r.block('Permutations')[0].u32(0)
        except Exception:                                                # noqa: BLE001
            pass
        return xc.sid_of('default', add=True)
