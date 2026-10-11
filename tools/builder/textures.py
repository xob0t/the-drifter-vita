"""Import textures the way Unity 2018.2's TextureImporter does for the PS Vita build.

    stream = Stream('sharedassets1.assets.resS', bytearray())
    data = texture(asset, file_name, stream)     # Texture2D release dict; image bytes appended to stream
    data = sprite(asset, placement, file_name)    # the Sprite (21300000) of a single-sprite texture

What the reference build shows (tools/builder/check_imported.py verifies it):
- Format: the importer's PSP2 override if it is on, else the default platform's setting
  (automatic: DXT5/DXT1 when compressed, RGBA32/RGB24 when not, by whether the image has
  alpha). The PS Vita compresses only power-of-two textures: any other size falls back to
  RGBA32 (DXT5) or RGB24 (DXT1). One mip level (no texture here enables mipmaps).
- Pixels are stored bottom row first, linear (not swizzled). With "alpha is transparency" the
  RGB of fully transparent texels is filled from the nearest visible texel (dilate()), so
  filtering at sprite edges does not bleed black.
- Readable textures keep their pixels in the object ("image data"); every other one streams
  them from <file>.resS, appended in path ID order with no padding (the .resS holds nothing
  else).
- m_LightmapFormat is 6 ("always padded") for sprites, 0 otherwise; m_ColorSpace is sRGBTexture.
"""

import hashlib
import struct
from functools import cache
from pathlib import Path

import numpy as np
import unityyaml
from paths import PROJECT, WORK
from PIL import Image

RGB24, RGBA32, DXT1, DXT5 = 3, 4, 10, 12
COMPRESSED = {DXT1: RGB24, DXT5: RGBA32}  # and what each falls back to when the PS Vita cannot use it
BUILD_TARGET = 'PSP2'
SPRITE = 8  # textureType
ALWAYS_PADDED = 6
CACHE = WORK / 'textures'
CACHE_VERSION = 2  # bump when the pixels change for the same input
FAR = 1 << 30
# Sprite pivots by the importer's alignment (9 is the custom spritePivot).
ALIGNMENTS = [(0.5, 0.5), (0, 1), (0.5, 1), (1, 1), (0, 0.5), (1, 0.5), (0, 0), (0.5, 0), (1, 0)]
SPRITE_FILE_ID = 21300000
TEXTURE_FILE_ID = 2800000


class Stream:
    """A .resS file being written (data: the bytearray build.py keeps in build.streams[name]).
    Textures append their image data in path ID order."""

    def __init__(self, name, data):
        self.name = name
        self.data = data

    def add(self, data):
        offset = len(self.data)
        self.data += data
        return {'offset': offset, 'size': len(data), 'path': self.name}


def importer(asset):
    return meta(asset.meta_path)['TextureImporter']


@cache
def meta(path):
    # unityyaml.meta cannot open the str path AssetDB stores
    return unityyaml.meta(Path(path))


def platform(settings, target):
    for p in settings.get('platformSettings') or []:
        if p.get('buildTarget') == target:
            return p
    return None


def has_alpha(rgba, settings):
    return settings.get('alphaUsage', '1') != '0' and bool((rgba[..., 3] < 255).any())


def is_pot(n):
    return n & (n - 1) == 0


def texture_format(settings, width, height, alpha):
    override = platform(settings, BUILD_TARGET)
    if override and override.get('overridden') == '1' and int(override.get('textureFormat', -1)) >= 0:
        fmt = int(override['textureFormat'])
    else:
        default = platform(settings, 'DefaultTexturePlatform') or {}
        requested = int(default.get('textureFormat', -1))
        if requested >= 0:
            fmt = requested
        elif default.get('textureCompression', '1') != '0':
            fmt = DXT5 if alpha else DXT1
        else:
            fmt = RGBA32 if alpha else RGB24
    if fmt in COMPRESSED and not (is_pot(width) and is_pot(height)):
        fmt = COMPRESSED[fmt]
    if fmt not in (RGB24, RGBA32, DXT1, DXT5):
        raise NotImplementedError(f'texture format {fmt}')
    return fmt


def dilate(rgba):
    """Fill the RGB of fully transparent texels as Unity's "alpha is transparency" does: each
    takes the colour of the visible texel nearest to it in Manhattan distance; on a tie the one
    in the lowest row wins (rows are bottom-up), then the leftmost. Matches all 205 RGBA32
    textures of the reference.

    Computed exactly in two separable passes: the nearest visible texel within each row, then a
    1D distance transform down and up each column that keeps the lower row on ties."""
    visible = rgba[..., 3] != 0
    if visible.all() or not visible.any():
        return rgba
    h, w = visible.shape
    cols = np.arange(w)
    left = np.maximum.accumulate(np.where(visible, cols, -1), axis=1)
    right = np.minimum.accumulate(np.where(visible, cols, FAR)[:, ::-1], axis=1)[:, ::-1]
    to_left = np.where(left >= 0, cols - left, FAR)
    to_right = np.where(right < FAR, right - cols, FAR)
    use_left = to_left <= to_right
    distance = np.where(use_left, to_left, to_right)
    source_x = np.where(use_left, left, right)
    rows = np.broadcast_to(np.arange(h)[:, None], (h, w))

    def sweep(order, keep_tie):
        d, sy, sx = distance.copy(), rows.copy(), source_x.copy()
        for prev, y in order:
            carried = d[prev] + 1
            take = carried <= d[y] if keep_tie else carried < d[y]
            d[y] = np.where(take, carried, d[y])
            sy[y] = np.where(take, sy[prev], sy[y])
            sx[y] = np.where(take, sx[prev], sx[y])
        return d, sy, sx

    # carried up from lower rows (they win ties), and down from higher rows (they lose them)
    up_d, up_y, up_x = sweep([(y - 1, y) for y in range(1, h)], True)
    down_d, down_y, down_x = sweep([(y + 1, y) for y in range(h - 2, -1, -1)], False)
    use_up = up_d <= down_d
    source_y = np.where(use_up, up_y, down_y)
    source_x = np.where(use_up, up_x, down_x)
    out = rgba.copy()
    hidden = ~visible
    out[hidden, :3] = rgba[source_y[hidden], source_x[hidden], :3]
    return out


def encoder():
    """quicktex (rgbcx, Unity-like quality) when installed, else etcpak (UnityPy's dependency)."""
    try:
        import quicktex
        from quicktex.s3tc import bc1, bc3

        # quality levels: BC3 at 5 already beats Unity's DXT5; BC1 is cheap enough for the best
        encoders = {DXT1: bc1.BC1Encoder(18, bc1.BC1Encoder.ColorMode.FourColor), DXT5: bc3.BC3Encoder(5)}

        def encode(rgba, fmt):
            h, w = rgba.shape[:2]
            return encoders[fmt].encode(quicktex.RawTexture.frombytes(rgba.tobytes(), w, h)).tobytes()

        return 'quicktex bc1 18 bc3 5', encode
    except ImportError:
        import etcpak

        def encode(rgba, fmt):
            h, w = rgba.shape[:2]
            return (etcpak.compress_bc3 if fmt == DXT5 else etcpak.compress_bc1)(rgba.tobytes(), w, h)

        return 'etcpak', encode


ENCODER_NAME, ENCODE = encoder()


def load_rgba(path):
    with Image.open(path) as img:
        return np.ascontiguousarray(np.asarray(img.convert('RGBA'))[::-1])


def pixels(path, settings, fmt, rgba):
    """The image data Unity stores for one texture (cached under work/builder/textures)."""
    recipe = (CACHE_VERSION, fmt, settings.get('alphaIsTransparency'), ENCODER_NAME)
    key = hashlib.sha1(path.read_bytes() + repr(recipe).encode())
    cached = CACHE / f'{key.hexdigest()}.bin'
    if cached.exists():
        return cached.read_bytes()
    if settings.get('alphaIsTransparency') == '1':
        rgba = dilate(rgba)
    if fmt == RGBA32:
        data = rgba.tobytes()
    elif fmt == RGB24:
        data = np.ascontiguousarray(rgba[..., :3]).tobytes()
    else:
        data = ENCODE(rgba, fmt)
    CACHE.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return data


def texture_settings(settings):
    t = settings.get('textureSettings') or {}
    return {
        'm_FilterMode': int(t.get('filterMode', -1)),
        'm_Aniso': int(t.get('aniso', -1)),
        'm_MipBias': float(t.get('mipBias', -100)),
        'm_WrapU': int(t.get('wrapU', -1)),
        'm_WrapV': int(t.get('wrapV', -1)),
        'm_WrapW': int(t.get('wrapW', -1)),
    }


def texture(asset, file_name, stream, project=PROJECT):
    """The Texture2D of an imported image. Non-readable textures append their pixels to stream
    (a Stream for file_name's .resS)."""
    settings = importer(asset)
    path = project / asset.path
    rgba = load_rgba(path)
    height, width = rgba.shape[:2]
    if settings['mipmaps'].get('enableMipMap', '0') != '0':
        raise NotImplementedError(f'{asset.path}: mipmaps')
    size = int((platform(settings, BUILD_TARGET) or {}).get('maxTextureSize') or settings.get('maxTextureSize', 2048))
    if max(width, height) > size:
        raise NotImplementedError(f'{asset.path}: downscaling to {size}')
    scaled = settings.get('nPOTScale', '0') != '0' and settings.get('textureType') != str(SPRITE)
    if scaled and not (is_pot(width) and is_pot(height)):
        raise NotImplementedError(f'{asset.path}: non-power-of-two scaling')
    fmt = texture_format(settings, width, height, has_alpha(rgba, settings))
    data = pixels(path, settings, fmt, rgba)
    readable = settings.get('isReadable') == '1'
    if readable:
        image, streamed = data, {'offset': 0, 'size': 0, 'path': ''}
    else:
        image, streamed = b'', stream.add(data)
    return {
        'm_Name': asset.name_of(TEXTURE_FILE_ID),
        'm_ForcedFallbackFormat': RGBA32,
        'm_DownscaleFallback': False,
        'm_Width': width,
        'm_Height': height,
        'm_CompleteImageSize': len(data),
        'm_TextureFormat': fmt,
        'm_MipCount': 1,
        'm_IsReadable': readable,
        'm_StreamingMipmaps': settings.get('streamingMipmaps', '0') == '1',
        'm_StreamingMipmapsPriority': int(settings.get('streamingMipmapsPriority', 0)),
        'm_ImageCount': 1,
        'm_TextureDimension': 2,
        'm_TextureSettings': texture_settings(settings),
        'm_LightmapFormat': ALWAYS_PADDED if settings.get('textureType') == str(SPRITE) else 0,
        'm_ColorSpace': int(settings['mipmaps'].get('sRGBTexture', 1)),
        'image data': image,
        'm_StreamData': streamed,
    }


def f32(x):
    return struct.unpack('<f', struct.pack('<f', x))[0]


def sprite(asset, placement, file_name, project=PROJECT):
    """The Sprite a texture imported as a single sprite makes: a full-rect quad over the whole
    texture (tight meshes and sprite sheets are not needed by this project)."""
    import convert

    settings = importer(asset)
    if settings.get('spriteMeshType', '1') != '0':
        raise NotImplementedError(f'{asset.path}: tight sprite mesh')
    with Image.open(project / asset.path) as img:
        width, height = img.size
    alignment = int(settings.get('alignment', 0))
    custom = alignment == 9
    pivot = tuple(f32(float(settings['spritePivot'][k])) for k in 'xy') if custom else ALIGNMENTS[alignment]
    ppu = f32(float(settings.get('spritePixelsToUnits', 100)))
    px, py = f32(pivot[0] * width), f32(pivot[1] * height)
    left, right = -px / ppu, (width - px) / ppu
    bottom, top = -py / ppu, (height - py) / ppu
    positions = struct.pack('<12f', left, top, 0, right, top, 0, left, bottom, 0, right, bottom, 0)
    # the build leaves the UVs zero: the player derives them from textureRect
    vertex_data = positions + bytes(4 * 8)
    channels = [{'stream': 0, 'offset': 0, 'format': 0, 'dimension': 0} for _ in range(14)]
    channels[0]['dimension'] = 3
    channels[4].update(stream=1, dimension=2)
    border = settings.get('spriteBorder') or {}
    file, texture_pid = placement.find(asset.guid, TEXTURE_FILE_ID)
    zero = {'x': 0.0, 'y': 0.0, 'z': 0.0}
    return {
        'm_Name': asset.name_of(SPRITE_FILE_ID),
        'm_Rect': {'x': 0.0, 'y': 0.0, 'width': float(width), 'height': float(height)},
        'm_Offset': {'x': f32(px - width / 2), 'y': f32(py - height / 2)},
        'm_Border': {k: float(border.get(k, 0)) for k in 'xyzw'},
        'm_PixelsToUnits': ppu,
        'm_Pivot': {'x': pivot[0], 'y': pivot[1]},
        'm_Extrude': int(settings.get('spriteExtrude', 1)),
        'm_IsPolygon': False,
        'm_RenderDataKey': (convert.guid_words(asset.guid), SPRITE_FILE_ID),
        'm_AtlasTags': [],
        'm_SpriteAtlas': {'m_FileID': 0, 'm_PathID': 0},
        'm_RD': {
            'texture': {'m_FileID': 0, 'm_PathID': texture_pid, 'file': file},
            'alphaTexture': {'m_FileID': 0, 'm_PathID': 0},
            'm_SubMeshes': [
                {
                    'firstByte': 0,
                    'indexCount': 6,
                    'topology': 0,
                    'baseVertex': 0,
                    'firstVertex': 0,
                    'vertexCount': 4,
                    'localAABB': {'m_Center': dict(zero), 'm_Extent': dict(zero)},
                }
            ],
            'm_IndexBuffer': list(struct.pack('<6H', 0, 1, 2, 2, 1, 3)),
            'm_VertexData': {'m_VertexCount': 4, 'm_Channels': channels, 'm_DataSize': vertex_data},
            'm_Bindpose': [],
            'textureRect': {'x': 0.0, 'y': 0.0, 'width': float(width), 'height': float(height)},
            'textureRectOffset': {'x': 0.0, 'y': 0.0},
            'atlasRectOffset': {'x': -1.0, 'y': -1.0},
            'settingsRaw': 0,
            'uvTransform': {'x': ppu, 'y': px, 'z': ppu, 'w': py},
            'downscaleMultiplier': 1.0,
        },
        'm_PhysicsShape': physics_shape(project / asset.path, settings, pivot, ppu),
        'm_Bones': [],
    }


def physics_shape(path, settings, pivot, ppu):
    """The fallback physics shape (generateFallbackPhysicsShape). Unity traces the alpha outline
    2 pixels out from the visible texels, within the sprite's rect, and simplifies it; this takes the
    convex hull of the same area, which has the same bounds (what the game reads, GuiUtils)."""
    if settings.get('spriteGenerateFallbackPhysicsShape', '1') == '0':
        return []
    rgba = load_rgba(path)
    h, w = rgba.shape[:2]
    ys, xs = np.nonzero(rgba[..., 3] > 0)
    if not len(ys):
        return []
    texels = zip(xs.tolist(), ys.tolist(), strict=True)
    corners = {(x + dx, y + dy) for x, y in texels for dx in (0, 1) for dy in (0, 1)}
    grown = {(min(max(x + dx, 0), w), min(max(y + dy, 0), h)) for x, y in convex_hull(sorted(corners))
             for dx in (-2, 2) for dy in (-2, 2)}  # fmt: skip
    hull = convex_hull(sorted(grown))
    px, py = pivot[0] * w, pivot[1] * h
    return [[{'x': f32((x - px) / ppu), 'y': f32((y - py) / ppu)} for x, y in hull]]


def convex_hull(points):
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]
