# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Remastered (H2A) sound  ->  classic (Vista) sound pairing.

Vista has no `sound_remastered\\*` tags.  A reference to one is replaced by the CLASSIC sound that Vista plays for the same purpose.
The planner (planner._Builder.decide) asks `resolve_classic()`; three sources, in this order:

  1. the override file  `h2conv\\data\\remastered_sound_overrides.json`  ({"sound_remastered\\\\x": "sound\\\\y"}; null / "" = keep it dropped)
  2. MCC's own link: every classic `snd!` has a 'Remastered Sound Reference Index' into the `remastered` tag-reference table of its sound
     gestalt (ugh!), and every classic `lsnd` has a leading 'Remastered Looping Sound' reference.  The pair counts only when exactly ONE
     classic tag of that group points at the remastered tag (a remastered sound shared by several classic sounds is ambiguous: reported,
     not guessed)
  3. the path mapping `sound_remastered\\X` -> `sound\\X`

If none of them names a classic tag that exists (Vista shared.map or the MCC maps) the reference stays NULL, exactly as before.
Opt-out: option `no_remastered_pairs` (CLI --no-remastered-pairs, env H2CONV_NO_REMASTERED_PAIRS=1) turns 1 and 2 off (only the old path
mapping remains = the converter's behaviour before 2026-10-21).
"""
import json
import os
import struct

REMASTERED_PREFIX = 'sound_remastered\\'
LEGACY_PREFIX = 'sound\\'
PAIR_GROUPS = ('snd!', 'lsnd')
OVERRIDES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'remastered_sound_overrides.json')

_PAIR_CACHE = {}


def path_twin(name):
    """sound_remastered\\X -> sound\\X (lower case), None for a name outside the remastered folder."""
    lo = (name or '').lower()
    if not lo.startswith(REMASTERED_PREFIX):
        return None
    return LEGACY_PREFIX + lo[len(REMASTERED_PREFIX):]


def load_overrides(path=None):
    """{(group-less) remastered name lower: classic name lower or None}.  Keys starting with '_' are comments.  A value may be
    "sound\\\\x" (both snd! and lsnd) ; an unreadable file is an error that must be shown, a missing one is an empty table."""
    p = path or OVERRIDES_PATH
    if not os.path.isfile(p):
        return {}
    with open(p, 'r', encoding='utf-8') as fh:
        d = json.load(fh)
    out = {}
    for k, v in d.items():
        if k.startswith('_'):
            continue
        out[k.lower()] = (v.lower() if isinstance(v, str) and v.strip() else None)
    return out


def _own_gestalt(cache):
    t = getattr(cache, 'sound_gestalt', None)
    if t is None or not t.has_data:
        return None
    return t


def _remastered_table(cache):
    """[(group int, datum)] of the cache's own ugh! `remastered` block (MCC ugh! +0x50, 8-byte tag references)."""
    t = _own_gestalt(cache)
    if t is None:
        return []
    base = cache.read_at_addr(t.addr, 0x78)
    cnt, ptr = struct.unpack_from('<iI', base, 0x50)
    if cnt <= 0 or not ptr:
        return []
    raw = cache.read_at_addr(ptr, cnt * 8)
    return [struct.unpack_from('<II', raw, i * 8) for i in range(cnt)]


def mcc_links(cache, fallback=None):
    """{(group, remastered name lower): set(classic names lower)} from the MCC links of `cache` (an MccCache).  `fallback` resolves
    a datum the cache does not know (an H2EK map whose gestalt points at shared tags).  Cached per file."""
    key = (cache.path, os.path.getmtime(cache.path), id(fallback) if fallback is not None else 0)
    hit = _PAIR_CACHE.get(key)
    if hit is not None:
        return hit
    out = {}

    def target(datum):
        t = cache.tag_by_datum(datum)
        if t is None and fallback is not None:
            t = fallback.tag_by_datum(datum)
        return t

    try:
        table = _remastered_table(cache)
    except Exception:                                    # noqa: BLE001 - a damaged gestalt must not stop the conversion
        table = []
    for t in cache.tags:
        if t.is_null or not t.name or not t.has_data or t.name.lower().startswith(REMASTERED_PREFIX):
            continue
        try:
            if t.group == 'snd!' and table:
                body = cache.read_at_addr(t.addr, 6)
                idx = struct.unpack_from('<h', body, 4)[0]
                if idx < 0 or idx >= len(table):
                    continue
                rt = target(table[idx][1])
            elif t.group == 'lsnd':
                body = cache.read_at_addr(t.addr, 0x10)
                rt = target(struct.unpack_from('<I', body, 4)[0])
            else:
                continue
        except Exception:                                # noqa: BLE001
            continue
        if rt is None or not rt.name or rt.group != t.group or not rt.name.lower().startswith(REMASTERED_PREFIX):
            continue
        out.setdefault((t.group, rt.name.lower()), set()).add(t.name.lower())
    _PAIR_CACHE[key] = out
    return out


def merge_links(*tables):
    out = {}
    for tb in tables:
        for k, v in tb.items():
            out.setdefault(k, set()).update(v)
    return out


def unique_link(links, group, remastered_lower):
    """The single classic name MCC links to, None when there is none or it is ambiguous."""
    s = links.get((group, remastered_lower))
    if s and len(s) == 1:
        return next(iter(s))
    return None


def same_family(remastered_lower, classic_lower):
    """Sanity rule for an MCC link: the first two folders after the root must agree (weapons\\sniper_rifle, vehicles\\banshee,
    characters\\footsteps, materials\\particles ...).  MCC reuses one remastered loop for unrelated classic loops in a few places
    (the Scorpion turret loop is linked from the Warthog turret traverse loop); such a link is reported, not applied."""
    a = remastered_lower[len(REMASTERED_PREFIX):].split('\\')[:2]
    b = classic_lower[len(LEGACY_PREFIX):].split('\\')[:2] if classic_lower.startswith(LEGACY_PREFIX) else []
    return bool(a) and a == b
