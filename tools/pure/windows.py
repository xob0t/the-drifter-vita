"""Lay out a runnable Windows (x64, Mono) player of the PC project without Unity.

    uv run tools/pure/windows.py [--out dir] [--data dir] [--compare] [--run seconds]

What Unity's Win64 Mono build writes besides the serialized data, and where it comes from:

- `<name>.exe`, `UnityPlayer.dll`, `UnityCrashHandler64.exe` and `MonoBleedingEdge/` (the whole
  folder) from the player variation win64_nondevelopment_mono; the exe is WindowsPlayer.exe.
- `<name>_Data/Managed`: the variation's engine assemblies and their .xml docs (the .mdb symbols
  stay out of a non-development build); the project's managed plugins (`Assets/Plugins/*.dll`
  enabled for Standalone Win64); the runtime DLLs of the four UnityExtensions a standalone
  player takes (UI, Networking, Timeline, SpatialTracking); and the class libraries from
  `MonoBleedingEdge/lib/mono/4.5` that all of these reference, transitively.
- `<name>_Data/Plugins`: the project's native plugins enabled for Standalone Win64.
- `<name>_Data/Resources/unity default resources` from the variation.
- `<name>_Data/app.info` (company and product name), `boot.config`, and `steam_appid.txt` next to
  the exe when the game folder's Steam manifest names one (the pipeline's build writes it so
  steam_api does not relaunch the player through Steam).

The serialized files (levels, sharedassets, resources, globalgamemanagers, their .resS and
.resource streams, Resources/unity_builtin_extra) come from --data, by default the Windows
reference build's data folder: the Unity-free writer makes PS Vita data only so far. dx11.py
compiles the Direct3D 11 shader programs that data needs (check_dx11.py).

--compare checks every file against the reference Windows build (builds/drifter-2018.2.19f1).
--run starts the player for that many seconds and reports what Player.log says.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

import dnfile
import paths
import unityyaml

NAME = 'TheDrifter'
PROJECT = paths.common.WORK / f'drifter-{paths.TARGET}'
REFERENCE = paths.ROOT / 'builds' / f'drifter-{paths.TARGET}'
OUT = paths.ROOT / 'builds' / f'drifter-{paths.TARGET}-pure'
VARIATION = paths.EDITOR_DATA / 'PlaybackEngines' / 'windowsstandalonesupport' / 'Variations'
VARIATION = VARIATION / 'win64_nondevelopment_mono'
CLASS_LIBRARIES = paths.EDITOR_DATA / 'MonoBleedingEdge' / 'lib' / 'mono' / '4.5'
EXTENSIONS = paths.EDITOR_DATA / 'UnityExtensions' / 'Unity'
# Runtime DLLs of the extensions whose plugin settings include Standalone. The others hold
# editor code only, or target Android (GoogleAudioSpatializer) or UWP (HoloLens).
EXTENSION_DLLS = (
    'GUISystem/Standalone/UnityEngine.UI.dll',
    'Networking/Standalone/UnityEngine.Networking.dll',
    'Timeline/Runtime/UnityEngine.Timeline.dll',
    'UnitySpatialTracking/Runtime/UnityEngine.SpatialTracking.dll',
)
BOOT_CONFIG = (
    'gfx-enable-native-gfx-jobs=\nwait-for-native-debugger=0\nscripting-runtime-version=latest\nvr-enabled=0\n'
)
DATA_PREFIXES = ('level', 'sharedassets', 'resources.', 'globalgamemanagers')


def player_settings(project=PROJECT):
    return unityyaml.meta(project / 'ProjectSettings' / 'ProjectSettings.asset')['PlayerSettings']


def is_managed(path):
    try:
        pe = dnfile.dnPE(str(path), fast_load=True)
        pe.parse_data_directories()
        return pe.net is not None
    except Exception:
        return False


def assembly_refs(path):
    table = dnfile.dnPE(str(path)).net.mdtables.AssemblyRef
    return [str(row.Name) for row in (table.rows if table else [])]


def plugin_enabled(dll):
    """Whether a PluginImporter includes the plugin in a Standalone Win64 build."""
    meta = unityyaml.meta(Path(f'{dll}.meta'))['PluginImporter']
    enabled = {}
    for entry in meta.get('platformData') or []:
        ((kind, platform),) = entry['first'].items()
        enabled[(kind, platform or '')] = entry['second'].get('enabled') == '1'
    if ('Standalone', 'Win64') in enabled:
        return enabled[('Standalone', 'Win64')]
    return enabled.get(('Any', ''), True)


def project_plugins(project=PROJECT):
    """(managed, native) plugin DLLs of the project that a Win64 build takes."""
    managed, native = [], []
    for dll in sorted((project / 'Assets').rglob('*.dll')):
        if 'Editor' in dll.relative_to(project / 'Assets').parts or not plugin_enabled(dll):
            continue
        (managed if is_managed(dll) else native).append(dll)
    return managed, native


def class_libraries(roots):
    """The class libraries the given assemblies reference, transitively."""
    have = {p.stem for p in roots}
    found, todo = {}, list(roots)
    while todo:
        for name in assembly_refs(todo.pop()):
            library = CLASS_LIBRARIES / f'{name}.dll'
            if name in have or name in found or not library.exists():
                continue
            found[name] = library
            todo.append(library)
    return sorted(found.values())


def steam_app_id(game=paths.common.DEFAULT_GAME):
    """The Steam app ID from the library manifest that owns the game folder (as the pipeline)."""
    if game is None:
        return None
    for manifest in Path(game).parent.parent.glob('appmanifest_*.acf'):
        text = manifest.read_text(encoding='utf-8', errors='replace')
        if re.search(rf'"installdir"\s+"{re.escape(Path(game).name)}"', text, re.I):
            return re.search(r'"appid"\s+"(\d+)"', text).group(1)
    return None


def copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def player(out, data, name=NAME, project=PROJECT):
    """Writes the player folder; `data` is the folder the serialized files come from."""
    out.mkdir(parents=True, exist_ok=True)
    target = out / f'{name}_Data'
    copy(VARIATION / 'WindowsPlayer.exe', out / f'{name}.exe')
    for f in ('UnityPlayer.dll', 'UnityCrashHandler64.exe'):
        copy(VARIATION / f, out / f)
    shutil.copytree(VARIATION / 'MonoBleedingEdge', out / 'MonoBleedingEdge', dirs_exist_ok=True)

    managed_dir = target / 'Managed'
    engine = [p for p in sorted((VARIATION / 'Data' / 'Managed').iterdir()) if p.suffix != '.mdb']
    managed, native = project_plugins(project)
    extensions = [EXTENSIONS / e for e in EXTENSION_DLLS]
    roots = [p for p in engine if p.suffix == '.dll'] + managed + extensions
    for p in [*engine, *managed, *extensions, *class_libraries(roots)]:
        copy(p, managed_dir / p.name)
    for p in native:
        copy(p, target / 'Plugins' / p.name)
    resources = 'unity default resources'
    copy(VARIATION / 'Data' / 'Resources' / resources, target / 'Resources' / resources)

    settings = player_settings(project)
    (target / 'app.info').write_bytes(f'{settings["companyName"]}\n{settings["productName"]}'.encode())
    (target / 'boot.config').write_text(BOOT_CONFIG, encoding='ascii', newline='\n')
    app_id = steam_app_id()
    if app_id:
        (out / 'steam_appid.txt').write_text(app_id + '\n', encoding='ascii')  # \r\n, as the pipeline

    for p in sorted(data.iterdir()):
        if p.is_file() and p.name.startswith(DATA_PREFIXES):
            copy(p, target / p.name)
    copy(data / 'Resources' / 'unity_builtin_extra', target / 'Resources' / 'unity_builtin_extra')


def compare(out, ref=REFERENCE):
    """(identical, differing, missing, extra) relative paths against the reference build."""
    skip = {f'{NAME}.exe.messages.txt'}  # the editor's build log
    mine = {p.relative_to(out).as_posix() for p in out.rglob('*') if p.is_file()}
    theirs = {p.relative_to(ref).as_posix() for p in ref.rglob('*') if p.is_file()} - skip
    same, differ = [], []
    for rel in sorted(mine & theirs):
        a, b = out / rel, ref / rel
        ok = a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
        (same if ok else differ).append(rel)
    return same, differ, sorted(theirs - mine), sorted(mine - theirs)


def run(out, seconds, name=NAME, log=paths.WORK / 'windows' / 'Player.log'):
    """Starts the player windowed for `seconds`: (exit code or None if it was still running,
    the log, its error lines)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    log.unlink(missing_ok=True)
    proc = subprocess.Popen([str(out / f'{name}.exe'), '-screen-fullscreen', '0', '-logFile', str(log)])
    try:
        exited = proc.wait(seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        exited = None
    text = log.read_text(encoding='utf-8', errors='replace') if log.exists() else ''
    errors = [line for line in text.splitlines() if re.search(r'Exception|[Ee]rror|[Ff]ailed', line)]
    return exited, text, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--data', type=Path, default=REFERENCE / f'{NAME}_Data')
    parser.add_argument('--compare', action='store_true')
    parser.add_argument('--run', type=float, metavar='SECONDS')
    args = parser.parse_args()
    player(args.out, args.data)
    status = 0
    if args.compare:
        same, differ, missing, extra = compare(args.out)
        for label, items in (('differs', differ), ('missing', missing), ('extra', extra)):
            for rel in items:
                print(f'{label}: {rel}')
        print(f'{len(same)} files identical to the reference, {len(differ)} differ, {len(missing)} missing, '
              f'{len(extra)} extra')  # fmt: skip
        status = 1 if differ or missing else 0
    if args.run:
        exited, text, errors = run(args.out, args.run)
        state = 'still running (stopped)' if exited is None else f'exited with {exited}'
        print(f'player {state} after {args.run:g} s; Player.log: {len(text.splitlines())} lines, {len(errors)} errors')
        for line in errors[:20]:
            print('  ', line)
        status = status or (1 if exited not in (None, 0) else 0)
    return status


if __name__ == '__main__':
    sys.exit(main())
