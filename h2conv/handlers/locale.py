# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
r"""Localized text: `unic` tags, the header language pack / locale tables and the matg language entries.

Checked against the stock maps (stock MCC headlong -> stock Vista headlong, 24 MP map
pairs, 15 campaign map pairs + mainmenu, Vista->Vista rebuild of all 43 stock maps).

How localized text is stored (both games, verified on 43 Vista + 43 MCC stock maps)
-----------------------------------------------------------------------------------
* A `unic` tag (0x34 B) holds `unicList` @0x10 = 9 x {u16 strings_index, u16 strings_count}: one RANGE per language
  into a per-language STRING ARRAY.  The text itself is NOT in the tag (the `Import String *` block/data are empty
  in every cache tag).
* A string array is a "language pack" `c_language_pack` (28 B):
      {ptr 0, ptr 0, count, data_size, index_table_file_offset, data_file_offset, bool+pad}
  index table = count x {string_id, offset into data}, data = NUL-terminated UTF-8.  Each table and each data block is
  written 0x200-aligned/padded (vista_writer: locale_data region, between the tag-name index and the bitmap pixels).
* Two places hold packs: the header (0x2DC/0x2E0, 252 B = 9 entries) and the matg tag (+0x188, 9 entries).  The
  Vista engine (halo2.exe sub_43E063, decompiled) MERGES them per language when a map loads: matg strings first,
  then the header strings; the data offsets of the appended index entries are shifted, and the strings_index of
  every `unic` tag with tag index < 10000 (= LOCAL tag) is increased by the matg string count - but only when the
  header pack has strings for that language.  Consequences (matching all 43 stock maps):
    - multiplayer map (matg LINKED to shared.map): ALL local unic tags index the HEADER pack, ranges start at 0.
      Stock: exactly 2 local tags (`scenarios\descriptions\<map>`, `scenarios\multi\<map>\location_names`); the
      shared tags (ui\hud\hud_messages ...) index shared.map's matg arrays.
    - campaign / mainmenu / shared (matg LOCAL, header pack -1/0): no merge; the local matg entries hold the strings of
      ALL local unic tags, packed back to back in tag order, ranges are absolute.
* MCC is identical (header block @0x2E4/0x2E8, matg @0x188) except that the arrays are different (MCC added strings).

What this module does
---------------------
  pre_pass  plan_locale(ctx)      for every planned `unic` tag (action convert/donor) read its source strings,
                                  allocate new ranges in the OUTPUT arrays (out_index order), map string ids by text
  node      unic ''               writes the new unicList, clears the Import String block/data
  node      matg ''               registers a local matg; its 9 language entries are written by apply_to_builder
  post_pass finish_locale(ctx, converted)   authoritative repair of unic/matg nodes, then stores
                                  ctx.locale_blob (bytes, len % 0x200 == 0), ctx.language_pack (252 B, offsets RELATIVE
                                  to the start of the blob, or None) and ctx.locale_state
  driver    apply_to_builder(ctx, builder)  after set_strings/set_tag_names: builder.set_locale(blob, pack with ABSOLUTE
                                  offsets (locale_base + position)), and the absolute offsets are written into the
                                  local matg node(s) BEFORE they are encoded.
  library   build_locale(ctx, locale_base=None) -> (locale_blob, language_pack_252_or_None)

Modes ('header' / 'matg'): see plan_locale.  The functions `mcc_pack`, `vista_pack`, `build_blob` are plain library
code used by the tests and by anything that needs the raw strings (`python -m h2conv.handlers.locale <map>` dumps
a map's packs).
"""
import os
import struct
import sys

from . import node_handler, pre_pass, post_pass

N_LANG = 9
PAGE = 0x200
LANG_NAMES = ['english', 'japanese', 'german', 'french', 'spanish', 'italian', 'korean', 'chinese', 'portuguese']
UNIC_LIST_OFF = 0x10               # 9 x {u16 index, u16 count}
MATG_LANG_OFF = 0x188              # 9 x c_language_pack (28 B) inside the matg tag (Vista and MCC)
PACK_ENTRY = 28
PACK_SIZE = N_LANG * PACK_ENTRY    # 252
MAX_STRINGS_PER_LANGUAGE = 0x8000  # text_group.h k_max_strings_per_language (engine asserts count < this)
PT = 8                             # Portuguese: empty in every stock Vista pack

NONE32 = 0xFFFFFFFF


def _align(v, a=PAGE):
    return (v + a - 1) & ~(a - 1)


def _stat(ctx, key, n=1):
    st = getattr(ctx, 'stats', None)
    if st is not None:
        st[key] += n


def _warn(ctx, code, msg, tag=None):
    w = getattr(ctx, 'warn', None)
    if w is not None:
        w(code, msg, tag)


# ====================================================================================================
# reading packs (library code, no ctx)
# ====================================================================================================
class LangArrays(object):
    """The 9 string arrays of one pack.  langs[L] = [(key_text, utf8_bytes)]  (key_text = the string_id's text)."""
    __slots__ = ('langs', 'origin')

    def __init__(self, langs, origin):
        self.langs = langs
        self.origin = origin

    def counts(self):
        return [len(x) for x in self.langs]


_ARRAYS = {}       # (path, kind) -> LangArrays   (source maps are read-only)


def _mcc_sid_text(cache, sid):
    if sid in (0, NONE32):
        return ''
    ids = cache.string_ids
    i = sid & 0xFFFFFF
    if i >= len(ids) or ids[i] is None:
        return ''
    return ids[i]


def _mcc_entry_strings(cache, e):
    cnt, tsz = e['string_count'], e['table_size']
    if not cnt:
        return []
    ito = e['index_table_offset'] & 0x7FFFFFFF       # bit 31 = "shared.map's own data" flag in shared.map's matg
    dto = e['data_offset'] & 0x7FFFFFFF
    idx = struct.unpack('<%dI' % (2 * cnt), cache.raw(ito, 8 * cnt))
    blob = cache.raw(dto, tsz)
    out = []
    for k in range(cnt):
        sid, off = idx[2 * k], idx[2 * k + 1]
        end = blob.find(b'\0', off)
        if end < 0:
            end = len(blob)
        out.append((_mcc_sid_text(cache, sid), blob[off:end]))
    return out


def mcc_pack_kind(cache):
    """Which pack the OWN tags of an MCC cache index: the header pack when the map has one (MP maps that use
    shared.map), else the matg arrays."""
    return 'header' if cache.locale_globals() else 'matg'


def mcc_pack(cache, kind=None):
    """LangArrays of an MCC cache: kind 'header' (header block @0x2E4) or 'matg' (matg tag @+0x188)."""
    kind = kind or mcc_pack_kind(cache)
    key = (os.path.abspath(cache.path), 'mcc', kind)
    got = _ARRAYS.get(key)
    if got is not None:
        return got
    ents = cache.locale_globals() if kind == 'header' else cache.matg_languages()
    langs = [_mcc_entry_strings(cache, e) for e in ents]
    langs += [[] for _ in range(N_LANG - len(langs))]
    got = LangArrays(langs, '%s:%s' % (os.path.basename(cache.path), kind))
    _ARRAYS[key] = got
    return got


def vista_pack_kind(vcache, tag=None):
    """Header pack when the Vista map has one and the tag is local to it, else the matg arrays."""
    if vcache.header.language_pack_offset != NONE32 and (tag is None or tag.addr):
        return 'header'
    return 'matg'


def _vista_entries(vcache, kind):
    h = vcache.header
    if kind == 'header':
        if h.language_pack_offset == NONE32:
            return []
        blob = vcache.raw(h.language_pack_offset, h.language_pack_size)
        base = 0
    else:
        g = [t for t in vcache.tags if t.group == 'matg' and t.addr and t.index < 10000]
        if not g:
            g = [t for t in vcache.tags if t.group == 'matg' and t.addr]
        if not g:
            return []
        blob = vcache.read_tag(g[0])
        base = MATG_LANG_OFF
    ents = []
    for i in range(N_LANG):
        o = base + PACK_ENTRY * i
        if o + PACK_ENTRY > len(blob):
            break
        cnt, size, io_, do_ = struct.unpack_from('<IIII', blob, o + 8)
        ents.append((cnt, size, io_, do_))
    return ents


def vista_pack(vcache, kind=None, tag=None):
    """LangArrays of a Vista cache (header pack or the local matg's arrays).  Pointer top bits: 0 = this map; in
    shared.map the arrays are addressed with bit 31 (its own database) - both are masked to 30 bits."""
    kind = kind or vista_pack_kind(vcache, tag)
    key = (os.path.abspath(vcache.path), 'vista', kind)
    got = _ARRAYS.get(key)
    if got is not None:
        return got
    langs = []
    for cnt, size, io_, do_ in _vista_entries(vcache, kind):
        if not cnt:
            langs.append([])
            continue
        io_ &= 0x3FFFFFFF
        do_ &= 0x3FFFFFFF
        idx = struct.unpack('<%dI' % (2 * cnt), vcache.raw(io_, 8 * cnt))
        blob = vcache.raw(do_, size)
        lst = []
        for k in range(cnt):
            sid, off = idx[2 * k], idx[2 * k + 1]
            end = blob.find(b'\0', off)
            if end < 0:
                end = len(blob)
            lst.append((vcache.string_id(sid) if sid not in (0, NONE32) else '', blob[off:end]))
        langs.append(lst)
    langs += [[] for _ in range(N_LANG - len(langs))]
    got = LangArrays(langs, '%s:%s' % (os.path.basename(vcache.path), kind))
    _ARRAYS[key] = got
    return got


def unic_ranges(data):
    """unicList of raw unic tag bytes -> [(index, count)] x 9."""
    return [struct.unpack_from('<HH', data, UNIC_LIST_OFF + 4 * i) for i in range(N_LANG)]


def unic_strings(cache, tag):
    """The effective strings of one MCC unic tag: 9 lists of (key_text, utf8_bytes), resolved through the pack that
    the tag's cache uses for its own tags (header pack of an MP map, else the matg arrays)."""
    arrays = mcc_pack(cache)
    out = []
    for L, (idx, cnt) in enumerate(unic_ranges(cache.read_tag(tag))):
        out.append(list(arrays.langs[L][idx:idx + cnt]) if cnt else [])
    return out


def unic_same_content(cache_a, tag_a, cache_b, tag_b, ignore_pt=True):
    """True when two MCC unic tags (possibly in different maps) carry the same strings in every language.
    The raw tag bytes cannot be compared (a self-contained map's ranges are offset by its own tags' strings:
    custom headlong's ranges start at 8 where shared.map's start at 0).  For the planner's link-vs-convert test."""
    a, b = unic_strings(cache_a, tag_a), unic_strings(cache_b, tag_b)
    n = N_LANG - 1 if ignore_pt else N_LANG
    return all(a[L] == b[L] for L in range(n))


# ====================================================================================================
# building the locale blob + language pack (library code)
# ====================================================================================================
def build_blob(out_langs, locale_base=0):
    """out_langs[L] = [(vista_string_id:int, utf8_bytes)] x 9  ->  (blob, entries, pack)

    blob     per language: index table padded to 0x200, then data padded to 0x200 (languages with no strings
             write nothing); len(blob) % 0x200 == 0
    entries  [(count, data_size, index_off, data_off)] with ABSOLUTE offsets = locale_base + position
    pack     252-byte c_language_pack[9]  {0, 0, count, data_size, index_off, data_off, 0}  (the last dword is the
             runtime `data_loaded` bool + 3 stale pad bytes in stock maps; written as 0)
    Empty languages get index_off = data_off = current end of the blob (stock: headlong language 8, highplains 1..8)."""
    blob = bytearray()
    entries = []
    for L in range(N_LANG):
        lst = out_langs[L] if L < len(out_langs) else []
        cnt = len(lst)
        if cnt == 0:
            entries.append((0, 0, locale_base + len(blob), locale_base + len(blob)))
            continue
        idx = bytearray()
        data = bytearray()
        for sid, b in lst:
            idx += struct.pack('<II', sid & 0xFFFFFFFF, len(data))
            data += bytes(b) + b'\0'
        io_ = locale_base + len(blob)
        blob += idx
        blob += b'\0' * (_align(len(blob)) - len(blob))
        do_ = locale_base + len(blob)
        blob += data
        blob += b'\0' * (_align(len(blob)) - len(blob))
        entries.append((cnt, len(data), io_, do_))
    pack = b''.join(struct.pack('<7I', 0, 0, c, s, i, d, 0) for c, s, i, d in entries)
    return bytes(blob), entries, pack


def arrays_to_vista(ctx, langs):
    """[(key_text, bytes)] x 9  ->  [(vista_sid, bytes)] x 9 (ids mapped BY TEXT through ctx.sid)."""
    memo = {}
    out = []
    for lst in langs:
        o = []
        for key, b in lst:
            v = memo.get(key)
            if v is None:
                v = ctx.sid(key) if key else 0
                memo[key] = v
            o.append((v, b))
        out.append(o)
    return out


# ====================================================================================================
# state
# ====================================================================================================
class Item(object):
    """One planned unic tag: where its strings come from and where they went."""
    __slots__ = ('out_index', 'name', 'src_addr', 'src_ranges', 'new_ranges', 'origin')

    def __init__(self, out_index, name, src_addr, src_ranges, origin):
        self.out_index = out_index
        self.name = name
        self.src_addr = src_addr
        self.src_ranges = src_ranges
        self.new_ranges = None
        self.origin = origin


class LocaleState(object):
    def __init__(self, ctx):
        opts = getattr(ctx, 'opts', None) or {}
        self.keep_pt = bool(opts.get('locale_keep_pt'))
        self.out = [[] for _ in range(N_LANG)]       # [(key_text, bytes)] per language (source ids, mapped at build)
        self.items = {}                              # out_index -> Item
        self.by_addr = {}                            # src_addr -> [Item]
        self.matg_nodes = []                         # Vista matg Nodes that need absolute language entries
        self.local_matg = False
        self.mode = 'header'                         # 'header' | 'matg'  (see plan_locale)
        self.locale_base = None
        self.planned = False
        self._ver = 0                                # bumped by add(); invalidates the id-mapped copy
        self._vlangs = None
        self._vkey = None

    # -- allocation ------------------------------------------------------------------------------
    def add(self, ctx, out_index, name, arrays, ranges, src_addr=None):
        it = Item(out_index, name, src_addr, list(ranges), arrays.origin)
        new = []
        for L in range(N_LANG):
            idx, cnt = ranges[L]
            if L == PT and not self.keep_pt:
                if cnt:
                    _stat(ctx, 'locale_pt_strings_dropped', cnt)
                new.append((0, 0))
                continue
            src = arrays.langs[L]
            if cnt and idx + cnt > len(src):
                _warn(ctx, 'locale_range', '%s: %s range (%d,%d) outside the %d-string array %s' % (
                    name, LANG_NAMES[L], idx, cnt, len(src), arrays.origin))
                cnt = max(0, min(cnt, len(src) - idx))
            if cnt <= 0:
                new.append((0, 0))
                continue
            start = len(self.out[L])
            self.out[L].extend(src[idx:idx + cnt])
            if start + cnt > 0xFFFF:
                _warn(ctx, 'locale_overflow', '%s: %s strings index %d exceeds 16 bits' % (name, LANG_NAMES[L], start + cnt))
            new.append((start, cnt))
        it.new_ranges = new
        self.items[out_index] = it
        self.by_addr.setdefault(src_addr, []).append(it)
        self._ver += 1
        return it

    def counts(self):
        return [len(x) for x in self.out]

    def override_text(self, key, text):
        """Replace the text of every string whose id text is `key` (all languages); returns the number replaced.  Used by the driver's
        --display-name: the localized map name of an MP map is the string 'name' of the map's own unic tag."""
        b = text.encode('utf-8')
        n = 0
        for L in range(N_LANG):
            lst = self.out[L]
            for i in range(len(lst)):
                if lst[i][0] == key:
                    lst[i] = (key, b)
                    n += 1
        if n:
            self._ver += 1
        return n

    @property
    def header_pack(self):
        return self.mode == 'header'

    # -- output ----------------------------------------------------------------------------------
    def build(self, ctx, locale_base=0):
        """(blob, entries, pack) with absolute offsets = locale_base + position.  The (expensive) id mapping is
        done once; later calls only re-base."""
        vkey = (self._ver, id(ctx))
        if self._vlangs is None or self._vkey != vkey:
            self._vlangs = arrays_to_vista(ctx, self.out)
            self._vkey = vkey
            for L, n in enumerate(self.counts()):
                if n >= MAX_STRINGS_PER_LANGUAGE:
                    _warn(ctx, 'locale_too_many', '%s: %d strings >= engine limit 0x8000' % (LANG_NAMES[L], n))
        return build_blob(self._vlangs, locale_base)

    def summary(self):
        return {'counts': self.counts(), 'items': len(self.items), 'local_matg': self.local_matg,
                'mode': self.mode, 'header_pack': self.header_pack, 'locale_base': self.locale_base}


def state(ctx, create=True):
    st = getattr(ctx, 'locale_state', None)
    if st is None and create:
        st = LocaleState(ctx)
        ctx.locale_state = st
    return st


# ====================================================================================================
# planning (pre-pass)
# ====================================================================================================
def _mcc_cache_for(ctx, src_map):
    if src_map in (None, 'src'):
        return ctx.src
    return ctx.mcc_shared.get(src_map)


def _source_of_entry(ctx, e):
    """-> (arrays, ranges, src_addr, origin) for one plan entry, or None."""
    if e.action == 'donor' and e.donor:
        path, tindex = e.donor[0], e.donor[1]
        vc = ctx.vista.open(os.path.basename(path)) if hasattr(ctx, 'vista') else None
        if vc is None:
            return None
        tag = vc.tags[tindex]
        if not tag.addr:
            return None
        arrays = vista_pack(vc, tag=tag)
        return arrays, unic_ranges(vc.read_tag(tag)), tag.addr, arrays.origin
    cache = _mcc_cache_for(ctx, e.src_map)
    if cache is None:
        return None
    tag = cache.tags[e.src_index]
    if tag.external:
        sh = ctx.mcc_shared.get('shared')
        if sh is None:
            return None
        tag = cache.resolve_external(tag, sh)
        cache = sh
    if not tag.has_data:
        return None
    arrays = mcc_pack(cache)
    return arrays, unic_ranges(cache.read_tag(tag)), tag.addr, arrays.origin


def plan_locale(ctx, entries=None, force_matg=None, mode=None):
    """Build ctx.locale_state from the plan.  entries = plan entries (default ctx.plan.entries); only group 'unic'
    with action convert/donor matter (linked tags keep indexing shared.map's arrays).  Idempotent.

    Mode (engine: halo2.exe sub_43E063 = c_language_pack load):
      'header'  the 252-byte header pack holds ALL local unic strings, local ranges start at 0.  The engine appends
                the header strings to the matg pack and adds the matg string count to the range start of every
                unic tag with index < 10000.  Stock for every multiplayer map (matg linked to shared.map).
      'matg'    no header pack (-1/0): the local matg's 9 entries hold the strings, ranges index them directly.
                Stock for campaign / mainmenu / shared / single_player_shared (matg local).
    Policy: matg not emitted locally -> 'header'; local matg -> 'matg' unless the map is multiplayer (type 1), which
    needs the header pack (map name/description in the custom-map list) and keeps shared.map's strings in matg.
    ctx.opts['locale_mode'] ('header'|'matg') overrides."""
    st = LocaleState(ctx)
    ctx.locale_state = st
    st.planned = True
    decisions = None
    if entries is None:
        plan = getattr(ctx, 'plan', None)
        entries = list(getattr(plan, 'entries', None) or [])
        decisions = list(getattr(plan, 'all_entries', None) or [])      # incl. 'link' / 'drop'
    entries = list(entries)
    st.local_matg = any(e.group == 'matg' and e.action in ('convert', 'donor') for e in entries)
    if force_matg is not None:
        st.local_matg = bool(force_matg)
    mt = getattr(ctx, 'map_type', 1)
    st.mode = 'header' if (mt == 1 or not st.local_matg) else 'matg'
    want = mode or (getattr(ctx, 'opts', None) or {}).get('locale_mode')
    if want in ('header', 'matg'):
        st.mode = want
    if st.mode == 'matg':
        # the engine's pack object is the LOCAL matg's: a linked (shared database) unic tag would index the wrong array
        for e in (decisions if decisions is not None else entries):
            if e.group == 'unic' and e.action == 'link':
                _warn(ctx, 'locale_linked_unic', 'unic %s is linked to a shared database but this map keeps its strings in '
                      'its own matg (all unic tags must be local)' % e.name, e.name)
    for e in sorted((x for x in entries if x.group == 'unic' and x.action in ('convert', 'donor')),
                    key=lambda x: x.out_index):
        src = _source_of_entry(ctx, e)
        if src is None:
            _warn(ctx, 'locale_no_source', 'unic %s (out %d): no readable source pack' % (e.name, e.out_index), e.name)
            continue
        arrays, ranges, addr, _origin = src
        st.add(ctx, e.out_index, e.name, arrays, ranges, addr)
        _stat(ctx, 'locale_unic_tags')
    return st


# ====================================================================================================
# node-level fixes
# ====================================================================================================
def set_unic_node(vista_node, ranges):
    """Write unicList and clear the (cache-empty) Import String block/data of a Vista unic Node."""
    for L in range(N_LANG):
        idx, cnt = ranges[L]
        struct.pack_into('<HH', vista_node.data, UNIC_LIST_OFF + 4 * L, min(idx, 0xFFFF), min(cnt, 0xFFFF))
    for f in vista_node.fields:
        if f.name == 'Import String References':
            vista_node.blocks[f.offset] = []
            struct.pack_into('<iI', vista_node.data, f.offset, 0, 0)
        elif f.name == 'Import String Data':
            vista_node.datas[f.offset] = b''
            struct.pack_into('<iI', vista_node.data, f.offset, 0, 0)


def patch_matg_node(vista_node, entries):
    """Write the 9 language entries {0,0,count,size,index_off,data_off,0} at matg+0x188."""
    for L, (c, s, i, d) in enumerate(entries):
        struct.pack_into('<7I', vista_node.data, MATG_LANG_OFF + PACK_ENTRY * L, 0, 0, c, s, i, d, 0)


def shared_matg_entries(ctx):
    """The 9 (count, size, index_off, data_off) of Vista shared.map's matg language entries, pointer values verbatim
    (bit 31 = "shared.map" database).  None when shared.map is not reachable."""
    cached = getattr(ctx, '_locale_shared_matg', None)
    if cached is not None:
        return cached or None
    ents = False
    try:
        sh = ctx.vista.shared
        g = next((t for t in sh.tags if t.group == 'matg' and t.addr), None)
        if g is not None:
            d = sh.read_tag(g)
            ents = [struct.unpack_from('<IIII', d, MATG_LANG_OFF + PACK_ENTRY * i + 8) for i in range(N_LANG)]
    except Exception:       # noqa: BLE001  (no Vista maps available: tests without shared.map)
        ents = False
    ctx._locale_shared_matg = ents
    return ents or None


def matg_entries(ctx, st, blob_entries):
    """What a LOCAL matg node gets in its 9 language entries.
    'matg' mode: the entries of our own blob.  'header' mode: shared.map's entries (the engine merges those strings
    with the header pack); zero entries (+warning) when shared.map cannot be read."""
    if st.mode == 'matg':
        return blob_entries
    ents = shared_matg_entries(ctx)
    if ents is None:
        _warn(ctx, 'locale_matg_shared', 'local matg in header mode but Vista shared.map matg language entries not readable')
        return [(0, 0, 0, 0)] * N_LANG
    return ents


def _item_for_node(st, mcc_node):
    cands = st.by_addr.get(mcc_node.src_addr, [])
    if len(cands) == 1:
        return cands[0]
    if cands:
        rng = unic_ranges(mcc_node.data)
        for c in cands:
            if c.src_ranges == rng:
                return c
    return None


def ensure_planned(ctx):
    """The state, planning from ctx.plan first if the pre-pass did not run (hand-driven tests)."""
    st = state(ctx)
    if not st.planned:
        st = plan_locale(ctx)
    return st


@pre_pass(prio=50)
def locale_pre(ctx):
    plan_locale(ctx)


@node_handler('unic', '')
def locale_unic_node(ctx, mcc_node, vista_node):
    st = ensure_planned(ctx)
    it = None
    ce = getattr(ctx, 'cur_entry', None)          # transcode.convert_tag sets the plan entry being converted
    if ce is not None and getattr(ce, 'group', None) == 'unic':
        it = st.items.get(ce.out_index)
    if it is None:
        it = _item_for_node(st, mcc_node)
    if it is None:
        _stat(ctx, 'locale_unic_node_unassigned')
        return
    set_unic_node(vista_node, it.new_ranges)


@node_handler('matg', '')
def locale_matg_node(ctx, mcc_node, vista_node):
    st = ensure_planned(ctx)
    st.local_matg = True
    if not any(n is vista_node for n in st.matg_nodes):
        st.matg_nodes.append(vista_node)
    # counts/sizes are final now; the absolute offsets are written by apply_to_builder
    _blob, entries, _pack = st.build(ctx, 0)
    patch_matg_node(vista_node, matg_entries(ctx, st, entries))


@post_pass(prio=50)
def finish_locale(ctx, converted):
    st = ensure_planned(ctx)
    missing = 0
    for out_index, node in converted.items():
        if node is None:                       # the tag failed to convert: nothing to patch (convert_failed already named it)
            missing += 1
            if out_index in st.items:
                _warn(ctx, 'locale_node_missing', 'unic out %d has no converted node (the tag failed to convert): its language ranges '
                      'are not written' % out_index)
            continue
        if node.group == 'unic' and out_index in st.items:
            set_unic_node(node, st.items[out_index].new_ranges)
        elif node.group == 'matg':
            st.local_matg = True
            if not any(n is node for n in st.matg_nodes):
                st.matg_nodes.append(node)
    if missing:
        _stat(ctx, 'locale_nodes_missing', missing)
    dn = (getattr(ctx, 'opts', None) or {}).get('display_name')
    if dn:
        n = st.override_text('name', dn)
        _stat(ctx, 'locale_display_name_strings', n)
        if not n:
            _warn(ctx, 'locale_display_name', "no string 'name' in the map's language pack: --display-name %r has no effect" % dn)
    blob, entries, pack = st.build(ctx, 0)
    ctx.locale_blob = blob
    ctx.language_pack = pack if st.header_pack else None       # offsets relative to the blob start
    for n in st.matg_nodes:
        patch_matg_node(n, matg_entries(ctx, st, entries))
    c = st.counts()
    _stat(ctx, 'locale_strings_english', c[0])
    _stat(ctx, 'locale_blob_bytes', len(blob))


# ====================================================================================================
# driver API
# ====================================================================================================
def build_locale(ctx, locale_base=None):
    """-> (locale_blob, language_pack_252_or_None) exactly as VistaBuilder.set_locale expects them.
    locale_base = absolute file offset of the blob (VistaBuilder.locale_base, valid after set_strings +
    set_tag_names); the pack's index/data offsets are locale_base + position.  None = relative (base 0)."""
    st = state(ctx)
    base = 0 if locale_base is None else locale_base
    blob, _entries, pack = st.build(ctx, base)
    return blob, (pack if st.header_pack else None)


def apply_to_builder(ctx, builder):
    """Call after builder.set_strings() and builder.set_tag_names() and before the tags are encoded:
    sets the locale blob + language pack and writes the absolute offsets into the local matg node(s)."""
    st = state(ctx)
    base = builder.locale_base
    st.locale_base = base
    blob, entries, pack = st.build(ctx, base)
    builder.set_locale(blob, pack if st.header_pack else None)
    for n in st.matg_nodes:
        patch_matg_node(n, matg_entries(ctx, st, entries))
    return base


# ====================================================================================================
# CLI: dump the packs of a map
# ====================================================================================================
def _dump(path):
    from h2conv.mcc_cache import MccCache
    from h2conv.vista_cache import VistaCache
    with open(path, 'rb') as fh:
        v8 = fh.read(8)[4:8] == b'\x08\0\0\0'
    if v8:
        c = VistaCache(path)
        for kind in ('header', 'matg'):
            a = vista_pack(c, kind)
            print('vista', kind, a.counts())
    else:
        c = MccCache(path)
        for kind in ('header', 'matg'):
            a = mcc_pack(c, kind)
            print('mcc', kind, a.counts())


if __name__ == '__main__':
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    for p in sys.argv[1:]:
        _dump(p)
