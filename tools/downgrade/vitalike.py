"""Vita-like PC runs: the Windows IL2CPP diag player slowed down to the PS Vita's CPU speed.

    uv run tools/downgrade/vitalike.py <tag> [--cap 5.5] [--mode duty|job|none] [--period 4] [--cores 3]
                            [--timeout 900] [--build dir] [--fresh] [--warmup] [--no-guard] [--pc-rules]
                            [--window a-b] -- <probe flags...>
    uv run tools/downgrade/vitalike.py summary <run dir or Vita tag>... [--window a-b]

Runs a real copy of builds/drifter-<version>-il2cpp-diag (work/diag/vitalike/player, refreshed when the
build changes; the probe is compiled in, as in the Vita diag build) off-screen and muted, with
-diag-out/-diag-remote in artifacts/vitalike/<tag>/, until done.txt, then prints a one-line summary.
See docs/vitalike.md for the calibration against the console.

The player starts suspended inside a Job Object that limits it to <cores> logical processors (one per
physical core, core 0 left out) and kills it with this script, at high priority; then:
  --mode duty (default): every thread of the player runs <cap> percent of the wall time, in slices of
      --period ms (4; all its threads suspended in between, NtSuspendProcess/NtResumeProcess), so each
      core behaves like one 100/<cap> times slower. The GPU, I/O and the frame limiter are not slowed.
      Between slices a high-priority busy loop per game core (a "guard", --no-guard to leave it out)
      keeps other programs off those cores: without it, whatever ran there in between (another
      program's window, for one) emptied the caches and runs swung between two speeds 30% apart.
  --mode job: the Job Object's CPU rate hard cap (JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | HARD_CAP) at
      <cap> percent of one core for the whole process. Windows enforces it in ~600 ms windows (at 25%:
      150 ms at full speed, then 450 ms stopped), and only once the process has used its share, so a
      game held at 60 fps that needs less is not slowed at all. Kept for comparison only.
  --cap 100 or --mode none: full speed with the same flags, for a PC baseline.

Unless --pc-rules is given, the flags that turn on the Vita's own rules on Windows are added (input
emulation, animator culling, particle rate and merge, empty particle cameras, fast noise, audio data,
stored saves, the one-pass CRT); a Vita-style off switch in the probe flags (-compat-no-anim-cull,
-compat-exact-noise, ...) leaves its rule out, so args lines of Vita runs can be replayed as they are.
Without -diag-vsync/-diag-target-fps, the player runs unsynchronized ("-diag-vsync 0 -diag-target-fps
-1", like the Vita's cpu3 runs): with "-diag-target-fps 60" Unity's frame limiter pads the short frames
that slicing makes, up to 12 ms a frame, and vsync would run at the monitor's refresh rate. Compare step and
script times with the Vita's vsync runs, and fps with its unsynchronized ones. -diag-skip-intro is added
like vita_run.py does.
--warmup runs at full speed until the probe's save restore has returned (log.txt), then slows down:
twice as fast for a smoke test, but not calibrated (scripts then run about 1.4 times slower at the same
cap, cause unknown) and the restore is a PC number.

The summary averages heartbeats in the last scene (the first one after a scene change left out), or
those inside --window a-b (seconds after that scene's load, e.g. 20-45 for a press at 15-45), and lists
the restore and each -diag-goto-room change (transition start to room-ready) from events.txt.
`summary` takes run folders or tags of artifacts/vita/runs, so Vita and Vita-like runs print the same
way. vitalike.txt in the run folder has the setup, the duty measured and the other programs' load.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import capture
import heartbeats
import lock
import pipeline
from heartbeats import parse_window, summary

BUILD = pipeline.ROOT / 'builds' / f'drifter-{pipeline.TARGET}-il2cpp-diag'
COPY = pipeline.WORK / 'diag' / 'vitalike' / 'player'
OUT = pipeline.ROOT / 'artifacts' / 'vitalike'

# Calibrated against Vita runs (docs/vitalike.md).
DEFAULT_CAP = 5.5
DEFAULT_PERIOD_MS = 4.0
WINDOW_SECONDS = 600  # how long the slowed player gets to open its window

# Vita rules that Windows players only apply with a flag, and the Vita's switch that turns each off.
VITA_RULES = [
    (['-vita-emulate'], None),
    (['-compat-anim-cull'], '-compat-no-anim-cull'),
    (['-compat-particle-rate'], '-compat-no-particle-rate'),
    (['-compat-particle-merge'], '-compat-no-particle-merge'),
    (['-skip-empty-particle-cameras'], None),
    (['-compat-fast-noise'], '-compat-exact-noise'),
    (['-compat-audio-data'], '-compat-keep-audio-data'),
    (['-compat-stored-saves'], None),
    (['-crt-single-pass'], '-crt-three-pass'),
    (['-crt-aber', 'on'], '-crt-aber'),
]

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
ntdll = ctypes.WinDLL('ntdll')
kernel32.CreateJobObjectW.restype = wt.HANDLE
kernel32.GetCurrentThread.restype = wt.HANDLE
kernel32.CreateWaitableTimerExW.restype = wt.HANDLE
kernel32.SetThreadAffinityMask.argtypes = [wt.HANDLE, ctypes.c_size_t]
kernel32.SetThreadAffinityMask.restype = ctypes.c_size_t
ntdll.NtSuspendProcess.argtypes = [wt.HANDLE]
ntdll.NtResumeProcess.argtypes = [wt.HANDLE]
kernel32.SetThreadPriority.argtypes = [wt.HANDLE, ctypes.c_int]
kernel32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
kernel32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.SetProcessAffinityMask.argtypes = [wt.HANDLE, ctypes.c_size_t]
kernel32.SetPriorityClass.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.SetWaitableTimer.argtypes = [
    wt.HANDLE,
    ctypes.POINTER(ctypes.c_longlong),
    wt.LONG,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wt.BOOL,
]
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]


class IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            'ReadOperationCount',
            'WriteOperationCount',
            'OtherOperationCount',
            'ReadTransferCount',
            'WriteTransferCount',
            'OtherTransferCount',
        )
    ]


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_longlong),
        ('PerJobUserTimeLimit', ctypes.c_longlong),
        ('LimitFlags', wt.DWORD),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', wt.DWORD),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', wt.DWORD),
        ('SchedulingClass', wt.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', BasicLimits),
        ('IoInfo', IoCounters),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


class CpuRate(ctypes.Structure):
    _fields_ = [('ControlFlags', wt.DWORD), ('CpuRate', wt.DWORD)]


class ProcessorInfo(ctypes.Structure):  # SYSTEM_LOGICAL_PROCESSOR_INFORMATION
    _fields_ = [('ProcessorMask', ctypes.c_size_t), ('Relationship', wt.DWORD), ('Reserved', ctypes.c_ulonglong * 2)]


def check(ok, what):
    if not ok:
        raise OSError(ctypes.get_last_error(), f'{what} failed')
    return ok


def physical_cores():
    """Logical processor masks per physical core, in order."""
    size = wt.DWORD(0)
    kernel32.GetLogicalProcessorInformation(None, ctypes.byref(size))
    buffer = (ProcessorInfo * (size.value // ctypes.sizeof(ProcessorInfo)))()
    check(kernel32.GetLogicalProcessorInformation(buffer, ctypes.byref(size)), 'GetLogicalProcessorInformation')
    return [entry.ProcessorMask for entry in buffer if entry.Relationship == 0]  # RelationProcessorCore


def pick_cpus(count):
    """One logical processor on each of <count> physical cores after core 0 (where most interrupts
    land), and one for the duty controller on the last core."""
    cores = physical_cores()
    if len(cores) < count + 2:
        raise SystemExit(f'{len(cores)} physical cores: not enough for {count} game cores and the controller')

    def lowest(mask):
        return mask & -mask

    game = 0
    for mask in cores[1 : count + 1]:
        game |= lowest(mask)
    return game, lowest(cores[-1])


JOB_LIMIT_AFFINITY, JOB_KILL_ON_CLOSE = 0x10, 0x2000  # JOB_OBJECT_LIMIT_*
EXTENDED_LIMITS, CPU_RATE_LIMITS = 9, 15  # JOBOBJECTINFOCLASS


def job_object(limit_flags, affinity=0):
    """A new Job Object with these basic limits."""
    job = check(kernel32.CreateJobObjectW(None, None), 'CreateJobObject')
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = limit_flags
    limits.BasicLimitInformation.Affinity = affinity
    check(
        kernel32.SetInformationJobObject(job, EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)),
        'SetInformationJobObject(limits)',
    )
    return job


def make_job(affinity, cap, mode):
    # A priority class limit needs a privilege; the process gets its priority from SetPriorityClass instead.
    job = job_object(JOB_LIMIT_AFFINITY | JOB_KILL_ON_CLOSE, affinity)
    if mode == 'job':
        # CpuRate is in 1/100 percent of all the machine's processors.
        rate = CpuRate(0x1 | 0x4, max(1, min(10000, round(cap * 100 / os.cpu_count()))))
        check(
            kernel32.SetInformationJobObject(job, CPU_RATE_LIMITS, ctypes.byref(rate), ctypes.sizeof(rate)),
            'SetInformationJobObject(cpu rate)',
        )
    return job


class Duty(threading.Thread):
    """Runs the process <fraction> of the wall time in slices of <period> seconds. The off time
    follows the measured run time, so timer and suspend overheads do not change the fraction."""

    def __init__(self, handle, fraction, period, cpu_mask, guards=()):
        super().__init__(daemon=True)
        self.handle, self.fraction, self.period, self.cpu_mask = handle, fraction, period, cpu_mask
        self.guards = list(guards)  # processes that run only while the game is suspended
        self.stop = threading.Event()
        self.ran = self.off = 0.0
        self.cycles = 0
        self.warming_up = False  # --warmup: full speed until the save restore has returned

    def wait(self, seconds):
        deadline = time.perf_counter() + seconds
        remaining = seconds
        if remaining > 0.0015:
            due = ctypes.c_longlong(-int((remaining - 0.001) * 1e7))
            kernel32.SetWaitableTimer(self.timer, ctypes.byref(due), 0, None, None, False)
            kernel32.WaitForSingleObject(self.timer, 0xFFFFFFFF)
        while time.perf_counter() < deadline:
            pass

    def run(self):
        try:
            self.cycle()
        finally:
            ntdll.NtResumeProcess(self.handle)

    def cycle(self):
        kernel32.SetThreadAffinityMask(kernel32.GetCurrentThread(), self.cpu_mask)
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 15)  # THREAD_PRIORITY_TIME_CRITICAL
        self.timer = kernel32.CreateWaitableTimerExW(None, None, 0x2, 0x1F0003)  # HIGH_RESOLUTION
        run_slice = self.period * self.fraction
        ntdll.NtResumeProcess(self.handle)  # created suspended
        while not self.stop.is_set():
            if self.warming_up:
                time.sleep(0.05)
                continue
            start = time.perf_counter()
            self.wait(run_slice)
            if ntdll.NtSuspendProcess(self.handle) != 0:
                break
            for guard in self.guards:
                ntdll.NtResumeProcess(guard)
            suspended = time.perf_counter()
            self.ran += suspended - start
            target = self.ran * (1 - self.fraction) / self.fraction - self.off
            self.wait(min(max(target, 0.0), 4 * self.period))
            for guard in self.guards:
                ntdll.NtSuspendProcess(guard)
            ntdll.NtResumeProcess(self.handle)
            self.off += time.perf_counter() - suspended
            self.cycles += 1

    def report(self):
        total = self.ran + self.off
        if not total:
            return 'duty: no cycles'
        return (
            f'duty {self.ran / total * 100:.2f}% of {total:.0f} s, {self.cycles} cycles, '
            f'mean period {total / max(1, self.cycles) * 1000:.2f} ms'
        )


def system_times():
    idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    return idle.value, kernel.value + user.value  # kernel time includes idle time


def other_load(start, end, args):
    """Busy share of all processors during the run without the game, the duty controller (about a
    third of a core) and the guards: roughly what other programs used (docs/vitalike.md)."""
    idle, total = end[0] - start[0], end[1] - start[1]
    busy = 1 - idle / max(1, total)
    own = args.cores * min(args.cap, 100) / 100  # the game, at most
    if args.mode == 'duty' and args.cap < 100:
        own += 0.3 + (args.cores * (1 - args.cap / 100) if args.guard else 0)  # controller, guards
    own /= os.cpu_count()
    return max(0.0, busy - own) * 100


def start_guards(mask):
    """--guard: one busy loop per game processor, pinned there at high priority, that the duty
    controller runs only while the game is suspended (and suspends before resuming the game). It
    keeps other programs (idle-priority ones too) off the game's cores between slices, so they do
    not empty its caches, and keeps the cores clocked up. Returns (job, process handles); the job
    kills them with this script. The base interpreter, not the venv's launcher (which has a child)."""
    job = job_object(JOB_KILL_ON_CLOSE)
    interpreter = getattr(sys, '_base_executable', sys.executable)
    handles = []
    for bit in range(64):
        if mask >> bit & 1:
            guard = subprocess.Popen(
                [interpreter, '-I', '-c', 'while True: pass'], creationflags=0x80 | 0x4
            )  # HIGH_PRIORITY_CLASS | CREATE_SUSPENDED
            handle = wt.HANDLE(int(guard._handle))
            check(kernel32.AssignProcessToJobObject(job, handle), 'AssignProcessToJobObject(guard)')
            check(kernel32.SetProcessAffinityMask(handle, ctypes.c_size_t(1 << bit)), 'SetProcessAffinityMask')
            handles.append((guard, handle))
    return job, handles


def prepare_player(build: Path, fresh: bool):
    """A real copy of the build (not hard links), so runs never hold the build's files open while
    pipeline.py rebuilds it. Refreshed when the build's GameAssembly.dll or data changes."""
    marker = COPY / 'vitalike-source.txt'
    sources = [build / 'GameAssembly.dll', build / 'TheDrifter_Data' / 'globalgamemanagers']
    if not all(path.exists() for path in sources):
        raise SystemExit(f'{build} is not an IL2CPP player; build it with pipeline.py build --backend il2cpp --diag')
    stamp = f'{build} ' + ' '.join(str(path.stat().st_mtime_ns) for path in sources)
    if not fresh and marker.exists() and marker.read_text() == stamp:
        return
    holder = lock.holder_of('pc-build')
    if holder:
        if marker.exists():
            print(f'pc-build is held ({holder}); using the existing copy of the player')
            return
        raise SystemExit(f'pc-build is held ({holder}); the build may be changing, try again when it is released')
    print(f'copying {build} to {COPY}')
    if COPY.exists():
        shutil.rmtree(COPY)
    shutil.copytree(build, COPY, ignore=shutil.ignore_patterns('*_BackUpThisFolder_ButDontShipItWithYourGame'))
    marker.write_text(stamp)


def vita_flags(flags):
    added = []
    for rule, off_switch in VITA_RULES:
        if off_switch is not None and off_switch in flags:
            continue
        added += rule
    return added


def run_flags(args, flags):
    """The probe flags of a run: the given ones, with -diag-skip-intro, the Vita's rules (unless
    --pc-rules) and an unsynchronized frame rate unless they set one."""
    flags = list(flags)
    if '-diag-skip-intro' not in flags:
        flags.insert(0, '-diag-skip-intro')
    if not args.pc_rules:
        flags += vita_flags(flags)
    if '-diag-vsync' not in flags and '-diag-target-fps' not in flags:
        # Unsynchronized: the frame limiter (targetFrameRate 60; vsync would follow the monitor, not 60 Hz)
        # pads the short frames that slicing makes (docs/vitalike.md), so the Vita's 60 Hz is not imitated.
        flags += ['-diag-vsync', '0', '-diag-target-fps', '-1']
    return flags


def wait_for_done(process, out: Path, timeout, duty, started):
    """Waits for the probe's done.txt; with --warmup, slows the player down once the restore returned."""
    deadline = time.time() + timeout
    while not (out / 'done.txt').exists():
        if process.poll() is not None:
            raise RuntimeError(f'player exited with {process.returncode} before done.txt')
        if time.time() > deadline:
            raise RuntimeError(f'timed out after {timeout:.0f} s waiting for done.txt')
        if duty is not None and not duty.is_alive():
            raise RuntimeError('the duty controller stopped')
        if duty is not None and duty.warming_up:
            log = out / 'log.txt'
            if log.exists() and heartbeats.RESTORE_RETURNED in log.read_text(errors='replace'):
                duty.ran = duty.off = 0.0
                duty.warming_up = False
                print(f'warmup over after {time.time() - started:.0f} s')
        time.sleep(0.25)


def launch(args, flags):
    prepare_player(args.build.resolve(), args.fresh)
    exe = COPY / 'TheDrifter.exe'
    # The game keeps saves in its working directory: fresh copies for every run.
    for save in pipeline.DEFAULT_GAME.glob('*.sav'):
        shutil.copy2(save, COPY / save.name)
    out = (OUT / args.tag).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob('*'):
        if stale.is_file():
            stale.unlink()
    flags = run_flags(args, flags)
    mode = 'none' if args.cap >= 100 else args.mode
    game_mask, controller_mask = pick_cpus(args.cores)
    command = [
        str(exe),
        '-screen-fullscreen',
        '0',
        '-screen-width',
        str(capture.WIDTH),
        '-screen-height',
        str(capture.HEIGHT),
        '-logFile',
        str(out / 'player.log'),
        '-diag-out',
        str(out),
        '-diag-remote',
        str(out),
        *flags,
    ]
    setup = (
        f'vitalike mode {mode} cap {args.cap}% period {args.period} ms cores {args.cores} '
        f'(mask {game_mask:#x}) warmup {args.warmup} guard {args.guard}\nargs {" ".join(command[1:])}\n'
    )
    (out / 'vitalike.txt').write_text(setup)
    job = make_job(game_mask, args.cap, mode)
    guards = start_guards(game_mask) if args.guard and mode == 'duty' else None
    process = subprocess.Popen(command, cwd=exe.parent, creationflags=0x4)  # CREATE_SUSPENDED
    handle = wt.HANDLE(int(process._handle))
    duty = None
    started = time.time()
    load_start = system_times()
    for name in ('pc-build', 'vita-build'):
        if lock.holder_of(name):
            print(f'warning: {name} is held; a build running next to the game inflates its times (docs/vitalike.md)')
    try:
        check(kernel32.AssignProcessToJobObject(job, handle), 'AssignProcessToJobObject')
        check(kernel32.SetPriorityClass(handle, 0x80), 'SetPriorityClass')  # HIGH_PRIORITY_CLASS
        if mode == 'duty':
            duty = Duty(
                handle, args.cap / 100, args.period / 1000, controller_mask, [h for _, h in guards[1]] if guards else ()
            )
            duty.warming_up = args.warmup
            duty.start()
        else:
            ntdll.NtResumeProcess(handle)
        capture.offscreen(capture.find_window(process.pid, timeout=WINDOW_SECONDS))
        wait_for_done(process, out, args.timeout, duty, started)
    finally:
        if duty is not None:
            duty.stop.set()
            duty.join(2)
        process.kill()
        process.wait()
        kernel32.CloseHandle(job)
        if guards is not None:
            kernel32.TerminateJobObject(guards[0], 0)
            kernel32.CloseHandle(guards[0])
    wall = time.time() - started
    note = f'run {wall:.0f} s, other load {other_load(load_start, system_times(), args):.0f}% of the PC' + (
        f'; {duty.report()}' if duty is not None else ''
    )
    with open(out / 'vitalike.txt', 'a') as file:
        file.write(note + '\n')
    print(note)
    return out


# Summaries: heartbeats.py, shared with Vita runs (heartbeats.py summary prints both alike).


def main():
    argv = sys.argv[1:]
    if argv[:1] == ['summary']:
        return heartbeats.main(argv)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('tag')
    parser.add_argument(
        '--cap',
        type=float,
        default=DEFAULT_CAP,
        help='percent of one PC core per game core (duty) or for the whole process (job)',
    )
    parser.add_argument('--mode', choices=['duty', 'job', 'none'], default='duty')
    parser.add_argument('--period', type=float, default=DEFAULT_PERIOD_MS, help='duty slice in ms')
    parser.add_argument('--cores', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=900)
    parser.add_argument('--fresh', action='store_true', help='copy the build again')
    parser.add_argument('--build', type=Path, default=BUILD)
    parser.add_argument('--warmup', action='store_true', help='full speed until the save restore returned')
    parser.add_argument(
        '--no-guard',
        dest='guard',
        action='store_false',
        help='let other programs use the game cores between slices (less reproducible)',
    )
    parser.add_argument('--pc-rules', action='store_true', help="do not add the Vita's rules")
    parser.add_argument('--window', default=None, help='summary: seconds a-b after the last scene load')
    cut = argv.index('--') if '--' in argv else len(argv)
    args = parser.parse_args(argv[:cut])
    if args.mode == 'none':
        args.cap = 100.0
    out = launch(args, argv[cut + 1 :])
    print(summary(out, parse_window(args.window)))


if __name__ == '__main__':
    main()
