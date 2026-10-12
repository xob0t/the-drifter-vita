"""Build the AudioClips and their .resource streams and compare them with the reference build.

    uv run --with soundfile tools/builder/check_audio.py [--every N] [--workers N] [--no-snr]

Clips pair with the reference by path ID (layout.py reproduces the reference order). Reports
per-field AudioClip matches, FSB5 header matches field by field, the stream layout (offsets and
file sizes) and the sound: SNR of our HEVAG and Unity's against the stretched source, and of ours
against Unity's, all decoded as the console does. Our .resource files go to work/builder/audio/.
--every N checks one clip in N (the stream layout then is not comparable and is skipped).
"""

import argparse
import collections
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import audio
import fsb5
import hevag
import numpy as np
import paths
import UnityPy
from layout import Layout
from placement import first_path_id

OUT = paths.WORK / 'audio'
FILES = ['resources.assets', 'sharedassets1.assets']


def reference_clips(file_name):
    env = UnityPy.load(str(paths.REFERENCE_MEDIA / file_name))
    sf = next(iter(env.files.values()))
    return {pid: o.read_typetree() for pid, o in sf.objects.items() if o.class_id == 83}


def frame_layout(data, channels, frames):
    """Whether the frames carry Unity's flags (6 first, 3 last, 2 between) and zeros follow."""
    f = np.frombuffer(data[: frames * 16 * channels], np.uint8).reshape(frames, channels, 16)
    expected = np.full(frames, hevag.MIDDLE)
    expected[-1], expected[0] = hevag.LAST, hevag.FIRST
    return bool(((f[..., 1] & 0x0F) == expected[:, None]).all() and not any(data[frames * 16 * channels :]))


def quality(items):
    """[(source path, frames, channels, ours, theirs)] -> [(ours/source, unity/source, ours/unity) dB]."""
    sources = []
    for path, frames, _, _, _ in items:
        pcm, _ = audio.read(path)
        sources.append(hevag.stretch(pcm, frames))
    ours = hevag.decode([(o, ch) for _, _, ch, o, _ in items])
    theirs = hevag.decode([(t, ch) for _, _, ch, _, t in items])
    return [(hevag.snr(s, o), hevag.snr(s, t), hevag.snr(t, o)) for s, o, t in zip(sources, ours, theirs, strict=True)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--every', type=int, default=1)
    parser.add_argument('--workers', type=int, help="default: the build's (audio.default_workers)")
    parser.add_argument('--no-snr', action='store_true')
    parser.add_argument('--show', type=int, default=3)
    args = parser.parse_args()
    layout = Layout()
    OUT.mkdir(parents=True, exist_ok=True)
    pairs = []  # (file, path ID, asset)
    for f in FILES:
        start = first_path_id(f)
        for i, (g, _, cid) in enumerate(layout.objects(f)):
            if cid == 83:
                pairs.append((f, start + i, layout.db.by_guid[g]))
    pairs = pairs[:: args.every]
    print(f'{len(pairs)} clips')
    args.workers = args.workers or audio.default_workers(len(pairs))

    started = time.time()
    built = audio.build([a for _, _, a in pairs], args.workers)
    elapsed = time.time() - started
    seconds = sum(f['m_Length'] for f, _ in built)
    print(f'built in {elapsed:.1f} s on {args.workers} workers ({seconds / 3600:.2f} hours of audio)')

    totals, examples = collections.Counter(), collections.defaultdict(list)

    def note(key, ok, example):
        totals[(key, ok)] += 1
        if not ok and len(examples[key]) < args.show:
            examples[key].append(example)

    jobs = []
    for f in FILES:
        refs = reference_clips(f)
        mine = [(pid, a, entry) for (ff, pid, a), entry in zip(pairs, built, strict=True) if ff == f]
        source = audio.resource_name(f)
        stream = audio.resource_stream([e for _, _, e in mine], source)
        (OUT / source).write_bytes(stream)
        reference_stream = paths.REFERENCE_MEDIA / source
        if args.every == 1:
            size = reference_stream.stat().st_size
            print(f'{source}: {len(stream)} bytes, reference {size}: {"same" if len(stream) == size else "differs"}')
        with open(reference_stream, 'rb') as handle:
            for pid, a, (fields, bank) in mine:
                theirs = refs[pid]
                for k, v in fields.items():
                    if k == 'm_Resource':
                        for kk in ('m_Source', 'm_Size') + (('m_Offset',) if args.every == 1 else ()):
                            note(f'{k}.{kk}', v[kk] == theirs[k][kk], (a.path, v[kk], theirs[k][kk]))
                    else:
                        note(k, v == theirs[k], (a.path, v, theirs[k]))
                r = theirs['m_Resource']
                handle.seek(r['m_Offset'])
                ref_bank = handle.read(r['m_Size'])
                ours, ref = fsb5.parse(bank), fsb5.parse(ref_bank)
                for k in ('codec', 'sample_headers', 'names', 'data_size', 'name_hash', 'word', 'chunks'):
                    note(f'fsb {k}', ours[k] == ref[k], (a.path, ours[k], ref[k]))
                note('fsb content_hash (expected to differ)', ours['content_hash'] == ref['content_hash'], (a.path,))
                head = ours['header'][:0x2C] + ours['header'][0x3C:]
                note(
                    'fsb header bytes but the content hash',
                    head == ref['header'][:0x2C] + ref['header'][0x3C:],
                    (a.path,),
                )
                note('fsb size', len(bank) == len(ref_bank), (a.path, len(bank), len(ref_bank)))
                frames = ours['samples'] // hevag.FRAME
                ch = ours['channels']
                note('fsb frame flags and padding', frame_layout(ours['data'], ch, frames), (a.path,))
                note('reference frame flags and padding', frame_layout(ref['data'], ch, frames), (a.path,))
                data, ref_data = ours['data'][: frames * 16 * ch], ref['data'][: frames * 16 * ch]
                note('HEVAG bytes identical', data == ref_data, (a.path,))
                jobs.append((a.path, paths.PROJECT / a.path, frames, ch, data, ref_data))

    for (key, ok), v in sorted(totals.items()):
        if ok:
            bad = totals[(key, False)]
            print(f'  {key:42} {v:5} same' + (f', {bad} differ' if bad else ''))
    for (key, ok), v in sorted(totals.items()):
        if not ok and not totals[(key, True)]:
            print(f'  {key:42} {v:5} differ')
    for key, ex in examples.items():
        for e in ex:
            print(f'    {key}: {e}')

    if args.no_snr:
        return 0
    started = time.time()
    groups = [jobs[i :: args.workers * 4] for i in range(args.workers * 4)]
    results = {}
    with ProcessPoolExecutor(args.workers) as pool:
        for group, out in zip(groups, pool.map(quality, [[j[1:] for j in g] for g in groups]), strict=True):
            for j, q in zip(group, out, strict=True):
                results[j[0]] = q
    snr = np.array(list(results.values()))
    finite = np.where(np.isfinite(snr), snr, np.nan)
    print(f'SNR over {len(snr)} clips, dB (decoded in {time.time() - started:.1f} s):')
    for i, name in enumerate(('ours vs source', 'Unity vs source', 'ours vs Unity')):
        col = finite[:, i]
        p = np.nanpercentile(col, [1, 10, 50, 90])
        spread = f'p1 {p[0]:6.2f}  p10 {p[1]:6.2f}  median {p[2]:6.2f}  p90 {p[3]:6.2f}'
        print(f'  {name:16} mean {np.nanmean(col):6.2f}  {spread}')
    better = np.sum(finite[:, 0] >= finite[:, 1])
    print(f'  ours at least as close to the source as Unity on {better} of {len(snr)} clips')
    worst = sorted(results.items(), key=lambda kv: kv[1][2])[: args.show]
    for path, q in worst:
        print(f'  lowest ours vs Unity: {path}: {q[2]:.2f} dB (ours/source {q[0]:.2f}, Unity/source {q[1]:.2f})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
