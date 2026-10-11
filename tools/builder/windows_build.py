"""The class builders of the Windows target (build.Build.IMPORTED_FOR['windows']).

    import windows_build
    windows_build.register(build.Build)
    b = build.Build(out, 'windows')

Per class, what differs from the PS Vita build:

- Shader (48): Direct3D 11 programs (windows_shaders.py).
- AudioClip (83): FSB5 banks of 16-bit PCM instead of HEVAG (windows_audio.py).
- Texture2D (28), Sprite (213), Font (128), font materials (21), TextAsset (49): the modules the
  PS Vita uses, given the PC project (build.root). Texture formats follow the importer's
  Standalone settings: see texture() below for what textures.py has to offer for that.
- Built-in material copies (21): as on the PS Vita (builtin_copies.py).

Everything else (scenes, prefabs, YAML assets) goes through convert.py as on the PS Vita.
"""

import builtin_copies

FONT_EXTENSIONS = {'.ttf', '.otf'}


def resS(build, file_name):
    import textures

    name = f'{file_name}.resS'
    return textures.Stream(name, build.streams.setdefault(name, bytearray()))


def texture(build, file_name, guid, file_id, asset):
    """Texture2D. textures.texture() picks the format from the PS Vita override, else the default
    platform's setting; the PC project's importers have no PS Vita override and import
    uncompressed (the pipeline's texture_overlay), which gives the Windows formats (RGBA32 /
    RGB24). What it needs to expose for Windows proper: a build target argument, e.g.
    texture(asset, file_name, stream, project, target='Standalone'), that reads that override
    and lets DXT through for non-power-of-two sizes that are multiples of 4 (Windows has no
    power-of-two rule), and the BC7 (25) format one texture asks for."""
    import fonts
    import textures

    if asset is None or asset.is_yaml:
        return None
    if asset.ext in FONT_EXTENSIONS:
        return fonts.texture(asset), None
    try:
        return textures.texture(asset, file_name, resS(build, file_name), project=build.root), None
    except NotImplementedError:
        return None


def sprite(build, file_name, guid, file_id, asset):
    import textures

    if asset is None or asset.is_yaml:
        return None
    return textures.sprite(asset, build.placement, file_name, project=build.root), None


def font(build, file_name, guid, file_id, asset):
    import fonts

    if asset is None or asset.is_yaml:
        return None
    return fonts.font(asset, build.placement, file_name, project=build.root), None


def material(build, file_name, guid, file_id, asset):
    import fonts

    made = builtin_copies.material(build, file_name, guid, file_id, asset)
    if made is None and asset is not None and asset.ext in FONT_EXTENSIONS:
        return fonts.material(asset, build.placement), None
    return made


def text(build, file_name, guid, file_id, asset):
    import imported

    if asset is None or asset.is_yaml:
        return None
    return imported.text_asset(asset, project=build.root), None


def shader(build, file_name, guid, file_id, asset):
    import windows_shaders

    return windows_shaders.imported(build, file_name, guid, file_id, asset)


def audio(build, file_name, guid, file_id, asset):
    import windows_audio

    return windows_audio.imported(build, file_name, guid, file_id, asset)


BUILDERS = {28: texture, 213: sprite, 128: font, 21: material, 49: text, 48: shader, 83: audio}


def register(build_class):
    build_class.IMPORTED_FOR['windows'].update(BUILDERS)
