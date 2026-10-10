// Sprites/Default for Compat EffectSprites: each quad of the page's static mesh (vertex
// (x, y, slot), x and y 0 or 1) draws one sprite from the page's arrays, as Sprites/Default drew
// it with its SpriteRenderer:
//   _EffectData[3 * slot], [3 * slot + 1], [3 * slot + 2] = A, B, C: A = (position x, y, matrix column 0), B = (matrix column 1, sprite
//   index, lit), C = colour (the renderer's colour bytes times the material's _Color;
//   for a lit drop its own colour, blended and rounded here);
//   _EffectRect[sprite] = the sprite's vertex rectangle (min xy, max xy), _EffectUV[sprite] = the
//   UVs at its corners.
// A lit drop (RainDrop with a light texture) blends the light texel under its position into its
// colour, as RainDrop.Update did with Texture2D.GetPixel((int)x + width / 2, (int)y + height / 2):
// _EffectLight = (width, height, width >> 1, height >> 1). Unused slots lie far outside the view.
// The arrays' sizes are EffectSprites.Slots * 3 (192) and EffectSprites.MaxSprites (8).
Shader "Hidden/DrifterEffectSprite" {
    Properties {
        _MainTex ("Sprite Texture", 2D) = "white" {}
        _LightTex ("Light", 2D) = "black" {}
    }
    SubShader {
        Tags { "Queue"="Transparent" "IgnoreProjector"="True" "RenderType"="Transparent" "PreviewType"="Plane" "DisableBatching"="True" }
        Cull Off
        Lighting Off
        ZWrite Off
        Blend One OneMinusSrcAlpha
        Pass {
            CGPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #include "UnityCG.cginc"

            float4 _EffectData[192];
            float4 _EffectRect[8];
            float4 _EffectUV[8];
            float4 _EffectLight;
            sampler2D _MainTex;
            sampler2D _LightTex;

            struct appdata
            {
                float4 vertex : POSITION;
            };

            struct v2f
            {
                float4 vertex : SV_POSITION;
                float4 color : COLOR;
                float2 uv : TEXCOORD0;
                float3 light : TEXCOORD1;
            };

            v2f vert(appdata v)
            {
                v2f o;
                int slot = (int)v.vertex.z;
                float4 a = _EffectData[slot * 3];
                float4 b = _EffectData[slot * 3 + 1];
                int sprite = (int)b.z;
                float4 rect = _EffectRect[sprite];
                float4 uv = _EffectUV[sprite];
                float2 local = lerp(rect.xy, rect.zw, v.vertex.xy);
                float2 world = a.xy + a.zw * local.x + b.xy * local.y;
                o.vertex = mul(UNITY_MATRIX_VP, float4(world, 0.0, 1.0));
                o.uv = lerp(uv.xy, uv.zw, v.vertex.xy);
                o.color = _EffectData[slot * 3 + 2];
                o.light.xy = (trunc(a.xy) + _EffectLight.zw + 0.5) / max(_EffectLight.xy, float2(1.0, 1.0));
                o.light.z = b.w;
                return o;
            }

            float4 frag(v2f i) : SV_Target
            {
                float4 color = i.color;
                if (i.light.z > 0.5)
                {
                    float4 light = tex2D(_LightTex, i.light.xy);
                    // As the renderer's colour: vertex bytes (the material's _Color is white).
                    color = floor(saturate(lerp(color, light, light.a)) * 255.0 + 0.5) / 255.0;
                }
                float4 c = tex2D(_MainTex, i.uv) * color;
                c.rgb *= c.a;
                return c;
            }
            ENDCG
        }
    }
}
