"""Bake every AnimationClip and AnimatorController the layout places in the shared files and compare
them with the reference build's.

    uv run tools/builder/check_animation.py [file names...] [--class 74|91] [--show 3]

Objects pair by path ID as in check_assets.py. Where the object at that path ID is not the same
clip (class and name), the layout's order within the class disagrees with the reference; such
objects pair by name instead and are counted, so the bake is still checked. References compare as
(file, path ID); MonoScripts resolve through the reference's globalgamemanagers.assets, which is
not built yet.
"""

import argparse
import collections
import sys

import animation
import paths
import UnityPy
from layout import Layout
from placement import AssetContext, Placement, first_path_id, same
from scripts import Scripts


class ScriptContext(AssetContext):
    """AssetContext that also resolves MonoScript references (assembly, namespace, class)."""

    def __init__(self, placement, file_name, guid, scripts, monoscripts):
        super().__init__(placement, file_name, guid)
        self.scripts, self.monoscripts = scripts, monoscripts

    def pptr(self, ref, node):
        if ref.get('guid') in self.scripts.assemblies:
            key = self.scripts.resolve(ref)
            pid = self.monoscripts.get(key, f'unresolved {key}')
            return {'m_FileID': 0, 'm_PathID': pid, 'file': 'globalgamemanagers.assets'}
        return super().pptr(ref, node)


def reference_monoscripts():
    env = UnityPy.load(str(paths.REFERENCE_MEDIA / 'globalgamemanagers.assets'))
    out = {}
    for o in env.objects:
        if o.type.name == 'MonoScript':
            t = o.read_typetree()
            out[(t['m_AssemblyName'], t['m_Namespace'], t['m_ClassName'])] = o.path_id
    return out


class Identity:
    """Whether a PPtr that differs by (file, path ID) still names the same object: same class and
    name. Path IDs of other assets follow the layout's order, which is checked elsewhere."""

    def __init__(self, layout, placement):
        self.db = layout.db
        self.at = {v: k for k, v in placement.where.items()}
        self.files = {}

    def reference(self, file_name, pid):
        if file_name not in self.files:
            path = paths.REFERENCE_MEDIA / file_name
            self.files[file_name] = next(iter(UnityPy.load(str(path)).files.values())) if path.exists() else None
        sf = self.files[file_name]
        o = sf and sf.objects.get(pid)
        return o and (o.class_id, o.peek_name())

    def same_object(self, diff):
        _, mine, theirs = diff
        if not (isinstance(mine, tuple) and isinstance(theirs, tuple) and mine[0] == theirs[0] and mine in self.at):
            return False
        g, fid = self.at[mine]
        a = self.db.by_guid.get(g)
        return a is not None and self.reference(*theirs) == (a.classes.get(fid), a.name_of(fid))


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
    scripts, monoscripts = Scripts(), reference_monoscripts()
    makers = {74: animation.clip, 91: animation.controller}
    identity = Identity(layout, placement)
    totals, examples = collections.Counter(), collections.defaultdict(list)
    for f in args.files or [*shared, 'resources.assets']:
        start = first_path_id(f)
        objs = [(start + i, g, fid, cid) for i, (g, fid, cid) in enumerate(layout.objects(f)) if cid in makers]
        objs = [o for o in objs if not args.only or o[3] == args.only]
        if not objs:
            continue
        env = UnityPy.load(str(paths.REFERENCE_MEDIA / f))
        sf = next(iter(env.files.values()))
        externals = [''] + [e.path for e in sf.externals]
        theirs, by_name = {}, collections.defaultdict(list)
        for pid, o in sf.objects.items():
            if o.class_id in makers:
                theirs[pid] = o.read_typetree()
                by_name[(o.class_id, theirs[pid]['m_Name'])].append(pid)
        # Same-named objects stand in asset path order in the reference.
        ours_by_name = collections.defaultdict(list)
        for o in sorted(objs, key=lambda o: layout.db.by_guid[o[1]].path):
            ours_by_name[(o[3], layout.db.by_guid[o[1]].docs[o[2]].fields.get('m_Name', ''))].append(o)
        for pid, g, fid, cid in objs:
            a = layout.db.by_guid[g]
            doc = a.docs[fid]
            name = doc.fields.get('m_Name', '')
            ref = theirs.get(pid)
            if ref is None or ref['m_Name'] != name or sf.objects[pid].class_id != cid or len(by_name[(cid, name)]) > 1:
                rank = ours_by_name[(cid, name)].index((pid, g, fid, cid))
                candidates = sorted(by_name.get((cid, name), []))
                if rank >= len(candidates):
                    totals[(cid, 'unpaired')] += 1
                    continue
                if candidates[rank] != pid:
                    totals[(cid, 'paired by name: layout order differs')] += 1
                pid = candidates[rank]
                ref = theirs[pid]
            ctx = ScriptContext(placement, f, g, scripts, monoscripts)
            mine = makers[cid](doc, ctx) if cid == 74 else makers[cid](doc, ctx, a, layout.db)
            diffs = []
            same(mine, ref, '', diffs, externals, f)
            moved = [d for d in diffs if identity.same_object(d)]
            diffs = [d for d in diffs if d not in moved]
            if moved:
                totals[(cid, 'references the right objects at other path IDs (layout)')] += 1
            totals[(cid, 'same' if not diffs else 'differs')] += 1
            for d in diffs:
                key = (cid, d[0].split('[')[0])
                totals[('field', key)] += 1
                if len(examples[key]) < args.show:
                    examples[key].append((f, a.path, d[0], d[1], d[2]))
    for k, v in sorted((k, v) for k, v in totals.items() if k[0] != 'field'):
        print(k, v)
    print('fields that differ:')
    for k, v in sorted(((k[1], v) for k, v in totals.items() if k[0] == 'field'), key=lambda x: -x[1])[:60]:
        print(f'  {v:6} {k}')
        for e in examples[k]:
            print(f'           {e[0]} {e[1][-40:]} {e[2][-40:]}: ours {str(e[3])[:80]} | ref {str(e[4])[:80]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
