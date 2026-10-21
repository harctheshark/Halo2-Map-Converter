# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""MCC (cache v13) -> Halo 2 Vista (cache v8) map converter: driver + CLI.

    py -3 -m h2conv.convert <in.map> <out.map | out_dir> [--name NAME] [--display-name TEXT] [--matg link|local|auto]
                            [--self-contained] [--report out.json] [--vista-maps DIR] [--mcc-maps DIR]
                            [--sound-workers N] [--no-sound-cache] [--sound-language N] [--no-pull-mcc-only]
                            [--havok-rcp stock|zero] [--no-taint-ignore] [--no-limits-check] [--no-deep-validate] [--quiet]
    (developer switches: --no-handlers --no-tag-handlers --standin-plan --limit N --groups a,b --debug --force-output-dir)

Pipeline:

    plan -> Ctx -> handlers.load_all + @pre_pass -> convert every 'convert'/'donor' entry (transcode.convert_tag / donor.import_tag)
         -> @post_pass -> place raw regions (sounds, geometry, [strings/names/locale], bitmaps) -> resolve tokens ->
         encode every Node at its final address -> write <stem>.tmp -> fsync -> rename to <stem>.map (atomic)
         -> vista_writer.validate_file + validate.validate_map + limits_check.check_map -> SHA-256 of the file (report)

Options and their trade-offs: README.md.
The stand-in planner / string table below are a fallback behind the same interfaces (used only if planner.py fails to import).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import collections
import hashlib
import importlib
import importlib.util
import json
import os
import pkgutil
import struct
import sys
import time
import traceback

from . import handlers as H
from . import limits as LIM
from . import paths as _paths
from . import tagtree as T
from . import transcode as X
from . import vista_writer as W
from .mcc_cache import MccCache, MccFormatError
from .vista_cache import VistaTag, VistaCache

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NULL = 0xFFFFFFFF
VISTA_BUILD = '11081.07.04.30.0934.main'
MP_MASK = 0x1400000
PAGE = 0x200


def _log(opts, msg):
    if not opts.get('quiet'):
        print(msg)
        sys.stdout.flush()


def peak_rss_mb():
    """Peak working set of this process in MB (Windows), 0 if unavailable."""
    try:
        class PMC(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                        ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.WinDLL('kernel32')
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        fn = k32.K32GetProcessMemoryInfo
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        fn.restype = ctypes.c_int
        fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.PeakWorkingSetSize / 1048576.0
    except Exception:
        return 0.0


# ================================================================================================
# stand-in planner + string table  (same interfaces as ARCHITECTURE section 6)
# ================================================================================================
class PlanEntry(object):
    __slots__ = ('src_map', 'src_index', 'group', 'name', 'action', 'out_index', 'out_datum', 'donor', 'redirect', 'reason')

    def __init__(self, src_map, src_index, group, name, action, out_index=None, out_datum=None, donor=None,
                 redirect=None, reason=''):
        self.src_map, self.src_index, self.group, self.name, self.action = src_map, src_index, group, name, action
        self.out_index, self.out_datum, self.donor, self.redirect, self.reason = out_index, out_datum, donor, redirect, reason

    def __repr__(self):
        return '<PlanEntry %s#%s %s %r %s -> %s>' % (self.src_map, self.src_index, self.group, self.name, self.action, self.out_index)


class StandInStrings(object):
    """Vista shared.map's strings as an exact prefix + appended strings.  sid(text) -> (len << 24) | index."""

    MAX_IDS = 24576

    def __init__(self, shared_strings):
        self.strings = list(shared_strings)
        self.shared_count = len(self.strings)
        self.by_text = {}
        for i, s in enumerate(self.strings):
            self.by_text.setdefault(s, i)
        self.appended = 0
        self.overflow = 0

    def sid(self, text):
        try:
            text.encode('latin-1')
        except UnicodeEncodeError:
            text = text.encode('latin-1', 'replace').decode('latin-1')
        i = self.by_text.get(text)
        if i is None:
            if len(self.strings) >= self.MAX_IDS:
                self.overflow += 1
                return 0
            i = len(self.strings)
            self.strings.append(text)
            self.by_text[text] = i
            self.appended += 1
        return (len(text) << 24) | i


class StandInPlan(object):
    """All local MCC tags -> convert (scnr first, ugh! last); MCC external tags -> link by (group, name) to Vista
    shared.map (dropped if Vista has no such tag); matg of an MP map -> the Vista shared matg."""

    def __init__(self, src, mcc_shared, vista, opts):
        self.src = src
        self.mcc_shared = mcc_shared
        self.vista = vista
        self.opts = opts
        self.map_type = src.header['scenario_type']
        self.mp = self.map_type == 1
        self.entries = []
        self.by_src = {}
        self.shared_placeholders = []
        self.stats = collections.Counter()
        self.dropped = []
        self._shared_by_name = {}
        self._shared_by_group = collections.defaultdict(list)
        self._local_by_name = {}
        self._cache = {}
        self._build()

    # ---- construction -----------------------------------------------------------------------------------
    def _build(self):
        src = self.src
        sh = None
        if self.mp:
            sh = self.vista.shared
            for t in sh.tags:
                if t.is_empty or t.index < 10000:
                    continue
                self.shared_placeholders.append(VistaTag(t.index, t.group, t.datum, 0, 0, t.name))
                if t.name:
                    self._shared_by_name.setdefault((t.group, t.name.lower()), t)
                self._shared_by_group[t.group].append(t)
            self.tag_count = len(sh.tags)
        local = [t for t in src.tags if (not t.is_null) and (not t.external)]
        first = [t for t in local if t.group == 'scnr']
        last = [t for t in local if t.group == 'ugh!']
        mid = [t for t in local if t.group not in ('scnr', 'ugh!')]
        order = first + mid + last
        limit = self.opts.get('limit')
        if limit:
            order = order[:int(limit)]
        # A map that shipped with MCC (compression fields set) is unmodified stock content: a local tag that Vista keeps in
        # shared.map is LINKED, not duplicated.  H2EK-built maps (fields 0) may carry modified copies -> kept local.
        stock_like = bool(src.was_compressed or src.header['compression_chunk_count'])
        link_dupes = self.opts.get('link_duplicates')
        if link_dupes is None:
            link_dupes = stock_like and self.mp
        for t in order:
            dup = self._shared_by_name.get((t.group, (t.name or '').lower())) if link_dupes else None
            if self.mp and t.group == 'matg' and self._shared_by_group.get('matg'):
                st = self._shared_by_group['matg'][0]
                e = PlanEntry('src', t.index, t.group, t.name, 'link', st.index, st.datum, reason='matg is shared in Vista MP maps')
                self.stats['matg_linked'] += 1
            elif dup is not None and t.group != 'scnr':
                e = PlanEntry('src', t.index, t.group, t.name, 'link', dup.index, dup.datum, reason='same (group, name) in Vista shared.map')
                self.stats['local_linked_to_shared'] += 1
            else:
                idx = len(self.entries)
                e = PlanEntry('src', t.index, t.group, t.name, 'convert', idx, W.make_datum(idx))
                self.entries.append(e)
                self._local_by_name.setdefault((t.group, (t.name or '').lower()), e)
            self.by_src[('src', t.index)] = e
        if not self.mp:
            self.tag_count = len(self.entries)
        # externals: link by name
        for t in src.tags:
            if t.is_null or not t.external:
                continue
            e = self._link_external(t, 'src')
            self.by_src[('src', t.index)] = e
        if len(self.entries) >= 10000:
            raise ValueError('%d local tags: Vista allows < 10000 local tag indices' % len(self.entries))

    def _link_external(self, t, src_map):
        st = self._shared_by_name.get((t.group, (t.name or '').lower()))
        if st is None:
            e = PlanEntry(src_map, t.index, t.group, t.name, 'drop', reason='no Vista shared tag with this (group, name)')
            self.dropped.append(e)
            self.stats['external_dropped'] += 1
            return e
        self.stats['external_linked'] += 1
        return PlanEntry(src_map, t.index, t.group, t.name, 'link', st.index, st.datum, reason='external -> Vista shared')

    # ---- queries ------------------------------------------------------------------------------------------
    def remap_datum(self, src_map, d):
        key = (src_map, d)
        if key in self._cache:
            return self._cache[key]
        r = self._remap(src_map, d)
        self._cache[key] = r
        return r

    def _remap(self, src_map, d):
        cache = self.src if src_map == 'src' else self.mcc_shared.get(src_map)
        if cache is None:
            return None
        t = cache.tag_by_datum(d)
        if t is None:
            self.stats['datum_unresolved'] += 1
            return None
        e = self.by_src.get((src_map, t.index))
        if e is None:
            # a tag of another MCC map (e.g. shared.map) used from a converted shared tag: match by name
            lk = self._local_by_name.get((t.group, (t.name or '').lower()))
            if lk is not None:
                return lk.out_datum
            st = self._shared_by_name.get((t.group, (t.name or '').lower()))
            return st.datum if st is not None else None
        if e.action in ('convert', 'link'):
            return e.out_datum
        if e.action == 'drop' and e.redirect is not None:
            return e.redirect.out_datum
        return None


# ================================================================================================
# handler loading (isolated: one broken handler module must not kill the run)
# ================================================================================================
def load_handlers(ctx):
    failed = []
    if H._loaded:
        return failed
    for m in pkgutil.iter_modules(H.__path__):
        if m.name.startswith('_'):
            continue
        try:
            importlib.import_module(H.__name__ + '.' + m.name)
        except Exception as ex:                                   # noqa: BLE001
            failed.append((m.name, repr(ex)))
            ctx.warn('handler_import_failed', '%s: %r' % (m.name, ex))
    H._loaded = True
    return failed


# ================================================================================================
# locale
# ================================================================================================
def _pad(b):
    return bytes(b) + b'\0' * ((-len(b)) % PAGE)


def build_locale(langs, base):
    """langs = 9 lists of (vista_string_id, utf8 bytes).  Returns (locale_blob, language_pack_252) with absolute
    file offsets assuming the blob starts at file offset `base`.  Layout = stock Vista (index table then data, each 0x200
    padded; an empty language points at the end of the blob)."""
    blob = bytearray()
    ents = []
    for items in langs:
        if not items:
            ents.append(None)
            continue
        idx = bytearray()
        data = bytearray()
        for sid, txt in items:
            idx += struct.pack('<II', sid & 0xFFFFFFFF, len(data))
            data += bytes(txt) + b'\0'
        ioff = base + len(blob)
        blob += _pad(idx)
        doff = base + len(blob)
        blob += _pad(data)
        ents.append((len(items), len(data), ioff, doff))
    end = base + len(blob)
    pack = bytearray()
    for e in ents:
        if e is None:
            pack += struct.pack('<7I', 0, 0, 0, 0, end, end, 0)
        else:
            pack += struct.pack('<7I', 0, 0, e[0], e[1], e[2], e[3], 0)
    return bytes(blob), bytes(pack)


def locale_from_mcc(ctx):
    """Language lists from the MCC map's locale area (header 0x2E4, MP stock maps), string ids mapped by text.
    A map without one (SP, H2EK-built) gets name/description strings for 8 languages."""
    langs = [[] for _ in range(9)]
    ents = []
    try:
        ents = ctx.src.locale_globals()
    except Exception as ex:                                       # noqa: BLE001
        ctx.warn('locale_read_failed', repr(ex))
    got = False
    for i, e in enumerate(ents[:9]):
        if not e['string_count']:
            continue
        try:
            items = ctx.src.locale_strings(e)
        except Exception as ex:                                   # noqa: BLE001
            ctx.warn('locale_read_failed', 'language %d: %r' % (i, ex))
            continue
        ctx.cur_src = 'src'
        langs[i] = [(ctx.remap_sid(sid), txt.encode('utf-8')) for sid, txt in items]
        got = True
    if not got:
        nm = ctx.src.header.get('internal_name') or 'converted'
        disp = nm.replace('_', ' ').title().encode('utf-8')
        ctx.warn('locale_synthesized', 'MCC map has no locale area: synthesized name/description for 8 languages')
        for i in range(8):
            langs[i] = [(ctx.sid('name'), disp), (ctx.sid('description'), b'Converted from Halo 2 MCC')]
    return langs


# ================================================================================================
# the driver
# ================================================================================================
def _real_planner_available():
    return importlib.util.find_spec('h2conv.planner') is not None


def _make_strings(vista, plan=None):
    """The output string table: planner's (plan.strings) > strings.StringTable > the stand-in."""
    s = getattr(plan, 'strings', None) if plan is not None else None
    if s is not None:
        return s, 'plan.strings'
    try:
        from . import strings as ST
        return ST.StringTable.from_vista_shared(vista.shared), 'strings.py'
    except Exception:                                             # noqa: BLE001
        return StandInStrings(vista.shared.all_strings()), 'stand-in'


def make_plan(src, mcc_shared, vista, opts, log=None):
    """Use planner.py when it exists and conforms (ARCHITECTURE section 6), else the stand-in plan.
    Returns (plan, strings, label)."""
    if _real_planner_available() and not opts.get('standin_plan'):
        try:
            from . import planner as PL
            plan = PL.build_plan(src, mcc_shared, vista, opts)
            for attr in ('entries', 'by_src', 'remap_datum', 'shared_placeholders', 'tag_count'):
                if not hasattr(plan, attr):
                    raise AttributeError('Plan has no %s' % attr)
            strs, how = _make_strings(vista, plan)
            return plan, strs, 'planner.py + ' + how
        except Exception as ex:                                   # noqa: BLE001
            if log:
                log('planner.py failed (%r) - using the stand-in plan' % (ex,))
    plan = StandInPlan(src, mcc_shared, vista, opts)
    strs, how = _make_strings(vista)
    return plan, strs, 'stand-in + ' + how


def _guard_output(src_path, out_path, vista_maps, opts):
    """Never overwrite the input, and never write into the stock Vista / MCC map folders unless --force-output-dir."""
    norm = lambda q: os.path.normcase(os.path.abspath(q))          # noqa: E731
    if norm(out_path) == norm(src_path):
        raise ValueError('output path is the input map: %s' % out_path)
    if opts.get('force_output_dir'):
        return
    outdir = norm(os.path.dirname(os.path.abspath(out_path)))
    for d in (vista_maps, os.path.dirname(os.path.abspath(src_path)), opts.get('mcc_maps_dir'), _paths.mcc_maps_dir(required=False)):
        if d and norm(d) == outdir:
            raise ValueError('refusing to write into %s (stock map folder / folder of the input map); choose another output '
                             'folder or pass --force-output-dir' % d)


MATG_MODES = {'link': 'link', 'local': 'convert', 'convert': 'convert', 'auto': 'auto', 'hud': 'hud'}


def resolve_output(out_arg, name=None, default_stem=None):
    """The file the converter writes.  `out_arg` is a file path or an existing/trailing-slash directory; `name` (--name) replaces
    the stem; a directory without --name gets <default_stem>.map (the map's internal name)."""
    out = os.path.abspath(out_arg)
    ext = os.path.splitext(out)[1].lower()
    is_dir = os.path.isdir(out) or str(out_arg).endswith(('\\', '/')) or (ext == '' and not os.path.isfile(out))
    if not is_dir and ext != '.map':
        raise ValueError('output file %r must end in .map (or pass a folder)' % out_arg)
    if is_dir:
        stem = name or default_stem
        if not stem:
            raise ValueError('output is a directory: pass --name or a file path')
        return os.path.join(out, stem + '.map')
    if name:
        return os.path.join(os.path.dirname(out), name + '.map')
    return out


def sha256_file(path, block=1 << 22):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            b = fh.read(block)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def write_atomic(img, out_path, overwrite=True):
    """<stem>.<pid>.<random>.tmp -> fsync -> rename onto <stem>.map: Cartographer's folder scan (`*.map`) never sees a half-written file.
    The temp name is new (made by paths.reserve_temp), so a file that was already in the folder is never replaced or deleted, and the temp file is
    removed again when anything fails.  (vista_writer.write_image itself goes through <tmp>.part.)"""
    tmp = _paths.reserve_temp(out_path)
    try:
        hdr = W.write_image(img, tmp)
        with open(tmp, 'rb+') as fh:
            fh.flush()
            os.fsync(fh.fileno())
        if not overwrite and os.path.exists(out_path):             # the output appeared while we were building: do not replace it
            raise FileExistsError('the output file %s already exists; pass --overwrite to replace it' % out_path)
        try:
            os.replace(tmp, out_path)
        except PermissionError as ex:
            raise PermissionError('cannot replace %s (is the game running with this map loaded?): %s' % (out_path, ex))
    except BaseException:
        _paths.discard_temp(tmp)
        raise
    return hdr


def convert_map(src_path, out_path, vista_maps=None, self_contained=False, report_path=None, opts=None):
    t_start = time.time()
    opts = dict(opts or {})
    opts['self_contained'] = bool(self_contained or opts.get('self_contained'))
    vista_maps = _paths.vista_maps_dir(vista_maps)           # None = H2CONV_VISTA_MAPS / settings.json / a search of your drives
    _paths.assembly_layout()                                 # the Assembly layout files are complete? (one readable error here, not a failure deep inside the planner)
    opts['vista_maps'] = vista_maps
    # the MCC side must be complete BEFORE planning: a missing textures.dat would make the planner compare every bitmap as "different"
    # (and link or copy the wrong tags) without a single error
    if not (opts.get('mcc_maps_dir') or opts.get('mcc_dat_dir')):
        # a map of a mod built against a custom shared.map but using none of its tags (mapinfo: ok, data_dir): its bitmaps / sounds are in the
        # mod's own textures.dat / sounds_*.dat (<mod>\halo2\h2_maps_win64_dx11), not in the installed game's
        try:
            from . import mapinfo as _MI
            _rec = _MI.identify(src_path)
            if _rec.status == 'ok' and _rec.data_dir:
                opts['mcc_maps_dir'] = opts['mcc_dat_dir'] = _rec.data_dir
                _log(opts, 'MCC data folder (the mod\'s own textures.dat / sounds_*.dat): %s' % _rec.data_dir)
            if _rec.status == 'ok' and _rec.custom_shared_map and not opts.get('shared_map'):
                opts['shared_map'] = _rec.custom_shared_map
                _log(opts, 'custom shared.map of the mod (its tags are compared with the stock one): %s' % _rec.custom_shared_map)
        except Exception:                                    # noqa: BLE001 - detection only; the installed game's folder is the fallback
            pass
    # the STOCK textures.dat / sounds_*.dat folder (custom-shared planning compares the mod's bitmaps and sounds with the stock ones; --mcc-maps names
    # the MOD's folder there, so this must not come from it)
    if not opts.get('stock_dat_dir'):
        try:
            opts['stock_dat_dir'] = _paths.stock_mcc_maps_dir()
        except Exception:                                    # noqa: BLE001
            opts['stock_dat_dir'] = None
    dat_dir = _paths.require_mcc_files(('textures.dat',), opts.get('mcc_maps_dir') or opts.get('mcc_dat_dir'))
    opts.setdefault('mcc_dat_dir', dat_dir)               # one folder for textures.dat / sounds_*.dat everywhere (planner, bitmaps, sounds)
    opts['matg'] = MATG_MODES.get(opts.get('matg') or 'hud', opts.get('matg'))
    # training is OFF unless --keep-training / H2CONV_KEEP_TRAINING: the planner then gives an MP map whose globals would be linked its own
    # training-less copy of Vista's matg + hudg (planner._setup_training_globals), gameplay.hudg_root empties a converted hudg
    opts['training_off'] = not (opts.get('keep_training') or os.environ.get('H2CONV_KEEP_TRAINING'))
    log = lambda m: _log(opts, m)                                  # noqa: E731
    name_opt = opts.get('name')
    if name_opt:
        why = LIM.check_map_name(name_opt)
        if why:
            raise ValueError('--name %r: %s' % (name_opt, why))
    if opts.get('display_name') and len(opts['display_name']) > LIM.MAP_NAME_MAX:
        raise ValueError('--display-name is longer than %d characters' % LIM.MAP_NAME_MAX)
    # (the output guard runs below, after resolve_output has made the real file name: `out_path` may be a folder, and the folder that holds
    #  that folder is allowed to be the folder of the input map)
    timings = collections.OrderedDict()
    report = collections.OrderedDict()
    report['input'] = src_path

    def tick(name, t0):
        timings[name] = round(time.time() - t0, 2)

    # ---- open --------------------------------------------------------------------------------------------
    t0 = time.time()
    src = MccCache(src_path)
    out_path = resolve_output(out_path, name_opt, src.header['internal_name'])
    _guard_output(src_path, out_path, vista_maps, opts)
    _paths.refuse_existing(out_path, opts, report_path)            # R16: before any work is done
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    stem = os.path.splitext(os.path.basename(out_path))[0]
    if len(stem) > LIM.MAP_STEM_MAX:
        log('WARNING: the file stem %r is longer than %d characters - Cartographer skips such files in its custom map list' % (stem, LIM.MAP_STEM_MAX))
    report['output'] = out_path
    # the shared.map the input was built against: the explicit one (--shared-map / mapinfo's custom_shared_map), else the file with the creation time
    # the map recorded (header 0x50..0x60) among the usual folders, else the first one found (as before)
    mcc_shared = X.MccShared([opts.get('mcc_maps_dir'), os.path.dirname(os.path.abspath(src_path))], shared_map=opts.get('shared_map'),
                             want_times=src.header['shared_creation_time'])
    vista = X.VistaRefs(vista_maps)
    map_type = src.header['scenario_type']
    mask = MP_MASK if map_type == 1 else 0
    log('input  : %s (%d tags, type %s, %d strings)' % (os.path.basename(src_path), len(src.tags), src.header['scenario_type_name'],
                                                      src.header['string_table_count']))
    plan, strs, planner_name = make_plan(src, mcc_shared, vista, opts, log)
    report['planner'] = planner_name
    report['options'] = {k: v for k, v in sorted(opts.items()) if k in (
        'name', 'display_name', 'matg', 'self_contained', 'pull_mcc_only', 'sound_workers', 'no_sound_cache', 'sound_language',
        'havok_rcp', 'only_groups', 'no_handlers', 'limit', 'taint_ignore', 'keep_training') and (v not in (None, False) or k == 'taint_ignore' and v == ())}
    ctx = X.Ctx(src, mcc_shared, vista, plan, strs, opts=opts, map_type=map_type)
    ctx.extra['out_path'] = out_path
    ctx.extra['converted'] = {}
    tick('open+plan', t0)
    log('plan   : %s, %d local tags to emit, tag_count %d, %d shared placeholders' % (
        planner_name, len(plan.entries), plan.tag_count, len(plan.shared_placeholders)))
    try:
        from . import shadows as SHD
        report['shadows'] = SHD.summary(plan)
        sh = report['shadows']
        if sh['count']:
            log('shadows: %d local tag(s) share (group, name) with a shared placeholder (stock Vista maps: none); %d are named in Cartographer\'s '
                'source (its name-keyed patches hit the local copy)%s' % (
                    sh['count'], sh['cartographer_keyed_count'], '' if sh['cartographer_source_found'] else ' [source folder not found]'))
        tn = (getattr(plan, 'stats', {}) or {}).get('taint_ignored')
        if tn:
            log('taint   : %d modified tag(s) (%s) do not taint their dependents: %d dependent tags are linked instead of copied' % (
                tn, ', '.join((getattr(plan, 'notes', {}) or {}).get('taint_ignored', [])),
                plan.stats.get('taint_ignored_dependents_linked', 0)))
    except Exception as ex:                                       # noqa: BLE001
        report['shadows'] = 'shadow summary failed: %r' % (ex,)

    # ---- handlers ----------------------------------------------------------------------------------------
    t0 = time.time()
    if not opts.get('no_handlers'):
        failed = load_handlers(ctx)
        for nm, why in failed:
            log('  handler module %s failed to import: %s' % (nm, why))
    for _p, nm, fn in (H.PRE if not opts.get('no_handlers') else []):
        try:
            fn(ctx)
        except Exception as ex:                                    # noqa: BLE001
            ctx.warn('pre_pass_failed', '%s: %r' % (nm, ex))
            log('  pre_pass %s FAILED: %r' % (nm, ex))
            if opts.get('debug'):
                traceback.print_exc()
    tick('handlers+pre', t0)

    # ---- convert ------------------------------------------------------------------------------------------
    t0 = time.time()
    try:
        from . import donor as DONOR
    except Exception:                                               # noqa: BLE001
        DONOR = None
    converted = ctx.extra['converted']
    failures = []
    only = opts.get('only_groups')
    for n, e in enumerate(plan.entries):
        if e.action not in ('convert', 'donor'):
            continue
        if only and e.group not in only:
            continue
        try:
            if e.action == 'donor' and DONOR is not None:
                root = DONOR.import_tag(ctx, e)
            else:
                root = X.convert_tag(ctx, e)
        except Exception as ex:                                     # noqa: BLE001
            failures.append({'index': e.out_index, 'group': e.group, 'name': e.name, 'error': repr(ex)[:300]})
            ctx.warn('convert_failed', '%s %s: %r' % (e.group, e.name, ex), tag=e.name)
            if opts.get('debug'):
                traceback.print_exc()
            root = None
        if root is None:
            try:
                root = X.empty_tag_node(ctx, e.group)
                ctx.stats['empty_substitutes'] += 1
            except Exception:                                       # noqa: BLE001
                root = None
        converted[e.out_index] = root
        if (n + 1) % 1000 == 0:
            log('  converted %d/%d tags (%.0fs)' % (n + 1, len(plan.entries), time.time() - t0))
    tick('convert', t0)
    log('convert: %d tags, %d failed, %d raw payloads (%.1f MB)' % (
        len(converted), len(failures), len(ctx.raws), sum(len(d) for _k, d in ctx.raws) / 1048576.0))

    t0 = time.time()
    if not opts.get('no_handlers'):
        for _p, nm, fn in H.POST:
            try:
                fn(ctx, converted)
            except Exception as ex:                                 # noqa: BLE001
                ctx.warn('post_pass_failed', '%s: %r' % (nm, ex))
                log('  post_pass %s FAILED: %r' % (nm, ex))
                if opts.get('debug'):
                    traceback.print_exc()
    tick('post', t0)

    # ---- build the file -------------------------------------------------------------------------------------
    t0 = time.time()
    hdr_info = build_file(ctx, plan, strs, converted, src, vista, vista_maps, mask, map_type, out_path, opts, log)
    tick('build', t0)
    report.update(hdr_info)

    # ---- validate ------------------------------------------------------------------------------------------
    t0 = time.time()
    problems = W.validate_file(out_path)
    report['validate_file'] = problems
    log('validate_file: %s' % ('OK (0 problems)' if not problems else '%d PROBLEMS: %s' % (len(problems), problems[:5])))
    try:
        if opts.get('no_deep_validate'):
            raise ImportError('skipped (--no-deep-validate)')
        from . import validate as VAL
        vres = VAL.validate_map(out_path)
        if hasattr(vres, 'to_dict'):
            d = vres.to_dict()
            report['validate_map'] = {'ok': d['ok'], 'errors': d['errors'], 'warnings': d['warnings'], 'notes': d['notes'],
                                      'codes': {c: {'level': v['level'], 'count': v['count'], 'examples': v['examples'][:3]}
                                                for c, v in d['codes'].items()}}
            log('validate_map : %d error(s), %d warning(s), %d note(s)' % (d['errors'], d['warnings'], d['notes']))
            if opts.get('verbose_validate') or d['errors']:
                log(vres.format_text(examples=3, levels=('error', 'warning')))
        else:
            report['validate_map'] = vres if isinstance(vres, (list, dict, str, int, float, bool)) or vres is None else repr(vres)
    except ImportError as ex:
        report['validate_map'] = 'validate.py not available / skipped: %s' % ex
    except Exception as ex:                                         # noqa: BLE001
        report['validate_map'] = 'validate.validate_map failed: %r' % (ex,)
        log('validate_map unavailable: %r' % (ex,))
    tick('validate', t0)

    # ---- engine-limit check + identity of the file ---------------------------------
    t0 = time.time()
    if opts.get('no_limits_check'):
        report['limits_check'] = 'skipped (--no-limits-check)'
    else:
        try:
            from . import limits_check as LC
            lr = LC.check_map(out_path, profile='mp_custom' if map_type in (0, 1) else 'engine', deep=True, maps_dir=vista_maps)
            report['limits_check'] = {'ok': lr.ok(), 'errors': lr.count('ERROR'), 'warnings': lr.count('WARN'), 'info': lr.count('INFO'),
                                      'items': [list(i) for i in lr.items], 'facts': lr.facts}
            log('limits_check : %d error(s), %d warning(s), %d info' % (lr.count('ERROR'), lr.count('WARN'), lr.count('INFO')))
            for sev, code, msg in lr.items:
                if sev in ('ERROR', 'WARN'):
                    log('  %-5s %s %s' % (sev, code, msg))
        except Exception as ex:                                     # noqa: BLE001
            report['limits_check'] = 'limits_check failed: %r' % (ex,)
            log('limits_check unavailable: %r' % (ex,))
    if opts.get('no_invariants'):
        report['invariants'] = 'skipped'
    else:
        try:
            from . import invariants as INV
            fi = INV.check_map(out_path, maps_dir=vista_maps)
            report['invariants'] = fi.to_dict()
            log('invariants   : %d error(s), %d warning(s)  (%d tags checked)' % (fi.errors, fi.warnings, fi.counts.get('tags checked', 0)))
            for lv, code, msg in fi.items:
                log('  %-7s %-22s %s' % (lv, code, msg[:300]))
        except Exception as ex:                                     # noqa: BLE001
            report['invariants'] = 'invariants check failed: %r' % (ex,)
            log('invariants unavailable: %r' % (ex,))
    report['sha256'] = sha256_file(out_path)
    tick('limits+invariants+sha256', t0)

    timings['total'] = round(time.time() - t_start, 2)
    report['timings_s'] = timings
    report['peak_rss_mb'] = round(peak_rss_mb(), 1)
    report['failures'] = failures
    report['stats'] = {k: v for k, v in sorted(ctx.stats.items()) if not k.startswith(('resize:', 'warn:'))}
    report['resize_fields'] = {k[7:]: v for k, v in sorted(ctx.stats.items()) if k.startswith('resize:')}
    report['warnings'] = {code: {'count': ctx.stats.get('warn:' + code, len(lst)),
                                 'examples': ['%s%s' % (m, (' [%s]' % t) if t else '') for m, t in lst[:8]]}
                          for code, lst in ctx.warnings.items()}
    report['plan_stats'] = dict(getattr(plan, 'stats', {}) or {})
    if hasattr(plan, 'report') and hasattr(plan, 'audit'):
        try:
            report['plan'] = plan.report(60)
            n_refs, bad = plan.audit()
            report['plan_audit'] = {'references': n_refs, 'problems': dict(collections.Counter(b[0] for b in bad)),
                                    'unexpected': [list(b) for b in bad if b[0] in ('no-entry', 'null')][:20]}
            if report['plan_audit']['unexpected']:
                log('plan audit: %d reference(s) of kept tags have no output tag, e.g. %s' % (
                    len(report['plan_audit']['unexpected']), report['plan_audit']['unexpected'][0]))
        except Exception as ex:                                     # noqa: BLE001
            report['plan'] = 'plan report failed: %r' % (ex,)
    report['out_size'] = os.path.getsize(out_path)
    if report_path:
        with open(report_path, 'w', encoding='utf-8') as fh:
            json.dump(_paths.report_for_file(report), fh, indent=1, default=str)
    print_report(report, log)
    return report, ctx


def header_secondary_ugh(ctx, plan, map_type):
    """Header 0x2E4.  The sound handler publishes the right value for the topology it built (ctx.sound_info['header_gestalt_datum']:
    multiplayer = the map's own ugh! as the secondary gestalt, solo / main menu = 0xFFFFFFFF because the local matg already points
    at the ONE local ugh!).  Without a sound handler result: the plan's value
    (planner.Plan.secondary_ugh_datum applies the same rule), else the stand-in plan's own gestalt for multiplayer, else -1."""
    si = getattr(ctx, 'sound_info', None)
    if si is not None and 'header_gestalt_datum' in si:
        v = si['header_gestalt_datum']
        pv = getattr(plan, 'secondary_ugh_datum', None)
        if pv is not None and pv != v:
            ctx.warn('header_datum_mismatch', 'secondary ugh!: plan 0x%X vs sound handler 0x%X (using the sound handler)' % (pv & 0xFFFFFFFF, v & 0xFFFFFFFF))
        return v
    pv = getattr(plan, 'secondary_ugh_datum', None)
    if pv is not None:
        return pv
    if map_type != 1:
        return NULL
    ugh = [e for e in plan.entries if e.group == 'ugh!' and e.action in ('convert', 'donor') and e.src_map == 'src']
    return ugh[0].out_datum if ugh else NULL


def build_file(ctx, plan, strs, converted, src, vista, vista_maps, mask, map_type, out_path, opts, log):
    """Place raws, strings, names, locale, bitmaps and the tag region; write and return header info."""
    shared_ct = W.read_shared_creation_times(vista_maps)
    if map_type == 1:
        uses = (1, 1, 0)
    elif map_type == 0:
        uses = (1, 1, 1)
    else:
        uses = (0, 0, 0)
    # --self-contained keeps the flags: the planner still lists matg/ugh! placeholders for MP maps, and
    # the validator requires mainmenu+shared to be flagged whenever shared placeholders exist
    secondary_ugh = header_secondary_ugh(ctx, plan, map_type)
    scen_name = src.header['scenario_name'] or src.header['internal_name']
    name = opts.get('name') or (src.header['internal_name'] or os.path.splitext(os.path.basename(scen_name.replace('\\', '/')))[0])[:31]
    # header name (0x1A4) = what Cartographer's custom map list shows whenever the vanilla list validator fails, i.e. for every map with a
    # LOCAL matg (the default since the training-off change): the MCC MapInfo title / --display-name instead of the file stem.  The file
    # name is untouched; an explicit `--name` (CLI) keeps its value unless --display-name is given.  Stock maps (no ModInfo.json) keep the stem.
    hdr_name, hdr_name_src = name, 'stem'
    if map_type == 1 and (opts.get('display_name') or not opts.get('name_explicit')):
        from .handlers import mp_level_names as _MN
        hdr_name, hdr_name_src = _MN.list_header_name(getattr(src, 'source_path', None), opts, name)
    hdr = W.new_header(hdr_name, scen_name, map_type=map_type, build=VISTA_BUILD, uses_shared=uses,
                       shared_creation_time=shared_ct, secondary_ugh_tag_index=secondary_ugh)
    groups = W.groups_from_stock(os.path.join(vista_maps, 'shared.map'))
    b = W.VistaBuilder(hdr, groups, tag_offset_mask=mask)

    # -- raw sounds + geometry
    ptr = [None] * len(ctx.raws)
    for kind in ('sound', 'geometry'):
        for i, (k, data) in enumerate(ctx.raws):
            if k != kind:
                continue
            off = b.add_sound(data) if kind == 'sound' else b.add_geometry(data)
            ptr[i] = off
    # -- strings + tag names (final: all handlers ran)
    tag_names = [None] * plan.tag_count
    for e in plan.entries:
        if e.action in ('convert', 'donor'):
            tag_names[e.out_index] = e.name or None
    for t in plan.shared_placeholders:
        if t.index < len(tag_names):
            tag_names[t.index] = t.name
    # locale (hooks, in priority order): ctx.locale_langs = 9 lists of (vista_sid, utf8 bytes) | ctx.locale_blob (+
    # ctx.language_pack, offsets relative to the blob start) | the MCC map's own locale area.  Built BEFORE the string table
    # is frozen because mapping the string ids may append strings.
    loc_mod = sys.modules.get('h2conv.handlers.locale')
    mode = 'mcc'
    langs = None
    if getattr(ctx, 'locale_langs', None) is not None:
        mode, langs = 'langs', ctx.locale_langs
    elif ctx.locale_blob is not None:
        mode = 'module' if (loc_mod is not None and hasattr(loc_mod, 'apply_to_builder')) else 'blob'
    else:
        langs = locale_from_mcc(ctx)
    b.set_strings(strs.strings)
    b.set_tag_names(tag_names)
    base = b.locale_base
    if mode == 'module':
        # handlers/locale.py: sets the locale on the builder AND writes the absolute offsets into local matg nodes
        loc_mod.apply_to_builder(ctx, b)
    elif mode == 'blob':
        blob = bytes(ctx.locale_blob)
        pack = None
        if ctx.language_pack is not None:
            pack = bytearray(ctx.language_pack)
            for i in range(9):                                      # handler offsets are relative to the blob start
                for o in (16, 20):
                    v = struct.unpack_from('<I', pack, 28 * i + o)[0]
                    struct.pack_into('<I', pack, 28 * i + o, v + base)
            pack = bytes(pack)
        b.set_locale(blob, pack)
    else:
        blob, pack = build_locale(langs, base)
        b.set_locale(blob, pack)
    ctx.stats['locale_mode:' + mode] += 1
    # -- bitmaps
    for i, (k, data) in enumerate(ctx.raws):
        if k == 'bitmap':
            ptr[i] = b.add_bitmap_pixels(data)
    for i, p in enumerate(ptr):
        if p is None:
            raise RuntimeError('raw payload %d (%s) was never placed' % (i, ctx.raws[i][0]))
        if p >= 1 << 30:
            raise RuntimeError('raw offset 0x%X does not fit in 30 bits' % p)
    ctx._resolved = ptr
    # -- tag region
    tag_count = plan.tag_count
    inst = [VistaTag(i, '', NULL, 0, 0) for i in range(tag_count)]
    for t in plan.shared_placeholders:
        if t.index < tag_count:
            inst[t.index] = VistaTag(t.index, t.group, t.datum, 0, 0, t.name)
    cursor = b.tag_region_data_offset(tag_count)
    start = cursor
    chunks = []
    enc_fail = 0
    for e in sorted((x for x in plan.entries if x.action in ('convert', 'donor')), key=lambda x: x.out_index):
        root = converted.get(e.out_index)
        data = b''
        if root is not None:
            try:
                data = T.encode_tag(root, ctx.lay_vista[e.group], mask + cursor, ctx.align, resolve=ctx.resolve)
            except Exception as ex:                                 # noqa: BLE001
                enc_fail += 1
                ctx.warn('encode_failed', '%s %s: %r' % (e.group, e.name, ex), tag=e.name)
                if opts.get('debug'):
                    traceback.print_exc()
        if len(data) % 4:
            data += b'\0' * (4 - len(data) % 4)
        inst[e.out_index] = VistaTag(e.out_index, e.group, e.out_datum, (mask + cursor) if data else 0, len(data), e.name)
        chunks.append(data)
        cursor += len(data)
    tag_data = b''.join(chunks)
    scn = src.scenario_datum
    ctx.cur_src = 'src'
    scn_out = ctx.remap_datum(scn)
    glb_out = ctx.remap_datum(src.globals_datum) if src.globals_datum not in (NULL, None) else NULL
    for attr, mine, label in (('scenario_datum', scn_out, 'scenario'), ('globals_datum', glb_out, 'globals')):
        pv = getattr(plan, attr, None)
        if pv is not None and pv != mine:
            ctx.warn('header_datum_mismatch', '%s datum: plan 0x%X vs remap 0x%X (using the plan)' % (label, pv & 0xFFFFFFFF, mine))
            if attr == 'scenario_datum':
                scn_out = pv
            else:
                glb_out = pv
    if scn_out == NULL:
        raise RuntimeError('scenario tag has no output datum')
    b.set_tags(inst, tag_data, scn_out, glb_out, 0)
    img = b.build_image()
    img.mopp_checksum = 'compute' if map_type == 1 else 0
    write_atomic(img, out_path, opts.get('overwrite', True))
    return {'name': name, 'header_name': hdr_name, 'header_name_source': hdr_name_src, 'scenario_path': scen_name, 'type': map_type, 'uses_shared': uses, 'tag_count': tag_count,
            'local_tags': len(plan.entries), 'tag_data_bytes': len(tag_data), 'strings': len(strs.strings),
            'raw_sounds': sum(1 for k, _d in ctx.raws if k == 'sound'),
            'raw_geometry': sum(1 for k, _d in ctx.raws if k == 'geometry'),
            'raw_bitmaps': sum(1 for k, _d in ctx.raws if k == 'bitmap'),
            'encode_failures': enc_fail, 'secondary_ugh': hex(secondary_ugh), 'scenario_datum': hex(scn_out),
            'globals_datum': hex(glb_out)}


def print_report(rep, log):
    log('--- conversion report ---')
    for k in ('output', 'name', 'header_name', 'header_name_source', 'scenario_path', 'type', 'tag_count', 'local_tags', 'tag_data_bytes', 'strings', 'raw_sounds',
              'raw_geometry', 'raw_bitmaps', 'out_size', 'sha256', 'peak_rss_mb'):
        if k in rep:
            log('  %-16s %s' % (k, rep[k]))
    log('  timings (s)      %s' % dict(rep['timings_s']))
    if rep['failures']:
        log('  FAILED tags: %d (first: %s)' % (len(rep['failures']), rep['failures'][0]))
    st = rep['stats']
    gen = {k: v for k, v in st.items() if k.startswith(('generic:', 'tag_handler:'))}
    log('  tags by path     %s' % gen)
    log('  other stats      %s' % {k: v for k, v in st.items() if k not in gen})
    log('  warnings by code:')
    for code, w in rep['warnings'].items():
        log('    %-22s %6d   e.g. %s' % (code, w['count'], w['examples'][0] if w['examples'] else ''))
    log('  validate_file    %s' % (rep['validate_file'] or 'OK'))


def report_ok(rep):
    """True when the written file passed every check: structural validation, deep validation, engine limits, no failed tag."""
    if rep.get('validate_file') or rep.get('failures') or rep.get('encode_failures'):
        return False
    vm = rep.get('validate_map')
    if isinstance(vm, dict) and vm.get('errors'):
        return False
    lc = rep.get('limits_check')
    if isinstance(lc, dict) and lc.get('errors'):
        return False
    iv = rep.get('invariants')
    if isinstance(iv, dict) and iv.get('errors'):
        return False
    return True


def main(argv=None):
    import argparse
    from . import __version__
    ap = argparse.ArgumentParser(
        prog='h2conv.convert', formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Convert a Halo 2 MCC multiplayer/solo map (cache v13) into a Halo 2 Vista map (cache v8) for Project Cartographer.',
        epilog='example:  py -3 -m h2conv.convert "...\\h2_maps_win64_dx11\\headlong.map" "...\\My Games\\Halo 2\\Maps" '
               '--name headlong_mcc\n(see README.md for the options)')
    ap.add_argument('input', help='MCC .map (stock compressed/decompressed maps and H2EK-built maps are accepted)')
    ap.add_argument('output', help='output .map path, or a folder (then the file is <name>.map)')
    ap.add_argument('--name', default=None, metavar='NAME',
                    help='header internal name AND output file stem (<=31 chars: letters, digits, _ - space).  Use it so the converted '
                         'map does not share the file name of the stock Vista map; the scenario path is left unchanged')
    ap.add_argument('--display-name', default=None, metavar='TEXT',
                    help='text of the localized map name shown in the custom map list (all 8 languages), <=31 chars')
    ap.add_argument('--matg', choices=('hud', 'link', 'local', 'auto'), default='hud',
                    help="globals tag: hud (default) = the input's globals converted locally when the map's HUD text (matg -> hudg -> unic, e.g. "
                         "custom pickup prompts) is customised, else Vista's globals; link = Vista's globals, the MCC edits to the globals/HUD "
                         "are NOT carried over; local = always convert the input's globals; auto = local if the input's globals differ from "
                         "the stock MCC ones.  Where Vista's globals are used, training off (the default) gives the map its OWN byte-faithful "
                         "copy of Vista's matg + hudg without the training block (a LOCAL globals tag, like chronopolis); only with "
                         "--keep-training are they the shared tags themselves (full custom-map list entry)")
    ap.add_argument('--keep-training', action='store_true',
                    help="keep the tutorial prompts ('hold X to ...'); by default EVERY multiplayer map gets no hudg training entries, "
                         "because the prompts draw as an empty square on converted maps (a map whose matg would be linked gets its own "
                         "training-less copy of Vista's matg + hudg instead; with this switch --matg link/hud use the shared tags unchanged)")
    ap.add_argument('--self-contained', action='store_true', help='do not link to the Vista shared.map (copies every dependency; experimental, much larger file)')
    ap.add_argument('--no-pull-mcc-only', action='store_true',
                    help='drop MCC-only objects (mongoose, falcon, ...) that a stock MCC map references instead of converting them: '
                         'reproduces the content of the stock Vista map of the same name')
    ap.add_argument('--no-taint-ignore', action='store_true',
                    help="copy every tag that references a modified tag, including the dependents of rasterizer\\linear_corner_fade "
                         "(256x256 in the H2EK kit tags, 16x16 in every shipped map; by default it does not taint, so ~300 tags are "
                         "linked to Vista's shared tags instead of copied)")
    _paths.add_common_options(ap, mcc=True, vista=True, assembly=True)
    ap.add_argument('--shared-map', default=None, metavar='FILE',
                    help='the CUSTOM shared.map (Steam-workshop mod) this multiplayer map was built against; default: found automatically (the file with the '
                         'creation time the map recorded, next to the map / in <mod>\\halo2\\h2_maps_win64_dx11).  Its tags are compared with the '
                         'stock MCC shared.map: the changed ones and everything that references them are copied, the rest is linked to Vista\'s')
    ap.add_argument('--overwrite', action='store_true', help='replace the output file (and the --report file) when it already exists; by default an existing file is never touched')
    ap.add_argument('--version', action='version', version='Halo 2 Map Converter %s' % __version__)
    ap.add_argument('--report', default=None, help='write the conversion report (JSON; includes the SHA-256 of the output)')
    ap.add_argument('--sound-workers', type=int, default=1, choices=(1, 2, 3, 4), help='processes for the Opus -> ADPCM transcode of a cold sound cache')
    ap.add_argument('--no-sound-cache', action='store_true', help='do not use the sound cache (the sound_cache folder inside the work folder)')
    ap.add_argument('--sound-language', type=int, default=0, help='language index of the sound data (0 = English)')
    ap.add_argument('--havok-rcp', choices=('stock', 'zero'), default='stock', help='Havok Runtime Code Pointer values (both are accepted by the engine)')
    ap.add_argument('--no-limits-check', action='store_true', help='skip h2conv.limits_check and h2conv.invariants after the build')
    ap.add_argument('--no-deep-validate', action='store_true', help='skip validate.validate_map (vista_writer.validate_file still runs)')
    ap.add_argument('--force-output-dir', action='store_true', help='allow writing into the stock map folders / the input folder')
    ap.add_argument('--quiet', action='store_true')
    dev = ap.add_argument_group('developer switches')
    dev.add_argument('--no-handlers', action='store_true', help='generic conversion only (skip every registered handler)')
    dev.add_argument('--no-tag-handlers', action='store_true', help='skip tag handlers (node handlers still run)')
    dev.add_argument('--standin-plan', action='store_true', help='force the built-in stand-in planner')
    dev.add_argument('--limit', type=int, default=None, help='stand-in planner: only the first N local tags (smoke tests)')
    dev.add_argument('--groups', default=None, help='comma separated groups to convert (others become empty slots)')
    dev.add_argument('--debug', action='store_true')
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):             # tag / string text may contain characters the console code page lacks
        try:
            stream.reconfigure(errors='replace')
        except Exception:                                # noqa: BLE001
            pass
    _paths.configure_from_args(a)
    opts = {'quiet': a.quiet, 'no_handlers': a.no_handlers, 'no_tag_handlers': a.no_tag_handlers or a.no_handlers,
            'no_node_handlers': a.no_handlers,
            'standin_plan': a.standin_plan, 'limit': a.limit, 'debug': a.debug, 'mcc_maps_dir': a.mcc_maps,
            'force_output_dir': a.force_output_dir, 'overwrite': a.overwrite, 'no_deep_validate': a.no_deep_validate, 'no_limits_check': a.no_limits_check,
            'no_invariants': a.no_limits_check,
            'matg': a.matg, 'name': a.name, 'name_explicit': bool(a.name), 'display_name': a.display_name, 'sound_workers': a.sound_workers,
            'no_sound_cache': a.no_sound_cache, 'sound_language': a.sound_language, 'havok_rcp': a.havok_rcp,
            'keep_training': a.keep_training}
    if a.no_pull_mcc_only:
        opts['pull_mcc_only'] = False
    if a.no_taint_ignore:
        opts['taint_ignore'] = ()
    if a.mcc_maps:
        opts['mcc_dat_dir'] = a.mcc_maps
    if a.shared_map:
        opts['shared_map'] = os.path.abspath(a.shared_map)
    if a.groups:
        opts['only_groups'] = set(a.groups.split(','))
    try:
        a.vista_maps = _paths.vista_maps_dir(a.vista_maps)
        rep, _ctx = convert_map(a.input, a.output, a.vista_maps, a.self_contained, a.report, opts)
    except MccFormatError as ex:                         # not an MCC map at all (a Vista map, any other file), or a damaged / truncated one
        why = str(ex)
        for pre in (a.input, os.path.abspath(a.input)):
            if why.startswith(pre + ': '):
                why = why[len(pre) + 2:]
        print('ERROR: %s is not a valid Halo 2 MCC (cache version 13) map (%s) - did you mean `h2conv rev` (Vista to MCC)?' % (a.input, why))
        return 2
    except (ValueError, PermissionError, FileNotFoundError, FileExistsError, _paths.PathsError) as ex:
        print('ERROR: %s' % ex)
        return 2
    return 0 if report_ok(rep) else 1


if __name__ == '__main__':
    sys.exit(main())
