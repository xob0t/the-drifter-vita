"""globalgamemanagers, the MonoScripts of globalgamemanagers.assets, and each file's PreloadData.

globalgamemanagers holds one object per global manager at fixed path IDs (the manager's slot in
the player's manager table plus one; slots the PS Vita player has no manager for are skipped).
Most are ProjectSettings/*.asset converted by name; BuildSettings, MonoManager, ScriptMapper,
ResourceManager and RuntimeInitializeOnLoadManager are what the build computes.

MonoScripts: every top-level MonoBehaviour or ScriptableObject class (abstract and generic ones
too) of the project's DLLs and of the runtime DLLs of Unity's extensions, one per DLL, so the
extensions shipped in two variants (UnityEngine.UI and Standalone/UnityEngine.UI) give two each.
Unity orders them like any other asset's objects: by local fileID, the first four bytes of
MD4("s\\0\\0\\0" + namespace + name), with the namespace a [MovedFrom] attribute names, as a
signed int; ties (the variants) by DLL GUID. They come after the file's PreloadData, material
and shaders. A class whose fileID collides with an earlier one in its DLL gets none.

    entries = monoscripts(layout)                   # [MonoScript] in path ID order; .fields is the object
    ids = monoscript_ids(entries)                   # {(assembly, namespace, class): path ID} for AssetContext
    register(placement, entries)                    # placement.find resolves {fileID, DLL guid} to them
    objects = managers(layout, placement, entries)  # [(path ID, class ID, dict)] of globalgamemanagers

PreloadData is in preload.py, the object reference graph it and ResourceManager use in references.py.
"""

import functools
import re
import struct
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import clrmeta
import convert
import editor_settings
import scripts as scriptlib
import typehash
import typemodel
import unityyaml
from assets import REF
from layout import BUILTIN_EXTRA, build_scenes
from paths import EDITOR_DATA, PROJECT, TARGET, WORK
from placement import DEFAULT_RESOURCES, AssetContext, first_path_id

GGM = 'globalgamemanagers'
GGA = 'globalgamemanagers.assets'
DEFAULT_RESOURCES_FILE = 'library/unity default resources'
BUILTIN_EXTRA_FILE = 'resources/unity_builtin_extra'
PSP2_PLAYER = EDITOR_DATA / 'PlaybackEngines' / 'PSP2Player'
EXTENSIONS = EDITOR_DATA / 'UnityExtensions' / 'Unity'
ENGINE_MODULES = EDITOR_DATA / 'Managed' / 'UnityEngine'

# Path ID of each manager: its slot in the player's manager table + 1. Slot 15 is
# ClusterInputManager and 17.. UnityConnectSettings and friends, which the PS Vita player lacks.
PATH_IDS = {
    129: 1, 13: 2, 78: 3, 11: 4, 94: 5, 116: 6, 30: 7, 5: 8, 98: 9, 55: 10, 141: 11, 47: 12, 147: 13,
    126: 14, 19: 15, 300: 17, 1403656975: 23,
}  # fmt: skip
SETTINGS = {
    129: 'ProjectSettings.asset', 13: 'InputManager.asset', 78: 'TagManager.asset', 11: 'AudioManager.asset',
    30: 'GraphicsSettings.asset', 5: 'TimeManager.asset', 98: 'DelayedCallManager.asset',
    55: 'DynamicsManager.asset', 47: 'QualitySettings.asset', 126: 'NavMeshAreas.asset',
    19: 'Physics2DSettings.asset', 1403656975: 'StreamingManager.asset',
}  # fmt: skip
# Unity's extensions that ship in a PS Vita player (MonoManager assembly type 8), in Unity's order.
# The others (HoloLens, SpatialTracking, GoogleAudioSpatializer, TestRunner) target other platforms
# but their MonoScripts are still written.
PLAYER_EXTENSIONS = ['UnityEngine.UI.dll', 'UnityEngine.Timeline.dll', 'UnityEngine.Networking.dll']
ENGINE, EXTENSION, USER = 2, 8, 16
# The editor gives the EventSystem this order itself (no attribute in the DLL).
EXECUTION_ORDER = {('UnityEngine.EventSystems', 'EventSystem'): -1000}
# Built-in shaders the build adds to GraphicsSettings' Always Included Shaders after the project's
# own list, as many as unity_builtin_extra has.
REQUIRED_SHADERS = [19, 65, 66, 67, 109, 110, 107, 111, 9000, 9001, 9002, 9003, 9004, 9007, 9100, 9102, 9101, 68,
                    10757, 15309, 15308]  # fmt: skip
PSP2_GRAPHICS_APIS = [12]  # GraphicsDeviceType.PlayStationVita
MOVED_FROM = 'UnityEngine.Scripting.APIUpdating.MovedFromAttribute'
RUNTIME_INITIALIZE = 'UnityEngine.RuntimeInitializeOnLoadMethodAttribute'


# --------------------------------------------------------------------------- assemblies


@dataclass
class Dll:
    path: Path
    guid: str
    kind: int  # EXTENSION or USER


def plugin_enabled(meta):
    """A project DLL goes in the player unless its importer leaves it to the editor or excludes PSP2."""
    platforms = {}
    for entry in meta.get('PluginImporter', {}).get('platformData') or []:
        (key,) = entry['first'].values() if isinstance(entry.get('first'), dict) else ('',)
        name = next(iter(entry['first'])) if isinstance(entry.get('first'), dict) else ''
        platforms[(name, key)] = str(entry.get('second', {}).get('enabled', '0')) == '1'
    any_on = next((v for (n, _), v in platforms.items() if n == 'Any'), True)
    vita = next((v for (n, k), v in platforms.items() if k == 'PSP2' or n == 'PSP2'), None)
    return vita if vita is not None else any_on


@functools.cache
def dlls(project=PROJECT):
    """The DLLs whose classes get MonoScripts: the extensions' runtime DLLs, then the project's."""
    out = []
    for ivy in sorted(EXTENSIONS.glob('*/ivy.xml')):
        for name, guid in re.findall(r'<artifact name="([^"]+)" type="dll"[^>]*e:guid="(\w+)"', ivy.read_text()):
            path = ivy.parent / f'{name}.dll'
            if not name.startswith('Editor/') and path.exists():
                out.append(Dll(path, guid.lower(), EXTENSION))
    for meta in sorted((project / 'Assets').rglob('*.dll.meta')):
        if '/Editor/' in meta.as_posix():
            continue
        m = unityyaml.meta(meta)
        if plugin_enabled(m):
            out.append(Dll(meta.with_suffix(''), m['guid'], USER))
    return out


@functools.cache
def universe(project=PROJECT):
    paths = [d.path for d in dlls(project)] + sorted(ENGINE_MODULES.glob('*.dll'))
    return clrmeta.Universe(paths)


def assembly(path, project=PROJECT):
    return next(a for a in universe(project).assemblies if a.path == path)


# --------------------------------------------------------------------------- MonoScripts


@dataclass
class MonoScript:
    path_id: int
    dll: Dll
    type: clrmeta.Type
    file_ids: set  # the fileIDs YAML can reference it by
    order_key: tuple
    fields: dict = field(default_factory=dict)
    aliases: list = field(default_factory=list)  # classes of the DLL with the same fileID

    @property
    def guid(self):
        return self.dll.guid

    @property
    def assembly_name(self):
        return self.dll.path.name


def execution_order(ty, dll):
    """The importer's order for the class ([DefaultExecutionOrder] acts at runtime and is not
    written here)."""
    if dll.kind == USER:
        meta = unityyaml.meta(Path(f'{dll.path}.meta'))
        orders = meta.get('PluginImporter', {}).get('executionOrder') or {}
        if ty.full_name in orders:
            return int(orders[ty.full_name])
    return EXECUTION_ORDER.get((ty.namespace, ty.name), 0)


def managed_folder(project=PROJECT):
    """Every assembly the field models can need in one folder: the project's, Unity's and the
    extensions' runtime DLLs (which scripts.Scripts.managed misses below the first level)."""
    folder = WORK / 'managers' / 'managed'
    folder.mkdir(parents=True, exist_ok=True)
    sources = list(scriptlib.Scripts(project).managed.glob('*.dll'))
    sources += [d.path for d in dlls(project) if d.kind == EXTENSION]
    for src in sources:
        dst = folder / src.name
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            dst.write_bytes(src.read_bytes())
    return folder


RECT_OFFSET = [
    {'n': n, 'node': {'k': 'prim', 't': 'int', 'size': 4}} for n in ('m_Left', 'm_Right', 'm_Top', 'm_Bottom')
]


def native_fields(node):
    """Corrections to the field model where Unity's serializer differs from it: RectOffset is a
    native class whose four ints Unity writes, PropertyName is written as its string, a generic
    struct's type name keeps its arity (ExposedReference`1), and lists of lists are not written."""
    if isinstance(node, dict):
        if node.get('k') == 'struct':
            full = node.get('full', '')
            if full == 'UnityEngine.RectOffset':
                node['fields'] = RECT_OFFSET
            elif full == 'UnityEngine.PropertyName':
                node.clear()
                node['k'] = 'string'
            elif '`' in full:
                node['t'] = full.rsplit('.', 1)[-1]
        if 'fields' in node:
            node['fields'] = [
                f
                for f in node['fields']
                if not (f.get('node', {}).get('k') == 'array' and f['node']['e']['k'] == 'array')
            ]
        for v in list(node.values()):
            native_fields(v)
    elif isinstance(node, list):
        for v in node:
            native_fields(v)
    return node


def monoscripts(layout, project=PROJECT):
    """Every MonoScript of globalgamemanagers.assets in path ID order, with its release fields."""
    u = universe(project)
    found = []
    for dll in dlls(project):
        seen = {}
        for ty in assembly(dll.path, project).types:
            if ty.nested or not u.is_script(ty):
                continue
            own = scriptlib.file_id(ty.namespace, ty.name)
            moved = clrmeta.attribute_string(ty.attribute(MOVED_FROM))
            fid = scriptlib.file_id(moved, ty.name) if moved is not None else own
            if fid in seen:
                seen[fid].aliases.append(ty)
                continue
            seen[fid] = MonoScript(0, dll, ty, {own, fid}, (fid, dll.guid))
            found.append(seen[fid])
    found.sort(key=lambda s: s.order_key)
    start = first_path_id(GGA) + len(layout.objects(GGA))
    concrete = [s for s in found if not (s.type.abstract or s.type.generic)]
    models = typemodel.models(managed_folder(project), [(s.assembly_name, s.type.full_name) for s in concrete])
    for i, s in enumerate(found):
        s.path_id = start + i
        digest = bytes(16)
        model = models.get((s.assembly_name, s.type.full_name), {}).get('model')
        if model is not None and not (s.type.abstract or s.type.generic):
            digest = typehash.script_hash(native_fields(model), convert.VERSION)
        s.fields = {
            'm_Name': s.type.name,
            'm_ExecutionOrder': execution_order(s.type, s.dll),
            'm_PropertiesHash': hash128(digest),
            'm_ClassName': s.type.name,
            'm_Namespace': s.type.namespace,
            'm_AssemblyName': s.assembly_name,
        }
    return found


def monoscript_ids(entries):
    """{(assembly, namespace, class): path ID} for placement.AssetContext. A class whose fileID
    collides with another's maps to that one's MonoScript; of an extension's two DLL variants the
    one listed first (the GUID the project references sorts first) wins."""
    out = {}
    for s in entries:
        for ty in [s.type, *s.aliases]:
            out.setdefault((s.assembly_name, ty.namespace, ty.name), s.path_id)
    return out


def register(placement, entries):
    """Make placement.find resolve m_Script references ({fileID, guid of the DLL}) to the
    MonoScripts."""
    for s in entries:
        for fid in s.file_ids:
            placement.where.setdefault((s.guid, fid), (GGA, s.path_id))


@functools.cache
def default_resources():
    """[(path ID, class ID, fields)] of the player's 'unity default resources'."""
    import UnityPy

    env = UnityPy.load(str(PSP2_PLAYER / 'Data' / 'Resources' / 'unity default resources'))
    out = []
    for pid, o in sorted(next(iter(env.files.values())).objects.items()):
        fields = o.read_typetree() if o.type.name in ('MonoScript', 'Shader') else {}
        out.append((pid, o.class_id, fields))
    return out


@functools.cache
def builtin_extra_shaders():
    """[(fileID, name, [dependency fileIDs])] of the editor's built-in extra shaders."""
    import UnityPy

    env = UnityPy.load(str(EDITOR_DATA / 'Resources' / 'unity_builtin_extra'))
    out = []
    for pid, o in sorted(next(iter(env.files.values())).objects.items()):
        if o.type.name == 'Shader':
            t = o.read_typetree()
            out.append((pid, t['m_ParsedForm']['m_Name'], [d['m_PathID'] for d in t['m_Dependencies']]))
    return out


# --------------------------------------------------------------------------- managers


def ptr(file_name, path_id):
    if not path_id:
        return {'m_FileID': 0, 'm_PathID': 0}
    return {'m_FileID': 0, 'm_PathID': path_id, 'file': file_name}


class Context(AssetContext):
    """References from ProjectSettings: built-in resources the build left in unity_builtin_extra
    stay there only if the player's copy has them."""

    def __init__(self, placement, extra):
        super().__init__(placement, GGM, None)
        self.extra = extra

    def pptr(self, ref, node):
        fid = int(ref.get('fileID', 0))
        guid = ref.get('guid')
        if fid and guid == BUILTIN_EXTRA and (guid, fid) not in self.placement.where and fid not in self.extra:
            return ptr('', 0)
        if fid and guid == DEFAULT_RESOURCES:
            return ptr(DEFAULT_RESOURCES_FILE, fid)
        return super().pptr(ref, node)


def settings(name, project=PROJECT):
    if name == 'GraphicsSettings.asset':  # as the editor holds them (editor_settings.py)
        return editor_settings.graphics_settings(project)
    (doc,) = unityyaml.load(project / 'ProjectSettings' / name)
    return doc.fields


class Managers:
    def __init__(self, layout, placement, entries, project=PROJECT):
        self.layout, self.placement, self.entries, self.project = layout, placement, entries, project

    @functools.cached_property
    def builtin_extra(self):
        """fileIDs of the built-in extra resources the player's unity_builtin_extra holds: what
        GraphicsSettings names (bar what the build copied elsewhere) and their dependencies."""
        text = editor_settings.graphics_settings_text(self.project)
        deps = {fid: d for fid, _, d in builtin_extra_shaders()}
        names = {fid for fid, g in ((int(f), g) for f, g in REF.findall(text)) if g == BUILTIN_EXTRA}
        names |= set(REQUIRED_SHADERS)
        out, stack = set(), [f for f in names if f in deps and f not in self.layout.builtin_file]
        while stack:
            f = stack.pop()
            if f not in out:
                out.add(f)
                stack.extend(d for d in deps.get(f, ()) if d not in self.layout.builtin_file)
        return out

    def ctx(self):
        return Context(self.placement, self.builtin_extra)

    def convert(self, class_id):
        d, _ = convert.convert(class_id, settings(SETTINGS[class_id], self.project), self.ctx())
        return d

    def all(self):
        out = []
        for cid, pid in sorted(PATH_IDS.items(), key=lambda x: x[1]):
            build = getattr(self, f'class_{cid}', None)
            out.append((pid, cid, build() if build else self.convert(cid)))
        return out

    # PlayerSettings, as the editor holds them when it builds (editor_settings.py)
    def class_129(self):
        d, _ = convert.convert(129, editor_settings.player_settings(self.project), self.ctx())
        return d

    # GraphicsSettings
    def class_30(self):
        fields = settings('GraphicsSettings.asset', self.project)
        d = self.convert(30)
        ctx = self.ctx()
        extra = [{'fileID': f, 'guid': BUILTIN_EXTRA} for f in REQUIRED_SHADERS if f in self.builtin_extra]
        d['m_AlwaysIncludedShaders'] = [ctx.pptr(r, None) for r in fields['m_AlwaysIncludedShaders'] + extra]
        # The player gets the tier settings of its own build target (the YAML lists the
        # Standalone ones; PS Vita has no entry, so the defaults apply).
        for tier in (1, 2, 3):
            d[f'm_TierSettings_Tier{tier}'] = {
                'renderingPath': 1,
                'hdrMode': 2,
                'realtimeGICPUUsage': 25,
                'useCascadedShadowMaps': False,
                'prefer32BitShadowMaps': False,
                'enableLPPV': False,
                'useHDR': False,
            }
        d['m_ShaderDefinesPerShaderCompiler'] = [
            {
                'shaderPlatform': 10,
                'defines_Tier1': [71438592],
                'defines_Tier2': [71438592],
                'defines_Tier3': [71438592],
            }
        ]
        return d

    # QualitySettings: the levels the PS Vita keeps, starting at its default level.
    def class_47(self):
        fields = settings('QualitySettings.asset', self.project)
        d = self.convert(47)
        keep = [
            i
            for i, q in enumerate(fields.get('m_QualitySettings') or [])
            if 'PSP2' not in (q.get('excludedTargetPlatforms') or [])
        ]
        d['m_QualitySettings'] = [d['m_QualitySettings'][i] for i in keep]
        for q in d['m_QualitySettings']:
            # 0 is no SkinWeights value; the editor reads it as OneBone
            q['blendWeights'] = q['blendWeights'] or 1
        default = int((fields.get('m_PerPlatformDefaultQuality') or {}).get('PSP2', d['m_CurrentQuality']))
        d['m_CurrentQuality'] = max(0, min(default, len(keep) - 1))
        return d

    # ScriptMapper
    def class_94(self):
        pairs = []
        for pid, cid, fields in default_resources():
            if cid == 48:
                pairs.append((ptr(DEFAULT_RESOURCES_FILE, pid), fields['m_ParsedForm']['m_Name']))
        for fid, name, _ in builtin_extra_shaders():
            if (BUILTIN_EXTRA, fid) in self.placement.where:
                pairs.append((ptr(*self.placement.where[(BUILTIN_EXTRA, fid)]), name))
            else:
                pairs.append((ptr(BUILTIN_EXTRA_FILE, fid) if fid in self.builtin_extra else ptr('', 0), name))
        for f, pid, name in self.project_shaders():
            pairs.append((ptr(f, pid), name))
        return {'m_Shaders': {'m_ObjectToName': pairs}, 'm_PreloadShaders': False}

    def project_shaders(self):
        out = []
        for (g, fid), (f, pid) in self.placement.where.items():
            a = self.layout.db.by_guid.get(g)
            if a is not None and a.classes.get(fid) == 48:
                text = (self.project / a.path).read_text(encoding='utf-8', errors='replace')
                m = re.search(r'^\s*Shader\s+"([^"]+)"', text, re.M)
                out.append((f, pid, m[1] if m else a.path))
        return sorted(out, key=lambda x: (file_rank(x[0]), x[1]))

    # MonoManager
    def class_116(self):
        scripts = [ptr(DEFAULT_RESOURCES_FILE, pid) for pid, cid, _ in default_resources() if cid == 115]
        by_dll = {}
        for s in self.entries:
            by_dll.setdefault(s.dll.path, []).append(s)
        for path in sorted(by_dll, key=lambda p: (p.name.lower(), str(p))):
            group = sorted(by_dll[path], key=lambda s: s.type.name.lower())
            scripts += [ptr(GGA, s.path_id) for s in group]
        names, kinds = self.assemblies()
        return {'m_Scripts': scripts, 'm_AssemblyNames': names, 'm_AssemblyTypes': kinds}

    def assemblies(self):
        modules = sorted(
            (p.name for p in (PSP2_PLAYER / 'Managed').glob('UnityEngine.*Module.dll')),
            key=lambda n: n[len('UnityEngine.') : -len('Module.dll')],
        )
        user = sorted({d.path.name for d in dlls(self.project) if d.kind == USER}, key=str.lower)
        names = ['UnityEngine.dll', *modules, *PLAYER_EXTENSIONS, *user]
        kinds = [ENGINE] * (1 + len(modules)) + [EXTENSION] * len(PLAYER_EXTENSIONS) + [USER] * len(user)
        return names, kinds

    # BuildSettings
    def class_141(self):
        scenes = [s['path'] for s in build_scenes(self.project)]
        build_guid = uuid.uuid5(uuid.NAMESPACE_URL, 'drifter-vita:' + '|'.join(scenes)).bytes
        script_hashes = {}
        for _, cid, fields in default_resources():
            if cid == 115:
                script_hashes[script_id(fields)] = bytes(fields['m_PropertiesHash'].values())
        for s in self.entries:
            script_hashes[script_id(s.fields)] = bytes(s.fields['m_PropertiesHash'].values())
        return {
            'scenes': scenes,
            'preloadedPlugins': [],
            'enabledVRDevices': [],
            'buildTags': [],
            'buildGUID': {f'data[{i}]': v for i, v in enumerate(struct.unpack('<4I', build_guid))},
            'hasPROVersion': True,
            'isNoWatermarkBuild': True,
            'isPrototypingBuild': True,
            'isEducationalBuild': True,
            'isEmbedded': False,
            'hasPublishingRights': False,
            'hasShadows': True,
            'hasSoftShadows': True,
            'hasLocalLightShadows': True,
            'hasAdvancedVersion': True,
            'enableDynamicBatching': False,
            'isDebugBuild': False,
            'usesOnMouseEvents': False,
            'hasClusterRendering': True,
            'm_Version': TARGET,
            'm_AuthToken': '',
            'runtimeClassHashes': [(cid, hash128(h)) for cid, h in runtime_classes()],
            # a std::map keyed by Hash128, which compares as two little-endian 64-bit halves
            'scriptHashes': [
                (hash128(k), hash128(script_hashes[k]))
                for k in sorted(script_hashes, key=lambda k: struct.unpack('<2Q', k))
            ],
            'm_GraphicsAPIs': PSP2_GRAPHICS_APIS,
        }

    # ResourceManager
    def class_147(self):
        container = []
        for (g, _), (f, pid) in self.placement.where.items():
            if f == 'resources.assets' and g in self.layout.resources:
                container.append((resource_key(self.layout.db.by_guid[g].path), ptr(f, pid)))
        container.sort(key=lambda x: (x[0], x[1]['m_PathID']))
        dependent = []
        for obj, refs in resource_dependencies(self.layout, self.placement):
            dependent.append({'m_Object': obj, 'm_Dependencies': refs})
        return {'m_Container': container, 'm_DependentAssets': dependent}

    # RuntimeInitializeOnLoadManager: the classes in type definition order. The editor's order is
    # its own (native; no name, token or hash order reproduces it). The set and the load types
    # match, and docs/builder.md says why the order of Compat's methods does not matter.
    def class_300(self):
        assemblies, namespaces, classes, methods = [], [], [], []
        before, after = [], []
        for dll in dlls(self.project):
            if dll.kind != USER:
                continue
            for ty in assembly(dll.path, self.project).types:
                found = [(m, v) for m in ty.methods for n, v in m.attributes if n == RUNTIME_INITIALIZE]
                if not found:
                    continue
                name = dll.path.stem
                if name not in assemblies:
                    assemblies.append(name)
                if ty.namespace not in namespaces:
                    namespaces.append(ty.namespace)
                classes.append({
                    'm_AssemblyNameIndex': assemblies.index(name),
                    'm_NamespaceIndex': namespaces.index(ty.namespace),
                    'm_ClassName': ty.name,
                    'm_NumOfMethods': len(found),
                    'm_MethodIndex': len(methods),
                    'm_IsUnityClass': False,
                })  # fmt: skip
                for m, value in found:
                    load_type = clrmeta.attribute_int(value) if len(value) >= 6 else 0
                    (before if load_type == 1 else after).append(len(methods))
                    methods.append({'m_ClassIndex': len(classes) - 1, 'm_MethodName': m.name, 'm_OrderNumber': 0})
        return {
            'm_AssemblyNames': assemblies,
            'm_NamespaceNames': namespaces,
            'm_ClassInfos': classes,
            'm_ClassMethodInfos': methods,
            'm_BeforeUnityMethodExecutionOrders': [],
            'm_AfterUnityMethodExecutionOrders': [],
            'm_BeforeMethodExecutionOrders': before,
            'm_AfterMethodExecutionOrders': after,
        }


def file_rank(file_name):
    """The order the build numbers its files in: built-ins, globalgamemanagers.assets,
    resources.assets, then sharedassetsN by N."""
    m = re.match(r'sharedassets(\d+)\.assets$', file_name)
    if m:
        return (4, int(m[1]))
    order = [BUILTIN_EXTRA_FILE, DEFAULT_RESOURCES_FILE, GGA, 'resources.assets']
    return (order.index(file_name), 0) if file_name in order else (9, file_name)


def script_id(fields):
    return typehash.script_id(fields['m_ClassName'], fields['m_Namespace'], fields['m_AssemblyName'])


def hash128(data):
    return {f'bytes[{i}]': b for i, b in enumerate(data)}


@functools.cache
def runtime_classes():
    """[(class ID, type hash)] of every class the player can load from data: the concrete,
    non-editor classes of this version, bar the global managers."""
    from tpk_ar.enums import TpkUnityClassFlags as Flags
    from UnityPy.helpers import Tpk

    tpk = Tpk.get_typetree()
    info = {}
    for cid, c in tpk.ClassInformation.items():
        try:
            vc = c.getVersionedClass(convert.VERSION)
        except ValueError:
            continue
        if vc is not None:
            info[cid] = vc
    by_name = {tpk.StringBuffer[v.Name]: k for k, v in info.items()}

    def managers_base(cid):
        for _ in range(32):
            v = info.get(cid)
            if v is None or v.Base is None:
                return False
            base = tpk.StringBuffer[v.Base]
            if base == 'GlobalGameManager':
                return True
            cid = by_name.get(base)
        return False

    player = player_type_hashes()
    out = []
    for cid, v in sorted(info.items()):
        if v.Flags & (Flags.IsAbstract | Flags.IsEditorOnly) or v.ReleaseRootNode is None or managers_base(cid):
            continue
        out.append((cid, player.get(cid) or typehash.builtin_hash(cid, convert.VERSION)))
    return out


# The PS Vita player's type hashes of the texture classes its own files do not hold. Its texture
# trees differ from the TPK's (Texture2D's does too: see player_type_hashes), so these are the
# player's as a 2018.2.19f1 PS Vita build records them in BuildSettings.runtimeClassHashes.
PLAYER_TEXTURE_HASHES = {
    89: '8ae46eedf9c00edaad35d0d09290bc8b',  # Cubemap
    117: '7b8ac67c57c69d8474c4193ded4bb141',  # Texture3D
    187: 'a0d4ce7908f2f4f9118f51938c142bf7',  # Texture2DArray
    188: 'd3344c9cd3d8b81b7d6eee88a126a0f5',  # CubemapArray
}


@functools.cache
def player_type_hashes():
    """{class ID: type hash} of the PS Vita player's own trees where they are known: those in its
    unity default resources (Texture2D, Mesh and Sprite differ from the TPK's) and the other
    texture classes."""
    from container import parse_serialized

    out = {cid: bytes.fromhex(h) for cid, h in PLAYER_TEXTURE_HASHES.items()}
    player = PSP2_PLAYER / 'Data' / 'Resources' / 'unity default resources'
    for t in parse_serialized(player.read_bytes()).types:
        if t.class_id != 114:  # MonoBehaviour's hash is per script
            out[t.class_id] = bytes(t.old_type_hash)
    return out


def resource_key(path):
    """Resources.Load's name for an asset: its path under the Resources folder, lowercase, without
    the extension."""
    rest = re.split(r'(?:^|/)Resources/', path, maxsplit=1)[1]
    return rest.rsplit('.', 1)[0].lower() if '.' in rest.rsplit('/', 1)[-1] else rest.lower()


def resource_dependencies(layout, placement):
    """(object, [references]) for each resources.assets object that references others."""
    import references

    graph = references.Graph(layout, placement)
    out = []
    for g, fid, _ in layout.objects('resources.assets'):
        where = placement.where[(g, fid)]
        refs = [ptr(*r) for r in graph.direct(g, fid) if r != where]
        if refs:
            out.append((ptr(*where), refs))
    return out


def managers(layout, placement, entries):
    """[(path ID, class ID, release dict)] of globalgamemanagers, in path ID order."""
    return Managers(layout, placement, entries).all()
