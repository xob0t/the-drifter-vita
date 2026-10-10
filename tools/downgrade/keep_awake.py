"""Keeps the PS Vita awake with vita.py's keep-awake chain ('nosleep on; screen on' every 10 s),
but only while there is work for it: a step of work/vita_steps.txt not yet in work/vita_steps.done,
or vita-device held by a job. Never while a person holds vita-device (lock.py's "user: <purpose>"):
'screen on' would wake a console they put to sleep.

    uv run tools/downgrade/keep_awake.py [seconds]
"""

import argparse
import contextlib
import sys
import time

import lock
import pipeline
import vita

WORK = pipeline.WORK
DEFAULT_SECONDS = 7000


def wanted():
    holder = lock.holder_of('vita-device')
    if holder is not None:
        return not lock.is_person(holder)
    steps = WORK / 'vita_steps.txt'
    done = WORK / 'vita_steps.done'
    names = (
        {line.split('|', 1)[0] for line in steps.read_text(errors='replace').splitlines() if line.strip()}
        if steps.exists()
        else set()
    )
    finished = set(done.read_text(errors='replace').split()) if done.exists() else set()
    return bool(names - finished)


def single_instance():
    """Holds a lock on work/keep_awake.instance while this runs: a second keep-awake loop would only
    double the console's commands, so it exits. Windows releases the lock when the process ends."""
    import msvcrt

    handle = open(WORK / 'keep_awake.instance', 'a+')  # noqa: SIM115 - held open while the loop runs: it is the lock
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        sys.exit('another keep_awake.py is already running')
    return handle


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('seconds', type=float, nargs='?', default=DEFAULT_SECONDS, help='how long to run')
    end = time.time() + parser.parse_args().seconds
    instance = single_instance()  # noqa: F841 - the lock lasts while the handle is open
    while time.time() < end:
        with contextlib.suppress(OSError):  # a step file or lock that changed while being read
            if wanted():
                vita.nudge()
        time.sleep(vita.KEEP_AWAKE_SECONDS)
    print('keep-awake ended', time.strftime('%H:%M'))


if __name__ == '__main__':
    main()
