# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Deep validator + comparer for Halo 2 Vista (cache v8) maps.

    validate_map(path, ...)  -> Report     structural + semantic validation of one Vista map (0 errors on all 43 stock maps)
    compare_maps(out, ref)   -> CompareReport   field-level diff of two Vista maps matched by (group, name)
    CLI:  py -3 -m h2conv.validate <map> [<map> ...] [--ref <vista map>] [--json out.json] [--all] [--jobs N]

What is checked (Report issue levels: error / warning / note; `flags` mark rules the engine or Cartographer ignore):
  header        cache_header_verify rules + build string, file_size, 0x2F0 XOR checksum, region layout / alignment, limits,
                reserved fields, mopp checksum, shared-database flags, custom-map-list rules ('cartographer-ignored')
  tables        group table, string table (ids < count, length byte == strlen, shared prefix), tag-name tables, instance
                table (datum salt/index, empties, shared placeholders vs shared.map, tag tiling, scnr/matg datums)
  tags          every local tag walked with the corrected Vista plugin layouts (numpy, level-by-level): every
                tagblock/dataref inside the tag region and in the tag's own extent or fully inside one earlier tag
                (legal de-duplicated share); counts sane; AlignTable alignment; intervals tile the tag exactly;
                tagrefs resolve to a live instance whose group is the stored group or a child group; string ids
  raw           bitmap lod1 pointer -> zlib 78 DA stream inflating to the exact size; geometry 'blkh' blocks; sound
                chunks; shared-database location bits (MP maps: mainmenu/shared only, never single_player_shared)
  sound         ugh! pitch range / permutation / chunk consistency, snd! indices
  data          NaN/Inf floats, enum values / flag bits outside the plugin options and the values seen in stock maps,
                the shared-tag dependency closure of an MP map (placeholders for everything a local tag needs)
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL
except Exception:
    pass

import collections
import hashlib
import json
import os
import struct
import sys
import time
import zlib

import numpy as np

from . import paths as _paths
from . import plugins as P
from . import tagtree as T
from .vista_cache import (COMPATIBLE_BUILDS, FIRST_SHARED_TAG_INSTANCE_INDEX, HEADER_SIZE, VistaCache, VistaHeader,
                          align_up, fourcc_str, mopp_checksum_of, xor32)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_maps_dir():
    """The stock Vista maps folder (--maps-dir / H2CONV_VISTA_MAPS (or the old H2V_MAPS) / settings.json / a search of your drives)."""
    return _paths.vista_maps_dir()


def __getattr__(name):                  # the former module constant, for the developer tools that still read it
    if name == 'DEFAULT_MAPS_DIR':
        return default_maps_dir()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


NULL = 0xFFFFFFFF
PAGE = 0x200
MAX_STRING_IDS = 24576                 # k_cache_file_max_string_ids
MAX_STRING_BYTES = 0x60000             # k_maximum_string_id_storage
MAX_NAME_BUFFER = 5120000              # k_cache_file_debug_name_buffer_size
MAX_NAME_INDEX = 20096                 # debug_tag_name_offsets[20096]
MAX_INSTANCES = 20000
CUSTOM_LIST_MIN, CUSTOM_LIST_MAX = 0xC00, 0xFA00000      # X 0x44F77F file-size window
SHARED_TAG_SIZE_MIN_MASK = 0xEAA000    # shared.map tag_size: tag_offset_mask must be >= this for MP maps
DB_FILES = {1: 'mainmenu.map', 2: 'shared.map', 3: 'single_player_shared.map'}     # raw pointer location bits
SELF_LOC = {3: 2}                      # map type -> location value that means "this very file" (shared.map uses 2 for itself)
WMA_SIGNATURE = bytes.fromhex('6175647300001000800000aa00389b71')      # 'auds' + MEDIASUBTYPE base GUID: first 16 bytes of 480/480 stock WMA permutations
UGH_BLOCKS = [('playbacks', 0x00, 0x38), ('scales', 0x08, 0x14), ('import_names', 0x10, 4), ('pitch_range_params', 0x18, 0x0A),
              ('pitch_ranges', 0x20, 0x0C), ('permutations', 0x28, 0x10), ('custom_playbacks', 0x30, 0x34),
              ('runtime_perm_flags', 0x38, 1), ('chunks', 0x40, 0x0C), ('promotions', 0x48, 0x1C), ('extra_info', 0x50, 0x2C)]

BITS_PER_PIXEL = {0: 8, 1: 8, 2: 8, 3: 16, 6: 16, 8: 16, 9: 16, 10: 32, 11: 32, 14: 4, 15: 8, 16: 8,
                  17: 8, 18: 8, 19: 128, 20: 96, 21: 48, 22: 16, 23: 16}      # engine k_bits_per_pixel (h2conv/bitmaps.py)


def bitmap_raw_size(width, height, depth, fmt, mip_count, bitmap_type):
    """Decompressed byte size of one Vista bitmap resource (what the engine's uncompress() must produce)."""
    bpp = BITS_PER_PIXEL.get(fmt)
    if bpp is None:
        return None
    total = 0
    for lvl in range(max(0, mip_count) + 1):
        w, h, d = max(1, width >> lvl), max(1, height >> lvl), max(1, depth >> lvl)
        if fmt in (14, 15, 16):
            w += -w & 3
            h += -h & 3
        total += w * h * d * bpp // 8
    return total * (6 if bitmap_type == 2 else 1)


# ------------------------------------------------------------------------------------------------------------------
# report
# ------------------------------------------------------------------------------------------------------------------
class Report(object):
    """Result of validate_map.  `counts[(level, code)]` = total occurrences, `examples[code]` = first few Issue dicts."""

    def __init__(self, path, max_examples=25):
        self.path = path
        self.counts = collections.Counter()
        self.examples = collections.defaultdict(list)
        self.level_of = {}
        self.stats = collections.Counter()
        self.info = {}
        self.max_examples = max_examples
        self.seconds = 0.0
        self._tagname = None

    def add(self, level, code, msg, tag=None, path='', n=1, flags=()):
        self.counts[(level, code)] += n
        self.level_of[code] = level
        ex = self.examples[code]
        if len(ex) < self.max_examples:
            d = {'level': level, 'code': code, 'msg': msg}
            if tag is not None:
                d['tag'] = tag.index
                d['group'] = tag.group
                d['name'] = tag.name
            if path:
                d['path'] = path
            if flags:
                d['flags'] = list(flags)
            ex.append(d)

    def error(self, code, msg, **kw):
        self.add('error', code, msg, **kw)

    def warn(self, code, msg, **kw):
        self.add('warning', code, msg, **kw)

    def note(self, code, msg, **kw):
        self.add('note', code, msg, **kw)

    def total(self, level):
        return sum(n for (lv, _c), n in self.counts.items() if lv == level)

    @property
    def errors(self):
        return self.total('error')

    @property
    def warnings(self):
        return self.total('warning')

    @property
    def notes(self):
        return self.total('note')

    @property
    def ok(self):
        return self.errors == 0

    def issues(self, level=None):
        out = []
        for code, lst in self.examples.items():
            for d in lst:
                if level is None or d['level'] == level:
                    out.append(d)
        return out

    def to_dict(self):
        codes = {}
        for (lv, code), n in sorted(self.counts.items()):
            codes[code] = {'level': lv, 'count': n, 'examples': self.examples.get(code, [])}
        return {'map': self.path, 'ok': self.ok, 'errors': self.errors, 'warnings': self.warnings, 'notes': self.notes,
                'seconds': round(self.seconds, 2), 'info': self.info, 'stats': dict(self.stats), 'codes': codes}

    def format_text(self, examples=3, levels=('error', 'warning', 'note')):
        L = []
        L.append('%s  [%s]  type=%s  %d error(s), %d warning(s), %d note(s)  (%.1fs)' % (
            os.path.basename(self.path), 'OK' if self.ok else 'FAIL', self.info.get('type_name', '?'), self.errors,
            self.warnings, self.notes, self.seconds))
        for lv in levels:
            rows = sorted(((n, code) for (l2, code), n in self.counts.items() if l2 == lv), reverse=True)
            for n, code in rows:
                ex = self.examples.get(code, [])
                fl = ''
                if ex and ex[0].get('flags'):
                    fl = ' {' + ','.join(ex[0]['flags']) + '}'
                L.append('  %-7s %-34s x%d%s' % (lv.upper(), code, n, fl))
                for d in ex[:examples]:
                    where = ''
                    if 'tag' in d:
                        where = ' [tag %d %s %s]' % (d['tag'], d.get('group', ''), d.get('name') or '')
                    L.append('      - %s%s%s' % (d['msg'], where, (' @' + d['path']) if d.get('path') else ''))
        return '\n'.join(L)


# ------------------------------------------------------------------------------------------------------------------
# shared, lazily built data: layouts, walk plans, alignment table
# ------------------------------------------------------------------------------------------------------------------
_ALIGN = None
_LAYOUTS = None
_PLANS = None


def _align_table():
    global _ALIGN
    if _ALIGN is None:
        d = json.load(open(os.path.join(_paths.data_dir(), 'block_alignment_vista.json'), 'r', encoding='utf-8'))
        _ALIGN = {}
        for k, v in d.items():
            if isinstance(v, dict):
                _ALIGN[k] = (int(v.get('align') or 4), int(v.get('n') or 0))
            else:
                _ALIGN[k] = (int(v or 4), 0)
    return _ALIGN


def vista_layouts():
    global _LAYOUTS
    if _LAYOUTS is None:
        _LAYOUTS = P.load_layouts('vista', corrected=True)
    return _LAYOUTS


class Plan(object):
    """Pre-compiled walk plan for one struct (root or tagblock element)."""
    __slots__ = ('group', 'path', 'es', 'b_off', 'b_child', 'b_name', 'b_ces', 'b_A', 'b_An', 'd_off', 'd_name', 'd_A', 'd_An',
                 'r_off', 'r_name', 't_off', 't_name', 's_off', 's_name', 'geo', 'hook', 'needs_rows', 'fields',
                 '_bidx', '_didx', '_ridx', '_tidx', '_sidx', 'f_off', 'f_name', 'f_lanes', '_fidx', '_flane', 'has_floats', 'e_fields', 'e_names', '_enum')

    def __init__(self, group, path, es):
        self.group, self.path, self.es = group, path, es
        self.b_off, self.b_child, self.b_name, self.b_ces, self.b_A, self.b_An = [], [], [], [], [], []
        self.d_off, self.d_name, self.d_A, self.d_An = [], [], [], []
        self.r_off, self.r_name, self.t_off, self.t_name, self.s_off, self.s_name = [], [], [], [], [], []
        self.geo = []
        self.hook = None
        self.needs_rows = False
        self.fields = None
        self.f_off, self.f_name, self.f_lanes = [], [], []
        self.has_floats = False
        self.e_fields, self.e_names = [], []
        self._enum = None

    def finish(self):
        def idx(offs, width):
            if not offs:
                return None
            return (np.asarray(offs, dtype=np.int64)[:, None] + np.arange(width)[None, :]).ravel()
        self._bidx, self._didx = idx(self.b_off, 8), idx(self.d_off, 8)
        self._ridx, self._tidx, self._sidx = idx(self.r_off, 8), idx(self.t_off, 4), idx(self.s_off, 4)
        self.b_ces = np.asarray(self.b_ces, dtype=np.int64)
        self.b_A = np.asarray(self.b_A, dtype=np.int64)
        self.b_An = np.asarray(self.b_An, dtype=np.int64)
        self.d_A = np.asarray(self.d_A, dtype=np.int64)
        self.d_An = np.asarray(self.d_An, dtype=np.int64)
        self.has_floats = bool(self.f_off)
        self._fidx = self._flane = None
        if self.f_off:
            self._fidx = np.concatenate([np.arange(o, o + 4 * n) for o, n in zip(self.f_off, self.f_lanes)])
            self._flane = np.concatenate([np.full(n, i, dtype=np.int64) for i, n in enumerate(self.f_lanes)])
        if self.e_fields:
            by_size = collections.OrderedDict()
            for i, e in enumerate(self.e_fields):
                by_size.setdefault(e['size'], []).append(i)
            self._enum = []
            for size, lst in by_size.items():
                idx = (np.asarray([self.e_fields[i]['off'] for i in lst], dtype=np.int64)[:, None] + np.arange(size)[None, :]).ravel()
                is_fl = np.asarray([self.e_fields[i]['flags'] for i in lst], dtype=bool)
                masks = np.zeros(len(lst), dtype=np.int64)
                keys = []
                for j, i in enumerate(lst):
                    e = self.e_fields[i]
                    if e['flags']:
                        masks[j] = e['allowed']
                    else:
                        keys.extend((j << 32) | (v & 0xFFFFFFFF) for v in e['allowed'])
                self._enum.append({'size': size, 'idx': idx, 'fidx': np.asarray(lst, dtype=np.int64), 'is_fl': is_fl, 'masks': masks,
                                   'keys': np.unique(np.asarray(keys, dtype=np.int64)), 'allones': (1 << (8 * size)) - 1})
        self.needs_rows = bool(self.b_off or self.d_off or self.r_off or self.t_off or self.s_off or self.geo or self.hook)


PATH_HOOKS = {('bitm', '/Bitmaps'): 'bitmaps'}
_ENUM_SIZES = {'enum8': 1, 'enum16': 2, 'enum32': 4, 'flags8': 1, 'flags16': 2, 'flags32': 4}
ENUM_SEEN_PATH = os.path.join(_paths.work_dir(), 'validate_enum_seen.json')
_ENUM_SEEN = None


_BUILTIN_ENUM_SEEN = {   # from `--learn-enums` over the 43 stock maps (validate_enum_seen.json in the work folder extends/overrides it)
    'bitm|/Bitmaps|Low-Detail Format@68': {'v': [32, 64, 128, 256, 512, 1024, 17021, 35664, 48232, 52384, 64760, 64984]},
    'effe||Flags@0': {'m': 62},
    'hlmt|/Variants/Regions/Permutations|Flags@5': {'m': 2},
    'ltmp|/Lightmap Groups|Flags@2': {'m': 2},
    'phmo|/Boxes|Flags@6': {'m': 254},
    'phmo|/Nodes|Flags@4': {'m': 4},
    'phmo|/Pills|Flags@6': {'m': 254},
    'phmo|/Polyhedra|Flags@6': {'m': 254},
    'phmo|/Spheres|Flags@6': {'m': 126},
    'prt3||Flags@0': {'m': 65536},
    'snd!||Flags@0': {'m': 15360},
    'vehi|/Phantom Shapes|Shape Type 0@18': {'v': [4640, 7984, 8944, 18752, 21840, 29872, 32080, 37552, 38928, 51504, 62048, 64896]},
    'vehi|/Phantom Shapes|Shape Type 1@20': {'v': [32224, 37696, 39072, 51648, 62192]},
}


def _learned_enums():
    """Enum values / flag bits that occur in genuine Vista maps although the Assembly plugin does not name them
    (validate_enum_seen.json in the work folder, built by `py -3 -m h2conv.validate --learn-enums` from the stock maps)."""
    global _ENUM_SEEN
    if _ENUM_SEEN is None:
        _ENUM_SEEN = {k: dict(v) for k, v in _BUILTIN_ENUM_SEEN.items()}
        try:
            for k, v in json.load(open(ENUM_SEEN_PATH, 'r', encoding='utf-8')).items():
                cur = _ENUM_SEEN.setdefault(k, {})
                if 'm' in v:
                    cur['m'] = cur.get('m', 0) | v['m']
                if 'v' in v:
                    cur['v'] = sorted(set(cur.get('v', ())) | set(v['v']))
        except Exception:
            pass
    return _ENUM_SEEN


_FLOAT_LANES = {'float32': 1, 'degree': 1, 'point2': 2, 'point3': 3, 'vector2': 2, 'vector3': 3, 'vector4': 4, 'degree2': 2,
                'degree3': 3, 'plane2': 3, 'plane3': 4, 'rangef': 2, 'colorf': 4}     # float32 lanes per plugin field type


def _build_plan(group, fields, es, path, align):
    p = Plan(group, path, es)
    p.hook = PATH_HOOKS.get((group, path))
    p.fields = fields
    for f in sorted(fields, key=lambda x: x.offset):
        t = f.type
        if t == 'tagblock':
            ces = T.element_size(f) or 0
            cp = path + '/' + f.name
            A, An = align.get('%s|%s' % (group, cp), (4, 0))
            p.b_off.append(f.offset)
            p.b_name.append(f.name)
            p.b_child.append(_build_plan(group, f.children or [], ces, cp, align))
            p.b_ces.append(ces)
            p.b_A.append(A)
            p.b_An.append(An)
        elif t == 'dataref':
            cp = path + '/' + f.name
            A, An = align.get('%s|%s [data]' % (group, cp), (4, 0))
            p.d_off.append(f.offset)
            p.d_name.append(f.name)
            p.d_A.append(A)
            p.d_An.append(An)
        elif t == 'tagref':
            if f.attrs.get('withGroup', True):
                p.r_off.append(f.offset)
                p.r_name.append(f.name)
            else:
                p.t_off.append(f.offset)
                p.t_name.append(f.name)
        elif t == 'stringid':
            p.s_off.append(f.offset)
            p.s_name.append(f.name)
        elif t == 'int32' and f.name == 'Resource Block Offset':
            p.geo.append(f.offset)
        if t in _ENUM_SIZES and f.options is not None:
            key = '%s|%s|%s@%x' % (group, path, f.name, f.offset)
            learned = _learned_enums().get(key)
            if t.startswith('flags'):
                allowed = 0
                for _n, bit in f.options:
                    allowed |= 1 << bit
                allowed |= int(learned.get('m', 0)) if learned else 0
            else:
                allowed = {v & ((1 << (8 * f.size)) - 1) for _n, v in f.options}
                if learned:
                    allowed |= set(int(x) for x in learned.get('v', ()))
            p.e_fields.append({'off': f.offset, 'size': f.size, 'flags': t.startswith('flags'), 'allowed': allowed, 'key': key})
            p.e_names.append(f.name)
        if t in _FLOAT_LANES:
            p.f_off.append(f.offset)
            p.f_name.append(f.name)
            p.f_lanes.append(f.size // 4)
    p.finish()
    return p


def vista_plans():
    global _PLANS
    if _PLANS is None:
        al = _align_table()
        _PLANS = {}
        for g, lay in vista_layouts().items():
            size = lay.base_size or lay.computed_size
            _PLANS[g] = _build_plan(g, lay.fields, size, '', al)
    return _PLANS


# ------------------------------------------------------------------------------------------------------------------
# numpy helpers
# ------------------------------------------------------------------------------------------------------------------
def gather_rows(buf, offs, cnts, es):
    """Concatenate `cnts[i]` elements of `es` bytes at file offset `offs[i]` -> (sum(cnts), es) uint8 array."""
    offs = np.asarray(offs, dtype=np.int64)
    lens = np.asarray(cnts, dtype=np.int64) * es
    total = int(lens.sum())
    if total == 0:
        return np.zeros((0, es), dtype=np.uint8)
    if len(offs) <= 24 or total > (24 << 20):
        parts = [buf[o:o + n] for o, n in zip(offs.tolist(), lens.tolist())]
        flat = np.concatenate(parts) if len(parts) > 1 else parts[0]
    else:
        starts = np.cumsum(lens) - lens
        flat = buf[np.repeat(offs - starts, lens) + np.arange(total, dtype=np.int64)]
    return flat.reshape(-1, es)


class Item(object):
    """A batch of struct instances of one Plan: rows (n, es) uint8, their addresses and the segments they came from."""
    __slots__ = ('plan', 'rows', 'row_addr', 'seg_start', 'seg_addr', 'path')

    def __init__(self, plan, rows, row_addr, seg_start, seg_addr):
        self.plan, self.rows, self.row_addr, self.seg_start, self.seg_addr = plan, rows, row_addr, seg_start, seg_addr
        self.path = plan.path

    def describe(self, r):
        s = int(np.searchsorted(self.seg_start, r, side='right')) - 1
        return 'element %d of block@%#x' % (r - int(self.seg_start[s]), int(self.seg_addr[s]))


# ------------------------------------------------------------------------------------------------------------------
# external shared databases (mainmenu.map / shared.map / single_player_shared.map), opened lazily
# ------------------------------------------------------------------------------------------------------------------
class ExtDB(object):
    def __init__(self, path):
        import mmap
        self.path = path
        self.exists = os.path.isfile(path)
        self.size = 0
        self.header = None
        self.buf = None
        self._mm = None
        self._f = None
        if self.exists:
            self._f = open(path, 'rb')
            self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
            self.size = len(self._mm)
            self.buf = np.frombuffer(self._mm, dtype=np.uint8)
            try:
                self.header = VistaHeader(bytes(self._mm[:HEADER_SIZE]))
            except Exception:
                self.header = None

    def close(self):
        self.buf = None
        try:
            self._mm.close()
            self._f.close()
        except Exception:
            pass


# ------------------------------------------------------------------------------------------------------------------
# one map opened for validation
# ------------------------------------------------------------------------------------------------------------------
class MapView(object):
    def __init__(self, path):
        self.path = path
        self.c = VistaCache(path)
        self.h = self.c.header
        self.size = self.c.size
        self.buf = np.frombuffer(self.c._mm, dtype=np.uint8)
        h = self.h
        self.mask, self.toff = h.tag_offset_mask, h.tag_offset
        self.ntags = self.c.tag_count
        th = self.c.tags_header_bytes
        io = self.c.instance_table_off
        self.inst = np.frombuffer(th, dtype='<u4', count=4 * self.ntags, offset=io).reshape(-1, 4)
        self.inst_group, self.inst_datum, self.inst_addr, self.inst_size = (self.inst[:, 0], self.inst[:, 1], self.inst[:, 2], self.inst[:, 3])
        gl = np.frombuffer(th, dtype='<u4', count=3 * self.c.group_count, offset=self.c.group_table_off).reshape(-1, 3)
        self.group_links = gl
        pm = {int(g[0]): (int(g[1]), int(g[2])) for g in gl}
        self.known_groups = np.asarray(sorted(pm), dtype=np.uint32)
        self.inst_parent = np.zeros(self.ntags, dtype=np.uint32)
        self.inst_grand = np.zeros(self.ntags, dtype=np.uint32)
        for g, (pa, gp) in pm.items():
            m = self.inst_group == g
            self.inst_parent[m] = pa
            self.inst_grand[m] = gp
        self.region_lo = self.mask + h.data_offset
        self.region_hi = self.mask + min(h.tag_size, max(0, self.size - self.toff))      # never beyond the end of the file
        live = np.nonzero((self.inst_addr != 0) & (self.inst_size != 0) & (self.inst_datum != NULL))[0]
        order = live[np.argsort(self.inst_addr[live].astype(np.int64), kind='stable')]
        self.ex_idx = order
        self.ex_start = self.inst_addr[order].astype(np.int64)
        self.ex_end = self.ex_start + self.inst_size[order].astype(np.int64)
        self.str_lens = None
        self.str_count = h.string_table_count
        self.tag_by_index = self.c.tags

    def fo(self, addr):
        return self.toff + addr - self.mask

    def close(self):
        self.buf = None
        self.inst = self.inst_group = self.inst_datum = self.inst_addr = self.inst_size = None
        try:
            self.c.close()
        except Exception:
            pass


def _known_group_mask(mv, g):
    return np.isin(g, mv.known_groups)


# ------------------------------------------------------------------------------------------------------------------
# the validator
# ------------------------------------------------------------------------------------------------------------------
class _ValidatorBase(object):
    """Header / table / instance checks + shared state (layouts, walk plans, external-database handles)."""

    def __init__(self, maps_dir=None, raw_decode=True, max_examples=25, walk=True, sounds=True, floats=True, enums=True, sound_data=True):
        self.maps_dir = maps_dir or default_maps_dir()
        self.check_sound_data = sound_data
        self.check_floats = floats
        self.check_enums = enums
        self._learn = None               # {field key: set(values) | flag mask} while learning enum values
        self.raw_decode = raw_decode
        self.max_examples = max_examples
        self.do_walk = walk
        self.do_sounds = sounds
        self.plans = vista_plans()
        self.layouts = vista_layouts()
        self._ext = {}
        self._zmemo = {}
        self._zself = {}
        self._shared_strings = None
        self._self_path = None
        self._collect = None             # {tag index: set(target indices)} while collecting tagref targets
        self._shared_deps = None

    # ---- external databases ---------------------------------------------------------------------
    def ext(self, name):
        if name not in self._ext:
            self._ext[name] = ExtDB(os.path.join(self.maps_dir, name))
        return self._ext[name]

    def close(self):
        for e in self._ext.values():
            e.close()
        self._ext = {}

    def shared_strings(self):
        """shared.map string list (bytes) or None."""
        if self._shared_strings is None:
            db = self.ext('shared.map')
            if not db.exists or db.header is None:
                self._shared_strings = False
            else:
                h = db.header
                tbl = bytes(db.buf[h.string_table_offset:h.string_table_offset + h.string_table_size])
                self._shared_strings = tbl.split(b'\0')[:h.string_table_count]
        return self._shared_strings or None

    def shared_deps(self):
        """{shared tag index: set(indices it references)} computed by walking shared.map once; None if unavailable."""
        if self._shared_deps is None:
            db = self.ext('shared.map')
            if not db.exists:
                self._shared_deps = False
            else:
                saved = (self.raw_decode, self.do_sounds, self._collect)
                self.raw_decode, self.do_sounds, self._collect = False, False, {}
                try:
                    self.validate(db.path)
                    self._shared_deps = self._collect
                except Exception:
                    self._shared_deps = False
                finally:
                    self.raw_decode, self.do_sounds, self._collect = saved
        return self._shared_deps or None

    def _check_shared_closure(self, R, mv, local_refs):
        """Every shared tag reachable from the local tags (through shared tags' own references) must be listed as a
        placeholder: the engine resolves shared tagrefs through THIS map's instance table."""
        deps = self.shared_deps()
        if deps is None:
            R.note('inst.shared_closure', 'shared.map not available - shared dependency closure not verified')
            return
        c = mv.c
        start = set()
        for tgts in (local_refs or {}).values():
            start.update(i for i in tgts if i >= FIRST_SHARED_TAG_INSTANCE_INDEX)
        for d in (c.globals_datum, c.scenario_datum, mv.h.secondary_ugh_tag_index):
            if d != NULL and (d & 0xFFFF) >= FIRST_SHARED_TAG_INSTANCE_INDEX:
                start.add(d & 0xFFFF)
        need = set()
        todo = list(start)
        while todo:
            i = todo.pop()
            if i in need:
                continue
            need.add(i)
            todo.extend(deps.get(i, ()))
        missing = sorted(i for i in need if i >= FIRST_SHARED_TAG_INSTANCE_INDEX and (i >= mv.ntags or mv.inst_datum[i] == NULL))
        R.stats['shared_closure_tags'] = len(need)
        if missing:
            names = []
            try:
                sc = VistaCache(self.ext('shared.map').path)
                names = ['%d %s %s' % (i, sc.tags[i].group, sc.tags[i].name) for i in missing[:4]]
                sc.close()
            except Exception:
                names = [str(i) for i in missing[:4]]
            R.error('inst.shared_closure', '%d shared tags needed (transitively) by local tags are missing from the instance table, e.g. %s' % (
                len(missing), '; '.join(names)))

    # ---- main entry -----------------------------------------------------------------------------
    def validate(self, path):
        t0 = time.time()
        R = Report(path, self.max_examples)
        try:
            mv = MapView(path)
        except Exception as e:           # not even parseable
            R.error('open.fail', 'cannot parse %s: %s: %s' % (os.path.basename(path), type(e).__name__, e))
            try:                          # header-only diagnostics so the caller still learns what is wrong with it
                with open(path, 'rb') as fh:
                    raw = fh.read(HEADER_SIZE)
                if len(raw) == HEADER_SIZE:
                    hh = VistaHeader(raw)
                    for p_ in hh.verify():
                        R.error('header.verify', 'cache_header_verify / build string: ' + p_)
                    if hh.file_size != os.path.getsize(path):
                        R.error('header.file_size', 'header file_size %#x != actual file size %#x' % (hh.file_size, os.path.getsize(path)))
            except Exception:
                pass
            R.seconds = time.time() - t0
            return R
        if mv.h.type == 1 and mv.ntags > FIRST_SHARED_TAG_INSTANCE_INDEX and self.do_walk:
            self.shared_deps()               # nested validate of shared.map: must run before this map's state is set up
        self.R, self.mv = R, mv
        self._zself = {}
        try:
            h = mv.h
            R.info.update({'name': h.name, 'type': h.type, 'type_name': h.scenario_type_name, 'size': mv.size,
                           'tag_count': mv.ntags, 'scenario_path': h.scenario_path, 'build': h.version_string})
            self._check_header(R, mv)
            self._check_regions(R, mv)
            self._check_groups(R, mv)
            self._check_strings(R, mv)
            self._check_names(R, mv)
            self._check_instances(R, mv)
            self._check_language_pack(R, mv)
            self.geo_blocks = {}
            self.bitmap_blobs = {}
            self.sound_chunks = {}
            if self.do_walk:
                collecting = mv.h.type == 1 and mv.ntags > FIRST_SHARED_TAG_INSTANCE_INDEX
                if collecting:
                    self._collect = {}
                try:
                    self._walk_all(R, mv)
                finally:
                    local_refs, self._collect = (self._collect if collecting else None), (None if collecting else self._collect)
                if collecting:
                    self._check_shared_closure(R, mv, local_refs)
            self._check_geometry_region(R, mv)
            self._check_bitmap_region(R, mv)
            if self.do_sounds:
                self._check_sounds(R, mv)
            self._check_globals(R, mv)
            if R.stats.get('raw_db_unavailable'):
                R.note('raw.db_unavailable', '%d raw pointers point into shared databases that are not available in %s - their contents were not verified' % (
                    R.stats['raw_db_unavailable'], self.maps_dir))
            self._check_custom_list(R, mv)
        except Exception as e:
            import traceback
            R.error('validator.crash', 'internal validator error: %s: %s\n%s' % (type(e).__name__, e, traceback.format_exc(limit=6)))
        finally:
            mv.close()
            self.mv = None
        R.seconds = time.time() - t0
        return R

    # ==============================================================================================
    # header
    # ==============================================================================================
    def _check_header(self, R, mv):
        h, c = mv.h, mv.c
        raw = h.raw
        for p in h.verify():
            R.error('header.verify', 'cache_header_verify / build string: ' + p)
        if h.file_size != mv.size:
            R.error('header.file_size', 'header file_size %#x != actual file size %#x' % (h.file_size, mv.size))
        if mv.size % PAGE:
            R.error('header.size_align', 'file size %#x is not a multiple of 0x200' % mv.size)
        x = xor32(c._mm, HEADER_SIZE, mv.size - ((mv.size - HEADER_SIZE) % 4))
        if x != h.checksum:
            R.error('header.checksum', 'checksum@0x2F0 = %#010x but XOR32 of [0x800, EOF) = %#010x' % (h.checksum, x))
        # reserved / constant fields (never read by the engine, all zero in the 43 stock maps)
        zero_fields = [('field_C', h.field_C), ('language', h.language), ('minor_version', h.minor_version),
                       ('pad_14E', h.pad_14E), ('field_158', h.field_158), ('tracked', h.tracked), ('field_15A', h.field_15A),
                       ('field_15B', h.field_15B), ('field_15C', h.field_15C), ('low_detail_bitmap_count', h.low_detail_bitmap_count),
                       ('low_detail_bitmap_offset', h.low_detail_bitmap_offset), ('low_detail_bitmap_size', h.low_detail_bitmap_size)]
        for nm, v in zero_fields:
            if v:
                R.warn('header.reserved', 'reserved header field %s = %#x (stock: 0)' % (nm, v), flags=('engine-ignored',))
        if any(raw[0x02C:0x12C]):
            R.warn('header.reserved', 'header.path (0x2C..0x12C) is not zero', flags=('engine-ignored',))
        if any(raw[0x2F8:0x7FC]):
            R.warn('header.reserved', 'reserved bytes 0x2F8..0x7FC are not zero', flags=('engine-ignored',))
        if any(raw[0x155:0x158]) or raw[0x183]:
            R.warn('header.reserved', 'header padding bytes (0x155/0x183) not zero', flags=('engine-ignored',))
        if h.crc_valid != 1:
            R.warn('header.crc_valid', 'crc_valid@0x154 = %d (stock: 1)' % h.crc_valid, flags=('engine-ignored',))
        if not h.name:
            R.warn('header.name', 'header name is empty')
        elif h.name.lower() != os.path.splitext(os.path.basename(mv.path))[0].lower():
            R.note('header.name_vs_file', 'header name %r differs from the file name %r' % (h.name, os.path.basename(mv.path)))
        # scenario type vs mask / shared flags
        t = h.type
        us = tuple(h.uses_shared_map)
        expect_us = {0: [(1, 1, 1), (1, 1, 0)], 1: [(1, 1, 0)], 2: [(0, 0, 0)], 3: [(1, 0, 0)], 4: [(1, 1, 0)]}.get(t)
        if expect_us and us not in expect_us:
            R.warn('header.uses_shared', 'uses_shared_map %s unusual for type %d (stock: %s)' % (us, t, expect_us))
        if t == 1:
            if h.tag_offset_mask != 0x1400000:
                (R.error if h.tag_offset_mask < SHARED_TAG_SIZE_MIN_MASK else R.warn)(
                    'header.tag_offset_mask', 'tag_offset_mask %#x (stock MP: 0x1400000; must be >= shared.map tag_size %#x)' % (
                        h.tag_offset_mask, SHARED_TAG_SIZE_MIN_MASK))
        elif h.tag_offset_mask != 0:
            R.warn('header.tag_offset_mask', 'tag_offset_mask %#x for type %d (stock: 0)' % (h.tag_offset_mask, t))
        if t == 1 and mv.ntags >= FIRST_SHARED_TAG_INSTANCE_INDEX and not (us[0] and us[1]):
            R.error('header.uses_shared', 'MP map with shared placeholders must flag mainmenu+shared in uses_shared_map, got %s' % (us,))
        # creation times of the shared databases this map was built against
        if t in (0, 1, 4):
            for i, nm in enumerate(('mainmenu.map', 'shared.map', 'single_player_shared.map')):
                sct = h.shared_creation_time[i]
                if us[i]:
                    db = self.ext(nm)
                    if db.exists and db.header is not None and db.header.creation_time != sct:
                        R.warn('header.shared_creation_time', 'shared_creation_time[%d] %#x != %s creation_time %#x' % (
                            i, sct, nm, db.header.creation_time), flags=('engine-ignored',))
                elif sct:
                    R.warn('header.shared_creation_time', 'shared_creation_time[%d] set but uses_shared_map[%d] = 0' % (i, i), flags=('engine-ignored',))
        # limits
        if h.string_table_count > MAX_STRING_IDS:
            R.error('limit.string_ids', 'string_table_count %d > %d' % (h.string_table_count, MAX_STRING_IDS))
        if h.string_table_size >= MAX_STRING_BYTES:
            R.error('limit.string_bytes', 'string_table_size %#x >= %#x' % (h.string_table_size, MAX_STRING_BYTES))
        if h.tag_name_buffer_size > MAX_NAME_BUFFER:
            R.error('limit.name_buffer', 'tag name buffer %d > %d' % (h.tag_name_buffer_size, MAX_NAME_BUFFER))
        if h.debug_tag_name_count > MAX_NAME_INDEX:
            R.error('limit.name_index', 'debug_tag_name_count %d > %d' % (h.debug_tag_name_count, MAX_NAME_INDEX))
        if mv.ntags > MAX_INSTANCES:
            R.error('limit.instances', 'tag_count %d > %d' % (mv.ntags, MAX_INSTANCES))
        # mopp checksum (engine-ignored)
        try:
            exp = mopp_checksum_of(c) if t == 1 else 0
            if exp != h.mopp_checksum:
                R.warn('header.mopp_checksum', 'mopp_checksum %#x != computed %#x' % (h.mopp_checksum, exp), flags=('engine-ignored',))
        except Exception as e:
            R.note('header.mopp_checksum', 'mopp checksum not computable: %s' % e)

    # ==============================================================================================
    # file regions
    # ==============================================================================================
    def _check_regions(self, R, mv):
        h, c = mv.h, mv.c
        regs = []          # (name, offset, payload size, check_align)
        regs.append(('string_block', h.string_block_offset, h.string_table_count * 128))
        regs.append(('string_idx', h.string_idx_offset, 4 * h.string_table_count))
        regs.append(('string_table', h.string_table_offset, h.string_table_size))
        regs.append(('tag_names', h.tag_name_buffer_offset, h.tag_name_buffer_size))
        regs.append(('tag_name_idx', h.tag_name_offset, 4 * h.debug_tag_name_count))
        has_geo = h.geometry_data_offset not in (0, NULL)
        if has_geo:
            regs.append(('geometry', h.geometry_data_offset, h.geometry_data_size))
        elif h.geometry_data_size:
            R.error('region.geometry', 'geometry_data_offset is -1/0 but geometry_data_size = %#x' % h.geometry_data_size)
        if h.language_pack_offset != NULL:
            regs.append(('language_pack', h.language_pack_offset, h.language_pack_size))
        if h.shared_tag_dependency_offset != NULL:
            regs.append(('shared_graph', h.shared_tag_dependency_offset, h.shared_tag_dependency_count))
        regs.append(('tag_region', h.tag_offset, h.tag_size))
        for nm, off, sz in regs:
            if off % PAGE:
                R.error('region.align', '%s offset %#x not 0x200-aligned' % (nm, off))
            if off < HEADER_SIZE or off + sz > mv.size:
                R.error('region.bounds', '%s [%#x, %#x) outside the file (size %#x)' % (nm, off, off + sz, mv.size))
        # ordering / overlap (padded extents)
        ext = sorted((off, align_up(off + sz, PAGE) if nm != 'tag_region' else off + sz, nm) for nm, off, sz in regs if sz)
        for (a0, a1, an), (b0, b1, bn) in zip(ext, ext[1:]):
            if b0 < a1:
                R.error('region.overlap', 'region %s [%#x, %#x) overlaps %s [%#x, %#x)' % (an, a0, a1, bn, b0, b1))
        if h.tag_offset + h.tag_size != mv.size:
            R.error('region.tag_end', 'tag region [%#x, +%#x) does not end at EOF %#x' % (h.tag_offset, h.tag_size, mv.size))
        if h.data_offset + h.data_size != h.tag_size:
            R.error('region.tag_split', 'data_offset %#x + data_size %#x != tag_size %#x' % (h.data_offset, h.data_size, h.tag_size))
        if h.data_offset % 0x1000 or h.data_size % 0x1000:
            R.warn('region.tag_4k', 'data_offset/data_size not 4 KiB aligned (%#x/%#x)' % (h.data_offset, h.data_size))
        need = c.instance_table_off + 16 * mv.ntags
        if h.data_offset < need:
            R.error('region.tag_header', 'data_offset %#x too small for header+groups+instances (%#x)' % (h.data_offset, need))
        elif h.data_offset != align_up(need, 0x1000):
            R.warn('region.tag_header', 'data_offset %#x is not the minimal 4 KiB-aligned size %#x' % (h.data_offset, align_up(need, 0x1000)))
        # the lowest region after the header: raw sounds live in [0x800, first region)
        first = min([off for nm, off, sz in regs if nm in ('geometry', 'string_block')] or [mv.size])
        mv.sound_lo, mv.sound_hi = HEADER_SIZE, first
        mv.geo_lo = h.geometry_data_offset if has_geo else 0
        mv.geo_hi = h.geometry_data_offset + h.geometry_data_size if has_geo else 0
        bm_lo = align_up(max(([off + sz for nm, off, sz in regs if nm in ('language_pack', 'tag_names', 'tag_name_idx', 'string_table', 'string_idx', 'string_block')] or [HEADER_SIZE])), PAGE)
        if h.language_pack_offset != NULL:
            lp = c.raw(h.language_pack_offset, min(h.language_pack_size, 0xFC))
            tops = [bm_lo]
            for i in range(len(lp) // 28):
                cnt, size, ioff, doff = struct.unpack_from('<IIII', lp, 28 * i + 8)
                if cnt or size:
                    tops += [ioff + 8 * cnt, doff + size]
            bm_lo = align_up(max(tops + [h.language_pack_offset + h.language_pack_size]), PAGE)
        mv.bitmap_lo, mv.bitmap_hi = bm_lo, h.tag_offset
        R.info['regions'] = {nm: [off, sz] for nm, off, sz in regs}

    # ==============================================================================================
    # group table
    # ==============================================================================================
    def _check_groups(self, R, mv):
        c = mv.c
        gl = mv.group_links
        if c.group_table_off != 0x20:
            R.error('groups.offset', 'group table offset %#x (expected 0x20)' % c.group_table_off)
        if c.instance_table_off != 0x20 + 12 * c.group_count:
            R.error('groups.instance_offset', 'instance table offset %#x != 0x20 + 12*%d' % (c.instance_table_off, c.group_count))
        if c.group_count != 120:
            R.warn('groups.count', 'group count %d (engine tables have 120 groups)' % c.group_count)
        g0 = gl[:, 0]
        if len(set(g0.tolist())) != len(g0):
            R.error('groups.duplicate', 'duplicate group four-CCs in the group table')
        known = set(g0.tolist())
        for g, pa, gp in gl.tolist():
            for ref, what in ((pa, 'parent'), (gp, 'grandparent')):
                if ref != NULL and ref not in known:
                    R.error('groups.parent', 'group %s has %s %s which is not in the table' % (fourcc_str(struct.pack('<I', g)), what, fourcc_str(struct.pack('<I', ref))))
        if list(g0.tolist()) != sorted(g0.tolist()):
            R.warn('groups.order', 'group links are not sorted ascending by stored dword (stock: sorted)')

    # ==============================================================================================
    # string table
    # ==============================================================================================
    def _check_strings(self, R, mv):
        h, c = mv.h, mv.c
        cnt, tsz = h.string_table_count, h.string_table_size
        if cnt == 0 or tsz == 0:
            R.error('strings.empty', 'string table is empty')
            mv.str_lens = np.zeros(0, dtype=np.int64)
            return
        if h.string_idx_offset + 4 * cnt > mv.size or h.string_table_offset + tsz > mv.size:
            R.error('strings.bounds', 'string index/table outside the file')
            mv.str_lens = np.zeros(0, dtype=np.int64)
            return
        idx = np.frombuffer(c._mm, dtype='<u4', count=cnt, offset=h.string_idx_offset).astype(np.int64)
        tbl = bytes(c._mm[h.string_table_offset:h.string_table_offset + tsz])
        if idx[0] != 0 or tbl[0:1] != b'\0':
            R.error('strings.first', 'string 0 must be the empty string at offset 0')
        if tbl[-1:] != b'\0':
            R.error('strings.terminator', 'string table does not end with NUL')
        bad = idx >= tsz
        if bad.any():
            R.error('strings.index', '%d string offsets point past the table' % int(bad.sum()))
            idx = np.minimum(idx, tsz - 1)
        # strings are stored back-to-back in index order
        parts = tbl.split(b'\0')
        if len(parts) != cnt + 1 or parts[-1] != b'':
            R.warn('strings.layout', 'string table splits into %d NUL-terminated strings, header says %d' % (len(parts) - 1, cnt))
        lens = np.asarray([len(x) for x in parts[:cnt]], dtype=np.int64)
        if len(lens) == cnt:
            exp = np.concatenate([[0], np.cumsum(lens + 1)[:-1]])
            if not np.array_equal(exp, idx):
                k = int(np.nonzero(exp != idx)[0][0])
                R.warn('strings.offsets', 'string %d offset %#x != back-to-back offset %#x' % (k, idx[k], exp[k]))
            mv.str_lens = lens
            if len(set(parts[:cnt])) != cnt:
                dup = [p for p, n in collections.Counter(parts[:cnt]).items() if n > 1]
                R.warn('strings.duplicate', '%d duplicated strings (e.g. %r)' % (len(dup), dup[0][:40]))
        else:
            mv.str_lens = np.zeros(cnt, dtype=np.int64)
            mv.str_lens[:min(cnt, len(lens))] = lens[:cnt]
        mv.str_parts = parts
        # shared prefix (every MP map's table starts with shared.map's table)
        if h.type == 1 and mv.ntags >= FIRST_SHARED_TAG_INSTANCE_INDEX:
            sh = self.shared_strings()
            if sh is None:
                R.note('strings.shared_prefix', 'shared.map not available - shared string prefix not verified')
            else:
                n = min(len(sh), cnt)
                if cnt < len(sh):
                    R.error('strings.shared_prefix', 'string table has %d entries, shared.map has %d (must be a prefix)' % (cnt, len(sh)))
                for i in range(n):
                    if parts[i] != sh[i]:
                        R.error('strings.shared_prefix', 'string %d %r differs from shared.map %r (first %d strings must equal shared.map)' % (
                            i, parts[i][:30], sh[i][:30], len(sh)))
                        break
        R.stats['strings'] = cnt

    # ==============================================================================================
    # tag names
    # ==============================================================================================
    def _check_names(self, R, mv):
        h, c = mv.h, mv.c
        cnt = h.debug_tag_name_count
        if cnt != mv.ntags:
            R.warn('names.count', 'debug_tag_name_count %d != tag_count %d' % (cnt, mv.ntags))
        nm_end = h.tag_name_buffer_offset + h.tag_name_buffer_size
        if h.tag_name_buffer_size and nm_end <= mv.size and c._mm[nm_end - 1:nm_end] != b'\0':
            R.warn('names.terminator', 'tag name buffer does not end with NUL')
        if h.tag_name_offset + 4 * cnt > mv.size:
            return
        idx = np.frombuffer(c._mm, dtype='<u4', count=cnt, offset=h.tag_name_offset)
        bad = (idx != NULL) & (idx >= h.tag_name_buffer_size)
        if bad.any():
            R.error('names.index', '%d tag-name offsets point past the name buffer' % int(bad.sum()))
        names = c._names
        seen = {}
        nonempty = mv.inst_datum != NULL
        for i in np.nonzero(nonempty)[0].tolist():
            if i >= len(names) or names[i] is None:
                R.warn('names.missing', 'tag %d (%s) has no name' % (i, fourcc_str(struct.pack('<I', int(mv.inst_group[i])))), tag=mv.tag_by_index[i])
                continue
            nm = names[i]
            if len(nm) > 255:
                R.error('names.length', 'tag name longer than 255 chars', tag=mv.tag_by_index[i])
            key = (int(mv.inst_group[i]), nm.lower())
            if key in seen:
                j = seen[key]
                if (j >= FIRST_SHARED_TAG_INSTANCE_INDEX) != (i >= FIRST_SHARED_TAG_INSTANCE_INDEX):
                    # a local copy next to the shared placeholder of the same name: legal (shared tags keep using the shared one)
                    # but stock maps never do it
                    R.note('names.shadows_shared', 'local tag %d shadows shared placeholder %d (%s %r)' % (min(i, j), max(i, j), mv.tag_by_index[i].group, nm), tag=mv.tag_by_index[i])
                else:
                    R.error('names.duplicate', 'tag %d and tag %d share group+name %r' % (j, i, nm), tag=mv.tag_by_index[i])
            else:
                seen[key] = i
            if any(ord(ch) < 32 for ch in nm):
                R.error('names.charset', 'control characters in tag name %r' % nm, tag=mv.tag_by_index[i])
        empt = np.nonzero(~nonempty)[0]
        empt = empt[empt < cnt]
        if len(empt) and (idx[empt] != NULL).any():
            R.warn('names.empty_slot', '%d empty tag slots carry a name' % int((idx[empt] != NULL).sum()))
        # header scenario_path vs the name of the scenario tag
        si = c.scenario_datum & 0xFFFF
        if h.type in (0, 1) and si < len(names) and names[si] and names[si].lower() != h.scenario_path.lower():
            R.warn('names.scenario_path', 'header scenario_path %r != name of the scenario tag %r' % (h.scenario_path, names[si]))

    # ==============================================================================================
    # tag instance table
    # ==============================================================================================
    def _check_instances(self, R, mv):
        h, c = mv.h, mv.c
        n = mv.ntags
        idxs = np.arange(n, dtype=np.uint32)
        live = mv.inst_datum != NULL
        # empties
        emp = ~live
        bad_empty = emp & ((mv.inst_group != NULL) | (mv.inst_addr != 0) | (mv.inst_size != 0))
        if bad_empty.any():
            R.warn('inst.empty_form', '%d empty instance slots are not {-1,-1,0,0}' % int(bad_empty.sum()))
        # datum = salt<<16 | index
        bad_idx = live & ((mv.inst_datum & 0xFFFF) != idxs)
        if bad_idx.any():
            i = int(np.nonzero(bad_idx)[0][0])
            R.error('inst.datum_index', '%d instances whose datum low word != index (first: tag %d datum %#x)' % (int(bad_idx.sum()), i, mv.inst_datum[i]), tag=mv.tag_by_index[i])
        bad_salt = live & ((mv.inst_datum >> 16) == 0)
        if bad_salt.any():
            i = int(np.nonzero(bad_salt)[0][0])
            R.error('inst.datum_salt', '%d instances with salt 0 (first: tag %d)' % (int(bad_salt.sum()), i), tag=mv.tag_by_index[i])
        bad_grp = live & ~_known_group_mask(mv, mv.inst_group)
        if bad_grp.any():
            i = int(np.nonzero(bad_grp)[0][0])
            R.error('inst.group', '%d instances with a group not in the group table (first: tag %d)' % (int(bad_grp.sum()), i), tag=mv.tag_by_index[i])
        # local vs shared
        local = live & (idxs < FIRST_SHARED_TAG_INSTANCE_INDEX)
        shared = live & (idxs >= FIRST_SHARED_TAG_INSTANCE_INDEX)
        R.stats['tags_local'] = int(local.sum())
        R.stats['tags_shared_placeholders'] = int(shared.sum())
        sh_data = shared & ((mv.inst_addr != 0) | (mv.inst_size != 0))
        if h.type != 3 and sh_data.any():
            i = int(np.nonzero(sh_data)[0][0])
            R.error('inst.shared_has_data', '%d shared placeholders (index >= 10000) carry addr/size (first: tag %d)' % (int(sh_data.sum()), i), tag=mv.tag_by_index[i])
        # tiling of local tag data
        lo, hi = mv.region_lo, mv.region_hi
        data_tags = np.nonzero(live & (mv.inst_addr != 0) & (mv.inst_size != 0))[0]
        if h.type == 3:
            data_tags = data_tags[data_tags >= FIRST_SHARED_TAG_INSTANCE_INDEX]
        addrs = mv.inst_addr[data_tags].astype(np.int64)
        sizes = mv.inst_size[data_tags].astype(np.int64)
        if len(data_tags):
            if addrs[0] != lo:
                R.error('inst.first_tag', 'first tag data at %#x, expected region start %#x (tag %d)' % (addrs[0], lo, data_tags[0]), tag=mv.tag_by_index[int(data_tags[0])])
            ends = addrs + sizes
            gaps = addrs[1:] != ends[:-1]
            if gaps.any():
                k = int(np.nonzero(gaps)[0][0])
                R.error('inst.tiling', '%d tag data gaps/overlaps (first between tag %d end %#x and tag %d start %#x)' % (
                    int(gaps.sum()), data_tags[k], ends[k], data_tags[k + 1], addrs[k + 1]), tag=mv.tag_by_index[int(data_tags[k + 1])])
            if ends[-1] > hi:
                R.error('inst.region_end', 'last tag ends at %#x beyond the tag region end %#x' % (ends[-1], hi))
            if ((addrs & 3) | (sizes & 3)).any():
                R.error('inst.align4', 'tag data not 4-byte aligned/sized')
            if ends[-1] + 0x1000 <= hi:
                R.warn('inst.region_slack', 'tag region has %#x unused bytes after the last tag (> 4 KiB)' % (hi - ends[-1]))
        zero = np.nonzero(live & (mv.inst_addr != 0) & (mv.inst_size == 0))[0]
        if h.type == 3:
            zero = zero[zero >= FIRST_SHARED_TAG_INSTANCE_INDEX]
        zb = (mv.inst_addr[zero].astype(np.int64) < lo) | (mv.inst_addr[zero].astype(np.int64) > hi)
        if zb.any():
            R.error('inst.zero_size_addr', '%d zero-size tags with an address outside the tag region' % int(zb.sum()))
        R.stats['tags_zero_size'] = int(len(zero))
        R.stats['tags_with_data'] = int(len(data_tags))
        no_addr = np.nonzero(local & (mv.inst_addr == 0))[0]
        if len(no_addr):
            R.error('inst.local_no_data', '%d local tags (index < 10000) have addr 0 (first: tag %d)' % (len(no_addr), no_addr[0]), tag=mv.tag_by_index[int(no_addr[0])])
        # scenario / globals datums
        sd, gd = c.scenario_datum, c.globals_datum
        si = sd & 0xFFFF
        if sd == NULL or si >= n or mv.inst_datum[si] != sd or mv.inst_group[si] != _fcc('scnr'):
            R.error('inst.scenario', 'scenario datum %#x does not reference a live scnr instance' % sd)
        else:
            if si >= FIRST_SHARED_TAG_INSTANCE_INDEX and h.type != 3:
                R.error('inst.scenario', 'scenario tag must be local, index %d' % si)
            st_ = mv.tag_by_index[si]
            if st_.addr and st_.size >= 0x12:
                stype = struct.unpack_from('<h', mv.buf, mv.fo(st_.addr) + 0x10)[0]
                if stype != h.type:
                    R.error('inst.scenario_type', 'scnr.Type = %d but header.type = %d' % (stype, h.type), tag=st_)
        gi = gd & 0xFFFF
        if gd == NULL:
            R.warn('inst.globals', 'no globals (matg) datum in the tags header')
        elif gi >= n or mv.inst_datum[gi] != gd or mv.inst_group[gi] != _fcc('matg'):
            R.error('inst.globals', 'globals datum %#x does not reference a live matg instance' % gd)
        elif h.type == 1 and gi < FIRST_SHARED_TAG_INSTANCE_INDEX:
            R.note('custom_list.globals', 'MP map: globals matg (index %d) is local; the stock custom-map list requires the shared matg' % gi, flags=('cartographer-ignored',))
        # secondary ugh!
        s2 = h.secondary_ugh_tag_index
        if s2 != NULL:
            i2 = s2 & 0xFFFF
            if i2 >= n or mv.inst_datum[i2] != s2 or mv.inst_group[i2] != _fcc('ugh!'):
                R.error('inst.secondary_ugh', 'secondary_ugh_tag_index %#x does not reference a live ugh! instance' % s2)
            elif i2 >= FIRST_SHARED_TAG_INSTANCE_INDEX:
                R.note('custom_list.secondary_ugh', 'secondary ugh! (index %d) is a shared placeholder; the custom-map list needs a local one' % i2, flags=('cartographer-ignored',))
        # shared placeholders must be identical to shared.map's instances
        if h.type == 1 and shared.any():
            db = self.ext('shared.map')
            if db.exists and db.header is not None:
                try:
                    sc = VistaCache(db.path)
                    sinst = np.frombuffer(sc.tags_header_bytes, dtype='<u4', count=4 * sc.tag_count, offset=sc.instance_table_off).reshape(-1, 4)
                    k = min(len(sinst), n)
                    sel = np.nonzero(shared[:k])[0]
                    diff = (sinst[sel, 0] != mv.inst_group[sel]) | (sinst[sel, 1] != mv.inst_datum[sel])
                    if diff.any():
                        j = int(sel[np.nonzero(diff)[0][0]])
                        R.error('inst.shared_mismatch', '%d shared placeholders differ from shared.map (first: tag %d group/datum %s/%#x vs shared %s/%#x)' % (
                            int(diff.sum()), j, fourcc_str(struct.pack('<I', int(mv.inst_group[j]))), mv.inst_datum[j],
                            fourcc_str(struct.pack('<I', int(sinst[j, 0]))), sinst[j, 1]), tag=mv.tag_by_index[j])
                    over = np.nonzero(shared[k:])[0]
                    if len(over):
                        R.error('inst.shared_mismatch', '%d placeholders beyond shared.map tag_count %d' % (len(over), len(sinst)))
                    # names should match as well
                    snames = sc._names
                    nd = 0
                    for j in sel.tolist():
                        if j < len(snames) and j < len(mv.c._names) and snames[j] != mv.c._names[j]:
                            nd += 1
                    if nd:
                        R.warn('inst.shared_names', '%d shared placeholder names differ from shared.map' % nd)
                    sc.close()
                except Exception as e:
                    R.note('inst.shared_compare', 'could not compare placeholders with shared.map: %s' % e)
            else:
                R.note('inst.shared_compare', 'shared.map not available - placeholders not verified')
        if h.type == 1 and n > FIRST_SHARED_TAG_INSTANCE_INDEX:
            if n != 15065:
                R.note('inst.tag_count', 'MP tag_count %d (stock: 15065)' % n)
            if mv.inst_datum[n - 1] == NULL:
                R.warn('inst.tag_count', 'tag_count %d but the last instance slot is empty (stock: highest used shared index + 1)' % n)

    # ==============================================================================================
    # language pack
    # ==============================================================================================
    def _check_language_pack(self, R, mv):
        """Locale tables: the header language pack (MP maps) and the nine 28-byte language entries of every local
        matg (+0x188).  Both are {ptr, ptr, count, size, index_off, data_off, bool}; index = {string_id, offset} pairs."""
        h, c = mv.h, mv.c
        sources = []                 # (what, tag, [(lang, count, size, ioff, doff)])
        if h.language_pack_offset == NULL:
            if h.language_pack_size:
                R.error('lang.size', 'language_pack_offset = -1 but size = %#x' % h.language_pack_size)
        else:
            if h.language_pack_size != 0xFC:
                R.warn('lang.size', 'language_pack_size %#x (stock: 0xFC = 9 x 28)' % h.language_pack_size)
            lp = c.raw(h.language_pack_offset, h.language_pack_size)
            ents = [(i,) + struct.unpack_from('<IIII', lp, 28 * i + 8) for i in range(min(9, len(lp) // 28))]
            sources.append(('header language pack', None, ents))
            R.info['languages'] = [e[1] for e in ents]
        for t in mv.tag_by_index:
            if t.group == 'matg' and t.datum != NULL and t.addr and t.size >= 0x188 + 9 * 28:
                fo = mv.fo(t.addr)
                ents = [(i,) + struct.unpack_from('<IIII', bytes(mv.buf[fo + 0x188 + 28 * i + 8:fo + 0x188 + 28 * i + 24])) for i in range(9)]
                sources.append(('matg locale', t, ents))
        counts_by_src = {}
        tops = []
        for what, tag, ents in sources:
            counts = []
            for (i, cnt, size, ioff, doff) in ents:
                counts.append(cnt)
                if cnt == 0 and size == 0:
                    continue
                where = '%s language %d' % (what, i)
                local = True
                for nm, off, sz in (('index', ioff, 8 * cnt), ('data', doff, size)):
                    kind, db, o = self._resolve_loc(R, mv, off, where + ' ' + nm + ' table', tag, '')
                    if kind != 'self':
                        local = False
                        continue
                    if o % PAGE or o < HEADER_SIZE or o + sz > mv.size:
                        R.error('lang.region', '%s %s table [%#x, +%#x) misaligned or outside the file' % (where, nm, o, sz), tag=tag)
                        local = False
                    else:
                        tops.append(o + sz)
                if not local:
                    continue
                o1 = ioff & 0x3FFFFFFF
                if cnt and o1 + 8 * cnt <= mv.size:
                    ent = np.frombuffer(c._mm, dtype='<u4', count=2 * cnt, offset=o1).reshape(-1, 2)
                    if (ent[:, 1] >= max(size, 1)).any():
                        R.error('lang.offsets', '%s has string offsets beyond its data (%#x)' % (where, size), tag=tag)
                    sid = ent[:, 0]
                    if mv.str_lens is not None and len(mv.str_lens):
                        idx_ = (sid & 0xFFFFFF).astype(np.int64)
                        ok = (idx_ < len(mv.str_lens)) | (sid == 0)
                        if not ok.all():
                            R.error('lang.string_ids', '%s: %d locale string ids outside the string table' % (where, int((~ok).sum())), tag=tag)
                        else:
                            bad = (sid != 0) & ((sid >> 24).astype(np.int64) != mv.str_lens[np.where(sid != 0, idx_, 0)])
                            if bad.any():
                                R.error('lang.string_length', '%s: %d locale string ids whose length byte != strlen' % (where, int(bad.sum())), tag=tag)
            counts_by_src[what] = counts
            # the empty 9th language (portuguese) and English-only packs (highplains) are normal in stock maps: only an
            # English pack that is EMPTY while another language has strings is suspicious
            if what == 'header language pack' and counts and counts[0] == 0 and any(counts[1:8]):
                R.note('lang.counts', '%s: English has no strings but other languages do: %s' % (what, counts), tag=tag)
        if tops:
            mv.bitmap_lo = max(mv.bitmap_lo, align_up(max(tops), PAGE))
        R.info['locale_sources'] = {k: v for k, v in counts_by_src.items()}

    # ==============================================================================================
    # custom-map-list rules (stock engine requirements that Cartographer ignores)
    # ==============================================================================================
    def _check_custom_list(self, R, mv):
        h = mv.h
        if h.type not in (0, 1):
            return
        fl = ('cartographer-ignored',)
        if not (CUSTOM_LIST_MIN <= mv.size <= CUSTOM_LIST_MAX):
            R.note('custom_list.size', 'file size %#x outside the custom-map list window [%#x, %#x]' % (mv.size, CUSTOM_LIST_MIN, CUSTOM_LIST_MAX), flags=fl)
        if h.type == 1:
            if h.language_pack_offset == NULL:
                R.note('custom_list.names', 'MP map has no language pack (map names for the 9 languages)', flags=fl)
            else:
                cnts = R.info.get('languages') or []
                if len(cnts) != 9 or min(cnts[:8]) < 1:
                    R.note('custom_list.names', 'language pack lacks map-name strings in some of the 9 languages: %s' % cnts, flags=fl)
            if mv.ntags > 15064 and (mv.inst_group[15064] != _fcc('ugh!') or mv.inst_datum[15064] == NULL):
                R.note('custom_list.shared_ugh', 'tag 15064 is not the shared sound gestalt ugh!', flags=fl)


def _fcc(s):
    """four-cc text -> the little-endian dword as stored in the file."""
    return struct.unpack('<I', s.encode('latin-1')[::-1])[0]


class _TagState(object):
    """Per-tag walk state: own extent, recorded intervals, blocks already walked."""
    __slots__ = ('tag', 'base', 'lo', 'hi', 's', 'e', 'a', 'seen', 'layout')

    def __init__(self, tag, layout):
        self.tag = tag
        self.base = tag.addr
        self.lo, self.hi = tag.addr, tag.addr + tag.size
        self.s, self.e, self.a = [], [], []
        self.seen = set()
        self.layout = layout


class _WalkMixin(object):
    # ==============================================================================================
    # tag walk
    # ==============================================================================================
    def _walk_all(self, R, mv):
        sel = np.nonzero((mv.inst_datum != NULL) & (mv.inst_addr != 0) & (mv.inst_size != 0))[0]
        for i in sel.tolist():
            self._walk_tag(R, mv, mv.tag_by_index[i])

    def _walk_tag(self, R, mv, tag):
        R.stats['tags_walked'] += 1
        plan = self.plans.get(tag.group)
        lay = self.layouts.get(tag.group)
        if plan is None or lay is None:
            R.stats['tags_no_layout'] += 1
            R.note('walk.no_layout', 'no plugin layout for group %s - tag only range-checked' % tag.group, tag=tag)
            return
        if tag.addr < mv.region_lo or tag.addr + tag.size > mv.region_hi:
            return                      # reported by the instance check
        bs = plan.es
        if tag.size < bs:
            R.error('walk.tag_too_small', 'tag size %#x < main struct size %#x' % (tag.size, bs), tag=tag)
            return
        fo = mv.fo(tag.addr)
        rows = mv.buf[fo:fo + bs].reshape(1, bs)
        st = _TagState(tag, lay)
        stack = [Item(plan, rows, np.array([tag.addr], dtype=np.int64), np.array([0], dtype=np.int64),
                      np.array([tag.addr], dtype=np.int64))]
        while stack:
            self._process_item(R, mv, st, stack.pop(), stack)
        self._tiling(R, mv, st)

    # ---- reporting helper ---------------------------------------------------------------------------
    def _rep(self, R, level, code, st, it, mask, ridx, fidx, names, text, flags=(), extra=None):
        """Report the entries where `mask` is true: count them all, keep the first as an example."""
        idx = np.nonzero(mask)[0]
        if not len(idx):
            return
        j = int(idx[0])
        fname = names[int(fidx[j])] if (fidx is not None and names is not None) else ''
        where = '%s/%s' % (it.path, fname) if fname else (it.path or '/')
        msg = '%s (%s %s)' % (text, where, it.describe(int(ridx[j])))
        if extra is not None:
            msg += ' ' + extra(j)
        R.add(level, code, msg, tag=st.tag, path=where, n=len(idx), flags=flags)

    # ---- one batch of structs ---------------------------------------------------------------------
    def _process_item(self, R, mv, st, it, stack):
        p, rows = it.plan, it.rows
        n = rows.shape[0]
        R.stats['rows_walked'] += n
        if p._ridx is not None:
            k = len(p.r_off)
            sub = np.ascontiguousarray(rows[:, p._ridx]).view('<u4').reshape(n, k, 2)
            self._check_refs(R, mv, st, it, sub[:, :, 0].ravel(), sub[:, :, 1].ravel(), p.r_name, k)
        if p._tidx is not None:
            k = len(p.t_off)
            sub = np.ascontiguousarray(rows[:, p._tidx]).view('<u4').reshape(n, k)
            self._check_refs(R, mv, st, it, None, sub.ravel(), p.t_name, k)
        if p._sidx is not None:
            k = len(p.s_off)
            sub = np.ascontiguousarray(rows[:, p._sidx]).view('<u4').reshape(n * k)
            self._check_sids(R, mv, st, it, sub, p.s_name, k)
        if p._didx is not None:
            k = len(p.d_off)
            sub = np.ascontiguousarray(rows[:, p._didx]).view('<u4').reshape(n, k, 2)
            self._datarefs(R, mv, st, it, sub[:, :, 0].view('<i4').ravel(), sub[:, :, 1].ravel(), k)
        if p._bidx is not None:
            k = len(p.b_off)
            sub = np.ascontiguousarray(rows[:, p._bidx]).view('<u4').reshape(n, k, 2)
            cnt = sub[:, :, 0].view('<i4').ravel()
            addr = sub[:, :, 1].ravel()
            nz = np.nonzero((cnt != 0) | (addr != 0))[0]
            if len(nz):
                self._blocks(R, mv, st, it, cnt[nz], addr[nz], nz // k, nz % k, stack)
        if self.check_floats and p._fidx is not None:
            col = np.ascontiguousarray(rows[:, p._fidx]).view('<f4')
            R.stats['floats_scanned'] += col.size
            bad = ~np.isfinite(col)
            if bad.any():
                r_, l_ = np.nonzero(bad)
                f_ = p._flane[l_]
                self._rep(R, 'error', 'float.nonfinite', st, it, np.ones(len(r_), dtype=bool), r_, f_, p.f_name, 'NaN / infinite float value')
        if p._enum is not None and (self.check_enums or self._learn is not None):
            self._check_enums(R, mv, st, it)
        if p.geo:
            self._geo_sites(R, mv, st, it)
        if p.hook:
            getattr(self, '_hook_' + p.hook)(R, mv, st, it)

    # ---- enum / flags -------------------------------------------------------------------------------------------
    def _check_enums(self, R, mv, st, it):
        p, rows = it.plan, it.rows
        n = rows.shape[0]
        for g in p._enum:
            size = g['size']
            vals = np.ascontiguousarray(rows[:, g['idx']]).view({1: '<u1', 2: '<u2', 4: '<u4'}[size]).reshape(n, -1).astype(np.int64)
            is_fl = g['is_fl']
            if self._learn is not None:
                for j in range(vals.shape[1]):
                    e = p.e_fields[int(g['fidx'][j])]
                    if is_fl[j]:
                        extra_bits = int(np.bitwise_or.reduce(vals[:, j])) & ~e['allowed']
                        if extra_bits:
                            self._learn.setdefault(e['key'], {'m': 0})['m'] |= extra_bits
                    else:
                        extra = set(np.unique(vals[:, j]).tolist()) - e['allowed'] - {g['allones']}
                        if extra:
                            self._learn.setdefault(e['key'], {'v': set()})['v'].update(extra)
                continue
            R.stats['enums_scanned'] += vals.size
            bad = np.zeros(vals.shape, dtype=bool)
            fl = np.nonzero(is_fl)[0]
            en = np.nonzero(~is_fl)[0]
            if len(fl):
                bad[:, fl] = (vals[:, fl] & ~g['masks'][fl]) != 0
            if len(en):
                k = (en[None, :].astype(np.int64) << 32) | vals[:, en]
                ok = np.isin(k.ravel(), g['keys']).reshape(k.shape) | (vals[:, en] == g['allones'])
                bad[:, en] = ~ok
            if bad.any():
                r_, c_ = np.nonzero(bad)
                f_ = g['fidx'][c_]
                isf = is_fl[c_]
                for mask_, code, what in ((isf, 'flags.range', 'flags value has bits the Vista plugin and stock maps never use'),
                                          (~isf, 'enum.range', 'enum value outside the Vista plugin options and stock data')):
                    if mask_.any():
                        self._rep(R, 'warning', code, st, it, mask_, r_, f_, p.e_names, what,
                                  extra=lambda j: 'value %#x' % int(vals[r_[j], c_[j]]))

    # ---- tag references -----------------------------------------------------------------------------
    def _check_refs(self, R, mv, st, it, g, d, names, k):
        nulls = d == NULL
        if g is not None:
            jn = nulls & (g != NULL) & ~_known_group_mask(mv, g)
            if jn.any():
                e = np.arange(len(d))
                # stock maps carry typed nulls whose group is an obsolete one (cdmg, antr, shdr ...): harmless
                self._rep(R, 'note', 'ref.null_group', st, it, jn, e // k, e % k, names, 'null tagref (datum -1) typed with a group missing from the group table',
                          extra=lambda j: 'group %s' % fourcc_str(struct.pack('<I', int(g[j]))))
        nn = np.nonzero(~nulls)[0]
        if not len(nn):
            return
        R.stats['tagrefs'] += len(nn)
        dd = d[nn]
        idx = (dd & 0xFFFF).astype(np.int64)
        oor = idx >= mv.ntags
        safe = np.where(oor, 0, idx)
        mism = ~oor & (mv.inst_datum[safe] != dd)
        r, f = nn // k, nn % k
        txt = lambda j: 'datum %#010x' % int(dd[j])
        # 'Owner Tag' style group-less datums are 0 instead of -1 when the block has no geometry (stock sbsp/ltmp, 85+ of 43k)
        zero = mism & (dd == 0) & (g is None)
        mism = mism & ~zero
        if self._collect is not None:
            self._collect.setdefault(st.tag.index, set()).update(idx[~oor & ~mism & ~zero].tolist())
        self._rep(R, 'note', 'ref.zero_datum', st, it, zero, r, f, names, 'group-less tag datum is 0 (stock uses 0 or -1 for "none")')
        self._rep(R, 'error', 'ref.index_range', st, it, oor, r, f, names, 'tagref index beyond tag_count', extra=txt)
        self._rep(R, 'error', 'ref.dangling', st, it, mism, r, f, names, 'tagref datum does not match the live instance (empty slot / wrong salt)', extra=txt)
        if g is not None:
            good = ~oor & ~mism
            gg = g[nn]
            okg = (gg == mv.inst_group[safe]) | (gg == mv.inst_parent[safe]) | (gg == mv.inst_grand[safe])
            self._rep(R, 'error', 'ref.group', st, it, good & ~okg, r, f, names,
                      'tagref group is neither the target group nor its parent/grandparent',
                      extra=lambda j: 'stored %s target %s' % (fourcc_str(struct.pack('<I', int(gg[j]))),
                                                             fourcc_str(struct.pack('<I', int(mv.inst_group[safe[j]])))))

    # ---- string ids -----------------------------------------------------------------------------------
    def _check_sids(self, R, mv, st, it, v, names, k):
        nzi = np.nonzero(v != 0)[0]
        if not len(nzi):
            return
        R.stats['stringids'] += len(nzi)
        sid = v[nzi]
        i = (sid & 0xFFFFFF).astype(np.int64)
        ln = (sid >> 24).astype(np.int64)
        r, f = nzi // k, nzi % k
        cnt = len(mv.str_lens)
        oor = i >= mv.str_count
        self._rep(R, 'error', 'sid.range', st, it, oor, r, f, names, 'string id index beyond the string table',
                  extra=lambda j: 'id %#010x (table has %d)' % (int(sid[j]), mv.str_count))
        if cnt:
            ok = ~oor & (i < cnt)
            lens = mv.str_lens[np.where(ok, i, 0)]
            self._rep(R, 'error', 'sid.length', st, it, ok & (lens != ln), r, f, names, 'string id length byte != strlen(string)',
                      extra=lambda j: 'id %#010x strlen %d' % (int(sid[j]), int(lens[j])))

    # ---- datarefs ----------------------------------------------------------------------------------------
    def _datarefs(self, R, mv, st, it, size, addr, k):
        p = it.plan
        nzs = np.nonzero((size != 0) | (addr != 0))[0]
        if not len(nzs):
            return
        size, addr = size[nzs], addr[nzs]
        r, f = nzs // k, nzs % k
        self._rep(R, 'error', 'walk.dataref_negative', st, it, size < 0, r, f, p.d_name, 'negative dataref size')
        self._rep(R, 'error', 'walk.dataref_null_address', st, it, (size > 0) & (addr == 0), r, f, p.d_name, 'dataref with size but no address')
        self._rep(R, 'warning', 'walk.empty_dataref_address', st, it, (size == 0) & (addr != 0), r, f, p.d_name, 'empty dataref keeps an address')
        ok = (size > 0) & (addr != 0)
        if not ok.any():
            return
        size, addr, r, f = size[ok].astype(np.int64), addr[ok].astype(np.int64), r[ok], f[ok]
        R.stats['datarefs'] += len(size)
        key = (addr << 31) | size
        _uk, first = np.unique(key, return_index=True)
        ua, ue, ur, uf = addr[first], addr[first] + size[first], r[first], f[first]
        own = self._classify(R, mv, st, it, ua, ue, uf, ur, p.d_name, p.d_A[uf], p.d_An[uf], 'dataref')
        if own.any():
            st.s.append(ua[own])
            st.e.append(ue[own])
            st.a.append(p.d_A[uf][own])

    # ---- tagblocks ---------------------------------------------------------------------------------------------
    def _blocks(self, R, mv, st, it, cnt, addr, r, f, stack):
        p = it.plan
        names = p.b_name
        neg = cnt < 0
        self._rep(R, 'error', 'walk.count_negative', st, it, neg, r, f, names, 'negative block count', extra=lambda j: 'count %d' % int(cnt[j]))
        self._rep(R, 'error', 'walk.block_null_address', st, it, (cnt > 0) & (addr == 0), r, f, names, 'block with a count but no address')
        self._rep(R, 'warning', 'walk.empty_block_address', st, it, (cnt == 0) & (addr != 0), r, f, names, 'empty block keeps an address')
        ok = (cnt > 0) & (addr != 0)
        if not ok.any():
            return
        cnt, addr, r, f = cnt[ok].astype(np.int64), addr[ok].astype(np.int64), r[ok], f[ok]
        ces = p.b_ces[f]
        z = ces == 0
        if z.any():
            R.note('walk.zero_size_element', 'block %s%s has elementSize 0 but count > 0' % (it.path, names[int(f[z][0])]), tag=st.tag)
            cnt, addr, r, f, ces = cnt[~z], addr[~z], r[~z], f[~z], ces[~z]
            if not len(cnt):
                return
        R.stats['blocks'] += len(cnt)
        R.stats['elements'] += int(cnt.sum())
        key = (addr << 31) | cnt
        _uk, first = np.unique(key, return_index=True)
        ua, uc, ur, uf = addr[first], cnt[first], r[first], f[first]
        ue = ua + uc * p.b_ces[uf]
        own = self._classify(R, mv, st, it, ua, ue, uf, ur, names, p.b_A[uf], p.b_An[uf], 'block')
        if not own.any():
            return
        st.s.append(ua[own])
        st.e.append(ue[own])
        st.a.append(p.b_A[uf][own])
        oa, oc, of_ = ua[own], uc[own], uf[own]
        for fi in np.unique(of_).tolist():
            child = p.b_child[fi]
            if not (child.needs_rows or (self.check_floats and child.has_floats) or
                    (child._enum is not None and (self.check_enums or self._learn is not None))):
                continue
            m = of_ == fi
            a_sel, c_sel = oa[m], oc[m]
            keep = np.asarray([(int(a), int(c), id(child)) not in st.seen for a, c in zip(a_sel.tolist(), c_sel.tolist())], dtype=bool)
            if not keep.any():
                continue
            a_sel, c_sel = a_sel[keep], c_sel[keep]
            for a, c in zip(a_sel.tolist(), c_sel.tolist()):
                st.seen.add((a, c, id(child)))
            rows2 = gather_rows(mv.buf, mv.toff + a_sel - mv.mask, c_sel, child.es)
            seg = np.cumsum(c_sel) - c_sel
            total = int(c_sel.sum())
            row_addr = np.repeat(a_sel, c_sel) + (np.arange(total, dtype=np.int64) - np.repeat(seg, c_sel)) * child.es
            stack.append(Item(child, rows2, row_addr, seg, a_sel))

    # ---- placement of a block / dataref range -----------------------------------------------------------------
    def _classify(self, R, mv, st, it, ua, ue, uf, ur, names, A, An, kind):
        """Classify ranges [ua, ue): returns the mask of ranges inside the tag's OWN extent.  Reports out-of-region,
        straddling, dangling, forward-share and misaligned ranges."""
        lo, hi = mv.region_lo, mv.region_hi
        tag = st.tag
        oor = (ua < lo) | (ue > hi)
        self._rep(R, 'error', 'walk.out_of_region', st, it, oor, ur, uf, names, kind + ' lies outside the tag region',
                  extra=lambda j: '[%#x, %#x) region [%#x, %#x)' % (int(ua[j]), int(ue[j]), lo, hi))
        own = (ua >= st.lo) & (ue <= st.hi) & ~oor
        strad = (ua < st.hi) & (ue > st.lo) & ~own & ~oor
        self._rep(R, 'error', 'walk.straddles_tag', st, it, strad, ur, uf, names, kind + ' straddles the tag boundary',
                  extra=lambda j: '[%#x, %#x) tag [%#x, %#x)' % (int(ua[j]), int(ue[j]), st.lo, st.hi))
        other = ~(own | strad | oor)
        if other.any():
            oi = np.nonzero(other)[0]
            o = np.searchsorted(mv.ex_start, ua[oi], side='right') - 1
            has = o >= 0
            oo = np.where(has, o, 0)
            inside = has & (ue[oi] <= mv.ex_end[oo])
            owner = mv.ex_idx[oo]
            bad = np.zeros(len(ua), dtype=bool)
            bad[oi[~inside]] = True
            self._rep(R, 'error', 'walk.dangling', st, it, bad, ur, uf, names, kind + ' points outside every tag extent',
                      extra=lambda j: '[%#x, %#x)' % (int(ua[j]), int(ue[j])))
            fwd = np.zeros(len(ua), dtype=bool)
            fwd[oi[inside & (owner > tag.index)]] = True
            self._rep(R, 'warning', 'walk.share_forward', st, it, fwd, ur, uf, names, kind + ' shares data of a LATER tag (stock only shares earlier tags)')
            R.stats['shared_ranges'] += int(inside.sum())
        mis = own & ((ua % A) != 0)
        if mis.any():
            strong = mis & (A >= 16) & (An >= 4)
            self._rep(R, 'error', 'walk.align', st, it, strong, ur, uf, names, kind + ' start violates its >=16-byte alignment',
                      extra=lambda j: 'addr %#x needs %d' % (int(ua[j]), int(A[j])))
            self._rep(R, 'warning', 'walk.align_soft', st, it, mis & ~strong, ur, uf, names, kind + ' start violates the AlignTable alignment',
                      extra=lambda j: 'addr %#x needs %d' % (int(ua[j]), int(A[j])))
        return own

    # ---- tiling of the tag extent ---------------------------------------------------------------------------------
    def _tiling(self, R, mv, st):
        tag, lay = st.tag, st.layout
        bs = lay.base_size or lay.computed_size
        S = np.concatenate([np.array([st.base], dtype=np.int64)] + st.s)
        E = np.concatenate([np.array([st.base + bs], dtype=np.int64)] + st.e)
        A = np.concatenate([np.array([1], dtype=np.int64)] + st.a)
        o = np.lexsort((E, S))
        S, E, A = S[o], E[o], A[o]
        if len(S) > 1:
            keep = np.ones(len(S), dtype=bool)
            keep[1:] = (S[1:] != S[:-1]) | (E[1:] != E[:-1])
            S, E, A = S[keep], E[keep], A[keep]
        pe = np.maximum.accumulate(E)
        if len(S) > 1:
            prev, nxt, an = pe[:-1], S[1:], A[1:]
            ov = nxt < prev
            if ov.any():
                j = int(np.nonzero(ov)[0][0])
                R.error('tile.overlap', '%d overlapping block/dataref ranges inside the tag (first: [%#x,%#x) vs previous end %#x)' % (
                    int(ov.sum()), S[j + 1], E[j + 1], prev[j]), tag=tag)
            gap = nxt - prev
            expect = (prev + an - 1) & ~(an - 1)
            slack = (gap > 0) & (nxt != expect)
            far = slack & (gap >= 64)
            if far.any():
                j = int(np.nonzero(far)[0][0])
                R.error('tile.gap', '%d unreferenced gaps >= 64 bytes inside the tag (first: %d bytes at %#x)' % (int(far.sum()), gap[j], prev[j]), tag=tag)
            near = slack & ~far
            if near.any():
                R.note('tile.order', '%d block starts differ from align_up(previous end, AlignTable) by < 64 bytes' % int(near.sum()), tag=tag)
        last = int(pe[-1])
        want = align_up(last, 4) + (lay.tail_pad or 0)          # absolute address where the tag should end
        if st.hi > want:
            g = st.hi - want
            if g >= 64:
                R.error('tile.tail_gap', 'tag has %d unreferenced bytes after its last block/dataref' % g, tag=tag)
            else:
                R.note('tile.tail', 'tag has %d slack bytes after its last block/dataref' % g, tag=tag)
        elif st.hi < want:
            if st.hi >= align_up(last, 4):
                R.note('tile.tail_pad_missing', 'tag lacks the %d-byte constant tail of %s' % (lay.tail_pad, tag.group), tag=tag)
            else:
                R.error('tile.short', 'tag ends %d bytes before the last block/dataref ends' % (want - st.hi), tag=tag)

    # ==============================================================================================
    # raw pointers
    # ==============================================================================================
    def _resolve_loc(self, R, mv, ptr, what, tag, where):
        """Raw pointer (loc<<30 | offset) -> ('self'|'ext'|'bad'|'unavail', ExtDB or None, offset)."""
        h = mv.h
        loc = (ptr >> 30) & 3
        off = ptr & 0x3FFFFFFF
        if loc == 0 or SELF_LOC.get(h.type) == loc:
            return 'self', None, off
        if h.type == 1 and loc == 3:
            R.error('raw.mp_sp_shared', '%s points into single_player_shared.map (never allowed for multiplayer maps)' % what, tag=tag, path=where)
            return 'bad', None, off
        if not h.uses_shared_map[loc - 1]:
            R.error('raw.db_not_flagged', '%s points into %s but header.uses_shared_map[%d] is 0' % (what, DB_FILES[loc], loc - 1), tag=tag, path=where)
            return 'bad', None, off
        db = self.ext(DB_FILES[loc])
        if not db.exists:
            R.stats['raw_db_unavailable'] += 1
            return 'unavail', None, off
        return 'ext', db, off

    def _blkh_ok(self, buf, off, size, rds):
        if off + size > len(buf):
            return 'block [%#x, +%#x) beyond the file' % (off, size)
        head = bytes(buf[off:off + 8])
        if head[:4] != b'hklb':
            return "block does not start with 'blkh' (bytes %s)" % head[:4].hex()
        if struct.unpack_from('<I', head, 4)[0] != rds:
            return 'blkh header resource size %#x != tag field %#x' % (struct.unpack_from('<I', head, 4)[0], rds)
        if bytes(buf[off + size - 4:off + size]) != b'fklb':
            return "block does not end with 'fklb'"
        return ''

    def _geo_sites(self, R, mv, st, it):
        p, rows = it.plan, it.rows
        tag = st.tag
        for off in p.geo:
            sub = np.ascontiguousarray(rows[:, off:off + 16]).view('<u4').reshape(-1, 4)
            nzi = np.nonzero((sub[:, 1] != 0) | ((sub[:, 0] != 0) & (sub[:, 0] != NULL)))[0]
            for j in nzi.tolist():
                ptr, size, sds, rds = (int(x) for x in sub[j])
                where = '%s@+%#x %s' % (it.path or '/', off, it.describe(j))
                if size == 0:
                    R.warn('geo.empty_with_offset', 'geometry block has offset %#x but size 0' % ptr, tag=tag, path=where)
                    continue
                R.stats['geo_blocks'] += 1
                if it.rows.shape[1] >= off + 0x1C:
                    owner = struct.unpack_from('<I', bytes(it.rows[j, off + 0x18:off + 0x1C]))[0]
                    if owner != tag.datum:
                        R.error('geo.owner', 'geometry_block_info owner tag %#x != the datum of the owning tag %#x' % (owner, tag.datum), tag=tag, path=where)
                if size != 8 + sds + rds + 4:
                    R.error('geo.size_formula', 'block_size %#x != 8 + section %#x + resource %#x + 4' % (size, sds, rds), tag=tag, path=where)
                kind, db, o = self._resolve_loc(R, mv, ptr, 'geometry block', tag, where)
                if kind in ('bad', 'unavail'):
                    continue
                if o % PAGE:
                    R.error('geo.align', 'geometry block offset %#x is not 0x200-aligned' % o, tag=tag, path=where)
                if kind == 'self':
                    if not (mv.geo_lo <= o and o + size <= mv.geo_hi):
                        R.error('geo.region', 'geometry block [%#x, +%#x) outside the geometry region [%#x, %#x)' % (o, size, mv.geo_lo, mv.geo_hi), tag=tag, path=where)
                        continue
                    self.geo_blocks.setdefault((o, size), (tag.index, where))
                    why = self._blkh_ok(mv.buf, o, size, rds)
                else:
                    key = (db.path, o, size, rds)
                    if key not in self._zmemo:
                        self._zmemo[key] = self._blkh_ok(db.buf, o, size, rds)
                    why = self._zmemo[key]
                if why:
                    R.error('geo.block', why, tag=tag, path=where)
                else:
                    self._geo_resources(R, mv, st, it, j, off, rds, sds, where)

    def _geo_resources(self, R, mv, st, it, j, off, rds, sds, where):
        """The 16-byte fixup entries of a geometry_block_info (Resources tagblock @+0x10 of the site)."""
        row = it.rows[j]
        cnt, ptr = struct.unpack_from('<iI', bytes(row[off + 0x10:off + 0x18]))
        if cnt <= 0 or ptr == 0:
            return
        if ptr < mv.region_lo or ptr + 16 * cnt > mv.region_hi:
            return                      # reported by the generic block walk
        ent = mv.buf[mv.fo(ptr):mv.fo(ptr) + 16 * cnt].reshape(cnt, 16)
        typ = ent[:, 0]
        if (typ > 2).any():
            R.error('geo.resource_type', '%d geometry resource entries with type > 2' % int((typ > 2).sum()), tag=st.tag, path=where)
        w = np.ascontiguousarray(ent[:, 8:16]).view('<u4').reshape(-1, 2)
        over = w[:, 1].astype(np.int64) + w[:, 0] > rds
        if over.any():
            R.error('geo.resource_range', '%d geometry resource fixups end beyond the resource data (%#x)' % (int(over.sum()), rds), tag=st.tag, path=where)

    # ---- bitmaps ------------------------------------------------------------------------------------------------------
    def _hook_bitmaps(self, R, mv, st, it):
        rows = np.ascontiguousarray(it.rows)
        if rows.shape[1] != 0x74:
            return
        tag = st.tag
        u32 = rows.view('<u4')
        i16 = rows.view('<i2')
        n = rows.shape[0]
        R.stats['bitmap_elements'] += n
        bad_par = u32[:, 19] != np.uint32(tag.datum)
        self._rep(R, 'error', 'bitmap.parent_datum', st, it, bad_par, np.arange(n), None, None, 'bitmap element parent datum != owning tag datum',
                  extra=lambda j: 'datum %#x vs tag %#x' % (int(u32[j, 19]), tag.datum))
        extra = (u32[:, 8] != 0) | (u32[:, 9] != 0) | (u32[:, 14] != 0) | (u32[:, 15] != 0)
        self._rep(R, 'warning', 'bitmap.lod23', st, it, extra, np.arange(n), None, None, 'bitmap lod2/lod3 offset/size must be 0 (Vista uses one resource)')
        for j in range(n):
            ptr, size = int(u32[j, 7]), int(u32[j, 13])
            where = '%s %s' % (it.path, it.describe(j))
            w, hgt, dep = int(i16[j, 2]), int(i16[j, 3]), int(rows[j, 8])
            typ, fmt, mips = int(i16[j, 5]), int(i16[j, 6]), int(i16[j, 10])
            if size == 0:
                R.warn('bitmap.no_data', 'bitmap element has no pixel data (offset %#x size 0)' % ptr, tag=tag, path=where)
                continue
            expect = bitmap_raw_size(w, hgt, max(dep, 1), fmt, mips, typ)
            if expect is None or typ not in (0, 1, 2):
                R.error('bitmap.format', 'unknown bitmap format %d / type %d' % (fmt, typ), tag=tag, path=where)
                continue
            kind, db, o = self._resolve_loc(R, mv, ptr, 'bitmap pixels', tag, where)
            if kind in ('bad', 'unavail'):
                continue
            if o % PAGE:
                R.error('bitmap.align', 'bitmap pixel offset %#x is not 0x200-aligned' % o, tag=tag, path=where)
            if kind == 'self':
                if not (mv.bitmap_lo <= o and o + size <= mv.bitmap_hi):
                    R.error('bitmap.region', 'bitmap pixels [%#x, +%#x) outside the bitmap region [%#x, %#x)' % (o, size, mv.bitmap_lo, mv.bitmap_hi), tag=tag, path=where)
                    continue
                self.bitmap_blobs.setdefault((o, size), (tag.index, where))
                buf, owner = mv.buf, mv.path
            else:
                if o + size > db.size:
                    R.error('bitmap.region', 'bitmap pixels [%#x, +%#x) beyond %s (%#x bytes)' % (o, size, os.path.basename(db.path), db.size), tag=tag, path=where)
                    continue
                buf, owner = db.buf, db.path
            key = (owner, o, size, expect, self.raw_decode)
            memo = self._zself if kind == 'self' else self._zmemo          # only external databases are immutable across calls
            why = memo.get(key)
            if why is None:
                why = self._zcheck(buf, o, size, expect)
                memo[key] = why
            if why:
                R.error('bitmap.zlib', why, tag=tag, path=where)

    def _zcheck(self, buf, o, size, expect):
        head = bytes(buf[o:o + 2])
        if head != b'\x78\xda':
            return 'bitmap blob does not start with the zlib header 78 DA (got %s)' % head.hex()
        if not self.raw_decode:
            return ''
        d = zlib.decompressobj()
        try:
            out = d.decompress(bytes(buf[o:o + size]))
        except zlib.error as e:
            return 'bitmap blob is not a valid zlib stream: %s' % e
        if not d.eof:
            return 'bitmap zlib stream is truncated by the declared size %#x' % size
        if d.unused_data:
            return 'bitmap zlib stream ends %d bytes before the declared size' % len(d.unused_data)
        if len(out) != expect:
            return 'bitmap inflates to %d bytes, the engine expects %d' % (len(out), expect)
        return ''

    # ---- region-level raw checks ----------------------------------------------------------------------------------------
    def _check_geometry_region(self, R, mv):
        blocks = sorted(self.geo_blocks)
        if not blocks:
            if mv.geo_hi > mv.geo_lo:
                R.warn('geo.region_unreferenced', 'geometry region [%#x, %#x) is declared but no tag references a block in it' % (mv.geo_lo, mv.geo_hi))
            return
        prev_end = mv.geo_lo
        gap_bytes = 0
        for (o, s) in blocks:
            if o < prev_end and not (o == blocks[0][0]):
                R.error('geo.overlap', 'geometry blocks overlap at %#x (previous block padded end %#x)' % (o, prev_end))
            elif o > prev_end:
                gap_bytes += o - prev_end
            prev_end = max(prev_end, align_up(o + s, PAGE))
        if prev_end < mv.geo_hi:
            gap_bytes += mv.geo_hi - prev_end
        if gap_bytes:
            R.warn('geo.unreferenced', '%#x bytes of the geometry region are not covered by any block referenced from this map' % gap_bytes)
        R.stats['geo_region_blocks'] = len(blocks)

    def _check_bitmap_region(self, R, mv):
        blobs = sorted(self.bitmap_blobs)
        if not blobs:
            return
        prev_end = 0
        gap_bytes = 0
        for (o, s) in blobs:
            if prev_end and o < prev_end:
                R.error('bitmap.overlap', 'bitmap blobs overlap at %#x (previous padded end %#x)' % (o, prev_end))
            elif prev_end and o > prev_end:
                gap_bytes += o - prev_end
            prev_end = max(prev_end, align_up(o + s, PAGE))
        if blobs[0][0] != mv.bitmap_lo:
            R.note('bitmap.region_start', 'first local bitmap blob at %#x, region starts at %#x' % (blobs[0][0], mv.bitmap_lo))
        if prev_end < mv.bitmap_hi:
            gap_bytes += mv.bitmap_hi - prev_end
        if gap_bytes:
            R.note('bitmap.unreferenced', '%#x bytes of the bitmap region are not covered by any local bitmap element' % gap_bytes)
        R.stats['bitmap_blobs'] = len(blobs)


class _SoundMixin(object):
    # ==============================================================================================
    # sounds: ugh! (sound cache file gestalt) and snd! consistency
    # ==============================================================================================
    def _check_globals(self, R, mv):
        """matg sound gestalt datum -> a live ugh!, scnr structure-bsp offsets (unused by the engine) are zero."""
        for t in mv.tag_by_index:
            if t.datum == NULL or not t.addr or not t.size:
                continue
            if t.group == 'matg' and t.size >= 0xC8:
                fo = mv.fo(t.addr)
                cnt, ptr = struct.unpack_from('<iI', bytes(mv.buf[fo + 0xC0:fo + 0xC8]))
                if cnt > 0 and ptr >= mv.region_lo and ptr + cnt * 0x24 <= mv.region_hi:
                    for e in range(cnt):
                        d = struct.unpack_from('<I', bytes(mv.buf[mv.fo(ptr) + e * 0x24 + 0x20:mv.fo(ptr) + e * 0x24 + 0x24]))[0]
                        i = d & 0xFFFF
                        if d == NULL or i >= mv.ntags or mv.inst_datum[i] != d or mv.inst_group[i] != _fcc('ugh!'):
                            R.error('matg.gestalt', 'matg sound_globals[%d].Sound Gestalt %#x does not reference a live ugh!' % (e, d), tag=t)
            elif t.group == 'scnr' and t.size >= 0x218:
                fo = mv.fo(t.addr)
                cnt, ptr = struct.unpack_from('<iI', bytes(mv.buf[fo + 0x210:fo + 0x218]))
                if 0 < cnt < 4096 and ptr >= mv.region_lo and ptr + cnt * 0x44 <= mv.region_hi:
                    blob = mv.buf[mv.fo(ptr):mv.fo(ptr) + cnt * 0x44].reshape(cnt, 0x44)
                    if blob[:, :12].any():
                        R.warn('scnr.bsp_offsets', 'scnr.structure_bsps offset/size/address are not 0 (Vista never uses them)', tag=t, flags=('engine-ignored',))

    def _ugh_arrays(self, R, mv, tag):
        if tag.size < 0x58:
            return None
        fo = mv.fo(tag.addr)
        root = bytes(mv.buf[fo:fo + 0x58])
        out = {}
        for name, off, es in UGH_BLOCKS:
            cnt, ptr = struct.unpack_from('<iI', root, off)
            if cnt <= 0 or ptr == 0:
                out[name] = np.zeros((0, es), dtype=np.uint8)
            elif ptr < mv.region_lo or ptr + cnt * es > mv.region_hi:
                out[name] = None
            else:
                o = mv.fo(ptr)
                out[name] = mv.buf[o:o + cnt * es].reshape(cnt, es)
        return out

    def _check_sounds(self, R, mv):
        ughs = [t for t in mv.tag_by_index if t.datum != NULL and t.group == 'ugh!' and t.addr and t.size]
        snds = [t for t in mv.tag_by_index if t.datum != NULL and t.group == 'snd!' and t.addr and t.size]
        R.stats['ugh_tags'] = len(ughs)
        R.stats['snd_tags'] = len(snds)
        if not ughs:
            if snds:
                R.note('sound.no_local_gestalt', '%d local snd! tags but no local ugh! (they index the shared gestalt - not verified)' % len(snds))
            return
        self.sound_ranges = {}
        arrs = []
        for t in ughs:
            A = self._ugh_arrays(R, mv, t)
            if A is None or any(v is None for v in A.values()):
                R.error('ugh.layout', 'ugh! tag is too small or has block pointers outside the tag region', tag=t)
                continue
            arrs.append((t, A))
            self._check_ugh(R, mv, t, A)
        self._check_sound_region(R, mv)
        if len(arrs) == 1 and snds:
            self._check_snds(R, mv, snds, arrs[0][1], arrs[0][0])
        elif len(arrs) > 1 and snds:
            R.note('sound.multiple_gestalt', '%d local ugh! tags - snd! indices not verified' % len(arrs))

    def _check_ugh(self, R, mv, tag, A):
        n_imp, n_par = len(A['import_names']), len(A['pitch_range_params'])
        pr = np.ascontiguousarray(A['pitch_ranges']).view('<i2').reshape(-1, 6) if len(A['pitch_ranges']) else np.zeros((0, 6), np.int16)
        pm = A['permutations']
        ch = A['chunks']
        n_perm, n_ch = len(pm), len(ch)
        # ---- chunks
        if n_ch:
            cu = np.ascontiguousarray(ch).view('<u4').reshape(-1, 3)
            size = (cu[:, 1] & 0x7FFFFFFF).astype(np.int64)
            ptr = cu[:, 0]
            loc = (ptr >> 30).astype(np.int64)
            off = (ptr & 0x3FFFFFFF).astype(np.int64)
            if (size == 0).any():
                R.error('ugh.chunk_size', '%d sound chunks have size 0' % int((size == 0).sum()), tag=tag)
            if (cu[:, 2] != NULL).any():
                R.warn('ugh.chunk_runtime', '%d sound chunks have runtime index != -1' % int((cu[:, 2] != NULL).sum()), tag=tag)
            if (off % PAGE != 0).any():
                R.error('ugh.chunk_align', '%d sound chunks are not 0x200-aligned (first offset %#x)' % (int((off % PAGE != 0).sum()), int(off[off % PAGE != 0][0])), tag=tag)
            for lv in np.unique(loc).tolist():
                m = loc == lv
                kind, db, _o = self._resolve_loc(R, mv, int(lv) << 30, 'sound chunks (%d)' % int(m.sum()), tag, '')
                if kind == 'self':
                    bad = m & ((off < mv.sound_lo) | (off + size > mv.sound_hi))
                    if bad.any():
                        j = int(np.nonzero(bad)[0][0])
                        R.error('ugh.chunk_region', '%d local sound chunks outside the raw-sound region [%#x, %#x) (first: chunk %d [%#x, +%#x))' % (
                            int(bad.sum()), mv.sound_lo, mv.sound_hi, j, off[j], size[j]), tag=tag)
                    sel = m & ~bad
                    for o_, s_ in zip(off[sel].tolist(), size[sel].tolist()):
                        self.sound_ranges[(o_, s_)] = 1
                elif kind == 'ext':
                    bad = m & (off + size > db.size)
                    if bad.any():
                        R.error('ugh.chunk_region', '%d sound chunks beyond %s' % (int(bad.sum()), os.path.basename(db.path)), tag=tag)
            R.stats['sound_chunks'] += n_ch
        # ---- permutations
        if n_perm:
            p16 = np.ascontiguousarray(pm).view('<i2').reshape(-1, 8)
            p32 = np.ascontiguousarray(pm).view('<u4').reshape(-1, 4)
            imp = pm[:, 0].astype(np.int64) | (pm[:, 1].astype(np.int64) << 8)
            first, cnt = p16[:, 6].astype(np.int64), p16[:, 7].astype(np.int64)
            ssize = p32[:, 2].astype(np.int64)
            if (imp >= max(n_imp, 1)).any() and n_imp:
                R.error('ugh.perm_import_name', '%d permutations with import name index >= %d' % (int((imp >= n_imp).sum()), n_imp), tag=tag)
            badr = (first < 0) | (cnt < 1) | (first + cnt > n_ch)
            if badr.any():
                j = int(np.nonzero(badr)[0][0])
                R.error('ugh.perm_chunks', '%d permutations with a bad chunk range (first: perm %d first %d count %d of %d chunks)' % (
                    int(badr.sum()), j, first[j], cnt[j], n_ch), tag=tag)
            good = ~badr
            if good.any() and n_ch:
                cs = np.concatenate([[0], np.cumsum(size)])
                tot = cs[np.where(good, first + cnt, 0)] - cs[np.where(good, first, 0)]
                mism = good & (tot != ssize)
                if mism.any():
                    j = int(np.nonzero(mism)[0][0])
                    R.error('ugh.perm_sample_size', '%d permutations whose Sample Size != sum of its chunk sizes (first: perm %d size %d vs chunks %d)' % (
                        int(mism.sum()), j, ssize[j], tot[j]), tag=tag)
        # ---- pitch ranges
        if len(pr):
            imp, par, enc, bit, first, cnt = (pr[:, i].astype(np.int64) for i in range(6))
            if n_imp and ((imp < -1) | (imp >= n_imp)).any():
                R.error('ugh.pr_import_name', '%d pitch ranges with a bad import name index' % int(((imp < -1) | (imp >= n_imp)).sum()), tag=tag)
            badp = (par < 0) | (par >= n_par)
            if badp.any():
                R.error('ugh.pr_params', '%d pitch ranges with a bad parameters index (%d params)' % (int(badp.sum()), n_par), tag=tag)
            badr = (first < 0) | (cnt < 0) | (first + cnt > n_perm)
            if badr.any():
                j = int(np.nonzero(badr)[0][0])
                R.error('ugh.pr_perms', '%d pitch ranges with a bad permutation range (first: pr %d first %d count %d of %d)' % (
                    int(badr.sum()), j, first[j], cnt[j], n_perm), tag=tag)
            if ((enc < -1) | (enc >= max(len(A['extra_info']), 1))).any() and (enc != -1).any():
                R.warn('ugh.pr_encoded', 'pitch ranges with an encoded-permutation index other than -1', tag=tag)
            # runtime permutation flag bits (measured on 6 stock Vista maps: 3059 + 6843 consecutive
            # allocations, 6/6 array sizes).  TWO independent sequences:
            #   * encoded index == -1 (normal): bit = running sum of ceil(n/8)*8; the total / 8 is the size of runtime_perm_flags;
            #   * encoded index != -1 and bit != -1 (lip-sync dialogue with Extra Info): a SECOND running sum from 0 with
            #     n + 5 + max(1, ceil(log2 n)) bits per range, NOT part of the array;
            #   * one permutation, no lip-sync: bit = -1.
            nbits = 8 * len(A['runtime_perm_flags'])
            exp = np.full(len(cnt), -1, dtype=np.int64)
            run_n = run_e = 0
            for q in range(len(cnt)):
                n_ = int(cnt[q])
                if enc[q] == -1:
                    if n_ > 1:
                        exp[q] = run_n
                        run_n += ((n_ + 7) // 8) * 8
                elif bit[q] != -1:
                    exp[q] = run_e
                    run_e += n_ + 5 + max(1, (max(n_, 1) - 1).bit_length())
            nbad = int((exp != bit).sum())
            if nbad or run_n != nbits:
                R.note('ugh.pr_runtime_bit_rule', '%d pitch ranges deviate from the two-sequence runtime-flag rule (normal: running sum of ceil(n/8)*8 == the %d-bit '
                       'array, got %d; lip-sync: n+5+ceil(log2 n))' % (nbad, nbits, run_n), tag=tag)
        # ---- extra info owner datum (geometry_block_info.owner tag == this ugh!)
        ex = A['extra_info']
        if len(ex):
            own = np.ascontiguousarray(ex[:, 0x20:0x24]).view('<u4').ravel()
            bad = own != np.uint32(tag.datum)
            if bad.any():
                R.warn('ugh.extra_owner', '%d extra_info elements whose Owner Tag != this ugh! datum' % int(bad.sum()), tag=tag)

    def _check_sound_region(self, R, mv):
        rng = sorted(self.sound_ranges)
        if not rng:
            return
        prev_end = mv.sound_lo
        gaps = 0
        for (o, s) in rng:
            if o < prev_end and o != rng[0][0]:
                R.error('ugh.chunk_overlap', 'sound chunks overlap at %#x (previous padded end %#x)' % (o, prev_end))
            elif o > prev_end:
                gaps += o - prev_end
            prev_end = max(prev_end, align_up(o + s, PAGE))
        if prev_end < mv.sound_hi:
            gaps += mv.sound_hi - prev_end
        if gaps:
            R.warn('ugh.sound_region_gaps', '%#x bytes of the raw-sound region [%#x, %#x) are not covered by any local chunk' % (gaps, mv.sound_lo, mv.sound_hi))

    def _chunk_view(self, mv, ptr, size):
        """Bytes of a sound chunk (any location), or None when it cannot be read (reported by the chunk checks)."""
        loc, off = (ptr >> 30) & 3, ptr & 0x3FFFFFFF
        if loc == 0 or SELF_LOC.get(mv.h.type) == loc:
            buf = mv.buf
        else:
            if not mv.h.uses_shared_map[loc - 1] or (mv.h.type == 1 and loc == 3):
                return None
            db = self.ext(DB_FILES[loc])
            if not db.exists:
                return None
            buf = db.buf
        if off + size > len(buf):
            return None
        return buf[off:off + size]

    @staticmethod
    def _adpcm_bad(view, stereo):
        """Index of the first Xbox ADPCM block with an invalid header, else -1.  Per channel: u8 step index <= 88,
        reserved byte 0, nibble 63 (high nibble of the channel's last byte) 0 - true for 556,460/556,460 stock blocks."""
        blk = 72 if stereo else 36
        n = len(view) // blk
        if n == 0:
            return -1
        a = view[:n * blk].reshape(n, blk)
        if stereo:
            bad = (a[:, 2] > 88) | (a[:, 3] != 0) | (a[:, 6] > 88) | (a[:, 7] != 0) | ((a[:, 67] >> 4) != 0) | ((a[:, 71] >> 4) != 0)
        else:
            bad = (a[:, 2] > 88) | (a[:, 3] != 0) | ((a[:, 35] >> 4) != 0)
        return int(np.argmax(bad)) if bad.any() else -1

    def _check_snds(self, R, mv, snds, A, ugh):
        pr, perm, ch = A['pitch_ranges'], A['permutations'], A['chunks']
        prv = np.ascontiguousarray(pr).view('<i2').reshape(-1, 6) if len(pr) else np.zeros((0, 6), np.int16)
        pv = np.ascontiguousarray(perm).view('<i2').reshape(-1, 8) if len(perm) else np.zeros((0, 8), np.int16)
        cu = np.ascontiguousarray(ch).view('<u4').reshape(-1, 3) if len(ch) else np.zeros((0, 3), np.uint32)
        csz = (cu[:, 1] & 0x7FFFFFFF).astype(np.int64)
        lens = {k: len(v) for k, v in A.items()}
        done = set()
        for t in snds:
            if t.size < 0x14:
                R.error('snd.size', 'snd! tag smaller than 0x14', tag=t)
                continue
            fo = mv.fo(t.addr)
            b = mv.buf[fo:fo + 0x14]
            rate, enc, comp = int(b[3]), int(b[4]), int(b[5])
            pb, pri = struct.unpack_from('<hh', b, 6)
            prc, sc, pm_, cp = int(b[0xA]), struct.unpack_from('<b', b, 0xB)[0], struct.unpack_from('<b', b, 0xC)[0], struct.unpack_from('<b', b, 0xD)[0]
            ex = struct.unpack_from('<h', b, 0xE)[0]
            if rate > 2 or enc > 2 or comp > 4:
                R.error('snd.codec', 'snd! sample rate/encoding/compression %d/%d/%d out of range' % (rate, enc, comp), tag=t)
            elif comp not in (1, 4):
                R.warn('snd.compression', 'snd! compression %d - stock Vista data only uses 1 (Xbox ADPCM) and 4 (WMA)' % comp, tag=t)
            for nm, v, n in (('playback', pb, lens['playbacks']), ('scale', sc, lens['scales']), ('promotion', pm_, lens['promotions']),
                             ('custom_playback', cp, lens['custom_playbacks']), ('extra_info', ex, lens['extra_info'])):
                if v != -1 and not (0 <= v < n):
                    R.error('snd.index', 'snd! %s index %d outside 0..%d' % (nm, v, n), tag=t)
            if prc == 0:
                R.warn('snd.no_pitch_ranges', 'snd! has no pitch ranges', tag=t)
                continue
            if pri < 0 or pri + prc > len(prv):
                R.error('snd.pitch_range_index', 'snd! pitch range %d..+%d outside 0..%d' % (pri, prc, len(prv)), tag=t)
                continue
            if comp == 1 and enc != 2:        # encoding 2 ("codec") + compression 1 is the WMA menu_music label quirk
                blk = 36 * (2 if enc == 1 else 1)
                kind = 'adpcm'
            elif enc == 2:
                blk, kind = 0, 'wma'
            else:
                continue
            for pi_ in range(pri, pri + prc):
                first, cnt = int(prv[pi_, 4]), int(prv[pi_, 5])
                if first < 0 or first + cnt > len(pv):
                    continue
                for q in range(first, first + cnt):
                    c0, cn = int(pv[q, 6]), int(pv[q, 7])
                    if c0 < 0 or cn < 1 or c0 + cn > len(csz) or (kind, blk, c0, cn) in done:
                        continue
                    done.add((kind, blk, c0, cn))
                    if kind == 'wma':
                        if self.check_sound_data:
                            v0 = self._chunk_view(mv, int(cu[c0, 0]), 16)
                            if v0 is not None and bytes(v0) != WMA_SIGNATURE:
                                R.error('snd.wma_signature', "WMA permutation does not start with the 'auds' stream header (got %s)" % bytes(v0).hex(), tag=t)
                        continue
                    if (csz[c0:c0 + cn] % blk).any():
                        R.error('snd.adpcm_block', 'Xbox ADPCM permutation chunk sizes are not multiples of the %d-byte block' % blk, tag=t)
                        continue
                    if (csz[c0:c0 + cn] > 65520).any():
                        R.warn('snd.adpcm_chunk_size', 'Xbox ADPCM chunk larger than 65520 bytes (stock: 0x7FF8 chunks, remainder <= 65520; the tool stores the size in 16 bits)', tag=t)
                    if self.check_sound_data:
                        for c in range(c0, c0 + cn):
                            v_ = self._chunk_view(mv, int(cu[c, 0]), int(csz[c]))
                            if v_ is None:
                                continue
                            R.stats['adpcm_blocks'] += len(v_) // blk
                            bad = self._adpcm_bad(v_, blk == 72)
                            if bad >= 0:
                                R.error('snd.adpcm_data', 'Xbox ADPCM chunk %d block %d has an invalid header (step index > 88, reserved byte != 0 or non-zero padding nibble)' % (c, bad), tag=t)
                                break
            R.stats['snd_checked'] += 1


class Validator(_ValidatorBase, _WalkMixin, _SoundMixin):
    """Reusable validator: Validator(maps_dir).validate(path) -> Report.  Keeps layouts, walk plans, external
    shared-database handles and the bitmap-inflate memo between maps (so a batch over many maps is fast)."""


def validate_map(path, maps_dir=None, raw_decode=True, max_examples=25, walk=True, sounds=True, validator=None, floats=True, enums=True,
                 sound_data=True):
    """Validate one Vista map.  maps_dir = folder holding mainmenu/shared/single_player_shared.map (default: the Vista
    maps folder); raw_decode=False skips inflating bitmap blobs."""
    v = validator or Validator(maps_dir, raw_decode, max_examples, walk, sounds, floats, enums, sound_data)
    try:
        return v.validate(path)
    finally:
        if validator is None:
            v.close()


# ------------------------------------------------------------------------------------------------------------------
# comparer: compare_maps(out, ref)
# ------------------------------------------------------------------------------------------------------------------
import re as _re

FLOAT_TYPES = frozenset(('float32', 'degree', 'point2', 'point3', 'vector2', 'vector3', 'vector4', 'degree2', 'degree3',
                         'plane2', 'plane3', 'rangef', 'colorf'))
RUNTIME_RE = _re.compile(r'\bruntime\b|runtime |user data|code pointer|\bpointer\b|hardware format|shape pad', _re.I)
OPAQUE_NAMES = ('unknown', 'padding', 'pad', '')
# raw-resource sites compared by decoded CONTENT: (group, block path) -> [(pointer offset, size offset, kind, name)]
RAW_SITES = {('bitm', '/Bitmaps'): [(0x1C, 0x34, 'bitmap', 'LOD1 Pixels')],
             ('ugh!', '/Permutation Chunks'): [(0x00, 0x04, 'sound', 'Chunk Data')]}


class FieldStat(object):
    __slots__ = ('total', 'equal', 'near', 'diff', 'cat', 'examples')

    def __init__(self, cat):
        self.total = self.equal = self.near = self.diff = 0
        self.cat = cat
        self.examples = []

    def to_dict(self):
        return {'total': self.total, 'equal': self.equal, 'near': self.near, 'diff': self.diff, 'category': self.cat,
                'pct_equal': round(100.0 * (self.equal + self.near) / self.total, 3) if self.total else None,
                'examples': self.examples}


class CField(object):
    __slots__ = ('off', 'size', 'name', 'type', 'kind', 'cat', 'key', 'float')


class CPlan(object):
    __slots__ = ('group', 'path', 'es', 'leaves', 'blocks', 'datas', 'raws')

    def __init__(self, group, path, es):
        self.group, self.path, self.es = group, path, es
        self.leaves, self.blocks, self.datas, self.raws = [], [], [], []


_CPLANS = None


def _field_cat(f):
    if f.type == 'undefined' or (f.name or '').lower() in OPAQUE_NAMES:
        return 'opaque'
    if RUNTIME_RE.search(f.name or ''):
        return 'runtime'
    return 'data'


def _build_cplan(group, fields, es, path):
    cp = CPlan(group, path, es)
    seen = collections.Counter(f.name for f in fields)
    geo = []
    for f in sorted(fields, key=lambda x: x.offset):
        nm = f.name or 'field'
        key = nm if (seen[f.name] == 1 and (f.name or '').lower() not in OPAQUE_NAMES) else '%s@%#x' % (nm, f.offset)
        t = f.type
        if t == 'tagblock':
            cp.blocks.append((f.offset, key, _build_cplan(group, f.children or [], T.element_size(f) or 0, path + '/' + f.name), T.element_size(f) or 0))
            continue
        if t == 'dataref':
            cp.datas.append((f.offset, key))
            continue
        c = CField()
        c.off, c.size, c.name, c.type, c.key, c.cat = f.offset, f.size, nm, t, key, _field_cat(f)
        c.float = t in FLOAT_TYPES
        if t == 'tagref':
            c.kind = 'tagref' if f.attrs.get('withGroup', True) else 'tagdatum'
        elif t == 'stringid':
            c.kind = 'sid'
        else:
            c.kind = 'raw'
        if t == 'int32' and f.name == 'Resource Block Offset':
            geo.append(f.offset)
        cp.leaves.append(c)
    sites = list(RAW_SITES.get((group, path), []))
    sites += [(o, o + 4, 'geometry', 'Geometry Block') for o in geo]
    for (po, so, kind, name) in sites:
        cp.leaves = [c for c in cp.leaves if not (c.off < po + 4 and po < c.off + c.size) and not (c.off < so + 4 and so < c.off + c.size)]
        cp.raws.append((po, so, kind, name))
    return cp


def compare_plans():
    global _CPLANS
    if _CPLANS is None:
        _CPLANS = {}
        for g, lay in vista_layouts().items():
            _CPLANS[g] = _build_cplan(g, lay.fields, lay.base_size or lay.computed_size, '')
    return _CPLANS


class _Interner(object):
    def __init__(self):
        self.d = {}

    def __call__(self, key):
        v = self.d.get(key)
        if v is None:
            v = len(self.d) + 1
            self.d[key] = v
        return v


class _Src(object):
    """A MapView prepared for comparison: interned target keys of every tag instance and every string."""

    def __init__(self, path, intern):
        self.mv = MapView(path)
        mv = self.mv
        names = mv.c._names
        self.tkey = np.full(mv.ntags, -1, dtype=np.int64)
        for i in np.nonzero(mv.inst_datum != NULL)[0].tolist():
            g = fourcc_str(struct.pack('<I', int(mv.inst_group[i])))
            nm = names[i] if i < len(names) and names[i] else '#%08x' % int(mv.inst_datum[i])
            self.tkey[i] = intern((g, nm))
        try:
            texts = mv.c.all_strings()
        except Exception:
            texts = []
        self.skey = np.asarray([intern(('s', t)) for t in texts] or [0], dtype=np.int64)
        self.slen = mv.str_lens if mv.str_lens is not None else None
        if self.slen is None or len(self.slen) != len(texts):
            self.slen = np.asarray([len(t) for t in texts] or [0], dtype=np.int64)

    def close(self):
        self.mv.close()


class CompareReport(object):
    def __init__(self, out_path, ref_path):
        self.out_path, self.ref_path = out_path, ref_path
        self.fields = collections.OrderedDict()          # (group, path, field) -> FieldStat
        self.counts = collections.OrderedDict()          # (group, path, block) -> FieldStat (element counts per parent)
        self.group_tags = collections.defaultdict(lambda: [0, 0])      # group -> [compared, identical]
        self.tag_results = {}                            # (group, name) -> (identical, ndiff)
        self.only_out, self.only_ref = [], []
        self.linked_equal = 0
        self.linked_diff = []
        self.unresolved = 0
        self.skipped = collections.Counter()
        self.notes = []
        self.seconds = 0.0
        self.raw_mode = True
        self.sounds = None               # filled by compare_maps when both maps hold a local ugh!

    # ---- aggregates ---------------------------------------------------------------------------------
    @property
    def tags_compared(self):
        return sum(v[0] for v in self.group_tags.values())

    @property
    def tags_identical(self):
        return sum(v[1] for v in self.group_tags.values())

    @property
    def totals(self):
        t = collections.Counter()
        for (g, p, f), s in self.fields.items():
            t['fields_total'] += s.total
            t['fields_equal'] += s.equal
            t['fields_near'] += s.near
            t['fields_diff'] += s.diff
            if s.cat == 'data':
                t['sem_total'] += s.total
                t['sem_equal'] += s.equal + s.near
        for s in self.counts.values():
            t['counts_total'] += s.total
            t['counts_equal'] += s.equal
        return t

    def group_summary(self):
        out = collections.OrderedDict()
        agg = collections.defaultdict(collections.Counter)
        for (g, p, f), s in self.fields.items():
            a = agg[g]
            a['total'] += s.total
            a['equal'] += s.equal
            a['near'] += s.near
            a['diff'] += s.diff
            if s.cat == 'data':
                a['sem_total'] += s.total
                a['sem_equal'] += s.equal + s.near
        for (g, p, f), s in self.counts.items():
            agg[g]['count_total'] += s.total
            agg[g]['count_diff'] += s.diff
        for g in sorted(set(agg) | set(self.group_tags)):
            a = agg[g]
            tg = self.group_tags.get(g, [0, 0])
            out[g] = {'tags': tg[0], 'identical': tg[1], 'fields': a['total'], 'equal': a['equal'], 'near': a['near'], 'diff': a['diff'],
                      'pct_equal': round(100.0 * (a['equal'] + a['near']) / a['total'], 3) if a['total'] else None,
                      'pct_semantic': round(100.0 * a['sem_equal'] / a['sem_total'], 3) if a['sem_total'] else None,
                      'count_mismatches': a['count_diff']}
        return out

    def path_summary(self):
        agg = collections.defaultdict(collections.Counter)
        for (g, p, f), s in self.fields.items():
            a = agg[(g, p)]
            a['total'] += s.total
            a['equal'] += s.equal + s.near
            a['diff'] += s.diff
        return agg

    def top_paths(self, n=15):
        """Block paths ranked by the number of differing fields: [(diff, total, group, path)]."""
        rows = [(a['diff'], a['total'], g, p) for (g, p), a in self.path_summary().items() if a['diff']]
        rows.sort(key=lambda r: -r[0])
        return rows[:n]

    def top_mismatches(self, n=25, categories=('data', 'runtime', 'opaque')):
        rows = [(s.diff, k, s) for k, s in self.fields.items() if s.diff and s.cat in categories]
        rows.sort(key=lambda r: -r[0])
        return rows[:n]

    def top_count_mismatches(self, n=15):
        rows = [(s.diff, k, s) for k, s in self.counts.items() if s.diff]
        rows.sort(key=lambda r: -r[0])
        return rows[:n]

    def to_dict(self, top=40, full=False):
        d = self._to_dict(top)
        if full:
            d['fields'] = [{'group': k[0], 'path': k[1], 'field': k[2], **s.to_dict()} for k, s in self.fields.items()]
            d['counts'] = [{'group': k[0], 'path': k[1], 'block': k[2], **s.to_dict()} for k, s in self.counts.items()]
        return d

    def _to_dict(self, top=40):
        return {'out': self.out_path, 'ref': self.ref_path, 'seconds': round(self.seconds, 2),
                'tags': {'compared': self.tags_compared, 'identical': self.tags_identical, 'only_out': self.only_out,
                         'only_ref': self.only_ref, 'linked_equal': self.linked_equal, 'linked_diff': self.linked_diff,
                         'unresolved_shared': self.unresolved, 'skipped': dict(self.skipped)},
                'totals': dict(self.totals), 'groups': self.group_summary(), 'sounds': self.sounds,
                'top_paths': [{'group': g, 'path': p, 'fields': t, 'diff': d, 'pct_equal': round(100.0 * (t - d) / max(1, t), 3)} for d, t, g, p in self.top_paths(top)],
                'top_mismatches': [{'group': k[0], 'path': k[1], 'field': k[2], **s.to_dict()} for _d, k, s in self.top_mismatches(top)],
                'top_count_mismatches': [{'group': k[0], 'path': k[1], 'block': k[2], **s.to_dict()} for _d, k, s in self.top_count_mismatches(top)],
                'tag_results': {'%s|%s' % k: list(v) for k, v in self.tag_results.items() if not v[0]},
                'notes': self.notes}

    def format_text(self, top=25):
        t = self.totals
        L = []
        L.append('compare  out=%s  ref=%s  (%.1fs)' % (os.path.basename(self.out_path), os.path.basename(self.ref_path), self.seconds))
        L.append('tags: compared %d (identical %d = %.1f%%), linked-equal %d, only in out %d, only in ref %d, unresolved shared %d, skipped %s' % (
            self.tags_compared, self.tags_identical, 100.0 * self.tags_identical / max(1, self.tags_compared), self.linked_equal,
            len(self.only_out), len(self.only_ref), self.unresolved, dict(self.skipped)))
        if t['fields_total']:
            L.append('fields: %d compared, %d equal (%.3f%%), %d float-near, %d different;  semantic (non-runtime, non-opaque): %d / %d = %.3f%%' % (
                t['fields_total'], t['fields_equal'], 100.0 * t['fields_equal'] / t['fields_total'], t['fields_near'], t['fields_diff'],
                t['sem_equal'], t['sem_total'], 100.0 * t['sem_equal'] / max(1, t['sem_total'])))
            L.append('block counts: %d parents compared, %d equal (%.3f%%)' % (t['counts_total'], t['counts_equal'], 100.0 * t['counts_equal'] / max(1, t['counts_total'])))
        L.append('')
        L.append('%-6s %6s %9s %10s %9s %9s %8s %8s' % ('group', 'tags', 'identical', 'fields', 'equal', 'diff', '%equal', '%sem'))
        gs = self.group_summary()
        perfect = [g for g, a in gs.items() if a['fields'] and not a['diff'] and not a['count_mismatches']]
        for g, a in sorted(gs.items(), key=lambda kv: (-(kv[1]['diff'] or 0), kv[0])):
            if (not a['fields'] and not a['tags']) or g in perfect:
                continue
            L.append('%-6s %6d %9d %10d %9d %9d %8s %8s' % (g, a['tags'], a['identical'], a['fields'], a['equal'] + a['near'], a['diff'],
                                                           ('%.2f' % a['pct_equal']) if a['pct_equal'] is not None else '-',
                                                           ('%.2f' % a['pct_semantic']) if a['pct_semantic'] is not None else '-'))
        if perfect:
            L.append('%d group(s) 100%% equal: %s' % (len(perfect), ' '.join(perfect)))
        tp = self.top_paths(min(top, 15))
        if tp:
            L.append('')
            L.append('block paths with the most differing fields:')
            for d, t, g, p in tp:
                L.append('  %7d/%-8d %6.2f%% eq  %s | %s' % (d, t, 100.0 * (t - d) / max(1, t), g, p or '/'))
        L.append('')
        L.append('top mismatching fields (group | block path | field):')
        for d, k, s in self.top_mismatches(top):
            ex = s.examples[0] if s.examples else None
            L.append('  %7d/%-8d %6.2f%% eq  [%s] %s | %s | %s' % (s.diff, s.total, 100.0 * (s.equal + s.near) / max(1, s.total), s.cat, k[0], k[1] or '/', k[2]))
            if ex:
                L.append('        e.g. %s elem %s: out=%s ref=%s' % (ex['tag'], ex['elem'], ex['out'], ex['ref']))
        cm = self.top_count_mismatches(10)
        if cm:
            L.append('')
            L.append('top block-count mismatches:')
            for d, k, s in cm:
                ex = s.examples[0] if s.examples else None
                L.append('  %7d/%-8d  %s | %s | %s' % (s.diff, s.total, k[0], k[1] or '/', k[2]) + (
                    ('   e.g. %s: out=%s ref=%s' % (ex['tag'], ex['out'], ex['ref'])) if ex else ''))
        if self.only_out:
            L.append('')
            L.append('only in out (%d): %s%s' % (len(self.only_out), ', '.join('%s %s' % x for x in self.only_out[:12]), ' ...' if len(self.only_out) > 12 else ''))
        if self.only_ref:
            L.append('only in ref (%d): %s%s' % (len(self.only_ref), ', '.join('%s %s' % x for x in self.only_ref[:12]), ' ...' if len(self.only_ref) > 12 else ''))
        if self.sounds:
            sd = self.sounds
            L.append('')
            L.append("sounds (snd! permutations resolved through each map's local ugh!): %d sounds compared, %d with all permutations byte-identical; "
                     'permutations: %d total, %d byte-identical, %d same compressed size but different bytes, %d other; sounds whose permutation counts differ: %d' % (
                         sd['sounds'], sd['sounds_identical'], sd['perms'], sd['perms_identical'], sd['perms_same_size'], sd['perms_other'], sd['perm_count_mismatch']))
            for ex in sd['examples']:
                L.append('   e.g. %s: out=%s ref=%s' % (ex['tag'], ex['out'], ex['ref']))
        for n in self.notes:
            L.append('note: ' + n)
        return '\n'.join(L)


class _Comparer(object):
    MAX_ROWS = 4000000

    def __init__(self, maps_dir, raw_content=True, float_tol=1e-5, max_examples=3):
        self.maps_dir = maps_dir or default_maps_dir()
        self.raw_content = raw_content
        self.tol = float_tol
        self.max_examples = max_examples
        self.plans = compare_plans()
        self.intern = _Interner()
        self._ext = {}
        self._digests = {}
        self.shared = None
        self.R = None

    def ext(self, name):
        if name not in self._ext:
            self._ext[name] = ExtDB(os.path.join(self.maps_dir, name))
        return self._ext[name]

    # ---- raw content digests ------------------------------------------------------------------------------------
    def _digest(self, src, kind, ptr, sizef):
        mv = src.mv
        h = mv.h
        size = sizef & 0x7FFFFFFF if kind == 'sound' else sizef
        if size == 0 and (ptr == 0 or ptr == NULL):
            return 'empty'
        loc, off = (ptr >> 30) & 3, ptr & 0x3FFFFFFF
        if loc == 0 or SELF_LOC.get(h.type) == loc:
            buf, owner = mv.buf, mv.path
        else:
            db = self.ext(DB_FILES.get(loc, ''))
            if not db.exists:
                return 'unavailable:%s:%x:%x' % (DB_FILES.get(loc), off, size)
            buf, owner = db.buf, db.path
        key = (owner, kind, off, size, self.raw_content)
        d = self._digests.get(key)
        if d is not None:
            return d
        if off + size > len(buf) or size < 0:
            d = 'bad-range'
        elif not self.raw_content:
            d = 'size:%d' % size
        else:
            blob = bytes(buf[off:off + size])
            if kind == 'bitmap':
                try:
                    dec = zlib.decompressobj()
                    blob = dec.decompress(blob)
                    d = 'px:' + hashlib.sha1(blob).hexdigest()[:20] + ':%d' % len(blob)
                except zlib.error:
                    d = 'bad-zlib'
            else:
                d = hashlib.sha1(blob).hexdigest()[:20] + ':%d' % size
        self._digests[key] = d
        return d

    # ---- keys --------------------------------------------------------------------------------------------------------
    def _ref_keys(self, src, g, d, own, with_group):
        mv = src.mv
        key = np.zeros(len(d), dtype=np.int64)
        nn = (d != NULL) & (d != 0) if not with_group else (d != NULL)
        idx = (d & 0xFFFF).astype(np.int64)
        okr = nn & (idx < mv.ntags)
        safe = np.where(okr, idx, 0)
        ok = okr & (mv.inst_datum[safe] == d)
        key[ok] = src.tkey[safe[ok]]
        bad = nn & ~ok
        key[bad] = -(d[bad].astype(np.int64)) - 10
        key[nn & (d == own)] = -2
        return key

    def _sid_keys(self, src, v):
        i = (v & 0xFFFFFF).astype(np.int64)
        ok = (v != 0) & (i < len(src.skey))
        key = np.zeros(len(v), dtype=np.int64)
        key[ok] = src.skey[i[ok]]
        key[(v != 0) & ~ok] = -3
        return key

    # ---- the tag walk -------------------------------------------------------------------------------------------------
    def compare_tag(self, R, group, name, sa, ta, sb, tb):
        plan = self.plans.get(group)
        if plan is None:
            R.skipped['no_layout'] += 1
            return
        if ta.size < plan.es or tb.size < plan.es:
            R.skipped['too_small'] += 1
            return
        ra = sa.mv.buf[sa.mv.fo(ta.addr):sa.mv.fo(ta.addr) + plan.es].reshape(1, plan.es)
        rb = sb.mv.buf[sb.mv.fo(tb.addr):sb.mv.fo(tb.addr) + plan.es].reshape(1, plan.es)
        self.R = R
        self.tag = name
        self.own = (np.uint32(ta.datum), np.uint32(tb.datum))
        self.ndiff = 0
        stack = [(plan, ra, rb, np.zeros(1, dtype=np.int64), 0)]
        while stack:
            self._item(sa, sb, stack.pop(), stack)
        R.group_tags[group][0] += 1
        ident = self.ndiff == 0
        R.group_tags[group][1] += int(ident)
        R.tag_results[(group, name)] = (ident, self.ndiff)

    def _stat(self, store, group, path, key, cat):
        k = (group, path, key)
        s = store.get(k)
        if s is None:
            s = store[k] = FieldStat(cat)
        return s

    def _example(self, s, elem, a, b):
        if len(s.examples) < self.max_examples:
            s.examples.append({'tag': self.tag, 'elem': int(elem), 'out': a, 'ref': b})

    def _item(self, sa, sb, item, stack):
        plan, RA, RB, elem, depth = item
        R = self.R
        m = RA.shape[0]
        g, p = plan.group, plan.path
        # ---- leaf fields
        for c in plan.leaves:
            ca = np.ascontiguousarray(RA[:, c.off:c.off + c.size])
            cb = np.ascontiguousarray(RB[:, c.off:c.off + c.size])
            near = None
            if c.kind == 'raw':
                eq = (ca == cb).all(axis=1)
                if c.float and not eq.all() and c.size % 4 == 0:
                    fa, fb = ca.view('<f4'), cb.view('<f4')
                    with np.errstate(invalid='ignore'):
                        close = np.isclose(fa, fb, rtol=self.tol, atol=self.tol, equal_nan=True).all(axis=1)
                    near = close & ~eq
            elif c.kind == 'tagref':
                ua, ub = ca.view('<u4').reshape(m, 2), cb.view('<u4').reshape(m, 2)
                ka = self._ref_keys(sa, ua[:, 0], ua[:, 1], self.own[0], True)
                kb = self._ref_keys(sb, ub[:, 0], ub[:, 1], self.own[1], True)
                eq = (ka == kb) & ((ua[:, 0] == ub[:, 0]) | (ua[:, 1] == NULL))
            elif c.kind == 'tagdatum':
                ua, ub = ca.view('<u4').reshape(m), cb.view('<u4').reshape(m)
                eq = self._ref_keys(sa, None, ua, self.own[0], False) == self._ref_keys(sb, None, ub, self.own[1], False)
            else:
                ua, ub = ca.view('<u4').reshape(m), cb.view('<u4').reshape(m)
                eq = self._sid_keys(sa, ua) == self._sid_keys(sb, ub)
            s = self._stat(R.fields, g, p, c.key, c.cat)
            n_eq, n_near = int(eq.sum()), int(near.sum()) if near is not None else 0
            s.total += m
            s.equal += n_eq
            s.near += n_near
            nd = m - n_eq - n_near
            if nd:
                s.diff += nd
                self.ndiff += nd
                bad = np.nonzero(~eq & ~(near if near is not None else False))[0]
                for j in bad[:self.max_examples].tolist():
                    self._example(s, elem[j], self._fmt(c, sa, ca[j]), self._fmt(c, sb, cb[j]))
        # ---- raw-resource pointers compared by decoded content
        for (po, so, kind, name) in plan.raws:
            s = self._stat(R.fields, g, p, name, 'data')
            ua = np.ascontiguousarray(RA[:, po:po + 4]).view('<u4').ravel()
            ub = np.ascontiguousarray(RB[:, po:po + 4]).view('<u4').ravel()
            sza = np.ascontiguousarray(RA[:, so:so + 4]).view('<u4').ravel()
            szb = np.ascontiguousarray(RB[:, so:so + 4]).view('<u4').ravel()
            for j in range(m):
                da = self._digest(sa, kind, int(ua[j]), int(sza[j]))
                db_ = self._digest(sb, kind, int(ub[j]), int(szb[j]))
                s.total += 1
                if da == db_ or (da.startswith('unavailable') and db_.startswith('unavailable')):
                    s.equal += 1
                else:
                    s.diff += 1
                    self.ndiff += 1
                    self._example(s, elem[j], da, db_)
        # ---- datarefs
        for (off, key) in plan.datas:
            s = self._stat(R.fields, g, p, key, 'data')
            ua = np.ascontiguousarray(RA[:, off:off + 8]).view('<u4').reshape(m, 2)
            ub = np.ascontiguousarray(RB[:, off:off + 8]).view('<u4').reshape(m, 2)
            for j in np.nonzero((ua[:, 0] != 0) | (ub[:, 0] != 0))[0].tolist():
                s.total += 1
                za, zb = int(ua[j, 0]), int(ub[j, 0])
                same = za == zb
                if same and za > 0:
                    oa, ob = sa.mv.fo(int(ua[j, 1])), sb.mv.fo(int(ub[j, 1]))
                    if 0 <= oa and oa + za <= len(sa.mv.buf) and 0 <= ob and ob + zb <= len(sb.mv.buf):
                        same = np.array_equal(sa.mv.buf[oa:oa + za], sb.mv.buf[ob:ob + zb])
                    else:
                        same = False
                if same:
                    s.equal += 1
                else:
                    s.diff += 1
                    self.ndiff += 1
                    self._example(s, elem[j], '%d bytes' % za, '%d bytes' % zb)
            # rows where both are empty count as equal
            both0 = int(((ua[:, 0] == 0) & (ub[:, 0] == 0)).sum())
            s.total += both0
            s.equal += both0
        # ---- child blocks
        if depth > 14:
            return
        for (off, key, child, ces) in plan.blocks:
            cA = np.ascontiguousarray(RA[:, off:off + 8]).view('<u4').reshape(m, 2)
            cB = np.ascontiguousarray(RB[:, off:off + 8]).view('<u4').reshape(m, 2)
            na, nb = cA[:, 0].view('<i4').astype(np.int64), cB[:, 0].view('<i4').astype(np.int64)
            na, nb = np.where(na > 0, na, 0), np.where(nb > 0, nb, 0)
            act = (na > 0) | (nb > 0)
            if not act.any():
                continue
            cs = self._stat(R.counts, g, p, key, 'data')
            ne = int((act & (na == nb)).sum())
            cs.total += int(act.sum())
            cs.equal += ne
            cs.diff += int(act.sum()) - ne
            self.ndiff += int(act.sum()) - ne
            for j in np.nonzero(act & (na != nb))[0][:self.max_examples].tolist():
                if len(cs.examples) < self.max_examples:
                    cs.examples.append({'tag': self.tag, 'elem': int(elem[j]), 'out': int(na[j]), 'ref': int(nb[j])})
            if ces == 0 or not (child.leaves or child.blocks or child.datas or child.raws):
                continue
            k = np.minimum(na, nb)
            sel = np.nonzero(k > 0)[0]
            if not len(sel):
                continue
            aa, ab = cA[sel, 1].astype(np.int64), cB[sel, 1].astype(np.int64)
            kk = k[sel]
            if int(kk.sum()) > self.MAX_ROWS:
                R.skipped['too_many_rows'] += 1
                continue
            lo_a, hi_a = sa.mv.region_lo, sa.mv.region_hi
            lo_b, hi_b = sb.mv.region_lo, sb.mv.region_hi
            good = (aa >= lo_a) & (aa + kk * ces <= hi_a) & (ab >= lo_b) & (ab + kk * ces <= hi_b)
            if not good.all():
                R.skipped['bad_block_pointer'] += int((~good).sum())
                sel, aa, ab, kk = sel[good], aa[good], ab[good], kk[good]
                if not len(sel):
                    continue
            rows_a = gather_rows(sa.mv.buf, sa.mv.toff + aa - sa.mv.mask, kk, ces)
            rows_b = gather_rows(sb.mv.buf, sb.mv.toff + ab - sb.mv.mask, kk, ces)
            seg = np.cumsum(kk) - kk
            total = int(kk.sum())
            celem = np.arange(total, dtype=np.int64) - np.repeat(seg, kk)
            stack.append((child, rows_a, rows_b, celem, depth + 1))

    def _fmt(self, c, src, raw):
        b = bytes(raw)
        if c.kind == 'raw':
            if c.float and c.size % 4 == 0:
                return ' '.join('%.6g' % x for x in struct.unpack('<%df' % (c.size // 4), b))
            if c.size <= 8:
                return '0x' + b[::-1].hex()
            return b[:16].hex() + ('...' if c.size > 16 else '')
        mv = src.mv
        if c.kind == 'sid':
            v = struct.unpack('<I', b)[0]
            i = v & 0xFFFFFF
            return repr(mv.c.string_by_index(i)) if v and i < mv.str_count else ('null' if not v else 'bad-sid %#x' % v)
        d = struct.unpack('<I', b[4:8])[0] if c.kind == 'tagref' else struct.unpack('<I', b)[0]
        if d in (NULL, 0 if c.kind == 'tagdatum' else NULL):
            return 'null'
        i = d & 0xFFFF
        if i < mv.ntags and mv.inst_datum[i] == d:
            nm = mv.c._names[i] if i < len(mv.c._names) else None
            return '%s:%s' % (fourcc_str(struct.pack('<I', int(mv.inst_group[i]))), nm)
        return 'dangling %#x' % d


def _chunk_bytes(cmp_, src, ptr, sf):
    mv = src.mv
    loc, off, size = (ptr >> 30) & 3, ptr & 0x3FFFFFFF, sf & 0x7FFFFFFF
    if loc == 0 or SELF_LOC.get(mv.h.type) == loc:
        buf = mv.buf
    else:
        db = cmp_.ext(DB_FILES.get(loc, ''))
        if not db.exists:
            return None
        buf = db.buf
    if off + size > len(buf):
        return None
    return buf[off:off + size]


def _sound_perms(cmp_, src, body):
    """[(language_neutral_time, sample_size, chunk_count, sha1 of the concatenated chunk payload)] for the permutations
    a snd! uses (pitch range index/count at snd!+8/+0xA, resolved through the map's local ugh!)."""
    A = src._ugh
    pri, prc = struct.unpack_from('<h', body, 8)[0], body[0xA]
    if A is None or prc == 0 or pri < 0 or pri + prc > len(A['pr']):
        return None
    out = []
    for p_ in range(pri, pri + prc):
        first, cnt = int(A['pr'][p_, 4]), int(A['pr'][p_, 5])
        if first < 0 or first + cnt > len(A['pv']):
            return None
        for q in range(first, first + cnt):
            lnt = int(A['pv'][q, 3]) & 0xFFFF
            ssize = int(A['pu'][q, 2])
            c0, cn = int(A['pv'][q, 6]), int(A['pv'][q, 7])
            if c0 < 0 or cn < 0 or c0 + cn > len(A['cu']):
                return None
            h = hashlib.sha1()
            bad = False
            for c in range(c0, c0 + cn):
                b = _chunk_bytes(cmp_, src, int(A['cu'][c, 0]), int(A['cu'][c, 1]))
                if b is None:
                    bad = True
                    break
                h.update(b)
            out.append((lnt, ssize, cn, 'unreadable' if bad else h.hexdigest()[:16]))
    return out


def _compare_sounds(cmp_, R, A, B, ia, ib):
    def prep(src):
        mv = src.mv
        ughs = [t for t in mv.tag_by_index if t.group == 'ugh!' and t.datum != NULL and t.addr and t.size >= 0x58 and t.index < FIRST_SHARED_TAG_INSTANCE_INDEX]
        src._ugh = None
        if len(ughs) == 1:
            a = _SoundMixin._ugh_arrays(None, None, mv, ughs[0])
            if a is not None and all(v is not None for v in a.values()):
                def vw(name, dt, cols):
                    return np.ascontiguousarray(a[name]).view(dt).reshape(-1, cols) if len(a[name]) else np.zeros((0, cols), dt)
                src._ugh = {'pr': vw('pitch_ranges', '<i2', 6), 'pv': vw('permutations', '<i2', 8),
                            'pu': vw('permutations', '<u4', 4), 'cu': vw('chunks', '<u4', 3)}
    prep(A)
    if B is not A:
        prep(B)
    if A._ugh is None or B._ugh is None:
        return None
    st = {'sounds': 0, 'sounds_identical': 0, 'perms': 0, 'perms_identical': 0, 'perms_same_size': 0, 'perms_other': 0,
          'perm_count_mismatch': 0, 'examples': []}
    for (g, name), ta in ia.items():
        if g != 'snd!' or not (ta.addr and ta.size >= 0x14):
            continue
        tb = ib.get((g, name))
        if tb is None or not (tb.addr and tb.size >= 0x14):
            continue
        ba = bytes(A.mv.buf[A.mv.fo(ta.addr):A.mv.fo(ta.addr) + 0x14])
        bb = bytes(B.mv.buf[B.mv.fo(tb.addr):B.mv.fo(tb.addr) + 0x14])
        pa, pb = _sound_perms(cmp_, A, ba), _sound_perms(cmp_, B, bb)
        if pa is None or pb is None:
            continue
        st['sounds'] += 1
        if len(pa) != len(pb):
            st['perm_count_mismatch'] += 1
            if len(st['examples']) < 3:
                st['examples'].append({'tag': name, 'out': '%d permutations' % len(pa), 'ref': '%d permutations' % len(pb)})
            continue
        allsame = True
        for x, y in zip(pa, pb):
            st['perms'] += 1
            if x[3] == y[3] and x[3] != 'unreadable':
                st['perms_identical'] += 1                      # payload identical (chunking / metadata may differ)
            elif x[1] == y[1]:
                st['perms_same_size'] += 1
                allsame = False
            else:
                st['perms_other'] += 1
                allsame = False
                if len(st['examples']) < 3:
                    st['examples'].append({'tag': name, 'out': 'sample_size %d chunks %d' % (x[1], x[2]), 'ref': 'sample_size %d chunks %d' % (y[1], y[2])})
        st['sounds_identical'] += int(allsame)
    return st


def compare_maps(out_path, ref_path, maps_dir=None, groups=None, name_filter=None, raw_content=True, float_tol=1e-5,
                 follow_shared=True, max_tags=None, max_examples=3):
    """Field-level comparison of two Vista maps.  Tags are matched by (group, name); tagrefs are compared by the
    target's (group, name), string ids by text, bitmap/geometry/sound pointers by the decoded CONTENT of what they
    point at (inflated pixels / block bytes / chunk bytes).  Shared placeholders (index >= 10000) are resolved through
    the maps folder's shared.map when `follow_shared` (a placeholder in both maps with the same datum counts as
    'linked_equal').  `groups` limits the compared groups, `name_filter` is a regex on the tag name.

    Returns a CompareReport: .fields {(group, block path, field): FieldStat(total, equal, near, diff, examples)},
    .counts (block element counts), .group_summary(), .top_mismatches(), .only_out / .only_ref, .format_text(), .to_dict()."""
    t0 = time.time()
    R = CompareReport(out_path, ref_path)
    cmp_ = _Comparer(maps_dir, raw_content, float_tol, max_examples)
    R.raw_mode = raw_content
    A = _Src(out_path, cmp_.intern)
    B = _Src(ref_path, cmp_.intern) if os.path.abspath(ref_path) != os.path.abspath(out_path) else A
    shared = [None]

    def shared_src():
        if shared[0] is None:
            p = os.path.join(cmp_.maps_dir, 'shared.map')
            shared[0] = _Src(p, cmp_.intern) if os.path.isfile(p) else False
        return shared[0] or None

    def resolve(src, tag):
        """-> (Src, VistaTag) holding the tag's data, or None."""
        if tag.addr and tag.size:
            return src, tag
        if tag.index >= FIRST_SHARED_TAG_INSTANCE_INDEX and follow_shared:
            sh = shared_src()
            if sh is not None and tag.index < sh.mv.ntags:
                st = sh.mv.tag_by_index[tag.index]
                if st.datum == tag.datum and st.addr and st.size:
                    return sh, st
        return None

    def index(src):
        d = {}
        for t in src.mv.tag_by_index:
            if t.datum != NULL and t.name:
                d[(t.group, t.name)] = t
        return d

    ia, ib = index(A), index(B)
    keys = sorted(set(ia) | set(ib))
    if groups:
        keys = [k for k in keys if k[0] in groups]
    if name_filter:
        rx = _re.compile(name_filter)
        keys = [k for k in keys if rx.search(k[1])]
    n = 0
    for (g, name) in keys:
        ta, tb = ia.get((g, name)), ib.get((g, name))
        if ta is None:
            R.only_ref.append((g, name))
            continue
        if tb is None:
            R.only_out.append((g, name))
            continue
        if not (ta.addr and ta.size) and not (tb.addr and tb.size):
            if ta.datum == tb.datum and ta.index == tb.index:
                R.linked_equal += 1
            elif ta.index >= FIRST_SHARED_TAG_INSTANCE_INDEX and tb.index >= FIRST_SHARED_TAG_INSTANCE_INDEX:
                R.linked_diff.append((g, name, '%#x' % ta.datum, '%#x' % tb.datum))
            else:
                R.skipped['empty_tags'] += 1
            continue
        ra, rb = resolve(A, ta), resolve(B, tb)
        if ra is None or rb is None:
            R.unresolved += 1
            continue
        if max_tags is not None and n >= max_tags:
            R.skipped['max_tags'] += 1
            continue
        n += 1
        try:
            cmp_.compare_tag(R, g, name, ra[0], ra[1], rb[0], rb[1])
        except Exception as e:
            import traceback
            R.skipped['compare_error'] += 1
            R.notes.append('compare error in %s %s: %s: %s' % (g, name, type(e).__name__, e))
            if len(R.notes) < 4:
                R.notes.append(traceback.format_exc(limit=4))
    if not groups or 'snd!' in groups:
        try:
            R.sounds = _compare_sounds(cmp_, R, A, B, ia, ib)
        except Exception as e:
            R.notes.append('sound comparison failed: %s: %s' % (type(e).__name__, e))
    if shared[0]:
        shared[0].close()
    if B is not A:
        B.close()
    A.close()
    for e in cmp_._ext.values():
        e.close()
    R.seconds = time.time() - t0
    return R



def learn_enums(maps_dir=None, out_path=ENUM_SEEN_PATH, verbose=True):
    """Scan every stock map and store the enum values / flag bits that occur in genuine Vista data but are not named by
    the Assembly plugin (the plugins are incomplete: e.g. snd! flags 12/13, phmo flags).  The enum/flags range check
    accepts plugin options + these."""
    global _PLANS, _ENUM_SEEN
    _PLANS, _ENUM_SEEN = None, {}
    v = Validator(maps_dir, raw_decode=False, floats=False, enums=False)
    v._learn = {}
    v.plans = vista_plans()
    for path in stock_maps(maps_dir):
        v.validate(path)
        if verbose:
            print('learned from', os.path.basename(path), flush=True)
    v.close()
    out = {}
    for k, d in v._learn.items():
        out[k] = {'m': d['m']} if 'm' in d else {'v': sorted(d['v'])}
    json.dump(out, open(out_path, 'w', encoding='utf-8'), indent=0, sort_keys=True)
    _PLANS, _ENUM_SEEN = None, None
    return out


def stock_maps(maps_dir=None):
    d = maps_dir or default_maps_dir()
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith('.map'))


_WORKER = {}


def _worker(args):
    """Pool worker: one Validator per process (layouts, plans and the shared.map dependency closure are built once)."""
    path, maps_dir, raw_decode, max_examples, floats, enums, sound_data = args
    key = (maps_dir, raw_decode, max_examples, floats, enums, sound_data)
    v = _WORKER.get(key)
    if v is None:
        v = _WORKER[key] = Validator(maps_dir, raw_decode, max_examples, floats=floats, enums=enums, sound_data=sound_data)
    return v.validate(path).to_dict()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='python -m h2conv.validate', description='Deep validator / comparer for Halo 2 Vista (v8) maps')
    ap.add_argument('maps', nargs='*', help='Vista .map files to validate')
    ap.add_argument('--all', action='store_true', help='validate every .map in --maps-dir')
    ap.add_argument('--ref', help='reference Vista map: compare the (single) map against it instead of only validating')
    ap.add_argument('--maps-dir', default=None, help='folder with mainmenu/shared/single_player_shared.map (default: H2CONV_VISTA_MAPS, the settings file, then a search of your drives)')
    _paths.add_common_options(ap)
    ap.add_argument('--json', help='write the full report as JSON')
    ap.add_argument('--no-raw-decode', action='store_true', help='do not inflate bitmap blobs (header + bounds only)')
    ap.add_argument('--no-floats', action='store_true', help='skip the NaN/Inf scan of float fields (faster)')
    ap.add_argument('--no-enums', action='store_true', help='skip the enum/flags range check')
    ap.add_argument('--no-sound-data', action='store_true', help='do not read the ADPCM/WMA chunk bytes (headers of every ADPCM block are checked by default)')
    ap.add_argument('--learn-enums', action='store_true', help='rebuild validate_enum_seen.json (in the work folder) from the stock maps in --maps-dir, then exit')
    ap.add_argument('--examples', type=int, default=3, help='examples printed per issue code (default 3)')
    ap.add_argument('--jobs', type=int, default=1, help='worker processes for several maps (max 4)')
    ap.add_argument('--quiet', action='store_true', help='one line per map')
    ap.add_argument('--top', type=int, default=25, help='compare: rows in the top-mismatch tables')
    ap.add_argument('--fail-on-warning', action='store_true')
    a = ap.parse_args(argv)
    _paths.configure(assembly=a.assembly_dir, vista_maps=a.maps_dir)
    try:
        sys.stdout.reconfigure(errors='replace')
    except Exception:
        pass
    if a.learn_enums:
        r = learn_enums(a.maps_dir)
        print('wrote %s: %d fields with values/bits the plugins do not name' % (ENUM_SEEN_PATH, len(r)))
        return 0
    paths = list(a.maps)
    if a.all:
        paths += [p for p in stock_maps(a.maps_dir) if p not in paths]
    if not paths:
        ap.error('no map given')
    if a.ref:
        try:
            rep = compare_maps(paths[0], a.ref, maps_dir=a.maps_dir)
        except Exception as e:
            print('cannot compare %s with %s: %s: %s' % (paths[0], a.ref, type(e).__name__, e))
            return 2
        print(rep.format_text(top=a.top))
        if a.json:
            json.dump(rep.to_dict(top=200, full=True), open(a.json, 'w', encoding='utf-8'), indent=1)
        return 0
    out = []
    t0 = time.time()
    jobs = max(1, min(a.jobs, 4, len(paths)))
    if jobs > 1:
        import multiprocessing as mp
        with mp.get_context('spawn').Pool(jobs) as pool:
            for d in pool.imap(_worker, [(p, a.maps_dir, not a.no_raw_decode, 25, not a.no_floats, not a.no_enums, not a.no_sound_data) for p in paths]):
                out.append(d)
                _print_dict(d, a)
    else:
        v = Validator(a.maps_dir, not a.no_raw_decode, floats=not a.no_floats, enums=not a.no_enums, sound_data=not a.no_sound_data)
        try:
            for p in paths:
                r = v.validate(p)
                d = r.to_dict()
                out.append(d)
                if a.quiet:
                    print('%-28s %-4s errors %-5d warnings %-5d notes %-5d  tags %-5s %.1fs' % (
                        os.path.basename(p), 'OK' if r.ok else 'FAIL', r.errors, r.warnings, r.notes, r.stats.get('tags_walked', 0), r.seconds), flush=True)
                else:
                    print(r.format_text(a.examples), flush=True)
        finally:
            v.close()
    tot_e = sum(d['errors'] for d in out)
    tot_w = sum(d['warnings'] for d in out)
    print('== %d map(s): %d error(s), %d warning(s), %.1fs' % (len(out), tot_e, tot_w, time.time() - t0))
    if a.json:
        json.dump({'maps': out}, open(a.json, 'w', encoding='utf-8'), indent=1)
    return 1 if tot_e or (a.fail_on_warning and tot_w) else 0


def _print_dict(d, a):
    if a.quiet:
        print('%-28s %-4s errors %-5d warnings %-5d notes %-5d %.1fs' % (os.path.basename(d['map']), 'OK' if d['ok'] else 'FAIL', d['errors'], d['warnings'], d['notes'], d['seconds']), flush=True)
    else:
        print('%s  [%s]  %d error(s), %d warning(s), %d note(s) (%.1fs)' % (os.path.basename(d['map']), 'OK' if d['ok'] else 'FAIL', d['errors'], d['warnings'], d['notes'], d['seconds']))
        for code, e in sorted(d['codes'].items()):
            print('  %-7s %-34s x%d' % (e['level'].upper(), code, e['count']))
            for ex in e['examples'][:a.examples]:
                print('      - %s' % ex['msg'])
        sys.stdout.flush()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except _paths.PathsError as _ex:
        print('ERROR: %s' % _ex)
        sys.exit(2)



