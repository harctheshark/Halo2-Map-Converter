# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""SOUND codec layer for the H2 MCC -> Vista converter.

Pure numpy + ctypes (libopus). No subprocesses, no windows.

What is here
------------
* XADPCM (Xbox ADPCM / Xbox IMA ADPCM, 36 bytes per channel per 64 samples)   decode + encode
* MCC Opus stream framing ([u16 LE length][Opus packet]...) decode via libopus (opus.dll) through ctypes
* Resampling (48 kHz -> 44.1 kHz) with scipy.signal.resample_poly (falls back to numpy windowed-sinc)
* Vista chunking rule (32760 byte chunks, last chunk takes the remainder up to 65520, 512 byte placement)
* WAV helper for debugging

"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass
import os
import struct
import wave
import numpy as np

# --------------------------------------------------------------------------------------------
# IMA tables
# --------------------------------------------------------------------------------------------
IMA_STEP = np.array([
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97,
    107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428,
    4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350,
    22385, 24623, 27086, 29794, 32767], dtype=np.int32)
IMA_INDEX = np.array([-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8], dtype=np.int32)

XADPCM_BLOCK_BYTES = 36      # per channel
XADPCM_BLOCK_SAMPLES = 64    # per channel per block


# --------------------------------------------------------------------------------------------
# Xbox ADPCM decode
# --------------------------------------------------------------------------------------------
def xadpcm_decode(data, channels):
    """Decode Xbox ADPCM bytes -> int16 array shape (nsamples, channels).

    Layout per block (36*channels bytes) -- VERIFIED against stock Vista data:
      channels * [int16 LE first-sample, uint8 step_index, uint8 reserved(0)]   (all headers first)
      then 8 groups; each group = for each channel 4 bytes (8 nibbles, LOW nibble first)
    A block yields 64 samples per channel:  out[0] = header sample (exact),
    out[1..63] = IMA-decoded from nibbles 0..62 (state = header sample, header step index).
    Nibble 63 (high nibble of the channel's last byte) is padding (always 0 in stock data).
    """
    data = bytes(data)
    bs = XADPCM_BLOCK_BYTES * channels
    nblocks = len(data) // bs
    if nblocks == 0:
        return np.zeros((0, channels), dtype=np.int16)
    a = np.frombuffer(data, dtype=np.uint8, count=nblocks * bs).reshape(nblocks, bs)
    out = np.zeros((nblocks, XADPCM_BLOCK_SAMPLES, channels), dtype=np.int16)
    for ch in range(channels):
        pred = (a[:, 4 * ch].astype(np.int32) | (a[:, 4 * ch + 1].astype(np.int32) << 8))
        pred = np.where(pred >= 32768, pred - 65536, pred).astype(np.int32)
        idx = np.clip(a[:, 4 * ch + 2].astype(np.int32), 0, 88)
        body = a[:, 4 * channels:].reshape(nblocks, 8, channels, 4)[:, :, ch, :].reshape(nblocks, 32)
        nib = np.empty((nblocks, 64), dtype=np.int32)
        nib[:, 0::2] = body & 0x0F
        nib[:, 1::2] = body >> 4
        out[:, 0, ch] = pred
        for k in range(63):
            n = nib[:, k]
            step = IMA_STEP[idx]
            diff = step >> 3
            diff = diff + np.where(n & 1, step >> 2, 0) + np.where(n & 2, step >> 1, 0) + np.where(n & 4, step, 0)
            diff = np.where(n & 8, -diff, diff)
            pred = np.clip(pred + diff, -32768, 32767)
            idx = np.clip(idx + IMA_INDEX[n], 0, 88)
            out[:, k + 1, ch] = pred
    return out.reshape(nblocks * XADPCM_BLOCK_SAMPLES, channels)


# --------------------------------------------------------------------------------------------
# Xbox ADPCM encode (block-independent, vectorised across blocks)
# --------------------------------------------------------------------------------------------
def _enc_pass(x, idx0):
    """Encode all blocks of one channel in parallel.
    x: (nblocks, 64) int32 ; the header sample is x[:,0]; nibbles 0..62 encode x[:,1..63].
    idx0: (nblocks,) start step index.  Returns nibbles (nblocks,64) (nibble 63 = 0) and squared error (nblocks,)."""
    nb = x.shape[0]
    pred = x[:, 0].copy()
    idx = idx0.copy()
    nib = np.zeros((nb, 64), dtype=np.int32)
    err = np.zeros(nb, dtype=np.float64)
    for k in range(63):
        step = IMA_STEP[idx]
        d = x[:, k + 1] - pred
        sign = (d < 0)
        ad = np.abs(d)
        n = np.zeros(nb, dtype=np.int32)
        diff = step >> 3
        c = ad >= step
        n = n | np.where(c, 4, 0)
        ad = np.where(c, ad - step, ad)
        diff = diff + np.where(c, step, 0)
        s2 = step >> 1
        c = ad >= s2
        n = n | np.where(c, 2, 0)
        ad = np.where(c, ad - s2, ad)
        diff = diff + np.where(c, s2, 0)
        s4 = step >> 2
        c = ad >= s4
        n = n | np.where(c, 1, 0)
        diff = diff + np.where(c, s4, 0)
        n = n | np.where(sign, 8, 0)
        diff = np.where(sign, -diff, diff)
        pred = np.clip(pred + diff, -32768, 32767)
        idx = np.clip(idx + IMA_INDEX[n], 0, 88)
        nib[:, k] = n
        e = (x[:, k + 1] - pred).astype(np.float64)
        err += e * e
    return nib, err


def xadpcm_encode(pcm, channels, search=True):
    """Encode int16 PCM (nsamples, channels) (or 1-D for mono) -> Xbox ADPCM bytes.
    Pads the tail with the last sample to a multiple of 64 samples.
    Block-independent: header = exact first sample of the block + a step index chosen by exhaustive search
    over a few candidates around an estimate; nibbles 0..62 encode samples 1..63, nibble 63 is 0
    (this is exactly the stock Vista block layout)."""
    pcm = np.asarray(pcm)
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    assert pcm.shape[1] == channels
    n = pcm.shape[0]
    nblocks = max(1, -(-n // XADPCM_BLOCK_SAMPLES))
    padded = np.empty((nblocks * XADPCM_BLOCK_SAMPLES, channels), dtype=np.int32)
    padded[:n] = pcm
    if n:
        padded[n:] = pcm[-1]
    else:
        padded[:] = 0
    bs = XADPCM_BLOCK_BYTES * channels
    out = np.zeros((nblocks, bs), dtype=np.uint8)
    for ch in range(channels):
        x = padded[:, ch].reshape(nblocks, 64)
        dd = np.abs(np.diff(x, axis=1)).mean(axis=1)
        target = np.maximum(dd * 1.2, 7)
        idx_est = np.clip(np.searchsorted(IMA_STEP, target).astype(np.int32), 0, 88)
        if search:
            best_err = None
            for o in (-6, -3, -1, 0, 1, 3, 6, 10):
                c = np.clip(idx_est + o, 0, 88)
                nibc, e = _enc_pass(x, c)
                if best_err is None:
                    best_err, best_nib, best_idx = e, nibc, c.copy()
                else:
                    m = e < best_err
                    best_err = np.where(m, e, best_err)
                    best_nib = np.where(m[:, None], nibc, best_nib)
                    best_idx = np.where(m, c, best_idx)
            nib, idxs = best_nib, best_idx
        else:
            nib, _ = _enc_pass(x, idx_est)
            idxs = idx_est
        p16 = x[:, 0] & 0xFFFF
        out[:, 4 * ch] = p16 & 0xFF
        out[:, 4 * ch + 1] = p16 >> 8
        out[:, 4 * ch + 2] = idxs.astype(np.uint8)
        out[:, 4 * ch + 3] = 0
        packed = (nib[:, 0::2] | (nib[:, 1::2] << 4)).astype(np.uint8)       # (nblocks, 32)
        body = out[:, 4 * channels:].reshape(nblocks, 8, channels, 4)
        body[:, :, ch, :] = packed.reshape(nblocks, 8, 4)
    return out.tobytes()


# --------------------------------------------------------------------------------------------
# MCC Opus stream (length-prefixed packets) -> PCM via libopus
# --------------------------------------------------------------------------------------------
ENV_OPUS = 'H2CONV_OPUS'          # the full path of an opus.dll to use instead of the bundled one
OPUS_DLL_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'third_party', 'opus.dll'),
    r'C:\Program Files\Audacity\opus.dll',
    r'C:\Program Files\Audacity 4\bin\opus.dll',
]
_opus = None


def _load_opus():
    global _opus
    if _opus is not None:
        return _opus
    last = None
    for p in ([os.environ[ENV_OPUS]] if os.environ.get(ENV_OPUS) else []) + OPUS_DLL_CANDIDATES:
        if os.path.exists(p):
            try:
                d = ctypes.CDLL(p)
                d.opus_decoder_create.restype = ctypes.c_void_p
                d.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
                d.opus_decode.restype = ctypes.c_int
                d.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
                d.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
                _opus = d
                return d
            except OSError as e:
                last = e
    raise RuntimeError('no usable opus.dll (last error %r)' % (last,))


def iter_opus_packets(data):
    """Yield the Opus packets of an MCC chunk: repeated [int16 LE length][packet].
    A NEGATIVE length (e.g. 0xFFD7 = -41) marks the FINAL packet of the stream (|length| bytes follow, then stop).
    Chunk data may be followed by padding to a multiple of 8 bytes; it is ignored."""
    o = 0
    n = len(data)
    while o + 2 <= n:
        L = data[o] | (data[o + 1] << 8)
        o += 2
        last = False
        if L >= 0x8000:
            L = 0x10000 - L
            last = True
        if L == 0 or o + L > n:
            break
        yield bytes(data[o:o + L])
        o += L
        if last:
            break


OPUS_PRESKIP_48K = 312       # libopus default encoder look-ahead (measured: MCC Opus lags the Vista original by 288 samples @44.1k = 312 @48k)


def opus_decode_stream(chunks, channels, rate=48000, preskip=0):
    """Decode a whole MCC Opus permutation -> int16 (n, channels) at `rate` (48000).

    `chunks`: a list of chunk byte strings (each chunk is a run of [int16 len][packet] ending in a NEGATIVE-length
    final packet) or a single bytes object. ONE decoder instance is used across all chunks of the permutation
    (verified: continuing the decoder across chunks correlates 0.99 with the Vista original, resetting per chunk 0.9).
    `preskip` samples are dropped from the start (use OPUS_PRESKIP_48K for converting)."""
    if isinstance(chunks, (bytes, bytearray, memoryview)):
        chunks = [chunks]
    d = _load_opus()
    err = ctypes.c_int(0)
    dec = d.opus_decoder_create(rate, channels, ctypes.byref(err))
    if err.value != 0 or not dec:
        raise RuntimeError('opus_decoder_create failed %d' % err.value)
    try:
        buf = (ctypes.c_int16 * (5760 * channels))()
        parts = []
        for chunk in chunks:
            for pk in iter_opus_packets(chunk):
                n = d.opus_decode(dec, pk, len(pk), buf, 5760, 0)
                if n < 0:
                    raise RuntimeError('opus_decode error %d' % n)
                parts.append(np.frombuffer(buf, dtype=np.int16, count=n * channels).reshape(n, channels).copy())
        if not parts:
            return np.zeros((0, channels), dtype=np.int16)
        pcm = np.concatenate(parts, axis=0)
        return pcm[preskip:] if preskip else pcm
    finally:
        d.opus_decoder_destroy(dec)


# --------------------------------------------------------------------------------------------
# Resampling
# --------------------------------------------------------------------------------------------
def resample(pcm, src_rate, dst_rate):
    """pcm int16 (n, ch) -> int16 (m, ch) with a Kaiser polyphase filter."""
    if src_rate == dst_rate:
        return pcm
    from math import gcd
    from scipy.signal import resample_poly
    g = gcd(src_rate, dst_rate)
    up, down = dst_rate // g, src_rate // g
    y = resample_poly(pcm.astype(np.float64), up, down, axis=0, window=('kaiser', 8.6))
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16)


# --------------------------------------------------------------------------------------------
# Vista chunking
# --------------------------------------------------------------------------------------------
VISTA_CHUNK = 32760          # 910 XADPCM blocks mono / 455 stereo blocks
VISTA_CHUNK_MAX_LAST = 65520  # 2 * 32760
VISTA_PLACE_ALIGN = 512      # CACHE_FILE_PAGE_SIZE


def vista_split_chunks(nbytes):
    """Chunk sizes for a permutation of nbytes encoded bytes (matches the stock Vista tool output)."""
    sizes = []
    rem = nbytes
    while rem > VISTA_CHUNK_MAX_LAST:
        sizes.append(VISTA_CHUNK)
        rem -= VISTA_CHUNK
    sizes.append(rem)
    return sizes


def write_wav(path, pcm, rate):
    pcm = np.asarray(pcm)
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    w = wave.open(path, 'wb')
    w.setnchannels(pcm.shape[1])
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(pcm.astype('<i2').tobytes())
    w.close()


# --------------------------------------------------------------------------------------------
# High level: MCC permutation -> Vista permutation payload
# --------------------------------------------------------------------------------------------
def opus_perm_to_vista_xadpcm(chunks, channels, dst_rate=44100):
    """MCC Opus permutation (list of chunk byte strings) -> (xadpcm_bytes, n_samples_per_channel_after_pad).

    Pipeline (validated against stock Vista originals):
      libopus decode 48 kHz (one decoder across chunks) -> drop 312 samples pre-skip -> polyphase resample
      48000 -> dst_rate -> Xbox ADPCM block encode (64 samples / 36 B / channel; tail padded with last sample)."""
    pcm = opus_decode_stream(chunks, channels, 48000, preskip=OPUS_PRESKIP_48K)
    pcm = resample(pcm, 48000, dst_rate)
    data = xadpcm_encode(pcm, channels)
    return data, (len(data) // (XADPCM_BLOCK_BYTES * channels)) * XADPCM_BLOCK_SAMPLES


def pcm_le_to_vista_xadpcm(raw, channels, src_rate, dst_rate=44100):
    """MCC 'None (Little Endian)' 16-bit PCM bytes -> Xbox ADPCM bytes (channels 1 or 2; quad is not representable)."""
    pcm = np.frombuffer(raw, dtype='<i2')
    pcm = pcm[: (len(pcm) // channels) * channels].reshape(-1, channels)
    pcm = resample(pcm, src_rate, dst_rate)
    return xadpcm_encode(pcm, channels)


def vista_chunk_records(nbytes, base_offset, last_flag=False, location=0):
    """Chunk table rows for ONE permutation payload placed at base_offset (512-aligned).
    Returns (rows, end_offset) with rows = [(file_offset_with_location_bits, size_and_flags_u32, runtime=-1), ...]
    size_and_flags_u32 = size (bits 0-30) | 0x80000000 if `last_flag` and this is the last chunk."""
    rows = []
    off = base_offset
    sizes = vista_split_chunks(nbytes)
    for i, sz in enumerate(sizes):
        flag = 0x80000000 if (last_flag and i == len(sizes) - 1) else 0
        rows.append(((location << 30) | off, sz | flag, -1))
        off += (sz + VISTA_PLACE_ALIGN - 1) // VISTA_PLACE_ALIGN * VISTA_PLACE_ALIGN
    return rows, off


def xadpcm_stream_consistency(data, channels):
    """Self-check used to validate the block layout against real data: in a continuous Xbox ADPCM stream the step
    index stored in block k+1's header equals the decoder's step index after nibble 62 of block k.
    Returns (matching_blocks, total_blocks_checked) over all channels."""
    data = bytes(data)
    bs = XADPCM_BLOCK_BYTES * channels
    nblocks = len(data) // bs
    a = np.frombuffer(data, dtype=np.uint8, count=nblocks * bs).reshape(nblocks, bs)
    ok = tot = 0
    for ch in range(channels):
        pred = (a[:, 4 * ch].astype(np.int32) | (a[:, 4 * ch + 1].astype(np.int32) << 8))
        pred = np.where(pred >= 32768, pred - 65536, pred).astype(np.int32)
        idx = np.clip(a[:, 4 * ch + 2].astype(np.int32), 0, 88)
        hidx = idx.copy()
        body = a[:, 4 * channels:].reshape(nblocks, 8, channels, 4)[:, :, ch, :].reshape(nblocks, 32)
        nib = np.empty((nblocks, 64), dtype=np.int32)
        nib[:, 0::2] = body & 0x0F
        nib[:, 1::2] = body >> 4
        for k in range(63):
            n = nib[:, k]
            step = IMA_STEP[idx]
            diff = step >> 3
            diff = diff + np.where(n & 1, step >> 2, 0) + np.where(n & 2, step >> 1, 0) + np.where(n & 4, step, 0)
            diff = np.where(n & 8, -diff, diff)
            pred = np.clip(pred + diff, -32768, 32767)
            idx = np.clip(idx + IMA_INDEX[n], 0, 88)
        ok += int((hidx[1:] == idx[:-1]).sum())
        tot += nblocks - 1
    return ok, tot
