# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Entry point of `h2conv.exe`, the command line program of the Windows build (a console program).

    h2conv.exe <command> [options]        the same commands as `py -3 -m h2conv`: convert, rev, batch, mapinfo, excession, validate, limits,
                                          invariants, paths, gui

Only used by packaging\\h2conv.spec; a Python install starts the program with `py -3 -m h2conv`.  `multiprocessing.freeze_support()` comes first:
a frozen program starts the helper processes of the sound transcoder (and of `validate --jobs`) by running this same exe with a hidden argument.
"""
import multiprocessing
import sys


def run():
    multiprocessing.freeze_support()
    from h2conv.__main__ import main
    return main()


if __name__ == '__main__':
    sys.exit(run())
