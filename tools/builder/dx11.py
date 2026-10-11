"""Compile one shader program for Direct3D 11 the way Unity 2018.2's shader compiler does, and derive
the subprogram record and the parsed-form lists Unity stores for it.

Unity compiles with its own Tools/D3Dcompiler_47.dll (vs_4_0/ps_4_0 up to `#pragma target 4.0`,
vs_5_0/ps_5_0 above), reads the bindings from the reflection data and then strips it: the stored
DXBC keeps only ISGN, OSGN, SHDR/SHEX and SFI0. The program code is a 6-byte header (1, then
the shader resource, constant buffer and sampler slot counts, then two zero bytes) and the
stripped DXBC. After the code the record carries the bindings Unity's D3D11 player reads:

    source map, [(source channel, target component)]
    groups: 1 + constant buffers; the first is the unnamed group of loose uniforms (always empty
      on D3D11), then per cbuffer: name, size, [(name, type, rows, columns, is matrix, array
      size, offset)] of the variables the program uses, 0 struct params
    bindings: textures (0, slot, sampler slot or -1, dimension * 2 + multisampled), constant
      buffers (1, slot, 0), buffers (2, slot, 0), UAVs (3, slot, 0), inline samplers (4, slot,
      sampler state)

The same data, with names turned into the pass's name indices, is the parsed form's
m_ConstantBuffers / m_ConstantBufferBindings / m_TextureParams / m_BufferParams / m_UAVParams /
m_Samplers / m_Channels.

    data = compile_program(text, shader_dir, 'vp', 'vert', '2.5', ['PIXELSNAP_ON'])
    data.code, data.tail      # the record's program code and binding tail (shader_blob.subprogram)
    unity_lists(data.bindings, nameidx)
"""

import ctypes
import re
import struct
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from paths import CGINCLUDES, EDITOR_DATA, PROJECT

D3DCOMPILER = EDITOR_DATA / 'Tools' / 'D3Dcompiler_47.dll'

PLATFORM_D3D11 = 4  # the Shader object's platforms entry
GPU_TYPES = {('vp', 4): 15, ('fp', 4): 17, ('vp', 5): 16, ('fp', 5): 18}  # DX11VertexSM40 ... DX11PixelSM50

# D3DCOMPILE_* flags Unity passes: optimization level 3 and backwards compatibility.
OPTIMIZATION_LEVEL3 = 1 << 15
ENABLE_BACKWARDS_COMPATIBILITY = 1 << 12  # half globals in UnityShaderVariables.cginc need it
COMPILE_FLAGS = OPTIMIZATION_LEVEL3 | ENABLE_BACKWARDS_COMPATIBILITY
STRIP_REFLECTION_DATA, STRIP_DEBUG_INFO, STRIP_TEST_BLOBS, STRIP_PRIVATE_DATA = 1, 2, 4, 8
STRIP_FLAGS = STRIP_REFLECTION_DATA | STRIP_DEBUG_INFO | STRIP_TEST_BLOBS | STRIP_PRIVATE_DATA

# What Unity defines for every D3D11 program on a desktop target (Windows Standalone, tier 3).
TARGET = {
    '2.0': ['SHADER_TARGET=20'],
    '2.5': ['SHADER_TARGET=25', 'SHADER_REQUIRE_DERIVATIVES=1'],
    '3.0': [
        'SHADER_TARGET=30',
        'SHADER_REQUIRE_INTERPOLATORS10=1',
        'SHADER_REQUIRE_DERIVATIVES=1',
        'SHADER_REQUIRE_SAMPLELOD=1',
        'SHADER_REQUIRE_FRAGCOORD=1',
    ],
}
PLATFORM = [
    'UNITY_ENABLE_REFLECTION_BUFFERS=1',
    'UNITY_USE_DITHER_MASK_FOR_ALPHABLENDED_SHADOWS=1',
    'UNITY_PBS_USE_BRDF1=1',
    'UNITY_SPECCUBE_BOX_PROJECTION=1',
    'UNITY_SPECCUBE_BLENDING=1',
    'UNITY_ENABLE_DETAIL_NORMALMAP=1',
    'SHADER_API_DESKTOP=1',
    'UNITY_COLORSPACE_GAMMA=1',
    'UNITY_LIGHT_PROBE_PROXY_VOLUME=1',
    'UNITY_LIGHTMAP_FULL_HDR=1',
]
AVAILABLE = [
    'SHADER_TARGET_AVAILABLE=50',
    'SHADER_AVAILABLE_INTERPOLATORS10=1',
    'SHADER_AVAILABLE_INTERPOLATORS15=1',
    'SHADER_AVAILABLE_INTEGERS=1',
    'SHADER_AVAILABLE_MRT4=1',
    'SHADER_AVAILABLE_DERIVATIVES=1',
    'SHADER_AVAILABLE_SAMPLELOD=1',
    'SHADER_AVAILABLE_FRAGCOORD=1',
    'SHADER_AVAILABLE_2DARRAY=1',
    'SHADER_AVAILABLE_INSTANCING=1',
    'SHADER_AVAILABLE_GEOMETRY=1',
    'SHADER_AVAILABLE_MSAATEX=1',
    'SHADER_AVAILABLE_RANDOMWRITE=1',
    'SHADER_AVAILABLE_TESSHW=1',
    'SHADER_AVAILABLE_TESSELLATION=1',
    'SHADER_AVAILABLE_MRT8=1',
    'SHADER_AVAILABLE_COMPUTE=1',
    'SHADER_AVAILABLE_CUBEARRAY=1',
    'SHADER_API_D3D11=1',
]


# ---------------------------------------------------------------------------------------------
# d3dcompiler


class Blob(ctypes.c_void_p):
    """An ID3DBlob*: read it and release it."""

    def take(self):
        if not self.value:
            return b''
        vtbl = ctypes.cast(ctypes.cast(self, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        get_pointer = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(vtbl[3])
        get_size = ctypes.WINFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(vtbl[4])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        data = ctypes.string_at(get_pointer(self), get_size(self))
        release(self)
        return data


class Macro(ctypes.Structure):
    _fields_ = (('name', ctypes.c_char_p), ('definition', ctypes.c_char_p))


OPEN = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_uint),
)
CLOSE = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)


class Includes:
    """An ID3DInclude: `#include "x"` is looked up next to the including file, then in the
    shader's folder, the project and CGIncludes, as Unity's compiler does."""

    def __init__(self, dirs):
        self.dirs = [Path(d) for d in dirs]
        self.buffers = {}  # address -> (buffer, folder of that file)
        self.open_fn, self.close_fn = OPEN(self.open), CLOSE(self.close)
        self.vtbl = (ctypes.c_void_p * 2)(
            ctypes.cast(self.open_fn, ctypes.c_void_p), ctypes.cast(self.close_fn, ctypes.c_void_p)
        )
        self.obj = ctypes.pointer(ctypes.c_void_p(ctypes.addressof(self.vtbl)))

    def find(self, name, parent):
        here = self.buffers.get(parent, (None, None))[1]
        for folder in ([here] if here else []) + self.dirs:
            path = folder / name
            if path.is_file():
                return path
        return None

    def open(self, _this, _kind, name, parent, out_data, out_size):
        path = self.find(name.decode(), parent)
        if path is None:
            return -2147467259  # E_FAIL
        data = path.read_bytes()
        buffer = ctypes.create_string_buffer(data, len(data))
        address = ctypes.addressof(buffer)
        self.buffers[address] = (buffer, path.parent)
        out_data[0] = address
        out_size[0] = len(data)
        return 0

    def close(self, _this, data):
        self.buffers.pop(data, None)
        return 0


@cache
def library():
    lib = ctypes.WinDLL(str(D3DCOMPILER))
    lib.D3DCompile.argtypes = [
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.POINTER(Macro),
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.POINTER(Blob),
        ctypes.POINTER(Blob),
    ]
    lib.D3DStripShader.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint, ctypes.POINTER(Blob)]
    return lib


def d3dcompile(text, defines, entry, profile, include_dirs):
    """(DXBC with reflection, messages); the DXBC is None when the compile fails."""
    macros = (Macro * (len(defines) + 1))()
    for i, d in enumerate(defines):
        k, _, v = d.partition('=')
        macros[i] = Macro(k.encode(), (v or '1').encode())
    code, errors = Blob(), Blob()
    src = text.encode()
    includes = Includes(include_dirs)
    hr = library().D3DCompile(
        src, len(src), b'shader', macros, ctypes.cast(includes.obj, ctypes.c_void_p),
        entry.encode(), profile.encode(), COMPILE_FLAGS, 0, ctypes.byref(code), ctypes.byref(errors),
    )  # fmt: skip
    data, log = code.take(), errors.take().decode(errors='replace')
    return (data if hr >= 0 else None), log


def strip(dxbc):
    out = Blob()
    hr = library().D3DStripShader(dxbc, len(dxbc), STRIP_FLAGS, ctypes.byref(out))
    if hr < 0:
        raise OSError(f'D3DStripShader failed: {hr:#x}')
    return out.take()


# ---------------------------------------------------------------------------------------------
# DXBC reflection (RDEF, ISGN)


def chunks(dxbc):
    count = struct.unpack_from('<I', dxbc, 28)[0]
    out = {}
    for i in range(count):
        offset = struct.unpack_from('<I', dxbc, 32 + 4 * i)[0]
        size = struct.unpack_from('<I', dxbc, offset + 4)[0]
        out[dxbc[offset : offset + 4].decode()] = dxbc[offset + 8 : offset + 8 + size]
    return out


def cstr(data, offset):
    return data[offset : data.index(b'\0', offset)].decode()


# D3D_SHADER_INPUT_TYPE
SIT_CBUFFER, SIT_TBUFFER, SIT_TEXTURE, SIT_SAMPLER = 0, 1, 2, 3
SIT_UAV = {4, 6, 8, 9, 10, 11}
SIT_BUFFER = {5, 7}
# D3D_SRV_DIMENSION -> (Unity texture dimension, multisampled)
SRV_DIMENSION = {4: (2, 0), 6: (2, 1), 8: (3, 0), 9: (4, 0), 5: (5, 0), 7: (5, 1), 10: (6, 0), 2: (2, 0), 3: (5, 0)}
SVF_USED = 2
# D3D_SHADER_VARIABLE_CLASS / TYPE
CLASS_MATRIX_ROWS, CLASS_MATRIX_COLUMNS = 2, 3
VAR_TYPE = {3: 0, 2: 1, 19: 1, 1: 2}  # float -> 0, int/uint -> 1, bool -> 2


@dataclass
class Variable:
    name: str
    offset: int
    size: int
    used: bool
    cls: int
    vtype: int
    rows: int
    columns: int
    elements: int


@dataclass
class CBuffer:
    name: str
    size: int
    variables: list


@dataclass
class Resource:
    name: str
    kind: int
    dimension: int
    samples: int
    slot: int
    count: int


@dataclass
class Signature:
    semantic: str
    index: int
    system_value: int
    register: int
    mask: int
    used: int


def rdef(body):
    cb_count, cb_offset, res_count, res_offset = struct.unpack_from('<4I', body, 0)
    sm5 = body[17] >= 5  # the target's major version
    resources = []
    for i in range(res_count):
        name, kind, _ret, dim, samples, slot, count, _flags = struct.unpack_from('<8I', body, res_offset + 32 * i)
        resources.append(Resource(cstr(body, name), kind, dim, samples, slot, count))
    cbuffers = []
    for i in range(cb_count):
        name, var_count, var_offset, size, _flags, _kind = struct.unpack_from('<6I', body, cb_offset + 24 * i)
        variables = []
        stride = 40 if sm5 else 24
        for j in range(var_count):
            vname, start, vsize, vflags, type_offset, _default = struct.unpack_from(
                '<6I', body, var_offset + stride * j
            )
            cls, vtype, rows, columns, elements, _members = struct.unpack_from('<6H', body, type_offset)
            variables.append(
                Variable(cstr(body, vname), start, vsize, bool(vflags & SVF_USED), cls, vtype, rows, columns, elements)
            )
        cbuffers.append(CBuffer(cstr(body, name), size, variables))
    return cbuffers, resources


def signature(body):
    count, _ = struct.unpack_from('<2I', body, 0)
    out = []
    for i in range(count):
        name, index, sv, _ctype, register, mask, used = struct.unpack_from('<5I2B', body, 8 + 24 * i)
        out.append(Signature(cstr(body, name), index, sv, register, mask, used))
    return out


# ---------------------------------------------------------------------------------------------
# Unity's binding data


# Vertex input semantic -> Unity shader channel; TEXCOORDn is channel 4 + n.
SEMANTIC_CHANNEL = {'POSITION': 0, 'SV_POSITION': 0, 'NORMAL': 1, 'TANGENT': 2, 'COLOR': 3}


def channel(sem):
    name = sem.semantic.upper()
    if name == 'TEXCOORD':
        return 4 + sem.index, 5 + sem.index
    if name in SEMANTIC_CHANNEL:
        c = SEMANTIC_CHANNEL[name]
        return c, c
    return None


@dataclass
class Bindings:
    chans: list = field(default_factory=list)  # [(source, target)]
    src_map: int = 0
    cbuffers: list = field(default_factory=list)  # [(name, size, [param])]
    textures: list = field(default_factory=list)  # [(name, slot, sampler slot, dim, multisampled)]
    cb_bindings: list = field(default_factory=list)  # [(name, slot)]
    buffers: list = field(default_factory=list)  # [(name, slot)]
    uavs: list = field(default_factory=list)  # [(name, slot)]
    samplers: list = field(default_factory=list)  # [(slot, sampler state)]
    header: bytes = b''


def param(v):
    """(name, type, rows, columns, is matrix, array size, offset) of a used cbuffer variable."""
    matrix = v.cls in (CLASS_MATRIX_ROWS, CLASS_MATRIX_COLUMNS)
    rows, columns = (v.rows, v.columns) if matrix else (1, v.columns)
    return v.name, VAR_TYPE.get(v.vtype, 0), rows, columns, int(matrix), v.elements, v.offset


# Inline sampler names (sampler_linear_clamp_u...): filter in bits 0-1, wrap U/V/W in bits 2-7.
FILTERS = {'point': 0, 'linear': 1, 'trilinear': 2}
WRAPS = {'repeat': 0, 'clamp': 1, 'mirror': 2, 'mirroronce': 3}


def inline_sampler(name):
    words = name.lower().split('_')[1:] if name.lower().startswith('sampler') else name.lower().split('_')
    pattern = r'point|trilinear|linear|repeat|clamp|mirroronce|mirror|[a-z]+'
    words = [w for part in words for w in re.findall(pattern, part)]
    flt, wrap = 0, None
    per_axis = {}
    i = 0
    while i < len(words):
        w = words[i]
        if w in FILTERS:
            flt = FILTERS[w]
        elif w in WRAPS:
            axis = words[i + 1] if i + 1 < len(words) and words[i + 1] in ('u', 'v', 'w') else None
            if axis:
                per_axis[axis] = WRAPS[w]
                i += 1
            else:
                wrap = WRAPS[w]
        elif w == 'compare':
            flt |= 1 << 8
        i += 1
    state = flt
    for shift, axis in ((2, 'u'), (4, 'v'), (6, 'w')):
        state |= per_axis.get(axis, wrap or 0) << shift
    return state


def is_inline_sampler(name):
    n = name.lower()
    return any(f in n for f in FILTERS) and any(w in n for w in WRAPS)


def derive(dxbc, stage):
    """Unity's binding data from a DXBC that still has its reflection."""
    parts = chunks(dxbc)
    cbuffers, resources = rdef(parts['RDEF'])
    b = Bindings()
    if stage == 'vp':
        # the source map has every declared input, the channel list only the ones the code reads
        inputs = [s for s in signature(parts['ISGN']) if s.system_value == 0]
        b.chans = list(dict.fromkeys(c for c in (channel(s) for s in inputs if s.used) if c))  # signature order
        b.src_map = sum(1 << c for c in {c[0] for c in (channel(s) for s in inputs) if c})
    samplers = {r.name: r.slot for r in resources if r.kind == SIT_SAMPLER}
    by_name = {c.name: c for c in cbuffers}
    srv_slots = cb_slots = sampler_slots = uav_slots = 0
    for r in resources:
        end = r.slot + max(r.count, 1)
        if r.kind == SIT_TEXTURE:
            dim, ms = SRV_DIMENSION.get(r.dimension, (2, 0))
            if r.dimension == 1:  # Buffer<T>
                b.buffers.append((r.name, r.slot))
            else:
                # sampler2D declares a sampler of the texture's own name, UNITY_DECLARE_TEX2D sampler<name>
                sampler = samplers.get(r.name, samplers.get('sampler' + r.name, -1))
                b.textures.append((r.name, r.slot, sampler, dim, ms))
            srv_slots = max(srv_slots, end)
        elif r.kind in SIT_BUFFER or r.kind == SIT_TBUFFER:
            b.buffers.append((r.name, r.slot))
            srv_slots = max(srv_slots, end)
        elif r.kind == SIT_CBUFFER:
            cb = by_name[r.name]
            # Unity lists a cbuffer's vectors before its matrices, each in declaration order
            params = sorted((param(v) for v in cb.variables if v.used), key=lambda p: p[4])
            b.cbuffers.append((cb.name, cb.size, params))
            b.cb_bindings.append((r.name, r.slot))
            cb_slots = max(cb_slots, end)
        elif r.kind == SIT_SAMPLER:
            if is_inline_sampler(r.name):
                b.samplers.append((r.slot, inline_sampler(r.name)))
            sampler_slots = max(sampler_slots, end)
        elif r.kind in SIT_UAV:
            b.uavs.append((r.name, r.slot))
            uav_slots = max(uav_slots, end)
    b.header = bytes([1, srv_slots, cb_slots, sampler_slots, uav_slots, 0])
    return b


def lstr(s):
    data = s.encode()
    return struct.pack('<I', len(data)) + data + b'\0' * ((-len(data)) % 4)


def tail_bytes(b):
    out = struct.pack('<II', b.src_map, len(b.chans)) + b''.join(struct.pack('<ii', s, t) for s, t in b.chans)
    out += struct.pack('<I', 1 + len(b.cbuffers))
    out += struct.pack('<4I', 0, 0, 0, 0)  # the loose-uniform group: no name, size 0, no params or structs
    for name, size, params in b.cbuffers:
        out += lstr(name) + struct.pack('<II', size, len(params))
        for p in params:
            out += lstr(p[0]) + struct.pack('<6i', *p[1:])
        out += struct.pack('<I', 0)
    binds = [lstr(n) + struct.pack('<4i', 0, slot, sampler, dim * 2 + ms) for n, slot, sampler, dim, ms in b.textures]
    binds += [lstr(n) + struct.pack('<3i', 1, slot, 0) for n, slot in b.cb_bindings]
    binds += [lstr(n) + struct.pack('<3i', 2, slot, 0) for n, slot in b.buffers]
    binds += [lstr(n) + struct.pack('<3i', 3, slot, 0) for n, slot in b.uavs]
    binds += [lstr('') + struct.pack('<3i', 4, slot, state) for slot, state in b.samplers]
    return out + struct.pack('<I', len(binds)) + b''.join(binds)


def names(b):
    """The names a subprogram adds to its pass's name table, in order of first use: its bindings
    as the record lists them (textures, then constant buffers), the constant buffers' parameters,
    then buffers and UAVs. The keywords follow (the caller adds them)."""
    out = [t[0] for t in b.textures] + [n for n, _ in b.cb_bindings]
    for _, _, params in b.cbuffers:
        out += [p[0] for p in params]
    return out + [n for n, _ in b.buffers] + [n for n, _ in b.uavs]


def unity_lists(b, nameidx):
    """The parsed form's per-subprogram lists, given the pass's name -> index map."""

    def vector(p):
        return {'m_NameIndex': nameidx[p[0]], 'm_Index': p[6], 'm_ArraySize': p[5], 'm_Type': p[1], 'm_Dim': p[3]}

    def matrix(p):
        return {'m_NameIndex': nameidx[p[0]], 'm_Index': p[6], 'm_ArraySize': p[5], 'm_Type': p[1], 'm_RowCount': p[2]}

    return {
        'm_Channels': {'m_Channels': [{'source': s, 'target': t} for s, t in b.chans], 'm_SourceMap': b.src_map},
        'm_VectorParams': [],
        'm_MatrixParams': [],
        'm_TextureParams': [
            {
                'm_NameIndex': nameidx[n],
                'm_Index': slot,
                'm_SamplerIndex': sampler,
                'm_MultiSampled': bool(ms),
                'm_Dim': dim,
            }
            for n, slot, sampler, dim, ms in b.textures
        ],
        'm_BufferParams': [{'m_NameIndex': nameidx[n], 'm_Index': slot} for n, slot in b.buffers],
        'm_ConstantBuffers': [
            {
                'm_NameIndex': nameidx[name],
                'm_MatrixParams': [matrix(p) for p in params if p[4]],
                'm_VectorParams': [vector(p) for p in params if not p[4]],
                'm_StructParams': [],
                'm_Size': size,
            }
            for name, size, params in b.cbuffers
        ],
        'm_ConstantBufferBindings': [{'m_NameIndex': nameidx[n], 'm_Index': slot} for n, slot in b.cb_bindings],
        'm_UAVParams': [{'m_NameIndex': nameidx[n], 'm_Index': slot, 'm_OriginalIndex': slot} for n, slot in b.uavs],
        'm_Samplers': [{'sampler': state, 'bindPoint': slot} for slot, state in b.samplers],
    }


# ---------------------------------------------------------------------------------------------
# Compiling one program


def program_text(text, prog, incs):
    """The .hlsl text Unity hands the D3D compiler for one program block (shaderlab.unity_input
    without the PS Vita header)."""
    nl = '\r\n' if '\r\n' in text else '\n'
    line = prog.line + 1
    dummy = f'#ifdef DUMMY_PREPROCESSOR_TO_WORK_AROUND_HLSL_COMPILER_LINE_HANDLING{nl}#endif{nl}'
    out = f'#line {line} ""{nl}{dummy}{nl}'
    if prog.lang == 'CG':
        out += f'#include "HLSLSupport.cginc"{nl}'
    out += f'#define UNITY_INSTANCED_LOD_FADE{nl}#define UNITY_INSTANCED_SH{nl}#define UNITY_INSTANCED_LIGHTMAPSTS{nl}'
    if prog.lang == 'CG':
        out += f'#include "UnityShaderVariables.cginc"{nl}#include "UnityShaderUtilities.cginc"{nl}'
    for inc in incs:
        if inc.lang != prog.lang:
            continue
        out += f'#line {inc.line + 1}{nl}' + inc.body.replace('#pragma', '//#prag')
    out += f'#line {line} ""{nl}{dummy}/* UNITY: Original start of shader */' + prog.body.replace('#pragma', '//#prag')
    return out


def target_number(target):
    return round(float(target) * 10)


MAX_SHADER_TARGET = 46


def defines(target, stage, keywords):
    t = target_number(target)
    if target in TARGET:
        head = TARGET[target]
    else:
        # `#pragma target 5.0` compiles as ps_5_0 but SHADER_TARGET stays below 50: the reference's
        # post-processing shaders take their SHADER_TARGET < 50 paths (no gather). 45 and 46 give
        # the same code for every shader here; 46 is the highest named target below 5.0.
        head = [f'SHADER_TARGET={min(t, MAX_SHADER_TARGET)}', *requirement_defines(t)]
    return (
        head
        + PLATFORM
        + [k + '=1' for k in keywords]
        + ['UNITY_VERSION=201839', 'SHADER_STAGE_VERTEX=1' if stage == 'vp' else 'SHADER_STAGE_FRAGMENT=1']
        + AVAILABLE
    )


def requirement_defines(t):
    out = ['SHADER_REQUIRE_INTERPOLATORS10=1', 'SHADER_REQUIRE_DERIVATIVES=1', 'SHADER_REQUIRE_SAMPLELOD=1',
           'SHADER_REQUIRE_FRAGCOORD=1']  # fmt: skip
    if t >= 35:
        out += ['SHADER_REQUIRE_INTERPOLATORS15=1', 'SHADER_REQUIRE_INTEGERS=1', 'SHADER_REQUIRE_MRT4=1',
                'SHADER_REQUIRE_2DARRAY=1', 'SHADER_REQUIRE_INSTANCING=1']  # fmt: skip
    if t >= 40:
        out += ['SHADER_REQUIRE_GEOMETRY=1']
    if t >= 45:
        out += ['SHADER_REQUIRE_RANDOMWRITE=1', 'SHADER_REQUIRE_COMPUTE=1', 'SHADER_REQUIRE_MSAATEX=1']
    if t >= 46:
        out += ['SHADER_REQUIRE_TESSELLATION=1', 'SHADER_REQUIRE_TESSHW=1']
    if t >= 50:
        out += ['SHADER_REQUIRE_MRT8=1', 'SHADER_REQUIRE_CUBEARRAY=1']
    return out


def shader_model(target):
    return 5 if target_number(target) >= 45 else 4


@dataclass
class Compiled:
    bindings: Bindings
    code: bytes  # the record's program code: Unity's header and the stripped DXBC
    tail: bytes
    gpu_type: int
    stats: tuple = (0, 0, 0, 0)  # the record's ALU, texture, flow control and temp register counts


def stats(dxbc):
    """(ALU, TEX, flow, temps) for the subprogram record, from the compiler's STAT chunk: float,
    int and uint instructions; plain texture samples (loads, comparison, bias and gradient
    samples not counted); static and dynamic flow control without the closing ret; temp
    registers."""
    stat = chunks(dxbc).get('STAT')
    if stat is None:
        return (0, 0, 0, 0)
    w = struct.unpack_from(f'<{len(stat) // 4}I', stat)
    alu = w[4] + w[5] + w[6]
    tex = w[14]
    flow = max(w[7] + w[8] - 1, 0)
    return (alu, tex, flow, w[1])


# Keywords that raise a snippet's requirements to target 3.0 (the snippet's m_KeywordTargetInfo,
# the same for every multi_compile_fwdbase / fwdadd snippet): such variants compile with
# SHADER_TARGET=30, which changes what surface shaders interpolate.
KEYWORD_TARGET = dict.fromkeys(
    ('SHADOWS_SOFT', 'DIRLIGHTMAP_COMBINED', 'DIRLIGHTMAP_SEPARATE', 'DYNAMICLIGHTMAP_ON', 'SHADOWS_SCREEN'), '3.0'
)


def effective_target(target, keywords):
    for k in keywords:
        raised = KEYWORD_TARGET.get(k)
        if raised and target_number(raised) > target_number(target):
            target = raised
    return target


def compile_program(text, shader_dir, stage, entry, target, keywords, project=PROJECT):
    """(Compiled, messages); Compiled is None when D3DCompile rejects the program. `project` is the
    root that `#include "Assets/..."` resolves against."""
    target = effective_target(target, keywords)
    sm = shader_model(target)
    profile = f'{"vs" if stage == "vp" else "ps"}_{sm}_0'
    dirs = [shader_dir, project, CGINCLUDES]
    dxbc, log = d3dcompile(text, defines(target, stage, keywords), entry, profile, dirs)
    if dxbc is None:
        return None, log
    b = derive(dxbc, stage)
    return Compiled(b, b.header + strip(dxbc), tail_bytes(b), GPU_TYPES[(stage, sm)], stats(dxbc)), log


# ---------------------------------------------------------------------------------------------
# Which program text each pass compiles


@dataclass
class PassProgram:
    text: str  # what D3DCompile gets
    shader_dir: Path
    vertex: str
    fragment: str
    target: str


def from_block(text, program, includes, shader_dir):
    from shaderlab import pragmas

    found = dict(pragmas(''.join(i.body for i in includes if i.lang == program.lang) + program.body))
    if 'surface' in found or 'vertex' not in found:
        return None
    return PassProgram(
        program_text(text, program, includes),
        shader_dir,
        found['vertex'][0],
        found['fragment'][0],
        found.get('target', ['2.5'])[0],
    )


def from_snippet(code):
    """A program from the editor's stored snippet text (built-in shaders: surface shaders arrive
    generated, fixed-function passes converted to HLSL)."""
    from shaderlab import pragmas

    found = dict(pragmas(code))
    return PassProgram(
        code.replace('#pragma', '//#prag'),
        CGINCLUDES,
        found['vertex'][0],
        found['fragment'][0],
        found.get('target', ['2.5'])[0],
    )


def pass_programs(form, source_text=None, shader_dir=None, editor_tree=None):
    """{(subshader, pass): PassProgram} for the passes of a parsed form that carry programs.

    Program blocks of the source go to their passes; when the source's pass numbering does not
    fit the form (a `Pass [_StencilOp]` property counted as a pass), a subshader's blocks go to
    its passes in order. Passes left over (surface shaders, fixed-function passes) take the
    editor's stored snippets for built-in shaders: generated (CG) snippets in snippet order to
    passes that have no block, converted fixed-function snippets (no asset path) to passes the
    source writes without a program block."""
    from shaderlab import scan

    out = {}
    blocks = {}
    if source_text is not None:
        programs, includes = scan(source_text)
        for p in programs:
            blocks.setdefault(p.sub, []).append(p)
        for si, sub in enumerate(form['m_SubShaders']):
            mine = sorted(blocks.get(si, []), key=lambda p: p.line)
            npasses = len(sub['m_Passes'])
            by_index = {p.passi: p for p in mine}
            if any(i >= npasses for i in by_index) and len(mine) == npasses:
                by_index = dict(enumerate(mine))
            for pi, p in by_index.items():
                if pi < npasses:
                    prog = from_block(source_text, p, includes, shader_dir)
                    if prog:
                        out[(si, pi)] = prog
    if editor_tree is not None:
        snippets = sorted(
            ((k, v) for k, v in editor_tree['m_CompileInfo']['m_Snippets'] if not v['m_FromOther']),
            key=lambda kv: kv[0] >> 16,
        )
        generated = [v['m_Code'] for _, v in snippets if v['m_AssetPath']]
        fixed = [v['m_Code'] for _, v in snippets if not v['m_AssetPath']]
        for si, sub in enumerate(form['m_SubShaders']):
            has_blocks = any(key[0] == si for key in out)  # surface blocks yield no program
            for pi, ps in enumerate(sub['m_Passes']):
                if (si, pi) in out or ps['m_Type'] != 0 or not ps['progVertex']['m_SubPrograms']:
                    continue
                pool = fixed if has_blocks else generated
                if pool:
                    out[(si, pi)] = from_snippet(pool.pop(0))
    return out
