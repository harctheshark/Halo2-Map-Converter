# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""h2conv lightmap: give a Halo 2 Vista map whose structure BSP has no usable lightmap one that is sized to its BSP.

    h2conv lightmap <map.map> <out.map> [--style native|neutral-valid] [--force] [--dry-run] [--overwrite] [--report FILE]

The game reads a map's lightmap tables (one entry per BSP cluster and per instanced geometry instance, the geometry buckets they point to)
without any bound check, so a map whose lightmap is missing, belongs to another version of its BSP, or is a few entries short can crash at
load or in the first frames.  This command looks at every structure BSP of the map and tells what it found:

    ok        a complete lightmap that belongs to the BSP (its Structure Checksum is the BSP's, every table is large enough)
    missing   the lightmap tag has no Lightmap Group (a Structure Lightmap reference of NONE cannot be repaired here: no tag to fill)
    too short a table the game indexes without a check is smaller than the BSP (clusters / instances), or a bucket reference points outside
    foreign   the lightmap was made for another version of the BSP (its Structure Checksum is not the BSP's)

A BSP that is not 'ok' gets a generated lightmap (h2conv.lightmap_gen), a map in which every BSP is 'ok' is left alone - no output file is
written - unless --force asks for the generated lightmap anyway.  Styles:

    native         (default) Bitmap Group NONE: the game draws the BSP the way the original Xbox draws a BSP without lightmap
    neutral-valid  a valid lightmap with constant grey lighting (the game then lights objects with the single neutral environment)

Only the lightmap tag (and, for neutral-valid, the lightmap entries of the BSP's predicted resources) change; every other byte of the map
stays, the new geometry buckets go to the end of the map's geometry region and the regions behind it move down (the bitmap pixel pointers
and the language pack are moved with them).  The result is validated before it is moved to its name.  Halo 2 MCC maps are not handled by
this command yet (convert them to Vista first); original Xbox maps go through `h2conv xbox`.

Exit codes: 0 the output was written, 3 nothing to do (every lightmap is ok; no output written), 1 the repair failed, 2 usage / unreadable input."""
import json
import os
import struct
import sys

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NOTHING = 0, 1, 2, 3
STATES = ('ok', 'missing', 'too short', 'foreign')
MASK_PAGE = 0x200


class LightmapCliError(RuntimeError):
    """The map cannot be read or repaired; the message is for the person running the command."""


class BspInfo(object):
    __slots__ = ('index', 'sbsp', 'ltmp', 'state', 'problems', 'native', 'sbsp_root', 'ltmp_root')

    def __init__(self, index, sbsp, ltmp):
        self.index, self.sbsp, self.ltmp = index, sbsp, ltmp
        self.state, self.problems, self.native = None, [], False
        self.sbsp_root = self.ltmp_root = None

    def brief(self):
        return {'bsp': self.index, 'structure': self.sbsp.name if self.sbsp is not None else None,
                'lightmap': self.ltmp.name if self.ltmp is not None else None, 'state': self.state, 'problems': list(self.problems)}


def _layouts():
    from . import plugins as P
    return P.load_layouts('vista', corrected=True)


def inspect(cache, lay=None):
    """The structure BSPs of a Vista map and the state of their lightmaps -> [BspInfo] (decoded roots attached)."""
    from . import lightmap_gen as LG
    from . import tagtree as T
    lay = lay or _layouts()
    sc = cache.tags[cache.scenario_datum & 0xFFFF] if cache.scenario_datum != 0xFFFFFFFF else None
    if sc is None or sc.group != 'scnr' or not sc.addr:
        raise LightmapCliError('the map has no scenario tag')
    scnr = T.decode_tag(cache.read_addr, lay['scnr'], sc.addr, 'scnr')
    out = []
    for i, e in enumerate(scnr.block('Structure BSPs')):
        fb, fl = e.field('Structure BSP'), e.field('Structure Lightmap')
        if fb is None or fl is None:
            raise LightmapCliError('the scenario layout has no Structure BSP / Structure Lightmap field')
        sd, ld = e.u32(fb.offset + 4), e.u32(fl.offset + 4)
        sb = cache.tags[sd & 0xFFFF] if sd != 0xFFFFFFFF and (sd & 0xFFFF) < len(cache.tags) else None
        lt = cache.tags[ld & 0xFFFF] if ld != 0xFFFFFFFF and (ld & 0xFFFF) < len(cache.tags) else None
        b = BspInfo(i, sb, lt)
        out.append(b)
        if sb is None or sb.group != 'sbsp' or not sb.addr or (sb.index & 0xFFFF) >= 10000:
            raise LightmapCliError('Structure BSP %d is not a tag of this map' % i)
        b.sbsp_root = T.decode_tag(cache.read_addr, lay['sbsp'], sb.addr, 'sbsp')
        if lt is None or lt.group != 'ltmp':
            b.state, b.problems = 'missing', ['the Structure Lightmap reference is NONE: there is no lightmap tag to fill (not repairable here)']
            continue
        if not lt.addr or (lt.index & 0xFFFF) >= 10000:
            b.state, b.problems = 'missing', ['the lightmap tag has no data in this map']
            continue
        b.ltmp_root = T.decode_tag(cache.read_addr, lay['ltmp'], lt.addr, 'ltmp')
        probs = LG.check(b.ltmp_root, b.sbsp_root)
        eng = LG.engine_problems(probs)
        if any(p.code == 'no_group' for p in probs):
            b.state = 'missing'
        elif eng:
            b.state = 'too short'
        elif any(p.code == 'checksum' for p in probs):
            b.state = 'foreign'
        else:
            b.state = 'ok'
            g = b.ltmp_root.block('Lightmap Groups')[0]
            b.native = g.u32(0x1C) == 0xFFFFFFFF
        b.problems = [p.message for p in (eng or [p for p in probs if p.code == 'checksum'])][:4] if b.state != 'ok' else []
    return out


def _patch_u32(blob, pos, v):
    struct.pack_into('<I', blob, pos, v & 0xFFFFFFFF)


def drop_lightmap_predictions(root):
    """Predicted Resources of types 5..8 (lightmap buckets / bitmaps of the lightmap that is replaced) index the OLD lightmap; with a valid
    generated lightmap the game would read them against the new, smaller tables.  They are removed from the decoded BSP tag (which is then
    written again).  -> number removed."""
    from . import tagtree as T
    n = 0
    for nd in root.iter_nodes():
        for f in nd.fields:
            if f.name != 'Predicted Resources' or f.type not in T.BLOCK_TYPES:
                continue
            els = nd.blocks.get(f.offset, [])
            keep = [e for e in els if not 5 <= e.i16(0) <= 8]
            if len(keep) == len(els):
                continue
            nd.blocks[f.offset] = keep
            n += len(els) - len(keep)
    return n


def repair(src, out, style='native', force=False, dry_run=False, log=print, overwrite=False):
    """Repair the lightmaps of the Vista map `src` and write the result to `out`.  -> the report (a dict).
    report['written'] is False when nothing was written (every lightmap ok, or --dry-run)."""
    from . import lightmap_gen as LG
    from . import paths as _paths
    from . import tagtree as T
    from . import vista_writer as VW
    from .vista_cache import VistaCache
    if style not in LG.STYLES:
        raise LightmapCliError('--style must be one of %s' % ', '.join(LG.STYLES))
    src, out = os.path.abspath(src), os.path.abspath(out)
    if os.path.normcase(src) == os.path.normcase(out):
        raise LightmapCliError('the output must be a different file than the input')
    try:
        cache = VistaCache(src)
    except (ValueError, OSError) as ex:
        raise LightmapCliError('%s is not a readable Halo 2 Vista map (%s)' % (os.path.basename(src), ex))
    h = cache.header
    rep = {'input': src, 'output': out, 'format': 'vista', 'style': style, 'force': bool(force), 'written': False, 'bsps': [], 'warnings': []}
    try:
        lay = _layouts()
        bsps = inspect(cache, lay)
        rep['bsps'] = [b.brief() for b in bsps]
        for b in bsps:
            log('  BSP %d %s: lightmap %s -> %s%s' % (b.index, b.sbsp.name, b.ltmp.name if b.ltmp is not None else '(none)', b.state,
                                                      (' (%s)' % '; '.join(b.problems)) if b.problems else ''))
        todo = [b for b in bsps if (b.state != 'ok' or force)]
        unfixable = [b for b in todo if b.ltmp_root is None]
        if unfixable:
            raise LightmapCliError('BSP %d has no lightmap tag that can be filled: %s' % (unfixable[0].index, unfixable[0].problems[0]))
        if not todo:
            log('nothing to do: every lightmap of the map belongs to its BSP and is complete (use --force to generate one anyway)')
            return rep
        if dry_run:
            log('dry run: would generate a %s lightmap for BSP %s' % (style, ', '.join(str(b.index) for b in todo)))
            return rep
        if h.low_detail_bitmap_count or h.low_detail_bitmap_offset:
            raise LightmapCliError('the map has a low-detail bitmap atlas, which this command cannot move')
        _paths.refuse_existing(out, {'overwrite': overwrite})
        img = VW.VistaImage.from_cache(cache)
        mask, data_off = img.tag_offset_mask, h.data_offset
        base = mask + data_off
        align = T.AlignTable.load(os.path.join(_paths.data_dir(), 'block_alignment_vista.json'))

        secs = {n: (o, s) for n, o, s in cache.sections()}
        sounds_end = (secs['geometry_blocks'][0] if 'geometry_blocks' in secs else secs['string_block'][0])
        old_geometry = bytes(img.geometry) if img.geometry is not None else b''
        geom_base = sounds_end
        if 'bitmap_pixels' in secs:
            bitmap_lo, bitmap_hi = secs['bitmap_pixels'][0], secs['bitmap_pixels'][0] + secs['bitmap_pixels'][1]
        else:
            bitmap_lo = bitmap_hi = 0
        new_geometry = VW.RegionBuilder()
        plans = {}                                                              # lightmap tag index -> (BspInfo, geometry pointers of its buckets)
        grey = None
        if style == LG.STYLE_NEUTRAL_VALID:
            grey = next((t for t in cache.tags if t.group == 'bitm' and (t.name or '').lower() == 'rasterizer\\default_2d'), None)
            if grey is None:
                raise LightmapCliError('neutral-valid needs rasterizer\\default_2d in the map\'s tag table')
        for b in todo:
            if b.ltmp.index in plans:
                rep['warnings'].append('two Structure BSPs use the lightmap %s; only the first got a generated lightmap' % b.ltmp.name)
                continue
            raws = []

            def add_raw(node, off, data, _r=raws):
                _r.append(data)
                node.patches[off] = ('raw', len(_r) - 1)
            kw = {}
            if grey is not None:
                kw['bitmap'] = grey.datum
            info = LG.generate(b.ltmp_root, b.sbsp_root, style, add_raw, b.ltmp.datum, **kw)
            plans[b.ltmp.index] = (b.ltmp_root, 'ltmp', [geom_base + len(old_geometry) + new_geometry.add(d) for d in raws])
            if style == LG.STYLE_NEUTRAL_VALID:
                n = drop_lightmap_predictions(b.sbsp_root)
                if n:
                    plans[b.sbsp.index] = (b.sbsp_root, 'sbsp', None)
                    rep['warnings'].append('%s: %d predicted resource(s) of the replaced lightmap removed' % (b.sbsp.name, n))
            log('  BSP %d: %s lightmap generated: %d cluster(s), %d instance(s), %d bucket(s) of %d vertices' % (
                b.index, style, info['clusters'], info['instances'], len(info['buckets']), info['bucket_vertices']))
            b.state = 'generated'
        img.tag_data = _rebuild_tag_data(img, bytes(img.tag_data), plans, lay, align, base)
        delta = len(new_geometry)
        img.geometry = old_geometry + new_geometry.data()
        moved = {'bitmaps': 0, 'language_pack': 0}
        if delta:
            moved = _move_regions(img, lay, delta, base, bitmap_lo, bitmap_hi, data_off)
        img.crc = None
        rep['geometry_added'] = delta
        rep['moved'] = moved
        tmp = _paths.reserve_temp(out)
        try:
            VW.write_image(img, tmp, crc_mode='compute')
            problems = VW.validate_file(tmp)
            problems += _verify(src, tmp, lay, [b for b in todo if b.state == 'generated'], style, delta, bitmap_lo, bitmap_hi, secs)
            rep['validate_file'] = problems
            if problems:
                raise LightmapCliError('the written map failed its checks, nothing was kept: %s' % '; '.join(problems[:3]))
            os.replace(tmp, out)
        except BaseException:
            _paths.discard_temp(tmp)
            raise
        rep['written'] = True
        log('written: %s' % out)
        return rep
    finally:
        cache.close()


SLOT_ALIGN = 16                     # what lies behind a replaced tag moves by a multiple of 16 (SSE / Havok blocks keep their alignment)


def _pointer_words(root):
    """[(offset of the pointer word inside the tag, pointer)] of every block / dataref of a decoded tag that the decoder followed (the same
    conditions as tagtree.decode_struct)."""
    from . import tagtree as T
    seen = {}
    for nd in root.iter_nodes():
        for f in nd.fields:
            if f.type in T.BLOCK_TYPES or f.type in T.DATA_TYPES:
                cnt, ptr = struct.unpack_from('<iI', nd.data, f.offset)
                if cnt > 0 and ptr and ptr != 0xFFFFFFFF:
                    seen[nd.src_addr + f.offset + 4] = ptr
    return seen


def _rebuild_tag_data(img, old, plans, lay, align, base):
    """The tag data with the lightmap tags of `plans` replaced by their regenerated versions.  A lightmap keeps its start; everything behind it
    moves by a multiple of 256 bytes (so every block alignment stays).  The cache builder shares blocks between tags (a block can lie inside
    another tag), so EVERY tag is decoded and every block / dataref pointer that leads behind a replaced lightmap moves with its target;
    each tag that changed is decoded again from the new data and must be the same tree.  img.tags gets the new addresses and sizes."""
    import bisect
    from . import tagtree as T
    rd_old = lambda a, n: old[a - base:a - base + n]
    live = sorted((t for t in img.tags if t.addr and t.size and not t.is_empty and (t.index & 0xFFFF) < 10000), key=lambda t: t.addr)
    # 1. the slot of every replaced lightmap (its encoded length does not depend on a move by a multiple of 256)
    ends, cum, deltas = [], [], {}
    total = 0
    for t in live:
        if t.index in plans:
            root_, grp_, _ = plans[t.index]
            n0 = len(T.encode_tag(root_, lay[grp_], t.addr, align, resolve=lambda tok: 0))
            n0 += (-n0) % 4
            deltas[t.index] = -(-(n0 - t.size) // SLOT_ALIGN) * SLOT_ALIGN
            total += deltas[t.index]
            ends.append(t.addr + t.size)
            cum.append(total)
    spans = [(t.addr, t.addr + t.size) for t in live if t.index in plans]

    def shift(addr):
        """How far the data at `addr` (old coordinates) moves."""
        k = bisect.bisect_right(ends, addr)
        return cum[k - 1] if k else 0

    def inside_replaced(addr):
        return any(lo <= addr < hi for lo, hi in spans)

    # 2. assemble
    new = bytearray()
    cur = 0
    changed = []
    for t in live:
        p = t.addr - base
        if p < cur:
            raise LightmapCliError('tag %d overlaps the tag before it' % t.index)
        new += old[cur:p]
        cur = p + t.size
        mv = shift(t.addr)
        if t.index in plans:
            root_, grp_, ptrs = plans[t.index]
            enc = T.encode_tag(root_, lay[grp_], t.addr + mv, align, resolve=(lambda tok, _p=ptrs: _p[tok[1]]) if ptrs is not None else None)
            enc += b'\0' * ((-len(enc)) % 4)
            slot = t.size + deltas[t.index]
            new += enc + b'\0' * (slot - len(enc))
            changed.append((t, t.addr, t.size))
            t.addr, t.size = t.addr + mv, slot
            continue
        chunk = bytearray(old[p:cur])
        if t.group not in lay:
            if total:
                raise LightmapCliError('tag %d (%s) cannot be checked for moved pointers: no layout for its group' % (t.index, t.group))
        else:
            root = T.decode_tag(rd_old, lay[t.group], t.addr, t.group)
            hit = False
            for w, ptr in _pointer_words(root).items():
                d = shift(ptr)
                if d:
                    if inside_replaced(ptr):
                        raise LightmapCliError('tag %d (%s) points into a lightmap tag that is replaced' % (t.index, t.group))
                    struct.pack_into('<I', chunk, w - t.addr, (ptr + d) & 0xFFFFFFFF)
                    hit = True
            if hit or mv:
                changed.append((t, t.addr, t.size))
        new += chunk
        t.addr += mv
    new += old[cur:]
    end = max(t.addr + t.size for t in live) - base               # the region is a whole number of 4 KiB pages; slack behind the last tag stays at most one page
    keep = (end + 4095) & ~4095
    if len(new) > keep:
        del new[keep:]                                          # stale bytes the cache builder left behind the last tag
    new = bytes(new)
    # 3. every tag that changed must read as the same tree from the new data
    rd_new = lambda a, n: new[a - base:a - base + n]
    for t, oa, osz in changed:
        if t.group not in lay or t.index in plans:
            continue
        r0 = T.decode_tag(rd_old, lay[t.group], oa, t.group)
        r1 = T.decode_tag(rd_new, lay[t.group], t.addr, t.group)
        ok, why = T.same_tree(r0, r1)
        if not ok:
            raise LightmapCliError('tag %d (%s) did not survive the move: %s' % (t.index, t.group, why))
    return new


def _move_regions(img, lay, delta, base, lo, hi, data_off):
    """The geometry region grew by `delta` bytes: every region behind it moved down by that much.  Their absolute file offsets that live in
    the map's tags and language pack follow: the pixel and LOD offsets of the local bitmaps and the offsets in the language pack."""
    from . import tagtree as T
    blob = bytearray(img.tag_data)
    rd = lambda a, n: bytes(blob[a - base:a - base + n])
    moved = {'bitmaps': 0, 'language_pack': 0}
    for t in img.tags:
        if t.group != 'bitm' or not t.addr or t.is_empty:
            continue
        root = T.decode_tag(rd, lay['bitm'], t.addr, 'bitm')
        for el in root.block('Bitmaps'):
            for name in ('Pixels Offset', 'LOD1 Offset', 'LOD2 Offset', 'LOD3 Offset'):
                f = el.field(name)
                v = el.u32(f.offset)
                if v >> 30 == 0 and lo <= v < hi:
                    _patch_u32(blob, el.src_addr + f.offset - base, v + delta)
                    moved['bitmaps'] += 1
    img.tag_data = bytes(blob)
    if img.language_pack is not None:
        lp = bytearray(img.language_pack)
        for k in range(9):
            for w in (4, 5):
                o = k * 28 + w * 4
                v = struct.unpack_from('<I', lp, o)[0]
                if v:
                    struct.pack_into('<I', lp, o, v + delta)
                    moved['language_pack'] += 1
        img.language_pack = bytes(lp)
    return moved


def _verify(src, out, lay, repaired, style, delta, lo, hi, secs):
    """Checks of the written map against the source: the new lightmaps pass the checker, every bitmap still points at its own pixels, the
    other regions are byte for byte the source's."""
    import mmap
    from . import lightmap_gen as LG
    from . import tagtree as T
    from .vista_cache import VistaCache
    problems = []
    a, b = VistaCache(src), VistaCache(out)
    try:
        if len(a.tags) != len(b.tags):
            return ['the tag table changed size']
        for bi in inspect(b, lay):
            if bi.index in {r.index for r in repaired}:
                probs = LG.check(bi.ltmp_root, bi.sbsp_root, style)
                if probs:
                    problems.append('BSP %d: the generated lightmap does not pass the checker: %s' % (bi.index, probs[0].message))
        # the bitmaps
        bad = 0
        for t in a.tags:
            if t.group != 'bitm' or not t.addr or t.is_empty:
                continue
            ra = T.decode_tag(a.read_addr, lay['bitm'], t.addr, 'bitm')
            rb = T.decode_tag(b.read_addr, lay['bitm'], b.tags[t.index].addr, 'bitm')
            for ea, eb in zip(ra.block('Bitmaps'), rb.block('Bitmaps')):
                for name in ('Pixels Offset', 'LOD1 Offset', 'LOD2 Offset', 'LOD3 Offset'):
                    f = ea.field(name)
                    va, vb = ea.u32(f.offset), eb.u32(f.offset)
                    if va >> 30 == 0 and lo <= va < hi:
                        if vb != va + delta or a.raw(va, 256) != b.raw(vb, 256):
                            bad += 1
                    elif va != vb:
                        bad += 1
        if bad:
            problems.append('%d bitmap pointer(s) do not lead to their pixels' % bad)
        # regions: sounds + old geometry unchanged, strings / names / bitmap pixels shifted but equal
        sa = {n: (o, s) for n, o, s in a.sections()}
        sb = {n: (o, s) for n, o, s in b.sections()}
        for n in ('raw_sounds', 'string_block', 'string_idx', 'string_table', 'tag_names', 'tag_name_idx', 'bitmap_pixels'):
            if n in sa:
                oa, za = sa[n]
                ob, zb = sb.get(n, (None, None))
                shift = 0 if n == 'raw_sounds' else delta
                if ob != oa + shift or zb != za or a.raw(oa, za) != b.raw(ob, zb):
                    problems.append('region %s differs from the source' % n)
        og, zg = sa.get('geometry_blocks', (None, 0))
        if og is not None and a.raw(og, zg) != b.raw(og, zg):
            problems.append('the old geometry blocks changed')
    finally:
        a.close()
        b.close()
    return problems


def main(argv=None):
    import argparse
    from . import __version__
    from . import paths as _paths
    from . import lightmap_gen as LG
    ap = argparse.ArgumentParser(prog='h2conv lightmap', formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description=__doc__.split('Exit codes:')[0].strip().split('\n', 1)[1].strip(),
                                 epilog='Exit codes: 0 the output was written, 3 nothing to do (no output written), 1 the repair failed, 2 usage / unreadable input.')
    ap.add_argument('--version', action='version', version='Halo 2 Map Converter %s' % __version__)
    ap.add_argument('map', help='the Halo 2 Vista .map file to look at')
    ap.add_argument('out', help='where the repaired map goes (never the input file)')
    ap.add_argument('--style', choices=LG.STYLES, default=LG.STYLE_NATIVE, help='native (default): the BSP is drawn as without lightmap, as on the Xbox; '
                    'neutral-valid: a valid lightmap with constant grey lighting')
    ap.add_argument('--force', action='store_true', help='generate the lightmap even when the map already has a complete one that belongs to its BSP')
    ap.add_argument('--dry-run', action='store_true', help='say what was found and what would be done; write nothing')
    ap.add_argument('--overwrite', action='store_true', help='replace an existing output (and report) file')
    ap.add_argument('--report', default=None, help='write what was found and done (JSON, file names only) here')
    ap.add_argument('--settings', default=None, help='settings.json to use (default %%APPDATA%%\\H2MapConverter\\settings.json)')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:                                                  # noqa: BLE001
            pass
    if not os.path.isfile(a.map):
        print('ERROR: %s does not exist' % a.map)
        return EXIT_USAGE
    if a.report and os.path.exists(a.report) and not a.overwrite:
        print('ERROR: the report file %s already exists; pass --overwrite to replace it (or choose another name)' % a.report)
        return EXIT_USAGE
    if os.path.exists(a.out) and not a.overwrite and not a.dry_run:
        print('ERROR: the output file %s already exists; pass --overwrite to replace it (or choose another name / folder)' % a.out)
        return EXIT_USAGE
    _paths.configure(settings_file=a.settings)
    from . import mapinfo as MI
    rec = MI.identify(a.map, deep=False)
    if rec.fmt == 'mcc':
        print('ERROR: %s is a Halo 2 MCC map. This command handles Vista maps; convert the map to Vista first (h2conv convert).' % os.path.basename(a.map))
        return EXIT_USAGE
    if rec.fmt == 'xbox':
        print('ERROR: %s is an original Xbox map. Use h2conv xbox: it generates the lightmap while it converts.' % os.path.basename(a.map))
        return EXIT_USAGE
    if rec.fmt != 'vista' and rec.status == 'invalid':
        print('ERROR: %s: %s' % (os.path.basename(a.map), rec.reason))
        return EXIT_USAGE
    log = (lambda m: None) if a.quiet else (lambda m: print(m, flush=True))
    log('%s (Vista)' % os.path.basename(a.map))
    try:
        rep = repair(a.map, a.out, a.style, a.force, a.dry_run, log, a.overwrite)
    except LightmapCliError as ex:
        print('ERROR: %s' % ex)
        return EXIT_FAILED
    except FileExistsError as ex:
        print('ERROR: %s' % ex)
        return EXIT_USAGE
    except _paths.PathsError as ex:
        print('ERROR: %s' % ex)
        return EXIT_USAGE
    if a.report:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(a.report)), exist_ok=True)
            with open(a.report, 'w', encoding='utf-8') as fh:
                json.dump(_paths.report_for_file(rep), fh, indent=1, default=str)
        except OSError as ex:
            print('could not write the report: %s' % ex)
    return EXIT_OK if rep.get('written') else (EXIT_OK if a.dry_run else EXIT_NOTHING)


if __name__ == '__main__':
    sys.exit(main())
