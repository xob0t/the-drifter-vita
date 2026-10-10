using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using Object = UnityEngine.Object;

// Unloaded rooms: clearing destroyed scripts' asset references after a scene unloads.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // PowerQuest's data objects (Room, Prop, Hotspot, Region, Character) keep their scene
    // component (m_instance) after the room's scene unloads. The destroyed component's managed
    // fields still hold the room's sprites and clips (RoomComponent.m_sprites and m_animations,
    // each prop's SpriteAnim clips), so Resources.UnloadUnusedAssets keeps every visited room's
    // atlas pages loaded: on the PS Vita a chain of five rooms left 1-9 MB more each (free video
    // memory 50 to 25 MB). The original game keeps every room's sprites loaded through its room
    // templates, which the translation clears (asset_rules.py template_overlay). After a scene
    // unloads, every destroyed script reachable from the PowerQuest object's data gets its asset
    // references (sprites, clips, textures, materials, audio clips, controllers; in fields, lists,
    // arrays and the plain objects they hold) dropped: fields and array items set to null, lists
    // of assets emptied. The data objects keep their (destroyed) components, so Instance still
    // compares equal to null as before; only reads of asset fields of an unloaded room's
    // components see null (or an empty list) instead of the old value. Off with
    // -compat-keep-unloaded-refs.
    public static class UnloadedRoomRefs
    {
        static Type questType;
        static readonly Dictionary<Type, System.Reflection.FieldInfo[]> fields = new Dictionary<Type, System.Reflection.FieldInfo[]>();

        static bool IsGameType(Type type)
        {
            string name = type.Assembly.GetName().Name;
            return name.StartsWith("Recovered.") || name.StartsWith("Assembly-CSharp");
        }

        // An asset type, or a pair holding one (SpriteAnim's override list of clip pairs).
        static bool HoldsAsset(Type type)
        {
            if (type.IsGenericType && type.GetGenericTypeDefinition() == typeof(KeyValuePair<,>))
                return type.GetGenericArguments().Any(IsAsset);
            return IsAsset(type);
        }

        static bool IsAsset(Type type)
        {
            return typeof(Sprite).IsAssignableFrom(type) || typeof(AnimationClip).IsAssignableFrom(type) || typeof(Texture).IsAssignableFrom(type) ||
                   typeof(Material).IsAssignableFrom(type) || typeof(AudioClip).IsAssignableFrom(type) || typeof(RuntimeAnimatorController).IsAssignableFrom(type);
        }

        static System.Reflection.FieldInfo[] FieldsOf(Type type)
        {
            System.Reflection.FieldInfo[] result;
            if (fields.TryGetValue(type, out result)) return result;
            var list = new List<System.Reflection.FieldInfo>();
            for (Type t = type; t != null && t != typeof(object) && t != typeof(MonoBehaviour); t = t.BaseType)
                foreach (System.Reflection.FieldInfo field in t.GetFields(System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public |
                                                                          System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.DeclaredOnly))
                    if (MayMatter(field.FieldType))
                        list.Add(field);
            return fields[type] = list.ToArray();
        }

        // Whether a field of this declared type can lead the walk anywhere: to an asset it would
        // clear or to a destroyed game script. Fields of other types are not read (on the PS Vita
        // a walk that read every field took about 3 s of every room change, reading and boxing
        // every Vector2, Color and the like of 16,000 objects). The walk never enters value types,
        // engine objects other than game scripts, nor collections other than arrays and List<T>, so leaving
        // those out changes nothing it clears. Game classes that are not sealed may hold a
        // subclass, so they count; object and interface fields count.
        static readonly Dictionary<Type, bool> matters = new Dictionary<Type, bool>();

        static bool MayMatter(Type type)
        {
            bool result;
            if (matters.TryGetValue(type, out result)) return result;
            matters[type] = true; // while a sealed type's own fields are looked at
            Type element;
            if (type.IsPrimitive || type.IsEnum || type == typeof(string) || type.IsPointer) result = false;
            else if (HoldsAsset(type)) result = true;
            else if ((element = ElementOf(type)) != null) result = ElementMatters(element);
            else if (type.IsValueType) result = false;
            else if (typeof(Object).IsAssignableFrom(type)) result = typeof(MonoBehaviour).IsAssignableFrom(type) || type.IsAssignableFrom(typeof(MonoBehaviour));
            else if (type == typeof(object) || type.IsInterface) result = true;
            else if (!IsGameType(type)) result = false;
            else
            {
                // The class or any game class derived from it has a field that matters.
                result = FieldsOf(type).Length > 0;
                List<Type> derived;
                if (!result && !type.IsSealed && Subclasses().TryGetValue(type, out derived))
                    foreach (Type sub in derived)
                        if (FieldsOf(sub).Length > 0) { result = true; break; }
            }
            return matters[type] = result;
        }

        // Every game class's game base classes -> the classes derived from them (made once).
        static Dictionary<Type, List<Type>> subclasses;

        static Dictionary<Type, List<Type>> Subclasses()
        {
            if (subclasses != null) return subclasses;
            subclasses = new Dictionary<Type, List<Type>>();
            foreach (System.Reflection.Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                string name = assembly.GetName().Name;
                if (!name.StartsWith("Recovered.") && !name.StartsWith("Assembly-CSharp")) continue;
                Type[] types;
                try { types = assembly.GetTypes(); }
                catch (System.Reflection.ReflectionTypeLoadException e) { types = e.Types.Where(t => t != null).ToArray(); }
                foreach (Type type in types)
                    for (Type b = type.BaseType; b != null && IsGameType(b); b = b.BaseType)
                    {
                        List<Type> list;
                        if (!subclasses.TryGetValue(b, out list)) subclasses[b] = list = new List<Type>();
                        list.Add(type);
                    }
            }
            return subclasses;
        }

        // Outside destroyed scripts the walk only looks for them: fields that can hold nothing
        // but assets are not read there.
        static readonly Dictionary<Type, System.Reflection.FieldInfo[]> searchFields = new Dictionary<Type, System.Reflection.FieldInfo[]>();

        static System.Reflection.FieldInfo[] SearchFieldsOf(Type type)
        {
            System.Reflection.FieldInfo[] result;
            if (searchFields.TryGetValue(type, out result)) return result;
            return searchFields[type] = FieldsOf(type).Where(f => !AssetsOnly(f.FieldType)).ToArray();
        }

        static bool AssetsOnly(Type type)
        {
            return HoldsAsset(ElementOf(type) ?? type); // assets (and pairs of them) are never scripts
        }

        // The element type of an array or List<T>, else null.
        static Type ElementOf(Type type)
        {
            if (type.IsArray) return type.GetElementType();
            return type.IsGenericType && type.GetGenericTypeDefinition() == typeof(List<>) ? type.GetGenericArguments()[0] : null;
        }

        static bool ElementMatters(Type element)
        {
            return HoldsAsset(element) || (!element.IsValueType && MayMatter(element));
        }

        // When a scene unloads (SceneUnloads).
        internal static void Release()
        {
            if (!MainThread.Current || Platform.HasArg("-compat-keep-unloaded-refs")) return;
            if (questType == null)
                foreach (System.Reflection.Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
                    if ((questType = assembly.GetType("PowerTools.Quest.PowerQuest")) != null) break;
            if (questType == null) return;
            // Every PowerQuest object: while a room loads, its scene's own copy (which destroys
            // itself) can be the one FindObjectOfType returns.
            float start = Time.realtimeSinceStartup;
            var visited = new HashSet<object>(ReferenceComparer<object>.Instance);
            int cleared = 0;
            seen = new HashSet<object>(ReferenceComparer<object>.Instance);
            skipped = 0;
            fieldReads = slowReads = 0;
            if (!FastFields.Ready) { } // calibrates the offsets before the first slots are made
            foreach (Object quest in Resources.FindObjectsOfTypeAll(questType))
                if (quest != null) Walk(quest, false, visited, 0, ref cleared);
            done = seen;
            seen = null;
            Debug.Log("UnloadedRoomRefs: released " + cleared + " asset references of unloaded rooms; " + done.Count + " destroyed scripts, " +
                      skipped + " already walked, " + visited.Count + " objects in " + ((Time.realtimeSinceStartup - start) * 1000f).ToString("0.0") + " ms (" +
                      fieldReads + " field reads, " + slowReads + " through FieldInfo)");
        }

        // Destroyed scripts walked at an earlier unload. Their asset fields are already null, and
        // nothing assigns fields of a destroyed component, so they are passed over (each walk took
        // reflection reads of every field of every destroyed room, prop and hotspot component of
        // every room visited in the session). Only the scripts the last walk reached are kept, so
        // the set holds no script the game has let go of.
        static HashSet<object> done = new HashSet<object>(ReferenceComparer<object>.Instance), seen;
        static int skipped;

        // Walks the game's plain objects and collections from value; inside destroyed scripts (and
        // the plain objects they hold) asset references are dropped. Per-type decisions are
        // made once (a Plan per type, for 4,000-5,200 objects per room change) and fields are read
        // and cleared at their offsets (FastFields; each FieldInfo read cost tens of microseconds
        // on the PS Vita, where a walk through reflection took 0.8-1.5 s of every room change).
        // Arrays and lists of value types, and value-type fields, are passed over, and arrays and
        // lists of assets outside destroyed scripts are not entered (assets are never walked).
        sealed class Plan
        {
            public bool skip;               // not walked (engine values, non-game classes, value types)
            public bool elements;           // an array or List<T>
            public bool list;               // a List<T>
            public bool multi;              // an array of more than one dimension
            public bool skipElements;       // elements of primitive, enum or string type
            public bool clearElements;      // elements that are assets (cleared inside destroyed scripts)
            public bool walkElements;       // reference-type elements that may lead somewhere
            public bool unityObject, gameScript;
            public FastFields.Slot[] all, search;
            public bool[] clears;           // per slot of all: an asset field set to null in destroyed scripts
        }

        static readonly Dictionary<Type, Plan> plans = new Dictionary<Type, Plan>();
        static int fieldReads, slowReads;

        static Plan PlanOf(Type type)
        {
            Plan plan;
            if (plans.TryGetValue(type, out plan)) return plan;
            plan = new Plan();
            Type element = ElementOf(type);
            if (element != null)
            {
                plan.elements = true;
                plan.list = !type.IsArray;
                plan.multi = type.IsArray && type.GetArrayRank() != 1;
                plan.skipElements = element.IsPrimitive || element.IsEnum || element == typeof(string);
                plan.clearElements = HoldsAsset(element);
                plan.walkElements = !element.IsValueType && !plan.clearElements;
            }
            else
            {
                plan.unityObject = typeof(Object).IsAssignableFrom(type);
                plan.gameScript = typeof(MonoBehaviour).IsAssignableFrom(type) && IsGameType(type);
                plan.skip = !plan.unityObject && (!IsGameType(type) || type.IsValueType);
                if (!plan.skip)
                {
                    plan.all = Slots(FieldsOf(type));
                    plan.search = Slots(SearchFieldsOf(type));
                    plan.clears = new bool[plan.all.Length];
                    for (int i = 0; i < plan.all.Length; i++)
                        plan.clears[i] = IsAsset(plan.all[i].field.FieldType) && !plan.all[i].field.IsInitOnly;
                }
            }
            return plans[type] = plan;
        }

        // Reference-type fields only: a value-type field (a pair holding assets) led Walk to a
        // boxed copy, where it stopped.
        static FastFields.Slot[] Slots(System.Reflection.FieldInfo[] fields)
        {
            var slots = new List<FastFields.Slot>();
            foreach (System.Reflection.FieldInfo field in fields)
                if (!field.FieldType.IsValueType) slots.Add(FastFields.MakeSlot(field, field.FieldType));
            return slots.ToArray();
        }

        // Levels of fields and items the walk follows from a PowerQuest object: a bound against
        // long chains of plain objects (linked lists and the like).
        const int MaxDepth = 10;

        static void Walk(object value, bool inDestroyed, HashSet<object> visited, int depth, ref int cleared)
        {
            if (value == null || depth > MaxDepth || !visited.Add(value)) return;
            Plan plan = PlanOf(value.GetType());
            if (plan.skip) return;
            if (plan.elements)
            {
                if (!plan.skipElements) WalkElements(value, plan, inDestroyed, visited, depth, ref cleared);
                return;
            }
            if (plan.unityObject)
            {
                // Live engine objects and assets are left alone; destroyed scripts are entered.
                if (!plan.gameScript || (Object)value != null) { if (depth > 0) return; }
                else
                {
                    inDestroyed = true;
                    seen.Add(value);
                    if (done.Contains(value)) { skipped++; return; }
                }
            }
            FastFields.Slot[] slots = inDestroyed ? plan.all : plan.search;
            for (int i = 0; i < slots.Length; i++)
            {
                FastFields.Slot slot = slots[i];
                fieldReads++;
                if (!slot.fast || !slot.state.verified) slowReads++;
                object child;
                try { child = FastFields.Get(slot, value); } catch (Exception) { continue; }
                if (child == null) continue;
                if (inDestroyed && plan.clears[i]) { FastFields.Set(slot, value, null); cleared++; continue; }
                Walk(child, inDestroyed, visited, depth + 1, ref cleared);
            }
        }

        // The items of an array or List<T>: assets inside a destroyed script are cleared (a list
        // emptied, an array's items set to null); other reference-type items are walked.
        static void WalkElements(object value, Plan plan, bool inDestroyed, HashSet<object> visited, int depth, ref int cleared)
        {
            if (plan.list)
            {
                var list = (System.Collections.IList)value;
                if (inDestroyed && plan.clearElements) { cleared += list.Count; list.Clear(); return; }
                if (!plan.walkElements) return;
                for (int i = 0; i < list.Count; i++) Walk(list[i], inDestroyed, visited, depth + 1, ref cleared);
                return;
            }
            var array = (Array)value;
            if (inDestroyed && plan.clearElements) { cleared += array.Length; Array.Clear(array, 0, array.Length); return; }
            if (!plan.walkElements) return;
            if (plan.multi) { foreach (object item in array) Walk(item, inDestroyed, visited, depth + 1, ref cleared); return; }
            var items = (object[])value; // arrays of reference types are object[] (covariance)
            for (int i = 0; i < items.Length; i++) Walk(items[i], inDestroyed, visited, depth + 1, ref cleared);
        }
    }
}
