"""Build The Drifter for the PS Vita: set up, build the VPK, clean up.

    uv run build.py

Runs tools/setup.py --vita, which downloads what the build needs (Unity's files, without installing
Unity, and the PS Vita SDK), then tools/downgrade/pipeline.py all --platform vita, which makes
builds/drifter-2018.2.19f1-vita.vpk, then pipeline.py clean, which deletes the work files and the tools.
--keep skips the clean, for faster rebuilds. docs/build.md describes the tools and their other options.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'tools' / 'downgrade'))
import progress  # noqa: E402
from common import registry_env  # noqa: E402

RETRY = 'Fix what it says above and run uv run build.py again: it picks up where it stopped.'


def run(failed, script, *args):
    """Run one of the tools; stop with failed (what to tell the user) when it fails."""
    # What setup just set for the user wins over an older value this shell started with.
    env = dict(os.environ)
    for name in ('SCE_PSP2_SDK_DIR', 'DRIFTER_GAME'):
        value = registry_env(name, 'user')
        if value:
            env[name] = value
    code = subprocess.run([sys.executable, str(ROOT / script), *args], env=env).returncode
    if code:
        progress.fail(f'\nbuild.py: {failed} (exit code {code}).')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--game', metavar='FOLDER', help="The Drifter, when it isn't in Steam's default folder")
    parser.add_argument('--vita-sdk', metavar='PATH', help='your own PS Vita SDK, an archive or a folder')
    parser.add_argument(
        '--keep', action='store_true', help='keep the work files and the tools: a rebuild then takes about 5 minutes'
    )
    parser.add_argument(
        '--unity',
        action='store_true',
        help='build with the Unity editor instead: installs it, needs a Unity license, keeps its files',
    )
    args = parser.parse_args()
    game = ['--game', args.game] if args.game else []
    sdk = ['--vita-sdk', args.vita_sdk] if args.vita_sdk else []
    unity = ['--unity'] if args.unity else []
    run(f'setup stopped. {RETRY}', 'tools/setup.py', '--vita', '--then-build', *game, *sdk, *unity)
    run(f'the build failed. {RETRY}', 'tools/downgrade/pipeline.py', 'all', '--platform', 'vita', *game, *unity)
    vpk = ROOT / 'builds' / ('drifter-2018.2.19f1-vita' + ('-unity' if args.unity else '') + '.vpk')
    # In a process of its own: the build's holds some of the tools' files open until it ends.
    if not args.keep and not args.unity:
        clean = 'uv run tools/downgrade/pipeline.py clean'
        run(f'the VPK is ready ({vpk}), but cleaning up failed. Try again: {clean}', *clean.split()[2:])
    progress.done(f'\nDone. The VPK: {vpk}')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        progress.fail('\nStopped. Run uv run build.py again to continue.')
