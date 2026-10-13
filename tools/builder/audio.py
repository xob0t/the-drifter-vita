"""AudioClips as Unity's PS Vita build makes them: the release object and its FSB5 bank.

On the PS Vita Unity's importer encodes every clip as HEVAG (the console's ADPCM), whatever
compression the importer settings ask for (the pipeline's audio_overlay in
tools/downgrade/asset_rules.py asks for MP3, which records as HEVAG too), so all 9089 clips of the
reference build are HEVAG FSB5 banks and none is Vorbis or PCM. The importer:

- decodes the source (.ogg or .wav) at its own rate and channel count;
- time-stretches it to whole 28-sample frames (hevag.stretch) instead of padding it;
- encodes each channel as HEVAG, frames of the channels side by side (hevag.encode);
- stores it in a one-sample FSB5 bank (fsb5.bank) at the source's rate; a rate outside FMOD's
  table goes in a frequency chunk.

The AudioClip keeps the source's sample count in m_Length; m_Frequency is the source's rate, or
for a rate outside FMOD's table the next rate up in it, and then m_Length counts the samples the
source would have at that rate, n * rate_out // rate.

Banks go to the .resource file of the clip's serialized file, in path ID order, each starting on
a 32-byte boundary (every bank's size is a multiple of 32, so they follow each other with no gap).

    entries = build(assets)                       # [(fields, bank)], in the order given
    data = resource_stream(entries, 'sharedassets1.resource')   # fills m_Resource

or, inside build.py, Build.IMPORTED[83] = audio.imported, which leaves each file's stream in
build.streams['<file>.resource'].

Reading .ogg needs soundfile (libsndfile), which is not in pyproject: uv run --with soundfile.
"""

import contextlib
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import fsb5
import hevag
import numpy as np
import unityyaml
from paths import PROJECT, common, progress

HEVAG_FORMAT = 5  # AudioCompressionFormat.HEVAG
PSP2_GROUP = '17'  # BuildTargetGroup.PSP2, the key of the importer's platform override
JOB_FRAMES = 1 << 20  # channel frames a worker encodes at once
# Processes that encode the clips. More than 4 hardly help: on an 8-core, 16-thread Ryzen 7 5800X3D
# the game's 9089 clips took 528 s with 1, 242 s with 2, 144 s with 4, 122 s with 8 and 123 s with
# 13. The largest job (MusCh9GeneralNoDrums, 5 minutes of stereo, and the clip grouped with it)
# allocated up to 0.6 GB by tracemalloc; 1 GB a worker leaves room. The reserve is for the system
# and the build process, which grew by 1 GB after the audio step started.
MAX_WORKERS = 4
MEMORY_PER_WORKER_GB = 1.0
MEMORY_RESERVE_GB = 2


def settings(meta):
    """The importer settings for the PS Vita: the PSP2 override if there is one, else the defaults."""
    importer = meta['AudioImporter']
    overrides = importer.get('platformSettingOverrides') or {}
    chosen = overrides.get(PSP2_GROUP) or importer['defaultSettings']
    out = {k: chosen[k] for k in ('loadType', 'sampleRateSetting', 'sampleRateOverride', 'conversionMode')}
    for k in ('forceToMono', 'normalize', 'preloadAudioData', 'loadInBackground', 'ambisonic', '3D'):
        out[k] = importer.get(k, '0')
    if out['sampleRateSetting'] != '0' or out['forceToMono'] != '0':
        # The game uses neither; no reference shows how the importer resamples or mixes down.
        raise NotImplementedError('AudioImporter sample rate or mono settings')
    return out


def meta(asset):
    # AssetDB stores meta_path as a str, which unityyaml.meta can't open.
    return unityyaml.meta(Path(asset.meta_path))


def read(path):
    """(samples as (channels, n) float32 in the 16-bit range, rate)."""
    import soundfile

    data, rate = soundfile.read(str(path), dtype='float32', always_2d=True)
    return np.ascontiguousarray(data.T) * np.float32(32768), rate


def frequency(rate):
    """The AudioClip's m_Frequency: the source's rate, or the next one up in FMOD's table."""
    if rate in fsb5.RATES:
        return rate
    return next((r for r in fsb5.RATES if r > rate), rate)


def length(samples, rate):
    out = frequency(rate)
    if out != rate:
        samples = samples * out // rate
    return float(np.float32(samples / out))


def fields(name, importer, channels, rate, samples):
    """The AudioClip release dict; m_Resource gets its source and offset from resource_stream."""
    return {
        'm_Name': name,
        'm_LoadType': int(importer['loadType']),
        'm_Channels': channels,
        'm_Frequency': frequency(rate),
        'm_BitsPerSample': 16,
        'm_Length': length(samples, rate),
        'm_IsTrackerFormat': False,
        'm_Ambisonic': importer['ambisonic'] != '0',
        'm_SubsoundIndex': 0,
        'm_PreloadAudioData': importer['preloadAudioData'] != '0',
        'm_LoadInBackground': importer['loadInBackground'] != '0',
        'm_Legacy3D': importer['3D'] != '0',
        'm_Resource': {'m_Source': '', 'm_Offset': 0, 'm_Size': 0},
        'm_CompressionFormat': HEVAG_FORMAT,
    }


def clip_name(asset):
    return asset.path.rsplit('/', 1)[-1].rsplit('.', 1)[0]


def _make(jobs):
    """[(name, source path, importer settings)] -> [(fields, bank)]; many clips encode together."""
    shapes, stretched = [], []
    for name, path, importer in jobs:
        pcm, rate = read(path)
        frames = hevag.frames_for(pcm.shape[1])
        shapes.append((name, importer, pcm.shape[0], rate, pcm.shape[1], frames))
        stretched.append(hevag.stretch(pcm, frames))
    data = hevag.encode(stretched)
    out = []
    for (name, importer, channels, rate, samples, frames), encoded in zip(shapes, data, strict=True):
        out.append(
            (
                fields(name, importer, channels, rate, samples),
                fsb5.bank(name, rate, channels, frames * hevag.FRAME, encoded),
            )
        )
    return out


def default_workers(jobs):
    """How many processes encode the audio: MAX_WORKERS, or fewer where there are fewer hardware
    threads or jobs, or where the free memory holds fewer (MEMORY_PER_WORKER_GB each after
    MEMORY_RESERVE_GB). One per thread ran a user's machine out of memory."""
    _, available = common.memory_gb()
    by_memory = int((available - MEMORY_RESERVE_GB) // MEMORY_PER_WORKER_GB)
    return max(1, min(MAX_WORKERS, os.cpu_count() or 1, jobs, by_memory))


@contextlib.contextmanager
def one_blas_thread():
    """Processes started inside load numpy with one OpenBLAS thread. By default OpenBLAS starts one
    per hardware thread, and splits the encoder's matrix products over them: the sums then add up in
    another order, and the HEVAG bytes depend on the thread count (12 clips gave 5 different
    encodings with 1, 2, 4, 8 and 16 threads, at the same SNR). Each thread also commits about
    32 MB, 0.5 GB a process with 16, which Windows counts against its memory. This fixes the thread
    count only: OpenBLAS picks its kernels by CPU, and those differ too (Haswell, Sandybridge and
    Nehalem kernels gave three different encodings)."""
    old = os.environ.get('OPENBLAS_NUM_THREADS')
    os.environ['OPENBLAS_NUM_THREADS'] = '1'
    try:
        yield
    finally:
        if old is None:
            del os.environ['OPENBLAS_NUM_THREADS']
        else:
            os.environ['OPENBLAS_NUM_THREADS'] = old


def build(assets, workers=None):
    """[(fields, bank)] for the audio assets, in their order, encoded in `workers` processes at once
    (default: default_workers()). Always in processes started with one OpenBLAS thread, so the bytes
    don't depend on the thread count, the free memory or the number of workers; they can still
    differ between CPU families (one_blas_thread)."""
    import soundfile

    jobs, sizes = [], []
    for a in assets:
        path = PROJECT / a.path
        info = soundfile.info(str(path))
        jobs.append((clip_name(a), path, settings(meta(a))))
        sizes.append(hevag.frames_for(info.frames) * info.channels)
    groups, group, total = [], [], 0
    for i in sorted(range(len(jobs)), key=lambda i: -sizes[i]):
        group.append(i)
        total += sizes[i]
        if total >= JOB_FRAMES:
            groups.append(group)
            group, total = [], 0
    if group:
        groups.append(group)
    out = [None] * len(jobs)
    if not jobs:
        return out
    workers = workers or default_workers(len(groups))
    with (
        one_blas_thread(),
        ProcessPoolExecutor(workers) as pool,
        # Counted in frames: the long clips go first, a few to a group, so clips done would say little.
        progress.step(
            f'Encoding audio ({workers} worker{"s" if workers > 1 else ""})', total=sum(sizes), eta=True, count=None
        ) as step,
    ):
        futures = {pool.submit(_make, [jobs[i] for i in g]): g for g in groups}
        done = 0
        step.note(f'0/{len(jobs)} clips')
        try:
            for future in as_completed(futures):
                g = futures[future]
                for i, result in zip(g, future.result(), strict=True):
                    out[i] = result
                step.advance(sum(sizes[i] for i in g))
                done += len(g)
                step.note(f'{done}/{len(jobs)} clips')
        except BaseException:  # Ctrl+C or a failed group: the groups not started yet are dropped
            pool.shutdown(cancel_futures=True)
            raise
    return out


def resource_name(file_name):
    """The stream file next to a serialized file: sharedassets1.assets -> sharedassets1.resource."""
    return file_name.rsplit('.', 1)[0] + '.resource' if file_name.endswith('.assets') else file_name + '.resource'


def append(stream, fields, bank, source):
    """Put a bank on the end of a .resource stream (a bytearray) at the next 32-byte boundary and
    point the clip's m_Resource at it."""
    stream += bytes(fsb5.align(len(stream)) - len(stream))
    fields['m_Resource'] = {'m_Source': source, 'm_Offset': len(stream), 'm_Size': len(bank)}
    stream += bank


def resource_stream(entries, source):
    """Lay out the banks of one serialized file's clips (in path ID order) in its .resource file,
    filling each clip's m_Resource; returns the file's bytes."""
    out = bytearray()
    for fields, bank in entries:
        append(out, fields, bank, source)
    return bytes(out)


def imported(builder, file_name, guid, file_id, asset):
    """Build.IMPORTED[83]: the AudioClip's (data, tree). Its bank goes on the end of
    builder.streams['<file>.resource'] (a bytearray per stream file, made here if build.py has
    none), for the writer to save next to the serialized file. Objects come in path ID order,
    which is the stream's order. The first clip of a file encodes all of the file's clips at once."""
    cache = builder.__dict__.setdefault('audio_clips', {})
    if file_name not in cache:
        guids = [g for g, _, cid in builder.layout.objects(file_name) if cid == 83]
        cache[file_name] = dict(zip(guids, build([builder.layout.db.by_guid[g] for g in guids]), strict=True))
    fields, bank = cache[file_name].pop(guid)
    source = resource_name(file_name)
    streams = builder.__dict__.setdefault('streams', {})
    append(streams.setdefault(source, bytearray()), fields, bank, source)
    return fields, None
