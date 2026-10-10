"""Project rules for the exported assets and settings: each *_overlay function returns
{path in the project: new content} for pipeline.assemble (see "What the pipeline does" in the docs folder),
except name_overlay and upstream_overlay, which also return the exported paths they move or replace."""

import io
import re
import sys
import tarfile
import zlib
from pathlib import Path

from common import (
    NATIVE_ASSETS,
    SHADER_NAME,
    WORK,
    download,
    folder_meta,
    game_data,
    guid_for,
    log,
    meta_guid,
    meta_of,
    stable_meta,
)


def current_text(exported: Path, overlay: dict, relative: Path):
    """A file's text as the rules before left it: its overlay entry, else the export's file."""
    if relative in overlay:
        return overlay[relative].decode('utf-8')
    return (exported / relative).read_text(encoding='utf-8')


def glyph_overlay(platform: str, diag: bool):
    """The PS Vita's control glyphs (vita_glyphs.py), Start glyph and controls-screen picture
    (vita_controls_art.py), in Resources/VitaGlyphs for Compat Controls: in Vita builds, and in
    diag builds, whose Windows players show them with -vita-emulate."""
    if platform != 'vita' and not diag:
        return {}
    import vita_controls_art
    import vita_glyphs

    overlay = {}
    root = Path('Assets') / 'Resources' / 'VitaGlyphs'

    def add(relative, image, pivot=(0.5, 0.5)):
        target = root / f'{relative}.png'
        png = io.BytesIO()
        image.save(png, format='PNG')
        overlay[target] = png.getvalue()
        alignment = 0 if pivot == (0.5, 0.5) else 9  # 9: a custom pivot
        overlay[meta_of(target)] = GLYPH_META.format(
            guid=guid_for('vita-glyph', relative), alignment=alignment, pivot_x=pivot[0], pivot_y=pivot[1]
        ).encode()

    for name, image in vita_glyphs.glyphs().items():
        add(name, image)
    # The Start glyph ends where the game's PlayStation one does; the picture sits like Ps_0.
    add('VitaStart', vita_glyphs.start_glyph(), vita_glyphs.START_PIVOT)
    add('VitaControls', vita_controls_art.picture(), vita_controls_art.PIVOT)
    overlay.update(folder_meta(root))
    return overlay


# Engine classes a platform's player lacks. Loading a component of such a class leaves
# a hole in its GameObject's component list, and activating the GameObject crashes
# the player (PS Vita: GameObject::ActivateAwakeRecursivelyInternal). Found by
# checking every class serialized in scenes and prefabs against the player's symbols.
PLATFORM_MISSING_CLASSES = {'vita': {328: 'VideoPlayer'}}


def missing_class_overlay(exported: Path, overlay: dict, platform: str):
    classes = PLATFORM_MISSING_CLASSES.get(platform, {})
    if not classes:
        return {}
    header = re.compile(r'^--- !u!(' + '|'.join(map(str, classes)) + r') &(-?\d+)[^\n]*\n', re.M)
    result, removed = {}, 0
    for suffix in ('*.unity', '*.prefab'):
        for path in (exported / 'Assets').rglob(suffix):
            relative = path.relative_to(exported)
            text = current_text(exported, overlay, relative)
            ids = [m[2] for m in header.finditer(text)]
            if not ids:
                continue
            for object_id in ids:
                # The component's document, up to the next one
                text = re.sub(r'^--- !u!\d+ &' + object_id + r'\b[^\n]*\n(?:(?!--- !u!).*\n?)*', '', text, flags=re.M)
                # Its entry in the GameObject's component list (2018 and older list formats)
                text = re.sub(r'^  - (?:component|\d+): \{fileID: ' + object_id + r'\}\n', '', text, flags=re.M)
                # Other references to it in this file become null
                text = text.replace('{fileID: ' + object_id + '}', '{fileID: 0}')
                removed += 1
            result[relative] = text.encode('utf-8')
    log(f'Removed {removed} components of classes the {platform} player lacks ({", ".join(classes.values())})')
    return result


# IL2CPP strips the class libraries down to what code references. Saves
# (BinaryFormatter) also name types the game never mentions, and PowerQuest finds
# scripts and their methods by name, so the game assemblies are kept whole.
LINK_PRESERVE = {
    'mscorlib': [
        'System.Collections.Generic.InternalStringComparer'
    ],  # default string comparer in source-version saves
}
PLATFORM_LINK_PRESERVE = {
    # Diagnostics: free memory for the parity probe; Utility: Compat PowerTicks and MemorySetup.
    'vita': {
        'UnityEngine.PSVitaModule': [
            'UnityEngine.PSVita.Diagnostics',
            'UnityEngine.PSVita.Utility',
            'UnityEngine.PSVita.Utility/PowerTickType',
        ]
    },
}


def link_xml(assemblies: list, platform: str):
    lines = ['<linker>']
    lines += [f'  <assembly fullname="{dll.stem}" preserve="all"/>' for dll in sorted(assemblies)]
    for assembly, types in {**LINK_PRESERVE, **PLATFORM_LINK_PRESERVE.get(platform, {})}.items():
        lines.append(f'  <assembly fullname="{assembly}">')
        lines += [f'    <type fullname="{t}" preserve="all"/>' for t in types]
        lines.append('  </assembly>')
    return '\n'.join([*lines, '</linker>', ''])


# PowerQuest keeps every room's prefab loaded as a data template: it copies the
# hotspot, prop and region data out of it and never instantiates it; rooms render
# from their scenes. In the source version the sprites a template references are
# late-bound to its room's atlas, so no page loads until that room requests it.
# With sprites bound to their pages (atlas_overlay) the templates would keep every
# room's pages resident, so their references to these classes are dropped.
TEMPLATE_STRIPPED = re.compile(r'\{fileID: (?:213\d{5}|7400000|9100000|22100000), guid: [0-9a-f]{32}, type: [23]\}')
#                                     Sprite      AnimationClip AnimatorController AnimatorOverrideController


def template_overlay(exported: Path, overlay: dict):
    lists = re.compile(r'^  m_roomPrefabs:\n((?:  - \{.*\}\n)+)', re.M)
    templates = set()
    for scene in (exported / 'Assets').rglob('*.unity'):
        for block in lists.findall(current_text(exported, overlay, scene.relative_to(exported))):
            templates.update(re.findall(r'guid: ([0-9a-f]{32})', block))
    result, cleared = {}, 0
    for meta in (exported / 'Assets').rglob('*.prefab.meta'):
        if meta_guid(meta) not in templates:
            continue
        prefab = meta.with_suffix('').relative_to(exported)
        text, count = TEMPLATE_STRIPPED.subn('{fileID: 0}', current_text(exported, overlay, prefab))
        if count:
            result[prefab] = text.encode('utf-8')
            cleared += count
    log(f'Cleared {cleared} sprite/animation references in {len(result)} of {len(templates)} template prefabs')
    return result


# Every room scene holds a copy of the PowerQuest object, so the game can start from any
# room in the editor; at run time the copy destroys itself, as the title room's copy is
# already running. Its lists name every room, character, GUI, inventory item and their
# sprites and animations, so loading any room first resolves 32,500 objects (the scene's
# preload table): 3.5-4.6 s per room on the PS Vita before the room's own content. Room
# copies keep their values but lose their asset references. The chapter select
# (Restart(room)) loads a room scene with no PowerQuest running, so the title room's copy
# gets an inactive duplicate as its child, PowerQuestTemplate, which the running copy keeps
# across scenes; a room copy that finds no PowerQuest running instantiates it (Compat
# SharedPowerQuest). (A Resources prefab of the full copy instead moved its 32,500 objects
# out of the title room's own file and made the PS Vita boot 10 s slower.)
# In the source version the chapter select's room runs with its own copy's values, so a room
# copy may differ from the title room's only in values SharedPowerQuest applies to the
# instantiated duplicate: top-level fields of the PowerQuest component written on one line
# (numbers, enums, strings, vectors and colours), not references or nested data. Any other
# difference stops the pipeline (powerquest_differences). SceneRoomCrypt's copy has
# m_dialogTextCharacterInRange {x: 50, y: 15} where the title room's has {x: 45, y: 18}.
POWERQUEST_TEMPLATE_SCENE = 'SceneRoomTitle'
POWERQUEST_TEMPLATE = 'PowerQuestTemplate'
POWERQUEST_LIST_ITEM = re.compile(r'^\s*- \{fileID: -?\d+, guid: [0-9a-f]{32}, type: \d\}$')
POWERQUEST_REFERENCE = re.compile(r'\{fileID: -?\d+, guid: [0-9a-f]{32}, type: \d\}')


def scene_documents(text):
    """{local file ID: (class ID, document)} of a serialized scene or prefab."""
    documents = {}
    for document in re.split(r'(?m)^(?=--- !u!)', text):
        match = re.match(r'--- !u!(\d+) &(-?\d+)', document)
        if match:
            documents[match[2]] = (match[1], document)
    return documents


def powerquest_objects(documents):
    """(GameObject id, its component ids) of the PowerQuest object, or None."""
    quest = [i for i, (cls, doc) in documents.items() if cls == '114' and '\n  m_characterPrefabs:' in doc]
    if not quest:
        return None
    game_object = re.search(r'm_GameObject: \{fileID: (-?\d+)\}', documents[quest[0]][1])[1]
    components = re.findall(r'component: \{fileID: (-?\d+)\}', documents[game_object][1])
    return game_object, components, quest[0]


POWERQUEST_PLAIN_LINE = re.compile(r'^  (\w+): (.*)$')
POWERQUEST_PLAIN_FLOW = re.compile(r'^\{(?:(?!fileID|guid)\w+: -?[0-9.eE+-]+(?:, |\}$))+$')
POWERQUEST_LINKS = re.compile(
    r'^\s*(?:- )?(?:m_GameObject|m_Father|m_Children|m_RootOrder|m_Component|component|'
    r'm_PrefabInternal|m_CorrespondingSourceObject|m_PrefabInstance|m_PrefabAsset|'
    r'm_PrefabParentObject):'
)


def powerquest_components(documents, found):
    """{(class ID, script file ID, whether the PowerQuest component): lines} of the PowerQuest
    object's GameObject and components, without their file IDs and links to each other."""
    game_object, components, _ = found
    result = {}
    for i in [game_object, *components]:
        cls, document = documents[i]
        script = re.search(r'm_Script: \{fileID: (-?\d+)', document)
        key = (cls, script[1] if script else None, i == found[2])
        result[key] = [line for line in document.rstrip('\n').split('\n')[1:] if not POWERQUEST_LINKS.match(line)]
    return result


def powerquest_differences(title, room):
    """Differences of a room's PowerQuest object from the title room's that SharedPowerQuest
    does not reproduce (see above), as text; empty if none."""
    import difflib

    def nested(doc, n):
        return n + 1 < len(doc) and not POWERQUEST_PLAIN_LINE.match(doc[n + 1]) and doc[n + 1].startswith('   ')

    def plain(value):
        return (value != '' and not value.startswith(('[', '{', '|', '>', '&', '*', '!'))) or bool(
            POWERQUEST_PLAIN_FLOW.match(value)
        )

    problems = []
    if set(title) != set(room):
        return [f'components differ: {sorted(set(title) ^ set(room))}']
    for key, lines in room.items():
        original = title[key]
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, original, lines, autojunk=False).get_opcodes():
            if tag == 'equal':
                continue
            # Lines replaced one for one in the PowerQuest component may differ in plain values.
            if not (tag == 'replace' and i2 - i1 == j2 - j1 and key[2]):
                problems.append(f'{tag}: {original[i1:i2]} -> {lines[j1:j2]}')
                continue
            for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
                a, b = POWERQUEST_PLAIN_LINE.match(original[i]), POWERQUEST_PLAIN_LINE.match(lines[j])
                if not (
                    a
                    and b
                    and a[1] == b[1]
                    and plain(a[2])
                    and plain(b[2])
                    and not nested(original, i)
                    and not nested(lines, j)
                ):
                    problems.append(f'{original[i].strip()} -> {lines[j].strip()}')
    return problems


def strip_asset_references(document):
    """Drop references to other files (except the script) from a serialized MonoBehaviour:
    list entries go, emptied lists become [], single references become null."""
    lines, out, list_keys, emptied = document.split('\n'), [], {}, set()
    for line in lines:
        key = re.match(r'^(\s*)(?:- )?(\w+):$', line)
        if key:
            list_keys[len(key[1])] = len(out)
        if POWERQUEST_LIST_ITEM.match(line):
            indent = len(line) - len(line.lstrip())
            if indent in list_keys:
                emptied.add(list_keys[indent])
            continue
        if 'm_Script:' not in line:
            line = POWERQUEST_REFERENCE.sub('{fileID: 0}', line)
        out.append(line)
    for index in emptied:
        following = out[index + 1] if index + 1 < len(out) else ''
        indent = len(out[index]) - len(out[index].lstrip())
        if not (following.startswith(' ' * indent + '- ') or len(following) - len(following.lstrip()) > indent):
            out[index] += ' []'
    return '\n'.join(out)


def powerquest_overlay(exported: Path, overlay: dict):
    scenes = sorted((exported / 'Assets').rglob('*.unity'))
    template = next(s for s in scenes if s.stem == POWERQUEST_TEMPLATE_SCENE)
    relative = template.relative_to(exported)
    text = current_text(exported, overlay, relative)
    documents = scene_documents(text)
    found = powerquest_objects(documents)
    game_object, components, _ = found
    title_components = powerquest_components(documents, found)
    ids = [game_object, *components]
    local = re.compile(r'\{fileID: (-?\d+)\}')
    for i in ids:
        for found in local.findall(documents[i][1]):
            if found != '0' and found not in ids:
                sys.exit(f'{template.name}: the PowerQuest object references scene object {found}')
    transform = next(i for i in components if documents[i][0] == '4')
    if '\n  m_Children: []\n' not in documents[transform][1]:
        sys.exit(f'{template.name}: the PowerQuest object has children')
    # The duplicate: new file IDs above the scene's, inactive, parented to the original.
    base = max(int(i) for i in documents) + 1
    renumber = {i: str(base + n) for n, i in enumerate(ids)}
    duplicate = []
    for i in ids:
        document = re.sub(r'^--- !u!(\d+) &-?\d+', rf'--- !u!\g<1> &{renumber[i]}', documents[i][1], count=1)
        document = local.sub(lambda m: '{fileID: ' + renumber.get(m[1], m[1]) + '}', document)
        if i == game_object:
            document = re.sub(r'^  m_Name: .*$', f'  m_Name: {POWERQUEST_TEMPLATE}', document, count=1, flags=re.M)
            document = re.sub(r'^  m_IsActive: 1$', '  m_IsActive: 0', document, count=1, flags=re.M)
        if i == transform:
            document = document.replace('\n  m_Father: {fileID: 0}\n', f'\n  m_Father: {{fileID: {transform}}}\n', 1)
        duplicate.append(document if document.endswith('\n') else document + '\n')
    text = text.replace(
        documents[transform][1],
        documents[transform][1].replace(
            '\n  m_Children: []\n', f'\n  m_Children:\n  - {{fileID: {renumber[transform]}}}\n', 1
        ),
        1,
    )
    text = (text if text.endswith('\n') else text + '\n') + ''.join(duplicate)
    result = {relative: text.encode('utf-8')}
    stripped = 0
    for scene in scenes:
        if scene == template:
            continue
        relative = scene.relative_to(exported)
        text = current_text(exported, overlay, relative)
        documents = scene_documents(text)
        found = powerquest_objects(documents)
        if found is None:
            continue
        problems = powerquest_differences(title_components, powerquest_components(documents, found))
        if problems:
            sys.exit(
                f"{scene.name}: its PowerQuest copy differs from {template.name}'s in ways the shared copy "
                f'cannot reproduce: ' + '; '.join(problems[:5])
            )
        document = documents[found[2]][1]
        text = text.replace(document, strip_asset_references(document), 1)
        result[relative] = text.encode('utf-8')
        stripped += 1
    log(f'PowerQuest copies without asset references in {stripped} scenes; {POWERQUEST_TEMPLATE} in {template.stem}')
    return result


# RenderTexture.m_ColorFormat is a GraphicsFormat in the source version but a
# RenderTextureFormat in 2018.2; AssetRipper copies the number unchanged.
# GraphicsFormat -> (RenderTextureFormat, sRGB)
RENDER_TEXTURE_FORMATS = {
    4: (0, 1),  # R8G8B8A8_SRGB -> ARGB32
    8: (0, 0),  # R8G8B8A8_UNorm -> ARGB32
    48: (2, 0),  # R16G16B16A16_SFloat -> ARGBHalf
    52: (11, 0),  # R32G32B32A32_SFloat -> ARGBFloat
}


# PowerQuest's pixel camera renders rooms at one texel per world unit into PixelCameraTex
# (orthographic size 540 on 1920x1080), and QuestCamera shows the texture on a quad of
# 1920x1080 units, enlarged with Unlit/TransparentAntiAliased. A room is 180 units high
# and zoom only narrows the view, so the original fills just the middle 320x180 texels.
# Platforms short of memory get a smaller texture, camera and quad at the same scale:
# 640x360 still covers a view zoomed out to half. (Scaling the texture alone renders
# the room at fewer texels than its art: the title logo lost strokes at 1280x720.)
PIXEL_CAMERA = {'vita': (640, 360)}


def render_texture_overlay(exported: Path, platform: str):
    overlay, scaled = {}, []
    for path in (exported / 'Assets').rglob('*.renderTexture'):
        text = path.read_text(encoding='utf-8')
        graphics = int(re.search(r'^  m_ColorFormat: (\d+)$', text, re.M).group(1))
        if graphics not in RENDER_TEXTURE_FORMATS:
            raise RuntimeError(f'{path.name}: no RenderTextureFormat rule for GraphicsFormat {graphics}')
        legacy, srgb = RENDER_TEXTURE_FORMATS[graphics]
        text = re.sub(r'^  m_ColorFormat: \d+$', f'  m_ColorFormat: {legacy}', text, flags=re.M)
        text = re.sub(r'^  m_SRGB: \d+$', f'  m_SRGB: {srgb}', text, flags=re.M)
        if platform in PIXEL_CAMERA and path.stem == 'PixelCameraTex':
            width, height = PIXEL_CAMERA[platform]
            text = re.sub(r'^  m_Width: \d+$', f'  m_Width: {width}', text, flags=re.M)
            text = re.sub(r'^  m_Height: \d+$', f'  m_Height: {height}', text, flags=re.M)
            scaled.append(f'{path.stem} to {width}x{height}')
        overlay[path.relative_to(exported)] = text.encode('utf-8')
    if platform in PIXEL_CAMERA and not scaled:
        sys.exit(f'No PixelCameraTex render texture to scale to {PIXEL_CAMERA[platform]} for {platform}')
    log(
        f'Converted the color format of {len(overlay)} render textures'
        + (f'; scaled {", ".join(scaled)}' if scaled else '')
    )
    return overlay


def pixel_camera_overlay(exported: Path, overlay: dict, platform: str):
    """Matches PixelCamera's orthographic size and its display quad to PixelCameraTex's size."""
    if platform not in PIXEL_CAMERA:
        return {}
    width, height = PIXEL_CAMERA[platform]
    relative = Path('Assets') / 'GameObject' / 'PixelCamera.prefab'
    text = current_text(exported, overlay, relative)
    text, cameras = re.subn(r'^(  orthographic size: )540$', rf'\g<1>{height // 2}', text, flags=re.M)
    text, quads = re.subn(
        r'^(  m_LocalScale: ){x: 1920, y: 1080, z: 1}$', rf'\g<1>{{x: {width}, y: {height}, z: 1}}', text, flags=re.M
    )
    if (cameras, quads) != (1, 1):
        sys.exit(f'{relative}: expected one camera of size 540 and one 1920x1080 quad, found {cameras} and {quads}')
    log(f'PixelCamera renders {width}x{height} units at one texel each')
    return {relative: text.encode('utf-8')}


# Audio import per platform, by the clip's load type (0 decompress on load,
# 1 compressed in memory, 2 streaming) -> (load type, AudioCompressionFormat, quality).
# The PS Vita player's FMOD cannot decode Vorbis inside Unity's FSB5 containers
# ("Unsupported file or audio format"), and its Unity importer encodes every clip as
# HEVAG, the console's ADPCM, whatever format is asked for (MP3 here). Sound effects
# are kept compressed in memory (PCM would be 3.5 times larger). On these platforms no clip
# preloads with its scene: the game's persistent objects reference most of its audio,
# which would otherwise all load at startup in about 105 MB of free RAM.
PLATFORM_AUDIO = {'vita': {0: (1, 5, None), 1: (1, 3, 0.5), 2: (2, 3, 0.7)}}
# Long loops the game keeps in memory (wind, rain, bees, machine hums: 6 clips of 52-113 s,
# 13.5 MB as HEVAG) stream instead, like its music: a clip kept in memory is read whole,
# on the main thread, when it first plays, which is during a room's entry (5.7 MB for
# WindIntTunnel), and then stays resident.
STREAM_SECONDS = 45


def audio_seconds(path: Path):
    """Length of an exported Ogg Vorbis or WAV clip in seconds, or None."""
    data = path.read_bytes()
    if path.suffix == '.ogg':
        header = data.find(b'\x01vorbis')
        last = data.rfind(b'OggS')
        if header < 0 or last < 0:
            return None
        rate = int.from_bytes(data[header + 12 : header + 16], 'little')
        granule = int.from_bytes(data[last + 6 : last + 14], 'little')
        return granule / rate if rate else None
    if path.suffix == '.wav':
        fmt, chunk = data.find(b'fmt '), data.find(b'data')
        if fmt < 0 or chunk < 0:
            return None
        byte_rate = int.from_bytes(data[fmt + 16 : fmt + 20], 'little')
        return int.from_bytes(data[chunk + 4 : chunk + 8], 'little') / byte_rate if byte_rate else None
    return None


def audio_overlay(exported: Path, platform: str):
    formats = PLATFORM_AUDIO.get(platform)
    if not formats:
        return {}
    overlay, streamed = {}, []
    settings = re.compile(
        r'^(  defaultSettings:\n    loadType: )(\d)(\n(?:    .*\n)*?    compressionFormat: )(\d+)'
        r'(\n    quality: )([\d.]+)',
        re.M,
    )
    for meta in (exported / 'Assets').rglob('*.meta'):
        if meta.suffixes[-2:-1] not in (['.ogg'], ['.wav'], ['.mp3']):
            continue
        text = original = meta.read_text(encoding='utf-8')

        def convert(m, meta=meta):
            load_type, compression, quality = formats[int(m[2])]
            if load_type == 1:
                seconds = audio_seconds(meta.with_suffix(''))
                if seconds is not None and seconds >= STREAM_SECONDS:
                    load_type, compression, quality = formats[2]
                    streamed.append(meta.with_suffix('').stem)
            return m[1] + str(load_type) + m[3] + str(compression) + m[5] + (m[6] if quality is None else str(quality))

        text = settings.sub(convert, text, count=1)
        text = re.sub(r'^  preloadAudioData: 1$', '  preloadAudioData: 0', text, flags=re.M)
        if text != original:
            overlay[meta.relative_to(exported)] = text.encode('utf-8')
    log(
        f'{platform} audio import changed for {len(overlay)} clips'
        + (f'; {len(streamed)} long clips stream: {", ".join(sorted(streamed))}' if streamed else '')
    )
    return overlay


# Platforms where a mixer group's reverb fed by sends (the game's RoomVerb, dialog's room
# reverb, which only the Dialog group sends to) is bypassed with the sends to it. A mixer runs
# its effects whenever any sound plays; on the PS Vita that reverb took about 17% of the audio
# mixer's core with no one speaking (LabKitchen: 79% of the core and output underruns with it,
# 62% and none without). See Compat SourceFilters for the voices' own reverbs.
MIXER_SEND_REVERB_OFF = {'vita'}


def mixer_overlay(exported: Path, platform: str):
    """Names of the audio mixers' exposed parameters. A built game keeps only their CRC32;
    AssetRipper writes 7-letter stand-ins with the same CRC32 (Mixer.mixer: "jhqmJNL" for
    reverbSend, "VrjPwqJ" for room, ...), so the game's AudioMixer.SetFloat calls failed
    ("Exposed name does not exist") and RoomComponent.SetReverb never set a room's reverb:
    the RoomVerb group kept the snapshot's room -10000 mB, silent, in every room. Each
    stand-in becomes the string literal of the game's assemblies with the same CRC32.
    On the platforms in MIXER_SEND_REVERB_OFF (the PS Vita) the reverb fed by sends is then
    bypassed with the sends to it (bypass_send_reverbs)."""
    literals = {}
    for dll in (exported / 'Assets' / 'Plugins').glob('*.dll'):
        for match in re.finditer(rb'(?:[\x20-\x7e]\x00){2,64}', dll.read_bytes()):
            name = match[0].decode('utf-16-le')
            literals.setdefault(zlib.crc32(name.encode()), name)
    overlay = {}
    for mixer in (exported / 'Assets').rglob('*.mixer'):
        text = mixer.read_text(encoding='utf-8')
        restored = []

        def rename(m, restored=restored):
            name = literals.get(zlib.crc32(m[2].encode()))
            if name is None or name == m[2]:
                return m[0]
            restored.append(name)
            return m[1] + name

        block = re.search(r'^  m_ExposedParameters:\n(?:  - .*\n    .*\n)*', text, re.M)
        if not block:
            continue
        text = (
            text[: block.start()] + re.sub(r'^(    name: )(\S+)$', rename, block[0], flags=re.M) + text[block.end() :]
        )
        if restored:
            log(f'{mixer.name}: exposed parameters {", ".join(restored)}')
        bypassed = []
        if platform in MIXER_SEND_REVERB_OFF:
            text, bypassed = bypass_send_reverbs(text)
            if bypassed:
                log(f'{mixer.name}: {platform} bypasses {", ".join(bypassed)}')
        if restored or bypassed:
            overlay[mixer.relative_to(exported)] = text.encode('utf-8')
    return overlay


def bypass_send_reverbs(text: str):
    """Bypasses the SFX Reverb of each mixer group with a Receive, and the Sends to it."""
    documents = re.split(r'(?m)^(?=--- !u!)', text)
    ids = {m[1]: i for i, d in enumerate(documents) if (m := re.match(r'--- !u!\d+ &(\d+)', d))}
    receives = {m[1] for d in documents if 'm_EffectName: Receive' in d and (m := re.match(r'--- !u!\d+ &(\d+)', d))}
    groups = []
    for d in documents:
        effects = (
            re.findall(r'^  - \{fileID: (\d+)\}$', d.split('m_Effects:', 1)[1], re.M)
            if 'AudioMixerGroupController:' in d
            else []
        )
        if receives & set(effects):
            name = re.search(r'^  m_Name: (.*)$', d, re.M)[1]
            for effect in effects:
                if 'm_EffectName: SFX Reverb' in documents[ids[effect]]:
                    documents[ids[effect]] = documents[ids[effect]].replace('  m_Bypass: 0', '  m_Bypass: 1')
                    groups.append(name)
    for i, d in enumerate(documents):
        target = re.search(r'm_EffectName: Send\n(?:.*\n)*?  m_SendTarget: \{fileID: (\d+)\}', d)
        if target and target[1] in receives:
            documents[i] = d.replace('  m_Bypass: 0', '  m_Bypass: 1')
    return ''.join(documents), [f"{g}'s reverb" for g in groups]


# TextureFormat values that are block-compressed; everything else in this game
# (RGBA32, RGB24, Alpha8) ships uncompressed.
COMPRESSED_TEXTURE_FORMATS = {10, 12, 24, 25, 26, 27, 28, 29, 34, 45, 46, 47, 48, 49, 50}


# Per-platform texture formats (TextureImporterFormat) for textures the game ships
# uncompressed, as (with alpha, without). The PS Vita has 512 MB shared by the CPU
# and GPU; its GPU samples DXT natively, at a quarter (DXT5) or an eighth (DXT1) of
# RGBA32's size. "Without" is decided from the exported image's own pixels: texture
# names are not unique (the game has 39 different Shadow_0 textures, some RGB24 and
# some RGBA32 character shadow masks that the shadow shader alpha-tests).
PLATFORM_TEXTURE_FORMATS = {'vita': ('PSP2', 12, 10)}


def fully_opaque(image: Path):
    from PIL import Image

    with Image.open(image) as picture:
        if 'A' not in picture.getbands() and 'transparency' not in picture.info:
            return True
        return picture.convert('RGBA').getchannel('A').getextrema()[0] == 255


def max_texture_size(largest):
    """The smallest power of two from 2048 up that holds a texture's largest side, so the import
    never downscales it."""
    return max(2048, 1 << (largest - 1).bit_length())


def texture_overlay(exported: Path, game: Path, platform: str):
    """Import textures the way the game shipped them.

    AssetRipper writes high-quality compression into every texture importer, so
    Unity block-compresses pixel art the game stores uncompressed, softening hard
    alpha edges and shifting colours. Use uncompressed import unless the original
    texture was compressed, and a max size that never downscales it."""
    import UnityPy

    data = game_data(game)
    compressed, largest = set(), {}
    for path in sorted(data.glob('*.assets')):
        for obj in UnityPy.load(str(path)).objects:
            if obj.type.name != 'Texture2D':
                continue
            tree = obj.read_typetree()
            if tree['m_TextureFormat'] in COMPRESSED_TEXTURE_FORMATS:
                compressed.add(tree['m_Name'])
            largest[tree['m_Name']] = max(largest.get(tree['m_Name'], 0), tree['m_Width'], tree['m_Height'])
    overlay, kept = {}, 0
    for meta in (exported / 'Assets').rglob('*.png.meta'):
        text = meta.read_text(encoding='utf-8')
        if 'TextureImporter:' not in text:
            continue
        stem = meta.name[: -len('.png.meta')]
        name = stem if stem in largest else re.sub(r'_\d+$', '', stem)
        if name in compressed:
            kept += 1
            continue
        size = max_texture_size(largest.get(name, 0))
        text = re.sub(r'^(\s*)textureCompression: \d+$', r'\g<1>textureCompression: 0', text, flags=re.M)
        text = re.sub(r'^(\s*)maxTextureSize: \d+$', rf'\g<1>maxTextureSize: {size}', text, flags=re.M)
        if platform in PLATFORM_TEXTURE_FORMATS:
            target, with_alpha, without_alpha = PLATFORM_TEXTURE_FORMATS[platform]
            text = text.replace(
                '  platformSettings:\n',
                '  platformSettings:\n'
                + TEXTURE_PLATFORM_ENTRY.format(
                    target=target, size=size, format=without_alpha if fully_opaque(meta.with_suffix('')) else with_alpha
                ),
                1,
            )
        overlay[meta.relative_to(exported)] = text.encode('utf-8')
    log(f'Uncompressed import for {len(overlay)} textures; {kept} stay compressed like the original')
    return overlay


# Sprites drawn by the hundred that the game keeps in separate textures, packed into one
# texture each so the sprites batch: atlas file -> (size, [(texture and sprite name,
# (x, y) of its texels in the atlas, from the bottom left)]).
# - Rain (Rain/RainDrop): about 600 drops in the cemetery rooms, each the falling Drop or,
#   once landed, one of four RainSplash sprites at random. Their sorting order follows
#   their depth, so neighbours in draw order mostly have different textures, and every
#   drop was its own draw call (CemeteryGrave on the PS Vita: 7.5 ms of the pixel camera's
#   9.4 ms). With one texture the drops batch.
# Every sprite keeps its texels: full-rect sprites of whole textures, point filtered,
# clamped, without mipmaps (checked), each copied whole at a position that is a multiple of
# 4, so the PS Vita's DXT5 blocks hold the same 16 texels as in the sprite's own texture.
# Around each sprite the atlas repeats its edge texels (in blocks of their own), so a read
# just outside its rect returns what clamping returned. Drop fills the atlas's height, so
# its v coordinates are the original ones, and the atlas is twice its width, so its u
# coordinates are halved exactly.
PACKED_SPRITES = {
    'DrifterRainAtlas': (
        (64, 32),
        [
            ('Drop', (0, 0)),
            ('RainSplash1', (40, 4)),
            ('RainSplash2', (52, 4)),
            ('RainSplash3', (40, 20)),
            ('RainSplash4', (52, 20)),
        ],
    ),
}
# The settingsRaw those sprites have in the export: not in an atlas and not rotated (bits 0-5
# clear); 64 is the mesh type bit.
UNPACKED_SPRITE_SETTINGS = 64


def packed_sprite_overlay(exported: Path, overlay: dict):
    """Pack the sprites of PACKED_SPRITES into one texture each and point the sprites'
    render data at it (texture, textureRect, uvTransform). Runs after texture_overlay,
    whose import settings (and platform format) the atlas takes from its first texture."""
    import numpy as np
    from PIL import Image

    result, packed = {}, 0
    for atlas, ((width, height), entries) in PACKED_SPRITES.items():
        pixels = np.zeros((height, width, 4), np.uint8)  # rows from the top, as in the PNG
        used = np.zeros((height, width), bool)
        sprites, meta_text = {}, None
        guid = guid_for('packed-sprites', atlas)
        for name, (x, y) in entries:
            texture = Path('Assets') / 'Texture2D' / f'{name}.png'
            sprite = Path('Assets') / 'Sprite' / f'{name}.asset'
            meta = current_text(exported, overlay, meta_of(texture))
            settings = {
                k: re.search(rf'^\s*{k}: (-?\d+)$', meta, re.M)
                for k in ('filterMode', 'wrapU', 'wrapV', 'enableMipMap')
            }
            if any(v is None for v in settings.values()) or [int(v[1]) for v in settings.values()] != [0, 1, 1, 0]:
                sys.exit(f'{atlas}: {name} is not a point-filtered, clamped texture without mipmaps')
            with Image.open(exported / texture) as picture:
                image = np.asarray(picture.convert('RGBA'))
            h, w = image.shape[:2]
            text = current_text(exported, overlay, sprite)
            rd = text.split('\n  m_RD:\n', 1)[1].split('\n  m_AtlasRD:\n', 1)[0]
            rect = dict(
                re.findall(r'^      (x|y|width|height): (\S+)$', rd.split('    textureRect:\n', 1)[1], re.M)[:4]
            )
            raw = int(re.search(r'^    settingsRaw: (\d+)$', rd, re.M)[1])
            uv = re.search(r'^    uvTransform: \{x: (\S+), y: (\S+), z: (\S+), w: (\S+)\}$', rd, re.M)
            if (
                [float(rect[k]) for k in ('x', 'y', 'width', 'height')] != [0, 0, w, h]
                or raw != UNPACKED_SPRITE_SETTINGS
                or uv is None
                or x % 4
                or y % 4
                or x + w > width
                or y + h > height
            ):
                sys.exit(f'{atlas}: {name} is not a full-rect sprite of its whole texture that fits at ({x}, {y})')
            top = height - y - h
            # The texels and, around them, their edge repeated (a clamped read), in rows from the top.
            padded = np.pad(image, ((1, 1), (1, 1), (0, 0)), mode='edge')
            region = (slice(max(top - 1, 0), min(top + h + 1, height)), slice(max(x - 1, 0), min(x + w + 1, width)))
            inner = (slice(top, top + h), slice(x, x + w))
            if used[region].any():
                sys.exit(f'{atlas}: {name} overlaps another sprite')
            pixels[region] = padded[
                region[0].start - top + 1 : region[0].stop - top + 1, region[1].start - x + 1 : region[1].stop - x + 1
            ]
            used[region] = True
            assert (pixels[inner] == image).all()
            rd_new = re.sub(
                r'^    texture: \{fileID: 2800000, guid: [0-9a-f]{32}, type: 3\}$',
                f'    texture: {{fileID: 2800000, guid: {guid}, type: 3}}',
                rd,
                count=1,
                flags=re.M,
            )
            rd_new = re.sub(
                r'^(    textureRect:\n      serializedVersion: 2\n)      x: \S+\n      y: \S+\n',
                rf'\g<1>      x: {x}\n      y: {y}\n',
                rd_new,
                count=1,
                flags=re.M,
            )
            ux, uy, uz, uw = (float(v) for v in uv.groups())
            rd_new = rd_new.replace(
                uv[0], f'    uvTransform: {{x: {ux:.9g}, y: {uy + x:.9g}, z: {uz:.9g}, w: {uw + y:.9g}}}'
            )
            if rd_new.count(guid) != 1 or rd_new == rd:
                sys.exit(f'{atlas}: unexpected render data in {name}')
            sprites[sprite] = text.replace(rd, rd_new, 1)
            if meta_text is None:
                meta_text = meta
        png = io.BytesIO()
        Image.fromarray(pixels, 'RGBA').save(png, format='PNG')
        target = Path('Assets') / 'Texture2D' / f'{atlas}.png'
        # The first texture's importer (sprite texture settings, platform format), as a
        # texture with no sprites of its own, like the game's atlas pages.
        meta_text = re.sub(r'^guid: [0-9a-f]{32}$', f'guid: {guid}', meta_text, count=1, flags=re.M)
        meta_text = re.sub(r'^timeCreated: .*\n', '', meta_text, flags=re.M)
        meta_text = re.sub(r'^  spriteMode: \d+$', '  spriteMode: 2', meta_text, count=1, flags=re.M)
        meta_text = re.sub(r'^  alignment: \d+$', '  alignment: 0', meta_text, count=1, flags=re.M)
        meta_text = re.sub(r'^  spritePivot: .*$', '  spritePivot: {x: 0.5, y: 0.5}', meta_text, count=1, flags=re.M)
        result[target] = png.getvalue()
        result[meta_of(target)] = meta_text.encode('utf-8')
        for sprite, text in sprites.items():
            result[sprite] = text.encode('utf-8')
        packed += len(sprites)
    log(f'Packed {packed} sprites into {len(PACKED_SPRITES)} textures')
    return result


# A texture importer's platformSettings entry for one build target (PLATFORM_TEXTURE_FORMATS).
TEXTURE_PLATFORM_ENTRY = """  - serializedVersion: 2
    buildTarget: {target}
    maxTextureSize: {size}
    resizeAlgorithm: 0
    textureFormat: {format}
    textureCompression: 1
    compressionQuality: 100
    crunchedCompression: 0
    allowsAlphaSplitting: 0
    overridden: 1
    androidETC2FallbackOverride: 0
"""


IMPORTED_BINARY = {'.png', '.ogg', '.wav', '.mp3', '.ttf', '.otf', '.mp4', '.webm', '.txt', '.bytes', '.json'}
# A native asset's m_Name is in its first lines; reading this much of each file finds it.
NAME_SEARCH_BYTES = 4000


def name_overlay(exported: Path, game: Path, overlay: dict):
    """Restore original object names that AssetRipper had to change for unique file names.

    Unity names an asset's main object after its file, so AssetRipper's "Name_1.anim"
    becomes an object called "Name_1" and the game's lookups by name fail. Move each
    such asset to "<stem>/<original name><ext>" with its .meta, so the GUID is kept.
    Resources paths are left alone; they are runtime load paths. Returns
    (moved overlay entries, excluded original paths); overlay entries already
    generated for a moved path are moved with it."""
    import UnityPy

    data = game_data(game)
    original = set()
    for path in [*sorted(data.glob('*.assets')), *sorted(data.glob('level*'))]:
        for obj in UnityPy.load(str(path)).objects:
            if obj.type.name in ('Texture2D', 'AudioClip', 'Font', 'VideoClip', 'TextAsset'):
                original.add(obj.peek_name())
    moved, excluded, skipped = {}, set(), 0
    assets = exported / 'Assets'
    for path in assets.rglob('*'):
        if path.suffix not in NATIVE_ASSETS | IMPORTED_BINARY or 'Resources' in path.relative_to(assets).parts:
            continue
        name = None
        if path.suffix in NATIVE_ASSETS:
            with path.open(encoding='utf-8', errors='replace') as handle:
                match = re.search(r'^  m_Name: (.*)$', handle.read(NAME_SEARCH_BYTES), re.M)
            if match and match.group(1) != path.stem:
                name = match.group(1)
        elif path.stem not in original:
            match = re.match(r'^(.*)_\d+$', path.stem)
            if match and match.group(1) in original:
                name = match.group(1)
        if name is None:
            continue
        if not name or re.search(r'[<>:"/\\|?*]', name) or name.rstrip('. ') != name:
            skipped += 1
            continue
        relative = path.relative_to(exported)
        meta = meta_of(relative)
        target = relative.parent / path.stem / (name + path.suffix)
        moved[target] = overlay.pop(relative, None) or path.read_bytes()
        moved[meta_of(target)] = overlay.pop(meta, None) or (exported / meta).read_bytes()
        moved.update(folder_meta(target.parent))
        excluded.update({relative, meta})
    log(
        f'Restored original names for {len(excluded) // 2} assets'
        + (f'; {skipped} names cannot be file names' if skipped else '')
    )
    return moved, excluded


def atlas_render_data(game: Path):
    """The game's SpriteAtlas render data: ({(render data key, second): (render data, page)}, {page: its
    texture's name}), a page being the (path ID, file name) of its texture."""
    import UnityPy

    data = game_data(game)
    pages, render = {}, {}
    for path in [data / 'resources.assets', *sorted(data.glob('sharedassets*.assets'))]:
        env = UnityPy.load(str(path))
        textures = {o.path_id: o for o in env.objects if o.type.name == 'Texture2D'}
        for obj in env.objects:
            if obj.type.name != 'SpriteAtlas':
                continue
            for (guid, second), value in obj.read_typetree()['m_RenderDataMap']:
                # The key as a sprite's m_RenderDataKey writes it: each 32-bit word low nibble first.
                key = ''.join(f'{guid[f"data[{i}]"] & 0xFFFFFFFF:08x}'[::-1] for i in range(4))
                texture = value['texture']
                if texture['m_FileID'] != 0:
                    continue
                page = (texture['m_PathID'], path.name)
                if page not in pages:
                    pages[page] = textures[texture['m_PathID']].read().m_Name
                render[key, second] = (value, page)
    return render, pages


def bind_sprite(text, value, page_guid):
    """A sprite asset with atlas render data (value) written into its own m_RD, its texture the
    page with page_guid, and no atlas tags."""

    def number(v):
        return f'{v:.9g}'

    head, rest = text.split('\n  m_RD:\n', 1)
    rd, tail = rest.split('\n  m_AtlasRD:\n', 1)
    rect, off, atlas_off, uv = (
        value['textureRect'],
        value['textureRectOffset'],
        value['atlasRectOffset'],
        value['uvTransform'],
    )
    # Sprites packed flipped (rotation 1 horizontal, 2 vertical, 3 both) mirror around
    # their rect. 2018 applies textureRectOffset on a flipped axis once more than the
    # source version, shifting UVs by 2 * offset; zero it on flipped axes.
    rotation = (value['settingsRaw'] >> 2) & 0xF
    off = {'x': 0 if rotation in (1, 3) else off['x'], 'y': 0 if rotation in (2, 3) else off['y']}
    replacements = [
        (r'^    texture: \{fileID: 0\}$', f'    texture: {{fileID: 2800000, guid: {page_guid}, type: 3}}'),
        (
            r'^    textureRect:\n      serializedVersion: 2\n(      \w+: .*\n){4}',
            '    textureRect:\n      serializedVersion: 2\n'
            + ''.join(f'      {k}: {number(rect[k])}\n' for k in ('x', 'y', 'width', 'height')),
        ),
        (r'^    textureRectOffset: .*$', f'    textureRectOffset: {{x: {number(off["x"])}, y: {number(off["y"])}}}'),
        (
            r'^    atlasRectOffset: .*$',
            f'    atlasRectOffset: {{x: {number(atlas_off["x"])}, y: {number(atlas_off["y"])}}}',
        ),
        (r'^    settingsRaw: .*$', f'    settingsRaw: {value["settingsRaw"]}'),
        (r'^    uvTransform: .*$', '    uvTransform: {' + ', '.join(f'{k}: {number(uv[k])}' for k in 'xyzw') + '}'),
        (r'^    downscaleMultiplier: .*$', f'    downscaleMultiplier: {number(value["downscaleMultiplier"])}'),
    ]
    for pattern, replacement in replacements:
        rd = re.sub(pattern, replacement, rd, flags=re.M)
    head = re.sub(r'^  m_AtlasTags:\n(  - .*\n)+', '  m_AtlasTags: []\n', head + '\n', flags=re.M).rstrip('\n')
    return head + '\n  m_RD:\n' + rd + '\n  m_AtlasRD:\n' + tail


def atlas_overlay(exported: Path, game: Path):
    """Bind late-binding atlas sprites directly to their atlas pages.

    The game ships sprites packed into SpriteAtlases that are loaded on request
    (Resources.Load in PowerQuest). AssetRipper exports the pages and the sprites
    but not the atlases, so those sprites have no texture. Write each sprite's
    atlas render data into its own m_RD and drop the atlas tags."""
    render, pages = atlas_render_data(game)
    page_guids = {}
    for page, name in pages.items():
        meta = exported / 'Assets' / 'Texture2D' / f'{name}.png.meta'
        if meta.exists():
            page_guids[page] = meta_guid(meta)

    overlay, bound, unbound = {}, 0, 0
    for path in (exported / 'Assets' / 'Sprite').glob('*.asset'):
        text = path.read_text(encoding='utf-8')
        if not re.search(r'^  m_AtlasTags:\n  - ', text, re.M):
            continue
        match = re.search(r'^  m_RenderDataKey:\n    ([0-9a-f]{32}): (-?\d+)', text, re.M)
        entry = render.get((match.group(1), int(match.group(2)))) if match else None
        if entry is None or entry[1] not in page_guids:
            unbound += 1
            continue
        value, page = entry
        overlay[path.relative_to(exported)] = bind_sprite(text, value, page_guids[page]).encode('utf-8')
        bound += 1
    log(f'Bound {bound} atlas sprites to their pages' + (f'; {unbound} without atlas data' if unbound else ''))
    return overlay


# Open-source packages compiled into the game. Their shaders come from upstream
# source instead of decompilation. The version was matched against the game's
# Unity.Postprocessing.Runtime.dll by comparing declared identifiers per release.
# (package, version, the package's SHA-1 from the registry, shader folder, its subfolders taken).
UPSTREAM_SHADERS = [
    (
        'com.unity.postprocessing',
        '3.1.1',
        '7f17893740f98a0943b2ff9c978f1de79fc3ba34',
        'PostProcessing/Shaders',
        ('Builtins', 'Debug', 'API'),
    ),
]


def upstream_overlay(exported: Path):
    """Place upstream shader sources under Assets/Upstream, each taking over the GUID of
    the exported shader with the same name. Returns (overlay, replaced exported paths)."""
    exported_shaders, exported_compute = {}, {}
    for path in (exported / 'Assets').rglob('*.shader'):
        match = SHADER_NAME.match(path.read_text(encoding='utf-8', errors='replace'))
        if match:
            exported_shaders[match.group(1)] = path
    for path in (exported / 'Assets' / 'ComputeShader').glob('*.asset'):
        exported_compute[path.stem] = path

    overlay, replaced = {}, set()
    for package, version, sha1, shader_dir, folders in UPSTREAM_SHADERS:
        name = f'{package}-{version}.tgz'
        tarball = download(f'https://download.packages.unity.com/{package}/-/{name}', WORK / 'upstream' / name, sha1)
        root = Path('Assets') / 'Upstream' / package
        prefix = f'package/{shader_dir}/'
        taken = 0
        with tarfile.open(tarball) as archive:
            for member in archive.getmembers():
                if not member.isfile() or not member.name.startswith(prefix) or member.name.endswith('.meta'):
                    continue
                relative = member.name[len(prefix) :]
                parts = relative.split('/')
                if len(parts) > 1 and parts[0] not in folders:
                    continue
                content = archive.extractfile(member).read()
                # Package-absolute includes point at the package's new home in Assets.
                content = content.replace(f'Packages/{package}/{shader_dir}/'.encode(), f'{root.as_posix()}/'.encode())
                target = root / relative
                original = None
                if target.suffix == '.shader':
                    match = SHADER_NAME.match(content.decode('utf-8', errors='replace'))
                    original = exported_shaders.get(match.group(1)) if match else None
                elif target.suffix == '.compute':
                    original = exported_compute.get(target.stem)
                if original is not None:
                    relative_original = original.relative_to(exported)
                    replaced.update({relative_original, meta_of(relative_original)})
                    guid = meta_guid(meta_of(original))
                    importer = 'ShaderImporter' if target.suffix == '.shader' else 'ComputeShaderImporter'
                    meta_text = f'fileFormatVersion: 2\nguid: {guid}\n{importer}:\n  userData:\n'
                    taken += 1
                else:
                    meta_text = stable_meta('upstream', f'{package}/{relative}', 'DefaultImporter')
                overlay[target] = content
                overlay[meta_of(target)] = meta_text.encode()
        for folder in {p.parent for p in overlay if p.suffix != '.meta'} | {root, root.parent}:
            for parent in [folder, *folder.parents]:
                if parent in (Path('Assets'), Path('.')):
                    break
                for meta, content in folder_meta(parent).items():
                    overlay.setdefault(meta, content)
        log(f'Upstream {package} {version}: {taken} shaders replace exported ones')
    return overlay, replaced


NATIVE_PLUGIN_META = """fileFormatVersion: 2
guid: {guid}
PluginImporter:
  serializedVersion: 2
  iconMap: {{}}
  executionOrder: {{}}
  isPreloaded: 0
  isOverridable: 0
  platformData:
  - first:
      Any:
    second:
      enabled: 0
      settings: {{}}
  - first:
      Editor: Editor
    second:
      enabled: 1
      settings:
        CPU: x86_64
        OS: Windows
  - first:
      Standalone: Win64
    second:
      enabled: 1
      settings:
        CPU: AnyCPU
  userData:
  assetBundleName:
  assetBundleVariant:
"""


def native_plugin_overlay(game: Path):
    """AssetRipper exports managed code only; carry the game's 64-bit native plugins over."""
    source = game_data(game) / 'Plugins' / 'x86_64'
    folder = Path('Assets') / 'Plugins' / 'x86_64'
    overlay = folder_meta(folder)
    for dll in sorted(source.glob('*.dll')):
        overlay[folder / dll.name] = dll.read_bytes()
        overlay[meta_of(folder / dll.name)] = NATIVE_PLUGIN_META.format(guid=guid_for('native', dll.name)).encode()
    log('Native plugins: ' + ', '.join(p.name for p in overlay if p.suffix == '.dll'))
    return overlay


# Player settings whose 2018 defaults differ from the source version's behaviour.
SETTING_RULES = {
    # Unity 2019.3 removed the launch-time resolution dialog; 2018 shows it unless disabled.
    'displayResolutionDialog': '0',
}

# Per platform. The PS Vita player starts in power mode A, with the GPU at 111 MHz
# (vitacompanion's perf): the title room took about 40 ms of rendering a frame whichever
# camera was turned off, with no CPU core busy. Mode C (2) raises the GPU clock and gives
# up the camera and full screen brightness; mode B would also turn off Wi-Fi. Setting
# PlayerSettings.PSVita.powerMode in the build script did not reach the built player.
PLATFORM_SETTING_RULES = {'vita': {'psp2PowerMode': '2'}}

# Joysticks a platform's player can report (ProjectSettings/InputManager.asset). InControl's
# setup gives the input manager an axis "joystick N analog M" for joysticks 1-10 and analogs
# 0-19 (200 of its 221 axes). The engine updates every axis each frame and Input.GetAxisRaw
# compares the name with every axis on each call (12 us a call on the PS Vita, where InControl
# reads 8 a frame). The PS Vita player reports four joysticks (the Vita's controls and three
# slots, empty on a Vita), and InControl reads only the axes of joysticks the player reports, so
# there the axes of joysticks 5-10, which no joystick feeds and nothing reads, are left out.
PLATFORM_INPUT_JOYSTICKS = {'vita': 4}
INCONTROL_JOYSTICKS, INCONTROL_ANALOGS = 10, 20
JOYSTICK_AXIS = re.compile(
    r'^  - serializedVersion: \d+\n    m_Name: joystick (\d+) analog \d+\n(?:    .*\n)*?    joyNum: (\d+)\n', re.M
)


# Splash images per platform: the key art from livearea.py (pic0 in full colour), imported as a texture.
PLATFORM_SPLASH = {'vita': ('psp2Splashimage', 'splash.png')}
SPLASH_ASSET = Path('Assets') / 'Downgrade' / 'Splash.png'


def settings_overlay(exported: Path, platform: str):
    relative = Path('ProjectSettings') / 'ProjectSettings.asset'
    text = (exported / relative).read_text(encoding='utf-8')
    rules = {**SETTING_RULES, **PLATFORM_SETTING_RULES.get(platform, {})}
    overlay = {}
    if platform in PLATFORM_SPLASH:
        import livearea

        key, image = PLATFORM_SPLASH[platform]
        guid = guid_for('splash', platform)
        overlay.update(folder_meta(SPLASH_ASSET.parent))
        overlay[SPLASH_ASSET] = (livearea.generate() / image).read_bytes()
        overlay[meta_of(SPLASH_ASSET)] = SPLASH_META.format(guid=guid).encode()
        rules[key] = f'{{fileID: 2800000, guid: {guid}, type: 3}}'
    for key, value in rules.items():
        text, count = re.subn(rf'^(  {key}:).*$', rf'\g<1> {value}', text, flags=re.M)
        if count == 0:
            # Keys the source version no longer has; the file holds one PlayerSettings mapping.
            text = text.rstrip('\n') + f'\n  {key}: {value}\n'
        elif count > 1:
            raise RuntimeError(f'ProjectSettings has {count} entries for {key}')
    overlay[relative] = text.encode('utf-8')
    if platform in PLATFORM_INPUT_JOYSTICKS:
        limit = PLATFORM_INPUT_JOYSTICKS[platform]
        inputs = Path('ProjectSettings') / 'InputManager.asset'
        text = (exported / inputs).read_text(encoding='utf-8')
        dropped = [0]

        def drop(match):
            if int(match.group(1)) > limit and match.group(2) == match.group(1):
                dropped[0] += 1
                return ''
            return match.group(0)

        text = JOYSTICK_AXIS.sub(drop, text)
        expected = INCONTROL_ANALOGS * (INCONTROL_JOYSTICKS - limit)
        if dropped[0] != expected:
            raise RuntimeError(
                f'InputManager: expected {expected} joystick axes above joystick {limit}, found {dropped[0]}'
            )
        overlay[inputs] = text.encode('utf-8')
    return overlay


SPLASH_META = """fileFormatVersion: 2
guid: {guid}
TextureImporter:
  serializedVersion: 6
  mipmaps:
    enableMipMap: 0
  textureFormat: 1
  maxTextureSize: 1024
  textureSettings:
    serializedVersion: 2
    filterMode: 0
    wrapU: 1
    wrapV: 1
  nPOTScale: 0
  textureType: 0
  textureShape: 1
  alphaUsage: 0
  platformSettings:
  - serializedVersion: 2
    buildTarget: DefaultTexturePlatform
    maxTextureSize: 1024
    textureFormat: -1
    textureCompression: 0
    overridden: 0
  userData:
"""


# The PS Vita's control glyphs and controls picture: sprites like the game's own (L1_0, Ps_0),
# one unit per pixel, point filtered, uncompressed; alignment 9 is a custom pivot.
GLYPH_META = """fileFormatVersion: 2
guid: {guid}
TextureImporter:
  serializedVersion: 6
  mipmaps:
    enableMipMap: 0
    sRGBTexture: 1
  isReadable: 0
  textureFormat: 4
  maxTextureSize: 2048
  textureSettings:
    serializedVersion: 2
    filterMode: 0
    aniso: 1
    mipBias: 0
    wrapU: 1
    wrapV: 1
    wrapW: 1
  nPOTScale: 0
  spriteMode: 1
  spriteExtrude: 1
  spriteMeshType: 0
  alignment: {alignment}
  spritePivot: {{x: {pivot_x}, y: {pivot_y}}}
  spritePixelsToUnits: 1
  alphaUsage: 1
  alphaIsTransparency: 1
  textureType: 8
  textureShape: 1
  platformSettings:
  - serializedVersion: 2
    buildTarget: DefaultTexturePlatform
    maxTextureSize: 2048
    textureFormat: -1
    textureCompression: 0
    overridden: 0
  spritePackingTag:
  userData:
"""
