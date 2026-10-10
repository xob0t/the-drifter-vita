"""Parallel audio import for the build stage, through the Unity Cache Server.

Unity 2018 imports one asset at a time. On the PS Vita nearly all of a first import is the
audio: each of the game's 9,089 clips is encoded to HEVAG (Sony's vagconv2) and, for the editor's
own playback, to Vorbis. Before the build's editor starts, prewarm() splits the clips that
need importing into one small project per job (the project's settings and the clips with
their .meta files, at the same paths), imports them with that many editors at once for the
same build target, and lets each upload its results to the cache server. The build's editor
then downloads them instead of importing them. A cached import is the importer's own output
for the same file, .meta, settings and target, so the project's Library ends up the same as
after a serial import (docs/build.md, "First build times").

Clips count as needing an import when the project has no imported data for them
(Library/metadata/<guid>) or the clip or its .meta is newer than that data: a first import, or
a rule that changed audio import settings. Clips the cache already holds cost a job only the
download.
"""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from common import ARTIFACTS, WORK, log, meta_guid, meta_of

AUDIO_SUFFIXES = ('.ogg', '.wav', '.mp3')
UPLOADED = re.compile(
    r'^Updating .+\.(?:ogg|wav|mp3) - GUID: [0-9a-f]{32}\.\.\.\n.*\(Uploaded to cache server\.\)', re.M | re.I
)
# Below this much estimated import work the jobs' start-up (about 30 s each) is not worth it.
MIN_SECONDS = 120
# The Vita's encode against Windows' (Vorbis only): 503 and 191 ms a clip on average.
TARGET_COST = {'PSP2': 1.0, 'Win64': 0.38}


def audio_cost(path: Path):
    """Estimated import seconds, from the file size alone (opening every clip is slow while virus
    scanners look at it): a fixed part plus the encode, which scales with the clip's length.
    Fitted on the Vita's first import of this game, Ogg Vorbis at about 9x smaller than 16-bit WAV;
    as good for balancing the jobs as the decoded length."""
    size = path.stat().st_size * (0.11 if path.suffix.lower() == '.wav' else 1)
    return 0.31 + 3.06e-6 * size


def pending_audio(project: Path):
    """(path relative to the project, estimated seconds) of clips the editor would import. Only
    clips changed since the editor last saved its asset database (Library/assetDatabase3, written
    when it exits) have their .meta read, so a rebuild with nothing new opens no file."""
    library = project / 'Library'
    database = library / 'assetDatabase3'
    since = database.stat().st_mtime if database.exists() else None
    pending = []
    for meta in (project / 'Assets').rglob('*.meta'):
        asset = meta.with_suffix('')
        if asset.suffix.lower() not in AUDIO_SUFFIXES or not asset.is_file():
            continue
        changed = max(asset.stat().st_mtime, meta.stat().st_mtime)
        if since is not None and changed <= since:
            continue
        guid = meta_guid(meta)
        if guid is None:
            continue
        imported = library / 'metadata' / guid[:2] / guid
        if imported.exists() and imported.stat().st_mtime >= changed:
            continue
        pending.append((asset.relative_to(project), audio_cost(asset)))
    return pending


def split(items, jobs):
    """Longest first onto the least loaded job."""
    buckets = [[0.0, []] for _ in range(jobs)]
    for path, cost in sorted(items, key=lambda item: -item[1]):
        bucket = min(buckets, key=lambda b: b[0])
        bucket[0] += cost
        bucket[1].append(path)
    return [b for b in buckets if b[1]]


def job_project(project: Path, folder: Path, assets):
    """A project with the build project's settings and the given assets, at the same paths with
    the same .meta files (so the same GUIDs and import settings), and their folders' .meta files."""
    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(project / 'ProjectSettings', folder / 'ProjectSettings')
    (folder / 'Packages').mkdir(parents=True)
    shutil.copy2(project / 'Packages' / 'manifest.json', folder / 'Packages' / 'manifest.json')
    folders = set()
    for relative in assets:
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        for name in (relative, meta_of(relative)):
            shutil.copy2(project / name, folder / name)
        folders.update(p for p in relative.parents if p.parts and p != Path('Assets'))
    for relative in folders:
        if (project / meta_of(relative)).exists():
            shutil.copy2(project / meta_of(relative), folder / meta_of(relative))


def memory_gb():
    """(total, available) physical memory in GB."""
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [
            ('dwLength', ctypes.c_ulong),
            ('dwMemoryLoad', ctypes.c_ulong),
            ('ullTotalPhys', ctypes.c_ulonglong),
            ('ullAvailPhys', ctypes.c_ulonglong),
            ('ullTotalPageFile', ctypes.c_ulonglong),
            ('ullAvailPageFile', ctypes.c_ulonglong),
            ('ullTotalVirtual', ctypes.c_ulonglong),
            ('ullAvailVirtual', ctypes.c_ulonglong),
            ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
        ]

    status = Status()
    status.dwLength = ctypes.sizeof(Status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullTotalPhys / 2**30, status.ullAvailPhys / 2**30


# Memory kept free for the main editor (which waits while the jobs run), the cache server and
# the rest of the system; an import editor peaked at about 0.7 GB, so 1 GB each leaves room.
MEMORY_RESERVE_GB = 4
MEMORY_PER_JOB_GB = 1.0


def default_jobs():
    """Three editors for every four hardware threads, each needing about 0.7 GB: the encoder runs
    on one thread per editor. On an 8-core, 16-thread Ryzen, 1,818 clips that take 953 s in one
    editor took 223 s with 8 editors, 185 s with 12 and 189 s with 16. The memory free when the
    import starts limits them too: in a Windows Sandbox sharing a 32 GB machine, 12 editors
    pushed the pagefile until the jobs had to be stopped, and 4 ran fine."""
    total, available = memory_gb()
    by_memory = int((min(available, total - MEMORY_RESERVE_GB) - MEMORY_RESERVE_GB) // MEMORY_PER_JOB_GB)
    return max(1, min(16, (os.cpu_count() or 2) * 3 // 4, by_memory))


def prewarm(project: Path, unity: Path, build_target: str, server: str, jobs: int):
    pending = pending_audio(project)
    estimate = sum(cost for _, cost in pending) * TARGET_COST.get(build_target, 1.0)
    if jobs < 2 or estimate < MIN_SECONDS:
        return
    buckets = split(pending, jobs)
    log(
        f'Importing {len(pending)} audio clips (about {estimate / 60:.0f} min in one editor) in {len(buckets)} editors '
        f'into the cache server'
    )
    root = WORK / 'import-jobs'
    logs = ARTIFACTS / 'import-jobs'
    logs.mkdir(parents=True, exist_ok=True)
    started, processes = time.time(), []
    for index, (_, assets) in enumerate(buckets):
        folder = root / f'{project.name}-{index}'
        job_project(project, folder, assets)
        log_path = logs / f'{project.name}-{index}.log'
        processes.append(
            (
                subprocess.Popen(
                    [
                        str(unity),
                        '-batchmode',
                        '-nographics',
                        '-quit',
                        '-projectPath',
                        str(folder),
                        '-buildTarget',
                        build_target,
                        '-CacheServerIPAddress',
                        server,
                        '-logFile',
                        str(log_path),
                    ]
                ),
                log_path,
            )
        )
    uploaded = failed = 0
    for process, log_path in processes:
        code = process.wait()
        text = log_path.read_text(encoding='utf-8', errors='replace') if log_path.exists() else ''
        uploaded += len(UPLOADED.findall(text))
        if code != 0:
            failed += 1
            log(f'An import job exited {code}; the build imports its clips itself. Log: {log_path}')
    shutil.rmtree(root, ignore_errors=True)
    log(
        f'Import jobs done after {time.time() - started:.0f}s: {uploaded} clips imported, '
        f'{len(pending) - uploaded} already in the cache or left to the build'
        + (f'; {failed} jobs failed' if failed else '')
    )
