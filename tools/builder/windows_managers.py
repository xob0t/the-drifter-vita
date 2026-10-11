"""globalgamemanagers for the Windows (x64, Mono) player, on top of managers.py.

What a Windows build writes differently from the PS Vita's (builds/drifter-2018.2.19f1 against
builds/drifter-2018.2.19f1-vita):

- ClusterInputManager (slot 15, path ID 16) is there: the Windows player has the manager.
- GraphicsSettings: the Standalone tier settings from the YAML (the PS Vita gets defaults), and
  shader compiler defines for Direct3D 11 (shader platform 4).
- QualitySettings: the levels Standalone does not exclude, from its default level.
- MonoManager: the Windows player's engine modules (its Managed folder, bar UNLISTED_MODULES),
  the four extensions a standalone player takes (UI, Timeline, Networking, SpatialTracking),
  then the project's DLLs.
- BuildSettings: graphics API Direct3D 11 (2), dynamic batching from PlayerSettings'
  Standalone batching, OnMouse events in use. Unity also writes a license auth token, which is
  left empty.

Not reproduced (managers.py's PS Vita output has the same gaps): the order of ScriptMapper's
built-in extra shaders, of BuildSettings.runtimeClassHashes and of RuntimeInitializeOnLoadManager's
classes; the build GUID and license token.

Everything else (PlayerSettings, InputManager, ScriptMapper, ResourceManager, ...) comes out of
the PC project through managers.py as it is.

    objects = managers(layout, placement, entries, project)   # [(path ID, class ID, dict)]
"""

import functools

import managers as base
import unityyaml
from build import WINDOWS_PLAYER

PATH_IDS = {**base.PATH_IDS, 236: 16}  # ClusterInputManager
SETTINGS = {**base.SETTINGS, 236: 'ClusterInputManager.asset'}
BUILD_TARGET = 'Standalone'
DIRECT3D11 = 2  # GraphicsDeviceType
SHADER_PLATFORM_D3D11 = 4
# GraphicsSettings.m_ShaderDefinesPerShaderCompiler for Direct3D 11 at each tier: the builtin
# shader defines bit set of a desktop Windows player (the reference's value).
D3D11_DEFINES = 147179008
# Engine modules of the player's Managed folder that MonoManager does not list (editor support,
# services and modules without runtime classes), as the reference build shows.
UNLISTED_MODULES = {
    'Baselib', 'CloudWebServices', 'Facebook', 'FileSystemHttp', 'HotReload', 'Profiler', 'TLS', 'Timeline', 'Umbra',
}  # fmt: skip
TIER_FIELDS = (
    ('renderingPath', int), ('hdrMode', int), ('realtimeGICPUUsage', int), ('useCascadedShadowMaps', bool),
    ('prefer32BitShadowMaps', bool), ('enableLPPV', bool), ('useHDR', bool),
)  # fmt: skip
WINDOWS_EXTENSIONS = ['UnityEngine.UI.dll', 'UnityEngine.Timeline.dll', 'UnityEngine.Networking.dll',
                      'UnityEngine.SpatialTracking.dll']  # fmt: skip


class Managers(base.Managers):
    def all(self):
        out = []
        for cid, pid in sorted(PATH_IDS.items(), key=lambda x: x[1]):
            build = getattr(self, f'class_{cid}', None)
            out.append((pid, cid, build() if build else self.convert(cid)))
        return out

    def convert(self, class_id):
        import convert

        d, _ = convert.convert(class_id, base.settings(SETTINGS[class_id], self.project), self.ctx())
        return d

    # GraphicsSettings: the YAML's Standalone tier settings (m_TierSettings entries of build target
    # 1, m_Tier 0-2); Direct3D 11 defines.
    def class_30(self):
        d = super().class_30()
        fields = base.settings('GraphicsSettings.asset', self.project)
        for entry in fields.get('m_TierSettings') or []:
            if str(entry.get('m_BuildTarget')) != '1':
                continue
            values = entry['m_Settings']
            tier = d[f'm_TierSettings_Tier{int(entry["m_Tier"]) + 1}']
            for name, kind in TIER_FIELDS:
                if name in values:
                    tier[name] = kind(int(values[name]))
        d['m_ShaderDefinesPerShaderCompiler'] = [
            {
                'shaderPlatform': SHADER_PLATFORM_D3D11,
                'defines_Tier1': [D3D11_DEFINES],
                'defines_Tier2': [D3D11_DEFINES],
                'defines_Tier3': [D3D11_DEFINES],
            }
        ]
        return d

    # QualitySettings: the levels Standalone keeps, starting at its default level.
    def class_47(self):
        fields = base.settings('QualitySettings.asset', self.project)
        d = self.convert(47)
        keep = [
            i
            for i, q in enumerate(fields.get('m_QualitySettings') or [])
            if BUILD_TARGET not in (q.get('excludedTargetPlatforms') or [])
        ]
        d['m_QualitySettings'] = [d['m_QualitySettings'][i] for i in keep]
        for q in d['m_QualitySettings']:
            q['blendWeights'] = q['blendWeights'] or 1
        default = int((fields.get('m_PerPlatformDefaultQuality') or {}).get(BUILD_TARGET, d['m_CurrentQuality']))
        d['m_CurrentQuality'] = max(0, min(default, len(keep) - 1))
        return d

    def assemblies(self):
        modules = sorted(
            (
                p.name
                for p in (WINDOWS_PLAYER / 'Data' / 'Managed').glob('UnityEngine.*Module.dll')
                if p.name[len('UnityEngine.') : -len('Module.dll')] not in UNLISTED_MODULES
            ),
            key=lambda n: n[len('UnityEngine.') : -len('Module.dll')],
        )
        user = sorted({d.path.name for d in base.dlls(self.project) if d.kind == base.USER}, key=str.lower)
        names = ['UnityEngine.dll', *modules, *WINDOWS_EXTENSIONS, *user]
        kinds = [base.ENGINE] * (1 + len(modules)) + [base.EXTENSION] * len(WINDOWS_EXTENSIONS)
        return names, kinds + [base.USER] * len(user)

    # BuildSettings
    def class_141(self):
        d = super().class_141()
        d['m_GraphicsAPIs'] = [DIRECT3D11]
        d['enableDynamicBatching'] = batching(self.project)
        d['usesOnMouseEvents'] = True
        return d

    # ClusterInputManager
    def class_236(self):
        return {'m_Inputs': []}


@functools.cache
def batching(project):
    """PlayerSettings' dynamic batching for Standalone (m_BuildTargetBatching), default on."""
    player = unityyaml.meta(project / 'ProjectSettings' / 'ProjectSettings.asset')['PlayerSettings']
    for entry in player.get('m_BuildTargetBatching') or []:
        if entry.get('m_BuildTarget') == BUILD_TARGET:
            return entry.get('m_DynamicBatching') == '1'
    return True


def managers(layout, placement, entries, project):
    """[(path ID, class ID, release dict)] of the Windows globalgamemanagers, in path ID order."""
    return Managers(layout, placement, entries, project).all()
