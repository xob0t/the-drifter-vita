"""Compare every room of the original and translated players.

    uv run tools/downgrade/sweep.py [--save 2] [--seconds 8] [--tag sweep] [--build dir] [--table rooms.tsv]
        [rooms...] [-diag-* flags]

Loads the save in both players, moves to each room with PowerQuest's own room
change (diag.py -diag-goto-room), captures a frame and writes the per-room mean
absolute difference to artifacts/parity/rooms.tsv. Rooms default to every
SceneRoom* in the translated project's build settings. The tag names the run: its
player copies (work/diag/<tag>) and its captures (artifacts/parity/<tag>/room-<room>).
The player flags come last, after the rooms and options.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import diag
import pipeline

OUTLIER = 5.0  # mean abs difference worth a second capture; the CRT noise floor is ~2.6
PLAYER_FLAG = re.compile(r'-\w+-')  # -diag-*, -compat-*, -crt-*, -vita-*, ...


def rooms_from_build_settings():
    settings = pipeline.WORK / f'drifter-{pipeline.TARGET}' / 'ProjectSettings' / 'EditorBuildSettings.asset'
    return re.findall(r'SceneRoom(\w+)\.unity', settings.read_text(encoding='utf-8'))


def room_flags(save, room=None):
    """The probe flags that restore a save, skipping the intro, and go to a room if one is given."""
    flags = ['-diag-fast-intro', '20', '-diag-skip-intro', '-diag-load-save', f'{save}@1']
    return flags + (['-diag-goto-room', room, '-diag-goto-delay', '2'] if room else [])


def compare(room, args, fresh):
    flags = [*room_flags(args.save, room), *args.flags]
    for attempt in range(2):
        command = diag.command(f'{args.tag}/room-{room}', args.tag, args.build, args.seconds, flags, fresh)
        result = subprocess.run(command, capture_output=True, text=True, cwd=pipeline.ROOT)
        match = re.search(r'mean abs difference ([0-9.]+)', result.stdout)
        if match:
            return float(match.group(1))
        # Say why, rather than only "failed" in the table: the end of diag.py's output.
        tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        pipeline.log(f'{room}: attempt {attempt + 1} failed (exit {result.returncode}): ' + ' | '.join(tail))
        fresh = False
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('rooms', nargs='*')
    parser.add_argument('--save', type=int, default=2)
    parser.add_argument('--seconds', type=float, default=8)
    parser.add_argument('--tag', default='sweep', help='the run: player copies and captures (see above)')
    parser.add_argument('--build', type=Path, help='translated player folder (default: the Mono build)')
    parser.add_argument('--table', default='rooms.tsv', help='results file under artifacts/parity/')
    # The player flags (e.g. -diag-dump-textures) are cut off first, so their values are not taken for rooms.
    argv = sys.argv[1:]
    cut = next((i for i, arg in enumerate(argv) if PLAYER_FLAG.match(arg)), len(argv))
    args = parser.parse_args(argv[:cut])
    args.flags = argv[cut:]
    rooms = args.rooms or rooms_from_build_settings()
    table = pipeline.ROOT / 'artifacts' / 'parity' / args.table
    table.parent.mkdir(parents=True, exist_ok=True)
    done = {}
    if table.exists():
        for line in table.read_text(encoding='utf-8').splitlines()[1:]:
            room, value = line.split('\t')
            done[room] = value
    for index, room in enumerate(rooms):
        difference = compare(room, args, fresh=index == 0)
        # One capture can land on a hitch mid-transition; recheck outliers once.
        if difference is not None and difference > OUTLIER:
            again = compare(room, args, fresh=False)
            if again is not None:
                difference = min(difference, again)
        done[room] = 'failed' if difference is None else f'{difference:.1f}'
        pipeline.log(f'{room}: {done[room]}')
        table.write_text(
            'room\tmean_abs_difference\n' + ''.join(f'{r}\t{v}\n' for r, v in sorted(done.items())), encoding='utf-8'
        )


if __name__ == '__main__':
    main()
