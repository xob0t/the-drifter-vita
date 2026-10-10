using System;
using System.Collections.Generic;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

// Per-frame script costs: GetScript, CacheCalls, effect sprites, the rain's drop reuse, name and
// sprite lookups, Animator culling, GetComponent, transform reads and List lambdas that capture
// one value.
// Part of the assembly rewriter; see DowngradeAssemblies.cs.
partial class DowngradeAssemblies
{
    // PowerQuest.GetScript<T> (GlobalScript.Script, GuiScript<T>.Script and the like, a dozen
    // times a frame) works out the kind of script and its name from T on every call:
    // Type.ToString, Substring and up to five Type.IsSubclassOf against RoomScript<T> and the
    // like. Its start, up to the lookup by name, becomes: ask Compat ScriptKinds<T> for the kind
    // (worked out once per T from the same type definitions, prefix lengths and global script
    // name), then call the same lookup with the same name.
    // What ScriptKinds<T>.Kind() returns before ScriptKinds<T>.Compute.
    const int KindNotComputed = -2;

    static int RewriteGetScript(ModuleDefinition module)
    {
        TypeDefinition quest = GameType(module, "PowerTools.Quest.PowerQuest");
        if (quest == null) return 0;
        MethodDefinition method = quest.Methods.Single(m => m.Name == "GetScript" && m.HasGenericParameters && m.Parameters.Count == 0);
        var code = method.Body.Instructions;
        string[] lookups = { "GetRoom", "GetCharacter", "GetDialogTree", "GetInventory", "GetGui" };
        var calls = new MethodReference[lookups.Length];
        var prefixes = new int[lookups.Length];
        var definitions = new TypeReference[lookups.Length];
        Instruction end = null;
        FieldReference globalName = code.Where(i => i.OpCode == OpCodes.Ldsfld).Select(i => (FieldReference)i.Operand).First(f => f.Name == "GLOBAL_SCRIPT_NAME");
        MethodReference typeFromHandle = code.Where(i => CalledName(i) == "GetTypeFromHandle").Select(i => (MethodReference)i.Operand).First();
        TypeReference[] tokens = code.Where(i => i.OpCode == OpCodes.Ldtoken && i.Operand is GenericInstanceType).Select(i => (TypeReference)i.Operand).ToArray();
        for (int k = 0; k < lookups.Length; k++)
        {
            Instruction call = code.Single(i => i.OpCode == OpCodes.Call && CalledName(i) == lookups[k] && ((MethodReference)i.Operand).Parameters.Count == 1);
            calls[k] = (MethodReference)call.Operand;
            // ... ldloc text; ldc <prefix>; callvirt Substring; stloc text; ldarg.0; ldloc text; call <lookup>; stloc; br <end>
            Instruction substring = call;
            while (substring != null && CalledName(substring) != "Substring") substring = substring.Previous;
            int? value = substring == null || substring.Previous == null ? null : LoadedInt(substring.Previous);
            Instruction branch = call.Next == null ? null : call.Next.Next;
            if (value == null || branch == null || (branch.OpCode != OpCodes.Br && branch.OpCode != OpCodes.Br_S) || (end != null && branch.Operand != end))
                throw new Exception("PowerQuest.GetScript does not have the expected shape");
            end = (Instruction)branch.Operand;
            prefixes[k] = value.Value;
            var token = tokens.Length == lookups.Length ? tokens[k] as GenericInstanceType : null;
            if (token == null || !token.ElementType.Name.StartsWith(lookups[k].Substring(3)))
                throw new Exception("PowerQuest.GetScript does not have the expected shape");
            definitions[k] = token.ElementType;
        }
        VariableDefinition questScriptable = method.Body.Variables[1];
        if (questScriptable.VariableType.Name != "IQuestScriptable" || method.Body.HasExceptionHandlers ||
            code.Skip(code.IndexOf(end)).Any(i => i.Operand is Instruction && code.IndexOf((Instruction)i.Operand) < code.IndexOf(end)))
            throw new Exception("PowerQuest.GetScript does not have the expected shape");

        var kinds = new GenericInstanceType(module.ImportReference(compat["DrifterCompat.ScriptKinds`1"]));
        kinds.GenericArguments.Add(method.GenericParameters[0]);
        Func<string, MethodReference> member = name =>
        {
            MethodDefinition definition = compat["DrifterCompat.ScriptKinds`1"].Methods.Single(m => m.Name == name);
            var reference = new MethodReference(name, module.ImportReference(definition.ReturnType), kinds) { HasThis = false };
            foreach (ParameterDefinition parameter in definition.Parameters)
                reference.Parameters.Add(new ParameterDefinition(module.ImportReference(parameter.ParameterType)));
            return reference;
        };
        var kind = new VariableDefinition(module.TypeSystem.Int32);
        method.Body.Variables.Add(kind);
        while (code[0] != end) code.RemoveAt(0);
        ILProcessor il = method.Body.GetILProcessor();
        var added = new List<Instruction>();
        Instruction known = il.Create(OpCodes.Stloc, kind);
        added.Add(il.Create(OpCodes.Call, member("Kind")));
        added.Add(il.Create(OpCodes.Dup));
        added.Add(il.Create(OpCodes.Ldc_I4, KindNotComputed));
        added.Add(il.Create(OpCodes.Bne_Un, known));
        added.Add(il.Create(OpCodes.Pop));
        for (int k = 0; k < lookups.Length; k++)
        {
            added.Add(il.Create(OpCodes.Ldtoken, definitions[k]));
            added.Add(il.Create(OpCodes.Call, typeFromHandle));
            added.Add(il.Create(OpCodes.Ldc_I4, prefixes[k]));
        }
        added.Add(il.Create(OpCodes.Ldsfld, globalName));
        added.Add(il.Create(OpCodes.Call, member("Compute")));
        added.Add(known);
        added.Add(il.Create(OpCodes.Ldnull));
        added.Add(il.Create(OpCodes.Stloc, questScriptable));
        var cases = new Instruction[lookups.Length + 1];
        for (int k = 0; k < cases.Length; k++) cases[k] = il.Create(OpCodes.Ldarg_0);
        added.Add(il.Create(OpCodes.Ldloc, kind));
        added.Add(il.Create(OpCodes.Switch, cases));
        added.Add(il.Create(OpCodes.Br, end));
        for (int k = 0; k < lookups.Length; k++)
        {
            added.Add(cases[k]);
            added.Add(il.Create(OpCodes.Call, member("Name")));
            added.Add(il.Create(OpCodes.Call, calls[k]));
            added.Add(il.Create(OpCodes.Stloc, questScriptable));
            added.Add(il.Create(OpCodes.Br, end));
        }
        added.Add(cases[lookups.Length]);  // the global script: PowerQuest itself
        added.Add(il.Create(OpCodes.Stloc, questScriptable));
        InsertBefore(il, code[0], added.ToArray());
        return 1;
    }

    // The int an ldc.i4 instruction loads, or null for any other instruction.
    static int? LoadedInt(Instruction instruction)
    {
        Code op = instruction.OpCode.Code;
        if (op >= Code.Ldc_I4_M1 && op <= Code.Ldc_I4_8) return op - Code.Ldc_I4_0;
        if (op == Code.Ldc_I4_S) return (sbyte)instruction.Operand;
        if (op == Code.Ldc_I4) return (int)instruction.Operand;
        return null;
    }

    // The CachedCalls rules (Patches.cs), shader property names to ids (ShaderIds) and
    // List.AddRange (Lists) in the game's code.
    static int CacheCalls(ModuleDefinition module)
    {
        int count = 0;
        var perRule = new int[CachedCalls.Length];
        TypeDefinition shaderIds = compat["DrifterCompat.ShaderIds"];
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
        {
            TypeDefinition outer = OuterType(type);
            foreach (Instruction instruction in method.Body.Instructions)
            {
                var called = instruction.Operand as MethodReference;
                if (called == null || (instruction.OpCode != OpCodes.Call && instruction.OpCode != OpCodes.Callvirt && instruction.OpCode != OpCodes.Newobj))
                    continue;
                if (instruction.Previous != null && instruction.Previous.OpCode == OpCodes.Constrained) continue;
                MethodDefinition replacement = null;
                for (int r = 0; r < CachedCalls.Length; r++)
                {
                    string[] rule = CachedCalls[r];
                    // A type's rules also cover the types nested in it (lambdas, closures, iterators).
                    if ((rule[0] != "*" && rule[0] != type.FullName && rule[0] != outer.FullName) ||
                        (rule[1] != "*" && rule[1] != method.Name) || rule[2] != called.FullName)
                        continue;
                    replacement = compat["DrifterCompat." + rule[3]].Methods.Single(m => m.Name == rule[4] &&
                        m.Parameters.Count == called.Parameters.Count + (called.HasThis && instruction.OpCode != OpCodes.Newobj ? 1 : 0));
                    perRule[r]++;
                    break;
                }
                if (replacement == null && instruction.OpCode != OpCodes.Newobj && ShaderIdTypes.Contains(called.DeclaringType.FullName) &&
                    !ShaderIdExcludedTypes.Contains(outer.FullName) && called.HasThis &&
                    called.Parameters.Count > 0 && called.Parameters[0].ParameterType.FullName == "System.String")
                    replacement = shaderIds.Methods.FirstOrDefault(m => m.Name == called.Name &&
                        m.Parameters.Count == called.Parameters.Count + 1 &&
                        m.Parameters[0].ParameterType.FullName == called.DeclaringType.FullName &&
                        Shape(m.ReturnType) == Shape(called.ReturnType) &&
                        called.Parameters.Select((p, i) => Shape(p.ParameterType) == Shape(m.Parameters[i + 1].ParameterType)).All(same => same));
                if (replacement == null && called.Name == "AddRange" && called.Parameters.Count == 1 && called.DeclaringType is GenericInstanceType &&
                    called.DeclaringType.GetElementType().FullName == "System.Collections.Generic.List`1")
                {
                    // list.AddRange(source.ToArray()) with source a List: the copy is dropped.
                    Instruction copy = instruction.Previous;
                    var copied = copy == null ? null : copy.Operand as MethodReference;
                    bool ofCopy = copied != null && copy.OpCode == OpCodes.Callvirt && copied.Name == "ToArray" && copied.Parameters.Count == 0 &&
                        copied.DeclaringType is GenericInstanceType && copied.DeclaringType.GetElementType().FullName == "System.Collections.Generic.List`1" &&
                        !method.Body.Instructions.Any(i => i.Operand == instruction) && !method.Body.ExceptionHandlers.Any(h =>
                            h.TryStart == instruction || h.TryEnd == instruction || h.HandlerStart == instruction || h.HandlerEnd == instruction);
                    if (ofCopy) Erase(copy);
                    instruction.OpCode = OpCodes.Call;
                    instruction.Operand = CompatMethod(module, "Lists", ofCopy ? "AddRangeOfCopy" : "AddRange",
                                                       ((GenericInstanceType)called.DeclaringType).GenericArguments[0]);
                    count++;
                    continue;
                }
                if (replacement == null) continue;
                instruction.OpCode = OpCodes.Call;
                instruction.Operand = module.ImportReference(replacement);
                // A generic helper takes the type arguments of the called member's type.
                if (replacement.HasGenericParameters)
                {
                    var instance = new GenericInstanceMethod((MethodReference)instruction.Operand);
                    foreach (TypeReference argument in ((GenericInstanceType)called.DeclaringType).GenericArguments) instance.GenericArguments.Add(argument);
                    instruction.Operand = instance;
                }
                count++;
            }
        }
        for (int r = 0; r < CachedCalls.Length; r++)
            Count("CachedCalls " + CachedCalls[r][0] + "." + CachedCalls[r][1] + ": " + CachedCalls[r][2], perRule[r]);
        return count;
    }

    // PowerQuest.GetCharacterId's whole body is m_characters.FindIndex(x => x == argument) over a
    // class without its own equality (a closure and a delegate per call); it runs for every region
    // and character each frame (Region.GetCharacterOnRegion, from CharacterRimlight and the region
    // updates). The body becomes Compat ScriptCache.IndexOfReference(m_characters, argument).
    static int FastCharacterIds(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.PowerQuest");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "GetCharacterId" && m.Parameters.Count == 1);
        FieldDefinition list = type.Fields.Single(f => f.Name == "m_characters");
        if (!method.Body.Instructions.Any(i => CalledName(i) == "FindIndex") ||
            !method.Body.Instructions.Any(i => i.Operand == list) || method.Parameters[0].ParameterType.IsValueType)
            throw new Exception("PowerQuest.GetCharacterId does not have the expected shape");
        method.Body.Instructions.Clear();
        method.Body.ExceptionHandlers.Clear();
        method.Body.Variables.Clear();
        ILProcessor il = method.Body.GetILProcessor();
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldfld, list);
        il.Emit(OpCodes.Ldarg_1);
        il.Emit(OpCodes.Call, CompatMethod(module, "ScriptCache", "IndexOfReference"));
        il.Emit(OpCodes.Ret);
        return 1;
    }

    // Compat EffectSprites draws the rain's drops and the bees (see there).
    // - After every Object.Instantiate, new GameObject(name, types) and AddComponent in the
    //   game's code, Created(result) learns the renderers that could sort among them, and
    //   every sorting layer assignment goes to SetSortingLayerID/Name (it caches their layers).
    // - In Rain.Spawn, Sand.Spawn (LabSurface's sand, a copy of Rain with the same RainDrop) and
    //   Bees.SpawnBee, Instantiated(object) after the Instantiate registers the new drop or bee.
    // - In RainDrop, Bees and Bees' closures, the calls that change what a drop or bee draws
    //   (activation, layer, sprite, colour, sorting order, property block, destruction, the
    //   transform's position, scale and rotation) go to EffectSprites methods that note the
    //   value for a drop or bee and make the call for anything else. These run after
    //   CacheTransformReads, whose TransformReads.SetPosition in RainDrop.Update is wrapped the
    //   same way. RainDrop's light read (Texture2D.GetPixel) goes to EffectSprites.GetPixel.
    // - In Rain.Spawn and Sand.Spawn, Collider2D.OverlapPoint goes to EffectSprites.OverlapPoint.
    static readonly string[][] BatchSpriteCalls =
    {
        new[] { "UnityEngine.GameObject", "SetActive", "SetActive" },
        new[] { "UnityEngine.GameObject", "set_layer", "SetLayer" },
        new[] { "UnityEngine.SpriteRenderer", "set_sprite", "SetSprite" },
        new[] { "UnityEngine.SpriteRenderer", "set_color", "SetColor" },
        new[] { "UnityEngine.Renderer", "set_sortingOrder", "SetSortingOrder" },
        new[] { "UnityEngine.Renderer", "SetPropertyBlock", "SetPropertyBlock" },
        new[] { "UnityEngine.Object", "Destroy", "Destroy" },
        new[] { "UnityEngine.Transform", "set_position", "SetPosition" },
        new[] { "DrifterCompat.TransformReads", "SetPosition", "SetReadsPosition" },
        new[] { "UnityEngine.Transform", "set_localScale", "SetLocalScale" },
        new[] { "UnityEngine.Transform", "set_localEulerAngles", "SetLocalEulerAngles" },
        new[] { "UnityEngine.Transform", "set_rotation", "SetRotation" },
        new[] { "UnityEngine.Texture2D", "GetPixel", "GetPixel" },
    };

    static int BatchSprites(ModuleDefinition module)
    {
        TypeDefinition effects = compat["DrifterCompat.EffectSprites"];
        MethodReference created = CompatMethod(module, "EffectSprites", "Created"), instantiated = CompatMethod(module, "EffectSprites", "Instantiated"),
            overlapPoint = CompatMethod(module, "EffectSprites", "OverlapPoint");
        int count = 0;
        var perType = new Dictionary<string, int>();
        Action<string> tally = key => perType[key] = (perType.ContainsKey(key) ? perType[key] : 0) + 1;
        foreach (TypeDefinition type in AllTypes(module.Types))
        {
            TypeDefinition outer = OuterType(type);
            string owner = outer.Namespace == "" && (outer.Name == "Rain" || outer.Name == "Sand" || outer.Name == "RainDrop" || outer.Name == "Bees") ? outer.Name : null;
            foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
            {
                ILProcessor il = method.Body.GetILProcessor();
                foreach (Instruction instruction in method.Body.Instructions.ToArray())
                {
                    var called = instruction.Operand as MethodReference;
                    if (called == null || (instruction.OpCode != OpCodes.Call && instruction.OpCode != OpCodes.Callvirt && instruction.OpCode != OpCodes.Newobj))
                        continue;
                    // Only the GameObject constructor is a newobj; the other names are methods.
                    string declaring = called.DeclaringType.FullName;
                    bool instantiate = declaring == "UnityEngine.Object" && called.Name == "Instantiate";
                    bool creates = instantiate || (declaring == "UnityEngine.GameObject" && called.Name == "AddComponent") ||
                                   (declaring == "UnityEngine.GameObject" && instruction.OpCode == OpCodes.Newobj && called.Parameters.Count == 2);
                    if (creates)
                    {
                        Instruction after = instruction;
                        var generic = called as GenericInstanceMethod;
                        bool gameObject = generic != null ? generic.GenericArguments[0].FullName == "UnityEngine.GameObject"
                                                          : called.ReturnType.FullName == "UnityEngine.GameObject";
                        if (instantiate && gameObject && ((owner == "Rain" || owner == "Sand") && method.Name == "Spawn" || owner == "Bees" && method.Name == "SpawnBee"))
                        {
                            Instruction register = il.Create(OpCodes.Call, instantiated);
                            il.InsertAfter(after, register);
                            after = register;
                            tally(owner + ".instantiate");
                        }
                        Instruction dup = il.Create(OpCodes.Dup);
                        il.InsertAfter(after, dup);
                        il.InsertAfter(dup, il.Create(OpCodes.Call, created));
                        count++;
                        continue;
                    }
                    if (declaring == "UnityEngine.Renderer" && (called.Name == "set_sortingLayerID" || called.Name == "set_sortingLayerName"))
                    {
                        instruction.OpCode = OpCodes.Call;
                        instruction.Operand = CompatMethod(module, "EffectSprites",
                            called.Name == "set_sortingLayerID" ? "SetSortingLayerID" : "SetSortingLayerName");
                        count++;
                        continue;
                    }
                    if ((owner == "Rain" || owner == "Sand") && declaring == "UnityEngine.Collider2D" &&
                        called.Name == "OverlapPoint" && called.Parameters.Count == 1 && called.Parameters[0].ParameterType.FullName == "UnityEngine.Vector2")
                    {
                        instruction.OpCode = OpCodes.Call;
                        instruction.Operand = overlapPoint;
                        tally(owner + ".overlap");
                        count++;
                        continue;
                    }
                    if (owner == null || owner == "Rain" || owner == "Sand") continue;
                    foreach (string[] rule in BatchSpriteCalls)
                    {
                        if (declaring != rule[0] || called.Name != rule[1]) continue;
                        MethodDefinition hook = effects.Methods.Single(m => m.Name == rule[2]);
                        if (hook.Parameters.Count != called.Parameters.Count + (called.HasThis ? 1 : 0)) continue;
                        instruction.OpCode = OpCodes.Call;
                        instruction.Operand = module.ImportReference(hook);
                        tally(owner);
                        count++;
                        break;
                    }
                }
            }
        }
        if (module.GetType("RainDrop") != null &&
            (!perType.ContainsKey("RainDrop") || perType["RainDrop"] < 9 || !perType.ContainsKey("Rain.instantiate") ||
             !perType.ContainsKey("Bees") || perType["Bees"] < 8 || !perType.ContainsKey("Bees.instantiate") ||
             !perType.ContainsKey("Rain.overlap") || perType["Rain.overlap"] != 3 || !perType.ContainsKey("Sand.overlap") || perType["Sand.overlap"] != 3))
            throw new Exception("Rain, RainDrop or Bees do not have the expected shape: " +
                                string.Join(", ", perType.Select(p => p.Key + "=" + p.Value).ToArray()));
        return count;
    }

    // Rain.Spawn and Sand.Spawn(camPos, pos, newDrop) look for the first finished drop of m_drops
    // from the start of the list for every spawn without a drop (about 26 a frame in CemeteryGrave,
    // some 30 us each on the PS Vita, mostly this search). Drops only finish in RainDrop.Update,
    // which only their owner's Update calls, before that Update's own spawns; so within one Update
    // the drops a search passed stay unfinished, and the next search may start at the drop the last
    // one found (which the spawn has made unfinished, or not, if it returned early). Each type gets
    // a field __drifterReuse, set to 0 at the start of Update, and a static
    // __DrifterFirstFinished(drops, ref cursor): an indexed search from the cursor that leaves it at
    // the drop found (or at the end, returning null). Spawn starts with
    //   if (newDrop == null) newDrop = __DrifterFirstFinished(m_drops, ref __drifterReuse);
    // and its own search then only runs when no drop is finished (finding none either), so it
    // spawns the same drop as before.
    static int FastRainReuse(ModuleDefinition module)
    {
        TypeDefinition dropType = GameType(module, "RainDrop");
        if (dropType == null) return 0;
        FieldDefinition finished = dropType.Fields.Single(f => f.Name == "m_finished");
        int count = 0;
        foreach (string name in new[] { "Rain", "Sand" })
        {
            TypeDefinition type = GameType(module, name);
            if (type == null) continue;
            FieldDefinition drops = type.Fields.Single(f => f.Name == "m_drops");
            var listType = drops.FieldType as GenericInstanceType;
            MethodDefinition spawn = type.Methods.Single(m => m.Name == "Spawn" && m.Parameters.Count == 3 && m.Parameters[2].ParameterType.FullName == "RainDrop");
            MethodDefinition update = type.Methods.Single(m => m.Name == "Update" && m.Parameters.Count == 0 && m.HasBody);
            if (listType == null || listType.ElementType.FullName != "System.Collections.Generic.List`1" ||
                listType.GenericArguments[0].FullName != "RainDrop" || finished.FieldType.FullName != "System.Boolean" ||
                !spawn.Body.Instructions.Any(i => i.Operand == finished) || !spawn.Body.Instructions.Any(i => i.Operand == drops) ||
                type.Methods.Where(m => m.HasBody).Any(m => m.Body.Instructions.Any(i => i.OpCode == OpCodes.Stfld && i.Operand == drops && m.Name != ".ctor")) ||
                AllTypes(module.Types).Where(t => t != dropType).SelectMany(t => t.Methods).Where(m => m.HasBody)
                      .Any(m => m.Body.Instructions.Any(i => i.OpCode == OpCodes.Stfld && i.Operand == finished)))
                throw new Exception(name + ".Spawn does not have the expected shape");
            var cursor = new FieldDefinition("__drifterReuse", FieldAttributes.Private, module.TypeSystem.Int32);
            type.Fields.Add(cursor);
            var helper = new MethodDefinition("__DrifterFirstFinished", MethodAttributes.Private | MethodAttributes.Static | MethodAttributes.HideBySig, dropType);
            helper.Parameters.Add(new ParameterDefinition("drops", ParameterAttributes.None, listType));
            helper.Parameters.Add(new ParameterDefinition("cursor", ParameterAttributes.None, new ByReferenceType(module.TypeSystem.Int32)));
            var index = new VariableDefinition(module.TypeSystem.Int32);
            var total = new VariableDefinition(module.TypeSystem.Int32);
            var drop = new VariableDefinition(dropType);
            helper.Body.Variables.Add(index);
            helper.Body.Variables.Add(total);
            helper.Body.Variables.Add(drop);
            helper.Body.InitLocals = true;
            ILProcessor il = helper.Body.GetILProcessor();
            Instruction loop = il.Create(OpCodes.Ldarg_0), next = il.Create(OpCodes.Ldloc, index), test = il.Create(OpCodes.Ldloc, index);
            il.Append(il.Create(OpCodes.Ldarg_0));
            il.Append(il.Create(OpCodes.Callvirt, ListMethod(module, listType, "get_Count")));
            il.Append(il.Create(OpCodes.Stloc, total));
            il.Append(il.Create(OpCodes.Ldarg_1));
            il.Append(il.Create(OpCodes.Ldind_I4));
            il.Append(il.Create(OpCodes.Stloc, index));
            il.Append(il.Create(OpCodes.Br, test));
            il.Append(loop);
            il.Append(il.Create(OpCodes.Ldloc, index));
            il.Append(il.Create(OpCodes.Callvirt, ListMethod(module, listType, "get_Item")));
            il.Append(il.Create(OpCodes.Stloc, drop));
            il.Append(il.Create(OpCodes.Ldloc, drop));
            il.Append(il.Create(OpCodes.Ldfld, finished));
            il.Append(il.Create(OpCodes.Brfalse, next));
            il.Append(il.Create(OpCodes.Ldarg_1));
            il.Append(il.Create(OpCodes.Ldloc, index));
            il.Append(il.Create(OpCodes.Stind_I4));
            il.Append(il.Create(OpCodes.Ldloc, drop));
            il.Append(il.Create(OpCodes.Ret));
            il.Append(next);
            il.Append(il.Create(OpCodes.Ldc_I4_1));
            il.Append(il.Create(OpCodes.Add));
            il.Append(il.Create(OpCodes.Stloc, index));
            il.Append(test);
            il.Append(il.Create(OpCodes.Ldloc, total));
            il.Append(il.Create(OpCodes.Blt, loop));
            il.Append(il.Create(OpCodes.Ldarg_1));
            il.Append(il.Create(OpCodes.Ldloc, total));
            il.Append(il.Create(OpCodes.Stind_I4));
            il.Append(il.Create(OpCodes.Ldnull));
            il.Append(il.Create(OpCodes.Ret));
            type.Methods.Add(helper);
            ILProcessor updateIl = update.Body.GetILProcessor();
            Prepend(update, updateIl.Create(OpCodes.Ldarg_0), updateIl.Create(OpCodes.Ldc_I4_0), updateIl.Create(OpCodes.Stfld, cursor));
            ILProcessor spawnIl = spawn.Body.GetILProcessor();
            Instruction first = spawn.Body.Instructions[0];
            ParameterDefinition newDrop = spawn.Parameters[2];
            Prepend(spawn, spawnIl.Create(OpCodes.Ldarg, newDrop), spawnIl.Create(OpCodes.Brtrue, first),
                spawnIl.Create(OpCodes.Ldarg_0), spawnIl.Create(OpCodes.Ldfld, drops), spawnIl.Create(OpCodes.Ldarg_0), spawnIl.Create(OpCodes.Ldflda, cursor),
                spawnIl.Create(OpCodes.Call, helper), spawnIl.Create(OpCodes.Starg, newDrop));
            count++;
        }
        return count;
    }

    // A Compat memo's Store before each ret of a method: "<arguments> call store" (store takes
    // the answer below them and returns it).
    static void StoreAnswers(MethodDefinition method, MethodReference store, Func<Instruction[]> arguments)
    {
        BeforeRets(method, () => arguments().Concat(new[] { Instruction.Create(OpCodes.Call, store) }).ToArray());
    }

    // A Compat memo's TryGet before an instruction: "if (tryGet(<arguments>, out answer)) return
    // answer;", answer a new local of the method's return type.
    static void ReturnIfKnown(MethodDefinition method, Instruction anchor, MethodReference tryGet, params Instruction[] arguments)
    {
        var answer = new VariableDefinition(method.ReturnType);
        method.Body.Variables.Add(answer);
        method.Body.InitLocals = true;
        InsertBeforeRetarget(method, anchor, arguments.Concat(new[]
        {
            Instruction.Create(OpCodes.Ldloca, answer), Instruction.Create(OpCodes.Call, tryGet), Instruction.Create(OpCodes.Brfalse, anchor),
            Instruction.Create(OpCodes.Ldloc, answer), Instruction.Create(OpCodes.Ret),
        }).ToArray());
    }

    // A lookup memoized by its key (the instructions key() makes): it starts with
    // "if (tryGet(<key>, out answer)) return answer;" and gives every answer to store.
    static void MemoizeFind(MethodDefinition find, MethodReference tryGet, MethodReference store, Func<Instruction[]> key)
    {
        // Store first: the known answer's own ret must not store it again.
        StoreAnswers(find, store, key);
        ReturnIfKnown(find, find.Body.Instructions[0], tryGet, key());
    }

    // QuestUtils.FindScriptableFast<T>(list, name) (PowerQuest.GetGui, GetInventory,
    // GetCharacter) asks Compat Scriptables.TryGet first and gives its answer to
    // Scriptables.Store; QuestUtils.ClearScriptableCache starts with Scriptables.Clear.
    static int MemoizeScriptableLookups(ModuleDefinition module)
    {
        TypeDefinition utils = GameType(module, "PowerTools.Quest.QuestUtils");
        if (utils == null) return 0;
        MethodDefinition find = utils.Methods.Single(m => m.Name == "FindScriptableFast");
        MethodDefinition clear = utils.Methods.Single(m => m.Name == "ClearScriptableCache");
        FieldDefinition cache = utils.Fields.Single(f => f.Name == "s_cachedScriptableList");
        if (find.GenericParameters.Count != 1 || find.Parameters.Count != 2 || find.Parameters[1].ParameterType.FullName != "System.String" ||
            !clear.Body.Instructions.Any(i => i.Operand == cache) ||
            utils.Methods.Any(m => m != find && m != clear && m.HasBody && m.Body.Instructions.Any(i => i.Operand == cache && i.OpCode != OpCodes.Stsfld)))
            throw new Exception("QuestUtils.FindScriptableFast does not have the expected shape");
        GenericParameter t = find.GenericParameters[0];
        ILProcessor il = find.Body.GetILProcessor();
        MemoizeFind(find, CompatMethod(module, "Scriptables", "TryGet", t), CompatMethod(module, "Scriptables", "Store", t),
                    () => new[] { il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldarg_1) });
        Prepend(clear, Instruction.Create(OpCodes.Call, CompatMethod(module, "Scriptables", "Clear")));
        return 2;
    }

    // PowerQuest.FindSpriteInList(list, name) asks Compat SpriteLists.TryGet first (with the
    // SPRITE_NUM_POSTFIX_0 it appends) and gives its answer to SpriteLists.Store.
    static int MemoizeSpriteLookups(ModuleDefinition module)
    {
        TypeDefinition quest = GameType(module, "PowerTools.Quest.PowerQuest");
        if (quest == null) return 0;
        MethodDefinition find = quest.Methods.Single(m => m.Name == "FindSpriteInList");
        FieldDefinition postfix = quest.Fields.Single(f => f.Name == "SPRITE_NUM_POSTFIX_0");
        if (!find.IsStatic || find.Parameters.Count != 2 || find.ReturnType.FullName != "UnityEngine.Sprite" ||
            find.Parameters[0].ParameterType.FullName != "System.Collections.Generic.List`1<UnityEngine.Sprite>" ||
            find.Parameters[1].ParameterType.FullName != "System.String" || !postfix.IsStatic || !postfix.IsInitOnly ||
            !find.Body.Instructions.Any(i => i.Operand == postfix))
            throw new Exception("PowerQuest.FindSpriteInList does not have the expected shape");
        ILProcessor il = find.Body.GetILProcessor();
        MemoizeFind(find, CompatMethod(module, "SpriteLists", "TryGet"), CompatMethod(module, "SpriteLists", "Store"),
                    () => new[] { il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldsfld, postfix) });
        return 1;
    }

    // Compat AnimatorCulling (culls the Animators of hidden, paused SpriteAnims) learns of every
    // change that could make the engine's animation step do something with them:
    // - SpriteAnim.Awake ends with AnimatorCulling.Register(m_animator);
    // - SpriteAnim's Play, Stop, Pause, Resume, SetSpeed and SetNormalizedTime start with
    //   AnimatorCulling.Wake(m_animator);
    // - elsewhere in the game's code every Animator obtained from a call (SpriteAnim.Animator,
    //   GetComponent<Animator>(), ...) or a field passes through AnimatorCulling.Woken;
    // - Behaviour.enabled assignments go to AnimatorCulling.SetEnabled (wakes an Animator).
    static readonly string[] SpriteAnimWakers = { "Play", "Stop", "Pause", "Resume", "SetSpeed", "SetNormalizedTime" };

    static int HookAnimatorCulling(ModuleDefinition module)
    {
        Func<string, MethodReference> helper = name => CompatMethod(module, "AnimatorCulling", name);
        int count = 0;
        TypeDefinition spriteAnim = GameType(module, "PowerTools.SpriteAnim");
        if (spriteAnim != null)
        {
            FieldDefinition animator = spriteAnim.Fields.Single(f => f.Name == "m_animator" && f.FieldType.FullName == "UnityEngine.Animator");
            MethodDefinition awake = spriteAnim.Methods.Single(m => m.Name == "Awake" && !m.HasParameters && m.HasBody);
            ILProcessor il = awake.Body.GetILProcessor();
            count += BeforeRets(awake, () => new[] { il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldfld, animator), il.Create(OpCodes.Call, helper("Register")) });
            foreach (string name in SpriteAnimWakers)
            {
                MethodDefinition[] methods = spriteAnim.Methods.Where(m => m.Name == name && m.HasBody && !m.IsStatic).ToArray();
                if (methods.Length == 0) throw new Exception("SpriteAnim." + name + " is missing");
                foreach (MethodDefinition method in methods)
                {
                    ILProcessor body = method.Body.GetILProcessor();
                    Prepend(method, body.Create(OpCodes.Ldarg_0), body.Create(OpCodes.Ldfld, animator), body.Create(OpCodes.Call, helper("Wake")));
                    count++;
                }
            }
        }
        MethodReference woken = helper("Woken"), setEnabled = helper("SetEnabled");
        foreach (TypeDefinition type in AllTypes(module.Types))
        {
            bool inSpriteAnim = OuterType(type).FullName == "PowerTools.SpriteAnim";
            foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
            {
                ILProcessor il = method.Body.GetILProcessor();
                foreach (Instruction instruction in method.Body.Instructions.ToArray())
                {
                    var called = instruction.Operand as MethodReference;
                    var field = instruction.Operand as FieldReference;
                    bool isCall = called != null && (instruction.OpCode == OpCodes.Call || instruction.OpCode == OpCodes.Callvirt);
                    if (isCall && called.Name == "set_enabled" && called.DeclaringType.FullName == "UnityEngine.Behaviour" &&
                        (instruction.Previous == null || instruction.Previous.OpCode != OpCodes.Constrained))
                    {
                        instruction.OpCode = OpCodes.Call;
                        instruction.Operand = setEnabled;
                        count++;
                        continue;
                    }
                    if (inSpriteAnim) continue;
                    bool returnsAnimator = isCall && ReturnedType(called).FullName == "UnityEngine.Animator";
                    bool loadsAnimator = field != null && instruction.OpCode == OpCodes.Ldfld && field.FieldType.FullName == "UnityEngine.Animator";
                    if (!returnsAnimator && !loadsAnimator) continue;
                    if (isCall && called.DeclaringType.FullName == "DrifterCompat.AnimatorCulling") continue;
                    il.InsertAfter(instruction, il.Create(OpCodes.Call, woken));
                    count++;
                }
            }
        }
        return count;
    }

    // The type a call leaves on the stack: a method's generic parameter resolved to its argument.
    static TypeReference ReturnedType(MethodReference called)
    {
        var generic = called.ReturnType as GenericParameter;
        var instance = called as GenericInstanceMethod;
        if (generic != null && instance != null && generic.Type == GenericParameterType.Method && generic.Position < instance.GenericArguments.Count)
            return instance.GenericArguments[generic.Position];
        return called.ReturnType;
    }

    // QuestUtils.FindByName<T>(list, name): the same memo as FindSpriteInList's (Compat
    // NamedLists): TryGet first, every answer through Store.
    static int MemoizeNameLookups(ModuleDefinition module)
    {
        TypeDefinition utils = GameType(module, "PowerTools.Quest.QuestUtils");
        if (utils == null) return 0;
        MethodDefinition find = utils.Methods.Single(m => m.Name == "FindByName");
        if (!find.IsStatic || find.GenericParameters.Count != 1 || find.Parameters.Count != 2 ||
            find.ReturnType != find.GenericParameters[0] || find.Parameters[1].ParameterType.FullName != "System.String" ||
            !(find.Parameters[0].ParameterType is GenericInstanceType) ||
            ((GenericInstanceType)find.Parameters[0].ParameterType).ElementType.FullName != "System.Collections.Generic.List`1" ||
            ((GenericInstanceType)find.Parameters[0].ParameterType).GenericArguments[0] != find.GenericParameters[0])
            throw new Exception("QuestUtils.FindByName does not have the expected shape");
        ILProcessor il = find.Body.GetILProcessor();
        MemoizeFind(find, CompatMethod(module, "NamedLists", "TryGet", find.ReturnType), CompatMethod(module, "NamedLists", "Store", find.ReturnType),
                    () => new[] { il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldarg_1) });
        return 1;
    }

    // Component.GetComponent<T>() and GameObject.GetComponent<T>() in the game's code go to
    // Compat Components.Get<T>/GetFrom<T>, which return the component the engine returned last
    // time for the same receiver and T while it is alive (see there).
    static int CacheGetComponent(ModuleDefinition module)
    {
        MethodReference get = CompatMethod(module, "Components", "Get"), getFrom = CompatMethod(module, "Components", "GetFrom");
        int count = 0;
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
        foreach (Instruction instruction in method.Body.Instructions)
        {
            var called = instruction.Operand as GenericInstanceMethod;
            if (called == null || (instruction.OpCode != OpCodes.Call && instruction.OpCode != OpCodes.Callvirt) ||
                called.Name != "GetComponent" || called.Parameters.Count != 0 || called.GenericArguments.Count != 1 ||
                (instruction.Previous != null && instruction.Previous.OpCode == OpCodes.Constrained))
                continue;
            string owner = called.DeclaringType.FullName;
            if (owner != "UnityEngine.Component" && owner != "UnityEngine.GameObject") continue;
            var replacement = new GenericInstanceMethod(owner == "UnityEngine.Component" ? get : getFrom);
            replacement.GenericArguments.Add(called.GenericArguments[0]);
            instruction.OpCode = OpCodes.Call;
            instruction.Operand = replacement;
            count++;
        }
        return count;
    }

    // RainDrop.Update runs for each of about 600 drops a frame in the cemetery rooms (from
    // Rain.Update): it reads its Transform's position twice, sets it, then reads it up to five
    // more times (Transform.position is an engine call each time; 3.9 ms of Rain.Update on the
    // PS Vita). In it Transform.position reads go to Compat TransformReads.Position, which
    // returns the value the last read of the same Transform got, and writes to
    // TransformReads.SetPosition, which sets it and forgets that value (the next read asks the
    // engine, as the position read back can differ from the value set under a parent). The
    // method starts with TransformReads.Forget(), and any other call to a Transform method other
    // than a getter, or to the game's own code (this module or the other game assembly, under its
    // new name by then), is followed by one, so a remembered value never outlives something that
    // could move the Transform.
    static int CacheTransformReads(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "RainDrop");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "Update" && m.HasBody);
        if (method.Body.HasExceptionHandlers) throw new Exception("RainDrop.Update does not have the expected shape");
        MethodReference position = CompatMethod(module, "TransformReads", "Position"), setPosition = CompatMethod(module, "TransformReads", "SetPosition"),
            forget = CompatMethod(module, "TransformReads", "Forget");
        ILProcessor il = method.Body.GetILProcessor();
        int replaced = 0;
        foreach (Instruction instruction in method.Body.Instructions.ToArray())
        {
            var called = instruction.Operand as MethodReference;
            if (called == null || (instruction.OpCode != OpCodes.Call && instruction.OpCode != OpCodes.Callvirt && instruction.OpCode != OpCodes.Newobj))
                continue;
            bool onTransform = called.DeclaringType.FullName == "UnityEngine.Transform";
            var scope = called.DeclaringType.Scope as AssemblyNameReference;
            MethodReference replacement = called.FullName == "UnityEngine.Vector3 UnityEngine.Transform::get_position()" ? position
                : called.FullName == "System.Void UnityEngine.Transform::set_position(UnityEngine.Vector3)" ? setPosition : null;
            if (replacement != null)
            {
                instruction.OpCode = OpCodes.Call;
                instruction.Operand = replacement;
                replaced++;
            }
            else if ((onTransform && !called.Name.StartsWith("get_")) || called.DeclaringType.Scope == module ||
                     (scope != null && renamedAssemblies.ContainsValue(scope.Name)))
                il.InsertAfter(instruction, il.Create(OpCodes.Call, forget));
        }
        if (replaced < 3) throw new Exception("RainDrop.Update does not have the expected shape");
        Prepend(method, il.Create(OpCodes.Call, forget));
        return replaced;
    }

    // List<T>.Find, Exists, FindIndex, FindAll and RemoveAll with a lambda capturing one value,
    // such as m_activeAudio.Find(item => item.handle == source) in SystemAudio.SetVolume, make a
    // closure and a delegate on every call: SystemAudio does it several times a frame for each
    // playing sound, as do Character.HasInventory and Pathfinder.RemoveNode. Where a method's
    // closure is used for nothing else (its fields only set and read in the method, its lambdas
    // only passed straight to those List methods and reading one field), the fields become
    // locals, each lambda a static method taking the item and that field's value, and the call
    // goes to Compat Lists with a delegate to the static method made once.
    static readonly string[] KeyedListMethods = { "Find", "Exists", "FindIndex", "FindAll", "RemoveAll" };

    static int InlineKeyClosures(ModuleDefinition module)
    {
        int count = 0;
        foreach (var pair in PrivateClosures(module))
            if (InlineKeyClosure(module, pair.Item2, pair.Item1)) count++;
        return count;
    }

    // The local an instruction stores (store) or loads (not ldloca), or null.
    static VariableDefinition LocalOf(MethodDefinition method, Instruction instruction, bool store)
    {
        Code op = instruction.OpCode.Code;
        if (op >= Code.Ldloc_0 && op <= Code.Ldloc_3) return store ? null : method.Body.Variables[op - Code.Ldloc_0];
        if (op >= Code.Stloc_0 && op <= Code.Stloc_3) return store ? method.Body.Variables[op - Code.Stloc_0] : null;
        if (op == Code.Ldloc || op == Code.Ldloc_S) return store ? null : (VariableDefinition)instruction.Operand;
        if (op == Code.Stloc || op == Code.Stloc_S) return store ? (VariableDefinition)instruction.Operand : null;
        return null;
    }

    // Values an instruction pops and pushes, or null for control flow and the unusual.
    static int[] StackEffect(Instruction instruction)
    {
        OpCode op = instruction.OpCode;
        if (op.FlowControl != FlowControl.Next && op.FlowControl != FlowControl.Call) return null;
        var called = instruction.Operand as MethodReference;
        if (op.StackBehaviourPop == StackBehaviour.Varpop || op.StackBehaviourPush == StackBehaviour.Varpush)
        {
            if (called == null || op.Code == Code.Calli) return null;
            bool construct = op.Code == Code.Newobj;
            int pops = called.Parameters.Count + (called.HasThis && !construct ? 1 : 0);
            int pushes = construct || called.ReturnType.FullName != "System.Void" ? 1 : 0;
            return new[] { pops, pushes };
        }
        int pop;
        switch (op.StackBehaviourPop)
        {
            case StackBehaviour.Pop0: pop = 0; break;
            case StackBehaviour.Pop1: case StackBehaviour.Popi: case StackBehaviour.Popref: pop = 1; break;
            case StackBehaviour.Pop1_pop1: case StackBehaviour.Popi_pop1: case StackBehaviour.Popi_popi: case StackBehaviour.Popi_popi8:
            case StackBehaviour.Popi_popr4: case StackBehaviour.Popi_popr8: case StackBehaviour.Popref_pop1: case StackBehaviour.Popref_popi: pop = 2; break;
            case StackBehaviour.Popi_popi_popi: case StackBehaviour.Popref_popi_popi: case StackBehaviour.Popref_popi_popi8:
            case StackBehaviour.Popref_popi_popr4: case StackBehaviour.Popref_popi_popr8: case StackBehaviour.Popref_popi_popref: pop = 3; break;
            default: return null;
        }
        int push;
        switch (op.StackBehaviourPush)
        {
            case StackBehaviour.Push0: push = 0; break;
            case StackBehaviour.Push1: case StackBehaviour.Pushi: case StackBehaviour.Pushi8: case StackBehaviour.Pushr4:
            case StackBehaviour.Pushr8: case StackBehaviour.Pushref: push = 1; break;
            case StackBehaviour.Push1_push1: push = 2; break;
            default: return null;
        }
        return new[] { pop, push };
    }

    static bool InlineKeyClosure(ModuleDefinition module, MethodDefinition method, TypeDefinition closure)
    {
        var body = method.Body;
        var code = body.Instructions;
        if (code.Any(i => i.Operand is VariableDefinition && ((VariableDefinition)i.Operand).VariableType == closure &&
                          (i.OpCode.Code == Code.Ldloca || i.OpCode.Code == Code.Ldloca_S)))
            return false;
        VariableDefinition[] locals = body.Variables.Where(v => v.VariableType == closure).ToArray();
        if (locals.Length != 1 || closure.Fields.Any(f => f.IsStatic) ||
            closure.Methods.Any(m => m.IsConstructor && (m.Parameters.Count > 0 || m.IsStatic)))
            return false;
        VariableDefinition local = locals[0];
        HashSet<Instruction> targets = BranchTargets(body);
        // Classify every use of the closure local.
        var creations = new List<Instruction>();  // the stloc after newobj
        var inits = new List<Tuple<Instruction, Instruction>>();  // ldloc, stfld
        var reads = new List<Instruction>();  // ldloc followed by ldfld
        var calls = new List<Instruction>();  // ldloc, ldftn, newobj, callvirt
        var lambdas = new Dictionary<MethodDefinition, FieldDefinition>();
        foreach (Instruction instruction in code)
        {
            if (LocalOf(method, instruction, true) == local)
            {
                var create = instruction.Previous == null ? null : instruction.Previous.Operand as MethodReference;
                if (create == null || instruction.Previous.OpCode != OpCodes.Newobj || create.Resolve() == null || create.Resolve().DeclaringType != closure ||
                    targets.Contains(instruction))
                    return false;
                creations.Add(instruction);
                continue;
            }
            if (LocalOf(method, instruction, false) != local) continue;
            Instruction next = instruction.Next;
            if (next == null || targets.Contains(next)) return false;
            var field = next.Operand as FieldReference;
            if (next.OpCode == OpCodes.Ldfld && field != null && field.DeclaringType == closure)
            {
                reads.Add(instruction);
                continue;
            }
            if (next.OpCode == OpCodes.Ldftn)
            {
                MethodDefinition lambda = ((MethodReference)next.Operand).Resolve();
                Instruction make = next.Next, call = make == null ? null : make.Next;
                if (lambda == null || lambda.DeclaringType != closure || call == null || targets.Contains(make) || targets.Contains(call)) return false;
                var predicate = make.Operand as MethodReference;
                var listCall = call.Operand as MethodReference;
                if (make.OpCode != OpCodes.Newobj || predicate == null || !(predicate.DeclaringType is GenericInstanceType) ||
                    predicate.DeclaringType.GetElementType().FullName != "System.Predicate`1" ||
                    call.OpCode != OpCodes.Callvirt || listCall == null || !KeyedListMethods.Contains(listCall.Name) ||
                    listCall.Parameters.Count != 1 || !(listCall.DeclaringType is GenericInstanceType) ||
                    listCall.DeclaringType.GetElementType().FullName != "System.Collections.Generic.List`1")
                    return false;
                FieldDefinition key = LambdaKey(lambda, closure);
                if (key == null) return false;
                lambdas[lambda] = key;
                calls.Add(instruction);
                continue;
            }
            // A field initialisation: <ldloc closure> <value> <stfld>, the value without branches.
            Instruction store = FieldStoreAfter(instruction, targets, closure, at => LocalOf(method, at, false) == local || LocalOf(method, at, true) == local);
            if (store == null) return false;
            inits.Add(Tuple.Create(instruction, store));
        }
        if (calls.Count == 0) return false;
        // Every lambda of the closure must be one of those passed to the List methods.
        if (closure.Methods.Any(m => !m.IsConstructor && !lambdas.ContainsKey(m))) return false;

        var fieldLocals = new Dictionary<FieldDefinition, VariableDefinition>();
        foreach (FieldDefinition field in closure.Fields)
        {
            var variable = new VariableDefinition(field.FieldType);
            body.Variables.Add(variable);
            fieldLocals[field] = variable;
        }
        Func<FieldReference, VariableDefinition> localFor = f => fieldLocals[f.Resolve()];
        foreach (Instruction stloc in creations)
        {
            Erase(stloc.Previous);
            Erase(stloc);
        }
        foreach (var init in inits)
        {
            Erase(init.Item1);
            init.Item2.Operand = localFor((FieldReference)init.Item2.Operand);
            init.Item2.OpCode = OpCodes.Stloc;
        }
        foreach (Instruction read in reads)
        {
            Erase(read);
            read.Next.Operand = localFor((FieldReference)read.Next.Operand);
            read.Next.OpCode = OpCodes.Ldloc;
        }
        var getters = new Dictionary<MethodDefinition, MethodDefinition>();
        foreach (Instruction load in calls)
        {
            Instruction ftn = load.Next, make = ftn.Next, call = make.Next;
            MethodDefinition lambda = ((MethodReference)ftn.Operand).Resolve();
            FieldDefinition key = lambdas[lambda];
            var listCall = (MethodReference)call.Operand;
            TypeReference item = ((GenericInstanceType)listCall.DeclaringType).GenericArguments[0];
            MethodDefinition getter;
            if (!getters.TryGetValue(lambda, out getter)) getters[lambda] = getter = StaticMatcher(module, method.DeclaringType, lambda, key, item);
            load.Operand = fieldLocals[key];
            load.OpCode = OpCodes.Ldloc;
            ftn.OpCode = OpCodes.Call;
            ftn.Operand = getter;
            Erase(make);
            var helper = new GenericInstanceMethod(module.ImportReference(compat["DrifterCompat.Lists"].Methods.Single(m =>
                m.Name == listCall.Name && m.GenericParameters.Count == 2)));
            helper.GenericArguments.Add(item);
            helper.GenericArguments.Add(key.FieldType);
            call.OpCode = OpCodes.Call;
            call.Operand = helper;
        }
        return true;
    }

    // The closure field a lambda compares its argument with: the lambda takes one argument
    // and uses its closure only to read that one field, without exception handlers.
    static FieldDefinition LambdaKey(MethodDefinition lambda, TypeDefinition closure)
    {
        if (lambda.IsStatic || lambda.Parameters.Count != 1 || lambda.ReturnType.FullName != "System.Boolean" ||
            !lambda.HasBody || lambda.Body.HasExceptionHandlers || lambda.HasGenericParameters)
            return null;
        FieldDefinition key = null;
        foreach (Instruction instruction in lambda.Body.Instructions)
        {
            Code op = instruction.OpCode.Code;
            if (IsThisLoad(instruction))
            {
                var field = instruction.Next == null ? null : instruction.Next.Operand as FieldReference;
                if (field == null || instruction.Next.OpCode != OpCodes.Ldfld || field.DeclaringType != closure) return null;
                if (key != null && key != field.Resolve()) return null;
                key = field.Resolve();
            }
            else if (op == Code.Starg || op == Code.Starg_S || op == Code.Ldarga || op == Code.Ldarga_S)
                return null;
        }
        return key;
    }

    // static Func<T, K, bool> name(): a delegate, made once, to a static copy of the lambda that
    // takes the item and the key.
    static MethodDefinition StaticMatcher(ModuleDefinition module, TypeDefinition owner, MethodDefinition lambda, FieldDefinition key, TypeReference item)
    {
        string name = "<" + lambda.Name.Trim('<', '>').Replace(">", "_") + ">" + lambda.DeclaringType.Name.Replace("<>c__", "");
        var match = new MethodDefinition(name + "Match", MethodAttributes.Private | MethodAttributes.Static | MethodAttributes.HideBySig, module.TypeSystem.Boolean);
        var itemParameter = new ParameterDefinition("item", ParameterAttributes.None, lambda.Parameters[0].ParameterType);
        var keyParameter = new ParameterDefinition("key", ParameterAttributes.None, key.FieldType);
        match.Parameters.Add(itemParameter);
        match.Parameters.Add(keyParameter);
        var variables = new Dictionary<VariableDefinition, VariableDefinition>();
        foreach (VariableDefinition variable in lambda.Body.Variables)
        {
            var copy = new VariableDefinition(variable.VariableType);
            match.Body.Variables.Add(copy);
            variables[variable] = copy;
        }
        match.Body.InitLocals = lambda.Body.InitLocals;
        var copies = new Dictionary<Instruction, Instruction>();
        var source = lambda.Body.Instructions;
        for (int i = 0; i < source.Count; i++)
        {
            Instruction original = source[i];
            Code op = original.OpCode.Code;
            Instruction copy;
            if (IsThisLoad(original)) copy = Instruction.Create(OpCodes.Nop);
            else if (i > 0 && op == Code.Ldfld && IsThisLoad(source[i - 1])) copy = Instruction.Create(OpCodes.Ldarg, keyParameter);
            else if (op == Code.Ldarg_1 || ((op == Code.Ldarg || op == Code.Ldarg_S) && original.Operand == lambda.Parameters[0]))
                copy = Instruction.Create(OpCodes.Ldarg, itemParameter);
            else
            {
                copy = Instruction.Create(OpCodes.Nop);
                copy.OpCode = original.OpCode;
                // Variables keep their order, so the short forms (ldloc.0 and the like) stay valid.
                copy.Operand = original.Operand is VariableDefinition ? variables[(VariableDefinition)original.Operand] : original.Operand;
            }
            copies[original] = copy;
            match.Body.Instructions.Add(copy);
        }
        foreach (Instruction copy in match.Body.Instructions)
        {
            if (copy.Operand is Instruction) copy.Operand = copies[(Instruction)copy.Operand];
            else if (copy.Operand is Instruction[]) copy.Operand = ((Instruction[])copy.Operand).Select(t => copies[t]).ToArray();
        }
        owner.Methods.Add(match);

        MethodDefinition find = compat["DrifterCompat.Lists"].Methods.Single(m => m.Name == "Find" && m.GenericParameters.Count == 2);
        TypeReference funcType = module.ImportReference(find.Parameters[2].ParameterType.GetElementType());
        var func = new GenericInstanceType(funcType);
        func.GenericArguments.Add(item);
        func.GenericArguments.Add(key.FieldType);
        func.GenericArguments.Add(module.TypeSystem.Boolean);
        var constructor = new MethodReference(".ctor", module.TypeSystem.Void, func) { HasThis = true };
        constructor.Parameters.Add(new ParameterDefinition(module.TypeSystem.Object));
        constructor.Parameters.Add(new ParameterDefinition(module.TypeSystem.IntPtr));
        var cache = new FieldDefinition(name + "Delegate", FieldAttributes.Private | FieldAttributes.Static, func);
        owner.Fields.Add(cache);
        var getter = new MethodDefinition(name + "Get", MethodAttributes.Private | MethodAttributes.Static | MethodAttributes.HideBySig, func);
        ILProcessor il = getter.Body.GetILProcessor();
        Instruction made = il.Create(OpCodes.Ldsfld, cache);
        il.Emit(OpCodes.Ldsfld, cache);
        il.Emit(OpCodes.Brtrue, made);
        il.Emit(OpCodes.Ldnull);
        il.Emit(OpCodes.Ldftn, match);
        il.Emit(OpCodes.Newobj, constructor);
        il.Emit(OpCodes.Stsfld, cache);
        il.Append(made);
        il.Emit(OpCodes.Ret);
        owner.Methods.Add(getter);
        return getter;
    }
}
