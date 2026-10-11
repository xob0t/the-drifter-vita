"""Build the objects of imported textures, fonts and text assets and compare them with the
reference build: fields, the serialized object bytes, and the image data in the .resS streams.

    uv run tools/pure/check_imported.py [file names...] [--show 3]
    uv run --with quicktex tools/pure/check_imported.py      # Unity-like DXT quality

Objects pair by path ID (layout.py reproduces the reference order). Uncompressed image data must
match byte for byte; DXT data comes from a different encoder, so both are decoded and compared
with the source image instead (mean absolute error per channel over visible texels).
"""

import argparse
import collections
import copy
import sys
import time

import imported
import numpy as np
import paths
import texture2ddecoder
import textures
import UnityPy
from layout import Layout
from placement import Placement, first_path_id, same
from UnityPy.helpers.TypeTreeHelper import write_typetree
from UnityPy.streams import EndianBinaryWriter

CLASSES = {28, 213, 128, 21, 49}


class Build:
    """What the hooks need from build.Build."""

    def __init__(self, placement):
        self.placement = placement
        self.streams = {}


def resolved(data, here, externals):
    """A copy with each reference's 'file' turned into the reference file's external index."""
    data = copy.deepcopy(data)

    def walk(v):
        if isinstance(v, dict):
            if 'm_PathID' in v and 'm_FileID' in v:
                f = v.pop('file', here)
                v['m_FileID'] = 0 if f == here or v['m_PathID'] == 0 else externals.index(f)
                return
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(data)
    return data


def serialize(data, class_id):
    import convert

    w = EndianBinaryWriter(endian='<')
    write_typetree(data, convert.release_tree(class_id), w)
    return w.bytes


def decode(data, w, h, fmt):
    decoder = texture2ddecoder.decode_bc3 if fmt == textures.DXT5 else texture2ddecoder.decode_bc1
    return np.frombuffer(decoder(data, w, h), np.uint8).reshape(h, w, 4)[..., [2, 1, 0, 3]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='*')
    parser.add_argument('--show', type=int, default=3)
    args = parser.parse_args()
    started = time.time()
    layout = Layout()
    shared = sorted(p.name for p in paths.REFERENCE_MEDIA.glob('sharedassets*.assets'))
    everything = [*shared, 'resources.assets', 'globalgamemanagers.assets']
    build = Build(Placement(layout, everything))
    totals, examples = collections.Counter(), collections.defaultdict(list)
    quality = collections.defaultdict(list)  # format -> [(ours, reference, texels)]
    print(f'DXT encoder: {textures.ENCODER_NAME}')
    for f in args.files or everything:
        objs = layout.objects(f)
        ours_here = [
            (i, g, fid, cid)
            for i, (g, fid, cid) in enumerate(objs)
            if cid in CLASSES and layout.db.by_guid.get(g) is not None and not layout.db.by_guid[g].is_yaml
        ]
        if not ours_here:
            continue
        env = UnityPy.load(str(paths.REFERENCE_MEDIA / f))
        sf = next(iter(env.files.values()))
        externals = [''] + [e.path for e in sf.externals]
        start = first_path_id(f)
        for i, g, fid, cid in ours_here:
            a = layout.db.by_guid[g]
            ref = sf.objects.get(start + i)
            if ref is None or ref.class_id != cid:
                totals[(cid, 'unpaired')] += 1
                continue
            try:
                made = imported.build_object(build, f, g, fid, a)
            except NotImplementedError as error:
                totals[(cid, 'not supported', str(error)[:60])] += 1
                continue
            if made is None:
                totals[(cid, 'not built')] += 1
                continue
            mine = made[0]
            theirs = ref.read_typetree()
            diffs = []
            same(mine, theirs, '', diffs, externals, f)
            raw = serialize(resolved(mine, f, externals), cid) == ref.get_raw_data()
            totals[(cid, 'same' if not diffs else 'differs', 'bytes same' if raw else 'bytes differ')] += 1
            for d in diffs:
                key = (cid, d[0].split('[')[0])
                totals[('field', key)] += 1
                if len(examples[key]) < args.show:
                    examples[key].append((f, a.path, d[1], d[2]))
            if cid == 28 and theirs['m_ImageCount']:
                compare_pixels(a, mine, theirs, build, totals, quality, examples, args.show)
    for name, s in sorted(build.streams.items()):
        ref = (paths.REFERENCE_MEDIA / name).read_bytes()
        totals[('stream', 'size same' if len(ref) == len(s) else 'size differs')] += 1
    for k, v in sorted(((k, v) for k, v in totals.items() if k[0] != 'field'), key=str):
        print(k, v)
    print('fields that differ:')
    for k, v in sorted(((k[1], v) for k, v in totals.items() if k[0] == 'field'), key=lambda x: -x[1]):
        print(f'  {v:6} {k}')
        for e in examples[k]:
            print(f'           {e[0]} {e[1][-50:]}: ours {str(e[2])[:90]} | ref {str(e[3])[:90]}')
    for fmt, rows in sorted(quality.items()):
        r = np.array(rows, dtype=np.float64)
        mean = [np.average(r[:, c], weights=r[:, 4]) for c in range(4)]
        print(
            f'format {fmt}: {len(rows)} textures; mean |error| over visible texels, RGB / alpha: '
            f'ours {mean[0]:.3f} / {mean[1]:.3f}, Unity {mean[2]:.3f} / {mean[3]:.3f}'
        )
    print(f'{time.time() - started:.0f} s')
    return 0


def compare_pixels(asset, mine, theirs, build, totals, quality, examples, show):
    sd = theirs['m_StreamData']
    if sd['path']:
        with open(paths.REFERENCE_MEDIA / sd['path'], 'rb') as fh:
            fh.seek(sd['offset'])
            ref = fh.read(sd['size'])
        s = build.streams.get(mine['m_StreamData']['path'])
        o = mine['m_StreamData']
        ours = bytes(s[o['offset'] : o['offset'] + o['size']]) if s else b''
    else:
        ref, ours = bytes(theirs['image data']), bytes(mine['image data'])
    fmt = theirs['m_TextureFormat']
    if fmt not in (textures.DXT1, textures.DXT5):
        exact = ours == ref
        totals[('pixels', fmt, 'identical' if exact else 'differ')] += 1
        if not exact:
            key = ('pixels', fmt)
            if len(examples[key]) < show:
                examples[key].append(('', asset.path, len(ours), len(ref)))
            totals[('field', key)] += 1
        return
    if mine['m_TextureFormat'] != fmt or len(ours) != len(ref):
        totals[('pixels', fmt, 'not comparable')] += 1
        return
    w, h = theirs['m_Width'], theirs['m_Height']
    source = textures.load_rgba(paths.PROJECT / asset.path).astype(np.int32)
    visible = source[..., 3] > 0
    if not visible.any():
        return
    errors = []
    for data in (ours, ref):
        e = np.abs(decode(data, w, h, fmt).astype(np.int32) - source)[visible]
        errors += [e[:, :3].mean(), e[:, 3].mean()]
    quality[fmt].append((*errors, int(visible.sum())))
    totals[('pixels', fmt, 'decoded and compared')] += 1


if __name__ == '__main__':
    sys.exit(main())
