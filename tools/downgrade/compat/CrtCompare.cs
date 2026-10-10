using System;
using UnityEngine;
using Object = UnityEngine.Object;

// The CRT filter's test flags (Crt, continued).
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // Test flags, read from the probe's flags only (Platform.ProbeArgs; release players ignore
    // them):
    // -crt-pass <n> draws pass n instead of the fast pass chosen (4: a cost floor, not the filter).
    // -crt-compare <dir> <frame>: on the filter's <frame>th frame, the single pass renders
    // into <dir>/crt-single.raw, then the original passes draw (crt-original.raw);
    // crt-input.raw is the filter's input (width, height as int32, RGBA rows bottom-up).
    public static partial class Crt
    {
        static int passOverride = -1;
        static string compareDir;
        static int compareFrame = -1, filterFrames;
        static bool comparePending;

        // From Configure.
        static void ConfigureTests()
        {
            int number;
            passOverride = int.TryParse(Platform.ProbeArg("-crt-pass"), out number) && number >= 0 ? number : -1;
            compareDir = Platform.ProbeArg("-crt-compare");
            compareFrame = compareDir != null && int.TryParse(Platform.ProbeArg("-crt-compare", 2), out number) && number >= 0 ? number : -1;
        }

        // The pass to draw: -crt-pass's instead of a fast one.
        static int TestPass(int pass)
        {
            return passOverride >= 0 && pass != GenericPass ? passOverride : pass;
        }

        // Whether this frame of the filter is the one -crt-compare asks for.
        static bool CompareFrame()
        {
            return compareFrame >= 0 && ++filterFrames >= compareFrame;
        }

        static void CancelCompare()
        {
            compareFrame = -1;
        }

        // The single pass into crt-single.raw and the input into crt-input.raw; the original
        // passes then draw, and AfterOriginal writes what they drew.
        static void Compare(Texture src, int pass)
        {
            compareFrame = -1;
            RenderTexture single = RenderTexture.GetTemporary(src.width, src.height, 0, RenderTextureFormat.ARGB32);
            Draw(src, single, pass);
            WriteActive(single, "crt-single.raw");
            RenderTexture.ReleaseTemporary(single);
            var input = src as RenderTexture;
            if (input != null) WriteActive(input, "crt-input.raw");
            comparePending = true;
        }

        // Called after the original passes, or after Render drew instead of them.
        public static void AfterOriginal()
        {
            if (!comparePending) return;
            comparePending = false;
            WriteActive(RenderTexture.active, "crt-original.raw");
        }

        static void WriteActive(RenderTexture target, string name)
        {
            RenderTexture previous = RenderTexture.active;
            RenderTexture.active = target;
            int width = target != null ? target.width : Screen.width, height = target != null ? target.height : Screen.height;
            var read = new Texture2D(width, height, TextureFormat.RGBA32, false);
            read.ReadPixels(new Rect(0, 0, width, height), 0, 0);
            read.Apply();
            RenderTexture.active = previous;
            byte[] pixels = read.GetRawTextureData();
            Object.Destroy(read);
            var data = new byte[8 + pixels.Length];
            BitConverter.GetBytes(width).CopyTo(data, 0);
            BitConverter.GetBytes(height).CopyTo(data, 4);
            pixels.CopyTo(data, 8);
            System.IO.File.WriteAllBytes(System.IO.Path.Combine(compareDir, name), data);
        }
    }
}
