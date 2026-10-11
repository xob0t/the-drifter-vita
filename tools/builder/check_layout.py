"""Compare layout.py's files with the reference build: which objects each holds, in what order.

    uv run tools/builder/check_layout.py [file names...] [--show 5]

Objects are compared by class and, for assets, name, in path ID order (PreloadData aside).
"""

import argparse
import collections
import re
import sys

import paths
from layout import COMPONENT_CLASSES, Layout

NAME = re.compile(rb'^\s*m_Name: (.*)$', re.M)


def reference_sequence(file_name):
    """[(class ID, name)] of a reference file's objects in path ID order, without PreloadData."""
    import UnityPy

    env = UnityPy.load(str(paths.REFERENCE_MEDIA / file_name))
    sf = next(iter(env.files.values()))
    out = []
    for pid in sorted(sf.objects):
        o = sf.objects[pid]
        if o.class_id == 150:
            continue
        name = ''
        if o.class_id not in COMPONENT_CLASSES:
            try:
                name = o.peek_name() or ''
            except Exception:
                name = '?'
        out.append((o.class_id, name))
    return out


def our_sequence(layout, file_name):
    out = []
    names = {}
    for g, fid, cid in layout.objects(file_name):
        if g not in layout.db.by_guid:
            out.append((cid, '' if cid == 48 else {10754: 'Sprites-Default', 10758: 'Sprites-Mask'}.get(fid, str(fid))))
            continue
        a = layout.db.by_guid[g]
        name = ''
        if cid not in COMPONENT_CLASSES:
            if a.is_yaml:
                if g not in names:
                    text = (paths.PROJECT / a.path).read_bytes()
                    docs = re.split(rb'(?m)^--- !u!\d+ &(-?\d+)', text)[1:]
                    names[g] = {
                        int(docs[i]): (m[1].decode().strip() if (m := NAME.search(docs[i + 1])) else '')
                        for i in range(0, len(docs), 2)
                    }
                name = names[g].get(fid, '')
            else:
                name = a.name_of(fid)
        out.append((cid, name))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='*')
    parser.add_argument('--show', type=int, default=4)
    args = parser.parse_args()
    layout = Layout()
    files = args.files or [
        *sorted(
            p.name
            for p in paths.REFERENCE_MEDIA.iterdir()
            if p.name.startswith('sharedassets') and p.suffix == '.assets'
        ),
        'resources.assets',
        'globalgamemanagers.assets',
    ]
    totals = collections.Counter()
    for f in files:
        ref, ours = reference_sequence(f), our_sequence(layout, f)
        if ref == ours:
            totals['exact'] += 1
            continue
        rc, oc = collections.Counter(ref), collections.Counter(ours)
        missing, extra = rc - oc, oc - rc
        status = 'order' if not missing and not extra else 'content'
        totals[status] += 1
        print(f'{f}: {status}; ref {len(ref)} ours {len(ours)}')
        if missing:
            print('   missing:', list(missing.items())[: args.show])
        if extra:
            print('   extra:  ', list(extra.items())[: args.show])
        if status == 'order':
            i = next(i for i, (a, b) in enumerate(zip(ref, ours, strict=False)) if a != b)
            print(f'   first difference at {i}: ref {ref[i : i + 3]} ours {ours[i : i + 3]}')
    print(dict(totals))
    return 0


if __name__ == '__main__':
    sys.exit(main())
