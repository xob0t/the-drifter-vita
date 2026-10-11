"""Script classes: which class a YAML m_Script names, and the type tree its MonoBehaviours use.

A script reference is {fileID, guid}: the guid names the assembly (the .meta beside a project DLL,
or Unity's ivy.xml for the DLLs Unity ships), the fileID the class, as the first four bytes of
MD4("s\\0\\0\\0" + namespace + name).
"""

import re
import struct
from functools import cached_property

import convert
import dnfile
import typemodel
import unityyaml
from Crypto.Hash import MD4
from paths import EDITOR_DATA, PROJECT, WORK
from UnityPy.helpers.TypeTreeNode import TypeTreeNode

ALIGN = 0x4000
SUB4 = {'bool', 'UInt8', 'SInt8', 'short', 'SInt16', 'unsigned short', 'UInt16', 'char'}
EXTENSIONS = EDITOR_DATA / 'UnityExtensions' / 'Unity'
UNITY_MANAGED = EDITOR_DATA / 'Managed' / 'UnityEngine'


def file_id(namespace, name):
    return struct.unpack('<i', MD4.new(('s\0\0\0' + namespace + name).encode()).digest()[:4])[0]


class Scripts:
    def __init__(self, project=PROJECT):
        self.project = project
        self.models = {}
        self._classes = {}

    @cached_property
    def assemblies(self):
        """guid -> DLL path."""
        out = {}
        for meta in (self.project / 'Assets').rglob('*.dll.meta'):
            out[unityyaml.meta(meta)['guid']] = meta.with_suffix('')
        for ivy in EXTENSIONS.glob('*/ivy.xml'):
            for name, guid in re.findall(r'<artifact name="([^"]+)" type="dll"[^>]*e:guid="(\w+)"', ivy.read_text()):
                out[guid] = ivy.parent / f'{name}.dll'
        return out

    def classes(self, dll):
        """fileID -> [(namespace, name), ...] for the top-level types of a DLL. The hash covers
        namespace + name joined, so PowerTools.Quest.GuiInventoryPanel and
        PowerTools.QuestGui.InventoryPanel share a fileID."""
        if dll not in self._classes:
            pe = dnfile.dnPE(str(dll))
            table = {}
            for t in pe.net.mdtables.TypeDef or []:
                ns, name = str(t.TypeNamespace or ''), str(t.TypeName)
                table.setdefault(file_id(ns, name), []).append((ns, name))
            self._classes[dll] = table
        return self._classes[dll]

    def resolve(self, ref, fields=None):
        """(assembly file name, namespace, class name) of a YAML m_Script reference, or None. When
        the fileID is shared, `fields` (the object's YAML) picks the class whose fields it has."""
        dll = self.assemblies.get(ref.get('guid'))
        if dll is None:
            return None
        found = self.classes(dll).get(int(ref['fileID']))
        if not found:
            return None
        if len(found) > 1 and fields is not None:
            self.load_models([(dll.name, *c) for c in found])

            def fit(c):
                model = self.model((dll.name, *c)) or {'fields': []}
                names = {f['n'] for f in model['fields'] if 'node' in f}
                return len(names & set(fields)) - len(names - set(fields))

            return (dll.name, *max(found, key=fit))
        return (dll.name, *found[0])

    @cached_property
    def managed(self):
        """A folder with every assembly the models can need: the project's DLLs and Unity's."""
        folder = WORK / 'managed'
        folder.mkdir(parents=True, exist_ok=True)
        sources = [*(self.project / 'Assets').rglob('*.dll'), *UNITY_MANAGED.glob('*.dll')]
        sources += EXTENSIONS.glob('*/*.dll')
        for src in sources:
            dst = folder / src.name
            if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
                dst.write_bytes(src.read_bytes())
        return folder

    def load_models(self, classes):
        """classes: (assembly, namespace, name) triples; models for them are generated at once."""
        want = {(a, f'{ns}.{n}' if ns else n) for a, ns, n in classes} - set(self.models)
        if want:
            self.models.update(typemodel.models(self.managed, want))

    def model(self, script):
        assembly, ns, name = script
        entry = self.models.get((assembly, f'{ns}.{name}' if ns else name))
        if entry is None:
            self.load_models([script])
            entry = self.models.get((assembly, f'{ns}.{name}' if ns else name))
        return entry.get('model') if entry else None

    def release_tree(self, model):
        """The release type tree of a MonoBehaviour with this script model."""
        b = ReleaseBuilder()
        base = convert.release_tree(114)
        b.copy(base, 0)
        b.fields(model['fields'], 1)
        return TypeTreeNode.from_list(b.out)


class ReleaseBuilder:
    """Script fields as the player reads them. A 1- or 2-byte field is padded to 4 bytes after it,
    which Unity marks with the align flag."""

    def __init__(self):
        self.out = []

    def add(self, level, typ, name, size, meta=0, version=1):
        node = {'m_Level': level, 'm_Type': typ, 'm_Name': name, 'm_ByteSize': size}
        node |= {'m_Version': version, 'm_MetaFlag': meta}
        self.out.append(node)

    def copy(self, node, level):
        self.add(level, node.m_Type, node.m_Name, node.m_ByteSize, node.m_MetaFlag or 0, node.m_Version)
        for c in node.m_Children:
            self.copy(c, level + 1)

    def value(self, m, level, name, element=False):
        k = m['k']
        if k == 'prim':
            # array elements pack tightly; the array as a whole is aligned after its last element
            pad = m['t'] in SUB4 and m['size'] < 4 and not element
            # A C# char is two bytes; Unity's type name 'char' is the one-byte string element.
            kind = 'UInt16' if m['t'] == 'char' else m['t']
            self.add(level, kind, name, m['size'], ALIGN if pad else 0)
        elif k == 'string':
            self.add(level, 'string', name, -1, 0x8000)
            self.add(level + 1, 'Array', 'Array', -1, ALIGN | 0x8000)
            self.add(level + 2, 'int', 'size', 4)
            self.add(level + 2, 'char', 'data', 1)
        elif k == 'pptr':
            self.add(level, f'PPtr<{m["t"]}>', name, 12)
            self.add(level + 1, 'int', 'm_FileID', 4)
            self.add(level + 1, 'SInt64', 'm_PathID', 8)
        elif k == 'bitfield':
            self.add(level, 'BitField', name, 4, 0, 2)
            self.add(level + 1, 'unsigned int', 'm_Bits', 4)
        elif k == 'curve':
            self.add(level, 'AnimationCurve', name, -1, 0, 2)
            self.add(level + 1, 'vector', 'm_Curve', -1, 0x8000)
            self.add(level + 2, 'Array', 'Array', -1, ALIGN | 0x8000)
            self.add(level + 3, 'int', 'size', 4)
            self.add(level + 3, 'Keyframe', 'data', 28, 0, 3)
            for t, n in (('float', 'time'), ('float', 'value'), ('float', 'inSlope'), ('float', 'outSlope'),
                         ('int', 'weightedMode'), ('float', 'inWeight'), ('float', 'outWeight')):  # fmt: skip
                self.add(level + 4, t, n, 4)
            for n in ('m_PreInfinity', 'm_PostInfinity', 'm_RotationOrder'):
                self.add(level + 1, 'int', n, 4)
        elif k == 'array':
            self.add(level, 'vector', name, -1, 0x8000)
            self.add(level + 1, 'Array', 'Array', -1, ALIGN | 0x8000)
            self.add(level + 2, 'int', 'size', 4)
            self.value(m['e'], level + 2, 'data', element=True)
        elif k == 'struct':
            self.add(level, m['t'], name, -1)
            self.fields(m['fields'], level + 1)
        else:
            raise ValueError(f'unsupported field {name}: {m}')

    def fields(self, fields, level):
        for f in fields:
            if 'node' in f:
                self.value(f['node'], level, f['n'])
