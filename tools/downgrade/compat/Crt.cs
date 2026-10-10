using System;
using System.Collections.Generic;
using UnityEngine;
using Object = UnityEngine.Object;

// The CRT filter's single-pass and two-step paths (its test flags are in CrtCompare.cs).
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // The image effect CRTEffect was (its OnRenderImage, renamed by the downgrader); see
    // Crt.Enabled.
    public class CrtImageEffect : MonoBehaviour
    {
        public CrtRender Render;

        void OnRenderImage(RenderTexture source, RenderTexture destination)
        {
            if (Render != null) Render(source, destination);
            else Graphics.Blit(source, destination);
        }
    }

    // The game's CRT filter (BaseCRTEffect.ProcessEffect) draws three full-screen passes
    // through two intermediate textures. CRT/SinglePass (made by crt_shader.py from
    // shaders/CrtSinglePass.shader.in and the game's own CRT shaders) is the
    // same filter without them; it leaves out CRT/Blur, which copies its input when the blur
    // kernel is a single tap (the game's blur size of 0). The downgrader calls Render before
    // the original passes and skips them when it returns true; it reads the uniforms
    // ProcessEffect has just set. Used on the PS Vita, or with -crt-single-pass (Windows diag
    // builds only: release Windows builds leave the shader out, and the passes draw), and once
    // the CRT mode patched (CrtToggle) asks for it (UseSinglePass).
    // The settings the game uses (FastModes: chromatic aberration with a bleeding distance of
    // about the aberration's, Lighten noise, no interference) get the fast passes 2 and 3: 11
    // source taps over a grid mesh whose vertices carry the curved positions, a lookup texture
    // for the per-pixel factors (pass 1, drawn once per size and settings) and a noise texture.
    // Other settings, or -crt-generic, get pass 0.
    // On the PS Vita (or with -crt-aber on), when the aberration is a whole number of texels,
    // the fast settings are drawn in two steps: a copy of the source with red and blue shifted
    // by the aberration (pass 5), then the filter from five reads of it (pass 6 or 7, or pass 8
    // for the game's own settings, with its own lookup texture, pass 9). The Vita's GPU pays
    // for each instruction, and far more for texture reads at positions the fragment shader
    // computes: in LabKitchen pass 2 took about 18 ms of GPU time there, passes 5 and 8 about
    // 7 ms. Test flags: see CrtCompare.cs. Main thread only.
    public static partial class Crt
    {
        static readonly string[] Floats =
        {
            "pixelSizeX", "pixelSizeY", "seconds", "bleedDist", "bleedStr", "blurStr", "rgbMaskSub", "rgbMaskSep",
            "rgbMaskStr", "colorNoiseMode", "colorNoiseStr", "monoNoiseMode", "monoNoiseStr", "interWidth",
            "interSpeed", "interStr", "interSplit", "aberStr",
        };
        static readonly string[] Colors = { "minLevels", "maxLevels", "blackPoint", "whitePoint" };
        static readonly string[] FinalFloats =
            { "maskMode", "maskStr", "vignetteStr", "vignetteSize", "crtBendX", "crtBendY", "crtOverscan", "flipV" };
        // What the lookup texture depends on besides the size.
        static readonly string[] LutFloats =
            { "pixelSizeX", "pixelSizeY", "maskMode", "maskStr", "vignetteStr", "vignetteSize", "crtBendX", "crtBendY", "crtOverscan",
              "rgbMaskSub", "rgbMaskSep", "rgbMaskStr" };
        // The shader's passes (see shaders/CrtSinglePass.shader.in).
        const int GenericPass = 0, LutPass = 1, FastPass = 2, FastColorPass = 3;
        const int AberCopyPass = 5, AberPass = 6, AberColorPass = 7, AberGamePass = 8, LutGamePass = 9;
        // The grid mesh's cells (16:9, as the screen; positions are interpolated linearly within
        // a cell), and the side of the noise texture (256x256 texels, wrapped).
        const int GridColumns = 32, GridRows = 18, NoiseSize = 256;

        static Material singlePass;
        static Mesh grid;
        static Texture2D noise;
        static RenderTexture lut;
        static readonly float[] lutKey = new float[LutFloats.Length];
        static bool useSinglePass, forceGeneric, shaderMissing;
        static string[] configuredFor;
        static int lutDraws;
        static bool useAber, useAberGame;
        static int[] floatIds, colorIds, finalIds, lutIds;
        static int colorMatId;
        static float lutGame = -1f, noiseColor = -1f, noiseMono = -1f;
        static Texture2D gameNoise;
        static RenderTexture aberTexture;
        static int lastPass = -1;
        static bool patched;

        // The CRT mode patched (CrtToggle): the single pass from now on. CrtToggle turns the
        // effect off before and on after, so Disabled and Enabled undo and set up the image-effect
        // or direct path.
        public static void UseSinglePass()
        {
            if (patched) return;
            patched = true;
            configuredFor = null;
            Configure();
        }

        // Whether the filter is drawn with the single pass (else the game's three passes).
        public static bool SinglePass
        {
            get
            {
                Configure();
                return useSinglePass;
            }
        }

        // Whether the build turned out to have no CRT/SinglePass (Windows release builds): the
        // original passes draw whatever was asked.
        internal static bool ShaderMissing
        {
            get { return shaderMissing; }
        }

        static void Configure()
        {
            // Again when the probe hands over its flags (it may start after the first call).
            if (!Platform.NewArgs(ref configuredFor)) return;
            bool vita = Application.platform == RuntimePlatform.PSP2;
            useSinglePass = !shaderMissing &&
                            (patched || ((vita || Platform.HasArg("-crt-single-pass")) && !Platform.HasArg("-crt-three-pass")));
            forceGeneric = Platform.HasArg("-crt-generic");
            ConfigureTests();
            // -crt-aber on|off: the two steps for the fast settings (on by default on the PS Vita).
            string aber = Platform.ArgValue("-crt-aber");
            useAber = aber != null ? aber == "on" : vita;
            // -crt-aber-game off: pass 6 instead of 8 for the game's settings.
            useAberGame = Platform.ArgValue("-crt-aber-game") != "off";
            string pathFlag = Platform.ArgValue("-crt-path");
            useDirect = pathFlag != null ? pathFlag == "direct" : vita;
            Debug.Log("CRT filter: " + (useDirect ? "direct" : "image effect") + ", " +
                      (useSinglePass ? useAber ? "two steps" : "single pass" : "three passes") + (patched ? " (mode patched)" : ""));
        }

        // How the filter gets the picture. The game's CRTEffect is an image effect on the GUI
        // camera, which clears nothing: Unity renders that camera into a texture after copying
        // the screen (the room camera's picture) into it, and the filter draws that texture to
        // the screen. The direct path renders the room and GUI cameras into one texture of the
        // screen's size and draws the filter from it to the screen after the GUI camera, with
        // no copy of the screen. The downgrader renames CRTEffect.OnRenderImage, so Unity sees
        // no image effect there; for the image-effect path Enabled gives the camera a
        // CrtImageEffect component that calls it. -crt-path direct|image picks the path
        // (direct on the PS Vita); -crt-three-pass draws the original passes.
        static bool useDirect;
        // How often PreCull rendered the earlier cameras again and cleared the screen (the probe's
        // -diag-frame-log reads it).
        public static int ScreenClears;
        static RenderTexture directTarget;
        static Camera directCamera;
        static readonly List<Camera> redirected = new List<Camera>();
        // Camera.GetAllCameras' buffer, grown when there are more cameras.
        static Camera[] cameras = new Camera[8];

        // From CRTEffect.OnEnable (added by the downgrader).
        public static void Enabled(Behaviour effect, CrtRender render)
        {
            Configure();
            if (useDirect)
            {
                Camera camera = effect.GetComponent<Camera>();
                if (camera != null) Target(camera);
                return;
            }
            var image = effect.GetComponent<CrtImageEffect>();
            if (image == null) image = effect.gameObject.AddComponent<CrtImageEffect>();
            image.Render = render;
            image.enabled = true;
        }

        // From the start of BaseCRTEffect.OnDisable.
        public static void Disabled(Behaviour effect)
        {
            var image = effect.GetComponent<CrtImageEffect>();
            if (image != null) image.enabled = false;
            if (directCamera == null || directCamera.gameObject != effect.gameObject) return;
            Untarget(directCamera);
            directCamera = null;
            // Off, the filter keeps no video memory (they are made again when it turns on).
            if (directTarget != null) directTarget.Release();
            if (aberTexture != null) aberTexture.Release();
            if (lut != null) lut.Release();
        }

        // From the start of BaseCRTEffect.InternalPreRender (the effect camera's OnPreCull);
        // true skips it. On the direct path the effect camera and the cameras drawing to the
        // screen before it (the room camera) render into directTarget.
        public static bool PreCull(Camera camera)
        {
            Configure();
            if (!useDirect || camera == null) return false;
            Target(camera);
            // Cameras that draw to the screen before this one: the first time, render them
            // again into the texture, as InternalPreRender does for otherCameras. They have
            // already drawn to the screen this frame, which is cleared to black then.
            if (Camera.allCamerasCount > cameras.Length) cameras = new Camera[Camera.allCamerasCount * 2];
            int count = Camera.GetAllCameras(cameras);
            bool drewToScreen = false;
            for (int i = 0; i < count; i++)
            {
                Camera other = cameras[i];
                if (other == camera || other.targetTexture != null || other.depth >= camera.depth) continue;
                other.targetTexture = directTarget;
                if (!redirected.Contains(other)) redirected.Add(other);
                other.Render();
                drewToScreen = true;
            }
            Array.Clear(cameras, 0, count);
            if (drewToScreen)
            {
                ScreenClears++;
                RenderTexture active = RenderTexture.active;
                RenderTexture.active = null;
                GL.Clear(true, true, Color.black);
                RenderTexture.active = active;
            }
            return true;
        }

        // The effect camera and the cameras redirected to directTarget render to the screen again.
        static void Untarget(Camera effectCamera)
        {
            foreach (Camera camera in redirected)
                if (camera != null && camera.targetTexture == directTarget) camera.targetTexture = null;
            redirected.Clear();
            if (effectCamera.targetTexture == directTarget) effectCamera.targetTexture = null;
        }

        // The effect camera renders into directTarget (made or remade at the screen's size).
        // Set when the effect is enabled, not only from its camera's OnPreCull: a target set
        // there takes effect from the next frame, so the first frame after enabling showed
        // the filter's picture without anything the effect camera draws (the GUI camera: the
        // fade, menus). On a room's first frame that was the room unfaded before the title's
        // fade-in (the PS Vita's title screen flashed).
        static void Target(Camera camera)
        {
            if (directTarget == null || directTarget.width != Screen.width || directTarget.height != Screen.height)
            {
                if (directTarget != null)
                {
                    Untarget(camera);
                    directTarget.Release();
                    Object.Destroy(directTarget);
                }
                directTarget = new RenderTexture(Screen.width, Screen.height, 24, RenderTextureFormat.ARGB32);
                directTarget.hideFlags = HideFlags.HideAndDontSave;
                directTarget.name = "CrtDirect";
            }
            if (!directTarget.IsCreated()) directTarget.Create();
            directCamera = camera;
            camera.targetTexture = directTarget;
        }

        // From the start of BaseCRTEffect.OnPostRender: on the direct path, the texture the
        // downgraded code passes to ProcessEffect(texture, null), which draws to the screen.
        public static Texture PostRender(Behaviour effect)
        {
            if (!useDirect || directCamera == null || directCamera.gameObject != effect.gameObject) return null;
            return directTarget;
        }

        public static bool Render(Texture src, RenderTexture dest, Material blur, Material post, Material final)
        {
            Configure();
            bool compare = CompareFrame();
            if (!useSinglePass && !compare) return false;
            // The blur pass is a copy only if its kernel's off-centre taps weigh nothing.
            Vector4 kernel = blur.GetVector("blurKernel");
            if (kernel.x * (kernel.x + kernel.y) * 8f > blur.GetFloat("blurZ") * 1e-5f) return false;
            if (singlePass == null && !CreateMaterial()) return false;
            CopyUniforms(post, final);
            int pass = TestPass(forceGeneric || !FastModes(post) ? GenericPass : Prepare(src, post));
            if (pass != lastPass)
            {
                lastPass = pass;
                Debug.Log("CRT single pass: pass " + pass + (pass == GenericPass ? " (generic)" : pass == FastPass || pass == AberPass ? " (fast)" : pass == FastColorPass || pass == AberColorPass ? " (fast with color transform)" : ""));
            }
            if (compare)
            {
                Compare(src, pass);
                return false;
            }
            Draw(src, dest, pass);
            return true;
        }

        // The single pass's material and the uniforms' ids; false, and the original passes from
        // then on, when the build has no CRT/SinglePass (Windows release builds leave it out,
        // pipeline.py).
        static bool CreateMaterial()
        {
            Shader shader = Shader.Find("CRT/SinglePass");
            if (shader == null)
            {
                Debug.LogWarning("CRT filter: CRT/SinglePass is not in this build; drawing the original passes");
                shaderMissing = true;
                useSinglePass = false;
                CancelCompare();
                return false;
            }
            singlePass = new Material(shader);
            singlePass.hideFlags = HideFlags.HideAndDontSave;
            floatIds = Ids(Floats);
            colorIds = Ids(Colors);
            finalIds = Ids(FinalFloats);
            lutIds = Ids(LutFloats);
            colorMatId = Shader.PropertyToID("colorMat");
            return true;
        }

        // The original materials' uniforms, by property ID: a lookup by name hashes the string,
        // and there are about 60 a frame.
        static void CopyUniforms(Material post, Material final)
        {
            foreach (int id in floatIds) singlePass.SetFloat(id, post.GetFloat(id));
            foreach (int id in colorIds) singlePass.SetColor(id, post.GetColor(id));
            singlePass.SetMatrix(colorMatId, post.GetMatrix(colorMatId));
            foreach (int id in finalIds) singlePass.SetFloat(id, final.GetFloat(id));
        }

        static int[] Ids(string[] names)
        {
            var ids = new int[names.Length];
            for (int i = 0; i < names.Length; i++) ids[i] = Shader.PropertyToID(names[i]);
            return ids;
        }

        // The settings the fast passes implement.
        static bool FastModes(Material post)
        {
            float aber = post.GetFloat("aberStr");
            if (aber == 0f || Mathf.Abs(post.GetFloat("bleedDist") - Mathf.Abs(aber)) > 0.05f) return false;
            if (post.GetFloat("interStr") != 0f) return false;
            if (post.GetFloat("colorNoiseStr") != 0f && post.GetFloat("colorNoiseMode") != 4f) return false; // Lighten
            if (post.GetFloat("monoNoiseStr") != 0f && post.GetFloat("monoNoiseMode") != 4f) return false;
            return post.GetMatrix(colorMatId).GetRow(3) == new Vector4(0f, 0f, 0f, 1f);
        }

        // Uniforms, lookup and noise textures for the fast passes; returns the pass.
        static int Prepare(Texture src, Material post)
        {
            float pixelX = 1f / src.width, pixelY = 1f / src.height;
            float red = -pixelX * post.GetFloat("aberStr"), bleed = post.GetFloat("bleedDist");
            singlePass.SetVector("_CrtTaps", new Vector4(red, red + Mathf.Sign(red) * pixelX * bleed, 0f, pixelY * bleed));
            singlePass.SetFloat("_CrtOffsetScale",
                2f * post.GetFloat("rgbMaskSep") * post.GetFloat("rgbMaskSub") * post.GetFloat("rgbMaskStr"));
            // A new noise offset whenever the filter's clock moves, as the original's noise follows it.
            double seconds = post.GetFloat("seconds");
            double x = Math.Sin(seconds * 12.9898) * 43758.5453, y = Math.Sin(seconds * 78.233) * 43758.5453;
            singlePass.SetVector("_CrtNoiseST", new Vector4((float)src.width / NoiseSize, (float)src.height / NoiseSize,
                (float)Math.Floor((x - Math.Floor(x)) * NoiseSize) / NoiseSize, (float)Math.Floor((y - Math.Floor(y)) * NoiseSize) / NoiseSize));
            bool identity = IdentityColor(post);
            bool game = useAber && WholeAberration(post);
            bool gamePass = game && useAberGame && post.GetFloat("blurStr") == 1f && identity;
            singlePass.SetTexture("_CrtNoise", gamePass ? GameNoise(post) : Noise());
            Lut(src, gamePass);
            if (gamePass) return AberGamePass;
            if (game) return identity ? AberPass : AberColorPass;
            return identity ? FastPass : FastColorPass;
        }

        // The lookup texture (pass 1, or pass 9 for pass 8), drawn again when the size, the pass
        // or a uniform it depends on (LutFloats) changed.
        static void Lut(Texture src, bool gamePass)
        {
            bool changed = lut == null || !lut.IsCreated() || lut.width != src.width || lut.height != src.height ||
                lutGame != (gamePass ? 1f : 0f);
            lutGame = gamePass ? 1f : 0f;
            for (int i = 0; i < LutFloats.Length; i++)
            {
                float value = singlePass.GetFloat(lutIds[i]);
                if (value != lutKey[i]) changed = true;
                lutKey[i] = value;
            }
            if (changed)
            {
                if (lut != null && (lut.width != src.width || lut.height != src.height))
                {
                    lut.Release();
                    Object.Destroy(lut);
                    lut = null;
                }
                if (lut == null)
                {
                    lut = new RenderTexture(src.width, src.height, 0, RenderTextureFormat.ARGB32, RenderTextureReadWrite.Linear);
                    lut.hideFlags = HideFlags.HideAndDontSave;
                    lut.filterMode = FilterMode.Point;
                    lut.wrapMode = TextureWrapMode.Clamp;
                }
                Graphics.Blit(src, lut, singlePass, gamePass ? LutGamePass : LutPass);
                if (++lutDraws <= 3 || lutDraws % 300 == 0) Debug.Log("CRT lookup texture drawn (" + lutDraws + ")");
            }
            singlePass.SetTexture("_CrtLut", lut);
        }

        static Texture2D NoiseTexture()
        {
            var texture = new Texture2D(NoiseSize, NoiseSize, TextureFormat.RGBA32, false, true);
            texture.hideFlags = HideFlags.HideAndDontSave;
            texture.filterMode = FilterMode.Bilinear;
            texture.wrapMode = TextureWrapMode.Repeat;
            return texture;
        }

        // The same noise for every pass.
        static byte[] NoiseBytes()
        {
            var bytes = new byte[NoiseSize * NoiseSize * 4];
            new System.Random(1).NextBytes(bytes);
            return bytes;
        }

        // The noise texture of passes 2, 3, 6 and 7 (pass 8 has GameNoise), made on first use.
        static Texture2D Noise()
        {
            if (noise == null)
            {
                noise = NoiseTexture();
                noise.LoadRawTextureData(NoiseBytes());
                noise.Apply(false, true);
            }
            return noise;
        }

        // Pass 8's noise texture: the noise times colorNoiseStr (rgb) and its red times
        // monoNoiseStr (a), made again when they change.
        static Texture2D GameNoise(Material post)
        {
            float color = post.GetFloat("colorNoiseStr"), mono = post.GetFloat("monoNoiseStr");
            if (gameNoise != null && color == noiseColor && mono == noiseMono) return gameNoise;
            noiseColor = color;
            noiseMono = mono;
            if (gameNoise == null) gameNoise = NoiseTexture();
            byte[] bytes = NoiseBytes();
            for (int i = 0; i < bytes.Length; i += 4)
            {
                byte red = bytes[i];
                bytes[i] = Scaled(red, color);
                bytes[i + 1] = Scaled(bytes[i + 1], color);
                bytes[i + 2] = Scaled(bytes[i + 2], color);
                bytes[i + 3] = Scaled(red, mono);
            }
            gameNoise.LoadRawTextureData(bytes);
            gameNoise.Apply(false, false);
            return gameNoise;
        }

        // value * factor, rounded and kept within a byte (a strength outside 0-1 would wrap).
        static byte Scaled(byte value, float factor)
        {
            return (byte)Mathf.Clamp(Mathf.RoundToInt(value * factor), 0, 255);
        }

        static bool IdentityColor(Material post)
        {
            Matrix4x4 matrix = post.GetMatrix(colorMatId);
            for (int row = 0; row < 4; row++)
                for (int column = 0; column < 4; column++)
                    if (Mathf.Abs(matrix[row, column] - (row == column ? 1f : 0f)) > 1e-5f) return false;
            Color min = post.GetColor("minLevels"), max = post.GetColor("maxLevels");
            Color black = post.GetColor("blackPoint"), white = post.GetColor("whitePoint");
            for (int i = 0; i < 3; i++)
                if (Mathf.Abs(min[i]) > 1e-5f || Mathf.Abs(max[i] - 1f) > 1e-5f || black[i] > 0f || white[i] < 1f) return false;
            return true;
        }

        // Passes 5-8 need the aberration to be a whole number of texels.
        static bool WholeAberration(Material post)
        {
            float aber = Mathf.Abs(post.GetFloat("aberStr"));
            return aber >= 0.5f && Mathf.Abs(aber - Mathf.Round(aber)) < 1e-3f;
        }

        static void Draw(Texture src, RenderTexture target, int pass)
        {
            if (pass == GenericPass)
            {
                Graphics.Blit(src, target, singlePass, pass);
                return;
            }
            if (pass == AberPass || pass == AberColorPass || pass == AberGamePass)
            {
                if (aberTexture == null || aberTexture.width != src.width || aberTexture.height != src.height)
                {
                    if (aberTexture != null) { aberTexture.Release(); Object.Destroy(aberTexture); }
                    aberTexture = new RenderTexture(src.width, src.height, 0, RenderTextureFormat.ARGB32);
                    aberTexture.hideFlags = HideFlags.HideAndDontSave;
                    aberTexture.name = "CrtAber";
                    aberTexture.filterMode = FilterMode.Bilinear;
                    aberTexture.wrapMode = TextureWrapMode.Clamp;
                }
                // Pass 5 reads texel centres: nearest filtering gives the same values.
                FilterMode filter = src.filterMode;
                src.filterMode = FilterMode.Point;
                Graphics.Blit(src, aberTexture, singlePass, AberCopyPass);
                src.filterMode = filter;
                singlePass.SetTexture("_CrtAber", aberTexture);
            }
            if (grid == null) grid = Grid();
            singlePass.SetTexture("_MainTex", src);
            RenderTexture.active = target;
            if (target == null) GL.Viewport(new Rect(0f, 0f, Screen.width, Screen.height));
            GL.PushMatrix();
            GL.LoadOrtho();
            singlePass.SetPass(pass);
            Graphics.DrawMeshNow(grid, Matrix4x4.identity);
            GL.PopMatrix();
        }

        // The unit square in GridColumns x GridRows cells, texcoords equal to positions.
        static Mesh Grid()
        {
            var vertices = new Vector3[(GridColumns + 1) * (GridRows + 1)];
            var texcoords = new Vector2[vertices.Length];
            for (int y = 0; y <= GridRows; y++)
                for (int x = 0; x <= GridColumns; x++)
                {
                    var position = new Vector2((float)x / GridColumns, (float)y / GridRows);
                    vertices[y * (GridColumns + 1) + x] = position;
                    texcoords[y * (GridColumns + 1) + x] = position;
                }
            var triangles = new int[GridColumns * GridRows * 6];
            for (int y = 0, i = 0; y < GridRows; y++)
                for (int x = 0; x < GridColumns; x++, i += 6)
                {
                    int corner = y * (GridColumns + 1) + x;
                    triangles[i] = corner;
                    triangles[i + 1] = corner + GridColumns + 1;
                    triangles[i + 2] = corner + 1;
                    triangles[i + 3] = corner + 1;
                    triangles[i + 4] = corner + GridColumns + 1;
                    triangles[i + 5] = corner + GridColumns + 2;
                }
            var mesh = new Mesh();
            mesh.hideFlags = HideFlags.HideAndDontSave;
            mesh.vertices = vertices;
            mesh.uv = texcoords;
            mesh.triangles = triangles;
            mesh.UploadMeshData(true);
            return mesh;
        }
    }
}
