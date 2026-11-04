# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Tag layouts of the Xbox build: Assembly Plugins\\Halo2Xbox (14 overrides) over Plugins\\Halo2 (the Vista set, with the converter's
cache-accurate corrections), as Engines.xml declares (fallbackPlugins Halo2).  The 54 groups without an override share the VERY
SAME Layout objects as load_layouts('vista', corrected=True), so a node decoded from an Xbox tag of those groups encodes straight
into Vista (4975/4975 tags of bulldog.map decode, 0 errors).

Differences of the 14 overrides vs Vista:
  bitm  root 0x4C (Vista 0x50: 4 compression bytes)       shad root 0x54 (Vista 0x5C: depth bias offset / slope scale)
  DECR / PRTM root -4                                       wgit root -8 (mouse cursor tagref), button +0x36 renamed
  sbsp / scnr Decorators Cache Blocks 0x2C (Vista 0x34)    hudg Player Training Data 0x14 (Vista 0x1C: 2 more string ids)
  vehi Phantom Shapes 0x2A0 (= Vista 0xF0 element + its 432-byte tail pad)
  jmad Animations 0x6C (resource-group index/offset, no dataref) + Tag Resource Groups / Unknown Resource blocks
  spas / vrtx: Xbox GPU code (replaced by the Vista tags of the same name)    skin / wigl: same layout"""
import os

from .. import plugins as P
from .. import paths as _paths

_CACHE = {}


def plugins_dir():
    return os.path.join(_paths.assembly_layout().plugins_root, 'Halo2Xbox')


def xbox_layouts():
    lays = _CACHE.get('xbox')
    if lays is not None:
        return lays
    lays = dict(P.load_layouts('vista', corrected=True))
    stems, names = P._stem_to_magic_map()
    d = plugins_dir()
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith('.xml'):
            continue
        stem = os.path.splitext(fn)[0]
        cands = stems.get(stem, [stem])
        magic = next((c for c in cands if c == stem), cands[0])
        lay = P.parse_plugin(os.path.join(d, fn), 'xbox')
        lay.group = magic
        lay.group_name = names.get(magic)
        lays[magic] = lay
    _CACHE['xbox'] = lays
    return lays


def vista_layouts():
    return P.load_layouts('vista', corrected=True)


OVERRIDES = ('DECR', 'PRTM', 'bitm', 'hudg', 'jmad', 'sbsp', 'scnr', 'shad', 'skin', 'spas', 'vehi', 'vrtx', 'wgit', 'wigl')
