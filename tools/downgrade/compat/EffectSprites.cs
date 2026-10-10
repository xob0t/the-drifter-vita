using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using UnityEngine.Rendering;
using Object = UnityEngine.Object;

// Effect sprites: rain and sand drops and bees drawn as pages.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // The rain's drops (Rain and Sand, each drop a RainDrop: about 600 sprites in CemeteryGrave,
    // CemeteryChapel and LabSurface) and the bees (Bees: about 450 in PaperOffice, EscCorridor and
    // Boss) cost the PS Vita several ms a frame in per-renderer work: the pixel camera culls,
    // sorts and draws each sprite, and the game's scripts make engine calls for each (position,
    // activation, rotation and scale writes). EffectSprites draws them instead, on by default;
    // -compat-effects-original keeps the original renderers.
    // - Each drop and bee keeps its GameObject, inactive. The calls the game's code makes on them
    //   (BatchSprites in rewriter/ScriptCosts.cs: activation, layer, sprite, colour, sorting order
    //   and layer, property block, destruction, position, scale and rotation) come here; for a drop or bee
    //   they only note the value, for anything else they make the call. Position reads in
    //   RainDrop.Update (TransformReads) return the noted position.
    // - Once a frame, before the first camera culls, the items whose state changed are written
    //   into the shader arrays of "pages": MeshRenderers drawing a static mesh of 64 quads, each quad
    //   reading its slot (position, 2x2 matrix or bee scale and wobble values, colour and sprite
    //   index) from the page's MaterialPropertyBlock. No mesh is ever updated: on the PS Vita
    //   mesh uploads of drawn meshes cost about 1 us a vertex and channel, so meshes rewritten
    //   each frame cost more than the sprites. Hidden/DrifterEffectSprite draws what
    //   Sprites/Default drew (the drops, Boss's BeeBack); Hidden/DrifterEffectBee draws what
    //   BeeSprite drew, with the same wobble from BeeTimer and each bee's _WobbleOffset and
    //   _WobbleSpeed.
    // - Draw order: a page holds items of one layer, sorting layer and texture whose order range
    //   holds no order of another renderer drawn by the same cameras on that sorting layer, and
    //   draws at its lowest item's order, so it sorts among the other renderers as its items did.
    //   Those orders are read every 8 frames, and every frame for 4 s after one changed (a walking
    //   character's); when one moves into a page's range, the items on its smaller side move to
    //   other pages (only the drops the character passed). The renderers' visibility is read every
    //   16 frames. Pages lie 0.01 nearer the camera than the items did, so at an equal order they
    //   draw after the room's renderers, as drops and bees activated after them did. An item keeps
    //   its slot while its page's range still fits; a page uploads its items in increasing order,
    //   but two pages can overlap in order (drops of different orders in different pages that
    //   overlap may swap; bees of one colour blend the same either way).
    // - A frame's work is the items the game changed (each hook marks its item) and their pages:
    //   items that sit still (landed drops, inactive pooled ones) cost nothing.
    // - The light of lit drops (RainDrop's per-frame Texture2D.GetPixel at the drop's position,
    //   blended into its colour) is read by the fragment shader from the same texel; the
    //   GetPixel call itself returns transparent black (the drop then sets its own colour).
    // - Collider2D.OverlapPoint in Rain.Spawn and Sand.Spawn (where drops may land and which are
    //   lit, about 15 us each on the PS Vita) is answered from the box or single-path polygon
    //   collider's shape (read every frame, its points every 120 frames; a polygon's edges indexed
    //   by horizontal band); other colliders ask the engine. -compat-collider-check asks both and counts disagreements.
    // An item the effect cannot draw (another material, a rotated or skewed sprite packing, a
    // parent with scale or rotation) is handed back to its renderer with its noted state.
    // Main thread only.
    public static partial class EffectSprites
    {
        // Items a page draws, and sprites a set's material holds. The shaders' arrays have
        // these sizes: _EffectData[Slots * 3] (192), _EffectRect and _EffectUV[MaxSprites] (8).
        public const int Slots = 64, MaxSprites = 8;
        const int SpriteShader = 1, BeeShader = 2;
        // Towards the camera, from the items' z (see the class comment).
        const float PageZ = -0.01f;
        // Where an empty slot's quad lies: far outside any view.
        const float Far = 1e6f;
        // Frames between reads of the other renderers' orders, and frames they are read every
        // frame after one changed (4 s at 60 fps): see BoundariesFor. The other renderers'
        // visibility is read in VisibilityFrames turns, a share of them a frame (Flush). Both
        // powers of two (frame counts are masked).
        const int OrderFrames = 8, MovingFrames = 240, VisibilityFrames = 16;

        sealed class Item
        {
            public Group group;
            public GameObject gameObject;
            public Transform transform;
            public SpriteRenderer renderer;
            public bool active, dead, released;
            public Sprite sprite;
            public Color color;
            public int order, layer, sortingLayer;
            public Vector3 position, scale = Vector3.one;
            public float cos = 1f, sin;
            // Columns of the local scale and rotation as a 2x2 matrix.
            public float m00 = 1f, m10, m01, m11 = 1f;
            public Texture2D light;
            public bool lit, lightRead;
            public bool hasBlock;
            public float wobbleOffset, wobbleSpeed;
            public Color blockColor;
            public Texture blockTexture;
            // Where it is drawn, and what changed since the last frame (Layout, Data).
            public Page page;
            public int slot = -1, dirty;
            public bool hasSprite;
            // What it was last drawn with.
            public Set set;
            public Sprite setSprite;
            public int setLayer, setSortingLayer, spriteIndex;
        }

        sealed class Group
        {
            public Transform transform;
            public string name;
            public int shader;
            public Material source;
            public Color materialColor = Color.white;
            public float wobbleOffset, wobbleSpeed;
            public readonly List<Item> items = new List<Item>();
            public readonly List<Set> sets = new List<Set>();
            public readonly List<Page> pages = new List<Page>();
        }

        sealed class Set
        {
            public int layer, sortingLayer;
            public Texture texture;
            public Material material;
            public readonly Sprite[] sprites = new Sprite[MaxSprites];
            public readonly Vector4[] rects = new Vector4[MaxSprites], uvs = new Vector4[MaxSprites];
            public int spriteCount, cutsVersion = -1;
            public Texture2D light;
        }

        sealed class Page
        {
            public GameObject gameObject;
            public MeshRenderer renderer;
            public readonly MaterialPropertyBlock block = new MaterialPropertyBlock();
            // Per slot three vectors (A, B, C: see the shaders), in one array: one upload a page.
            public readonly Vector4[] data = new Vector4[Slots * 3];
            // Its items by slot, of one set; no other renderer's order lies in (minOrder, maxOrder]
            // (its items' orders, or a range around them).
            public readonly Item[] items = new Item[Slots];
            // The upload: the slots' vectors in increasing order of their items.
            public readonly Vector4[] upload = new Vector4[Slots * 3];
            public readonly int[] sorted = new int[Slots];
            public int uploaded = Slots;
            public Set set;
            public int count, minOrder, maxOrder, order, layer = -1, sortingLayer;
            public bool enabled, placed, dirty;
            public Material material;
        }

        sealed class Foreign
        {
            public Renderer renderer;
            public GameObject gameObject;
            public int layer, sortingLayer, order, index, movingUntil = -1;
            public bool shown, gone, ordered;
        }

        // Orders of other renderers that a camera drawing the layer draws on the sorting layer.
        sealed class Boundaries
        {
            public int layer, sortingLayer, frame = -1, version, foreignVersion = -1, mask = -1;
            public bool changed;
            public List<int> orders = new List<int>(), scratch = new List<int>();
            public readonly List<Foreign> members = new List<Foreign>();
        }

        // Object identity -> value, open addressing (a Dictionary lookup costs about 1.5 us on
        // the PS Vita; the hooks look up every drop and bee call).
        sealed class IdentityTable<T> where T : class
        {
            // A power of two (Slot masks), doubled when half full.
            object[] keys = new object[1024];
            T[] values = new T[1024];
            int count, shift = 22;

            int Slot(object key)
            {
                return (int)(((uint)System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(key) * 2654435761u) >> shift);
            }

            public T Get(object key)
            {
                int mask = keys.Length - 1;
                for (int i = Slot(key); ; i = (i + 1) & mask)
                {
                    object k = keys[i];
                    if (k == null) return null;
                    if (ReferenceEquals(k, key)) return values[i];
                }
            }

            public void Set(object key, T value)
            {
                if ((count + 1) * 2 > keys.Length) Resize(keys.Length * 2);
                int mask = keys.Length - 1, i = Slot(key);
                while (keys[i] != null && !ReferenceEquals(keys[i], key)) i = (i + 1) & mask;
                if (keys[i] == null) count++;
                keys[i] = key;
                values[i] = value;
            }

            public void Clear()
            {
                Array.Clear(keys, 0, keys.Length);
                Array.Clear(values, 0, values.Length);
                count = 0;
            }

            void Resize(int size)
            {
                object[] oldKeys = keys;
                T[] oldValues = values;
                keys = new object[size];
                values = new T[size];
                count = 0;
                shift = 32;
                for (int s = size; s > 1; s >>= 1) shift--;
                for (int i = 0; i < oldKeys.Length; i++) if (oldKeys[i] != null) Set(oldKeys[i], oldValues[i]);
            }
        }

        static readonly IdentityTable<Item> table = new IdentityTable<Item>();
        static readonly IdentityTable<Page> pageTable = new IdentityTable<Page>();
        static readonly IdentityTable<Foreign> foreignTable = new IdentityTable<Foreign>();
        static readonly List<Group> groups = new List<Group>();
        static readonly List<Foreign> foreign = new List<Foreign>();
        static readonly List<Boundaries> boundaries = new List<Boundaries>();
        static readonly List<Renderer> scratch = new List<Renderer>();
        // Camera.GetAllCameras' buffer, grown when there are more cameras.
        static Camera[] cameras = new Camera[16];
        static readonly int[] related = new int[32];
        static Mesh quads;
        static Shader spriteShader, beeShader;
        static Item last, lightItem;
        static bool tracking, installed, rescan = true;
        static int flushedFrame = -1, frame, versions, foreignVersion;

        static readonly int dataId = Shader.PropertyToID("_EffectData"),
            rectId = Shader.PropertyToID("_EffectRect"), uvId = Shader.PropertyToID("_EffectUV"), lightId = Shader.PropertyToID("_LightTex"),
            lightSizeId = Shader.PropertyToID("_EffectLight"), mainTexId = Shader.PropertyToID("_MainTex"), colorId = Shader.PropertyToID("_Color"),
            wobbleOffsetId = Shader.PropertyToID("_WobbleOffset"), wobbleSpeedId = Shader.PropertyToID("_WobbleSpeed");

        // For the probe's heartbeat. Items and pages drawn in the last frame and other renderers
        // known; flushes and their time (ms) since the probe last reset them (it does at each
        // read); since the start: items handed back to their renderers, OverlapPoint answers, and
        // with -compat-collider-check the engine's checks and disagreements.
        public static int StatItems, StatPages, StatForeign, StatFlushes, StatReleased;
        // Stopwatch ticks since the probe last reset them: other renderers (visibility, cameras),
        // changed items (places, writes; with the other renderers' orders), page uploads, the
        // other renderers' orders.
        public static readonly long[] StatTicks = new long[4];
        public static double StatMs;
        public static long StatShapeCalls, StatCheckCalls, StatCheckMismatches;

        static string[] configuredFor;
        static bool enabled, checkColliders;

        public static bool Enabled()
        {
            if (!Platform.NewArgs(ref configuredFor)) return enabled;
            checkColliders = Platform.HasArg("-compat-collider-check");
            logPages = Platform.HasArg("-compat-effects-log");
            enabled = !Platform.HasArg("-compat-effects-original");
            if (enabled && spriteShader == null)
            {
                spriteShader = Shader.Find("Hidden/DrifterEffectSprite");
                if (spriteShader != null && !spriteShader.isSupported) spriteShader = null;
                beeShader = Shader.Find("Hidden/DrifterEffectBee");
                if (beeShader != null && !beeShader.isSupported) beeShader = null;
                Debug.Log("EffectSprites: sprite shader " + (spriteShader != null) + ", bee shader " + (beeShader != null));
            }
            if (spriteShader == null) enabled = false;
            return enabled;
        }

        static void Install()
        {
            if (installed) return;
            installed = true;
            Camera.onPreCull += PreCull;
            UnityEngine.SceneManagement.SceneManager.sceneLoaded += (scene, mode) => { rescan = true; };
            UnityEngine.SceneManagement.SceneManager.sceneUnloaded += scene => Unloaded();
        }

        // ---- Registration (after Object.Instantiate in Rain.Spawn, Sand.Spawn and Bees.SpawnBee)

        public static GameObject Instantiated(GameObject gameObject)
        {
            if ((object)gameObject == null || !Enabled() || !MainThread.Current) return gameObject;
            SpriteRenderer renderer = gameObject.GetComponent<SpriteRenderer>();
            if (renderer == null) return gameObject;
            Transform transform = gameObject.transform, parent = transform.parent;
            if (parent == null) return gameObject;
            Material material = renderer.sharedMaterial;
            if (material == null || renderer.sharedMaterials.Length != 1 || renderer.drawMode != SpriteDrawMode.Simple || renderer.flipX ||
                renderer.flipY || renderer.maskInteraction != SpriteMaskInteraction.None || transform.childCount != 0)
                return gameObject;
            Group group = null;
            foreach (Group g in groups)
                if (ReferenceEquals(g.transform, parent)) { group = g; break; }
            if (group == null)
            {
                group = NewGroup(parent, material);
                groups.Add(group);
            }
            if (group.shader == 0 || !ReferenceEquals(group.source, material)) return gameObject;
            Install();
            var item = new Item { group = group, gameObject = gameObject, transform = transform, renderer = renderer };
            item.active = gameObject.activeSelf;
            item.sprite = renderer.sprite;
            item.hasSprite = item.sprite != null;
            item.color = renderer.color;
            item.order = renderer.sortingOrder;
            item.sortingLayer = renderer.sortingLayerID;
            item.layer = gameObject.layer;
            item.position = transform.position;
            item.scale = transform.localScale;
            Angle(item, transform.localEulerAngles.z);
            group.items.Add(item);
            table.Set(gameObject, item);
            table.Set(transform, item);
            table.Set(renderer, item);
            tracking = true;
            Touch(item, Layout | Data);
            // From now on the effect draws it.
            gameObject.SetActive(false);
            return gameObject;
        }

        static Group NewGroup(Transform parent, Material material)
        {
            var group = new Group { transform = parent, source = material, name = parent.name };
            Vector3 scale = parent.lossyScale;
            Quaternion rotation = parent.rotation;
            bool plain = scale.x == 1f && scale.y == 1f && scale.z == 1f && rotation.x == 0f && rotation.y == 0f && rotation.z == 0f;
            Shader shader = material.shader;
            string name = shader != null ? shader.name : "";
            if (plain && !material.IsKeywordEnabled("PIXELSNAP_ON"))
            {
                if (name == "Sprites/Default") group.shader = SpriteShader;
                else if (name == "BeeSprite" && beeShader != null) group.shader = BeeShader;
            }
            if (group.shader != 0)
            {
                if (material.HasProperty(colorId)) group.materialColor = material.GetColor(colorId);
                if (material.HasProperty(wobbleOffsetId)) group.wobbleOffset = material.GetFloat(wobbleOffsetId);
                if (material.HasProperty(wobbleSpeedId)) group.wobbleSpeed = material.GetFloat(wobbleSpeedId);
            }
            Debug.Log("EffectSprites: " + group.name + " (" + name + ") " + (group.shader != 0 ? "drawn by pages" : "keeps its renderers") +
                      (plain ? "" : " (parent scaled or rotated)"));
            return group;
        }

        // ---- Hooks (BatchSprites in rewriter/ScriptCosts.cs): for a drop or bee the value is noted, for
        // anything else the call is made.

        static Item Find(object key)
        {
            if (!tracking || key == null) return null;
            Item item = last;
            if (item != null && (ReferenceEquals(item.transform, key) || ReferenceEquals(item.renderer, key) || ReferenceEquals(item.gameObject, key)))
                return item.released ? null : item;
            item = table.Get(key);
            if (item == null || item.released) return null;
            last = item;
            return item;
        }

        public static void SetActive(GameObject gameObject, bool value)
        {
            Item item = Find(gameObject);
            if (item == null) { gameObject.SetActive(value); return; }
            if (item.active != value) Touch(item, Layout);
            item.active = value;
        }

        public static void SetLayer(GameObject gameObject, int value)
        {
            Item item = Find(gameObject);
            if (item == null) { gameObject.layer = value; return; }
            if (item.layer != value) Touch(item, Layout);
            item.layer = value;
        }

        public static void SetSprite(SpriteRenderer renderer, Sprite value)
        {
            Item item = Find(renderer);
            if (item == null) { renderer.sprite = value; return; }
            if (ReferenceEquals(item.sprite, value)) return;
            item.sprite = value;
            item.hasSprite = value != null;
            Touch(item, Layout | Data);
        }

        // A colour set right after the drop's light read (RainDrop.Update) is its own colour: the
        // shader blends in the light.
        public static void SetColor(SpriteRenderer renderer, Color value)
        {
            Item item = Find(renderer);
            if (item == null) { renderer.color = value; return; }
            item.color = value;
            item.lit = item.lightRead;
            item.lightRead = false;
            Touch(item, Data);
        }

        public static void SetSortingOrder(Renderer renderer, int value)
        {
            Item item = Find(renderer);
            if (item == null) { renderer.sortingOrder = value; return; }
            if (item.order != value) Touch(item, Layout);
            item.order = value;
        }

        // The engine call is made for drops and bees too (BurnBee reads the block back).
        public static void SetPropertyBlock(Renderer renderer, MaterialPropertyBlock block)
        {
            renderer.SetPropertyBlock(block);
            Item item = Find(renderer);
            if (item == null) return;
            item.hasBlock = block != null && !block.isEmpty;
            if (!item.hasBlock) return;
            item.wobbleOffset = block.GetFloat(wobbleOffsetId);
            item.wobbleSpeed = block.GetFloat(wobbleSpeedId);
            item.blockColor = block.GetVector(colorId);
            item.blockTexture = block.GetTexture(mainTexId);
            item.set = null;
            Touch(item, Layout | Data);
        }

        public static void Destroy(Object target)
        {
            Object.Destroy(target);
            Item item = Find(target);
            if (item == null) return;
            item.dead = true;
            Touch(item, Layout);
        }

        public static void SetPosition(Transform transform, Vector3 value)
        {
            Item item = Find(transform);
            if (item == null) { transform.position = value; return; }
            item.position = value;
            Touch(item, Data);
        }

        // TransformReads.SetPosition in RainDrop.Update.
        public static void SetReadsPosition(Transform transform, Vector3 value)
        {
            Item item = Find(transform);
            if (item == null) { TransformReads.SetPosition(transform, value); return; }
            item.position = value;
            Touch(item, Data);
            TransformReads.Remember(transform, value);
        }

        public static void SetLocalScale(Transform transform, Vector3 value)
        {
            Item item = Find(transform);
            if (item == null) { transform.localScale = value; return; }
            item.scale = value;
            Columns(item);
            Touch(item, Data);
        }

        public static void SetLocalEulerAngles(Transform transform, Vector3 value)
        {
            Item item = Find(transform);
            if (item != null && value.x == 0f && value.y == 0f)
            {
                Angle(item, value.z);
                Touch(item, Data);
                return;
            }
            if (item != null) Release(item);
            transform.localEulerAngles = value;
        }

        public static void SetRotation(Transform transform, Quaternion value)
        {
            Item item = Find(transform);
            if (item != null && value.x == 0f && value.y == 0f)
            {
                // About z: (0, 0, sin(a / 2), cos(a / 2)).
                float n = value.z * value.z + value.w * value.w;
                if (n <= 0f) n = 1f;
                item.cos = (value.w * value.w - value.z * value.z) / n;
                item.sin = 2f * value.z * value.w / n;
                Columns(item);
                Touch(item, Data);
                return;
            }
            if (item != null) Release(item);
            transform.rotation = value;
        }

        // TransformReads.Position (RainDrop.Update): the noted position.
        public static bool TryPosition(Transform transform, out Vector3 value)
        {
            Item item = Find(transform);
            lightItem = item;
            if (item == null)
            {
                value = default(Vector3);
                return false;
            }
            value = item.position;
            return true;
        }

        // RainDrop.Update's Texture2D.GetPixel, after it read the drop's position: transparent
        // black if the shader reads the light for that drop.
        public static Color GetPixel(Texture2D texture, int x, int y)
        {
            Item item = lightItem;
            if (item == null || item.released || texture == null) return texture.GetPixel(x, y);
            item.lightRead = true;
            item.light = texture;
            return default(Color);
        }

        static void Angle(Item item, float degrees)
        {
            float radians = degrees * Mathf.Deg2Rad;
            item.cos = Mathf.Cos(radians);
            item.sin = Mathf.Sin(radians);
            Columns(item);
        }

        static void Columns(Item item)
        {
            item.m00 = item.cos * item.scale.x;
            item.m10 = item.sin * item.scale.x;
            item.m01 = -item.sin * item.scale.y;
            item.m11 = item.cos * item.scale.y;
        }

        // Back to its own renderer, with the state the game gave it.
        static void Release(Item item)
        {
            if (item.released) return;
            item.released = true;
            StatReleased++;
            Free(item);
            if (item.dead || item.gameObject == null || item.renderer == null) return;
            Apply(item);
            item.gameObject.SetActive(item.active);
        }

        // The noted state on the item's own renderer (a lit drop's colour as RainDrop computed it).
        static void Apply(Item item)
        {
            item.renderer.sprite = item.sprite;
            Color color = item.color;
            if (item.lit && item.light != null)
            {
                Color pixel = item.light.GetPixel((int)item.position.x + (item.light.width >> 1), (int)item.position.y + (item.light.height >> 1));
                color = Color.Lerp(color, pixel, pixel.a);
            }
            item.renderer.color = color;
            item.renderer.sortingOrder = item.order;
            item.renderer.sortingLayerID = item.sortingLayer;
            item.gameObject.layer = item.layer;
            item.transform.position = item.position;
            item.transform.localScale = item.scale;
            item.transform.localEulerAngles = new Vector3(0f, 0f, Mathf.Atan2(item.sin, item.cos) * Mathf.Rad2Deg);
        }

        // The probe's A/B check (-diag-check-effects), between frames: 0 draws the pages (as
        // normally), 1 every item with its own renderer in the state the game gave it instead, 2
        // neither.
        public static void Show(int mode)
        {
            foreach (Group group in groups)
            {
                foreach (Page page in group.pages) if (page.renderer != null) page.renderer.enabled = mode == 0 && page.enabled;
                foreach (Item item in group.items)
                {
                    if (item.released || item.dead || item.gameObject == null || item.renderer == null) continue;
                    if (mode == 1 && item.active) Apply(item);
                    item.gameObject.SetActive(mode == 1 && item.active);
                }
            }
        }

        static void ReleaseAll()
        {
            foreach (Group group in groups)
            {
                foreach (Item item in group.items) Release(item);
                HidePages(group);
            }
        }

        // ---- Other renderers

        // After every Object.Instantiate, new GameObject(name, types) and AddComponent in the game's code.
        public static void Created(Object created)
        {
            if (!tracking || (object)created == null || !MainThread.Current) return;
            GameObject gameObject = created as GameObject;
            if ((object)gameObject == null)
            {
                Component component = created as Component;
                if (component == null) return;
                gameObject = component.gameObject;
            }
            if (gameObject == null) return;
            gameObject.GetComponentsInChildren<Renderer>(true, scratch);
            foreach (Renderer renderer in scratch) AddForeign(renderer);
            scratch.Clear();
        }

        // Every sorting layer assignment in the game's code.
        public static void SetSortingLayerID(Renderer renderer, int value)
        {
            renderer.sortingLayerID = value;
            SortingLayerSet(renderer);
        }

        public static void SetSortingLayerName(Renderer renderer, string value)
        {
            renderer.sortingLayerName = value;
            SortingLayerSet(renderer);
        }

        static void SortingLayerSet(Renderer renderer)
        {
            if (!tracking || !MainThread.Current) return;
            Item item = Find(renderer);
            if (item != null && item.sortingLayer != renderer.sortingLayerID)
            {
                item.sortingLayer = renderer.sortingLayerID;
                Touch(item, Layout);
            }
            Foreign entry = foreignTable.Get(renderer);
            if (entry != null && entry.sortingLayer != renderer.sortingLayerID)
            {
                entry.sortingLayer = renderer.sortingLayerID;
                foreignVersion++;
            }
        }

        static void AddForeign(Renderer renderer)
        {
            if (renderer == null || table.Get(renderer) != null || pageTable.Get(renderer) != null || foreignTable.Get(renderer) != null) return;
            var entry = new Foreign { renderer = renderer, gameObject = renderer.gameObject, index = foreign.Count };
            Refresh(entry);
            foreign.Add(entry);
            foreignTable.Set(renderer, entry);
            foreignVersion++;
        }

        static void Refresh(Foreign entry)
        {
            if (entry.gone) return;
            if (entry.renderer == null)
            {
                entry.gone = true;
                foreignVersion++;
                return;
            }
            bool shown = entry.renderer.enabled && entry.gameObject.activeInHierarchy;
            int layer = entry.gameObject.layer, sortingLayer = entry.renderer.sortingLayerID;
            if (shown == entry.shown && layer == entry.layer && sortingLayer == entry.sortingLayer) return;
            entry.shown = shown;
            entry.layer = layer;
            entry.sortingLayer = sortingLayer;
            foreignVersion++;
        }

        static void Rescan()
        {
            rescan = false;
            foreign.Clear();
            foreignTable.Clear();
            foreignVersion++;
            foreach (Renderer renderer in Resources.FindObjectsOfTypeAll<Renderer>())
                if (renderer.gameObject.scene.IsValid()) AddForeign(renderer);
        }

        // After a scene unloads: items, pages and groups whose objects are gone are dropped (an
        // item's slot is freed first, so its page does not draw it on), and the tables made again.
        static void Unloaded()
        {
            for (int g = groups.Count - 1; g >= 0; g--)
            {
                Group group = groups[g];
                foreach (Item item in group.items)
                    if (item.gameObject == null || item.renderer == null) Free(item);
                group.items.RemoveAll(item => item.gameObject == null || item.renderer == null || item.released);
                group.pages.RemoveAll(page => page.renderer == null);
                if (group.transform != null && group.items.Count > 0) continue;
                foreach (Set set in group.sets) if (set.material != null) Object.Destroy(set.material);
                foreach (Page page in group.pages) if (page.gameObject != null) Object.Destroy(page.gameObject);
                groups.RemoveAt(g);
            }
            Reindex();
            last = lightItem = null;
            tracking = groups.Count > 0;
        }

        // The identity tables made again from what is left (they never remove a key).
        static void Reindex()
        {
            table.Clear();
            pageTable.Clear();
            foreach (Group group in groups)
            {
                foreach (Item item in group.items)
                {
                    table.Set(item.gameObject, item);
                    table.Set(item.transform, item);
                    table.Set(item.renderer, item);
                }
                foreach (Page page in group.pages) pageTable.Set(page.renderer, page);
                foreach (Item item in group.items)
                    if (item.page != null && item.page.renderer == null)
                    {
                        item.page = null;
                        item.slot = -1;
                        Touch(item, Layout | Data);
                    }
            }
            foreign.RemoveAll(entry => entry.renderer == null);
            foreignTable.Clear();
            for (int i = 0; i < foreign.Count; i++)
            {
                foreign[i].index = i;
                foreignTable.Set(foreign[i].renderer, foreign[i]);
            }
            boundaries.Clear();
            foreignVersion++;
        }

        // ---- Drawing

        // What a hook changed: where the item belongs (activation, order, layer, sprite, block,
        // destruction) or only what its slot holds (position, scale, rotation, colour).
        const int Layout = 1, Data = 2;
        // Items touched since the last flush (grown as needed).
        static Item[] dirty = new Item[1024];
        static int dirtyCount;

        static void Touch(Item item, int flags)
        {
            if (item.dirty == 0)
            {
                if (dirtyCount == dirty.Length) Array.Resize(ref dirty, dirtyCount * 2);
                dirty[dirtyCount++] = item;
            }
            item.dirty |= flags;
        }

        static void PreCull(Camera camera)
        {
            if (!tracking) return;
            int now = Time.frameCount;
            if (flushedFrame == now || !MainThread.Current) return;
            flushedFrame = now;
            long start = System.Diagnostics.Stopwatch.GetTimestamp();
            Flush();
            StatMs += (System.Diagnostics.Stopwatch.GetTimestamp() - start) * 1000.0 / System.Diagnostics.Stopwatch.Frequency;
            StatFlushes++;
        }

        // Only what changed since the last frame: the items the hooks touched, the pages they
        // are in, and the pages a change of the other renderers' orders cut through.
        static void Flush()
        {
            if (!Enabled())
            {
                ReleaseAll();
                return;
            }
            frame++;
            long start = System.Diagnostics.Stopwatch.GetTimestamp();
            if (rescan) Rescan();
            RelatedLayers();
            // Visibility of the other renderers, a share of them a frame.
            for (int i = frame & (VisibilityFrames - 1); i < foreign.Count; i += VisibilityFrames) Refresh(foreign[i]);
            StatForeign = foreign.Count;
            long t1 = System.Diagnostics.Stopwatch.GetTimestamp();
            StatTicks[0] += t1 - start;
            SplitCutPages();
            for (int i = 0; i < dirtyCount; i++)
            {
                Item item = dirty[i];
                dirty[i] = null;
                int flags = item.dirty;
                item.dirty = 0;
                Process(item, flags);
            }
            dirtyCount = 0;
            long t2 = System.Diagnostics.Stopwatch.GetTimestamp();
            StatTicks[1] += t2 - t1;
            FinishPages();
            if (logPages && frame % LogFrames == 0) LogPages();
            StatTicks[2] += System.Diagnostics.Stopwatch.GetTimestamp() - t2;
        }

        // For each layer, the layers drawn by a camera that draws it.
        static void RelatedLayers()
        {
            int count = Camera.allCamerasCount;
            if (cameras.Length < count) cameras = new Camera[count * 2];
            count = Camera.GetAllCameras(cameras);
            for (int l = 0; l < 32; l++) related[l] = 0;
            for (int i = 0; i < count; i++)
            {
                int mask = cameras[i].cullingMask;
                for (int l = 0; l < 32; l++) if ((mask & (1 << l)) != 0) related[l] |= mask;
                cameras[i] = null;
            }
        }

        // Pages whose order range another renderer's order now cuts (a walking character's): the
        // items on the side with fewer of them find new places, the others stay.
        static void SplitCutPages()
        {
            foreach (Group group in groups)
                foreach (Set set in group.sets)
                {
                    Boundaries cuts = BoundariesFor(set.layer, set.sortingLayer);
                    if (set.cutsVersion == cuts.version) continue;
                    set.cutsVersion = cuts.version;
                    List<int> orders = cuts.orders;
                    foreach (Page page in group.pages)
                    {
                        if (!ReferenceEquals(page.set, set) || page.count == 0) continue;
                        if (Below(orders, page.minOrder) == Below(orders, page.maxOrder)) continue;
                        Split(page, orders);
                    }
                }
        }

        // Uploads the changed pages; counts what is drawn.
        static void FinishPages()
        {
            int pages = 0, items = 0;
            foreach (Group group in groups)
                foreach (Page page in group.pages)
                {
                    if (page.dirty) Finish(page);
                    if (page.count == 0) continue;
                    pages++;
                    items += page.count;
                }
            StatPages = pages;
            StatItems = items;
        }

        // -compat-effects-log: every LogFrames frames (5 s at 60 fps), each page's set, range, item
        // count and the other renderers' orders (for tuning).
        static bool logPages;
        const int LogFrames = 300;

        static void LogPages()
        {
            var text = new System.Text.StringBuilder("EffectSprites pages:");
            foreach (Group group in groups)
            {
                text.Append(" | " + group.name + ":");
                foreach (Page page in group.pages)
                    if (page.count > 0) text.Append(" " + group.sets.IndexOf(page.set) + "[" + page.minOrder + ".." + page.maxOrder + "]x" + page.count);
                foreach (Set set in group.sets)
                    text.Append(" cuts " + string.Join(",", BoundariesFor(set.layer, set.sortingLayer).orders.Select(o => o.ToString()).ToArray()));
            }
            Debug.Log(text.ToString());
        }

        static void Process(Item item, int flags)
        {
            if (item.released || item.dead || !item.active || !item.hasSprite || item.gameObject == null)
            {
                Free(item);
                return;
            }
            if ((flags & Layout) != 0 || item.page == null)
            {
                if (!Resolve(item.group, item)) return;  // handed back to its renderer (Release)
                Set set = item.set;
                List<int> cuts = BoundariesFor(set.layer, set.sortingLayer).orders;
                Page page = item.page;
                bool same = page != null && ReferenceEquals(page.set, set);
                if (page != null && page.count == 1) page.minOrder = page.maxOrder = item.order;
                bool fits = same && Fits(page, item.order, cuts);
                // An item of a page with few items moves to a fuller one it fits (pages fill up
                // instead of splitting into ever more as drops come and go).
                Page target = fits && page.count > Slots / 4 ? null : FindPage(item, set, cuts, page);
                if (fits && (target == null || target.count <= page.count)) Widen(page, item.order);
                else
                {
                    Free(item);
                    Put(item, set, target);
                    flags |= Data;
                }
                item.page.dirty = true;
            }
            if ((flags & Data) != 0)
            {
                Write(item.group, item.set, item.page, item.slot, item);
                item.page.dirty = true;
            }
        }

        // How many of the other renderers' (sorted) orders are at most the order: an item draws
        // after those (and, at an equal order, after them too).
        static int Below(List<int> cuts, int order)
        {
            int a = 0, b = cuts.Count;
            while (a < b)
            {
                int m = (a + b) >> 1;
                if (cuts[m] <= order) a = m + 1; else b = m;
            }
            return a;
        }

        // Whether the page can hold an item of that order: no other renderer's order would lie
        // within its range.
        static bool Fits(Page page, int order, List<int> cuts)
        {
            int min = order < page.minOrder ? order : page.minOrder, max = order > page.maxOrder ? order : page.maxOrder;
            return Below(cuts, min) == Below(cuts, max);
        }

        static void Widen(Page page, int order)
        {
            if (order < page.minOrder) page.minOrder = order;
            if (order > page.maxOrder) page.maxOrder = order;
        }

        // A page that another renderer's order now cuts: its exact range first; if still cut, the
        // items outside the side holding most of them are moved.
        static void Split(Page page, List<int> cuts)
        {
            int min = int.MaxValue, max = int.MinValue;
            for (int s = 0; s < Slots; s++)
            {
                Item item = page.items[s];
                if (item == null) continue;
                if (item.order < min) min = item.order;
                if (item.order > max) max = item.order;
            }
            page.minOrder = min;
            page.maxOrder = max;
            if (Below(cuts, min) == Below(cuts, max)) return;
            int keep = -1, most = 0;
            for (int s = 0; s < Slots; s++)
            {
                Item item = page.items[s];
                if (item == null) continue;
                int side = Below(cuts, item.order), count = 0;
                if (side == keep) continue;
                for (int t = 0; t < Slots; t++)
                    if (page.items[t] != null && Below(cuts, page.items[t].order) == side) count++;
                if (count > most) { most = count; keep = side; }
            }
            for (int s = 0; s < Slots; s++)
            {
                Item item = page.items[s];
                if (item != null && Below(cuts, item.order) != keep) Touch(item, Layout);
            }
        }

        static void Free(Item item)
        {
            Page page = item.page;
            if (page == null) return;
            int slot = item.slot;
            page.items[slot] = null;
            page.count--;
            Vacate(page.data, slot);
            page.dirty = true;
            item.page = null;
            item.slot = -1;
        }

        // An empty slot's vectors: a quad far outside any view (position Far, scale 1).
        static void Vacate(Vector4[] data, int slot)
        {
            data[slot * 3] = new Vector4(Far, Far, 1f, 0f);
            data[slot * 3 + 1] = Vector4.zero;
            data[slot * 3 + 2] = Vector4.zero;
        }

        // Of the set's pages with room that the item fits (other than its own), the one whose range
        // lies nearest the item's order, and of those the fullest.
        static Page FindPage(Item item, Set set, List<int> cuts, Page except)
        {
            Page target = null;
            long best = long.MaxValue;
            foreach (Page page in item.group.pages)
            {
                if (page.count == 0 || page.count == Slots || ReferenceEquals(page, except) || !ReferenceEquals(page.set, set) ||
                    !Fits(page, item.order, cuts))
                    continue;
                long distance = item.order < page.minOrder ? (long)page.minOrder - item.order : item.order > page.maxOrder ? (long)item.order - page.maxOrder : 0;
                long score = distance * (Slots + 1) + (Slots - page.count);
                if (score < best)
                {
                    best = score;
                    target = page;
                }
            }
            return target;
        }

        // Into the page (or an empty or new one), at its first free slot.
        static void Put(Item item, Set set, Page target)
        {
            if (target == null)
            {
                foreach (Page page in item.group.pages)
                    if (page.count == 0) { target = page; break; }
                if (target == null) target = NewPage(item.group);
                target.set = set;
                target.minOrder = target.maxOrder = item.order;
                target.placed = false;
            }
            else Widen(target, item.order);
            int slot = 0;
            while (target.items[slot] != null) slot++;
            target.items[slot] = item;
            target.count++;
            target.dirty = true;
            item.page = target;
            item.slot = slot;
        }

        static byte Byte(float value)
        {
            float clamped = value < 0f ? 0f : value > 1f ? 1f : value;
            return (byte)(float)(clamped * 255f + 0.5f);
        }

        static void Write(Group group, Set set, Page page, int slot, Item item)
        {
            Vector3 p = item.position;
            int at = slot * 3;
            if (group.shader == SpriteShader)
            {
                bool lit = item.lit && item.light != null;
                if (lit && !ReferenceEquals(set.light, item.light)) SetLight(set, item.light);
                page.data[at] = new Vector4(p.x, p.y, item.m00, item.m10);
                page.data[at + 1] = new Vector4(item.m01, item.m11, item.spriteIndex, lit ? 1f : 0f);
                // Sprites/Default: the renderer's colour as vertex bytes, times the material's _Color.
                // A lit drop's colour is blended with the light and rounded to bytes by the shader.
                Color tint = item.hasBlock ? item.blockColor : group.materialColor;
                Color c = item.color;
                page.data[at + 2] = lit ? new Vector4(c.r, c.g, c.b, c.a)
                                        : new Vector4(Byte(c.r) / 255f * tint.r, Byte(c.g) / 255f * tint.g, Byte(c.b) / 255f * tint.b, Byte(c.a) / 255f * tint.a);
            }
            else
            {
                // BeeSprite: tex * _Color (its vertex colour is unused).
                page.data[at] = new Vector4(p.x, p.y, item.scale.x, item.hasBlock ? item.wobbleOffset : group.wobbleOffset);
                page.data[at + 1] = new Vector4(item.hasBlock ? item.wobbleSpeed : group.wobbleSpeed, 0f, item.spriteIndex, 0f);
                Color tint = item.hasBlock ? item.blockColor : group.materialColor;
                page.data[at + 2] = new Vector4(tint.r, tint.g, tint.b, tint.a);
            }
        }

        // Uploads a changed page; one without items is turned off (and reused for anything).
        static void Finish(Page page)
        {
            page.dirty = false;
            MeshRenderer renderer = page.renderer;
            if (renderer == null) return;
            if (page.count == 0)
            {
                if (page.enabled) renderer.enabled = page.enabled = false;
                return;
            }
            Set set = page.set;
            // Uploaded in increasing order (the slots keep their items; the upload is sorted).
            int n = 0;
            int[] sorted = page.sorted;
            for (int s = 0; s < Slots; s++)
            {
                Item item = page.items[s];
                if (item == null) continue;
                int k = n++;
                while (k > 0 && page.items[sorted[k - 1]].order > item.order) { sorted[k] = sorted[k - 1]; k--; }
                sorted[k] = s;
            }
            Vector4[] data = page.data, upload = page.upload;
            for (int k = 0; k < n; k++)
            {
                int from = sorted[k] * 3, to = k * 3;
                upload[to] = data[from];
                upload[to + 1] = data[from + 1];
                upload[to + 2] = data[from + 2];
            }
            for (int k = n; k < page.uploaded; k++) Vacate(upload, k);
            page.uploaded = n;
            int order = page.minOrder = page.items[sorted[0]].order;
            page.maxOrder = page.items[sorted[n - 1]].order;
            page.block.SetVectorArray(dataId, upload);
            renderer.SetPropertyBlock(page.block);
            if (!ReferenceEquals(page.material, set.material)) renderer.sharedMaterial = page.material = set.material;
            if (!page.placed || page.order != order) renderer.sortingOrder = page.order = order;
            if (!page.placed || page.sortingLayer != set.sortingLayer) renderer.sortingLayerID = page.sortingLayer = set.sortingLayer;
            if (page.layer != set.layer) page.gameObject.layer = page.layer = set.layer;
            page.placed = true;
            if (!page.enabled) renderer.enabled = page.enabled = true;
        }

        static Page NewPage(Group group)
        {
            var gameObject = new GameObject("DrifterEffectPage");
            gameObject.transform.SetParent(group.transform, false);
            gameObject.transform.position = new Vector3(0f, 0f, PageZ);
            gameObject.AddComponent<MeshFilter>().sharedMesh = Quads();
            MeshRenderer renderer = gameObject.AddComponent<MeshRenderer>();
            renderer.shadowCastingMode = ShadowCastingMode.Off;
            renderer.receiveShadows = false;
            renderer.lightProbeUsage = LightProbeUsage.Off;
            renderer.reflectionProbeUsage = ReflectionProbeUsage.Off;
            renderer.motionVectorGenerationMode = MotionVectorGenerationMode.ForceNoMotion;
            renderer.enabled = false;
            var page = new Page { gameObject = gameObject, renderer = renderer };
            for (int s = 0; s < Slots; s++) Vacate(page.data, s);
            group.pages.Add(page);
            pageTable.Set(renderer, page);
            return page;
        }

        static void HidePages(Group group)
        {
            foreach (Page page in group.pages)
            {
                if (!page.enabled || page.renderer == null) continue;
                page.renderer.enabled = page.enabled = false;
            }
        }

        // Quad k of the mesh: vertices (x, y, k) with x, y 0 or 1, the corners of the sprite's rectangle.
        static Mesh Quads()
        {
            if (quads != null) return quads;
            var vertices = new Vector3[Slots * 4];
            var triangles = new int[Slots * 6];
            for (int k = 0; k < Slots; k++)
            {
                vertices[k * 4] = new Vector3(0f, 0f, k);
                vertices[k * 4 + 1] = new Vector3(0f, 1f, k);
                vertices[k * 4 + 2] = new Vector3(1f, 1f, k);
                vertices[k * 4 + 3] = new Vector3(1f, 0f, k);
                triangles[k * 6] = k * 4;
                triangles[k * 6 + 1] = k * 4 + 1;
                triangles[k * 6 + 2] = k * 4 + 2;
                triangles[k * 6 + 3] = k * 4 + 2;
                triangles[k * 6 + 4] = k * 4 + 3;
                triangles[k * 6 + 5] = k * 4;
            }
            quads = new Mesh { name = "DrifterEffectQuads", hideFlags = HideFlags.DontUnloadUnusedAsset };
            quads.vertices = vertices;
            quads.triangles = triangles;
            // The shader places the quads anywhere: never culled.
            quads.bounds = new Bounds(Vector3.zero, new Vector3(1e5f, 1e5f, 1f));
            quads.UploadMeshData(true);
            return quads;
        }

        // The other renderers' orders for a layer and sorting layer this frame. Which renderers
        // count is kept until one of them changes visibility, layer or sorting layer or the
        // cameras change; their orders are read every OrderFrames frames, and every frame for
        // MovingFrames after one changed (a walking character's); version changes when the
        // sorted orders do.
        static Boundaries BoundariesFor(int layer, int sortingLayer)
        {
            Boundaries result = null;
            foreach (Boundaries b in boundaries)
                if (b.layer == layer && b.sortingLayer == sortingLayer) { result = b; break; }
            if (result == null)
            {
                result = new Boundaries { layer = layer, sortingLayer = sortingLayer, version = ++versions };
                boundaries.Add(result);
            }
            if (result.frame == frame) return result;
            long start = System.Diagnostics.Stopwatch.GetTimestamp();
            result.frame = frame;
            int mask = layer >= 0 && layer < 32 ? related[layer] : 0;
            if (result.foreignVersion != foreignVersion || result.mask != mask) Members(result, mask);
            bool changed = result.changed;
            result.changed = false;
            List<Foreign> members = result.members;
            for (int i = 0; i < members.Count; i++)
            {
                Foreign entry = members[i];
                if (entry.ordered && entry.movingUntil < frame && ((frame + entry.index) & (OrderFrames - 1)) != 0) continue;
                if (entry.renderer == null)
                {
                    entry.gone = true;
                    foreignVersion++;
                    continue;
                }
                int order = entry.renderer.sortingOrder;
                if (!entry.ordered || order != entry.order)
                {
                    if (entry.ordered) entry.movingUntil = frame + MovingFrames;
                    entry.order = order;
                    entry.ordered = true;
                    changed = true;
                }
            }
            if (changed) SortOrders(result);
            StatTicks[3] += System.Diagnostics.Stopwatch.GetTimestamp() - start;
            return result;
        }

        // The other renderers shown on the sorting layer and on a layer the cameras (mask) draw.
        static void Members(Boundaries result, int mask)
        {
            result.foreignVersion = foreignVersion;
            result.mask = mask;
            result.members.Clear();
            foreach (Foreign entry in foreign)
                if (!entry.gone && entry.shown && entry.sortingLayer == result.sortingLayer && (mask & (1 << entry.layer)) != 0) result.members.Add(entry);
            result.changed = true;
        }

        // The members' distinct orders, sorted; a new version if they differ from the last.
        static void SortOrders(Boundaries result)
        {
            List<int> orders = result.scratch;
            orders.Clear();
            foreach (Foreign entry in result.members) if (!entry.gone) orders.Add(entry.order);
            orders.Sort();
            int distinct = 0;
            for (int i = 0; i < orders.Count; i++)
                if (i == 0 || orders[i] != orders[distinct - 1]) orders[distinct++] = orders[i];
            orders.RemoveRange(distinct, orders.Count - distinct);
            bool same = orders.Count == result.orders.Count;
            for (int i = 0; same && i < orders.Count; i++) same = orders[i] == result.orders[i];
            if (same) return;
            result.scratch = result.orders;
            result.orders = orders;
            result.version = ++versions;
        }

        // The item's set (material) and sprite index; false (and back to its renderer) if the
        // effect cannot draw it.
        static bool Resolve(Group group, Item item)
        {
            Sprite sprite = item.sprite;
            if (item.set != null && ReferenceEquals(item.setSprite, sprite) && item.setLayer == item.layer && item.setSortingLayer == item.sortingLayer)
                return true;
            foreach (Set set in group.sets)
            {
                if (set.layer != item.layer || set.sortingLayer != item.sortingLayer) continue;
                for (int i = 0; i < set.spriteCount; i++)
                    if (ReferenceEquals(set.sprites[i], sprite))
                    {
                        Assign(item, set, i);
                        return true;
                    }
            }
            Texture texture = sprite.texture;
            if (texture == null || (item.hasBlock && item.blockTexture != null && !ReferenceEquals(item.blockTexture, texture)))
            {
                Release(item);
                return false;
            }
            Set target = SetFor(group, item, texture);
            Vector4 rect, uv;
            if (!Geometry(sprite, out rect, out uv))
            {
                Debug.LogWarning("EffectSprites: " + group.name + ": sprite " + sprite.name + " is packed rotated; it keeps its renderer");
                Release(item);
                return false;
            }
            int index = target.spriteCount++;
            target.sprites[index] = sprite;
            target.rects[index] = rect;
            target.uvs[index] = uv;
            target.material.SetVectorArray(rectId, target.rects);
            target.material.SetVectorArray(uvId, target.uvs);
            Assign(item, target, index);
            return true;
        }

        // A set of the item's layers and texture with room for a sprite, made if there is none.
        static Set SetFor(Group group, Item item, Texture texture)
        {
            foreach (Set set in group.sets)
                if (set.layer == item.layer && set.sortingLayer == item.sortingLayer && ReferenceEquals(set.texture, texture) && set.spriteCount < MaxSprites)
                    return set;
            var material = new Material(group.shader == BeeShader ? beeShader : spriteShader) { hideFlags = HideFlags.DontUnloadUnusedAsset };
            material.name = "DrifterEffect " + group.name;
            material.SetTexture(mainTexId, texture);
            var made = new Set { layer = item.layer, sortingLayer = item.sortingLayer, texture = texture, material = material };
            group.sets.Add(made);
            return made;
        }

        static void Assign(Item item, Set set, int index)
        {
            item.set = set;
            item.setSprite = item.sprite;
            item.setLayer = item.layer;
            item.setSortingLayer = item.sortingLayer;
            item.spriteIndex = index;
        }

        // The sprite's vertex rectangle (pivot-relative, in units) and the UVs at its corners
        // (min x, min y) and (max x, max y), if its UVs are an axis-aligned map of its vertices
        // (a tight mesh's quad over the same rectangle draws the same texels; the rest are
        // transparent).
        static bool Geometry(Sprite sprite, out Vector4 rect, out Vector4 uv)
        {
            Vector2[] vertices = sprite.vertices, uvs = sprite.uv;
            rect = uv = Vector4.zero;
            if (vertices.Length < 3 || uvs.Length != vertices.Length) return false;
            int minX = 0, maxX = 0, minY = 0, maxY = 0;
            for (int i = 1; i < vertices.Length; i++)
            {
                if (vertices[i].x < vertices[minX].x) minX = i;
                if (vertices[i].x > vertices[maxX].x) maxX = i;
                if (vertices[i].y < vertices[minY].y) minY = i;
                if (vertices[i].y > vertices[maxY].y) maxY = i;
            }
            float x0 = vertices[minX].x, x1 = vertices[maxX].x, y0 = vertices[minY].y, y1 = vertices[maxY].y;
            float u0 = uvs[minX].x, u1 = uvs[maxX].x, v0 = uvs[minY].y, v1 = uvs[maxY].y;
            if (x1 <= x0 || y1 <= y0) return false;
            float tolU = Mathf.Abs(u1 - u0) * 1e-3f + 1e-6f, tolV = Mathf.Abs(v1 - v0) * 1e-3f + 1e-6f;
            for (int i = 0; i < vertices.Length; i++)
            {
                float u = u0 + (vertices[i].x - x0) / (x1 - x0) * (u1 - u0), v = v0 + (vertices[i].y - y0) / (y1 - y0) * (v1 - v0);
                if (Mathf.Abs(uvs[i].x - u) > tolU || Mathf.Abs(uvs[i].y - v) > tolV) return false;
            }
            rect = new Vector4(x0, y0, x1, y1);
            uv = new Vector4(u0, v0, u1, v1);
            return true;
        }

        static void SetLight(Set set, Texture2D light)
        {
            set.light = light;
            set.material.SetTexture(lightId, light);
            int width = light.width, height = light.height;
            set.material.SetVector(lightSizeId, new Vector4(width, height, width >> 1, height >> 1));
        }
    }
}
