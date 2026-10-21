# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
r"""scnr Level Data / Multiplayer: the name + description the Vista custom-map LIST shows.

Found 2026-10-20 (user screenshot: three entries of Cartographer's custom map list had a BLANK name and the preview pane said
"No custom maps found!"; the maps were the converted Steam-workshop MCC maps martian, plaza and stone_walls).

How the engine fills one custom-map-list entry (halo2.exe sub_44F690, "vanilla list validator", called by Cartographer's
validate_and_read_custom_map_data; reads the scenario, NOT the header language pack):

    mp   = scnr.level_data[0].multiplayer[0]                       (element size 0xC64)
    entry.map_name        = mp.English Name        (utf16 @0xC, 32 wchar)        <- the name in the list
    entry.description[L]  = mp.<lang> Description  (utf16 @0x24C + 0x100*L, 9 x 128 wchar)
    the name must not contain '|', and neither the name nor a description may contain control characters or private-use
    characters (U+E000..U+F8FF: the button glyphs), else the function fails (sub_44C706 / sub_44C792)
    the function returns 1 only when the mopp checksum also matches; when it returns 0 Cartographer falls back to the
    header 'name' (the file-name-like text katabatic / sweet ... show) - but an EMPTY English Name passes every check, so the
    entry succeeds with an empty map_name = a blank row.

MCC maps authored with the H2EK (workshop maps) often leave these utf16 fields empty (the MCC UI takes the title from the
mod's MapInfo JSON / the unic tag instead); the converter copied them verbatim -> blank names in the Vista list.
Maps whose author filled them (headlong_mcc, cyberdyne_h2, cbsp) were fine.

Fix (this module): when a field is empty, fill it from the map's own localized `name` / `description` strings (the unic
tag `scenarios\descriptions\<map>`, i.e. what the header language pack holds), per language, English as the fallback
(Vista's own maps do the same: the Portuguese slot repeats English).  Fields that the author did fill are never touched.

  post_pass fill_mp_names     runs after handlers/locale.finish_locale (prio 50), before the tags are encoded
  library   fill_block(data, names, descs) -> number of fields written        (bytes level, used by h2conv.mpnames)
"""
import unicodedata

from . import post_pass

N_LANG = 9
NAME_OFF = 0xC            # 9 x utf16[32]
NAME_SIZE = 0x40
DESC_OFF = 0x24C          # 9 x utf16[128]
DESC_SIZE = 0x100
MP_ELEMENT_SIZE = 0xC64
OFF_LEVEL_DATA = 0x398
LEVEL_DATA_MP_BLOCK = 0x10


def _stat(ctx, key, n=1):
    try:
        ctx.stats[key] += n
    except Exception:                                   # noqa: BLE001
        pass


def clean_text(text, max_units, name=False):
    """Make `text` acceptable to the engine's list validator and fit `max_units` UTF-16 code units (NUL excluded)."""
    out = []
    for ch in text:
        cp = ord(ch)
        cat = unicodedata.category(ch)
        if 0xE000 <= cp <= 0xF8FF:                      # private use: Halo button / weapon glyphs
            continue
        if cat in ('Cc', 'Cs', 'Zl', 'Zp') or cp == 0xFFFE or cp == 0xFFFF:
            out.append(' ')
            continue
        if name and ch == '|':
            continue
        out.append(ch)
    s = ' '.join(''.join(out).split())
    # truncate on code point boundaries
    res, units = [], 0
    for ch in s:
        u = 2 if ord(ch) > 0xFFFF else 1
        if units + u > max_units:
            break
        res.append(ch)
        units += u
    return ''.join(res).strip()


def _w(text, size):
    b = text.encode('utf-16-le')
    return b[:size - 2].ljust(size, b'\0')


def _read(data, off, size):
    return bytes(data[off:off + size]).decode('utf-16-le', 'replace').split('\0')[0]


def fill_block(data, names, descs, force=False):
    """data = bytearray of ONE Level Data / Multiplayer element (0xC64).  names / descs = 9 str each (English fallback is
    the caller's business).  Writes only the fields that are empty (force=True: every field that has a new text and differs);
    returns the number of fields written."""
    n = 0
    for L in range(N_LANG):
        no, do = NAME_OFF + NAME_SIZE * L, DESC_OFF + DESC_SIZE * L
        if names[L] and (not _read(data, no, NAME_SIZE) or force) and \
                _read(data, no, NAME_SIZE) != clean_text(names[L], NAME_SIZE // 2 - 1, name=True):
            data[no:no + NAME_SIZE] = _w(clean_text(names[L], NAME_SIZE // 2 - 1, name=True), NAME_SIZE)
            n += 1
        if descs[L] and (not _read(data, do, DESC_SIZE) or force) and \
                _read(data, do, DESC_SIZE) != clean_text(descs[L], DESC_SIZE // 2 - 1):
            data[do:do + DESC_SIZE] = _w(clean_text(descs[L], DESC_SIZE // 2 - 1), DESC_SIZE)
            n += 1
    return n


def texts_from_langs(langs):
    """langs = 9 lists of (key_text, utf8 bytes) (the header pack / LocaleState.out): the FIRST 'name' and 'description' of
    each language, empty/missing -> English.  Returns (names[9], descs[9]) as str."""
    def first(L, key):
        for k, v in langs[L]:
            if k == key:
                t = v.decode('utf-8', 'replace').strip()
                if t:
                    return t
        return ''
    en_n, en_d = first(0, 'name'), first(0, 'description')
    names = [first(L, 'name') or en_n for L in range(N_LANG)]
    descs = [first(L, 'description') or en_d for L in range(N_LANG)]
    return names, descs


def _description_langs(st):
    """Per-language [(key, bytes)] of the map's own description unic tag (name contains 'descriptions'), else all strings."""
    items = [it for it in st.items.values() if it.new_ranges and 'descriptions' in (it.name or '').lower()]
    if not items:
        return st.out
    it = sorted(items, key=lambda x: x.out_index)[0]
    langs = []
    for L in range(N_LANG):
        start, cnt = it.new_ranges[L]
        langs.append(list(st.out[L][start:start + cnt]) if cnt else [])
    # languages the description tag has no strings for (Portuguese is dropped) fall back to English in texts_from_langs
    return langs


def mcc_mapinfo_texts(source_path):
    """(title, description) from the MCC mod's own MapInfo JSON (<mod>\\multiplayer\\<stem>.json, the name the MCC menu
    shows), or None.  The mod root is the nearest folder above the map holding ModInfo.json (maps sit in <mod>\\maps\\ or
    <mod>\\halo2\\h2_maps_win64_dx11\\).  A title that is just the file stem (e.g. 'stone_walls') is humanised ('Stone Walls').
    2026-10-20 (user): the converted list names should be the MCC names ('Plaza', not the language pack's 'District')."""
    import json
    import os
    if not source_path:
        return None
    stem = os.path.splitext(os.path.basename(source_path))[0]
    d = os.path.dirname(os.path.abspath(source_path))
    for _ in range(4):
        if os.path.isfile(os.path.join(d, 'ModInfo.json')):
            break
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    else:
        return None
    mp_dir = os.path.join(d, 'multiplayer')
    if not os.path.isdir(mp_dir):
        return None
    cands = [os.path.join(mp_dir, stem + '.json')]
    cands += [os.path.join(mp_dir, f) for f in sorted(os.listdir(mp_dir)) if f.lower().endswith('.json')]
    for p in cands:
        try:
            with open(p, encoding='utf-8-sig') as fh:
                info = json.load(fh)
        except (OSError, ValueError):
            continue
        scen = str(info.get('ScenarioFile') or '').replace('\\', '/').rstrip('/')
        if os.path.splitext(os.path.basename(p))[0].lower() != stem.lower() and scen.split('/')[-1].lower() != stem.lower():
            continue
        title = ((info.get('Title') or {}).get('Neutral') or '').strip()
        desc = ((info.get('Description') or {}).get('Neutral') or '').strip()
        if title and title.lower() == stem.lower() and ('_' in title or title.islower()):
            title = ' '.join(w[:1].upper() + w[1:] for w in title.replace('_', ' ').split())
        if title or desc:
            return title, desc
    return None


# ----------------------------------------------------------------------------------------------------
# the HEADER name (0x1A4): what the list shows when the vanilla list validator fails (every map with a LOCAL matg)
# ----------------------------------------------------------------------------------------------------
# halo2.exe sub_44F690 fails at 0x44F942 (`cmp eax,2710h; jl` on the globals datum index) for a local matg, BEFORE it copies any name.
# Cartographer's validate_and_read_custom_map_data (xlive.dll 0x1008F9xx-0x1008FD9x in the installed build, = map_repository.cpp:956-971)
# then sets entry.map_name = utf8->wchar(header.name) (strnlen < 32 required; empty -> the file name), no description, no preview.
# header.name is read by nothing else that matters: the exe only verifies strnlen < 32; Cartographer compares it with the stock names
# 'coagulation' / 'lockout' (Halloween.cpp) and '05a_deltaapproach' (tag_fixes.cpp) with strcmp (case-sensitive); map download uses the
# FILE name; the lobby identity is {entry.map_name, sha256}.  54 of the 157 community maps in one collection of community maps have a header name that
# differs from the file name (and four share the header name 'a').
HEADER_NAME_MAX = 31
STOCK_KEYED_NAMES = ('coagulation', 'lockout', '05a_deltaapproach')


def header_name_from_title(title):
    """A title as a header name: ASCII only (accents folded), printable, no '|' (the UI escape), <= 31 bytes, no leading/trailing
    space.  None when nothing usable is left or when it is exactly a stock name Cartographer keys code on."""
    if not title:
        return None
    s = unicodedata.normalize('NFKD', title)
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    s = ''.join(ch if 0x20 <= ord(ch) <= 0x7E and ch != '|' else ' ' for ch in s)
    s = ' '.join(s.split())[:HEADER_NAME_MAX].strip()
    if not any(ch.isalnum() for ch in s) or s in STOCK_KEYED_NAMES:
        return None
    return s


def list_header_name(source_path, opts, default):
    """(header name, source) for a converted multiplayer map.  Order: --display-name, then the MCC MapInfo title (the name the MCC menu
    shows), else `default` (the file stem / MCC internal name).  The FILE name is never touched."""
    opts = opts or {}
    dn = header_name_from_title(opts.get('display_name'))
    if dn:
        return dn, 'display-name'
    try:
        mi = mcc_mapinfo_texts(source_path)
    except Exception:                                   # noqa: BLE001 - a broken MapInfo JSON: keep the default
        mi = None
    if mi and mi[0]:
        t = header_name_from_title(mi[0])
        if t:
            return t, 'mapinfo'
    return default, 'stem'


@post_pass(prio=60)
def fill_mp_names(ctx, converted):
    if getattr(ctx, 'map_type', None) != 1:
        return
    st = getattr(ctx, 'locale_state', None)
    if st is None:
        return
    try:
        names, descs = texts_from_langs(_description_langs(st))
    except Exception as ex:                             # noqa: BLE001
        ctx.warn('mp_names_source', 'could not read the localized name / description: %r' % (ex,))
        return
    try:
        mi = mcc_mapinfo_texts(getattr(getattr(ctx, 'src', None), 'source_path', None))
    except Exception:                                   # noqa: BLE001 - a broken MapInfo JSON: keep the localized texts
        mi = None
    if mi:
        if mi[0]:
            names = [mi[0]] * N_LANG
        if mi[1]:
            descs = [mi[1]] * N_LANG
    dn = (getattr(ctx, 'opts', None) or {}).get('display_name')
    if dn:
        names = [dn] * N_LANG
    if not any(names) and not any(descs):
        return
    for node in converted.values():
        if node is None or node.group != 'scnr':
            continue
        for ld in node.blocks.get(OFF_LEVEL_DATA) or []:
            for mp in ld.blocks.get(LEVEL_DATA_MP_BLOCK) or []:
                if len(mp.data) != MP_ELEMENT_SIZE:
                    ctx.warn('mp_names_layout', 'scnr Level Data/Multiplayer element is %#x bytes, expected %#x: names not filled'
                             % (len(mp.data), MP_ELEMENT_SIZE))
                    continue
                f = mp.field('English Name')
                if f is None or f.offset != NAME_OFF:
                    ctx.warn('mp_names_layout', "scnr Level Data/Multiplayer has no 'English Name' at 0xC: names not filled")
                    continue
                # the MCC menu title (MapInfo JSON) and an explicit --display-name WIN over a name the author left in the scenario
                # (2026-10-20 user: cyberdyne_h2 said 'Zanzibar', MCC shows 'The Pit' -> 'The Pit')
                blank = [''] * N_LANG
                n = fill_block(mp.data, names, blank, force=bool((mi and mi[0]) or dn)) + \
                    fill_block(mp.data, blank, descs, force=bool(mi and mi[1]))
                if n:
                    _stat(ctx, 'mp_level_names_filled', n)
                    ctx.warn('mp_level_names_filled', "scnr Level Data/Multiplayer name/description: wrote %d fields from %s" % (
                        n, 'the MCC MapInfo JSON / --display-name' if (mi or dn) else "the map's own 'name' / 'description' strings "
                        "(they were empty: the Vista custom-map list would show a blank name)"))
