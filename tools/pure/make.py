"""Build the PS Vita player from the translated project without Unity, in one go.

    uv run tools/pure/make.py [--out dir] [--vpk]

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


def make(out):
    start = time.time()
    package.vita_folder(out)
    data.write_all(data.Build(out, platform='vita'))
    il2cpp.build(out)
    print(f'built {out} in {time.time() - start:.0f} s')


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
