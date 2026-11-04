# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Xbox sound gestalt (ugh!) access.  The Xbox ugh! has the Vista layout (Plugins\\Halo2\\ugh!.xml, no Xbox override) and the same
12-byte chunk records {u32 file offset (location bits), u32 size+flags, i32 runtime index}; the audio is the same Xbox ADPCM / WMA
bytes.  A snd! body (20 bytes) indexes the gestalt of ITS map, so equal sounds in two maps differ in
those indices: `fingerprint()` resolves a snd! through its gestalt into map-independent content for the link-vs-convert test."""
import hashlib
import struct

from .. import tagtree as T
from . import layouts as XL

NULL16 = 0xFFFF


class Gestalt(object):
    """The (single) ugh! of an XboxCache, decoded once."""

    def __init__(self, xc):
        self.xc = xc
        t = xc.find('ugh!')
        self.tag = t[0] if t else None
        self.root = T.decode_tag(xc.read, XL.vista_layouts()['ugh!'], self.tag.addr, 'ugh!') if self.tag else None
        r = self.root
        self.playback = r.block('Playback Parameters') if r else []
        self.scales = r.block('Scales') if r else []
        self.import_names = r.block('Import Names') if r else []
        self.pr_params = r.block('Pitch Range Parameters') if r else []
        self.pitch_ranges = r.block('Pitch Ranges') if r else []
        self.perms = r.block('Permutations') if r else []
        self.custom = r.block('Custom Playbacks') if r else []
        self.chunks = r.block('Permutation Chunks') if r else []
        self.promotions = r.block('Promotions') if r else []
        self.extra = r.block('Extra Info') if r else []
        self._chunk_hash = {}

    def name(self, i):
        if 0 <= i < len(self.import_names):
            return self.xc.string_id(self.import_names[i].u32(0))
        return None

    def chunk_digest(self, k):
        """Map-independent identity of chunk k: (size word, md5 of the audio bytes)."""
        h = self._chunk_hash.get(k)
        if h is None:
            c = self.chunks[k]
            ptr, szw = c.u32(0), c.u32(4)
            size = szw & 0x3FFFFFFF
            h = self._chunk_hash[k] = (szw, hashlib.md5(self.xc.raw(ptr, size)).hexdigest())
        return h

    def permutation_info(self, p, names=True):
        e = self.perms[p]
        first, cnt = e.i16(0xC), e.i16(0xE)
        return (self.name(e.i16(0)) if names else None, bytes(e.data[2:0xC]),
                tuple(self.chunk_digest(first + k) for k in range(max(cnt, 0))))

    def fingerprint(self, snd_data, names=True):
        """snd! tag bytes (20-byte body) -> hashable content of the sound (indices resolved through this gestalt).
        names=False leaves out the pitch range / permutation import names: dawn_of_warfare's re-merged gestalt points some of them at
        an empty name ('' for '|default|') while the audio is byte-identical, so its dialog matched nothing."""
        b = bytes(snd_data[:0x14])
        body = b[0:6] + b[0xA:0xB] + b[0x10:0x14]
        pb, pr, prc = struct.unpack_from('<h', b, 6)[0], struct.unpack_from('<h', b, 8)[0], b[0xA]
        sc, pm, cp = struct.unpack_from('<b', b, 0xB)[0], struct.unpack_from('<b', b, 0xC)[0], struct.unpack_from('<b', b, 0xD)[0]
        ex = struct.unpack_from('<h', b, 0xE)[0]
        out = [body,
               bytes(self.playback[pb].data) if 0 <= pb < len(self.playback) else None,
               bytes(self.scales[sc].data) if 0 <= sc < len(self.scales) else None]
        for i in range(prc):
            if not 0 <= pr + i < len(self.pitch_ranges):
                out.append(('bad pitch range', pr + i))
                continue
            e = self.pitch_ranges[pr + i]
            pp = e.i16(2)
            first, cnt = e.i16(8), e.i16(0xA)
            out.append((self.name(e.i16(0)) if names else None, bytes(self.pr_params[pp].data) if 0 <= pp < len(self.pr_params) else None,
                        tuple(self.permutation_info(first + k, names) for k in range(max(cnt, 0)))))
        for idx, blk in ((pm, self.promotions), (cp, self.custom), (ex, self.extra)):
            if 0 <= idx < len(blk):
                out.append(_node_bytes(blk[idx], self.xc))
        return tuple(out)

    def defects(self, snd_data):
        """What makes a snd! (20-byte body) unplayable as stored, by the checks validate._check_snds makes on the output (the chunks are
        copied as they are): sample rate / encoding / compression out of range, a scale / promotion / extra-info index or pitch range /
        permutation / chunk range past its block, an Xbox ADPCM chunk that is no whole number of blocks or has an invalid block header
        (step index > 88, reserved byte != 0, padding nibble != 0: true for 556,460 / 556,460 stock blocks).  Playback and custom
        playback indices past the end are not counted: the converter appends the elements the Xbox read there
        (convert_xbox.extend_past_end_*).  [] = none (convert_xbox.stock_named_broken, w3 2026-10-27)."""
        b = bytes(snd_data[:0x14])
        out = []
        rate, enc, comp = b[3], b[4], b[5]
        if rate > 2 or enc > 2 or comp > 4:
            out.append('sample rate / encoding / compression %d / %d / %d out of range' % (rate, enc, comp))
        pr, prc = struct.unpack_from('<h', b, 8)[0], b[0xA]
        sc, pm = struct.unpack_from('<b', b, 0xB)[0], struct.unpack_from('<b', b, 0xC)[0]
        ex = struct.unpack_from('<h', b, 0xE)[0]
        for nm, v, blk in (('scale', sc, self.scales), ('promotion', pm, self.promotions), ('extra info', ex, self.extra)):
            if v != -1 and not 0 <= v < len(blk):
                out.append('%s index %d outside 0..%d' % (nm, v, len(blk)))
        if prc and (pr < 0 or pr + prc > len(self.pitch_ranges)):
            out.append('pitch range %d..+%d outside 0..%d' % (pr, prc, len(self.pitch_ranges)))
            return out
        blk = 36 * (2 if enc == 1 else 1) if comp == 1 and enc != 2 else 0
        for i in range(pr, pr + prc):
            e = self.pitch_ranges[i]
            first, cnt = e.i16(8), e.i16(0xA)
            if first < 0 or first + cnt > len(self.perms):
                out.append('pitch range %d permutations %d..+%d outside 0..%d' % (i, first, cnt, len(self.perms)))
                continue
            for p in range(first, first + max(cnt, 0)):
                c0, cn = self.perms[p].i16(0xC), self.perms[p].i16(0xE)
                if c0 < 0 or c0 + cn > len(self.chunks):
                    out.append('permutation %d chunks %d..+%d outside 0..%d' % (p, c0, cn, len(self.chunks)))
                    continue
                for k in range(c0, c0 + max(cn, 0)) if blk else ():
                    c = self.chunks[k]
                    size = c.u32(4) & 0x7FFFFFFF
                    if size % blk:
                        out.append('ADPCM chunk %d: %d bytes, not whole %d-byte blocks' % (k, size, blk))
                        continue
                    try:
                        v = self.xc.raw(c.u32(0), size)
                    except Exception:                                    # noqa: BLE001
                        continue                         # unreadable: no evidence (the converter reports the copy)
                    hi = (67, 71) if blk == 72 else (35,)
                    if max(v[2::blk] or b'\0') > 88 or any(v[3::blk]) or (blk == 72 and (max(v[6::blk] or b'\0') > 88 or any(v[7::blk]))) or \
                            any(x >> 4 for h in hi for x in v[h::blk]):
                        out.append('ADPCM chunk %d has an invalid block header' % k)
        return out


_PLAN_RULES = None


def _node_bytes(n, c):
    """Struct bytes of a node and its children (placement independent): block / dataref pointer words zeroed, and by
    plan_xbox.field_kind's rules references as (group, name), string ids as text, raw data by its bytes, runtime words dropped.
    Extra Info holds the owning snd!'s DATUM (+0x20) and a raw pointer (+0x08) to the lipsync resource: compared as bytes, every
    dialog sound with lipsync differed between any two maps - Foundry's 22 masterchief dialog sounds converted and tainted udlg, bipd
    and hlmt masterchief_mp (2026-10-26)."""
    global _PLAN_RULES
    if _PLAN_RULES is None:
        from . import plan_xbox as PX                                        # plan_xbox imports this module: resolved on first use
        _PLAN_RULES = (PX.field_kind, PX.raw_size, PX._raw_token, PX._label)
    field_kind, raw_size, _raw_token, _label = _PLAN_RULES
    d = bytearray(n.data)
    parts = []
    for f in n.fields:
        k = field_kind(n, f)
        if k in ('block', 'data'):
            struct.pack_into('<I', d, f.offset + 4, 0)
            if k == 'block':
                parts.extend(_node_bytes(x, c) for x in n.blocks.get(f.offset) or [])
            else:
                parts.append(n.datas.get(f.offset) or b'')
        elif k == 'ref':
            o = f.offset + (4 if f.size == 8 else 0)
            parts.append(('ref', _label(c, n.u32(o))))
            struct.pack_into('<I', d, o, 0)
        elif k == 'sid':
            parts.append(('sid', c.string_id(n.u32(f.offset))))
            struct.pack_into('<I', d, f.offset, 0)
        elif k == 'skip':
            d[f.offset:f.offset + f.size] = bytes(f.size)
        elif k == 'raw':
            parts.append(_raw_token(c, n.u32(f.offset), raw_size(n, f)))
            struct.pack_into('<I', d, f.offset, 0)
    return (bytes(d), tuple(parts))


# ---- WMA (compression 4) ---------------------------------------------------------------------------------------------------
# Xbox stores a WMA permutation as a whole ASF file; Vista as a DirectShow AM_MEDIA_TYPE (64 bytes: majortype 'auds', subtype = the
# format tag's GUID, bFixedSizeSamples 1, bTemporalCompression 0, lSampleSize = nBlockAlign, formattype FORMAT_WaveFormatEx, cbFormat)
# + the WAVEFORMATEX of the ASF stream + the media object bytes of every data packet, packet headers and padding dropped.  Bungie's 480
# stock pairs (Xbox shared.map vs Vista shared.map) match this byte for byte.  Copied raw, Vista's WMA decoder crashed on the ASF
# header: WMADMOD.DLL+0x37BF0 right after spawning on Tortuga, whose announcer is the mod's own (2026-11-02).
ASF_HEADER = bytes.fromhex('3026b2758e66cf11a6d900aa0062ce6c')
ASF_DATA = bytes.fromhex('3626b2758e66cf11a6d900aa0062ce6c')
ASF_STREAM_PROPS = bytes.fromhex('9107dcb7b7a9cf118ee600c00c205365')
ASF_FILE_PROPS = bytes.fromhex('a1dcab8c47a9cf118ee400c00c205365')
_GUID_TAIL = bytes.fromhex('000010008000' '00aa00389b71')
MEDIATYPE_AUDIO = b'auds' + _GUID_TAIL
FORMAT_WAVEFORMATEX = bytes.fromhex('819f580556c3ce11bf0100aa0055595a')


class WmaError(ValueError):
    pass


_TRUNCATED = [0]          # conversions that kept only the whole packets of an over-long ASF (read by convert_xbox for its stats)


def is_asf(data):
    return bytes(data[:16]) == ASF_HEADER


def _vlen(t, d, o):
    """ASF variable-size field of length type t (0 none, 1 byte, 2 word, 3 dword) at o: (value, new offset)."""
    if t == 0:
        return 0, o
    n = (0, 1, 2, 4)[t]
    if o + n > len(d):
        raise WmaError('packet field past the end')
    return int.from_bytes(d[o:o + n], 'little'), o + n


def xbox_wma_to_vista(asf):
    """One Xbox WMA chunk (a complete ASF file) -> the Vista chunk bytes.  Raises WmaError on anything it does not understand."""
    d = bytes(asf)
    if not is_asf(d):
        raise WmaError('not an ASF file')
    hsize, nobj = struct.unpack_from('<QI', d, 16)
    o, wfx, packet_size = 30, None, None
    for _ in range(nobj):
        if o + 24 > hsize:
            raise WmaError('ASF header object past the header')
        gid, size = d[o:o + 16], struct.unpack_from('<Q', d, o + 16)[0]
        if size < 24:
            raise WmaError('ASF header object of size %d' % size)
        if gid == ASF_STREAM_PROPS:
            if wfx is not None:
                raise WmaError('more than one stream')
            tsd_len = struct.unpack_from('<I', d, o + 24 + 40)[0]
            wfx = d[o + 24 + 54:o + 24 + 54 + tsd_len]
        elif gid == ASF_FILE_PROPS:
            mn, mx = struct.unpack_from('<II', d, o + 24 + 68)
            if mn != mx:
                raise WmaError('variable packet size %d..%d' % (mn, mx))
            packet_size = mn
        o += size
    if wfx is None or len(wfx) < 18 or packet_size is None:
        raise WmaError('no stream / file properties')
    tag, block_align, cb = struct.unpack_from('<H', wfx, 0)[0], struct.unpack_from('<H', wfx, 12)[0], struct.unpack_from('<H', wfx, 16)[0]
    wfx = wfx[:18 + cb]
    o = hsize
    if d[o:o + 16] != ASF_DATA:
        raise WmaError('no data object after the header')
    dsize, npk = struct.unpack_from('<Q', d, o + 16)[0], struct.unpack_from('<Q', d, o + 40)[0]
    p0 = o + 50
    if p0 + npk * packet_size > len(d) or o + dsize > len(d):
        # an injected sound larger than the chunk it was written into (Tortuga's round_over / suicide: 9 packets declared, the stock
        # chunk size holds 8 and part of the 9th): the Xbox reads only the chunk, so only the whole packets inside it are kept
        whole = max(0, (len(d) - p0) // packet_size)
        if not whole:
            raise WmaError('data object past the end, no whole packet')
        npk = whole
        _TRUNCATED[0] += 1
    payload = bytearray()
    for k in range(npk):
        pk = d[p0 + k * packet_size:p0 + (k + 1) * packet_size]
        q = 0
        if pk[0] & 0x80:                                   # error correction data
            q = 1 + (pk[0] & 0x0F)
        ltf, pf = pk[q], pk[q + 1]
        q += 2
        if ltf & 0x01:
            raise WmaError('multiple payloads per packet')
        plen, q = _vlen((ltf >> 5) & 3, pk, q)
        _, q = _vlen((ltf >> 1) & 3, pk, q)               # sequence
        pad, q = _vlen((ltf >> 3) & 3, pk, q)
        q += 6                                             # send time, duration
        q += 1                                             # stream number
        _, q = _vlen((pf >> 4) & 3, pk, q)                 # media object number
        _, q = _vlen((pf >> 2) & 3, pk, q)                 # offset into media object
        rep, q = _vlen(pf & 3, pk, q)
        if rep == 1:
            raise WmaError('compressed payload')
        q += rep
        end = (plen or packet_size) - pad
        if not q <= end <= len(pk):
            raise WmaError('packet %d payload %d..%d' % (k, q, end))
        payload += pk[q:end]
    head = (MEDIATYPE_AUDIO + struct.pack('<I', tag) + _GUID_TAIL + struct.pack('<IIi', 1, 0, block_align) + FORMAT_WAVEFORMATEX +
            struct.pack('<I', len(wfx)))
    return head + wfx + bytes(payload)


# ---- unic strings ---------------------------------------------------------------------------------------------------
def locale_tables(xc):
    """The 9 language tables of the map's matg (+0x190, 0x1C per language): list of [(string id, text bytes)]."""
    g = xc.globals_tag
    if g is None or not g.addr:
        return [[] for _ in range(9)]
    d = xc.read(g.addr, 0x190 + 0x1C * 9)
    out = []
    for L in range(9):
        cnt, tsz, ito, dto = struct.unpack_from('<IIII', d, 0x190 + 0x1C * L)
        if not cnt:
            out.append([])
            continue
        idx = struct.unpack_from('<%dI' % (2 * cnt), xc.m, ito & 0x7FFFFFFF)
        blob = bytes(xc.m[dto & 0x7FFFFFFF:(dto & 0x7FFFFFFF) + tsz])
        out.append([(idx[2 * k], blob[idx[2 * k + 1]:blob.find(b'\0', idx[2 * k + 1])]) for k in range(cnt)])
    return out


def unic_strings(xc, tag, tables=None):
    """Per language: [(string id TEXT, utf-8 bytes)] of one unic tag (its Strings unicList at +0x10: u16 start, u16 count)."""
    tables = tables if tables is not None else locale_tables(xc)
    d = xc.read(tag.addr, 0x34)
    out = []
    for L in range(9):
        start, cnt = struct.unpack_from('<HH', d, 0x10 + 4 * L)
        out.append([(xc.string_id(s), t) for s, t in tables[L][start:start + cnt]])
    return out
