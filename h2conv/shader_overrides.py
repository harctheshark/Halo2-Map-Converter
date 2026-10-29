"""Per-map shader overrides (data/shader_overrides.json): a small table of (input map, shader) -> ops applied to the converted shader's compiled arrays.
  lookup(map_scenario_path, shader_name) -> list of ops ([] when the map / shader has no entry)
  apply_pc_scale(pixel_constant_bytes, indices, factor) -> new bytes (first 4 bytes of the listed Pixel Constants scaled, floor per byte)
A map without an entry is never touched (the handler returns before reading anything else)."""
import json
import os

_TABLE = None


def table():
    global _TABLE
    if _TABLE is None:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'shader_overrides.json'), encoding='utf-8') as f:
            raw = json.load(f).get('overrides', [])
        t = {}
        for e in raw:
            t.setdefault((e['map'].lower(), e['shader'].lower()), []).extend(e['ops'])
        _TABLE = t
    return _TABLE


def has_map(map_name):
    m = (map_name or '').lower()
    return any(k[0] == m for k in table())


def lookup(map_name, shader_name):
    return list(table().get(((map_name or '').lower(), (shader_name or '').lower()), ()))


def scale_bytes(b, factor):
    """first 4 bytes (B, G, R, A of a Pixel Constant) scaled by factor, floor per byte; anything beyond 4 bytes is kept"""
    b = bytes(b)
    return bytes(int(x * factor) for x in b[:4]) + b[4:]
