"""Quick checks of the pipeline's pure rules and of the shared tools; no Unity, game or console needed.

    uv run python -m unittest discover tools/downgrade/tests

The full check of a change to the rules is still a project/build and a parity run (docs/parity.md,
"Checking a change"); these catch slips in the text rewrites and helpers in a second.
"""

import contextlib
import io
import json
import os
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import asset_rules  # noqa: E402
import common  # noqa: E402
import fastnoise  # noqa: E402
import heartbeats  # noqa: E402
import lock  # noqa: E402
import pipeline  # noqa: E402
import shader_rules  # noqa: E402


def wav(seconds, channels=1, rate=8000):
    data = bytes(rate * 2 * channels * seconds)
    return (
        b'RIFF'
        + struct.pack('<I', 36 + len(data))
        + b'WAVEfmt '
        + struct.pack('<IHHIIHH', 16, 1, channels, rate, rate * 2 * channels, 2 * channels, 16)
        + b'data'
        + struct.pack('<I', len(data))
        + data
    )


class ShaderRules(unittest.TestCase):
    def test_mask_logic(self):
        self.assertEqual(shader_rules.mask_logic('x = uint4(a) & uint4(b);'), 'x = (a) * (b);')
        self.assertEqual(shader_rules.mask_logic('x = uint2(a.xy) | uint2(b.xy);'), 'x = max(a.xy, b.xy);')

    def test_strip_builtin_declarations(self):
        text = 'float4 _Time; // 0\nfloat4 _Mine;\nfloat4x4 unity_ObjectToWorld[2];\n'
        self.assertEqual(
            shader_rules.strip_builtin_declarations(text, {'_Time', 'unity_ObjectToWorld'}), 'float4 _Mine;\n'
        )

    def test_noise_constants_match_the_texture(self):
        self.assertAlmostEqual(float(shader_rules.NOISE_SKEW), fastnoise.F2, places=7)
        scale, offset = (float(v) for v in shader_rules.NOISE_DECODE)
        self.assertAlmostEqual(scale, 255 / 127, places=6)  # texel v (0-1) holds (255 v - 128) / 127
        self.assertAlmostEqual(offset, 128 / 127, places=6)

    def test_noise_fast(self):
        text = shader_rules.noise_fast(noise_shader(), 'WaterSprite')
        self.assertEqual(text.count('#pragma multi_compile __ DRIFTER_FAST_NOISE'), 1)
        for line in ('float2 drifterUV : TEXCOORD3;', 'sampler2D _DrifterNoiseTex;', 'o.drifterUV = drifterUV;'):
            self.assertEqual(text.count(line), 4, line)
        # The uniforms the vertex programs read are declared there, not in the fragment programs.
        self.assertEqual(text.count('float _BlurbleSpeed;'), 4)
        self.assertNotIn('float _BlurbleSpeed; //', text)
        self.assertIn(f'* {1.0 / fastnoise.PERIOD!r} + frac(', text)
        self.assertEqual(text.count(f'.w) * {" - ".join(shader_rules.NOISE_DECODE)};'), 4)
        # The original code stays behind #else.
        self.assertEqual(text.count('tmp0.xy = tmp0.xz * _Timer;'), 4)
        self.assertEqual(text.count('#else'), 4)
        with self.assertRaises(SystemExit):
            shader_rules.noise_fast(noise_shader(variants=3), 'WaterSprite')

    def test_noise_split(self):
        text = shader_rules.noise_split(noise_shader(), 'WaterSprite')
        self.assertIn('Shader "Hidden/DrifterWaterSpriteSplit"', text)
        self.assertIn('_WaterMask ("Noise skip mask", 2D) = "black" {}', text)
        self.assertEqual(text.count('Pass {'), 2)
        self.assertEqual(text.count('tmp0 = tex2D(_MainTex, inp.centre);'), 4)  # pass 0 reads the centre
        self.assertEqual(text.count(') discard;'), 8)  # each pass discards the other's pixels
        self.assertEqual(text.count('o.maskPos = o.centre * _WaterMaskRect.xy + _WaterMaskRect.zw;'), 8)


def noise_shader(variants=4):
    """A stand-in for a decompiled noise shader (WaterSprite): one pass, one CGPROGRAM of `variants`
    vertex and fragment program pairs with the lines the noise rules look for."""
    lines = [
        'Shader "WaterSprite" {',
        '    Properties {',
        '        _MainTex ("Texture", 2D) = "white" {}',
        '        _Timer ("Timer", Float) = 0',
        '    }',
        '    SubShader {',
        '        Pass {',
        '            CGPROGRAM',
        '            #pragma vertex vert',
        '            #pragma fragment frag',
    ]
    for _ in range(variants):
        lines += [
            '            struct v2f',
            '            {',
            '                float4 position : SV_POSITION0;',
            '                float2 texcoord : TEXCOORD;',
            '            };',
            '            v2f vert(appdata v)',
            '            {',
            '                v2f o;',
            '                o.texcoord.xy = v.texcoord.xy;',
            '                return o;',
            '            }',
            '            struct fout',
            '            {',
            '                float4 sv_target : SV_Target0;',
            '            };',
            *(
                f'            float{4 if name == "_MainTex_ST" else ""} {name}; // {i}'
                for i, name in enumerate(
                    ['_MainTex_ST', '_BlurbleScaleX', '_BlurbleScaleY', '_BlurbleSpeed', '_Timer', '_OffsetDist']
                )
            ),
            '            sampler2D _MainTex; // 0',
            '            fout frag(v2f inp)',
            '            {',
            '                fout o;',
            '                float4 tmp0;',
            '                tmp0.xz = float2(0.0, 1.0);',
            '                tmp0.xy = tmp0.xz * _Timer;',
            '                tmp0 = tex2D(_MainTex, tmp0.xy);',
            '                o.sv_target = tmp0;',
            '                return o;',
            '            }',
        ]
    return '\n'.join([*lines, '            ENDCG', '        }', '    }', '}'])


def fragment(*body):
    return (
        'Shader "X" {\n    SubShader {\n        Pass {\n            CGPROGRAM\n'
        '            fout frag(v2f inp)\n            {\n'
        + ''.join(f'                {line}\n' for line in body)
        + '            }\n            ENDCG\n        }\n    }\n}\n'
    )


# Stand-ins for the decompiled CRT programs, with the lines crt_shader.py looks for and
# placeholder arithmetic in between.
CRT_FIRST = fragment(
    'fout o;',
    'float4 tmp0;',
    'float4 tmp1;',
    'float4 tmp2;',
    'tmp0 = tex2D(_MainTex, inp.texcoord.xy);',
    'tmp1 = tex2D(_BlurTex, inp.texcoord1.xy);',
    'tmp0.xyz = tmp1.xyz * bleedStr.xxx + tmp0.xyz;',
    'tmp2.xy = inp.texcoord.xy / float2(pixelSizeX.x, pixelSizeY.x);',
    'tmp0.xyz = tmp1.xyz * monoNoiseStr.xxx + tmp0.xyz;',
    'tmp1.xyz = tmp2.xxx * rgbMaskSep;',
    'tmp0.xyz = tmp1.xyz * rgbMaskStr.xxx + tmp0.xyz;',
    'tmp0.x = inp.texcoord.y;',
    'tmp1 = tmp0 * colorMat._m00_m10_m20_m30;',
    'o.sv_target = tmp1;',
    'return o;',
)
CRT_FINAL = fragment(
    'fout o;',
    'float4 tmp0;',
    'float4 tmp1;',
    'float4 tmp2;',
    'tmp0.xy = inp.texcoord.xy * crtBendX;',
    'tmp0.zw = tmp0.xy < float2(0.0, 0.0);',
    'if (tmp0.z) {',
    '    o.sv_target = float4(0.0, 0.0, 0.0, 1.0);',
    '    return o;',
    '} else {',
    '    tmp1 = tex2D(_MainTex, tmp0.xy);',
    '    if (!(maskMode)) {',
    '        tmp2.xyz = 1.0;',
    '    } else {',
    '        tmp2.xyz = 0.0;',
    '    }',
    '    tmp2.xyz = tmp1.xyz * tmp2.xyz;',
    '    tmp0.x = dot(tmp0.xy, tmp0.xy);',
    '    tmp0.x = saturate(tmp0.x * vignetteSize);',
    '    o.sv_target = tmp1 * tmp0.x;',
    '    return o;',
    '}',
    'return o;',
)


class CrtShader(unittest.TestCase):
    def test_parts_from_the_programs(self):
        import crt_shader

        text = crt_shader.single_pass(CRT_FIRST, CRT_FINAL)
        self.assertIsNone(re.search(r'^\s*// @lift \w+$', text, re.M))
        for signature in (
            'float2 bend(float2 texcoord)',
            'float mask(float2 texcoord)',
            'float vignette(float2 bent)',
            'float3 columnMask(float texel)',
            'float3 levels(float3 color)',
            'float4 postprocess(float2 uv, float2 pixel)',
            'float4 finalPostprocess(float2 texcoord)',
        ):
            self.assertEqual(text.count(f'\n    {signature}\n    {{\n'), 1, signature)
        self.assertIn('tmp1 = postprocess(tmp0.xy, inp.texcoord.xy);', text)
        self.assertIn('tmp1 = tex2D(_MainTex, inp.texcoord1.xy);', text)  # _BlurTex is the source
        self.assertIn('tmp1.xyz = tmp2.xxx * unitSep;', text)  # columnMask at unit strength
        self.assertIn('tmp0.xyz -= rgbMaskOffset(uv.x);', text)
        self.assertNotIn('tmp1.xyz = tmp2.xxx * rgbMaskSep;', text)  # postprocess's own mask is replaced
        self.assertEqual(text.count('inp.texcoord = pixel;'), 1)

    def test_missing_line_stops(self):
        import crt_shader

        with self.assertRaises(SystemExit):
            crt_shader.single_pass(CRT_FIRST.replace('monoNoiseStr', 'other'), CRT_FINAL)


class AssetRules(unittest.TestCase):
    def test_strip_asset_references(self):
        document = (
            '  m_Script: {fileID: 11500000, guid: 0123456789abcdef0123456789abcdef, type: 3}\n'
            '  m_sprite: {fileID: 21300000, guid: 11111111111111111111111111111111, type: 3}\n'
            '  m_list:\n'
            '  - {fileID: 21300000, guid: 22222222222222222222222222222222, type: 3}\n'
            '  m_value: 5'
        )
        self.assertEqual(
            asset_rules.strip_asset_references(document),
            '  m_Script: {fileID: 11500000, guid: 0123456789abcdef0123456789abcdef, type: 3}\n'
            '  m_sprite: {fileID: 0}\n'
            '  m_list: []\n'
            '  m_value: 5',
        )

    def test_powerquest_differences(self):
        key = ('114', '11500000', True)
        title = {
            key: [
                '  m_speed: 45',
                '  m_range: {x: 45, y: 18}',
                '  m_items:',
                '  - {fileID: 1, guid: ' + 'a' * 32 + ', type: 2}',
            ]
        }
        plain = {key: ['  m_speed: 50', '  m_range: {x: 50, y: 15}', *title[key][2:]]}
        self.assertEqual(asset_rules.powerquest_differences(title, plain), [])
        reference = {key: [*title[key][:3], '  - {fileID: 1, guid: ' + 'b' * 32 + ', type: 2}']}
        self.assertTrue(asset_rules.powerquest_differences(title, reference))

    def test_audio_seconds_wav(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'clip.wav'
            path.write_bytes(wav(2))
            self.assertAlmostEqual(asset_rules.audio_seconds(path), 2)

    def test_link_xml(self):
        text = asset_rules.link_xml([Path('Recovered.Drifter.dll')], 'vita')
        self.assertIn('<assembly fullname="Recovered.Drifter" preserve="all"/>', text)
        self.assertIn('UnityEngine.PSVita.Utility/PowerTickType', text)
        self.assertNotIn('PSVita', asset_rules.link_xml([], 'windows'))

    def test_bypass_send_reverbs(self):
        def effect(file_id, name, extra=''):
            return f'--- !u!244 &{file_id}\nAudioMixerEffectController:\n  m_EffectName: {name}\n  m_Bypass: 0\n{extra}'

        def group(file_id, name, effects):
            listed = ''.join(f'  - {{fileID: {e}}}\n' for e in effects)
            return f'--- !u!243 &{file_id}\nAudioMixerGroupController:\n  m_Name: {name}\n  m_Effects:\n{listed}'

        mixer = ''.join(
            [
                effect(100, 'Receive'),
                effect(101, 'SFX Reverb'),
                group(200, 'RoomVerb', [100, 101]),
                effect(102, 'Send', '  m_SendTarget: {fileID: 100}\n'),
                effect(103, 'SFX Reverb'),
                group(201, 'Voice', [103]),
            ]
        )
        text, bypassed = asset_rules.bypass_send_reverbs(mixer)
        self.assertEqual(bypassed, ["RoomVerb's reverb"])
        bypass = {
            document[1]: document[2]
            for document in re.finditer(r'--- !u!\d+ &(\d+)\nAudioMixerEffectController:\n.*\n  m_Bypass: (\d)', text)
        }
        self.assertEqual(bypass, {'100': '0', '101': '1', '102': '1', '103': '0'})

    def test_max_texture_size(self):
        self.assertEqual(
            [asset_rules.max_texture_size(n) for n in (0, 2048, 2049, 4096, 5000)], [2048] * 2 + [4096] * 2 + [8192]
        )

    def test_pixel_camera_texture_is_required(self):
        with tempfile.TemporaryDirectory() as folder:
            exported = Path(folder)
            path = exported / 'Assets' / 'RenderTexture' / 'Other.renderTexture'
            path.parent.mkdir(parents=True)
            path.write_text('  m_ColorFormat: 8\n  m_SRGB: 0\n  m_Width: 1920\n  m_Height: 1080\n', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(len(asset_rules.render_texture_overlay(exported, 'windows')), 1)
                with self.assertRaises(SystemExit):
                    asset_rules.render_texture_overlay(exported, 'vita')
                path.rename(path.with_name('PixelCameraTex.renderTexture'))
                text = asset_rules.render_texture_overlay(exported, 'vita')[
                    Path('Assets/RenderTexture/PixelCameraTex.renderTexture')
                ].decode()
            self.assertIn('  m_Width: 640\n  m_Height: 360\n', text)


class SharedNames(unittest.TestCase):
    """Names the pipeline's files and the C# compat code both spell out."""

    @staticmethod
    def compat(name):
        return (HERE / 'compat' / name).read_text(encoding='utf-8')

    def test_glyphs(self):
        made = {p.stem for p in asset_rules.glyph_overlay('vita', False) if p.suffix == '.png'}
        listed = re.search(r'glyphNames = \{([^}]*)\}', self.compat('Platform.cs'))[1]
        self.assertEqual(set(re.findall(r'"(\w+)"', listed)), made)

    def test_shaders(self):
        provided = {split.shader for split in shader_rules.NOISE_SPLITS.values()}
        for path in [*(HERE / 'shaders').glob('*.shader'), *(HERE / 'shaders').glob('*.shader.in')]:
            provided.add(re.search(r'^\s*Shader\s+"([^"]+)"', path.read_text(encoding='utf-8'), re.M)[1])
        found = set()
        for path in (HERE / 'compat').glob('*.cs'):
            found.update(re.findall(r'Shader\.Find\("([^"]+)"\)', path.read_text(encoding='utf-8')))
        self.assertTrue(found)
        self.assertEqual(found - provided, set())
        pairs = re.findall(r'new\[\] \{ "(\w+)", "([^"]+)" \}', self.compat('Noise.cs'))
        self.assertEqual(dict(pairs), {name: split.shader for name, split in shader_rules.NOISE_SPLITS.items()})

    def test_noise_texture(self):
        noise = self.compat('Noise.cs')
        self.assertIn(f'Resources.Load<TextAsset>("{fastnoise.RESOURCE_NAME}")', noise)
        self.assertIn('Keyword = "DRIFTER_FAST_NOISE"', noise)
        self.assertEqual(shader_rules.FAST_IF.split(), ['#if', 'DRIFTER_FAST_NOISE'])
        self.assertIn('Shader.SetGlobalTexture("_DrifterNoiseTex"', noise)


class Pipeline(unittest.TestCase):
    def test_split_sources_are_all_listed(self):
        for sources in (pipeline.COMPAT_SOURCES, pipeline.DIAG_SOURCES, pipeline.REWRITER_SOURCES):
            self.assertEqual([p.name for p in sources if not p.exists()], [])
        pipeline.check_parts()  # exits if a part is unlisted

    def test_tools_use_existing_names(self):
        # Every pipeline.<name> the tools (and the queue script) use exists.
        for path in [*HERE.glob('*.py'), *HERE.glob('*.sh')]:
            for name in set(re.findall(r'\bpipeline\.([A-Za-z_]\w*)', path.read_text(encoding='utf-8'))) - {'py'}:
                self.assertTrue(hasattr(pipeline, name), f'{path.name}: pipeline.{name}')


class Heartbeats(unittest.TestCase):
    # The first heartbeat as an older probe on a Windows player wrote it (decimal commas).
    LOG = (
        '[10.0] sceneLoaded SceneRoomLabKitchen\n'
        '[12.0] heartbeat frame 100 fps 55.0 renderMs 7.5 otherMs 9.0 scene SceneRoomLabKitchen '
        'cams Pixel:1.0+0.5 markers '
        'scriptsMs 4.0 A.Update:2.0/1 loop Update.ScriptRunBehaviourUpdate:3.0 gcs 1 gcAllocB 200\n'
        '[17.0] heartbeat frame 400 fps 60.0 renderMs 7.0 otherMs 8.0 scene SceneRoomLabKitchen '
        'markers gcs 0 gcAllocB 100\n'
        '[22.0] heartbeat frame 700 fps 58.0 renderMs 7.0 otherMs 8.0 scene SceneRoomLabKitchen '
        'markers gcs 0 gcAllocB 100\n'
    )

    def test_parse(self):
        beats = heartbeats.heartbeats(self.LOG)
        self.assertEqual(len(beats), 3)
        self.assertEqual(beats[0]['fps'], 55.0)
        self.assertEqual(beats[0]['cams'], {'Pixel': (1.0, 0.5)})
        self.assertEqual(beats[0]['scripts'], {'A.Update': 2.0})
        self.assertEqual(beats[0]['loop'], {'Update.ScriptRunBehaviourUpdate': 3.0})

    def test_sections_end_at_the_next(self):
        # allocB (-diag-alloc-scripts) comes between loop and gcs; a section that failed has no entries.
        line = (
            '[5.00] heartbeat frame 9 fps 30.0 scene SceneRoomBridge managedMB 20.0 freeLPDDR 120.5 '
            'cams Pixel:1.0+0.5 markers error NullReferenceException scriptsMs 2.0 A.Update:1.50/1 '
            'loop Update.ScriptRunBehaviourUpdate:2.00 allocB probe.frame:120 A.Update:40 gcs 0 gcAllocB 160'
        )
        beat = heartbeats.heartbeats(line)[0]
        self.assertEqual(beat['loop'], {'Update.ScriptRunBehaviourUpdate': 2.0})
        self.assertEqual(beat['cams'], {'Pixel': (1.0, 0.5)})
        self.assertEqual((beat['scriptsMs'], beat['scripts']), (2.0, {'A.Update': 1.5}))
        self.assertEqual((beat['freeLPDDR'], beat['gcs'], beat['gcAllocB']), (120.5, 0.0, 160.0))
        self.assertNotIn(' cams', heartbeats.without_cams(line))
        self.assertIn(' markers error', heartbeats.without_cams(line))
        failed = heartbeats.heartbeats('[5.00] heartbeat frame 9 fps 30.0 scene S gcs error InvalidCastException')[0]
        self.assertNotIn('gcs', failed)

    def test_nothing_to_average(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'log.txt').write_text('[1.00] scene SceneRoomTitle\n')
            self.assertIn('no heartbeats in log.txt', heartbeats.summary(Path(folder)))
            (Path(folder) / 'log.txt').write_text(self.LOG.splitlines(keepends=True)[1])
            self.assertIn('no steady heartbeats in SceneRoomLabKitchen', heartbeats.summary(Path(folder)))

    def test_pick_and_summary(self):
        text = self.LOG
        picked = heartbeats.pick(heartbeats.heartbeats(text), heartbeats.scene_loads(text), None)
        self.assertEqual([b['t'] for b in picked], [17.0, 22.0])
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'log.txt').write_text(self.LOG)
            (Path(folder) / 'events.txt').write_text(
                '[1] restore-returned x after 900ms\n[2] goto Drowning ok\n[3] room-ready +2500ms\n'
            )
            line = heartbeats.summary(Path(folder))
        self.assertIn('fps 59.0', line)
        self.assertIn('restore 900ms', line)
        self.assertIn('rooms Drowning:2500', line)


class Locks(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patch = mock.patch.object(lock, 'WORK', Path(folder.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_take_release(self):
        self.assertEqual(lock.take('vita-device', 'a: test', 0), 0)
        self.assertEqual(lock.take('vita-device', 'b: test', 0), 1)
        self.assertEqual(lock.release('vita-device', 'b'), 1)
        self.assertEqual(lock.release('vita-device', 'a'), 0)
        self.assertFalse((lock.WORK / 'vita-device.lock').exists())

    def test_one_pipeline_at_a_time(self):
        self.assertEqual(lock.take('vita-build', 'a: test', 0), 0)
        self.assertEqual(lock.take('pc-build', 'b: test', 0), 1)
        self.assertFalse((lock.WORK / 'pc-build.lock').exists())
        self.assertEqual(lock.take('vita-device', 'b: test', 0), 0)

    def test_release_needs_the_whole_name(self):
        self.assertEqual(lock.owner('coordinator: vita steps (x) 16:09'), 'coordinator')
        self.assertEqual(lock.owner('USER 9:05'), 'USER')
        self.assertEqual(lock.take('vita-device', 'ab: test', 0), 0)
        self.assertEqual(lock.release('vita-device', 'a'), 1)
        self.assertEqual(lock.release('vita-device', ''), 1)
        self.assertTrue((lock.WORK / 'vita-device.lock').exists())
        self.assertEqual(lock.release('vita-device', 'ab: done'), 0)
        self.assertFalse((lock.WORK / 'vita-device.lock').exists())

    def test_run_releases(self):
        code = lock.run('pc-build', 'a: test', 0, [sys.executable, '-c', 'import sys; sys.exit(3)'])
        self.assertEqual(code, 3)
        self.assertFalse((lock.WORK / 'pc-build.lock').exists())

    def test_command_line(self):
        # --wait may come anywhere, also before the lock's name.
        self.assertEqual(lock.main(['take', '--wait', '0', 'vita-device', 'a: test']), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lock.main(['take', 'vita-device', 'b: test', '--wait', '0']), 1)
        self.assertEqual(lock.main(['release', 'vita-device', 'a']), 0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            lock.main(['take', 'vita-device'])

    def test_person(self):
        self.assertTrue(lock.is_person('user: at the console 9:05'))
        self.assertTrue(lock.is_person('USER 9:05'))
        self.assertFalse(lock.is_person('coordinator: vita steps (x) 16:09'))
        self.assertFalse(lock.is_person('username: x'))


class Fingerprint(unittest.TestCase):
    def test_mvid(self):
        import fingerprint

        dll = HERE.parents[1] / 'tools' / 'external' / 'downgrade-bin' / 'DrifterCompat.dll'
        if not dll.exists():
            self.skipTest('no compiled DrifterCompat.dll')
        data = dll.read_bytes()
        at = fingerprint.mvid_offset(data)
        self.assertIsNotNone(at)
        self.assertEqual(data[at + 7] >> 4, 4)  # a version 4 (random) GUID, as compilers make
        self.assertIn('il', fingerprint.describe(dll))

    def test_compare(self):
        import fingerprint

        before = {'a.dll': {'sha256': '1', 'il': 'x'}, 'b.txt': {'sha256': '2'}}
        stamps = {'a.dll': {'sha256': '3', 'il': 'x'}, 'b.txt': {'sha256': '2'}}
        changed = {'a.dll': {'sha256': '3', 'il': 'y'}, 'b.txt': {'sha256': '2'}}
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for i, manifest in enumerate((before, stamps, changed, {'a.dll': before['a.dll']})):
                paths.append(Path(folder) / f'{i}.json')
                paths[-1].write_text(json.dumps(manifest))
            with contextlib.redirect_stdout(io.StringIO()):
                results = [fingerprint.compare(paths[0], other) for other in paths[1:]]
        self.assertEqual(results, [0, 1, 1])


class ImportJobs(unittest.TestCase):
    def test_cost_scales_with_size(self):
        import import_jobs

        with tempfile.TemporaryDirectory() as folder:
            small, large, wav = Path(folder) / 'a.ogg', Path(folder) / 'b.ogg', Path(folder) / 'c.wav'
            small.write_bytes(bytes(10000))
            large.write_bytes(bytes(110000))
            wav.write_bytes(bytes(1000000))
            self.assertAlmostEqual(import_jobs.audio_cost(large) - import_jobs.audio_cost(small), 100000 * 3.06e-6)
            self.assertAlmostEqual(import_jobs.audio_cost(wav), import_jobs.audio_cost(large))

    def test_split_balances(self):
        import import_jobs

        items = [(Path(f'{i}.ogg'), cost) for i, cost in enumerate([9, 7, 5, 4, 3, 2, 2, 1, 1])]
        buckets = import_jobs.split(items, 3)
        self.assertLessEqual(max(t for t, _ in buckets) - min(t for t, _ in buckets), 1)
        self.assertEqual(sorted(p for _, paths in buckets for p in paths), sorted(p for p, _ in items))
        self.assertEqual(len(import_jobs.split(items[:2], 8)), 2)  # no empty jobs

    def test_pending_and_job_project(self):
        import import_jobs

        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / 'project'
            for name in ('ProjectSettings', 'Packages', 'Assets/Sound', 'Library/metadata/aa', 'Library/metadata/bb'):
                (project / name).mkdir(parents=True)
            (project / 'ProjectSettings' / 'ProjectSettings.asset').write_text('settings')
            (project / 'Packages' / 'manifest.json').write_text('{}')
            (project / 'Assets' / 'Sound.meta').write_text('guid: ' + 'f' * 32)
            for guid, name in (('a' * 32, 'old'), ('b' * 32, 'new'), ('c' * 32, 'none')):
                (project / 'Assets' / 'Sound' / f'{name}.wav').write_bytes(wav(1))
                (project / 'Assets' / 'Sound' / f'{name}.wav.meta').write_text(f'guid: {guid}\nAudioImporter:\n')
            (project / 'Assets' / 'Sound' / 'icon.png').write_bytes(b'png')
            (project / 'Assets' / 'Sound' / 'icon.png.meta').write_text('guid: ' + 'd' * 32)
            imported_a, imported_b = (
                project / 'Library/metadata/aa' / ('a' * 32),
                project / 'Library/metadata/bb' / ('b' * 32),
            )
            imported_a.write_bytes(b'')
            imported_b.write_bytes(b'')
            os.utime(imported_b, (0, 0))  # imported before the clip changed
            pending = sorted(str(path.as_posix()) for path, _ in import_jobs.pending_audio(project))
            self.assertEqual(pending, ['Assets/Sound/new.wav', 'Assets/Sound/none.wav'])
            # After the editor saved its database, only clips changed since then are looked at.
            database = project / 'Library' / 'assetDatabase3'
            database.write_bytes(b'')
            self.assertEqual(import_jobs.pending_audio(project), [])
            os.utime(database, (0, 0))
            self.assertEqual(len(import_jobs.pending_audio(project)), 2)
            job = Path(folder) / 'job'
            import_jobs.job_project(project, job, [Path('Assets/Sound/new.wav')])
            self.assertEqual(
                sorted(p.relative_to(job).as_posix() for p in job.rglob('*') if p.is_file()),
                [
                    'Assets/Sound.meta',
                    'Assets/Sound/new.wav',
                    'Assets/Sound/new.wav.meta',
                    'Packages/manifest.json',
                    'ProjectSettings/ProjectSettings.asset',
                ],
            )


class RipStamp(unittest.TestCase):
    def test_stamp_follows_game_files_and_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            game = Path(folder) / 'The Drifter'
            data = game / 'TheDrifter_Data'
            data.mkdir(parents=True)
            (game / 'TheDrifter.exe').write_bytes(b'exe')
            (data / 'data.unity3d').write_bytes(b'1')
            first = pipeline.rip_stamp(game, '2018.2.19f1')
            self.assertEqual(first, pipeline.rip_stamp(game, '2018.2.19f1'))
            self.assertNotEqual(first, pipeline.rip_stamp(game, '2018.2.21f1'))
            (data / 'data.unity3d').write_bytes(b'22')
            self.assertNotEqual(first, pipeline.rip_stamp(game, '2018.2.19f1'))
            out = Path(folder) / 'rip'
            out.mkdir()
            self.assertFalse(pipeline.rip_is_current(game, out, '2018.2.19f1'))
            (out / pipeline.RIP_STAMP).write_text(pipeline.rip_stamp(game, '2018.2.19f1') + '\n')
            self.assertTrue(pipeline.rip_is_current(game, out, '2018.2.19f1'))

    def test_game_data_names_a_missing_folder(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(SystemExit) as stop:
            common.game_data(Path(folder))
        self.assertIn('_Data', str(stop.exception))


class Tools(unittest.TestCase):
    def test_room_flags(self):
        import sweep

        self.assertNotIn('-diag-goto-room', sweep.room_flags(2))
        flags = sweep.room_flags(3, 'Drowning')
        self.assertIn('3@1', flags)
        self.assertEqual(flags[flags.index('-diag-goto-room') + 1], 'Drowning')

    def test_diag_command(self):
        import diag

        command = diag.command('room-A', 'sweep', None, 8, ['-diag-skip-intro'], fresh=True)
        self.assertEqual(command[3:], ['room-A', '--tag', 'sweep', '--fresh', '--seconds', '8', '-diag-skip-intro'])

    def test_mean_difference(self):
        import capture
        from PIL import Image

        black, grey = Image.new('RGB', (4, 2)), Image.new('RGB', (8, 4), (10, 20, 30))
        self.assertEqual(capture.mean_difference(black, grey), 20.0)

    def test_apply_patch(self):
        patch = (
            'A comment.\n\n'
            'diff --git a/src/a.cs b/src/a.cs\n--- a/src/a.cs\n+++ b/src/a.cs\n'
            '@@ -2,3 +2,3 @@\n one\n-two\n+TWO\n three\n'
            '@@ -6,2 +6,3 @@\n five\n+five and a half\n six\n'
        )
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'src').mkdir()
            source = folder / 'src' / 'a.cs'
            source.write_bytes(b'\xef\xbb\xbfzero\r\none\r\ntwo\r\nthree\r\nfour\r\nfive\r\nsix\r\n')
            (folder / 'p.patch').write_text(patch)
            common.apply_patch(folder / 'p.patch', folder)
            self.assertEqual(
                source.read_bytes(),
                b'\xef\xbb\xbfzero\r\none\r\nTWO\r\nthree\r\nfour\r\nfive\r\nfive and a half\r\nsix\r\n',
            )
            with self.assertRaises(SystemExit):  # 'two' is gone
                common.apply_patch(folder / 'p.patch', folder)


if __name__ == '__main__':
    os.chdir(HERE)
    unittest.main()
