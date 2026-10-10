"""Models of the fields Unity's managed serializer writes for script classes.

typemodel/Program.cs reads the player's assemblies with Mono.Cecil; models() builds it on first
use with the pipeline's .NET 8 SDK and returns {(assembly, full name): model}. A model is a
'struct' node whose fields hold nodes of kind prim, string, pptr, array, struct, bitfield
(LayerMask) or curve (AnimationCurve).
"""

import hashlib
import json
import os
import subprocess

import common
from paths import WORK

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, 'typemodel')
DOTNET = common.EXTERNAL / 'dotnet' / 'dotnet.exe'
BUILD = WORK / 'typemodel'


def generator():
    files = [os.path.join(SOURCE, n) for n in ('Program.cs', 'Gen.csproj')]
    digest = hashlib.sha256(b''.join(open(f, 'rb').read() for f in files)).hexdigest()
    dll, stamp = BUILD / 'bin' / 'TypeModelGen.dll', BUILD / 'stamp'
    if dll.exists() and stamp.exists() and stamp.read_text() == digest:
        return dll
    common.run([DOTNET, 'build', '-c', 'Release', '-o', BUILD / 'bin', f'-p:BaseIntermediateOutputPath={BUILD / "obj"}/',
                f'-p:CecilPath={common.mono_cecil()}', os.path.join(SOURCE, 'Gen.csproj')])
    stamp.write_text(digest)
    return dll


def models(managed, classes):
    """classes: iterable of (assembly file name, full class name)."""
    BUILD.mkdir(parents=True, exist_ok=True)
    requests, output = BUILD / 'requests.json', BUILD / 'models.json'
    requests.write_text(json.dumps([{'assembly': a, 'fullname': f} for a, f in sorted(set(classes))]), encoding='utf-8')
    subprocess.run([str(DOTNET), str(generator()), str(managed), str(requests), str(output)], check=True,
                   env={**os.environ, 'DOTNET_ROOT': str(DOTNET.parent)})
    return {(e['assembly'], e['fullname']): e for e in json.loads(output.read_text(encoding='utf-8'))}
