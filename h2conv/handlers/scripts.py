# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""HaloScript (hs) conversion for the scenario tag: Halo 2 MCC (cache v13) -> Halo 2 Vista (cache v8).

What lives in the scnr tag (both games, same layout, verified on 43 stock maps + custom headlong)
----------------------------------------------------------------------------------------------
  0x1A8 Script Syntax Data   dataref   always EMPTY in compiled caches (authoring-only)
  0x1B0 Script String Data   dataref   pool of NUL-separated tokens (function/global names, string constants...)
  0x1B8 Scripts              block 0x28   name[32], script_type u16, return_type u16, root_expression datum
  0x1C0 Globals              block 0x28   name[32], type u16, pad, initialization_expression datum
  0x1C8 Script References    block 0x8    tagref (tags used by script constants)
  0x1D0 Source Files Block   2 x uint32   zero in compiled caches
  0x238 Script Expressions   block 0x14   the compiled syntax-node array ("script node" data array, max 36864)

Syntax node (0x14 bytes, = hs_syntax_node of the engine):
  +0 u16 salt (datum identifier; 0 = FREE slot)   +2 i16 opcode (function index | script index | constant type)
  +4 u16 value type                               +6 u16 flags (1 primitive, 2 script, 4 variable, 8 permanent)
  +8 datum next sibling                           +C u32 source offset (offset of the token in the string pool)
  +10 u32 value (constant | first child datum of a group)
A call is a GROUP node (flags 8 / 10 for a script call, opcode = function (script) index, value = datum of the first
child) whose first child is a function-name node (flags 9, type 2, same opcode) followed by the arguments.

Conversion (all measured against the stock Vista maps)
  * node indices and salts are KEPT (in-place conversion), so every datum (next, first child, Scripts/Globals root
    expression) stays valid;
  * function opcodes are remapped BY NAME + SIGNATURE (MCC inserted/removed ~60 functions in a 918/927 entry table);
    MCC-only functions are replaced in place by a same-typed constant (value) or by `(print "")` (void);
  * external (engine) global designators (0x8000|index) are remapped for the 15 globals verified in real Vista/MCC
    script pairs; any other external read becomes a constant and a `set` of it becomes `(print "")` unless
    ctx.opts['hs_unverified_globals'] is set (then the type-sequence alignment of the two tables is used);
  * tag-reference constants -> ctx.remap_datum, string_id constants -> ctx.remap_sid, string constants keep the
    pool offset (the Vista engine rewrites the pointer from the source offset at load: halo2.exe sub_4F3107);
  * Script References is rebuilt as "unique tag constants in order of first use" (the rule of all 41 stock scenarios);
  * free slots keep the MCC 0xBABA fill (identical to what the Vista tool writes for ~half of its free slots);
  * more than 36864 nodes (halo2.exe data array size) -> trailing free slots are cut, else the table is compacted
    (04b_floodlab: 37509 -> 34835 + 256 free).

Public API: convert_scenario_scripts(ctx, mcc_scnr_node, vista_scnr_node) (also registered as node handler
('scnr', '')), ProgramConverter, HsTables/get_tables(), validate_program(), compact_nodes().
"""
import collections
import difflib
import os
import struct
import xml.etree.ElementTree as ET

from . import node_handler
from .. import plugins as P
from .. import tagtree as T

# ------------------------------------------------------------------------------------------------
# constants
# ------------------------------------------------------------------------------------------------
NODE_SIZE = 0x14
MAX_NODES = 36864                      # halo2.exe hs_initialize: data array "script node", 36864 x 20
MAX_POOL = 614400                      # scnr.hs_string_constants DataSize (Cartographer scenario_definitions.h)
FREE_SLACK = 256                       # free slots appended after a compaction
MAX_SCRIPTS, MAX_GLOBALS, MAX_REFS = 1024, 256, 512     # Cartographer hs.h / scenario_definitions.h block limits
NONE32 = 0xFFFFFFFF
FREE_RECORD = struct.pack('<HhHHII4s', 0, -17734, 0xBABA, 0xBABA, 0xBABABABA, 0xBABABABA, b'\xba\xba\xba\xba')
PRIMITIVE, SCRIPT, VARIABLE, PERMANENT = 1, 2, 4, 8

VT_VOID, VT_BOOLEAN, VT_REAL, VT_SHORT, VT_LONG, VT_STRING = 4, 5, 6, 7, 8, 9
VT_FUNCTION_NAME = 2
VT_STRING_ID = 0x0B
VT_TAG_FIRST, VT_TAG_LAST = 0x20, 0x2B  # sound .. lightmap_definition (tag datum constants)

def default_xml_mcc():
    """The MCC HaloScript function table of the Assembly files in use (H2MCC_Scripting.xml, else H2MCC_2023Jul_Scripting.xml)."""
    return P.assembly().scripting_mcc_xml


def default_xml_vista():
    return P.assembly().scripting_vista_xml


def __getattr__(name):                  # the former module constants, for the developer tools that still read them
    if name == 'XML_MCC':
        return default_xml_mcc()
    if name == 'XML_VISTA':
        return default_xml_vista()
    if name == 'ASSEMBLY_FORMATS':
        return P.assembly().formats_root
    raise AttributeError('module %r has no attribute %r' % (__name__, name))

# Ticks per second of the Vista engine; MCC `(game_tick_rate)` returns 60 there.  Scripts written with the
# `(* N (game_tick_rate_scalar))` / `tick_seconds` idiom then reproduce the Vista originals exactly.
VISTA_TICK_RATE = 30

# Renames between the two function tables (signature identical, verified at table build time).
FUNCTION_ALIASES = {
    'player_effect_set_max_vibration': 'player_effect_set_max_rumble',    # also verified in real scripts (05b_deltatowers)
    'webstats_test_submit': 'webstats_submit',
    'ui_test_error_ok': 'ui_error_ok',
    'ui_test_error_ok_cancel': 'ui_error_ok_cancel',
    'ui_test_confirmation': 'ui_confirmation',
    'ui_debug_load_main_menu': 'ui_load_main_menu',
    'ui_debug_text_bounds': 'ui_text_bounds',
    'ui_debug_show_title_safe_bounds': 'ui_show_title_safe_bounds',
    'ui_debug_element_bounds': 'ui_element_bounds',
    'ui_debug_screen_tag': 'ui_screen_tag',
    'ui_debug_show_screen_tag_path': 'ui_show_screen_tag_path',
    'ui_debug_show_current_screen_tag': 'ui_show_current_screen_tag',
    'net_test_ping': 'net_ping',
    'net_test_channel_loopback': 'net_channel_loopback',
    'net_test_channel_delete': 'net_channel_delete',
    'net_test_leave_squad': 'net_leave_squad',
    'net_test_delegate_host': 'net_delegate_host',
    'net_test_delegate_leader': 'net_delegate_leader',
    'net_test_map_name': 'net_map_name',
    'net_test_campaign_difficulty': 'net_campaign_difficulty',
    'net_test_player_color': 'net_player_color',
    'net_test_reset_objects': 'net_reset_objects',
    'net_test_fatal_error': 'net_fatal_error',
    # MCC "breadcrumbs" navigation markers fall back to the classic Halo 2 nav points (identical signatures).
    'breadcrumbs_activate_team_nav_point_flag': 'activate_team_nav_point_flag',
    'breadcrumbs_activate_team_nav_point_object': 'activate_team_nav_point_object',
    'breadcrumbs_deactivate_team_nav_point_flag': 'deactivate_team_nav_point_flag',
    'breadcrumbs_deactivate_team_nav_point_object': 'deactivate_team_nav_point_object',
}

# MCC-only functions with a better fallback than the generic "same-typed zero / print ''" rule.
#   ('const', value)        replace the call by this constant of the call's return type
#   ('noop', None)          replace the call by (print "")
#   ('cmp_distance', None)  real distance used in a comparison: pick the constant that makes the comparison true
SOLO_MP_SAVE_CALLS = ('game_save', 'game_save_cancel', 'game_save_no_timeout', 'game_save_immediate', 'game_save_unsafe')

MCC_ONLY_POLICY = {
    ('game_tick_rate', ()): ('const', VISTA_TICK_RATE),
    ('game_tick_rate_scalar', ()): ('const', 1),
    ('game_tick_get', ()): ('const', 0),
    ('game_tick_length', ()): ('const', 1.0 / VISTA_TICK_RATE),
    ('bink_done', ()): ('const', True),              # fail-open: a movie wait never blocks the script
    ('objects_distance_to_position', ('object_list', 'real', 'real', 'real')): ('cmp_distance', None),
}

# Verified anchors: MCC external global name -> Vista external index (pairs of real, structurally identical
# scripts of the 15 campaign maps: MCC index 787..808 -> Vista +8, cinematic_letterbox_style 978 -> 986).
VERIFIED_GLOBALS = {
    'ai_current_squad': 795, 'ai_current_actor': 796,
    'ai_combat_status_idle': 804, 'ai_combat_status_alert': 805, 'ai_combat_status_active': 806,
    'ai_combat_status_uninspected': 807, 'ai_combat_status_definite': 808, 'ai_combat_status_certain': 809,
    'ai_combat_status_visible': 810, 'ai_combat_status_clear_los': 811, 'ai_combat_status_dangerous': 812,
    'ai_movement_patrol': 813, 'ai_movement_combat': 815, 'ai_movement_flee': 816,
    'cinematic_letterbox_style': 986,
}


# ------------------------------------------------------------------------------------------------
# scripting tables (function / global opcodes by name)
# ------------------------------------------------------------------------------------------------
def load_scripting_xml(path):
    """-> {'vt': {op: name}, 'fn': {op: dict(name, ret, args)}, 'gl': {op: dict(name, type)}}"""
    root = ET.parse(path).getroot()
    d = {'vt': {}, 'fn': {}, 'gl': {}}
    for t in root.find('valueTypes'):
        d['vt'][int(t.get('opcode'), 16)] = t.get('name')
    for f in root.find('functions'):
        argc = f.get('argc')
        d['fn'][int(f.get('opcode'), 16)] = dict(name=f.get('name'), ret=f.get('returnType'),
                                                 args=tuple(a.get('type') for a in f.findall('arg')),
                                                 argc=int(argc) if argc is not None else None,
                                                 special=f.get('flags') is not None)   # begin/if/set/... : variadic forms
    for g in root.find('globals'):
        d['gl'][int(g.get('opcode'), 16)] = dict(name=g.get('name'), type=g.get('type'))
    return d


class FnPlan(object):
    """What to do with one MCC function opcode."""
    __slots__ = ('kind', 'vista_op', 'value', 'name', 'ret', 'args')

    def __init__(self, kind, vista_op, value, name, ret, args):
        self.kind = kind          # 'map' | 'alias' | 'const' | 'noop' | 'cmp_distance'
        self.vista_op = vista_op
        self.value = value
        self.name = name
        self.ret = ret
        self.args = args


class HsTables(object):
    """MCC -> Vista remap tables for functions and external globals."""

    def __init__(self, xml_mcc=None, xml_vista=None):
        self.mcc = load_scripting_xml(xml_mcc or default_xml_mcc())
        self.vista = load_scripting_xml(xml_vista or default_xml_vista())
        self.value_types_equal = self.mcc['vt'] == self.vista['vt']
        self.vt_by_name = {v: k for k, v in self.mcc['vt'].items()}
        self.fn_plan = {}
        self.fn_alias_rejected = []
        self._build_functions()
        self.gl_map = {}               # VERIFIED: mcc external index -> vista external index (real script pairs)
        self.gl_verified = set()       # == gl_map keys
        self.gl_aligned = {}           # candidates from type-sequence alignment (unverified; opt-in)
        self._build_globals()
        v = self.vista['fn']
        self.print_op = next(op for op, f in v.items() if f['name'] == 'print' and f['args'] == ('string',))

    # ---- functions ----
    def _build_functions(self):
        mfn, vfn = self.mcc['fn'], self.vista['fn']
        sig = lambda f: (f['ret'], f['args'])
        vby = collections.defaultdict(list)
        for op in sorted(vfn):
            if vfn[op]['name']:
                vby[vfn[op]['name']].append(op)
        used = set()
        # pass 1: identical name + identical signature, k-th overload with k-th overload
        for op in sorted(mfn):
            f = mfn[op]
            for vop in vby.get(f['name'], ()):
                if vop not in used and sig(vfn[vop]) == sig(f):
                    used.add(vop)
                    self.fn_plan[op] = FnPlan('map', vop, None, f['name'], f['ret'], f['args'])
                    break
        # pass 2: explicit aliases with identical signature
        for op in sorted(mfn):
            if op in self.fn_plan:
                continue
            f = mfn[op]
            tgt = FUNCTION_ALIASES.get(f['name'])
            if tgt is None:
                continue
            for vop in vby.get(tgt, ()):
                if sig(vfn[vop]) == sig(f):          # an alias target may also be the target of its own MCC twin
                    self.fn_plan[op] = FnPlan('alias', vop, None, f['name'], f['ret'], f['args'])
                    break
            else:
                self.fn_alias_rejected.append(f['name'])
        # pass 3: MCC-only functions -> policy / generic fallback by return type
        for op in sorted(mfn):
            if op in self.fn_plan:
                continue
            f = mfn[op]
            pol = MCC_ONLY_POLICY.get((f['name'], f['args']))
            if pol is not None:
                kind, val = pol
            elif f['ret'] == 'void':
                kind, val = 'noop', None
            else:
                kind, val = 'const', default_constant(f['ret'])
            self.fn_plan[op] = FnPlan(kind, None, val, f['name'], f['ret'], f['args'])

    # ---- external globals ----
    def _build_globals(self):
        mgl, vgl = self.mcc['gl'], self.vista['gl']
        mt = [mgl[i]['type'] for i in range(len(mgl))]
        vt = [vgl[i]['type'] for i in range(len(vgl))]
        sm = difflib.SequenceMatcher(None, mt, vt, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == 'equal':
                for k in range(i2 - i1):
                    self.gl_aligned[i1 + k] = j1 + k
        by_name = {g['name']: i for i, g in mgl.items()}
        for name, vidx in VERIFIED_GLOBALS.items():
            midx = by_name.get(name)
            if midx is None or midx >= len(mgl) or vidx >= len(vgl) or mgl[midx]['type'] != vgl[vidx]['type']:
                continue
            self.gl_map[midx] = vidx
            self.gl_verified.add(midx)

    def lookup_global(self, mcc_index, use_unverified=False):
        v = self.gl_map.get(mcc_index)
        if v is None and use_unverified:
            v = self.gl_aligned.get(mcc_index)
        return v

    def global_name(self, mcc_index):
        g = self.mcc['gl'].get(mcc_index)
        return g['name'] if g else '#%d' % mcc_index

    def global_type(self, mcc_index):
        g = self.mcc['gl'].get(mcc_index)
        return g['type'] if g else None


_TABLES = {}


def get_tables(xml_mcc=None, xml_vista=None):
    xml_mcc = xml_mcc or default_xml_mcc()
    xml_vista = xml_vista or default_xml_vista()
    key = (xml_mcc, xml_vista)
    if key not in _TABLES:
        _TABLES[key] = HsTables(xml_mcc, xml_vista)
    return _TABLES[key]


def default_constant(type_name):
    """Neutral value for a hs value type (used for dropped MCC-only calls / globals)."""
    if type_name in ('boolean', 'real', 'short', 'long', 'void'):
        return 0
    return None        # NONE / 0xFFFFFFFF


def pack_constant(vt, value):
    """4-byte node value for a constant of value type vt (padding bytes as the Tool writes them)."""
    if value is None:
        return b'\xff\xff\xff\xff'
    if vt == VT_BOOLEAN:
        return bytes([1 if value else 0, 0xFF, 0xFF, 0xFF])
    if vt == VT_REAL:
        return struct.pack('<f', float(value))
    if vt == VT_SHORT:
        return struct.pack('<h', int(value)) + b'\xff\xff'
    if vt == VT_LONG:
        return struct.pack('<i', int(value))
    return b'\x00\x00\x00\x00'


# ------------------------------------------------------------------------------------------------
# node table model
# ------------------------------------------------------------------------------------------------
class Rec(object):
    __slots__ = ('salt', 'op', 'vt', 'fl', 'nxt', 'sa', 'val')

    def __init__(self, salt, op, vt, fl, nxt, sa, val):
        self.salt, self.op, self.vt, self.fl, self.nxt, self.sa, self.val = salt, op, vt, fl, nxt, sa, bytearray(val)

    @classmethod
    def unpack(cls, raw):
        salt, op, vt, fl, nxt, sa, val = struct.unpack('<HhHHII4s', raw)
        return cls(salt, op, vt, fl, nxt, sa, val)

    def pack(self):
        return struct.pack('<HhHHII4s', self.salt, self.op, self.vt, self.fl, self.nxt, self.sa, bytes(self.val))

    @property
    def free(self):
        return self.salt == 0

    def val_u32(self):
        return struct.unpack('<I', self.val)[0]

    def set_val_u32(self, v):
        self.val[:] = struct.pack('<I', v & 0xFFFFFFFF)

    def make_free(self):
        self.salt, self.op, self.vt, self.fl, self.nxt, self.sa = 0, -17734, 0xBABA, 0xBABA, 0xBABABABA, 0xBABABABA
        self.val[:] = b'\xba\xba\xba\xba'


def idx_of(datum):
    return datum & 0xFFFF


def salt_for(index):
    """Identifier of node `index` (same scheme as the tool: 'sc' + counter | 0x8000; any nonzero value works)."""
    return ((index + 0x6373) & 0xFFFF) | 0x8000


class HsLimitError(RuntimeError):
    pass


class ProgramConverter(object):
    """Converts one MCC syntax-node table (+ string pool) in place into the Vista form.

    services: object with remap_datum(mcc_datum)->int, remap_sid(mcc_sid)->int, warn(code, msg), stats (Counter).
    """

    def __init__(self, nodes, pool, tables, services):
        self.n = nodes                      # list[Rec]
        self.pool = bytearray(pool)
        self.t = tables
        self.svc = services
        self.orig = [(r.op, r.fl, r.vt) for r in nodes]     # snapshot before mutation
        self.free = [i for i, r in enumerate(nodes) if r.free]
        self.parent = {}                    # child index -> (group index, position)
        self.stats = collections.Counter()
        self._empty_str = None
        self._print_tok = None
        self.tag_order = collections.OrderedDict()      # mcc tag datum -> converted datum, in first-use (node index) order
        self.use_unverified = bool((getattr(services, "opts", None) or {}).get("hs_unverified_globals"))
        # OPT-IN since 2026-10-21 (was default on 2026-10-20): the level1 "checkpoint crash" turned out to be a GPU hang (nvlddmkm TDR) from the
        # Truth hologram, not game_save, so checkpoints are kept again by default (user).  opts['no_checkpoints'] (convert --no-checkpoints) or
        # env H2CONV_NO_CHECKPOINTS=1 still turns game_save* into no-ops on solo maps.  Multiplayer maps are never touched.
        _o = getattr(services, 'opts', None) or {}
        self.solo_mp_no_save = getattr(services, 'map_type', 1) != 1 and bool(
            _o.get('no_checkpoints') or os.environ.get('H2CONV_NO_CHECKPOINTS'))
        self._build_parents()

    # ---- helpers ----
    def _build_parents(self):
        n = self.n
        for g, r in enumerate(n):
            if r.free or (r.fl & PRIMITIVE):
                continue
            c = r.val_u32()
            pos = 0
            guard = 0
            while c != NONE32 and idx_of(c) < len(n) and guard < 4096:
                ci = idx_of(c)
                self.parent[ci] = (g, pos)
                c = n[ci].nxt
                pos += 1
                guard += 1

    def children(self, g):
        out = []
        r = self.n[g]
        c = r.val_u32()
        guard = 0
        while c != NONE32 and idx_of(c) < len(self.n) and guard < 4096:
            out.append(idx_of(c))
            c = self.n[idx_of(c)].nxt
            guard += 1
        return out

    def datum(self, i):
        return (self.n[i].salt << 16) | i

    def free_subtree(self, g, keep_root=True):
        """Free every descendant of group g (and g itself when keep_root is False)."""
        stack = list(self.children(g)) if not (self.n[g].fl & PRIMITIVE) else []
        if not keep_root:
            stack.append(g)
        while stack:
            i = stack.pop()
            r = self.n[i]
            if r.free:
                continue
            if not (r.fl & PRIMITIVE):
                stack.extend(self.children(i))
            r.make_free()
            self.free.append(i)
            self.parent.pop(i, None)

    def alloc(self):
        if self.free:
            self.free.sort()
            i = self.free.pop(0)
        else:
            i = len(self.n)
            if i >= MAX_NODES:
                raise HsLimitError('no free syntax node (limit %d)' % MAX_NODES)
            self.n.append(Rec.unpack(FREE_RECORD))
            self.orig.append((0, 0, 0))
        return i

    def pool_empty_string(self):
        if self._empty_str is None:
            k = self.pool.find(b'\0')
            if k < 0:
                self.pool.append(0)
                k = len(self.pool) - 1
            self._empty_str = k
        return self._empty_str

    def pool_token(self, text):
        if text == 'print' and self._print_tok is not None:
            return self._print_tok
        b = text.encode('ascii')
        k = self.pool.find(b'\0' + b + b'\0')
        if k >= 0:
            k += 1
        else:
            self.pool.extend(b + b'\0')
            k = len(self.pool) - len(b) - 1
        if text == 'print':
            self._print_tok = k
        return k

    def warn(self, code, msg):
        self.svc.warn(code, msg)

    # ---- main entry ----
    def run(self):
        n = self.n
        used = [i for i, r in enumerate(n) if not r.free]
        # pass A: calls (group nodes)
        for i in used:
            r = n[i]
            if r.free or (r.fl & PRIMITIVE) or (r.fl & SCRIPT):
                continue
            self._convert_call(i)
        # pass B: leaves (constants / variables)
        for i in used:
            r = n[i]
            if r.free or not (r.fl & PRIMITIVE):
                continue
            if r.vt == VT_FUNCTION_NAME:
                continue                       # handled with its group
            if r.fl & VARIABLE:
                self._convert_variable(i)
            else:
                self._convert_constant(i)
        return self

    # ---- calls ----
    def _convert_call(self, g):
        n = self.n
        r = n[g]
        op = self.orig[g][0]
        plan = self.t.fn_plan.get(op)
        if plan is None:
            self.stats['fn_unknown_opcode'] += 1
            self.warn('hs_fn_unknown', 'function opcode %d is not in the MCC table' % op)
            self._to_noop(g)
            return
        if self.solo_mp_no_save and plan.name in SOLO_MP_SAVE_CALLS:
            self.stats['fn_solo_mp_save_noop'] += 1
            self._to_noop(g)
            return
        kids = self.children(g)
        # `set` onto an unmappable external global: drop the assignment
        if plan.name == 'set' and len(kids) >= 2:
            tv = n[kids[1]]
            if (tv.fl & VARIABLE) and (tv.fl & PRIMITIVE):
                des = struct.unpack('<h', bytes(tv.val[:2]))[0]
                if des < 0 and self.t.lookup_global(des & 0x7FFF, self.use_unverified) is None:
                    self.stats['set_dropped'] += 1
                    self.warn('hs_set_dropped', 'set of external global %s dropped (no Vista equivalent)' % self.t.global_name(des & 0x7FFF))
                    self._to_noop(g)
                    return
        if plan.kind in ('map', 'alias'):
            r.op = plan.vista_op
            if kids and n[kids[0]].vt == VT_FUNCTION_NAME and (n[kids[0]].fl & PRIMITIVE):
                n[kids[0]].op = plan.vista_op
            self.stats['fn_' + plan.kind] += 1
            if plan.kind == 'alias':
                self.stats['fn_alias_used:' + plan.name] += 1
            return
        # MCC-only
        self.stats['fn_mcc_only:' + plan.name] += 1
        self.warn('hs_fn_mcc_only', 'MCC-only function %s replaced (%s)' % (plan.name, plan.kind))
        if plan.kind == 'noop':
            self._to_noop(g)
        elif plan.kind == 'const':
            self._to_constant(g, self.t.vt_by_name.get(plan.ret, r.vt), plan.value)
        elif plan.kind == 'cmp_distance':
            self._to_constant(g, VT_REAL, self._distance_fail_open(g))
        else:
            raise AssertionError(plan.kind)

    def _distance_fail_open(self, g):
        """Constant for a real-valued MCC-only distance so that the enclosing comparison is true."""
        small, large = 0.0, 1.0e9
        par = self.parent.get(g)
        if par is None:
            return small
        pg, pos = par
        pop = self.orig[pg][0]
        pf = self.t.mcc['fn'].get(pop)
        name = pf['name'] if pf and not (self.orig[pg][1] & SCRIPT) else ''
        if name in ('<', '<='):
            return small if pos == 1 else large
        if name in ('>', '>='):
            return large if pos == 1 else small
        return small

    def _to_constant(self, g, ret_vt, value):
        """Group node -> primitive constant (the node keeps its index, salt and sibling link).

        The constant takes the type of the slot it sits in when that is numeric (exactly what a literal would be
        compiled to); otherwise the replaced function's own return type (the engine casts at run time)."""
        r = self.n[g]
        self.free_subtree(g)
        ct = r.vt if r.vt in (VT_BOOLEAN, VT_REAL, VT_SHORT, VT_LONG) else ret_vt
        r.fl = PRIMITIVE | PERMANENT
        r.op = ct
        if r.vt in (0, 1, 2, 3):
            r.vt = ct
        r.val[:] = pack_constant(ct, value)
        if ct == VT_STRING:
            r.sa = self.pool_empty_string()
            r.set_val_u32(r.sa)
        self.stats['to_constant'] += 1

    def _to_noop(self, g):
        """Group node -> (print "")."""
        n = self.n
        r = n[g]
        kids = self.children(g)
        name_i = kids[0] if kids and n[kids[0]].vt == VT_FUNCTION_NAME and (n[kids[0]].fl & PRIMITIVE) else None
        # free all descendants, then rebuild: group -> [name node] -> [string constant node]
        self.free_subtree(g)
        if name_i is None:
            name_i = self.alloc()
        else:
            self.free.remove(name_i)           # reuse the old name node's slot
        arg_i = self.alloc()
        for i in (name_i, arg_i):
            n[i].salt = n[i].salt or salt_for(i)
        tok = self.pool_token('print')
        # group
        r.op = self.t.print_op
        r.fl = PERMANENT                       # (vt stays: the slot type of the replaced void call)
        r.set_val_u32((n[name_i].salt << 16) | name_i)
        # function-name node
        nm = n[name_i]
        nm.op, nm.vt, nm.fl, nm.sa = self.t.print_op, VT_FUNCTION_NAME, PRIMITIVE | PERMANENT, tok
        nm.val[:] = b'\0\0\0\0'
        nm.nxt = (n[arg_i].salt << 16) | arg_i
        # string constant node
        a = n[arg_i]
        a.op, a.vt, a.fl, a.nxt = VT_STRING, VT_STRING, PRIMITIVE | PERMANENT, NONE32
        a.sa = self.pool_empty_string()
        a.set_val_u32(a.sa)
        # keep alloc bookkeeping consistent
        self.parent[name_i] = (g, 0)
        self.parent[arg_i] = (g, 1)
        self.stats['to_noop'] += 1

    # ---- leaves ----
    def _convert_variable(self, i):
        r = self.n[i]
        des = struct.unpack('<h', bytes(r.val[:2]))[0]
        if des >= 0:
            return                               # script global: index unchanged
        midx = des & 0x7FFF
        vidx = self.t.lookup_global(midx, self.use_unverified)
        if vidx is None:
            self.stats['ext_global_dropped'] += 1
            self.warn('hs_global_dropped', 'external global %s has no Vista equivalent; reads become a constant' % self.t.global_name(midx))
            gt = self.t.global_type(midx)
            vt = next((k for k, v in self.t.mcc['vt'].items() if v == gt), r.vt)
            if r.vt in (0, 1, 2, 3):
                r.vt = vt                        # an unparsed slot takes the global's own type
            r.fl = PRIMITIVE | PERMANENT
            r.op = vt
            r.val[:] = pack_constant(vt, default_constant(gt))
            return
        r.val[0:2] = struct.pack('<H', 0x8000 | vidx)
        self.stats['ext_global_mapped'] += 1
        if midx not in self.t.gl_verified:
            self.stats['ext_global_unverified'] += 1
            self.warn('hs_global_unverified', 'external global %s mapped by type alignment only (MCC %d -> Vista %d)' % (
                self.t.global_name(midx), midx, vidx))

    def _convert_constant(self, i):
        r = self.n[i]
        ct = r.op & 0xFFFF                   # constant type
        if ct == VT_STRING:
            r.set_val_u32(r.sa)              # the engine rebuilds the pointer from the source offset at load
            self.stats['const_string'] += 1
        elif ct == VT_STRING_ID:
            v = r.val_u32()
            if v:
                r.set_val_u32(self.svc.remap_sid(v))
            self.stats['const_string_id'] += 1
        elif VT_TAG_FIRST <= ct <= VT_TAG_LAST:
            v = r.val_u32()
            if v != NONE32:
                nv = self.svc.remap_datum(v)
                r.set_val_u32(nv)
                self.tag_order.setdefault(v, nv)
                self.stats['const_tagref_null' if nv == NONE32 else 'const_tagref'] += 1
                if nv == NONE32:
                    self.warn('hs_tagref_null', 'script constant tag %08X has no Vista equivalent' % v)
        else:
            self.stats['const_other'] += 1


# ------------------------------------------------------------------------------------------------
# validator (structure + function signatures); calibrated on the 43 stock Vista maps
# ------------------------------------------------------------------------------------------------
def validate_program(recs, root_datums, pool, table, n_scripts=0, check_types=True):
    """Check a syntax-node table.  `table` = HsTables.vista / HsTables.mcc (function signatures).

    root_datums: datums of the Scripts' root expressions and the Globals' initialization expressions.
    Returns collections.Counter {code: count} and a list of up to 50 examples [(code, node, message)]."""
    probs = collections.Counter()
    ex = []

    def bad(code, i, msg):
        probs[code] += 1
        if len(ex) < 50:
            ex.append((code, i, msg))

    n = len(recs)
    fn = table['fn']
    vt_names = table['vt']
    incoming = collections.Counter()

    def check_datum(d, owner, what):
        if d == NONE32:
            return None
        i = idx_of(d)
        if i >= n or recs[i].free:
            bad('dangling_' + what, owner, 'datum %08X -> free/out of range' % d)
            return None
        if recs[i].salt != (d >> 16):
            bad('salt_mismatch_' + what, owner, 'datum %08X vs salt %04X' % (d, recs[i].salt))
        incoming[i] += 1
        return i

    for i, r in enumerate(recs):
        if r.free:
            continue
        check_datum(r.nxt, i, 'next')
        if not (r.fl & PRIMITIVE):
            check_datum(r.val_u32(), i, 'child')
    for d in root_datums:
        if d != NONE32:
            i = idx_of(d)
            if i >= n or recs[i].free or recs[i].salt != (d >> 16):
                bad('bad_root', i, 'root datum %08X' % d)
            else:
                incoming[i] += 0
    roots = {idx_of(d) for d in root_datums if d != NONE32}
    for i, r in enumerate(recs):
        if r.free:
            continue
        if incoming[i] > 1:
            bad('multi_parent', i, 'node referenced %d times' % incoming[i])
        if incoming[i] == 0 and i not in roots:
            bad('orphan', i, 'unreferenced node')
        if r.fl & PRIMITIVE:
            if r.vt == VT_FUNCTION_NAME:
                if r.op not in fn and not (r.op < n_scripts):
                    bad('fname_unknown_op', i, 'op %d' % r.op)
            elif (r.fl & VARIABLE) and (r.fl & PRIMITIVE):
                des = struct.unpack('<h', bytes(r.val[:2]))[0]
                if des < 0 and (des & 0x7FFF) not in table['gl']:
                    bad('ext_global_out_of_range', i, 'designator %d' % des)
            elif r.op == VT_STRING and r.vt == VT_STRING:
                if r.sa >= len(pool) or pool.find(b'\0', r.sa) < 0:
                    bad('string_offset', i, 'sa %d pool %d' % (r.sa, len(pool)))
            continue
        # group
        kids = []
        c = r.val_u32()
        g = 0
        while c != NONE32 and idx_of(c) < n and not recs[idx_of(c)].free and g < 4096:
            kids.append(idx_of(c))
            c = recs[idx_of(c)].nxt
            g += 1
        if not kids:
            bad('empty_group', i, 'no children')
            continue
        k0 = recs[kids[0]]
        if not ((k0.fl & PRIMITIVE) and k0.vt == VT_FUNCTION_NAME):
            bad('group_no_name', i, 'first child is not a function name')
            continue
        if k0.op != r.op:
            bad('name_op_mismatch', i, 'group op %d child op %d' % (r.op, k0.op))
        if r.fl & SCRIPT:
            if not (0 <= r.op < max(n_scripts, 1)):
                bad('script_call_range', i, 'script index %d / %d' % (r.op, n_scripts))
            continue
        f = fn.get(r.op)
        if f is None:
            bad('unknown_function', i, 'op %d' % r.op)
            continue
        if not f['special']:
            if len(kids) - 1 != len(f['args']):
                bad('arity', i, '%s: %d args, expects %d' % (f['name'], len(kids) - 1, len(f['args'])))
            elif check_types:
                for k, (cid, want) in enumerate(zip(kids[1:], f['args'])):
                    got = vt_names.get(recs[cid].vt)
                    if got != want and want not in ('passthrough',):
                        bad('arg_type', cid, '%s arg %d: node type %s, declared %s' % (f['name'], k, got, want))
    return probs, ex


# ------------------------------------------------------------------------------------------------
# compaction (only when the table would exceed the Vista data-array limit)
# ------------------------------------------------------------------------------------------------
def compact_nodes(nodes):
    """Drop free slots.  Returns (new_nodes, index_map {old: new}) and rewrites next/first-child datums in place."""
    imap = {}
    out = []
    for i, r in enumerate(nodes):
        if not r.free:
            imap[i] = len(out)
            out.append(r)

    def fix(d):
        if d == NONE32:
            return d
        i = idx_of(d)
        return (d & 0xFFFF0000) | imap[i] if i in imap else NONE32

    for r in out:
        r.nxt = fix(r.nxt)
        if not (r.fl & PRIMITIVE):
            r.set_val_u32(fix(r.val_u32()))
    return out, imap, fix


# ------------------------------------------------------------------------------------------------
# scnr node handler
# ------------------------------------------------------------------------------------------------
def _field(node, name):
    f = node.field(name)
    if f is None:
        raise KeyError('scnr has no field %r' % name)
    return f


def _read_records(block_nodes):
    return [Rec.unpack(bytes(e.data)) for e in block_nodes]


def _make_elements(parent, fname, raws):
    """Build Node elements for block `fname` of `parent` from raw element byte strings."""
    f = _field(parent, fname)
    size = T.element_size(f)
    path = T.child_path(parent.path, f)
    out = []
    for raw in raws:
        if len(raw) != size:
            raise ValueError('%s element size %d != %d' % (fname, len(raw), size))
        out.append(T.Node(parent.group, path, f.children, size, raw))
    return f.offset, out


TRAINING_OFF_SCRIPT = 'disable_training'      # name of the synthesised startup script (see add_training_off_script)


def training_off_script_wanted(ctx):
    """True when the planner chose the SCRIPT route for 'training off' (plan.notes['training_off']['route'] == 'script': a multiplayer map
    whose globals are Vista's SHARED matg/hudg, which cannot be edited per map).  Stub contexts (tests) have no plan -> False."""
    notes = getattr(getattr(ctx, 'plan', None), 'notes', None)
    t = notes.get('training_off') if isinstance(notes, dict) else None
    return bool(isinstance(t, dict) and t.get('route') == 'script')


def add_training_off_script(conv, scripts_out, tables, ctx=None):
    """Append ONE startup script `disable_training` = (begin (hud_enable_training false)) to the converted program.

    Why (HOW_IT_WORKS.md, section 14): halo2.exe keeps a single master flag for the HUD tutorial ("training") prompts, byte +1765 of the 1768-byte
    'player training globals' block (module sub_4B3E55 / initialize_for_new_map sub_4B3E73 sets it to 1 at every map load);
    sub_4B62F8 (per-player state machine) and sub_4B6720 (the prompt frame/text draw) return at once when it is 0, and the script function
    hud_enable_training (Vista opcode 0x279 -> sub_4F0DE5 -> sub_4B428F) just stores its argument there.  A startup script (script type 0) is
    started by hs_runtime_initialize_for_new_map (sub_496BFD) and first evaluated in the tick loop (sub_496DF7), i.e. after every module's
    map initialisation, so it wins.  This switches the prompts off WITHOUT touching the shared hudg, so the map can keep Vista's shared
    matg and the custom-map list validator (halo2.exe sub_44F690, wants a shared matg) passes -> full list entry (description, preview,
    max teams per game type).

    Shapes copied from the compiled stock scripts (zanzibar/dune/needle): root = group node (opcode 0, type = return type, flags 8) whose first
    child is the function-name node of `begin` (opcode 0, type 2, flags 9); statement = group (opcode = function, flags 8) + function-name
    node (same opcode, flags 9) + constant (opcode = type = boolean 5, flags 9, value 00 FF FF FF).  Appended at the END of the Scripts block
    so every existing script index stays valid.  Returns the number of nodes added, 0 when it could not be done (a warning is raised)."""
    warn = getattr(ctx, 'warn', None) or (lambda code, msg: None)
    v = tables.vista['fn']
    op = next((o for o, f in v.items() if f['name'] == 'hud_enable_training' and f['args'] == ('boolean',)), None)
    if op is None:
        warn('hs_training_off_script', 'the Vista function table has no hud_enable_training(boolean): training prompts stay on')
        return 0
    if any(s[:len(TRAINING_OFF_SCRIPT)] == TRAINING_OFF_SCRIPT.encode('ascii') for s in scripts_out):
        return 0                                                    # idempotent
    if len(scripts_out) >= MAX_SCRIPTS:
        warn('hs_training_off_script', 'script limit %d reached: training prompts stay on' % MAX_SCRIPTS)
        return 0
    n = conv.n
    need = 5
    free = [i for i, r in enumerate(n) if r.free][:need]
    while len(free) < need:
        if len(n) >= MAX_NODES:
            warn('hs_training_off_script', 'no free syntax nodes (limit %d): training prompts stay on' % MAX_NODES)
            return 0
        n.append(Rec.unpack(FREE_RECORD))
        free.append(len(n) - 1)
    pool = conv.pool
    placeholder = len(pool) > 0 and pool.count(0xCD) == len(pool)       # script-less map: the pool is only the 0xCD filler
    if placeholder:
        del pool[:]
    t_begin, t_fn, t_false = conv.pool_token('begin'), conv.pool_token('hud_enable_training'), conv.pool_token('false')
    if placeholder:
        pool.extend(b'\xcd' * 1023)                                      # stock script-less-pool shape (needle/street_sweeper: tokens + 1023 x 0xCD)
    i_root, i_bfn, i_stmt, i_fn, i_const = free
    dat = lambda i: (salt_for(i) << 16) | i                              # noqa: E731
    mk = lambda i, o, vt, fl, nxt, sa, val: setattr_rec(n, i, Rec(salt_for(i), o, vt, fl, nxt, sa, val))   # noqa: E731
    mk(i_root, 0, VT_VOID, PERMANENT, NONE32, t_begin, struct.pack('<I', dat(i_bfn)))
    mk(i_bfn, 0, VT_FUNCTION_NAME, PERMANENT | PRIMITIVE, dat(i_stmt), t_begin, b'\0\0\0\0')
    mk(i_stmt, op, VT_VOID, PERMANENT, NONE32, t_fn, struct.pack('<I', dat(i_fn)))
    mk(i_fn, op, VT_FUNCTION_NAME, PERMANENT | PRIMITIVE, dat(i_const), t_fn, b'\0\0\0\0')
    mk(i_const, VT_BOOLEAN, VT_BOOLEAN, PERMANENT | PRIMITIVE, NONE32, t_false, pack_constant(VT_BOOLEAN, False))
    name = TRAINING_OFF_SCRIPT.encode('ascii')
    scripts_out.append(name + b'\0' * (0x20 - len(name)) + struct.pack('<HHI', 0, VT_VOID, dat(i_root)))
    return need


def setattr_rec(nodes, i, rec):
    nodes[i] = rec


def convert_scenario_scripts(ctx, mcc_node, vista_node, tables=None):
    """Rebuild the script blocks of `vista_node` (scnr main struct) from the MCC scnr node.

    Returns the ProgramConverter (carries .stats) so tests can inspect the result."""
    t = tables or get_tables()
    f_exp = _field(mcc_node, 'Script Expressions')
    f_pool = _field(mcc_node, 'Script String Data')
    f_scripts = _field(mcc_node, 'Scripts')
    f_globals = _field(mcc_node, 'Globals')
    f_refs = _field(mcc_node, 'Script References')

    nodes = _read_records(mcc_node.blocks.get(f_exp.offset, []))
    pool = bytes(mcc_node.datas.get(f_pool.offset) or b'')
    scripts_raw = [bytes(e.data) for e in mcc_node.blocks.get(f_scripts.offset, [])]
    globals_raw = [bytes(e.data) for e in mcc_node.blocks.get(f_globals.offset, [])]

    conv = ProgramConverter(nodes, pool, t, ctx).run()

    # --- fit the Vista data-array limit (compaction renumbers; only when needed) ---
    imap = None
    fix = None
    highest = max((i for i, r in enumerate(conv.n) if not r.free), default=-1) + 1
    if len(conv.n) > MAX_NODES:
        if highest <= MAX_NODES:
            del conv.n[highest:]                       # trailing free slots
        else:
            used = sum(1 for r in conv.n if not r.free)
            if used > MAX_NODES:
                raise HsLimitError('%d syntax nodes in use > Vista limit %d' % (used, MAX_NODES))
            conv.n, imap, fix = compact_nodes(conv.n)
            # keep some free slots for runtime script evaluation (console / debug commands)
            for _ in range(min(FREE_SLACK, MAX_NODES - len(conv.n))):
                conv.n.append(Rec.unpack(FREE_RECORD))
            ctx.warn('hs_compacted', 'script nodes compacted %d -> %d (Vista limit %d)' % (len(nodes), len(conv.n), MAX_NODES))
    if len(conv.pool) > MAX_POOL:
        raise HsLimitError('script string pool %d > %d bytes' % (len(conv.pool), MAX_POOL))

    def fix_root(raw, off):
        d = struct.unpack_from('<I', raw, off)[0]
        if fix is not None and d != NONE32:
            raw = bytearray(raw)
            struct.pack_into('<I', raw, off, fix(d))
            return bytes(raw)
        return raw

    scripts_out = [fix_root(r, 0x24) for r in scripts_raw]
    globals_out = [fix_root(r, 0x24) for r in globals_raw]
    if training_off_script_wanted(ctx):
        # training OFF for a map that keeps Vista's SHARED globals: a startup script instead of a hudg edit (see add_training_off_script)
        if add_training_off_script(conv, scripts_out, t, ctx):
            ctx.stats['hs:training_off_script'] += 1
            ctx.warn('hs_training_off_script', 'startup script %s = (hud_enable_training false) added: the tutorial prompts are off while the map '
                                               'keeps Vista\'s shared globals (full custom-map list entry); --training-via-hudg uses a hudg edit '
                                               'instead' % TRAINING_OFF_SCRIPT)

    # --- write the Vista blocks ---
    off, els = _make_elements(vista_node, 'Script Expressions', [r.pack() for r in conv.n])
    vista_node.blocks[off] = els
    off, els = _make_elements(vista_node, 'Scripts', scripts_out)
    vista_node.blocks[off] = els
    off, els = _make_elements(vista_node, 'Globals', globals_out)
    vista_node.blocks[off] = els
    # Script References = the unique tag datums used by tag-typed constants, in order of first use (verified on all
    # 41 stock Vista scenarios; the MCC list obeys the same rule).  Rebuilt so dropped tags disappear consistently.
    group_of = {}
    for e in mcc_node.blocks.get(f_refs.offset, []):
        group_of[struct.unpack_from('<I', e.data, 4)[0]] = bytes(e.data[0:4])
    ref_raws = []
    seen_refs = set()
    for mdat, ndat in conv.tag_order.items():
        if ndat == NONE32 or ndat in seen_refs:
            continue
        seen_refs.add(ndat)
        gw = group_of.get(mdat)
        if gw is None:
            gwf = getattr(ctx, 'mcc_group_word', None)
            gw = struct.pack('<I', gwf(mdat) if gwf is not None else NONE32)
        ref_raws.append(gw + struct.pack('<I', ndat))
    conv.stats['refs_out'] += len(ref_raws)
    conv.stats['refs_mcc'] += len(group_of)
    off, els = _make_elements(vista_node, 'Script References', ref_raws)
    vista_node.blocks[off] = els
    vista_node.datas[_field(vista_node, 'Script String Data').offset] = bytes(conv.pool)
    vista_node.datas[_field(vista_node, 'Script Syntax Data').offset] = b''
    # source files block (two hidden words): zero as in every compiled cache
    for f in vista_node.fields:
        if f.name == 'Source Files Block':
            vista_node.data[f.offset:f.offset + f.size] = b'\0' * f.size

    # keep the count / size words of the struct consistent with the replaced payloads (the encoder rewrites the
    # pointer words)
    for fname in ('Script Expressions', 'Scripts', 'Globals', 'Script References'):
        f = _field(vista_node, fname)
        struct.pack_into('<iI', vista_node.data, f.offset, len(vista_node.blocks[f.offset]), 0)
    for fname in ('Script String Data', 'Script Syntax Data'):
        f = _field(vista_node, fname)
        struct.pack_into('<iI', vista_node.data, f.offset, len(vista_node.datas[f.offset]), 0)

    for what, n, lim in (('scripts', len(scripts_out), MAX_SCRIPTS), ('globals', len(globals_out), MAX_GLOBALS),
                         ('script references', len(ref_raws), MAX_REFS)):
        if n > lim:
            ctx.warn('hs_limit', '%d %s exceed the Vista limit %d' % (n, what, lim))

    for k, v in conv.stats.items():
        ctx.stats['hs:' + k] += v
    ctx.stats['hs:nodes_in'] += len(nodes)
    ctx.stats['hs:nodes_out'] += len(conv.n)
    ctx.stats['hs:scripts'] += len(scripts_out)
    ctx.stats['hs:globals'] += len(globals_out)
    conv.index_map = imap
    return conv


@node_handler('scnr', '')
def _scnr_scripts(ctx, mcc_node, vista_node):
    convert_scenario_scripts(ctx, mcc_node, vista_node)
