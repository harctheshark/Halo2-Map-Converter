# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Where everything is: the ONE module that knows folders on this PC.

Every folder the converter needs is looked up here, in this order (first hit wins):

    1. the command line (an explicit argument, or the value a CLI handed to configure() after parsing its options)
    2. an environment variable
    3. the per-user settings file   %APPDATA%\\H2MapConverter\\settings.json   (H2MAPCONVERTER_HOME moves it)
    4. auto-detection (Steam libraries, the usual Halo 2 install places, the Windows Documents folder, the bundled data)
    5. a clear error that names the option and the environment variable that would fix it

    what                         option            environment variable          settings.json key
    MCC maps folder              --mcc-maps        H2CONV_MCC_MAPS               mcc_maps_dir
    Vista maps folder (stock)    --vista-maps      H2CONV_VISTA_MAPS (H2V_MAPS)  vista_maps_dir
    Assembly layout files        --assembly-dir    H2CONV_ASSEMBLY               assembly_dir      (default: the copy in h2conv\\data\\assembly)
    caches / scratch             -                 H2CONV_WORK                   work_dir          (default: %LOCALAPPDATA%\\H2MapConverter\\work)
    H2EK maps (extra donors)     --h2ek-maps       H2CONV_H2EK_MAPS              h2ek_maps_dir     (never auto-detected)
    Vista output (batch)         --vista-out       -                             vista_output_dir  (default: Documents\\My Games\\Halo 2\\Maps)
    MCC output package (batch)   --mcc-out         -                             mcc_output_dir    (no default)
    Excession (optional)         -                 H2CONV_EXCESSION              excession_dir
    ffmpeg (optional, Ogg sounds) -                H2CONV_FFMPEG                 ffmpeg_path       (a file or a folder; else ffmpeg on the PATH)
    Cartographer source (optional) -               H2CONV_CARTOGRAPHER_SRC       cartographer_src_dir   (only for a diagnostic line of the report)

`py -3 -m h2conv.paths [--json]` prints every folder with where it came from (option / env / settings / auto / bundled) and checks the key files.
Stdlib only; importing this module never touches the disk beyond reading the settings file when a folder is asked for.
"""
import contextlib
import ctypes
import json
import ntpath
import os
import re
import sys

APP_NAME = 'H2MapConverter'
ASSEMBLY_TESTED = '2026.02.16'                  # the Assembly release the layouts were taken from / tested with
ASSEMBLY_MIN_PLUGINS = {'Halo2': 98, 'Halo2MCC': 35}
# the plugin files (file stems) the converter needs: Plugins\Halo2 (Vista, also the MCC fallback) and Plugins\Halo2MCC (the 35 MCC overrides)
ASSEMBLY_PLUGIN_STEMS = {
    'Halo2': ('DECR MGS2 PRTM ____ _cen _eap _ehi _fx_ _igh _ipd _qip _rea _sce adlg ai__ ant! bipd bitm bloc bsdt char cin_ clu_ clwd coll colo cont crea '
              'ctrl dc_s dec_ deca dgr_ effe egor eqip fog foot fpch garb gldf goof hlmt hsc_ hudg itmc jmad jpt! lens lifi ligh lsnd ltmp mach matg mcsr '
              'mdlg mode mulg nhdt phmo phys pmov pphy proj prt3 sbsp scen scnr sfx+ shad sily skin sky sncl snd! snde snmx spas spk! ssce sslt stem styl '
              'tdtl trak trg_ udlg ugh! unic vehc vehi vrtx weap weat wgit wgtz wigl').split(),
    'Halo2MCC': ('bipd bitm bloc char coll crea ctrl eqip garb goof hlmt lifi lsnd ltmp mach matg mulg nhdt phmo phys proj sbsp scen scnr shad sily sncl '
                 'snd! snde spas ssce ugh! vehi vrtx weap').split(),
}
MCC_MAPS_SUBPATH = os.path.join('steamapps', 'common', 'Halo The Master Chief Collection', 'halo2', 'h2_maps_win64_dx11')
H2EK_MAPS_NAME = 'h2_maps_win64_dx11'
VISTA_KEY_FILES = ('shared.map', 'mainmenu.map', 'single_player_shared.map')
# a sub-folder of the MCC maps folder in which some modders keep an untouched copy of the stock maps: h2conv.mapinfo reads the creation times of the
# stock databases from it when it exists, and the converters refuse to write into it
STOCK_BACKUP_DIR_NAME = 'Backup NOT MODDED'

ENV_MCC_MAPS = 'H2CONV_MCC_MAPS'
ENV_VISTA_MAPS = 'H2CONV_VISTA_MAPS'
ENV_VISTA_MAPS_OLD = 'H2V_MAPS'                  # the name the validator used before
ENV_ASSEMBLY = 'H2CONV_ASSEMBLY'
ENV_WORK = 'H2CONV_WORK'
ENV_H2EK_MAPS = 'H2CONV_H2EK_MAPS'
ENV_EXCESSION = 'H2CONV_EXCESSION'
ENV_FFMPEG = 'H2CONV_FFMPEG'
ENV_CARTOGRAPHER_SRC = 'H2CONV_CARTOGRAPHER_SRC'
ENV = {
    'mcc_maps': (ENV_MCC_MAPS,),
    'vista_maps': (ENV_VISTA_MAPS, ENV_VISTA_MAPS_OLD),
    'assembly': (ENV_ASSEMBLY,),
    'work': (ENV_WORK,),
    'h2ek_maps': (ENV_H2EK_MAPS,),
    'excession': (ENV_EXCESSION,),
    'ffmpeg': (ENV_FFMPEG,),
    'cartographer_src': (ENV_CARTOGRAPHER_SRC,),
}
SETTING = {
    'mcc_maps': 'mcc_maps_dir', 'vista_maps': 'vista_maps_dir', 'assembly': 'assembly_dir', 'work': 'work_dir',
    'h2ek_maps': 'h2ek_maps_dir', 'excession': 'excession_dir', 'vista_out': 'vista_output_dir', 'mcc_out': 'mcc_output_dir',
    'ffmpeg': 'ffmpeg_path', 'cartographer_src': 'cartographer_src_dir',
}
OPTION = {
    'mcc_maps': '--mcc-maps', 'vista_maps': '--vista-maps', 'assembly': '--assembly-dir', 'h2ek_maps': '--h2ek-maps',
    'vista_out': '--vista-out', 'mcc_out': '--mcc-out', 'work': None, 'excession': None, 'ffmpeg': None, 'cartographer_src': None,
}
LABEL = {
    'mcc_maps': 'Halo 2 MCC maps folder', 'vista_maps': 'Halo 2 Vista (Project Cartographer) stock maps folder',
    'assembly': 'Assembly layout files', 'work': 'work folder (caches)', 'h2ek_maps': 'H2EK maps folder (extra donors)',
    'vista_out': 'Vista output folder', 'mcc_out': 'MCC output package folder', 'excession': 'Excession install',
    'ffmpeg': 'ffmpeg (Ogg sounds)', 'cartographer_src': 'Cartographer source folder',
}


def report_for_file(report):
    """A shallow copy of a conversion report for the --report JSON file: 'input' / 'output' become file NAMES (people paste reports into bug reports; the
    folders hold the account name) and a module object ('_writer_module') becomes its name.  The in-memory report is not touched."""
    out = report.__class__(report)
    for k in ('input', 'output'):
        if isinstance(out.get(k), str):
            out[k] = ntpath.basename(out[k].replace('/', '\\'))
    w = out.get('_writer_module')
    if w is not None and not isinstance(w, str):
        out['_writer_module'] = getattr(w, '__name__', str(w))
    return out


def refuse_existing(out_path, opts, report_path=None):
    """R16: the command line never replaces a file.  `opts['overwrite']` is False for a command-line run without --overwrite; callers that pass no
    such option (batch, tests, library use) keep the old behaviour.  Raises FileExistsError (the converters' main() prints it and exits 2)."""
    if opts.get('overwrite', True):
        return
    for p, what in ((out_path, 'output'), (report_path, 'report')):
        if p and os.path.exists(p):
            raise FileExistsError('the %s file %s already exists; pass --overwrite to replace it (or choose another name / folder)' % (what, p))


def reserve_temp(out_path):
    """A temporary file name next to `out_path` that did not exist before: <stem>.<pid>.<random>.tmp, created empty and exclusively, so the name is ours.
    The converters write the new map to it and rename it onto the output; a file that was already there (whatever its name) is never replaced or deleted."""
    import secrets
    base = os.path.splitext(os.path.abspath(out_path))[0]
    for _ in range(100):
        tmp = '%s.%d.%s.tmp' % (base, os.getpid(), secrets.token_hex(4))
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0), 0o666)
        except FileExistsError:
            continue
        os.close(fd)
        return tmp
    raise PathsError('cannot create a temporary file next to %s' % out_path)


def discard_temp(tmp):
    """Remove a temp file made by reserve_temp (and the '<tmp>.part' a writer may have left); never raises."""
    for p in (tmp, tmp + '.part'):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


class PathsError(RuntimeError):
    """A folder or file the converter needs is missing / unusable.  The message is meant to be shown to the person running the converter as it is."""


# --------------------------------------------------------------------------------------------------
# state (set by the command line tools)
# --------------------------------------------------------------------------------------------------
_override = {}              # kind -> value given on the command line
_settings_file = [None]     # a settings.json chosen with --settings
_env_saved = {}             # environment variables configure() exported: name -> previous value (None = was not set)
_auto_cache = {}
_settings_cache = {}


def configure(settings_file=None, **kinds):
    """Called by a CLI right after it parsed its options: configure(mcc_maps=a.mcc_maps, vista_maps=a.vista_maps, assembly=a.assembly_dir, ...).
    Values that are None / empty are ignored.  Everything that asks paths later sees the command line first."""
    if settings_file:
        _settings_file[0] = settings_file
        _env_saved.setdefault('H2MAPCONVERTER_SETTINGS_FILE', os.environ.get('H2MAPCONVERTER_SETTINGS_FILE'))
        os.environ['H2MAPCONVERTER_SETTINGS_FILE'] = os.path.abspath(settings_file)
    for k, v in kinds.items():
        if k not in SETTING:
            raise KeyError('unknown folder kind %r' % k)
        if v:
            _override[k] = os.path.abspath(str(v))
            # export it, so that worker processes (multiprocessing 'spawn', batch --isolate children) see the same folder
            if ENV.get(k) and ENV[k][0] not in _env_saved:
                _env_saved[ENV[k][0]] = os.environ.get(ENV[k][0])
            if ENV.get(k):
                os.environ[ENV[k][0]] = _override[k]
    _auto_cache.clear()


def reset():
    """Forget configure() (tests): the overrides, the exported environment variables, the caches."""
    _override.clear()
    _settings_file[0] = None
    for name, old in _env_saved.items():
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old
    _env_saved.clear()
    _auto_cache.clear()
    _settings_cache.clear()


@contextlib.contextmanager
def scoped(settings_file=None, **kinds):
    """configure() for the duration of a `with` block (h2conv.batch.run_batch uses it): afterwards the overrides and the exported environment
    variables are exactly as they were, so a library user's later calls do not inherit the folders of an earlier batch."""
    names = set(n for t in ENV.values() for n in t) | {'H2MAPCONVERTER_SETTINGS_FILE'}
    env0 = dict((n, os.environ.get(n)) for n in names)
    over0, sf0, saved0 = dict(_override), _settings_file[0], dict(_env_saved)
    try:
        configure(settings_file, **kinds)
        yield
    finally:
        _override.clear()
        _override.update(over0)
        _settings_file[0] = sf0
        _env_saved.clear()
        _env_saved.update(saved0)
        for n, v in env0.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        _auto_cache.clear()


def package_dir():
    """The h2conv package folder."""
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """h2conv\\data (shipped data files)."""
    return os.path.join(package_dir(), 'data')


def bundled_assembly_dir():
    return os.path.join(data_dir(), 'assembly')


# --------------------------------------------------------------------------------------------------
# settings.json
# --------------------------------------------------------------------------------------------------
def settings_dir():
    return os.environ.get('H2MAPCONVERTER_HOME') or os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), APP_NAME)


def settings_file():
    return _settings_file[0] or os.environ.get('H2MAPCONVERTER_SETTINGS_FILE') or os.path.join(settings_dir(), 'settings.json')


def load_settings():
    """The settings.json as a dict ({} when missing / unreadable).  Re-read when the file changed."""
    p = settings_file()
    try:
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    hit = _settings_cache.get(p)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        with open(p, 'r', encoding='utf-8-sig') as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            d = {}
    except (OSError, ValueError):
        d = {}
    _settings_cache[p] = (key, d)
    return d


def _clean(v):
    if v is None:
        return None
    v = str(v).strip().strip('"').strip()
    return v or None


def pick(kind, explicit=None):
    """-> (value, source) from the command line / environment / settings; (None, None) when none of them names the folder."""
    v = _clean(explicit)
    if v:
        return v, 'option'
    v = _clean(_override.get(kind))
    if v:
        return v, 'option'
    for name in ENV.get(kind, ()):
        v = _clean(os.environ.get(name))
        if v:
            return v, 'env'
    v = _clean(load_settings().get(SETTING[kind]))
    if v:
        return v, 'settings'
    return None, None


def _how(kind):
    bits = []
    if OPTION.get(kind):
        bits.append('pass %s <folder>' % OPTION[kind])
    if ENV.get(kind):
        bits.append('set the environment variable %s' % ENV[kind][0])
    bits.append('or put "%s" in %s' % (SETTING[kind], settings_file()))
    return ', '.join(bits)


def _norm(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


# --------------------------------------------------------------------------------------------------
# disks, Steam, known folders
# --------------------------------------------------------------------------------------------------
def fixed_drives():
    """The root folder of every fixed (not removable, not network) drive, one per drive letter."""
    out = []
    if os.name != 'nt':
        return out
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                root = chr(65 + i) + ':\\'
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:           # DRIVE_FIXED
                    out.append(root)
    except Exception:                                                          # noqa: BLE001
        pass
    return out


def documents_dir():
    """The Windows 'Documents' known folder (it may be redirected, e.g. to OneDrive); ~\\Documents when the API is unavailable."""
    if os.name == 'nt':
        try:
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [('Data1', ctypes.c_ulong), ('Data2', ctypes.c_ushort), ('Data3', ctypes.c_ushort), ('Data4', ctypes.c_ubyte * 8)]
            fid = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))   # FOLDERID_Documents
            buf = ctypes.c_wchar_p()
            fn = ctypes.windll.shell32.SHGetKnownFolderPath
            fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
            fn.restype = ctypes.c_long
            if fn(ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0 and buf.value:
                path = buf.value
                ctypes.windll.ole32.CoTaskMemFree(buf)
                return path
        except Exception:                                                      # noqa: BLE001
            pass
    return os.path.join(os.path.expanduser('~'), 'Documents')


def documents_maps_dir():
    """<Documents>\\My Games\\Halo 2\\Maps: the folder Project Cartographer scans for custom maps."""
    return os.path.join(documents_dir(), 'My Games', 'Halo 2', 'Maps')


def local_appdata_dir():
    return os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), 'AppData', 'Local')


def steam_libraries():
    """Existing Steam library folders (each holds steamapps\\common): the registry's Steam folder and the default one with the libraries listed in
    their libraryfolders.vdf, then <drive>:\\SteamLibrary, <drive>:\\Steam, <drive>:\\Program Files (x86)\\Steam, <drive>:\\Games\\... on every fixed drive."""
    if 'steam' in _auto_cache:
        return list(_auto_cache['steam'])
    roots = []
    if os.name == 'nt':
        try:
            import winreg
            for hive, sub in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam'), (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam')):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        for val in ('SteamPath', 'InstallPath'):
                            try:
                                roots.append(os.path.normpath(winreg.QueryValueEx(k, val)[0]))
                            except OSError:
                                pass
                except OSError:
                    pass
        except Exception:                                                      # noqa: BLE001
            pass
    for env in ('ProgramFiles(x86)', 'ProgramFiles'):
        if os.environ.get(env):
            roots.append(os.path.join(os.environ[env], 'Steam'))
    libs = []
    for r in roots:
        libs.append(r)
        for vdf in (os.path.join(r, 'steamapps', 'libraryfolders.vdf'), os.path.join(r, 'config', 'libraryfolders.vdf')):
            try:
                with open(vdf, 'r', encoding='utf-8', errors='replace') as fh:
                    txt = fh.read()
            except OSError:
                continue
            for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
                libs.append(os.path.normpath(m.group(1).replace('\\\\', '\\')))
    for d in fixed_drives():
        for sub in ('SteamLibrary', 'Steam', os.path.join('Program Files (x86)', 'Steam'), os.path.join('Games', 'SteamLibrary'),
                    os.path.join('Games', 'Steam'), os.path.join('Program Files', 'Steam')):
            libs.append(os.path.join(d, sub))
    out, seen = [], set()
    for L in libs:
        n = _norm(L)
        if n not in seen and os.path.isdir(os.path.join(L, 'steamapps')):
            seen.add(n)
            out.append(L)
    _auto_cache['steam'] = out
    return list(out)


# --------------------------------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------------------------------
def _isfile(*parts):
    return os.path.isfile(os.path.join(*parts))


def _map_version(path):
    try:
        with open(path, 'rb') as fh:
            h = fh.read(8)
        if len(h) == 8 and h[:4] == b'daeh':
            return int.from_bytes(h[4:8], 'little')
    except OSError:
        pass
    return None


def looks_like_mcc_maps(d, strict=False):
    """strict: shared.map + textures.dat (what auto-detection requires).  Otherwise: any .map file or textures.dat."""
    if not d or not os.path.isdir(d):
        return False
    if strict:
        return _isfile(d, 'shared.map') and _isfile(d, 'textures.dat')
    if _isfile(d, 'textures.dat'):
        return True
    try:
        return any(n.lower().endswith('.map') for n in os.listdir(d))
    except OSError:
        return False


def has_vista_files(d):
    """The three stock databases are there (existence only: no file is opened)."""
    return bool(d) and os.path.isdir(d) and all(_isfile(d, n) for n in VISTA_KEY_FILES)


def looks_like_vista_maps(d):
    """has_vista_files() and shared.map is a cache-file version 8 (Halo 2 Vista) - this reads 8 bytes of shared.map."""
    return has_vista_files(d) and _map_version(os.path.join(d, "shared.map")) == 8


# --------------------------------------------------------------------------------------------------
# MCC maps folder
# --------------------------------------------------------------------------------------------------
def _auto_mcc():
    if 'mcc' not in _auto_cache:
        found = None
        for L in steam_libraries():
            d = os.path.join(L, MCC_MAPS_SUBPATH)
            if looks_like_mcc_maps(d, strict=True):
                found = d
                break
        _auto_cache['mcc'] = found
    return _auto_cache['mcc']


def mcc_maps_dir(explicit=None, required=True):
    """The Halo 2 MCC maps folder (halo2\\h2_maps_win64_dx11: shared.map, the stock maps, textures.dat, sounds_*.dat).
    explicit / env / settings values must name a folder with .map files or textures.dat; the auto-detected one must hold shared.map + textures.dat."""
    v, src = pick('mcc_maps', explicit)
    if v is None:
        v, src = _auto_mcc(), 'auto'
    if v is None:
        if not required:
            return None
        raise PathsError('Halo: The Master Chief Collection (Halo 2) was not found in your Steam libraries.  Install it, or tell the converter where its '
                         'halo2\\h2_maps_win64_dx11 folder is: %s.' % _how('mcc_maps'))
    v = os.path.abspath(v)
    if src != 'auto' and not looks_like_mcc_maps(v):
        raise PathsError('The MCC maps folder %s (from the %s) does not exist or holds no .map files and no textures.dat.  Point %s at the '
                         'halo2\\h2_maps_win64_dx11 folder of Halo: The Master Chief Collection.'
                         % (v, {'option': 'command line', 'env': 'environment', 'settings': 'settings file'}[src], OPTION['mcc_maps']))
    return v


def stock_mcc_maps_dir():
    """The STOCK MCC maps folder (the installed game's halo2\\h2_maps_win64_dx11: the stock shared.map, textures.dat, sounds_*.dat), None if
    unknown.  Unlike mcc_maps_dir() this ignores a per-map `--mcc-maps` (a workshop mod's own folder: its shared.map and its .dat files are NOT
    the stock ones, custom-shared support compares against the stock ones); the auto-detected Steam install comes first, then the folder
    mcc_maps_dir() resolves (a non-Steam stock install given by option / environment / settings)."""
    inst = _auto_mcc()
    if inst:
        return inst
    return mcc_maps_dir(required=False)


def mcc_dat_dir(explicit=None, need=('textures.dat',)):
    """The folder that holds textures.dat / sounds_*.dat: the MCC maps folder in use when it has the `need` files, else the installed game's.
    (--mcc-maps may point at a folder of decompressed maps without the .dat files.)"""
    base = mcc_maps_dir(explicit, required=False)
    if base and all(_isfile(base, n) for n in need):
        return base
    inst = _auto_mcc()                                                  # only looked up when the folder in use lacks the files
    if inst and all(_isfile(inst, n) for n in need):
        return inst
    if base:
        return base
    return mcc_maps_dir(explicit)                                       # raises the readable error


def require_mcc_files(names=('textures.dat',), explicit=None):
    """-> the folder holding `names`, or a PathsError that says which file is missing where."""
    d = mcc_dat_dir(explicit, need=names)
    for n in names:
        if not _isfile(d, n):
            raise PathsError('%s was not found in %s (the MCC maps folder).  Pass the halo2\\h2_maps_win64_dx11 folder of Halo: The Master Chief '
                             'Collection with %s, or set H2CONV_MCC_MAPS.' % (n, d, OPTION['mcc_maps']))
    return d


# --------------------------------------------------------------------------------------------------
# Vista maps folder
# --------------------------------------------------------------------------------------------------
_SKIP_DIRS = {'windows', '$recycle.bin', 'system volume information', 'programdata', 'perflogs', 'recovery', 'config.msi', 'msocache',
              'documents and settings', '$windows.~bt', '$windows.~ws', 'windowsapps', 'node_modules', '.git', 'appdata'}


def _subdirs(d):
    out = []
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False) and e.name.lower() not in _SKIP_DIRS:
                        out.append(e.path)
                except OSError:
                    pass
    except OSError:
        pass
    return out


def vista_candidates():
    """Folders on the fixed drives that look like the maps folder of Halo 2 Vista / Project Cartographer (shared.map v8, mainmenu.map,
    single_player_shared.map).  Looks at the usual install places and at <drive>:\\*\\maps and <drive>:\\*\\*\\maps."""
    if 'vista_all' in _auto_cache:
        return list(_auto_cache['vista_all'])
    found, seen = [], set()

    def test(d):
        n = _norm(d)
        if n in seen:
            return
        seen.add(n)
        if has_vista_files(d):
            found.append(d)

    if os.name == 'nt':
        try:
            import winreg
            for sub in (r'SOFTWARE\WOW6432Node\Microsoft\Microsoft Games\Halo 2', r'SOFTWARE\Microsoft\Microsoft Games\Halo 2'):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                        for val in ('InstallPath', 'EXE Path', 'Path'):
                            try:
                                test(os.path.join(winreg.QueryValueEx(k, val)[0], 'maps'))
                            except OSError:
                                pass
                except OSError:
                    pass
        except Exception:                                                      # noqa: BLE001
            pass
    drives = fixed_drives()
    for d in drives:
        for sub in (os.path.join('Program Files (x86)', 'Microsoft Games', 'Halo 2'), os.path.join('Program Files', 'Microsoft Games', 'Halo 2'),
                    'Halo 2', os.path.join('Games', 'Halo 2')):
            test(os.path.join(d, sub, 'maps'))
    for d in drives:
        test(os.path.join(d, 'maps'))
        for a in _subdirs(d):
            test(os.path.join(a, 'maps'))
            for b in _subdirs(a):
                test(os.path.join(b, 'maps'))

    def rank(p):
        lo = p.lower()
        return (0 if 'cartographer' in lo else 1, 0 if 'halo' in lo else 1, p)
    found.sort(key=rank)
    _auto_cache['vista_all'] = found
    return list(found)


def _auto_vista():
    """The best candidate (ranked: a path with "cartographer", then with "halo") whose shared.map really is a Halo 2 Vista (version 8) map."""
    if 'vista' not in _auto_cache:
        _auto_cache['vista'] = next((d for d in vista_candidates() if looks_like_vista_maps(d)), None)
    return _auto_cache['vista']


def vista_maps_dir(explicit=None, required=True):
    """The folder with the STOCK Halo 2 Vista maps (shared.map, mainmenu.map, single_player_shared.map, the stock maps): read, never written."""
    v, src = pick('vista_maps', explicit)
    if v is None:
        v, src = _auto_vista(), 'auto'
    if v is None:
        if not required:
            return None
        raise PathsError('The folder with the stock Halo 2 Vista maps (shared.map, mainmenu.map, single_player_shared.map ...) was not found.  It is the '
                         '"maps" folder of Halo 2 for Windows Vista with Project Cartographer.  Tell the converter where it is: %s.' % _how('vista_maps'))
    v = os.path.abspath(v)
    if src != 'auto':
        where = {'option': 'command line', 'env': 'environment', 'settings': 'settings file'}[src]
        if not os.path.isdir(v):
            raise PathsError('The Vista maps folder %s (from the %s) does not exist.  Point %s at the "maps" folder of Halo 2 Vista / Project Cartographer.'
                             % (v, where, OPTION['vista_maps']))
        missing = [n for n in VISTA_KEY_FILES if not _isfile(v, n)]
        if missing:
            raise PathsError('The Vista maps folder %s (from the %s) has no %s.  It must be the "maps" folder of Halo 2 Vista / Project Cartographer with '
                             'the stock maps (%s).' % (v, where, ', '.join(missing), ', '.join(VISTA_KEY_FILES)))
    return v


STOCK_VISTA_MAP_NAMES = frozenset((
    '00a_introduction', '01a_tutorial', '01b_spacestation', '03a_oldmombasa', '03b_newmombasa', '04a_gasgiant', '04b_floodlab',
    '05a_deltaapproach', '05b_deltatowers', '06a_sentinelwalls', '06b_floodzone', '07a_highcharity', '07b_forerunnership',
    '08a_deltacliffs', '08b_deltacontrol', 'zanzibar', 'ascension', 'backwash', 'beavercreek', 'burial_mounds', 'coagulation',
    'colossus', 'foundation', 'headlong', 'cyclotron', 'lockout', 'midship', 'waterworks', 'containment', 'warlock', 'deltatap',
    'turf', 'elongation', 'gemini', 'dune', 'triplicate', 'street_sweeper', 'needle', 'derelict', 'highplains',
    'shared', 'single_player_shared', 'mainmenu'))           # the 43 .map files of a stock Halo 2 Vista / Project Cartographer install
_warned_extra = set()


def extra_vista_maps(maps_dir):
    """The .map files in a Vista maps folder that are not stock Halo 2 maps (community maps, converted maps, test files)."""
    try:
        names = os.listdir(maps_dir)
    except OSError:
        return []
    return sorted(f for f in names if f.lower().endswith('.map') and os.path.splitext(f)[0].lower() not in STOCK_VISTA_MAP_NAMES)


def warn_extra_vista_maps(maps_dir, stream=None):
    """Once per folder and process: say on stderr that the Vista maps folder holds maps that are not stock maps.  The converters read EVERY
    .map of that folder as a donor / reference, so extra maps can change which tags are linked or copied.  Returns the list of extras."""
    key = _norm(maps_dir)
    if key in _warned_extra:
        return []
    _warned_extra.add(key)
    extra = extra_vista_maps(maps_dir)
    if extra:
        shown = ', '.join(extra[:6]) + (' ... (%d in all)' % len(extra) if len(extra) > 6 else '')
        print('WARNING: the Vista maps folder %s holds %d .map file(s) that are not stock Halo 2 maps (%s).  Every .map in this folder is used as '
              'a donor, so the result can depend on them.  Use a folder with only the stock maps (--vista-maps).' % (maps_dir, len(extra), shown),
              file=stream or sys.stderr)
    return extra


# --------------------------------------------------------------------------------------------------
# H2EK maps (opt-in donors), Excession, outputs, work
# --------------------------------------------------------------------------------------------------
def h2ek_maps_dir(explicit=None):
    """The MCC Halo 2 Editing Kit's built-maps folder, used as an extra (last) donor tier by Vista -> MCC.  NEVER auto-detected: None unless
    --h2ek-maps / H2CONV_H2EK_MAPS / "h2ek_maps_dir" names it.  The kit's root folder is accepted too (its h2_maps_win64_dx11 is used)."""
    v, src = pick('h2ek_maps', explicit)
    if v is None:
        return None
    v = os.path.abspath(v)
    sub = os.path.join(v, H2EK_MAPS_NAME)
    if not os.path.isdir(v):
        raise PathsError('The H2EK maps folder %s does not exist (%s).' % (v, 'from ' + {'option': '--h2ek-maps', 'env': 'H2CONV_H2EK_MAPS',
                                                                                         'settings': 'the settings file'}[src]))
    if os.path.isdir(sub) and not any(n.lower().endswith('.map') for n in os.listdir(v)):
        v = sub
    return v


def sample_maps():
    """{'custom': the H2EK headlong, 'mcc': the stock MCC headlong (the untouched copy in 'Backup NOT MODDED' when there is one), 'vista': the stock Vista
    headlong, 'vista_shared' / 'vista_sp_shared' / 'vista_mainmenu': the Vista databases} for the developer tools and tests that open maps by key; a key whose
    folder is not configured is missing.  The converter itself never uses this."""
    out = {}
    try:
        h2ek = h2ek_maps_dir()
    except PathsError:
        h2ek = None
    mcc = mcc_maps_dir(required=False)
    vista = vista_maps_dir(required=False)
    if h2ek:
        out['custom'] = os.path.join(h2ek, 'headlong.map')
    if mcc:
        backup = os.path.join(mcc, STOCK_BACKUP_DIR_NAME, 'headlong.map')
        out['mcc'] = backup if os.path.isfile(backup) else os.path.join(mcc, 'headlong.map')
    if vista:
        out['vista'] = os.path.join(vista, 'headlong.map')
        out['vista_shared'] = os.path.join(vista, 'shared.map')
        out['vista_sp_shared'] = os.path.join(vista, 'single_player_shared.map')
        out['vista_mainmenu'] = os.path.join(vista, 'mainmenu.map')
    return out


def h2ek_tags_dir():
    """<H2EK root>\\tags when an H2EK maps folder is configured and the kit's tags are beside it (diagnostics only), else None."""
    try:
        m = h2ek_maps_dir()
    except PathsError:
        return None
    if not m:
        return None
    t = os.path.join(os.path.dirname(m), 'tags')
    return t if os.path.isdir(t) else None


def excession_dir():
    """Excession's install folder (optional: only its built-in Halo 2 data file is read), or None."""
    v, _src = pick('excession')
    if v and os.path.isdir(v):
        return os.path.abspath(v)
    if 'excession' not in _auto_cache:
        found = None
        for L in steam_libraries():
            d = os.path.join(L, 'steamapps', 'common', 'Excession')
            if os.path.isdir(d):
                found = d
                break
        _auto_cache['excession'] = found
    return _auto_cache['excession']


def excession_halo2_json():
    d = excession_dir()
    return os.path.join(d, 'Data', 'BuiltInData', 'Halo2.json') if d else None


def work_dir():
    """Caches and scratch files (decompressed MCC maps ~2 GB, transcoded sounds ~0.5 GB, planner index).  Created by whoever writes into it."""
    v, _src = pick('work')
    return os.path.abspath(v) if v else os.path.join(local_appdata_dir(), APP_NAME, 'work')


def work_subdir(name):
    return os.path.join(work_dir(), name)


def vista_out_dir(explicit=None, required=True):
    """Where MCC -> Vista writes (Cartographer's custom maps folder): option / settings, else <Documents>\\My Games\\Halo 2\\Maps when
    <Documents>\\My Games\\Halo 2 exists."""
    v, _src = pick('vista_out', explicit)
    if v:
        return os.path.abspath(v)
    d = documents_maps_dir()
    if os.path.isdir(os.path.dirname(d)):
        return d
    if not required:
        return None
    raise PathsError('There is no output folder for the Vista maps yet: %s.  (Project Cartographer reads custom maps from '
                     '<Documents>\\My Games\\Halo 2\\Maps.)' % _how('vista_out'))


def mcc_out_dir(explicit=None, required=True):
    """The Excession mod package folder Vista -> MCC writes.  No default: it has to be chosen once (batch remembers it)."""
    v, _src = pick('mcc_out', explicit)
    if v:
        return os.path.abspath(v)
    if not required:
        return None
    raise PathsError('Vista -> MCC needs an output folder (an Excession mod package folder) and none was chosen yet: %s.  Batch mode remembers it '
                     'for the next runs.' % _how('mcc_out'))


# --------------------------------------------------------------------------------------------------
# Assembly layout files
# --------------------------------------------------------------------------------------------------
SCRIPTING_MCC_NAMES = ('H2MCC_Scripting.xml', 'H2MCC_2023Jul_Scripting.xml')     # the installed name, then the name in the Assembly source (same content)


class AssemblyLayout(object):
    """The Assembly files the converter reads, found under `root`:  plugins_vista (Plugins\\Halo2, 98), plugins_mcc (Plugins\\Halo2MCC, 35),
    groupnames_xml, scripting_vista_xml, scripting_mcc_xml."""

    def __init__(self, root, source, plugins_root, formats_root):
        self.root = root
        self.source = source                                    # 'bundled' | 'option' | 'env' | 'settings'
        self.plugins_root = plugins_root
        self.formats_root = formats_root
        self.plugins_vista = os.path.join(plugins_root, 'Halo2')
        self.plugins_mcc = os.path.join(plugins_root, 'Halo2MCC')
        self.groupnames_xml = os.path.join(formats_root, 'Halo2Vista', 'H2V_GroupNames.xml')
        self.scripting_vista_xml = os.path.join(formats_root, 'Halo2Vista', 'H2V_Scripting.xml')
        self.scripting_mcc_xml = None
        for n in SCRIPTING_MCC_NAMES:
            p = os.path.join(formats_root, 'Halo2MCC', n)
            if os.path.isfile(p):
                self.scripting_mcc_xml = p
                break
        if self.scripting_mcc_xml is None:
            self.scripting_mcc_xml = os.path.join(formats_root, 'Halo2MCC', SCRIPTING_MCC_NAMES[0])

    def counts(self):
        def n(d):
            try:
                return sum(1 for f in os.listdir(d) if f.lower().endswith('.xml'))
            except OSError:
                return 0
        return {'Halo2': n(self.plugins_vista), 'Halo2MCC': n(self.plugins_mcc)}

    def problems(self):
        """List of readable problems (empty = usable)."""
        out = []
        for label, d in (('Plugins\\Halo2', self.plugins_vista), ('Plugins\\Halo2MCC', self.plugins_mcc)):
            if not os.path.isdir(d):
                out.append('the folder %s' % d)
        for p in (self.groupnames_xml, self.scripting_vista_xml, self.scripting_mcc_xml):
            if not os.path.isfile(p):
                out.append('the file %s' % p)
        if not out:
            have = {'Halo2': self.plugins_vista, 'Halo2MCC': self.plugins_mcc}
            missing = []
            for k, stems in ASSEMBLY_PLUGIN_STEMS.items():
                try:
                    names = set(f.lower() for f in os.listdir(have[k]))
                except OSError:
                    names = set()
                missing += [os.path.join(have[k], s.strip() + '.xml') for s in stems if (s.strip() + '.xml').lower() not in names]
            if missing:
                out.append('the plugin file%s %s' % ('s' if len(missing) > 1 else '', ', '.join(missing[:3]) + (' and %d more' % (len(missing) - 3) if len(missing) > 3 else '')))
            c = self.counts()
            for k, need in ASSEMBLY_MIN_PLUGINS.items():
                if c[k] < need and not missing:
                    out.append('%d plugin files in Plugins\\%s (at least %d expected)' % (c[k], k, need))
        return out


def _assembly_candidates(d):
    """(plugins_root, formats_root) pairs inside folder `d`: an installed Assembly (Plugins + Formats), the unpacked source
    (src\\Assembly\\Plugins + src\\Blamite\\Formats), both also one wrapper folder down (Assembly-2026.02.16\\...)."""
    bases = [d] + _subdirs(d)
    out = []
    for b in bases:
        out.append((os.path.join(b, 'Plugins'), os.path.join(b, 'Formats')))
        out.append((os.path.join(b, 'src', 'Assembly', 'Plugins'), os.path.join(b, 'src', 'Blamite', 'Formats')))
        out.append((os.path.join(b, 'Assembly', 'Plugins'), os.path.join(b, 'Blamite', 'Formats')))
    return out


def find_assembly(d, source='option'):
    """AssemblyLayout for folder `d` or a PathsError that names the first missing file, the folders searched and the tested Assembly version."""
    d = os.path.abspath(d)
    if not os.path.isdir(d):
        raise PathsError('The Assembly folder %s does not exist.  It must hold the Assembly layout files (Plugins\\Halo2, Plugins\\Halo2MCC and '
                         'Formats\\Halo2Vista, Formats\\Halo2MCC), as an installed Assembly or as the unpacked Assembly source.  Tested with Assembly %s.'
                         % (d, ASSEMBLY_TESTED))
    cands = _assembly_candidates(d)
    best = None
    for pr, fr in cands:
        if os.path.isdir(os.path.join(pr, 'Halo2')) and os.path.isdir(os.path.join(pr, 'Halo2MCC')):
            lay = AssemblyLayout(d, source, pr, fr)
            if not lay.problems():
                return lay
            if best is None:
                best = lay
    if best is None:
        raise PathsError('No Assembly layout files found in %s: there is no Plugins\\Halo2 + Plugins\\Halo2MCC folder in it (searched %s, one folder level down '
                         'as well).  The folder must hold an installed Assembly (Plugins, Formats) or the unpacked Assembly source '
                         '(src\\Assembly\\Plugins, src\\Blamite\\Formats).  Tested with Assembly %s.'
                         % (d, ', '.join(['%s' % os.path.relpath(p, d) for p, _f in cands[:3]]), ASSEMBLY_TESTED))
    raise PathsError('The Assembly folder %s is incomplete: missing %s (searched under %s).  Tested with Assembly %s; the converter needs the Halo 2 '
                     'plugins and the 3 Halo 2 format files (H2V_GroupNames.xml, H2V_Scripting.xml, %s).'
                     % (d, '; '.join(best.problems()), best.plugins_root, ASSEMBLY_TESTED, ' or '.join(SCRIPTING_MCC_NAMES)))


def assembly_layout(explicit=None):
    """The Assembly files to use: --assembly-dir / H2CONV_ASSEMBLY / "assembly_dir", else the copy bundled in h2conv\\data\\assembly.
    Validated once per folder (the plugin counts and the format files); raises PathsError with one message when something is missing."""
    v, src = pick('assembly', explicit)
    if v is None:
        v, src = bundled_assembly_dir(), 'bundled'
    key = ('assembly', _norm(v))
    lay = _auto_cache.get(key)
    if lay is None:
        lay = _auto_cache[key] = find_assembly(v, src)
        lay.source = src
    return lay


def assembly_dir(explicit=None):
    return assembly_layout(explicit).root


# --------------------------------------------------------------------------------------------------
# command line helpers
# --------------------------------------------------------------------------------------------------
_COMMON = {'--mcc-maps': 'mcc_maps', '--vista-maps': 'vista_maps', '--assembly-dir': 'assembly', '--h2ek-maps': 'h2ek_maps'}


def add_common_options(ap, mcc=False, vista=False, assembly=True, h2ek=False):
    """Add --assembly-dir (and, when asked, --mcc-maps / --vista-maps / --h2ek-maps) to an argparse parser, with the resolution order in the help."""
    if mcc:
        ap.add_argument('--mcc-maps', default=None, help='folder with the MCC maps / textures.dat / sounds_*.dat (default: H2CONV_MCC_MAPS, the settings '
                                                         'file, then your Steam library)')
    if vista:
        ap.add_argument('--vista-maps', default=None, help='folder with the stock Vista maps (shared.map, mainmenu.map, ...) (default: H2CONV_VISTA_MAPS, '
                                                           'the settings file, then a search of your drives)')
    if assembly:
        ap.add_argument('--assembly-dir', default=None, help='folder with the Assembly layout files (Plugins, Formats); default: H2CONV_ASSEMBLY, the '
                                                             'settings file, then the copy in h2conv\\data\\assembly')
    if h2ek:
        ap.add_argument('--h2ek-maps', default=None, help='MCC Halo 2 Editing Kit maps folder: its maps become an extra, last donor tier (default: '
                                                          'H2CONV_H2EK_MAPS or the settings file; otherwise none)')


def configure_from_args(a):
    """configure() from an argparse namespace made with add_common_options() (missing attributes are ignored)."""
    configure(mcc_maps=getattr(a, 'mcc_maps', None), vista_maps=getattr(a, 'vista_maps', None), assembly=getattr(a, 'assembly_dir', None),
              h2ek_maps=getattr(a, 'h2ek_maps', None), vista_out=getattr(a, 'vista_out', None), mcc_out=getattr(a, 'mcc_out', None))


def consume_common_options(argv):
    """For tools that parse argv by hand: remove `--assembly-dir X`, `--mcc-maps X`, `--vista-maps X`, `--h2ek-maps X` (also `--opt=X`) from argv,
    configure() them, return the remaining arguments."""
    rest, vals, i = [], {}, 0
    argv = list(argv)
    while i < len(argv):
        a = argv[i]
        name, eq, val = a.partition('=')
        if name in _COMMON:
            if eq:
                vals[_COMMON[name]] = val
            elif i + 1 < len(argv):
                vals[_COMMON[name]] = argv[i + 1]
                i += 1
        else:
            rest.append(a)
        i += 1
    configure(**vals)
    return rest


# --------------------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------------------
def _source_of(kind, auto_value):
    v, src = pick(kind)
    if v:
        return v, src
    return auto_value, ('auto' if auto_value else None)


def describe():
    """[{'item','kind','path','source','ok','detail','how'}] for every folder (what `py -3 -m h2conv.paths` prints)."""
    rows = []

    def add(kind, path, source, ok, detail, required=True):
        rows.append({'item': LABEL[kind], 'kind': kind, 'path': path, 'source': source, 'ok': ok, 'required': required, 'detail': detail,
                     'how': None if ok else _how(kind)})

    # MCC
    try:
        p = mcc_maps_dir()
        v, src = pick('mcc_maps')
        n_maps = len([f for f in os.listdir(p) if f.lower().endswith('.map')])
        files = [n for n in ('shared.map', 'textures.dat', 'sounds_neutral.dat', 'sounds_en.dat') if not _isfile(p, n)]
        add('mcc_maps', p, src or 'auto', not files and n_maps > 0, ('%d .map files' % n_maps) + (('; missing ' + ', '.join(files)) if files else ''))
    except PathsError as ex:
        add('mcc_maps', None, None, False, str(ex))
    # Vista
    try:
        p = vista_maps_dir()
        v, src = pick('vista_maps')
        n_maps = len([f for f in os.listdir(p) if f.lower().endswith('.map')])
        add('vista_maps', p, src or 'auto', True, '%d .map files' % n_maps)
        others = [c for c in vista_candidates() if _norm(c) != _norm(p)] if not v else []
        if others:
            rows[-1]['detail'] += '; other candidates: ' + ' | '.join(others[:4])
    except PathsError as ex:
        add('vista_maps', None, None, False, str(ex))
    # Assembly
    try:
        lay = assembly_layout()
        c = lay.counts()
        add('assembly', lay.root, lay.source, True, '%d Halo2 + %d Halo2MCC plugins, scripting tables: %s' % (
            c['Halo2'], c['Halo2MCC'], os.path.basename(lay.scripting_mcc_xml)) + '; tested with Assembly ' + ASSEMBLY_TESTED)
    except PathsError as ex:
        add('assembly', None, None, False, str(ex))
    # work
    w = work_dir()
    v, src = pick('work')
    add('work', w, src or 'default', True, 'exists' if os.path.isdir(w) else 'will be created when the first cache is written')
    # H2EK
    try:
        h = h2ek_maps_dir()
        v, src = pick('h2ek_maps')
        add('h2ek_maps', h, src if h else 'not set', True, ('%d .map files' % len([f for f in os.listdir(h) if f.lower().endswith('.map')])) if h
            else 'optional: not used (pass --h2ek-maps to use it)', required=False)
    except PathsError as ex:
        add('h2ek_maps', None, None, False, str(ex), required=False)
    # outputs
    v, src = pick('vista_out')
    o = vista_out_dir(required=False)
    add('vista_out', o, src or ('default' if o else 'not set'), True, '' if o else 'optional for single conversions; batch MCC -> Vista needs it', required=False)
    v, src = pick('mcc_out')
    o = mcc_out_dir(required=False)
    add('mcc_out', o, src or 'not set', True, '' if o else 'optional for single conversions; batch Vista -> MCC needs --mcc-out the first time', required=False)
    e = excession_dir()
    v, src = pick('excession')
    add('excession', e, (src or 'auto') if e else 'not found', True, 'optional: only its built-in Halo 2 data is read' if e else 'optional (not needed to convert)',
        required=False)
    return rows


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='h2conv.paths', description='Show where the converter looks for every folder it needs, where each value came '
                                 'from (option / env / settings / auto / bundled), and whether the key files are there.')
    add_common_options(ap, mcc=True, vista=True, assembly=True, h2ek=True)
    ap.add_argument('--json', action='store_true', help='machine readable output')
    a = ap.parse_args(argv)
    configure_from_args(a)
    rows = describe()
    if a.json:
        print(json.dumps({'settings_file': settings_file(), 'settings_file_exists': os.path.isfile(settings_file()), 'folders': rows}, indent=1))
    else:
        print('settings file: %s%s' % (settings_file(), '' if os.path.isfile(settings_file()) else '  (not created yet)'))
        for r in rows:
            tag = 'ok     ' if r['ok'] else ('MISSING' if r['required'] else 'note   ')
            print('%s %-52s %s' % (tag, r['item'], r['path'] or '-'))
            if r['ok'] or not r['required'] and not r['path']:
                print('         %s%s' % (('[%s] ' % r['source']) if r['source'] else '', r['detail']))
            else:
                print('         ' + r['detail'])
    bad = [r for r in rows if r['required'] and not r['ok']]
    if bad and not a.json:
        print('\n%d required item(s) missing: %s' % (len(bad), ', '.join(r['item'] for r in bad)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
