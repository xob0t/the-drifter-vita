"""Visit every room on the PS Vita and record how it runs.

    uv run tools/downgrade/vita_sweep.py [--save 2] [--seconds 8] [--timeout 300] [--reference tag]
                                         [rooms...] [probe flags...]

Needs an installed diag build (pipeline.py --platform vita --diag, vita.py run).
For each room the probe restores the save, moves to the room with PowerQuest's own
room change and dumps its loaded textures; this script then takes a screenshot,
reads the probe's heartbeats and checks for crash dumps. Results go to
artifacts/vita/rooms.tsv and artifacts/vita/rooms/<room>.png, with the mean
absolute difference against the PC original's frame of the same room
(artifacts/parity/<--reference>/room-<room>/original/frame.png, from sweep.py) when
that exists. The save is uploaded to the folder the game reads saves from.
"""

import argparse
import contextlib
import ftplib
import io
import re
import sys
import time

import capture
import heartbeats
import pipeline
import sweep
import vita
from PIL import Image

OUT = pipeline.ROOT / 'artifacts' / 'vita'
# -diag-dump-after counts from the save restore, which takes about a minute on the Vita
# before the room change can start.
RESTORE_SECONDS = 70


SAVE_FOLDER = pipeline.WORK / 'vita-save-folder.txt'


def save_folder():
    """The game's save folder on the console, from the probe's startup line. The last one seen
    is remembered, since log.txt is empty or partial while a run starts or after one was cut off."""
    log = vita.read(f'{vita.REMOTE}/log.txt').decode(errors='replace')
    match = heartbeats.SAVES.search(log)
    if match:
        SAVE_FOLDER.write_text(match.group(1).rstrip('/'))
    elif not SAVE_FOLDER.exists():
        raise RuntimeError('the probe did not report the save folder; launch the diag build once first')
    return SAVE_FOLDER.read_text().strip()


def upload_save(slot):
    folder = re.sub(r'^(\w+:)/*', r'\1/', save_folder())  # the FTP server wants device:/path
    data = (pipeline.DEFAULT_GAME / f'Save{slot}.sav').read_bytes()
    with vita.ftp() as connection:
        vita.makedirs(connection, folder, set())
        connection.storbinary(f'STOR {folder}/Save{slot}.sav', io.BytesIO(data))
    pipeline.log(f'Uploaded Save{slot}.sav to {folder}')


def newest_dump():
    with vita.ftp() as connection:
        dumps = vita.crash_dumps(connection)
    return dumps[-1] if dumps else None


def remove(names):
    with vita.ftp() as connection:
        for name in names:
            with contextlib.suppress(ftplib.error_perm):
                connection.delete(f'{vita.REMOTE}/{name}')


def exists(name):
    with vita.ftp() as connection:
        return any(n.rsplit('/', 1)[-1] == name for n in connection.nlst(vita.REMOTE))


def launch(flags, attempts=3):
    """Launches the game and checks from the probe's log that it started with these flags;
    a launch right after quitting can be ignored while the previous run is still closing.
    Each failed attempt quits the game and says why; False after the last one."""
    for attempt in range(1, attempts + 1):
        time.sleep(5)
        vita.command(f'{vita.KEEP_AWAKE}; launch {pipeline.VITA_TITLE_ID}')
        time.sleep(15)
        try:
            # The probe writes log.txt anew at each start, with its args line near the top. The
            # whole line is compared: a long one (a -vita-press schedule) once ran past the 600
            # characters an older check read, which quit the game at every launch.
            log = vita.read(f'{vita.REMOTE}/log.txt').decode(errors='replace')
        except vita.FTP_ERRORS as error:
            log, reason = '', f'log.txt not read ({error!r})'
        else:
            found = heartbeats.ARGS_LINE.search(log)
            reason = 'no args line in log.txt' if not found else f'args line differs: {found.group(1)[:200]}'
        if f'] args {flags}\n' in log:
            return True
        pipeline.log(f'launch attempt {attempt} of {attempts}: {reason}; quitting the game')
        vita.command('quit all; wait 3s')
    return False


def run(flags, stale, timeout):
    """Starts the game with these probe flags (after removing the stale outputs of a run) and
    waits for the probe's done.txt: 'done', 'crash' (a new crash dump), 'timeout' or
    'launch-failed'."""
    vita.command('quit all; wait 2s')
    remove(['done.txt', *stale])
    vita.write(f'{vita.REMOTE}/args.txt', flags.encode())
    before = newest_dump()
    if not launch(flags):
        return 'launch-failed'
    deadline, unanswered = time.time() + timeout, 0
    while time.time() < deadline:
        time.sleep(5)
        try:
            if exists('done.txt'):
                return 'done'
            if newest_dump() != before:
                return 'crash'
            unanswered = 0
        except vita.FTP_ERRORS as error:
            # The console drops off Wi-Fi now and then; keep polling until the deadline.
            unanswered += 1
            if unanswered in (1, 12):
                print(f'FTP not answering ({error!r}); still waiting')
    return 'timeout'


def visit(room, args):
    flags = ' '.join(
        [
            *sweep.room_flags(args.save, room),
            '-diag-dump-after',
            str(args.seconds + RESTORE_SECONDS),
            '-diag-dump-textures',
            '-diag-time-scripts',
            '-diag-time-loop',
            *args.flags,
        ]
    )
    status = run(flags, ['textures.tsv', 'screenshot.raw'], args.timeout)
    result = {'room': room, 'status': status}
    try:
        record(room, args, status, result)
    except vita.FTP_ERRORS as error:
        # The console drops off Wi-Fi now and then; the next room tries again.
        result['status'] += f' (FTP: {error!r})'
    return result


def record(room, args, status, result):
    """Adds the room's screenshot, texture total, heartbeat figures and log to result."""
    if status == 'done':
        shot = OUT / 'rooms' / f'{room}.png'
        vita.screenshot(shot)
        reference = pipeline.ROOT / 'artifacts' / 'parity' / args.reference / f'room-{room}' / 'original' / 'frame.png'
        if reference.exists():
            result['difference'] = f'{capture.mean_difference(Image.open(reference), Image.open(shot)):.1f}'
        try:
            total = vita.read(f'{vita.REMOTE}/textures.tsv').decode().strip().splitlines()[-1].split('\t')[-1]
            result['textureMB'] = f'{int(total) / 2**20:.0f}'
        except (*vita.FTP_ERRORS, UnicodeDecodeError, ValueError, IndexError) as error:
            print(f'{room}: no texture total ({error!r})')
    log = vita.read(f'{vita.REMOTE}/log.txt').decode(errors='replace')
    (OUT / 'rooms').mkdir(parents=True, exist_ok=True)
    (OUT / 'rooms' / f'{room}.log').write_text(log, encoding='utf-8')
    figures = room_figures(log, room)
    result.update(figures)
    if not figures:
        result['status'] += f' (no heartbeats in SceneRoom{room})'
    if 'Could not allocate memory' in log:
        result['status'] += '+oom'


def room_figures(log, room):
    """fps, renderMs and otherMs (the median heartbeat's) and the least free memory of the
    heartbeats in the room; {} without any."""
    in_room = [b for b in heartbeats.heartbeats(log) if b.get('scene') == f'SceneRoom{room}']
    if not in_room:
        return {}
    # The last heartbeat covers the probe's own dump and screenshot (stalls of 400-750 ms), so the
    # steady rate is the median of the ones before it.
    steady = sorted(in_room[:-1] or in_room, key=lambda b: b.get('fps', 0))
    middle = steady[len(steady) // 2]
    figures = {key: f'{middle[key]:.1f}' for key in ('fps', 'renderMs', 'otherMs') if key in middle}
    for key, label in (('freeLPDDR', 'minLPDDR'), ('freeCDRAM', 'minCDRAM')):
        free = [b[key] for b in in_room if key in b]
        if free:
            figures[label] = min(free)
    return figures


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('rooms', nargs='*')
    parser.add_argument('--save', type=int, default=2)
    parser.add_argument('--seconds', type=float, default=8, help='seconds in the room before the dump')
    parser.add_argument('--timeout', type=float, default=300, help='seconds to wait for each room')
    parser.add_argument(
        '--reference', default='il2cpp19', help='sweep.py tag under artifacts/parity/ with the PC frames'
    )
    # Extra probe or Compat flags (e.g. -compat-crt-mode off) come last, as in sweep.py.
    argv = sys.argv[1:]
    cut = next((i for i, arg in enumerate(argv) if sweep.PLAYER_FLAG.match(arg)), len(argv))
    args = parser.parse_args(argv[:cut])
    args.flags = argv[cut:]
    rooms = args.rooms or sweep.rooms_from_build_settings()
    vita.keep_awake()
    upload_save(args.save)
    columns = ['room', 'status', 'difference', 'fps', 'renderMs', 'otherMs', 'minLPDDR', 'minCDRAM', 'textureMB']
    table = OUT / 'rooms.tsv'
    done = {}
    if table.exists():
        for line in table.read_text(encoding='utf-8').splitlines()[1:]:
            done[line.split('\t')[0]] = line
    for room in rooms:
        result = visit(room, args)
        done[room] = '\t'.join(str(result.get(c, '')) for c in columns)
        pipeline.log(done[room])
        table.write_text(
            '\t'.join(columns) + '\n' + ''.join(v + '\n' for _, v in sorted(done.items())), encoding='utf-8'
        )
    vita.write(f'{vita.REMOTE}/args.txt', b'')


if __name__ == '__main__':
    main()
