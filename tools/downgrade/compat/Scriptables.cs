using System.Collections.Generic;

// Script costs: PowerQuest's scriptable lookups by name.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // PowerQuest.GetGui, GetInventory and GetCharacter (behind G.Options, I.Jerrycan and the
    // like, about 70 calls a frame) go to QuestUtils.FindScriptableFast(list, name): two
    // dictionary lookups (the list, then the name: about 3 us on the PS Vita). The first call for
    // a list and a name keeps the game's own answer, and later calls return it until
    // QuestUtils.ClearScriptableCache empties all of them (PowerQuest.Awake and
    // InitialiseQuestObjects), so for a list and a name the answer stays the same until then.
    // The downgrader makes FindScriptableFast ask TryGet first and Store its answer, and
    // ClearScriptableCache call Clear. Kept by name and list object (both compared by
    // reference; the names are the game's constants and fields), main thread only.
    public static class Scriptables
    {
        sealed class Entry
        {
            public object List, Result;
            public Entry Next;
        }

        static readonly Dictionary<string, Entry> found = new Dictionary<string, Entry>(ReferenceComparer<string>.Instance);
        // Names kept before all are dropped: only a bound (ClearScriptableCache empties them too).
        const int MaxNames = 4096;

        public static bool TryGet<T>(List<T> list, string name, out T result)
        {
            result = default(T);
            if (list == null || name == null || !MainThread.Current) return false;
            Entry entry;
            if (!found.TryGetValue(name, out entry)) return false;
            for (; entry != null; entry = entry.Next)
                if (ReferenceEquals(entry.List, list))
                {
                    result = (T)entry.Result;
                    return true;
                }
            return false;
        }

        public static T Store<T>(T result, List<T> list, string name)
        {
            if (list == null || name == null || !MainThread.Current) return result;
            if (found.Count >= MaxNames) found.Clear();
            Entry next;
            found.TryGetValue(name, out next);
            found[name] = new Entry { List = list, Result = result, Next = next };
            return result;
        }

        public static void Clear()
        {
            found.Clear();
        }
    }
}
