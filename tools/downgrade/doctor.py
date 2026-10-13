"""Check what the pipeline needs for a target and print a checklist with the command that fixes each gap.

    uv run tools/setup.py doctor [--target windows|il2cpp|vita] [--license]

Exits 1 if something required is missing. pipeline.py runs the fast checks of the stage it is about
to run (preflight), so a missing tool stops it in seconds instead of an hour into a build.
--license also starts the editor once in batch mode (about a minute) to see that it runs.

tools/setup.py installs what these checks look for; the paths, versions and download pins are here.
"""

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

import progress
from common import (
    ASSETRIPPER,
    CECIL,
    CSC,
    EDITORS,
    EXTERNAL,
    ROOT,
    SOURCE_VERSION,
    STEAM,
    STEAM_GAME,
    TARGET,
    WORK,
    default_game,
    editor_data,
    env,
    file_hash,
)
from rich.text import Text

SETUP = 'uv run tools/setup.py'

# Unity 2018.2.19f1's installers: Unity's CDN, except the PS Vita module, which Unity no longer serves (the
# archive.org copy is Unity's own signed installer; setup.py checks the hash and the signature).
UNITY_CDN = 'https://download.unity3d.com/download_unity/06990f28ba00'
INSTALLERS = {
    'editor': (
        f'{UNITY_CDN}/Windows64EditorInstaller/UnitySetup64-2018.2.19f1.exe',
        '4a2e95a7bd7b04cc95d3ce110e17775ba5d810b59c2c925ad6e34da4147d3f5e',
    ),
    'il2cpp': (
        f'{UNITY_CDN}/TargetSupportInstaller/UnitySetup-Windows-IL2CPP-Support-for-Editor-2018.2.19f1.exe',
        '56d829305af445d0d91a1b52790fe8a59e02020c73771c5162fee94c351c9d24',
    ),
    'vita': (
        'https://archive.org/download/unitypsvitasupport/'
        'UnitySetup-Playstation-Vita-Support-for-Editor-2018.2.19f1.exe',
        'e68ad5a92622c34abb0c535a6de166531a1e8a192944fd1a50fe8297f7427901',
    ),
}
# What each installer leaves in the editor folder, and what it is called.
EDITOR_FILES = {
    'editor': 'Editor/Unity.exe',
    'il2cpp': 'Editor/Data/PlaybackEngines/windowsstandalonesupport/Variations/win64_nondevelopment_il2cpp',
    'vita': 'Editor/Data/PlaybackEngines/PSP2Player/PSP2Player_Il2cpp.self',
}
MODULE_NAMES = {'il2cpp': 'Windows IL2CPP module', 'vita': 'PS Vita module'}

# What the pipeline and the editor-free PS Vita build read from Unity's installers, which setup.py
# extracts with 7-Zip instead of installing them: paths inside each installer (NSIS archives). The PS
# Vita module's folder becomes Editor/Data/PlaybackEngines/PSP2Player.
UNITY_FILES = {
    'editor': [
        r'Editor\Data\CGIncludes',
        r'Editor\Data\Managed',
        r'Editor\Data\MonoBleedingEdge\lib\mono\unityaot',
        r'Editor\Data\MonoBleedingEdge\lib\mono\unityjit',
        r'Editor\Data\MonoBleedingEdge\etc',
        r'Editor\Data\il2cpp',
        r'Editor\Data\Tools\MapFileParser',
        r'Editor\Data\Resources\unity_builtin_extra',
        r'Editor\Data\UnityExtensions\Unity',
    ],
    'vita': ['$INSTDIR$_59_'],
}
UNITY_FILES_VITA_FOLDER = '$INSTDIR$_59_'
# A file of each part, under Editor/Data, to check the extraction by.
UNITY_FILES_CHECKED = [
    'CGIncludes/UnityCG.cginc',
    'Managed/UnityEngine/UnityEngine.CoreModule.dll',
    'MonoBleedingEdge/lib/mono/unityaot/mscorlib.dll',
    'MonoBleedingEdge/lib/mono/unityjit/mscorlib.dll',
    'il2cpp/build/il2cpp.exe',
    'Tools/MapFileParser/MapFileParser.exe',
    'Resources/unity_builtin_extra',
    'UnityExtensions/Unity/GUISystem/UnityEngine.UI.dll',
    'PlaybackEngines/PSP2Player/PSP2Player_Il2cpp.self',
]
# 7-Zip reads NSIS installers; where none is installed, setup.py unpacks its package with 7zr.
SEVEN_ZIP = {
    '7zr': (
        'https://github.com/ip7z/7zip/releases/download/26.04/7zr.exe',
        '256feca8e274e5da655e2a284fabafd9f554365eb164862089dacd4e8276d282',
    ),
    'package': (
        'https://github.com/ip7z/7zip/releases/download/26.04/7z2604-x64.exe',
        'd54bf805f9f3704d1e8db2fa3498ae7ef2df0312b40b558e7c71c734430a665d',
    ),
}


def module_name(module, version=TARGET):
    return f'Unity {version} editor' if module == 'editor' else MODULE_NAMES[module]


ASSETRIPPER_ZIP = (
    'https://github.com/AssetRipper/AssetRipper/releases/download/2.0.0/AssetRipper_win_x64.zip',
    '9a7ef0e7c5c3ea5b90b4e6d855e2d98d5f7ec8c3f9e26fccbc194c6a7b01baf7',
)

GAME_DIR = STEAM_GAME.name
GAME_EXE = 'TheDrifter.exe'
# The game's GOG product ID, and the folder GOG installs it in by default.
GOG_ID = '1629486561'
GOG_GAME = Path(r'C:\GOG Games\The Drifter')
# The game code the rules are written for, by the SHA-256 of TheDrifter_Data/Managed/Assembly-CSharp.dll:
# the current Steam release, and GOG's 1.1.5 (the same code without Steam's restart check).
GAME_CODE = {
    'fc939732d5d9c86a187352a43f60cbc1fefe17bc79ea7d277c9de8e6c51ae334': 'Steam',
    '87860bbb5b316d6ee67a5106baa12f3536002e4ef56d7e1e24926bb589407d91': 'GOG 1.1.5',
}

# Where setup.py extracts an SDK archive by default; the SDK is its sdk folder.
VITA_SDK_FOLDER = Path(r'C:\vitasdk')
# The PS Vita SDK files Unity's PS Vita module checks before a build: under SCE_PSP2_SDK_DIR ...
SDK_TOOLS = (
    'host_tools/build/bin/psp2snc.exe',
    'host_tools/build/bin/psp2ld.exe',
    'host_tools/build/bin/arm-eabi-as.exe',
    'host_tools/build/bin/psp2snarl.exe',
    'host_tools/build/bin/psp2bin.exe',
    'host_tools/bin/psp2psarc.exe',
)
SDK_VERSION_FILE = 'target/include/sdk_version.h'
MIN_SDK = (3, 570)
# ... and under SCE_ROOT_DIR, from the SDK's Target Manager Server and Publishing Tools installers.
SCE_TOOLS = (
    'PSP2/Tools/Target Manager Server/bin/psp2ctrl.exe',
    'PSP2/Tools/Target Manager Server/bin/psp2run.exe',
    'PSP2/Tools/Publishing Tools/bin/psp2pubcmd.exe',
)
# The SDK's installers of those tools, and what each installs.
SCE_INSTALLERS = {'TMServer*.msi': SCE_TOOLS[:2], 'PublishingTools*.msi': SCE_TOOLS[2:]}
# What setup.py downloads when the machine has no SDK: SDK 3.570 and the two installers from archive.org's
# copy of the DevNet release, pinned to the SHA-1 in the item's metadata (archive.org lists no SHA-256).
VITA_SDK_ITEM = 'https://archive.org/download/psvita-sdk-3.570-devnet-full/SDK%20Software'
VITA_SDK_ZIP = (f'{VITA_SDK_ITEM}/PSVITA.zip', 'fbb3a9dfa24bc886eff02805e3005abdec755cb8')
VITA_SDK_DOWNLOADED = VITA_SDK_FOLDER / 'PSVITA' / 'sdk'  # where VITA_SDK_ZIP's SDK lands
# That SDK is 3.570.011. Sony's patches bring it to the 3.570.071 this pipeline was built with, applied over
# it in this order: the shader compiler psp2cgc (3.570.021), then libc (3.570.071).
VITA_SDK_PATCHED = (3, 570, 71)
_PATCHES = f'{VITA_SDK_ITEM}/Tools/Plugins%20-%20Addons/SDK%20Patches/%5BSDK%5D%20SDK%203.570%20Patch%20-%20'
VITA_SDK_PATCHES = (
    (f'{_PATCHES}psp2cgc-3.570.021/SDK-3_570_021-patch.zip', '5ecc5d6cefbeaa527a139a331d65d048d2238185'),
    (f'{_PATCHES}libc-3.570.071/SDK-3_570_071-P03460462-patch.zip', 'bd76d99979ba2d4881b533ca20c48a12edc01a2d'),
)
SCE_INSTALLER_DOWNLOADS = {
    'TMServer*.msi': (
        f'{VITA_SDK_ITEM}/Tools/Installers/Target%20Manager%20Server/TMServer-3_60_0_2.msi',
        '0ad5c3dd4efaeb9efc43675a3f94fc93e192abc6',
    ),
    'PublishingTools*.msi': (
        f'{VITA_SDK_ITEM}/Tools/Installers/Publishing%20Tools/PublishingTools-2_31_0_1669.msi',
        'ba551ea6ef78e8635e0f7b03b8b383e93b2157fd',
    ),
}

# Free space a build needs, in GB, from the sizes of this pipeline's outputs: the AssetRipper export
# (work/rip-*, 0.8 GB), the project with Unity's import cache (Windows 3.6 GB, Vita 5.3 GB), the
# player (Windows 0.8 GB, IL2CPP 1.4 GB, Vita 1.3 GB and a VPK of about half that), the tools the
# pipeline fetches (1 GB), the cache server's copy of the imports (work/unity-cache, up to the size
# of the project's Library: 2.9 GB, Vita 4.6 GB) and Unity's temporary files during the build.
# A rebuild only rewrites the player and the files that changed.
FIRST_BUILD_GB = {'windows': 10, 'il2cpp': 12, 'vita': 15}
REBUILD_GB = {'windows': 3, 'il2cpp': 4, 'vita': 5}
# The editor and its modules (3.9 GB with both, installers included while they run).
EDITOR_GB = 5
# The two installers and what setup.py extracts from them (about 1.2 GB).
UNITY_FILES_GB = 3
# A first Vita build's minutes without Node.js and with it (the cache server and parallel audio import).
FIRST_VITA_BUILD_MINUTES = (100, 40)
LICENSE_CHECK = 'Unity license (batch mode)'
LICENSE_CHECK_SECONDS = 900  # how long the editor gets to start and quit in the license check


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ''
    fix: str = ''
    required: bool = True


# ---------------------------------------------------------------- pure helpers


def parse_sdk_version(text):
    """(3, 570, 71) from sdk_version.h's '#define SCE_PSP2_SDK_VERSION 0x03570071', or None."""
    match = re.search(r'#define\s+SCE_PSP2_SDK_VERSION\s+0x([0-9A-Fa-f]{8})\b', text)
    if not match:
        return None
    digits = match.group(1)
    return int(digits[0:2]), int(digits[2:5]), int(digits[5:8])


def format_sdk_version(version):
    return f'{version[0]}.{version[1]:03d}.{version[2]:03d}'


def parse_library_folders(text):
    """The library paths in Steam's steamapps/libraryfolders.vdf (both the old and the new format)."""
    paths = [m.group(1) for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text)]
    if not paths:  # before 2021: "1" "D:\\SteamLibrary"
        paths = [m.group(1) for m in re.finditer(r'^\s*"\d+"\s+"((?:[^"\\]|\\.)*)"\s*$', text, re.M)]
    return [p.replace('\\\\', '\\') for p in paths]


def game_candidates(steam_roots, read_text):
    """Where The Drifter can be: every Steam library of every Steam install, in order, without repeats.
    read_text(path) returns a file's text or None."""
    found = []
    for root in steam_roots:
        libraries = [root]
        text = read_text(Path(root) / 'steamapps' / 'libraryfolders.vdf')
        if text:
            libraries += parse_library_folders(text)
        for library in libraries:
            candidate = Path(library) / 'steamapps' / 'common' / GAME_DIR
            if all(os.path.normcase(str(candidate)) != os.path.normcase(str(f)) for f in found):
                found.append(candidate)
    return found


def unity_version(header: bytes):
    """The Unity version in the header of a serialized file such as globalgamemanagers."""
    match = re.search(rb'(\d{4}\.\d+\.\d+[abfp]\d+)\0', header[:512])
    return match.group(1).decode() if match else None


def signer_is_unity(status, subject):
    """Get-AuthenticodeSignature's Status and SignerCertificate.Subject of a Unity installer."""
    return (
        status == 'Valid' and re.search(r'(^|,\s*)O=Unity Technologies( ApS| SF)?\s*(,|$)', subject, re.I) is not None
    )


def license_log_problem(text, returncode, platform):
    """Why a short batch-mode run of the editor failed, or None if it worked."""
    for pattern in (
        'No valid Unity Editor license',
        'Failed to activate/update license',
        'License is not active',
        'not licensed',
        'Aborting batchmode due to failure',
    ):
        if pattern.lower() in text.lower():
            line = next(candidate for candidate in text.splitlines() if pattern.lower() in candidate.lower())
            return line.strip()[:200]
    if returncode != 0:
        return f'the editor exited with code {returncode}'
    if platform == 'vita' and not re.search(r'Register platform support module: .*PSP2Player', text):
        return 'the editor did not load its PS Vita module'
    return None


def space_needed(target, project_exists):
    return (REBUILD_GB if project_exists else FIRST_BUILD_GB)[target]


def missing(root: Path, relatives):
    return [r for r in relatives if not (root / r).exists()]


def find_sdk_root(folder: Path, depth=4):
    """The folder holding host_tools/ and target/ (what SCE_PSP2_SDK_DIR names) in or below folder."""
    folder = Path(folder)
    if (folder / SDK_TOOLS[0]).exists():
        return folder
    if depth == 0 or not folder.is_dir():
        return None
    try:
        children = sorted(p for p in folder.iterdir() if p.is_dir())
    except OSError:
        return None
    for child in children:
        found = find_sdk_root(child, depth - 1)
        if found:
            return found
    return None


def summarize(checks):
    """The checklist as text lines, and the number of required items that failed."""
    lines, failed = [], 0
    width = max((len(c.name) for c in checks), default=0)
    for check in checks:
        status = 'ok' if check.ok else ('MISSING' if check.required else 'optional')
        failed += not check.ok and check.required
        lines.append(f'  [{status:^8}] {check.name:<{width}}  {check.detail}'.rstrip())
        if not check.ok and check.fix:
            lines.append(f'  {"":10} {"":<{width}}  fix: {check.fix}')
    return lines, failed


STATUS_STYLES = {'ok': 'green', 'MISSING': 'bold red', 'optional': 'yellow'}


def show(console, *lines):
    """Print lines, with the status of summarize()'s in color."""
    for line in lines:
        text = Text(line)
        style = STATUS_STYLES.get(line[3:11].strip()) if line.startswith('  [') else None
        if style:
            text.stylize(style, 2, 12)
        console.print(text)


# ---------------------------------------------------------------- the machine


def adopt_env(names):
    """Take variables set for new shells into this process, so the tools it starts see them."""
    for name in names:
        value, stale = env(name)
        if stale:
            os.environ[name] = value


def steam_roots():
    import winreg

    roots = [str(STEAM)]
    for key, path, name in (
        (winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Valve\Steam', 'InstallPath'),
    ):
        try:
            with winreg.OpenKey(key, path) as handle:
                roots.append(str(Path(winreg.QueryValueEx(handle, name)[0])))
        except OSError:
            pass
    return roots


def read_text(path: Path):
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None


def steam_game():
    """The Drifter's folder in the first Steam library that has it, or None."""
    return next((c for c in game_candidates(steam_roots(), read_text) if (c / GAME_EXE).exists()), None)


def gog_game():
    """The Drifter's GOG install, from the key GOG's installer writes, else GOG's default folder; or None.
    Its data is the Steam release's: only Steam's restart check is missing from its code."""
    import winreg

    folders = []
    for path in (rf'SOFTWARE\WOW6432Node\GOG.com\Games\{GOG_ID}', rf'SOFTWARE\GOG.com\Games\{GOG_ID}'):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as handle:
                folders.append(Path(winreg.QueryValueEx(handle, 'path')[0]))
        except OSError:
            pass
    return next((f for f in [*folders, GOG_GAME] if (f / GAME_EXE).exists()), None)


def find_game():
    """The Drifter's folder in a Steam library, else its GOG install, or None."""
    return steam_game() or gog_game()


def sdk_version(sdk: Path):
    text = read_text(sdk / SDK_VERSION_FILE)
    return parse_sdk_version(text) if text else None


def sce_root_candidates():
    value, _ = env('SCE_ROOT_DIR')
    candidates = [Path(value)] if value else []
    candidates += [
        Path(os.environ[variable]) / 'SCE'
        for variable in ('ProgramFiles(x86)', 'ProgramFiles')
        if os.environ.get(variable)
    ]
    return candidates


def sce_root():
    """The first SCE folder with the Target Manager tools, or None."""
    return next((c for c in sce_root_candidates() if not missing(c, SCE_TOOLS)), None)


def sdk_status(sdk: Path):
    """(what is wrong with the SDK in a folder or None, its version as shown)."""
    gaps = missing(sdk, SDK_TOOLS)
    version = sdk_version(sdk)
    shown = format_sdk_version(version) if version else f'unknown, no {SDK_VERSION_FILE}'
    if gaps:
        return f'{sdk} lacks ' + ', '.join(Path(g).name for g in gaps), shown
    if version and version[:2] < MIN_SDK:
        return f'{sdk}: version {shown}; Unity needs {MIN_SDK[0]}.{MIN_SDK[1]} or later', shown
    return None, shown


def sdk_candidates():
    value, _ = env('SCE_PSP2_SDK_DIR')
    candidates = [Path(value)] if value else []
    candidates += [VITA_SDK_FOLDER / 'sdk', VITA_SDK_DOWNLOADED]
    for root in sce_root_candidates():
        candidates += sorted((root / 'PSP2 SDKs').glob('*'), reverse=True)
    return candidates


def dotnet35():
    """Whether .NET Framework 3.5 is on. Publishing Tools' installer needs it; a new Windows has it off."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v3.5') as key:
            return winreg.QueryValueEx(key, 'Install')[0] == 1
    except OSError:
        return False


def visual_cpp():
    """The installation path of a Visual Studio (or Build Tools) with the C++ x64 tools, or None."""
    vswhere = (
        Path(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)'))
        / 'Microsoft Visual Studio'
        / 'Installer'
        / 'vswhere.exe'
    )
    if not vswhere.exists():
        return None
    result = subprocess.run(
        [
            str(vswhere),
            '-products',
            '*',
            '-latest',
            '-requires',
            'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
            '-property',
            'installationPath',
        ],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None


def free_gb(path: Path):
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free / 2**30


# ---------------------------------------------------------------- checks


def check_python():
    for module in ('numpy', 'PIL', 'UnityPy', 'elftools'):
        if importlib.util.find_spec(module) is None:
            return Check('Python packages', False, f'No module named {module!r}', 'uv sync')
    return Check('Python packages', True, 'numpy, Pillow, UnityPy, pyelftools')


def check_csc():
    return Check(
        '.NET Framework C# compiler',
        CSC.exists(),
        str(CSC),
        'part of Windows (.NET Framework 4.8); install it from Windows features',
    )


def check_game(game: Path):
    name = 'The Drifter'
    fix = f'{SETUP} --game <folder with {GAME_EXE}>'
    if not (game / GAME_EXE).exists():
        found = find_game()
        hint = f'; found in {found}' if found and found != game else ''
        return Check(name, False, f'no {GAME_EXE} in {game}{hint}', fix)
    data = game / 'TheDrifter_Data'
    try:
        with (data / 'globalgamemanagers').open('rb') as handle:
            version = unity_version(handle.read(512))
    except OSError:
        version = None
    if version != SOURCE_VERSION:
        return Check(name, False, f'{game}: built with Unity {version or "?"}, the rules expect {SOURCE_VERSION}', fix)
    code = data / 'Managed' / 'Assembly-CSharp.dll'
    if not code.exists():
        return Check(name, False, f'{game}: no TheDrifter_Data/Managed/Assembly-CSharp.dll', fix)
    release = GAME_CODE.get(file_hash(code))
    if release is None:
        return Check(
            name,
            False,
            f"{game}: another release of the game (its Assembly-CSharp.dll is neither Steam's nor GOG's "
            'current one); the rules are written for those',
            'update The Drifter in Steam or GOG, or have Steam verify its files',
        )
    return Check(name, True, f'{game} ({release}, Unity {version})')


def check_assetripper():
    return Check('AssetRipper 2.0.0', ASSETRIPPER.exists(), str(ASSETRIPPER.parent), SETUP)


def check_fetched():
    """Tools the pipeline downloads on its first run; listed, never a failure."""
    pending = [
        name
        for name, path in (
            ('Mono.Cecil', CECIL),
            ('.NET 8 SDK', EXTERNAL / 'dotnet' / 'dotnet.exe'),
            ('USCSandbox', EXTERNAL / 'USCSandbox'),
        )
        if not path.exists()
    ]
    return Check(
        'Downloaded by the pipeline',
        not pending,
        'fetched on the first run: ' + ', '.join(pending) if pending else 'Mono.Cecil, .NET 8 SDK, USCSandbox',
        required=False,
    )


def check_editor(version, module='editor'):
    root = EDITORS / version
    flag = '' if module == 'editor' else f' --{module}'
    fix = (
        f'{SETUP}{flag}'
        if version == TARGET
        else f'install Unity {version}{"" if module == "editor" else " and the module"} in {root}'
    )
    path = root / EDITOR_FILES[module]
    return Check(module_name(module, version), path.exists(), str(root if module == 'editor' else path), fix)


def check_unity_files(version=TARGET):
    """The files the pipeline and the editor-free PS Vita build read from Unity: extracted from its
    installers by setup.py, or an installed editor's with its PS Vita module."""
    data = editor_data(version)
    gaps = missing(data, UNITY_FILES_CHECKED)
    return Check(
        "Unity's files",
        not gaps,
        f'{data} lacks ' + ', '.join(gaps) if gaps else str(data),
        f'{SETUP} --vita (extracts them from Unity {version} installers; installs nothing)',
    )


def check_visual_cpp():
    path = visual_cpp()
    return Check(
        'Visual Studio C++ tools',
        bool(path),
        path or 'not found (IL2CPP compiles the player with them)',
        'winget install Microsoft.VisualStudio.2022.BuildTools --override '
        '"--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"',
    )


# The builder's native packages (quicktex's encoders) are built with Visual Studio 2022 17.10 or later and
# crash in an older msvcp140.dll, such as the 2015 runtime (14.0.23026) the Unity 2018 installer brings.
VC_RUNTIME_MIN = (14, 40)
VC_REDIST_URL = 'https://aka.ms/vs/17/release/vc_redist.x64.exe'


def file_version(path: Path):
    """(major, minor, build, revision) from a DLL's version resource, or None."""
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL('version')
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, data):
        return None
    info, length = ctypes.c_void_p(), wintypes.UINT()
    if not version.VerQueryValueW(data, '\\', ctypes.byref(info), ctypes.byref(length)):
        return None
    fixed = ctypes.cast(info, ctypes.POINTER(wintypes.DWORD * 4)).contents  # signature, struct version, MS, LS
    return fixed[2] >> 16, fixed[2] & 0xFFFF, fixed[3] >> 16, fixed[3] & 0xFFFF


def vc_runtime():
    """The version of the 64-bit Visual C++ runtime (System32's msvcp140.dll), or None."""
    path = Path(os.environ.get('SYSTEMROOT', r'C:\Windows')) / 'System32' / 'msvcp140.dll'
    return file_version(path) if path.exists() else None


def check_vc_runtime():
    found = vc_runtime()
    shown = '.'.join(map(str, found)) if found else None
    return Check(
        'Visual C++ runtime',
        bool(found) and found[:2] >= VC_RUNTIME_MIN,
        f'{shown} (msvcp140.dll)'
        if found and found[:2] >= VC_RUNTIME_MIN
        else f'{shown or "none"}; the builder needs {".".join(map(str, VC_RUNTIME_MIN))} or later',
        f'{SETUP} --vita (installs the current Visual C++ Redistributable)',
    )


def signer_is_microsoft(status, subject):
    """Get-AuthenticodeSignature's Status and SignerCertificate.Subject of a Microsoft installer."""
    return status == 'Valid' and re.search(r'(^|,\s*)O=Microsoft Corporation\s*(,|$)', subject, re.I) is not None


def check_vita_sdk(unity=False):
    """The PS Vita SDK and the Target Manager tools, through the variables Unity reads. Only the
    editor's build (unity) needs Target Manager Server and Publishing Tools; the builder uses the SDK alone."""
    fix = f'{SETUP} --vita (downloads the SDK), or --vita-sdk <your SDK archive or folder>'
    checks = []
    sdk, stale = env('SCE_PSP2_SDK_DIR')
    if not sdk:
        found = next((c for c in sdk_candidates() if (c / SDK_TOOLS[0]).exists()), None)
        checks.append(
            Check(
                'PS Vita SDK',
                False,
                'SCE_PSP2_SDK_DIR is not set' + (f'; an SDK is in {found}' if found else ''),
                f'{SETUP} --vita-sdk "{found}"' if found else fix,
            )
        )
    else:
        problem, shown = sdk_status(Path(sdk))
        if problem:
            checks.append(Check('PS Vita SDK', False, problem, fix))
        else:
            checks.append(
                Check(
                    'PS Vita SDK',
                    True,
                    f'{sdk} (version {shown})' + (' - set for new shells; this one does not see it' if stale else ''),
                )
            )
    fix = f'{SETUP} --vita --unity (installs Target Manager Server and Publishing Tools from the SDK)'
    root, stale = env('SCE_ROOT_DIR')
    if not root:
        found = sce_root()
        checks.append(
            Check(
                'Target Manager (SCE_ROOT_DIR)',
                False,
                'SCE_ROOT_DIR is not set' + (f'; the tools are in {found}' if found else ''),
                fix,
            )
        )
    else:
        gaps = missing(Path(root), SCE_TOOLS)
        checks.append(
            Check(
                'Target Manager (SCE_ROOT_DIR)',
                not gaps,
                f'{root} lacks ' + ', '.join(Path(g).name for g in gaps)
                if gaps
                else str(root) + (' - set for new shells; this one does not see it' if stale else ''),
                fix,
            )
        )
    if not unity:
        checks[-1] = only_unity(checks[-1])
    return checks


def check_vita_host(required=False):
    value, _ = env('DRIFTER_VITA_HOST')  # the tools read it from the registry if this shell lacks it
    fix = f"{SETUP} --vita-host <the console's IP address>"
    return Check(
        'DRIFTER_VITA_HOST',
        bool(value),
        value or "not set (deploy and vita.py need the console's address)",
        fix,
        required=required,
    )


def check_node():
    node = shutil.which('node')
    without, with_node = FIRST_VITA_BUILD_MINUTES
    return Check(
        'Node.js (Unity Cache Server)',
        bool(node),
        node
        or f'not found: a first editor build imports audio in one editor (about {without} min for the Vita, with '
        f'--unity, instead of {with_node}) and runs without a cache server',
        'winget install OpenJS.NodeJS.LTS',
        required=False,
    )


def check_space(target, project_exists):
    need = space_needed(target, project_exists)
    free = free_gb(WORK)
    what = 'a rebuild' if project_exists else 'a first build'
    return Check(
        'Free disk space',
        free >= need,
        f'{free:.0f} GB free on {ROOT.anchor}, {what} needs about {need} GB',
        f'free space on {ROOT.anchor} or move the repository to a larger drive',
    )


def check_license(version, platform):
    """Start the editor in batch mode with a new empty project and read its log (about a minute)."""
    unity = EDITORS / version / EDITOR_FILES['editor']
    name = LICENSE_CHECK
    fix = (
        'activate a license in the editor, or remove its license check with unigate '
        f'(git clone https://github.com/xob0t/unigate, then in its folder: uv run unigate.py --editor '
        f'"{unity.parent}")'
    )
    if not unity.exists():
        return Check(name, False, 'no editor', f'{SETUP}')
    with tempfile.TemporaryDirectory(prefix='drifter-license-') as temp:
        log_path = Path(temp) / 'editor.log'
        try:
            result = subprocess.run(
                [
                    str(unity),
                    '-batchmode',
                    '-nographics',
                    '-quit',
                    '-createProject',
                    str(Path(temp) / 'project'),
                    '-buildTarget',
                    'PSP2' if platform == 'vita' else 'Win64',
                    '-logFile',
                    str(log_path),
                ],
                timeout=LICENSE_CHECK_SECONDS,
            )
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = None
        text = read_text(log_path) or ''
        problem = (
            license_log_problem(text, code, platform)
            if code is not None
            else f'the editor did not quit within {LICENSE_CHECK_SECONDS // 60} minutes'
        )
        if problem and text:
            kept = WORK / 'license-check.log'
            kept.parent.mkdir(parents=True, exist_ok=True)
            kept.write_text(text, encoding='utf-8')
            problem += f' (log: {kept})'
    if problem is None:
        LICENSE_STAMP.parent.mkdir(parents=True, exist_ok=True)
        with LICENSE_STAMP.open('a', encoding='utf-8') as handle:
            handle.write(license_key(version, platform) + '\n')
    return Check(name, problem is None, problem or f'the editor starts in batch mode with the {platform} target', fix)


# Passed license checks, one line per editor build and target, so setup.py does not start the editor every run.
LICENSE_STAMP = WORK / 'license-check.ok'


def license_key(version, platform):
    unity = EDITORS / version / EDITOR_FILES['editor']
    return f'{version} {platform} {unity.stat().st_mtime_ns if unity.exists() else 0}'


def license_checked(version, platform):
    text = read_text(LICENSE_STAMP)
    return bool(text) and license_key(version, platform) in text.splitlines()


# ---------------------------------------------------------------- targets and stages

TARGETS = ('windows', 'il2cpp', 'vita')


def target_of(platform, backend):
    return 'vita' if platform == 'vita' else ('il2cpp' if backend == 'il2cpp' else 'windows')


def project_dir(version, platform):
    return WORK / (f'drifter-{version}' + ('-vita' if platform == 'vita' else ''))


def module_checks(target, version, unity=False):
    """The editor module a target builds with and what its builds need besides (unity: a PS Vita build
    by the editor, pipeline.py --unity)."""
    if target == 'il2cpp':
        return [check_editor(version, 'il2cpp'), check_visual_cpp()]
    if target == 'vita':
        files = check_editor(version, 'vita') if unity else check_unity_files(version)
        return [files, *check_vita_sdk(unity), check_vc_runtime()]
    return []


def only_unity(check):
    """A check only builds with --unity (the editor's PS Vita build) need."""
    return replace(check, required=False, detail=check.detail + ' (only builds with --unity need it)')


def license_status(target, platform, version, license):
    """license: the check setup.py has just run, True to run it now, or False to report the last one. The
    PS Vita player is built without running the editor unless pipeline.py build gets --unity, so for the vita
    target the license is optional."""
    if isinstance(license, Check):
        check = license
    elif license:
        check = check_license(version, platform)
    elif license_checked(version, platform):
        check = Check(LICENSE_CHECK, True, f'the editor ran in batch mode with the {platform} target when last checked')
    else:
        check = Check(
            LICENSE_CHECK, True, f'not checked; {SETUP} doctor --target {target} --license starts the editor once'
        )
    return only_unity(check) if target == 'vita' else check


def checks_for(target, game: Path, version=TARGET, license=False):
    """Every requirement of a target, for the doctor's full checklist."""
    platform = 'vita' if target == 'vita' else 'windows'
    checks = [
        check_python(),
        check_csc(),
        check_game(game),
        check_assetripper(),
        check_fetched(),
    ]
    if target == 'vita':  # the PS Vita build reads Unity's files; only --unity runs the editor
        checks += [only_unity(check_editor(version)), only_unity(check_editor(version, 'vita'))]
    else:
        checks.append(check_editor(version))
    checks += module_checks(target, version)
    if target == 'vita':
        checks.append(check_vita_host())
    checks += [check_space(target, project_dir(version, platform).exists()), check_node()]
    checks.append(license_status(target, platform, version, license))
    return checks


def stage_checks(stage, platform, backend, game: Path, version=TARGET, unity=False):
    """The fast checks of one pipeline.py stage (no network, no editor start)."""
    target = target_of(platform, backend)
    project = project_dir(version, platform)
    stages = {'all': ('rip', 'project', 'build')}.get(stage, (stage,))
    # The PS Vita player of the target version is built without the editor, from Unity's files alone.
    editor_free = platform == 'vita' and version == TARGET and not unity
    checks = []
    if 'rip' in stages:
        checks += [check_game(game), check_assetripper()]
    if 'project' in stages:
        if 'rip' not in stages:
            checks.append(check_game(game))
            exported = WORK / f'rip-{version}' / 'ExportedProject' / 'ProjectSettings' / 'ProjectVersion.txt'
            checks.append(
                Check(
                    'AssetRipper export',
                    exported.exists(),
                    str(exported.parents[1]),
                    'uv run tools/downgrade/pipeline.py rip',
                )
            )
        checks += [check_csc(), check_unity_files(version) if editor_free else check_editor(version)]
    if 'build' in stages:
        if 'project' not in stages:
            checks.append(
                Check(
                    'Assembled project',
                    project.exists(),
                    str(project),
                    'uv run tools/downgrade/pipeline.py project' + (' --platform vita' if platform == 'vita' else ''),
                )
            )
        checks += ([] if editor_free else [check_editor(version)]) + module_checks(target, version, not editor_free)
    if stage in ('rip', 'project', 'build', 'all'):
        checks.append(check_space(target, project.exists()))
    if stage == 'deploy':
        checks.append(check_vita_host(required=True))
    # Each check once, in order (all repeats the game and the editor).
    unique = {}
    for check in checks:
        unique.setdefault(check.name, check)
    return list(unique.values())


def preflight(stage, platform, backend, game: Path, version=TARGET, unity=False):
    """Stop before a stage whose requirements are missing, with the checklist and the fixes. The SDK's
    variables set for new shells are taken into this one, for the editor the build starts."""
    adopt_env(('SCE_PSP2_SDK_DIR', 'SCE_ROOT_DIR'))
    checks = stage_checks(stage, platform, backend, game, version, unity)
    lines, failed = summarize(checks)
    if failed:
        show(progress.errors, f'pipeline.py {stage}: {failed} requirement(s) missing:', *lines)
        show(
            progress.errors,
            f'Full checklist: {SETUP} doctor --target {target_of(platform, backend)}. '
            'Skip these checks with --skip-checks.',
        )
        sys.exit(1)


def main(argv=None, license_check=None):
    parser = argparse.ArgumentParser(
        prog='setup.py doctor', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--target',
        choices=TARGETS,
        default='windows',
        help='windows (Mono player), il2cpp (Windows IL2CPP player) or vita',
    )
    parser.add_argument('--license', action='store_true', help='start the editor once in batch mode (about a minute)')
    args = parser.parse_args(argv)
    checks = checks_for(args.target, default_game(), license=license_check or args.license)
    lines, failed = summarize(checks)
    show(progress.console, f'Requirements for the {args.target} target:', *lines)
    if failed:
        progress.console.print(Text(f'{failed} required item(s) missing.', 'bold red'))
        return 1
    progress.console.print(Text('Everything required is in place.', 'bold green'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
