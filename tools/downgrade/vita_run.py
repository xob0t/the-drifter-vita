"""Runs the installed diag build on the PS Vita once with the given probe flags and fetches
its logs.

    uv run tools/downgrade/vita_run.py <tag> [--save 2] [--no-upload] [--timeout 400] [--keep-intro] -- <probe flags...>

The flags should include -diag-dump-after so the run ends (done.txt). -diag-skip-intro is
added unless --keep-intro is given. Writes log.txt, events.txt and a screenshot to
artifacts/vita/runs/<tag>/. Needs the vita-device lock (lock.py); the Vita queue
(vita_steps.sh) holds it for its steps.

The last line says how the run ended: done, crash (a new crash dump), timeout, or
launch-failed (the probe's log never showed these flags, so the game was not left running);
the exit code is 0 only for done.
"""

import argparse
import sys

import pipeline
import vita
import vita_sweep


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('tag')
    parser.add_argument('--save', type=int, default=2)
    parser.add_argument('--timeout', type=float, default=400)
    parser.add_argument('--no-upload', action='store_true', help="keep the console's own save")
    parser.add_argument(
        '--keep-intro', action='store_true', help='play the logos and title intro (no -diag-skip-intro)'
    )
    parser.add_argument('flags', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    flags = [f for f in args.flags if f != '--']
    if not args.keep_intro and '-diag-skip-intro' not in flags:
        flags.insert(0, '-diag-skip-intro')
    flags = ' '.join(flags)
    out = pipeline.ROOT / 'artifacts' / 'vita' / 'runs' / args.tag
    out.mkdir(parents=True, exist_ok=True)
    vita.keep_awake()
    if not args.no_upload:
        vita_sweep.upload_save(args.save)
    status = vita_sweep.run(flags, ['events.txt', 'screenshot.raw'], args.timeout)
    if status == 'done':
        vita.screenshot(out / 'screen.png')
    for name in ['log.txt', 'events.txt']:
        try:
            (out / name).write_bytes(vita.read(f'{vita.REMOTE}/{name}'))
        except vita.FTP_ERRORS as error:
            print(f'{name}: {error}')
    vita.write(f'{vita.REMOTE}/args.txt', b'')
    print(f'{args.tag}: {status}; {out}')
    return 0 if status == 'done' else 1


if __name__ == '__main__':
    sys.exit(main())
