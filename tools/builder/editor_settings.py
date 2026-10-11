"""The project settings as the editor holds them when it builds the PS Vita player.

The project's ProjectSettings folder is the AssetRipper export of a Unity 2020 project. When Unity
2018.2 first opens it, it adds the PlayerSettings fields of its own version the export lacks, with
their defaults, and drops a shader from GraphicsSettings' Always Included Shaders; the pipeline's
build script (tools/downgrade/unity/DowngradeBuild.cs) then sets the PS Vita title.
player_settings() and graphics_settings_text() do the same, so a project the editor never opened
builds like one it saved.
"""

import unityyaml
from paths import PROJECT

BUILTIN_EXTRA = '0000000000000000f000000000000000'
# The editor takes Hidden/VideoDecodeOSX out of Always Included Shaders when it loads the settings the
# Unity 2020 export wrote (they list it after Hidden/VideoDecode); the cause is not known.
EDITOR_DROPS = {16001}

# Unity 2018.2.19f1's values for PlayerSettings fields the export lacks, as the editor saves them:
# the editor-only PS Vita fields the build reads, and the player's fields whose default is not zero.
EDITOR_DEFAULTS = {
    'n3dsEnableSharedListOpt': '1',
    'psp2AcquireBGM': '1',
    'vrSettings': {
        'cardboard': {'depthFormat': '0', 'enableTransitionView': '0'},
        'daydream': {
            'depthFormat': '0',
            'useSustainedPerformanceMode': '0',
            'enableVideoLayer': '0',
            'useProtectedVideoMemory': '0',
            'minimumSupportedHeadTracking': '0',
            'maximumSupportedHeadTracking': '1',
        },
        'hololens': {'depthFormat': '1', 'depthBufferSharingEnabled': '0'},
        'oculus': {'sharedDepthBuffer': '0', 'dashSupport': '0'},
    },
    'psp2NPAgeRating': '12',
    'psp2NPCommsID': '',
    'psp2NPCommsPassphrase': '',
    'psp2NPCommsSig': '',
    'psp2NPTrophyPackPath': '',
    'psp2MemoryExpansionMode': '0',
    'psp2SaveDataQuota': '10240',
    'psp2ParentalLevel': '1',
    'psp2ShortTitle': 'Not Set',
    'psp2ContentID': 'IV0000-ABCD12345_00-0123456789ABCDEF',
    'psp2Category': '0',
    'psp2MasterVersion': '01.00',
    'psp2AppVersion': '01.00',
    'psp2EnterButtonAssignment': '2',
}

# What DowngradeBuild.cs sets before a PS Vita build (the IL2CPP arguments it adds are il2cpp.py's).
BUILD_SCRIPT = {
    'psp2ContentID': 'IV0000-DRFT00001_00-THEDRIFTERVITA00',
    'psp2ShortTitle': 'The Drifter',
    'psp2MemoryExpansionMode': '3',  # ExpandBy109MB
}


def with_defaults(fields, defaults):
    """fields with every key of defaults it lacks, nested mappings included."""
    out = dict(fields)
    for key, value in defaults.items():
        if key not in out:
            out[key] = value
        elif isinstance(value, dict) and isinstance(out[key], dict):
            out[key] = with_defaults(out[key], value)
    return out


def graphics_settings_text(project=PROJECT):
    """GraphicsSettings.asset as the editor holds it: without the Always Included Shaders it drops."""
    text = (project / 'ProjectSettings' / 'GraphicsSettings.asset').read_text(encoding='utf-8')
    head, rest = text.split('m_AlwaysIncludedShaders:', 1)
    block, tail = rest.split('m_PreloadedShaders', 1)
    dropped = {f'  - {{fileID: {fid}, guid: {BUILTIN_EXTRA}, type: 0}}' for fid in EDITOR_DROPS}
    block = ''.join(line for line in block.splitlines(keepends=True) if line.rstrip('\r\n') not in dropped)
    return head + 'm_AlwaysIncludedShaders:' + block + 'm_PreloadedShaders' + tail


def graphics_settings(project=PROJECT):
    """The GraphicsSettings fields as the editor holds them."""
    return unityyaml.documents(graphics_settings_text(project))[0].fields


def player_settings(project=PROJECT):
    """The PlayerSettings fields of ProjectSettings.asset as the editor builds with them."""
    (doc,) = unityyaml.load(project / 'ProjectSettings' / 'ProjectSettings.asset')
    return {**with_defaults(doc.fields, EDITOR_DEFAULTS), **BUILD_SCRIPT}
