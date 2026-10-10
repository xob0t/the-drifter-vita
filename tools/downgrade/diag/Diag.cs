using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using UnityEngine;

// Diagnostic hooks injected into throwaway copies of the original and translated
// players, so both can be inspected the same way. Controlled by command-line flags:
//   -diag-out <dir>            where dumps are written
//   -diag-dump-after <secs>    dump cameras, image effects and visible renderers
//   -diag-disable <substring>  disable Behaviours whose type name contains it (repeatable)
//   -diag-hide-shader <name>   disable renderers drawing with this shader (repeatable)
// ReadFlags (Run.cs) reads the flags (on the PS Vita from args.txt, Remote.cs); each is described
// where it is used. diag.py injects the probe into PC players; pipeline.py --diag builds it in
// (IL2CPP and PS Vita players).
//
// The probe is this file and the other diag/*.cs but InjectDiag.cs (the injector), compiled
// together in the order of pipeline.DIAG_SOURCES; Probe is one partial class across them:
//   Diag.cs         Hook, Probe's state and helpers: dumps, room changes, events, drivers
//   Checks.cs       on/off checks at dump time and every frame
//   Experiments.cs  coroutine experiments after the dump
//   Remote.cs       remote mode (PS Vita), heartbeats, frame timing, input logging, screenshots
//   Run.cs          Start and the flags, the per-frame hook, the dump
//   Timers.cs       AllocTimer, LoopTimer, RoomTiming, ScriptTimer
// Numbers in the probe's files and logs are written with the invariant culture (Inv): a
// Windows player would otherwise write them in the PC's culture ("60,3").
namespace DrifterDiag
{
    public static class Hook
    {
        static bool started;

        public static void Init()
        {
            if (started) return;
            started = true;
            // Players only attach MonoBehaviours from registered script assemblies, so the
            // probe runs from a static per-camera callback instead of a component.
            Probe.Start();
        }
    }

    public static partial class Probe
    {
        internal static readonly System.Globalization.CultureInfo Inv = System.Globalization.CultureInfo.InvariantCulture;
        internal const float MB = 1048576f;
        // Reflection flags: any instance member, any static member, or either.
        const System.Reflection.BindingFlags AnyInstance = System.Reflection.BindingFlags.Instance |
            System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic;
        const System.Reflection.BindingFlags AnyStatic = System.Reflection.BindingFlags.Static |
            System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic;
        const System.Reflection.BindingFlags AnyMember = AnyInstance | System.Reflection.BindingFlags.Static;

        static string outDir = ".";
        static float dumpAfter = -1;
        static int lastFrame = -1;
        static bool dumped;
        static string[] disable = new string[0], hideShaders = new string[0], assignments = new string[0];
        static string loadSave;
        static string[] dumpProps = new string[0], dumpTargets = new string[0], hidePaths = new string[0];
        static string gotoRoom;
        static bool wantsGoto;
        static float gotoAt = -1;
        static float gotoDelay = 3f, introScale = 1f;
        static bool dumpSprites, dumpTextures;
        static string lastScene;

        // -diag-dump-sprites writes sprites.txt: every loaded sprite with its texture,
        // packing and the first vertices mapped to texture pixels.
        static void DumpSprites()
        {
            if (!dumpSprites) return;
            var text = new StringBuilder();
            foreach (Sprite sprite in Resources.FindObjectsOfTypeAll<Sprite>().OrderBy(s => s.name))
            {
                if (sprite.texture == null) continue;
                text.AppendLine(sprite.name + "\t" + sprite.texture.name + "\t" + (sprite.packed ? sprite.packingRotation.ToString() : "unpacked") + "\t" +
                                VertexMap(sprite, 4, "0.###"));
            }
            File.WriteAllText(System.IO.Path.Combine(outDir, "sprites.txt"), text.ToString());
        }

        // A sprite's first vertices (at most count), each with the texture pixel it maps to
        // ("x,y->u,v"), space separated.
        static string VertexMap(Sprite sprite, int count, string format)
        {
            Vector2[] vertices = sprite.vertices, uv = sprite.uv;
            int width = sprite.texture.width, height = sprite.texture.height;
            return string.Join(" ", Enumerable.Range(0, Math.Min(count, vertices.Length)).Select(k =>
                vertices[k].x.ToString(format, Inv) + "," + vertices[k].y.ToString(format, Inv) + "->" +
                (uv[k].x * width).ToString(format, Inv) + "," + (uv[k].y * height).ToString(format, Inv)).ToArray());
        }

        // -diag-dump-textures writes textures.tsv: every loaded texture with its size,
        // format and estimated memory (Profiler sizes are 0 outside development players).
        static void DumpTextures(string file)
        {
            if (!dumpTextures) return;
            var text = new StringBuilder("name\tkind\twidth\theight\tformat\tmips\tbytes\n");
            long total = 0;
            foreach (Texture texture in Resources.FindObjectsOfTypeAll<Texture>().OrderBy(t => t.name))
            {
                string format;
                long bits;
                int mips = 1;
                var texture2D = texture as Texture2D;
                var target = texture as RenderTexture;
                if (texture2D != null)
                {
                    format = texture2D.format.ToString();
                    mips = texture2D.mipmapCount;
                    bits = BitsPerPixel(texture2D.format);
                }
                else if (target != null)
                {
                    format = target.format + "+d" + target.depth;
                    bits = (target.format == RenderTextureFormat.ARGBHalf ? 64 : target.format == RenderTextureFormat.ARGBFloat ? 128 : 32) +
                           target.depth * Math.Max(1, target.antiAliasing);
                }
                else { format = texture.GetType().Name; bits = 32; }
                long bytes = (long)texture.width * texture.height * bits / 8;
                if (mips > 1) bytes = bytes * 4 / 3;
                total += bytes;
                text.Append(texture.name + "\t" + texture.GetType().Name + "\t" + texture.width + "\t" + texture.height + "\t" +
                            format + "\t" + mips + "\t" + bytes + "\n");
            }
            text.Append("TOTAL\t\t\t\t\t\t" + total + "\n");
            File.WriteAllText(System.IO.Path.Combine(outDir, file), text.ToString());
        }

        static long BitsPerPixel(TextureFormat format)
        {
            switch (format)
            {
                case TextureFormat.Alpha8: case TextureFormat.R8: return 8;
                case TextureFormat.RGB24: return 24;
                case TextureFormat.RGBA4444: case TextureFormat.ARGB4444: case TextureFormat.RGB565: case TextureFormat.R16:
                case TextureFormat.RHalf: case TextureFormat.RG16: return 16;
                case TextureFormat.DXT1: case TextureFormat.ETC_RGB4: case TextureFormat.ETC2_RGB: case TextureFormat.PVRTC_RGB4:
                case TextureFormat.PVRTC_RGBA4: case TextureFormat.BC4: return 4;
                case TextureFormat.PVRTC_RGB2: case TextureFormat.PVRTC_RGBA2: return 2;
                case TextureFormat.DXT5: case TextureFormat.BC5: case TextureFormat.BC6H: case TextureFormat.BC7: case TextureFormat.ETC2_RGBA8: return 8;
                case TextureFormat.RGBAHalf: return 64;
                case TextureFormat.RGBAFloat: return 128;
                default: return 32;
            }
        }

        // -diag-goto-room <ScriptName>[,<ScriptName>...] moves the game to that room
        // -diag-goto-delay seconds (default 3) after the save restore has finished (the
        // restored room's loop started), through PowerQuest's own ChangeRoomBG; a list
        // visits the rooms in turn, each -diag-goto-delay seconds after the previous one
        // is ready. Capture timing then counts from the last room change.
        // A room is ready when its loop starts, after its enter sequence. Some sequences wait
        // for their lines to be skipped (Crypt's intro: "My head hits stone. Hard"), so a chain
        // stopped there; once such a room has loaded and gotoDelay + 5 s have passed, its lines
        // are skipped (PowerQuest.SkipDialog, as a click does) every half second until it is ready.
        static float nextLineSkipAt;
        static int gotoCount;

        static void GotoRoom()
        {
            if (loadedAt < 0 || Time.realtimeSinceStartup < loadedAt + gotoDelay) return;
            if (gotoAt >= 0 && transitionStartAt > gotoAt && transitionStartAt > roomReadyAt && wasLevelLoaded && !wasTransitioning &&
                Time.realtimeSinceStartup > transitionStartAt + gotoDelay + 5f && Time.realtimeSinceStartup >= nextLineSkipAt)
            {
                nextLineSkipAt = Time.realtimeSinceStartup + 0.5f;
                UnityEngine.Object instance = FindQuest();
                System.Reflection.MethodInfo skip = instance == null ? null : QuestType().GetMethod("SkipDialog", new[] { typeof(bool) });
                if (skip != null)
                {
                    skip.Invoke(instance, new object[] { false });
                    Event("goto-wait skip-dialog");
                }
            }
            // A room change while PowerQuest is still loading a room is refused or races
            // the load (on the PS Vita the restore takes longer than the delay). The restored
            // room's scene must have loaded too: when the title room's loop started just before the
            // restore call (slow players, tools/downgrade/vitalike.py), its room-ready let the first
            // goto race the restored room's load, which then won and the goto was lost.
            if (roomReadyAt < 0 || sceneLoadedAt < loadedAt || transitionStartAt > roomReadyAt ||
                Time.realtimeSinceStartup < Math.Max(roomReadyAt, sceneLoadedAt) + gotoDelay) return;
            UnityEngine.Object quest = FindQuest();
            if (quest == null) return;
            Type questType = QuestType();
            string[] rooms = gotoRoom.Split(',');
            // With -diag-dump-textures, the loaded textures as each room is left (or the restored one).
            DumpTextures("textures-goto" + (gotoCount++) + "-" + UnityEngine.SceneManagement.SceneManager.GetActiveScene().name + ".tsv");
            gotoAt = Time.realtimeSinceStartup;
            object room = questType.GetMethod("GetRoom", new[] { typeof(string) }).Invoke(quest, new object[] { rooms[0] });
            string result = "missing";
            if (room != null)
            {
                // -diag-time-room-change: from the call, which starts the scene load.
                BeginRoomTiming();
                long callStart = System.Diagnostics.Stopwatch.GetTimestamp();
                int callCollections = GC.CollectionCount(0);
                questType.GetMethods().First(m => m.Name == "ChangeRoomBG" && m.GetParameters().Length == 1).Invoke(quest, new[] { room });
                RoomTiming.Capture("call PowerQuest.ChangeRoomBG", System.Diagnostics.Stopwatch.GetTimestamp() - callStart,
                                   GC.CollectionCount(0) - callCollections);
                result = "ok";
            }
            Event("goto " + rooms[0] + " " + result);
            gotoRoom = rooms.Length > 1 ? string.Join(",", rooms.Skip(1).ToArray()) : null;
        }

        static Type questTypeCache;

        static Type QuestType()
        {
            if (questTypeCache == null) questTypeCache = FindType("PowerTools.Quest.PowerQuest");
            return questTypeCache;
        }

        // The live PowerQuest, or null (not loaded yet).
        static UnityEngine.Object FindQuest()
        {
            Type type = QuestType();
            return type == null ? null : UnityEngine.Object.FindObjectOfType(type);
        }

        // Any active MonoBehaviour of the game's, to run the probe's coroutines on, or null.
        static MonoBehaviour AnyActiveBehaviour()
        {
            return UnityEngine.Object.FindObjectsOfType<MonoBehaviour>().FirstOrDefault(b => b.isActiveAndEnabled);
        }

        // A type by full name from whichever loaded assembly has it, or null.
        internal static Type FindType(string name)
        {
            return AppDomain.CurrentDomain.GetAssemblies().Select(a => a.GetType(name)).FirstOrDefault(t => t != null);
        }

        // Room transition timing, written to events.txt (and the remote log) with
        // realtimeSinceStartup: Unity's scene events, PowerQuest's transition state
        // (m_transitioning, m_levelLoadedCalled, m_roomLoopStarted) and fade, and every
        // frame that took longer than StallSeconds ("stall"). Lines in events.txt:
        // "[time] f<frame> what +ms", where +ms counts from the start of the last transition
        // (absent before the first); the remote log has "[time] what +ms".
        const float StallSeconds = 0.1f;
        static float lastFrameAt = -1, transitionStartAt = -1, roomReadyAt = -1;
        static bool wasTransitioning, wasLevelLoaded = true, wasLoopStarted, fadingIn;
        static float wasFade;
        static float fadedOutAt = -1;
        static bool stuckLogged;
        static UnityEngine.Object questInstance;
        static System.Reflection.FieldInfo transitioningField, levelLoadedField, loopStartedField;
        static System.Reflection.MethodInfo fadeRatioMethod;
        static int nextQuestSearch, lastCollections;

        static void Event(string line)
        {
            // -diag-time-room-change's lines, captured earlier, go first so the file stays in order.
            if (RoomTiming.HasPending) RoomTiming.Flush(EventAt);
            EventAt(Time.realtimeSinceStartup, Time.frameCount, line);
        }

        // With -diag-time-room-change, lines are held from a transition's start until its room is
        // ready (at most MaxHoldSeconds) and then written at once: on the PS Vita each line's file
        // append and log write took tens of milliseconds, inside the frames being measured.
        const float MaxHoldSeconds = 20f;
        static StringBuilder heldEvents, heldRemote;
        static float holdingSince = -1;

        static void BeginRoomTiming()
        {
            if (RoomTiming.Threshold < 0) return;
            RoomTiming.WrapSceneHandlers();
            HoldEvents(true);
        }

        static void EventAt(float now, int frame, string line)
        {
            string since = transitionStartAt >= 0 ? " +" + ((now - transitionStartAt) * 1000).ToString("0", Inv) + "ms" : "";
            string text = "[" + now.ToString("0.000", Inv) + "] f" + frame + " " + line + since + "\n";
            if (holdingSince >= 0)
            {
                heldEvents.Append(text);
                heldRemote.Append("[" + now.ToString("0.00", Inv) + "] " + line + since + "\n");
                return;
            }
            File.AppendAllText(System.IO.Path.Combine(outDir, "events.txt"), text);
            RemoteLine(line + since);
        }

        static void HoldEvents(bool hold)
        {
            if (hold)
            {
                if (holdingSince >= 0) return;
                heldEvents = new StringBuilder();
                heldRemote = new StringBuilder();
                holdingSince = Time.realtimeSinceStartup;
                RoomTiming.Active = true;
                return;
            }
            if (holdingSince < 0) return;
            RoomTiming.Active = false;
            if (RoomTiming.HasPending) RoomTiming.Flush(EventAt);
            holdingSince = -1;
            File.AppendAllText(System.IO.Path.Combine(outDir, "events.txt"), heldEvents.ToString());
            if (remoteLog != null) lock (remoteLog) remoteLog.Write(heldRemote.ToString());
            heldEvents = heldRemote = null;
        }

        static bool mute;  // see ReadFlags

        // -diag-stick-walk <seconds>: from 5 s after the room is ready, every frame calls
        // SystemInput.Walk with a full left or right stick (turning every <seconds>), as the
        // game's UpdateBlocking does each frame while the left stick is held: a path search and
        // a walk order a frame, as on the PS Vita with the stick, without a gamepad.
        static float stickWalkSeconds = -1, stickWalkStart = -1;
        static Action<Vector2> stickWalk;

        // -diag-place "<x>,<y>": when -diag-stick-walk starts, puts the player there first
        // (Character.Position), to walk somewhere else than at the room's entrance.
        static float[] placeAt;
        static bool placed;

        static void PlaceDriver()
        {
            if (placeAt == null || placed || questInstance == null) return;
            placed = true;
            object player = questInstance.GetType().GetProperty("Player").GetValue(questInstance, null);
            System.Reflection.PropertyInfo position = player == null ? null : player.GetType().GetProperty("Position");
            if (position == null) { Event("place: no player"); return; }
            position.SetValue(player, new Vector2(placeAt[0], placeAt[1]), null);
            Event("place " + placeAt[0].ToString(Inv) + "," + placeAt[1].ToString(Inv));
        }

        static void StickWalkDriver()
        {
            if (stickWalkSeconds <= 0 || roomReadyAt < 0 || questInstance == null) return;
            float now = Time.realtimeSinceStartup;
            if (now < roomReadyAt + 5f) return;
            if (stickWalk == null)
            {
                Type type = questInstance.GetType().Assembly.GetType("SystemInput");
                UnityEngine.Object input = type == null ? null : UnityEngine.Object.FindObjectOfType(type);
                System.Reflection.MethodInfo walk = type == null ? null : type.GetMethod("Walk",
                    System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic, null, new[] { typeof(Vector2) }, null);
                if (input == null || walk == null) { Event("stick-walk: no SystemInput.Walk"); stickWalkSeconds = -1; return; }
                PlaceDriver();
                stickWalk = (Action<Vector2>)Delegate.CreateDelegate(typeof(Action<Vector2>), input, walk);
                stickWalkStart = now;
                Event("stick-walk start");
            }
            // The game walks with the stick from PowerQuest's main loop, a coroutine resumed at the
            // end of the frame (PostLateUpdate.PlayerSendFrameComplete, otherMs). Called from here,
            // the camera pre-cull, the walk's cost was counted in FinishFrameRendering and renderMs.
            // So it runs from an end-of-frame coroutine on SystemInput, restarted when it stops (its
            // object disabled or destroyed).
            if (Time.frameCount - stickWalkFrame <= 2) return;
            MonoBehaviour host = stickWalk.Target as MonoBehaviour;
            if (host == null || !host.isActiveAndEnabled) { if (host == null) stickWalk = null; return; }
            stickWalkFrame = Time.frameCount;
            host.StartCoroutine(StickWalkLoop(++stickWalkGeneration));
        }

        static int stickWalkGeneration, stickWalkFrame = -10;

        static System.Collections.IEnumerator StickWalkLoop(int generation)
        {
            while (generation == stickWalkGeneration && stickWalk != null)
            {
                yield return new WaitForEndOfFrame();
                if (generation != stickWalkGeneration || stickWalk == null) yield break;
                stickWalkFrame = Time.frameCount;
                bool right = ((int)((Time.realtimeSinceStartup - stickWalkStart) / stickWalkSeconds) & 1) == 0;
                stickWalk(new Vector2(right ? 1f : -1f, 0f));
            }
        }

        static float sceneLoadedAt = -1;  // the last sceneLoaded (GotoRoom)

        static void SceneEvents()
        {
            UnityEngine.SceneManagement.SceneManager.sceneLoaded += (scene, mode) =>
            {
                sceneLoadedAt = Time.realtimeSinceStartup;
                Event("sceneLoaded " + scene.name + " " + mode);
            };
            UnityEngine.SceneManagement.SceneManager.sceneUnloaded += scene => Event("sceneUnloaded " + scene.name);
            UnityEngine.SceneManagement.SceneManager.activeSceneChanged += (from, to) => Event("activeScene " + to.name);
        }

        static void TrackTransition()
        {
            float now = Time.realtimeSinceStartup;
            int collections = GC.CollectionCount(0);
            // The per-frame breakdowns are formatted only for a stall: building them every frame
            // allocated more than the game does.
            bool stall = lastFrameAt > 0 && now - lastFrameAt > StallSeconds;
            if (holdingSince >= 0 && now - holdingSince > MaxHoldSeconds) HoldEvents(false);
            else if (RoomTiming.HasPending) RoomTiming.Flush(EventAt);
            string scripts = ScriptTimer.FrameReport(stall) + LoopTimer.FrameReport(stall);
            if (stall)
                Event("stall " + ((now - lastFrameAt) * 1000).ToString("0", Inv) + "ms gc " + (collections - lastCollections) +
                      " managedMB " + (GC.GetTotalMemory(false) / MB).ToString("0.0", Inv) + scripts);
            lastFrameAt = now;
            lastCollections = collections;
            if (questInstance == null)
            {
                if (Time.frameCount < nextQuestSearch) return;
                nextQuestSearch = Time.frameCount + 30;
                questInstance = FindQuest();
                if (questInstance == null) return;
                Type type = QuestType();
                transitioningField = type.GetField("m_transitioning", AnyInstance);
                levelLoadedField = type.GetField("m_levelLoadedCalled", AnyInstance);
                loopStartedField = type.GetField("m_roomLoopStarted", AnyInstance);
                fadeRatioMethod = type.GetMethod("GetFadeRatio", Type.EmptyTypes);
            }
            bool transitioning = transitioningField != null && (bool)transitioningField.GetValue(questInstance);
            bool levelLoaded = levelLoadedField == null || (bool)levelLoadedField.GetValue(questInstance);
            bool loopStarted = loopStartedField != null && (bool)loopStartedField.GetValue(questInstance);
            float fade = fadeRatioMethod == null ? 0 : (float)fadeRatioMethod.Invoke(questInstance, null);
            if (!levelLoaded && wasLevelLoaded)
            {
                BeginRoomTiming();
                transitionStartAt = now;
                Event("transition-start from " + UnityEngine.SceneManagement.SceneManager.GetActiveScene().name +
                      " backgroundLoadingPriority " + Application.backgroundLoadingPriority);
            }
            if (levelLoaded && !wasLevelLoaded)
            {
                fadingIn = true;
                Event("level-loaded");
            }
            if (transitioning != wasTransitioning) Event(transitioning ? "transitioning" : "transitioned");
            if (loopStarted && !wasLoopStarted)
            {
                roomReadyAt = now;
                Event("room-ready");
                HoldEvents(false);
            }
            if (fadingIn && fade > 0f && fade < 1f && wasFade >= 1f) Event("fade-in-start");
            if (!levelLoaded && fade >= 1f && wasFade < 1f) { Event("faded-out"); fadedOutAt = now; stuckLogged = false; }
            // A transition that has not activated its scene 5 s after the fade-out: PowerQuest's
            // waits at that point (atlas load, blocking, exit coroutines), once per transition.
            if (!levelLoaded && fadedOutAt >= 0 && !stuckLogged && now - fadedOutAt > 5f)
            {
                stuckLogged = true;
                var state = new StringBuilder("transition-stuck");
                foreach (string name in new[] { "m_loadingAtlas", "m_blocking", "m_restoring", "m_coroutineMainLoop", "m_currentSequence", "m_atlasToUnload", "m_lastAtlas" })
                {
                    System.Reflection.FieldInfo field = questInstance.GetType().GetField(name, AnyInstance);
                    object value = field == null ? "?" : field.GetValue(questInstance);
                    state.Append(" " + name + "=" + (value == null ? "null" : Convert.ToString(value, Inv)));
                }
                Event(state.ToString());
            }
            if (levelLoaded) fadedOutAt = -1;
            wasFade = fade;
            if (fadingIn && fade <= 0f)
            {
                fadingIn = false;
                Event("faded-in");
            }
            wasTransitioning = transitioning;
            wasLevelLoaded = levelLoaded;
            wasLoopStarted = loopStarted;
        }

        // Exceptions thrown (and mostly caught) since start; PowerQuest's save surrogate
        // swallows one per field a save lacks. IL2CPP may not raise the event.
        static int exceptions;

        static void CountExceptions()
        {
            AppDomain.CurrentDomain.FirstChanceException += (sender, e) => exceptions++;
        }

        // -diag-prepare-methods: at dump time, compiles every non-generic method of the game's and
        // Compat's assemblies (RuntimeHelpers.PrepareMethod), so IL a rewrite left invalid shows
        // as an event (InvalidProgramException and the like) even in methods the run never called.
        // On Mono players; IL2CPP compiled them at build time.
        static bool prepareMethods;

        static void PrepareMethods()
        {
            int prepared = 0, failed = 0;
            const System.Reflection.BindingFlags flags = AnyMember | System.Reflection.BindingFlags.DeclaredOnly;
            foreach (System.Reflection.Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                string name = assembly.GetName().Name;
                if (name != "Recovered.Drifter" && name != "Recovered.PowerTools" && name != "DrifterCompat" && name != "InControl") continue;
                Type[] types;
                try { types = assembly.GetTypes(); }
                catch (System.Reflection.ReflectionTypeLoadException e) { types = e.Types.Where(t => t != null).ToArray(); }
                foreach (Type type in types)
                {
                    if (type.ContainsGenericParameters) continue;
                    foreach (System.Reflection.MethodBase method in type.GetMethods(flags).Cast<System.Reflection.MethodBase>().Concat(type.GetConstructors(flags)))
                    {
                        if (method.IsAbstract || method.ContainsGenericParameters || method.GetMethodBody() == null) continue;
                        try
                        {
                            System.Runtime.CompilerServices.RuntimeHelpers.PrepareMethod(method.MethodHandle);
                            prepared++;
                        }
                        catch (Exception e)
                        {
                            failed++;
                            if (failed <= 20) Event("prepare-method failed " + type.FullName + "." + method.Name + ": " + e.GetType().Name + " " + e.Message);
                        }
                    }
                }
            }
            Event("prepare-methods " + prepared + " failed " + failed);
        }

        // -diag-check-allocs <n>: at dump time, asks the game the same questions with Compat's
        // allocation rules on and off (the probe gives Compat its flags again, with and without
        // -compat-alloc-original, which Compat Allocs re-reads) and counts differing answers
        // (check-allocs event): n path searches between random points of the room (FindPath,
        // IsPointInArea, GetClosestPointToArea, bit for bit), each region's distance for n/10
        // points (at least one, each asked twice), text wrapping of the game's strings (each twice
        // with the rules on: stored, then remembered), every character's transition animations,
        // GetAllClickables, GUI layout rects and line ids. Not with -compat-alloc-original on the
        // command line (PC).
        static int checkAllocs;

        static void SetCompatFlag(string flag, bool present)
        {
            Type platform = FindType("DrifterCompat.Platform");
            if (platform == null) return;
            System.Reflection.FieldInfo field = platform.GetField("ProbeArgs");
            var args = ((string[])field.GetValue(null)).Where(a => a != flag).ToList();
            if (present) args.Add(flag);
            field.SetValue(null, args.ToArray());
        }
    }
}
