using System;
using System.Collections.Generic;
using UnityEngine;

// Allocations: reused closures, delegates, arrays and lists, and the GUI reshow rule.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // Per-frame managed allocations (the ReduceAllocations passes, rewriter/Allocs.cs; docs/pipeline.md
    // "Allocations").
    // The PS Vita's Boehm collector stops the game for about 200 ms per collection of a 40 MB
    // heap, and the game's scripts made 3-18 KB of garbage a frame (closures and delegates for
    // List/Array searches, lists and arrays made per call, collider point copies, text wrapping),
    // which the console feels as stutters every few seconds while walking. Each rule below gives
    // the game the same values it got before, without the allocation; all are main-thread only,
    // take no locks, and drop their references when a scene unloads. Switches (command line, or
    // the probe's flags on the PS Vita): -compat-alloc-original turns all of them off, and each
    // has its own (named at each class).
    public static class Allocs
    {
        static string[] configuredFor;
        static bool closures, lambdas, childArrays, scratchLists, colliderPoints, clickables, wrapMemo, pathPool, numberText, regexes, vertices, concats, textIds;

        static void Check()
        {
            if (!Platform.NewArgs(ref configuredFor)) return;
            bool on = !Platform.HasArg("-compat-alloc-original");
            closures = on && !Platform.HasArg("-compat-no-reuse-closures");
            lambdas = on && !Platform.HasArg("-compat-no-cached-lambdas");
            childArrays = on && !Platform.HasArg("-compat-no-child-arrays");
            scratchLists = on && !Platform.HasArg("-compat-no-scratch-lists");
            colliderPoints = on && !Platform.HasArg("-compat-no-collider-points");
            clickables = on && !Platform.HasArg("-compat-no-reuse-clickables");
            wrapMemo = on && !Platform.HasArg("-compat-no-wrap-memo");
            pathPool = on && !Platform.HasArg("-compat-no-path-pool");
            numberText = on && !Platform.HasArg("-compat-no-number-text");
            regexes = on && !Platform.HasArg("-compat-no-regex-cache");
            vertices = on && !Platform.HasArg("-compat-no-sprite-vertices");
            concats = on && !Platform.HasArg("-compat-no-concat-memo");
            textIds = on && !Platform.HasArg("-compat-no-text-ids");
        }

        public static bool ReuseClosures { get { Check(); return closures; } }
        public static bool CacheLambdas { get { Check(); return lambdas; } }
        public static bool PoolChildArrays { get { Check(); return childArrays; } }
        public static bool UseScratchLists { get { Check(); return scratchLists; } }
        public static bool CacheColliderPoints { get { Check(); return colliderPoints; } }
        public static bool ReuseClickables { get { Check(); return clickables; } }
        public static bool MemoizeWrap { get { Check(); return wrapMemo; } }
        public static bool PoolPaths { get { Check(); return pathPool; } }
        public static bool CacheNumberText { get { Check(); return numberText; } }
        public static bool CacheRegexes { get { Check(); return regexes; } }
        public static bool CacheSpriteVertices { get { Check(); return vertices; } }
        public static bool MemoizeConcats { get { Check(); return concats; } }
        public static bool MemoizeTextIds { get { Check(); return textIds; } }

        // When a scene unloads (SceneUnloads).
        public static void Clear()
        {
            ReusedClosures.Clear();
            CachedLambdas.Clear();
            ChildArrays.Clear();
            ScratchLists.Clear();
            ColliderPoints.Clear();
            WrapMemo.Clear();
            PathPool.Clear();
            SpriteVertices.Clear();
            Concats.Clear();
            TextIds.Clear();
        }

        // Grows a per-site table to hold the site.
        internal static void Fit<T>(ref T[] table, int site)
        {
            if (site < table.Length) return;
            Array.Resize(ref table, Math.Max(site + 1, table.Length * 2));
        }
    }

    // A compiler-made closure (the class holding a method's captured variables, made at the
    // start of their scope) whose instance never leaves its method: it is only read and written
    // there and in its own lambdas, and its lambdas are only made into delegates passed straight
    // to List/Array methods that do not keep them (Find, FindIndex, Exists, RemoveAll, ForEach,
    // Sort and the like); the method is not an iterator, and the scope is not inside a loop. Such
    // a closure is made once per site and used again by later calls (Take/Keep), with the
    // delegates made from it (Delegate/KeepDelegate). C# requires a captured variable to be
    // assigned before it is read (in the method or in a lambda made from it), so the values the
    // closure held from the last call are never seen. A call that finds the site's closure in use
    // (the method re-entered) makes its own; Release, before every return, frees the site.
    // A method that throws leaves it in use, and its later calls make their own, as before.
    // Main thread only. Examples: SystemInput.UpdateInput (RemoveAll and Find on the selectable
    // things), PropComponent.UpdateBaseline (two Array.ForEach per move: six props a frame in
    // Drowning), PowerQuest.ProcessClick. -compat-no-reuse-closures.
    public static class ReusedClosures
    {
        static object[] instances = new object[0], owners = new object[0], delegates = new object[0];
        static bool[] busy = new bool[0];

        public static object Take(int site)
        {
            if (site >= instances.Length || busy[site] || !MainThread.Current || !Allocs.ReuseClosures) return null;
            object instance = instances[site];
            if (instance != null) busy[site] = true;
            return instance;
        }

        // A closure just made (none to take): kept for the site unless it is in use.
        public static object Keep(object instance, int site)
        {
            if (!MainThread.Current || !Allocs.ReuseClosures) return instance;
            Allocs.Fit(ref instances, site);
            Allocs.Fit(ref busy, site);
            if (busy[site]) return instance;
            instances[site] = instance;
            busy[site] = true;
            return instance;
        }

        public static void Release(object instance, int site)
        {
            if (instance != null && site < instances.Length && ReferenceEquals(instances[site], instance)) busy[site] = false;
        }

        // The delegate this site made last, if it was made from this closure (same method too).
        public static object Delegate(object closure, int site)
        {
            if (closure == null || site >= owners.Length || !ReferenceEquals(owners[site], closure) || !MainThread.Current || !Allocs.ReuseClosures)
                return null;
            return delegates[site];
        }

        public static object KeepDelegate(object made, object closure, int site)
        {
            if (!MainThread.Current || !Allocs.ReuseClosures) return made;
            Allocs.Fit(ref owners, site);
            Allocs.Fit(ref delegates, site);
            owners[site] = closure;
            delegates[site] = made;
            return made;
        }

        public static void Clear()
        {
            Array.Clear(instances, 0, instances.Length);
            Array.Clear(busy, 0, busy.Length);
            Array.Clear(owners, 0, owners.Length);
            Array.Clear(delegates, 0, delegates.Length);
        }
    }

    // A delegate to one of the object's own methods (a lambda that captures only "this", or a
    // method group) made at every call and passed straight to a List/Array method that does not
    // keep it: Pathfinder.UpdateObstacles (List.ForEach, run by every IsPointInArea, which
    // GetClosestPointToArea calls twice per polygon edge: 100-200 delegates a call while walking
    // with the stick), GuiControl.Alpha's two Array.ForEach (GuiBgRed sets its fade every
    // frame), SystemInput.UpdateInventorySelection. The delegate made for the same object at
    // the same site is the same delegate (same target, same method), so the last ones made
    // there (four objects per site) are given again. Main thread only. -compat-no-cached-lambdas.
    public static class CachedLambdas
    {
        const int Ways = 4;
        static object[] targets = new object[0], delegates = new object[0];
        static int[] next = new int[0];

        public static object Get(object target, int site)
        {
            int at = site * Ways;
            if (target == null || at + Ways > targets.Length || !MainThread.Current || !Allocs.CacheLambdas) return null;
            for (int i = 0; i < Ways; i++)
                if (ReferenceEquals(targets[at + i], target)) return delegates[at + i];
            return null;
        }

        public static object Keep(object made, object target, int site)
        {
            if (target == null || !MainThread.Current || !Allocs.CacheLambdas) return made;
            Allocs.Fit(ref next, site);
            Allocs.Fit(ref targets, site * Ways + Ways - 1);
            Allocs.Fit(ref delegates, site * Ways + Ways - 1);
            int slot = next[site];
            next[site] = (slot + 1) % Ways;
            targets[site * Ways + slot] = target;
            delegates[site * Ways + slot] = made;
            return made;
        }

        public static void Clear()
        {
            Array.Clear(targets, 0, targets.Length);
            Array.Clear(delegates, 0, delegates.Length);
        }
    }

    // GetComponentsInChildren<T>(includeInactive) at call sites whose array is only read in the
    // method (ChildArraySites in rewriter/Allocs.cs: PowerQuest.UpdateRegions, each character's
    // PowerSprites every frame; GuiControl.Alpha, a GUI control's sprites and texts). The engine
    // fills a list with the same components in the same order (the same internal search as the
    // array form), and the site's array of that length is filled from it and lent to the method
    // until its next call. -compat-no-child-arrays.
    public static class ChildArrays
    {
        static readonly CacheClears clears = new CacheClears();
        // Lengths a site keeps arrays of before it drops them all: only a bound for a site whose
        // count of components keeps changing.
        const int MaxLengths = 64;

        static class Of<T>
        {
            public static readonly List<T> Found = new List<T>();
            public static Dictionary<int, T[]>[] BySite = clears.Add(new Dictionary<int, T[]>[0], () =>
            {
                for (int i = 0; i < BySite.Length; i++) if (BySite[i] != null) BySite[i].Clear();
            });
        }

        // Calls of each rewritten method in progress (Enter at its start, Exit before each
        // return): the arrays are lent only to the outermost call, so a method re-entered while
        // it reads its array gets new arrays. A method that throws stays entered, and its later
        // calls get new arrays, as before.
        static int[] depth = new int[0];

        public static void Enter(int method)
        {
            if (!MainThread.Current) return;
            Allocs.Fit(ref depth, method);
            depth[method]++;
        }

        public static void Exit(int method)
        {
            if (MainThread.Current && method < depth.Length && depth[method] > 0) depth[method]--;
        }

        static bool Lend(int method)
        {
            return MainThread.Current && method < depth.Length && depth[method] == 1 && Allocs.PoolChildArrays;
        }

        public static T[] InChildren<T>(Component owner, bool includeInactive, int site, int method)
        {
            if (owner == null || !Lend(method)) return owner.GetComponentsInChildren<T>(includeInactive);
            owner.GetComponentsInChildren(includeInactive, Of<T>.Found);
            return Fill<T>(site);
        }

        public static T[] InChildrenOf<T>(GameObject owner, bool includeInactive, int site, int method)
        {
            if (owner == null || !Lend(method)) return owner.GetComponentsInChildren<T>(includeInactive);
            owner.GetComponentsInChildren(includeInactive, Of<T>.Found);
            return Fill<T>(site);
        }

        static T[] Fill<T>(int site)
        {
            List<T> found = Of<T>.Found;
            Allocs.Fit(ref Of<T>.BySite, site);
            Dictionary<int, T[]> byLength = Of<T>.BySite[site];
            if (byLength == null) Of<T>.BySite[site] = byLength = new Dictionary<int, T[]>();
            T[] array;
            if (!byLength.TryGetValue(found.Count, out array))
            {
                if (byLength.Count >= MaxLengths) byLength.Clear();
                byLength[found.Count] = array = new T[found.Count];
            }
            found.CopyTo(array);
            found.Clear();
            return array;
        }

        public static void Clear()
        {
            clears.Run();
        }
    }

    // new List<T>() kept in a local that never leaves its method (only the list's own methods
    // are called on it, and nothing keeps it: ScratchListMethods in rewriter/Allocs.cs, the
    // pathfinder's FindPath, EvaluateDijkstra and GetClosestPointToArea, two to four times a
    // frame while walking with the stick). The site's list is given out empty instead, and
    // emptied again before every return of the method (Release); a call that finds it in use
    // gets a new one. Main thread only. -compat-no-scratch-lists.
    public static class ScratchLists
    {
        static object[] lists = new object[0];
        static bool[] busy = new bool[0];

        public static List<T> Take<T>(int site)
        {
            if (!MainThread.Current || !Allocs.UseScratchLists) return new List<T>();
            Allocs.Fit(ref lists, site);
            Allocs.Fit(ref busy, site);
            if (busy[site]) return new List<T>();
            var list = lists[site] as List<T>;
            if (list == null) lists[site] = list = new List<T>();
            busy[site] = true;
            return list;
        }

        // A list kept in a reused closure's field (see ReusedClosures), made new at each call of
        // the method: the previous call's list, which only that field held, emptied.
        public static List<T> Refill<T>(List<T> previous)
        {
            if (previous == null || !MainThread.Current || !Allocs.ReuseClosures) return new List<T>();
            previous.Clear();
            return previous;
        }

        public static void Release(object list, int site)
        {
            if (list == null || site >= lists.Length || !ReferenceEquals(lists[site], list)) return;
            ((System.Collections.IList)list).Clear();
            busy[site] = false;
        }

        public static void Clear()
        {
            for (int i = 0; i < lists.Length; i++)
                if (lists[i] != null && !busy[i]) lists[i] = null;
        }
    }

    // PolygonCollider2D.points copies the collider's first path into a new array on every read.
    // RegionComponent.GetDistanceIntoRegion reads it for every region with a fade distance and
    // every character standing in it, every frame (PowerQuest.UpdateRegions, CharacterRimlight),
    // and only reads the copy (RegionPolyUtil.CalcDistToEdge). Its copy is kept per collider
    // (instance id) and given again until the game sets that collider's points, path or path
    // count (every such call in the game's code goes through Forget) or a scene unloads. Colliders
    // with auto tiling (the engine reshapes them) are not kept. The array is only handed to that
    // one reader. -compat-no-collider-points.
    public static class ColliderPoints
    {
        static readonly Dictionary<int, Vector2[]> points = new Dictionary<int, Vector2[]>();
        // Colliders kept before all are dropped: only a bound for a scene that runs long.
        const int MaxColliders = 1024;

        public static Vector2[] Get(PolygonCollider2D collider)
        {
            if (collider == null || !MainThread.Current || !Allocs.CacheColliderPoints) return collider.points;
            int id = collider.GetInstanceID();
            Vector2[] found;
            if (points.TryGetValue(id, out found)) return found;
            found = collider.points;
            if (!collider.autoTiling)
            {
                if (points.Count >= MaxColliders) points.Clear();
                points[id] = found;
            }
            return found;
        }

        public static void Forget(PolygonCollider2D collider)
        {
            if (!ReferenceEquals(collider, null) && points.Count > 0) points.Remove(collider.GetInstanceID());
        }

        public static void SetPath(PolygonCollider2D collider, int index, Vector2[] path)
        {
            Forget(collider);
            collider.SetPath(index, path);
        }

        public static void SetPoints(PolygonCollider2D collider, Vector2[] value)
        {
            Forget(collider);
            collider.points = value;
        }

        public static void SetPathCount(PolygonCollider2D collider, int count)
        {
            Forget(collider);
            collider.pathCount = count;
        }

        public static void Clear()
        {
            points.Clear();
        }
    }

    // PowerQuest.UpdateGuiVisibility (every frame) first sets each GUI's HiddenBySystem from the
    // GUIs obscuring it (HideObscuredGuis, cutscenes), then hides every GUI named in a visible
    // GUI's HideSpecificGuis. A GUI in such a list that nothing obscures is shown by the first
    // pass and hidden again by the second (Gui.OnVisibilityChanged: GameObject.SetActive true,
    // then false), in every frame such a GUI shows: the inventory wheel hides ControllerInv and
    // ControllerTopics, the title menu ControllerTute, the chapter end seven bars. Nothing renders,
    // animates or steps physics in between, so what the pair leaves is what the scripts' OnEnable
    // and OnDisable do (GuiControllerTopics.OnDisable resets the description override and the
    // info bar's offset and wrap width); the engine's part (renderers, text meshes, Animators,
    // colliders entering and leaving the scene) is undone at once.
    // The downgrader (SkipGuiReshows) marks the GUIs the second pass will hide (BeginPass, Mark)
    // before the first pass, and sends the first pass's HiddenBySystem = false of a marked GUI
    // that is visible, hidden and does not pause the game to Reshow. When the GUI's hierarchy
    // (the objects the activation would reach) holds only engine components without script-
    // visible activation effects (Transform, sprite and mesh renderers, TextMesh, Animator, 2D
    // colliders) and MonoBehaviours that have had Awake (they were active at the GUI's last real
    // deactivation, SetActive) and whose OnEnable/OnDisable are known (below; GuiComponent's
    // calls the GUI script's OnShow, so a script with one is not), Reshow calls those behaviours'
    // OnEnable, then their OnDisable, in hierarchy order (an exception is logged, as the engine
    // does), and the GameObject stays inactive; the second pass's assignment then changes
    // nothing. Otherwise the original assignment runs. The known callbacks touch only the GUI's
    // own objects (AlignToScreen, FitToObject and GUIContain lay them out again, QuestText sets
    // its offset; all of it is done again when the GUI really shows) or do the same while the
    // GUI is inactive (QuestText's text registration, GuiComponent with no OnShow,
    // GuiControllerTopics.Hide, GuiControllerTute's unpause). -compat-no-gui-reshow.
    public static class GuiReshows
    {
        static string[] configuredFor;
        static bool on;
        public static int Count;  // replayed pairs, for the probe

        static bool On
        {
            get
            {
                if (Platform.NewArgs(ref configuredFor)) on = !Platform.HasArg("-compat-no-gui-reshow");
                return on;
            }
        }

        static readonly string[] knownCallbacks =
        {
            "PowerTools.Quest.GuiComponent", "PowerTools.Quest.QuestText", "PowerTools.Quest.AlignToScreen",
            "PowerTools.Quest.FitToObject", "PowerTools.Quest.GUIContain", "PowerTools.Quest.GuiControllerTopics",
            "PowerTools.Quest.GuiControllerTute",
        };

        static readonly List<object> marked = new List<object>();

        public static void BeginPass()
        {
            marked.Clear();
        }

        public static void Mark(object gui)
        {
            if (gui == null || !MainThread.Current) return;
            for (int i = 0; i < marked.Count; i++)
                if (ReferenceEquals(marked[i], gui)) return;
            marked.Add(gui);
        }

        public static bool Marked(object gui)
        {
            if (!MainThread.Current || !On) return false;
            for (int i = 0; i < marked.Count; i++)
                if (ReferenceEquals(marked[i], gui)) return true;
            return false;
        }

        // Behaviours (instance ids) on objects active in the hierarchy at a GUI root's last real
        // deactivation: they have had Awake. Kept for the session: instance ids only (nothing is
        // held), one set per GUI root the game has hidden.
        static readonly Dictionary<int, HashSet<int>> awake = new Dictionary<int, HashSet<int>>();
        static readonly List<MonoBehaviour> behaviours = new List<MonoBehaviour>();

        // Gui.OnVisibilityChanged's m_instance.gameObject.SetActive(visible).
        public static void SetActive(GameObject root, bool value)
        {
            if (!value && root != null && root.activeInHierarchy && MainThread.Current && On)
            {
                var ids = new HashSet<int>();
                root.GetComponentsInChildren<MonoBehaviour>(false, behaviours);
                for (int i = 0; i < behaviours.Count; i++)
                    if (!ReferenceEquals(behaviours[i], null)) ids.Add(behaviours[i].GetInstanceID());
                behaviours.Clear();
                awake[root.GetInstanceID()] = ids;
            }
            root.SetActive(value);
        }

        sealed class Callbacks
        {
            public bool Allowed;
            public System.Reflection.MethodInfo OnEnable, OnDisable;
        }

        static readonly Dictionary<Type, Callbacks> callbacks = new Dictionary<Type, Callbacks>();
        static readonly Dictionary<Type, bool> scriptsWithShow = new Dictionary<Type, bool>();
        static readonly Dictionary<int, Action> enables = new Dictionary<int, Action>(), disables = new Dictionary<int, Action>();
        static readonly List<Component> components = new List<Component>();
        static readonly List<MonoBehaviour> notified = new List<MonoBehaviour>();

        static System.Reflection.MethodInfo Message(Type type, string name)
        {
            const System.Reflection.BindingFlags flags = System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public |
                                                         System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.DeclaredOnly;
            for (Type t = type; t != null && t != typeof(MonoBehaviour); t = t.BaseType)
            {
                System.Reflection.MethodInfo method = t.GetMethod(name, flags, null, Type.EmptyTypes, null);
                if (method != null) return method;
            }
            return null;
        }

        static Callbacks CallbacksOf(Type type)
        {
            Callbacks found;
            if (callbacks.TryGetValue(type, out found)) return found;
            found = new Callbacks { OnEnable = Message(type, "OnEnable"), OnDisable = Message(type, "OnDisable") };
            found.Allowed = (found.OnEnable == null && found.OnDisable == null) || Array.IndexOf(knownCallbacks, type.FullName) >= 0;
            callbacks[type] = found;
            return found;
        }

        static bool SafeEngineComponent(Component component)
        {
            return component is Transform || component is SpriteRenderer || component is MeshRenderer || component is MeshFilter ||
                   component is TextMesh || component is Animator || component is BoxCollider2D || component is PolygonCollider2D ||
                   component is CircleCollider2D;
        }

        // Whether activating root would make this object active in the hierarchy.
        static bool WouldActivate(Transform transform, Transform root)
        {
            for (Transform at = transform; at != root; at = at.parent)
                if (at == null || !at.gameObject.activeSelf) return false;
            return true;
        }

        static void Call(Dictionary<int, Action> cache, MonoBehaviour behaviour, System.Reflection.MethodInfo method)
        {
            if (method == null) return;
            int id = behaviour.GetInstanceID();
            Action action;
            if (!cache.TryGetValue(id, out action))
                cache[id] = action = (Action)Delegate.CreateDelegate(typeof(Action), behaviour, method);
            try { action(); }
            catch (Exception e) { Debug.LogException(e, behaviour); }
        }

        // The first pass's HiddenBySystem = false of a marked, visible, hidden GUI that does not
        // pause the game: true when the pair was replayed (the GUI stays hidden).
        public static bool Reshow(Component instance, object script)
        {
            if (instance == null || !MainThread.Current || !On) return false;
            GameObject root = instance.gameObject;
            if (root.activeSelf) return false;
            Transform parent = root.transform.parent;
            if (parent != null && !parent.gameObject.activeInHierarchy) return false;  // SetActive(true) would notify nothing
            HashSet<int> seen;
            if (!awake.TryGetValue(root.GetInstanceID(), out seen) || HasShow(script) || !Collect(root, seen)) return false;
            for (int i = 0; i < notified.Count; i++) Call(enables, notified[i], CallbacksOf(notified[i].GetType()).OnEnable);
            for (int i = 0; i < notified.Count; i++)
                if (notified[i] != null) Call(disables, notified[i], CallbacksOf(notified[i].GetType()).OnDisable);
            notified.Clear();
            Count++;
            return true;
        }

        // Whether the GUI script has an OnShow (GuiComponent's OnEnable calls it).
        static bool HasShow(object script)
        {
            if (script == null) return false;
            bool show;
            Type type = script.GetType();
            if (!scriptsWithShow.TryGetValue(type, out show))
                scriptsWithShow[type] = show = type.GetMethod("OnShow", System.Reflection.BindingFlags.Instance |
                    System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic) != null;
            return show;
        }

        // Whether everything activating root would reach is known (see above); if so, notified
        // holds the enabled behaviours with callbacks, in hierarchy order.
        static bool Collect(GameObject root, HashSet<int> seen)
        {
            Transform top = root.transform;
            root.GetComponentsInChildren<Component>(true, components);
            notified.Clear();
            bool ok = true;
            for (int i = 0; i < components.Count && ok; i++)
            {
                Component component = components[i];
                if (component == null) { ok = false; break; }  // a missing script
                if (!WouldActivate(component.transform, top)) continue;
                var behaviour = component as MonoBehaviour;
                if (ReferenceEquals(behaviour, null)) { ok = SafeEngineComponent(component); continue; }
                Callbacks found = CallbacksOf(behaviour.GetType());
                ok = found.Allowed && seen.Contains(behaviour.GetInstanceID());
                if (ok && behaviour.enabled && (found.OnEnable != null || found.OnDisable != null)) notified.Add(behaviour);
            }
            components.Clear();
            if (!ok) notified.Clear();
            return ok;
        }

        // When a scene unloads (SceneUnloads). The delegates hold their behaviours: dropped, so
        // destroyed ones do not keep what their fields reference loaded.
        internal static void Unloaded()
        {
            enables.Clear();
            disables.Clear();
            marked.Clear();
        }
    }

    // TextWrapper.WrapTextMinimiseWidth(input, width, minWidth) (QuestText.WrapText: the info
    // bar sets its text again every frame while it shows a name, and wrapping one line takes
    // 3-10 KB of strings). Its result depends only on the input, the two widths and the widths
    // it reads from the wrapper's Hashtable (GetTextWidth); a character missing there is measured
    // once and added, and entries are never changed or removed. So once a wrapper has wrapped an
    // input at the same widths (every character of it is then in its table), it returns the same
    // text again, and with Compat TextWidths that call touches nothing else (no character to
    // measure). The last results are kept per wrapper (by reference), input (by value) and
    // widths (bit for bit). -compat-no-wrap-memo.
    public static class WrapMemo
    {
        sealed class Entry
        {
            public object Wrapper;
            public string Input, Result;
            public int Width, MinWidth;
        }

        // The last results (a ring; the info bar wraps the same text every frame).
        static readonly Entry[] entries = new Entry[16];
        static int next;

        public static bool TryGet(object wrapper, string input, float width, float minWidth, out string result)
        {
            result = null;
            if (wrapper == null || input == null || !MainThread.Current || !Allocs.MemoizeWrap) return false;
            int w = FloatBits.Of(width), m = FloatBits.Of(minWidth);
            for (int i = 0; i < entries.Length; i++)
            {
                Entry entry = entries[i];
                if (entry == null || !ReferenceEquals(entry.Wrapper, wrapper) || entry.Width != w || entry.MinWidth != m ||
                    !string.Equals(entry.Input, input)) continue;
                result = entry.Result;
                return true;
            }
            return false;
        }

        public static string Store(string result, object wrapper, string input, float width, float minWidth)
        {
            if (wrapper == null || input == null || !MainThread.Current || !Allocs.MemoizeWrap) return result;
            Entry entry = entries[next] ?? (entries[next] = new Entry());
            next = (next + 1) % entries.Length;
            entry.Wrapper = wrapper;
            entry.Input = input;
            entry.Result = result;
            entry.Width = FloatBits.Of(width);
            entry.MinWidth = FloatBits.Of(minWidth);
            return result;
        }

        public static void Clear()
        {
            for (int i = 0; i < entries.Length; i++) entries[i] = null;
        }
    }

    // The pathfinder (PowerTools.Pathfinder) finds two paths a frame while the player walks with
    // the stick (SystemInput.Walk: CalcWalkToFirstPoint, then WalkTo). Each adds two nodes to the
    // room's graph, links them to every node in sight (CalculateLinks: a PathLink for each
    // direction, added to the nodes' m_links lists, which grow from empty for the new nodes) and
    // removes them again (RemoveNode, which removes the links to them from the other nodes'
    // lists): 1-3 KB a path. A PathLink is held only by the one m_links list it was added to, and
    // a node only by the graph's node list (and stale m_previous pointers, which the search resets
    // before it reads them); a removed node is linked from no other node, so nothing reaches its
    // list or links again. So the links RemoveNode takes out of the other lists, and the removed
    // node's own links and list, are kept here (PoolPathLinks in rewriter/Allocs.cs), and
    // CalculateLinks and the PathNode constructor take them again: a link's two fields are both
    // set right after (the object initializer), and a list is handed out empty. RemoveAll calls
    // the predicate on the same items in the same order and removes the same ones as
    // List.RemoveAll. Main thread only; -compat-no-path-pool.
    public static class PathPool
    {
        static readonly CacheClears clears = new CacheClears();
        // Links and lists kept per type: only bounds, so a burst of paths does not leave more
        // kept than later paths take.
        const int MaxItems = 4096, MaxLists = 256;

        static class Of<T> where T : class
        {
            public static readonly Stack<List<T>> Lists = new Stack<List<T>>();
            public static readonly Stack<T> Items = clears.Add(new Stack<T>(), () => { Items.Clear(); Lists.Clear(); });
        }

        static bool On()
        {
            return MainThread.Current && Allocs.PoolPaths;
        }

        public static T Take<T>() where T : class, new()
        {
            if (On() && Of<T>.Items.Count > 0) return Of<T>.Items.Pop();
            return new T();
        }

        public static List<T> TakeList<T>() where T : class
        {
            if (On() && Of<T>.Lists.Count > 0) return Of<T>.Lists.Pop();
            return new List<T>();
        }

        // List<T>.RemoveAll(match), keeping the removed items.
        public static int RemoveAll<T>(List<T> list, Predicate<T> match) where T : class
        {
            if (list == null || match == null || !On()) return list.RemoveAll(match);
            int count = list.Count, free = 0;
            while (free < count && !match(list[free])) free++;
            if (free >= count) return 0;
            Keep(list[free]);
            int current = free + 1;
            while (current < count)
            {
                while (current < count && match(list[current])) Keep(list[current++]);
                if (current < count) list[free++] = list[current++];
            }
            int removed = count - free;
            list.RemoveRange(free, removed);
            return removed;
        }

        static void Keep<T>(T item) where T : class
        {
            if (Of<T>.Items.Count < MaxItems) Of<T>.Items.Push(item);
        }

        // A removed node's links and list.
        public static void Recycle<T>(List<T> list) where T : class
        {
            if (list == null || !On()) return;
            for (int i = 0; i < list.Count; i++) Keep(list[i]);
            list.Clear();
            if (Of<T>.Lists.Count < MaxLists) Of<T>.Lists.Push(list);
        }

        public static void Clear()
        {
            clears.Run();
        }
    }

    // Regex.IsMatch(input, pattern, options) builds a Regex (and its key in the regex cache, and
    // a new runner) on every call: CharacterComponent.GetTransitionAnim tests each of the
    // character's transition animations with two of them every time an animation starts, which
    // while walking and turning is most frames (about 2.4 KB a frame). The same pattern and
    // options give a Regex that answers the same; one is kept per pattern, options and current
    // culture (IgnoreCase depends on it, as the static method's cache key does) and asked
    // instead. A pattern the constructor rejects throws on every call, as before.
    // -compat-no-regex-cache.
    public static class Regexes
    {
        sealed class Entry
        {
            public string Pattern;
            public System.Text.RegularExpressions.RegexOptions Options;
            public System.Globalization.CultureInfo Culture;
            public System.Text.RegularExpressions.Regex Regex;
        }

        // The last Regexes made (a ring; GetTransitionAnim asks two per transition animation).
        static readonly Entry[] entries = new Entry[64];
        static int next;

        public static bool IsMatch(string input, string pattern, System.Text.RegularExpressions.RegexOptions options)
        {
            if (input == null || pattern == null || !MainThread.Current || !Allocs.CacheRegexes)
                return System.Text.RegularExpressions.Regex.IsMatch(input, pattern, options);
            System.Globalization.CultureInfo culture = System.Globalization.CultureInfo.CurrentCulture;
            for (int i = 0; i < entries.Length; i++)
            {
                Entry entry = entries[i];
                if (entry != null && entry.Options == options && ReferenceEquals(entry.Culture, culture) &&
                    (ReferenceEquals(entry.Pattern, pattern) || string.Equals(entry.Pattern, pattern)))
                    return entry.Regex.IsMatch(input);
            }
            var regex = new System.Text.RegularExpressions.Regex(pattern, options);
            entries[next] = new Entry { Pattern = pattern, Options = options, Culture = culture, Regex = regex };
            next = (next + 1) % entries.Length;
            return regex.IsMatch(input);
        }
    }

    // Sprite.vertices copies the sprite's vertices into a new array on every read. GUI layout
    // reads them every frame (FitToObject and ObjectAlign through GuiUtils.CalculateGuiRectFromSprite,
    // about 200 B a frame in the cemetery rooms), and CharacterComponent.GetTextPositionInternal
    // for each line a character says; both only read the array. A sprite's geometry never changes
    // in the game (nothing calls Sprite.OverrideGeometry), so its array is kept per sprite
    // (instance id) and given to those readers again; dropped when a scene unloads.
    // -compat-no-sprite-vertices.
    public static class SpriteVertices
    {
        static readonly Dictionary<int, Vector2[]> found = new Dictionary<int, Vector2[]>();

        public static Vector2[] Get(Sprite sprite)
        {
            if (sprite == null || !MainThread.Current || !Allocs.CacheSpriteVertices) return sprite.vertices;
            int id = sprite.GetInstanceID();
            Vector2[] vertices;
            if (!found.TryGetValue(id, out vertices))
            {
                if (found.Count >= 1024) found.Clear();
                found[id] = vertices = sprite.vertices;
            }
            return vertices;
        }

        public static void Clear()
        {
            found.Clear();
        }
    }

    // string.Concat of the same strings (by reference) as a recent call gives a string equal to
    // that call's result, which is given again (MemoizeConcats in rewriter/Allocs.cs: the cursor's
    // script override name, built every frame while the cursor or selection is on something:
    // "GetCursor" + type name + script name, whose parts are the same string objects each time).
    // The result is only used as a method name to look up. -compat-no-concat-memo.
    public static class Concats
    {
        // The last calls (a ring; the cursor's name is built again every frame).
        const int Kept = 16;
        static readonly string[] a = new string[Kept], b = new string[Kept], c = new string[Kept], results = new string[Kept];
        static int next;

        public static string Concat(string first, string second, string third)
        {
            if (!MainThread.Current || !Allocs.MemoizeConcats) return string.Concat(first, second, third);
            for (int i = 0; i < results.Length; i++)
                if (results[i] != null && ReferenceEquals(a[i], first) && ReferenceEquals(b[i], second) && ReferenceEquals(c[i], third))
                    return results[i];
            string result = string.Concat(first, second, third);
            a[next] = first;
            b[next] = second;
            c[next] = third;
            results[next] = result;
            next = (next + 1) % results.Length;
            return result;
        }

        public static void Clear()
        {
            Array.Clear(a, 0, a.Length);
            Array.Clear(b, 0, b.Length);
            Array.Clear(c, 0, c.Length);
            Array.Clear(results, 0, results.Length);
        }
    }

    // SystemText.ParseIdFromText(ref text) splits a line id from "&123 Text" with two Substring
    // calls; the info bar localizes its text again every frame while it shows something. The
    // answer depends only on the text and the current culture (int.TryParse), so the last
    // answers are kept per text (by value) and culture: the id, and the rest of the text the
    // method assigned (the same string object again), or that it left the text alone.
    // -compat-no-text-ids.
    public static class TextIds
    {
        sealed class Entry
        {
            public string Input, Rest;
            public int Id;
            public bool Changed;
            public System.Globalization.CultureInfo Culture;
        }

        // The last answers (a ring; the info bar localizes the same text every frame).
        static readonly Entry[] entries = new Entry[8];
        static int next;

        public static bool TryGet(string input, out int id, out bool changed, out string rest)
        {
            id = -1;
            changed = false;
            rest = null;
            if (input == null || !MainThread.Current || !Allocs.MemoizeTextIds) return false;
            System.Globalization.CultureInfo culture = System.Globalization.CultureInfo.CurrentCulture;
            for (int i = 0; i < entries.Length; i++)
            {
                Entry entry = entries[i];
                if (entry == null || !ReferenceEquals(entry.Culture, culture) || !string.Equals(entry.Input, input)) continue;
                id = entry.Id;
                changed = entry.Changed;
                rest = entry.Rest;
                return true;
            }
            return false;
        }

        public static int Store(int id, string input, string rest)
        {
            if (input == null || !MainThread.Current || !Allocs.MemoizeTextIds) return id;
            Entry entry = entries[next] ?? (entries[next] = new Entry());
            next = (next + 1) % entries.Length;
            entry.Input = input;
            entry.Rest = rest;
            entry.Id = id;
            entry.Changed = !ReferenceEquals(rest, input);
            entry.Culture = System.Globalization.CultureInfo.CurrentCulture;
            return id;
        }

        public static void Clear()
        {
            for (int i = 0; i < entries.Length; i++) entries[i] = null;
        }
    }

    // int.ToString() of small numbers shown every frame (GuiControllerTopics: the count of new
    // topics, set on its label each frame). The same text, from a table of the strings made
    // before. -compat-no-number-text.
    public static class NumberText
    {
        // Numbers 0-1023 (the topic counts shown are far smaller); others are formatted each time.
        static readonly string[] made = new string[1024];

        public static string Get(int value)
        {
            if (value < 0 || value >= made.Length || !MainThread.Current || !Allocs.CacheNumberText) return value.ToString();
            return made[value] ?? (made[value] = value.ToString());
        }
    }
}
