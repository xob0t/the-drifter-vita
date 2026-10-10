"""Derive Unity 2018.2's PSP2 binding data for one compiled program from its GXP parameter table."""

import struct

from gxp import params

# GXP vertex attribute semantic -> Unity shader channel
SEM_CHANNEL = {11: 0, 9: 1, 15: 2, 6: 3}  # POSITION, NORMAL, TANGENT, COLOR; TEXCOORDn -> 4+n


def channel(p):
    if p['sem'] == 14:
        return 4 + p['semidx']
    return SEM_CHANNEL[p['sem']]


def target(p, semantic):
    # TEXCOORD with no number binds Unity's generic texcoord component, TEXCOORDn the numbered one
    if p['sem'] == 14:
        return 4 if semantic.upper() == 'TEXCOORD' else 5 + p['semidx']
    return SEM_CHANNEL[p['sem']]


def derive(g, semantics=None):
    gp = params(g)
    attrs = [p for p in gp if p['cat'] == 'attr']
    chans = sorted({(channel(p), target(p, (semantics or {}).get(p['name'], 'TEXCOORD'))) for p in attrs})
    src_map = sum(1 << c for c, _ in chans)
    vectors, matrices, textures, uniforms = [], [], [], []
    for p in reversed(gp):
        if p['cat'] == 'uniform':
            idx = len(uniforms)
            uniforms.append(p['name'])
            if p['comps'] == 4 and p['arr'] == 4:
                matrices.append({'name': p['name'], 'index': idx, 'rows': 4, 'cols': 4, 'arr': 0})
            else:
                vectors.append(
                    {
                        'name': p['name'],
                        'index': idx,
                        'rows': 1,
                        'cols': 1 if p['comps'] == 1 else 4,
                        'arr': 0 if p['arr'] == 1 else p['arr'],
                    }
                )
        elif p['cat'] == 'sampler':
            textures.append({'name': p['name'], 'index': p['res'], 'sampler': p['res']})
        elif p['cat'] != 'attr':
            raise ValueError(p)
    return {
        'chans': chans,
        'src_map': src_map,
        'vectors': vectors,
        'matrices': matrices,
        'textures': textures,
        'uniforms': uniforms,
    }


def lstr(s, pad=True):
    b = s.encode()
    return struct.pack('<I', len(b)) + b + b'\0' * ((-len(b)) % 4 if pad else 0)


def code_bytes(g, b):
    extra = b''.join(struct.pack('<II', 0, len(n) + 1) + n.encode() + b'\0' for n in b['uniforms'])
    return struct.pack('<III', b['src_map'], len(g), len(extra)) + g + extra


def tail_bytes(b):
    out = struct.pack('<II', b['src_map'], len(b['chans']))
    out += b''.join(struct.pack('<ii', c, t) for c, t in b['chans'])
    out += struct.pack('<III', 1, 0, 0)
    consts = b['vectors'] + b['matrices']
    out += struct.pack('<I', len(consts))
    for c in consts:
        out += lstr(c['name']) + struct.pack(
            '<6i', 0, c['rows'], c['cols'], 1 if c in b['matrices'] else 0, c['arr'], c['index']
        )
    out += struct.pack('<II', 0, len(b['textures']))
    for t in b['textures']:
        out += lstr(t['name']) + struct.pack('<4i', 0, t['index'], t['sampler'], 4)
    return out


def unity_lists(b, nameidx):
    """The m_VectorParams / m_MatrixParams / m_TextureParams of the parsed form, given a name -> index map."""
    return {
        'm_VectorParams': [
            {
                'm_NameIndex': nameidx[v['name']],
                'm_Index': v['index'],
                'm_ArraySize': v['arr'],
                'm_Type': 0,
                'm_Dim': v['cols'],
            }
            for v in b['vectors']
        ],
        'm_MatrixParams': [
            {
                'm_NameIndex': nameidx[m['name']],
                'm_Index': m['index'],
                'm_ArraySize': m['arr'],
                'm_Type': 0,
                'm_RowCount': 4,
            }
            for m in b['matrices']
        ],
        'm_TextureParams': [
            {
                'm_NameIndex': nameidx[t['name']],
                'm_Index': t['index'],
                'm_SamplerIndex': t['sampler'],
                'm_MultiSampled': False,
                'm_Dim': 2,
            }
            for t in b['textures']
        ],
        'm_Channels': {'m_Channels': [{'source': c, 'target': t} for c, t in b['chans']], 'm_SourceMap': b['src_map']},
    }


def attribute_semantics(sdb):
    """symbolName -> declared binding string ('TEXCOORD', 'TEXCOORD0', ...) from psp2cgc's XML SDB."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(sdb)

    def strip(t):
        return t.split('}')[-1]

    decls, records = {}, {}
    for el in root.iter():
        tag = strip(el.tag)
        if tag in ('paramdecl', 'vardecl') and el.get('declID'):
            decls[el.get('declID')] = el
        if tag == 'recorddecl':
            records.setdefault(el.get('name'), el)
    out = {}
    for el in root.iter():
        if strip(el.tag) != 'attribute':
            continue
        sym = el.get('symbolName')
        d = decls[el.get('declID')]
        parts = sym.split('.')
        if len(parts) == 1:
            out[sym] = d.get('binding')
            continue
        typ = d.get('type')
        for name in parts[1:]:
            rec = records[typ]
            f = next(f for f in rec if strip(f.tag) == 'field' and f.get('name') == name)
            typ = f.get('type')
            binding = f.get('binding')
        out[sym] = binding
    return out
