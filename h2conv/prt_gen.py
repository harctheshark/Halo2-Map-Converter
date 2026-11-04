# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Precomputed radiance transfer (PRT) of render models: checking, repairing from Bungie's data, and a neutral generator.

A render model ('mode' tag) that is lit by spherical harmonics carries a 'PRT Info' block: per vertex a cluster number and four weights
(the PRT vertex stream), and per cluster a mean plus four principal vectors of the vertices' transfer (the Cluster Basis).  The game
combines them with the SH light of the object on the GPU (vertex shader rasterizer\\vertex_shaders_dx9\\prt: four weights, so four
principal vectors are fixed; one constant register block per cluster, so the cluster count is limited to 34).

Some modded maps carry models whose PRT block was damaged: the header is zeroed (no clusters, no SH order), or the geometry block of the
stream is destroyed.  Three outcomes, in this order of preference:

1. The model is a stock campaign model (same geometry as Bungie's copy in a stock Vista campaign map): Bungie's own PRT Info is used
   (adopt_prt).  "Same geometry" is proven by comparing the converted sections with the stock sections (compare_sections), never by name.
2. Otherwise a NEUTRAL PRT is generated from the model's own vertex normals (neutral_prt): the transfer of an unshadowed surface,
   clamped cosine / pi, expanded in the SH basis of the game and compressed with the same clustered PCA the stock data uses.
3. If the sections themselves are unreadable and no stock copy fits, nothing can be done and the model is reported.

This module reads and writes decoded Vista 'mode' tags (h2conv.tagtree nodes); it imports nothing from h2conv.xbox and serves any map.

Encoding (verified on the stock Vista models, spec section 6):
    PRT Info element (0x58 bytes)   SH order 3, clusters K, PCA vectors 4, rays 1024, bounces 1, length scale 25.0, LODs 1
    LOD Info (1 element)            Cluster Offset 0, Section Info = every model section: (section index, PCA offset = 5 x vertices before it)
    Cluster Basis                   K x 5 vectors (mean, then the four PCA vectors) x 27 floats (R[9] G[9] B[9], the three channels equal)
    PRT geometry block              'hklb' | 32 bytes section data (38 0C 00 00 + zeros: declaration 56, stride 12) | one resource of
                                    12 x N bytes (type 2, align 2, secondary -1): per vertex float32 cluster number + 4 x int16 weights
                                    (SHORT4N: weight / 32767) | 'fklb'
Unshadowed transfer of a vertex with normal n:  T_i = (A_i / pi) * Y_i(n),  A = (pi, 2pi/3 x3, pi/4 x5)  ->  coefficient vector
[1, 2/3, 2/3, 2/3, 1/4 x5] * Y(n) in the D3DX basis (Y_0 = 1 / (2 sqrt(pi)))."""
import collections
import math
import struct

from . import tagtree as T
from .handlers import geometry as geo

SH_ORDER = 3
SH_COEFFS = 9
PCA_VECTORS = 4
VECTORS = PCA_VECTORS + 1                           # mean + the PCA vectors
DEFAULT_CLUSTERS = 16                               # shading error under 7 % of the peak (spec 6.5); Bungie's own models use 10
MAX_CLUSTERS = 32                                   # the vertex shader constant file allows 34 (c70 + 4 K <= c207)
RAYS, BOUNCES, LENGTH_SCALE = 1024, 1, 25.0         # header values of every stock model
WEIGHT_SCALE = 32767.0                              # SHORT4N
LOBE = (1.0, 2.0 / 3, 2.0 / 3, 2.0 / 3, 0.25, 0.25, 0.25, 0.25, 0.25)
STREAM_STRIDE = 12                                  # decl 56: float32 cluster + 4 x int16
DECL_PRT = 56
PRT_SECTION_DATA = bytes([DECL_PRT, STREAM_STRIDE, 0, 0]) + bytes(28)
PRT_OFFSET_UNIT = 5                                 # PCA Data Offset = 5 x vertices before the section
OWNER_SECTION_OFFSET, GEOMETRY_FLAGS, OWNER_UNKNOWN = -8, 0x0900, -1    # PRT Info +0x50 / +0x52 / +0x54 in every stock model
TANGENT_FRAME_DECL = 25                             # vertex stream: float3 normal, tangent, binormal (stride 36)

# node offsets
PRT_INFO_BLOCK = 0x74                               # mode root: PRT Info block
PRT_GEOMETRY_INFO = 0x34                            # PRT Info element: geometry_block_info (offset, size, sds, rds), Resources at +0x44
SECTION_GEOMETRY_INFO = 0x38                        # Sections element: geometry_block_info, Resources at +0x48
GEOMETRY_TOLERANCE = 0.01                           # vertex data of a converted Xbox model vs Bungie's PC export: SHORT quantisation (largest 2e-3)
SMALL_TOLERANCE = 1e-5                              # non-vertex float data (the section's bounds)

# status
OK, HEADER, GEOMETRY, SECTIONS = 'ok', 'header', 'geometry', 'sections'


class PrtError(ValueError):
    """The model cannot be read as a Vista 'mode' tag with PRT Info."""


# ---------------------------------------------------------------------------------------------------------------------
# the math
# ---------------------------------------------------------------------------------------------------------------------
def sh_basis(n):
    """D3DX order-3 real SH basis (as the game evaluates it) of unit vectors n (N, 3) -> (N, 9) float64."""
    import numpy as np
    n = np.asarray(n, np.float64).reshape(-1, 3)
    x, y, z = n[:, 0], n[:, 1], n[:, 2]
    inv = 1.0 / math.sqrt(math.pi)
    s3, s5, s15 = math.sqrt(3.0), math.sqrt(5.0), math.sqrt(15.0)
    out = np.empty((len(n), 9), np.float64)
    out[:, 0] = 0.5 * inv
    out[:, 1] = -0.5 * s3 * inv * y
    out[:, 2] = 0.5 * s3 * inv * z
    out[:, 3] = -0.5 * s3 * inv * x
    out[:, 4] = 0.5 * s15 * inv * x * y
    out[:, 5] = -0.5 * s15 * inv * y * z
    out[:, 6] = 0.25 * s5 * inv * (3.0 * z * z - 1.0)
    out[:, 7] = -0.5 * s15 * inv * z * x
    out[:, 8] = 0.25 * s15 * inv * (x * x - y * y)
    return out


def unit_normals(n):
    """Normals as unit vectors; a zero / non-finite normal becomes (0, 0, 1)."""
    import numpy as np
    n = np.asarray(n, np.float64).reshape(-1, 3).copy()
    ln = np.sqrt((n * n).sum(1))
    bad = ~np.isfinite(ln) | (ln < 1e-6)
    n[bad] = (0.0, 0.0, 1.0)
    ln[bad] = 1.0
    return n / ln[:, None]


def unshadowed_transfer(normals):
    """(N, 9) transfer coefficients of unshadowed Lambertian surfaces with these normals."""
    import numpy as np
    return sh_basis(unit_normals(normals)) * np.array(LOBE)


def fibonacci_sphere(k):
    import numpy as np
    i = np.arange(k, dtype=np.float64) + 0.5
    phi = np.arccos(1.0 - 2.0 * i / k)
    theta = math.pi * (1.0 + math.sqrt(5.0)) * i
    return np.stack([np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)], 1)


def cluster_normals(normals, k, iterations=8):
    """k-means on unit normals: Fibonacci sphere seeds, Lloyd iterations on the sphere (centroid = normalised mean), nearest centroid by dot
    product.  -> (cluster id per vertex, centroids (k, 3)).  Deterministic."""
    import numpy as np
    n = unit_normals(normals)
    c = fibonacci_sphere(k)
    for _ in range(iterations):
        ids = np.argmax(n @ c.T, 1)
        for j in range(k):
            m = ids == j
            if m.any():
                v = n[m].sum(0)
                ln = float(np.sqrt((v * v).sum()))
                if ln > 1e-9:
                    c[j] = v / ln
    return np.argmax(n @ c.T, 1), c


class PrtData(object):
    """A model's PRT: basis (K, 5, 3, 9) float32, cluster ids (N,), weights (N, 4) int16 (SHORT4N)."""

    def __init__(self, basis, ids, weights):
        self.basis, self.ids, self.weights = basis, ids, weights

    @property
    def clusters(self):
        return self.basis.shape[0]

    @property
    def vertices(self):
        return len(self.ids)

    def basis_floats(self):
        import numpy as np
        return np.ascontiguousarray(self.basis, '<f4').reshape(-1)

    def stream(self):
        """The PRT vertex stream: per vertex float32 cluster number + 4 x int16 weights (12 bytes)."""
        import numpy as np
        a = np.zeros((self.vertices, STREAM_STRIDE), np.uint8)
        a[:, 0:4] = np.ascontiguousarray(self.ids.astype('<f4')).view(np.uint8).reshape(-1, 4)
        a[:, 4:12] = np.ascontiguousarray(self.weights.astype('<i2')).view(np.uint8).reshape(-1, 8)
        return a.tobytes()

    def reconstruct(self):
        """(N, 3, 9) transfer vectors as the game decodes them: mean + sum_p (weight_p / 32767) * vector_p."""
        import numpy as np
        w = self.weights.astype(np.float64) / WEIGHT_SCALE
        b = self.basis.astype(np.float64)[self.ids]                       # (N, 5, 3, 9)
        return b[:, 0] + np.einsum('np,npcs->ncs', w, b[:, 1:])


def compress(transfer, ids, k):
    """Clustered PCA of per-vertex transfer vectors (N, 9) (identical in R, G, B) -> PrtData.  Per cluster: mean and the four right-singular
    vectors of the centred transfer; vector p is scaled by the largest |projection| so the weight is a fraction of it (SHORT4N);
    a cluster with fewer than four significant vectors gets zero vectors and zero weights for the rest."""
    import numpy as np
    n = len(transfer)
    basis = np.zeros((k, VECTORS, 3, SH_COEFFS), np.float64)
    weights = np.zeros((n, PCA_VECTORS), np.int16)
    for j in range(k):
        m = ids == j
        if not m.any():
            continue
        t = transfer[m]
        mean = t.mean(0)
        d = t - mean
        basis[j, 0, :, :] = mean
        if len(t) < 2:
            continue
        _, s, vt = np.linalg.svd(d, full_matrices=False)
        cols = []
        for p in range(min(PCA_VECTORS, len(s))):
            if s[p] <= 1e-9:
                break
            v = vt[p].copy()
            if v[int(np.argmax(np.abs(v)))] < 0:                           # sign convention: the largest component is positive
                v = -v
            proj = d @ v
            sp = float(np.max(np.abs(proj)))
            if sp < 1e-9:
                break
            basis[j, 1 + p, :, :] = v * sp
            cols.append((p, proj / sp))
        w = np.zeros((int(m.sum()), PCA_VECTORS), np.int16)
        for p, q in cols:
            w[:, p] = np.rint(np.clip(q, -1.0, 1.0) * WEIGHT_SCALE).astype(np.int16)
        weights[m] = w
    return PrtData(basis.astype(np.float32), ids.astype(np.int64), weights)


def neutral_prt(normals, clusters=DEFAULT_CLUSTERS):
    """The neutral PRT of vertices with these normals (N, 3): unshadowed transfer, clustered by normal, compressed to 4 PCA vectors."""
    if not 1 <= clusters <= MAX_CLUSTERS:
        raise PrtError('cluster count %d outside 1..%d' % (clusters, MAX_CLUSTERS))
    import numpy as np
    n = unit_normals(normals)
    if len(n) == 0:
        return PrtData(np.zeros((clusters, VECTORS, 3, SH_COEFFS), np.float32), np.zeros(0, np.int64), np.zeros((0, PCA_VECTORS), np.int16))
    ids, _ = cluster_normals(n, clusters)
    return compress(unshadowed_transfer(n), ids, clusters)


def shading_error(prt, normals, lights=64, seed=1):
    """Error of the decoded transfer against the analytic unshadowed transfer, in SHADING: max |(T_hat - T) . Y(d)| over `lights` random
    unit light directions and every vertex -> (max, mean, peak shading of the exact transfer)."""
    import numpy as np
    rng = np.random.RandomState(seed)
    d = rng.normal(size=(lights, 3))
    y = sh_basis(unit_normals(d))                                          # (L, 9)
    exact = unshadowed_transfer(normals) @ y.T                              # (N, L)
    got = prt.reconstruct()[:, 0, :] @ y.T
    err = np.abs(got - exact)
    return float(err.max()), float(err.mean()), float(exact.max())


# ---------------------------------------------------------------------------------------------------------------------
# the PRT geometry block
# ---------------------------------------------------------------------------------------------------------------------
def prt_block(stream):
    """-> (raw block bytes, section data size, resource data size, resource entry bytes) of a PRT vertex stream (N x 12 bytes)."""
    entry = geo.Entry(2, 2, 0, 0, -1, len(stream), 4)
    blk, ents, rds = geo.layout_block(PRT_SECTION_DATA, [stream], [entry])
    return blk, len(PRT_SECTION_DATA), rds, [geo.encode_entry(e) for e in ents]


def read_prt_stream(blk, sds, rds, entries):
    """-> (cluster ids float array (N,), weights int16 (N, 4)) of a PRT block."""
    import numpy as np
    sd, pays = geo.split_block(blk, sds, rds, entries)
    vs = [p for e, p in zip(entries, pays) if e.type == geo.RES_VERTEX_BUFFER]
    if len(vs) != 1 or len(vs[0]) % STREAM_STRIDE:
        raise geo.GeometryError('PRT block holds %d vertex buffers (%d bytes)' % (len(vs), len(vs[0]) if vs else 0))
    a = np.frombuffer(vs[0], np.uint8).reshape(-1, STREAM_STRIDE)
    return a[:, 0:4].copy().view('<f4')[:, 0], a[:, 4:12].copy().view('<i2').reshape(-1, 4)


# ---------------------------------------------------------------------------------------------------------------------
# reading a model
# ---------------------------------------------------------------------------------------------------------------------
Section = collections.namedtuple('Section', 'head sd entries pays')    # head = counts / bounds of the element; sd None = unreadable block


def read_sections(root, raw_of):
    """The render model's sections.  raw_of(node, info_offset) -> the raw geometry block bytes of that geometry_block_info, or None."""
    out = []
    for n in root.block('Sections'):
        head = bytes(n.data[0:0x1C]) + bytes(n.data[0x24:0x30])
        blk = raw_of(n, SECTION_GEOMETRY_INFO)
        sd = ents = pays = None
        if blk is not None and n.u32(SECTION_GEOMETRY_INFO + 4):
            try:
                ents = [geo.decode_entry(r.data) for r in n.blocks.get(SECTION_GEOMETRY_INFO + 0x10, [])]
                sd, pays = geo.split_block(blk, n.u32(SECTION_GEOMETRY_INFO + 8), n.u32(SECTION_GEOMETRY_INFO + 12), ents)
            except (geo.GeometryError, struct.error):
                sd = ents = pays = None
        out.append(Section(head, sd, ents, pays))
    return out


def section_vertex_count(sec):
    return struct.unpack_from('<H', sec.head, 4)[0]


def section_normals(sec, count):
    """(count, 3) normals of a section from its tangent-frame vertex stream (declaration 25: float3 normal, tangent, binormal), or None."""
    import numpy as np
    if sec.sd is None:
        return None
    decls = []
    for e, p in zip(sec.entries, sec.pays):
        if e.type == geo.RES_TAG_BLOCK and e.secondary == 32:
            decls = [(p[k * 32], p[k * 32 + 1]) for k in range(len(p) // 32)]
    for e, p in zip(sec.entries, sec.pays):
        if e.type != geo.RES_VERTEX_BUFFER or not 0 <= e.secondary < len(decls):
            continue
        decl, stride = decls[e.secondary]
        if decl == TANGENT_FRAME_DECL and stride >= 12 and stride % 4 == 0 and len(p) == stride * count:
            return np.frombuffer(p, '<f4').reshape(count, stride // 4)[:, 0:3].astype(np.float64)
    return None


def model_normals(secs):
    """-> (normals (N, 3) over all sections in order, [vertices per section], number of sections whose normals were missing)."""
    import numpy as np
    parts, counts, missing = [], [], 0
    for s in secs:
        c = section_vertex_count(s)
        nm = section_normals(s, c)
        if nm is None:
            nm = np.tile(np.array([[0.0, 0.0, 1.0]]), (c, 1))
            missing += 1 if c else 0
        parts.append(nm)
        counts.append(c)
    return (np.concatenate(parts) if parts else np.zeros((0, 3))), counts, missing


Report = collections.namedtuple('Report', 'status problems')


def check_prt(root, raw_of, secs=None):
    """Is the model's PRT usable by the game?  -> Report(status, problems); status OK, HEADER (zeroed / inconsistent numbers),
    GEOMETRY (the stream block is missing, damaged or does not fit the sections) or None for a model without PRT Info.
    The rules hold for every stock model (314 PRT Info blocks of the original maps, 0 false alarms): SH order, clusters and PCA vectors
    non zero, Cluster Basis = LODs x clusters x 5 x 3 x order^2 floats, every Section Info index inside the model, PCA offsets = 5 x the
    vertices before the section, PRT stream = the vertices of all listed sections, cluster ids inside the cluster count."""
    import numpy as np
    prt = root.block('PRT Info')
    if not prt:
        return Report(None, [])
    p = prt[0]
    prob = []
    order, k, pca = p.u16(0), p.u16(2), p.u16(4)
    lods = p.block('LOD Info')
    if order == 0 or k == 0 or pca == 0:
        prob.append('SH order %d, %d clusters, %d PCA vectors' % (order, k, pca))
    elif len(p.block('Cluster Basis')) != len(lods) * k * (pca + 1) * 3 * order * order:
        prob.append('Cluster Basis holds %d floats, %d expected' % (len(p.block('Cluster Basis')), len(lods) * k * (pca + 1) * 3 * order * order))
    elif not np.isfinite(np.array([c.f32(0) for c in p.block('Cluster Basis')], np.float32)).all():
        prob.append('Cluster Basis has non-finite values')
    if not lods:
        prob.append('no LOD Info')
    if prob:
        return Report(HEADER, prob)
    secs = secs if secs is not None else read_sections(root, raw_of)
    gprob = []
    total = 0
    for li, lod in enumerate(lods):
        for si in lod.block('Section Info'):
            idx, off = si.i32(0), si.u32(4)
            if not 0 <= idx < len(secs):
                gprob.append('Section Info index %d outside the %d sections' % (idx, len(secs)))
                continue
            if off != PRT_OFFSET_UNIT * total:
                gprob.append('PCA Data Offset %d, %d expected' % (off, PRT_OFFSET_UNIT * total))
            total += section_vertex_count(secs[idx])
    blk = raw_of(p, PRT_GEOMETRY_INFO)
    if blk is None or not p.u32(PRT_GEOMETRY_INFO + 4):
        gprob.append('the PRT vertex stream block is missing')
    else:
        try:
            ents = [geo.decode_entry(r.data) for r in p.blocks.get(PRT_GEOMETRY_INFO + 0x10, [])]
            cid, _ = read_prt_stream(blk, p.u32(PRT_GEOMETRY_INFO + 8), p.u32(PRT_GEOMETRY_INFO + 12), ents)
            if len(cid) != total:
                gprob.append('the PRT stream holds %d vertices, the sections %d' % (len(cid), total))
            elif len(cid) and (not np.isfinite(cid).all() or cid.min() < 0 or cid.max() >= k):
                gprob.append('cluster numbers outside 0..%d' % (k - 1))
        except (geo.GeometryError, struct.error) as ex:
            gprob.append('the PRT vertex stream block is damaged (%s)' % str(ex)[:80])
    return Report(GEOMETRY if gprob else OK, gprob)


# ---------------------------------------------------------------------------------------------------------------------
# is this model the stock model?
# ---------------------------------------------------------------------------------------------------------------------
def _words_close(a, b, tol):
    """Bytes a, b (equal size) equal, or equal as float32 words within tol where both words are plausible floats (a changed integer such
    as an index or a packed field reads as a denormal and is NOT close)."""
    import numpy as np
    if a == b:
        return True, 0.0
    if len(a) != len(b) or len(a) % 4:
        return False, float('inf')
    wa, wb = np.frombuffer(a, '<u4'), np.frombuffer(b, '<u4')
    m = wa != wb
    fa, fb = wa[m].view('<f4').astype(np.float64), wb[m].view('<f4').astype(np.float64)
    with np.errstate(all='ignore'):
        plausible = (np.isfinite(fa) & np.isfinite(fb) & ((np.abs(fa) >= 1e-20) | (fa == 0)) & ((np.abs(fb) >= 1e-20) | (fb == 0)))
        if not plausible.all():
            return False, float('inf')
        d = float(np.abs(fa - fb).max())
    return d <= tol, d


Verdict = collections.namedtuple('Verdict', 'kind max_diff why unreadable')


def compare_sections(out, stock, tolerance=GEOMETRY_TOLERANCE):
    """Do the sections of a converted model equal the sections of the stock Vista model?  Both are lists of Section.
    Verdict.kind: 'exact' (every section readable and equal: section data, resource layout, index and other data byte for byte, vertex
    streams within `tolerance` as float32 words), 'struct' (some sections of `out` are unreadable, every readable one is equal and all the
    section headers - vertex / triangle / part counts and bounds - are equal), or 'no'.  unreadable = the indices of the unreadable sections."""
    if len(out) != len(stock):
        return Verdict('no', None, '%d sections, the stock model has %d' % (len(out), len(stock)), [])
    worst, unreadable = 0.0, []
    for i, (a, b) in enumerate(zip(out, stock)):
        if a.head != b.head:
            return Verdict('no', None, 'section %d: counts or bounds differ' % i, [])
        if b.sd is None:
            return Verdict('no', None, 'section %d of the stock model is unreadable' % i, [])
        if a.sd is None:
            unreadable.append(i)
            continue
        if a.sd != b.sd or len(a.entries) != len(b.entries):
            return Verdict('no', None, 'section %d: section data differs' % i, [])
        for j, (ea, eb, pa, pb) in enumerate(zip(a.entries, b.entries, a.pays, b.pays)):
            if ea[:6] != eb[:6]:
                return Verdict('no', None, 'section %d resource %d: layout differs' % (i, j), [])
            ok, d = _words_close(pa, pb, GEOMETRY_TOLERANCE if ea.type == geo.RES_VERTEX_BUFFER else SMALL_TOLERANCE)
            if not ok:
                return Verdict('no', d if d != float('inf') else None, 'section %d resource %d: data differs' % (i, j), [])
            worst = max(worst, d)
    return Verdict('struct' if unreadable else 'exact', worst, '', unreadable)


# ---------------------------------------------------------------------------------------------------------------------
# writing a model
# ---------------------------------------------------------------------------------------------------------------------
def clone(node):
    """A copy of a node and its children (no raw patches)."""
    c = T.Node(node.group, node.path, node.fields, len(node.data), node.data)
    c.src_addr = node.src_addr
    for off, kids in node.blocks.items():
        c.blocks[off] = [clone(k) for k in kids]
    c.datas = dict(node.datas)
    return c


def _new_child(parent, field_name, data=None):
    f = parent.field(field_name)
    return T.Node(parent.group, T.child_path(parent.path, f), f.children, T.element_size(f), data)


def _set_info(node, off, size, sds, rds):
    node.set_u32(off + 4, size)
    node.set_u32(off + 8, sds)
    node.set_u32(off + 12, rds)


def adopt_prt(dst, src, src_block, put_block):
    """Replace the PRT Info element `dst` of a converted model by Bungie's element `src` (a node of the stock Vista model; src_block = its
    raw geometry block): header numbers, LOD Info, Cluster Basis, the stream block and its resource entries.  The owner tag (+0x4C) is the
    caller's.  put_block(node, offset, block bytes) stores the raw block."""
    dst.data[0:0x14] = src.data[0:0x14]
    for name in ('LOD Info', 'Cluster Basis', 'Resources'):
        fd, fs = dst.field(name), src.field(name)
        dst.blocks[fd.offset] = [clone(k) for k in src.blocks.get(fs.offset, [])]
    dst.data[0x50:0x58] = src.data[0x50:0x58]
    put_block(dst, PRT_GEOMETRY_INFO, src_block)
    _set_info(dst, PRT_GEOMETRY_INFO, len(src_block), src.u32(PRT_GEOMETRY_INFO + 8), src.u32(PRT_GEOMETRY_INFO + 12))


def adopt_section(dst, src, src_block, put_block):
    """Give an unreadable section of a converted model the geometry block of the stock section `src` (same counts and bounds)."""
    fd, fs = dst.field('Resources'), src.field('Resources')
    dst.blocks[fd.offset] = [clone(k) for k in src.blocks.get(fs.offset, [])]
    put_block(dst, SECTION_GEOMETRY_INFO, src_block)
    _set_info(dst, SECTION_GEOMETRY_INFO, len(src_block), src.u32(SECTION_GEOMETRY_INFO + 8), src.u32(SECTION_GEOMETRY_INFO + 12))
    dst.data[0x54:0x58] = src.data[0x54:0x58]


def write_neutral(dst, secs, data, put_block):
    """Write a generated PrtData into the PRT Info element `dst`: header, one LOD Info with a Section Info per model section (PCA offsets
    5 x the vertices before it), the Cluster Basis, the stream block and its resource entry."""
    k = data.clusters
    dst.data[0:0x14] = struct.pack('<6HfHH', SH_ORDER, k, PCA_VECTORS, RAYS, BOUNCES, 0, LENGTH_SCALE, 1, 0)[0:0x14]
    lod = _new_child(dst, 'LOD Info', bytes(12))
    sec_field = lod.field('Section Info')
    kids, total = [], 0
    for i, s in enumerate(secs):
        e = T.Node(lod.group, T.child_path(lod.path, sec_field), sec_field.children, T.element_size(sec_field),
                   struct.pack('<iI', i, PRT_OFFSET_UNIT * total))
        kids.append(e)
        total += section_vertex_count(s)
    lod.blocks[sec_field.offset] = kids
    dst.blocks[dst.field('LOD Info').offset] = [lod]
    dst.blocks[dst.field('Cluster Basis').offset] = [_new_child(dst, 'Cluster Basis', struct.pack('<f', v)) for v in data.basis_floats().tolist()]
    blk, sds, rds, ents = prt_block(data.stream())
    dst.blocks[dst.field('Resources').offset] = [_new_child(dst, 'Resources', e) for e in ents]
    for off in (0x24, 0x28, 0x2C, 0x30):
        dst.set_u32(off, 0)
    dst.set_i32(PRT_GEOMETRY_INFO, 0)
    put_block(dst, PRT_GEOMETRY_INFO, blk)
    _set_info(dst, PRT_GEOMETRY_INFO, len(blk), sds, rds)
    struct.pack_into('<hHi', dst.data, 0x50, OWNER_SECTION_OFFSET, GEOMETRY_FLAGS, OWNER_UNKNOWN)
    return blk


def zero_basis(prt):
    """Make the transfer of a PRT Info element zero: every Cluster Basis float becomes 0.0.  The game then uploads (valid header) a block of
    zero constants per cluster and the vertex shader's lighting term is exactly black for every vertex of the model."""
    for c in prt.block('Cluster Basis'):
        c.data[0:4] = bytes(4)
