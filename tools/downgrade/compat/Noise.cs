using System;
using System.Collections.Generic;
using UnityEngine;
using Object = UnityEngine.Object;

// Noise shaders: the fast noise texture and the exact skip (water masks).
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // The game's noise shaders (WaterSprite, PropBurble, PropLightMasked) evaluate two simplex
    // noises per pixel (151-168 cycles on the PS Vita). shader_rules.py noise_fast gives each a second
    // path behind the global keyword DRIFTER_FAST_NOISE that reads the same kind of noise from a
    // precomputed texture instead (Resources/DrifterNoise.bytes from fastnoise.py: 512x512 8-bit
    // texels, periodic, read with bilinear filtering and wrapping) at coordinates the vertex
    // programs compute: 13 cycles a pixel for WaterSprite (psp2shaderperf). The noise has the
    // game's kernel, gradients, scale, direction and speed but not its values, so the ripples
    // read the same without matching pixel for pixel. On by default on the PS Vita, where it
    // replaces the exact skip (WaterMasks); -compat-exact-noise keeps the game's noise there (and
    // WaterMasks). On Windows with -compat-fast-noise. Configured when a scene loads and from
    // WaterMasks (the probe's flags arrive after the start); the probe's
    // -diag-check-global "<camera>:keyword:DRIFTER_FAST_NOISE=0" renders a frame both ways.
    public static class FastNoise
    {
        public const string Keyword = "DRIFTER_FAST_NOISE";
        static string[] configuredFor;
        static bool enabled;
        static Texture2D texture;

        public static bool Enabled()
        {
            Configure();
            return enabled;
        }

        // Main thread only (it loads and sets engine state).
        public static void Configure()
        {
            if (!Platform.NewArgs(ref configuredFor)) return;
            enabled = Platform.VitaDefault("-compat-exact-noise", "-compat-fast-noise");
            if (enabled && texture == null)
            {
                TextAsset data = Resources.Load<TextAsset>("DrifterNoise");
                byte[] bytes = data != null ? data.bytes : null;
                int size = bytes != null ? (int)Math.Round(Math.Sqrt(bytes.Length)) : 0;
                if (size == 0 || size * size != bytes.Length)
                {
                    Debug.LogWarning("FastNoise: no noise texture, keeping the game's noise");
                    enabled = false;
                }
                else
                {
                    bool alpha8 = SystemInfo.SupportsTextureFormat(TextureFormat.Alpha8);
                    texture = new Texture2D(size, size, alpha8 ? TextureFormat.Alpha8 : TextureFormat.RGBA32, false, true);
                    if (alpha8) texture.LoadRawTextureData(bytes);
                    else
                    {
                        var texels = new Color32[bytes.Length];
                        for (int i = 0; i < bytes.Length; i++) texels[i] = new Color32(bytes[i], bytes[i], bytes[i], bytes[i]);
                        texture.SetPixels32(texels);
                    }
                    texture.name = "DrifterNoise";
                    texture.filterMode = FilterMode.Bilinear;
                    texture.wrapMode = TextureWrapMode.Repeat;
                    texture.anisoLevel = 0;
                    texture.hideFlags = HideFlags.HideAndDontSave;
                    texture.Apply(false, true);
                }
                if (data != null) Resources.UnloadAsset(data);
            }
            if (enabled)
            {
                Shader.SetGlobalTexture("_DrifterNoiseTex", texture);
                Shader.EnableKeyword(Keyword);
            }
            else Shader.DisableKeyword(Keyword);
        }

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Register()
        {
            UnityEngine.SceneManagement.SceneManager.sceneLoaded += (scene, mode) => { if (MainThread.Current) Configure(); };
        }
    }

    // Two game shaders move each pixel's texture read by simplex noise: WaterSprite (151 cycles a
    // pixel on the PS Vita: 33 ms of GPU time a frame for Drowning's surface sprite, which is on
    // the HighRes layer and drawn by QuestCamera at screen resolution) and PropBurble (156
    // cycles; Crypt's lighter shadow). The read moves less than a bound (shader_rules.py NOISE_SPLITS);
    // where every texel within that reach has the same value the read cannot change.
    // Hidden/DrifterWaterSpriteSplit and Hidden/DrifterPropBurbleSplit (shader_rules.py noise_split)
    // are those shaders in two passes that skip the noise there, given a mask (_WaterMask)
    // holding per texel the radius of the uniform square around it, and draw every pixel once
    // with the original's value. The masks are drawn on the GPU from the texture as the shaders
    // sample it (two passes of Hidden/DrifterWaterMask), once per texture, for point-filtered
    // textures without mipmaps. A renderer whose material has one of the two shaders gets a copy
    // with the split shader, and the mask in its property block (black, where every pixel takes
    // the noise, if the texture has none). A material that already has a split shader (an
    // instance the game made from a copy) is kept. Called at the end of
    // WaterSprite.LateUpdate and ShaderAnimator.LateUpdate, the scripts that animate those
    // materials. On the PS Vita with -compat-exact-noise (FastNoise replaces it by default;
    // -compat-no-water-mask turns it off), or on Windows with -compat-water-mask.
    public static class WaterMasks
    {
        // The largest mask drawn (a 1024x1024 texture's, 1 MB at R8): room atlases (2048x1024 and
        // up) get masks of their sprites' regions instead.
        const int MaxTexels = 1024 * 1024;

        static string[] configuredFor;
        static bool enabled;
        static Texture maskTest;
        static Material material;
        static readonly Dictionary<string, Shader> splits = new Dictionary<string, Shader>();
        static int maskId, rowsId, maskRectId, regionId, erodeId;
        static RenderTextureFormat format;
        static MaterialPropertyBlock block;
        static readonly Dictionary<int, RenderTexture> masks = new Dictionary<int, RenderTexture>();
        // Sprites on textures too big for a whole-texture mask (room atlases: EscReactorWater's and
        // LabReactor's water, 2048x1024 and 2048x2048 pages): a mask of the sprite's texels only,
        // by sprite instance id, with the split shader's _WaterMaskRect and the mask draw's region.
        static readonly Dictionary<int, RenderTexture> spriteMasks = new Dictionary<int, RenderTexture>();
        static readonly Dictionary<int, Vector4> spriteRects = new Dictionary<int, Vector4>();
        static readonly Dictionary<int, Vector4> spriteRegions = new Dictionary<int, Vector4>();
        static readonly Vector4 WholeTexture = new Vector4(1f, 1f, 0f, 0f);
        const int MaskRadius = 15; // = WATER_MASK_RADIUS in shaders/WaterMask.shader
        static readonly Dictionary<int, SpriteRenderer> renderers = new Dictionary<int, SpriteRenderer>();
        static readonly Dictionary<int, Material> copies = new Dictionary<int, Material>();

        static bool Enabled()
        {
            if (!Platform.NewArgs(ref configuredFor)) return enabled;
            enabled = Platform.VitaDefault("-compat-no-water-mask", "-compat-water-mask");
            // The fast noise path replaces the exact skip (its one pass costs less than the skip's cheap pass).
            if (FastNoise.Enabled()) enabled = false;
            // Timing test only (wrong picture): every pixel through pass 0 (white) or pass 1 (black).
            string test = Platform.ArgValue("-compat-water-mask-test");
            maskTest = test == "white" ? Texture2D.whiteTexture : test == "black" ? Texture2D.blackTexture : null;
            if (enabled && material == null)
            {
                Shader shader = Shader.Find("Hidden/DrifterWaterMask");
                if (shader == null || !shader.isSupported) { enabled = false; return false; }
                foreach (string[] pair in new[] { new[] { "WaterSprite", "Hidden/DrifterWaterSpriteSplit" }, new[] { "PropBurble", "Hidden/DrifterPropBurbleSplit" } })
                {
                    Shader split = Shader.Find(pair[1]);
                    if (split != null && split.isSupported) splits[pair[0]] = split;
                }
                material = new Material(shader);
                material.hideFlags = HideFlags.HideAndDontSave;
                maskId = Shader.PropertyToID("_WaterMask");
                rowsId = Shader.PropertyToID("_WaterRows");
                maskRectId = Shader.PropertyToID("_WaterMaskRect");
                regionId = Shader.PropertyToID("_WaterRect");
                erodeId = Shader.PropertyToID("_WaterErode");
                format = SystemInfo.SupportsRenderTextureFormat(RenderTextureFormat.R8) ? RenderTextureFormat.R8 : RenderTextureFormat.ARGB32;
                block = new MaterialPropertyBlock();
            }
            return enabled;
        }

        public static void After(MonoBehaviour animator)
        {
            if (!MainThread.Current || !Enabled()) return;
            int id = animator.GetInstanceID();
            SpriteRenderer renderer;
            if (!renderers.TryGetValue(id, out renderer)) renderers[id] = renderer = animator.GetComponent<SpriteRenderer>();
            if (renderer == null) return;
            Material current = renderer.sharedMaterial;
            if (current == null || current.shader == null) return;
            if (!IsSplit(current.shader))
            {
                Shader split;
                if (!splits.TryGetValue(current.shader.name, out split)) return;
                renderer.sharedMaterial = Copy(current, split);
            }
            Sprite sprite = renderer.sprite;
            Vector4 rect;
            RenderTexture mask = Mask(sprite, renderer, out rect);
            renderer.GetPropertyBlock(block);
            if (maskTest != null)
            {
                if (block.GetTexture(maskId) == maskTest) return;
                block.SetTexture(maskId, maskTest);
                renderer.SetPropertyBlock(block);
                return;
            }
            Texture had = block.GetTexture(maskId);
            if (mask != null ? had == mask && block.GetVector(maskRectId).Equals(rect) : had == Texture2D.blackTexture) return;
            block.SetTexture(maskId, mask != null ? (Texture)mask : Texture2D.blackTexture);
            block.SetVector(maskRectId, rect);
            renderer.SetPropertyBlock(block);
        }

        // The sprite's mask and the split shader's _WaterMaskRect for it: a whole-texture mask
        // (rect (1, 1, 0, 0)) where the texture is small enough, else, for WaterSprite (whose read
        // centre is the sprite's own texture coordinate), a mask of the sprite's region.
        static RenderTexture Mask(Sprite sprite, SpriteRenderer renderer, out Vector4 rect)
        {
            rect = WholeTexture;
            Texture texture = sprite != null ? sprite.texture : null;
            if (texture == null) return null;
            if (texture.width * texture.height <= MaxTexels) return Mask(texture);
            int id = sprite.GetInstanceID();
            RenderTexture mask;
            if (spriteMasks.TryGetValue(id, out mask))
            {
                if (mask == null) return null;
                rect = spriteRects[id];
                if (!mask.IsCreated()) Draw(texture, mask, spriteRegions[id], 1f);
                return mask;
            }
            Shader waterSplit;
            Material shared = renderer.sharedMaterial;
            int x0, y0, w, h;
            if (!splits.TryGetValue("WaterSprite", out waterSplit) || shared.shader != waterSplit || !CanMask(texture) ||
                !Region(sprite, texture, shared, out x0, out y0, out w, out h))
            {
                spriteMasks[id] = null;
                return null;
            }
            int width = texture.width, height = texture.height;
            mask = new RenderTexture(w, h, 0, format, RenderTextureReadWrite.Linear);
            mask.name = "WaterMask " + texture.name + " " + sprite.name;
            mask.hideFlags = HideFlags.HideAndDontSave;
            mask.filterMode = FilterMode.Point;
            mask.wrapMode = TextureWrapMode.Clamp;
            rect = new Vector4((float)width / w, (float)height / h, -(float)x0 / w, -(float)y0 / h);
            Vector4 region = new Vector4((float)w / width, (float)h / height, (float)x0 / width, (float)y0 / height);
            spriteMasks[id] = mask;
            spriteRects[id] = rect;
            spriteRegions[id] = region;
            Draw(texture, mask, region, 1f);
            return mask;
        }

        // The texels of the sprite's texture that a sprite mask covers (from x0, y0, w by h), false
        // if none or more than MaxTexels: those the read centre (texcoord * _MainTex_ST.xy +
        // _MainTex_ST.zw) can fall in, and one more on each side, as the mask is eroded by one
        // texel: a mask read that lands in a neighbouring texel (the mask's coordinate is computed
        // differently from the texture's) still answers for a square that holds the texture read's
        // own. Rows within MaskRadius of the top and bottom of the drawn region may be wrong (see
        // WaterMask.shader), so the region is that much taller.
        static bool Region(Sprite sprite, Texture texture, Material shared, out int x0, out int y0, out int w, out int h)
        {
            Vector2 scale = shared.GetTextureScale("_MainTex"), offset = shared.GetTextureOffset("_MainTex");
            float minU = float.MaxValue, minV = float.MaxValue, maxU = float.MinValue, maxV = float.MinValue;
            foreach (Vector2 uv in sprite.uv)
            {
                float u = uv.x * scale.x + offset.x, v = uv.y * scale.y + offset.y;
                minU = Mathf.Min(minU, u); maxU = Mathf.Max(maxU, u);
                minV = Mathf.Min(minV, v); maxV = Mathf.Max(maxV, v);
            }
            int width = texture.width, height = texture.height;
            x0 = Mathf.FloorToInt(minU * width) - 1;
            y0 = Mathf.FloorToInt(minV * height) - 1 - MaskRadius;
            w = Mathf.CeilToInt(maxU * width) + 1 - x0;
            h = Mathf.CeilToInt(maxV * height) + 1 + MaskRadius - y0;
            return !(minU > maxU) && (long)w * h <= MaxTexels;
        }

        // Textures the masks are drawn for: point-filtered Texture2Ds without mipmaps, as the
        // split shaders read them.
        static bool CanMask(Texture texture)
        {
            Texture2D image = texture as Texture2D;
            return image != null && image.mipmapCount <= 1 && texture.filterMode == FilterMode.Point;
        }

        static bool IsSplit(Shader shader)
        {
            foreach (Shader split in splits.Values) if (split == shader) return true;
            return false;
        }

        static Material Copy(Material original, Shader split)
        {
            Material copy;
            if (copies.TryGetValue(original.GetInstanceID(), out copy) && copy != null) return copy;
            copy = new Material(original);
            copy.shader = split;
            copy.name = original.name;
            copy.hideFlags = HideFlags.DontSave;
            copies[original.GetInstanceID()] = copy;
            return copy;
        }

        // A texture's mask, drawn on first use (null where the texture cannot have one); for a
        // texture of at most MaxTexels. Keyed by instance id: a reference here would keep the
        // room's texture loaded.
        static RenderTexture Mask(Texture texture)
        {
            RenderTexture mask;
            if (masks.TryGetValue(texture.GetInstanceID(), out mask))
            {
                if (mask != null && !mask.IsCreated()) Draw(texture, mask, WholeTexture, 0f);
                return mask;
            }
            if (!CanMask(texture))
            {
                masks[texture.GetInstanceID()] = null;
                return null;
            }
            mask = new RenderTexture(texture.width, texture.height, 0, format, RenderTextureReadWrite.Linear);
            mask.name = "WaterMask " + texture.name;
            mask.hideFlags = HideFlags.HideAndDontSave;
            mask.filterMode = FilterMode.Point;
            mask.wrapModeU = texture.wrapModeU;
            mask.wrapModeV = texture.wrapModeV;
            masks[texture.GetInstanceID()] = mask;
            Draw(texture, mask, WholeTexture, 0f);
            return mask;
        }

        // Masks drawn so far (the probe's -diag-frame-log reads it).
        public static int MaskDraws;

        // region: the part of the texture the mask covers (uv scale xy, offset zw); erode 1 lowers
        // every radius by one (sprite region masks, see Mask(Sprite, ...)).
        static void Draw(Texture texture, RenderTexture mask, Vector4 region, float erode)
        {
            MaskDraws++;
            RenderTexture previous = RenderTexture.active;
            mask.Create();
            RenderTexture rows = RenderTexture.GetTemporary(mask.width, mask.height, 0, format, RenderTextureReadWrite.Linear);
            rows.filterMode = FilterMode.Point;
            rows.wrapModeU = mask.wrapModeU;
            rows.wrapModeV = mask.wrapModeV;
            material.SetVector(regionId, region);
            material.SetFloat(erodeId, erode);
            Graphics.Blit(texture, rows, material, 0);
            material.SetTexture(rowsId, rows);
            Graphics.Blit(texture, mask, material, 1);
            material.SetTexture(rowsId, null);
            RenderTexture.ReleaseTemporary(rows);
            RenderTexture.active = previous;
        }

        // After a scene unloads (a room change; SceneUnloads), the masks and material copies that
        // no live renderer uses any more are destroyed, so the room's textures can unload with it.
        internal static void Unloaded()
        {
            if (renderers.Count == 0 && masks.Count == 0 && copies.Count == 0 && spriteMasks.Count == 0) return;
            var usedTextures = new HashSet<int>();
            var usedSprites = new HashSet<int>();
            var usedMaterials = new HashSet<int>();
            var gone = new List<int>();
            foreach (KeyValuePair<int, SpriteRenderer> pair in renderers)
            {
                SpriteRenderer renderer = pair.Value;
                if (renderer == null) { gone.Add(pair.Key); continue; }
                if (renderer.sprite != null) usedSprites.Add(renderer.sprite.GetInstanceID());
                if (renderer.sprite != null && renderer.sprite.texture != null) usedTextures.Add(renderer.sprite.texture.GetInstanceID());
                if (renderer.sharedMaterial != null) usedMaterials.Add(renderer.sharedMaterial.GetInstanceID());
            }
            foreach (int key in gone) renderers.Remove(key);
            Prune(masks, (key, mask) => usedTextures.Contains(key), gone);
            Prune(spriteMasks, (key, mask) => usedSprites.Contains(key), gone);
            foreach (int key in gone)
            {
                spriteRects.Remove(key);
                spriteRegions.Remove(key);
            }
            Prune(copies, (key, copy) => copy != null && usedMaterials.Contains(copy.GetInstanceID()), gone);
        }

        // Destroys and removes the entries keep refuses; their keys are left in gone.
        static void Prune<T>(Dictionary<int, T> map, Func<int, T, bool> keep, List<int> gone) where T : Object
        {
            gone.Clear();
            foreach (KeyValuePair<int, T> pair in map)
                if (!keep(pair.Key, pair.Value)) gone.Add(pair.Key);
            foreach (int key in gone)
            {
                if (map[key] != null) Object.Destroy(map[key]);
                map.Remove(key);
            }
        }
    }
}
