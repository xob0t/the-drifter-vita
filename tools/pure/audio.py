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

import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import fsb5
import hevag
import numpy as np
import unityyaml
from paths import PROJECT

HEVAG_FORMAT = 5  # AudioCompressionFormat.HEVAG
PSP2_GROUP = '17'  # BuildTargetGroup.PSP2, the key of the importer's platform override
JOB_FRAMES = 1 << 20  # channel frames a worker encodes at once


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
    # Asset.meta reads meta_path, which AssetDB stores as a str that unityyaml.meta can't open.
    return unityyaml.meta(Path(asset.meta_path))


def read(path):
    """(samples as (channels, n) float64 in the 16-bit range, rate)."""
    import soundfile

    data, rate = soundfile.read(str(path), dtype='float32', always_2d=True)
    return data.T.astype(np.float64) * 32768, rate


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


def _make(jobs, candidates=hevag.CANDIDATES):
    """[(name, source path, importer settings)] -> [(fields, bank)]; many clips encode together."""
    shapes, stretched = [], []
    for name, path, importer in jobs:
        pcm, rate = read(path)
        frames = hevag.frames_for(pcm.shape[1])
        shapes.append((name, importer, pcm.shape[0], rate, pcm.shape[1], frames))
        stretched.append(hevag.stretch(pcm, frames))
    data = hevag.encode(stretched, candidates)
    out = []
    for (name, importer, channels, rate, samples, frames), encoded in zip(shapes, data, strict=True):
        out.append(
            (
                fields(name, importer, channels, rate, samples),
                fsb5.bank(name, rate, channels, frames * hevag.FRAME, encoded),
            )
        )
    return out


def clip(asset, project=PROJECT):
    """(AudioClip fields, FSB5 bank) of one audio asset."""
    return _make([(clip_name(asset), project / asset.path, settings(meta(asset)))])[0]


def build(assets, workers=None, project=PROJECT, progress=None):
    """[(fields, bank)] for the audio assets, in their order, encoded on all cores."""
    import soundfile

    jobs, sizes = [], []
    for a in assets:
        path = project / a.path
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
    if len(groups) <= 1:
        return _make(jobs)
    with ProcessPoolExecutor(workers or os.cpu_count()) as pool:
        futures = {pool.submit(_make, [jobs[i] for i in g]): g for g in groups}
        for done, (future, g) in enumerate(futures.items()):
            for i, result in zip(g, future.result(), strict=True):
                out[i] = result
            if progress:
                progress(done + 1, len(groups))
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
