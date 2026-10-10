"""Build the PS Vita player's data from the translated project, without Unity.

    uv run tools/pure/build.py [--out dir] [--only level0,sharedassets1.assets,...] [--compare]

Writes Media/ under the output folder. --compare checks every written file against the
reference build byte for byte and reports the first difference.
"""

import argparse
import collections
import sys
import time
from typing import ClassVar

import animation
import mixer
import classes
import convert
import paths
import scene as scenelib
import scripts as scriptlib
import typehash
import UnityPy
from layout import Layout
from placement import AssetContext, Placement, first_path_id
from writer import SerializedWriter, TypeHashes

DEFAULT_RESOURCES = '0000000000000000e000000000000000'


class SceneContext(AssetContext):
    """References from a scene: its own objects by fileID, assets through the placement."""

    def __init__(self, placement, file_name, pid_of, scripts, monoscripts):
        super().__init__(placement, file_name, None, scripts, monoscripts)
        self.pid_of = pid_of

    def pptr(self, ref, node):
        fid = int(ref.get('fileID', 0))
        if fid == 0:
            return {'m_FileID': 0, 'm_PathID': 0}
        if ref.get('guid') in (None, '0' * 32):
            return {'m_FileID': 0, 'm_PathID': self.pid_of.get(fid, 0)}
        return super().pptr(ref, node)


class Build:
    def __init__(self, out):
        self.out = out
        self.media = out / 'Media'
        self.layout = Layout()
        shared = [f'sharedassets{i}.assets' for i in range(len(self.layout.scenes))]
        self.files = [*shared, 'resources.assets', 'globalgamemanagers.assets']
        self.placement = Placement(self.layout, self.files)
        self.scripts = scriptlib.Scripts()
        self.project = classes.Project(paths.PROJECT)
        self.monoscripts = self.reference_monoscripts()  # until managers.py builds globalgamemanagers.assets
        self.trees = {}
        version = convert.VERSION

        def script_info(script):
            model = self.scripts.model(script)
            asm, ns, name = script
            return typehash.script_id(name, ns, asm), typehash.script_hash(model, version), self.monoscripts[script]

        self.hashes = TypeHashes(script_info, self.texture_sprite_hashes())

    @staticmethod
    def reference_monoscripts():
        env = UnityPy.load(str(paths.REFERENCE_MEDIA / 'globalgamemanagers.assets'))
        out = {}
        for o in env.objects:
            if o.type.name == 'MonoScript':
                t = o.read_typetree()
                out[(t['m_AssemblyName'], t['m_Namespace'], t['m_ClassName'])] = o.path_id
        return out

    @staticmethod
    def texture_sprite_hashes():
        """Texture2D and Sprite type hashes. UnityPy's TPK trees for these two differ from the
        player's, so they come from the PS Vita player's own unity default resources."""
        from container import parse_serialized

        player = paths.PSP2_PLAYER / 'Data' / 'Resources' / 'unity default resources'
        out = {}
        for t in parse_serialized(player.read_bytes()).types:
            if t.class_id in (28, 213):
                out[t.class_id] = t.old_type_hash
        return out

    def script_tree(self, script):
        if script not in self.trees:
            model = script and self.scripts.model(script)
            self.trees[script] = self.scripts.release_tree(model) if model else None
        return self.trees[script]

    def level(self, index):
        name = f'level{index}'
        sc = scenelib.Scene(paths.PROJECT / self.layout.scenes[index]['path'])
        objects = sc.objects()
        pid_of = {o.doc.file_id: o.path_id for o in objects}
        ctx = SceneContext(self.placement, name, pid_of, self.scripts, self.monoscripts)
        mbs = [o.doc for o in objects if o.doc.class_id == 114]
        self.scripts.load_models({self.scripts.resolve(d.fields['m_Script'], d.fields) for d in mbs} - {None})
        out = SerializedWriter(name)
        for o in objects:
            cid, doc = o.doc.class_id, o.doc
            tree, script = None, None
            if cid == 114:
                script = self.scripts.resolve(doc.fields['m_Script'], doc.fields)
                tree = self.script_tree(script)
            ctx.fields = doc.fields
            data, _ = convert.convert(cid, doc.fields, ctx, tree)
            if cid in classes.FIXUPS:
                classes.FIXUPS[cid](data, doc, self.project, sc)
            out.add(o.path_id, cid, data, tree, script)
        return out.write(self.hashes)

    # Builders the other modules provide, by class: f(build, file_name, guid, file_id, asset) ->
    # (data, tree) or None while that class is not done yet. Stream data goes through self.streams.
    IMPORTED: ClassVar[dict] = {}

    def shared(self, file_name):
        """A sharedassetsN.assets / resources.assets / globalgamemanagers.assets file. Returns (bytes,
        classes that could not be built yet)."""
        objects = self.layout.objects(file_name)
        start = first_path_id(file_name)
        out = SerializedWriter(file_name)
        missing = collections.Counter()
        db = self.layout.db
        mbs = [db.by_guid[g].docs[fid] for g, fid, cid in objects if cid == 114]
        self.scripts.load_models({self.scripts.resolve(d.fields['m_Script'], d.fields) for d in mbs} - {None})
        for i, (g, fid, cid) in enumerate(objects):
            a = db.by_guid.get(g)
            tree, script, data = None, None, None
            if a is not None and a.is_yaml:
                doc = a.docs[fid]
                ctx = AssetContext(self.placement, file_name, g, self.scripts, self.monoscripts)
                ctx.fields = doc.fields
                if cid == 74:
                    data = animation.clip(doc, ctx)
                elif cid == 91:
                    data = animation.controller(doc, ctx, a, db)
                elif cid == 241:
                    data = mixer.mixer(doc, ctx, a)
                else:
                    if cid == 114:
                        script = self.scripts.resolve(doc.fields['m_Script'], doc.fields)
                        tree = self.script_tree(script)
                    data, _ = convert.convert(cid, doc.fields, ctx, tree)
                    if cid in classes.FIXUPS:
                        classes.FIXUPS[cid](data, doc, self.project, None)
                    if cid in classes.ASSET_FIXUPS:
                        classes.ASSET_FIXUPS[cid](data, doc, a)
            elif cid in self.IMPORTED:
                made = self.IMPORTED[cid](self, file_name, g, fid, a)
                if made:
                    data, tree = made
            if data is None:
                missing[cid] += 1
                continue
            out.add(start + i, cid, data, tree, script)
        return out.write(self.hashes), missing

    def write(self, name, data):
        path = self.media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def compare(name, data):
    ref = (paths.REFERENCE_MEDIA / name).read_bytes()
    if ref == data:
        return 'identical'
    first = next((i for i, (a, b) in enumerate(zip(ref, data, strict=False)) if a != b), min(len(ref), len(data)))
    return f'differs at byte {first} (sizes {len(data)} vs reference {len(ref)})'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=lambda p: __import__('pathlib').Path(p), default=paths.OUT)
    parser.add_argument('--only', default='')
    parser.add_argument('--compare', action='store_true')
    args = parser.parse_args()
    build = Build(args.out)
    only = set(filter(None, args.only.split(',')))
    for i in range(len(build.layout.scenes)):
        name = f'level{i}'
        if only and name not in only:
            continue
        start = time.time()
        data = build.level(i)
        build.write(name, data)
        note = compare(name, data) if args.compare else ''
        print(f'{name}: {len(data)} bytes, {time.time() - start:.1f} s {note}')
    for name in build.files:
        if only and name not in only:
            continue
        start = time.time()
        data, missing = build.shared(name)
        build.write(name, data)
        note = compare(name, data) if args.compare else ''
        gaps = f'; not built yet: {dict(missing)}' if missing else ''
        print(f'{name}: {len(data)} bytes, {time.time() - start:.1f} s {note}{gaps}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
