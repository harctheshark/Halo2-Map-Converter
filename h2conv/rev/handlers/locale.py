# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
r"""Localized text, REVERSE direction: `unic` tags, the language packs and the matg language entries - Vista (v8) -> MCC (v13).

Inverse of handlers/locale.py.

Storage is identical in both games (verified by the forward work, re-checked here on the MCC side): a `unic` tag (0x34 B)
only carries 9 x {u16 strings_index, u16 strings_count} ranges; the text lives in per-language string ARRAYS
(c_language_pack, 28 B: {0, 0, count, data_size, index_table_offset, data_offset, runtime bool}; index = {string_id, offset}
pairs, data = NUL-terminated UTF-8).  Two places can hold the arrays: the header block (MCC hdr 0x2E4, 252 B) and the matg tag
(+0x188, 9 x 28 B).  What differs is the *policy* of the target:

  * MCC (and the H2EK-built self-contained maps) keep ALL strings of a self-contained map in their OWN matg: header
    locale -1/0, `unic` ranges are absolute indices into the matg arrays, packed back to back in tag order (custom headlong:
    its own description/location_names first `(0,2) (2,6)`, ui\hud\hud_messages `(8,374)` ...), index tables and data blocks
    0x80-aligned, absolute FILE offsets without the bit-31 "shared.map" flag, last pack dword 0xCDCDCD00 (r17_locale_dump.py).
    => mode 'matg' (default whenever the matg is emitted locally).
  * A map whose matg stays LINKED to MCC's shared.map keeps its own strings in the header pack (stock MCC multiplayer maps)
    and every local `unic` tag indexes that pack from 0 => mode 'header'.
  * Vista has no Portuguese and some community maps only English: a language array that is EMPTY for a tag is back-filled with
    the English strings (opts['rev_locale_backfill'] = False disables it) so no player gets the engine's "string not found" text.

What this module does (same shape as the forward module)
  pre_pass  plan_locale(ctx)      read the strings of every planned `unic` tag (action convert/donor), allocate ranges in the
                                  OUTPUT arrays (out_index order), map string ids BY TEXT (ctx.sid)
  node      unic ''               writes the new unicList, clears the Import String block/data
  node      matg ''               registers a local matg; its 9 language entries are written by build/apply
  post_pass finish_locale         authoritative repair of unic/matg nodes, then ctx.locale_blob (bytes, 0x80-aligned tables),
                                  ctx.language_pack (252 B, offsets RELATIVE to the blob; None in 'matg' mode), ctx.locale_state
  driver    apply_to_builder(ctx, builder)  builder.locale_base / builder.set_locale(blob, pack)
  library   build_locale(ctx, locale_base) -> (blob, pack); build_blob_mcc(); patch_matg_node_mcc()
"""
import struct

from . import node_handler, pre_pass, post_pass
from . import _revmisc_common as C
from ...handlers import locale as FL

N_LANG = FL.N_LANG
ALIGN_MCC = 0x80
MATG_LANG_OFF = FL.MATG_LANG_OFF
PACK_ENTRY = FL.PACK_ENTRY
PACK_LAST_DWORD = 0xCDCDCD00           # runtime bool (0) + 3 bytes of MSVC debug fill, as the MCC tools write it
MAX_STRINGS_PER_LANGUAGE = FL.MAX_STRINGS_PER_LANGUAGE
NONE32 = 0xFFFFFFFF


def _align(v, a=ALIGN_MCC):
    return (v + a - 1) & ~(a - 1)


# ====================================================================================================
# building (library)
# ====================================================================================================
def build_blob_mcc(out_langs, locale_base=0, align=ALIGN_MCC):
    """out_langs[L] = [(mcc_string_id:int, utf8_bytes)] x 9  ->  (blob, entries, pack)

    per language: index table padded to `align`, then the data padded to `align` (languages with no strings write nothing);
    entries [(count, data_size, index_off, data_off)] with ABSOLUTE offsets = locale_base + position;
    pack = 252 bytes {0, 0, count, size, index_off, data_off, 0xCDCDCD00} x 9.  Empty languages point at the current end."""
    blob = bytearray()
    entries = []
    for L in range(N_LANG):
        lst = out_langs[L] if L < len(out_langs) else []
        if not lst:
            entries.append((0, 0, locale_base + len(blob), locale_base + len(blob)))
            continue
        idx = bytearray()
        data = bytearray()
        for sid, b in lst:
            idx += struct.pack('<II', sid & 0xFFFFFFFF, len(data))
            data += bytes(b) + b'\0'
        io_ = locale_base + len(blob)
        blob += idx
        blob += b'\0' * (_align(len(blob), align) - len(blob))
        do_ = locale_base + len(blob)
        blob += data
        blob += b'\0' * (_align(len(blob), align) - len(blob))
        entries.append((len(lst), len(data), io_, do_))
    pack = b''.join(struct.pack('<7I', 0, 0, c, s, i, d, PACK_LAST_DWORD) for c, s, i, d in entries)
    return bytes(blob), entries, pack


def patch_matg_node_mcc(mcc_node, entries):
    """Write the 9 language entries {0,0,count,size,index_off,data_off,0xCDCDCD00} at matg+0x188."""
    for L, (c, s, i, d) in enumerate(entries):
        struct.pack_into('<7I', mcc_node.data, MATG_LANG_OFF + PACK_ENTRY * L, 0, 0, c, s, i, d, PACK_LAST_DWORD)


def set_unic_node(mcc_node, ranges):
    FL.set_unic_node(mcc_node, ranges)


# ====================================================================================================
# state
# ====================================================================================================
class RevLocaleState(FL.LocaleState):
    """FL.LocaleState with the MCC policy: Portuguese kept, English back-fill, 0x80-aligned tables."""

    def __init__(self, ctx):
        FL.LocaleState.__init__(self, ctx)
        self.keep_pt = True
        self.backfill = bool(C.opt(ctx, 'rev_locale_backfill', True))
        self.backfilled = 0

    def add(self, ctx, out_index, name, arrays, ranges, src_addr=None):
        it = FL.Item(out_index, name, src_addr, list(ranges), arrays.origin)
        new = []
        eidx, ecnt = ranges[0]
        for L in range(N_LANG):
            idx, cnt = ranges[L]
            src = arrays.langs[L]
            use = None
            if cnt and idx + cnt > len(src):
                C.warn(ctx, 'rev_locale_range', '%s: %s range (%d,%d) outside the %d-string array %s' % (
                    name, FL.LANG_NAMES[L], idx, cnt, len(src), arrays.origin))
                cnt = max(0, min(cnt, len(src) - idx))
            if cnt > 0:
                use = src[idx:idx + cnt]
            elif self.backfill and L > 0 and ecnt:
                use = arrays.langs[0][eidx:eidx + ecnt]               # English back-fill for an empty language
                if use:
                    self.backfilled += len(use)
            if not use:
                new.append((0, 0))
                continue
            start = len(self.out[L])
            self.out[L].extend(use)
            if start + len(use) > 0xFFFF:
                C.warn(ctx, 'rev_locale_overflow', '%s: %s strings index %d exceeds 16 bits' % (name, FL.LANG_NAMES[L], start + len(use)))
            new.append((start, len(use)))
        it.new_ranges = new
        self.items[out_index] = it
        self.by_addr.setdefault(src_addr, []).append(it)
        self._ver += 1
        return it

    def build(self, ctx, locale_base=0):
        vkey = (self._ver, id(ctx))
        if self._vlangs is None or self._vkey != vkey:
            self._vlangs = FL.arrays_to_vista(ctx, self.out)          # (key text) -> ctx.sid(text): the target's string ids
            self._vkey = vkey
            for L, n in enumerate(self.counts()):
                if n >= MAX_STRINGS_PER_LANGUAGE:
                    C.warn(ctx, 'rev_locale_too_many', '%s: %d strings >= engine limit 0x8000' % (FL.LANG_NAMES[L], n))
        return build_blob_mcc(self._vlangs, locale_base)


def state(ctx, create=True):
    st = getattr(ctx, 'locale_state', None)
    if (st is None or not isinstance(st, RevLocaleState)) and create:
        st = RevLocaleState(ctx)
        ctx.locale_state = st
    return st


# ====================================================================================================
# planning (pre-pass)
# ====================================================================================================
def _vista_cache_for(ctx, src_map):
    fn = getattr(ctx, 'cache_for', None)
    if fn is not None:
        try:
            return fn(src_map)
        except Exception:  # noqa: BLE001
            pass
    if src_map in (None, 'src'):
        return ctx.src
    v = getattr(ctx, 'vista', None)
    if v is not None:
        try:
            return v.open(src_map + '.map') if not str(src_map).endswith('.map') else v.open(src_map)
        except Exception:  # noqa: BLE001
            return None
    return None


def _source_of_entry(ctx, e):
    """-> (LangArrays, [(idx, cnt)] x 9, src_addr, origin) for one plan entry, or None."""
    hook = getattr(ctx, 'locale_source', None)
    if hook is not None:
        r = hook(e)
        if r is not None:
            return r
    if getattr(e, 'action', None) in ('mcc_donor', 'donor') and getattr(e, 'donor', None) is not None:
        # a stock MCC tag copied as it is (planner action 'mcc_donor'): its strings live in the arrays of ITS map
        plan = getattr(ctx, 'plan', None)
        fn = getattr(plan, 'donor_source_of', None)
        got = fn(e) if fn is not None else None
        if got is None:
            return None
        cache, tag = got
        if tag.external:
            sh = ctx.mcc_shared.get('shared') if getattr(ctx, 'mcc_shared', None) is not None else None
            if sh is None:
                return None
            tag = cache.resolve_external(tag, sh)
            cache = sh
        if not tag.has_data:
            return None
        arrays = FL.mcc_pack(cache)
        return arrays, FL.unic_ranges(cache.read_tag(tag)), tag.addr, arrays.origin
    cache = _vista_cache_for(ctx, getattr(e, 'src_map', 'src'))
    if cache is None:
        return None
    tag = cache.tags[e.src_index]
    if not tag.addr:
        return None
    arrays = FL.vista_pack(cache, tag=tag)
    return arrays, FL.unic_ranges(cache.read_tag(tag)), tag.addr, arrays.origin


def plan_locale(ctx, entries=None, force_matg=None, mode=None):
    """Build ctx.locale_state from the plan (only group 'unic', action convert/donor, matter).  Idempotent.
    Mode: 'matg' when the matg is emitted locally (default, self-contained MCC map), else 'header'; ctx.opts['rev_locale_mode']."""
    st = RevLocaleState(ctx)
    ctx.locale_state = st
    st.planned = True
    decisions = None
    if entries is None:
        plan = getattr(ctx, 'plan', None)
        entries = list(getattr(plan, 'entries', None) or [])
        decisions = list(getattr(plan, 'all_entries', None) or [])
    entries = list(entries)
    st.local_matg = any(e.group == 'matg' and e.action in ('convert', 'donor', 'mcc_donor') for e in entries)
    if force_matg is not None:
        st.local_matg = bool(force_matg)
    st.mode = 'matg' if st.local_matg else 'header'
    want = mode or C.opt(ctx, 'rev_locale_mode')
    if want in ('header', 'matg'):
        st.mode = want
    if st.mode == 'matg':
        for e in (decisions if decisions is not None else entries):
            if e.group == 'unic' and e.action == 'link':
                C.warn(ctx, 'rev_locale_linked_unic', 'unic %s is linked to a shared database but this map keeps its strings in its '
                       'own matg (all unic tags must be local)' % e.name, e.name)
    for e in sorted((x for x in entries if x.group == 'unic' and x.action in ('convert', 'donor', 'mcc_donor')), key=lambda x: x.out_index):
        src = _source_of_entry(ctx, e)
        if src is None:
            C.warn(ctx, 'rev_locale_no_source', 'unic %s (out %d): no readable source pack' % (e.name, e.out_index), e.name)
            continue
        arrays, ranges, addr, _origin = src
        st.add(ctx, e.out_index, e.name, arrays, ranges, addr)
        C.stat(ctx, 'rev_locale_unic_tags')
    return st


def ensure_planned(ctx):
    st = state(ctx)
    if not st.planned:
        st = plan_locale(ctx)
    return st


@pre_pass(prio=50)
def locale_pre(ctx):
    plan_locale(ctx)


# ====================================================================================================
# node handlers
# ====================================================================================================
def _item_for_node(st, vista_node):
    cands = st.by_addr.get(vista_node.src_addr, [])
    if len(cands) == 1:
        return cands[0]
    if cands:
        rng = FL.unic_ranges(vista_node.data)
        for c in cands:
            if c.src_ranges == rng:
                return c
    return None


@node_handler('unic', '')
def locale_unic_node(ctx, vista_node, mcc_node):
    st = ensure_planned(ctx)
    it = None
    ce = getattr(ctx, 'cur_entry', None)
    if ce is not None and getattr(ce, 'group', None) == 'unic' and hasattr(ce, 'out_index'):
        it = st.items.get(ce.out_index)
    if it is None:
        it = _item_for_node(st, vista_node)
    if it is None:
        C.stat(ctx, 'rev_locale_unic_node_unassigned')
        return
    set_unic_node(mcc_node, it.new_ranges)


def matg_entries(ctx, st, blob_entries):
    """What a LOCAL matg node gets in its 9 language entries: our own blob in 'matg' mode; in 'header' mode the entries of the
    MCC shared matg verbatim (the engine merges the header pack with them)."""
    if st.mode == 'matg':
        return blob_entries
    sh = getattr(ctx, 'mcc_shared', None)
    try:
        c = sh.get('shared') if sh is not None else None
        ents = c.matg_languages() if c is not None else []
    except Exception:  # noqa: BLE001
        ents = []
    if not ents:
        C.warn(ctx, 'rev_locale_matg_shared', 'local matg in header mode but the MCC shared.map matg language entries are not readable')
        return [(0, 0, 0, 0)] * N_LANG
    return [(e['string_count'], e['table_size'], e['index_table_offset'], e['data_offset']) for e in ents]


@node_handler('matg', '')
def locale_matg_node(ctx, vista_node, mcc_node):
    st = ensure_planned(ctx)
    st.local_matg = True
    if not any(n is mcc_node for n in st.matg_nodes):
        st.matg_nodes.append(mcc_node)
    _blob, entries, _pack = st.build(ctx, 0)
    patch_matg_node_mcc(mcc_node, matg_entries(ctx, st, entries))


def _report_missing_nodes(ctx, st, missing):
    """converted[out_index] is None for a tag that failed to convert (and could not even be replaced by an empty struct).  Nothing to
    patch there: say which locale-relevant tags are affected (unic / matg), count the rest (convert_failed already named them)."""
    by_idx = {}
    for e in (getattr(getattr(ctx, 'plan', None), 'entries', None) or []):
        by_idx[getattr(e, 'out_index', None)] = e
    for i in missing:
        e = by_idx.get(i)
        g = getattr(e, 'group', None)
        if g in ('unic', 'matg') or i in st.items:
            what = ('its language ranges are not written' if g == 'unic' else 'the map has no local globals to hold the language arrays'
                    if g == 'matg' else 'skipped')
            C.warn(ctx, 'rev_locale_node_missing', '%s %s (out %d): the tag has no converted node (it failed to convert), %s' % (
                g, getattr(e, 'name', '?'), i, what), getattr(e, 'name', None))
    C.stat(ctx, 'rev_locale_nodes_missing', len(missing))


@post_pass(prio=50)
def finish_locale(ctx, converted):
    st = ensure_planned(ctx)
    missing = []
    for out_index, node in converted.items():
        if node is None:
            missing.append(out_index)
            continue
        if node.group == 'unic' and out_index in st.items:
            set_unic_node(node, st.items[out_index].new_ranges)
        elif node.group == 'matg':
            st.local_matg = True
            if not any(n is node for n in st.matg_nodes):
                st.matg_nodes.append(node)
    if missing:
        _report_missing_nodes(ctx, st, missing)
    dn = C.opt(ctx, 'display_name')
    if dn:
        n = st.override_text('name', dn)
        C.stat(ctx, 'rev_locale_display_name_strings', n)
        if not n:
            C.warn(ctx, 'rev_locale_display_name', "no string 'name' in the map's language pack: display_name %r has no effect" % dn)
    blob, entries, pack = st.build(ctx, 0)
    ctx.locale_blob = blob
    ctx.language_pack = pack if st.header_pack else None
    for n in st.matg_nodes:
        patch_matg_node_mcc(n, matg_entries(ctx, st, entries))
    c = st.counts()
    C.stat(ctx, 'rev_locale_strings_english', c[0])
    C.stat(ctx, 'rev_locale_blob_bytes', len(blob))
    C.stat(ctx, 'rev_locale_backfilled_strings', st.backfilled)


# ====================================================================================================
# driver API
# ====================================================================================================
def build_locale(ctx, locale_base=None):
    """-> (locale_blob, language_pack_252_or_None); entries' offsets = locale_base + position (None = relative, base 0)."""
    st = state(ctx)
    base = 0 if locale_base is None else locale_base
    blob, _entries, pack = st.build(ctx, base)
    return blob, (pack if st.header_pack else None)


def apply_to_builder(ctx, builder):
    """Driver hook: after the map's file layout is known (builder.locale_base = absolute file offset of the locale blob), hand
    the blob (+ header pack in 'header' mode) to the writer and write the absolute offsets into the local matg node(s).
    The writer needs `locale_base` and `set_locale(blob, pack_or_None)`."""
    st = state(ctx)
    base = builder.locale_base
    st.locale_base = base
    blob, entries, pack = st.build(ctx, base)
    builder.set_locale(blob, pack if st.header_pack else None)
    for n in st.matg_nodes:
        patch_matg_node_mcc(n, matg_entries(ctx, st, entries))
    return base
