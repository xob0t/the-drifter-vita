using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using UnityEngine;

// The timers the heartbeat reports: AllocTimer, LoopTimer, RoomTiming, ScriptTimer.
// Part of the parity probe (DrifterDiag); see Diag.cs.
namespace DrifterDiag
{
    // The per-method sums of AllocTimer and ScriptTimer, by the ids InjectDiag gives the timed
    // methods; the arrays grow as higher ids come. A method's start is kept until its end (0: not
    // started, so the values Begin takes are never 0).
    sealed class SlotTable
    {
        public string[] Names = new string[256];
        public long[] Started = new long[256], Total = new long[256], Frame = new long[256];
        public int[] Calls = new int[256];

        public void Begin(int id, string name, long now)
        {
            if (id >= Names.Length)
            {
                int size = Math.Max(Names.Length * 2, id + 1);
                Array.Resize(ref Names, size);
                Array.Resize(ref Started, size);
                Array.Resize(ref Total, size);
                Array.Resize(ref Frame, size);
                Array.Resize(ref Calls, size);
            }
            Names[id] = name;
            Started[id] = now;
        }

        // What passed since the method's Begin (counted as a call), or -1 without one.
        public long End(int id, long now)
        {
            if (id >= Names.Length || Started[id] == 0) return -1;
            long elapsed = now - Started[id];
            Started[id] = 0;
            Calls[id]++;
            return elapsed;
        }

        // The called methods' ids, the largest totals first (the first id of equal ones), at most count.
        public IEnumerable<int> Top(int count)
        {
            return Enumerable.Range(0, Names.Length).Where(i => Calls[i] > 0).OrderByDescending(i => Total[i]).Take(count);
        }

        public void ClearTotals()
        {
            Array.Clear(Total, 0, Total.Length);
            Array.Clear(Calls, 0, Calls.Length);
        }
    }

    // Managed memory allocated in the game's Update, LateUpdate and FixedUpdate methods
    // (-diag-alloc-scripts), from GC.GetTotalMemory before and after each (inclusive, as
    // ScriptTimer). The heap grows in chunks, so a method's figure is a share of the chunks
    // it started; over a heartbeat the shares add up. A collection inside a method drops it.
    // The heartbeat lists the 12 largest in bytes per frame.
    // Every heartbeat (with or without the flags) shows " gcs <n> gcAllocB <b>": collections of
    // generation 0 since the last heartbeat, and the managed heap's growth per frame, from
    // GC.GetTotalMemory once a frame (frames in which a collection ran are left out).
    public static class AllocTimer
    {
        public static bool Enabled;
        static long lastHeap = -1, heapGrowth;
        static int lastCollections = -1, collectionsAtReport = -1, grownFrames;

        public static void Frame()
        {
            long heap = GC.GetTotalMemory(false);
            int collections = GC.CollectionCount(0);
            if (collectionsAtReport < 0) collectionsAtReport = collections;
            if (lastHeap >= 0 && collections == lastCollections)
            {
                if (heap > lastHeap) heapGrowth += heap - lastHeap;
                grownFrames++;
            }
            lastHeap = heap;
            lastCollections = collections;
        }

        public static string GcReport()
        {
            int collections = GC.CollectionCount(0);
            string text = " gcs " + (collectionsAtReport < 0 ? 0 : collections - collectionsAtReport) +
                          " gcAllocB " + (grownFrames == 0 ? 0 : heapGrowth / grownFrames);
            collectionsAtReport = collections;
            heapGrowth = 0;
            grownFrames = 0;
            return text;
        }

        // The heap's size is taken as one more than it is, so a start is never 0 (SlotTable).
        static readonly SlotTable slots = new SlotTable();

        public static void Begin(int id, string name)
        {
            if (Enabled) slots.Begin(id, name, GC.GetTotalMemory(false) + 1);
        }

        public static void End(int id)
        {
            if (!Enabled) return;
            long allocated = slots.End(id, GC.GetTotalMemory(false) + 1);
            if (allocated > 0) slots.Total[id] += allocated;
        }

        public static string Report(int frames)
        {
            if (!Enabled || frames <= 0) return "";
            var text = new StringBuilder(" allocB");
            foreach (int i in slots.Top(12)) text.Append(" " + slots.Names[i] + ":" + (slots.Total[i] / frames));
            slots.ClearTotals();
            return text.ToString();
        }
    }

    // Time per engine step, from Unity's player loop (-diag-time-loop): a timestamp system is
    // inserted before every subsystem of every phase (scripts, animation, audio, canvases,
    // rendering, present...), and the time up to the next timestamp is that subsystem's.
    // Works in release players, which have no profiler markers.
    public static class LoopTimer
    {
        public static bool Enabled;
        sealed class Stamp { }
        static readonly List<string> names = new List<string>();
        static long[] total = new long[0], frame = new long[0];
        static long last;
        static int current = -1;

        public static void Install()
        {
            var loop = UnityEngine.Experimental.LowLevel.PlayerLoop.GetDefaultPlayerLoop();
            // The translated build's Compat adds its own steps (DrifterCompat.LoopSteps); 2018.2
            // cannot read the loop back, so start from the one Compat set.
            Type steps = Probe.FindType("DrifterCompat.LoopSteps");
            if (steps != null && steps.GetField("Installed") != null && (bool)steps.GetField("Installed").GetValue(null))
                loop = (UnityEngine.Experimental.LowLevel.PlayerLoopSystem)steps.GetField("Current").GetValue(null);
            var phases = loop.subSystemList;
            for (int p = 0; p < phases.Length; p++)
            {
                var timed = new List<UnityEngine.Experimental.LowLevel.PlayerLoopSystem>();
                var subs = phases[p].subSystemList ?? new UnityEngine.Experimental.LowLevel.PlayerLoopSystem[0];
                foreach (var sub in subs)
                {
                    timed.Add(Marker(phases[p].type.Name + "." + (sub.type == null ? "?" : sub.type.Name)));
                    timed.Add(sub);
                }
                timed.Add(Marker(phases[p].type.Name + ".(end)"));
                phases[p].subSystemList = timed.ToArray();
            }
            loop.subSystemList = phases;
            total = new long[names.Count];
            frame = new long[names.Count];
            UnityEngine.Experimental.LowLevel.PlayerLoop.SetPlayerLoop(loop);
        }

        static UnityEngine.Experimental.LowLevel.PlayerLoopSystem Marker(string name)
        {
            int index = names.Count;
            names.Add(name);
            var system = new UnityEngine.Experimental.LowLevel.PlayerLoopSystem();
            system.type = typeof(Stamp);
            system.updateDelegate = () => Mark(index);
            return system;
        }

        static void Mark(int index)
        {
            long now = System.Diagnostics.Stopwatch.GetTimestamp();
            if (current >= 0)
            {
                total[current] += now - last;
                frame[current] += now - last;
                if (RoomTiming.Threshold >= 0) RoomTiming.Step(names[current], now - last);
            }
            current = index;
            last = now;
        }

        static string Top(long[] sums, double toMs, int count, string format)
        {
            var text = new StringBuilder();
            foreach (int i in Enumerable.Range(0, sums.Length).OrderByDescending(i => sums[i]).Take(count))
                if (sums[i] * toMs >= 0.05) text.Append(" " + names[i] + ":" + (sums[i] * toMs).ToString(format, Probe.Inv));
            return text.ToString();
        }

        public static string Report(int frames)
        {
            if (!Enabled || frames <= 0 || total.Length == 0) return "";
            string text = " loop" + Top(total, 1000.0 / System.Diagnostics.Stopwatch.Frequency / frames, 14, "0.00");
            Array.Clear(total, 0, total.Length);
            return text;
        }

        public static string FrameReport(bool format)
        {
            if (!Enabled || frame.Length == 0) return "";
            string text = format ? " loop" + Top(frame, 1000.0 / System.Diagnostics.Stopwatch.Frequency, 5, "0") : "";
            Array.Clear(frame, 0, frame.Length);
            return text;
        }
    }

    // -diag-time-room-change [ms] (default 5) splits the frames of a room change: it turns on
    // -diag-time-loop and -diag-time-scripts and writes an event, stamped with the time it ended,
    // for every player-loop step ("step <name> <ms>ms gc <collections in it>"), timed script method
    // ("script <Type.Method> <ms>ms": Update and the like, Awake, OnEnable, Start, OnDisable,
    // OnDestroy) and scene-event handler ("handler <event> <Type.Method> <ms>ms gc <n>", each
    // handler of sceneUnloaded, activeSceneChanged and sceneLoaded on its own, wrapped at each
    // transition start; test only: a handler removed after that stays registered) that took at
    // least that long, from a transition's start until its room is ready. These lines and the probe's
    // other events are held and written at once when the room is ready (Probe.HoldEvents), so the
    // file writes (tens of ms each on the PS Vita) do not count; stall lines list 12 methods, not 3.
    public static class RoomTiming
    {
        public static long Threshold = -1; // Stopwatch ticks; off below 0
        public static bool Active;         // during a room change (the probe's held events)
        struct Line { public float At; public int Frame; public string Text; }
        static readonly List<Line> pending = new List<Line>();
        public static bool HasPending { get { return pending.Count > 0; } }
        static int stepCollections;

        public static void Enable(double ms)
        {
            Threshold = (long)(ms * System.Diagnostics.Stopwatch.Frequency / 1000.0);
            stepCollections = GC.CollectionCount(0);
        }

        public static void Step(string name, long elapsed)
        {
            int collections = GC.CollectionCount(0);
            if (Active && elapsed >= Threshold) Capture("step " + name, elapsed, collections - stepCollections);
            stepCollections = collections;
        }

        public static void Capture(string what, long elapsed, int collections)
        {
            if (!Active) return;
            pending.Add(new Line
            {
                At = Time.realtimeSinceStartup, Frame = Time.frameCount,
                Text = what + " " + (elapsed * 1000.0 / System.Diagnostics.Stopwatch.Frequency).ToString("0.0", Probe.Inv) + "ms" +
                       (collections >= 0 ? " gc " + collections : ""),
            });
        }

        public static void Flush(Action<float, int, string> write)
        {
            var lines = pending.ToArray();
            pending.Clear();
            foreach (Line line in lines) write(line.At, line.Frame, line.Text);
        }

        sealed class Wrapper
        {
            public string Label;
            public UnityEngine.Events.UnityAction<UnityEngine.SceneManagement.Scene> Unloaded;
            public UnityEngine.Events.UnityAction<UnityEngine.SceneManagement.Scene, UnityEngine.SceneManagement.Scene> Active;
            public UnityEngine.Events.UnityAction<UnityEngine.SceneManagement.Scene, UnityEngine.SceneManagement.LoadSceneMode> Loaded;

            long start;
            int collections;

            void Begin() { collections = GC.CollectionCount(0); start = System.Diagnostics.Stopwatch.GetTimestamp(); }

            void End()
            {
                long elapsed = System.Diagnostics.Stopwatch.GetTimestamp() - start;
                // Collections before the handler in the same loop step: where in the step a collection fell.
                if (elapsed >= Threshold) Capture(Label + " (gc earlier in step " + (collections - stepCollections) + ")", elapsed, GC.CollectionCount(0) - collections);
            }

            public void OnUnloaded(UnityEngine.SceneManagement.Scene scene) { Begin(); Unloaded(scene); End(); }
            public void OnActive(UnityEngine.SceneManagement.Scene from, UnityEngine.SceneManagement.Scene to) { Begin(); Active(from, to); End(); }
            public void OnLoaded(UnityEngine.SceneManagement.Scene scene, UnityEngine.SceneManagement.LoadSceneMode mode) { Begin(); Loaded(scene, mode); End(); }
        }

        static string Label(string field, Delegate handler)
        {
            Type type = handler.Method.DeclaringType;
            // Lambdas live in compiler classes nested in the class that wrote them.
            while (type != null && type.DeclaringType != null && type.Name.StartsWith("<")) type = type.DeclaringType;
            return "handler " + field + " " + (type == null ? "?" : type.FullName) + "." + handler.Method.Name;
        }

        static void Wrap(string field, Func<Delegate, Wrapper, Delegate> wrap)
        {
            System.Reflection.FieldInfo info = typeof(UnityEngine.SceneManagement.SceneManager).GetField(field,
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Public);
            var current = info == null ? null : info.GetValue(null) as Delegate;
            if (current == null) return;
            var list = new List<Delegate>();
            bool changed = false;
            foreach (Delegate handler in current.GetInvocationList())
            {
                // The probe's own handlers (its event lines) and those already wrapped stay as they are.
                if (handler.Target is Wrapper || handler.Method.DeclaringType.Assembly == typeof(RoomTiming).Assembly) { list.Add(handler); continue; }
                list.Add(wrap(handler, new Wrapper { Label = Label(field, handler) }));
                changed = true;
            }
            if (changed) info.SetValue(null, Delegate.Combine(list.ToArray()));
        }

        public static void WrapSceneHandlers()
        {
            Wrap("sceneUnloaded", (h, w) =>
            {
                w.Unloaded = (UnityEngine.Events.UnityAction<UnityEngine.SceneManagement.Scene>)h;
                return new UnityEngine.Events.UnityAction<UnityEngine.SceneManagement.Scene>(w.OnUnloaded);
            });
            Wrap("activeSceneChanged", (h, w) =>
            {
                w.Active = (UnityEngine.Events.UnityAction<UnityEngine.SceneManagement.Scene, UnityEngine.SceneManagement.Scene>)h;
                return new UnityEngine.Events.UnityAction<UnityEngine.SceneManagement.Scene, UnityEngine.SceneManagement.Scene>(w.OnActive);
            });
            Wrap("sceneLoaded", (h, w) =>
            {
                w.Loaded = (UnityEngine.Events.UnityAction<UnityEngine.SceneManagement.Scene, UnityEngine.SceneManagement.LoadSceneMode>)h;
                return new UnityEngine.Events.UnityAction<UnityEngine.SceneManagement.Scene, UnityEngine.SceneManagement.LoadSceneMode>(w.OnLoaded);
            });
        }
    }

    // Time spent in the game's Update, LateUpdate and FixedUpdate methods, for players
    // without profiler markers (the PS Vita's development player crashes on this game's
    // data). InjectDiag --time-methods wraps each such method in Begin/End; -diag-time-scripts
    // turns the timing on and the heartbeat lists the costliest methods in ms per frame
    // (inclusive: a method's time includes what it calls). The total, scriptsMs, counts only
    // the outermost timed calls, so nested ones are not counted twice.
    public static class ScriptTimer
    {
        public static bool Enabled;
        static readonly SlotTable slots = new SlotTable();
        // The outermost timed call running (-1: none) and the outermost calls' time. A call that
        // throws never ends; the next call of the same method takes its place.
        static int outer = -1;
        static long outerTotal, outerFrame;

        public static void Begin(int id, string name)
        {
            if (!Enabled) return;
            if (outer < 0) outer = id;
            slots.Begin(id, name, System.Diagnostics.Stopwatch.GetTimestamp());
        }

        public static void End(int id)
        {
            if (!Enabled) return;
            long elapsed = slots.End(id, System.Diagnostics.Stopwatch.GetTimestamp());
            if (elapsed < 0) return;
            slots.Total[id] += elapsed;
            slots.Frame[id] += elapsed;
            if (id == outer)
            {
                outer = -1;
                outerTotal += elapsed;
                outerFrame += elapsed;
            }
            if (RoomTiming.Threshold >= 0 && elapsed >= RoomTiming.Threshold) RoomTiming.Capture("script " + slots.Names[id], elapsed, -1);
        }

        // -diag-time-calls: the call sites InjectDiag's DetailCallSites lists, timed one by one
        // ("Type.Method>Callee.Name"); off otherwise, as the timing itself costs on the PS Vita.
        public static bool CallsEnabled;

        public static void CallBegin(int id, string name)
        {
            if (CallsEnabled) Begin(id, name);
        }

        public static void CallEnd(int id)
        {
            if (CallsEnabled) End(id);
        }

        // The script time since the last call (one frame), for the probe's stall events (empty
        // unless format): the total and the costliest methods, 3 of them (12 with
        // -diag-time-room-change). Clears the per-frame sums.
        public static string FrameReport(bool format)
        {
            if (!Enabled) return "";
            long[] frame = slots.Frame;
            long total = outerFrame;
            outerFrame = 0;
            if (!format)
            {
                Array.Clear(frame, 0, frame.Length);
                return "";
            }
            double toMs = 1000.0 / System.Diagnostics.Stopwatch.Frequency;
            var text = new StringBuilder(" scriptsMs " + (total * toMs).ToString("0.0", Probe.Inv));
            foreach (int i in Enumerable.Range(0, frame.Length).Where(i => frame[i] != 0).OrderByDescending(i => frame[i])
                         .Take(RoomTiming.Threshold >= 0 ? 12 : 3))
                text.Append(" " + slots.Names[i] + ":" + (frame[i] * toMs).ToString("0.0", Probe.Inv));
            Array.Clear(frame, 0, frame.Length);
            return text.ToString();
        }

        public static string Report(int frames)
        {
            if (!Enabled || frames <= 0) return "";
            double toMs = 1000.0 / System.Diagnostics.Stopwatch.Frequency / frames;
            var text = new StringBuilder(" scriptsMs " + (outerTotal * toMs).ToString("0.0", Probe.Inv));
            outerTotal = 0;
            foreach (int i in slots.Top(CallsEnabled ? 120 : 24))
                text.Append(" " + slots.Names[i] + ":" + (slots.Total[i] * toMs).ToString("0.00", Probe.Inv) + "/" +
                            ((float)slots.Calls[i] / frames).ToString("0.#", Probe.Inv));
            slots.ClearTotals();
            return text.ToString();
        }
    }
}
