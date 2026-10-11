"""Build Unity 2018.2's release Shader object (class 48) for the PS Vita from ShaderLab source.

What the editor's import + build does for a shader:

- ShaderLab outside the program blocks (Properties, SubShader/Pass tags, LOD, render state,
  Fallback, CustomEditor, Dependency) becomes the parsed form. State values bound to a property
  (`ZTest [_ZTest]`) keep the property's name and 0 in place of a value.
- Each program block (with the shader's CGINCLUDE/HLSLINCLUDE text) is compiled once per variant:
  the odometer over its multi_compile/shader_feature directives (include blocks first, the last
  directive varying fastest). The build keeps shader_feature combinations that a material in the
  build uses (all of them for Always Included shaders), drops what the PS Vita cannot run
  (instancing, ETC1 split alpha, fog modes no scene uses) and variants that fail to compile.
  A pass whose `#pragma target` is above 3.0, or whose renderers exclude psp2, gets no programs.
- Each pass lists its subprograms (vertex, then fragment) with names indexed per pass in order of
  first use: per subprogram its vector, matrix and texture parameters, then its keywords.
- The blob holds every subprogram record (vertex records of a pass, then its fragment records),
  LZ4-compressed, for the one platform (10, PSP2).

Not reproduced: the low 16 bits of a project pass's gpuProgramID (a hash the editor computes when
it imports the shader; the reference matches no hash of the snippet text tried), so project
shaders get a stable stand-in that keeps "same snippet, same id". Built-in shaders take theirs
from the editor's unity_builtin_extra, which carries every built-in shader's parsed form and
program snippets.
"""

import contextlib
import hashlib
import itertools
import pickle
import re
import struct
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import lz4.block
import psp2cgc
import shader_bindings as bindings
from gxp import params as gxp_params
from paths import EDITOR_DATA, PROJECT, WORK
from shader_blob import blob as assemble_blob
from shader_blob import subprogram as blob_record
from shaderlab import Program, pragmas, scan, unity_input

# Unity's built-in shader source for 2018.2.19f1 (builtin_shaders-2018.2.19f1.zip, unpacked).
BUILTIN_SOURCE = WORK / 'builtin_shaders'
CACHE = WORK / 'shader_object' / 'cache'

PLATFORM_PSP2 = 10
GPU_VERTEX, GPU_FRAGMENT = 26, 27
HARDWARE_TIER = 3
PASS_NORMAL, PASS_USE, PASS_GRAB = 0, 1, 2
NONINIT = '<noninit>'

PROP_TYPES = {'color': 0, 'vector': 1, 'float': 2, 'int': 2, 'range': 3}
TEX_DIMS = {'2d': 2, '3d': 3, 'cube': 4, '2darray': 5, 'cubearray': 6, 'any': 1, 'rect': 2}
PROP_FLAGS = {'hideininspector': 1, 'perrendererdata': 2, 'noscaleoffset': 4, 'normal': 8, 'hdr': 16, 'gamma': 32}

BLEND = {
    'zero': 0, 'one': 1, 'dstcolor': 2, 'srccolor': 3, 'oneminusdstcolor': 4, 'srcalpha': 5,
    'oneminussrccolor': 6, 'dstalpha': 7, 'oneminusdstalpha': 8, 'srcalphasaturate': 9, 'oneminussrcalpha': 10,
}  # fmt: skip
BLEND_OP = {
    'add': 0, 'sub': 1, 'revsub': 2, 'min': 3, 'max': 4, 'logicalclear': 5, 'logicalset': 6, 'logicalcopy': 7,
    'logicalcopyinverted': 8, 'logicalnoop': 9, 'logicalinvert': 10, 'logicaland': 11, 'logicalnand': 12,
    'logicalor': 13, 'logicalnor': 14, 'logicalxor': 15, 'logicalequiv': 16, 'logicalandreverse': 17,
    'logicalandinverted': 18, 'logicalorreverse': 19, 'logicalorinverted': 20, 'multiply': 21, 'screen': 22,
    'overlay': 23, 'darken': 24, 'lighten': 25, 'colordodge': 26, 'colorburn': 27, 'hardlight': 28,
    'softlight': 29, 'difference': 30, 'exclusion': 31, 'hslhue': 32, 'hslsaturation': 33, 'hslcolor': 34,
    'hslluminosity': 35,
}  # fmt: skip
COMPARE = {
    'disabled': 0, 'never': 1, 'less': 2, 'equal': 3, 'lequal': 4, 'greater': 5, 'notequal': 6, 'gequal': 7,
    'always': 8, 'off': 8,
}  # fmt: skip
STENCIL_OP = {'keep': 0, 'zero': 1, 'replace': 2, 'incrsat': 3, 'decrsat': 4, 'invert': 5, 'incrwrap': 6, 'decrwrap': 7}
CULL = {'off': 0, 'front': 1, 'back': 2}
ON_OFF = {'on': 1, 'off': 0, 'true': 1, 'false': 0}
FOG_MODE = {'off': 0, 'linear': 1, 'exp': 2, 'exp2': 3, 'global': -1}

# Unity keeps tag names and values in one case-insensitive table; the spelling it registered
# first wins, so `"Queue"="Transparent" "IgnoreProjector"="True"` comes out as QUEUE/true.
TAG_SPELLING = {
    s.lower(): s
    for s in (
        'QUEUE', 'IGNOREPROJECTOR', 'LIGHTMODE', 'SHADOWSUPPORT', 'true', 'FORWARDBASE', 'FORWARDADD',
        'SHADOWCASTER', 'DEFERRED', 'PREPASSBASE', 'PREPASSFINAL', 'ALWAYS', 'META', 'MOTIONVECTORS',
    )
}  # fmt: skip

# Shorthand directives and the keyword sets they stand for (None: no keyword).
SHORTHANDS = {
    'multi_compile_fog': [[None, 'FOG_LINEAR', 'FOG_EXP', 'FOG_EXP2']],
    'multi_compile_instancing': [[None, 'INSTANCING_ON']],
    'multi_compile_fwdadd': [['POINT', 'DIRECTIONAL', 'SPOT', 'POINT_COOKIE', 'DIRECTIONAL_COOKIE']],
    'multi_compile_shadowcaster': [['SHADOWS_DEPTH', 'SHADOWS_CUBE']],
    'multi_compile_particles': [[None, 'SOFTPARTICLES_ON']],
}
# Directives whose passes get the SHADOWSUPPORT=true tag.
SHADOW_SUPPORT = {
    'multi_compile_fwdbase', 'multi_compile_fwdadd_fullshadows', 'multi_compile_shadowcaster',
    'multi_compile_lightpass',
}  # fmt: skip
FOG_KEYWORDS = {'FOG_LINEAR': 1, 'FOG_EXP': 2, 'FOG_EXP2': 3}
# Keywords whose variants the PS Vita player never gets: no instancing, stereo or ETC1 split alpha.
PLATFORM_STRIPPED = {
    'INSTANCING_ON', 'PROCEDURAL_INSTANCING_ON', 'STEREO_INSTANCING_ON', 'STEREO_MULTIVIEW_ON',
    'ETC1_EXTERNAL_ALPHA',
}  # fmt: skip
# Requirements a keyword adds to its variant (the editor's m_KeywordTargetInfo).
KEYWORD_REQUIREMENTS = {
    'SHADOWS_SOFT': 227, 'DIRLIGHTMAP_COMBINED': 227, 'DIRLIGHTMAP_SEPARATE': 227, 'DYNAMICLIGHTMAP_ON': 227,
    'SHADOWS_SCREEN': 227,
}  # fmt: skip


# ---------------------------------------------------------------------------------------------
# ShaderLab outside the program blocks


@dataclass
class Token:
    kind: str  # 'word', 'str', 'num', 'punct', 'block'
    value: str
    pos: int
    end: int


BLOCK_END = {'CG': 'ENDCG', 'HLSL': 'ENDHLSL', 'GLSL': 'ENDGLSL'}
BLOCK = re.compile(r'(CG|HLSL|GLSL)(PROGRAM|INCLUDE)\b')
NUMBER = re.compile(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?(?![\w.])')
WORD = re.compile(r'[A-Za-z0-9_][\w.]*')


def tokens(text):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith('//', i):
            j = text.find('\n', i)
            i = n if j < 0 else j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
        elif c == '"':
            j = text.find('"', i + 1)
            yield Token('str', text[i + 1 : j], i, j + 1)
            i = j + 1
        elif m := BLOCK.match(text, i):
            j = text.index(BLOCK_END[m[1]], m.end())
            yield Token('block', m[0], i, j + len(BLOCK_END[m[1]]))
            i = j + len(BLOCK_END[m[1]])
        elif m := NUMBER.match(text, i):
            yield Token('num', m[0], i, m.end())
            i = m.end()
        elif m := WORD.match(text, i):
            yield Token('word', m[0], i, m.end())
            i = m.end()
        else:
            yield Token('punct', c, i, i + 1)
            i += 1


@dataclass
class Prop:
    name: str
    description: str
    attributes: list
    type: int
    flags: int
    default: list
    texture: str = ''
    dim: int = 1


@dataclass
class PassSource:
    kind: int
    name: str = ''
    tags: dict = field(default_factory=dict)
    commands: list = field(default_factory=list)  # render state commands, the subshader's first
    program: object = None  # shaderlab.Program
    use_name: str = ''
    texture_name: str = ''
    lod: int = None  # a LOD inside the pass overrides the subshader's for the pass state


@dataclass
class SubShaderSource:
    tags: dict = field(default_factory=dict)
    lod: int = 0
    passes: list = field(default_factory=list)


@dataclass
class ShaderSource:
    name: str
    path: Path
    text: str
    props: list = field(default_factory=list)
    subshaders: list = field(default_factory=list)
    fallback: str = ''
    fallback_off: bool = False  # `Fallback Off`: no "no subshaders" message either
    custom_editor: str = ''
    dependencies: list = field(default_factory=list)  # (from, to)
    includes: list = field(default_factory=list)  # shaderlab.Include


class ParseError(ValueError):
    pass


class Parser:
    def __init__(self, text, path):
        self.text = text
        self.toks = list(tokens(text))
        self.i = 0
        programs, self.includes = scan(text)
        self.programs = {p.line: p for p in programs}
        self.path = path

    def peek(self, k=0):
        return self.toks[self.i + k] if self.i + k < len(self.toks) else Token('eof', '', len(self.text), 0)

    def next(self):
        t = self.peek()
        self.i += 1
        return t

    def expect(self, value):
        t = self.next()
        if t.value != value:
            line = self.text.count('\n', 0, t.pos) + 1
            raise ParseError(f'{self.path}:{line}: expected {value!r}, got {t.value!r}')
        return t

    def word(self):
        return self.next().value.lower()

    def skip_braces(self):
        self.expect('{')
        depth = 1
        while depth:
            t = self.next()
            if t.kind == 'eof':
                raise ParseError('unbalanced braces')
            if t.value == '{':
                depth += 1
            elif t.value == '}':
                depth -= 1

    def block_program(self, tok):
        """The shaderlab.Program of a program block token (None for include blocks)."""
        if tok.value.endswith('INCLUDE'):
            return None
        line = self.text.count('\n', 0, tok.pos) + 1
        if tok.value.startswith('GLSL'):  # kept in the parsed form, never compiled for the PS Vita
            return Program('GLSL', -1, -1, line, self.text[tok.pos + len(tok.value) : tok.end - len('ENDGLSL')])
        return self.programs.get(line)

    def shader(self):
        if self.next().value.lower() != 'shader':
            raise ParseError(f'{self.path}: not a shader')
        s = ShaderSource(self.next().value, self.path, self.text, includes=self.includes)
        self.expect('{')
        self.body(s, [], 0, {})
        return s

    def body(self, s, commands, lod, tags):
        while (t := self.peek()).value != '}':
            k = t.value.lower()
            if t.kind == 'block':
                self.next()
            elif k == 'properties':
                self.next()
                self.properties(s)
            elif k == 'subshader':
                self.next()
                s.subshaders.append(self.subshader(commands, lod, tags))
            elif k == 'category':
                self.next()
                self.expect('{')
                self.body(s, list(commands), lod, dict(tags))
            elif k == 'lod':
                self.next()
                lod = int(self.next().value)
            elif k == 'fallback':
                self.next()
                v = self.next()
                s.fallback_off = v.kind == 'word' and v.value.lower() == 'off'
                s.fallback = '' if s.fallback_off else v.value
            elif k == 'customeditor':
                self.next()
                s.custom_editor = self.next().value
            elif k == 'dependency':
                self.next()
                a = self.next().value
                self.expect('=')
                s.dependencies.append((a, self.next().value))
            elif k == 'tags':
                self.next()
                tags = {**tags, **self.tags()}
            elif t.kind == 'eof':
                raise ParseError('unexpected end of shader')
            else:
                self.state_command(commands)
        self.next()

    def properties(self, s):
        self.expect('{')
        while self.peek().value != '}':
            s.props.append(self.prop())
        self.next()

    def prop(self):
        attrs, flags = [], 0
        while self.peek().value == '[':
            start = self.next().end
            depth = 1
            while depth:
                t = self.next()
                depth += {'[': 1, ']': -1}.get(t.value, 0)
            raw = ' '.join(self.text[start : t.pos].split())
            flag = PROP_FLAGS.get(raw.lower())
            if flag:
                flags |= flag
            else:
                attrs.append(raw)
        name = self.next().value
        self.expect('(')
        description = self.next().value
        self.expect(',')
        kind = self.next().value
        type_args = []
        if kind.lower() == 'range':
            self.expect('(')
            type_args.append(float(self.next().value))
            self.expect(',')
            type_args.append(float(self.next().value))
            self.expect(')')
        self.expect(')')
        self.expect('=')
        k = kind.lower()
        if k in TEX_DIMS:
            texture = self.next().value
            if self.peek().value == '{':
                self.skip_braces()
            return Prop(name, description, attrs, 4, flags, [0.0] * 4, texture, TEX_DIMS[k])
        if self.peek().value == '(':
            self.next()
            values = []
            while self.peek().value != ')':
                t = self.next()
                if t.value != ',':
                    values.append(float(t.value))
            self.next()
            values = (values + [1.0 if k == 'color' and len(values) == 3 else 0.0] + [0.0] * 3)[:4]
        else:
            values = [float(self.next().value), 0.0, 0.0, 0.0]
        if k == 'range':
            values = [values[0], type_args[0], type_args[1], 0.0]
        return Prop(name, description, attrs, PROP_TYPES[k], flags, [f32(v) for v in values])

    def tags(self):
        self.expect('{')
        out = {}
        while self.peek().value != '}':
            k = self.next().value
            self.expect('=')
            out[k] = self.next().value
        self.next()
        return out

    def subshader(self, inherited, lod, tags):
        sub = SubShaderSource(tags=dict(tags), lod=lod)
        commands = list(inherited)
        self.expect('{')
        while (t := self.peek()).value != '}':
            k = t.value.lower()
            if t.kind == 'block':
                self.next()
            elif k == 'tags':
                self.next()
                sub.tags.update(self.tags())
            elif k == 'lod':
                self.next()
                sub.lod = int(self.next().value)
            elif k == 'pass':
                self.next()
                sub.passes.append(self.pass_(commands))
            elif k == 'usepass':
                self.next()
                sub.passes.append(PassSource(PASS_USE, use_name=self.next().value))
            elif k == 'grabpass':
                self.next()
                sub.passes.append(self.grab_pass())
            elif t.kind == 'eof':
                raise ParseError('unexpected end of subshader')
            else:
                self.state_command(commands)
        self.next()
        return sub

    def pass_(self, inherited):
        p = PassSource(PASS_NORMAL, commands=list(inherited))
        self.expect('{')
        while (t := self.peek()).value != '}':
            k = t.value.lower()
            if t.kind == 'block':
                self.next()
                prog = self.block_program(t)
                if prog is not None:
                    p.program = prog
            elif k == 'name':
                self.next()
                p.name = self.next().value
            elif k == 'tags':
                self.next()
                p.tags.update(self.tags())
            elif k == 'lod':
                self.next()
                p.lod = int(self.next().value)
            elif t.kind == 'eof':
                raise ParseError('unexpected end of pass')
            else:
                self.state_command(p.commands)
        self.next()
        return p

    def grab_pass(self):
        p = PassSource(PASS_GRAB)
        self.expect('{')
        while (t := self.peek()).value != '}':
            k = t.value.lower()
            if t.kind == 'str':
                p.texture_name = self.next().value
            elif k == 'name':
                self.next()
                p.name = self.next().value
            elif k == 'tags':
                self.next()
                p.tags.update(self.tags())
            else:
                self.next()
        self.next()
        return p

    def value(self):
        """A state value: a number or word, or a property reference [name] -> (value, property)."""
        t = self.next()
        if t.value == '[':
            name = self.next().value
            self.expect(']')
            return (None, name)
        return (t.value, None)

    def state_command(self, commands):
        t = self.next()
        k = t.value.lower()
        if k in ('cull', 'zwrite', 'ztest', 'zclip', 'alphatomask', 'lighting', 'separatespecular', 'colormaterial'):
            commands.append((k, [self.value()]))
        elif k == 'blend':
            args = [self.value()]
            if args[0][0] is not None and args[0][0].lower() == 'off':
                commands.append(('blend', [('off', None)]))
                return
            if args[0][0] is not None and args[0][0].isdigit():  # Blend N src dst: per render target
                rt = int(args[0][0])
                args = [self.value()]
            else:
                rt = None
            if args[0][0] is not None and args[0][0].lower() == 'off':
                commands.append(('blend', [('off', None)], rt))
                return
            args.append(self.value())
            if self.peek().value == ',':
                self.next()
                args += [self.value(), self.value()]
            commands.append(('blend', args, rt))
        elif k == 'blendop':
            args = [self.value()]
            if self.peek().value == ',':
                self.next()
                args.append(self.value())
            commands.append(('blendop', args))
        elif k == 'colormask':
            args = [self.value()]
            if self.peek().kind == 'num' and self.peek(1).value.lower() not in ('', '}'):
                args.append(self.value())
            commands.append(('colormask', args))
        elif k == 'offset':
            a = self.value()
            self.expect(',')
            commands.append(('offset', [a, self.value()]))
        elif k == 'stencil':
            self.expect('{')
            while self.peek().value != '}':
                commands.append(('stencil', [self.next().value.lower(), self.value()]))
            self.next()
        elif k == 'fog':
            self.expect('{')
            while self.peek().value != '}':
                key = self.word()
                if key == 'color':
                    if self.peek().value == '(':
                        self.next()
                        values = []
                        while self.peek().value != ')':
                            v = self.next()
                            if v.value != ',':
                                values.append(float(v.value))
                        self.next()
                        commands.append(('fogcolor', values))
                    else:
                        commands.append(('fogcolor', [self.value()]))
                elif key == 'range':
                    a = self.value()
                    self.expect(',')
                    commands.append(('fogrange', [a, self.value()]))
                else:
                    commands.append(('fog' + key, [self.value()]))
            self.next()
        elif k in ('material', 'settexture', 'bindchannels'):
            if k == 'settexture':
                self.value()
            self.skip_braces()
        elif k == 'alphatest':
            a = self.value()
            if self.peek().kind in ('num', 'punct') and self.peek().value != '}':
                self.value()
            commands.append(('alphatest', [a]))
        elif k == 'name':
            self.next()
        elif k in ('color', 'shininess'):  # fixed function material values
            if self.peek().value == '(':
                while self.next().value != ')':
                    pass
            else:
                self.value()
        else:
            line = self.text.count('\n', 0, t.pos) + 1
            raise ParseError(f'{self.path}:{line}: unknown ShaderLab command {t.value!r}')


def parse(text, path):
    return Parser(text, path).shader()


# ---------------------------------------------------------------------------------------------
# Render state


def f32(v):
    return struct.unpack('<f', struct.pack('<f', v))[0]


def fv(val=0.0, name=NONINIT):
    return {'val': float(val), 'name': name}


def default_state(grab=False):
    z = 0.0 if grab else None

    def blend():
        return {
            'srcBlend': fv(1),
            'destBlend': fv(0),
            'srcBlendAlpha': fv(1),
            'destBlendAlpha': fv(0),
            'blendOp': fv(0),
            'blendOpAlpha': fv(0),
            'colMask': fv(15),
        }

    def stencil():
        return {'pass': fv(0), 'fail': fv(0), 'zFail': fv(0), 'comp': fv(z if grab else 8)}

    fog = NONINIT if grab else None
    state = {'m_Name': ''}
    state.update({f'rtBlend{i}': blend() for i in range(8)})
    state.update(
        {
            'rtSeparateBlend': False,
            'zClip': fv(0 if grab else 1),
            'zTest': fv(0 if grab else 4),
            'zWrite': fv(0 if grab else 1),
            'culling': fv(0 if grab else 2),
            'offsetFactor': fv(0),
            'offsetUnits': fv(0),
            'alphaToMask': fv(0),
            'stencilOp': stencil(),
            'stencilOpFront': stencil(),
            'stencilOpBack': stencil(),
            'stencilReadMask': fv(0 if grab else 255),
            'stencilWriteMask': fv(0 if grab else 255),
            'stencilRef': fv(0),
            'fogStart': fv(0, fog or 'unity_FogStart'),
            'fogEnd': fv(0, fog or 'unity_FogEnd'),
            'fogDensity': fv(0, fog or 'unity_FogDensity'),
            'fogColor': {'x': fv(0), 'y': fv(0), 'z': fv(0), 'w': fv(0), 'name': fog or 'unity_FogColor'},
            'fogMode': -1,
            'gpuProgramID': -1,
            'm_Tags': {'tags': []},
            'm_LOD': 0,
            'lighting': False,
        }
    )
    return state


def resolve(arg, table):
    """A state value from (word, None) or (None, property) -> {'val', 'name'}."""
    word, prop = arg
    if prop is not None:
        return fv(0, prop)
    w = word.lower()
    if w in table:
        return fv(table[w])
    return fv(float(word))


def color_mask(arg):
    word, prop = arg
    if prop is not None:
        return fv(0, prop)
    if word == '0':
        return fv(0)
    bits = {'r': 8, 'g': 4, 'b': 2, 'a': 1}
    return fv(sum(bits[c] for c in word.lower()))


def apply_state(state, commands):
    for command in commands:
        k, args = command[0], command[1]
        if k == 'cull':
            state['culling'] = resolve(args[0], CULL)
        elif k == 'zwrite':
            state['zWrite'] = resolve(args[0], ON_OFF)
        elif k == 'ztest':
            state['zTest'] = resolve(args[0], COMPARE)
        elif k == 'zclip':
            state['zClip'] = resolve(args[0], ON_OFF)
        elif k == 'alphatomask':
            state['alphaToMask'] = resolve(args[0], ON_OFF)
        elif k == 'lighting':
            state['lighting'] = bool(resolve(args[0], ON_OFF)['val'])
        elif k == 'blend':
            rt = command[2] if len(command) > 2 else None
            targets = [f'rtBlend{rt}'] if rt is not None else ['rtBlend0']
            if rt is not None:
                state['rtSeparateBlend'] = True
            for target in targets:
                b = state[target]
                if args[0][0] == 'off':
                    b.update(srcBlend=fv(1), destBlend=fv(0), srcBlendAlpha=fv(1), destBlendAlpha=fv(0))
                    continue
                src, dst = resolve(args[0], BLEND), resolve(args[1], BLEND)
                src_a, dst_a = (resolve(args[2], BLEND), resolve(args[3], BLEND)) if len(args) == 4 else (src, dst)
                b.update(srcBlend=src, destBlend=dst, srcBlendAlpha=dict(src_a), destBlendAlpha=dict(dst_a))
        elif k == 'blendop':
            op = resolve(args[0], BLEND_OP)
            state['rtBlend0']['blendOp'] = op
            state['rtBlend0']['blendOpAlpha'] = resolve(args[1], BLEND_OP) if len(args) > 1 else dict(op)
        elif k == 'colormask':
            rt = int(args[1][0]) if len(args) > 1 else 0
            state[f'rtBlend{rt}']['colMask'] = color_mask(args[0])
        elif k == 'offset':
            state['offsetFactor'] = resolve(args[0], {})
            state['offsetUnits'] = resolve(args[1], {})
        elif k == 'stencil':
            key, arg = args
            simple = {'ref': 'stencilRef', 'readmask': 'stencilReadMask', 'writemask': 'stencilWriteMask'}
            if key in simple:
                state[simple[key]] = resolve(arg, {})
                continue
            m = re.fullmatch(r'(comp|pass|fail|zfail)(front|back)?', key)
            if not m:
                raise ParseError(f'unknown stencil command {key}')
            field_name = {'comp': 'comp', 'pass': 'pass', 'fail': 'fail', 'zfail': 'zFail'}[m[1]]
            target = {None: 'stencilOp', 'front': 'stencilOpFront', 'back': 'stencilOpBack'}[m[2]]
            state[target][field_name] = resolve(arg, COMPARE if m[1] == 'comp' else STENCIL_OP)
        elif k == 'fogmode':
            state['fogMode'] = int(resolve(args[0], FOG_MODE)['val'])
        elif k == 'fogcolor':
            if len(args) == 1 and isinstance(args[0], tuple):
                state['fogColor']['name'] = args[0][1]
            else:
                for c, v in zip('xyzw', args, strict=False):
                    state['fogColor'][c] = fv(v)
                state['fogColor']['name'] = NONINIT
        elif k == 'fogdensity':
            state['fogDensity'] = resolve(args[0], {})
        elif k == 'fogrange':
            state['fogStart'] = resolve(args[0], {})
            state['fogEnd'] = resolve(args[1], {})
    return state


def canonical_tag(s):
    return TAG_SPELLING.get(s.lower(), s)


def tag_list(*tag_dicts):
    out = {}
    for tags in tag_dicts:
        for k, v in tags.items():
            out[canonical_tag(k)] = canonical_tag(v)
    return sorted(out.items())


# ---------------------------------------------------------------------------------------------
# Variants


def directive_sets(name, args):
    """The keyword sets of one directive, or None if it is not a variant directive."""
    if name in SHORTHANDS:
        return SHORTHANDS[name]
    if name in ('multi_compile', 'shader_feature'):
        words = [None if set(a) == {'_'} else a for a in args]
        if name == 'shader_feature' and len(words) == 1:
            words = [None, *words]
        return [words]
    if name.startswith('multi_compile_'):
        return [('unsupported', name)]
    return None


@dataclass
class Snippet:
    """One program block with what its pragmas say."""

    target: str
    vertex: str
    fragment: str
    directives: list  # [(kind, [keyword or None, ...])], kind 'multi_compile' or 'shader_feature'
    renderers_ok: bool
    instancing: bool
    procedural_instancing: bool
    program_mask: int  # 1 << stage for vertex, fragment, geometry, hull, domain
    shadow_support: bool


def snippet_info(program, includes):
    found = pragmas(''.join(i.body for i in includes if i.lang == program.lang) + program.body)
    directives, info = [], {}
    instancing = procedural = False
    for name, args in found:
        sets = directive_sets(name, args)
        if sets is not None:
            kind = 'shader_feature' if name == 'shader_feature' else 'multi_compile'
            directives += [(kind, s) for s in sets]
            instancing |= name == 'multi_compile_instancing'
        elif name == 'instancing_options':
            procedural |= any(a.startswith('procedural:') for a in args)
        else:
            info[name] = args
    ok = True
    if 'only_renderers' in info:
        ok = 'psp2' in info['only_renderers']
    if 'exclude_renderers' in info and 'psp2' in info['exclude_renderers']:
        ok = False
    target = info.get('target', ['2.5'])[0]
    target = f'{float(target):.1f}'
    stages = ('vertex', 'fragment', 'geometry', 'hull', 'domain')
    mask = sum(2 << i for i, stage in enumerate(stages) if stage in info) or 6
    return Snippet(
        target,
        info.get('vertex', [None])[0],
        info.get('fragment', [None])[0],
        directives,
        ok,
        instancing,
        procedural,
        mask,
        any(name in SHADOW_SUPPORT for name, _ in found),
    )


def enumerate_variants(snippet, material_sets=(), all_features=False, fog_modes=()):
    """The keyword combinations the build compiles for a snippet, in Unity's order (odometer over
    the directives, the last one fastest), before compile failures are dropped."""
    unsupported = [s[1] for _, s in snippet.directives if isinstance(s, tuple)]
    if unsupported:
        raise NotImplementedError(f'#pragma {unsupported[0]}')
    choices = [s for _, s in snippet.directives]
    features = {k for kind, s in snippet.directives if kind == 'shader_feature' for k in s if k}
    allowed = None
    if features and not all_features:
        allowed = {frozenset()}
        for keywords in material_sets:
            used = frozenset(keywords) & features
            allowed.add(used)
    out = []
    for combo in itertools.product(*choices):
        keywords = [k for k in combo if k]
        if allowed is not None and frozenset(k for k in keywords if k in features) not in allowed:
            continue
        if any(k in PLATFORM_STRIPPED for k in keywords):
            continue
        if any(k in FOG_KEYWORDS and FOG_KEYWORDS[k] not in fog_modes for k in keywords):
            continue
        out.append(keywords)
    return out


class KeywordRegistry:
    """Unity's global keyword table: a variant lists its keywords in table order. Keywords get
    their place when a shader that uses them is first loaded, in the order of its directives."""

    def __init__(self):
        self.index = {}

    def register(self, snippet):
        for _, s in snippet.directives:
            for k in [] if isinstance(s, tuple) else s:
                if k and k not in self.index:
                    self.index[k] = len(self.index)

    def order(self, keywords):
        return sorted(keywords, key=lambda k: self.index.get(k, len(self.index)))


# ---------------------------------------------------------------------------------------------
# Compiling


MISSING = object()
TARGET_OF = {v: k for k, v in psp2cgc.REQUIREMENTS.items()}


def cache_path(text, shader_dir, stage, entry, target, keywords):
    key = repr(('bindings-3', text, str(shader_dir), stage, entry, target, sorted(keywords)))
    digest = hashlib.sha256(key.encode()).hexdigest()
    return CACHE / digest[:2] / f'{digest}.pickle'


def compile_at(text, shader_dir, stage, entry, target, keywords, level):
    """psp2cgc.compile at another optimization level (it always uses -O3): (gxp, sdb) or None."""
    import subprocess

    vertex = stage == 'vp'
    defines = psp2cgc.TARGET[target] + psp2cgc.PLATFORM + [k + '=1' for k in keywords]
    defines += ['UNITY_VERSION=201839', 'SHADER_STAGE_VERTEX=1' if vertex else 'SHADER_STAGE_FRAGMENT=1']
    defines += psp2cgc.AVAILABLE
    psp2cgc.TMP.mkdir(parents=True, exist_ok=True)
    src = psp2cgc.TMP / f'GXM_{"VP" if vertex else "FP"}_{uuid.uuid4().hex[:16]}'
    out, sdb = src.with_suffix('.gxp'), src.with_suffix('.sdb')
    src.write_bytes(text.encode())
    command = [str(psp2cgc.PSP2CGC), str(src), level, '-W3', '-fastmath', '-pedantic', '-fx', '-nofastint']
    command += ['-D' + d for d in defines]
    includes = [shader_dir, PROJECT, psp2cgc.CGINCLUDES, psp2cgc.PSP2_PLAYER / 'CgBatchPlugins64' / 'include']
    command += [f'-I{d}' for d in includes]
    command += ['-profile', 'sce_vp_psp2' if vertex else 'sce_fp_psp2', '-entry', entry, '-o', str(out)]
    command += ['-xmlsdb', str(sdb)]
    result = subprocess.run(command, capture_output=True, cwd=PROJECT)
    data = (out.read_bytes(), sdb.read_text(encoding='utf-8')) if result.returncode == 0 and out.exists() else None
    for path in (src, out, sdb):
        path.unlink(missing_ok=True)
    return data


def compile_program(text, shader_dir, stage, entry, target, keywords):
    """(binding data, code bytes) of one program, or None if psp2cgc rejects it."""
    # psp2cgc now and then fails a program it compiles fine on the next try when many run at
    # once; a failure only counts when it repeats.
    for _ in range(3):
        result, log = psp2cgc.compile(text, shader_dir, stage, entry, target, sorted(keywords))
        if result is not None:
            break
    # When psp2cgc crashes at -O3, the editor's PS Vita compiler steps the optimization down
    # (Hidden/Internal-DepthNormalsTexture's tree passes come out of -O1).
    if result is None and 'internal compiler error' in log:
        for level in ('-O2', '-O1', '-O0'):
            result = compile_at(text, shader_dir, stage, entry, target, sorted(keywords), level)
            if result is not None:
                break
    if result is None:
        return None
    gxp, sdb = result
    b = bindings.derive(gxp, bindings.attribute_semantics(sdb), bindings.sampler_dims(sdb))
    refine(b, gxp, sdb)
    return b, bindings.code_bytes(gxp, b)


SDB_ATTRIBUTE = re.compile(r'<attribute symbolName="([^"]+)"[^>]*declID="(\d+)"')
SDB_UNIFORM = re.compile(r'<uniform symbolName="([^"]+)"[^>]*declID="(\d+)"')
SDB_DECL = re.compile(r'<(?:vardecl|paramdecl) declID="(\d+)" name="[^"]*" type="([^"]+)"')
MATRIX_TYPE = re.compile(r'^[a-z]+4x4(?:\[(\d+)\])?$')


def refine(b, gxp, sdb):
    """What the GXP alone cannot tell, from the SDB's declarations:
    - a float4x4 is a matrix and a float4[4] a vector array (both are four float4 rows);
    - vertex channels come per entry parameter, the last declared first (a struct parameter's
      members keep their order)."""
    decl_type = dict(SDB_DECL.findall(sdb))
    uniform_type = {name: decl_type.get(d, '') for name, d in SDB_UNIFORM.findall(sdb)}
    consts = []
    for entry in b['vectors'] + b['matrices']:
        t = uniform_type.get(entry['name'])
        m = MATRIX_TYPE.match(t) if t else None
        if t is None:
            consts.append(entry)
            continue
        if m:
            entry.update(rows=4, cols=4, arr=int(m[1] or 0))
        elif entry['rows'] == 4:  # an array of rows the GXP made look like a matrix
            entry.update(rows=1, cols=4, arr=4)
        consts.append(entry)
    b['matrices'] = sorted((c for c in consts if c['rows'] == 4), key=lambda c: c['index'])
    b['vectors'] = sorted((c for c in consts if c['rows'] != 4), key=lambda c: c['index'])
    decl = {name.split('.')[0]: int(d) for name, d in SDB_ATTRIBUTE.findall(sdb)}
    semantics = bindings.attribute_semantics(sdb)
    attrs = [p for p in gxp_params(gxp) if p['cat'] == 'attr']
    keyed = sorted(
        (
            -decl.get(p['name'].split('.')[0], 0),
            i,
            (bindings.channel(p), bindings.target(p, semantics.get(p['name'], 'TEXCOORD'))),
        )
        for i, p in enumerate(attrs)
    )
    b['chans'] = list(dict.fromkeys(c for _, _, c in keyed))


def cached(path):
    """A kept compile result, or MISSING (also for a file a crash left unfinished)."""
    try:
        return pickle.loads(path.read_bytes())
    except (OSError, EOFError, pickle.UnpicklingError):
        return MISSING


def compile_jobs(text, shader_dir, jobs, pool):
    """[(binding data, code bytes) or None] for [(stage, entry, target, keywords)]. Results are
    kept under WORK, keyed by everything psp2cgc sees."""
    paths_ = [cache_path(text, shader_dir, *job) for job in jobs]

    def run(i):
        result = compile_program(text, shader_dir, *jobs[i])
        paths_[i].parent.mkdir(parents=True, exist_ok=True)
        tmp = paths_[i].with_suffix(f'.{uuid.uuid4().hex}.tmp')
        tmp.write_bytes(pickle.dumps(result))
        try:
            tmp.replace(paths_[i])
        except PermissionError:  # Windows: someone is reading the same result right now
            tmp.unlink(missing_ok=True)
        return result

    results = [cached(path) for path in paths_]
    todo = list({paths_[i]: i for i, r in enumerate(results) if r is MISSING}.values())
    done = dict(zip([paths_[i] for i in todo], pool.map(run, todo), strict=True))
    return [done[path] if r is MISSING else r for path, r in zip(paths_, results, strict=True)]


def compile_variants(text, shader_dir, snippet, variants, pool):
    """[(vertex bindings, fragment bindings, vertex code, fragment code) or None] per variant: a
    variant either stage rejects is dropped."""
    # A keyword that needs more than the snippet's target (SHADOWS_SCREEN: 3.0) raises its variant's
    # compile target.
    jobs = [
        (stage, entry, TARGET_OF[requirements(snippet.target, keywords)], keywords)
        for keywords in variants
        for stage, entry in (('vp', snippet.vertex), ('fp', snippet.fragment))
    ]
    results = compile_jobs(text, shader_dir, jobs, pool)
    out = []
    for vp, fp in zip(results[::2], results[1::2], strict=True):
        out.append(None if vp is None or fp is None else (vp[0], fp[0], vp[1], fp[1]))
    return out


def fill_programs(out, stages, records, requirements_of):
    """Put a pass's compiled programs into its parsed form and the blob records.

    stages: [(gpu type, 'progVertex'/'progFragment', [(keywords, bindings, code bytes)])] in
    stage order. Names get their index in order of first use: per stage, per subprogram its
    vector, matrix and texture parameters, then its keywords."""
    names = {}
    for _, _, kept in stages:
        for keywords, b, _ in kept:
            for group in ('vectors', 'matrices', 'textures'):
                for item in b[group]:
                    names.setdefault(item['name'], len(names))
            for k in keywords:
                names.setdefault(k, len(names))
    out['m_NameIndices'] = sorted(names.items())
    for gpu_type, key, kept in stages:
        subs = []
        for keywords, b, code in kept:
            reqs = requirements_of(keywords)
            subs.append(subprogram_dict(len(records), b, [names[k] for k in keywords], gpu_type, reqs, names))
            records.append(blob_record(gpu_type, keywords, code, bindings.tail_bytes(b)))
        out[key] = {'m_SubPrograms': subs}


# ---------------------------------------------------------------------------------------------
# The Shader object


def gpu_program_id(index, code):
    """Stand-in for the editor's snippet id: the snippet's index in its shader in the high bits,
    16 bits of a hash of the snippet code (as the editor stores it) in the low bits."""
    low = struct.unpack_from('<H', hashlib.sha1(code.encode()).digest())[0]
    return (index << 16) | low


def snippet_code(text, program, includes):
    """The snippet text the editor stores (the compile input without the PS Vita header line)."""
    return unity_input(text, program, includes).split('\n', 1)[1].replace('//#prag', '#pragma')


def requirements(target, keywords):
    out = psp2cgc.REQUIREMENTS[target]
    for k in keywords:
        out |= KEYWORD_REQUIREMENTS.get(k, 0)
    return out


def empty_program():
    return {'m_SubPrograms': []}


def subprogram_dict(blob_index, b, keyword_indices, gpu_type, reqs, nameidx):
    lists = bindings.unity_lists(b, nameidx)
    sp = {
        'm_BlobIndex': blob_index,
        'm_Channels': lists['m_Channels'],
        'm_KeywordIndices': keyword_indices,
        'm_ShaderHardwareTier': HARDWARE_TIER,
        'm_GpuProgramType': gpu_type,
        'm_VectorParams': lists['m_VectorParams'],
        'm_MatrixParams': lists['m_MatrixParams'],
        'm_TextureParams': lists['m_TextureParams'],
        'm_BufferParams': [],
        'm_ConstantBuffers': [],
        'm_ConstantBufferBindings': [],
        'm_UAVParams': [],
        'm_Samplers': [],
        'm_ShaderRequirements': reqs,
    }
    if gpu_type == GPU_FRAGMENT:
        sp['m_Channels'] = {'m_Channels': [], 'm_SourceMap': 0}
    return sp


def prop_dict(p):
    out = {
        'm_Name': p.name,
        'm_Description': p.description,
        'm_Attributes': p.attributes,
        'm_Type': p.type,
        'm_Flags': p.flags,
    }
    out.update({f'm_DefValue[{i}]': float(v) for i, v in enumerate(p.default)})
    out['m_DefTexture'] = {'m_DefaultName': p.texture, 'm_TexDim': p.dim}
    return out


@dataclass
class BuildOptions:
    material_sets: tuple = ()  # keyword sets of the build's materials that use this shader
    all_features: bool = False  # Always Included shaders get every shader_feature combination
    fog_modes: tuple = ()  # fog modes (1 linear, 2 exp, 3 exp2) whose variants are kept
    snippet_ids: dict = None  # snippet index -> gpuProgramID, when known (built-in shaders)
    snippet_codes: dict = None  # snippet index -> the editor's snippet text (built-in shaders)
    dependency: object = None  # shader name -> PPtr dict, for Fallback/Dependency/UsePass


def build(source, options, registry, pool):
    """The release Shader tree for a parsed ShaderSource (see parse())."""
    shader_dir = source.path.parent
    records = []
    subshaders = []
    snippets = [p.program for sub in source.subshaders for p in sub.passes if p.program is not None]
    order = {id(p): i for i, p in enumerate(sorted(snippets, key=lambda p: p.line))}
    for sub in source.subshaders:
        sub_tags = tag_list(sub.tags)
        passes = []
        for ps in sub.passes:
            state = default_state(ps.kind != PASS_NORMAL)
            out = {
                'm_NameIndices': [],
                'm_Type': ps.kind,
                'm_State': state,
                'm_ProgramMask': 0,
                'progVertex': empty_program(),
                'progFragment': empty_program(),
                'progGeometry': empty_program(),
                'progHull': empty_program(),
                'progDomain': empty_program(),
                'm_HasInstancingVariant': False,
                'm_HasProceduralInstancingVariant': False,
                'm_UseName': ps.use_name,
                'm_Name': '',
                'm_TextureName': ps.texture_name,
                'm_Tags': {'tags': []},
            }
            passes.append(out)
            if ps.kind == PASS_GRAB:
                out['m_Tags'] = {'tags': tag_list(ps.tags)}
            if ps.kind != PASS_NORMAL:
                continue
            apply_state(state, ps.commands)
            state['m_Name'] = ps.name
            info = ps.program and snippet_info(ps.program, source.includes)
            extra = {'SHADOWSUPPORT': 'true'} if info and info.shadow_support else {}
            state['m_Tags'] = {'tags': tag_list(sub.tags, ps.tags, extra)}
            state['m_LOD'] = sub.lod if ps.lod is None else ps.lod
            if ps.program is None:
                continue
            index = order[id(ps.program)]
            code = (options.snippet_codes or {}).get(index) or snippet_code(source.text, ps.program, source.includes)
            known = (options.snippet_ids or {}).get(index)
            state['gpuProgramID'] = known if known is not None else gpu_program_id(index, code)
            out['m_ProgramMask'] = info.program_mask
            registry.register(info)
            out['m_HasInstancingVariant'] = info.instancing
            out['m_HasProceduralInstancingVariant'] = info.procedural_instancing
            if ps.program.lang == 'GLSL' or not info.renderers_ok or float(info.target) > 3.0:
                continue
            variants = enumerate_variants(info, options.material_sets, options.all_features, options.fog_modes)
            variants = [registry.order(v) for v in variants]
            text = '#include "HLSLToPSP2.cginc"\n' + code.replace('#pragma', '//#prag')
            if options.snippet_codes is None:
                text = unity_input(source.text, ps.program, source.includes)
            results = compile_variants(text, shader_dir, info, variants, pool)
            kept = [(v, r) for v, r in zip(variants, results, strict=True) if r is not None]
            stages = [
                (GPU_VERTEX, 'progVertex', [(v, r[0], r[2]) for v, r in kept]),
                (GPU_FRAGMENT, 'progFragment', [(v, r[1], r[3]) for v, r in kept]),
            ]
            fill_programs(out, stages, records, lambda keywords, info=info: requirements(info.target, keywords))
        subshaders.append({'m_Passes': passes, 'm_Tags': {'tags': sub_tags}, 'm_LOD': sub.lod})

    raw = assemble_blob(records)
    compressed = lz4.block.compress(raw, mode='high_compression', compression=9, store_size=False)
    dependency = options.dependency or (lambda name: None)
    deps = []
    for name in [source.fallback] + [to for _, to in source.dependencies]:
        pptr = dependency(name) if name else None
        if pptr is not None and pptr not in deps:
            deps.append(pptr)
    return {
        'm_Name': '',
        'm_ParsedForm': {
            'm_PropInfo': {'m_Props': [prop_dict(p) for p in source.props]},
            'm_SubShaders': subshaders,
            'm_Name': source.name,
            'm_CustomEditorName': source.custom_editor,
            'm_FallbackName': source.fallback,
            'm_Dependencies': [{'from': a, 'to': b} for a, b in source.dependencies],
            'm_DisableNoSubshadersMessage': source.fallback_off,
        },
        'platforms': [PLATFORM_PSP2],
        'offsets': [0],
        'compressedLengths': [len(compressed)],
        'decompressedLengths': [len(raw)],
        'compressedBlob': compressed,
        'm_Dependencies': deps,
        'm_NonModifiableTextures': [],
        'm_ShaderIsBaked': True,
    }


# ---------------------------------------------------------------------------------------------
# Built-in shaders and the project


@cache
def editor_builtin_shaders():
    """fileID -> type tree of every shader in the editor's unity_builtin_extra (parsed forms and
    program snippets, no compiled programs)."""
    import UnityPy

    env = UnityPy.load(str(EDITOR_DATA / 'Resources' / 'unity_builtin_extra'))
    sf = next(iter(env.files.values()))
    return {pid: o.read_typetree() for pid, o in sf.objects.items() if o.type.name == 'Shader'}


@cache
def builtin_sources():
    """Shader name -> .shader file in Unity's built-in shader source."""
    from shaderlab import SHADER_NAME

    out = {}
    for path in sorted(BUILTIN_SOURCE.rglob('*.shader')):
        m = SHADER_NAME.search(path.read_text(encoding='utf-8', errors='replace'))
        if m:
            out.setdefault(m[1], path)
    return out


def builtin_options(file_id):
    """Snippet ids and texts the editor recorded for a built-in shader."""
    tree = editor_builtin_shaders().get(file_id)
    if tree is None:
        return None, None
    snippets = [(k, v) for k, v in tree['m_CompileInfo']['m_Snippets'] if not v['m_FromOther']]
    snippets.sort(key=lambda kv: kv[0] >> 16)
    ids = {k >> 16: k for k, _ in snippets}
    codes = {k >> 16: v['m_Code'] for k, v in snippets}
    return ids, codes


def load(path):
    text = path.read_text(encoding='utf-8', newline='')
    return parse(text, path)


BUILTIN_EXTRA = '0000000000000000f000000000000000'
FOG_STRIP_AUTOMATIC = 0
SCENE_FOG = re.compile(r'^RenderSettings:\n(?:  .*\n)*?  m_Fog: (\d)\n(?:  .*\n)*?  m_FogMode: (\d)', re.M)


def fog_modes(project, scene_paths):
    """The fog modes whose variants the build keeps: those the build's scenes use (automatic
    stripping) or the ones GraphicsSettings keeps (custom)."""
    import unityyaml

    graphics = unityyaml.meta(project / 'ProjectSettings' / 'GraphicsSettings.asset')['GraphicsSettings']
    if int(graphics.get('m_FogStripping', 0)) != FOG_STRIP_AUTOMATIC:
        keep = ('m_FogKeepLinear', 'm_FogKeepExp', 'm_FogKeepExp2')
        return tuple(i + 1 for i, k in enumerate(keep) if graphics.get(k, '1') == '1')
    modes = set()
    for path in scene_paths:
        m = SCENE_FOG.search((project / path).read_text(encoding='utf-8', errors='replace'))
        if m and m[1] == '1':
            modes.add(int(m[2]))
    return tuple(sorted(modes))


class ProjectShaders:
    """Every Shader object the layout places, built the way the editor's player build does.

    shaders = ProjectShaders(layout, placement)
    tree = shaders.build(file_name, guid, file_id)   # in the build's order, see build_all()
    """

    def __init__(self, layout, placement, project=PROJECT):
        from layout import always_included_shaders

        self.layout, self.placement, self.project = layout, placement, project
        self.always = always_included_shaders(project)
        graphics = (project / 'ProjectSettings' / 'GraphicsSettings.asset').read_text(encoding='utf-8')
        block = graphics.split('m_AlwaysIncludedShaders:')[1].split('m_PreloadedShaders')[0]
        refs = re.findall(r'fileID: (\d+), guid: ([0-9a-f]{32})', block)
        self.always_builtin = {int(fid) for fid, g in refs if g == BUILTIN_EXTRA}
        self.fog = fog_modes(project, [s['path'] for s in layout.scenes])
        self.registry = KeywordRegistry()
        self.pool = ThreadPoolExecutor(16)
        self.materials = {}  # (guid, fileID) of a shader -> [keyword sets]
        for g in layout.file_of:
            a = layout.db.by_guid[g]
            for fid, cid in a.classes.items():
                if cid != 21 or not a.is_yaml:
                    continue
                doc = a.docs.get(fid)
                if doc is None:
                    continue
                ref = doc.fields.get('m_Shader') or {}
                key = (ref.get('guid', g), int(ref.get('fileID', 0)))
                keywords = (doc.fields.get('m_ShaderKeywords') or '').split()
                self.materials.setdefault(key, []).append(frozenset(keywords))
        for fid, (shader, keywords) in _builtin_materials().items():
            if fid in layout.builtin_file:
                self.materials.setdefault((BUILTIN_EXTRA, shader), []).append(frozenset(keywords))
        self.names = {}

    def source(self, guid, file_id):
        if guid == BUILTIN_EXTRA:
            name = editor_builtin_shaders()[file_id]['m_ParsedForm']['m_Name']
            return load(builtin_sources()[name])
        return load(self.project / self.layout.db.by_guid[guid].path)

    def dependency(self, file_name):
        def resolve(name):
            where = self.names.get(name)
            if where is None:
                return None
            f, pid = where
            return {'m_FileID': 0, 'm_PathID': pid, 'file': f}

        return resolve

    def build(self, file_name, guid, file_id):
        src = self.source(guid, file_id)
        ids = codes = None
        if guid == BUILTIN_EXTRA:
            ids, codes = builtin_options(file_id)
            always = file_id in self.always_builtin
        else:
            always = guid in self.always
        options = BuildOptions(
            material_sets=tuple(self.materials.get((guid, file_id), ())),
            all_features=always,
            fog_modes=self.fog,
            snippet_ids=ids,
            snippet_codes=codes,
            dependency=self.dependency(file_name),
        )
        return build(src, options, self.registry, self.pool)

    def placed(self, files):
        """[(file, path ID, guid, fileID)] of the shaders the layout puts in these files, in order."""
        from placement import first_path_id

        out = []
        for f in files:
            start = first_path_id(f)
            for i, (g, fid, cid) in enumerate(self.layout.objects(f)):
                if cid == 48:
                    out.append((f, start + i, g, fid))
        return out

    def build_all(self, files):
        """{(file, path ID): Shader tree} for every placed shader; built in file order, which is
        the order the keyword registry sees them."""
        placed = self.placed(files)
        for f, pid, g, fid in placed:
            with contextlib.suppress(ParseError, KeyError, OSError):
                self.names.setdefault(self.source(g, fid).name, (f, pid))
        return {(f, pid): self.build(f, g, fid) for f, pid, g, fid in placed}


@cache
def _builtin_materials():
    """fileID -> (shader fileID, keywords) for the editor's built-in materials."""
    import UnityPy

    env = UnityPy.load(str(EDITOR_DATA / 'Resources' / 'unity_builtin_extra'))
    out = {}
    for pid, o in next(iter(env.files.values())).objects.items():
        if o.type.name == 'Material':
            t = o.read_typetree()
            out[pid] = (t['m_Shader']['m_PathID'], tuple(t['m_ShaderKeywords'].split()))
    return out


def build_order(files):
    """The order the player build loads shaders in, which fixes the keyword registry:
    globalgamemanagers.assets (Always Included), then sharedassets0..N, then resources.assets."""

    def key(name):
        if name == 'globalgamemanagers.assets':
            return (0, 0)
        if name.startswith('sharedassets'):
            return (1, int(re.search(r'\d+', name)[0]))
        return (2, 0)

    return sorted(files, key=key)


def imported(build, file_name, guid, file_id, asset):
    """build.Build.IMPORTED[48]: (Shader tree, None for the stock type tree), or None. The first
    call builds every shader of the build at once, in build_order()."""
    made = getattr(build, 'shader_objects', None)
    if made is None:
        shaders = ProjectShaders(build.layout, build.placement)
        made = build.shader_objects = shaders.build_all(build_order(build.files))
        shaders.pool.shutdown()
    where = build.placement.where.get((guid, file_id))
    if where is None or where[0] != file_name or where not in made:
        return None
    return made[where], None
