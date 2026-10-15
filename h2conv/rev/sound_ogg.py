# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Pluggable Ogg decoding for the REVERSE sound path, Vista (v8) -> MCC (v13).

Some community Vista maps carry snd! permutations whose payload is an Ogg Vorbis file although the tag says "PCM" (3 sounds of `grounded(OLD).map`).  The Vista tool stored the file bytes unchanged and priced the sound as if they were PCM (Maximum Play Time =
bytes / PCM rate, e.g. 118 ms for a 0.73 s clip).  MCC wants every classic (non-Opus, non-WMA) sound as Xbox ADPCM, so such a permutation is
DECODED to PCM here and then encoded exactly like every other PCM source (`h2conv.sound_codec.xadpcm_encode`).

The decoder is a plug-in with a tiny contract so the C# port can replace it:

    class OggDecoder:
        name                      short id
        availability() -> (ok: bool, why: str)
        decode(data: bytes) -> Decoded          # int16 PCM (n, channels) at the stream's NATIVE rate; raises OggDecoderUnavailable / OggDecodeFailed

Default plug-in `FfmpegDecoder`: a console `ffmpeg.exe` run through `subprocess` with CREATE_NO_WINDOW + BELOW_NORMAL priority, stdin = DEVNULL,
the Ogg bytes handed over as a temporary file (`-i file`) and the PCM read back from stdout (`-f f32le`, quantised here: rint(x * 32768) clipped
to int16, the same rounding the ffmpeg float->s16 converter uses).  No window is ever created.

Where ffmpeg is looked for (first hit wins):  opts['ffmpeg_path']  >  environment H2CONV_FFMPEG  >  settings.json "ffmpeg_path"  >  PATH.
The value may be the .exe or a folder holding ffmpeg.exe (or bin\\ffmpeg.exe).  When nothing is found `decode` raises OggDecoderUnavailable, the
caller emits ONE clear warning (`snd_ogg_no_decoder`) and the affected sound falls back to a valid SILENT body (the pre-existing `snd_failed` path).

Other plug-ins:  `register_decoder(name, factory)`; select with opts['ogg_decoder'] (a name, or an object with .decode / .availability) or the
environment variable H2CONV_OGG_DECODER.  The name 'none' disables decoding on purpose (silent fallback).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass
import os
import shutil
import struct
import subprocess
import tempfile

import numpy as np

from .. import paths as _paths

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
ENV_FFMPEG = 'H2CONV_FFMPEG'
ENV_DECODER = 'H2CONV_OGG_DECODER'
FFMPEG_CANDIDATES = []                 # no built-in location: give ffmpeg with H2CONV_FFMPEG / settings.json "ffmpeg_path", or put it on the PATH
DECODE_TIMEOUT_S = 120


class OggError(Exception):
    pass


class OggDecoderUnavailable(OggError):
    """No usable decoder (ffmpeg not found / decoder disabled).  The caller falls back to a silent sound and warns once."""


class OggDecodeFailed(OggError):
    """The decoder ran but could not decode this stream."""


class Decoded(object):
    """PCM result: int16 `pcm` of shape (n, channels) at `rate` Hz (the stream's native rate)."""
    __slots__ = ('pcm', 'rate', 'channels', 'codec')

    def __init__(self, pcm, rate, channels, codec='vorbis'):
        self.pcm = pcm
        self.rate = int(rate)
        self.channels = int(channels)
        self.codec = codec

    @property
    def frames(self):
        return int(self.pcm.shape[0])


# ------------------------------------------------------------------------------------------------
# pure-Python Ogg inspection (no decoder needed): codec, channels, rate, expected sample count
# ------------------------------------------------------------------------------------------------
def inspect_ogg(data):
    """-> dict(codec 'vorbis'|'opus'|'flac'|None, channels, rate, granule, pages) from the identification header and the last page's granule
    position (= the number of output samples of a Vorbis stream).  Raises OggDecodeFailed when `data` is not an Ogg file."""
    data = bytes(data)
    if data[:4] != b'OggS' or len(data) < 28:
        raise OggDecodeFailed('not an Ogg stream')
    info = dict(codec=None, channels=0, rate=0, granule=None, pages=0, streams=0)
    pos = 0
    n = len(data)
    first = True
    while pos + 27 <= n and data[pos:pos + 4] == b'OggS':
        htype = data[pos + 5]
        granule = struct.unpack_from('<q', data, pos + 6)[0]
        nseg = data[pos + 26]
        if pos + 27 + nseg > n:
            break
        body = sum(data[pos + 27:pos + 27 + nseg])
        if first:
            pk = data[pos + 27 + nseg:pos + 27 + nseg + 32]
            if pk[:7] == b'\x01vorbis' and len(pk) >= 16:
                info.update(codec='vorbis', channels=pk[11], rate=struct.unpack_from('<I', pk, 12)[0])
            elif pk[:8] == b'OpusHead' and len(pk) >= 12:
                info.update(codec='opus', channels=pk[9], rate=48000)
            elif pk[:5] == b'\x7fFLAC' and len(pk) >= 32:
                v = struct.unpack_from('>I', pk, 17 + 10)[0]               # Ogg FLAC mapping: 0x7F 'FLAC' ver(2) hdrs(2) 'fLaC' blockhdr(4) STREAMINFO
                info.update(codec='flac', channels=((v >> 9) & 7) + 1, rate=v >> 12)
            first = False
        if htype & 0x02:
            info['streams'] += 1
        if granule >= 0:
            info['granule'] = granule
        info['pages'] += 1
        pos += 27 + nseg + body
    return info


# ------------------------------------------------------------------------------------------------
# decoder plug-ins
# ------------------------------------------------------------------------------------------------
class OggDecoder(object):
    """Contract of a decoder plug-in (documentation base class)."""
    name = 'abstract'

    def availability(self):
        return False, 'abstract decoder'

    def decode(self, data):
        raise OggDecoderUnavailable('abstract decoder')


class NullDecoder(OggDecoder):
    """Decoding switched off on purpose: every Ogg sound becomes silent (with a warning)."""
    name = 'none'

    def availability(self):
        return False, 'Ogg decoding disabled (ogg_decoder = none)'

    def decode(self, data):
        raise OggDecoderUnavailable('Ogg decoding disabled (ogg_decoder = none)')


def _resolve_ffmpeg(path):
    """A folder or an .exe -> the ffmpeg.exe path, or None."""
    if not path:
        return None
    path = os.path.expandvars(os.path.expanduser(str(path)))
    if os.path.isfile(path):
        return path
    if os.path.isdir(path):
        for rel in ('ffmpeg.exe', os.path.join('bin', 'ffmpeg.exe'), 'ffmpeg'):
            q = os.path.join(path, rel)
            if os.path.isfile(q):
                return q
    return None


def find_ffmpeg(explicit=None):
    """-> (path or None, how).  explicit > env H2CONV_FFMPEG > settings.json "ffmpeg_path" > PATH."""
    cand, src = _paths.pick('ffmpeg', explicit)
    if cand:
        how = 'opts' if src == 'option' else src
        p = _resolve_ffmpeg(cand)
        if p:
            return p, how
        return None, '%s %r does not exist' % (how, cand)                 # an explicit choice that is wrong must not silently fall through
    for cand in FFMPEG_CANDIDATES:
        p = _resolve_ffmpeg(cand)
        if p:
            return p, 'default'
    p = shutil.which('ffmpeg')
    if p:
        return p, 'PATH'
    return None, 'not found'


class FfmpegDecoder(OggDecoder):
    name = 'ffmpeg'

    def __init__(self, path=None, temp_dir=None, timeout=DECODE_TIMEOUT_S):
        self.path, self.how = find_ffmpeg(path)
        self.temp_dir = temp_dir
        self.timeout = timeout

    def availability(self):
        if not self.path:
            return False, ('ffmpeg.exe was not found (%s); set opts["ffmpeg_path"] or the %s environment variable to ffmpeg.exe or its folder'
                           % (self.how, ENV_FFMPEG))
        return True, 'ffmpeg: %s (%s)' % (self.path, self.how)

    def decode(self, data):
        ok, why = self.availability()
        if not ok:
            raise OggDecoderUnavailable(why)
        info = inspect_ogg(data)
        ch, rate = info['channels'], info['rate']
        if not ch or not rate:
            raise OggDecodeFailed('cannot read channels / rate of the %s stream' % (info['codec'] or 'unknown Ogg'))
        fd, tmp = tempfile.mkstemp(suffix='.ogg', prefix='h2conv_ogg_', dir=self.temp_dir or None)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(bytes(data))
            cmd = [self.path, '-hide_banner', '-nostdin', '-loglevel', 'error', '-i', tmp, '-map', '0:a:0', '-vn', '-f', 'f32le',
                   '-acodec', 'pcm_f32le', 'pipe:1']
            kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.timeout,
                      creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS)
            try:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0                                         # SW_HIDE
                kw['startupinfo'] = si
            except AttributeError:                                         # not Windows
                kw.pop('creationflags')
            try:
                r = subprocess.run(cmd, **kw)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise OggDecodeFailed('ffmpeg failed to run: %r' % (e,))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if r.returncode != 0 or not r.stdout:
            raise OggDecodeFailed('ffmpeg exit %d: %s' % (r.returncode, r.stderr.decode('utf8', 'replace').strip()[:300]))
        raw = r.stdout
        if len(raw) % (4 * ch):
            raw = raw[:len(raw) // (4 * ch) * (4 * ch)]
        x = np.frombuffer(raw, dtype='<f4').reshape(-1, ch)
        g = info.get('granule')
        if info['codec'] == 'vorbis' and g is not None and 0 < g < x.shape[0]:
            # The Ogg spec defines a Vorbis stream's length as the granule position of its last page.  ffmpeg's CLI keeps the tail of the final
            # block when the stream ends with a 1-byte EOS packet (grounded(OLD) 'ready': 16,384 decoded vs granule 16,175), so cut to the granule.
            x = x[:g]
        pcm = np.clip(np.rint(x.astype(np.float64) * 32768.0), -32768, 32767).astype(np.int16)
        return Decoded(pcm, rate, ch, info['codec'] or 'ogg')


_REGISTRY = {'ffmpeg': lambda o: FfmpegDecoder(path=o.get('ffmpeg_path'), temp_dir=o.get('temp_dir')),
             'none': lambda o: NullDecoder()}
_CACHE = {}


def register_decoder(name, factory):
    """Plug in another decoder: `factory(opts: dict)` -> OggDecoder-like object (the conversion's opts).  Select it with opts['ogg_decoder'] = name."""
    _REGISTRY[name] = factory
    _CACHE.clear()


def get_decoder(opts=None):
    """The decoder for a conversion's `opts`: opts['ogg_decoder'] (name or object) > env H2CONV_OGG_DECODER > 'ffmpeg'."""
    o = opts or {}
    d = o.get('ogg_decoder')
    if d is not None and not isinstance(d, str):
        return d
    name = d or os.environ.get(ENV_DECODER) or 'ffmpeg'
    if name not in _REGISTRY:
        raise OggDecoderUnavailable('unknown Ogg decoder %r (known: %s)' % (name, ', '.join(sorted(_REGISTRY))))
    key = (name, str(o.get('ffmpeg_path') or ''), str(o.get('temp_dir') or ''), os.environ.get(ENV_FFMPEG, ''))
    dec = _CACHE.get(key)
    if dec is None:
        dec = _CACHE[key] = _REGISTRY[name](o)
    return dec


# ------------------------------------------------------------------------------------------------
# decoded PCM -> the sound's labelled rate / channel count -> Xbox ADPCM
# ------------------------------------------------------------------------------------------------
def fit_pcm(pcm, src_rate, dst_rate, dst_channels):
    """int16 (n, ch) at src_rate -> int16 (m, dst_channels) at dst_rate (mono<->stereo by averaging / duplicating, polyphase resample)."""
    from h2conv import sound_codec as sc
    ch = pcm.shape[1]
    if ch != dst_channels:
        if dst_channels == 1:
            pcm = np.clip(np.rint(pcm.astype(np.float64).mean(axis=1, keepdims=True)), -32768, 32767).astype(np.int16)
        elif dst_channels == 2 and ch == 1:
            pcm = np.repeat(pcm, 2, axis=1)
        elif dst_channels == 2 and ch > 2:
            pcm = pcm[:, :2]
        else:
            raise OggDecodeFailed('cannot map %d decoded channels to %d' % (ch, dst_channels))
    if src_rate != dst_rate:
        pcm = sc.resample(pcm, src_rate, dst_rate)
    return pcm


class OggResult(object):
    __slots__ = ('payload', 'frames', 'native_frames', 'duration_ms', 'src_rate', 'src_channels', 'codec', 'granule', 'mismatch')


def ogg_to_xadpcm(data, channels, rate_hz, decoder):
    """Decode one Ogg permutation payload and encode it as Xbox ADPCM at the sound's labelled rate / channels.

    -> OggResult(payload bytes, frames (decoded samples per channel at rate_hz, before the 64-sample block padding), duration_ms =
    floor(frames * 1000 / rate_hz) (the Vista tool's Maximum Play Time rule: floor of the longest permutation), ...)."""
    from h2conv import sound_codec as sc
    dec = decoder.decode(data)
    info = inspect_ogg(data)
    pcm = fit_pcm(dec.pcm, dec.rate, rate_hz, channels)
    res = OggResult()
    res.mismatch = (dec.rate != rate_hz or dec.channels != channels)
    res.src_rate, res.src_channels, res.codec, res.granule = dec.rate, dec.channels, dec.codec, info.get('granule')
    res.native_frames = dec.frames                            # samples per channel at the stream's own rate (== the last Ogg granule for Vorbis)
    res.frames = int(pcm.shape[0])
    res.duration_ms = res.frames * 1000 // rate_hz
    res.payload = sc.xadpcm_encode(pcm, channels)
    return res
