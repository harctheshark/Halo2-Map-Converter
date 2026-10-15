# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Fields the Vista cache builder COMPUTES (MCC stores zero / something else) and systematic MCC-vs-Vista value
conventions that the generic transcoder cannot see.

Node handlers (after the generic conversion of one struct; mcc_node = MCC struct, vista_node = converted struct)
    hlmt ''                 Render-Only Node Flags 1-8 (+0xA4), Render-Only Section Flags 1-8 (+0xC4), Runtime Flags (+0xE4)
    effe ''                 Flags bit 2 (set when an event part creates a bloc / char / proj / jpt! / ligh)
    effe /Events, foot /Effects, jmad ''
                            drop list elements whose only reference was a dropped sound_remastered tag
Post pass
    predicted_resources     object Predicted Resources rebuilt from the OUTPUT render model + shader lists (the exact
                            Vista tool rule: 3812/3812 stock Vista objects, 3808/3808 converted MCC objects), every
                            other Predicted Resources / Predicted Bitmaps block cleaned (dangling + duplicate
                            entries removed)

Library (no ctx needed; used by the tests):
    compute_model_flags(ModelInfo) -> (node_flags[8], section_flags[8], runtime_flags)
    collect_model_info(hlmt, mode, phmo, namer, node_map_reader) -> ModelInfo
    object_predicted_list(render_model_datum, shader_lists) -> [(type, index, datum)]
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import struct

from . import node_handler, post_pass
from .. import tagtree as T

NODE_FLAG_WORDS = 8            # hlmt Render-Only Node Flags 1..8 = 256 bits
SECTION_FLAG_WORDS = 8
OFF_NODE_FLAGS = 0xA4
OFF_SECTION_FLAGS = 0xC4
OFF_RUNTIME_FLAGS = 0xE4
NULL = 0xFFFFFFFF

# nodes the engine uses by NAME (never render-only) - derived from the Vista corpus
SPECIAL_NODE_NAMES = ('physics_control', 'origin_marker')
# the LOD levels whose sections decide which nodes are needed outside rendering: L1..L3 (Super Low .. Medium)
USED_LOD_LEVELS = (0, 1, 2)


def _stat(ctx, key, n=1):
    st = getattr(ctx, 'stats', None)
    if st is not None:
        st[key] += n


def _warn(ctx, code, msg, tag=None):
    w = getattr(ctx, 'warn', None)
    if w is not None:
        w(code, msg, tag)


# ====================================================================================================
# small Node helpers (field access by plugin NAME so one code path serves MCC and Vista layouts)
# ====================================================================================================
_FMT = {'int8': '<b', 'uint8': '<B', 'enum8': '<B', 'flags8': '<B', 'int16': '<h', 'uint16': '<H', 'enum16': '<H',
        'flags16': '<H', 'int32': '<i', 'uint32': '<I', 'enum32': '<I', 'flags32': '<I', 'stringid': '<I',
        'datum': '<I', 'float32': '<f'}


def rd(node, name, default=None):
    f = node.field(name)
    if f is None:
        return default
    fmt = _FMT.get(f.type)
    if fmt is None:
        return default
    return struct.unpack_from(fmt, node.data, f.offset)[0]


def wr(node, name, value):
    f = node.field(name)
    if f is None:
        return False
    fmt = _FMT[f.type]
    if fmt in ('<B', '<H', '<I'):
        value &= (1 << (8 * struct.calcsize(fmt))) - 1
    struct.pack_into(fmt, node.data, f.offset, value)
    return True


def blk(node, name):
    """Elements of the (first) tagblock called `name`; [] when the field or block is missing."""
    f = node.field(name)
    if f is None or f.children is None:
        return []
    return node.blocks.get(f.offset, [])


def tagref_datum(node, name):
    """Datum of a tag reference field (8-byte {group, datum} or 4-byte datum-only)."""
    f = node.field(name)
    if f is None or f.type != 'tagref':
        return NULL
    off = f.offset + (4 if f.size == 8 else 0)
    return struct.unpack_from('<I', node.data, off)[0]


def new_element(parent, field):
    """A zeroed element Node for tagblock `field` of `parent`."""
    return T.Node(parent.group, T.child_path(parent.path, field), field.children, T.element_size(field))


def set_block(node, field, kids):
    """Replace the elements of tagblock `field` and keep the (non-authoritative) count word of the struct in step, so
    struct-level comparisons stay meaningful before the encoder rewrites the pointer words."""
    node.blocks[field.offset] = kids
    struct.pack_into('<i', node.data, field.offset, len(kids))


# ====================================================================================================
# 1. hlmt: Render-Only Node / Section Flags, Runtime Flags
# ====================================================================================================
class ModelInfo(object):
    """What the flag computation needs about one model.
    nodes        [(name, parent_index)]                        hlmt Nodes
    sections     [{'rigid': int, 'nmap': [node idx] | None}]   render-model sections (rigid = Rigid Node Index or -1)
    lod_sections set of section indices referenced by a region permutation at LOD level 0..2
    marker_nodes set of node indices carrying a render-model marker
    phys_nodes   set of node indices used by physics rigid bodies / node edges
    """
    __slots__ = ('nodes', 'sections', 'lod_sections', 'marker_nodes', 'phys_nodes', 'incomplete')

    def __init__(self):
        self.nodes = []
        self.sections = []
        self.lod_sections = set()
        self.marker_nodes = set()
        self.phys_nodes = set()
        self.incomplete = False        # a section needed by the rule has an unreadable node map


def _closure_to_roots(used, nodes):
    s = set(used)
    n = len(nodes)
    for u in list(used):
        p = nodes[u][1] if 0 <= u < n else -1
        guard = 0
        while p is not None and 0 <= p < n and p not in s and guard < 512:
            s.add(p)
            p = nodes[p][1]
            guard += 1
    return s


def used_nodes(info):
    """Set of node indices that are NOT render-only."""
    N = len(info.nodes)
    used = set()
    for si in info.lod_sections:
        if not 0 <= si < len(info.sections):
            continue
        s = info.sections[si]
        nm = s['nmap']
        if nm is not None:
            used.update(x for x in nm if 0 <= x < N)
        elif 0 <= s['rigid'] < N:
            used.add(s['rigid'])
    used.update(x for x in info.marker_nodes if 0 <= x < N)
    used.update(x for x in info.phys_nodes if 0 <= x < N)
    for i, (name, parent) in enumerate(info.nodes):
        if parent is None or parent < 0 or name in SPECIAL_NODE_NAMES:
            used.add(i)
    return _closure_to_roots(used, info.nodes)


def compute_model_flags(info):
    """(node_flags[8], section_flags[8], runtime_flags) exactly as the Vista tool writes them.

    node flags   bit i set = node i is render-only (not needed by sections of LOD L1-L3, markers, physics, nor an
                 ancestor of such a node, nor a root / 'physics_control' / 'origin_marker'); the unused bits >= node
                 count of the LAST word holding a node are set as well (N=1 -> 0xFFFFFFFE, 0, 0 ...; N=33 -> word 1
                 also has bits 33..63 set).  0 nodes -> all zero.
    section flags bit s set = the node map of section s contains a render-only node.
    runtime flags bit 0 'Contains Runtime Nodes' = (node count > 0)."""
    N = len(info.nodes)
    if N == 0:
        return [0] * NODE_FLAG_WORDS, [0] * SECTION_FLAG_WORDS, 0
    used = used_nodes(info)
    ro = set(i for i in range(N) if i not in used)
    nbits = 0
    for i in ro:
        nbits |= 1 << i
    n = min(N, 32 * NODE_FLAG_WORDS)
    words = (n + 31) // 32                      # padding bits fill only the LAST word that holds a node
    nbits |= ((1 << (32 * words)) - 1) ^ ((1 << n) - 1)
    node_flags = [(nbits >> (32 * w)) & 0xFFFFFFFF for w in range(NODE_FLAG_WORDS)]
    sbits = 0
    for si, s in enumerate(info.sections):
        if si >= 32 * SECTION_FLAG_WORDS:
            break
        nm = s['nmap']
        if nm is None:
            nm = [s['rigid']] if s['rigid'] >= 0 else []
        if any(x in ro for x in nm):
            sbits |= 1 << si
    sec_flags = [(sbits >> (32 * w)) & 0xFFFFFFFF for w in range(SECTION_FLAG_WORDS)]
    return node_flags, sec_flags, 1


def collect_model_info(hlmt, mode, phmo, namer, node_map_reader):
    """Gather a ModelInfo from decoded Nodes (hlmt required; mode / phmo may be None).
    namer(stringid) -> text of an hlmt node name; node_map_reader(section_node) -> [node idx] | None."""
    info = ModelInfo()
    for e in blk(hlmt, 'Nodes'):
        name = namer(rd(e, 'Name', 0))
        parent = rd(e, 'Parent Node Index', -1)
        info.nodes.append((name, parent))
    if mode is not None:
        for s in blk(mode, 'Sections'):
            try:
                nm = node_map_reader(s)
            except Exception:
                nm = None
            info.sections.append({'rigid': rd(s, 'Rigid Node Index', -1), 'nmap': nm})
        for region in blk(mode, 'Regions'):
            for perm in blk(region, 'Permutations'):
                lod_fields = [f for f in perm.fields if f.name and f.name.startswith('L') and 'Section Index' in f.name]
                lod_fields.sort(key=lambda f: f.offset)
                for k in USED_LOD_LEVELS:
                    if k < len(lod_fields):
                        si = struct.unpack_from('<h', perm.data, lod_fields[k].offset)[0]
                        if si >= 0:
                            info.lod_sections.add(si)
        for si in info.lod_sections:
            if 0 <= si < len(info.sections) and info.sections[si]['nmap'] is None and info.sections[si]['rigid'] < 0:
                info.incomplete = True
        for group in blk(mode, 'Marker Groups'):
            for m in blk(group, 'Markers'):
                ni = rd(m, 'Node Index', 255)
                if ni is not None and 0 <= ni < 255:
                    info.marker_nodes.add(ni)
    if phmo is not None:
        for rb in blk(phmo, 'Rigid Bodies'):
            ni = rd(rb, 'Node Index', -1)
            if ni is not None and ni >= 0:
                info.phys_nodes.add(ni)
        for ed in blk(phmo, 'Node Edges'):
            for nm in ('Node A Index', 'Node B Index'):
                ni = rd(ed, nm, -1)
                if ni is not None and ni >= 0:
                    info.phys_nodes.add(ni)
    return info


# ---- node maps (the tail of the section resource data: 'crsr' then one byte per hardware node) ---------
def parse_node_map(blkh, section_data_size, resource_data_size, node_map_size):
    """Node map of a geometry block: the `node_map_size` bytes after the LAST 'crsr' marker of the resource data
    (verified 9239/9239 MCC sections == the Vista section of the same render model; 3088/3088 Vista sections
    give the section flags)."""
    if blkh is None or len(blkh) < 12 or blkh[:4] != b'hklb':
        return None
    rds = blkh[8 + section_data_size:8 + section_data_size + resource_data_size]
    k = rds.rfind(b'crsr')
    if k < 0:
        return None
    return list(rds[k + 4:k + 4 + node_map_size])


def _section_geometry_fields(s):
    return (rd(s, 'Resource Block Offset', 0) & 0x3FFFFFFF, rd(s, 'Resource Block Size', 0),
            rd(s, 'Section Data Size', 0), rd(s, 'Resource Data Size', 0), rd(s, 'Node Map Size', 0))


class MccModelSource(object):
    """Reads the tags of one MCC hlmt: render model + physics model (decoded) and the section node maps (raw)."""

    def __init__(self, src, shared_caches, decode, string):
        """src: MccCache of the input map; shared_caches: [MccCache] (shared.map ...) for external tags;
        decode(cache, tag) -> Node; string(sid) -> str (strings of `src`)."""
        self.src = src
        self.shared = [c for c in shared_caches if c is not None]
        self.decode = decode
        self.string = string

    @classmethod
    def from_ctx(cls, ctx):
        sh = ctx.mcc_shared
        caches = [sh.get(k) for k in ('shared', 'single_player_shared', 'mainmenu')]
        try:
            src = ctx.cur_cache          # the MCC map the tag being converted comes from (datums/strings are relative to it)
        except Exception:                # a plan-less test context / no cur_src
            src = ctx.src
        return cls(src, caches, lambda cache, tag: ctx.decode_mcc(tag, cache=cache), ctx.mcc_string)

    def locate(self, datum, group):
        """(cache, tag) holding the data of MCC tag `datum`, or None."""
        if datum in (NULL, 0, None):
            return None
        t = self.src.tag_by_datum(datum)
        if t is None or t.group != group:
            return None
        if t.external:
            for c in self.shared:
                st = c.tags[t.index] if t.index < len(c.tags) else None
                if st is not None and st.datum == t.datum and st.group == t.group and st.has_data:
                    return c, st
            return None
        if not t.has_data:
            return None
        return self.src, t

    def node_of(self, datum, group):
        loc = self.locate(datum, group)
        if loc is None:
            return None, None
        return self.decode(loc[0], loc[1]), loc[0]

    def model_info(self, hlmt):
        mode, mcache = self.node_of(tagref_datum(hlmt, 'Render Model'), 'mode')
        phmo, _ = self.node_of(tagref_datum(hlmt, 'Physics Model'), 'phmo')

        def reader(section):
            off, size, sds, rds, nms = _section_geometry_fields(section)
            if nms <= 0:
                return []                     # Node Map Size 0: the section needs no node (NOT 'unknown')
            if size <= 0 or mcache is None:
                return None
            return parse_node_map(mcache.raw(off, size), sds, rds, nms)
        return collect_model_info(hlmt, mode, phmo, self.string, reader)


def apply_model_flags(vista_node, node_flags, sec_flags, runtime):
    for i in range(NODE_FLAG_WORDS):
        wr(vista_node, 'Render-Only Node Flags %d' % (i + 1), node_flags[i])
    for i in range(SECTION_FLAG_WORDS):
        wr(vista_node, 'Render-Only Section Flags %d' % (i + 1), sec_flags[i])
    wr(vista_node, 'Runtime Flags', runtime)


@node_handler('hlmt', '')
def hlmt_computed_flags(ctx, mcc_node, vista_node):
    """Recompute the three builder-computed flag sets (MCC stores zeros in ~90% of hlmt tags)."""
    try:
        info = MccModelSource.from_ctx(ctx).model_info(mcc_node)
        if info.incomplete:
            raise ValueError('a section node map is unreadable')
    except Exception as ex:                      # keep conversion going: safe fallback = nothing render-only
        _warn(ctx, 'hlmt_flags', 'model info failed (%s: %s) - no node marked render-only' % (type(ex).__name__, ex))
        info = ModelInfo()
        info.nodes = [(n, p) for n, p in ((ctx.mcc_string(rd(e, 'Name', 0)), rd(e, 'Parent Node Index', -1))
                                           for e in blk(mcc_node, 'Nodes'))]
        info.lod_sections = set()
        info.marker_nodes = set(range(len(info.nodes)))       # => every node 'used'
    nf, sf, rf = compute_model_flags(info)
    apply_model_flags(vista_node, nf, sf, rf)
    _stat(ctx, 'computed:hlmt_flags')


# ====================================================================================================
# 2. flag / value conventions found by the corpus scan
#    Not here (handled elsewhere): option-list differences known to the plugins (transcode + h2conv/data/enum_policy.json),
#    damage-reporting 42/43 (enum policy), jpt! Flags bit 2 (transcode built-in policy, found independently here:
#    1444/1444), vehi Phantom Shapes unused slots (handlers/havok.py, found independently here: 145/145 slots),
#    MCC-only game types / mulg / matg truncation / hudg donor values (handlers/gameplay.py), shader postprocess
#    (handlers/shaders.py).
# ====================================================================================================
# effe Flags: bit 0 is the only authored bit; bits 1-4 are filled by the cache builder.  MCC and Vista agree on bits 1, 3
# and 4 (8271/8272 stock pairs) but bit 2 differs in 561/8272 pairs.  Vista's tool sets bit 2 exactly when an event
# part creates a gameplay-relevant tag: 'bloc', 'char', 'proj', 'jpt!' or 'ligh' (Vista: 948/953 effects; applied to the
# MCC effects: 8190/8272 whole-flag matches vs 7712/8272 when the MCC value is copied).
EFFECT_BIT2_PART_GROUPS = (b'bloc', b'char', b'proj', b'jpt!', b'ligh')


def effect_needs_bit2(effe):
    """Vista rule for effe Flags bit 2 on a Vista-layout (or MCC-layout) effe Node."""
    for ev in blk(effe, 'Events'):
        for part in blk(ev, 'Parts'):
            f = part.field('Type')
            if f is None or f.type != 'tagref' or f.size != 8:
                continue
            word = bytes(part.data[f.offset:f.offset + 4])[::-1]          # fourcc as text
            if word in EFFECT_BIT2_PART_GROUPS and struct.unpack_from('<I', part.data, f.offset + 4)[0] != NULL:
                return True
    return False


@node_handler('effe', '')
def effe_flags(ctx, mcc_node, vista_node):
    f = vista_node.field('Flags')
    if f is None or f.type != 'flags32':
        return
    v = struct.unpack_from('<I', vista_node.data, f.offset)[0]
    nv = (v & ~4) | (4 if effect_needs_bit2(vista_node) else 0)
    if nv != v:
        struct.pack_into('<I', vista_node.data, f.offset, nv)
        _stat(ctx, 'computed:effe_flags_bit2')


# ====================================================================================================
# 2b. references to MCC-only 'sound_remastered' tags that the converter drops (remap -> NULL):
#     list elements that existed only to hold such a reference are removed, exactly like the Vista tool never wrote them
#     (corpus: effe event parts 559/619 differing events fixed, foot sounds 103/105, jmad sound references 44/44)
# ====================================================================================================
def _ref_datums(node, names=None):
    out = []
    for f in node.fields:
        if f.type == 'tagref' and (names is None or f.name in names):
            out.append(struct.unpack_from('<I', node.data, f.offset + (4 if f.size == 8 else 0))[0])
    return out


def _became_dangling(mel, vel, names=None):
    """True when the MCC element had a tag reference that is NULL in the converted element and the converted element
    has no other reference left (it only existed for the dropped tag)."""
    mr, vr = _ref_datums(mel, names), _ref_datums(vel, names)
    return bool(vr) and all(d == NULL for d in vr) and any(d != NULL for d in mr)


def _drop_dangling_elements(ctx, mcc_node, vista_node, block, stat, names=None):
    """Remove the converted elements of `block` whose only reference(s) were dropped by the remap.  The MCC and
    converted element lists are parallel (generic pass); a differing length means the work is already done."""
    mf, vf = mcc_node.field(block), vista_node.field(block)
    if mf is None or vf is None or mf.children is None or vf.children is None:
        return []
    mk, vk = mcc_node.blocks.get(mf.offset, []), vista_node.blocks.get(vf.offset, [])
    if len(mk) != len(vk) or not vk:
        return list(range(len(vk)))
    keep = [i for i in range(len(vk)) if not _became_dangling(mk[i], vk[i], names)]
    if len(keep) != len(vk):
        set_block(vista_node, vf, [vk[i] for i in keep])
        _stat(ctx, stat, len(vk) - len(keep))
    return keep


@node_handler('effe', '/Events')
def effe_event_dangling_parts(ctx, mcc_node, vista_node):
    """MCC effects carry extra snd! parts pointing at sound_remastered\* (lod / lfe / reflection variants); Vista has none."""
    _drop_dangling_elements(ctx, mcc_node, vista_node, 'Parts', 'computed:effe_parts_dropped', names=('Type',))


@node_handler('foot', '/Effects')
def foot_dangling_sounds(ctx, mcc_node, vista_node):
    _drop_dangling_elements(ctx, mcc_node, vista_node, 'Sounds', 'computed:foot_sounds_dropped')


@node_handler('jmad', '')
def jmad_dangling_sound_references(ctx, mcc_node, vista_node):
    """Sound References whose sound was a dropped sound_remastered tag are removed (corpus: always trailing, so the
    indices of the others do not move); Sound Events that pointed at a removed reference are removed and the rest
    renumbered, so no event can reach a stale or foreign slot."""
    f = vista_node.field('Sound References')
    mf = mcc_node.field('Sound References')
    if f is None or mf is None:
        return
    mk, vk = mcc_node.blocks.get(mf.offset, []), vista_node.blocks.get(f.offset, [])
    if len(mk) != len(vk) or not vk:
        return
    keep = [i for i in range(len(vk)) if not _became_dangling(mk[i], vk[i])]
    if len(keep) == len(vk):
        return
    newidx = {old: new for new, old in enumerate(keep)}
    set_block(vista_node, f, [vk[i] for i in keep])
    _stat(ctx, 'computed:jmad_sound_refs_dropped', len(vk) - len(keep))
    af = vista_node.field('Animations')
    for anim in vista_node.blocks.get(af.offset, []) if af is not None else []:
        sf = anim.field('Sound Events')
        if sf is None:
            continue
        evs = anim.blocks.get(sf.offset, [])
        out = []
        for e in evs:
            old = struct.unpack_from('<h', e.data, 0)[0]
            if old in newidx:
                struct.pack_into('<h', e.data, 0, newidx[old])
                out.append(e)
            elif old < 0:
                out.append(e)
        if len(out) != len(evs):
            set_block(anim, sf, out)
            _stat(ctx, 'computed:jmad_sound_events_dropped', len(evs) - len(out))


# ====================================================================================================
# 3. Predicted Resources
# ====================================================================================================
OBJECT_GROUPS = ('bipd', 'bloc', 'crea', 'ctrl', 'eqip', 'garb', 'lifi', 'mach', 'proj', 'scen', 'ssce', 'vehi', 'weap')
PREDICT_TYPE_BITMAP = 0
PREDICT_TYPE_SOUND = 1
PREDICT_TYPE_RENDER_MODEL_GEOMETRY = 2


def _pred_entries(node, field):
    """[(type, resource_index, datum)] of a 'Predicted Resources' block."""
    out = []
    for e in node.blocks.get(field.offset, []):
        out.append((struct.unpack_from('<h', e.data, 0)[0], struct.unpack_from('<h', e.data, 2)[0],
                    struct.unpack_from('<I', e.data, 4)[0]))
    return out


def object_predicted_list(render_model_datum, shader_lists):
    """The Vista tool's object-level list: (2, -1, render model) then the union, in material order, of the
    shaders' own Predicted Resources, first occurrence kept (verified exact on all 1406 Vista objects of the 43
    stock maps and on 1378/1384 MCC objects)."""
    out = [(PREDICT_TYPE_RENDER_MODEL_GEOMETRY, -1, render_model_datum)]
    seen = set(out)
    for lst in shader_lists:
        for ent in lst:
            if ent in seen:
                continue
            seen.add(ent)
            out.append(ent)
    return out


def _set_pred_block(node, field, entries):
    els = []
    for ty, ri, d in entries:
        e = new_element(node, field)
        struct.pack_into('<hhI', e.data, 0, ty, ri, d)
        els.append(e)
    set_block(node, field, els)


class OutputWorld(object):
    """Resolve an OUTPUT datum to a Vista-layout Node: a converted/donor tag of this map (converted[index]) or a
    tag of the Vista shared.map the map links against (index >= 10000)."""

    def __init__(self, ctx, converted):
        self.ctx = ctx
        self.converted = converted
        self._shared = {}

    def node(self, datum, group):
        if datum in (None, NULL):
            return None
        idx = datum & 0xFFFF
        n = self.converted.get(idx) if hasattr(self.converted, 'get') else None
        if n is not None:
            return n if n.group == group else None
        vista = getattr(self.ctx, 'vista', None)
        sh = getattr(vista, 'shared', None) if vista is not None else None
        if sh is None or idx >= len(sh.tags):
            return None
        key = idx
        if key not in self._shared:
            t = sh.tags[idx]
            self._shared[key] = (self.ctx.decode_vista(sh, t) if (t.group == group and t.addr) else None)
        n = self._shared[key]
        return n if n is not None and n.group == group else None


def rebuild_object_predicted(ctx, converted, world=None):
    """Rewrite the root 'Predicted Resources' block of every converted object tag from the output model chain.
    Returns (rebuilt, kept) counts.  Objects whose model chain cannot be read completely (no hlmt, no render model,
    a missing or zero-filled shader) keep their converted (and cleaned) list."""
    world = world or OutputWorld(ctx, converted)
    rebuilt = kept = 0
    shader_cache = {}
    for idx in list(converted.keys()):
        node = converted[idx]
        if node is None or node.group not in OBJECT_GROUPS:
            continue
        f = node.field('Predicted Resources')
        if f is None or f.children is None:
            continue
        hd = tagref_datum(node, 'Model')
        hlmt = world.node(hd, 'hlmt')
        if hlmt is None:
            kept += 1
            continue
        rmd = tagref_datum(hlmt, 'Render Model')
        if rmd == NULL:
            _set_pred_block(node, f, [])
            rebuilt += 1
            continue
        mode = world.node(rmd, 'mode')
        if mode is None:                           # render model not readable: keep the converted list (cleaned later)
            _stat(ctx, 'computed:predicted_incomplete')
            kept += 1
            continue
        lists = []
        complete = True
        for m in blk(mode, 'Materials'):
            sd = tagref_datum(m, 'Shader')
            if sd == NULL:
                continue
            if sd not in shader_cache:
                sn = world.node(sd, 'shad')
                sf = sn.field('Predicted Resources') if sn is not None else None
                # a real shader always has a Template; a zero-filled stand-in (MCC 'empty' tag) has none
                usable = sf is not None and tagref_datum(sn, 'Template') != NULL
                shader_cache[sd] = _pred_entries(sn, sf) if usable else None
            if shader_cache[sd] is None:
                complete = False
                break
            lists.append(shader_cache[sd])
        if not complete:                           # a shader is missing/empty: the old list holds more than we can derive
            _stat(ctx, 'computed:predicted_incomplete')
            kept += 1
            continue
        _set_pred_block(node, f, object_predicted_list(rmd, lists))
        rebuilt += 1
    return rebuilt, kept


def clean_predicted_blocks(converted):
    """Drop dangling (null tag) and duplicate entries of every 'Predicted Resources' / 'Predicted Bitmaps' block
    (stock Vista and MCC blocks contain neither).  Returns (blocks_cleaned, entries_removed)."""
    cleaned = removed = 0
    for node in converted.values():
        if node is None:
            continue
        for n in node.iter_nodes():
            for f in n.fields:
                if f.type != 'tagblock' or f.children is None or f.name not in ('Predicted Resources', 'Predicted Bitmaps'):
                    continue
                els = n.blocks.get(f.offset)
                if not els:
                    continue
                keep, seen = [], set()
                for e in els:
                    d = struct.unpack_from('<I', e.data, 4)[0]
                    key = bytes(e.data)
                    if d == NULL or key in seen:
                        continue
                    seen.add(key)
                    keep.append(e)
                if len(keep) != len(els):
                    set_block(n, f, keep)
                    cleaned += 1
                    removed += len(els) - len(keep)
    return cleaned, removed


@post_pass(prio=200)
def predicted_resources(ctx, converted):
    r, k = rebuild_object_predicted(ctx, converted)
    c, e = clean_predicted_blocks(converted)
    _stat(ctx, 'computed:predicted_objects_rebuilt', r)
    _stat(ctx, 'computed:predicted_objects_kept', k)
    _stat(ctx, 'computed:predicted_blocks_cleaned', c)
    _stat(ctx, 'computed:predicted_entries_removed', e)


def selftest():
    """Offline self checks of the pure functions."""
    info = ModelInfo()
    info.nodes = [('root', -1), ('a', 0), ('b', 0), ('c', 2)]
    info.sections = [{'rigid': 0, 'nmap': [0]}, {'rigid': -1, 'nmap': [0, 1, 2]}, {'rigid': -1, 'nmap': [0, 1, 2, 3]}]
    info.lod_sections = {0, 1}
    nf, sf, rf = compute_model_flags(info)
    assert rf == 1
    assert nf[0] == 0xFFFFFFFF ^ 0b0111 | 0b1000, hex(nf[0])        # node 3 render-only, bits >= 4 set
    assert sf[0] == 0b100, bin(sf[0])                                # section 2 holds node 3
    assert compute_model_flags(ModelInfo()) == ([0] * 8, [0] * 8, 0)
    assert object_predicted_list(7, [[(0, 0, 1), (0, 0, 2)], [(0, 0, 2), (0, 0, 3)]]) == \
        [(2, -1, 7), (0, 0, 1), (0, 0, 2), (0, 0, 3)]
    return True


if __name__ == '__main__':
    print('selftest', selftest())
