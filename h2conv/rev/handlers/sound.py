# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Reverse sound handlers: Vista ugh! / snd! / lsnd / snde / sncl / matg Sound Globals -> MCC.

The inverse of h2conv/handlers/sound.py.

  @tag_handler('ugh!')            builds the ONE MCC sound gestalt (0x78 root, 15 blocks) for ALL snd! tags of the plan: Vista tags (action 'convert': the input
                                  map's own gestalt and the Vista shared maps the planner pulled sounds from) AND MCC's own tags (action 'mcc_donor'), merged
                                  into one set of tables; registers every MCC sound chunk through ctx.add_raw('sound', payload, 0x80) (+ node.patches on the
                                  chunk record's offset word) and every lip-sync block through ctx.add_raw('geometry', blob)
  @donor_handler('snd!')          the 28-byte MCC body of a donated MCC snd! with its indices rebased into the merged gestalt
  @node_handler('snd!', '')       28-byte MCC body of a converted Vista snd! (codec index, remastered reference = -1, classic tail)
  @node_handler('lsnd', '')       0x2C -> 0x34: prepend a null 'Remastered Looping Sound' tagref, flag bits 8/9 clear
  @node_handler('snde', '')       Reflection Type stringid (+0x38): the stock value of the 14 stock reverbs that have one, else 0
  @node_handler('sncl', '')       54 classes 0x5C -> 0x78: Vista bytes + the MCC legacy tail (low pass, function, ext ducking) of the same class
  @node_handler('matg', '/Sound Globals')   0x24 -> 0x2C: prepend 'Remastered Sound Classes', gestalt = the plan's ONE ugh!
  @pre_pass                       builds the sound bundle once up front
  @post_pass                      sanity check

Handler signatures are those of h2conv.rev.handlers: fn(ctx, vista_node, mcc_node).  The handlers are idempotent and recompute everything
they own from the Vista node.  After the ugh! handler ran, `ctx.sound_info` describes the result for the driver:
    mode                  always 'self_contained' (the H2EK-style MCC target: ONE ugh! referenced by the local matg, header 0x2EC = 0xFFFFFFFF)
    header_gestalt_datum  0xFFFFFFFF
    ugh_out_index / ugh_datum, counts, codecs, audio bytes, ...
ctx.opts keys read: sound_language_slots (default 8), sound_language (0), mcc_dat_dir, sound_remastered_classes ('legacy' default | 'null'),
and for Ogg Vorbis data under a PCM label (rev/sound_ogg.py): ffmpeg_path (ffmpeg.exe or its folder; env H2CONV_FFMPEG; default the local download),
ogg_decoder (a registered name | 'none' | a decoder object), temp_dir.
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass
import struct

from h2conv.rev.handlers import node_handler, tag_handler, donor_handler, pre_pass, post_pass
from h2conv import tagtree as T
from h2conv.rev import sound_bundle as sb
from h2conv.rev import sound_rules as R

NULL = 0xFFFFFFFF
_FALLBACK = {}          # id(ctx) -> state, for ctx objects that refuse new attributes


def group_word(group):
    """The u32 a tag reference stores for a group: the fourcc as a big-endian integer ('lsnd' -> 0x6C736E64, bytes 64 6E 73 6C in the file)."""
    return int.from_bytes(group.encode('latin1'), 'big')


class SoundState(object):
    def __init__(self, bundle):
        self.bundle = bundle

    def find(self, vnode, ctx=None):
        """The SndRec of a Vista snd! node: by the plan entry being converted (ctx.cur_entry: src_map + src_index), else by tag address
        verified against the 20 raw bytes."""
        ent = getattr(ctx, 'cur_entry', None)
        if ent is not None and getattr(ent, 'group', None) == 'snd!':
            r = self.bundle.by_tag.get((getattr(ent, 'src_map', None) or 'src', ent.src_index))
            if r is not None:
                return r
        cands = self.bundle.by_addr.get(vnode.src_addr) or []
        raw = bytes(vnode.data[:R.VISTA_SND_SIZE])
        for r in cands:
            if r.v_body == raw:
                return r
        return None

    def find_donor(self, entry):
        """The SndRec of an 'mcc_donor' plan entry."""
        d = getattr(entry, 'donor', None)
        if d is None:
            return None
        return self.bundle.by_tag.get((sb.donor_key(d), d.index))


def get_state(ctx, build=True):
    st = getattr(ctx, '_rev_sound_state', None)
    if st is None:
        st = _FALLBACK.get(id(ctx))
    if st is None and build:
        B = sb.build_bundle(ctx)
        st = SoundState(B)
        try:
            ctx._rev_sound_state = st
        except AttributeError:
            _FALLBACK[id(ctx)] = st
        stats = getattr(ctx, 'stats', None)
        if stats is not None:
            for k in ('sounds', 'perms', 'pitch_ranges', 'chunks', 'audio_bytes_unique', 'perms_ogg_decoded'):
                stats['sound_' + k] += B.notes.get(k, 0)
    return st


def reset_state(ctx):
    try:
        ctx._rev_sound_state = None
    except AttributeError:
        pass
    _FALLBACK.pop(id(ctx), None)


# ------------------------------------------------------------------------------------------------
# small helpers
# ------------------------------------------------------------------------------------------------
def _plan_entry(ctx, group):
    plan = getattr(ctx, 'plan', None)
    if plan is None:
        return None
    best = None
    for e in plan.entries:
        if e.group == group:
            if e.action == 'convert':
                return e
            best = best or e
    return best


def gestalt_mode(ctx):
    """The MCC target is the self-contained (H2EK-built) form: ONE ugh! for every sound of the map, referenced by the local matg,
    header 0x2EC = -1.  Sounds pulled from the Vista shared maps live in the same gestalt."""
    return 'self_contained'


def ugh_out_datum(ctx, entry=None):
    """The datum the output sound gestalt tag gets."""
    d = getattr(entry, 'out_datum', None) if entry is not None else None
    if d is not None:
        return d
    e = _plan_entry(ctx, 'ugh!')
    if e is not None and getattr(e, 'out_datum', None) is not None:
        return e.out_datum
    d = getattr(ctx, 'ugh_out_datum', None)               # stub contexts
    if d is not None:
        return d
    ctx.warn('snd_ugh_datum', 'no output datum known for the sound gestalt; using 0xFFFFFFFF')
    return NULL


def _remap_ref(ctx, data, off, with_group=True):
    """Remap one tagref of `data` (bytearray) from Vista datum to output datum.  The group word is kept (an unset reference keeps its group)."""
    pos = off + 4 if with_group else off
    d = struct.unpack_from('<I', data, pos)[0]
    struct.pack_into('<I', data, pos, ctx.remap_datum(d) if d != NULL else NULL)


# ------------------------------------------------------------------------------------------------
# pre pass + ugh!
# ------------------------------------------------------------------------------------------------
@pre_pass()
def prepare_sounds(ctx):
    """Reads the audio of every converted snd! once, up front (no transcoding is needed in this direction)."""
    get_state(ctx)


@tag_handler('ugh!')
def build_gestalt(ctx, entry=None):
    st = get_state(ctx)
    datum = ugh_out_datum(ctx, entry)
    B = st.bundle
    root = sb.bundle_to_ugh_node(ctx, B, datum)
    ctx.sound_info = dict(
        mode=gestalt_mode(ctx), ugh_datum=datum, ugh_out_index=getattr(entry, 'out_index', None),
        header_gestalt_datum=NULL,
        sounds=len(B.recs), failed=sum(1 for r in B.recs if r.error), perms=len(B.perms), chunks=len(B.chunk_rows),
        pitch_ranges=len(B.pitch_ranges), codecs=list(B.codecs), audio_bytes=B.notes.get('audio_bytes_unique', 0),
        warnings=B.notes.get('warnings', 0), notes=dict(B.notes))
    return root


@post_pass()
def check_gestalt(ctx, converted):
    """Every converted snd! needs the gestalt: warn when sounds were bundled but no ugh! tag was built."""
    st = getattr(ctx, '_rev_sound_state', None) or _FALLBACK.get(id(ctx))
    if st is None or not st.bundle.recs:
        return
    if getattr(ctx, 'sound_info', None) is None:
        ctx.warn('snd_no_gestalt', '%d snd! tags were converted but the plan has no ugh! entry to hold their gestalt'
                 % len(st.bundle.recs))


# ------------------------------------------------------------------------------------------------
# snd!
# ------------------------------------------------------------------------------------------------
@node_handler('snd!', '')
def fix_snd(ctx, vnode, mnode):
    st = get_state(ctx)
    rec = st.find(vnode, ctx)
    if rec is None:
        ctx.warn('snd_no_record', 'snd! node @%#x has no bundle record; writing a silent MCC body' % (vnode.src_addr or 0))
        body = R.mcc_snd_body(R.vista_flags_to_mcc(vnode.u16(0)), vnode.u8(2), 0, -1, -1, -1, 0, -1, -1, -1, -1, 0)
    else:
        body = rec.body
    if len(mnode.data) != R.MCC_SND_SIZE:
        raise ValueError('snd! MCC node is %d bytes, expected %d' % (len(mnode.data), R.MCC_SND_SIZE))
    mnode.data[:] = body
    mnode.blocks.clear()
    mnode.datas.clear()


@donor_handler('snd!')
def import_donor_snd(ctx, entry, mcc_node, mcc_cache):
    """MCC's OWN snd! tag (planner action 'mcc_donor') with its indices rewritten for the merged gestalt: its sound, pitch ranges, permutations,
    chunks (copied byte for byte), custom playback / promotion / extra info / remastered / reflection / low-pass entries were merged into the
    output ugh! by the bundle."""
    st = get_state(ctx)
    rec = st.find_donor(entry)
    if rec is None:
        ctx.warn('snd_no_record', 'donor snd! %s has no bundle record; writing a silent MCC body' % getattr(entry, 'name', '?'))
        body = R.mcc_snd_body(mcc_node.u16(0), mcc_node.u8(2), 0, -1, -1, -1, 0, -1, -1, -1, -1, 0)
    else:
        body = rec.body
    node = T.Node('snd!', '', ctx.lay_mcc['snd!'].fields, R.MCC_SND_SIZE, body)
    node.src_addr = getattr(mcc_node, 'src_addr', None)
    return node


# ------------------------------------------------------------------------------------------------
# lsnd
# ------------------------------------------------------------------------------------------------
@node_handler('lsnd', '')
def fix_lsnd(ctx, vnode, mnode):
    """Vista 0x2C -> MCC 0x34: a null 'Remastered Looping Sound' tagref in front (the sounds have no remastered twin), Flags bits 8/9
    ('Legacy Only' / 'Remastered Only') clear, everything else shifted by 8.  Tracks / Detail Sounds are re-cloned with
    remapped sound references and string ids."""
    lay = ctx.lay_mcc['lsnd']
    mf = {f.name: f for f in lay.fields}
    v = vnode.data
    m = mnode.data
    if len(v) != 0x2C or len(m) != 0x34:
        raise ValueError('lsnd node sizes %#x -> %#x, expected 0x2C -> 0x34' % (len(v), len(m)))
    struct.pack_into('<II', m, 0, group_word('lsnd'), NULL)
    struct.pack_into('<I', m, 8, struct.unpack_from('<I', v, 0)[0] & ~0x300)
    m[0xC:0x1C] = v[4:0x14]                                    # Marty's Music Time, Distance Bounds
    m[0x1C:0x24] = v[0x14:0x1C]                                # Continuous Damage Effect
    _remap_ref(ctx, m, 0x1C)
    for name, voff in (('Tracks', 0x1C), ('Detail Sounds', 0x24)):
        f = mf[name]
        cp = T.child_path('', f)
        es = T.element_size(f)
        kids = [sb.clone_node(ctx, None, vk, f.children, cp, 'lsnd', es) for vk in (vnode.blocks.get(voff) or [])]
        mnode.blocks[f.offset] = kids
        struct.pack_into('<i', m, f.offset, len(kids))         # keep the (count) word of the struct consistent


# ------------------------------------------------------------------------------------------------
# snde / sncl / matg
# ------------------------------------------------------------------------------------------------
# MCC `snde` 'Reflection Type' (stringid at +0x38) of the 32 stock reverbs, by tag name (all 43 stock MCC maps): it
# is matched against the type of the reflection sounds of a remastered `ugh!` (the MCC-only `reflections` block, which this converter leaves
# empty), so it is cosmetic here; names without an entry (community reverbs, 15 of the stock ones) keep 0 like MCC does for them.
SNDE_REFLECTION_TYPE = {
    'sound\\dsp_effects\\reverbs\\auditorium': 'indoor_large', 'sound\\dsp_effects\\reverbs\\city': 'city',
    'sound\\dsp_effects\\reverbs\\cpaul_alley': 'indoor_med', 'sound\\dsp_effects\\reverbs\\cpaul_big_room_new': 'indoor_large',
    'sound\\dsp_effects\\reverbs\\cpaul_cave_mp': 'outdoor_open', 'sound\\dsp_effects\\reverbs\\cpaul_hallway': 'indoor_med',
    'sound\\dsp_effects\\reverbs\\cpaul_little_somethin_new': 'indoor_small', 'sound\\dsp_effects\\reverbs\\cpaul_small_enclosed': 'indoor_small',
    'sound\\dsp_effects\\reverbs\\cpaul_small_room': 'indoor_small', 'sound\\dsp_effects\\reverbs\\cpaul_stone_room_new': 'indoor_med',
    'sound\\dsp_effects\\reverbs\\forest': 'outdoor_open', 'sound\\dsp_effects\\reverbs\\generic': 'indoor_med',
    'sound\\dsp_effects\\reverbs\\mountains': 'outdoor_open', 'sound\\dsp_effects\\reverbs\\sewer_pipe': 'indoor_small'}


def _cur_tag_name(ctx, group):
    """Name of the tag being converted (plan entry) or ''."""
    ent = getattr(ctx, 'cur_entry', None)
    if ent is not None and getattr(ent, 'group', None) == group:
        return (getattr(ent, 'name', '') or '').lower()
    return ''


@node_handler('snde', '')
def fix_snde(ctx, vnode, mnode):
    """Same 0x48 layout; Vista's 'undefined' dword at +0x38 is MCC's stringid 'Reflection Type': the stock value of the reverb when the tag name is
    a stock one (SNDE_REFLECTION_TYPE), else 0."""
    mnode.data[:] = vnode.data[:0x48]
    text = SNDE_REFLECTION_TYPE.get(_cur_tag_name(ctx, 'snde'))
    struct.pack_into('<I', mnode.data, 0x38, ctx.sid(text) if text else 0)


# MCC legacy `sound\sound_classes` tail (class bytes 0x5C..0x77) - identical in stock shared.map and the H2EK-built map for all 54 classes
# Inner Silence Distance 0.0, Low Pass Minimum/Maximum Distance 0.0 / 1.0, Function = this 28-byte
# default (1 constant) in every class, Ext Ducking = 0x14-byte elements {class 0x2D, ducking, fade in 0.1, sustain 0, fade out} in 10 classes;
# `Flags` bit 13 (Bypass Lfe) is clear in all 54.
SNCL_FUNCTION = bytes.fromhex('01000000000000000000000000000000000000000000803f0000803f')
SNCL_EXT_DUCKING = {
    0: '2d000000000000c1cdcccc3d000000000000003f', 4: '2d000000000020c1cdcccc3d000000000000803f',
    13: '2d000000000070c1cdcccc3d000000000000003f', 15: '2d000000000020c1cdcccc3d000000000000003f',
    19: '2d000000000060c1cdcccc3d000000000000003f', 20: '2d000000000020c1cdcccc3d000000000000003f',
    23: '2d000000000020c1cdcccc3d000000000000803f', 33: '2d0000000000c0c0cdcccc3d000000000000803f',
    39: '2d000000000020c1cdcccc3d000000000000003f', 40: '2d000000000020c1cdcccc3d000000000000003f'}


@node_handler('sncl', '')
def fix_sncl(ctx, vnode, mnode):
    """Vista classes (0x5C bytes each) -> MCC classes (0x78): the first 0x5C bytes are layout-identical and kept (Flags bit 13 stays clear),
    the tail (inner silence, low pass, Function dataref, Ext Ducking) is the MCC legacy default of the class (see SNCL_*)."""
    lay = ctx.lay_mcc['sncl']
    f = next(x for x in lay.fields if x.type in T.BLOCK_TYPES)
    vf = next(x for x in ctx.lay_vista['sncl'].fields if x.type in T.BLOCK_TYPES)
    cp = T.child_path('', f)
    ext_f = next(x for x in f.children if x.name == 'Ext Ducking')
    ext_path = T.child_path(cp, ext_f)
    kids = []
    for i, vk in enumerate(vnode.blocks.get(vf.offset) or []):
        d = bytearray(0x78)
        d[:0x5C] = vk.data[:0x5C]
        struct.pack_into('<H', d, 0xA, struct.unpack_from('<H', d, 0xA)[0] & 0x1FFF)
        struct.pack_into('<fff', d, 0x5C, 0.0, 0.0, 1.0)
        struct.pack_into('<i', d, 0x68, len(SNCL_FUNCTION))
        ext = SNCL_EXT_DUCKING.get(i)
        struct.pack_into('<i', d, 0x70, 1 if ext else 0)
        n = T.Node('sncl', cp, f.children, 0x78, d)
        n.datas[0x68] = SNCL_FUNCTION
        n.blocks[0x70] = [T.Node('sncl', ext_path, ext_f.children, 0x14, bytes.fromhex(ext))] if ext else []
        kids.append(n)
    mnode.blocks[f.offset] = kids
    struct.pack_into('<i', mnode.data, f.offset, len(kids))


@node_handler('matg', '/Sound Globals')
def fix_matg_sound_globals(ctx, vnode, mnode):
    """Vista element 0x24 -> MCC 0x2C.  MCC: +0x00 Remastered Sound Classes, +0x08 Legacy Sound Classes (== Vista 'Sound Classes'), +0x10 Sound
    Effects, +0x18 Sound Mix, +0x20 Sound Combat Dialogue Constants, +0x28 Sound Gestalt (datum only).  The gestalt is the map's ONE local ugh!.
    'Remastered Sound Classes' (opts sound_remastered_classes): 'legacy' (default) points at the same sncl as the legacy field so the reference
    is never a null the engine could dereference in remastered mode; 'null' leaves it unset."""
    if len(mnode.data) != 0x2C or len(vnode.data) != 0x24:
        raise ValueError('matg Sound Globals sizes %#x -> %#x, expected 0x24 -> 0x2C' % (len(vnode.data), len(mnode.data)))
    m = mnode.data
    m[0x08:0x28] = vnode.data[0x00:0x20]
    for off in (0x08, 0x10, 0x18, 0x20):
        _remap_ref(ctx, m, off)
    legacy_group, legacy_datum = struct.unpack_from('<II', m, 0x08)
    rem = NULL
    plan = getattr(ctx, 'plan', None)
    if plan is not None and hasattr(plan, 'remap_name'):                  # the H2EK layout: the planner keeps MCC's sound_remastered\sound_classes as tag #1
        try:
            rem = plan.remap_name('sncl', 'sound_remastered\\sound_classes')
        except Exception:                                                # noqa: BLE001
            rem = NULL
    if rem != NULL:
        struct.pack_into('<II', m, 0, legacy_group, rem)
    elif str((getattr(ctx, 'opts', None) or {}).get('sound_remastered_classes', 'legacy')) == 'null':
        struct.pack_into('<II', m, 0, legacy_group, NULL)
    else:
        struct.pack_into('<II', m, 0, legacy_group, legacy_datum)
    gd = struct.unpack_from('<I', vnode.data, 0x20)[0]
    struct.pack_into('<I', m, 0x28, ugh_out_datum(ctx) if gd != NULL else NULL)
    mnode.blocks.clear()
    mnode.datas.clear()
