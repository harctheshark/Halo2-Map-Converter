# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Original Xbox Halo 2 multiplayer maps -> Halo 2 Vista, or -> Vista -> Halo 2 MCC (Excession mod packages).

    py -3 -m h2conv xbox <map or folder> [...] --to vista --out DIR [--stock-xbox-maps DIR] [--overwrite]
    py -3 -m h2conv xbox <map or folder> [...] --to mcc   --out DIR [--stock-xbox-maps DIR] [--overwrite]

--to vista   one Vista .map per Xbox map, named after the map and its pack: bulldog.map from the pack folder "Phantom v1" ->
             Bulldog_PhantomV1.map; the maps of the original game -> Lockout_OG.map.  With one input map, --out may also be a file name
             ending in .map.  The in-game name is the map's own name ("Dawn_of_Warfare.map" -> "Dawn Of Warfare").
--to mcc     Xbox -> Vista -> MCC in one run.  The Vista maps made on the way live in the converter's cache folder (xbox_jobs) and are
             deleted when the run ends.  The result is one Excession mod package per source pack, inside the folder given by --out:

               * a pack of up to 45 maps is one package, titled like the pack ("Phantom V1");
               * a bigger pack is split into the fewest packages of at most 45 maps, as equal in size as possible, the maps in alphabetical
                 order of their in-game names: "Killtrocity V1 Part 1", "Killtrocity V1 Part 2" ... (the folder has the same name);
               * the map titles in the packages are the maps' in-game names;
               * the description of a package is "<Package title> Map Pack Ported From OG Xbox to Halo MCC.", two blank lines, "Map Names:",
                 then one in-game map title per line.

             A package folder that already exists is never built into again (that would add a second, renamed copy of every map): the run
             stops before it starts, unless --overwrite is given, which replaces the package folder with a freshly built one.

Which files count: a multiplayer map of the original Xbox Halo 2.  Campaign maps, mainmenu / shared / single_player_shared, Halo 3 and Reach
files, Vista and MCC maps are listed and skipped with the reason.  A truncated download is reported and not converted.

Exit codes: 0 done, 1 a conversion failed, 2 a usage / folder problem (nothing was converted), 3 nothing converted because every input was
skipped (not a Halo 2 Xbox map, or not a multiplayer map), 4 an input could not be used (for example a truncated download).

Your original Xbox maps (--stock-xbox-maps, H2CONV_STOCK_XBOX_MAPS, or "stock_xbox_maps" in settings.json) are optional; without them some maps
convert with lower quality, and the summary says so once.  Nothing here makes a network connection.
"""
import collections
import os
import re
import secrets
import shutil
import sys
import threading

from . import cache_admin as _cache
from . import excession as EX
from . import mapinfo as MI
from . import paths as _paths

OD = collections.OrderedDict
MAX_MAPS_PER_PACKAGE = 45                  # an Excession mod package holds at most this many maps (a bigger pack is split)
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_SKIPPED, EXIT_BAD_INPUT = 0, 1, 2, 3, 4
XBOX_DIRECTIONS = ('xbox2vista', 'xbox2mcc')
DESCRIPTION_HEAD = '%s Map Pack Ported From OG Xbox to Halo MCC.\n\n\nMap Names:\n'     # the first line, two blank lines, then the names
NOTE_CODES = ('stock_xbox_maps_missing', 'stock_xbox_maps_incomplete')            # converter warnings that are told once per run
NEED_XBOX_OUT = ('Xbox -> MCC needs a folder for the mod packages and none was given: pass --xbox-out <folder> (or --mcc-out: the packages '
                 'are made next to that package folder).')
SKIP_CODES = ('not_halo2', 'not_multiplayer', 'not_xbox')                           # inputs that are skipped, not failed


def _packs():
    from .xbox import packs as PK            # imported when needed: the Xbox converter's modules are not loaded by the other commands
    return PK


# ======================================================================================================
# naming rules (pure)
# ======================================================================================================
def split_sizes(n, limit=MAX_MAPS_PER_PACKAGE):
    """Sizes of the packages for a pack of `n` maps: the fewest packages of at most `limit` maps, as equal as possible, the larger ones first.
    45 -> [45]; 47 -> [24, 23]; 90 -> [45, 45]; 91 -> [31, 30, 30]; 0 -> []."""
    if n <= 0:
        return []
    k = -(-n // max(1, limit))
    base, extra = divmod(n, k)
    return [base + 1] * extra + [base] * (k - extra)


def package_titles(pack_title, n_maps, limit=MAX_MAPS_PER_PACKAGE):
    """The package titles of a pack: ['Phantom V1'] when it fits in one package, else ['Killtrocity V1 Part 1', 'Killtrocity V1 Part 2', ...]."""
    k = len(split_sizes(n_maps, limit))
    if k <= 1:
        return [pack_title]
    return ['%s Part %d' % (pack_title, i + 1) for i in range(k)]


def package_description(title, map_titles):
    """'<title> Map Pack Ported From OG Xbox to Halo MCC.', two blank lines, 'Map Names:', one in-game map title per line."""
    return DESCRIPTION_HEAD % title + '\n'.join(map_titles)


def package_folder_name(title):
    """A safe Windows folder name for a package title."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', str(title or ''))
    s = re.sub(r'\s+', ' ', s).strip(' .')[:60].strip(' .')
    if not s or s.lower() in EX.RESERVED:
        s = 'Xbox maps'
    return s


def split_items(items, limit=MAX_MAPS_PER_PACKAGE):
    """Cut `items` (already in the order of the packages) into consecutive groups of the split_sizes() sizes."""
    out, i = [], 0
    for n in split_sizes(len(items), limit):
        out.append(items[i:i + n])
        i += n
    return out


class PackagePlan(object):
    """One Excession package of an Xbox -> MCC run."""

    def __init__(self, pack_title, title, folder, items, index, total):
        self.pack_title, self.title, self.folder, self.items, self.index, self.total = pack_title, title, folder, items, index, total
        self.result = None                        # filled by package_job: dict(folder, title, maps, validation)

    @property
    def stage_name(self):
        return 'p%02d' % self.index


# ======================================================================================================
# planning (called by h2conv.batch.make_plan)
# ======================================================================================================
def xbox_out_dir(opts):
    """The folder that receives the packages of an Xbox -> MCC run: --xbox-out, else the folder that holds the Vista -> MCC package (--mcc-out)."""
    if getattr(opts, 'xbox_out', None):
        return os.path.abspath(opts.xbox_out)
    if opts.mcc_out:
        return os.path.dirname(os.path.abspath(opts.mcc_out))
    return None


def estimate_bytes(rec, to_mcc):
    """A rough upper estimate of the output of one Xbox map: the Vista map is about as large as the Xbox map (a little larger), the MCC map
    follows the Vista -> MCC line of h2conv.batch."""
    vista = int(rec.size * 1.3)
    if not to_mcc:
        return vista
    from . import batch as B
    base, k = B.MCC_OUT_COMMUNITY
    return int(base + k * vista)


def plan_xbox(plan, opts):
    """Names and output places of the Xbox items of a batch plan (items with action 'convert' and direction xbox2vista / xbox2mcc).
    Vista: <Map>_<PackSuffix>.map in the Vista output folder.  MCC: the packages (plan.xbox_packages)."""
    items = [i for i in plan.items if i.action == 'convert' and i.direction in XBOX_DIRECTIONS]
    plan.xbox_packages = []
    if not items:
        return
    PK = _packs()
    stock = _paths.stock_xbox_maps_dir(getattr(opts, 'stock_xbox_maps', None))
    for it in sorted(items, key=lambda i: i.rec.path.lower()):
        info = PK.detect(it.rec.path, stock)
        it.pack = info
        it.display = PK.display_name(it.rec.path, stock)
        it.kind = 'multiplayer'
    to_vista = [i for i in items if i.direction == 'xbox2vista']
    to_mcc = [i for i in items if i.direction == 'xbox2mcc']
    if to_vista:
        if not opts.vista_out:
            raise RuntimeError(_batch_const('NEED_VISTA_OUT'))
        single = getattr(opts, 'xbox_out_file', None)
        for it in sorted(to_vista, key=lambda i: i.rec.path.lower()):
            if single and len(to_vista) == 1:
                it.stem = os.path.splitext(os.path.basename(single))[0]
                it.out_path = os.path.abspath(single)
            else:
                it.stem = PK.file_stem(it.rec.stem, it.pack.suffix, plan.vista_used)
                it.out_path = os.path.join(opts.vista_out, it.stem + '.map')
            plan.vista_used.add(it.stem.lower())
            it.naming, it.naming_note = 'new', 'new Vista map (%s)' % it.pack.title
            if os.path.exists(it.out_path):
                if opts.overwrite:
                    it.naming, it.naming_note = 'replaces', 'output %s exists and --overwrite replaces it' % it.out_path
                else:
                    it.action, it.code, it.reason = 'skip', 'exists', 'output %s already exists (use --overwrite)' % it.out_path
                    it.naming, it.naming_note = None, ''
    if to_mcc:
        out = xbox_out_dir(opts)
        if not out:
            raise RuntimeError(NEED_XBOX_OUT)
        if os.path.isfile(os.path.join(out, 'ModInfo.json')):
            raise RuntimeError('%s is itself a mod package. Xbox maps become one package per source pack inside a folder that holds mod '
                               'packages: choose that folder (--xbox-out).' % out)
        taken = set()
        by_pack = collections.OrderedDict()
        for it in sorted(to_mcc, key=lambda i: (i.pack.title.lower(), i.display.lower(), i.rec.path.lower())):
            it.stem = PK.file_stem(it.rec.stem, it.pack.suffix, taken)
            taken.add(it.stem.lower())
            by_pack.setdefault(it.pack.title, []).append(it)
        limit = getattr(opts, 'max_per_package', None) or MAX_MAPS_PER_PACKAGE
        for pack_title, its in by_pack.items():
            titles = package_titles(pack_title, len(its), limit)
            for k, (title, group) in enumerate(zip(titles, split_items(its, limit)), 1):
                pp = PackagePlan(pack_title, title, os.path.join(out, package_folder_name(title)), group, k, len(titles))
                plan.xbox_packages.append(pp)
                exists = os.path.exists(pp.folder)
                for it in group:
                    it.package = pp
                    it.out_path = os.path.join(pp.folder, 'maps', EX.sanitize_stem(it.stem) + '.map')
                    it.naming, it.naming_note = 'new', 'new map in the package "%s"' % title
                    if exists:
                        if opts.overwrite:
                            it.naming, it.naming_note = 'replaces', 'the package folder %s exists and --overwrite replaces it' % pp.folder
                        else:
                            it.action, it.code = 'skip', 'exists'
                            it.reason = ('the package folder %s already exists (a package is never built into again; choose another folder '
                                         'or use --overwrite to replace it)' % pp.folder)
                            it.naming, it.naming_note = None, ''
        # a package whose folder is refused is not built: drop it from the plan
        plan.xbox_packages = [p for p in plan.xbox_packages if any(i.action == 'convert' for i in p.items)]


def _batch_const(name):
    from . import batch as B
    return getattr(B, name)


# ======================================================================================================
# converting one map
# ======================================================================================================
def convert_file(src, out, vista_maps=None, stock_xbox_maps=None, pack_dir=None, display_name=None, strict=False, debug=False, log=None,
                 lightmap='auto'):
    """Xbox map `src` -> Vista map `out` (written to a new temporary name beside it, then renamed onto `out`, which is replaced).
    -> the conversion report (a dict; 'validate_file' lists the problems of the written file).  Raises the converter's exceptions:
    NotHalo2MapError, NotMultiplayerError, TruncatedMapError ... (see main())."""
    from .xbox import convert_xbox as CX
    PK = _packs()
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = _paths.reserve_temp(out)
    try:
        conv = CX.XboxToVista(src, tmp, vista_maps, pack_dir, opts={'display_name': display_name, 'strict': strict, 'debug': debug,
                                                                  'stock_xbox_maps': stock_xbox_maps, 'lightmap': lightmap},
                              log=log or (lambda m: None))
        rep = conv.run()
        rep['display_name'] = display_name or PK.display_name(src, conv.stock_dir)
        rep['output'] = out
        rep['input'] = os.path.abspath(src)
        os.replace(tmp, out)
        return rep
    except BaseException:
        _paths.discard_temp(tmp)
        raise


def convert_inprocess(item, staged, opts):
    """Xbox -> Vista in this process (a batch without --isolate).  -> (report, ok, why)."""
    fn = opts.converters.get('xbox2vista')
    if fn is not None:
        from . import batch as B
        rep = B.call_filtered(fn, item.src, staged, name=item.stem, quiet=True, vista_maps=opts.vista_maps)
        if isinstance(rep, tuple) and rep:
            rep = rep[0]
        return rep, True, ''
    extra = {'lightmap': opts.lightmap} if getattr(opts, 'lightmap', 'auto') != 'auto' else {}
    rep = convert_file(item.src, staged, opts.vista_maps, getattr(opts, 'stock_xbox_maps', None), getattr(opts, 'pack_dir', None), **extra)
    ok = not rep.get('validate_file')
    return rep, ok, '' if ok else 'the converter\'s own checks failed: validate_file: %s' % str(rep['validate_file'])[:150]


def child_arguments(item, staged, rep_path, opts, vista_maps):
    """The arguments (after the program) of the child process that converts one Xbox map to the Vista map `staged`."""
    a = [item.src, '--to', 'vista', '--out', staged, '--single', '--overwrite', '--quiet', '--report', rep_path, '--vista-maps', vista_maps]
    if getattr(opts, 'stock_xbox_maps', None):
        a += ['--stock-xbox-maps', opts.stock_xbox_maps]
    if getattr(opts, 'pack_dir', None):
        a += ['--pack-dir', opts.pack_dir]
    if getattr(opts, 'lightmap', 'auto') != 'auto':
        a += ['--lightmap', opts.lightmap]
    return a


def collect_notes(plan, item, rep):
    """Remember the warnings that are told once per run (a missing OG Xbox maps folder) instead of once per map."""
    w = (rep or {}).get('warnings') if isinstance(rep, dict) else None
    if not isinstance(w, dict):
        return
    notes = plan.__dict__.setdefault('xbox_notes', OD())
    for code in NOTE_CODES:
        v = w.get(code)
        if not v:
            continue
        msg = v[0] if isinstance(v, (list, tuple)) else (v.get('examples') or [''])[0] if isinstance(v, dict) else str(v)
        n = notes.setdefault(code, {'code': code, 'message': msg, 'maps': []})
        n['maps'].append(os.path.basename(item.src))


def notes_for_result(plan):
    return [dict(n, maps=len(n['maps']), names=n['maps'][:20]) for n in getattr(plan, 'xbox_notes', {}).values()]


# ======================================================================================================
# the job folder (Xbox -> MCC): the Vista maps made on the way
# ======================================================================================================
_JOB_LOCK = threading.Lock()                  # --isolate --jobs N: several threads ask for the folder at once


def job_dir(plan):
    with _JOB_LOCK:
        d = plan.__dict__.get('xbox_job_dir')
        if d is None:
            d = os.path.join(_paths.work_subdir(_cache.XBOX_JOBS), '%d-%s' % (os.getpid(), secrets.token_hex(3)))
            os.makedirs(d, exist_ok=True)
            plan.xbox_job_dir = d
        return d


def stage_dir(plan, item):
    """Where the Vista map of `item` is made: its own folder per package inside the job folder."""
    d = os.path.join(job_dir(plan), item.package.stage_name)
    os.makedirs(d, exist_ok=True)
    return d


def cleanup(plan):
    """Delete the job folder (the intermediate Vista maps).  Only ever deletes a folder inside the converter's xbox_jobs cache folder."""
    d = plan.__dict__.pop('xbox_job_dir', None)
    if not d:
        return
    root = os.path.normcase(os.path.abspath(_paths.work_subdir(_cache.XBOX_JOBS)))
    if not os.path.normcase(os.path.abspath(d)).startswith(root + os.sep):
        return
    shutil.rmtree(d, ignore_errors=True)
    try:
        os.rmdir(root)                      # the cache folder itself, when nothing else lives in it
    except OSError:
        pass


# ======================================================================================================
# stage 2: Vista -> MCC, one package at a time
# ======================================================================================================
def package_job(plan, opts, log):
    """Build the Excession packages of the converted Xbox -> MCC items from the Vista maps in the job folder (h2conv.batch, Vista -> MCC)."""
    from . import batch as B
    PK = _packs()
    job = plan.__dict__.get('xbox_job_dir')
    if not job:
        return
    out_root = xbox_out_dir(opts)
    os.makedirs(out_root, exist_ok=True)
    saved_env = os.environ.get(_cache.ENV_NO_TRIM)
    saved_start = _cache._job_start[0]
    os.environ[_cache.ENV_NO_TRIM] = '1'          # the nested runs must not trim the cache while later packages still need their Vista maps
    try:
        for pp in plan.xbox_packages:
            its = [i for i in pp.items if i.status == 'converted' and i.stage_path and os.path.isfile(i.stage_path)]
            if not its:
                continue
            for i in its:
                _cache.note_used(i.stage_path)
            _build_package(plan, pp, its, opts, log, B, PK, out_root)
    finally:
        _cache._job_start[0] = saved_start
        if saved_env is None:
            os.environ.pop(_cache.ENV_NO_TRIM, None)
        else:
            os.environ[_cache.ENV_NO_TRIM] = saved_env


def _build_package(plan, pp, its, opts, log, B, PK, out_root):
    build = os.path.join(out_root, '.%s.part%d' % (package_folder_name(pp.title), os.getpid()))
    if os.path.exists(build):
        shutil.rmtree(build, ignore_errors=True)
    log('package "%s": %d map(s) ...' % (pp.title, len(its)))
    fail = lambda why: _fail_items(its, why)                                  # noqa: E731
    try:
        EX.ModPackage(build, create=True, title=pp.title,
                      description=package_description(pp.title, sorted((i.display for i in its), key=str.lower)))
        recs = [i.stage_rec for i in its]
        sub = B.Options(direction='vista2mcc', mcc_out=build, vista_maps=opts.vista_maps, mcc_maps=opts.mcc_maps, h2ek_maps=opts.h2ek_maps,
                        assembly_dir=opts.assembly_dir, isolate=opts.isolate, jobs=opts.jobs, timeout=opts.timeout, kinds=('multiplayer',),
                        title_from=PK.TITLE_FROM, package_title=pp.title, hash_duplicates=False, remember=False, quiet=True,
                        settings_path=False, cancel_file=opts.cancel_file, converters=opts.converters)
        splan = B.make_plan(job_dir(plan), sub, records=recs)
        res = B.run_batch(job_dir(plan), sub, log=lambda m: None, plan=splan)
    except BaseException as ex:                                              # noqa: BLE001 - includes KeyboardInterrupt: the part folder goes
        shutil.rmtree(build, ignore_errors=True)
        if isinstance(ex, KeyboardInterrupt):
            raise
        fail('%s: %s' % (type(ex).__name__, ex))
        return
    by_src = {os.path.normcase(os.path.abspath(d['src'])): d for d in res['items']}
    kept = 0
    for i in its:
        d = by_src.get(os.path.normcase(os.path.abspath(i.stage_path)))
        if d is not None and d['status'] == 'converted':
            i.mcc_stem = d['stem']
            i.out_size = d.get('out_size')
            i.out_path = os.path.join(pp.folder, 'maps', d['stem'] + '.map')
            kept += 1
        else:
            i.status = 'failed'
            i.error = ('Vista -> MCC: ' + ((d or {}).get('reason') or 'no result')) if d is not None else 'Vista -> MCC: no result'
    if not kept:
        shutil.rmtree(build, ignore_errors=True)
        return
    # the description lists the titles the package really shows (a repeated title gets a suffix in the package)
    info = os.path.join(build, 'ModInfo.json')
    mi = EX.read_json(info)
    pkg = EX.ModPackage(build, create=False)
    titles = sorted((e.title for e in pkg.entries if e.kind == 'multiplayer'), key=str.lower)
    mi['Description'] = OD([('Neutral', package_description(pp.title, titles))])
    EX.write_json_atomic(info, mi)
    try:
        for sub_name in ('staging', 'backups'):           # the part folder's own scratch space (backups: of the empty ModInfo.json it started from)
            shutil.rmtree(os.path.join(build, EX.STATE_DIR, sub_name), ignore_errors=True)
        _put_in_place(build, pp.folder, opts.overwrite)
    except OSError as ex:
        shutil.rmtree(build, ignore_errors=True)
        _fail_items(its, 'cannot make the package folder %s: %s' % (pp.folder, ex))
        return
    iss = EX.validate_package(pp.folder)
    errs = EX.errors_of(iss)
    pp.result = OD([('title', pp.title), ('folder', pp.folder), ('maps', kept), ('map_titles', titles),
                    ('validation', {'errors': len(errs), 'warnings': sum(1 for x in iss if x.level == 'warning'),
                                    'first_errors': [str(x) for x in errs[:10]]})])
    log('package "%s": %d map(s) -> %s' % (pp.title, kept, pp.folder))


def _fail_items(its, why):
    for i in its:
        if i.status == 'converted':
            i.status = 'failed'
            i.error = why[:600]


def _put_in_place(build, final, overwrite):
    """Rename the finished part folder to the package folder; an old package folder is replaced only with --overwrite (and only if it is there)."""
    old = None
    if os.path.exists(final):
        if not overwrite:
            raise OSError('%s already exists' % final)
        old = '%s.old%d' % (final, os.getpid())
        os.replace(final, old)
    try:
        os.replace(build, final)
    except OSError:
        if old:
            os.replace(old, final)
        raise
    if old:
        shutil.rmtree(old, ignore_errors=True)


# ======================================================================================================
# the command
# ======================================================================================================
def _write_single_report(path, rep):
    if path:
        import json
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(_paths.report_for_file(rep), fh, indent=1, default=str)


def run_single(a):
    """One Xbox map -> one Vista map, no scanning, no summary: what `batch --isolate` starts as a child process (hidden option --single) and
    what a script can call.  The conversion report is the --report file.  Exit codes: see the module help."""
    from .xbox import xbox_cache as XC
    from .xbox import convert_xbox as CX
    PK = _packs()
    src = a.inputs[0]
    if len(a.inputs) != 1 or os.path.isdir(src) or a.to != 'vista':
        print('ERROR: --single converts exactly one map file to Vista')
        return EXIT_USAGE
    out = os.path.abspath(a.out)
    if os.path.isdir(out) or a.out.endswith(('\\', '/')):
        info = PK.detect(src, _paths.stock_xbox_maps_dir())
        out = os.path.join(out, PK.file_stem(os.path.splitext(os.path.basename(src))[0], info.suffix) + '.map')
    try:
        _paths.refuse_existing(out, {'overwrite': a.overwrite}, a.report)
        rec = MI.identify(src, deep=False)
        if rec.fmt != 'xbox' or rec.status != 'ok':
            if rec.fmt in ('vista', 'mcc') and rec.status == 'ok':
                code = 'not_xbox'
                msg = '%s is a Halo 2 %s map, not an original Xbox map' % (os.path.basename(src), 'Vista' if rec.fmt == 'vista' else 'MCC')
            else:
                code, msg = rec.code, rec.reason or 'not a map this converter can use'
            skipped = code in SKIP_CODES or code == 'database'
            print(('SKIPPED: ' if skipped else 'ERROR: ') + msg, flush=True)
            _write_single_report(a.report, {'status': 'skipped' if skipped else 'failed', 'code': code, 'message': msg,
                                            'input': os.path.basename(src)})
            return EXIT_SKIPPED if skipped else (EXIT_BAD_INPUT if code == 'truncated' else EXIT_USAGE)
        extra = {'lightmap': a.lightmap} if getattr(a, 'lightmap', 'auto') != 'auto' else {}
        rep = convert_file(src, out, _paths.vista_maps_dir(None), _paths.stock_xbox_maps_dir(), a.pack_dir, None, False, a.debug,
                           None if a.quiet else (lambda m: print(m, flush=True)), **extra)
    except FileExistsError as ex:
        print('ERROR: %s' % ex)
        return EXIT_USAGE
    except (XC.NotHalo2MapError, CX.NotMultiplayerError) as ex:
        print('SKIPPED: %s' % ex, flush=True)
        _write_single_report(a.report, {'status': 'skipped', 'code': 'not_halo2' if isinstance(ex, XC.NotHalo2MapError) else 'not_multiplayer',
                                        'message': str(ex), 'input': os.path.basename(src)})
        return EXIT_SKIPPED
    except XC.TruncatedMapError as ex:
        print('ERROR: %s' % ex, flush=True)
        _write_single_report(a.report, {'status': 'failed', 'code': 'truncated', 'message': str(ex), 'input': os.path.basename(src)})
        return EXIT_BAD_INPUT
    rep['status'] = 'ok' if not rep['validate_file'] else 'failed'
    _write_single_report(a.report, rep)
    if rep['validate_file']:
        print('ERROR: the written map failed its own checks: %s' % str(rep['validate_file'])[:300])
    return EXIT_OK if not rep['validate_file'] else EXIT_FAILED


def scrub_report(res):
    """A copy of a batch result for the --report file with file NAMES instead of folders (people paste reports into bug reports, and the folders
    hold the account name), like the reports of the other converters (paths.report_for_file)."""
    import copy
    base = lambda p: os.path.basename(str(p).replace('\\', '/')) if p else p     # noqa: E731
    r = copy.deepcopy(res)
    r['root'] = base(r.get('root'))
    for sect in ('items', 'failed', 'needs_attention'):
        for d in r.get(sect) or []:
            for k in ('src', 'out'):
                d[k] = base(d.get(k))
    r['output_dirs'] = {k: base(v) for k, v in (r.get('output_dirs') or {}).items()}
    if isinstance(r.get('package'), dict):
        r['package']['root'] = base(r['package'].get('root'))
    for p in r.get('xbox_packages') or []:
        p['folder'] = base(p.get('folder'))
    return r


def exit_code(res):
    """Exit code of a finished run: see the module help."""
    items = res.get('items') or []
    if res['counts'].get('failed'):
        return EXIT_FAILED
    if any(d.get('code') == 'truncated' for d in items):
        return EXIT_BAD_INPUT
    if not res['counts'].get('converted') and any(d.get('code') in SKIP_CODES for d in items):
        return EXIT_SKIPPED
    return EXIT_OK


def gather(inputs, recursive, exclude=()):
    """-> ([MapRecord], root folder) of the files and folders given on the command line (a folder: every .map below it)."""
    recs, seen = [], set()
    for p in inputs:
        for r in MI.scan_folder(p, recursive=recursive, exclude=exclude, deep=False):
            k = os.path.normcase(os.path.abspath(r.path))
            if k not in seen:
                seen.add(k)
                recs.append(r)
    dirs = [os.path.abspath(p) if os.path.isdir(p) else os.path.dirname(os.path.abspath(p)) for p in inputs]
    try:
        root = os.path.commonpath(dirs) if len(dirs) > 1 else dirs[0]
    except ValueError:
        root = dirs[0]
    return recs, root


def main(argv=None):
    import argparse
    from . import __version__
    from . import batch as B
    ap = argparse.ArgumentParser(prog='h2conv xbox', formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description=__doc__.split('Which files count')[0].strip(),
                                 epilog='Exit codes: 0 done, 1 a conversion failed, 2 usage / folder problem, 3 every input was skipped, 4 unusable input '
                                        '(for example a truncated download).')
    ap.add_argument('--version', action='version', version='Halo 2 Map Converter %s' % __version__)
    ap.add_argument('inputs', nargs='+', metavar='map_or_folder', help='original Xbox Halo 2 map file(s), or folder(s) searched for them')
    ap.add_argument('--to', required=True, choices=('vista', 'mcc'), help='vista: Vista .map files; mcc: Excession mod packages (Xbox -> Vista -> MCC)')
    ap.add_argument('--out', required=True, metavar='DIR', help='--to vista: the folder for the Vista maps (a file name ending in .map for one input map); '
                                                                 '--to mcc: the folder that receives one mod package per source pack')
    _paths.add_common_options(ap, mcc=True, vista=True, assembly=True, h2ek=True, stock_xbox=True)
    ap.add_argument('--pack-dir', default=None, metavar='DIR', help="folder of the pack's shared.map / mainmenu.map (default: the map's folder, the "
                                                                    "folder above it, or a maps folder beside it)")
    ap.add_argument('--overwrite', action='store_true', help='replace existing output files / package folders (without it the run stops before it '
                                                             'starts when one exists)')
    ap.add_argument('--no-recursive', action='store_true', help='only the files directly in a folder')
    ap.add_argument('--isolate', action='store_true', help='convert every map in its own process (a crash or a hang cannot stop the run)')
    ap.add_argument('--jobs', type=int, default=1, help='maps converted at the same time (needs --isolate; 1 to 4)')
    ap.add_argument('--timeout', type=int, default=3600, help='seconds per map in --isolate mode')
    ap.add_argument('--max-maps-per-package', type=int, default=MAX_MAPS_PER_PACKAGE, metavar='N',
                    help='--to mcc: most maps in one package (1 to %d, default %d)' % (MAX_MAPS_PER_PACKAGE, MAX_MAPS_PER_PACKAGE))
    ap.add_argument('--lightmap', choices=('auto', 'native', 'neutral-valid'), default='auto', help='what a map gets whose BSP has no usable lightmap (the mod stripped it, or it does not fit the BSP): auto (default) = the complete stock lightmap when the BSP is the unmodified BSP of a stock map, else native; native = a complete lightmap generated from the BSP that the game treats as not valid, so the BSP is drawn as on the original Xbox (no lightmap); neutral-valid = a generated valid lightmap with constant grey lighting. Maps that carry a working lightmap keep it.')
    ap.add_argument('--package-title', default=None, metavar='TEXT', help='--to mcc: title of the package (only when the maps come from one pack; default: the '
                                                                          'pack\'s name)')
    ap.add_argument('--dry-run', action='store_true', help='list what would be done; nothing is written')
    ap.add_argument('--report', default=None, help='write the result (JSON, file names only) here')
    ap.add_argument('--settings', default=None, help='settings.json to use (default %%APPDATA%%\\H2MapConverter\\settings.json)')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--debug', action='store_true')
    ap.add_argument('--xbox-out', default=None, help=argparse.SUPPRESS)
    ap.add_argument('--single', action='store_true', help=argparse.SUPPRESS)            # one map, no scan / summary (the --isolate children of a batch)
    ap.add_argument('--cancel-file', default=None, help=argparse.SUPPRESS)
    ap.add_argument('--announce', action='store_true', help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:                                                  # noqa: BLE001
            pass
    log = (lambda m: None) if a.quiet else B.say
    if not 1 <= a.jobs <= 4:
        ap.error('--jobs must be from 1 to 4')
    if not 1 <= a.max_maps_per_package <= MAX_MAPS_PER_PACKAGE:
        ap.error('--max-maps-per-package must be from 1 to %d' % MAX_MAPS_PER_PACKAGE)
    for p in a.inputs:
        if not os.path.exists(p):
            print('ERROR: %s does not exist' % p)
            return EXIT_USAGE
    _paths.configure(settings_file=a.settings)
    _paths.configure_from_args(a)
    if a.single:
        return run_single(a)
    out = os.path.abspath(a.out)
    out_file = out if (a.to == 'vista' and out.lower().endswith('.map') and not os.path.isdir(out)) else None
    if out_file and (len(a.inputs) != 1 or os.path.isdir(a.inputs[0])):
        print('ERROR: --out %s names a file, which needs exactly one input map. Give a folder to convert several maps.' % a.out)
        return EXIT_USAGE
    if a.report and os.path.exists(a.report) and not a.overwrite:
        print('ERROR: the report file %s already exists; pass --overwrite to replace it (or choose another name)' % a.report)
        return EXIT_USAGE
    try:
        vista_maps = _paths.vista_maps_dir(None, required=not a.dry_run)
        if not a.dry_run:
            _paths.assembly_layout(None)
        recs, root = gather(a.inputs, not a.no_recursive, exclude=[out] if os.path.isdir(out) else [])
        opts = B.Options(
            direction='xbox', xbox_only=True, xbox_to=a.to, vista_out=(os.path.dirname(out_file) if out_file else out) if a.to == 'vista' else None,
            xbox_out=out if a.to == 'mcc' else None, xbox_out_file=out_file, mcc_out=None, vista_maps=vista_maps,
            mcc_maps=_paths.pick('mcc_maps')[0], h2ek_maps=_paths.h2ek_maps_dir(None), assembly_dir=_paths.pick('assembly')[0],
            stock_xbox_maps=_paths.stock_xbox_maps_dir(), pack_dir=a.pack_dir, overwrite=a.overwrite, dry_run=a.dry_run, isolate=a.isolate,
            jobs=a.jobs, timeout=a.timeout, kinds=('multiplayer', 'solo'), hash_duplicates=False, remember=False, quiet=a.quiet,
            settings_path=False, list_plan=a.dry_run, max_per_package=a.max_maps_per_package, cancel_file=a.cancel_file, announce=a.announce,
            recursive=not a.no_recursive, lightmap=a.lightmap)
        plan = B.make_plan(root, opts, records=recs, log=log)
        refused = [i for i in plan.items if i.code == 'exists']
        if refused:
            print('ERROR: nothing was converted, because the output already exists (pass --overwrite to replace it):')
            for i in refused:
                print('  %s' % i.reason)
            return EXIT_USAGE
        if a.package_title:
            packs = {p.pack_title for p in plan.xbox_packages}
            if len(packs) > 1:
                print('ERROR: --package-title needs maps from one pack, these come from %d packs: %s' % (len(packs), ', '.join(sorted(packs))))
                return EXIT_USAGE
            if len(plan.xbox_packages) > 1:
                print('ERROR: --package-title cannot name %d packages (the pack is split). Leave it out: they are titled "<pack> Part n".'
                      % len(plan.xbox_packages))
                return EXIT_USAGE
            for p in plan.xbox_packages:
                p.title = a.package_title
                p.folder = os.path.join(os.path.dirname(p.folder), package_folder_name(a.package_title))
                for i in p.items:
                    i.out_path = os.path.join(p.folder, 'maps', os.path.basename(i.out_path or ''))
                if os.path.exists(p.folder) and not a.overwrite:
                    print('ERROR: the package folder %s already exists; pass --overwrite to replace it' % p.folder)
                    return EXIT_USAGE
        res = B.run_batch(root, opts, log=log, plan=plan)
    except (RuntimeError, EX.PackageError, FileExistsError, _paths.PathsError) as ex:
        print('ERROR: %s' % ex)
        return EXIT_USAGE
    if a.report:
        try:
            import json
            os.makedirs(os.path.dirname(os.path.abspath(a.report)), exist_ok=True)
            with open(a.report, 'w', encoding='utf-8') as fh:
                json.dump(scrub_report(res), fh, indent=1, default=str)
        except OSError as ex:
            print('could not write the report: %s' % ex)
    if res.get('interrupted'):
        return 130
    return exit_code(res)


if __name__ == '__main__':
    sys.exit(main())
