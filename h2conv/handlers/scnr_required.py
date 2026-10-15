# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Blocks the Vista engine dereferences WITHOUT a count check - an empty one crashes on load.

scnr Level Data (0x398): halo2.exe sub_427EDD (Vista exe VA 0x427EDD, runs for every type-1 = multiplayer scenario right after
it loads; crash site 0x427F16) reads level_data[0] -> its Multiplayer block address word (+0x14) -> multiplayer[0].map_id
(compared with 100 / 1200 for two hard-coded spawn-position fixups).  It checks the address words against -1 but never checks
the counts, so an EMPTY Level Data block (cache {0, 0}) makes it read the tag cache base + 0x14 as a pointer and fault.
Found 2026-10-14: a custom MCC chronopolis (H2EK-built, scnr Level Data count 0) crashed Cartographer with
C0000005 at halo2.exe+0x27F16, EAX = garbage pointer.  Every stock Vista MP map has 1 Level Data element; the community
Vista chronopolis (which loads fine) has exactly 1 element = { Level Description = {unic, -1}, Campaign Level Data empty,
Multiplayer empty } - that is what we synthesise.  An empty Multiplayer block is safe: its address word 0 is turned into
base + 0 (a valid read) and the map id simply matches neither 100 nor 1200.
"""
from . import node_handler
from .. import tagtree as T

OFF_LEVEL_DATA = 0x398
LEVEL_DATA_ELEMENT = b'cinu' + b'\xff\xff\xff\xff' + b'\0' * 16      # {unic, -1} tagref, two empty blocks


@node_handler('scnr', '', prio=900)
def ensure_level_data(ctx, mcc_node, vista_node):
    if getattr(ctx, 'map_type', None) != 1:
        return
    kids = vista_node.blocks.get(OFF_LEVEL_DATA)
    if kids:
        return
    f = next((x for x in vista_node.fields if x.offset == OFF_LEVEL_DATA and x.type in T.BLOCK_TYPES), None)
    if f is None or T.element_size(f) != len(LEVEL_DATA_ELEMENT):
        ctx.warn('scnr_level_data_layout', 'unexpected scnr layout at 0x398; Level Data not synthesised')
        return
    el = T.Node(vista_node.group, T.child_path(vista_node.path, f), f.children, len(LEVEL_DATA_ELEMENT), LEVEL_DATA_ELEMENT)
    for c in f.children:
        if c.type in T.BLOCK_TYPES:
            el.blocks[c.offset] = []
        elif c.type in T.DATA_TYPES:
            el.datas[c.offset] = b''
    vista_node.blocks[OFF_LEVEL_DATA] = [el]
    ctx.stats['scnr_level_data_synthesised'] += 1
    ctx.warn('scnr_level_data_synthesised', 'multiplayer scenario had no Level Data (the engine reads element 0 unchecked in '
             'sub_427EDD and crashes): added one empty element like community Vista maps')
