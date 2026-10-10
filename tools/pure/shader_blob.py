"""Assemble Unity 2018.2's per-platform shader blob (before LZ4)."""

import struct

VERSION = 201802150


def lstr(s):
    b = s.encode()
    return struct.pack('<I', len(b)) + b + b'\0' * ((-len(b)) % 4)


def subprogram(gpu_type, keywords, code, tail):
    out = struct.pack('<Ii4I', VERSION, gpu_type, 0, 0, 0, 0)
    out += struct.pack('<I', len(keywords)) + b''.join(lstr(k) for k in keywords)
    out += struct.pack('<I', len(code)) + code + b'\0' * ((-len(code)) % 4)
    return out + tail


def blob(subs):
    head = 4 + 8 * len(subs)
    out, table, off = b'', b'', head
    for s in subs:
        table += struct.pack('<II', off, len(s))
        out += s
        off += len(s)
    return struct.pack('<I', len(subs)) + table + out
