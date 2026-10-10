// Uniform-neighbourhood radii of a sprite texture for WaterSprite's skip (DrifterCompat.WaterMasks,
// shader_rules.py noise_split and NOISE_SPLITS). Drawn once per texture with Graphics.Blit into a texture
// of the same size, point sampled, so each output pixel reads its own texel and its neighbours
// at their centres with the texture's own sampler (filter and wrap) as WaterSprite reads them.
// Output: r = d / 255, where d (0-15) is the largest radius such that every texel in the square
// of side 2d + 1 around the texel has its value (see same()), less _WaterErode (0 or 1, at least 0).
// _WaterRect (uv scale xy, offset zw; (1, 1, 0, 0) for the whole texture) maps the output onto a
// region of the texture: a sprite's texels on an atlas too big for a whole-texture mask. Then the
// rows within WATER_MASK_RADIUS of the region's top and bottom edges may be wrong (pass 1 reads
// _WaterRows clamped there); Compat WaterMasks makes the region that much taller than the sprite.
// Pass 0: per texel, the largest h such that the 2h + 1 texels of its row around it match.
// Pass 1: from pass 0's output (_WaterRows), the largest d such that the rows within d above
//         and below match the texel and each has h >= d.
Shader "Hidden/DrifterWaterMask" {
    Properties {
        _MainTex ("Texture", 2D) = "white" {}
        _WaterRows ("Rows", 2D) = "black" {}
        _WaterRect ("Region", Vector) = (1, 1, 0, 0)
        _WaterErode ("Erode", Float) = 0
    }
    CGINCLUDE
    #include "UnityCG.cginc"

    sampler2D _MainTex;
    float4 _MainTex_TexelSize;
    sampler2D _WaterRows;
    float4 _WaterRows_TexelSize;
    float4 _WaterRect;
    float _WaterErode;

    #define WATER_MASK_RADIUS 15 // = WaterMasks.MaskRadius in compat/Noise.cs

    // Texels match when equal, or when both are fully transparent: WaterSprite and PropBurble
    // output exactly (0, 0, 0, 0) for a read with alpha 0 (every term is multiplied by it), which
    // their blend (One OneMinusSrcAlpha) leaves unchanged, whatever its colour. Transparent texels
    // of the console's DXT5 copies keep varying colours. An equivalence relation, so the rows and
    // squares below hold texels that all give the centre texel's output.
    float same(float4 a, float4 b)
    {
        return all(a == b) || (a.a == 0.0 && b.a == 0.0) ? 1.0 : 0.0;
    }

    float4 rows(v2f_img i) : SV_Target
    {
        float2 uv = i.uv * _WaterRect.xy + _WaterRect.zw;
        float4 c = tex2Dlod(_MainTex, float4(uv, 0, 0));
        float h = 0.0, alive = 1.0;
        for (int r = 1; r <= WATER_MASK_RADIUS; r++)
        {
            float2 o = float2(r * _MainTex_TexelSize.x, 0.0);
            alive *= same(tex2Dlod(_MainTex, float4(uv - o, 0, 0)), c) * same(tex2Dlod(_MainTex, float4(uv + o, 0, 0)), c);
            h += alive;
        }
        return float4(h / 255.0, 0.0, 0.0, 1.0);
    }

    float4 square(v2f_img i) : SV_Target
    {
        float2 uv = i.uv * _WaterRect.xy + _WaterRect.zw;
        float4 c = tex2Dlod(_MainTex, float4(uv, 0, 0));
        float d = floor(tex2Dlod(_WaterRows, float4(i.uv, 0, 0)).x * 255.0 + 0.5);
        for (int r = 1; r <= WATER_MASK_RADIUS; r++)
        {
            float2 o = float2(0.0, r * _MainTex_TexelSize.y);
            float2 q = float2(0.0, r * _WaterRows_TexelSize.y);
            float rowsMatch = same(tex2Dlod(_MainTex, float4(uv - o, 0, 0)), c) * same(tex2Dlod(_MainTex, float4(uv + o, 0, 0)), c);
            float below = floor(tex2Dlod(_WaterRows, float4(i.uv - q, 0, 0)).x * 255.0 + 0.5);
            float above = floor(tex2Dlod(_WaterRows, float4(i.uv + q, 0, 0)).x * 255.0 + 0.5);
            // Rows within r - 1 are verified with h >= d; while r <= d, row r either keeps d
            // (at most the smaller h of its two texels) or ends it at r - 1.
            float grown = rowsMatch > 0.5 ? min(d, min(below, above)) : r - 1.0;
            d = r <= d ? grown : d;
        }
        return float4(max(d - _WaterErode, 0.0) / 255.0, 0.0, 0.0, 1.0);
    }
    ENDCG

    SubShader {
        ZTest Always Cull Off ZWrite Off
        Pass {
            CGPROGRAM
            #pragma vertex vert_img
            #pragma fragment rows
            #pragma target 3.0
            ENDCG
        }
        Pass {
            CGPROGRAM
            #pragma vertex vert_img
            #pragma fragment square
            #pragma target 3.0
            ENDCG
        }
    }
}
