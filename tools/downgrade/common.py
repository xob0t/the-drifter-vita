"""Paths, constants and small helpers shared by pipeline.py, its rule modules (asset_rules.py,
shader_rules.py) and the other tools. Tools that import pipeline also reach the names pipeline.py
imports from here as pipeline.<name>."""

import contextlib
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import progress
import truststore
from rich.text import Text

# Every download checks TLS with Windows' certificate store, which fetches missing root certificates
# on demand. Python's own copy of the store lacks them on a fresh Windows (archive.org's, for one).
truststore.inject_into_ssl()


def registry_env(name, scope):
    """A variable as stored for the user ('user') or the machine ('machine'), or None."""
    import winreg

    key, path = (
        (winreg.HKEY_CURRENT_USER, 'Environment')
        if scope == 'user'
        else (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')
    )
    try:
        with winreg.OpenKey(key, path) as handle:
            value, kind = winreg.QueryValueEx(handle, name)
    except OSError:
        return None
    return os.path.expandvars(value) if kind == winreg.REG_EXPAND_SZ else value


def effective_env(process, user, machine):
    """The value a variable has for the tools, and whether a new shell is needed to see it: (value, stale)."""
    if process:
        return process, False
    value = user or machine
    return value, bool(value)


def env(name):
    """(value, stale) of a variable: from this process, else as set for new shells (stale), since a shell
    opened before setup.py set it does not have it yet."""
    return effective_env(os.environ.get(name), registry_env(name, 'user'), registry_env(name, 'machine'))


def setting(name):
    """A variable's value for the tools (env), or ''."""
    return env(name)[0] or ''


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / 'tools'
EXTERNAL = TOOLS / 'external'
HERE = Path(__file__).resolve().parent
WORK = ROOT / 'work'
ARTIFACTS = ROOT / 'artifacts' / 'downgrade'
EDITORS = Path(os.environ['LOCALAPPDATA']) / 'Unity' / 'Editors'
# Steam's default install folder, and the game's folder in its library.
STEAM = Path(r'C:\Program Files (x86)\Steam')
STEAM_GAME = STEAM / 'steamapps' / 'common' / 'The Drifter'


def default_game():
    """The game installation pipeline.py reads (--game overrides it) and the test tools copy saves from:
    DRIFTER_GAME for another install folder, else Steam's default folder."""
    return Path(setting('DRIFTER_GAME') or STEAM_GAME)


DEFAULT_GAME = default_game()
# The newest 2018.2 release whose PS Vita build module is known to exist.
TARGET = '2018.2.19f1'
SOURCE_VERSION = '2020.3.49f1'
USCSANDBOX_COMMIT = '563391b849116de5d470420cc0e3f1bc6a4b6157'
USCSANDBOX_SHA256 = '05459f727bd591cd588b1b4d3baa8d8c0b0b8bf4ad3990e7f1f1a187594f9c82'  # its GitHub zip
# Mono.Cecil, which the assembly rewriter, StubAssembly and the probe's injector are built with: the NuGet
# release of the version Unity 2020.3's Unity.Cecil.dll forks (0.10.0-beta6). The rewritten assemblies
# are byte-identical to those made with Unity.Cecil; later releases write them differently.
CECIL_VERSION = '0.10.0-beta6'
CECIL_SHA256 = 'fcfeb1c90073641fc56c21409af5d6499d28be691bf19c87bb9b90afe6d015cb'  # the .nupkg
CECIL = EXTERNAL / f'mono.cecil-{CECIL_VERSION}' / 'Mono.Cecil.dll'
CECIL_URL = f'https://api.nuget.org/v3-flatcontainer/mono.cecil/{CECIL_VERSION}/mono.cecil.{CECIL_VERSION}.nupkg'
ASSETRIPPER = EXTERNAL / 'assetripper-2.0.0' / 'AssetRipper.GUI.Free.exe'
CSC = Path(os.environ['WINDIR']) / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319' / 'csc.exe'
# The PS Vita's address on the network (VitaShell's FTP server, vitacompanion's command server), from
# DRIFTER_VITA_HOST. Only the console tools need it (vita_host() stops with a message without it).
VITA_HOST = setting('DRIFTER_VITA_HOST')


def vita_host():
    if not VITA_HOST:
        sys.exit("Set DRIFTER_VITA_HOST to the PS Vita's address on the network.")
    return VITA_HOST


def log(message):
    progress.console.print(Text.assemble((time.strftime('[%H:%M:%S]'), 'dim'), ' ', str(message)))


def run(command):
    log(' '.join(str(c) for c in command))
    subprocess.run([str(c) for c in command], check=True)


def unity_files(version):
    """Where setup.py extracts, without installing anything, the parts of Unity's editor and PS Vita
    module installers the pipeline and the editor-free build read (laid out as the editor's folder)."""
    return EXTERNAL / f'unity-{version}'


def editor_data(version):
    """Unity's Editor/Data folder: the files setup.py extracted from Unity's installers, or else an
    installed editor's (the same files)."""
    extracted = unity_files(version) / 'Editor' / 'Data'
    return extracted if extracted.exists() else EDITORS / version / 'Editor' / 'Data'


def download(url, destination: Path, digest: str | None):
    """Download url to destination and check its hash: SHA-256, or SHA-1 for a 40-digit pin (archive.org
    lists only SHA-1 and MD5 for an item's files). A file already there with that hash is kept.
    Timeouts, dropped connections and server errors are retried a few times. Stops on a mismatch
    and leaves nothing behind. digest None: a file its publisher updates in place, which the caller
    checks another way (its signature)."""
    algorithm = 'sha1' if digest and len(digest) == 40 else 'sha256'
    if digest and destination.exists() and file_hash(destination, algorithm) == digest:
        return destination
    import http.client
    import urllib.error

    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + '.part')
    log(f'Downloading {url}')
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            fetch(url, part)
            break
        except (OSError, http.client.HTTPException) as error:
            permanent = isinstance(error, urllib.error.HTTPError) and error.code < 500 and error.code != 429
            if permanent or attempt == DOWNLOAD_ATTEMPTS:
                part.unlink(missing_ok=True)
                sys.exit(f'{url}: {error}' + ('' if permanent else '. Check the connection and run it again.'))
            wait = 5 * 2 ** (attempt - 1)
            log(f'  {error}; trying again in {wait} s ({attempt} of {DOWNLOAD_ATTEMPTS} attempts failed)')
            time.sleep(wait)
    actual = file_hash(part, algorithm)
    if digest and actual != digest:
        part.unlink()
        sys.exit(
            f'{url}: {algorithm.upper()} {actual}, expected {digest}. The download is corrupt or not the pinned file.'
        )
    part.replace(destination)
    return destination


DOWNLOAD_ATTEMPTS = 5


def fetch(url, part: Path):
    """One download attempt: url into part, from the start."""
    import urllib.request

    request = urllib.request.Request(url, headers={'User-Agent': 'drifter-vita-setup'})
    with urllib.request.urlopen(request, timeout=60) as response, part.open('wb') as out:
        total = int(response.headers.get('Content-Length') or 0) or None
        with progress.step(f'Downloading {url.rsplit("/", 1)[-1]}', total=total, eta=True, count='MB') as step:
            while chunk := response.read(1 << 20):
                out.write(chunk)
                step.advance(len(chunk))


def memory_gb():
    """(total, available) physical memory in GB."""
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [
            ('dwLength', ctypes.c_ulong),
            ('dwMemoryLoad', ctypes.c_ulong),
            ('ullTotalPhys', ctypes.c_ulonglong),
            ('ullAvailPhys', ctypes.c_ulonglong),
            ('ullTotalPageFile', ctypes.c_ulonglong),
            ('ullAvailPageFile', ctypes.c_ulonglong),
            ('ullTotalVirtual', ctypes.c_ulonglong),
            ('ullAvailVirtual', ctypes.c_ulonglong),
            ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
        ]

    status = Status()
    status.dwLength = ctypes.sizeof(Status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullTotalPhys / 2**30, status.ullAvailPhys / 2**30


def file_hash(path: Path, algorithm='sha256'):
    digest = hashlib.new(algorithm)
    with path.open('rb') as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def mono_cecil():
    """Mono.Cecil.dll at CECIL_VERSION, fetched from NuGet on first use."""
    if not CECIL.exists():
        import tempfile
        import zipfile

        with tempfile.TemporaryDirectory() as temp:
            package = download(CECIL_URL, Path(temp) / 'mono.cecil.nupkg', CECIL_SHA256)
            with zipfile.ZipFile(package) as archive:
                data = archive.read('lib/net40/Mono.Cecil.dll')
        CECIL.parent.mkdir(parents=True, exist_ok=True)
        CECIL.write_bytes(data)
    return CECIL


def remove_tree(folder: Path):
    """Delete a folder or a file, read-only files included (Git's object files, for one). A link (a
    symbolic link or a junction) is removed, never what it points to. A file another program holds (an
    antivirus scan, a process still exiting) is tried again for a few seconds, then left. Returns
    (bytes deleted, files left)."""
    if is_link(folder):
        return (0, []) if remove_path(folder, os.rmdir if folder.is_dir() else os.remove) else (0, [folder])
    if not folder.is_dir():
        size = folder.lstat().st_size
        return (size, []) if remove_path(folder, os.remove) else (0, [folder])
    deleted, left, folders = 0, [], []
    for root, names, files in os.walk(folder):
        root = Path(root)
        folders.append(root)
        for name in [n for n in names if is_link(root / n)]:
            names.remove(name)  # not into it
            if not remove_path(root / name, os.rmdir):
                left.append(root / name)
        for name in files:
            path = root / name
            size = path.lstat().st_size
            if remove_path(path, os.remove):
                deleted += size
            else:
                left.append(path)
    for path in reversed(folders):
        remove_path(path, os.rmdir)  # stays when a file in it does
    return deleted, left


def is_link(path: Path):
    return path.is_symlink() or path.is_junction()


DELETING = '.deleting'


def delete(path: Path):
    """Delete a folder or a file whole, or leave it whole: it is first renamed to <name>.deleting, which
    Windows refuses while a program has a file in it open, so a tool is never left half there for the
    next run to trust. finish_deleting clears what an interrupted run left. Returns (bytes deleted,
    paths left)."""
    if is_link(path) or not path.exists():
        return remove_tree(path) if is_link(path) else (0, [])
    renamed = path.with_name(path.name + DELETING)
    if renamed.exists() or is_link(renamed):
        remove_tree(renamed)
    if not remove_path(path, lambda p: os.rename(p, renamed)):
        return 0, [path]
    return remove_tree(renamed)


def finish_deleting(*folders: Path):
    """Delete the <name>.deleting an interrupted delete left in these folders."""
    for folder in folders:
        if folder.is_dir():
            for path in folder.glob('*' + DELETING):
                remove_tree(path)


REMOVE_ATTEMPTS = 6  # half a second apart


def remove_path(path: Path, function):
    import stat

    for attempt in range(REMOVE_ATTEMPTS):
        try:
            function(path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            with contextlib.suppress(OSError):
                os.chmod(path, stat.S_IWRITE)
            if attempt:
                time.sleep(0.5)
        except OSError:  # a folder that still has files
            return False
    return False


def github_source(repository, commit, digest, folder: Path):
    """A GitHub repository's files at a pinned commit, from its zip archive, into folder (replaced)."""
    import tempfile
    import zipfile

    with tempfile.TemporaryDirectory() as temp:
        archive = download(f'https://github.com/{repository}/archive/{commit}.zip', Path(temp) / 'source.zip', digest)
        if folder.exists() and remove_tree(folder)[1]:  # a Git clone, from earlier versions of the tools
            sys.exit(f'Could not delete {folder}: a program has files in it open. Close it and run this again.')
        with zipfile.ZipFile(archive) as handle:
            for member in handle.infolist():
                name = member.filename.split('/', 1)[1]  # below the archive's top folder
                if name and not member.is_dir():
                    target = folder / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(handle.read(member))
    return folder


def apply_patch(patch: Path, folder: Path):
    """Apply a unified diff (git diff's output; the lines before its first file are a comment) to the
    files in folder. Each hunk must match at the line its header names. Lines are compared without
    their line ending, and each file keeps its own."""
    lines = patch.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].startswith('--- '):
            i += 1
            continue
        path = folder / lines[i + 1].removeprefix('+++ b/')
        i += 2
        text = path.read_bytes().decode('utf-8')
        newline = '\r\n' if '\r\n' in text else '\n'
        source = re.split(r'\r?\n', text)
        patched, done = [], 0  # done: the source lines already copied or replaced
        while i < len(lines) and lines[i].startswith('@@'):
            header = re.match(r'@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@', lines[i])
            start, old_count, new_count = int(header[1]), int(header[2] or 1), int(header[3] or 1)
            i += 1
            old, new = [], []
            while len(old) < old_count or len(new) < new_count:
                kind, body = lines[i][:1] or ' ', lines[i][1:]
                i += 1
                if kind != '+':
                    old.append(body)
                if kind != '-':
                    new.append(body)
            first = start - 1 if old_count else start
            if source[first : first + len(old)] != old:
                sys.exit(f'{patch.name}: a hunk does not match {path} at line {start}.')
            patched += source[done:first] + new
            done = first + len(old)
        path.write_bytes(newline.join(patched + source[done:]).encode('utf-8'))


SHADER_NAME = re.compile(r'\s*Shader\s+"([^"]+)"')  # a .shader file's name line

# Serialized YAML assets with one main object, which Unity names after the file (m_Name) ...
NATIVE_ASSETS = {
    '.asset',
    '.anim',
    '.mat',
    '.prefab',
    '.controller',
    '.overrideController',
    '.mixer',
    '.playable',
    '.physicsMaterial2D',
    '.fontsettings',
    '.guiskin',
    '.renderTexture',
}
# ... and with scenes, every serialized YAML file of a project: what can reference other files by GUID.
YAML_FILES = NATIVE_ASSETS | {'.unity'}


def game_data(game: Path) -> Path:
    """The game's <name>_Data folder."""
    data = next(game.glob('*_Data'), None)
    if data is None:
        sys.exit(f'No <name>_Data folder in {game}: is that the game folder? (--game, DRIFTER_GAME)')
    return data


def meta_guid(meta: Path):
    match = re.search(r'^guid: ([0-9a-f]{32})', meta.read_text(encoding='utf-8'), re.M)
    return match.group(1) if match else None


def meta_of(path: Path) -> Path:
    """The .meta file of an asset or folder (with_suffix would cut a name with dots)."""
    return path.with_name(path.name + '.meta')


def guid_for(kind, name):
    """A GUID that is the same on every run, for a generated asset of a kind and name."""
    return hashlib.md5(f'{kind}:{name}'.encode()).hexdigest()


def stable_meta(kind, name, importer):
    folder = 'folderAsset: yes\n' if kind == 'folder' else ''
    return f'fileFormatVersion: 2\nguid: {guid_for(kind, name)}\n{folder}{importer}:\n  userData:\n'


def folder_meta(folder: Path):
    """{.meta path: content} of a generated folder."""
    return {meta_of(folder): stable_meta('folder', folder, 'DefaultImporter').encode()}
