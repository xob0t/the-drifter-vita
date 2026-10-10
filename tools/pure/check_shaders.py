"""Rebuild every shader program of the reference build from source and compare.

    uv run --with lz4 tools/pure/check_shaders.py

For each subprogram: compile it with psp2cgc, derive Unity's binding data from the GXP and the
SDB, and compare the code bytes (GXP + header + uniform names; the source GUID aside), the blob
record's binding tail, and the parsed form's parameter lists with the reference build.
"""

import collections
import struct
import sys
from concurrent.futures import ThreadPoolExecutor

import paths
import psp2cgc
import reference
import shader_bindings as bindings
from shaderlab import find_shaders, pragmas, scan, unity_input


def program_args(path, text, programs, includes, record):
    p = programs[(record['sub'], record['passi'])]
    found = dict(pragmas(''.join(i.body for i in includes if i.lang == p.lang) + p.body))
    stage = 'vp' if record['stage'] == 'progVertex' else 'fp'
    entry = found['vertex' if stage == 'vp' else 'fragment'][0]
    return (
        unity_input(text, p, includes),
        str(path.parent),
        stage,
        entry,
        found.get('target', ['2.5'])[0],
        record['keywords'],
    )


def check(job):
    name, record, args = job
    result, log = psp2cgc.compile(*args, sdb=True)
    if result is None:
        return name, 'compile failed', log[-300:]
    gxp, sdb = result
    _a, gxp_length, _ = struct.unpack_from('<III', record['code'], 0)
    reference_gxp = record['code'][12 : 12 + gxp_length]
    gxp = gxp[:16] + reference_gxp[16:20] + gxp[20:]  # the source GUID: a hash of the input's path
    b = bindings.derive(gxp, bindings.attribute_semantics(sdb))
    if bindings.code_bytes(gxp, b) != record['code']:
        return name, 'code differs', ''
    if bindings.tail_bytes(b) != record['tail']:
        return name, 'binding tail differs', ''
    for key, value in bindings.unity_lists(b, record['names']).items():
        if record['sp'][key] != value:
            return name, f'{key} differs', ''
    return name, 'match', ''


def main():
    sources = find_shaders(paths.PROJECT)
    jobs, missing = [], []
    for shader in reference.shaders():
        path = sources.get(shader['name'])
        if path is None:
            missing.append(shader['name'])
            continue
        text = path.read_text(encoding='utf-8', newline='')
        programs, includes = scan(text)
        programs = {(p.sub, p.passi): p for p in programs}
        jobs += [(shader['name'], r, program_args(path, text, programs, includes, r)) for r in shader['programs']]
    results = list(ThreadPoolExecutor(16).map(check, jobs))
    counts = collections.Counter(r for _, r, _ in results)
    print(f'{len(jobs)} subprograms: {dict(counts)}')
    if missing:
        print('no source in the project:', ', '.join(missing))
    for name, r, info in results:
        if r != 'match':
            print(f'  {name}: {r} {info}')
    return 0 if set(counts) == {'match'} else 1


if __name__ == '__main__':
    sys.exit(main())
