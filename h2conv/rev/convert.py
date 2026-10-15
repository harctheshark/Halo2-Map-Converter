# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Halo 2 Vista (cache v8) -> Halo 2 MCC (cache v13) map converter: driver + CLI.

    py -3 -m h2conv.rev.convert <vista.map> <out.map|dir> [--vista-maps DIR] [--mcc-maps DIR] [--name NAME] [--report r.json]
                                [--no-handlers] [--no-tag-handlers] [--no-verify] [--no-donor-raw] [--limit-groups g1,g2]
                                [--force-output-dir] [--debug] [--quiet] [--plan-opt key=value ...]

Pipeline (mirror of h2conv/convert.py):

    plan   = h2conv.rev.planner.build_rev_plan(vista, RevEnv)       which Vista tags become MCC tags ('convert'), which are MCC's own
                                                                    tags ('mcc_donor'), output indices / datums (self-contained map)
    strings= h2conv.rev.strings.McStringTable (2463 engine strings frozen) ; ctx = transcode.RevCtx
    handlers.load_all + @pre_pass -> 'convert' entries: transcode.convert_tag ; 'mcc_donor' entries: import_donor (MCC tag decoded with the
    MCC layouts, tag references re-pointed BY NAME, string ids BY TEXT, bitmap / geometry blobs copied) -> @post_pass ->
    place raws (sounds, geometry) -> strings / names -> locale hook -> bitmaps -> resolve tokens -> encode every Node with the MCC layout and
    the MCC alignment table at its final address -> writer (h2conv/mcc_writer.py when present, else h2conv/rev/mini_writer.py)
    -> <stem>.tmp -> rename -> verify_output (MccCache + a plugin walk of every tag: every block / dataref inside the tag data, every tag
    reference resolves to a tag of the right group, every string id valid, every tag tiles).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import collections
import hashlib
import importlib
import json
import os
import struct
import sys
import time
import traceback

from .. import paths as _paths
from .. import tagtree as T
from ..mcc_cache import MccCache
from ..vista_cache import VistaCache
from . import handlers as H
from . import transcode as X

ROOT = X.ROOT
NULL = 0xFFFFFFFF
_U32 = struct.Struct('<I')
_U32x2 = struct.Struct('<II')


def _log(opts, msg):
    if not opts.get('quiet'):
        print(msg)
        sys.stdout.flush()


def peak_rss_mb():
    try:
        class PMC(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                        ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.WinDLL('kernel32')
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        fn = k32.K32GetProcessMemoryInfo
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        fn.restype = ctypes.c_int
        fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.PeakWorkingSetSize / 1048576.0
    except Exception:
        return 0.0


# ================================================================================================
# the MCC writer: the real one when it exists and conforms, else the minimal internal one
# ================================================================================================
def load_writer(prefer_real=True):
    """-> (module, label).  The module offers new_header, groups_from_stock, MccBuilder (or MiniBuilder), MccTagInstance, make_datum."""
    if prefer_real:
        try:
            mw = importlib.import_module('h2conv.mcc_writer')
            for name in ('MccBuilder', 'new_header', 'groups_from_stock', 'MccTagInstance'):
                if not hasattr(mw, name):
                    raise AttributeError(name)
            return mw, 'h2conv.mcc_writer'
        except Exception:                                           # noqa: BLE001
            pass
    from . import mini_writer as mw
    return mw, 'h2conv.rev.mini_writer'


def builder_class(mw):
    return getattr(mw, 'MccBuilder', None) or mw.MiniBuilder


# ================================================================================================
# MCC donors
# ================================================================================================
def _walk_nodes(root):
    """Every distinct Node of a tree (the decoder shares lists of identical blocks between fields: visit each object once)."""
    seen = set()
    stack = [root]
    while stack:
        n = stack.pop()
        if id(n) in seen:
            continue
        seen.add(id(n))
        yield n
        for kids in n.blocks.values():
            stack.extend(kids)


class DonorRaw(object):
    """Raw blobs of MCC donor tags: bitmap pixel blobs (textures.dat or the donor map), geometry 'blkh' blocks."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._dat = {}
        self._tokens = {}

    def _dat_dir(self):
        return _paths.mcc_dat_dir(self.ctx.opts.get('mcc_dat_dir'))

    def _texture_bytes(self, cache, off, size):
        flags = cache.header.get('flags', 0) if hasattr(cache.header, 'get') else 0
        if flags & 0x08:
            p = os.path.join(self._dat_dir(), 'textures.dat')
            fh = self._dat.get(p)
            if fh is None:
                fh = self._dat[p] = open(p, 'rb')
            fh.seek(off)
            return fh.read(size)
        return cache.raw(off, size)

    def close(self):
        for fh in self._dat.values():
            fh.close()
        self._dat = {}

    def bitmap(self, node, cache):
        """Re-add the donor element's pixel blob: patches[0x38] = token (LOD1 Offset), size stays."""
        off = _U32.unpack_from(node.data, 0x38)[0]
        size = _U32.unpack_from(node.data, 0x50)[0]
        if not size:
            return
        key = (id(cache), off, size)
        tok = self._tokens.get(key)
        if tok is None:
            blob = self._texture_bytes(cache, off, size)
            if len(blob) != size:
                raise ValueError('bitmap blob 0x%X+0x%X outside its store' % (off, size))
            tok = self._tokens[key] = self.ctx.add_raw('bitmap', blob, 0x80)
        node.patches[0x38] = tok

    def geometry(self, node, cache, info_off):
        info = node.data[info_off:info_off + 0x24]
        boff, bsize = _U32x2.unpack_from(info, 0)
        if boff in (0, NULL) or not bsize:
            return
        off = boff & 0x3FFFFFFF
        key = (id(cache), off, bsize)
        tok = self._tokens.get(key)
        if tok is None:
            blob = cache.raw(off, bsize)
            if blob[:4] != b'hklb' or blob[-4:] != b'fklb':
                raise ValueError("no 'blkh' block at 0x%X" % off)
            tok = self._tokens[key] = self.ctx.add_raw('geometry', blob, 0x80)
        node.patches[info_off] = tok


_GEOMETRY_SITES = None


def geometry_sites():
    global _GEOMETRY_SITES
    if _GEOMETRY_SITES is None:
        from ..handlers import geometry as GEO                 # forward module: the 14 geometry_block_info users (read-only)
        _GEOMETRY_SITES = {}
        for s in GEO.SITES:
            _GEOMETRY_SITES.setdefault((s.group, s.path), []).append(s.info_off)
    return _GEOMETRY_SITES


def import_donor(ctx, entry, raw=None):
    """An 'mcc_donor' plan entry: MCC's own tag of that (group, name), decoded with the MCC layouts and re-pointed into the output."""
    prev = ctx.cur_src
    ctx.cur_entry = entry
    try:
        got = ctx.plan.donor_source_of(entry)
        if got is None:
            raise KeyError('no donor source for %s %s' % (entry.group, entry.name))
        cache, mtag = got
        node = ctx.decode_mcc(cache, mtag)
        fn = H.donor_handler_for(entry.group)
        if fn is not None and not ctx.opts.get('no_tag_handlers'):
            out = fn(ctx, entry, node, cache)
            ctx.stats['donor_handler:' + entry.group] += 1
            return out
        clone_donor(ctx, entry, node, cache, raw)
        ctx.stats['donor:' + entry.group] += 1
        return node
    finally:
        ctx.cur_src = prev
        ctx.cur_entry = None


def clone_donor(ctx, entry, root, cache, raw=None):
    """In place: tag references by name (plan.remap_mcc_datum), string ids by text, raw bitmap / geometry blobs copied."""
    sites = geometry_sites()
    plan = ctx.plan
    for n in _walk_nodes(root):
        for f in n.fields:
            t = f.type
            if t == 'tagref':
                if f.size == 8:
                    g, d = _U32x2.unpack_from(n.data, f.offset)
                    if d != NULL and not (d == 0 and g == 0):
                        nd = plan.remap_mcc_datum(cache, d)
                        if nd == NULL:
                            ctx.stats['donor_tagref_dropped'] += 1
                        _U32x2.pack_into(n.data, f.offset, g, nd)
                else:
                    d = _U32.unpack_from(n.data, f.offset)[0]
                    if d not in (NULL, 0):
                        nd = plan.remap_mcc_datum(cache, d)
                        if nd == NULL:
                            ctx.stats['donor_tagref_dropped'] += 1
                        _U32.pack_into(n.data, f.offset, nd)
            elif t == 'stringid':
                s = _U32.unpack_from(n.data, f.offset)[0]
                if s not in (0, NULL):
                    try:
                        s = ctx.sid(cache.string_id(s))
                    except Exception as ex:                          # noqa: BLE001
                        ctx.warn('bad_stringid', 'donor %s %s: %r' % (entry.group, entry.name, ex))
                        s = 0
                    _U32.pack_into(n.data, f.offset, s)
        if raw is not None and not ctx.opts.get('no_donor_raw'):
            try:
                if n.group == 'bitm' and n.path == '/Bitmaps':
                    raw.bitmap(n, cache)
                for info_off in sites.get((n.group, n.path), ()):
                    raw.geometry(n, cache, info_off)
            except Exception as ex:                                  # noqa: BLE001
                ctx.warn('donor_raw_failed', '%s %s %s: %r' % (n.group, entry.name, n.path, ex))
                ctx.stats['donor_raw_failed'] += 1
    X.resort_engine_sorted(ctx, root)                                # string ids renumbered -> binary-searched blocks re-sorted
    return root


# ================================================================================================
# output verification: MccCache + a plugin walk of every tag
# ================================================================================================
def _check_bitmap_blob(c, off, size):
    """'' when file[off:off+size] is a well formed MCC chunked pixel resource (u32 n; i32 size[n]; chunks), else the reason."""
    if off + size > c.file_size or off < 0x380:
        return 'blob 0x%X+0x%X outside the file' % (off, size)
    if size < 8:
        return 'blob of %d bytes' % size
    n = _U32.unpack(c.raw(off, 4))[0]
    if n == 0 or n > 4096 or 4 + 4 * n > size:
        return 'chunk count %d' % n
    sizes = struct.unpack('<%di' % n, c.raw(off + 4, 4 * n))
    if 4 + 4 * n + sum(abs(s) for s in sizes) != size:
        return 'chunk sizes %d + header %d != blob size %d' % (sum(abs(s) for s in sizes), 4 + 4 * n, size)
    return ''


def verify_output(path, lay_mcc=None, max_problems=40):
    """Open the written file with MccCache and walk EVERY tag with the MCC plugin layouts:
        * every tag_block / data_reference pointer lies inside the tag data of the map (decode_tag reads it, MccCache rejects others)
        * every non-null tag reference (8 byte {group, datum} and 4 byte datum, except the 0 of empty postprocess elements) names a tag
          of the table, and an 8 byte reference carries that tag's group
        * every string id is valid for the map's string table (index < count, length byte == utf-8 length)
        * every tag 'tiles': the end of its last block / data == the end of the tag (4-byte padded), nothing points beyond it
        * raw resources: every bitmap element's LOD1 blob (offset +0x38, size +0x50) lies inside the file and is a well formed chunked
          resource (`u32 n; i32 size[n]; chunks`, 4 + 4n + sum|size| == size); every geometry_block_info that names a block points
          at a 'blkh' ... 'fklb' block of exactly block_size bytes
    Returns a dict (counts + problems)."""
    lay_mcc = lay_mcc or X.layouts('mcc')
    res = collections.OrderedDict()
    problems = []
    cnt = collections.Counter()
    c = MccCache(path)
    sids = c.string_ids
    gsites = geometry_sites()
    raw_lo = c.header['raw_table_offset']
    for t in c.tags:
        if t.is_null or not t.has_data:
            if not t.is_null:
                cnt['tags_without_data'] += 1
            continue
        lay = lay_mcc.get(t.group)
        if lay is None:
            problems.append(('no_layout', t.group, t.name, ''))
            continue
        cnt['tags'] += 1
        try:
            root = T.decode_tag(lambda a, n, c=c: c.read_at_addr(a, n), lay, t.addr, t.group)
        except Exception as ex:                                       # noqa: BLE001
            problems.append(('decode', t.group, t.name, repr(ex)[:160]))
            continue
        hi = t.addr + len(root.data)
        lo_ok = True
        for n in _walk_nodes(root):
            cnt['structs'] += 1
            if n.group == 'bitm' and n.path == '/Bitmaps':
                boff, bsize = _U32.unpack_from(n.data, 0x38)[0], _U32.unpack_from(n.data, 0x50)[0]
                if bsize:
                    cnt['raw_bitmaps'] += 1
                    why = _check_bitmap_blob(c, boff, bsize)
                    if why:
                        problems.append(('raw_bitmap', t.group, t.name, why))
            for info_off in gsites.get((n.group, n.path), ()):
                boff, bsize = _U32x2.unpack_from(n.data, info_off)
                if boff not in (0, NULL) and bsize:
                    cnt['raw_geometry'] += 1
                    if boff + bsize > c.file_size or c.raw(boff, 4) != b'hklb' or c.raw(boff + bsize - 4, 4) != b'fklb':
                        problems.append(('raw_geometry', t.group, t.name, '%s info +0x%X block 0x%X+0x%X' % (n.path, info_off, boff, bsize)))
            # blocks halo2.dll binary-searches by label (jmad Modes / Weapon Class / Weapon Type / ...): must ascend with THIS map's
            # string ids, or lookups silently miss (unsorted donor jmads: driver / gunner seats could not be entered)
            for off in X._sort_offsets(n.fields, n.group, n.path):
                kids = n.blocks.get(off) or []
                if len(kids) > 1:
                    cnt['engine_sorted_blocks'] += 1
                    if X.sort_block_elements(kids) is not kids:
                        problems.append(('unsorted_engine_block', t.group, t.name, '%s|%s +0x%X' % (n.group, n.path, off)))
            for f in n.fields:
                tp = f.type
                if tp in T.BLOCK_TYPES:
                    cc, ptr = struct.unpack_from('<iI', n.data, f.offset)
                    if cc > 0 and ptr:
                        cnt['blocks'] += 1
                        end = ptr + cc * T.element_size(f)
                        if ptr < c.mask or end > c.mask + c.meta_size:
                            problems.append(('block_range', t.group, t.name, '%s|%s %s' % (n.group, n.path, f.name)))
                        # pointers into another (earlier) tag are legal (shared sub-blocks); outside the tag only if inside an earlier tag
                        elif not (t.addr <= ptr and end <= t.addr + t.size):
                            o = c.owner_of(ptr)
                            if o is None or end > o.addr + o.size:
                                problems.append(('block_owner', t.group, t.name, '%s|%s %s' % (n.group, n.path, f.name)))
                            else:
                                cnt['blocks_in_other_tag'] += 1
                        else:
                            hi = max(hi, end)
                elif tp == 'dataref':
                    sz, ptr = struct.unpack_from('<iI', n.data, f.offset)
                    if sz > 0 and ptr:
                        cnt['datarefs'] += 1
                        end = ptr + sz
                        if ptr < c.mask or end > c.mask + c.meta_size:
                            problems.append(('data_range', t.group, t.name, '%s|%s %s' % (n.group, n.path, f.name)))
                        elif not (t.addr <= ptr and end <= t.addr + t.size):
                            o = c.owner_of(ptr)
                            if o is None or end > o.addr + o.size:
                                problems.append(('data_owner', t.group, t.name, '%s|%s %s' % (n.group, n.path, f.name)))
                            else:
                                cnt['datarefs_in_other_tag'] += 1
                        else:
                            hi = max(hi, end)
                elif tp == 'tagref':
                    if f.size == 8:
                        g, d = _U32x2.unpack_from(n.data, f.offset)
                    else:
                        g, d = None, _U32.unpack_from(n.data, f.offset)[0]
                    if d == NULL or (d == 0 and g in (None, 0)):
                        cnt['tagrefs_null'] += 1
                        continue
                    cnt['tagrefs'] += 1
                    tt = c.tag_by_datum(d)
                    if tt is None:
                        problems.append(('tagref_unresolved', t.group, t.name, '%s|%s %s 0x%08X' % (n.group, n.path, f.name, d)))
                    elif g is not None and g != tt.group_int:
                        problems.append(('tagref_group', t.group, t.name, '%s|%s %s -> %s %s (word %08X)' % (
                            n.group, n.path, f.name, tt.group, tt.name, g)))
                elif tp == 'stringid':
                    s = _U32.unpack_from(n.data, f.offset)[0]
                    if s in (0, NULL):
                        continue
                    cnt['stringids'] += 1
                    i = s & 0xFFFFFF
                    if i >= len(sids) or sids[i] is None or (s >> 24) != len(sids[i].encode('utf-8')):
                        problems.append(('stringid', t.group, t.name, '%s|%s %s 0x%08X' % (n.group, n.path, f.name, s)))
        # tiling: the last emitted byte of the tag (its blocks / datarefs inside it) is the (4-padded) end of the tag
        tail = lay.tail_pad or 0
        want_end = (t.addr + t.size) - tail
        if ((hi + 3) & ~3) != want_end:
            cnt['tiling_mismatch'] += 1
            if cnt['tiling_mismatch'] <= 5:
                problems.append(('tiling', t.group, t.name, 'children end 0x%X, tag end 0x%X (tail %d)' % (hi, t.addr + t.size, tail)))
    c.close()
    by = collections.Counter(p[0] for p in problems)
    res['counts'] = dict(cnt)
    res['problems_by_kind'] = dict(by)
    res['problems'] = [list(p) for p in problems[:max_problems]]
    res['ok'] = not problems
    return res


# ================================================================================================
# driver
# ================================================================================================
def sha256_file(path, block=1 << 22):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            b = fh.read(block)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def resolve_output(out_arg, name=None, default_stem=None):
    out = os.path.abspath(out_arg)
    ext = os.path.splitext(out)[1].lower()
    is_dir = os.path.isdir(out) or str(out_arg).endswith(('\\', '/')) or (ext == '' and not os.path.isfile(out))
    if not is_dir and ext != '.map':
        raise ValueError('output file %r must end in .map (or pass a folder)' % out_arg)
    if is_dir:
        stem = name or default_stem
        if not stem:
            raise ValueError('output is a directory: pass --name or a file path')
        return os.path.join(out, stem + '.map')
    if name:
        return os.path.join(os.path.dirname(out), name + '.map')
    return out


def _guard_output(src_path, out_path, vista_maps, opts):
    """Never overwrite the input; never write into a stock / game / toolkit map folder unless --force-output-dir."""
    norm = lambda q: os.path.normcase(os.path.abspath(q))          # noqa: E731
    if norm(out_path) == norm(src_path):
        raise ValueError('output path is the input map: %s' % out_path)
    if opts.get('force_output_dir'):
        return
    outdir = norm(os.path.dirname(os.path.abspath(out_path)))
    mcc = _paths.mcc_maps_dir(required=False)
    h2ek = opts.get('h2ek_maps') or _paths.h2ek_maps_dir()
    for d in (vista_maps, os.path.dirname(os.path.abspath(src_path)), opts.get('mcc_maps_dir'), mcc, h2ek, X.MCC_DECOMP,
              os.path.join(mcc, _paths.STOCK_BACKUP_DIR_NAME) if mcc else None):
        if d and norm(d) == outdir:
            raise ValueError('refusing to write into %s (stock / game / input map folder); choose another output folder '
                             'or pass --force-output-dir' % d)


def make_plan(vista, env, opts, log=None):
    from . import planner as PL
    plan = PL.build_rev_plan(vista, env, opts)
    for attr in ('entries', 'remap_datum', 'tag_count', 'scenario_datum', 'globals_datum', 'tag_names', 'donor_source_of'):
        if not hasattr(plan, attr):
            raise AttributeError('rev plan has no %s' % attr)
    return plan


def make_strings():
    from . import strings as S
    return S.McStringTable.from_mcc_builtin()


def convert_map(src_path, out_path, vista_maps=None, mcc_maps=None, report_path=None, opts=None):
    t_start = time.time()
    opts = dict(opts or {})
    vista_maps = _paths.vista_maps_dir(vista_maps)           # None = H2CONV_VISTA_MAPS / settings.json / a search of your drives
    _paths.assembly_layout()                                 # the Assembly layout files are complete? (one readable error here, not a failure deep inside the planner)
    opts['vista_maps'] = vista_maps
    if mcc_maps:
        opts['mcc_maps_dir'] = mcc_maps                      # the output guard and the group table (build_file) honour --mcc-maps too
    # textures.dat (bitmap donors) must be readable BEFORE planning; one folder for textures.dat / sounds_*.dat everywhere
    opts.setdefault('mcc_dat_dir', _paths.require_mcc_files(('textures.dat',), mcc_maps))
    log = lambda m: _log(opts, m)                                    # noqa: E731
    # (the output guard runs below, after resolve_output has made the real file name: `out_path` may be a folder, and the folder that holds
    #  that folder is allowed to be the folder of the input map)
    timings = collections.OrderedDict()
    report = collections.OrderedDict()
    report['input'] = src_path

    def tick(name, t0):
        timings[name] = round(time.time() - t0, 2)

    # ---- open + plan ----------------------------------------------------------------------------------
    t0 = time.time()
    vista = VistaCache(src_path)
    name_opt = opts.get('name')
    out_path = resolve_output(out_path, name_opt, os.path.splitext(os.path.basename(src_path))[0])
    _guard_output(src_path, out_path, vista_maps, opts)
    _paths.refuse_existing(out_path, opts, report_path)            # R16: before the planner runs
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    report['output'] = out_path
    from . import planner as PL
    env_kw = {}
    if mcc_maps:
        env_kw['stock_dir'] = mcc_maps
    if opts.get('h2ek_maps'):
        env_kw['h2ek_dir'] = opts['h2ek_maps']              # --h2ek-maps; without it H2CONV_H2EK_MAPS / settings.json "h2ek_maps_dir", else no H2EK tier
    env = PL.RevEnv(vista_maps, use_h2ek=opts.get('use_h2ek_donors', True), use_stock_locals=opts.get('use_stock_locals', True), **env_kw)
    plan_opts = {k: v for k, v in opts.get('plan_opts', {}).items()}
    log('input  : %s (%s, %d tag slots, %d strings)' % (os.path.basename(src_path), vista.header.scenario_type_name, vista.tag_count,
                                                      vista.header.string_table_count))
    plan = make_plan(vista, env, plan_opts, log)
    report['plan'] = plan.summary() if hasattr(plan, 'summary') else {}
    strs = make_strings()
    mcc_refs = X.MccRefs([mcc_maps] if mcc_maps else [])
    ctx = X.RevCtx(vista, env, mcc_refs, plan, strs, opts=opts, map_type=vista.header.type)
    ctx.extra['out_path'] = out_path
    ctx.extra['converted'] = {}
    water_chain_warnings(ctx, plan)
    sky_pin_warnings(ctx, plan)
    tick('open+plan', t0)
    log('plan   : %s' % (report['plan'],))

    # ---- handlers -----------------------------------------------------------------------------------------
    t0 = time.time()
    if not opts.get('no_handlers'):
        for nm, why in H.load_all():
            ctx.warn('handler_import_failed', '%s: %s' % (nm, why))
            log('  handler module %s failed to import: %s' % (nm, why))
        for _p, nm, fn in H.PRE:
            try:
                fn(ctx)
            except Exception as ex:                                    # noqa: BLE001
                ctx.warn('pre_pass_failed', '%s: %r' % (nm, ex))
                log('  pre_pass %s FAILED: %r' % (nm, ex))
                if opts.get('debug'):
                    traceback.print_exc()
    tick('handlers+pre', t0)

    # ---- convert -----------------------------------------------------------------------------------------------
    t0 = time.time()
    converted = ctx.extra['converted']
    failures = []
    only = opts.get('only_groups')
    raw = DonorRaw(ctx)
    n_done = 0
    for e in plan.entries:
        if only and e.group not in only:
            continue
        try:
            if e.action == 'mcc_donor':
                root = import_donor(ctx, e, raw)
            elif e.action == 'convert':
                root = X.convert_tag(ctx, e)
            else:
                continue
        except Exception as ex:                                        # noqa: BLE001
            failures.append({'index': e.out_index, 'group': e.group, 'name': e.name, 'action': e.action, 'error': repr(ex)[:300]})
            ctx.warn('convert_failed', '%s %s: %r' % (e.group, e.name, ex), tag=e.name)
            if opts.get('debug'):
                traceback.print_exc()
            root = None
        if root is None:
            try:
                root = X.empty_tag_node(ctx, e.group)
                ctx.stats['empty_substitutes'] += 1
            except Exception:                                          # noqa: BLE001
                root = None
        converted[e.out_index] = root
        n_done += 1
        if n_done % 1000 == 0:
            log('  converted %d/%d tags (%.0fs)' % (n_done, len(plan.entries), time.time() - t0))
    raw.close()
    tick('convert', t0)
    log('convert: %d tags, %d failed, %d raw payloads (%.1f MB)' % (
        len(converted), len(failures), len(ctx.raws), sum(len(d) for _k, d in ctx.raws) / 1048576.0))

    t0 = time.time()
    if not opts.get('no_handlers'):
        for _p, nm, fn in H.POST:
            try:
                fn(ctx, converted)
            except Exception as ex:                                    # noqa: BLE001
                ctx.warn('post_pass_failed', '%s: %r' % (nm, ex))
                log('  post_pass %s FAILED: %r' % (nm, ex))
                if opts.get('debug'):
                    traceback.print_exc()
    tick('post', t0)

    # ---- build the file -----------------------------------------------------------------------------------------
    t0 = time.time()
    hdr_info = build_file(ctx, plan, strs, converted, vista, out_path, opts, log)
    tick('build', t0)
    report.update(hdr_info)

    # ---- verify -----------------------------------------------------------------------------------------------------
    t0 = time.time()
    MW = hdr_info.pop('_writer_module', None)
    if MW is not None and hasattr(MW, 'validate_file'):
        try:
            report['validate_file'] = MW.validate_file(out_path)
        except Exception as ex:                                        # noqa: BLE001
            report['validate_file'] = ['validate_file failed: %r' % (ex,)]
    else:
        report['validate_file'] = []
    log('validate_file: %s' % ('OK (0 problems)' if not report['validate_file'] else '%d PROBLEMS: %s' % (
        len(report['validate_file']), report['validate_file'][:5])))
    if opts.get('no_verify'):
        report['verify'] = 'skipped (--no-verify)'
    else:
        try:
            vr = verify_output(out_path, ctx.lay_mcc)
            report['verify'] = vr
            log('verify : %d tags walked, %d blocks, %d datarefs, %d tagrefs (%d null), %d string ids; problems: %s' % (
                vr['counts'].get('tags', 0), vr['counts'].get('blocks', 0), vr['counts'].get('datarefs', 0), vr['counts'].get('tagrefs', 0),
                vr['counts'].get('tagrefs_null', 0), vr['counts'].get('stringids', 0), vr['problems_by_kind'] or 'none'))
        except Exception as ex:                                        # noqa: BLE001
            report['verify'] = 'verify_output failed: %r' % (ex,)
            log('verify_output failed: %r' % (ex,))
    report['sha256'] = sha256_file(out_path)
    tick('verify+sha256', t0)

    # ---- shaders: what was ported, what is UNPORTABLE, and an independent check of the written file -----------------
    t0 = time.time()
    report['shader_port'] = shader_port_report(plan, ctx)
    if opts.get('no_shader_check') or opts.get('only_groups'):
        report['shader_check'] = 'skipped'
    else:
        try:
            from . import shader_check as SCK
            sc = SCK.check_output(out_path, vista, plan, ctx)
            report['shader_check'] = sc
            for line in SCK.summary_lines(sc):
                log(line)
        except Exception as ex:                                        # noqa: BLE001
            report['shader_check'] = 'shader check failed: %r' % (ex,)
            log('shader check failed: %r' % (ex,))
            if opts.get('debug'):
                traceback.print_exc()
    tick('shader_check', t0)

    timings['total'] = round(time.time() - t_start, 2)
    report['timings_s'] = timings
    report['peak_rss_mb'] = round(peak_rss_mb(), 1)
    report['failures'] = failures
    report['stats'] = {k: v for k, v in sorted(ctx.stats.items()) if not k.startswith(('resize:', 'warn:'))}
    report['resize_fields'] = {k[7:]: v for k, v in sorted(ctx.stats.items()) if k.startswith('resize:')}
    report['warnings'] = {code: {'count': ctx.stats.get('warn:' + code, len(lst)),
                                 'examples': ['%s%s' % (m, (' [%s]' % t) if t else '') for m, t in lst[:8]]}
                          for code, lst in ctx.warnings.items()}
    if hasattr(plan, 'report'):
        try:
            report['plan_report'] = plan.report(30)
        except Exception as ex:                                        # noqa: BLE001
            report['plan_report'] = 'plan report failed: %r' % (ex,)
    report['out_size'] = os.path.getsize(out_path)
    if report_path:
        with open(report_path, 'w', encoding='utf-8') as fh:
            json.dump(_paths.report_for_file(report), fh, indent=1, default=str)
    print_report(report, log)
    return report, ctx


def water_chain_warnings(ctx, plan):
    """One report warning per finding of the private water chain; the rows are in report['shader_port']['water_chain'].
    reflection_bitmap_type_mismatch   the reflection slot of a water shader holds a bitmap of the other dimension than the sampler of the pass it is on (a 2D bitmap in the Vista
                                      cube sampler: kept Vista-literal, undefined on D3D9 / D3D11; or a cube in MCC's 2D sampler under water_chain=mcc)
    water_chain_no_water_definition   a BSP uses a water-chain shader but has no Water Definition (the engine then never writes the water pixel-shader constants c0..c2)
    water_chain_fallback              a Vista water chain could not be ported and MCC's chain of that name is used"""
    wc = ((plan.notes.get('shader_port') or {}).get('water_chain')) or {}
    for r in wc.get('reflection_bitmap_type_mismatch') or ():
        ctx.warn('reflection_bitmap_type_mismatch', '%s (%s): reflection bitmap %s is %s [%s]' % (r['shader'], r['chain'], r['bitmap'] or '-', r['bitmap_type'], r['kind']), tag=r['shader'])
    for r in wc.get('no_water_definition') or ():
        ctx.warn('water_chain_no_water_definition', 'BSP %s uses water-chain shaders but has no Water Definition' % r['sbsp'], tag=r['sbsp'])
    for r in wc.get('fallbacks') or ():
        ctx.warn('water_chain_fallback', '%s: %s' % (r.get('name'), r.get('detail')), tag=r.get('name'))


def sky_pin_warnings(ctx, plan):
    """One report warning per sky vertex shader that kept MCC's donor (real depth) although the option sky_depth is 'vista'; the rows are in
    report['shader_port']['sky_pin'].
    sky_pin_fallback   the Vista tag could not be ported with the far-plane pin in every classification, or the input carries no such tag"""
    sp = ((plan.notes.get('shader_port') or {}).get('sky_pin')) or {}
    for r in sp.get('fallbacks') or ():
        ctx.warn('sky_pin_fallback', '%s: %s' % (r.get('name'), r.get('detail')), tag=r.get('name'))


def shader_port_report(plan, ctx):
    """The shader section of the conversion report: the faithful port (what was ported, what is UNPORTABLE and why, which shaders therefore point
    at shaders\\invalid, flags that need the engine to verify, NON-FAITHFUL opt-ins used), the shad handler's counters and the shaders it flagged."""
    notes = dict(plan.notes.get('shader_port') or {})
    st = ctx.stats
    notes['built'] = {g: st.get('port_built:' + g, 0) for g in ('stem', 'spas', 'vrtx')}
    notes['built_facts'] = ctx.extra.get('shader_port', {})
    notes['shad_counters'] = {k: v for k, v in sorted(st.items()) if k.startswith('rev_shad_')}
    rep = ctx.extra.get('rev_shad_report') or {}
    notes['shaders_converted'] = len(rep)
    notes['shaders_exact'] = sum(1 for r in rep.values() if r.get('exact'))
    notes['shaders_flagged'] = sorted(n for n, r in rep.items() if not r.get('exact'))[:200]
    notes['shader_warnings'] = {code: ctx.stats.get('warn:' + code, 0) for code in ctx.warnings if code.startswith(('rev_shad', 'shader_port', 'shader_tags'))}
    return notes


def build_file(ctx, plan, strs, converted, vista, out_path, opts, log):
    """Place raws, strings, names, locale, bitmaps and the tag region; write the file; return header facts."""
    MW, wlabel = load_writer(not opts.get('mini_writer'))
    B = builder_class(MW)
    map_type = vista.header.type
    hv = plan.header_values() if hasattr(plan, 'header_values') else {}
    scen_path = vista.header.scenario_path or ''
    # internal name: --name, else the Vista map's own (like the MCC tool, which takes it from the scenario; the stock folder holds mod maps whose
    # file name differs from it: triplicateBarriersOFF.map has name 'triplicate'), else the file stem.  The OUTPUT FILE STEM defaults to the
    # input file stem so that e.g. HeadlongRetexured.map does not collide with headlong.map in one mod package.
    name = (opts.get('name') or vista.header.name or os.path.splitext(os.path.basename(out_path))[0])[:31]
    hdr = MW.new_header(name, scen_path, map_type)
    # the stock MCC shared.map the group table is taken from: the --mcc-maps folder's, else the decompression cache's, else the installed game's
    groups_src = next((p for p in (os.path.join(opts['mcc_maps_dir'], 'shared.map') if opts.get('mcc_maps_dir') else None,
                                   os.path.join(X.MCC_DECOMP, 'shared.map'),
                                   os.path.join(_paths.mcc_maps_dir(), 'shared.map')) if p and os.path.isfile(p)),
                      os.path.join(_paths.mcc_maps_dir(), 'shared.map'))
    groups = MW.groups_from_stock(groups_src)
    mask = hv.get('meta_offset_mask', 0)
    b = B(hdr, groups, mask)
    gd = hv.get('sound_gestalt_datum', NULL)
    si = getattr(ctx, 'sound_info', None)
    if si is not None and 'header_gestalt_datum' in si:
        gd = si['header_gestalt_datum']
    if hasattr(hdr, 'gestalt_datum'):
        hdr.gestalt_datum = gd
    else:
        hdr.set_u32(0x2EC, gd)

    # -- raw sounds + geometry
    ptr = [None] * len(ctx.raws)
    for kind in ('sound', 'geometry'):
        for i, (k, data) in enumerate(ctx.raws):
            if k != kind:
                continue
            ptr[i] = b.add_sound(data) if kind == 'sound' else b.add_geometry(data)
    # -- strings + tag names (final: all handlers ran).  Locale blobs are laid out after the tables, so the locale hook runs here.
    b.set_strings(strs.strings)
    b.set_tag_names(plan.tag_names())
    loc_mod = sys.modules.get('h2conv.rev.handlers.locale')
    if loc_mod is not None and hasattr(loc_mod, 'apply_to_builder'):
        loc_mod.apply_to_builder(ctx, b)
        ctx.stats['locale_mode:handler'] += 1
    else:
        ctx.stats['locale_mode:none'] += 1
    # -- bitmaps
    for i, (k, data) in enumerate(ctx.raws):
        if k == 'bitmap':
            ptr[i] = b.add_bitmap(data)
    for i, p in enumerate(ptr):
        if p is None:
            raise RuntimeError('raw payload %d (%s) was never placed' % (i, ctx.raws[i][0]))
        if p >= 1 << 32:
            raise RuntimeError('raw offset 0x%X does not fit in 32 bits' % p)
    ctx._resolved = ptr
    # -- tag region
    tag_count = plan.tag_count
    inst = [None] * tag_count
    cursor = b.tag_region_data_offset(tag_count)
    chunks = []
    enc_fail = 0
    for e in sorted(plan.entries, key=lambda x: x.out_index):
        root = converted.get(e.out_index)
        data = b''
        if root is not None:
            try:
                data = T.encode_tag(root, ctx.lay_mcc[e.group], mask + cursor, ctx.align, resolve=ctx.resolve)
            except Exception as ex:                                    # noqa: BLE001
                enc_fail += 1
                ctx.warn('encode_failed', '%s %s: %r' % (e.group, e.name, ex), tag=e.name)
                if opts.get('debug'):
                    traceback.print_exc()
        if len(data) % 4:
            data += b'\0' * (4 - len(data) % 4)
        inst[e.out_index] = MW.MccTagInstance(e.out_index, e.group, e.out_datum, (mask + cursor) if data else 0, len(data), e.name)
        chunks.append(data)
        cursor += len(data)
    for i, t in enumerate(inst):
        if t is None:
            inst[i] = MW.MccTagInstance.null(i)
    tag_data = b''.join(chunks)
    scn, glb = plan.scenario_datum, plan.globals_datum
    if scn == NULL:
        raise RuntimeError('the plan has no scenario tag')
    b.set_tags(inst, tag_data, scn, glb)
    img = b.build_image()
    tmp = _paths.reserve_temp(out_path)           # a NEW temp name beside the output: a file that was already there is never replaced or deleted
    try:
        if hasattr(MW, 'write_image'):
            MW.write_image(img, tmp)
        else:
            b.write(tmp)
        with open(tmp, 'rb+') as fh:
            fh.flush()
            os.fsync(fh.fileno())
        if not opts.get('overwrite', True) and os.path.exists(out_path):      # the output appeared while we were building: do not replace it
            raise FileExistsError('the output file %s already exists; pass --overwrite to replace it' % out_path)
        try:
            os.replace(tmp, out_path)
        except PermissionError as ex:
            raise PermissionError('cannot replace %s: %s' % (out_path, ex))
    except BaseException:
        _paths.discard_temp(tmp)
        raise
    return {'name': name, 'scenario_path': scen_path, 'type': map_type, 'writer': wlabel, 'tag_count': tag_count,
            'tag_data_bytes': len(tag_data), 'strings': len(strs.strings),
            'raw_sounds': sum(1 for k, _d in ctx.raws if k == 'sound'),
            'raw_geometry': sum(1 for k, _d in ctx.raws if k == 'geometry'),
            'raw_bitmaps': sum(1 for k, _d in ctx.raws if k == 'bitmap'),
            'encode_failures': enc_fail, 'scenario_datum': hex(scn), 'globals_datum': hex(glb),
            '_writer_module': MW}


def print_report(rep, log):
    log('--- reverse conversion report ---')
    for k in ('output', 'name', 'scenario_path', 'type', 'writer', 'tag_count', 'tag_data_bytes', 'strings', 'raw_sounds',
              'raw_geometry', 'raw_bitmaps', 'out_size', 'sha256', 'peak_rss_mb'):
        if k in rep:
            log('  %-16s %s' % (k, rep[k]))
    log('  timings (s)      %s' % dict(rep['timings_s']))
    if rep['failures']:
        log('  FAILED tags: %d (first: %s)' % (len(rep['failures']), rep['failures'][0]))
    sp = rep.get('shader_port')
    if isinstance(sp, dict):
        pt = sp.get('ported') or {}
        log('  shader port      ported stems/passes/vertex shaders: %d/%d/%d; UNPORTABLE tags %d, UNPORTABLE shaders -> shaders\\invalid %d; shaders converted %d, exact %d, flagged %d; '
            'non-faithful opt-ins used: %d' % (len(pt.get('stem', ())), len(pt.get('spas', ())), len(pt.get('vrtx', ())), len(sp.get('unportable') or ()),
                                              len(sp.get('unportable_shaders') or ()), sp.get('shaders_converted', 0), sp.get('shaders_exact', 0),
                                              len(sp.get('shaders_flagged') or ()), len(sp.get('non_faithful') or ())))
        for u in (sp.get('unportable') or ())[:10]:
            log('    UNPORTABLE %s %s: %s' % (u['group'], u['name'], u['reason'][:200]))
        for f in (sp.get('flags') or ())[:10]:
            log('    FLAG %s' % (f[:220],))
        wc = sp.get('water_chain') or {}
        if wc.get('chains') or wc.get('donor_chain_shaders'):
            log('  water chain      mode %s: %d private Vista chain(s) carrying %d shader(s); %d shader(s) on MCC\'s donor chain; reflection bitmap type mismatch %d; BSPs without a Water Definition %d; '
                'fallbacks %d' % (wc.get('mode'), sum(1 for c in wc.get('chains') or () if c.get('decision') == 'port'), sum(c.get('shaders', 0) for c in wc.get('chains') or ()),
                                  len(wc.get('donor_chain_shaders') or ()), len(wc.get('reflection_bitmap_type_mismatch') or ()), len(wc.get('no_water_definition') or ()),
                                  len(wc.get('fallbacks') or ())))
    st = rep['stats']
    gen = {k: v for k, v in st.items() if k.startswith(('generic:', 'tag_handler:', 'donor:', 'donor_handler:'))}
    log('  tags by path     %s' % gen)
    log('  other stats      %s' % {k: v for k, v in st.items() if k not in gen})
    log('  warnings by code:')
    for code, w in rep['warnings'].items():
        log('    %-24s %6d   e.g. %s' % (code, w['count'], w['examples'][0] if w['examples'] else ''))


def report_ok(rep):
    if rep.get('validate_file') or rep.get('failures') or rep.get('encode_failures'):
        return False
    v = rep.get('verify')
    if isinstance(v, dict) and not v.get('ok'):
        return False
    sc = rep.get('shader_check')
    if isinstance(sc, dict) and not sc.get('ok'):
        return False
    return True


def main(argv=None):
    import argparse
    from .. import __version__
    ap = argparse.ArgumentParser(
        prog='h2conv.rev.convert', formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Convert a Halo 2 Vista map (cache v8) into a self-contained Halo 2 MCC map (cache v13, H2EK style).',
        epilog='example:  py -3 -m h2conv.rev.convert "...\\maps\\headlong.map" headlong_from_vista.map')
    ap.add_argument('input', help='Vista .map')
    ap.add_argument('output', help='output .map path, or a folder (then the file is <name>.map)')
    ap.add_argument('--name', default=None, help='header internal name AND output file stem (<32 chars)')
    _paths.add_common_options(ap, mcc=True, vista=True, assembly=True, h2ek=True)
    ap.add_argument('--no-h2ek-donors', action='store_true', help='do not use the H2EK maps as donors even when H2CONV_H2EK_MAPS / the settings file names a folder')
    ap.add_argument('--overwrite', action='store_true', help='replace the output file (and the --report file) when it already exists; by default an existing file is never touched')
    ap.add_argument('--version', action='version', version='Halo 2 Map Converter %s' % __version__)
    ap.add_argument('--report', default=None, help='write the conversion report (JSON; the input and output are recorded as file names)')
    ap.add_argument('--no-verify', action='store_true', help='skip the plugin walk of the written file')
    ap.add_argument('--no-shader-check', action='store_true', help='skip the independent check of the written shaders')
    ap.add_argument('--no-donor-raw', action='store_true', help='do not copy the bitmap / geometry blobs of MCC donor tags')
    ap.add_argument('--force-output-dir', action='store_true', help='allow writing into the stock map folders / the input folder')
    ap.add_argument('--mini-writer', action='store_true', help='use h2conv/rev/mini_writer.py even if h2conv/mcc_writer.py exists')
    ap.add_argument('--plan-opt', action='append', default=[], metavar='KEY=VALUE', help='planner option, repeatable')
    ap.add_argument('--quiet', action='store_true')
    dev = ap.add_argument_group('developer switches')
    dev.add_argument('--no-handlers', action='store_true', help='generic conversion only (skip every registered handler)')
    dev.add_argument('--no-tag-handlers', action='store_true', help='skip tag handlers (node handlers still run)')
    dev.add_argument('--limit-groups', default=None, help='comma separated groups to convert (others become empty slots)')
    dev.add_argument('--strict', action='store_true', help='re-raise handler errors instead of falling back')
    dev.add_argument('--debug', action='store_true')
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:                                              # noqa: BLE001
            pass
    _paths.configure_from_args(a)
    popts = {}
    for kv in a.plan_opt:
        k, _, v = kv.partition('=')
        popts[k.strip()] = {'true': True, 'false': False}.get(v.strip().lower(), v.strip())
    opts = {'quiet': a.quiet, 'no_handlers': a.no_handlers, 'no_tag_handlers': a.no_tag_handlers or a.no_handlers,
            'no_node_handlers': a.no_handlers, 'debug': a.debug, 'strict': a.strict, 'name': a.name,
            'force_output_dir': a.force_output_dir, 'overwrite': a.overwrite, 'no_verify': a.no_verify, 'no_donor_raw': a.no_donor_raw, 'no_shader_check': a.no_shader_check,
            'mini_writer': a.mini_writer, 'plan_opts': popts}
    if a.limit_groups:
        opts['only_groups'] = set(a.limit_groups.split(','))
    if a.h2ek_maps:
        opts['h2ek_maps'] = a.h2ek_maps
    if a.no_h2ek_donors:
        opts['use_h2ek_donors'] = False
    try:
        rep, _ctx = convert_map(a.input, a.output, a.vista_maps, a.mcc_maps, a.report, opts)
    except (ValueError, PermissionError, FileNotFoundError, FileExistsError, _paths.PathsError) as ex:
        print('ERROR: %s' % ex)
        return 2
    except Exception as ex:                                          # noqa: BLE001  planner / reader errors on a broken input map
        print('ERROR: conversion failed: %r' % (ex,))
        if a.debug:
            traceback.print_exc()
        return 3
    return 0 if report_ok(rep) else 1


if __name__ == '__main__':
    sys.exit(main())
