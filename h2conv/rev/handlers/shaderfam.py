# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Reverse tag handlers of the shader-template family: stem / spas / vrtx that MCC does not have are PORTED from the compiled Vista tag.

A plan entry reaches these handlers only when the planner decided 'convert' for a stem, pass or vertex shader (shader_port.PortPolicy said the whole
closure is proven; names MCC has are 'mcc_donor' entries and never come here - except the two sky vertex shaders of shader_tags.SKY_DEPTH_PINNED_VRTX, which the planner
(option sky_depth='vista', default) ports from the Vista tag under MCC's own name because Vista pins their clip depth to the far plane: z = 0, translate_vs fixup 'pin').  The handlers rebuild the MCC tag with the verified builders of
h2conv/rev/shader_tags.py and the DXBC of h2conv/rev/shader_port.py (translation + proof by evaluation):

    vrtx   build_mcc_vrtx(ctx, vista, [vs_5_0 DXBC per geometry classification])
    spas   build_mcc_spas(ctx, vista, {(impl, ps): (plain, z-prime, MRT)})        (alpha test compiled in from the pass's render states)
    stem   build_mcc_stem(ctx, vista)                                              (structure unchanged; Pass references re-pointed)

The private water chain (planner option water_chain) goes through the same three handlers: a private stem '<name>__vista' names the private pass
(_PrivatePassResolver), the private pass the private vertex shader (_PrivateVsResolver).

A failure raises (transcode.NO_GENERIC_FALLBACK: never converted generically); nothing is approximated.  Facts per ported tag go to
ctx.extra['shader_port'][group][name] and the counters ctx.stats['port_built:<group>'].
"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:  # pragma: no cover
    pass

from . import tag_handler
from .. import shader_port as SP
from .. import shader_tags as ST


def _vista_node(ctx, entry):
    cache = ctx.cache_for(entry.src_map)
    vtag = cache.tags[entry.src_index]
    return ctx.decode_vista(vtag, cache)


def _record(ctx, group, entry, **facts):
    ctx.extra.setdefault('shader_port', {}).setdefault(group, {})[entry.name.lower()] = facts
    ctx.stats['port_built:' + group] += 1


@tag_handler('vrtx')
def port_vrtx(ctx, entry):
    node = _vista_node(ctx, entry)
    vp = SP.port_vrtx(node, entry.name)
    if not vp.ok:
        raise ST.ShaderTagError('vertex shader %s cannot be ported: %s' % (entry.name, '; '.join(vp.problems[:3])))
    root = ST.build_mcc_vrtx(ctx, node, vp.dxbc)
    probs = ST.validate_vrtx(root)
    if probs:
        raise ST.ShaderTagError('ported vrtx %s does not validate: %s' % (entry.name, '; '.join(probs[:3])))
    live = [e for e in vp.elements if not e.get('empty')]
    _record(ctx, 'vrtx', entry, classifications=len(vp.dxbc), dxbc_bytes=sum(len(b) for b in vp.dxbc),
            z_remap=[e.get('fixup') is True for e in live],                 # z = w * c10.w + c10.z
            z_pin=[e.get('fixup') == 'pin' for e in live])                  # z = 0: Vista clip z == clip w (the sky), the reverse-Z far plane
    return root


class _PrivateVsResolver(object):
    """Resolver for build_mcc_spas: a Vertex Shader reference to a Vista vertex shader that has a PRIVATE port for this pass points at the private tag
    (shader_port.private_vrtx_name); everything else is the context's own resolver."""

    def __init__(self, ctx, names):
        self._ctx = ctx
        self._names = names                                  # {vista vrtx name (lower): private tag name}

    def __getattr__(self, attr):
        return getattr(self._ctx, attr)

    def remap_datum(self, datum):
        t = self._ctx.vista_tag(datum) if datum not in (None, 0xFFFFFFFF, 0) else None
        if t is not None and t.group == 'vrtx' and (t.name or '').lower() in self._names:
            return self._ctx.remap_name('vrtx', self._names[(t.name or '').lower()])
        return self._ctx.remap_datum(datum)


class _PrivatePassResolver(object):
    """Resolver for build_mcc_stem: a Pass reference to a Vista pass that has a PRIVATE port for this stem (the water chain, shader_port.private_water_name) points at the private
    tag; everything else is the context's own resolver."""

    def __init__(self, ctx, names):
        self._ctx = ctx
        self._names = names                                  # {vista pass name (lower): private tag name}

    def __getattr__(self, attr):
        return getattr(self._ctx, attr)

    def remap_datum(self, datum):
        t = self._ctx.vista_tag(datum) if datum not in (None, 0xFFFFFFFF, 0) else None
        if t is not None and t.group == 'spas' and (t.name or '').lower() in self._names:
            return self._ctx.remap_name('spas', self._names[(t.name or '').lower()])
        return self._ctx.remap_datum(datum)


@tag_handler('spas')
def port_spas(ctx, entry):
    node = _vista_node(ctx, entry)
    sp = SP.port_spas(node, entry.name)
    if not sp.ok:
        raise ST.ShaderTagError('pass %s cannot be ported: %s' % (entry.name, '; '.join(sp.problems[:3])))
    priv = {vl: SP.private_vrtx_name(v[0]) for vl, v in ((getattr(entry, 'port', None) and entry.port.info.get('private_vrtx')) or {}).items()}
    root = ST.build_mcc_spas(_PrivateVsResolver(ctx, priv) if priv else ctx, node, sp.dxbc)
    probs = SP.validate_ported_spas(root, node)
    if probs:
        raise ST.ShaderTagError('ported spas %s does not validate: %s' % (entry.name, '; '.join(probs[:3])))
    for f in sp.flags:
        ctx.warn('shader_port_flag', '%s: %s' % (entry.name.split('\\')[-1], f), tag=entry.name)
    _record(ctx, 'spas', entry, implementations=sp.impls, elements=len(sp.elements), blobs=sum(1 for v in sp.dxbc.values() for b in v if b),
            dxbc_bytes=sum(len(b) for v in sp.dxbc.values() for b in v), alpha_test=sum(1 for e in sp.elements if e['alpha']),
            empty_variants=len(sp.flags))
    return root


@tag_handler('stem')
def port_stem(ctx, entry):
    node = _vista_node(ctx, entry)
    priv = {pl: SP.private_water_name(v[0]) for pl, v in ((getattr(entry, 'port', None) and entry.port.info.get('private_spas')) or {}).items()}
    root = ST.build_mcc_stem(_PrivatePassResolver(ctx, priv) if priv else ctx, node)
    probs = ST.validate_stem(root)
    if probs:
        raise ST.ShaderTagError('ported stem %s does not validate: %s' % (entry.name, '; '.join(probs[:3])))
    pps = root.blocks.get(ST.STEM_PP) or []
    p0 = pps[0] if pps else None
    _record(ctx, 'stem', entry, lods=len(p0.blocks.get(0x00) or []) if p0 else 0, passes=len(p0.blocks.get(0x10) or []) if p0 else 0,
            implementations=len(p0.blocks.get(0x18) or []) if p0 else 0, remappings=len(p0.blocks.get(0x20) or []) if p0 else 0)
    return root
