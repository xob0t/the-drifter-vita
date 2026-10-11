"""Compute every type table hash of the reference build from scratch and compare.

    uv run tools/builder/check_typehash.py

Built-in classes: OldTypeHash from the TPK release tree. Scripts: ScriptID from the MonoScript's
names, OldTypeHash from the script's field model (typemodel.py) over the editor base.
"""

import collections
import contextlib
import sys

import paths
import reference
import typehash
import typemodel
import UnityPy

MANAGED = paths.PROJECT / 'Library' / 'PlayerDataCache' / 'PSP2' / 'Data' / 'Managed'


def full_name(script):
    return f'{script["m_Namespace"]}.{script["m_ClassName"]}' if script['m_Namespace'] else script['m_ClassName']


def main():
    env = UnityPy.Environment()
    for path in reference.media_files():
        with contextlib.suppress(Exception):
            env.load_file(str(path))
    gga = env.files[str(paths.REFERENCE_MEDIA / 'globalgamemanagers.assets')]
    scripts = {o.path_id: o.read_typetree() for o in gga.objects.values() if o.type.name == 'MonoScript'}
    models = typemodel.models(MANAGED, ((s['m_AssemblyName'], full_name(s)) for s in scripts.values()))
    stats, bad, seen = collections.Counter(), [], set()
    for sf in env.files.values():
        for t in getattr(sf, 'types', []):
            if t.class_id != 114 or t.script_type_index < 0:
                if ('b', t.class_id) in seen:
                    continue
                seen.add(('b', t.class_id))
                ok = typehash.builtin_hash(t.class_id, sf.version) == t.old_type_hash
                stats[f'built-in hash {"ok" if ok else "differs"}'] += 1
                ok or bad.append(f'built-in class {t.class_id}')
                continue
            ref = sf.script_types[t.script_type_index]
            s = scripts[ref.local_identifier_in_file]
            if ('s', full_name(s), s['m_AssemblyName']) in seen:
                continue
            seen.add(('s', full_name(s), s['m_AssemblyName']))
            ok = typehash.script_id(s['m_ClassName'], s['m_Namespace'], s['m_AssemblyName']) == t.script_id
            stats[f'script ID {"ok" if ok else "differs"}'] += 1
            entry = models.get((s['m_AssemblyName'], full_name(s)), {})
            try:
                ok = typehash.script_hash(entry['model'], sf.version) == t.old_type_hash
            except (KeyError, ValueError) as error:
                stats['script hash: no model'] += 1
                bad.append(f'{full_name(s)}: no model ({entry.get("error") or error})')
                continue
            stats[f'script hash {"ok" if ok else "differs"}'] += 1
            ok or bad.append(f'script {full_name(s)}')
    for k, v in sorted(stats.items()):
        print(f'{k}: {v}')
    for b in bad:
        print('  ', b)


if __name__ == '__main__':
    sys.exit(main())
