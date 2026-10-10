"""Build every Shader object of the Windows build and compare it with the Windows reference.

    uv run --with lz4 tools/pure/check_windows_shaders.py [--only name,...] [-v]

Per shader (paired by file and path ID): the parsed form field by field (gpuProgramID's low
16 bits of project shaders aside, see shader_object.py), the platforms, and the decompressed
blob record by record.
"""

import argparse
import sys
from pathlib import Path

import builtin_extra
import lz4.block
import UnityPy
import windows_build
import windows_builtin_extra
import windows_shaders
from build import Build
from check_windows import field_diff
from reference import parse_blob
from shader_object import BUILTIN_EXTRA, build_order


def reference_shaders(folder):
    out = {}
    names = [*sorted(p.name for p in folder.iterdir() if p.is_file()), 'Resources/unity_builtin_extra']
    for name in names:
        if name == 'Resources/unity_builtin_extra':
            pass
        elif not name.startswith(('sharedassets', 'resources.assets', 'globalgamemanagers.assets')):
            continue
        if name.endswith(('.resS', '.resource')):
            continue
        for pid, o in next(iter(UnityPy.load(str(folder / name)).files.values())).objects.items():
            if o.type.name == 'Shader':
                out[(name, pid)] = o.read_typetree()
    return out


def blob(tree):
    data = bytes(tree['compressedBlob'])
    return lz4.block.decompress(data[: tree['compressedLengths'][0]], uncompressed_size=tree['decompressedLengths'][0])


def strip_ids(form, project_shader):
    """gpuProgramID of a project shader: only the snippet index (high 16 bits) is reproduced."""
    if project_shader:
        for sub in form['m_SubShaders']:
            for ps in sub['m_Passes']:
                ps['m_State']['gpuProgramID'] >>= 16
    return form


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--only', default='')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()
    only = set(filter(None, args.only.split(',')))
    windows_build.register(Build)
    b = Build(Path('unused'), 'windows')
    guid_of = {}
    shaders = windows_shaders.WindowsShaders(b.layout, b.placement, b.root)
    made = shaders.build_all(build_order(b.files))
    shaders.pool.shutdown()
    b.shader_objects = made
    extra = windows_builtin_extra.Builder(b.layout, b.root)
    for fid in builtin_extra.builtin_shader_ids(b.layout, b.root):
        made[('Resources/unity_builtin_extra', fid)] = extra.shader(fid)
        guid_of[('Resources/unity_builtin_extra', fid)] = BUILTIN_EXTRA
    for tree in windows_builtin_extra.project_trees(b):
        made[('Resources/unity_builtin_extra', builtin_extra.PROJECT_SHADER)] = tree
    extra.pool.shutdown()
    for f in b.files:
        for g, fid, cid in b.layout.objects(f):
            if cid == 48:
                guid_of[(f, b.placement.where[(g, fid)][1])] = g
    ref = reference_shaders(b.target.reference)
    same = 0
    for key in sorted(ref.keys() | made.keys()):
        theirs, mine = ref.get(key), made.get(key)
        name = (theirs or mine)['m_ParsedForm']['m_Name']
        if only and name not in only:
            continue
        if theirs is None or mine is None:
            print(f'{key} {name}: only {"built" if theirs is None else "in the reference"}')
            continue
        problems = []
        project_shader = guid_of.get(key) != BUILTIN_EXTRA
        diff = field_diff(
            strip_ids(mine['m_ParsedForm'], project_shader), strip_ids(theirs['m_ParsedForm'], project_shader), limit=6
        )
        if diff:
            problems.append('parsed form: ' + '; '.join(diff))
        if list(mine['platforms']) != list(theirs['platforms']):
            problems.append(f'platforms {mine["platforms"]} vs {theirs["platforms"]}')
        ra, rb = blob(mine), blob(theirs)
        if ra != rb:
            a, c = parse_blob(ra), parse_blob(rb)
            if len(a) != len(c):
                problems.append(f'{len(a)} records vs {len(c)}')
            bad = [i for i, (x, y) in enumerate(zip(a, c, strict=False)) if x != y]
            if bad:
                x, y = a[bad[0]], c[bad[0]]
                what = [k for k in x if x[k] != y.get(k)]
                problems.append(f'records differ: {len(bad)} (first {bad[0]}: {what})')
        if problems:
            print(f'{key} {name}:')
            for p in problems:
                print('   ', p)
        else:
            same += 1
            if args.verbose:
                print(f'{key} {name}: identical')
    print(f'{same} shaders identical')
    return 0


if __name__ == '__main__':
    sys.exit(main())
