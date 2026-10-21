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
    dd = (getattr(ctx, 'opts', None) or {}).get('description')        # --description wins like --display-name
    if dd:
        descs = [dd] * N_LANG
        mi = (mi[0] if mi else '', dd)
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


# ----------------------------------------------------------------------------------------------------------------------
# --mp-info FILE / --mp-info-dir DIR (opt-in, 2026-10-21 user: chronopolis, then EVERY map): write the scnr Level Data[0].Multiplayer element.
# The element is ADDED when the map has none and UPDATED when it has one (apply_mp_info below).
# Layout (Assembly Halo2 Multiplayer Levels, 0xC64): +0 Map ID int32, +4 Bitmap tagref, +0xC 9 x utf16[32] names,
# +0x24C 9 x utf16[128] descriptions, +0xB4C ascii[256] path, +0xC4C sort order int32, +0xC50 flags u8, +0xC54 16 x int8 max teams.
# Defaults follow the community map H2.5 BoneYard: map id -1, max teams 8,2,8,8,8,8,8,8,8,2,0...  sub_427EDD reads map_id only
# against 100 / 1200.  Every community map references a bitmap, so 'bitmap' names an existing bitmap of the map (never NONE).
# JSON: {"map_id": -1, "bitmap": "<bitm tag name>", "names": [9 str] or "str", "descriptions": [9 str] or "str",
#        "sort_order": 0, "flags": 0, "max_teams": [16 ints]}      (language order = the engine's: English, Japanese, German, French,
#        Spanish, Italian, Korean, Chinese, Portuguese)
# 'bitmap' = the PREVIEW: a bitm tag of the output map, or a tag of the stock mainmenu.map (e.g. ui\screens\game_shell\pregame_lobby\unknown_map),
# which the planner then adds as a donor tag whose pixel pointer is rebased to mainmenu.map (see PREVIEW_* below).
MP_INFO_TEAMS = [8, 2, 8, 8, 8, 8, 8, 8, 8, 2, 0, 0, 0, 0, 0, 0]

# The lobby's preview image (halo2.exe sub_44F690 -> sub_4C2441 -> sub_4C2069): the bitmap named by Multiplayer[0]+8 must be a bitm tag whose FIRST
# bitmap element is exactly 440 x 414 (sub_4C2069: `word[+4] == 440 && word[+6] == 414`, else the preview is silently dropped), format 11 (A8R8G8B8),
# no mipmaps, flags without 0xC000 / compressed / palettized / swizzled (sub_67536F), pixel stream = ONE zlib stream (78 DA ...) of 440*414*4 bytes at
# the raw pointer element+0x1C (top 2 bits = file: 0 this map, 1 mainmenu.map, 2 shared.map, 3 single_player_shared.map; low 30 bits = offset), its
# compressed size at element+0x34.  A 2048 x 2048 / 11-mip bitmap (chronopolis' chrono_plasma_sign) is therefore never shown.
# Every stock map has its own map_shots\map_mp_<name> bitmap; several (zanzibar, needle, ...) point at mainmenu.map's `ui\code_global_bitmaps\multiplayerNN`
# pixels.  mainmenu.map's own generic placeholder (the grid map with a question mark) is this one:
PREVIEW_BITMAP = 'ui\\screens\\game_shell\\pregame_lobby\\unknown_map'
PREVIEW_W, PREVIEW_H = 440, 414


def load_info(path):
    """Read + validate one mp-info JSON (the shape documented above); returns the dict."""
    import json
    with open(path, encoding='utf-8-sig') as fh:
        info = json.load(fh)
    if not isinstance(info, dict):
        raise ValueError('%s: the mp-info JSON must be an object' % path)
    for k in ('names', 'descriptions'):
        v = info.get(k) or ''
        if not isinstance(v, str) and len(v) != N_LANG:
            raise ValueError('%s: %s needs 1 string or %d entries' % (path, k, N_LANG))
    if info.get('max_teams') is not None and len(info['max_teams']) != 16:
        raise ValueError('%s: max_teams needs 16 values' % path)
    return info


def resolve_mp_info(opts, stem):
    """Fill opts['mp_info_resolved'] (the info dict) and opts['mp_info_path']: an explicit --mp-info FILE, else <--mp-info-dir>\\<output stem>.json
    (case-insensitive file name), else None.  Called by convert before planning because the planner needs the preview bitmap name."""
    import os
    opts['mp_info_resolved'] = None
    opts['mp_info_path'] = None
    path = opts.get('mp_info')
    d = opts.get('mp_info_dir')
    if not path and d and os.path.isdir(d):
        want = (stem + '.json').lower()
        for f in sorted(os.listdir(d)):
            if f.lower() == want:
                path = os.path.join(d, f)
                break
    if not path:
        return None
    opts['mp_info_resolved'] = load_info(path)
    opts['mp_info_path'] = path
    return opts['mp_info_resolved']


@post_pass(prio=61)
def add_mp_info(ctx, converted):
    opts = getattr(ctx, 'opts', None) or {}
    if getattr(ctx, 'map_type', None) != 1:
        return
    info, path = opts.get('mp_info_resolved'), opts.get('mp_info_path')
    if info is None and opts.get('mp_info'):                        # a ctx that did not go through convert (tests)
        path = opts['mp_info']
        info = load_info(path)
    if info is None:
        return
    _apply_mp_info(ctx, converted, info, path)


def _set_teams_where_zero(data, teams):
    """Fill the zero bytes of the max-teams array (+0xC54, 16 x u8) from `teams`; non-zero (author-set) values are never touched.  Returns the count."""
    n = 0
    for i in range(16):
        if not data[OFF_MAX_TEAMS + i] and teams[i]:
            data[OFF_MAX_TEAMS + i] = teams[i] & 0xFF
            n += 1
    return n


def _apply_mp_info(ctx, converted, info, path):
    """Write the 0xC64-byte Multiplayer element of scnr Level Data[0] from `info` (the --mp-info JSON shape): ADD it when the map has none, else UPDATE
    it (English names in every language slot, the descriptions, the preview bitmap; max teams only where the scenario has zero; map id / sort order /
    flags / path kept).  `path` = where the info came from."""
    import struct
    from .. import tagtree as T
    names = info.get('names') or ''
    descs = info.get('descriptions') or ''
    names = [names] * N_LANG if isinstance(names, str) else list(names)
    descs = [descs] * N_LANG if isinstance(descs, str) else list(descs)
    if len(names) != N_LANG or len(descs) != N_LANG:
        raise ValueError('--mp-info: names / descriptions need 1 or %d entries' % N_LANG)
    bm = NULL32 = 0xFFFFFFFF
    if info.get('bitmap'):
        bm = ctx.remap_name('bitm', info['bitmap'])
        if bm in (None, NULL32):
            raise ValueError('--mp-info: bitmap %r is not in the output map' % info['bitmap'])
    teams = list(info.get('max_teams') or MP_INFO_TEAMS)
    if len(teams) != 16:
        raise ValueError('--mp-info: max_teams needs 16 values')
    data = bytearray(MP_ELEMENT_SIZE)
    struct.pack_into('<i', data, 0, int(info.get('map_id', -1)))
    data[4:8] = b'bitm'[::-1]
    struct.pack_into('<I', data, 8, bm)
    for L in range(N_LANG):
        no, do = NAME_OFF + NAME_SIZE * L, DESC_OFF + DESC_SIZE * L
        data[no:no + NAME_SIZE] = _w(clean_text(names[L], NAME_SIZE // 2 - 1, name=True), NAME_SIZE)
        data[do:do + DESC_SIZE] = _w(clean_text(descs[L], DESC_SIZE // 2 - 1), DESC_SIZE)
    struct.pack_into('<i', data, 0xC4C, int(info.get('sort_order', 0)))
    data[0xC50] = int(info.get('flags', 0)) & 0xFF
    data[0xC54:0xC64] = bytes(t & 0xFF for t in teams)
    for node in converted.values():
        if node is None or node.group != 'scnr':
            continue
        lds = node.blocks.get(OFF_LEVEL_DATA) or []
        if not lds:
            ctx.warn('mp_info_skipped', 'scnr has no Level Data element: --mp-info not applied')
            return
        ld = lds[0]
        old = ld.blocks.get(LEVEL_DATA_MP_BLOCK)
        if old:
            mp = old[0]
            if len(mp.data) != MP_ELEMENT_SIZE:
                ctx.warn('mp_info_layout', 'scnr Level Data/Multiplayer element is %#x bytes, expected %#x: --mp-info not applied' % (len(mp.data), MP_ELEMENT_SIZE))
                return
            was = bytes(mp.data)
            for L in range(N_LANG):
                no, do = NAME_OFF + NAME_SIZE * L, DESC_OFF + DESC_SIZE * L
                if names[L]:
                    mp.data[no:no + NAME_SIZE] = _w(clean_text(names[L], NAME_SIZE // 2 - 1, name=True), NAME_SIZE)
                if descs[L]:
                    mp.data[do:do + DESC_SIZE] = _w(clean_text(descs[L], DESC_SIZE // 2 - 1), DESC_SIZE)
            if bm != NULL32:
                mp.data[4:8] = b'bitm'[::-1]
                struct.pack_into('<I', mp.data, 8, bm)
            nt = _set_teams_where_zero(mp.data, teams)
            _stat(ctx, 'mp_info_updated', 1)
            ctx.warn('mp_info_updated', 'updated scnr Level Data[0].Multiplayer element from %s (names %r in %d languages, preview %s, %d max-team slots filled '
                     'where zero, map id %d kept)%s' % (path, names[0], N_LANG, info.get('bitmap'), nt, struct.unpack_from('<i', mp.data, 0)[0],
                                                       '' if bytes(mp.data) != was else ' - nothing changed'))
            return
        f = next((x for x in ld.fields if x.offset == LEVEL_DATA_MP_BLOCK and x.type in T.BLOCK_TYPES), None)
        if f is None or T.element_size(f) != MP_ELEMENT_SIZE:
            ctx.warn('mp_info_layout', 'unexpected Level Data/Multiplayer layout: --mp-info not applied')
            return
        el = T.Node(node.group, T.child_path(ld.path, f), f.children, MP_ELEMENT_SIZE, data)
        ld.blocks[LEVEL_DATA_MP_BLOCK] = [el]
        _stat(ctx, 'mp_info_added', 1)
        ctx.warn('mp_info_added', 'added scnr Level Data[0].Multiplayer element from %s (bitmap %s, max teams %s)'
                 % (path, info.get('bitmap'), teams[:10]))
        return


# ----------------------------------------------------------------------------------------------------------------------
# MapInfo JSON -> max teams per game type (2026-10-21).  The lobby's team menu reads the max-teams-per-game-type array of the
# map's list entry (halo2.exe sub_459369 copies entry+0x968..0x977 = scnr Level Data[0].Multiplayer[0]+0xC54..0xC63; sub_5B087B builds the allowed
# team mask from it, sub_643B35 / sub_616490 test the team count).  MCC workshop maps made with the H2EK often leave that array ZERO (or have no
# Multiplayer element at all): MCC takes the limits from the mod's MapInfo JSON ('MaximumTeamsByGameCategory'), so a converted map would let
# nobody change teams even with a full list entry.  Fill them from that JSON, only where the scenario has none.
MAPINFO_TEAM_SLOTS = {'GameCategory_CTF': 1, 'GameCategory_Slayer': 2, 'GameCategory_Oddball': 3, 'GameCategory_KOTH': 4, 'GameCategory_Race': 5,
                      'GameCategory_Headhunter': 6, 'GameCategory_Juggernaut': 7, 'GameCategory_Territories': 8, 'GameCategory_Assault': 9}
OFF_MAX_TEAMS = 0xC54


def mcc_mapinfo_json(source_path):
    """The mod's MapInfo JSON of the map (same lookup as mcc_mapinfo_texts) as a dict, or None."""
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
    cands = [os.path.join(mp_dir, stem + '.json')] + [os.path.join(mp_dir, f) for f in sorted(os.listdir(mp_dir)) if f.lower().endswith('.json')]
    for p in cands:
        try:
            with open(p, encoding='utf-8-sig') as fh:
                info = json.load(fh)
        except (OSError, ValueError):
            continue
        scen = str(info.get('ScenarioFile') or '').replace('\\', '/').rstrip('/')
        if os.path.splitext(os.path.basename(p))[0].lower() != stem.lower() and scen.split('/')[-1].lower() != stem.lower():
            continue
        return info
    return None


def mapinfo_max_teams(info):
    """16-entry max-teams array (Vista game-type slots, see MAPINFO_TEAM_SLOTS) from a MapInfo JSON dict, or None when it has no limits."""
    m = (info or {}).get('MaximumTeamsByGameCategory')
    if not isinstance(m, dict) or not m:
        return None
    teams = [0] * 16
    for k, slot in MAPINFO_TEAM_SLOTS.items():
        try:
            teams[slot] = max(0, min(16, int(m.get(k, 0))))
        except (TypeError, ValueError):
            pass
    return teams if any(teams[1:10]) else None


@post_pass(prio=62)
def fill_mapinfo_teams(ctx, converted):
    import os
    opts = getattr(ctx, 'opts', None) or {}
    if getattr(ctx, 'map_type', None) != 1 or opts.get('no_mapinfo_teams') or os.environ.get('H2CONV_NO_MAPINFO_TEAMS') or opts.get('mp_info') \
            or opts.get('mp_info_resolved'):
        return
    try:
        info = mcc_mapinfo_json(getattr(getattr(ctx, 'src', None), 'source_path', None))
    except Exception:                                   # noqa: BLE001 - a broken MapInfo JSON: leave the scenario alone
        info = None
    teams = mapinfo_max_teams(info)
    if teams is None:
        return
    for node in converted.values():
        if node is None or node.group != 'scnr':
            continue
        lds = node.blocks.get(OFF_LEVEL_DATA) or []
        if not lds:
            return
        mps = lds[0].blocks.get(LEVEL_DATA_MP_BLOCK) or []
        if mps:
            mp = mps[0]
            if len(mp.data) == MP_ELEMENT_SIZE and not any(mp.data[OFF_MAX_TEAMS + 1:OFF_MAX_TEAMS + 10]):
                mp.data[OFF_MAX_TEAMS:OFF_MAX_TEAMS + 16] = bytes(t & 0xFF for t in teams)
                _stat(ctx, 'mapinfo_teams_filled', 1)
                ctx.warn('mapinfo_teams_filled', 'scnr Level Data/Multiplayer had no max teams per game type (the lobby cannot change teams without them): '
                         'filled from the MCC MapInfo JSON %s' % teams[:10])
            return
        # no Multiplayer element at all: build one from the MapInfo JSON (Title / Description 'Neutral', bitmap NONE like Cartographer's fallback entry)
        title = ((info.get('Title') or {}).get('Neutral') or opts.get('display_name') or '').strip()
        desc = ((info.get('Description') or {}).get('Neutral') or opts.get('description') or '').strip()
        _apply_mp_info(ctx, converted, {'map_id': -1, 'names': opts.get('display_name') or title, 'descriptions': opts.get('description') or desc,
                                        'max_teams': teams}, 'the MCC MapInfo JSON')
        return
