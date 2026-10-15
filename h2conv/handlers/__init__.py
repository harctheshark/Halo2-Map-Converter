# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Handler registry.

Each topic module in this package registers functions with the decorators below.  transcode.py imports every
module of the package (load_all()) and calls the registered functions; modules must not import transcode.py at
import time (avoid cycles) - they receive a Ctx object.

  @node_handler('bitm', '/Bitmaps')        fn(ctx, mcc_node, vista_node) -> None
        Runs AFTER the generic conversion of one struct (main struct path '' or a block element path), after its
        children were converted.  Mutates vista_node in place (fields, blocks, datas, patches).
  @tag_handler('ugh!')                       fn(ctx, entry) -> tagtree.Node
        REPLACES the whole generic conversion of a tag of that group (entry = planner.PlanEntry).  Return the
        finished Vista root Node.
  @pre_pass                                  fn(ctx) -> None
        Runs once before any tag is converted (e.g. build a sound bundle, a script opcode map).
  @post_pass                                 fn(ctx, converted) -> None
        Runs once after all tags are converted; converted = {out_index: vista root Node}.

Priorities: lower runs first (default 100).  Several node handlers may be registered for one (group, path).
"""
import importlib
import pkgutil

NODE = {}       # (group, path) -> [(prio, name, fn)]
TAG = {}        # group -> (prio, name, fn)
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
    """Import every handler module in this package (once)."""
    global _loaded
    if _loaded:
        return
    for m in pkgutil.iter_modules(__path__):
        if m.name.startswith('_') or m.name in skip:
            continue
        importlib.import_module(__name__ + '.' + m.name)
    _loaded = True


def node_handlers(group, path):
    return [fn for _, _, fn in NODE.get((group, path), ())]


def tag_handler_for(group):
    h = TAG.get(group)
    return h[2] if h else None


def summary():
    return {
        'node': {'%s|%s' % k: [n for _, n, _ in v] for k, v in NODE.items()},
        'tag': {g: n for g, (_, n, _) in TAG.items()},
        'pre': [n for _, n, _ in PRE],
        'post': [n for _, n, _ in POST],
    }
