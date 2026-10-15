# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Assembly plugin (XML) tag-structure layouts for Halo 2 Vista and Halo 2 MCC.

Pure stdlib.

Public API
----------
load_layouts(game, corrected=False, extra=True)  -> dict[group_fourcc -> Layout]      game in {'vista','mcc'}
        'vista' = Plugins/Halo2 (98 groups).
        'mcc'   = Plugins/Halo2MCC override where present (35 groups), else Plugins/Halo2 (per Engines.xml fallbackPlugins).
        corrected=True applies CORRECTIONS (cache-accurate element sizes found by walking real maps).
parse_plugin(path, game)             -> Layout
Layout.fields                        -> list[Field]  (tree; tagblock fields carry .children and .element_size)
Layout.base_size / .computed_size / .tail_pad
walk_blocks(fields) / walk_fields(fields)
check_layout(layout)                 -> list of structural issues (overlap / gap / size mismatch)
diff_layouts(mcc_layout, vista_layout) -> {block_path: {...fields: [entries]}}   (use corrected layouts)
special_fields(layout, other)        -> list of fields that need non-trivial conversion

Cache sizes (all verified by sibling-offset gap analysis over 14k fields + real-map tiling, see docs)
-------------------------------------------------------------------------------------------------
int8/uint8/enum8/flags8 1 | int16/uint16/enum16/flags16 2 | int32/uint32/enum32/flags32/float32/degree/stringid/
datum/undefined 4 | uint64 8 | point2/vector2/degree2/rangef/ranged 8 | point3/vector3/degree3/plane2 12 |
vector4/plane3 16 | point16/range16 4 | quat16/rect16 8 | color32 4 | colorf 16 (alpha) or 12 | ascii/utf16 'size'
BYTES | tagref 8 (group,datum) or 4 when withGroup='false' | dataref 8 {size,addr} | tagblock 8 {count,addr}
| unicList 4*languages (9 -> 36).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass

import os
import re
import json
import sys
import xml.parsers.expat as expat

try:
    from . import paths as _paths
except ImportError:                     # run as a script / imported as a top-level module by the developer tools
    import paths as _paths


# Where the Assembly layout files are: h2conv.paths.assembly_layout() = --assembly-dir / H2CONV_ASSEMBLY / settings.json "assembly_dir",
# else the copy bundled in h2conv/data/assembly (Assembly 2026.02.16, GPL-3.0; see THIRD_PARTY_NOTICES.md).  Both an installed Assembly
# (Plugins, Formats) and the unpacked Assembly source (src/Assembly/Plugins, src/Blamite/Formats) are accepted.
def assembly():
    """The Assembly files in use (a paths.AssemblyLayout: .root, .plugins_vista, .plugins_mcc, .groupnames_xml, .scripting_vista_xml, .scripting_mcc_xml)."""
    return _paths.assembly_layout()


def plugin_dirs():
    a = _paths.assembly_layout()
    return {'vista': a.plugins_vista, 'mcc_override': a.plugins_mcc}


def groupnames_xml():
    return _paths.assembly_layout().groupnames_xml


def __getattr__(name):                  # the former module constants, for the developer tools that still read them
    if name == 'ASSEMBLY':
        return _paths.assembly_layout().root
    if name == 'PLUGIN_DIRS':
        return plugin_dirs()
    if name == 'GROUPNAMES_XML':
        return groupnames_xml()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------------------
# element type table.  key = lower-cased tag name.  value = (canonical type, size|None, kind)
# --------------------------------------------------------------------------------------
TYPE_SIZES = {
    'int8':    ('int8', 1, 'scalar'),   'uint8':   ('uint8', 1, 'scalar'),
    'int16':   ('int16', 2, 'scalar'),  'uint16':  ('uint16', 2, 'scalar'),
    'int32':   ('int32', 4, 'scalar'),  'uint32':  ('uint32', 4, 'scalar'),
    'uint64':  ('uint64', 8, 'scalar'),
    'float32': ('float32', 4, 'scalar'),
    'degree':  ('degree', 4, 'scalar'),
    'enum8':   ('enum8', 1, 'enum'),    'enum16':  ('enum16', 2, 'enum'),   'enum32':  ('enum32', 4, 'enum'),
    'flags8':  ('flags8', 1, 'flags'),  'flags16': ('flags16', 2, 'flags'), 'flags32': ('flags32', 4, 'flags'),
    'stringid': ('stringid', 4, 'scalar'),
    'datum':   ('datum', 4, 'scalar'),
    'undefined': ('undefined', 4, 'opaque'),
    'point2':  ('point2', 8, 'vec'),    'point3':  ('point3', 12, 'vec'),
    'vector2': ('vector2', 8, 'vec'),   'vector3': ('vector3', 12, 'vec'),  'vector4': ('vector4', 16, 'vec'),
    'degree2': ('degree2', 8, 'vec'),   'degree3': ('degree3', 12, 'vec'),
    'plane2':  ('plane2', 12, 'vec'),   'plane3':  ('plane3', 16, 'vec'),
    'point16': ('point16', 4, 'vec'),   'quat16':  ('quat16', 8, 'vec'),
    'rect16':  ('rect16', 8, 'vec'),
    'range16': ('range16', 4, 'vec'),   'rangef':  ('rangef', 8, 'vec'),    'ranged':  ('ranged', 8, 'vec'),
    'color32': ('color32', 4, 'color'),
    'colorf':  ('colorf', None, 'color'),
    'ascii':   ('ascii', None, 'string'),
    'utf16':   ('utf16', None, 'string'),
    'tagref':  ('tagref', None, 'ref'),
    'dataref': ('dataref', 8, 'data'),
    'tagblock': ('tagblock', 8, 'block'),
    'unicList': ('unicList', None, 'special'),
}
TYPE_SIZES_LC = {k.lower(): v for k, v in TYPE_SIZES.items()}
IGNORED = {'comment', 'revisions', 'revision', 'option', 'bit'}


def _int(s):
    s = s.strip()
    return int(s, 16) if s.lower().startswith('0x') else int(s)


class Field:
    __slots__ = ('type', 'name', 'offset', 'size', 'line', 'attrs', 'children', 'element_size',
                 'computed_element_size', 'options', 'kind', 'raw_tag', 'plugin_element_size', 'correction')

    def __init__(self):
        self.children = None
        self.element_size = None
        self.computed_element_size = None
        self.options = None
        self.attrs = {}
        self.raw_tag = None
        self.plugin_element_size = None
        self.correction = None
        self.line = 0

    @property
    def end(self):
        return self.offset + self.size

    def to_json(self, with_options=True):
        d = {'off': self.offset, 'name': self.name, 'type': self.type, 'size': self.size, 'line': self.line}
        if self.attrs:
            d['attrs'] = self.attrs
        if with_options and self.options:
            d['opts'] = self.options
        if self.children is not None:
            d['element_size'] = self.element_size
            d['computed_element_size'] = self.computed_element_size
            if self.plugin_element_size is not None:
                d['plugin_element_size'] = self.plugin_element_size
                d['correction'] = self.correction
            d['children'] = [c.to_json(with_options) for c in self.children]
        return d


class Layout:
    def __init__(self):
        self.group = None      # fourcc, e.g. 'bipd', '*cen'
        self.group_name = None
        self.file = None
        self.game = None       # 'vista' | 'mcc-override' | 'mcc-fallback'
        self.base_size = None
        self.computed_size = None
        self.fields = []
        self.plugin_game_attr = None
        self.tail_pad = 0      # constant zero tail after all declared data (see TAIL_PAD)
        self.corrections = []

    def to_json(self, with_options=True):
        d = {'group': self.group, 'group_name': self.group_name, 'file': self.file, 'source': self.game,
             'base_size': self.base_size, 'computed_size': self.computed_size}
        if self.tail_pad:
            d['tail_pad'] = self.tail_pad
        if self.corrections:
            d['corrections'] = self.corrections
        d['fields'] = [f.to_json(with_options) for f in self.fields]
        return d


class _Node:
    __slots__ = ('tag', 'attrs', 'children', 'line')

    def __init__(self, tag, attrs, line):
        self.tag = tag
        self.attrs = attrs
        self.children = []
        self.line = line


def _parse_xml(path):
    with open(path, 'rb') as fh:
        data = fh.read()
    p = expat.ParserCreate()
    stack = [_Node('#root', {}, 0)]

    def start(name, attrs):
        n = _Node(name, dict(attrs), p.CurrentLineNumber)
        stack[-1].children.append(n)
        stack.append(n)

    def end(name):
        stack.pop()

    p.StartElementHandler = start
    p.EndElementHandler = end
    p.Parse(data, True)
    return stack[0].children[0]


def _field_from_node(n):
    lc = n.tag.lower()
    ent = TYPE_SIZES_LC.get(lc)
    if ent is None:
        raise ValueError('unknown element <%s> line %d' % (n.tag, n.line))
    ctype, size, kind = ent
    f = Field()
    f.raw_tag = n.tag
    f.type = ctype
    f.kind = kind
    f.line = n.line
    f.name = n.attrs.get('name', '')
    f.offset = _int(n.attrs['offset'])
    a = {}
    if ctype == 'colorf':
        alpha = n.attrs.get('alpha', 'true').lower() == 'true'
        size = 16 if alpha else 12
        a['alpha'] = alpha
    elif ctype == 'color32':
        a['alpha'] = n.attrs.get('alpha', 'true').lower() == 'true'
    elif ctype == 'ascii':
        size = _int(n.attrs['size'])
    elif ctype == 'utf16':
        size = _int(n.attrs['size'])            # 'size' is BYTES (gap probe: size=64 -> next field +64; 87 fields agree)
        a['chars'] = size // 2
    elif ctype == 'tagref':
        wg = n.attrs.get('withGroup', 'true').lower() != 'false'
        size = 8 if wg else 4
        a['withGroup'] = wg
    elif ctype == 'unicList':
        size = 4 * _int(n.attrs.get('languages', '9'))   # 9 x {int16 range start, int16 range size}; unic baseSize 0x34 = 0x10 + 0x24
        a['languages'] = _int(n.attrs.get('languages', '9'))
    f.size = size
    for k in ('align', 'format', 'sort'):
        if k in n.attrs:
            a[k] = n.attrs[k]
    f.attrs = a
    if kind in ('enum', 'flags'):
        opts = []
        for c in n.children:
            if c.tag == 'option':
                opts.append([c.attrs.get('name', ''), _int(c.attrs['value'])])
            elif c.tag == 'bit':
                opts.append([c.attrs.get('name', ''), _int(c.attrs['index'])])
        f.options = opts
    if ctype == 'tagblock':
        f.element_size = _int(n.attrs['elementSize'])
        f.children = _children_fields(n)
        f.computed_element_size = _compute_extent(f.children)
    return f


def _children_fields(node):
    out = []
    for c in node.children:
        if c.tag.lower() in IGNORED or c.tag in IGNORED:
            continue
        out.append(_field_from_node(c))
    return out


def _compute_extent(fields):
    end = 0
    for f in fields:
        end = max(end, f.end)
    return end


def parse_plugin(path, game):
    root = _parse_xml(path)
    lay = Layout()
    lay.file = os.path.basename(path)
    lay.game = game
    lay.plugin_game_attr = root.attrs.get('game')
    lay.base_size = _int(root.attrs['baseSize'])
    lay.fields = _children_fields(root)
    lay.computed_size = _compute_extent(lay.fields)
    return lay


# --------------------------------------------------------------------------------------
# group name <-> file stem mapping  (file names replace * / < > with '_' and strip trailing spaces)
# --------------------------------------------------------------------------------------
def _load_group_names():
    import xml.etree.ElementTree as ET
    t = ET.parse(groupnames_xml())
    out = {}
    for g in t.getroot().iter('group'):
        out[g.get('magic')] = g.get('name')
    return out


def _stem_to_magic_map():
    names = _load_group_names()
    m = {}
    for mg in names:
        stem = re.sub(r'[*/<>:"|?\\]', '_', mg).rstrip()
        m.setdefault(stem, []).append(mg)
    return m, names


# project-local plugin XMLs for the tag groups the Assembly plugin set has NO plugin for (wind, hud#, ...), same XML syntax.
# load_layouts merges them for groups that have no Assembly plugin in the requested game (an Assembly plugin always wins).
# Provenance of every file: h2conv/data/plugins_extra/README.txt.
EXTRA_DIR = os.path.join(_paths.data_dir(), 'plugins_extra')


def load_layouts(game, corrected=False, extra=True):
    """game: 'vista' -> Halo2 plugins (98).  'mcc' -> Halo2MCC override else Halo2 fallback (98 effective).
    extra=True (default) additionally merges h2conv/data/plugins_extra/*.xml for groups with no plugin (Layout.game == 'extra');
    extra=False returns exactly the Assembly plugin set."""
    stems, names = _stem_to_magic_map()
    pdirs = plugin_dirs()
    out = {}

    def add(path, src):
        stem = os.path.splitext(os.path.basename(path))[0]
        cands = stems.get(stem, [stem])
        magic = cands[0]
        if len(cands) > 1:
            exact = [c for c in cands if c == stem]
            if exact:
                magic = exact[0]
        lay = parse_plugin(path, src)
        lay.group = magic
        lay.group_name = names.get(magic)
        out[magic] = lay

    for fn in sorted(os.listdir(pdirs['vista'])):
        if fn.lower().endswith('.xml'):
            add(os.path.join(pdirs['vista'], fn), 'vista' if game == 'vista' else 'mcc-fallback')
    if game == 'mcc':
        for fn in sorted(os.listdir(pdirs['mcc_override'])):
            if fn.lower().endswith('.xml'):
                add(os.path.join(pdirs['mcc_override'], fn), 'mcc-override')
    if extra and os.path.isdir(EXTRA_DIR):
        for fn in sorted(os.listdir(EXTRA_DIR)):
            if not fn.lower().endswith('.xml'):
                continue
            stem = os.path.splitext(fn)[0]
            cands = stems.get(stem, [stem])
            magic = next((c for c in cands if c == stem), cands[0])
            if magic in out:
                continue                       # an Assembly plugin is never replaced
            lay = parse_plugin(os.path.join(EXTRA_DIR, fn), 'extra')
            lay.group = magic
            lay.group_name = names.get(magic)
            out[magic] = lay
    if corrected:
        apply_corrections(out, game)
    return out


# --------------------------------------------------------------------------------------
# CORRECTIONS: cache-accurate fixes proven by walking real maps.
# --------------------------------------------------------------------------------------
_VB_FIELDS = [   # engine rasterizer_vertex_buffer (Blam/Engine/rasterizer/rasterizer_vertex_buffers.h), 32 bytes
    ('int8', 'Declaration', 0x00), ('uint8', 'Stride', 0x01), ('uint16', 'Count', 0x02),
    ('int32', 'Actual Vertex Offset Bytes', 0x04), ('int32', 'Default Vertex Offset Bytes', 0x08),
    ('uint32', 'Vertex Data Pointer', 0x0C), ('int32', 'Unknown', 0x10), ('uint32', 'Hardware Format Pointer', 0x14),
    ('uint32', 'Unknown', 0x18), ('uint32', 'Unknown', 0x1C)]

CORRECTIONS = [
    {'id': 'V1', 'game': 'vista', 'group': 'coll', 'block': 'Regions/Permutations/BSP Physics',
     'element_size': 0x70, 'truncate_at': 0x70,
     'evidence': 'Plugin elementSize 0x74. In every Vista map with count>=2 the MOPP-code dataref data of element 0 sits at '
                 'block+count*0x70 (counts 1,2,3,11,12,17 seen: 0x70,0xE0,0x150,...,0x770) and element 1 read at stride 0x74 '
                 'is garbage; with 0x70 all 2,051 coll tag instances in 43 Vista maps tile exactly. The trailing "Unknown"@0x70 does not exist.'},
    {'id': 'V2', 'game': 'vista', 'group': 'sbsp', 'block': 'Instanced Geometry Definitions/BSP Physics',
     'element_size': 0x70, 'truncate_at': 0x70,
     'evidence': 'Same as V1: sbsp instanced-geometry BSP Physics MOPP data starts at block+0x70 (29/29 elements in headlong, '
                 'sbsp tiles with 0x70 in all 43 Vista maps).'},
    {'id': 'V3', 'game': ('vista', 'mcc'), 'group': '*', 'groups': ('ltmp', 'sbsp'), 'block_name': 'Vertex Buffers',
     'element_size': 0x20,
     'evidence': 'Plugin elementSize 4 (uint32). Vista 03a_oldmombasa/03b_newmombasa and MCC 07b_forerunnership ltmp Clusters/'
                 'Cache Data: count 5 at +0x8A14, 160 bytes of populated data = 5 x 32 (declaration 01,17,19,1D,2F; stride '
                 '0C,08,24,08,0C) = engine rasterizer_vertex_buffer (32 B). With 0x20 every failing ltmp tag tiles. '
                 '(Count is 0 in every other map, so it only matters for those.)'},
    {'id': 'V4', 'game': ('vista',), 'group': 'dc*s', 'block': 'Decorators/Cache Blocks/Cache Block Data',
     'element_size': 0x88, 'dcs_fix': True,
     'evidence': 'Plugin elementSize 0x50. In 29 Vista dc*s tags (03a/03b mombasa, spacestation, ...) the 80-byte element is followed by '
                 'a 56-byte hole before Placements; element is 0x88 and holds a SECOND (count,ptr) pair set at +0x48 (48-byte '
                 'elements = sprite vertices) and +0x50 (2-byte indices); the plugin blocks at +0x2C/+0x34 are never populated '
                 '(0 of 1126+ non-empty elements). With 0x88 + blocks@0x48/0x50 all 29 tags tile. MCC maps contain no dc*s tags.'},
]

# Constant zero(ish) tail that follows all declared data of a tag but is referenced by NO plugin field.
TAIL_PAD = {
    ('vista', 'vehi'): {'bytes': 432,
                        'evidence': '225/225 Vista vehi tag instances in 43 maps end with exactly 432 B after the Phantom Shapes block; '
                                    'no u32 in the tag points into it; all zero except 5 of the 225'},
    ('mcc', 'vehi'): {'bytes': 480,
                      'evidence': '233/233 MCC stock vehi tag instances (43 maps) + 20/20 in the custom headlong end with '
                                  'exactly 480 B after all declared data; unreferenced; all zero except 5 of 233 (stock) and 1 of 20 (custom)'},
}


def _mk_filler(off, n):
    k = Field()
    k.type = 'undefined'
    k.kind = 'opaque'
    k.name = 'Unknown'
    k.offset = off
    k.size = n
    k.raw_tag = 'undefined'
    return k


def _fill_gaps(children, size):
    """Insert 4-byte 'undefined' placeholders into every uncovered hole so the corrected element tiles [0,size)."""
    out = sorted(children, key=lambda x: x.offset)
    res = []
    cur = 0
    for c in out:
        while cur < c.offset:
            res.append(_mk_filler(cur, 4))
            cur += 4
        res.append(c)
        cur = c.end
    while cur < size:
        res.append(_mk_filler(cur, 4))
        cur += 4
    return res


def _apply_dcs_fix(f):
    """V4: dc*s Cache Block Data = 0x88 bytes; sprite vertex/index blocks live at +0x48/+0x50."""
    sprite_vertices = None
    sprite_indices = None
    for c in f.children:
        if c.children is not None and c.name == 'Sprite Vertices':
            sprite_vertices = c
        elif c.children is not None and c.name == 'Sprite Indices':
            sprite_indices = c
    keep = [c for c in f.children if c.offset not in (0x2C, 0x34) and not (0x40 <= c.offset < 0x50)]
    if sprite_vertices is not None:
        sprite_vertices.offset = 0x48
        sprite_vertices.correction = 'V4'
        keep.append(sprite_vertices)
    if sprite_indices is not None:
        sprite_indices.offset = 0x50
        sprite_indices.correction = 'V4'
        keep.append(sprite_indices)
    # drop leftovers that now overlap the moved blocks
    keep = [c for c in keep if not (c.children is None and 0x48 <= c.offset < 0x58)]
    f.children = _fill_gaps(keep, 0x88)


def _find_block(fields, path_parts):
    cur = fields
    f = None
    for part in path_parts:
        f = None
        for c in cur:
            if c.children is not None and c.name == part:
                f = c
                break
        if f is None:
            return None
        cur = f.children
    return f


def _all_blocks_named(fields, name):
    for _p, f in walk_blocks(fields):
        if f.name == name:
            yield f


def apply_corrections(layouts, game):
    for c in CORRECTIONS:
        games = c['game'] if isinstance(c['game'], (tuple, list)) else (c['game'],)
        if game not in games:
            continue
        targets = []
        if 'block' in c:
            lay = layouts.get(c['group'])
            if lay is not None:
                f = _find_block(lay.fields, c['block'].split('/'))
                if f is not None:
                    targets.append((lay, f))
        else:
            for g in c['groups']:
                lay = layouts.get(g)
                if lay is None:
                    continue
                for f in _all_blocks_named(lay.fields, c['block_name']):
                    targets.append((lay, f))
        for lay, f in targets:
            if f.plugin_element_size is None:
                f.plugin_element_size = f.element_size
            f.correction = c['id']
            f.element_size = c['element_size']
            if 'truncate_at' in c:
                f.children = [x for x in f.children if x.offset < c['truncate_at']]
            if c['id'] == 'V3':
                kids = []
                for (t, n, off) in _VB_FIELDS:
                    k = Field()
                    k.type = t
                    k.kind = 'scalar'
                    k.name = n
                    k.offset = off
                    k.size = TYPE_SIZES[t][1]
                    k.raw_tag = t
                    kids.append(k)
                f.children = kids
            if c.get('dcs_fix'):
                _apply_dcs_fix(f)
            f.computed_element_size = _compute_extent(f.children)
            lay.corrections.append(c['id'])
    for g, lay in layouts.items():
        tp = TAIL_PAD.get((game, g))
        if tp:
            lay.tail_pad = tp['bytes']


# --------------------------------------------------------------------------------------
# structural checks
# --------------------------------------------------------------------------------------
def check_struct(fields, declared, path, issues, scope_label):
    """declared size vs computed; overlaps; gaps.  Appends dicts to issues."""
    fs = sorted(fields, key=lambda f: (f.offset, f.size))
    end = 0
    for f in fs:
        if f.offset < end:
            issues.append({'kind': 'overlap', 'path': path, 'scope': scope_label,
                           'detail': '%s@0x%X(%d) starts before previous end 0x%X' % (f.name, f.offset, f.size, end)})
        elif f.offset > end:
            issues.append({'kind': 'gap', 'path': path, 'scope': scope_label,
                           'detail': 'gap 0x%X..0x%X (%d bytes) before %s' % (end, f.offset, f.offset - end, f.name)})
        end = max(end, f.end)
    if end > declared:
        issues.append({'kind': 'exceeds_declared', 'path': path, 'scope': scope_label,
                       'detail': 'computed extent 0x%X > declared 0x%X' % (end, declared)})
    elif end < declared:
        issues.append({'kind': 'trailing_gap', 'path': path, 'scope': scope_label,
                       'detail': 'computed extent 0x%X < declared 0x%X (%d bytes uncovered)' % (end, declared, declared - end)})
    for f in fields:
        if f.children is not None:
            check_struct(f.children, f.element_size, path + '/' + f.name, issues, scope_label)


def check_layout(lay):
    issues = []
    check_struct(lay.fields, lay.base_size, lay.group, issues, lay.file)
    return issues


def walk_blocks(fields, path=''):
    """yield (path, block_field)"""
    for f in fields:
        if f.children is not None:
            p = path + '/' + f.name if path else f.name
            yield p, f
            yield from walk_blocks(f.children, p)


def walk_fields(fields, path=''):
    """yield (path, field) for every leaf and block field."""
    for f in fields:
        yield path, f
        if f.children is not None:
            p = path + '/' + f.name if path else f.name
            yield from walk_fields(f.children, p)


# --------------------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------------------
_GENERIC_NAMES = ('unknown', 'padding', 'pad', 'undefined', '')


def _norm_name(n):
    """Name key for matching: case-insensitive, whitespace removed ('BSP3D Nodes' == 'BSP 3D Nodes')."""
    return re.sub(r'\s+', '', n.strip().lower())


def _lcs_pairs(a_keys, b_keys):
    """Longest-common-subsequence alignment; returns list of (i, j) matched index pairs."""
    n, m = len(a_keys), len(b_keys)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        ai = a_keys[i]
        row, nxt = dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            if ai == b_keys[j]:
                row[j] = nxt[j + 1] + 1
            else:
                row[j] = nxt[j] if nxt[j] >= row[j + 1] else row[j + 1]
    i = j = 0
    out = []
    while i < n and j < m:
        if a_keys[i] == b_keys[j]:
            out.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return out


def _entry(m, v, status):
    e = {'status': status, '_m': m, '_v': v}
    if m is not None:
        e['mcc_offset'] = m.offset
        e['name'] = m.name
        e['type'] = m.type
        e['size'] = m.size
    else:
        e['mcc_offset'] = None
    if v is not None:
        e['vista_offset'] = v.offset
        if m is None:
            e['name'] = v.name
            e['type'] = v.type
            e['size'] = v.size
    else:
        e['vista_offset'] = None
    if m is not None and v is not None:
        if m.type != v.type:
            e['vista_type'] = v.type
        if m.size != v.size:
            e['vista_size'] = v.size
        if m.name != v.name:
            e['vista_name'] = v.name
        if m.options is not None and v.options is not None:
            mo = {tuple(x) for x in m.options}
            vo = {tuple(x) for x in v.options}
            only_m = sorted(mo - vo, key=lambda x: x[1])
            only_v = sorted(vo - mo, key=lambda x: x[1])
            if only_m or only_v:
                e['options_only_mcc'] = [list(x) for x in only_m]
                e['options_only_vista'] = [list(x) for x in only_v]
        if m.attrs.get('withGroup') != v.attrs.get('withGroup'):
            e['tagref_withGroup'] = {'mcc': m.attrs.get('withGroup'), 'vista': v.attrs.get('withGroup')}
        if m.children is not None and v.children is not None and m.element_size != v.element_size:
            e['block_element_size'] = {'mcc': m.element_size, 'vista': v.element_size}
    else:
        f = m if m is not None else v
        if f.options:
            e['options'] = f.options
        if f.children is not None:
            e['block_element_size'] = {'mcc' if m is not None else 'vista': f.element_size}
    return e


def diff_fields(mf, vf):
    """Align two field lists (MCC, Vista).  Returns entries ordered by (mcc offset, vista offset).
    Passes: (1) LCS on (name,type); (2) inside each LCS gap pair same-name/different-type and same-offset+size;
    (3) global pass pairing leftovers with identical (name,type) regardless of order ('reordered')."""
    mk = [(_norm_name(f.name), f.type) for f in mf]
    vk = [(_norm_name(f.name), f.type) for f in vf]
    pairs = _lcs_pairs(mk, vk)
    entries = []
    pairs_ext = pairs + [(len(mf), len(vf))]
    pi = pj = 0
    for (i, j) in pairs_ext:
        L = list(range(pi, i))
        R = list(range(pj, j))
        used_r = set()
        used_l = set()
        for li in L:
            nm = _norm_name(mf[li].name)
            if nm in _GENERIC_NAMES:
                continue
            for rj in R:
                if rj in used_r:
                    continue
                if nm == _norm_name(vf[rj].name):
                    used_l.add(li)
                    used_r.add(rj)
                    entries.append(_entry(mf[li], vf[rj], 'retyped'))
                    break
        for li in L:
            if li in used_l:
                continue
            for rj in R:
                if rj in used_r:
                    continue
                if mf[li].offset == vf[rj].offset and mf[li].size == vf[rj].size:
                    used_l.add(li)
                    used_r.add(rj)
                    entries.append(_entry(mf[li], vf[rj], 'renamed_retyped'))
                    break
        for li in L:
            if li not in used_l:
                entries.append(_entry(mf[li], None, 'only_mcc'))
        for rj in R:
            if rj not in used_r:
                entries.append(_entry(None, vf[rj], 'only_vista'))
        if i < len(mf) and j < len(vf):
            m, v = mf[i], vf[j]
            if m.offset == v.offset and m.size == v.size:
                st = 'same'
            elif m.size == v.size:
                st = 'moved'
            else:
                st = 'size_changed'
            entries.append(_entry(m, v, st))
        pi, pj = i + 1, j + 1
    # pass 3: global pairing of leftovers with identical (name,type)
    om = [e for e in entries if e['status'] == 'only_mcc' and _norm_name(e['name']) not in _GENERIC_NAMES]
    ov = [e for e in entries if e['status'] == 'only_vista' and _norm_name(e['name']) not in _GENERIC_NAMES]
    used = set()
    for e in om:
        for k, w in enumerate(ov):
            if k in used:
                continue
            if _norm_name(e['name']) == _norm_name(w['name']) and e['type'] == w['type']:
                used.add(k)
                ne = _entry(e['_m'], w['_v'], 'reordered')
                entries.remove(e)
                entries.remove(w)
                entries.append(ne)
                break
    entries.sort(key=lambda e: (e['mcc_offset'] if e['mcc_offset'] is not None else 10**9,
                                e['vista_offset'] if e['vista_offset'] is not None else 10**9))
    return entries


# Pairings the name/offset heuristics cannot see; each verified against real data.
SEMANTIC_PAIRS = [
    ('matg', '/Sound Globals', 'Legacy Sound Classes', 'Sound Classes',
     "MCC has two sound_classes refs (Remastered @0x0, Legacy @0x8); Vista's single ref equals the LEGACY one "
     "(headlong: sound/sound_classes, 18/18 tags)"),
    ('vrtx', '', 'Geometry Classifications DX9', 'Geometry Classifications',
     'Vista block = DX9 vertex shaders. In MCC data the DX9 block is EMPTY and the DX11 block holds DXBC bytecode '
     '(7/7 headlong vrtx: first dword "DXBC"); Vista bytecode starts 00 02 FE FF (vs_2_0)'),
    ('vrtx', '', 'Output Swizzles DX9', 'Output Swizzles', 'values identical between MCC DX9 swizzles and Vista swizzles'),
]


def _apply_semantic(ents, mname, vname, note):
    em = next((e for e in ents if e['_m'] is not None and e['_m'].name == mname), None)
    ev = next((e for e in ents if e['_v'] is not None and e['_v'].name == vname), None)
    if em is None or ev is None or em is ev:
        return ents
    mfield, vfield = em['_m'], ev['_v']
    out = [e for e in ents if e is not em and e is not ev]
    for e in (em, ev):
        if e['_m'] is not None and e['_m'] is not mfield:
            out.append(_entry(e['_m'], None, 'only_mcc'))
        if e['_v'] is not None and e['_v'] is not vfield:
            out.append(_entry(None, e['_v'], 'only_vista'))
    ne = _entry(mfield, vfield, 'semantic')
    ne['note'] = note
    out.append(ne)
    out.sort(key=lambda e: (e['mcc_offset'] if e['mcc_offset'] is not None else 10**9,
                            e['vista_offset'] if e['vista_offset'] is not None else 10**9))
    return out


def _unique_block_keys(fields):
    seen = {}
    out = []
    for f in fields:
        if f.children is not None:
            nm = _norm_name(f.name)
            k = seen.get(nm, 0)
            seen[nm] = k + 1
            out.append((nm + '#' + str(k), f))
    return out


def diff_layouts(mlay, vlay, keep_objs=False):
    """Diff an MCC layout against a Vista layout, at every nesting level.  Returns {block_path: info}.
    block_path '' is the tag root; children are '/Name' joined with MCC names ('#n' suffix for duplicate sibling names)."""
    blocks = {}

    def rec(path, mf, vf, m_decl, v_decl, m_comp, v_comp, mobj, vobj):
        ents = diff_fields(mf, vf)
        for (sg, sp, smn, svn, snote) in SEMANTIC_PAIRS:
            if sg == mlay.group and sp == path:
                ents = _apply_semantic(ents, smn, svn, snote)
        counts = {}
        for e in ents:
            counts[e['status']] = counts.get(e['status'], 0) + 1
        opt_changes = sum(1 for e in ents if 'options_only_mcc' in e)
        blocks[path] = {
            'mcc_size': m_decl, 'vista_size': v_decl, 'mcc_computed': m_comp, 'vista_computed': v_comp,
            'fields': ents, 'counts': counts, 'option_changes': opt_changes,
            # 'identical' = same offsets/types/sizes everywhere in this block (enum/flag option lists may still differ:
            # see option_changes).  A block with identical=True and option_changes==0 can be copied byte-for-byte
            # (modulo tag-ref / string-id / pointer fixups).
            'identical': (all(e['status'] == 'same' for e in ents) and m_decl == v_decl
                          and not any('tagref_withGroup' in e for e in ents)),
        }
        blocks[path]['plan'] = make_plan(ents)
        if keep_objs:
            blocks[path]['_mobj'] = mobj
            blocks[path]['_vobj'] = vobj
        mblk = {}
        vblk = {}
        seen_m = {}
        for e in ents:
            m, v = e['_m'], e['_v']
            if m is not None and v is not None and m.children is not None and v.children is not None:
                nm = m.name
                k = seen_m.get(nm, 0)
                seen_m[nm] = k + 1
                cp = path + '/' + nm + ('#%d' % k if k else '')
                rec(cp, m.children, v.children, m.element_size, v.element_size, m.computed_element_size,
                    v.computed_element_size, m, v)

    rec('', mlay.fields, vlay.fields, mlay.base_size, vlay.base_size, mlay.computed_size, vlay.computed_size, None, None)
    return blocks


FIXUP_KINDS = {'tagref': 'tagref', 'datum': 'tag_datum', 'stringid': 'stringid', 'dataref': 'dataref',
               'tagblock': 'tagblock', 'unicList': 'unic_ranges'}


def make_plan(entries):
    """Per-block conversion plan from diff entries.
    raw       [[mcc_off, vista_off, nbytes], ...]  verbatim byte copies (adjacent fields merged when contiguous in both)
    fixup     fields needing a value rewrite (tag ids, string ids, data/block pointers) - dicts with kind
    enum_remap  matched enum/flags whose option sets differ (values/bits that exist only in MCC need handling)
    vista_fill  Vista bytes with no MCC counterpart  [[vista_off, size, name, type]]
    mcc_drop    MCC bytes with no Vista counterpart  [[mcc_off, size, name, type]]"""
    raw = []
    fixup = []
    enum_remap = []
    fill = []
    drop = []
    matched = []
    for e in entries:
        m, v = e['_m'], e['_v']
        if m is not None and v is not None:
            if m.type in FIXUP_KINDS or v.type in FIXUP_KINDS:
                kind = FIXUP_KINDS.get(m.type) or FIXUP_KINDS.get(v.type)
                if m.type == 'tagref' and m.size != v.size:
                    kind = 'tagref_size_change'
                fixup.append({'kind': kind, 'mcc_offset': m.offset, 'vista_offset': v.offset, 'mcc_size': m.size,
                              'vista_size': v.size, 'name': m.name, 'status': e['status']})
            elif m.size == v.size:
                matched.append((m.offset, v.offset, m.size))
                if 'options_only_mcc' in e:
                    enum_remap.append({'mcc_offset': m.offset, 'vista_offset': v.offset, 'name': m.name, 'type': m.type,
                                       'options_only_mcc': e['options_only_mcc'], 'options_only_vista': e['options_only_vista']})
            else:
                # size changed (e.g. uint64 runtime pointer vs uint32, flags16 vs flags32): needs value conversion
                fixup.append({'kind': 'resize', 'mcc_offset': m.offset, 'vista_offset': v.offset, 'mcc_size': m.size,
                              'vista_size': v.size, 'name': m.name, 'mcc_type': m.type, 'vista_type': v.type,
                              'status': e['status']})
        elif m is not None:
            drop.append([m.offset, m.size, m.name, m.type])
        else:
            fill.append([v.offset, v.size, v.name, v.type])
    matched.sort()
    for mo, vo, n in matched:
        if raw and raw[-1][0] + raw[-1][2] == mo and raw[-1][1] + raw[-1][2] == vo:
            raw[-1][2] += n
        else:
            raw.append([mo, vo, n])
    return {'raw': raw, 'fixup': fixup, 'enum_remap': enum_remap, 'vista_fill': fill, 'mcc_drop': drop}


def strip_entry(e):
    return {k: v for k, v in e.items() if not k.startswith('_')}


# --------------------------------------------------------------------------------------
# special-conversion classification
# --------------------------------------------------------------------------------------
HAVOK_RE = re.compile(r'mopp|havok|runtime code pointer|user data|shape|rigid bod|polyhedr|phantom|constraint|ball and socket|'
                      r'hinge|ragdoll|prismatic|limited|boxes|triangles|pills|spheres|lists$|list shapes|bsp physics|'
                      r'contact points|four vectors|plane equations|nodes$', re.I)
RESOURCE_RE = re.compile(r'locator|resource data|raw |resource$|resources$|cache data|cache block|section data|native |'
                         r'runtime pointer|vertex buffer|index buffer|compressed|lod\d? (offset|size)|low-detail|mipmap', re.I)
SHADER_RE = re.compile(r'shader|bytecode|pixel|vertex constant|texture states|postprocess|compiled|implementation|overlays?$|'
                       r'layers$|passes$|remapping|animated parameter', re.I)
SOUND_GROUPS = {'snd!', 'ugh!', 'snde', 'lsnd', 'sncl', 'snmx', 'spk!', 'ssce', '<fx>', 'MGS2', 'PRTM'}
SOUND_RE = re.compile(r'sound|codec|pitch|permutation|chunk|promotion|extra info|gestalt|encoding|compression|sample|'
                      r'reverb|reflection|low pass|silence|playback|loudness|bend|gain|skip fraction', re.I)


def classify_field(group, path, f):
    """Return a list of category strings for one field (static, name/type based)."""
    cats = []
    t = f.type
    nm = f.name
    full = path + '/' + nm
    if t == 'undefined':
        cats.append('opaque')
    elif nm.lower() == 'unknown' and t in ('int8', 'int16', 'int32', 'uint8', 'uint16', 'uint32', 'float32', 'enum8',
                                           'enum16', 'enum32', 'flags8', 'flags16', 'flags32'):
        cats.append('opaque')
    if t == 'uint64':
        cats.append('runtime_ptr64')
    if t in ('tagref', 'datum'):
        cats.append('id_remap_tag')
    if t == 'stringid':
        cats.append('id_remap_string')
    if t == 'dataref':
        cats.append('dataref_payload')
    if f.children is not None or t == 'dataref':
        if HAVOK_RE.search(nm):
            cats.append('havok')
        if RESOURCE_RE.search(nm):
            cats.append('raw_resource')
        if group in ('shad', 'spas', 'stem', 'sily', 'sfx+', 'cont', 'deca', 'prt3', 'PRTM', 'MGS2', 'effe') and SHADER_RE.search(full):
            cats.append('shader')
    else:
        if RESOURCE_RE.search(nm):
            cats.append('raw_resource')
        if re.search(r'mopp|code pointer|user data', nm, re.I):
            cats.append('havok')
    if group in SOUND_GROUPS and (SOUND_RE.search(nm) or t in ('dataref',)):
        cats.append('sound')
    if group == 'bitm' and (RESOURCE_RE.search(nm) or t in ('dataref',) or 'LOD' in nm or 'Mipmap' in nm or 'Low-Detail' in nm):
        cats.append('bitmap')
    return cats


def special_fields(lay, other=None):
    """List every field of `lay` needing non-trivial handling.  Returns list of dicts."""
    out = []
    for path, f in walk_fields(lay.fields):
        cats = classify_field(lay.group, path, f)
        if cats:
            out.append({'path': path, 'off': f.offset, 'name': f.name, 'type': f.type, 'size': f.size, 'cats': cats})
    return out


def opaque_runs(fields):
    """Merge adjacent opaque ('undefined'/Unknown scalar) fields into (offset, length) runs for one struct level."""
    runs = []
    for f in sorted(fields, key=lambda x: x.offset):
        if f.children is not None:
            continue
        if f.type == 'undefined' or (f.name.lower() == 'unknown' and f.kind in ('scalar', 'enum', 'flags')):
            if runs and runs[-1][0] + runs[-1][1] == f.offset:
                runs[-1][1] += f.size
            else:
                runs.append([f.offset, f.size])
    return runs


# --------------------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------------------
def type_inventory():
    """{set: {type: count}} over both plugin directories + size column."""
    import collections
    inv = {}
    for label, d in (('Halo2 (Vista)', plugin_dirs()['vista']), ('Halo2MCC (override)', plugin_dirs()['mcc_override'])):
        c = collections.Counter()
        for fn in os.listdir(d):
            if not fn.lower().endswith('.xml'):
                continue
            root = _parse_xml(os.path.join(d, fn))

            def rec(n):
                for ch in n.children:
                    c[ch.tag] += 1
                    rec(ch)
            rec(root)
        inv[label] = dict(c)
    return inv


