using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using Object = UnityEngine.Object;

// The main-thread flag, scene-unload work and helpers shared by the caches, and per-frame engine work: transform reads,
// particle cameras, player-loop steps, particle rate, Animator culling.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // The caches here serve the main thread only, where the game saves, restores and builds
    // its objects, so they need no locks: IL2CPP's Monitor.Exit resets a kernel event every time.
    public static class MainThread
    {
        static int id = -1;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Record()
        {
            id = System.Threading.Thread.CurrentThread.ManagedThreadId;
            isMain = true;
        }

        // Set on the main thread only (thread-static: false on every other thread), so the check
        // makes no call into the runtime for the current Thread object (0.8 us on the PS Vita);
        // a main thread that has not set it yet (Record not run) sets it on its first check.
        [ThreadStatic] static bool isMain;

        public static bool Current
        {
            get
            {
                if (isMain) return true;
                if (id < 0 || id != System.Threading.Thread.CurrentThread.ManagedThreadId) return false;
                isMain = true;
                return true;
            }
        }
    }

    // Compat's work when a scene unloads, in this order (none of it reads what another part
    // changes). The probe's census lists sceneUnloaded's handlers by class: Compat's show as this.
    public static class SceneUnloads
    {
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Register()
        {
            UnityEngine.SceneManagement.SceneManager.sceneUnloaded += scene => Unloaded();
        }

        static void Unloaded()
        {
            UnloadedRoomRefs.Release();
            WaterMasks.Unloaded();
            MemorySetup.Apply();
            ScriptCache.Unloaded();
            AudioData.Release();
            SpriteLists.Clear();
            NamedLists.Clear();
            AnimatorOverrides.Unloaded();
            AnimDirections.Unloaded();
            Components.Clear();
            SightEdges.Clear();
            AreaMemo.Clear();
            Allocs.Clear();
            GuiReshows.Unloaded();
        }
    }

    // Keys compared by reference and hashed by identity, whatever Equals and GetHashCode their
    // type has: strings are not hashed by their characters, engine objects not looked up in the
    // engine, and a destroyed object is still its own key.
    sealed class ReferenceComparer<T> : IEqualityComparer<T> where T : class
    {
        public static readonly ReferenceComparer<T> Instance = new ReferenceComparer<T>();

        public bool Equals(T a, T b) { return ReferenceEquals(a, b); }
        public int GetHashCode(T value) { return System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(value); }
    }

    // The clear actions of a family of per-type caches (static Of<T> classes, made on first use
    // of each T), run together by the family's Clear.
    sealed class CacheClears
    {
        readonly List<Action> clears = new List<Action>();

        // The cache, its clear action noted.
        public T Add<T>(T cache, Action clear)
        {
            clears.Add(clear);
            return cache;
        }

        public void Run()
        {
            for (int i = 0; i < clears.Count; i++) clears[i]();
        }
    }

    static class FloatBits
    {
        // A float's bits, for keys and exact comparisons (NaN equal to itself, 0 and -0 apart).
        public static unsafe int Of(float value)
        {
            return *(int*)&value;
        }
    }

    // Transform.position reads in methods that read it several times in a row (RainDrop.Update;
    // see CacheTransformReads in rewriter/ScriptCosts.cs): the value the last read of the same
    // Transform got, until Forget or SetPosition. Main thread only, no locks.
    public static class TransformReads
    {
        static Transform last;
        static Vector3 position;
        // Whether the method that called Forget runs on the main thread: checked once per call
        // of the method (MainThread.Current costs about 0.5 us on the PS Vita, as much as the
        // engine read it saves).
        static bool onMain;

        public static void Forget()
        {
            last = null;
            onMain = MainThread.Current;
        }

        public static Vector3 Position(Transform transform)
        {
            if ((object)transform == (object)last && (object)last != null) return position;
            Vector3 value;
            // A drop Compat EffectSprites draws: the position the game gave it.
            if (!EffectSprites.TryPosition(transform, out value)) value = transform.position;
            if (onMain)
            {
                last = transform;
                position = value;
            }
            return value;
        }

        public static void SetPosition(Transform transform, Vector3 value)
        {
            last = null;
            transform.position = value;
        }

        // A position set elsewhere (Compat EffectSprites): later reads return it.
        public static void Remember(Transform transform, Vector3 value)
        {
            last = onMain ? transform : null;
            position = value;
        }
    }

    // QuestCamera's particle cameras (ParticleCamera1-3) render their layers (Particle1-3) into
    // a texture every frame that a child quad (ParticleRenderN) shows, about 0.5 ms of CPU each
    // on the PS Vita. Only 10 rooms put anything on those layers, and only room scenes do (no
    // prefab spawned later is on them, and no code moves objects onto them). So when a scene
    // loads, a camera with no renderer on its layers in any loaded scene, inactive ones
    // included, is turned off with its quad: it would only ever draw an empty texture. On the
    // PS Vita, or with -skip-empty-particle-cameras.
    public static class ParticleCameras
    {
        static readonly List<Behaviour> disabledCameras = new List<Behaviour>();
        static readonly List<Renderer> disabledQuads = new List<Renderer>();

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Register()
        {
            if (Application.platform != RuntimePlatform.PSP2 && !Platform.OnCommandLine("-skip-empty-particle-cameras")) return;
            UnityEngine.SceneManagement.SceneManager.sceneLoaded += (scene, mode) => Apply();
        }

        static void Apply()
        {
            Restore();
            foreach (Behaviour camera in disabledCameras) if (camera != null) camera.enabled = true;
            foreach (Renderer quad in disabledQuads) if (quad != null) quad.enabled = true;
            disabledCameras.Clear();
            disabledQuads.Clear();
            var cameras = new List<Camera>();
            foreach (Camera camera in Resources.FindObjectsOfTypeAll<Camera>())
                if (camera.gameObject.scene.IsValid() && camera.name.StartsWith("ParticleCamera")) cameras.Add(camera);
            if (cameras.Count == 0) return;
            int used = 0;
            foreach (Renderer renderer in Resources.FindObjectsOfTypeAll<Renderer>())
            {
                if (!renderer.gameObject.scene.IsValid()) continue;
                bool quad = false;
                foreach (Camera camera in cameras) if (renderer.transform.IsChildOf(camera.transform)) quad = true;
                if (!quad) used |= 1 << renderer.gameObject.layer;
            }
            foreach (Camera camera in cameras)
            {
                if (!camera.enabled || (camera.cullingMask & used) != 0) continue;
                Disable(camera);
            }
            // The quads get their sorting order from a script's Start, after the scene loaded:
            // merged at the first player loop step of the next frame (LoopSteps).
            if (Platform.VitaDefault("-compat-no-particle-merge", "-compat-particle-merge")) pendingMerge = cameras;
        }

        static List<Camera> pendingMerge;

        public static void Late()
        {
            if (pendingMerge == null) return;
            List<Camera> cameras = pendingMerge;
            pendingMerge = null;
            Merge(cameras);
        }

        static void Disable(Camera camera)
        {
            camera.enabled = false;
            disabledCameras.Add(camera);
            foreach (Renderer quad in camera.GetComponentsInChildren<Renderer>(true))
            {
                if (!quad.enabled) continue;
                quad.enabled = false;
                disabledQuads.Add(quad);
            }
        }

        // Particle merge (an effect: reads the same, not bit-exact; on the PS Vita unless
        // -compat-no-particle-merge, elsewhere with -compat-particle-merge and
        // -skip-empty-particle-cameras): two
        // particle cameras set up alike (same parent, placement, projection, clear, texture size
        // and format) whose quads draw alike (same shader, colour, placement, sorting layer and
        // order, which no other renderer of the loaded scenes shares, so nothing can sort between
        // them) become one: the second also renders the first's layers, and the first is turned
        // off with its quad. In Drowning, ParticleCamera2 (layer Particle2: ParticleDarkBig) and
        // ParticleCamera3 (Particle3: the player's bubbles and splashes) both show at order 1000
        // through Unlit/Transparent. A pixel changes only where particles of both layers cover it:
        // they blend in one texture before the quad blends it, instead of one quad over the other.
        static readonly List<Camera> widened = new List<Camera>();
        static readonly List<int> widenedMasks = new List<int>();

        static void Merge(List<Camera> cameras)
        {
            var live = cameras.Where(c => c.enabled && c.targetTexture != null).ToList();
            for (int i = 0; i < live.Count; i++)
                for (int j = i + 1; j < live.Count; j++)
                {
                    Camera a = live[i], b = live[j];
                    if (!a.enabled || !b.enabled || !Alike(a, b)) continue;
                    widened.Add(b);
                    widenedMasks.Add(b.cullingMask);
                    b.cullingMask |= a.cullingMask;
                    Disable(a);
                }
        }

        static Renderer Quad(Camera camera)
        {
            Renderer found = null;
            foreach (Renderer renderer in camera.GetComponentsInChildren<Renderer>(true))
            {
                if (!renderer.enabled || !renderer.gameObject.activeInHierarchy) continue;
                if (found != null) return null;
                found = renderer;
            }
            return found;
        }

        static bool Alike(Camera a, Camera b)
        {
            if (!SameView(a, b)) return false;
            Renderer qa = Quad(a), qb = Quad(b);
            return qa != null && qb != null && SameQuad(qa, qb, a.targetTexture, b.targetTexture) && AloneAtOrder(qa, qb);
        }

        // Same parent, placement, projection, clear and texture size and format; layers apart.
        static bool SameView(Camera a, Camera b)
        {
            Transform ta = a.transform, tb = b.transform;
            RenderTexture ra = a.targetTexture, rb = b.targetTexture;
            return !(ta.parent != tb.parent || ta.localPosition != tb.localPosition || ta.localRotation != tb.localRotation || ta.localScale != tb.localScale ||
                     a.orthographic != b.orthographic || a.orthographicSize != b.orthographicSize || a.fieldOfView != b.fieldOfView ||
                     a.nearClipPlane != b.nearClipPlane || a.farClipPlane != b.farClipPlane || a.rect != b.rect || a.depth != b.depth ||
                     a.clearFlags != b.clearFlags || a.backgroundColor != b.backgroundColor || (a.cullingMask & b.cullingMask) != 0 ||
                     ra.width != rb.width || ra.height != rb.height || ra.format != rb.format || ra.depth != rb.depth || ra.filterMode != rb.filterMode);
        }

        // The quads draw alike: the same shader, colour and placement, each its camera's texture,
        // at the same sorting layer and order.
        static bool SameQuad(Renderer qa, Renderer qb, RenderTexture ra, RenderTexture rb)
        {
            Material ma = qa.sharedMaterial, mb = qb.sharedMaterial;
            return !(ma == null || mb == null || ma.shader != mb.shader || ma.renderQueue != mb.renderQueue ||
                     (ma.HasProperty("_Color") && ma.GetColor("_Color") != mb.GetColor("_Color")) ||
                     ma.mainTextureScale != mb.mainTextureScale || ma.mainTextureOffset != mb.mainTextureOffset ||
                     ma.mainTexture != ra || mb.mainTexture != rb ||
                     qa.sortingLayerID != qb.sortingLayerID || qa.sortingOrder != qb.sortingOrder ||
                     qa.transform.localPosition != qb.transform.localPosition || qa.transform.localRotation != qb.transform.localRotation ||
                     qa.transform.localScale != qb.transform.localScale);
        }

        // No other renderer drawn by the cameras that draw the quads shares their sorting layer
        // and order, so nothing can sort between them.
        static bool AloneAtOrder(Renderer qa, Renderer qb)
        {
            int drawn = 0;
            foreach (Camera camera in Camera.allCameras)
                if ((camera.cullingMask & (1 << qa.gameObject.layer)) != 0 || (camera.cullingMask & (1 << qb.gameObject.layer)) != 0) drawn |= camera.cullingMask;
            foreach (Renderer other in Object.FindObjectsOfType<Renderer>())
                if (other != qa && other != qb && (drawn & (1 << other.gameObject.layer)) != 0 &&
                    other.sortingLayerID == qa.sortingLayerID && other.sortingOrder == qa.sortingOrder) return false;
            return true;
        }

        // Last change first: a camera widened twice (by two merges) gets its first mask back.
        static void Restore()
        {
            for (int i = widened.Count - 1; i >= 0; i--) if (widened[i] != null) widened[i].cullingMask = widenedMasks[i];
            widened.Clear();
            widenedMasks.Clear();
        }
    }

    // Compat's steps in Unity's player loop. Unity 2018.2 cannot read back the loop it runs, so
    // the probe's -diag-time-loop starts from Current (the loop set here) when it exists.
    // - Before PreLateUpdate.DirectorUpdateAnimationBegin: AnimatorCulling.Poll and
    //   ParticleCameras.Late (particle merge).
    // - Before PreLateUpdate.ParticleSystemBeginUpdateAll: ParticleRate.Before; after
    //   PostLateUpdate.ParticleSystemEndUpdateAll: ParticleRate.After.
    public static class LoopSteps
    {
        sealed class AnimatorCullingPoll { }
        sealed class ParticleCameraMerge { }
        sealed class ParticleRateBefore { }
        sealed class ParticleRateAfter { }

        public static UnityEngine.Experimental.LowLevel.PlayerLoopSystem Current;
        public static bool Installed;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Install()
        {
            var loop = UnityEngine.Experimental.LowLevel.PlayerLoop.GetDefaultPlayerLoop();
            var phases = loop.subSystemList;
            if (phases == null) return;
            for (int p = 0; p < phases.Length; p++)
            {
                var subs = phases[p].subSystemList;
                if (subs == null) continue;
                var steps = new List<UnityEngine.Experimental.LowLevel.PlayerLoopSystem>();
                foreach (var sub in subs)
                {
                    string name = sub.type == null ? "" : sub.type.Name;
                    if (name == "DirectorUpdateAnimationBegin")
                    {
                        steps.Add(Step(typeof(AnimatorCullingPoll), AnimatorCulling.Poll));
                        steps.Add(Step(typeof(ParticleCameraMerge), ParticleCameras.Late));
                    }
                    if (name == "ParticleSystemBeginUpdateAll") steps.Add(Step(typeof(ParticleRateBefore), ParticleRate.Before));
                    steps.Add(sub);
                    if (name == "ParticleSystemEndUpdateAll") steps.Add(Step(typeof(ParticleRateAfter), ParticleRate.After));
                }
                phases[p].subSystemList = steps.ToArray();
            }
            loop.subSystemList = phases;
            UnityEngine.Experimental.LowLevel.PlayerLoop.SetPlayerLoop(loop);
            Current = loop;
            Installed = true;
        }

        static UnityEngine.Experimental.LowLevel.PlayerLoopSystem Step(Type type, UnityEngine.Experimental.LowLevel.PlayerLoopSystem.UpdateFunction function)
        {
            var step = new UnityEngine.Experimental.LowLevel.PlayerLoopSystem();
            step.type = type;
            step.updateDelegate = function;
            return step;
        }
    }

    // Slow ambient particle systems simulated every other frame (an effect: reads the same, not
    // bit-exact; on the PS Vita unless -compat-no-particle-rate, elsewhere with
    // -compat-particle-rate). Drowning's water specks
    // (ParticleLight, ParticleMid, ParticleDark, ParticleDarkBig: about 1,900 one- and two-texel
    // particles with Force, Limit Velocity and Noise) move at most 0.5 texels a frame at 60 fps
    // (speed limit 30 units/s at simulation speed 0.4-1; one unit is one texel of the 320x180
    // particle textures). On every other frame they are paused for the engine's particle update
    // (LoopSteps: paused before PreLateUpdate.ParticleSystemBeginUpdateAll, resumed after
    // PostLateUpdate.ParticleSystemEndUpdateAll, so the game's Update, coroutines and the end of
    // the frame see them as before); the next update runs them over both frames' time
    // (simulationSpeed scaled by the two frames' time over this frame's, restored after the
    // update, with Time.maximumParticleDeltaTime raised to that time for the update when this
    // frame's own time is within it, so they make one step as the other systems do). They keep
    // drawing, so their particles move at 30 Hz instead of 60.
    public static class ParticleRate
    {
        // The scene, then its slow systems.
        static readonly string[] Slow = { "SceneRoomDrowning", "ParticleLight", "ParticleMid", "ParticleDark", "ParticleDarkBig" };

        static readonly List<ParticleSystem> systems = new List<ParticleSystem>();
        static List<ParticleSystem> paused = new List<ParticleSystem>();
        static readonly List<ParticleSystem> caughtUp = new List<ParticleSystem>();
        static readonly List<float> speeds = new List<float>();
        static List<ParticleSystem> lastPaused = new List<ParticleSystem>();
        static bool skipNext, registered;
        static float pending, savedMax = -1f;
        static string[] configuredFor;
        static bool enabled;
        // Simulation steps skipped (the probe's heartbeat shows the count since the last one).
        public static int Skipped;

        static bool Enabled()
        {
            if (!Platform.NewArgs(ref configuredFor)) return enabled;
            enabled = Platform.VitaDefault("-compat-no-particle-rate", "-compat-particle-rate");
            if (enabled && !registered)
            {
                registered = true;
                UnityEngine.SceneManagement.SceneManager.sceneLoaded += (scene, mode) => Collect();
                UnityEngine.SceneManagement.SceneManager.sceneUnloaded += scene => Collect();
                Collect();
            }
            return enabled;
        }

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Register()
        {
            UnityEngine.SceneManagement.SceneManager.sceneLoaded += (scene, mode) => Enabled();
        }

        static void Collect()
        {
            systems.Clear();
            lastPaused.Clear();
            pending = 0f;
            skipNext = false;
            for (int s = 0; s < UnityEngine.SceneManagement.SceneManager.sceneCount; s++)
            {
                var scene = UnityEngine.SceneManagement.SceneManager.GetSceneAt(s);
                if (!scene.isLoaded || scene.name != Slow[0]) continue;
                foreach (GameObject root in scene.GetRootGameObjects())
                    foreach (ParticleSystem system in root.GetComponentsInChildren<ParticleSystem>(true))
                        if (Array.IndexOf(Slow, system.name, 1) > 0 && !system.main.useUnscaledTime) systems.Add(system);
            }
        }

        public static void Before()
        {
            if (systems.Count == 0 || !Enabled()) return;
            float dt = Time.deltaTime;
            if (dt <= 0f) return;
            if (skipNext)
            {
                skipNext = false;
                foreach (ParticleSystem system in systems)
                {
                    if (system == null || !system.isPlaying) continue;
                    system.Pause(false);
                    paused.Add(system);
                }
                pending += dt;
                Skipped++;
                return;
            }
            skipNext = true;
            // Time skipped while nothing was paused is not caught up.
            if (lastPaused.Count == 0) pending = 0f;
            if (pending <= 0f) return;
            float scale = (pending + dt) / dt;
            float max = Time.maximumParticleDeltaTime;
            if (dt <= max && pending + dt > max)
            {
                savedMax = max;
                Time.maximumParticleDeltaTime = pending + dt;
            }
            pending = 0f;
            foreach (ParticleSystem system in lastPaused)
            {
                if (system == null || !system.isPlaying) continue;
                ParticleSystem.MainModule main = system.main;
                speeds.Add(main.simulationSpeed);
                caughtUp.Add(system);
                main.simulationSpeed = main.simulationSpeed * scale;
            }
            lastPaused.Clear();
        }

        public static void After()
        {
            if (paused.Count == 0 && caughtUp.Count == 0 && savedMax < 0f) return;
            for (int i = 0; i < caughtUp.Count; i++)
            {
                if (caughtUp[i] == null) continue;
                ParticleSystem.MainModule main = caughtUp[i].main;
                main.simulationSpeed = speeds[i];
            }
            caughtUp.Clear();
            speeds.Clear();
            if (savedMax >= 0f)
            {
                Time.maximumParticleDeltaTime = savedMax;
                savedMax = -1f;
            }
            foreach (ParticleSystem system in paused)
                if (system != null && system.isPaused) system.Play(false);
            // The systems paused this frame catch up at the next update.
            List<ParticleSystem> swap = lastPaused;
            lastPaused = paused;
            paused = swap;
            paused.Clear();
        }
    }

    // The engine's animation step evaluates every enabled Animator every frame. PowerQuest hides
    // a prop by disabling its renderers and pausing its SpriteAnim (Animator.speed 0), and rooms
    // keep many such props: 27 of EscCorridor's 41 Animators, 13 in EscReactor and in Drowning.
    // A paused Animator's state does not advance and it writes the same pose every frame. Before
    // the animation step (LoopSteps), an Animator of a SpriteAnim whose speed is 0 and whose
    // renderers (its own and its children's) are all disabled gets CullCompletely, which the
    // engine skips because no renderer of it is visible; any other gets its own culling mode
    // back before that step. Anything that could change what the step would do first restores
    // the mode (Wake), and the Animator is then evaluated at least once before it can be culled
    // again: SpriteAnim's Play, Stop, Pause, Resume, SetSpeed and SetNormalizedTime, every
    // Animator the game's code gets hold of (calls returning an Animator: SpriteAnim.Animator,
    // GetComponent<Animator>()) and Behaviour.enabled assignments (SystemTime's pause disables
    // and enables Animators). The renderers are disabled, so nothing they draw changes; the
    // state's time and events do not move at speed 0. On the PS Vita (-compat-no-anim-cull
    // turns it off), or on Windows with -compat-anim-cull. Main thread only, no locks.
    public static class AnimatorCulling
    {
        sealed class Entry
        {
            public Animator animator;
            public Renderer[] renderers;
            public AnimatorCullingMode mode;
            public bool culled, woken;
        }

        static readonly List<Entry> entries = new List<Entry>();
        static readonly Dictionary<Animator, Entry> byAnimator = new Dictionary<Animator, Entry>(ReferenceComparer<Animator>.Instance);
        static string[] configuredFor;
        static bool enabled;
        static int next;
        // Not culled Animators looked at per frame; culled ones are looked at every frame.
        const int CandidatesPerFrame = 8;
        // Animators culled now (the probe's heartbeat shows it).
        public static int Culled;

        public static bool Enabled()
        {
            if (!Platform.NewArgs(ref configuredFor)) return enabled;
            enabled = Platform.VitaDefault("-compat-no-anim-cull", "-compat-anim-cull");
            if (!enabled) Clear();
            return enabled;
        }

        // At the end of SpriteAnim.Awake, with its Animator.
        public static void Register(Animator animator)
        {
            if (!MainThread.Current || !Enabled() || animator == null || byAnimator.ContainsKey(animator)) return;
            var entry = new Entry { animator = animator, renderers = animator.GetComponentsInChildren<Renderer>(true), mode = animator.cullingMode, woken = true };
            entries.Add(entry);
            byAnimator[animator] = entry;
        }

        public static void Wake(Animator animator)
        {
            if ((object)animator == null || entries.Count == 0 || !MainThread.Current) return;
            Entry entry;
            if (!byAnimator.TryGetValue(animator, out entry)) return;
            entry.woken = true;
            if (!entry.culled) return;
            entry.culled = false;
            Culled--;
            if (animator != null) animator.cullingMode = entry.mode;
        }

        // A call returning an Animator in the game's code: the caller may change it.
        public static Animator Woken(Animator animator)
        {
            Wake(animator);
            return animator;
        }

        public static void SetEnabled(Behaviour behaviour, bool value)
        {
            Wake(behaviour as Animator);
            behaviour.enabled = value;
        }

        public static void Poll()
        {
            int count = entries.Count;
            if (count == 0 || !Enabled()) return;
            int candidates = 0;
            bool dead = false;
            for (int k = 0; k < count; k++)
            {
                Entry entry = entries[(next + k) % count];
                if (!entry.culled && (entry.woken || candidates >= CandidatesPerFrame)) continue;
                if (!entry.culled) candidates++;
                Animator animator = entry.animator;
                if (animator == null)
                {
                    if (entry.culled) Culled--;
                    entry.culled = false;
                    entry.renderers = null;
                    dead = true;
                    continue;
                }
                bool cull = animator.speed == 0f && AllDisabled(entry.renderers);
                if (cull == entry.culled) continue;
                animator.cullingMode = cull ? AnimatorCullingMode.CullCompletely : entry.mode;
                entry.culled = cull;
                Culled += cull ? 1 : -1;
            }
            next = (next + CandidatesPerFrame) % count;
            // Woken Animators (never culled here) are evaluated in this frame's step, so from the
            // next frame on they may be culled again.
            foreach (Entry entry in entries) entry.woken = false;
            if (!dead) return;
            for (int i = entries.Count - 1; i >= 0; i--)
            {
                if (entries[i].renderers != null) continue;
                byAnimator.Remove(entries[i].animator);
                entries.RemoveAt(i);
            }
        }

        // An Animator without renderers (a SpriteAnim on a UI Image) is never culled: the engine
        // cannot see whether it shows.
        static bool AllDisabled(Renderer[] renderers)
        {
            if (renderers.Length == 0) return false;
            foreach (Renderer renderer in renderers)
                if (renderer != null && renderer.enabled) return false;
            return true;
        }

        static void Clear()
        {
            foreach (Entry entry in entries)
                if (entry.culled && entry.animator != null) entry.animator.cullingMode = entry.mode;
            entries.Clear();
            byAnimator.Clear();
            Culled = 0;
        }
    }
}
