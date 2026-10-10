"""Compare the original and translated players with identical diagnostic hooks.

    uv run tools/downgrade/diag.py <name> [--seconds 10] [--tag main] [--fresh] [--build dir] [player flags...]

Builds hard-linked copies of both players under work/diag/<tag>/, injects
DrifterDiag.Hook.Init() into LoadingScreen.OnEnable and PowerQuest.Awake, runs
each off-screen with the given -diag-* flags (see diag/Diag.cs), and writes the
probe's frame capture, renderer dumps and a side-by-side image to
artifacts/parity/<name>/. Use --fresh after changing diag/*.cs or rebuilding.
--build picks the translated player; IL2CPP players have no managed DLLs, so
they must be built with the probe already in (pipeline.py --diag).
"""

import argparse
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import capture
import pipeline
from PIL import Image

DIAG = pipeline.WORK / 'diag'  # per --tag subdirectory, so parallel investigations don't share copies
PLAYERS = {
    'original': (pipeline.WORK / 'original', 'Assembly-CSharp.dll'),
    'translated': (pipeline.ROOT / 'builds' / f'drifter-{pipeline.TARGET}', 'Recovered.Drifter.dll'),
}
# The timed methods of each assembly get ids from a multiple of this (-diag-time-scripts), so the
# assemblies' ids never overlap.
METHOD_IDS_PER_ASSEMBLY = 3000
DONE_SECONDS = 240  # how long a player gets to write the probe's done.txt


def command(name, tag, build, seconds, flags, fresh=False):
    """The command line of a diag.py run, for tools that run it (sweep.py, effect_strips.py)."""
    return [
        'uv',
        'run',
        str(Path(__file__)),
        name,
        '--tag',
        tag,
        *(['--build', str(build)] if build else []),
        *(['--fresh'] if fresh else []),
        '--seconds',
        str(seconds),
        *flags,
    ]


def compile_diag(diag: Path):
    source = pipeline.HERE / 'diag'
    (diag / 'bin').mkdir(parents=True, exist_ok=True)
    cecil = pipeline.mono_cecil()
    shutil.copy2(cecil, diag / 'bin' / cecil.name)
    dll, injector = diag / 'bin' / 'DrifterDiag.dll', diag / 'bin' / 'InjectDiag.exe'
    pipeline.engine_library(
        pipeline.TARGET,
        dll,
        pipeline.DIAG_SOURCES,
        ['ScreenCaptureModule', 'AudioModule', 'TextRenderingModule', 'AnimationModule'],
    )
    pipeline.run([pipeline.CSC, '/nologo', f'/r:{cecil}', f'/out:{injector}', source / 'InjectDiag.cs'])
    return dll, injector


def player_exe(folder: Path):
    """The player executable in a player folder (not Unity's crash handler), or None."""
    return next((p for p in folder.glob('*.exe') if p.name != 'UnityCrashHandler64.exe'), None)


def prepare(diag: Path, name, source: Path, assembly: str, dll: Path, injector: Path):
    """Hard-link the player into work/diag/<tag>/<name> and patch the copied game assembly."""
    target = diag / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    managed = pipeline.game_data(source).name + '/Managed'
    for path in source.rglob('*'):
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.relative_to(source).as_posix() == f'{managed}/{assembly}':
            shutil.copy2(path, destination)
        else:
            os.link(path, destination)
    # IL2CPP players and diag builds (pipeline.py --diag) already have the probe built in.
    built_in = not (source / managed / assembly).exists() or (source / managed / dll.name).exists()
    if not built_in:
        shutil.copy2(dll, target / managed / dll.name)
    # The game keeps saves in its working directory. Real copies, so autosaves
    # in a diagnostic run never write through a hard link.
    for save in pipeline.DEFAULT_GAME.glob('*.sav'):
        destination = target / save.name
        if destination.exists():
            destination.unlink()
        shutil.copy2(save, destination)
    # LoadingScreen runs in the first scene, so the probe starts before the splash
    # screens; PowerQuest.Awake is a fallback.
    if not built_in:
        inject(injector, target / managed / assembly, dll)
    return player_exe(target)


def inject(injector: Path, assembly: Path, dll: Path, *references: Path):
    pipeline.run([injector, assembly, 'LoadingScreen', 'OnEnable', dll, *references])
    pipeline.run([injector, assembly, 'PowerTools.Quest.PowerQuest', 'Awake', dll, *references])
    # Update methods report their time with -diag-time-scripts (DrifterDiag.ScriptTimer).
    for first_id, name in enumerate(
        [assembly.name, 'Assembly-CSharp-firstpass.dll', 'Recovered.PowerTools.dll', 'InControl.dll']
    ):
        target = assembly.with_name(name)
        if not target.exists():
            continue
        if first_id:  # player copies hard-link everything else to the real build
            data = target.read_bytes()
            target.unlink()
            target.write_bytes(data)
        pipeline.run([injector, target, '--time-methods', str(first_id * METHOD_IDS_PER_ASSEMBLY), dll, *references])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('name')
    parser.add_argument(
        '--seconds', type=float, default=10, help='seconds after the title room (or the restored save) to capture'
    )
    parser.add_argument('--fresh', action='store_true', help='rebuild the diagnostic player copies')
    parser.add_argument('--tag', default='main', help='separate set of player copies (one per parallel investigation)')
    parser.add_argument('--build', type=Path, default=PLAYERS['translated'][0], help='translated player folder')
    args, flags = parser.parse_known_args()
    players = dict(PLAYERS, translated=(args.build.resolve(), PLAYERS['translated'][1]))
    diag = DIAG / args.tag
    out = pipeline.ROOT / 'artifacts' / 'parity' / args.name
    dll, injector = None, None
    shots, runs = {}, []
    for name, (source, assembly) in players.items():
        exe = player_exe(diag / name)
        if exe is None or args.fresh:
            if dll is None:
                dll, injector = compile_diag(diag)
            exe = prepare(diag, name, source, assembly, dll, injector)
        run_dir = out / name
        if run_dir.exists():
            shutil.rmtree(run_dir)
        player_flags = ['-diag-out', str(run_dir), '-diag-dump-after', str(args.seconds), *flags]
        # The probe captures the frame itself at a game-state-relative time; the window
        # stays off-screen so the cursor cannot hover it.
        runs.append((exe, run_dir, player_flags))
        shots[name] = run_dir / 'frame.png'
    # Both players run at once; timing is relative to each game's own state.
    with ThreadPoolExecutor(len(runs)) as pool:
        for future in [
            pool.submit(capture.run, exe, run_dir, f'0:offscreen; 0:waitfile done.txt {DONE_SECONDS}', player_flags)
            for exe, run_dir, player_flags in runs
        ]:
            future.result()
    original, translated = (Image.open(shots[name]).convert('RGB') for name in ('original', 'translated'))
    side = Image.new('RGB', (original.width, original.height * 2))
    side.paste(original, (0, 0))
    side.paste(translated, (0, original.height))
    side.save(out / 'compare.png')
    print(f'mean abs difference {capture.mean_difference(original, translated):.1f}; {out / "compare.png"}')


if __name__ == '__main__':
    main()
