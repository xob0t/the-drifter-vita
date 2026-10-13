"""Checks of the progress display's helpers and of what is written under it: no terminal needed.

uv run python -m unittest discover tools/downgrade/tests
"""

import contextlib
import io
import os
import random
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import doctor  # noqa: E402
import pipeline  # noqa: E402
import progress  # noqa: E402
from rich.console import Console  # noqa: E402


class LastLine(unittest.TestCase):
    def last(self, data, start=0, tail=None):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'tool.log'
            log.write_bytes(data)
            with mock.patch.object(progress, 'TAIL_BYTES', tail or progress.TAIL_BYTES):
                return progress.last_line(log, start)

    def test_lines(self):
        self.assertEqual(self.last(b''), '')
        self.assertEqual(self.last(b'one\r\ntwo\r\n\r\n'), 'two')
        self.assertEqual(self.last(b'one\ntwo'), 'two')  # still being written
        self.assertEqual(self.last(b'ab', start=3), '')  # shorter than the header it skips
        self.assertEqual(self.last(b'command line\n\n', start=14), '')  # only the header

    def test_window(self):
        # The read starts within a line: that line is dropped, unless it is all there is.
        self.assertEqual(self.last(b'x' * 20 + b'\nlast\n', tail=8), 'last')
        self.assertEqual(self.last(b'head\n' + b'c' * 20 + b'\n', tail=8), 'c' * 7)
        self.assertEqual(self.last(b'head\n' + b'c' * 20 + b'\n\n', tail=8), 'c' * 6)
        # The read starts on a line: that line is whole.
        self.assertEqual(self.last(b'a' * 4 + b'\n' + b'b' * 7 + b'\n', tail=8), 'b' * 7)

    def test_cut(self):
        line = self.last(b'y' * 200 + b'\n')
        self.assertEqual(len(line), progress.NOTE_WIDTH)
        self.assertTrue(line.endswith('...'))


class Lines(unittest.TestCase):
    def printed(self, function, *args, **kwargs):
        out = io.StringIO()
        with mock.patch.object(progress, 'console', Console(file=out, width=200, soft_wrap=True)):
            function(*args, **kwargs)
        return out.getvalue()

    def test_written(self):
        name = 'sharedassets1.assets'
        self.assertEqual(
            self.printed(progress.written, name, 23640868, 182.06), f'{name:<34}   23,640,868 bytes   182.1 s\n'
        )
        self.assertEqual(self.printed(progress.written, name, 5), f'{name:<34}            5 bytes\n')  # no spaces after
        noted = self.printed(progress.written, name, 5, note='identical', missing={114: 2})
        self.assertEqual(noted, f'{name:<34}            5 bytes            identical  not built yet: {{114: 2}}\n')

    def test_no_markup(self):
        self.assertEqual(self.printed(progress.written, '[red]x[/]', 1).split()[0], '[red]x[/]')
        self.assertEqual(self.printed(progress.done, 'Built [b]y'), 'Built [b]y\n')

    def test_doctor_show(self):
        lines, failed = doctor.summarize(
            [doctor.Check('Game', True, 'D:/x'), doctor.Check('SDK', False, 'none', 'run setup')]
        )
        console = Console(file=io.StringIO(), width=200, force_terminal=True, color_system='standard')
        with console.capture() as captured:
            doctor.show(console, *lines)
        self.assertEqual(failed, 1)
        # The status tags, and only they, get the colors.
        self.assertIn('\x1b[32m[   ok   ]\x1b[0m Game', captured.get())
        self.assertIn('\x1b[1;31m[MISSING ]\x1b[0m SDK', captured.get())
        self.assertIn('fix: run setup', captured.get())


class Package(unittest.TestCase):
    def test_same_as_zipfile_write(self):
        """package_vita writes each file a piece at a time; the entries are those ZipFile.write writes."""
        with tempfile.TemporaryDirectory() as folder:
            build = Path(folder) / 'drifter-vita'
            (build / 'Media').mkdir(parents=True)
            (build / 'x.self').write_bytes(b'self')
            rnd = random.Random(1)
            files = []
            for name, size in [('a.bin', 0), ('Media/b.bin', pipeline.PACKAGE_CHUNK + 77), ('Media/c.txt', 5000)]:
                path = build / name
                path.write_bytes(rnd.randbytes(size // 2) + b'text ' * (size - size // 2) if size else b'')
                os.utime(path, (1700000000, 1700000000))
                files.append((name, path))
            items = [('eboot.bin', b'player' * 1000), *files]
            with (
                mock.patch.object(pipeline, 'vita_app_files', lambda _: iter(items)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                vpk = pipeline.package_vita(build)
            old = Path(folder) / 'old.vpk'
            with zipfile.ZipFile(old, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name, source in items:
                    archive.writestr(name, source) if isinstance(source, bytes) else archive.write(source, name)

            def entries(path):
                with zipfile.ZipFile(path) as z:
                    return [
                        (i.filename, i.CRC, i.compress_size, i.file_size, i.flag_bits, i.external_attr)
                        + ((i.date_time,) if i.filename != 'eboot.bin' else ())  # written now
                        + (z.read(i),)
                        for i in z.infolist()
                    ]

            self.assertEqual(entries(vpk), entries(old))
            self.assertEqual(vpk.stat().st_size, old.stat().st_size)


if __name__ == '__main__':
    unittest.main()
