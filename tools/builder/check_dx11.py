"""Rebuild every Direct3D 11 shader program of the Windows reference build from source and compare.

    uv run --with lz4 tools/builder/check_dx11.py [--only name,...] [-v]

For each d3d11 subprogram of builds/drifter-2018.2.19f1: compile it with dx11.py, then compare
the record's program code (header + stripped DXBC), the binding tail, the GPU program type and the
parsed form's binding lists with the reference. Shader source comes from the PC project, then
Unity's built-in shader source; surface and fixed-function passes of built-in shaders compile
the snippets the editor's unity_builtin_extra stores.
"""

import argparse
import collections
import pickle
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import cache

import dx11
import lz4.block
import paths
import UnityPy
from reference import parse_blob
from shaderlab import find_shaders

WINDOWS = paths.ROOT / 'builds' / 'drifter-2018.2.19f1'
WINDOWS_DATA = WINDOWS / 'TheDrifter_Data'
PC_PROJECT = paths.common.WORK / 'drifter-2018.2.19f1'
BUILTIN_SOURCE = paths.WORK / 'builtin_shaders'
CACHE = paths.WORK / 'windows' / 'ref_shaders.pickle'
DATA_SKIP = {'.resS', '.resource'}


def reference_shaders():
    """[{file, pid, tree, raw}] of every Shader in the Windows build, with its decompressed blob."""
    if CACHE.exists():
        return pickle.loads(CACHE.read_bytes())
    files = [p for p in sorted(WINDOWS_DATA.iterdir()) if p.is_file() and p.suffix not in DATA_SKIP]
    files += [WINDOWS_DATA / 'Resources' / 'unity_builtin_extra']
    out = []
    for path in files:
        if path.name in ('boot.config', 'app.info'):
            continue
        for o in UnityPy.load(str(path)).objects:
            if o.type.name != 'Shader':
                continue
            t = o.read_typetree()
            if not t['compressedLengths']:
                continue
            blob = bytes(t['compressedBlob'])
            raw = lz4.block.decompress(blob[: t['compressedLengths'][0]], uncompressed_size=t['decompressedLengths'][0])
            t['compressedBlob'] = None
            out.append({'file': path.relative_to(WINDOWS_DATA).as_posix(), 'pid': o.path_id, 'tree': t, 'raw': raw})
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_bytes(pickle.dumps(out))
    return out


def sources():
    """Shader name -> source path: the PC project first, then Unity's built-in shaders."""
    out = find_shaders(PC_PROJECT)
    for f in sorted(BUILTIN_SOURCE.rglob('*.shader')):
        m = re.search(r'^\s*Shader\s*"([^"]+)"', f.read_text(encoding='utf-8', errors='replace'), re.M)
        if m and m.group(1) not in out:
            out[m.group(1)] = f
    return out


@cache
def editor_trees():
    """Shader name -> type tree of the editor's unity_builtin_extra (stored snippets)."""
    path = paths.EDITOR_DATA / 'Resources' / 'unity_builtin_extra'
    out = {}
    for o in UnityPy.load(str(path)).objects:
        if o.type.name == 'Shader':
            t = o.read_typetree()
            out[t['m_ParsedForm']['m_Name']] = t
    return out


def jobs_for(shader, path):
    """(label, record, compile args, parsed subprogram, pass names) per subprogram, or a skip reason."""
    form = shader['tree']['m_ParsedForm']
    text = path.read_text(encoding='utf-8', newline='') if path else None
    progs = dx11.pass_programs(form, text, path.parent if path else None, editor_trees().get(form['m_Name']))
    records = parse_blob(shader['raw'])
    out = []
    for si, sub in enumerate(form['m_SubShaders']):
        for pi, ps in enumerate(sub['m_Passes']):
            names = dict(ps['m_NameIndices'])
            for stage_name, stage in (('progVertex', 'vp'), ('progFragment', 'fp')):
                for sp in ps[stage_name]['m_SubPrograms']:
                    rec = records[sp['m_BlobIndex']]
                    label = f'{form["m_Name"]} {si}.{pi} {stage} {" ".join(rec["keywords"]) or "-"}'
                    p = progs.get((si, pi))
                    if p is None:
                        return f'no program for pass {si}.{pi}'
                    entry = p.vertex if stage == 'vp' else p.fragment
                    args = (p.text, p.shader_dir, stage, entry, p.target, rec['keywords'])
                    out.append((label, rec, args, sp, names))
    return out


def compare(job):
    label, rec, args, sp, names = job
    result, log = dx11.compile_program(*args, project=PC_PROJECT)
    if result is None:
        return label, ['compile failed: ' + log.strip().splitlines()[0] if log.strip() else 'compile failed']
    problems = []
    if result.gpu_type != rec['type']:
        problems.append(f'gpu type {result.gpu_type} != {rec["type"]}')
    if result.code != rec['code']:
        mine, theirs = dx11.chunks(result.code[6:]), dx11.chunks(rec['code'][6:])
        if result.code[:6] != rec['code'][:6]:
            problems.append(f'header {result.code[:6].hex()} != {rec["code"][:6].hex()}')
        differ = [k for k in sorted(set(mine) | set(theirs)) if mine.get(k) != theirs.get(k)]
        problems.append(f'code differs in {differ}' if differ else 'code differs (container)')
    if result.tail != rec['tail']:
        problems.append('tail differs')
    lists = dx11.unity_lists(result.bindings, names)
    for key, value in lists.items():
        theirs = sp[key]
        if key == 'm_Channels' and args[2] == 'fp':
            continue
        if value != theirs:
            problems.append(f'{key} differs')
    return label, problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--only', help='comma-separated shader names')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()
    only = set(args.only.split(',')) if args.only else None
    src = sources()
    jobs, skipped = [], collections.Counter()
    for shader in reference_shaders():
        name = shader['tree']['m_ParsedForm']['m_Name']
        if only and name not in only:
            continue
        if name not in src and name not in editor_trees():
            skipped[f'{name}: no source'] += 1
            continue
        found = jobs_for(shader, src.get(name))
        if isinstance(found, str):
            skipped[f'{name}: {found}'] += 1
            continue
        jobs += found
    bad = collections.Counter()
    ok = 0
    with ThreadPoolExecutor(16) as pool:
        for label, problems in pool.map(compare, jobs):
            if problems:
                for p in problems:
                    bad[p] += 1
                if args.verbose or sum(bad.values()) <= 40:
                    print('DIFF', label, '; '.join(problems))
            else:
                ok += 1
    for reason in sorted(skipped):
        print('skipped', reason)
    print('problems', dict(bad.most_common()))
    print(f'{ok}/{len(jobs)} subprograms identical')
    return 0 if ok == len(jobs) else 1


if __name__ == '__main__':
    sys.exit(main())
