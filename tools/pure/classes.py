"""What the editor's build changes beyond copying an object's fields (see convert.py).

Each fixup in FIXUPS takes the release dict, the YAML Doc, the Project and the Scene (None for an
object in a prefab), and edits the dict in place. ASSET_FIXUPS take the dict, the Doc and the Asset.
"""

from functools import cached_property

import unityyaml

RENDERERS = {23, 96, 120, 137, 199, 212, 331}  # Mesh, Trail, Line, SkinnedMesh, Particle, Sprite renderers, SpriteMask
# Unity's built-in tags; the project's own tags are numbered from 20000.
BUILTIN_TAGS = {
    'Untagged': 0,
    'Respawn': 1,
    'Finish': 2,
    'EditorOnly': 3,
    'MainCamera': 5,
    'Player': 6,
    'GameController': 7,
}


class Project:
    def __init__(self, root):
        self.root = root

    @cached_property
    def tags(self):
        manager = unityyaml.meta(self.root / 'ProjectSettings' / 'TagManager.asset')['TagManager']
        tags = dict(BUILTIN_TAGS)
        for i, tag in enumerate(manager.get('tags') or []):
            tags[tag] = 20000 + i
        return tags


def renderer(d, doc, project, scene):
    # No baked lighting: the build writes "no lightmap" (65535) and the identity scale/offset.
    for name in ('m_LightmapIndex', 'm_LightmapIndexDynamic'):
        d[name] = 65535
    for name in ('m_LightmapTilingOffset', 'm_LightmapTilingOffsetDynamic'):
        d[name] = {'x': 1.0, 'y': 1.0, 'z': 0.0, 'w': 0.0}


def sprite_renderer(d, doc, project, scene):
    renderer(d, doc, project, scene)
    # Loading a renderer whose sprite is gone (room templates lose theirs) clears the flag.
    d['m_WasSpriteAssigned'] = bool(d['m_Sprite']['m_PathID'])


def particle_renderer(d, doc, project, scene):
    renderer(d, doc, project, scene)
    # The second material slot is the trail material.
    while len(d['m_Materials']) < 2:
        d['m_Materials'].append({'m_FileID': 0, 'm_PathID': 0})


def particle_system(d, doc, project, scene):
    # 2018's startDelay is a constant or a random pick between two: its curves are not kept.
    for curve in ('maxCurve', 'minCurve'):
        d['startDelay'][curve]['m_Curve'] = []


def game_object(d, doc, project, scene):
    d['m_Tag'] = project.tags.get(doc.fields.get('m_TagString', 'Untagged'), 0)


def lightmap_settings(d, doc, project, scene):
    # Without baked lighting the build writes these regardless of the scene's settings.
    d['m_LightmapsMode'] = 1
    d['m_UseShadowmask'] = True


def rect_transform(d, doc, project, scene):
    """A screen space camera canvas drives its root RectTransform. The editor build writes the
    values for its batch mode screen (640x480) scaled by the CanvasScaler; the player recomputes
    them every frame, so only their shape matters. A prefab keeps its values as saved."""
    if scene is None:
        return
    canvas = scene.component(doc.fields['m_GameObject'], 223)
    if canvas is None or canvas.fields.get('m_RenderMode') != '1':
        return
    scaler = scene.script_component(doc.fields['m_GameObject'], 'm_ReferenceResolution')
    height = float(scaler.fields['m_ReferenceResolution']['y']) if scaler else 480.0
    d['m_LocalPosition'] = {'x': 0.0, 'y': 0.0, 'z': float(canvas.fields.get('m_PlaneDistance', 100))}
    d['m_LocalScale'] = {'x': 1.0, 'y': 1.0, 'z': 1.0}
    d['m_SizeDelta'] = {'x': height * 640 / 480, 'y': height}
    d['m_Pivot'] = {'x': 0.5, 'y': 0.5}


FIXUPS = {1: game_object, 157: lightmap_settings, 198: particle_system, 199: particle_renderer, 212: sprite_renderer}
FIXUPS[224] = rect_transform
for _cid in RENDERERS - {199, 212}:
    FIXUPS[_cid] = renderer


def sprite(d, doc, asset):
    # The build keys a sprite's render data by the sprite's own asset and fileID.
    import convert

    d['m_RenderDataKey'] = (convert.guid_words(asset.guid), doc.file_id)


# Fixups that need the asset the object comes from: f(release dict, YAML doc, Asset).
ASSET_FIXUPS = {213: sprite}
