"""One ltmp Geometry Bucket's cache geometry block from its raw vertex streams (used by the lightmap generator)."""
import struct

import numpy as np

BUCKET_RAW = 0x18
F32 = np.float32
GEO = None            # h2conv.handlers.geometry, imported lazily (the handlers package registers handlers on import)


def _geo():
    global GEO
    if GEO is None:
        from .handlers import geometry as g
        GEO = g
    return GEO


def _vb_descriptor(decl, stride):
    return struct.pack('<BBHiiIiIII', decl, stride, 0, 0, 0, 0, 0, 0, 0, 0)


def bucket_to_cache(raw):
    """raw bucket vertices (n x 24 uint8) -> (block, sds, rds, entries).  Stream 0: declaration 47 (incident direction, copied: buckets do NOT get the cluster 1e-5 substitution); stream 1: declaration 46
    (floor(colour*255)) when any colour is non-zero, else the empty descriptor (0, 0)."""
    geo = _geo()
    raw = np.asarray(raw, np.uint8).reshape(-1, BUCKET_RAW)
    n = len(raw)
    col = np.frombuffer(np.ascontiguousarray(raw[:, 0:12]).tobytes(), '<f4').reshape(n, 3)
    dr = np.frombuffer(np.ascontiguousarray(raw[:, 12:24]).tobytes(), '<f4').reshape(n, 3)
    has_col = bool(col.any())
    desc = _vb_descriptor(47, 12) + (_vb_descriptor(46, 4) if has_col else _vb_descriptor(0, 0))
    pays = [desc, dr.astype('<f4').tobytes()]
    ents = [geo.Entry(geo.RES_TAG_BLOCK, 2, 0, 0, 0x20, len(desc), 0), geo.Entry(geo.RES_VERTEX_BUFFER, 2, 0, 0, 0, len(pays[1]), 0)]
    if has_col:
        c = np.zeros((n, 4), np.uint8)
        c[:, :3] = np.clip(np.floor(col.astype(np.float32) * F32(255)), 0, 255).astype(np.uint8)
        pays.append(c.tobytes())
        ents.append(geo.Entry(geo.RES_VERTEX_BUFFER, 2, 0, 0, 1, len(pays[2]), 0))
    sd = struct.pack('<ii', 2, 0)
    blk, new, rds = geo.layout_block(sd, pays, ents)
    return blk, len(sd), rds, new
