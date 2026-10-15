# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Sound handlers: ugh! (gestalt) / snd! / lsnd / snde / sncl / matg Sound Globals.

  @tag_handler('ugh!')            builds the Vista sound gestalt for ALL converted (local) snd! tags of the plan - or, with a
                                  plan-less test context, all local classic snd! tags of the input - and registers every Vista sound
                                  chunk through ctx.add_raw('sound', payload) (+ node.patches on the chunk offset word).
  @node_handler('snd!', '')       20-byte Vista body with indices into the new gestalt (sound_bundle.SndRec.body)
  @node_handler('lsnd', '')       drops the leading remastered tagref (0x34 -> 0x2C), clears flag bits 8/9 (+ remastered-only entries)
  @node_handler('snde', '')       Reflection Type stringid (+0x38) -> 0
  @node_handler('sncl', '/Sound Classes')   0x78 -> 0x5C element, flag bit 13 (Bypass Lfe) cleared
  @node_handler('matg', '/Sound Globals')   0x2C -> 0x24: drop the leading Remastered Sound Classes tagref; gestalt datum = the planner's
                                  (shared ugh! in a multiplayer output, the local ugh! otherwise)
  @pre_pass                       builds the sound bundle once (audio transcode) before any tag is converted
  @post_pass                      warns when the driver's header 0x2E4 value disagrees with the sound topology

The handlers are idempotent and recompute everything they own from the MCC node (they do not depend on what the generic
conversion put into the Vista node).  After the ugh! handler ran, `ctx.sound_info` describes the result for the driver:
    mode                  'secondary' (multiplayer output; stock headlong layout: header[0x2E4] = the new ugh! datum, the engine merges it with
                          shared.map's gestalt, the snd! indices are secondary-relative) or 'self_contained' (solo / main menu: ONE ugh!,
                          header[0x2E4] = 0xFFFFFFFF)
    header_gestalt_datum  the value for the Vista header field 0x2E4 (convert.py should prefer it over plan.secondary_ugh_datum)
    ugh_out_index / ugh_datum, counts, audio bytes, ...
"""
import struct

from h2conv.handlers import node_handler, tag_handler, pre_pass, post_pass
from h2conv import tagtree as T
from h2conv import sound_bundle as sb

NULL = 0xFFFFFFFF
_FALLBACK = {}          # id(ctx) -> state, for ctx objects that refuse new attributes


class SoundState(object):
    def __init__(self, bundle):
        self.bundle = bundle

    def find(self, mnode, ctx=None):
        """The SndRec of an MCC snd! node: by the plan entry being converted (ctx.cur_entry: src_map + src_index), else by tag
        address verified against the 0x1C raw bytes."""
        ent = getattr(ctx, 'cur_entry', None)
        if ent is not None and getattr(ent, 'group', None) == 'snd!':
            r = self.bundle.by_tag.get((getattr(ent, 'src_map', None) or 'src', ent.src_index))
            if r is not None:
                return r
        cands = self.bundle.by_addr.get(mnode.src_addr) or []
        raw = bytes(mnode.data[:0x1C])
        for r in cands:
            if r.mcc_body == raw:
                return r
        return None


def get_state(ctx, build=True):
    st = getattr(ctx, '_sound_state', None)
    if st is None:
        st = _FALLBACK.get(id(ctx))
    if st is None and build:
        B = sb.build_bundle(ctx)
        st = SoundState(B)
        try:
            ctx._sound_state = st
        except AttributeError:
            _FALLBACK[id(ctx)] = st
        stats = getattr(ctx, 'stats', None)
        if stats is not None:
            for k in ('sounds', 'perms', 'pitch_ranges', 'chunks', 'audio_bytes_unique'):
                stats['sound_' + k] += B.notes.get(k, 0)
    return st


def reset_state(ctx):
    try:
        ctx._sound_state = None
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
    """Topology of the output sound gestalts (corpus: all 551 stock/community MP maps):
    'secondary'      multiplayer output: the SHARED ugh! (Vista shared.map #15064) stays the primary gestalt (matg points at it, also a LOCAL
                     matg), the map's own ugh! is the secondary one named by header 0x2E4; local snd! indices are secondary-relative.
    'self_contained' solo / main-menu output: ONE local ugh! referenced by the (local) matg, header 0x2E4 = 0xFFFFFFFF."""
    return 'secondary' if getattr(ctx, 'map_type', 1) == 1 else 'self_contained'


def shared_ugh_datum(ctx):
    """Datum of Vista shared.map's sound gestalt (NULL if unknown)."""
    plan = getattr(ctx, 'plan', None)
    for t in (getattr(plan, 'shared_placeholders', None) or ()):
        if getattr(t, 'group', None) == 'ugh!':
            return t.datum
    try:
        sh = ctx.vista.shared
        for t in sh.tags:
            if t.group == 'ugh!' and t.addr:
                return t.datum
    except Exception:
        pass
    return NULL


def ugh_out_datum(ctx, entry=None):
    """The datum the output sound gestalt tag gets."""
    d = getattr(entry, 'out_datum', None) if entry is not None else None
    if d is not None:
        return d
    e = _plan_entry(ctx, 'ugh!')
    if e is not None and getattr(e, 'out_datum', None) is not None:
        return e.out_datum
    ref_tag = getattr(ctx, 'ref_tag', None)            # test context: the reference map's own gestalt
    if ref_tag is not None:
        g = ctx.src.sound_gestalt
        if g is not None and g.name:
            r = ref_tag('ugh!', g.name)
            if r is not None:
                return r.datum
    ctx.warn('snd_ugh_datum', 'no output datum known for the sound gestalt; using 0xFFFFFFFF')
    return NULL


def _remap_ref(ctx, data, off, with_group=True):
    """Remap one tagref of `data` (bytearray) from MCC datum to output datum.  The group word is kept: stock Vista keeps the
    group of an unset reference (e.g. lsnd Continuous Damage Effect = 'dcmg' + datum -1)."""
    if with_group:
        d = struct.unpack_from('<I', data, off + 4)[0]
        struct.pack_into('<I', data, off + 4, ctx.remap_datum(d) if d != NULL else NULL)
    else:
        d = struct.unpack_from('<I', data, off)[0]
        struct.pack_into('<I', data, off, ctx.remap_datum(d) if d != NULL else NULL)


# ------------------------------------------------------------------------------------------------
# pre pass + ugh!
# ------------------------------------------------------------------------------------------------
@pre_pass()
def prepare_sounds(ctx):
    """Reads + transcodes the audio of every converted snd! once, up front (the sound cache makes re-runs fast)."""
    get_state(ctx)


@tag_handler('ugh!')
def build_gestalt(ctx, entry=None):
    st = get_state(ctx)
    datum = ugh_out_datum(ctx, entry)
    root = sb.bundle_to_ugh_node(ctx, st.bundle, datum)
    mode = gestalt_mode(ctx)
    B = st.bundle
    ctx.sound_info = dict(
        mode=mode, ugh_datum=datum, ugh_out_index=getattr(entry, 'out_index', None),
        header_gestalt_datum=(datum if mode == 'secondary' else NULL),
        sounds=len(B.recs), failed=sum(1 for r in B.recs if r.error), perms=len(B.perms), chunks=len(B.chunk_rows),
        pitch_ranges=len(B.pitch_ranges), audio_bytes=B.notes.get('audio_bytes_unique', 0),
        warnings=B.notes.get('warnings', 0), notes=dict(B.notes))
    return root


@post_pass()
def check_gestalt_header(ctx, converted):
    """The driver writes header 0x2E4 from the plan (plan.secondary_ugh_datum = the own ugh!).  That is right for the multiplayer topology only; for a
    solo / main-menu output the local matg points at the local ugh! and the header must be -1, otherwise the engine merges the gestalt with itself.
    The right value is ctx.sound_info['header_gestalt_datum']; until the driver uses it, say so loudly."""
    si = getattr(ctx, 'sound_info', None)
    plan = getattr(ctx, 'plan', None)
    if si is None or plan is None:
        return
    pv = getattr(plan, 'secondary_ugh_datum', None)
    if pv is not None and pv != si['header_gestalt_datum']:
        ctx.warn('snd_header_gestalt', 'header 0x2E4 would be %#x (plan.secondary_ugh_datum) but the %s sound topology needs %#x: the driver must use '
                 'ctx.sound_info["header_gestalt_datum"]' % (pv & 0xFFFFFFFF, si['mode'], si['header_gestalt_datum'] & 0xFFFFFFFF))


# ------------------------------------------------------------------------------------------------
# snd!
# ------------------------------------------------------------------------------------------------
@node_handler('snd!', '')
def fix_snd(ctx, mnode, vnode):
    st = get_state(ctx)
    rec = st.find(mnode, ctx)
    if rec is None:
        ctx.warn('snd_no_record', 'snd! node @%#x has no bundle record; writing a silent Vista body' % (mnode.src_addr or 0))
        flags = mnode.u16(0)
        body = struct.pack('<HBBBBhhBBBBhI', sb.mcc_flags_to_vista(flags), mnode.u8(2), 1, 0, 1, -1, -1, 0, 0xFF, 0xFF,
                           0xFF, -1, 0)
    else:
        body = rec.body
    if len(vnode.data) != 0x14:
        raise ValueError('snd! Vista node is %d bytes, expected 20' % len(vnode.data))
    vnode.data[:] = body
    vnode.blocks.clear()
    vnode.datas.clear()


# ------------------------------------------------------------------------------------------------
# lsnd
# ------------------------------------------------------------------------------------------------
def _ref_targets(ctx, node):
    """[(offset, with_group, datum)] of the non-null tag references of one struct."""
    out = []
    for f in node.fields:
        if f.type == 'tagref':
            if f.size == 8:
                d = struct.unpack_from('<I', node.data, f.offset + 4)[0]
            else:
                d = struct.unpack_from('<I', node.data, f.offset)[0]
            if d != NULL:
                out.append((f.offset, f.size == 8, d))
    return out


def _was_remastered_only(ctx, mnode, vnode):
    """True when the MCC element referenced sounds and ALL of them were sound_remastered\\ tags that no longer resolve."""
    refs = _ref_targets(ctx, mnode)
    if not refs:
        return False
    for off, wg, d in refs:
        t = ctx.mcc_tag(d)                                   # relative to the map being converted (ctx.cur_src)
        if t is None or not sb.is_remastered_name(t.name):
            return False
        new = struct.unpack_from('<I', vnode.data, off + (4 if wg else 0))[0]
        if new != NULL:
            return False
    return True


@node_handler('lsnd', '')
def fix_lsnd(ctx, mnode, vnode):
    """MCC 0x34 -> Vista 0x2C: drop the leading Remastered Looping Sound tagref, clear Flags bits 8/9 ('Legacy Only' /
    'Remastered Only'), shift everything by 8 (verified against stock Vista lsnd tags)."""
    lay = ctx.lay_vista['lsnd']
    vf = {f.name: f for f in lay.fields}
    m = mnode.data
    d = vnode.data
    if len(d) != 0x2C or len(m) != 0x34:
        raise ValueError('lsnd node sizes %#x -> %#x, expected 0x34 -> 0x2C' % (len(m), len(d)))
    flags = struct.unpack_from('<I', m, 8)[0] & ~0x300
    struct.pack_into('<I', d, 0, flags)
    d[4:0x14] = m[0xC:0x1C]                                  # Marty's Music Time, Distance Bounds
    d[0x14:0x1C] = m[0x1C:0x24]                              # Continuous Damage Effect
    _remap_ref(ctx, d, 0x14)
    for name, moff in (('Tracks', 0x24), ('Detail Sounds', 0x2C)):
        f = vf[name]
        kids = []
        cp = T.child_path('', f)
        es = T.element_size(f)
        for mk in (mnode.blocks.get(moff) or []):
            vk = sb.clone_node(ctx, None, mk, f.children, cp, 'lsnd', es)
            if _was_remastered_only(ctx, mk, vk):
                ctx.stats['lsnd_remastered_only_entries_dropped'] += 1
                continue
            kids.append(vk)
        vnode.blocks[f.offset] = kids
        struct.pack_into('<i', d, f.offset, len(kids))           # keep the (count) word of the struct consistent


# ------------------------------------------------------------------------------------------------
# snde / sncl / matg
# ------------------------------------------------------------------------------------------------
@node_handler('snde', '')
def fix_snde(ctx, mnode, vnode):
    """Same 0x48 layout; MCC's stringid 'Reflection Type' (+0x38) sits in a Vista 'undefined' slot -> 0."""
    vnode.data[:] = mnode.data[:0x48]
    struct.pack_into('<I', vnode.data, 0x38, 0)


@node_handler('sncl', '/Sound Classes')
def fix_sncl_class(ctx, mnode, vnode):
    """MCC element 0x78 -> Vista 0x5C: the first 0x5C bytes are layout-identical; the tail (inner silence, low pass,
    Function dataref, Ext Ducking block) is dropped; Flags (+0xA) bit 13 'Bypass Lfe' is MCC-only."""
    if len(vnode.data) != 0x5C:
        raise ValueError('sncl Vista element is %d bytes, expected 0x5C' % len(vnode.data))
    vnode.data[:] = mnode.data[:0x5C]
    fl = struct.unpack_from('<H', vnode.data, 0xA)[0] & 0x1FFF
    struct.pack_into('<H', vnode.data, 0xA, fl)
    vnode.blocks.clear()
    vnode.datas.clear()


@node_handler('matg', '/Sound Globals')
def fix_matg_sound_globals(ctx, mnode, vnode):
    """MCC element 0x2C -> Vista 0x24.  MCC: +0x00 Remastered Sound Classes, +0x08 Legacy Sound Classes (== Vista 'Sound
    Classes'), +0x10 Sound Effects, +0x18 Sound Mix, +0x20 Combat Dialogue Constants, +0x28 Sound Gestalt (datum only)."""
    if len(vnode.data) != 0x24:
        raise ValueError('matg Sound Globals Vista element is %d bytes, expected 0x24' % len(vnode.data))
    m = mnode.data
    d = vnode.data
    d[0:0x20] = m[0x08:0x28]
    for off in (0x00, 0x08, 0x10, 0x18):
        _remap_ref(ctx, d, off)
    # the gestalt: whatever the planner made of the MCC one (own ugh! -> the converted local ugh!, any other -> Vista's shared ugh!).  In a
    # multiplayer output the PRIMARY gestalt must be the shared one (the local ugh! is the header's secondary)
    d[0x20:0x24] = b'\xff\xff\xff\xff'
    old = struct.unpack_from('<I', m, 0x28)[0]
    if old != NULL:
        new = ctx.remap_datum(old)
        if gestalt_mode(ctx) == 'secondary':
            shared = shared_ugh_datum(ctx)
            if shared != NULL and (new == NULL or new == ugh_out_datum(ctx)):
                new = shared
        elif new == NULL:
            new = ugh_out_datum(ctx)
        struct.pack_into('<I', d, 0x20, new)
    vnode.blocks.clear()
    vnode.datas.clear()
