"""Build Resources/unity_builtin_extra and compare it with the reference build's.

    uv run tools/builder/check_builtin_extra.py [--show 3]

Per object: the Shader tree (blob compared record by record, without the GXP source GUID) and
its serialized bytes once the reference's LZ4 stream (any stream will do) and, for project
shaders, gpuProgramIDs (the editor's hash is not reproduced) are put in. Then the whole file is
written again from those objects and compared byte for byte.
"""

import argparse
import collections
import copy
import re
import struct
import sys
import types

import builtin_extra as be
import lz4.block
import paths
import UnityPy
from check_shader_objects import compare_blob
from layout import Layout
from placement import Placement, same
from writer import TypeHashes

REFERENCE = paths.REFERENCE_MEDIA / 'Resources' / 'unity_builtin_extra'


def reference_table(data):
    """[(path ID, byte start, byte size)] of the reference file's object table."""
    meta = struct.unpack_from('>I', data, 0)[0]
    p = data.index(b'\0', 20) + 1 + 4 + 1  # unity version, platform, type tree flag
    count = struct.unpack_from('<i', data, p)[0]
    p += 4 + count * 23  # class ID, stripped, script index, old type hash (no MonoBehaviours here)
    table = []
    n = struct.unpack_from('<i', data, p)[0]
    p += 4
    for _ in range(n):
        p = (p + 3) & ~3
        pid, start, size, _t = struct.unpack_from('<qIIi', data, p)
        table.append((pid, start, size))
        p += 20
    assert p <= 20 + meta
    return table


def with_reference_values(tree, theirs):
    """Our tree with the reference's LZ4 stream and gpuProgramIDs."""
    tree = copy.deepcopy(tree)
    tree['compressedBlob'] = bytes(theirs['compressedBlob'])
    tree['compressedLengths'] = theirs['compressedLengths']
    ours_form, their_form = tree['m_ParsedForm'], theirs['m_ParsedForm']
    for s1, s2 in zip(ours_form['m_SubShaders'], their_form['m_SubShaders'], strict=True):
        for p1, p2 in zip(s1['m_Passes'], s2['m_Passes'], strict=True):
            p1['m_State']['gpuProgramID'] = p2['m_State']['gpuProgramID']
    return tree


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--show', type=int, default=3)
    args = parser.parse_args()
    layout = Layout()
    files = [f'sharedassets{i}.assets' for i in range(len(layout.scenes))]
    files += ['resources.assets', 'globalgamemanagers.assets']
    build = types.SimpleNamespace(
        layout=layout, placement=Placement(layout, files), files=files, hashes=TypeHashes(None)
    )
    ref_data = REFERENCE.read_bytes()
    table = reference_table(ref_data)
    ref_objects = next(iter(UnityPy.load(str(REFERENCE)).files.values())).objects

    builder = be.Builder(layout)
    ids = be.builtin_shader_ids(layout)
    built = [(fid, builder.shader(fid)) for fid in ids]
    built += [(be.PROJECT_SHADER, tree) for tree in be.project_trees(build)]
    print(f'built-in shaders: {len(ids)}, project Always Included shaders: {len(built) - len(ids)}')
    ref_ids = [pid for pid, _, _ in table]
    print(f'object table: ours {sorted({p for p, _ in built})}')
    print(f'              ref  {sorted(ref_ids)}')

    totals, fields, examples = collections.Counter(), collections.Counter(), collections.defaultdict(list)
    substituted = []
    always = [(g, be.PROJECT_SHADER) for g in be.always_included_project()]
    project_where = [build.placement.where[k] for k in always if k in build.placement.where]
    ggm = next(iter(UnityPy.load(str(paths.REFERENCE_MEDIA / 'globalgamemanagers.assets')).files.values())).objects
    for i, (pid, tree) in enumerate(built):
        if pid == be.PROJECT_SHADER:
            # each equals its globalgamemanagers.assets copy (the earlier ones are the dead bytes)
            obj = ggm[project_where[i - len(ids)][1]]
        elif pid in ref_objects:
            obj = ref_objects[pid]
        else:
            totals['not in the reference'] += 1
            substituted.append((pid, be.serialize(tree)))
            continue
        theirs, raw = obj.read_typetree(), obj.get_raw_data()
        name = tree['m_ParsedForm']['m_Name']
        blob = bytes(theirs['compressedBlob'])
        size = theirs['decompressedLengths'][0]
        theirs_raw = lz4.block.decompress(blob[: theirs['compressedLengths'][0]], uncompressed_size=size)
        diffs = []
        same(tree, theirs, '', diffs, [''], be.NAME)
        compare_blob(tree, {'raw': theirs_raw}, diffs)
        accepted = ('.compressedBlob', '.compressedLengths[0]')
        real = [d for d in diffs if not d[0].endswith('.gpuProgramID') and d[0] not in accepted]
        totals['gpuProgramID differs'] += any(d[0].endswith('.gpuProgramID') for d in diffs)
        totals['same' if not real else 'differs'] += 1
        fixed = with_reference_values(tree, theirs)
        data = be.serialize(fixed)
        totals['serialized bytes equal'] += data == raw
        substituted.append((pid, data))
        for path, ours, ref in real:
            key = re.sub(r'\[\d+\]', '[]', path)
            fields[key] += 1
            if len(examples[key]) < args.show:
                examples[key].append(f'{name}: {path}: ours {str(ours)[:80]} | ref {str(ref)[:80]}')
        print(f'{pid} {name}: {"same" if not real else f"{len(real)} differences"}', flush=True)

    whole = be.write_file(substituted, build.hashes.builtin(48))
    print()
    for k, v in sorted(totals.items()):
        print(f'  {k}: {v}')
    state = 'identical' if whole == ref_data else 'differs'
    print(f'  file with the reference LZ4 streams and gpuProgramIDs: {state} ({len(whole)} vs {len(ref_data)} bytes)')
    ours = be.write_file([(p, be.serialize(t)) for p, t in built], build.hashes.builtin(48))
    print(f'  file as built: {len(ours)} bytes (reference {len(ref_data)})')
    print('fields that differ (count of values):')
    for k, v in fields.most_common():
        print(f'  {v:6} {k}')
        for e in examples[k]:
            print(f'           {e}')
    builder.pool.shutdown()
    return 0 if whole == ref_data else 1


if __name__ == '__main__':
    sys.exit(main())
