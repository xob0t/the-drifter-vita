"""Read the parity probe's run logs: heartbeats in log.txt and room changes in events.txt.

    uv run tools/downgrade/heartbeats.py summary <run dir or tag>... [--window a-b]
    uv run tools/downgrade/heartbeats.py compare <run dir or tag>[:t0-t1]...

A run is a folder with the probe's log.txt (and events.txt): artifacts/vita/runs/<tag> (vita_run.py),
artifacts/vitalike/<tag> (vitalike.py) or any other path. The probe writes a heartbeat every 5 s
(diag/Remote.cs Heartbeat: "heartbeat frame N fps F renderMs R otherMs O scene S ... cams ...
markers ... scriptsMs ... loop ... allocB ... gcs N gcAllocB B"). The patterns the tools read from
the probe's logs are kept here (HEARTBEAT_SECTIONS and below).

summary: one line per run, the means of the heartbeats in the last scene (the first one after the
scene's load left out), or of those inside --window a-b (seconds after that scene's load, e.g.
20-45 for a press at 15-45), with the restore and each -diag-goto-room change (transition start to
room-ready) from events.txt. Vita and Vita-like runs print the same way.
compare: a table of every heartbeat value's mean, one column per run, optionally within t0-t1
seconds of the log's clock.
"""

import itertools
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN_FOLDERS = [ROOT / 'artifacts' / 'vita' / 'runs', ROOT / 'artifacts' / 'vitalike']

LOOP_KEYS = [
    ('Update.ScriptRunBehaviourUpdate', 'update'),
    ('PreLateUpdate.ScriptRunBehaviourLateUpdate', 'late'),
    ('PreLateUpdate.DirectorUpdateAnimationBegin', 'animB'),
    ('PreLateUpdate.DirectorUpdateAnimationEnd', 'animE'),
    ('PreLateUpdate.ParticleSystemBeginUpdateAll', 'partB'),
    ('PostLateUpdate.ParticleSystemEndUpdateAll', 'partE'),
    ('PostLateUpdate.PlayerSendFrameComplete', 'sendFC'),
    ('PostLateUpdate.FinishFrameRendering', 'finish'),
]


def run_dir(name):
    """A run folder given as a path, or as a tag of artifacts/vita/runs or artifacts/vitalike."""
    path = Path(name)
    if path.exists():
        return path
    return next((folder / name for folder in RUN_FOLDERS if (folder / name).exists()), RUN_FOLDERS[-1] / name)


# The heartbeat's list sections, in the order the probe writes them; each runs up to the next one
# present (markers has a " scripts" part of its own). A section that failed reads
# "<section> error <exception>".
HEARTBEAT_SECTIONS = ('cams', 'markers', 'scriptsMs', 'loop', 'allocB', 'gcs')
SECTION_START = re.compile(r' (' + '|'.join(HEARTBEAT_SECTIONS) + r')(?= |$)')
HEARTBEAT_LINE = re.compile(r'\[([\d.]+)\] heartbeat (.*)')
# The heartbeat's single numbers (key value) that heartbeats() reads.
HEARTBEAT_NUMBERS = ('fps', 'renderMs', 'otherMs', 'flushMs', 'managedMB', 'freeLPDDR', 'freeCDRAM', 'gcs', 'gcAllocB')
# Other lines of the probe's log.txt the tools read: the flags it started with, the save folder (in
# its "system ... | saves <dir> | ..." line), each change of the active scene, the scenes' loads
# (also in events.txt), and the save restore's end (events.txt too).
ARGS_LINE = re.compile(r'\] args (.*)')
SAVES = re.compile(r'\| saves (\S+)')
SCENE_LINE = re.compile(r'\] scene (\S+)')
SCENE_LOADED = re.compile(r'\[([\d.]+)\] sceneLoaded (\S+)')
RESTORE_RETURNED = 'restore-returned'


def sections(body):
    """A heartbeat's text split at its list sections: {'': the part before them, section: its text}."""
    starts = list(SECTION_START.finditer(body))
    parts = {'': body[: starts[0].start()] if starts else body}
    for start, following in itertools.pairwise([*starts, None]):
        parts[start.group(1)] = body[start.end() : following.start() if following else len(body)]
    return parts


def without_cams(line):
    """A heartbeat line without its cams section (profile_room.py prints them so)."""
    return re.sub(r' cams .*?(?=' + SECTION_START.pattern + r'|$)', '', line, count=1)


def heartbeats(log_text):
    """The heartbeats of a log (numbers with points), as dicts: t (log seconds), scene and the
    HEARTBEAT_NUMBERS when present, loop {step: ms}, scriptsMs and scripts {method: ms} with
    -diag-time-scripts, cams {camera: (ms, extra ms)}."""
    beats = []
    for line in log_text.splitlines():
        match = HEARTBEAT_LINE.match(line)
        if not match:
            continue
        body = match.group(2)
        beat = {'t': float(match.group(1))}
        scene = re.search(r'(?:^| )scene (\S+)', body)
        if scene:
            beat['scene'] = scene.group(1)
        for key in HEARTBEAT_NUMBERS:
            found = re.search(r'(?:^| )' + key + r' (-?[\d.]+)(?= |$)', body)
            if found:
                beat[key] = float(found.group(1))
        parts = sections(body)
        beat['loop'] = {k: float(v) for k, v in re.findall(r'(\S+):([\d.]+)', parts.get('loop', ''))}
        scripts = re.match(r' ([\d.]+)(.*)', parts.get('scriptsMs', ''))
        if scripts:
            beat['scriptsMs'] = float(scripts.group(1))
            beat['scripts'] = {k: float(v) for k, v in re.findall(r'(\S+?):([\d.]+)/', scripts.group(2))}
        beat['cams'] = {
            k: (float(a), float(c)) for k, a, c in re.findall(r'(\S+):([\d.]+)\+([\d.]+)', parts.get('cams', ''))
        }
        beats.append(beat)
    return beats


def scene_loads(log_text):
    return [(float(t), scene) for t, scene in SCENE_LOADED.findall(log_text)]


def pick(beats, loads, window):
    """The heartbeats a summary averages: see the module's docstring."""
    if not beats:
        return []
    last_scene = beats[-1].get('scene')
    if window:
        start, end = window
        loaded = [t for t, scene in loads if scene == last_scene]
        if not loaded:
            return []
        base = loaded[-1]
        return [b for b in beats if base + start + 5 <= b['t'] + 0.01 and b['t'] <= base + end + 0.5]
    chosen = []
    for previous, beat in itertools.pairwise(beats):
        if beat.get('scene') == last_scene and previous.get('scene') == last_scene and beat.get('fps', 0) >= 5:
            chosen.append(beat)
    # Drop the first heartbeat after the last scene's load as well (the load's tail).
    later = [t for t, scene in loads if scene == last_scene]
    if later and chosen and chosen[0]['t'] < later[-1] + 6:
        chosen = chosen[1:]
    return chosen


def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else None


# events.txt (diag/Diag.cs Event): a -diag-goto-room change started, its room ready (ms since the
# transition's start), and the save restore's time.
GOTO_OK = re.compile(r' goto (\S+) ok')
ROOM_READY = re.compile(r' room-ready \+(\d+)ms')
RESTORE_TIME = re.compile(RESTORE_RETURNED + r' \S+ after (\d+)ms')


def room_changes(events_text):
    """([(room, transition start to room-ready ms)], save restore ms or None) from events.txt."""
    changes = []
    target = None
    for line in events_text.splitlines():
        goto = GOTO_OK.search(line)
        if goto:
            target = goto.group(1)
        ready = ROOM_READY.search(line)
        if ready and target:
            changes.append((target, int(ready.group(1))))
            target = None
    restore = RESTORE_TIME.search(events_text)
    return changes, int(restore.group(1)) if restore else None


def summary(run: Path, window=None):
    log = run / 'log.txt'
    if not log.exists():
        return f'{run.name:28} no log.txt'
    text = log.read_text(errors='replace')
    every = heartbeats(text)
    beats = pick(every, scene_loads(text), window)
    parts = [f'{run.name:28}']
    if not every:
        parts.append('no heartbeats in log.txt')
    elif not beats:
        scene = every[-1].get('scene', '?')
        parts.append(f'no heartbeats in {scene} inside --window' if window else f'no steady heartbeats in {scene}')
    else:
        parts.append(f'n {len(beats)}')
        for key in ('fps', 'renderMs', 'otherMs'):
            value = mean(b[key] for b in beats if key in b)
            if value is not None:
                parts.append(f'{key} {value:.1f}')
        for key, label in LOOP_KEYS:
            values = [b['loop'].get(key, 0.0) for b in beats if b['loop']]
            if values and max(values) > 0:
                parts.append(f'{label} {mean(values):.2f}')
        if any('scriptsMs' in b for b in beats):
            parts.append(f'scriptsMs {mean(b.get("scriptsMs", 0) for b in beats):.1f}')
            totals = {}
            for b in beats:
                for name, ms in b.get('scripts', {}).items():
                    totals[name] = totals.get(name, 0) + ms / len(beats)
            top = sorted(totals.items(), key=lambda e: -e[1])[:4]
            parts.append('top ' + ' '.join(f'{n}:{ms:.2f}' for n, ms in top))
        if any('gcs' in b for b in beats):
            gcs = sum(b.get('gcs', 0) for b in beats)
            parts.append(f'gcs {gcs:.0f} allocB {mean(b["gcAllocB"] for b in beats if "gcAllocB" in b):.0f}')
        parts.append(f'scene {beats[-1].get("scene", "?").replace("SceneRoom", "")}')
    events = run / 'events.txt'
    if events.exists():
        changes, restore = room_changes(events.read_text(errors='replace'))
        if restore is not None:
            parts.append(f'restore {restore}ms')
        if changes:
            parts.append('rooms ' + ' '.join(f'{room}:{ms}' for room, ms in changes))
    return ' | '.join(parts)


def parse_window(text):
    if not text:
        return None
    start, end = text.split('-')
    return float(start), float(end)


def flat(beat):
    """A heartbeat's numbers under one key each, for compare."""
    values = {k: v for k, v in beat.items() if isinstance(v, float) and k not in ('t', 'gcs')}
    values.update({f'cam {k}': ms for k, (ms, _) in beat['cams'].items()})
    values.update({f'cam+ {k}': extra for k, (_, extra) in beat['cams'].items()})
    values.update({f'L {k}': ms for k, ms in beat['loop'].items()})
    values.update({f'S {k}': ms for k, ms in beat.get('scripts', {}).items()})
    return values


def compare(specs):
    runs, notes = [], []
    for spec in specs:
        name, _, window = spec.partition(':')
        t0, t1 = parse_window(window) or (0, 1e9)
        log = run_dir(name) / 'log.txt'
        beats = heartbeats(log.read_text(errors='replace')) if log.exists() else []
        runs.append((spec, [flat(b) for b in beats if t0 <= b['t'] <= t1]))
        if not runs[-1][1]:
            notes.append(
                ' (no log.txt)'
                if not log.exists()
                else ' (no heartbeats in log.txt)'
                if not beats
                else ' (none inside the window)'
            )
        else:
            notes.append('')
    keys = list(dict.fromkeys(k for _, beats in runs for b in beats for k in b))

    def average(beats, key):
        return sum(b.get(key, 0.0) for b in beats) / len(beats) if beats else 0.0

    order = [k for k in ('fps', 'renderMs', 'otherMs', 'flushMs', 'scriptsMs') if k in keys]
    rest = sorted(
        (k for k in keys if k not in order), key=lambda k: (k.split(' ')[0], -max(average(bs, k) for _, bs in runs))
    )
    print(f'{"n":<48}', ' '.join(f'{len(bs):10d}' for _, bs in runs))
    for key in order + rest:
        values = [average(bs, key) for _, bs in runs]
        if max(values) >= 0.05:
            print(f'{key[:48]:<48}', ' '.join(f'{v:10.2f}' for v in values))
    for i, ((spec, _), note) in enumerate(zip(runs, notes, strict=True)):
        print(i, spec + note)


def main(argv):
    if argv[:1] == ['summary'] and len(argv) > 1:
        window = parse_window(argv[argv.index('--window') + 1]) if '--window' in argv else None
        names = [a for i, a in enumerate(argv[1:], 1) if a != '--window' and argv[i - 1] != '--window']
        for name in names:
            print(summary(run_dir(name), window))
    elif argv[:1] == ['compare'] and len(argv) > 1:
        compare(argv[1:])
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main(sys.argv[1:])
