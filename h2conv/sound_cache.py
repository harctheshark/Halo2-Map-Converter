# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""On-disk cache of transcoded sound payloads.

Transcoding one MCC Opus permutation to Vista Xbox ADPCM is the only expensive step of the sound conversion (custom
headlong: 3196 permutations, ~2 min).  The result depends ONLY on the MCC bytes (all chunks of the permutation), the
channel count and the converter constants, so it is cached by a content hash:

    <work folder>/sound_cache/<hh>/<key>.bin       payload = the Vista XBOX ADPCM bytes of the whole permutation
    key = <algo>_<md5(chunk bytes, length-prefixed)>_<channels>

`ALGO` names every constant that changes the output (pre-skip, target rate, resampler, encoder): bump it when
sound_codec.py changes in a way that alters the bytes.  A corrupt/short entry is ignored (the length must be a
multiple of 36*channels).  Writes are atomic (temp file + os.replace).  Thread-safe (no shared state but the dir).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass
import hashlib
import os
import struct
import threading

from . import paths as _paths
from . import sound_codec as sc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(_paths.work_dir(), 'sound_cache')

# everything that influences the bytes produced by sc.opus_perm_to_vista_xadpcm / sc.pcm_le_to_vista_xadpcm
ALGO = 'o1p%d_r44100_k86_xa1' % sc.OPUS_PRESKIP_48K


def content_key(chunks, channels, kind='opus', extra=b''):
    """Cache key for a list of MCC chunk byte strings (hash is length-prefixed so chunk boundaries matter)."""
    h = hashlib.md5()
    for c in chunks:
        h.update(struct.pack('<I', len(c)))
        h.update(c)
    h.update(extra)
    return '%s_%s_%s_%d' % (kind, ALGO, h.hexdigest(), channels)


class SoundCache(object):
    def __init__(self, directory=None, enabled=True):
        self.dir = directory or DEFAULT_DIR
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self._lock = threading.Lock()

    def _path(self, key):
        h = key.rsplit('_', 2)[-2]
        return os.path.join(self.dir, h[:2], key + '.bin')

    def get(self, key, channels):
        if not self.enabled:
            return None
        p = self._path(key)
        try:
            with open(p, 'rb') as f:
                data = f.read()
        except OSError:
            data = None
        if data is not None and data and len(data) % (sc.XADPCM_BLOCK_BYTES * channels) == 0:
            with self._lock:
                self.hits += 1
            return data
        with self._lock:
            self.misses += 1
        return None

    def put(self, key, data):
        if not self.enabled:
            return
        p = self._path(key)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = '%s.%d.%d.tmp' % (p, os.getpid(), threading.get_ident())
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, p)
            with self._lock:
                self.stores += 1
        except OSError:
            pass            # a read-only / full disk only costs speed

    def stats(self):
        return {'hits': self.hits, 'misses': self.misses, 'stores': self.stores, 'dir': self.dir,
                'algo': ALGO, 'enabled': self.enabled}


def transcode_opus(chunks, channels, cache=None, dst_rate=44100):
    """MCC Opus permutation (chunk byte strings) -> Vista Xbox ADPCM bytes, through the cache."""
    key = content_key(chunks, channels, 'opus', b'' if dst_rate == 44100 else struct.pack('<I', dst_rate))
    if cache is not None:
        hit = cache.get(key, channels)
        if hit is not None:
            return hit
    data, _ = sc.opus_perm_to_vista_xadpcm(chunks, channels, dst_rate)
    if cache is not None:
        cache.put(key, data)
    return data


def transcode_pcm(raw_le, channels, src_rate, cache=None, dst_rate=44100):
    """MCC 16-bit little-endian PCM bytes (mono/stereo) -> Vista Xbox ADPCM bytes, through the cache."""
    key = content_key([raw_le], channels, 'pcm', struct.pack('<II', src_rate, dst_rate))
    if cache is not None:
        hit = cache.get(key, channels)
        if hit is not None:
            return hit
    data = sc.pcm_le_to_vista_xadpcm(raw_le, channels, src_rate, dst_rate)
    if cache is not None:
        cache.put(key, data)
    return data


# ------------------------------------------------------------------------------------------------
# opt-in process pool (sound_workers = 2..4).  Threads do NOT help: the encoder/resampler are GIL bound (measured 3x slower
# with 4 threads).  Uses the 'spawn' context with pythonw.exe so that no console window can appear on the desktop;
# the calling script must therefore be import-safe (`if __name__ == '__main__':` guard), as for any multiprocessing use.
# ------------------------------------------------------------------------------------------------
def _pool_job(arg):
    key, kind, args, cdir, enabled = arg
    try:
        if kind == 'opus':
            out = sc.opus_perm_to_vista_xadpcm(args[0], args[1], args[2])[0]
        else:
            out = sc.pcm_le_to_vista_xadpcm(args[0], args[1], args[2], args[3])
        SoundCache(cdir, enabled).put(key, out)
        return key, out, None
    except Exception as e:                      # reported to the parent, which turns it into a per-sound failure
        return key, None, repr(e)


def run_pool(workers, jobs, cache_dir, enabled=True):
    """jobs = [(key, kind, args)] -> yields (key, payload, error_text_or_None) as they finish (kind 'opus': (chunks, ch, dst_rate); 'pcm': (raw, ch, src_rate, dst_rate))."""
    import multiprocessing
    import sys
    mpc = multiprocessing.get_context('spawn')
    if not getattr(sys, 'frozen', False):
        # a Python install: pythonw.exe has no console window.  A frozen exe (sys.frozen) has no pythonw.exe beside it: the exe is its own worker
        # (h2conv/__main__.py calls multiprocessing.freeze_support() first), and a windowed build shows no console either
        pyw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
        if os.path.exists(pyw):
            mpc.set_executable(pyw)
    work = [(k, kind, args, cache_dir, enabled) for k, kind, args in jobs]
    with mpc.Pool(max(1, min(int(workers), 4))) as pool:
        for key, out, err in pool.imap_unordered(_pool_job, work, chunksize=1):
            yield key, out, err
