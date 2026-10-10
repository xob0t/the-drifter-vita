using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using UnityEngine;

// A run: Start (ReadFlags reads the flags) installs the hooks, OnPreCull drives each frame (room
// changes, drivers, the dump at -diag-dump-after, done.txt), and the dump itself (DumpAll, Dump,
// FindRefs).
// Part of the parity probe (DrifterDiag); see Diag.cs.
namespace DrifterDiag
{
    public static partial class Probe
    {
        public static void Start()
        {
            string[] args = RemoteArgs(Environment.GetCommandLineArgs());
            // Compat's own flags (-crt-*, -compat-*) come from the command line, which the PS
            // Vita does not have; give it the probe's.
            Type compatPlatform = FindType("DrifterCompat.Platform");
            System.Reflection.FieldInfo probeArgs = compatPlatform == null ? null : compatPlatform.GetField("ProbeArgs");
            if (probeArgs != null) probeArgs.SetValue(null, args);
            ReadFlags(args);
            if (LoopTimer.Enabled) LoopTimer.Install();
            CountExceptions();
            Directory.CreateDirectory(outDir);
            SceneEvents();
            Event("loading settings backgroundLoadingPriority " + Application.backgroundLoadingPriority +
                  " asyncUploadTimeSlice " + QualitySettings.asyncUploadTimeSlice + " asyncUploadBufferSize " + QualitySettings.asyncUploadBufferSize +
                  " targetFrameRate " + Application.targetFrameRate + " vSyncCount " + QualitySettings.vSyncCount +
                  " maxQueuedFrames " + QualitySettings.maxQueuedFrames + " graphicsMultiThreaded " + SystemInfo.graphicsMultiThreaded +
                  " graphics " + SystemInfo.graphicsDeviceType + " " + SystemInfo.graphicsDeviceVersion);
            foreach (string globals in Values(args, "-diag-global")) SetGlobals(globals);
            // The game pauses when its window loses focus; diagnostic runs must not.
            Application.runInBackground = true;
            Camera.onPreCull += AllocTimer.Enabled ? (Camera.CameraCallback)MeasuredPreCull : OnPreCull;
            if (remoteDir != null)
            {
                // Remote runs have no one at the controls; ask the system not to sleep.
                Screen.sleepTimeout = SleepTimeout.NeverSleep;
                cameraTiming = true;
                Camera.onPreCull += CameraPreCull;
                Camera.onPostRender += CameraPostRender;
            }
        }

        // The probe's flags into its state; each is described where it is used.
        static void ReadFlags(string[] args)
        {
            crtSetting = Value(args, "-diag-crt");
            outDir = Value(args, "-diag-out") ?? outDir;
            dumpAfter = FloatFlag(args, "-diag-dump-after", dumpAfter);
            disable = Values(args, "-diag-disable");
            hideShaders = Values(args, "-diag-hide-shader");
            assignments = Values(args, "-diag-set");
            loadSave = Value(args, "-diag-load-save");
            dumpProps = Values(args, "-diag-dump-props");
            dumpTargets = Values(args, "-diag-dump-rt");
            hidePaths = Values(args, "-diag-hide-path");
            gotoRoom = Value(args, "-diag-goto-room");
            wantsGoto = gotoRoom != null;
            findRefs = Value(args, "-diag-find-refs");
            gotoDelay = FloatFlag(args, "-diag-goto-delay", gotoDelay);
            // -diag-fast-intro <scale> runs the splash screens at that time scale until the title room.
            introScale = FloatFlag(args, "-diag-fast-intro", introScale);
            dumpSprites = args.Contains("-diag-dump-sprites"); // takes no value
            dumpTextures = args.Contains("-diag-dump-textures"); // takes no value
            logInput = args.Contains("-diag-log-input"); // takes no value
            skipIntro = args.Contains("-diag-skip-intro"); // takes no value
            int frameLogIndex = Array.IndexOf(args, "-diag-frame-log");
            if (frameLogIndex >= 0 && frameLogIndex + 2 < args.Length)
            {
                frameLogScene = args[frameLogIndex + 1];
                frameLogFrames = int.Parse(args[frameLogIndex + 2]);
                float delay;
                if (frameLogIndex + 3 < args.Length && float.TryParse(args[frameLogIndex + 3], System.Globalization.NumberStyles.Float, Inv, out delay))
                {
                    frameLogDelay = delay;
                    if (frameLogIndex + 4 < args.Length && !args[frameLogIndex + 4].StartsWith("-")) frameLogSave = args[frameLogIndex + 4];
                }
            }
            ScriptTimer.Enabled = args.Contains("-diag-time-scripts"); // takes no value
            ScriptTimer.CallsEnabled = args.Contains("-diag-time-calls"); // takes no value; needs -diag-time-scripts
            prepareMethods = args.Contains("-diag-prepare-methods"); // takes no value
            checkWalk = IntFlag(args, "-diag-check-walk", checkWalk);
            checkAllocs = IntFlag(args, "-diag-check-allocs", checkAllocs);
            checkPath = args.Contains("-diag-check-path"); // takes no value
            checkText = args.Contains("-diag-check-text"); // takes no value
            checkKeys = args.Contains("-diag-check-keys"); // takes no value
            checkAnimCull = args.Contains("-diag-check-anim-cull"); // takes no value
            checkWater = args.Contains("-diag-check-water"); // takes no value
            checkEffects = IntFlag(args, "-diag-check-effects", checkEffects);
            checkGlobal = Value(args, "-diag-check-global");
            checkBlend = Value(args, "-diag-check-blend");
            AllocTimer.Enabled = args.Contains("-diag-alloc-scripts"); // takes no value
            // Test players on PC run muted; -diag-sound keeps the sound. The mix still runs, so
            // audio timing is unaffected. The PS Vita keeps its sound for listening checks. Applied
            // every frame: the game sets AudioListener.volume from its master volume setting.
            mute = Application.platform != RuntimePlatform.PSP2 && !args.Contains("-diag-sound");
            stickWalkSeconds = FloatFlag(args, "-diag-stick-walk", stickWalkSeconds);
            string place = Value(args, "-diag-place");
            if (place != null) placeAt = place.Split(',').Select(v => Float(v)).ToArray();
            // -diag-time-room-change [ms]: see RoomTiming; implies -diag-time-loop and -diag-time-scripts.
            int roomTimingIndex = Array.IndexOf(args, "-diag-time-room-change");
            if (roomTimingIndex >= 0)
            {
                double roomTimingMs;
                if (roomTimingIndex + 1 >= args.Length ||
                    !double.TryParse(args[roomTimingIndex + 1], System.Globalization.NumberStyles.Float, Inv, out roomTimingMs)) roomTimingMs = 5;
                RoomTiming.Enable(roomTimingMs);
                ScriptTimer.Enabled = true;
            }
            LoopTimer.Enabled = args.Contains("-diag-time-loop") || roomTimingIndex >= 0; // takes no value
            forcedVSync = OptionalInt(args, "-diag-vsync");
            forcedTargetFps = OptionalInt(args, "-diag-target-fps");
        }

        // A flag's value (after its last occurrence; null without one), and all values of a repeatable flag.
        static string Value(string[] args, string flag)
        {
            string value = null;
            for (int i = 0; i < args.Length - 1; i++) if (args[i] == flag) value = args[i + 1];
            return value;
        }

        static string[] Values(string[] args, string flag)
        {
            return Enumerable.Range(0, Math.Max(0, args.Length - 1)).Where(i => args[i] == flag).Select(i => args[i + 1]).ToArray();
        }

        static float Float(string text)
        {
            return float.Parse(text, Inv);
        }

        // A flag's number, or otherwise without the flag.
        static float FloatFlag(string[] args, string flag, float otherwise)
        {
            string value = Value(args, flag);
            return value == null ? otherwise : Float(value);
        }

        static int IntFlag(string[] args, string flag, int otherwise)
        {
            string value = Value(args, flag);
            return value == null ? otherwise : int.Parse(value, Inv);
        }

        static int? OptionalInt(string[] args, string flag)
        {
            string value = Value(args, flag);
            return value == null ? (int?)null : int.Parse(value, Inv);
        }

        // -diag-skip-intro (test runs): the game's own skips, as soon as each part starts. The
        // logos (LoadingScreen.m_skip, which Return held sets once the title room has loaded;
        // set at once, they end as the title room finishes loading) and the title room's intro
        // cutscene (PowerQuest.SkipCutscene, as Return or Select do). Saves restore as before.
        static bool skipIntro, logosSkipped, titleSkipped;

        static void SkipIntro(string scene)
        {
            if (!skipIntro || titleSkipped) return;
            try
            {
                if (!logosSkipped && scene == "SceneLoading")
                {
                    Type loading = FindType("LoadingScreen");
                    UnityEngine.Object screen = loading == null ? null : UnityEngine.Object.FindObjectOfType(loading);
                    if (screen == null) return;
                    loading.GetField("m_skip", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic).SetValue(screen, true);
                    logosSkipped = true;
                    Event("intro: logos skipped");
                }
                else if (scene == "SceneRoomTitle")
                {
                    UnityEngine.Object quest = FindQuest();
                    if (quest == null) return;
                    // The intro's cutscene starts after the room's first fade; until then this
                    // returns false. Give up after 20 s (an intro that has already ended).
                    bool late = titleAt >= 0 && Time.realtimeSinceStartup > titleAt + 20f;
                    if ((bool)QuestType().GetMethod("SkipCutscene", Type.EmptyTypes).Invoke(quest, null) || late)
                    {
                        titleSkipped = true;
                        Event("intro: title " + (late ? "not skipped" : "skipped"));
                    }
                }
            }
            catch (Exception e)
            {
                if (scene == "SceneLoading") logosSkipped = true;
                else titleSkipped = true;
                Event("intro skip failed: " + e.GetType().Name);
            }
        }

        // -diag-frame-log <scene> <frames> [delay seconds [all|outliers|<n>]]: from <delay> s after
        // <scene> becomes active, every frame for <frames> frames: the screen's mean luminance
        // (0-255), the mean absolute luminance difference from the previous frame, the largest
        // such difference of a 4x4 grid of regions and the 16 region means, in
        // framelog-<scene>.txt. Frames are saved as framelog-<scene>-<n>.raw (width, height as
        // int32, RGBA rows bottom-up): all, every n-th, or (the default) outliers with their
        // previous frame (a region OutlierRegionDiff levels or the whole frame OutlierFrameDiff
        // levels from the previous frame). framelog-<scene>-changes.txt lists what changes
        // discretely per frame: renderers turning on or off, material, shader, sorting order and
        // sprite swaps, colour steps, cameras turning on or off or changing target, and Compat's
        // counters (Crt.ScreenClears, WaterMasks.MaskDraws and its copies and masks tables).
        // Written to -diag-out (or the remote folder on the PS Vita).
        static string frameLogScene, frameLogSave = "outliers";
        static int frameLogFrames, frameLogged = -1;
        static float frameLogDelay;
        const int FrameLogGrid = 4;
        const float OutlierRegionDiff = 12f, OutlierFrameDiff = 4f;

        static void FrameLogStart(string scene)
        {
            if (frameLogScene == null || frameLogged >= 0 || scene != frameLogScene) return;
            MonoBehaviour host = AnyActiveBehaviour();
            if (host == null) return;
            frameLogged = 0;
            host.StartCoroutine(FrameLog());
        }

        static string RendererState(Renderer r)
        {
            Material m = r.sharedMaterial;
            string state = (r.enabled && r.gameObject.activeInHierarchy ? "on" : "off") + " mat=" +
                (m == null ? "null" : m.name + "#" + m.GetInstanceID() + " shader=" + (m.shader == null ? "null" : m.shader.name)) +
                " order=" + r.sortingOrder + " layer=" + r.gameObject.layer;
            var sprite = r as SpriteRenderer;
            if (sprite != null)
            {
                Color c = sprite.color;
                // Colour in steps of 1/8: smooth fades show as a few steps, flicker as jumps.
                state += " color=" + Mathf.RoundToInt(c.r * 8) + "," + Mathf.RoundToInt(c.g * 8) + "," + Mathf.RoundToInt(c.b * 8) + "," + Mathf.RoundToInt(c.a * 8);
            }
            return state;
        }

        static int CompatCounter(string type, string field)
        {
            Type t = FindType("DrifterCompat." + type);
            if (t == null) return -1;
            var f = t.GetField(field, AnyStatic);
            if (f == null) return -1;
            object value = f.GetValue(null);
            if (value is int) return (int)value;
            var collection = value as System.Collections.ICollection;
            return collection != null ? collection.Count : -1;
        }

        static void WriteRaw(string path, Texture2D shot)
        {
            byte[] pixels = shot.GetRawTextureData();
            using (var stream = new FileStream(path, FileMode.Create))
            {
                stream.Write(BitConverter.GetBytes(shot.width), 0, 4);
                stream.Write(BitConverter.GetBytes(shot.height), 0, 4);
                stream.Write(pixels, 0, pixels.Length);
            }
        }

        // What the frame log compares from one frame to the next.
        sealed class FrameState
        {
            public readonly List<Renderer> Renderers = new List<Renderer>();
            // By instance id: each renderer's and camera's state, each shown sprite renderer's sprite.
            public readonly Dictionary<int, string> RendererStates = new Dictionary<int, string>(), Sprites = new Dictionary<int, string>(),
                CameraStates = new Dictionary<int, string>();
            public string Counters;
        }

        // Frame n's discrete changes since the last frame, a line each ("" without any); the
        // first frame (n 0) only records the starting states.
        static string FrameChanges(FrameState known, int n)
        {
            var changes = new StringBuilder();
            if (n % 30 == 0)
            {
                known.Renderers.Clear();
                foreach (Renderer r in Resources.FindObjectsOfTypeAll<Renderer>())
                    if (r.gameObject.scene.IsValid()) known.Renderers.Add(r);
            }
            foreach (Renderer r in known.Renderers)
            {
                if (r == null) continue;
                int id = r.GetInstanceID();
                string state = RendererState(r), had;
                if (!known.RendererStates.TryGetValue(id, out had) || had != state)
                {
                    if (had != null || n > 0) changes.Append("  renderer " + Path(r.transform) + ": " + (had ?? "new") + " -> " + state + "\n");
                    known.RendererStates[id] = state;
                }
                var spriteRenderer = r as SpriteRenderer;
                if (spriteRenderer != null && spriteRenderer.enabled && r.gameObject.activeInHierarchy)
                {
                    string sprite = spriteRenderer.sprite == null ? "null" : spriteRenderer.sprite.name;
                    if (known.Sprites.TryGetValue(id, out had) && had != sprite)
                        changes.Append("  sprite " + Path(r.transform) + ": " + had + " -> " + sprite + "\n");
                    known.Sprites[id] = sprite;
                }
            }
            foreach (Camera c in Resources.FindObjectsOfTypeAll<Camera>())
            {
                if (!c.gameObject.scene.IsValid()) continue;
                string state = (c.enabled && c.gameObject.activeInHierarchy ? "on" : "off") + " target=" +
                    (c.targetTexture == null ? "screen" : c.targetTexture.name) + " depth=" + c.depth.ToString(Inv) +
                    " clear=" + c.clearFlags + " mask=" + c.cullingMask, had;
                int id = c.GetInstanceID();
                if (!known.CameraStates.TryGetValue(id, out had) || had != state)
                {
                    if (had != null || n > 0) changes.Append("  camera " + Path(c.transform) + ": " + (had ?? "new") + " -> " + state + "\n");
                    known.CameraStates[id] = state;
                }
            }
            string counters = "crtClears=" + CompatCounter("Crt", "ScreenClears") + " maskDraws=" + CompatCounter("WaterMasks", "MaskDraws") +
                " copies=" + CompatCounter("WaterMasks", "copies") + " masks=" + CompatCounter("WaterMasks", "masks");
            if (counters != known.Counters) { changes.Append("  compat " + counters + "\n"); known.Counters = counters; }
            return changes.ToString();
        }

        // A screenshot's levels: the luminance of every second pixel of every second row, their
        // mean, the means of a FrameLogGrid x FrameLogGrid grid of regions (bottom row first), and
        // the mean absolute difference from the previous frame's luminance, whole and the largest
        // of a region's (0 without a previous frame).
        sealed class FrameLevels
        {
            public float[] Luminance;
            public double Mean;
            public float[] RegionMeans;
            public float MeanDiff, MaxRegionDiff;
        }

        static FrameLevels MeasureFrame(Texture2D shot, float[] previous)
        {
            Color32[] pixels = shot.GetPixels32();
            int w = shot.width, h = shot.height;
            const int regions = FrameLogGrid * FrameLogGrid;
            var sums = new float[regions];
            var counts = new int[regions];
            var regionDiff = new float[regions];
            var levels = new FrameLevels { Luminance = new float[(w / 2) * (h / 2)], RegionMeans = new float[regions] };
            if (previous != null && previous.Length != levels.Luminance.Length) previous = null;
            double total = 0, diff = 0;
            for (int y = 0; y + 1 < h; y += 2)
                for (int x = 0; x + 1 < w; x += 2)
                {
                    Color32 p = pixels[y * w + x];
                    float l = 0.299f * p.r + 0.587f * p.g + 0.114f * p.b;
                    int region = (y * FrameLogGrid / h) * FrameLogGrid + x * FrameLogGrid / w;
                    int k = (y / 2) * (w / 2) + x / 2;
                    levels.Luminance[k] = l;
                    sums[region] += l;
                    counts[region]++;
                    total += l;
                    if (previous == null) continue;
                    float d = Math.Abs(l - previous[k]);
                    regionDiff[region] += d;
                    diff += d;
                }
            int samples = levels.Luminance.Length;
            for (int r = 0; r < regions; r++)
            {
                if (counts[r] == 0) continue;
                levels.RegionMeans[r] = sums[r] / counts[r];
                levels.MaxRegionDiff = Math.Max(levels.MaxRegionDiff, regionDiff[r] / counts[r]);
            }
            levels.Mean = total / samples;
            levels.MeanDiff = previous != null ? (float)(diff / samples) : 0f;
            return levels;
        }

        static System.Collections.IEnumerator FrameLog()
        {
            string folder = remoteDir ?? outDir;
            float start = Time.realtimeSinceStartup;
            while (Time.realtimeSinceStartup < start + frameLogDelay) yield return null;
            string stem = System.IO.Path.Combine(folder, "framelog-" + frameLogScene);
            int every = 0;
            int.TryParse(frameLogSave, out every);
            File.WriteAllText(stem + ".txt", "# n frame realtime levelTime mean diff maxRegionDiff regions(" + FrameLogGrid + "x" + FrameLogGrid + ", bottom row first)\n");
            File.WriteAllText(stem + "-changes.txt", "");
            float[] previous = null;
            Texture2D previousShot = null;
            int previousSaved = -1;
            var known = new FrameState();
            for (frameLogged = 0; frameLogged < frameLogFrames; frameLogged++)
            {
                yield return new WaitForEndOfFrame();
                int n = frameLogged;
                try
                {
                    string changes = FrameChanges(known, n);
                    if (changes.Length > 0)
                        File.AppendAllText(stem + "-changes.txt", "n " + n + " frame " + Time.frameCount + " levelTime " + Time.timeSinceLevelLoad.ToString("0.000", Inv) + "\n" + changes);
                }
                catch (Exception e) { File.AppendAllText(stem + "-changes.txt", "n " + n + " state failed: " + e + "\n"); }
                Texture2D shot = null;
                try
                {
                    shot = ScreenCapture.CaptureScreenshotAsTexture();
                    FrameLevels levels = MeasureFrame(shot, previous);
                    File.AppendAllText(stem + ".txt", n + " " + Time.frameCount + " " + Time.realtimeSinceStartup.ToString("0.000", Inv) + " " +
                        Time.timeSinceLevelLoad.ToString("0.000", Inv) + " " + levels.Mean.ToString("0.00", Inv) + " " +
                        levels.MeanDiff.ToString("0.00", Inv) + " " + levels.MaxRegionDiff.ToString("0.00", Inv) +
                        string.Concat(levels.RegionMeans.Select(m => " " + m.ToString("0.0", Inv)).ToArray()) + "\n");
                    bool outlier = previous != null && (levels.MaxRegionDiff > OutlierRegionDiff || levels.MeanDiff > OutlierFrameDiff);
                    bool save = frameLogSave == "all" || (every > 0 && n % every == 0) || outlier;
                    if (outlier && previousShot != null && previousSaved != n - 1)
                        WriteRaw(stem + "-" + (n - 1) + ".raw", previousShot);
                    if (save)
                    {
                        WriteRaw(stem + "-" + n + ".raw", shot);
                        previousSaved = n;
                    }
                    previous = levels.Luminance;
                    if (previousShot != null) UnityEngine.Object.Destroy(previousShot);
                    previousShot = shot;
                    shot = null;
                }
                catch (Exception e)
                {
                    File.AppendAllText(stem + ".txt", "# " + n + " capture failed: " + e.GetType().Name + " " + e.Message + "\n");
                    if (shot != null) UnityEngine.Object.Destroy(shot);
                    yield break;
                }
            }
            if (previousShot != null) UnityEngine.Object.Destroy(previousShot);
            Event("frame log done " + frameLogFrames);
        }

        // Times are measured from game state, not process start: the title room becoming
        // active, then the save being restored. -diag-dump-after counts from the later.
        static float titleAt = -1, loadedAt = -1;
        static int doneFrame = -1;

        // With -diag-alloc-scripts, the probe's own per-frame work is measured too ("probe.frame":
        // its scene name read, transition fields read through reflection, heartbeats), so it can
        // be told apart from the game's in gcAllocB.
        // Above the ids InjectDiag gives the timed methods (diag.py: 0, 3000, 6000 and 9000 upwards,
        // one range per assembly); InjectDiag fails if they reach it.
        const int ProbeFrameId = 12000;

        static void MeasuredPreCull(Camera camera)
        {
            AllocTimer.Begin(ProbeFrameId, "probe.frame");
            try { OnPreCull(camera); }
            finally { AllocTimer.End(ProbeFrameId); }
        }

        static void OnPreCull(Camera camera)
        {
            if (Time.frameCount == lastFrame) return;
            lastFrame = Time.frameCount;
            string scene = UnityEngine.SceneManagement.SceneManager.GetActiveScene().name;
            if (scene != lastScene)
            {
                lastScene = scene;
                RemoteLine("scene " + scene);
            }
            RemoteFrame(scene);
            TrackTransition();
            StickWalkDriver();
            if (checkKeys) CheckKeys();
            if (checkAnimCull) CheckAnimCull();
            if (mute) AudioListener.volume = 0f;
            SkipIntro(scene);
            FrameLogStart(scene);
            if (titleAt < 0 && scene == "SceneRoomTitle")
            {
                titleAt = Time.realtimeSinceStartup;
                if (introScale != 1f) Time.timeScale = 1f;
            }
            else if (titleAt < 0 && introScale != 1f)
                Time.timeScale = introScale;
            ApplyForcedRates();
            ApplyCrtSetting();
            Apply();
            float anchor = wantsGoto ? gotoAt : loadSave != null ? loadedAt : titleAt;
            if (!dumped && dumpAfter >= 0 && anchor >= 0 && Time.realtimeSinceStartup >= anchor + dumpAfter)
            {
                dumped = true;
                DumpAll();
                doneFrame = Time.frameCount + 5;
            }
            // done.txt tells diag.py the run's files are complete, so on PC it also waits (up to 120
            // frames) for frame.png, which CaptureScreenshot writes at the end of a later frame. Vita runs
            // fetch a screenshot of their own (vita.py), so there done.txt does not wait for it.
            if (doneFrame >= 0 && Time.frameCount >= doneFrame && !experimentsRunning &&
                (Application.platform == RuntimePlatform.PSP2 || Time.frameCount >= doneFrame + 120 ||
                 File.Exists(System.IO.Path.Combine(outDir, "frame.png"))))
            {
                doneFrame = -1;
                HoldEvents(false);
                File.WriteAllText(System.IO.Path.Combine(outDir, "done.txt"), "ok");
            }
        }

        // The dump and the dump-time checks. A step that throws is logged ("<step> failed: ...")
        // and the others still run, so the run still ends with done.txt.
        static void DumpAll()
        {
            DumpStep("scan", Scan);  // the disables and hides once more, right before the dump
            DumpStep("dump", Dump);
            DumpStep("dump-sprites", DumpSprites);
            DumpStep("dump-textures", () => DumpTextures("textures.tsv"));
            if (prepareMethods) DumpStep("prepare-methods", PrepareMethods);
            if (checkAllocs > 0) DumpStep("check-allocs", () => CheckAllocs(checkAllocs));
            if (checkWalk > 0) DumpStep("check-walk", () => CheckWalk(checkWalk));
            if (checkPath) DumpStep("check-path", CheckPath);
            if (findRefs != null) DumpStep("find-refs", () => FindRefs(findRefs));
            if (checkText) DumpStep("check-text", CheckText);
            if (checkKeys) Event("check-keys frames " + keyFrames + " reads " + keyReads + " pressed " + keyPressed + " differ " + keyDiffer);
            if (checkAnimCull) Event("check-anim-cull samples " + animCullSamples + " checked " + animCullChecked + " differ " + animCullDiffer);
            if (checkWater || checkGlobal != null || checkBlend != null || checkEffects > 0)
            {
                MonoBehaviour host = AnyActiveBehaviour();
                if (host != null) { experimentsRunning = true; host.StartCoroutine(Experiments(host)); }
                else Event("experiments: no active behaviour to run on");
            }
            DumpStep("dump-rt", DumpTargets);
            DumpStep("frame.png", () => ScreenCapture.CaptureScreenshot(System.IO.Path.Combine(outDir, "frame.png")));
        }

        static void DumpStep(string name, Action step)
        {
            try { step(); }
            catch (Exception e) { Event(name + " failed: " + Failure(e)); }
        }

        // An exception's type and message; for one thrown through reflection, the original's.
        static string Failure(Exception e)
        {
            if (e is System.Reflection.TargetInvocationException && e.InnerException != null) e = e.InnerException;
            return e.GetType().Name + " " + e.Message;
        }

        // -diag-load-save "<slot>[@<seconds>]" restores a save through PowerQuest the given
        // time (default 3s) after the title room became active.
        static void LoadSave()
        {
            string[] spec = loadSave.Split('@');
            float delay = spec.Length > 1 ? Float(spec[1]) : 3f;
            if (titleAt < 0 || Time.realtimeSinceStartup < titleAt + delay) return;
            UnityEngine.Object quest = FindQuest();
            if (quest == null) return;
            loadedAt = Time.realtimeSinceStartup;
            Event("restore-call slot " + spec[0]);
            int before = exceptions, collections = GC.CollectionCount(0);
            object restored = QuestType().GetMethod("RestoreSave", new[] { typeof(int) }).Invoke(quest, new object[] { int.Parse(spec[0], Inv) });
            Event("restore-returned " + restored + " after " + ((Time.realtimeSinceStartup - loadedAt) * 1000).ToString("0", Inv) + "ms exceptions " + (exceptions - before) +
                  " collections " + (GC.CollectionCount(0) - collections) + " managedMB " + (GC.GetTotalMemory(false) / MB).ToString("0.0", Inv));
        }

        static int nextScanFrame;

        static void Apply()
        {
            if (loadSave != null && loadedAt < 0) LoadSave();
            if (gotoRoom != null) GotoRoom();
            // The scans walk every component; each frame they cost the PS Vita a whole CPU core,
            // which skews frame timings. Objects the game re-enables are caught at the next scan.
            if (Time.frameCount >= nextScanFrame) Scan();
        }

        // -diag-disable, -diag-hide-path, -diag-hide-shader and -diag-set, on what is live now.
        static void Scan()
        {
            nextScanFrame = Time.frameCount + 30;
            if (disable.Length > 0)
                foreach (Behaviour behaviour in UnityEngine.Object.FindObjectsOfType<Behaviour>())
                    if (behaviour.enabled && disable.Any(d => behaviour.GetType().FullName.Contains(d)))
                        behaviour.enabled = false;
            if (hidePaths.Length > 0)
                foreach (Renderer renderer in UnityEngine.Object.FindObjectsOfType<Renderer>())
                    if (renderer.enabled && hidePaths.Any(p => Path(renderer.transform).Contains(p)))
                        renderer.enabled = false;
            if (hideShaders.Length > 0)
                foreach (Renderer renderer in UnityEngine.Object.FindObjectsOfType<Renderer>())
                    if (renderer.enabled && renderer.sharedMaterial != null && renderer.sharedMaterial.shader != null &&
                        hideShaders.Contains(renderer.sharedMaterial.shader.name))
                        renderer.enabled = false;
            // -diag-set Type.field=value assigns a field on every live component of that type.
            foreach (string assignment in assignments)
            {
                string[] sides = assignment.Split(new[] { '=' }, 2);
                int dot = sides[0].LastIndexOf('.');
                string typeName = sides[0].Substring(0, dot), fieldName = sides[0].Substring(dot + 1), value = sides[1];
                foreach (Behaviour behaviour in UnityEngine.Object.FindObjectsOfType<Behaviour>())
                {
                    if (behaviour.GetType().Name != typeName && behaviour.GetType().FullName != typeName) continue;
                    for (Type type = behaviour.GetType(); type != null; type = type.BaseType)
                    {
                        var field = type.GetField(fieldName, AnyInstance);
                        if (field == null) continue;
                        object parsed = field.FieldType.IsEnum ? Enum.Parse(field.FieldType, value)
                            : Convert.ChangeType(value, field.FieldType, Inv);
                        field.SetValue(behaviour, parsed);
                        break;
                    }
                }
            }
        }

        // -diag-dump-after's diag-dump.txt: the cameras with their components' settings, the room
        // components' sprite lists, the active Animators and the renderers.
        static void Dump()
        {
            var text = new StringBuilder();
            text.AppendLine("unity " + Application.unityVersion + " screen " + Screen.width + "x" + Screen.height +
                            " colorSpace " + QualitySettings.activeColorSpace +
                            " scene " + UnityEngine.SceneManagement.SceneManager.GetActiveScene().name);
            DumpCameras(text);
            DumpRoomSprites(text);
            DumpAnimators(text);
            DumpRenderers(text);
            File.WriteAllText(System.IO.Path.Combine(outDir, "diag-dump.txt"), text.ToString());
        }

        static void DumpCameras(StringBuilder text)
        {
            foreach (Camera camera in Camera.allCameras.OrderBy(c => c.depth))
            {
                RenderTexture target = camera.targetTexture;
                text.AppendLine("camera " + Path(camera.transform) + " depth=" + camera.depth.ToString(Inv) + " ortho=" + camera.orthographic +
                                " size=" + camera.orthographicSize.ToString(Inv) + " mask=" + camera.cullingMask + " clear=" + camera.clearFlags +
                                " bg=" + Invariant(camera.backgroundColor) + " target=" + (target ? target.name + "(" +
                                    target.format + " srgb=" + target.sRGB + " " + target.width + "x" + target.height +
                                    " aa=" + target.antiAliasing + " depth=" + target.depth + ")" : "screen") +
                                " transparencySort=" + camera.transparencySortMode +
                                " hdr=" + camera.allowHDR);
                foreach (Behaviour behaviour in camera.GetComponents<Behaviour>())
                {
                    text.AppendLine("  component " + behaviour.GetType().FullName + " enabled=" + behaviour.enabled);
                    if (!(behaviour is MonoBehaviour)) continue;
                    for (Type type = behaviour.GetType(); type != null && type != typeof(MonoBehaviour); type = type.BaseType)
                        foreach (var field in type.GetFields(AnyInstance | System.Reflection.BindingFlags.DeclaredOnly))
                        {
                            Type ft = field.FieldType;
                            if (ft.IsPrimitive || ft.IsEnum || ft == typeof(Color) || ft == typeof(Vector2) || ft == typeof(Vector3) || ft == typeof(Vector4) || ft == typeof(string))
                                text.AppendLine("    " + type.Name + "." + field.Name + " = " + Invariant(field.GetValue(behaviour)));
                        }
                }
            }
        }

        // Sprite lists on room components (PowerQuest looks sprites up by name in them).
        static void DumpRoomSprites(StringBuilder text)
        {
            foreach (MonoBehaviour behaviour in UnityEngine.Object.FindObjectsOfType<MonoBehaviour>())
            {
                if (behaviour.GetType().Name != "RoomComponent") continue;
                var field = behaviour.GetType().GetField("m_sprites", AnyInstance);
                var list = field == null ? null : field.GetValue(behaviour) as List<Sprite>;
                if (list == null) { text.AppendLine("room " + behaviour.name + " m_sprites=null"); continue; }
                text.AppendLine("room " + behaviour.name + " m_sprites count=" + list.Count + " null=" + list.Count(s => s == null) +
                                " first=" + string.Join(",", list.Where(s => s != null).Take(5).Select(s => s.name + "/" + (s.texture ? s.texture.name : "notex")).ToArray()));
            }
        }

        // Active Animators: what the engine's animation step evaluates every frame.
        static void DumpAnimators(StringBuilder text)
        {
            foreach (Animator animator in UnityEngine.Object.FindObjectsOfType<Animator>().OrderBy(a => Path(a.transform)))
            {
                AnimatorClipInfo[] clips = animator.runtimeAnimatorController != null && animator.isInitialized ? animator.GetCurrentAnimatorClipInfo(0) : new AnimatorClipInfo[0];
                AnimationClip clip = clips.Length > 0 ? clips[0].clip : null;
                Renderer own = animator.GetComponent<Renderer>();
                Renderer[] under = animator.GetComponentsInChildren<Renderer>();
                text.AppendLine("animator " + Path(animator.transform) + " enabled=" + animator.enabled + " speed=" + animator.speed.ToString(Inv) +
                                " culling=" + animator.cullingMode + " rootMotion=" + animator.applyRootMotion +
                                " renderers=" + under.Count(r => r.enabled) + "/" + under.Length +
                                (own ? " own=" + (own.enabled ? "enabled" : "disabled") + (own.isVisible ? ",visible" : "") : "") +
                                " clip=" + (clip ? clip.name + " len=" + clip.length.ToString("0.###", Inv) +
                                    " fps=" + clip.frameRate.ToString(Inv) + " loop=" + clip.isLooping + " events=" + clip.events.Length +
                                    " t=" + animator.GetCurrentAnimatorStateInfo(0).normalizedTime.ToString("0.###", Inv) : "none"));
            }
        }

        static void DumpRenderers(StringBuilder text)
        {
            var renderers = UnityEngine.Object.FindObjectsOfType<Renderer>()
                .OrderBy(r => r.sortingLayerID).ThenBy(r => r.sortingOrder).ThenBy(r => Path(r.transform));
            foreach (Renderer renderer in renderers)
            {
                Material material = renderer.sharedMaterial;
                var sprite = renderer as SpriteRenderer;
                string keywords = material != null ? string.Join(" ", material.shaderKeywords) : "";
                Vector3 position = renderer.transform.position, scale = renderer.transform.lossyScale;
                text.AppendLine("renderer " + Path(renderer.transform) + " type=" + renderer.GetType().Name +
                                " state=" + (renderer.gameObject.activeInHierarchy ? "active" : "inactive") +
                                (renderer.enabled ? "" : ",disabled") + (renderer.isVisible ? ",visible" : "") +
                                " layer=" + renderer.sortingLayerName + " order=" + renderer.sortingOrder +
                                " pos=" + position.x.ToString("0.##", Inv) + "/" + position.y.ToString("0.##", Inv) +
                                " z=" + position.z.ToString("0.###", Inv) +
                                " scale=" + scale.x.ToString("0.##", Inv) + "/" + scale.y.ToString("0.##", Inv) +
                                " material=" + (material ? material.name : "null") +
                                " shader=" + (material && material.shader ? material.shader.name : "null") +
                                " keywords=[" + keywords + "]" +
                                (sprite ? " sprite=" + (sprite.sprite ? sprite.sprite.name : "null") + " color=" + Invariant(sprite.color) : ""));
                // -diag-dump-props "Shader:_A,_B" lists material properties for that shader's renderers.
                foreach (string spec in dumpProps)
                {
                    string[] parts = spec.Split(':');
                    if (material == null || material.shader == null || material.shader.name != parts[0]) continue;
                    var block = new MaterialPropertyBlock();
                    renderer.GetPropertyBlock(block);
                    Sprite shown = sprite ? sprite.sprite : null;
                    foreach (string property in parts[1].Split(','))
                    {
                        if (!material.HasProperty(property)) { text.AppendLine("    " + property + " missing"); continue; }
                        Texture texture = material.GetTexture(property);
                        Texture blockTexture = block.GetTexture(property);
                        text.AppendLine("    " + property + " float=" + material.GetFloat(property).ToString(Inv) +
                                        " vector=" + Invariant(material.GetVector(property), "F4") +
                                        " texture=" + (texture ? texture.name + " " + texture.width + "x" + texture.height : "none") +
                                        " block=" + (blockTexture ? blockTexture.name : "none") +
                                        (shown ? " spriteTexture=" + shown.texture.name + " " + shown.texture.width + "x" + shown.texture.height +
                                         " rect=" + Invariant(shown.textureRect) + " uv0=" + (shown.uv.Length > 0 ? Invariant(shown.uv[0], "F4") : "") +
                                         " packed=" + shown.packed + " rotation=" + (shown.packed ? shown.packingRotation.ToString() : "-") +
                                         " mesh=" + VertexMap(shown, int.MaxValue, "0.#") : ""));
                    }
                }
            }
        }

        // -diag-find-refs <text>: at dump time, what keeps the sprites of textures whose name
        // contains <text> (e.g. RoomDrowningAtlas) loaded: sprite renderers anywhere (scenes,
        // DontDestroyOnLoad, assets), and managed references found by walking the fields of every
        // loaded MonoBehaviour and ScriptableObject and the static fields of the game's types
        // (lists, arrays and nested objects included). Writes refs.txt.
        static string findRefs;

        sealed class SameObject : IEqualityComparer<object>
        {
            public new bool Equals(object a, object b) { return ReferenceEquals(a, b); }
            public int GetHashCode(object o) { return System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(o); }
        }

        static void FindRefs(string text)
        {
            float start = Time.realtimeSinceStartup;
            var sprites = new HashSet<object>(Resources.FindObjectsOfTypeAll<Sprite>().Where(s => s.texture != null && s.texture.name.Contains(text)).Cast<object>(), new SameObject());
            var textures = new HashSet<object>(Resources.FindObjectsOfTypeAll<Texture>().Where(t => t.name.Contains(text)).Cast<object>(), new SameObject());
            // Animation clips named after those sprites (Back1 for Back1_0) count as targets too.
            var spriteNames = new HashSet<string>(sprites.Cast<Sprite>().Select(s => System.Text.RegularExpressions.Regex.Replace(s.name, "_\\d+$", "")));
            foreach (AnimationClip clip in Resources.FindObjectsOfTypeAll<AnimationClip>())
                if (spriteNames.Contains(clip.name)) textures.Add(clip);
            var report = new StringBuilder("find-refs " + text + ": " + sprites.Count + " sprites, " + textures.Count + " textures\n");
            int found = 0;
            foreach (SpriteRenderer renderer in Resources.FindObjectsOfTypeAll<SpriteRenderer>())
                if (renderer.sprite != null && sprites.Contains(renderer.sprite) && found++ < 200)
                    report.Append("renderer " + Path(renderer.transform) + " scene=" + (renderer.gameObject.scene.IsValid() ? renderer.gameObject.scene.name : "<none>") +
                                  " sprite=" + renderer.sprite.name + "\n");
            var visited = new HashSet<object>(new SameObject());
            const System.Reflection.BindingFlags flags = AnyInstance;
            var fieldCache = new Dictionary<Type, System.Reflection.FieldInfo[]>();
            Func<Type, System.Reflection.FieldInfo[]> fieldsOf = type =>
            {
                System.Reflection.FieldInfo[] list;
                if (fieldCache.TryGetValue(type, out list)) return list;
                var all = new List<System.Reflection.FieldInfo>();
                for (Type t = type; t != null && t != typeof(object) && t != typeof(UnityEngine.Object); t = t.BaseType)
                    all.AddRange(t.GetFields(flags | System.Reflection.BindingFlags.DeclaredOnly).Where(fi => !fi.FieldType.IsPrimitive && fi.FieldType != typeof(string) && !fi.FieldType.IsEnum));
                return fieldCache[type] = all.ToArray();
            };
            var hits = new Dictionary<string, int>();
            Action<object, string, int> walk = null;
            walk = (value, path, depth) =>
            {
                if (value == null || depth > 12 || value is string || value is Type || value is System.Reflection.MemberInfo) return;
                var callback = value as Delegate;
                if (callback != null)
                {
                    if (!visited.Add(value)) return;
                    foreach (Delegate single in callback.GetInvocationList()) walk(single.Target, path + "()", depth + 1);
                    return;
                }
                Type type = value.GetType();
                if (type.IsPrimitive || type.IsEnum || type.IsPointer) return;
                // An engine object, live or destroyed (a destroyed one compares equal to null).
                bool engineObject = value is UnityEngine.Object;
                if (engineObject && depth > 0)
                {
                    if (sprites.Contains(value) || textures.Contains(value))
                    {
                        string key = path + " -> " + type.Name;
                        int n;
                        hits.TryGetValue(key, out n);
                        hits[key] = n + 1;
                    }
                    // Components and other engine objects are walked as roots of their own.
                    // Destroyed scripts (a room's components after it unloads) are reachable only this way.
                    if (!(value is ScriptableObject || value is MonoBehaviour) || !visited.Add(value)) return;
                    if (value is MonoBehaviour && (UnityEngine.Object)value == null) path += "(destroyed)";
                }
                else if (!type.IsValueType && !visited.Add(value) && depth > 0) return;
                var list = value as System.Collections.IEnumerable;
                // Only arrays and the class library's collections are enumerated (a game iterator
                // would run game code).
                if (list != null && !engineObject && (type.IsArray || (type.Namespace ?? "").StartsWith("System.Collections")))
                {
                    if (type.IsArray && type.GetElementType().IsPrimitive) return;
                    int index = 0;
                    foreach (object item in list) { walk(item, path + "[]", depth + 1); if (++index > 5000) break; }
                    if (type.IsArray || value is System.Collections.IList) return;
                }
                // Fields are walked only in the game's and Compat's own types (engine and class
                // library internals hold native pointers; collections were walked above).
                string owner = type.Assembly.GetName().Name;
                if (!engineObject && !owner.StartsWith("Recovered.") && !owner.StartsWith("Assembly-CSharp") && owner != "DrifterCompat" &&
                    !(type.IsValueType && type.Namespace == "System.Collections.Generic")) return;
                foreach (System.Reflection.FieldInfo field in fieldsOf(type))
                {
                    object child;
                    try { child = field.GetValue(value); } catch (Exception) { continue; }
                    walk(child, path + "." + field.Name, depth + 1);
                }
            };
            // The roots are all marked visited first, so references to one stop there and each is
            // walked under its own name (walk skips the visited test at depth 0).
            var roots = new Dictionary<UnityEngine.Object, string>();
            foreach (MonoBehaviour behaviour in Resources.FindObjectsOfTypeAll<MonoBehaviour>())
                if (behaviour != null && !behaviour.GetType().Assembly.GetName().Name.StartsWith("UnityEngine"))
                    roots[behaviour] = behaviour.GetType().Name + "(" + (behaviour.gameObject.scene.IsValid() ? behaviour.gameObject.scene.name : "<none>") + ")";
            foreach (ScriptableObject asset in Resources.FindObjectsOfTypeAll<ScriptableObject>())
                roots[asset] = asset.GetType().Name + "(" + asset.name + ")";
            foreach (UnityEngine.Object root in roots.Keys) visited.Add(root);
            foreach (var root in roots) walk(root.Key, root.Value, 0);
            Type questType = QuestType();
            var assemblies = AppDomain.CurrentDomain.GetAssemblies().Where(a => a.GetName().Name.StartsWith("Recovered.") || a.GetName().Name.StartsWith("Assembly-CSharp") || a.GetName().Name == "DrifterCompat");
            foreach (System.Reflection.Assembly assembly in assemblies)
            {
                Type[] types;
                try { types = assembly.GetTypes(); }
                catch (System.Reflection.ReflectionTypeLoadException e) { types = e.Types.Where(t => t != null).ToArray(); }
                foreach (Type type in types)
                {
                    if (type.ContainsGenericParameters) continue;
                    foreach (System.Reflection.FieldInfo field in type.GetFields(AnyStatic | System.Reflection.BindingFlags.DeclaredOnly))
                    {
                        if (field.FieldType.IsPrimitive || field.FieldType == typeof(string) || field.IsLiteral) continue;
                        object value;
                        try { value = field.GetValue(null); } catch (Exception) { continue; }
                        walk(value, "static " + type.Name + "." + field.Name, 1);
                    }
                }
            }
            foreach (var hit in hits.OrderByDescending(h => h.Value).Take(100)) report.Append(hit.Value + " " + hit.Key + "\n");
            // Engine-side holders: materials showing the textures, loaded animation clips and
            // GameObjects outside scenes (prefab assets), by root name.
            foreach (Material material in Resources.FindObjectsOfTypeAll<Material>())
                if (material.HasProperty("_MainTex") && material.mainTexture != null && textures.Contains(material.mainTexture))
                    report.Append("material " + material.name + " shader=" + (material.shader != null ? material.shader.name : "") + "\n");
            report.Append("clips " + string.Join(" ", Resources.FindObjectsOfTypeAll<AnimationClip>().Select(c => c.name).OrderBy(n => n).ToArray()) + "\n");
            foreach (var group in Resources.FindObjectsOfTypeAll<GameObject>().Where(g => !g.scene.IsValid() && g.transform.parent == null)
                         .GroupBy(g => g.name).OrderBy(g => g.Key))
                report.Append("asset root " + group.Key + " x" + group.Count() + " children " + group.Sum(g => g.GetComponentsInChildren<Transform>(true).Length) + "\n");
            report.Append("visited " + visited.Count + " in " + ((Time.realtimeSinceStartup - start) * 1000).ToString("0", Inv) + "ms\n");
            File.WriteAllText(System.IO.Path.Combine(outDir, "refs.txt"), report.ToString());
            Event("find-refs " + text + " sprites " + sprites.Count + " renderers " + found + " paths " + hits.Count);
        }

        static string Path(Transform transform)
        {
            string path = transform.name;
            for (Transform parent = transform.parent; parent != null; parent = parent.parent) path = parent.name + "/" + path;
            return path;
        }

        // A value as text with the invariant culture. Colours, vectors and rects keep Unity's own
        // layout ("RGBA(1.000, ...)", "(1.0, 2.0)"), whose ToString uses the player's culture;
        // format, when given, replaces the one for their numbers.
        static string Invariant(object value, string format = null)
        {
            if (value is Color) { var c = (Color)value; return "RGBA(" + Numbers(format ?? "F3", c.r, c.g, c.b, c.a) + ")"; }
            if (value is Vector2) { var v = (Vector2)value; return "(" + Numbers(format ?? "F1", v.x, v.y) + ")"; }
            if (value is Vector3) { var v = (Vector3)value; return "(" + Numbers(format ?? "F1", v.x, v.y, v.z) + ")"; }
            if (value is Vector4) { var v = (Vector4)value; return "(" + Numbers(format ?? "F1", v.x, v.y, v.z, v.w) + ")"; }
            if (value is Rect)
            {
                var r = (Rect)value;
                string f = format ?? "F2";
                return "(x:" + r.x.ToString(f, Inv) + ", y:" + r.y.ToString(f, Inv) + ", width:" + r.width.ToString(f, Inv) + ", height:" + r.height.ToString(f, Inv) + ")";
            }
            var formattable = value as IFormattable;
            return formattable != null ? formattable.ToString(format, Inv) : Convert.ToString(value, Inv);
        }

        static string Numbers(string format, params float[] values)
        {
            return string.Join(", ", values.Select(v => v.ToString(format, Inv)).ToArray());
        }
    }
}
