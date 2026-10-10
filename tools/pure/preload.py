"""PreloadData: path ID 1 of each sharedassetsN.assets and of globalgamemanagers.assets.

When the player loads level N it first loads sharedassetsN's PreloadData, whose m_Assets lists
every object outside levelN and sharedassetsN that their objects reach, following references
transitively through other files: earlier sharedassets, resources.assets, globalgamemanagers.assets
(MonoScripts, the default sprite material), Unity's built-in files. The list is ordered by file in
the build's file order (unity_builtin_extra, unity default resources, globalgamemanagers.assets,
resources.assets, sharedassets0, 1, ...), then by path ID. Run on the reference build's own
references this rule reproduces all 62 reference lists exactly.

globalgamemanagers.assets' PreloadData lists the MonoScripts MonoManager names in other files:
the ones in unity default resources. levelN and resources.assets have none.

    builder = Preload(layout, placement)       # placement with managers.register() applied
    builder.preload('sharedassets3.assets')    # {'m_Name', 'm_Assets', 'm_Dependencies', ...}
"""

import scene
from managers import GGA, default_resources, file_rank
from paths import PROJECT
from references import DEFAULT_RESOURCES_FILE, Graph


class Preload:
    def __init__(self, layout, placement, project=PROJECT):
        self.layout, self.placement, self.project = layout, placement, project
        self.graph = Graph(layout, placement)
        self._refs = {}

    def refs(self, place):
        if place not in self._refs:
            self._refs[place] = self.graph.place_refs(place)
        return self._refs[place]

    def own(self, file_name):
        return {p for p in self.placement.where.values() if p[0] == file_name}

    def roots(self, n, own):
        """What levelN's objects and sharedassetsN's objects reference."""
        s = self.layout.scenes[n]
        docs = [d for d in scene.Scene(self.project / s['path']).docs if d.class_id not in scene.EDITOR_ONLY]
        out = self.graph.scene_refs(docs, s.get('guid'))
        for p in sorted(own):
            out += self.refs(p)
        return out

    def assets(self, file_name):
        """The (file, path ID) list of a file's PreloadData."""
        if file_name == GGA:
            return [(DEFAULT_RESOURCES_FILE, pid) for pid, cid, _ in default_resources() if cid == 115]
        n = int(file_name[len('sharedassets') : -len('.assets')])
        own = self.own(file_name)
        seen, stack = set(), list(self.roots(n, own))
        while stack:
            p = stack.pop()
            if p in seen or p in own:
                continue
            seen.add(p)
            stack.extend(self.refs(p))
        return sorted(seen, key=lambda p: (file_rank(p[0]), p[1]))

    def preload(self, file_name):
        assets = [{'m_FileID': 0, 'm_PathID': pid, 'file': f} for f, pid in self.assets(file_name)]
        return {'m_Name': '', 'm_Assets': assets, 'm_Dependencies': [], 'm_ExplicitDataLayout': False}

    def describe(self, place):
        """A place with the asset it comes from, for reports."""
        key = self.graph.source.get(place)
        if key is None:
            return place
        a = self.layout.db.by_guid.get(key[0])
        return (*place, a.path.rsplit('/', 1)[-1] if a else key[0][:8], key[1])


def preload(layout, placement, file_name, project=PROJECT):
    """The PreloadData dict of a file (path ID 1); references carry their file as 'file'."""
    return Preload(layout, placement, project).preload(file_name)
