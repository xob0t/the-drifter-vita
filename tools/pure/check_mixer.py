"""Bake every AudioMixerController the layout places in the shared files and compare it with the
reference build's.

    uv run tools/pure/check_mixer.py [file names...]

Objects pair by path ID as in check_assets.py; every field is compared, m_MixerConstant down to
each snapshot value.
"""

import argparse
import sys

import mixer
import paths
import UnityPy
from layout import Layout
from placement import AssetContext, Placement, first_path_id, same


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='*')
    args = parser.parse_args()
    layout = Layout()
    shared = sorted(p.name for p in paths.REFERENCE_MEDIA.glob('sharedassets*.assets'))
    everything = [*shared, 'resources.assets', 'globalgamemanagers.assets']
    placement = Placement(layout, everything)
    checked = failed = 0
    for f in args.files or [*shared, 'resources.assets']:
        start = first_path_id(f)
        objs = [(start + i, g, fid) for i, (g, fid, cid) in enumerate(layout.objects(f)) if cid == 241]
        if not objs:
            continue
        sf = next(iter(UnityPy.load(str(paths.REFERENCE_MEDIA / f)).files.values()))
        externals = [''] + [e.path for e in sf.externals]
        for pid, g, fid in objs:
            a = layout.db.by_guid[g]
            ref = sf.objects.get(pid)
            checked += 1
            if ref is None or ref.class_id != 241:
                print(f'{f} {pid} {a.path}: no AudioMixerController there in the reference')
                failed += 1
                continue
            mine = mixer.mixer(a.docs[fid], AssetContext(placement, f, g), a)
            theirs = ref.read_typetree()
            diffs = []
            same(mine, theirs, '', diffs, externals, f)
            constant = theirs['m_MixerConstant']
            print(
                f'{f} {pid} {a.path}: {len(constant["groups"])} groups, {len(constant["effects"])} effects, '
                f'{len(constant["snapshots"])} snapshots of {len(constant["snapshots"][0]["values"])} values, '
                f'{len(constant["exposedParameterNames"])} exposed: '
                + ('same' if not diffs else f'{len(diffs)} fields differ')
            )
            for path, ours, ref_value in diffs[:20]:
                print(f'    {path}: ours {str(ours)[:80]} | ref {str(ref_value)[:80]}')
            failed += bool(diffs)
    print(f'{checked} mixers, {checked - failed} same, {failed} differ')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
