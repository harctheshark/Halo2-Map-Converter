# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Independent check of the shaders of a WRITTEN reverse-converted MCC map (reads the output file, not the in-memory trees).

    from h2conv.rev import shader_check as SC
    res = SC.check_output(out_path, vista_cache, plan, ctx)         # dict, JSON-able

What it proves:
  family   every stem's Pass references resolve, the implementation range of every stem pass has as many variants as the referenced pass has
           Implementations (8630/8630 MCC, 8084/8084 Vista in stock data), every PORTED stem / pass / vertex shader of the plan validates and carries
           well-formed DXBC (container, profile, size), no Vista D3D9 byte code is left in the file
  shaders  every converted shader fits the stem it names (skeleton + array lengths), and after the mapping every slot that the Vista stem and the
           output stem pair by (pass, variant, register) holds exactly what the Vista pass register reads: BITMAP slots by tag name, Pixel / Vertex
           Constant components by value.  A shader on a ported stem is checked slot for slot (the structure is the Vista one).  This is the
           'bump map as albedo' check: a bump map in an albedo sampler is a paired slot that holds the wrong bitmap.
  names    the same for the parameter names both games' learned maps give (bump_map, base_map, detail_map ...), where both know the stem
The check never trusts the converter's own bookkeeping except to find which Vista shader belongs to which output shader (same name).
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

import collections
import struct

from .. import shader_params as SPM
from .. import tagtree as T
from ..mcc_cache import MccCache
from . import shader_port as SP
from . import shader_tags as ST
from . import shader_structure as SS

NULL = 0xFFFFFFFF
NULLISH = ('', 'rasterizer\\default_2d')


def _bm_eq(a, b):
    na, nb = (a[0] or '').lower(), (b[0] or '').lower()
    if na in NULLISH and nb in NULLISH:
        return True
    return na == nb and a[1] == b[1]


def _learned_for(tn, vsd, msd):
    vl = SPM.vista_map(tn)
    vl = vl if vl and vl.get('fp') == vsd.fp and vl.get('n', 0) >= SPM.MIN_SAMPLES and vl.get('use') is not False else None
    ml = SPM.mcc_map(tn, msd.fp)
    ml = ml if ml and ml.get('n', 0) >= SPM.MIN_SAMPLES and ml.get('use') is not False else None
    return vl, ml


def check_output(path, vista, plan, ctx=None, max_examples=25):
    from . import transcode as X
    from .handlers import shaders as RS
    lm = X.layouts('mcc')
    c = MccCache(path)
    out = collections.OrderedDict()
    ex = collections.defaultdict(list)

    def add(kind, text):
        if len(ex[kind]) < max_examples:
            ex[kind].append(text)

    def mnode(t):
        return T.decode_tag(lambda a, n: c.read_at_addr(a, n), lm[t.group], t.addr, t.group)

    try:
        # ---------------------------------------------------------------- 1. the shader-template family of the file
        fam = collections.Counter()
        stems, spas_impls, spas_nodes, vrtx_nodes = {}, {}, {}, {}
        for t in c.tags:
            if t.is_null or not t.has_data or not t.name or t.group not in ('stem', 'spas', 'vrtx'):
                continue
            node = mnode(t)
            fam[t.group] += 1
            nl = t.name.lower()
            if t.group == 'stem':
                stems[nl] = node
            elif t.group == 'spas':
                pps = node.blocks.get(ST.SPAS_PP) or []
                spas_impls[nl] = len(pps[0].blocks.get(ST.SPAS_PP_IMPL) or []) if pps else 0
                spas_nodes[nl] = node
            else:
                vrtx_nodes[nl] = node
        out['family_tags'] = dict(fam)
        # stem -> pass coupling (every stem, MCC's own and ported)
        n_pass_refs = bad_ref = 0
        coupling_bad = []
        for nl, node in stems.items():
            pps = node.blocks.get(ST.STEM_PP) or []
            if not pps:
                continue
            for k, e in enumerate(pps[0].blocks.get(0x10) or []):
                d = e.u32(4)
                n_pass_refs += 1
                pt = c.tag_by_datum(d) if d not in (NULL, 0) else None
                if pt is None or pt.group != 'spas' or not pt.name:
                    bad_ref += 1
                    add('stem_pass_refs', '%s pass %d -> %s' % (nl.split('\\')[-1], k, 'null/unresolved' if pt is None else '%s %s' % (pt.group, pt.name)))
                    continue
                want = spas_impls.get(pt.name.lower())
                have = ST.unpack_range(e.u16(8))[1]
                if want is not None and want != have:
                    coupling_bad.append((nl, k, pt.name, have, want))
                    add('stem_pass_coupling', '%s pass %d (%s): stem has %d variants, the pass %d implementations' % (nl.split('\\')[-1], k, pt.name.split('\\')[-1], have, want))
        out['stem_pass_refs'] = n_pass_refs
        out['stem_pass_refs_unresolved'] = bad_ref
        out['stem_pass_coupling_mismatch'] = len(coupling_bad)
        # ported tags: validate what was WRITTEN
        ported = {'stem': 0, 'spas': 0, 'vrtx': 0}
        problems = []
        blobs = 0
        for e in plan.entries:
            if e.group not in ('stem', 'spas', 'vrtx') or e.action != 'convert':
                continue
            ported[e.group] += 1
            t = c.tag_by_name(e.group, e.name)                 # by NAME: the file may come from another run (the check also accepts a package map)
            if t is None:
                problems.append((e.group, e.name, 'not in the written file'))
                continue
            node = mnode(t)
            if e.group == 'stem':
                pr = ST.validate_stem(node)
            elif e.group == 'spas':
                vcache = plan.vista_cache(e.src_map)
                vnode = T.decode_tag(vcache.read_addr, X.layouts('vista')['spas'], vcache.tags[e.src_index].addr, 'spas')
                pr = SP.validate_ported_spas(node, vnode)
                for q in (node.blocks.get(ST.SPAS_PP) or [])[:1]:
                    for impl in q.blocks.get(ST.SPAS_PP_IMPL) or []:
                        d = impl.u32(ST.SPAS_IMPL_VERTEX_SHADER + 4)
                        if d not in (NULL, 0):
                            vt = c.tag_by_datum(d)
                            if vt is None or vt.group != 'vrtx':
                                pr.append('a Vertex Shader reference does not resolve to a vrtx tag')
                        for ps in impl.blocks.get(ST.SPAS_IMPL_PS) or []:
                            blobs += sum(1 for o in ST.M_PS_BLOBS if ps.datas.get(o))
            else:
                pr = ST.validate_vrtx(node)
                blobs += sum(1 for el in node.blocks.get(ST.M_VRTX_DX11) or [] if el.datas.get(ST.M_VRTX_EL_BLOB))
            for p in pr:
                problems.append((e.group, e.name, p))
        out['ported_tags_in_file'] = ported
        out['ported_dxbc_blobs'] = blobs
        out['ported_tag_problems'] = problems[:max_examples]
        out['ported_tag_problem_count'] = len(problems)
        # no D3D9 byte code left anywhere in the family (version token 0xFFFE....)
        d3d9 = 0
        for nl, node in spas_nodes.items():
            pps = node.blocks.get(ST.SPAS_PP) or []
            for impl in (pps[0].blocks.get(ST.SPAS_PP_IMPL) or []) if pps else []:
                for ps in impl.blocks.get(ST.SPAS_IMPL_PS) or []:
                    for o in ST.M_PS_BLOBS:
                        b = ps.datas.get(o)
                        if b and b[:4] != b'DXBC':
                            d3d9 += 1
        for nl, node in vrtx_nodes.items():
            for el in node.blocks.get(ST.M_VRTX_DX11) or []:
                b = el.datas.get(ST.M_VRTX_EL_BLOB)
                if b and b[:4] != b'DXBC':
                    d3d9 += 1
        out['non_dxbc_blobs'] = d3d9

        # ---------------------------------------------------------------- 2. every converted shader against its Vista twin
        vsd_memo = {}
        msd_memo = {}

        def vista_stem_of(cache, datum):
            t = cache.tags[datum & 0xFFFF] if (datum & 0xFFFF) < len(cache.tags) else None
            if t is None or t.datum != datum or t.group != 'stem':
                return None, None
            key = (cache.path, t.index)
            hit = vsd_memo.get(key)
            if hit is None:
                rt, rc = t, cache
                if not t.addr:                                   # shared placeholder
                    for nm in ('shared', 'single_player_shared', 'mainmenu'):
                        sh = plan.env.vista(nm)
                        if t.index < len(sh.tags) and sh.tags[t.index].datum == t.datum and sh.tags[t.index].addr:
                            rt, rc = sh.tags[t.index], sh
                            break
                root = T.decode_tag(rc.read_addr, X.layouts('vista')['stem'], rt.addr, 'stem')
                hit = vsd_memo[key] = ((t.name or '').lower(), RS.stem_data(root, RS._vista_name_of(rc), t.name or ''))
            return hit

        def mcc_stem_data(name_l):
            hit = msd_memo.get(name_l)
            if hit is None:
                node = stems.get(name_l)
                name_of = RS._mcc_name_of(c)
                if name_l.endswith(SP.PRIVATE_SUFFIX):
                    # a private water-chain stem names the private passes '<pass>__vista' (same structure, other tag names): compare it as the Vista stem it is, otherwise the pass
                    # pairing by tag name (SPM.stem_corr) pairs nothing and the shaders on it would silently go unchecked
                    name_of = (lambda d, base=name_of: SP.strip_private_suffix(base(d)))
                hit = msd_memo[name_l] = RS.stem_data(node, name_of, name_l) if node is not None else None
            return hit

        agg = collections.Counter({k: 0 for k in ('shaders', 'missing_in_file', 'no_postprocess', 'stem_unresolved', 'out_stem_unreadable', 'substituted_stem',
                                                  'bitmap_slots_checked', 'bitmap_slots_wrong', 'constant_components_checked', 'constant_components_wrong',
                                                  'shaders_with_wrong_bitmap_slots', 'shaders_with_wrong_constants', 'skeleton_fits', 'lengths_fit',
                                                  'bitmap_roles_checked', 'bitmap_roles_wrong', 'shaders_with_wrong_bitmap_roles', 'bitmap_roles_vista_only', 'bitmap_roles_mcc_only')})
        names_proof = collections.Counter()
        flagged = {'unresolved': [], 'default': [], 'dropped_features': [], 'animation_dropped': []}
        by_stem = collections.Counter()
        report = (ctx.extra.get('rev_shad_report') if ctx is not None else None) or {}
        for e in plan.entries:
            if e.group != 'shad' or e.action != 'convert':
                continue
            agg['shaders'] += 1
            ot = c.tag_by_name('shad', e.name)
            if ot is None or not ot.has_data:
                add('shaders', '%s: not in the written file' % e.name)
                agg['missing_in_file'] += 1
                continue
            vcache = plan.vista_cache(e.src_map)
            vtag = vcache.tags[e.src_index]
            if not vtag.addr:
                continue
            vnode = T.decode_tag(vcache.read_addr, X.layouts('vista')['shad'], vtag.addr, 'shad')
            onode = mnode(ot)
            vpps, opps = RS._blk(vnode, 'Postprocess Definition'), RS._blk(onode, 'Postprocess Definition')
            if not vpps or not opps:
                agg['no_postprocess'] += 1
                continue
            vname, vsd = vista_stem_of(vcache, vnode.u32(4))
            ostem = c.tag_by_datum(onode.u32(4))
            if vsd is None or ostem is None or not ostem.name:
                agg['stem_unresolved'] += 1
                add('shaders', '%s: template reference does not resolve (vista %s, output %s)' % (e.name, vname, ostem))
                continue
            msd = mcc_stem_data(ostem.name.lower())
            if msd is None:
                agg['out_stem_unreadable'] += 1
                continue
            # a stem the INPUT carries under a stock name is ported under a private name ('<name>__<map>', planner.custom_stem_entry): not a substitute
            priv = plan.entry_of_datum(e.src_map, vnode.u32(4)) if getattr(plan, 'private_stems', None) else None
            priv = priv if (priv is not None and getattr(priv, 'private_of', None) and priv.name.lower() == ostem.name.lower()) else None
            if ostem.name.lower() != vname and priv is None:
                agg['substituted_stem'] += 1
                add('substituted_stem', '%s: %s -> %s' % (e.name, vname, ostem.name))
            kind = 'ported' if (msd.fp == vsd.fp and (priv is not None or (plan.entry_of_name('stem', vname) is not None and plan.entry_of_name('stem', vname).action == 'convert'))) else (
                'identical' if msd.fp == vsd.fp else 'mapped')
            by_stem[kind] += 1
            va, oa = RS.shader_arrays(vpps[0]), RS.shader_arrays(opps[0])
            vnames = [((vcache.tags[d & 0xFFFF].name or '') if d not in (NULL, 0) and (d & 0xFFFF) < len(vcache.tags) else '') for d, _i, _l in va['bitmaps']]
            onames = [((c.tag_by_datum(d).name or '') if d not in (NULL, 0) and c.tag_by_datum(d) is not None else '') for d, _i, _l in oa['bitmaps']]
            corr = {'bm': {i: i for i in range(min(len(vnames), len(onames)))}, 'pc': {i: i for i in range(min(len(va['pc']), len(oa['pc'])))},
                    'vc': {i: i for i in range(min(len(va['vc']), len(oa['vc'])))}} if msd.fp == vsd.fp else SPM.stem_corr(msd.tab, vsd.tab)
            bad_bm = []
            for iM, iV in corr['bm'].items():
                if iM >= len(onames) or iV >= len(vnames):
                    continue
                agg['bitmap_slots_checked'] += 1
                if not _bm_eq((onames[iM], oa['bitmaps'][iM][1]), (vnames[iV], va['bitmaps'][iV][1])):
                    agg['bitmap_slots_wrong'] += 1
                    bad_bm.append((iM, iV, onames[iM], vnames[iV]))
            bad_c = 0
            for kd in ('pc', 'vc'):
                for iM, iV in corr[kd].items():
                    if iM >= len(oa[kd]) or iV >= len(va[kd]):
                        continue
                    for cc in range(4):
                        ref = va[kd][iV][cc]
                        tol = 1 if kd == 'pc' else 1e-3 * max(1.0, abs(ref))
                        agg['constant_components_checked'] += 1
                        if abs(oa[kd][iM][cc] - ref) > tol:
                            agg['constant_components_wrong'] += 1
                            bad_c += 1
            # the bitmap PARAMETER roles by template order (both games' template tags, h2conv/data params_*): slot i of a compiled shader is the i-th bitmap parameter of its
            # template, so the MCC slot of parameter P must hold what the Vista slot of P holds - whatever mechanism filled it (engine pairing, learned name, template order).
            # This is the check that a bitmap the pairing could not place (a NULL slot) cannot hide behind: 'dreamervar1 elite_jetpack' (change_color_map / multiply_map NULL)
            if msd.fp != vsd.fp:
                vp, mp = SS.bitmap_param_names(vname, 'vista'), SS.bitmap_param_names(ostem.name.lower(), 'mcc')
                if vp and mp and len(vp) <= len(vnames) and len(mp) <= len(onames):
                    bad_roles = []
                    for iM, pn in enumerate(mp):
                        if pn in vp:
                            iV = vp.index(pn)
                            agg['bitmap_roles_checked'] += 1
                            if not _bm_eq((onames[iM], oa['bitmaps'][iM][1]), (vnames[iV], va['bitmaps'][iV][1])):
                                agg['bitmap_roles_wrong'] += 1
                                bad_roles.append((pn, iM, iV, onames[iM], vnames[iV]))
                        else:
                            agg['bitmap_roles_mcc_only'] += 1
                    agg['bitmap_roles_vista_only'] += sum(1 for pn in vp if pn not in mp)
                    if bad_roles:
                        agg['shaders_with_wrong_bitmap_roles'] += 1
                        add('wrong_bitmap_roles', '%s (%s): %s' % (e.name.split(chr(92))[-1], vname.split(chr(92))[-1], bad_roles[:3]))
            if bad_bm:
                agg['shaders_with_wrong_bitmap_slots'] += 1
                add('wrong_bitmap_slots', '%s (%s): %s' % (e.name.split('\\')[-1], vname.split('\\')[-1], bad_bm[:3]))
            if bad_c:
                agg['shaders_with_wrong_constants'] += 1
                add('wrong_constants', '%s (%s): %d components' % (e.name.split('\\')[-1], vname.split('\\')[-1], bad_c))
            # structure: skeleton and array lengths fit the stem the shader names
            fm = msd.formula_shape()
            sk = (len(oa['lods']) == len(msd.lods) and all(a[1] == b[0] for a, b in zip(oa['lods'], msd.lods)) and list(oa['layers']) == list(msd.layers)
                  and list(oa['passes']) == list(msd.passes) and len(oa['impls']) == msd.n_impl)
            if msd.fp == vsd.fp:       # the Vista structure: the arrays must be the Vista shader's own (lengths are a function of the stem; formula_shape misses a few templates)
                sh = (len(oa['bitmaps']), len(oa['pc']), len(oa['vc'])) == (len(va['bitmaps']), len(va['pc']), len(va['vc']))
            else:                      # an MCC revision of the template: 1 + the highest register index, plus at most one trailing bitmap-property placeholder
                sh = (len(oa['bitmaps']), len(oa['pc']), len(oa['vc'])) in (fm, (fm[0] + 1, fm[1], fm[2]))
            agg['skeleton_fits'] += sk
            agg['lengths_fit'] += sh
            if not (sk and sh):
                add('structure', '%s on %s: skeleton %s lengths %s' % (e.name.split('\\')[-1], ostem.name.split('\\')[-1], sk, sh))
            # the learned parameter names of both games (shaders on an MCC stem both maps know)
            if msd.fp != vsd.fp:
                vl, ml = _learned_for(vname, vsd, msd)
                if vl is not None and ml is not None:
                    for i in range(min(len(onames), len(ml['bitmaps']))):
                        pm = RS._bm_param(ml, i)
                        if pm is None or pm[0] != 'p':
                            continue
                        js = [j for j in range(len(vl['bitmaps'])) if RS._bm_param(vl, j) == pm and j < len(vnames)]
                        if len(js) == 1:
                            ok = _bm_eq((onames[i], oa['bitmaps'][i][1]), (vnames[js[0]], va['bitmaps'][js[0]][1]))
                            names_proof['slots'] += 1
                            names_proof['wrong'] += (not ok)
                            if pm[1] in ('bump_map', 'base_map'):
                                names_proof[pm[1] + ' slots'] += 1
                                names_proof[pm[1] + ' wrong'] += (not ok)
            rp = report.get(e.name.lower())
            if rp is not None:
                if rp.get('unresolved'):
                    flagged['unresolved'].append({'shader': e.name, 'stem': ostem.name, 'slots': len(rp['unresolved'])})
                if rp.get('by_default'):
                    flagged['default'].append({'shader': e.name, 'stem': ostem.name, 'components': sum(rp['by_default'].values())})
                if rp.get('dropped'):
                    flagged['dropped_features'].append({'shader': e.name, 'stem': ostem.name, 'features': [list(x) for x in rp['dropped'][:6]]})
                if rp.get('animation') == 'dropped':
                    flagged['animation_dropped'].append({'shader': e.name, 'stem': ostem.name})
        out['shaders'] = dict(agg)
        out['shaders_by_stem_kind'] = dict(by_stem)
        out['parameter_name_proof'] = dict(names_proof)
        out['flagged'] = {k: v[:max_examples] for k, v in flagged.items()}
        out['flagged_counts'] = {k: len(v) for k, v in flagged.items()}
        out['examples'] = {k: v for k, v in ex.items()}
        a = agg
        out['ok'] = (not bad_ref and not coupling_bad and not problems and not d3d9 and a['bitmap_slots_wrong'] == 0 and a['bitmap_roles_wrong'] == 0
                     and a['constant_components_wrong'] == 0 and a['skeleton_fits'] == a['shaders'] - a['missing_in_file'] - a['no_postprocess'] - a['stem_unresolved'] - a['out_stem_unreadable']
                     and a['lengths_fit'] == a['skeleton_fits'] and a['missing_in_file'] == 0)
    finally:
        c.close()
    return out


def summary_lines(res):
    """A few human-readable lines for the driver's console report."""
    a = res.get('shaders', {})
    ln = ['shader check: family %s; stem->pass refs %d (%d unresolved, %d coupling mismatches); ported tags in file %s, %d DXBC blobs, %d problems, %d non-DXBC blobs' % (
        res.get('family_tags'), res.get('stem_pass_refs', 0), res.get('stem_pass_refs_unresolved', 0), res.get('stem_pass_coupling_mismatch', 0),
        res.get('ported_tags_in_file'), res.get('ported_dxbc_blobs', 0), res.get('ported_tag_problem_count', 0), res.get('non_dxbc_blobs', 0))]
    ln.append('shader check: %d shaders (%s); bitmap slots paired by the stems %d checked / %d wrong (%d shaders); constants %d / %d wrong; skeleton fits %d, lengths fit %d; '
              'substituted stems %d; bitmap parameter roles (template order) %d checked / %d wrong' % (
                  a.get('shaders', 0), res.get('shaders_by_stem_kind'), a.get('bitmap_slots_checked', 0), a.get('bitmap_slots_wrong', 0),
                  a.get('shaders_with_wrong_bitmap_slots', 0), a.get('constant_components_checked', 0), a.get('constant_components_wrong', 0),
                  a.get('skeleton_fits', 0), a.get('lengths_fit', 0), a.get('substituted_stem', 0), a.get('bitmap_roles_checked', 0), a.get('bitmap_roles_wrong', 0)))
    fc = res.get('flagged_counts', {})
    ln.append('shader check: flagged shaders: %s; learned-name proof %s; RESULT %s' % (fc, res.get('parameter_name_proof'), 'OK' if res.get('ok') else 'PROBLEMS'))
    return ln
