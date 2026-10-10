using System;
using System.Collections.Generic;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

// Patches of single game methods: a room's post-processing volumes, the CRT filter, the console
// layout of the menus, PowerQuest's startup, saves and reflection caches, CacheCalls' tables (used
// in ScriptCosts.cs), the audio source filters and the water sprites' noise.
// Part of the assembly rewriter; see DowngradeAssemblies.cs.
partial class DowngradeAssemblies
{
    // RoomComponent.SetPostProcessingVolume switches a room's post-processing volumes; it ends
    // with Compat Platform.PostProcessingChanged().
    static int HookPostProcessingVolume(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.RoomComponent");
        if (type == null) return 0;
        MethodReference changed = CompatMethod(module, "Platform", "PostProcessingChanged");
        int count = 0;
        foreach (MethodDefinition method in type.Methods.Where(m => m.Name == "SetPostProcessingVolume" && m.HasBody))
            count += BeforeRets(method, () => new[] { Instruction.Create(OpCodes.Call, changed) });
        return count;
    }

    // BaseCRTEffect.ProcessEffect sets the CRT filter's uniforms, then draws its three passes.
    // Before the first Graphics.Blit, Compat Crt.Render(src, dest, blur, postprocess, final)
    // may draw the single-pass version instead; if it returns true the passes are skipped and
    // ProcessEffect goes on releasing its textures. Crt.AfterOriginal runs after the passes.
    static int PatchCrt(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "BaseCRTEffect");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.First(m => m.Name == "ProcessEffect" && m.HasBody);
        var code = method.Body.Instructions;
        int blit = code.IndexOf(code.First(i => Calls(i, "UnityEngine.Graphics", "Blit")));
        int discard = code.IndexOf(code.First(i => Calls(i, "UnityEngine.RenderTexture", "DiscardContents")));
        // Graphics.Blit(src, temporary, blurMaterial) and temporary.DiscardContents() as compiled.
        Instruction start = code[blit - 4], end = code[discard - 1];
        if (start.OpCode != OpCodes.Ldarg_1 || !end.OpCode.Name.StartsWith("ldloc") ||
            code.Any(i => i.Operand == start))
            throw new Exception("BaseCRTEffect.ProcessEffect does not have the expected shape");
        Func<string, FieldDefinition> field = name => type.Fields.First(f => f.Name == name);
        ILProcessor il = method.Body.GetILProcessor();
        // Branches to the end run AfterOriginal first: it takes the end's place.
        Instruction moved = end.Operand == null ? il.Create(end.OpCode) : il.Create(end.OpCode, (VariableDefinition)end.Operand);
        il.InsertAfter(end, moved);
        end.OpCode = OpCodes.Call;
        end.Operand = CompatMethod(module, "Crt", "AfterOriginal");
        InsertBefore(il, start, il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldarg_2),
                     il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldfld, field("blurMaterial")),
                     il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldfld, field("postProMaterial")),
                     il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldfld, field("finalPostProMaterial")),
                     il.Create(OpCodes.Call, CompatMethod(module, "Crt", "Render")), il.Create(OpCodes.Brtrue, end));
        PatchCrtPath(module, type);
        return 1;
    }

    // The game's console layout of its menus (Switch) on the PS Vita: every read of
    // GlobalScript.PlatformSwitch goes through Compat Platform.ConsoleUi. In GuiOptions (see
    // Compat VitaOptions) the Fullscreen entry becomes the CRT filter's: OnShow skips its
    // RemoveFromGrid("Fullscreen") when VitaOptions.KeepFullscreen() is true, OnClickFullscreen
    // starts with "IEnumerator e = VitaOptions.FullscreenClick(this); if (e != null) return e;"
    // and UpdateText ends with VitaOptions.Labels(this).
    static int PatchConsoleUi(ModuleDefinition module)
    {
        TypeDefinition options = GameType(module, "GuiOptions");
        if (options == null) return 0;
        int count = 0;
        MethodReference consoleUi = CompatMethod(module, "Platform", "ConsoleUi");
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
            count += CallAfter(method, i => i.OpCode == OpCodes.Ldfld && i.Operand is FieldReference &&
                ((FieldReference)i.Operand).Name == "PlatformSwitch" && ((FieldReference)i.Operand).DeclaringType.FullName == "GlobalScript", consoleUi);
        Count("GlobalScript.PlatformSwitch reads", count);
        // OnShow's statement Container("GridContainer").RemoveFromGrid("Fullscreen") (ldarg.0;
        // ldstr; call Container; ldstr; callvirt) starts with
        // "if (VitaOptions.KeepFullscreen()) skip it"; the stack is empty there.
        MethodDefinition show = options.Methods.First(m => m.Name == "OnShow" && m.HasBody);
        Instruction remove = show.Body.Instructions.Single(i => i.OpCode == OpCodes.Ldstr && (string)i.Operand == "Fullscreen" &&
            CalledName(i.Next) == "RemoveFromGrid").Next;
        Instruction statement = remove.Previous.Previous.Previous.Previous;
        if (statement == null || statement.OpCode != OpCodes.Ldarg_0 || statement.Next.OpCode != OpCodes.Ldstr ||
            CalledName(statement.Next.Next) != "Container" ||
            remove.Next == null || show.Body.Instructions.Any(i => i.Operand == statement.Next || i.Operand == statement.Next.Next ||
                i.Operand == remove.Previous || i.Operand == remove))
            throw new Exception("GuiOptions.OnShow does not have the expected shape");
        ILProcessor il = show.Body.GetILProcessor();
        InsertBeforeRetarget(show, statement, il.Create(OpCodes.Call, CompatMethod(module, "VitaOptions", "KeepFullscreen")),
                             il.Create(OpCodes.Brtrue, remove.Next));

        MethodDefinition click = options.Methods.First(m => m.Name == "OnClickFullscreen" && m.HasBody);
        var routine = new VariableDefinition(click.ReturnType);
        click.Body.Variables.Add(routine);
        click.Body.InitLocals = true;
        il = click.Body.GetILProcessor();
        Instruction first = click.Body.Instructions[0];
        Prepend(click, il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Call, CompatMethod(module, "VitaOptions", "FullscreenClick")),
                il.Create(OpCodes.Stloc, routine), il.Create(OpCodes.Ldloc, routine), il.Create(OpCodes.Brfalse, first),
                il.Create(OpCodes.Ldloc, routine), il.Create(OpCodes.Ret));

        MethodDefinition update = options.Methods.First(m => m.Name == "UpdateText" && m.HasBody);
        int labels = BeforeRets(update, () => new[] { Instruction.Create(OpCodes.Ldarg_0), Instruction.Create(OpCodes.Call, CompatMethod(module, "VitaOptions", "Labels")) });
        return count + 2 + labels;
    }

    // The PS Vita's CRT mode combo, Select + R (Compat CrtToggle): its R (the right trigger)
    // must not reach the game. UnityInputDevice.ReadRawButtonState starts with
    // "if (CrtToggle.SuppressButton(index)) return false;". It runs after VirtualButtons (the
    // pass list's order), which also prepends a check there, so a suppressed R reads released
    // even when Controls.VirtualButton would press it.
    static int PatchCrtCombo(ModuleDefinition module)
    {
        TypeDefinition device = GameType(module, "InControl.UnityInputDevice");
        if (device == null) return 0;
        MethodDefinition read = device.Methods.First(m => m.Name == "ReadRawButtonState" && m.HasBody);
        ILProcessor il = read.Body.GetILProcessor();
        Instruction first = read.Body.Instructions[0];
        Prepend(read, il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Call, CompatMethod(module, "CrtToggle", "SuppressButton")),
                il.Create(OpCodes.Brfalse, first), il.Create(OpCodes.Ldc_I4_0), il.Create(OpCodes.Ret));
        return 1;
    }

    // How the filter gets its picture (see Compat Crt.useDirect):
    // - CRTEffect.OnRenderImage becomes RenderImageCompat, and a new CRTEffect.OnEnable calls
    //   Crt.Enabled(this, new CrtRender(RenderImageCompat)), which may give the camera a
    //   CrtImageEffect that calls it;
    // - BaseCRTEffect.InternalPreRender starts with "if (Crt.PreCull(m_mainCamera)) return;";
    // - BaseCRTEffect.OnPostRender starts with
    //   "Texture t = Crt.PostRender(this); if (t != null) { ProcessEffect(t, null); return; }";
    // - BaseCRTEffect.OnDisable starts with Crt.Disabled(this).
    static void PatchCrtPath(ModuleDefinition module, TypeDefinition baseType)
    {
        Func<string, MethodReference> crt = name => CompatMethod(module, "Crt", name);
        Func<string, MethodDefinition> baseMethod = name => baseType.Methods.First(m => m.Name == name && m.HasBody);
        TypeDefinition effect = module.GetType("CRTEffect");
        MethodDefinition renderImage = effect == null ? null : effect.Methods.FirstOrDefault(m => m.Name == "OnRenderImage");
        if (renderImage == null || effect.Methods.Any(m => m.Name == "OnEnable") || baseType.Methods.Any(m => m.Name == "OnEnable"))
            throw new Exception("CRTEffect does not have the expected shape");
        renderImage.Name = "RenderImageCompat";
        var onEnable = new MethodDefinition("OnEnable", MethodAttributes.Private | MethodAttributes.HideBySig, module.TypeSystem.Void);
        effect.Methods.Add(onEnable);
        ILProcessor il = onEnable.Body.GetILProcessor();
        il.Append(il.Create(OpCodes.Ldarg_0));
        il.Append(il.Create(OpCodes.Ldarg_0));
        il.Append(il.Create(OpCodes.Ldftn, renderImage));
        il.Append(il.Create(OpCodes.Newobj, CompatMethod(module, "CrtRender", ".ctor")));
        il.Append(il.Create(OpCodes.Call, crt("Enabled")));
        il.Append(il.Create(OpCodes.Ret));

        MethodDefinition preRender = baseMethod("InternalPreRender");
        il = preRender.Body.GetILProcessor();
        Instruction first = preRender.Body.Instructions[0];
        Prepend(preRender, il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldfld, baseType.Fields.First(f => f.Name == "m_mainCamera")),
                il.Create(OpCodes.Call, crt("PreCull")), il.Create(OpCodes.Brfalse, first), il.Create(OpCodes.Ret));

        MethodDefinition postRender = baseMethod("OnPostRender");
        MethodDefinition process = baseMethod("ProcessEffect");
        var source = new VariableDefinition(process.Parameters[0].ParameterType);
        postRender.Body.Variables.Add(source);
        postRender.Body.InitLocals = true;
        il = postRender.Body.GetILProcessor();
        first = postRender.Body.Instructions[0];
        Prepend(postRender, il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Call, crt("PostRender")),
                il.Create(OpCodes.Stloc, source), il.Create(OpCodes.Ldloc, source), il.Create(OpCodes.Brfalse, first),
                il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldloc, source), il.Create(OpCodes.Ldnull),
                il.Create(OpCodes.Call, process), il.Create(OpCodes.Ret));

        MethodDefinition disable = baseMethod("OnDisable");
        il = disable.Body.GetILProcessor();
        Prepend(disable, il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Call, crt("Disabled")));
    }

    // PowerQuest's save surrogate, save manager and field copier ask Type.GetFields and
    // Attribute.IsDefined again for every object and field they handle; a save restore
    // spends most of its time there (about 35 s on the PS Vita). In these types (and the
    // compiler-generated types nested in them) the calls go to Compat ReflectionCache.
    static readonly string[] ReflectionCachedTypes =
    {
        "PowerTools.Quest.QuestSaveSurrogateSelector", "PowerTools.Quest.QuestSaveManager", "PowerTools.Quest.QuestUtils",
    };

    // PowerQuest.Awake starts with Compat SharedPowerQuest.Awake(this, m_roomPrefabs): room
    // scenes keep a PowerQuest copy without asset references, which loads the full one when
    // it is the first (see asset_rules.py powerquest_overlay).
    static int PatchPowerQuestAwake(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.PowerQuest");
        if (type == null) return 0;
        MethodDefinition awake = type.Methods.First(m => m.Name == "Awake" && !m.HasParameters && m.HasBody);
        FieldDefinition rooms = type.Fields.First(f => f.Name == "m_roomPrefabs");
        ILProcessor il = awake.Body.GetILProcessor();
        Prepend(awake, il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldfld, rooms),
                il.Create(OpCodes.Call, CompatMethod(module, "SharedPowerQuest", "Awake")));
        return 1;
    }

    // Game methods whose answer depends only on their Type argument, asked again for every
    // saved object and field: (type, method). Each keeps its body under <name>Uncached; the
    // method itself asks Compat TypeMemo first.
    static readonly string[][] TypePredicates =
    {
        new[] { "PowerTools.Quest.QuestSaveSurrogateSelector", "IsIgnoredType" },
        new[] { "PowerTools.Quest.QuestSaveSurrogateSelector", "IsKnownType" },
        new[] { "PowerTools.Quest.QuestSaveSurrogateSelector", "IsNullableType" },
    };

    static int MemoizeTypePredicates(ModuleDefinition module)
    {
        int count = 0;
        MethodReference tryGet = CompatMethod(module, "TypeMemo", "TryGet"), store = CompatMethod(module, "TypeMemo", "Store");
        for (int slot = 0; slot < TypePredicates.Length; slot++)
        {
            TypeDefinition type = GameType(module, TypePredicates[slot][0]);
            if (type == null) continue;
            MethodDefinition original = type.Methods.FirstOrDefault(m => m.Name == TypePredicates[slot][1] && m.HasBody &&
                m.Parameters.Count == 1 && m.Parameters[0].ParameterType.FullName == "System.Type" && m.ReturnType.FullName == "System.Boolean");
            if (original == null) throw new Exception(TypePredicates[slot][0] + "." + TypePredicates[slot][1] + " does not have the expected shape");
            MethodDefinition memoized = MemoizedCopy(module, original);
            var value = new VariableDefinition(module.TypeSystem.Boolean);
            memoized.Body.Variables.Add(value);
            memoized.Body.InitLocals = true;
            ILProcessor il = memoized.Body.GetILProcessor();
            OpCode argument = original.IsStatic ? OpCodes.Ldarg_0 : OpCodes.Ldarg_1;
            Instruction compute = il.Create(OpCodes.Ldc_I4, slot);
            il.Append(il.Create(OpCodes.Ldc_I4, slot));
            il.Append(il.Create(argument));
            il.Append(il.Create(OpCodes.Ldloca_S, value));
            il.Append(il.Create(OpCodes.Call, tryGet));
            il.Append(il.Create(OpCodes.Brfalse, compute));
            il.Append(il.Create(OpCodes.Ldloc, value));
            il.Append(il.Create(OpCodes.Ret));
            il.Append(compute);
            il.Append(il.Create(argument));
            if (!original.IsStatic) il.Append(il.Create(OpCodes.Ldarg_0));
            il.Append(il.Create(argument));
            il.Append(il.Create(original.IsStatic ? OpCodes.Call : OpCodes.Callvirt, original));
            il.Append(il.Create(OpCodes.Call, store));
            il.Append(il.Create(OpCodes.Ret));
            count++;
        }
        return count;
    }

    // A method's body moves to <name>Uncached; the returned method takes its name, signature
    // and attributes (so its interface or virtual slot too) with an empty body for the memo,
    // and the module's calls to the original go to it. (Calls from other assemblies bind by
    // name, to it as well.)
    static MethodDefinition MemoizedCopy(ModuleDefinition module, MethodDefinition original)
    {
        var memoized = new MethodDefinition(original.Name, original.Attributes, original.ReturnType);
        foreach (ParameterDefinition parameter in original.Parameters)
            memoized.Parameters.Add(new ParameterDefinition(parameter.Name, parameter.Attributes, parameter.ParameterType));
        original.Name += "Uncached";
        // After the copy above: the slot is the memoized method's alone.
        original.Attributes &= ~(MethodAttributes.Virtual | MethodAttributes.Final | MethodAttributes.NewSlot);
        original.DeclaringType.Methods.Add(memoized);
        foreach (MethodDefinition method in AllTypes(module.Types).SelectMany(t => t.Methods).Where(m => m.HasBody && m != original))
            foreach (Instruction instruction in method.Body.Instructions)
                if (instruction.Operand == original) instruction.Operand = memoized;
        return memoized;
    }

    // QuestSaveSurrogateSelector.SetObjectData restores each field in its own try block,
    // where info.GetValue throws for fields a save lacks. Inside those inner blocks GetValue and
    // FieldInfo.SetValue go to Compat SaveFields, which skips such fields without throwing.
    static int SkipMissingSaveFields(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.QuestSaveSurrogateSelector");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.First(m => m.Name == "SetObjectData" && m.HasBody);
        MethodReference getValue = CompatMethod(module, "SaveFields", "GetValue"), setValue = CompatMethod(module, "SaveFields", "SetValue");
        var code = method.Body.Instructions;
        // Try blocks nested in another one: the per-field blocks.
        var inner = method.Body.ExceptionHandlers.Where(h => method.Body.ExceptionHandlers.Any(o => o != h &&
            code.IndexOf(o.TryStart) <= code.IndexOf(h.TryStart) && code.IndexOf(h.TryEnd) <= code.IndexOf(o.TryEnd))).ToList();
        if (inner.Count == 0) throw new Exception("QuestSaveSurrogateSelector.SetObjectData does not have the expected shape");
        int count = 0;
        for (int i = 0; i < code.Count; i++)
        {
            var called = code[i].Operand as MethodReference;
            if (called == null || !inner.Any(h => code.IndexOf(h.TryStart) <= i && i < code.IndexOf(h.TryEnd))) continue;
            if (called.FullName == "System.Object System.Runtime.Serialization.SerializationInfo::GetValue(System.String,System.Type)")
                code[i].Operand = getValue;
            else if (called.FullName == "System.Void System.Reflection.FieldInfo::SetValue(System.Object,System.Object)")
                code[i].Operand = setValue;
            else continue;
            code[i].OpCode = OpCodes.Call;
            count++;
        }
        return count;
    }

    // QuestSaveManager compresses saves with new GZipStream(stream, level, leaveOpen); the call
    // goes to Compat SaveCompression.Create, which writes stored gzip blocks on the PS Vita.
    static int RedirectSaveCompression(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.QuestSaveManager");
        if (type == null) return 0;
        MethodReference create = CompatMethod(module, "SaveCompression", "Create");
        int count = 0;
        foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
            foreach (Instruction instruction in method.Body.Instructions)
            {
                var called = instruction.Operand as MethodReference;
                if (instruction.OpCode != OpCodes.Newobj || called == null || called.FullName !=
                    "System.Void System.IO.Compression.GZipStream::.ctor(System.IO.Stream,System.IO.Compression.CompressionLevel,System.Boolean)") continue;
                instruction.OpCode = OpCodes.Call;
                instruction.Operand = create;
                count++;
            }
        if (count != 1) throw new Exception("QuestSaveManager: expected one compressing GZipStream, found " + count);
        return count;
    }

    // Field access without reflection (Compat FastFields, SaveSurrogate):
    // - QuestUtils.CopyFields<T>(to, from) becomes FastFields.Copy(to, from);
    // - QuestSaveSurrogateSelector.SetObjectData and GetObjectData start with
    //   SaveSurrogate.TrySetObjectData / TryGetObjectData, which do their work from a per-type
    //   field list and return false where the original body must run instead.
    static int FastSaveFields(ModuleDefinition module)
    {
        TypeDefinition utils = GameType(module, "PowerTools.Quest.QuestUtils");
        TypeDefinition selector = GameType(module, "PowerTools.Quest.QuestSaveSurrogateSelector");
        if (utils == null || selector == null) return 0;
        MethodDefinition copy = utils.Methods.Single(m => m.Name == "CopyFields" && m.HasGenericParameters && m.Parameters.Count == 2);
        copy.Body.Instructions.Clear();
        copy.Body.Variables.Clear();
        copy.Body.ExceptionHandlers.Clear();
        ILProcessor il = copy.Body.GetILProcessor();
        il.Append(il.Create(OpCodes.Ldarg_0));
        il.Append(il.Create(OpCodes.Box, copy.GenericParameters[0]));
        il.Append(il.Create(OpCodes.Ldarg_1));
        il.Append(il.Create(OpCodes.Box, copy.GenericParameters[0]));
        il.Append(il.Create(OpCodes.Call, CompatMethod(module, "FastFields", "Copy")));
        il.Append(il.Create(OpCodes.Ret));

        MethodDefinition set = selector.Methods.Single(m => m.Name == "SetObjectData" && m.Parameters.Count == 4);
        var result = new VariableDefinition(module.TypeSystem.Object);
        set.Body.Variables.Add(result);
        set.Body.InitLocals = true;
        il = set.Body.GetILProcessor();
        Instruction start = set.Body.Instructions[0];
        Prepend(set, il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldarg_2), il.Create(OpCodes.Ldloca, result),
                il.Create(OpCodes.Call, CompatMethod(module, "SaveSurrogate", "TrySetObjectData")),
                il.Create(OpCodes.Brfalse, start), il.Create(OpCodes.Ldloc, result), il.Create(OpCodes.Ret));

        // GetObjectData: TryGetObjectData(this, obj, info, obj is IQuestSaveCachable c && !c.SaveDirtyEver).
        MethodDefinition get = selector.Methods.Single(m => m.Name == "GetObjectData" && m.Parameters.Count == 3);
        TypeDefinition cachable = module.GetType("PowerTools.Quest.IQuestSaveCachable");
        MethodDefinition dirtyEver = cachable.Methods.Single(m => m.Name == "get_SaveDirtyEver");
        var cached = new VariableDefinition(cachable);
        var unchanged = new VariableDefinition(module.TypeSystem.Boolean);
        get.Body.Variables.Add(cached);
        get.Body.Variables.Add(unchanged);
        get.Body.InitLocals = true;
        il = get.Body.GetILProcessor();
        start = get.Body.Instructions[0];
        // The original body is one try block with a catch that swallows everything; the prologue
        // goes inside it (an exception from SaveDirtyEver is swallowed as in the original) and
        // leaves to the instruction after the catch, the method's ret.
        ExceptionHandler whole = get.Body.ExceptionHandlers.SingleOrDefault(h => h.TryStart == start);
        if (whole == null || whole.HandlerType != ExceptionHandlerType.Catch || whole.HandlerEnd == null ||
            whole.HandlerEnd.OpCode != OpCodes.Ret || get.Body.ExceptionHandlers.Any(h => h != whole && h.TryStart == start))
            throw new Exception("QuestSaveSurrogateSelector.GetObjectData does not have the expected shape");
        // No values on the stack across branches: IL2CPP merges them badly.
        Instruction call = il.Create(OpCodes.Ldarg_0), first = il.Create(OpCodes.Ldc_I4_0);
        Prepend(get, first, il.Create(OpCodes.Stloc, unchanged),
                il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Isinst, cachable), il.Create(OpCodes.Stloc, cached),
                il.Create(OpCodes.Ldloc, cached), il.Create(OpCodes.Brfalse, call),
                il.Create(OpCodes.Ldloc, cached), il.Create(OpCodes.Callvirt, dirtyEver), il.Create(OpCodes.Ldc_I4_0), il.Create(OpCodes.Ceq),
                il.Create(OpCodes.Stloc, unchanged),
                call, il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldarg_2), il.Create(OpCodes.Ldloc, unchanged),
                il.Create(OpCodes.Call, CompatMethod(module, "SaveSurrogate", "TryGetObjectData")),
                il.Create(OpCodes.Brfalse, start), il.Create(OpCodes.Leave, whole.HandlerEnd));
        whole.TryStart = first;
        return 3;
    }

    // QuestSaveSurrogateSelector.GetSurrogate(type, context, out selector) answers either
    // (this, this) or (null, null), from the type alone, for every object saved or restored.
    // Its body moves to GetSurrogateUncached; GetSurrogate (the ISurrogateSelector method the
    // formatter calls) asks Compat TypeMemo first (slot after the TypePredicates ones) and
    // remembers consistent answers.
    static int MemoizeSurrogateLookup(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.QuestSaveSurrogateSelector");
        if (type == null) return 0;
        MethodDefinition original = type.Methods.Single(m => m.Name == "GetSurrogate" && m.Parameters.Count == 3);
        MethodDefinition memoized = MemoizedCopy(module, original);
        MethodReference tryGet = CompatMethod(module, "TypeMemo", "TryGet"), store = CompatMethod(module, "TypeMemo", "Store");
        int slot = TypePredicates.Length;
        var known = new VariableDefinition(module.TypeSystem.Boolean);
        var result = new VariableDefinition(original.ReturnType);
        memoized.Body.Variables.Add(known);
        memoized.Body.Variables.Add(result);
        memoized.Body.InitLocals = true;
        ILProcessor il = memoized.Body.GetILProcessor();
        // Short argument forms: IL2CPP 2018.2 misreads the long ldarg of a by-ref argument.
        Instruction compute = il.Create(OpCodes.Ldarg_0), returnNull = il.Create(OpCodes.Ldarg_3), skip = il.Create(OpCodes.Ldloc, result), storeFalse = il.Create(OpCodes.Ldc_I4, slot);
        foreach (Instruction instruction in new[]
        {
            il.Create(OpCodes.Ldc_I4, slot), il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldloca_S, known),
            il.Create(OpCodes.Call, tryGet), il.Create(OpCodes.Brfalse, compute),
            // No values on the stack across branches: IL2CPP merges them badly.
            il.Create(OpCodes.Ldloc, known), il.Create(OpCodes.Brfalse, returnNull),
            il.Create(OpCodes.Ldarg_3), il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Stind_Ref),
            il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ret),
            returnNull, il.Create(OpCodes.Ldnull), il.Create(OpCodes.Stind_Ref), il.Create(OpCodes.Ldnull), il.Create(OpCodes.Ret),
            compute, il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldarg_2), il.Create(OpCodes.Ldarg_3),
            il.Create(OpCodes.Call, original), il.Create(OpCodes.Stloc, result),
            il.Create(OpCodes.Ldarg_3), il.Create(OpCodes.Ldind_Ref), il.Create(OpCodes.Ldloc, result), il.Create(OpCodes.Bne_Un, skip),
            il.Create(OpCodes.Ldloc, result), il.Create(OpCodes.Brfalse, storeFalse),
            il.Create(OpCodes.Ldloc, result), il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Bne_Un, skip),
            il.Create(OpCodes.Ldc_I4, slot), il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldc_I4_1),
            il.Create(OpCodes.Call, store), il.Create(OpCodes.Pop), il.Create(OpCodes.Br, skip),
            storeFalse, il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Ldc_I4_0),
            il.Create(OpCodes.Call, store), il.Create(OpCodes.Pop),
            skip, il.Create(OpCodes.Ret),
        })
            il.Append(instruction);
        return 1;
    }

    static int CacheReflection(ModuleDefinition module)
    {
        int count = 0;
        var replacements = new Dictionary<string, string>
        {
            { "System.Reflection.FieldInfo[] System.Type::GetFields(System.Reflection.BindingFlags)", "GetFields" },
            { "System.Boolean System.Attribute::IsDefined(System.Reflection.MemberInfo,System.Type)", "IsDefined" },
        };
        var perType = ReflectionCachedTypes.ToDictionary(t => t, t => 0);
        foreach (TypeDefinition type in AllTypes(module.Types))
        {
            string outer = OuterType(type).FullName;
            if (!perType.ContainsKey(outer)) continue;
            foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
                foreach (Instruction instruction in method.Body.Instructions)
                {
                    var called = instruction.Operand as MethodReference;
                    string replacement;
                    if (called == null || !replacements.TryGetValue(called.FullName, out replacement)) continue;
                    instruction.OpCode = OpCodes.Call;
                    instruction.Operand = CompatMethod(module, "ReflectionCache", replacement);
                    perType[outer]++;
                    count++;
                }
        }
        foreach (var entry in perType) Count("ReflectionCache in " + entry.Key, entry.Value);
        return count;
    }

    // Calls the game's scripts repeat every frame although the answer cannot change, which
    // the PS Vita (IL2CPP, one 444 MHz core for scripts) feels far more than the PC: (caller
    // type, caller method, called method, Compat class, Compat method). "*" matches any caller in the game's
    // assemblies. See Compat ScriptCache and AnimatorOverrides.
    // - Type.GetMethod: PowerQuest finds its scripts' Update functions (and the interaction
    //   and region functions it calls) by name every frame.
    // - MethodBase.GetParameters copies the parameter array; that caller only reads its length.
    // - MethodInfo.Invoke: PowerQuest.Update then calls those Update functions by reflection.
    // - QuestMenuManager.Update sorts the same GUIs every frame (Compat Lists.Sort).
    // - CharacterRimlight.LateUpdate creates a MaterialPropertyBlock up to three times a frame.
    // Also List<T>.AddRange, which copies its argument to a temporary array (Compat Lists).
    static readonly string[][] CachedCalls =
    {
        new[] { "*", "*", "System.Reflection.MethodInfo System.Type::GetMethod(System.String,System.Reflection.BindingFlags)", "ScriptCache", "GetMethod" },
        new[] { "*", "*", "System.Reflection.MethodInfo System.Type::GetMethod(System.String)", "ScriptCache", "GetMethod" },
        new[] { "PowerTools.Quest.PowerQuest", "StartScriptInteractionCoroutine", "System.Reflection.ParameterInfo[] System.Reflection.MethodBase::GetParameters()", "ScriptCache", "GetParameters" },
        new[] { "PowerTools.Quest.PowerQuest", "Update", "System.Object System.Reflection.MethodBase::Invoke(System.Object,System.Object[])", "ScriptCache", "Invoke" },
        new[] { "PowerTools.Quest.PowerQuest", "StartScriptInteractionCoroutine", "System.Object System.Reflection.MethodBase::Invoke(System.Object,System.Object[])", "ScriptCache", "Invoke" },
        new[] { "PowerTools.Quest.QuestMenuManager", "Update", "System.Void System.Collections.Generic.List`1<PowerTools.Quest.Gui>::Sort(System.Comparison`1<!0>)", "Lists", "Sort" },
        new[] { "PowerTools.Quest.PowerQuest", "UpdateGuiVisibility", "System.Void System.Collections.Generic.List`1<PowerTools.Quest.Gui>::Sort(System.Comparison`1<!0>)", "Lists", "SortByKey" },
        new[] { "PowerTools.Quest.QuestMenuManager", "Update", "System.Void UnityEngine.Transform::SetSiblingIndex(System.Int32)", "Hierarchy", "SetSiblingIndex" },
        new[] { "CharacterRimlight", "LateUpdate", "System.Void UnityEngine.MaterialPropertyBlock::.ctor()", "ScriptCache", "NewPropertyBlock" },
        // Object.name in the name searches over clip, sprite and cue lists (Compat Names), and
        // every name assignment, which empties that cache.
        new[] { "PowerTools.Quest.QuestUtils", "*", "System.String UnityEngine.Object::get_name()", "Names", "Get" },
        new[] { "PowerTools.Quest.SystemAudio", "*", "System.String UnityEngine.Object::get_name()", "Names", "Get" },
        new[] { "PowerTools.SpriteAnim", "*", "System.String UnityEngine.Object::get_name()", "Names", "Get" },
        new[] { "PowerTools.Quest.CharacterComponent", "*", "System.String UnityEngine.Object::get_name()", "Names", "Get" },
        new[] { "PowerTools.Quest.PowerQuest", "*", "System.String UnityEngine.Object::get_name()", "Names", "Get" },
        // GuiControl.ScriptName reads its GameObject's name on every call (4 us on the PS Vita) to
        // see whether it changed; GUI scripts look their controls up by script name every frame.
        new[] { "PowerTools.Quest.GuiControl", "get_ScriptName", "System.String UnityEngine.Object::get_name()", "Names", "Get" },
        // GuiPlatformContent.Update shows its sprite by name every frame (gui images look sprites up by name).
        new[] { "GuiPlatformContent", "Update", "System.String UnityEngine.Object::get_name()", "Names", "Get" },
        new[] { "*", "*", "System.Void UnityEngine.Object::set_name(System.String)", "Names", "Set" },
        // SystemAudio starting and deactivating a voice: on the PS Vita a source gets only the
        // filters it uses, and dialog its room's reverb (Compat SourceFilters; with
        // PatchSourceFilters). Play and PlayDelayed also stamp the clip for Compat AudioData.
        new[] { "PowerTools.Quest.SystemAudio", "*", "System.Void UnityEngine.AudioSource::Play()", "SourceFilters", "Play" },
        new[] { "PowerTools.Quest.SystemAudio", "*", "System.Void UnityEngine.AudioSource::PlayDelayed(System.Single)", "SourceFilters", "PlayDelayed" },
        new[] { "PowerTools.Quest.SystemAudio", "*", "System.Void UnityEngine.GameObject::SetActive(System.Boolean)", "SourceFilters", "SetActive" },
        // A character's sprite-shaped hotspot rebuilt at each sprite change: pathCount and offset
        // assignments of the values the collider already has are skipped (Compat Colliders).
        new[] { "PowerTools.Quest.CharacterComponent", "Update", "System.Void UnityEngine.PolygonCollider2D::set_pathCount(System.Int32)", "Colliders", "SetPathCount" },
        new[] { "PowerTools.Quest.CharacterComponent", "Update", "System.Void UnityEngine.Collider2D::set_offset(UnityEngine.Vector2)", "Colliders", "SetOffset" },
        // A prop's sorting orders set again at each move: assignments of the order a renderer
        // already has are skipped (Compat Renderers).
        new[] { "PowerTools.Quest.PropComponent", "*", "System.Void UnityEngine.Renderer::set_sortingOrder(System.Int32)", "Renderers", "SetSortingOrder" },
    };

    // Material and MaterialPropertyBlock calls that name a shader property by string go to
    // the int-id overloads through Compat ShaderIds (the engine looks the string up each call).
    // The CRT filter's own code is left as it is.
    static readonly string[] ShaderIdTypes = { "UnityEngine.Material", "UnityEngine.MaterialPropertyBlock" };
    static readonly string[] ShaderIdExcludedTypes = { "BaseCRTEffect" };

    // The audio filter components of SystemAudio's sources (Compat SourceFilters): in SystemAudio,
    // GameObject.GetComponent<T> and AddComponent<T> of an audio filter type go to
    // SourceFilters.StageGet and StageAdd; in the rest of the game, GetComponent<T> of one goes to
    // SourceFilters.Live (Component) or LiveGameObject.
    static readonly string[] AudioFilterTypes = { "UnityEngine.AudioLowPassFilter", "UnityEngine.AudioHighPassFilter",
        "UnityEngine.AudioEchoFilter", "UnityEngine.AudioChorusFilter", "UnityEngine.AudioDistortionFilter", "UnityEngine.AudioReverbFilter" };

    static int PatchSourceFilters(ModuleDefinition module)
    {
        int count = 0;
        foreach (TypeDefinition type in AllTypes(module.Types))
        {
            bool system = OuterType(type).FullName == "PowerTools.Quest.SystemAudio";
            foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
            foreach (Instruction instruction in method.Body.Instructions)
            {
                var called = instruction.Operand as GenericInstanceMethod;
                if (called == null || (instruction.OpCode != OpCodes.Call && instruction.OpCode != OpCodes.Callvirt) ||
                    called.GenericArguments.Count != 1 || called.Parameters.Count != 0 ||
                    !AudioFilterTypes.Contains(called.GenericArguments[0].FullName))
                    continue;
                string owner = called.DeclaringType.FullName;
                string target = owner == "UnityEngine.GameObject" && called.Name == "GetComponent" ? (system ? "StageGet" : "LiveGameObject")
                    : owner == "UnityEngine.GameObject" && called.Name == "AddComponent" ? (system ? "StageAdd" : null)
                    : owner == "UnityEngine.Component" && called.Name == "GetComponent" && !system ? "Live" : null;
                if (target == null) continue;
                instruction.OpCode = OpCodes.Call;
                instruction.Operand = CompatMethod(module, "SourceFilters", target, called.GenericArguments[0]);
                count++;
            }
        }
        return count;
    }

    // WaterSprite.LateUpdate and ShaderAnimator.LateUpdate set their renderer's noise uniforms
    // (WaterSprite and PropBurble materials); at their end Compat WaterMasks.After(this) gives
    // the renderer the split shader and the uniform-neighbourhood mask of its sprite's texture,
    // which skip the noise where it cannot change the read.
    static int PatchWaterSprite(ModuleDefinition module)
    {
        MethodReference after = null;
        int count = 0;
        foreach (string name in new[] { "WaterSprite", "ShaderAnimator" })
        {
            TypeDefinition type = GameType(module, name);
            if (type == null) continue;
            if (after == null) after = CompatMethod(module, "WaterMasks", "After");
            int hooked = 0;
            foreach (MethodDefinition method in type.Methods.Where(m => m.Name == "LateUpdate" && m.HasBody))
                hooked += BeforeRets(method, () => new[] { Instruction.Create(OpCodes.Ldarg_0), Instruction.Create(OpCodes.Call, after) });
            Count("WaterMasks.After in " + name + ".LateUpdate", hooked);
            count += hooked;
        }
        return count;
    }
}
