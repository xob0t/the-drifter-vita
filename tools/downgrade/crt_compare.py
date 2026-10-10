"""Compare the game's CRT filter with CRT/SinglePass on the same frame and input.

    uv run tools/downgrade/crt_compare.py [--frame 600] [--build dir] [--quiet-noise] [--generic]
                                          [--label name] [flags...]

Runs a translated Windows diag build (pipeline.py --diag; release Windows builds leave the
one-pass shader out) at the Vita's 960x544 with -crt-compare (DrifterCompat.Crt, a flag read
from the probe's flags only): on the filter's given frame (counted from its
first, in the title room) the single pass renders into a texture from the filter's input,
then the original passes draw as usual and the screen is read back. Writes original.png,
single.png, diff.png (differences x16) and input.png to artifacts/crt/ (prefixed with
--label) and prints how far apart they are, the single pass's mean bias per channel and the
fine-grain (noise) energy of both. --quiet-noise sets the filter's noise strengths to 0 in
both, so differences are the filter's deterministic part; the noise itself cannot match
pixel for pixel, as the single pass reads it from a texture. --generic forces the single
pass's pass 0 (every mode) instead of the fast passes for the game's modes.
"""

import argparse
import re
from pathlib import Path

import capture
import diag
import numpy as np
import pipeline
from capture import read_raw
from PIL import Image

OUT = pipeline.ROOT / 'artifacts' / 'crt'


def grain(image):
    """RMS of the difference to the 3x3 mean, in levels: the noise's (and edges') energy."""
    image = image.astype(float)
    padded = np.pad(image, ((1, 1), (1, 1), (0, 0)), mode='edge')
    mean = sum(padded[y : y + image.shape[0], x : x + image.shape[1]] for y in range(3) for x in range(3)) / 9
    return np.sqrt(((image - mean) ** 2).mean())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--frame', type=int, default=600)
    parser.add_argument(
        '--build', type=Path, default=diag.PLAYERS['translated'][0].with_name(f'drifter-{pipeline.TARGET}-diag')
    )
    parser.add_argument('--quiet-noise', action='store_true')
    parser.add_argument('--generic', action='store_true')
    parser.add_argument('--label', default='')
    args, flags = parser.parse_known_args()
    work = pipeline.WORK / 'diag' / 'crt'
    dll, injector = diag.compile_diag(work)
    exe = diag.prepare(work, 'translated', args.build, diag.PLAYERS['translated'][1], dll, injector)
    run_dir = OUT / 'run'
    for stale in run_dir.glob('*'):
        stale.unlink()
    noise = (
        ['-diag-set', 'CRTEffect.colorNoiseStrength=0', '-diag-set', 'CRTEffect.whiteNoiseStrength=0']
        if args.quiet_noise
        else []
    )
    generic = ['-crt-generic'] if args.generic else []
    # The probe ends the run after its dump; dump well after the compared frame.
    capture.run(
        exe,
        run_dir,
        f'0:offscreen; 0:waitfile done.txt {diag.DONE_SECONDS}',
        [
            '-diag-out',
            str(run_dir),
            '-diag-dump-after',
            '30',
            '-crt-compare',
            str(run_dir),
            str(args.frame),
            # The filter must draw whatever the game's saved setting says.
            '-diag-crt',
            'on',
            *noise,
            *generic,
            *flags,
        ],
    )
    original, single = read_raw(run_dir / 'crt-original.raw'), read_raw(run_dir / 'crt-single.raw')
    prefix = args.label + '-' if args.label else ''
    if (run_dir / 'crt-input.raw').exists():
        Image.fromarray(read_raw(run_dir / 'crt-input.raw')).save(OUT / f'{prefix}input.png')
    difference = np.abs(original.astype(int) - single.astype(int))
    Image.fromarray(original).save(OUT / f'{prefix}original.png')
    Image.fromarray(single).save(OUT / f'{prefix}single.png')
    Image.fromarray(np.clip(difference * 16, 0, 255).astype(np.uint8)).save(OUT / f'{prefix}diff.png')
    passes = re.findall(r'CRT single pass: (.*)', (run_dir / 'player.log').read_text(errors='replace'))
    largest = difference.max(axis=2)
    bias = (single.astype(float) - original).mean(axis=(0, 1))
    print(f'pass: {passes[-1] if passes else "?"}')
    print(
        f'{original.shape[1]}x{original.shape[0]}: mean difference {difference.mean():.3f} levels, '
        f'largest {largest.max()}; pixels off by more than 1 level {(largest > 1).mean() * 100:.2f}%, '
        f'more than 4 {(largest > 4).mean() * 100:.2f}%, more than 16 {(largest > 16).mean() * 100:.2f}%'
    )
    print(
        f'bias (single - original) R {bias[0]:+.3f} G {bias[1]:+.3f} B {bias[2]:+.3f}; '
        f'grain original {grain(original):.3f}, single {grain(single):.3f}'
    )


if __name__ == '__main__':
    main()
