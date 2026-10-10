using System;
using System.Collections.Generic;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

// More per-frame costs in the game's systems: SpriteAnim.Play, inventory icons, GUI reshows, region
// states, the pathfinder's geometry, text widths, controller angles, directional animations, lip
// sync, enum names.
// Part of the assembly rewriter; see DowngradeAssemblies.cs.
partial class DowngradeAssemblies
{
    // PowerTools' SpriteAnim.Play(clip) puts the clip in its override controller's one slot
    // (ApplyOverrides), enables the Animator, evaluates it (Animator.Update(0)), restarts the
    // state at time 0 and evaluates it again. GuiControllerInv plays each inventory icon again
    // every frame, and each Animator.Update costs about 0.05 ms on the PC. When the slot
    // already held the clip and the Animator was already enabled, the first evaluation only
    // repeats the pose the second one overwrites; Compat AnimatorOverrides skips it then. For
    // GuiControllerInv's icons it skips the second evaluation too (see AnimatorOverrides);
    // GuiControllerInv.Update marks its Play calls with BeginIconReplay and EndIconReplay
    // (MarkIconReplays).
    static int PatchSpriteAnimPlay(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.SpriteAnim");
        if (type == null) return 0;
        MethodDefinition play = type.Methods.Single(m => m.Name == "Play" && m.Parameters.Count == 2);
        var code = play.Body.Instructions;
        Instruction[] apply = code.Where(i => CalledFullName(i).StartsWith("System.Void UnityEngine.AnimatorOverrideController::ApplyOverrides(")).ToArray();
        Instruction[] enable = code.Where(i => CalledFullName(i) == "System.Void UnityEngine.Behaviour::set_enabled(System.Boolean)").ToArray();
        Instruction[] update = code.Where(i => CalledFullName(i) == "System.Void UnityEngine.Animator::Update(System.Single)").ToArray();
        if (apply.Length != 1 || enable.Length != 1 || update.Length != 2 ||
            !(code.IndexOf(apply[0]) < code.IndexOf(enable[0]) && code.IndexOf(enable[0]) < code.IndexOf(update[0])))
            throw new Exception("SpriteAnim.Play does not have the expected shape");
        foreach (var pair in new[] { Tuple.Create(apply[0], "ApplyOverrides"), Tuple.Create(enable[0], "SetEnabled"),
                                     Tuple.Create(update[0], "UpdateUnlessUnchanged"), Tuple.Create(update[1], "UpdateAfterRestart") })
        {
            pair.Item1.OpCode = OpCodes.Call;
            pair.Item1.Operand = CompatMethod(module, "AnimatorOverrides", pair.Item2);
        }
        return 4;
    }

    static int MarkIconReplays(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "GuiControllerInv");
        if (type == null) return 0;
        MethodDefinition update = type.Methods.Single(m => m.Name == "Update" && !m.HasParameters);
        MethodReference begin = CompatMethod(module, "AnimatorOverrides", "BeginIconReplay"), end = CompatMethod(module, "AnimatorOverrides", "EndIconReplay");
        ILProcessor il = update.Body.GetILProcessor();
        Instruction[] plays = update.Body.Instructions.Where(i =>
            CalledFullName(i) == "System.Void PowerTools.SpriteAnim::Play(UnityEngine.AnimationClip,System.Single)").ToArray();
        if (plays.Length == 0) throw new Exception("GuiControllerInv.Update does not have the expected shape");
        foreach (Instruction play in plays)
        {
            // The arguments are already on the stack; branches to the call itself would skip
            // only the marker, which leaves the replay unmarked.
            il.InsertBefore(play, il.Create(OpCodes.Call, begin));
            il.InsertAfter(play, il.Create(OpCodes.Call, end));
        }
        return plays.Length;
    }

    // See Compat GuiReshows. PowerQuest.UpdateGuiVisibility gets, after its sort and before its
    // first pass, a call to __DrifterMarkReshows:
    //   GuiReshows.BeginPass();
    //   for (int i = 0; i < m_sortedGuis.Count; i++)
    //       if (m_sortedGuis[i].Visible && m_sortedGuis[i].HideSpecificGuis != null)
    //           foreach (string name in m_sortedGuis[i].HideSpecificGuis)
    //               if (GetGui(name) != null) GuiReshows.Mark(GetGui(name));
    // (the second pass's choice; the getters and GetGui only read), and the first pass's
    // sortedGui.HiddenBySystem = false goes to Gui.__DrifterFirstPassHidden(gui, false):
    //   if (!value && GuiReshows.Marked(gui) && gui.Visible && gui.HiddenBySystem && !gui.PauseGame &&
    //       GuiReshows.Reshow(gui.m_instance, gui.GetScript())) return;
    //   gui.HiddenBySystem = value;
    // Gui.OnVisibilityChanged's SetActive goes to GuiReshows.SetActive (notes what has had Awake).
    static int SkipGuiReshows(ModuleDefinition module)
    {
        TypeDefinition quest = GameType(module, "PowerTools.Quest.PowerQuest");
        TypeDefinition gui = GameType(module, "PowerTools.Quest.Gui");
        if (quest == null || gui == null) return 0;
        MethodDefinition update = quest.Methods.Single(m => m.Name == "UpdateGuiVisibility" && !m.HasParameters);
        MethodDefinition changed = gui.Methods.Single(m => m.Name == "OnVisibilityChanged" && !m.HasParameters);
        MethodDefinition setHidden = gui.Methods.Single(m => m.Name == "set_HiddenBySystem" && m.Parameters.Count == 1);
        MethodDefinition getHidden = gui.Methods.Single(m => m.Name == "get_HiddenBySystem" && !m.HasParameters);
        MethodDefinition getVisible = gui.Methods.Single(m => m.Name == "get_Visible" && !m.HasParameters);
        MethodDefinition getPause = gui.Methods.Single(m => m.Name == "get_PauseGame" && !m.HasParameters);
        MethodDefinition getHideList = gui.Methods.Single(m => m.Name == "get_HideSpecificGuis" && !m.HasParameters);
        MethodDefinition getScript = gui.Methods.Single(m => m.Name == "GetScript" && !m.HasParameters && !m.HasGenericParameters);
        FieldDefinition instance = gui.Fields.Single(f => f.Name == "m_instance");
        FieldDefinition sorted = quest.Fields.Single(f => f.Name == "m_sortedGuis");
        var code = update.Body.Instructions;
        Instruction[] sets = code.Where(i => CalledFullName(i) == setHidden.FullName).ToArray();
        MethodReference getGui = code.Select(i => i.Operand as MethodReference).FirstOrDefault(r => r != null && r.Name == "GetGui");
        Instruction[] enumerate = code.Where(i => CalledName(i) == "GetEnumerator").ToArray();
        Instruction[] activations = changed.Body.Instructions.Where(i => CalledFullName(i) == "System.Void UnityEngine.GameObject::SetActive(System.Boolean)").ToArray();
        var listType = sorted.FieldType as GenericInstanceType;
        if (sets.Length != 4 || sets[2].Previous == null || sets[2].Previous.OpCode != OpCodes.Ldc_I4_0 ||
            sets.Take(2).Any(s => s.Previous == null || s.Previous.OpCode != OpCodes.Ldc_I4_1) || sets[3].Previous.OpCode != OpCodes.Ldc_I4_1 ||
            getGui == null || getGui.Parameters.Count != 1 || getGui.ReturnType.FullName != gui.FullName || enumerate.Length != 2 ||
            enumerate[0].Previous == null || enumerate[0].Previous.Operand != sorted || enumerate[0].Previous.Previous.OpCode != OpCodes.Ldarg_0 ||
            activations.Length != 1 || listType == null || listType.GenericArguments[0].FullName != gui.FullName ||
            instance.FieldType.Resolve() == null || getScript.ReturnType.IsValueType)
            throw new Exception("PowerQuest.UpdateGuiVisibility does not have the expected shape");
        MethodDefinition mark = MarkReshowsMethod(module, gui, sorted, listType, getVisible, getHideList, getGui);
        MethodDefinition first = FirstPassHiddenMethod(module, gui, instance, getVisible, getHidden, getPause, getScript, setHidden);
        sets[2].OpCode = OpCodes.Call;
        sets[2].Operand = first;
        Instruction anchor = enumerate[0].Previous.Previous;
        InsertBeforeRetarget(update, anchor, Instruction.Create(OpCodes.Ldarg_0), Instruction.Create(OpCodes.Call, mark));
        activations[0].OpCode = OpCodes.Call;
        activations[0].Operand = CompatMethod(module, "GuiReshows", "SetActive");
        return 3;
    }

    // PowerQuest.__DrifterMarkReshows() (see SkipGuiReshows).
    static MethodDefinition MarkReshowsMethod(ModuleDefinition module, TypeDefinition gui, FieldDefinition sorted, GenericInstanceType listType,
                                              MethodDefinition getVisible, MethodDefinition getHideList, MethodReference getGui)
    {
        Func<string, MethodReference> helper = name => CompatMethod(module, "GuiReshows", name);
        var mark = new MethodDefinition("__DrifterMarkReshows", MethodAttributes.Private | MethodAttributes.HideBySig, module.TypeSystem.Void);
        var index = new VariableDefinition(module.TypeSystem.Int32);
        var names = new VariableDefinition(getHideList.ReturnType);
        var at = new VariableDefinition(module.TypeSystem.Int32);
        var found = new VariableDefinition(gui);
        foreach (var variable in new[] { index, names, at, found }) mark.Body.Variables.Add(variable);
        mark.Body.InitLocals = true;
        ILProcessor il = mark.Body.GetILProcessor();
        Instruction outerTest = il.Create(OpCodes.Ldloc, index), outerBody = il.Create(OpCodes.Ldarg_0), outerNext = il.Create(OpCodes.Ldloc, index),
                    innerTest = il.Create(OpCodes.Ldloc, at), innerBody = il.Create(OpCodes.Ldarg_0), innerNext = il.Create(OpCodes.Ldloc, at);
        // No values on the stack across branches: IL2CPP merges them badly.
        il.Append(il.Create(OpCodes.Call, helper("BeginPass")));
        il.Append(il.Create(OpCodes.Ldc_I4_0)); il.Append(il.Create(OpCodes.Stloc, index));
        il.Append(il.Create(OpCodes.Br, outerTest));
        il.Append(outerBody);
        il.Append(il.Create(OpCodes.Ldfld, sorted)); il.Append(il.Create(OpCodes.Ldloc, index)); il.Append(il.Create(OpCodes.Callvirt, ListMethod(module, listType, "get_Item")));
        il.Append(il.Create(OpCodes.Callvirt, getVisible)); il.Append(il.Create(OpCodes.Brfalse, outerNext));
        il.Append(il.Create(OpCodes.Ldarg_0)); il.Append(il.Create(OpCodes.Ldfld, sorted)); il.Append(il.Create(OpCodes.Ldloc, index));
        il.Append(il.Create(OpCodes.Callvirt, ListMethod(module, listType, "get_Item"))); il.Append(il.Create(OpCodes.Callvirt, getHideList)); il.Append(il.Create(OpCodes.Stloc, names));
        il.Append(il.Create(OpCodes.Ldloc, names)); il.Append(il.Create(OpCodes.Brfalse, outerNext));
        il.Append(il.Create(OpCodes.Ldc_I4_0)); il.Append(il.Create(OpCodes.Stloc, at));
        il.Append(il.Create(OpCodes.Br, innerTest));
        il.Append(innerBody);
        il.Append(il.Create(OpCodes.Ldloc, names)); il.Append(il.Create(OpCodes.Ldloc, at)); il.Append(il.Create(OpCodes.Ldelem_Ref));
        il.Append(il.Create(OpCodes.Call, getGui)); il.Append(il.Create(OpCodes.Stloc, found));
        il.Append(il.Create(OpCodes.Ldloc, found)); il.Append(il.Create(OpCodes.Brfalse, innerNext));
        il.Append(il.Create(OpCodes.Ldloc, found)); il.Append(il.Create(OpCodes.Call, helper("Mark")));
        il.Append(innerNext);
        il.Append(il.Create(OpCodes.Ldc_I4_1)); il.Append(il.Create(OpCodes.Add)); il.Append(il.Create(OpCodes.Stloc, at));
        il.Append(innerTest);
        il.Append(il.Create(OpCodes.Ldloc, names)); il.Append(il.Create(OpCodes.Ldlen)); il.Append(il.Create(OpCodes.Conv_I4));
        il.Append(il.Create(OpCodes.Blt, innerBody));
        il.Append(outerNext);
        il.Append(il.Create(OpCodes.Ldc_I4_1)); il.Append(il.Create(OpCodes.Add)); il.Append(il.Create(OpCodes.Stloc, index));
        il.Append(outerTest);
        il.Append(il.Create(OpCodes.Ldarg_0)); il.Append(il.Create(OpCodes.Ldfld, sorted)); il.Append(il.Create(OpCodes.Callvirt, ListMethod(module, listType, "get_Count")));
        il.Append(il.Create(OpCodes.Blt, outerBody));
        il.Append(il.Create(OpCodes.Ret));
        sorted.DeclaringType.Methods.Add(mark);
        return mark;
    }

    // static Gui.__DrifterFirstPassHidden(Gui gui, bool value) (see SkipGuiReshows).
    static MethodDefinition FirstPassHiddenMethod(ModuleDefinition module, TypeDefinition gui, FieldDefinition instance, MethodDefinition getVisible,
                                                  MethodDefinition getHidden, MethodDefinition getPause, MethodDefinition getScript, MethodDefinition setHidden)
    {
        Func<string, MethodReference> helper = name => CompatMethod(module, "GuiReshows", name);
        var first = new MethodDefinition("__DrifterFirstPassHidden", MethodAttributes.Assembly | MethodAttributes.Static | MethodAttributes.HideBySig, module.TypeSystem.Void);
        first.Parameters.Add(new ParameterDefinition("gui", ParameterAttributes.None, gui));
        first.Parameters.Add(new ParameterDefinition("value", ParameterAttributes.None, module.TypeSystem.Boolean));
        ILProcessor il = first.Body.GetILProcessor();
        Instruction assign = il.Create(OpCodes.Ldarg_0);
        il.Append(il.Create(OpCodes.Ldarg_1)); il.Append(il.Create(OpCodes.Brtrue, assign));
        il.Append(il.Create(OpCodes.Ldarg_0)); il.Append(il.Create(OpCodes.Call, helper("Marked"))); il.Append(il.Create(OpCodes.Brfalse, assign));
        il.Append(il.Create(OpCodes.Ldarg_0)); il.Append(il.Create(OpCodes.Callvirt, getVisible)); il.Append(il.Create(OpCodes.Brfalse, assign));
        il.Append(il.Create(OpCodes.Ldarg_0)); il.Append(il.Create(OpCodes.Callvirt, getHidden)); il.Append(il.Create(OpCodes.Brfalse, assign));
        il.Append(il.Create(OpCodes.Ldarg_0)); il.Append(il.Create(OpCodes.Callvirt, getPause)); il.Append(il.Create(OpCodes.Brtrue, assign));
        il.Append(il.Create(OpCodes.Ldarg_0)); il.Append(il.Create(OpCodes.Ldfld, instance));
        il.Append(il.Create(OpCodes.Ldarg_0)); il.Append(il.Create(OpCodes.Callvirt, getScript));
        il.Append(il.Create(OpCodes.Call, helper("Reshow"))); il.Append(il.Create(OpCodes.Brfalse, assign));
        il.Append(il.Create(OpCodes.Ret));
        il.Append(assign);
        il.Append(il.Create(OpCodes.Ldarg_1)); il.Append(il.Create(OpCodes.Callvirt, setHidden));
        il.Append(il.Create(OpCodes.Ret));
        gui.Methods.Add(first);
        return first;
    }

    // RegionComponent.UpdateCharacterOnRegionState(index, background) runs for every region and
    // character every frame: PowerQuest's main loop (at the end of the frame) calls it for all
    // characters, about 18 of them, so EscCorridor's 66 regions make 1,155 calls a frame and
    // EscReactor's 34 make 595 (PostLateUpdate.PlayerSendFrameComplete on the PS Vita: 2.9 and
    // 1.7 ms, 0.4 ms in Drowning). A call answers None when the index is past the current
    // mask's length; otherwise it compares the character's bit in the current mask with its bit
    // in the old mask for that background (Stay when both are set, Exit when only the old one
    // is, Enter when only the current one is, None when neither), copies the current bit into
    // the old mask and returns the answer. It fetched the region's masks five times on the way
    // and read the current mask's bit twice. The getters return a field and nothing between
    // the two reads changes the mask, so the body becomes the same reads and writes on each
    // mask fetched once, in the same order (Count, Get, Get on the old mask, Set with the bit
    // read first). The BitArrays are saved with the region, so the same Set (and its version
    // count) still runs.
    static int FastRegionStates(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.RegionComponent");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "UpdateCharacterOnRegionState" && m.Parameters.Count == 2);
        FieldDefinition data = type.Fields.Single(f => f.Name == "m_data");
        TypeDefinition region = data.FieldType.Resolve();
        MethodDefinition getMask = region.Methods.Single(m => m.Name == "GetCharacterOnRegionMask" && !m.HasParameters);
        MethodDefinition getOld = region.Methods.Single(m => m.Name == "GetCharacterOnRegionMaskOld" && m.Parameters.Count == 1);
        Func<Instruction, string> called = CalledFullName;
        var code = method.Body.Instructions;
        MethodReference count = code.Select(i => i.Operand as MethodReference).FirstOrDefault(r => r != null && r.Name == "get_Count");
        MethodReference get = code.Select(i => i.Operand as MethodReference).FirstOrDefault(r => r != null && r.Name == "Get" && r.DeclaringType.FullName == "System.Collections.BitArray");
        MethodReference set = code.Select(i => i.Operand as MethodReference).FirstOrDefault(r => r != null && r.Name == "Set" && r.DeclaringType.FullName == "System.Collections.BitArray");
        TypeDefinition result = method.ReturnType.Resolve();
        Func<string, int> value = name => (int)result.Fields.Single(f => f.Name == name).Constant;
        if (count == null || get == null || set == null || count.DeclaringType.FullName != "System.Collections.BitArray" ||
            code.Count(i => called(i) == getMask.FullName) != 3 || code.Count(i => called(i) == getOld.FullName) != 2 ||
            code.Count(i => i.Operand == get) != 3 || code.Count(i => i.Operand == set) != 1 || code.Count(i => i.Operand == count) != 1 ||
            method.Parameters[0].ParameterType.FullName != "System.Int32" || method.Parameters[1].ParameterType.FullName != "System.Boolean" ||
            method.Body.HasExceptionHandlers || value("None") != 0)
            throw new Exception("RegionComponent.UpdateCharacterOnRegionState does not have the expected shape");
        method.Body.Instructions.Clear();
        method.Body.Variables.Clear();
        var bits = new VariableDefinition(count.DeclaringType);
        var old = new VariableDefinition(count.DeclaringType);
        var inRegion = new VariableDefinition(module.TypeSystem.Boolean);
        var answer = new VariableDefinition(method.ReturnType);
        foreach (var variable in new[] { bits, old, inRegion, answer }) method.Body.Variables.Add(variable);
        method.Body.InitLocals = true;
        ILProcessor il = method.Body.GetILProcessor();
        Instruction go = il.Create(OpCodes.Nop), notOld = il.Create(OpCodes.Nop), stay = il.Create(OpCodes.Nop),
                    none = il.Create(OpCodes.Nop), store = il.Create(OpCodes.Nop);
        // No values on the stack across branches: IL2CPP merges them badly.
        il.Emit(OpCodes.Ldarg_0); il.Emit(OpCodes.Ldfld, data); il.Emit(OpCodes.Callvirt, module.ImportReference(getMask)); il.Emit(OpCodes.Stloc, bits);
        il.Emit(OpCodes.Ldarg_1); il.Emit(OpCodes.Ldloc, bits); il.Emit(OpCodes.Callvirt, count); il.Emit(OpCodes.Blt, go);
        il.Emit(OpCodes.Ldc_I4, value("None")); il.Emit(OpCodes.Ret);
        il.Append(go);
        il.Emit(OpCodes.Ldloc, bits); il.Emit(OpCodes.Ldarg_1); il.Emit(OpCodes.Callvirt, get); il.Emit(OpCodes.Stloc, inRegion);
        il.Emit(OpCodes.Ldarg_0); il.Emit(OpCodes.Ldfld, data); il.Emit(OpCodes.Ldarg_2); il.Emit(OpCodes.Callvirt, module.ImportReference(getOld)); il.Emit(OpCodes.Stloc, old);
        il.Emit(OpCodes.Ldloc, old); il.Emit(OpCodes.Ldarg_1); il.Emit(OpCodes.Callvirt, get); il.Emit(OpCodes.Brfalse, notOld);
        il.Emit(OpCodes.Ldloc, inRegion); il.Emit(OpCodes.Brtrue, stay);
        il.Emit(OpCodes.Ldc_I4, value("Exit")); il.Emit(OpCodes.Stloc, answer); il.Emit(OpCodes.Br, store);
        il.Append(stay);
        il.Emit(OpCodes.Ldc_I4, value("Stay")); il.Emit(OpCodes.Stloc, answer); il.Emit(OpCodes.Br, store);
        il.Append(notOld);
        il.Emit(OpCodes.Ldloc, inRegion); il.Emit(OpCodes.Brfalse, none);
        il.Emit(OpCodes.Ldc_I4, value("Enter")); il.Emit(OpCodes.Stloc, answer); il.Emit(OpCodes.Br, store);
        il.Append(none);
        il.Emit(OpCodes.Ldc_I4, value("None")); il.Emit(OpCodes.Stloc, answer);
        il.Append(store);
        il.Emit(OpCodes.Ldloc, old); il.Emit(OpCodes.Ldarg_1); il.Emit(OpCodes.Ldloc, inRegion); il.Emit(OpCodes.Callvirt, set);
        il.Emit(OpCodes.Ldloc, answer); il.Emit(OpCodes.Ret);
        return 1;
    }

    // PowerTools' Pathfinder (see Compat PathMath): HasLineOfSight's per-edge intersection
    // tests, IsPointInPoly and LineIntersector.HasIntersection run the same float operations
    // in Compat without a call, struct copies and bounds checks per edge. Stick walking finds
    // two paths a frame, and each tests the segments from both new nodes to every node.
    // HasLineOfSight(a, b) becomes:
    //   if (PathMath.Near(a, b)) return true;
    //   test m_pathPolys[PathMath.LastPoly] first (if in range), then the others:
    //   for (int i = 0; i < m_pathPolys.Count; i++)
    //       if (m_pathPolys[i].m_enabled && PathMath.SegmentCrosses(a, b, m_pathPolys[i].m_verts))
    //           { PathMath.LastPoly = i; return false; }
    //   return true;
    // The answer is whether any enabled polygon crosses, so the order cannot change it; a
    // line blocked once is mostly blocked by the same polygon from nearby points. The original
    // also loads the segment into its private LineIntersector, which nothing else reads.
    // RedirectPathMath does the rest.
    static int FastPathfinder(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Pathfinder");
        if (type == null) return 0;
        TypeDefinition math = compat["DrifterCompat.PathMath"];
        TypeDefinition poly = type.NestedTypes.Single(n => n.Name == "PathPoly");
        FieldDefinition polys = type.Fields.Single(f => f.Name == "m_pathPolys");
        FieldDefinition enabled = poly.Fields.Single(f => f.Name == "m_enabled");
        FieldDefinition verts = poly.Fields.Single(f => f.Name == "m_verts");
        MethodDefinition sight = type.Methods.Single(m => m.Name == "HasLineOfSight" && m.Parameters.Count == 2);
        var code = sight.Body.Instructions;
        Instruction getItem = code.FirstOrDefault(i => Calls(i, polys.FieldType.FullName, "get_Item"));
        Instruction getCount = code.FirstOrDefault(i => Calls(i, polys.FieldType.FullName, "get_Count"));
        if (getItem == null || getCount == null || sight.Parameters.Any(p => p.ParameterType.FullName != "UnityEngine.Vector2") ||
            !code.Any(i => CalledName(i) == "Calculate") || !code.Any(i => CalledName(i) == "SetFirstLine") ||
            !code.Any(i => i.Operand == enabled) || !code.Any(i => i.Operand == verts) || sight.ReturnType.FullName != "System.Boolean")
            throw new Exception("Pathfinder.HasLineOfSight does not have the expected shape");
        MethodReference itemCall = (MethodReference)getItem.Operand, countCall = (MethodReference)getCount.Operand;
        Func<string, MethodReference> helper = name => CompatMethod(module, "PathMath", name);
        code.Clear();
        sight.Body.ExceptionHandlers.Clear();
        sight.Body.Variables.Clear();
        var index = new VariableDefinition(module.TypeSystem.Int32);
        var item = new VariableDefinition(poly);
        var first = new VariableDefinition(module.TypeSystem.Int32);
        sight.Body.Variables.Add(index);
        sight.Body.Variables.Add(item);
        sight.Body.Variables.Add(first);
        sight.Body.InitLocals = true;
        FieldReference lastPoly = module.ImportReference(math.Fields.Single(f => f.Name == "LastPoly"));
        ILProcessor il = sight.Body.GetILProcessor();
        Instruction afterNear = il.Create(OpCodes.Ldsfld, lastPoly), loop = il.Create(OpCodes.Ldc_I4_0),
            condition = il.Create(OpCodes.Ldloc, index), body = il.Create(OpCodes.Ldloc, index), next = il.Create(OpCodes.Ldloc, index);
        // if (PathMath.Near(a, b)) return true;
        il.Emit(OpCodes.Ldarg_1);
        il.Emit(OpCodes.Ldarg_2);
        il.Emit(OpCodes.Call, helper("Near"));
        il.Emit(OpCodes.Brfalse, afterNear);
        il.Emit(OpCodes.Ldc_I4_1);
        il.Emit(OpCodes.Ret);
        // first = PathMath.LastPoly; if ((uint)first < (uint)m_pathPolys.Count) { item = m_pathPolys[first];
        //     if (item.m_enabled && PathMath.SegmentCrosses(a, b, item.m_verts)) return false; }
        il.Append(afterNear);
        il.Emit(OpCodes.Stloc, first);
        il.Emit(OpCodes.Ldloc, first);
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldfld, polys);
        il.Emit(OpCodes.Callvirt, countCall);
        il.Emit(OpCodes.Bge_Un, loop);
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldfld, polys);
        il.Emit(OpCodes.Ldloc, first);
        il.Emit(OpCodes.Callvirt, itemCall);
        il.Emit(OpCodes.Stloc, item);
        il.Emit(OpCodes.Ldloc, item);
        il.Emit(OpCodes.Ldfld, enabled);
        il.Emit(OpCodes.Brfalse, loop);
        il.Emit(OpCodes.Ldarg_1);
        il.Emit(OpCodes.Ldarg_2);
        il.Emit(OpCodes.Ldloc, item);
        il.Emit(OpCodes.Ldfld, verts);
        il.Emit(OpCodes.Call, helper("SegmentCrosses"));
        il.Emit(OpCodes.Brfalse, loop);
        il.Emit(OpCodes.Ldc_I4_0);
        il.Emit(OpCodes.Ret);
        // for (index = 0; index < m_pathPolys.Count; index++) { if (index == first) continue;
        //     item = m_pathPolys[index]; if (item.m_enabled && PathMath.SegmentCrosses(a, b, item.m_verts))
        //     { PathMath.LastPoly = index; return false; } }
        il.Append(loop);
        il.Emit(OpCodes.Stloc, index);
        il.Emit(OpCodes.Br, condition);
        il.Append(body);
        il.Emit(OpCodes.Ldloc, first);
        il.Emit(OpCodes.Beq, next);
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldfld, polys);
        il.Emit(OpCodes.Ldloc, index);
        il.Emit(OpCodes.Callvirt, itemCall);
        il.Emit(OpCodes.Stloc, item);
        il.Emit(OpCodes.Ldloc, item);
        il.Emit(OpCodes.Ldfld, enabled);
        il.Emit(OpCodes.Brfalse, next);
        il.Emit(OpCodes.Ldarg_1);
        il.Emit(OpCodes.Ldarg_2);
        il.Emit(OpCodes.Ldloc, item);
        il.Emit(OpCodes.Ldfld, verts);
        il.Emit(OpCodes.Call, helper("SegmentCrosses"));
        il.Emit(OpCodes.Brfalse, next);
        il.Emit(OpCodes.Ldloc, index);
        il.Emit(OpCodes.Stsfld, lastPoly);
        il.Emit(OpCodes.Ldc_I4_0);
        il.Emit(OpCodes.Ret);
        il.Append(next);
        il.Emit(OpCodes.Ldc_I4_1);
        il.Emit(OpCodes.Add);
        il.Emit(OpCodes.Stloc, index);
        il.Append(condition);
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldfld, polys);
        il.Emit(OpCodes.Callvirt, countCall);
        il.Emit(OpCodes.Blt, body);
        il.Emit(OpCodes.Ldc_I4_1);
        il.Emit(OpCodes.Ret);
        return 1;
    }

    // Calls to IsPointInPoly and LineIntersector.HasIntersection in Pathfinder go to Compat
    // PathMath, which runs the same float operations (see FastPathfinder).
    static int RedirectPathMath(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Pathfinder");
        if (type == null) return 0;
        var redirects = new Dictionary<string, string>
        {
            { "System.Boolean PowerTools.Pathfinder::IsPointInPoly(UnityEngine.Vector2[],UnityEngine.Vector2)", "IsPointInPoly" },
            { "System.Boolean PowerTools.LineIntersector::HasIntersection(UnityEngine.Vector2,UnityEngine.Vector2,UnityEngine.Vector2,UnityEngine.Vector2)", "HasIntersection" },
        };
        int count = 0;
        foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
            foreach (Instruction instruction in method.Body.Instructions)
            {
                string target;
                if (instruction.OpCode != OpCodes.Call || !redirects.TryGetValue(CalledFullName(instruction), out target)) continue;
                instruction.Operand = CompatMethod(module, "PathMath", target);
                count++;
            }
        return count;
    }

    // get_Count or get_Item of a List<T> type, imported.
    static MethodReference ListMethod(ModuleDefinition module, GenericInstanceType listType, string name)
    {
        MethodDefinition definition = listType.Resolve().Methods.Single(m => m.Name == name && m.Parameters.Count == (name == "get_Item" ? 1 : 0));
        var reference = new MethodReference(definition.Name, definition.ReturnType, listType) { HasThis = true };
        foreach (ParameterDefinition parameter in definition.Parameters) reference.Parameters.Add(new ParameterDefinition(parameter.ParameterType));
        return module.ImportReference(reference);
    }

    // PowerTools' TextWrapper.GetTextWidth(s) becomes Compat TextWidths.Measure(textMesh,
    // renderer, dict, s): the same widths from the same Hashtable, without setting the
    // TextMesh's text back when no character had to be measured (each set rebuilds its mesh).
    static int FastTextWidths(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.TextWrapper");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "GetTextWidth" && m.Parameters.Count == 1);
        FieldDefinition mesh = type.Fields.Single(f => f.Name == "textMesh"), renderer = type.Fields.Single(f => f.Name == "renderer"),
            dict = type.Fields.Single(f => f.Name == "dict");
        Func<Instruction, string> called = CalledName;
        var code = method.Body.Instructions;
        if (mesh.FieldType.FullName != "UnityEngine.TextMesh" || renderer.FieldType.FullName != "UnityEngine.Renderer" ||
            dict.FieldType.FullName != "System.Collections.Hashtable" || method.ReturnType.FullName != "System.Single" ||
            method.Parameters[0].ParameterType.FullName != "System.String" || method.Body.HasExceptionHandlers ||
            code.Count(i => called(i) == "set_text") != 2 || code.Count(i => called(i) == "get_text") != 1 ||
            !code.Any(i => called(i) == "ContainsKey") || !code.Any(i => called(i) == "get_bounds") || !code.Any(i => called(i) == "ToCharArray"))
            throw new Exception("TextWrapper.GetTextWidth does not have the expected shape");
        code.Clear();
        method.Body.Variables.Clear();
        ILProcessor il = method.Body.GetILProcessor();
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldfld, mesh);
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldfld, renderer);
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldfld, dict);
        il.Emit(OpCodes.Ldarg_1);
        il.Emit(OpCodes.Call, CompatMethod(module, "TextWidths", "Measure"));
        il.Emit(OpCodes.Ret);
        return 1;
    }

    // SystemInput.GetControllerClickableAngles: after the first loop (each selectable's angle
    // around the look origin), Compat ClickableAngles.Find may return the last result for the
    // same angles and selection angle; otherwise the spreading runs as before and its result
    // is passed to ClickableAngles.Store before it is returned.
    static int CacheClickableAngles(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "SystemInput");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "GetControllerClickableAngles" && m.Parameters.Count == 1);
        var code = method.Body.Instructions;
        Instruction[] selects = code.Where(i => i.OpCode == OpCodes.Ldfld && ((FieldReference)i.Operand).Name == "m_controllerSelectAngle").ToArray();
        Instruction newArray = code.FirstOrDefault(i => i.OpCode == OpCodes.Newarr && ((TypeReference)i.Operand).FullName == "System.Single");
        Instruction[] returns = code.Where(i => i.OpCode == OpCodes.Ret).ToArray();
        VariableDefinition array = newArray == null ? null : LocalOf(method, newArray.Next, true);
        Instruction select = selects.Length == 1 ? selects[0] : null;
        if (select == null || array == null || returns.Length != 1 || select.Previous == null || select.Previous.OpCode != OpCodes.Ldarg_0 ||
            returns[0].Previous == null || LocalOf(method, returns[0].Previous, false) != array || method.Body.HasExceptionHandlers ||
            !code.Any(i => CalledName(i) == "ControllerAnglePush") || code.IndexOf(select) < code.IndexOf(code.First(i => CalledName(i) == "GetDirectionAngle")))
            throw new Exception("SystemInput.GetControllerClickableAngles does not have the expected shape");
        ILProcessor il = method.Body.GetILProcessor();
        // No values on the stack across branches: IL2CPP merges them badly.
        var cached = new VariableDefinition(method.ReturnType);
        method.Body.Variables.Add(cached);
        method.Body.InitLocals = true;
        Instruction spread = il.Create(OpCodes.Nop);
        InsertBeforeRetarget(method, select.Previous,
            il.Create(OpCodes.Ldloc, array), il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldfld, (FieldReference)select.Operand),
            il.Create(OpCodes.Call, CompatMethod(module, "ClickableAngles", "Find")),
            il.Create(OpCodes.Stloc, cached), il.Create(OpCodes.Ldloc, cached), il.Create(OpCodes.Brfalse, spread),
            il.Create(OpCodes.Ldloc, cached), il.Create(OpCodes.Ret), spread);
        InsertBeforeRetarget(method, returns[0].Previous,
            il.Create(OpCodes.Ldloc, array), il.Create(OpCodes.Call, CompatMethod(module, "ClickableAngles", "Store")));
        return 1;
    }

    // CharacterComponent.FindDirectionalAnimationName(facing, name, out flip, out animFacing)
    // starts with a loop over GetAnimations() that sets the directions the animation has in a
    // BitMask local. Before the loop, Compat AnimDirections.Find(this, name)
    // may give the mask found before (the loop is skipped); after the loop, Store keeps it.
    static int CacheAnimationDirections(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.CharacterComponent");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "FindDirectionalAnimationName" && m.Parameters.Count == 4);
        var code = method.Body.Instructions;
        Func<Instruction, string> called = CalledName;
        Instruction init = code.FirstOrDefault(i => i.OpCode == OpCodes.Initobj);
        VariableDefinition mask = init == null || init.Previous == null ? null : init.Previous.Operand as VariableDefinition;
        // The loop: ldc.i4.0; stloc i; br <condition> ... <condition>: ldloc i; ldarg.0; call GetAnimations;
        // callvirt get_Count; blt <body>; then the code after the loop.
        Instruction jump = init == null ? null : code.Skip(code.IndexOf(init)).FirstOrDefault(i => i.OpCode == OpCodes.Br || i.OpCode == OpCodes.Br_S);
        Instruction condition = jump == null ? null : (Instruction)jump.Operand;
        Instruction back = condition == null ? null : code.Skip(code.IndexOf(condition)).FirstOrDefault(i => i.OpCode == OpCodes.Blt || i.OpCode == OpCodes.Blt_S);
        Instruction start = jump == null ? null : jump.Previous == null ? null : jump.Previous.Previous;
        if (mask == null || init.Previous.OpCode != OpCodes.Ldloca_S && init.Previous.OpCode != OpCodes.Ldloca || mask.VariableType.Name != "BitMask" ||
            start == null || start.OpCode != OpCodes.Ldc_I4_0 || back == null || back.Next == null || (Instruction)back.Operand != jump.Next ||
            !code.Any(i => called(i) == "GetAnimations") || method.Parameters[1].ParameterType.FullName != "System.String" || method.Body.HasExceptionHandlers ||
            !code.Skip(code.IndexOf(condition)).Take(code.IndexOf(back) - code.IndexOf(condition)).Any(i => called(i) == "get_Count") ||
            code.Count(i => called(i) == "SetAt") != 3 || code.Skip(code.IndexOf(back)).Any(i => called(i) == "SetAt") ||
            code.Take(code.IndexOf(start)).Any(i => called(i) == "SetAt"))
            throw new Exception("CharacterComponent.FindDirectionalAnimationName does not have the expected shape");
        // BitMask's Value property, by reference on the local's type.
        var setValue = new MethodReference("set_Value", module.TypeSystem.Void, mask.VariableType) { HasThis = true };
        setValue.Parameters.Add(new ParameterDefinition(module.TypeSystem.Int32));
        var getValue = new MethodReference("get_Value", module.TypeSystem.Int32, mask.VariableType) { HasThis = true };
        var found = new VariableDefinition(module.TypeSystem.Int32);
        method.Body.Variables.Add(found);
        ILProcessor il = method.Body.GetILProcessor();
        Instruction after = back.Next;
        Instruction skip = il.Create(OpCodes.Nop), scan = il.Create(OpCodes.Nop);
        // After the loop: AnimDirections.Store(this, name, mask.Value); the cached path joins after it.
        InsertBeforeRetarget(method, after,
            il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldarg_2),
            il.Create(OpCodes.Ldloca, mask), il.Create(OpCodes.Call, getValue),
            il.Create(OpCodes.Call, CompatMethod(module, "AnimDirections", "Store")), skip);
        // Before the loop: found = AnimDirections.Find(this, name); if (found != -1) { mask.Value = found; skip the loop }
        // No values on the stack across branches: IL2CPP merges them badly.
        InsertBeforeRetarget(method, start,
            il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldarg_2),
            il.Create(OpCodes.Call, CompatMethod(module, "AnimDirections", "Find")),
            il.Create(OpCodes.Stloc, found), il.Create(OpCodes.Ldloc, found), il.Create(OpCodes.Ldc_I4_M1), il.Create(OpCodes.Beq, scan),
            il.Create(OpCodes.Ldloca, mask), il.Create(OpCodes.Ldloc, found),
            il.Create(OpCodes.Call, setValue), il.Create(OpCodes.Br, skip), scan);
        return 1;
    }

    // CharacterComponent.UpdateLipSync, while the character is Talking with a mouth animation:
    //   (B) m_mouth.gameObject.SetActive(true); set its sorting order; find the line's text data
    //       and dialog AudioSource;
    //   (D) if the source has stopped, or (E) there is none but there was one for this line:
    //       m_mouth.gameObject.SetActive(false); return;
    //   (F) set the mouth's pose from the lip sync data.
    // A line that waits for the player after its voice ended takes path E every frame, so the
    // mouth is activated and deactivated again each frame. On the PS Vita that pair cost more
    // and more (its Animator enabled and disabled each frame): 12.5 to 17.6 ms a frame over
    // 25 s with Mick's line waiting in Hospital, and the same in PaperOffice. Nothing between B
    // and D/E depends on whether the mouth is active (the sorting order is set either way), so
    // the activation moves to the start of F: paths D and E then leave an inactive mouth
    // inactive, and F starts with the mouth active as before.
    static int DeferMouthActivation(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.CharacterComponent");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "UpdateLipSync" && !m.HasParameters);
        var code = method.Body.Instructions;
        Func<Instruction, string> called = CalledName;
        Func<Instruction, string> field = i => i.Operand is FieldReference ? ((FieldReference)i.Operand).Name : "";
        // Paths D and E each deactivate the mouth; F's first call, a few instructions in, is
        // GetLipSyncAnimTime.
        const int deactivations = 2, lipSyncCallWithin = 4;
        Instruction[] activations = code.Where(i => called(i) == "SetActive" && i.Previous != null && i.Previous.OpCode == OpCodes.Ldc_I4_1).ToArray();
        Instruction set = activations.Length == 1 ? activations[0] : null;
        // ldarg.0; ldfld m_mouth; callvirt get_gameObject; ldc.i4.1; callvirt SetActive
        Instruction start = set == null ? null : set.Previous.Previous.Previous.Previous;
        Instruction guard = start == null ? null : start.Previous;
        Instruction flag = guard == null ? null : guard.Previous;
        Instruction hadSource = code.FirstOrDefault(i => i.OpCode == OpCodes.Ldfld && field(i) == "m_voHadAudioSource");
        Instruction skip = hadSource == null ? null : hadSource.Next;
        Instruction lipSync = skip == null ? null : skip.Operand as Instruction;
        VariableDefinition flagLocal = flag == null ? null : LocalOf(method, flag, false);
        if (set == null || start.OpCode != OpCodes.Ldarg_0 || field(start.Next) != "m_mouth" || called(start.Next.Next) != "get_gameObject" ||
            guard.OpCode != OpCodes.Brfalse && guard.OpCode != OpCodes.Brfalse_S || flagLocal == null || flagLocal.VariableType.FullName != "System.Boolean" ||
            skip.OpCode != OpCodes.Brfalse && skip.OpCode != OpCodes.Brfalse_S || lipSync == null ||
            code.IndexOf(lipSync) < code.IndexOf(skip) || code.IndexOf(set) > code.IndexOf(hadSource) || method.Body.HasExceptionHandlers ||
            code.Skip(code.IndexOf(set) + 1).Take(code.IndexOf(lipSync) - code.IndexOf(set)).Count(i => called(i) == "SetActive") != deactivations ||
            !code.Skip(code.IndexOf(lipSync)).Take(lipSyncCallWithin).Any(i => called(i) == "GetLipSyncAnimTime"))
            throw new Exception("CharacterComponent.UpdateLipSync does not have the expected shape");
        FieldReference mouth = (FieldReference)start.Next.Operand;
        MethodReference gameObject = (MethodReference)start.Next.Next.Operand, setActive = (MethodReference)set.Operand;
        foreach (Instruction part in new[] { start, start.Next, start.Next.Next, set.Previous, set }) Erase(part);
        ILProcessor il = method.Body.GetILProcessor();
        InsertBeforeRetarget(method, lipSync,
            il.Create(OpCodes.Ldloc, flagLocal), il.Create(OpCodes.Brfalse, lipSync), il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldfld, mouth),
            il.Create(OpCodes.Callvirt, gameObject), il.Create(OpCodes.Ldc_I4_1), il.Create(OpCodes.Callvirt, setActive));
        return 1;
    }

    // Enum.ToString() on an enum with an int underlying type goes to Compat EnumText<T>.Name,
    // which asks reflection once per value (InControl's InputDeviceStyle, every frame in
    // SystemInput.UpdateInput and GuiPlatformContent.Update). The IL loads the value's address
    // and calls ToString constrained to the enum, or boxes the value first.
    static int CacheEnumNames(ModuleDefinition module)
    {
        int count = 0;
        TypeDefinition text = compat["DrifterCompat.EnumText`1"];
        var loads = new Dictionary<Code, OpCode>
        {
            { Code.Ldloca, OpCodes.Ldloc }, { Code.Ldloca_S, OpCodes.Ldloc }, { Code.Ldarga, OpCodes.Ldarg }, { Code.Ldarga_S, OpCodes.Ldarg },
            { Code.Ldflda, OpCodes.Ldfld }, { Code.Ldsflda, OpCodes.Ldsfld },
        };
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
        foreach (Instruction call in method.Body.Instructions)
        {
            var called = call.Operand as MethodReference;
            if (call.OpCode != OpCodes.Callvirt || called == null || called.FullName != "System.String System.Object::ToString()") continue;
            Instruction prefix = call.Previous;
            if (prefix == null) continue;
            TypeReference enumType = prefix.OpCode == OpCodes.Constrained || prefix.OpCode == OpCodes.Box ? prefix.Operand as TypeReference : null;
            TypeDefinition definition;
            try { definition = enumType == null ? null : enumType.Resolve(); }
            catch (AssemblyResolutionException e) { skipped.Add("CacheEnumNames " + method.FullName + ": " + e.Message); continue; }
            if (definition == null || !definition.IsEnum || enumType.IsGenericParameter ||
                definition.Fields.First(f => f.Name == "value__").FieldType.FullName != "System.Int32")
                continue;
            if (prefix.OpCode == OpCodes.Constrained)
            {
                Instruction address = prefix.Previous;
                OpCode load;
                if (address == null || !loads.TryGetValue(address.OpCode.Code, out load) || method.Body.Instructions.Any(i => i.Operand == prefix))
                    continue;
                address.OpCode = load;
            }
            Erase(prefix);
            var name = new GenericInstanceType(module.ImportReference(text));
            name.GenericArguments.Add(enumType);
            call.OpCode = OpCodes.Call;
            call.Operand = new MethodReference("Name", module.TypeSystem.String, name)
            {
                HasThis = false,
                Parameters = { new ParameterDefinition(module.TypeSystem.Int32) },
            };
            count++;
        }
        return count;
    }
}
