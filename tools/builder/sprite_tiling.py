"""A 2D collider's sprite tiling (m_SpriteTilingProperty of PolygonCollider2D and BoxCollider2D).

The build writes it as the editor recomputes it on load, not as saved: when the collider's
GameObject has a SpriteRenderer with a sprite, it comes from that renderer and its sprite (the
sprite's border and pivot, its rect in units as oldSize, and the renderer's size, draw mode and
tiling settings). Scenes and prefabs alike. A collider with no renderer, or a renderer with no
sprite, keeps the saved values.

    in_scene(d, doc, project, scene)     # a FIXUPS entry
    in_prefab(d, doc, asset, project)    # an ASSET_FIXUPS entry
"""

from functools import cache

SPRITE_RENDERER = 212
SPRITE = 213
ADAPTIVE = '1'  # SpriteTileMode.Adaptive


def in_scene(d, doc, project, scene):
    if scene is not None:  # a prefab's objects go through in_prefab
        apply(d, scene.component(doc.fields['m_GameObject'], SPRITE_RENDERER), project.root)


def in_prefab(d, doc, asset, project):
    go = asset.docs.get(int(doc.fields['m_GameObject']['fileID']))
    components = (asset.docs.get(int(next(iter(c.values()))['fileID'])) for c in go.fields.get('m_Component', []))
    renderer = next((c for c in components if c is not None and c.class_id == SPRITE_RENDERER), None)
    apply(d, renderer, project.root, asset.guid)


def apply(d, renderer, root, guid=None):
    from convert import number
    from textures import f32

    if renderer is None:
        return
    r = renderer.fields
    sprite = _sprite(r['m_Sprite'], root, guid)
    if sprite is None:
        return
    width, height, pivot, ppu, border = sprite
    d['m_SpriteTilingProperty'].update(
        border=border,
        pivot=pivot,
        oldSize={'x': f32(width / ppu), 'y': f32(height / ppu)},
        newSize={k: number(r['m_Size'][k], 'float') for k in 'xy'},
        adaptiveTilingThreshold=number(r['m_AdaptiveModeThreshold'], 'float'),
        drawMode=int(r['m_DrawMode']),
        adaptiveTiling=str(r['m_SpriteTileMode']) == ADAPTIVE,
    )


@cache
def _db(root):
    from assets import AssetDB

    return AssetDB(root)


def _sprite(ref, root, guid):
    """(rect width, height, pivot, pixels to units, border) of the Sprite a reference names, or None."""
    from convert import number

    fid = int(ref.get('fileID', 0))
    a = _db(root).by_guid.get(ref.get('guid', guid)) if fid else None
    if a is None or a.classes.get(fid) != SPRITE:
        return None
    if a.is_yaml:
        f = a.docs[fid].fields
        rect, pivot, border = f['m_Rect'], f['m_Pivot'], f['m_Border']
        return (
            number(rect['width'], 'float'),
            number(rect['height'], 'float'),
            {k: number(pivot[k], 'float') for k in 'xy'},
            number(f['m_PixelsToUnits'], 'float'),
            {k: number(border[k], 'float') for k in 'xyzw'},
        )
    # A texture imported as one sprite (textures.sprite): its rect is the whole image.
    import textures
    from PIL import Image

    settings = textures.importer(a)
    with Image.open(root / a.path) as img:
        width, height = img.size
    alignment = int(settings.get('alignment', 0))
    if alignment == 9:
        pivot = {k: number(settings['spritePivot'][k], 'float') for k in 'xy'}
    else:
        pivot = dict(zip('xy', map(float, textures.ALIGNMENTS[alignment]), strict=True))
    border = settings.get('spriteBorder') or {}
    return (
        float(width),
        float(height),
        pivot,
        number(str(settings.get('spritePixelsToUnits', 100)), 'float'),
        {k: number(str(border.get(k, 0)), 'float') for k in 'xyzw'},
    )
