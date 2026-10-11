"""Read the parameter table of a GXP program (layout from Vita3K's gxm types)."""

import struct

CAT = {0: 'attr', 1: 'uniform', 2: 'sampler', 3: 'aux', 4: 'ubuf'}
TYPE = {0: 'f32', 1: 'f16', 2: 'c10', 3: 'u32', 4: 's32', 5: 'u16', 6: 's16', 7: 'u8', 8: 's8', 9: 'aggr'}


def params(g):
    count, off = struct.unpack_from('<II', g, 0x24)
    base = 0x28 + off
    out = []
    for i in range(count):
        p = base + 16 * i
        name_off, bits, sem, semidx, arr, res = struct.unpack_from('<iHBBIi', g, p)
        e = g.index(b'\0', p + name_off)
        out.append(
            {
                'i': i,
                'name': g[p + name_off : e].decode(),
                'cat': CAT[bits & 15],
                'type': TYPE.get((bits >> 4) & 15),
                'comps': (bits >> 8) & 15,
                'container': (bits >> 12) & 15,
                'sem': sem,
                'semidx': semidx & 15,
                'arr': arr,
                'res': res,
            }
        )
    return out
