"""Translate the installed PC build of The Drifter into a Unity 2018.2 project and player.

    uv run tools/downgrade/pipeline.py all

Stages, each runnable alone:
  rip      AssetRipper exports the game as a project serialized for the target version.
  project  Assemble work/drifter-<version>[-vita]: rewrite assemblies, prune plugins, decompile
           shaders over AssetRipper's dummies, add the build script.
  build    Build the player. The PS Vita player is built without running Unity (tools/builder);
           --unity, and every Windows player, imports and builds it with the editor in batch mode.
  package  PS Vita: turn the build into a VPK.
           For the PS Vita, all also packages the VPK and then, unless --keep (or --unity), deletes
           the work files, the unpacked build and the tools setup.py installed.
  deploy   PS Vita: package, then upload the VPK to the console (DRIFTER_VITA_HOST, see common.py).

The game installation is only read. Outputs go to work/, builds/ and artifacts/.

The rules live beside this file: asset_rules.py (scenes, prefabs, textures, audio, settings),
shader_rules.py (decompiled shaders and their rewrites), DowngradeAssemblies.cs with rewriter/*.cs
(game code), Compat2018.cs with compat/*.cs (the DrifterCompat runtime the rewritten code calls) and
StubAssembly.cs; common.py holds the paths and helpers they share. Other scripts use this
module's names, and those it imports from common.py, as pipeline.<name>; the rules are imported
from their own modules.
"""

import argparse
import hashlib
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import fastnoise
import livearea
from asset_rules import (
    atlas_overlay,
    audio_overlay,
    glyph_overlay,
    link_xml,
    missing_class_overlay,
    mixer_overlay,
    name_overlay,
    native_plugin_overlay,
    packed_sprite_overlay,
    pixel_camera_overlay,
    powerquest_overlay,
    render_texture_overlay,
    settings_overlay,
    template_overlay,
    texture_overlay,
    upstream_overlay,
)
from common import (
    ARTIFACTS,
    ASSETRIPPER,
    CECIL,
    CSC,
    DEFAULT_GAME,
    EDITORS,
    EXTERNAL,
    HERE,
    ROOT,
    TARGET,
    VITA_HOST,
    WORK,
    YAML_FILES,
    editor_data,
    folder_meta,
    game_data,
    log,
    meta_guid,
    meta_of,
    mono_cecil,
    remove_tree,
    run,
    stable_meta,
    vita_host,
)
from shader_rules import shader_overlay

# ---------------------------------------------------------------- rip

RIP_SETTINGS = {
    'DefaultVersion': '0.0.0a0',
    'BundledAssetsExportMode': 'DirectExport',
    'ScriptContentLevel': 'Level2',
    'AudioExportFormat': 'Default',
    'ImageExportFormat': 'Png',
    'LightmapTextureExportFormat': 'Yaml',
    'SpriteExportMode': 'Yaml',
    'ShaderExportMode': 'Dummy',
    'TextExportMode': 'Parse',
    'ScriptLanguageVersion': 'AutoSafe',
    'ScriptExportMode': 'DllExportWithoutRenaming',
    'PreferOriginalTextureExtension': '',
}
RIP_STAMP = 'drifter-rip.txt'
# AssetRipper's server gets STARTUP_POLLS tries, POLL_SECONDS apart, to answer (about a minute);
# loading the game or exporting the project is one request that can take up to REQUEST_SECONDS.
STARTUP_POLLS, POLL_SECONDS = 120, 0.5
REQUEST_SECONDS = 3600


def rip_stamp(game: Path, version: str):
    """What an export depends on: the game's data files (names, sizes, dates), AssetRipper's
    executable and the export settings."""
    exe = ASSETRIPPER
    files = sorted(p for p in game_data(game).rglob('*') if p.is_file())
    lines = [f'{p.relative_to(game).as_posix()} {p.stat().st_size} {p.stat().st_mtime_ns}' for p in files]
    lines.append(f'{exe.name} {exe.stat().st_size} {exe.stat().st_mtime_ns}' if exe.exists() else 'no AssetRipper')
    lines.append(repr(sorted({**RIP_SETTINGS, 'TargetVersion': version}.items())))
    return hashlib.sha256('\n'.join(lines).encode()).hexdigest()


def rip_is_current(game: Path, out: Path, version: str):
    stamp = out / RIP_STAMP
    return stamp.exists() and stamp.read_text(encoding='utf-8').strip() == rip_stamp(game, version)


def rip(game: Path, out: Path, version: str):
    exe = ASSETRIPPER
    stamp = rip_stamp(game, version)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with open(ARTIFACTS / 'assetripper.log', 'w', encoding='utf-8') as log_file:
        server = subprocess.Popen(
            [str(exe), '--headless', '--port', str(port), '--log-path', str(ARTIFACTS / 'assetripper-file.log')],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(STARTUP_POLLS):
                try:
                    urllib.request.urlopen(base + '/', timeout=2)
                    break
                except OSError:
                    time.sleep(POLL_SECONDS)
            else:
                raise RuntimeError('AssetRipper did not start')
            post(base, '/Settings/Update', {**RIP_SETTINGS, 'TargetVersion': version})
            settings = urllib.request.urlopen(base + '/Settings/Edit').read().decode()
            if f'name="TargetVersion" value="{version}"' not in settings:
                raise RuntimeError('AssetRipper rejected the target version setting')
            if out.exists():
                shutil.rmtree(out)
            log(f'Loading {game}')
            post(base, '/LoadFolder', {'path': str(game)})
            log(f'Exporting to {out}')
            post(base, '/Export/UnityProject', {'path': str(out)})
            project_version = out / 'ExportedProject' / 'ProjectSettings' / 'ProjectVersion.txt'
            if not project_version.exists():
                raise RuntimeError('AssetRipper export produced no project; see ' + str(ARTIFACTS / 'assetripper.log'))
            log('Exported project: ' + project_version.read_text().strip())
            (out / RIP_STAMP).write_text(stamp + '\n', encoding='utf-8')
        finally:
            server.kill()


def post(base, path, fields):
    """POST a form to AssetRipper's server and wait for it to finish the request."""
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(base + path, data=data, method='POST')
    with urllib.request.urlopen(request, timeout=REQUEST_SECONDS) as response:
        response.read()


# ---------------------------------------------------------------- project


# Engine assemblies of each target platform. Vita modules can be empty shells
# (UnityEngine.VideoModule), so the game is rewritten against them; types they
# lack go to the shim like any other missing API.
PLATFORM_ENGINE = {'vita': Path('PlaybackEngines') / 'PSP2Player' / 'Managed'}


def engine_library(version, out: Path, sources, modules=()):
    """Compile a C# 5 library from source files against the editor's unityaot class libraries,
    UnityEngine's core module and the given UnityEngine modules (e.g. 'AudioModule')."""
    check_parts()
    data = editor_data(version)
    bcl = data / 'MonoBleedingEdge' / 'lib' / 'mono' / 'unityaot'
    engine = data / 'Managed' / 'UnityEngine'
    run(
        [
            CSC,
            '/nologo',
            '/noconfig',
            '/target:library',
            '/nostdlib+',
            '/unsafe',
            *(f'/r:{bcl / name}' for name in ('mscorlib.dll', 'System.dll', 'System.Core.dll')),
            *(f'/r:{engine / f"UnityEngine.{name}.dll"}' for name in ('CoreModule', *modules)),
            f'/out:{out}',
            *sources,
        ]
    )


# The C# sources split into parts, each list in the order of the single file it was split from
# (its first file explains the parts; the order keeps the compiled output as it was):
# DrifterCompat and the UnityEngine modules it uses, the parity probe and the assembly rewriter.
COMPAT_SOURCES = [
    HERE / 'Compat2018.cs',
    *(
        HERE / 'compat' / f'{name}.cs'
        for name in (
            'Saves',
            'StoredSaves',
            'FastFields',
            'Engine',
            'UnloadedRoomRefs',
            'Noise',
            'Platform',
            'Crt',
            'CrtCompare',
            'ApiShims',
            'ScriptCache',
            'Audio',
            'ScriptCosts',
            'PathMath',
            'RepeatedCalls',
            'Walking',
            'Allocs',
            'Scriptables',
            'EffectSprites',
            'EffectShapes',
        )
    ),
]
COMPAT_MODULES = ['ParticleSystemModule', 'AnimationModule', 'AudioModule', 'TextRenderingModule', 'Physics2DModule']
DIAG_SOURCES = [HERE / 'diag' / f'{name}.cs' for name in ('Diag', 'Checks', 'Experiments', 'Remote', 'Run', 'Timers')]
REWRITER_SOURCES = [
    HERE / 'DowngradeAssemblies.cs',
    *(
        HERE / 'rewriter' / f'{name}.cs'
        for name in ('Patches', 'ScriptCosts', 'Walking', 'Allocs', 'Gameplay', 'Controls', 'Redirect')
    ),
]
# Every .cs file in these folders belongs to one of the lists (InjectDiag.cs is the probe's injector).
PART_FOLDERS = {
    HERE / 'compat': COMPAT_SOURCES,
    HERE / 'diag': [*DIAG_SOURCES, HERE / 'diag' / 'InjectDiag.cs'],
    HERE / 'rewriter': REWRITER_SOURCES,
}


def check_parts():
    """Stops if a part is missing from its list: the compile would leave it out without a word."""
    unlisted = sorted(
        f'{path.parent.name}/{path.name}'
        for folder, listed in PART_FOLDERS.items()
        for path in folder.glob('*.cs')
        if path not in listed
    )
    if unlisted:
        sys.exit(f'sources missing from COMPAT_SOURCES, DIAG_SOURCES or REWRITER_SOURCES: {", ".join(unlisted)}')


def compile_tools(version, platform):
    data = editor_data(version)
    bcl = data / 'MonoBleedingEdge' / 'lib' / 'mono' / 'unityaot'
    engine = data / 'Managed' / 'UnityEngine'
    bin_dir = EXTERNAL / 'downgrade-bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    cecil = bin_dir / CECIL.name  # beside the tools, which load it at run time
    if not cecil.exists() or cecil.read_bytes() != mono_cecil().read_bytes():
        shutil.copy2(mono_cecil(), cecil)
    compat = bin_dir / 'DrifterCompat.dll'
    engine_library(version, compat, COMPAT_SOURCES, COMPAT_MODULES)
    rewriter = bin_dir / 'DowngradeAssemblies.exe'
    run([CSC, '/nologo', f'/r:{cecil}', f'/out:{rewriter}', *REWRITER_SOURCES])
    run([CSC, '/nologo', f'/r:{cecil}', f'/out:{bin_dir / "StubAssembly.exe"}', HERE / 'StubAssembly.cs'])
    target_engine = data / PLATFORM_ENGINE[platform] if platform in PLATFORM_ENGINE else engine
    return compat, rewriter, [bcl, target_engine, data / 'UnityExtensions' / 'Unity' / 'GUISystem']


def referenced_guids(assets: Path):
    """GUIDs referenced from serialized YAML assets."""
    pattern = re.compile(rb'guid: ([0-9a-f]{32})')
    found = set()
    for path in assets.rglob('*'):
        if path.suffix in YAML_FILES and path.is_file():
            found.update(m.decode() for m in pattern.findall(path.read_bytes()))
    return found


def plugin_meta(guid):
    return f"""fileFormatVersion: 2
guid: {guid}
PluginImporter:
  serializedVersion: 2
  externalObjects: {{}}
  iconMap: {{}}
  executionOrder: {{}}
  isPreloaded: 0
  isOverridable: 0
  platformData:
  - first:
      Any:
    second:
      enabled: 1
      settings: {{}}
  - first:
      Editor: Editor
    second:
      enabled: 0
      settings:
        DefaultValueInitialized: true
  userData:
  assetBundleName:
  assetBundleVariant:
"""


def sync_tree(source: Path, destination: Path, overlay: dict, excluded: set):
    """Mirror source into destination with generated files from overlay replacing or
    adding paths, and excluded source paths left out. Unchanged files keep their
    timestamps so Unity's import cache stays valid between runs."""
    destination.mkdir(parents=True, exist_ok=True)
    wanted = set(overlay)
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        if relative in excluded or relative in overlay:
            continue
        wanted.add(relative)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        stat = path.stat()
        if target.exists():
            other = target.stat()
            if other.st_size == stat.st_size and int(other.st_mtime) == int(stat.st_mtime):
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for relative, content in overlay.items():
        write_if_changed(destination / relative, content)
        wanted.update(relative.parents)
    for path in sorted(destination.rglob('*'), reverse=True):
        if path.relative_to(destination) not in wanted:
            shutil.rmtree(path) if path.is_dir() else path.unlink()


def write_if_changed(path: Path, content: bytes):
    if path.exists() and path.read_bytes() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# Managed wrappers of native libraries a platform lacks; their methods become no-ops
# returning defaults, so the game's own "not available" paths run instead.
STUBBED_ON = {'vita': ['com.rlabrecque.steamworks.net']}


def assembly_overlay(exported: Path, version: str, platform: str, diag: bool):
    """The rewritten game assemblies and the plugins they use, with DrifterCompat (and the parity probe
    built in for diag), in Assets/Plugins. Returns (overlay, the exported plugin files they replace,
    the assemblies)."""
    plugins = exported / 'Assets' / 'Plugins'
    plugins_relative = Path('Assets') / 'Plugins'
    overlay, excluded = {}, set()

    # Plugins referenced by assets are roots alongside the game's own code.
    guids = referenced_guids(exported / 'Assets')
    roots = ['Assembly-CSharp', 'Assembly-CSharp-firstpass']
    original_meta = {}
    for dll in plugins.glob('*.dll'):
        meta = meta_of(dll)
        original_meta[dll.stem] = meta.read_bytes()
        excluded.update({plugins_relative / dll.name, plugins_relative / meta.name})
        if meta_guid(meta) in guids and dll.stem not in roots:
            roots.append(dll.stem)
    log('Assembly roots: ' + ', '.join(roots))

    compat, rewriter, references = compile_tools(version, platform)
    rewritten = WORK / f'assemblies-{version}'
    if rewritten.exists():
        shutil.rmtree(rewritten)
    run([rewriter, plugins, rewritten, compat, ','.join(roots), *references])
    for name in STUBBED_ON.get(platform, []):
        target = rewritten / f'{name}.dll'
        if target.exists():
            stubbed = target.with_name(target.stem + '.stub.dll')
            run([EXTERNAL / 'downgrade-bin' / 'StubAssembly.exe', target, stubbed])
            stubbed.replace(target)
    if diag:
        inject_probe(rewritten, [compat.parent, *references])
    assembly_names = {'Recovered.Drifter': 'Assembly-CSharp', 'Recovered.PowerTools': 'Assembly-CSharp-firstpass'}
    for dll in [*rewritten.glob('*.dll'), compat]:
        source = assembly_names.get(dll.stem, dll.stem)
        meta = original_meta.get(source) or plugin_meta(hashlib.md5(dll.stem.encode()).hexdigest()).encode()
        overlay[plugins_relative / dll.name] = dll.read_bytes()
        overlay[meta_of(plugins_relative / dll.name)] = meta
    log('Plugins: ' + ', '.join(sorted(p.name for p in overlay if p.suffix == '.dll')))
    return overlay, excluded, [*rewritten.glob('*.dll'), compat]


def asset_overlay(exported: Path, game: Path, version: str, platform: str, diag: bool, assemblies: list):
    """The rules for the exported assets and settings (asset_rules.py, shader_rules.py), in order: a rule
    reads the files the rules before it changed. Returns (overlay, exported paths left out)."""
    overlay, excluded = {}, set()
    overlay.update(shader_overlay(exported, game, version))
    # Windows players use the one-pass CRT filter only behind test flags (-crt-single-pass,
    # crt_compare.py), so only their diag builds include it.
    if platform == 'windows' and not diag:
        for name in ('CrtSinglePass.shader', 'CrtSinglePass.shader.meta'):
            overlay.pop(Path('Assets') / 'Resources' / name, None)
    upstream, replaced = upstream_overlay(exported)
    for path in replaced:
        overlay.pop(path, None)
    overlay.update(upstream)
    excluded.update(replaced)
    overlay.update(settings_overlay(exported, platform))
    if platform == 'windows':
        overlay.update(native_plugin_overlay(game))
    overlay.update(atlas_overlay(exported, game))
    overlay.update(render_texture_overlay(exported, platform))
    overlay.update(audio_overlay(exported, platform))
    overlay.update(mixer_overlay(exported, platform))
    overlay.update(template_overlay(exported, overlay))
    overlay.update(powerquest_overlay(exported, overlay))
    overlay.update(missing_class_overlay(exported, overlay, platform))
    overlay.update(pixel_camera_overlay(exported, overlay, platform))
    overlay[Path('Assets') / 'link.xml'] = link_xml(assemblies, platform).encode()
    overlay[Path('Assets') / 'link.xml.meta'] = stable_meta('asset', 'link.xml', 'TextScriptImporter').encode()
    overlay.update(texture_overlay(exported, game, platform))
    overlay.update(packed_sprite_overlay(exported, overlay))
    moved, renamed = name_overlay(exported, game, overlay)
    overlay.update(moved)
    excluded.update(renamed)
    return overlay, excluded


# The pipeline's editor scripts (unity/*.cs: the build entry point) in the project.
EDITOR_SCRIPTS = Path('Assets') / 'Editor' / 'Downgrade'


def editor_scripts():
    """The pipeline's editor scripts, with fixed GUIDs so reruns don't reimport them."""
    overlay = folder_meta(EDITOR_SCRIPTS)
    for source in (HERE / 'unity').glob('*.cs'):
        overlay[EDITOR_SCRIPTS / source.name] = source.read_bytes()
        overlay[meta_of(EDITOR_SCRIPTS / source.name)] = stable_meta('script', source.name, 'MonoImporter').encode()
    return overlay


def pipeline_overlay(platform: str, diag: bool):
    """Files of the pipeline's own: the compat rules' shaders and noise texture, the Vita's glyphs and
    the editor scripts."""
    overlay = {}
    # Shaders the compat rules use (shaders/), in Resources for Shader.Find, with fixed GUIDs.
    for source in (HERE / 'shaders').glob('*.shader'):
        target = Path('Assets') / 'Resources' / source.name
        overlay[target] = source.read_bytes()
        overlay[meta_of(target)] = stable_meta('shader', source.name, 'ShaderImporter').encode()
    # The fast noise path's texture (fastnoise.py, Compat FastNoise) as raw texels.
    noise = Path('Assets') / 'Resources' / f'{fastnoise.RESOURCE_NAME}.bytes'
    overlay[noise] = fastnoise.texels()
    overlay[meta_of(noise)] = stable_meta('asset', noise.name, 'TextScriptImporter').encode()
    overlay.update(glyph_overlay(platform, diag))
    overlay.update(editor_scripts())
    return overlay


def assemble(rip_dir: Path, project: Path, version: str, game: Path, platform: str, diag: bool):
    exported = rip_dir / 'ExportedProject'
    plugins, plugin_files, assemblies = assembly_overlay(exported, version, platform, diag)
    assets, excluded = asset_overlay(exported, game, version, platform, diag, assemblies)
    overlay = {**plugins, **assets, **pipeline_overlay(platform, diag)}
    excluded |= plugin_files

    log(f'Synchronizing {project}')
    for part in ('Assets', 'ProjectSettings', 'Packages'):
        sync_tree(
            exported / part,
            project / part,
            {k.relative_to(part): v for k, v in overlay.items() if k.parts[0] == part},
            {k.relative_to(part) for k in excluded if k.parts[0] == part},
        )


def steam_app_id(game: Path):
    """The Steam app ID from the library manifest that owns this install folder."""
    for manifest in game.parent.parent.glob('appmanifest_*.acf'):
        text = manifest.read_text(encoding='utf-8', errors='replace')
        if re.search(rf'"installdir"\s+"{re.escape(game.name)}"', text, re.I):
            return re.search(r'"appid"\s+"(\d+)"', text).group(1)
    return None


# ---------------------------------------------------------------- build


def inject_probe(assemblies: Path, references: list):
    """Build the parity probe (diag/Diag.cs) into the game assembly. diag.py injects it
    into player copies instead, which needs managed DLLs; IL2CPP players have none."""
    import diag

    dll, injector = diag.compile_diag(WORK / 'diag' / 'pipeline')
    diag.inject(injector, assemblies / 'Recovered.Drifter.dll', dll, *references)
    shutil.copy2(dll, assemblies / dll.name)


CACHE_SERVER = ('127.0.0.1', 8126)
CACHE_SERVER_PACKAGE = 'unity-cache-server@6.4.0'


def cache_server():
    """A local Unity Cache Server, so imports already done for the same file, settings
    and platform are fetched instead of repeated: Unity 2018 imports one asset at a
    time, and the Vita audio encode alone takes about an hour. Started in the
    background on first use and left running; needs Node.js. Returns host:port or None."""
    address = f'{CACHE_SERVER[0]}:{CACHE_SERVER[1]}'
    try:
        socket.create_connection(CACHE_SERVER, timeout=1).close()
        return address
    except OSError:
        pass
    node, npm = shutil.which('node'), shutil.which('npm')
    if not node or not npm:
        log('Node.js not found; building without a cache server')
        return None
    package = EXTERNAL / 'unity-cache-server'
    main = package / 'node_modules' / 'unity-cache-server' / 'main.js'
    if not main.exists():
        run([npm, 'install', '--prefix', package, CACHE_SERVER_PACKAGE, '--no-audit', '--no-fund'])
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with (ARTIFACTS / 'cache-server.log').open('a') as server_log:
        subprocess.Popen(
            [
                node,
                '-r',
                str(HERE / 'node_util_compat.js'),
                str(main),
                '--host',
                CACHE_SERVER[0],
                '--port',
                str(CACHE_SERVER[1]),
                '--cache-path',
                str(WORK / 'unity-cache'),
                '--log-level',
                '3',
            ],
            stdin=subprocess.DEVNULL,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    for _ in range(50):
        time.sleep(0.2)
        try:
            socket.create_connection(CACHE_SERVER, timeout=1).close()
            log(f'Started a Unity Cache Server on {address} (cache in {WORK / "unity-cache"})')
            return address
        except OSError:
            pass
    log('The cache server did not start; see ' + str(ARTIFACTS / 'cache-server.log'))
    return None


def build(
    project: Path,
    version: str,
    output: Path,
    game: Path,
    backend: str = 'mono',
    platform: str = 'windows',
    diag: bool = False,
    development: bool = False,
    jobs: int | None = None,
):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    unity = EDITORS / version / 'Editor' / 'Unity.exe'
    log_path = ARTIFACTS / (f'build-{version}-{platform}-{backend}' + ('-diag' if diag else '') + '.log')
    # The build entry point must match this script even if only the build stage reruns. Only the
    # scripts: the editor rewrites .meta files it imports, and a rewritten one would import again.
    for relative, content in editor_scripts().items():
        if relative.suffix == '.cs':
            write_if_changed(project / relative, content)
    server = cache_server()
    target = 'PSP2' if platform == 'vita' else 'Win64'
    if server:
        # Clips the editor would import are imported by several editors at once into the cache.
        import import_jobs

        import_jobs.prewarm(project, unity, target, server, import_jobs.default_jobs() if jobs is None else jobs)
    started = time.time()
    result = subprocess.run(
        [
            str(unity),
            '-batchmode',
            '-nographics',
            '-projectPath',
            str(project),
            '-executeMethod',
            'DowngradeBuild.Build',
            '-buildOutput',
            str(output),
            '-scriptingBackend',
            backend,
            '-buildTarget',
            target,
            *(['-CacheServerIPAddress', server] if server else []),
            *(['-developmentBuild'] if development else []),
            '-logFile',
            str(log_path),
        ]
    )
    log(f'Unity exited {result.returncode} after {time.time() - started:.0f}s; log {log_path}')
    summarize_log(log_path)
    if result.returncode != 0:
        sys.exit(result.returncode)
    # Unity can report success after a failed IL2CPP step; check for the player, and for
    # IL2CPP's failure in the log (a failed link leaves the previous build's game module).
    player = next(output.glob('*.self'), None) if platform == 'vita' else (output if output.exists() else None)
    if player is None:
        sys.exit(f'Unity reported success but wrote no player at {output}')
    # The previous build's player stays in place, and the executables are copies that keep
    # the editor's dates, so check a data file every build writes.
    written = (
        output / 'Media' / 'globalgamemanagers'
        if platform == 'vita'
        else output.parent / f'{output.stem}_Data' / 'globalgamemanagers'
    )
    if not written.exists() or written.stat().st_mtime < started:
        sys.exit(f'Unity reported success but did not write {written}')
    if 'BuilderFailedException' in log_path.read_text(encoding='utf-8', errors='replace'):
        sys.exit(f'Unity reported success but IL2CPP failed; see {log_path}')
    # Without this, steam_api relaunches the player through Steam and exits, as the
    # original does. Lets both run side by side for comparison.
    app_id = steam_app_id(game) if platform == 'windows' else None
    if app_id:
        (output.parent / 'steam_appid.txt').write_text(app_id + '\n')


# Build log lines worth a look; the summary prints each distinct one with its count, the first
# SUMMARY_LINES of them, cut at SUMMARY_WIDTH characters.
LOG_KEYS = re.compile(
    r'DOWNGRADE_BUILD|error CS\d+|Exception|Build Finished|Aborting batchmode|'
    r'Compilation failed|Unable to parse|could not be loaded|is missing'
)
SUMMARY_LINES, SUMMARY_WIDTH = 80, 300


def summarize_log(log_path: Path):
    text = log_path.read_text(encoding='utf-8', errors='replace')
    counts = Counter(line.strip() for line in text.splitlines() if LOG_KEYS.search(line))
    for line, count in list(counts.items())[:SUMMARY_LINES]:
        print(f'  {count:5d}x {line[:SUMMARY_WIDTH]}')


# ---------------------------------------------------------------- vita packaging

VITA_TITLE_ID = 'DRFT00001'  # matches the content ID set in DowngradeBuild.cs
VITA_FTP = (VITA_HOST, 1337)  # VitaShell FTP server
VITA_COMMANDS = (VITA_HOST, 1338)  # vitacompanion command server


def vita_app_files(build: Path):
    """Files of the installed app for a PC Hosted PS Vita build, as (path in the app,
    local path or bytes): the player becomes eboot.bin with the trial flag cleared,
    host-only files are left out."""
    selfs = list(build.glob('*.self'))
    if len(selfs) != 1:
        raise RuntimeError(f'expected one .self in {build}, found {[p.name for p in selfs]}')
    player = bytearray(selfs[0].read_bytes())
    player[0x80] = 0  # trial flag, as the community PSVita Unity Utilities do
    marker = player.find(b'trial.png')
    if marker >= 0:
        player[marker] = 0  # no trial watermark
    yield 'eboot.bin', bytes(player)
    art = livearea.generate()
    skip = {'SymbolFiles', 'configuration.psp2path'}
    for path in sorted(build.rglob('*')):
        relative = path.relative_to(build)
        if path.is_dir() or relative.parts[0] in skip or path.suffix in ('.self', '.bat'):
            continue
        name = relative.as_posix()
        yield name, art / VITA_ARTWORK[name] if name in VITA_ARTWORK else path


# LiveArea images from livearea.py, in place of Unity's placeholders.
VITA_ARTWORK = {
    'sce_sys/icon0.png': 'icon0.png',
    'sce_sys/pic0.png': 'pic0.png',
    'sce_sys/livearea/contents/bg0.png': 'bg0.png',
    'sce_sys/livearea/contents/default_gate.png': 'gate.png',
}


def package_vita(build: Path) -> Path:
    """Turn a PC Hosted PS Vita build into an installable VPK."""
    import zipfile

    if not any(build.glob('*.self')):
        sys.exit(f'No PS Vita build in {build}; build it first: uv run tools/downgrade/pipeline.py all --platform vita')
    vpk = build.with_name(build.name + '.vpk')  # with_suffix would cut the version at its dots
    with zipfile.ZipFile(vpk, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, source in vita_app_files(build):
            if isinstance(source, bytes):
                archive.writestr(name, source)
            else:
                archive.write(source, name)
    log(f'Packaged {vpk} ({vpk.stat().st_size / 2**20:.0f} MB)')
    return vpk


def deploy_vita(vpk: Path):
    """Upload the VPK to ux0:data/ over VitaShell FTP, to be installed in VitaShell. (Updates of
    an installed app go through vita.py sync or install instead.)"""
    import ftplib

    vita_host()
    with ftplib.FTP() as ftp:
        ftp.connect(*VITA_FTP, timeout=30)
        ftp.login()
        ftp.cwd('ux0:/data/')
        with vpk.open('rb') as handle:
            ftp.storbinary(f'STOR {vpk.name}', handle, blocksize=1 << 16)
    log(f'Uploaded to ux0:data/{vpk.name}; install it with VitaShell, then: launch {VITA_TITLE_ID}')


def remove_work_files(work):
    """Delete what all leaves besides the VPK: the work files, the unpacked build, and the tools
    setup.py installed for the build (setup.installed_tools). Say how much space that frees."""
    sys.path.insert(0, str(HERE.parent))
    import setup

    sdk = setup.our_sdk()
    size = 0
    for path in work + setup.installed_tools():
        if not path.exists():
            continue
        files = [path] if path.is_file() else [f for f in path.rglob('*') if f.is_file()]
        size += sum(f.stat().st_size for f in files)
        try:
            if path.is_file():
                path.unlink()
            else:
                remove_tree(path)
        except OSError as error:
            log(f'Could not delete {path}: {error}')
    if sdk and not sdk.exists():
        setup.remove_user_env('SCE_PSP2_SDK_DIR')
    if EXTERNAL.exists() and not any(EXTERNAL.iterdir()):
        EXTERNAL.rmdir()
    log(
        f'Deleted the work files, the unpacked build and the tools setup.py installed ({size / 2**30:.1f} GB). '
        'To build again, run setup.py again; --keep keeps them'
    )


# ---------------------------------------------------------------- main


def build_dir(version: str, backend: str, platform: str = 'windows', diag: bool = False, unity: bool = False):
    """unity: a PS Vita player the editor built (--unity), kept apart from the one built without it."""
    suffix = '-diag' if diag else ''
    if platform == 'vita':
        return ROOT / 'builds' / (f'drifter-{version}-vita' + ('-unity' if unity else '') + suffix)
    return ROOT / 'builds' / (f'drifter-{version}' + ('-il2cpp' if backend == 'il2cpp' else '') + suffix)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        'stage',
        choices=['rip', 'project', 'build', 'all', 'package', 'deploy'],
        help='package: make the Vita VPK; deploy: also upload it to the Vita (both imply --platform vita)',
    )
    parser.add_argument('--game', type=Path, default=DEFAULT_GAME)
    parser.add_argument('--version', default=TARGET, help='Unity 2018.2 patch release to target')
    parser.add_argument(
        '--backend',
        choices=['mono', 'il2cpp'],
        default='mono',
        help='scripting backend of the Windows player (the Vita target is IL2CPP only)',
    )
    parser.add_argument(
        '--platform',
        choices=['windows', 'vita'],
        default='windows',
        help='vita needs the Unity PS Vita build module and Sony SDK installed',
    )
    parser.add_argument(
        '--diag',
        action='store_true',
        help='build the parity probe into the player (for diag.py/sweep.py --build on IL2CPP); '
        'outputs to a separate -diag folder. Rerun project without it to remove the probe',
    )
    parser.add_argument(
        '--development',
        action='store_true',
        help="build: Unity's development player, which fills the engine's profiler markers "
        '(the probe logs them in its heartbeat); slower than a release player',
    )
    parser.add_argument(
        '--import-jobs',
        type=int,
        metavar='N',
        help='build: editors that import audio at once through the cache server before the build '
        '(default: three per four hardware threads; 1 imports in the build editor alone)',
    )
    parser.add_argument(
        '--unity',
        action='store_true',
        help='build, package, deploy: the PS Vita player built by the Unity editor, into a separate -unity folder. '
        f'Without it the Vita player for {TARGET} is built without running Unity (tools/builder/make.py: no license, '
        "no import; it reads the installed editor's files and the SDK's tools). Windows players always use the editor",
    )
    parser.add_argument(
        '--keep',
        action='store_true',
        help='all, PS Vita: keep the work files (the export, the project, the caches that make a rebuild '
        'take minutes), the unpacked build, which vita.py sync uploads, and the tools setup.py installed. '
        'Without it all deletes them once the VPK is made',
    )
    parser.add_argument(
        '--skip-checks', action='store_true', help="don't check the stage's requirements first (doctor.py's preflight)"
    )
    args = parser.parse_args()
    if not args.version.startswith('2018.2.'):
        parser.error('the compatibility rules are written for Unity 2018.2')
    rip_dir = WORK / f'rip-{args.version}'
    if args.platform == 'vita' or args.stage in ('package', 'deploy'):
        args.platform, args.backend = 'vita', 'il2cpp'
    # The editor-free build makes the PS Vita player of the target version; anything else needs the editor.
    unity = args.unity or args.platform != 'vita' or args.version != TARGET
    if not unity and (args.development or args.import_jobs):
        parser.error('--development and --import-jobs are for the editor build: add --unity')
    if not args.skip_checks:
        import doctor

        doctor.preflight(args.stage, args.platform, args.backend, args.game, args.version, unity)
    project = WORK / (f'drifter-{args.version}' + ('-vita' if args.platform == 'vita' else ''))
    output = build_dir(args.version, args.backend, args.platform, args.diag, unity)
    if args.platform == 'windows':
        output = output / 'TheDrifter.exe'
    # `all` reuses an export of the same game files with the same AssetRipper and settings
    # (the Windows and Vita projects come from the same one); the rip stage always exports.
    if args.stage == 'rip' or (args.stage == 'all' and not rip_is_current(args.game, rip_dir, args.version)):
        rip(args.game, rip_dir, args.version)
    elif args.stage == 'all':
        log(f'Reusing the export in {rip_dir}: same game files, AssetRipper and settings')
    # The project folder is the same with or without the probe, the build folder is not:
    # remember which the project has, so a build never lands in the other folder.
    marker = project / 'ProjectSettings' / 'downgrade-diag.txt'
    if args.stage in ('project', 'all'):
        assemble(rip_dir, project, args.version, args.game, args.platform, args.diag)
        marker.write_text(str(args.diag), encoding='utf-8')
    if args.stage in ('build', 'all') and marker.exists() and marker.read_text(encoding='utf-8') != str(args.diag):
        sys.exit(
            f'{project} was assembled with diag={marker.read_text(encoding="utf-8")}; run the project stage '
            f'again or build with the same --diag'
        )
    if args.stage in ('build', 'all') and not unity:
        sys.path.insert(0, str(HERE.parent / 'builder'))
        import make

        make.make(output)
    elif args.stage in ('build', 'all'):
        build(
            project,
            args.version,
            output,
            args.game,
            args.backend,
            args.platform,
            args.diag,
            args.development,
            args.import_jobs,
        )
    if args.stage in ('package', 'deploy') or (args.stage == 'all' and args.platform == 'vita'):
        vpk = package_vita(build_dir(args.version, 'il2cpp', 'vita', args.diag, unity))
        if args.stage == 'deploy':
            deploy_vita(vpk)
    # The editor build's project holds Unity's import of the game (up to 100 minutes), so --unity keeps it.
    if args.stage == 'all' and args.platform == 'vita' and not unity and not args.keep:
        remove_work_files(
            [
                rip_dir,
                project,
                WORK / f'assemblies-{args.version}',
                WORK / 'shaders',
                WORK / 'upstream',
                WORK / 'livearea',
                WORK / 'builder',
                output,
            ]
        )


if __name__ == '__main__':
    main()
