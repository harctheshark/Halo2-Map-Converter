# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Constants and pure rules of the Vista -> MCC sound conversion  (REVERSE direction).

Everything here is the inverse of a forward rule or a rule measured on the MCC
oracles.  No file access, no ctx.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass
import struct

NULL = 0xFFFFFFFF

# ---- MCC ugh! root (0x78 bytes, 15 tag blocks) and element sizes -----------------------------------------------------------
MCC_UGH = dict(codecs=0x00, playbacks=0x08, scales=0x10, import_names=0x18, pitch_range_params=0x20,
               pitch_ranges=0x28, permutations=0x30, custom_playbacks=0x38, reflections=0x40, lowpass=0x48,
               remastered=0x50, runtime_perm_flags=0x58, chunks=0x60, promotions=0x68, extra_info=0x70)
MCC_UGH_SIZE = 0x78
MCC_EXTRA_SIZE = 0x14C
VISTA_EXTRA_SIZE = 0x2C
MCC_SND_SIZE = 0x1C
VISTA_SND_SIZE = 0x14
LANG_SLOTS = 9                      # ugh!.chunks has 9 language slots; Permutation.Localized Chunks Info has 9 entries

# ---- audio kinds ----------------------------------------------------------------------------------------------------------
# Vista compression: 0 PCM big endian, 1 Xbox ADPCM, 2 IMA ADPCM, 3 PCM little endian, 4 WMA.  Vista encoding: 0 mono, 1 stereo, 2 codec.
# MCC codec triple (rate, encoding, compression): rate 0 22.05k 1 44.1k 2 32k 3 48k; encoding adds 3 = quad; compression adds 5 = Opus.
CHANNELS = {0: 1, 1: 2}
XADPCM_BLOCK = 36                   # bytes per channel per block
XADPCM_SAMPLES = 64                 # samples per channel per block

# ---- MCC chunking rule (measured) ---------------------------------------------------------------
# XADPCM permutations are cut into chunks of 262,080 bytes (= 7280 mono / 3640 stereo blocks = 8 x the Vista 32,760) while MORE than
# two chunks' worth remains; the last chunk takes the remainder (so it is 1..524,160 bytes).  Verified on the H2EK maps
# (custom / chronopolis / triplicate) and stock MCC headlong/shared/single_player_shared/mainmenu: every non-last chunk is 262,080,
# the largest single chunk is 473,616 (mono) / 442,368 (stereo), the smallest multi-chunk permutation is 552,384 = 262,080 + 290,304.
MCC_CHUNK = 262080
MCC_CHUNK_MAX_LAST = 2 * MCC_CHUNK
# size_flags dword of an MCC chunk record: bits 0..28 size, bit 29 = last chunk of the permutation, bit 31 = F31 (copied from Vista)
MCC_LAST_BIT = 0x20000000
MCC_F31_BIT = 0x80000000
MCC_SIZE_MASK = 0x1FFFFFFF
VISTA_F31_BIT = 0x80000000


def mcc_split_chunks(nbytes, kind='xadpcm'):
    """MCC chunk sizes of one permutation payload of `nbytes` encoded bytes.  WMA ('codec' blobs) are ONE chunk."""
    if kind == 'wma' or nbytes <= MCC_CHUNK_MAX_LAST:
        return [nbytes]
    sizes = []
    rem = nbytes
    while rem > MCC_CHUNK_MAX_LAST:
        sizes.append(MCC_CHUNK)
        rem -= MCC_CHUNK
    sizes.append(rem)
    return sizes


def pcm_bytes_of(size, kind, channels):
    """MCC chunk 'uncompressed size' field (= decoded 16-bit PCM bytes of the chunk; WMA and PCM: the stored size)."""
    if kind == 'xadpcm':
        return size // (XADPCM_BLOCK * channels) * XADPCM_SAMPLES * 2 * channels
    return size


def f31_parts(vista_chunk_sizes, vista_f31, part_sizes):
    """Which MCC parts carry F31.  A Vista chunk with F31 set contributes its END offset; the MCC part that contains the last byte
    of that Vista chunk gets the flag (a flag on the last Vista chunk therefore lands on the last MCC chunk)."""
    ends = []
    pos = 0
    for sz, f in zip(vista_chunk_sizes, vista_f31):
        pos += sz
        if f and sz:
            ends.append(pos)
    out = [False] * len(part_sizes)
    if not ends:
        return out
    bounds = []
    pos = 0
    for sz in part_sizes:
        bounds.append((pos, pos + sz))
        pos += sz
    for e in ends:
        for i, (s, t) in enumerate(bounds):
            if s < e <= t:
                out[i] = True
                break
        else:
            out[-1] = True
    return out


# ---- snd! flags ----------------------------------------------------------------------------------------------------------------
def vista_flags_to_mcc(f):
    """Inverse of sound_bundle.mcc_flags_to_vista: Vista bits 0..9 stay, Vista bits 10..13 become MCC bits 12..15 (MCC inserted bit 10
    'Play Only In Legacy Mode' and bit 11 'Play Only In Remastered Mode').  Vista bits 14/15 do not exist in MCC and are dropped."""
    return (f & 0x3FF) | ((f & 0x3C00) << 2)


def mcc_snd_body(flags, cls, codec_index, remastered_index, playback, pitch_range, pr_count, scale, promotion, custom,
                 extra_info, max_play, inner_silence=0.0, refl_first=-1, refl_count=0, lowpass=0xFF):
    """The 28-byte MCC snd! tag body.  Classic (non-remastered) sounds of every stock/H2EK map carry the tail
    (inner silence 0.0, first reflection -1, reflection count 0, low-pass index 0xFF) in 7,000+ of 7,000+ sounds checked."""
    return struct.pack('<HBBhhhBBBBhI', flags & 0xFFFF, cls, codec_index & 0xFF, remastered_index,
                       playback, pitch_range, pr_count, scale & 0xFF, promotion & 0xFF, custom & 0xFF, extra_info,
                       max_play) + struct.pack('<fhBB', inner_silence, refl_first, refl_count, lowpass)


def parse_mcc_snd_body(b):
    """28-byte MCC snd! body -> dict (codec_index not resolved)."""
    flags, cls, ci, rem, pb, pr = struct.unpack_from('<HBBhhh', b, 0)
    pc, sc, pm, cu = b[0xA], b[0xB], b[0xC], b[0xD]
    s8 = lambda v: v - 256 if v > 127 else v
    ex, mx = struct.unpack_from('<hI', b, 0xE)
    inner, rf, rc, lp = struct.unpack_from('<fhBB', b, 0x14)
    return dict(flags=flags, cls=cls, codec_index=ci, remastered_index=rem, playback=pb, pitch_range=pr, pitch_range_count=pc,
                scale=s8(sc), promotion=s8(pm), custom_playback=s8(cu), extra_info=ex, max_play=mx, inner_silence=inner,
                refl_first=rf, refl_count=rc, lowpass=lp)
