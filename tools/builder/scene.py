"""Scene YAML -> the objects of a levelN file.

Unity writes a level's objects in class ID order, MonoBehaviours last, each class in the YAML's
fileID order, numbered 1..N. NavMeshSettings stays in the editor. (The editor build also adds
DrifterCompat.CrtImageEffect to the GUI camera: CRTEffect is [ExecuteInEditMode] and its OnEnable
adds it while the scene is loaded for the build. The game adds it at runtime anyway, so it is not
written here.)
"""

from dataclasses import dataclass

import unityyaml

EDITOR_ONLY = {196}  # NavMeshSettings
MONOBEHAVIOUR = 114


@dataclass
class SceneObject:
    path_id: int
    doc: unityyaml.Doc


def hierarchy_paths(docs):
    """fileID -> 'Root/Child#1/...' for GameObjects, through their Transforms; #n numbers
    siblings that share a name, in their parent's child order."""
    by_id = {d.file_id: d for d in docs}
    transform_of = {}
    for d in docs:
        if d.class_id in (4, 224):
            transform_of[int(d.fields['m_GameObject']['fileID'])] = d
    names = {}

    def label(go_id, siblings):
        name = by_id[go_id].fields.get('m_Name', '')
        same = [g for g in siblings if by_id[g].fields.get('m_Name', '') == name]
        return name if len(same) < 2 else f'{name}#{same.index(go_id)}'

    def path(go_id, depth=0):
        if go_id in names:
            return names[go_id]
        t = transform_of.get(go_id)
        parent = int(t.fields.get('m_Father', {}).get('fileID', 0)) if t else 0
        if parent and depth < 256:
            father = by_id[parent]
            siblings = [
                int(by_id[int(c['fileID'])].fields['m_GameObject']['fileID'])
                for c in father.fields.get('m_Children', [])
            ]
            name = path(int(father.fields['m_GameObject']['fileID']), depth + 1) + '/' + label(go_id, siblings)
        else:
            name = by_id[go_id].fields.get('m_Name', '')
        names[go_id] = name
        return name

    for d in docs:
        if d.class_id == 1:
            path(d.file_id)
    return names


class Scene:
    """A scene's documents with lookups the class fixups need."""

    def __init__(self, path):
        self.docs = [d for d in unityyaml.load(path) if not d.stripped]
        self.by_id = {d.file_id: d for d in self.docs}

    def objects(self):
        docs = [d for d in self.docs if d.class_id not in EDITOR_ONLY]
        order = sorted(docs, key=lambda d: (d.class_id == MONOBEHAVIOUR, d.class_id, d.file_id))
        return [SceneObject(i + 1, d) for i, d in enumerate(order)]

    def components(self, go_ref):
        go = self.by_id.get(int(go_ref['fileID']))
        for c in go.fields.get('m_Component', []) if go else []:
            d = self.by_id.get(int(next(iter(c.values()))['fileID']))
            if d is not None:
                yield d

    def component(self, go_ref, class_id):
        return next((d for d in self.components(go_ref) if d.class_id == class_id), None)

    def script_component(self, go_ref, field):
        """The first MonoBehaviour on the GameObject that has the given field."""
        return next((d for d in self.components(go_ref) if d.class_id == MONOBEHAVIOUR and field in d.fields), None)
