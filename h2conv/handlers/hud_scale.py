# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""HUD bitmaps that MCC draws at a fraction of their pixel size: scale them down for Vista (bitm group, after handlers/bitmaps.py).

Why (engine evidence, 2026-10-20, MCC halo2.dll widget draw routine sub_18082CA30 + init sub_18082D7F0)
------------------------------------------------------------------------------------------------------
The MCC engine looks the bitmap tag `ui\\hud\\bitmaps\\new_hud\\crosshairs\\hud_reticles` up BY NAME when a map loads and, when it draws
a new-HUD bitmap widget whose Anchor is 4 (Crosshair) and whose bitmap is that tag, multiplies the draw scale by 0.5.  The
shipped MCC shared.map therefore holds the reticle atlas at 2x the pixels of Vista's (220x220 vs 110x110 for sequence 0, colour plate
576 vs 288, all 59 sequences) while every nhdt widget (sequence indices, offsets, registration points) is IDENTICAL in the two games:
the nhdt layouts of Halo2 and Halo2MCC differ only in 4 MCC-only Anchor values, there is no scale field to carry the factor in.
Vista draws a bitmap at its native pixel size, so a CUSTOM `hud_reticles` (a map that ships its own, author-edited atlas; the
converter has to copy it because it differs from the shared one) comes out 2x too big on screen: cyberdyne_h2 ("The Pit"), user
screenshots 2026-10-20.  A stock MCC map links Vista's own atlas (planner: equal-by-name -> shared), so it never gets here.
(`hud_multiplayer` is also 4x bigger in MCC shared.map, 128x64 vs 32x16, but the engine has no by-name rule for it and the
converter links it to Vista's copy; a map that carries a local oversized one is converted by this handler.)

What this does
--------------
For a bitm group whose NAME is in HALVED_BY_MCC_ENGINE and whose elements are really `factor` times the size of Vista's shared
twin (checked element by element - a group that is already Vista-sized is left alone), every element is area-averaged down by
`factor`: premultiplied-alpha box filter (exact 2x2 mean for even sizes, fractional-overlap weights for odd ones, round half up),
dimensions floor(w / factor) like Vista's own atlas, registration point round(r / factor), colour plate size / factor, the
sprite registration points likewise.  Sequences and sprite rectangles are normalised (0..1) and the nhdt sequence indices do not
change, so the sprite/sequence registration stays consistent.  Only uncompressed 32-bit mip-less 2D formats are handled
(A8R8G8B8 / X8R8G8B8 - what the MCC HUD atlases use); any other element leaves the whole group unchanged and warns
`hud_downscale_skipped`.  Deterministic (numpy float64, no randomness).  opts['no_hud_downscale'] = True disables it.
"""
import functools
import struct
import zlib

from . import node_handler

# bitmap tag name (lower case) -> the factor MCC divides its draw scale by (engine fact above)
HALVED_BY_MCC_ENGINE = {
    'ui\\hud\\bitmaps\\new_hud\\crosshairs\\hud_reticles': 2,
}

VISTA_ELEM = 0x74
FORMATS_32BIT = (10, 11)          # X8R8G8B8, A8R8G8B8 (bytes B, G, R, A)
RATIO_TOLERANCE = (1.7, 2.3)      # accepted source/twin size ratio around factor 2 (odd sizes floor)
MIN_MATCH = 0.9                   # share of elements that must show the factor against the Vista twin


@functools.lru_cache(maxsize=256)
def _weights(n_src, n_dst):
    """(n_dst, n_src) area-overlap matrix: row j averages the source span [j*s, (j+1)*s), s = n_src / n_dst."""
    import numpy as np
    s = n_src / float(n_dst)
    m = np.zeros((n_dst, n_src), dtype=np.float64)
    for j in range(n_dst):
        lo, hi = j * s, (j + 1) * s
        i0 = int(lo)
        i1 = min(n_src, int(hi) + 1)
        for i in range(i0, i1):
            ov = min(hi, i + 1.0) - max(lo, float(i))
            if ov > 0:
                m[j, i] = ov
        m[j] /= m[j].sum()
    return m


def downscale_bgra(raw, w, h, nw, nh, has_alpha=True):
    """Area-average a w x h 32-bit B,G,R,A image down to nw x nh.  has_alpha: colour is weighted by alpha (no dark/coloured fringes
    where transparent pixels carry garbage colour); X8R8G8B8 (has_alpha False) is a plain mean."""
    import numpy as np
    px = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4).astype(np.float64)
    if has_alpha:
        a = px[..., 3:4]
        px = np.concatenate([px[..., :3] * a / 255.0, a], axis=-1)
    out = np.einsum('jy,yxc->jxc', _weights(h, nh), px)
    out = np.einsum('ix,jxc->jic', _weights(w, nw), out)
    if has_alpha:
        oa = out[..., 3:4]
        col = np.where(oa > 0, out[..., :3] * 255.0 / np.maximum(oa, 1e-9), 0.0)
        out = np.concatenate([col, oa], axis=-1)
    return np.clip(np.floor(out + 0.5), 0, 255).astype(np.uint8).tobytes()


def _round_div(v, f):
    return int(v / float(f) + 0.5) if v >= 0 else -int(-v / float(f) + 0.5)


def _twin_sizes(ctx, name_l):
    """[(w, h)] of the Vista shared.map tag of this name, or None."""
    try:
        vc = ctx.vista.shared
    except Exception:                                           # noqa: BLE001
        return None
    for t in vc.tags:
        if t.group == 'bitm' and t.name and t.name.lower() == name_l and not t.is_empty:
            b = vc.read_tag(t)
            cnt, ptr = struct.unpack_from('<iI', b, 0x44)
            if cnt <= 0 or not ptr:
                return None
            blk = vc.read_addr(ptr, cnt * VISTA_ELEM)
            return [struct.unpack_from('<HH', blk, i * VISTA_ELEM + 4) for i in range(cnt)]
    return None


def _field_off(node, name, default):
    f = node.field(name)
    return f.offset if f is not None else default


def plan_factor(ctx, name_l, mcc_node):
    """-> (factor or None, reason).  The factor is only returned when the source elements really are `factor` x Vista's twin."""
    factor = HALVED_BY_MCC_ENGINE.get(name_l)
    if not factor:
        return None, 'not-listed'
    if ctx.opts.get('no_hud_downscale'):
        return None, 'disabled'
    twin = _twin_sizes(ctx, name_l)
    if not twin:
        return None, 'no Vista twin to compare with'
    src = mcc_node.blocks.get(_field_off(mcc_node, 'Bitmaps', 0x44), [])
    n = min(len(src), len(twin))
    if n == 0:
        return None, 'no elements'
    lo, hi = RATIO_TOLERANCE
    ok = 0
    for i in range(n):
        w, h = struct.unpack_from('<HH', src[i].data, 4)
        tw, th = twin[i]
        if tw and th and lo <= w / float(tw) <= hi and lo <= h / float(th) <= hi:
            ok += 1
    if ok < MIN_MATCH * n:
        return None, 'already Vista-sized (%d of %d elements are ~%dx the Vista twin)' % (ok, n, factor)
    return factor, None


@node_handler('bitm', '', prio=300)
def bitm_hud_downscale(ctx, mcc_node, vista_node):
    ent = getattr(ctx, 'cur_entry', None)
    name = getattr(ent, 'name', None)
    if not name:
        return
    name_l = name.lower()
    if name_l not in HALVED_BY_MCC_ENGINE:
        return
    factor, why = plan_factor(ctx, name_l, mcc_node)
    if factor is None:
        if why not in ('not-listed',):
            ctx.stats['hud_downscale_unchanged'] += 1
            if why not in ('disabled',) and not why.startswith('already'):
                ctx.warn('hud_downscale_skipped', '%s: %s' % (name, why), name)
        return
    bo = _field_off(vista_node, 'Bitmaps', 0x44)
    elems = vista_node.blocks.get(bo, [])
    # all-or-nothing: every element must be a mip-less 2D 32-bit image with a pixel blob we can replace
    for i, v in enumerate(elems):
        w, h, d = struct.unpack_from('<hhB', v.data, 4)
        typ = struct.unpack_from('<h', v.data, 0xA)[0]
        fmt = struct.unpack_from('<H', v.data, 0xC)[0]
        mips = struct.unpack_from('<h', v.data, 0x14)[0]
        tok = v.patches.get(0x1C)
        if fmt not in FORMATS_32BIT or typ != 0 or d != 1 or mips != 0 or not tok or tok[0] != 'raw' or w < factor or h < factor:
            ctx.warn('hud_downscale_skipped', '%s[%d]: format %d type %d depth %d mips %d (only mip-less 2D A8R8G8B8/X8R8G8B8)' % (
                name, i, fmt, typ, d, mips), name)
            return
    for v in elems:
        w, h = struct.unpack_from('<hh', v.data, 4)
        fmt = struct.unpack_from('<H', v.data, 0xC)[0]
        tok = v.patches[0x1C]
        kind, blob = ctx.raws[tok[1]]
        raw = zlib.decompress(blob)
        if len(raw) != w * h * 4:
            ctx.warn('hud_downscale_skipped', '%s: pixel blob %d != %dx%dx4' % (name, len(raw), w, h), name)
            return
        nw, nh = max(1, w // factor), max(1, h // factor)
        newraw = downscale_bgra(raw, w, h, nw, nh, has_alpha=(fmt == 11))
        nblob = zlib.compress(newraw, 9)
        ctx.raws[tok[1]] = (kind, nblob)
        struct.pack_into('<hh', v.data, 4, nw, nh)
        rx, ry = struct.unpack_from('<hh', v.data, 0x10)
        struct.pack_into('<hh', v.data, 0x10, _round_div(rx, factor), _round_div(ry, factor))
        struct.pack_into('<I', v.data, 0x34, len(nblob))
        ctx.stats['hud_downscale_elements'] += 1
    # tool-side colour plate size (cosmetic, Vista's own atlas has plate / factor)
    cw, ch = struct.unpack_from('<hh', vista_node.data, 0x18)
    struct.pack_into('<hh', vista_node.data, 0x18, cw // factor, ch // factor)
    # sprite registration points are pixels; the rectangles are normalised and stay
    so = _field_off(vista_node, 'Sequences', 0x3C)
    for seq in vista_node.blocks.get(so, []):
        for off, kids in seq.blocks.items():
            for sp in kids:
                if len(sp.data) >= 0x20:
                    rx, ry = struct.unpack_from('<ff', sp.data, 0x18)
                    struct.pack_into('<ff', sp.data, 0x18, rx / factor, ry / factor)
    ctx.stats['hud_downscale_groups'] += 1
    ctx.stats['hud_downscale:' + name_l] += 1
