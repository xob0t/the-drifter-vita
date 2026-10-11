"""Build every Shader object the layout places and compare it with the reference build.

    uv run tools/builder/check_shader_objects.py [--show 3] [shader names...]

Shaders pair by (file, path ID). The blob is compared record by record after decompression
(any LZ4 stream will do, so the compressed bytes are not compared); a record's code compares
without the GXP's source GUID, a hash of the path psp2cgc read the program from.
"""

import argparse
import collections
import re
import sys

import convert
import lz4.block
import paths
import reference
import UnityPy
from layout import Layout
from placement import Placement, same
from shader_object import ProjectShaders, build_order

GUID_OFFSET = 12 + 16  # code header (12 bytes), then the GXP's source GUID at offset 16


def without_guid(code):
    return code[:GUID_OFFSET] + b'\0' * 4 + code[GUID_OFFSET + 4 :]


def compare_blob(mine, theirs, diffs):
    raw = lz4.block.decompress(mine['compressedBlob'], uncompressed_size=mine['decompressedLengths'][0])
    a = reference.parse_blob(raw)
    b = reference.parse_blob(theirs['raw'])
    if len(a) != len(b):
        diffs.append(('blob.records', f'{len(a)} records', f'{len(b)} records'))
        return
    for i, (x, y) in enumerate(zip(a, b, strict=True)):
        for key in ('version', 'type', 'keywords', 'tail'):
            if x[key] != y[key]:
                diffs.append((f'blob.record[{i}].{key}', str(x[key])[:60], str(y[key])[:60]))
        if without_guid(x['code']) != without_guid(y['code']):
            diffs.append((f'blob.record[{i}].code', f'{len(x["code"])} bytes', f'{len(y["code"])} bytes'))


FIELDS = [
    'm_ParsedForm.m_PropInfo', 'm_ParsedForm.m_Name', 'm_ParsedForm.m_CustomEditorName',
    'm_ParsedForm.m_FallbackName', 'm_ParsedForm.m_Dependencies', 'm_ParsedForm.m_DisableNoSubshadersMessage',
    'm_ParsedForm.m_SubShaders (without gpuProgramID)', 'gpuProgramID', 'm_Name', 'platforms', 'offsets',
    'compressedLengths', 'decompressedLengths', 'compressedBlob', 'blob records (without GXP source GUID)',
    'm_Dependencies', 'm_NonModifiableTextures', 'm_ShaderIsBaked',
]  # fmt: skip


def field_of(path):
    if path.endswith('.gpuProgramID'):
        return 'gpuProgramID'
    if path.startswith('blob.'):
        return 'blob records (without GXP source GUID)'
    parts = path.lstrip('.').split('.')
    top = re.sub(r'\[\d+\]$', '', parts[0])
    if top == 'm_ParsedForm' and len(parts) > 1:
        sub = re.sub(r'\[\d+\]$', '', parts[1])
        return f'm_ParsedForm.{sub}' + (' (without gpuProgramID)' if sub == 'm_SubShaders' else '')
    return top


def serialized(mine, theirs, node):
    """Whether our object, with the reference's LZ4 stream and gpuProgramIDs, serializes to the
    reference's bytes."""
    import copy

    from UnityPy.helpers.TypeTreeHelper import write_typetree
    from UnityPy.streams import EndianBinaryWriter

    t = theirs['tree']
    mine = copy.deepcopy(mine)
    mine['compressedBlob'] = bytes(t['compressedBlob'])
    mine['compressedLengths'] = t['compressedLengths']
    for s1, s2 in zip(mine['m_ParsedForm']['m_SubShaders'], t['m_ParsedForm']['m_SubShaders'], strict=True):
        for p1, p2 in zip(s1['m_Passes'], s2['m_Passes'], strict=True):
            p1['m_State']['gpuProgramID'] = p2['m_State']['gpuProgramID']
    w = EndianBinaryWriter(endian='<')
    write_typetree(mine, node, w)
    return w.bytes == theirs['bytes']


def categorize(path):
    """Name the known, accepted differences."""
    if path.endswith('.gpuProgramID'):
        return 'gpuProgramID (editor hash not reproduced)'
    if path in ('.compressedBlob', '.compressedLengths[0]'):
        return 'LZ4 stream (any valid stream works)'
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('names', nargs='*')
    parser.add_argument('--show', type=int, default=3)
    args = parser.parse_args()
    layout = Layout()
    shared = sorted(
        (p.name for p in paths.REFERENCE_MEDIA.glob('sharedassets*.assets')),
        key=lambda n: int(re.search(r'\d+', n)[0]),
    )
    files = ['globalgamemanagers.assets', *shared, 'resources.assets']
    placement = Placement(layout, files)
    shaders = ProjectShaders(layout, placement)
    refs = {(s['file'], s['pid']): s for s in reference.shaders()}
    for f in {f for f, _ in refs}:
        objects = next(iter(UnityPy.load(str(paths.REFERENCE_MEDIA / f)).files.values())).objects
        for (g, pid), s in refs.items():
            if g == f:
                s['bytes'] = objects[pid].get_raw_data()
    placed = shaders.placed(build_order(files))
    built = shaders.build_all(build_order(files))
    node = convert.release_tree(48)

    totals, fields, examples = collections.Counter(), collections.Counter(), collections.defaultdict(list)
    accepted, per_field = collections.Counter(), collections.Counter()
    seen = set()
    for f, pid, _g, _fid in placed:
        theirs = refs.get((f, pid))
        mine = built[(f, pid)]
        name = mine['m_ParsedForm']['m_Name']
        if args.names and name not in args.names:
            continue
        if theirs is None:
            totals['placed, not in the reference'] += 1
            continue
        seen.add((f, pid))
        diffs = []
        tree = dict(theirs['tree'])
        same(mine, tree, '', diffs, [''], f)
        compare_blob(mine, theirs, diffs)
        real = []
        for d in diffs:
            category = categorize(d[0])
            if category:
                accepted[category] += 1
            else:
                real.append(d)
        differing = {field_of(d[0]) for d in diffs}
        for key in FIELDS:
            per_field[key] += key not in differing
        totals['same' if not real else 'differs'] += 1
        totals['serialized bytes equal'] += serialized(mine, theirs, node)
        for path, ours, ref in real:
            key = re.sub(r'\[\d+\]', '[]', path)
            fields[key] += 1
            if len(examples[key]) < args.show:
                examples[key].append(f'{name}: {path}: ours {str(ours)[:80]} | ref {str(ref)[:80]}')
        print(f'{f} {pid} {name}: {"same" if not real else f"{len(real)} differences"}', flush=True)
    missing = [s['name'] for k, s in refs.items() if k not in seen and not args.names]
    print()
    print(f'{len(placed)} shaders placed; reference has {len(refs)}')
    for k, v in sorted(totals.items()):
        print(f'  {k}: {v}')
    if missing:
        print('  in the reference, not built:', ', '.join(missing))
    print(f'fields equal to the reference (of {len(seen)} shaders):')
    for key in FIELDS:
        print(f'  {per_field[key]:4} {key}')
    print('accepted differences (count of values):')
    for k, v in sorted(accepted.items()):
        print(f'  {v:6} {k}')
    print('fields that differ (count of values):')
    for k, v in fields.most_common():
        print(f'  {v:6} {k}')
        for e in examples[k]:
            print(f'           {e}')
    shaders.pool.shutdown()
    return 0 if totals['differs'] == 0 and not missing else 1


if __name__ == '__main__':
    sys.exit(main())
