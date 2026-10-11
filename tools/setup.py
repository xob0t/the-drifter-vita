"""Install what the pipeline needs, skipping what is already there, then check the result.

    uv run tools/setup.py                        # Unity 2018.2.19f1, AssetRipper, Mono.Cecil, Python packages
    uv run tools/setup.py --il2cpp               # also the editor's Windows IL2CPP module
    uv run tools/setup.py --vita                 # also the PS Vita module and the PS Vita SDK
    uv run tools/setup.py --vita --vita-sdk <your SDK archive or folder>
    uv run tools/setup.py --vita --unity         # also what the editor's PS Vita build needs
    uv run tools/setup.py doctor [--target windows|il2cpp|vita] [--license]

The Unity installers come from Unity's CDN, the PS Vita module from its archive.org copy (Unity no
longer serves it). Every download is checked against a pinned hash (SHA-256; SHA-1 for the SDK's
files, the only one archive.org lists), and the Unity installers must carry Unity Technologies'
Authenticode signature before they run. The one exception is Microsoft's Visual C++
Redistributable, which Microsoft updates in place: it must carry Microsoft's signature instead.
Installers that need administrator rights ask for them (UAC).

With --vita and no PS Vita SDK on the machine, setup downloads SDK 3.570 from archive.org and
extracts it to C:\\vitasdk. --vita-sdk takes your own copy instead, an archive (.zip, .7z, ...) or a
folder. Either way setup checks the SDK's tools and version and sets SCE_PSP2_SDK_DIR for your user.
--vita also installs the current Visual C++ runtime when Windows has an older one (the Unity 2018
installer brings the 2015 one, which the builder's texture encoder crashes in). The PS Vita build
(tools/builder) needs nothing more. --unity adds what the editor's PS Vita build
(pipeline.py --unity) needs: the SDK's Target Manager Server and Publishing Tools (SCE_ROOT_DIR;
Publishing Tools need .NET Framework 3.5), and the license check. --dry-run reports what would be done.
"""

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'downgrade'))
import doctor
from common import (
    CECIL,
    CECIL_SHA256,
    CECIL_URL,
    CECIL_VERSION,
    EDITORS,
    ROOT,
    STEAM_GAME,
    TARGET,
    WORK,
    download,
    mono_cecil,
)

DOWNLOADS = WORK / 'installers'
# How deep below the SDK's folder its Target Manager and Publishing Tools installers are looked for.
INSTALLER_DEPTH = 4


class Setup:
    def __init__(self, args):
        self.args = args
        self.notes = []  # what the user still has to do
        self.license_check = None

    def say(self, step, message):
        print(f'[{step}] {message}', flush=True)

    def confirm(self, question):
        if self.args.yes:
            return True
        if not sys.stdin.isatty():
            print(f'  {question} Not asked here: rerun with --yes to accept.')
            return False
        try:
            return input(f'  {question} [y/N] ').strip().lower() in ('y', 'yes')
        except EOFError:  # nothing to read the answer from (stdin is NUL, which counts as a terminal)
            print('\n  Not answered: rerun with --yes to accept.')
            return False

    # ------------------------------------------------------------ Python, AssetRipper, Mono.Cecil

    def python(self):
        uv = shutil.which('uv')
        if not uv:
            self.say('python', 'uv is not on PATH; install it (https://docs.astral.sh/uv/), then run uv sync')
            return
        if self.args.dry_run:
            self.say('python', 'would run uv sync')
            return
        subprocess.run([uv, 'sync'], cwd=ROOT, check=True)
        self.say('python', 'packages in sync with uv.lock')

    def assetripper(self):
        target = doctor.ASSETRIPPER.parent
        if doctor.ASSETRIPPER.exists():
            self.say('assetripper', f'already installed ({target})')
            return
        if self.args.dry_run:
            self.say('assetripper', f'would download AssetRipper 2.0.0 into {target}')
            return
        url, sha256 = doctor.ASSETRIPPER_ZIP
        with tempfile.TemporaryDirectory() as temp:
            archive = download(url, Path(temp) / 'AssetRipper_win_x64.zip', sha256)
            extract_zip(archive, target)
        if not doctor.ASSETRIPPER.exists():
            sys.exit(f'AssetRipper: the archive has no {doctor.ASSETRIPPER.name}')
        self.say('assetripper', f'installed into {target}')

    def cecil(self):
        if CECIL.exists():
            self.say('cecil', f'Mono.Cecil {CECIL_VERSION} already installed ({CECIL.parent})')
        elif self.args.dry_run:
            self.say('cecil', f'would download Mono.Cecil {CECIL_VERSION} from NuGet into {CECIL.parent}')
        else:
            mono_cecil()
            self.say('cecil', f'Mono.Cecil {CECIL_VERSION} installed into {CECIL.parent}')

    # ------------------------------------------------------------ Unity

    def unity(self, module):
        """The editor, or one of its modules ('il2cpp', 'vita'), in %LOCALAPPDATA%\\Unity\\Editors\\<version>."""
        name = doctor.module_name(module)
        target = EDITORS / TARGET
        if (target / doctor.EDITOR_FILES[module]).exists():
            self.say(module, f'{name} already installed ({target})')
            return
        if module != 'editor' and not (target / doctor.EDITOR_FILES['editor']).exists() and not self.args.dry_run:
            sys.exit(f'{name}: install the editor first')
        url, sha256 = doctor.INSTALLERS[module]
        installer = DOWNLOADS / url.rsplit('/', 1)[1]
        if self.args.dry_run:
            self.say(module, f'would download {url}, check it and install it into {target}')
            return
        free = doctor.free_gb(EDITORS)
        if module == 'editor' and free < doctor.EDITOR_GB:
            sys.exit(f'{name}: {free:.1f} GB free on {EDITORS.anchor}; the editor needs about {doctor.EDITOR_GB} GB')
        download(url, installer, sha256)
        verify_unity_signature(installer)
        self.say(module, f'installing {name} into {target}')
        code = run_installer(installer, f'/S /D={target}')
        if code != 0 or not (target / doctor.EDITOR_FILES[module]).exists():
            sys.exit(f'{installer.name} exited with code {code} and did not install {doctor.EDITOR_FILES[module]}')
        if not self.args.keep_downloads:
            installer.unlink()
        self.say(module, f'{name} installed')

    def license(self, platform):
        """Start the editor once in batch mode; a pass is remembered until the editor changes."""
        unity = EDITORS / TARGET / doctor.EDITOR_FILES['editor']
        if self.args.dry_run or self.args.skip_license_check or not unity.exists():
            return
        if doctor.license_checked(TARGET, platform):
            self.say('license', f'already checked: the editor runs in batch mode with the {platform} target')
            return
        self.say('license', 'starting the editor once in batch mode (about a minute)')
        self.license_check = doctor.check_license(TARGET, platform)  # the checklist at the end shows it
        self.say('license', self.license_check.detail)

    # ------------------------------------------------------------ PS Vita SDK

    def vita_sdk(self):
        source = self.args.vita_sdk
        found = None
        if source:
            sdk = self.install_sdk(Path(source))
        else:
            found = next((c for c in doctor.sdk_candidates() if (c / doctor.SDK_TOOLS[0]).exists()), None)
            sdk = found or self.download_sdk()
        if sdk is None:
            return
        if not source and (found is None or found == doctor.VITA_SDK_DOWNLOADED):
            self.patch_sdk(sdk)
        self.validate_sdk(sdk)
        # An SDK found on the machine is offered; one passed with --vita-sdk or downloaded is what the user asked for.
        self.set_env('vita-sdk', 'SCE_PSP2_SDK_DIR', sdk, ask=found is not None)
        root = doctor.sce_root()
        if root is None and self.args.unity:
            root = self.install_target_manager(sdk)
        elif root is None:
            self.say(
                'vita-sdk', 'Target Manager Server and Publishing Tools skipped: only builds with --unity need them'
            )
        if root is not None:
            self.set_env('vita-sdk', 'SCE_ROOT_DIR', root, ask=found is not None)

    def vc_runtime(self):
        """Microsoft's current Visual C++ Redistributable, when System32's msvcp140.dll is older than the
        builder's native packages need."""
        check = doctor.check_vc_runtime()
        if check.ok:
            self.say('vc-runtime', f'Visual C++ runtime {check.detail}')
            return
        if self.args.dry_run:
            self.say('vc-runtime', f'{check.detail}: would install {doctor.VC_REDIST_URL}')
            return
        installer = DOWNLOADS / 'vc_redist.x64.exe'
        download(doctor.VC_REDIST_URL, installer, None)
        status, subject = authenticode(installer)
        if not doctor.signer_is_microsoft(status, subject):
            sys.exit(
                f'{installer.name}: no valid Authenticode signature of Microsoft (status {status}, '
                f'signer {subject or "none"}). Not running it.'
            )
        print(f'  signature: {status}, {subject}')
        self.say('vc-runtime', f'{check.detail}: installing the current Visual C++ Redistributable')
        code = run_elevated(installer, '/install /quiet /norestart')
        installer.unlink(missing_ok=True)
        # 3010: installed, a restart pending; 1638: a newer version is already installed.
        if code not in (0, 3010, 1638) or not doctor.check_vc_runtime().ok:
            sys.exit(f'{installer.name} exited with code {code}; {doctor.check_vc_runtime().detail}')
        self.say('vc-runtime', f'Visual C++ runtime {doctor.check_vc_runtime().detail}')

    def download_sdk(self):
        """SDK 3.570 from archive.org, extracted as an archive passed with --vita-sdk is."""
        url, digest = doctor.VITA_SDK_ZIP
        archive = DOWNLOADS / url.rsplit('/', 1)[1]
        destination = Path(self.args.vita_sdk_dir or doctor.VITA_SDK_FOLDER)
        existing = doctor.find_sdk_root(destination) if destination.exists() else None
        if existing:  # extracted there by an earlier run with --vita-sdk-dir
            self.say('vita-sdk', f'already installed ({existing})')
            return existing
        if self.args.dry_run:
            self.say(
                'vita-sdk',
                f'no PS Vita SDK found; would download {url}, extract it to {destination} and patch it to '
                + doctor.format_sdk_version(doctor.VITA_SDK_PATCHED),
            )
            return None
        self.say('vita-sdk', 'no PS Vita SDK found; downloading SDK 3.570 (350 MB)')
        download(url, archive, digest)
        sdk = self.install_sdk(archive)
        if not self.args.keep_downloads:
            archive.unlink()
        return sdk

    def patch_sdk(self, sdk: Path):
        """Sony's patches over the downloaded SDK, up to VITA_SDK_PATCHED. Each zip holds sdk/..., the files
        it replaces."""
        if (doctor.sdk_version(sdk) or (0, 0, 0)) >= doctor.VITA_SDK_PATCHED or self.args.dry_run:
            return
        for url, digest in doctor.VITA_SDK_PATCHES:
            patch = download(url, DOWNLOADS / url.rsplit('/', 1)[1], digest)
            self.say('vita-sdk', f'applying {patch.name}')
            extract_zip(patch, sdk.parent)
            if not self.args.keep_downloads:
                patch.unlink()

    def install_sdk(self, source: Path):
        if not source.exists():
            sys.exit(f'--vita-sdk: {source} does not exist')
        if source.is_dir() and (
            not self.args.vita_sdk_dir or Path(self.args.vita_sdk_dir).resolve() == source.resolve()
        ):
            sdk = doctor.find_sdk_root(source)
            if sdk is None:
                sys.exit(f'--vita-sdk: no PS Vita SDK in {source} (looked for {doctor.SDK_TOOLS[0]})')
            self.say('vita-sdk', f'using the SDK in place: {sdk}')
            return sdk
        destination = Path(self.args.vita_sdk_dir or doctor.VITA_SDK_FOLDER)
        existing = doctor.find_sdk_root(destination) if destination.exists() else None
        if existing:
            self.say('vita-sdk', f'already installed ({existing})')
            return existing
        if source.is_dir():
            if self.args.dry_run:
                self.say('vita-sdk', f'would copy {source} to {destination}')
                return None
            self.say('vita-sdk', f'copying {source} to {destination}')
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            if self.args.dry_run:
                self.say('vita-sdk', f'would extract {source} to {destination}')
                return None
            command = extract_command(source, destination, seven_zip(), windows_tar())
            self.say('vita-sdk', f'extracting {source.name} to {destination}')
            destination.mkdir(parents=True, exist_ok=True)
            if command is None:
                extract_zip(source, destination)
            else:
                subprocess.run(command, check=True)
        sdk = doctor.find_sdk_root(destination)
        if sdk is None:
            sys.exit(f'--vita-sdk: no PS Vita SDK in {destination} after extracting (looked for {doctor.SDK_TOOLS[0]})')
        return sdk

    def validate_sdk(self, sdk: Path):
        problem, shown = doctor.sdk_status(sdk)
        if problem:
            sys.exit(f'PS Vita SDK: {problem}')
        self.say('vita-sdk', f'{sdk}: version {shown}, tools present')

    def install_target_manager(self, sdk: Path):
        """Target Manager Server and Publishing Tools (psp2ctrl, psp2run, psp2pubcmd), from the SDK's installers."""
        search = [sdk.parent] + (
            [Path(self.args.vita_sdk)] if self.args.vita_sdk and Path(self.args.vita_sdk).is_dir() else []
        )
        wanted = {
            pattern: tools
            for pattern, tools in doctor.SCE_INSTALLERS.items()
            if not any(not doctor.missing(c, tools) for c in doctor.sce_root_candidates())  # not installed
        }
        msis = msi_files(search) if wanted else []
        installers, downloaded = [], []
        for pattern in wanted:
            found = next((p for p in msis if fnmatch.fnmatch(p.name, pattern)), None)
            if found is None:  # not beside the SDK: archive.org's copy
                url, digest = doctor.SCE_INSTALLER_DOWNLOADS[pattern]
                found = DOWNLOADS / url.rsplit('/', 1)[1]
                if not self.args.dry_run:
                    download(url, found, digest)
                downloaded.append(found)
            installers.append(found)
        # Publishing Tools' installer stops with error 1603 without .NET Framework 3.5, a Windows feature.
        need_net35 = any(p.name.lower().startswith('publishingtools') for p in installers) and not doctor.dotnet35()
        net35 = ' and turn on .NET Framework 3.5, which Publishing Tools needs' if need_net35 else ''
        if self.args.dry_run:
            fetch = f'download {", ".join(p.name for p in downloaded)} from archive.org, ' if downloaded else ''
            self.say('vita-sdk', f'would {fetch}install ' + ', '.join(p.name for p in installers) + net35)
            return None
        if not self.confirm(
            'Install ' + ', '.join(p.name for p in installers) + net35 + ' (needs administrator rights)?'
        ):
            self.notes.append(
                'Install '
                + ', '.join(str(p) for p in installers)
                + (' (after turning on .NET Framework 3.5 in Windows features)' if need_net35 else '')
                + ', then rerun setup.py --vita'
            )
            return None
        if need_net35:
            # DISM downloads it from Windows Update.
            self.say('vita-sdk', 'turning on .NET Framework 3.5 (a few minutes)')
            code = run_elevated('dism.exe', '/Online /Enable-Feature /FeatureName:NetFx3 /All /NoRestart /Quiet')
            if code not in (0, 3010) or not doctor.dotnet35():
                sys.exit(
                    f'DISM exited with code {code} and did not turn on .NET Framework 3.5. Turn it on in '
                    '"Turn Windows features on or off", then rerun.'
                )
        for msi in installers:
            self.say('vita-sdk', f'installing {msi.name}')
            code = run_elevated('msiexec.exe', f'/i "{msi}" /qn /norestart')
            if code not in (0, 3010):
                sys.exit(f'{msi.name}: msiexec exited with code {code}')
        if not self.args.keep_downloads:
            for msi in downloaded:
                msi.unlink()
        root = doctor.sce_root()
        if root is None:
            sys.exit(
                'Target Manager Server installed, but psp2ctrl.exe is in none of: '
                + ', '.join(str(c) for c in doctor.sce_root_candidates())
            )
        return root

    # ------------------------------------------------------------ environment

    def set_env(self, step, name, value, ask=False):
        value = str(value)
        current, stale = doctor.env(name)
        if current and os.path.normcase(current.rstrip('\\')) == os.path.normcase(value.rstrip('\\')):
            self.say(step, f'{name} already set to {current}' + (' (open a new shell to see it)' if stale else ''))
            return
        if self.args.dry_run:
            self.say(step, f'would set {name}={value} for your user' + (f' (now {current})' if current else ''))
            return
        if ask and not self.confirm(f'Set {name}={value} for your user?'):
            self.notes.append(f'Set {name}: uv run tools/setup.py --yes ... or setx {name} "{value}"')
            return
        set_user_env(name, value)
        self.say(step, f'{name} set to {value} for your user; new shells see it')

    def game(self):
        if self.args.game:
            game = Path(self.args.game)
            if not (game / doctor.GAME_EXE).exists():
                sys.exit(f'--game: no {doctor.GAME_EXE} in {game}')
            self.set_env('game', 'DRIFTER_GAME', game)
            return
        current, _ = doctor.env('DRIFTER_GAME')
        if current:
            if (Path(current) / doctor.GAME_EXE).exists():
                self.say('game', f'already found: DRIFTER_GAME={current}')
            else:
                self.say('game', f'DRIFTER_GAME={current} has no {doctor.GAME_EXE}; pass --game <folder>')
                self.notes.append('Point DRIFTER_GAME at the game: uv run tools/setup.py --game <folder>')
            return
        found = doctor.steam_game()
        if found is None:
            self.say('game', 'The Drifter is not in any Steam library; install it with Steam, or pass --game <folder>')
            self.notes.append('Install The Drifter, or: uv run tools/setup.py --game <folder>')
        elif os.path.normcase(str(found)) == os.path.normcase(str(STEAM_GAME)):
            self.say('game', f"already found at Steam's default folder ({STEAM_GAME})")
        else:
            self.say('game', f'found in {found}')
            self.set_env('game', 'DRIFTER_GAME', found, ask=True)

    def vita_host(self):
        host = self.args.vita_host
        if not re.fullmatch(r'[A-Za-z0-9.:-]+', host):
            sys.exit(f'--vita-host: {host!r} is not an address')
        self.set_env('vita-host', 'DRIFTER_VITA_HOST', host)


# ---------------------------------------------------------------- helpers


def msi_files(folders, depth=INSTALLER_DEPTH):
    """The .msi files in the folders and up to depth levels below them, each once, in order: one walk,
    as an SDK's folder can sit in a large one."""
    found = []
    for folder in folders:
        for current, subfolders, files in os.walk(folder):
            if len(Path(current).relative_to(folder).parts) >= depth:
                subfolders.clear()
            subfolders.sort()
            found += [Path(current) / name for name in sorted(files) if name.lower().endswith('.msi')]
    return list(dict.fromkeys(found))


def extract_zip(archive: Path, destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(destination)


def seven_zip():
    found = shutil.which('7z') or shutil.which('7za')
    if found:
        return found
    for variable in ('ProgramFiles', 'ProgramFiles(x86)'):
        candidate = Path(os.environ.get(variable, '')) / '7-Zip' / '7z.exe'
        if os.environ.get(variable) and candidate.exists():
            return str(candidate)
    return None


def windows_tar():
    """Windows' own tar (bsdtar), which also reads .7z and .rar; Git's GNU tar on PATH does not."""
    candidate = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'System32' / 'tar.exe'
    return str(candidate) if candidate.exists() else None


def extract_command(archive: Path, destination: Path, seven, tar):
    """How to extract an archive: None for a .zip (Python's zipfile), else 7-Zip's or Windows' tar command."""
    if archive.suffix.lower() == '.zip':
        return None
    if seven:
        return [seven, 'x', '-y', '-bso0', f'-o{destination}', str(archive)]
    if tar:
        return [tar, '-xf', str(archive), '-C', str(destination)]
    sys.exit(
        f'No tool to extract {archive.name}: install 7-Zip (winget install 7zip.7zip) or extract it yourself '
        'and pass the folder'
    )


def authenticode(path: Path):
    """(Status, signer subject) from PowerShell's Get-AuthenticodeSignature."""
    script = (
        '$s = Get-AuthenticodeSignature -LiteralPath $env:SETUP_SIGNED_FILE; '
        '$s.Status.ToString(); if ($s.SignerCertificate) { $s.SignerCertificate.Subject }'
    )
    result = subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True,
        text=True,
        env={**os.environ, 'SETUP_SIGNED_FILE': str(path)},
    )
    lines = result.stdout.strip().splitlines()
    return (lines[0].strip() if lines else 'unknown'), (lines[1].strip() if len(lines) > 1 else '')


def verify_unity_signature(path: Path):
    status, subject = authenticode(path)
    if not doctor.signer_is_unity(status, subject):
        sys.exit(
            f'{path.name}: no valid Authenticode signature of Unity Technologies (status {status}, '
            f'signer {subject or "none"}). Not running it.'
        )
    print(f'  signature: {status}, {subject}')


def run_installer(installer: Path, arguments: str):
    """Run an NSIS installer silently; through UAC when it asks for administrator rights. NSIS wants
    /D= last and unquoted, so the command line is passed as one string."""
    try:
        return subprocess.run(f'"{installer}" {arguments}').returncode
    except OSError as error:
        if getattr(error, 'winerror', None) != 740:  # ERROR_ELEVATION_REQUIRED
            raise
    return run_elevated(installer, arguments)


def run_elevated(program, arguments: str):
    print('  Windows asks for administrator rights for this installer.', flush=True)
    script = (
        '$p = Start-Process -FilePath $env:SETUP_PROGRAM -ArgumentList $env:SETUP_ARGUMENTS -Verb RunAs '
        '-Wait -PassThru; exit $p.ExitCode'
    )
    return subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
        env={**os.environ, 'SETUP_PROGRAM': str(program), 'SETUP_ARGUMENTS': arguments},
    ).returncode


def set_user_env(name, value):
    """Set a variable for the user (HKCU\\Environment) and tell running programs, so new shells,
    Git Bash included, see it."""
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    os.environ[name] = value
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, 'Environment', 0x0002, 5000, ctypes.byref(result))


def download_only(args):
    """Download and verify installers into a folder without installing anything."""
    folder = Path(args.to)
    for item in args.items:
        if item == 'assetripper':
            url, sha256 = doctor.ASSETRIPPER_ZIP
        elif item == 'cecil':
            url, sha256 = CECIL_URL, CECIL_SHA256
        else:
            url, sha256 = doctor.INSTALLERS[item]
        path = download(url, folder / url.rsplit('/', 1)[1], sha256)
        print(f'{item}: {path} (SHA-256 matches)')
        if item in doctor.INSTALLERS:
            verify_unity_signature(path)


def main():
    if sys.argv[1:2] == ['doctor']:
        sys.exit(doctor.main(sys.argv[2:]))
    if sys.argv[1:2] == ['download']:
        parser = argparse.ArgumentParser(prog='setup.py download', description=download_only.__doc__)
        parser.add_argument('items', nargs='+', choices=[*doctor.INSTALLERS, 'assetripper', 'cecil'])
        parser.add_argument('--to', required=True, help='the folder to download into')
        download_only(parser.parse_args(sys.argv[2:]))
        return
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--il2cpp', action='store_true', help="the editor's Windows IL2CPP module")
    parser.add_argument(
        '--vita', action='store_true', help='the PS Vita module, and the SDK (downloaded if the machine has none)'
    )
    parser.add_argument(
        '--vita-sdk', metavar='PATH', help='your own PS Vita SDK: an archive or a folder (implies --vita)'
    )
    parser.add_argument(
        '--vita-sdk-dir',
        metavar='DIR',
        help=f'where to extract (or copy) the SDK; default {doctor.VITA_SDK_FOLDER} for an archive, '
        'in place for a folder',
    )
    parser.add_argument('--vita-host', metavar='ADDRESS', help="set DRIFTER_VITA_HOST, the console's address")
    parser.add_argument('--game', metavar='FOLDER', help='set DRIFTER_GAME to this copy of The Drifter')
    parser.add_argument(
        '--yes', '-y', action='store_true', help='accept the offers (set variables, run SDK installers)'
    )
    parser.add_argument('--dry-run', action='store_true', help='report what would be done, change nothing')
    parser.add_argument(
        '--keep-downloads', action='store_true', help=f'keep the installers and the SDK archive in {DOWNLOADS}'
    )
    parser.add_argument(
        '--unity',
        action='store_true',
        help="with --vita: also what the editor's PS Vita build (pipeline.py --unity) needs: Target Manager "
        'Server, Publishing Tools and .NET Framework 3.5, and the license check',
    )
    parser.add_argument('--skip-license-check', action='store_true', help='do not start the editor at the end')
    args = parser.parse_args()
    if args.vita_sdk:
        args.vita = True
    setup = Setup(args)
    target = 'vita' if args.vita else ('il2cpp' if args.il2cpp else 'windows')
    setup.python()
    setup.unity('editor')
    if args.il2cpp:
        setup.unity('il2cpp')
    if args.vita:
        setup.unity('vita')
    setup.assetripper()
    setup.cecil()
    setup.game()
    if args.vita:
        setup.vita_sdk()
        setup.vc_runtime()
    if args.vita_host:
        setup.vita_host()
    if not args.vita or args.unity:  # the PS Vita build runs no editor
        setup.license('vita' if args.vita else 'windows')
    print()
    if args.dry_run:
        print('Dry run: nothing was changed.')
        return
    code = doctor.main(['--target', target], setup.license_check)
    for note in setup.notes:
        print('To do: ' + note)
    if code == 0 and not setup.notes:
        platform = ' --platform vita' if args.vita else (' --backend il2cpp' if args.il2cpp else '')
        print(f'Next: uv run tools/downgrade/pipeline.py all{platform}')
    sys.exit(code)


if __name__ == '__main__':
    main()
