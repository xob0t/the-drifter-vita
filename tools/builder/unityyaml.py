"""Read Unity's YAML (scenes, prefabs, .asset/.mat/.anim files, ProjectSettings, .meta files).

Every scalar stays a string, so values like `Yes` or `0x10` are not reinterpreted; converters
turn them into numbers by the target type tree. Objects come back as Doc(class_id, file_id,
stripped, class_name, fields).
"""

import re
from dataclasses import dataclass

import yaml

HEADER = re.compile(r'^--- !u!(\d+) &(-?\d+)( stripped)?[^\n]*$', re.M)
Loader = getattr(yaml, 'CBaseLoader', yaml.BaseLoader)


@dataclass
class Doc:
    class_id: int
    file_id: int
    stripped: bool
    class_name: str
    fields: dict


def documents(text):
    """The objects of a Unity YAML file, in file order."""
    heads = list(HEADER.finditer(text))
    if not heads:
        return []
    # One load for the whole file: each header becomes a plain document separator.
    bodies = yaml.load_all(HEADER.sub('---', text[heads[0].start() :]), Loader=Loader)
    out = []
    for head, body in zip(heads, bodies, strict=False):
        ((class_name, fields),) = body.items() if body else (('', {}),)
        out.append(Doc(int(head[1]), int(head[2]), bool(head[3]), class_name, fields or {}))
    return out


def load(path):
    return documents(path.read_text(encoding='utf-8'))


def meta(path):
    """A .meta file as a plain dict."""
    return yaml.load(path.read_text(encoding='utf-8'), Loader=Loader)
