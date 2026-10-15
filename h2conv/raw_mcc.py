# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Raw-resource access for MCC Halo 2 cache files (version 13).

"Raw" = every byte range a tag points at that is NOT inside the tag-data region:
    * bitmap pixel data            (bitm.bitmaps[].lod1 offset/size   -> textures.dat or the map itself)
    * sound sample chunks          (ugh! "Permutation Chunks"          -> sounds_*.dat or the map itself)
    * geometry 'blkh' blocks       (mode/sbsp/ltmp/DECR/PRTM/weat/... -> always inside a map file)
Pure stdlib (mmap, struct, zlib); READ-ONLY on everything.

Quick use:
    from h2conv import raw_mcc as R
    m = R.MccMap(R.STOCK_HEADLONG)                    # stock map: external textures.dat / sounds_*.dat
    for i in m.tags_of('bitm'):
        for el in m.bitmap_elements(i):
            pixels = m.read_bitmap_pixels(el)          # decompressed pixel bytes (identical to Vista's)
    g = m.sound_gestalt()
    data = g.read_permutation(perm_index, snd_flags=0x7002)   # concatenated chunk bytes (Xbox ADPCM / Opus)
    for info in m.geometry_infos():                    # every 'blkh' reference
        blob = m.read_geometry_block(info)

Vista side:  vista_split_pointer(ptr) -> (location, offset);  VistaRaw(map_path).read(ptr, size).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass

import mmap
import os
import struct
import zlib

# --------------------------------------------------------------------------------------------------
# paths / constants
# --------------------------------------------------------------------------------------------------
try:
    from . import paths as _paths
except ImportError:      # run as a script
    import paths as _paths

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
DECOMP_DIR = os.path.join(_paths.work_dir(), 'mcc_decompressed')


def __getattr__(name):
    """The former module constants (folders on one PC), now resolved by h2conv.paths; for the developer tools that still read them."""
    if name == 'STOCK_MAPS_DIR':
        return _paths.mcc_maps_dir()
    if name == 'VISTA_MAPS_DIR':
        return _paths.vista_maps_dir()
    if name == 'STOCK_HEADLONG':
        return os.path.join(_paths.mcc_maps_dir(), _paths.STOCK_BACKUP_DIR_NAME, 'headlong.map')       # uncompressed
    if name == 'CUSTOM_HEADLONG':
        d = _paths.h2ek_maps_dir()
        if not d:
            raise _paths.PathsError('CUSTOM_HEADLONG needs the H2EK maps folder: set H2CONV_H2EK_MAPS or "h2ek_maps_dir" in the settings file')
        return os.path.join(d, 'headlong.map')
    if name == 'VISTA_HEADLONG':
        return os.path.join(_paths.vista_maps_dir(), 'headlong.map')
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


MCC_HEADER_SIZE = 0x380
HDR_FLAG_COMPRESSED = 0x0001      # u16 @0x1C bit0: body is chunk-compressed (see mcc_cache.py)
HDR_FLAG_EXTERNAL_DAT = 0x0008    # u16 @0x1C bit3: bitmap/sound raw lives in textures.dat / sounds_*.dat
                                  # (set in all 48 stock maps, clear in all 4 H2EK-built self-contained maps)

# Halo 2 sound-gestalt language list order (9 lists; MCC ugh! "Permutation Chunks" is 9 x chunk list)
LANGUAGES = ('english', 'japanese', 'german', 'french', 'spanish', 'italian', 'korean', 'chinese', 'portuguese')
# list index -> sounds_<x>.dat.  Verified by chunk validity: 0,2,3,4,5.  1,6,7,8 have no .dat shipped.
LANGUAGE_DAT = {0: 'sounds_en.dat', 2: 'sounds_de.dat', 3: 'sounds_fr.dat', 4: 'sounds_sp.dat', 5: 'sounds_it.dat'}
TEXTURES_DAT = 'textures.dat'
NEUTRAL_DAT = 'sounds_neutral.dat'
REMASTERED_DAT = 'sounds_remastered.dat'

SND_FLAG_PLAY_ONLY_LEGACY = 0x0400       # snd! flags bit 10
SND_FLAG_PLAY_ONLY_REMASTERED = 0x0800   # snd! flags bit 11 (all sound_remastered\* tags)

CHUNK_SIZE_MASK = 0x0FFFFFFF
CHUNK_FLAG_LAST = 0x20000000             # last chunk of a language-neutral permutation (NOT set for localized perms)
CHUNK_FLAG_BIT31 = 0x80000000            # copied unchanged into the Vista last-chunk dword bit31 (same meaning)

OPUS_RATE = 48000
OPUS_FRAME_SAMPLES = 240                 # every MCC Opus packet is CELT-only fullband 5 ms (TOC 0xE8 mono / 0xEC stereo)
XBOX_ADPCM_BLOCK_SAMPLES = 64

# Vista pointer location selector = top 2 bits of any raw offset dword (bitmap lod1, ugh! chunk, blkh offset)
LOC_SELF, LOC_MAINMENU, LOC_SHARED, LOC_SP_SHARED = 0, 1, 2, 3
VISTA_LOCATION_FILES = (None, 'mainmenu.map', 'shared.map', 'single_player_shared.map')


def vista_split_pointer(ptr):
    """Vista raw offset dword -> (location 0..3, file offset)."""
    return (ptr >> 30) & 3, ptr & 0x3FFFFFFF


def vista_make_pointer(location, offset):
    if not 0 <= offset < (1 << 30):
        raise ValueError('offset too large for 30 bits: 0x%X' % offset)
    return (location << 30) | offset


# --------------------------------------------------------------------------------------------------
# tiny helpers
# --------------------------------------------------------------------------------------------------
_U32 = struct.Struct('<I')
_I32 = struct.Struct('<i')
_U16 = struct.Struct('<H')


def u32(b, o):
    return _U32.unpack_from(b, o)[0]


def i32(b, o):
    return _I32.unpack_from(b, o)[0]


def u16(b, o):
    return _U16.unpack_from(b, o)[0]


class RawError(Exception):
    pass


class DatNotFoundError(RawError, FileNotFoundError):
    """textures.dat / sounds_*.dat is not in the MCC maps folder."""


_WARNED_DATS = set()


# --------------------------------------------------------------------------------------------------
# textures.dat / MCC bitmap blobs
# --------------------------------------------------------------------------------------------------
def decode_texture_blob(buf, off, expected_total=None):
    """Decode one MCC bitmap pixel blob located at buf[off:].

    Layout (identical in textures.dat and in self-contained maps):
        u32 chunk_count
        i32 chunk_size[chunk_count]      size > 0: zlib stream (RFC1950) of that many bytes
                                         size < 0: chunk stored RAW, length -size
        chunk data back-to-back
    Each zlib chunk inflates to 0x40000 bytes except the last.  The reference's size field (bitm +0x50) is
    4 + 4*count + sum(|size|) (i.e. the whole blob, no padding).  Returns (pixel_bytes, consumed_bytes).
    """
    count = u32(buf, off)
    if count == 0 or count > 4096:
        raise RawError('bad blob chunk count %d at 0x%X' % (count, off))
    sizes = struct.unpack_from('<%di' % count, buf, off + 4)
    pos = off + 4 + 4 * count
    out = []
    for s in sizes:
        if s < 0:
            out.append(bytes(buf[pos:pos - s]))
            pos += -s
        else:
            out.append(zlib.decompress(bytes(buf[pos:pos + s])))
            pos += s
    consumed = pos - off
    if expected_total is not None and consumed != expected_total:
        raise RawError('blob at 0x%X consumed %d bytes, reference says %d' % (off, consumed, expected_total))
    return b''.join(out), consumed


def encode_vista_bitmap_blob(pixels, level=9):
    """Vista bitmap raw = ONE plain zlib stream (78 DA ...) of the whole pixel buffer, no header."""
    return zlib.compress(pixels, level)


# --------------------------------------------------------------------------------------------------
# sound chunk helpers
# --------------------------------------------------------------------------------------------------
def iter_opus_packets(chunk):
    """Split an MCC Opus chunk into packets.  Framing: repeat { int16 length_LE ; packet[length] }.
    The LAST packet of every chunk has its length stored NEGATED (two's complement, i.e. >= 0x8000 as u16).
    Yields (packet_bytes, is_last).  Raises RawError if the framing does not end exactly at len(chunk)."""
    pos = 0
    n = len(chunk)
    while pos < n:
        if pos + 2 > n:
            raise RawError('truncated opus length at %d' % pos)
        L = u16(chunk, pos)
        last = L >= 0x8000
        if last:
            L = 0x10000 - L
        if L == 0 or pos + 2 + L > n:
            raise RawError('bad opus packet length %d at %d' % (L, pos))
        yield bytes(chunk[pos + 2:pos + 2 + L]), last
        pos += 2 + L
        if last:
            if pos != n:
                raise RawError('opus chunk has %d trailing bytes' % (n - pos))
            return
    raise RawError('opus chunk ended without a negative-length last packet')


def looks_like_xbox_adpcm(buf, stereo):
    """Cheap validity test: every 36-byte (mono) / 72-byte (stereo) block has step-index <= 88 and reserved byte 0
    in each 4-byte channel header (stereo: the two headers are the first 8 bytes of the block)."""
    bs = 72 if stereo else 36
    if len(buf) == 0 or len(buf) % bs:
        return False
    for o in range(0, len(buf), bs):
        if buf[o + 2] > 88 or buf[o + 3] != 0:
            return False
        if stereo and (buf[o + 6] > 88 or buf[o + 7] != 0):
            return False
    return True


def looks_like_opus_chunk(buf):
    try:
        for _ in iter_opus_packets(buf):
            pass
        return True
    except RawError:
        return False


# --------------------------------------------------------------------------------------------------
# external .dat store
# --------------------------------------------------------------------------------------------------
class DatStore(object):
    """Lazy mmaps of textures.dat / sounds_*.dat in the MCC maps folder."""

    def __init__(self, directory=None):
        # None = the MCC maps folder in use (--mcc-maps / H2CONV_MCC_MAPS / settings / your Steam library)
        self.directory = directory or _paths.mcc_dat_dir()
        self._maps = {}
        self._files = {}

    def get(self, name):
        if name not in self._maps:
            path = os.path.join(self.directory, name)
            try:
                f = open(path, 'rb')
            except OSError as ex:
                # loud, once: callers that compare bitmaps / sounds treat a failure to read as "different" and would otherwise go on silently
                if name not in _WARNED_DATS:
                    _WARNED_DATS.add(name)
                    import sys
                    print('WARNING: cannot open %s (%s).  textures.dat / sounds_*.dat must be in the MCC maps folder: pass --mcc-maps <folder> or set '
                          'H2CONV_MCC_MAPS.' % (path, ex), file=sys.stderr)
                raise DatNotFoundError('%s not found in %s (the MCC maps folder): %s' % (name, self.directory, ex))
            self._files[name] = f
            self._maps[name] = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        return self._maps[name]

    def read(self, name, offset, size):
        mm = self.get(name)
        if offset < 0 or offset + size > len(mm):
            raise RawError('%s: range 0x%X+0x%X beyond file size 0x%X' % (name, offset, size, len(mm)))
        return bytes(mm[offset:offset + size])


# --------------------------------------------------------------------------------------------------
# bitmap element (bitm.bitmaps[] element, 0xA8 bytes in MCC)
# --------------------------------------------------------------------------------------------------
BITMAP_ELEMENT_SIZE = 0xA8
BITMAP_BLOCK_OFFSET = 0x44          # bitm tag: tagblock "Bitmaps" at +0x44


class BitmapElement(object):
    """Decoded fields of one MCC bitmap element.  Raw location lives in lod_offset[]/lod_size[] (+0x38/+0x50)."""

    def __init__(self, tag_index, index, struct_off, b):
        self.tag_index = tag_index
        self.index = index
        self.struct_off = struct_off
        self.signature = bytes(b[0:4])
        self.width, self.height = struct.unpack_from('<hh', b, 4)
        self.depth = b[8]
        self.more_flags = b[9]
        self.type = u16(b, 0xA)
        self.format = u16(b, 0xC)
        self.flags = u16(b, 0xE)
        self.reg_x, self.reg_y = struct.unpack_from('<hh', b, 0x10)
        self.mipmap_count = u16(b, 0x14)
        self.lod_adjust = b[0x16]
        self.cache_usage = b[0x17]
        self.pixels_offset = u32(b, 0x18)          # virtual offset inside the tag's pixel buffer (== Vista's)
        self.parent_datum = u32(b, 0x1C)
        # +0x20 tagblock Native Mipmap Info, +0x28 Native Size, +0x2C Tile Mode: always 0 in files seen
        self.lod_offset = struct.unpack_from('<III', b, 0x38)    # LOD1/2/3 raw offset (only LOD1 ever non-zero)
        self.lod_size = struct.unpack_from('<III', b, 0x50)      # LOD1/2/3 blob size incl. 4+4*count header
        self.raw = bytes(b)

    def __repr__(self):
        return '<Bitmap t%d.%d %dx%d fmt%d mips%d off=0x%X size=0x%X>' % (
            self.tag_index, self.index, self.width, self.height, self.format, self.mipmap_count,
            self.lod_offset[0], self.lod_size[0])


# --------------------------------------------------------------------------------------------------
# geometry block infos
# --------------------------------------------------------------------------------------------------
GEOMETRY_INFO_SIZE = 0x24
# (group, [(block_field_offset, element_size), ...] path from tag root, info_offset_in_innermost_element, label)
# Derived from Assembly plugins (identical in Halo2 and Halo2MCC dirs).
GEOMETRY_INFO_SITES = (
    ('mode', ((0x24, 0x5C),), 0x38, 'Sections'),
    ('mode', ((0x74, 0x58),), 0x34, 'PRT Info'),
    ('sbsp', ((0x9C, 0xB0),), 0x28, 'Clusters'),
    ('sbsp', ((0x138, 0xC8),), 0x28, 'Instanced Geometry Definitions'),
    ('sbsp', ((0x214, 0xAC),), 0x10, 'Water Definitions'),
    ('sbsp', ((0x234, 0x30), (0x10, 0x34)), 0x00, 'Decorators/Cache Blocks'),
    ('ltmp', ((0x80, 0x68), (0x20, 0x54)), 0x28, 'Lightmap Groups/Clusters'),
    ('ltmp', ((0x80, 0x68), (0x30, 0x54)), 0x28, 'Lightmap Groups/Poop Definitions'),
    ('ltmp', ((0x80, 0x68), (0x40, 0x38)), 0x0C, 'Lightmap Groups/Geometry Buckets'),
    ('scnr', ((0x360, 0x30), (0x10, 0x34)), 0x00, 'Decorators/Cache Blocks'),
    ('DECR', (), 0x38, '(root)'),
    ('PRTM', (), 0xA0, '(root)'),
    ('dc_s', ((0x00, 0x30), (0x10, 0x34)), 0x00, 'Decorators/Cache Blocks'),
    ('weat', ((0x00, 0x8C),), 0x40, 'Particle System'),
)
NO_GEOMETRY = 0xFFFFFFFF


class GeometryInfo(object):
    """One 0x24-byte geometry_block_info struct inside a tag (engine: geometry_block_info, cache layout):
        +0x00 u32 block_offset  (absolute file offset of the 'blkh' block; top 2 bits = location on Vista/shared)
        +0x04 u32 block_size    (== 8 + section_data_size + resource_data_size + 4(crsr) ... whole blkh..fklb)
        +0x08 u32 section_data_size
        +0x0C u32 resource_data_size
        +0x10 tagblock resources[] (16-byte geometry_block_resource entries, FIXUP list, lives in tag data)
        +0x18 datum owner tag   +0x1C i16 owner section offset   +0x1E flags   +0x20 i32 geometry cache index (-1)
    """

    def __init__(self, tag_index, group, label, struct_off, b):
        self.tag_index = tag_index
        self.group = group
        self.label = label
        self.struct_off = struct_off
        (self.block_offset, self.block_size, self.section_data_size, self.resource_data_size,
         self.resource_count, self.resource_address) = struct.unpack_from('<IIIIiI', b, 0)

    @property
    def present(self):
        return self.block_offset not in (0, NO_GEOMETRY) and self.block_size > 0

    @property
    def location(self):
        return self.block_offset >> 30

    @property
    def offset(self):
        return self.block_offset & 0x3FFFFFFF

    def __repr__(self):
        return '<Geom t%d %s/%s blk=0x%X size=0x%X sds=0x%X rds=0x%X nres=%d>' % (
            self.tag_index, self.group, self.label, self.block_offset, self.block_size,
            self.section_data_size, self.resource_data_size, self.resource_count)


# --------------------------------------------------------------------------------------------------
# sound gestalt
# --------------------------------------------------------------------------------------------------
class SoundChunk(object):
    """One 16-byte MCC chunk record {u32 file_offset; u32 size_flags; u32 uncompressed_size; u32 runtime_index}."""
    __slots__ = ('list_index', 'index', 'file_offset', 'size_flags', 'uncompressed_size', 'runtime_index')

    def __init__(self, list_index, index, rec):
        self.list_index = list_index
        self.index = index
        self.file_offset, self.size_flags, self.uncompressed_size, self.runtime_index = rec

    @property
    def size(self):
        return self.size_flags & CHUNK_SIZE_MASK

    @property
    def is_last_flag(self):
        return bool(self.size_flags & CHUNK_FLAG_LAST)

    @property
    def bit31(self):
        return bool(self.size_flags & CHUNK_FLAG_BIT31)

    def __repr__(self):
        return '<Chunk L%d #%d off=0x%X size=0x%X flags=0x%X us=0x%X>' % (
            self.list_index, self.index, self.file_offset, self.size, self.size_flags >> 28, self.uncompressed_size)


class Permutation(object):
    def __init__(self, index, b, localized):
        self.index = index
        (self.import_name_index, self.encoded_skip_fraction, self.gain, self.permutation_info_index,
         self.language_neutral_time) = struct.unpack_from('<hhbbh', b, 0)
        # localized[lang] = (sample_size, first_chunk_index, chunk_count) ; sample_size = decoded PCM16 byte count
        self.localized = localized

    @property
    def is_localized(self):
        """language_neutral_time != 0  <=>  the chunks live in sounds_<lang>.dat"""
        return self.language_neutral_time != 0


class SoundTagInfo(object):
    """MCC snd! (0x1C bytes) fields relevant to raw-data resolution."""

    def __init__(self, tag_index, b):
        self.tag_index = tag_index
        self.flags = u16(b, 0)
        self.sound_class = b[2]
        self.codec_index = b[3]
        self.remastered_reference_index = u16(b, 4)
        self.playback_parameter_index = u16(b, 6)
        self.pitch_range_index = u16(b, 8)
        self.pitch_range_count = b[0xA]

    @property
    def remastered_only(self):
        return bool(self.flags & SND_FLAG_PLAY_ONLY_REMASTERED)

    @property
    def legacy_only(self):
        return bool(self.flags & SND_FLAG_PLAY_ONLY_LEGACY)


class SoundGestalt(object):
    """View of the MCC sound_cache_file_gestalt ('ugh!') raw-related blocks.  Offsets from Assembly Halo2MCC/ugh!.xml:
        +0x00 Codecs (3 B each: sample_rate, encoding, compression)   +0x28 Pitch Ranges (12 B)
        +0x30 Permutations (16 B)   +0x60 Permutation Chunks = 9 languages x { count, address -> 16-byte chunk records }
    """

    def __init__(self, mapobj, tag_index):
        self.map = mapobj
        self.tag_index = tag_index
        m = mapobj.m
        o = self.o = mapobj.tag_off(tag_index)
        blk = lambda off: (i32(m, o + off), mapobj.a2o(u32(m, o + off + 4)) if u32(m, o + off + 4) else 0)
        self.codec_count, self._codec_o = blk(0x00)
        self.pitch_range_count, self._pr_o = blk(0x28)
        self.permutation_count, self._pm_o = blk(0x30)
        self.list_count, self._lists_o = blk(0x60)
        self.codecs = [tuple(m[self._codec_o + k * 3:self._codec_o + k * 3 + 3]) for k in range(self.codec_count)]
        self._lists = {}

    # codec helpers ------------------------------------------------------------------------------
    def codec_kind(self, codec_index):
        """'adpcm_mono' | 'adpcm_stereo' | 'adpcm_quad' | 'opus_mono' | 'opus_stereo' | 'pcm_le' | 'wma' | other"""
        rate, enc, comp = self.codecs[codec_index]
        names = {0: 'pcm_be', 1: 'adpcm', 2: 'ima_adpcm', 3: 'pcm_le', 4: 'wma', 5: 'opus'}
        chans = {0: 'mono', 1: 'stereo', 2: 'codec', 3: 'quad'}
        return '%s_%s' % (names.get(comp, 'comp%d' % comp), chans.get(enc, 'enc%d' % enc))

    # chunk lists --------------------------------------------------------------------------------
    def chunk_list(self, lang=0):
        """All chunk records of language list `lang`.  Empty lists (custom maps: lists 1..8) fall back to list 0."""
        m = self.map.m
        if lang >= self.list_count:
            lang = 0
        if lang not in self._lists:
            n = i32(m, self._lists_o + lang * 8)
            ptr = u32(m, self._lists_o + lang * 8 + 4)
            if n <= 0 or ptr == 0:
                self._lists[lang] = self.chunk_list(0) if lang != 0 else []
            else:
                lo = self.map.a2o(ptr)
                self._lists[lang] = [SoundChunk(lang, q, struct.unpack_from('<IIII', m, lo + q * 16)) for q in range(n)]
        return self._lists[lang]

    def permutation(self, index):
        m = self.map.m
        b = m[self._pm_o + index * 0x10:self._pm_o + index * 0x10 + 0x10]
        n, ptr = i32(b, 8), u32(b, 12)
        loc = []
        if n > 0 and ptr:
            lo = self.map.a2o(ptr)
            for L in range(n):
                loc.append(struct.unpack_from('<IHH', m, lo + L * 8))
        return Permutation(index, b, loc)

    def pitch_range(self, index):
        """-> dict(first_permutation, permutation_count, ...) for pitch range `index`"""
        m = self.map.m
        b = m[self._pr_o + index * 12:self._pr_o + index * 12 + 12]
        imp, ppi, epd, erp, fp, pc = struct.unpack_from('<hhhhhh', b, 0)
        return dict(import_name_index=imp, parameter_index=ppi, first_permutation=fp, permutation_count=pc)

    @staticmethod
    def language_used(perm, lang=0):
        """List index really used for `lang`: falls back to list 0 when the permutation has no chunks for it
        (custom H2EK maps only fill list 0)."""
        if lang < len(perm.localized) and perm.localized[lang][2]:
            return lang
        return 0

    def permutation_chunks(self, perm, lang=0):
        """Chunk records of permutation `perm` (Permutation or index) for language list `lang`."""
        if isinstance(perm, int):
            perm = self.permutation(perm)
        if not perm.localized:
            return []
        L = self.language_used(perm, lang)
        ss, fc, cc = perm.localized[L]
        return self.chunk_list(L)[fc:fc + cc]

    # where does a chunk live? --------------------------------------------------------------------
    def chunk_store(self, perm, snd_flags, lang=0):
        """Name of the file a chunk of this permutation is read from:  'map' | 'sounds_xx.dat' | 'sounds_neutral.dat'
        | 'sounds_remastered.dat'.  Rule (verified on 40k+ chunks):
            map not external            -> 'map'  (offset is an absolute offset inside the map itself)
            perm.language_neutral_time  -> sounds_<lang>.dat     (localized dialogue, Opus mono)
            snd.flags & 0x800           -> sounds_remastered.dat  (sound_remastered\\* tags)
            otherwise                   -> sounds_neutral.dat      (classic Xbox ADPCM / Opus)
        """
        if not self.map.external:
            return 'map'
        if perm.is_localized:
            L = self.language_used(perm, lang)
            return LANGUAGE_DAT.get(L, LANGUAGE_DAT[0])
        if snd_flags & SND_FLAG_PLAY_ONLY_REMASTERED:
            return REMASTERED_DAT
        return NEUTRAL_DAT

    def read_chunk(self, chunk, store, dats=None):
        if store == 'map':
            return bytes(self.map.m[chunk.file_offset:chunk.file_offset + chunk.size])
        dats = dats or self.map.dats
        return dats.read(store, chunk.file_offset, chunk.size)

    def read_permutation(self, perm, snd_flags=0, lang=0, dats=None):
        """Concatenated raw bytes of all chunks of a permutation (for ADPCM this is exactly the Vista sample data)."""
        if isinstance(perm, int):
            perm = self.permutation(perm)
        store = self.chunk_store(perm, snd_flags, lang)
        return b''.join(self.read_chunk(c, store, dats) for c in self.permutation_chunks(perm, lang))

    def sound_tags(self):
        """Yield SoundTagInfo for every snd! tag whose data is present in this map file."""
        for i in self.map.tags_of('snd!'):
            o = self.map.tag_off(i)
            if 0 <= o < len(self.map.m) - 0x1C:
                yield SoundTagInfo(i, self.map.m[o:o + 0x1C])

    def sound_permutations(self, info):
        """Permutation indices owned by a snd! tag (via its pitch ranges)."""
        out = []
        for r in range(info.pitch_range_index, info.pitch_range_index + info.pitch_range_count):
            pr = self.pitch_range(r)
            out.extend(range(pr['first_permutation'], pr['first_permutation'] + pr['permutation_count']))
        return out


def vista_chunk_plan(total_size, full=0x7FF8):
    """How Vista splits one permutation's sample bytes into chunks (observed on all 120 headlong perms):
    full chunks of 0x7FF8 while at least 2*0x7FF8 bytes remain, then ONE final chunk (0x24..0xFFFF)."""
    sizes = []
    rem = total_size
    while rem >= 2 * full:
        sizes.append(full)
        rem -= full
    sizes.append(rem)
    return sizes


# --------------------------------------------------------------------------------------------------
# the map
# --------------------------------------------------------------------------------------------------
class MccMap(object):
    """Read-only view of an UNCOMPRESSED MCC (v13) map.  For compressed stock maps pass through
    h2conv.mcc_cache.ensure_decompressed() first (done automatically if the flag bit0 is set)."""

    def __init__(self, path, dat_dir=None, dats=None, auto_decompress=True):
        self.path = path
        with open(path, 'rb') as f:
            hdr = f.read(MCC_HEADER_SIZE)
        if u32(hdr, 4) != 13 or hdr[0:4] != b'daeh':
            raise RawError('%s is not an MCC v13 map' % path)
        if u16(hdr, 0x1C) & HDR_FLAG_COMPRESSED:
            if not auto_decompress:
                raise RawError('%s is compressed' % path)
            try:
                from . import mcc_cache
            except ImportError:      # run as a script
                import mcc_cache
            self.path = path = mcc_cache.ensure_decompressed(path)
        self._f = open(path, 'rb')
        self.m = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        m = self.m
        self.dats = dats or DatStore(dat_dir)
        self.file_size = u32(m, 8)
        self.scenario_type = u16(m, 0x18)
        self.flags = u16(m, 0x1C)
        self.external = bool(self.flags & HDR_FLAG_EXTERNAL_DAT)
        self.meta_off = u32(m, 0x10)
        self.meta_size = u32(m, 0x14)
        self.mask = u32(m, 0x2D0)
        self.raw_off = u32(m, 0x2F0)
        self.raw_size = u32(m, 0x2F4)
        mo = self.meta_off
        self.tag_table_off = mo + u32(m, mo + 8)
        self.tag_count = u32(m, mo + 0x18)
        self.tags = []
        for i in range(self.tag_count):
            g, d, a, s = struct.unpack_from('<4sIII', m, self.tag_table_off + i * 16)
            self.tags.append((g[::-1].decode('latin1'), d, a, s))
        self._names = None
        self._gestalt = None

    # addressing ---------------------------------------------------------------------------------
    def a2o(self, addr):
        """tag-data memory address -> file offset in THIS map (may be out of range for shared tags)"""
        return self.meta_off + (addr - self.mask)

    def tag_off(self, i):
        return self.a2o(self.tags[i][2])

    def tag_is_local(self, i):
        g, d, a, s = self.tags[i]
        o = self.a2o(a)
        return s > 0 and 0 <= o and o + s <= len(self.m)

    def tags_of(self, group):
        return [i for i, t in enumerate(self.tags) if t[0] == group]

    def names(self):
        if self._names is None:
            m = self.m
            cnt, off, sz, ioff = u32(m, 0x20), u32(m, 0x24), u32(m, 0x28), u32(m, 0x2C)
            out = []
            for i in range(cnt):
                o = u32(m, ioff + i * 4)
                if o == 0xFFFFFFFF or o >= sz:
                    out.append(None)
                    continue
                e = m.find(b'\x00', off + o)
                out.append(bytes(m[off + o:e]).decode('latin1'))
            self._names = out
        return self._names

    # bitmaps ------------------------------------------------------------------------------------
    @property
    def bitmap_store(self):
        return TEXTURES_DAT if self.external else 'map'

    def bitmap_elements(self, tag_index):
        """List of BitmapElement for a local bitm tag ([] if the tag has no data in this map)."""
        if not self.tag_is_local(tag_index):
            return []
        o = self.tag_off(tag_index)
        n, p = i32(self.m, o + BITMAP_BLOCK_OFFSET), u32(self.m, o + BITMAP_BLOCK_OFFSET + 4)
        if n <= 0 or p == 0:
            return []
        bo = self.a2o(p)
        return [BitmapElement(tag_index, j, bo + j * BITMAP_ELEMENT_SIZE,
                              self.m[bo + j * BITMAP_ELEMENT_SIZE:bo + (j + 1) * BITMAP_ELEMENT_SIZE]) for j in range(n)]

    def read_bitmap_pixels(self, el, lod=0):
        """Decompressed pixel bytes of a BitmapElement (the same bytes the Vista map stores zlib-compressed)."""
        off, size = el.lod_offset[lod], el.lod_size[lod]
        if size == 0:
            return None
        buf = self.dats.get(TEXTURES_DAT) if self.external else self.m
        data, _ = decode_texture_blob(buf, off, size)
        return data

    # geometry -----------------------------------------------------------------------------------
    def geometry_infos(self, groups=None):
        """All geometry_block_info structs of local tags (sound-gestalt Extra Info lipsync slots excluded)."""
        out = []
        m = self.m
        for i, t in enumerate(self.tags):
            g = t[0]
            if groups is not None and g not in groups:
                continue
            sites = [s for s in GEOMETRY_INFO_SITES if s[0] == g]
            if not sites or not self.tag_is_local(i):
                continue
            root = self.tag_off(i)
            for _, path, field, label in sites:
                for so in self._walk(root, path):
                    fo = so + field
                    out.append(GeometryInfo(i, g, label, fo, m[fo:fo + GEOMETRY_INFO_SIZE]))
        return out

    def _walk(self, base, path, depth=0):
        if depth == len(path):
            yield base
            return
        boff, esz = path[depth]
        n, p = i32(self.m, base + boff), u32(self.m, base + boff + 4)
        if n <= 0 or n > 100000 or p == 0:
            return
        bo = self.a2o(p)
        if not 0 <= bo < len(self.m):
            return
        for k in range(n):
            for x in self._walk(bo + k * esz, path, depth + 1):
                yield x

    def read_geometry_block(self, info):
        """Bytes of the 'blkh' ... 'fklb' block.  MCC maps: absolute offsets in this file (location bits only
        appear in shared.map, where location 2 = shared.map itself)."""
        if not info.present:
            return None
        if info.location not in (LOC_SELF, LOC_SHARED, LOC_SP_SHARED, LOC_MAINMENU):
            raise RawError('bad location')
        blob = bytes(self.m[info.offset:info.offset + info.block_size])
        if blob[:4] != b'hklb':
            raise RawError("no 'blkh' magic at 0x%X (location %d)" % (info.offset, info.location))
        return blob

    # sound --------------------------------------------------------------------------------------
    def sound_gestalt(self):
        if self._gestalt is None:
            ug = [i for i in self.tags_of('ugh!') if self.tag_is_local(i)]
            if not ug:
                raise RawError('no local ugh! tag')
            self._gestalt = SoundGestalt(self, ug[0])
        return self._gestalt


# --------------------------------------------------------------------------------------------------
# Vista reader (reference implementation)
# --------------------------------------------------------------------------------------------------
class VistaRaw(object):
    """Resolve Vista raw offset dwords against a Vista map + its shared maps (mainmenu/shared/single_player_shared)."""

    def __init__(self, map_path, maps_dir=None):
        self.maps_dir = maps_dir or _paths.vista_maps_dir()
        self._mm = {}
        self._files = []
        self.self_path = map_path

    def _file(self, loc):
        if loc not in self._mm:
            path = self.self_path if loc == LOC_SELF else os.path.join(self.maps_dir, VISTA_LOCATION_FILES[loc])
            f = open(path, 'rb')
            self._files.append(f)
            self._mm[loc] = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        return self._mm[loc]

    def read(self, ptr, size):
        loc, off = vista_split_pointer(ptr)
        mm = self._file(loc)
        if off + size > len(mm):
            raise RawError('vista pointer 0x%08X +0x%X beyond file' % (ptr, size))
        return bytes(mm[off:off + size])

    def read_bitmap_pixels(self, ptr, size):
        return zlib.decompress(self.read(ptr, size))
