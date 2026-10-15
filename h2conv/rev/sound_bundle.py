# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Sound bundle builder for the REVERSE direction, Vista (v8) -> MCC (v13).

The inverse of `h2conv.sound_bundle`.  It merges the sounds of the plan into ONE MCC gestalt (the H2EK-built MCC maps have exactly one `ugh!`,
header 0x2EC = -1).  Two kinds of sound go into it:

  * 'convert' snd! entries: a Vista snd! tag + its Vista gestalt (the input map's own `ugh!` and the Vista shared maps').  Xbox ADPCM and WMA
    are stored AS-IS (byte-identical in both games) and only re-framed into MCC chunks; raw PCM is re-encoded to Xbox ADPCM.
  * 'mcc_donor' snd! entries: MCC's OWN snd! tag of that name (the planner prefers it for tags that came through Vista's shared.map): its
    pitch ranges / permutations / chunks (Opus or ADPCM, copied byte for byte from the MCC map or from sounds_*.dat), custom playbacks,
    promotions, lip-sync extra info, remastered / reflection / low-pass entries are merged into the same gestalt (MCC -> MCC, nothing is decoded).

Output gestalt tables (MCC `ugh!`, 0x78 root, 15 blocks): codecs, playbacks, scales, import names, pitch-range parameters, pitch ranges, permutations
(with the 9-entry `Localized Chunks Info`), custom playbacks, reflections, lowpass, remastered, runtime permutation flags, the 9 per-language chunk
lists, promotions, extra info.  The reflections / lowpass / remastered blocks hold only what a kept DONOR sound references (empty for pure Vista
conversions); every kept sound's indices point into the merged tables.

Ordering rules are the forward ones: playbacks / scales / import names / pitch-range parameters = used entries only in
SOURCE order per source, de-duplicated by content; custom playbacks / promotions / extra info = first use in output sound order, one element per
distinct SOURCE element; pitch ranges, permutations, chunks = output sound order.  Runtime permutation flags: `h2conv.sound_bundle.encoded_flag_bits`
(two sequences; verified on all 43 stock MCC gestalts + 3 H2EK maps: 29,225 + 30,611 allocations, 0 exceptions).

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

from h2conv import tagtree as T
from h2conv import sound_codec as sc
from h2conv import sound_bundle as fb                      # forward module, read-only: MCC gestalt reader + dat pool + runtime-flag rule
from h2conv.sound_bundle import encoded_flag_bits
from h2conv.sound_gestalt import DatPool, SAMPLE_RATE_HZ, mcc_dat_dir
from . import sound_rules as R
from . import sound_ogg as og                              # pluggable Ogg Vorbis decoder (community maps with Ogg data under a PCM label)
from .sound_rules import NULL
from .sound_vista import VistaGestalt, parse_vista_snd, vista_maps_for, SND_SIZE

WMA_BYTES_PER_SECOND = 44100            # MCC time -> byte arithmetic for compression 4 (stock mainmenu: 481,637 B -> 10,921 ms)
I16_MAX = 32767
MCC_SOUND_ALIGN = 0x80                  # every chunk of the H2EK-built maps starts on a 128-byte boundary, packed back to back (100 % of 3 maps)
MCC_CH = {0: 1, 1: 2, 3: 4}


# ------------------------------------------------------------------------------------------------
# classes
# ------------------------------------------------------------------------------------------------
class RevSource(object):
    """One VISTA map whose snd! tags are converted: its cache and its (own) sound gestalt."""
    side = 'vista'

    def __init__(self, key, cache, gestalt):
        self.key = key
        self.cache = cache
        self.g = gestalt


class MccSource(object):
    """One MCC map that donates snd! tags: its cache, its gestalt (forward McGestalt) and its external .dat pool (forward Source)."""
    side = 'mcc'

    def __init__(self, key, cache, gestalt, fsrc):
        self.key = key
        self.cache = cache
        self.g = gestalt
        self.fsrc = fsrc


class Sel(object):
    """One snd! tag selected for conversion (side 'vista') or donation (side 'mcc')."""
    __slots__ = ('src_key', 'cache', 'tag', 'name', 'out_index', 'out_datum', 'sort_key', 'side')

    def __init__(self, src_key, cache, tag, out_index=None, out_datum=None, sort_key=None, side='vista'):
        self.src_key = src_key
        self.cache = cache
        self.tag = tag
        self.name = tag.name or ''
        self.out_index = out_index
        self.out_datum = out_datum
        self.sort_key = sort_key if sort_key is not None else (out_index if out_index is not None else tag.index)
        self.side = side


class PermAudio(object):
    """One distinct permutation payload, already cut into MCC chunks.  `tokens` are filled by the ugh! builder."""
    __slots__ = ('parts', 'sizes', 'tokens', 'key')

    def __init__(self, parts, key):
        self.parts = parts
        self.sizes = [len(p) for p in parts]
        self.tokens = None
        self.key = key


class PermIn(object):
    """One source permutation, ready to be placed: MCC chunk byte strings, their size|flags dwords and uncompressed sizes."""
    __slots__ = ('src_idx', 'perm', 'kind', 'parts', 'flags', 'uncs', 'ssz')


class SndRec(object):
    def __init__(self, sel, body):
        self.sel = sel
        self.name = sel.name
        self.v_body = body            # the source tag body (20 bytes Vista / 28 bytes MCC)
        self.snd = None
        self.prs = []                 # [(src pitch range dict, [PermIn])]
        self.body = None              # final 28-byte MCC body
        self.codec = None             # (rate, encoding, compression)
        self.max_play_ms = 0
        self.error = None
        self.decoded_ms = 0           # longest permutation (ms) that was DECODED from Ogg Vorbis; 0 when none was
        self.ogg_perms = 0            # permutations of this sound that held Ogg data
        self.total_perms = 0          # permutations of this sound that were analysed


class ExtraInfo(object):
    __slots__ = ('node', 'blob', 'token')

    def __init__(self, node, blob):
        self.node = node
        self.blob = blob
        self.token = None


class Bundle(object):
    def __init__(self):
        self.recs = []
        self.codecs = []              # [(rate, encoding, compression)] sorted
        self.playbacks = []
        self.scales = []
        self.pr_params = []
        self.import_names = []        # texts
        self.custom = []              # MCC Nodes
        self.promotions = []
        self.extras = []
        self.reflections = []         # MCC Nodes (0x10)
        self.lowpass = []             # MCC Nodes (0x10 + Function data)
        self.remastered = []          # output snd! datums
        self.pitch_ranges = []        # (import, params, encoded, runtime_flag_bit, first_perm, perm_count)
        self.perms = []               # (import, skip, gain, info, lnt, sample_size_pcm, first_chunk, chunk_count)
        self.perm_kinds = []          # 'xadpcm' | 'wma' | 'mcc' per permutation (the audio kind the chunk rules depend on)
        self.chunk_rows = []          # (PermAudio, part index, size_flags_u32, unc_bytes)
        self.rt_bytes = 0
        self.by_addr = {}             # tag address -> [SndRec]
        self.by_tag = {}              # (src_key, tag index) -> SndRec
        self.notes = {}
        self.sources = {}
        self.opts = {}


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------
def source_cache(ctx, key):
    """The VistaCache a plan source key names."""
    if key in (None, 'src', 'input'):
        return ctx.src
    refs = getattr(ctx, 'vista', None) or getattr(ctx, 'vista_refs', None) or getattr(ctx, 'vref', None)
    if refs is None:
        return None
    try:
        return {'shared': refs.shared, 'single_player_shared': refs.sp_shared, 'mainmenu': refs.mainmenu}.get(key)
    except Exception:
        return None


def own_gestalt_tag(cache):
    """The sound gestalt the snd! tags LOCAL to Vista map `cache` index: header 0x2E4 (secondary ugh!) when it names a local tag, else the single
    local ugh! (solo maps, mainmenu, shared.map: header -1 and ONE self-contained gestalt)."""
    d = getattr(cache.header, 'secondary_ugh_tag_index', NULL)
    if d not in (NULL, 0) and (d & 0xFFFF) < len(cache.tags):
        t = cache.tags[d & 0xFFFF]
        if t.group == 'ugh!' and t.addr:
            return t
    for t in cache.tags:
        if t.group == 'ugh!' and t.addr:
            return t
    return None


def _datum_from(ctx, src, datum):
    """Source datum -> output (MCC) datum (0xFFFFFFFF when null/dropped).  `src` = the source the node was read from (a RevSource or MccSource);
    None = "the map ctx is converting right now" (ctx.cur_src, what ctx.remap_datum already assumes)."""
    if datum in (NULL, 0, None):
        return NULL
    if src is None:
        return ctx.remap_datum(datum)
    plan = getattr(ctx, 'plan', None)
    if src.side == 'mcc':                                  # a donor tag: MCC datums resolve by (group, name) through the plan
        if plan is not None and hasattr(plan, 'remap_mcc_datum'):
            try:
                r = plan.remap_mcc_datum(src.cache, datum)
            except Exception:
                r = None
            return NULL if r is None else r
        t = src.cache.tag_by_datum(datum)
        if t is None or not t.name:
            return NULL
        return ctx.remap_name(t.group, t.name) if hasattr(ctx, 'remap_name') else NULL
    if plan is not None and hasattr(plan, 'remap_datum'):
        try:
            r = plan.remap_datum(src.key, datum)
        except Exception:
            r = None
        return NULL if r is None else r
    if src.cache is ctx.src:
        return ctx.remap_datum(datum)
    t = src.cache.get_tag(datum)
    if t is None or not t.name:
        return NULL
    for s2 in ctx.src.tags:
        if s2.group == t.group and s2.name == t.name:
            return ctx.remap_datum(s2.datum)
    ctx.warn('snd_ref_unresolved', 'tag reference %s %s of a non-input map has no twin in the input map' % (t.group, t.name))
    return NULL


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
            pos = f.offset + 4 if f.size == 8 else f.offset
            old = struct.unpack_from('<I', d, pos)[0]
            if old != 0:                                       # 0 is not a datum (unused 4-byte references of empty structs): left alone
                struct.pack_into('<I', d, pos, _datum_from(ctx, src, old) if old != NULL else NULL)
        elif f.type == 'stringid':
            struct.pack_into('<I', d, f.offset, _sid_from(ctx, src, struct.unpack_from('<I', d, f.offset)[0]))


def clone_node(ctx, src, v, mfields, mpath, group, size=None):
    """Copy source node `v` into a node of the MCC layout `mfields` (same offsets for the part that is kept; bytes past the end of the source
    struct are zero), remapping tag references and string ids.  `size` = MCC struct size (extra info 0x2C -> 0x14C)."""
    sz = len(v.data) if size is None else size
    raw = bytes(v.data[:sz]).ljust(sz, b'\0')
    n = T.Node(group, mpath, mfields, sz, raw)
    remap_fields(ctx, src, n)
    for f in mfields:
        if f.type in T.BLOCK_TYPES:
            es = T.element_size(f)
            cp = T.child_path(mpath, f)
            n.blocks[f.offset] = [clone_node(ctx, src, k, f.children, cp, group, es) for k in (v.blocks.get(f.offset) or [])]
        elif f.type in T.DATA_TYPES:
            n.datas[f.offset] = bytes(v.datas.get(f.offset) or b'')
    return n


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


def xadpcm_blocks_valid(b, ch):
    """True when `b` is a whole number of Xbox ADPCM blocks with legal headers (step index <= 88, reserved byte 0)."""
    import numpy as np
    bs = R.XADPCM_BLOCK * ch
    if not b or len(b) % bs:
        return False
    a = np.frombuffer(b, dtype=np.uint8).reshape(-1, bs)
    for c in range(ch):
        if (a[:, 4 * c + 2] > 88).any() or (a[:, 4 * c + 3] != 0).any():
            return False
    return True


def _swap16(b):
    import numpy as np
    return np.frombuffer(b, dtype='>i2').astype('<i2').tobytes()


# ------------------------------------------------------------------------------------------------
# selection
# ------------------------------------------------------------------------------------------------
def read_donor_chunk(src, name, fo, size, un, comp, ch, use, lnt, snd_flags, warn=None):
    """One donor chunk.  The forward reader locates the right sounds_*.dat by the documented rule AND a strict framing check of the classic
    Xbox ADPCM / Opus layouts; the remastered quad XADPCM chunks (01a_tutorial, the remastered music tags) do not satisfy that check (their block
    headers carry step index 0xFF), so when no dat validates the chunk it is taken from the dat the rule names, unvalidated."""
    try:
        return src.fsrc.read_chunk(fo, size, un, comp, ch, name, use, lnt, snd_flags)
    except ValueError:
        if not src.fsrc.external:
            raise
        from h2conv.sound_gestalt import mcc_pick_dat
        mm = src.fsrc.dats.get(mcc_pick_dat(name, snd_flags, lnt, use))
        if fo + size > len(mm):
            raise
        if warn is not None:
            warn('snd_chunk_unvalidated', '%s: chunk @%#x+%d did not validate in any sounds_*.dat; taken unvalidated from the dat the rule names'
                 % (name, fo, size))
        return bytes(mm[fo:fo + size])


def donor_key(donor):
    return 'mcc:' + donor.label


def select_sounds(ctx):
    """The snd! tags to convert / merge, in OUTPUT order.
    * With a plan: every entry of group 'snd!' with action 'convert' (a Vista tag) or 'mcc_donor' (MCC's own tag); order = out_index.
    * Without a plan (stub contexts / unit tests): every local snd! of the input with data, in input tag order."""
    sels = []
    plan = getattr(ctx, 'plan', None)
    if plan is not None:
        for e in plan.entries:
            if e.group != 'snd!':
                continue
            if e.action == 'convert':
                key = getattr(e, 'src_map', None) or 'src'
                cache = source_cache(ctx, key)
                if cache is None:
                    ctx.warn('snd_no_source', 'snd! %s: source map %r is not available' % (e.name, key))
                    continue
                tag = cache.tags[e.src_index]
                if not tag.addr:
                    ctx.warn('snd_no_data', 'snd! %s has no tag data in %s' % (e.name, key))
                    continue
                sels.append(Sel(key, cache, tag, e.out_index, getattr(e, 'out_datum', None)))
            elif e.action == 'mcc_donor':
                try:
                    cache, tag = plan.donor_source_of(e)
                except Exception as ex:                                  # noqa: BLE001
                    ctx.warn('snd_no_source', 'snd! donor %s: %r' % (e.name, ex))
                    continue
                if not tag.has_data:
                    ctx.warn('snd_no_data', 'snd! donor %s has no tag data in %s' % (e.name, e.donor.label))
                    continue
                sels.append(Sel(donor_key(e.donor), cache, tag, e.out_index, getattr(e, 'out_datum', None), side='mcc'))
        sels.sort(key=lambda s: s.sort_key)
        return sels
    for t in ctx.src.tags:
        if t.group == 'snd!' and t.addr:
            sels.append(Sel('src', ctx.src, t, None, None, t.index))
    sels.sort(key=lambda s: s.sort_key)
    return sels


# ------------------------------------------------------------------------------------------------
# the builder
# ------------------------------------------------------------------------------------------------
class Builder(object):
    def __init__(self, ctx, sels, opts=None):
        self.ctx = ctx
        self.sels = sels
        self.opts = dict(getattr(ctx, 'opts', None) or {})
        if opts:
            self.opts.update(opts)
        self.lang = int(self.opts.get('sound_language', 0))
        self.sources = {}
        self.warnings = []
        self.opener = vista_maps_for(ctx)
        self.mlay = ctx.lay_mcc['ugh!']
        self.vlay = ctx.lay_vista['ugh!']
        self.mf = {f.name: f for f in self.mlay.fields}
        # --mcc-maps may point at a folder of decompressed maps without the .dat files: then the installed game's folder is used
        self.dat_dir = mcc_dat_dir(self.opts.get('mcc_dat_dir'))
        self._dats = None
        self._ogg_cache = {}          # (md5 of the Ogg payload, channels, Hz) -> og.OggResult
        self._ogg_no_decoder_warned = False

    # ---- sources --------------------------------------------------------------------------------
    def source(self, sel):
        s = self.sources.get(sel.src_key)
        if s is not None:
            return s
        if sel.side == 'mcc':
            cache = sel.cache
            tag = cache.sound_gestalt
            if tag is None or not tag.has_data:
                raise RuntimeError('MCC donor map %s has no sound gestalt (ugh!) tag with data' % sel.src_key)
            external = bool(cache.header['flags'] & 0x08)
            if external and self._dats is None:
                self._dats = DatPool(self.dat_dir)
            g = fb.McGestalt(cache, tag, self.mlay)
            s = MccSource(sel.src_key, cache, g, fb.Source(sel.src_key, cache, g, self._dats if external else None, external))
        else:
            cache = sel.cache
            tag = own_gestalt_tag(cache) if cache is not None else None
            if tag is None:
                raise RuntimeError('source map %s has no sound gestalt (ugh!) tag with data' % sel.src_key)
            s = RevSource(sel.src_key, cache, VistaGestalt(cache, tag, self.vlay, maps={0: cache}, opener=self.opener))
        self.sources[sel.src_key] = s
        return s

    def warn(self, code, msg, tag=None):
        self.warnings.append((code, msg))
        self.ctx.warn(code, msg, tag)

    # ---- phase A: analysis ----------------------------------------------------------------------
    def analyse(self, B):
        for sel in self.sels:
            if sel.src_key in self.sources:
                src = self.sources[sel.src_key]
            else:
                try:
                    src = self.source(sel)
                except Exception as e:                            # noqa: BLE001
                    rec = SndRec(sel, b'')
                    rec.snd = dict(flags=0, cls=0, rate=1, encoding=0, compression=1, playback=-1, pitch_range=-1, pitch_range_count=0, scale=-1,
                                   promotion=-1, custom_playback=-1, extra_info=-1, max_play=0)
                    rec.error = '%s: %s' % (type(e).__name__, e)
                    B.recs.append(rec)
                    self.warn('snd_failed', 'snd! %s skipped (%s)' % (rec.name, rec.error))
                    continue
            if sel.side == 'mcc':
                body = sel.cache.read_at_addr(sel.tag.addr, R.MCC_SND_SIZE)
                rec = SndRec(sel, body)
                rec.snd = src.g.parse_snd(body)
                rec.snd.update(refl_first=struct.unpack_from('<h', body, 0x18)[0], refl_count=body[0x1A], lowpass=body[0x1B])
            else:
                body = sel.cache.read_addr(sel.tag.addr, SND_SIZE)
                rec = SndRec(sel, body)
                rec.snd = parse_vista_snd(body)
            B.recs.append(rec)
            B.by_tag[(sel.src_key, sel.tag.index)] = rec
            B.by_addr.setdefault(sel.tag.addr, []).append(rec)
            try:
                if sel.side == 'mcc':
                    self._analyse_mcc(src, rec)
                else:
                    self._analyse_one(src, rec)
            except Exception as e:                            # a broken sound must not kill the whole map
                rec.error = '%s: %s' % (type(e).__name__, e)
                rec.prs = []
                self.warn('snd_failed', 'snd! %s skipped (%s)' % (rec.name, rec.error))

    # ---- Vista side -------------------------------------------------------------------------------
    def _kind_of(self, s):
        """Audio kind of a Vista sound: 'wma' | 'xadpcm' | 'pcm'.  Vista 'codec' encoding (2) is the WMA blob regardless of its compression
        label (menu_music is labelled XADPCM, the 479 shared announcers WMA).  Compression 2 ("IMA ADPCM") is Xbox ADPCM in the one community
        sound that uses it (salvation.map fusion_coil_destroyed: 36-byte blocks, step index <= 88, reserved bytes 0) - checked per permutation."""
        if s['encoding'] == 2 or s['compression'] == 4:
            return 'wma'
        comp = s['compression']
        if comp in (1, 2):
            return 'xadpcm'
        if comp in (0, 3):
            return 'pcm'
        raise NotImplementedError('Vista compression %d' % comp)

    def _vista_rows(self, payload, kind, ch, v_sizes, v_f31):
        """MCC chunk parts / size|flags dwords / uncompressed sizes of one Vista permutation payload."""
        ck = 'wma' if kind == 'wma' else 'xadpcm'
        sizes = R.mcc_split_chunks(len(payload), ck)
        parts, pos = [], 0
        for sz in sizes:
            parts.append(payload[pos:pos + sz])
            pos += sz
        f31 = R.f31_parts(v_sizes, v_f31, sizes)
        uncs = [R.pcm_bytes_of(sz, 'xadpcm' if kind == 'xadpcm' else 'raw', ch) for sz in sizes]
        flags = [sz | (R.MCC_LAST_BIT if ci == len(sizes) - 1 else 0) | (R.MCC_F31_BIT if f31[ci] else 0) for ci, sz in enumerate(sizes)]
        return parts, flags, uncs

    def _analyse_one(self, src, rec):
        g = src.g
        s = rec.snd
        kind = self._kind_of(s)
        if kind == 'wma':
            ch = 2 if s['encoding'] == 1 else 1             # irrelevant for WMA (the blob carries its own format), kept for the record
        else:
            if s['encoding'] not in R.CHANNELS:
                raise ValueError('encoding %d' % s['encoding'])
            ch = R.CHANNELS[s['encoding']]
        rate = s['rate'] if s['rate'] in (0, 1, 2) else 1
        rec.codec = (rate, 2, 4) if kind == 'wma' else (rate, s['encoding'], 1)         # PCM is re-encoded to Xbox ADPCM below
        if s['pitch_range'] < 0 or s['pitch_range_count'] <= 0:
            return
        for k in range(s['pitch_range_count']):
            pr = g.pitch_range(s['pitch_range'] + k)
            perms = []
            for j in range(pr['perm_count']):
                pidx = pr['first_perm'] + j
                perm = g.permutation(pidx)
                if not perm['chunk_count']:
                    self.warn('snd_perm_empty', '%s: permutation %d has no chunks' % (rec.name, pidx))
                    continue
                chunks = g.perm_chunks(perm)
                pi = PermIn()
                pi.src_idx = pidx
                pi.perm = perm
                pi.kind = kind
                v_sizes = [size for _, size, _ in chunks]
                v_f31 = [f for _, _, f in chunks]
                payload = b''.join(p for p, _, _ in chunks)
                rec.total_perms += 1
                if payload[:4] == b'OggS':                                   # Ogg Vorbis data under a PCM (or ADPCM) label: decode, then encode like any PCM
                    if kind == 'wma':
                        raise ValueError('permutation %d holds Ogg data under a WMA / codec label (not supported)' % pidx)
                    res = self._ogg_perm(rec, pidx, payload, ch, rate)
                    payload = res.payload
                    kind = 'xadpcm'
                    pi.kind = kind
                    v_sizes = [len(payload)]
                    v_f31 = [False]                                          # the F31 hint belongs to the original encode, not to this one
                    rec.ogg_perms += 1
                    rec.decoded_ms = max(rec.decoded_ms, res.duration_ms)
                elif kind == 'xadpcm':
                    if s['compression'] == 2 and not xadpcm_blocks_valid(payload, ch):
                        raise ValueError('compression-2 (IMA) payload of permutation %d is not Xbox ADPCM' % pidx)
                    if len(payload) % (R.XADPCM_BLOCK * ch):
                        self.warn('snd_adpcm_len', '%s: XADPCM payload %d not a multiple of %d' % (rec.name, len(payload), R.XADPCM_BLOCK * ch))
                    if sum(v_sizes) != perm['sample_size']:
                        self.warn('snd_sample_size', '%s: permutation %d sample size %d != chunk sum %d'
                                  % (rec.name, pidx, perm['sample_size'], sum(v_sizes)))
                elif kind == 'wma':
                    if payload[:4] != b'auds':
                        self.warn('snd_wma_head', '%s: permutation %d is a codec-encoded sound but its blob does not start with "auds"' % (rec.name, pidx))
                else:                                                        # raw PCM -> Xbox ADPCM (the classic MCC path); synthetic tests only, no community sound
                    raw = _swap16(payload) if s['compression'] == 0 else payload
                    payload = sc.pcm_le_to_vista_xadpcm(raw, ch, SAMPLE_RATE_HZ.get(rate, 44100), SAMPLE_RATE_HZ.get(rate, 44100))
                    kind = 'xadpcm'
                    pi.kind = kind
                    v_sizes = [len(payload)]
                    v_f31 = [False]                                          # the F31 hint belongs to the original encode, not to this one
                pi.parts, pi.flags, pi.uncs = self._vista_rows(payload, kind, ch, v_sizes, v_f31)
                pi.ssz = sum(pi.uncs)
                perms.append(pi)
                kind = self._kind_of(s)                                      # a PCM permutation does not change the kind of the next one
            rec.prs.append((pr, perms))

    def _ogg_perm(self, rec, pidx, payload, ch, rate):
        """One permutation whose payload is an Ogg stream -> og.OggResult (Xbox ADPCM payload at the sound's labelled rate / channel count).
        Raises ValueError (-> the sound becomes silent, `snd_failed`) when no decoder is available or the stream cannot be decoded; the missing
        decoder is reported ONCE per conversion with the code `snd_ogg_no_decoder` ('Ogg Vorbis under a PCM label')."""
        hz = SAMPLE_RATE_HZ.get(rate, 44100)
        key = (hashlib.md5(payload).digest(), ch, hz)
        res = self._ogg_cache.get(key)
        if res is None:
            try:
                dec = og.get_decoder(self.opts)
                ok, why = dec.availability()
                if not ok:
                    raise og.OggDecoderUnavailable(why)
                res = og.ogg_to_xadpcm(payload, ch, hz, dec)
            except og.OggDecoderUnavailable as e:
                if not self._ogg_no_decoder_warned:
                    self._ogg_no_decoder_warned = True
                    self.warn('snd_ogg_no_decoder', 'Ogg Vorbis data (e.g. snd! %s) cannot be decoded and the affected sounds are converted SILENT: %s' % (rec.name, e))
                raise ValueError('permutation %d holds Ogg Vorbis data and no Ogg decoder is available' % pidx)
            except og.OggError as e:
                raise ValueError('permutation %d holds Ogg data that could not be decoded: %s' % (pidx, e))
            self._ogg_cache[key] = res
            if res.mismatch:
                self.warn('snd_ogg_format', '%s: permutation %d is %s %d Hz x %d ch but the sound is labelled %d Hz x %d ch; converted to the label'
                          % (rec.name, pidx, res.codec, res.src_rate, res.src_channels, hz, ch))
            if res.granule is not None and res.native_frames != res.granule:
                self.warn('snd_ogg_length', '%s: permutation %d decoded to %d samples but the Ogg stream says %d' % (rec.name, pidx, res.native_frames, res.granule))
        self.warn('snd_ogg_decoded', '%s: permutation %d decoded from %s (%d Hz x %d ch, %d samples, %d ms) and encoded as Xbox ADPCM (%d B)'
                  % (rec.name, pidx, res.codec, res.src_rate, res.src_channels, res.native_frames, res.duration_ms, len(res.payload)))
        return res

    # ---- MCC donor side ---------------------------------------------------------------------------
    def _analyse_mcc(self, src, rec):
        """A donated MCC sound: chunks are copied byte for byte (from the map or sounds_*.dat), chunk framing / flags / PCM sizes kept."""
        g = src.g
        s = rec.snd
        rec.codec = (s['rate'], s['encoding'], s['compression'])
        comp = s['compression']
        ch = MCC_CH.get(s['encoding'], 1)
        if s['pitch_range'] < 0 or s['pitch_range_count'] <= 0:
            return
        for k in range(s['pitch_range_count']):
            pr = g.pitch_range(s['pitch_range'] + k)
            perms = []
            for j in range(pr['perm_count']):
                pidx = pr['first_perm'] + j
                perm = g.permutation(pidx)
                langs = perm['langs']
                use = self.lang if (self.lang < len(langs) and langs[self.lang][2]) else next((i for i, l in enumerate(langs) if l[2]), None)
                if use is None:
                    self.warn('snd_perm_empty', '%s: permutation %d has no chunks' % (rec.name, pidx))
                    continue
                ssz, first, cnt = langs[use]
                pi = PermIn()
                pi.src_idx = pidx
                pi.perm = dict(import_name=perm['import_name'], skip=perm['skip'], gain=perm['gain'], info=perm['info'], lnt=perm['lnt'])
                pi.kind = 'mcc'
                parts, flags, uncs = [], [], []
                for c in range(first, first + cnt):
                    fo, size, fl, un, rt = g.chunk(c, use)
                    parts.append(self._read_donor_chunk(src, rec, fo, size, un, comp, ch, use, perm['lnt'], s['flags']))
                    flags.append((size & R.MCC_SIZE_MASK) | ((fl & 5) << 29))
                    uncs.append(un)
                pi.parts, pi.flags, pi.uncs, pi.ssz = parts, flags, uncs, ssz
                perms.append(pi)
            rec.prs.append((pr, perms))

    def _read_donor_chunk(self, src, rec, fo, size, un, comp, ch, use, lnt, snd_flags):
        return read_donor_chunk(src, rec.name, fo, size, un, comp, ch, use, lnt, snd_flags, self.warn)

    # ---- phase C: assembly ----------------------------------------------------------------------
    def assemble(self, B):
        ctx = self.ctx
        mf = self.mf
        recs = [r for r in B.recs if r.error is None]
        # ---- codec table: sorted unique (rate, encoding, compression) of the kept sounds  (verified on 4 stock maps + 3 H2EK maps);
        # failed sounds get a silent body that uses 44k mono XADPCM
        triples = {r.codec for r in recs}
        if len(recs) != len(B.recs):
            triples.add((1, 0, 1))
        codecs = sorted(triples)
        B.codecs = codecs
        codec_index = {c: i for i, c in enumerate(codecs)}
        # ---- usage marking per source (for the source-ordered tables)
        used = {}
        for r in recs:
            u = used.setdefault(r.sel.src_key, dict(playbacks=set(), scales=set(), import_names=set(), pr_params=set()))
            g_ = self.sources[r.sel.src_key].g
            lim_ = {k: g_.count(k) for k in ('playbacks', 'scales', 'import_names', 'pitch_range_params')}
            if 0 <= r.snd['playback'] < lim_['playbacks']:
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
        promo_map, extra_map, cust_map = {}, {}, {}
        refl_map, low_map = {}, {}
        rem_t = _Table()
        audio_by_key = {}
        blob_by_key = {}
        rt_bit = 0                    # normal sequence: ceil(n/8)*8 bits per pitch range; the array holds exactly these bits
        enc_bit = 0                   # 'encoded' (lip-sync dialogue) sequence, NOT in the array

        def cust_index(src, i):
            k = (src.key, i)
            if k not in cust_map:
                vn = src.g.node('custom_playbacks', i)
                cust_map[k] = len(B.custom)          # one element per distinct SOURCE element (the Vista tool does not merge equal ones either)
                B.custom.append(clone_node(ctx, src, vn, mf['Custom Playbacks'].children, T.child_path('', mf['Custom Playbacks']), 'ugh!'))
            return cust_map[k]

        def promo_index(src, i):
            k = (src.key, i)
            if k not in promo_map:
                vn = src.g.node('promotions', i)
                promo_map[k] = len(B.promotions)
                B.promotions.append(clone_node(ctx, src, vn, mf['Promotions'].children, T.child_path('', mf['Promotions']), 'ugh!'))
            return promo_map[k]

        def extra_index(src, i):
            k = (src.key, i)
            if k not in extra_map:
                vn = src.g.node('extra_info', i)
                if src.side == 'mcc':
                    # MCC Extra Info = encoded section + EIGHT resource groups (one lip-sync blkh per language): keep the converted language's group
                    # (first non-empty fallback) as group 0, like the Vista element has it
                    lg = self.lang if 0 <= self.lang < 8 and any(vn.data[8 + 0x24 * self.lang:12 + 0x24 * self.lang]) else \
                        next((x for x in range(8) if any(vn.data[8 + 0x24 * x:12 + 0x24 * x])), 0)
                    if lg:
                        vn.data[8:0x2C] = vn.data[8 + 0x24 * lg:0x2C + 0x24 * lg]
                        vn.blocks[0x18] = vn.blocks.get(0x18 + 0x24 * lg) or []
                    vn.data[0x2C:] = b'\0' * (len(vn.data) - 0x2C)
                    for gi in range(1, 8):
                        vn.blocks[0x18 + 0x24 * gi] = []
                struct.pack_into('<I', vn.data, 0x20, NULL)         # Owner Tag: set to the new ugh! datum later
                mn = clone_node(ctx, src, vn, mf['Extra Info'].children, T.child_path('', mf['Extra Info']), 'ugh!', R.MCC_EXTRA_SIZE)
                roff, rsize = struct.unpack_from('<II', vn.data, 8)
                blob = None
                if rsize:
                    try:
                        blob = src.g.read_raw(roff, rsize) if src.side == 'vista' else src.fsrc.read_blob(roff, rsize)
                        if blob[:4] != b'hklb' or blob[-4:] != b'fklb':
                            raise ValueError('not a blkh block')
                    except Exception as e:
                        blob = None
                        self.warn('snd_extra_blob', 'extra info %d of %s: lip-sync block @%#x+%#x unreadable (%s)' % (i, src.key, roff, rsize, e))
                if blob is not None:
                    blob = blob_by_key.setdefault(hashlib.md5(blob).digest(), blob)
                extra_map[k] = len(B.extras)
                B.extras.append(ExtraInfo(mn, blob))
            return extra_map[k]

        def remastered_index(src, idx, name):
            """Output index of the donor's remastered twin reference; -1 when the twin is not part of the plan."""
            if idx < 0:
                return -1
            cnt, off = src.g.blocks['remastered']
            if idx >= cnt or off is None:
                self.warn('snd_bad_index', '%s: remastered index %d is outside the MCC table' % (name, idx))
                return -1
            grp, dat = struct.unpack_from('<II', src.cache.mm, off + idx * 8)
            out = _datum_from(ctx, src, dat)
            if out == NULL:
                return -1
            return rem_t.add(out)

        def reflection_range(src, first, count, name):
            if count <= 0 or first < 0:
                return -1, 0
            k = (src.key, first, count)
            if k in refl_map:
                return refl_map[k]
            cnt, off = src.g.blocks['reflections']
            rf = mf['Reflections']
            kept = []
            if off is None or first + count > cnt:
                self.warn('snd_bad_index', '%s: reflections %d+%d outside the MCC table (%d)' % (name, first, count, cnt))
            else:
                for q in range(first, first + count):
                    raw = bytearray(src.cache.mm[off + q * 0x10: off + (q + 1) * 0x10])
                    n = T.Node('ugh!', T.child_path('', rf), rf.children, 0x10, bytes(raw))
                    remap_fields(ctx, src, n)
                    if struct.unpack_from('<I', n.data, 8)[0] == NULL:                 # the reflection sound is not part of the plan: the entry is useless
                        continue
                    struct.pack_into('<I', n.data, 0xC, 0)
                    kept.append(n)
            if not kept:
                refl_map[k] = (-1, 0)
            else:
                refl_map[k] = (len(B.reflections), len(kept))
                B.reflections.extend(kept)
            return refl_map[k]

        def lowpass_index(src, idx, name):
            if idx == 0xFF:
                return 0xFF
            k = (src.key, idx)
            if k not in low_map:
                cnt, off = src.g.blocks['lowpass']
                lf = mf['Lowpass Cut Off Parameters']
                if off is None or idx >= cnt:
                    self.warn('snd_bad_index', '%s: low pass index %d is outside the MCC table (%d)' % (name, idx, cnt))
                    low_map[k] = 0xFF
                else:
                    raw = bytes(src.cache.mm[off + idx * 0x10: off + (idx + 1) * 0x10])
                    n = T.Node('ugh!', T.child_path('', lf), lf.children, 0x10, raw)
                    dsz, daddr = struct.unpack_from('<iI', raw, 8)
                    n.datas[8] = src.cache.read_at_addr(daddr, dsz) if dsz > 0 and daddr else b''
                    low_map[k] = len(B.lowpass)
                    B.lowpass.append(n)
            return low_map[k]

        n_xadpcm = n_wma = n_pcm = n_mcc = 0
        for r in B.recs:
            if r.error is not None:
                continue
            src = self.sources[r.sel.src_key]
            m = maps[r.sel.src_key]
            s = r.snd
            is_mcc = src.side == 'mcc'
            new_pr_first = len(B.pitch_ranges)
            n_new_pr = 0
            has_audio = False
            wma_max_bytes = 0
            for pr, perms in r.prs:
                first_perm_new = len(B.perms)
                nperm = 0
                for pi in perms:
                    payload_key = hashlib.md5(b''.join(pi.parts)).digest()
                    ak = (payload_key, tuple(len(p) for p in pi.parts))
                    pa = audio_by_key.get(ak)
                    if pa is None:
                        pa = audio_by_key[ak] = PermAudio(pi.parts, ak)
                    first_chunk = len(B.chunk_rows)
                    for ci in range(len(pi.parts)):
                        B.chunk_rows.append((pa, ci, pi.flags[ci], pi.uncs[ci]))
                    B.perms.append((m['import_names'].get(pi.perm['import_name'], -1), pi.perm['skip'], pi.perm['gain'],
                                    pi.perm['info'], pi.perm['lnt'], pi.ssz, first_chunk, len(pi.parts)))
                    B.perm_kinds.append(pi.kind)
                    nperm += 1
                    has_audio = True
                    if pi.kind == 'wma':
                        wma_max_bytes = max(wma_max_bytes, sum(len(p) for p in pi.parts))
                        n_wma += 1
                    elif pi.kind == 'xadpcm':
                        n_xadpcm += 1
                    elif pi.kind == 'mcc':
                        n_mcc += 1
                    else:
                        n_pcm += 1
                    pi.parts = None
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
            maxplay = s['max_play']
            if wma_max_bytes and not is_mcc and s['compression'] != 4:
                # Vista labels the WMA music blob 'Xbox ADPCM' (comp 1) and times it at 24,806.25 B/s; MCC labels it comp 4 and
                # times it at 44,100 B/s (stock mainmenu: 481,637 B -> 10,921 ms, loop 2,413,762 B -> 54,733 ms).  The Vista announcers that really
                # are comp 4 (shared.map, 479 of them) carry their true duration (round_over 1,253 ms for a 22 KB blob) - copied.
                maxplay = wma_max_bytes * 1000 // WMA_BYTES_PER_SECOND
            if r.decoded_ms and not is_mcc:
                # Ogg Vorbis under a PCM label: the Vista tool priced the FILE BYTES as PCM (grounded(OLD) 'ready': 5,236 B -> 118 ms for a 0.73 s clip).
                # The decoded audio is what MCC plays, so the Maximum Play Time is its real length: floor of the longest permutation;
                # a sound that mixes Ogg and other permutations keeps the larger of the two values.
                maxplay = r.decoded_ms if r.ogg_perms == r.total_perms else max(r.decoded_ms, maxplay)
            has_pr =s['pitch_range'] >= 0 and n_new_pr > 0 and has_audio
            pb_new = m['playbacks'].get(s['playback'], -1) if s['playback'] >= 0 else -1
            sc_new = m['scales'].get(s['scale'], -1) if s['scale'] >= 0 else -1

            def safe_index(fn, idx, what):
                try:
                    return fn(src, idx) if idx >= 0 else -1
                except IndexError:                                 # index outside the source table (corrupt tag): the sound simply lacks it
                    self.warn('snd_bad_index', '%s: %s index %d is outside the table of %s' % (r.name, what, idx, src.key))
                    return -1
            cu_new = safe_index(cust_index, s['custom_playback'], 'custom playback')
            pr_new = safe_index(promo_index, s['promotion'], 'promotion')
            ex_new = safe_index(extra_index, s['extra_info'], 'extra info')
            if is_mcc:
                rem_new = remastered_index(src, s['remastered_index'], r.name)
                rf_new, rc_new = reflection_range(src, s['refl_first'], s['refl_count'], r.name)
                lp_new = lowpass_index(src, s['lowpass'], r.name)
                r.body = R.mcc_snd_body(s['flags'], s['cls'], codec_index[r.codec], rem_new, pb_new, new_pr_first if has_pr else -1,
                                        n_new_pr if has_pr else 0, sc_new, pr_new, cu_new, ex_new, maxplay,
                                        inner_silence=s['inner_silence'], refl_first=rf_new, refl_count=rc_new, lowpass=lp_new)
            else:
                if s['flags'] & 0xC000:
                    self.warn('snd_flags', '%s: Vista snd! flag bits 14/15 (%#x) have no MCC counterpart and are dropped' % (r.name, s['flags'] & 0xC000))
                r.body = R.mcc_snd_body(R.vista_flags_to_mcc(s['flags']), s['cls'], codec_index[r.codec], -1, pb_new,
                                        new_pr_first if has_pr else -1, n_new_pr if has_pr else 0, sc_new, pr_new, cu_new, ex_new, maxplay)
            r.max_play_ms = maxplay
        B.rt_bytes = rt_bit // 8
        B.remastered = rem_t.items
        B.notes['encoded_flag_bits'] = enc_bit
        B.notes.update(perms_xadpcm=n_xadpcm, perms_wma=n_wma, perms_pcm=n_pcm, perms_mcc_donor=n_mcc,
                       sounds_mcc_donor=sum(1 for r in B.recs if r.sel.side == 'mcc'),
                       perms_ogg_decoded=sum(r.ogg_perms for r in B.recs if r.error is None),
                       sounds_ogg_decoded=sum(1 for r in B.recs if r.error is None and r.ogg_perms))
        for r in B.recs:                                       # failed sounds get a silent, valid body (codec 44k mono XADPCM)
            if r.body is None:
                s = r.snd
                fl = s['flags'] if r.sel.side == 'mcc' else R.vista_flags_to_mcc(s['flags'])
                r.body = R.mcc_snd_body(fl, s['cls'], codec_index[(1, 0, 1)], -1, -1, -1, 0, -1, -1, -1, -1, 0)
        B.notes['budget_problems'] = check_budgets(ctx, B, self.warn)


def check_budgets(ctx, B, warn):
    """Index limits of the MCC gestalt arrays (int16 indices, int8 scale / promotion / custom playback, u16 first chunk)."""
    lim = (('pitch_ranges', len(B.pitch_ranges), 32767), ('permutations', len(B.perms), 32767),
           ('chunks', len(B.chunk_rows), 65535), ('import_names', len(B.import_names), 32767),
           ('playbacks', len(B.playbacks), 32767), ('scales', len(B.scales), 127), ('custom_playbacks', len(B.custom), 127),
           ('promotions', len(B.promotions), 127), ('extra_info', len(B.extras), 32767), ('codecs', len(B.codecs), 255),
           ('remastered', len(B.remastered), 32767), ('reflections', len(B.reflections), 32767), ('lowpass', len(B.lowpass), 254))
    probs = []
    for nm, n, mx in lim:
        if n > mx:
            probs.append('%s: %d > %d' % (nm, n, mx))
    for pr in probs:
        warn('snd_budget', 'sound gestalt budget exceeded: ' + pr)
    if max([p[3] for p in B.pitch_ranges] + [0]) > I16_MAX or B.notes.get('encoded_flag_bits', 0) > I16_MAX:
        warn('snd_budget', 'runtime permutation flag bit index exceeds int16')
        probs.append('runtime flag bit index > int16')
    return probs


def build_bundle(ctx, sels=None, opts=None):
    """Select (or take) the snd! tags, read their audio and assemble the MCC gestalt."""
    t0 = time.time()
    if sels is None:
        sels = select_sounds(ctx)
    bd = Builder(ctx, sels, opts)
    B = Bundle()
    B.sources = bd.sources
    bd.analyse(B)
    bd.assemble(B)
    B.notes.update(sounds=len(B.recs), sounds_failed=sum(1 for r in B.recs if r.error), perms=len(B.perms),
                   pitch_ranges=len(B.pitch_ranges), chunks=len(B.chunk_rows), playbacks=len(B.playbacks),
                   scales=len(B.scales), import_names=len(B.import_names), custom_playbacks=len(B.custom),
                   promotions=len(B.promotions), extra_info=len(B.extras), rt_flag_bytes=B.rt_bytes,
                   codecs=len(B.codecs), reflections=len(B.reflections), lowpass=len(B.lowpass), remastered=len(B.remastered),
                   seconds=round(time.time() - t0, 1), warnings=len(bd.warnings))
    seen = set()
    total = 0
    for pa, ci, fl, unc in B.chunk_rows:
        if pa.key not in seen:
            seen.add(pa.key)
            total += sum(pa.sizes)
    B.notes['audio_bytes_unique'] = total
    B.notes['audio_payloads_unique'] = len(seen)
    B.opts = bd.opts
    return B


# ------------------------------------------------------------------------------------------------
# bundle -> MCC ugh! Node tree
# ------------------------------------------------------------------------------------------------
def add_sound_raw(ctx, payload):
    """Register one MCC sound chunk payload with the driver.  The H2EK-built MCC maps keep their sound chunks as ONE run that starts right after
    the 0x380 header, packed back to back with every chunk start 128-aligned (100 % of 7,835 / 8,175 / 7,712 chunks
    of 3 maps), so the alignment is passed when the driver's add_raw accepts it."""
    try:
        return ctx.add_raw('sound', payload, MCC_SOUND_ALIGN)
    except TypeError:
        return ctx.add_raw('sound', payload)


def sync_counts(root):
    """Keep the (count) / (size) words of every struct in the tree consistent with its blocks / datarefs (the encoder rewrites them anyway;
    this makes the Node tree comparable with a decoded one)."""
    for n in root.iter_nodes():
        for f in n.fields:
            if f.type in T.BLOCK_TYPES:
                struct.pack_into('<i', n.data, f.offset, len(n.blocks.get(f.offset) or []))
            elif f.type in T.DATA_TYPES:
                struct.pack_into('<i', n.data, f.offset, len(n.datas.get(f.offset) or b''))


def _copy_node(n):
    """Deep copy of a node tree (patches share their tokens)."""
    c = T.Node(n.group, n.path, n.fields, len(n.data), bytes(n.data))
    c.blocks = {off: [_copy_node(k) for k in kids] for off, kids in n.blocks.items()}
    c.datas = dict(n.datas)
    c.patches = dict(n.patches)
    c.src_addr = n.src_addr
    return c


def bundle_to_ugh_node(ctx, B, ugh_datum, slots=None):
    """Build the MCC `ugh!` root node (0x78 bytes, 15 blocks).  Chunk payloads are registered through ctx.add_raw('sound', payload)
    (once per distinct permutation payload and MCC chunk) and the chunk-record offset word is a patch; lip-sync blobs go through
    ctx.add_raw('geometry', blob) and patch the extra-info Resource Block Offset.  Idempotent: tokens are remembered in the bundle.
    `slots` = number of language slots whose chunk lists (and lip-sync groups) are filled (opts 'sound_language_slots'; default 8 like the stock
    maps: slots 0..7 hold identical lists, slot 8 is empty; the H2EK-built maps fill slot 0 only)."""
    if slots is None:
        slots = int((getattr(B, 'opts', None) or ctx.opts or {}).get('sound_language_slots', 8))
    slots = max(1, min(slots, R.LANG_SLOTS - 1))
    lay = ctx.lay_mcc['ugh!']
    root = T.Node('ugh!', '', lay.fields, lay.base_size or lay.computed_size)
    vf = {f.name: f for f in lay.fields}

    def mk(name, data_list):
        f = vf[name]
        es = T.element_size(f)
        cp = T.child_path('', f)
        kids = [T.Node('ugh!', cp, f.children, es, d) for d in data_list]
        root.blocks[f.offset] = kids
        return kids

    mk('Codecs', [bytes(c) for c in B.codecs])
    mk('Playback Parameters', B.playbacks)
    mk('Scales', B.scales)
    sids = [ctx.sid(t) if t else 0 for t in B.import_names]
    lost = sum(1 for t, sid in zip(B.import_names, sids) if t and not sid)
    if lost:
        ctx.warn('snd_string_overflow', '%d sound import names could not get a string id (string table full)' % lost)
    mk('Import Names', [struct.pack('<I', sid) for sid in sids])
    mk('Pitch Range Parameters', B.pr_params)
    mk('Pitch Ranges', [struct.pack('<6h', *p) for p in B.pitch_ranges])
    pf = vf['Permutations']
    lf = [c for c in pf.children if c.name == 'Localized Chunks Info'][0]
    pkids = mk('Permutations', [struct.pack('<hHbbH', imp, skip, gain, info, lnt) + b'\0' * 8
                                for (imp, skip, gain, info, lnt, ssz, fc, cc) in B.perms])
    lpath = T.child_path(T.child_path('', pf), lf)
    for node, (imp, skip, gain, info, lnt, ssz, fc, cc) in zip(pkids, B.perms):
        node.blocks[lf.offset] = [T.Node('ugh!', lpath, lf.children, 8,
                                         struct.pack('<IHH', ssz, fc, cc) if L < slots else b'\0' * 8) for L in range(R.LANG_SLOTS)]
    root.blocks[vf['Custom Playbacks'].offset] = B.custom
    root.blocks[vf['Reflections'].offset] = B.reflections
    root.blocks[vf['Lowpass Cut Off Parameters'].offset] = B.lowpass
    mk('Remastered Sound', [struct.pack('<II', R_SND_GROUP, d) for d in B.remastered])
    mk('Runtime Permutation Flags', [b'\0'] * B.rt_bytes)
    # chunks: allocate payload tokens in chunk order (== file order, like stock maps), one 9-slot block of 16-byte records
    cf = vf['Permutation Chunks']
    chf = [c for c in cf.children if c.name == 'Languages'][0]
    slot_nodes = mk('Permutation Chunks', [b'\0' * 8] * R.LANG_SLOTS)
    cpath = T.child_path(T.child_path('', cf), chf)
    for L, sn in enumerate(slot_nodes):
        kids = []
        if L < slots:
            for (pa, ci, fl, unc) in B.chunk_rows:
                if pa.tokens is None:
                    pa.tokens = [add_sound_raw(ctx, p) for p in pa.parts]
                    pa.parts = None
                k = T.Node('ugh!', cpath, chf.children, 16, struct.pack('<IIIi', 0, fl, unc, -1))
                k.patches[0] = pa.tokens[ci]
                kids.append(k)
        sn.blocks[chf.offset] = kids
    root.blocks[vf['Promotions'].offset] = B.promotions
    root.blocks[vf['Extra Info'].offset] = [e.node for e in B.extras]
    for e in B.extras:
        struct.pack_into('<I', e.node.data, 0x20, ugh_datum & 0xFFFFFFFF)          # Owner Tag (group 0)
        if e.blob is not None:
            if e.token is None:
                e.token = ctx.add_raw('geometry', e.blob)
            e.node.patches[0x08] = e.token
        else:
            struct.pack_into('<iI', e.node.data, 8, -1, 0)
        _replicate_groups(e.node, slots, ugh_datum)
    sync_counts(root)
    return root


R_SND_GROUP = int.from_bytes(b'snd!', 'big')            # group word of a tag reference to a snd! tag


def _replicate_groups(node, slots, ugh_datum):
    """Fill lip-sync resource groups 1..slots-1 with copies of group 0 (same blob token): a language other than English then finds the English
    lip-sync of the English audio that plays for it.  Idempotent."""
    for gi in range(1, slots):
        base = 0x24 * gi
        node.data[8 + base:0x2C + base] = node.data[8:0x2C]
        node.blocks[0x18 + base] = [_copy_node(k) for k in (node.blocks.get(0x18) or [])]
        if 0x08 in node.patches:
            node.patches[0x08 + base] = node.patches[0x08]
        struct.pack_into('<I', node.data, 0x20 + base, ugh_datum & 0xFFFFFFFF)
