# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""The MCC H2EK tool's shader compile recipe, reproduced EXACTLY: HLSL source -> the DXBC blobs stored in MCC caches.

What the H2EK ``tool.exe`` does (decompiled with idalib from tool.exe.i64; addresses are tool.exe VAs)
---------------------------------------------------------------------------------------------------
Pixel shaders (``spas`` Postprocess Definition > Implementations > Pixel Shader, three DXBC blobs per element):

* The source is ``tags\\rasterizer\\pixel_shaders_dx9\\<pass tag path with \\ -> _>_<implementation index>.psh`` (sub_8C5A20 0x8C5A20).
* Leading ';' comment lines are skipped, then ``#const Cn.<mask> = $name`` directives are consumed (sub_8C90F0), then the text is either ONE
  shader or a sequence of ``#case $var=N [$var2=M ...]`` sections (sub_8C8D30), one pixel shader element per section.
* Every shader is compiled THREE times (sub_8C23B0 0x8C23B0) with D3DCompile of ``D3DCompiler_47.dll`` (LoadLibraryA, sub_8C69C0 0x8C69C0):
      pSrcData   = DX11 PRELUDE + text            (PRELUDE_DX11: the DX11_sampler2D / DX11_tex2Dproj ... macros)
      pSourceName= "<psh file name>"              (only used in error messages)
      pDefines   = DX11=1 [, ZPRIME=1 | MRT=1] [, CENTROID<stage>=1] [, ALPHA_TEST=<ref/255 %f>, ALPHA_TEST_{GREATER,NOT_EQUAL,LESS}=1]
      pInclude   = NULL (no #include), entry "main", target "ps_5_0", Flags1 = 0x1000 (D3DCOMPILE_ENABLE_BACKWARDS_COMPATIBILITY), Flags2 = 0
  variant 0 = plain, variant 1 = ZPRIME (z-prime / depth stripe), variant 2 = MRT (colour + depth render targets).
  The tool keeps a compile cache ``shader_cache\\%x.%d.%d.%d.%x.bin`` = hash . 0 . variant . 0 . flags; every entry stores (defines string, source,
  DXBC), which makes it an independent oracle: all 2847 entries of the H2EK tool's compile cache recompile byte for byte.
* The alpha test defines come from the pass tag's Alpha-Test State (flags bit 0 = enabled; function 1 less / 4 greater / 5 not-equal; reference
  clamped to 255, printed as ``%f`` of float(ref/255.0)); the cache keeps only the compiled render states, which carry the same triple
  (state 15 = ALPHATESTENABLE, 24 = ALPHAREF - omitted when 0, 25 = ALPHAFUNC with D3DCMP = tool function + 1): ``alpha_test_from_render_states``.

Vertex shaders (``vrtx``): there is NO vertex-shader compile in tool.exe (no "vs_" profile string); the MCC tool only COPIES the DX11 DXBC block
of the ``rasterizer\\vertex_shaders_dx9\\*.vertex_shader`` tag into the vrtx cache tag.  That DXBC is reproducible from the tag's own DX11 ``Code``
(HLSL text, CRLF): D3DCompile(Code, entry "main", target "vs_5_0", Flags1 = 0x2000 = D3DCOMPILE_IEEE_STRICTNESS, no defines).

The compiler must be a D3DCompiler_47.dll whose RDEF creator string is "Microsoft (R) HLSL Shader Compiler 10.1" (the Windows 10/11 builds,
10.0.26100.x, System32 and SysWOW64 give identical output); the 10.0.10074.0 copy shipped in the MCC folder writes another creator string and
therefore other bytes (``compiler_info()`` / ``check_compiler()``).

Public API
----------
compile_pixel_shader(hlsl, alpha_test=None, centroid_stages=(), source_name=None) -> [dxbc_plain, dxbc_zprime, dxbc_mrt]
variant_defines(variant, alpha_test=None, centroid_stages=())
AlphaTest(function, reference);  alpha_test_from_render_states(states);  alpha_test_from_tag_impl(impl)
parse_psh(data) -> PshFile;  compile_psh(data, ...) -> CompiledPsh        (the whole Pixel Shader block of one pass implementation)
psh_file_name(pass_tag_name, impl_index)
compile_vertex_shader(code) -> dxbc;  read_vertex_shader_tag(path) -> dict
read_tag_file(path, group, game='mcc')            generic editing-kit tag file reader driven by the Assembly plugin layouts
check_compiler() -> (ok, text);  compiler_info()
"""
import ctypes
try:                                                                  # BELOW_NORMAL priority
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:                                                     # noqa: BLE001  pragma: no cover
    pass

import collections
import os
import struct

from .. import paths as _paths

# =====================================================================================================================
# 1. constants taken from tool.exe
# =====================================================================================================================
TOOL_EXE_MD5 = 'c2011bb9b07a7325492d7a804bd939eb'         # the H2EK's tool.exe the recipe was read from
PS_TARGET = b'ps_5_0'                                      # tool.exe 0xE60204, passed by sub_8C69C0
PS_ENTRY = b'main'
FLAGS_RELEASE = 0x1000                                     # D3DCOMPILE_ENABLE_BACKWARDS_COMPATIBILITY (sub_8C69C0: 4096)
FLAGS_DEBUG = 0x1005                                       # + DEBUG | SKIP_OPTIMIZATION when the tool shell flag 28 is set (never in a built map)
VS_TARGET = b'vs_5_0'
VS_ENTRY = b'main'
VS_FLAGS = 0x2000                                          # D3DCOMPILE_IEEE_STRICTNESS: the only flag set that reproduces the tag DXBC (probe: 2^13 combos)
VARIANTS = ('plain', 'zprime', 'mrt')                      # blob 0 / 1 / 2 of every Pixel Shader element
VARIANT_PROFILE_LABELS = ('ps_2_0', 'ps_2_0', 'ps_3_0')    # the D3D9 profile each variant stands for (labels in the tool's define tables, tool.exe 0x102E3A8)

# The DX11 prelude (tool.exe 0xE5F8B0 / file offset 0xA5EAB0, 411 bytes) is prepended to every pixel shader source; the non-DX11 prelude
# (0xE5F788, 292 bytes) is only used when the tool runs in the legacy compile mode (dword_14849DC == 1), never for MCC caches.
PRELUDE_DX11 = (
    b'#define DX11_sampler2D(i)          SamplerState s##i : register(S##i); Texture2D s##i##tex : register(t##i)\n'
    b'#define DX11_samplerCUBE(i)        SamplerState s##i : register(S##i); TextureCube s##i##tex : register(t##i)\n'
    b'#define DX11_tex2D(si, tci)        si##tex.Sample(si, tci.xy)\n'
    b'#define DX11_tex2Dproj(si, tci)    si##tex.Sample(si, tci.xy/tci.w)\n'
    b'#define DX11_texCUBE(si, tci)      si##tex.Sample(si, tci.xyz)\n')
PRELUDE_DX9 = (
    b'#define DX11_sampler2D(i)          sampler2D s##i : register(S##i)\n'
    b'#define DX11_samplerCUBE(i)        samplerCUBE s##i : register(S##i)\n'
    b'#define DX11_tex2D(si, tci)        tex2D(si, tci)\n'
    b'#define DX11_tex2Dproj(si, tci)    tex2Dproj(si, tci)\n'
    b'#define DX11_texCUBE(si, tci)      texCUBE(si, tci)\n')

COMPILER_DEFAULT = os.environ.get('H2CONV_D3DCOMPILER', os.path.join(os.environ.get('SystemRoot') or r'C:\Windows', 'System32', 'D3DCompiler_47.dll'))
EXPECTED_CREATOR = b'Microsoft (R) HLSL Shader Compiler 10.1'

# tool alpha compare function numbers (spas Alpha-Test State, Assembly plugin) and the D3DCMP value of the compiled render state
ALPHA_LESS, ALPHA_GREATER, ALPHA_NOT_EQUAL = 1, 4, 5
ALPHA_DEFINE_NAME = {ALPHA_GREATER: 'ALPHA_TEST_GREATER', ALPHA_NOT_EQUAL: 'ALPHA_TEST_NOT_EQUAL', ALPHA_LESS: 'ALPHA_TEST_LESS'}
D3DRS_ALPHATESTENABLE, D3DRS_ALPHAREF, D3DRS_ALPHAFUNC = 15, 24, 25
SOURCE_EXTERN_LIGHTMAP = 11                                # Textures[i].Source Extern == Lightmap -> define CENTROID<i> (variant 0 only)

# const-directive component masks (sub_8C90F0) -> (mask index stored in code bits 5..7)
CONST_MASKS = {'r': 0, 'g': 1, 'b': 2, 'a': 3, 'rgb': 4}
CONST_FUNCTIONS = ('IDENTITY', 'INVERT', 'COS', 'COSINV', 'SIN', 'SININV')     # tool.exe 0xE5F1EC table, code = index (stored as code byte1 = 2 * f)


class ShaderCompileError(ValueError):
    """The tool would refuse / mis-compile this input (the message names the reason); callers report, never approximate."""


# =====================================================================================================================
# 2. D3DCompiler_47 (ctypes, in-process)
# =====================================================================================================================
class _Macro(ctypes.Structure):
    _fields_ = [('Name', ctypes.c_char_p), ('Definition', ctypes.c_char_p)]


_dll_cache = {}


def _dll(path=None):
    path = path or COMPILER_DEFAULT
    d = _dll_cache.get(path)
    if d is None:
        d = ctypes.WinDLL(path)
        d.D3DCompile.restype = ctypes.c_long
        d.D3DCompile.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p,
                                 ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
        d.D3DDisassemble.restype = ctypes.c_long
        d.D3DDisassemble.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        _dll_cache[path] = d
    return d


def _take_blob(p):
    """ID3DBlob* (vtable: QueryInterface, AddRef, Release, GetBufferPointer, GetBufferSize) -> bytes, released"""
    if not p:
        return b''
    vt = ctypes.cast(p, ctypes.POINTER(ctypes.c_void_p))[0]
    tab = ctypes.cast(vt, ctypes.POINTER(ctypes.c_void_p))
    get_ptr = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(tab[3])
    get_size = ctypes.WINFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(tab[4])
    release = ctypes.WINFUNCTYPE(ctypes.c_uint, ctypes.c_void_p)(tab[2])
    data = ctypes.string_at(get_ptr(p), get_size(p))
    release(p)
    return data


def d3d_compile(src, defines=(), target=PS_TARGET, entry=PS_ENTRY, flags1=FLAGS_RELEASE, flags2=0, source_name=None, compiler=None):
    """Raw D3DCompile.  defines = [(name, value)] in macro-table order.  Returns (dxbc | None, compiler log text)."""
    if isinstance(src, str):
        src = src.encode('latin-1')
    arr = (_Macro * (len(defines) + 1))()
    keep = []
    for i, (n, v) in enumerate(defines):
        nb = n.encode() if isinstance(n, str) else n
        vb = v.encode() if isinstance(v, str) else v
        keep.append((nb, vb))
        arr[i].Name = nb
        arr[i].Definition = vb
    sn = source_name.encode() if isinstance(source_name, str) else source_name
    code, err = ctypes.c_void_p(0), ctypes.c_void_p(0)
    hr = _dll(compiler).D3DCompile(src, len(src), sn, arr if defines else None, None, entry, target, flags1, flags2,
                                   ctypes.byref(code), ctypes.byref(err))
    log = _take_blob(err.value).decode('latin-1', 'replace')
    blob = _take_blob(code.value)
    if hr < 0 or not blob:
        return None, 'HRESULT %08X %s' % (hr & 0xFFFFFFFF, log)
    return blob, log


def d3d_disassemble(dxbc, flags=0, compiler=None):
    out = ctypes.c_void_p(0)
    buf = ctypes.create_string_buffer(bytes(dxbc), len(dxbc))
    hr = _dll(compiler).D3DDisassemble(buf, len(dxbc), flags, None, ctypes.byref(out))
    if hr < 0:
        return None
    return _take_blob(out.value).decode('latin-1', 'replace')


def _file_version(path):
    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    ver.GetFileVersionInfoW(path, 0, size, buf)
    p, n = ctypes.c_void_p(), ctypes.c_uint()
    ver.VerQueryValueW(buf, '\\', ctypes.byref(p), ctypes.byref(n))
    ms, ls = struct.unpack('<II', ctypes.string_at(p.value + 8, 8))
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def compiler_info(compiler=None):
    path = compiler or COMPILER_DEFAULT
    return {'path': path, 'version': _file_version(path)}


def check_compiler(compiler=None):
    """(ok, text): does this D3DCompiler_47 write the creator string the H2EK tool's compiler writes (so the bytes can be identical)?"""
    try:
        blob, log = d3d_compile(PRELUDE_DX11 + b'float4 main(float4 a : COLOR0) : COLOR { return a; }\n', [('DX11', '1')], compiler=compiler)
    except OSError as e:
        return False, 'cannot load D3DCompiler: %s' % e
    if blob is None:
        return False, log
    if EXPECTED_CREATOR not in blob:
        return False, 'compiler creator string differs (expected %r): the DXBC would not be byte-identical to the tool\'s' % EXPECTED_CREATOR
    return True, 'ok %s' % (compiler_info(compiler),)


# =====================================================================================================================
# 3. defines / variants / alpha test
# =====================================================================================================================
class AlphaTest(collections.namedtuple('AlphaTest', 'function reference')):
    """function: 1 less / 4 greater / 5 not-equal (the spas Alpha-Test State numbering); reference 0..255 (values above 255 are clamped by the tool)."""
    __slots__ = ()

    @classmethod
    def from_d3dcmp(cls, d3dcmp, reference):
        """D3DRS_ALPHAFUNC value (D3DCMP_LESS = 2, GREATER = 5, NOTEQUAL = 6) -> AlphaTest (tool function = D3DCMP - 1)"""
        f = d3dcmp - 1
        if f not in ALPHA_DEFINE_NAME:
            raise ShaderCompileError('alpha-test function D3DCMP %d is not one the tool supports (less / greater / not-equal)' % d3dcmp)
        return cls(f, reference)

    def defines(self):
        if self.function not in ALPHA_DEFINE_NAME:
            raise ShaderCompileError('alpha-test function %d is not one of less(1) / greater(4) / not-equal(5) (the tool asserts)' % self.function)
        ref = min(int(self.reference), 255)
        # the tool prints (float)((float)ref / 255.0) with "%f": the float32 value, 6 decimals
        f32 = struct.unpack('<f', struct.pack('<f', ref / 255.0))[0]
        return [('ALPHA_TEST', '%f' % f32), (ALPHA_DEFINE_NAME[self.function], '1')]


def alpha_test_from_render_states(states):
    """The pass implementation's compiled render states ([(state, value)] of its Render States range, the ones the pass SETS) -> AlphaTest | None.

    Verified against the .shader_pass tag files: the compiled states carry ALPHATESTENABLE (15) / ALPHAREF (24) / ALPHAFUNC (25) exactly when the
    pass has an enabled Alpha-Test State."""
    enable = ref = func = None
    for s, v in states:
        if s == D3DRS_ALPHATESTENABLE and enable is None:
            enable = v
        elif s == D3DRS_ALPHAREF and ref is None:
            ref = v
        elif s == D3DRS_ALPHAFUNC and func is None:
            func = v
    if not enable:
        return None
    if func is None:
        raise ShaderCompileError('alpha test enabled without an ALPHAFUNC render state')
    # the compiled states omit ALPHAREF when it is 0 (the D3D default; the restore list carries (24, 0))
    return AlphaTest.from_d3dcmp(func, 0 if ref is None else ref)


def alpha_test_from_tag_impl(impl):
    """impl = one element of a .shader_pass tag file's Implementations (read_tag_file); -> AlphaTest | None (Alpha-Test State flags bit 0)"""
    blk = impl.get('Alpha-Test State') or []
    if not blk:
        return None
    e = blk[0]
    if not (num(e, 'Flags') & 1):
        return None
    return AlphaTest(num(e, 'Alpha Compare Function'), num(e, 'Alpha-Test Ref'))


def variant_defines(variant, alpha_test=None, centroid_stages=()):
    """The D3D_SHADER_MACRO list (in table order) the tool passes for variant 0/1/2.  centroid_stages = indices of the pass's Textures whose
    Source Extern is Lightmap (CENTROID<i>=1 is defined for variant 0 only; the DX11 branch of the sources never reads it)."""
    out = [('DX11', '1')]
    if variant == 0:
        out += [('CENTROID%d' % i, '1') for i in centroid_stages]
    elif variant == 1:
        out.append(('ZPRIME', '1'))
    elif variant == 2:
        out.append(('MRT', '1'))
    else:
        raise ValueError('variant must be 0, 1 or 2')
    if alpha_test is not None:
        out += AlphaTest(*alpha_test).defines()
    return out


_PS_MEMO = collections.OrderedDict()           # (source, alpha, centroid, flags, compiler) -> (blob0, blob1, blob2); bounded
_PS_MEMO_MAX = 4096


def compile_pixel_shader(hlsl, alpha_test=None, centroid_stages=(), source_name=None, flags=FLAGS_RELEASE, compiler=None):
    """HLSL text (what follows the header comments / #const / #case lines in a .psh) -> [dxbc_plain, dxbc_zprime, dxbc_mrt]: the three blobs of one
    Pixel Shader element, exactly as tool.exe compiles them.  alpha_test = AlphaTest | (function, reference) | None.  The result is memoised (the
    same source compiles to the same bytes; thousands of maps share a few hundred passes).  Raises ShaderCompileError with the compiler log
    when a variant does not compile."""
    if isinstance(hlsl, str):
        hlsl = hlsl.encode('latin-1')
    alpha = None if alpha_test is None else AlphaTest(*alpha_test)
    key = (bytes(hlsl), alpha, tuple(centroid_stages), flags, compiler or COMPILER_DEFAULT)
    hit = _PS_MEMO.get(key)
    if hit is not None:
        _PS_MEMO.move_to_end(key)
        return list(hit)
    src = PRELUDE_DX11 + hlsl
    blobs = []
    for v in range(3):
        code, log = d3d_compile(src, variant_defines(v, alpha, centroid_stages), flags1=flags, source_name=source_name, compiler=compiler)
        if code is None:
            raise ShaderCompileError('variant %d (%s) failed: %s' % (v, VARIANTS[v], log))
        blobs.append(code)
    _PS_MEMO[key] = tuple(blobs)
    if len(_PS_MEMO) > _PS_MEMO_MAX:
        _PS_MEMO.popitem(last=False)
    return blobs


# =====================================================================================================================
# 4. .psh files  (sub_8C5A20 / sub_8C90F0 / sub_8C8D30)
# =====================================================================================================================
PSH_DIR_REL = 'rasterizer\\pixel_shaders_dx9'


def psh_file_name(pass_tag_name, impl_index):
    """shaders\\shader_passes\\bloom\\bloom, 0 -> shaders_shader_passes_bloom_bloom_0.psh  (sub_8C5A20: '\\' and '/' -> '_', then '_%ld.psh')"""
    return '%s_%d.psh' % (pass_tag_name.replace('\\', '_').replace('/', '_'), impl_index)


def _isspace(b):
    """C isspace() of one byte (False for the empty slice past the end of the buffer = the NUL terminator)"""
    return len(b) == 1 and b in b' \t\n\v\f\r'


class ConstDirective(collections.namedtuple('ConstDirective', 'register mask function name code line')):
    """``#const C<register>.<mask> = [FUNCTION:]$name``.  code = the 16-bit word the tool stores in the pass's constant tables:
    low byte = register | mask_index << 5, high byte = 2 * function index (function 0 = none)."""
    __slots__ = ()

    @property
    def mask_index(self):
        return CONST_MASKS[self.mask]


class PshCase(collections.namedtuple('PshCase', 'index variables text')):
    """one pixel shader element's source: variables = ((name, value), ...) of the #case line (empty for a single shader)"""
    __slots__ = ()


class PshFile(collections.namedtuple('PshFile', 'consts cases')):
    __slots__ = ()

    @property
    def switch_variables(self):
        return tuple(n for n, _ in self.cases[0].variables) if self.cases else ()


def _skip_comment_header(data):
    """sub_8C5A20: if the file starts with ';' skip comment lines (and the whitespace after each) until a line that does not start with ';'"""
    n = len(data)
    i = 0
    if data[:1] == b';':
        while True:
            j = data.find(b'\n', i)
            if j < 0:
                raise ShaderCompileError('psh file consists only of comment lines')
            i = j
            while i < n and _isspace(data[i:i + 1]):
                i += 1
            if not (i < n and data[i:i + 1] == b';'):
                break
    return i


def _parse_const_line(line):
    """one '#const ...' line without its newline -> ConstDirective (the checks of sub_8C90F0 raise ShaderCompileError)"""
    s = line.rstrip()
    p = len('#const')
    while p < len(s) and _isspace(s[p:p + 1]):
        p += 1
    if s[p:p + 1] != b'C':
        raise ShaderCompileError('expected const register in %r' % line)
    p += 1
    q = p
    while q < len(s) and s[q:q + 1].isdigit():
        q += 1
    if q == p:
        raise ShaderCompileError('expected const register index in %r' % line)
    reg = int(s[p:q])
    p = q
    if s[p:p + 1] != b'.':
        raise ShaderCompileError("expected '.mask' after C# register in %r" % line)
    p += 1
    ch = s[p:p + 1]
    if ch in (b'a', b'b', b'g'):
        mask = ch.decode()
        p += 1
    elif ch == b'r':
        if s[p:p + 3] == b'rgb':
            mask, p = 'rgb', p + 3
        else:
            mask, p = 'r', p + 1
    else:
        raise ShaderCompileError('invalid component mask on C# register in %r' % line)
    while p < len(s) and _isspace(s[p:p + 1]):
        p += 1
    if s[p:p + 1] != b'=':
        raise ShaderCompileError("expected '=' in %r" % line)
    p += 1
    while p < len(s) and _isspace(s[p:p + 1]):
        p += 1
    if s[p:p + 1] != b'$':
        raise ShaderCompileError('$ expected in %r' % line)
    p += 1
    rest = s[p:]
    func = 0
    colon = rest.find(b':')
    if colon >= 0:
        if mask == 'rgb':
            raise ShaderCompileError('constant functions not supported on vector parms: %r' % line)
        fname = rest[:colon]
        for i, nm in enumerate(CONST_FUNCTIONS):                 # the tool matches by prefix, in table order
            if fname.startswith(nm.encode()):
                func = i
                break
        else:
            raise ShaderCompileError('invalid constant function in %r' % line)
        rest = rest[colon + 1:]
    code = (reg & 0x1F) | (CONST_MASKS[mask] << 5) | ((2 * func) << 8)
    return ConstDirective(reg, mask, func, rest.decode('latin-1'), code, line.decode('latin-1'))


def parse_psh(data):
    """The text of a .psh -> PshFile(consts, cases).  Reproduces the tool's splitting exactly (sub_8C5A20): the HLSL handed to the compiler is
    ``case.text`` (everything after the header comment / #const / #case lines), single shaders have exactly one case with no variables."""
    data = bytes(data)
    i = _skip_comment_header(data)
    consts = []
    if data.startswith(b'#const', i):
        while True:
            j = data.find(b'\n', i)
            line = data[i:j if j >= 0 else len(data)]
            consts.append(_parse_const_line(line))
            if j < 0:
                i = len(data)
                break
            i = j + 1
            if not data.startswith(b'#const', i):
                break
    cases = []
    if not data.startswith(b'#case', i):
        cases.append(PshCase(0, (), data[i:]))
    else:
        while True:
            p = i + len('#case')
            if _isspace(data[p:p + 1]):                          # skip blanks, stop AT a newline
                while True:
                    if data[p:p + 1] == b'\n':
                        break
                    p += 1
                    if not _isspace(data[p:p + 1]):
                        break
            variables = []
            while True:                                          # sub_8C8D30: tokens '$name=N' up to the end of the line
                while _isspace(data[p:p + 1]) and data[p:p + 1] != b'\n':
                    p += 1
                c = data[p:p + 1]
                if c in (b'', b'\n'):
                    break
                if c != b'$':
                    raise ShaderCompileError("invalid #case line - missing '$'")
                p += 1
                e = p
                while e < len(data) and data[e:e + 1] not in (b'=', b'\n'):
                    e += 1
                if data[e:e + 1] != b'=':
                    raise ShaderCompileError("invalid #case line - missing '='")
                name = data[p:e].decode('latin-1')
                p = e + 1
                d = p
                while d < len(data) and data[d:d + 1].isdigit():
                    d += 1
                if d == p or int(data[p:d]) > 31:
                    raise ShaderCompileError('#case line missing or invalid integer case enum index')
                variables.append((name, int(data[p:d])))
                p = d
            q = p
            while q < len(data) and _isspace(data[q:q + 1]):     # the case text starts at the first non-blank after the #case line
                q += 1
            nxt = data.find(b'#case', q)                         # strstr: ANY later '#case' ends the text
            text = data[q:nxt if nxt >= 0 else len(data)]
            cases.append(PshCase(len(cases), tuple(variables), text))
            if nxt < 0:
                break
            i = nxt
    if len(cases) > 1:
        names = [tuple(n for n, _ in c.variables) for c in cases]
        if any(x != names[0] for x in names):
            raise ShaderCompileError('all #case statements do not use the same variables in the same order')
    return PshFile(tuple(consts), tuple(cases))


# Externs.  g_extern_remapping (tool.exe 0xE648E0, 94 x 12 bytes, index = the pass parameter's "Source Extern" enum value) = (global extern index,
# kind, local index u16, flag dword, flag dword).  kind 1 = value extern, 2 = colour / vector extern, 3 = switch extern.  The local index is what the
# cache stores: Pixel Shader Switch Extern Map "Switch Parameter" (kind 3) and the Extern Index of the pass's Externs records (kind 1 / 2).  Embedded here
# (kind, local, w1, w2); tests compare it with tool.exe.  w2 bit 0x100 marks the direction-vector externs (eye forward / right / up, ...): a #const rgb
# on one of them sets bit 8 of the code word.
EXTERN_KIND_VALUE, EXTERN_KIND_COLOR, EXTERN_KIND_SWITCH = 1, 2, 3
EXTERN_VECTOR_FLAG = 0x100
EXTERN_TABLE = (
    (1, 255, 0, 0), (2, 0, 0, 272), (2, 1, 0, 272), (2, 2, 0, 272), (2, 3, 1, 2), (2, 4, 1, 2),
    (1, 0, 1, 2), (2, 5, 0, 4), (2, 6, 0, 4), (2, 7, 0, 260), (2, 8, 0, 260), (2, 9, 0, 260),
    (2, 10, 1, 260), (2, 11, 1, 260), (2, 12, 1, 260), (1, 1, 1, 4), (2, 13, 1, 4), (1, 2, 1, 8),
    (2, 14, 1, 264), (2, 15, 1, 8), (2, 16, 1, 264), (2, 17, 1, 8), (2, 18, 0, 16), (1, 10, 0, 16),
    (2, 19, 0, 16), (1, 3, 0, 16), (1, 4, 0, 16), (1, 5, 1, 16), (1, 6, 1, 16), (2, 20, 1, 16),
    (1, 7, 1, 16), (1, 8, 1, 2), (1, 9, 1, 2), (3, 0, 0, 4), (3, 1, 0, 4), (3, 2, 0, 4),
    (3, 3, 0, 4), (3, 4, 0, 1), (1, 11, 0, 0), (1, 12, 0, 0), (1, 13, 0, 0), (1, 14, 0, 0),
    (1, 15, 0, 0), (1, 16, 1, 6), (1, 17, 0, 4), (1, 18, 0, 16), (1, 19, 0, 16), (1, 20, 0, 16),
    (2, 21, 1, 2), (2, 22, 1, 2), (2, 23, 1, 9), (3, 5, 0, 1), (2, 24, 1, 9), (1, 21, 0, 1),
    (1, 22, 0, 8), (1, 23, 1, 8), (1, 24, 0, 16), (2, 25, 0, 16), (2, 26, 0, 16), (1, 25, 0, 16),
    (1, 26, 0, 16), (1, 27, 0, 16), (1, 28, 0, 16), (2, 27, 0, 16), (2, 28, 0, 16), (1, 29, 0, 16),
    (1, 30, 0, 16), (1, 31, 0, 16), (1, 32, 0, 16), (2, 29, 0, 16), (2, 30, 0, 16), (1, 33, 0, 16),
    (2, 31, 0, 16), (2, 32, 0, 16), (1, 34, 0, 16), (1, 35, 0, 16), (1, 36, 0, 16), (2, 33, 1, 4),
    (2, 34, 1, 4), (2, 35, 1, 9), (3, 6, 0, 4), (3, 7, 0, 4), (1, 37, 1, 2), (1, 38, 1, 2),
    (2, 36, 1, 4), (2, 37, 1, 4), (2, 38, 1, 4), (2, 39, 1, 4), (2, 40, 1, 2), (2, 41, 1, 272),
    (1, 39, 1, 16), (1, 40, 1, 4), (1, 41, 1, 4), (1, 42, 1, 4),
)


def extern_remapping_table(tool_exe=None):
    """-> [(global, kind, local, w1, w2)] x 94.  Without an argument: the embedded copy; with a tool.exe path: read from that file's data section."""
    if tool_exe is None:
        return [(i,) + row for i, row in enumerate(EXTERN_TABLE)]
    with open(tool_exe, 'rb') as fh:
        d = fh.read()
    pe = struct.unpack_from('<I', d, 0x3C)[0]
    nsec = struct.unpack_from('<H', d, pe + 6)[0]
    optsz = struct.unpack_from('<H', d, pe + 20)[0]
    base = struct.unpack_from('<I', d, pe + 24 + 28)[0]
    off = None
    for i in range(nsec):
        so = pe + 24 + optsz + 40 * i
        vsz, va, rsz, rptr = struct.unpack_from('<IIII', d, so + 8)
        rva = 0xE648E0 - base
        if va <= rva < va + max(vsz, rsz):
            off = rptr + rva - va
    if off is None:
        raise ShaderCompileError('g_extern_remapping not found in %s' % tool_exe)
    return [struct.unpack_from('<BBHII', d, off + 12 * i) for i in range(94)]


def pass_externs(consts, source_externs):
    """The pass implementation's Externs records for its #const directives: [(code word, 0, local extern index)] in directive order, one for every
    directive whose pass parameter has a Source Extern.  consts = PshFile.consts; source_externs = {parameter name: Source Extern enum value (0 = none)}.
    Verified against the cache (tests: 'externs').  Directives on plain parameters (Source Extern 0) produce no pass record: their code word is the
    Pixel Constants register code of the stem's Remappings instead."""
    out = []
    for c in consts:
        se = source_externs.get(c.name, 0)
        if not se:
            continue
        kind, local, _w1, w2 = EXTERN_TABLE[se]
        code = c.code
        if c.mask == 'rgb' and kind == EXTERN_KIND_COLOR and (w2 & EXTERN_VECTOR_FLAG):
            code |= 0x100
        out.append((code, 0, local))
    return out


def case_tables(cases, switch_local_indices):
    """The pass implementation's 'Pixel Shader Switch Extern Map' and 'Pixel Shader Index Block' for a #case file (sub_8C5A20 tail).

    cases = PshFile.cases; switch_local_indices = for each switch variable (in #case order) the LOCAL switch extern index of the pass parameter's Source
    Extern (g_extern_remapping kind 3).  Returns (switch_extern_map [(local index, scalar)], index_block [case index | -1])."""
    nvars = len(switch_local_indices)
    if nvars == 0:
        return [], []
    vals = [[v for _, v in c.variables] for c in cases]
    maxes = [max(row[k] for row in vals) for k in range(nvars)]
    scalar = [1]
    for k in range(1, nvars):
        scalar.append((scalar[k - 1] * (maxes[k - 1] + 1)) & 0xFF)       # u8 arithmetic in the tool
    total = scalar[-1] * (maxes[-1] + 1)                                   # int arithmetic in the tool (only the per-variable scalars are bytes)
    block = [-1] * (total or 1)
    for c, row in zip(cases, vals):
        idx = sum(scalar[k] * row[k] for k in range(nvars))
        block[idx] = c.index
    return [(switch_local_indices[k], scalar[k]) for k in range(nvars)], block


class CompiledPsh(collections.namedtuple('CompiledPsh', 'consts cases blobs switch_extern_map index_block alpha_test')):
    """the Pixel Shader block of one pass implementation: blobs[i] = (plain, zprime, mrt) DXBC of cases[i]; the Constant Register Defaults of every
    element are 32 zero dwords (sub_8C23B0)."""
    __slots__ = ()


def compile_psh(data, alpha_test=None, centroid_stages=(), switch_local_indices=None, source_name=None, compiler=None):
    """Whole .psh file bytes -> CompiledPsh (the Pixel Shader block of one pass implementation).

    alpha_test: AlphaTest | None (from the pass tag, or alpha_test_from_render_states of the compiled/Vista implementation).
    switch_local_indices: for #case files, the local switch extern index of each case variable (see case_tables); when it is None the tables are not
    computed (switch_extern_map and index_block are None - never silently empty)."""
    f = parse_psh(data)
    blobs = [tuple(compile_pixel_shader(c.text, alpha_test, centroid_stages, source_name, compiler=compiler)) for c in f.cases]
    if not (len(f.cases) > 1 or f.cases[0].variables):
        smap, iblock = [], []
    elif switch_local_indices is None:
        smap = iblock = None
    else:
        smap, iblock = case_tables(f.cases, switch_local_indices)
    return CompiledPsh(f.consts, f.cases, blobs, smap, iblock, alpha_test)


def compile_psh_file(path, alpha_test=None, centroid_stages=(), switch_local_indices=None, compiler=None):
    """compile_psh of a file on disk (the file name is passed to the compiler as the source name, like the tool does)"""
    with open(path, 'rb') as fh:
        return compile_psh(fh.read(), alpha_test, centroid_stages, switch_local_indices, os.path.basename(path), compiler)

# =====================================================================================================================
# 5. editing-kit tag files (shader_pass .shader_pass, vertex_shader .vertex_shader), driven by the Assembly plugin layouts
# =====================================================================================================================
class TagFileError(ValueError):
    pass


def _file_size(f):
    t = f.type
    return 12 if t == 'tagblock' else 16 if t == 'tagref' else 20 if t == 'dataref' else 4 if t == 'uint64' else f.size


def _struct_file_size(fields):
    return sum(_file_size(f) for f in fields)


class _TagReader(object):
    """File element size = tagblock 12 (count, ptr, definition), tagRef 16 (group, ptr, length, datum), dataref 20, uint64 runtime pointer 4,
    anything else its plugin size.  Variable data follows the element array: per element, per field in offset order: tagRef -> name + NUL,
    stringid -> text, tagblock -> 'dfbt' header + elements (recursive), dataref -> bytes."""

    def __init__(self, data, path=''):
        self.d, self.p, self.path = data, 0x40, path

    def err(self, msg):
        raise TagFileError('%s @0x%X: %s' % (self.path, self.p, msg))

    def take(self, n):
        if n < 0 or self.p + n > len(self.d):
            self.err('read of %d bytes past the end' % n)
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def block(self, fields, count, what):
        esz = _struct_file_size(fields)
        if self.d[self.p:self.p + 4] != b'dfbt':
            self.err('expected dfbt (%s)' % what)
        self.p += 4
        _ver, n, size = struct.unpack('<III', self.take(12))
        if n != count or size != esz:
            self.err('%s: count %d/%d or element size 0x%X/0x%X mismatch' % (what, n, count, size, esz))
        raw = [bytes(self.take(esz)) for _ in range(n)]
        return [self.element(fields, r, what) for r in raw]

    def element(self, fields, raw, what):
        rec, pend, off = {}, [], 0
        for f in sorted(fields, key=lambda x: x.offset):
            sz = _file_size(f)
            b = raw[off:off + sz]
            off += sz
            nm = f.name or ('f%X' % f.offset)
            key = nm if nm not in rec else '%s#%X' % (nm, f.offset)
            t = f.type
            if t == 'tagblock':
                cnt = struct.unpack_from('<i', b, 0)[0]
                rec[key] = []
                if cnt > 0:
                    pend.append((key, 'block', (f, cnt)))
            elif t == 'dataref':
                size = struct.unpack_from('<i', b, 0)[0]
                rec[key] = b''
                if size > 0:
                    pend.append((key, 'data', size))
            elif t == 'tagref':
                grp, _p, ln, _dat = struct.unpack('<4sIiI', b)
                rec[key] = ''
                rec[key + '#group'] = grp[::-1].decode('latin-1')
                if ln > 0:
                    pend.append((key, 'tref', ln))
            elif t == 'stringid':
                v = struct.unpack('<I', b)[0]
                rec[key] = ''
                rec[key + '#id'] = v
                if v >> 24:
                    pend.append((key, 'sid', v >> 24))
            else:
                rec[key] = bytes(b)
                rec[key + '#t'] = t
        for key, kind, arg in pend:
            if kind == 'sid':
                rec[key] = self.take(arg).decode('latin-1')
            elif kind == 'tref':
                rec[key] = self.take(arg).decode('latin-1')
                if self.take(1) != b'\0':
                    self.err('%s.%s: tag path not NUL terminated' % (what, key))
            elif kind == 'block':
                rec[key] = self.block(arg[0].children, arg[1], what + '/' + (arg[0].name or ''))
            else:
                rec[key] = bytes(self.take(arg))
        return rec


def num(rec, key):
    """integer / float value of a scalar field of a read_tag_file record (signed for intN)"""
    b, t = rec[key], rec[key + '#t']
    if t in ('float32', 'degree'):
        return struct.unpack('<f', b)[0]
    return int.from_bytes(b, 'little', signed=t.startswith('int'))


_layouts = {}


def read_tag_file(path, group, game='mcc'):
    """Editing-kit tag file -> dict tree (field name -> value; blocks are lists of dicts, datarefs bytes, scalars raw bytes read with num()).
    The parse must consume the file exactly (verified on all 166 .shader_pass and 112 .vertex_shader files of the MCC kit)."""
    from .. import plugins as P
    if game not in _layouts:
        _layouts[game] = P.load_layouts(game, corrected=True)
    lay = _layouts[game][group]
    with open(path, 'rb') as fh:
        d = fh.read()
    if len(d) < 0x40 or d[0x24:0x28][::-1].decode('latin-1') != group or d[0x3C:0x40] != b'!MLB':
        raise TagFileError('%s: not a %s tag file' % (path, group))
    r = _TagReader(d, path)
    root = r.block(lay.fields, 1, group)
    if r.p != len(d):
        raise TagFileError('%s: %d trailing bytes (parsed to 0x%X of 0x%X)' % (path, len(d) - r.p, r.p, len(d)))
    return root[0]


# =====================================================================================================================
# 6. vertex shaders
# =====================================================================================================================
def compile_vertex_shader(code, compiler=None):
    """The DX11 ``Code`` HLSL of a .vertex_shader tag -> its DX11 DXBC (vs_5_0, entry main, Flags1 = D3DCOMPILE_IEEE_STRICTNESS, no defines)."""
    blob, log = d3d_compile(code, [], target=VS_TARGET, entry=VS_ENTRY, flags1=VS_FLAGS, compiler=compiler)
    if blob is None:
        raise ShaderCompileError('vertex shader does not compile: %s' % log)
    return blob


def read_vertex_shader_tag(path, game='mcc'):
    """MCC .vertex_shader tag file -> {'dx11': [(dxbc, code)], 'dx9': [(d3d9 blob, code)], 'xbox1': [...], 'swizzles': (dx11, dx9, xbox1)}"""
    r = read_tag_file(path, 'vrtx', game)
    out = {'swizzles': tuple(num(r, k) for k in ('Output Swizzles DX11', 'Output Swizzles DX9', 'Output Swizzles Xbox1'))}
    for key, name in (('dx11', 'DX11'), ('dx9', 'DX9'), ('xbox1', 'Xbox1')):
        out[key] = [(e['Compiled Shader'], e['Code']) for e in r['Geometry Classifications ' + name]]
    return out


# =====================================================================================================================
# 7. DXBC inspection (conventions of the MCC blobs; used by the tests and by porters to check their own output)
# =====================================================================================================================
def dxbc_chunks(blob):
    """DXBC container -> {fourcc: chunk bytes}"""
    if blob[:4] != b'DXBC':
        raise ValueError('not a DXBC container')
    n = struct.unpack_from('<I', blob, 28)[0]
    offs = struct.unpack_from('<%dI' % n, blob, 32)
    return {blob[o:o + 4].decode('latin-1'): blob[o + 8:o + 8 + struct.unpack_from('<I', blob, o + 4)[0]] for o in offs}


def dxbc_signature(blob, which='ISGN'):
    """-> [(semantic, index, system value, register, mask)] of the input ('ISGN') or output ('OSGN') signature"""
    ch = dxbc_chunks(blob)[which]
    n = struct.unpack_from('<I', ch, 0)[0]
    out = []
    for i in range(n):
        name_off, idx, sv, _ctype, reg, mask, _rw, _pad = struct.unpack_from('<IIIIIBBH', ch, 8 + 24 * i)
        out.append((ch[name_off:ch.index(b'\0', name_off)].decode('latin-1'), idx, sv, reg, mask))
    return out


def dxbc_resources(blob):
    """-> ([(cbuffer name, variables, size)], [(resource name, type, bind point)]) from the RDEF chunk (type 0 cbuffer, 2 texture, 3 sampler)"""
    r = dxbc_chunks(blob)['RDEF']
    ncb, cboff, nres, resoff = struct.unpack_from('<IIII', r, 0)
    cbs = []
    for i in range(ncb):
        nm, nvar, _voff, size, _fl, _typ = struct.unpack_from('<IIIIII', r, cboff + 24 * i)
        cbs.append((r[nm:r.index(b'\0', nm)].decode('latin-1'), nvar, size))
    res = []
    for i in range(nres):
        nm, typ, _rf, _dim, _ns, bp, _bc, _fl = struct.unpack_from('<IIIIIIII', r, resoff + 32 * i)
        res.append((r[nm:r.index(b'\0', nm)].decode('latin-1'), typ, bp))
    return cbs, res
