"""The 16-byte hashes in a serialized file's type table.

OldTypeHash is MD4 over the type tree, node by node in pre-order: type name, field name (no
terminator), then byte size, is-array, version and the align flag as little-endian int32. Built-in
classes hash their release tree; scripts hash the editor tree (the editor MonoBehaviour base,
then the script's fields), which is also MonoScript.m_PropertiesHash. A script type's ScriptID
is MD4 of class name + namespace + assembly name.
"""

import struct

from Crypto.Hash import MD4
from UnityPy.helpers import Tpk

ALIGN = 0x4000
PRIM = {'bool': 'UInt8', 'UInt8': 'UInt8', 'SInt8': 'SInt8', 'short': 'SInt16', 'unsigned short': 'UInt16',
        'int': 'int', 'unsigned int': 'unsigned int', 'long long': 'SInt64', 'unsigned long long': 'UInt64',
        'float': 'float', 'double': 'double', 'char': 'UInt16'}
# Unity's own value types: (type name, byte size, version, [(field type, field name, size)])
F = 'float'
ENGINE = {
    'Vector2': ('Vector2f', 8, 1, [(F, 'x', 4), (F, 'y', 4)]),
    'Vector3': ('Vector3f', 12, 1, [(F, 'x', 4), (F, 'y', 4), (F, 'z', 4)]),
    'Vector4': ('Vector4f', 16, 1, [(F, 'x', 4), (F, 'y', 4), (F, 'z', 4), (F, 'w', 4)]),
    'Quaternion': ('Quaternionf', 16, 1, [(F, 'x', 4), (F, 'y', 4), (F, 'z', 4), (F, 'w', 4)]),
    'Color': ('ColorRGBA', 16, 1, [(F, 'r', 4), (F, 'g', 4), (F, 'b', 4), (F, 'a', 4)]),
    'Rect': ('Rectf', 16, 2, [(F, 'x', 4), (F, 'y', 4), (F, 'width', 4), (F, 'height', 4)]),
    'Color32': ('ColorRGBA', 4, 2, [('unsigned int', 'rgba', 4)]),
}

class HashBuilder:
    def __init__(self, base):
        self.out = list(base)

    def add(self, level, typ, name, size, meta=0, version=1):
        self.out.append(dict(m_Level=level, m_Type=typ, m_Name=name, m_ByteSize=size, m_Version=version, m_MetaFlag=meta))

    def value(self, m, level, name, meta=0):
        k = m['k']
        if k == 'prim':
            self.add(level, PRIM[m['t']], name, m['size'], meta | (ALIGN if m['size'] < 4 else 0))
        elif k == 'bitfield':
            self.add(level, 'BitField', name, 4, meta, 2)
            self.add(level + 1, 'unsigned int', 'm_Bits', 4)
        elif k == 'string':
            self.add(level, 'string', name, -1, meta)
            self.add(level + 1, 'Array', 'Array', -1, ALIGN)
            self.add(level + 2, 'int', 'size', 4)
            self.add(level + 2, 'char', 'data', 1)
        elif k == 'curve':
            # AnimationCurve<float> as the editor serializes it (Keyframe v3 keeps tangentMode)
            self.add(level, 'AnimationCurve', name, -1, meta, 2)
            self.add(level + 1, 'vector', 'm_Curve', -1)
            self.add(level + 2, 'Array', 'Array', -1, ALIGN)
            self.add(level + 3, 'int', 'size', 4)
            self.add(level + 3, 'Keyframe', 'data', 32, 0, 3)
            for t, n in (('float', 'time'), ('float', 'value'), ('float', 'inSlope'), ('float', 'outSlope'),
                         ('int', 'tangentMode'), ('int', 'weightedMode'), ('float', 'inWeight'), ('float', 'outWeight')):
                self.add(level + 4, t, n, 4)
            for n in ('m_PreInfinity', 'm_PostInfinity', 'm_RotationOrder'):
                self.add(level + 1, 'int', n, 4)
        elif k == 'pptr':
            self.add(level, f'PPtr<${m["t"]}>', name, 12, meta)
            self.add(level + 1, 'int', 'm_FileID', 4)
            self.add(level + 1, 'SInt64', 'm_PathID', 8)
        elif k == 'array':
            e = m['e']
            # An array of a user class is labelled with the class name, other arrays are 'vector'
            user = e['k'] == 'struct' and not (e['t'] in ENGINE and e.get('full', '').startswith('UnityEngine.'))
            self.add(level, e['t'] if user else 'vector', name, -1, meta)
            # the element run is padded to 4 bytes when it can end unaligned
            unaligned = e['k'] in ('string', 'prim', 'bitfield') or (e['k'] == 'struct' and not user)
            self.add(level + 1, 'Array', 'Array', -1, ALIGN if unaligned else 0)
            self.add(level + 2, 'int', 'size', 4)
            self.value(e, level + 2, 'data')
        elif k == 'struct' and m['t'] in ENGINE and m.get('full', '').startswith('UnityEngine.'):
            t, size, ver, fields = ENGINE[m['t']]
            self.add(level, t, name, size, meta, ver)
            for ft, fn, fs in fields: self.add(level + 1, ft, fn, fs)
        elif k == 'struct':
            start = len(self.out)
            self.add(level, m['t'], name, -1, meta)
            self.fields(m['fields'], level + 1)
            # A class whose members all have a fixed size gets that size, like Unity's own structs
            kids = [n for n in self.out[start + 1:] if n['m_Level'] == level + 1]
            if all(n['m_ByteSize'] >= 0 for n in kids):
                self.out[start]['m_ByteSize'] = sum(n['m_ByteSize'] for n in kids)
        else:
            raise ValueError(m)

    def fields(self, fields, level):
        for f in fields:
            if 'node' in f: self.value(f['node'], level, f['n'])

def build(base, model):
    b = HashBuilder(base)
    b.fields(model['fields'], 1)
    return b.out


def tpk_nodes(root):
    """A TPK node tree as a pre-order list of node dicts."""
    tpk = Tpk.get_typetree()
    out, stack = [], [(root, 0)]
    while stack:
        nid, level = stack.pop(0)
        n = tpk.NodeBuffer[nid]
        name = tpk.StringBuffer
        out.append(dict(m_Level=level, m_Type=name[n.TypeName], m_Name=name[n.Name], m_ByteSize=n.ByteSize, m_Version=n.Version, m_MetaFlag=n.MetaFlag))
        stack = [(c, level + 1) for c in n.SubNodes] + stack
    return out


def class_info(class_id, version):
    return Tpk.get_typetree().ClassInformation[class_id].getVersionedClass(version)


def type_hash(nodes):
    md4 = MD4.new()
    for n in nodes:
        md4.update(n['m_Type'].encode())
        md4.update(n['m_Name'].encode())
        md4.update(struct.pack('<iiii', n['m_ByteSize'], n['m_Type'] == 'Array', n['m_Version'], n['m_MetaFlag'] & ALIGN))
    return md4.digest()


# The player names these classes without the editor's "Controller" suffix.
RELEASE_ROOT = {241: 'AudioMixer', 243: 'AudioMixerGroup', 245: 'AudioMixerSnapshot'}


def builtin_hash(class_id, version):
    nodes = tpk_nodes(class_info(class_id, version).ReleaseRootNode)
    if class_id in RELEASE_ROOT:
        nodes[0]['m_Type'] = RELEASE_ROOT[class_id]
    return type_hash(nodes)


def editor_base(version):
    """The editor MonoBehaviour tree every script's hashed tree starts with."""
    return tpk_nodes(class_info(114, version).EditorRootNode)


def script_hash(model, version):
    return type_hash(build(editor_base(version), model))


def script_id(class_name, namespace, assembly):
    return MD4.new((class_name + namespace + assembly).encode()).digest()
