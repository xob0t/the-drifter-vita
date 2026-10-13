"""The builder's progress display: a bar for each step that takes a while, a line for each file written.

    with progress.display():                      # make.py, build.py: one display for the whole build
        with progress.step('Shaders', total=len(shaders), eta=True) as step:
            for s in shaders:
                step.note(s.name)                 # shown after the step's name
                ...
                step.advance()
        for o in progress.track(objects, 'level0'):   # or a step for a list
            ...
        progress.written('level0', len(data), seconds, note)

A step outside display() shows a display of its own while it runs (check_audio.py calling
audio.build), so the modules don't need to know who called them. The bars only show on a terminal
and are gone when the build ends; the written() lines, and anything printed meanwhile, stay above
them, and are all that a redirected output gets.
"""

import contextlib

from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, ProgressColumn, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text

console = Console(highlight=False)
NAME_WIDTH = 34  # written(): sharedassets59.assets.resS and Resources/unity_builtin_extra fit

_progress = None  # the display shown, if any


class _Count(ProgressColumn):
    """'done/total', or nothing for a step without a total."""

    def render(self, task):
        return Text('' if task.total is None else f'{task.completed:.0f}/{task.total:.0f}', 'progress.download')


class _Left(ProgressColumn):
    """The time left, for a step that asked for it once it has done enough to tell."""

    def render(self, task):
        left = task.time_remaining
        if not task.fields.get('eta') or left is None or not task.completed:
            return Text('')
        minutes, seconds = divmod(round(left), 60)
        return Text(f'{minutes}:{seconds:02d} left', 'progress.remaining')


@contextlib.contextmanager
def display():
    """Show the steps started inside; nested calls share the outer one's display."""
    global _progress
    if _progress is not None:
        yield
        return
    columns = (SpinnerColumn(), TextColumn('{task.description}'), BarColumn(), _Count(), TimeElapsedColumn(), _Left())
    with Progress(*columns, console=console, transient=True) as p:
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

    def note(self, text):
        """What the step is on, after its name."""
        self.progress.update(self.task, description=f'{self.name} [dim]{escape(str(text))}[/]')


@contextlib.contextmanager
def step(name, total=None, eta=False):
    """A bar for the block (a spinner without a total), removed when it ends. Yields a Step. eta:
    show the time left, for steps whose items take about as long as each other."""
    with display():
        p = _progress
        task = p.add_task(name, total=total, eta=eta)
        try:
            yield Step(p, task, name)
        finally:
            p.remove_task(task)


def track(items, name):
    """The items (a list), with a step for them that advances as each is taken."""
    with step(name, total=len(items)) as s:
        for item in items:
            yield item
            s.advance()


def written(name, size, seconds=None, note='', missing=None):
    """The line for a file the build wrote: its size, how long it took, the --compare result and
    the classes it lacks ({class ID: objects}) because they cannot be built yet."""
    line = Text(f'{name:<{NAME_WIDTH}} {size:>12,} bytes')
    line.append(f'  {seconds:6.1f} s' if seconds is not None else ' ' * 10, 'dim')
    if note:
        style = 'green' if note == 'identical' else 'yellow'
        line.append(f'  {note}', style)
    if missing:
        line.append(f'  not built yet: {dict(missing)}', 'red')
    console.print(line)


def done(text):
    console.print(f'[bold green]{text}[/]')
