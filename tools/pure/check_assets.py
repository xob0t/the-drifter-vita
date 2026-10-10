"""Convert the YAML assets of the shared files and compare them with the reference build.

    uv run tools/pure/check_assets.py [file names...] [--class N] [--show 3]

Objects pair by path ID (layout.py reproduces the reference order); references compare as
(file, path ID).
"""

import argparse
import collections
import sys

import classes
import convert
import paths
import scripts as scriptlib
import UnityPy
from layout import Layout
from placement import AssetContext, Placement, first_path_id, same


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='*')
    parser.add_argument('--class', dest='only', type=int)
    parser.add_argument('--show', type=int, default=3)
    args = parser.parse_args()
    layout = Layout()
    shared = sorted(p.name for p in paths.REFERENCE_MEDIA.glob('sharedassets*.assets'))
    everything = [*shared, 'resources.assets', 'globalgamemanagers.assets']
    placement = Placement(layout, everything)
    project = classes.Project(paths.PROJECT)
    lib = scriptlib.Scripts()
    trees = {}
    totals, examples = collections.Counter(), collections.defaultdict(list)
    for f in args.files or [*shared, 'resources.assets']:
        env = UnityPy.load(str(paths.REFERENCE_MEDIA / f))
        sf = next(iter(env.files.values()))
        externals = [''] + [e.path for e in sf.externals]
        start = first_path_id(f)
        objs = layout.objects(f)
        mbs = [(g, fid) for g, fid, cid in objs if cid == 114]
        lib.load_models({lib.resolve(layout.db.by_guid[g].docs[fid].fields['m_Script']) for g, fid in mbs} - {None})
        for i, (g, fid, cid) in enumerate(objs):
            if args.only and cid != args.only:
                continue
            a = layout.db.by_guid.get(g)
            if a is None:
                totals[(cid, 'built-in: not converted yet')] += 1
                continue
            if not a.is_yaml:
                totals[(cid, 'imported: not converted yet')] += 1
                continue
            ref = sf.objects.get(start + i)
            if ref is None or ref.class_id != cid:
                totals[(cid, 'unpaired')] += 1
                continue
            doc = a.docs[fid]
            tree = None
            if cid == 114:
                script = lib.resolve(doc.fields['m_Script'])
                if script not in trees:
                    model = script and lib.model(script)
                    try:
                        trees[script] = lib.release_tree(model) if model else None
                    except ValueError:
                        trees[script] = None
                tree = trees[script]
                if tree is None:
                    totals[(cid, 'no model')] += 1
                    continue
            try:
                theirs = ref.read_typetree(nodes=tree, check_read=tree is not None) if tree else ref.read_typetree()
            except Exception as error:
                totals[(cid, 'reference does not read', str(error)[:50])] += 1
                continue
            try:
                mine, _ = convert.convert(cid, doc.fields, AssetContext(placement, f, g), tree)
            except (TypeError, ValueError, KeyError) as error:
                totals[(cid, 'convert error', str(error)[:70])] += 1
                continue
            if cid in classes.FIXUPS:
                classes.FIXUPS[cid](mine, doc, project, None)
            if cid in classes.ASSET_FIXUPS:
                classes.ASSET_FIXUPS[cid](mine, doc, a)
            diffs = []
            same(mine, theirs, '', diffs, externals, f)
            totals[(cid, 'same' if not diffs else 'differs')] += 1
            for d in diffs:
                key = (cid, d[0].split('[')[0])
                totals[('field', key)] += 1
                if len(examples[key]) < args.show:
                    examples[key].append((f, a.path, d[1], d[2]))
    for k, v in sorted((k, v) for k, v in totals.items() if k[0] != 'field'):
        print(k, v)
    print('fields that differ:')
    for k, v in sorted(((k[1], v) for k, v in totals.items() if k[0] == 'field'), key=lambda x: -x[1])[:60]:
        print(f'  {v:6} {k}')
        for e in examples[k]:
            print(f'           {e[0]} {e[1][-50:]}: ours {str(e[2])[:90]} | ref {str(e[3])[:90]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
