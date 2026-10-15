# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Entry point of `Halo 2 Map Converter.exe`, the window program of the Windows build (a program without a console).

    Halo 2 Map Converter.exe                          opens the window
    Halo 2 Map Converter.exe --selftest               checks the installation (modules, data files, Opus, Tcl / Tk) and exits; opens NO window
    Halo 2 Map Converter.exe --selftest --selftest-report FILE      the same, and writes the results to FILE (a windowed program has no console)
    Halo 2 Map Converter.exe --root FOLDER            opens the window with this folder chosen
    Halo 2 Map Converter.exe <command> ...            any command line command (convert, rev, batch, ...) also works, but prints nothing

Only used by packaging\\h2conv.spec.  A windowed program has no console, so sys.stdout / sys.stderr start as None: they are pointed at the null
device before anything can print to them.  `multiprocessing.freeze_support()` comes first, for the helper processes of a frozen program.
"""
import multiprocessing
import os
import sys


def _no_console_streams():
    for name in ('stdout', 'stderr'):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, 'w', encoding='utf-8'))


def run():
    multiprocessing.freeze_support()
    _no_console_streams()
    args = sys.argv[1:]
    if not args or (args[0].startswith('-') and args[0] not in ('--version', '-V', '-h', '--help')):
        args = ['gui'] + args                 # no arguments, or only options (--selftest, --root, --settings): the window program
    from h2conv.__main__ import main
    return main(args)


if __name__ == '__main__':
    sys.exit(run())
