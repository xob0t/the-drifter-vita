"""The game code: Unity's IL2CPP step of a PS Vita build, run without the editor.

    uv run tools/pure/il2cpp.py [--out dir] [--file-count N] [--force] [--compare [--reference player]]
    uv run tools/pure/il2cpp.py --list-unity-files

Unity's build turns the managed assemblies into Media/Modules/il2CppAssemblies.suprx with two of
its own tools, which run headless and need no license:

1. UnityLinker.exe strips the assemblies. Its roots are the assemblies whose classes the player can
   instantiate by name (every MonoBehaviour and ScriptableObject class, abstract and generic ones
   too, and every class with a [RuntimeInitializeOnLoadMethod]); those classes are preserved whole
   through a generated link.xml, next to Unity's per-module whitelists, the platform's
   native_link.xml and the project's own link.xml files.
2. il2cpp.exe converts what is left to C++ and writes global-metadata.dat and the class libraries'
   *-resources.dat; its PSP2 plugin compiles the C++ with psp2snc and links the module with psp2ld,
   together with ApplicationInfo.o, which Unity generates from the player settings (title ID, NP
   IDs, file count) and the player reads through the module's exports. MapFileParser turns the link
   map into SymbolMap.

The managed input is what Unity puts in Temp/StagingArea/Data/Managed: the project's plugin DLLs,
the engine modules of the PS Vita IL2CPP variation (UnityEngine.VideoModule there is a stub, unlike
the one under PSP2Player/Tools), the runtime variant of each extension the player ships, and the
class libraries of the unityjit profile those reference.

A build directory under work/pure/il2cpp keeps the staging area and psp2snc's object cache, so a
rebuild recompiles only C++ files whose content changed; when no input changed at all the previous
outputs are reused.

Against Unity's own build of the same project, global-metadata.dat, the *-resources.dat files,
pthread.suprx and platform_native_link.xml come out identical, and so do the compiled objects but
for psp2snc's names of file-local symbols, which hash the source file's path. The module itself is
not reproducible, not even between two of Unity's builds: the plugin hands psp2ld the objects in
the order they finish compiling, which moves code around (and, through --strip-duplicates, changes
its size by a few KB). --compare therefore checks the module's sections, imports, exports, symbol
names and the ApplicationInfo symbols rather than its bytes.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from bisect import bisect_right
from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path

import dnfile
import paths
import unityyaml

IL2CPP_BUILD = paths.EDITOR_DATA / 'il2cpp' / 'build'
UNITY_LINKER = IL2CPP_BUILD / 'UnityLinker.exe'
IL2CPP = IL2CPP_BUILD / 'il2cpp.exe'
MAP_FILE_PARSER = paths.EDITOR_DATA / 'Tools' / 'MapFileParser' / 'MapFileParser.exe'
IL2CPP_ROOT = paths.EDITOR_DATA / 'il2cpp' / 'il2cpp_root'  # how il2cpp finds its folder
LIBIL2CPP = paths.EDITOR_DATA / 'il2cpp' / 'libil2cpp'
PLUGIN = paths.PSP2_PLAYER / 'Tools' / 'PSP2Il2CppPlugin.dll'
PSP2_IL2CPP = paths.PSP2_PLAYER / 'Tools' / 'il2cpp'  # the plugin's libil2cpp, bdwgc and pthread
PTHREAD_STUB = paths.PSP2_PLAYER / 'Tools' / 'pthread_stub.a'
NATIVE_LINK = paths.PSP2_PLAYER / 'Tools' / 'native_link.xml'
WHITELISTS = paths.PSP2_PLAYER / 'Whitelists'
ENGINE = paths.PSP2_PLAYER / 'Variations' / 'il2cpp' / 'Managed'
PTHREAD = paths.PSP2_PLAYER / 'Media' / 'Modules' / 'pthread.suprx'
CLASS_LIBRARIES = paths.EDITOR_DATA / 'MonoBleedingEdge' / 'lib' / 'mono' / 'unityjit'
EXTENSIONS = paths.EDITOR_DATA / 'UnityExtensions' / 'Unity'
# The extensions a PS Vita player ships (MonoManager lists them as type 8), in Unity's order.
PLAYER_EXTENSIONS = ['UnityEngine.UI', 'UnityEngine.Timeline', 'UnityEngine.Networking']
PSP2SNC = paths.SDK / 'host_tools' / 'build' / 'bin' / 'psp2snc.exe'
PSP2BIN = paths.SDK / 'host_tools' / 'build' / 'bin' / 'psp2bin.exe'
BUILD = paths.WORK / 'il2cpp'

SCRIPT_ROOTS = {'UnityEngine.MonoBehaviour', 'UnityEngine.ScriptableObject'}
RUNTIME_INITIALIZE = 'UnityEngine.RuntimeInitializeOnLoadMethodAttribute'
GENERICINST = 0x15
# Explicit il2cpp inputs besides the roots and UnityEngine.dll: Unity names every assembly of the
# stripped folder its class registry marks used, which for this player was the project's DLLs
# that MonoManager lists, mscorlib and the platform module.
ALWAYS_USED = {'mscorlib.dll', 'UnityEngine.PSVitaModule.dll'}
# Unity's defaults when the player settings leave a value empty.
DEFAULT_TITLE_ID = 'ABCD12345'
DEFAULT_COMMS_ID = 'DCBA54321_00'
# What the code step writes, relative to the output folder.
OUTPUTS = [
    'Media/Modules/il2CppAssemblies.suprx',
    'Media/Modules/pthread.suprx',
    'Media/Metadata/global-metadata.dat',
    'Media/Resources/mscorlib.dll-resources.dat',
    'Media/Resources/System.dll-resources.dat',
    'Media/Resources/System.Data.dll-resources.dat',
    'Media/SymbolMap',
    'Media/platform_native_link.xml',
    'SymbolFiles/il2CppAssemblies.suprx',
    'SymbolFiles/pthread.suprx',
]
CODE_FILES = {'Media/' + p for p in ('SymbolMap', 'platform_native_link.xml')}


def is_code_output(relative):
    """Whether a Media file is one the code step writes (so not counted in g_PSP2FileCount)."""
    return (
        relative in CODE_FILES
        or relative.startswith(('Media/Modules/', 'Media/Metadata/'))
        or relative.endswith('-resources.dat')
    )


def ordinal_ignore_case(path):
    return path.name.upper()


# --------------------------------------------------------------------------- assemblies


def table_name(index):
    return type(index.table).__name__


def compressed(data, pos):
    b = data[pos]
    if b & 0x80 == 0:
        return b, pos + 1
    if b & 0xC0 == 0x80:
        return ((b & 0x3F) << 8) | data[pos + 1], pos + 2
    return ((b & 0x1F) << 24) | (data[pos + 1] << 16) | (data[pos + 2] << 8) | data[pos + 3], pos + 4


def joined(ns, name):
    return f'{ns}.{name}' if ns else name


@dataclass
class Type:
    name: str  # full name as link.xml writes it: Namespace.Outer/Inner
    base: tuple | None  # (assembly name or None for this one, full name)
    runtime_initialize: bool = False


class Assembly:
    """An assembly's name and references, and (read on first use) its types."""

    def __init__(self, path: Path):
        self.path = path
        # Lazy: dnfile reads a table's rows when they're first used, which for mscorlib is the
        # difference between 0.3 and 4.5 seconds.
        t = self.tables = dnfile.dnPE(str(path), clr_lazy_load=True).net.mdtables
        self.name = str(t.Assembly[0].Name) if t.Assembly else path.stem
        self.references = [str(r.Name) for r in t.AssemblyRef or []]

    @cached_property
    def types(self):
        t = self.tables
        rows = list(t.TypeDef or [])
        outer = {n.NestedClass.row_index: n.EnclosingClass.row_index for n in t.NestedClass or []}

        def full(i):
            r = rows[i - 1]
            if i in outer:
                return f'{full(outer[i])}/{r.TypeName}'
            return joined(str(r.TypeNamespace or ''), str(r.TypeName))

        types = [Type(full(i), self.type_ref(r.Extends)) for i, r in enumerate(rows, 1)]
        # A type's methods run from its MethodList index to the next type's; reading the raw index
        # saves dnfile building every method row.
        starts = [r.struct.MethodList_Index for r in rows]
        for ca in t.CustomAttribute or []:
            if table_name(ca.Parent) != 'MethodDef' or not starts:
                continue
            ctor = ca.Type
            if (
                table_name(ctor) == 'MemberRef'
                and table_name(ctor.row.Class) == 'TypeRef'
                and self.typeref_name(ctor.row.Class.row)[1] == RUNTIME_INITIALIZE
            ):
                types[bisect_right(starts, ca.Parent.row_index) - 1].runtime_initialize = True
        return types

    def typeref_name(self, row):
        name = joined(str(row.TypeNamespace or ''), str(row.TypeName))
        scope = row.ResolutionScope
        kind = table_name(scope) if scope is not None and scope.row_index else None
        if kind == 'TypeRef':
            assembly, outer = self.typeref_name(scope.row)
            return assembly, f'{outer}/{name}'
        return (str(scope.row.Name) if kind == 'AssemblyRef' else None), name

    def type_ref(self, index):
        """(assembly name or None, full name) of a TypeDefOrRef coded index; a generic instance
        names its generic type."""
        if index is None or not index.row_index:
            return None
        kind = table_name(index)
        if kind == 'TypeDef':
            r = index.row
            return None, joined(str(r.TypeNamespace or ''), str(r.TypeName))
        if kind == 'TypeRef':
            return self.typeref_name(index.row)
        sig = bytes(getattr(index.row.Signature, 'value', index.row.Signature) or b'')
        if len(sig) < 3 or sig[0] != GENERICINST:
            return None
        coded, _ = compressed(sig, 2)
        tag, row = coded & 3, coded >> 2
        if tag == 0:
            r = self.tables.TypeDef[row - 1]
            return None, joined(str(r.TypeNamespace or ''), str(r.TypeName))
        if tag == 1:
            return self.typeref_name(self.tables.TypeRef[row - 1])
        return None


@cache
def load(path: Path) -> Assembly:
    return Assembly(path)


class Universe:
    """Assemblies resolving each other's base classes by full name (UnityEngine.dll only forwards
    to the modules, so a reference through it resolves by name alone)."""

    def __init__(self, assemblies):
        self.types = {}
        for a in assemblies:
            for ty in a.types:
                self.types.setdefault((a.name, ty.name), (a, ty))
                self.types.setdefault((None, ty.name), (a, ty))

    def is_script(self, assembly, ty):
        for _ in range(64):
            if ty.base is None:
                return False
            if ty.base[1] in SCRIPT_ROOTS:
                return True
            owner, full = ty.base
            found = self.types.get((owner or assembly.name, full)) or self.types.get((None, full))
            if found is None:
                return False
            assembly, ty = found
        return False


# --------------------------------------------------------------------------- inputs


def plugin_enabled(meta):
    """A project DLL goes in the player unless its importer leaves it to the editor or turns PSP2 off."""
    any_on, vita = True, None
    for entry in meta.get('PluginImporter', {}).get('platformData') or []:
        first = entry.get('first')
        if not isinstance(first, dict):
            continue
        ((name, key),) = first.items()
        enabled = str((entry.get('second') or {}).get('enabled', '0')) == '1'
        if name == 'Any':
            any_on = enabled
        elif 'PSP2' in (name, key):
            vita = enabled
    return vita if vita is not None else any_on


def project_plugins(project):
    out = []
    for meta in sorted((project / 'Assets').rglob('*.dll.meta')):
        if '/Editor/' in meta.as_posix():
            continue
        if plugin_enabled(unityyaml.meta(meta)):
            out.append(meta.with_suffix(''))
    return sorted(out, key=ordinal_ignore_case)


def extension_dlls():
    """The runtime variant of each player extension: the one built without UnityEditor."""
    out = []
    for name in PLAYER_EXTENSIONS:
        for path in sorted(EXTENSIONS.rglob(f'{name}.dll')):
            if 'Editor' not in path.parent.name and 'UnityEditor' not in load(path).references:
                out.append(path)
                break
        else:
            raise FileNotFoundError(f'no runtime {name}.dll under {EXTENSIONS}')
    return out


@dataclass
class Inputs:
    project: Path
    plugins: list  # project DLLs
    extensions: list  # Unity's extension DLLs
    engine: list  # UnityEngine.dll and the modules
    libraries: list  # class libraries

    @cached_property
    def assemblies(self):
        return {p.name: load(p) for p in self.plugins + self.extensions + self.engine + self.libraries}

    def files(self):
        """name -> source of every file of the Managed folder, the engine's .mdb files included."""
        out = {p.name: p for p in self.plugins + self.extensions + self.engine + self.libraries}
        for p in self.engine:
            mdb = p.with_name(p.name + '.mdb')
            if mdb.exists():
                out[mdb.name] = mdb
        return out

    def roots(self, saved: Path | None = None):
        """The assemblies with classes the player creates by name, in Unity's order (extensions
        first, then the project's DLLs), with those classes. Reading the types takes a while, so
        the answer is kept in `saved` for the same assemblies."""
        # A script's base classes are in the project, the extensions and the engine; the class
        # libraries (whose types take long to read) hold none.
        sources = self.plugins + self.extensions + self.engine
        # This file is part of the key, so a change to how roots are found invalidates it.
        key = digest(
            Path(__file__).read_bytes(), *(f'{p}:{hashlib.sha256(p.read_bytes()).hexdigest()}' for p in sources)
        )
        if saved and saved.exists():
            data = json.loads(saved.read_text(encoding='utf-8'))
            if data['key'] == key:
                return [(self.assemblies[name], types) for name, types in data['roots']]
        out = self.read_roots(sources)
        if saved:
            saved.parent.mkdir(parents=True, exist_ok=True)
            roots = [(a.path.name, types) for a, types in out]
            saved.write_text(json.dumps({'key': key, 'roots': roots}), encoding='utf-8')
        return out

    def read_roots(self, sources):
        u = Universe(self.assemblies[p.name] for p in sources)
        project_refs = set()
        for p in self.plugins:
            project_refs |= set(self.assemblies[p.name].references)
        out = []
        for p in self.extensions + self.plugins:
            a = self.assemblies[p.name]
            if p in self.extensions and a.name not in project_refs:
                continue
            types = [ty.name for ty in a.types if ty.runtime_initialize or u.is_script(a, ty)]
            if types:
                out.append((a, types))
        return out


def gather(project=paths.PROJECT):
    plugins = project_plugins(project)
    extensions = extension_dlls()
    engine = sorted(ENGINE.glob('*.dll'), key=ordinal_ignore_case)
    known = {p.stem for p in plugins + extensions + engine}
    libraries, todo = {}, [load(p) for p in plugins + extensions + engine]
    while todo:
        for ref in todo.pop().references:
            path = CLASS_LIBRARIES / f'{ref}.dll'
            if ref in known or ref in libraries or not path.exists():
                continue
            libraries[ref] = path
            todo.append(load(path))
    unresolved = set()
    for p in [*plugins, *extensions, *engine, *libraries.values()]:
        unresolved |= set(load(p).references) - known - set(libraries) - {'UnityEditor'}
    if unresolved:
        raise FileNotFoundError(f'assemblies referenced but not found: {sorted(unresolved)}')
    return Inputs(project, plugins, extensions, engine, sorted(libraries.values(), key=ordinal_ignore_case))


# --------------------------------------------------------------------------- link.xml


def types_xml(roots):
    lines = ['<linker>']
    for a, types in roots:
        lines.append(f'<assembly fullname="{a.name}">')
        lines += [f'<type fullname="{t}" preserve="all"/>' for t in types]
        lines.append('</assembly>')
    lines.append('</linker>')
    return '\r\n'.join(lines) + '\r\n'


def methods_xml(project):
    """Methods UnityEvents call by name. The game has none; rather than guess Unity's format for
    them, a project that has some stops the build."""
    found = []
    for path in (project / 'Assets').rglob('*'):
        if path.suffix not in ('.unity', '.prefab', '.asset'):
            continue
        data = path.read_bytes()
        if b'm_MethodName: ' in data:
            found += [(path.name, m.decode()) for m in re.findall(rb'm_MethodName: (\S+)', data)]
    if found:
        raise NotImplementedError(f'UnityEvent persistent calls are not supported yet: {found[:5]}')
    return '<linker>\r\n</linker>\r\n'


# Engine classes the player only creates from native code: none to keep beyond the whitelists.
MODULES_XML = '<linker><assembly fullname="UnityEngine" preserve="nothing"/></linker>'


def project_link_xmls(project):
    return sorted((project / 'Assets').rglob('link.xml'))


# --------------------------------------------------------------------------- ApplicationInfo


def hex_bytes(text, count, what):
    values = [int(v, 16) for v in re.findall(r'0x([0-9a-fA-F]{1,2})', text or '')]
    if values and len(values) != count:
        raise ValueError(f'{what}: {len(values)} values, expected {count}')
    return values or [0] * count


def application_info(project, file_count):
    """ApplicationInfo.cpp as Unity writes it from the PS Vita player settings."""
    s = unityyaml.meta(project / 'ProjectSettings' / 'ProjectSettings.asset')['PlayerSettings']
    content_id = str(s.get('psp2ContentID') or '')
    title_id = content_id[7:16] if len(content_id) >= 16 else DEFAULT_TITLE_ID
    service_id = content_id[:19] if len(content_id) >= 19 else f'IV0000-{title_id}_00'
    comms = str(s.get('psp2NPCommsID') or '') or DEFAULT_COMMS_ID
    if not re.fullmatch(r'[A-Z0-9]{9}_\d\d', comms):
        raise ValueError(f'NP communications ID {comms!r} is not like ABCD12345_00')
    passphrase = hex_bytes(s.get('psp2NPCommsPassphrase'), 128, 'NP pass phrase')
    signature = hex_bytes(s.get('psp2NPCommsSig'), 160, 'NP signature')
    trophies = bool(str(s.get('psp2NPTrophyPackPath') or '').strip())

    def array(values):
        return ', '.join(f'0x{v:02x}' for v in values)

    chars = ','.join(f"'{c}'" for c in comms[:9])
    return '\n'.join([
        '#include <stdio.h>',
        '#include <np.h>',
        '#define STORAGE_CLASS  __declspec(dllexport)',
        f"STORAGE_CLASS SceNpCommunicationId g_PSP2TitleNPCommsId = {{{{ {chars} }},'\\0',{int(comms[10:])}, 0}};",
        f'STORAGE_CLASS SceNpCommunicationPassphrase g_PSP2TitleNPCommsPassphrase = {{{array(passphrase)}}};',
        f'STORAGE_CLASS SceNpCommunicationSignature g_PSP2TitleNPCommsSig = {{{array(signature)}}};',
        f'STORAGE_CLASS bool g_PSP2TitleNPHasTrophyPack = {"true" if trophies else "false"};',
        f'STORAGE_CLASS int g_PSP2TitleNPAgeRating = {int(s.get("psp2NPAgeRating") or 0)};',
        f'STORAGE_CLASS const char* g_PSP2TitleNPServiceId = "{service_id}";',
        f'STORAGE_CLASS const char* g_PSP2TitleId = "{title_id}";',
        'STORAGE_CLASS bool g_PSP2TitleTrial = false;',
        'STORAGE_CLASS bool g_PSP2HasPSArc = false;',
        f'STORAGE_CLASS int g_PSP2FileCount = {file_count};',
        '',
    ])  # fmt: skip


def data_file_count(out):
    """Files of Media the data build writes (the levels, assets, resources, unity default resources)."""
    media = out / 'Media'
    if not media.exists():
        return 0
    return sum(1 for p in media.rglob('*') if p.is_file() and not is_code_output(p.relative_to(out).as_posix()))


def file_count(out, inputs):
    """g_PSP2FileCount: Unity counts the files of Temp/StagingArea/Data while it writes
    Data/MiscCPP/ApplicationInfo.cpp, so the data files, Managed and that file itself (the platform
    link.xml is copied there later)."""
    return data_file_count(out) + len(inputs.files()) + 1


# --------------------------------------------------------------------------- running the tools


def tool_env():
    env = dict(os.environ)
    env['SCE_PSP2_SDK_DIR'] = str(paths.SDK)
    return env


def run(command, log, cwd=None):
    """Run a tool, its output to a log file; on failure show the log's tail."""
    start = time.time()
    with log.open('w', encoding='utf-8', errors='replace') as f:
        f.write(subprocess.list2cmdline([str(c) for c in command]) + '\n\n')
        f.flush()
        result = subprocess.run(
            [str(c) for c in command], cwd=cwd, env=tool_env(), stdout=f, stderr=subprocess.STDOUT, check=False
        )
    if result.returncode:
        tail = log.read_text(encoding='utf-8', errors='replace').splitlines()[-30:]
        raise RuntimeError(f'{Path(command[0]).name} failed ({result.returncode}), see {log}:\n' + '\n'.join(tail))
    return time.time() - start


def digest(*parts):
    h = hashlib.sha256()
    for part in parts:
        h.update(part if isinstance(part, bytes) else str(part).encode())
        h.update(b'\0')
    return h.hexdigest()


def build(out=paths.OUT, project=paths.PROJECT, count=None, force=False, work=BUILD):
    """Build the code module and copy what the code step writes into `out`. Returns a dict of timings."""
    times = {}
    start = time.time()
    inputs = gather(project)
    if count is None:
        if not data_file_count(out):
            print('warning: no data files under the output folder yet, so g_PSP2FileCount is too low; '
                  'build the data first or pass --file-count')  # fmt: skip
        count = file_count(out, inputs)
    roots = inputs.roots(work / 'roots.json')
    xmls = {
        'methods.xml': methods_xml(project),
        'modules.xml': MODULES_XML,
        'types.xml': types_xml(roots),
    }
    appinfo = application_info(project, count)
    times['inputs'] = time.time() - start

    files = inputs.files()
    stamp = digest(
        *(f'{n}:{hashlib.sha256(p.read_bytes()).hexdigest()}' for n, p in sorted(files.items())),
        *(xmls[n] for n in sorted(xmls)),
        *(p.read_bytes() for p in project_link_xmls(project)),
        appinfo,
        UNITY_LINKER.stat().st_size,
        IL2CPP.stat().st_size,
    )
    staging = work / 'StagingArea'
    data, managed, native = staging / 'Data', staging / 'Data' / 'Managed', staging / 'Data' / 'Native'
    generated = staging / 'il2cppOutput'
    module = native / 'Il2CppAssemblies.suprx'
    stamp_file = work / 'stamp.txt'
    fresh = (
        not force
        and stamp_file.exists()
        and stamp_file.read_text() == stamp
        and module.exists()
        and (generated / 'Data' / 'Metadata' / 'global-metadata.dat').exists()
    )
    if not fresh:
        stamp_file.unlink(missing_ok=True)
        for d in (data, generated):
            if d.exists():
                shutil.rmtree(d)
        managed.mkdir(parents=True)
        native.mkdir(parents=True)
        for name, src in files.items():
            shutil.copy2(src, managed / name)
        shutil.copy2(NATIVE_LINK, data / 'platform_native_link.xml')
        xml_dir = work / 'xml'
        xml_dir.mkdir(parents=True, exist_ok=True)
        for name, text in xmls.items():
            (xml_dir / name).write_bytes(text.encode('utf-8'))

        times['unitylinker'] = run(linker_command(inputs, roots, managed, data, xml_dir), work / 'unitylinker.log')
        # As Unity does: the stripped DLLs replace the originals (the .mdb files stay).
        strip = managed / 'tempStrip'
        for p in managed.glob('*.dll'):
            p.unlink()
        for p in strip.iterdir():
            shutil.copy2(p, managed / p.name)
        shutil.rmtree(strip)

        (work / 'ApplicationInfo.cpp').write_text(appinfo, encoding='utf-8')
        snc = [PSP2SNC, '-DNDEBUG', '-DSN_TARGET_PSP2=1', '-O3', '-Xc-=exceptions', '-Xc-=rtti', '-Xthumb=0']
        snc += ['-Xblxcall=1', '-c', 'ApplicationInfo.cpp', '-o', 'ApplicationInfo.o']
        times['appinfo'] = run(snc, work / 'appinfo.log', cwd=work)

        command = il2cpp_command(inputs, roots, managed, module, generated, work / 'ApplicationInfo.o', work / 'cache')
        times['il2cpp'] = run(command, work / 'il2cpp.log')
        stamp_file.write_text(stamp)

    sources = {
        'Media/Modules/il2CppAssemblies.suprx': module,
        'Media/Modules/pthread.suprx': PTHREAD,
        'Media/Metadata/global-metadata.dat': generated / 'Data' / 'Metadata' / 'global-metadata.dat',
        'Media/SymbolMap': generated / 'Data' / 'SymbolMap',
        'Media/platform_native_link.xml': data / 'platform_native_link.xml',
        'SymbolFiles/il2CppAssemblies.suprx': module,
        'SymbolFiles/pthread.suprx': PTHREAD,
    }
    for p in sorted((generated / 'Data' / 'Resources').glob('*-resources.dat')):
        sources[f'Media/Resources/{p.name}'] = p
    for rel, src in sources.items():
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    times['total'] = time.time() - start
    times['reused'] = fresh
    times['file_count'] = count
    return times


def linker_command(inputs, roots, managed, data, xml_dir):
    whitelists = sorted(WHITELISTS.glob('*.xml'), key=ordinal_ignore_case)
    command = [UNITY_LINKER, f'-out={managed / "tempStrip"}', f'-x={WHITELISTS / "Core.xml"}']
    for x in [data / 'platform_native_link.xml', *(xml_dir / n for n in ('methods.xml', 'modules.xml', 'types.xml'))]:
        command += ['-x', x]
    for x in [*whitelists, *project_link_xmls(inputs.project)]:
        command += ['-x', x]
    command += ['-d', managed]
    for a, _ in roots:
        command += ['-a', managed / a.path.name]
    return [*command, '--dotnetruntime=il2cpp', '--dotnetprofile=unityjit', '--use-editor-options']


def il2cpp_command(inputs, roots, managed, module, generated, appinfo_o, cache):
    """il2cpp's arguments as Unity passes them for a release PS Vita build."""
    explicit = [managed / a.path.name for a, _ in roots] + [managed / 'UnityEngine.dll']
    used = {p.name for p in inputs.plugins} | ALWAYS_USED
    for p in sorted(managed.glob('*.dll'), key=ordinal_ignore_case):
        if p.name in used and p not in explicit:
            explicit.append(p)
    return [
        IL2CPP,
        '--convert-to-cpp',
        '--dotnetprofile=unityjit',
        '--compile-cpp',
        '--libil2cpp-static',
        '--platform=PSP2',
        '--architecture=ARMv7',
        '--configuration=Release',
        f'--outputpath={module}',
        f'--cachedirectory={cache}',
        '--compiler-flags=-O3',
        f'--linker-flags=--strip-unused-data --strip-duplicates {appinfo_o}',
        f'--plugin={PLUGIN}',
        f'--map-file-parser={MAP_FILE_PARSER}',
        '--emit-null-checks',
        '--enable-array-bounds-check',
        *(f'--assembly={p}' for p in explicit),
        f'--generatedcppdir={generated}',
    ]


def unity_files(inputs=None):
    """The files of Unity's installation the build reads, to take just those from the installers:
    UnityLinker with its descriptors, il2cpp and MapFileParser, the libil2cpp sources il2cpp compiles (with the
    libraries they include), the PS Vita plugin with its platform layer, Boehm GC and pthread, the
    link.xml whitelists, the managed inputs and Mono's etc folder."""
    inputs = inputs or gather()
    il2cpp = IL2CPP_ROOT.parent
    trees = [IL2CPP_BUILD, il2cpp / 'LinkerDescriptors', LIBIL2CPP, il2cpp / 'external', MAP_FILE_PARSER.parent]
    trees += [PSP2_IL2CPP, CLASS_LIBRARIES.parents[2] / 'etc']  # il2cpp copies Mono's etc folder
    files = [IL2CPP_ROOT, PLUGIN, PTHREAD_STUB, NATIVE_LINK, PTHREAD, *WHITELISTS.glob('*.xml')]
    files += [p for p in inputs.files().values() if paths.EDITOR_DATA in p.parents]
    files += [p for tree in trees for p in tree.rglob('*') if p.is_file()]
    return sorted(set(files))


# --------------------------------------------------------------------------- comparison


def psp2bin(path, *options):
    result = subprocess.run(
        [str(PSP2BIN), str(path), *options], capture_output=True, text=True, errors='replace', check=False
    )
    return result.stdout


def module_facts(path):
    sections = []
    for line in psp2bin(path, '-dsh').splitlines():
        m = re.match(r'\d+\s+(\S+)\s+\(?(0x[0-9A-F]+)\)?\s+(\S+)', line)
        if m:
            sections.append((m[1], int(m[2], 16), m[3]))
    summary = psp2bin(path, '-ms').splitlines()
    imports, exports, head = [], [], []
    part = None
    for line in summary:
        if line.startswith('---'):
            part = line.strip('- ')
            continue
        if part == 'Summary' and line.strip() and not line.startswith('Debug Fingerprint'):
            head.append(line.strip())
        elif part == 'Imports' and line.strip() and not line.startswith('Library'):
            lib, kind, _, name = line.split(None, 3)
            imports.append((lib, kind, name))
        elif part and part.startswith('Exports') and line.strip() and not line.startswith('Library'):
            exports.append(line.split()[-1])
    symbols = {}
    for line in psp2bin(path, '-dsy', '-c').splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].startswith('0x') and not parts[3].startswith('.'):
            symbols[parts[3]] = (parts[1], int(parts[2], 16))
    return {'size': path.stat().st_size, 'sections': sections, 'head': head, 'imports': imports,
            'exports': exports, 'symbols': symbols}  # fmt: skip


def compare_bytes(ours, ref):
    a, b = ours.read_bytes(), ref.read_bytes()
    if a == b:
        return 'identical'
    first = next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
    return f'differs from byte {first} (ours {len(a)}, reference {len(b)})'


def compare(out=paths.OUT, reference=paths.REFERENCE):
    lines = []
    for rel in OUTPUTS:
        if rel.endswith('il2CppAssemblies.suprx'):
            continue
        ours, ref = out / rel, reference / rel
        lines.append(f'{rel}: ' + (compare_bytes(ours, ref) if ours.exists() and ref.exists() else 'missing'))
    rel = 'Media/Modules/il2CppAssemblies.suprx'
    ours, ref = out / rel, reference / rel
    lines.append(f'{rel}: {compare_bytes(ours, ref)}')
    a, b = module_facts(ours), module_facts(ref)
    lines.append(f'  size: ours {a["size"]:,}, reference {b["size"]:,} ({a["size"] / b["size"] - 1:+.2%})')
    names = [s[0] for s in a['sections']], [s[0] for s in b['sections']]
    lines.append(f'  sections: {"same names and order" if names[0] == names[1] else "DIFFERENT names"}')
    for (n, size, _), (_, ref_size, _) in zip(a['sections'], b['sections'], strict=False):
        if size != ref_size and size + ref_size > 0:
            lines.append(f'    {n}: {size:#x} vs {ref_size:#x} ({size / max(ref_size, 1) - 1:+.2%})')
    head = [(x, y) for x, y in zip(a['head'], b['head'], strict=False) if x != y]
    lines.append('  summary: ' + ('same' if not head else '; '.join(f'{x} (reference {y})' for x, y in head)))
    lines.append(f'  imports: {"identical" if a["imports"] == b["imports"] else "DIFFERENT"} ({len(b["imports"])})')
    # Unnamed exports show as their addresses, which move with the link order.
    named = [sorted(n for n in f['exports'] if not n.startswith('<')) for f in (a, b)]
    same = named[0] == named[1] and len(a['exports']) == len(b['exports'])
    lines.append(f'  exports: {"same names" if same else "DIFFERENT"} ({len(b["exports"])})')
    sa, sb = set(a['symbols']), set(b['symbols'])
    common = len(sa & sb)
    lines.append(
        f'  symbol names: ours {len(sa)}, reference {len(sb)}, common {common} ({common / max(len(sb), 1):.2%}), '
        f'only ours {len(sa - sb)}, only reference {len(sb - sa)}'
    )
    app = sorted(n for n in sb if n.startswith('g_PSP2'))
    same = all(a['symbols'].get(n) == b['symbols'][n] for n in app)
    lines.append(f'  application info symbols ({len(app)}): {"same kinds and sizes" if same else "DIFFERENT"}')
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, default=paths.OUT)
    parser.add_argument(
        '--file-count', type=int, help="g_PSP2FileCount (default: the output's data files + Managed + 1)"
    )
    parser.add_argument('--force', action='store_true', help='rerun the tools even if no input changed')
    parser.add_argument('--compare', action='store_true', help='compare the outputs with a Unity-built player')
    parser.add_argument('--reference', type=Path, default=paths.REFERENCE, help='the player --compare reads')
    parser.add_argument('--list-unity-files', action='store_true', help='print the Unity files the build reads')
    args = parser.parse_args()
    if args.list_unity_files:
        for p in unity_files():
            print(p.relative_to(paths.EDITOR_DATA).as_posix() + ('/' if p.is_dir() else ''))
        return 0
    times = build(args.out, count=args.file_count, force=args.force)
    print(', '.join(f'{k} {v:.1f} s' if isinstance(v, float) else f'{k} {v}' for k, v in times.items()))
    if args.compare:
        print('\n'.join(compare(args.out, args.reference)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
