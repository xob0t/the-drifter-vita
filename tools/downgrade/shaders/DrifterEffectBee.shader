// The game's BeeSprite for Compat EffectSprites: each quad of the page's static mesh (vertex
// (x, y, slot), x and y 0 or 1) draws one bee from the page's arrays, as BeeSprite drew it with
// its SpriteRenderer and property block:
//   _EffectData[3 * slot], [3 * slot + 1], [3 * slot + 2] = A, B, C: A = (position x, y, scale, _WobbleOffset), B = (_WobbleSpeed, 0,
//   sprite index, 0), C = _Color; _EffectRect / _EffectUV as in DrifterEffectSprite.
// BeeSprite moves each vertex by 1.5 / |object matrix row 0| (1.5 / scale) times
// (sin w, sin 0.7678901 w), w = (BeeTimer + _WobbleOffset) * _WobbleSpeed, in object space, and
// outputs the texel times _Color (premultiplied). Unused slots lie far outside the view.
// The arrays' sizes are EffectSprites.Slots * 3 (192) and EffectSprites.MaxSprites (8).
Shader "Hidden/DrifterEffectBee" {
    Properties {
        _MainTex ("Sprite Texture", 2D) = "white" {}
    }
    SubShader {
        Tags { "Queue"="Transparent" "IgnoreProjector"="True" "RenderType"="Transparent" "PreviewType"="Plane" "DisableBatching"="True" }
        Cull Off
        ZWrite Off
        Blend One OneMinusSrcAlpha, One OneMinusSrcAlpha
        Pass {
            CGPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #include "UnityCG.cginc"

            float4 _EffectData[192];
            float4 _EffectRect[8];
            float4 _EffectUV[8];
            float BeeTimer;
            sampler2D _MainTex;

            struct appdata
            {
                float4 vertex : POSITION;
            };

            struct v2f
            {
                float4 vertex : SV_POSITION;
                float4 color : COLOR;
                float2 uv : TEXCOORD0;
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
                float scale = a.z;
                float amplitude = 1.5 / abs(scale);
                float w = (BeeTimer + a.w) * b.x;
                float2 wobble = amplitude * float2(sin(w), sin(w * 0.7678901));
                float2 world = a.xy + scale * (local + wobble);
                o.vertex = mul(UNITY_MATRIX_VP, float4(world, 0.0, 1.0));
                o.uv = lerp(uv.xy, uv.zw, v.vertex.xy);
                o.color = _EffectData[slot * 3 + 2];
                return o;
            }

            float4 frag(v2f i) : SV_Target
            {
                float4 c = tex2D(_MainTex, i.uv) * i.color;
                return float4(c.rgb * c.a, c.a);
            }
            ENDCG
        }
    }
}
