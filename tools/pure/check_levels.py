"""Convert scenes to level objects and compare them with the reference build's levelN files.

    uv run tools/pure/check_levels.py [level numbers...] [--class N] [--show 5]

Objects are paired by what they are (class, GameObject path, index among the GameObject's
components of that class), not by PathID, and references are compared the same way. References
to other files are only checked for being set or not until those files are built.
"""

import argparse
import collections
import math
import sys

import classes
import convert
import paths
import scene
import UnityPy
import unityyaml


def scene_paths():
    settings = unityyaml.meta(paths.PROJECT / 'ProjectSettings' / 'EditorBuildSettings.asset')
    return [
        paths.PROJECT / s['path'] for s in settings['EditorBuildSettings']['m_Scenes'] if s.get('enabled', '1') == '1'
    ]


class SceneContext:
    def __init__(self, pid_of):
        self.pid_of = pid_of

    def pptr(self, ref, node):
        fid = int(ref.get('fileID', 0))
        if fid == 0:
            return {'m_FileID': 0, 'm_PathID': 0}
        if 'guid' not in ref or ref.get('guid', '0' * 32) == '0' * 32:
            return {'m_FileID': 0, 'm_PathID': self.pid_of.get(fid, -fid)}
        return {'m_FileID': -1, 'm_PathID': fid, 'guid': ref['guid']}  # resolved when assets are laid out


def yaml_signatures(objs):
    docs = [o.doc for o in objs]
    names = scene.hierarchy_paths(docs)
    by_id = {d.file_id: d for d in docs}
    sig = {}
    for d in docs:
        if d.class_id == 1:
            sig[d.file_id] = (1, names[d.file_id], 0)
            seen = collections.Counter()
            for c in d.fields.get('m_Component', []):
                cid = int(next(iter(c.values()))['fileID'])
                comp = by_id.get(cid)
                if comp is None:
                    continue
                key = comp.class_id
                if comp.class_id == 114:
                    key = (114, comp.fields.get('m_Script', {}).get('fileID'))
                sig[cid] = (comp.class_id, names[d.file_id], seen[key])
                seen[key] += 1
        elif 'm_GameObject' not in d.fields:
            sig[d.file_id] = (d.class_id, '', 0)
    return sig


def reference_level(index):
    env = UnityPy.load(str(paths.REFERENCE_MEDIA / f'level{index}'))
    sf = next(iter(env.files.values()))
    objs = {}
    for pid, o in sf.objects.items():
        tree = None
        if o.type.name != 'MonoBehaviour':
            tree = o.read_typetree()
        objs[pid] = (o.class_id, tree, o)
    return objs


def reference_signatures(objs):
    go_name, parent_of, go_of_transform, comps, children = {}, {}, {}, {}, {}
    for pid, (cid, tree, _) in objs.items():
        if cid == 1:
            go_name[pid] = tree['m_Name']
            comps[pid] = [c['component']['m_PathID'] for c in tree['m_Component']]
        if cid in (4, 224):
            go_of_transform[pid] = tree['m_GameObject']['m_PathID']
            parent_of[tree['m_GameObject']['m_PathID']] = tree['m_Father']['m_PathID']
            children[pid] = [c['m_PathID'] for c in tree['m_Children']]
    names = {}

    def path(go):
        if go not in names:
            father = parent_of.get(go, 0)
            if father:
                siblings = [go_of_transform[c] for c in children[father]]
                same = [g for g in siblings if go_name[g] == go_name[go]]
                label = go_name[go] if len(same) < 2 else f'{go_name[go]}#{same.index(go)}'
                names[go] = path(go_of_transform[father]) + '/' + label
            else:
                names[go] = go_name[go]
        return names[go]

    sig = {}
    for go in go_name:
        sig[go] = (1, path(go), 0)
        seen = collections.Counter()
        for c in comps[go]:
            cid, tree, o = objs[c]
            key = cid
            if cid == 114:
                key = (114, o.parse_monobehaviour_head().m_Script.m_PathID)
            sig[c] = (cid, path(go), seen[key])
            seen[key] += 1
    for pid, (cid, tree, _) in objs.items():
        if pid not in sig and (tree is None or 'm_GameObject' not in tree):
            sig[pid] = (cid, '', 0)
    return sig


def same(a, b, ours_sig, ref_sig, path, diffs):
    if isinstance(a, dict) and set(a) >= {'m_FileID', 'm_PathID'}:
        if a.get('guid') is not None:
            ok = (b['m_FileID'] != 0) == (a['m_PathID'] != 0)
        elif a['m_PathID'] == 0 or b['m_PathID'] == 0:
            ok = a['m_PathID'] == b['m_PathID'] and b['m_FileID'] == 0
        else:
            ok = b['m_FileID'] == 0 and ours_sig.get(a['m_PathID']) == ref_sig.get(b['m_PathID'])
        if not ok:
            diffs.append((path, a, b))
        return
    if isinstance(a, dict):
        if not isinstance(b, dict) or set(a) != set(b):
            diffs.append((path, 'keys', sorted(set(a) ^ set(b or {}))))
            return
        for k in a:
            same(a[k], b[k], ours_sig, ref_sig, f'{path}.{k}', diffs)
        return
    if isinstance(a, (list, tuple)):
        if not isinstance(b, (list, tuple)) or len(a) != len(b):
            diffs.append((path, f'len {len(a)}', f'len {len(b) if isinstance(b, (list, tuple)) else b}'))
            return
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            same(x, y, ours_sig, ref_sig, f'{path}[{i}]', diffs)
        return
    if isinstance(a, float) and isinstance(b, float):
        if a == b or (math.isnan(a) and math.isnan(b)):
            return
    elif a == b:
        return
    diffs.append((path, a, b))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('levels', nargs='*', type=int)
    parser.add_argument('--class', dest='only', type=int)
    parser.add_argument('--show', type=int, default=3)
    args = parser.parse_args()
    scenes = scene_paths()
    totals = collections.Counter()
    project = classes.Project(paths.PROJECT)
    examples = collections.defaultdict(list)
    for index in args.levels or range(len(scenes)):
        sc = scene.Scene(scenes[index])
        ours = sc.objects()
        pid_of = {o.doc.file_id: o.path_id for o in ours}
        ctx = SceneContext(pid_of)
        ours_sig_by_fid = yaml_signatures(ours)
        ours_sig = {pid_of[f]: s for f, s in ours_sig_by_fid.items() if f in pid_of}
        ref = reference_level(index)
        ref_sig = reference_signatures(ref)
        ref_by_sig = {s: p for p, s in ref_sig.items()}
        for o in ours:
            cid = o.doc.class_id
            if cid == 114 or (args.only and cid != args.only):
                continue
            s = ours_sig.get(o.path_id)
            rp = ref_by_sig.get(s)
            if rp is None:
                totals[(cid, 'unpaired')] += 1
                continue
            mine, missing = convert.convert(cid, o.doc.fields, ctx)
            if cid in classes.FIXUPS:
                classes.FIXUPS[cid](mine, o.doc, project, sc)
            diffs = []
            same(mine, ref[rp][1], ours_sig, ref_sig, '', diffs)
            totals[(cid, 'same' if not diffs else 'differs')] += 1
            for d in diffs:
                key = (cid, d[0].split('[')[0])
                totals[('field', key)] += 1
                if len(examples[key]) < args.show:
                    examples[key].append((index, s, d[1], d[2], missing))
    for k, v in sorted((k, v) for k, v in totals.items() if k[0] != 'field'):
        print(k, v)
    print('fields that differ:')
    for k, v in sorted(((k[1], v) for k, v in totals.items() if k[0] == 'field'), key=lambda x: -x[1]):
        print(f'  {v:6} {k}')
        for e in examples[k]:
            print(f'           level{e[0]} {e[1]}: ours {str(e[2])[:100]} | ref {str(e[3])[:100]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
