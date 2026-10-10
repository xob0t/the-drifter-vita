"""Where a Unity 2018 batch build spent its time, from its editor log.

    uv run tools/downgrade/import_times.py <build log> [--project <project>] [--top N]

Sums the per-asset import times Unity logs ("Updating <path> ... done. [Time: X ms]") by importer
(read from the asset's .meta when --project is given, otherwise guessed from the extension) and
lists the slowest assets, then the build steps the log times: the asset refresh, Cache Server
downloads, shader compiles, UnityLinker, IL2CPP and the player build. Assets fetched from a cache
server are not imported and have no "Updating" line; their download shows as Cache Server time.
"""

import argparse
import re
from collections import defaultdict
from pathlib import Path

# An asset's import: its "Updating" line, then up to IMPORT_LINES lines of the importer's output, then
# the line with its time.
IMPORT_LINES = 40
UPDATE = re.compile(
    rf'^Updating (.+?) - GUID: [0-9a-f]{{32}}\.\.\.\n(?:(?!Updating ).*\n){{0,{IMPORT_LINES}}}?'
    r'.*?done\. \[Time: ([\d.]+) ms\]',
    re.M,
)
IMPORTERS = {
    '.ogg': 'AudioImporter',
    '.wav': 'AudioImporter',
    '.mp3': 'AudioImporter',
    '.png': 'TextureImporter',
    '.jpg': 'TextureImporter',
    '.tga': 'TextureImporter',
    '.dll': 'PluginImporter',
    '.so': 'PluginImporter',
    '.cs': 'MonoImporter',
    '.shader': 'ShaderImporter',
    '.compute': 'ComputeShaderImporter',
    '.ttf': 'TrueTypeFontImporter',
    '.otf': 'TrueTypeFontImporter',
    '.webm': 'VideoClipImporter',
    '.mp4': 'VideoClipImporter',
    '.unity': 'DefaultImporter (scene)',
    '.txt': 'TextScriptImporter',
    '.bytes': 'TextScriptImporter',
    '.json': 'TextScriptImporter',
    '.hlsl': 'ShaderIncludeImporter',
    '.cginc': 'ShaderIncludeImporter',
}
STEPS = [
    ('Refresh, detecting changes', re.compile(r'^Refresh: elapses ([\d.]+) seconds', re.M), 1),
    ('Refresh, importing', re.compile(r'^Refresh completed in ([\d.]+) seconds', re.M), 1),
    ('Cache Server downloads', re.compile(r'CacheServer download: ([\d.]+)s', re.M), 1),
    ('Cache Server integrate', re.compile(r'CacheServerIntegrate time: ([\d.]+)s', re.M), 1),
    ('Shader compiles (wall, parallel)', re.compile(r"^Compiled shader '.*' in ([\d.]+)s", re.M), 1),
    ('UnityLinker', re.compile(r'UnityLinker\.exe exited after (\d+) ms', re.M), 0.001),
    ('IL2CPP (convert and C++ compile)', re.compile(r'il2cpp\.exe exited after (\d+) ms', re.M), 0.001),
    ('Player build (Build.Player.*)', re.compile(r"^\*\*\* Completed 'Build\.Player\.\w+' in (\d+) seconds", re.M), 1),
]


def importer_of(path: str, project: Path | None = None):
    if project is not None:
        meta = project / (path + '.meta')
        if meta.is_file():
            match = re.search(r'^(\w+Importer):', meta.read_text(encoding='utf-8', errors='replace'), re.M)
            if match:
                return match[1]
    suffix = Path(path).suffix.lower()
    if suffix in IMPORTERS:
        return IMPORTERS[suffix]
    if not suffix or not path.startswith(('Assets/', 'Packages/')):
        return 'folders and other'
    return 'NativeFormatImporter'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('log', type=Path)
    parser.add_argument('--project', type=Path)
    parser.add_argument('--top', type=int, default=15)
    args = parser.parse_args()
    text = args.log.read_text(encoding='utf-8', errors='replace')
    by_importer = defaultdict(lambda: [0, 0.0])
    by_folder = defaultdict(lambda: [0, 0.0])
    assets = []
    for match in UPDATE.finditer(text):
        path, seconds = match[1], float(match[2]) / 1000
        importer = importer_of(path, args.project)
        by_importer[importer][0] += 1
        by_importer[importer][1] += seconds
        folder = by_folder[f'{path.rsplit("/", 1)[0] if "/" in path else path} ({importer})']
        folder[0] += 1
        folder[1] += seconds
        assets.append((seconds, path))
    total = sum(seconds for seconds, _ in assets)
    print(f'{len(assets)} imports, {total:.0f} s ({total / 60:.1f} min) in total')
    print(f'\n{"importer":34} {"assets":>7} {"seconds":>9} {"share":>6} {"ms each":>8}')
    for name, (count, seconds) in sorted(by_importer.items(), key=lambda kv: -kv[1][1]):
        print(f'{name:34} {count:7d} {seconds:9.1f} {seconds / total:6.1%} {1000 * seconds / count:8.1f}')
    print(f'\n{"folder (importer)":60} {"assets":>7} {"seconds":>9}')
    for name, (count, seconds) in sorted(by_folder.items(), key=lambda kv: -kv[1][1])[: args.top]:
        print(f'{name[:60]:60} {count:7d} {seconds:9.1f}')
    print(f'\nslowest {args.top}:')
    for seconds, path in sorted(assets, reverse=True)[: args.top]:
        print(f'{seconds:8.2f} s  {path}')
    print(f'\n{"step":34} {"seconds":>9} {"count":>6}')
    for name, pattern, scale in STEPS:
        values = [float(v) * scale for v in pattern.findall(text)]
        if values:
            print(f'{name:34} {sum(values):9.1f} {len(values):6d}')


if __name__ == '__main__':
    main()
