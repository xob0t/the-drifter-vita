"""Built-in extra resources the build copies into its own files (layout.place_builtins): the
default sprite material and the sprite mask material. Their data comes from the editor's
Resources/unity_builtin_extra, mapped onto the player's type tree by field name."""

from functools import cache

import convert
import UnityPy
from layout import BUILTIN_EXTRA
from paths import EDITOR_DATA


@cache
def editor_objects():
    env = UnityPy.load(str(EDITOR_DATA / 'Resources' / 'unity_builtin_extra'))
    return next(iter(env.files.values())).objects


def release(node, value):
    """The editor's value reduced to the release tree's fields."""
    if isinstance(value, dict) and node.m_Children and node.m_Type not in ('vector', 'map'):
        return {c.m_Name: release(c, value.get(c.m_Name, convert.default(c))) for c in node.m_Children}
    return value


def material(build, file_name, guid, file_id, asset):
    """Build.IMPORTED[21] for built-in materials: (data, None), or None for other materials."""
    if guid != BUILTIN_EXTRA:
        return None
    data = release(convert.release_tree(21), editor_objects()[file_id].read_typetree())
    shader = data['m_Shader']['m_PathID']
    found = build.placement.where.get((BUILTIN_EXTRA, shader))
    data['m_Shader'] = (
        {'m_FileID': 0, 'm_PathID': found[1], 'file': found[0]}
        if found
        else {'m_FileID': 0, 'm_PathID': shader, 'file': 'resources/unity_builtin_extra'}
    )
    return data, None
