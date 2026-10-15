# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""What a Halo 2 `.map` file IS (cheap header-level identification + classification) and the Excession MapInfo JSON for it.

Pure stdlib (+ lazy imports of h2conv.vista_cache / h2conv.handlers.locale for map titles).

Public API
----------
identify(path, deep=True)   -> MapRecord      header-only look at one file (v8 Vista / v13 MCC / invalid), never raises
scan_folder(root)           -> [MapRecord]    recursive scan for *.map (junctions/symlinks are not followed), sorted by path
stock_mcc_times()           -> {'mainmenu','shared','single_player_shared': FILETIME}   creation times of the stock MCC databases
read_titles(path)           -> {'title','description','source'}   localized map name / description of a Vista map
source_sha256(path)         -> hex                streamed SHA-256 of a file (BELOW_NORMAL process, 4 MiB blocks)
mp_mapinfo(...) / campaign_mapinfo(...)           ordered dicts that serialise to exactly the JSON Excession writes
dumps(obj)                                        Excession's JSON formatting (tab indent, LF, no trailing newline, UTF-8)

Classification (MapRecord.status / .code):
    ok       convertible: Vista v8 (MP / solo) or MCC v13 (MP / solo, compressed or not, H2EK self-contained or stock-linked)
    skip     deliberately not converted: code 'database' (shared / single_player_shared / mainmenu), 'custom_shared'
             (MCC map that needs a CUSTOM shared.map - unsupported)
    invalid  not a usable Halo 2 cache: 'unreadable', 'too_small', 'bad_signature', 'unsupported_version', 'bad_footer',
             'bad_type', 'not_pc_build', 'truncated', 'bad_tags_header'
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL
except Exception:  # pragma: no cover
    pass

import collections
import hashlib
import json
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

MCC_HEADER_SIZE = 0x380
VISTA_HEADER_SIZE = 0x800
MCC_VERSION = 13
VISTA_VERSION = 8
TAGS_SIG = b'sgat'                 # 'tags' stored little endian, at <tag region>+0x1C in both games
NONE32 = 0xFFFFFFFF

try:
    from . import paths as _paths
except ImportError:                 # imported as a top-level module by the developer tools
    import paths as _paths


def __getattr__(name):
    """STOCK_MCC_DIR / EXCESSION_DIR / EXCESSION_HALO2_JSON: folders on one PC, now resolved by h2conv.paths (--mcc-maps / H2CONV_MCC_MAPS /
    settings.json / your Steam libraries); kept as attributes for the developer tools."""
    if name == 'STOCK_MCC_DIR':
        return _paths.mcc_maps_dir()
    if name == 'EXCESSION_DIR':
        return _paths.excession_dir()
    if name == 'EXCESSION_HALO2_JSON':
        return _paths.excession_halo2_json()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


# creation FILETIMEs (header 0x48) of the stock MCC databases of the installed build - used when the MCC folder is not readable.
FALLBACK_STOCK_TIMES = {'mainmenu': 0x01D99694043A2F2F, 'shared': 0x01D9969417F1A86C, 'single_player_shared': 0x01D996943AC13C7D}
SHARED_SLOTS = ('mainmenu', 'shared', 'single_player_shared')      # order of the three recorded times (v13: 0x50 / 0x58 / 0x60)

KIND_NAMES = {0: 'solo', 1: 'multiplayer', 2: 'mainmenu', 3: 'shared', 4: 'single_player_shared'}
DATABASE_KINDS = ('mainmenu', 'shared', 'single_player_shared')
VISTA_BUILDS = ('11028.07.03.23.1927.main', '11081.07.04.30.0934.main', '11122.07.08.24.1808.main')   # limits.BUILDS
PC_BUILD_RE = re.compile(r'^\d{4,6}(\.\d{1,4}){4}\.[A-Za-z]+$')

MAX_TITLE = 64                      # conservative caps (Excession's editor limits are not in the data files)
MAX_DESCRIPTION = 256


# ======================================================================================================
# helpers
# ======================================================================================================
def _u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def _u64(b, o):
    return struct.unpack_from('<Q', b, o)[0]


def _cstr(b, o, n):
    return bytes(b[o:o + n]).split(b'\0')[0].decode('latin-1')


def filetime_hex(v):
    return '0x%016X' % v if v else '0'


def norm_scenario(path):
    """'scenarios\\Multi\\Box\\box' -> 'scenarios/multi/box/box' (Excession writes forward slashes; compares are case-insensitive)."""
    return (path or '').replace('\\', '/').strip('/').lower()


def humanize(name):
    """'dam_defense(OLD)' -> 'Dam Defense (OLD)'; used when a map carries no localized name."""
    s = re.sub(r'[_\-]+', ' ', name or '').strip()
    s = re.sub(r'(?<=[A-Za-z0-9])\(', ' (', s)
    s = re.sub(r'\s+', ' ', s)
    return ' '.join(w[:1].upper() + w[1:] if w.islower() else w for w in s.split(' ')) if s else (name or '')


def clean_text(s, limit):
    """Strip control characters / NULs, collapse whitespace, cut to `limit` characters (on a word boundary when possible)."""
    s = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', s or '')
    s = re.sub(r'[ \t]+', ' ', s).strip()
    if len(s) > limit:
        cut = s[:limit]
        sp = cut.rfind(' ')
        s = (cut[:sp] if sp > limit * 0.6 else cut).rstrip(' ,;:-') + '...'
        s = s[:limit]
    return s


def source_sha256(path, block=1 << 22):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            b = fh.read(block)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ======================================================================================================
# MapRecord
# ======================================================================================================
class MapRecord(object):
    """One looked-at file.  `fmt` 'vista' | 'mcc' | 'invalid'; `status` 'ok' | 'skip' | 'invalid'; `code` machine readable cause."""
    FIELDS = ('path', 'size', 'mtime', 'fmt', 'version', 'kind', 'type_code', 'name', 'scenario_path', 'build', 'compressed',
              'style', 'creation_time', 'shared_times', 'custom_shared', 'external_tags', 'status', 'code', 'reason', 'warnings',
              'header_file_size')

    def __init__(self, path):
        self.path = path
        self.size = 0
        self.mtime = 0.0
        self.fmt = 'invalid'
        self.version = None
        self.kind = None                 # solo | multiplayer | mainmenu | shared | single_player_shared
        self.type_code = None
        self.name = ''                   # header internal name
        self.scenario_path = ''          # as stored (backslashes)
        self.build = ''                  # Vista build string
        self.compressed = False          # MCC chunk-compressed
        self.style = None                # MCC: 'stock' (bitmaps/sounds in textures.dat/sounds_*.dat) | 'h2ek' (self-contained)
        self.creation_time = 0
        self.shared_times = (0, 0, 0)    # recorded creation FILETIMEs of the databases it was built against
        self.custom_shared = None        # MCC: True = needs a non-stock shared.map
        self.external_tags = None        # MCC: count of tags that live in shared.map (deep check), None = not counted
        self.status = 'invalid'
        self.code = 'unreadable'
        self.reason = ''
        self.warnings = []
        self.header_file_size = None

    # -- convenience --------------------------------------------------------------------------------
    @property
    def stem(self):
        return os.path.splitext(os.path.basename(self.path))[0]

    @property
    def is_database(self):
        return self.kind in DATABASE_KINDS

    @property
    def convertible(self):
        return self.status == 'ok'

    @property
    def scenario_norm(self):
        return norm_scenario(self.scenario_path)

    def label(self):
        """Short human readable classification, e.g. 'Vista v8 multiplayer (build 11081.07.04.30.0934.main)'."""
        if self.fmt == 'vista':
            return 'Vista v8 %s (build %s)' % (self.kind, self.build)
        if self.fmt == 'mcc':
            extra = ['compressed' if self.compressed else 'uncompressed', 'H2EK self-contained' if self.style == 'h2ek' else 'stock-linked']
            return 'MCC v13 %s (%s)' % (self.kind, ', '.join(extra))
        return 'invalid'

    def to_dict(self):
        d = collections.OrderedDict()
        for k in self.FIELDS:
            v = getattr(self, k)
            if k == 'shared_times':
                v = [filetime_hex(x) for x in v]
            elif k == 'creation_time':
                v = filetime_hex(v)
            d[k] = v
        d['label'] = self.label()
        return d

    def __repr__(self):
        return '<MapRecord %s %s %s/%s>' % (os.path.basename(self.path), self.label(), self.status, self.code)


def _set(rec, status, code, reason=''):
    rec.status, rec.code, rec.reason = status, code, reason
    return rec


# ======================================================================================================
# stock MCC database times
# ======================================================================================================
_STOCK_CACHE = {}


def _read_head(path, n):
    with open(path, 'rb') as fh:
        return fh.read(n)


def stock_mcc_times(mcc_dir=None, refresh=False):
    """{'mainmenu','shared','single_player_shared': creation FILETIME (header 0x48)} of the stock MCC databases.  Read from
    `<mcc_dir>/<paths.STOCK_BACKUP_DIR_NAME>` when that folder exists (an untouched copy some modders keep), else from `mcc_dir` itself (default: the installed
    game's h2_maps_win64_dx11); falls back to the values measured on this PC for a missing file."""
    try:
        mcc_dir = mcc_dir or _paths.mcc_maps_dir(required=False)
    except _paths.PathsError:                          # identify() never raises: a broken setting is reported by the converters, not here
        mcc_dir = None
    if not mcc_dir:                                    # no MCC found: the values of the tested build
        return dict(FALLBACK_STOCK_TIMES)
    key = os.path.normcase(os.path.abspath(mcc_dir))
    if refresh or key not in _STOCK_CACHE:
        out = dict(FALLBACK_STOCK_TIMES)
        src = {}
        for slot in SHARED_SLOTS:
            for d in (os.path.join(mcc_dir, _paths.STOCK_BACKUP_DIR_NAME), mcc_dir):
                p = os.path.join(d, slot + '.map')
                try:
                    b = _read_head(p, MCC_HEADER_SIZE)
                    if len(b) == MCC_HEADER_SIZE and b[:4] == b'daeh' and _u32(b, 4) == MCC_VERSION:
                        out[slot] = _u64(b, 0x48)
                        src[slot] = p
                        break
                except OSError:
                    pass
        _STOCK_CACHE[key] = (out, src)
    return dict(_STOCK_CACHE[key][0])


def near_shared_time(map_path):
    """(path, creation FILETIME) of a v13 `shared.map` that sits in the same folder as `map_path`, else (None, 0)."""
    d = os.path.dirname(os.path.abspath(map_path))
    try:
        names = os.listdir(d)
    except OSError:
        return None, 0
    for n in names:
        if n.lower() == 'shared.map':
            p = os.path.join(d, n)
            try:
                b = _read_head(p, MCC_HEADER_SIZE)
            except OSError:
                continue
            if len(b) == MCC_HEADER_SIZE and b[:4] == b'daeh' and _u32(b, 4) == MCC_VERSION:
                return p, _u64(b, 0x48)
    return None, 0


def count_external_tags(path, meta_offset):
    """Tags of an UNcompressed v13 map that live in shared.map (valid group, address 0, size 0).  None if the table cannot be read."""
    try:
        with open(path, 'rb') as fh:
            fh.seek(meta_offset)
            mh = fh.read(0x20)
            if len(mh) < 0x20 or mh[0x1C:0x20] != TAGS_SIG:
                return None
            tag_table_off, n_tags = _u32(mh, 8), _u32(mh, 0x18)
            if n_tags > 100000:
                return None
            fh.seek(meta_offset + tag_table_off)
            tbl = fh.read(16 * n_tags)
        if len(tbl) != 16 * n_tags:
            return None
        n = 0
        for g, _datum, addr, sz in struct.iter_unpack('<IIII', tbl):
            if g != NONE32 and addr == 0 and sz == 0:
                n += 1
        return n
    except (OSError, struct.error):
        return None


# ======================================================================================================
# identify
# ======================================================================================================
def identify(path, deep=True, mcc_dir=None):
    """Header-level look at one file.  Never raises; the verdict is in `.status` / `.code` / `.reason`."""
    rec = MapRecord(path)
    try:
        st = os.stat(path)
        rec.size, rec.mtime = st.st_size, st.st_mtime
        with open(path, 'rb') as fh:
            buf = fh.read(VISTA_HEADER_SIZE)
    except OSError as ex:
        return _set(rec, 'invalid', 'unreadable', 'cannot read: %s' % ex)
    if len(buf) < MCC_HEADER_SIZE:
        return _set(rec, 'invalid', 'too_small', 'file is only %d bytes (a cache header is 0x380 / 0x800)' % len(buf))
    if buf[:4] != b'daeh':
        return _set(rec, 'invalid', 'bad_signature', "not a Halo 2 cache file (signature %r, expected b'daeh')" % bytes(buf[:4]))
    rec.version = _u32(buf, 4)
    if rec.version == MCC_VERSION:
        return _identify_mcc(rec, buf, deep, mcc_dir)
    if rec.version == VISTA_VERSION:
        return _identify_vista(rec, buf)
    return _set(rec, 'invalid', 'unsupported_version',
                'cache version %d (this tool handles Halo 2 Vista = 8 and Halo 2 MCC = 13)' % rec.version)


def _identify_vista(rec, buf):
    rec.fmt = 'vista'
    if len(buf) < VISTA_HEADER_SIZE:
        return _set(rec, 'invalid', 'too_small', 'version-8 file shorter than the 0x800-byte Vista header')
    if buf[0x7FC:0x800] != b'toof':
        return _set(rec, 'invalid', 'bad_footer', "no 'foot' marker at 0x7FC")
    rec.type_code = struct.unpack_from('<h', buf, 0x14C)[0]
    if not 0 <= rec.type_code < 5:
        return _set(rec, 'invalid', 'bad_type', 'scenario type %d outside [0,5)' % rec.type_code)
    rec.kind = KIND_NAMES[rec.type_code]
    rec.build = _cstr(buf, 0x12C, 0x20)
    rec.name = _cstr(buf, 0x1A4, 0x20)
    rec.scenario_path = _cstr(buf, 0x1C8, 0x100)
    rec.creation_time = _u64(buf, 0x184)
    rec.shared_times = struct.unpack_from('<3Q', buf, 0x18C)
    rec.header_file_size = _u32(buf, 8)
    tag_offset = _u32(buf, 0x10)
    if not PC_BUILD_RE.match(rec.build):
        return _set(rec, 'invalid', 'not_pc_build',
                    'build string %r is not a Halo 2 Vista (PC) build (Xbox / alpha / beta cache?)' % rec.build)
    if rec.size < rec.header_file_size:
        return _set(rec, 'invalid', 'truncated', 'file is %d bytes but its header says %d' % (rec.size, rec.header_file_size))
    try:
        with open(rec.path, 'rb') as fh:
            fh.seek(tag_offset + 0x1C)
            sig = fh.read(4)
    except OSError as ex:
        return _set(rec, 'invalid', 'unreadable', 'cannot read: %s' % ex)
    if sig != TAGS_SIG:
        return _set(rec, 'invalid', 'bad_tags_header', "no 'tags' signature at the tag region (offset 0x%X)" % tag_offset)
    if rec.size > rec.header_file_size:
        rec.warnings.append('file is %d bytes larger than its header says (trailing data)' % (rec.size - rec.header_file_size))
    if rec.build not in VISTA_BUILDS:
        rec.warnings.append('build %s is not in the Vista loader allow-list (%s) - Cartographer cannot load it, the reverse '
                            'converter may still be able to read it' % (rec.build, ', '.join(VISTA_BUILDS)))
    if rec.is_database:
        return _set(rec, 'skip', 'database', '%s is a shared database map, not a playable map' % rec.kind)
    return _set(rec, 'ok', 'vista', '')


def _identify_mcc(rec, buf, deep, mcc_dir):
    rec.fmt = 'mcc'
    if buf[0x37C:0x380] != b'toof':
        return _set(rec, 'invalid', 'bad_footer', "no 'foot' marker at 0x37C")
    rec.type_code = struct.unpack_from('<H', buf, 0x18)[0]
    if rec.type_code > 4:
        return _set(rec, 'invalid', 'bad_type', 'scenario type %d outside [0,5)' % rec.type_code)
    rec.kind = KIND_NAMES[rec.type_code]
    flags = struct.unpack_from('<H', buf, 0x1C)[0]
    rec.compressed = bool(flags & 1)
    rec.style = 'stock' if (flags & 0x08) else 'h2ek'
    rec.header_file_size = _u32(buf, 8)
    meta_offset, meta_size = _u32(buf, 0x10), _u32(buf, 0x14)
    mask = _u32(buf, 0x2D0)
    rec.creation_time = _u64(buf, 0x48)
    rec.shared_times = struct.unpack_from('<3Q', buf, 0x50)
    rec.name = _cstr(buf, 0xB0, 0x20)
    rec.scenario_path = _cstr(buf, 0xD0, 0x100)
    if rec.compressed:
        n, tbl = _u32(buf, 0x314), _u32(buf, 0x310)
        if n == 0 or tbl + 8 * n > rec.size:
            return _set(rec, 'invalid', 'truncated', 'compressed map whose chunk table (0x%X, %d chunks) lies beyond the end of the file' % (tbl, n))
    else:
        if rec.size < rec.header_file_size:
            return _set(rec, 'invalid', 'truncated', 'file is %d bytes but its header says %d' % (rec.size, rec.header_file_size))
        if rec.size > rec.header_file_size:
            rec.warnings.append('file is %d bytes larger than its header says (trailing data)' % (rec.size - rec.header_file_size))
        if meta_offset + meta_size > rec.size:
            return _set(rec, 'invalid', 'truncated', 'tag region 0x%X+0x%X lies beyond the end of the file' % (meta_offset, meta_size))
        try:
            with open(rec.path, 'rb') as fh:
                fh.seek(meta_offset + 0x1C)
                sig = fh.read(4)
        except OSError as ex:
            return _set(rec, 'invalid', 'unreadable', 'cannot read: %s' % ex)
        if sig != TAGS_SIG:
            return _set(rec, 'invalid', 'bad_tags_header', "no 'tags' signature at the meta region (offset 0x%X)" % meta_offset)
    if rec.is_database:
        return _set(rec, 'skip', 'database', '%s is a shared database map, not a playable map' % rec.kind)
    # ---- custom shared.map detection ---------------------------------------------------------------------------------------
    stock = stock_mcc_times(mcc_dir)
    wrong = [(slot, t) for slot, t in zip(SHARED_SLOTS, rec.shared_times) if t and t != stock[slot]]
    uses_shared = bool(any(rec.shared_times)) or mask != 0
    rec.custom_shared = False
    if wrong:
        rec.custom_shared = True
        why = ', '.join('%s built against creation time %s, stock %s is %s' % (s, filetime_hex(t), s, filetime_hex(stock[s])) for s, t in wrong)
        near_path, near_t = near_shared_time(rec.path)
        if near_path and any(near_t == t for _s, t in wrong):
            why += '; the matching shared.map is next to the map (%s)' % near_path
        elif near_path:
            why += '; a shared.map next to the map has creation time %s, which does not match either' % filetime_hex(near_t)
        if deep and not rec.compressed:
            rec.external_tags = count_external_tags(rec.path, meta_offset)
            if rec.external_tags == 0:
                rec.custom_shared = False
                rec.warnings.append('built against a non-stock shared.map (%s) but references no shared tags - converted like a self-contained map' % why)
                why = ''
        if rec.custom_shared:
            return _set(rec, 'skip', 'custom_shared',
                        'depends on a CUSTOM shared.map (unsupported): %s%s' % (why, '' if rec.external_tags is None else '; %d tags are external' % rec.external_tags))
    elif mask != 0 and not rec.shared_times[1]:
        # linked to a shared.map (mask != 0) yet no creation time recorded: cannot be compared, count the external tags
        if deep and not rec.compressed:
            rec.external_tags = count_external_tags(rec.path, meta_offset)
        if rec.external_tags:
            rec.custom_shared = True
            return _set(rec, 'skip', 'custom_shared',
                        'references %d shared.map tags but records no shared.map creation time - the shared.map it needs cannot be '
                        'verified to be the stock one (unsupported)' % rec.external_tags)
        rec.warnings.append('header marks the map as shared-linked (mask 0x%X) but no creation time is recorded' % mask)
    elif uses_shared and rec.style == 'h2ek':
        rec.warnings.append('H2EK-style flags but a shared-map reference is recorded')
    return _set(rec, 'ok', 'mcc', '')


# ======================================================================================================
# scanning
# ======================================================================================================
SKIP_DIRS = {'$recycle.bin', 'system volume information', '.git', '__pycache__', '.h2conv', '.h2conv_staging'}
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_link(path):
    """Symlink or junction (Windows reparse point): never followed (old farm dirs contain junctions into live tag folders)."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    return bool(getattr(st, 'st_file_attributes', 0) & FILE_ATTRIBUTE_REPARSE_POINT) or os.path.islink(path)


def iter_map_files(root, recursive=True, exclude=()):
    """Yield every `*.map` file path below `root` (sorted, case-insensitive), skipping reparse points and the folders in `exclude`."""
    excl = set(os.path.normcase(os.path.abspath(e)) for e in exclude if e)

    def walk(d):
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            return
        subdirs = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower() not in SKIP_DIRS and not _is_link(e.path) and os.path.normcase(os.path.abspath(e.path)) not in excl:
                        subdirs.append(e.path)
                elif e.is_file(follow_symlinks=False) and e.name.lower().endswith('.map'):
                    yield e.path
            except OSError:
                continue
        if recursive:
            for sd in subdirs:
                for p in walk(sd):
                    yield p

    for p in walk(os.path.abspath(root)):
        yield p


def scan_folder(root, recursive=True, exclude=(), deep=True, mcc_dir=None, progress=None):
    """-> [MapRecord] of every .map below `root` (a single file is accepted too)."""
    if os.path.isfile(root):
        return [identify(root, deep=deep, mcc_dir=mcc_dir)]
    out = []
    for p in iter_map_files(root, recursive, exclude):
        out.append(identify(p, deep=deep, mcc_dir=mcc_dir))
        if progress:
            progress(len(out), p)
    return out


# ======================================================================================================
# titles
# ======================================================================================================
_STOCK_CAMPAIGN_TITLES = {
    '00a_introduction': 'The Heretic', '01a_tutorial': 'The Armory', '01b_spacestation': 'Cairo Station', '03a_oldmombasa': 'Outskirts',
    '03b_newmombasa': 'Metropolis', '04a_gasgiant': 'The Arbiter', '04b_floodlab': 'The Oracle', '05a_deltaapproach': 'Delta Halo',
    '05b_deltatowers': 'Regret', '06a_sentinelwalls': 'Sacred Icon', '06b_floodzone': 'Quarantine Zone', '07a_highcharity': 'Gravemind',
    '08a_deltacliffs': 'Uprising', '07b_forerunnership': 'High Charity', '08b_deltacontrol': 'The Great Journey'}


def read_titles(path, rec=None):
    """Localized map name + description of a Vista map from its header language pack ('name' / 'description' strings, English).
    Falls back to the stock campaign level names, then to a humanised file name.  Never raises."""
    rec = rec or identify(path, deep=False)
    stem = os.path.splitext(os.path.basename(path))[0]
    title = desc = ''
    source = 'file name'
    if rec.fmt == 'vista':
        try:
            from h2conv.vista_cache import VistaCache
            from h2conv.handlers import locale as L
            c = VistaCache(path)
            try:
                if c.header.language_pack_offset != NONE32:
                    arr = L.vista_pack(c, 'header')
                    for key, val in arr.langs[0]:
                        t = val.decode('utf-8', 'replace')
                        if key == 'name' and not title:
                            title = t
                        elif key == 'description' and not desc:
                            desc = t
                    if title:
                        source = 'language pack'
            finally:
                L._ARRAYS.pop((os.path.abspath(path), 'vista', 'header'), None)     # do not keep the (big) string arrays of 500 maps
                c.close()
        except Exception:                                                              # noqa: BLE001 - damaged map: use the fallbacks
            title = desc = ''
    if not title:
        t = _STOCK_CAMPAIGN_TITLES.get((rec.name or stem).lower()) or _STOCK_CAMPAIGN_TITLES.get(stem.lower())
        if t:
            title, source = t, 'stock campaign table'
        else:
            title, source = humanize(rec.name or stem), 'file name'
    return {'title': clean_text(title, MAX_TITLE) or humanize(stem), 'description': clean_text(desc, MAX_DESCRIPTION), 'source': source}


# ======================================================================================================
# Excession MapInfo JSON
# ======================================================================================================
def _strip_json_comments(text):
    """Remove // and /* */ comments outside of strings (Excession's built-in data files are JSON with comments)."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif c == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                i += 1
            continue
        elif c == '/' and i + 1 < n and text[i + 1] == '*':
            i = text.find('*/', i + 2)
            i = n if i < 0 else i + 2
            continue
        else:
            out.append(c)
        i += 1
    return ''.join(out)


# the key order Excession itself writes (taken from working Excession packages), values = Excession's built-in Halo 2 defaults
DEFAULT_MAX_TEAMS = collections.OrderedDict([
    ('GameCategory_CTF', 2), ('GameCategory_Slayer', 8), ('GameCategory_Oddball', 8), ('GameCategory_KOTH', 8),
    ('GameCategory_Juggernaut', 8), ('GameCategory_Infection', 2), ('GameCategory_Race', 2), ('GameCategory_VIP', 8),
    ('GameCategory_Territories', 8), ('GameCategory_Assault', 2), ('GameCategory_Headhunter', 8)])
GAME_CATEGORIES = tuple(DEFAULT_MAX_TEAMS)                     # = Halo2.json "MultiplayerGameCategories" without the commented-out ones
DEFAULT_PRIMARY_WEAPON = '_damage_reporting_type_smg'          # Halo2.json "MapDefaultPrimaryWeapon"
STOCK_TEAM_OVERRIDES = {'foundation': {'GameCategory_CTF': 4, 'GameCategory_Assault': 4},
                        'waterworks': {'GameCategory_CTF': 4, 'GameCategory_Assault': 4},
                        'warlock': {'GameCategory_CTF': 4, 'GameCategory_Assault': 4}}
MAP_FLAGS = ('DisableSavedFilms',)
CAMPAIGN_MAP_KINDS = ('Default', 'Tutorial', 'Epilogue', 'Hidden')     # strings next to CampaignMapKind in ManagedUFL.dll; only 'Default' is seen in real packages
IMAGE_KEYS = ('Large', 'Thumbnail', 'LoadingScreen', 'TopDown')


def builtin_halo2(path=None):
    """Excession's own Halo 2 data (Data/BuiltInData/Halo2.json) as a dict, or None when Excession is not installed."""
    path = path or _paths.excession_halo2_json()
    if not path:
        return None
    try:
        with open(path, 'r', encoding='utf-8-sig') as fh:
            return json.loads(_strip_json_comments(fh.read()))
    except (OSError, ValueError):
        return None


def stock_team_overrides(name):
    """MaximumTeamsByGameCategory overrides of a stock Halo 2 map by its (internal) name, from Excession's built-in data (fallback table)."""
    b = builtin_halo2()
    if b:
        for m in b.get('MultiplayerMaps', []):
            if m.get('LegacyMapName', '').lower() == (name or '').lower():
                return dict(m.get('MaximumTeamsByGameCategory', {}))
        return {}
    return dict(STOCK_TEAM_OVERRIDES.get((name or '').lower(), {}))


def dumps(obj):
    """Excession's JSON formatting: UTF-8, LF, one TAB per level, `"key": value`, no trailing newline (verified byte-exact on the
    user's ModInfo.json / CampaignInfo.json / multiplayer\\*.json / campaign\\*.json)."""
    return json.dumps(obj, indent='\t', ensure_ascii=False, separators=(',', ': '))


def _localized(text):
    return collections.OrderedDict([('Neutral', text)])


def _images(images=None):
    d = collections.OrderedDict((k, '') for k in IMAGE_KEYS)
    if images:
        d.update(images)
    return d


def mp_mapinfo(guid, scenario_file, title, description=None, *, flags=MAP_FLAGS, images=None, insertion_points=None,
               max_teams=None, weapon=DEFAULT_PRIMARY_WEAPON, weapon_forge=None, stock_name=None):
    """multiplayer\\<stem>.json - key order and defaults exactly as Excession writes them."""
    teams = collections.OrderedDict(DEFAULT_MAX_TEAMS)
    if stock_name:
        teams.update(stock_team_overrides(stock_name))
    if max_teams:
        teams.update(max_teams)
    d = collections.OrderedDict()
    d['MapGuid'] = guid
    d['ScenarioFile'] = scenario_file
    d['Title'] = _localized(title)
    if description is not None:
        d['Description'] = _localized(description)
    d['Images'] = _images(images)
    d['Flags'] = list(flags)
    d['InsertionPoints'] = list(insertion_points or [])
    d['MaximumTeamsByGameCategory'] = teams
    d['MapDefaultPrimaryWeapon'] = weapon
    d['MapDefaultPrimaryWeaponForge'] = weapon_forge or weapon
    return d


def campaign_mapinfo(guid, scenario_file, title, description=None, *, flags=MAP_FLAGS, images=None, insertion_points=None,
                     kind='Default', non_scoring=False, segments=0, par=0.0, average=0.0, maximum=0.0):
    """campaign\\<stem>.json."""
    d = collections.OrderedDict()
    d['MapGuid'] = guid
    d['ScenarioFile'] = scenario_file
    d['Title'] = _localized(title)
    if description is not None:
        d['Description'] = _localized(description)
    d['Images'] = _images(images)
    d['Flags'] = list(flags)
    d['InsertionPoints'] = list(insertion_points or [])
    d['CampaignMapKind'] = kind
    d['CampaignMetagame'] = collections.OrderedDict([
        ('IsNonScoring', bool(non_scoring)), ('MissionSegmentCount', int(segments)), ('ParTimeInSeconds', float(par)),
        ('AverageTimeInSeconds', float(average)), ('MaxTimeInSeconds', float(maximum))])
    return d


def main(argv=None):
    """py -3 -m h2conv.mapinfo <file-or-folder> [...]   print the classification of every map found."""
    import argparse
    ap = argparse.ArgumentParser(prog='h2conv.mapinfo', description='Classify Halo 2 .map files (Vista v8 / MCC v13 / invalid).')
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--json', action='store_true', help='print JSON records')
    ap.add_argument('--titles', action='store_true', help='also read the localized name / description of Vista maps')
    _paths.add_common_options(ap, mcc=True, assembly=True)
    a = ap.parse_args(argv)
    _paths.configure_from_args(a)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:                                # noqa: BLE001
            pass
    for p in a.paths:
        if not os.path.exists(p):
            print('ERROR: %s does not exist' % p)
            return 2
    recs = []
    for p in a.paths:
        recs.extend(scan_folder(p))
    for r in recs:
        if a.json:
            d = r.to_dict()
            if a.titles and r.fmt == 'vista':
                d['titles'] = read_titles(r.path, r)
            print(json.dumps(d))
        else:
            line = '%-9s %-18s %s  %s' % (r.status, r.code, r.label(), r.path)
            if r.reason:
                line += '   [%s]' % r.reason
            print(line)
            if a.titles and r.fmt == 'vista':
                print('          title: %r' % (read_titles(r.path, r),))
    print(collections.Counter((r.status, r.code) for r in recs))
    return 0


if __name__ == '__main__':
    sys.exit(main())
