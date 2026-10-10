"""Fingerprint generated outputs, to show that a refactor of the pipeline left them unchanged.

    uv run tools/downgrade/fingerprint.py write <manifest.json> <folder>...   # SHA-256 of every file
    uv run tools/downgrade/fingerprint.py compare <before.json> <after.json>  # exit 1 if any file differs

Folders are recorded relative to the repository root. Unity's import cache (Library) and
temporary folders are left out. Managed assemblies also get an "il" hash with their module
version id (MVID), PE timestamp and <PrivateImplementationDetails>{GUID} type name zeroed (the
.NET Framework C# compiler makes them new on every compile), so a DLL that was only recompiled
from the same source compares equal on that hash.
"""

import hashlib
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIPPED = {'Library', 'Temp', 'obj', 'Logs'}
PRIVATE_DETAILS = re.compile(rb'(<PrivateImplementationDetails>\{)[0-9A-Fa-f-]{36}\}')


def mvid_offset(data: bytes):
    """Offset of the MVID in a .NET assembly (ECMA-335 #GUID heap, Module row 1), or None."""
    try:
        pe = struct.unpack_from('<I', data, 0x3C)[0]
        if data[pe : pe + 4] != b'PE\0\0':
            return None
        sections, optional = struct.unpack_from('<H', data, pe + 6)[0], struct.unpack_from('<H', data, pe + 20)[0]
        magic = struct.unpack_from('<H', data, pe + 24)[0]
        directories = pe + 24 + (96 if magic == 0x10B else 112)
        cli_rva = struct.unpack_from('<I', data, directories + 14 * 8)[0]
        if not cli_rva:
            return None
        table = pe + 24 + optional

        def offset(rva):
            for i in range(sections):
                virtual_size, virtual, _, raw = struct.unpack_from('<IIII', data, table + 40 * i + 8)
                if virtual <= rva < virtual + max(virtual_size, 1):
                    return rva - virtual + raw
            raise ValueError(rva)

        cli = offset(cli_rva)
        metadata = offset(struct.unpack_from('<I', data, cli + 8)[0])
        version_length = struct.unpack_from('<I', data, metadata + 12)[0]
        streams = metadata + 16 + version_length
        count = struct.unpack_from('<H', data, streams + 2)[0]
        position, heaps = streams + 4, {}
        for _ in range(count):
            start, _ = struct.unpack_from('<II', data, position)
            end = data.index(b'\0', position + 8)
            name = data[position + 8 : end].decode()
            position = (end + 4) & ~3
            heaps[name] = metadata + start
        tables = heaps['#~']
        heap_sizes = data[tables + 6]
        string_size, guid_size = (4 if heap_sizes & 1 else 2), (4 if heap_sizes & 2 else 2)
        # Module row: Generation (2), Name (string index), Mvid (GUID index), EncId, EncBaseId.
        valid = struct.unpack_from('<Q', data, tables + 8)[0]
        rows = tables + 24 + 4 * bin(valid).count('1')
        index_at = rows + 2 + string_size
        mvid = struct.unpack_from('<I' if guid_size == 4 else '<H', data, index_at)[0]
        return heaps['#GUID'] + 16 * (mvid - 1)
    except (struct.error, ValueError, KeyError, IndexError):
        return None


def describe(path: Path):
    data = path.read_bytes()
    entry = {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    if path.suffix.lower() in ('.dll', '.exe'):
        at = mvid_offset(data)
        if at is not None:
            stamp = struct.unpack_from('<I', data, 0x3C)[0] + 8  # COFF header TimeDateStamp
            data = bytearray(data)
            data[at : at + 16], data[stamp : stamp + 4] = bytes(16), bytes(4)
            data = PRIVATE_DETAILS.sub(lambda m: m[1] + b'0' * 36 + b'}', bytes(data))
            entry['il'] = hashlib.sha256(data).hexdigest()
    return entry


def write(manifest: Path, folders):
    files = {}
    for folder in folders:
        folder = Path(folder).resolve()
        for path in sorted(folder.rglob('*')):
            if path.is_file() and not SKIPPED & set(path.relative_to(folder).parts):
                files[path.relative_to(ROOT).as_posix()] = describe(path)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(files, indent=1, sort_keys=True), encoding='utf-8')
    print(f'{manifest}: {len(files)} files')


def compare(before: Path, after: Path):
    a, b = (json.loads(p.read_text(encoding='utf-8')) for p in (before, after))
    changed = [k for k in sorted(set(a) & set(b)) if a[k]['sha256'] != b[k]['sha256']]
    only_il = [k for k in changed if a[k].get('il') and a[k].get('il') == b[k].get('il')]
    for k in sorted(set(a) - set(b)):
        print(f'only before: {k}')
    for k in sorted(set(b) - set(a)):
        print(f'only after:  {k}')
    for k in changed:
        print(f'differs:     {k}' + ('  (only compiler stamps)' if k in only_il else ''))
    same = len(set(a) & set(b)) - len(changed)
    print(
        f'{same} identical, {len(only_il)} differ only in compiler stamps, {len(changed) - len(only_il)} differ, '
        f'{len(set(a) ^ set(b))} in one manifest only'
    )
    return 0 if len(changed) == len(only_il) and set(a) == set(b) else 1


if __name__ == '__main__':
    if len(sys.argv) >= 4 and sys.argv[1] == 'write':
        write(Path(sys.argv[2]), sys.argv[3:])
    elif len(sys.argv) == 4 and sys.argv[1] == 'compare':
        sys.exit(compare(Path(sys.argv[2]), Path(sys.argv[3])))
    else:
        sys.exit(__doc__)
