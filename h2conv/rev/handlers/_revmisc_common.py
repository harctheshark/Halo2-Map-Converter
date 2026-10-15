# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Small helpers shared by the reverse (Vista -> MCC) specialist handlers: havok.py, scripts.py, gameplay.py, computed.py,
locale.py of this package.  Underscore-prefixed, so `h2conv.rev.handlers.load_all()` does not import it as a handler module.

Everything here is duck-typed against the reverse Ctx: a handler only needs
    ctx.remap_sid(vista_sid) -> MCC string id          ctx.remap_datum(vista_datum) -> MCC datum
    ctx.warn(code, msg, tag=None)    ctx.stats (Counter)    ctx.opts (dict)
and optionally  ctx.plan, ctx.mcc_shared, ctx.src_string(vista_sid), ctx.sid(text), ctx.remap_name(group, name).
Every accessor degrades gracefully when the attribute is missing, so the handlers can be unit-tested with a stub.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import struct

from ... import tagtree as T

NULL = 0xFFFFFFFF
BLOCK_TYPES = ('tagblock', 'reflexive')

_FMT = {'int8': '<b', 'uint8': '<B', 'enum8': '<B', 'flags8': '<B', 'int16': '<h', 'uint16': '<H', 'enum16': '<H',
        'flags16': '<H', 'int32': '<i', 'uint32': '<I', 'enum32': '<I', 'flags32': '<I', 'stringid': '<I',
        'datum': '<I', 'float32': '<f', 'uint64': '<Q'}


# ---------------------------------------------------------------------------------------------------------------
# ctx access
# ---------------------------------------------------------------------------------------------------------------
def stat(ctx, key, n=1):
    st = getattr(ctx, 'stats', None)
    if st is not None:
        st[key] += n


def warn(ctx, code, msg, tag=None):
    w = getattr(ctx, 'warn', None)
    if w is not None:
        w(code, msg, tag)


def opt(ctx, key, default=None):
    o = getattr(ctx, 'opts', None) or {}
    return o.get(key, default)


def src_string(ctx, sid):
    """Text of a VISTA string id of the map being converted ('' for 0 / null)."""
    if not sid or sid == NULL:
        return ''
    for name in ('src_string', 'vista_string', 'source_string'):
        fn = getattr(ctx, name, None)
        if fn is not None:
            return fn(sid)
    cc = getattr(ctx, 'cur_cache', None) or getattr(ctx, 'src', None)
    if cc is not None and hasattr(cc, 'string_id'):
        return cc.string_id(sid)
    raise AttributeError('ctx has no way to read a Vista string id')


def remap_name(ctx, group, name):
    """Output (MCC) datum of the planned tag (group, name), NULL when unknown."""
    fn = getattr(ctx, 'remap_name', None)
    if fn is None:
        plan = getattr(ctx, 'plan', None)
        fn = getattr(plan, 'remap_name', None) if plan is not None else None
    if fn is None or not name:
        return NULL
    try:
        r = fn(group, name)
    except Exception:  # noqa: BLE001
        return NULL
    return NULL if r is None else r


def mcc_donor(ctx, group, name):
    """(Node, MccCache) of the stock MCC tag with this (group, name) - shared.map / single_player_shared.map / mainmenu.map
    (ctx.mcc_shared) or ctx.opts['mcc_donor_caches'] - or (None, None).  Used to fill MCC-only content (extra mulg weapons,
    the matg anniversary refs, the campaign metagame data of a char ...) with what the real MCC tag holds.
    ctx.opts['rev_no_donor'] = True disables the lookup (tests measure the defaults-only quality with it)."""
    if opt(ctx, 'rev_no_donor') or not name:
        return None, None
    fn = getattr(ctx, 'mcc_donor', None)
    if fn is not None:
        try:
            r = fn(group, name)
            return r if isinstance(r, tuple) else (r, None)
        except Exception:  # noqa: BLE001
            return None, None
    caches = []
    sh = getattr(ctx, 'mcc_shared', None)
    if sh is not None:
        for k in ('shared', 'single_player_shared', 'mainmenu'):
            try:
                c = sh.get(k)
            except Exception:  # noqa: BLE001
                c = None
            if c is not None:
                caches.append(c)
    caches += list(opt(ctx, 'mcc_donor_caches', ()) or ())
    want = name.lower()
    lay = getattr(ctx, 'lay_mcc', None)
    if lay is None or group not in lay:
        return None, None
    for c in caches:
        for t in c.tags:
            if t.group == group and t.has_data and not t.is_null and (t.name or '').lower() == want:
                return T.decode_tag(lambda a, n, c=c: c.read_at_addr(a, n), lay[group], t.addr, group), c
    return None, None


def donor_ref(ctx, cache, group_word, datum):
    """Map a tag reference of a donor MCC tag into the OUTPUT map: (group word, datum) with the datum looked up BY NAME
    (ctx.remap_name); a reference the output does not carry becomes (group word, NULL)."""
    if datum == NULL or cache is None:
        return group_word, NULL
    t = cache.tag_by_datum(datum)
    if t is None or not t.name:
        return group_word, NULL
    return group_word, remap_name(ctx, t.group, t.name)


def clone_donor_tree(ctx, cache, node):
    """Deep copy of a donor MCC Node tree with every tag reference re-pointed BY NAME into the output map and every string id
    re-mapped BY TEXT (ctx.sid) - the donor belongs to another map, its datums and string ids mean nothing here."""
    n = clone_node(node)
    for nd in n.iter_nodes():
        for f in nd.fields:
            if f.type == 'tagref':
                if f.size == 8:
                    g, d = struct.unpack_from('<II', nd.data, f.offset)
                    g2, d2 = donor_ref(ctx, cache, g, d)
                    struct.pack_into('<II', nd.data, f.offset, g2 if d2 != NULL or d == NULL else NULL, d2)
                else:
                    d = struct.unpack_from('<I', nd.data, f.offset)[0]
                    struct.pack_into('<I', nd.data, f.offset, donor_ref(ctx, cache, NULL, d)[1])
            elif f.type == 'stringid':
                sid = struct.unpack_from('<I', nd.data, f.offset)[0]
                if sid and sid != NULL and cache is not None:
                    try:
                        struct.pack_into('<I', nd.data, f.offset, ctx.sid(cache.string_id(sid)))
                    except Exception:  # noqa: BLE001
                        struct.pack_into('<I', nd.data, f.offset, 0)
    return n


# ---------------------------------------------------------------------------------------------------------------
# Node field helpers (by plugin NAME, so one code path serves both layouts)
# ---------------------------------------------------------------------------------------------------------------
def rd(node, name, default=None, nth=0):
    k = 0
    for f in node.fields:
        if f.name and f.name.lower() == name.lower():
            if k == nth:
                fmt = _FMT.get(f.type)
                if fmt is None:
                    return default
                return struct.unpack_from(fmt, node.data, f.offset)[0]
            k += 1
    return default


def wr(node, name, value, nth=0):
    k = 0
    for f in node.fields:
        if f.name and f.name.lower() == name.lower():
            if k == nth:
                fmt = _FMT.get(f.type)
                if fmt is None:
                    return False
                if fmt in ('<B', '<H', '<I', '<Q'):
                    value &= (1 << (8 * struct.calcsize(fmt))) - 1
                struct.pack_into(fmt, node.data, f.offset, value)
                return True
            k += 1
    return False


def field_at(node, name, offset):
    n = name.lower()
    for f in node.fields:
        if f.offset == offset and f.name and f.name.lower() == n:
            return f
    return None


def block_fields(node, name):
    n = name.lower()
    return [f for f in node.fields if f.type in BLOCK_TYPES and f.name and f.name.lower() == n]


def blk(node, name):
    """Elements of the (first) tagblock called `name`; [] when the field or block is missing."""
    f = node.field(name)
    if f is None or f.children is None:
        return []
    return node.blocks.get(f.offset, [])


def set_block(node, field, kids):
    """Replace the elements of tagblock `field`; also write the (non-authoritative) count word."""
    node.blocks[field.offset] = list(kids)
    struct.pack_into('<i', node.data, field.offset, len(kids))


def new_element(parent, field):
    """A zeroed element Node for tagblock `field` of `parent`."""
    return T.Node(parent.group, T.child_path(parent.path, field), field.children, T.element_size(field))


def clone_node(node):
    """Deep copy of a Node tree (shares the immutable Field lists)."""
    n = T.Node(node.group, node.path, node.fields, len(node.data), node.data)
    n.src_addr = node.src_addr
    n.datas = dict(node.datas)
    n.patches = dict(node.patches)
    for off, kids in node.blocks.items():
        n.blocks[off] = [clone_node(k) for k in kids]
    return n


def tagref_datum(node, name):
    f = node.field(name)
    if f is None or f.type != 'tagref':
        return NULL
    return struct.unpack_from('<I', node.data, f.offset + (4 if f.size == 8 else 0))[0]


def fcc(text):
    """Group word (as stored in an 8-byte tag reference) of a 4-character group name."""
    return struct.unpack('<I', text.encode('latin-1')[::-1])[0]


def s32(x):
    return x - (1 << 32) if x & 0x80000000 else x


def cur_name(ctx):
    """Name of the tag being converted (ctx.cur_entry.name), '' when unknown."""
    e = getattr(ctx, 'cur_entry', None)
    return (getattr(e, 'name', None) or '') if e is not None else ''
