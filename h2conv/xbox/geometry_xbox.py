# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Xbox -> Vista conversion of the GEOMETRY blocks ('blkh' ... 'fklb') of a Halo 2 Xbox (cache v8, build 02.09.27.09809) map.

Entry point:  convert_block(block_bytes, resources, section_data_size, group, site, warn=None, compound=None, stats=None)
                  -> (vista_block_bytes, vista_resources, vista_section_data_size, vista_resource_data_size)
    block_bytes  the Xbox block ('hklb' ... 'fklb'), raw pointer already resolved by the caller (XboxCache.raw(block_offset, block_size))
    resources    the geometry_block_info "Resources" fixup entries: tagtree Node elements, 7-tuples, 16-byte bytes or dicts (parse_resources)
    group, site  tag group ('mode', 'sbsp', ...) and site label ('Sections', 'Clusters', 'Water Definitions', ...); only the word 'water'
                 changes behaviour, the rest is for error messages.  warn(str) is called for repaired/dropped oddities of modded maps.
    compound     mode Sections only: (model node count, Compound Nodes of the section's Section Group), see expand_compound_nodes;
                 stats (a Counter, optional) gets 'compound_vertices_expanded'.
    Returns the Vista block, the fixup entries (7-tuples (type, flags, u16, primary, secondary, size, offset), SAME order as the input) with
    the new size/offset, the (unchanged) section data size and the new resource data size.  The caller writes block size = len(vista_block),
    Section Data Size and Resource Data Size into the tag's geometry_block_info and the new size/offset into its "Resources" block
    (apply_to_nodes does the latter for Node elements).  Pure function: no file access.  numpy required.  ~1.9 s for the 139 MB (10 631
    blocks) of all 45 pack maps.

Evidence and verification: every rule below was fitted against the stock Vista maps (see the numbers in each rule).
Everything below was proven against the stock Vista maps (the oracle) on same-named tags; the numbers are in the REPORT.

BLOCK FORMAT (identical on both sides)
  'hklb'(bytes of 'blkh') | u32 resource_data_size | section data (sds bytes) | resource data (rds bytes) | 'fklb'
  resource data = sequence of  'crsr' marker + resource payload.  Payload starts at fixup.offset (= marker + 4).  The marker of a
  type 0 / type 2 resource is 4-aligned, the marker of a type 1 (tag data) resource is 16-aligned; padding bytes are zero; the whole
  resource data is padded with zeros to a multiple of 4.  Re-packing the payloads of 1846/1846 Vista blocks (headlong, 01a_tutorial,
  shared.map) with this rule reproduces the original bytes exactly (a probe); on the Xbox side 3864/3913 stock/mod blocks repack
  identically (the 49 others are hand-built modded models, see "MODDED MAPS" below).
  The fixup table ("Resources" block of geometry_block_info in the TAG, 16 B each) is the same on both sides:
      {u8 type (0 tag block, 1 tag data, 2 vertex buffer), u8 flags (2/4), u16 0, u16 primary locator, i16 secondary locator,
       u32 size, u32 offset}
  primary locator = offset in the SECTION DATA of the pointer that is fixed up; secondary = element size (type 0), -1 (type 1) or the
  index of the vertex-buffer descriptor (type 2 inside a descriptor block) / -1 (type 2 with its descriptor dword in the section data).

WHAT DIFFERS (and nothing else): the vertex buffers (type 2 resources).
  * Xbox stores them in packed hardware formats, Vista in floats; the vertex buffer DESCRIPTOR (u8 declaration, u8 stride, u16 count=0,
    28 zero bytes) carries the vertex declaration id/stride and must be rewritten.  Two descriptor homes exist:
      - descriptor BLOCK: a type-0 resource with secondary locator 32 whose primary locator equals the one of the type-2 resources;
        the type-2 resource's secondary locator is the descriptor index (mode Sections, sbsp Clusters / Instanced Geometry / Water,
        ltmp Geometry Buckets).
      - a bare DWORD in the section data at section_data[primary locator] (secondary locator -1): DECR, PRTM, mode PRT Info,
        sbsp/scnr/dc*s decorator Cache Blocks (decal vertices at 0x18, sprite vertices at 0x58, ...).
  * Everything else is identical Xbox vs Vista (up to float noise, see REPORT): section data (all section data SIZES equal; every dword
    equal except the bare descriptor dwords and the water index count), parts, subparts, visibility bounds, index buffers (strips),
    mopp code/reorder tables, decorator placements, weather, ... .  Block sizes change ONLY through the vertex buffers (Xbox mode block
    3056 B -> Vista ~5.5 KB ...) and the water index buffer.
  * EXCEPTION sbsp "Water Definitions": the Xbox index buffer (type 0, primary 0x20, secondary 2) is a QUAD list (4 indices per quad,
    D3DPT_QUADLIST) and Vista's a triangle list: quad (a,b,c,d) -> triangles (a,b,d) and (d,b,c); the u32 index count in the section
    data at +0x20 changes from n to 1.5 n (5376 -> 8064 in headlong; index buffer, descriptors and section data then byte-equal to Vista).
  * MODDED MAPS (hand-built models of the pack): (a) the fixup SIZE of a vertex buffer can be wrong (e.g. 4 bytes/vertex recorded for a
    12-byte tangent frame buffer) while all data is present; convert_block repairs it from the vertex count of the buffers whose size is
    consistent (314 buffers in 148 tags, none of them in a stock Vista tag); (b) some blocks have no 'crsr' marker or garbage between
    resources (dropped) or a header {0, block size} instead of {'blkh', rds} (rebuilt).  All reported through warn().
  * sprite vertices (decl 55): the block also holds a float copy (type 0, 48-byte elements, primary = buffer primary - 16); Vista's
    vertex buffer is byte-identical to that copy (152/152 Vista blocks) and the copy is far closer to the oracle than the decoded
    28-byte vertices (7.6e-6 vs 1.6e-3 max error), so it is used when present.
  * mode skinned vertices that reference a COMPOUND node (node index >= the model's node count; Xbox stock fp_body knees and
    fp_beam_rifle) are expanded to the compound's real nodes, as the PC tool did (expand_compound_nodes; the Vista engine builds compound
    palette slots without the inverse bind pose, the knees of the first-person body were thrown 2-2.6 ft off, Foundry 2026-10-26).

VERTEX FORMAT TABLE  (Xbox declaration, stride)  ->  (Vista declaration, stride)     (the ids are the cache's rasterizer declaration ids)
  (0,0)   -> (0,0)      unused descriptor slot (ltmp buckets), no data
  (1,12)  -> (1,12)     float3 position (sbsp world verts, a few mode sections)                   byte copy
  (2,6)   -> (1,12)     mode rigid position  3 x int16  ->  3 x float32                           f = (q + 0.5) / 32767.5
  (4,8)   -> (2,16)     mode skinned 1: pos16 + [node0, node1] -> float3 + 4 node bytes            [n0 n1] + 00 00
  (6,12)  -> (4,20)     mode skinned 2: pos16 + 00 00 [n0 n1] [w0 w1] -> float3 + [n0 n1 00 00] [w0 w1 00 00]
  (8,12)  -> (6,20)     mode skinned 3: pos16 + [n0 n1 n2] [w0 w1 w2] -> float3 + [n0 n1 n2 00] [w0 w1 w2 00]
  (10,16) -> (8,20)     mode skinned 4: pos16 + 00 00 [n0..n3] [w0..w3] -> float3 + [n0..n3] [w0..w3]
  (24,8)  -> (23,8)     float2 texcoord                                                           byte copy
  (25,4)  -> (23,8)     mode texcoord 2 x int16 -> 2 x float32                                    f = (q + 0.5) / 32767.5
  (27,12) -> (25,36)    tangent frame: 3 x packed 11/11/10 dword -> 9 floats (normal, binormal, tangent)   see _dec3n
  (31,4)  -> (29,8)     lightmap texcoord 2 x int16 -> 2 x float32                                f = 0 if q == 0 else (q + 1) / 32768
  (47,3)  -> (46,4)     per-vertex RGB (ltmp / mode lightmap colours) 3 bytes -> 4 bytes         b0 b1 b2 + 00
  (48,4)  -> (47,12)    packed 11/11/10 vector (incident radiosity direction) -> 3 floats          see _dec3n
  (51,32) -> (49,56)    DECR vertex  float3 | 3 x dec3n | float2 uv  -> float3 | 9 floats | float2 uv
  (61,32) -> (59,56)    PRTM vertex  same layout as DECR
  (53,20) -> (51,32)    decorator decal vertex  float3 | uv16x2 | rgba -> float3 | uv float2 | uv float2 (copy) | rgba
  (55,28) -> (53,48)    decorator sprite vertex float3 | dec3n | dec3n | uv16x2 | rgba -> float3 | 3f | 3f | float2 | rgba
  (57,10) -> (56,12)    mode PRT Info vertex  u16 n | 8 bytes -> float32(n) | the same 8 bytes
  dec3n  = D3D NORMPACKED3: bits 0-10 x, 11-21 y (signed 11), 22-31 z (signed 10).  f = (q + 0.5) / (max + 0.5) with max 1023 (x,y) / 511 (z),
           and q in {0, -1} -> exactly 0.0 (exact zeros are encoded as 0 or -1; proven on 1.9 M components).
  Positions stay in the model's compression-bounds space (Compression Info is identical on both sides), so no rescaling is needed.
  The Vista vertex-buffer values are the ORIGINAL floats, the Xbox ones are quantised from them, so the conversion is exact only up to
  the quantisation step: pos/texcoord16 <= 1.53e-5 (half step of 1/32767.5, mean 7.5e-6), lightmap texcoord16 <= 2.7e-5 (97.5 % bit-exact),
  dec3n <= 1.96e-3 (z of 10 bits; mean 2-3e-4), everything else bit-exact / float noise.

THE TAG (outside this module): the geometry_block_info of each site must get the NEW values returned here:
  Block Size (+0x04) = len(vista_block), Section Data Size (+0x08) (unchanged), Resource Data Size (+0x0C), and every element of the
  "Resources" block (+0x10): size/offset fields rewritten (type/locators unchanged).  Nothing else in the geometry-bearing tag
  structs changes (compared field by field with the Vista tags: mode Sections, sbsp Clusters / Instanced Geometry, ltmp buckets are
  identical outside the block-info/pointer words; the only layout differences are the known Xbox/Vista element sizes of the decorator
  Cache Blocks (0x2C vs 0x34, Vista tail = 8 zero bytes), DECR (-4, Vista tail ffffffff) and PRTM (-4, Vista tail 00000000)).
  weat: the particle data is a RANDOM sample of the tag's Mass/Size ranges, regenerate/rescale it together with the Size range (REPORT).
"""
import collections
import struct

import numpy as np

BLK_MAGIC = b'hklb'
BLK_TAIL = b'fklb'
CRSR = b'crsr'
RES_BLOCK, RES_DATA, RES_VERTEX = 0, 1, 2
DESC_SIZE = 32
WATER_INDEX_PRIMARY = 0x20      # sbsp water definition: index buffer pointer in the section data
WATER_INDEX_COUNT_AT = 0x20     # ... and its u32 index count
NODE_MAP_PRIMARY = 0x64         # mode section data: the section's node map (type 0 resource, element size 1: u8 model node per local index)
SKIN_NODES = {2: 1, 4: 2, 6: 3, 8: 4}   # Vista skinned declaration -> node slots per vertex (2: one node, no weight bytes)


class GeometryConvertError(Exception):
    pass


# --------------------------------------------------------------------------------------------------------------------
# fixup entries
# --------------------------------------------------------------------------------------------------------------------
class Res(object):
    """One 16-byte fixup entry."""
    __slots__ = ('type', 'flags', 'u16', 'primary', 'secondary', 'size', 'offset')

    def __init__(self, type_, flags, u16, primary, secondary, size, offset):
        self.type, self.flags, self.u16, self.primary, self.secondary, self.size, self.offset = type_, flags, u16, primary, secondary, size, offset

    def tup(self):
        return (self.type, self.flags, self.u16, self.primary, self.secondary, self.size, self.offset)

    def pack(self):
        return struct.pack('<BBHHhII', *self.tup())


def parse_resources(resources):
    """Accepts a list whose items are 7-tuples (type, flags, u16, primary, secondary, size, offset), 16-byte bytes objects, dicts with
    those keys (type, flags, u16, primary, secondary, size, offset), or tagtree Node elements of the 'Resources' block (u8/u16/i16/u32
    accessors).  Returns list[Res]."""
    out = []
    for r in resources:
        if isinstance(r, Res):
            out.append(Res(*r.tup()))
        elif isinstance(r, (bytes, bytearray)):
            out.append(Res(*struct.unpack('<BBHHhII', bytes(r))))
        elif isinstance(r, dict):
            out.append(Res(r['type'], r['flags'], r['u16'], r['primary'], r['secondary'], r['size'], r['offset']))
        elif hasattr(r, 'u8') and hasattr(r, 'u32'):
            out.append(Res(r.u8(0), r.u8(1), r.u16(2), r.u16(4), r.i16(6), r.u32(8), r.u32(12)))
        else:
            out.append(Res(*r))
    return out


def pack_resources(resources):
    """list of 7-tuples / Res -> bytes (16 per entry), the layout of the tag's 'Resources' block elements."""
    return b''.join(Res(*r).pack() if not isinstance(r, Res) else r.pack() for r in resources)


def apply_to_nodes(nodes, new_resources):
    """Write the converted size/offset of every entry back into the tagtree Node elements of the 'Resources' block (same order)."""
    if len(nodes) != len(new_resources):
        raise GeometryConvertError('resource count changed')
    for n, r in zip(nodes, new_resources):
        r = r if isinstance(r, Res) else Res(*r)
        n.set_u32(8, r.size)
        n.set_u32(12, r.offset)


# --------------------------------------------------------------------------------------------------------------------
# component decoders  (numpy)
# --------------------------------------------------------------------------------------------------------------------
def _i16_centre(q):
    """int16 -> float32, (q + 0.5) / 32767.5  (Xbox SHORT normalised; proven: residual v*32767.5-q is uniform on [0,1) for every q)."""
    return ((q.astype(np.float64) + 0.5) / 32767.5).astype(np.float32)


def _i16_lightmap(q):
    """int16 -> float32 for lightmap / decal texcoords: 0 -> 0.0, otherwise (q + 1) / 32768 (exact on 97.5 % of 151 k oracle values)."""
    f = (q.astype(np.float64) + 1.0) / 32768.0
    f[q == 0] = 0.0
    return f.astype(np.float32)


def _dec3n(d):
    """uint32 array -> (n, 3) float32.  11/11/10 signed fields; f = (q + 0.5) / (max + 0.5); q in {0, -1} -> 0.0."""
    d = d.astype(np.uint32)
    x = (d & 0x7FF).astype(np.int32)
    y = ((d >> 11) & 0x7FF).astype(np.int32)
    z = ((d >> 22) & 0x3FF).astype(np.int32)
    x = np.where(x >= 1024, x - 2048, x)
    y = np.where(y >= 1024, y - 2048, y)
    z = np.where(z >= 512, z - 1024, z)
    out = np.empty((len(d), 3), dtype=np.float32)
    for k, (q, mx) in enumerate(((x, 1023.5), (y, 1023.5), (z, 511.5))):
        f = (q.astype(np.float64) + 0.5) / mx
        f[(q == 0) | (q == -1)] = 0.0
        out[:, k] = f
    return out


def _view(a, off, dtype, cnt):
    """column slice of an (n, stride) uint8 array, as an (n, cnt) array of dtype."""
    w = np.dtype(dtype).itemsize * cnt
    return np.ascontiguousarray(a[:, off:off + w]).view(dtype).reshape(len(a), cnt)


def _f32bytes(*arrs):
    """concatenate (n, k) float32 arrays into (n, 4*sum k) uint8"""
    n = len(arrs[0])
    return np.concatenate([np.ascontiguousarray(x, dtype=np.float32).reshape(n, -1).view(np.uint8) for x in arrs], axis=1)


def _cv_copy(a):
    return a


def _cv_pos16(a):
    return _f32bytes(_i16_centre(_view(a, 0, '<i2', 3)))


def _cv_skin(nidx, nw, pad_front):
    """Xbox tail after the int16x3 position: [pad_front zero bytes][nidx index bytes][nw weight bytes]  ->  Vista [idx padded to 4][w padded to 4]
    (decl 4/8 have no weights: tail = [n0 n1])."""
    def f(a):
        n = len(a)
        pos = _f32bytes(_i16_centre(_view(a, 0, '<i2', 3)))
        base = 6 + pad_front
        idx = np.zeros((n, 4), dtype=np.uint8)
        idx[:, :nidx] = a[:, base:base + nidx]
        parts = [pos, idx]
        if nw:
            w = np.zeros((n, 4), dtype=np.uint8)
            w[:, :nw] = a[:, base + nidx:base + nidx + nw]
            parts.append(w)
        return np.concatenate(parts, axis=1)
    return f


def _q255(w):
    """float weight -> weight byte as the PC tool wrote it: round-half-even of (shortest decimal of the float32) * 255.  Matches all 59
    oracle vertices (0.5 -> 128, 0.9 -> 230, 0.1 -> 26, 0.8 -> 204, 0.2 -> 51, 0.7 -> 178, 0.3 -> 76, 0.6 -> 153, 0.4 -> 102)."""
    return int(round(float('%.6g' % float(np.float32(w))) * 255.0))


def expand_compound_nodes(vb, vdecl, node_map, node_count, compounds, warn=None):
    """Replace compound-node references of converted Vista skinned vertices by the compound's real nodes, as Bungie's PC tool did.

    vb          (n, vstride) uint8 Vista skinned vertices from _cv_skin: float3 | node[4] | weight[4]  (decl 2: float3 | node[4])
    vdecl       their Vista declaration (SKIN_NODES)
    node_map    the section's node map (local vertex node -> model node), None / empty when the vertices index model nodes directly
    node_count  len(mode Nodes)
    compounds   the Section Group's Compound Nodes [(node bytes[4], weights[3]), ...]; model node node_count + k = compound k
                (None: the section may be drawn with several different lists - its compound references are only reported)
    Returns (vertices, number of vertices changed).

    On Xbox a vertex node (through the node map when the section has one) >= node_count is compound node (node - node_count): a blend of
    up to 4 real nodes, read as UNSIGNED bytes up to the first 0xFF, weight 3 = 1 - the others.  Xbox stock content uses it in exactly two
    models (all 230 Xbox sources): fp_body (knees = thigh + calf 0.5 / 0.5, 38 of 819 vertices) and fp_beam_rifle (21 of 1539).
    halo2.exe builds those palette slots (sub_77DEBD, 0x77E007-0x77E0EC: movzx node byte, cmp 0FFh, imul 34h into the object's RAW node
    matrices, weight 3 = 1 - running sum) WITHOUT the inverse bind pose every real slot gets (sub_77DD88: node matrix x Nodes[i] +0x28 via
    sub_478209), so a model-space vertex on a compound slot lands ~|its position| off the bone (0.2-0.27 wu on the fp_body knees: the
    stretched Spartan legs of Foundry, 2026-10-26).  The PC tool never leaves such a reference (stock Vista fp_body / fp_beam_rifle keep
    their Compound Nodes and Hardware Node Count, 0 vertices reference them): it expands the vertex to the compound's nodes, merges equal
    nodes, sorts by weight (stable: ties keep the compound's order), keeps the declaration's node count, renormalises and quantises
    (_q255) - byte-identical to stock Vista shared.map fp_body 38/38 and fp_beam_rifle 21/21.  Vertex count, stride, node map and
    section data stay as they are.  A vertex that cannot be expanded (compound past the list, a compound node that is no model node,
    a local node past the node map, a result node missing from the node map, unusable weights) is left as it is and reported."""
    cap = SKIN_NODES.get(vdecl)
    if cap is None or (compounds is not None and not compounds) or not len(vb):
        return vb, 0
    nm = [int(x) for x in (node_map or [])]
    gi = vb[:, 12:16].astype(np.int32)
    if nm:
        lut = np.full(256, -1, dtype=np.int32)
        lut[:len(nm)] = nm[:256]
        gi = lut[gi]                                       # model node per slot (-1: local node past the node map)
    used = np.zeros((len(vb), 4), dtype=bool)
    used[:, :cap] = True
    if vdecl != 2:
        used &= vb[:, 16:20] > 0
    hit = np.nonzero((used & (gi >= node_count)).any(axis=1))[0]
    if not len(hit):
        return vb, 0
    if compounds is None:
        if warn:
            warn('%d skinned vertices reference a compound node and were left as they are: the section may be drawn with several '
                 'different Compound Nodes lists' % len(hit))
        return vb, 0
    out = np.array(vb, dtype=np.uint8, copy=True)
    local = {g: i for i, g in enumerate(nm)}
    bad = collections.Counter()

    def expand(v):
        acc = []                                           # [(model node, weight)] in first-seen order
        for k in range(cap):
            if not used[v, k]:
                continue
            g, wt = int(gi[v, k]), (int(out[v, 16 + k]) / 255.0 if vdecl != 2 else 1.0)
            if g < 0:
                return 'a node past the section node map'
            if g < node_count:
                parts = [(g, wt)]
            elif g - node_count < len(compounds):
                ni, cw = compounds[g - node_count]
                parts, tot = [], 0.0
                for j in range(4):
                    m = int(ni[j]) & 0xFF                  # unsigned, as the engine reads it (movzx); 0xFF ends the list
                    if m == 0xFF:
                        break
                    if m >= node_count:
                        return 'a compound node that is no model node'
                    ww = cw[j] if j < 3 else 1.0 - tot
                    tot += ww
                    parts.append((m, wt * ww))
            else:
                return 'a compound past the %d Compound Nodes' % len(compounds)
            for m, x in parts:
                for q, (mm, xx) in enumerate(acc):
                    if mm == m:
                        acc[q] = (mm, xx + x)
                        break
                else:
                    acc.append((m, x))
        acc = sorted(acc, key=lambda p: -p[1])[:cap]
        s = sum(x for _, x in acc)
        if not acc or not np.isfinite(s) or s <= 0 or any(x < 0 for _, x in acc):
            return 'unusable compound weights'
        acc = [(m, x / s) for m, x in acc]
        if nm:
            if any(m not in local for m, _ in acc):
                return 'expands to a node missing from the section node map'
            acc = [(local[m], x) for m, x in acc]
        return acc

    changed = 0
    for v in hit:
        acc = expand(v)
        if isinstance(acc, str):
            bad[acc] += 1
            continue
        out[v, 12:16] = [m for m, _ in acc] + [0] * (4 - len(acc))
        if vdecl != 2:
            out[v, 16:20] = [_q255(x) for _, x in acc] + [0] * (4 - len(acc))
        changed += 1
    if warn:
        for why, c in sorted(bad.items()):
            warn('%d skinned vertices reference a compound node and were left as they are: %s' % (c, why))
    return out, changed


def _cv_uv16(a):
    return _f32bytes(_i16_centre(_view(a, 0, '<i2', 2)))


def _cv_lmuv16(a):
    return _f32bytes(_i16_lightmap(_view(a, 0, '<i2', 2)))


def _cv_tbn(a):
    d = _view(a, 0, '<u4', 3)
    return _f32bytes(_dec3n(d[:, 0]), _dec3n(d[:, 1]), _dec3n(d[:, 2]))


def _cv_dec3n1(a):
    return _f32bytes(_dec3n(_view(a, 0, '<u4', 1)[:, 0]))


def _cv_rgb(a):
    return np.concatenate([a, np.zeros((len(a), 1), dtype=np.uint8)], axis=1)


def _cv_tbn_vertex(a):
    """DECR / PRTM: float3 pos | 3 dec3n | float2 uv"""
    d = _view(a, 12, '<u4', 3)
    return np.concatenate([a[:, :12], _f32bytes(_dec3n(d[:, 0]), _dec3n(d[:, 1]), _dec3n(d[:, 2])), a[:, 24:32]], axis=1)


def _cv_decal(a):
    """float3 pos | uv16x2 | rgba -> float3 | uv f2 | uv f2 | rgba"""
    uv = _f32bytes(_i16_lightmap(_view(a, 12, '<i2', 2)))
    return np.concatenate([a[:, :12], uv, uv, a[:, 16:20]], axis=1)


def _cv_sprite(a):
    """float3 pos | dec3n | dec3n | uv16x2 | rgba -> float3 | 3f | 3f | uv f2 | rgba"""
    d = _view(a, 12, '<u4', 2)
    uv = _f32bytes(_i16_lightmap(_view(a, 20, '<i2', 2)))
    return np.concatenate([a[:, :12], _f32bytes(_dec3n(d[:, 0]), _dec3n(d[:, 1])), uv, a[:, 24:28]], axis=1)


def _cv_prt(a):
    n = _view(a, 0, '<u2', 1)[:, 0].astype(np.float32)
    return np.concatenate([_f32bytes(n), a[:, 2:10]], axis=1)


class Kind(object):
    def __init__(self, name, xdecl, xstride, vdecl, vstride, fn, vlayout, tol):
        self.name, self.xdecl, self.xstride, self.vdecl, self.vstride = name, xdecl, xstride, vdecl, vstride
        self.fn = fn
        self.vlayout = vlayout      # Vista vertex layout for comparisons: list of (kind, count): 'f' float32 / 'b' byte
        self.tol = tol              # absolute float tolerance of the conversion vs the original Vista floats


# layouts: 'f' = float32 components, 'b' = raw bytes (colour / node index / weights / copied data), 'x' = padding byte (not compared)
KINDS = {}


def _k(name, xd, xs, vd, vs, fn, vlayout, tol):
    KINDS[(xd, xs)] = Kind(name, xd, xs, vd, vs, fn, vlayout, tol)


_k('unused', 0, 0, 0, 0, _cv_copy, [], 0.0)
_k('float3 position', 1, 12, 1, 12, _cv_copy, [('f', 3)], 0.0)
_k('mode position16', 2, 6, 1, 12, _cv_pos16, [('f', 3)], 1.6e-5)
_k('mode skinned1', 4, 8, 2, 16, _cv_skin(2, 0, 0), [('f', 3), ('b', 4)], 1.6e-5)
_k('mode skinned2', 6, 12, 4, 20, _cv_skin(2, 2, 2), [('f', 3), ('b', 8)], 1.6e-5)
_k('mode skinned3', 8, 12, 6, 20, _cv_skin(3, 3, 0), [('f', 3), ('b', 8)], 1.6e-5)
_k('mode skinned4', 10, 16, 8, 20, _cv_skin(4, 4, 2), [('f', 3), ('b', 8)], 1.6e-5)
_k('float2 texcoord', 24, 8, 23, 8, _cv_copy, [('f', 2)], 0.0)
_k('texcoord16', 25, 4, 23, 8, _cv_uv16, [('f', 2)], 1.6e-5)
_k('tangent frame', 27, 12, 25, 36, _cv_tbn, [('f', 9)], 2.0e-3)
_k('lightmap texcoord16', 31, 4, 29, 8, _cv_lmuv16, [('f', 2)], 3.2e-5)
_k('rgb', 47, 3, 46, 4, _cv_rgb, [('b', 3), ('x', 1)], 0.0)
_k('packed vector', 48, 4, 47, 12, _cv_dec3n1, [('f', 3)], 2.0e-3)
_k('DECR vertex', 51, 32, 49, 56, _cv_tbn_vertex, [('f', 12), ('f', 2)], 2.0e-3)
_k('PRTM vertex', 61, 32, 59, 56, _cv_tbn_vertex, [('f', 12), ('f', 2)], 2.0e-3)
_k('decal vertex', 53, 20, 51, 32, _cv_decal, [('f', 3), ('f', 4), ('b', 4)], 3.2e-5)
_k('sprite vertex', 55, 28, 53, 48, _cv_sprite, [('f', 3), ('f', 6), ('f', 2), ('b', 4)], 2.0e-3)
_k('PRT info vertex', 57, 10, 56, 12, _cv_prt, [('f', 1), ('b', 8)], 0.0)


# --------------------------------------------------------------------------------------------------------------------
# block parsing / packing
# --------------------------------------------------------------------------------------------------------------------
def _align(v, a):
    return (v + a - 1) & ~(a - 1)


def split_block(block, section_data_size, warn=None):
    """-> (section_data bytes, resource_data bytes).  The trailing 'fklb' is required; the 8-byte header is NOT trusted (some modded
    Xbox maps carry blocks whose header is {0, block_size} instead of {'blkh', rds}); the resource data size is block size - 12 - sds."""
    if block[-4:] != BLK_TAIL:
        raise GeometryConvertError("not a geometry block (no 'fklb' trailer)")
    rds = len(block) - 12 - section_data_size
    if rds < 0:
        raise GeometryConvertError('block size %d is smaller than 12 + sds %d' % (len(block), section_data_size))
    if block[:4] != BLK_MAGIC or struct.unpack_from('<I', block, 4)[0] != rds:
        if warn:
            warn("geometry block header is not {'blkh', rds}: %s (rebuilt)" % block[:8].hex())
    return block[8:8 + section_data_size], block[8 + section_data_size:8 + section_data_size + rds]


def pack_resource_data(order_res, payloads):
    """Rebuild the resource data.  order_res: list[Res] sorted by old offset; payloads: dict id(Res) -> bytes.  Updates .offset/.size.
    Returns the new resource data (bytes)."""
    out = bytearray()
    for r in order_res:
        data = payloads[id(r)]
        pos = _align(len(out), 16 if r.type == RES_DATA else 4)
        out += b'\0' * (pos - len(out))
        out += CRSR
        r.offset = len(out)
        r.size = len(data)
        out += data
    out += b'\0' * (_align(len(out), 4) - len(out))
    return bytes(out)


def convert_block(block_bytes, resources, section_data_size, group='', site='', warn=None, compound=None, stats=None):
    """Convert one Xbox geometry block to Vista.

    block_bytes        'hklb' ... 'fklb' bytes (geometry_block_info.block_offset/size, location bits resolved by the caller)
    resources          the "Resources" fixup entries of that geometry_block_info (see parse_resources for the accepted forms)
    section_data_size  geometry_block_info +0x08
    group, site        only used in error messages (e.g. 'mode', 'Sections')
    compound           mode Sections only: (len(mode Nodes), Compound Nodes of the section's Section Group as [(node bytes[4],
                       weights[3]), ...]) - skinned vertices that reference a compound node are expanded (expand_compound_nodes)
    stats              optional Counter: 'compound_vertices_expanded' += the vertices expand_compound_nodes changed
    Returns (vista_block_bytes, vista_resources, vista_section_data_size, vista_resource_data_size) where vista_resources is a list of
    7-tuples (type, flags, u16, primary, secondary, size, offset) in the SAME order as the input; section data size is unchanged."""
    res = parse_resources(resources)
    sd, rd = split_block(block_bytes, section_data_size, warn)
    sd = bytearray(sd)
    where = '%s %s' % (group, site)

    # resource payloads.  Every resource must sit behind its 'crsr' marker.  Padding/garbage between resources is dropped (warn).
    order = sorted(range(len(res)), key=lambda i: res[i].offset)
    pos = 0
    payloads = {}
    avail = {}
    for j, i in enumerate(order):
        r = res[i]
        m = r.offset - 4
        if m < pos:
            raise GeometryConvertError('%s: resource at offset %d overlaps the previous one' % (where, r.offset))
        if rd[m:m + 4] != CRSR and warn:
            warn('%s: no crsr marker before the resource at %d (%s)' % (where, r.offset, rd[m:m + 4].hex()))
        if r.offset + r.size > len(rd):
            raise GeometryConvertError('%s: resource outside the resource data' % where)
        if warn and any(rd[pos:m]):
            warn('%s: %d unreferenced non-zero bytes before resource at %d dropped' % (where, m - pos, r.offset))
        payloads[id(r)] = rd[r.offset:r.offset + r.size]
        nxt = res[order[j + 1]].offset - 4 if j + 1 < len(order) else len(rd)
        avail[id(r)] = nxt - r.offset                 # room up to the next marker (>= size)
        pos = r.offset + r.size
    if warn and any(rd[pos:]):
        warn('%s: non-zero bytes after the last resource dropped' % where)

    # vertex buffer descriptors
    desc_blocks = {}            # primary locator -> Res of the descriptor block
    for r in res:
        if r.type == RES_BLOCK and r.secondary == DESC_SIZE:
            desc_blocks[r.primary] = r
    desc_data = {k: bytearray(payloads[id(r)]) for k, r in desc_blocks.items()}
    vbs = []                    # (res, container bytearray, offset of the descriptor in it, Kind, group key)
    for r in res:
        if r.type != RES_VERTEX:
            continue
        if r.secondary >= 0:
            blk = desc_data.get(r.primary)
            if blk is None or (r.secondary + 1) * DESC_SIZE > len(blk):
                raise GeometryConvertError('%s: vertex buffer %d has no descriptor block' % (where, r.secondary))
            o = r.secondary * DESC_SIZE
            gkey = r.primary
        else:
            o = r.primary
            blk = sd
            gkey = ('single', id(r))
        decl, stride = blk[o], blk[o + 1]
        kind = KINDS.get((decl, stride))
        if kind is None:
            raise GeometryConvertError('%s: unsupported Xbox vertex format (declaration %d, stride %d)' % (where, decl, stride))
        vbs.append((r, blk, o, kind, gkey))

    # Repair wrong fixup sizes.  Some modded maps record a wrong vertex-buffer size (e.g. 4 bytes/vertex for a 12-byte tangent-frame
    # buffer); the data is all there, the next marker is further away.  A size is trusted when it is a whole number of vertices
    # and fills the room up to the next marker (<= 3 bytes of alignment padding).  All buffers of one descriptor block have the same
    # vertex count, so a suspect buffer takes the most common count of the trusted ones.
    groups = {}
    for r, blk, o, kind, gkey in vbs:
        if kind.xstride:
            groups.setdefault(gkey, []).append((r, kind))
    for gkey, lst in groups.items():
        counts = [len(payloads[id(r)]) // k.xstride for r, k in lst
                  if len(payloads[id(r)]) % k.xstride == 0 and avail[id(r)] - len(payloads[id(r)]) <= 3]
        consensus = None
        if counts:
            consensus = max(set(counts), key=lambda c: (counts.count(c), c))
        for r, k in lst:
            size = len(payloads[id(r)])
            if size % k.xstride == 0 and avail[id(r)] - size <= 3:
                continue
            n = consensus if consensus is not None and consensus * k.xstride <= avail[id(r)] else avail[id(r)] // k.xstride
            if n * k.xstride == size:
                continue
            if warn:
                warn('%s: vertex buffer (decl %d) fixup size %d repaired to %d (%d vertices)' % (where, k.xdecl, size, n * k.xstride, n))
            payloads[id(r)] = rd[r.offset:r.offset + n * k.xstride]
        # buffers of one descriptor block must hold the same number of vertices (a few modded weapons carry a tangent-frame buffer with
        # 132 entries next to 1394 positions): pad the short ones with zero vertices instead of letting the GPU read past the buffer.
        cn = {id(r): len(payloads[id(r)]) // k.xstride for r, k in lst}
        if len(set(cn.values())) > 1:
            vals = list(cn.values())
            tgt = max(set(vals), key=lambda c: (vals.count(c), c))
            for r, k in lst:
                if cn[id(r)] < tgt:
                    if warn:
                        warn('%s: vertex buffer (decl %d) has %d vertices, its siblings %d: padded with zero vertices' % (where, k.xdecl, cn[id(r)], tgt))
                    payloads[id(r)] = payloads[id(r)] + bytes((tgt - cn[id(r)]) * k.xstride)

    for r, blk, o, kind, gkey in vbs:
        data = payloads[id(r)]
        if kind.xstride == 0:
            if data:
                raise GeometryConvertError('%s: data behind an unused vertex descriptor' % where)
            continue
        if len(data) % kind.xstride:
            raise GeometryConvertError('%s: vertex buffer size %d is not a multiple of stride %d' % (where, len(data), kind.xstride))
        n = len(data) // kind.xstride
        out = None
        if kind.xdecl == 55:
            # decorator sprite vertices: the block also carries an uncompressed float copy of the same vertices (type 0 resource of 48-byte
            # elements, primary locator = this buffer's primary - 16).  Vista's vertex buffer is byte-identical to that copy (152/152 Vista
            # blocks), and the copy is 800x closer to Vista than the decoded 28-byte vertices (max err 1.9e-6 vs 1.6e-3) -> use it.
            cp = next((q for q in res if q.type == RES_BLOCK and q.primary == r.primary - 16 and q.secondary == 48 and q.size == n * 48), None)
            if cp is not None:
                out = np.frombuffer(payloads[id(cp)], dtype=np.uint8).reshape(n, 48)
        if out is None:
            out = kind.fn(np.frombuffer(data, dtype=np.uint8).reshape(n, kind.xstride))
        if out.shape != (n, kind.vstride):
            raise GeometryConvertError('%s: internal error, %s produced %s' % (where, kind.name, out.shape))
        if compound and kind.vdecl in SKIN_NODES:
            nmr = next((q for q in res if q.type == RES_BLOCK and q.primary == NODE_MAP_PRIMARY and q.secondary == 1), None)
            out, nexp = expand_compound_nodes(out, kind.vdecl, list(payloads[id(nmr)]) if nmr is not None else None,
                                              compound[0], compound[1], warn=(lambda m: warn('%s: %s' % (where, m))) if warn else None)
            if nexp and stats is not None:
                stats['compound_vertices_expanded'] += nexp
        payloads[id(r)] = np.ascontiguousarray(out).tobytes()
        blk[o] = kind.vdecl
        blk[o + 1] = kind.vstride
    for k, r in desc_blocks.items():
        payloads[id(r)] = bytes(desc_data[k])

    # sbsp Water Definitions: the Xbox index buffer is a QUAD list (4 indices per quad, D3DPT_QUADLIST), Vista wants a triangle list.
    # quad (a, b, c, d) -> triangles (a, b, d) and (d, b, c) (proven on headlong: 5376 Xbox indices -> 8064 Vista indices, byte-identical).
    # The index count is stored in the section data at +0x20 (u32).
    if 'water' in ('%s %s' % (group, site)).lower():
        ib = next((r for r in res if r.type == RES_BLOCK and r.primary == WATER_INDEX_PRIMARY and r.secondary == 2), None)
        if ib is not None:
            idx = np.frombuffer(payloads[id(ib)], dtype='<u2')
            if len(idx) % 4:
                raise GeometryConvertError('%s: water index count %d is not a multiple of 4' % (where, len(idx)))
            q = idx.reshape(-1, 4)
            tri = np.stack([q[:, 0], q[:, 1], q[:, 3], q[:, 3], q[:, 1], q[:, 2]], axis=1).astype('<u2')
            payloads[id(ib)] = tri.tobytes()
            old_count = struct.unpack_from('<I', sd, WATER_INDEX_COUNT_AT)[0]
            if old_count != len(idx) and warn:
                warn('%s: water index count in the section data (%d) != index buffer (%d)' % (where, old_count, len(idx)))
            struct.pack_into('<I', sd, WATER_INDEX_COUNT_AT, tri.size)

    new_rd = pack_resource_data([res[i] for i in order], payloads)
    new_block = BLK_MAGIC + struct.pack('<I', len(new_rd)) + bytes(sd) + new_rd + BLK_TAIL
    return new_block, [r.tup() for r in res], len(sd), len(new_rd)


def vertex_buffers(block_bytes, resources, section_data_size):
    """Debug/verification helper: [(Res, (decl, stride), payload bytes)] of the type-2 resources of an Xbox OR Vista block."""
    res = parse_resources(resources)
    sd, rd = split_block(block_bytes, section_data_size)
    desc_blocks = {r.primary: r for r in res if r.type == RES_BLOCK and r.secondary == DESC_SIZE}
    out = []
    for r in res:
        if r.type != RES_VERTEX:
            continue
        if r.secondary >= 0:
            b = desc_blocks[r.primary]
            o = b.offset + r.secondary * DESC_SIZE
            dd = (rd[o], rd[o + 1])
        else:
            dd = (sd[r.primary], sd[r.primary + 1])
        out.append((r, dd, rd[r.offset:r.offset + r.size]))
    return out
