# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""The window program: pick a folder, let it find every Halo 2 map below it, convert them all.

    py -3 -m h2conv gui                       open the window
    py -3 -m h2conv gui --selftest            import every module, build the controller, check the bundled data; opens NO window (exit 0 = fine)
    Halo 2 Map Converter.exe                  the Windows build opens the same window

Two layers, so that all the logic can be tested without a screen:

  * Controller / model  (this file, top part): settings, folders, the scan, the options, the command line of the worker, the progress
    lines, the summary of a batch report.  It never imports tkinter.
  * View (class App, bottom part): a thin tkinter layer that only draws what the controller knows and forwards clicks to it.
    tkinter is imported when a window is built, not before.

The conversions themselves are NOT done in this process.  "Convert" starts a worker, which is the command line program running
`batch run ... --isolate` (every map in its own process, so a crash or a hang cannot stop the batch); the window reads its output, shows
the progress and, at the end, the summary: converted / skipped / FAILED maps with a one-line reason each, and the output folders.
Nothing is ever uploaded: no part of the program makes a network connection.
"""
import collections
import contextlib
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from . import __version__
from . import batch as B
from . import excession as EX
from . import mapinfo as MI
from . import paths as P

OD = collections.OrderedDict
APP_TITLE = 'Halo 2 Map Converter'
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000
NORMAL_PRIORITY = 0x00000020
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DIRECTION_LABELS = OD([
    ('auto', 'Automatic: Vista maps go to MCC, MCC maps go to Vista'),
    ('vista2mcc', 'Vista maps to MCC only (an Excession mod package)'),
    ('mcc2vista', 'MCC maps to Vista only (Project Cartographer)'),
])
# plain words for the machine codes of h2conv.batch / h2conv.mapinfo
SKIP_WORDS = OD([
    ('database', 'game database (shared / mainmenu), not a playable map'),
    ('custom_shared', 'needs a custom shared.map - not supported, skipped'),
    ('duplicate', 'byte-identical copy of another map'),
    ('up_to_date', 'already converted (tick "convert again" to redo it)'),
    ('exists', 'the output file already exists (tick "replace" to overwrite it)'),
    ('wrong_direction', 'the other direction is selected'),
    ('not_selected', 'multiplayer / campaign maps are not selected'),
    ('limit', 'beyond the limit'),
    ('cancelled', 'not started: the batch was stopped'),
])
SOURCE_WORDS = {'auto': 'found automatically', 'settings': 'from your saved settings', 'env': 'from an environment variable',
                'option': 'from the command line', 'bundled': 'bundled with the program', 'chosen': 'chosen by you',
                'default': 'the standard folder', 'none': 'not set'}
FOLDER_ORDER = ('mcc_maps', 'vista_maps', 'assembly', 'h2ek_maps', 'mcc_out', 'vista_out')
FOLDER_LABELS = OD([
    ('mcc_maps', 'Halo 2 MCC maps folder'),
    ('vista_maps', 'Halo 2 Vista maps folder (stock maps)'),
    ('assembly', 'Assembly layout files'),
    ('h2ek_maps', 'MCC Halo 2 Editing Kit maps (optional extra donors)'),
    ('mcc_out', 'Excession mod package (output of Vista to MCC)'),
    ('vista_out', 'Halo 2 maps folder (output of MCC to Vista)'),
])
MCC_HINT = 'the halo2\\h2_maps_win64_dx11 folder of Halo: The Master Chief Collection'
VISTA_HINT = 'the "maps" folder of Halo 2 for Windows Vista / Project Cartographer (with shared.map, mainmenu.map and single_player_shared.map)'
PLACEHOLDER_PACKAGE = 'no package folder chosen yet'
ESTIMATE_FACTOR_H2EK = 0.4          # the same rough figures h2conv.batch uses for the MCC to Vista estimate
ESTIMATE_FACTOR_STOCK = 15


class GuiError(Exception):
    """A problem with what the person asked for; the text is shown as it is."""


# ======================================================================================================
# small helpers
# ======================================================================================================
def one_line(text, limit=170):
    """Collapse white space and cut to `limit` characters (a reason for the summary list)."""
    s = re.sub(r'\s+', ' ', str(text or '')).strip()
    return s if len(s) <= limit else s[:limit - 3].rstrip() + '...'


def failure_text(ex):
    """'ModuleNotFoundError: No module named x (file.py line 12)': the exception and the innermost place it was raised (for the self test)."""
    where = ''
    try:
        frames = traceback.extract_tb(ex.__traceback__)
        if frames:
            where = ' (%s line %d)' % (os.path.basename(frames[-1].filename), frames[-1].lineno)
    except Exception:                                                             # noqa: BLE001
        pass
    return '%s: %s%s' % (type(ex).__name__, ex, where)


def human(n):
    return B.human(n)


def rel_path(path, root):
    try:
        r = os.path.relpath(path, root)
    except ValueError:
        return path
    return path if r.startswith('..') else r


def folder_name(title):
    """A safe Windows folder name for a package title ('Converted Vista Maps' when nothing usable is left)."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', str(title or ''))
    s = re.sub(r'\s+', ' ', s).strip(' .')[:60].strip(' .')
    if not s or s.lower() in EX.RESERVED:
        s = EX.DEFAULT_TITLE
    return s


def is_inside(child, parent):
    """True when `child` is `parent` or lies below it (case-insensitive, separators normalised)."""
    try:
        c = os.path.normcase(os.path.abspath(child))
        p = os.path.normcase(os.path.abspath(parent))
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False                                  # different drives


def count_maps(folder):
    try:
        return sum(1 for n in os.listdir(folder) if n.lower().endswith('.map'))
    except OSError:
        return 0


def free_bytes(folder):
    """Free space of the drive that holds (or would hold) `folder`; None when unknown."""
    import shutil
    probe = os.path.abspath(folder or '.')
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def open_folder(path):
    """Show a folder in Windows Explorer."""
    os.startfile(path)                                # noqa: S606 - Windows only, a folder the program wrote


def _real_toolkit():
    import tkinter
    from tkinter import filedialog, messagebox, ttk
    return collections.namedtuple('Toolkit', 'tk ttk filedialog messagebox')(tkinter, ttk, filedialog, messagebox)


# ======================================================================================================
# the folders
# ======================================================================================================
class FolderChoice(object):
    """One folder row of the window: what it is, where the value came from, and whether it is usable."""

    def __init__(self, kind):
        self.kind = kind
        self.label = FOLDER_LABELS[kind]
        self.path = None
        self.source = 'none'                  # option | env | settings | auto | bundled | chosen | default | none
        self.ok = False
        self.message = ''

    def describe(self):
        if not self.path:
            return 'not set'
        return '%s   [%s]' % (self.path, SOURCE_WORDS.get(self.source, self.source))

    def to_dict(self):
        return OD([('kind', self.kind), ('label', self.label), ('path', self.path), ('source', self.source), ('ok', self.ok), ('message', self.message)])


def resolve_package_dir(chosen, title):
    """The Excession mod package folder that "choose this folder" means -> (package folder, note, error).
         a folder that holds ModInfo.json      -> that folder is the package (its maps are kept, new ones are added)
         an empty folder, or one that does not exist yet -> that folder becomes the package
         any other folder (for example the folder Excession scans for mods) -> a sub-folder named after the package title is the package"""
    chosen = os.path.abspath(chosen)
    if os.path.isfile(chosen):
        return None, '', 'That is a file, not a folder.'
    if os.path.isfile(os.path.join(chosen, 'ModInfo.json')):
        return chosen, _package_note(chosen), ''
    if not os.path.exists(chosen):
        if not os.path.isdir(os.path.dirname(chosen)):
            return None, '', 'The folder %s cannot be made because %s does not exist.' % (chosen, os.path.dirname(chosen))
        return chosen, 'a new package folder will be made here', ''
    try:
        empty = not os.listdir(chosen)
    except OSError as ex:
        return None, '', 'Cannot read %s: %s' % (chosen, ex)
    if empty:
        return chosen, 'this empty folder becomes the package', ''
    sub = os.path.join(chosen, folder_name(title))
    if os.path.isfile(os.path.join(sub, 'ModInfo.json')):
        return sub, _package_note(sub), ''
    if os.path.exists(sub):
        try:
            sub_empty = os.path.isdir(sub) and not os.listdir(sub)
        except OSError:
            sub_empty = False
        if not sub_empty:
            return None, '', ('%s already exists and is not an Excession mod package. Choose an empty folder, an existing mod package, or the folder '
                              'that is meant to hold mod packages.' % sub)
    return sub, 'a new package folder "%s" will be made inside the chosen folder' % os.path.basename(sub), ''


def _package_note(folder):
    try:
        n = len(EX.ModPackage(folder, create=False).entries)
    except Exception:                                  # noqa: BLE001 - a damaged package is reported by the batch itself
        return 'an existing Excession mod package'
    return 'an existing Excession mod package with %d maps (maps already converted are kept)' % n


# ======================================================================================================
# the scan
# ======================================================================================================
class ScanRow(object):
    def __init__(self, path, name, what, action, detail, direction=None, code=''):
        self.path, self.name, self.what, self.action, self.detail, self.direction, self.code = path, name, what, action, detail, direction, code

    def values(self):
        return (self.name, self.what, self.action, self.detail)


def _what(rec):
    if rec.fmt == 'invalid':
        return 'not a usable Halo 2 map'
    kind = {'solo': 'campaign', 'multiplayer': 'multiplayer'}.get(rec.kind, rec.kind or '?')
    return '%s %s' % ('Vista' if rec.fmt == 'vista' else 'MCC', kind)


class ScanResult(object):
    """What the scan found and what a batch would do with it (a dry-run plan; names of Vista maps may still change when the real run hashes the files)."""

    def __init__(self, root, records, plan, error='', need_mcc_out=False, need_vista_out=False, key=None):
        self.root, self.records, self.plan, self.error = root, records, plan, error
        self.need_mcc_out, self.need_vista_out, self.key = need_mcc_out, need_vista_out, key
        self.items = list(plan.items) if plan is not None else []
        self.rows = [self._row(i) for i in self.items] if plan is not None else [self._rec_row(r) for r in records]
        ok = [r for r in records if r.status == 'ok']
        self.n_files = len(records)
        self.vista = [r for r in ok if r.fmt == 'vista']
        self.mcc = [r for r in ok if r.fmt == 'mcc']
        self.to_vista2mcc = [i for i in self.items if i.action == 'convert' and i.direction == 'vista2mcc']
        self.to_mcc2vista = [i for i in self.items if i.action == 'convert' and i.direction == 'mcc2vista']
        self.skipped = collections.Counter(i.code for i in self.items if i.action == 'skip')
        self.invalid = [i for i in self.items if i.action == 'invalid']
        self.custom_shared = [r for r in records if r.status == 'skip' and r.code == 'custom_shared']

    @property
    def n_convert(self):
        return len(self.to_vista2mcc) + len(self.to_mcc2vista)

    def _rec_row(self, r):
        return ScanRow(r.path, os.path.basename(r.path), _what(r), 'found' if r.status == 'ok' else 'ignored', one_line(r.reason), code=r.code)

    def _row(self, it):
        r = it.rec
        name = rel_path(r.path, self.root)
        if it.action == 'convert':
            act = 'Vista -> MCC' if it.direction == 'vista2mcc' else 'MCC -> Vista'
            return ScanRow(r.path, name, _what(r), act, ('-> %s.map  %s' % (it.stem, B.plan_label(it))).strip(), it.direction, it.code)
        if it.action == 'skip':
            return ScanRow(r.path, name, _what(r), 'skipped', SKIP_WORDS.get(it.code) or one_line(it.reason), code=it.code)
        return ScanRow(r.path, name, _what(r), 'ignored', one_line(it.reason), code=it.code)

    def headline(self):
        if not self.n_files:
            return 'No .map files were found in this folder or the folders below it.'
        mp_v = sum(1 for r in self.vista if r.kind == 'multiplayer')
        mp_m = sum(1 for r in self.mcc if r.kind == 'multiplayer')
        bits = ['%d .map files found' % self.n_files]
        bits.append('%d Vista maps (%d multiplayer, %d campaign)' % (len(self.vista), mp_v, len(self.vista) - mp_v))
        bits.append('%d MCC maps (%d multiplayer, %d campaign)' % (len(self.mcc), mp_m, len(self.mcc) - mp_m))
        text = '; '.join(bits) + '.'
        text += '  To convert now: %d (%d Vista to MCC, %d MCC to Vista).' % (self.n_convert, len(self.to_vista2mcc), len(self.to_mcc2vista))
        return text

    def skipped_text(self):
        """One line about what is left out, '' when nothing is."""
        bits = []
        for code, n in sorted(self.skipped.items(), key=lambda kv: -kv[1]):
            if code in ('wrong_direction', 'not_selected', 'limit'):
                continue
            bits.append('%d %s' % (n, SKIP_WORDS.get(code, code)))
        if self.invalid:
            bits.append('%d not usable Halo 2 maps' % len(self.invalid))
        return ('Left out: ' + '; '.join(bits) + '.') if bits else ''

    def to_dict(self):
        return OD([('root', self.root), ('files', self.n_files), ('vista', len(self.vista)), ('mcc', len(self.mcc)), ('to_vista2mcc', len(self.to_vista2mcc)),
                   ('to_mcc2vista', len(self.to_mcc2vista)), ('skipped', dict(self.skipped)), ('invalid', len(self.invalid)),
                   ('custom_shared', len(self.custom_shared)), ('error', self.error)])


# ======================================================================================================
# the running batch (the worker process)
# ======================================================================================================
PROGRESS_RE = re.compile(r'^\[\s*(\d+)/(\d+)\]\s+(ok|FAIL|\?\?)\s+(\S.*?)\s+(\d+(?:\.\d+)?)s\s*(.*)$')
START_RE = re.compile(r'^converting (.+) \.\.\.$')
SCANNED_RE = re.compile(r'^scanned (.+): (\d+) \.map files, (\d+) to convert$')


def parse_line(line):
    """One line of the worker's output -> a dict with 'kind' (progress | start | scanned | text) and the parts that are known."""
    m = PROGRESS_RE.match(line)
    if m:
        return {'kind': 'progress', 'done': int(m.group(1)), 'total': int(m.group(2)), 'ok': m.group(3) == 'ok', 'failed': m.group(3) == 'FAIL',
                'name': m.group(4).strip(), 'seconds': float(m.group(5)), 'message': m.group(6).strip()}
    m = START_RE.match(line)
    if m:
        return {'kind': 'start', 'path': m.group(1), 'name': os.path.splitext(os.path.basename(m.group(1)))[0]}
    m = SCANNED_RE.match(line)
    if m:
        return {'kind': 'scanned', 'root': m.group(1), 'files': int(m.group(2)), 'total': int(m.group(3))}
    return {'kind': 'text', 'text': line}


class _Job(object):
    """A Windows job object that holds the worker and, through it, every converter process the worker starts: ending the job ends them all, and
    closing the window ends them too (kill-on-close), so no converter is left running in the background."""

    def __init__(self, handle, k32):
        self.handle, self.k32 = handle, k32

    @classmethod
    def for_process(cls, proc):
        if os.name != 'nt' or proc is None or not hasattr(proc, '_handle'):
            return None
        try:
            from ctypes import wintypes

            class BASIC(ctypes.Structure):
                _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', wintypes.DWORD),
                            ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                            ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

            class IOC(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                                                           'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

            class EXTENDED(ctypes.Structure):
                _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IOC), ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                            ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.TerminateJobObject.argtypes = [wintypes.HANDLE, ctypes.c_uint]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return None
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000                    # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):      # JobObjectExtendedLimitInformation
                k32.CloseHandle(job)
                return None
            if not k32.AssignProcessToJobObject(job, int(proc._handle)):
                k32.CloseHandle(job)
                return None
            return cls(job, k32)
        except Exception:                                                     # noqa: BLE001 - without a job the worker alone is ended
            return None

    def terminate(self):
        if self.handle:
            self.k32.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self.k32.CloseHandle(self.handle)               # kill-on-close: whatever is still in the job ends here
            self.handle = None


class BatchRun(object):
    """The worker process: starts it without a console window, reads its output line by line on a thread and hands every line to `emit`.
    emit(name, payload) is called from the reader thread: ('line', text) for each line and, at the end, ('exit', return code)."""

    def __init__(self, cmd, cwd=None, env=None, emit=None, report_path=None, cancel_file=None, popen=None):
        self.cmd, self.cwd, self.env = list(cmd), cwd, env
        self.emit = emit or (lambda name, payload: None)
        self.report_path, self.cancel_file = report_path, cancel_file
        self._popen = popen or subprocess.Popen
        self.proc = None
        self.job = None
        self.thread = None
        self.lines = []
        self.returncode = None
        self.cancel_requested = False
        self.killed = False
        self.started = None

    def start(self):
        flags = (CREATE_NO_WINDOW | BELOW_NORMAL) if os.name == 'nt' else 0
        kw = {'creationflags': flags} if flags else {}
        self.proc = self._popen(self.cmd, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
        self.job = _Job.for_process(self.proc)
        self.started = time.time()
        self.thread = threading.Thread(target=self._read, name='h2conv-batch-reader', daemon=True)
        self.thread.start()
        return self

    def _read(self):
        try:
            for raw in iter(self.proc.stdout.readline, b''):
                line = raw.decode('utf-8', 'replace').rstrip('\r\n')
                self.lines.append(line)
                self.emit('line', line)
        except Exception as ex:                                               # noqa: BLE001
            self.emit('line', 'could not read the worker output: %s' % ex)
        rc = self.proc.wait()
        self.returncode = rc
        if self.job is not None:
            self.job.close()
            self.job = None
        self.emit('exit', rc)

    @property
    def running(self):
        return self.proc is not None and self.returncode is None

    def request_cancel(self):
        """Ask for a clean stop: no further map is started; the map being converted is finished and kept."""
        self.cancel_requested = True
        if self.cancel_file:
            try:
                with open(self.cancel_file, 'w') as fh:
                    fh.write('stop\n')
            except OSError:
                return False
        return True

    def kill(self):
        """Stop at once: the worker and every converter it started end now (a map being converted is lost; maps already converted are kept)."""
        self.killed = True
        if self.job is not None:
            self.job.terminate()
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.kill()
        except OSError:
            pass


# ======================================================================================================
# the summary
# ======================================================================================================
OUTPUT_LABELS = {'mcc': 'Excession mod package (MCC maps)', 'vista': 'Halo 2 maps folder (Vista maps)'}


def _map_name(src):
    return os.path.splitext(os.path.basename(src or ''))[0] or str(src)


class Summary(object):
    """What happened, in the words of the end screen: converted / skipped / FAILED maps and where the output is."""

    def __init__(self):
        self.has_report = False
        self.counts = OD()
        self.converted = []                  # {'name','src','out','size','seconds','direction'}
        self.failed = []                     # {'name','src','reason'}
        self.attention = []                  # skipped or unusable files worth a look: {'name','src','code','reason'}
        self.skipped = collections.Counter()
        self.output_dirs = []                # [(label, folder)]
        self.package = None
        self.validation = None
        self.elapsed = None
        self.interrupted = False
        self.exit_code = None
        self.note = ''
        self.report_path = None

    @classmethod
    def from_report(cls, rep, report_path=None, exit_code=None):
        s = cls()
        s.has_report = True
        s.report_path, s.exit_code = report_path, exit_code
        s.counts = OD(rep.get('counts') or {})
        s.elapsed = rep.get('elapsed_s')
        s.interrupted = bool(rep.get('interrupted'))
        s.package, s.validation = rep.get('package'), rep.get('package_validation')
        for d in rep.get('items') or []:
            st = d.get('status')
            if st == 'converted':
                s.converted.append({'name': d.get('stem') or _map_name(d.get('src')), 'src': d.get('src'), 'out': d.get('out'), 'size': d.get('out_size'),
                                    'seconds': d.get('seconds'), 'direction': d.get('direction')})
            elif st == 'failed':
                s.failed.append({'name': _map_name(d.get('src')), 'src': d.get('src'), 'reason': one_line(d.get('reason') or 'unknown reason')})
            elif st == 'skipped':
                s.skipped[d.get('code') or ''] += 1
        for d in rep.get('needs_attention') or []:
            s.attention.append({'name': _map_name(d.get('src')), 'src': d.get('src'), 'code': d.get('code'),
                                'reason': SKIP_WORDS.get(d.get('code')) or one_line(d.get('reason'))})
        for k, v in (rep.get('output_dirs') or {}).items():
            if v:
                s.output_dirs.append((OUTPUT_LABELS.get(k, k), v))
        return s

    @classmethod
    def from_log(cls, lines, exit_code):
        """No report was written (the worker ended early or was stopped hard): what the output lines say."""
        s = cls()
        s.exit_code = exit_code
        for ln in lines:
            p = parse_line(ln)
            if p['kind'] == 'progress':
                if p['ok']:
                    s.converted.append({'name': p['name'], 'src': None, 'out': p['message'].replace('->', '').strip() or None, 'size': None,
                                        'seconds': p['seconds'], 'direction': None})
                elif p['failed']:
                    s.failed.append({'name': p['name'], 'src': None, 'reason': one_line(p['message'] or 'unknown reason')})
            m = re.match(r'^output directory \((MCC maps|Vista maps)\): (.+)$', ln)
            if m:
                s.output_dirs.append(('Excession mod package (MCC maps)' if m.group(1) == 'MCC maps' else 'Halo 2 maps folder (Vista maps)', m.group(2)))
        s.counts = OD([('converted', len(s.converted)), ('failed', len(s.failed))])
        s.note = ('The batch did not finish normally (exit code %s) and wrote no report, so this list is built from its output lines.' % exit_code)
        return s

    @property
    def n_failed(self):
        return len(self.failed)

    def headline(self):
        if not self.has_report:
            return '%d converted, %d FAILED (the batch ended early)' % (len(self.converted), len(self.failed))
        c = self.counts
        skipped = c.get('skipped', 0) + c.get('invalid', 0)
        head = '%d converted, %d skipped' % (c.get('converted', len(self.converted)), skipped)
        head += ', %d FAILED' % c.get('failed', len(self.failed)) if self.failed else ', none failed'
        if self.interrupted:
            head += ' - stopped before the end'
        return head

    def text(self):
        """The whole summary as plain text (the window shows it as a list; this is what "Copy summary" puts on the clipboard)."""
        out = [self.headline()]
        if self.note:
            out.append(self.note)
        if self.failed:
            out += ['', 'FAILED maps (%d):' % len(self.failed)]
            out += ['  %s - %s' % (f['name'], f['reason']) for f in self.failed]
        if self.attention:
            out += ['', 'Skipped or unusable files that need a look (%d):' % len(self.attention)]
            out += ['  %s - %s' % (a['name'], a['reason']) for a in self.attention]
        if self.skipped:
            out += ['', 'Skipped: ' + ', '.join('%d %s' % (n, SKIP_WORDS.get(c, c or 'other')) for c, n in sorted(self.skipped.items(), key=lambda kv: -kv[1]))]
        for label, folder in self.output_dirs:
            out += ['', '%s: %s' % (label, folder)]
            for cv in self.converted:
                o = cv.get('out') or ''
                if o and is_inside(o, folder):
                    out.append('  %s  %s' % (os.path.basename(o), human(cv['size']) if cv.get('size') else ''))
        if self.validation:
            out += ['', 'Package check: %d error(s), %d warning(s)' % (self.validation.get('errors', 0), self.validation.get('warnings', 0))]
        if self.report_path:
            out += ['', 'Full report (file names and folders on your computer): %s' % self.report_path]
        return '\n'.join(out)


# ======================================================================================================
# the controller
# ======================================================================================================
class Controller(object):
    """Everything the window knows, without any window.  Folder rows, options, the scan, the problems, the worker command line and the
    summary.  `settings_path` None = the program's usual settings.json (shared with the command line)."""

    def __init__(self, settings_path=None, opener=None):
        self.settings_path = settings_path
        self.opener = opener or open_folder
        self.settings = B.Settings(settings_path)
        self.root = ''
        self.direction = 'auto'
        self.multiplayer = True
        self.campaign = True
        self.overwrite = False
        self.force = False
        self.jobs = 1
        self.package_title = EX.DEFAULT_TITLE
        self.folders = OD((k, FolderChoice(k)) for k in FOLDER_ORDER)
        self.scan_result = None
        self.run = None
        self.summary = None
        self.state = 'idle'                  # loading | idle | scanning | running | cancelling
        self.popen = None                    # tests: stands in for subprocess.Popen when the worker is started
        self.report_path = os.path.join(os.path.dirname(os.path.abspath(settings_path)) if settings_path else P.settings_dir(), 'last_batch_report.json')

    # -- settings / folders -----------------------------------------------------------------------
    @contextlib.contextmanager
    def _scope(self):
        if self.settings_path:
            with P.scoped(settings_file=self.settings_path):
                yield
        else:
            yield

    def load(self, root=None):
        """Read the settings and find the games.  The search of the drives for the Vista folder can take a few seconds when nothing is saved.
        `root`: the folder with the maps to start with (default: the one used last time)."""
        self.settings = B.Settings(self.settings_path)
        s = self.settings
        self.root = root or s.get('last_scan_root') or ''
        self.direction = s.get('direction') if s.get('direction') in DIRECTION_LABELS else 'auto'
        self.package_title = s.get('package_title') or EX.DEFAULT_TITLE
        for kind in FOLDER_ORDER:
            self.detect(kind)
        return self

    def detect(self, kind):
        """Find the folder of `kind` the way the command line does: setting / environment variable, then automatic search."""
        fc = self.folders[kind]
        fc.path, fc.source, fc.ok, fc.message = None, 'none', False, ''
        with self._scope():
            v, src = P.pick(kind)
            if v is None:
                if kind == 'mcc_maps':
                    v, src = P.mcc_maps_dir(required=False), 'auto'
                elif kind == 'vista_maps':
                    v, src = P.vista_maps_dir(required=False), 'auto'
                elif kind == 'assembly':
                    v, src = P.bundled_assembly_dir(), 'bundled'
                elif kind == 'vista_out':
                    v, src = P.vista_out_dir(required=False), 'default'
        if v is None:
            fc.ok = kind in ('h2ek_maps',)                       # the extra donors are optional
            fc.message = {'mcc_maps': 'Halo: The Master Chief Collection was not found. Use "Change" and pick %s.' % MCC_HINT,
                          'vista_maps': 'The Vista maps folder was not found. Use "Change" and pick %s.' % VISTA_HINT,
                          'h2ek_maps': 'not used',
                          'mcc_out': 'Choose where the Excession mod package goes (needed to convert Vista maps to MCC).',
                          'vista_out': 'Choose your Halo 2 Maps folder (needed to convert MCC maps to Vista).'}.get(kind, '')
            return fc
        fc.path, fc.source = os.path.abspath(v), src
        if kind == 'mcc_out':
            pkg, note, err = resolve_package_dir(fc.path, self.package_title)
            fc.ok, fc.message = (bool(pkg), note or err)
            if pkg:
                fc.path = pkg
        else:
            fc.ok, fc.message, fc.path = self._check(kind, fc.path)
        return fc

    def _check(self, kind, path):
        """-> (ok, message, normalised path) for a folder of `kind`."""
        if kind == 'mcc_maps':
            if not os.path.isdir(path):
                return False, 'This folder does not exist.', path
            if not P.looks_like_mcc_maps(path):
                return False, 'No .map files and no textures.dat in this folder. It should be %s.' % MCC_HINT, path
            extra = '' if os.path.isfile(os.path.join(path, 'textures.dat')) else '; textures.dat is not in this folder (the installed game is used for it if it is found)'
            return True, '%d .map files%s' % (count_maps(path), extra), path
        if kind == 'vista_maps':
            if not os.path.isdir(path):
                return False, 'This folder does not exist.', path
            missing = [n for n in P.VISTA_KEY_FILES if not os.path.isfile(os.path.join(path, n))]
            if missing:
                return False, 'Missing %s. It should be %s.' % (', '.join(missing), VISTA_HINT), path
            if not P.looks_like_vista_maps(path):
                return False, 'shared.map in this folder is not a Halo 2 Vista (cache version 8) map.', path
            return True, '%d .map files' % count_maps(path), path
        if kind == 'assembly':
            try:
                lay = P.find_assembly(path, 'option')
            except P.PathsError as ex:
                return False, one_line(ex, 400), path
            c = lay.counts()
            return True, '%d Halo 2 + %d Halo 2 MCC layout files' % (c['Halo2'], c['Halo2MCC']), path
        if kind == 'h2ek_maps':
            if not os.path.isdir(path):
                return False, 'This folder does not exist.', path
            sub = os.path.join(path, P.H2EK_MAPS_NAME)
            if os.path.isdir(sub) and not count_maps(path):
                path = sub
            return True, '%d .map files' % count_maps(path), path
        if kind == 'vista_out':
            if os.path.isfile(path):
                return False, 'That is a file, not a folder.', path
            if not os.path.isdir(path) and not os.path.isdir(os.path.dirname(path)):
                return False, 'The folder cannot be made because %s does not exist.' % os.path.dirname(path), path
            return True, ('%d maps already in it' % count_maps(path)) if os.path.isdir(path) else 'this folder will be made', path
        return False, 'unknown folder', path

    def check_folder(self, kind, path):
        """A person's choice for `kind` -> (ok, message, value to keep)."""
        path = os.path.abspath(path)
        if kind == 'mcc_out':
            pkg, note, err = resolve_package_dir(path, self.package_title)
            if not pkg:
                return False, err, path
            bad = self._output_clash(pkg, 'The mod package')
            return (False, bad, pkg) if bad else (True, note, pkg)
        ok, msg, final = self._check(kind, path)
        if ok and kind == 'vista_out':
            bad = self._output_clash(final, 'The output folder')
            if bad:
                return False, bad, final
        return ok, msg, final

    def _output_clash(self, out, what):
        """A message when an output folder would sit inside a folder the program only reads."""
        for kind in ('mcc_maps', 'vista_maps', 'h2ek_maps'):
            fc = self.folders[kind]
            if fc.path and is_inside(out, fc.path):
                return '%s must not be inside %s (%s). It is only ever read, never written.' % (what, fc.label.lower(), fc.path)
        return ''

    def set_folder(self, kind, text):
        """A folder was chosen or typed -> (ok, message).  A good choice is remembered in the settings file; an empty text forgets the choice
        (the automatic value is used again).  A bad choice leaves the old value in place."""
        text = (text or '').strip().strip('"').strip()
        fc = self.folders[kind]
        if not text:
            self._remember(kind, None)
            self.detect(kind)
            return fc.ok, fc.message
        ok, msg, final = self.check_folder(kind, text)
        if not ok:
            return False, msg
        fc.path, fc.source, fc.ok, fc.message = final, 'chosen', True, msg
        saved = self._remember(kind, final)
        return True, msg if saved else msg + ' (the settings file could not be saved)'

    def _remember(self, kind, value):
        self.settings.set(P.SETTING[kind], value)
        return self.settings.save()

    def set_root(self, text):
        self.root = (text or '').strip().strip('"').strip()

    def root_path(self):
        return os.path.abspath(self.root) if self.root else ''

    def set_direction(self, d):
        if d not in DIRECTION_LABELS:
            raise GuiError('unknown direction %r' % (d,))
        self.direction = d

    def set_kinds(self, multiplayer, campaign):
        self.multiplayer, self.campaign = bool(multiplayer), bool(campaign)

    def kinds(self):
        return tuple(k for k, on in (('multiplayer', self.multiplayer), ('solo', self.campaign)) if on)

    def kind_option(self):
        k = self.kinds()
        return 'all' if len(k) == 2 else (k[0] if k else None)

    def set_jobs(self, n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise GuiError('"Maps at the same time" must be a number from 1 to 4.')
        if not 1 <= n <= 4:
            raise GuiError('"Maps at the same time" must be a number from 1 to 4.')
        self.jobs = n

    def save_choices(self):
        """Remember the root folder, the direction and the package title (the same keys `batch run` remembers)."""
        s = self.settings
        s.set('last_scan_root', self.root_path())
        s.set('direction', self.direction)
        s.set('package_title', self.package_title)
        for kind, key in (('mcc_out', 'mcc_output_dir'), ('vista_out', 'vista_output_dir')):
            fc = self.folders[kind]
            if fc.path and fc.ok and fc.source in ('chosen', 'settings', 'default', 'option', 'env'):
                s.set(key, fc.path)
        return s.save()

    # -- the scan -----------------------------------------------------------------------------------
    def scan_key(self):
        f = self.folders
        return (self.root_path(), self.direction, self.kinds(), self.overwrite, self.force, f['mcc_out'].path, f['vista_out'].path, f['mcc_maps'].path)

    def scan_is_current(self):
        return self.scan_result is not None and self.scan_result.key == self.scan_key()

    def check_root(self):
        root = self.root_path()
        if not root:
            raise GuiError('Choose the folder that holds your maps first.')
        if not os.path.isdir(root):
            raise GuiError('The folder %s does not exist.' % root)
        if not self.kinds():
            raise GuiError('Tick multiplayer maps, campaign maps, or both.')
        return root

    def scan(self, progress=None):
        """Look at every .map below the root folder (blocking: the window runs it on a thread) -> ScanResult.  Reads headers only; writes nothing."""
        root = self.check_root()
        f = self.folders
        mcc_out, vista_out = (f['mcc_out'].path if f['mcc_out'].ok else None), (f['vista_out'].path if f['vista_out'].ok else None)
        key = self.scan_key()
        mcc_dir = f['mcc_maps'].path if f['mcc_maps'].ok else None
        recs = MI.scan_folder(root, recursive=True, exclude=[d for d in (mcc_out, vista_out) if d], deep=True, mcc_dir=mcc_dir, progress=progress)
        # a plan with stand-in output folders when none is chosen yet: a dry run only reads (it makes no folder and writes no file)
        opts = B.Options(direction=self.direction, mcc_out=mcc_out or os.path.join(P.settings_dir(), PLACEHOLDER_PACKAGE),
                         vista_out=vista_out or P.documents_maps_dir(), vista_maps=f['vista_maps'].path, mcc_maps=mcc_dir, dry_run=True,
                         hash_sources=False, kinds=self.kinds(), overwrite=self.overwrite, force=self.force, remember=False, quiet=True,
                         settings_path=False, package_title=self.package_title)
        plan, err = None, ''
        try:
            plan = B.make_plan(root, opts, records=recs)
        except (RuntimeError, EX.PackageError) as ex:
            err = str(ex)
        res = ScanResult(root, recs, plan, err, need_mcc_out=mcc_out is None, need_vista_out=vista_out is None, key=key)
        self.scan_result = res
        return res

    def estimate(self, res=None):
        """[(label, maps, estimated bytes, free bytes, folder)] for the planned conversions (rough, like the batch's own estimate)."""
        res = res or self.scan_result
        out = []
        if res is None:
            return out
        if res.to_vista2mcc:
            d = self.folders['mcc_out'].path
            out.append(('Vista to MCC', len(res.to_vista2mcc), sum(B.estimate_mcc_bytes(i.rec) for i in res.to_vista2mcc), free_bytes(d), d))
        if res.to_mcc2vista:
            d = self.folders['vista_out'].path
            est = int(sum(i.rec.size * (ESTIMATE_FACTOR_H2EK if i.rec.style == 'h2ek' else ESTIMATE_FACTOR_STOCK) for i in res.to_mcc2vista))
            out.append(('MCC to Vista', len(res.to_mcc2vista), est, free_bytes(d), d))
        return out

    # -- checks before Convert --------------------------------------------------------------------
    def problems(self):
        """-> (errors, warnings) for pressing Convert now.  Errors stop the run; warnings are shown first and can be accepted."""
        errs, warns = [], []
        f = self.folders
        res = self.scan_result
        try:
            self.check_root()
        except GuiError as ex:
            errs.append(str(ex))
        if not 1 <= self.jobs <= 4:
            errs.append('"Maps at the same time" must be a number from 1 to 4.')
        if res is None or not self.scan_is_current():
            errs.append('Scan the folder first (or use Convert, which scans first).')
            return errs, warns
        if res.error:
            errs.append(res.error)
        for kind in ('mcc_maps', 'vista_maps', 'assembly'):
            if not f[kind].ok:
                errs.append('%s: %s' % (f[kind].label, f[kind].message or 'not set'))
        if f['h2ek_maps'].path and not f['h2ek_maps'].ok:
            errs.append('%s: %s' % (f['h2ek_maps'].label, f['h2ek_maps'].message))
        if res.n_files and not res.n_convert:
            errs.append('There is nothing to convert in this folder with the current choices. %s' % (res.skipped_text() or ''))
        if not res.n_files:
            errs.append('No .map files were found in this folder or the folders below it.')
        if res.to_vista2mcc and not (f['mcc_out'].path and f['mcc_out'].ok):
            errs.append('Choose the Excession mod package folder: %s' % (f['mcc_out'].message or 'not set'))
        if res.to_mcc2vista and not (f['vista_out'].path and f['vista_out'].ok):
            errs.append('Choose your Halo 2 Maps folder: %s' % (f['vista_out'].message or 'not set'))
        if res.custom_shared:
            warns.append('%d MCC map(s) need a custom shared.map. That is not supported: they are listed and skipped.' % len(res.custom_shared))
        for label, n, est, free, folder in self.estimate(res):
            if free is not None and est > free:
                warns.append('%s: about %s of output is expected for %d map(s), but only %s are free where it will be written (%s).'
                             % (label, human(est), n, human(free), folder))
        return errs, warns

    # -- the worker -----------------------------------------------------------------------------------
    def worker_command(self):
        """The program that runs the batch: from the Windows build the command line exe beside this one, from a Python install `python -m h2conv`."""
        if getattr(sys, 'frozen', False):
            cli = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'h2conv.exe')
            return [cli] if os.path.isfile(cli) else [sys.executable]       # the window exe also understands the commands
        exe = sys.executable
        if os.path.basename(exe).lower() == 'pythonw.exe':
            alt = os.path.join(os.path.dirname(exe), 'python.exe')
            exe = alt if os.path.isfile(alt) else exe
        return [exe, '-m', 'h2conv']

    def batch_args(self, report_path=None, cancel_file=None):
        """The arguments of `h2conv batch run ...` for the current choices (a list of strings, without the program)."""
        f = self.folders
        root = self.check_root()
        a = ['batch', 'run', root, '--direction', self.direction, '--kind', self.kind_option(), '--isolate', '--jobs', str(self.jobs),
             '--no-remember', '--announce']
        for kind, opt in (('mcc_out', '--mcc-out'), ('vista_out', '--vista-out'), ('mcc_maps', '--mcc-maps'), ('vista_maps', '--vista-maps'),
                          ('h2ek_maps', '--h2ek-maps')):
            if f[kind].path and f[kind].ok:
                a += [opt, f[kind].path]
        if f['assembly'].path and f['assembly'].ok and f['assembly'].source != 'bundled':
            a += ['--assembly-dir', f['assembly'].path]
        if self.overwrite:
            a.append('--overwrite')
        if self.force:
            a.append('--force')
        if self.package_title and self.package_title != EX.DEFAULT_TITLE:
            a += ['--package-title', self.package_title]
        if self.settings_path:
            a += ['--settings', self.settings_path]
        if report_path:
            a += ['--report', report_path]
        if cancel_file:
            a += ['--cancel-file', cancel_file]
        return a

    def worker_env(self):
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        if not getattr(sys, 'frozen', False):
            env['PYTHONPATH'] = PACKAGE_ROOT + ((os.pathsep + env['PYTHONPATH']) if env.get('PYTHONPATH') else '')
        return env

    def make_run(self, emit, popen=None):
        """Remember the choices, prepare the worker (not started yet) and return it."""
        popen = popen or self.popen
        errs, _warns = self.problems()
        if errs:
            raise GuiError(errs[0])
        os.makedirs(os.path.dirname(self.report_path), exist_ok=True)
        for p in (self.report_path,):
            try:
                os.remove(p)                      # an old report must never be mistaken for this run's
            except OSError:
                pass
        cancel = os.path.join(tempfile.gettempdir(), 'h2conv_stop_%d_%d.flag' % (os.getpid(), int(time.time())))
        try:
            os.remove(cancel)
        except OSError:
            pass
        self.save_choices()
        cmd = self.worker_command() + self.batch_args(self.report_path, cancel)
        self.run = BatchRun(cmd, cwd=None if getattr(sys, 'frozen', False) else PACKAGE_ROOT, env=self.worker_env(), emit=emit,
                            report_path=self.report_path, cancel_file=cancel, popen=popen)
        self.summary = None
        return self.run

    def finish_run(self, rc):
        """The worker ended -> Summary (from its report when it wrote one, else from its output lines)."""
        run = self.run
        rep = None
        if run is not None and run.report_path:
            try:
                with open(run.report_path, 'r', encoding='utf-8') as fh:
                    rep = json.load(fh)
            except (OSError, ValueError):
                rep = None
        if isinstance(rep, dict) and rep.get('counts'):
            self.summary = Summary.from_report(rep, run.report_path, rc)
            if run.killed:
                self.summary.interrupted = True
        else:
            self.summary = Summary.from_log(run.lines if run is not None else [], rc)
            if run is not None and run.killed:
                self.summary.note = 'The batch was stopped at once; no report was written, so this list is built from its output lines.'
        if run is not None and run.cancel_file:
            try:
                os.remove(run.cancel_file)
            except OSError:
                pass
        self.state = 'idle'
        return self.summary

    def open_output(self, folder):
        if not folder or not os.path.isdir(folder):
            raise GuiError('The folder %s does not exist.' % folder)
        self.opener(folder)


# ======================================================================================================
# self test (no window)
# ======================================================================================================
def _opus_silence_packet():
    return bytes((0xF8, 0xFF, 0xFE))               # the 3-byte "silent frame" packet of Opus (CELT, full band, 20 ms, mono)


def selftest(report_path=None, out=None):
    """Import every module of the package, build the controller on a scratch settings file, check the bundled data files, the Opus library, the
    resampler and (when tkinter is there) the Tcl / Tk files, and run the worker with --version.  Creates no window.  -> exit code (0 = all fine)."""
    results = []
    out = out or sys.stdout
    tk_early = 'tkinter' in sys.modules

    def check(name, ok, detail=''):
        results.append({'check': name, 'ok': bool(ok), 'detail': str(detail)})
        try:
            if out is not None:
                out.write('%s  %s%s\n' % ('PASS' if ok else 'FAIL', name, ('  (%s)' % detail) if detail else ''))
                out.flush()
        except Exception:                                                         # noqa: BLE001
            pass
        return ok
    check('version', True, __version__ + (' (frozen)' if getattr(sys, 'frozen', False) else ''))
    # 1. every module imports
    import importlib
    import pkgutil
    import h2conv
    names, failed = [], []
    for mi in pkgutil.walk_packages(h2conv.__path__, 'h2conv.', onerror=lambda n: failed.append((n, 'walk'))):
        if mi.name.endswith('.__main__'):
            continue
        names.append(mi.name)
        try:
            importlib.import_module(mi.name)
        except Exception as ex:                                                   # noqa: BLE001
            failed.append((mi.name, failure_text(ex)))
    check('every module imports', not failed and len(names) >= 60, '%d modules%s' % (len(names), ('; failed: %s' % failed[:3]) if failed else ''))
    check('no module imports tkinter before a window is built', not tk_early and 'tkinter' not in sys.modules,
          'tkinter was loaded already' if (tk_early or 'tkinter' in sys.modules) else 'tkinter not loaded by the program modules')
    # 2. data files
    data = P.data_dir()
    need = ['enum_policy.json', 'block_alignment_vista.json', 'block_alignment_mcc.json', 'shader_param_maps.json']
    missing = [n for n in need if not os.path.isfile(os.path.join(data, n))]
    missing += [n for n in ('mcc_tag_groups.json', 'register_layout.json', 'rev_defaults.json', 'rev_null_words.json')
                if not os.path.isfile(os.path.join(P.package_dir(), 'rev', 'data', n))]
    check('data files', not missing, 'missing: %s' % missing if missing else 'all present')
    try:
        lay = P.find_assembly(P.bundled_assembly_dir(), 'bundled')
        c = lay.counts()
        check('bundled Assembly layout files', c['Halo2'] >= 98 and c['Halo2MCC'] >= 35, '%d + %d plugins' % (c['Halo2'], c['Halo2MCC']))
    except P.PathsError as ex:
        check('bundled Assembly layout files', False, ex)
    # 3. numpy / scipy / opus
    try:
        import numpy as np
        from scipy.signal import resample_poly
        y = resample_poly(np.arange(480, dtype=np.float64), 441, 480)
        check('numpy + scipy resampler', len(y) == 441, 'numpy %s' % np.__version__)
    except Exception as ex:                                                       # noqa: BLE001
        check('numpy + scipy resampler', False, failure_text(ex))
    try:
        from h2conv import sound_codec as sc
        dll = sc._load_opus()
        buf = (ctypes.c_int16 * 5760)()
        err = ctypes.c_int(0)
        dec = dll.opus_decoder_create(48000, 1, ctypes.byref(err))
        n = dll.opus_decode(dec, _opus_silence_packet(), 3, ctypes.cast(buf, ctypes.c_void_p), 5760, 0) if dec else -1
        if dec:
            dll.opus_decoder_destroy(dec)
        where = getattr(dll, '_name', '?')
        bundled = os.path.normcase(os.path.join(P.package_dir(), 'third_party', 'opus.dll'))
        check('opus.dll loads and decodes', n > 0, '%d samples; %s' % (n, 'the bundled copy' if os.path.normcase(os.path.abspath(where)) == bundled else where))
    except Exception as ex:                                                       # noqa: BLE001
        check('opus.dll loads and decodes', False, failure_text(ex))
    # 4. the controller on a scratch settings file
    import shutil
    tmp = tempfile.mkdtemp(prefix='h2conv_selftest_')
    try:
        sp = os.path.join(tmp, 'settings.json')
        c = Controller(settings_path=sp, opener=lambda p: None).load()
        c.set_direction('mcc2vista')
        c.set_root(tmp)
        c.save_choices()
        c2 = Controller(settings_path=sp).load()
        check('controller + settings round trip', c2.direction == 'mcc2vista' and c2.root_path() == os.path.abspath(tmp), os.path.basename(sp))
        res = c.scan()
        check('scan of an empty folder', res.n_files == 0 and 'No .map files' in res.headline(), res.headline())
        check('worker command line', c.batch_args(None, None)[:3] == ['batch', 'run', os.path.abspath(tmp)], ' '.join(c.worker_command()))
    except Exception as ex:                                                       # noqa: BLE001
        check('controller', False, '%s: %s\n%s' % (type(ex).__name__, ex, traceback.format_exc()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # 5. tkinter (no window is created: only the Tcl interpreter and the files Tk needs are looked at)
    try:
        import _tkinter
        import tkinter
        interp = tkinter.Tcl()
        lib = interp.eval('info library')
        tk_lib = os.environ.get('TK_LIBRARY') or os.path.join(os.path.dirname(lib), 'tk%s' % _tkinter.TK_VERSION)
        check('Tcl library', os.path.isfile(os.path.join(lib, 'init.tcl')), '%s, Tcl %s' % (lib, interp.eval('info patchlevel')))
        check('Tk library', os.path.isfile(os.path.join(tk_lib, 'tk.tcl')), 'Tk %s, %s' % (_tkinter.TK_VERSION, tk_lib))
        from tkinter import filedialog, messagebox, ttk       # noqa: F401 - only to prove that they import
    except Exception as ex:                                                       # noqa: BLE001
        check('tkinter (the window cannot open without it)', False, failure_text(ex))
    # 6. the worker program starts
    try:
        ctl = Controller(opener=lambda p: None)
        cmd = ctl.worker_command() + ['--version']
        p = subprocess.run(cmd, cwd=None if getattr(sys, 'frozen', False) else PACKAGE_ROOT, env=ctl.worker_env(), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, creationflags=CREATE_NO_WINDOW if os.name == 'nt' else 0)
        txt = p.stdout.decode('utf-8', 'replace').strip()
        check('worker program --version', p.returncode == 0 and __version__ in txt, '%s -> %s' % (os.path.basename(cmd[0]), one_line(txt, 80)))
    except Exception as ex:                                                       # noqa: BLE001
        check('worker program --version', False, '%s: %s' % (type(ex).__name__, ex))
    bad = [r for r in results if not r['ok']]
    if report_path:
        try:
            with open(report_path, 'w', encoding='utf-8') as fh:
                json.dump({'version': __version__, 'frozen': bool(getattr(sys, 'frozen', False)), 'ok': not bad, 'checks': results}, fh, indent=1)
        except OSError:
            pass
    check('SELFTEST', not bad, '%d of %d checks passed' % (len(results) - len(bad), len(results)))
    return 1 if bad else 0


# ======================================================================================================
# the window (thin view)
# ======================================================================================================
class App(object):
    """The tkinter window.  Everything it shows comes from the Controller; button presses go back to it.  The work (scan, batch) runs on
    threads / in the worker process and reaches the window through a queue that `_tick` empties on the window's own thread."""
    POLL_MS = 120

    def __init__(self, controller, toolkit=None, start_root=None):
        tk = toolkit or _real_toolkit()
        self.tk, self.ttk, self.filedialog, self.messagebox = tk.tk, tk.ttk, tk.filedialog, tk.messagebox
        self.c = controller
        self.events = queue.Queue()
        self.progress = {'done': 0, 'total': 0, 'current': ''}
        self.after_convert = False
        self.outputs = {}
        self.win = self.tk.Tk()
        self.scale = self._dpi_scale()
        self.win.title('%s %s' % (APP_TITLE, __version__))
        width, height = self.px(1040), self.px(800)
        try:                                            # never taller / wider than the screen (a 768 pixel laptop screen)
            width, height = min(width, self.win.winfo_screenwidth() - 40), min(height, self.win.winfo_screenheight() - 90)
        except Exception:                               # noqa: BLE001
            pass
        self.win.minsize(min(self.px(860), width), min(self.px(540), height))
        self.win.geometry('%dx%d' % (width, height))
        self.win.report_callback_exception = self._callback_error
        self.win.protocol('WM_DELETE_WINDOW', self.on_close)
        self.start_root = start_root
        self._build()
        self._fill_from_controller()
        self._set_state('loading')                      # until the settings are read and the games are found
        self.var_status.set('Looking for your games ...')
        self.win.after(self.POLL_MS, self._tick)
        self._spawn(self._load_folders)

    # -- building the window --------------------------------------------------------------------------
    def _dpi_scale(self):
        """Pixel sizes below are for 96 dpi; on a screen set to more, the fixed sizes (column widths, text wrapping) grow with it."""
        try:
            s = float(self.win.winfo_fpixels('1i')) / 96.0
        except Exception:                               # noqa: BLE001
            return 1.0
        return min(3.0, max(1.0, s))

    def px(self, n):
        return int(round(n * self.scale))

    def _build(self):
        tk, ttk = self.tk, self.ttk
        wrap = self.px(900)
        self.var_root = tk.StringVar(master=self.win)
        self.var_direction = tk.StringVar(master=self.win, value='auto')
        self.var_mp = tk.BooleanVar(master=self.win, value=True)
        self.var_camp = tk.BooleanVar(master=self.win, value=True)
        self.var_overwrite = tk.BooleanVar(master=self.win, value=False)
        self.var_force = tk.BooleanVar(master=self.win, value=False)
        self.var_jobs = tk.StringVar(master=self.win, value='1')
        self.var_status = tk.StringVar(master=self.win, value='Starting ...')
        self.var_headline = tk.StringVar(master=self.win, value='')
        self.var_out_mcc = tk.StringVar(master=self.win)
        self.var_out_vista = tk.StringVar(master=self.win)
        self.var_game = {k: tk.StringVar(master=self.win) for k in ('mcc_maps', 'vista_maps', 'assembly', 'h2ek_maps')}
        self.var_out_note = {k: tk.StringVar(master=self.win) for k in ('mcc_out', 'vista_out')}

        main = ttk.Frame(self.win, padding=10)
        main.grid(row=0, column=0, sticky='nsew')
        self.win.columnconfigure(0, weight=1)
        self.win.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(6, weight=1)

        ttk.Label(main, text='Pick the folder that holds your Halo 2 maps. The program looks in it and in every folder below it, recognises Halo 2 Vista '
                             'and Halo 2 MCC maps by their file header, and converts them all. Nothing is ever uploaded.', wraplength=wrap,
                  justify='left').grid(row=0, column=0, sticky='ew', pady=(0, 8))

        # 1. root folder
        f1 = ttk.LabelFrame(main, text='1. Folder with your maps', padding=8)
        f1.grid(row=1, column=0, sticky='ew', pady=3)
        f1.columnconfigure(0, weight=1)
        self.entry_root = ttk.Entry(f1, textvariable=self.var_root)
        self.entry_root.grid(row=0, column=0, sticky='ew', padx=(0, 6))
        self.btn_root = ttk.Button(f1, text='Choose ...', command=self.on_choose_root)
        self.btn_root.grid(row=0, column=1, padx=3)
        self.btn_scan = ttk.Button(f1, text='Find maps', command=self.on_scan)
        self.btn_scan.grid(row=0, column=2, padx=3)

        # 2. what to convert
        f2 = ttk.LabelFrame(main, text='2. What to convert', padding=8)
        f2.grid(row=2, column=0, sticky='ew', pady=3)
        self.radios = []
        for i, (key, label) in enumerate(DIRECTION_LABELS.items()):
            rb = ttk.Radiobutton(f2, text=label, value=key, variable=self.var_direction, command=self.on_options)
            rb.grid(row=0, column=i, sticky='w', padx=(0, 14))
            self.radios.append(rb)
        self.chk_mp = ttk.Checkbutton(f2, text='Multiplayer maps', variable=self.var_mp, command=self.on_options)
        self.chk_mp.grid(row=1, column=0, sticky='w', pady=(6, 0))
        self.chk_camp = ttk.Checkbutton(f2, text='Campaign maps', variable=self.var_camp, command=self.on_options)
        self.chk_camp.grid(row=1, column=1, sticky='w', pady=(6, 0))
        self.chk_overwrite = ttk.Checkbutton(f2, text='Replace Vista maps that already exist in the output folder', variable=self.var_overwrite, command=self.on_options)
        self.chk_overwrite.grid(row=2, column=0, columnspan=2, sticky='w')
        self.chk_force = ttk.Checkbutton(f2, text='Convert again maps that are already in the Excession mod package', variable=self.var_force, command=self.on_options)
        self.chk_force.grid(row=2, column=2, sticky='w')
        jobs = ttk.Frame(f2)
        jobs.grid(row=1, column=2, sticky='w', pady=(6, 0))
        ttk.Label(jobs, text='Maps at the same time (1 to 4; each needs about 1.5 GB of memory):').grid(row=0, column=0)
        self.spin_jobs = ttk.Spinbox(jobs, from_=1, to=4, width=3, textvariable=self.var_jobs, command=self.on_options)
        self.spin_jobs.grid(row=0, column=1, padx=4)

        # 3. where the maps go
        f3 = ttk.LabelFrame(main, text='3. Where the converted maps go', padding=8)
        f3.grid(row=3, column=0, sticky='ew', pady=3)
        f3.columnconfigure(1, weight=1)
        ttk.Label(f3, text='Vista to MCC: Excession folder (mod package):').grid(row=0, column=0, sticky='w')
        self.entry_mcc = ttk.Entry(f3, textvariable=self.var_out_mcc)
        self.entry_mcc.grid(row=0, column=1, sticky='ew', padx=6)
        self.btn_mcc = ttk.Button(f3, text='Choose ...', command=lambda: self.on_choose_folder('mcc_out'))
        self.btn_mcc.grid(row=0, column=2)
        ttk.Label(f3, textvariable=self.var_out_note['mcc_out'], foreground='#555555').grid(row=1, column=1, columnspan=2, sticky='w', padx=6)
        ttk.Label(f3, text='MCC to Vista: your Halo 2 Maps folder:').grid(row=2, column=0, sticky='w')
        self.entry_vista = ttk.Entry(f3, textvariable=self.var_out_vista)
        self.entry_vista.grid(row=2, column=1, sticky='ew', padx=6)
        self.btn_vista = ttk.Button(f3, text='Choose ...', command=lambda: self.on_choose_folder('vista_out'))
        self.btn_vista.grid(row=2, column=2)
        ttk.Label(f3, textvariable=self.var_out_note['vista_out'], foreground='#555555').grid(row=3, column=1, columnspan=2, sticky='w', padx=6)

        # 4. games
        f4 = ttk.LabelFrame(main, text='4. Games and layout files (found automatically)', padding=8)
        f4.grid(row=4, column=0, sticky='ew', pady=3)
        f4.columnconfigure(1, weight=1)
        self.game_buttons = []
        for r, kind in enumerate(('mcc_maps', 'vista_maps', 'assembly', 'h2ek_maps')):
            ttk.Label(f4, text=FOLDER_LABELS[kind] + ':').grid(row=r, column=0, sticky='w')
            ttk.Label(f4, textvariable=self.var_game[kind]).grid(row=r, column=1, sticky='w', padx=6)
            b = ttk.Button(f4, text='Change ...', command=(lambda k=kind: self.on_choose_folder(k)))
            b.grid(row=r, column=2, padx=3)
            self.game_buttons.append(b)
            b2 = ttk.Button(f4, text='Automatic', command=(lambda k=kind: self.on_reset_folder(k)))
            b2.grid(row=r, column=3, padx=3)
            self.game_buttons.append(b2)

        # actions
        f5 = ttk.Frame(main)
        f5.grid(row=5, column=0, sticky='ew', pady=6)
        f5.columnconfigure(3, weight=1)
        self.btn_convert = ttk.Button(f5, text='Convert', command=self.on_convert)
        self.btn_convert.grid(row=0, column=0, padx=3)
        self.btn_cancel = ttk.Button(f5, text='Cancel', command=self.on_cancel)
        self.btn_cancel.grid(row=0, column=1, padx=3)
        self.bar = ttk.Progressbar(f5, orient='horizontal', mode='determinate', maximum=100, length=260)
        self.bar.grid(row=0, column=2, padx=8)
        ttk.Label(f5, textvariable=self.var_status).grid(row=0, column=3, sticky='w')

        # tabs
        self.tabs = ttk.Notebook(main)
        self.tabs.grid(row=6, column=0, sticky='nsew')
        self.tab_found = ttk.Frame(self.tabs, padding=4)
        self.tab_log = ttk.Frame(self.tabs, padding=4)
        self.tab_sum = ttk.Frame(self.tabs, padding=4)
        self.tabs.add(self.tab_found, text='Maps found')
        self.tabs.add(self.tab_log, text='Progress')
        self.tabs.add(self.tab_sum, text='Summary')

        self.tab_found.columnconfigure(0, weight=1)
        self.tab_found.rowconfigure(1, weight=1)
        ttk.Label(self.tab_found, textvariable=self.var_headline, wraplength=wrap, justify='left').grid(row=0, column=0, columnspan=2, sticky='ew')
        self.tree_found = self._tree(self.tab_found, 1, ('map', 'what', 'action', 'detail'), ('Map', 'What it is', 'What happens', 'Output / reason'), (self.px(320), self.px(150), self.px(110), self.px(380)))

        self.tab_log.columnconfigure(0, weight=1)
        self.tab_log.rowconfigure(0, weight=1)
        self.text_log = self.tk.Text(self.tab_log, height=10, wrap='none', state='disabled')
        self.text_log.grid(row=0, column=0, sticky='nsew')
        sb = ttk.Scrollbar(self.tab_log, orient='vertical', command=self.text_log.yview)
        sb.grid(row=0, column=1, sticky='ns')
        self.text_log.configure(yscrollcommand=sb.set)
        self.text_log.tag_configure('fail', foreground='#b00020')

        self.tab_sum.columnconfigure(0, weight=1)
        self.tab_sum.rowconfigure(1, weight=1)
        self.var_sum_head = self.tk.StringVar(master=self.win, value='Nothing converted yet.')
        ttk.Label(self.tab_sum, textvariable=self.var_sum_head).grid(row=0, column=0, columnspan=2, sticky='w')
        self.tree_sum = self._tree(self.tab_sum, 1, ('result', 'map', 'detail'), ('Result', 'Map', 'Details'), (self.px(110), self.px(300), self.px(560)))
        self.tree_sum.tag_configure('failed', foreground='#b00020')
        self.frame_out = ttk.Frame(self.tab_sum)
        self.frame_out.grid(row=2, column=0, columnspan=2, sticky='ew', pady=4)
        self.btn_copy = ttk.Button(self.tab_sum, text='Copy summary', command=self.on_copy_summary)
        self.btn_copy.grid(row=3, column=0, sticky='w')

        ttk.Label(main, text='MCC maps that need a custom shared.map are not supported: they are listed and skipped. '
                             'A map that fails is skipped and the batch carries on; the Summary tab lists what failed.',
                  wraplength=wrap, justify='left', foreground='#555555').grid(row=7, column=0, sticky='ew', pady=(6, 0))

    def _tree(self, parent, row, cols, heads, widths):
        t = self.ttk.Treeview(parent, columns=cols, show='headings', height=8)
        for c, h, w in zip(cols, heads, widths):
            t.heading(c, text=h)
            t.column(c, width=w, anchor='w')
        t.grid(row=row, column=0, sticky='nsew')
        sb = self.ttk.Scrollbar(parent, orient='vertical', command=t.yview)
        sb.grid(row=row, column=1, sticky='ns')
        t.configure(yscrollcommand=sb.set)
        return t

    # -- helpers -----------------------------------------------------------------------------------------
    def _spawn(self, fn):
        """Run fn on a thread (the tests replace this with a direct call)."""
        threading.Thread(target=fn, daemon=True).start()

    def _callback_error(self, exc, val, tb):
        text = ''.join(traceback.format_exception(exc, val, tb))
        self.log_error(text)
        try:
            self.messagebox.showerror(APP_TITLE, 'Something went wrong inside the program:\n\n%s\n\nThe details were saved in %s' % (one_line(val, 300), error_log_path()))
        except Exception:                                                         # noqa: BLE001
            pass

    def log_error(self, text):
        try:
            os.makedirs(os.path.dirname(error_log_path()), exist_ok=True)
            with open(error_log_path(), 'a', encoding='utf-8') as fh:
                fh.write('--- %s\n%s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), text))
        except OSError:
            pass

    def _log(self, line, tag=None):
        t = self.text_log
        t.configure(state='normal')
        t.insert('end', line + '\n', (tag,) if tag else ())
        try:
            n = int(str(t.index('end-1c')).split('.')[0])
            if n > 6000:
                t.delete('1.0', '%d.0' % (n - 5000))
        except (ValueError, TypeError):
            pass
        t.see('end')
        t.configure(state='disabled')

    def _set_state(self, state):
        """loading | idle | scanning | running | cancelling: which controls can be used."""
        self.c.state = state
        idle = state == 'idle'
        for w in [self.entry_root, self.btn_root, self.btn_scan, self.btn_convert, self.chk_mp, self.chk_camp, self.chk_overwrite, self.chk_force, self.spin_jobs,
                  self.entry_mcc, self.btn_mcc, self.entry_vista, self.btn_vista] + self.radios + self.game_buttons:
            w.state(['!disabled'] if idle else ['disabled'])
        self.btn_cancel.state(['!disabled'] if state in ('running', 'cancelling') else ['disabled'])
        self.btn_cancel.configure(text='Stop now' if state == 'cancelling' else 'Cancel')
        if state == 'scanning':
            self.bar.configure(mode='indeterminate')
            self.bar.start(15)
        elif state in ('running', 'cancelling'):
            self.bar.stop()
            self.bar.configure(mode='determinate')
        else:
            self.bar.stop()
            self.bar.configure(mode='determinate')
            self.bar['value'] = 0

    def _pull(self):
        """Controller <- window: read every field."""
        c = self.c
        c.set_root(self.var_root.get())
        c.set_direction(self.var_direction.get())
        c.set_kinds(self.var_mp.get(), self.var_camp.get())
        c.overwrite, c.force = bool(self.var_overwrite.get()), bool(self.var_force.get())
        c.set_jobs(self.var_jobs.get())
        notes = []
        for kind, var in (('mcc_out', self.var_out_mcc), ('vista_out', self.var_out_vista)):
            text = var.get().strip()
            fc = c.folders[kind]
            if text and os.path.abspath(text.strip('"')) != (fc.path or '') :
                ok, msg = c.set_folder(kind, text)
                if not ok:
                    notes.append(msg)
                    self.var_out_note[kind].set(msg)
        return notes

    def _fill_from_controller(self):
        """Window <- controller."""
        c = self.c
        self.var_root.set(c.root)
        self.var_direction.set(c.direction)
        self.var_mp.set(c.multiplayer)
        self.var_camp.set(c.campaign)
        self.var_overwrite.set(c.overwrite)
        self.var_force.set(c.force)
        self.var_jobs.set(str(c.jobs))
        self._refresh_folders()
        self._set_state('idle')

    def _refresh_folders(self):
        f = self.c.folders
        for kind in ('mcc_maps', 'vista_maps', 'assembly', 'h2ek_maps'):
            fc = f[kind]
            text = fc.describe() if fc.path else ('not used' if kind == 'h2ek_maps' else 'NOT FOUND')
            if fc.message and kind != 'h2ek_maps':
                text += '   -   ' + one_line(fc.message, 150)
            self.var_game[kind].set(text)
        self.var_out_mcc.set(f['mcc_out'].path or '')
        self.var_out_vista.set(f['vista_out'].path or '')
        for kind in ('mcc_out', 'vista_out'):
            fc = f[kind]
            self.var_out_note[kind].set(('%s   [%s]' % (fc.message, SOURCE_WORDS.get(fc.source, fc.source))) if fc.path else fc.message)

    def _load_folders(self):
        try:
            self.c.load(root=self.start_root)
        except Exception as ex:                                                   # noqa: BLE001
            self.log_error(traceback.format_exc())
            self.events.put(('error', 'Could not read the settings: %s' % ex))
            self.events.put(('folders', None))
            return
        self.events.put(('folders', None))

    # -- button handlers -------------------------------------------------------------------------------
    def on_choose_root(self):
        d = self.filedialog.askdirectory(title='Choose the folder that holds your maps', initialdir=self.var_root.get() or None, mustexist=True)
        if d:
            self.var_root.set(os.path.normpath(d))
            self.c.set_root(self.var_root.get())

    def on_choose_folder(self, kind):
        fc = self.c.folders[kind]
        hint = {'mcc_maps': 'Choose ' + MCC_HINT, 'vista_maps': 'Choose ' + VISTA_HINT, 'assembly': 'Choose a folder with Assembly layout files',
                'h2ek_maps': 'Choose the MCC Halo 2 Editing Kit maps folder (h2_maps_win64_dx11)',
                'mcc_out': 'Choose the folder for the Excession mod package (or the folder that holds your mod packages)',
                'vista_out': 'Choose your Halo 2 Maps folder (Documents\\My Games\\Halo 2\\Maps)'}[kind]
        start = fc.path if (fc.path and os.path.isdir(fc.path)) else None
        d = self.filedialog.askdirectory(title=hint[:120], initialdir=start, mustexist=kind not in ('mcc_out', 'vista_out'))
        if not d:
            return
        ok, msg = self.c.set_folder(kind, os.path.normpath(d))
        if not ok:
            self.messagebox.showerror(APP_TITLE, msg)
        self._refresh_folders()

    def on_reset_folder(self, kind):
        self.c.set_folder(kind, '')
        self._refresh_folders()

    def on_options(self):
        try:
            self._pull()
        except GuiError as ex:
            self.var_status.set(str(ex))

    def on_scan(self, then=None):
        try:
            notes = self._pull()
            self.c.check_root()
        except GuiError as ex:
            self.messagebox.showerror(APP_TITLE, str(ex))
            return
        self.after_convert = bool(then)
        self._set_state('scanning')
        self.var_status.set('Looking for maps ...')
        self.var_headline.set('')
        self.tabs.select(self.tab_found)
        self._spawn(self._scan_thread)

    def _scan_thread(self):
        count = [0]

        def progress(n, path):
            count[0] = n
            if n % 10 == 0:
                self.events.put(('scan_progress', (n, path)))
        try:
            res = self.c.scan(progress)
        except GuiError as ex:
            self.events.put(('scan_error', str(ex)))
        except Exception as ex:                                                   # noqa: BLE001
            self.log_error(traceback.format_exc())
            self.events.put(('scan_error', 'The scan failed: %s: %s' % (type(ex).__name__, ex)))
        else:
            self.events.put(('scan_done', res))

    def on_convert(self):
        if not self.c.state == 'idle':
            return
        try:
            self._pull()
            self.c.check_root()
        except GuiError as ex:
            self.messagebox.showerror(APP_TITLE, str(ex))
            return
        if not self.c.scan_is_current():
            self.on_scan(then=True)
            return
        self._begin_convert()

    def _begin_convert(self):
        errs, warns = self.c.problems()
        if errs:
            self.messagebox.showerror(APP_TITLE, '\n\n'.join(errs[:4]))
            return
        res = self.c.scan_result
        if warns and not self.messagebox.askyesno(APP_TITLE, 'Please note:\n\n%s\n\nConvert %d map(s) anyway?' % ('\n'.join('- ' + w for w in warns), res.n_convert)):
            return
        try:
            run = self.c.make_run(lambda name, payload: self.events.put((name, payload)))
            run.start()
        except (GuiError, OSError) as ex:
            self.messagebox.showerror(APP_TITLE, 'The conversion could not be started: %s' % ex)
            return
        self.progress = {'done': 0, 'total': res.n_convert, 'current': ''}
        self.text_log.configure(state='normal')
        self.text_log.delete('1.0', 'end')
        self.text_log.configure(state='disabled')
        self._set_state('running')
        self.var_status.set('Converting %d map(s) ...' % res.n_convert)
        self.bar['value'] = 0
        self.tabs.select(self.tab_log)

    def on_cancel(self):
        c = self.c
        if c.state == 'scanning':
            self.var_status.set('The scan cannot be stopped half way; it only reads headers and ends in a moment.')
            return
        run = c.run
        if run is None:
            return
        if c.state == 'running':
            if run.request_cancel():
                c.state = 'cancelling'
                self._set_state('cancelling')
                self.var_status.set('Stopping after the map that is being converted. "Stop now" ends it at once and loses that map.')
            else:
                self.messagebox.showerror(APP_TITLE, 'The stop request could not be written; use Stop now.')
        elif c.state == 'cancelling':
            if self.messagebox.askyesno(APP_TITLE, 'Stop at once? The map that is being converted is lost; maps that are finished are kept.'):
                run.kill()
                self.var_status.set('Stopping ...')

    def on_open_folder(self, folder):
        try:
            self.c.open_output(folder)
        except (GuiError, OSError) as ex:
            self.messagebox.showerror(APP_TITLE, str(ex))

    def on_copy_summary(self):
        if self.c.summary is not None:
            self.win.clipboard_clear()
            self.win.clipboard_append(self.c.summary.text())
            self.var_status.set('The summary was copied to the clipboard.')

    def on_close(self):
        if self.c.state in ('running', 'cancelling'):
            if not self.messagebox.askyesno(APP_TITLE, 'A conversion is running. Close the window and stop it? The map being converted is lost; finished maps are kept.'):
                return
            if self.c.run is not None:
                self.c.run.kill()
        self.win.destroy()

    # -- events from the threads ----------------------------------------------------------------------
    def _tick(self):
        try:
            while True:
                name, payload = self.events.get_nowait()
                self._handle(name, payload)
        except queue.Empty:
            pass
        except Exception:                                                         # noqa: BLE001
            self.log_error(traceback.format_exc())
        self.win.after(self.POLL_MS, self._tick)

    def _handle(self, name, payload):
        if name == 'folders':
            self._fill_from_controller_folders()
        elif name == 'error':
            self.var_status.set(payload)
        elif name == 'scan_progress':
            n, path = payload
            self.var_status.set('Looking for maps ... %d files looked at' % n)
        elif name == 'scan_error':
            self._set_state('idle')
            self.var_status.set('')
            self.messagebox.showerror(APP_TITLE, payload)
        elif name == 'scan_done':
            self._show_scan(payload)
            self._set_state('idle')
            if self.after_convert:
                self.after_convert = False
                self._begin_convert()
        elif name == 'line':
            self._on_line(payload)
        elif name == 'exit':
            self._on_exit(payload)

    def _fill_from_controller_folders(self):
        self._fill_from_controller()
        self.var_status.set('Ready. Choose a folder and press "Find maps".' if not self.c.root else 'Ready. Press "Find maps".')

    def _show_scan(self, res):
        t = self.tree_found
        t.delete(*t.get_children())
        for row in res.rows:
            t.insert('', 'end', values=row.values())
        head = res.headline()
        if res.skipped_text():
            head += '  ' + res.skipped_text()
        if res.custom_shared:
            head += '  %d MCC map(s) need a custom shared.map and are skipped.' % len(res.custom_shared)
        if res.error:
            head += '  (%s)' % res.error
        self.var_headline.set(head)
        self.var_status.set('%d map(s) can be converted.' % res.n_convert if res.n_convert else 'Nothing to convert with the current choices.')

    def _on_line(self, line):
        p = parse_line(line)
        self._log(line, 'fail' if (p['kind'] == 'progress' and p['failed']) or line.startswith(('FAILED', 'ERROR')) else None)
        if p['kind'] == 'progress':
            self.progress['done'], self.progress['total'] = p['done'], max(p['total'], 1)
            if p['failed']:
                self.progress['failed'] = self.progress.get('failed', 0) + 1
            self.bar['value'] = 100.0 * p['done'] / max(p['total'], 1)
            self.var_status.set('%d of %d done%s' % (p['done'], p['total'], (', %d failed so far' % self.progress['failed']) if self.progress.get('failed') else ''))
        elif p['kind'] == 'start':
            self.progress['current'] = p['name']
            self.var_status.set('Converting %s  (%d of %d done)' % (p['name'], self.progress['done'], self.progress['total']))

    def _on_exit(self, rc):
        summary = self.c.finish_run(rc)
        self._set_state('idle')
        self._show_summary(summary)
        self.tabs.select(self.tab_sum)
        self.var_status.set(summary.headline())
        self.bar['value'] = 100 if not summary.n_failed else self.bar['value']

    def _show_summary(self, s):
        self.var_sum_head.set(s.headline())
        t = self.tree_sum
        t.delete(*t.get_children())
        for f in s.failed:
            t.insert('', 'end', values=('FAILED', f['name'], f['reason']), tags=('failed',))
        for a in s.attention:
            t.insert('', 'end', values=('skipped', a['name'], a['reason']))
        for cv in s.converted:
            t.insert('', 'end', values=('converted', cv['name'], ('%s  (%s)' % (os.path.basename(cv['out'] or ''), human(cv['size']))) if cv.get('size') else (cv.get('out') or '')))
        if s.note:
            t.insert('', 'end', values=('note', '', s.note))
        for child in list(self.frame_out.winfo_children()):
            child.destroy()
        for i, (label, folder) in enumerate(s.output_dirs):
            self.ttk.Label(self.frame_out, text='%s: %s' % (label, folder)).grid(row=i, column=0, sticky='w')
            self.ttk.Button(self.frame_out, text='Open folder', command=(lambda d=folder: self.on_open_folder(d))).grid(row=i, column=1, padx=8)

    def mainloop(self):
        self.win.mainloop()


def error_log_path():
    return os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), P.APP_NAME, 'gui_error.log')


# ======================================================================================================
# entry points
# ======================================================================================================
def _fix_streams():
    """A windowed Windows program has no console: print() must not fail (the GUI exe starts with sys.stdout / sys.stderr = None)."""
    for name in ('stdout', 'stderr'):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, 'w'))


def run_gui(settings_path=None, root=None):
    _fix_streams()
    try:
        toolkit = _real_toolkit()
    except Exception as ex:                                                       # noqa: BLE001
        sys.stderr.write('The window needs Python with tkinter (Tk), and it is not available: %s\n' % ex)
        return 2
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), NORMAL_PRIORITY)     # the window stays responsive; the conversions run below normal
    except Exception:                                                             # noqa: BLE001
        pass
    try:
        app = App(Controller(settings_path), toolkit=toolkit, start_root=root)
        app.mainloop()
    except Exception:                                                             # noqa: BLE001
        text = traceback.format_exc()
        try:
            os.makedirs(os.path.dirname(error_log_path()), exist_ok=True)
            with open(error_log_path(), 'a', encoding='utf-8') as fh:
                fh.write('--- %s\n%s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), text))
            toolkit.messagebox.showerror(APP_TITLE, 'The window could not be opened:\n\n%s\n\nThe details were saved in %s' % (one_line(text.splitlines()[-1], 300), error_log_path()))
        except Exception:                                                         # noqa: BLE001
            pass
        return 1
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='h2conv gui', description='The window program: pick a folder, find the Halo 2 maps below it, convert them all. '
                                 'Nothing is ever uploaded.')
    ap.add_argument('--version', action='version', version='%s %s' % (APP_TITLE, __version__))
    ap.add_argument('--selftest', action='store_true', help='check the installation (modules, data files, Opus, Tk) and exit; opens no window')
    ap.add_argument('--selftest-report', default=None, metavar='FILE', help='with --selftest: also write the results here as JSON')
    ap.add_argument('--settings', default=None, help='settings.json to use instead of %%APPDATA%%\\H2MapConverter\\settings.json')
    ap.add_argument('--root', default=None, help='start with this folder chosen')
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest(a.selftest_report)
    return run_gui(a.settings, a.root)


if __name__ == '__main__':
    sys.exit(main())
