"""Build the DrifterClock native plugin for the PS Vita (clock.c): DrifterClock.suprx in work/native/clock,
with the PS Vita SDK's psp2snc (SCE_PSP2_SDK_DIR), and the bootstrap module the editor would make for it
(DrifterClock.suprx.b.suprx). IL2CPP's [DllImport] on the PS Vita (libil2cpp/os/PSP2/dlopen.cpp) loads
app0:/Media/Plugins/<name>.suprx, then <name>.suprx.b.suprx, whose module_start registers the name and
address of each function with RegisterModule.

The SDK's libScePower_stub.a has no clock functions. Each of its function stubs is an object that names
the function and holds its NID; scePowerSetConfigurationMode's name has the same length (28) as the four
clock functions, so a copy of its object with the name and NID replaced imports each of them (the
firmware's user ScePower library exports them). The NIDs object (absolute _NID_ symbols) gets the same
treatment, its other names made unique per copy. From Crow Country's port (xob0t/crow-country-vita).

    uv run tools/downgrade/native/clock/build.py
"""

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from common import TARGET, WORK, editor_data, log, setting  # noqa: E402

OUT = WORK / 'native' / 'clock'
FILES = ['DrifterClock.suprx', 'DrifterClock.suprx.b.suprx']
SDK = Path(setting('SCE_PSP2_SDK_DIR') or r'C:\vitasdk\PSVITA\sdk')
BIN = SDK / 'host_tools' / 'build' / 'bin'
SOURCE = HERE / 'clock.c'
# The functions the plugin exports: what the source declares PRX_EXPORT.
EXPORT = re.compile(r'^PRX_EXPORT int (\w+)\(', re.MULTILINE)

# The SDK stub each new one is copied from: a function whose name has the same length.
BASE_NAME, BASE_NID = b'scePowerSetConfigurationMode', 0x3CE187B6
# The functions to import, by NID (vita-headers db/360/ScePower.yml, user library ScePower).
FUNCTIONS = {
    b'scePowerSetArmClockFrequency': 0x74DB5AE5,
    b'scePowerGetArmClockFrequency': 0xABC6F88F,
    b'scePowerSetGpuClockFrequency': 0x717DB06C,
    b'scePowerGetGpuClockFrequency': 0x1B04A1D6,
}

# The editor's bootstrap source for a plugin (UnityEditor.PSP2.Extensions), for DrifterClock's exports.
BRIDGE = r"""#include <stddef.h>
#include <kernel.h>
#include <moduleinfo.h>
SCE_MODULE_INFO(DrifterClock_b, SCE_MODULE_ATTR_NONE, 1, 1);
{declarations}
typedef int (*EXTERNFUNCTION)();
static struct FunctionMap {{ const char* name; EXTERNFUNCTION address; }} s_FunctionNames[] = {{
{entries}
}};
extern "C" __declspec(dllimport) void RegisterModule(const char* module_name, void* exports,
  size_t exports_count, SceSize size, void* arg);
extern "C" int module_start(SceSize sz, const void* arg) {{
  RegisterModule("DrifterClock", s_FunctionNames, sizeof(s_FunctionNames) / sizeof(s_FunctionNames[0]),
    sz, (void*)arg);
  return SCE_KERNEL_START_SUCCESS;
}}
"""


def bridge_source():
    """BRIDGE for every PRX_EXPORT function of the source: one missing from it would fail only on the
    console, as EntryPointNotFound at its first call."""
    names = EXPORT.findall(SOURCE.read_text(encoding='utf-8'))
    return BRIDGE.format(
        declarations='\n'.join(f'extern "C" int {name}();' for name in names),
        entries='\n'.join(f'  {{ "{name}", &{name} }},' for name in names),
    )


def run(*command, cwd=OUT):
    result = subprocess.run([str(c) for c in command], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        sys.exit(f'{Path(command[0]).name} failed ({result.returncode}):\n{result.stdout}{result.stderr}')


def patch(data: bytes, name: bytes, nid: int, mangle: bytes | None = None) -> bytes:
    old, new = BASE_NID.to_bytes(4, 'little'), nid.to_bytes(4, 'little')
    if old not in data or BASE_NAME not in data:
        sys.exit('libScePower_stub.a is not the one this script knows')
    data = data.replace(BASE_NAME, name).replace(old, new)
    if mangle:
        # Every other _NID_ symbol of the NIDs object gets a name no other copy defines.
        data = data.replace(b'_NID_', mangle).replace(mangle + name, b'_NID_' + name)
    return data


def stub_library() -> Path:
    extract = OUT / 'power'
    shutil.rmtree(extract, ignore_errors=True)
    extract.mkdir(parents=True)
    run(BIN / 'psp2snarl.exe', 'x', SDK / 'target' / 'lib' / 'libScePower_stub.a', cwd=extract)
    nids = (extract / '_ScePower-0001_NIDS.o').read_bytes()
    function = (extract / f'_ScePower-0001_F00_{BASE_NID:08x}.o').read_bytes()
    members = [extract / '_ScePower-0001_head.o']
    for index, (name, nid) in enumerate(FUNCTIONS.items()):
        stub = extract / f'_ScePower-0001_F00_{nid:08x}.o'  # psp2ld wants this name
        stub.write_bytes(patch(function, name, nid))
        names = extract / f'_ScePower-0001_NIDS_{index}.o'
        names.write_bytes(patch(nids, name, nid, b'_N%d__' % index))
        members += [stub, names]
    library = OUT / 'libDrifterPower_stub.a'
    library.unlink(missing_ok=True)
    run(BIN / 'psp2snarl.exe', 'crs', library, *members)
    return library


def build():
    """Builds the plugin unless it is up to date; returns the folder with FILES."""
    if not (BIN / 'psp2snc.exe').exists():
        sys.exit(f'No psp2snc in {BIN}; set SCE_PSP2_SDK_DIR (uv run tools/setup.py --vita)')
    modules = editor_data(TARGET) / 'PlaybackEngines' / 'PSP2Player' / 'Data' / 'Modules'
    # Rebuilt when the source, this script, the SDK's compiler or the player's stub changes.
    digest = hashlib.sha256()
    for path in [SOURCE, Path(__file__), BIN / 'psp2snc.exe', modules / 'Il2CppAssemblies_stub_weak.a']:
        digest.update(f'{path.name} {path.stat().st_size} '.encode() + hashlib.sha256(path.read_bytes()).digest())
    stamp = OUT / 'build.stamp'
    if stamp.exists() and stamp.read_text() == digest.hexdigest() and all((OUT / f).exists() for f in FILES):
        return OUT
    stamp.unlink(missing_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    library = stub_library()
    run(BIN / 'psp2snc.exe', '-O2', '-Xmserrors', '-c', SOURCE, '-o', OUT / 'clock.o')
    stubs = OUT / 'stubs'
    shutil.rmtree(stubs, ignore_errors=True)
    stubs.mkdir()
    run(
        BIN / 'psp2snc.exe',
        '-Wl,--oformat=prx',
        f'-Wl,--prx-stub-output-dir={stubs}',
        OUT / 'clock.o',
        library,
        SDK / 'target' / 'lib' / 'libSceKernel_stub.a',
        '-o',
        OUT / FILES[0],
    )
    bridge = OUT / 'bridge.cpp'
    bridge.write_text(bridge_source(), encoding='utf-8')
    run(BIN / 'psp2snc.exe', '-O2', '-Xmserrors', '-c', bridge, '-o', OUT / 'bridge.o')
    run(
        BIN / 'psp2snc.exe',
        '-Wl,--oformat=prx',
        '-Wl,--disable-warning=0431',
        OUT / 'bridge.o',
        stubs / 'DrifterClock_stub.a',
        modules / 'Il2CppAssemblies_stub_weak.a',
        '-o',
        OUT / FILES[1],
    )
    stamp.write_text(digest.hexdigest())
    log(f'Built {", ".join(FILES)} in {OUT}')
    return OUT


if __name__ == '__main__':
    build()
