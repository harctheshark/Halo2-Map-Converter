# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Read-only view of a Halo 2 Xbox (cache v8, build 02.09.27.09809) .map file.

Format facts (probes of bulldog.map of the Phantom v1 pack, 2026-10-21):
* Header (0x800, 'head' .. 'foot'): the Vista header WITHOUT the 12 bytes Vista inserts at 0x20 (tag_offset_mask, shared dependency
  offset/count).  Every Vista field after 0x20 sits at (Vista offset - 12): build string 0x120, scenario type 0x140, name 0x198,
  scenario path 0x1BC, string table 0x164/0x168/0x16C/0x170, tag names 0x2C0 (count) / 0x2C4 (buffer) / 0x2C8 (size) / 0x2CC (index).
* Tag region at header 0x10, its index part is header 0x14 bytes long.  TWO address bases:
      index magic = group_table_ptr - (tag_offset + 0x20)          (group table / instance table pointers)
      data magic  = tag0.addr - (tag_offset + header[0x14])         (every tag data pointer)
* All tags are local (matg, ugh!, shaders ...).  sbsp / ltmp have address 0 in the index: they live in the BSP section of
  scnr Structure BSPs (element 0x44: +0 file offset, +4 size, +8 load address); the section starts with
  {u32 size, u32 sbsp addr, u32 ltmp addr, 'sbsp'} and is addressed by (load address - file offset).
* Raw pointers (bitmap LODs, geometry blocks, sound chunks, jmad resource groups) carry location bits like Vista:
  0 this map, 1 mainmenu.map, 2 shared.map (the PACK's Xbox shared files, not Vista's), 3 single_player_shared.map.
* Datums (2026-10-26, audit of the packs): tag_by_datum needs the exact datum; when several entries share it, the one at index
  datum & 0xFFFF wins (the engine's lookup).  The tags-header scenario / globals datum alone falls back to its index when the salt is
  stale (.repairs says so).  No general stale-salt fallback: Vista's tag_get checks the full datum and the group.
"""
import mmap
import os
import struct

HEADER_SIZE = 0x800
XBOX_VERSION = 8
XBOX_BUILD = '02.09.27.09809'
LOC_FILES = {1: 'mainmenu.map', 2: 'shared.map', 3: 'single_player_shared.map'}
NULL = 0xFFFFFFFF


class XboxFormatError(Exception):
    pass


class NotHalo2MapError(XboxFormatError):
    """The file is not an original Xbox Halo 2 map (a Halo 3 / Reach / Xbox 360 file, a Vista map, something else).  Nothing to convert."""


class TruncatedMapError(XboxFormatError):
    """The file is shorter than its own header says: an incomplete download."""


# a download cut by less than this is still read: narbarbe (512 bytes) and weapwar (2,560 bytes) of Phantom v2 are that short, miss only
# the unused end of their last tag and convert correctly; Dead_in_the_Water (64 KB short) loses real data
TRUNCATION_TOLERANCE = 4096


def _cstr(buf, off, n):
    return bytes(buf[off:off + n]).split(b'\0')[0].decode('latin-1')


def short_bytes(header, real_size):
    """Bytes the file lacks against its own header's file size (0 when it is complete or longer).  `header`: the first 0x10 bytes or more."""
    if len(header) < 0x10 or bytes(header[:4]) != b'daeh':
        return 0
    return max(0, struct.unpack_from('<I', header, 8)[0] - real_size)


def not_halo2(path):
    """None for a Halo 2 cache map (header 'daeh', version 8, little-endian), else why not.  The stock folder also holds Xbox 360 files
    (big-endian 'head'): construct.map / shrine.map = Halo 3 (version 11, build 11855.07.08.20.2317.halo3_ship), campaign.map = Reach
    (version 12, 11860.10.07.24.0147.omaha_release) - reported as skipped, not as conversion failures."""
    with open(path, 'rb') as fh:
        h = fh.read(0x140)
    if len(h) < 0x140:
        return 'not a Halo 2 map (%d-byte file)' % len(h)
    if h[:4] == b'daeh' and struct.unpack_from('<i', h, 4)[0] == XBOX_VERSION:
        return None
    be = h[:4] == b'head'
    ver = struct.unpack_from('>i' if be else '<i', h, 4)[0]
    build = (h[0x11C:0x13C] if be else h[0x120:0x140]).split(b'\0')[0].decode('latin-1')
    return 'not a Halo 2 map (version %d, build %r)' % (ver, build)


def classify_header(header, real_size):
    """None for a Halo 2 Xbox map (cache v8, build 02.09.27.09809, complete); else (code, message):
        'not_halo2'  Halo 3 / Reach / other Xbox 360 files (big-endian 'head'), unknown files and files too small to be a map
        'not_xbox'   a Halo 2 cache v8 map of another build (Vista / beta): not an original Xbox map
        'truncated'  an Xbox map whose file is shorter than its header says (an incomplete download)
    `header` = the first 0x140 bytes (or more) of the file, `real_size` = the file's size."""
    h = bytes(header)
    if len(h) < 0x140:
        return 'not_halo2', 'not a Halo 2 map (a %d-byte file)' % real_size
    if h[:4] == b'daeh' and struct.unpack_from('<i', h, 4)[0] == XBOX_VERSION:
        build = _cstr(h, 0x120, 32)
        if build != XBOX_BUILD:
            return 'not_xbox', ('not an original Xbox Halo 2 map (cache version 8, build %r; the Xbox build is %s). This looks like a '
                                'Halo 2 Vista or beta map' % (build, XBOX_BUILD))
        miss = short_bytes(h, real_size)
        if miss >= TRUNCATION_TOLERANCE:
            return 'truncated', ('the file is shorter than its header says (truncated download?): the header says %d bytes, the file has %d '
                                 '(%d bytes missing). Download it again' % (struct.unpack_from('<I', h, 8)[0], real_size, miss))
        return None
    if h[:4] not in (b'daeh', b'head'):
        return 'not_halo2', 'not a Halo 2 map (the file has no map header)'
    be = h[:4] == b'head'
    ver = struct.unpack_from('>i' if be else '<i', h, 4)[0]
    build = (h[0x11C:0x13C] if be else h[0x120:0x140]).split(b'\0')[0].decode('latin-1')
    return 'not_halo2', ('not a Halo 2 map (version %d, build %r). Halo 3, Halo 3: ODST, Reach and other Xbox 360 maps cannot be converted'
                         % (ver, build))


# scenario type codes (header 0x140, and the scenario tag's own Type) -> the kind of map
XBOX_KINDS = {0: 'solo', 1: 'multiplayer', 2: 'mainmenu', 3: 'shared', 4: 'single_player_shared'}


def xbox_kind(header_type, scenario_type=None):
    """The kind of an Xbox map: 'multiplayer' | 'solo' (campaign) | 'mainmenu' | 'shared' | 'single_player_shared' | 'unknown'.
    A map is multiplayer when its header says 1, or - 24 Phantom maps whose header was zeroed by a mod tool - when the scenario tag's own Type
    says 1 (`scenario_type`, see scenario_tag_type); every other kind is read from the scenario Type, else from the header.
    The converter (convert_xbox) and h2conv.mapinfo both decide through this one function."""
    if header_type == 1 or scenario_type == 1:
        return 'multiplayer'
    return XBOX_KINDS.get(scenario_type if scenario_type in (0, 2, 3, 4) else header_type, 'unknown')


def scenario_tag_type(xc):
    """The scenario tag's own Type (0 campaign, 1 multiplayer, 2 main menu ...) of an open XboxCache; None when the scenario tag has no data."""
    from . import layouts as XL
    scn = xc.scenario
    if scn is None or not scn.addr:
        return None
    ft = next(f for f in XL.xbox_layouts()['scnr'].fields if f.name == 'Type')
    return struct.unpack_from('<h', xc.read(scn.addr + ft.offset, 2))[0]


def file_kind(path, header_type):
    """-> (kind, scenario_type) of the Xbox map at `path` whose header says `header_type`.  Opens the map only when the header alone does not
    say multiplayer (campaign maps, and the Phantom maps with a zeroed header); a map that cannot be opened is judged by its header."""
    if header_type == 1:
        return 'multiplayer', None
    stype = None
    try:
        with XboxCache(path) as xc:
            stype = scenario_tag_type(xc)
    except Exception:                                                    # noqa: BLE001 - judged by the header; the converter reports the real problem
        stype = None
    return xbox_kind(header_type, stype), stype


class XboxTag(object):
    __slots__ = ('index', 'group', 'datum', 'addr', 'size', 'name', 'bsp')

    def __init__(self, index, group, datum, addr, size, name):
        self.index = index
        self.group = group
        self.datum = datum
        self.addr = addr
        self.size = size
        self.name = name
        self.bsp = None              # (file offset, size, load address) for sbsp / ltmp

    @property
    def is_empty(self):
        return self.datum == NULL

    def __repr__(self):
        return '<XboxTag %d %s %s addr=0x%X size=%d>' % (self.index, self.group, self.name, self.addr, self.size)


class XboxCache(object):
    """Read-only Xbox v8 map.  `shared_dir` = folder of the PACK's mainmenu.map / shared.map (default: the map's own folder)."""

    def __init__(self, path, shared_dir=None, fallback_dirs=()):
        self.path = os.path.abspath(path)
        self.shared_dir = shared_dir or os.path.dirname(self.path)
        # where a location file the pack does not ship is looked for next (the stock Xbox maps): Killtrocity's Outflod keeps PRTM raw
        # in single_player_shared.map, which no pack includes - every stock Xbox copy of that file is the same
        self.fallback_dirs = tuple(d for d in fallback_dirs if d)
        self._f = open(self.path, 'rb')
        try:
            head = self._f.read(0x140)
            bad = classify_header(head, os.path.getsize(self.path))
            if bad:
                raise (NotHalo2MapError if bad[0] != 'truncated' else TruncatedMapError)('%s: %s' % (os.path.basename(self.path), bad[1]))
            self.m = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        except BaseException:
            self._f.close()
            raise
        m = self.m
        if bytes(m[0:4]) != b'daeh' or struct.unpack_from('<i', m, 4)[0] != XBOX_VERSION:
            raise XboxFormatError('%s is not a cache v8 map' % path)
        self.build = _cstr(m, 0x120, 32)
        if self.build != XBOX_BUILD:
            raise XboxFormatError('%s: build %r is not the Xbox retail build %s' % (path, self.build, XBOX_BUILD))
        u = lambda o: struct.unpack_from('<I', m, o)[0]                    # noqa: E731
        self.header = {
            'file_size': u(0x8), 'tag_offset': u(0x10), 'tag_index_size': u(0x14), 'tag_data_size': u(0x18), 'tag_size': u(0x1C),
            'scenario_type': struct.unpack_from('<h', m, 0x140)[0], 'name': _cstr(m, 0x198, 32), 'scenario_path': _cstr(m, 0x1BC, 256),
            'string_count': u(0x164), 'string_table_size': u(0x168), 'string_index_offset': u(0x16C), 'string_table_offset': u(0x170),
            'tag_name_count': u(0x2C0), 'tag_name_buffer': u(0x2C4), 'tag_name_buffer_size': u(0x2C8), 'tag_name_index': u(0x2CC),
        }
        to = self.header['tag_offset']
        (gt, self.group_count, it, self.scenario_datum, self.globals_datum, _f14, tc, sig) = struct.unpack_from('<IIIIIIII', m, to)
        if struct.pack('<I', sig) != b'sgat':
            raise XboxFormatError('tags header signature mismatch')
        self.index_magic = gt - (to + 0x20)
        io = it - self.index_magic
        hn = self.header
        names_idx = struct.unpack_from('<%dI' % hn['tag_name_count'], m, hn['tag_name_index'])
        nb = hn['tag_name_buffer']
        self.tags = []
        for i in range(tc):
            g, datum, addr, sz = struct.unpack_from('<4sIII', m, io + 16 * i)
            name = None
            if i < len(names_idx) and names_idx[i] != NULL:
                s = nb + names_idx[i]
                name = bytes(m[s:m.find(b'\0', s)]).decode('latin-1')
            grp = '' if g == b'\xff\xff\xff\xff' else g[::-1].decode('latin-1')
            self.tags.append(XboxTag(i, grp, datum, addr, sz, name))
        first = next(t for t in self.tags if t.addr)
        self.magic = first.addr - (to + hn['tag_index_size'])
        self._by_datum = {t.datum: t for t in self.tags if not t.is_empty}
        # one datum on several entries (a mod tool cloned an index entry WITH its datum): the engine reads entry datum & 0xFFFF and
        # checks the full datum there (Vista tag_get 0x4316C3), so that entry wins, not the last one in the table.  Danzshot_day:
        # entry 4084 weap mortar_turret_mp carries F0E40F5E = entry 3934 cannon_turret_mp, and the Scorpion's cannon_mp Weapons
        # became the Wraith mortar (2026-10-26).  Stale salts (no entry with that exact datum) still resolve to nothing
        for t in self.tags:
            if not t.is_empty and (t.datum & 0xFFFF) == t.index:
                self._by_datum[t.datum] = t
        # tags-header scenario / globals datum with a stale salt: 8 Phantom maps (desertedmetro, gridiron, monsoon ...) say E1780003
        # while entry 3 is scnr E1770003 - a tool renumbered the salts to E174 + index and left the header.  The engine loads both
        # through tag_get_fast (index only; Cartographer cache_files.cpp:114 / 351-352), so those maps load: the entry at datum & 0xFFFF,
        # and only when it IS a scnr / matg (a junk datum must not pick an arbitrary tag)
        self.repairs = []
        for attr, grp in (('scenario_datum', 'scnr'), ('globals_datum', 'matg')):
            d = getattr(self, attr)
            i = d & 0xFFFF
            if d not in self._by_datum and i < len(self.tags) and not self.tags[i].is_empty and self.tags[i].group == grp:
                setattr(self, attr, self.tags[i].datum)
                self.repairs.append('tags header %s 0x%08X has no entry: entry %d (%s 0x%08X) by index, as the engine loads it'
                                    % (attr, d, i, grp, self.tags[i].datum))
        self._strings = None
        self._loc = {}
        self._bsp_ranges = []
        self._locate_bsps()

    # ---- lifecycle --------------------------------------------------------------------------------------------
    def close(self):
        try:
            self.m.close()
        finally:
            self._f.close()
        for mm, fh in self._loc.values():
            mm.close()
            fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---- tags -------------------------------------------------------------------------------------------------
    def tag_by_datum(self, datum):
        return self._by_datum.get(datum)

    def find(self, group=None, name=None):
        return [t for t in self.tags if not t.is_empty and (group is None or t.group == group) and (name is None or t.name == name)]

    @property
    def scenario(self):
        return self.tag_by_datum(self.scenario_datum)

    @property
    def globals_tag(self):
        return self.tag_by_datum(self.globals_datum)

    # ---- addresses --------------------------------------------------------------------------------------------
    def read(self, addr, size):
        """Bytes at a tag-data address (ordinary tags) or a BSP-section address (sbsp / ltmp)."""
        for (lo, hi, base) in self._bsp_ranges:
            if lo <= addr and addr + size <= hi:
                o = base + (addr - lo)
                return bytes(self.m[o:o + size])
        o = addr - self.magic
        if o < 0 or o + size > len(self.m):
            raise XboxFormatError('address 0x%X (+%d) is outside the file' % (addr, size))
        return bytes(self.m[o:o + size])

    def tag_addr(self, t):
        """Data address of a tag (the BSP-section address for sbsp / ltmp)."""
        return t.addr

    def _locate_bsps(self):
        from . import layouts as XL
        from .. import tagtree as T
        scn = self.scenario
        if scn is None or not scn.addr:
            return
        lay = XL.xbox_layouts()['scnr']
        f = next(x for x in lay.fields if x.name == 'Structure BSPs')
        cnt, ptr = struct.unpack_from('<iI', self.read(scn.addr + f.offset, 8))
        for k in range(max(cnt, 0)):
            off, size, load = struct.unpack_from('<III', self.read(ptr + k * f.element_size, 12))
            if not size:
                continue
            self._bsp_ranges.append((load, load + size, off))
            _sz, sbsp_addr, ltmp_addr, _sig = struct.unpack_from('<III4s', self.m, off)
            sd, ld = struct.unpack_from('<II', self.read(ptr + k * f.element_size + 0x14, 4) + self.read(ptr + k * f.element_size + 0x1C, 4))
            for d, a in ((sd, sbsp_addr), (ld, ltmp_addr)):
                t = self.tag_by_datum(d)
                if t is not None and a:
                    t.addr = a
                    t.bsp = (off, size, load)
        del T

    # ---- raw data ---------------------------------------------------------------------------------------------
    def loc_path(self, loc):
        """The file a raw pointer of location `loc` reads (this map for 0; the pack's location file, else a fallback folder's)."""
        if loc == 0:
            return self.path
        memo = self.__dict__.setdefault('_loc_paths', {})
        if loc not in memo:
            p = os.path.join(self.shared_dir, LOC_FILES[loc])
            if not os.path.isfile(p):
                p = next((os.path.join(d, LOC_FILES[loc]) for d in self.fallback_dirs if os.path.isfile(os.path.join(d, LOC_FILES[loc]))), p)
            memo[loc] = os.path.abspath(p)
        return memo[loc]

    def _loc_map(self, loc):
        if loc == 0:
            return self.m
        r = self._loc.get(loc)
        if r is None:
            p = self.loc_path(loc)
            if not os.path.isfile(p):
                raise XboxFormatError('raw data is in %s, which is not next to the map (pass shared_dir)' % LOC_FILES[loc])
            fh = open(p, 'rb')
            r = self._loc[loc] = (mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ), fh)
        return r[0]

    def raw(self, pointer, size):
        """Raw bytes behind a raw pointer (top 2 bits = location, low 30 bits = file offset)."""
        mm = self._loc_map(pointer >> 30)
        o = pointer & 0x3FFFFFFF
        if o + size > len(mm):
            raise XboxFormatError('raw pointer 0x%08X (+%d) is outside %s' % (pointer, size, LOC_FILES.get(pointer >> 30, 'the map')))
        return bytes(mm[o:o + size])

    # ---- string ids -------------------------------------------------------------------------------------------
    def strings(self):
        if self._strings is None:
            h = self.header
            idx = struct.unpack_from('<%dI' % h['string_count'], self.m, h['string_index_offset'])
            tbl = bytes(self.m[h['string_table_offset']:h['string_table_offset'] + h['string_table_size']])
            self._strings = [tbl[o:tbl.find(b'\0', o)].decode('latin-1') for o in idx]
        return self._strings

    def string_id(self, sid):
        """string_id (high 8 bits = length, low 24 = index) -> text ('' for 0)."""
        if sid in (0, NULL):
            return ''
        i = sid & 0xFFFFFF
        s = self.strings()
        return s[i] if i < len(s) else None

    def sid_of(self, text, add=False):
        """string_id of `text` in this map ('' -> 0), or None.  add=True: a text the map's table lacks is appended to the in-memory
        table (not the file) and gets a new id, so data restored from another map (salvage_xbox: a stock map's block) keeps its names;
        string_id() then reads it like any other."""
        if not text:
            return 0
        memo = self.__dict__.get('_sid_of')
        s = self.strings()
        if memo is None:
            memo = self._sid_of = {}
            for i, x in enumerate(s):
                memo.setdefault(x, ((len(x) & 0xFF) << 24) | i)
        r = memo.get(text)
        if r is None and add:
            s.append(text)
            r = memo[text] = ((len(text) & 0xFF) << 24) | (len(s) - 1)
        return r
