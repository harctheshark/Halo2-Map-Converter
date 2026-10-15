# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Output string-id table of a REVERSE (Vista -> MCC) map.

Facts (all measured):

* string_id = (len(utf-8 text) << 24) | index into the header string table - the same scheme as Vista (which uses latin-1).
* The first 2463 strings of EVERY MCC map are identical (67 of 67 maps: 43 stock maps + 24 maps built with the H2EK); string 2462 is
  'hud_show_terminal', string 2463 is already map specific.  Those 2463 strings are the engine's compiled-in string ids
  (Vista: 2439 = the 2439 `_string_id_*` constants of tag_groups.h; common prefix of all 43 stock Vista maps = 2439 exactly).
  Inside the builtin region MCC differs from Vista after only 278 entries (MCC inserted 24 strings), so ids can never be copied
  from a Vista map - every Vista id is re-mapped BY TEXT (`remap_vista`).
* A self-contained H2EK map (headlong 13105 strings, chronopolis 13619, triplicate 12974, 01b_spacestation 17342) is
  [2463 builtin strings] + map strings in tool order; it does NOT contain MCC shared.map's table as a prefix (common prefix with
  shared.map = exactly 2463 in all 24 H2EK maps), so the only frozen prefix a reverse map needs is the builtin region.
* The table is stored as cumulative NUL-terminated utf-8 text with an index of u32 byte offsets (rebuilt byte-exactly from the
  string list for H2EK headlong, MCC shared.map and MCC headlong; no padding between strings); `pack()` produces exactly that.
* Limits: the Vista engine allows 24576 ids / < 0x60000 text bytes.  The MCC limit has NOT been measured; the largest
  stock MCC table is 17342 ids / 328,681 bytes (H2EK 01b_spacestation).  The defaults below are the Vista numbers (conservative);
  StringTableError is raised instead of silently truncating.

    st = McStringTable.from_mcc_builtin()            # 2463 builtin strings frozen, nothing else
    sid = st.sid('medic')                            # appends when missing
    st.remap_vista(vista_cache, vista_sid)           # Vista id -> MCC id by text
    index_bytes, blob = st.pack()
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:                                                   # pragma: no cover
    pass

import os
import struct

from .. import paths as _paths

BUILTIN_COUNT = 2463                  # engine-builtin strings = common prefix of every MCC map
VISTA_BUILTIN_COUNT = 2439            # the same for Vista (tag_groups.h has 2439 _string_id_ constants)
MAX_STRING_IDS = 24576                # conservative (Vista k_cache_file_max_string_ids); stock MCC max seen 17342
MAX_STRING_BYTES = 0x60000            # conservative (Vista k_maximum_string_id_storage); stock MCC max seen 328,681
NULL_SID = 0
INVALID_SID = 0xFFFFFFFF
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MCC_DECOMP = os.path.join(_paths.work_dir(), 'mcc_decompressed')


class McStringTableError(ValueError):
    """Engine limit exceeded or an unrepresentable string."""


StringTableError = McStringTableError


def blen(text):
    """Length byte of the string id of `text` (utf-8 byte count, <= 255)."""
    n = len(text.encode('utf-8'))
    if n > 255:
        raise McStringTableError('string of %d bytes does not fit the 8-bit length of a string id: %r' % (n, text[:40]))
    return n


class McStringTable(object):
    """Ordered MCC string table with a frozen prefix (the engine-builtin strings).  API-compatible with the McStringTable that
    h2conv/rev/transcode.py defines (sid / text / has / strings / appended / base_count / from_mcc_shared)."""

    def __init__(self, base_strings=(), max_ids=MAX_STRING_IDS, max_bytes=MAX_STRING_BYTES):
        self.max_ids = max_ids
        self.max_bytes = max_bytes
        self._strings = [s if s is not None else '' for s in base_strings]
        self.base_count = len(self._strings)
        self._index = {}
        self._bytes = 0
        for i, s in enumerate(self._strings):
            self._index.setdefault(s, i)                       # duplicates would be a source bug; the first occurrence wins
            self._bytes += blen(s) + 1
        self._check()

    # ---- construction -----------------------------------------------------------------------------------------------
    @classmethod
    def from_mcc_builtin(cls, source=None, count=BUILTIN_COUNT, **kw):
        """The frozen prefix = the first `count` (2463) strings of an MCC map (`source`: MccCache, path, or None = the decompressed
        stock shared.map in <work folder>/mcc_decompressed or the MCC install)."""
        strings = _mcc_strings(source)
        if len(strings) < count:
            raise McStringTableError('source table has only %d strings (< %d builtin)' % (len(strings), count))
        return cls(strings[:count], **kw)

    @classmethod
    def from_mcc_shared(cls, shared=None, **kw):
        """Compatibility with transcode.McStringTable: the WHOLE table of `shared` (path | MccCache | None) as frozen prefix.  Not needed
        for self-contained maps (wastes ~10k ids); use from_mcc_builtin()."""
        return cls(_mcc_strings(shared), **kw)

    # ---- limits -----------------------------------------------------------------------------------------------------
    def _check(self):
        if len(self._strings) > self.max_ids:
            raise McStringTableError('too many string ids: %d > %d' % (len(self._strings), self.max_ids))
        if self.max_bytes and self._bytes >= self.max_bytes:
            raise McStringTableError('string text too large: %d bytes >= 0x%X' % (self._bytes, self.max_bytes))

    # ---- queries ----------------------------------------------------------------------------------------------------
    @property
    def strings(self):
        return self._strings

    def __len__(self):
        return len(self._strings)

    @property
    def text_bytes(self):
        """Bytes of the string blob (every string + its NUL)."""
        return self._bytes

    @property
    def appended(self):
        return self._strings[self.base_count:]

    def has(self, text):
        return text in self._index

    def index_of(self, text):
        return self._index.get(text)

    # ---- id <-> text ------------------------------------------------------------------------------------------------
    def sid(self, text):
        """MCC string id of `text` (empty / None -> 0), appending the text when it is missing."""
        if not text:
            return NULL_SID
        i = self._index.get(text)
        if i is None:
            n = blen(text)
            if len(self._strings) + 1 > self.max_ids:
                raise McStringTableError('too many string ids: appending %r would exceed %d' % (text, self.max_ids))
            if self.max_bytes and self._bytes + n + 1 >= self.max_bytes:
                raise McStringTableError('string text too large: appending %r would reach 0x%X bytes (limit 0x%X)' %
                                         (text, self._bytes + n + 1, self.max_bytes))
            i = len(self._strings)
            self._strings.append(text)
            self._index[text] = i
            self._bytes += n + 1
        return (blen(text) << 24) | i

    def text(self, sid):
        if sid in (0, INVALID_SID):
            return ''
        i = sid & 0xFFFFFF
        if i >= len(self._strings):
            raise McStringTableError('string id 0x%08X: index %d >= table size %d' % (sid, i, len(self._strings)))
        s = self._strings[i]
        if (sid >> 24) != blen(s):
            raise McStringTableError('string id 0x%08X: length byte %d != len(%r)' % (sid, sid >> 24, s))
        return s

    def remap_vista(self, vista_cache, vista_sid):
        """Vista string id (of `vista_cache`) -> MCC id of the same TEXT.  0 / 0xFFFFFFFF -> 0."""
        if vista_sid in (0, INVALID_SID):
            return NULL_SID
        return self.sid(vista_cache.string_id(vista_sid))

    def remap_mcc(self, mcc_cache, mcc_sid):
        """MCC string id of ANOTHER MCC map (a donor) -> id in this table, by text."""
        if mcc_sid in (0, INVALID_SID):
            return NULL_SID
        return self.sid(mcc_cache.string_id(mcc_sid))

    # ---- verification / serialisation -------------------------------------------------------------------------------
    def is_prefix_of(self, other_strings):
        n = self.base_count
        return len(other_strings) >= n and list(other_strings[:n]) == self._strings[:n]

    def pack(self):
        """(index bytes u32[n] of byte offsets, string blob) exactly as MCC stores them (cumulative NUL-terminated utf-8)."""
        offs, parts, cur = [], [], 0
        for s in self._strings:
            b = s.encode('utf-8') + b'\0'
            offs.append(cur)
            parts.append(b)
            cur += len(b)
        return struct.pack('<%dI' % len(offs), *offs), b''.join(parts)

    def summary(self):
        return {'strings': len(self._strings), 'builtin_prefix': self.base_count, 'appended': len(self._strings) - self.base_count,
                'text_bytes': self._bytes, 'max_ids': self.max_ids, 'max_bytes': self.max_bytes}


def _mcc_strings(source):
    """string list of an MccCache / path / None (stock shared.map)."""
    if source is None:
        for d in (MCC_DECOMP, _paths.mcc_maps_dir(required=False)):
            if not d:
                continue
            p = os.path.join(d, 'shared.map')
            if os.path.isfile(p):
                source = p
                break
        else:
            raise McStringTableError('no stock MCC shared.map found')
    if isinstance(source, str):
        from ..mcc_cache import MccCache
        c = MccCache(source, load_names=False)
        try:
            return [s if s is not None else '' for s in c.string_ids]
        finally:
            c.close()
    return [s if s is not None else '' for s in source.string_ids]


def selftest(h2ek=None):
    """PASS/FAIL checks against real maps (nothing is written).  h2ek = an H2EK-built headlong.map to rebuild the string table of
    (default: headlong.map in the H2EK maps folder, when one is configured)."""
    from ..mcc_cache import MccCache
    if h2ek is None:
        d = _paths.h2ek_maps_dir()
        h2ek = os.path.join(d, 'headlong.map') if d else ''
    st = McStringTable.from_mcc_builtin()
    assert st.base_count == BUILTIN_COUNT == len(st)
    assert st.strings[2462] == 'hud_show_terminal', st.strings[2462]
    a = st.sid('default')
    assert a == (7 << 24) | 1 and len(st) == BUILTIN_COUNT
    b = st.sid('definitely_not_in_the_table_xyz')
    assert st.text(b) == 'definitely_not_in_the_table_xyz' and len(st) == BUILTIN_COUNT + 1
    assert st.sid('definitely_not_in_the_table_xyz') == b
    if os.path.isfile(h2ek):
        c = MccCache(h2ek, load_names=False)
        s = [x if x is not None else '' for x in c.string_ids]
        assert McStringTable.from_mcc_builtin().is_prefix_of(s)
        full = McStringTable(s)                                         # rebuild the H2EK headlong table byte-exactly
        idx, blob = full.pack()
        h = c.header
        assert blob == bytes(c.mm[h['string_table_offset']:h['string_table_offset'] + h['string_table_size']])
        assert idx == bytes(c.mm[h['string_index_table_offset']:h['string_index_table_offset'] + 4 * h['string_table_count']])
        c.close()
    return True


if __name__ == '__main__':
    print('rev strings selftest', 'PASS' if selftest() else 'FAIL')
