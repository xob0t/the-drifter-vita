"""Run psp2cgc the way Unity 2018.2's PS Vita CgBatch plugin does.

With these flags and shaderlab.unity_input()'s text, psp2cgc's output matches the reference
build's GXP programs byte for byte, except the source GUID at offset 16: a hash of the input
file's path, which differs between any two machines anyway.
"""

import subprocess
import uuid

from paths import CGINCLUDES, PROJECT, PSP2_PLAYER, PSP2CGC, WORK

TMP = WORK / 'cgc'

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
# The shader requirement bits Unity records for each target (m_ShaderRequirements).
REQUIREMENTS = {'2.0': 1, '2.5': 33, '3.0': 227}
PLATFORM = [
    'UNITY_NO_SCREENSPACE_SHADOWS=1',
    'UNITY_PBS_USE_BRDF3=1',
    'SHADER_API_MOBILE=1',
    'UNITY_COLORSPACE_GAMMA=1',
    'UNITY_LIGHTMAP_RGBM_ENCODING=1',
]
AVAILABLE = [
    'SHADER_TARGET_AVAILABLE=30',
    'SHADER_AVAILABLE_INTERPOLATORS10=1',
    'SHADER_AVAILABLE_DERIVATIVES=1',
    'SHADER_AVAILABLE_SAMPLELOD=1',
    'SHADER_AVAILABLE_FRAGCOORD=1',
    'SHADER_API_PSP2=1',
]


def compile(text, shader_dir, stage, entry, target, keywords):
    """((gxp, the SDB's XML text), log), or (None, log) when it fails: the SDB holds the declared
    types and semantics the GXP leaves out."""
    vertex = stage == 'vp'
    defines = (
        TARGET[target]
        + PLATFORM
        + [k + '=1' for k in keywords]
        + ['UNITY_VERSION=201839', 'SHADER_STAGE_VERTEX=1' if vertex else 'SHADER_STAGE_FRAGMENT=1']
        + AVAILABLE
    )
    TMP.mkdir(parents=True, exist_ok=True)
    src = TMP / f'GXM_{"VP" if vertex else "FP"}_{uuid.uuid4().hex[:16]}'
    out = src.with_suffix('.gxp')
    sdb_file = src.with_suffix('.sdb')
    src.write_bytes(text.encode())
    # -nofastint is missing from the command line Unity logs, yet the reference build's programs
    # were compiled with it: without it, vertex programs that convert floats to ints differ.
    command = [str(PSP2CGC), str(src), '-O3', '-W3', '-fastmath', '-pedantic', '-fx', '-nofastint']
    command += ['-D' + d for d in defines]
    command += [
        f'-I{shader_dir}',
        f'-I{PROJECT}',
        f'-I{CGINCLUDES}',
        f'-I{PSP2_PLAYER / "CgBatchPlugins64" / "include"}',
    ]
    command += ['-profile', 'sce_vp_psp2' if vertex else 'sce_fp_psp2', '-entry', entry, '-o', str(out)]
    command += ['-xmlsdb', str(sdb_file)]
    result = subprocess.run(command, capture_output=True, text=True, cwd=PROJECT)
    data = out.read_bytes() if result.returncode == 0 and out.exists() else None
    if data is not None:
        data = (data, sdb_file.read_text(encoding='utf-8'))
    for path in (src, out, sdb_file):
        path.unlink(missing_ok=True)
    return data, result.stdout + result.stderr
