# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Local tags that SHADOW a shared placeholder (same group + name) and the Cartographer code that keys on tag names.

Why: the 43 stock Vista maps never hold a local tag with the (group, name) of a tag that is also in their
instance table; a converted map can (the custom headlong had 506).  References are by full datum and unaffected, but Cartographer looks
tags up BY NAME (`tag_loaded`, cache_files.cpp:363 returns the first = lowest index = the LOCAL copy) in tag_fixes.cpp, H2X.cpp,
cartographer_player_profile_weapon_offsets.cpp, Infection.cpp, Birthday.cpp, Christmas.cpp, cheats.cpp ...: such patches land on the local
copy, not on the shared copy that shared tags (mulg, matg) still use.  The conversion report lists the count and the affected names.

    shadowed(plan)                   -> [(group, name, local out_index)]
    cartographer_keyed(names, dir)   -> {lower name: [source file, ...]}   string literals of the Cartographer source that equal a tag name
    summary(plan, src_dir=...)       -> JSON-able dict for the conversion report
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import collections
import os
import re

from . import paths as _paths


def cartographer_src_dir():
    """The Cartographer source folder to scan, from H2CONV_CARTOGRAPHER_SRC / settings.json "cartographer_src_dir"; None = no scan (it only adds a
    diagnostic line to the conversion report; the converted map does not depend on it)."""
    return _paths.pick('cartographer_src')[0]


_BS = chr(92)
_LITERAL = re.compile(r'"((?:[a-z0-9_\-.]+' + re.escape(_BS + _BS) + r')+[a-z0-9_\-.]+)"', re.I)
_CACHE = {}


def shadowed(plan):
    """Local plan entries whose (group, lower name) equals a shared placeholder's."""
    ph = set()
    for t in getattr(plan, 'shared_placeholders', ()) or ():
        if getattr(t, 'name', None):
            ph.add((t.group, t.name.lower()))
    out = []
    for e in getattr(plan, 'entries', ()) or ():
        if e.action in ('convert', 'donor') and e.name and (e.group, e.name.lower()) in ph:
            out.append((e.group, e.name, e.out_index))
    return out


def cartographer_literals(src_dir=None):
    """{lower tag-name-like string literal: sorted source files} of the Cartographer source tree ({} if it is not there)."""
    src_dir = src_dir or cartographer_src_dir()
    if not src_dir:
        return {}
    key = os.path.normcase(src_dir)
    if key in _CACHE:
        return _CACHE[key]
    lit = collections.defaultdict(set)
    if os.path.isdir(src_dir):
        for root, _dirs, files in os.walk(src_dir):
            for f in files:
                if not f.endswith(('.cpp', '.h', '.inl')):
                    continue
                p = os.path.join(root, f)
                try:
                    with open(p, encoding='utf-8', errors='ignore') as fh:
                        txt = fh.read()
                except OSError:
                    continue
                for m in _LITERAL.finditer(txt):
                    lit[m.group(1).replace(_BS + _BS, _BS).lower()].add(os.path.relpath(p, src_dir).replace('\\', '/'))
    res = {k: sorted(v) for k, v in lit.items()}
    _CACHE[key] = res
    return res


def cartographer_keyed(names, src_dir=None):
    lit = cartographer_literals(src_dir)
    return {n.lower(): lit[n.lower()] for n in names if n and n.lower() in lit}


def summary(plan, src_dir=None, max_items=60):
    src_dir = src_dir or cartographer_src_dir()
    sh = shadowed(plan)
    by_group = collections.Counter(g for g, _n, _i in sh)
    keyed = cartographer_keyed([n for _g, n, _i in sh], src_dir)
    items = []
    for g, n, i in sh:
        if n.lower() in keyed:
            items.append({'group': g, 'name': n, 'files': keyed[n.lower()][:3]})
    return {
        'count': len(sh),
        'by_group': dict(by_group.most_common()),
        'cartographer_source_found': bool(src_dir) and os.path.isdir(src_dir),
        'cartographer_keyed_count': len(items),
        'cartographer_keyed': items[:max_items],
    }
