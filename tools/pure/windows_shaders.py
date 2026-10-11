"""Shader objects (class 48) for the Windows player: shader_object.py's parsed form, render
state, variants and keyword registry, with Direct3D 11 programs (dx11.py) in the blob.

What differs from the PS Vita build (verified against builds/drifter-2018.2.19f1 by
check_windows_shaders.py):

- One platform, 4 (d3d11), whose subprograms are DX11VertexSM40/SM50 and DX11PixelSM40/SM50.
- Variants: instancing, procedural instancing and stereo multiview/instancing are stripped
  (no material enables instancing, VR is off); ETC1 split alpha is kept. Passes above target
  3.0 compile (SM 5.0 where the target asks for 4.5 or more).
- A pass's name table: per subprogram (vertex ones, then fragment ones) the names of its bindings
  in the record's order (textures, then constant buffers), its constant buffers' parameters,
  its buffers and UAVs, then its keywords.
- m_ShaderRequirements: the target's requirements (REQUIREMENTS) and the keywords' (the
  editor's m_KeywordTargetInfo).

    shaders = WindowsShaders(layout, placement, project)
    trees = shaders.build_all(shader_object.build_order(files))   # {(file, path ID): tree}
"""

import hashlib
import itertools
import pickle
import struct
import uuid

import dx11
import lz4.block
import shader_object as so
from shader_blob import blob as assemble_blob
from shader_blob import subprogram as blob_record
from shaderlab import pragmas

PLATFORM_D3D11 = dx11.PLATFORM_D3D11
CACHE = so.WORK / 'windows' / 'dx11_cache'
# Variants a Windows player never gets here: instancing (no material enables it) and VR.
STRIPPED = {'INSTANCING_ON', 'PROCEDURAL_INSTANCING_ON', 'STEREO_INSTANCING_ON', 'STEREO_MULTIVIEW_ON'}
# ShaderRequirements of each `#pragma target` on Direct3D 11. 5.0 records as 4.5 does (the
# reference has both); 4.0 and 4.6 are not used here.
REQUIREMENTS = {'2.0': 1, '2.5': 33, '3.0': 227, '3.5': 3819, '4.5': 1101547, '5.0': 1101547}
KEYWORD_REQUIREMENTS = {
    **so.KEYWORD_REQUIREMENTS,
    'INSTANCING_ON': 2048, 'PROCEDURAL_INSTANCING_ON': 16384, 'STEREO_MULTIVIEW_ON': 3819,
    'STEREO_INSTANCING_ON': 3819,
}  # fmt: skip


def requirements(target, keywords):
    out = REQUIREMENTS[target]
    for k in keywords:
        out |= KEYWORD_REQUIREMENTS.get(k, 0)
    return out


def renderers_ok(program, includes):
    """Whether a program block's only_renderers / exclude_renderers let Direct3D 11 in."""
    info = dict(pragmas(''.join(i.body for i in includes if i.lang == program.lang) + program.body))
    if 'only_renderers' in info and 'd3d11' not in info['only_renderers']:
        return False
    return not ('exclude_renderers' in info and 'd3d11' in info['exclude_renderers'])


def enumerate_variants(snippet, material_sets=(), all_features=False, fog_modes=()):
    """shader_object.enumerate_variants with the Windows player's stripping."""
    unsupported = [s[1] for _, s in snippet.directives if isinstance(s, tuple)]
    if unsupported:
        raise NotImplementedError(f'#pragma {unsupported[0]}')
    features = {k for kind, s in snippet.directives if kind == 'shader_feature' for k in s if k}
    allowed = None
    if features and not all_features:
        allowed = {frozenset()} | {frozenset(keywords) & features for keywords in material_sets}
    out = []
    for combo in itertools.product(*(s for _, s in snippet.directives)):
        keywords = [k for k in combo if k]
        if allowed is not None and frozenset(k for k in keywords if k in features) not in allowed:
            continue
        if any(k in STRIPPED for k in keywords):
            continue
        if any(k in so.FOG_KEYWORDS and so.FOG_KEYWORDS[k] not in fog_modes for k in keywords):
            continue
        out.append(keywords)
    return out


# ---------------------------------------------------------------------------------------------
# Compiling, with results kept under work/pure/windows


def cache_path(*key):
    digest = hashlib.sha256(repr(('dx11-3', *key)).encode()).hexdigest()
    return CACHE / digest[:2] / f'{digest}.pickle'


def compile_one(text, shader_dir, project, stage, entry, target, keywords):
    path = cache_path(text, str(shader_dir), str(project), stage, entry, target, sorted(keywords), keywords)
    try:
        return pickle.loads(path.read_bytes())
    except (OSError, EOFError, pickle.UnpicklingError):
        pass
    result, _ = dx11.compile_program(text, shader_dir, stage, entry, target, keywords, project=project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f'.{uuid.uuid4().hex}.tmp')
    tmp.write_bytes(pickle.dumps(result))
    try:
        tmp.replace(path)
    except PermissionError:  # someone is reading the same result right now
        tmp.unlink(missing_ok=True)
    return result


def compile_variants(text, shader_dir, project, info, variants, pool):
    """[(vertex Compiled, fragment Compiled) or None] per variant."""
    jobs = [(stage, entry, kw) for kw in variants for stage, entry in (('vp', info.vertex), ('fp', info.fragment))]
    results = list(pool.map(lambda j: compile_one(text, shader_dir, project, j[0], j[1], info.target, j[2]), jobs))
    return [None if v is None or f is None else (v, f) for v, f in zip(results[::2], results[1::2], strict=True)]


def subprogram_dict(blob_index, compiled, keyword_indices, reqs, nameidx):
    lists = dx11.unity_lists(compiled.bindings, nameidx)
    return {
        'm_BlobIndex': blob_index,
        'm_Channels': lists['m_Channels'],
        'm_KeywordIndices': keyword_indices,
        'm_ShaderHardwareTier': so.HARDWARE_TIER,
        'm_GpuProgramType': compiled.gpu_type,
        'm_VectorParams': lists['m_VectorParams'],
        'm_MatrixParams': lists['m_MatrixParams'],
        'm_TextureParams': lists['m_TextureParams'],
        'm_BufferParams': lists['m_BufferParams'],
        'm_ConstantBuffers': lists['m_ConstantBuffers'],
        'm_ConstantBufferBindings': lists['m_ConstantBufferBindings'],
        'm_UAVParams': lists['m_UAVParams'],
        'm_Samplers': lists['m_Samplers'],
        'm_ShaderRequirements': reqs,
    }


def record(c, keywords):
    """A subprogram record: shader_blob's layout with the compiler statistics filled in."""
    data = bytearray(blob_record(c.gpu_type, keywords, c.code, c.tail))
    data[8:24] = struct.pack('<4i', *c.stats)
    return bytes(data)


def fill_programs(out, stages, records, requirements_of):
    """Put a pass's compiled programs into its parsed form and the blob records.

    stages: [('progVertex' / 'progFragment', [(keywords, Compiled)])] in stage order;
    requirements_of(keywords) gives a subprogram's m_ShaderRequirements. records is a dict
    record -> blob index: a record the shader already has (a pass reusing another's vertex
    program) is not stored twice."""
    names = {}
    for _, items in stages:
        for keywords, c in items:
            for n in [*dx11.names(c.bindings), *keywords]:
                names.setdefault(n, len(names))
    out['m_NameIndices'] = sorted(names.items())
    for key, items in stages:
        subs = []
        for keywords, c in items:
            reqs = requirements_of(keywords)
            index = records.setdefault(record(c, keywords), len(records))
            subs.append(subprogram_dict(index, c, [names[k] for k in keywords], reqs, names))
        out[key] = {'m_SubPrograms': subs}


# ---------------------------------------------------------------------------------------------
# The Shader object


def build(source, options, registry, pool, project):
    """The Windows release Shader tree for a parsed ShaderSource."""
    shader_dir = source.path.parent
    records, subshaders = {}, []
    snippets = [p.program for sub in source.subshaders for p in sub.passes if p.program is not None]
    order = {id(p): i for i, p in enumerate(sorted(snippets, key=lambda p: p.line))}
    for sub in source.subshaders:
        passes = []
        for ps in sub.passes:
            state = so.default_state(ps.kind != so.PASS_NORMAL)
            out = {
                'm_NameIndices': [],
                'm_Type': ps.kind,
                'm_State': state,
                'm_ProgramMask': 0,
                'progVertex': so.empty_program(),
                'progFragment': so.empty_program(),
                'progGeometry': so.empty_program(),
                'progHull': so.empty_program(),
                'progDomain': so.empty_program(),
                'm_HasInstancingVariant': False,
                'm_HasProceduralInstancingVariant': False,
                'm_UseName': ps.use_name,
                'm_Name': '',
                'm_TextureName': ps.texture_name,
                'm_Tags': {'tags': []},
            }
            passes.append(out)
            if ps.kind == so.PASS_GRAB:
                out['m_Tags'] = {'tags': so.tag_list(ps.tags)}
            if ps.kind != so.PASS_NORMAL:
                continue
            so.apply_state(state, ps.commands)
            state['m_Name'] = ps.name
            info = ps.program and so.snippet_info(ps.program, source.includes)
            extra = {'SHADOWSUPPORT': 'true'} if info and info.shadow_support else {}
            state['m_Tags'] = {'tags': so.tag_list(sub.tags, ps.tags, extra)}
            state['m_LOD'] = sub.lod if ps.lod is None else ps.lod
            if ps.program is None:
                continue
            index = order[id(ps.program)]
            code = (options.snippet_codes or {}).get(index) or so.snippet_code(source.text, ps.program, source.includes)
            known = (options.snippet_ids or {}).get(index)
            state['gpuProgramID'] = known if known is not None else so.gpu_program_id(index, code)
            out['m_ProgramMask'] = info.program_mask
            registry.register(info)
            out['m_HasInstancingVariant'] = info.instancing
            out['m_HasProceduralInstancingVariant'] = info.procedural_instancing
            if ps.program.lang == 'GLSL' or not renderers_ok(ps.program, source.includes):
                continue
            variants = enumerate_variants(info, options.material_sets, options.all_features, options.fog_modes)
            variants = [registry.order(v) for v in variants]
            text = dx11.program_text(source.text, ps.program, source.includes)
            results = compile_variants(text, shader_dir, project, info, variants, pool)
            kept = [(v, *r) for v, r in zip(variants, results, strict=True) if r is not None]
            stages = (('progVertex', [(kw, v) for kw, v, _ in kept]), ('progFragment', [(kw, f) for kw, _, f in kept]))
            fill_programs(out, stages, records, lambda keywords, t=info.target: requirements(t, keywords))
        subshaders.append({'m_Passes': passes, 'm_Tags': {'tags': so.tag_list(sub.tags)}, 'm_LOD': sub.lod})

    raw = assemble_blob(list(records))
    compressed = lz4.block.compress(raw, mode='high_compression', compression=9, store_size=False)
    dependency = options.dependency or (lambda name: None)
    deps = []
    for name in [source.fallback] + [to for _, to in source.dependencies]:
        pptr = dependency(name) if name else None
        if pptr is not None and pptr not in deps:
            deps.append(pptr)
    return {
        'm_Name': '',
        'm_ParsedForm': {
            'm_PropInfo': {'m_Props': [so.prop_dict(p) for p in source.props]},
            'm_SubShaders': subshaders,
            'm_Name': source.name,
            'm_CustomEditorName': source.custom_editor,
            'm_FallbackName': source.fallback,
            'm_Dependencies': [{'from': a, 'to': b} for a, b in source.dependencies],
            'm_DisableNoSubshadersMessage': source.fallback_off,
        },
        'platforms': [PLATFORM_D3D11],
        'offsets': [0],
        'compressedLengths': [len(compressed)],
        'decompressedLengths': [len(raw)],
        'compressedBlob': compressed,
        'm_Dependencies': deps,
        'm_NonModifiableTextures': [],
        'm_ShaderIsBaked': True,
    }


# The engine's built-in keywords, which take the first places in the global keyword table (in
# this order, from UnityPlayer.dll): a variant lists them before any keyword a shader declares.
BUILTIN_KEYWORDS = [
    'SPOT',
    'DIRECTIONAL',
    'DIRECTIONAL_COOKIE',
    'POINT',
    'POINT_COOKIE',
    'SHADOWS_DEPTH',
    'SHADOWS_SCREEN',
    'SHADOWS_CUBE',
    'SHADOWS_SOFT',
    'SHADOWS_SPLIT_SPHERES',
    'SHADOWS_SINGLE_CASCADE',
    'LIGHTMAP_ON',
    'DIRLIGHTMAP_COMBINED',
    'DYNAMICLIGHTMAP_ON',
    'LIGHTMAP_SHADOW_MIXING',
    'SHADOWS_SHADOWMASK',
    'LIGHTPROBE_SH',
    'FOG_LINEAR',
    'FOG_EXP',
    'FOG_EXP2',
    '_EMISSION',
    'VERTEXLIGHT_ON',
    'SOFTPARTICLES_ON',
    'UNITY_HDR_ON',
    'LOD_FADE_CROSSFADE',
    'INSTANCING_ON',
    'PROCEDURAL_INSTANCING_ON',
    'UNITY_SINGLE_PASS_STEREO',
    'ETC1_EXTERNAL_ALPHA',
    'STEREO_INSTANCING_ON',
    'STEREO_MULTIVIEW_ON',
    'STEREO_CUBEMAP_RENDER_ON',
    'EDITOR_VISUALIZATION',
]


class WindowsShaders(so.ProjectShaders):
    """shader_object.ProjectShaders building Direct3D 11 Shader objects."""

    def __init__(self, layout, placement, project):
        super().__init__(layout, placement, project)
        for k in BUILTIN_KEYWORDS:
            self.registry.index.setdefault(k, len(self.registry.index))

    def build(self, file_name, guid, file_id):
        src = self.source(guid, file_id)
        ids = codes = None
        if guid == so.BUILTIN_EXTRA:
            ids, codes = so.builtin_options(file_id)
            always = file_id in self.always_builtin
        else:
            always = guid in self.always
        options = so.BuildOptions(
            material_sets=tuple(self.materials.get((guid, file_id), ())),
            all_features=always,
            fog_modes=self.fog,
            snippet_ids=ids,
            snippet_codes=codes,
            dependency=self.dependency(file_name),
        )
        return build(src, options, self.registry, self.pool, self.project)


def imported(build_, file_name, guid, file_id, asset):
    """build.Build.IMPORTED_FOR['windows'][48]: (Shader tree, None), or None. The first call
    builds every shader of the build at once, in shader_object.build_order()."""
    made = getattr(build_, 'shader_objects', None)
    if made is None:
        shaders = WindowsShaders(build_.layout, build_.placement, build_.root)
        made = build_.shader_objects = shaders.build_all(so.build_order(build_.files))
        shaders.pool.shutdown()
    where = build_.placement.where.get((guid, file_id))
    if where is None or where[0] != file_name or where not in made:
        return None
    return made[where], None
