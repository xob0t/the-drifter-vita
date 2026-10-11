"""Drive the PS Vita build remotely: install updates, launch, screenshots, logs.

    uv run tools/downgrade/vita.py <command> [options]

The console is at DRIFTER_VITA_HOST. sync, log, args and crashes need only an FTP server
on port 1337: VitaShell's (press Select in VitaShell) or vitacompanion's; seed needs no
console. The other commands need vitacompanion's command server on port 1338; install, perf
and shot use commands the released vitacompanion lacks (install, perf, screenshot) and stop
with a message without them. The app must have been installed once from the VPK (pipeline.py
deploy, then VitaShell); after that:

  sync [--build dir]    upload the files that changed since the last sync into
                        ux0:app/<title> (the first sync uploads everything)
  seed --build dir      record that build as installed (e.g. from its VPK) without
                        uploading, so the next sync sends only differences
  install [--build dir] package the build as a VPK and stream it to the installer (also
                        what applies param.sfo changes such as the memory expansion)
  launch / quit         start the game, or quit all apps
  run                   quit, sync, launch
  do "<chain>"          run a vitacompanion command chain, e.g. synthetic input:
                        do "press up; wait 100ms; release up"
  shot [out.png]        system screenshot of whatever is displayed (any build)
  reboot                reboot the console
  perf                  clocks, CPU load per core, frame rate and battery (vitacompanion)
  screenshot [out.png]  ask the probe for a screenshot of the game's frame (diag builds only)
  log [--tail N]        print the probe's log.txt (diag builds only)
  args [flags...]       set the probe's -diag-* flags for the next launch (none clears)
  crashes [--pull]      list crash dumps in ux0:data, newest last; --pull fetches the
                        newest and prints the crashed thread's symbolized call chain
                        (needs the SDK's psp2bin and the build's SymbolFiles)

Probe screenshots and logs come from the parity probe built into the player
(pipeline.py --platform vita --diag); it writes them to ux0:data/drifter.
vitacompanion 1.20 or later (install, press/release, screenshot) is needed for
install, do and shot; 1.33 for perf.
"""

import argparse
import contextlib
import ftplib
import hashlib
import io
import json
import socket
import struct
import sys
import time
from pathlib import Path

import pipeline

REMOTE = 'ux0:/data/drifter'  # the probe's remote directory (diag/Remote.cs VitaRemoteDir)
APP = f'ux0:/app/{pipeline.VITA_TITLE_ID}'
# What a dropped or stalled FTP session raises (TimeoutError is an OSError).
FTP_ERRORS = (OSError, EOFError, ftplib.Error)
# The vitacompanion chain that keeps the console awake, and how often it is repeated (keep_awake).
KEEP_AWAKE = 'nosleep on; screen on'
KEEP_AWAKE_SECONDS = 10
SCREENSHOT_SECONDS = 20  # how long the probe gets to write a requested screenshot


class Unreachable(OSError):
    """The console's FTP or command server does not answer (main prints the message)."""


def ftp():
    pipeline.vita_host()
    connection = ftplib.FTP()
    try:
        connection.connect(*pipeline.VITA_FTP, timeout=30)
    except OSError as error:
        raise Unreachable(
            f"No FTP server at {pipeline.VITA_HOST}:1337 ({error}). Start VitaShell's FTP "
            f'server (press Select in VitaShell), or run vitacompanion.'
        ) from error
    connection.login()
    return connection


def command(chain, timeout=10):
    """Runs a vitacompanion command chain ("quit all; wait 2s; launch ...") and returns its output."""
    pipeline.vita_host()
    try:
        connection = socket.create_connection(pipeline.VITA_COMMANDS, timeout=10)
    except OSError as error:
        raise Unreachable(
            f"vitacompanion's command server does not answer at {pipeline.VITA_HOST}:1338 "
            f'({error}). This needs vitacompanion running on the console. Without it, install '
            f"the VPK in VitaShell, update with VitaShell's FTP server and `vita.py sync`, and "
            f'start the game from the LiveArea.'
        ) from error
    with connection:
        connection.sendall((chain + '\n').encode())
        connection.settimeout(timeout)
        output = b''
        try:
            while chunk := connection.recv(4096):
                output += chunk
        except TimeoutError:
            pass
    return output.decode(errors='replace').strip()


# Commands of the released vitacompanion: help, launch, nosleep, press, promote, quit, release,
# reboot, screen, version, wait. install, perf and screenshot come from an extended build.
EXTENDED = ('install', 'perf', 'screenshot')


def require(name):
    """Stops with a message if the console's vitacompanion lacks the command `name`."""
    listed = command('help', timeout=4)
    names = {line.split()[0] for line in listed.splitlines() if line.strip()}
    if listed and name not in names:
        hint = ' To install a VPK, use `pipeline.py deploy` and install it in VitaShell.' if name == 'install' else ''
        sys.exit(
            f'The vitacompanion on the console has no `{name}` command (the released vitacompanion '
            f'lacks {", ".join(EXTENDED)}).{hint}'
        )


_awake = None


def nudge():
    """Sends KEEP_AWAKE once; a console that does not answer is left alone."""
    with contextlib.suppress(OSError):
        command(KEEP_AWAKE, timeout=2)


def keep_awake():
    """Keeps the console from sleeping while this script runs. vitacompanion's "nosleep on"
    alone does not hold: without input the console still suspends, which freezes the game
    a few seconds into a launch. Repeating it with "screen on" from the host does."""
    global _awake
    if _awake:
        return
    import threading

    def tick():
        while True:
            nudge()
            time.sleep(KEEP_AWAKE_SECONDS)

    _awake = threading.Thread(target=tick, daemon=True)
    _awake.start()


def shot(out: Path):
    """Saves the displayed frame through vitacompanion's screenshot command."""
    from PIL import Image

    require('screenshot')
    remote = 'ux0:data/vitacompanion_shot.bmp'
    print(command(f'screenshot {remote}'))
    data = read(remote)
    with ftp() as connection:
        connection.delete(remote)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.open(io.BytesIO(data)).convert('RGB').save(out)
    print(f'saved {out}')


def install(build: Path):
    """Packages the build's VPK and streams it to vitacompanion's install command
    ("install - <size>", then the VPK; the Vita extracts it as it arrives), then records
    the build as synced."""
    require('install')
    vpk = pipeline.package_vita(build)
    size = vpk.stat().st_size
    with socket.create_connection(pipeline.VITA_COMMANDS, timeout=30) as connection, vpk.open('rb') as handle:
        connection.sendall(f'install - {size}\n'.encode())
        connection.settimeout(300)  # the Vita stops reading while it writes out large files
        sent, started, shown = 0, time.time(), 0
        while chunk := handle.read(1 << 20):
            connection.sendall(chunk)
            sent += len(chunk)
            if sent - shown >= 100 << 20 or sent == size:
                shown = sent
                pipeline.log(
                    f'Sent {sent >> 20} of {size >> 20} MB ({sent / 2**20 / (time.time() - started):.1f} MB/s)'
                )
        connection.settimeout(1800)  # promoting a large package takes a while after the last byte
        reply = b''
        while not reply.endswith(b'\n') and (chunk := connection.recv(4096)):
            reply += chunk
    reply = reply.decode(errors='replace').strip()
    print(reply)
    if not reply.startswith('Installed'):
        sys.exit(f'Installation failed: {reply}')
    seed(build)


def read(path):
    buffer = io.BytesIO()
    with ftp() as connection:
        connection.retrbinary(f'RETR {path}', buffer.write)
    return buffer.getvalue()


def write(path, data: bytes):
    with ftp() as connection:
        connection.storbinary(f'STOR {path}', io.BytesIO(data))


def makedirs(connection, directory, known):
    parts = directory.split('/')
    for i in range(2, len(parts) + 1):
        partial = '/'.join(parts[:i])
        if partial in known:
            continue
        with contextlib.suppress(ftplib.error_perm):  # exists
            connection.mkd(partial)
        known.add(partial)


MANIFEST = pipeline.ARTIFACTS / f'vita-synced-{pipeline.VITA_TITLE_ID}.json'


def digests(build: Path):
    for name, source in pipeline.vita_app_files(build):
        data = source if isinstance(source, bytes) else source.read_bytes()
        yield name, data, hashlib.sha1(data).hexdigest()


def seed(build: Path):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps({name: digest for name, _, digest in digests(build)}, indent=0))
    pipeline.log(f'Recorded {build} as installed')


def sync(build: Path):
    """Uploads the build's app files whose content changed since the last sync."""
    manifest_path = MANIFEST
    synced = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    connection = ftp()
    try:
        try:
            connection.cwd(APP)
        except ftplib.error_perm:
            sys.exit(f'{APP} does not exist: install the VPK once (pipeline.py deploy, then VitaShell)')
        changed, total, known = 0, 0, set()
        for name, data, digest in digests(build):
            total += 1
            if synced.get(name) == digest:
                continue
            target = f'{APP}/{name}'
            # The console's FTP server sometimes stops answering mid-transfer; a new
            # connection usually carries on.
            for attempt in range(3):
                try:
                    makedirs(connection, target.rsplit('/', 1)[0], known)
                    connection.storbinary(f'STOR {target}', io.BytesIO(data), blocksize=1 << 16)
                    break
                except FTP_ERRORS as error:
                    if attempt == 2:
                        raise
                    pipeline.log(f'{name}: {error!r}; reconnecting')
                    with contextlib.suppress(OSError):
                        connection.close()
                    time.sleep(5)
                    connection = ftp()
            synced[name] = digest
            changed += 1
            manifest_path.write_text(json.dumps(synced, indent=0))  # progress survives an interrupted sync
    finally:
        connection.close()
    pipeline.log(f'Synced {changed} of {total} files to {APP}')


def screenshot(out: Path):
    """Asks the probe for a screenshot of the game's frame and saves it."""
    with ftp() as connection:
        with contextlib.suppress(ftplib.error_perm):
            connection.delete(f'{REMOTE}/screenshot.raw')
        connection.storbinary(f'STOR {REMOTE}/screenshot.req', io.BytesIO(b'1'))
        deadline = time.time() + SCREENSHOT_SECONDS
        while time.time() < deadline:
            time.sleep(0.5)
            if any(n.rsplit('/', 1)[-1] == 'screenshot.raw' for n in connection.nlst(REMOTE)):
                buffer = io.BytesIO()
                connection.retrbinary(f'RETR {REMOTE}/screenshot.raw', buffer.write)
                break
        else:
            sys.exit('No screenshot: is a diag build running? See the log command.')
    from PIL import Image

    data = buffer.getvalue()
    width, height = struct.unpack('<ii', data[:8])
    image = Image.frombuffer('RGBA', (width, height), data[8 : 8 + width * height * 4], 'raw', 'RGBA', 0, -1)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.convert('RGB').save(out)
    print(f'saved {out} ({width}x{height})')


PARSE_CORE_COMMIT = '644b5f081c5f3c9b205180793ab8f4209dfd9d97'
PARSE_CORE_SHA256 = '5b81bf0d6e05df282e1189b7c0e7bc9ffd3005655b2d2d1d6787747ac66bc96a'  # its GitHub zip
ENGINE_BASE = 0x81000000  # link address of the first segment of the player and of the IL2CPP module
STACK_WORDS = 700  # words of the crashed thread's stack scanned for return addresses
ADDR2LINE_BATCH = 40  # addresses per psp2bin call
PSP2BIN = 'host_tools/build/bin/psp2bin.exe'  # in the PS Vita SDK


def parse_core(dump: Path):
    """A crash dump read with vita-parse-core, which is downloaded on first use."""
    parser_dir = pipeline.EXTERNAL / 'vita-parse-core'
    if not (parser_dir / 'util.py').exists():
        from common import github_source

        github_source('xyzz/vita-parse-core', PARSE_CORE_COMMIT, PARSE_CORE_SHA256, parser_dir)
    sys.path.insert(0, str(parser_dir))
    import util

    def c_str(buf, off):  # the parser is Python 2 code
        end = buf.find(b'\0', off)
        return bytes(buf[off : end if end >= 0 else len(buf)]).decode('latin-1')

    util.c_str = c_str
    import core as core_module

    core_module.c_str = c_str
    return core_module.CoreParser(str(dump))


def addr2line(binary: Path, addresses):
    """{address: symbol name} of addresses in a binary, from the SDK's psp2bin."""
    import re
    import subprocess

    import doctor

    if not addresses:
        return {}
    psp2bin = next((sdk / PSP2BIN for sdk in doctor.sdk_candidates() if (sdk / PSP2BIN).exists()), None)
    if psp2bin is None:
        sys.exit('No psp2bin.exe to resolve the addresses: point SCE_PSP2_SDK_DIR at the PS Vita SDK')
    names = {}
    for i in range(0, len(addresses), ADDR2LINE_BATCH):
        batch = ','.join(hex(a) for a in addresses[i : i + ADDR2LINE_BATCH])
        output = subprocess.run(
            [str(psp2bin), '-i', str(binary), '--addr2line=' + batch], capture_output=True, text=True
        ).stdout
        for block in output.split('Address:')[1:]:
            found = re.search(r'Symbol:[ \t]*(.*)', block)
            names[int(block.split()[0], 16)] = found.group(1).strip() if found else '?'
    return names


def symbolize(dump: Path, build: Path):
    """Prints the crashed thread's PC, LR and the return addresses on its stack, resolved
    against the Unity player's symbols and the build's IL2CPP module. Stack scanning also
    finds stale return addresses; read the chain from the top."""
    core = parse_core(dump)
    player = pipeline.editor_data(pipeline.TARGET) / 'PlaybackEngines' / 'PSP2Player' / 'PSP2Player_Il2cpp.self'
    symbols = {'LIBIL2CPP_PRX': build / 'SymbolFiles' / 'il2CppAssemblies.suprx'}
    modules = {m.name: m for m in core.modules}
    engine = next(name for name in modules if name.startswith('UnityPlayer'))
    symbols[engine] = player

    def locate(address):
        for name in symbols:
            segment = modules[name].segments[0]
            if segment.start <= address < segment.start + segment.size:
                return name, ENGINE_BASE + address - segment.start
        return None, None

    thread = next(t for t in core.threads if t.stop_reason)
    print(
        f'thread {thread.name}, stop reason 0x{thread.stop_reason:x}, registers '
        + ' '.join(f'r{i}={v:x}' for i, v in enumerate(thread.regs.gpr[:13]))
    )
    frames = [('PC', *locate(thread.pc)), ('LR', *locate(thread.regs.gpr[14]))]
    sp = thread.regs.gpr[13]
    for k in range(STACK_WORDS):
        try:
            value = int.from_bytes(core.read_vaddr(sp + 4 * k, 4), 'little')
        except Exception:
            break
        name, link = locate(value)
        if name and value & 1:  # Thumb return addresses
            frames.append((f'sp+{4 * k:x}', name, link))
    names = {}
    for name in symbols:
        wanted = sorted({link & ~1 for _, n, link in frames if n == name})
        for address, symbol in addr2line(symbols[name], wanted).items():
            names[name, address] = symbol
    for where, name, link in frames:
        if name:
            print(f'{where:>8} {"engine" if name == engine else "game":6} {names.get((name, link & ~1), "?")[:160]}')


def crash_dumps(connection):
    """The console's crash dumps, oldest first: (modified, name, size)."""
    return sorted(
        (facts.get('modify', ''), name, int(facts.get('size', 0)))
        for name, facts in connection.mlsd('ux0:/data/', facts=['modify', 'size'])
        if name.endswith('.psp2dmp')
    )


def crashes(pull: bool, build: Path):
    with ftp() as connection:
        dumps = crash_dumps(connection)
        for modify, name, size in dumps[-10:]:
            print(f'{modify}  {size:>10}  {name}')
        if pull and dumps:
            out = pipeline.ROOT / 'artifacts' / 'vita' / dumps[-1][1]
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open('wb') as handle:
                connection.retrbinary(f'RETR ux0:/data/{dumps[-1][1]}', handle.write)
            print(f'saved {out}')
    if pull and dumps:
        symbolize(out, build)


def main():
    try:
        run_command()
    except Unreachable as error:
        sys.exit(str(error))


def run_command():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        'command',
        choices=[
            'sync',
            'seed',
            'install',
            'launch',
            'quit',
            'run',
            'do',
            'shot',
            'reboot',
            'perf',
            'screenshot',
            'log',
            'args',
            'crashes',
        ],
    )
    parser.add_argument('--build', type=Path, help='Vita build folder (default: the diag build if present)')
    parser.add_argument('--tail', type=int, default=0, metavar='N', help="log: only the log's last N lines")
    parser.add_argument('--pull', action='store_true', help='crashes: fetch and symbolize the newest dump')
    # The options may come anywhere; everything else after the command is its arguments, in order
    # (the probe flags of args, the chain of do, the file of shot and screenshot).
    args, args.rest = parser.parse_known_args()
    default = pipeline.build_dir(pipeline.TARGET, 'il2cpp', 'vita', diag=True)
    build = args.build or (default if default.exists() else pipeline.build_dir(pipeline.TARGET, 'il2cpp', 'vita'))

    if args.command in ('quit', 'run'):
        print(command(f'{KEEP_AWAKE}; quit all; wait 2s'))
    if args.command == 'seed':
        seed(build)
    if args.command in ('sync', 'run'):
        sync(build)
    if args.command in ('launch', 'run'):
        print(command(f'{KEEP_AWAKE}; launch {pipeline.VITA_TITLE_ID}'))
    if args.command == 'install':
        install(build)
    if args.command == 'do':
        print(command(' '.join(args.rest), timeout=30))
    if args.command == 'shot':
        shot(Path(args.rest[0]) if args.rest else pipeline.ROOT / 'artifacts' / 'vita' / 'shot.png')
    if args.command == 'perf':
        require('perf')
        print(command('perf', timeout=5))
    if args.command == 'reboot':
        print(command('reboot', timeout=2))
    if args.command == 'screenshot':
        screenshot(Path(args.rest[0]) if args.rest else pipeline.ROOT / 'artifacts' / 'vita' / 'screenshot.png')
    if args.command == 'log':
        lines = read(f'{REMOTE}/log.txt').decode(errors='replace').splitlines()
        print('\n'.join(lines[-args.tail :] if args.tail else lines))
    if args.command == 'args':
        with ftp() as connection:
            makedirs(connection, REMOTE, set())
        write(f'{REMOTE}/args.txt', ' '.join(args.rest).encode())
        print('args.txt: ' + (' '.join(args.rest) or '(empty)'))
    if args.command == 'crashes':
        crashes(args.pull, build)


if __name__ == '__main__':
    main()
