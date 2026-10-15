# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Output string-id table for the Vista map.

A Vista multiplayer map's string table is the Vista ``shared.map`` table (10522 strings) as an EXACT PREFIX followed by the
map's own strings (every one of the 25 stock MP maps): string ids stored inside the
shared tags index shared.map's table, so the first 10522 entries may never move.

    string_id = (len(text) << 24) | index            (E tag_groups.cpp:13-15,424; verified on 30,712 MCC stringId fields)

Engine limits: <= 24576 string ids (k_cache_file_max_string_ids) and the NUL-terminated
text must stay below 0x60000 bytes (k_maximum_string_id_storage).  Exceeding either raises StringTableError with the
numbers, never a silent truncation.

    st = StringTable.from_vista_shared(r'<Vista maps folder>\\shared.map')      # or StringTable(list_of_strings)
    sid = st.sid('medic')            # appends when missing; returns the full 32-bit string id
    st.text(sid)                     # -> 'medic'
    st.remap_mcc(mcc_cache, mcc_sid) # MCC id -> Vista id, by TEXT (the MCC and Vista tables share only 278 entries)
    st.strings                       # the list to hand to VistaBuilder.set_strings()
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass

MAX_STRING_IDS = 24576            # k_cache_file_max_string_ids (g_string_id_index_buffer[0x6000])
MAX_STRING_BYTES = 0x60000        # k_maximum_string_id_storage: total NUL-terminated text must be < this
NULL_SID = 0                      # index 0 is the empty string, whose id is (0 << 24) | 0 == 0
INVALID_SID = 0xFFFFFFFF


class StringTableError(ValueError):
    """Engine limit exceeded or an unrepresentable string."""


def _blen(text):
    """Length byte of the string id.  The cache stores the table as latin-1 bytes (MCC and Vista strings are pure ASCII);
    anything else cannot be represented faithfully."""
    try:
        n = len(text.encode('latin-1'))
    except UnicodeEncodeError:
        raise StringTableError('string %r is not latin-1 encodable (Vista string table is single-byte)' % text)
    if n > 255:
        raise StringTableError('string of %d bytes does not fit the 8-bit length of a string id: %r' % (n, text[:40]))
    return n


class StringTable(object):
    """Ordered string table with an exact, frozen prefix (the Vista shared.map strings)."""

    def __init__(self, base_strings, max_ids=MAX_STRING_IDS, max_bytes=MAX_STRING_BYTES):
        """max_ids / max_bytes: engine limits (24448 ids are recommended so the debug ASSERT cannot trip)."""
        self.max_ids = max_ids
        self.max_bytes = max_bytes
        self._strings = list(base_strings)
        self.base_count = len(self._strings)
        self._index = {}
        self._bytes = 0
        for i, s in enumerate(self._strings):
            self._index.setdefault(s, i)          # duplicates would be a source bug; the first occurrence wins
            self._bytes += _blen(s) + 1
        self._check_limits()

    # ---- construction ---------------------------------------------------------------------------
    @classmethod
    def from_vista_shared(cls, shared, **kw):
        """shared: a path, or a VistaCache of shared.map.  kw: max_ids / max_bytes."""
        if isinstance(shared, str):
            from .vista_cache import VistaCache
            c = VistaCache(shared)
            try:
                return cls(c.all_strings(), **kw)
            finally:
                c.close()
        return cls(shared.all_strings(), **kw)

    # ---- limits ---------------------------------------------------------------------------------
    def _check_limits(self):
        if len(self._strings) > self.max_ids:
            raise StringTableError('too many string ids: %d > %d (k_cache_file_max_string_ids)' %
                                   (len(self._strings), self.max_ids))
        if self._bytes >= self.max_bytes:
            raise StringTableError('string text too large: %d bytes >= 0x%X (k_maximum_string_id_storage)' %
                                   (self._bytes, self.max_bytes))

    # ---- queries --------------------------------------------------------------------------------
    @property
    def strings(self):
        return self._strings

    def __len__(self):
        return len(self._strings)

    @property
    def text_bytes(self):
        """Bytes the table occupies in the engine's string buffer (each string + its NUL)."""
        return self._bytes

    @property
    def appended(self):
        """Strings added after the frozen prefix."""
        return self._strings[self.base_count:]

    def has(self, text):
        return text in self._index

    def index_of(self, text):
        return self._index.get(text)

    # ---- id <-> text -----------------------------------------------------------------------------
    def sid(self, text):
        """Vista string id of `text`, appending it to the table when it is missing."""
        if text is None:
            return NULL_SID
        i = self._index.get(text)
        if i is None:
            n = _blen(text)
            if len(self._strings) + 1 > self.max_ids:
                raise StringTableError('too many string ids: appending %r would exceed %d' % (text, self.max_ids))
            if self._bytes + n + 1 >= self.max_bytes:
                raise StringTableError('string text too large: appending %r would reach 0x%X bytes (limit 0x%X)' %
                                       (text, self._bytes + n + 1, self.max_bytes))
            i = len(self._strings)
            self._strings.append(text)
            self._index[text] = i
            self._bytes += n + 1
        return (_blen(text) << 24) | i

    def text(self, sid):
        """Text of a string id of this table (0 and 0xFFFFFFFF -> '')."""
        if sid in (0, INVALID_SID):
            return ''
        i = sid & 0xFFFFFF
        if i >= len(self._strings):
            raise StringTableError('string id 0x%08X: index %d >= table size %d' % (sid, i, len(self._strings)))
        s = self._strings[i]
        if (sid >> 24) != _blen(s):
            raise StringTableError('string id 0x%08X: length byte %d != len(%r)' % (sid, sid >> 24, s))
        return s

    def remap_mcc(self, mcc_cache, mcc_sid):
        """String id of the MCC map `mcc_cache` -> id of the same TEXT in this (Vista) table.  0 / 0xFFFFFFFF stay 0."""
        if mcc_sid in (0, INVALID_SID):
            return NULL_SID
        return self.sid(mcc_cache.string_id(mcc_sid))

    # ---- verification helpers ----------------------------------------------------------------------
    def is_prefix_of(self, other_strings):
        """True if the frozen base is an exact prefix of `other_strings` (e.g. a stock Vista MP map's table)."""
        n = self.base_count
        return len(other_strings) >= n and list(other_strings[:n]) == self._strings[:n]

    def pack(self):
        """(index bytes uint32[n], table bytes) exactly as vista_writer.pack_string_table would write them."""
        import struct
        offs, parts, cur = [], [], 0
        for s in self._strings:
            b = s.encode('latin-1') + b'\0'
            offs.append(cur)
            parts.append(b)
            cur += len(b)
        return struct.pack('<%dI' % len(offs), *offs), b''.join(parts)


def selftest(shared_map=None):
    if shared_map is None:                       # the stock Vista shared.map (h2conv.paths: --vista-maps / H2CONV_VISTA_MAPS / settings / auto)
        import os
        from . import paths
        shared_map = os.path.join(paths.vista_maps_dir(), 'shared.map')
    st = StringTable.from_vista_shared(shared_map)
    assert st.base_count == 10522, st.base_count
    n0 = len(st)
    a = st.sid('default')
    assert a == (7 << 24) | 1, hex(a)
    assert len(st) == n0
    b = st.sid('definitely_not_in_shared_table_xyz')
    assert len(st) == n0 + 1 and st.text(b) == 'definitely_not_in_shared_table_xyz'
    assert st.sid('definitely_not_in_shared_table_xyz') == b
    return True


if __name__ == '__main__':
    print('strings selftest', 'PASS' if selftest() else 'FAIL')
