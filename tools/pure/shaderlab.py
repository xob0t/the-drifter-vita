"""Find the program blocks in a ShaderLab file and rebuild the text Unity 2018.2 hands to psp2cgc for each."""
import re
from dataclasses import dataclass, field

KW = re.compile(r'\b(CGPROGRAM|CGINCLUDE|HLSLPROGRAM|HLSLINCLUDE|GLSLPROGRAM|SubShader|Pass|UsePass|GrabPass)\b')

@dataclass
class Program:
    lang: str            # 'CG' or 'HLSL'
    sub: int
    passi: int
    line: int            # line of the CGPROGRAM keyword (1-based)
    body: str            # text after the keyword up to ENDCG
    pragmas: dict = field(default_factory=dict)

@dataclass
class Include:
    lang: str
    line: int
    body: str

def scan(text):
    progs, incs = [], []
    i, n = 0, len(text)
    depth = 0
    sub = -1; passi = -1
    sub_depth = None
    while i < n:
        c = text[i]
        if text.startswith('//', i):
            j = text.find('\n', i); i = n if j < 0 else j; continue
        if text.startswith('/*', i):
            j = text.find('*/', i + 2); i = n if j < 0 else j + 2; continue
        if c == '"':
            j = text.find('"', i + 1); i = j + 1; continue
        if c == '{':
            depth += 1; i += 1; continue
        if c == '}':
            depth -= 1
            if sub_depth is not None and depth < sub_depth: sub_depth = None
            i += 1; continue
        m = KW.match(text, i) if (c.isalpha() and (i == 0 or not (text[i-1].isalnum() or text[i-1] == '_'))) else None
        if not m:
            i += 1; continue
        k = m.group(1); line = text.count('\n', 0, i) + 1
        if k in ('CGPROGRAM', 'CGINCLUDE', 'HLSLPROGRAM', 'HLSLINCLUDE', 'GLSLPROGRAM'):
            lang = 'HLSL' if k.startswith('HLSL') else ('GLSL' if k.startswith('GLSL') else 'CG')
            end = {'CG': 'ENDCG', 'HLSL': 'ENDHLSL', 'GLSL': 'ENDGLSL'}[lang]
            j = text.index(end, m.end())
            body = text[m.end():j]
            if k.endswith('INCLUDE'): incs.append(Include(lang, line, body))
            elif lang != 'GLSL': progs.append(Program(lang, sub, passi, line, body))
            i = j + len(end); continue
        if k == 'SubShader':
            sub += 1; passi = -1; sub_depth = depth + 1
        elif k in ('Pass', 'UsePass', 'GrabPass') and sub_depth is not None:
            passi += 1
        i = m.end()
    return progs, incs

PRAGMA = re.compile(r'^[ \t]*#pragma[ \t]+(\w+)([^\n\r]*)', re.M)

def pragmas(text):
    out = []
    for m in PRAGMA.finditer(text):
        args = m.group(2).split('//')[0].split()
        out.append((m.group(1), args))
    return out

def unity_input(text, prog, incs):
    """The .cg/.hlsl text Unity writes for one program."""
    nl = '\r\n' if '\r\n' in text else '\n'
    L = prog.line + 1
    dummy = f'#ifdef DUMMY_PREPROCESSOR_TO_WORK_AROUND_HLSL_COMPILER_LINE_HANDLING{nl}#endif{nl}'
    out = f'#include "HLSLToPSP2.cginc"{nl}#line {L} ""{nl}{dummy}{nl}'
    if prog.lang == 'CG':
        out += f'#include "HLSLSupport.cginc"{nl}'
    out += f'#define UNITY_INSTANCED_LOD_FADE{nl}#define UNITY_INSTANCED_SH{nl}#define UNITY_INSTANCED_LIGHTMAPSTS{nl}'
    if prog.lang == 'CG':
        out += f'#include "UnityShaderVariables.cginc"{nl}#include "UnityShaderUtilities.cginc"{nl}'
    for inc in incs:
        if inc.lang != prog.lang: continue
        out += f'#line {inc.line + 1}{nl}' + inc.body.replace('#pragma', '//#prag')
    out += f'#line {L} ""{nl}{dummy}/* UNITY: Original start of shader */' + prog.body.replace('#pragma', '//#prag')
    return out


SHADER_NAME = re.compile(r'^\s*Shader\s*"([^"]+)"', re.M)


def find_shaders(project):
    """Shader name -> .shader file, over the project's Assets."""
    out = {}
    for path in sorted((project / 'Assets').rglob('*.shader')):
        m = SHADER_NAME.search(path.read_text(encoding='utf-8', errors='replace'))
        if m:
            out[m.group(1)] = path
    return out
