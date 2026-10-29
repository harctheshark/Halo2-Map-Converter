# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""The converter's cache folders: how big they are, trimming them to a size cap, clearing them.

    py -3 -m h2conv cache info            where the cache is, size and file count per folder, the size cap
    py -3 -m h2conv cache trim            delete the least recently used cache files until the cache is under the cap
    py -3 -m h2conv cache clear [--yes]   delete every cache file (they are rebuilt when needed; the next conversion is slower)

What is cached (all below the work folder: H2CONV_WORK / settings.json "work_dir" / %LOCALAPPDATA%\\H2MapConverter\\work):

    mcc_decompressed\\   uncompressed copies of the compressed MCC maps (100-600 MB each) + cache_index.json (source map -> copy)
    sound_cache\\        one file per converted sound
    rev_planner\\        planner index of the Vista to MCC direction (small)
    validate_enum_seen.json                                                        (tiny)

The cap is settings.json "cache_limit_gb" (default 5; 0 = never trim by itself; H2CONV_CACHE_LIMIT_GB overrides it).  It is applied at
the end of a conversion run (`convert`, `rev`, and once at the end of a `batch`, not in every child process of a batch), oldest-USED
files first.  "Used" is the file's modification time: every cache hit bumps it with os.utime (NTFS last-access times are unreliable).
Files the run itself used are never removed by its own trim.  A file that cannot be deleted (another instance has it open) is skipped.

Stdlib only.  A trim can never fail a conversion (trim_after_job swallows every error).
"""
import json
import os
import sys
import threading
import time

from . import paths as _paths

GB = 1 << 30
INDEX_NAME = 'cache_index.json'                  # inside mcc_decompressed: normalised source path -> name of the decompressed copy
ENV_NO_TRIM = 'H2CONV_NO_CACHE_TRIM'             # set by `batch --isolate` in its children: only the batch parent trims
DECOMP, SOUND, PLANNER, ENUM = 'mcc_decompressed', 'sound_cache', 'rev_planner', 'validate_enum_seen.json'
TRIMMABLE = (DECOMP, SOUND)                      # the big folders; the small ones are only counted
CATEGORIES = (DECOMP, SOUND, PLANNER, ENUM)      # what `cache clear` deletes
PART_GUARD_S = 3600                              # a half-written *.part / *.tmp younger than this may belong to a running instance: never trimmed
DATABASE_NAMES = ('shared.map', 'single_player_shared.map', 'mainmenu.map')   # = mcc_cache.DATABASE_FILE_NAMES (kept here: no import cycle)

_lock = threading.Lock()
_used = set()                                    # normalised paths of the decompressed copies this process returned
_job_start = [time.time()]


# ----------------------------------------------------------------------------------------------------------------------
# bookkeeping done by the cache users
# ----------------------------------------------------------------------------------------------------------------------
def _norm(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def begin_job():
    """Called by the entry points at their start: files the job uses from now on are protected from its own trim."""
    _job_start[0] = time.time()
    with _lock:
        _used.clear()


def note_used(path):
    with _lock:
        _used.add(_norm(path))


def is_used(path):
    with _lock:
        return _norm(path) in _used


def touch(path):
    """Mark a cache file as used now (mtime = least-recently-used order)."""
    try:
        os.utime(path, None)
    except (OSError, ValueError):
        pass


def read_index(directory):
    """{normalised source path: file name} of the decompressed copies; {} when the index is missing / unreadable / not a mapping."""
    try:
        with open(os.path.join(directory, INDEX_NAME), 'r', encoding='utf-8') as fh:
            d = json.load(fh)
        s = d.get('sources') if isinstance(d, dict) else None
        if isinstance(s, dict):
            return dict((str(k), v) for k, v in s.items() if isinstance(v, str) and v and os.path.basename(v) == v)
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def write_index(directory, sources):
    try:
        os.makedirs(directory, exist_ok=True)
        p = os.path.join(directory, INDEX_NAME)
        tmp = '%s.%d.%d.tmp' % (p, os.getpid(), threading.get_ident())
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'version': 1, 'sources': sources}, fh, indent=1, sort_keys=True)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def register_decompressed(directory, src_path, dst_path):
    """A decompressed copy `dst_path` of the map `src_path` was just returned (made now, or an existing hit): mark it used, and when the same
    source file had another copy before (it was edited and rebuilt: a new header = a new hashed name) delete that older copy.  Never deletes
    dst itself, a copy this process uses, a copy another source path also points at, or a bare stock database name.  Never raises."""
    try:
        note_used(dst_path)
        touch(dst_path)
        name = os.path.basename(dst_path)
        key = _norm(src_path)
        idx = read_index(directory)
        old = idx.get(key)
        if old == name:
            return None
        idx[key] = name
        write_index(directory, idx)
        if not old or old == name:
            return None
        if any(v == old for k, v in idx.items()):                               # another source file shares that copy
            return None
        if old.lower() in DATABASE_NAMES:                                       # the bare database name is the planner's STOCK copy: never ours to delete
            return None
        p = os.path.join(directory, old)
        if _norm(p) == _norm(dst_path) or is_used(p) or not os.path.isfile(p):
            return None
        try:
            os.remove(p)
            return p
        except OSError:                                                         # open in another instance: keep it, trim removes it later
            return None
    except Exception:                                                           # noqa: BLE001 - bookkeeping must never fail a conversion
        return None


# ----------------------------------------------------------------------------------------------------------------------
# looking at the cache
# ----------------------------------------------------------------------------------------------------------------------
def cache_root(work=None):
    return os.path.abspath(work) if work else _paths.work_dir()


def _is_link(path):
    """A symlink or a Windows junction / mount point (never followed or cleared)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    import stat as _stat
    return _stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


def _files_in(folder):
    """[(path, size, mtime)] of every regular file below `folder` (links are not followed)."""
    out = []
    stack = [folder]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink() or _is_link(e.path):
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    out.append((e.path, st.st_size, st.st_mtime))
            except OSError:
                continue
    return out


def category_files(name, work=None):
    p = os.path.join(cache_root(work), name)
    if name == ENUM:
        try:
            st = os.stat(p)
            return [(p, st.st_size, st.st_mtime)] if os.path.isfile(p) else []
        except OSError:
            return []
    if not os.path.isdir(p) or _is_link(p):
        return []
    return _files_in(p)


def info(work=None):
    """-> dict: root, limit_gb, total_bytes, categories [{name, path, files, bytes, trimmable}]."""
    root = cache_root(work)
    cats = []
    total = 0
    for name in CATEGORIES:
        fs = category_files(name, work)
        n = sum(f[1] for f in fs)
        total += n
        cats.append({'name': name, 'path': os.path.join(root, name), 'files': len(fs), 'bytes': n, 'trimmable': name in TRIMMABLE})
    return {'root': root, 'limit_gb': _paths.cache_limit_gb(), 'total_bytes': total, 'files': sum(c['files'] for c in cats), 'categories': cats}


def total_bytes(work=None):
    return sum(f[1] for name in CATEGORIES for f in category_files(name, work))


def human(n):
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%d B' % n) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024.0


# ----------------------------------------------------------------------------------------------------------------------
# trimming and clearing
# ----------------------------------------------------------------------------------------------------------------------
def trim(limit_bytes=None, work=None, protect_since=None, protect=(), dry_run=False, now=None):
    """Delete the least recently used files of mcc_decompressed and sound_cache until the whole cache (the small folders count) is <= limit.
    limit_bytes None = the configured cap (settings "cache_limit_gb"); 0 = no trimming.  protect_since: files modified at or after this time
    (time.time() value) survive; protect: paths that survive.  Files this process returned from ensure_decompressed survive.  A file that
    cannot be deleted is skipped.  -> dict(limit, before, after, removed [paths], freed, skipped [paths], protected)."""
    if limit_bytes is None:
        limit_bytes = int(_paths.cache_limit_gb() * GB)
    now = time.time() if now is None else now
    prot = set(_norm(p) for p in protect)
    files = []
    before = 0
    for name in CATEGORIES:
        for path, size, mtime in category_files(name, work):
            before += size
            if name in TRIMMABLE and os.path.basename(path) != INDEX_NAME:
                files.append((mtime, path, size))
    res = {'limit': int(limit_bytes), 'before': before, 'after': before, 'removed': [], 'freed': 0, 'skipped': [], 'protected': 0}
    if limit_bytes <= 0 or before <= limit_bytes:
        return res
    files.sort(key=lambda t: (t[0], t[1]))                                       # oldest use first
    total = before
    for mtime, path, size in files:
        if total <= limit_bytes:
            break
        low = path.lower()
        if (protect_since is not None and mtime >= protect_since) or _norm(path) in prot or is_used(path) \
                or (low.endswith(('.part', '.tmp')) and now - mtime < PART_GUARD_S):
            res['protected'] += 1
            continue
        if dry_run:
            ok = True
        else:
            try:
                os.remove(path)
                ok = True
            except OSError:
                ok = False
        if ok:
            res['removed'].append(path)
            res['freed'] += size
            total -= size
        else:
            res['skipped'].append(path)
    res['after'] = total
    if not dry_run:
        _prune_empty(os.path.join(cache_root(work), SOUND))
    return res


def _prune_empty(folder):
    """Remove empty sub-folders of the sound cache (not the folder itself)."""
    try:
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if os.path.isdir(p) and not _is_link(p):
                try:
                    os.rmdir(p)                               # only when empty
                except OSError:
                    pass
    except OSError:
        pass


def trim_after_job(log=None, work=None):
    """The automatic trim at the end of a conversion run.  The files this run used survive (modified since begin_job(), or returned by
    ensure_decompressed).  Does nothing when the cap is 0 or H2CONV_NO_CACHE_TRIM is set.  NEVER raises.  `log(text)` gets one line when
    something was removed or skipped.  -> the trim() result, or None."""
    if os.environ.get(ENV_NO_TRIM):
        return None
    try:
        lim = _paths.cache_limit_gb()
        if lim <= 0:
            return None
        res = trim(int(lim * GB), work=work, protect_since=_job_start[0] - 2.0)
        if log and (res['removed'] or res['skipped']):
            log('cache: trimmed to the %s cap: %d old file(s) removed, %s freed (now %s)%s' % (
                ('%g GB' % lim), len(res['removed']), human(res['freed']), human(res['after']),
                ('; %d in use were kept' % len(res['skipped'])) if res['skipped'] else ''))
        return res
    except Exception as ex:                                                      # noqa: BLE001 - a trim must never fail the conversion
        if log:
            try:
                log('cache: trim skipped (%s: %s)' % (type(ex).__name__, ex))
            except Exception:                                                    # noqa: BLE001
                pass
        return None


def clear(work=None, dry_run=False):
    """Delete every file of the cache folders (links inside are removed as links, never followed; a cache folder that is itself a link is skipped).
    -> dict(removed (count), freed (bytes), failed [paths], skipped_links [paths])."""
    root = cache_root(work)
    res = {'removed': 0, 'freed': 0, 'failed': [], 'skipped_links': []}
    for name in CATEGORIES:
        p = os.path.join(root, name)
        if not os.path.lexists(p):
            continue
        if _is_link(p):
            res['skipped_links'].append(p)
            continue
        if name == ENUM:
            files, dirs = category_files(name, work), []
        else:
            files = _files_in(p)
            dirs = _dirs_in(p)
        for path, size, _m in files:
            if dry_run:
                res['removed'] += 1
                res['freed'] += size
                continue
            try:
                os.remove(path)
                res['removed'] += 1
                res['freed'] += size
            except OSError:
                res['failed'].append(path)
        if not dry_run and name != ENUM:
            for d in sorted(dirs, key=len, reverse=True) + [p]:
                try:
                    os.rmdir(d)
                except OSError:
                    pass
    return res


def _dirs_in(folder):
    out = []
    stack = [folder]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False) and not _is_link(e.path):
                            out.append(e.path)
                            stack.append(e.path)
                    except OSError:
                        pass
        except OSError:
            pass
    return out


# ----------------------------------------------------------------------------------------------------------------------
# the command   h2conv cache info|trim|clear
# ----------------------------------------------------------------------------------------------------------------------
def print_info(inf, out=None):
    w = (out or sys.stdout).write
    lim = inf['limit_gb']
    w('cache folder : %s\n' % inf['root'])
    for c in inf['categories']:
        w('  %-24s %8s  %6d file(s)\n' % (c['name'], human(c['bytes']), c['files']))
    w('  %-24s %8s  %6d file(s)\n' % ('total', human(inf['total_bytes']), inf['files']))
    w('size cap     : %s\n' % (('%g GB (settings.json "cache_limit_gb"; 0 = no automatic trimming)' % lim) if lim > 0 else '0 = no automatic trimming'))
    if lim > 0 and inf['total_bytes'] > lim * GB:
        w('over the cap : yes - `h2conv cache trim` removes the oldest files; a conversion run does it by itself at its end\n')


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='h2conv cache', description='Look at, trim or clear the converter\'s cache folders (decompressed MCC maps, converted '
                                 'sounds).  Caches are rebuilt when needed; deleting them only makes the next conversion slower.')
    ap.add_argument('--work', default=None, metavar='DIR', help='cache (work) folder to use instead of H2CONV_WORK / settings.json / the default')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('info', help='folder, size per category, file counts, size cap')
    s.add_argument('--json', action='store_true', help='print the numbers as JSON')
    s = sub.add_parser('trim', help='delete the least recently used files until the cache is under the cap')
    s.add_argument('--limit-gb', type=float, default=None, metavar='GB', help='use this cap instead of settings.json "cache_limit_gb" (default 5)')
    s.add_argument('--dry-run', action='store_true', help='only say what would be removed')
    s = sub.add_parser('clear', help='delete every cache file')
    s.add_argument('--yes', '-y', action='store_true', help='do not ask (required when there is no keyboard to answer: a script, a pipe)')
    s.add_argument('--dry-run', action='store_true', help='only say what would be removed')
    a = ap.parse_args(argv)
    work = a.work
    if a.cmd == 'info':
        inf = info(work)
        if a.json:
            print(json.dumps(inf, indent=1))
        else:
            print_info(inf)
        return 0
    if a.cmd == 'trim':
        lim = _paths.cache_limit_gb() if a.limit_gb is None else a.limit_gb
        if lim <= 0:
            print('the size cap is 0 (no trimming); nothing removed.  Give --limit-gb to trim to a size.')
            return 0
        r = trim(int(lim * GB), work=work, dry_run=a.dry_run)
        print('cache %s: %s -> %s (cap %g GB): %d file(s) %s, %s freed%s' % (
            cache_root(work), human(r['before']), human(r['after']), lim, len(r['removed']), 'would go' if a.dry_run else 'removed', human(r['freed']),
            ('; %d file(s) in use were skipped' % len(r['skipped'])) if r['skipped'] else ''))
        return 0
    # clear
    inf = info(work)
    if inf['files'] == 0:
        print('the cache is already empty (%s)' % inf['root'])
        return 0
    if not a.yes and not a.dry_run:
        if not sys.stdin or not sys.stdin.isatty():
            print('ERROR: `cache clear` deletes %d file(s), %s, from %s - pass --yes to confirm (no keyboard to ask).' % (inf['files'], human(inf['total_bytes']), inf['root']))
            return 2
        try:
            ans = input('Delete %d cache file(s), %s, from %s ? [y/N] ' % (inf['files'], human(inf['total_bytes']), inf['root']))
        except EOFError:                          # stdin looks like a console (Windows reports NUL as one) but nobody can answer
            print()
            print('ERROR: no answer is possible - pass --yes to confirm.')
            return 2
        if ans.strip().lower() not in ('y', 'yes'):
            print('nothing deleted')
            return 1
    r = clear(work, dry_run=a.dry_run)
    print('%s %d file(s), %s freed from %s' % ('would delete' if a.dry_run else 'deleted', r['removed'], human(r['freed']), inf['root']))
    for p in r['skipped_links']:
        print('skipped (the folder is a link, not followed): %s' % p)
    if r['failed']:
        print('%d file(s) could not be deleted (in use by another program?): %s%s' % (len(r['failed']), r['failed'][0], ' ...' if len(r['failed']) > 1 else ''))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
