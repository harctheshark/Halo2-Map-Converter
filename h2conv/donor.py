# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Vista donor tags: find them by (group, name), import them into the output space, compute reference closures.

Why donors?  MCC ships no D3D9 shader bytecode (vrtx/spas hold DXBC only), so every shader-family tag the Vista engine
will run has to come from a Vista map that already has it.  The planner marks such tags action='donor' with
entry.donor = (vista map path, tag index); the driver calls import_tag(ctx, entry), which

  1. decodes the Vista tag with the Vista layout,
  2. remaps every tag reference of the donor map -> (group, name) -> the OUTPUT datum (ctx.plan / ctx.remap_name),
  3. remaps every string id by TEXT through ctx.sid,
  4. keeps raw-resource pointers that stay valid in the output (location 1 mainmenu.map / 2 shared.map) and copies the
     bytes of the ones that do not (location 0 = the donor map itself, location 3 = single_player_shared.map for an
     MP output) with ctx.add_raw + node.patches,
  5. applies the per-group post hooks (shad: Log Bitmap Dimension recomputation).

ctx-independent helpers for the planner:  DonorIndex / get_index(maps_dir), find(), references(), closure().

Duck-typed Ctx surface used: ctx.vista (.maps_dir, .open(name)), ctx.decode_vista(cache, tag), ctx.sid(text),
ctx.add_raw(kind, data), ctx.warn(code, msg, tag), ctx.plan (entries / shared_placeholders), ctx.map_type, ctx.stats,
optional ctx.remap_name(group, name) -> datum, ctx.ref_tag(group, name) (a test double).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import collections
import glob
import os
import struct

from . import paths as _paths
from . import tagtree as T
from .vista_cache import VistaCache

NULL = 0xFFFFFFFF
SHARED_FIRST = ('shared.map', 'single_player_shared.map', 'mainmenu.map')      # preferred donor order
SHADER_FAMILY = ('shad', 'stem', 'spas', 'vrtx')
LOC_SELF, LOC_MAINMENU, LOC_SHARED, LOC_SP_SHARED = 0, 1, 2, 3
LOC_FILES = (None, 'mainmenu.map', 'shared.map', 'single_player_shared.map')

# Vista raw-pointer sites outside bitm: (group, [(block field offset, element size)...], offset of geometry_block_info)
# identical to raw_mcc.GEOMETRY_INFO_SITES (block offsets/sizes are the same in both games).
GEOMETRY_SITES = (
    ('mode', ((0x24, 0x5C),), 0x38),
    ('mode', ((0x74, 0x58),), 0x34),
    ('sbsp', ((0x9C, 0xB0),), 0x28),
    ('sbsp', ((0x138, 0xC8),), 0x28),
    ('sbsp', ((0x214, 0xAC),), 0x10),
    ('sbsp', ((0x234, 0x30), (0x10, 0x34)), 0x00),
    ('ltmp', ((0x80, 0x68), (0x20, 0x54)), 0x28),
    ('ltmp', ((0x80, 0x68), (0x30, 0x54)), 0x28),
    ('ltmp', ((0x80, 0x68), (0x40, 0x38)), 0x0C),
    ('scnr', ((0x360, 0x30), (0x10, 0x34)), 0x00),
    ('DECR', (), 0x38),
    ('PRTM', (), 0xA0),
    ('dc_s', ((0x00, 0x30), (0x10, 0x34)), 0x00),
    ('weat', ((0x00, 0x8C),), 0x40),
)


# ================================================================================================
# donor index
# ================================================================================================
class Donor(collections.namedtuple('Donor', 'cache tag')):
    """A Vista tag that carries data: .cache VistaCache, .tag VistaTag."""
    __slots__ = ()

    @property
    def path(self):
        return self.cache.path

    @property
    def map_name(self):
        return os.path.basename(self.cache.path)

    @property
    def spec(self):
        """(map path, tag index) - what PlanEntry.donor holds."""
        return (self.cache.path, self.tag.index)


class DonorIndex(object):
    """{(group, name.lower()): [Donor, ...]} over the tags that carry data in every map of a Vista maps folder.
    Order of each list: shared.map, single_player_shared.map, mainmenu.map, then the other maps alphabetically."""

    def __init__(self, maps_dir, opener=None, groups=None):
        self.maps_dir = maps_dir
        _paths.warn_extra_vista_maps(maps_dir)               # every .map of the folder becomes a donor: say so when it holds more than the stock maps
        self.groups = set(groups) if groups else None
        self._open = {}
        self._opener = opener
        self.by_key = {}
        self.by_group = collections.defaultdict(list)
        names = list(SHARED_FIRST)
        names += [os.path.basename(p) for p in sorted(glob.glob(os.path.join(maps_dir, '*.map')))
                  if os.path.basename(p).lower() not in SHARED_FIRST]
        self.map_names = []
        for n in names:
            p = os.path.join(maps_dir, n)
            if not os.path.isfile(p):
                continue
            try:
                c = self.open(n)
            except Exception:                                   # noqa: BLE001  (not a Vista map)
                continue
            self.map_names.append(n)
            for t in c.tags:
                if t.is_empty or not t.name or not t.addr or not t.size:
                    continue
                if self.groups is not None and t.group not in self.groups:
                    continue
                d = Donor(c, t)
                self.by_key.setdefault((t.group, t.name.lower()), []).append(d)
                self.by_group[t.group].append(d)

    def open(self, name):
        name = os.path.basename(name)
        c = self._open.get(name)
        if c is None:
            c = self._opener(name) if self._opener is not None else VistaCache(os.path.join(self.maps_dir, name))
            self._open[name] = c
        return c

    def find(self, group, name, prefer=None):
        """First Donor holding (group, name) locally; `prefer` = a map basename to try first."""
        lst = self.by_key.get((group, (name or '').lower()))
        if not lst:
            return None
        if prefer:
            for d in lst:
                if os.path.basename(d.cache.path).lower() == prefer.lower():
                    return d
        return lst[0]

    def find_all(self, group, name):
        return list(self.by_key.get((group, (name or '').lower()), ()))


_INDEXES = {}


def get_index(ctx_or_dir, groups=None):
    """Cached DonorIndex for a ctx (ctx.vista.maps_dir) or a maps directory."""
    if isinstance(ctx_or_dir, str):
        maps_dir, opener = ctx_or_dir, None
    else:
        v = ctx_or_dir.vista
        maps_dir = v.maps_dir
        opener = v.open
    key = os.path.normcase(os.path.abspath(maps_dir))
    ix = _INDEXES.get(key)
    if ix is None:
        ix = _INDEXES[key] = DonorIndex(maps_dir, opener=opener)
    return ix


def find(ctx, group, name, prefer=None):
    """Donor for (group, name) in any Vista map (data carried locally), or None."""
    return get_index(ctx).find(group, name, prefer)


def open_donor(ctx, spec):
    """spec = PlanEntry.donor = (vista map path or name, tag index) -> Donor."""
    path, index = spec
    ix = get_index(ctx)
    cache = ix.open(path)
    return Donor(cache, cache.tags[index])


# ================================================================================================
# tag references / strings of a decoded Vista node tree
# ================================================================================================
def _ref_sites(node):
    """[(field, kind)] for the node's own struct; kind: 'ref8' (group+datum), 'ref4' (datum only), 'sid'."""
    out = []
    for f in node.fields:
        if f.type == 'tagref':
            out.append((f, 'ref8' if f.size == 8 else 'ref4'))
        elif f.type == 'stringid':
            out.append((f, 'sid'))
    return out


def _unique_nodes(root):
    """Pre-order over every node exactly once (decode_tag shares identical block intervals between fields)."""
    seen = set()
    stack = [root]
    while stack:
        n = stack.pop()
        if id(n) in seen:
            continue
        seen.add(id(n))
        yield n
        for off in sorted(n.blocks, reverse=True):
            stack.extend(reversed(n.blocks[off]))


def tag_label(cache, datum):
    """(group, name) of the tag a Vista datum names in `cache`, or None."""
    if datum in (NULL, 0, None):
        return None
    i = datum & 0xFFFF
    if i < len(cache.tags):
        t = cache.tags[i]
        if t.datum == datum and not t.is_empty:
            return (t.group, t.name or '')
    return None


def references(root, cache):
    """Every non-null tag reference of a decoded Vista tree -> list of (group, name) (duplicates removed, order kept).
    ctx-independent (planner closure logic)."""
    seen = {}
    for n in _unique_nodes(root):
        for f, kind in _ref_sites(n):
            if kind == 'sid':
                continue
            d = struct.unpack_from('<I', n.data, f.offset + (4 if kind == 'ref8' else 0))[0]
            lab = tag_label(cache, d)
            if lab is not None:
                seen.setdefault(lab, None)
    return list(seen)


class Closure(object):
    """Result of closure(): .items [(Donor)] to import (start first), .leaves {(group, name)} referenced outside
    `follow` (the planner resolves them like any other tag), .missing {(group, name)} followed groups with no donor."""

    def __init__(self):
        self.items = []
        self.leaves = collections.OrderedDict()
        self.missing = collections.OrderedDict()

    def names(self):
        return [(d.tag.group, d.tag.name) for d in self.items]


def closure(index, start, follow=SHADER_FAMILY, decode=None, layouts=None):
    """Transitive set of donor tags needed to import `start` (a Donor): the start tag and every referenced tag whose
    group is in `follow` that some Vista map carries.  `index` = DonorIndex.  ctx-independent: pass `layouts`
    (plugins.load_layouts('vista', corrected=True)) or a `decode(cache, tag) -> Node`."""
    if decode is None:
        if layouts is None:
            from . import plugins as P
            layouts = P.load_layouts('vista', corrected=True)
        decode = lambda cache, tag: T.decode_tag(cache.read_addr, layouts[tag.group], tag.addr, tag.group)  # noqa: E731
    res = Closure()
    seen = set()
    stack = [start]
    while stack:
        d = stack.pop()
        key = (d.tag.group, (d.tag.name or '').lower())
        if key in seen:
            continue
        seen.add(key)
        res.items.append(d)
        root = decode(d.cache, d.tag)
        for grp, nm in references(root, d.cache):
            if grp in follow:
                dd = index.find(grp, nm, prefer=os.path.basename(d.cache.path))
                if dd is None:
                    res.missing[(grp, nm)] = None
                else:
                    stack.append(dd)
            else:
                res.leaves[(grp, nm)] = None
    return res


# ================================================================================================
# output-space lookups (name -> datum)
# ================================================================================================
def _name_map(ctx):
    """{(group, name.lower()): out_datum} from ctx.plan (entries + shared placeholders), cached on the ctx."""
    nm = getattr(ctx, '_donor_name_map', None)
    if nm is not None:
        return nm
    nm = {}
    plan = getattr(ctx, 'plan', None)
    if plan is not None:
        for p in (getattr(plan, 'shared_placeholders', None) or ()):
            if getattr(p, 'name', None) and getattr(p, 'datum', NULL) != NULL:
                nm.setdefault((p.group, p.name.lower()), p.datum)
        for e in (getattr(plan, 'entries', None) or ()):
            if not e.name:
                continue
            d = None
            if e.action in ('convert', 'link', 'donor'):
                d = e.out_datum
            elif e.action == 'drop' and getattr(e, 'redirect', None) is not None:
                d = e.redirect.out_datum
            if d is not None and d != NULL:
                nm[(e.group, e.name.lower())] = d
    try:
        ctx._donor_name_map = nm
    except Exception:                                           # noqa: BLE001
        pass
    return nm


def out_datum(ctx, group, name):
    """Output datum of the tag (group, name), NULL when the output has no such tag."""
    plan = getattr(ctx, 'plan', None)
    for fn in (getattr(ctx, 'remap_name', None), getattr(plan, 'remap_name', None)):   # Ctx hook, then planner.Plan
        if callable(fn):
            d = fn(group, name)
            if d is not None and d != NULL:
                return d
    d = _name_map(ctx).get((group, (name or '').lower()))
    if d is not None:
        return d
    rt = getattr(ctx, 'ref_tag', None)                           # a test double: the reference map's own datums
    if callable(rt):
        t = rt(group, name)
        if t is not None:
            return t.datum
    return NULL


def in_output(ctx, group, name):
    return out_datum(ctx, group, name) != NULL


def out_label(ctx, datum):
    """(group, name.lower()) of the output tag with this datum, None when unknown (reverse of out_datum)."""
    if datum in (NULL, 0, None):
        return None
    fn = getattr(ctx, 'out_label', None)
    if callable(fn):
        lab = fn(datum)
        if lab is not None:
            return lab
    rev = getattr(ctx, '_donor_rev_map', None)
    nm = _name_map(ctx)
    if rev is None or len(rev) != len(nm):
        rev = {}
        for (grp, n), dat in nm.items():
            rev.setdefault(dat, (grp, n))
        try:
            ctx._donor_rev_map = rev
        except Exception:                                       # noqa: BLE001
            pass
    lab = rev.get(datum)
    if lab is not None:
        return lab
    ref = getattr(ctx, 'ref', None)                              # a test double (the reference map of the tests)
    if ref is not None:
        i = datum & 0xFFFF
        if i < len(ref.tags) and ref.tags[i].datum == datum and ref.tags[i].name:
            return (ref.tags[i].group, ref.tags[i].name.lower())
    return None


# ================================================================================================
# remapping a decoded donor tree into the output
# ================================================================================================
def _shared_slot_datum(ctx, cache, dat, lab):
    """Output datum of a reference inside a CLONE of a Vista shared tag (PlanEntry.shared_refs): the clone hudg's local datum, else the
    shared slot itself (same index + datum as shared.map) provided the plan lists it as a shared placeholder.  NEVER resolved by name: a
    local tag that shadows a shared tag must not capture the clone's references.  NULL when the slot is not listed."""
    plan = getattr(ctx, 'plan', None)
    if lab is not None:
        fn = getattr(plan, 'shared_clone_datum', None)
        local = fn(lab[0], lab[1]) if callable(fn) else None
        if local is not None:
            return local
    if os.path.basename(cache.path).lower() != 'shared.map':
        return NULL
    listed = getattr(plan, 'shared_listed', None)
    if callable(listed) and not listed(dat & 0xFFFF):
        return NULL
    return dat


def remap_tree(ctx, root, cache, label=None, shared_refs=False):
    """In place: tag references -> output datums (by (group, name)), string ids -> output ids (by text).
    shared_refs=True (a clone of a Vista shared tag): references stay on the shared slots instead (see _shared_slot_datum).
    Idempotent per tree only if called once (it rewrites ids)."""
    stats = ctx.stats
    for n in _unique_nodes(root):
        d = n.data
        for f, kind in _ref_sites(n):
            if kind == 'sid':
                s = struct.unpack_from('<I', d, f.offset)[0]
                if s in (0, NULL):
                    continue
                try:
                    text = cache.string_id(s)
                except Exception:                              # noqa: BLE001
                    ctx.warn('donor_bad_sid', '%s|%s +0x%X sid 0x%08X' % (n.group, n.path, f.offset, s), label)
                    struct.pack_into('<I', d, f.offset, 0)
                    continue
                struct.pack_into('<I', d, f.offset, ctx.sid(text))
                stats['donor_sids'] += 1
                continue
            off = f.offset + (4 if kind == 'ref8' else 0)
            dat = struct.unpack_from('<I', d, off)[0]
            if dat in (NULL, 0):
                continue
            lab = tag_label(cache, dat)
            nd = NULL
            if shared_refs:
                nd = _shared_slot_datum(ctx, cache, dat, lab)
            elif lab is not None:
                nd = out_datum(ctx, lab[0], lab[1])
            stats['donor_refs'] += 1
            if nd == NULL:
                stats['donor_refs_dropped'] += 1
                ctx.warn('donor_ref_dropped', '%s|%s +0x%X %s -> %s' % (
                    n.group, n.path or '/', f.offset, f.name, '%s %s' % lab if lab else 'datum 0x%08X unresolved' % dat), label)
                if kind == 'ref8':
                    struct.pack_into('<II', d, f.offset, NULL, NULL)
                else:
                    struct.pack_into('<I', d, off, NULL)
            else:
                struct.pack_into('<I', d, off, nd)


# ---- raw pointers ----------------------------------------------------------------------------------
def _loc_file(ctx, cache, loc):
    if loc == LOC_SELF:
        return cache
    return get_index(ctx).open(LOC_FILES[loc])


def _must_copy(ctx, loc):
    if loc == LOC_SELF:
        return True
    if loc == LOC_SP_SHARED:
        return getattr(ctx, 'map_type', 1) == 1          # MP maps may reference mainmenu.map / shared.map only
    return False


def _relocate_pointer(ctx, node, off, size_off, cache, kind, label, rebase_mainmenu=False):
    """node.data[off] = raw offset dword (loc<<30 | offset), size at size_off (or None).  Copy or keep.
    rebase_mainmenu (donor = mainmenu.map, PlanEntry.mainmenu_raw): a pointer into the donor itself (loc 0) becomes loc 1 (mainmenu.map) with the same
    offset, the bytes stay where they are (the map-list preview bitmap: stock zanzibar / needle / ... do exactly this)."""
    ptr = struct.unpack_from('<I', node.data, off)[0]
    size = struct.unpack_from('<I', node.data, size_off)[0] if size_off is not None else 0
    if size == 0 or ptr in (0, NULL):
        return False
    loc, fo = ptr >> 30, ptr & 0x3FFFFFFF
    if rebase_mainmenu and loc == LOC_SELF:
        struct.pack_into('<I', node.data, off, (LOC_MAINMENU << 30) | fo)
        ctx.stats['donor_raw_rebased_mainmenu'] += 1
        return False
    if not _must_copy(ctx, loc):
        ctx.stats['donor_raw_kept'] += 1
        return False
    src = _loc_file(ctx, cache, loc)
    data = src.raw(fo, size)
    if len(data) != size:
        ctx.warn('donor_raw_short', '%s +0x%X: wanted %d bytes at 0x%X of %s, got %d' % (
            node.group, off, size, fo, os.path.basename(src.path), len(data)), label)
        return False
    node.patches[off] = ctx.add_raw(kind, data)
    ctx.stats['donor_raw_copied'] += 1
    ctx.stats['donor_raw_bytes'] += size
    return True


def relocate_raw(ctx, root, cache, label=None, rebase_mainmenu=False):
    """Raw-resource pointers of the donor tag: bitmap pixel streams (bitm element +0x1C / size +0x34) and geometry blocks
    (geometry_block_info +0x00 / size +0x04).  Sound chunks (snd!/ugh!) are NOT relocated (warn)."""
    g = root.group
    if g == 'bitm':
        f = root.field('Bitmaps')
        for el in root.blocks.get(f.offset, []) if f is not None else []:
            _relocate_pointer(ctx, el, 0x1C, 0x34, cache, 'bitmap', label, rebase_mainmenu)
    for (grp, path, info_off) in GEOMETRY_SITES:
        if grp != g:
            continue
        level = [root]
        for boff, _es in path:
            nxt = []
            for n in level:
                nxt.extend(n.blocks.get(boff, []))
            level = nxt
        for n in level:
            _relocate_pointer(ctx, n, info_off, info_off + 4, cache, 'geometry', label)
    if g in ('snd!', 'ugh!'):
        ctx.warn('donor_sound_raw', '%s donor imported without sound-chunk relocation' % g, label)


# ================================================================================================
# import
# ================================================================================================
_POST_HOOKS = {}


def post_hook(group):
    """Register fn(ctx, root_node, donor_cache, entry) for imports of `group` (used by handlers/shaders.py)."""
    def deco(fn):
        _POST_HOOKS[group] = fn
        return fn
    return deco


def import_tag(ctx, entry):
    """PlanEntry (action 'donor', entry.donor = (vista map path, tag index)) -> Vista root Node in output space."""
    if not getattr(entry, 'donor', None):
        raise ValueError('plan entry %s %s has no donor' % (entry.group, entry.name))
    d = open_donor(ctx, entry.donor)
    return import_donor(ctx, d, entry)


def import_donor(ctx, d, entry=None):
    """Import a Donor (see find()) - the engine of import_tag; entry is optional (labels / handlers)."""
    prev = getattr(ctx, 'cur_entry', None)
    label = getattr(entry, 'name', None) or d.tag.name
    if entry is not None:
        ctx.cur_entry = entry
    try:
        root = ctx.decode_vista(d.cache, d.tag)
        shared_refs = bool(getattr(entry, 'shared_refs', False))
        remap_tree(ctx, root, d.cache, label, shared_refs=shared_refs)
        mm_raw = bool(getattr(entry, 'mainmenu_raw', False))
        relocate_raw(ctx, root, d.cache, label, mm_raw)
        if mm_raw and root.group == 'bitm' and entry is not None and getattr(entry, 'out_datum', None) not in (None, NULL):
            f = root.field('Bitmaps')
            for el in root.blocks.get(f.offset, []) if f is not None else []:
                struct.pack_into('<I', el.data, 0x4C, entry.out_datum)          # Vista +0x4C = the bitmap tag's own datum (bitmaps.py parent_datum)
        if shared_refs and root.group == 'hudg':
            _strip_training(ctx, root, label)
        if shared_refs and root.group == 'matg':
            ex = getattr(ctx, 'extra', None)
            if ex is not None:
                ex.setdefault('clone_matg_nodes', []).append(root)       # handlers/locale.py keeps the clone's language-entry tail dwords
        hook = _POST_HOOKS.get(root.group)
        if hook is None and root.group == 'shad':
            _load_shader_hooks()
            hook = _POST_HOOKS.get('shad')
        if hook is not None:
            hook(ctx, root, d.cache, entry)
        ctx.stats['donor_imports'] += 1
        ctx.stats['donor_import:%s' % root.group] += 1
        return root
    finally:
        ctx.cur_entry = prev


def _strip_training(ctx, root, label=None):
    """Training off: empty the hudg 'Player Training Data' block (the only change of the cloned Vista hudg; handlers/gameplay.hudg_root
    does the same for a converted hudg).  The planner only creates such a clone when training is off."""
    f = next((x for x in root.fields if x.name and x.name.lower() == 'player training data'), None)
    if f is None:
        ctx.warn('gp_hudg_training_field', 'hudg clone has no Player Training Data block', label)
        return 0
    n = len(root.blocks.get(f.offset) or [])
    root.blocks[f.offset] = []
    root.set_i32(f.offset, 0)
    ctx.stats['gp_hudg_training_entries_removed'] += n
    ctx.warn('gp_hudg_training_disabled', 'hudg Player Training Data emptied (%d entries; the game shows no tutorial prompts; '
                                          '--keep-training keeps them)' % n, label)
    return n


def _load_shader_hooks():
    try:
        from .handlers import shaders  # noqa: F401  (registers the 'shad' post hook)
    except Exception:                                           # noqa: BLE001
        pass
