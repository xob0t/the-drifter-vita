"""What the build reads from .NET assemblies besides field layouts: each type's base class (to find
the MonoBehaviour and ScriptableObject classes that get a MonoScript), and the attributes Unity acts
on ([MovedFrom], [DefaultExecutionOrder], [RuntimeInitializeOnLoadMethod]).

dnfile reads the metadata tables. Base classes resolve by full name, preferring the assembly the
TypeRef names, so references through a facade (UnityEngine.dll forwards to the modules) still
resolve.
"""

import struct
from dataclasses import dataclass, field
from functools import cached_property

import dnfile

SCRIPT_ROOTS = {'UnityEngine.MonoBehaviour', 'UnityEngine.ScriptableObject'}
ABSTRACT = 0x80
GENERICINST = 0x15


@dataclass
class Method:
    name: str
    flags: int
    attributes: list = field(default_factory=list)  # [(attribute type full name, value blob)]


@dataclass
class Type:
    assembly: str  # the assembly's name, e.g. 'Recovered.Drifter'
    index: int  # TypeDef row: metadata order
    namespace: str
    name: str
    flags: int
    base: tuple | None  # (assembly name or None for this one, full name)
    nested: bool
    generic: bool
    methods: list = field(default_factory=list)
    attributes: list = field(default_factory=list)

    @property
    def full_name(self):
        return f'{self.namespace}.{self.name}' if self.namespace else self.name

    @property
    def abstract(self):
        return bool(self.flags & ABSTRACT)

    def attribute(self, name):
        """The value blob of the first attribute of this type, or None."""
        return next((value for n, value in self.attributes if n == name), None)


def table_name(index):
    return type(index.table).__name__


def blob(item):
    return bytes(getattr(item, 'value', item) or b'')


def compressed(data, pos):
    """An ECMA-335 compressed unsigned integer: (value, next position)."""
    b = data[pos]
    if b & 0x80 == 0:
        return b, pos + 1
    if b & 0xC0 == 0x80:
        return ((b & 0x3F) << 8) | data[pos + 1], pos + 2
    return ((b & 0x1F) << 24) | (data[pos + 1] << 16) | (data[pos + 2] << 8) | data[pos + 3], pos + 4


def attribute_int(value):
    """The first fixed argument of an attribute whose constructor takes an int or an int enum."""
    return struct.unpack_from('<i', value, 2)[0] if value and len(value) >= 6 else None


def attribute_string(value):
    """The first fixed argument of an attribute whose constructor takes a string."""
    if not value or len(value) <= 2 or value[2] == 0xFF:
        return None
    n, pos = compressed(value, 2)
    return value[pos : pos + n].decode('utf-8')


def joined(ns, name):
    return f'{ns}.{name}' if ns else name


class Assembly:
    def __init__(self, path):
        self.path = path
        self.file_name = path.name
        self.tables = t = dnfile.dnPE(str(path)).net.mdtables
        self.name = str(t.Assembly[0].Name) if t.Assembly else path.stem
        self.references = [str(r.Name) for r in t.AssemblyRef or []]

    def typedef_name(self, row):
        return None, joined(str(row.TypeNamespace or ''), str(row.TypeName))

    def typeref_name(self, row):
        name = joined(str(row.TypeNamespace or ''), str(row.TypeName))
        scope = row.ResolutionScope
        kind = table_name(scope) if scope is not None and scope.row_index else None
        if kind == 'TypeRef':
            outer = self.typeref_name(scope.row)
            return outer[0], f'{outer[1]}/{name}'
        return (str(scope.row.Name) if kind == 'AssemblyRef' else None), name

    def type_ref(self, index):
        """(assembly name or None, full name) of a TypeDefOrRef coded index; a generic instance
        (class Foo : Bar<T>) names its generic type (Bar)."""
        if index is None or not index.row_index:
            return None
        kind = table_name(index)
        if kind == 'TypeDef':
            return self.typedef_name(index.row)
        if kind == 'TypeRef':
            return self.typeref_name(index.row)
        sig = blob(index.row.Signature)
        if len(sig) < 3 or sig[0] != GENERICINST:
            return None
        coded, _ = compressed(sig, 2)
        tag, row = coded & 3, coded >> 2
        if tag == 0:
            return self.typedef_name(self.tables.TypeDef[row - 1])
        if tag == 1:
            return self.typeref_name(self.tables.TypeRef[row - 1])
        return None

    @cached_property
    def types(self):
        t = self.tables
        rows = list(t.TypeDef or [])
        nested = {n.NestedClass.row_index for n in t.NestedClass or []}
        generic = {g.Owner.row_index for g in t.GenericParam or [] if table_name(g.Owner) == 'TypeDef'}
        methods = list(t.MethodDef or [])
        out, owner = [], {}
        for i, r in enumerate(rows, 1):
            ty = Type(self.name, i, str(r.TypeNamespace or ''), str(r.TypeName), r.struct.Flags,
                      self.type_ref(r.Extends), i in nested, i in generic)  # fmt: skip
            for m in r.MethodList or []:
                row = methods[m.row_index - 1]
                method = Method(str(row.Name), row.struct.Flags)
                ty.methods.append(method)
                owner[m.row_index] = (ty, method)
            out.append(ty)
        for ca in t.CustomAttribute or []:
            ctor, parent = ca.Type, ca.Parent
            if table_name(ctor) == 'MemberRef':
                cls = ctor.row.Class
                name = self.type_ref(cls)[1] if table_name(cls) in ('TypeRef', 'TypeDef') else ''
            else:
                name = owner[ctor.row_index][0].full_name if ctor.row_index in owner else ''
            kind = table_name(parent)
            if kind == 'TypeDef':
                out[parent.row_index - 1].attributes.append((name, blob(ca.Value)))
            elif kind == 'MethodDef' and parent.row_index in owner:
                owner[parent.row_index][1].attributes.append((name, blob(ca.Value)))
        return out


class Universe:
    """Assemblies that resolve each other's base classes."""

    def __init__(self, paths):
        self.assemblies = []
        self.types = {}
        for p in paths:
            try:
                a = Assembly(p)
            except Exception:  # native DLLs
                continue
            self.assemblies.append(a)
            for ty in a.types:
                if not ty.nested:
                    self.types.setdefault((a.name, ty.full_name), ty)
                    self.types.setdefault((None, ty.full_name), ty)

    def resolve(self, ty, ref):
        assembly, full = ref
        return self.types.get((assembly or ty.assembly, full)) or self.types.get((None, full))

    def is_script(self, ty):
        """Whether a type derives from MonoBehaviour or ScriptableObject."""
        for _ in range(64):
            if ty is None or ty.base is None:
                return False
            if ty.base[1] in SCRIPT_ROOTS:
                return True
            ty = self.resolve(ty, ty.base)
        return False
