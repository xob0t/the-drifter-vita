using System;
using System.Collections.Generic;
using UnityEngine;
using Object = UnityEngine.Object;

// Script costs: calls the game repeats every frame, answered from what was kept (text widths, clickable angles, animation
// directions, input keys, components) or skipped when they would write an unchanged value (colliders, renderers, hierarchy).
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // PowerTools' TextWrapper.GetTextWidth (see FastTextWidths in rewriter/Gameplay.cs): the
    // width of a string is the sum of its characters' widths, kept in the wrapper's Hashtable
    // (boxed char keys and float values); a character not seen yet is measured by setting it
    // as the TextMesh's text and reading the renderer's bounds. The original reads the mesh's
    // text first and sets it back at the end of every call, and setting a TextMesh's text
    // rebuilds its mesh. WrapTextMinimiseWidth measures each word once per trial width, so the
    // controller's hover text (GuiInfoBar, set again every frame while a hotspot is selected)
    // cost the PS Vita up to 10 ms a frame. Here the text is read and set back only when a
    // character was measured; otherwise the mesh keeps the same text either way. Widths are
    // added in the same order. ASCII widths found in the Hashtable are also kept in an array
    // for the last Hashtable used (entries are only ever added there, never changed).
    public static class TextWidths
    {
        static System.Collections.Hashtable lastDict;
        // Widths of the characters below 128 (NaN: not read yet).
        static readonly float[] ascii = new float[128];

        public static float Measure(TextMesh textMesh, Renderer renderer, System.Collections.Hashtable dict, string s)
        {
            bool main = MainThread.Current;
            if (main && !ReferenceEquals(dict, lastDict))
            {
                for (int i = 0; i < ascii.Length; i++) ascii[i] = float.NaN;
                lastDict = dict;
            }
            int length = s.Length;
            float total = 0f;
            string original = null;
            bool changed = false, tag = false;
            for (int i = 0; i < length; i++)
            {
                char c = s[i];
                if (c == '<') tag = true;
                if (!tag)
                {
                    float width = main && c < 128 ? ascii[c] : float.NaN;
                    if (float.IsNaN(width))
                    {
                        object known = dict[c];
                        if (known != null)
                        {
                            width = (float)known;
                        }
                        else
                        {
                            if (!changed)
                            {
                                original = textMesh.text;
                                changed = true;
                            }
                            textMesh.text = c.ToString();
                            width = renderer.bounds.size.x;
                            dict.Add(c, width);
                        }
                        if (main && c < 128) ascii[c] = width;
                    }
                    total += width;
                }
                if (c == '>') tag = false;
            }
            if (changed) textMesh.text = original;
            return total;
        }
    }

    // SystemInput.GetControllerClickableAngles (see CacheClickableAngles in
    // rewriter/Gameplay.cs): the angle of each selectable thing around the player, then up
    // to 100 passes pushing apart angles closer than the selection angle (ControllerAnglePush,
    // recursive). The game asks twice a frame while the right stick is held (SystemInput and
    // ControllerHud), about 1.7 ms each on the PS Vita with LabKitchen's hotspots. The
    // spreading depends only on the first angles and the selection angle, so when both are
    // the last call's, a copy of the last result is returned. (Skipped then: the "Hit recurse
    // count" logging of a spreading that needs more than ten nested pushes.)
    public static class ClickableAngles
    {
        static float[] lastInput, lastResult, pending;
        static float lastSelect, pendingSelect;

        // Before the spreading: a copy of the last result for the same input, or null.
        public static float[] Find(float[] angles, float selectAngle)
        {
            pending = null;
            if (!MainThread.Current) return null;
            if (lastInput != null && lastResult != null && selectAngle.Equals(lastSelect) && Same(lastInput, angles))
                return (float[])lastResult.Clone();
            pending = (float[])angles.Clone();
            pendingSelect = selectAngle;
            return null;
        }

        // After the spreading, with the array about to be returned.
        public static void Store(float[] result)
        {
            if (pending == null || !MainThread.Current) return;
            lastInput = pending;
            lastSelect = pendingSelect;
            lastResult = (float[])result.Clone();
            pending = null;
        }

        static bool Same(float[] a, float[] b)
        {
            if (a.Length != b.Length) return false;
            for (int i = 0; i < a.Length; i++)
                if (!a[i].Equals(b[i])) return false;
            return true;
        }
    }

    // CharacterComponent.FindDirectionalAnimationName (see CacheAnimationDirections in
    // rewriter/Gameplay.cs) first works out which directions an animation has: a pass over
    // all of the character's clips (Mick has hundreds) comparing names with the base name and
    // its direction postfixes. While the right stick selects something, SystemInput turns
    // the player to it every frame (Character.FaceBG, a new CoroutineFace each frame), which
    // makes two such passes: about 4.7 ms a frame on the PS Vita. The clip list is the
    // component's serialized list (m_animations), which the game never assigns or changes, and
    // clip names change only through name assignments (Names.Version), so the directions found
    // for a component and a base name are kept. Components are kept by instance id, not by
    // reference: a static reference to the list would keep its clips (and their sprites and
    // atlas pages) loaded after the component's room is gone (Resources.UnloadUnusedAssets
    // counts it as a use). The entries are dropped when a scene unloads.
    public static class AnimDirections
    {
        static readonly Dictionary<int, Dictionary<string, int>> found = new Dictionary<int, Dictionary<string, int>>();
        // Components kept before all are dropped: only a bound for a scene that runs long
        // (entries are dropped when a scene unloads).
        const int MaxComponents = 256;
        static int version;

        // When a scene unloads (SceneUnloads).
        internal static void Unloaded()
        {
            found.Clear();
        }

        // The directions found before for this component and name, or -1.
        public static int Find(Object owner, string name)
        {
            if (ReferenceEquals(owner, null) || name == null || !MainThread.Current) return -1;
            if (version != Names.Version)
            {
                found.Clear();
                version = Names.Version;
            }
            Dictionary<string, int> byName;
            int mask;
            if (found.TryGetValue(owner.GetInstanceID(), out byName) && byName.TryGetValue(name, out mask)) return mask;
            return -1;
        }

        public static void Store(Object owner, string name, int mask)
        {
            if (ReferenceEquals(owner, null) || name == null || !MainThread.Current || version != Names.Version) return;
            int id = owner.GetInstanceID();
            Dictionary<string, int> byName;
            if (!found.TryGetValue(id, out byName))
            {
                if (found.Count >= MaxComponents) found.Clear();
                found[id] = byName = new Dictionary<string, int>();
            }
            byName[name] = mask;
        }
    }

    // InControl reads a Unity joystick's buttons by name every frame (UnityInputDevice
    // .ReadRawButtonState: Input.GetKey(buttonQueries[JoystickId - 1, index]), from a static
    // table of names "joystick N button M"); the engine looks each name up in its key-name table
    // (9.7 us a call on the PS Vita, 0.5 us by KeyCode). Unity names joysticks 1-8's buttons
    // 0-19 "joystick N button M", which are KeyCode.JoystickNButtonM = 350 + 20 (N - 1) + M (the
    // probe's -diag-check-keys compares both reads for all 160 on the player). GetJoystickButton
    // gets the name, the device's joystick and the button: a name that is exactly that
    // joystick's and button's (checked once per string object, which is the table's) is read
    // through its KeyCode; anything else (joysticks 9-10, which the engine has no name for and
    // throws on, or another string) through the name as before. Input is read on the main
    // thread; a verified name (a reference written once) is safe on any.
    public static class InputKeys
    {
        // Unity's named joystick buttons: joysticks 1-8, buttons 0-19 each.
        const int Joysticks = 8, Buttons = 20;
        static readonly string[] verified = new string[Joysticks * Buttons];

        public static bool GetJoystickButton(string name, int joystick, int button)
        {
            if (joystick >= 1 && joystick <= Joysticks && button >= 0 && button < Buttons && name != null)
            {
                int slot = Buttons * (joystick - 1) + button;
                if (ReferenceEquals(verified[slot], name) ||
                    (string.Equals(name, "joystick " + joystick.ToString(System.Globalization.CultureInfo.InvariantCulture) + " button " +
                                         button.ToString(System.Globalization.CultureInfo.InvariantCulture), StringComparison.Ordinal) &&
                     (verified[slot] = name) != null))
                    return Input.GetKey((KeyCode)((int)KeyCode.Joystick1Button0 + slot));
            }
            return Input.GetKey(name);
        }

        // UnityInputDevice.ReadRawAnalogValue: Input.GetAxisRaw("joystick 1 analog N"), 9.8 us a
        // call on the PS Vita, made twice a frame for each stick axis (once by the mapping for
        // each direction). The engine samples input once a frame, before scripts, and nothing in
        // the game resets it (Input.ResetInputAxes is never called), so within a frame the same
        // name gives the same value: it is read once per frame and name (a name that throws
        // throws every time, as before). Main thread only. Up to MaxAxes names a frame are kept
        // (well above the axes of a device's sticks); any more are read every time.
        const int MaxAxes = 32;
        static int axisFrame = -1, axisCount;
        static readonly string[] axisNames = new string[MaxAxes];
        static readonly float[] axisValues = new float[MaxAxes];

        public static float GetAxisRaw(string name)
        {
            if (name == null || !MainThread.Current) return Input.GetAxisRaw(name);
            int frame = Time.frameCount;
            if (frame != axisFrame)
            {
                axisFrame = frame;
                axisCount = 0;
            }
            for (int i = 0; i < axisCount; i++)
                if (ReferenceEquals(axisNames[i], name)) return axisValues[i];
            float value = Input.GetAxisRaw(name);
            if (Controls.HasAxisPresses) value = Controls.VirtualAxis(name, value);
            if (axisCount < axisNames.Length)
            {
                axisNames[axisCount] = name;
                axisValues[axisCount] = value;
                axisCount++;
            }
            return value;
        }
    }

    // Component.GetComponent<T>() and GameObject.GetComponent<T>() in the game's code. On the PS
    // Vita each call takes about 12 us (0.4 us for a plain engine property), and the game's per-frame scripts make dozens (GuiControllerInv, SystemGame,
    // PropComponent and others ask for the same component every frame). GetComponent<T> returns
    // the first component of the GameObject that is a T, in component order. Components are
    // only ever appended to that order and never move to another GameObject, so as long as the
    // component it returned is alive (not destroyed), it is still the first T and the answer.
    // Get remembers the answer per receiver (by instance id) and T, returns it while it and the
    // receiver are alive, and otherwise asks the engine (a dead receiver throws there as it
    // did). Null answers are not remembered (a T can be added later). Main thread only; the
    // entries are dropped when a scene unloads (a static reference to a destroyed component
    // would keep the assets its fields reference loaded) and when a cache passes 4096 entries.
    public static class Components
    {
        static readonly CacheClears clears = new CacheClears();
        // Entries a T's cache holds before it is emptied: only a bound for a scene that runs long.
        const int MaxEntries = 4096;

        public static void Clear()
        {
            clears.Run();
        }

        static class Of<T>
        {
            public static readonly Dictionary<int, T> Found = clears.Add(new Dictionary<int, T>(), () => Found.Clear());
        }

        public static T Get<T>(Component owner)
        {
            if (owner == null || !MainThread.Current) return owner.GetComponent<T>();
            return Find<T>(owner, owner.GetInstanceID(), null);
        }

        public static T GetFrom<T>(GameObject owner)
        {
            if (owner == null || !MainThread.Current) return owner.GetComponent<T>();
            return Find<T>(null, owner.GetInstanceID(), owner);
        }

        static T Find<T>(Component component, int id, GameObject gameObject)
        {
            Dictionary<int, T> found = Of<T>.Found;
            T cached;
            if (found.TryGetValue(id, out cached) && (Object)(object)cached != null) return cached;
            T answer = component != null ? component.GetComponent<T>() : gameObject.GetComponent<T>();
            if ((Object)(object)answer != null)
            {
                if (found.Count >= MaxEntries) found.Clear();
                found[id] = answer;
            }
            else found.Remove(id);
            return answer;
        }
    }

    // CharacterComponent.Update rebuilds a character's sprite-shaped hotspot
    // (UseSpriteAsHotspot, a trigger PolygonCollider2D) whenever its sprite changes: pathCount =
    // the sprite's physics shape count, SetPath for each path, then offset. Each assignment
    // makes the engine rebuild the collider's shapes (on the PS Vita pathCount= took about
    // 1.3 ms and SetPath 0.8 ms, at every animation frame of a character using it). The paths
    // are all set right after, so a pathCount assignment of the count the collider already has
    // changes nothing that is left once they are; and an offset assignment of the exact value
    // (same bits) it has changes nothing. Those two are skipped; the game uses the collider only
    // for point queries (no trigger callbacks).
    public static class Colliders
    {
        public static void SetPathCount(PolygonCollider2D collider, int count)
        {
            ColliderPoints.Forget(collider);
            if (collider == null || collider.pathCount != count) collider.pathCount = count;
        }

        public static void SetOffset(Collider2D collider, Vector2 offset)
        {
            if (collider != null)
            {
                Vector2 current = collider.offset;
                if (FloatBits.Of(current.x) == FloatBits.Of(offset.x) && FloatBits.Of(current.y) == FloatBits.Of(offset.y)) return;
            }
            collider.offset = offset;
        }
    }

    // PropComponent.UpdateBaseline sets the sorting order of the prop's sprites and of its first
    // particle system's renderer each time the prop moves (Prop.Position: OnSetPosition, and once
    // more when y changed), though the order (from y, rounded to tenths) rarely changes. An
    // assignment of the order a renderer already has changes nothing (the renderer keeps its
    // place in the engine's renderer list either way) and is skipped. In Drowning the room
    // script moves six props every frame, one with particle systems under it (1 ms a frame in
    // its set_Position on the PS Vita).
    public static class Renderers
    {
        public static void SetSortingOrder(Renderer renderer, int order)
        {
            if (renderer == null || renderer.sortingOrder != order) renderer.sortingOrder = order;
        }
    }

    // QuestMenuManager.Update puts every GUI's object at its sorted place among its siblings
    // each frame (Transform.SetSiblingIndex, 34 calls; the order changes only when a GUI's
    // baseline does). Moving a transform to the index it already has leaves the hierarchy as it
    // is; that assignment is skipped. Any other index (including one the engine clamps) is set.
    public static class Hierarchy
    {
        public static void SetSiblingIndex(Transform transform, int index)
        {
            if (transform == null || transform.GetSiblingIndex() != index) transform.SetSiblingIndex(index);
        }
    }
}
