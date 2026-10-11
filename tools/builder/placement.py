"""Where every asset object lands (file, path ID), and comparing converted objects with the
reference build's.

    placement = Placement(Layout(), file_names)
    ctx = AssetContext(placement, file_name, asset_guid)   # for convert.convert(..., ctx)
    same(ours, theirs, '', diffs, externals, file_name)       # externals: [''] + reference paths
"""

import math

BUILTIN_EXTRA = '0000000000000000f000000000000000'
DEFAULT_RESOURCES = '0000000000000000e000000000000000'


def first_path_id(file_name):
    return 2 if file_name.startswith('sharedassets') or file_name == 'globalgamemanagers.assets' else 1


class Placement:
    """(guid, fileID) -> (file, path ID) for every asset object the layout writes."""

    def __init__(self, layout, files):
        self.where = {}
        for f in files:
            start = first_path_id(f)
            for i, (g, fid, _) in enumerate(layout.objects(f)):
                self.where[(g, fid)] = (f, start + i)

    def find(self, guid, fid):
        if (guid, fid) in self.where:
            return self.where[(guid, fid)]
        if guid == BUILTIN_EXTRA:
            return ('resources/unity_builtin_extra', fid)
        if guid == DEFAULT_RESOURCES:
            return ('library/unity default resources', fid)
        # Not written by the build (editor-only classes, compute shaders, unused sub-objects): the
        # player reads the reference as null.
        return (None, 0)


class AssetContext:
    """Resolves an asset's YAML references: to (file, path ID) through the placement, and script
    references (m_Script, script-bound curves) to MonoScripts in globalgamemanagers.assets through
    `monoscripts` {(assembly, namespace, class): path ID} when `scripts` is given."""

    def __init__(self, placement, file_name, guid, scripts=None, monoscripts=None):
        self.placement, self.file, self.guid = placement, file_name, guid
        self.scripts, self.monoscripts = scripts, monoscripts or {}
        self.fields = None  # the YAML of the object being converted, for ambiguous script references

    def pptr(self, ref, node):
        fid = int(ref.get('fileID', 0))
        if fid == 0:
            return {'m_FileID': 0, 'm_PathID': 0}
        guid = ref.get('guid', self.guid)
        if self.scripts is not None and guid in self.scripts.assemblies:
            pid = self.monoscripts.get(self.scripts.resolve(ref, self.fields), 0)
            return {'m_FileID': 0, 'm_PathID': pid, 'file': 'globalgamemanagers.assets'}
        f, pid = self.placement.find(guid, fid)
        if f is None:
            return {'m_FileID': 0, 'm_PathID': 0}
        return {'m_FileID': 0, 'm_PathID': pid, 'file': f}


def same(a, b, path, diffs, externals, here):
    if isinstance(a, dict) and 'm_PathID' in a and 'm_FileID' in a:
        n = b['m_FileID']
        f = (externals[n] if 0 <= n < len(externals) else f'?external {n}') if n else here
        theirs = ('', 0) if b['m_PathID'] == 0 else (f, b['m_PathID'])
        mine = ('', 0) if a['m_PathID'] == 0 else (a.get('file', here), a['m_PathID'])
        if mine != theirs:
            diffs.append((path, mine, theirs))
        return
    if isinstance(a, dict):
        if not isinstance(b, dict) or set(a) != set(b):
            diffs.append((path, 'keys', sorted(set(a) ^ set(b or {}))[:6]))
            return
        for k in a:
            same(a[k], b[k], f'{path}.{k}', diffs, externals, here)
        return
    if isinstance(a, (list, tuple)):
        if not isinstance(b, (list, tuple)) or len(a) != len(b):
            diffs.append((path, f'len {len(a)}', f'len {len(b) if isinstance(b, (list, tuple)) else b}'))
            return
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            same(x, y, f'{path}[{i}]', diffs, externals, here)
        return
    if isinstance(a, float) and isinstance(b, float) and (a == b or (math.isnan(a) and math.isnan(b))):
        return
    if isinstance(a, (bytes, bytearray)) or isinstance(b, (bytes, bytearray)):
        if bytes(a) != bytes(b):
            diffs.append((path, f'{len(a)} bytes', f'{len(b)} bytes'))
        return
    if a != b:
        diffs.append((path, a, b))
