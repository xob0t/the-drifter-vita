"""Checks of tools/setup.py and doctor.py's pure parts: path detection, version parsing, the checklist.

uv run python -m unittest discover tools/downgrade/tests
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import common  # noqa: E402
import doctor  # noqa: E402
from doctor import Check  # noqa: E402

_spec = importlib.util.spec_from_file_location('drifter_setup', HERE.parent / 'setup.py')
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


class Versions(unittest.TestCase):
    def test_sdk_version(self):
        text = '/* header */\n#define SCE_PSP2_SDK_VERSION\t0x03570071\n#endif\n'
        self.assertEqual(doctor.parse_sdk_version(text), (3, 570, 71))
        self.assertEqual(doctor.format_sdk_version((3, 570, 71)), '3.570.071')
        self.assertEqual(doctor.parse_sdk_version('#define SCE_PSP2_SDK_VERSION 0x03650011'), (3, 650, 11))
        self.assertIsNone(doctor.parse_sdk_version('#define SCE_PSP2_SDK_VERSION_MAJOR 3'))

    def test_unity_version(self):
        header = bytes(48) + b'2020.3.49f1\0' + bytes(8)
        self.assertEqual(doctor.unity_version(header), '2020.3.49f1')
        self.assertEqual(doctor.unity_version(bytes(16) + b'2018.2.19f1\0'), '2018.2.19f1')
        self.assertIsNone(doctor.unity_version(bytes(64)))


class Steam(unittest.TestCase):
    NEW = """"libraryfolders"
{
\t"0"
\t{
\t\t"path"\t\t"C:\\\\Program Files (x86)\\\\Steam"
\t\t"label"\t\t""
\t\t"apps"
\t\t{
\t\t\t"228980"\t\t"741239787"
\t\t}
\t}
\t"1"
\t{
\t\t"path"\t\t"D:\\\\SteamLibrary"
\t}
}
"""
    OLD = """"LibraryFolders"
{
\t"TimeNextStatsReport"\t\t"1561832478"
\t"ContentStatsID"\t\t"-158337411110787451"
\t"1"\t\t"E:\\\\Games\\\\Steam"
}
"""

    def test_library_folders(self):
        self.assertEqual(doctor.parse_library_folders(self.NEW), [r'C:\Program Files (x86)\Steam', r'D:\SteamLibrary'])
        self.assertEqual(doctor.parse_library_folders(self.OLD), [r'E:\Games\Steam'])
        self.assertEqual(doctor.parse_library_folders(''), [])

    def test_game_candidates(self):
        files = {Path(r'c:\program files (x86)\steam') / 'steamapps' / 'libraryfolders.vdf': self.NEW}
        found = doctor.game_candidates([r'c:\program files (x86)\steam', r'C:\Program Files (x86)\Steam'], files.get)
        self.assertEqual(
            found,
            [
                Path(r'c:\program files (x86)\steam\steamapps\common\The Drifter'),
                Path(r'D:\SteamLibrary\steamapps\common\The Drifter'),
            ],
        )

    def test_game_check(self):
        with tempfile.TemporaryDirectory() as temp:
            game = Path(temp)
            self.assertFalse(doctor.check_game(game).ok)
            (game / doctor.GAME_EXE).write_bytes(b'')
            data = game / 'TheDrifter_Data'
            (data / 'Managed').mkdir(parents=True)
            (data / 'globalgamemanagers').write_bytes(bytes(48) + b'2019.4.1f1\0' + bytes(16))
            self.assertIn('2019.4.1f1', doctor.check_game(game).detail)
            (data / 'globalgamemanagers').write_bytes(bytes(48) + doctor.SOURCE_VERSION.encode() + bytes(16))
            self.assertIn('Assembly-CSharp', doctor.check_game(game).detail)
            (data / 'Managed' / 'Assembly-CSharp.dll').write_bytes(b'')
            self.assertTrue(doctor.check_game(game).ok)


class VitaSdk(unittest.TestCase):
    def test_find_sdk_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertIsNone(doctor.find_sdk_root(root))
            sdk = root / 'extracted' / 'sdk'
            for tool in doctor.SDK_TOOLS:
                (sdk / tool).parent.mkdir(parents=True, exist_ok=True)
                (sdk / tool).write_bytes(b'')
            (root / 'extracted' / 'Tools').mkdir()
            self.assertEqual(doctor.find_sdk_root(root), sdk)
            self.assertEqual(doctor.find_sdk_root(sdk), sdk)
            self.assertIsNone(doctor.find_sdk_root(root, depth=1))
            self.assertEqual(doctor.missing(sdk, doctor.SDK_TOOLS), [])
            (sdk / doctor.SDK_TOOLS[1]).unlink()
            self.assertEqual(doctor.missing(sdk, doctor.SDK_TOOLS), [doctor.SDK_TOOLS[1]])

    def test_extract_command(self):
        archive, out = Path(r'D:\sdk.7z'), Path(r'C:\vitasdk')
        self.assertIsNone(setup.extract_command(Path(r'D:\sdk.ZIP'), out, '7z.exe', 'tar.exe'))
        self.assertEqual(
            setup.extract_command(archive, out, '7z.exe', 'tar.exe'),
            ['7z.exe', 'x', '-y', '-bso0', r'-oC:\vitasdk', r'D:\sdk.7z'],
        )
        self.assertEqual(
            setup.extract_command(archive, out, None, 'tar.exe'), ['tar.exe', '-xf', r'D:\sdk.7z', '-C', r'C:\vitasdk']
        )
        with self.assertRaises(SystemExit):
            setup.extract_command(archive, out, None, None)

    def test_msi_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in ('Tools/TMServer-1.msi', 'a/b/c/PublishingTools.MSI', 'a/b/c/d/deep.msi', 'readme.txt'):
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_bytes(b'')
            found = [p.relative_to(root).as_posix() for p in setup.msi_files([root, root / 'Tools'], depth=3)]
            self.assertEqual(found, ['Tools/TMServer-1.msi', 'a/b/c/PublishingTools.MSI'])

    def test_our_sdk(self):
        """Only a folder setup created or filled is marked; pipeline.py clean deletes what it lists."""
        from types import SimpleNamespace
        from unittest import mock

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'mine' / 'sdk'
            for tool in doctor.SDK_TOOLS:
                (source / tool).parent.mkdir(parents=True, exist_ok=True)
                (source / tool).write_bytes(b'')
            for name, ours in (('new', True), ('busy', False)):
                destination = Path(temp) / name
                if not ours:
                    destination.mkdir()
                    (destination / 'other.txt').write_bytes(b'')
                args = SimpleNamespace(vita_sdk_dir=str(destination), dry_run=False)
                with mock.patch('builtins.print'):
                    sdk = setup.Setup(args).install_sdk(source.parent)
                self.assertEqual((destination / setup.SDK_MARKER).exists(), ours)
                with mock.patch.object(setup, 'setting', return_value=str(sdk)):
                    self.assertEqual(setup.our_sdk(), destination if ours else None)
                if ours:  # only what setup put there
                    (destination / 'later.txt').write_bytes(b'')
                    self.assertEqual(setup.sdk_parts(destination), ([destination / 'sdk'], True))
            self.assertFalse(setup.setup_may_own(Path(Path(temp).anchor)))  # never a drive's root
            self.assertTrue(setup.setup_may_own(Path(temp) / 'nothing-here-yet'))

    def test_downloads(self):
        self.assertEqual(set(doctor.SCE_INSTALLER_DOWNLOADS), set(doctor.SCE_INSTALLERS))
        for url, digest in [doctor.VITA_SDK_ZIP, *doctor.VITA_SDK_PATCHES, *doctor.SCE_INSTALLER_DOWNLOADS.values()]:
            self.assertTrue(url.startswith('https://archive.org/download/'))
            self.assertRegex(digest, r'^[0-9a-f]{40}$')
        # A SHA-1 pin (archive.org's) is checked like a SHA-256 one.
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'PSVITA.zip'
            source.write_bytes(b'sdk')
            sha1 = common.file_hash(source, 'sha1')
            self.assertEqual(common.download(source.as_uri(), Path(temp) / 'a.zip', sha1).read_bytes(), b'sdk')
            with self.assertRaises(SystemExit):
                common.download(source.as_uri(), Path(temp) / 'b.zip', '0' * 40)
            self.assertFalse((Path(temp) / 'b.zip.part').exists())


class Signatures(unittest.TestCase):
    SUBJECT = 'CN=Unity Technologies Aps, OU=Core Developer Services, O=Unity Technologies Aps, L=Kobenhavn K, C=DK'

    def test_unity_signer(self):
        self.assertTrue(doctor.signer_is_unity('Valid', self.SUBJECT))
        self.assertTrue(doctor.signer_is_unity('Valid', 'CN=Unity Technologies SF, O=Unity Technologies SF, C=US'))
        self.assertFalse(doctor.signer_is_unity('HashMismatch', self.SUBJECT))
        self.assertFalse(doctor.signer_is_unity('NotSigned', ''))
        self.assertFalse(doctor.signer_is_unity('Valid', 'CN=Unity Technologies, O=Someone Else'))
        self.assertFalse(doctor.signer_is_unity('Valid', 'CN=x, O=Unity Technologies Fan Club'))

    def test_microsoft_signature(self):
        microsoft = 'CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US'
        self.assertTrue(doctor.signer_is_microsoft('Valid', microsoft))
        self.assertFalse(doctor.signer_is_microsoft('HashMismatch', microsoft))
        self.assertFalse(doctor.signer_is_microsoft('Valid', 'CN=x, O=Microsoft Corporation Fans'))

    def test_vc_runtime(self):
        found = doctor.vc_runtime()  # None on a Windows without the runtime
        if found:
            self.assertEqual(found[0], 14)


class License(unittest.TestCase):
    def test_log(self):
        good = (
            'LICENSE SYSTEM [2026109 0:59:24] Next license update check is after 2026-10-05\n'
            'Register platform support module: C:/Unity/Editor/Data/PlaybackEngines/PSP2Player/x.dll\n'
        )
        self.assertIsNone(doctor.license_log_problem(good, 0, 'vita'))
        self.assertIsNone(doctor.license_log_problem('started\n', 0, 'windows'))
        self.assertIn('module', doctor.license_log_problem('started\n', 0, 'vita'))
        self.assertIn('code 1', doctor.license_log_problem('started\n', 1, 'windows'))
        self.assertIn(
            'No valid Unity Editor license',
            doctor.license_log_problem('x\nNo valid Unity Editor license found. Please activate.\n', 1, 'windows'),
        )


class Checklist(unittest.TestCase):
    def test_summarize(self):
        checks = [
            Check('Editor', True, 'C:/Unity'),
            Check('SDK', False, 'not set', 'setup.py --vita-sdk x'),
            Check('Node.js', False, 'not found', 'winget install node', required=False),
        ]
        lines, failed = doctor.summarize(checks)
        self.assertEqual(failed, 1)
        self.assertEqual(lines[0], '  [   ok   ] Editor   C:/Unity')
        self.assertTrue(lines[1].startswith('  [MISSING ] SDK'))
        self.assertTrue(lines[2].strip() == 'fix: setup.py --vita-sdk x')
        self.assertTrue(lines[3].startswith('  [optional] Node.js'))
        self.assertEqual(doctor.summarize([Check('a', True)])[1], 0)

    def test_env(self):
        self.assertEqual(common.effective_env('a', 'b', 'c'), ('a', False))
        self.assertEqual(common.effective_env(None, 'b', 'c'), ('b', True))
        self.assertEqual(common.effective_env('', None, 'c'), ('c', True))
        self.assertEqual(common.effective_env(None, None, None), (None, False))

    def test_space(self):
        for target in doctor.TARGETS:
            self.assertGreater(doctor.space_needed(target, False), doctor.space_needed(target, True))
        self.assertGreater(doctor.space_needed('vita', False), doctor.space_needed('windows', False))

    def test_stage_checks(self):
        names = lambda *a: [c.name for c in doctor.stage_checks(*a, game=Path('nowhere'))]  # noqa: E731
        rip = names('rip', 'windows', 'mono')
        self.assertIn('AssetRipper 2.0.0', rip)
        self.assertNotIn('Unity 2018.2.19f1 editor', rip)
        build = names('build', 'vita', 'il2cpp')
        self.assertIn("Unity's files", build)  # the PS Vita build runs no editor
        self.assertNotIn('Unity 2018.2.19f1 editor', build)
        self.assertIn('PS Vita SDK', build)
        self.assertNotIn('AssetRipper 2.0.0', build)
        unity = [c.name for c in doctor.stage_checks('build', 'vita', 'il2cpp', game=Path('nowhere'), unity=True)]
        self.assertIn('PS Vita module', unity)
        self.assertIn('Unity 2018.2.19f1 editor', unity)
        # Only the editor's PS Vita build (--unity) needs Target Manager Server and Publishing Tools.
        manager = lambda unity: next(  # noqa: E731
            c
            for c in doctor.stage_checks('build', 'vita', 'il2cpp', game=Path('nowhere'), unity=unity)
            if c.name.startswith('Target Manager')
        )
        self.assertFalse(manager(False).required)
        self.assertTrue(manager(True).required)
        everything = names('all', 'windows', 'il2cpp')
        self.assertEqual(len(everything), len(set(everything)))
        self.assertIn('Visual Studio C++ tools', everything)
        self.assertNotIn('Assembled project', everything)
        self.assertIn('DRIFTER_VITA_HOST', names('deploy', 'vita', 'il2cpp'))
        self.assertEqual(doctor.target_of('vita', 'mono'), 'vita')
        self.assertEqual(doctor.target_of('windows', 'il2cpp'), 'il2cpp')


if __name__ == '__main__':
    unittest.main()
