"""Which objects each object of the build references, as (file, path ID), in field order.

The build's dependency lists (PreloadData, ResourceManager.m_DependentAssets) are made of these.
YAML objects reference what their fields name, minus the editor's prefab links; the animator's
states and transitions stay in the editor, so an AnimatorController references what they do (its
clips and state machine behaviours). Imported files reference their sub-objects as their importer
makes them (a sprite its texture, a font its material, texture and fallback fonts, the font
material Unity's font shader and the font texture). Project shaders reference nothing in this
build. Built-in objects reference what their copies in Unity's files do.
"""

import contextlib
import functools
from pathlib import Path

import unityyaml
from layout import BUILTIN_EXTRA, builtin_extra
from paths import EDITOR_DATA
from placement import DEFAULT_RESOURCES

DEFAULT_RESOURCES_FILE = 'library/unity default resources'
BUILTIN_EXTRA_FILE = 'resources/unity_builtin_extra'
# Links the editor keeps between prefab instances and prefabs; the player has none of them.
EDITOR_FIELDS = {
    'm_PrefabParentObject', 'm_PrefabInternal', 'm_CorrespondingSourceObject', 'm_PrefabInstance',
    'm_PrefabAsset', 'm_ParentPrefab', 'm_SourcePrefab', 'm_RootGameObject', 'm_LightingDataAsset',
}  # fmt: skip
# Editor-only objects the build folds into their owner (the AnimatorController's state machines).
TRANSPARENT = {1101, 1102, 1107, 1109, 1111, 206}
FONT_SHADER = 10101  # 'GUI/Text Shader' in unity default resources


def yaml_refs(fields, out):
    """(guid or None, fileID) of every reference in a YAML document's fields, in order."""
    if isinstance(fields, dict):
        if 'fileID' in fields and len(fields) <= 3 and set(fields) <= {'fileID', 'guid', 'type'}:
            fid = int(fields['fileID'])
            if fid:
                out.append((fields.get('guid'), fid))
            return
        for k, v in fields.items():
            if k not in EDITOR_FIELDS:
                yaml_refs(v, out)
    elif isinstance(fields, list):
        for v in fields:
            yaml_refs(v, out)


@functools.cache
def unity_file_refs(path):
    """{path ID: [path ID]} of the local references in one of Unity's own serialized files."""
    import UnityPy

    env = UnityPy.load(str(path))
    out = {}
    for pid, o in next(iter(env.files.values())).objects.items():
        refs = []
        with contextlib.suppress(Exception):  # objects without a readable type tree reference nothing
            walk_pptrs(o.read_typetree(), refs)
        out[pid] = [p for f, p in refs if f == 0 and p]
    return out


def walk_pptrs(v, out):
    if isinstance(v, dict):
        if 'm_FileID' in v and 'm_PathID' in v and len(v) == 2:
            out.append((v['m_FileID'], v['m_PathID']))
            return
        for x in v.values():
            walk_pptrs(x, out)
    elif isinstance(v, (list, tuple)):
        for x in v:
            walk_pptrs(x, out)


class Graph:
    def __init__(self, layout, placement):
        self.layout, self.placement = layout, placement
        self.source = {place: key for key, place in placement.where.items()}

    def place(self, guid, fid):
        f, pid = self.placement.find(guid, fid)
        return (f, pid) if f and pid else None  # None: the build does not write it

    def asset_refs(self, guid, fid):
        """(guid, fileID) references of a project asset's object."""
        a = self.layout.db.by_guid[guid]
        if not a.is_yaml:
            cid = a.classes.get(fid)
            if cid == 213:
                return [(guid, 2800000)]
            if cid == 128:
                fallbacks = (
                    unityyaml.meta(Path(a.meta_path)).get('TrueTypeFontImporter', {}).get('fallbackFontReferences')
                    or []
                )
                return [(guid, 2100000), (guid, 2800000), *((r['guid'], int(r['fileID'])) for r in fallbacks)]
            if cid == 21 and a.ext in ('.ttf', '.otf'):
                return [(DEFAULT_RESOURCES, FONT_SHADER), (guid, 2800000)]
            return []
        out, seen, stack = [], {fid}, [fid]
        while stack:
            doc = a.docs.get(stack.pop(0))
            if doc is None:
                continue
            found = []
            yaml_refs(doc.fields, found)
            for g, f in found:
                g = g or guid
                inner = a.docs.get(f) if g == guid else None
                if inner is not None and inner.class_id in TRANSPARENT:
                    if f not in seen:
                        seen.add(f)
                        stack.append(f)
                    continue
                out.append((g, f))
        return out

    def refs_of(self, guid, fid):
        """References of an object, as places, distinct, in field order."""
        if guid == BUILTIN_EXTRA:
            local = unity_file_refs(EDITOR_DATA / 'Resources' / 'unity_builtin_extra').get(fid, [])
            keys = [(BUILTIN_EXTRA, f) for f in local if f in builtin_extra()]
        elif guid == DEFAULT_RESOURCES:
            from managers import PSP2_PLAYER

            local = unity_file_refs(PSP2_PLAYER / 'Data' / 'Resources' / 'unity default resources').get(fid, [])
            keys = [(DEFAULT_RESOURCES, f) for f in local]
        elif guid in self.layout.db.by_guid:
            keys = self.asset_refs(guid, fid)
        else:
            keys = []  # MonoScripts
        out = []
        for g, f in keys:
            p = self.place(g, f)
            if p is not None and p not in out:
                out.append(p)
        return out

    def place_refs(self, place):
        """References of the object at a place, for objects of the build's asset files and
        Unity's files."""
        key = self.source.get(place)
        if key is None:
            f, pid = place
            if f == BUILTIN_EXTRA_FILE:
                key = (BUILTIN_EXTRA, pid)
            elif f == DEFAULT_RESOURCES_FILE:
                key = (DEFAULT_RESOURCES, pid)
            else:
                return []
        return self.refs_of(*key)

    def direct(self, guid, fid):
        return self.refs_of(guid, fid)

    def scene_refs(self, docs, guid=None):
        """References out of a scene (docs: its YAML documents) to other files, in order."""
        local = {d.file_id for d in docs}
        out, seen = [], set()
        for d in docs:
            found = []
            yaml_refs(d.fields, found)
            for g, f in found:
                if (g is None or g == guid or g == '0' * 32) and f in local:
                    continue
                p = self.place(g, f) if g else None
                if p is not None and p not in seen:
                    seen.add(p)
                    out.append(p)
        return out
