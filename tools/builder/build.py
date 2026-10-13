"""Build a player's data from the translated project, without Unity.

    uv run tools/builder/build.py [--platform vita|windows] [--out dir] [--only level0,...] [--compare]

Writes Media/ (PS Vita) or TheDrifter_Data/ (Windows) under the output folder. --compare checks
every written file against the platform's reference build byte for byte and reports the first
difference.

Targets: 'vita' builds work/drifter-<version>-vita for the PS Vita (header platform 30);
'windows' builds the PC project work/drifter-<version> for Windows x64 (header platform 19), with
Direct3D 11 shaders, PCM audio and the Windows managers (windows_build.py registers the class
builders in IMPORTED_FOR['windows']).
"""

import argparse
import collections
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import animation
import audio
import builtin_copies
import builtin_extra
import classes
import convert
import imported
import managers
import mixer
import paths
import preload as preloadlib
import progress
import scene as scenelib
import scripts as scriptlib
import shader_object
import typehash
from layout import Layout
from placement import AssetContext, Placement, first_path_id
from writer import SerializedWriter, TypeHashes

DEFAULT_RESOURCES = '0000000000000000e000000000000000'
WINDOWS_PLAYER = paths.INSTALLED_EDITOR_DATA / 'PlaybackEngines' / 'windowsstandalonesupport' / 'Variations'
WINDOWS_PLAYER = WINDOWS_PLAYER / 'win64_nondevelopment_mono'


@dataclass(frozen=True)
class Target:
    name: str
    platform: int  # BuildTarget in the serialized files' header
    project: Path  # the Unity project the data comes from
    data_dir: str  # the data folder under the output folder
    reference: Path  # the data folder of the build Unity made, for --compare
    default_resources: Path  # the player's 'unity default resources' (Texture2D and Sprite hashes)
    work: Path  # scratch space for caches keyed by path only


TARGETS = {
    'vita': Target(
        'vita',
        30,
        paths.PROJECT,
        'Media',
        paths.REFERENCE_MEDIA,
        paths.PSP2_PLAYER / 'Data' / 'Resources' / 'unity default resources',
        paths.WORK,
    ),
    'windows': Target(
        'windows',
        19,
        paths.common.WORK / f'drifter-{paths.TARGET}',
        'TheDrifter_Data',
        paths.ROOT / 'builds' / f'drifter-{paths.TARGET}' / 'TheDrifter_Data',
        WINDOWS_PLAYER / 'Data' / 'Resources' / 'unity default resources',
        paths.WORK / 'windows',
    ),
}


def use_target(target):
    """Point the modules that read paths.PROJECT at call time, or keep caches keyed by path
    under paths.WORK, at the target's project and scratch folder. The PS Vita target leaves them
    as they are; one process builds one target."""
    if target.name == 'vita':
        return
    import assets

    target.work.mkdir(parents=True, exist_ok=True)
    assets.PROJECT, assets.WORK = target.project, target.work  # Asset.docs, the asset index cache
    scriptlib.WORK = target.work  # Scripts.managed copies the project's DLLs here
    managers.WORK = target.work  # managers.managed_folder


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
    def __init__(self, out, platform='vita'):
        self.target = TARGETS[platform]
        use_target(self.target)
        self.root = self.target.project  # the Unity project; class builders read assets under it
        self.out = out
        self.streams = {}  # '<file>.resS' / '<file>.resource' -> bytearray the class builders append to
        self.media = out / self.target.data_dir
        self.layout = Layout(project=self.root)
        shared = [f'sharedassets{i}.assets' for i in range(len(self.layout.scenes))]
        self.files = [*shared, 'resources.assets', 'globalgamemanagers.assets']
        self.placement = Placement(self.layout, self.files)
        self.scripts = scriptlib.Scripts(self.root)
        self.project = classes.Project(self.root)
        self.entries = managers.monoscripts(self.layout, self.root)
        self.monoscripts = managers.monoscript_ids(self.entries)
        managers.register(self.placement, self.entries)
        self.preloader = preloadlib.Preload(self.layout, self.placement, self.root)
        self.imported = self.IMPORTED_FOR[platform]
        self.trees = {}
        version = convert.VERSION

        def script_info(script):
            model = self.scripts.model(script)
            asm, ns, name = script
            return typehash.script_id(name, ns, asm), typehash.script_hash(model, version), self.monoscripts[script]

        self.hashes = TypeHashes(script_info, self.texture_sprite_hashes(self.target.default_resources))

    @staticmethod
    def texture_sprite_hashes(player=TARGETS['vita'].default_resources):
        """Texture2D and Sprite type hashes. UnityPy's TPK trees for these two differ from the
        player's, so they come from the player's own unity default resources."""
        from container import parse_serialized

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
        sc = scenelib.Scene(self.root / self.layout.scenes[index]['path'])
        objects = sc.objects()
        pid_of = {o.doc.file_id: o.path_id for o in objects}
        ctx = SceneContext(self.placement, name, pid_of, self.scripts, self.monoscripts)
        mbs = [o.doc for o in objects if o.doc.class_id == 114]
        self.scripts.load_models({self.scripts.resolve(d.fields['m_Script'], d.fields) for d in mbs} - {None})
        out = SerializedWriter(name, self.target.platform)
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
    # IMPORTED is the PS Vita's table; a build uses IMPORTED_FOR[its platform] (windows_build.py
    # fills the Windows one).
    IMPORTED: ClassVar[dict] = {}
    IMPORTED_FOR: ClassVar[dict] = {'vita': IMPORTED, 'windows': {}}

    def shared(self, file_name):
        """A sharedassetsN.assets / resources.assets / globalgamemanagers.assets file. Returns (bytes,
        classes that could not be built yet)."""
        objects = self.layout.objects(file_name)
        start = first_path_id(file_name)
        out = SerializedWriter(file_name, self.target.platform)
        missing = collections.Counter()
        if start == 2:  # sharedassetsN.assets and globalgamemanagers.assets open with their PreloadData
            out.add(1, 150, self.preloader.preload(file_name))
        if file_name == 'globalgamemanagers.assets':
            for s in self.entries:
                out.add(s.path_id, 115, dict(s.fields))
        db = self.layout.db
        mbs = [db.by_guid[g].docs[fid] for g, fid, cid in objects if cid == 114]
        self.scripts.load_models({self.scripts.resolve(d.fields['m_Script'], d.fields) for d in mbs} - {None})
        for i, (g, fid, cid) in enumerate(progress.track(objects, file_name)):
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
                        classes.ASSET_FIXUPS[cid](data, doc, a, self.project)
            elif cid in self.imported:
                made = self.imported[cid](self, file_name, g, fid, a)
                if made:
                    data, tree = made
            if data is None:
                missing[cid] += 1
                continue
            out.add(start + i, cid, data, tree, script)
        return out.write(self.hashes), missing

    def globalgamemanagers(self):
        out = SerializedWriter('globalgamemanagers', self.target.platform)
        if self.target.name == 'windows':
            import windows_managers

            objects = windows_managers.managers(self.layout, self.placement, self.entries, self.root)
        else:
            objects = managers.managers(self.layout, self.placement, self.entries)
        for path_id, class_id, data in objects:
            out.add(path_id, class_id, data)
        return out.write(self.hashes)

    def write(self, name, data):
        path = self.media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


Build.IMPORTED[83] = audio.imported
Build.IMPORTED[48] = shader_object.imported
Build.IMPORTED[21] = builtin_copies.material
imported.register(Build)  # textures, sprites, fonts, font materials, text assets


def compare(name, data, reference=paths.REFERENCE_MEDIA):
    ref = (reference / name).read_bytes()
    if ref == data:
        return 'identical'
    first = next((i for i, (a, b) in enumerate(zip(ref, data, strict=False)) if a != b), min(len(ref), len(data)))
    return f'differs at byte {first} (sizes {len(data)} vs reference {len(ref)})'


def write_streams(build, names, compare_files=False):
    """Writes the named streams of build.streams and drops them, so they don't stay in memory
    for the rest of the build."""
    for name in names:
        data = build.streams.pop(name, None)
        if data is None:
            continue
        build.write(name, data)
        progress.written(name, len(data), note=compare(name, data, build.target.reference) if compare_files else '')


def write_all(build, only=frozenset(), compare_files=False):
    """Writes every file of the player's data (or those named in `only`), with a line for each."""
    paths.builtin_shaders()
    ref = build.target.reference
    levels = [i for i in range(len(build.layout.scenes)) if not only or f'level{i}' in only]
    shared = [name for name in build.files if not only or name in only]
    extra, ggm = 'Resources/unity_builtin_extra', 'globalgamemanagers'
    rest = [name for name in (extra, ggm) if not only or name in only]

    def done(name, data, start, missing=None):
        build.write(name, data)
        note = compare(name, data, ref) if compare_files else ''
        progress.written(name, len(data), time.time() - start, note, missing)
        files.advance()

    with progress.display(), progress.step('Data files', total=len(levels) + len(shared) + len(rest)) as files:
        for i in levels:
            files.note(f'level{i}')
            start = time.time()
            done(f'level{i}', build.level(i), start)
        for name in shared:
            files.note(name)
            start = time.time()
            data, missing = build.shared(name)
            done(name, data, start, missing)
            # Only building a file appends to its streams, so they are complete.
            write_streams(build, [f'{name}.resS', audio.resource_name(name)], compare_files)
        if extra in rest:
            files.note(extra)
            start = time.time()
            if build.target.name == 'windows':
                import windows_builtin_extra

                data = windows_builtin_extra.build_extra(build)
            else:
                data = builtin_extra.build_extra(build)
            done(extra, data, start)
        if ggm in rest:
            files.note(ggm)
            start = time.time()
            done(ggm, build.globalgamemanagers(), start)
        write_streams(build, list(build.streams), compare_files)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--platform', choices=sorted(TARGETS), default='vita')
    parser.add_argument(
        '--out', type=Path, help="default: the pipeline's builds/drifter-<version>-vita[-diag], -builder for windows"
    )
    parser.add_argument('--only', default='')
    parser.add_argument('--compare', action='store_true')
    args = parser.parse_args()
    if args.platform == 'windows':
        import windows_build

        windows_build.register(Build)
    out = args.out or (
        paths.OUT if args.platform == 'vita' else paths.ROOT / 'builds' / f'drifter-{paths.TARGET}-builder'
    )
    write_all(Build(out, args.platform), set(filter(None, args.only.split(','))), args.compare)
    return 0


if __name__ == '__main__':
    sys.exit(main())
