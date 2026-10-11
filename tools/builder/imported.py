"""Objects of imported files (textures, fonts, text) for build.py.

    import imported
    imported.register(Build)    # after build.py's own Build.IMPORTED entries: it chains to them

Hooks take (build, file_name, guid, file_id, asset) and return (data, tree) or None; objects
this module does not make (built-in ones have no asset) go to the hook registered before.
Textures append their pixels to build.streams['<file>.resS'] (a bytearray, as for audio's
.resource), in path ID order, which build.py writes next to the serialized file.

A text asset keeps its file's bytes as they are; the player reads them as a string, so bytes
that are not UTF-8 travel as surrogate escapes (that is how UnityPy writes them back).
"""

import fonts
import textures
from paths import PROJECT

FONT_EXTENSIONS = {'.ttf', '.otf'}


def text_asset(asset, project=PROJECT):
    data = (project / asset.path).read_bytes()
    return {'m_Name': asset.name_of(4900000), 'm_Script': data.decode('utf-8', 'surrogateescape')}


def stream(build, file_name):
    name = f'{file_name}.resS'
    streams = build.__dict__.setdefault('streams', {})
    return textures.Stream(name, streams.setdefault(name, bytearray()))


def texture2d(build, file_name, guid, file_id, asset):
    if asset.ext in FONT_EXTENSIONS:
        return fonts.texture(asset)
    return textures.texture(asset, file_name, stream(build, file_name))


def sprite(build, file_name, guid, file_id, asset):
    return textures.sprite(asset, build.placement, file_name)


def font(build, file_name, guid, file_id, asset):
    return fonts.font(asset, build.placement, file_name)


def font_material(build, file_name, guid, file_id, asset):
    if asset.ext not in FONT_EXTENSIONS:
        return None
    return fonts.material(asset, build.placement)


def text(build, file_name, guid, file_id, asset):
    return text_asset(asset)


BUILDERS = {28: texture2d, 213: sprite, 128: font, 21: font_material, 49: text}


def build_object(build, file_name, guid, file_id, asset):
    """(data, tree) for an imported file's object, or None if this module does not make it."""
    if asset is None or asset.is_yaml or asset.classes.get(file_id) not in BUILDERS:
        return None
    data = BUILDERS[asset.classes[file_id]](build, file_name, guid, file_id, asset)
    return None if data is None else (data, None)


def register(build_class):
    for class_id in BUILDERS:
        previous = build_class.IMPORTED.get(class_id)

        def hook(build, file_name, guid, file_id, asset, previous=previous):
            made = build_object(build, file_name, guid, file_id, asset)
            if made is None and previous is not None:
                return previous(build, file_name, guid, file_id, asset)
            return made

        build_class.IMPORTED[class_id] = hook
