# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Tag NAME RECOVERY for Xbox maps whose tag-name table was blanked by a "protection" tool (2026-10-25).

Nine pack maps have no tag names at all (Phantom tokyo / miami / 3volvebeta / california / funbox / havok, Killtrocity Cassiopeia, Killjoy
evolveation / havoklation) and a few more have part of the table wiped (Dawn_of_Warfare 1,151 of 4,695).  The converter matches by name
everywhere (Vista templates, links to Vista shared.map, donor vertex shaders), so such a map lost 374 shaders (tokyo) and crashed.

Recovery, NOT invention:
  1. every named tag of the pack's shared.map gets a reference-blind FINGERPRINT: block structure, plain bytes, data, string-id texts,
     the GROUP of each reference (not its target), raw data by its bytes; pointers / runtime words ignored (the same field rules as
     plan_xbox._same, plan_xbox.field_kind).  snd! = the resolved gestalt content (sound_xbox.Gestalt.fingerprint), unic = its strings.
  2. a nameless map tag whose fingerprint matches exactly one shared tag of its group takes that name;
  3. ties (identical content under several names) are broken by the base level (the name the stock map the mod was built from has at
     the tag's datum), then by the references: the candidate whose referenced tags' names equal the names already recovered for this
     tag's references wins; repeated until nothing changes;
  4. what is left is the map's own content: scenario / globals / BSPs / lightmaps get the names the engine expects (from the map header and
     the scenario's Structure BSPs), everything else a plain LABEL  recovered\\<scenario name>\\<group>_<index>  (reported).
Names already present are never changed."""
import collections
import hashlib
import re
import struct

from .. import tagtree as T
from . import layouts as XL
from . import sound_xbox as SX
from . import plan_xbox as PX

NULL = 0xFFFFFFFF
_GOOD = re.compile(r'^[a-z0-9_ \-\.\(\)\'\\&,!#\[\]+~]+$', re.I)
EXT = {'scnr': 'scenario', 'sbsp': 'structure_bsp', 'ltmp': 'lightmap', 'matg': 'globals'}


def name_is_valid(name):
    return bool(name) and '\\' in name and bool(_GOOD.match(name))


def nameless_tags(xc):
    """Tags that need a name (the sound gestalt has a junk name in every pack map: left alone)."""
    return [t for t in xc.tags if not t.is_empty and t.group != 'ugh!' and not name_is_valid(t.name)]


class _Fp(object):
    def __init__(self, cache, shared_loc):
        self.c = cache
        # location bits of a raw pointer into the pack shared.map (2 from a map, 0 in shared.map itself); unused since raw data is hashed
        # by its bytes (index v3), kept for the callers
        self.shared_loc = shared_loc
        self.lay = XL.xbox_layouts()
        self.gestalt = None
        self.loc = None

    def tag(self, t):
        if t.group == 'snd!':
            if self.gestalt is None:
                self.gestalt = SX.Gestalt(self.c)
            return ('snd!', repr(self.gestalt.fingerprint(self.c.read(t.addr, 0x14), names=False)))
        if t.group == 'unic':
            if self.loc is None:
                self.loc = SX.locale_tables(self.c)
            return ('unic', repr(SX.unic_strings(self.c, t, self.loc)))
        if t.group not in self.lay:
            return None
        root = T.decode_tag(self.c.read, self.lay[t.group], self.c.tag_addr(t), t.group)
        h = hashlib.sha1()
        self.node(root, h)
        return (t.group, h.hexdigest())

    def node(self, n, h):
        # plan_xbox.field_kind's rules (2026-10-26): bitm LOD2 / LOD3 and Havok User Data ignored, raw data by its BYTES - a raw
        # pointer by shared.map offset ('P-local' for the map's own data) made every moved LOD1 / geometry block a different tag and every
        # local one the same (audit-tag-tricks RC3)
        skip = set()
        for f in sorted(n.fields, key=lambda x: x.offset):
            k = PX.field_kind(n, f)
            if k == 'block':
                kids = n.blocks.get(f.offset) or []
                h.update(b'B%d|' % len(kids))
                for x in kids:
                    self.node(x, h)
                skip.update(range(f.offset, f.offset + 8))
            elif k == 'data':
                h.update(b'D' + bytes(n.datas.get(f.offset) or b'') + b'|')
                skip.update(range(f.offset, f.offset + 8))
            elif f.type.lower() == 'tagref':
                o = f.offset + (4 if f.size == 8 else 0)
                d = struct.unpack_from('<I', n.data, o)[0]
                if f.size == 8:
                    g = bytes(n.data[f.offset:f.offset + 4])     # the stored group
                else:
                    # a 4-byte reference is the DATUM alone (bitm Bitmaps 'Datum', mode Sections / ltmp 'Owner Tag', jmad Tag Resource
                    # Groups, Predicted Resources 'Tag Index'): hashed as bytes it was the tag's own placement, so only a copy with the
                    # same datum (the base level the mod was built from) ever matched.  Cassiopeia's UI bitmaps lost their names when
                    # raw data was hashed by bytes (the pack mainmenu re-skinned lockout's): the pack shared.map copies had the same
                    # pixels but other datums (2026-10-26).  The group of the target, as for 8-byte references
                    tt = self.c.tag_by_datum(d) if d not in (0, NULL) else None
                    g = b'4' + (tt.group.encode('latin1', 'replace') if tt is not None else b'?')
                h.update(b'R' + g + (b'0' if d in (0, NULL) else b'1'))
                skip.update(range(f.offset, f.offset + f.size))
            elif k == 'sid':
                h.update(b'S' + (self.c.string_id(struct.unpack_from('<I', n.data, f.offset)[0]) or '').encode('latin1', 'replace') + b'|')
                skip.update(range(f.offset, f.offset + 4))
            elif k == 'skip':
                skip.update(range(f.offset, f.offset + f.size))
            elif k == 'raw':
                h.update(PX._raw_token(self.c, n.u32(f.offset), PX.raw_size(n, f)))
                skip.update(range(f.offset, f.offset + 4))
        h.update(bytes(b for i, b in enumerate(n.data) if i not in skip))


def _refs(cache, lay, t):
    """datums referenced by tag t (all nodes)."""
    if t.group not in lay:
        return []
    root = T.decode_tag(cache.read, lay[t.group], cache.tag_addr(t), t.group)
    out = []
    for n in root.iter_nodes():
        for f in n.fields:
            if f.type.lower() == 'tagref':
                d = struct.unpack_from('<I', n.data, f.offset + (4 if f.size == 8 else 0))[0]
                if d not in (0, NULL):
                    out.append(d)
    return out


class _Ref(object):
    """A named reference tag: (group, name) + where it came from (for the reference tie-break)."""
    __slots__ = ('group', 'name', 'cache', 'datum', 'refnames')

    def __init__(self, group, name, cache, datum, refnames=None):
        self.group, self.name, self.cache, self.datum, self.refnames = group, name, cache, datum, refnames


def _index_cache(cache, shared_loc, groups, lay, keep_refs):
    """{(group, digest): [_Ref]} of the named tags of one reference map."""
    fp = _Fp(cache, shared_loc)
    out = collections.defaultdict(list)
    for t in cache.tags:
        if t.is_empty or not t.addr or t.group not in groups or not name_is_valid(t.name):
            continue
        try:
            k = fp.tag(t)
        except Exception:                                    # noqa: BLE001
            continue
        if k is None:
            continue
        rn = None
        if keep_refs:
            try:
                rn = [(x.group, x.name.lower()) if x is not None and x.name else None for x in (cache.tag_by_datum(d) for d in _refs(cache, lay, t))]
            except Exception:                                # noqa: BLE001
                rn = None
        out[k].append(_Ref(t.group, t.name, None, t.datum, rn))
    return out


def _stock_index(path, groups, lay, cache_dir, log):
    """Index of one stock Xbox map, cached on disk (keyed by the map's size and a hash of a fixed sample of its bytes: stock_index.sample_digest)."""
    import os
    import pickle
    from .xbox_cache import XboxCache
    # v2: snd! fingerprints without import names (an index built with them would never match again); v4: raw data by bytes, bitm LOD2/3
    # and Havok User Data ignored, 4-byte references by target group (_Fp.node).  The key also holds stock_index.rules_hash() (the Xbox
    # layouts + plan_xbox's field tables, which _Fp follows), so a layout or rule edit never serves stale fingerprints; a new generation
    # of a map's file removes that map's older ones
    from .stock_index import rules_hash, sample_digest, prune_generations
    key = hashlib.sha1(('v4|%s|%s' % (rules_hash(), sample_digest(path))).encode()).hexdigest()[:16]
    base = os.path.splitext(os.path.basename(path))[0]
    fn = os.path.join(cache_dir, 'names_%s_%s.pkl' % (base, key)) if cache_dir else None
    idx = None
    if fn and os.path.isfile(fn):
        try:
            with open(fn, 'rb') as fh:
                idx = pickle.load(fh)
        except Exception as ex:                                  # noqa: BLE001
            log('name recovery: %s unreadable (%s) - rebuilt' % (os.path.basename(fn), ex))
    if idx is None:
        log('name recovery: indexing %s (first use: kept in %s for later conversions) ...' % (os.path.basename(path), cache_dir or 'memory only'))
        c = XboxCache(path)
        idx = dict(_index_cache(c, 2, ALL_GROUPS, lay, True))
        for v in idx.values():
            for r in v:
                r.cache = None
        if fn:
            # per-process tmp + os.replace: parallel workers rebuilding a missing index never race on one file or leave a half-written
            # one; a failed save only costs a rebuild next time
            tmp = '%s.%d.tmp' % (fn, os.getpid())
            try:
                os.makedirs(cache_dir, exist_ok=True)
                with open(tmp, 'wb') as fh:
                    pickle.dump(idx, fh)
                os.replace(tmp, fn)
                prune_generations(cache_dir, 'names_%s_' % base, os.path.basename(fn))
            except Exception as ex:                              # noqa: BLE001
                log('name recovery: %s not saved (%s)' % (os.path.basename(fn), ex))
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        log('name recovery: indexed %s (%d fingerprints)' % (os.path.basename(path), len(idx)))
    return {k: v for k, v in idx.items() if k[0] in groups}


ALL_GROUPS = None                                            # set lazily: every group the Xbox layouts know


def recover_names(xc, ps, log=None, stock_dir=None, cache_dir=None):
    """Fill in the names of xc's nameless tags (in place).  ps = the pack's shared.map XboxCache; stock_dir = folder of the stock Xbox
    maps (more references: the base level the mod was built from, the campaign maps, single_player_shared); cache_dir = where their
    indexes are kept.  -> stats dict."""
    import os
    global ALL_GROUPS
    log = log or (lambda m: None)
    todo = nameless_tags(xc)
    stats = collections.Counter(nameless=len(todo))
    if not todo:
        return stats
    lay = XL.xbox_layouts()
    if ALL_GROUPS is None:
        ALL_GROUPS = set(lay) | {'snd!', 'unic'}
    # every group of the map, not only the nameless tags' groups: a scrambled table (_scrambled_names) adds tags of other groups
    groups = {t.group for t in xc.tags if not t.is_empty}
    fp_map = _Fp(xc, 2)
    index = collections.defaultdict(list)                    # (group, digest) -> [_Ref], in priority order
    base_by_datum = {}                                       # datum -> (group, lower name) of the base level's tags
    for k, v in _index_cache(ps, 0, groups, lay, True).items():
        index[k].extend(v)
    if stock_dir and os.path.isdir(stock_dir):
        base = (xc.header.get('scenario_path') or '').strip('\0').rsplit('\\', 1)[-1].lower()
        maps = sorted(f for f in os.listdir(stock_dir) if f.lower().endswith('.map') and f.lower() not in ('shared.map',))
        maps.sort(key=lambda f: (os.path.splitext(f)[0].lower() != base, f.lower()))       # the base level first
        for f in maps:
            try:
                for k, v in _stock_index(os.path.join(stock_dir, f), groups, lay, cache_dir, log).items():
                    if os.path.splitext(f)[0].lower() == base:
                        for r in v:
                            base_by_datum[r.datum] = (r.group, r.name.lower())
                    seen = {(r.group, r.name.lower()) for r in index[k]}
                    index[k].extend(r for r in v if (r.group, r.name.lower()) not in seen)
            except Exception as ex:                          # noqa: BLE001
                log('name recovery: %s not usable (%s)' % (f, ex))
    stock_names = collections.defaultdict(dict)              # group -> {lower name: name} of every reference tag
    for v in index.values():
        for r in v:
            stock_names[r.group].setdefault(r.name.lower(), r.name)
    frags = _scrambled_names(xc, stock_names, stats)        # tag index -> the name fragment it had
    todo += [xc.tags[i] for i in frags]
    used = {(t.group, t.name.lower()) for t in xc.tags if not t.is_empty and name_is_valid(t.name)}
    pending = {}
    for t in todo:
        try:
            k = fp_map.tag(t)
        except Exception:                                    # noqa: BLE001
            k = None
        cands = [c for c in index.get(k, ()) if (c.group, c.name.lower()) not in used] if k is not None else []
        frag = frags.get(t.index)
        if frag and len(cands) > 1:                          # identical content under several names: the one the fragment is the tail of
            cands = [c for c in cands if c.name.lower().endswith(frag.lower())] or cands
        if len(cands) > 1:
            # identical content under several names: the name the BASE level (the stock map the mod was built from, which keeps its
            # tag order) has at this datum.  Before that this came by accident from hashing 4-byte references as bytes (_Fp.node);
            # without it Cassiopeia (lockout) swapped content-identical tags' names (soft_organic_flesh_grunt /
            # hard_metal_thin_cov_grunt, ghost_damage / blown_open).  A datum alone is no evidence: other maps' datums coincide
            # (itmc single_weapons were shuffled when any candidate's datum counted)
            bn = base_by_datum.get(t.datum)
            same = [c for c in cands if (c.group, c.name.lower()) == bn] if bn is not None and bn[0] == t.group else []
            if len(same) == 1:
                cands = same
                stats['matched_by_datum'] += 1                   # (also counted in matched_unique)
        if len(cands) == 1:
            t.name = cands[0].name
            used.add((t.group, t.name.lower()))
            stats['matched_unique'] += 1
        elif cands:
            pending[t.index] = (t, cands)
    changed = True
    while changed and pending:
        changed = False
        for idx in list(pending):
            t, cands = pending[idx]
            mine = [xc.tag_by_datum(d) for d in _refs(xc, lay, t)]
            mine_names = [(m.group, m.name.lower()) if m is not None and name_is_valid(m.name) else None for m in mine]
            good = []
            for c in cands:
                if (c.group, c.name.lower()) in used or c.refnames is None:
                    continue
                tn = c.refnames
                if len(tn) == len(mine_names) and all(a is None or a == b for a, b in zip(mine_names, tn)):
                    good.append(c)
            if len(good) == 1 or (good and all(m is not None for m in mine_names)):
                t.name = good[0].name
                used.add((t.group, t.name.lower()))
                stats['matched_by_references'] += 1
                del pending[idx]
                changed = True
    for t, cands in pending.values():                        # still tied: identical content, the name does not change what converts
        c = next((c for c in cands if (c.group, c.name.lower()) not in used), None)
        if c is not None:
            t.name = c.name
            used.add((t.group, t.name.lower()))
            stats['matched_tie_first'] += 1
    _restore_fragments(xc, frags, stock_names, used, stats)
    _name_own_content(xc, used, stats)
    log('name recovery: %d nameless tags -> %s' % (len(todo), dict(stats)))
    return stats


def _scrambled_names(xc, stock_names, stats):
    """A name table that is shifted, not blanked (dawn_of_warfare: from tag 493 on every name lost its first 6 characters,
    'objects\\weapons\\...' -> 's\\weapons\\...'): when most of the valid-looking names no reference tag uses are the tail of a reference
    name, those names are fragments.  They are cleared (recovered like nameless tags).  -> {tag index: fragment}."""
    valid = [t for t in xc.tags if not t.is_empty and t.group != 'ugh!' and name_is_valid(t.name)]
    unknown = [t for t in valid if t.name.lower() not in stock_names[t.group]]
    tails = [t for t in unknown if any(s.endswith(t.name.lower()) for s in stock_names[t.group])]
    if len(tails) < 50 or len(tails) < 0.25 * len(valid):
        return {}
    frags = {}
    for t in unknown:
        frags[t.index] = t.name
        t.name = ''
    stats['scrambled_names'] = len(unknown)
    stats['scrambled_tails'] = len(tails)
    return frags


def _restore_fragments(xc, frags, stock_names, used, stats):
    """Fragments no content match named: the one reference name ending with the fragment, else the fragment itself (made unique).
    The level's own scenario / BSP / lightmap / globals are named from the header by _name_own_content."""
    for i, frag in frags.items():
        t = xc.tags[i]
        if name_is_valid(t.name) or t.group in ('scnr', 'sbsp', 'ltmp', 'matg'):
            continue
        fl = frag.lower()
        full = [n for k, n in stock_names[t.group].items() if k.endswith(fl) and (t.group, k) not in used]
        if not full and len(stock_names[t.group]) == 1 and sum(1 for x in xc.tags if x.group == t.group and not x.is_empty) == 1:
            # a one-per-map group with one stock name (dawn's mulg was given a scenery fragment, '\\burial_mounds\\ship_debris\\...')
            full = list(stock_names[t.group].values())
        if len(full) == 1:
            t.name = full[0]
            stats['fragment_restored'] += 1
        else:
            n, k = frag, 1
            while (t.group, n.lower()) in used:
                k += 1
                n = '%s_%d' % (frag, k)
            t.name = n
            stats['fragment_kept'] += 1
        used.add((t.group, t.name.lower()))


def _name_own_content(xc, used, stats):
    scen_path = (xc.header.get('scenario_path') or '').strip('\0') or 'recovered\\unnamed\\unnamed'
    base = scen_path.rsplit('\\', 1)[-1]

    def take(t, name):
        n, i = name, 1
        while (t.group, n.lower()) in used:
            i += 1
            n = '%s_%d' % (name, i)
        t.name = n
        used.add((t.group, n.lower()))

    sc = xc.scenario
    if sc is not None and not name_is_valid(sc.name):
        take(sc, scen_path)
        stats['named_scenario'] += 1
    g = getattr(xc, 'globals_tag', None)
    if callable(g):
        g = g()
    if g is None:
        g = next((t for t in xc.tags if not t.is_empty and t.group == 'matg'), None)
    if g is not None and not name_is_valid(g.name):
        take(g, 'globals\\globals')
        stats['named_globals'] += 1
    bsps = [t for t in xc.tags if not t.is_empty and t.group == 'sbsp']
    for i, t in enumerate(bsps):
        if not name_is_valid(t.name):
            take(t, scen_path if len(bsps) == 1 else '%s_bsp_%d' % (scen_path, i))
            stats['named_bsp'] += 1
    for t in xc.tags:
        if not t.is_empty and t.group == 'ltmp' and not name_is_valid(t.name):
            take(t, '%s_%s_lightmap' % (scen_path, base))
            stats['named_lightmap'] += 1
    for t in xc.tags:                                       # the map's gestalt: a junk name with control characters (funbox)
        if not t.is_empty and t.group == 'ugh!' and not all(' ' <= ch <= '~' for ch in (t.name or '?')):
            t.name = "i've got a lovely bunch of coconuts"      # the name of the secondary ugh! in every stock Vista MP map
            stats['named_gestalt'] += 1
    for t in xc.tags:
        if not t.is_empty and t.group != 'ugh!' and not name_is_valid(t.name):
            take(t, 'recovered\\%s\\%s_%04d' % (base, t.group.strip().replace('!', '').replace('*', ''), t.index))
            stats['labelled'] += 1
