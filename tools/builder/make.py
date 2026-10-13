"""Build the PS Vita player from the translated project without Unity, in one go.

    uv run tools/builder/make.py [--out dir] [--vpk]

Lays out the build folder (package.py), writes the serialized data and streams (build.py), builds
the game code module (il2cpp.py), and with --vpk packages it like the pipeline's `package` stage.
"""

import argparse
import sys
import time
from pathlib import Path

import build as data
import il2cpp
import package
import paths
from paths import progress


def make(out):
    start = time.time()
    with progress.display():
        with progress.step('Preparing the build folder'):
            paths.builtin_shaders()
            package.vita_folder(out)
        with progress.step('Reading the project'):  # its asset index: about 3 minutes the first time
            build = data.Build(out, platform='vita')
        data.write_all(build)
        with progress.step('Code module'):
            times = il2cpp.build(out)
    if times['reused']:
        progress.console.print('Code module: reused, its inputs have not changed')
    else:
        steps = ', '.join(f'{k} {times[k]:.0f} s' for k in ('unitylinker', 'il2cpp'))
        progress.console.print(f'Code module: {steps}')
    progress.done(f'Built {out} in {time.time() - start:.0f} s')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, default=paths.OUT)
    parser.add_argument('--vpk', action='store_true', help='also package it as a VPK beside the folder')
    args = parser.parse_args()
    make(args.out)
    if args.vpk:
        sys.path.insert(0, str(paths.ROOT / 'tools' / 'downgrade'))
        import pipeline

        pipeline.package_vita(args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
