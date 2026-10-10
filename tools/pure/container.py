"""
Standalone reader/writer for Unity SerializedFile containers (.assets / levelN /
globalgamemanagers), for Unity 2018.2.19f1 (serialized version 17), PSP2 target,
WITHOUT type trees. Pure Python 3, no UnityPy.

    python serialized_container.py <file> [...]      # parse -> rebuild -> compare bytes
    from serialized_container import parse_serialized, write_serialized, HeaderInfo

FORMAT RULES (derived from the Media/ answer key; all 127 version-17 files in
builds/drifter-2018.2.19f1-vita/Media satisfy them, and the rebuild is byte-identical)

1. Header: 20 bytes. The four u32 fields are BIG-endian regardless of content endian.
     [0:4]    metadata_size  = len(metadata blob)   (excludes header and padding)
     [4:8]    file_size      = total file length
     [8:12]   version        = 17
     [12:16]  data_offset    = absolute offset of the object-data region
     [16]     endian         = 0 little / 1 big (applies to the metadata blob only)
     [17:20]  reserved       = 00 00 00

2. data_offset = max(4096, align16(20 + metadata_size)).
   Bytes between the metadata end and data_offset are zero.
   (UnityPy's save() only does align16, so it places data at 1168 for level0 instead of
   4096, which is the whole size mismatch: 3656 = 1168 + 2488 vs 6584 = 4096 + 2488.)

3. Metadata blob (starts at file offset 20, endian = header endian):
     unity version      C string, e.g. "2018.2.19f1\0"
     target platform    i32 (30 = PSP2)
     enable_type_tree   u8 (0 in every file here; 1 would add a type-tree blob per type)
     type count         i32, then per type:
        class_id i32, is_stripped u8, script_type_index i16 (-1 in all built-in types seen),
        script_id 16 bytes  ONLY if class_id == 114 (MonoBehaviour),
        old_type_hash 16 bytes
     object count       i32, then per object (align 4 before each entry):
        path_id i64, byte_start u32 (relative to data_offset), byte_size u32, type_id i32
        -> class_id is not stored per object for v>=16; it comes from types[type_id]
        -> entries are sorted by path_id (ascending) in every file
     script types       i32 count, then per entry: file_index i32, align 4, local_id i64
     externals          i32 count, then per entry: temp_empty C string, guid 16 bytes,
                        type i32, path C string
     ref types          only for version >= 20 (absent in v17: no count written)
     user information   C string (empty -> single 00)

4. Object data region (starts at data_offset):
   - Objects are laid out in a DATA ORDER that is not always path_id order. Unity's own
     write order is recorded only through byte_start. In 126 of 127 files it equals path_id
     order; globalgamemanagers.assets is the exception (all 327 MonoScripts, class 115,
     first, then PreloadData pid 1, Material pid 2, Shaders pids 3..16, ...).
   - Each object starts at an 8-byte-aligned offset. The padding before each object is
     zeros and is < 8 bytes. There are NO trailing bytes after the last object (UnityPy
     pads after every object including the last one, which adds 0-7 stray bytes for files
     whose last object size is not a multiple of 8, e.g. level7 and globalgamemanagers.assets).
   - parse_serialized() returns objects in data order; write_serialized() takes the
     same data order and sorts the metadata table by path_id itself.
"""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from pathlib import Path

HEADER_SIZE = 20
DATA_ALIGN = 8
MIN_DATA_OFFSET = 4096
SUPPORTED_VERSION = 17
MONOBEHAVIOUR = 114


# --------------------------------------------------------------------------- model


@dataclass
class HeaderInfo:
    version: int = SUPPORTED_VERSION
    endian: str = '<'  # "<" little, ">" big (metadata blob endian)
    unity_version: str = '2018.2.19f1'
    target_platform: int = 30  # PSP2
    enable_type_tree: bool = False
    user_information: str = ''
    reserved: bytes = b'\x00\x00\x00'


@dataclass
class TypeEntry:
    class_id: int
    is_stripped: bool = False
    script_type_index: int = -1
    script_id: bytes | None = None  # 16 bytes, required when class_id == 114
    old_type_hash: bytes = b'\x00' * 16


@dataclass
class ObjectEntry:
    path_id: int
    type_id: int  # index into types
    data: bytes  # raw object bytes; byte_size == len(data)


@dataclass
class ScriptType:
    file_index: int
    local_id: int


@dataclass
class External:
    path: str
    temp_empty: str = ''
    guid: bytes = b'\x00' * 16
    type: int = 0


@dataclass
class SerializedFile:
    header: HeaderInfo
    types: list[TypeEntry]
    objects: list[ObjectEntry]  # in DATA order (= on-disk byte_start order)
    script_types: list[ScriptType]
    externals: list[External]


# --------------------------------------------------------------------------- byte IO


def _align(n: int, a: int) -> int:
    return (n + a - 1) // a * a


class _Reader:
    def __init__(self, data: bytes, pos: int, endian: str):
        self.d = data
        self.p = pos
        self.e = endian

    def _unpack(self, fmt: str, size: int):
        v = struct.unpack_from(self.e + fmt, self.d, self.p)[0]
        self.p += size
        return v

    def i32(self) -> int:
        return self._unpack('i', 4)

    def i16(self) -> int:
        return self._unpack('h', 2)

    def i64(self) -> int:
        return self._unpack('q', 8)

    def u32(self) -> int:
        return self._unpack('I', 4)

    def u8(self) -> int:
        v = self.d[self.p]
        self.p += 1
        return v

    def bytes_(self, n: int) -> bytes:
        v = bytes(self.d[self.p : self.p + n])
        self.p += n
        return v

    def cstr(self) -> str:
        end = self.d.index(b'\0', self.p)
        s = self.d[self.p : end].decode('utf8', 'surrogateescape')
        self.p = end + 1
        return s

    def align(self, a: int) -> None:
        self.p = _align(self.p, a)


class _Writer:
    def __init__(self, endian: str):
        self.e = endian
        self.buf = bytearray()

    def _pack(self, fmt: str, v) -> None:
        self.buf += struct.pack(self.e + fmt, v)

    def i32(self, v: int):
        self._pack('i', v)

    def i16(self, v: int):
        self._pack('h', v)

    def i64(self, v: int):
        self._pack('q', v)

    def u32(self, v: int):
        self._pack('I', v)

    def u8(self, v: int) -> None:
        self.buf.append(v & 0xFF)

    def bytes_(self, b: bytes) -> None:
        self.buf += b

    def cstr(self, s: str) -> None:
        self.buf += s.encode('utf8', 'surrogateescape') + b'\0'

    def align(self, a: int) -> None:
        self.buf += b'\0' * ((a - len(self.buf) % a) % a)


# --------------------------------------------------------------------------- parse


def parse_serialized(data: bytes) -> SerializedFile:
    ms, fs, version, data_offset = struct.unpack_from('>IIII', data, 0)
    if version != SUPPORTED_VERSION:
        raise NotImplementedError(f'serialized version {version} unsupported (only {SUPPORTED_VERSION})')
    if fs != len(data):
        raise ValueError('file_size field does not match input length')
    endian = '>' if data[16] else '<'
    hdr = HeaderInfo(version=version, endian=endian, reserved=bytes(data[17:20]))

    r = _Reader(data, HEADER_SIZE, endian)
    hdr.unity_version = r.cstr()
    hdr.target_platform = r.i32()
    hdr.enable_type_tree = bool(r.u8())
    if hdr.enable_type_tree:
        raise NotImplementedError('type trees are not supported by this writer')

    types: list[TypeEntry] = []
    for _ in range(r.i32()):
        t = TypeEntry(class_id=r.i32())
        t.is_stripped = bool(r.u8())
        t.script_type_index = r.i16()
        if t.class_id == MONOBEHAVIOUR:
            t.script_id = r.bytes_(16)
        t.old_type_hash = r.bytes_(16)
        types.append(t)

    entries = []
    for _ in range(r.i32()):
        r.align(4)
        path_id = r.i64()
        byte_start = r.u32()
        byte_size = r.u32()
        type_id = r.i32()
        entries.append((path_id, byte_start, byte_size, type_id))

    script_types: list[ScriptType] = []
    for _ in range(r.i32()):
        fi = r.i32()
        r.align(4)
        script_types.append(ScriptType(fi, r.i64()))

    externals: list[External] = []
    for _ in range(r.i32()):
        temp_empty = r.cstr()
        guid = r.bytes_(16)
        typ = r.i32()
        externals.append(External(path=r.cstr(), temp_empty=temp_empty, guid=guid, type=typ))

    hdr.user_information = r.cstr()
    if r.p != HEADER_SIZE + ms:
        raise ValueError(f'metadata ended at {r.p - HEADER_SIZE}, header says {ms}')
    if data_offset != data_offset_for(ms):
        raise ValueError(f'data_offset {data_offset} breaks the data_offset rule')

    # Objects in data order; verify the layout is exactly what write_serialized produces.
    entries.sort(key=lambda e: e[1])
    objects: list[ObjectEntry] = []
    cursor = 0
    for path_id, byte_start, byte_size, type_id in entries:
        gap = byte_start - cursor
        if not (0 <= gap < DATA_ALIGN) or any(data[data_offset + cursor + i] for i in range(gap)):
            raise ValueError(f'object {path_id}: unexpected non-padding gap of {gap} bytes')
        start = data_offset + byte_start
        objects.append(ObjectEntry(path_id, type_id, bytes(data[start : start + byte_size])))
        cursor = byte_start + byte_size
    if data_offset + cursor != len(data):
        raise ValueError('trailing bytes after the last object')

    return SerializedFile(hdr, types, objects, script_types, externals)


# --------------------------------------------------------------------------- write


def data_offset_for(metadata_size: int) -> int:
    return max(MIN_DATA_OFFSET, _align(HEADER_SIZE + metadata_size, 16))


def write_serialized(
    header_info: HeaderInfo,
    types: list[TypeEntry],
    objects: list[ObjectEntry],
    externals: list[External],
    script_types: list[ScriptType] | None = None,
) -> bytes:
    """Build a version-17 serialized file byte-identical to Unity's output.

    `objects` must be given in DATA order (the order the bytes are laid out); the metadata
    object table is emitted sorted by path_id. For normal files data order == path_id order.
    """
    h = header_info
    if h.version != SUPPORTED_VERSION:
        raise NotImplementedError('only serialized version 17 is supported')
    if h.enable_type_tree:
        raise NotImplementedError('type trees are not supported')
    script_types = script_types or []
    if len({o.path_id for o in objects}) != len(objects):
        raise ValueError('duplicate path_id')

    # ---- object data region: each object starts 8-aligned, zero padding before it
    region = bytearray()
    starts = {}
    for o in objects:
        region += b'\0' * ((DATA_ALIGN - len(region) % DATA_ALIGN) % DATA_ALIGN)
        starts[o.path_id] = len(region)
        region += o.data

    # ---- metadata blob
    w = _Writer(h.endian)
    w.cstr(h.unity_version)
    w.i32(h.target_platform)
    w.u8(1 if h.enable_type_tree else 0)

    w.i32(len(types))
    for t in types:
        w.i32(t.class_id)
        w.u8(1 if t.is_stripped else 0)
        w.i16(t.script_type_index)
        if t.class_id == MONOBEHAVIOUR:
            assert t.script_id is not None and len(t.script_id) == 16
            w.bytes_(t.script_id)
        assert len(t.old_type_hash) == 16
        w.bytes_(t.old_type_hash)

    w.i32(len(objects))
    for o in sorted(objects, key=lambda o: o.path_id):
        w.align(4)
        w.i64(o.path_id)
        w.u32(starts[o.path_id])
        w.u32(len(o.data))
        w.i32(o.type_id)

    w.i32(len(script_types))
    for s in script_types:
        w.i32(s.file_index)
        w.align(4)
        w.i64(s.local_id)

    w.i32(len(externals))
    for e in externals:
        w.cstr(e.temp_empty)
        w.bytes_(e.guid)
        w.i32(e.type)
        w.cstr(e.path)

    # no ref-type count for version 17
    w.cstr(h.user_information)
    meta = bytes(w.buf)

    # ---- header, zero padding to data_offset, data region
    data_offset = data_offset_for(len(meta))
    file_size = data_offset + len(region)
    header = struct.pack('>IIII', len(meta), file_size, h.version, data_offset)
    header += bytes([1 if h.endian == '>' else 0]) + h.reserved
    pad = b'\0' * (data_offset - HEADER_SIZE - len(meta))
    return header + meta + pad + bytes(region)


# --------------------------------------------------------------------------- self-check


def rebuild(data: bytes) -> bytes:
    sf = parse_serialized(data)
    return write_serialized(sf.header, sf.types, sf.objects, sf.externals, sf.script_types)


def main(argv: list[str]) -> int:
    ok_all = True
    for name in argv:
        raw = Path(name).read_bytes()
        out = rebuild(raw)
        same = out == raw
        ok_all &= same
        first = next((i for i, (a, b) in enumerate(zip(raw, out, strict=False)) if a != b), None)
        print(
            f'{name}: orig={len(raw)} rebuilt={len(out)} identical={same}'
            + ('' if same or first is None else f' first_diff@{first}')
        )
    return 0 if ok_all else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
