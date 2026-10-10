using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using UnityEngine;

// Experiments that run as coroutines after the dump (-diag-check-water, -diag-check-global/-blend,
// -diag-check-effects), and -diag-dump-rt.
// Part of the parity probe (DrifterDiag); see Diag.cs.
namespace DrifterDiag
{
    public static partial class Probe
    {
        // done.txt waits for these, so runs are not stopped halfway.
        static bool experimentsRunning;

        static System.Collections.IEnumerator Experiments(MonoBehaviour host)
        {
            try
            {
                // The water check reads render textures back; inside a camera callback (where the
                // probe runs) that crashed the PS Vita player in ReadPixels, so it runs at frame end.
                if (checkWater)
                {
                    yield return new WaitForEndOfFrame();
                    DumpStep("check-water", CheckWater);
                }
                if (checkGlobal != null) yield return host.StartCoroutine(Guarded("check-global", CheckGlobal()));
                if (checkBlend != null) yield return host.StartCoroutine(Guarded("check-blend", CheckBlend()));
                if (checkEffects > 0) yield return host.StartCoroutine(Guarded("check-effects", CheckEffects()));
            }
            finally { experimentsRunning = false; }
        }

        // Runs an experiment's steps, ending it with "<name> failed: ..." when one throws (a
        // coroutine that throws would leave the one waiting for it waiting for good).
        static System.Collections.IEnumerator Guarded(string name, System.Collections.IEnumerator steps)
        {
            while (true)
            {
                object current;
                try
                {
                    if (!steps.MoveNext()) yield break;
                    current = steps.Current;
                }
                catch (Exception e)
                {
                    Event(name + " failed: " + Failure(e));
                    yield break;
                }
                yield return current;
            }
        }

        // -diag-check-effects <samples>: at dump time, <samples> times (every third frame), renders
        // every camera with a target texture three times in the same frame: with each drop and bee
        // Compat EffectSprites draws shown by its own renderer in the state the game gave it (the
        // original's way), with neither, and with the effect's pages (as played); reads them back
        // and counts the pixels that differ between the first and the last, and those the items
        // cover at all (against the second), with the mean absolute difference over the covered
        // pixels (check-effects.txt). The first six renders are written as
        // effects-<n>-original.raw, effects-<n>-pages.raw and effects-<n>-none.raw.
        static int checkEffects;

        static System.Collections.IEnumerator CheckEffects()
        {
            Type effects = FindType("DrifterCompat.EffectSprites");
            if (effects == null) { Event("check-effects: no EffectSprites"); yield break; }
            System.Reflection.MethodInfo show = effects.GetMethod("Show");
            var text = new StringBuilder();
            text.AppendLine("enabled " + effects.GetMethod("Enabled").Invoke(null, null));
            var reads = new Dictionary<RenderTexture, Texture2D>();
            long total = 0, differing = 0, covered = 0, renders = 0;
            double absSum = 0;
            int saved = 0, samples = 0;
            for (int sample = 0; sample < checkEffects; sample++)
            {
                yield return new WaitForEndOfFrame();
                foreach (Camera camera in Camera.allCameras)
                {
                    RenderTexture target = camera.targetTexture;
                    if (target == null) continue;
                    Texture2D read;
                    if (!reads.TryGetValue(target, out read)) reads[target] = read = new Texture2D(target.width, target.height, TextureFormat.RGBA32, false);
                    show.Invoke(null, new object[] { 1 });
                    camera.Render();
                    Color32[] original = ReadTarget(target, read);
                    show.Invoke(null, new object[] { 2 });
                    camera.Render();
                    Color32[] none = ReadTarget(target, read);
                    show.Invoke(null, new object[] { 0 });
                    camera.Render();
                    Color32[] pages = ReadTarget(target, read);
                    int count = Differing(original, pages), cover = 0;
                    double abs = 0;
                    for (int k = 0; k < original.Length; k++)
                    {
                        Color32 o = original[k], n = none[k], q = pages[k];
                        bool touched = o.r != n.r || o.g != n.g || o.b != n.b || o.a != n.a || q.r != n.r || q.g != n.g || q.b != n.b || q.a != n.a;
                        if (!touched) continue;
                        cover++;
                        abs += (Math.Abs(o.r - q.r) + Math.Abs(o.g - q.g) + Math.Abs(o.b - q.b)) / 3.0;
                    }
                    if (cover == 0 && count == 0) continue;
                    renders++;
                    total += original.Length;
                    differing += count;
                    covered += cover;
                    absSum += abs;
                    text.AppendLine("sample " + sample + " frame " + Time.frameCount + " " + camera.name + " differing " + count + " covered " + cover +
                                    " meanAbsCovered " + (cover > 0 ? abs / cover : 0).ToString("0.000", Inv) + " of " + original.Length);
                    if (saved < 6)
                    {
                        var shot = new Texture2D(target.width, target.height, TextureFormat.RGBA32, false);
                        foreach (var picture in new[] { new KeyValuePair<string, Color32[]>("original", original),
                                                        new KeyValuePair<string, Color32[]>("pages", pages), new KeyValuePair<string, Color32[]>("none", none) })
                        {
                            shot.SetPixels32(picture.Value);
                            WriteRaw(System.IO.Path.Combine(outDir, "effects-" + saved + "-" + picture.Key + ".raw"), shot);
                        }
                        UnityEngine.Object.Destroy(shot);
                        saved++;
                    }
                }
                samples++;
                yield return null;
                yield return null;
            }
            foreach (Texture2D read in reads.Values) UnityEngine.Object.Destroy(read);
            string summary = "check-effects samples " + samples + " renders " + renders + " differing " + differing + " covered " + covered + " of " + total +
                             " meanAbsCovered " + (covered > 0 ? absSum / covered : 0).ToString("0.000", Inv);
            text.AppendLine(summary);
            File.WriteAllText(System.IO.Path.Combine(outDir, "check-effects.txt"), text.ToString());
            Event(summary);
        }

        // -diag-global "<name>=<value>[,...]" sets global shader floats at start (they last for
        // the run), e.g. _DrifterNoAlphaCull=1 to draw PropLightMasked's transparent pixels again.
        // -diag-check-global "<camera>:<name>=<value>": at dump time, 8 times a quarter second
        // apart, renders the camera (by name prefix) into its target with the global at its
        // current value and at <value>, reads both back and counts differing pixels
        // (check-global.txt), with each render's time between GPU syncs. "moved" counts pixels
        // that changed since the previous sample, to show the scene was not static.
        // <camera> may list several cameras joined by '+' (rendered in that order each time, the
        // last one read back: "PixelCamera+QuestCamera" for the room picture at screen size), and
        // <name> may be keyword:<KEYWORD> (a global shader keyword, value 0 off, 1 on).
        static string checkGlobal;

        static void SetGlobals(string list)
        {
            foreach (string pair in list.Split(','))
            {
                int eq = pair.IndexOf('=');
                if (eq <= 0) continue;
                float value = Float(pair.Substring(eq + 1));
                SetGlobal(pair.Substring(0, eq), value);
                Event("global " + pair.Substring(0, eq) + " = " + value.ToString(Inv));
            }
        }

        static System.Collections.IEnumerator CheckGlobal()
        {
            var text = new StringBuilder();
            int colon = checkGlobal.IndexOf(':'), eq = checkGlobal.IndexOf('=');
            string prefix = checkGlobal.Substring(0, colon), name = checkGlobal.Substring(colon + 1, eq - colon - 1);
            float value = Float(checkGlobal.Substring(eq + 1));
            var cameras = new List<Camera>();
            foreach (string part in prefix.Split('+'))
            {
                Camera found = Camera.allCameras.FirstOrDefault(c => c.name.StartsWith(part));
                if (found == null)
                {
                    Event("check-global: no camera named " + part);
                    yield break;
                }
                cameras.Add(found);
            }
            Camera camera = cameras[cameras.Count - 1];
            // A camera drawing to the screen renders into a screen-sized texture for the check.
            RenderTexture target = camera.targetTexture, temporary = null;
            if (target == null)
            {
                temporary = new RenderTexture(Screen.width, Screen.height, 24, RenderTextureFormat.ARGB32);
                target = temporary;
            }
            var read = new Texture2D(target.width, target.height, TextureFormat.RGBA32, false);
            Color32[] previous = null;
            long total = 0, differing = 0;
            float msCurrent = 0, msValue = 0;
            const int samples = 8;
            for (int sample = 0; sample < samples; sample++)
            {
                yield return new WaitForEndOfFrame();
                float current = GetGlobal(name), withValue, withCurrent;
                SetGlobal(name, value);
                Color32[] a = RenderAndRead(cameras, temporary, target, read, out withValue);
                SetGlobal(name, current);
                Color32[] b = RenderAndRead(cameras, temporary, target, read, out withCurrent);
                int count = Differing(a, b), moved = previous != null ? Differing(b, previous) : 0;
                if (sample == 0)
                {
                    // Both pictures of the first sample (RGBA32 rows bottom-up) and the differences'
                    // sizes, for working out where they come from.
                    File.WriteAllBytes(System.IO.Path.Combine(outDir, "check-global-current.raw"), Raw(b));
                    File.WriteAllBytes(System.IO.Path.Combine(outDir, "check-global-value.raw"), Raw(a));
                    File.WriteAllText(System.IO.Path.Combine(outDir, "check-global-size.txt"), target.width + " " + target.height);
                }
                var sizes = new int[5];
                for (int k = 0; k < a.Length; k++)
                {
                    int d = Math.Max(Math.Max(Math.Abs(a[k].r - b[k].r), Math.Abs(a[k].g - b[k].g)), Math.Max(Math.Abs(a[k].b - b[k].b), Math.Abs(a[k].a - b[k].a)));
                    if (d > 0) sizes[d == 1 ? 0 : d == 2 ? 1 : d <= 4 ? 2 : d <= 16 ? 3 : 4]++;
                }
                text.AppendLine("  differing by 1: " + sizes[0] + ", 2: " + sizes[1] + ", 3-4: " + sizes[2] + ", 5-16: " + sizes[3] + ", more: " + sizes[4]);
                previous = b;
                total += b.Length;
                differing += count;
                msCurrent += withCurrent;
                msValue += withValue;
                text.AppendLine("sample " + sample + " " + name + " " + current.ToString(Inv) + ": " + withCurrent.ToString("0.00", Inv) + " ms, " +
                                value.ToString(Inv) + ": " + withValue.ToString("0.00", Inv) + " ms, differing " + count + " of " + b.Length + " moved " + moved);
                yield return new WaitForSecondsRealtime(0.25f); // realtime: game time can stop (LabReactor on the PS Vita)
            }
            UnityEngine.Object.Destroy(read);
            if (temporary != null) UnityEngine.Object.Destroy(temporary);
            string summary = "check-global " + prefix + " " + name + " differing " + differing + " of " + total + " ms " +
                             (msCurrent / samples).ToString("0.00", Inv) + " current, " + (msValue / samples).ToString("0.00", Inv) + " at " + value.ToString(Inv);
            text.AppendLine(summary);
            File.WriteAllText(System.IO.Path.Combine(outDir, "check-global.txt"), text.ToString());
            Event(summary);
        }

        // Renders the cameras in turn (the last one into temporary, when there is one) between
        // two GPU syncs, and reads target back; ms is the time the renders took.
        static Color32[] RenderAndRead(List<Camera> cameras, RenderTexture temporary, RenderTexture target, Texture2D read, out float ms)
        {
            Camera last = cameras[cameras.Count - 1];
            GpuSync();
            float start = Time.realtimeSinceStartup;
            foreach (Camera each in cameras) RenderInto(each, each == last ? temporary : null);
            GpuSync();
            ms = (Time.realtimeSinceStartup - start) * 1000f;
            return ReadTarget(target, read);
        }

        // Waits for the GPU: reading a pixel back cannot finish before the work queued before it.
        static RenderTexture syncTarget;
        static Texture2D syncRead;

        static void GpuSync()
        {
            if (syncTarget == null)
            {
                syncTarget = new RenderTexture(1, 1, 0);
                syncRead = new Texture2D(1, 1, TextureFormat.RGBA32, false);
            }
            RenderTexture previous = RenderTexture.active;
            RenderTexture.active = syncTarget;
            syncRead.ReadPixels(new Rect(0, 0, 1, 1), 0, 0);
            RenderTexture.active = previous;
        }

        // "maximumLOD" stands for Shader.globalMaximumLOD: 100 skips the PS Vita SubShaders the
        // pipeline adds with LOD 200 (shader_rules.py psp2_frag_color, psp2_alpha_discard).
        static float GetGlobal(string name)
        {
            if (name.StartsWith("keyword:")) return Shader.IsKeywordEnabled(name.Substring(8)) ? 1f : 0f;
            return name == "maximumLOD" ? Shader.globalMaximumLOD : Shader.GetGlobalFloat(name);
        }

        static void SetGlobal(string name, float value)
        {
            // The default is int.MaxValue, which a float round trip would overflow.
            if (name.StartsWith("keyword:"))
            {
                if (value != 0f) Shader.EnableKeyword(name.Substring(8));
                else Shader.DisableKeyword(name.Substring(8));
            }
            else if (name == "maximumLOD") Shader.globalMaximumLOD = value >= 2e9f ? int.MaxValue : (int)value;
            else Shader.SetGlobalFloat(name, value);
        }

        static void RenderInto(Camera camera, RenderTexture temporary)
        {
            if (temporary == null) { camera.Render(); return; }
            camera.targetTexture = temporary;
            camera.Render();
            camera.targetTexture = null;
        }

        // -diag-check-blend "<shader>": at dump time, draws a 256x256 quad with a material of that
        // shader (white, _Contrast 1, _Tint (1,1,1,0)) over a known picture into a texture, through
        // the shader's first SubShader and with Shader.globalMaximumLOD 100 (the original, for the
        // pipeline's PS Vita SubShaders), for _Color alpha 1, 0.8, 0.6 and 0.9. The picture under it
        // (blend-dst.raw) has (x, y, (7x + 3y) & 255, 255) at texel (x, y); the sprite texture
        // (blend-src.raw) (y, x, (x + y) & 255, 255). Writes blend-<alpha>-<current|lod100>.raw
        // (RGBA32 rows bottom-up) and the differing pixel counts, for working out how the GPU
        // converts and blends what the original's program outputs.
        static string checkBlend;

        static System.Collections.IEnumerator CheckBlend()
        {
            const int size = 256, layer = 31;
            Shader shader = Shader.Find(checkBlend);
            if (shader == null) { Event("check-blend: no shader " + checkBlend); yield break; }
            yield return new WaitForEndOfFrame();
            var dst = new Texture2D(size, size, TextureFormat.RGBA32, false, true);
            var src = new Texture2D(size, size, TextureFormat.RGBA32, false, true);
            dst.filterMode = src.filterMode = FilterMode.Point;
            dst.wrapMode = src.wrapMode = TextureWrapMode.Clamp;
            var d = new Color32[size * size];
            var t = new Color32[size * size];
            for (int y = 0; y < size; y++)
                for (int x = 0; x < size; x++)
                {
                    d[y * size + x] = new Color32((byte)x, (byte)y, (byte)((7 * x + 3 * y) & 255), 255);
                    t[y * size + x] = new Color32((byte)y, (byte)x, (byte)((x + y) & 255), 255);
                }
            Upload(dst, d);
            Upload(src, t);
            File.WriteAllBytes(System.IO.Path.Combine(outDir, "blend-dst.raw"), Raw(d));
            File.WriteAllBytes(System.IO.Path.Combine(outDir, "blend-src.raw"), Raw(t));
            var material = new Material(shader);
            material.mainTexture = src;
            material.SetFloat("_Contrast", 1f);
            material.SetColor("_Tint", new Color(1, 1, 1, 0));
            material.SetVector("_Offset", Vector4.zero);
            var mesh = new Mesh();
            mesh.vertices = new[] { new Vector3(-0.5f, -0.5f, 0), new Vector3(0.5f, -0.5f, 0), new Vector3(-0.5f, 0.5f, 0), new Vector3(0.5f, 0.5f, 0) };
            mesh.uv = new[] { new Vector2(0, 0), new Vector2(1, 0), new Vector2(0, 1), new Vector2(1, 1) };
            mesh.colors32 = new[] { new Color32(255, 255, 255, 255), new Color32(255, 255, 255, 255), new Color32(255, 255, 255, 255), new Color32(255, 255, 255, 255) };
            mesh.triangles = new[] { 0, 2, 1, 2, 3, 1 };
            var quad = new GameObject("diag-blend-quad");
            quad.layer = layer;
            quad.AddComponent<MeshFilter>().sharedMesh = mesh;
            quad.AddComponent<MeshRenderer>().sharedMaterial = material;
            var cameraObject = new GameObject("diag-blend-camera");
            cameraObject.transform.position = new Vector3(0, 0, -1);
            Camera camera = cameraObject.AddComponent<Camera>();
            camera.enabled = false;
            camera.orthographic = true;
            camera.orthographicSize = 0.5f;
            camera.aspect = 1f;
            camera.clearFlags = CameraClearFlags.Nothing;
            camera.cullingMask = 1 << layer;
            camera.nearClipPlane = 0.1f;
            camera.farClipPlane = 10f;
            var target = new RenderTexture(size, size, 0, RenderTextureFormat.ARGB32, RenderTextureReadWrite.Linear);
            target.filterMode = FilterMode.Point;
            camera.targetTexture = target;
            var read = new Texture2D(size, size, TextureFormat.RGBA32, false, true);
            var text = new StringBuilder();
            int lod = Shader.globalMaximumLOD;
            // The last round (alpha 0.9) also varies the sprite's alpha, (7x + 13y) & 255, and the
            // alpha under it, (5x + 11y) & 255.
            foreach (float alpha in new[] { 1f, 0.8f, 0.6f, 0.9f })
            {
                if (alpha == 0.9f)
                {
                    for (int y = 0; y < size; y++)
                        for (int x = 0; x < size; x++)
                        {
                            d[y * size + x].a = (byte)((5 * x + 11 * y) & 255);
                            t[y * size + x].a = (byte)((7 * x + 13 * y) & 255);
                        }
                    Upload(dst, d);
                    Upload(src, t);
                }
                material.SetColor("_Color", new Color(1, 1, 1, alpha));
                Color32[] current = null;
                foreach (bool original in new[] { false, true })
                {
                    Shader.globalMaximumLOD = original ? 100 : lod;
                    Graphics.Blit(dst, target);
                    camera.Render();
                    Color32[] result = ReadTarget(target, read);
                    File.WriteAllBytes(System.IO.Path.Combine(outDir, "blend-" + alpha.ToString("0.0", Inv) +
                                                               (original ? "-lod100" : "-current") + ".raw"), Raw(result));
                    if (original) text.AppendLine("alpha " + alpha.ToString(Inv) + " differing " + Differing(current, result) + " of " + result.Length);
                    else current = result;
                }
            }
            Shader.globalMaximumLOD = lod;
            UnityEngine.Object.Destroy(quad);
            UnityEngine.Object.Destroy(cameraObject);
            UnityEngine.Object.Destroy(target);
            UnityEngine.Object.Destroy(read);
            UnityEngine.Object.Destroy(material);
            UnityEngine.Object.Destroy(mesh);
            UnityEngine.Object.Destroy(dst);
            UnityEngine.Object.Destroy(src);
            File.WriteAllText(System.IO.Path.Combine(outDir, "check-blend.txt"), text.ToString());
            Event("check-blend " + checkBlend + ": " + text.ToString().Replace(System.Environment.NewLine, "; "));
        }

        static void Upload(Texture2D texture, Color32[] pixels)
        {
            texture.SetPixels32(pixels);
            texture.Apply();
        }

        static byte[] Raw(Color32[] pixels)
        {
            var bytes = new byte[pixels.Length * 4];
            for (int k = 0; k < pixels.Length; k++)
            {
                bytes[k * 4] = pixels[k].r;
                bytes[k * 4 + 1] = pixels[k].g;
                bytes[k * 4 + 2] = pixels[k].b;
                bytes[k * 4 + 3] = pixels[k].a;
            }
            return bytes;
        }

        static Color32[] ReadTarget(RenderTexture target, Texture2D read)
        {
            RenderTexture previous = RenderTexture.active;
            RenderTexture.active = target;
            read.ReadPixels(new Rect(0, 0, target.width, target.height), 0, 0);
            RenderTexture.active = previous;
            return read.GetPixels32();
        }

        // -diag-dump-rt <name> writes the render texture's RGBA32 contents to rt-<name>.raw
        // with its size in rt-<name>.txt.
        static void DumpTargets()
        {
            foreach (string name in dumpTargets)
            {
                RenderTexture texture = Resources.FindObjectsOfTypeAll<RenderTexture>().FirstOrDefault(t => t.name == name);
                if (texture == null) { File.WriteAllText(System.IO.Path.Combine(outDir, "rt-" + name + ".txt"), "missing"); continue; }
                var read = new Texture2D(texture.width, texture.height, TextureFormat.RGBA32, false);
                ReadTarget(texture, read);
                File.WriteAllBytes(System.IO.Path.Combine(outDir, "rt-" + name + ".raw"), read.GetRawTextureData());
                UnityEngine.Object.Destroy(read);
                File.WriteAllText(System.IO.Path.Combine(outDir, "rt-" + name + ".txt"), texture.width + " " + texture.height);
            }
        }
    }
}
