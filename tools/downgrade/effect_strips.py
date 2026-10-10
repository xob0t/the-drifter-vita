"""Side-by-side frame strips and statistics of a room's effects: original against translated.

    uv run tools/downgrade/effect_strips.py <Room> [--frames 180] [--every 18] [--delay 3] [--tag effects]
                                            [--build dir] [--fresh] [--name out] [--skip-run] [-diag-* flags]

Runs diag.py (both players, save 2, -diag-goto-room <Room>) with the probe's frame log, which
saves every n-th of <frames> frames from <delay> s after the room's scene became active, then
writes artifacts/effects/<name>/:
  strip.png         original (top row) and translated (bottom row), every saved frame
  strip-crop.png    the same, 2x, on the window region with the most frame-to-frame change
  stats.txt         per player: mean luminance, mean frame-to-frame difference (motion energy,
                    which the rain and bees dominate) and its spread, over all logged frames
Rain and bees are random, so the frames are compared as pictures and statistics, not pixel by
pixel; tools/downgrade/diag.py with -diag-check-effects compares the translated build's two ways
of drawing the same frame exactly.
"""

import argparse
import subprocess
import sys
from pathlib import Path

import diag
import numpy as np
import pipeline
import sweep
from capture import read_raw
from PIL import Image

CROP = (240, 136)  # the busiest region's size in window pixels, shown at 2x
CROP_STEP = (30, 17)  # how far apart the regions tried are
CROP_COLUMNS = 6  # frames per row of strip-crop.png


def frame_log(run_dir: Path, scene: str):
    """Per logged frame [mean, diff, maxRegionDiff] from framelog-<scene>.txt (no rows without it)."""
    rows = []
    path = run_dir / f'framelog-{scene}.txt'
    if not path.exists():
        return np.zeros((0, 3))
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.startswith('#'):
            continue
        parts = line.split()
        rows.append([float(parts[4]), float(parts[5]), float(parts[6])])
    return np.array(rows)


def player_frames(run_dir: Path, scene: str, player: str):
    """The player's saved frames, in order, and its statistics line."""
    frames = sorted(run_dir.glob(f'framelog-{scene}-*.raw'), key=lambda p: int(p.stem.rsplit('-', 1)[1]))
    log = frame_log(run_dir, scene)
    if not len(log):
        return [read_raw(p) for p in frames], f'{player}: no frame log'
    motion = log[1:, 1]
    stats = (
        f'{player}: frames {len(log)} mean luminance {log[:, 0].mean():.2f} '
        f'motion (mean frame-to-frame difference) {motion.mean():.3f} sd {motion.std():.3f} '
        f'p10 {np.percentile(motion, 10):.3f} p90 {np.percentile(motion, 90):.3f}'
    )
    return [read_raw(p) for p in frames], stats


def save_strip(rows, count, path: Path):
    """Every saved frame at half size, one row per player."""
    h, w = rows[0][0].shape[:2]
    strip = Image.new('RGB', (w // 2 * count, h // 2 * 2))
    for r, frames in enumerate(rows):
        for c, frame in enumerate(frames[:count]):
            strip.paste(Image.fromarray(frame).resize((w // 2, h // 2), Image.LANCZOS), (c * (w // 2), r * (h // 2)))
    strip.save(path)


def busiest_region(rows, count):
    """(x, y) of the CROP region with the largest mean change between saved frames, both players."""
    h, w = rows[0][0].shape[:2]
    cw, ch = CROP
    change = sum(np.abs(np.diff(np.stack(f[:count]).astype(float), axis=0)).mean(axis=(0, 3)) for f in rows)
    best, at = -1, (0, 0)
    for y in range(0, h - ch + 1, CROP_STEP[1]):
        for x in range(0, w - cw + 1, CROP_STEP[0]):
            score = change[y : y + ch, x : x + cw].mean()
            if score > best:
                best, at = score, (x, y)
    return at


def save_crop(rows, count, at, path: Path):
    """The first CROP_COLUMNS frames of each player cut to the region at `at`, at 2x."""
    cw, ch = CROP
    columns = min(count, CROP_COLUMNS)
    crop = Image.new('RGB', (cw * 2 * columns, ch * 2 * 2))
    for r, frames in enumerate(rows):
        for c, frame in enumerate(frames[:columns]):
            piece = Image.fromarray(frame[at[1] : at[1] + ch, at[0] : at[0] + cw]).resize(
                (cw * 2, ch * 2), Image.NEAREST
            )
            crop.paste(piece, (c * cw * 2, r * ch * 2))
    crop.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('room')
    parser.add_argument('--frames', type=int, default=180)
    parser.add_argument('--every', type=int, default=18)
    parser.add_argument('--delay', type=float, default=3)
    parser.add_argument('--tag', default='effects')
    parser.add_argument('--build', type=Path)
    parser.add_argument('--fresh', action='store_true')
    parser.add_argument('--name')
    parser.add_argument('--skip-run', action='store_true', help='only redraw from an earlier run')
    args, flags = parser.parse_known_args()
    name = args.name or args.room
    scene = f'SceneRoom{args.room}'
    parity = pipeline.ROOT / 'artifacts' / 'parity' / f'effects-{name}'
    out = pipeline.ROOT / 'artifacts' / 'effects' / name
    out.mkdir(parents=True, exist_ok=True)
    if not args.skip_run:
        logged = ['-diag-frame-log', scene, str(args.frames), str(args.delay), str(args.every)]
        seconds = args.delay + args.frames / 30 + 15
        command = diag.command(
            f'effects-{name}',
            args.tag,
            args.build,
            seconds,
            [*sweep.room_flags(2, args.room), *logged, *flags],
            args.fresh,
        )
        subprocess.run(command, check=True, cwd=pipeline.ROOT)
    rows, stats = zip(
        *(player_frames(parity / player, scene, player) for player in ('original', 'translated')), strict=True
    )
    count = min(len(r) for r in rows)
    if count == 0:
        sys.exit('no frames saved: ' + '; '.join(stats))
    save_strip(rows, count, out / 'strip.png')
    at = busiest_region(rows, count)
    save_crop(rows, count, at, out / 'strip-crop.png')
    cw, ch = CROP
    text = '\n'.join(
        [
            f'room {args.room}: {count} frames per row (every {args.every} of {args.frames} frames '
            f'from {args.delay} s); top original, bottom translated',
            f'crop at {at} ({cw}x{ch} window pixels, 2x)',
            *stats,
        ]
    )
    (out / 'stats.txt').write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
