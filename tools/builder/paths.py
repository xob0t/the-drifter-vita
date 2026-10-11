"""Paths the Unity-free build reads and writes."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools' / 'downgrade'))
import common  # noqa: E402

TARGET = common.TARGET
# The translated project the pipeline's rules produce (pipeline.py project --platform vita).
PROJECT = common.WORK / f'drifter-{TARGET}-vita'
# Whether the project has the parity probe in (pipeline.py project --diag); its builds go to -diag folders.
DIAG_MARKER = PROJECT / 'ProjectSettings' / 'downgrade-diag.txt'
DIAG = DIAG_MARKER.exists() and DIAG_MARKER.read_text(encoding='utf-8') == 'True'
# The player Unity built from that project (pipeline.py build --platform vita --unity): the reference the
# checks compare against.
REFERENCE = ROOT / 'builds' / (f'drifter-{TARGET}-vita-unity' + ('-diag' if DIAG else ''))
REFERENCE_MEDIA = REFERENCE / 'Media'
# Files taken from Unity's installers (CGIncludes, the PS Vita player and its shader includes, IL2CPP):
# what setup.py extracted from them, or an installed editor's.
EDITOR_DATA = common.editor_data(TARGET)
# The Windows build's player and tools, which only an installed editor has.
INSTALLED_EDITOR_DATA = common.EDITORS / TARGET / 'Editor' / 'Data'
CGINCLUDES = EDITOR_DATA / 'CGIncludes'
PSP2_PLAYER = EDITOR_DATA / 'PlaybackEngines' / 'PSP2Player'
SDK = Path(common.setting('SCE_PSP2_SDK_DIR') or r'C:\vitasdk\sdk')
PSP2CGC = SDK / 'host_tools' / 'bin' / 'psp2cgc.exe'
# Scratch space and outputs.
WORK = common.WORK / 'builder'
# The pipeline's build folder (pipeline.py build --platform vita).
OUT = ROOT / 'builds' / (f'drifter-{TARGET}-vita' + ('-diag' if DIAG else ''))

# Unity's built-in shader source (Sprites/Default, Unlit/Transparent and the engine's shaders the
# build compiles), from Unity's download server, pinned by hash.
BUILTIN_SHADERS_URL = 'https://download.unity3d.com/download_unity/06990f28ba00/builtin_shaders-2018.2.19f1.zip'
BUILTIN_SHADERS_SHA256 = 'cf29363ba6832f1e420defa4db3493d78c2f6f1aceacfa0817e7cd8fedb40d00'
BUILTIN_SHADERS = WORK / 'builtin_shaders'


def builtin_shaders():
    """The unpacked built-in shader source, fetched and checked on first use."""
    if not (BUILTIN_SHADERS / 'DefaultResourcesExtra').exists():
        import zipfile

        archive = common.download(
            BUILTIN_SHADERS_URL,
            WORK / 'builtin_shaders_zip' / BUILTIN_SHADERS_URL.rsplit('/', 1)[1],
            BUILTIN_SHADERS_SHA256,
        )
        with zipfile.ZipFile(archive) as z:
            z.extractall(BUILTIN_SHADERS)
    return BUILTIN_SHADERS
