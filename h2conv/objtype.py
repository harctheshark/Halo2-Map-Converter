# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Object tags whose data is another object type (2026-10-26).

Old Xbox mods repurposed a tag by pointing its index entry at another tag's data: Killtrocity's Aerial has four WEAPON entries
(ghost_gun, cannon_turret, ...) whose data is a MACHINE (the deltatemple bridges and gondolas).  Item collections spawn them through
netgame equipment and the engine builds whatever the definition's 'Object Type' (+0 of every object definition) says.  Every
converter keeps such a tag's own group in the tag index (that is what the item collection references) but decodes / encodes its
data with the layout of the group the Object Type names: layout_group()."""
import struct

# object_definition 'Object Type' (+0) -> tag group (Halo 2: biped .. creature)
OBJECT_TYPE_GROUPS = ('bipd', 'vehi', 'weap', 'eqip', 'garb', 'proj', 'scen', 'mach', 'ctrl', 'lifi', 'ssce', 'bloc', 'crea')


def pad_alias_root(root, index_group, layouts):
    """An aliased tag (data of root.group under an index_group entry, e.g. a 0x134-byte machine under a weapon entry) is zero-padded
    to index_group's main struct size: code that walks every tag of the index group reads ITS fields past the data's end.  MCC
    halo2.dll walks every weapon's New Triggers (+0x2C8) at map load (sub_180036240 -> charging effect); without the padding aerial
    crashed there at halo2.dll+0x36434 reading the bytes after the machine (2026-10-26).  The data's own bytes, Object Type included,
    are unchanged.  No-op for every non-aliased tag."""
    if root is None or root.group == index_group or index_group not in layouts:
        return
    lay = layouts[index_group]
    have, need = len(root.data), lay.base_size or lay.computed_size
    if have >= need:
        return
    root.data.extend(b'\0' * (need - have))
    # an EMPTY weapon past the data: tag references NONE (the walk hands its 13 reference datums to sub_1800360C0 - a zero datum
    # would be tag 0, the scenario), blocks / datas / scalars zero
    for f in lay.fields:
        if f.type.lower() == 'tagref' and f.offset >= have and f.offset + f.size <= need:
            root.data[f.offset:f.offset + f.size] = b'\xff' * f.size


def layout_group(group, read, addr, layouts=None):
    """The group whose layout the tag's data has: for an object tag, the group its Object Type names (when that differs and is a
    known object type), else `group` itself.  read(addr, size) -> bytes; any read error -> `group`.
    layouts ({group: layout}) -> the other group only when the data really DECODES with its layout (all 31 Xbox maps that use the
    trick: 104 aliases share their data with a tag of exactly that group; blockfort's high_shipping_crate scenery and Infiltration's
    house_gate machine carry another type number but only decode as themselves - those stay what they are)."""
    if group not in OBJECT_TYPE_GROUPS or not addr:
        return group
    try:
        t = struct.unpack_from('<h', read(addr, 2), 0)[0]
    except Exception:                                                    # noqa: BLE001
        return group
    other = OBJECT_TYPE_GROUPS[t] if 0 <= t < len(OBJECT_TYPE_GROUPS) else group
    if other == group or layouts is None:
        return other
    if other not in layouts:
        return group
    from . import tagtree as T                                           # only the rare mismatching object tags get here
    try:
        T.decode_tag(read, layouts[other], addr, other)
    except Exception:                                                    # noqa: BLE001
        return group
    return other
