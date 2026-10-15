# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Generic tag tree: decode a cache tag into nested Nodes using an Assembly plugin layout, and encode a
Node tree back into cache bytes using the Vista emission/alignment rules.

This is the SHARED CONTRACT of the converter.  Every handler works on Nodes.

Node
    group   fourcc of the owning tag ('scnr', ...)
    path    block path inside the tag: '' for the main struct, '/Block Name/Sub Block' for block elements
    fields  plugin Field list for this struct (plugins.Field)
    data    bytearray of the fixed struct (len == struct size).  Pointer words of tagblock/dataref fields are
            NOT authoritative - the encoder rewrites them from .blocks / .datas.
    blocks  {field_offset: [Node, ...]}   children of every tagblock field (empty list = empty block)
    datas   {field_offset: bytes}         payload of every dataref field (b'' = empty)
    patches {field_offset: token}         u32 words resolved at encode time through resolve(token) -> int
                                          (raw-resource pointers whose final file offset is not known yet)

decode_tag(read, layout, addr, group)            read(addr, size) -> bytes over the cache's address space
encode_tag(root, layout, base_addr, align_of, resolve=None) -> bytes
"""
import struct

from . import plugins as P  # noqa: F401  (Field type lives there)

BLOCK_TYPES = ('tagblock', 'reflexive')
DATA_TYPES = ('dataref',)


class Node(object):
    __slots__ = ('group', 'path', 'fields', 'data', 'blocks', 'datas', 'patches', 'src_addr')

    def __init__(self, group, path, fields, size, data=None):
        self.group = group
        self.path = path
        self.fields = fields
        self.data = bytearray(size) if data is None else bytearray(data)
        if len(self.data) != size:
            raise ValueError('%s|%s: data %d != size %d' % (group, path, len(self.data), size))
        self.blocks = {}
        self.datas = {}
        self.patches = {}
        self.src_addr = None

    # ---- scalar helpers -------------------------------------------------------------------------
    def u8(self, off):
        return self.data[off]

    def u16(self, off):
        return struct.unpack_from('<H', self.data, off)[0]

    def i16(self, off):
        return struct.unpack_from('<h', self.data, off)[0]

    def u32(self, off):
        return struct.unpack_from('<I', self.data, off)[0]

    def i32(self, off):
        return struct.unpack_from('<i', self.data, off)[0]

    def f32(self, off):
        return struct.unpack_from('<f', self.data, off)[0]

    def set_u8(self, off, v):
        self.data[off] = v & 0xFF

    def set_u16(self, off, v):
        struct.pack_into('<H', self.data, off, v & 0xFFFF)

    def set_u32(self, off, v):
        struct.pack_into('<I', self.data, off, v & 0xFFFFFFFF)

    def set_i32(self, off, v):
        struct.pack_into('<i', self.data, off, v)

    def set_f32(self, off, v):
        struct.pack_into('<f', self.data, off, v)

    # ---- structure helpers ----------------------------------------------------------------------
    def field(self, name):
        """First field with this (case-insensitive) name, or None."""
        n = name.lower()
        for f in self.fields:
            if f.name and f.name.lower() == n:
                return f
        return None

    def block(self, name):
        f = self.field(name)
        if f is None or f.type not in BLOCK_TYPES:
            raise KeyError('%s|%s has no block %r' % (self.group, self.path, name))
        return self.blocks.setdefault(f.offset, [])

    def iter_nodes(self):
        """Pre-order over this node and every descendant element."""
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            for off in sorted(n.blocks, reverse=True):
                stack.extend(reversed(n.blocks[off]))

    def __repr__(self):
        return '<Node %s|%s size=0x%X blocks=%d datas=%d>' % (
            self.group, self.path or '/', len(self.data), sum(len(v) for v in self.blocks.values()), len(self.datas))


def child_path(parent_path, field):
    return parent_path + '/' + field.name


def struct_size(fields):
    return max((f.offset + f.size for f in fields), default=0)


def element_size(field):
    return field.element_size or field.computed_element_size


# ------------------------------------------------------------------------------------------------
# decode
# ------------------------------------------------------------------------------------------------
def decode_struct(read, group, path, fields, size, addr, raw=None, _seen=None):
    """Decode one struct at addr (or from raw bytes) and, recursively, its blocks and datarefs.
    Identical (address, count, element size) blocks and (address, size) datarefs referenced twice inside one
    tag decode to the SAME list / bytes object, so encode_tag emits them once (cache-builder behaviour)."""
    if _seen is None:
        _seen = {}
    data = raw if raw is not None else read(addr, size)
    node = Node(group, path, fields, size, data)
    node.src_addr = addr
    for f in fields:
        if f.type in BLOCK_TYPES:
            cnt, ptr = struct.unpack_from('<iI', node.data, f.offset)
            es = element_size(f)
            kids = []
            if cnt > 0 and ptr:
                key = ('b', ptr, cnt, es)
                if key in _seen:
                    node.blocks[f.offset] = _seen[key]
                    continue
                _seen[key] = kids
                blob = read(ptr, cnt * es)
                cp = child_path(path, f)
                for i in range(cnt):
                    kids.append(decode_struct(read, group, cp, f.children, es, ptr + i * es,
                                              raw=blob[i * es:(i + 1) * es], _seen=_seen))
            node.blocks[f.offset] = kids
        elif f.type in DATA_TYPES:
            sz, ptr = struct.unpack_from('<iI', node.data, f.offset)
            if sz > 0 and ptr:
                key = ('d', ptr, sz)
                if key not in _seen:
                    _seen[key] = bytes(read(ptr, sz))
                node.datas[f.offset] = _seen[key]
            else:
                node.datas[f.offset] = b''
    return node


def decode_tag(read, layout, addr, group=None):
    g = group or layout.group
    size = layout.base_size or layout.computed_size
    return decode_struct(read, g, '', layout.fields, size, addr)


# ------------------------------------------------------------------------------------------------
# encode
# ------------------------------------------------------------------------------------------------
def align_up(v, a):
    return (v + a - 1) & ~(a - 1)


class AlignTable(object):
    """Per-path alignment for blocks/datarefs, from h2conv/data/block_alignment_vista.json (default 4)."""

    def __init__(self, table=None, default=4):
        self.t = {}
        self.default = default
        if table:
            for k, v in table.items():
                a = v.get('align') if isinstance(v, dict) else v
                # MCC jmad Frame Events / Sound Events / Modes have no consistent rule (builder padding quirk):
                # fall back to the default; the Vista writer never needs MCC alignment anyway.
                self.t[k] = int(a) if a else default

    @classmethod
    def load(cls, path):
        import json
        with open(path, 'r', encoding='utf-8') as fh:
            return cls(json.load(fh))

    def block(self, group, path):
        return self.t.get('%s|%s' % (group, path), self.default)

    def data(self, group, path):
        return self.t.get('%s|%s [data]' % (group, path), self.default)


def encode_tag(root, layout, base_addr, align, resolve=None, tail_pad=None):
    """Serialise a Node tree as one cache tag placed at base_addr.

    Emission rule (Vista, verified 100%): main struct, then depth-first pre-order: for each field of a
    struct in offset order, a non-empty tagblock's elements back-to-back followed - before the next sibling
    field - by the children of its elements in element order; a non-empty dataref contributes its bytes.
    Each block/data start = align_up(previous_end, A[path]).  Empty blocks/datarefs = {0, 0}.
    Returns bytes whose length is a multiple of 4 (plus the layout's constant tail, e.g. vehi 432 B).
    """
    out = bytearray(root.data)
    emitted = {}   # id(block list) / id(dataref payload) -> address: a list/payload object shared by several
                   # fields (decode_tag shares identical intervals) is emitted once, like the cache builder does

    def emit_children(node, node_off):
        for f in sorted(node.fields, key=lambda x: x.offset):
            if f.type in BLOCK_TYPES:
                kids = node.blocks.get(f.offset) or []
                if not kids:
                    struct.pack_into('<iI', out, node_off + f.offset, 0, 0)
                    continue
                if id(kids) in emitted:
                    struct.pack_into('<iI', out, node_off + f.offset, len(kids), emitted[id(kids)])
                    continue
                es = element_size(f)
                cp = child_path(node.path, f)
                # alignment is on the ABSOLUTE address (tag bases are only 4-aligned)
                start = align_up(base_addr + len(out), align.block(node.group, cp)) - base_addr
                out.extend(b'\0' * (start - len(out)))
                emitted[id(kids)] = base_addr + start
                struct.pack_into('<iI', out, node_off + f.offset, len(kids), base_addr + start)
                offs = []
                for k in kids:
                    if len(k.data) != es:
                        raise ValueError('%s|%s element size %d != %d' % (k.group, k.path, len(k.data), es))
                    offs.append(len(out))
                    out.extend(k.data)
                for k, ko in zip(kids, offs):
                    emit_children(k, ko)
            elif f.type in DATA_TYPES:
                payload = node.datas.get(f.offset) or b''
                if not payload:
                    struct.pack_into('<iI', out, node_off + f.offset, 0, 0)
                    continue
                if len(payload) > 1 and id(payload) in emitted:
                    struct.pack_into('<iI', out, node_off + f.offset, len(payload), emitted[id(payload)])
                    continue
                start = align_up(base_addr + len(out), align.data(node.group, child_path(node.path, f))) - base_addr
                out.extend(b'\0' * (start - len(out)))
                emitted[id(payload)] = base_addr + start
                struct.pack_into('<iI', out, node_off + f.offset, len(payload), base_addr + start)
                out.extend(payload)
        if node.patches:
            if resolve is None:
                raise ValueError('%s|%s has unresolved patches' % (node.group, node.path))
            for off, tok in node.patches.items():
                struct.pack_into('<I', out, node_off + off, resolve(tok) & 0xFFFFFFFF)

    emit_children(root, 0)
    out.extend(b'\0' * (align_up(len(out), 4) - len(out)))
    tp = layout.tail_pad if tail_pad is None else tail_pad
    if tp:
        out.extend(b'\0' * tp)
    return bytes(out)


# ------------------------------------------------------------------------------------------------
# comparison helper (used by tests and the validator)
# ------------------------------------------------------------------------------------------------
def same_tree(a, b, ignore_pointer_words=True):
    """Structural equality of two Node trees (block counts, struct bytes, data payloads).
    Pointer words of tagblock/dataref fields are ignored (they depend on placement)."""
    if len(a.data) != len(b.data):
        return False, '%s: size %d vs %d' % (a.path, len(a.data), len(b.data))
    da, db = bytearray(a.data), bytearray(b.data)
    for f in a.fields:
        if f.type in BLOCK_TYPES or f.type in DATA_TYPES:
            if ignore_pointer_words:
                da[f.offset + 4:f.offset + 8] = b'\0\0\0\0'
                db[f.offset + 4:f.offset + 8] = b'\0\0\0\0'
    if da != db:
        i = next(i for i in range(len(da)) if da[i] != db[i])
        return False, '%s: struct bytes differ at +0x%X' % (a.path or '/', i)
    for off in set(a.blocks) | set(b.blocks):
        ka, kb = a.blocks.get(off, []), b.blocks.get(off, [])
        if len(ka) != len(kb):
            return False, '%s@0x%X: count %d vs %d' % (a.path or '/', off, len(ka), len(kb))
        for x, y in zip(ka, kb):
            ok, why = same_tree(x, y, ignore_pointer_words)
            if not ok:
                return False, why
    for off in set(a.datas) | set(b.datas):
        if (a.datas.get(off) or b'') != (b.datas.get(off) or b''):
            return False, '%s@0x%X: dataref payload differs' % (a.path or '/', off)
    return True, ''
