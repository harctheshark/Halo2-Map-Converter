# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""The single entry point:   py -3 -m h2conv <command> [options]      (and the entry of a frozen Windows .exe)

    convert     MCC -> Vista    (h2conv.convert)
    rev         Vista -> MCC    (h2conv.rev.convert)
    batch       a whole folder, either direction (h2conv.batch)
    mapinfo     what is this .map file? (h2conv.mapinfo)
    excession   inspect / validate an Excession mod package (h2conv.excession)
    validate    deep checks of a Vista map (h2conv.validate)
    limits      engine limits of a Vista map (h2conv.limits_check)
    invariants  semantic checks of a converted Vista map (h2conv.invariants)
    paths       where the converter looks for MCC, Vista, Assembly ... (h2conv.paths)
    gui         the window program: pick a folder, find the maps below it, convert them all (h2conv.gui)
    --version

`py -3 -m h2conv.convert ...` and the other module entry points keep working.  This file exists so that ONE program (a frozen exe, where there
is no `python -m`) can start every tool, and so that `batch --isolate` can start its child processes the same way in both cases: from a
frozen exe a child is `<exe> convert|rev <arguments>`, from a Python install it is `python -m h2conv.convert|h2conv.rev.convert <arguments>`.
"""
import sys

# command -> (module, function).  Plain import statements below (not importlib) so that a freezer such as PyInstaller sees every module.
COMMANDS = ('convert', 'rev', 'batch', 'mapinfo', 'excession', 'validate', 'limits', 'invariants', 'paths', 'gui')


def _run(cmd, argv):
    if cmd == 'convert':
        from h2conv import convert as m
    elif cmd == 'rev':
        from h2conv.rev import convert as m
    elif cmd == 'batch':
        from h2conv import batch as m
    elif cmd == 'mapinfo':
        from h2conv import mapinfo as m
    elif cmd == 'excession':
        from h2conv import excession as m
    elif cmd == 'validate':
        from h2conv import validate as m
    elif cmd == 'limits':
        from h2conv import limits_check as m
        return m.main(argv)
    elif cmd == 'invariants':
        from h2conv import invariants as m
    elif cmd == 'paths':
        from h2conv import paths as m
    elif cmd == 'gui':
        from h2conv import gui as m
    else:
        raise KeyError(cmd)
    return m.main(argv)


def main(argv=None):
    import multiprocessing
    multiprocessing.freeze_support()            # a frozen exe starts its multiprocessing workers (sounds, validate --jobs) through this same entry
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] in (['--version'], ['-V']):
        from h2conv import __version__
        print('Halo 2 Map Converter %s' % __version__)
        return 0
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(__doc__.strip())
        return 0 if argv else 2
    cmd = argv[0]
    if cmd not in COMMANDS:
        print('h2conv: unknown command %r (one of: %s)' % (cmd, ', '.join(COMMANDS)))
        return 2
    try:
        from h2conv import paths
        rc = _run(cmd, argv[1:])
    except paths.PathsError as ex:             # a missing folder / file is a message, not a traceback
        print('ERROR: %s' % ex)
        return 2
    return 0 if rc is None else rc


if __name__ == '__main__':
    sys.exit(main())
