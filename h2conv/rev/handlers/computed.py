# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Fields the cache builders COMPUTE and the systematic value conventions of the MCC tool, REVERSE direction (Vista -> MCC).

Inverse of the forward handlers/computed.py.

What the forward direction needed and what the reverse needs
------------------------------------------------------------
* hlmt Render-Only Node Flags / Section Flags / Runtime Flags (+0xA4 / +0xC4 / +0xE4).  The forward docs read "H2EK leaves them
  zero, Vista always fills them" and recomputed them from the model.  The reverse oracle shows the real convention: the MCC tool
  writes the SAME render-only bits as the Vista tool but WITHOUT the padding bits (bits >= node count of the last node word) and
  sets Runtime Flags bit 0 only for models that HAVE a render-only node: of 978 same-name pairs 933 MCC models are all-zero (928
  of them have no render-only node in Vista either), 45 are non-zero and 43 equal Vista's minus padding (the H2EK-built custom map has the same: 16/125 non-zero).  So:  node = Vista node flags with the padding
  bits cleared, sections = Vista, runtime = 1 iff any render-only bit.
* jpt! Flags bit 2: undefined in both plugins, SET by the MCC tool (stock MCC 1422/1422 pairs, H2EK-built custom map 92/98),
  never by the Vista tool -> set.
* prt3 Postprocess Definition: MCC keeps three elements ([1] and [2] always empty), Vista one -> padded to three.
* effe Flags bit 2 is builder-computed with a DIFFERENT rule in the two tools.  The MCC rule is not recoverable from the part
  groups alone (best group rule: 911/951 distinct stock MCC effects; ligh parts set it in 26 of 60 effects, jpt! parts in 12 of
  17), so the Vista bit is kept (it equals the MCC bit in 93.1% of the same-name pairs).
* Predicted Resources / Predicted Bitmaps: the lists are prefetch hints computed from the model + shader chain with the same
  rule in both tools; the generic remap carries them over.  A post pass removes entries that became dangling (tag not in the
  output) and exact duplicates, like the forward direction does.

Public: node handlers (hlmt '', jpt! '', prt3 ''), post pass predicted_cleanup, strip_padding_bits().
"""
import struct

from . import node_handler, post_pass
from . import _revmisc_common as C

NODE_FLAG_WORDS = 8
SECTION_FLAG_WORDS = 8
NULL = 0xFFFFFFFF
PRT3_POSTPROCESS_ELEMENTS = 3


def strip_padding_bits(words, n_nodes):
    """Clear every bit >= n_nodes in the 256-bit render-only node flag set (list of 8 dwords)."""
    out = []
    for w, v in enumerate(words):
        lo = w * 32
        if n_nodes >= lo + 32:
            out.append(v & 0xFFFFFFFF)
        elif n_nodes <= lo:
            out.append(0)
        else:
            out.append(v & ((1 << (n_nodes - lo)) - 1))
    return out


@node_handler('hlmt', '', prio=100)
def hlmt_flags(ctx, vista_node, mcc_node):
    n_nodes = len(C.blk(vista_node, 'Nodes'))
    node = [C.rd(vista_node, 'Render-Only Node Flags %d' % (i + 1), 0) for i in range(NODE_FLAG_WORDS)]
    sect = [C.rd(vista_node, 'Render-Only Section Flags %d' % (i + 1), 0) for i in range(SECTION_FLAG_WORDS)]
    node = strip_padding_bits(node, n_nodes)
    for i in range(NODE_FLAG_WORDS):
        C.wr(mcc_node, 'Render-Only Node Flags %d' % (i + 1), node[i])
    for i in range(SECTION_FLAG_WORDS):
        C.wr(mcc_node, 'Render-Only Section Flags %d' % (i + 1), sect[i])
    C.wr(mcc_node, 'Runtime Flags', 1 if any(node) else 0)
    C.stat(ctx, 'rev_computed:hlmt_flags')


@node_handler('jpt!', '', prio=100)
def jpt_flags(ctx, vista_node, mcc_node):
    f = next((x for x in mcc_node.fields if x.name == 'Flags' and x.offset == 0xC), None)
    if f is None:
        return
    fmt = {1: '<B', 2: '<H', 4: '<I'}.get(f.size)
    if fmt is None:
        return
    v = struct.unpack_from(fmt, mcc_node.data, f.offset)[0]
    if not v & 4:
        struct.pack_into(fmt, mcc_node.data, f.offset, v | 4)
        C.stat(ctx, 'rev_computed:jpt_flags_bit2')


@node_handler('prt3', '', prio=100)
def prt3_postprocess(ctx, vista_node, mcc_node):
    f = mcc_node.field('Postprocess Definition')
    if f is None or f.children is None:
        return
    kids = list(mcc_node.blocks.get(f.offset, []))
    if not kids or len(kids) >= PRT3_POSTPROCESS_ELEMENTS:
        return
    while len(kids) < PRT3_POSTPROCESS_ELEMENTS:
        kids.append(C.new_element(mcc_node, f))
    C.set_block(mcc_node, f, kids)
    C.stat(ctx, 'rev_computed:prt3_postprocess_padded')


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
                    C.set_block(n, f, keep)
                    cleaned += 1
                    removed += len(els) - len(keep)
    return cleaned, removed


@post_pass(prio=200)
def predicted_cleanup(ctx, converted):
    c, e = clean_predicted_blocks(converted)
    C.stat(ctx, 'rev_computed:predicted_blocks_cleaned', c)
    C.stat(ctx, 'rev_computed:predicted_entries_removed', e)
