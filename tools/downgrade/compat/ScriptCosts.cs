using System;
using System.Collections.Generic;
using UnityEngine;
using Object = UnityEngine.Object;

// Script costs: name, list, script-kind, enum, shader-id and animator-override answers the game asks for every frame.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // Object.name copies the native name into a new string on every call (on IL2CPP also
    // converting it from UTF-8). PowerQuest looks clips, sprites and audio cues up by name
    // through whole lists: SystemAudio.Play reads every cue's name for each footstep,
    // QuestUtils.FindByName every inventory clip's several times a frame. Names are remembered
    // by instance id (no reference is kept, so unloading is unaffected); every name assignment
    // in the game's code goes through Set, which forgets them all. Destroyed objects still get
    // the original call, which throws.
    public static class Names
    {
        static readonly Dictionary<int, string> names = new Dictionary<int, string>();

        public static string Get(Object value)
        {
            if (value == null || !MainThread.Current) return value.name;
            int id = value.GetInstanceID();
            string name;
            if (!names.TryGetValue(id, out name)) names[id] = name = value.name;
            return name;
        }

        // Incremented by every name assignment (for caches built from names, AnimDirections).
        public static int Version;

        public static void Set(Object value, string name)
        {
            names.Clear();
            Version++;
            value.name = name;
        }
    }

    // PowerQuest.FindSpriteInList(list, name) (RoomComponent.GetSprite, GetGuiSprite,
    // GetInventorySprite) returns the first sprite of the list that is alive and whose name is
    // name or name + SPRITE_NUM_POSTFIX_0, ignoring case (List.Find; names through Names.Get).
    // CharacterRimlight looks its rim light's mask sprite up in the room's sprite list several
    // times a second; in EscCorridor that cost about 3 ms a call on the PS Vita (1.8 ms a frame).
    // The downgrader (MemoizeSpriteLookups in rewriter/ScriptCosts.cs) makes it ask TryGet first
    // and give its answers to Store, which keep them as ListAnswers says. Main thread only;
    // cleared when a scene unloads (the references would keep the room's sprites loaded).
    public static class SpriteLists
    {
        static readonly ListAnswers answers = new ListAnswers();

        public static void Clear()
        {
            answers.Clear();
        }

        public static bool TryGet(List<Sprite> list, string name, string postfix, out Sprite result)
        {
            return answers.TryGet(list, name, postfix, out result);
        }

        public static Sprite Store(Sprite result, List<Sprite> list, string name, string postfix)
        {
            return answers.Store(result, list, name, postfix);
        }
    }

    // QuestUtils.FindByName<T>(list, name) returns the first item of the list that is alive and
    // whose name is name, ignoring case (names through Names.Get). GuiControllerInv plays each
    // inventory slot's icon again every frame, looking its clip up by name among PowerQuest's
    // 164 inventory animations (PowerQuest.GetInventoryAnimation, up to five a frame: 0.2-0.5 ms
    // a call on the PS Vita in EscReactor); characters and props look their clips up the same
    // way. The downgrader (MemoizeNameLookups in rewriter/ScriptCosts.cs) makes it ask TryGet
    // first and give its answers to Store, which keep them as ListAnswers says. Main thread only;
    // cleared when a scene unloads (the references would keep a room's clips loaded).
    public static class NamedLists
    {
        static readonly ListAnswers answers = new ListAnswers();

        public static void Clear()
        {
            answers.Clear();
        }

        public static bool TryGet<T>(List<T> list, string name, out T result) where T : Object
        {
            if (!answers.TryGet(list, name, null, out result)) return false;
            if (Check()) Compare(list, name, result);
            return true;
        }

        // -compat-check-named-lists: every answer given from here is compared with the search
        // (Checked, Mismatched; the first mismatch is logged).
        public static int Checked, Mismatched;
        static string[] checkFor;
        static bool check;

        static bool Check()
        {
            if (Platform.NewArgs(ref checkFor)) check = Platform.HasArg("-compat-check-named-lists");
            return check;
        }

        static void Compare<T>(List<T> list, string name, T given) where T : Object
        {
            T searched = null;
            foreach (T item in list)
                if (item != null && string.Equals(Names.Get(item), name, StringComparison.OrdinalIgnoreCase)) { searched = item; break; }
            Checked++;
            if (ReferenceEquals(searched, given)) return;
            if (Mismatched++ == 0) Debug.LogError("NamedLists mismatch for " + name + ": " + (given == null ? "null" : given.name) + " instead of " + (searched == null ? "null" : searched.name));
        }

        // CharacterComponent.FindDirectionalAnimation's clip search (see WalkRules.AnimLookup):
        // Lists.Find(list, name, match) where match is "item alive and named name, ignoring case"
        // (string.Equals(name, item == null ? null : item.name, OrdinalIgnoreCase)), which is
        // what the answers here are kept for.
        public static T FindNamed<T>(List<T> list, string name, Func<T, string, bool> match) where T : Object
        {
            return Find(list, name, match, WalkRules.AnimLookup);
        }

        // SystemAudio.Play(cueName, emitter)'s cue search (see WalkRules.CueLookup), match being
        // "item alive and named name, ignoring case".
        public static T FindCue<T>(List<T> list, string name, Func<T, string, bool> match) where T : Object
        {
            return Find(list, name, match, WalkRules.CueLookup);
        }

        static T Find<T>(List<T> list, string name, Func<T, string, bool> match, bool cached) where T : Object
        {
            if (list == null || name == null || !MainThread.Current || !cached) return Lists.Find(list, name, match);
            T result;
            if (TryGet(list, name, out result)) return result;
            return Store(Lists.Find(list, name, match), list, name);
        }

        public static T Store<T>(T result, List<T> list, string name) where T : Object
        {
            return answers.Store(result, list, name, null);
        }
    }

    // Answers of a search of a list by name (SpriteLists, NamedLists): its first item that is
    // alive and whose name matches. An answer is kept per list (by reference) and name with the
    // list's items up to it (all of them when there was none), and given again while the list
    // has the same count and the same items (by reference) up to there, no name has been
    // assigned since (Names.Version) and the answer is alive. The items before it did not match:
    // an item that did not match can only come to match through a name assignment (a destroyed
    // one never does), so the search returns the same answer. A postfix the search appends to
    // the name (SpriteLists) must be the same string object. Main thread only.
    sealed class ListAnswers
    {
        sealed class Entry
        {
            public Object[] Items;
            public int Count, Version, Index;
            public string Postfix;
        }

        // Entries kept before all are dropped: only a bound for a scene that runs long (the
        // owners clear them when a scene unloads).
        const int MaxEntries = 4096;
        readonly Dictionary<object, Dictionary<string, Entry>> found = new Dictionary<object, Dictionary<string, Entry>>();
        int entries;

        public void Clear()
        {
            found.Clear();
            entries = 0;
        }

        public bool TryGet<T>(List<T> list, string name, string postfix, out T result) where T : Object
        {
            result = null;
            if (list == null || name == null || !MainThread.Current) return false;
            Dictionary<string, Entry> byName;
            Entry entry;
            if (!found.TryGetValue(list, out byName) || !byName.TryGetValue(name, out entry)) return false;
            if (entry.Version != Names.Version || entry.Count != list.Count || !ReferenceEquals(entry.Postfix, postfix)) return false;
            Object[] items = entry.Items;
            for (int i = 0; i < items.Length; i++)
                if (!ReferenceEquals(list[i], items[i])) return false;
            if (entry.Index >= 0)
            {
                T answer = list[entry.Index];
                if (answer == null) return false;
                result = answer;
            }
            return true;
        }

        public T Store<T>(T result, List<T> list, string name, string postfix) where T : Object
        {
            if (list == null || name == null || !MainThread.Current) return result;
            int count = list.Count, index = -1;
            if (!ReferenceEquals(result, null))
            {
                for (int i = 0; i < count; i++)
                    if (ReferenceEquals(list[i], result)) { index = i; break; }
                if (index < 0) return result;
            }
            var items = new Object[index >= 0 ? index + 1 : count];
            for (int i = 0; i < items.Length; i++) items[i] = list[i];
            if (entries >= MaxEntries) Clear();
            Dictionary<string, Entry> byName;
            if (!found.TryGetValue(list, out byName)) found[list] = byName = new Dictionary<string, Entry>();
            if (!byName.ContainsKey(name)) entries++;
            byName[name] = new Entry { Items = items, Count = count, Version = Names.Version, Index = index, Postfix = postfix };
            return result;
        }
    }

    // PowerQuest.GetScript<T> (behind GlobalScript.Script, GuiScript<T>.Script and the like,
    // a dozen times a frame) works out from T alone which kind of script it is and the name to
    // look it up by: T derives from RoomScript<T>, CharacterScript<T>, DialogTreeScript<T>,
    // InventoryScript<T> or GuiScript<T> (in that order; the name is T's name without the
    // kind's prefix), or T's name is the global script's. The downgrader (RewriteGetScript in
    // rewriter/ScriptCosts.cs) makes GetScript ask here; the answer is worked out once per T,
    // the lookup by name stays. Main thread only.
    public static class ScriptKinds<T>
    {
        static int kind = -2;
        static string name;

        // The kind (0-4 in the order above, 5 the global script, -1 none), -2 before Compute.
        public static int Kind() { return kind; }

        public static string Name() { return name; }

        public static int Compute(Type room, int roomPrefix, Type character, int characterPrefix, Type dialogTree, int dialogTreePrefix,
                                  Type inventory, int inventoryPrefix, Type gui, int guiPrefix, string globalName)
        {
            Type type = typeof(T);
            string text = type.ToString();
            Type[] kinds = { room, character, dialogTree, inventory, gui };
            int[] prefixes = { roomPrefix, characterPrefix, dialogTreePrefix, inventoryPrefix, guiPrefix };
            int found = -1;
            string foundName = null;
            for (int i = 0; i < kinds.Length && found < 0; i++)
                if (Derives(type, kinds[i]))
                {
                    foundName = text.Substring(prefixes[i]);
                    found = i;
                }
            if (found < 0 && text == globalName)
            {
                foundName = globalName;
                found = 5;
            }
            name = foundName;
            kind = found;
            return found;
        }

        // type.IsSubclassOf(definition<type>): the generic class built from definition with type
        // as its argument is among type's base classes.
        static bool Derives(Type type, Type definition)
        {
            for (Type parent = type.BaseType; parent != null; parent = parent.BaseType)
                if (parent.IsGenericType && parent.GetGenericTypeDefinition() == definition)
                {
                    Type[] arguments = parent.GetGenericArguments();
                    if (arguments.Length == 1 && arguments[0] == type) return true;
                }
            return false;
        }
    }

    // Enum.ToString by reflection, for enums with an int underlying type: InputDeviceStyle in
    // SystemInput.UpdateInput and GuiPlatformContent.Update, every frame.
    public static class EnumText<T>
    {
        static readonly Dictionary<int, string> names = new Dictionary<int, string>();

        public static string Name(int value)
        {
            if (!MainThread.Current) return Enum.ToObject(typeof(T), value).ToString();
            string name;
            if (!names.TryGetValue(value, out name)) names[value] = name = Enum.ToObject(typeof(T), value).ToString();
            return name;
        }
    }

    // The string overloads of the material and property block setters look the name up in the
    // engine each call (on IL2CPP also converting it to UTF-8); the int overloads take the id
    // Shader.PropertyToID would return.
    public static class ShaderIds
    {
        static readonly Dictionary<string, int> ids = new Dictionary<string, int>();

        static int Id(string name)
        {
            int id;
            if (!MainThread.Current) return Shader.PropertyToID(name);
            if (!ids.TryGetValue(name, out id)) ids[name] = id = Shader.PropertyToID(name);
            return id;
        }

        public static void SetFloat(MaterialPropertyBlock block, string name, float value) { if (name == null) block.SetFloat(name, value); else block.SetFloat(Id(name), value); }
        public static void SetInt(MaterialPropertyBlock block, string name, int value) { if (name == null) block.SetInt(name, value); else block.SetInt(Id(name), value); }
        public static void SetColor(MaterialPropertyBlock block, string name, Color value) { if (name == null) block.SetColor(name, value); else block.SetColor(Id(name), value); }
        public static void SetVector(MaterialPropertyBlock block, string name, Vector4 value) { if (name == null) block.SetVector(name, value); else block.SetVector(Id(name), value); }
        public static void SetTexture(MaterialPropertyBlock block, string name, Texture value) { if (name == null) block.SetTexture(name, value); else block.SetTexture(Id(name), value); }
        public static void SetFloat(Material material, string name, float value) { if (name == null) material.SetFloat(name, value); else material.SetFloat(Id(name), value); }
        public static void SetInt(Material material, string name, int value) { if (name == null) material.SetInt(name, value); else material.SetInt(Id(name), value); }
        public static void SetColor(Material material, string name, Color value) { if (name == null) material.SetColor(name, value); else material.SetColor(Id(name), value); }
        public static void SetVector(Material material, string name, Vector4 value) { if (name == null) material.SetVector(name, value); else material.SetVector(Id(name), value); }
        public static void SetTexture(Material material, string name, Texture value) { if (name == null) material.SetTexture(name, value); else material.SetTexture(Id(name), value); }
        public static void SetTextureOffset(Material material, string name, Vector2 value) { if (name == null) material.SetTextureOffset(name, value); else material.SetTextureOffset(Id(name), value); }
        public static void SetTextureScale(Material material, string name, Vector2 value) { if (name == null) material.SetTextureScale(name, value); else material.SetTextureScale(Id(name), value); }
    }

    // PowerTools' SpriteAnim.Play: see PatchSpriteAnimPlay in rewriter/Gameplay.cs. Play calls
    // ApplyOverrides, then SetEnabled only if the Animator was disabled, then
    // UpdateUnlessUnchanged for its first Animator.Update(0), restarts the state at time 0 and
    // calls UpdateAfterRestart for the second. When the controller's overrides are the ones
    // last applied to it, ApplyOverrides (which makes the engine rebind the Animator) and the
    // first evaluation are skipped. For GuiControllerInv's icons (BeginIconReplay before its
    // Play calls, EndIconReplay after) the second is skipped too then: the restart still takes
    // effect at the frame's animation update, which evaluates the clip at the same time as
    // before; until then the icon keeps its pose, and nothing reads it in between (the icon
    // clips have no events).
    // The overrides last applied are kept as instance ids (the controller's, then each pair's
    // clips), not references: a static reference counts as a use for Resources.UnloadUnusedAssets,
    // and the controllers (made with new in SpriteAnim.Awake, never destroyed) would keep their
    // clips, sprites and atlas pages loaded after their room is gone. Instance ids are never
    // reused, so the same ids mean the same objects. The entries are dropped when a scene
    // unloads; the next Play of a controller then applies its overrides as the original does.
    // Main thread only.
    public static class AnimatorOverrides
    {
        static readonly Dictionary<int, int[]> applied = new Dictionary<int, int[]>();
        // Controllers kept before all are dropped: only a bound for a scene that runs long
        // (entries are dropped when a scene unloads).
        const int MaxControllers = 1024;
        static bool unchanged, iconReplay, iconReplayRequested;

        // Counts for the diagnostic probe's heartbeat: clip changes applied (each rebinds an
        // Animator) and replays of an unchanged clip.
        public static int Applied, Skipped;

        // When a scene unloads (SceneUnloads).
        internal static void Unloaded()
        {
            applied.Clear();
        }

        public static void BeginIconReplay() { iconReplayRequested = true; }

        public static void EndIconReplay() { iconReplayRequested = iconReplay = false; }

        public static void ApplyOverrides(AnimatorOverrideController controller, IList<KeyValuePair<AnimationClip, AnimationClip>> overrides)
        {
            unchanged = false;
            iconReplay = iconReplayRequested;
            iconReplayRequested = false;
            if (controller == null || overrides == null || !MainThread.Current)
            {
                controller.ApplyOverrides(overrides);
                return;
            }
            int id = controller.GetInstanceID();
            int[] last;
            if (applied.TryGetValue(id, out last) && Same(last, overrides))
            {
                unchanged = true;
                Skipped++;
                return;
            }
            controller.ApplyOverrides(overrides);
            Applied++;
            var ids = new int[overrides.Count * 2];
            for (int i = 0; i < overrides.Count; i++)
            {
                KeyValuePair<AnimationClip, AnimationClip> pair = overrides[i];
                ids[2 * i] = Id(pair.Key);
                ids[2 * i + 1] = Id(pair.Value);
            }
            if (applied.Count >= MaxControllers) applied.Clear();
            applied[id] = ids;
        }

        public static void SetEnabled(Behaviour behaviour, bool enabled)
        {
            unchanged = false;
            behaviour.enabled = enabled;
        }

        public static void UpdateUnlessUnchanged(Animator animator, float deltaTime)
        {
            if (!(unchanged && deltaTime == 0f && animator != null)) animator.Update(deltaTime);
        }

        public static void UpdateAfterRestart(Animator animator, float deltaTime)
        {
            bool skip = unchanged && iconReplay && deltaTime == 0f && animator != null;
            unchanged = iconReplay = false;
            if (!skip) animator.Update(deltaTime);
        }

        // 0 for a null reference (not for a destroyed object, which keeps its id).
        static int Id(Object value)
        {
            return ReferenceEquals(value, null) ? 0 : value.GetInstanceID();
        }

        static bool Same(int[] last, IList<KeyValuePair<AnimationClip, AnimationClip>> overrides)
        {
            if (last.Length != overrides.Count * 2) return false;
            for (int i = 0; i < overrides.Count; i++)
            {
                KeyValuePair<AnimationClip, AnimationClip> pair = overrides[i];
                if (last[2 * i] != Id(pair.Key) || last[2 * i + 1] != Id(pair.Value)) return false;
            }
            return true;
        }
    }
}
