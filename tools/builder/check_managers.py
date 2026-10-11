"""Compare managers.py with the reference build: globalgamemanagers, the MonoScripts of
globalgamemanagers.assets, and every PreloadData.

    uv run tools/builder/check_managers.py [--show 5] [--skip-preload | --only-preload]

Objects pair by path ID; references compare as (file, path ID). Lists whose order is the editor's
instance ID or hash map order (MonoManager.m_Scripts, ScriptMapper, BuildSettings.runtimeClassHashes,
ResourceManager.m_DependentAssets, RuntimeInitializeOnLoadManager) are also compared as sets.
"""

import argparse
import collections
import sys

import managers
import paths
import UnityPy
from layout import Layout
from placement import Placement, same

UNORDERED = {
    (116, 'm_Scripts'),
    (94, 'm_Shaders.m_ObjectToName'),
    (141, 'runtimeClassHashes'),
    (147, 'm_DependentAssets'),
}


def load(file_name):
    env = UnityPy.load(str(paths.REFERENCE_MEDIA / file_name))
    sf = next(iter(env.files.values()))
    return sf, [''] + [e.path for e in sf.externals]


def resolve(v, externals, here):
    """A reference dict's (file, path ID); other values as a hashable form."""
    if isinstance(v, dict) and 'm_PathID' in v:
        if not v['m_PathID']:
            return ('', 0)
        if 'file' in v:
            return (v['file'], v['m_PathID'])
        return (externals[v['m_FileID']] if v['m_FileID'] else here, v['m_PathID'])
    if isinstance(v, dict):
        return tuple((k, resolve(x, externals, here)) for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return tuple(resolve(x, externals, here) for x in v)
    return v


def get(d, path):
    for part in path.split('.'):
        d = d[part]
    return d


def check_monoscripts(entries, show):
    sf, externals = load(managers.GGA)
    theirs = {pid: o.read_typetree() for pid, o in sf.objects.items() if o.class_id == 115}
    totals = collections.Counter()
    by_name = collections.Counter((t['m_AssemblyName'], t['m_Namespace'], t['m_ClassName']) for t in theirs.values())
    ours_names = collections.Counter((s.assembly_name, s.type.namespace, s.type.name) for s in entries)
    print(f'MonoScripts: ours {len(entries)}, reference {len(theirs)}')
    print('  classes missing:', sorted((by_name - ours_names).elements())[:show])
    print('  classes extra:  ', sorted((ours_names - by_name).elements())[:show])
    fields = collections.Counter()
    for s in entries:
        t = theirs.get(s.path_id)
        if t is None:
            totals['no reference object at path ID'] += 1
            continue
        diffs = []
        same(s.fields, t, '', diffs, externals, managers.GGA)
        totals['same' if not diffs else 'differs'] += 1
        for d in diffs:
            fields[d[0]] += 1
            if fields[d[0]] <= show:
                print(f'  {s.path_id} {s.type.full_name}: {d[0]} ours {d[1]} ref {d[2]}')
    print('  per object:', dict(totals), 'fields that differ:', dict(fields))


def check_managers(objects, show):
    sf, externals = load(managers.GGM)
    print(f'globalgamemanagers: ours {[(p, c) for p, c, _ in objects]}')
    print(f'                    ref  {sorted((p, o.class_id) for p, o in sf.objects.items())}')
    print(f'  reference externals: {externals[1:]}')
    for pid, cid, mine in objects:
        o = sf.objects.get(pid)
        if o is None or o.class_id != cid:
            print(f'  {pid} class {cid}: no reference object')
            continue
        theirs = o.read_typetree()
        diffs = []
        same(mine, theirs, '', diffs, externals, managers.GGM)
        unordered = [d for d in diffs if any(d[0].startswith(f'.{p}') for c, p in UNORDERED if c == cid)]
        rest = [d for d in diffs if d not in unordered]
        notes = []
        for c, p in UNORDERED:
            if c != cid:
                continue
            a = collections.Counter(resolve(get(mine, p), externals, managers.GGM))
            b = collections.Counter(resolve(get(theirs, p), externals, managers.GGM))
            in_order = not any(d[0].startswith(f'.{p}') for d in diffs)
            notes.append(
                f'{p}: {sum(a.values())} vs {sum(b.values())}, {sum((a & b).values())} in common'
                + (', same order' if in_order else ', order differs')
            )
            notes += [f'    only ours: {str(x)[:150]}' for x in list((a - b).elements())[:show]]
            notes += [f'    only ref:  {str(x)[:150]}' for x in list((b - a).elements())[:show]]
        if cid == 300:
            a, b = initialize_methods(mine), initialize_methods(theirs)
            notes.append(f'methods (class order aside): {len(a)} vs {len(b)}, {len(a & b)} in common')
        leaves = count_leaves(theirs)
        print(f'  {pid} {o.type.name}: {leaves - len(rest)}/{leaves} values match' + ('' if rest else ' (all)'))
        for n in notes:
            print(f'    {n}')
        for d in rest[:show]:
            print(f'    {d[0]}: ours {str(d[1])[:100]} | ref {str(d[2])[:100]}')
        if len(rest) > show:
            print(f'    ... {len(rest) - show} more')


def initialize_methods(d):
    """RuntimeInitializeOnLoadManager's methods as (assembly, namespace, class, method, before)."""
    before = set(d['m_BeforeMethodExecutionOrders'])
    out = set()
    for i, m in enumerate(d['m_ClassMethodInfos']):
        c = d['m_ClassInfos'][m['m_ClassIndex']]
        names = (d['m_AssemblyNames'][c['m_AssemblyNameIndex']], d['m_NamespaceNames'][c['m_NamespaceIndex']])
        out.add((*names, c['m_ClassName'], m['m_MethodName'], i in before))
    return out


def count_leaves(v):
    if isinstance(v, dict) and not ('m_PathID' in v and 'm_FileID' in v):
        return sum(count_leaves(x) for x in v.values()) or 1
    if isinstance(v, (list, tuple)):
        return sum(count_leaves(x) for x in v) or 1
    return 1


def check_preload(layout, placement, entries, show):
    import preload as preloadlib

    totals = collections.Counter()
    builder = preloadlib.Preload(layout, placement)
    # The editor adds DrifterCompat.CrtImageEffect to the GUI camera while it saves each level for
    # the build (scene.py does not), so the reference preloads its MonoScript; set it aside.
    added = {(managers.GGA, s.path_id) for s in entries if s.type.full_name == 'DrifterCompat.CrtImageEffect'}
    shared = sorted((p.name for p in paths.REFERENCE_MEDIA.glob('sharedassets*.assets')), key=lambda n: int(n[12:-7]))
    files = [*shared, managers.GGA]
    for f in files:
        sf, externals = load(f)
        o = sf.objects.get(1)
        if o is None or o.class_id != 150:
            totals['reference has no PreloadData'] += 1
            continue
        theirs = o.read_typetree()
        mine = builder.preload(f)
        a = [resolve(p, externals, f) for p in mine['m_Assets']]
        b = [resolve(p, externals, f) for p in theirs['m_Assets']]
        common = len(set(a) & set(b))
        totals['entries ref'] += len(b)
        totals['entries ours'] += len(a)
        totals['entries in common'] += common
        status = 'exact' if a == b else 'same set' if set(a) == set(b) else 'differs'
        if status != 'exact' and a == [x for x in b if x not in added]:
            status = 'exact but the editor-added CrtImageEffect'
            totals['entries: editor-added CrtImageEffect'] += len(b) - len(a)
        totals[f'files {status}'] += 1
        others = {k: v for k, v in mine.items() if k != 'm_Assets'} != {
            k: v for k, v in theirs.items() if k != 'm_Assets'
        }
        if others:
            totals['other fields differ'] += 1
        if not status.startswith('exact') and show:
            missing = [x for x in b if x not in set(a)]
            extra = [x for x in a if x not in set(b)]
            print(f'  {f}: {status}, ours {len(a)} ref {len(b)} common {common}')
            print(f'     missing: {[builder.describe(x) for x in missing[:show]]}')
            print(f'     extra:   {[builder.describe(x) for x in extra[:show]]}')
    print('PreloadData:', dict(totals))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--show', type=int, default=5)
    parser.add_argument('--skip-preload', action='store_true')
    parser.add_argument('--only-preload', action='store_true')
    args = parser.parse_args()
    layout = Layout()
    shared = sorted(p.name for p in paths.REFERENCE_MEDIA.glob('sharedassets*.assets'))
    placement = Placement(layout, [*shared, 'resources.assets', managers.GGA])
    entries = managers.monoscripts(layout)
    managers.register(placement, entries)
    if not args.only_preload:
        check_monoscripts(entries, args.show)
        check_managers(managers.managers(layout, placement, entries), args.show)
    if not args.skip_preload:
        check_preload(layout, placement, entries, args.show)
    return 0


if __name__ == '__main__':
    sys.exit(main())
