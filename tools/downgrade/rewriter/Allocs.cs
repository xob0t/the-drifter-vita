using System;
using System.Collections.Generic;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

// Per-frame allocations, the ReduceAllocations passes of DowngradeAssemblies.Passes (Compat Allocs
// and the classes after it; docs/pipeline.md "Allocations").
// Part of the assembly rewriter; see DowngradeAssemblies.cs.
partial class DowngradeAssemblies
{
    // Each rewritten site gets a number (one process rewrites every assembly, so they are unique)
    // that indexes the site's state in Compat.
    static int allocSite;

    static readonly HashSet<string> NonRetainingListMethods = new HashSet<string>
        { "Find", "FindIndex", "FindLast", "FindLastIndex", "FindAll", "Exists", "TrueForAll", "RemoveAll", "ForEach", "ConvertAll", "Sort" };
    static readonly HashSet<string> NonRetainingArrayMethods = new HashSet<string>
        { "ForEach", "Find", "FindIndex", "FindLast", "FindLastIndex", "FindAll", "Exists", "TrueForAll", "ConvertAll", "Sort" };

    static HashSet<Instruction> BranchTargets(MethodBody body)
    {
        var targets = new HashSet<Instruction>(body.Instructions.SelectMany(i => i.Operand is Instruction ? new[] { (Instruction)i.Operand }
            : i.Operand is Instruction[] ? (Instruction[])i.Operand : new Instruction[0]));
        foreach (ExceptionHandler handler in body.ExceptionHandlers)
            foreach (Instruction at in new[] { handler.TryStart, handler.TryEnd, handler.HandlerStart, handler.HandlerEnd, handler.FilterStart })
                if (at != null) targets.Add(at);
        return targets;
    }

    // Whether an instruction lies inside a loop: some branch at or after it jumps back to it or
    // before it.
    static bool InLoop(MethodBody body, Instruction instruction)
    {
        var code = body.Instructions;
        int at = code.IndexOf(instruction);
        for (int i = at; i < code.Count; i++)
        {
            var single = code[i].Operand as Instruction;
            var many = code[i].Operand as Instruction[];
            foreach (Instruction target in single != null ? new[] { single } : many ?? new Instruction[0])
                if (code.IndexOf(target) <= at) return true;
        }
        return false;
    }

    // The instruction that pops the value an instruction pushes, on a straight path without
    // branch targets, and the value's place among what it pops (0: the last pushed); null when
    // that cannot be told.
    static Instruction ConsumerOf(Instruction load, HashSet<Instruction> targets, out int position)
    {
        position = -1;
        int above = 0;
        for (Instruction at = load.Next; at != null; at = at.Next)
        {
            if (targets.Contains(at)) return null;
            int[] effect = StackEffect(at);
            if (effect == null) return null;
            if (effect[0] > above)
            {
                position = above;
                return at;
            }
            above += effect[1] - effect[0];
        }
        return null;
    }

    // A call that takes a delegate as its last argument and does not keep it.
    static bool TakesDelegateWithoutKeeping(Instruction call)
    {
        var method = call.Operand as MethodReference;
        if (method == null || (call.OpCode != OpCodes.Call && call.OpCode != OpCodes.Callvirt) || method.Parameters.Count == 0) return false;
        string last = method.Parameters[method.Parameters.Count - 1].ParameterType.GetElementType().FullName;
        if (!last.StartsWith("System.Predicate`") && !last.StartsWith("System.Action`") && !last.StartsWith("System.Comparison`") &&
            !last.StartsWith("System.Converter`") && last != "System.Text.RegularExpressions.MatchEvaluator")
            return false;
        string owner = method.DeclaringType.GetElementType().FullName;
        if (owner == "System.Collections.Generic.List`1") return method.HasThis && NonRetainingListMethods.Contains(method.Name);
        if (owner == "System.Array") return !method.HasThis && NonRetainingArrayMethods.Contains(method.Name);
        if (owner == "System.Text.RegularExpressions.Regex") return method.HasThis && method.Name == "Replace";
        return false;
    }

    static bool IsDelegateConstructor(Instruction make)
    {
        var constructor = make == null ? null : make.Operand as MethodReference;
        return make != null && make.OpCode == OpCodes.Newobj && constructor != null && constructor.Parameters.Count == 2 &&
               constructor.Parameters[0].ParameterType.FullName == "System.Object" && constructor.Parameters[1].ParameterType.FullName == "System.IntPtr";
    }

    static bool IsThisLoad(Instruction instruction)
    {
        Code op = instruction.OpCode.Code;
        return op == Code.Ldarg_0 || ((op == Code.Ldarg || op == Code.Ldarg_S) && ((ParameterDefinition)instruction.Operand).Index == -1);
    }

    // <load object> <value without branches> stfld owner::field: the stfld, or null.
    static Instruction FieldStoreAfter(Instruction load, HashSet<Instruction> targets, TypeDefinition owner, Func<Instruction, bool> reloads)
    {
        int depth = 1;
        for (Instruction at = load.Next; at != null; at = at.Next)
        {
            if (targets.Contains(at)) return null;
            var stored = at.Operand as FieldReference;
            if (at.OpCode == OpCodes.Stfld && stored != null && stored.DeclaringType == owner && depth == 2) return at;
            if (reloads(at)) return null;
            int[] effect = StackEffect(at);
            if (effect == null || depth - effect[0] < 1) return null;
            depth += effect[1] - effect[0];
        }
        return null;
    }

    // Closure classes (<>c__DisplayClass) that only one method outside them refers to, a method
    // of the class they are nested in, neither of them generic: (closure, method).
    static List<Tuple<TypeDefinition, MethodDefinition>> PrivateClosures(ModuleDefinition module)
    {
        var users = new Dictionary<TypeDefinition, HashSet<MethodDefinition>>();
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
        foreach (Instruction instruction in method.Body.Instructions)
        {
            TypeReference referenced = instruction.Operand is MemberReference && !(instruction.Operand is TypeReference)
                ? ((MemberReference)instruction.Operand).DeclaringType : instruction.Operand as TypeReference;
            var closure = referenced as TypeDefinition;
            if (closure == null || closure == type || !closure.Name.StartsWith("<>c__DisplayClass")) continue;
            HashSet<MethodDefinition> set;
            if (!users.TryGetValue(closure, out set)) users[closure] = set = new HashSet<MethodDefinition>();
            set.Add(method);
        }
        var closures = new List<Tuple<TypeDefinition, MethodDefinition>>();
        foreach (var entry in users)
        {
            TypeDefinition closure = entry.Key;
            if (entry.Value.Count != 1 || closure.HasGenericParameters || closure.DeclaringType == null || closure.DeclaringType.HasGenericParameters ||
                closure.BaseType.FullName != "System.Object")
                continue;
            MethodDefinition method = entry.Value.Single();
            if (!method.HasGenericParameters && method.DeclaringType == closure.DeclaringType) closures.Add(Tuple.Create(closure, method));
        }
        return closures;
    }

    // See Compat ReusedClosures.
    static int ReuseClosures(ModuleDefinition module)
    {
        int count = 0;
        foreach (var pair in PrivateClosures(module))
            if (!pair.Item1.HasNestedTypes && !pair.Item1.Fields.Any(f => f.IsStatic) && !pair.Item2.DeclaringType.Name.StartsWith("<") &&
                ClosureStaysInside(pair.Item1) && ReuseClosure(module, pair.Item2, pair.Item1))
                count++;
        return count;
    }

    // The closure's own methods use their instance only to read and write its fields.
    static bool ClosureStaysInside(TypeDefinition closure)
    {
        foreach (MethodDefinition method in closure.Methods)
        {
            if (method.IsConstructor)
            {
                if (method.IsStatic || method.Parameters.Count > 0) return false;
                continue;
            }
            if (method.IsStatic || !method.HasBody || method.HasGenericParameters) return false;
            HashSet<Instruction> targets = BranchTargets(method.Body);
            foreach (Instruction instruction in method.Body.Instructions)
            {
                Code op = instruction.OpCode.Code;
                if ((op == Code.Starg || op == Code.Starg_S || op == Code.Ldarga || op == Code.Ldarga_S) &&
                    ((ParameterDefinition)instruction.Operand).Index == -1)
                    return false;
                if (!IsThisLoad(instruction)) continue;
                Instruction next = instruction.Next;
                var field = next == null ? null : next.Operand as FieldReference;
                if (field != null && (next.OpCode == OpCodes.Ldfld || next.OpCode == OpCodes.Ldflda) && field.DeclaringType == closure &&
                    !targets.Contains(next))
                    continue;
                if (FieldStoreAfter(instruction, targets, closure, IsThisLoad) != null) continue;
                return false;
            }
        }
        return true;
    }

    static bool ReuseClosure(ModuleDefinition module, MethodDefinition method, TypeDefinition closure)
    {
        MethodBody body = method.Body;
        var code = body.Instructions;
        if (!body.InitLocals || code.Any(i => i.Operand is VariableDefinition && ((VariableDefinition)i.Operand).VariableType == closure &&
                                              (i.OpCode.Code == Code.Ldloca || i.OpCode.Code == Code.Ldloca_S)))
            return false;
        Instruction[] makes = code.Where(i => i.OpCode == OpCodes.Newobj && ((MethodReference)i.Operand).DeclaringType == closure).ToArray();
        if (makes.Length != 1) return false;
        Instruction make = makes[0];
        VariableDefinition local = make.Next == null ? null : LocalOf(method, make.Next, true);
        HashSet<Instruction> targets = BranchTargets(body);
        if (local == null || local.VariableType != closure || targets.Contains(make.Next) || InLoop(body, make) ||
            code.Count(i => LocalOf(method, i, true) == local) != 1)
            return false;
        var delegateLoads = new List<Instruction>();
        HashSet<Instruction> cachedIdiom = CachedDelegateIdioms(method, closure, local);
        if (cachedIdiom == null) return false;
        foreach (Instruction instruction in code)
        {
            if (LocalOf(method, instruction, false) != local || cachedIdiom.Contains(instruction)) continue;
            Instruction next = instruction.Next;
            if (next == null || targets.Contains(next)) return false;
            var field = next.Operand as FieldReference;
            if (field != null && (next.OpCode == OpCodes.Ldfld || next.OpCode == OpCodes.Ldflda) && field.DeclaringType == closure) continue;
            if (next.OpCode == OpCodes.Ldftn)
            {
                var lambda = next.Operand as MethodDefinition;
                Instruction made = next.Next, call = made == null ? null : made.Next;
                if (lambda == null || lambda.DeclaringType != closure || call == null || targets.Contains(made) || targets.Contains(call) ||
                    !IsDelegateConstructor(made) || !TakesDelegateWithoutKeeping(call))
                    return false;
                delegateLoads.Add(instruction);
                continue;
            }
            if (FieldStoreAfter(instruction, targets, closure, at => LocalOf(method, at, false) == local || LocalOf(method, at, true) == local) != null)
                continue;
            return false;
        }
        List<Instruction> listInits = RefillableClosureLists(method, closure, local, targets);
        int site = allocSite++;
        MethodReference take = CompatMethod(module, "ReusedClosures", "Take"), keep = CompatMethod(module, "ReusedClosures", "Keep"),
            release = CompatMethod(module, "ReusedClosures", "Release"), cached = CompatMethod(module, "ReusedClosures", "Delegate"),
            keepDelegate = CompatMethod(module, "ReusedClosures", "KeepDelegate");
        ILProcessor il = body.GetILProcessor();
        TypeReference closureType = ((MethodReference)make.Operand).DeclaringType;
        Instruction cast = il.Create(OpCodes.Castclass, closureType);
        InsertBeforeRetarget(method, make, il.Create(OpCodes.Ldc_I4, site), il.Create(OpCodes.Call, take), il.Create(OpCodes.Dup),
                             il.Create(OpCodes.Brtrue, cast), il.Create(OpCodes.Pop));
        InsertAfter(il, make, il.Create(OpCodes.Ldc_I4, site), il.Create(OpCodes.Call, keep), cast);
        BeforeRets(method, () => new[] { il.Create(OpCodes.Ldloc, local), il.Create(OpCodes.Ldc_I4, site), il.Create(OpCodes.Call, release) });
        foreach (Instruction newList in listInits)
        {
            var field = (FieldReference)newList.Next.Operand;
            InsertBeforeRetarget(method, newList, il.Create(OpCodes.Ldloc, local), il.Create(OpCodes.Ldfld, field));
            newList.OpCode = OpCodes.Call;
            newList.Operand = CompatMethod(module, "ScratchLists", "Refill", ((GenericInstanceType)((MethodReference)newList.Operand).DeclaringType).GenericArguments[0]);
        }
        foreach (Instruction load in delegateLoads) CacheDelegate(il, load, allocSite++, cached, keepDelegate, () => il.Create(OpCodes.Ldloc, local));
        return true;
    }

    // The compiler keeps a delegate made in a loop in a field of the closure itself:
    //   ldloc c; ldfld f; dup; brtrue X; pop; ldloc c; ldloc c; ldftn m; newobj D; dup; stloc t; stfld f; ldloc t; X: call
    // The delegate is bound to the closure and stays with it; the call must not keep it. The
    // instructions of each such idiom from the second "ldloc c" (ReuseClosure leaves them as
    // they are), or null when a cache field is used elsewhere too.
    static HashSet<Instruction> CachedDelegateIdioms(MethodDefinition method, TypeDefinition closure, VariableDefinition local)
    {
        const int idiomLength = 8;  // ldloc c; ldloc c; ldftn; newobj; dup; stloc t; stfld f; ldloc t
        var code = method.Body.Instructions;
        var idioms = new HashSet<Instruction>();
        foreach (Instruction instruction in code)
        {
            if (LocalOf(method, instruction, false) != local) continue;
            Instruction[] run = new Instruction[idiomLength];
            Instruction at = instruction;
            for (int i = 0; i < run.Length && at != null; i++, at = at.Next) run[i] = at;
            if (run[7] == null || LocalOf(method, run[1], false) != local || run[2].OpCode != OpCodes.Ldftn || !(run[2].Operand is MethodDefinition) ||
                ((MethodDefinition)run[2].Operand).DeclaringType != closure || !IsDelegateConstructor(run[3]) || run[4].OpCode != OpCodes.Dup ||
                LocalOf(method, run[5], true) == null || run[6].OpCode != OpCodes.Stfld || !(run[6].Operand is FieldReference) ||
                ((FieldReference)run[6].Operand).DeclaringType != closure || LocalOf(method, run[7], false) != LocalOf(method, run[5], true) ||
                run[7].Next == null || !TakesDelegateWithoutKeeping(run[7].Next))
                continue;
            FieldReference cacheField = (FieldReference)run[6].Operand;
            // The field is used only here: read before (ldloc c; ldfld f) and written here.
            if (code.Count(i => i.Operand == cacheField && i.OpCode == OpCodes.Stfld) != 1 ||
                closure.Methods.Any(m => m.HasBody && m.Body.Instructions.Any(i => i.Operand == cacheField)))
                return null;
            foreach (Instruction part in run) idioms.Add(part);
        }
        return idioms;
    }

    // A list made for a closure field at each call: the previous call's one is refilled when
    // nothing but the field ever holds it (every read of the field is the object of one of the
    // list's own methods, in the method and in the closure's lambdas). The list constructions.
    static List<Instruction> RefillableClosureLists(MethodDefinition method, TypeDefinition closure, VariableDefinition local, HashSet<Instruction> targets)
    {
        var code = method.Body.Instructions;
        var listInits = new List<Instruction>();
        foreach (Instruction instruction in code)
        {
            if (LocalOf(method, instruction, false) != local || instruction.Next == null || instruction.Next.OpCode != OpCodes.Newobj) continue;
            Instruction newList = instruction.Next, store = newList.Next;
            var constructor = (MethodReference)newList.Operand;
            var field = store == null ? null : store.Operand as FieldDefinition;
            if (constructor.Parameters.Count != 0 || constructor.DeclaringType.GetElementType().FullName != "System.Collections.Generic.List`1" ||
                store.OpCode != OpCodes.Stfld || field == null || field.DeclaringType != closure || targets.Contains(newList) || targets.Contains(store) ||
                code.Count(i => i.Operand == field && i.OpCode == OpCodes.Stfld) != 1)
                continue;
            bool onlyList = true;
            foreach (MethodDefinition user in closure.Methods.Where(m => m.HasBody).Concat(new[] { method }))
            {
                HashSet<Instruction> userTargets = user == method ? targets : BranchTargets(user.Body);
                foreach (Instruction read in user.Body.Instructions.Where(i => i.Operand == field && i.OpCode != OpCodes.Stfld))
                {
                    int position = -1;
                    Instruction use = read.OpCode == OpCodes.Ldfld ? ConsumerOf(read, userTargets, out position) : null;
                    var used = use == null ? null : use.Operand as MethodReference;
                    if (used == null || !used.HasThis || used.DeclaringType.GetElementType().FullName != "System.Collections.Generic.List`1" ||
                        !ScratchListUses.Contains(used.Name) || position != used.Parameters.Count)
                        onlyList = false;
                }
            }
            if (onlyList) listInits.Add(newList);
        }
        return listInits;
    }

    // "<load owner>; ldftn m; newobj D" becomes "<load owner>; ldc site; call get; dup; brtrue got;
    // pop; <owner>; ldftn m; newobj D; <owner>; ldc site; call keep; got: castclass D": get(owner,
    // site) answers the delegate kept for the owner, or null; keep(made, owner, site) keeps it.
    static void CacheDelegate(ILProcessor il, Instruction load, int site, MethodReference get, MethodReference keep, Func<Instruction> owner)
    {
        Instruction made = load.Next.Next;
        Instruction got = il.Create(OpCodes.Castclass, ((MethodReference)made.Operand).DeclaringType);
        InsertAfter(il, load, il.Create(OpCodes.Ldc_I4, site), il.Create(OpCodes.Call, get), il.Create(OpCodes.Dup), il.Create(OpCodes.Brtrue, got),
                    il.Create(OpCodes.Pop), owner());
        InsertAfter(il, made, owner(), il.Create(OpCodes.Ldc_I4, site), il.Create(OpCodes.Call, keep), got);
    }

    // See Compat CachedLambdas: this.<method> made into a delegate and passed straight to a
    // List/Array method that does not keep it.
    static int CacheThisLambdas(ModuleDefinition module)
    {
        MethodReference get = CompatMethod(module, "CachedLambdas", "Get"), keep = CompatMethod(module, "CachedLambdas", "Keep");
        int count = 0;
        foreach (TypeDefinition type in AllTypes(module.Types))
        {
            if (type.IsValueType || type.IsInterface || type.HasGenericParameters || type.Name.StartsWith("<") ||
                (type.DeclaringType != null && type.DeclaringType.HasGenericParameters))
                continue;
            foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody && !m.IsStatic))
            {
                MethodBody body = method.Body;
                if (body.Instructions.Any(i => (i.OpCode.Code == Code.Starg || i.OpCode.Code == Code.Starg_S) &&
                                               ((ParameterDefinition)i.Operand).Index == -1))
                    continue;
                HashSet<Instruction> targets = null;
                foreach (Instruction load in body.Instructions.Where(i => i.OpCode == OpCodes.Ldarg_0).ToArray())
                {
                    Instruction ftn = load.Next, made = ftn == null ? null : ftn.Next, call = made == null ? null : made.Next;
                    if (ftn == null || ftn.OpCode != OpCodes.Ldftn || call == null || !IsDelegateConstructor(made) || !TakesDelegateWithoutKeeping(call))
                        continue;
                    if (!((MethodReference)ftn.Operand).HasThis) continue;
                    if (targets == null) targets = BranchTargets(body);
                    if (targets.Contains(ftn) || targets.Contains(made) || targets.Contains(call)) continue;
                    ILProcessor il = body.GetILProcessor();
                    CacheDelegate(il, load, allocSite++, get, keep, () => il.Create(OpCodes.Ldarg_0));
                    count++;
                }
            }
        }
        return count;
    }

    // See Compat ChildArrays: (type, method, expected sites). The arrays are only read in these
    // methods (indexed loops, Array.ForEach) and the code run meanwhile makes only engine calls.
    static readonly string[][] ChildArraySites =
    {
        new[] { "PowerTools.Quest.PowerQuest", "UpdateRegions", "1" },
        new[] { "PowerTools.Quest.GuiControl", "set_Alpha", "2" },
        // FitToObject.LateUpdate (GUI layout, every frame) for each object it fits around.
        new[] { "PowerTools.Quest.GuiUtils", "CalculateGuiRect", "1" },
    };

    static int PoolChildArrays(ModuleDefinition module)
    {
        int count = 0;
        foreach (string[] rule in ChildArraySites)
        {
            TypeDefinition type = GameType(module, rule[0]);
            if (type == null) continue;
            MethodDefinition method = type.Methods.Single(m => m.Name == rule[1] && m.HasBody);
            Instruction[] calls = method.Body.Instructions.Where(i => i.Operand is GenericInstanceMethod &&
                CalledName(i) == "GetComponentsInChildren" && ((MethodReference)i.Operand).Parameters.Count == 1 &&
                ((MethodReference)i.Operand).Parameters[0].ParameterType.FullName == "System.Boolean").ToArray();
            if (calls.Length != int.Parse(rule[2]) || calls.Any(c => ((MethodReference)c.Operand).DeclaringType.FullName != "UnityEngine.Component" &&
                                                                     ((MethodReference)c.Operand).DeclaringType.FullName != "UnityEngine.GameObject") ||
                BranchTargets(method.Body).Contains(method.Body.Instructions[0]))
                throw new Exception(rule[0] + "." + rule[1] + " does not have the expected shape");
            int methodSite = allocSite++;
            foreach (Instruction call in calls)
            {
                var called = (GenericInstanceMethod)call.Operand;
                bool onComponent = called.DeclaringType.FullName == "UnityEngine.Component";
                InsertBeforeRetarget(method, call, Instruction.Create(OpCodes.Ldc_I4, allocSite++), Instruction.Create(OpCodes.Ldc_I4, methodSite));
                call.OpCode = OpCodes.Call;
                call.Operand = CompatMethod(module, "ChildArrays", onComponent ? "InChildren" : "InChildrenOf", called.GenericArguments[0]);
                count++;
            }
            BeforeRets(method, () => new[] { Instruction.Create(OpCodes.Ldc_I4, methodSite), Instruction.Create(OpCodes.Call, CompatMethod(module, "ChildArrays", "Exit")) });
            Prepend(method, Instruction.Create(OpCodes.Ldc_I4, methodSite), Instruction.Create(OpCodes.Call, CompatMethod(module, "ChildArrays", "Enter")));
        }
        return count;
    }

    // See Compat ScratchLists: methods whose local new List<T>() never leaves them.
    static readonly string[][] ScratchListMethods =
    {
        new[] { "PowerTools.Pathfinder", "FindPath" },
        new[] { "PowerTools.Pathfinder", "EvaluateDijkstra" },
        new[] { "PowerTools.Pathfinder", "GetClosestPointToArea" },
        // GUI layout (FitToObject, every frame): a small sprite's physics shape.
        new[] { "PowerTools.Quest.GuiUtils", "CalculateGuiRectFromSprite" },
    };

    static readonly HashSet<string> ScratchListUses = new HashSet<string>
    {
        "Add", "AddRange", "Insert", "InsertRange", "get_Item", "set_Item", "get_Count", "Remove", "RemoveAt", "RemoveAll", "RemoveRange",
        "Clear", "Contains", "IndexOf", "LastIndexOf", "ToArray", "GetEnumerator", "Sort", "Reverse", "Find", "FindIndex", "Exists",
        "ForEach", "TrueForAll", "CopyTo",
    };

    static int UseScratchLists(ModuleDefinition module)
    {
        int count = 0;
        foreach (string[] rule in ScratchListMethods)
        {
            TypeDefinition type = GameType(module, rule[0]);
            if (type == null) continue;
            int lists = count;
            foreach (MethodDefinition method in type.Methods.Where(m => m.Name == rule[1] && m.HasBody))
            {
                MethodBody body = method.Body;
                HashSet<Instruction> targets = BranchTargets(body);
                if (!body.InitLocals) throw new Exception(rule[0] + "." + rule[1] + " does not have the expected shape");
                foreach (Instruction make in body.Instructions.Where(i => i.OpCode == OpCodes.Newobj &&
                    ((MethodReference)i.Operand).DeclaringType.GetElementType().FullName == "System.Collections.Generic.List`1" &&
                    ((MethodReference)i.Operand).Parameters.Count == 0).ToArray())
                {
                    VariableDefinition local = make.Next == null ? null : LocalOf(method, make.Next, true);
                    if (local == null || targets.Contains(make.Next) || InLoop(body, make) ||
                        body.Instructions.Count(i => LocalOf(method, i, true) == local) != 1 ||
                        body.Instructions.Any(i => i.Operand == local && (i.OpCode.Code == Code.Ldloca || i.OpCode.Code == Code.Ldloca_S)))
                        throw new Exception(rule[0] + "." + rule[1] + " does not have the expected shape");
                    foreach (Instruction load in body.Instructions.Where(i => LocalOf(method, i, false) == local))
                    {
                        int position;
                        Instruction use = ConsumerOf(load, targets, out position);
                        var used = use == null ? null : use.Operand as MethodReference;
                        bool own = used != null && used.HasThis && used.DeclaringType.GetElementType().FullName == "System.Collections.Generic.List`1" &&
                                   ScratchListUses.Contains(used.Name) && position == used.Parameters.Count;
                        bool compatList = used != null && !used.HasThis && used.DeclaringType.FullName == "DrifterCompat.Lists" &&
                                          position == used.Parameters.Count - 1;
                        // Engine calls that fill the list they are given.
                        bool filled = used != null && position == 0 &&
                                      used.FullName == "System.Int32 UnityEngine.Sprite::GetPhysicsShape(System.Int32,System.Collections.Generic.List`1<UnityEngine.Vector2>)";
                        if (!own && !compatList && !filled) throw new Exception(rule[0] + "." + rule[1] + ": a list leaves the method");
                    }
                    var listType = (GenericInstanceType)((MethodReference)make.Operand).DeclaringType;
                    MethodReference takeList = CompatMethod(module, "ScratchLists", "Take", listType.GenericArguments[0]);
                    int site = allocSite++;
                    InsertBeforeRetarget(method, make, Instruction.Create(OpCodes.Ldc_I4, site));
                    make.OpCode = OpCodes.Call;
                    make.Operand = takeList;
                    MethodReference release = CompatMethod(module, "ScratchLists", "Release");
                    BeforeRets(method, () => new[] { Instruction.Create(OpCodes.Ldloc, local), Instruction.Create(OpCodes.Ldc_I4, site), Instruction.Create(OpCodes.Call, release) });
                    count++;
                }
            }
            Count("ScratchLists in " + rule[0] + "." + rule[1], count - lists);
        }
        return count;
    }

    // See Compat ColliderPoints: RegionComponent.GetDistanceIntoRegion's points read, and every
    // change of a polygon collider's paths in the game's code.
    static int CacheColliderPoints(ModuleDefinition module)
    {
        int count = 0;
        var writes = new Dictionary<string, string>
        {
            { "System.Void UnityEngine.PolygonCollider2D::SetPath(System.Int32,UnityEngine.Vector2[])", "SetPath" },
            { "System.Void UnityEngine.PolygonCollider2D::set_points(UnityEngine.Vector2[])", "SetPoints" },
            { "System.Void UnityEngine.PolygonCollider2D::set_pathCount(System.Int32)", "SetPathCount" },
        };
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
        foreach (Instruction instruction in method.Body.Instructions)
        {
            var called = instruction.Operand as MethodReference;
            string helper;
            if (called == null || (instruction.OpCode != OpCodes.Call && instruction.OpCode != OpCodes.Callvirt) ||
                !writes.TryGetValue(called.FullName, out helper))
                continue;
            instruction.OpCode = OpCodes.Call;
            instruction.Operand = CompatMethod(module, "ColliderPoints", helper);
            count++;
        }
        // Reads whose copy is only indexed or measured on the spot (ldelem/ldlen right after the
        // call, nothing else sees the array): InvWheel.GetIntersection reads m_poly.points up to
        // three times per polygon edge, for each wheel item every frame the wheel is open.
        const string pointsGetter = "UnityEngine.Vector2[] UnityEngine.PolygonCollider2D::get_points()";
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
        {
            HashSet<Instruction> targets = null;
            foreach (Instruction read in method.Body.Instructions.Where(i => (i.OpCode == OpCodes.Call || i.OpCode == OpCodes.Callvirt) &&
                     CalledFullName(i) == pointsGetter).ToList())
            {
                if (targets == null) targets = BranchTargets(method.Body);
                Instruction next = read.Next;
                bool indexed = next != null && !targets.Contains(next) && next.OpCode == OpCodes.Ldlen;
                if (!indexed && next != null && !targets.Contains(next))
                {
                    int position;
                    Instruction consumer = ConsumerOf(read, targets, out position);
                    indexed = consumer != null && position == 1 && consumer.OpCode == OpCodes.Ldelem_Any &&
                              ((TypeReference)consumer.Operand).FullName == "UnityEngine.Vector2";
                }
                if (!indexed) continue;
                read.OpCode = OpCodes.Call;
                read.Operand = CompatMethod(module, "ColliderPoints", "Get");
                count++;
            }
        }
        TypeDefinition region = GameType(module, "PowerTools.Quest.RegionComponent");
        if (region == null) return count;
        MethodDefinition distance = region.Methods.Single(m => m.Name == "GetDistanceIntoRegion" && m.HasBody);
        Instruction[] reads = distance.Body.Instructions.Where(i => CalledFullName(i) == pointsGetter).ToArray();
        TypeDefinition util = AllTypes(module.Types).FirstOrDefault(t => t.Name == "RegionPolyUtil");
        MethodDefinition edge = util == null ? null : util.Methods.FirstOrDefault(m => m.Name == "CalcDistToEdge");
        if (reads.Length != 1 || edge == null || reads[0].Next == null || reads[0].Next.Next == null ||
            reads[0].Next.Next.Operand != edge ||
            edge.Body.Instructions.Any(i => i.OpCode.Code.ToString().StartsWith("Stelem") || i.OpCode == OpCodes.Stobj || i.OpCode == OpCodes.Initobj ||
                                             i.OpCode == OpCodes.Starg || i.OpCode == OpCodes.Starg_S || i.OpCode == OpCodes.Call && i.Operand is MethodReference &&
                                             ((MethodReference)i.Operand).Parameters.Any(p => p.ParameterType.FullName == "UnityEngine.Vector2[]")))
            throw new Exception("RegionComponent.GetDistanceIntoRegion does not have the expected shape");
        reads[0].OpCode = OpCodes.Call;
        reads[0].Operand = CompatMethod(module, "ColliderPoints", "Get");
        return count + 1;
    }

    // See Compat PathPool.
    static int PoolPathLinks(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Pathfinder");
        if (type == null) return 0;
        TypeDefinition node = type.NestedTypes.Single(t => t.Name == "PathNode"), link = type.NestedTypes.Single(t => t.Name == "PathLink");
        FieldDefinition links = node.Fields.Single(f => f.Name == "m_links");
        FieldDefinition[] linkFields = link.Fields.Where(f => !f.IsStatic).ToArray();
        MethodDefinition constructor = node.Methods.Single(m => m.IsConstructor && !m.IsStatic && m.Parameters.Count == 0);
        MethodDefinition calculate = type.Methods.Single(m => m.Name == "CalculateLinks");
        MethodDefinition remove = type.Methods.Single(m => m.Name == "RemoveNode");
        Instruction[] listMakes = constructor.Body.Instructions.Where(i => i.OpCode == OpCodes.Newobj && i.Next != null && i.Next.Operand == links).ToArray();
        Instruction[] linkMakes = calculate.Body.Instructions.Where(i => i.OpCode == OpCodes.Newobj && ((MethodReference)i.Operand).DeclaringType == link).ToArray();
        Instruction[] removeAlls = remove.Body.Instructions.Where(i => CalledName(i) == "RemoveAll" &&
            ((MethodReference)i.Operand).DeclaringType.GetElementType().FullName == "System.Collections.Generic.List`1").ToArray();
        // Every new link gets both of its fields right away: newobj; dup; <value>; stfld; dup; <value>; stfld.
        Func<Instruction, bool> initialised = make =>
        {
            var set = new HashSet<FieldDefinition>();
            Instruction at = make.Next;
            for (int i = 0; i < 2 && at != null; i++)
            {
                if (at.OpCode != OpCodes.Dup || at.Next == null || at.Next.Next == null || at.Next.Next.OpCode != OpCodes.Stfld) return false;
                set.Add(((FieldReference)at.Next.Next.Operand).Resolve());
                at = at.Next.Next.Next;
            }
            return set.Count == linkFields.Length && linkFields.All(set.Contains);
        };
        if (listMakes.Length != 1 || linkMakes.Length != 2 || removeAlls.Length != 1 || linkFields.Length != 2 || !linkMakes.All(initialised) ||
            remove.Parameters.Count != 1 || remove.Parameters[0].ParameterType != node ||
            remove.Body.Instructions.Any(i => (i.OpCode == OpCodes.Starg || i.OpCode == OpCodes.Starg_S)) ||
            link.Methods.Any(m => !m.IsConstructor) || type.NestedTypes.Any(t => t != link && t.Fields.Any(f => f.FieldType == link)) ||
            type.Fields.Any(f => f.FieldType == link))
            throw new Exception("PowerTools.Pathfinder does not have the expected shape for pooled links");
        listMakes[0].OpCode = OpCodes.Call;
        listMakes[0].Operand = CompatMethod(module, "PathPool", "TakeList", link);
        foreach (Instruction make in linkMakes)
        {
            make.OpCode = OpCodes.Call;
            make.Operand = CompatMethod(module, "PathPool", "Take", link);
        }
        removeAlls[0].OpCode = OpCodes.Call;
        removeAlls[0].Operand = CompatMethod(module, "PathPool", "RemoveAll", link);
        MethodReference recycle = CompatMethod(module, "PathPool", "Recycle", link);
        int recycles = BeforeRets(remove, () => new[] { Instruction.Create(OpCodes.Ldarg_1), Instruction.Create(OpCodes.Ldfld, links), Instruction.Create(OpCodes.Call, recycle) });
        return listMakes.Length + linkMakes.Length + removeAlls.Length + recycles;
    }

    // See Compat Regexes: the static Regex.IsMatch(input, pattern, options) in the game's code.
    static int CacheRegexes(ModuleDefinition module)
    {
        int count = 0;
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
        foreach (Instruction instruction in method.Body.Instructions)
        {
            var called = instruction.Operand as MethodReference;
            if (called == null || instruction.OpCode != OpCodes.Call || called.FullName !=
                "System.Boolean System.Text.RegularExpressions.Regex::IsMatch(System.String,System.String,System.Text.RegularExpressions.RegexOptions)")
                continue;
            instruction.Operand = CompatMethod(module, "Regexes", "IsMatch");
            count++;
        }
        return count;
    }

    // See Compat SpriteVertices: (type, method) whose Sprite.vertices arrays are only read.
    static readonly string[][] SpriteVertexMethods =
    {
        new[] { "PowerTools.Quest.GuiUtils", "CalculateGuiRectFromSprite" },
        new[] { "PowerTools.Quest.CharacterComponent", "GetTextPositionInternal" },
    };

    static int CacheSpriteVertices(ModuleDefinition module)
    {
        int count = 0;
        foreach (string[] rule in SpriteVertexMethods)
        {
            TypeDefinition type = GameType(module, rule[0]);
            if (type == null) continue;
            MethodDefinition method = type.Methods.Single(m => m.Name == rule[1] && m.HasBody);
            Instruction[] reads = method.Body.Instructions.Where(i => CalledFullName(i) == "UnityEngine.Vector2[] UnityEngine.Sprite::get_vertices()").ToArray();
            if (reads.Length != 1) throw new Exception(rule[0] + "." + rule[1] + " does not have the expected shape");
            reads[0].OpCode = OpCodes.Call;
            reads[0].Operand = CompatMethod(module, "SpriteVertices", "Get");
            count++;
        }
        return count;
    }

    // See Compat Concats: QuestCursorComponent.CalcCursorVisuals' three-string concatenation is
    // memoized.
    static int MemoizeConcats(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.QuestCursorComponent");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "CalcCursorVisuals" && m.HasBody);
        Instruction[] calls = method.Body.Instructions.Where(i =>
            CalledFullName(i) == "System.String System.String::Concat(System.String,System.String,System.String)").ToArray();
        if (calls.Length != 1) throw new Exception("QuestCursorComponent.CalcCursorVisuals does not have the expected shape");
        calls[0].Operand = CompatMethod(module, "Concats", "Concat");
        return 1;
    }

    // See Compat TextIds: SystemText.ParseIdFromText(ref string text) asks TextIds.TryGet first
    // (and then sets text only if the method had) and gives its answers to TextIds.Store.
    static int MemoizeTextIds(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.SystemText");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "ParseIdFromText" && m.HasBody);
        var code = method.Body.Instructions;
        if (method.Parameters.Count != 1 || !method.Parameters[0].ParameterType.IsByReference ||
            ((ByReferenceType)method.Parameters[0].ParameterType).ElementType.FullName != "System.String" ||
            method.ReturnType.FullName != "System.Int32" || method.Body.HasExceptionHandlers || BranchTargets(method.Body).Contains(code[0]) ||
            code.Any(i => i.OpCode == OpCodes.Starg || i.OpCode == OpCodes.Starg_S || i.OpCode == OpCodes.Ldarga || i.OpCode == OpCodes.Ldarga_S) ||
            code.Count(i => i.OpCode == OpCodes.Stind_Ref) != 1 || !code.Any(i => CalledName(i) == "Substring"))
            throw new Exception("SystemText.ParseIdFromText does not have the expected shape");
        var input = new VariableDefinition(module.TypeSystem.String);
        var id = new VariableDefinition(module.TypeSystem.Int32);
        var changed = new VariableDefinition(module.TypeSystem.Boolean);
        var rest = new VariableDefinition(module.TypeSystem.String);
        foreach (VariableDefinition variable in new[] { input, id, changed, rest }) method.Body.Variables.Add(variable);
        method.Body.InitLocals = true;
        ILProcessor il = method.Body.GetILProcessor();
        StoreAnswers(method, CompatMethod(module, "TextIds", "Store"),
                     () => new[] { il.Create(OpCodes.Ldloc, input), il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldind_Ref) });
        Instruction original = code[0];
        Instruction answer = il.Create(OpCodes.Ldloc, id);
        Prepend(method, il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldind_Ref), il.Create(OpCodes.Stloc, input),
                il.Create(OpCodes.Ldloc, input), il.Create(OpCodes.Ldloca, id), il.Create(OpCodes.Ldloca, changed),
                il.Create(OpCodes.Ldloca, rest), il.Create(OpCodes.Call, CompatMethod(module, "TextIds", "TryGet")),
                il.Create(OpCodes.Brfalse, original), il.Create(OpCodes.Ldloc, changed), il.Create(OpCodes.Brfalse, answer),
                il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldloc, rest), il.Create(OpCodes.Stind_Ref), answer, il.Create(OpCodes.Ret));
        return 1;
    }

    // A copy of a method under another name, with extra parameters appended.
    static MethodDefinition CloneMethod(MethodDefinition source, string name, params ParameterDefinition[] extra)
    {
        var clone = new MethodDefinition(name, source.Attributes & ~(MethodAttributes.Virtual | MethodAttributes.NewSlot | MethodAttributes.Final),
                                         source.ReturnType);
        clone.HasThis = source.HasThis;
        var parameters = new Dictionary<ParameterDefinition, ParameterDefinition>();
        foreach (ParameterDefinition parameter in source.Parameters)
        {
            var copy = new ParameterDefinition(parameter.Name, parameter.Attributes, parameter.ParameterType);
            clone.Parameters.Add(copy);
            parameters[parameter] = copy;
        }
        foreach (ParameterDefinition parameter in extra) clone.Parameters.Add(parameter);
        var variables = new Dictionary<VariableDefinition, VariableDefinition>();
        foreach (VariableDefinition variable in source.Body.Variables)
        {
            var copy = new VariableDefinition(variable.VariableType);
            clone.Body.Variables.Add(copy);
            variables[variable] = copy;
        }
        clone.Body.InitLocals = source.Body.InitLocals;
        var copies = new Dictionary<Instruction, Instruction>();
        foreach (Instruction original in source.Body.Instructions)
        {
            Instruction copy = Instruction.Create(OpCodes.Nop);
            copy.OpCode = original.OpCode;
            copy.Operand = original.Operand is VariableDefinition ? variables[(VariableDefinition)original.Operand]
                : original.Operand is ParameterDefinition && parameters.ContainsKey((ParameterDefinition)original.Operand)
                    ? parameters[(ParameterDefinition)original.Operand] : original.Operand;
            copies[original] = copy;
            clone.Body.Instructions.Add(copy);
        }
        foreach (Instruction copy in clone.Body.Instructions)
        {
            if (copy.Operand is Instruction) copy.Operand = copies[(Instruction)copy.Operand];
            else if (copy.Operand is Instruction[]) copy.Operand = ((Instruction[])copy.Operand).Select(t => copies[t]).ToArray();
        }
        foreach (ExceptionHandler handler in source.Body.ExceptionHandlers)
            clone.Body.ExceptionHandlers.Add(new ExceptionHandler(handler.HandlerType)
            {
                CatchType = handler.CatchType,
                TryStart = handler.TryStart == null ? null : copies[handler.TryStart],
                TryEnd = handler.TryEnd == null ? null : copies[handler.TryEnd],
                HandlerStart = handler.HandlerStart == null ? null : copies[handler.HandlerStart],
                HandlerEnd = handler.HandlerEnd == null ? null : copies[handler.HandlerEnd],
                FilterStart = handler.FilterStart == null ? null : copies[handler.FilterStart],
            });
        source.DeclaringType.Methods.Add(clone);
        return clone;
    }

    // SystemInput.UpdateInput replaces its list of selectable things every frame with a new one
    // from PowerQuest.GetAllClickables(true) (about 1.2 KB a frame in CemeteryFront: the list and
    // its growth). The old list is referenced only by that field (ControllerHud asks
    // GetControllerClickables for it each frame and keeps it in a local), and GetAllClickables
    // reads nothing of SystemInput, so the call becomes GetAllClickablesInto(true,
    // m_controllerClickables): GetAllClickables' own body, filling that list after emptying it
    // in place of a new one. The same items in the same order, and the field holds a list with
    // them, as before. -compat-no-reuse-clickables.
    static int ReuseClickables(ModuleDefinition module)
    {
        TypeDefinition quest = GameType(module, "PowerTools.Quest.PowerQuest"), input = GameType(module, "SystemInput");
        if (quest == null || input == null) return 0;
        MethodDefinition all = quest.Methods.Single(m => m.Name == "GetAllClickables" && m.Parameters.Count == 1);
        FieldDefinition field = input.Fields.Single(f => f.Name == "m_controllerClickables");
        MethodDefinition update = input.Methods.Single(m => m.Name == "UpdateInput" && m.HasBody);
        Instruction[] makes = all.Body.Instructions.Where(i => i.OpCode == OpCodes.Newobj &&
            ((MethodReference)i.Operand).DeclaringType.FullName == all.ReturnType.FullName && ((MethodReference)i.Operand).Parameters.Count == 0).ToArray();
        Instruction[] calls = update.Body.Instructions.Where(i => i.Operand == all).ToArray();
        if (makes.Length != 1 || calls.Length != 1 || calls[0].Next == null || calls[0].Next.OpCode != OpCodes.Stfld ||
            calls[0].Next.Operand != field || field.FieldType.FullName != all.ReturnType.FullName ||
            all.Body.Instructions.Any(i => i.Operand is FieldReference && ((FieldReference)i.Operand).DeclaringType == input))
            throw new Exception("PowerQuest.GetAllClickables / SystemInput.UpdateInput do not have the expected shape");
        var into = new ParameterDefinition("into", ParameterAttributes.None, all.ReturnType);
        MethodDefinition fill = CloneMethod(all, "GetAllClickablesInto", into);
        // The new list: the given one, emptied.
        Instruction make = fill.Body.Instructions[all.Body.Instructions.IndexOf(makes[0])];
        ILProcessor il = fill.Body.GetILProcessor();
        MethodReference clear = module.ImportReference(new MethodReference("Clear", module.TypeSystem.Void, all.ReturnType) { HasThis = true });
        make.OpCode = OpCodes.Ldarg;
        make.Operand = into;
        InsertAfter(il, make, il.Create(OpCodes.Dup), il.Create(OpCodes.Callvirt, clear));
        // Off, or no list given: the original.
        Instruction start = fill.Body.Instructions[0];
        Instruction original = il.Create(OpCodes.Ldarg_0);
        Prepend(fill, il.Create(OpCodes.Call, CompatMethod(module, "Allocs", "get_ReuseClickables")), il.Create(OpCodes.Brfalse, original),
                il.Create(OpCodes.Ldarg, into), il.Create(OpCodes.Brtrue, start), original, il.Create(OpCodes.Ldarg_1),
                il.Create(OpCodes.Call, all), il.Create(OpCodes.Ret));
        // SystemInput.UpdateInput passes the field's list.
        ILProcessor caller = update.Body.GetILProcessor();
        InsertBeforeRetarget(update, calls[0], caller.Create(OpCodes.Ldarg_0), caller.Create(OpCodes.Ldfld, field));
        calls[0].Operand = fill;
        return 1;
    }

    // See Compat WrapMemo: TextWrapper.WrapTextMinimiseWidth asks WrapMemo.TryGet first and
    // gives its result to WrapMemo.Store (with the width it was called with).
    static int MemoizeWrapText(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.TextWrapper");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "WrapTextMinimiseWidth" && m.HasBody);
        var code = method.Body.Instructions;
        if (method.Parameters.Count != 3 || method.ReturnType.FullName != "System.String" ||
            method.Parameters[0].ParameterType.FullName != "System.String" || method.Parameters[1].ParameterType.FullName != "System.Single" ||
            method.Parameters[2].ParameterType.FullName != "System.Single" || method.Body.HasExceptionHandlers ||
            code.Any(i => (i.OpCode == OpCodes.Starg || i.OpCode == OpCodes.Starg_S) && i.Operand != method.Parameters[1]) ||
            code.Any(i => i.OpCode == OpCodes.Ldarga || i.OpCode == OpCodes.Ldarga_S) ||
            !code.Any(i => CalledName(i) == "WrapTextNicerInternal") ||
            BranchTargets(method.Body).Contains(code[0]))
            throw new Exception("TextWrapper.WrapTextMinimiseWidth does not have the expected shape");
        var width = new VariableDefinition(module.TypeSystem.Single);
        method.Body.Variables.Add(width);
        ILProcessor il = method.Body.GetILProcessor();
        StoreAnswers(method, CompatMethod(module, "WrapMemo", "Store"), () => new[]
        {
            il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldloc, width), il.Create(OpCodes.Ldarg_3),
        });
        ReturnIfKnown(method, code[0], CompatMethod(module, "WrapMemo", "TryGet"), il.Create(OpCodes.Ldarg_2), il.Create(OpCodes.Stloc, width),
                      il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldarg_2), il.Create(OpCodes.Ldarg_3));
        return 1;
    }

    // See Compat NumberText: int.ToString() of a local in GuiControllerTopics.UpdateNewItemIcon.
    static int CacheNumberText(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.GuiControllerTopics");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "UpdateNewItemIcon" && m.HasBody);
        Instruction[] calls = method.Body.Instructions.Where(i => CalledFullName(i) == "System.String System.Int32::ToString()" && i.Previous != null &&
            (i.Previous.OpCode == OpCodes.Ldloca || i.Previous.OpCode == OpCodes.Ldloca_S)).ToArray();
        if (calls.Length != 1) throw new Exception("GuiControllerTopics.UpdateNewItemIcon does not have the expected shape");
        calls[0].Previous.OpCode = OpCodes.Ldloc;
        calls[0].OpCode = OpCodes.Call;
        calls[0].Operand = CompatMethod(module, "NumberText", "Get");
        return 1;
    }
}
