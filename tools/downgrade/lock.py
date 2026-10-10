"""Take, release or hold a lock in work/ atomically, so jobs that run at the same time (builds, console runs)
don't collide.

    uv run tools/downgrade/lock.py take <name> "<holder: purpose>" [--wait SECONDS]  # exit 0 when taken, 1 on timeout
    uv run tools/downgrade/lock.py release <name> "<holder>"                         # removes it only if you hold it
    uv run tools/downgrade/lock.py run <name> "<holder: purpose>" [--wait SECONDS] -- <command...>
    uv run tools/downgrade/lock.py status

<name> is vita-device, vita-build or pc-build. Taking creates work/<name>.lock with O_EXCL, so the
check and the write are one step and two jobs can never both get it. Release checks that the
file still names you before removing it.

Only one pipeline.py runs at a time, whatever the platform: a build lock (vita-build, pc-build)
counts as taken only while the other one is free; if the other appears right after, ours is
released again and the wait goes on.

`run` takes the lock, runs the command and releases the lock however the command ends (also on
Ctrl+C); it exits with the command's code, or 75 if the lock was not taken in time. For example:

    uv run tools/downgrade/lock.py run pc-build "user: mono build" --wait 3600 -- \\
        bash -c "uv run tools/downgrade/pipeline.py project && uv run tools/downgrade/pipeline.py build"

A person at the console holds vita-device under the name "user" ("user: <purpose>", any case);
jobs use their own names. keep_awake.py never wakes the console while a person holds it.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

WORK = Path(__file__).resolve().parents[2] / 'work'
LOCKS = ('vita-device', 'vita-build', 'pc-build')
BUILD_LOCKS = ('vita-build', 'pc-build')
NOT_TAKEN = 75  # exit code of `run` when the lock stayed held (EX_TEMPFAIL)
POLL_SECONDS = 5
PERSON = 'user'  # the holder name of a person, as opposed to a job


def path(name):
    if name not in LOCKS:
        sys.exit(f'unknown lock {name!r}; one of {", ".join(LOCKS)}')
    return WORK / f'{name}.lock'


def holder_of(name):
    try:
        return path(name).read_text(errors='replace').strip()
    except FileNotFoundError:
        return None


def take(name, holder, wait):
    lock = path(name)
    others = [other for other in BUILD_LOCKS if other != name] if name in BUILD_LOCKS else []
    end = time.time() + wait
    while True:
        busy = next((other for other in others if path(other).exists()), None)
        if busy is None:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                busy = name
            else:
                os.write(fd, f'{holder} {time.strftime("%H:%M")}\n'.encode())
                os.close(fd)
                busy = next((other for other in others if path(other).exists()), None)
                if busy is None:
                    return 0
                lock.unlink()  # the other build lock was taken in between: one pipeline at a time
        if time.time() >= end:
            print(f'{busy} held by: {holder_of(busy)}')
            return 1
        time.sleep(POLL_SECONDS)


def owner(text):
    """The name before the purpose in a holder ("me: purpose") or a lock's text ("me: purpose 16:09")."""
    return re.sub(r' \d{1,2}:\d\d$', '', text.strip()).split(':')[0].strip()


def is_person(text):
    """Whether a holder or a lock's text names a person (PERSON), not a job."""
    return owner(text).lower() == PERSON


def release(name, holder):
    text = holder_of(name)
    if text is None:
        return 0
    # The whole name must match: "a" must not release "ab: ..." (or "" anyone's lock).
    if not owner(holder) or owner(text) != owner(holder):
        print(f'{name} is held by someone else: {text}')
        return 1
    path(name).unlink()
    return 0


def run(name, holder, wait, command):
    if not command:
        sys.exit('run: no command after --')
    if take(name, holder, wait) != 0:
        return NOT_TAKEN
    try:
        return subprocess.run(command).returncode
    except KeyboardInterrupt:
        return 130
    finally:
        release(name, holder)


def main(argv):
    command = argv[argv.index('--') + 1 :] if '--' in argv else []
    argv = argv[: argv.index('--')] if '--' in argv else argv
    parser = argparse.ArgumentParser(
        prog='lock.py', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('action', choices=['take', 'release', 'run', 'status'])
    parser.add_argument('name', nargs='?', choices=LOCKS)
    parser.add_argument('holder', nargs='?', help='"<name>: <purpose>"; release needs only the name')
    parser.add_argument('--wait', type=float, default=0, metavar='SECONDS', help='take, run: how long to wait')
    args = parser.parse_args(argv)
    if args.action == 'status':
        for name in LOCKS:
            print(f'{name:12} {holder_of(name) or "free"}')
        return 0
    if args.holder is None:
        parser.error(f'{args.action} needs a lock name and a holder')
    if args.action == 'take':
        return take(args.name, args.holder, args.wait)
    if args.action == 'release':
        return release(args.name, args.holder)
    return run(args.name, args.holder, args.wait, command)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
