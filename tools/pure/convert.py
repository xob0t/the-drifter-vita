"""Turn an object's editor YAML into the release form the player reads.

The release type tree (TPK) says which fields the player reads and their types; the YAML has the
editor's fields under the same names. convert() walks the release tree and pulls each field from
the YAML by name, so most classes need no code of their own. Class converters in classes.py
fix up the rest.

Values come out in UnityPy's type tree form: dicts for structs, lists for vectors, lists of
(key, value) for maps, {'m_FileID', 'm_PathID'} for PPtrs, bytes for TypelessData.
"""

import math
import struct
from functools import cache

from paths import TARGET
from UnityPy.helpers.Tpk import get_typetree_node
from UnityPy.helpers.UnityVersion import UnityVersion

VERSION = UnityVersion.from_str(TARGET)

INTS = {
    'SInt8', 'UInt8', 'SInt16', 'UInt16', 'short', 'unsigned short', 'int', 'unsigned int',
    'SInt32', 'UInt32', 'SInt64', 'UInt64', 'long long', 'unsigned long long', 'char', 'FileSize',
}  # fmt: skip
FLOATS = {'float', 'double'}


@cache
def release_tree(class_id):
    return get_typetree_node(class_id, VERSION)


def number(text, kind):
    if isinstance(text, (int, float)):
        return text
    text = text.strip()
    if kind in FLOATS:
        low = text.lower()
        if low in ('infinity', 'inf', '+infinity'):
            return math.inf
        if low in ('-infinity', '-inf'):
            return -math.inf
        if low == 'nan':
            return math.nan
        if low.startswith('0x'):
            # Unity writes floats it cannot round-trip in decimal as their bits
            return struct.unpack('<f', struct.pack('<I', int(low, 16)))[0]
        if kind == 'float' and abs(float(text)) > 3.4028234663852886e38:
            return math.copysign(math.inf, float(text))
        value = float(text)
        return struct.unpack('<f', struct.pack('<f', value))[0] if kind == 'float' else value
    if kind == 'bool':
        return text not in ('0', 'false', 'False', '')
    try:
        return int(text)
    except ValueError:
        return int(float(text))


def default(node):
    t = node.m_Type
    if t in INTS:
        return 0
    if t in FLOATS:
        return 0.0
    if t == 'bool':
        return False
    if t == 'string':
        return ''
    if t in ('TypelessData',):
        return b''
    if t.startswith('PPtr<'):
        return {'m_FileID': 0, 'm_PathID': 0}
    if t in ('vector', 'map', 'staticvector', 'set'):
        return []
    return {c.m_Name: default(c) for c in node.m_Children}


class Converter:
    """ctx.pptr(yaml_ref, node) turns a YAML reference into {'m_FileID', 'm_PathID'}."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.missing = []

    def value(self, node, y, path):
        t = node.m_Type
        if y is None:
            self.missing.append(path)
            return default(node)
        if t in INTS or t in FLOATS or t == 'bool':
            return number(y, t)
        if t == 'string':
            return y if isinstance(y, str) else str(y)
        if t.startswith('PPtr<'):
            return self.ctx.pptr(y, node)
        if t == 'TypelessData':
            return bytes.fromhex(y.strip()) if isinstance(y, str) else bytes(y)
        if t in ('vector', 'staticvector', 'set', 'map'):
            return self.array(node, y, path)
        if t == 'pair':
            first, second = node.m_Children
            if isinstance(y, dict) and 'first' not in y and len(y) == 1:
                ((k, v),) = y.items()  # written as {first: second}
                y = {'first': k, 'second': v}
            return (
                self.value(first, y.get('first'), path + '.first'),
                self.value(second, y.get('second'), path + '.second'),
            )
        return self.struct(node, y, path)

    def struct(self, node, y, path):
        if node.m_Type == 'GUID' and isinstance(y, str):
            return guid_words(y)
        if not isinstance(y, dict):
            raise TypeError(f'{path}: expected a mapping for {node.m_Type}, got {y!r:.60}')
        out = {}
        for c in node.m_Children:
            # YAML writes typeless data as a size field and a sibling _typelessdata hex string
            v = y.get('_typelessdata', '') if c.m_Type == 'TypelessData' else y.get(c.m_Name)
            out[c.m_Name] = self.value(c, v, f'{path}.{c.m_Name}')
        return out

    def array(self, node, y, path):
        array = node.m_Children[0]
        element = array.m_Children[1]
        if isinstance(y, str):
            # vectors of numbers can be written as one hex string (little-endian elements)
            if element.m_Type in INTS or element.m_Type in FLOATS or element.m_Type == 'bool':
                return list(hex_elements(y, element))
            if y == '':
                return []
            raise TypeError(f'{path}: unexpected string for {node.m_Type}')
        if isinstance(y, dict) and node.m_Type == 'map':
            y = [{k: v} for k, v in y.items()]
        out = []
        for i, item in enumerate(y or []):
            if node.m_Type == 'map' and isinstance(item, dict) and 'first' not in item and len(item) == 1:
                # Unity writes maps with simple keys as "- key: value"
                ((k, v),) = item.items()
                first, second = element.m_Children
                out.append((self.value(first, k, f'{path}[{i}].first'), self.value(second, v, f'{path}[{i}].second')))
            else:
                out.append(self.value(element, item, f'{path}[{i}]'))
        return out


FORMATS = {
    'SInt8': 'b', 'UInt8': 'B', 'char': 'B', 'bool': '?', 'SInt16': 'h', 'short': 'h', 'UInt16': 'H',
    'unsigned short': 'H', 'int': 'i', 'SInt32': 'i', 'unsigned int': 'I', 'UInt32': 'I', 'SInt64': 'q',
    'long long': 'q', 'UInt64': 'Q', 'unsigned long long': 'Q', 'float': 'f', 'double': 'd',
}  # fmt: skip


def guid_words(text):
    """A GUID as Unity writes it (32 hex digits, each word's nibbles low first) -> its four words."""
    words = {}
    for i in range(4):
        part = text[8 * i : 8 * i + 8]
        words[f'data[{i}]'] = sum(int(c, 16) << (4 * j) for j, c in enumerate(part))
    return words


def hex_elements(text, element):
    data = bytes.fromhex(text)
    fmt = FORMATS[element.m_Type]
    size = struct.calcsize(fmt)
    return (v for (v,) in struct.iter_unpack('<' + fmt, data[: len(data) // size * size]))


def convert(class_id, fields, ctx, tree=None):
    """(release dict, list of field paths the YAML lacked)."""
    converter = Converter(ctx)
    node = tree or release_tree(class_id)
    return converter.struct(node, fields, node.m_Type), converter.missing
