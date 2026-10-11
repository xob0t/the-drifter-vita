"""Read the reference build (what Unity made from the same project) for the checks."""

import struct

import lz4.block
import UnityPy
from paths import REFERENCE_MEDIA


def media_files():
    """The reference build's serialized files (not the .resS/.resource streams)."""
    skip = {'.resS', '.resource'}
    return [p for p in sorted(REFERENCE_MEDIA.iterdir()) if p.is_file() and p.suffix not in skip and '.' not in p.stem]


def parse_blob(data):
    """The subprogram records of a decompressed shader blob, in blob index order."""
    count = struct.unpack_from('<I', data, 0)[0]
    subs = []
    for i in range(count):
        offset, length = struct.unpack_from('<II', data, 4 + 8 * i)
        s = data[offset : offset + length]
        version, gpu_type = struct.unpack_from('<Ii', s, 0)
        keywords = []
        p = 28
        for _ in range(struct.unpack_from('<I', s, 24)[0]):
            n = struct.unpack_from('<I', s, p)[0]
            keywords.append(s[p + 4 : p + 4 + n].decode())
            p = (p + 4 + n + 3) & ~3
        code_length = struct.unpack_from('<I', s, p)[0]
        code = s[p + 4 : p + 4 + code_length]
        p = (p + 4 + code_length + 3) & ~3
        subs.append(
            {'version': version, 'type': gpu_type, 'keywords': keywords, 'code': code, 'tail': s[p:], 'record': s}
        )
    return subs


def shaders():
    """Every Shader in the reference build: file, path ID, type tree, decompressed blob, and its
    subprograms with their place in the parsed form (subshader, pass, stage)."""
    out = []
    for path in media_files():
        try:
            env = UnityPy.load(str(path))
        except Exception:
            continue
        for obj in env.objects:
            if obj.type.name != 'Shader':
                continue
            tree = obj.read_typetree()
            blob = bytes(tree['compressedBlob'])
            raw = lz4.block.decompress(
                blob[: tree['compressedLengths'][0]], uncompressed_size=tree['decompressedLengths'][0]
            )
            records = parse_blob(raw)
            form = tree['m_ParsedForm']
            programs = [
                dict(
                    sub=si,
                    passi=pi,
                    stage=stage,
                    sp=sp,
                    names=dict(shader_pass['m_NameIndices']),
                    **records[sp['m_BlobIndex']],
                )
                for si, subshader in enumerate(form['m_SubShaders'])
                for pi, shader_pass in enumerate(subshader['m_Passes'])
                for stage in ('progVertex', 'progFragment')
                for sp in shader_pass[stage]['m_SubPrograms']
            ]
            out.append(
                {
                    'file': path.name,
                    'pid': obj.path_id,
                    'name': form['m_Name'],
                    'tree': tree,
                    'raw': raw,
                    'programs': programs,
                }
            )
    return out
