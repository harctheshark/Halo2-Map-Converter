# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
r"""Repair the blank custom-map-list name of an ALREADY converted Vista multiplayer map (no re-conversion).

    py -3 -m h2conv.mpnames <in.map> <out.map>         write a fixed copy (never modifies <in.map>)
    py -3 -m h2conv.mpnames <in.map> --check           report only

Why: see handlers/mp_level_names.py.  scnr Level Data[0].Multiplayer[0] English Name / Descriptions that are empty are
filled from the map's header language pack ('name' / 'description' of the map's own unic tag).  The patch is in place and
same-size (utf16 fields inside the scnr tag data), then the header 0x2F0 XOR32 checksum and the 0x150 JAMCRC are recomputed
exactly as vista_writer does (the crc is first re-derived from the UNPATCHED file; when that does not reproduce the stored
value the stored one is kept and a note is printed).
"""
import argparse
import os
import shutil
import struct
import sys

from .vista_cache import VistaCache, HEADER_SIZE, xor32, crc_from_items, reconstruct_write_items
from .handlers import locale as L
from .handlers.mp_level_names import (N_LANG, OFF_LEVEL_DATA, LEVEL_DATA_MP_BLOCK, MP_ELEMENT_SIZE, NAME_OFF, NAME_SIZE, DESC_OFF,
                                      DESC_SIZE, fill_block, texts_from_langs, _read)


def _lowprio():
    try:
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    except Exception:                                   # noqa: BLE001
        pass


def locate_mp(c):
    """(file offset, count) of scnr.level_data[0].multiplayer[], or None."""
    scn = c.find('scnr')
    if not scn:
        return None
    d = c.read_tag(scn[0])
    cnt, addr = struct.unpack_from('<II', d, OFF_LEVEL_DATA)
    if cnt < 1 or not addr:
        return None
    el = c.read_addr(addr, 24)
    mc, ma = struct.unpack_from('<II', el, LEVEL_DATA_MP_BLOCK)
    if mc < 1 or not ma:
        return None
    return c.addr_to_offset(ma), mc


def header_texts(c):
    if c.header.language_pack_offset == 0xFFFFFFFF:
        return None
    arr = L.vista_pack(c, 'header')
    names, descs = texts_from_langs(arr.langs)
    L._ARRAYS.pop((os.path.abspath(c.path), 'vista', 'header'), None)
    return names, descs


def analyse(path):
    """dict: has_mp, name, desc, would_write (fields), source name."""
    c = VistaCache(path)
    try:
        r = {'path': path, 'type': c.header.type, 'mp': None, 'english_name': None, 'pack_name': None, 'fields': 0}
        loc = locate_mp(c)
        ht = header_texts(c)
        r['pack_name'] = ht[0][0] if ht else None
        if loc:
            off, _ = loc
            blob = bytearray(c.raw(off, MP_ELEMENT_SIZE))
            r['mp'] = off
            r['english_name'] = _read(blob, NAME_OFF, NAME_SIZE)
            if ht:
                r['fields'] = fill_block(blob, *ht)
        return r
    finally:
        c.close()


def _crc_of(path):
    c = VistaCache(path)
    try:
        try:
            items = reconstruct_write_items(c)
            crc, problems = crc_from_items(c, items)
        except Exception as ex:                         # noqa: BLE001 - converted maps: the payload boundaries are not reconstructible
            return None, ['payload layout not reconstructible: %s' % ex], c.header.crc
        return crc, problems, c.header.crc
    finally:
        c.close()


def fix(src, dst, name=None, desc=None, force=False):
    c = VistaCache(src)
    try:
        if c.header.type != 1:
            raise SystemExit('%s: not a multiplayer map (type %d)' % (src, c.header.type))
        loc = locate_mp(c)
        if not loc:
            raise SystemExit('%s: scnr Level Data[0] has no Multiplayer element (the engine falls back to the header name; nothing to fix)' % src)
        off, _ = loc
        ht = header_texts(c)
        if ht is None:
            raise SystemExit('%s: no header language pack' % src)
        blob = bytearray(c.raw(off, MP_ELEMENT_SIZE))
    finally:
        c.close()
    if name:
        ht = ([name] * N_LANG, ht[1])
    if desc:
        ht = (ht[0], [desc] * N_LANG)
    n = fill_block(blob, *ht, force=force)
    if not n:
        print('%s: nothing to fill (English Name = %r)' % (src, _read(blob, NAME_OFF, NAME_SIZE)))
        return 0
    crc0, problems0, stored = _crc_of(src)
    crc_ok = (crc0 == stored and not problems0)
    shutil.copyfile(src, dst)
    with open(dst, 'r+b') as f:
        f.seek(off)
        f.write(bytes(blob))
    # header: checksum + crc
    crc1, problems1, _ = _crc_of(dst) if crc_ok else (None, [], None)
    with open(dst, 'r+b') as f:
        hdr = bytearray(f.read(HEADER_SIZE))
        size = f.seek(0, 2)
    import mmap
    with open(dst, 'r+b') as f:
        mm = mmap.mmap(f.fileno(), 0)
        try:
            xs = xor32(mm, HEADER_SIZE, len(mm))
        finally:
            mm.close()
    struct.pack_into('<I', hdr, 0x2F0, xs)
    if crc_ok:
        struct.pack_into('<I', hdr, 0x150, crc1)
    with open(dst, 'r+b') as f:
        f.write(bytes(hdr))
    print('%s -> %s: %d fields written (English Name now %r); checksum %#010x; crc %s' % (
        os.path.basename(src), dst, n, ht[0][0], xs,
        ('%#010x (was %#010x)' % (crc1, stored)) if crc_ok else 'KEPT %#010x (reconstruction of the source did not reproduce it: %s %s)' % (stored, crc0 if crc0 is None else '%#010x' % crc0, problems0[:2])))
    return n


def main(argv=None):
    _lowprio()
    ap = argparse.ArgumentParser(prog='h2conv.mpnames', description=__doc__.split('\n')[0])
    ap.add_argument('src')
    ap.add_argument('dst', nargs='?')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--name', help='use this text as the list name in every language (default: the own localized name of the map)')
    ap.add_argument('--desc', help='use this text as the description in every language')
    ap.add_argument('--force', action='store_true', help='also replace a name / description the map already has (with --name / --desc / --from-mcc)')
    ap.add_argument('--from-mcc', metavar='MCC_MAP', help="take the name and description from the MCC mod's MapInfo JSON of this "
                    "source map (<mod>\\multiplayer\\<stem>.json, the title the MCC menu shows); --name / --desc still win")
    a = ap.parse_args(argv)
    if a.from_mcc:
        from .handlers.mp_level_names import mcc_mapinfo_texts
        mi = mcc_mapinfo_texts(a.from_mcc)
        if not mi:
            raise SystemExit('%s: no MCC MapInfo JSON found for this map' % a.from_mcc)
        a.name = a.name or mi[0] or None
        a.desc = a.desc or mi[1] or None
    if a.check or not a.dst:
        r = analyse(a.src)
        print('%s: type=%s mp_element=%s english_name=%r pack_name=%r would_write=%d' % (
            os.path.basename(a.src), r['type'], r['mp'], r['english_name'], r['pack_name'], r['fields']))
        return 0
    if os.path.abspath(a.src) == os.path.abspath(a.dst):
        raise SystemExit('refusing to overwrite the source')
    fix(a.src, a.dst, a.name, a.desc, a.force)
    return 0


if __name__ == '__main__':
    sys.exit(main())
