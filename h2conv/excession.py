# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Excession mod package writer / validator for Halo 2 MCC maps (Vista -> MCC output).  NO Steam upload, ever.

Pure stdlib.

An Excession mod package is a folder (directly below one of Excession's root folders, e.g. `<root folder>\\<package>`):

    ModInfo.json            identity, versions, Engine "Halo2", GameModContents {HasCampaign, MultiplayerMaps: ["multiplayer/<stem>.json", ...]}
    CampaignInfo.json       (campaign only) CampaignGuid, Title, Description, CampaignMaps: ["campaign/<stem>.json", ...]
    maps\\<stem>.map         the MCC v13 maps (H2EK style: uncompressed, every tag local, raw data inside the map)
    multiplayer\\<stem>.json MapInfo of one multiplayer map  {MapGuid, ScenarioFile, Title, Images, Flags, InsertionPoints, ...}
    campaign\\<stem>.json    MapInfo of one campaign map     {... CampaignMapKind, CampaignMetagame}
    assets\\ / images\\      optional PNG/JPG referenced by the MapInfo `Images`
    .h2conv\\                this tool's state (manifest.json = provenance of every converted map: file NAME, size and SHA-256 of the source, never
                            its folder; backups\\, staging\\)

Rules this module implements:
  * ScenarioFile of an entry is the map's FILE STEM (single token, lower case, [a-z0-9_-], <= 31 chars) and equals the header
    internal name the reverse converter writes: every working package on this PC uses a ScenarioFile whose last component is the
    .map file stem (03_headlong, lighttest, arcology_trickground, levels/h24/level1/level1 -> level1.map ...) - never the
    scenario tag path of the header, which is NOT unique for community maps (82 of 408 distinct paths are shared).
  * MapGuid = uuid5(namespace, normalised scenario path + '|' + SHA-256 of the SOURCE map): re-running replaces the entry in place,
    two different files with the same scenario path get different GUIDs, an identical copy gets the same one (-> duplicate).
  * add_map is idempotent: same GUID -> same stem, JSON and .map replaced (user edits of the JSON are kept unless
    refresh_metadata); a different GUID that wants an already used stem gets `<stem>_<guid6>`.
  * ModInfo.json / CampaignInfo.json / the MapInfo JSONs are written byte-for-byte the way Excession writes them
    (tab indent, LF, no trailing newline) and unknown keys of an existing file are preserved.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL
except Exception:  # pragma: no cover
    pass

import collections
import datetime
import json
import ntpath
import os
import re
import shutil
import sys
import threading
import unicodedata
import uuid

from . import mapinfo as MI
from . import paths as _paths

OD = collections.OrderedDict



def __getattr__(name):
    """DEFAULT_PACKAGE_DIR: the package folder chosen with --mcc-out / settings.json "mcc_output_dir" (there is no built-in default; raises
    h2conv.paths.PathsError when none was chosen); kept as an attribute for the developer tools."""
    if name == 'DEFAULT_PACKAGE_DIR':
        return _paths.mcc_out_dir()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


DEFAULT_TITLE = 'Converted Vista Maps'
DEFAULT_DESCRIPTION = ('Halo 2 Vista (Project Cartographer) community maps converted to Halo 2 MCC by h2conv, to keep the '
                       'Vista map archive playable. Local use only - not meant for the Steam Workshop.')
GUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, 'h2conv.halo2.vista-to-mcc.mapguid')
STEM_MAX = 31                       # header internal name is char[32]; Cartographer / MCC both cut longer stems
GUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
RESERVED = {'con', 'prn', 'aux', 'nul', 'clock$'} | set('com%d' % i for i in range(1, 10)) | set('lpt%d' % i for i in range(1, 10))
ENGINES = ('Halo1', 'Halo2', 'Halo2A', 'Halo3', 'Halo3ODST', 'Halo4', 'HaloReach')       # Data\BuiltInData\*.json of Excession
INHERIT_VALUES = ('FromMCC', 'FromExternalMod', 'Disabled')                              # strings in ManagedUFL.dll ("Disabled" per the docs)
STATE_DIR = '.h2conv'
TOOL_NAME = 'h2conv-rev'

# play order of the stock Halo 2 campaign (Excession Halo2.json "CampaignMaps")
STOCK_CAMPAIGN_ORDER = ['00a_introduction', '01a_tutorial', '01b_spacestation', '03a_oldmombasa', '03b_newmombasa', '04a_gasgiant',
                        '04b_floodlab', '05a_deltaapproach', '05b_deltatowers', '06a_sentinelwalls', '06b_floodzone',
                        '07a_highcharity', '08a_deltacliffs', '07b_forerunnership', '08b_deltacontrol']


class PackageError(Exception):
    pass


MANIFEST_VERSION = 2          # 1: 'source' held the absolute path of the source map;  2: 'source' is the file name only


def source_name(value):
    """The file name of a source path (either kind of separator).  The package manifest keeps names, never folders, so that a package that is
    shared or backed up does not carry the folder layout (or the Windows account name) of the PC it was built on."""
    if not isinstance(value, str):
        return value
    return ntpath.basename(value.replace('/', '\\'))


def scrub_provenance(prov):
    """A copy of a provenance record in which 'source' is a file name.  Everything else is kept as it is."""
    out = OD()
    for k, v in (prov or {}).items():
        out[k] = source_name(v) if k == 'source' else v
    return out


# ======================================================================================================
# identifiers
# ======================================================================================================
def derive_map_guid(scenario_path, source_sha256):
    """Stable MapGuid of a converted map: uuid5 over the normalised scenario path and the SHA-256 of the source (Vista) map."""
    return str(uuid.uuid5(GUID_NAMESPACE, '%s|%s' % (MI.norm_scenario(scenario_path), (source_sha256 or '').lower())))


def new_guid():
    return str(uuid.uuid4())


def is_guid(s):
    return isinstance(s, str) and bool(GUID_RE.match(s))


def sanitize_stem(name, maxlen=STEM_MAX):
    """File stem / ScenarioFile / header name for a map: ASCII, lower case, [a-z0-9_-], at most `maxlen` characters (a hash of the
    original name keeps truncated names distinct).  'dam_defense(OLD)' -> 'dam_defense_old', 'h2.5 arbitration' -> 'h2_5_arbitration'."""
    s = unicodedata.normalize('NFKD', name or '').encode('ascii', 'ignore').decode('ascii').lower()
    s = re.sub(r'[^a-z0-9_]+', '_', s)
    s = re.sub(r'_+', '_', s).strip('_')
    if not s:
        s = 'map'
    if len(s) > maxlen:
        import hashlib
        h = hashlib.sha1((name or '').encode('utf-8')).hexdigest()[:6]
        s = s[:maxlen - 7].rstrip('_-') + '_' + h
    if s in RESERVED:
        s += '_'
    return s


def _stem_candidates(base, guid, maxlen=STEM_MAX):
    """base, base_<guid6>, base_<guid8>, ... (each cut so that the suffix survives)."""
    yield base
    g = (guid or '').replace('-', '')
    for n in (6, 8, 12, 20, 32):
        suf = '_' + g[:n]
        yield base[:max(1, maxlen - len(suf))].rstrip('_-') + suf


# ======================================================================================================
# small io helpers
# ======================================================================================================
def read_json(path):
    """-> OrderedDict (key order preserved).  Raises PackageError with Excession's wording on a parse failure."""
    try:
        with open(path, 'r', encoding='utf-8-sig') as fh:
            return json.load(fh, object_pairs_hook=OD)
    except ValueError as ex:
        raise PackageError("Failed to parse JSON file '%s': %s" % (path, ex))
    except OSError as ex:
        raise PackageError("Failed to load JSON file '%s': %s" % (path, ex))


def write_text_atomic(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = '%s.%d.tmp' % (path, os.getpid())
    with open(tmp, 'w', encoding='utf-8', newline='') as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_json_atomic(path, obj):
    write_text_atomic(path, MI.dumps(obj))


def _posix(rel):
    return rel.replace('\\', '/')


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


class Issue(object):
    """level 'error' (Excession drops the mod / entry or logs an error) | 'warning' | 'info'.
    source 'excession' = Excession itself reports it (log line or message string in ManagedUFL.dll/Excession.dll),
           'game' = the game would fail to use the map, 'hygiene' = package tidiness."""
    __slots__ = ('level', 'code', 'message', 'path', 'source')

    def __init__(self, level, code, message, path='', source='excession'):
        self.level, self.code, self.message, self.path, self.source = level, code, message, path, source

    def __str__(self):
        return '%-7s %-22s %s%s' % (self.level.upper(), self.code, self.message, ('  [%s]' % self.path) if self.path else '')

    def to_dict(self):
        return {'level': self.level, 'code': self.code, 'message': self.message, 'path': self.path, 'source': self.source}

    def __repr__(self):
        return '<Issue %s>' % self


# ======================================================================================================
# templates
# ======================================================================================================
def new_modinfo(title, description, mod_guid, *, engine='Halo2', version=(1, 0, 0), app_min=(0, 0, 0), app_max=(0, 0, 0),
                has_campaign=False, mp_maps=()):
    """ModInfo.json as Excession writes it (key order of working Excession packages).  app_min/app_max (0,0,0) = any game version
    (the published packages use that; the local ones use 1.3385.0 .. 1.3528.0 = Excession's own range)."""
    def ver(t):
        return OD([('Major', t[0]), ('Minor', t[1]), ('Patch', t[2])])
    d = OD()
    d['ModIdentifier'] = OD([('ModGuid', mod_guid)])
    d['ModVersion'] = ver(version)
    d['MinAppVersion'] = ver(app_min)
    d['MaxAppVersion'] = ver(app_max)
    d['Engine'] = engine
    d['Title'] = OD([('Neutral', title)])
    d['Description'] = OD([('Neutral', description)])
    d['InheritSharedFiles'] = 'FromMCC'
    d['ModContents'] = OD([('HasBackgroundVideos', False), ('HasNameplates', False)])
    d['GameModContents'] = OD([('HasSharedFiles', False), ('HasCampaign', bool(has_campaign)), ('HasSpartanOps', False),
                               ('HasController', False), ('MultiplayerMaps', list(mp_maps))])
    return d


def new_campaigninfo(title, description, campaign_guid, maps=()):
    d = OD()
    d['CampaignGuid'] = campaign_guid
    d['Title'] = OD([('Neutral', title)])
    d['Description'] = OD([('Neutral', description)])
    d['CampaignMaps'] = list(maps)
    return d


# ======================================================================================================
# entries
# ======================================================================================================
class MapEntry(object):
    """One MapInfo JSON of the package."""

    def __init__(self, kind, rel, data):
        self.kind = kind                              # 'multiplayer' | 'campaign'
        self.rel = rel                                # 'multiplayer/<stem>.json'
        self.data = data                              # OrderedDict (parsed JSON, written back)

    @property
    def stem(self):
        return os.path.splitext(os.path.basename(self.rel))[0]

    @property
    def guid(self):
        return self.data.get('MapGuid')

    @property
    def scenario_file(self):
        return self.data.get('ScenarioFile')

    @property
    def title(self):
        t = self.data.get('Title')
        return (t.get('Neutral') if isinstance(t, dict) else None) or ''

    def __repr__(self):
        return '<MapEntry %s %s guid=%s>' % (self.kind, self.rel, self.guid)


class ModPackage(object):
    """A mod folder.  `ModPackage(folder)` opens it (and creates the skeleton when missing and create=True)."""

    def __init__(self, folder, create=True, *, title=DEFAULT_TITLE, description=DEFAULT_DESCRIPTION, engine='Halo2',
                 mod_guid=None, campaign_guid=None, version=(1, 0, 0), app_min=(0, 0, 0), app_max=(0, 0, 0)):
        self.root = os.path.abspath(folder)
        self._campaign_guid = campaign_guid
        self.created = False
        self.modinfo = None
        self.campaign = None
        self.entries = []
        self.manifest = {'version': MANIFEST_VERSION, 'entries': OD()}
        self.manifest_migrated = False                # True: an old manifest was read and its absolute source paths were reduced to file names
        self._backed_up = set()
        self._mtimes = {}                             # control file -> mtime_ns right after OUR last write (to notice outside edits)
        self._lock = threading.RLock()
        self.is_virtual = False                          # True = in-memory only (dry runs): nothing is ever written
        self._reserved = {}                           # guid -> stem handed out by plan_stem(reserve=True) but not added yet
        info = os.path.join(self.root, 'ModInfo.json')
        if os.path.isfile(info):
            self.modinfo = read_json(info)
            if not isinstance(self.modinfo, dict):
                raise PackageError("Invalid data in '%s'" % info)
        elif create:
            os.makedirs(self.root, exist_ok=True)
            self.modinfo = new_modinfo(title, description, mod_guid or new_guid(), engine=engine, version=version,
                                       app_min=app_min, app_max=app_max)
            self.created = True
            write_json_atomic(info, self.modinfo)
            self._mtimes[info] = os.stat(info).st_mtime_ns
        else:
            raise PackageError("'%s' does not contain a ModInfo.json" % self.root)
        for sub in ('maps', 'multiplayer'):
            if create:
                os.makedirs(os.path.join(self.root, sub), exist_ok=True)
        self._load_campaign()
        self._load_entries()
        self._load_manifest()

    @classmethod
    def virtual(cls, folder, title=DEFAULT_TITLE, description=DEFAULT_DESCRIPTION, engine='Halo2'):
        """An in-memory, never-written package for dry runs (plans names against an empty folder)."""
        self = cls.__new__(cls)
        self.root = os.path.abspath(folder)
        self._campaign_guid = None
        self.created = False
        self.modinfo = new_modinfo(title, description, new_guid(), engine=engine)
        self.campaign = None
        self.entries = []
        self.manifest = {'version': MANIFEST_VERSION, 'entries': OD()}
        self.manifest_migrated = False
        self._backed_up = set()
        self._mtimes = {}
        self._lock = threading.RLock()
        self.is_virtual = True
        self._reserved = {}
        return self

    # -- paths ----------------------------------------------------------------------------------------
    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def map_path(self, stem):
        return self.path('maps', stem + '.map')

    def json_rel(self, kind, stem):
        return '%s/%s.json' % (kind, stem)

    def staging_path(self, stem):
        """Where a converter should write its output before it is committed with add_map(map_file=...)."""
        d = self.path(STATE_DIR, 'staging')
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, stem + '.map')

    # -- loading ----------------------------------------------------------------------------------------
    @property
    def mod_guid(self):
        return (self.modinfo.get('ModIdentifier') or {}).get('ModGuid')

    def _game(self):
        g = self.modinfo.setdefault('GameModContents', OD())
        return g

    def _load_campaign(self):
        p = self.path('CampaignInfo.json')
        self.campaign = read_json(p) if os.path.isfile(p) else None

    def _list_rels(self, kind):
        if kind == 'multiplayer':
            return list(self._game().get('MultiplayerMaps') or [])
        return list((self.campaign or {}).get('CampaignMaps') or [])

    def _load_entries(self):
        self.entries = []
        for kind in ('multiplayer', 'campaign'):
            for rel in self._list_rels(kind):
                p = self.path(*_posix(rel).split('/'))
                if os.path.isfile(p):
                    try:
                        self.entries.append(MapEntry(kind, _posix(rel), read_json(p)))
                    except PackageError:
                        pass                                    # reported by validate()

    def _load_manifest(self):
        p = self.path(STATE_DIR, 'manifest.json')
        if os.path.isfile(p):
            try:
                m = read_json(p)
                if isinstance(m.get('entries'), dict):
                    self.manifest = m
            except PackageError:
                pass
        # a manifest written by an earlier version holds the absolute path of every source map: keep only the file name (in memory now, on disk with the
        # next save(); `save_manifest_if_migrated` writes it at once)
        for rec in self.manifest['entries'].values():
            if isinstance(rec, dict) and isinstance(rec.get('source'), str) and source_name(rec['source']) != rec['source']:
                rec['source'] = source_name(rec['source'])
                self.manifest_migrated = True
        if self.manifest_migrated:
            self.manifest['version'] = MANIFEST_VERSION

    def save_manifest_if_migrated(self):
        """Write the manifest now when an old one was migrated on load (so a package gets clean even when nothing is converted into it).
        No backup copy is made on purpose: the old file holds exactly the paths that are being removed.  True = it was written."""
        if self.is_virtual or not self.manifest_migrated:
            return False
        write_json_atomic(self.path(STATE_DIR, 'manifest.json'), self.manifest)
        self.manifest_migrated = False
        return True

    # -- queries ----------------------------------------------------------------------------------------
    def find(self, guid):
        for e in self.entries:
            if e.guid == guid:
                return e
        return None

    def find_by_stem(self, stem):
        s = stem.lower()
        for e in self.entries:
            if e.stem.lower() == s:
                return e
        return None

    def used_stems(self):
        """Stems that may not be given to a new entry: entries (any kind), JSON files on disk, .map files on disk."""
        used = set(e.stem.lower() for e in self.entries)
        for sub, ext in (('multiplayer', '.json'), ('campaign', '.json'), ('maps', '.map')):
            try:
                for n in os.listdir(self.path(sub)):
                    if n.lower().endswith(ext):
                        used.add(n[:-len(ext)].lower())
            except OSError:
                pass
        return used

    def plan_stem(self, wanted, guid, reserve=False):
        """The stem `guid` gets: its existing one, else sanitize(wanted) or a guid-suffixed variant when taken.  reserve=True keeps the
        stem for this GUID until add_map() (dry runs and batches plan many maps before any of them is added)."""
        e = self.find(guid)
        if e is not None:
            return e.stem
        if guid in self._reserved:
            return self._reserved[guid]
        used = self.used_stems() | set(v.lower() for v in self._reserved.values())
        base = sanitize_stem(wanted)
        for cand in _stem_candidates(base, guid):
            if cand.lower() not in used:
                if reserve:
                    self._reserved[guid] = cand
                return cand
        raise PackageError('cannot find a free map name for %r' % wanted)

    def is_current(self, guid):
        """True when the package already holds the finished conversion for this GUID (JSON + .map of the recorded size)."""
        e = self.find(guid)
        m = self.manifest['entries'].get(guid)
        if e is None or not m:
            return False
        try:
            return os.path.getsize(self.map_path(e.stem)) == m.get('out_size')
        except OSError:
            return False

    # -- writing ----------------------------------------------------------------------------------------
    def _backup(self, p):
        """Keep a timestamped copy in .h2conv/backups of a control file we are about to overwrite when it was not written by us:
        a file that existed before this session, or one that was edited by someone else (e.g. in Excession) since our last write."""
        if not os.path.isfile(p):
            return
        mt = os.stat(p).st_mtime_ns
        if self._mtimes.get(p) == mt or (p, mt) in self._backed_up:
            return
        self._backed_up.add((p, mt))
        d = self.path(STATE_DIR, 'backups')
        os.makedirs(d, exist_ok=True)
        shutil.copy2(p, os.path.join(d, '%s.bak-%s' % (os.path.basename(p), datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))))

    def _write_control(self, p, obj):
        self._backup(p)
        write_json_atomic(p, obj)
        self._mtimes[p] = os.stat(p).st_mtime_ns

    def save(self):
        """Write ModInfo.json, CampaignInfo.json (when there are campaign maps) and the manifest; keeps both lists consistent."""
        if self.is_virtual:
            raise PackageError('virtual (dry-run) packages cannot be written')
        mp = [e.rel for e in self.entries if e.kind == 'multiplayer']
        cm = [e.rel for e in self.entries if e.kind == 'campaign']
        g = self._game()
        g['MultiplayerMaps'] = mp
        g['HasCampaign'] = bool(cm)
        self._write_control(self.path('ModInfo.json'), self.modinfo)
        if cm or self.campaign is not None:
            if self.campaign is None:
                self.campaign = new_campaigninfo(DEFAULT_TITLE, 'Campaign levels converted from Halo 2 Vista maps.',
                                                 self._campaign_guid or new_guid())
            self.campaign['CampaignMaps'] = cm
            self._write_control(self.path('CampaignInfo.json'), self.campaign)
        write_json_atomic(self.path(STATE_DIR, 'manifest.json'), self.manifest)
        self.manifest_migrated = False

    def add_map(self, kind, guid, stem, title, description=None, *, map_file=None, move=True, provenance=None,
                refresh_metadata=False, title_hint=None, **mapinfo_kwargs):
        """Add or replace the entry `guid`.  `stem` must come from plan_stem().  `map_file` (a finished MCC map) is moved (or copied)
        to maps/<stem>.map; None = the file is already there.  Returns the MapEntry.  Thread safe.
        `title_hint` (e.g. the source file name) is appended to the Title when another map of the package already shows that Title."""
        with self._lock:
            return self._add_map(kind, guid, stem, title, description, map_file, move, provenance, refresh_metadata, mapinfo_kwargs, title_hint)

    def unique_title(self, title, hint, guid, stem=None):
        """`title`, or `title (hint)` / `title (stem)` / `... #n` when another entry already shows that Title (the in-game list would otherwise
        hold several indistinguishable entries, e.g. four 'Dam Defense' versions).  `hint` is usually the source file name, `stem` the
        unique map name (used when the hint adds nothing, e.g. two files called chronopolis.map in different folders)."""
        used = set(x.title.strip().lower() for x in self.entries if x.guid != guid)
        if title.strip().lower() not in used:
            return title
        flat = lambda t: re.sub(r'[^a-z0-9]', '', (t or '').lower())                    # noqa: E731
        cands = []
        for h in (hint, stem):
            if h and flat(h) != flat(title) and ' (%s)' % h not in [c[len(c) - len(h) - 3:] for c in cands]:
                suffix = ' (%s)' % h
                cands.append(title[:max(8, MI.MAX_TITLE - len(suffix))].rstrip() + suffix)
        if not cands:
            cands = [title]
        for c in cands:
            if c.strip().lower() not in used:
                return c
        n = 2
        while True:
            c = '%s #%d' % (cands[-1], n)
            if c.strip().lower() not in used:
                return c
            n += 1

    def _add_map(self, kind, guid, stem, title, description, map_file, move, provenance, refresh_metadata, mapinfo_kwargs, title_hint=None):
        if self.is_virtual:
            raise PackageError('virtual (dry-run) packages cannot be written')
        if kind not in ('multiplayer', 'campaign'):
            raise PackageError('kind must be multiplayer or campaign, not %r' % kind)
        if not is_guid(guid):
            raise PackageError('not a GUID: %r' % guid)
        if sanitize_stem(stem) != stem:
            raise PackageError('stem %r is not a valid map name (use sanitize_stem / plan_stem)' % stem)
        e = self.find(guid)
        if e is not None and e.stem != stem:
            raise PackageError('GUID %s already has the stem %r, not %r' % (guid, e.stem, stem))
        if e is None:
            other = self.find_by_stem(stem)
            if other is not None:
                raise PackageError('stem %r is already used by %s' % (stem, other.guid))
        if e is not None and e.kind != kind:                       # the same map listed under the other kind: move it
            try:
                os.remove(self.path(*e.rel.split('/')))
            except OSError:
                pass
            self.remove_map(guid, delete_files=False)
            e = None
        # 1. the map file first: a half-added map is never listed
        dst = self.map_path(stem)
        if map_file is not None:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.abspath(map_file) != os.path.abspath(dst):
                if move:
                    try:
                        os.replace(map_file, dst)
                    except OSError:
                        shutil.copyfile(map_file, dst + '.part')
                        os.replace(dst + '.part', dst)
                        os.remove(map_file)
                else:
                    shutil.copyfile(map_file, dst + '.part')
                    os.replace(dst + '.part', dst)
        # 2. the MapInfo JSON
        rel = self.json_rel(kind, stem)
        jpath = self.path(*rel.split('/'))
        if e is not None and not refresh_metadata:
            data = e.data
            data['MapGuid'] = guid
            data['ScenarioFile'] = stem
        else:
            title = self.unique_title(title, title_hint, guid, stem)
            if kind == 'multiplayer':
                data = MI.mp_mapinfo(guid, stem, title, description, **mapinfo_kwargs)
            else:
                data = MI.campaign_mapinfo(guid, stem, title, description, **mapinfo_kwargs)
        write_json_atomic(jpath, data)
        if e is None:
            e = MapEntry(kind, rel, data)
            self.entries.append(e)
        else:
            e.data = data
        # 3. provenance + lists
        rec = OD()
        rec['kind'] = kind
        rec['stem'] = stem
        rec.update(scrub_provenance(provenance))
        try:
            rec['out_size'] = os.path.getsize(dst)
        except OSError:
            rec['out_size'] = None
        rec['updated'] = _now()
        self.manifest['entries'][guid] = rec
        self._reserved.pop(guid, None)
        self.save()
        return e

    def remove_map(self, guid, delete_files=True):
        e = self.find(guid)
        if e is None:
            return False
        self.entries.remove(e)
        if delete_files:
            for p in (self.path(*e.rel.split('/')), self.map_path(e.stem)):
                try:
                    os.remove(p)
                except OSError:
                    pass
        self.manifest['entries'].pop(guid, None)
        self.save()
        return True

    def sort_maps(self, key=None):
        """Order the lists: multiplayer by (title, stem); campaign by the stock play order, then by stem.  Does not save."""
        def mp_key(e):
            return (e.title.lower(), e.stem)

        def cm_key(e):
            s = e.stem.lower()
            return (STOCK_CAMPAIGN_ORDER.index(s) if s in STOCK_CAMPAIGN_ORDER else len(STOCK_CAMPAIGN_ORDER), s)
        mp = sorted([e for e in self.entries if e.kind == 'multiplayer'], key=key or mp_key)
        cm = sorted([e for e in self.entries if e.kind == 'campaign'], key=cm_key)
        self.entries = mp + cm

    def sync(self):
        """Make ModInfo.json / CampaignInfo.json consistent with the files: drop list entries without a MapInfo JSON, list MapInfo JSONs
        that are not listed.  Returns {'dropped': [...], 'adopted': [...]} and saves."""
        dropped, adopted = [], []
        listed = {}
        for kind in ('multiplayer', 'campaign'):
            for rel in self._list_rels(kind):
                listed[_posix(rel)] = kind
        keep = []
        for e in self.entries:
            keep.append(e)
        for rel, kind in listed.items():
            if not os.path.isfile(self.path(*rel.split('/'))):
                dropped.append(rel)
        have = set(e.rel for e in keep)
        for kind in ('multiplayer', 'campaign'):
            d = self.path(kind)
            if not os.path.isdir(d):
                continue
            for n in sorted(os.listdir(d)):
                rel = '%s/%s' % (kind, n)
                if n.lower().endswith('.json') and rel not in have:
                    try:
                        data = read_json(self.path(kind, n))
                    except PackageError:
                        continue
                    if is_guid(data.get('MapGuid')):
                        keep.append(MapEntry(kind, rel, data))
                        adopted.append(rel)
        self.entries = keep
        self.save()
        return {'dropped': dropped, 'adopted': adopted}

    # -- validation ---------------------------------------------------------------------------------------
    def validate(self, siblings=True):
        return validate_package(self.root, siblings=siblings)

    def summary(self):
        n_mp = sum(1 for e in self.entries if e.kind == 'multiplayer')
        n_cm = len(self.entries) - n_mp
        total = 0
        for e in self.entries:
            try:
                total += os.path.getsize(self.map_path(e.stem))
            except OSError:
                pass
        return {'root': self.root, 'title': (self.modinfo.get('Title') or {}).get('Neutral'), 'mod_guid': self.mod_guid,
                'multiplayer_maps': n_mp, 'campaign_maps': n_cm, 'map_bytes': total}


# ======================================================================================================
# validation (mirror of Excession / ManagedUFL.dll)
# ======================================================================================================
def _version_ok(v):
    return isinstance(v, dict) and all(isinstance(v.get(k), int) and not isinstance(v.get(k), bool) and v.get(k) >= 0
                                       for k in ('Major', 'Minor', 'Patch'))


def _vt(v):
    return (v.get('Major', 0), v.get('Minor', 0), v.get('Patch', 0))


def _safe_rel(rel):
    """None if `rel` is a usable relative path inside the package, else the reason."""
    if not isinstance(rel, str) or not rel.strip():
        return 'empty path'
    if rel.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', rel):
        return 'absolute path'
    if '..' in re.split(r'[\\/]', rel):
        return "path leaves the package ('..')"
    return None


def _check_images(issues, root, data, jrel):
    imgs = data.get('Images')
    if imgs is None:
        return
    if not isinstance(imgs, dict):
        issues.append(Issue('error', 'images_type', 'Images must be an object', jrel))
        return
    for k, v in imgs.items():
        if k not in MI.IMAGE_KEYS:
            issues.append(Issue('warning', 'images_key', 'unknown Images key %r' % k, jrel, 'hygiene'))
        if not v:
            continue
        bad = _safe_rel(v)
        if bad:
            issues.append(Issue('error', 'image_path', 'Images.%s: %s (%r) - images must live inside the mod folder' % (k, bad, v), jrel))
            continue
        p = os.path.join(root, *_posix(v).split('/'))
        if not os.path.isfile(p):
            issues.append(Issue('error', 'image_missing', 'Images.%s: file %r does not exist' % (k, v), jrel))
        elif not v.lower().endswith(('.png', '.jpg', '.jpeg')):
            issues.append(Issue('warning', 'image_format', 'Images.%s: %r is not a .png/.jpg image' % (k, v), jrel))
        elif os.path.getsize(p) > 1 << 20:
            issues.append(Issue('warning', 'image_size', 'Images.%s: %s is larger than the recommended ~900 KB' % (k, v), jrel, 'hygiene'))


def _check_mapinfo(issues, root, kind, rel, data, engine, seen_guids, seen_stems, pkg_stems):
    jrel = rel
    stem = os.path.splitext(os.path.basename(rel))[0]
    g = data.get('MapGuid')
    if not is_guid(g):
        issues.append(Issue('error', 'map_guid', 'MapGuid %r is not a GUID' % (g,), jrel))
    elif g in seen_guids:
        issues.append(Issue('error', 'map_guid_dup', 'MapGuid %s is used by %s too' % (g, seen_guids[g]), jrel, 'game'))
    else:
        seen_guids[g] = jrel
    sf = data.get('ScenarioFile')
    if not isinstance(sf, str) or not sf.strip():
        issues.append(Issue('error', 'scenario_file', 'ScenarioFile is missing or empty', jrel))
        sf = ''
    if '\\' in sf:
        issues.append(Issue('warning', 'scenario_slash', 'ScenarioFile %r uses backslashes (Excession writes forward slashes)' % sf, jrel, 'hygiene'))
    last = re.split(r'[\\/]', sf)[-1] if sf else ''
    t = data.get('Title')
    if not (isinstance(t, dict) and isinstance(t.get('Neutral'), str) and t['Neutral'].strip()):
        issues.append(Issue('warning', 'title', 'Title.Neutral is missing or empty', jrel, 'hygiene'))
    d = data.get('Description')
    if d is not None and not (isinstance(d, dict) and isinstance(d.get('Neutral', ''), str)):
        issues.append(Issue('error', 'description', 'Description must be an object with a Neutral string', jrel))
    _check_images(issues, root, data, jrel)
    fl = data.get('Flags')
    if fl is not None:
        if not isinstance(fl, list):
            issues.append(Issue('error', 'flags_type', 'Flags must be a list', jrel))
        else:
            for f in fl:
                if f not in MI.MAP_FLAGS:
                    issues.append(Issue('warning', 'flag_unknown', 'unknown map flag %r (known: %s)' % (f, ', '.join(MI.MAP_FLAGS)), jrel))
    ip = data.get('InsertionPoints')
    if ip is not None and not isinstance(ip, list):
        issues.append(Issue('error', 'insertion_points', 'InsertionPoints must be a list', jrel))
    if kind == 'multiplayer' and engine == 'Halo2':
        mt = data.get('MaximumTeamsByGameCategory')
        if mt is not None:
            if not isinstance(mt, dict):
                issues.append(Issue('error', 'max_teams_type', 'MaximumTeamsByGameCategory must be an object', jrel))
            else:
                for k, v in mt.items():
                    if k not in MI.GAME_CATEGORIES:
                        issues.append(Issue('error', 'max_teams_key', 'MaximumTeamsByGameCategory: %r is not a Halo 2 game category' % k, jrel))
                    elif not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 8:
                        issues.append(Issue('error', 'max_teams_value', 'Invalid max teams value %r for %s (1..8)' % (v, k), jrel))
        for key in ('MapDefaultPrimaryWeapon', 'MapDefaultPrimaryWeaponForge'):
            w = data.get(key)
            if w is not None and not (isinstance(w, str) and w.startswith('_damage_reporting_type_')):
                issues.append(Issue('error', 'weapon', '%s %r is not a _damage_reporting_type_* value' % (key, w), jrel))
    if kind == 'campaign':
        k = data.get('CampaignMapKind', 'Default')
        if k not in MI.CAMPAIGN_MAP_KINDS:
            issues.append(Issue('warning', 'campaign_kind', 'CampaignMapKind %r (known: %s)' % (k, ', '.join(MI.CAMPAIGN_MAP_KINDS)), jrel))
        m = data.get('CampaignMetagame')
        if isinstance(m, dict):
            par, avg, mx = (m.get(x, 0.0) or 0.0 for x in ('ParTimeInSeconds', 'AverageTimeInSeconds', 'MaxTimeInSeconds'))
            if avg < par:
                issues.append(Issue('error', 'metagame_time', 'AverageTimeInSeconds must be greater than or equal to ParTimeInSeconds!', jrel))
            if mx < avg:
                issues.append(Issue('error', 'metagame_time', 'MaxTimeInSeconds must be greater than or equal to AverageTimeInSeconds!', jrel))
        elif m is not None:
            issues.append(Issue('error', 'metagame_type', 'CampaignMetagame must be an object', jrel))
    # ---- the map file (Excession does not look at it; the game needs it) ------------------------------------------------------
    if last:
        if last.lower() != stem.lower():
            issues.append(Issue('warning', 'stem_mismatch', "last component of ScenarioFile ('%s') differs from the JSON name ('%s')" % (last, stem), jrel, 'game'))
        if last.lower() in seen_stems and seen_stems[last.lower()] != jrel:
            issues.append(Issue('error', 'scenario_dup', "two MapInfos resolve to the map name '%s': %s and %s" % (last, seen_stems[last.lower()], jrel), jrel, 'game'))
        seen_stems.setdefault(last.lower(), jrel)
        mp = os.path.join(root, 'maps', last + '.map')
        pkg_stems.add(last.lower())
        if not os.path.isfile(mp):
            issues.append(Issue('error', 'map_missing', "maps/%s.map does not exist (the game cannot load this entry)" % last, jrel, 'game'))
        elif engine == 'Halo2':
            r = MI.identify(mp, deep=True)
            if r.status == 'invalid':
                issues.append(Issue('error', 'map_invalid', 'maps/%s.map is not a usable cache file: %s' % (last, r.reason), jrel, 'game'))
            elif r.fmt == 'vista':
                issues.append(Issue('error', 'map_is_vista', 'maps/%s.map is a Halo 2 Vista (v8) map; MCC needs version 13' % last, jrel, 'game'))
            elif r.fmt == 'mcc':
                if r.custom_shared and not r.custom_shared_dir:
                    issues.append(Issue('error', 'map_custom_shared', 'maps/%s.map needs a custom shared.map that is not next to it (unsupported)' % last, jrel, 'game'))
                if r.compressed:
                    issues.append(Issue('info', 'map_compressed', 'maps/%s.map is chunk-compressed (fine for MCC, H2EK maps are not)' % last, jrel, 'hygiene'))
                want = 'multiplayer' if kind == 'multiplayer' else 'solo'
                if r.kind != want:
                    issues.append(Issue('warning', 'map_type', 'maps/%s.map has scenario type %r but is listed as %s' % (last, r.kind, kind), jrel, 'game'))
                if r.name and r.name.lower() != last.lower():
                    issues.append(Issue('warning', 'map_name', "header internal name '%s' differs from the map name '%s'" % (r.name, last), jrel, 'game'))
                if len(last) > STEM_MAX:
                    issues.append(Issue('warning', 'stem_long', "map name '%s' is longer than %d characters" % (last, STEM_MAX), jrel, 'game'))


def validate_package(folder, siblings=True):
    """Mirror of what Excession (ManagedUFL.dll mod scanner) checks, plus the checks the game needs.  Returns [Issue].
    `siblings`: also look at the neighbouring mod folders for a ModGuid clash."""
    folder = os.path.abspath(folder)
    issues = []
    info_path = os.path.join(folder, 'ModInfo.json')
    if not os.path.isfile(info_path):
        return [Issue('error', 'no_modinfo', "Expected the directory '%s' to contain a 'ModInfo.json' file, so it is being skipped"
                      % folder.replace('\\', '/'), folder)]
    try:
        mi = read_json(info_path)
    except PackageError as ex:
        return [Issue('error', 'modinfo_parse', str(ex), info_path)]
    if not isinstance(mi, dict):
        return [Issue('error', 'modinfo_parse', "Invalid data in '%s'" % info_path, info_path)]
    ident = mi.get('ModIdentifier')
    guid = ident.get('ModGuid') if isinstance(ident, dict) else None
    if not is_guid(guid):
        issues.append(Issue('error', 'mod_guid', 'ModIdentifier.ModGuid %r is not a GUID' % (guid,), info_path))
    engine = mi.get('Engine')
    if engine not in ENGINES:
        issues.append(Issue('error', 'engine', 'Invalid engine value %r (one of %s)' % (engine, ', '.join(ENGINES)), info_path))
    for k in ('ModVersion', 'MinAppVersion', 'MaxAppVersion'):
        if not _version_ok(mi.get(k)):
            issues.append(Issue('error', 'version', '%s must be {Major, Minor, Patch} non-negative integers' % k, info_path))
    if _version_ok(mi.get('MinAppVersion')) and _version_ok(mi.get('MaxAppVersion')):
        lo, hi = _vt(mi['MinAppVersion']), _vt(mi['MaxAppVersion'])
        if hi != (0, 0, 0) and lo > hi:
            issues.append(Issue('error', 'version_range', 'MinAppVersion %s is greater than MaxAppVersion %s' % ('.'.join(map(str, lo)), '.'.join(map(str, hi))), info_path))
    t = mi.get('Title')
    if not (isinstance(t, dict) and isinstance(t.get('Neutral'), str) and t['Neutral'].strip()):
        issues.append(Issue('warning', 'title', 'Title.Neutral is missing or empty', info_path, 'hygiene'))
    inh = mi.get('InheritSharedFiles')
    if inh is not None and inh not in INHERIT_VALUES:
        issues.append(Issue('warning', 'inherit', 'InheritSharedFiles %r (known: %s)' % (inh, ', '.join(INHERIT_VALUES)), info_path))
    gm = mi.get('GameModContents')
    if not isinstance(gm, dict):
        issues.append(Issue('error', 'game_contents', 'GameModContents is missing', info_path))
        gm = {}
    # ---- sibling GUID clash ---------------------------------------------------------------------------------------------------
    if siblings and is_guid(guid):
        parent = os.path.dirname(folder)
        try:
            for n in sorted(os.listdir(parent)):
                p = os.path.join(parent, n)
                if os.path.normcase(p) == os.path.normcase(folder) or not os.path.isfile(os.path.join(p, 'ModInfo.json')):
                    continue
                try:
                    other = read_json(os.path.join(p, 'ModInfo.json'))
                    og = (other.get('ModIdentifier') or {}).get('ModGuid')
                except (PackageError, AttributeError):
                    continue
                if og == guid:
                    issues.append(Issue('error', 'mod_guid_dup', "ModGuid(%s) is already claimed by a mod with higher priority, ignoring '%s' (same GUID in %s)"
                                        % (guid, folder, p), info_path))
        except OSError:
            pass
    # ---- map lists ------------------------------------------------------------------------------------------------------------
    seen_guids, seen_stems, pkg_stems, listed_rel = {}, {}, set(), set()
    mp_list = gm.get('MultiplayerMaps')
    if mp_list is not None and not isinstance(mp_list, list):
        issues.append(Issue('error', 'mp_list', 'MultiplayerMaps must be a list', info_path))
        mp_list = []
    for rel in (mp_list or []):
        issues.extend(_resolve_entry(folder, 'Multiplayer', 'multiplayer', rel, engine, seen_guids, seen_stems, pkg_stems, listed_rel))
    has_campaign = bool(gm.get('HasCampaign'))
    cpath = os.path.join(folder, 'CampaignInfo.json')
    if has_campaign:
        if not os.path.isfile(cpath):
            issues.append(Issue('error', 'campaign_missing', "HasCampaign is true but CampaignInfo.json does not exist", info_path))
        else:
            try:
                ci = read_json(cpath)
            except PackageError as ex:
                issues.append(Issue('error', 'campaign_parse', str(ex), cpath))
                ci = None
            if ci is not None:
                if not is_guid(ci.get('CampaignGuid')):
                    issues.append(Issue('error', 'campaign_guid', 'CampaignGuid %r is not a GUID' % (ci.get('CampaignGuid'),), cpath))
                cl = ci.get('CampaignMaps')
                if not isinstance(cl, list) or not cl:
                    issues.append(Issue('warning', 'campaign_empty', 'Campaign info is being ignored, no maps are defined', cpath))
                    cl = []
                for rel in cl:
                    issues.extend(_resolve_entry(folder, 'Campaign', 'campaign', rel, engine, seen_guids, seen_stems, pkg_stems, listed_rel))
    elif os.path.isfile(cpath):
        issues.append(Issue('warning', 'campaign_unused', 'CampaignInfo.json exists but HasCampaign is false (the campaign is ignored)', cpath, 'hygiene'))
    # ---- orphans (tidiness; Excession never lists them) ------------------------------------------------------------------------
    for sub in ('multiplayer', 'campaign'):
        d = os.path.join(folder, sub)
        if os.path.isdir(d):
            for n in sorted(os.listdir(d)):
                rel = '%s/%s' % (sub, n)
                if n.lower().endswith('.json') and rel not in listed_rel:
                    issues.append(Issue('warning', 'orphan_json', '%s is not listed in ModInfo.json / CampaignInfo.json (Excession does not show it)' % rel,
                                        os.path.join(d, n), 'hygiene'))
    md = os.path.join(folder, 'maps')
    if os.path.isdir(md):
        for n in sorted(os.listdir(md)):
            if n.lower().endswith('.map') and n[:-4].lower() not in pkg_stems:
                issues.append(Issue('warning', 'orphan_map', 'maps/%s is not used by any MapInfo' % n, os.path.join(md, n), 'hygiene'))
    return issues


def _resolve_entry(folder, label, sub, rel, engine, seen_guids, seen_stems, pkg_stems, listed_rel):
    out = []
    if not isinstance(rel, str):
        return [Issue('error', 'entry_type', '%s map list entry %r is not a string' % (label, rel), folder)]
    bad = _safe_rel(rel)
    p = os.path.join(folder, *_posix(rel).split('/')) if not bad else None
    if bad or not os.path.isfile(p):
        return [Issue('error', 'unresolved', "Could not resolve %s MapInfo '%s'" % (label, rel), folder)]
    listed_rel.add(_posix(rel))
    if _posix(rel).split('/')[0] != sub:
        out.append(Issue('warning', 'entry_folder', "%s MapInfo '%s' is not below %s/" % (label, rel, sub), p, 'hygiene'))
    try:
        data = read_json(p)
    except PackageError as ex:
        return [Issue('error', 'mapinfo_parse', str(ex), p)]
    if not isinstance(data, dict):
        return [Issue('error', 'mapinfo_parse', "Invalid data in '%s'" % p, p)]
    _check_mapinfo(out, folder, 'multiplayer' if sub == 'multiplayer' else 'campaign', _posix(rel), data, engine, seen_guids, seen_stems, pkg_stems)
    return out


def errors_of(issues, source=None):
    return [i for i in issues if i.level == 'error' and (source is None or i.source == source)]


# ======================================================================================================
# a whole root folder (mirror of Excession.log "Enumerating mods in local file system")
# ======================================================================================================
def scan_mods_root(root):
    """Replays what Excession's ModManager does with a root folder (one level deep): returns a list of events
    ('skipped'|'error'|'loaded', folder, text, issues) in the wording of Excession.log."""
    events = []
    root = os.path.abspath(root)
    for n in sorted(os.listdir(root), key=lambda s: s.lower()):
        d = os.path.join(root, n)
        if not os.path.isdir(d):
            continue
        if not os.path.isfile(os.path.join(d, 'ModInfo.json')):
            events.append(('skipped', d, "Expected the directory '%s' to contain a 'ModInfo.json' file, so it is being skipped" % d.replace('\\', '/'), []))
            continue
        issues = validate_package(d, siblings=True)
        fatal = False
        for i in errors_of(issues, 'excession'):
            if i.code == 'unresolved':
                events.append(('error', d, i.message, [i]))
            else:
                events.append(('error', d, i.message, [i]))
                if i.code in ('modinfo_parse', 'mod_guid', 'mod_guid_dup', 'engine'):
                    fatal = True
        if not fatal:
            events.append(('loaded', d, 'Mod loaded: %s' % d, issues))
    return events


def parse_excession_log(path):
    """Events found in an Excession.log: {'skipped': {dir}, 'errors': [(message, guid, folder)], 'loaded': {dir}} - for the tests."""
    skipped, errors, loaded = set(), [], set()
    lines = open(path, 'r', encoding='utf-8-sig', errors='replace').read().splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"Excession Verbose: Expected the directory '(.+)' to contain a 'ModInfo.json' file, so it is being skipped", ln)
        if m:
            skipped.add(os.path.normcase(os.path.normpath(m.group(1))))
        m = re.match(r'Excession Error:\s+(Could not resolve .+)$', ln)
        if m:
            guid = lines[i + 1].strip() if i + 1 < len(lines) else ''
            folder = lines[i + 2].strip() if i + 2 < len(lines) else ''
            errors.append((m.group(1).strip(), guid, os.path.normcase(os.path.normpath(folder))))
        m = re.match(r'Excession Information: Mod loaded: (.+)$', ln)
        if m:
            loaded.add(os.path.normcase(os.path.normpath(m.group(1).strip())))
        i += 1
    return {'skipped': skipped, 'errors': errors, 'loaded': loaded}


# ======================================================================================================
# CLI
# ======================================================================================================
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='h2conv.excession', description='Inspect / validate / repair an Excession mod package (no Steam upload).')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('validate', help='validate one package folder (mirror of Excession\'s checks)')
    p.add_argument('package', nargs='?', default=None, help='the package folder (default: settings.json mcc_output_dir, which batch mode remembers)')
    p.add_argument('--no-siblings', action='store_true')
    p = sub.add_parser('check-root', help='replay Excession\'s scan of a root folder (a folder that holds mod packages)')
    p.add_argument('root')
    p = sub.add_parser('info', help='print the package summary')
    p.add_argument('package', nargs='?', default=None, help='the package folder (default: settings.json mcc_output_dir)')
    p = sub.add_parser('create', help='create an empty package (ModInfo.json + folders)')
    p.add_argument('package', nargs='?', default=None, help='the package folder (default: settings.json mcc_output_dir)')
    p.add_argument('--title', default=DEFAULT_TITLE)
    p.add_argument('--description', default=DEFAULT_DESCRIPTION)
    p = sub.add_parser('sync', help='make the map lists consistent with the JSON files on disk')
    p.add_argument('package', nargs='?', default=None, help='the package folder (default: settings.json mcc_output_dir)')
    p = sub.add_parser('remove', help='remove a map (by GUID or stem) and its files')
    p.add_argument('package')
    p.add_argument('map')
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:                                # noqa: BLE001
            pass
    if getattr(a, 'package', '') is None:                # no folder given: the one batch mode remembered (settings.json mcc_output_dir)
        a.package = _paths.mcc_out_dir(required=False)
        if not a.package:
            raise _paths.PathsError('no package folder given: pass the folder of the Excession mod package (a batch run with --mcc-out '
                                    'remembers it as "mcc_output_dir" in %s)' % _paths.settings_file())
    if a.cmd == 'check-root':
        if not os.path.isdir(a.root):
            print('ERROR: %s %s' % (a.root, 'is not a folder' if os.path.exists(a.root) else 'does not exist'))
            return 2
        ev = scan_mods_root(a.root)
        for kind, d, text, issues in ev:
            print('%-8s %s' % (kind.upper(), text))
        nerr = sum(1 for e in ev if e[0] == 'error')
        print('%d folders skipped, %d errors, %d mods loaded' % (sum(1 for e in ev if e[0] == 'skipped'), nerr, sum(1 for e in ev if e[0] == 'loaded')))
        return 1 if nerr else 0
    if a.cmd == 'validate':
        issues = validate_package(a.package, siblings=not a.no_siblings)
        for i in issues:
            print(i)
        ne = len(errors_of(issues))
        print('%d errors, %d warnings, %d notes' % (ne, sum(1 for i in issues if i.level == 'warning'), sum(1 for i in issues if i.level == 'info')))
        return 1 if ne else 0
    if a.cmd == 'create':
        pkg = ModPackage(a.package, create=True, title=a.title, description=a.description)
        print(('created ' if pkg.created else 'opened ') + pkg.root)
        print(json.dumps(pkg.summary(), indent=1))
        return 0
    try:
        pkg = ModPackage(a.package, create=False)
    except PackageError as ex:                          # no such folder / no ModInfo.json: a message, not a traceback
        print('ERROR: %s' % ex)
        return 2
    if a.cmd == 'info':
        print(json.dumps(pkg.summary(), indent=1))
        for e in pkg.entries:
            print('  %-11s %-34s %s  %s' % (e.kind, e.stem, e.guid, e.title))
        return 0
    if a.cmd == 'sync':
        print(json.dumps(pkg.sync(), indent=1))
        return 0
    if a.cmd == 'remove':
        e = pkg.find(a.map) or pkg.find_by_stem(a.map)
        if not e:
            print('no such map: %s' % a.map)
            return 2
        pkg.remove_map(e.guid)
        print('removed %s' % e.rel)
        return 0
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except _paths.PathsError as _ex:
        print('ERROR: %s' % _ex)
        sys.exit(2)
