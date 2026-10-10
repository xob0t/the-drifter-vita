using System;
using System.Collections.Generic;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

// The translation itself: missing types and members to DrifterCompat (naming convention in
// Compat2018.cs), attributes the target lacks, native callbacks, unused references; the IL
// helpers the passes share (inserting code, erasing an instruction, what a call names).
// Part of the assembly rewriter; see DowngradeAssemblies.cs.
partial class DowngradeAssemblies
{
    // Inserting code may put a short branch's target out of its reach.
    static void WidenBranches(MethodBody body)
    {
        var longForms = new Dictionary<Code, OpCode>
        {
            { Code.Br_S, OpCodes.Br }, { Code.Brfalse_S, OpCodes.Brfalse }, { Code.Brtrue_S, OpCodes.Brtrue },
            { Code.Beq_S, OpCodes.Beq }, { Code.Bne_Un_S, OpCodes.Bne_Un }, { Code.Bge_S, OpCodes.Bge },
            { Code.Bge_Un_S, OpCodes.Bge_Un }, { Code.Bgt_S, OpCodes.Bgt }, { Code.Bgt_Un_S, OpCodes.Bgt_Un },
            { Code.Ble_S, OpCodes.Ble }, { Code.Ble_Un_S, OpCodes.Ble_Un }, { Code.Blt_S, OpCodes.Blt },
            { Code.Blt_Un_S, OpCodes.Blt_Un }, { Code.Leave_S, OpCodes.Leave },
        };
        OpCode wide;
        foreach (Instruction instruction in body.Instructions)
            if (longForms.TryGetValue(instruction.OpCode.Code, out wide)) instruction.OpCode = wide;
    }

    // Mono marshals any delegate to native code; IL2CPP only methods marked
    // [MonoPInvokeCallback]. Mark methods wrapped in delegates of native callback
    // types (those with [UnmanagedFunctionPointer]).
    static int MarkNativeCallbacks(ModuleDefinition module)
    {
        int count = 0;
        MethodDefinition attribute = target["AOT.MonoPInvokeCallbackAttribute"].Methods.First(m => m.IsConstructor && m.Parameters.Count == 1);
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods)
        {
            if (!method.HasBody) continue;
            foreach (Instruction instruction in method.Body.Instructions)
            {
                var wrapped = instruction.Operand as MethodReference;
                var construct = instruction.Next != null ? instruction.Next.Operand as MethodReference : null;
                if (instruction.OpCode != OpCodes.Ldftn || construct == null || instruction.Next.OpCode != OpCodes.Newobj) continue;
                TypeDefinition callback;
                MethodDefinition callee;
                try { callback = construct.DeclaringType.Resolve(); callee = wrapped.Resolve(); }
                catch (AssemblyResolutionException e) { skipped.Add("MarkNativeCallbacks " + method.FullName + ": " + e.Message); continue; }
                if (callback == null || callee == null || callee.Module != module || !callee.IsStatic) continue;
                if (!callback.CustomAttributes.Any(a => a.AttributeType.FullName == "System.Runtime.InteropServices.UnmanagedFunctionPointerAttribute")) continue;
                if (callee.CustomAttributes.Any(a => a.AttributeType.FullName == "AOT.MonoPInvokeCallbackAttribute")) continue;
                MethodReference constructor = module.ImportReference(attribute);
                var marker = new CustomAttribute(constructor);
                marker.ConstructorArguments.Add(new CustomAttributeArgument(constructor.Parameters[0].ParameterType, module.ImportReference(callback)));
                callee.CustomAttributes.Add(marker);
                count++;
            }
        }
        return count;
    }

    static string CompatName(string fullName)
    {
        return "DrifterCompat." + fullName.Replace('.', '_').Replace("/", "__");
    }

    static bool IsExternal(TypeReference type)
    {
        while (type.DeclaringType != null) type = type.DeclaringType;
        var scope = type.Scope as AssemblyNameReference;
        return scope != null && !shipped.Contains(scope.Name) && !renamedAssemblies.ContainsValue(scope.Name)
            && scope.Name != compatModule.Assembly.Name.Name;
    }

    static bool SetScope(ModuleDefinition module, TypeReference type, AssemblyNameDefinition assembly)
    {
        var current = type.Scope as AssemblyNameReference;
        if (current != null && current.Name == assembly.Name) return false;
        AssemblyNameReference reference = module.AssemblyReferences.FirstOrDefault(r => r.Name == assembly.Name);
        if (reference == null)
        {
            reference = new AssemblyNameReference(assembly.Name, assembly.Version) { PublicKeyToken = assembly.PublicKeyToken };
            module.AssemblyReferences.Add(reference);
        }
        type.Scope = reference;
        return true;
    }

    // Attributes whose types the target lacks only affect the editor; drop them.
    static int StripAttributes(ModuleDefinition module)
    {
        int count = 0;
        var providers = new List<ICustomAttributeProvider> { module, module.Assembly };
        foreach (TypeDefinition type in AllTypes(module.Types))
        {
            providers.Add(type);
            providers.AddRange(type.Fields);
            providers.AddRange(type.Properties);
            providers.AddRange(type.Events);
            foreach (MethodDefinition method in type.Methods)
            {
                providers.Add(method);
                providers.AddRange(method.Parameters);
            }
        }
        foreach (ICustomAttributeProvider provider in providers)
        {
            if (!provider.HasCustomAttributes) continue;
            for (int i = provider.CustomAttributes.Count - 1; i >= 0; i--)
            {
                TypeReference type = provider.CustomAttributes[i].AttributeType;
                if (IsExternal(type) && !renamedTypes.ContainsKey(type.FullName) && !target.ContainsKey(type.FullName) &&
                    !compat.ContainsKey(CompatName(type.FullName)))
                {
                    strippedTypes.Add(type.FullName);
                    provider.CustomAttributes.RemoveAt(i);
                    count++;
                }
            }
        }
        return count;
    }

    // Engine calls that go to Compat Platform: (type, method, Platform method).
    // QualitySettings.vSyncCount and Application.targetFrameRate assignments (see
    // Platform.SetVSyncCount) and Application.isFocused reads (Platform.IsFocused).
    static readonly string[][] PlatformCalls =
    {
        new[] { "UnityEngine.QualitySettings", "set_vSyncCount", "SetVSyncCount" },
        new[] { "UnityEngine.Application", "set_targetFrameRate", "SetTargetFrameRate" },
        new[] { "UnityEngine.Application", "get_isFocused", "IsFocused" },
    };

    static int RedirectMembers(ModuleDefinition module)
    {
        // Sites of each rule but the shims, for the NO MATCH check: "./", the 540, PlatformCalls'
        // rows, Type.GetType and BinaryFormatter.set_Binder.
        var sites = new int[PlatformCalls.Length + 4];
        int count = 0;
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods)
        {
            if (!method.HasBody) continue;
            foreach (Instruction instruction in method.Body.Instructions)
            {
                // "./" (the working directory) is read-only on consoles; see Compat Platform.
                if (instruction.OpCode == OpCodes.Ldstr && (string)instruction.Operand == "./")
                {
                    instruction.OpCode = OpCodes.Call;
                    instruction.Operand = CompatMethod(module, "Platform", "CurrentDirectory");
                    sites[0]++;
                    count++;
                    continue;
                }
                int platformCall = instruction.OpCode == OpCodes.Call ? Array.FindIndex(PlatformCalls, c => Calls(instruction, c[0], c[1])) : -1;
                if (platformCall >= 0)
                {
                    instruction.Operand = CompatMethod(module, "Platform", PlatformCalls[platformCall][2]);
                    sites[4 + platformCall]++;
                    count++;
                    continue;
                }
                // The game turns its CRT filter off on screens under 540 lines; see Compat
                // Platform.LowResolutionLines.
                if (instruction.OpCode == OpCodes.Ldc_R4 && (float)instruction.Operand == 540f &&
                    type.FullName == "GlobalScript" && method.Name == "UpdateNoPause")
                {
                    instruction.OpCode = OpCodes.Call;
                    instruction.Operand = CompatMethod(module, "Platform", "LowResolutionLines");
                    sites[1]++;
                    count++;
                    continue;
                }
                var field = instruction.Operand as FieldReference;
                if (field != null && IsMissing(field)) problems.Add(module.Name + ": field " + field.FullName);
                var called = instruction.Operand as MethodReference;
                if (called == null) continue;
                // Assembly-qualified names, e.g. in saves, still name the renamed assemblies;
                // see Compat TypeNames. Mono can resolve them by event, IL2CPP cannot.
                if (called.DeclaringType.FullName == "System.Type" && called.Name == "GetType" && called.Parameters.Count > 0 &&
                    called.Parameters.All(p => p.ParameterType.FullName == "System.String" || p.ParameterType.FullName == "System.Boolean"))
                {
                    instruction.Operand = module.ImportReference(compat["DrifterCompat.TypeNames"].Methods.First(m =>
                        m.Name == "GetType" && m.Parameters.Count == called.Parameters.Count));
                    sites[2]++;
                    count++;
                    continue;
                }
                if (called.DeclaringType.FullName == "System.Runtime.Serialization.Formatters.Binary.BinaryFormatter" && called.Name == "set_Binder")
                {
                    instruction.OpCode = OpCodes.Call;
                    instruction.Operand = CompatMethod(module, "TypeNames", "SetBinder");
                    sites[3]++;
                    count++;
                    continue;
                }
                var generic = called as GenericInstanceMethod;
                MethodReference element = generic != null ? generic.ElementMethod : called;
                if (!IsMissing(element)) continue;
                MethodDefinition shim = FindShim(element);
                if (shim == null) { problems.Add(module.Name + ": member " + element.FullName); continue; }
                MethodReference replacement = module.ImportReference(shim);
                if (generic != null)
                {
                    var instance = new GenericInstanceMethod(replacement);
                    foreach (TypeReference argument in generic.GenericArguments) instance.GenericArguments.Add(argument);
                    replacement = instance;
                }
                instruction.OpCode = OpCodes.Call;
                instruction.Operand = replacement;
                count++;
            }
        }
        string[] rules = { "\"./\"", "GlobalScript.UpdateNoPause 540f", "Type.GetType", "BinaryFormatter.set_Binder" };
        for (int i = 0; i < sites.Length; i++)
            Count("redirect " + (i < rules.Length ? rules[i] : PlatformCalls[i - rules.Length][0] + "." + PlatformCalls[i - rules.Length][1]), sites[i]);
        return count;
    }

    static bool IsMissing(MemberReference member)
    {
        TypeReference owner = member.DeclaringType;
        if (owner == null || owner is ArrayType) return false;
        while (owner is TypeSpecification) owner = ((TypeSpecification)owner).ElementType;
        if (!IsExternal(owner)) return false;
        TypeDefinition definition;
        // A type the target lacks was moved to its Compat shim in Rewrite (no longer external
        // then) or reported there as unresolved.
        if (!target.TryGetValue(owner.FullName, out definition)) return false;
        for (TypeDefinition current = definition; current != null; current = current.BaseType == null ? null : Lookup(current.BaseType))
        {
            var field = member as FieldReference;
            if (field != null && current.Fields.Any(f => f.Name == field.Name)) return false;
            var method = member as MethodReference;
            if (method != null && current.Methods.Any(m => m.Name == method.Name && Matches(m, method, 0))) return false;
            if (method != null && method.Name == ".ctor") break;
        }
        return true;
    }

    static TypeDefinition Lookup(TypeReference type)
    {
        TypeDefinition definition;
        return target.TryGetValue(type.FullName, out definition) ? definition : null;
    }

    static MethodDefinition FindShim(MethodReference missing)
    {
        TypeReference owner = missing.DeclaringType;
        while (owner is TypeSpecification) owner = ((TypeSpecification)owner).ElementType;
        TypeDefinition shimType;
        if (!compat.TryGetValue(CompatName(owner.FullName), out shimType)) return null;
        bool constructor = missing.Name == ".ctor";
        string name = constructor ? "ctor" : missing.Name;
        int offset = missing.HasThis && !constructor ? 1 : 0;
        foreach (MethodDefinition candidate in shimType.Methods.Where(m => m.IsStatic && m.Name == name))
        {
            // Matches first: it checks the parameter count.
            if (!Matches(candidate, missing, offset)) continue;
            if (offset == 1 && Shape(candidate.Parameters[0].ParameterType).TrimEnd('&') != Shape(owner)) continue;
            if (constructor && Shape(candidate.ReturnType) != Shape(owner)) continue;
            return candidate;
        }
        return null;
    }

    static bool Matches(MethodDefinition candidate, MethodReference reference, int offset)
    {
        if (candidate.Parameters.Count != reference.Parameters.Count + offset) return false;
        if (candidate.GenericParameters.Count != reference.GenericParameters.Count) return false;
        if (reference.Name != ".ctor" && Shape(candidate.ReturnType) != Shape(reference.ReturnType)) return false;
        for (int i = 0; i < reference.Parameters.Count; i++)
            if (Shape(candidate.Parameters[i + offset].ParameterType) != Shape(reference.Parameters[i].ParameterType)) return false;
        return true;
    }

    // Generic parameters compare by position, other types by full name.
    static string Shape(TypeReference type)
    {
        var generic = type as GenericParameter;
        if (generic != null) return (generic.Type == GenericParameterType.Method ? "!!" : "!") + generic.Position;
        if (type is ByReferenceType) return Shape(((TypeSpecification)type).ElementType) + "&";
        if (type is ArrayType) return Shape(((TypeSpecification)type).ElementType) + "[]";
        var instance = type as GenericInstanceType;
        if (instance != null) return Shape(instance.ElementType) + "<" + string.Join(",", instance.GenericArguments.Select(Shape)) + ">";
        return type.FullName;
    }

    static int RemoveUnusedReferences(ModuleDefinition module)
    {
        var used = new HashSet<string>(module.GetTypeReferences().Select(t =>
        {
            while (t.DeclaringType != null) t = t.DeclaringType;
            var scope = t.Scope as AssemblyNameReference;
            return scope == null ? null : scope.Name;
        }));
        int count = 0;
        for (int i = module.AssemblyReferences.Count - 1; i >= 0; i--)
        {
            string name = module.AssemblyReferences[i].Name;
            if (!used.Contains(name) && (name.StartsWith("UnityEngine") || name == "netstandard"))
            {
                module.AssemblyReferences.RemoveAt(i);
                count++;
            }
        }
        return count;
    }

    // ---- IL helpers ----
    // Where inserted code goes:
    // - Prepend: at a method's start, run once on entry (a loop back to the first instruction
    //   does not run it again; a try block that started there still starts there).
    // - InsertBeforeRetarget: before a statement, as part of it (branches to the statement and
    //   handler bounds there take in the inserted code); also BeforeRets.
    // - InsertBefore: before an instruction whose operands are already on the stack, such as a
    //   call's added arguments (branches to the anchor skip the inserted code).
    // - InsertAfter and CallAfter: after an instruction, with its value on the stack.

    static void Prepend(MethodDefinition method, params Instruction[] added)
    {
        InsertBefore(method.Body.GetILProcessor(), method.Body.Instructions[0], added);
    }

    // Inserts instructions, in order, before an existing one; branches to it still skip them.
    static void InsertBefore(ILProcessor il, Instruction anchor, params Instruction[] added)
    {
        foreach (Instruction instruction in added) il.InsertBefore(anchor, instruction);
    }

    // Inserts instructions, in order, after an existing one.
    static void InsertAfter(ILProcessor il, Instruction anchor, params Instruction[] added)
    {
        foreach (Instruction instruction in added)
        {
            il.InsertAfter(anchor, instruction);
            anchor = instruction;
        }
    }

    // Inserts "call target" after each instruction of a method that matches (where it leaves a
    // value, the call takes it and leaves its own); the number of calls inserted.
    static int CallAfter(MethodDefinition method, Func<Instruction, bool> match, MethodReference target)
    {
        ILProcessor il = method.Body.GetILProcessor();
        Instruction[] found = method.Body.Instructions.Where(match).ToArray();
        foreach (Instruction instruction in found) il.InsertAfter(instruction, il.Create(OpCodes.Call, target));
        return found.Length;
    }

    // Inserts code before each ret of a method (branches to a ret run it too); the number of rets.
    static int BeforeRets(MethodDefinition method, Func<Instruction[]> code)
    {
        Instruction[] rets = method.Body.Instructions.Where(i => i.OpCode == OpCodes.Ret).ToArray();
        foreach (Instruction ret in rets) InsertBeforeRetarget(method, ret, code());
        return rets.Length;
    }

    // Inserts instructions before an existing one; branches and handlers that pointed at it
    // point at the first inserted instruction instead.
    static void InsertBeforeRetarget(MethodDefinition method, Instruction anchor, params Instruction[] added)
    {
        InsertBefore(method.Body.GetILProcessor(), anchor, added);
        foreach (Instruction instruction in method.Body.Instructions)
        {
            if (instruction.Operand == anchor && !added.Contains(instruction)) instruction.Operand = added[0];
            var targets = instruction.Operand as Instruction[];
            if (targets != null)
                for (int i = 0; i < targets.Length; i++) if (targets[i] == anchor) targets[i] = added[0];
        }
        foreach (ExceptionHandler handler in method.Body.ExceptionHandlers)
        {
            if (handler.TryStart == anchor) handler.TryStart = added[0];
            if (handler.TryEnd == anchor) handler.TryEnd = added[0];
            if (handler.HandlerStart == anchor) handler.HandlerStart = added[0];
            if (handler.HandlerEnd == anchor) handler.HandlerEnd = added[0];
            if (handler.FilterStart == anchor) handler.FilterStart = added[0];
        }
    }

    // Turns an instruction into a nop; it stays where branches may point at it.
    static void Erase(Instruction instruction)
    {
        instruction.OpCode = OpCodes.Nop;
        instruction.Operand = null;
    }

    // The name or full name of the method an instruction names (a call, ldftn, ...), or "".
    static string CalledName(Instruction instruction)
    {
        var called = instruction.Operand as MethodReference;
        return called == null ? "" : called.Name;
    }

    static string CalledFullName(Instruction instruction)
    {
        var called = instruction.Operand as MethodReference;
        return called == null ? "" : called.FullName;
    }

    // Whether an instruction names the method <type>::<name>, the type by full name.
    static bool Calls(Instruction instruction, string type, string name)
    {
        var called = instruction.Operand as MethodReference;
        return called != null && called.Name == name && called.DeclaringType.FullName == type;
    }

    // The type a nested type is nested in at the top, or the type itself: rules for a type also
    // cover its lambdas, closures and iterators.
    static TypeDefinition OuterType(TypeDefinition type)
    {
        while (type.DeclaringType != null) type = type.DeclaringType;
        return type;
    }

    static IEnumerable<TypeDefinition> AllTypes(IEnumerable<TypeDefinition> types)
    {
        foreach (TypeDefinition type in types)
        {
            yield return type;
            foreach (TypeDefinition nested in AllTypes(type.NestedTypes)) yield return nested;
        }
    }
}
