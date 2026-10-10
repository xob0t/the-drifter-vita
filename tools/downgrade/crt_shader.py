"""CRT/SinglePass (Compat Crt), made during the project stage from the template
shaders/CrtSinglePass.shader.in and the game's own CRT shaders.

The template holds the pipeline's structure: the passes, the grid mesh path, the lookup
texture, the two-step aberration path. The code that computes the game's filter is taken from
the game: the fragment programs of CRT/Postprocess (the first pass) and CRT/FinalPostprocess
(the final pass) as USCSandbox decompiles them and shader_rules.py rewrites them. Each
"// @lift <part>" line of the template becomes a function around a run of their lines, kept as
they are (same registers, same operations in the same order):

- bend: the final program from its first statement up to its first range test of the curved
  position (the register that test compares with float2(0.0, 0.0)).
- mask: the final program's if/else chain on maskMode; the value is the register the line after
  the chain multiplies the intermediate's colour by.
- vignette: the final program after that chain, from the first line that reads the curved
  position to the first saturate.
- columnMask: the first program's RGB mask (the lines after the mono noise's blend, up to the
  RGB mask's blend) with rgbMaskSep, rgbMaskSub and rgbMaskStr set to 1 and a colour of 0, for
  one texel column: minus the result is what the mask subtracts there in units of the uniforms.
- levels: the first program from its first use of colorMat to its end.
- postprocess: the whole first program, reading the source (_MainTex) for its _BlurTex (the
  blur pass copies at the game's blur size of 0), with the noise taken at the output pixel and
  the RGB mask replaced by the template's rgbMaskOffset, blended at the curved position.
- final: the whole final program, its read of the intermediate texture replaced by postprocess.

Nothing of the game's code is in the repository: without the game's shaders there is no
CRT/SinglePass. A line the parts look for that is missing or found twice stops the stage, as
the other shader rules do.
"""

import re
import sys

from common import HERE

TEMPLATE = HERE / 'shaders' / 'CrtSinglePass.shader.in'
FILE_NAME = 'CrtSinglePass.shader'
FIRST, FINAL = 'CRT/Postprocess', 'CRT/FinalPostprocess'
INDENT = '    '
REGISTER = re.compile(r'\btmp\d+\b')


def fail(message):
    sys.exit(f'CRT/SinglePass: {message}')


def fragment_body(text, name):
    """The lines of a shader's one fragment program between its braces, indented from there."""
    lines = text.split('\n')
    starts = [i for i, line in enumerate(lines) if line.strip() == 'fout frag(v2f inp)']
    if len(starts) != 1 or lines[starts[0] + 1].strip() != '{':
        fail(f'{name}: expected one fragment program')
    depth, body = 0, []
    base = len(lines[starts[0] + 1]) - len(lines[starts[0] + 1].lstrip()) + len(INDENT)
    for line in lines[starts[0] + 1 :]:
        depth += line.count('{') - line.count('}')
        body.append(line[base:] if line[:base].strip() == '' else line.strip())
        if depth == 0:
            return body[1:-1]
    fail(f'{name}: fragment program not closed')


def find(lines, pattern, what, start=0, end=None):
    """The index of the one line (from start to end) that matches pattern, and the match."""
    found = []
    for i in range(start, len(lines) if end is None else end):
        match = re.fullmatch(pattern, lines[i].strip())
        if match:
            found.append((i, match))
    if len(found) != 1:
        fail(f'{what}: expected one line matching {pattern!r}, found {len(found)}')
    return found[0]


def find_first(lines, pattern, start=0):
    """The index of the first line from start that matches pattern, and the match; (None, None) if none."""
    for i in range(start, len(lines)):
        match = re.fullmatch(pattern, lines[i].strip())
        if match:
            return i, match
    return None, None


def statements_start(body):
    """The first line after the program's output and register declarations."""
    i = 0
    while i < len(body) and re.fullmatch(r'fout o;|float4 tmp\d+;', body[i].strip()):
        i += 1
    return i


def closing_brace(body, start):
    depth = 0
    for i in range(start, len(body)):
        depth += body[i].count('{') - body[i].count('}')
        if depth == 0:
            return i
    fail('unbalanced braces')


def registers(lines):
    names = set()
    for line in lines:
        names.update(REGISTER.findall(line))
    return sorted(names, key=lambda n: int(n[3:]))


def function(signature, lines, inputs=(), setup=(), result=None):
    """A function of the taken lines: the input lines, the registers the lines and the setup use
    (unless the lines declare them) declared as 0, the setup lines, the lines, and a return of
    result (None: the lines return themselves)."""
    declared = [
        f'float4 {name} = 0.0;'
        for name in registers([*lines, *setup])
        if f'float4 {name};' not in (line.strip() for line in lines)
    ]
    margin = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
    lines = [line[margin:] for line in lines]
    body = [*inputs, *declared, *setup, *lines] + ([f'return {result};'] if result is not None else [])
    return [INDENT + signature, INDENT + '{'] + [INDENT * 2 + line for line in body] + [INDENT + '}']


def lifted_input(texcoord):
    return ['crtLifted inp;', f'inp.texcoord = {texcoord};', f'inp.texcoord1 = {texcoord};']


def parts(first_text, final_text):
    """The template's parts: name -> lines of the function."""
    first, final = fragment_body(first_text, FIRST), fragment_body(final_text, FINAL)
    return {**final_parts(final), **first_parts(first)}


def final_parts(final):
    """bend, mask, vignette and final, from the final program's lines."""
    out = {}
    test, m = find(final, r'(tmp\d+)\.\w+ = (tmp\d+)\.xy < float2\(0\.0, 0\.0\);', FINAL)
    bent = m[2]
    read, m = find(final, rf'(tmp\d+) = tex2D\(_MainTex, {bent}\.xy\);', FINAL)
    color = m[1]
    begin = statements_start(final)
    out['bend'] = function(
        'float2 bend(float2 texcoord)', final[begin:test], lifted_input('texcoord'), result=f'{bent}.xy'
    )
    chain, _ = find(final, r'if \(!\(maskMode\)\) \{', FINAL)
    chain_end = closing_brace(final, chain)
    _, m = find(final, rf'(tmp\d+)\.xyz = {color}\.xyz \* (tmp\d+)\.xyz;', FINAL, chain_end + 1, chain_end + 2)
    out['mask'] = function(
        'float mask(float2 texcoord)', final[chain : chain_end + 1], lifted_input('texcoord'), result=f'{m[2]}.x'
    )
    vignette = next(
        (i for i in range(chain_end + 1, len(final)) if '=' in final[i] and f'{bent}.xy' in final[i].split('=', 1)[1]),
        None,
    )
    if vignette is None:
        fail(f'{FINAL}: no read of the curved position after the mask')
    if any(re.match(rf'{bent}\.[xyzw]*[xy][xyzw]* =', line.strip()) for line in final[chain_end + 1 : vignette]):
        fail(f'{FINAL}: the curved position changes before the vignette')
    end, m = find_first(final, r'(tmp\d+\.[xyzw]) = saturate\(.*\);', vignette)
    if end is None:
        fail(f'{FINAL}: no vignette value')
    out['vignette'] = function(
        'float vignette(float2 bent)', final[vignette : end + 1], setup=[f'{bent}.xy = bent;'], result=m[1]
    )

    def lifted(i, line):
        # The intermediate texture's read becomes postprocess, and the program returns its colour.
        if i == read:
            return line.replace(f'tex2D(_MainTex, {bent}.xy)', f'postprocess({bent}.xy, inp.texcoord.xy)')
        if line.strip() == 'return o;':
            return line.replace('return o;', 'return o.sv_target;')
        return line

    whole = [lifted(i, line) for i, line in enumerate(final)]
    out['final'] = function('float4 finalPostprocess(float2 texcoord)', whole, lifted_input('texcoord'))
    return out


def first_parts(first):
    """columnMask, levels and postprocess, from the first program's lines: taps, blur and bleeding
    up to bleedStr's blend; noise up to monoNoiseStr's; the RGB mask up to rgbMaskStr's;
    interference, colour matrix and levels."""
    out = {}
    bleed, m = find(first, r'(tmp\d+)\.xyz = (tmp\d+)\.xyz \* bleedStr\.xxx \+ (tmp\d+)\.xyz;', FIRST)
    value = m[1]
    if m[3] != value:
        fail(f'{FIRST}: unexpected bleeding blend')
    noise, _ = find(first, rf'{value}\.xyz = (tmp\d+)\.xyz \* monoNoiseStr\.xxx \+ {value}\.xyz;', FIRST)
    masked, _ = find(first, rf'{value}\.xyz = (tmp\d+)\.xyz \* rgbMaskStr\.xxx \+ {value}\.xyz;', FIRST)
    _, m = find(
        first, r'(tmp\d+)\.xy = inp\.texcoord\.xy / float2\(pixelSizeX\.x, pixelSizeY\.x\);', FIRST, bleed + 1, noise
    )
    texel = m[1]
    matrix = next((i for i in range(masked + 1, len(first)) if 'colorMat.' in first[i]), None)
    if not bleed < noise < masked or matrix is None or first[-1].strip() != 'return o;':
        fail(f'{FIRST}: unexpected order of its parts')
    unit = {'rgbMaskSep': 'unitSep', 'rgbMaskSub': 'unitSub', 'rgbMaskStr': 'unitStr'}
    column = [re.sub(r'\brgbMask(Sep|Sub|Str)\b', lambda u: unit[u[0]], line) for line in first[noise + 1 : masked + 1]]
    out['columnMask'] = function(
        'float3 columnMask(float texel)',
        column,
        ['float unitSep = 1.0, unitSub = 1.0, unitStr = 1.0;'],
        [f'{texel}.x = texel;', f'{value}.xyz = 0.0;'],
        f'-{value}.xyz',
    )
    out['levels'] = function(
        'float3 levels(float3 color)', first[matrix:-1], ['fout o;'], [f'{value}.xyz = color;'], 'o.sv_target.xyz'
    )
    source = [re.sub(r'\b_BlurTex\b', '_MainTex', line) for line in first]
    whole = [
        *source[: bleed + 1],
        'inp.texcoord = pixel;',
        *source[bleed + 1 : noise + 1],
        f'{value}.xyz -= rgbMaskOffset(uv.x);',
        'inp.texcoord = uv;',
        *source[masked + 1 : -1],
        'return o.sv_target;',
    ]
    out['postprocess'] = function('float4 postprocess(float2 uv, float2 pixel)', whole, lifted_input('uv'))
    return out


def single_pass(first_text, final_text):
    """CRT/SinglePass's source from the game's CRT/Postprocess and CRT/FinalPostprocess."""
    made = parts(first_text, final_text)
    template = TEMPLATE.read_text(encoding='utf-8').split('\n')
    out = [
        '// Made by tools/downgrade/crt_shader.py from shaders/CrtSinglePass.shader.in and the',
        "// game's CRT shaders. Not part of the repository.",
    ]
    used = []
    for line in template:
        m = re.fullmatch(r'\s*// @lift (\w+)', line)
        if not m:
            out.append(line)
            continue
        if m[1] not in made or m[1] in used:
            fail(f'template part {m[1]!r} unknown or used twice')
        used.append(m[1])
        out += made[m[1]]
    if sorted(used) != sorted(made):
        fail(f'template parts missing: {sorted(set(made) - set(used))}')
    return '\n'.join(out)
