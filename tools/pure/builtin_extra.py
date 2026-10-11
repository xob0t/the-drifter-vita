"""Build the player's Resources/unity_builtin_extra: the built-in shaders the engine needs.

What Unity 2018.2's player build writes there (all Shader objects, path ID = built-in fileID):

- the built-in shaders GraphicsSettings names and the build does not copy elsewhere (Always
  Included Shaders, the deferred / legacy deferred / depth-normals shaders), when the editor has
  them (2018.2 lacks some of the default list, e.g. 111, 15312, 19011);
- the shaders those depend on (Fallback, Dependency, UsePass), from the editor's m_Dependencies;
- the project's Always Included shaders, each written under its own fileID 4800000: every one is
  appended to the file and the object table keeps the last, so the earlier ones remain in the
  file as unreferenced bytes before it.

The editor's own unity_builtin_extra holds each built-in shader's parsed form (render state,
gpuProgramIDs and all) and its program snippets as the editor compiled them (also the generated
code of surface and fixed function shaders), with their variant lists. The build compiles those
snippets for the PS Vita: snippets whose platform mask leaves out PSP2 (bit 10) or whose
requirements go beyond shader model 3.0 get no programs; variants of keywords the PS Vita cannot
have (instancing, stereo), of fog modes and lightmap modes no scene uses are stripped.

    data = build_extra(build)        # bytes of Media/Resources/unity_builtin_extra
"""

import copy
import itertools
import re
import struct
from concurrent.futures import ThreadPoolExecutor
from functools import cache

import convert
import lz4.block
import psp2cgc
import shader_object as so
import unityyaml
from container import HeaderInfo, ObjectEntry, TypeEntry, write_serialized
from paths import PROJECT
from shader_blob import blob as assemble_blob
from shaderlab import pragmas
from UnityPy.helpers.TypeTreeHelper import write_typetree
from UnityPy.streams import EndianBinaryWriter

NAME = 'Resources/unity_builtin_extra'
BUILTIN_EXTRA = so.BUILTIN_EXTRA
PROJECT_SHADER = 4800000
PSP2_PLATFORM_BIT = 1 << so.PLATFORM_PSP2
PSP2_REQUIREMENTS = psp2cgc.REQUIREMENTS['3.0']  # all a variant may need
TARGET_OF = {v: k for k, v in psp2cgc.REQUIREMENTS.items()}
LANGUAGES = {0, 1}  # Cg and HLSL snippets; GLSL ones never compile for the PS Vita
LIGHTMAP_KEYWORDS = {
    'LIGHTMAP_ON', 'DIRLIGHTMAP_COMBINED', 'DIRLIGHTMAP_SEPARATE', 'DYNAMICLIGHTMAP_ON', 'SHADOWS_SHADOWMASK',
    'LIGHTMAP_SHADOW_MIXING',
}  # fmt: skip
# Unity's built-in keywords in its keyword table order: a variant lists them in this order, before
# user keywords. Only the relative order of keywords that occur together matters.
BUILTIN_KEYWORDS = [
    'SPOT', 'DIRECTIONAL', 'DIRECTIONAL_COOKIE', 'POINT', 'POINT_COOKIE', 'SHADOWS_DEPTH', 'SHADOWS_SCREEN',
    'SHADOWS_CUBE', 'SHADOWS_SOFT', 'SHADOWS_SPLIT_SPHERES', 'SHADOWS_SINGLE_CASCADE', 'LIGHTMAP_ON',
    'DIRLIGHTMAP_COMBINED', 'DIRLIGHTMAP_SEPARATE', 'DYNAMICLIGHTMAP_ON', 'SHADOWS_SHADOWMASK',
    'LIGHTMAP_SHADOW_MIXING', 'LIGHTPROBE_SH', 'VERTEXLIGHT_ON', 'UNITY_HDR_ON', 'FOG_LINEAR', 'FOG_EXP',
    'FOG_EXP2', 'INSTANCING_ON', 'PROCEDURAL_INSTANCING_ON', 'STEREO_MULTIVIEW_ON', 'STEREO_INSTANCING_ON',
]  # fmt: skip
BUILTIN_SOURCE_DIR = so.BUILTIN_SOURCE / 'DefaultResourcesExtra'


# ---------------------------------------------------------------------------------------------
# What goes in


def graphics_refs(project=PROJECT):
    """(fileID, guid) of every reference in GraphicsSettings, in file order."""
    text = (project / 'ProjectSettings' / 'GraphicsSettings.asset').read_text(encoding='utf-8')
    return [(int(fid), g) for fid, g in re.findall(r'fileID: (-?\d+), guid: ([0-9a-f]{32})', text)]


def always_included_project(project=PROJECT):
    """GUIDs of the project's Always Included shaders, in list order."""
    text = (project / 'ProjectSettings' / 'GraphicsSettings.asset').read_text(encoding='utf-8')
    block = text.split('m_AlwaysIncludedShaders:')[1].split('m_PreloadedShaders')[0]
    refs = re.findall(r'fileID: (-?\d+), guid: ([0-9a-f]{32})', block)
    return [g for fid, g in refs if g != BUILTIN_EXTRA and int(fid) == PROJECT_SHADER]


def builtin_shader_ids(layout, project=PROJECT):
    """Path IDs of the built-in shaders the file holds, ascending."""
    editor = so.editor_builtin_shaders()
    wanted = [fid for fid, g in graphics_refs(project) if g == BUILTIN_EXTRA and fid in editor]
    wanted = [fid for fid in wanted if fid not in layout.builtin_file]  # copied into another file
    out, stack = set(), list(wanted)
    while stack:
        fid = stack.pop()
        if fid in out or fid not in editor:
            continue
        out.add(fid)
        stack += [d['m_PathID'] for d in editor[fid]['m_Dependencies'] if d['m_FileID'] == 0]
    return sorted(out)


# ---------------------------------------------------------------------------------------------
# Variant stripping the build applies


def lighting_assets(layout, project=PROJECT):
    """The LightingDataAsset documents of the build's scenes."""
    for scene in layout.scenes:
        text = (project / scene['path']).read_text(encoding='utf-8', errors='replace')
        m = re.search(r'm_LightingDataAsset: \{fileID: \d+, guid: ([0-9a-f]{32})', text)
        a = m and layout.db.by_guid.get(m[1])
        if a:
            yield from (d.fields for d in unityyaml.load(project / a.path) if d.class_id == 1120)


def lightmap_keywords_kept(layout, project=PROJECT):
    """Lightmap keywords whose variants the build keeps: those the scenes' lighting needs
    (automatic stripping) or the ones GraphicsSettings keeps (custom)."""
    graphics = unityyaml.meta(project / 'ProjectSettings' / 'GraphicsSettings.asset')['GraphicsSettings']
    if graphics.get('m_LightmapStripping', '0') != '0':
        flags = {
            'm_LightmapKeepPlain': {'LIGHTMAP_ON'},
            'm_LightmapKeepDirCombined': {'LIGHTMAP_ON', 'DIRLIGHTMAP_COMBINED'},
            'm_LightmapKeepDynamicPlain': {'DYNAMICLIGHTMAP_ON'},
            'm_LightmapKeepDynamicDirCombined': {'DYNAMICLIGHTMAP_ON', 'DIRLIGHTMAP_COMBINED'},
            'm_LightmapKeepShadowMask': {'SHADOWS_SHADOWMASK'},
            'm_LightmapKeepSubtractive': {'LIGHTMAP_SHADOW_MIXING'},
        }
        return set().union(*(v for k, v in flags.items() if graphics.get(k, '1') == '1'))
    keep = set()
    for data in lighting_assets(layout, project):
        directional = data.get('m_LightmapsMode', '0') == '1'
        if data.get('m_Lightmaps'):
            keep |= {'LIGHTMAP_ON'} | ({'DIRLIGHTMAP_COMBINED'} if directional else set())
        if (data.get('m_EnlightenSceneMapping') or {}).get('m_Systems'):
            keep |= {'DYNAMICLIGHTMAP_ON'} | ({'DIRLIGHTMAP_COMBINED'} if directional else set())
        for output in data.get('m_LightBakingOutputs') or []:
            mode = (output.get('lightmapBakeMode') or {}).get('mixedLightingMode')
            keep |= {'1': {'SHADOWS_SHADOWMASK'}, '2': {'LIGHTMAP_SHADOW_MIXING'}}.get(mode, set())
    return keep


@cache
def keyword_rank():
    return {k: i for i, k in enumerate(BUILTIN_KEYWORDS)}


def stage_variants(snippet, stage, registry, strip):
    """The variants of one stage of an editor snippet: its built-in keyword combinations (outer)
    times the odometer over its user directives (last fastest), with stripped ones left out."""
    builtin = snippet[f'm_VariantsBuiltin{stage}'] or [[]]
    user = [[None if set(k) == {'_'} else k for k in d] for d in snippet[f'm_VariantsUser{stage}']]
    rank = keyword_rank()
    out = []
    for b in builtin:
        for combo in itertools.product(*user):
            keywords = list(b) + [k for k in combo if k]
            if strip(keywords):
                continue
            builtin_part = sorted((k for k in keywords if k in rank), key=rank.get)
            out.append(builtin_part + registry.order([k for k in keywords if k not in rank]))
    return out


# ---------------------------------------------------------------------------------------------
# One built-in shader


class Builder:
    def __init__(self, layout, project=PROJECT, registry=None):
        self.layout = layout
        self.fog = so.fog_modes(project, [s['path'] for s in layout.scenes])
        self.lightmaps = lightmap_keywords_kept(layout, project)
        self.registry = registry if registry is not None else so.KeywordRegistry()
        self.pool = ThreadPoolExecutor(16)

    def stripper(self, snippet):
        base = snippet['m_BaseRequirements']
        needs = {k['keywordName']: k['requirements'] for k in snippet['m_KeywordTargetInfo']}

        def strip(keywords):
            reqs = base
            for k in keywords:
                reqs |= needs.get(k, 0)
            if reqs & ~PSP2_REQUIREMENTS:
                return True
            if any(k in so.PLATFORM_STRIPPED for k in keywords):
                return True
            if any(k in so.FOG_KEYWORDS and so.FOG_KEYWORDS[k] not in self.fog for k in keywords):
                return True
            return any(k in LIGHTMAP_KEYWORDS and k not in self.lightmaps for k in keywords)

        return strip, needs

    def shader(self, file_id):
        """The release Shader tree of a built-in shader, from the editor's copy."""
        editor = so.editor_builtin_shaders()[file_id]
        form = copy.deepcopy(editor['m_ParsedForm'])
        snippets = dict(editor['m_CompileInfo']['m_Snippets'])
        records = []
        for sub in form['m_SubShaders']:
            for ps in sub['m_Passes']:
                snippet = snippets.get(ps['m_State']['gpuProgramID'])
                if ps['m_Type'] != so.PASS_NORMAL or snippet is None:
                    continue
                self.programs(ps, snippet, records)
        raw = assemble_blob(records)
        compressed = lz4.block.compress(raw, mode='high_compression', compression=9, store_size=False)
        return {
            'm_Name': '',
            'm_ParsedForm': form,
            'platforms': [so.PLATFORM_PSP2],
            'offsets': [0],
            'compressedLengths': [len(compressed)],
            'decompressedLengths': [len(raw)],
            'compressedBlob': compressed,
            'm_Dependencies': [{'m_FileID': 0, 'm_PathID': d['m_PathID']} for d in editor['m_Dependencies']],
            'm_NonModifiableTextures': [],
            'm_ShaderIsBaked': True,
        }

    def programs(self, ps, snippet, records):
        base = snippet['m_BaseRequirements']
        if not snippet['m_PlatformMask'] & PSP2_PLATFORM_BIT or snippet['m_Language'] not in LANGUAGES:
            return
        if base & ~PSP2_REQUIREMENTS or base not in TARGET_OF:
            return
        code = snippet['m_Code']
        found = dict(pragmas(code))
        strip, needs = self.stripper(snippet)

        def requirements(keywords):
            out = base
            for k in keywords:
                out |= needs.get(k, 0)
            return out

        text = '#include "HLSLToPSP2.cginc"\n' + code.replace('#pragma', '//#prag')
        stages = []
        for stage, key, gpu_type, entry in (
            (0, 'progVertex', so.GPU_VERTEX, found.get('vertex', [None])[0]),
            (1, 'progFragment', so.GPU_FRAGMENT, found.get('fragment', [None])[0]),
        ):
            variants = stage_variants(snippet, stage, self.registry, strip)
            # a keyword that needs more than the snippet (SHADOWS_SCREEN: 3.0) raises the variant's target
            jobs = [('vp' if stage == 0 else 'fp', entry, TARGET_OF[requirements(v)], v) for v in variants]
            results = so.compile_jobs(text, BUILTIN_SOURCE_DIR, jobs, self.pool)
            kept = [(v, r[0], r[1]) for v, r in zip(variants, results, strict=True) if r is not None]
            stages.append((gpu_type, key, kept))
        so.fill_programs(ps, stages, records, requirements)


# ---------------------------------------------------------------------------------------------
# The file


def serialize(tree):
    w = EndianBinaryWriter(endian='<')
    write_typetree(tree, convert.release_tree(48), w)
    return w.bytes


def project_trees(build):
    """The Shader trees of the project's Always Included shaders, as globalgamemanagers.assets
    holds them, in GraphicsSettings order."""
    made = getattr(build, 'shader_objects', None)
    if made is None:
        shaders = so.ProjectShaders(build.layout, build.placement)
        made = build.shader_objects = shaders.build_all(so.build_order(build.files))
        shaders.pool.shutdown()
    out = []
    for g in always_included_project():
        where = build.placement.where.get((g, PROJECT_SHADER))
        if where in made:
            tree = copy.deepcopy(made[where])
            for d in tree['m_Dependencies']:
                if d.pop('file', None) not in (None, NAME):
                    raise NotImplementedError('a project shader in unity_builtin_extra depends on another file')
            out.append(tree)
    return out


def objects(build):
    """[(path ID, serialized bytes)] in write order; a path ID can repeat (the last one wins)."""
    builder = Builder(build.layout)
    out = [(fid, serialize(builder.shader(fid))) for fid in builtin_shader_ids(build.layout)]
    out += [(PROJECT_SHADER, serialize(tree)) for tree in project_trees(build)]
    return out


def write_file(written, type_hash):
    """The serialized file. Objects follow each other 8-byte aligned in write order; an object
    written again under the same path ID leaves its earlier bytes in place, unreferenced."""
    live = dict(written)  # the last object written under a path ID is the one the table points to
    order = list(dict.fromkeys(pid for pid, _ in written))
    region, starts = bytearray(), {}
    for pid, data in written:
        region += b'\0' * (-len(region) % 8)
        starts[pid] = len(region)
        region += data
    # write_serialized lays objects out back to back; hand it each live object together with
    # the dead bytes in front of it, then point the table past them.
    objects_, cursor = [], 0
    for pid in sorted(order, key=lambda p: starts[p]):
        lead = (-cursor) % 8
        chunk = bytes(region[cursor + lead : starts[pid] + len(live[pid])])
        objects_.append(ObjectEntry(pid, 0, chunk))
        cursor = starts[pid] + len(live[pid])
    data = bytearray(
        write_serialized(HeaderInfo(target_platform=30), [TypeEntry(48, old_type_hash=type_hash)], objects_, [])
    )
    offset = struct.unpack_from('>I', data, 12)[0]
    for o in objects_:
        dead = len(o.data) - len(live[o.path_id])
        if not dead:
            continue
        start = starts[o.path_id] - dead
        entry = struct.pack('<qII', o.path_id, start, len(o.data))
        at = data.index(entry, 20, offset)
        struct.pack_into('<qII', data, at, o.path_id, start + dead, len(live[o.path_id]))
    return bytes(data)


def build_extra(build):
    """Media/Resources/unity_builtin_extra for build.py."""
    return write_file(objects(build), build.hashes.builtin(48))
