"""AudioClips for the Windows player: FSB5 banks of 16-bit PCM.

Unity's Windows build stores most clips as Vorbis (FSB5 codec 15) and some as PCM (codec 2),
as the importer asks. FMOD only decodes Vorbis streams whose setup header it knows (by CRC), so
this writes every clip as PCM, which the player plays the same way. The PCM banks have the
reference's layout (fsb5.bank with codec 2):

- the source decoded at its own rate and channel count, as interleaved little-endian int16
  (rounded, clipped), no stretching or padding;
- the rate in the sample header, or a frequency chunk for a rate outside FMOD's table; the clip's
  m_Frequency is the source's rate either way;
- m_Length = samples / rate (as float32), m_CompressionFormat 0 (PCM), m_BitsPerSample 16;
- load type, preload and background loading from the importer's Standalone override (build
  target group 1) or its defaults.

Banks go to the serialized file's .resource stream in path ID order at 32-byte boundaries, as
audio.py does.

    imported(build, file_name, guid, file_id, asset)   # Build.IMPORTED_FOR['windows'][83]

Reading .ogg needs soundfile: uv run --with soundfile.
"""

from concurrent.futures import ThreadPoolExecutor

import audio
import fsb5
import numpy as np

PCM_FORMAT = 0  # AudioCompressionFormat.PCM
CODEC_PCM16 = 2  # FMOD_SOUND_FORMAT_PCM16
STANDALONE_GROUP = '1'  # BuildTargetGroup.Standalone


def settings(meta):
    importer = meta['AudioImporter']
    overrides = importer.get('platformSettingOverrides') or {}
    chosen = overrides.get(STANDALONE_GROUP) or importer['defaultSettings']
    out = {k: chosen[k] for k in ('loadType', 'sampleRateSetting', 'sampleRateOverride', 'conversionMode')}
    for k in ('forceToMono', 'normalize', 'preloadAudioData', 'loadInBackground', 'ambisonic', '3D'):
        out[k] = importer.get(k, '0')
    if out['sampleRateSetting'] != '0' or out['forceToMono'] != '0':
        raise NotImplementedError('AudioImporter sample rate or mono settings')
    return out


def clip(asset, project):
    """(AudioClip fields, FSB5 bank) of one audio asset."""
    name = audio.clip_name(asset)
    pcm, rate = audio.read(project / asset.path)
    channels, samples = pcm.shape
    data = np.clip(np.rint(pcm.T), -32768, 32767).astype('<i2').tobytes()
    fields = audio.fields(name, settings(audio.meta(asset)), channels, rate, samples)
    fields['m_Frequency'] = rate
    fields['m_Length'] = float(np.float32(samples / rate))
    fields['m_CompressionFormat'] = PCM_FORMAT
    return fields, fsb5.bank(name, rate, channels, samples, data, codec=CODEC_PCM16)


def imported(builder, file_name, guid, file_id, asset):
    """Build.IMPORTED_FOR['windows'][83]: the AudioClip's (data, None); its bank goes on the end
    of builder.streams['<file>.resource']. The first clip of a file decodes all of the file's
    clips at once."""
    cache = builder.__dict__.setdefault('windows_audio_clips', {})
    if file_name not in cache:
        guids = [g for g, _, cid in builder.layout.objects(file_name) if cid == 83]
        assets = [builder.layout.db.by_guid[g] for g in guids]
        with ThreadPoolExecutor(16) as pool:
            made = list(pool.map(lambda a: clip(a, builder.root), assets))
        cache[file_name] = dict(zip(guids, made, strict=True))
    fields, bank = cache[file_name].pop(guid)
    source = audio.resource_name(file_name)
    audio.append(builder.streams.setdefault(source, bytearray()), fields, bank, source)
    return fields, None
