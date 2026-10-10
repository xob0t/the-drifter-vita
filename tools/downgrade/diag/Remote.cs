using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using UnityEngine;

// Remote mode (the PS Vita: args.txt, log.txt, heartbeats, screenshots over FTP), frame and camera
// timing, memory, input logging, the forced frame rate and the CRT setting.
// Part of the parity probe (DrifterDiag); see Diag.cs.
namespace DrifterDiag
{
    public static partial class Probe
    {
        // Remote mode, for players without a console or command line (PS Vita), driven
        // over FTP by tools/downgrade/vita.py. In <dir>:
        //   args.txt        extra -diag-* flags, whitespace separated, read at start
        //   log.txt         Unity log messages, scene changes and a heartbeat every HeartbeatSeconds
        //                   (RemoteFrame; tools/downgrade/heartbeats.py reads them)
        //   screenshot.req  create it to request a screenshot; the probe deletes it and
        //                   writes screenshot.raw (width, height as int32, RGBA32 rows bottom-up)
        // On PS Vita it is on by default in ux0:data/drifter; elsewhere -diag-remote <dir>.
        const string VitaRemoteDir = "ux0:data/drifter";
        const float HeartbeatSeconds = 5f;
        static string remoteDir;
        static StreamWriter remoteLog;
        static float heartbeatAt;
        static int heartbeatFrame;
        static bool capturing;

        static string[] RemoteArgs(string[] args)
        {
            int index = Array.IndexOf(args, "-diag-remote");
            if (index >= 0 && index + 1 < args.Length) remoteDir = args[index + 1];
            else if (Application.platform == RuntimePlatform.PSP2) remoteDir = VitaRemoteDir;
            if (remoteDir == null) return args;
            Directory.CreateDirectory(remoteDir);
            outDir = remoteDir;
            remoteLog = new StreamWriter(System.IO.Path.Combine(remoteDir, "log.txt"), false) { AutoFlush = true };
            remoteLog.WriteLine("probe " + Application.unityVersion + " " + Application.platform + " " + DateTime.Now.ToString("s"));
            Application.logMessageReceivedThreaded += (message, stack, type) =>
            {
                lock (remoteLog)
                {
                    remoteLog.WriteLine("[" + Time.realtimeSinceStartup.ToString("0.00", Inv) + "] " + type + ": " + message);
                    if (type == LogType.Exception || type == LogType.Error || type == LogType.Assert)
                        remoteLog.WriteLine(stack);
                }
            };
            string file = System.IO.Path.Combine(remoteDir, "args.txt");
            if (!File.Exists(file)) return args;
            string[] extra = File.ReadAllText(file).Split(new[] { ' ', '\t', '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);
            RemoteLine("args " + string.Join(" ", extra));
            return args.Concat(extra).ToArray();
        }

        // Counters of the translated build's Compat rules, for the heartbeat: AnimatorOverrides'
        // SpriteAnim clip changes (each rebinds an Animator) and unchanged replays since the last
        // heartbeat ("animApplied", "animReplayed"), NamedLists' answers checked and wrong
        // (-compat-check-named-lists), AnimatorCulling's Animators culled now ("animCulled"),
        // ParticleRate's simulation steps skipped and GuiReshows' replayed GUI show/hide pairs since
        // the last heartbeat ("particleSkips", "guiReshows"). Empty in players without Compat.
        static System.Reflection.FieldInfo appliedField, skippedField, cullingField, rateField, namedChecked, namedMismatched, reshowsField;
        static int lastApplied, lastSkipped, lastRateSkipped, lastReshows;
        static bool compatLooked;

        // A static field of a Compat type, or null when the build's Compat lacks it.
        static System.Reflection.FieldInfo CompatField(System.Reflection.Assembly compat, string type, string field)
        {
            Type found = compat.GetType("DrifterCompat." + type);
            return found == null ? null : found.GetField(field);
        }

        static string CompatCounts()
        {
            if (!compatLooked)
            {
                compatLooked = true;
                Type overrides = FindType("DrifterCompat.AnimatorOverrides");
                if (overrides != null)
                {
                    System.Reflection.Assembly compat = overrides.Assembly;
                    appliedField = overrides.GetField("Applied");
                    skippedField = overrides.GetField("Skipped");
                    cullingField = CompatField(compat, "AnimatorCulling", "Culled");
                    rateField = CompatField(compat, "ParticleRate", "Skipped");
                    reshowsField = CompatField(compat, "GuiReshows", "Count");
                    namedChecked = CompatField(compat, "NamedLists", "Checked");
                    namedMismatched = CompatField(compat, "NamedLists", "Mismatched");
                }
            }
            if (appliedField == null || skippedField == null) return "";
            int applied = (int)appliedField.GetValue(null), skipped = (int)skippedField.GetValue(null);
            string text = " animApplied " + (applied - lastApplied) + " animReplayed " + (skipped - lastSkipped);
            if (namedChecked != null && namedMismatched != null && (int)namedChecked.GetValue(null) > 0)
                text += " namedChecked " + (int)namedChecked.GetValue(null) + " namedMismatched " + (int)namedMismatched.GetValue(null);
            if (cullingField != null) text += " animCulled " + (int)cullingField.GetValue(null);
            if (rateField != null)
            {
                int rateSkipped = (int)rateField.GetValue(null);
                text += " particleSkips " + (rateSkipped - lastRateSkipped);
                lastRateSkipped = rateSkipped;
            }
            if (reshowsField != null)
            {
                int reshows = (int)reshowsField.GetValue(null);
                if (reshows != lastReshows) text += " guiReshows " + (reshows - lastReshows);
                lastReshows = reshows;
            }
            lastApplied = applied;
            lastSkipped = skipped;
            return text;
        }

        // Compat EffectSprites (the drops and bees drawn by pages): items and pages drawn in the
        // last frame, other renderers known, mean flush time since the last heartbeat; collider
        // answers, and with -compat-collider-check the engine's disagreements.
        static Type effectsType;
        static bool effectsLooked;

        static string EffectCounts()
        {
            if (!effectsLooked)
            {
                effectsLooked = true;
                effectsType = FindType("DrifterCompat.EffectSprites");
            }
            if (effectsType == null) return "";
            // A build whose Compat predates a counter (the probe is compiled from the current
            // source) has none of the section rather than an error at every heartbeat.
            if (effectsType.GetField("StatShapeCalls") == null || effectsType.GetField("StatCheckCalls") == null) return "";
            Func<string, object> get = name => effectsType.GetField(name).GetValue(null);
            int flushes = (int)get("StatFlushes");
            if (flushes == 0) return "";
            double ms = (double)get("StatMs");
            string text = " effects " + get("StatItems") + " pages " + get("StatPages") + " others " + get("StatForeign") + " released " + get("StatReleased") +
                          " flushMs " + (ms / flushes).ToString("0.00", Inv) + " shapes " + get("StatShapeCalls");
            long checks = (long)get("StatCheckCalls");
            if (checks > 0) text += " shapeCheck " + get("StatCheckMismatches") + "/" + checks;
            // ms a flush: other renderers, changed items (with boundaries), page uploads, boundaries.
            var ticks = (long[])get("StatTicks");
            text += " parts";
            for (int i = 0; i < ticks.Length; i++)
            {
                text += " " + (ticks[i] * 1000.0 / System.Diagnostics.Stopwatch.Frequency / flushes).ToString("0.00", Inv);
                ticks[i] = 0;
            }
            effectsType.GetField("StatFlushes").SetValue(null, 0);
            effectsType.GetField("StatMs").SetValue(null, 0.0);
            return text;
        }

        static void RemoteLine(string line)
        {
            if (remoteLog == null) return;
            lock (remoteLog) remoteLog.WriteLine("[" + Time.realtimeSinceStartup.ToString("0.00", Inv) + "] " + line);
        }

        // Frame split for the heartbeat: renderMs from the first camera's pre-cull to the end of
        // the frame (culling, draw submission, image effects); otherMs the rest (scripts,
        // animation, and the wait at present when the GPU is behind).
        static float renderStart = -1, frameEnd = -1, renderSum, otherSum;
        static int timedFrames;
        static MonoBehaviour timingHost;

        // Per camera: "render" from its pre-cull to its post-render, "after" from its post-render
        // to the next camera's pre-cull or the end of the frame (image effects such as
        // OnRenderImage run there). Averages per frame go to the heartbeat.
        static readonly System.Collections.Generic.Dictionary<string, float[]> cameraTimes =
            new System.Collections.Generic.Dictionary<string, float[]>();
        static string openCamera;
        static float openAt, closedAt;
        static bool cameraTiming;

        static void CameraPreCull(Camera camera)
        {
            float now = Time.realtimeSinceStartup;
            CloseCamera(now);
            openCamera = CameraName(camera);
            openAt = now;
            closedAt = -1;
        }

        static void CameraPostRender(Camera camera)
        {
            if (openCamera != CameraName(camera)) return;
            closedAt = Time.realtimeSinceStartup;
            Add(openCamera, 0, closedAt - openAt);
        }

        // The open camera's "after" ends at the next camera's pre-cull or the frame's end.
        static void CloseCamera(float now)
        {
            if (openCamera != null && closedAt > 0) Add(openCamera, 1, now - closedAt);
        }

        // Camera.name allocates a new string on every read; the timing runs twice a camera a frame.
        static readonly Dictionary<int, string> cameraNames = new Dictionary<int, string>();

        static string CameraName(Camera camera)
        {
            string name;
            if (!cameraNames.TryGetValue(camera.GetInstanceID(), out name)) cameraNames[camera.GetInstanceID()] = name = camera.name;
            return name;
        }

        static void CloseFrame(float frameEnd)
        {
            CloseCamera(frameEnd);
            openCamera = null;
        }

        static void Add(string camera, int slot, float seconds)
        {
            float[] times;
            if (!cameraTimes.TryGetValue(camera, out times)) cameraTimes[camera] = times = new float[2];
            times[slot] += seconds;
        }

        static string CameraTimes(int frames)
        {
            string text = "";
            if (cameraTimes.Count > 0 && frames > 0)
            {
                text = " cams";
                foreach (var entry in cameraTimes)
                    text += " " + entry.Key.Replace(" ", "") + ":" + (entry.Value[0] * 1000 / frames).ToString("0.0", Inv) +
                            "+" + (entry.Value[1] * 1000 / frames).ToString("0.0", Inv);
            }
            cameraTimes.Clear();
            return text;
        }

        // Time a frame spends in the engine's own profiler markers, from Recorder, which only
        // development players fill: where the frame goes on platforms without a profiler at
        // hand (pipeline.py build --diag --development). Markers the player lacks are skipped.
        static readonly string[] MarkerNames =
        {
            "Update.ScriptRunBehaviourUpdate", "PreLateUpdate.ScriptRunBehaviourLateUpdate",
            "Update.ScriptRunDelayedDynamicFrameRate", "FixedUpdate.ScriptRunBehaviourFixedUpdate",
            "FixedUpdate.Physics2DFixedUpdate", "FixedUpdate.PhysicsFixedUpdate",
            "PreLateUpdate.DirectorUpdateAnimationBegin", "PreLateUpdate.DirectorUpdateAnimationEnd",
            "PreLateUpdate.ParticleSystemBeginUpdateAll", "PostLateUpdate.ParticleSystemEndUpdateAll",
            "PostLateUpdate.UpdateAllRenderers", "PostLateUpdate.PlayerUpdateCanvases",
            "PostLateUpdate.PlayerEmitCanvasGeometry", "PostLateUpdate.UpdateAudio",
            "PostLateUpdate.FinishFrameRendering", "Camera.Render", "Culling", "Drawing",
            "Render.OpaqueGeometry", "Render.TransparentGeometry", "Camera.ImageEffects",
            "Gfx.WaitForPresent", "Gfx.PresentFrame", "WaitForTargetFPS", "GC.Collect",
        };
        static List<UnityEngine.Profiling.Recorder> recorders;
        static List<string> markerLabels;
        static double[] markerSums;
        static int markerFrames;

        static void SampleMarkers()
        {
            if (recorders == null)
            {
                recorders = new List<UnityEngine.Profiling.Recorder>();
                markerLabels = new List<string>();
                foreach (string name in MarkerNames)
                {
                    UnityEngine.Profiling.Recorder recorder = UnityEngine.Profiling.Recorder.Get(name);
                    if (recorder == null || !recorder.isValid) continue;
                    recorder.enabled = true;
                    recorders.Add(recorder);
                    markerLabels.Add(name);
                }
                markerSums = new double[recorders.Count];
            }
            // Script methods get markers named "Type.Method()" as they first run; pick up new
            // ones now and then so the heartbeat can name the scripts that cost the most.
            if (Time.frameCount >= nextScriptScan)
            {
                nextScriptScan = Time.frameCount + 300;
                var names = new List<string>();
                UnityEngine.Profiling.Sampler.GetNames(names);
                foreach (string name in names)
                {
                    if (!name.EndsWith("()") || markerLabels.Contains(name)) continue;
                    UnityEngine.Profiling.Recorder recorder = UnityEngine.Profiling.Recorder.Get(name);
                    if (recorder == null || !recorder.isValid) continue;
                    recorder.enabled = true;
                    recorders.Add(recorder);
                    markerLabels.Add(name);
                }
                Array.Resize(ref markerSums, recorders.Count);
            }
            for (int i = 0; i < recorders.Count; i++) markerSums[i] += recorders[i].elapsedNanoseconds;
            markerFrames++;
        }
        static int nextScriptScan;

        static string MarkerTimes()
        {
            if (recorders == null || markerFrames == 0) return "";
            var text = new StringBuilder(" markers");
            var scripts = new List<KeyValuePair<string, double>>();
            for (int i = 0; i < recorders.Count; i++)
            {
                double ms = markerSums[i] / 1e6 / markerFrames;
                if (markerLabels[i].EndsWith("()")) scripts.Add(new KeyValuePair<string, double>(markerLabels[i], ms));
                else if (ms >= 0.05) text.Append(" " + markerLabels[i] + ":" + ms.ToString("0.0", Inv));
                markerSums[i] = 0;
            }
            if (scripts.Count > 0) text.Append(" scripts");
            foreach (var entry in scripts.OrderByDescending(e => e.Value).Take(15).Where(e => e.Value >= 0.05))
                text.Append(" " + entry.Key + ":" + entry.Value.ToString("0.00", Inv));
            markerFrames = 0;
            return text.ToString();
        }

        static void TimeFrame()
        {
            float now = Time.realtimeSinceStartup;
            if (frameEnd > 0 && now - frameEnd < 1f) otherSum += now - frameEnd;
            renderStart = now;
            if (timingHost == null || !timingHost.isActiveAndEnabled || now - frameEnd > 1f)
            {
                timingHost = AnyActiveBehaviour();
                if (timingHost != null) timingHost.StartCoroutine(FrameEnds(timingHost));
            }
        }

        static System.Collections.IEnumerator FrameEnds(MonoBehaviour host)
        {
            while (host == timingHost)
            {
                yield return new WaitForEndOfFrame();
                frameEnd = Time.realtimeSinceStartup;
                if (cameraTiming) CloseFrame(frameEnd);
                if (renderStart > 0) { renderSum += frameEnd - renderStart; timedFrames++; }
            }
        }

        static void RemoteFrame(string scene)
        {
            if (remoteDir == null) return;
            TimeFrame();
            if (logInput) LogInput();
            SampleMarkers();
            AllocTimer.Frame();
            if (Time.realtimeSinceStartup >= heartbeatAt + HeartbeatSeconds)
            {
                if (heartbeatAt > 0) RemoteLine(Heartbeat(scene));
                else
                    RemoteLine("system " + SystemInfo.operatingSystem + " | " + SystemInfo.deviceModel +
                               " | data " + Application.persistentDataPath + " | saves " + DrifterSaveDir() +
                               " | joysticks [" + JoystickNames() + "]");
                heartbeatAt = Time.realtimeSinceStartup;
                heartbeatFrame = Time.frameCount;
                renderSum = otherSum = 0;
                timedFrames = 0;
            }
            if (capturing || Time.frameCount % 10 != 0) return;
            string request = System.IO.Path.Combine(remoteDir, "screenshot.req");
            if (!File.Exists(request)) return;
            File.Delete(request);
            // Coroutines need a MonoBehaviour; any active one of the game's will do.
            MonoBehaviour host = AnyActiveBehaviour();
            if (host == null) { RemoteLine("screenshot: no active behaviour to run on"); return; }
            capturing = true;
            host.StartCoroutine(Capture());
        }

        // The heartbeat line, over the frames since the last one: "heartbeat frame N fps F
        // [renderMs R otherMs O] scene S vSyncCount V targetFrameRate T maxQueuedFrames Q managedMB M
        // [freeLPDDR ... gcFreeMB ...] input 'device' devices N [animApplied ...] [effects ... parts ...]
        // [cams <camera>:<ms>+<after ms>...] [markers <marker>:<ms>... [scripts <method>():<ms>...]]
        // [scriptsMs <ms> <method>:<ms>/<calls>...] [loop <step>:<ms>...] [allocB <method>:<bytes>...]
        // gcs N gcAllocB B". tools/downgrade/heartbeats.py parses it (HEARTBEAT_SECTIONS there).
        // A section that throws reads " <section> error <exception>", and the rest is still written.
        static string Heartbeat(string scene)
        {
            int frames = Time.frameCount - heartbeatFrame;
            float fps = frames / Math.Max(0.001f, Time.realtimeSinceStartup - heartbeatAt);
            string split = timedFrames == 0 ? "" : " renderMs " + (renderSum * 1000 / timedFrames).ToString("0.0", Inv) +
                                                  " otherMs " + (otherSum * 1000 / timedFrames).ToString("0.0", Inv);
            return "heartbeat frame " + Time.frameCount + " fps " + fps.ToString("0.0", Inv) + split + " scene " + scene +
                   " vSyncCount " + QualitySettings.vSyncCount + " targetFrameRate " + Application.targetFrameRate +
                   " maxQueuedFrames " + QualitySettings.maxQueuedFrames +
                   " managedMB " + (GC.GetTotalMemory(false) / MB).ToString("0.0", Inv) + Section("vita", VitaMemory) +
                   " input " + ActiveInputDevice() + Section("compat", CompatCounts) + Section("effects", EffectCounts) +
                   Section("cams", () => CameraTimes(timedFrames)) + Section("markers", MarkerTimes) +
                   Section("scriptsMs", () => ScriptTimer.Report(frames)) + Section("loop", () => LoopTimer.Report(frames)) +
                   Section("allocB", () => AllocTimer.Report(frames)) + Section("gcs", AllocTimer.GcReport);
        }

        static string Section(string name, Func<string> section)
        {
            try { return section(); }
            catch (Exception e) { return " " + name + " error " + e.GetType().Name; }
        }

        // Free system memory on PS Vita, from the player's own diagnostics (UnityEngine.PSVitaModule,
        // kept by link.xml): main LPDDR2 RAM, video CDRAM and physically contiguous RAM, in MB.
        static Type vitaDiagnostics;

        static string VitaMemory()
        {
            if (Application.platform != RuntimePlatform.PSP2) return "";
            if (vitaDiagnostics == null) vitaDiagnostics = Type.GetType("UnityEngine.PSVita.Diagnostics, UnityEngine.PSVitaModule");
            if (vitaDiagnostics == null) return " vita diagnostics missing";
            Func<string, string> free = name =>
                ((int)vitaDiagnostics.GetMethod(name).Invoke(null, null) / MB).ToString("0.0", Inv);
            return " freeLPDDR " + free("GetFreeMemoryLPDDR") + " freeCDRAM " + free("GetFreeMemoryCDRAM") +
                   " freePHYSCONT " + free("GetFreeMemoryPHYSCONT") + GcHeap();
        }

        // The IL2CPP garbage collector's heap (Boehm, linked into the game module on IL2CPP
        // players): its size as taken from the system, which it never gives back, and the part of
        // it free after the last collection, in MB. GC.GetTotalMemory is only the part in use.
        [System.Runtime.InteropServices.DllImport("__Internal")] static extern UIntPtr GC_get_heap_size();
        [System.Runtime.InteropServices.DllImport("__Internal")] static extern UIntPtr GC_get_free_bytes();
        static bool noGcHeap;

        static string GcHeap()
        {
            if (noGcHeap) return "";
            try
            {
                return " gcHeapMB " + ((ulong)GC_get_heap_size() / MB).ToString("0.0", Inv) +
                       " gcFreeMB " + ((ulong)GC_get_free_bytes() / MB).ToString("0.0", Inv);
            }
            catch (Exception)
            {
                noGcHeap = true;
                return " gcHeap unavailable";
            }
        }

        // Where the translated game keeps saves (DrifterCompat.Platform.CurrentDirectory).
        static string DrifterSaveDir()
        {
            Type platform = Type.GetType("DrifterCompat.Platform, DrifterCompat");
            return platform == null ? "?" : (string)platform.GetMethod("CurrentDirectory").Invoke(null, null);
        }

        // UnityEngine.Input is in CoreModule in 2018 and InputLegacyModule in 2020, and the probe runs
        // in both players (the original is a 2020 one), so the heartbeat and -diag-log-input reach
        // it by reflection. (-diag-check-keys calls it directly: it checks Compat's InputKeys, which
        // only the translated, 2018 players have.)
        static Type inputType, inControlManager;
        static bool inputLooked, inControlLooked;

        static Type InputType()
        {
            if (!inputLooked)
            {
                inputLooked = true;
                inputType = Type.GetType("UnityEngine.Input, UnityEngine.InputLegacyModule") ?? Type.GetType("UnityEngine.Input, UnityEngine.CoreModule");
            }
            return inputType;
        }

        // InControl's InputManager, through which the game reads controllers, or null.
        static Type InControlManager()
        {
            if (!inControlLooked)
            {
                inControlLooked = true;
                inControlManager = Type.GetType("InControl.InputManager, InControl");
            }
            return inControlManager;
        }

        static string JoystickNames()
        {
            Type input = InputType();
            if (input == null) return "?";
            return string.Join(", ", (string[])input.GetMethod("GetJoystickNames").Invoke(null, null));
        }

        // Unity's names for joysticks 1-8's buttons 0-19, "joystick N button M", in the order of
        // their KeyCodes (KeyCode.Joystick1Button0 + 20 (N - 1) + M), and joysticks 1-4's axes
        // "joystick N analog M" (InControl's, in the Input Manager), with -diag-log-input's labels.
        static readonly string[] JoystickButtonNames =
            Enumerable.Range(0, 160).Select(i => "joystick " + (i / 20 + 1) + " button " + i % 20).ToArray();
        static readonly string[] JoystickAxisNames =
            Enumerable.Range(0, 80).Select(i => "joystick " + (i / 20 + 1) + " analog " + i % 20).ToArray();
        static readonly string[] ButtonLabels = Enumerable.Range(0, 80).Select(i => "j" + (i / 20 + 1) + "b" + i % 20).ToArray();
        static readonly string[] AxisLabels = Enumerable.Range(0, 80).Select(i => "j" + (i / 20 + 1) + "a" + i % 20).ToArray();

        // -diag-log-input: logs each change in the raw state of joysticks 1-4 (buttons 0-19 and the
        // "joystick N analog M" axes), to map a platform's controls to Unity's joystick numbering.
        static bool logInput;
        static string lastInput = "";
        static System.Reflection.MethodInfo getKeyByName, getAxisRaw;
        static readonly object[] inputArgument = new object[1];

        static void LogInput()
        {
            Type input = InputType();
            if (getKeyByName == null)
            {
                getKeyByName = input.GetMethod("GetKey", new[] { typeof(string) });
                getAxisRaw = input.GetMethod("GetAxisRaw", new[] { typeof(string) });
            }
            var state = new List<string>();
            for (int i = 0; i < 80; i++)
            {
                inputArgument[0] = JoystickButtonNames[i];
                if ((bool)getKeyByName.Invoke(null, inputArgument)) state.Add(ButtonLabels[i]);
                inputArgument[0] = JoystickAxisNames[i];
                float value = (float)getAxisRaw.Invoke(null, inputArgument);
                if (Math.Abs(value) > 0.2f) state.Add(AxisLabels[i] + "=" + value.ToString("0.00", Inv));
            }
            AddInputExtras(input, state);
            string line = string.Join(" ", state.ToArray());
            if (line == lastInput) return;
            lastInput = line;
            RemoteLine("input [" + line + "]");
        }

        // Also logged with -diag-log-input: touches ("touch=x,y"), the mouse position
        // ("mouse=x,y"), the pressed controls of InControl's active device ("ic:Target") and
        // whether the game is in controller mode ("pad" or "mousemode", SystemInput's
        // ControllerActive).
        static UnityEngine.Object systemInput;
        static System.Reflection.PropertyInfo controllerActive;

        static void AddInputExtras(Type input, List<string> state)
        {
            try
            {
                int touches = (int)input.GetProperty("touchCount").GetValue(null, null);
                for (int i = 0; i < touches; i++)
                {
                    object touch = input.GetMethod("GetTouch").Invoke(null, new object[] { i });
                    var position = (Vector2)touch.GetType().GetProperty("position").GetValue(touch, null);
                    state.Add("touch=" + position.x.ToString("0", Inv) + "," + position.y.ToString("0", Inv) + ":" +
                              touch.GetType().GetProperty("phase").GetValue(touch, null));
                }
                var mouse = (Vector3)input.GetProperty("mousePosition").GetValue(null, null);
                state.Add("mouse=" + mouse.x.ToString("0", Inv) + "," + mouse.y.ToString("0", Inv));
                Type manager = InControlManager();
                object device = manager == null ? null : manager.GetProperty("ActiveDevice").GetValue(null, null);
                var controls = device == null ? null : device.GetType().GetProperty("Controls").GetValue(device, null) as System.Collections.IEnumerable;
                if (controls != null)
                    foreach (object control in controls)
                        if (control != null && (bool)control.GetType().GetProperty("IsPressed").GetValue(control, null))
                            state.Add("ic:" + control.GetType().GetProperty("Target").GetValue(control, null));
                if (systemInput == null && Time.frameCount % 60 == 0)
                {
                    Type type = Type.GetType("SystemInput, Recovered.Drifter") ?? Type.GetType("SystemInput, Assembly-CSharp");
                    systemInput = type == null ? null : UnityEngine.Object.FindObjectOfType(type);
                    controllerActive = type == null ? null : type.GetProperty("ControllerActive");
                }
                if (systemInput != null && controllerActive != null)
                    state.Add((bool)controllerActive.GetValue(systemInput, null) ? "pad" : "mousemode");
            }
            catch (Exception e) { state.Add("extras-error " + e.GetType().Name); }
        }

        // The name of InControl's active device and the number of its devices.
        static string ActiveInputDevice()
        {
            try
            {
                Type manager = InControlManager();
                if (manager == null) return "no-incontrol";
                object device = manager.GetProperty("ActiveDevice").GetValue(null, null);
                object name = device == null ? null : device.GetType().GetProperty("Name").GetValue(device, null);
                var devices = manager.GetField("Devices").GetValue(null) as System.Collections.ICollection;
                return "'" + name + "' devices " + (devices == null ? -1 : devices.Count);
            }
            catch (Exception e) { return "error " + e.GetType().Name; }
        }

        static System.Collections.IEnumerator Capture()
        {
            yield return new WaitForEndOfFrame();
            try
            {
                Texture2D shot = ScreenCapture.CaptureScreenshotAsTexture();
                string temp = System.IO.Path.Combine(remoteDir, "screenshot.tmp");
                WriteRaw(temp, shot);
                string target = System.IO.Path.Combine(remoteDir, "screenshot.raw");
                if (File.Exists(target)) File.Delete(target);
                File.Move(temp, target);
                RemoteLine("screenshot " + shot.width + "x" + shot.height + " " + shot.format);
                UnityEngine.Object.Destroy(shot);
            }
            catch (Exception e) { RemoteLine("screenshot failed: " + e); }
            capturing = false;
        }

        // -diag-crt on|off sets the game's CRT filter setting (QuestSettings.CameraFilterOn) 2 s
        // after the title room and 2 s after a save restore, and saves the settings (the game
        // saves them with every save and on leaving its options).
        static string crtSetting;
        static int crtApplied;

        static void ApplyCrtSetting()
        {
            if (crtSetting == null) return;
            float now = Time.realtimeSinceStartup;
            int due = loadedAt >= 0 && now >= loadedAt + 2f ? 2 : titleAt >= 0 && now >= titleAt + 2f ? 1 : 0;
            if (due <= crtApplied) return;
            UnityEngine.Object quest = FindQuest();
            if (quest == null) return;
            crtApplied = due;
            object settings = quest.GetType().GetProperty("Settings").GetValue(quest, null);
            settings.GetType().GetProperty("CameraFilterOn").SetValue(settings, crtSetting == "on", null);
            quest.GetType().GetMethod("SaveSettings", Type.EmptyTypes).Invoke(quest, null);
            RemoteLine("crt setting " + crtSetting);
        }

        // -diag-vsync <n> sets QualitySettings.vSyncCount again after the game's rules, every 30
        // frames; -diag-target-fps <n> likewise Application.targetFrameRate (-1: no limit).
        // "-diag-vsync 0 -diag-target-fps -1" lets the PS Vita run unsynchronized, so the frame
        // rate shows the frame's real cost instead of the 60 Hz limit.
        static int? forcedVSync, forcedTargetFps;

        static void ApplyForcedRates()
        {
            if (Time.frameCount % 30 != 0) return;
            if (forcedVSync.HasValue && QualitySettings.vSyncCount != forcedVSync.Value) QualitySettings.vSyncCount = forcedVSync.Value;
            if (forcedTargetFps.HasValue && Application.targetFrameRate != forcedTargetFps.Value) Application.targetFrameRate = forcedTargetFps.Value;
        }
    }
}
