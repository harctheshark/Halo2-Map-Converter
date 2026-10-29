"""Known rasterizer\\invalid slots of community shaders (data/bitmap_repairs.json): the Vista compile holds the red/green 'missing bitmap' checker
because the author lacked a stock detail map; the repair puts back the bitmap every intact copy of the shader carries in that slot.
  planner : bitmaps_for(shader name) -> repair bitmap names to plan (only for a shader that references rasterizer\\invalid)
  handler : repairs_for(shader name, slot names) -> {slot: bitmap name} for the slots that hold rasterizer\\invalid while every 'match' slot agrees"""
import json
import os

INVALID_BITMAP = 'rasterizer\\invalid'
_TABLE = None


def table():
    global _TABLE
    if _TABLE is None:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'bitmap_repairs.json'), encoding='utf-8') as f:
            raw = json.load(f).get('shaders', {})
        _TABLE = {k.lower(): ({int(s): n.lower() for s, n in v.get('match', {}).items()}, {int(s): n for s, n in v['slots'].items()})
                  for k, v in raw.items() if not k.startswith('_')}
    return _TABLE


def bitmaps_for(shader_name):
    row = table().get((shader_name or '').lower())
    return sorted(set(row[1].values())) if row else []


def repairs_for(shader_name, slot_names):
    """slot_names: the Vista postprocess Bitmaps tag names in slot order (None for a null slot)."""
    row = table().get((shader_name or '').lower())
    if not row:
        return {}
    match, slots = row
    names = [(n or '').lower() for n in slot_names]
    if any(i >= len(names) or names[i] != want for i, want in match.items()):
        return {}
    return {i: n for i, n in slots.items() if i < len(names) and names[i] == INVALID_BITMAP}
