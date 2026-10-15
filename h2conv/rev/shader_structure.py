# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Register structure of compiled shader templates (stems), used by the reverse `shad` handler.
Learned data: h2conv/rev/data/register_layout.json (learned offline from the stock maps of both games).

What a register code says about the slot it feeds, independent of the pass NAME (the engine register pairing, shader_params.stem_corr, only pairs registers of passes both
stems list; these rules cover the slots of passes MCC re-authored):

  * Bitmaps[i] of a compiled shader is the i-th bitmap parameter of its template (`bitmap_param_names`), the trailing placeholder slots follow.
  * Vertex Constant code of a bitmap transform = rank << 10 | ((mask << 1) | 1) << 4 | register.  `rank` is the bitmap register of the SAME pass implementation
    (Remappings bm: code -> Bitmaps[] slot), `mask` says which floats carry sx sy tx ty (13: all four, 9: scale in comps 0,1, 10: scale in comps 2,3 ...).
    `struct_roles` names the parameter component of such a constant; `register_layout()['masks']` holds the component letters and the floats that are one constant.
  * Pixel Constant code = register | mask_index << 5 | (2 * function) << 8 (mask index r 0, g 1, b 2, a 3, rgb 4): a byte no register
    delivers is 0 (`slot_const`).
  * Other Vertex Constant codes (rank >= 7 or an even F field) that hold ONE float over >= 50 compiled shaders of the corpus are constants (`register_layout()['codes']`).

Everything is measured over the stock corpus (43 Vista maps, 24 H2EK-built MCC maps, 43 stock MCC maps) and re-checked against the MCC twins of every Vista shader.  The forward direction does not use this module.

"""
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:                                         # noqa: BLE001
    pass

import collections
import glob
import json
import os
import sys

from .. import shader_params as SPM

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, 'data', 'register_layout.json')

# pixel register mask index -> array byte positions (b g r a = 0 1 2 3) it writes
PC_DELIVERED = {0: (2,), 1: (1,), 2: (0,), 3: (3,), 4: (0, 1, 2)}

_LAYOUT = []


def register_layout(reload=False, path=None):
    """the learned register-layout tables (h2conv/rev/data/register_layout.json) or None when the file does not exist"""
    if not _LAYOUT or reload or path:
        p = path or DATA_PATH
        try:
            with open(p, 'r', encoding='utf-8') as fh:
                val = json.load(fh)
        except (OSError, ValueError):
            val = None
        if path:
            return val
        _LAYOUT[:] = [val]
    return _LAYOUT[0]


def bitmap_param_names(template, game):
    """names of the template's bitmap parameters in template order = the first Bitmaps[] slots of a compiled shader; [] when the template is unknown"""
    return [p['name'] for p in SPM.param_defs(template, game) if p['type'] == 0]


def decode_vc_code(code):
    """Vertex Constant register code -> ('tex', rank, mask, register) for a bitmap-transform constant, else ('other', rank, mask, register)"""
    rank = code >> 10
    f = (code >> 4) & 0x3F
    return ('tex' if (rank < 7 and (f & 1)) else 'other'), rank, f >> 1, code & 0xF


def struct_roles(tab, bm_names, layout):
    """{(vc index, component): (feature letter, bitmap parameter)} for the bitmap-transform Vertex Constants of ONE stem.
    A constant is named only when EVERY register that reads it agrees: the register's rank must be a bitmap register of the same pass implementation
    that sends it to a bitmap array slot with a template parameter name (bm_names[slot]), and the mask must have a learned component layout."""
    res = {}
    poison = set()
    letters = (layout or {}).get('masks') or {}
    for _pname, impls in tab.passes:
        for im in impls:
            if im is None:
                continue
            bmap = dict(im['bm'])
            for code, idx in im['vc']:
                kind, rank, mask, _reg = decode_vc_code(code)
                if kind != 'tex':
                    poison.add(idx)
                    continue
                b = bmap.get(rank)
                ent = letters.get(str(mask))
                if b is None or b >= len(bm_names) or bm_names[b] is None or ent is None:
                    poison.add(idx)
                    continue
                for comp in range(4):
                    letter = ent['f'][comp]
                    if letter:
                        res.setdefault((idx, comp), set()).add((letter, bm_names[b]))
    out = {}
    for (idx, comp), s in res.items():
        if idx not in poison and len(s) == 1:
            out[(idx, comp)] = next(iter(s))
    return out


def slot_roles(learned, tab, shape, roles=None, bm_names=None, layout=None):
    """shader_params.slot_roles plus the STRUCTURAL role: a Vertex Constant component that neither the learned map nor the register-role table names (and the table does not
    contradict itself about) gets the role struct_roles finds -> ((letter, parameter), 'struct').  Without bm_names and layout it is shader_params.slot_roles."""
    out = SPM.slot_roles(learned, tab, shape, roles)
    if tab is None or bm_names is None or layout is None:
        return out
    srole = struct_roles(tab, bm_names, layout)
    if not srole:
        return out
    roles = SPM.code_roles() if roles is None else roles
    keys = tab.slot_keys()
    for idx in range(min(shape[1], len(out['vc']))):
        for comp in range(4):
            if out['vc'][idx][comp] is not None or (idx, comp) not in srole:
                continue
            preds = set()
            for ps, var, code in keys.get(('vc', idx), ()):
                r = roles.get(SPM.role_key(ps, var, 'vc', code, comp))
                if r:
                    preds.add((r[0], r[1]))
            if not preds:                                     # a role table that contradicts itself is not overridden by the structure
                out['vc'][idx][comp] = (srole[(idx, comp)], 'struct')
    return out


def slot_const(tab, kind, idx, comp, layout):
    """The constant value of ONE component of a slot that the register structure fixes (None when it is not fixed).
      pc : a byte no register of the stem delivers is 0 (165,000 components of the stock corpus, 0 exceptions)
      vc : (mask, comp) of a bitmap-transform code, or the code itself for the other codes, is one single float over >= 50 compiled shaders of the corpus;
           every register that reads the slot must give the same value."""
    keys = tab.slot_keys().get((kind, idx))
    if not keys or layout is None:
        return None
    if kind == 'pc':
        delivered = set()
        for _ps, _var, code in keys:
            d = PC_DELIVERED.get((code >> 5) & 7)
            if d is None:
                return None
            delivered.update(d)
        return 0.0 if comp not in delivered else None
    vals = set()
    for _ps, _var, code in keys:
        k, _rank, mask, _reg = decode_vc_code(code)
        ent = (layout.get('masks') or {}).get(str(mask)) if k == 'tex' else (layout.get('codes') or {}).get(str(code))
        v = ent['c'][comp] if ent else None
        if v is None:
            return None
        vals.add(v)
    return next(iter(vals)) if len(vals) == 1 else None

