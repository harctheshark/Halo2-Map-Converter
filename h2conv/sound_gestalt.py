# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Readers for the sound cache-file gestalt ('ugh!') + 'snd!' tags of Vista (v8) and MCC (v13) maps.
Depends only on h2conv.sound_mapio.H2Map.

Vista ugh! (0x58 bytes) block order  : playbacks, scales, import_names, pitch_range_params, pitch_ranges,
                                       permutations, custom_playbacks, runtime_perm_flags, chunks, promotions, extra_info
MCC   ugh! (0x78 bytes) block order  : codecs, playbacks, scales, import_names, pitch_range_params, pitch_ranges,
                                       permutations, custom_playbacks, reflections, lowpass, remastered,
                                       runtime_perm_flags, chunks, promotions, extra_info

"""
import struct
import mmap
import os

from . import paths as _paths
from .sound_mapio import H2Map

VISTA_ORDER = ['playbacks', 'scales', 'import_names', 'pitch_range_params', 'pitch_ranges', 'permutations',
               'custom_playbacks', 'runtime_perm_flags', 'chunks', 'promotions', 'extra_info']
MCC_ORDER = ['codecs', 'playbacks', 'scales', 'import_names', 'pitch_range_params', 'pitch_ranges', 'permutations',
             'custom_playbacks', 'reflections', 'lowpass', 'remastered', 'runtime_perm_flags', 'chunks',
             'promotions', 'extra_info']

SAMPLE_RATE_HZ = {0: 22050, 1: 44100, 2: 32000, 3: 48000}
ENCODING_NAME = {0: 'mono', 1: 'stereo', 2: 'codec', 3: 'quad'}
COMPRESSION_NAME = {0: 'pcm_be', 1: 'xbox_adpcm', 2: 'ima_adpcm', 3: 'pcm_le', 4: 'wma', 5: 'opus'}
CHANNELS = {0: 1, 1: 2, 3: 4}

def mcc_dat_dir(explicit=None):
    """The folder with sounds_*.dat: `explicit` (--mcc-maps) when it holds them, else the MCC maps folder in use / the installed game's."""
    return _paths.mcc_dat_dir(explicit, need=('sounds_neutral.dat',))


def __getattr__(name):                  # the former module constant, for the developer tools that still read it
    if name == 'MCC_DAT_DIR':
        return mcc_dat_dir()
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


class Gestalt:
    def __init__(self, mp, tag=None):
        self.mp = mp
        self.m = mp.m
        self.vista = mp.kind == 'vista'
        if tag is None:
            cands = [t for t in mp.tags_of('ugh!') if t['foff']]
            tag = cands[0]
        self.tag = tag
        base = tag['foff']
        order = VISTA_ORDER if self.vista else MCC_ORDER
        self.blocks = {}
        for i, name in enumerate(order):
            cnt, addr = struct.unpack_from('<iI', self.m, base + i * 8)
            self.blocks[name] = (cnt, mp.a2f(addr) if cnt > 0 else None)
        if not self.vista:
            cnt, off = self.blocks['codecs']
            self.codecs = [tuple(self.m[off + i * 3: off + i * 3 + 3]) for i in range(cnt)]
        else:
            self.codecs = None

    def count(self, name):
        return self.blocks[name][0]

    # ---- element accessors -------------------------------------------------------------
    def pitch_range(self, i):
        cnt, off = self.blocks['pitch_ranges']
        imp, par, enc, rtf, first, n = struct.unpack_from('<hhhhhh', self.m, off + i * 12)
        return dict(import_name=imp, params=par, encoded_data=enc, runtime_flag=rtf, first_perm=first, perm_count=n)

    def permutation(self, i):
        cnt, off = self.blocks['permutations']
        o = off + i * 16
        imp, skip, gain, pinfo, lnt = struct.unpack_from('<HHbbH', self.m, o)
        d = dict(import_name=imp, skip=skip, gain=gain, info=pinfo, lang_neutral_time=lnt)
        if self.vista:
            ssz, fci, ccnt = struct.unpack_from('<IHH', self.m, o + 8)
            d['sample_size'] = ssz; d['first_chunk'] = fci; d['chunk_count'] = ccnt
        else:
            lc, la = struct.unpack_from('<iI', self.m, o + 8)
            langs = []
            if lc > 0:
                lo = self.mp.a2f(la)
                for L in range(lc):
                    langs.append(struct.unpack_from('<IHH', self.m, lo + L * 8))   # (sample_size, first_chunk, chunk_count)
            d['langs'] = langs
        return d

    def chunk(self, i, lang=0):
        """Vista: (file_offset, size, flags, runtime). MCC: (file_offset, size, flags_hi3, pcm_bytes, runtime)."""
        cnt, off = self.blocks['chunks']
        if self.vista:
            fo, size, fl, rt = struct.unpack_from('<IHHi', self.m, off + i * 12)
            return fo, size, fl, rt
        gc, ga = struct.unpack_from('<iI', self.m, off + lang * 8)
        go = self.mp.a2f(ga)
        fo, X, un, rt = struct.unpack_from('<IIIi', self.m, go + i * 16)
        return fo, X & 0x1FFFFFFF, X >> 29, un, rt

    def sound_tag(self, tag):
        """Parse a snd! tag instance (dict from H2Map.tags()) into a dict."""
        m = self.m; d = tag['foff']
        flags, cls = struct.unpack_from('<HB', m, d)
        pb, pr = struct.unpack_from('<hh', m, d + 6)
        prc, scale, promo, custom = m[d + 0xA], m[d + 0xB], m[d + 0xC], m[d + 0xD]
        extra, = struct.unpack_from('<h', m, d + 0xE)
        maxplay, = struct.unpack_from('<I', m, d + 0x10)
        s8 = lambda v: v - 256 if v > 127 else v
        out = dict(flags=flags, cls=cls, playback=pb, pitch_range=pr, pitch_range_count=prc, scale=s8(scale),
                   promotion=s8(promo), custom_playback=s8(custom), extra_info=extra, max_play_time=maxplay)
        if self.vista:
            out.update(rate=m[d + 3], encoding=m[d + 4], compression=m[d + 5])
        else:
            ci = m[d + 3]
            out['codec_index'] = ci
            out['rate'], out['encoding'], out['compression'] = self.codecs[ci]
            out['remastered_index'], = struct.unpack_from('<h', m, d + 4)
            out['inner_silence'], = struct.unpack_from('<f', m, d + 0x14)
            out['reflection_first'], = struct.unpack_from('<h', m, d + 0x18)
            out['reflection_count'] = m[d + 0x1A]
            out['lowpass_index'] = m[d + 0x1B]
        return out

    def iter_perms(self, snd):
        """Yield (pitch_range_idx_global, pitch_range_dict, perm_idx_global, perm_dict) for a parsed snd."""
        if snd['pitch_range'] < 0:
            return
        for k in range(snd['pitch_range_count']):
            pri = snd['pitch_range'] + k
            pr = self.pitch_range(pri)
            for j in range(pr['perm_count']):
                pi = pr['first_perm'] + j
                yield pri, pr, pi, self.permutation(pi)


class DatPool:
    """Memory-mapped MCC external sound .dat files."""
    def __init__(self, folder=None):
        self.folder = folder or mcc_dat_dir()
        self.maps = {}

    def get(self, name):
        if name not in self.maps:
            f = open(os.path.join(self.folder, 'sounds_%s.dat' % name), 'rb')
            self.maps[name] = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        return self.maps[name]


def mcc_perm_bytes(g, perm, lang=0, dat=None, snd_name=''):
    """Encoded bytes of an MCC permutation for the given language slot.
    Self-contained maps (custom H2EK build): chunk offsets are absolute offsets in the map file.
    Stock maps: offsets are into sounds_neutral.dat (legacy 'sound\\') / sounds_remastered.dat ('sound_remastered\\')."""
    langs = perm['langs']
    if lang >= len(langs):
        return b''
    ssz, first, cnt = langs[lang]
    parts = []
    for c in range(first, first + cnt):
        fo, size, fl, un, rt = g.chunk(c, lang)
        if dat is None:
            parts.append(bytes(g.m[fo:fo + size]))
        else:
            dn = 'remastered' if snd_name.startswith('sound_remastered') else 'neutral'
            parts.append(bytes(dat.get(dn)[fo:fo + size]))
    return b''.join(parts)


def vista_perm_bytes(g, perm, shared_maps=None):
    """Encoded bytes of a Vista permutation. Chunk file_offset top 2 bits select the file:
    0 = this map, 1 = mainmenu.map, 2 = shared.map, 3 = single_player_shared.map; low 30 bits = file offset."""
    parts = []
    for c in range(perm['first_chunk'], perm['first_chunk'] + perm['chunk_count']):
        fo, size, fl, rt = g.chunk(c)
        loc = fo >> 30
        off = fo & 0x3FFFFFFF
        if loc == 0:
            src = g.m
        else:
            src = (shared_maps or {})[loc].m
        parts.append(bytes(src[off:off + size]))
    return b''.join(parts)


# ------------------------------------------------------------------------------------------------
# External data (stock MCC maps): which sounds_*.dat holds a chunk?
# ------------------------------------------------------------------------------------------------
DAT_BY_SLOT = {0: 'en', 2: 'de', 3: 'fr', 4: 'sp', 5: 'it'}      # language slot -> dat (slots 1,6,7,8 have no dat)


def _strict_opus(b, un, ch):
    o = 0
    n = 0
    while o + 2 <= len(b):
        L = b[o] | (b[o + 1] << 8)
        neg = L >= 0x8000
        if neg:
            L = 0x10000 - L
        if L == 0 or o + 2 + L > len(b) or b[o + 2] not in (0xE8, 0xEC, 0xF8, 0xFC):
            return False
        o += 2 + L
        n += 1
        if neg:
            break
    else:
        return False
    return n * 240 * 2 * ch == un and len(b) - o < 16


def _strict_xadpcm(b, un, ch):
    import numpy as np
    bs = 36 * ch
    if len(b) % bs:
        return False
    nb = len(b) // bs
    if nb * 64 * 2 * ch != un:
        return False
    a = np.frombuffer(b, dtype=np.uint8).reshape(nb, bs)
    for c in range(ch):
        if (a[:, 4 * c + 2] > 88).any() or (a[:, 4 * c + 3] != 0).any():
            return False
    return True


def mcc_pick_dat(name, snd_flags, lang_neutral_time, slot):
    """Which external dat holds the chunks of (sound, language slot)?  Rule (verified on all 8509
    ADPCM/Opus chunks of stock shared.map; re-verified here on single_player_shared.map with strict framing checks):
        permutation.language_neutral_time != 0  -> sounds_<lang>.dat, lang from the slot: 0 en, 2 de, 3 fr, 4 sp, 5 it
        elif snd!.flags & 0x800 / name starts 'sound_remastered\' -> sounds_remastered.dat
        else                                    -> sounds_neutral.dat"""
    if lang_neutral_time and slot in DAT_BY_SLOT:
        return DAT_BY_SLOT[slot]
    if (snd_flags & 0x800) or name.startswith('sound_remastered'):
        return 'remastered'
    return 'neutral'


def mcc_find_dat(dats, fo, size, un, comp, ch, name, slot, lang_neutral_time=0, snd_flags=0):
    """Return the chunk bytes from the right external dat (rule above); verified with a strict framing check, with the
    other dats as a fallback."""
    first = mcc_pick_dat(name, snd_flags, lang_neutral_time, slot)
    order = [first] + [d for d in ('neutral', 'en', 'de', 'fr', 'it', 'sp', 'remastered') if d != first]
    chk = {1: _strict_xadpcm, 5: _strict_opus}.get(comp)
    for dn in order:
        mm = dats.get(dn)
        if fo + size > len(mm):
            continue
        b = bytes(mm[fo:fo + size])
        if chk is None:
            return b
        if chk(b, un, ch):
            return b
    raise ValueError('chunk @%#x size %d not found in any dat for %s' % (fo, size, name))
