# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Handler registry of the REVERSE direction (Halo 2 Vista cache v8 -> Halo 2 MCC cache v13).

A SEPARATE registry with the same decorators as `h2conv.handlers` (forward): the forward handlers are keyed by the
VISTA (target) path and take (ctx, mcc_node, vista_node); the reverse handlers are keyed by the MCC (target) path and
take the arguments in source -> target order:

  @node_handler('bitm', '/Bitmaps')        fn(ctx, vista_node, mcc_node) -> None
        Runs AFTER the generic conversion of one struct (main struct path '' or a block element path), after its
        children were converted.  `vista_node` is the decoded SOURCE struct (read it, do not modify it), `mcc_node` the
        generic result in the TARGET layout (mutate fields, blocks, datas, patches in place).  `path` is the tagtree path
        built from the MCC (target) block names: '' = main struct, '/Regions/Permutations' ... .
  @tag_handler('ugh!')                       fn(ctx, entry) -> tagtree.Node
        REPLACES the whole generic conversion of a tag of that group (entry = rev planner entry).  Return the finished
        MCC root Node (layout ctx.lay_mcc[group]).
  @pre_pass                                  fn(ctx) -> None
        Runs once before any tag is converted (e.g. build a sound bundle, a script opcode map).
  @post_pass                                 fn(ctx, converted) -> None
        Runs once after all tags are converted; converted = {out_index: MCC root Node}.

Priorities: lower runs first (default 100).  Several node handlers may be registered for one (group, path).  Modules in
this package are imported by `load_all()` (handler modules must not import h2conv.rev.transcode at import time).

"""
import importlib
import pkgutil

NODE = {}       # (group, path) -> [(prio, name, fn)]
TAG = {}        # group -> (prio, name, fn)
DONOR = {}      # group -> (prio, name, fn)
PRE = []        # [(prio, name, fn)]
POST = []       # [(prio, name, fn)]


def _name(fn):
    return '%s.%s' % (fn.__module__, fn.__name__)


def node_handler(group, path='', prio=100):
    def deco(fn):
        NODE.setdefault((group, path), []).append((prio, _name(fn), fn))
        NODE[(group, path)].sort(key=lambda x: (x[0], x[1]))
        return fn
    return deco


def tag_handler(group, prio=100):
    def deco(fn):
        old = TAG.get(group)
        if old is not None and old[1] != _name(fn) and old[0] <= prio:
            raise RuntimeError('two tag handlers for %s: %s and %s' % (group, old[1], _name(fn)))
        TAG[group] = (prio, _name(fn), fn)
        return fn
    return deco


def donor_handler(group, prio=100):
    """@donor_handler('snd!')  fn(ctx, entry, mcc_node, mcc_cache) -> tagtree.Node
    Optional.  Replaces the DRIVER's generic import of an `mcc_donor` plan entry of that group (MCC's own tag copied into the
    output): `mcc_node` is the donor tag decoded with the MCC layouts, `mcc_cache` the MccCache it comes from (datums and string
    ids inside it are relative to that map).  The default import remaps tag references by name, string ids by text and copies
    the raw bitmap / geometry blocks of the donor."""
    def deco(fn):
        old = DONOR.get(group)
        if old is not None and old[1] != _name(fn) and old[0] <= prio:
            raise RuntimeError('two donor handlers for %s: %s and %s' % (group, old[1], _name(fn)))
        DONOR[group] = (prio, _name(fn), fn)
        return fn
    return deco


def pre_pass(prio=100):
    def deco(fn):
        PRE.append((prio, _name(fn), fn))
        PRE.sort(key=lambda x: (x[0], x[1]))
        return fn
    return deco


def post_pass(prio=100):
    def deco(fn):
        POST.append((prio, _name(fn), fn))
        POST.sort(key=lambda x: (x[0], x[1]))
        return fn
    return deco


_loaded = False


def load_all(skip=()):
    """Import every handler module in this package (once).  Returns [(module, error repr)] for modules that failed."""
    global _loaded
    failed = []
    if _loaded:
        return failed
    for m in pkgutil.iter_modules(__path__):
        if m.name.startswith('_') or m.name in skip:
            continue
        try:
            importlib.import_module(__name__ + '.' + m.name)
        except Exception as ex:                                   # noqa: BLE001  one broken module must not kill the run
            failed.append((m.name, repr(ex)))
    _loaded = True
    return failed


def node_handlers(group, path):
    return [fn for _, _, fn in NODE.get((group, path), ())]


def tag_handler_for(group):
    h = TAG.get(group)
    return h[2] if h else None


def donor_handler_for(group):
    h = DONOR.get(group)
    return h[2] if h else None


def summary():
    return {
        'node': {'%s|%s' % k: [n for _, n, _ in v] for k, v in NODE.items()},
        'tag': {g: n for g, (_, n, _) in TAG.items()},
        'donor': {g: n for g, (_, n, _) in DONOR.items()},
        'pre': [n for _, n, _ in PRE],
        'post': [n for _, n, _ in POST],
    }
