# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Cached CONTENT index of the stock Xbox maps: (group, lower name) -> [(map, content digest)].

Two questions need every stock Xbox copy of a tag name:
  * plan_xbox: is a tag whose name the pack's shared.map lacks ('unverifiable': stock content of another level) or whose pack copy
    differs (a level variant / cache-build trim) really stock?  760 unverifiable links of the batch differed from every stock copy
    (aerial tough_terrain_mud Emitters, hlmt crate_tech Collision Regions names, coll panel_truck planes; audit-tag-tricks RC8), and
    every stock Xbox MP map carries a 654-animation masterchief jmad where shared.map has 662 - Bungie's Vista port links it all the
    same (0 of 7,700 local tags of the 25 stock Vista MP maps share a name with Vista's shared.map: a probe);
  * convert_xbox.stock_xbox_equal (shader identity), which opened and decoded ~26 stock maps per conversion (+32 % time).
The digest is plan_xbox.content_digest (equal digests <=> plan_xbox._same: references by name, string ids by text, raw data by its
bytes, LOD2/3 / Havok User Data / placement words ignored); snd! = the gestalt-resolved content without import names
(sound_xbox.Gestalt.fingerprint(names=False), equal_to_pack's acceptance), unic = its strings.  A stock map is opened only when its
tags are needed themselves (StockIndex.tags: closest_stock_arrays, bitmap pixels).

Files (in the converter's cache, <work folder>\\xbox_stock_index; paths.xbox_index_dir): content_<map>_<key>.pkl per stock map and
combined_<key>.pkl, all of them combined (what a conversion loads).  A map's key is its SIZE and a hash of a fixed sample of its bytes (the
first 2 MB, the last MB and 32 evenly spaced 64 KB blocks) with INDEX_VERSION and rules_hash (the layouts + field tables): the same stock
maps in another folder, on another PC, or copied again (new modification time) give the same key; a different dump of a map gives another
one.  Missing files are built on first use (with progress lines in the log: a few minutes for the whole set); writing a new generation of a
map's file removes the older ones of that map, so the cache does not grow with each change of the stock folder.  `h2conv cache info` lists
the folder, `h2conv cache clear` removes it, the size cap never trims it."""
import hashlib
import os
import pickle
import re

from .. import tagtree as T
from . import layouts as XL
from . import plan_xbox as PX
from . import sound_xbox as SX

# 2: sound_xbox._node_bytes by field_kind (Extra Info owner datum / lipsync pointer); bump whenever plan_xbox.field_kind or a digest changes
INDEX_VERSION = 2
# never compared with a stock copy: the map's own content (plan_xbox.NEVER_LINK) and the shader family (always Vista's)
SKIP_GROUPS = set(PX.NEVER_LINK) | set(PX.SHADER_FAMILY)
MP_TYPE = 1                                      # header scenario type of a multiplayer map (0 campaign, 2 mainmenu, 3 / 4 shared)


class Digester(object):
    """content digests of the tags of one XboxCache (snd! / unic resolved through that map's gestalt / locale tables)."""

    def __init__(self, cache):
        self.c = cache
        self.lay = XL.xbox_layouts()
        self.gestalt = None
        self.loc = None

    def tag(self, t, root=None):
        """20-byte digest of tag t, or None (no layout).  root: t already decoded."""
        if t.group == 'snd!':
            if self.gestalt is None:
                self.gestalt = SX.Gestalt(self.c)
            fp = self.gestalt.fingerprint(self.c.read(t.addr, 0x14), names=False)
            return hashlib.sha1(b'snd!' + repr(fp).encode('latin1', 'replace')).digest()
        if t.group == 'unic':
            if self.loc is None:
                self.loc = SX.locale_tables(self.c)
            return hashlib.sha1(b'unic' + repr(SX.unic_strings(self.c, t, self.loc)).encode('latin1', 'replace')).digest()
        if t.group not in self.lay:
            return None
        if root is None:
            root = T.decode_tag(self.c.read, self.lay[t.group], self.c.tag_addr(t), t.group)
        return PX.content_digest(root, self.c)


_RULES = None


def _layout_sig(fields):
    return [(f.type, f.name, f.offset, f.size, f.element_size, _layout_sig(f.children) if f.children else None) for f in fields]


def rules_hash():
    """Short hash of everything a digest depends on besides the map bytes: the Xbox layouts (XL.xbox_layouts: types, names, offsets,
    sizes, element sizes) and plan_xbox's field tables (IGNORED_GROUP_FIELDS, RUNTIME_FIELD_NAMES, RAW_FIELD_NAMES, RAW_SIZE_FIELD,
    IGNORED_FIELD_NAMES, IGNORED_REFS).  Part of every index key, so a layout or rule edit made without an INDEX_VERSION bump never
    serves stale digests; name_recovery keys its index with it too."""
    global _RULES
    if _RULES is None:
        lx = XL.xbox_layouts()
        sig = [(g, _layout_sig(lx[g].fields), lx[g].base_size or lx[g].computed_size) for g in sorted(lx)]
        tabs = [sorted((k, sorted(v)) for k, v in PX.IGNORED_GROUP_FIELDS.items()), sorted(PX.RUNTIME_FIELD_NAMES), sorted(PX.RAW_FIELD_NAMES),
                sorted(PX.RAW_SIZE_FIELD.items()), sorted(PX.IGNORED_FIELD_NAMES), sorted(PX.IGNORED_REFS)]
        _RULES = hashlib.sha1(repr((sig, tabs)).encode('utf-8', 'replace')).hexdigest()[:12]
    return _RULES


SAMPLE_HEAD, SAMPLE_TAIL, SAMPLE_BLOCKS, SAMPLE_BLOCK = 2 << 20, 1 << 20, 32, 64 << 10
_SAMPLES = {}


def sample_digest(path):
    """'<size>-<sha1 of a fixed sample>' of a file: its size, the first 2 MB, the last MB and 32 evenly spaced 64 KB blocks.  A read of
    about 5 MB whatever the file size; two different dumps of a map differ in size or in a sampled byte.  Independent of the file's
    path, name and modification time.  Remembered for the life of the process (keyed by path, size and mtime)."""
    st = os.stat(path)
    memo = (os.path.abspath(path).lower(), st.st_size, st.st_mtime_ns)
    hit = _SAMPLES.get(memo)
    if hit is not None:
        return hit
    size = st.st_size
    h = hashlib.sha1(b'sample-v1|%d|' % size)
    with open(path, 'rb') as fh:
        h.update(fh.read(SAMPLE_HEAD))
        if size > SAMPLE_HEAD + SAMPLE_TAIL:
            fh.seek(size - SAMPLE_TAIL)
            h.update(fh.read(SAMPLE_TAIL))
            span = size - SAMPLE_HEAD - SAMPLE_TAIL - SAMPLE_BLOCK
            if span > 0:
                for i in range(SAMPLE_BLOCKS):
                    fh.seek(SAMPLE_HEAD + span * i // (SAMPLE_BLOCKS - 1))
                    h.update(fh.read(SAMPLE_BLOCK))
    _SAMPLES[memo] = '%d-%s' % (size, h.hexdigest()[:24])
    return _SAMPLES[memo]


def _key(path):
    """Index key of one stock map: its size + content sample (sample_digest), the index version and the rules hash."""
    return hashlib.sha1(('stock-content-v%d|%s|%s' % (INDEX_VERSION, rules_hash(), sample_digest(path))).encode()).hexdigest()[:16]


def prune_generations(cache_dir, prefix, keep):
    """Remove the other generations of an index file: <prefix><16 hex>.pkl except the file named `keep`.  Never raises."""
    if not cache_dir:
        return
    pat = re.compile(re.escape(prefix) + r'[0-9a-f]{16}\.pkl$', re.I)
    try:
        for n in os.listdir(cache_dir):
            if n != keep and pat.match(n):
                try:
                    os.remove(os.path.join(cache_dir, n))
                except OSError:
                    pass
    except OSError:
        pass


def stock_status(stock_dir):
    """What a stock Xbox maps folder holds: dict(dir, exists, maps [file names of its Halo 2 Xbox (cache v8) maps], other [the other .map
    files: Halo 3 / Reach / Vista ...], has_shared, has_mainmenu, has_sp_shared, campaign [the stock campaign maps in it]).  A folder that
    is None, missing or holds no Halo 2 Xbox map gives maps == []."""
    from .xbox_cache import classify_header
    out = {'dir': stock_dir, 'exists': bool(stock_dir) and os.path.isdir(stock_dir), 'maps': [], 'other': [], 'has_shared': False,
           'has_mainmenu': False, 'has_sp_shared': False, 'campaign': []}
    if not out['exists']:
        return out
    try:
        names = sorted(os.listdir(stock_dir), key=str.lower)
    except OSError:
        out['exists'] = False
        return out
    for n in names:
        p = os.path.join(stock_dir, n)
        if not n.lower().endswith('.map') or not os.path.isfile(p):
            continue
        try:
            with open(p, 'rb') as fh:
                bad = classify_header(fh.read(0x800), os.path.getsize(p))
        except OSError:
            bad = ('unreadable', 'unreadable')
        (out['other'] if bad and bad[0] != 'truncated' else out['maps']).append(n)
    low = {n.lower() for n in out['maps']}
    out['has_shared'], out['has_mainmenu'], out['has_sp_shared'] = 'shared.map' in low, 'mainmenu.map' in low, 'single_player_shared.map' in low
    out['campaign'] = [n for n in out['maps'] if re.match(r'^\d\d[a-z]_', n.lower())]
    return out


def _save(fn, obj, log=None):
    """Write an index file atomically: a per-process tmp name (parallel rebuild_all_xbox workers rebuilding a missing index raced on one
    '.tmp'), then os.replace - never a half-written index (the PC may switch off mid-run).  A failed save costs only a rebuild next
    time, never the conversion."""
    tmp = '%s.%d.tmp' % (fn, os.getpid())
    try:
        with open(tmp, 'wb') as fh:
            pickle.dump(obj, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, fn)
    except Exception as ex:                                              # noqa: BLE001
        if log:
            log('stock index: %s not saved (%s)' % (os.path.basename(fn), ex))
        try:
            os.remove(tmp)
        except OSError:
            pass


def index_map(path, stock_dir):
    """{'type': header scenario type, 'tags': {(group, lower name): [digest, ...]}} of one stock Xbox map (raises if it is no v8 map)."""
    from .xbox_cache import XboxCache
    c = XboxCache(path, shared_dir=stock_dir)
    try:
        dg = Digester(c)
        out = {}
        for t in c.tags:
            if t.is_empty or not t.addr or not t.name or t.group in SKIP_GROUPS:
                continue
            try:
                d = dg.tag(t)
            except Exception:                                            # noqa: BLE001
                continue                         # undecodable in the stock map itself: no evidence either way
            if d is not None:
                out.setdefault((t.group, t.name.lower()), []).append(d)
        return {'type': c.header['scenario_type'], 'tags': out}
    finally:
        c.close()


class StockIndex(object):
    """The combined index of every stock Xbox map in stock_dir (cached in cache_dir)."""

    def __init__(self, stock_dir, cache_dir, log=None):
        self.stock_dir = stock_dir
        self.cache_dir = cache_dir
        self.log = log or (lambda m: None)
        self.maps = []                           # [(basename, header scenario type)]
        self.names = {}                          # (group, lower name) -> [(map number, digest)]
        self._caches = {}
        if stock_dir and os.path.isdir(stock_dir):
            self._load()

    def _load(self):
        paths = sorted(os.path.join(self.stock_dir, f) for f in os.listdir(self.stock_dir) if f.lower().endswith('.map'))
        keys = [(p, _key(p)) for p in paths]
        ck = hashlib.sha1('|'.join(k for _, k in keys).encode()).hexdigest()[:16]
        cfn = os.path.join(self.cache_dir, 'combined_%s.pkl' % ck) if self.cache_dir else None
        if cfn and os.path.isfile(cfn):
            try:
                with open(cfn, 'rb') as fh:
                    d = pickle.load(fh)
                self.maps, self.names = d['maps'], d['names']
                return
            except Exception as ex:                                      # noqa: BLE001
                self.log('stock index: %s unreadable (%s) - rebuilt' % (os.path.basename(cfn), ex))
        maps, names = [], {}
        todo = [p for p, k in keys if not (self.cache_dir and os.path.isfile(os.path.join(
            self.cache_dir, 'content_%s_%s.pkl' % (os.path.splitext(os.path.basename(p))[0], k))))]
        if todo:
            self.log('stock index: building the index of the original Xbox maps (%d of %d maps; first use or a stock map changed - a few '
                     'minutes; kept in %s and reused by every later conversion)' % (len(todo), len(keys), self.cache_dir or 'memory only'))
        done = 0
        for p, k in keys:
            base = os.path.splitext(os.path.basename(p))[0]
            fn = os.path.join(self.cache_dir, 'content_%s_%s.pkl' % (base, k)) if self.cache_dir else None
            d = None
            if fn and os.path.isfile(fn):
                try:
                    with open(fn, 'rb') as fh:
                        d = pickle.load(fh)
                except Exception:                                        # noqa: BLE001
                    d = None
            if d is None:
                done += 1
                self.log('stock index: [%d/%d] indexing %s ...' % (done, len(todo), os.path.basename(p)))
                try:
                    d = index_map(p, self.stock_dir)
                except Exception as ex:                                  # noqa: BLE001
                    # a Halo 3 / Reach file in the folder: not a v8 map
                    d = {'type': None, 'tags': {}, 'error': str(ex)}
                if fn:
                    os.makedirs(self.cache_dir, exist_ok=True)
                    _save(fn, d, self.log)
                    prune_generations(self.cache_dir, 'content_%s_' % base, os.path.basename(fn))
                self.log('stock index: indexed %s (%d names%s)' % (os.path.basename(p), len(d['tags']),
                                                                    ', ' + d['error'][:60] if d.get('error') else ''))
            if d.get('error'):
                continue
            i = len(maps)
            maps.append((os.path.basename(p), d['type']))
            for nk, ds in d['tags'].items():
                lst = names.setdefault(nk, [])
                lst.extend((i, x) for x in ds)
        self.maps, self.names = maps, names
        if cfn:
            os.makedirs(self.cache_dir, exist_ok=True)
            _save(cfn, {'maps': maps, 'names': names}, self.log)
            prune_generations(self.cache_dir, 'combined_', os.path.basename(cfn))

    # ---- queries ---------------------------------------------------------------------------------------------------
    def has(self, group, name):
        return (group, (name or '').lower()) in self.names

    def match(self, group, name, digest, mp_only=False):
        """Basename of the first stock map whose copy of (group, name) has this digest (mp_only: multiplayer maps only), or None."""
        for i, d in self.names.get((group, (name or '').lower()), ()):
            if d == digest and (not mp_only or self.maps[i][1] == MP_TYPE):
                return self.maps[i][0]
        return None

    def tags(self, group, name):
        """[(XboxCache, XboxTag)] of the stock copies of (group, name); only the maps that have one are opened (kept open)."""
        from .xbox_cache import XboxCache
        out = []
        n = (name or '').lower()
        for i in sorted({i for i, _ in self.names.get((group, n), ())}):
            base = self.maps[i][0]
            c = self._caches.get(base)
            if c is None:
                try:
                    c = self._caches[base] = XboxCache(os.path.join(self.stock_dir, base), shared_dir=self.stock_dir)
                except Exception:                                        # noqa: BLE001
                    continue
                by = c._stock_by_name = {}
                for t in c.tags:
                    if t.addr and not t.is_empty and t.name:
                        by.setdefault((t.group, t.name.lower()), []).append(t)
            out.extend((c, t) for t in c._stock_by_name.get((group, n), ()))
        return out

    def close(self):
        for c in self._caches.values():
            try:
                c.close()
            except Exception:                                            # noqa: BLE001
                pass
        self._caches = {}
