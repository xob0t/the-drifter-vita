"""Lay out the PS Vita build folder around the data and code the other modules write.

    uv run tools/pure/package.py [--out dir] [--compare]

What Unity's build copies or generates besides the serialized files and the IL2CPP output:
the player (`<product>.self`, Unity's stock PSP2Player_Il2cpp.self, the app's eboot), the system
modules (the player's sce_module, with libc and libfios2 from the SDK), sce_sys (the player's
LiveArea placeholders and a param.sfo made from PlayerSettings), configuration.psp2path and the
host launcher .bat (PC Hosted runs), Media/Modules/pthread.suprx, Media/Resources/unity default
resources, Media/boot.config and Media/platform_native_link.xml.
"""

import argparse
import shutil
import struct
import sys

import paths
import unityyaml

PLATFORM_NATIVE_LINK = """<linker>
\t<assembly fullname="UnityEngine">

\t\t<!-- Events that come from the OS will trigger callbacks on these objects -->
\t\t<!-- Games should be acting on those events, but we make sure they are not -->
\t\t<!-- stripped so you don't have to worry about them for quick prototypes -->
\t\t<!--type fullname="UnityEngine.PSP2.Input" /-->
\t</assembly>
\t
\t<assembly fullname="mscorlib">
\t\t<type fullname="System.Exception" />
\t</assembly>
</linker>
"""
BOOT_CONFIG = (
    'gfx-enable-native-gfx-jobs=\nwait-for-native-debugger=0\nscripting-runtime-version=latest\nvr-enabled=0\n'
)
SDK_MODULES = ('libc.suprx', 'libfios2.suprx')


def player_settings():
    return unityyaml.meta(paths.PROJECT / 'ProjectSettings' / 'ProjectSettings.asset')['PlayerSettings']


def sfo(entries):
    """A param.sfo from (key, value, max length) entries: strings are UTF-8 with a terminator,
    padded to their max length; ints are 32-bit."""
    entries = sorted(entries, key=lambda e: e[0])
    keys, data, table = b'', b'', b''
    for key, value, size in entries:
        if isinstance(value, int):
            raw, fmt, length = struct.pack('<I', value), 0x0404, 4
        else:
            raw = value.encode() + b'\0'
            fmt, length = 0x0204, len(raw)
            raw = raw.ljust(size, b'\0')
        table += struct.pack('<HHIII', len(keys), fmt, length, size, len(data))
        keys += key.encode() + b'\0'
        data += raw
    keys = keys.ljust((len(keys) + 3) // 4 * 4, b'\0')
    key_offset = 20 + len(table)
    header = struct.pack('<4sIIII', b'\0PSF', 0x0101, key_offset, key_offset + len(keys), len(entries))
    return header + table + keys + data


def param_sfo(settings):
    content_id = settings['psp2ContentID']
    return sfo(
        [
            ('APP_VER', settings['psp2AppVersion'], 8),
            ('ATTRIBUTE', 0, 4),
            # memory expansion: 0 none, then +29, +77 and +109 MB in bits 2-3
            ('ATTRIBUTE2', int(settings['psp2MemoryExpansionMode']) * 4, 4),
            # the enter button: 2 is cross
            ('ATTRIBUTE_MINOR', 2 if settings['psp2EnterButtonAssignment'] == '2' else 0, 4),
            ('CATEGORY', 'gp' if settings['psp2Category'] == '1' else 'gd', 4),
            ('CONTENT_ID', content_id, 48),
            ('GC_RO_SIZE', 0, 4),
            ('GC_RW_SIZE', 0, 4),
            ('PARENTAL_LEVEL', int(settings['psp2ParentalLevel']), 4),
            ('PSP2_DISP_VER', '00.000', 8),
            ('PSP2_SYSTEM_VER', 0, 4),
            ('REGION_DENY', 0, 4),
            ('SAVEDATA_MAX_SIZE', int(settings['psp2SaveDataQuota']), 4),
            ('STITLE', settings['psp2ShortTitle'], 52),
            ('TITLE', settings['productName'], 128),
            ('TITLE_ID', content_id[7:16], 12),
            ('VERSION', settings['psp2MasterVersion'], 8),
        ]
    )


def vita_folder(out):
    """Copies and writes everything but the serialized data and the code module."""
    name = 'drifter-2018.2'
    player = paths.PSP2_PLAYER
    settings = player_settings()
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(player / 'PSP2Player_Il2cpp.self', out / f'{name}.self')
    shutil.copyfile(player / 'configuration.psp2path', out / 'configuration.psp2path')
    (out / f'{name}.bat').write_text(
        f'psp2run /console:all /log:"{name}.log" /fsroot . /elf "{name}.self"', encoding='ascii'
    )
    shutil.copytree(player / 'sce_module', out / 'sce_module', dirs_exist_ok=True)
    for module in SDK_MODULES:
        shutil.copyfile(paths.SDK / 'target' / 'sce_module' / module, out / 'sce_module' / module)
    shutil.copytree(player / 'sce_sys', out / 'sce_sys', dirs_exist_ok=True)
    (out / 'sce_sys' / 'param.sfo').write_bytes(param_sfo(settings))
    (out / 'savedata').mkdir(exist_ok=True)
    media = out / 'Media'
    (media / 'Modules').mkdir(parents=True, exist_ok=True)
    (media / 'Resources').mkdir(parents=True, exist_ok=True)
    shutil.copyfile(player / 'Media' / 'Modules' / 'pthread.suprx', media / 'Modules' / 'pthread.suprx')
    resources = 'unity default resources'
    shutil.copyfile(player / 'Media' / 'Resources' / resources, media / 'Resources' / resources)
    (media / 'boot.config').write_text(BOOT_CONFIG, encoding='ascii', newline='\n')
    (media / 'platform_native_link.xml').write_text(PLATFORM_NATIVE_LINK, encoding='ascii', newline='\n')


def compare(out):
    """The files vita_folder writes, against the reference build."""
    ref = paths.REFERENCE
    same = differ = 0
    for path in sorted(p for p in out.rglob('*') if p.is_file()):
        rel = path.relative_to(out)
        theirs = ref / rel
        if not theirs.exists():
            continue
        if path.read_bytes() == theirs.read_bytes():
            same += 1
        else:
            differ += 1
            print('differs:', rel.as_posix())
    return same, differ


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=lambda p: __import__('pathlib').Path(p), default=paths.OUT)
    parser.add_argument('--compare', action='store_true')
    args = parser.parse_args()
    vita_folder(args.out)
    if args.compare:
        same, differ = compare(args.out)
        print(f'{same} files identical to the reference, {differ} differ')
    return 0


if __name__ == '__main__':
    sys.exit(main())
