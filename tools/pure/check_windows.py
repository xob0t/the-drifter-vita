"""Compare the Windows data build.py wrote with the Windows reference build, object by object.

    uv run tools/pure/check_windows.py [--out dir] [--only file,...] [--class Name] [-v]

For each serialized file: type table and externals differences, objects missing on either side,
and per class how many objects are byte-identical. With --class, the first differing fields of
that class's objects (decoded with UnityPy).
"""

import argparse
import collections
import sys
from pathlib import Path

import paths
from build import TARGETS
from container import parse_serialized

TARGET = TARGETS['windows']
OUT = paths.ROOT / 'builds' / f'drifter-{paths.TARGET}-pure' / TARGET.data_dir


def serialized_names(folder):
    names = [p.name for p in folder.iterdir() if p.is_file() and p.suffix not in ('.resS', '.resource')]
    names = [n for n in names if n.startswith(('level', 'sharedassets', 'resources.', 'globalgamemanagers'))]
    return [*sorted(names), 'Resources/unity_builtin_extra']


def class_names():
    from UnityPy.enums import ClassIDType

    return lambda cid: ClassIDType(cid).name if cid in ClassIDType._value2member_map_ else str(cid)


def field_diff(a, b, path='', out=None, limit=8):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a.keys() | b.keys():
            field_diff(a.get(k), b.get(k), f'{path}.{k}', out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f'{path}: {len(a)} items vs {len(b)}')
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            field_diff(x, y, f'{path}[{i}]', out, limit)
    elif a != b:
        out.append(f'{path}: {str(a)[:80]} vs {str(b)[:80]}')
    return out


def compare_file(name, mine_dir, ref_dir, detail=None, verbose=False):
    name_of = class_names()
    mine, ref = (mine_dir / name).read_bytes(), (ref_dir / name).read_bytes()
    if mine == ref:
        return True, f'{name}: identical'
    a, b = parse_serialized(mine), parse_serialized(ref)
    lines = [f'{name}: differs ({len(mine)} vs {len(ref)} bytes)']
    if a.header.target_platform != b.header.target_platform:
        lines.append(f'  platform {a.header.target_platform} vs {b.header.target_platform}')
    ea, eb = [e.path for e in a.externals], [e.path for e in b.externals]
    if ea != eb:
        lines.append(f'  externals {ea} vs {eb}')
    ta = [(t.class_id, t.script_id, bytes(t.old_type_hash or b'')) for t in a.types]
    tb = [(t.class_id, t.script_id, bytes(t.old_type_hash or b'')) for t in b.types]
    if ta != tb:
        diffs = [(i, x[0], y[0]) for i, (x, y) in enumerate(zip(ta, tb, strict=False)) if x != y]
        lines.append(f'  types {len(ta)} vs {len(tb)}; first differing entries {diffs[:5]}')
    oa = {o.path_id: o for o in a.objects}
    ob = {o.path_id: o for o in b.objects}
    only_mine, only_ref = sorted(oa.keys() - ob.keys()), sorted(ob.keys() - oa.keys())
    if only_mine or only_ref:
        lines.append(
            f'  objects only here {only_mine[:10]} ({len(only_mine)}), '
            f'only in reference {only_ref[:10]} ({len(only_ref)})'
        )
    per = collections.defaultdict(lambda: [0, 0])
    differing = collections.defaultdict(list)
    for pid in sorted(oa.keys() & ob.keys()):
        cid = b.types[ob[pid].type_id].class_id
        same = oa[pid].data == ob[pid].data
        per[cid][0] += same
        per[cid][1] += 1
        if not same:
            differing[cid].append(pid)
    bad = {name_of(c): f'{s}/{n}' for c, (s, n) in sorted(per.items()) if s != n}
    if bad:
        lines.append(f'  objects that differ, by class (identical/total): {bad}')
    if detail:
        import UnityPy

        ua = next(iter(UnityPy.load(str(mine_dir / name)).files.values())).objects
        ub = next(iter(UnityPy.load(str(ref_dir / name)).files.values())).objects
        for cid, pids in differing.items():
            if name_of(cid) not in detail:
                continue
            for pid in pids[: 3 if not verbose else 50]:
                try:
                    diff = field_diff(ua[pid].read_typetree(), ub[pid].read_typetree())
                except Exception as e:
                    diff = [f'unreadable: {e}']
                lines.append(f'    {name_of(cid)} {pid}: ' + '; '.join(diff))
    return False, '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--only', default='')
    parser.add_argument('--class', dest='detail', default='')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()
    only = set(filter(None, args.only.split(',')))
    detail = set(filter(None, args.detail.split(',')))
    same = total = 0
    for name in serialized_names(TARGET.reference):
        if only and name not in only:
            continue
        if not (args.out / name).exists():
            print(f'{name}: not written')
            continue
        ok, text = compare_file(name, args.out, TARGET.reference, detail, args.verbose)
        same += ok
        total += 1
        if not ok or args.verbose:
            print(text)
    print(f'{same}/{total} files identical')
    return 0 if same == total else 1


if __name__ == '__main__':
    sys.exit(main())
