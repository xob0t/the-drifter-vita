"""Resources/unity_builtin_extra for the Windows player: builtin_extra.py's selection and layout
with Direct3D 11 programs.

The built-in shaders compile from the snippets the editor's unity_builtin_extra stores (surface
shaders arrive generated, fixed-function passes converted), as on the PS Vita, but for
Direct3D 11: snippets whose platform mask has d3d11 (bit 4) compile, at any shader model; the
Windows player's variant stripping applies (windows_shaders.STRIPPED, fog and lightmap modes no
scene uses); the header says platform 19.

    data = build_extra(build)        # bytes of TheDrifter_Data/Resources/unity_builtin_extra
"""

import copy
import itertools
import struct

import builtin_extra as be
import dx11
import shader_object as so
import windows_shaders as ws

D3D11_PLATFORM_BIT = 1 << dx11.PLATFORM_D3D11
WINDOWS_PLATFORM = 19


RANK = {k: i for i, k in enumerate(ws.BUILTIN_KEYWORDS)}


def stage_variants(snippet, stage, registry, strip):
    """The variants of one stage of an editor snippet: the odometer over its user directives
    (outer, last fastest) times its built-in keyword combinations (inner), with stripped ones
    left out; built-in keywords first, in the Windows player's table order. (builtin_extra.py
    runs the two loops the other way round.)"""
    builtin = snippet[f'm_VariantsBuiltin{stage}'] or [[]]
    user = [[None if set(k) == {'_'} else k for k in d] for d in snippet[f'm_VariantsUser{stage}']]
    out = []
    for combo in itertools.product(*user):
        for b in builtin:
            keywords = list(b) + [k for k in combo if k]
            if strip(keywords):
                continue
            builtin_part = sorted((k for k in keywords if k in RANK), key=RANK.get)
            out.append(builtin_part + registry.order([k for k in keywords if k not in RANK]))
    return out


class Builder(be.Builder):
    def __init__(self, layout, project, registry=None, pool=None):
        registry = registry if registry is not None else so.KeywordRegistry()
        for k in ws.BUILTIN_KEYWORDS:
            registry.index.setdefault(k, len(registry.index))
        super().__init__(layout, project, registry, pool)
        self.project = project

    def stripper(self, snippet):
        needs = {k['keywordName']: k['requirements'] for k in snippet['m_KeywordTargetInfo']}

        def strip(keywords):
            if any(k in ws.STRIPPED for k in keywords):
                return True
            if any(k in so.FOG_KEYWORDS and so.FOG_KEYWORDS[k] not in self.fog for k in keywords):
                return True
            return any(k in be.LIGHTMAP_KEYWORDS and k not in self.lightmaps for k in keywords)

        return strip, needs

    def shader(self, file_id):
        tree = super().shader(file_id)
        tree['platforms'] = [dx11.PLATFORM_D3D11]
        return tree

    def programs(self, ps, snippet, records):
        """Fills one pass. `records` is the list builtin_extra.Builder.shader assembles; identical
        records are shared, as in windows_shaders."""
        if not snippet['m_PlatformMask'] & D3D11_PLATFORM_BIT or snippet['m_Language'] not in be.LANGUAGES:
            return
        base = snippet['m_BaseRequirements']
        strip, needs = self.stripper(snippet)

        def requirements(keywords):
            out = base
            for k in keywords:
                out |= needs.get(k, 0)
            return out

        program = dx11.from_snippet(snippet['m_Code'])
        stages = []
        for stage, key, kind, entry in (
            (0, 'progVertex', 'vp', program.vertex),
            (1, 'progFragment', 'fp', program.fragment),
        ):
            variants = stage_variants(snippet, stage, self.registry, strip)
            results = list(
                self.pool.map(
                    lambda v, kind=kind, entry=entry: ws.compile_one(
                        program.text, program.shader_dir, self.project, kind, entry, program.target, v
                    ),
                    variants,
                )
            )
            stages.append((key, [(v, r) for v, r in zip(variants, results, strict=True) if r is not None]))
        index = {r: i for i, r in enumerate(records)}
        ws.fill_programs(ps, stages, index, requirements)
        records[:] = list(index)


def project_trees(build):
    """The project's Always Included shaders as globalgamemanagers.assets holds them."""
    made = getattr(build, 'shader_objects', None)
    if made is None:
        shaders = ws.WindowsShaders(build.layout, build.placement, build.root)
        made = build.shader_objects = shaders.build_all(so.build_order(build.files))
        shaders.pool.shutdown()
    out = []
    for g in be.always_included_project(build.root):
        where = build.placement.where.get((g, be.PROJECT_SHADER))
        if where in made:
            tree = copy.deepcopy(made[where])
            for d in tree['m_Dependencies']:
                if d.pop('file', None) not in (None, be.NAME):
                    raise NotImplementedError('a project shader in unity_builtin_extra depends on another file')
            out.append(tree)
    return out


def build_extra(build):
    """TheDrifter_Data/Resources/unity_builtin_extra for build.py --platform windows."""
    builder = Builder(build.layout, build.root)
    written = [(fid, be.serialize(builder.shader(fid))) for fid in be.builtin_shader_ids(build.layout, build.root)]
    written += [(be.PROJECT_SHADER, be.serialize(tree)) for tree in project_trees(build)]
    builder.pool.shutdown()
    data = bytearray(be.write_file(written, build.hashes.builtin(48)))
    # builtin_extra.write_file writes the PS Vita's platform into the header (after the version)
    version = b'2018.2.19f1\0'
    at = data.index(version) + len(version)
    assert struct.unpack_from('<i', data, at)[0] == 30
    struct.pack_into('<i', data, at, WINDOWS_PLATFORM)
    return bytes(data)
