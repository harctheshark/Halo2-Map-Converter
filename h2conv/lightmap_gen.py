# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Generated lightmaps: a complete 'scenario_structure_lightmap' (ltmp) built from a structure BSP alone.

Some maps carry no usable lightmap: a modified BSP whose lightmap was stripped, or a lightmap that does not belong to its BSP (wrong
checksum, arrays one element short).  The game reads the lightmap's per-cluster and per-instance tables without any bound check, so
such a map needs an ltmp whose tables are sized to ITS BSP.  This module builds one from the BSP's own counts (Vista tag layout; the
Vista -> MCC converter turns the same tag into the MCC one), in two styles:

native         The BSP is drawn exactly as a BSP with no lightmap is: Bitmap Group = NONE, so the game's "lightmap is valid" test
               (a lightmap with a bitmap group whose Structure Checksum equals the BSP's checksum) is false and every structure draw
               uses the no-lightmap shader permutation; objects with precomputed radiance transfer get the white default light.
               Everything the game indexes is still there and correctly sized (stock-shaped tables, buckets, checksum = the BSP's).
neutral-valid  The lightmap IS valid but carries constant lighting: Bitmap Group = a plain grey bitmap (page 2 of rasterizer\\default_2d,
               RGB 128), every cluster / per-pixel instance points at that page, per-vertex buckets hold the constant colour
               (128, 125, 120) and one Lighting Environment gives radiance-transfer objects a white light.

Structure of the result (both styles), all counts taken from the BSP:
    Lightmap Groups            exactly one: Type 0, Flags 2, Structure Checksum = the BSP Checksum, no palettes
    Clusters                   one per BSP cluster: the first 0x54 bytes of the BSP cluster, geometry reference fields cleared
    Cluster Render Info        one per BSP cluster
    Poop Definitions           one per Instanced Geometry Definition, built like Clusters
    Instance Render Info       one per Instanced Geometry Instance
    Instance Bucket Refs       one per instance: flags 0, bucket index, exactly one Section Offset
    Geometry Buckets           per-vertex streams carrying one slice per instance (Total Vertex Count vertices each); a bucket holds at
                               most 32767 vertices; per-pixel instances sit in buckets with flags 1, per-vertex instances in flags 3
    Scenery Object Info/Refs   empty
This module imports nothing from h2conv.xbox: it serves any map whose BSP is available as a decoded tag."""
import collections
import struct

from . import tagtree as T

STYLE_NATIVE = 'native'
STYLE_NEUTRAL_VALID = 'neutral-valid'
STYLES = (STYLE_NATIVE, STYLE_NEUTRAL_VALID)
CHOICES = ('auto',) + STYLES                       # the --lightmap option: auto = the complete stock lightmap of an unmodified stock BSP, else native

NULL = 0xFFFFFFFF
BUCKET_LIMIT = 32767                               # Section Offsets are int16: a bucket never starts a slice past this
NEUTRAL_COLOUR = (128, 125, 120, 0)                # per-vertex colour bytes of neutral-valid (the no-lightmap shader's ambient average); byte 3 stays 0
NEUTRAL_PAGE = 2                                   # rasterizer\default_2d: page 2 is RGB 128
GROUP_FLAGS = 2                                    # Lightmap Group Flags in every stock map
BITMAP_GROUP = 0x6269746D                          # 'bitm'
BUCKET_FLAGS_PER_PIXEL, BUCKET_FLAGS_PER_VERTEX = 1, 3
BSP_BLOCK_PREFIX = 0x54                            # bytes of a BSP cluster / definition struct that a Clusters / Poop Definitions element mirrors
DEFAULT_ENV_SCALE = 1.0625


class LightmapGenError(ValueError):
    """The BSP cannot be read, or the request is inconsistent."""


# ---------------------------------------------------------------------------------------------------------------------
# the neutral light for radiance-transfer objects
# ---------------------------------------------------------------------------------------------------------------------
def no_lightmap_environment():
    """The 220-byte Lighting Environment that lights radiance-transfer (PRT) objects exactly as the game does when the BSP has NO
    lightmap: a white directional light from (0, 0, -1), as order-3 spherical harmonics, all float32 in the game's operation order
    (D3DXSHEvalDirectionalLight: basis x (pi / 1.0625) x intensity).  With zero environments the game uses a green debug light instead
    (stock deltatap / lockout have none); with exactly one it copies that one whatever the position: SH coefficients at +12 / +48 / +84
    (order 3 = 9 each), the direction at +164 (Bump Direction)."""
    import numpy as np
    f = np.float32
    x, y, z = f(0.0), f(0.0), f(-1.0)
    inv = f(1.0) / f(np.sqrt(np.float64(f(np.pi))))
    v8 = f(np.sqrt(3.0)) * inv
    s15 = f(np.sqrt(15.0))
    v35 = s15 * inv
    basis = [inv * f(0.5), (v8 * y) * f(-0.5), (v8 * z) * f(0.5), (v8 * x) * f(-0.5),
             ((y * x) * v35) * f(0.5), ((v35 * y) * z) * f(-0.5),
             ((f(np.sqrt(5.0)) * ((z * z) * f(3.0) - f(1.0))) * inv) * f(0.25),
             ((z * x) * v35) * f(-0.5), (((x * x - y * y) * s15) * inv) * f(0.25)]
    scale = (np.frombuffer(bytes.fromhex('dc0f4940'), '<f4')[0] / f(DEFAULT_ENV_SCALE)) * f(1.0)
    sh = struct.pack('<9f', *[b * scale for b in basis])
    e = bytearray(220)
    e[12:48] = e[48:84] = e[84:120] = sh                                   # red / green / blue: white
    struct.pack_into('<3f', e, 120, x, y, z)                               # Mean Incoming Light Direction
    struct.pack_into('<3f', e, 132, 1.0, 1.0, 1.0)                         # Incoming Light Intensity
    struct.pack_into('<i', e, 144, -1)                                     # Specular Bitmap Index: none (stock)
    struct.pack_into('<3f', e, 164, x, y, z)                               # Bump Direction (= mean direction, as in stock maps)
    return bytes(e)


# ---------------------------------------------------------------------------------------------------------------------
# reading the BSP
# ---------------------------------------------------------------------------------------------------------------------
class BspInfo(object):
    """What the lightmap needs from a structure BSP (a decoded sbsp tag, Vista layout)."""

    def __init__(self, sbsp):
        try:
            self.checksum = sbsp.u32(0x08)                                  # BSP Checksum
            self.clusters = list(sbsp.block('Clusters'))
            self.definitions = list(sbsp.block('Instanced Geometry Definitions'))
            insts = list(sbsp.block('Instanced Geometry Instances'))
        except (KeyError, AttributeError) as ex:
            raise LightmapGenError('not a decoded structure BSP: %s' % ex)
        self.def_vertices = []
        for d in self.definitions:
            f = d.field('Total Vertex Count')
            self.def_vertices.append(d.u16(f.offset) if f is not None else 0)
        self.instances = []                                                 # (definition index, Lightmapping Policy, vertices)
        for i in insts:
            fi, fp = i.field('Instance Definition Index'), i.field('Lightmapping Policy')
            if fi is None or fp is None:
                raise LightmapGenError('the BSP instance layout lacks Instance Definition Index / Lightmapping Policy')
            di, pol = i.i16(fi.offset), i.u16(fp.offset)
            v = self.def_vertices[di] if 0 <= di < len(self.def_vertices) else 0
            self.instances.append((di, pol, v))

    @property
    def n_clusters(self):
        return len(self.clusters)

    @property
    def n_instances(self):
        return len(self.instances)


def plan_buckets(items):
    """items = [(vertex count, Lightmapping Policy)] per instance -> (buckets [[flags, vertex count]], refs [(bucket index, section offset)]).
    Per-pixel (policy 0) instances go to flags-1 buckets, per-vertex (policy 1) to flags-3 buckets, in instance order; the slices tile
    each bucket from 0, and a bucket is closed before it would pass BUCKET_LIMIT vertices.  An instance without vertices has offset -1
    and sits in any flags-3 bucket (as in stock maps); only if no bucket exists at all a one-vertex dummy bucket carries them."""
    buckets, refs, cur, empties = [], [None] * len(items), {}, []
    for k, (v, pol) in enumerate(items):
        if v <= 0:
            empties.append(k)
            continue
        flags = BUCKET_FLAGS_PER_VERTEX if pol else BUCKET_FLAGS_PER_PIXEL
        b = cur.get(flags)
        if b is None or (buckets[b][1] > 0 and buckets[b][1] + v > BUCKET_LIMIT):
            buckets.append([flags, 0])
            b = cur[flags] = len(buckets) - 1
        refs[k] = (b, buckets[b][1])
        buckets[b][1] += v
    if empties:
        b = next((i for i, x in enumerate(buckets) if x[0] == BUCKET_FLAGS_PER_VERTEX), None)
        if b is None:
            if not buckets:
                buckets.append([BUCKET_FLAGS_PER_VERTEX, 1])
            b = 0
        for k in empties:
            refs[k] = (b, -1)
    return buckets, refs


def bucket_raw(flags, n, colour=NEUTRAL_COLOUR):
    """The vertex streams of a bucket as the n x 24 uint8 array lightmap_bucket.bucket_to_cache takes (colour float3 | direction float3):
    direction (0, 0, 0) (a legal value: stock buckets hold zero vectors), colour bytes `colour` for per-vertex buckets, zero otherwise."""
    import numpy as np
    raw = np.zeros((n, 24), np.uint8)
    if flags & 2:
        col = np.empty((n, 3), '<f4')
        for i in range(3):
            col[:, i] = np.float32((colour[i] + 0.5) / 255.0)               # decoded as (b + 0.5) / 255, written back as floor(c * 255)
        raw[:, 0:12] = np.frombuffer(col.tobytes(), np.uint8).reshape(n, 12)
    return raw


def bucket_block(flags, n, colour=NEUTRAL_COLOUR):
    """-> (raw geometry block, section data size, resource data size, [resource entries]) of one bucket."""
    from . import lightmap_bucket as LB
    return LB.bucket_to_cache(bucket_raw(flags, n, colour))


def bucket_vertices(flags, rds):
    """Vertex count of a bucket from its flags and Resource Data Size (rds = 72 + 12 n, or 76 + 16 n with the colour stream)."""
    return (rds - 76) // 16 if flags & 2 else (rds - 72) // 12


# ---------------------------------------------------------------------------------------------------------------------
# generating
# ---------------------------------------------------------------------------------------------------------------------
def _field(fields, name):
    f = next((x for x in fields if x.name == name), None)
    if f is None:
        raise LightmapGenError('the ltmp layout has no field %r' % name)
    return f


def _node(parent, f, data=None):
    size = T.element_size(f)
    return T.Node(parent.group, T.child_path(parent.path, f), f.children, size, None if data is None else bytes(data).ljust(size, b'\0'))


def _mirror_elements(g, f, sources):
    """Clusters / Poop Definitions: the first 0x54 bytes of each BSP element; the geometry bookkeeping that points into the BSP's own
    geometry (Resource Block Offset / Size, Resources, Owner Tag) is cleared, the section / resource data sizes stay the BSP's."""
    blocks = [x for x in f.children if x.type in T.BLOCK_TYPES]
    out = []
    for s in sources:
        n = _node(g, f, bytes(s.data[:BSP_BLOCK_PREFIX]).ljust(T.element_size(f), b'\0'))
        for x in blocks:
            n.data[x.offset:x.offset + x.size] = b'\0' * x.size            # Compression Info / Resources / Cache Data: none
        n.set_u32(0x28, NULL)                                               # Resource Block Offset
        n.set_u32(0x2C, 0)                                                  # Resource Block Size
        n.set_u32(0x40, NULL)                                               # Owner Tag
        n.set_u32(0x44, 0)                                                  # Owner Tag Section Offset + flags
        out.append(n)
    return out


def generate(ltmp, sbsp, style, add_raw, owner, bitmap=None, bitmap_pages=None, environment=None):
    """Replace the Lightmap Groups of the decoded ltmp tag `ltmp` (Vista layout; its root settings stay) with the generated group for the
    decoded BSP `sbsp`.
      style        STYLE_NATIVE | STYLE_NEUTRAL_VALID
      add_raw      add_raw(node, offset, block bytes): attach a bucket's geometry block to node.patches[offset] (the tag writer's raw patch)
      owner        the ltmp tag's own datum (the Owner Tag of every bucket)
      bitmap       neutral-valid: the datum of the grey bitmap (rasterizer\\default_2d); bitmap_pages: how many pages it has (optional check)
      environment  neutral-valid: the 220-byte Lighting Environment (default: no_lightmap_environment())
    -> a dict: counts, the replaced group nodes ('old_groups') and the bucket list."""
    if style not in STYLES:
        raise LightmapGenError('unknown lightmap style %r (use one of %s)' % (style, ', '.join(STYLES)))
    neutral = style == STYLE_NEUTRAL_VALID
    if neutral:
        if bitmap in (None, 0, NULL):
            raise LightmapGenError('neutral-valid needs the datum of the grey bitmap')
        if bitmap_pages is not None and bitmap_pages <= NEUTRAL_PAGE:
            raise LightmapGenError('the grey bitmap has %d page(s), page %d is needed' % (bitmap_pages, NEUTRAL_PAGE))
    bi = BspInfo(sbsp)
    fg = _field(ltmp.fields, 'Lightmap Groups')
    g = _node(ltmp, fg)
    old = ltmp.blocks.get(fg.offset, [])
    gf = {x.name: x for x in fg.children}

    g.set_u16(0, 0)                                                         # Type
    g.set_u16(2, GROUP_FLAGS)
    g.set_u32(4, bi.checksum)                                               # Structure Checksum = the BSP's
    if neutral:
        g.set_u32(0x18, BITMAP_GROUP)
        g.set_u32(0x1C, bitmap)
    else:
        g.set_u32(0x18, NULL)                                               # Bitmap Group NONE: the lightmap is not valid
        g.set_u32(0x1C, NULL)
    g.blocks[gf['Clusters'].offset] = _mirror_elements(g, gf['Clusters'], bi.clusters)
    g.blocks[gf['Poop Definitions'].offset] = _mirror_elements(g, gf['Poop Definitions'], bi.definitions)

    page = struct.pack('<hbb', NEUTRAL_PAGE, 0, 0) if neutral else struct.pack('<hbb', -1, -1, 0)
    none = struct.pack('<hbb', -1, -1, 0)
    f = gf['Cluster Render Info']
    g.blocks[f.offset] = [_node(g, f, page) for _ in bi.clusters]
    f = gf['Instance Render Info']
    g.blocks[f.offset] = [_node(g, f, page if (neutral and pol == 0 and v > 0) else none) for _, pol, v in bi.instances]

    buckets, refs = plan_buckets([(v, pol) for _, pol, v in bi.instances])
    fb = gf['Geometry Buckets']
    fr = _field(fb.children, 'Resources')
    bnodes = []
    for flags, n in buckets:
        blk, sds, rds, entries = bucket_block(flags, n)
        b = _node(g, fb)
        b.set_u16(0, flags)
        b.set_u32(0x10, len(blk))
        b.set_u32(0x14, sds)
        b.set_u32(0x18, rds)
        b.set_u32(0x24, owner)                                              # Owner Tag: this ltmp
        struct.pack_into('<h', b.data, 0x28, 0x24)                          # Owner Tag Section Offset
        b.set_u16(0x2A, 0x0100)                                             # geometry flags (stock buckets)
        b.set_i32(0x2C, -1)
        b.blocks[fr.offset] = [_node(b, fr, _entry_bytes(e)) for e in entries]
        add_raw(b, 0x0C, blk)
        bnodes.append(b)
    g.blocks[fb.offset] = bnodes

    f = gf['Instance Bucket Refs']
    fo = _field(f.children, 'Section Offsets')
    ibr = []
    for bucket, off in refs:
        r = _node(g, f, struct.pack('<hh', 0, bucket))
        so = _node(r, fo, struct.pack('<h', off))
        r.blocks[fo.offset] = [so]
        ibr.append(r)
    g.blocks[f.offset] = ibr

    f = gf['Lighting Environments']
    if neutral:
        env = _node(g, f)
        e = environment if environment is not None else no_lightmap_environment()
        env.data[:len(e)] = e
        g.blocks[f.offset] = [env]
    else:
        g.blocks[f.offset] = []
    for name in ('Section Palette', 'Writable Palettes', 'Scenery Object Info', 'Scenery Object Bucket Refs'):
        g.blocks[gf[name].offset] = []
    ltmp.blocks[fg.offset] = [g]
    ltmp.set_f32(0x34, 0.0)                                                 # Lightprobe Interpolation Override
    return {'style': style, 'clusters': bi.n_clusters, 'definitions': len(bi.definitions), 'instances': bi.n_instances,
            'buckets': [tuple(b) for b in buckets], 'bucket_vertices': sum(n for _, n in buckets), 'old_groups': list(old),
            'checksum': bi.checksum, 'group': g}


def _entry_bytes(e):
    from .handlers import geometry as GEO
    return GEO.encode_entry(e)


# ---------------------------------------------------------------------------------------------------------------------
# checking (the engine's reads, and fidelity to the stock tool's output)
# ---------------------------------------------------------------------------------------------------------------------
Problem = collections.namedtuple('Problem', 'code engine message')      # engine True: the game would read outside a table / a block


def _group(ltmp):
    gs = ltmp.block('Lightmap Groups')
    return gs[0] if gs else None


def check(ltmp, sbsp, style=None):
    """Problems of the lightmap `ltmp` against its BSP `sbsp` (both decoded, Vista layout) -> [Problem].
    Problems with engine=True are reads the game makes without any bound check (a count too small, a bucket index or slice outside its
    table / vertex stream, an empty Section Offsets block, no group at all); the others are fidelity to what the stock tool writes.
    style: STYLE_NATIVE / STYLE_NEUTRAL_VALID adds the checks of that style (None: the lightmap is judged as it is)."""
    P = []

    def add(code, engine, msg):
        P.append(Problem(code, engine, msg))

    g = _group(ltmp)
    if g is None:
        add('no_group', True, 'the lightmap has no Lightmap Groups element (the game reads the first one at map load)')
        return P
    bi = BspInfo(sbsp)
    if len(ltmp.block('Lightmap Groups')) != 1:
        add('group_count', False, '%d Lightmap Groups (stock: exactly one)' % len(ltmp.block('Lightmap Groups')))
    if g.i32(4) != _i32(bi.checksum):
        add('checksum', False, 'Structure Checksum %08X is not the BSP Checksum %08X (the game then treats the lightmap as not valid)'
            % (g.u32(4), bi.checksum))
    cri, iri, ibr = g.block('Cluster Render Info'), g.block('Instance Render Info'), g.block('Instance Bucket Refs')
    bks = g.block('Geometry Buckets')
    if len(cri) < bi.n_clusters:
        add('cri_short', True, 'Cluster Render Info has %d elements, the BSP %d clusters' % (len(cri), bi.n_clusters))
    elif len(cri) != bi.n_clusters:
        add('cri_count', False, 'Cluster Render Info has %d elements, the BSP %d clusters' % (len(cri), bi.n_clusters))
    for nm, got in (('Instance Render Info', iri), ('Instance Bucket Refs', ibr)):
        if len(got) < bi.n_instances:
            add('instance_table_short', True, '%s has %d elements, the BSP %d instances' % (nm, len(got), bi.n_instances))
        elif len(got) != bi.n_instances:
            add('instance_table_count', False, '%s has %d elements, the BSP %d instances' % (nm, len(got), bi.n_instances))
    if len(g.block('Clusters')) != bi.n_clusters:
        add('clusters_count', False, 'Clusters has %d elements, the BSP %d' % (len(g.block('Clusters')), bi.n_clusters))
    if len(g.block('Poop Definitions')) != len(bi.definitions):
        add('poop_count', False, 'Poop Definitions has %d elements, the BSP %d definitions' % (len(g.block('Poop Definitions')), len(bi.definitions)))
    for nm, ours in (('Clusters', bi.clusters), ('Poop Definitions', bi.definitions)):
        for k, (a, b) in enumerate(zip(g.block(nm), ours)):
            if a.data[0x30:0x38] != b.data[0x30:0x38]:
                add('section_sizes', False, '%s[%d]: Section / Resource Data Size differ from the BSP\'s' % (nm, k))
                break
    nb = []                                                               # vertex count per bucket
    for k, b in enumerate(bks):
        flags = b.u16(0)
        n = bucket_vertices(flags, b.u32(0x18))
        nb.append(n)
        res = b.block('Resources')
        want = 3 if flags & 2 else 2
        if b.u32(0x14) != 8 or b.u32(0x10) != b.u32(0x18) + 20 or len(res) != want or n < 0:
            add('bucket_numbers', False, 'bucket %d: section data %d, size %d, resource data %d, %d resources (stock: 8, rds + 20, rds, %d)'
                % (k, b.u32(0x14), b.u32(0x10), b.u32(0x18), len(res), want))
        if flags not in (1, 3):
            add('bucket_flags', False, 'bucket %d: flags %d (stock: 1 per-pixel, 3 per-vertex)' % (k, flags))
    scenery_buckets = {r.u16(2) for r in g.block('Scenery Object Bucket Refs')}      # per-vertex slices of placed objects: sizes unknown here
    slices = collections.defaultdict(list)
    for k, r in enumerate(ibr[:bi.n_instances]):
        bk = r.u16(2)
        so = r.block('Section Offsets')
        if bk >= len(bks):
            add('bucket_index', True, 'Instance Bucket Refs[%d]: bucket %d of %d' % (k, bk, len(bks)))
            continue
        if len(so) != 1:
            add('section_offsets', True, 'Instance Bucket Refs[%d]: %d Section Offsets (the game reads the first one)' % (k, len(so)))
            continue
        off, v = so[0].i16(0), bi.instances[k][2]
        if off == -1 or v <= 0:
            continue
        if off < 0 or off + v > nb[bk]:
            add('slice_outside', True, 'Instance Bucket Refs[%d]: slice %d + %d vertices outside bucket %d (%d vertices)' % (k, off, v, bk, nb[bk]))
        slices[bk].append((off, v))
    for bk, sl in slices.items():
        if bk in scenery_buckets:
            continue
        pos = 0
        for off, v in sorted(sl):
            if off != pos:
                add('tiling', False, 'bucket %d: slices do not tile the stream (gap or overlap at vertex %d)' % (bk, pos))
                break
            pos += v
        else:
            if pos != nb[bk]:
                add('tiling', False, 'bucket %d: slices cover %d of %d vertices' % (bk, pos, nb[bk]))
    if style is None and g.u32(0x1C) == NULL:
        add('bitmap_none', False, 'Bitmap Group is NONE (the game treats the lightmap as not valid and draws the BSP without lightmap)')
    if style == STYLE_NATIVE and g.u32(0x1C) != NULL:
        add('native_bitmap', False, 'native style: Bitmap Group must be NONE')
    if style == STYLE_NEUTRAL_VALID:
        if g.u32(0x1C) in (0, NULL):
            add('neutral_bitmap', False, 'neutral-valid style: Bitmap Group must be the grey bitmap')
        if len(g.block('Lighting Environments')) != 1:
            add('neutral_env', False, 'neutral-valid style: exactly one Lighting Environment')
    return P


def engine_problems(problems):
    return [p for p in problems if p.engine]


def _i32(v):
    return struct.unpack('<i', struct.pack('<I', v & 0xFFFFFFFF))[0]


def structure_signature(ltmp, sbsp):
    """The values a generated lightmap must share with the stock tool's (the oracle): a dict of tuples / numbers.
    Values that depend on the bake (pages, palettes, bucket partition, stream contents, Bitmap Group) are not in it."""
    g = _group(ltmp)
    bi = BspInfo(sbsp)
    sig = {'groups': len(ltmp.block('Lightmap Groups')), 'type': g.u16(0), 'flags': g.u16(2), 'checksum': g.u32(4)}

    def masked(els):
        # every dword that is a block count / pointer or one of the six cleared geometry references
        out = []
        for e in els:
            d = bytearray(e.data[:BSP_BLOCK_PREFIX])
            for off in (0x18, 0x1C, 0x28, 0x2C, 0x38, 0x3C, 0x40, 0x44, 0x4C, 0x50):
                d[off:off + 4] = b'\0\0\0\0'
            out.append(bytes(d))
        return out

    sig['clusters'] = masked(g.block('Clusters'))
    sig['cri_count'] = len(g.block('Cluster Render Info'))
    sig['poop'] = masked(g.block('Poop Definitions'))
    sig['iri_count'] = len(g.block('Instance Render Info'))
    ibr = g.block('Instance Bucket Refs')
    sig['ibr_count'] = len(ibr)
    sig['ibr_flags_zero'] = all(r.i16(0) == 0 for r in ibr)
    sig['ibr_one_offset'] = all(len(r.block('Section Offsets')) == 1 for r in ibr)
    bnum = []
    bflags = []
    for b in g.block('Geometry Buckets'):
        fl = b.u16(0)
        bflags.append(fl & 3)
        bnum.append((fl, b.u32(0x14), b.u32(0x18), b.u32(0x10), len(b.block('Resources'))))
    # vertices of the instances' own slices per bucket class (the stock buckets also hold placed objects' per-vertex sections, which
    # belong to the scenario, so the bucket sizes themselves are not comparable)
    tot = {1: 0, 3: 0}
    for k, r in enumerate(ibr[:bi.n_instances]):
        so = r.block('Section Offsets')
        if r.u16(2) < len(bflags) and so and so[0].i16(0) >= 0:
            tot[bflags[r.u16(2)]] = tot.get(bflags[r.u16(2)], 0) + bi.instances[k][2]
    sig['class_totals'] = (tot[1], tot[3])
    sig['bucket_vertices'] = (sum(bucket_vertices(f, rds) for f, _, rds, _, _ in bnum if f & 3 == 1),
                              sum(bucket_vertices(f, rds) for f, _, rds, _, _ in bnum if f & 3 == 3))
    sig['bucket_numbers_ok'] = all(f in (1, 3) and s == 8 and size == rds + 20 and nres == (3 if f & 2 else 2) and
                                   rds == (76 + 16 * bucket_vertices(f, rds) if f & 2 else 72 + 12 * bucket_vertices(f, rds))
                                   for f, s, rds, size, nres in bnum)
    sig['expected'] = {'clusters': bi.n_clusters, 'definitions': len(bi.definitions), 'instances': bi.n_instances}
    return sig
