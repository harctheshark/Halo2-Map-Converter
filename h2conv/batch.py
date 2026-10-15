# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Batch map conversion: scan a folder tree, classify every .map, convert each one in the chosen direction, list what failed.

A graphical front end can mirror this module: same classification, same options, same JSON result.  NEVER uploads anything.

    py -3 -m h2conv.batch scan   ROOT [--json]                       classify every .map below ROOT (recursive), no conversion
    py -3 -m h2conv.batch run    ROOT [--direction auto|vista2mcc|mcc2vista] [--mcc-out DIR] [--vista-out DIR] [--dry-run [--list] [--no-hash]] ...
    py -3 -m h2conv.batch settings [--show] [--set KEY=VALUE ...]    the remembered choices (%APPDATA%\\H2MapConverter\\settings.json)

--dry-run plans EXACTLY what the real run would do (same GUIDs, names, skips: it hashes every Vista source like the real run does);
--dry-run --no-hash is the explicit fast approximation: unhashed items are flagged provisional everywhere.

Directions
    vista2mcc  Halo 2 Vista (v8) -> Halo 2 MCC (v13): h2conv.rev.convert, output = ONE Excession mod package (--mcc-out, required
               the first time, remembered afterwards), maps\\<stem>.map + multiplayer\\<stem>.json
    mcc2vista  Halo 2 MCC (v13) -> Halo 2 Vista (v8): h2conv.convert.convert_map, output = the Halo 2 Maps folder of Cartographer
               (--vista-out; default <Documents>\\My Games\\Halo 2\\Maps when that game folder exists)
    auto       every convertible map goes the other way (Vista maps to the mod package, MCC maps to the Maps folder)

Per-map failures are caught, recorded and never stop the batch; the summary lists failed / skipped maps with reasons and the
output directories.  Skipped on purpose: shared / single_player_shared / mainmenu databases, MCC maps that need a CUSTOM
shared.map (unsupported), byte-identical duplicates, maps already converted (resume), outputs that already exist (mcc2vista).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL
except Exception:  # pragma: no cover
    pass

import collections
import concurrent.futures
import datetime
import fnmatch
import gc
import importlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

from . import excession as EX
from . import mapinfo as MI
from . import paths as _paths

OD = collections.OrderedDict
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000


def __getattr__(name):
    """DEFAULT_VISTA_OUT / DEFAULT_VISTA_MAPS: folders on one PC, now resolved by h2conv.paths; kept as attributes for the developer tools."""
    if name == 'DEFAULT_VISTA_OUT':
        return _paths.vista_out_dir()
    if name == 'DEFAULT_VISTA_MAPS':
        return _paths.vista_maps_dir()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


# fitted to the 484-map Vista to MCC run (14 stock maps of 17..41 MB gave 268..296 MB, 470 community maps of 3..355 MB gave 250..609 MB): an upper line
# for each group, out = base + k * source (no map of the run was larger), so the estimate is a little high rather than low
MCC_OUT_STOCK = (256 * 1048576, 1.19)
MCC_OUT_COMMUNITY = (289 * 1048576, 1.18)
STOCK_MAP_NAMES = ('00a_introduction', '01a_tutorial', '01b_spacestation', '03a_oldmombasa', '03b_newmombasa', '04a_gasgiant', '04b_floodlab',
                   '05a_deltaapproach', '05b_deltatowers', '06a_sentinelwalls', '06b_floodzone', '07a_highcharity', '07b_forerunnership',
                   '08a_deltacliffs', '08b_deltacontrol', 'zanzibar', 'ascension', 'backwash', 'beavercreek', 'burial_mounds', 'coagulation',
                   'colossus', 'foundation', 'headlong', 'cyclotron', 'lockout', 'midship', 'waterworks', 'containment', 'warlock', 'deltatap',
                   'turf', 'elongation', 'gemini', 'dune', 'triplicate', 'street_sweeper', 'needle', 'derelict', 'highplains')


def estimate_mcc_bytes(rec):
    base, k = MCC_OUT_STOCK if (rec.name or rec.stem).lower() in STOCK_MAP_NAMES else MCC_OUT_COMMUNITY
    return int(base + k * rec.size)

# result codes of skipped items that are NOT worth a warning in the summary
BENIGN_SKIPS = ('database', 'wrong_direction', 'up_to_date', 'duplicate', 'not_selected', 'limit', 'cancelled')

# the two output folders have no built-in value: the caller's own choice (or Cartographer's folder in Documents for the Vista maps)
NEED_MCC_OUT = ('Vista -> MCC needs an output folder (an Excession mod package) and none was chosen yet: pass --mcc-out <folder>.  '
                'Batch mode remembers it for the next runs.')
NEED_VISTA_OUT = ('MCC -> Vista needs the folder Project Cartographer reads custom maps from and none was found (<Documents>\\My Games\\Halo 2\\Maps '
                  'does not exist): pass --vista-out <folder>.  Batch mode remembers it for the next runs.')


# ======================================================================================================
# settings  (%APPDATA%\H2MapConverter\settings.json)
# ======================================================================================================
def settings_dir():
    return _paths.settings_dir()


class Settings(object):
    """Remembered choices.  Corrupt / missing file -> defaults.  Never raises on load; save() reports failure by returning False.
    The same file holds the folders h2conv.paths reads (mcc_maps_dir, vista_maps_dir, assembly_dir, work_dir, h2ek_maps_dir, excession_dir).
    Keys this version does not know are kept when the file is saved again."""
    DEFAULTS = OD([
        ('version', 1),
        ('mcc_output_dir', None),                          # Excession mod folder = output of Vista -> MCC (no built-in default: --mcc-out the first time)
        ('vista_output_dir', None),                        # Halo 2 Maps folder = output of MCC -> Vista (None = <Documents>\My Games\Halo 2\Maps)
        ('vista_maps_dir', None),                          # stock Vista maps (shared.map, donors) (None = H2CONV_VISTA_MAPS, then a search of the drives)
        ('mcc_maps_dir', None),                            # MCC folder with textures.dat / sounds_*.dat (None = H2CONV_MCC_MAPS, then your Steam library)
        ('assembly_dir', None),                            # Assembly layout files (None = H2CONV_ASSEMBLY, then the copy bundled in h2conv\data\assembly)
        ('work_dir', None),                                # caches (None = H2CONV_WORK, then %LOCALAPPDATA%\H2MapConverter\work)
        ('h2ek_maps_dir', None),                           # MCC Halo 2 Editing Kit maps = extra donors for Vista -> MCC (None = not used)
        ('excession_dir', None),                           # Excession install (optional; None = looked up in your Steam libraries)
        ('last_scan_root', ''),
        ('direction', 'auto'),
        ('package_title', EX.DEFAULT_TITLE),
    ])

    def __init__(self, path=None):
        self.path = path or _paths.settings_file()
        self.data = OD(self.DEFAULTS)
        self.extra = OD()
        self.load()

    def load(self):
        try:
            with open(self.path, 'r', encoding='utf-8-sig') as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                for k, v in d.items():
                    if k in self.DEFAULTS:
                        self.data[k] = v
                    else:
                        self.extra[k] = v
        except (OSError, ValueError):
            pass
        return self

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + '.tmp'
            out = OD(self.data)
            out.update(self.extra)
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(out, fh, indent=1)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    def __getitem__(self, k):
        return self.data[k]

    def get(self, k, default=None):
        return self.data.get(k, default)

    def set(self, k, v):
        if k not in self.DEFAULTS:
            raise KeyError('unknown setting %r (known: %s)' % (k, ', '.join(self.DEFAULTS)))
        self.data[k] = v


# ======================================================================================================
# plan
# ======================================================================================================
class Item(object):
    """One scanned file and what the batch does with it."""

    def __init__(self, rec):
        self.rec = rec
        self.action = 'skip'            # convert | skip | invalid
        self.code = ''                  # machine readable cause for skip / invalid ('database', 'custom_shared', 'wrong_direction', ...)
        self.reason = ''
        self.direction = None           # 'vista2mcc' | 'mcc2vista'
        self.stem = None                # output name (file stem == header name == ScenarioFile for MCC output)
        self.out_path = None            # final output file
        self.guid = None                # MapGuid (vista2mcc)
        self.guid_provisional = False   # dry run --no-hash ONLY: GUID guessed / derived without hashing the file (name + skip decision provisional)
        self.sha256 = None
        self.naming = None              # why the output has this name: new | sanitised | suffix | replaces (see NAMING_LABELS); None = no name planned
        self.naming_note = ''           # one sentence for the UI / report
        self.kind = None                # 'multiplayer' | 'campaign'
        self.warnings = list(rec.warnings)
        # result
        self.status = None              # converted | failed | skipped | invalid | planned
        self.error = None
        self.seconds = 0.0
        self.out_size = None
        self.report = None

    @property
    def src(self):
        return self.rec.path

    def to_dict(self):
        d = OD()
        d['src'] = self.rec.path
        d['classification'] = self.rec.label()
        d['status'] = self.status or self.action
        d['code'] = self.code
        d['reason'] = self.error or self.reason
        d['direction'] = self.direction
        d['stem'] = self.stem
        d['out'] = self.out_path
        d['guid'] = self.guid
        d['naming'] = self.naming
        d['naming_note'] = self.naming_note
        d['provisional'] = bool(self.guid_provisional)
        d['seconds'] = round(self.seconds, 2)
        d['out_size'] = self.out_size
        d['src_size'] = self.rec.size
        d['warnings'] = self.warnings
        if self.status == 'failed' and self.report and self.report.get('traceback'):
            d['traceback'] = self.report['traceback']
        return d


class Options(object):
    """Everything a run needs (defaults = the CLI defaults)."""

    def __init__(self, **kw):
        self.direction = 'auto'
        self.mcc_out = None             # Excession mod package folder (vista2mcc): no default, --mcc-out the first time (settings.json remembers it)
        self.vista_out = None           # Halo 2 Maps folder (mcc2vista): None = settings.json, then <Documents>/My Games/Halo 2/Maps
        self.vista_maps = None          # stock Vista maps folder: None = H2CONV_VISTA_MAPS / settings.json / a search of the drives
        self.mcc_maps = None            # MCC maps folder: None = H2CONV_MCC_MAPS / settings.json / your Steam library
        self.h2ek_maps = None           # H2EK maps folder = extra donors for vista2mcc (None = H2CONV_H2EK_MAPS / settings.json, else not used)
        self.assembly_dir = None        # Assembly layout files (None = H2CONV_ASSEMBLY / settings.json / the bundled copy)
        self.dry_run = False
        self.only = []                  # fnmatch patterns on the file stem
        self.exclude = []
        self.kinds = ('multiplayer', 'solo')
        self.limit = None
        self.force = False              # vista2mcc: convert again although the package already holds this exact source
        self.overwrite = False          # mcc2vista: replace an existing output file
        self.suffix = ''                # mcc2vista: appended to the output name (e.g. '_mcc' so it does not look like the stock map)
        self.matg = 'hud'               # mcc2vista --matg
        self.isolate = False            # run every conversion in its own python process (hard crashes / hangs cannot stop the batch)
        self.timeout = 3600             # seconds per map in isolate mode
        self.jobs = 1                   # parallel conversions (isolate mode only, max 4)
        self.hash_duplicates = True     # skip byte-identical copies
        self.hash_sources = True        # dry run: hash every Vista source like a real run does (False = --no-hash: fast, names / skips PROVISIONAL)
        self.sort_maps = True           # sort the map lists of the package at the end
        self.refresh_metadata = False   # vista2mcc: regenerate the MapInfo JSON of maps that are already in the package
        self.package_title = EX.DEFAULT_TITLE
        self.package_description = EX.DEFAULT_DESCRIPTION
        self.recursive = True           # scan sub-folders too
        self.deep = True                # read the tag table of MCC maps that need a closer look at their shared.map
        self.remember = True            # save the folders to settings.json after a real run
        self.settings_path = None
        self.report = None              # write the JSON result here
        self.quiet = False
        self.list_plan = False          # dry run: print one line per planned conversion and per skipped / invalid file (except selection skips)
        self.fail_fast = False          # tests: stop at the first failure
        self.cancel_file = None         # the window program: when this file appears no further map is started (maps already done are kept)
        self.announce = False           # the window program: also print 'converting <file> ...' before every map in --isolate mode
        self.converters = {}            # tests / UI: {'vista2mcc': callable(src, out, **kw), 'mcc2vista': callable(src, out, **kw)}
        self.__dict__.update(kw)


def _wanted(rec, opts):
    stem = rec.stem.lower()
    if opts.only and not any(fnmatch.fnmatch(stem, p.lower()) for p in opts.only):
        return False
    if opts.exclude and any(fnmatch.fnmatch(stem, p.lower()) for p in opts.exclude):
        return False
    return True


def _provisional_guid(rec):
    """--no-hash only: a stand-in GUID for a source that was not hashed (a real run derives the GUID from the SHA-256)."""
    return EX.derive_map_guid(rec.scenario_path, 'dry-run:%d:%s' % (rec.size, rec.path.lower()))


# why an output has the name it has (Item.naming) -> the words of the --list labels / the UI
NAMING_LABELS = OD([('replaces', 'replaces existing'), ('new', 'new'), ('sanitised', 'name sanitised'), ('suffix', 'name taken -> suffix')])
# --list prints no line for items skipped only because of a selection made on the command line (counts are in the summary)
LIST_QUIET_CODES = ('not_selected', 'wrong_direction', 'database', 'limit')


def plan_label(item):
    """The parenthesised tag of a --list line / UI row: '(replaces existing)', '(name sanitised)', '(name taken -> suffix)', '(new)';
    ';  provisional - not hashed' is added when the item's GUID (so its name / skip decision) was not derived from the file's SHA-256."""
    lab = NAMING_LABELS.get(item.naming, item.naming or '')
    if item.guid_provisional:
        lab += '; provisional - not hashed' if lab else 'provisional - not hashed'
    return '(%s)' % lab if lab else ''


def _manifest_key(src, size, scenario_path):
    """(lower-case source FILE NAME, size, normalised scenario path): the package manifest keeps names, never folders (h2conv.excession.source_name)."""
    return (EX.source_name(src).lower(), size, MI.norm_scenario(scenario_path or ''))


def _manifest_index(package):
    """--no-hash only: {(source file name, size, normalised scenario path): guid} of the package's manifest, to recognise a map that was
    converted from this very file (a GUESS: the file may have changed without changing its size, and two files of the same name, size and
    scenario in different folders look alike; every answer is flagged provisional)."""
    idx, clash = {}, set()
    for guid, m in (package.manifest.get('entries') or {}).items():
        if isinstance(m, dict) and m.get('source') and m.get('source_size') is not None:
            k = _manifest_key(m['source'], m['source_size'], m.get('scenario_path'))
            if k in idx:
                clash.add(k)                      # two entries look alike: no guess (the map is then treated as not converted yet)
            idx[k] = guid
    for k in clash:
        del idx[k]
    return idx


def _plan_vista_name(plan, it, batch_stems, batch_guids):
    """vista2mcc: the stem / output path of `it` (reserved in the package) and WHY it has that name (it.naming / it.naming_note).
    Must be called in plan order: the name taken by an earlier item decides the later ones."""
    r, pkg = it.rec, plan.package
    existing = pkg.find(it.guid)
    it.stem = pkg.plan_stem(r.stem, it.guid, reserve=True)
    it.out_path = pkg.map_path(it.stem)
    base = EX.sanitize_stem(r.stem)
    prior = batch_guids.get(it.guid)
    if existing is not None:
        it.naming = 'replaces'
        it.naming_note = 'this exact source (GUID %s) is already in the package as %s' % (it.guid, existing.rel)
    elif prior is not None:
        it.naming = 'replaces'
        it.naming_note = 'same map as %s (--no-dedupe): the later file overwrites the earlier output' % prior.src
    elif it.stem == base:
        if base.lower() != r.stem.lower():
            it.naming = 'sanitised'
            it.naming_note = "file name '%s' is not a valid map name; sanitised to '%s'" % (r.stem, base)
        else:
            it.naming = 'new'
            it.naming_note = 'new package entry'
    else:
        it.naming = 'suffix'
        owner = pkg.find_by_stem(base)
        if owner is not None:
            who = 'package entry %s (GUID %s)' % (owner.rel, owner.guid)
        elif base.lower() in batch_stems:
            who = 'the map planned from %s' % batch_stems[base.lower()].src
        else:
            who = 'a file that is already in the package folder'
        it.naming_note = "name '%s'%s is taken by %s; GUID suffix added" % (
            base, '' if base.lower() == r.stem.lower() else " (sanitised from '%s')" % r.stem, who)
    batch_stems.setdefault(it.stem.lower(), it)
    batch_guids.setdefault(it.guid, it)


def _mcc_base(rec, suffix=''):
    """The wanted output name of an MCC -> Vista conversion before collisions: the input's file stem (+ suffix), valid for Cartographer's list."""
    from . import limits as LIM
    base = rec.stem + suffix
    if LIM.check_map_name(base):
        base = EX.sanitize_stem(base)
    return base


def mcc_stem(rec, used, suffix=''):
    """Output name of an MCC -> Vista conversion: the input's file stem (+ suffix), valid for Cartographer's list, unique in `used`."""
    from . import limits as LIM
    base = _mcc_base(rec, suffix)
    for n in range(1, 1000):
        cand = base if n == 1 else '%s_%d' % (base[:LIM.MAP_NAME_MAX - 4], n)
        if cand.lower() not in used:
            return cand
    raise RuntimeError('no free output name for %s' % rec.path)


class Plan(object):
    def __init__(self, root, opts):
        self.root = root
        self.opts = opts
        self.items = []
        self.package = None              # ModPackage (real or virtual) for vista2mcc
        self.package_note = ''
        self.vista_used = set()
        self.notes = []

    def count(self, **kw):
        return sum(1 for i in self.items if all(getattr(i, k) == v for k, v in kw.items()))

    def to_summary(self):
        c = collections.Counter(i.action if i.action != 'skip' else 'skip:' + i.code for i in self.items)
        return dict(c)


def make_plan(root, opts, records=None, log=None):
    """Scan `root` and decide, per file, convert / skip / invalid with output names.  Touches nothing on disk except reading."""
    log = log or (lambda m: None)
    if records is None and not os.path.exists(root):
        raise RuntimeError('%s does not exist' % root)
    excl = [d for d in (opts.mcc_out, opts.vista_out) if d]
    recs = records if records is not None else MI.scan_folder(root, recursive=opts.recursive, exclude=excl, deep=opts.deep)
    plan = Plan(root, opts)
    # ---- vista2mcc needs the package for stem planning --------------------------------------------------------------------------
    need_pkg = opts.direction in ('auto', 'vista2mcc') and any(r.fmt == 'vista' and r.status == 'ok' for r in recs)
    if need_pkg and not opts.mcc_out:
        if any(r.fmt == 'vista' and r.status == 'ok' and r.kind in opts.kinds and _wanted(r, opts) for r in recs):
            raise RuntimeError(NEED_MCC_OUT)
        need_pkg = False                 # no Vista map is selected: no package is needed
    if need_pkg:
        if os.path.isfile(os.path.join(opts.mcc_out, 'ModInfo.json')):
            plan.package = EX.ModPackage(opts.mcc_out, create=False)
            plan.package_note = 'existing package (%d maps)' % len(plan.package.entries)
        elif opts.dry_run:
            plan.package = EX.ModPackage.virtual(opts.mcc_out, title=opts.package_title, description=opts.package_description)
            plan.package_note = 'would be created'
        else:
            plan.package = EX.ModPackage(opts.mcc_out, create=True, title=opts.package_title, description=opts.package_description)
            plan.package_note = 'created'
    # ---- classify each file ---------------------------------------------------------------------------------------------------
    by_size = collections.defaultdict(list)
    for r in recs:
        it = Item(r)
        plan.items.append(it)
        if r.status == 'invalid':
            it.action, it.code, it.reason = 'invalid', r.code, r.reason
            continue
        if r.status == 'skip':
            it.action, it.code, it.reason = 'skip', r.code, r.reason
            continue
        # a playable map
        want = {'vista': 'vista2mcc', 'mcc': 'mcc2vista'}[r.fmt]
        if opts.direction in ('vista2mcc', 'mcc2vista') and opts.direction != want:
            it.action, it.code = 'skip', 'wrong_direction'
            it.reason = '%s map, but the direction is %s' % ('Vista' if r.fmt == 'vista' else 'MCC', opts.direction)
            continue
        if r.kind not in opts.kinds:
            it.action, it.code, it.reason = 'skip', 'not_selected', 'kind %s not selected' % r.kind
            continue
        if not _wanted(r, opts):
            it.action, it.code, it.reason = 'skip', 'not_selected', 'filtered out by --only / --exclude'
            continue
        it.direction = want
        it.action = 'convert'
        by_size[(want, r.size)].append(it)
    # ---- byte-identical copies --------------------------------------------------------------------------------------------------
    if opts.hash_duplicates:
        for (_d, _s), group in by_size.items():
            if len(group) < 2:
                continue
            seen = {}
            for it in group:
                try:
                    h = MI.source_sha256(it.src)
                except OSError as ex:
                    it.action, it.code, it.reason = 'invalid', 'unreadable', 'cannot read: %s' % ex
                    continue
                it.sha256 = h
                if h in seen:
                    it.action, it.code = 'skip', 'duplicate'
                    it.reason = 'byte-identical copy of %s' % seen[h]
                else:
                    seen[h] = it.src
    # ---- limit -----------------------------------------------------------------------------------------------------------------
    if opts.limit is not None:
        n = 0
        for it in plan.items:
            if it.action == 'convert':
                n += 1
                if n > opts.limit:
                    it.action, it.code, it.reason = 'skip', 'limit', 'beyond --limit %d' % opts.limit
    # ---- names / GUIDs ---------------------------------------------------------------------------------------------------------
    # A dry run decides EXACTLY like a real run: every Vista source is hashed, its GUID derived from the hash, its name planned against the
    # package (an already converted map keeps its entry's stem), and 'up_to_date' decided from the manifest.  The only way to get different
    # (provisional) answers is the explicit --no-hash (opts.hash_sources = False): those items carry guid_provisional and say so everywhere.
    hashing = (not opts.dry_run) or opts.hash_sources
    pending = [i for i in plan.items if i.action == 'convert' and i.direction == 'vista2mcc' and i.sha256 is None]
    if hashing and len(pending) > 8:
        log('hashing %d Vista maps (%.1f GB) to derive stable map GUIDs ...' % (len(pending), sum(i.rec.size for i in pending) / 1073741824.0))
    guess = _manifest_index(plan.package) if (pending and not hashing) else {}
    batch_stems, batch_guids = {}, {}
    for it in plan.items:
        if it.action != 'convert':
            continue
        r = it.rec
        if it.direction == 'vista2mcc':
            it.kind = 'multiplayer' if r.kind == 'multiplayer' else 'campaign'
            if it.sha256 is None and hashing:
                try:
                    it.sha256 = MI.source_sha256(it.src)
                except OSError as ex:
                    it.action, it.code, it.reason = 'invalid', 'unreadable', 'cannot read: %s' % ex
                    continue
            if it.sha256:
                it.guid = EX.derive_map_guid(r.scenario_path, it.sha256)
            else:                                   # --no-hash: recognise a map converted from this very file, else a stand-in GUID
                g = guess.get(_manifest_key(it.src, r.size, r.scenario_path))
                it.guid, it.guid_provisional = (g or _provisional_guid(r)), True
            _plan_vista_name(plan, it, batch_stems, batch_guids)
            if not opts.force and plan.package.is_current(it.guid):
                it.action, it.code = 'skip', 'up_to_date'
                if it.guid_provisional:
                    it.reason = 'probably already converted (same source file name, size and scenario as package entry %s; NOT hashed, so provisional - ' \
                                'run the dry run without --no-hash to confirm; --force redoes it)' % it.stem
                else:
                    it.reason = 'already converted from this exact source (use --force to redo)'
        else:
            if not opts.vista_out:
                raise RuntimeError(NEED_VISTA_OUT)
            it.kind = 'multiplayer' if r.kind == 'multiplayer' else 'campaign'
            it.stem = mcc_stem(r, plan.vista_used, opts.suffix)
            plan.vista_used.add(it.stem.lower())
            it.out_path = os.path.join(opts.vista_out, it.stem + '.map')
            wanted = r.stem + opts.suffix
            if it.stem.lower() == wanted.lower():
                it.naming, it.naming_note = 'new', 'new Vista map'
            elif it.stem.lower() == _mcc_base(r, opts.suffix).lower():
                it.naming, it.naming_note = 'sanitised', "'%s' is not a valid Cartographer map name; sanitised to '%s'" % (wanted, it.stem)
            else:
                it.naming, it.naming_note = 'suffix', "name '%s' is already used by another output of this batch; '%s' chosen" % (_mcc_base(r, opts.suffix), it.stem)
            if os.path.exists(it.out_path):
                if opts.overwrite:
                    it.naming, it.naming_note = 'replaces', 'output %s exists and --overwrite replaces it' % it.out_path
                else:
                    it.action, it.code, it.reason = 'skip', 'exists', 'output %s already exists (use --overwrite)' % it.out_path
                    it.naming, it.naming_note = None, ''        # skipped: no output name is planned, so no naming explanation
    return plan


# ======================================================================================================
# converters
# ======================================================================================================
def load_reverse_converter():
    """(callable, module_name) of h2conv.rev.convert, or (None, reason).  The callable must be convert_map(src, out, ...)."""
    try:
        mod = importlib.import_module('h2conv.rev.convert')
    except Exception as ex:                                                 # noqa: BLE001
        return None, 'h2conv.rev.convert is not available (%s: %s)' % (type(ex).__name__, ex)
    fn = getattr(mod, 'convert_map', None) or getattr(mod, 'convert', None)
    if not callable(fn):
        return None, 'h2conv.rev.convert has no convert_map()'
    return fn, mod


def call_filtered(fn, src, out, **kw):
    """Call fn(src, out, <the keyword arguments fn accepts>); an `opts` dict parameter receives the rest."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(src, out)
    params = sig.parameters
    var_kw = any(p.kind == p.VAR_KEYWORD for p in params.values())
    use = {k: v for k, v in kw.items() if k in params or var_kw}
    if 'opts' in params and 'opts' not in kw:
        use['opts'] = {k: v for k, v in kw.items() if k not in params}
    return fn(src, out, **use)


def _report_ok(mod, rep):
    ok = getattr(mod, 'report_ok', None)
    if callable(ok):
        try:
            return bool(ok(rep)), ''
        except Exception as ex:                                              # noqa: BLE001
            return False, 'report_ok failed: %r' % (ex,)
    if isinstance(rep, dict):
        for k in ('failures', 'errors', 'validate_file'):
            v = rep.get(k)
            if v:
                return False, '%s: %s' % (k, str(v)[:200])
    return True, ''


def _brief_report(rep):
    if not isinstance(rep, dict):
        return None
    out = OD()
    for k in ('sha256', 'out_size', 'local_tags', 'tag_count', 'timings_s', 'peak_rss_mb'):
        if k in rep:
            out[k] = rep[k]
    w = rep.get('warnings')
    if isinstance(w, dict):
        out['warning_codes'] = {c: (v.get('count') if isinstance(v, dict) else v) for c, v in list(w.items())[:40]}
    if rep.get('failures'):
        out['failures'] = len(rep['failures'])
    return out


class ConvertError(Exception):
    pass


def _forward_inprocess(item, out_file, opts):
    """MCC -> Vista with h2conv.convert.convert_map."""
    from h2conv import convert as F
    o = {'quiet': True, 'name': item.stem, 'matg': opts.matg}
    if opts.mcc_maps:
        o['mcc_maps_dir'] = o['mcc_dat_dir'] = opts.mcc_maps
    fn = opts.converters.get('mcc2vista')
    if fn is not None:
        rep = call_filtered(fn, item.src, out_file, name=item.stem, quiet=True, vista_maps=opts.vista_maps)
        if isinstance(rep, tuple) and rep:
            rep = rep[0]
        return rep, True, ''
    rep, _ctx = F.convert_map(item.src, out_file, opts.vista_maps, False, None, o)
    ok = F.report_ok(rep)
    del _ctx
    return rep, ok, '' if ok else 'the converter\'s own checks failed: %s' % _why_failed(rep)


def _why_failed(rep):
    bits = []
    if rep.get('validate_file'):
        bits.append('validate_file: %s' % str(rep['validate_file'])[:150])
    if rep.get('failures'):
        bits.append('%d tag(s) failed to convert (first: %s)' % (len(rep['failures']), str(rep['failures'][0])[:150]))
    if rep.get('encode_failures'):
        bits.append('%s encode failure(s)' % rep['encode_failures'])
    for k in ('validate_map', 'limits_check', 'invariants'):
        v = rep.get(k)
        if isinstance(v, dict) and v.get('errors'):
            bits.append('%s: %s error(s)' % (k, v['errors']))
    return '; '.join(bits) or 'unknown'


def _reverse_inprocess(item, out_file, opts):
    """Vista -> MCC with h2conv.rev.convert."""
    fn = opts.converters.get('vista2mcc')
    mod = None
    if fn is None:
        fn, mod = load_reverse_converter()
        if fn is None:
            raise ConvertError(mod)
    extra = {'h2ek_maps': opts.h2ek_maps} if opts.h2ek_maps else {}          # only the real converter reads it (in its opts dict)
    rep = call_filtered(fn, item.src, out_file, name=item.stem, quiet=True, vista_maps=opts.vista_maps, mcc_maps=opts.mcc_maps, **extra)
    if isinstance(rep, tuple) and rep:              # (report, ctx) like h2conv.convert.convert_map
        rep = rep[0]
    ok, why = _report_ok(mod, rep) if mod is not None else (True, '')
    return rep, ok, why


def child_command(tool):
    """The command that starts one converter in its own process: tool 'convert' = MCC -> Vista, 'rev' = Vista -> MCC.
    From a Python install:  python -m h2conv.convert | h2conv.rev.convert.  From a frozen exe (sys.frozen) there is no `python -m`: the exe is
    the single entry point (h2conv/__main__.py), so the child is `<exe> convert|rev <arguments>`."""
    if getattr(sys, 'frozen', False):
        return [sys.executable, tool]
    return [sys.executable, '-m', {'convert': 'h2conv.convert', 'rev': 'h2conv.rev.convert'}[tool]]


def _subprocess_convert(item, out_file, opts):
    """One conversion in its own python process (hard crashes, hangs and memory leaks cannot hurt the batch)."""
    stage = os.path.dirname(out_file)
    rep_path = os.path.join(stage, item.stem + '.report.json')
    vista_maps = _paths.vista_maps_dir(opts.vista_maps)
    if item.direction == 'mcc2vista':
        cmd = child_command('convert') + [item.src, out_file, '--name', item.stem, '--quiet', '--report', rep_path,
                                          '--vista-maps', vista_maps, '--matg', opts.matg]
        if opts.mcc_maps:
            cmd += ['--mcc-maps', opts.mcc_maps]
    else:
        cmd = child_command('rev') + [item.src, out_file, '--name', item.stem, '--quiet', '--report', rep_path,
                                      '--vista-maps', vista_maps]
        if opts.mcc_maps:
            cmd += ['--mcc-maps', opts.mcc_maps]
        if opts.h2ek_maps:
            cmd += ['--h2ek-maps', opts.h2ek_maps]
    if opts.assembly_dir:
        cmd += ['--assembly-dir', opts.assembly_dir]
    cmd.append('--overwrite')        # the staging folder is the batch's own scratch space (it removed any leftover); what may replace what is decided by the plan
    if item.direction != 'mcc2vista':
        # reverse planner options, e.g. H2CONV_REV_PLAN_OPTS="shared_policy=mcc".  NEVER stem_fallback:
        # substitute templates are never used - shaders are ported faithfully or flagged unportable.
        for kv in (os.environ.get('H2CONV_REV_PLAN_OPTS') or '').split(';'):
            if kv.strip():
                cmd += ['--plan-opt', kv.strip()]
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    frozen = bool(getattr(sys, 'frozen', False))
    if not frozen:                                       # the package must be importable in the child whatever the working directory of the parent was
        env['PYTHONPATH'] = ROOT + ((os.pathsep + env['PYTHONPATH']) if env.get('PYTHONPATH') else '')
    try:
        p = subprocess.run(cmd, cwd=None if frozen else ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=opts.timeout, creationflags=CREATE_NO_WINDOW | BELOW_NORMAL)
    except subprocess.TimeoutExpired:
        raise ConvertError('timed out after %d s' % opts.timeout)
    tail = (p.stdout or b'').decode('utf-8', 'replace').strip().splitlines()[-6:]
    rep = None
    try:
        with open(rep_path, 'r', encoding='utf-8') as fh:
            rep = json.load(fh)
        os.remove(rep_path)
    except (OSError, ValueError):
        pass
    if p.returncode != 0:
        raise ConvertError('converter exit code %d: %s' % (p.returncode, ' | '.join(tail)[-400:]))
    return rep, True, ''


# ======================================================================================================
# run
# ======================================================================================================
def _lock(path):
    """Refuse to run two batches into the same output; returns a release() function."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        try:
            pid = int(open(path).read().split()[0])
        except (OSError, ValueError, IndexError):
            pid = 0
        alive = False
        if pid and pid != os.getpid():
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                alive = True
        if alive:
            raise RuntimeError('another batch (process %d) is writing to %s' % (pid, os.path.dirname(path)))
    with open(path, 'w') as fh:
        fh.write('%d %s\n' % (os.getpid(), datetime.datetime.now().isoformat()))

    def release():
        try:
            os.remove(path)
        except OSError:
            pass
    return release


def _clear_staged(staged):
    """Remove what an earlier, interrupted conversion of the same map left in the tool's OWN staging folder: the staged file, its '.part' and the
    '<stem>.<pid>.<hex>.tmp' (+ '.part') temp files of the converters.  Matches exact names only, so another map's files are never touched."""
    d, base = os.path.split(staged)
    stem = os.path.splitext(base)[0]
    tmp_rx = re.compile(r'^%s\.\d+\.[0-9a-f]{8}\.tmp(\.part)?$' % re.escape(stem), re.I)
    names = [staged, staged + '.part', os.path.splitext(staged)[0] + '.tmp']
    try:
        names += [os.path.join(d, fn) for fn in os.listdir(d) if tmp_rx.match(fn)]
    except OSError:
        pass
    for p in names:
        if os.path.exists(p):
            os.remove(p)


def _convert_item(item, plan, opts):
    """Convert one planned item; sets item.status / error / out_size.  Never raises (except KeyboardInterrupt)."""
    t0 = time.time()
    pkg = plan.package
    try:
        if item.direction == 'vista2mcc':
            stage_dir = os.path.join(pkg.root, EX.STATE_DIR, 'staging')
        else:
            stage_dir = os.path.join(opts.vista_out, '.h2conv_staging')
        os.makedirs(stage_dir, exist_ok=True)
        staged = os.path.join(stage_dir, item.stem + '.map')
        _clear_staged(staged)
        titles = None
        if item.direction == 'vista2mcc':
            titles = MI.read_titles(item.src, item.rec)
        # ---- run the converter ----------------------------------------------------------------------------------------------
        if opts.isolate:
            rep, ok, why = _subprocess_convert(item, staged, opts)
        elif item.direction == 'mcc2vista':
            rep, ok, why = _forward_inprocess(item, staged, opts)
        else:
            rep, ok, why = _reverse_inprocess(item, staged, opts)
        if not ok:
            raise ConvertError(why or 'the converter reported errors')
        if not os.path.isfile(staged):
            raise ConvertError('the converter finished but wrote no output file')
        # ---- check what was written ----------------------------------------------------------------------------------------
        got = MI.identify(staged, deep=False)
        want_fmt = 'mcc' if item.direction == 'vista2mcc' else 'vista'
        if got.fmt != want_fmt or got.status == 'invalid':
            raise ConvertError('output is not a valid %s map: %s' % (want_fmt, got.reason or got.label()))
        item.out_size = os.path.getsize(staged)
        item.report = _brief_report(rep)
        if got.name and got.name.lower() != item.stem.lower():
            item.warnings.append("the converter wrote the internal map name '%s', not '%s' (did it ignore name=?)" % (got.name, item.stem))
        if got.kind != item.rec.kind:
            item.warnings.append('scenario type changed in conversion: %s -> %s' % (item.rec.kind, got.kind))
        # ---- commit -----------------------------------------------------------------------------------------------------------
        if item.direction == 'mcc2vista':
            os.replace(staged, item.out_path)
        else:
            prov = OD([('source', EX.source_name(item.src)), ('source_sha256', item.sha256), ('source_size', item.rec.size),
                       ('source_build', item.rec.build), ('scenario_path', item.rec.scenario_path), ('title_source', titles['source']),
                       ('converter', 'h2conv.rev.convert'), ('seconds', round(time.time() - t0, 1))])
            if item.kind == 'multiplayer':
                extra = {'stock_name': item.rec.name or item.rec.stem}
            else:
                extra = {}
            pkg.add_map(item.kind, item.guid, item.stem, titles['title'], titles['description'] or None, map_file=staged,
                        provenance=prov, refresh_metadata=opts.refresh_metadata, title_hint=item.rec.stem, **extra)
        item.status = 'converted'
    except KeyboardInterrupt:
        raise
    except BaseException as ex:                                                 # noqa: BLE001 - includes SystemExit from a converter
        item.status = 'failed'
        msg = '%s: %s' % (type(ex).__name__, ex) if not isinstance(ex, ConvertError) else str(ex)
        item.error = msg[:600]
        item.report = {'traceback': traceback.format_exc().splitlines()[-6:]}
        try:
            _clear_staged(staged)
        except (OSError, UnboundLocalError):
            pass
    finally:
        item.seconds = time.time() - t0
        gc.collect()


def say(msg):
    """print + flush: a long batch redirected to a file / read by a UI must show progress at once."""
    print(msg)
    try:
        sys.stdout.flush()
    except Exception:                                                           # noqa: BLE001
        pass


def run_batch(root, opts, log=say, plan=None, progress=None):
    """Plan (unless given) and execute.  Returns the result dict."""
    # the folders of this run are what the converters (in this process and in --isolate children) must use; they are forgotten when the run ends
    with _paths.scoped(mcc_maps=opts.mcc_maps, vista_maps=opts.vista_maps, assembly=opts.assembly_dir, h2ek_maps=opts.h2ek_maps):
        return _run_batch(root, opts, log, plan, progress)


def _run_batch(root, opts, log, plan, progress):
    t_start = time.time()
    if opts.quiet:
        log = lambda m: None                                                    # noqa: E731
    settings = Settings(opts.settings_path) if opts.settings_path is not False else None
    if plan is None:
        plan = make_plan(root, opts, log=log)
    items = plan.items
    todo = [i for i in items if i.action == 'convert']
    result = OD()
    result['tool'] = 'h2conv.batch'
    result['root'] = root
    result['direction'] = opts.direction
    result['dry_run'] = bool(opts.dry_run)
    result['started'] = datetime.datetime.now().isoformat(timespec='seconds')
    result['output_dirs'] = OD()
    if any(i.direction == 'vista2mcc' for i in items):
        result['output_dirs']['mcc'] = opts.mcc_out
    if any(i.direction == 'mcc2vista' for i in items):
        result['output_dirs']['vista'] = opts.vista_out
    log('scanned %s: %d .map files, %d to convert' % (root, len(items), len(todo)))
    if opts.dry_run:
        for i in todo:
            i.status = 'planned'
        if opts.list_plan:
            # one line per planned conversion (what the real run would write and why that name), and per skipped / invalid file whose
            # fate is not simply the caller's own selection (--only / --exclude / --kind / --limit / direction / the databases)
            for i in items:
                if i.action == 'convert':
                    log('plan  %-9s %-8s %s -> %s.map   %s' % (i.direction, i.kind, i.rec.path, i.stem, plan_label(i)))
                elif i.code not in LIST_QUIET_CODES:
                    log('%-5s %-9s %-8s %s%s   [%s] %s%s' % ('skip' if i.action == 'skip' else 'bad', i.direction or '-', i.kind or i.rec.kind or '-', i.rec.path,
                                                             (' -> %s.map' % i.stem) if i.stem else '', i.code, i.reason,
                                                             '   (provisional - not hashed)' if i.guid_provisional else ''))
    else:
        locks = []
        try:
            if plan.package is not None and plan.package.save_manifest_if_migrated():
                log('package manifest: the absolute source paths of an older version were reduced to file names')
            if plan.package is not None and any(i.direction == 'vista2mcc' for i in todo):
                locks.append(_lock(os.path.join(plan.package.root, EX.STATE_DIR, 'batch.lock')))
            if any(i.direction == 'mcc2vista' for i in todo):
                os.makedirs(opts.vista_out, exist_ok=True)
                locks.append(_lock(os.path.join(opts.vista_out, '.h2conv_staging', 'batch.lock')))
            _execute(todo, plan, opts, log, progress)
            if any(i.code == 'cancelled' for i in todo):
                log('stopped as requested - the maps converted so far are kept')
                result['interrupted'] = True
        except KeyboardInterrupt:
            log('interrupted - the maps converted so far are kept')
            result['interrupted'] = True
        finally:
            for rel in locks:
                rel()
            for sd in ([os.path.join(plan.package.root, EX.STATE_DIR, 'staging')] if plan.package is not None and not plan.package.is_virtual else [])                     + ([os.path.join(opts.vista_out, '.h2conv_staging')] if opts.vista_out and any(i.direction == 'mcc2vista' for i in todo) else []):
                try:
                    os.rmdir(sd)                       # only when empty
                except OSError:
                    pass
        if plan.package is not None and opts.sort_maps and any(i.status == 'converted' and i.direction == 'vista2mcc' for i in todo):
            plan.package.sort_maps()
            plan.package.save()
    # ---- results ------------------------------------------------------------------------------------------------------------------
    converted = [i for i in items if i.status == 'converted']
    failed = [i for i in items if i.status == 'failed']
    for i in items:
        if i.status is None:
            i.status = 'invalid' if i.action == 'invalid' else 'skipped'
    result['counts'] = OD([('scanned', len(items)), ('convertible', len(todo)), ('converted', len(converted)), ('failed', len(failed)),
                           ('skipped', sum(1 for i in items if i.status == 'skipped')), ('invalid', sum(1 for i in items if i.status == 'invalid')),
                           ('planned', sum(1 for i in items if i.status == 'planned'))])
    result['skipped_by_code'] = dict(collections.Counter(i.code for i in items if i.status == 'skipped'))
    result['naming_counts'] = dict(collections.Counter(i.naming for i in todo if i.naming))     # why the planned outputs have their names
    result['provisional'] = sum(1 for i in items if i.guid_provisional)                         # >0 only for a dry run with --no-hash
    result['items'] = [i.to_dict() for i in items]
    result['failed'] = [i.to_dict() for i in failed]
    result['needs_attention'] = [i.to_dict() for i in items if (i.status == 'skipped' and i.code not in BENIGN_SKIPS) or i.status == 'invalid']
    result['elapsed_s'] = round(time.time() - t_start, 1)
    if plan.package is not None:
        result['package'] = dict(plan.package.summary(), note=plan.package_note)
        if not opts.dry_run and any(i.direction == 'vista2mcc' for i in todo):
            iss = plan.package.validate()
            result['package_validation'] = {'errors': len(EX.errors_of(iss)), 'warnings': sum(1 for x in iss if x.level == 'warning'),
                                            'first_errors': [str(x) for x in EX.errors_of(iss)[:10]]}
    # disk space / size estimate (dry run and real run)
    est = {}
    tgt = {'vista2mcc': opts.mcc_out, 'mcc2vista': opts.vista_out}
    for d in ('vista2mcc', 'mcc2vista'):
        n = sum(1 for i in todo if i.direction == d)
        if not n:
            continue
        probe = tgt[d]
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            free = None
        src_bytes = sum(i.rec.size for i in todo if i.direction == d)
        if d == 'vista2mcc':
            out_bytes = sum(estimate_mcc_bytes(i.rec) for i in todo if i.direction == d)
        else:   # measured: custom H2EK headlong 336 MB -> 75 MB (x0.22, chronopolis 560 -> 243 MB = x0.43); stock compressed maps 1.7-4 MB -> 26-45 MB (x11..x20)
            out_bytes = int(sum(i.rec.size * (0.4 if i.rec.style == 'h2ek' else 15) for i in todo if i.direction == d))
        est[d] = {'maps': n, 'source_bytes': src_bytes, 'free_bytes': free, 'estimated_output_bytes': out_bytes}
    result['estimate'] = est
    if settings is not None and opts.remember and not opts.dry_run and (converted or failed):
        settings.set('last_scan_root', root)
        settings.set('direction', opts.direction)
        if any(i.direction == 'vista2mcc' for i in todo):
            settings.set('mcc_output_dir', opts.mcc_out)
        if any(i.direction == 'mcc2vista' for i in todo):
            settings.set('vista_output_dir', opts.vista_out)
        settings.save()
    if opts.report:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(opts.report)), exist_ok=True)
            with open(opts.report, 'w', encoding='utf-8') as fh:
                json.dump(result, fh, indent=1, default=str)
        except OSError as ex:
            log('could not write the report: %s' % ex)
    print_summary(result, log)
    return result


def _execute(todo, plan, opts, log, progress):
    n = len(todo)
    jobs = max(1, min(4, int(opts.jobs))) if opts.isolate else 1
    import threading
    lock = threading.Lock()
    done = [0]

    def finish(it):
        with lock:
            done[0] += 1
            sym = {'converted': 'ok  ', 'failed': 'FAIL'}.get(it.status, '??  ')
            log('[%*d/%d] %s %-34s %6.1fs  %s' % (len(str(n)), done[0], n, sym, it.rec.stem[:34], it.seconds,
                                                 (it.error or ('-> %s' % it.out_path))[:200]))
            if progress:
                progress(done[0], n, it)

    def cancelled():
        return bool(opts.cancel_file) and os.path.exists(opts.cancel_file)

    def not_started(it):
        it.status, it.code, it.reason = 'skipped', 'cancelled', 'not started: the batch was stopped'

    def work(it):
        if cancelled():
            not_started(it)
            return
        if jobs > 1 and opts.announce:
            log('converting %s ...' % it.src)
        _convert_item(it, plan, opts)       # ModPackage.add_map is thread safe
        finish(it)
    if jobs == 1:
        for it in todo:
            if cancelled():
                not_started(it)
                continue
            if not opts.isolate or opts.announce:
                log('converting %s ...' % it.src)
            work(it)
            if opts.fail_fast and it.status == 'failed':
                break
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:     # the work happens in child processes; threads only wait
        list(ex.map(work, todo))


def human(n):
    if n is None:
        return '?'
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024 or u == 'TB':
            return ('%d %s' % (n, u)) if u == 'B' else ('%.1f %s' % (n, u))
        n /= 1024.0


def print_summary(res, log=say):
    c = res['counts']
    log('')
    log('=== batch %s: %s ===' % ('DRY RUN' if res['dry_run'] else 'finished', res['direction']))
    log('scanned %d files: %d convertible, %d converted, %d failed, %d skipped, %d invalid%s' % (
        c['scanned'], c['convertible'], c['converted'], c['failed'], c['skipped'], c['invalid'],
        (', %d planned' % c['planned']) if c['planned'] else ''))
    if res['skipped_by_code']:
        log('skipped by cause: ' + ', '.join('%s=%d' % kv for kv in sorted(res['skipped_by_code'].items())))
    if res['dry_run'] and res.get('naming_counts'):
        log('planned names: ' + ', '.join('%s=%d' % (NAMING_LABELS[k], res['naming_counts'][k]) for k in NAMING_LABELS if k in res['naming_counts']))
    if res.get('provisional'):
        log('NOTE (--no-hash): %d Vista map(s) were NOT hashed - their GUIDs, GUID-suffixed names and up_to_date decisions are PROVISIONAL '
            '(marked in the list / report); run the dry run without --no-hash for the exact plan' % res['provisional'])
    if res['failed']:
        log('')
        log('FAILED maps (%d):' % len(res['failed']))
        for d in res['failed']:
            log('  %s\n      %s' % (d['src'], d['reason']))
    if res['needs_attention']:
        log('')
        log('SKIPPED / INVALID maps that need attention (%d):' % len(res['needs_attention']))
        for d in res['needs_attention']:
            log('  [%s] %s\n      %s' % (d['code'], d['src'], d['reason']))
    for d, e in res.get('estimate', {}).items():
        log('')
        log('%s: %d maps, %s of source maps; estimated output about %s (a rough upper estimate: maps with many custom bitmaps or sounds can be larger); '
            'free space on the target drive: %s' % (d, e['maps'], human(e['source_bytes']), human(e['estimated_output_bytes']), human(e['free_bytes'])))
        if e['free_bytes'] is not None and e['estimated_output_bytes'] > e['free_bytes']:
            log('  WARNING: the estimated output is larger than the free space')
    if res.get('package'):
        p = res['package']
        log('')
        log('Excession mod package: %s  (%s)' % (p['root'], p.get('note', '')))
        log('  %d multiplayer + %d campaign maps, %s of maps' % (p['multiplayer_maps'], p['campaign_maps'], human(p['map_bytes'])))
        v = res.get('package_validation')
        if v:
            log('  validation: %d error(s), %d warning(s)%s' % (v['errors'], v['warnings'], ''.join('\n    ' + x for x in v['first_errors'])))
    if res['output_dirs']:
        log('')
        for k, v in res['output_dirs'].items():
            log('output directory (%s): %s' % ('MCC maps' if k == 'mcc' else 'Vista maps', v))
    log('elapsed %s s' % res['elapsed_s'])


# ======================================================================================================
# CLI
# ======================================================================================================
def _split(v):
    return [x.strip() for x in (v or '').split(',') if x.strip()]


def main(argv=None):
    import argparse
    from . import __version__
    ap = argparse.ArgumentParser(prog='h2conv.batch', formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description='Batch conversion of Halo 2 maps (Vista <-> MCC).  Never uploads anything.')
    ap.add_argument('--version', action='version', version='Halo 2 Map Converter %s' % __version__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('scan', help='classify every .map below ROOT')
    s.add_argument('root')
    s.add_argument('--json', action='store_true')
    s.add_argument('--no-deep', action='store_true')
    s.add_argument('--no-recursive', action='store_true')
    _paths.add_common_options(s, mcc=True, assembly=False)
    r = sub.add_parser('run', help='convert every convertible map below ROOT')
    r.add_argument('root')
    r.add_argument('--direction', choices=('auto', 'vista2mcc', 'mcc2vista'), default=None)
    r.add_argument('--no-recursive', action='store_true', help='only the files directly in ROOT')
    r.add_argument('--mcc-out', default=None, help='Excession mod package folder for the MCC output of Vista -> MCC (required the first time; remembered afterwards)')
    r.add_argument('--vista-out', default=None, help='Halo 2 Maps folder for the Vista output of MCC -> Vista (default: remembered / <Documents>\\My Games\\Halo 2\\Maps)')
    _paths.add_common_options(r, mcc=True, vista=True, assembly=True, h2ek=True)
    r.add_argument('--dry-run', action='store_true', help='scan, classify and plan names only; nothing is written')
    r.add_argument('--only', default='', help='comma separated file-stem patterns (fnmatch), e.g. "lockout,h2.5*"')
    r.add_argument('--exclude', default='')
    r.add_argument('--kind', choices=('all', 'multiplayer', 'solo'), default='all')
    r.add_argument('--limit', type=int, default=None, help='convert at most N maps')
    r.add_argument('--force', action='store_true', help='vista2mcc: convert again even if the package already holds this exact source')
    r.add_argument('--overwrite', action='store_true', help='mcc2vista: replace existing output files')
    r.add_argument('--suffix', default='', help='mcc2vista: text appended to the output name, e.g. _mcc')
    r.add_argument('--matg', choices=('hud', 'link', 'local', 'auto'), default='hud')
    r.add_argument('--isolate', action='store_true', help='run every conversion in its own python process')
    r.add_argument('--jobs', type=int, default=1, help='parallel conversions (needs --isolate; max 4)')
    r.add_argument('--timeout', type=int, default=3600, help='seconds per map in --isolate mode')
    r.add_argument('--no-dedupe', action='store_true', help='do not skip byte-identical copies')
    r.add_argument('--no-hash', action='store_true', help='dry run only: do not hash the Vista maps (fast); GUIDs, GUID-suffixed names and up_to_date '
                                                          'decisions become PROVISIONAL and are marked as such.  Without it a dry run plans exactly what a real run does')
    r.add_argument('--keep-order', action='store_true', help='do not sort the map lists of the package')
    r.add_argument('--refresh-metadata', action='store_true', help='regenerate the MapInfo JSON of maps already in the package')
    r.add_argument('--package-title', default=None)
    r.add_argument('--report', default=None, help='write the JSON result here')
    r.add_argument('--settings', default=None, help='settings.json to use (default %%APPDATA%%\\H2MapConverter\\settings.json)')
    r.add_argument('--no-remember', action='store_true')
    r.add_argument('--list', action='store_true', help='dry run: print every planned conversion (with how its name was chosen) and every '
                                                      'skipped / invalid file with its reason (except files your own selection excludes)')
    r.add_argument('--quiet', action='store_true')
    r.add_argument('--cancel-file', default=None, help=argparse.SUPPRESS)       # used by the window program: no further map is started once this file exists
    r.add_argument('--announce', action='store_true', help=argparse.SUPPRESS)   # used by the window program: print 'converting <file> ...' also with --isolate
    t = sub.add_parser('settings', help='show / change the remembered choices')
    t.add_argument('--show', action='store_true')
    t.add_argument('--set', action='append', default=[], metavar='KEY=VALUE')
    t.add_argument('--settings', default=None)
    a = ap.parse_args(argv)
    if a.cmd == 'run' and a.no_hash and not a.dry_run:
        ap.error('--no-hash only applies to --dry-run (a real run must hash every source to derive its GUID)')
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:                                # noqa: BLE001
            pass
    if a.cmd == 'settings':
        st = Settings(a.settings)
        for kv in a.set:
            k, _, v = kv.partition('=')
            st.set(k.strip(), None if v.strip().lower() in ('', 'none', 'null') else v.strip())
        if a.set:
            print('saved' if st.save() else 'COULD NOT SAVE %s' % st.path)
        print('settings file: %s' % st.path)
        print(json.dumps(dict(st.data, **st.extra), indent=1))
        return 0
    if not os.path.exists(a.root):
        print('ERROR: %s does not exist' % a.root)
        return 2
    _paths.configure(settings_file=getattr(a, 'settings', None))
    _paths.configure_from_args(a)
    if a.cmd == 'scan':
        recs = MI.scan_folder(a.root, recursive=not a.no_recursive, deep=not a.no_deep)
        if a.json:
            print(json.dumps([r.to_dict() for r in recs], indent=1))
        else:
            for r in recs:
                print('%-8s %-16s %s  %s%s' % (r.status, r.code, r.label(), r.path, ('   [%s]' % r.reason) if r.reason else ''))
            print(dict(collections.Counter('%s/%s' % (r.status, r.code) for r in recs)))
        return 0
    st = Settings(a.settings)
    try:
        # every folder: the command line > an environment variable > settings.json > auto-detection (h2conv.paths); a missing output
        # folder is only an error once a conversion needs it (make_plan)
        direction = a.direction or st['direction'] or 'auto'
        if not a.dry_run:
            _paths.assembly_layout(a.assembly_dir)           # the Assembly layout files must be complete before the first map is touched
        mcc_out = _paths.mcc_out_dir(a.mcc_out, required=False)
        vista_out = _paths.vista_out_dir(a.vista_out, required=False)
        if direction == 'vista2mcc' and not mcc_out:
            raise RuntimeError(NEED_MCC_OUT)
        if direction == 'mcc2vista' and not vista_out:
            raise RuntimeError(NEED_VISTA_OUT)
        opts = Options(
            direction=direction,
            mcc_out=os.path.abspath(mcc_out) if mcc_out else None,
            vista_out=os.path.abspath(vista_out) if vista_out else None,
            vista_maps=_paths.vista_maps_dir(a.vista_maps, required=not a.dry_run),
            mcc_maps=_paths.pick('mcc_maps', a.mcc_maps)[0],                  # None = the installed game (auto-detected where it is needed)
            h2ek_maps=_paths.h2ek_maps_dir(a.h2ek_maps),                      # None = no H2EK donors
            assembly_dir=_paths.pick('assembly', a.assembly_dir)[0],          # None = the bundled copy
            dry_run=a.dry_run, only=_split(a.only), exclude=_split(a.exclude),
            kinds=('multiplayer', 'solo') if a.kind == 'all' else (a.kind,), limit=a.limit, force=a.force, overwrite=a.overwrite,
            suffix=a.suffix, matg=a.matg, isolate=a.isolate, jobs=a.jobs, timeout=a.timeout, hash_duplicates=not a.no_dedupe, hash_sources=not a.no_hash,
            sort_maps=not a.keep_order, refresh_metadata=a.refresh_metadata, package_title=a.package_title or st['package_title'],
            remember=not a.no_remember, recursive=not a.no_recursive, settings_path=a.settings, report=a.report, quiet=a.quiet, list_plan=a.list,
            cancel_file=a.cancel_file, announce=a.announce)
        res = run_batch(os.path.abspath(a.root), opts)
    except (RuntimeError, EX.PackageError) as ex:
        print('ERROR: %s' % ex)
        return 2
    if res.get('interrupted'):
        return 130
    return 1 if res['counts']['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
