using System;
using System.Collections.Generic;
using UnityEngine;
using Object = UnityEngine.Object;

// PowerQuest data at a save restore and in saves: the shared PowerQuest copy, reflection and type caches, SaveFields
// (StoredSaves.cs and FastFields.cs go on with the saves).
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // Room scenes keep their PowerQuest copy without asset references (asset_rules.py
    // powerquest_overlay); the title room's copy has an inactive duplicate as its child,
    // PowerQuestTemplate. Called first in PowerQuest.Awake. The full copy keeps the duplicate
    // across scenes (one is enough; later title loads drop theirs). A copy without rooms
    // that finds no other PowerQuest running (the chapter select restarts the game into a
    // room scene) instantiates the duplicate, which becomes the game's PowerQuest; the copy
    // then removes itself as it does when one is running. In the source version that room's
    // own copy would have run, so before the duplicate wakes it gets the room copy's values
    // of the fields written on one line (numbers, enums, strings, vectors, colours): room
    // copies differ from the title room's only there (asset_rules.py checks it); SceneRoomCrypt's
    // has its own m_dialogTextCharacterInRange.
    public static class SharedPowerQuest
    {
        const string TemplateName = "PowerQuestTemplate";
        static GameObject template;

        public static void Awake(MonoBehaviour quest, System.Collections.IList roomPrefabs)
        {
            if (roomPrefabs == null) return;
            if (roomPrefabs.Count > 0)
            {
                Transform child = quest.transform.Find(TemplateName);
                if (child == null) return;
                if (template != null) { Object.Destroy(child.gameObject); return; }
                child.SetParent(null, false);
                Object.DontDestroyOnLoad(child.gameObject);
                template = child.gameObject;
                return;
            }
            foreach (Object other in Object.FindObjectsOfType(quest.GetType()))
                if (other != quest) return;
            if (template == null) { Debug.LogError("PowerQuest: no running copy and no " + TemplateName); return; }
            GameObject copy = Object.Instantiate(template);
            copy.name = "PowerQuest";
            Component running = copy.GetComponent(quest.GetType());
            if (running != null) CopySceneValues(quest, running);
            copy.SetActive(true);
        }

        static readonly Type[] PlainStructs =
        {
            typeof(Vector2), typeof(Vector3), typeof(Vector4), typeof(Vector2Int), typeof(Vector3Int), typeof(Quaternion),
            typeof(Color), typeof(Color32),
        };

        // The serialized fields of the given types (Unity's rules: public or [SerializeField],
        // not [NonSerialized], static, const or readonly), from the PowerQuest class up to
        // MonoBehaviour.
        static void CopySceneValues(Component from, Component to)
        {
            for (Type type = from.GetType(); type != null && type != typeof(MonoBehaviour); type = type.BaseType)
                foreach (System.Reflection.FieldInfo field in type.GetFields(System.Reflection.BindingFlags.DeclaredOnly |
                    System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic))
                {
                    Type value = field.FieldType;
                    if (field.IsNotSerialized || field.IsInitOnly || field.IsLiteral ||
                        !(field.IsPublic || field.IsDefined(typeof(SerializeField), false)) ||
                        !(value.IsPrimitive || value.IsEnum || value == typeof(string) || Array.IndexOf(PlainStructs, value) >= 0))
                        continue;
                    object own = field.GetValue(from);
                    if (Equals(own, field.GetValue(to))) continue;
                    field.SetValue(to, own);
                    Debug.Log("PowerQuest: " + field.Name + " = " + own + " (this room's copy)");
                }
        }
    }

    // PowerQuest saves, restores and copies its data objects field by field through
    // reflection: for every object it asks Type.GetFields and Attribute.IsDefined for the
    // type and each field again (QuestSaveSurrogateSelector, QuestSaveManager, QuestUtils).
    // The answers never change, so the downgrader sends those calls here, where they are
    // asked once per type or member. Callers only read the arrays.
    public static class ReflectionCache
    {
        // Keyed by reference types only: IL2CPP compares struct keys such as KeyValuePair
        // through ValueType.Equals, by reflection.
        static readonly Dictionary<int, Dictionary<Type, System.Reflection.FieldInfo[]>> fields =
            new Dictionary<int, Dictionary<Type, System.Reflection.FieldInfo[]>>();
        static readonly Dictionary<Type, Dictionary<System.Reflection.MemberInfo, bool>> defined =
            new Dictionary<Type, Dictionary<System.Reflection.MemberInfo, bool>>();

        public static System.Reflection.FieldInfo[] GetFields(Type type, System.Reflection.BindingFlags flags)
        {
            if (!MainThread.Current) return type.GetFields(flags);
            Dictionary<Type, System.Reflection.FieldInfo[]> byType;
            if (!fields.TryGetValue((int)flags, out byType)) fields[(int)flags] = byType = new Dictionary<Type, System.Reflection.FieldInfo[]>();
            System.Reflection.FieldInfo[] result;
            if (!byType.TryGetValue(type, out result)) byType[type] = result = type.GetFields(flags);
            return result;
        }

        public static bool IsDefined(System.Reflection.MemberInfo member, Type attribute)
        {
            if (!MainThread.Current) return Attribute.IsDefined(member, attribute);
            Dictionary<System.Reflection.MemberInfo, bool> byMember;
            if (!defined.TryGetValue(attribute, out byMember)) defined[attribute] = byMember = new Dictionary<System.Reflection.MemberInfo, bool>();
            bool result;
            if (!byMember.TryGetValue(member, out result)) byMember[member] = result = Attribute.IsDefined(member, attribute);
            return result;
        }
    }

    // Answers of game methods that depend only on a Type (PowerQuest's save surrogate asks
    // IsIgnoredType and IsKnownType for every saved object and field); the downgrader's
    // MemoizeTypePredicates routes them here, one slot per method.
    public static class TypeMemo
    {
        static readonly List<Dictionary<Type, bool>> slots = new List<Dictionary<Type, bool>>();

        public static bool TryGet(int slot, Type type, out bool value)
        {
            value = false;
            if (type == null || !MainThread.Current) return false;
            while (slots.Count <= slot) slots.Add(new Dictionary<Type, bool>());
            return slots[slot].TryGetValue(type, out value);
        }

        public static bool Store(int slot, Type type, bool value)
        {
            if (type != null && MainThread.Current)
            {
                while (slots.Count <= slot) slots.Add(new Dictionary<Type, bool>());
                slots[slot][type] = value;
            }
            return value;
        }
    }

    // PowerQuest's save surrogate restores an object field by field, each in its own try
    // block: info.GetValue throws for a field the save lacks, and the catch skips it. On
    // IL2CPP every throw captures a stack trace. In those blocks the downgrader sends
    // GetValue here, which answers Missing without throwing, and FieldInfo.SetValue, which
    // skips Missing the way the catch did.
    public static class SaveFields
    {
        public static readonly object Missing = new object();
        // The names of the last info asked about (one object, the restore's current one: it is
        // kept until the next restore asks about another).
        static System.Runtime.Serialization.SerializationInfo namesOf;
        static int namesCount = -1;
        static readonly HashSet<string> names = new HashSet<string>();

        public static object GetValue(System.Runtime.Serialization.SerializationInfo info, string name, Type type)
        {
            if (!MainThread.Current) return info.GetValue(name, type);
            if (info != namesOf || info.MemberCount != namesCount)
            {
                names.Clear();
                foreach (System.Runtime.Serialization.SerializationEntry entry in info) names.Add(entry.Name);
                namesOf = info;
                namesCount = info.MemberCount;
            }
            return names.Contains(name) ? info.GetValue(name, type) : Missing;
        }

        public static void SetValue(System.Reflection.FieldInfo field, object target, object value)
        {
            if (value != Missing) field.SetValue(target, value);
        }
    }
}
