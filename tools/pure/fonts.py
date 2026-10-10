"""Import TrueType fonts the way Unity 2018.2's TrueTypeFontImporter does for a dynamic font.

    font(asset, placement, file_name)    # Font (12800000)
    material(asset, placement)           # 'Font Material' (2100000)
    texture(asset)                       # 'Font Texture' (2800000)

A dynamic font keeps the whole TTF (m_FontData, byte for byte) and renders glyphs at run time,
so its texture is empty and its material is the built-in Font shader over that texture. What
the importer computes from the TTF is FreeType's view of it at the font size in pixels:
- ascent, descent and line spacing: the face's ascender, descender and height (hhea, or OS/2
  when hhea has none) times size / unitsPerEm, in float32.
- kerning: every pair of the 'kern' table (GPOS is not read), by character code through the
  Unicode cmap, as FT_Get_Kerning's default mode returns it: scaled, then shrunk by ppem/25
  below 25 ppem and rounded to whole pixels; zero pairs are left out.
"""

import struct
from functools import cache
from pathlib import Path

import numpy as np
import unityyaml
from paths import PROJECT

FONT_SHADER = ('library/unity default resources', 10101)  # Font.shader ("GUI/Text Shader")
MATERIAL_FILE_ID, TEXTURE_FILE_ID = 2100000, 2800000
PIXEL_SCALE = 0.10000000149011612  # 0.1 as float32
FACE_ASCENDER, FACE_BOUNDING_BOX = 1, 2


def f32(x):
    return float(np.float32(x))


class TrueType:
    """The few tables the importer reads."""

    def __init__(self, data):
        self.data = data
        count = struct.unpack_from('>H', data, 4)[0]
        self.tables = {}
        for i in range(count):
            tag, _, offset, length = struct.unpack_from('>4sIII', data, 12 + 16 * i)
            self.tables[tag.decode('latin-1')] = (offset, length)

    def table(self, tag):
        offset, length = self.tables[tag]
        return self.data[offset : offset + length]

    @property
    def units_per_em(self):
        return struct.unpack_from('>H', self.table('head'), 18)[0]

    @property
    def y_max(self):
        return struct.unpack_from('>h', self.table('head'), 42)[0]

    def metrics(self):
        """(ascender, descender, height) in font units, as FreeType sets them."""
        ascender, descender, gap = struct.unpack_from('>hhh', self.table('hhea'), 4)
        height = ascender - descender + gap
        if not (ascender or descender) and 'OS/2' in self.tables:
            os2 = self.table('OS/2')
            typo_asc, typo_desc, typo_gap, win_asc, win_desc = struct.unpack_from('>hhhHH', os2, 68)
            if typo_asc or typo_desc:
                ascender, descender, height = typo_asc, typo_desc, typo_asc - typo_desc + typo_gap
            else:
                ascender, descender = win_asc, -win_desc
                height = ascender - descender
        return ascender, descender, height

    def cmap(self):
        """{character code: glyph} from the Unicode subtable FreeType selects."""
        t = self.table('cmap')
        count = struct.unpack_from('>H', t, 2)[0]
        subtables = {}
        for i in range(count):
            pid, eid, offset = struct.unpack_from('>HHI', t, 4 + 8 * i)
            subtables[(pid, eid)] = offset
        # FreeType prefers a UCS-4 table, then any Unicode one
        for key in ((3, 10), (0, 6), (0, 4), (3, 1), (0, 3), (0, 2), (0, 1), (0, 0)):
            if key in subtables:
                return self.subtable(t, subtables[key])
        return {}

    @staticmethod
    def subtable(t, offset):
        fmt = struct.unpack_from('>H', t, offset)[0]
        out = {}
        if fmt == 4:
            segs = struct.unpack_from('>H', t, offset + 6)[0] // 2
            base = offset + 14
            ends = struct.unpack_from(f'>{segs}H', t, base)
            starts = struct.unpack_from(f'>{segs}H', t, base + 2 * segs + 2)
            deltas = struct.unpack_from(f'>{segs}h', t, base + 4 * segs + 2)
            range_base = base + 6 * segs + 2
            ranges = struct.unpack_from(f'>{segs}H', t, range_base)
            for i in range(segs):
                for c in range(starts[i], ends[i] + 1):
                    if c == 0xFFFF:
                        continue
                    if ranges[i] == 0:
                        g = (c + deltas[i]) & 0xFFFF
                    else:
                        at = range_base + 2 * i + ranges[i] + 2 * (c - starts[i])
                        g = struct.unpack_from('>H', t, at)[0]
                        g = (g + deltas[i]) & 0xFFFF if g else 0
                    if g:
                        out[c] = g
        elif fmt == 12:
            groups = struct.unpack_from('>I', t, offset + 12)[0]
            for i in range(groups):
                start, end, glyph = struct.unpack_from('>III', t, offset + 16 + 12 * i)
                for c in range(start, end + 1):
                    out[c] = glyph + c - start
        elif fmt == 6:
            first, count = struct.unpack_from('>HH', t, offset + 6)
            for i, g in enumerate(struct.unpack_from(f'>{count}H', t, offset + 10)):
                if g:
                    out[first + i] = g
        elif fmt == 0:
            out = {c: g for c, g in enumerate(t[offset + 6 : offset + 262]) if g}
        else:
            raise NotImplementedError(f'cmap format {fmt}')
        return out

    def kerning(self):
        """{(left glyph, right glyph): value in font units} from the 'kern' table's horizontal
        format 0 subtables, as FreeType reads it (the first such subtable it finds)."""
        if 'kern' not in self.tables:
            return {}
        t = self.table('kern')
        version, count = struct.unpack_from('>HH', t, 0)
        if version != 0:
            return {}
        p = 4
        for _ in range(count):
            _, length, coverage = struct.unpack_from('>HHH', t, p)
            if coverage >> 8 == 0 and coverage & 0x7 == 0x1:  # format 0, horizontal, kerning values
                n = struct.unpack_from('>H', t, p + 6)[0]
                pairs = {}
                for i in range(n):
                    left, right, value = struct.unpack_from('>HHh', t, p + 14 + 6 * i)
                    pairs[(left, right)] = value
                return pairs
            p += length
        return {}


def mul_fix(a, b):
    sign = -1 if (a < 0) != (b < 0) else 1
    return sign * ((abs(a) * abs(b) + 0x8000) >> 16)


def mul_div(a, b, c):
    sign = -1 if (a < 0) != (b < 0) else 1
    return sign * ((abs(a) * abs(b) + c // 2) // c)


def kerning(face, size):
    """[((left code, right code), pixels)] sorted by codes, as FT_Get_Kerning (FT_KERNING_DEFAULT)
    gives them at size pixels per em."""
    pairs = face.kerning()
    if not pairs:
        return []
    x_scale = div_fix(size * 64, face.units_per_em)
    codes = {}
    for code, glyph in face.cmap().items():
        codes.setdefault(glyph, []).append(code)
    out = {}
    for (left, right), value in pairs.items():
        x = mul_fix(value, x_scale)
        if size < 25:
            x = mul_div(x, size, 25)
        x = (x + 32) & ~63
        if x == 0:
            continue
        for a in codes.get(left, ()):
            for b in codes.get(right, ()):
                out[(a, b)] = x / 64
    return sorted(out.items())


def div_fix(a, b):
    sign = -1 if (a < 0) != (b < 0) else 1
    return sign * (((abs(a) << 16) + abs(b) // 2) // abs(b))


def importer(asset):
    return meta(asset.meta_path)['TrueTypeFontImporter']


@cache
def meta(path):
    # Asset.meta cannot open the str path AssetDB stores
    return unityyaml.meta(Path(path))


def font(asset, placement, file_name, project=PROJECT):
    settings = importer(asset)
    if settings.get('fontRenderingMode') is None or settings.get('includeFontData', '1') != '1':
        raise NotImplementedError(f'{asset.path}: font without its data')
    data = (project / asset.path).read_bytes()
    face = TrueType(data)
    size = int(settings.get('fontSize', 16))
    scale = np.float32(size) / np.float32(face.units_per_em)
    ascender, descender, height = face.metrics()
    mode = int(settings.get('ascentCalculationMode', FACE_ASCENDER))
    if mode == FACE_BOUNDING_BOX:
        ascender = face.y_max
    elif mode != FACE_ASCENDER:
        raise NotImplementedError(f'{asset.path}: ascent calculation mode {mode}')

    def ref(file_id):
        f, pid = placement.find(asset.guid, file_id)
        return {'m_FileID': 0, 'm_PathID': pid, 'file': f}

    fallbacks = []
    for r in settings.get('fallbackFontReferences') or []:
        f, pid = placement.find(r['guid'], int(r['fileID']))
        fallbacks.append({'m_FileID': 0, 'm_PathID': pid, 'file': f})
    return {
        'm_Name': asset.name_of(12800000),
        'm_LineSpacing': f32(np.float32(height) * scale),
        'm_DefaultMaterial': ref(MATERIAL_FILE_ID),
        'm_FontSize': float(size),
        'm_Texture': ref(TEXTURE_FILE_ID),
        'm_AsciiStartOffset': 0,
        'm_Tracking': 1.0,
        'm_CharacterSpacing': int(settings.get('characterSpacing', 0)),
        'm_CharacterPadding': int(settings.get('characterPadding', 1)),
        'm_ConvertCase': int(settings.get('forceTextureCase', -2)),
        'm_CharacterRects': [],
        'm_KerningValues': kerning(face, size),
        'm_PixelScale': PIXEL_SCALE,
        'm_FontData': data,
        'm_Ascent': f32(np.float32(ascender) * scale),
        'm_Descent': f32(np.float32(descender) * scale),
        'm_DefaultStyle': 0,
        'm_FontNames': list(settings.get('fontNames') or [settings.get('fontName', '')]),
        'm_FallbackFonts': fallbacks,
        'm_FontRenderingMode': int(settings['fontRenderingMode']),
        'm_UseLegacyBoundsCalculation': settings.get('useLegacyBoundsCalculation', '0') == '1',
        'm_ShouldRoundAdvanceValue': settings.get('shouldRoundAdvanceValue', '1') == '1',
    }


def material(asset, placement):
    f, texture_pid = placement.find(asset.guid, TEXTURE_FILE_ID)
    return {
        'm_Name': 'Font Material',
        'm_Shader': {'m_FileID': 0, 'm_PathID': FONT_SHADER[1], 'file': FONT_SHADER[0]},
        'm_ShaderKeywords': '',
        'm_LightmapFlags': 4,
        'm_EnableInstancingVariants': False,
        'm_DoubleSidedGI': False,
        'm_CustomRenderQueue': -1,
        'stringTagMap': [],
        'disabledShaderPasses': [],
        'm_SavedProperties': {
            'm_TexEnvs': [
                (
                    '_MainTex',
                    {
                        'm_Texture': {'m_FileID': 0, 'm_PathID': texture_pid, 'file': f},
                        'm_Scale': {'x': 1.0, 'y': 1.0},
                        'm_Offset': {'x': 0.0, 'y': 0.0},
                    },
                )
            ],
            'm_Floats': [],
            'm_Colors': [('_Color', {'r': 1.0, 'g': 1.0, 'b': 1.0, 'a': 1.0})],
        },
    }


def texture(asset):
    """The empty texture a dynamic font fills at run time."""
    return {
        'm_Name': 'Font Texture',
        'm_ForcedFallbackFormat': 4,
        'm_DownscaleFallback': False,
        'm_Width': 0,
        'm_Height': 0,
        'm_CompleteImageSize': 0,
        'm_TextureFormat': 4,
        'm_MipCount': 1,
        'm_IsReadable': False,
        'm_StreamingMipmaps': False,
        'm_StreamingMipmapsPriority': 0,
        'm_ImageCount': 0,
        'm_TextureDimension': 2,
        'm_TextureSettings': {
            'm_FilterMode': 1,
            'm_Aniso': 1,
            'm_MipBias': 0.0,
            'm_WrapU': 0,
            'm_WrapV': 0,
            'm_WrapW': 0,
        },
        'm_LightmapFormat': 0,
        'm_ColorSpace': 0,
        'image data': b'',
        'm_StreamData': {'offset': 0, 'size': 0, 'path': ''},
    }
