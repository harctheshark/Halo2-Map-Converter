# -*- mode: python ; coding: utf-8 -*-
# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""PyInstaller spec of the Windows build: ONE folder, `Halo2MapConverter`, with TWO programs that share every library.

    Halo 2 Map Converter.exe      the window program (no console)       entry packaging\\h2conv_gui.py
    h2conv.exe                    the command line (a console program)  entry packaging\\h2conv_cli.py

Build (from the repository folder; PyInstaller 6 and the same Python / numpy / scipy that run the source):

    py -3 -m PyInstaller --noconfirm --clean --distpath <dist folder> --workpath <work folder> packaging\\h2conv.spec

Result: <dist folder>\\Halo2MapConverter\\   (the two exes, `_internal\\` with Python, numpy, scipy, Tcl / Tk and the h2conv data files, README-WINDOWS.txt,
the project's LICENSE / NOTICE / THIRD_PARTY_NOTICES.md and a LICENSES folder with the licence texts of everything the programs bundle).
The licence texts of CPython, Tcl / Tk, numpy, scipy and PyInstaller are read from the Python and the packages that do the build, never from the
network, and the build stops if one of them is missing.

Build from the repository as it is published.  The spec refuses to run in a folder that still holds a tests or work folder (a development copy),
because the module list is "everything below h2conv\\" and the data list is "every file below h2conv\\ that is not code".
"""
import importlib.metadata
import os
import re
import shutil
import sys

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo

SPEC_DIR = os.path.abspath(SPECPATH)                                    # noqa: F821 - SPECPATH is defined by PyInstaller
ROOT = os.path.dirname(SPEC_DIR)
FOLDER = 'Halo2MapConverter'
GUI_NAME = 'Halo 2 Map Converter'
CLI_NAME = 'h2conv'


def stop(message):
    raise SystemExit('h2conv.spec: ' + message)


for need in ('LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md', os.path.join('h2conv', '__main__.py'), os.path.join('h2conv', 'gui.py'),
             os.path.join('h2conv', 'third_party', 'opus.dll'), os.path.join('packaging', 'h2conv_cli.py'), os.path.join('packaging', 'h2conv_gui.py'),
             os.path.join('packaging', 'WINDOWS_README.txt')):
    if not os.path.isfile(os.path.join(ROOT, need)):
        stop('%s is missing in %s - run this spec from the repository folder' % (need, ROOT))
for dev in ('tests', 'work'):
    if os.path.exists(os.path.join(ROOT, dev)):
        stop('%s exists in %s: that is a development copy. Build from the published repository (or its release archive).' % (dev, ROOT))
DATA_SUFFIXES = ('.xml', '.json', '.txt')                      # the kinds of data file below h2conv\

sys.path.insert(0, ROOT)
from h2conv import __version__                                           # noqa: E402

# ---------------------------------------------------------------------------------------------------------------------------------------------
# what goes in
# ---------------------------------------------------------------------------------------------------------------------------------------------
def package_data_files():
    """(source, folder inside the bundle) for every file below h2conv\\ that is not code: the layout files, the tables, the licence texts of the Opus build.
    opus.dll is added as a binary below, so that PyInstaller also bundles the DLLs it needs."""
    out = []
    pkg = os.path.join(ROOT, 'h2conv')
    for dp, dn, fn in os.walk(pkg):
        dn[:] = sorted(d for d in dn if d != '__pycache__')
        for f in sorted(fn):
            low = f.lower()
            if low.endswith(('.py', '.pyc', '.dll')):                 # code, or opus.dll (a binary, added below)
                continue
            if not low.endswith(DATA_SUFFIXES):
                stop('do not know what to do with %s (data files are %s)' % (os.path.join(dp, f), ', '.join(DATA_SUFFIXES)))
            out.append((os.path.join(dp, f), os.path.relpath(dp, ROOT)))
    return out


hidden = collect_submodules('h2conv')                                   # the handlers are found with pkgutil at run time
if 'h2conv.gui' not in hidden or 'h2conv.rev.convert' not in hidden:
    stop('collect_submodules did not find the h2conv package (is %s the repository folder?)' % ROOT)
# imported by compiled (Cython) scipy modules, which the freezer cannot read: scipy.stats._sobol does `import importlib.resources`, and
# scipy.signal pulls scipy.stats in
hidden += ['importlib.resources']
datas = package_data_files()
binaries = [(os.path.join(ROOT, 'h2conv', 'third_party', 'opus.dll'), os.path.join('h2conv', 'third_party'))]

# Python packages that must never end up in the bundle even if a hook or a user-site package would pull them in.  (numpy.f2py is NOT excluded: numpy 2
# lists it in numpy.__all__, and scipy does `from numpy import *`.)
excludes = ['pytest', 'matplotlib', 'pandas', 'IPython', 'PIL', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'wx', 'sphinx', 'jupyter', 'notebook',
            'distutils', 'setuptools', 'pkg_resources', '_distutils_hack']


def version_info(original, description):
    parts = [int(x) for x in re.findall(r'\d+', __version__)][:4]
    parts += [0] * (4 - len(parts))
    nums = tuple(parts)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[StringFileInfo([StringTable('040904B0', [
            StringStruct('CompanyName', 'harctheshark'),
            StringStruct('FileDescription', description),
            StringStruct('FileVersion', __version__),
            StringStruct('InternalName', original.rsplit('.', 1)[0]),
            StringStruct('LegalCopyright', 'Copyright (C) 2026 harctheshark. GPL-3.0-or-later.'),
            StringStruct('OriginalFilename', original),
            StringStruct('ProductName', 'Halo 2 Map Converter'),
            StringStruct('ProductVersion', __version__)])]),
            VarFileInfo([VarStruct('Translation', [1033, 1200])])])


# ---------------------------------------------------------------------------------------------------------------------------------------------
# one analysis, two programs
# ---------------------------------------------------------------------------------------------------------------------------------------------
a = Analysis([os.path.join(SPEC_DIR, 'h2conv_cli.py'), os.path.join(SPEC_DIR, 'h2conv_gui.py')],       # noqa: F821
             pathex=[ROOT], binaries=binaries, datas=datas, hiddenimports=hidden, hookspath=[], runtime_hooks=[], excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)                                                       # noqa: F821
# a.scripts holds the start-up scripts of the freezer and both entry scripts: each program gets the start-up scripts and its own entry script only
scripts_cli = [s for s in a.scripts if s[0] != 'h2conv_gui']
scripts_gui = [s for s in a.scripts if s[0] != 'h2conv_cli']
if len(scripts_cli) != len(a.scripts) - 1 or len(scripts_gui) != len(a.scripts) - 1:
    stop('could not tell the two entry scripts apart in %r' % [s[0] for s in a.scripts])
exe_cli = EXE(pyz, scripts_cli, [], exclude_binaries=True, name=CLI_NAME, console=True, upx=False, strip=False, debug=False,         # noqa: F821
              version=version_info(CLI_NAME + '.exe', 'Halo 2 Map Converter - command line'))
exe_gui = EXE(pyz, scripts_gui, [], exclude_binaries=True, name=GUI_NAME, console=False, upx=False, strip=False, debug=False,       # noqa: F821
              version=version_info(GUI_NAME + '.exe', 'Halo 2 Map Converter'))
coll = COLLECT(exe_cli, exe_gui, a.binaries, a.datas, strip=False, upx=False, name=FOLDER)                                      # noqa: F821

# ---------------------------------------------------------------------------------------------------------------------------------------------
# after the collect: documents and licence texts beside the programs
# ---------------------------------------------------------------------------------------------------------------------------------------------
OUT = os.path.join(DISTPATH, FOLDER)                                   # noqa: F821
LICENSE_FILE = re.compile(r'(^|[\\/])(LICENSE|LICENCE|COPYING|NOTICE)[^\\/]*$', re.I)


def copy_text(src, dest_dir, dest_name):
    os.makedirs(dest_dir, exist_ok=True)
    shutil.copyfile(src, os.path.join(dest_dir, dest_name))


def distribution_licenses(dist):
    """[(path inside the distribution, file)] of every licence / copying / notice file that the installed distribution `dist` carries: the one in its
    metadata folder (<name>.dist-info) and the ones that sit beside code inside the package (numpy\\random, scipy\\spatial\\qhull_src ...)."""
    d = importlib.metadata.distribution(dist)
    found = {}
    for f in d.files or []:
        if LICENSE_FILE.search(str(f)):
            path = str(f.locate())
            if os.path.isfile(path):
                found[str(f).replace('\\', '/')] = path
    return sorted(found.items())


def finish():
    lic = os.path.join(OUT, 'LICENSES')
    for name in ('LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md'):
        copy_text(os.path.join(ROOT, name), OUT, name)
    copy_text(os.path.join(ROOT, 'packaging', 'WINDOWS_README.txt'), OUT, 'README-WINDOWS.txt')
    for name in ('README.md', 'RELEASE_NOTES.md'):
        if os.path.isfile(os.path.join(ROOT, name)):
            copy_text(os.path.join(ROOT, name), OUT, name)
    for name in ('HOW_IT_WORKS.md', 'KNOWN_ISSUES.md'):
        if os.path.isfile(os.path.join(ROOT, 'docs', name)):
            copy_text(os.path.join(ROOT, 'docs', name), os.path.join(OUT, 'docs'), name)
    # the licence texts of what the programs bundle
    base = sys.base_prefix
    wanted = [('CPython-LICENSE.txt', os.path.join(base, 'LICENSE.txt')),
              ('Tcl-Tk-license.terms.txt', os.path.join(base, 'tcl', 'tk8.6', 'license.terms')),
              ('libopus-LICENSE-opus.txt', os.path.join(ROOT, 'h2conv', 'third_party', 'LICENSE-opus.txt'))]
    for dest, src in wanted:
        if not os.path.isfile(src):
            stop('licence text missing: %s' % src)
        copy_text(src, lic, dest)
    built = []
    for dist, label in (('numpy', 'numpy'), ('scipy', 'scipy'), ('pyinstaller', 'PyInstaller'), ('pyinstaller-hooks-contrib', 'PyInstaller-hooks-contrib')):
        files = distribution_licenses(dist)
        if not any('.dist-info/' in rel for rel, _ in files):
            stop('no licence file found in the metadata folder of the installed distribution %r' % dist)
        for rel, src in files:                                          # LICENSES\<label>\<path inside the distribution>
            copy_text(src, os.path.join(lic, label, os.path.dirname(rel).replace('/', os.sep)), os.path.basename(rel))
        built.append('%s %s' % (label, importlib.metadata.version(dist)))
    with open(os.path.join(lic, 'README.txt'), 'w', encoding='utf-8', newline='\r\n') as fh:
        fh.write(LICENSES_README + '\nBuilt with: Python %s, %s.\n' % (sys.version.split()[0], ', '.join(built)))


LICENSES_README = """Licences of what is inside this folder
======================================

Halo 2 Map Converter itself: GPL-3.0-or-later, Copyright (C) 2026 harctheshark.  The full text is the file LICENSE in the folder above this one;
NOTICE and THIRD_PARTY_NOTICES.md (also above) name the other works that are part of the download and say where each one comes from.  The bundled
Assembly layout files are GPL-3.0, so the download as a whole is distributed under GPL-3.0.  The source code of this version is the "Source code"
download of the same release page.

What the programs add inside the _internal folder, and the text that covers it:

  CPython 3.10 (python310.dll, the standard library, the *.pyd modules)
        CPython-LICENSE.txt - the licence file of the Windows build of Python: the Python Software Foundation License, the terms for the Microsoft
        distributable code in that build, and the notices of some of the libraries inside it (OpenSSL 1.1.1 = libcrypto-1_1.dll and libssl-1_1.dll,
        libffi = libffi-7.dll, bzip2, Tcl / Tk).  zlib, expat (pyexpat.pyd), liblzma (_lzma.pyd) and libmpdec (_decimal.pyd) are inside Python too and
        are permissively licensed (zlib licence, MIT, public domain / 0BSD, BSD-2-Clause); their notices are in the Python source distribution and in the
        chapter "Licenses and Acknowledgements for Incorporated Software" of the Python documentation.
  Tcl / Tk 8.6 (tcl86t.dll, tk86t.dll, _tcl_data, _tk_data; used by the window program)
        Tcl-Tk-license.terms.txt
  numpy (BSD-3-Clause) and the libraries in its wheel (OpenBLAS in numpy.libs, the Microsoft C++ runtime DLL beside it)
        numpy\\ - every licence file of the numpy package; the main one is in the *.dist-info folder and lists the bundled libraries
  scipy (BSD-3-Clause) and the libraries in its wheel (OpenBLAS in scipy.libs)
        scipy\\ - every licence file of the scipy package, the main one in the *.dist-info folder
  libopus (h2conv\\third_party\\opus.dll)
        libopus-LICENSE-opus.txt - BSD-3-Clause with the patent licence notes of the Opus codec
  PyInstaller (the bootloader that is the start of both .exe files, and its run-time support)
        PyInstaller\\ - PyInstaller\\...\\COPYING.txt: GPL-2.0-or-later with the bootloader exception, which allows programs that are frozen
        with it to be distributed under their own licence; the run-time hooks are under the Apache License 2.0.
        PyInstaller-hooks-contrib\\ - the licence of the community hooks that were used.
  Microsoft Visual C++ runtime and Universal C runtime DLLs (VCRUNTIME140*.dll, msvcp140*.dll, ucrtbase.dll, api-ms-win-crt-*.dll)
        redistributable files of the Microsoft Visual C++ Redistributable / Universal CRT, copied from the Python and the packages that built this
        folder and used under the terms that come with them (see the section "Additional Conditions for this Windows binary build" in CPython-LICENSE.txt).

The Python code of h2conv is stored inside the two .exe files in compiled form; nothing else of this download is hidden or encrypted.
"""

finish()
