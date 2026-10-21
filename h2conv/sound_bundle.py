# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Sound bundle builder for the handler architecture.

Builds, from the MCC sound gestalts of the conversion sources (the input map's own `ugh!`, and the `ugh!` of every MCC
shared map a converted `snd!` tag lives in), everything the Vista side needs:

  * one flat Vista gestalt: playbacks, scales, import names, pitch-range parameters, pitch ranges, permutations,
    custom playbacks, runtime permutation flags, chunks, promotions, extra info  (see `Bundle`)
  * one 20-byte Vista `snd!` body per converted sound (`SndRec.body`) with indices into that gestalt
  * the converted audio, one `PermAudio` per distinct permutation payload (chunks already cut the Vista way)

It generalises an earlier single-gestalt prototype: it reads through
`mcc_cache.MccCache` (compressed inputs, MP maps with the shared address mask) instead of the private `H2Map`, merges
several source gestalts into one output gestalt, compacts every table to the entries the kept sounds use, and caches
the expensive Opus transcodes on disk (sound_cache.py).

Ordering rules (derived from the stock MCC headlong -> Vista headlong pair):
  * playbacks, scales, import names, pitch-range parameters: used entries only, in SOURCE-table order (the stock Vista
    gestalt keeps the relative order of the MCC one), de-duplicated by content;
  * custom playbacks, promotions, extra info: first use in output sound order, one output element per distinct SOURCE element
    (stock Vista does not merge byte-equal custom playbacks; promotions / extra info carry runtime state or an owner), not
    merged by content;
  * pitch ranges / permutations / chunks: in output sound order;
  * runtime permutation flags: two sequences (normal byte-aligned, lip-sync dialogue n+5+ceil(log2 n) bits), see `encoded_flag_bits`;
  * transcoded mono dialogue (classes 19 / 47) is resampled to 22.05 kHz like the stock Vista maps; promotion timing values are
    recomputed from the converted audio.

Pure Python + numpy; no windows, no subprocesses.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass
import hashlib
import os
import struct
import time

from . import tagtree as T
from . import sound_codec as sc
from . import sound_cache as scache
from .sound_gestalt import DatPool, mcc_find_dat, mcc_dat_dir, SAMPLE_RATE_HZ, CHANNELS

NULL = 0xFFFFFFFF
VISTA_RATE_ENUM = {22050: 0, 44100: 1, 32000: 2}
DIALOG_CLASSES = (19,)              # unit_dialog: 22.05 kHz mono in the stock Vista maps (22611 of 22612 over 15 campaign maps; mission / cortana dialogue is MIXED 22/32/44 kHz)
MCC_UGH = dict(codecs=0x00, playbacks=0x08, scales=0x10, import_names=0x18, pitch_range_params=0x20,
               pitch_ranges=0x28, permutations=0x30, custom_playbacks=0x38, reflections=0x40, lowpass=0x48,
               remastered=0x50, runtime_perm_flags=0x58, chunks=0x60, promotions=0x68, extra_info=0x70)
ELEM = dict(playbacks=0x38, scales=0x14, import_names=4, pitch_range_params=0xA, pitch_ranges=0xC, permutations=0x10,
            custom_playbacks=0x34, promotions=0x1C, extra_info=0x14C)
VISTA_EXTRA_SIZE = 0x2C
VISTA_ELEM = dict(playbacks=0x38, scales=0x14, import_names=4, pitch_range_params=0xA, pitch_ranges=0xC,
                  permutations=0x10, custom_playbacks=0x34, runtime_perm_flags=1, chunks=0xC, promotions=0x1C,
                  extra_info=VISTA_EXTRA_SIZE)
I16_MAX = 32767
# Pitch Range +6 (runtime permutation flag bit index) is an UNSIGNED 16-bit field in the engine, 0xFFFF = none: halo2.exe sub_481040 returns
# (unsigned __int16)this[3] (0xFFFF -> -1), sub_43DB82 / sub_43DBE6 use `*(unsigned __int16 *)(a2 + 6) >> 3` as the byte index into the Runtime
# Permutation Flags array, and the load-time merge sub_43CAE5 writes the second (encoded, class 19 Deterministic) counter into it as a WORD, up to
# 50,176 = the size in bits of the 6,272-byte 'deterministic game sound globals' buffer (sub_4A5498).  So the limits are 65,534 for the normal
# sequence and 50,176 for the encoded one - not int16.  (level5 / level6 of Mombasa Mayhem: 35,001 encoded bits, 4,478 pitch ranges.)
FLAG_INDEX_MAX = 0xFFFE
ENCODED_FLAG_BITS_MAX = 50176

# WMA music (the 'codec' encoding 2 blobs, only menu_music in/loop in the MCC maps).  Stock Vista labels the two music sounds compression 1
# (XADPCM) although the data is the WMA blob, and writes Maximum Play Time = bytes / 24806.25 B/s (= 44100 * 9/16, the real WMA data
# rate: 481,637 B = 19.4 s): 19415 ms and 97304 ms in mainmenu.map AND shared.map, reproduced exactly by floor(bytes * 4000 / 99225).
# The engine's time->byte routine (halo2.exe sub_480AF2) is keyed by the compression byte - with comp 4 it assumes 44,100 B/s and the
# track "lasts" 10.9 s (MCC keeps comp 4 + bytes/44100).  The loader dispatches on the ENCODING byte (sub_43C4A4), so the label only
# changes that math.  The 480 stock comp-4 announcer sounds of shared.map are linked, never converted.
WMA_VISTA_COMPRESSION = 1
WMA_BYTES_PER_SECOND_X4 = 99225

# snd! sound classes that exist only in MCC (Vista enum slots are 'unused'); none occurs in classic sound\ tags
# Fallback: weapon_fire_lod(0xF)/weapon_fire_lod_far(0x10) -> weapon_fire(4)
CLASS_FALLBACK = {0x0F: 0x04, 0x10: 0x04}
MCC_ONLY_CLASSES = {0x0F, 0x10, 0x11, 0x27, 0x28, 0x29}


def encoded_flag_bits(n):
    """Runtime-flag bits one 'encoded' (lip-sync dialogue, Encoded Permutation Data Index != -1) pitch range of n permutations
    reserves: n + 5 + max(1, ceil(log2 n)).  Own sequence, starts at 0, independent of the byte-aligned normal sequence (whose
    total is the 'Runtime Permutation Flags' array size).  Verified on the pitch-range tables: MCC single_player_shared 3026/3026
    consecutive deltas, Vista single_player_shared 2494/2494 (n=2 -> 8, 3 -> 10, 4 -> 11, 5 -> 13, ... 23 -> 33, 19 -> 29)."""
    n = max(int(n), 1)
    return n + 5 + max(1, (n - 1).bit_length())


def mcc_flags_to_vista(f):
    """snd! flags16: MCC inserted bits 10,11 ('Play Only In Legacy/Remastered Mode'); bits 12..15 of MCC are bits
    10..13 of Vista (19/19 stock sounds)."""
    return (f & 0x3FF) | ((f & 0xF000) >> 2)


def is_remastered_name(name):
    return (name or '').lower().startswith('sound_remastered\\')


def _s8(v):
    return v - 256 if v > 127 else v


# ------------------------------------------------------------------------------------------------
# node helpers
# ------------------------------------------------------------------------------------------------
def _datum_from(ctx, src, datum):
    """MCC datum -> output datum (0xFFFFFFFF when null/dropped).  `src` = the Source the node was read from (its table defines the
    datum), or None = "the map ctx is converting right now" (ctx.cur_src, what ctx.remap_datum already assumes)."""
    if datum in (NULL, 0, None):
        return NULL
    if src is None:
        return ctx.remap_datum(datum)
    plan = getattr(ctx, 'plan', None)
    if plan is not None and hasattr(plan, 'remap_datum'):
        try:
            r = plan.remap_datum(src.key, datum)
        except Exception:
            r = None
        return NULL if r is None else r
    if src.cache is ctx.src:
        return ctx.remap_datum(datum)
    t = src.cache.tag_by_datum(datum)
    if t is None or not t.name:
        return NULL
    s2 = ctx.src.tag_by_name(t.group, t.name)
    if s2 is None:
        ctx.warn('snd_ref_unresolved', 'tag reference %s %s of a non-input map has no twin in the input map' % (t.group, t.name))
        return NULL
    return ctx.remap_datum(s2.datum)


def _sid_from(ctx, src, sid):
    if not sid:
        return 0
    if src is None:
        return ctx.remap_sid(sid)
    return ctx.sid(src.cache.string_id(sid))


def remap_fields(ctx, src, node):
    """Remap tag references / string ids of ONE node's struct in place (children are not touched)."""
    d = node.data
    for f in node.fields:
        if f.type == 'tagref':
            if f.size == 8:
                old = struct.unpack_from('<I', d, f.offset + 4)[0]
                struct.pack_into('<I', d, f.offset + 4, _datum_from(ctx, src, old) if old != NULL else NULL)
            else:
                struct.pack_into('<I', d, f.offset, _datum_from(ctx, src, struct.unpack_from('<I', d, f.offset)[0]))
        elif f.type == 'stringid':
            struct.pack_into('<I', d, f.offset, _sid_from(ctx, src, struct.unpack_from('<I', d, f.offset)[0]))


def clone_node(ctx, src, m, vfields, vpath, group, size=None):
    """Copy MCC node `m` into a node of the Vista layout `vfields` (same offsets for the part that is kept),
    remapping tag references and string ids.  `size` truncates the struct (extra info 0x14C -> 0x2C)."""
    sz = len(m.data) if size is None else size
    v = T.Node(group, vpath, vfields, sz, bytes(m.data[:sz]))
    remap_fields(ctx, src, v)
    for f in vfields:
        if f.type in T.BLOCK_TYPES:
            es = T.element_size(f)
            cp = T.child_path(vpath, f)
            v.blocks[f.offset] = [clone_node(ctx, src, k, f.children, cp, group, es)
                                  for k in (m.blocks.get(f.offset) or [])]
        elif f.type in T.DATA_TYPES:
            v.datas[f.offset] = bytes(m.datas.get(f.offset) or b'')
    return v


def node_signature(n):
    """Hashable content signature of a node tree (pointer words of blocks/datarefs ignored)."""
    d = bytearray(n.data)
    for f in n.fields:
        if f.type in T.BLOCK_TYPES or f.type in T.DATA_TYPES:
            d[f.offset:f.offset + 8] = b'\0' * 8
    return (bytes(d),
            tuple((off, tuple(node_signature(k) for k in kids)) for off, kids in sorted(n.blocks.items())),
            tuple((off, bytes(b)) for off, b in sorted(n.datas.items())))


# ------------------------------------------------------------------------------------------------
# one MCC sound gestalt
# ------------------------------------------------------------------------------------------------
class McGestalt(object):
    """Reader of one MCC `ugh!` tag (MCC layout) on top of MccCache."""

    def __init__(self, cache, tag, mcc_layout):
        self.cache = cache
        self.tag = tag
        self.mm = cache.mm
        base = tag.file_offset
        self.blocks = {}
        for name, off in MCC_UGH.items():
            cnt, addr = struct.unpack_from('<iI', self.mm, base + off)
            self.blocks[name] = (cnt, cache.addr_to_offset(addr) if cnt > 0 and addr else None)
        self.fields = {f.name: f for f in mcc_layout.fields}
        cnt, off = self.blocks['codecs']
        self.codecs = [tuple(self.mm[off + i * 3: off + i * 3 + 3]) for i in range(cnt)] if cnt else []
        cnt, off = self.blocks['chunks']
        self.chunk_lists = []                     # language slot -> (count, file offset of the 16-byte records)
        for L in range(max(cnt, 0)):
            c, a = struct.unpack_from('<iI', self.mm, off + L * 8)
            self.chunk_lists.append((c, cache.addr_to_offset(a) if c > 0 and a else None))

    def count(self, name):
        return max(self.blocks[name][0], 0)

    def elem_bytes(self, name, i):
        cnt, off = self.blocks[name]
        if not 0 <= i < cnt:
            raise IndexError('%s[%d] outside 0..%d' % (name, i, cnt))
        es = ELEM[name]
        return bytes(self.mm[off + i * es: off + (i + 1) * es])

    def pitch_range(self, i):
        imp, par, enc, rtf, first, n = struct.unpack('<3hHhh', self.elem_bytes('pitch_ranges', i))
        if rtf == 0xFFFF:                                                  # +6 is an unsigned bit index in the engine; 0xFFFF = none
            rtf = -1
        return dict(import_name=imp, params=par, encoded_data=enc, runtime_flag=rtf, first_perm=first, perm_count=n)

    def permutation(self, i):
        cnt, off = self.blocks['permutations']
        if not 0 <= i < cnt:
            raise IndexError('permutation %d outside 0..%d' % (i, cnt))
        o = off + i * 16
        imp, skip, gain, info, lnt = struct.unpack_from('<hHbbH', self.mm, o)
        lc, la = struct.unpack_from('<iI', self.mm, o + 8)
        langs = []
        if lc > 0 and la:
            lo = self.cache.addr_to_offset(la)
            for L in range(lc):
                langs.append(struct.unpack_from('<IHH', self.mm, lo + L * 8))      # (sample_size, first_chunk, count)
        return dict(import_name=imp, skip=skip, gain=gain, info=info, lnt=lnt, langs=langs)

    def chunk(self, i, lang):
        """-> (file_offset, size, flags_hi3, pcm_bytes, runtime): flags_hi3 bit 2 (4) = F31 (bit 31 of size_flags)."""
        if lang >= len(self.chunk_lists) or self.chunk_lists[lang][1] is None:
            raise IndexError('no chunk list for language slot %d' % lang)
        cnt, off = self.chunk_lists[lang]
        if not 0 <= i < cnt:
            raise IndexError('chunk %d of slot %d outside 0..%d' % (i, lang, cnt))
        fo, X, un, rt = struct.unpack_from('<IIIi', self.mm, off + i * 16)
        return fo, X & 0x1FFFFFFF, X >> 29, un, rt

    def node(self, name, i):
        """Decode one element of a verbatim block (custom_playbacks / promotions / extra_info) with its children."""
        cnt, off = self.blocks[name]
        if not 0 <= i < cnt:
            raise IndexError('%s[%d] outside 0..%d' % (name, i, cnt))
        f = self.fields[{'custom_playbacks': 'Custom Playbacks', 'promotions': 'Promotions',
                         'extra_info': 'Extra Info'}[name]]
        es = ELEM[name]
        raw = bytes(self.mm[off + i * es: off + (i + 1) * es])
        cache = self.cache
        return T.decode_struct(lambda a, n: cache.read_at_addr(a, n), 'ugh!', T.child_path('', f), f.children, es,
                               cache.offset_to_addr(off + i * es), raw=raw)

    def parse_snd(self, body):
        """MCC snd! tag body (0x1C) -> dict."""
        flags, cls = struct.unpack_from('<HB', body, 0)
        ci = body[3]
        rate, enc, comp = self.codecs[ci] if ci < len(self.codecs) else (1, 0, 1)
        pb, pr = struct.unpack_from('<hh', body, 6)
        return dict(flags=flags, cls=cls, codec_index=ci, rate=rate, encoding=enc, compression=comp,
                    remastered_index=struct.unpack_from('<h', body, 4)[0],
                    playback=pb, pitch_range=pr, pitch_range_count=body[0xA], scale=_s8(body[0xB]),
                    promotion=_s8(body[0xC]), custom_playback=_s8(body[0xD]),
                    extra_info=struct.unpack_from('<h', body, 0xE)[0],
                    max_play=struct.unpack_from('<I', body, 0x10)[0],
                    inner_silence=struct.unpack_from('<f', body, 0x14)[0])


class Source(object):
    """One MCC map whose snd! tags are converted: its cache, its gestalt, its external .dat pool."""

    def __init__(self, key, cache, gestalt, dats, external):
        self.key = key
        self.cache = cache
        self.g = gestalt
        self.dats = dats
        self.external = external

    def read_chunk(self, fo, size, un, comp, ch, name, slot, lnt, snd_flags):
        if not self.external:
            return self.cache.raw(fo, size)
        return mcc_find_dat(self.dats, fo, size, un, comp, ch, name, slot, lnt, snd_flags)

    def read_blob(self, off, size):
        """A lip-sync `blkh` resource block (extra info).  H2EK-built maps keep it inside the map; in stock maps the dialogue
        blocks live in sounds_<lang>.dat (stock shared.map: 51/51 found in sounds_en.dat at the element's offset).  A block is
        recognised by its 'hklb' head and 'fklb' tail (8 + section data + resource data + 4 == size)."""
        def ok(mm):
            return off + size <= len(mm) and bytes(mm[off:off + 4]) == b'hklb' and bytes(mm[off + size - 4:off + size]) == b'fklb'
        if not self.external:
            return self.cache.raw(off, size)
        if ok(self.cache.mm):
            return self.cache.raw(off, size)
        for dn in ('en', 'neutral', 'de', 'fr', 'it', 'sp', 'remastered'):
            try:
                mm = self.dats.get(dn)
            except OSError:
                continue
            if ok(mm):
                return bytes(mm[off:off + size])
        raise ValueError('lip-sync block @%#x+%#x not found in the map or any sounds_*.dat' % (off, size))


# ------------------------------------------------------------------------------------------------
# bundle data classes
# ------------------------------------------------------------------------------------------------
class Sel(object):
    """One snd! tag selected for conversion."""
    __slots__ = ('src_key', 'cache', 'tag', 'name', 'out_index', 'out_datum', 'sort_key')

    def __init__(self, src_key, cache, tag, out_index=None, out_datum=None, sort_key=None):
        self.src_key = src_key
        self.cache = cache
        self.tag = tag
        self.name = tag.name or ''
        self.out_index = out_index
        self.out_datum = out_datum
        self.sort_key = sort_key if sort_key is not None else (out_index if out_index is not None else tag.index)


class PermAudio(object):
    """One distinct permutation payload, already cut into Vista chunks.  `tokens` are filled by the ugh! builder."""
    __slots__ = ('parts', 'sizes', 'tokens', 'key')

    def __init__(self, parts, key):
        self.parts = parts
        self.sizes = [len(p) for p in parts]
        self.tokens = None
        self.key = key


class SndRec(object):
    def __init__(self, sel, body):
        self.sel = sel
        self.name = sel.name
        self.mcc_body = body
        self.snd = None
        self.prs = []                 # [(src pitch range dict, [PermIn])]
        self.body = None              # final 20-byte Vista body
        self.max_play_ms = 0
        self.vista = {}               # rate / encoding / compression written
        self.error = None


class PermIn(object):
    __slots__ = ('src_idx', 'perm', 'use', 'chunks', 'meta', 'job', 'payload', 'f31', 'samples', 'kind', 'out_rate')

    def __init__(self):
        self.job = None
        self.payload = None
        self.f31 = False
        self.samples = 0
        self.out_rate = 0


class ExtraInfo(object):
    __slots__ = ('node', 'blob', 'token')

    def __init__(self, node, blob):
        self.node = node
        self.blob = blob
        self.token = None


class Bundle(object):
    def __init__(self):
        self.recs = []
        self.playbacks = []
        self.scales = []
        self.pr_params = []
        self.import_names = []
        self.custom = []
        self.promotions = []
        self.extras = []
        self.pitch_ranges = []        # (import, params, encoded, runtime_flag_bit, first_perm, perm_count)
        self.perms = []               # (import, skip, gain, info, lnt, sample_size, first_chunk, chunk_count)
        self.chunk_rows = []          # (PermAudio, part index, size_and_flags_u32)
        self.rt_bytes = 0
        self.by_addr = {}             # tag address -> [SndRec]
        self.by_tag = {}              # (src_key, tag index) -> SndRec
        self.notes = {}
        self.sources = {}


# ------------------------------------------------------------------------------------------------
# selection
# ------------------------------------------------------------------------------------------------
def source_cache(ctx, key):
    if key in (None, 'src', 'input'):
        return ctx.src
    sh = getattr(ctx, 'mcc_shared', None)
    return sh.get(key) if sh is not None else None


def select_sounds(ctx, include_remastered=None):
    """The snd! tags to convert, in OUTPUT order.
    * With a plan: every entry (group 'snd!', action 'convert') - the planner already dropped/redirected the
      sound_remastered\\ ones; order = out_index.
    * Without a plan (a test double / unit tests): every local snd! of the input, `sound_remastered\\` excluded; order = the
      reference map's tag index when ctx.ref_tag finds the name, else the input tag index."""
    sels = []
    plan = getattr(ctx, 'plan', None)
    if include_remastered is None:
        include_remastered = bool((ctx.opts or {}).get('sound_include_remastered', False))
    if plan is not None:
        for e in plan.entries:
            if e.group != 'snd!' or e.action != 'convert':
                continue
            key = getattr(e, 'src_map', None) or 'src'
            cache = source_cache(ctx, key)
            if cache is None:
                ctx.warn('snd_no_source', 'snd! %s: source map %r is not available' % (e.name, key))
                continue
            tag = cache.tags[e.src_index]
            if not tag.has_data:
                ctx.warn('snd_no_data', 'snd! %s has no tag data in %s' % (e.name, key))
                continue
            sels.append(Sel(key, cache, tag, e.out_index, getattr(e, 'out_datum', None)))
        sels.sort(key=lambda s: s.sort_key)
        return sels
    ref_tag = getattr(ctx, 'ref_tag', None)
    for t in ctx.src.tags:
        if t.group != 'snd!' or not t.has_data:
            continue
        if not include_remastered and (is_remastered_name(t.name) or
                                       struct.unpack('<H', ctx.src.read_at_addr(t.addr, 2))[0] & 0x800):   # 'Play Only In Remastered Mode'
            continue
        sk = t.index
        if ref_tag is not None and t.name:
            r = ref_tag('snd!', t.name)
            if r is not None:
                sk = (0, r.index)
            else:
                sk = (1, t.index)
        else:
            sk = (1, t.index)
        sels.append(Sel('src', ctx.src, t, None, None, sk))
    sels.sort(key=lambda s: s.sort_key)
    return sels


# ------------------------------------------------------------------------------------------------
# the builder
# ------------------------------------------------------------------------------------------------
class _Table(object):
    """Insertion-ordered de-duplicating table."""

    def __init__(self):
        self.items = []
        self.index = {}

    def add(self, key, item=None):
        i = self.index.get(key)
        if i is None:
            i = len(self.items)
            self.items.append(key if item is None else item)
            self.index[key] = i
        return i


class Builder(object):
    def __init__(self, ctx, sels, opts=None):
        self.ctx = ctx
        self.sels = sels
        self.opts = dict(ctx.opts or {})
        if opts:
            self.opts.update(opts)
        self.lang = int(self.opts.get('sound_language', 0))
        self.sources = {}
        self.cache = scache.SoundCache(self.opts.get('sound_cache_dir'),
                                       enabled=not self.opts.get('no_sound_cache', False))
        self.dat_dir = mcc_dat_dir(self.opts.get('mcc_dat_dir'))
        self.jobs = {}                # job key -> (kind, args)
        self.job_out = {}
        self.job_err = {}
        self.warnings = []
        self.progress = self.opts.get('progress')
        self.quiet = bool(self.opts.get('quiet'))

    # ---- sources --------------------------------------------------------------------------------
    def source(self, key):
        s = self.sources.get(key)
        if s is not None:
            return s
        cache = source_cache(self.ctx, key)
        tag = cache.sound_gestalt
        if tag is None or not tag.has_data:
            raise RuntimeError('source map %s has no sound gestalt (ugh!) tag with data' % key)
        external = bool(cache.header['flags'] & 0x08)
        s = Source(key, cache, McGestalt(cache, tag, self.ctx.lay_mcc['ugh!']),
                   DatPool(self.dat_dir) if external else None, external)
        self.sources[key] = s
        return s

    def warn(self, code, msg, tag=None):
        self.warnings.append((code, msg))
        self.ctx.warn(code, msg, tag)

    # ---- phase A: analysis ----------------------------------------------------------------------
    def analyse(self, B):
        t0 = time.time()
        n = 0
        for sel in self.sels:
            src = self.source(sel.src_key)
            body = sel.cache.read_at_addr(sel.tag.addr, 0x1C)
            rec = SndRec(sel, body)
            rec.snd = src.g.parse_snd(body)
            B.recs.append(rec)
            B.by_tag[(sel.src_key, sel.tag.index)] = rec
            B.by_addr.setdefault(sel.tag.addr, []).append(rec)
            try:
                self._analyse_one(src, rec)
            except Exception as e:                            # a broken sound must not kill the whole map
                rec.error = '%s: %s' % (type(e).__name__, e)
                rec.prs = []
                self.warn('snd_failed', 'snd! %s skipped (%s)' % (rec.name, rec.error))
            n += 1
            if self.progress and n % 100 == 0:
                self.progress('analyse', n, len(self.sels), time.time() - t0)

    def _analyse_one(self, src, rec):
        g = src.g
        s = rec.snd
        comp = s['compression']
        ch = CHANNELS.get(s['encoding'], 1)
        if s['pitch_range'] < 0 or s['pitch_range_count'] <= 0:
            return
        out_rate = self.out_rate_for(s, comp, ch)
        for k in range(s['pitch_range_count']):
            pr = g.pitch_range(s['pitch_range'] + k)
            perms = []
            for j in range(pr['perm_count']):
                pidx = pr['first_perm'] + j
                perm = g.permutation(pidx)
                langs = perm['langs']
                lang = self.lang
                use = lang if (lang < len(langs) and langs[lang][2]) else next(
                    (i for i, l in enumerate(langs) if l[2]), None)
                if use is None:
                    self.warn('snd_perm_empty', '%s: permutation %d has no chunks' % (rec.name, pidx))
                    continue
                pi = PermIn()
                pi.src_idx = pidx
                pi.perm = perm
                pi.use = use
                ssz, first, cnt = langs[use]
                chunks, meta = [], []
                for c in range(first, first + cnt):
                    fo, size, fl, un, rt = g.chunk(c, use)
                    chunks.append(src.read_chunk(fo, size, un, comp, ch, rec.name, use, perm['lnt'], s['flags']))
                    meta.append((size, fl, un))
                pi.chunks = chunks
                pi.meta = meta
                self._prepare_audio(rec, pi, comp, ch, out_rate)
                perms.append(pi)
            rec.prs.append((pr, perms))

    def out_rate_for(self, s, comp, ch):
        """Vista sample rate of a TRANSCODED sound.  MCC re-encodes every Opus sound at 48 kHz; the stock Vista maps keep the author's
        22.05 kHz for mono `unit_dialog` (class 19: 22,611 of 22,612 mono Opus sounds of 15 campaign maps + 2,538/2,538 in
        single_player_shared, 46/46 in shared.map).  Mission / cortana dialogue (classes 45, 47) is a per-line author choice (22.05 / 32 /
        44.1 kHz = 574 / 290 / 1469 sounds) that MCC does not record (the Opus spectrum shows no difference), so those stay 44.1 kHz - the
        majority and never lossy.  Every other transcoded class is 44.1 kHz.  `sound_dialog_rate` = 44100 disables the rule."""
        if comp in (5, 0, 3) and ch == 1 and s['cls'] in DIALOG_CLASSES:
            return int(self.opts.get('sound_dialog_rate', 22050))
        return 44100

    def _prepare_audio(self, rec, pi, comp, ch, out_rate=44100):
        s = rec.snd
        pi.out_rate = out_rate
        if comp == 1:                                          # Xbox ADPCM: byte-identical to Vista (120/120 perms)
            payload = b''.join(pi.chunks)
            if len(payload) % (36 * ch):
                self.warn('snd_adpcm_len', '%s: XADPCM payload %d not a multiple of %d' % (rec.name, len(payload), 36 * ch))
            pi.payload = payload
            pi.samples = len(payload) // (36 * ch) * 64
            pi.f31 = bool(pi.meta[-1][1] & 4)
            pi.kind = 'xadpcm'
            pi.chunks = None
        elif comp == 5:                                        # Opus -> 44.1 kHz (dialogue: 22.05 kHz) Xbox ADPCM
            key = scache.content_key(pi.chunks, ch, 'opus', b'' if out_rate == 44100 else struct.pack('<I', out_rate))
            self.jobs.setdefault(key, ('opus', (pi.chunks, ch, out_rate)))
            pi.job = key
            pi.kind = 'opus'
            pi.chunks = None
        elif comp == 4:                                        # WMA ('codec' encoding): same blob in both games
            pi.payload = b''.join(pi.chunks)
            pi.kind = 'wma'
            pi.chunks = None
        elif comp in (0, 3):                                   # raw PCM (remastered tags only) -> XADPCM
            import numpy as np
            raw = b''.join(pi.chunks)
            if comp == 0:
                raw = np.frombuffer(raw, dtype='>i2').astype('<i2').tobytes()
            ch_out = ch
            if ch == 4:
                a = np.frombuffer(raw, dtype='<i2').reshape(-1, 4).astype(np.int32)
                raw = np.stack([(a[:, 0] + a[:, 2]) // 2, (a[:, 1] + a[:, 3]) // 2], axis=1).astype('<i2').tobytes()
                ch_out = 2
            rate_hz = SAMPLE_RATE_HZ.get(s['rate'], 44100)
            key = scache.content_key([raw], ch_out, 'pcm', struct.pack('<II', rate_hz, out_rate))
            self.jobs.setdefault(key, ('pcm', (raw, ch_out, rate_hz, out_rate)))
            pi.job = key
            pi.kind = 'pcm'
            pi.chunks = None
        else:
            raise NotImplementedError('compression %d' % comp)

    # ---- phase B: transcode jobs ---------------------------------------------------------------------
    def run_jobs(self, B):
        """Transcode every distinct job.  Warm-cache hits are served in-process; the misses run sequentially, or - opt-in
        `sound_workers` = 2..4 - on a spawn process pool (python threads were measured 3x SLOWER than one thread:
        the numpy/scipy work is GIL bound)."""
        t0 = time.time()
        keys = list(self.jobs)
        miss = []
        for key in keys:
            kind, args = self.jobs[key]
            ch = args[1]
            hit = self.cache.get(key, ch)
            if hit is not None:
                self.job_out[key] = hit
            else:
                miss.append(key)
        workers = max(1, min(int(self.opts.get('sound_workers', 1)), 4))
        done = 0
        if len(miss) >= 100 and not self.quiet:
            print('  sound: transcoding %d permutations to Xbox ADPCM (%d cached)%s' % (
                len(miss), len(keys) - len(miss), ', %d worker processes' % workers if workers > 1 else ''), flush=True)
            if self.progress is None:
                self.progress = lambda ph, n, tot, dt: print('  sound: %s %d/%d (%.0fs)' % (ph, n, tot, dt), flush=True) if (ph == 'transcode' and n % 250 == 0) else None
        if workers > 1 and len(miss) >= 16:
            try:
                for key, out, err in scache.run_pool(workers, [(k, self.jobs[k][0], self.jobs[k][1]) for k in miss],
                                                     self.cache.dir, self.cache.enabled):
                    if err:
                        self.job_err[key] = err
                    else:
                        self.job_out[key] = out
                        if self.cache.enabled:
                            self.cache.stores += 1     # the worker process wrote the entry itself
                    done += 1
                    if self.progress and done % 50 == 0:
                        self.progress('transcode', done, len(miss), time.time() - t0)
            except Exception as e:                    # the pool itself failed (spawn problem): finish sequentially
                self.warn('snd_pool_failed', 'transcode process pool failed (%r); continuing in this process' % (e,))
        for key in miss:
            if key in self.job_out or key in self.job_err:
                continue
            kind, args = self.jobs[key]
            try:
                if kind == 'opus':
                    out = sc.opus_perm_to_vista_xadpcm(args[0], args[1], args[2])[0]
                else:
                    out = sc.pcm_le_to_vista_xadpcm(args[0], args[1], args[2], args[3])
                self.cache.put(key, out)
                self.job_out[key] = out
            except Exception as e:                    # e.g. opus.dll missing: only the sounds that need this permutation are lost
                self.job_err[key] = repr(e)
            done += 1
            if self.progress and done % 50 == 0:
                self.progress('transcode', done, len(miss), time.time() - t0)
        for key in keys:                                       # the source bytes are not needed any more
            self.jobs[key] = (self.jobs[key][0], None)
        B.notes['transcode_jobs'] = len(keys)
        B.notes['transcode_computed'] = len(miss)
        B.notes['transcode_failed'] = len(self.job_err)
        B.notes['transcode_seconds'] = round(time.time() - t0, 1)
        B.notes['cache'] = self.cache.stats()

    def fix_promotion_timing(self, vnode, rec):
        """Promotion rules carry two DERIVED int32s (+8, +0xC): u2 = floor(mean permutation duration in ms of the rule's pitch
        range) and u1 = floor(u2 * (max_playing_count - 0.25)), 0 when the count is 0 (verified on 96 Vista rules of 5 stock maps:
        u2 92/96, u1 61/61, zero rule 35/35).  The converted audio is a little shorter/longer than the MCC one, so recompute them
        from the converted permutations; keep the MCC values when a permutation has no known length (WMA)."""
        s = rec.snd
        for rule in (vnode.blocks.get(0) or []):
            idx, mx = struct.unpack_from('<hh', rule.data, 0)
            if not 0 <= idx < len(rec.prs):
                continue
            perms = rec.prs[idx][1]
            ms = []
            for pi in perms:
                if not pi.samples:
                    ms = None
                    break
                hz = pi.out_rate if pi.kind in ('opus', 'pcm') else SAMPLE_RATE_HZ.get(s['rate'], 44100)
                ms.append(pi.samples * 1000.0 / hz)
            if not ms:
                continue
            u2 = int(sum(ms) / len(ms))
            u1 = int(u2 * (mx - 0.25)) if mx > 0 else 0
            struct.pack_into('<ii', rule.data, 8, u1, u2)

    # ---- phase C: assembly ------------------------------------------------------------------------------
    def assemble(self, B):
        ctx = self.ctx
        vlay = ctx.lay_vista['ugh!']
        vf = {f.name: f for f in vlay.fields}
        for r in B.recs:
            if r.error is None:
                bad = [pi.job for pr, ps in r.prs for pi in ps if pi.job is not None and pi.job in self.job_err]
                if bad:
                    r.error = 'transcode failed: %s' % self.job_err[bad[0]]
                    r.prs = []
                    self.warn('snd_failed', 'snd! %s skipped (%s)' % (r.name, r.error))
        recs = [r for r in B.recs if r.error is None]
        # ---- usage marking per source (for the source-ordered tables)
        used = {}
        for r in recs:
            u = used.setdefault(r.sel.src_key, dict(playbacks=set(), scales=set(), import_names=set(), pr_params=set()))
            g_ = self.sources[r.sel.src_key].g
            lim_ = {k: g_.count(k) for k in ('playbacks', 'scales', 'import_names', 'pitch_range_params')}
            if 0 <= r.snd['playback'] < lim_['playbacks']:                 # an index outside the table (corrupt tag) maps to -1
                u['playbacks'].add(r.snd['playback'])
            if 0 <= r.snd['scale'] < lim_['scales']:
                u['scales'].add(r.snd['scale'])
            for pr, perms in r.prs:
                if 0 <= pr['params'] < lim_['pitch_range_params']:
                    u['pr_params'].add(pr['params'])
                if 0 <= pr['import_name'] < lim_['import_names']:
                    u['import_names'].add(pr['import_name'])
                for pi in perms:
                    if 0 <= pi.perm['import_name'] < lim_['import_names']:
                        u['import_names'].add(pi.perm['import_name'])
        pb_t, sc_t, ip_t, imp_t = _Table(), _Table(), _Table(), _Table()
        maps = {}
        order = []
        for r in recs:
            if r.sel.src_key not in order:
                order.append(r.sel.src_key)
        for key in order:
            src = self.sources[key]
            g = src.g
            u = used[key]
            m = maps[key] = dict(playbacks={}, scales={}, pr_params={}, import_names={})
            for i in sorted(u['playbacks']):
                m['playbacks'][i] = pb_t.add(g.elem_bytes('playbacks', i))
            for i in sorted(u['scales']):
                m['scales'][i] = sc_t.add(g.elem_bytes('scales', i))
            for i in sorted(u['pr_params']):
                m['pr_params'][i] = ip_t.add(g.elem_bytes('pitch_range_params', i))
            for i in sorted(u['import_names']):
                sid = struct.unpack('<I', g.elem_bytes('import_names', i))[0]
                text = src.cache.string_id(sid) if sid else ''
                m['import_names'][i] = imp_t.add(text)
        B.playbacks, B.scales, B.pr_params, B.import_names = pb_t.items, sc_t.items, ip_t.items, imp_t.items
        # ---- first-use tables
        B.custom = []
        promo_map = {}
        extra_map = {}
        cust_map = {}
        audio_by_key = {}
        rt_bit = 0                    # normal sequence: ceil(n/8)*8 bits per pitch range; the array holds exactly these bits
        enc_bit = 0                   # 'encoded' (lip-sync dialogue) sequence: n + 5 + max(1, ceil(log2 n)) bits, NOT in the array

        def cust_index(src, i):
            k = (src.key, i)
            if k in cust_map:
                return cust_map[k]
            mn = src.g.node('custom_playbacks', i)
            vn = clone_node(ctx, src, mn, vf['Custom Playbacks'].children, T.child_path('', vf['Custom Playbacks']),
                            'ugh!')
            new = len(B.custom)          # one element per distinct SOURCE element (stock Vista does not merge equal ones)
            B.custom.append(vn)
            cust_map[k] = new
            return new

        def promo_index(src, i, rec):
            k = (src.key, i)
            if k not in promo_map:
                mn = src.g.node('promotions', i)
                promo_map[k] = len(B.promotions)
                vn = clone_node(ctx, src, mn, vf['Promotions'].children, T.child_path('', vf['Promotions']), 'ugh!')
                B.promotions.append(vn)
                self.fix_promotion_timing(vn, rec)
            return promo_map[k]

        def extra_index(src, i):
            k = (src.key, i)
            if k not in extra_map:
                mn = src.g.node('extra_info', i)
                # MCC Extra Info = Encoded Permutation Section + EIGHT resource groups (0x24 B each from +8): one lip-sync blkh per
                # language (group 0 = English is the one stock Vista keeps, byte-identical).  Use the converted language's group.
                lg = self.lang
                if not (0 <= lg < 8) or not any(mn.data[8 + 0x24 * lg:12 + 0x24 * lg]) and lg != 0:
                    lg = 0
                if lg:
                    mn.data[8:0x2C] = mn.data[8 + 0x24 * lg: 0x2C + 0x24 * lg]
                    mn.blocks[0x18] = mn.blocks.get(0x18 + 0x24 * lg) or []
                struct.pack_into('<I', mn.data, 0x20, NULL)        # Owner Tag: set to the new ugh! datum later
                vn = clone_node(ctx, src, mn, vf['Extra Info'].children, T.child_path('', vf['Extra Info']),
                                'ugh!', VISTA_EXTRA_SIZE)
                roff, rsize = struct.unpack_from('<II', mn.data, 8)
                blob = None
                if rsize:
                    try:
                        blob = src.read_blob(roff, rsize)
                    except Exception as e:
                        self.warn('snd_extra_blob', 'extra info %d of %s: resource block @%#x+%#x unreadable (%s)'
                                  % (i, src.key, roff, rsize, e))
                extra_map[k] = len(B.extras)
                B.extras.append(ExtraInfo(vn, blob))
            return extra_map[k]

        for r in recs:
            src = self.sources[r.sel.src_key]
            m = maps[r.sel.src_key]
            s = r.snd
            comp = s['compression']
            new_pr_first = len(B.pitch_ranges)
            n_new_pr = 0
            max_ms = 0
            has_audio = False
            wma_max_bytes = 0
            for pr, perms in r.prs:
                first_perm_new = len(B.perms)
                nperm = 0
                for pi in perms:
                    payload = pi.payload if pi.payload is not None else self.job_out[pi.job]
                    ch_out = CHANNELS.get(s['encoding'], 1)
                    if pi.kind == 'pcm' and ch_out == 4:
                        ch_out = 2
                    if pi.kind in ('opus', 'pcm'):
                        pi.samples = len(payload) // (36 * ch_out) * 64
                        pi.f31 = False
                    # chunks
                    if pi.kind == 'wma':
                        sizes = [len(payload)]
                        wma_max_bytes = max(wma_max_bytes, len(payload))
                    else:
                        sizes = sc.vista_split_chunks(len(payload))
                    parts, pos = [], 0
                    for sz in sizes:
                        parts.append(payload[pos:pos + sz])
                        pos += sz
                    ak = (hashlib.md5(payload).digest(), len(payload), tuple(sizes))
                    pa = audio_by_key.get(ak)
                    if pa is None:
                        pa = audio_by_key[ak] = PermAudio(parts, ak)
                    first_chunk = len(B.chunk_rows)
                    for ci, sz in enumerate(sizes):
                        flag = 0x80000000 if (pi.f31 and ci == len(sizes) - 1) else 0
                        B.chunk_rows.append((pa, ci, sz | flag))
                    B.perms.append((m['import_names'].get(pi.perm['import_name'], -1), pi.perm['skip'], pi.perm['gain'],
                                    pi.perm['info'], pi.perm['lnt'], len(payload), first_chunk, len(sizes)))
                    nperm += 1
                    has_audio = True
                    if pi.samples:
                        rate = pi.out_rate if pi.kind in ('opus', 'pcm') else SAMPLE_RATE_HZ.get(s['rate'], 44100)
                        max_ms = max(max_ms, pi.samples * 1000 // rate)
                    pi.payload = None
                if pr['runtime_flag'] == -1:
                    rtb = -1
                elif pr['encoded_data'] != -1:
                    rtb = enc_bit
                    enc_bit += encoded_flag_bits(nperm)
                else:
                    rtb = rt_bit
                    rt_bit += (nperm + 7) // 8 * 8
                B.pitch_ranges.append((m['import_names'].get(pr['import_name'], -1), m['pr_params'].get(pr['params'], -1),
                                       pr['encoded_data'], rtb, first_perm_new, nperm))
                n_new_pr += 1
            # ---- the Vista 20-byte snd! body
            if comp in (5, 0, 3):
                orates = {pi.out_rate for pr, perms in r.prs for pi in perms} or {44100}
                rate_e, comp_v = VISTA_RATE_ENUM.get(min(orates), 1), 1
            else:
                rate_e = s['rate'] if s['rate'] in (0, 1, 2) else 1
                comp_v = comp
            enc_v = 1 if s['encoding'] == 3 else s['encoding']
            # maximum play time: the MCC value is kept.  The Vista tool's rule is NOT simply "longest permutation" (139 of 840 shared.map sounds
            # differ from their own longest permutation, e.g. ball_drop 120 ms vs 483 ms) but the MCC tool applies the same rule: copying it is within
            # 0.3 % (median) / 4.2 % (p99) of Vista on 660 Opus sounds, recomputing from the longest permutation is 34 % off at p99.
            maxplay = s['max_play']
            if wma_max_bytes:
                comp_v = WMA_VISTA_COMPRESSION
                maxplay = wma_max_bytes * 4000 // WMA_BYTES_PER_SECOND_X4
            cls = s['cls']
            if cls in MCC_ONLY_CLASSES:
                new = CLASS_FALLBACK.get(cls, cls)
                self.warn('snd_class', '%s: MCC-only sound class %#x -> %#x' % (r.name, cls, new))
                cls = new
            has_pr = s['pitch_range'] >= 0 and n_new_pr > 0
            pb_new = m['playbacks'].get(s['playback'], -1) if s['playback'] >= 0 else -1
            sc_new = m['scales'].get(s['scale'], -1) if s['scale'] >= 0 else -1
            def safe_index(fn, idx, what, *extra):
                try:
                    return fn(src, idx, *extra) if idx >= 0 else -1
                except IndexError:                                 # index outside the source table (corrupt tag): the sound simply lacks it
                    self.warn('snd_bad_index', '%s: %s index %d is outside the MCC table' % (r.name, what, idx))
                    return -1
            cu_new = safe_index(cust_index, s['custom_playback'], 'custom playback')
            pr_new = safe_index(promo_index, s['promotion'], 'promotion', r)
            ex_new = safe_index(extra_index, s['extra_info'], 'extra info')
            if not has_audio:
                has_pr = False
            r.body = struct.pack('<HBBBBhhBBBBhI', mcc_flags_to_vista(s['flags']), cls, rate_e, enc_v, comp_v, pb_new,
                                 new_pr_first if has_pr else -1, n_new_pr if has_pr else 0, sc_new & 0xFF, pr_new & 0xFF,
                                 cu_new & 0xFF, ex_new, maxplay)
            r.max_play_ms = maxplay
            r.vista = dict(rate=rate_e, encoding=enc_v, compression=comp_v)
        B.rt_bytes = rt_bit // 8
        B.notes['encoded_flag_bits'] = enc_bit
        for r in B.recs:                                       # failed sounds get a silent, valid body
            if r.body is None:
                s = r.snd
                r.body = struct.pack('<HBBBBhhBBBBhI', mcc_flags_to_vista(s['flags']), s['cls'], 1, 0, 1, -1, -1, 0,
                                     0xFF, 0xFF, 0xFF, -1, 0)
                r.vista = dict(rate=1, encoding=0, compression=1)
        B.notes['budget_problems'] = check_budgets(ctx, B, self.opts.get('sound_mode'), self.warn)


# Vista shared.map's sound gestalt (the primary every MP map merges its own gestalt into): counts that the local indices are added to
# at load time: pitch_ranges 1365, permutations 4383, chunks 5570, import_names 3031, extra_info 46
SHARED_GESTALT_COUNTS = {'pitch_ranges': 1365, 'permutations': 4383, 'chunks': 5570, 'import_names': 3031, 'extra_info': 46,
                         'custom_playbacks': 13, 'promotions': 9, 'scales': 53}


def shared_gestalt_counts(ctx):
    """Block counts of the Vista shared.map `ugh!` (read from the file when the context has the Vista maps, else the constants)."""
    try:
        sh = ctx.vista.shared
        t = next(t for t in sh.tags if t.group == 'ugh!' and t.addr)
        raw = sh.read_addr(t.addr, 0x58)
        names = ['playbacks', 'scales', 'import_names', 'pitch_range_params', 'pitch_ranges', 'permutations', 'custom_playbacks',
                 'runtime_perm_flags', 'chunks', 'promotions', 'extra_info']
        return {n: struct.unpack_from('<i', raw, i * 8)[0] for i, n in enumerate(names)}
    except Exception:
        return dict(SHARED_GESTALT_COUNTS)


def check_budgets(ctx, B, mode, warn):
    """Index limits of the Vista gestalt arrays.  In the secondary topology (every MP map) the load-time merge ADDS the shared primary's
    counts to the local indices, so the local arrays must leave room: int16 arrays <= 32767 in total, chunks <= 65535 (u16 first chunk),
    and the int8 snd! fields (scale / promotion / custom playback index) <= 127.  Returns a list of problem strings."""
    secondary = mode != 'self_contained' and getattr(ctx, 'map_type', 1) == 1
    base = shared_gestalt_counts(ctx) if secondary else {}
    lim = (('pitch_ranges', len(B.pitch_ranges), 32767), ('permutations', len(B.perms), 32767),
           ('chunks', len(B.chunk_rows), 65535), ('import_names', len(B.import_names), 32767),
           ('playbacks', len(B.playbacks), 32767), ('scales', len(B.scales), 127), ('custom_playbacks', len(B.custom), 127),
           ('promotions', len(B.promotions), 127), ('extra_info', len(B.extras), 32767))
    probs = []
    for nm, n, mx in lim:
        total = n + max(base.get(nm, 0), 0)
        if total > mx:
            probs.append('%s: %d local + %d primary = %d > %d' % (nm, n, base.get(nm, 0), total, mx))
    for pr in probs:
        warn('snd_budget', 'sound gestalt budget exceeded: ' + pr)
    normal_bits = B.rt_bytes * 8 + 8 * max(base.get('runtime_perm_flags', 0), 0)
    if max([p[3] for p in B.pitch_ranges] + [0]) > FLAG_INDEX_MAX or normal_bits > FLAG_INDEX_MAX + 1:
        warn('snd_budget', 'runtime permutation flag bit index exceeds the engine\'s unsigned 16-bit field (%d bits + primary)' % normal_bits)
        probs.append('runtime flag bit index > uint16')
    if B.notes.get('encoded_flag_bits', 0) > ENCODED_FLAG_BITS_MAX:
        warn('snd_budget', 'encoded runtime permutation flag bits %d exceed the engine\'s 50,176-bit deterministic sound buffer' %
             B.notes.get('encoded_flag_bits', 0))
        probs.append('encoded flag bits > 50176')
    return probs


def build_bundle(ctx, sels=None, opts=None):
    """Select (or take) the snd! tags, read + transcode their audio and assemble the output gestalt."""
    t0 = time.time()
    if sels is None:
        sels = select_sounds(ctx)
    bd = Builder(ctx, sels, opts)
    B = Bundle()
    B.sources = bd.sources
    bd.analyse(B)
    bd.run_jobs(B)
    bd.assemble(B)
    B.notes.update(sounds=len(B.recs), sounds_failed=sum(1 for r in B.recs if r.error), perms=len(B.perms),
                   pitch_ranges=len(B.pitch_ranges), chunks=len(B.chunk_rows), playbacks=len(B.playbacks),
                   scales=len(B.scales), import_names=len(B.import_names), custom_playbacks=len(B.custom),
                   promotions=len(B.promotions), extra_info=len(B.extras), rt_flag_bytes=B.rt_bytes,
                   seconds=round(time.time() - t0, 1), warnings=len(bd.warnings))
    seen = set()
    total = 0
    for pa, ci, sz in B.chunk_rows:
        if pa.key not in seen:
            seen.add(pa.key)
            total += sum(pa.sizes)
    B.notes['audio_bytes_unique'] = total
    B.notes['audio_payloads_unique'] = len(seen)
    return B


# ------------------------------------------------------------------------------------------------
# bundle -> Vista ugh! Node tree
# ------------------------------------------------------------------------------------------------
def bundle_to_ugh_node(ctx, B, ugh_datum):
    """Build the Vista `ugh!` root node (0x58 bytes, 11 blocks).  Chunk payloads are registered through
    ctx.add_raw('sound', payload) (once per distinct permutation payload) and the chunk-record offset word is a patch;
    lip-sync blobs go through ctx.add_raw('geometry', blob) and patch the extra-info Resource Block Offset.
    Idempotent: tokens are remembered in the bundle."""
    lay = ctx.lay_vista['ugh!']
    root = T.Node('ugh!', '', lay.fields, lay.base_size or lay.computed_size)
    vf = {f.name: f for f in lay.fields}

    def mk(name, data_list):
        f = vf[name]
        es = T.element_size(f)
        cp = T.child_path('', f)
        kids = []
        for d in data_list:
            kids.append(T.Node('ugh!', cp, f.children, es, d))
        root.blocks[f.offset] = kids
        return kids

    mk('Playback Parameters', B.playbacks)
    mk('Scales', B.scales)
    sids = [ctx.sid(t) if t else 0 for t in B.import_names]
    lost = sum(1 for t, sid in zip(B.import_names, sids) if t and not sid)
    if lost:                                   # StringTable overflow answers 0 (24,448 ids)
        ctx.warn('snd_string_overflow', '%d sound import names could not get a string id (string table full)' % lost)
    mk('Import Names', [struct.pack('<I', sid) for sid in sids])
    mk('Pitch Range Parameters', B.pr_params)
    mk('Pitch Ranges', [struct.pack('<3hHhh', p[0], p[1], p[2], p[3] & 0xFFFF, p[4], p[5]) for p in B.pitch_ranges])      # +6 unsigned, -1 -> 0xFFFF
    mk('Permutations', [struct.pack('<HHbbHIHH', imp & 0xFFFF, skip, gain, info, lnt, ssz, fc, cc)
                        for (imp, skip, gain, info, lnt, ssz, fc, cc) in B.perms])
    root.blocks[vf['Custom Playbacks'].offset] = B.custom
    mk('Runtime Permutation Flags', [b'\0'] * B.rt_bytes)
    # chunks: allocate payload tokens in chunk order (== file order, like stock maps)
    kids = mk('Permutation Chunks', [struct.pack('<IIi', 0, sz, -1) for (pa, ci, sz) in B.chunk_rows])
    for node, (pa, ci, sz) in zip(kids, B.chunk_rows):
        if pa.tokens is None:
            pa.tokens = [ctx.add_raw('sound', p) for p in pa.parts]
            pa.parts = None
        node.patches[0] = pa.tokens[ci]
    root.blocks[vf['Promotions'].offset] = B.promotions
    root.blocks[vf['Extra Info'].offset] = [e.node for e in B.extras]
    for e in B.extras:
        struct.pack_into('<I', e.node.data, 0x20, ugh_datum & 0xFFFFFFFF)          # Owner Tag
        if e.blob is not None:
            if e.token is None:
                e.token = ctx.add_raw('geometry', e.blob)
            e.node.patches[0x08] = e.token
        else:
            struct.pack_into('<iI', e.node.data, 8, -1, 0)
    for f in root.fields:                                      # keep the (count) words of the root struct consistent
        if f.type in T.BLOCK_TYPES:
            struct.pack_into('<i', root.data, f.offset, len(root.blocks.get(f.offset, [])))
    return root
