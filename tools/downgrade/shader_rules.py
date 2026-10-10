"""Shaders: AssetRipper exports placeholders; the game's shaders are decompiled (USCSandbox) and
rewritten by the rules here (noise split and fast noise, the PS Vita's frame-colour and alpha
discard SubShaders). pipeline.asset_overlay takes shader_overlay's result."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import NamedTuple

import fastnoise
from common import (
    EXTERNAL,
    HERE,
    SHADER_NAME,
    SOURCE_VERSION,
    USCSANDBOX_COMMIT,
    WORK,
    editor_data,
    game_data,
    log,
    meta_of,
    run,
    stable_meta,
)

# Lines of the decompiled programs the rules look for.
PASS_START = 'Pass {'
FRAGMENT_PRAGMA = '#pragma fragment frag'
FRAGMENT_SIGNATURE = 'fout frag(v2f inp)'
VERTEX_SIGNATURE = 'v2f vert(appdata v)'
VERTEX_TEXCOORD = 'o.texcoord.xy = v.texcoord.xy;'
V2F_TEXCOORD = 'float2 texcoord : TEXCOORD;'
MAIN_SAMPLER = 'sampler2D _MainTex; // 0'
NOISE_START = 'tmp0.xz = float2(0.0, 1.0);'  # the first line of the simplex noise code


def indent(line):
    """The leading whitespace of a line."""
    return line[: len(line) - len(line.lstrip())]


def first_pass(lines, start):
    """(its 'Pass {' line, its ENDCG, its closing brace) of the first pass from start on."""
    first = next(i for i in range(start, len(lines)) if lines[i].strip() == PASS_START)
    endcg = next(i for i in range(first, len(lines)) if lines[i].strip() == 'ENDCG')
    last = next(i for i in range(endcg, len(lines)) if lines[i].strip() == '}')
    return first, endcg, last


def only_psp2(body):
    """A pass's text with its programs compiled for the PS Vita only; (text, 1) if it had the pragma."""
    return re.subn(r'(\n(\s*)#pragma fragment frag\n)', r'\1\2#pragma only_renderers psp2\n', body, count=1)


def psp2_subshader_first(lines, sub, own, lod_indent, body):
    """The shader with a PS Vita SubShader before the original (lines[sub], 'SubShader {'): the
    original's own lines before own, LOD 200, then body. Shader.globalMaximumLOD 100 (the original
    has 0) selects the original again, for A/B checks on the console (the probe's -diag-check-global
    with maximumLOD)."""
    psp2 = [*lines[sub:own], lod_indent + 'LOD 200', *body.split('\n'), indent(lines[sub]) + '}']
    return '\n'.join(lines[:sub] + psp2 + lines[sub:])


def uscsandbox():
    """Build the pinned USCSandbox DX11 shader decompiler, with our patches, using a local .NET 8 SDK."""
    repo = EXTERNAL / 'USCSandbox'
    exe = repo / 'USCSandbox' / 'bin' / 'Release' / 'net8.0' / 'USCSandbox.exe'
    patches = sorted((HERE / 'patches').glob('uscsandbox-*.patch'))
    stamp = repo / '.drifter-patches'
    digest = hashlib.sha256(USCSANDBOX_COMMIT.encode() + b''.join(p.read_bytes() for p in patches)).hexdigest()
    if exe.exists() and stamp.exists() and stamp.read_text() == digest:
        return exe
    dotnet = EXTERNAL / 'dotnet' / 'dotnet.exe'
    if not dotnet.exists():
        installer = EXTERNAL / 'dotnet-install.ps1'
        urllib.request.urlretrieve('https://dot.net/v1/dotnet-install.ps1', installer)
        run(
            [
                'powershell',
                '-NoProfile',
                '-ExecutionPolicy',
                'Bypass',
                '-File',
                installer,
                '-Channel',
                '8.0',
                '-InstallDir',
                dotnet.parent,
                '-NoPath',
            ]
        )
    if not repo.exists():
        run(['git', 'clone', 'https://github.com/nesrak1/USCSandbox.git', repo])
    run(['git', '-C', repo, 'checkout', '-q', '--force', USCSANDBOX_COMMIT])
    for patch in patches:
        run(['git', '-C', repo, 'apply', patch.resolve()])
    run([dotnet, 'build', '-c', 'Release', repo / 'USCSandbox' / 'USCSandbox.csproj'])
    stamp.write_text(digest)
    return exe


def decompile_shaders(game: Path):
    """Decompile every DX11 shader in the game. Returns {shader name: ShaderLab source}."""
    exe = uscsandbox()
    work = WORK / 'shaders'
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    shutil.copy2(EXTERNAL / 'USCSandbox' / 'Files' / 'classdata.tpk', work)
    failures = []
    data = game_data(game)
    # USCSandbox.exe runs on the .NET 8 runtime of the local SDK, which is not on PATH.
    dotnet_env = {**os.environ, 'DOTNET_ROOT': str(EXTERNAL / 'dotnet')}
    for assets in sorted(data.glob('*.assets')):
        result = subprocess.run(
            [str(exe), 'null', str(assets), '--all', '--version', SOURCE_VERSION],
            cwd=work,
            capture_output=True,
            text=True,
            env=dotnet_env,
        )
        if result.returncode != 0:
            message = result.stderr.strip().splitlines()
            failures.append(f'{assets.name}: {message[0] if message else f"exit {result.returncode}"}')
    shaders = {}
    for path in (work / 'out').rglob('*.shader'):
        text = path.read_text(encoding='utf-8')
        match = SHADER_NAME.match(text)
        if match:
            shaders[match.group(1)] = text
    if failures:
        sys.exit(f'USCSandbox failed on: {failures}')
    log(f'Decompiled {len(shaders)} shaders')
    return shaders


def builtin_uniforms(version):
    """Uniforms that the target editor declares in includes every CGPROGRAM gets."""
    includes = editor_data(version) / 'CGIncludes'
    declaration = re.compile(
        r'^\s*(?:uniform\s+)?(?:float|half|fixed|int|uint|bool)\w*\s+(\w+)\s*(?:\[[^\]]*\])?\s*;', re.M
    )
    names = set()
    for name in ('HLSLSupport.cginc', 'UnityShaderVariables.cginc'):
        names.update(declaration.findall((includes / name).read_text(encoding='utf-8')))
    return names


# DX11 bytecode combines comparison masks (all bits set or clear) with bitwise
# and/or; the decompiler writes them as uint casts of the 0/1 floats it uses for
# masks. That truncates a value and-ed with a mask (an alpha becomes 0). The rule
# writes them as float arithmetic, with no integer casts or bitwise operations: and
# is a product (one operand is always a mask), or of two masks a maximum.
MASK_LOGIC = re.compile(r'uint([1-4])\(([^()]+)\) ([&|]) uint\1\(([^()]+)\)')


def mask_logic(text):
    return MASK_LOGIC.sub(lambda m: f'({m[2]}) * ({m[4]})' if m[3] == '&' else f'max({m[2]}, {m[4]})', text)


def strip_builtin_declarations(text, builtins):
    """The decompiler declares every uniform a program reads; drop those Unity already declares."""
    pattern = re.compile(r'^[ \t]*(?:float|half|fixed|int|uint|bool)\w*\s+(\w+)\s*(?:\[[^\]]*\])?\s*;[^\n]*\n', re.M)
    return pattern.sub(lambda m: '' if m.group(1) in builtins else m.group(0), text)


class NoiseSplit(NamedTuple):
    shader: str  # the split shader's name, in Resources
    file_name: str
    centre: str  # the position the read takes with the noise at its centre
    bound: str  # how far the noise moves the read, in texels
    declarations: list  # what the fragment programs lack


# Game shaders that move each pixel's texture read by simplex noise, which Compat WaterMasks
# draws in two passes (noise_split).
# - WaterSprite: uv + noise * _OffsetDist texels, |noise| < 1 (0.9996 at most, sampled).
# - PropBurble: uv + (noise * 65 + 1) * (_ScaleX, _ScaleY) in uv units, where the noise term
#   (the same simplex dot product, which the game's 130 scales to at most 0.9996 in WaterSprite)
#   times 65 is within 0.5: the read is within 0.5 * scale * size texels of uv + scale.
NOISE_SPLITS = {
    'WaterSprite': NoiseSplit(
        'Hidden/DrifterWaterSpriteSplit',
        'DrifterWaterSpriteSplit.shader',
        'inp.texcoord.xy * _MainTex_ST.xy + _MainTex_ST.zw',
        '_OffsetDist',
        [],
    ),
    'PropBurble': NoiseSplit(
        'Hidden/DrifterPropBurbleSplit',
        'DrifterPropBurbleSplit.shader',
        'inp.texcoord.xy * _MainTex_ST.xy + _MainTex_ST.zw + float2(_ScaleX, _ScaleY)',
        'max(abs(_ScaleX) * _MainTex_TexelSize.z, abs(_ScaleY) * _MainTex_TexelSize.w) * 0.5',
        ['float4 _MainTex_TexelSize;'],
    ),
}


def noise_split(text, name):
    """A game shader whose fragment programs move their texture read by simplex noise
    (NOISE_SPLITS), in two passes for Compat WaterMasks. WaterSprite's noise costs 151 cycles a
    pixel on the PS Vita: 33 ms of GPU time a frame for Drowning's surface sprite, which
    QuestCamera draws at screen resolution. The read moves by less than the bound, so where
    every texel within ceil(bound) of the centre position's texel has its value, the read
    returns that value wherever the noise moves it. _WaterMask holds per texel the radius of the
    uniform square around it. Pass 0 draws the pixels where the mask reaches, reading the texel
    at the centre position; pass 1 is the original pass for the other pixels, each discarding
    the other's pixels first (on the PS Vita a discarded pixel costs the program's first few
    instructions). Each pixel is drawn once, with the original's value; without a mask (black)
    pass 1 draws every pixel. On the console, at three times Drowning's surface texture, one
    draw took 45.7 ms with the original and 5.6 ms split (docs/vita.md, "Exact noise skip", has
    the alternatives tried)."""
    split = NOISE_SPLITS[name]
    centre, bound = split.centre, split.bound
    marker_read = 'tmp0 = tex2D(_MainTex, tmp0.xy);'
    lines = text.split('\n')
    first, _, last = first_pass(lines, 0)
    # The centre position reaches the fragment programs as a varying of its own (centre),
    # computed per vertex: a texture read at a varying as it arrives is non-dependent on the
    # PS Vita (fetched before the program runs), one at a computed position waits for it. The
    # expression is affine in texcoord, so its interpolation is the original's value.
    # The mask is read at its own varying, maskPos = centre * _WaterMaskRect.xy + _WaterMaskRect.zw:
    # (1, 1, 0, 0), centre itself, for a mask of the whole texture; for a sprite on a texture too
    # big for one (room atlases), the mask covers only the sprite's texels and maps them there
    # (Compat WaterMasks; that mask is eroded by one texel, so a read one texel off is safe).
    reaches = f'tex2D(_WaterMask, inp.maskPos).x * 255.0 >= ceil({bound} + 0.01) - 0.5'

    def make(body):
        out, count, i = [], 0, first
        while i <= last:
            line = lines[i]
            if line.strip() == NOISE_START:
                pad = indent(line)
                if body == 'slow':
                    out += [f'{pad}if ({reaches}) discard;', line]
                    count += 1
                    i += 1
                    continue
                end = next(j for j in range(i, last) if lines[j].strip() == marker_read)
                out += [f'{pad}if (!({reaches})) discard;', f'{pad}tmp0 = tex2D(_MainTex, inp.centre);']
                count += 1
                i = end + 1
                continue
            elif line.strip() == MAIN_SAMPLER:
                pad = indent(line)
                out += [line, pad + 'sampler2D _WaterMask;'] + [pad + d for d in split.declarations]
            else:
                out.append(line)
            i += 1
        if count != 4:
            sys.exit(f'{name}: expected 4 fragment programs, found {count}')
        return out

    passes = make('fast') + make('slow')
    # The uniforms the centre position uses move from the fragment programs' declarations to the
    # vertex programs' (the same variant's vertex program comes first in the CGPROGRAM).
    moved = list(dict.fromkeys(re.findall(r'\b(_\w+)\b', centre)))
    types, kept = {}, []
    for line in passes:
        match = re.match(r'\s*(float\w*) (\w+); //', line)
        if match and match[2] in moved:
            types[match[2]] = match[1]
            continue
        kept.append(line)
    if sorted(types) != sorted(moved):
        sys.exit(f'{name}: centre position uniforms {moved} not all declared')
    passes, in_v2f, counts = [], False, [0, 0, 0]
    for line in kept:
        pad = indent(line)
        if line.strip() == VERTEX_SIGNATURE:
            passes += [f'{pad}{types[u]} {u};' for u in moved] + [f'{pad}float4 _WaterMaskRect;']
            counts[0] += 1
        passes.append(line)
        if line.strip() == 'struct v2f':
            in_v2f = True
        elif in_v2f and line.strip() == V2F_TEXCOORD:
            passes += [f'{pad}float2 centre : TEXCOORD1;', f'{pad}float2 maskPos : TEXCOORD2;']
            in_v2f = False
            counts[1] += 1
        elif line.strip() == VERTEX_TEXCOORD:
            passes += [
                f'{pad}o.centre = {centre.replace("inp.texcoord", "v.texcoord")};',
                f'{pad}o.maskPos = o.centre * _WaterMaskRect.xy + _WaterMaskRect.zw;',
            ]
            counts[2] += 1
    if counts != [8, 8, 8]:
        sys.exit(f'{name}: unexpected vertex programs {counts}')
    result = '\n'.join(lines[:first] + passes + lines[last + 1 :])
    result, named = re.subn(rf'^(\s*)Shader "{re.escape(name)}"', rf'\1Shader "{split.shader}"', result, count=1)
    result, properties = re.subn(
        r'(\n(\s*)_Timer \("Timer", Float\) = 0\n)',
        r'\1\2_WaterMask ("Noise skip mask", 2D) = "black" {}\n'
        r'\2_WaterMaskRect ("Noise skip mask rect", Vector) = (1, 1, 0, 0)\n',
        result,
    )
    if named != 1 or properties != 1:
        sys.exit(f'{name}: unexpected shader name or properties')
    return result


class NoiseFast(NamedTuple):
    end: str  # the line after the last one the fast code replaces
    uniforms: list  # what the vertex programs need
    noise_a: tuple  # noise A's scale and constant shift
    noise_b: tuple  # noise B's
    tail: list  # the fragment lines after the two noise reads


# The fast noise path (noise_fast, Compat FastNoise). Each noise is the game's
# 130 * snoise(uv * scale + shift), uv = texcoord * _MainTex_ST.xy + _MainTex_ST.zw, read from
# the noise texture into drifterNoise (A in x, B in y). The rest is the original's arithmetic.
# - WaterSprite: snoise((uv - (t * speed / scale, 0)) * scale) for x, the same in y with
#   the y scale; the read moves by noise * _OffsetDist texels.
# - PropBurble: noise at (uv + (t * _Speed, 1)) * _NoiseScale and (uv + (1, t * _Speed)) *
#   _NoiseScale; the read moves by (noise / 2 + 1) * (_ScaleX, _ScaleY) (the game's 65 *
#   snoise is half of 130 * snoise).
# - PropLightMasked: noise at (uv + (t, 1)) * 2 and (uv + (1, t)) * 2; the light mask's scale
#   is (noise / 2 + _BlurbleScale + 0.5) / (2 * _BlurbleScale). The sprite read (and the PS
#   Vita SubShader's discard after it, psp2_alpha_discard) comes first, at a varying.
NOISE_FAST = {
    'WaterSprite': NoiseFast(
        'tmp0 = tex2D(_MainTex, tmp0.xy);',
        ['_MainTex_ST', '_BlurbleScaleX', '_BlurbleScaleY', '_BlurbleSpeed', '_Timer'],
        ('_BlurbleScaleX', 'float2(-_Timer * _BlurbleSpeed, 0.0)'),
        ('_BlurbleScaleY', 'float2(0.0, -_Timer * _BlurbleSpeed)'),
        [
            'tmp0.xy = drifterNoise * _OffsetDist.xx;',
            'tmp0.xy = tmp0.xy * _MainTex_TexelSize.xy + inp.drifterUV;',
            'tmp0 = tex2D(_MainTex, tmp0.xy);',
        ],
    ),
    'PropBurble': NoiseFast(
        'tmp0 = tex2D(_MainTex, tmp0.xy);',
        ['_MainTex_ST', '_NoiseScale', '_Speed', '_Timer'],
        ('_NoiseScale', 'float2(_Timer * _Speed, 1.0) * _NoiseScale'),
        ('_NoiseScale', 'float2(1.0, _Timer * _Speed) * _NoiseScale'),
        [
            'tmp0.xy = drifterNoise * float2(0.5, 0.5) + float2(1.0, 1.0);',
            'tmp0.xy = tmp0.xy * float2(_ScaleX, _ScaleY) + inp.drifterUV;',
            'tmp0 = tex2D(_MainTex, tmp0.xy);',
        ],
    ),
    'PropLightMasked': NoiseFast(
        'tmp2.y = tmp0.x / tmp0.y;',
        ['_MainTex_ST', '_Timer'],
        ('2.0', 'float2(_Timer, 1.0) * 2.0'),
        ('2.0', 'float2(1.0, _Timer) * 2.0'),
        [
            'tmp0.y = _BlurbleScale + _BlurbleScale;',
            'tmp2.xy = drifterNoise * float2(0.5, 0.5) + (_BlurbleScale + 0.5);',
            'tmp2.xy = tmp2.xy / tmp0.yy;',
        ],
    ),
}
NOISE_FAST_READ = {'PropLightMasked': ('tmp1 = tex2D(_MainTex, tmp1.zw);', 'tmp1 = tex2D(_MainTex, inp.drifterUV);')}
# fastnoise.F2, the simplex lattice's skew, as the vertex programs write it.
NOISE_SKEW = '0.3660254'
# A texel's value v (0 to 1) holds the noise (255 v - 128) / 127 (fastnoise.py): v * scale - offset.
NOISE_DECODE = ('2.007874', '1.007874')
FAST_IF, FAST_ELSE, FAST_END = '            #if DRIFTER_FAST_NOISE', '            #else', '            #endif'


def noise_coordinate(target, scale, shift):
    """Vertex program lines that set o.<target> to the noise texture's coordinate of
    p = drifterUV * scale + shift: skew(p) / fastnoise.PERIOD, the shift's part wrapped, which keeps
    the coordinates small as the timer grows (the texture repeats)."""
    f2, inv = f'float2({NOISE_SKEW}, {NOISE_SKEW})', f'{1.0 / fastnoise.PERIOD!r}'
    return [
        f'drifterP = drifterUV * {scale};',
        f'drifterC = {shift};',
        f'o.{target} = (drifterP + dot(drifterP, {f2})) * {inv} + frac((drifterC + dot(drifterC, {f2})) * {inv});',
    ]


def moved_uniforms(block, moved, name):
    """Where a CGPROGRAM block's uniforms in moved go from the fragment programs' declarations to the
    vertex programs' of the same variant: (the vertex structs' line indexes, the declaration lines to
    drop, {vertex program line: declarations to add before it})."""
    # Per variant k: the vertex section's declarations end at its k-th 'v2f vert', the
    # fragment section's run from its k-th 'struct fout' to its k-th 'fout frag'.
    verts = [j for j, line in enumerate(block) if line.strip() == VERTEX_SIGNATURE]
    fouts = [j for j, line in enumerate(block) if line.strip() == 'struct fout']
    frags = [j for j, line in enumerate(block) if line.strip() == FRAGMENT_SIGNATURE]
    structs = [j for j, line in enumerate(block) if line.strip() == 'struct v2f']
    if not (len(verts) == len(fouts) == len(frags) == len(structs)) or not verts:
        sys.exit(f'{name}: unexpected programs for the fast noise path')
    types, remove, add = {}, set(), {}
    for k in range(len(verts)):
        declared = {
            m[2]: m[1] for m in (re.match(r'\s*(float\w*) (\w+);', line) for line in block[structs[k] : verts[k]]) if m
        }
        needed = []
        for j in range(fouts[k], frags[k]):
            m = re.match(r'\s*(float\w*) (\w+); //', block[j])
            if m and m[2] in moved:
                types[m[2]] = m[1]
                remove.add(j)
                if m[2] not in declared:
                    needed.append(f'{m[1]} {m[2]};')
        missing = [u for u in moved if u not in declared and u not in types]
        if missing:
            sys.exit(f'{name}: uniforms {missing} not declared')
        add[verts[k]] = needed
    return structs, remove, add


def fast_noise_span(span, name, pad):
    """The fragment lines from the noise code's start to its end (span), with the fast path before them."""
    fast = []
    read = NOISE_FAST_READ.get(name)
    if read:
        if sum(s.strip() == read[0] for s in span) != 1:
            sys.exit(f'{name}: expected one sprite read in the noise code')
        fast.append(read[1])
        fast += [s.strip() for s in span if s.strip().endswith('discard;')]
    scale, offset = NOISE_DECODE
    fast += [
        'float2 drifterNoise = float2(tex2D(_DrifterNoiseTex, inp.drifterNoiseA).w, '
        f'tex2D(_DrifterNoiseTex, inp.drifterNoiseB).w) * {scale} - {offset};',
        *NOISE_FAST[name].tail,
    ]
    return [FAST_IF, *(pad + s for s in fast), FAST_ELSE, *span, FAST_END]


def fast_noise_block(block, name, counts):
    """A CGPROGRAM block (its lines) with the fast path; counts what it adds."""
    rule = NOISE_FAST[name]
    structs, remove, add = moved_uniforms(block, rule.uniforms, name)
    new, j = [], 0
    while j < len(block):
        line, pad = block[j], indent(block[j])
        if j in remove:
            j += 1
            continue
        if j in add:
            new += [pad + d for d in add[j]]
        if line.strip() == FRAGMENT_PRAGMA:
            new += [line, pad + '#pragma multi_compile __ DRIFTER_FAST_NOISE']
            counts['pragma'] += 1
        elif line.strip() == V2F_TEXCOORD and any(s < j and j - s < 8 for s in structs):
            new += [
                line,
                FAST_IF,
                pad + 'float2 drifterUV : TEXCOORD3;',
                pad + 'float2 drifterNoiseA : TEXCOORD4;',
                pad + 'float2 drifterNoiseB : TEXCOORD5;',
                FAST_END,
            ]
            counts['struct'] += 1
        elif line.strip() == VERTEX_TEXCOORD:
            new += [
                line,
                FAST_IF,
                pad + 'float2 drifterUV = v.texcoord.xy * _MainTex_ST.xy + _MainTex_ST.zw;',
                pad + 'float2 drifterP, drifterC;',
                pad + 'o.drifterUV = drifterUV;',
            ]
            coordinates = noise_coordinate('drifterNoiseA', *rule.noise_a) + noise_coordinate(
                'drifterNoiseB', *rule.noise_b
            )
            new += [pad + c for c in coordinates] + [FAST_END]
            counts['vertex'] += 1
        elif line.strip() == MAIN_SAMPLER:
            new += [line, pad + 'sampler2D _DrifterNoiseTex;']
            counts['sampler'] += 1
        elif line.strip() == NOISE_START:
            stop = next(s for s in range(j, len(block)) if block[s].strip() == rule.end)
            new += fast_noise_span(block[j : stop + 1], name, pad)
            counts['span'] += 1
            j = stop + 1
            continue
        else:
            new.append(line)
        j += 1
    return new


def noise_fast(text, name):
    """A game shader whose fragment programs evaluate two simplex noises per pixel (NOISE_FAST),
    with a second path behind the global keyword DRIFTER_FAST_NOISE (#pragma multi_compile, set by
    Compat FastNoise) that reads the noise from a precomputed texture (_DrifterNoiseTex,
    fastnoise.py) instead. The vertex programs compute the texture coordinates (affine in the
    sprite's texture coordinate, so they interpolate exactly) and pass them as varyings, which
    the PS Vita fetches before the fragment program runs: for WaterSprite psp2shaderperf counts
    13 cycles a pixel (2 non-dependent reads and the dependent sprite read) against the
    original's 151, and the exact skip's cheap pass (noise_split) alone 16. Without the keyword
    every program is the original. The noise is the game's kind of noise (same kernel, gradients,
    scale, direction and speed), not the same values: the game's hash has no period a texture
    could wrap at; see fastnoise.py.
    The uniforms the vertex programs read move from the fragment programs' declarations to the
    vertex programs' of the same variant (which come first in the CGPROGRAM), as in noise_split."""
    lines = text.split('\n')
    out, i = [], 0
    counts = {'pragma': 0, 'struct': 0, 'vertex': 0, 'span': 0, 'sampler': 0}
    while i < len(lines):
        if lines[i].strip() != 'CGPROGRAM':
            out.append(lines[i])
            i += 1
            continue
        end = next(j for j in range(i, len(lines)) if lines[j].strip() == 'ENDCG')
        block = lines[i : end + 1]
        i = end + 1
        has_noise = any(line.strip() == NOISE_START for line in block)
        out += fast_noise_block(block, name, counts) if has_noise else block
    programs = counts['span']
    if (
        not programs
        or counts['struct'] != programs
        or counts['vertex'] != programs
        or counts['sampler'] != programs
        or counts['pragma'] * 4 != programs
    ):
        sys.exit(f'{name}: unexpected programs for the fast noise path {counts}')
    return '\n'.join(out)


def psp2_frag_color(text, name, grab):
    """A shader whose first pass reads the colour under each pixel through a GrabPass, with a
    SubShader for the PS Vita before the original that reads it from the render target itself
    (programmable blending: the FRAGCOLOR input of a __nativecolor fragment program) and does
    the pass's blend (SrcAlpha or One, OneMinusSrcAlpha) in the program, with no GrabPass. The grab copy
    texture is sampled at the pixel's own screen position, so the value is the same colour.
    On the console, CharacterShadow's GrabPass in Crypt made about one frame in 25 show stale
    memory instead of the room while Mick walked with the Zippo lit (vitacompanion
    screenshots: 10 of 260 with the shadow drawn, 0 of 180 with it hidden, 0 of 100 in
    LabKitchen). Other renderers skip the PS Vita SubShader (#pragma only_renderers psp2) and
    keep the original; if it fails to compile there, Unity falls back to the original too.
    Where two shadows overlap the second sees the first one's pixels, which the grab, taken
    once per frame, did not include. grab None is an unnamed GrabPass (_GrabTexture), which
    Unity takes again before every draw, so there the colour read is the grab's even where
    such sprites overlap. SpriteDodge and FlamerDodgeSprite (colour dodge over the room) read
    their grab at the vertex position without _Offset, which PowerSprite sets from its
    offset: zero for every renderer that draws with them (PropFlamerDodge, PropScanlines,
    PropFgDodge, CharacterFlamerDodge and MonsterFlameDodge; nothing sets theirs), and no
    material of theirs enables PIXELSNAP_ON (which snaps the position but not the grab's)."""
    lines = text.split('\n')
    sub = next(i for i, line in enumerate(lines) if line.strip() == 'SubShader {')
    grab_text = f'GrabPass {{ "{grab}" }}' if grab else 'GrabPass { }'
    grab_line = next(i for i in range(sub, len(lines)) if lines[i].strip() == grab_text)
    grab = grab or '_GrabTexture'
    first, _, last = first_pass(lines, grab_line)
    pass_lines = lines[first : last + 1]
    blend = [i for i, line in enumerate(pass_lines) if line.strip().startswith('Blend ')]
    # The pass's blend, done in the program as the PS Vita does it for the original (the probe's
    # -diag-check-blend and -diag-check-global with maximumLOD): the original's output reaches
    # the blend as half floats, rounded to 8 bits, and blended in 8-bit fixed point, which
    # __fastblend does (the add.fx8 instruction). Rounding to half precision is done by hand
    # (ties to even), as the compiler drops a cast to half4 and rewrites exp2(floor(log2(x)))
    # inexactly; fbUlp is the spacing of half floats at the value (2^-24 below 2^-14). Over every
    # pair of 8-bit colours (256x256 texels through SpriteDodge, _Color alpha 1, 0.8, 0.6, and 0.9
    # with the sprite's and the target's alpha varied too) this gave the original's pixels
    # exactly; a float blend rounded once differed in 31% at alpha 0.8 and 0.6, and without the
    # half step 2-4% at every alpha (1 level). In the lit Crypt a float blend left 33 of
    # CharacterShadow's pixels a level off; this, none. Where the scene under a dodge sprite
    # has sharp edges, the original's bilinear grab read leaks a hair of the neighbour (PaperPC:
    # 1.4% of the frame, all at edges, 1-15 levels after the dodge); this reads the pixel's own.
    factors = {
        'Blend SrcAlpha OneMinusSrcAlpha, SrcAlpha OneMinusSrcAlpha': '__BLEND_FACTOR_SRC1_ALPHA',
        'Blend One OneMinusSrcAlpha, One OneMinusSrcAlpha': '__BLEND_FACTOR_ONE',
    }
    if len(blend) != 1 or pass_lines[blend[0]].strip() not in factors:
        sys.exit(f'{name}: unexpected blend state')
    factor = factors[pass_lines[blend[0]].strip()]
    blend_code = (
        'float4 fbValue = saturate(o.sv_target);',
        'float4 fbUlp = 5.960464477539063e-08;',
        *[f'fbUlp = max(fbUlp, (float4)(fbValue >= {2.0**-k!r}) * {2.0 ** (-k - 10)!r});' for k in range(14, -1, -1)],
        'float4 fbSteps = fbValue / fbUlp;',
        'float4 fbRounded = floor(fbSteps + 0.5);',
        'fbRounded -= (float4)(fbRounded - fbSteps == 0.5) * (float4)(frac(fbRounded * 0.5) != 0.0);',
        'unsigned char4 fbSource = (unsigned char4)floor(fbRounded * fbUlp * 255.0 + 0.5);',
        f'return __fastblend(fbSource, fbColor, __BLEND_OP_ADD, {factor}, __BLEND_FACTOR_ONE_MINUS_SRC1_ALPHA,',
        f'                   __BLEND_OP_ADD, {factor}, __BLEND_FACTOR_ONE_MINUS_SRC1_ALPHA);',
    )
    pass_lines[blend[0]] = indent(pass_lines[blend[0]]) + 'Blend Off'
    body, pragmas = only_psp2('\n'.join(pass_lines))
    body, signatures = re.subn(
        rf'\b{re.escape(FRAGMENT_SIGNATURE)}',
        '__nativecolor __regformat unsigned char4 frag(v2f inp, __regformat unsigned char4 fbColor : FRAGCOLOR)',
        body,
    )
    body, reads = re.subn(rf'tex2D\({re.escape(grab)}, [^;]*\);', '(float4)fbColor * (1.0 / 255.0);', body)
    # The first return after each fragment program's signature (vertex programs return o too).
    body, returns = re.subn(
        r'(FRAGCOLOR\)(?:(?!return o;).)*?)\n(\s*)return o;',
        lambda m: m[1] + ''.join('\n' + m[2] + line for line in blend_code),
        body,
        flags=re.S,
    )
    if pragmas != 1 or signatures < 1 or signatures != reads or reads != returns:
        sys.exit(f'{name}: unexpected fragment programs ({pragmas} {signatures} {reads} {returns})')
    # The SubShader's own lines up to the GrabPass: its Tags block.
    return psp2_subshader_first(lines, sub, grab_line, indent(lines[sub + 1]), body)


# Shaders that read the colour under each pixel through a GrabPass: shader -> grab texture name
# (None: an unnamed GrabPass).
PSP2_FRAG_COLOR = {'CharacterShadow': '_CharShadow', 'SpriteDodge': None, 'FlamerDodgeSprite': None}


def psp2_alpha_discard(text, name, read):
    """A PS Vita SubShader before the original whose fragment programs discard a pixel right
    after their sprite texture read (`read`, the original's own line) when its alpha is 0,
    for shaders whose output is then exactly 0 with Blend One OneMinusSrcAlpha, which leaves
    the pixel as it was. PropLightMasked (the Crypt's Zippo light: 13 props of 400x180 texels
    drawn when it is lit, most of them 0-7% opaque) multiplies everything it outputs by that
    alpha after two simplex noises (168 cycles a pixel, psp2shaderperf); the PS Vita stops a
    discarded pixel's program there: the lit Crypt (as restored after a death there) went from
    10.9 to 38.4 fps, with 0 of 1.8 million pixels differing from the original's (8 frames,
    the probe's -diag-check-global). Setting the global _DrifterNoAlphaCull to 1 draws every
    pixel again (the probe's -diag-global and -diag-check-global, for A/B checks). Other
    renderers keep the original (#pragma only_renderers psp2).

    The light mask read could not be bounded the same way (discarding where every noise value
    puts it on mask texels that are 0): the masks are 0 outside a disc in the game's files, but
    the PS Vita's compressed copies have texels that are not 0 on all four edges (read back on
    the console), and the mask is clamped, so no pixel of BgLit (the one opaque prop) reads 0
    for certain."""
    lines = text.split('\n')
    sub = next(i for i, line in enumerate(lines) if line.strip() == 'SubShader {')
    first, endcg, last = first_pass(lines, sub)
    if any(line.strip().startswith('GrabPass') for line in lines[sub:first]) or len(
        re.findall(r'^\s*Pass \{', '\n'.join(lines[last:]), flags=re.M)
    ):
        sys.exit(f'{name}: expected one pass')
    blend = [line.strip() for line in lines[first:endcg] if line.strip().startswith('Blend ')]
    if blend != ['Blend One OneMinusSrcAlpha, One OneMinusSrcAlpha']:
        sys.exit(f'{name}: unexpected blend state')
    body, pragmas = only_psp2('\n'.join(lines[first : last + 1]))
    target = re.match(r'(\w+) = ', read)[1]
    body, discards = re.subn(
        rf'\n(\s*){re.escape(read)}\n',
        rf'\n\1{read}\n\1if ({target}.w == 0.0 && _DrifterNoAlphaCull == 0.0) discard;\n',
        body,
    )
    body, uniforms = re.subn(
        r'\n(\s*)(// Textures for DX11PixelSM40)\n', r'\n\1float _DrifterNoAlphaCull;\n\1\2\n', body
    )
    programs = len(re.findall(rf'\b{re.escape(FRAGMENT_SIGNATURE)}', body))
    if pragmas != 1 or not programs or discards != programs or uniforms != programs:
        sys.exit(f'{name}: unexpected fragment programs ({pragmas} {programs} {discards} {uniforms})')
    return psp2_subshader_first(lines, sub, first, indent(lines[first]), body)


# Shaders whose output is 0 where their sprite texture's alpha is: shader -> that read.
PSP2_ALPHA_DISCARD = {'PropLightMasked': 'tmp1 = tex2D(_MainTex, tmp1.zw);'}


def shader_overlay(exported: Path, game: Path, version: str):
    """Replace AssetRipper's dummy shaders with decompiled source, keeping their .meta GUIDs.
    Also makes Compat Crt's CRT/SinglePass from the game's CRT shaders as rewritten here
    (crt_shader.py), in Resources."""
    import crt_shader

    decompiled = decompile_shaders(game)
    builtins = builtin_uniforms(version)
    overlay, replaced, missing, crt = {}, 0, [], {}
    for path in (exported / 'Assets').rglob('*.shader'):
        text = path.read_text(encoding='utf-8', errors='replace')
        match = SHADER_NAME.match(text)
        if not match or 'DummyShaderTextExporter' not in text:
            continue
        name = match.group(1)
        if name in decompiled:
            text = mask_logic(strip_builtin_declarations(decompiled[name], builtins))
            if name in PSP2_FRAG_COLOR:
                text = psp2_frag_color(text, name, PSP2_FRAG_COLOR[name])
            if name in PSP2_ALPHA_DISCARD:
                text = psp2_alpha_discard(text, name, PSP2_ALPHA_DISCARD[name])
            # The exact skip (noise_split) is made from the original programs, the shader itself
            # gets the fast path behind its keyword.
            original = text
            if name in (crt_shader.FIRST, crt_shader.FINAL):
                crt[name] = text
            if name in NOISE_FAST:
                text = noise_fast(text, name)
            overlay[path.relative_to(exported)] = text.encode('utf-8')
            replaced += 1
            if name in NOISE_SPLITS:
                split = Path('Assets') / 'Resources' / NOISE_SPLITS[name].file_name
                overlay[split] = noise_split(original, name).encode('utf-8')
                overlay[meta_of(split)] = stable_meta('shader', split.name, 'ShaderImporter').encode()
        else:
            missing.append(name)
    if missing:
        sys.exit(f'No decompiled source for the exported shaders {missing}')
    log(f'Replaced {replaced} dummy shaders')
    if len(crt) != 2:
        sys.exit(f'No decompiled {crt_shader.FIRST} and {crt_shader.FINAL} for CRT/SinglePass')
    single = Path('Assets') / 'Resources' / crt_shader.FILE_NAME
    overlay[single] = crt_shader.single_pass(crt[crt_shader.FIRST], crt[crt_shader.FINAL]).encode('utf-8')
    overlay[meta_of(single)] = stable_meta('shader', single.name, 'ShaderImporter').encode()
    return overlay
