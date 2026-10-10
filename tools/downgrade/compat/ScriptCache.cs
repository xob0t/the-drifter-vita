using System;
using System.Collections.Generic;
using UnityEngine;

// Script costs: PowerQuest's reflected script calls and list helpers.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // Answers the game's scripts ask again every frame although they cannot change. On the PS
    // Vita (IL2CPP, scripts on one 444 MHz core) reflection, enum names and string-keyed
    // engine calls cost far more than on the PC. The downgrader (rewriter/ScriptCosts.cs
    // CacheCalls, by the CachedCalls table in rewriter/Patches.cs) sends the calls here; each
    // helper returns what the call it replaces returns, and leaves null arguments to the
    // original call so its exceptions stay.
    public static class ScriptCache
    {
        // Type.GetMethod: PowerQuest finds its scripts' Update, UpdateNoPause and interaction
        // functions by name every frame (and Invokes them). A type's methods never change.
        // Kept per type (by reference) as a short list of (name, flags, method); the names are
        // nearly always the same string objects (the game's constants), compared by reference
        // first and by value otherwise. (Nested dictionaries keyed by flags, type and name took
        // 5.1 us a lookup on the PS Vita.)
        sealed class Found
        {
            public string Name;
            public int Flags;
            public System.Reflection.MethodInfo Method;
        }

        static readonly Dictionary<Type, List<Found>> methods = new Dictionary<Type, List<Found>>(ReferenceComparer<Type>.Instance);
        // Entries a type's list holds before it starts again (see Method).
        const int FoundPerType = 64;

        // The caches here are used only on the main thread (MainThread.Current), without locks:
        // on IL2CPP every Monitor.Exit signals a kernel event. Other threads get the original call.
        public static System.Reflection.MethodInfo GetMethod(Type type, string name, System.Reflection.BindingFlags flags)
        {
            if (type == null || name == null || !MainThread.Current) return type.GetMethod(name, flags);
            return Method(type, name, (int)flags);
        }

        public static System.Reflection.MethodInfo GetMethod(Type type, string name)
        {
            if (type == null || name == null || !MainThread.Current) return type.GetMethod(name);
            return Method(type, name, -1); // -1 is not a valid flags combination: GetMethod(name)
        }

        static System.Reflection.MethodInfo Method(Type type, string name, int flags)
        {
            List<Found> found;
            if (!methods.TryGetValue(type, out found)) methods[type] = found = new List<Found>();
            int count = found.Count;
            for (int i = 0; i < count; i++)
            {
                Found entry = found[i];
                if (ReferenceEquals(entry.Name, name) && entry.Flags == flags) return entry.Method;
            }
            for (int i = 0; i < count; i++)
            {
                Found entry = found[i];
                if (entry.Flags == flags && string.Equals(entry.Name, name)) return entry.Method;
            }
            // An ambiguous match throws here, every time, as before.
            System.Reflection.MethodInfo method = flags != -1 ? type.GetMethod(name, (System.Reflection.BindingFlags)flags) : type.GetMethod(name);
            // Interaction lookups build names (prefix + script name); past a few dozen per type,
            // entries with names made at run time would only be scanned, so the list is restarted.
            if (count >= FoundPerType) found.Clear();
            found.Add(new Found { Name = name, Flags = flags, Method = method });
            return method;
        }

        // MethodBase.GetParameters copies the parameter array on every call; the callers this
        // replaces only read its length.
        static readonly Dictionary<System.Reflection.MethodBase, System.Reflection.ParameterInfo[]> parameters =
            new Dictionary<System.Reflection.MethodBase, System.Reflection.ParameterInfo[]>();

        public static System.Reflection.ParameterInfo[] GetParameters(System.Reflection.MethodBase method)
        {
            if (method == null || !MainThread.Current) return method.GetParameters();
            System.Reflection.ParameterInfo[] result;
            if (!parameters.TryGetValue(method, out result)) parameters[method] = result = method.GetParameters();
            return result;
        }

        // MethodInfo.Invoke(target, null) of a parameterless instance method returning void or
        // IEnumerator: PowerQuest.Update calls its scripts' UpdateNoPause and Update functions
        // (global, room and each shown GUI's) this way every frame, and PowerQuest's main loop
        // starts the global and room scripts' UpdateBlocking coroutines through
        // StartScriptInteractionCoroutine (MethodInfo.Invoke: 27-50 us a call on the PS Vita).
        // A delegate bound to the target is made once per method and target and called instead;
        // it returns what the method returns, and an exception it throws is wrapped in
        // TargetInvocationException, as Invoke wraps it. Anything else is left to Invoke.
        sealed class BoundInvoker
        {
            public object Target;
            public bool Returns;
            public Action Call;
            public Func<System.Collections.IEnumerator> Get;
        }

        // Per method; the delegates hold their targets (the game's scripts), so they are dropped
        // when a scene unloads and made again on the next call. notInvokable holds only methods.
        static readonly Dictionary<System.Reflection.MethodBase, BoundInvoker> invokers =
            new Dictionary<System.Reflection.MethodBase, BoundInvoker>();
        static readonly HashSet<System.Reflection.MethodBase> notInvokable = new HashSet<System.Reflection.MethodBase>();

        public static object Invoke(System.Reflection.MethodBase method, object target, object[] arguments)
        {
            if (method == null || target == null || (arguments != null && arguments.Length != 0) || !MainThread.Current)
                return method.Invoke(target, arguments);
            BoundInvoker invoker = InvokerFor(method);
            if (invoker == null) return method.Invoke(target, arguments);
            if (!ReferenceEquals(invoker.Target, target))
            {
                // Invoke rejects a target of another type with TargetException.
                if (!method.DeclaringType.IsInstanceOfType(target)) return method.Invoke(target, arguments);
                if (invoker.Returns)
                    invoker.Get = (Func<System.Collections.IEnumerator>)Delegate.CreateDelegate(typeof(Func<System.Collections.IEnumerator>), target, (System.Reflection.MethodInfo)method);
                else
                    invoker.Call = (Action)Delegate.CreateDelegate(typeof(Action), target, (System.Reflection.MethodInfo)method);
                invoker.Target = target;
            }
            try
            {
                if (invoker.Returns) return invoker.Get();
                invoker.Call();
            }
            catch (Exception exception)
            {
                throw new System.Reflection.TargetInvocationException(exception);
            }
            return null;
        }

        // The method's invoker, or null if it is not of the shape handled here.
        static BoundInvoker InvokerFor(System.Reflection.MethodBase method)
        {
            BoundInvoker invoker;
            if (invokers.TryGetValue(method, out invoker)) return invoker;
            if (notInvokable.Contains(method)) return null;
            var info = method as System.Reflection.MethodInfo;
            if (info == null || info.IsStatic || (info.ReturnType != typeof(void) && info.ReturnType != typeof(System.Collections.IEnumerator)) ||
                info.ContainsGenericParameters || info.GetParameters().Length != 0 || info.DeclaringType.IsValueType)
            {
                notInvokable.Add(method);
                return null;
            }
            return invokers[method] = new BoundInvoker { Returns = info.ReturnType != typeof(void) };
        }

        // When a scene unloads (SceneUnloads).
        internal static void Unloaded()
        {
            invokers.Clear();
        }

        // List.FindIndex with a predicate comparing references (a delegate and closure on each
        // call). Replaces whole methods of that shape, such as PowerQuest.GetCharacterId.
        public static int IndexOfReference(System.Collections.IList list, object item)
        {
            int count = list.Count;
            for (int i = 0; i < count; i++)
                if (ReferenceEquals(list[i], item)) return i;
            return -1;
        }

        // CharacterRimlight.LateUpdate creates up to three MaterialPropertyBlocks a frame per
        // character, each filled by GetPropertyBlock and copied to the renderer by
        // SetPropertyBlock in the same method. They share one, empty when handed out (main thread
        // only, as that method runs).
        static MaterialPropertyBlock sharedBlock;

        public static MaterialPropertyBlock NewPropertyBlock()
        {
            if (sharedBlock == null) sharedBlock = new MaterialPropertyBlock();
            sharedBlock.Clear();
            return sharedBlock;
        }
    }

    // List<T>.AddRange of another collection copies it into a temporary array first
    // (List.InsertRange): QuestMenuManager.Update and PowerQuest.GetAllClickables do that every
    // frame, the latter of a ToArray copy made only for it. The same items in the same order,
    // without the temporary arrays.
    public static class Lists
    {
        public static void AddRange<T>(List<T> list, IEnumerable<T> items)
        {
            if (list != null && !ReferenceEquals(items, list))
            {
                var indexed = items as IList<T>;
                if (indexed != null)
                {
                    Reserve(list, indexed.Count);
                    for (int i = 0; i < indexed.Count; i++) list.Add(indexed[i]);
                    return;
                }
                // A List<Derived> passed as IEnumerable<Base>.
                var readOnly = items as IReadOnlyList<T>;
                if (readOnly != null)
                {
                    Reserve(list, readOnly.Count);
                    for (int i = 0; i < readOnly.Count; i++) list.Add(readOnly[i]);
                    return;
                }
            }
            list.AddRange(items);
        }

        // list.AddRange(other.ToArray()): the copy only fed AddRange. A null source threw in
        // ToArray.
        public static void AddRangeOfCopy<T>(List<T> list, IEnumerable<T> items)
        {
            if (items == null) throw new NullReferenceException();
            AddRange(list, items);
        }

        // List<T>.Sort(comparison) of a list refilled with the same items every frame
        // (QuestMenuManager.Update sorts the GUIs by baseline, then instance id;
        // PowerQuest.UpdateGuiVisibility by baseline alone). Sort depends only on the answers of
        // the comparisons it makes. When the items are the previous call's, in the same order,
        // with the same comparison, and every comparison it could make answers as in the previous
        // call, it would produce the previous result, which is copied back without sorting.
        // - Sort: the previous result must still be in strictly increasing order; it is then the
        //   only sorted order of the items.
        // - SortByKey, for comparisons of one key per item (a.Baseline.CompareTo(b.Baseline): a
        //   total preorder): neighbours in the previous result may also be equal. For such a
        //   comparison the answer for any two items follows from the answers for the neighbours
        //   between them in a sorted list, so if each pair of neighbours still compares as it did
        //   (sign), every pair does.
        // With any change, Sort runs.
        sealed class SortMemo
        {
            public object[] Input = new object[0], Output = new object[0];
            public int[] Signs = new int[0];
            public Delegate Comparison;
        }

        // By list. Kept for the session: the lists sorted are QuestMenuManager's and PowerQuest's
        // m_sortedGuis, which live as long as the game, as do the GUIs they hold.
        static readonly Dictionary<object, SortMemo> sorts = new Dictionary<object, SortMemo>();

        public static void Sort<T>(List<T> list, Comparison<T> comparison) where T : class
        {
            Memoized(list, comparison, false);
        }

        public static void SortByKey<T>(List<T> list, Comparison<T> comparison) where T : class
        {
            Memoized(list, comparison, true);
        }

        static void Memoized<T>(List<T> list, Comparison<T> comparison, bool ties) where T : class
        {
            if (list == null || comparison == null || !MainThread.Current)
            {
                list.Sort(comparison);
                return;
            }
            SortMemo memo;
            if (!sorts.TryGetValue(list, out memo)) sorts[list] = memo = new SortMemo();
            int count = list.Count;
            bool same = memo.Input.Length == count && ReferenceEquals(memo.Comparison, comparison);
            for (int i = 0; same && i < count; i++) same = ReferenceEquals(memo.Input[i], list[i]);
            for (int i = 0; same && i + 1 < count; i++)
            {
                int sign = Math.Sign(comparison((T)memo.Output[i], (T)memo.Output[i + 1]));
                same = ties ? sign == memo.Signs[i] : sign < 0;
            }
            if (same)
            {
                for (int i = 0; i < count; i++) list[i] = (T)memo.Output[i];
                return;
            }
            if (memo.Input.Length != count)
            {
                memo.Input = new object[count];
                memo.Output = new object[count];
                memo.Signs = new int[Math.Max(0, count - 1)];
            }
            memo.Comparison = comparison;
            for (int i = 0; i < count; i++) memo.Input[i] = list[i];
            list.Sort(comparison);
            for (int i = 0; i < count; i++) memo.Output[i] = list[i];
            if (ties)
                for (int i = 0; i + 1 < count; i++) memo.Signs[i] = Math.Sign(comparison(list[i], list[i + 1]));
        }

        // Room for count more items, grown as List<T> grows itself (from 4, doubling).
        static void Reserve<T>(List<T> list, int count)
        {
            if (list.Capacity < list.Count + count)
                list.Capacity = Math.Max(list.Count + count, list.Capacity == 0 ? 4 : list.Capacity * 2);
        }

        // List<T>.Find, Exists, FindIndex, FindAll and RemoveAll whose predicate was a lambda
        // capturing one value (see InlineKeyClosures in rewriter/ScriptCosts.cs): match is a
        // delegate made once to a static method taking the item and that value. Same visits
        // in the same order as the List methods.
        public static T Find<T, K>(List<T> list, K key, Func<T, K, bool> match)
        {
            for (int i = 0; i < list.Count; i++)
                if (match(list[i], key)) return list[i];
            return default(T);
        }

        public static int FindIndex<T, K>(List<T> list, K key, Func<T, K, bool> match)
        {
            int count = list.Count;
            for (int i = 0; i < count; i++)
                if (match(list[i], key)) return i;
            return -1;
        }

        public static bool Exists<T, K>(List<T> list, K key, Func<T, K, bool> match)
        {
            return FindIndex(list, key, match) != -1;
        }

        public static List<T> FindAll<T, K>(List<T> list, K key, Func<T, K, bool> match)
        {
            var found = new List<T>();
            for (int i = 0; i < list.Count; i++)
                if (match(list[i], key)) found.Add(list[i]);
            return found;
        }

        public static int RemoveAll<T, K>(List<T> list, K key, Func<T, K, bool> match)
        {
            int free = 0;
            while (free < list.Count && !match(list[free], key)) free++;
            if (free >= list.Count) return 0;
            int current = free + 1;
            while (current < list.Count)
            {
                while (current < list.Count && match(list[current], key)) current++;
                if (current < list.Count) list[free++] = list[current++];
            }
            int removed = list.Count - free;
            list.RemoveRange(free, removed);
            return removed;
        }
    }
}
