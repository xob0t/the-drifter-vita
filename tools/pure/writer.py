"""Write one of the player's serialized files from converted objects.

    out = SerializedWriter('sharedassets1.assets')
    out.add(path_id, class_id, data, tree, script=None)  # data: dict in UnityPy type tree form
    bytes_ = out.write(types)                            # types: TypeHashes

References in `data` are {'m_FileID', 'm_PathID', 'file'}: 'file' names the file they point into
('sharedassets0.assets', 'resources/unity_builtin_extra', ...); the writer turns it into an index
into this file's externals, which it lists in order of first use (globalgamemanagers.assets first
when anything here uses it, as Unity does). Types are listed in order of first use too, one per
class and one per script for MonoBehaviours.
"""

from functools import cache

import convert
import typehash
from container import External, HeaderInfo, ObjectEntry, ScriptType, TypeEntry, write_serialized
from UnityPy.helpers.TypeTreeHelper import write_typetree
from UnityPy.streams import EndianBinaryWriter

BUILTIN_GUIDS = {
    'library/unity default resources': bytes.fromhex('00000000000000000e00000000000000'),
    'resources/unity_builtin_extra': bytes.fromhex('00000000000000000f00000000000000'),
}
MONOBEHAVIOUR = 114


class TypeHashes:
    """Type table hashes. `script_info(script)` -> (script_id, old type hash, MonoScript path ID in
    globalgamemanagers.assets) for a (assembly, namespace, class) script."""

    def __init__(self, script_info, overrides=None):
        self.script_info = script_info
        self.overrides = overrides or {}

    @cache  # noqa: B019 - one instance per build
    def builtin(self, class_id):
        if class_id in self.overrides:
            return self.overrides[class_id]
        return typehash.builtin_hash(class_id, convert.VERSION)


class SerializedWriter:
    def __init__(self, name, platform=30):
        self.name, self.platform = name, platform
        self.objects = []  # (path_id, class_id, script, data, tree)
        self.externals = []

    def add(self, path_id, class_id, data, tree=None, script=None):
        self.objects.append((path_id, class_id, script, data, tree))

    def external(self, file_name):
        if file_name not in self.externals:
            self.externals.append(file_name)
        return self.externals.index(file_name) + 1

    def resolve(self, value):
        """Turns 'file' in references into m_FileID indices, in place."""
        if isinstance(value, dict):
            if 'm_PathID' in value and 'm_FileID' in value:
                target = value.pop('file', None)
                if target is not None and target != self.name and value['m_PathID'] != 0:
                    value['m_FileID'] = self.external(target)
                return
            for v in value.values():
                self.resolve(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                self.resolve(v)

    def write(self, hashes):
        self.objects.sort(key=lambda o: o[0])
        # globalgamemanagers.assets holds the MonoScripts every script type points to
        if any(o[1] == MONOBEHAVIOUR for o in self.objects) and self.name != 'globalgamemanagers.assets':
            self.external('globalgamemanagers.assets')
        types, type_index, script_types = [], {}, []
        entries = []
        for path_id, class_id, script, data, tree in self.objects:
            if class_id == MONOBEHAVIOUR and script is None:
                raise ValueError(f"{self.name}: MonoBehaviour {path_id}'s m_Script does not resolve to a script")
            self.resolve(data)
            key = (class_id, script if class_id == MONOBEHAVIOUR else None)
            if key not in type_index:
                type_index[key] = len(types)
                if class_id == MONOBEHAVIOUR:
                    script_id, old_hash, monoscript = hashes.script_info(script)
                    index = (
                        0 if self.name == 'globalgamemanagers.assets' else self.external('globalgamemanagers.assets')
                    )
                    script_types.append(ScriptType(index, monoscript))
                    types.append(TypeEntry(class_id, False, script_types[-1], script_id, old_hash))
                else:
                    types.append(TypeEntry(class_id, False, -1, None, hashes.builtin(class_id)))
            node = tree or convert.release_tree(class_id)
            w = EndianBinaryWriter(endian='<')
            write_typetree(data, node, w)
            entries.append(ObjectEntry(path_id, type_index[key], w.bytes))
        # Unity lists script types by MonoScript; the type entries point into that order.
        script_types.sort(key=lambda s: (s.file_index, s.local_id))
        for t in types:
            if isinstance(t.script_type_index, ScriptType):
                t.script_type_index = script_types.index(t.script_type_index)
        externals = [External(path=f, guid=BUILTIN_GUIDS.get(f, b'\0' * 16)) for f in self.externals]
        header = HeaderInfo(target_platform=self.platform)
        return write_serialized(header, types, entries, externals, script_types)
