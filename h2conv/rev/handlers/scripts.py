# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""HaloScript (hs) conversion for the scenario tag, REVERSE direction: Halo 2 Vista (cache v8) -> Halo 2 MCC (cache v13).

Inverse of handlers/scripts.py.

The compiled script lives in the same scnr blocks in both games (Script Expressions = the hs_syntax_node array, 0x14 B per node;
Script String Data = the token pool; Scripts; Globals; Script References) and the node format is identical, so the forward
conversion could keep every node index and datum.  The reverse does the same - an in-place rewrite of the node table:

  * function opcodes are remapped BY NAME + SIGNATURE from the Vista table (927 entries) to the MCC table (918 entries);
    Vista-only functions (HUD / network / debug leftovers MCC dropped) are replaced in place by a same-typed constant or by
    `(print "")` (void), exactly as the forward direction does for MCC-only functions;
  * external (engine) global designators (0x8000|index) are remapped for the 15 globals verified in real script pairs
    (Vista index -> MCC index, the inverse of the +8 shift); anything else is dropped (reads become a constant, `set` becomes
    `(print "")`) unless ctx.opts['hs_unverified_globals'] enables the type-sequence alignment;
  * tag-reference constants -> ctx.remap_datum, string_id constants -> ctx.remap_sid, string constants get value = pool
    offset (the MCC convention; the Vista tool stored a tool-heap pointer there);
  * Script References is rebuilt as "unique tag constants in order of first use" (the rule of every stock scenario);
  * free slots are rewritten to the MCC 0xBABA fill; MCC's node array holds 64,512 nodes (halo2.dll sub_1806E3330:
    data_array_new("script node", 64512, 20)), so no compaction is ever needed (Vista's own limit was 36,864).

The node-table machinery is the FORWARD ProgramConverter (handlers/scripts.py) driven with swapped tables: it never looks at
which game a node comes from.  RevHsTables exposes the source tables under the attribute names the converter expects
(`.mcc` = SOURCE tables = Vista, `.vista` = TARGET tables = MCC).

Public API: convert_scenario_scripts(ctx, vista_scnr_node, mcc_scnr_node) (also the node handler ('scnr', '')), RevHsTables /
get_tables(), ProgramConverter (= forward), validate_program (forward, pass tables.dst).
"""
import collections
import difflib
import struct

from . import node_handler
from . import _revmisc_common as C
from ... import tagtree as T
from ...handlers import scripts as FS

ProgramConverter = FS.ProgramConverter
Rec = FS.Rec
validate_program = FS.validate_program
NONE32 = FS.NONE32
MAX_NODES_MCC = 64512                  # halo2.dll sub_1806E3330: data_array_new("script node", 64512, 20)
MAX_POOL = FS.MAX_POOL
FREE_RECORD = FS.FREE_RECORD

# Vista name -> MCC name for functions whose twin was renamed (the inverse of FS.FUNCTION_ALIASES)
REV_ALIASES = {}
for _mcc_name, _vista_name in FS.FUNCTION_ALIASES.items():
    REV_ALIASES.setdefault(_vista_name, []).append(_mcc_name)

# Vista-only functions with a better fallback than the generic "same-typed zero / print ''" rule.
#   ('const', value)  replace the call by this constant of the call's return type      ('noop', None)  replace by (print "")
VISTA_ONLY_POLICY = {
    ('game_tick_rate', ('long',)): ('noop', None),
}


class RevFnPlan(FS.FnPlan):
    """FnPlan whose `vista_op` is the TARGET (MCC) opcode (the converter reads that attribute)."""
    __slots__ = ()


class RevHsTables(object):
    """Vista -> MCC remap tables for functions and external globals (same interface as FS.HsTables)."""

    def __init__(self, xml_vista=None, xml_mcc=None):
        self.src = FS.load_scripting_xml(xml_vista or FS.default_xml_vista())
        self.dst = FS.load_scripting_xml(xml_mcc or FS.default_xml_mcc())
        self.mcc = self.src                   # names the forward ProgramConverter expects: .mcc = source tables
        self.vista = self.dst                 #                                             .vista = target tables
        self.value_types_equal = self.src['vt'] == self.dst['vt']
        self.vt_by_name = {v: k for k, v in self.src['vt'].items()}
        self.fn_plan = {}
        self.fn_alias_rejected = []
        self._build_functions()
        self.gl_map = {}                      # VERIFIED: Vista external index -> MCC external index
        self.gl_verified = set()
        self.gl_aligned = {}                  # unverified candidates (type-sequence alignment)
        self._gl_names = {}
        self._build_globals()
        self.print_op = next(op for op, f in self.dst['fn'].items() if f['name'] == 'print' and f['args'] == ('string',))

    # ---- functions ----
    def _build_functions(self):
        sfn, dfn = self.src['fn'], self.dst['fn']
        sig = lambda f: (f['ret'], f['args'])
        dby = collections.defaultdict(list)
        for op in sorted(dfn):
            if dfn[op]['name']:
                dby[dfn[op]['name']].append(op)
        used = set()
        # pass 1: identical name + identical signature, k-th overload with k-th overload
        for op in sorted(sfn):
            f = sfn[op]
            for dop in dby.get(f['name'], ()):
                if dop not in used and sig(dfn[dop]) == sig(f):
                    used.add(dop)
                    self.fn_plan[op] = RevFnPlan('map', dop, None, f['name'], f['ret'], f['args'])
                    break
        # pass 2: explicit aliases with identical signature
        for op in sorted(sfn):
            if op in self.fn_plan:
                continue
            f = sfn[op]
            for tgt in REV_ALIASES.get(f['name'], ()):
                hit = None
                for dop in dby.get(tgt, ()):
                    if dop not in used and sig(dfn[dop]) == sig(f):
                        hit = dop
                        break
                if hit is not None:
                    used.add(hit)
                    self.fn_plan[op] = RevFnPlan('alias', hit, None, f['name'], f['ret'], f['args'])
                    break
            else:
                if f['name'] in REV_ALIASES:
                    self.fn_alias_rejected.append(f['name'])
        # pass 3: Vista-only functions -> policy / generic fallback by return type
        for op in sorted(sfn):
            if op in self.fn_plan:
                continue
            f = sfn[op]
            pol = VISTA_ONLY_POLICY.get((f['name'], f['args']))
            if pol is not None:
                kind, val = pol
            elif f['ret'] == 'void':
                kind, val = 'noop', None
            else:
                kind, val = 'const', FS.default_constant(f['ret'])
            self.fn_plan[op] = RevFnPlan(kind, None, val, f['name'], f['ret'], f['args'])

    # ---- external globals ----
    def _build_globals(self):
        sgl, dgl = self.src['gl'], self.dst['gl']
        st = [sgl[i]['type'] for i in range(len(sgl))]
        dt = [dgl[i]['type'] for i in range(len(dgl))]
        sm = difflib.SequenceMatcher(None, st, dt, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == 'equal':
                for k in range(i2 - i1):
                    self.gl_aligned[i1 + k] = j1 + k
        by_name = {g['name']: i for i, g in dgl.items()}
        for name, vidx in FS.VERIFIED_GLOBALS.items():
            didx = by_name.get(name)
            self._gl_names[vidx] = name
            if didx is None or vidx >= len(sgl) or didx >= len(dgl) or sgl[vidx]['type'] != dgl[didx]['type']:
                continue
            self.gl_map[vidx] = didx
            self.gl_verified.add(vidx)

    def lookup_global(self, vista_index, use_unverified=False):
        v = self.gl_map.get(vista_index)
        if v is None and use_unverified:
            v = self.gl_aligned.get(vista_index)
        return v

    def global_name(self, vista_index):
        g = self.src['gl'].get(vista_index)
        n = (g['name'] if g and g['name'] else None) or self._gl_names.get(vista_index)
        return n or '#%d' % vista_index

    def global_type(self, vista_index):
        g = self.src['gl'].get(vista_index)
        return g['type'] if g else None


_TABLES = {}


def get_tables(xml_vista=None, xml_mcc=None):
    xml_vista = xml_vista or FS.default_xml_vista()
    xml_mcc = xml_mcc or FS.default_xml_mcc()
    key = (xml_vista, xml_mcc)
    if key not in _TABLES:
        _TABLES[key] = RevHsTables(xml_vista, xml_mcc)
    return _TABLES[key]


# ------------------------------------------------------------------------------------------------
# scnr node handler
# ------------------------------------------------------------------------------------------------
def _field(node, name):
    f = node.field(name)
    if f is None:
        raise KeyError('scnr has no field %r' % name)
    return f


def _make_elements(parent, fname, raws):
    f = _field(parent, fname)
    size = T.element_size(f)
    path = T.child_path(parent.path, f)
    out = []
    for raw in raws:
        if len(raw) != size:
            raise ValueError('%s element size %d != %d' % (fname, len(raw), size))
        out.append(T.Node(parent.group, path, f.children, size, raw))
    return f.offset, out


def convert_scenario_scripts(ctx, vista_node, mcc_node, tables=None):
    """Rebuild the script blocks of `mcc_node` (the MCC scnr main struct) from the Vista scnr node.  Returns the
    ProgramConverter (carries .stats)."""
    t = tables or get_tables()
    f_exp = _field(vista_node, 'Script Expressions')
    f_pool = _field(vista_node, 'Script String Data')
    f_scripts = _field(vista_node, 'Scripts')
    f_globals = _field(vista_node, 'Globals')
    f_refs = _field(vista_node, 'Script References')

    nodes = [Rec.unpack(bytes(e.data)) for e in vista_node.blocks.get(f_exp.offset, [])]
    pool = bytes(vista_node.datas.get(f_pool.offset) or b'')
    scripts_raw = [bytes(e.data) for e in vista_node.blocks.get(f_scripts.offset, [])]
    globals_raw = [bytes(e.data) for e in vista_node.blocks.get(f_globals.offset, [])]

    conv = ProgramConverter(nodes, pool, t, ctx).run()

    # free slots: the MCC fill (the Vista tool leaves tokeniser leftovers in about half of them; the engine only tests salt != 0)
    for r in conv.n:
        if r.free:
            r.make_free()
    if len(conv.n) > MAX_NODES_MCC:
        used = sum(1 for r in conv.n if not r.free)
        highest = max((i for i, r in enumerate(conv.n) if not r.free), default=-1) + 1
        if used > MAX_NODES_MCC or highest > MAX_NODES_MCC:
            raise FS.HsLimitError('%d syntax nodes (highest used %d) > MCC limit %d' % (used, highest, MAX_NODES_MCC))
        del conv.n[MAX_NODES_MCC:]
    if len(conv.pool) > MAX_POOL:
        raise FS.HsLimitError('script string pool %d > %d bytes' % (len(conv.pool), MAX_POOL))

    off, els = _make_elements(mcc_node, 'Script Expressions', [r.pack() for r in conv.n])
    mcc_node.blocks[off] = els
    off, els = _make_elements(mcc_node, 'Scripts', scripts_raw)
    mcc_node.blocks[off] = els
    off, els = _make_elements(mcc_node, 'Globals', globals_raw)
    mcc_node.blocks[off] = els
    group_of = {}
    for e in vista_node.blocks.get(f_refs.offset, []):
        group_of[struct.unpack_from('<I', e.data, 4)[0]] = bytes(e.data[0:4])
    ref_raws = []
    seen_refs = set()
    for vdat, ndat in conv.tag_order.items():
        if ndat == NONE32 or ndat in seen_refs:
            continue
        seen_refs.add(ndat)
        gw = group_of.get(vdat)
        if gw is None:
            gwf = getattr(ctx, 'vista_group_word', None)
            gw = struct.pack('<I', gwf(vdat) if gwf is not None else NONE32)
        ref_raws.append(gw + struct.pack('<I', ndat))
    conv.stats['refs_out'] += len(ref_raws)
    conv.stats['refs_vista'] += len(group_of)
    off, els = _make_elements(mcc_node, 'Script References', ref_raws)
    mcc_node.blocks[off] = els
    mcc_node.datas[_field(mcc_node, 'Script String Data').offset] = bytes(conv.pool)
    mcc_node.datas[_field(mcc_node, 'Script Syntax Data').offset] = b''
    for f in mcc_node.fields:
        if f.name == 'Source Files Block':
            mcc_node.data[f.offset:f.offset + f.size] = b'\0' * f.size

    for fname in ('Script Expressions', 'Scripts', 'Globals', 'Script References'):
        f = _field(mcc_node, fname)
        struct.pack_into('<iI', mcc_node.data, f.offset, len(mcc_node.blocks[f.offset]), 0)
    for fname in ('Script String Data', 'Script Syntax Data'):
        f = _field(mcc_node, fname)
        struct.pack_into('<iI', mcc_node.data, f.offset, len(mcc_node.datas[f.offset]), 0)

    for k, v in conv.stats.items():
        C.stat(ctx, 'rev_hs:' + k, v)
    C.stat(ctx, 'rev_hs:nodes_in', len(nodes))
    C.stat(ctx, 'rev_hs:nodes_out', len(conv.n))
    C.stat(ctx, 'rev_hs:scripts', len(scripts_raw))
    C.stat(ctx, 'rev_hs:globals', len(globals_raw))
    return conv


@node_handler('scnr', '')
def _scnr_scripts(ctx, vista_node, mcc_node):
    convert_scenario_scripts(ctx, vista_node, mcc_node)
