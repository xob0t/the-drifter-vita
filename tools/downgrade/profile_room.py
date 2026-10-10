"""Profiles a room on the PS Vita: standing still, then walking with the left stick.

    uv run tools/downgrade/profile_room.py [--save 2] [--room Name] [--idle 15] [--walk 8] [--tag name]
                                           [--plan name:seconds,...] [probe flags...]

Needs the diag build installed (a development build fills the probe's profiler markers).
Loads the save (and moves to --room), waits until the room is up, then records the
probe's heartbeats (every 5 s) and vitacompanion's perf for each phase. Writes the raw
heartbeats per phase to artifacts/vita/profile-<tag>.txt. Needs the vita-device lock (lock.py).
"""

import argparse
import re
import sys
import time

import heartbeats
import pipeline
import sweep
import vita
import vita_sweep

ROOM_SECONDS = 240  # how long the save's room (or --room) gets to load
SETTLE_SECONDS = 10  # then the wait for loading to settle before the first phase
PERF_SECONDS = 2  # between vitacompanion perf readings
SHOWN_CHARACTERS = 1500  # of each heartbeat printed (the report keeps them whole)
# Plan phases: the vitacompanion commands that start and end each.
MOVES = {
    'idle': (None, None),
    'walk-right': ('press left-stick 255 128', 'release left-stick'),
    'walk-left': ('press left-stick 0 128', 'release left-stick'),
    'neutral': ('press left-stick 128 128', 'release left-stick'),
    'look-right': ('press right-stick 255 128', 'release right-stick'),
    'look-left': ('press right-stick 0 128', 'release right-stick'),
    'look-up': ('press right-stick 128 0', 'release right-stick'),
}


def log_lines():
    return vita.read(f'{vita.REMOTE}/log.txt').decode(errors='replace').splitlines()


def phase(name, seconds, start, stop, report):
    before = len(log_lines())
    if start:
        vita.command(start, timeout=5)
    loads = []
    end = time.time() + seconds
    while time.time() < end:
        perf = vita.command('perf', timeout=5)
        match = re.search(r'cpu_load: (.*)', perf)
        fps = re.search(r'fps: app=([\d.]+)', perf)
        loads.append(f'{fps.group(1) if fps else "?"} fps, cpu {match.group(1) if match else "?"}')
        time.sleep(PERF_SECONDS)
    if stop:
        vita.command(stop, timeout=5)
    beats = [line for line in log_lines()[before:] if 'heartbeat' in line]
    report.append(f'== {name}')
    report.extend('perf ' + load for load in loads)
    report.extend(beats)
    print(f'== {name}: ' + '; '.join(loads))
    for beat in beats:
        print(heartbeats.without_cams(beat)[:SHOWN_CHARACTERS])


def profile(args, extra):
    vita_sweep.upload_save(args.save)
    flags = ' '.join([*sweep.room_flags(args.save, args.room), '-diag-time-scripts', *extra])
    vita.command('quit all; wait 2s')
    vita.write(f'{vita.REMOTE}/args.txt', flags.encode())
    if not vita_sweep.launch(flags):
        sys.exit('the game did not start with the probe flags')
    # Wait for the save's room (or the goto) and let loading settle.
    deadline = time.time() + ROOM_SECONDS
    while time.time() < deadline:
        text = '\n'.join(log_lines())
        rooms = [scene for scene in heartbeats.SCENE_LINE.findall(text) if scene.startswith('SceneRoom')]
        if rooms and rooms[-1] != 'SceneRoomTitle' and (not args.room or rooms[-1] == f'SceneRoom{args.room}'):
            break
        time.sleep(5)
    else:
        sys.exit('the room never loaded')
    print('in', rooms[-1])
    time.sleep(SETTLE_SECONDS)
    report = [f'room {rooms[-1]}']
    plan = args.plan or f'idle:{args.idle},walk-right:{args.walk},walk-left:{args.walk},idle:{args.idle}'
    for step in plan.split(','):
        name, seconds = step.split(':')
        phase(name, float(seconds), *MOVES[name], report)
    vita.write(f'{vita.REMOTE}/args.txt', b'')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--save', type=int, default=2)
    parser.add_argument('--room')
    parser.add_argument('--idle', type=float, default=15)
    parser.add_argument('--walk', type=float, default=8)
    parser.add_argument('--tag', default='room')
    parser.add_argument(
        '--plan',
        help=f'phases as name:seconds,... ({", ".join(MOVES)}); default idle, walk right, walk left, idle',
    )
    args, extra = parser.parse_known_args()
    vita.keep_awake()
    with vita_sweep.saves_kept():
        report = profile(args, extra)
    out = pipeline.ROOT / 'artifacts' / 'vita' / f'profile-{args.tag}.txt'
    out.write_text('\n'.join(report) + '\n', encoding='utf-8')
    print('wrote', out)


if __name__ == '__main__':
    main()
