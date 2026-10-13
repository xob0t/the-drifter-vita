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
from pathlib import Path

import common
from paths import WORK

SOURCE = Path(__file__).resolve().parent / 'typemodel'
DOTNET = common.EXTERNAL / 'dotnet' / 'dotnet.exe'
BUILD = WORK / 'typemodel'


def generator():
    digest = hashlib.sha256(b''.join((SOURCE / n).read_bytes() for n in ('Program.cs', 'Gen.csproj'))).hexdigest()
    dll, stamp = BUILD / 'bin' / 'TypeModelGen.dll', BUILD / 'stamp'
    if dll.exists() and stamp.exists() and stamp.read_text() == digest:
        return dll
    command = [
        DOTNET,
        'build',
        '-c',
        'Release',
        '--disable-build-servers',  # they would hold the .NET SDK's files, which pipeline.py clean deletes
        '-o',
        BUILD / 'bin',
        f'-p:BaseIntermediateOutputPath={BUILD / "obj"}/',
        f'-p:CecilPath={common.mono_cecil()}',
        SOURCE / 'Gen.csproj',
    ]
    common.log('Building TypeModelGen')
    # Its output captured: make.py builds it under a step, and it would print through the bars.
    result = subprocess.run([str(c) for c in command], capture_output=True, text=True, errors='replace')
    if result.returncode:
        tail = (result.stdout + result.stderr).splitlines()[-30:]
        raise RuntimeError(f'dotnet build of TypeModelGen failed ({result.returncode}):\n' + '\n'.join(tail))
    stamp.write_text(digest)
    return dll


def models(managed, classes):
    """classes: iterable of (assembly file name, full class name)."""
    BUILD.mkdir(parents=True, exist_ok=True)
    requests, output = BUILD / 'requests.json', BUILD / 'models.json'
    requests.write_text(json.dumps([{'assembly': a, 'fullname': f} for a, f in sorted(set(classes))]), encoding='utf-8')
    result = subprocess.run(
        [str(DOTNET), str(generator()), str(managed), str(requests), str(output)],
        capture_output=True,  # it writes only its file; anything else (an error) would print through the bars
        text=True,
        errors='replace',
        env={**os.environ, 'DOTNET_ROOT': str(DOTNET.parent)},
    )
    if result.returncode:
        tail = (result.stdout + result.stderr).splitlines()[-30:]
        raise RuntimeError(f'TypeModelGen failed ({result.returncode}):\n' + '\n'.join(tail))
    return {(e['assembly'], e['fullname']): e for e in json.loads(output.read_text(encoding='utf-8'))}
