"""What the tools show while they work: a bar for each step that takes a while, a line for each thing done.

    with progress.display():                      # make.py, build.py: one display for the whole build
        with progress.step('Compiling shaders', total=len(shaders), eta=True) as step:
            for s in shaders:
                step.note(s.name)                 # shown after the step's name
                ...
                step.advance()
        for o in progress.track(objects, 'Objects'):  # or a step for a list
            ...
        progress.written('level0', len(data), seconds, note)
    with progress.following('IL2CPP', log):       # a tool writing a log: its last line as the note
        subprocess.run(...)

A step outside display() shows a display of its own while it runs (check_audio.py calling
audio.build, setup.py's downloads), so the modules don't need to know who called them. The bars only
show on an interactive terminal and are gone when the step ends; the lines, and anything printed
meanwhile, stay above them, and are all that a redirected output gets. Lines are never wrapped, so a
log keeps them whole. A tool run under a step must not write to the terminal itself (its output to a
log file or captured): it would print through the bars.
"""

import contextlib
import datetime
import os
import re
import sys
import threading

from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, ProgressColumn, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Column
from rich.text import Text

console = Console(highlight=False, soft_wrap=True)
errors = Console(stderr=True, highlight=False, soft_wrap=True)  # for messages outside a display
NAME_WIDTH = 34  # written(): sharedassets59.assets.resS and Resources/unity_builtin_extra fit
NOTE_WIDTH = 70  # characters of a log's last line following() shows
POLL_SECONDS = 0.5  # how often following() reads the log
TAIL_BYTES = 4096  # how much of the log's end last_line() reads

_progress = None  # the display shown, if any


class _Count(ProgressColumn):
    """What the step has done, as its `count` asks: 'done/total', the MB with the speed, or nothing."""

    def render(self, task):
        count = task.fields.get('count')
        if count == 'MB':
            done = f'{task.completed / 2**20:.0f}' + ('' if task.total is None else f'/{task.total / 2**20:.0f}')
            speed = f', {task.speed / 2**20:.1f} MB/s' if task.speed else ''
            return Text(f'{done} MB{speed}', 'progress.download')
        if count == 'items' and task.total is not None:
            return Text(f'{task.completed:.0f}/{task.total:.0f}', 'progress.download')
        return Text('')


class _Left(ProgressColumn):
    """The time left, for a step that asked for it once it has done enough to tell."""

    def render(self, task):
        left = task.time_remaining
        if not task.fields.get('eta') or left is None or not task.completed:
            return Text('')
        return Text(f'{datetime.timedelta(seconds=round(left))} left', 'progress.remaining')


def _columns():
    """One line per step: the name and note give way (cut with …), then the bar. The time left keeps its
    width, so the bars don't move when a step that shows it starts or ends."""
    fixed = Column(no_wrap=True)
    return (
        SpinnerColumn(table_column=fixed),
        TextColumn('{task.description}', table_column=Column(no_wrap=True, overflow='ellipsis', ratio=3)),
        BarColumn(bar_width=None, table_column=Column(min_width=10, max_width=40, ratio=1)),
        _Count(table_column=fixed),
        TimeElapsedColumn(table_column=fixed),
        _Left(table_column=Column(no_wrap=True, min_width=len('0:00:00 left'))),
    )


@contextlib.contextmanager
def display():
    """Show the steps started inside; nested calls share the outer one's display."""
    global _progress
    if _progress is not None:
        yield
        return
    p = Progress(*_columns(), console=console, transient=True, expand=True)
    # Not started without a terminal to draw it on (it would leave an empty line when it stops), or one
    # that can't show its spinner (a forced terminal on a cp1252 stream).
    drawn = console.is_interactive and console.encoding.lower().startswith('utf')
    with p if drawn else contextlib.nullcontext():
        _progress = p
        try:
            yield
        finally:
            _progress = None


class Step:
    def __init__(self, progress, task, name):
        self.progress, self.task, self.name = progress, task, name

    def advance(self, n=1):
        self.progress.advance(self.task, n)

    def update(self, completed, total=None):
        self.progress.update(self.task, completed=completed, **({} if total is None else {'total': total}))

    def note(self, text):
        """What the step is on, after its name."""
        self.progress.update(self.task, description=f'{self.name} [dim]{escape(str(text))}[/]')


@contextlib.contextmanager
def step(name, total=None, eta=False, count='items'):
    """A bar for the block (a spinner without a total), removed when it ends. Yields a Step. eta:
    show the time left, for steps whose items take about as long as each other. count: what the bar
    shows it has done: 'items' (done/total), 'MB' (it counts bytes), or None (the total is a measure
    of the work, as the audio's frames)."""
    with display():
        p = _progress
        task = p.add_task(escape(name), total=total, eta=eta, count=count)
        # A first sample at the start: rich's speed leaves out the first sample's work. (A step whose total
        # comes later, as following()'s with a pattern, starts its samples over then.)
        if eta:
            p.advance(task, 0)
        try:
            yield Step(p, task, escape(name))
        finally:
            p.remove_task(task)


def track(items, name):
    """The items (a list), with a step for them that advances as each is taken."""
    with step(name, total=len(items)) as s:
        for item in items:
            yield item
            s.advance()


@contextlib.contextmanager
def following(name, log, start=0, pattern=None):
    """A step called `name` for a block that waits on a tool writing `log`: its note is the last line
    the log has after byte `start`, read every POLL_SECONDS. pattern: a regular expression whose
    groups are the items done and their total, for a tool that logs them (AssetRipper's export); the
    step then has a bar and the time left, and the note is the rest of the line."""
    with step(name, eta=pattern is not None) as s:
        stop = threading.Event()

        def poll():
            while not stop.wait(POLL_SECONDS):
                with contextlib.suppress(OSError):  # not created yet
                    line = last_line(log, start)
                    found = pattern and re.search(pattern, line)
                    if found:
                        s.update(int(found[1]), int(found[2]))
                        line = line[found.end() :].strip()
                    s.note(line)

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        try:
            yield s
        finally:
            stop.set()
            thread.join()


def last_line(log, start=0):
    """The last line written to log after byte `start`, cut to NOTE_WIDTH."""
    with open(log, 'rb') as f:
        end = f.seek(0, os.SEEK_END)
        at = max(start, end - TAIL_BYTES)
        f.seek(max(at - 1, start))
        within = at > start and f.read(1) != b'\n'  # the read starts within a line
        lines = f.read().decode('utf-8', errors='replace').splitlines()
    if within and any(x.strip() for x in lines[1:]):
        lines = lines[1:]
    line = next((x.strip() for x in reversed(lines) if x.strip()), '')
    return line if len(line) <= NOTE_WIDTH else line[: NOTE_WIDTH - 3] + '...'


def written(name, size, seconds=None, note='', missing=None):
    """The line for a file the build wrote: its size, how long it took, the --compare result and
    the classes it lacks ({class ID: objects}) because they cannot be built yet."""
    line = Text(f'{name:<{NAME_WIDTH}} {size:>12,} bytes')
    if seconds is not None:
        line.append(f'  {seconds:6.1f} s', 'dim')
    elif note or missing:
        line.append(' ' * 10)  # where the time would be, so the notes line up
    if note:
        style = 'green' if note == 'identical' else 'yellow'
        line.append(f'  {note}', style)
    if missing:
        line.append(f'  not built yet: {dict(missing)}', 'red')
    console.print(line)


def done(text):
    console.print(Text(text, 'bold green'))


def fail(text):
    """Stop with text, in red, on stderr (as sys.exit(text) would, without the color)."""
    errors.print(Text(text, 'bold red'))
    sys.exit(1)
