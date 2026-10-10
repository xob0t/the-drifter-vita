using System;
using System.Collections.Generic;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

// InControl, and the PS Vita's controls, prompts, touches and intro skip.
// Part of the assembly rewriter; see DowngradeAssemblies.cs.
partial class DowngradeAssemblies
{
    // The game ships InControl's Windows build. On other platforms:
    // - its platform name is still the Windows version, so profiles for other platforms never
    //   apply (see Compat Platform.InputPlatform);
    // - joysticks without a name become unknown devices; the PS Vita reports "PS Vita" and three
    //   empty slots (see Compat Platform.JoystickNames);
    // - its PS Vita profile has the PlayStation Mobile layout, which reads the D-pad from axes 5
    //   and 6. Unity's PS Vita player reports it as joystick buttons 8-11 (up, right, down, left).
    static int PatchInControl(ModuleDefinition module)
    {
        int platforms = 0, names = 0, dpads = 0;
        var dpad = new Dictionary<string, int> { { "DPadUp", 8 }, { "DPadRight", 9 }, { "DPadDown", 10 }, { "DPadLeft", 11 } };
        TypeDefinition profileType = module.GetType("InControl.InputDeviceProfile");
        TypeDefinition mappingType = module.GetType("InControl.InputControlMapping");
        MethodDefinition button = profileType.Methods.First(m => m.Name == "Button");
        MethodDefinition setSource = mappingType.Methods.First(m => m.Name == "set_Source");
        MethodDefinition setSourceRange = mappingType.Methods.First(m => m.Name == "set_SourceRange");
        int zeroToOne = (int)module.GetType("InControl.InputRangeType").Fields.First(f => f.Name == "ZeroToOne").Constant;
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods)
        {
            if (!method.HasBody) continue;
            ILProcessor il = method.Body.GetILProcessor();
            foreach (Instruction instruction in method.Body.Instructions.ToArray())
            {
                var called = instruction.Operand as MethodReference;
                if (called == null) continue;
                if (called.DeclaringType.FullName == "InControl.Utility" && called.Name == "GetPlatformName")
                {
                    il.InsertAfter(instruction, il.Create(OpCodes.Call, CompatMethod(module, "Platform", "InputPlatform")));
                    platforms++;
                }
                else if (called.DeclaringType.FullName == "UnityEngine.Input" && called.Name == "GetJoystickNames")
                {
                    il.InsertAfter(instruction, il.Create(OpCodes.Call, CompatMethod(module, "Platform", "JoystickNames")));
                    names++;
                }
                else if (type.FullName == "InControl.UnityDeviceProfiles.PlayStationVitaPSMUnityProfile" && called.Name.StartsWith("DPad"))
                {
                    int index = dpad[called.Name.Substring(0, called.Name.IndexOf("Mapping"))];
                    InsertAfter(il, instruction, il.Create(OpCodes.Dup), il.Create(OpCodes.Ldc_I4, index), il.Create(OpCodes.Call, button),
                                il.Create(OpCodes.Callvirt, setSource), il.Create(OpCodes.Dup), il.Create(OpCodes.Ldc_I4, zeroToOne),
                                il.Create(OpCodes.Callvirt, setSourceRange));
                    dpads++;
                }
            }
        }
        Count("InControl Utility.GetPlatformName calls", platforms);
        Count("InControl Input.GetJoystickNames calls", names);
        if (dpads != dpad.Count) throw new Exception("InControl: expected the PS Vita profile's four D-pad mappings, found " + dpads);
        return platforms + names + dpads;
    }

    // UnityInputDevice.ReadRawButtonState reads each mapped button with
    // Input.GetKey(buttonQueries[JoystickId - 1, index]) every frame; the name goes to Compat
    // InputKeys.GetJoystickButton with the device's JoystickId and the index, which reads names
    // of the form "joystick N button M" through their KeyCode and any other through the name.
    static int FastJoystickButtons(ModuleDefinition module)
    {
        TypeDefinition device = module.GetType("InControl.UnityInputDevice");
        MethodDefinition read = device.Methods.First(m => m.Name == "ReadRawButtonState");
        MethodDefinition joystickId = device.Methods.First(m => m.Name == "get_JoystickId");
        MethodReference getKey = CompatMethod(module, "InputKeys", "GetJoystickButton");
        ILProcessor il = read.Body.GetILProcessor();
        Instruction[] calls = read.Body.Instructions.Where(i => i.Operand is MethodReference &&
            ((MethodReference)i.Operand).FullName == "System.Boolean UnityEngine.Input::GetKey(System.String)").ToArray();
        if (calls.Length != 1 || read.IsStatic || read.Parameters.Count != 1 || read.Parameters[0].ParameterType.FullName != "System.Int32")
            throw new Exception("InControl: UnityInputDevice.ReadRawButtonState does not have the expected shape");
        InsertBefore(il, calls[0], il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Call, joystickId), il.Create(OpCodes.Ldarg_1));
        calls[0].OpCode = OpCodes.Call;
        calls[0].Operand = getKey;
        // ReadRawAnalogValue's Input.GetAxisRaw(name): read once per frame and name (Compat InputKeys.GetAxisRaw).
        MethodDefinition analog = device.Methods.First(m => m.Name == "ReadRawAnalogValue");
        Instruction[] axes = analog.Body.Instructions.Where(i => i.Operand is MethodReference &&
            ((MethodReference)i.Operand).FullName == "System.Single UnityEngine.Input::GetAxisRaw(System.String)").ToArray();
        if (axes.Length != 1) throw new Exception("InControl: UnityInputDevice.ReadRawAnalogValue does not have the expected shape");
        axes[0].OpCode = OpCodes.Call;
        axes[0].Operand = CompatMethod(module, "InputKeys", "GetAxisRaw");
        return 2;
    }

    // The PS Vita has no triggers (see Compat Controls). In InControl's Vita profile, L and R
    // (buttons 4 and 5) become LeftTrigger and RightTrigger, and two mappings are added:
    // LeftBumper and RightBumper from virtual buttons 20 and 21, the front touch screen's
    // halves (see VirtualButtons).
    static int PatchVitaProfile(ModuleDefinition module)
    {
        TypeDefinition controls = compat["DrifterCompat.Controls"];
        TypeDefinition mappingType = module.GetType("InControl.InputControlMapping");
        TypeDefinition controlType = module.GetType("InControl.InputControlType");
        Func<string, int> control = name => (int)controlType.Fields.First(f => f.Name == name).Constant;
        MethodDefinition profile = module.GetType("InControl.UnityDeviceProfiles.PlayStationVitaPSMUnityProfile").Methods.First(m => m.Name == "Define");
        MethodDefinition button = module.GetType("InControl.InputDeviceProfile").Methods.First(m => m.Name == "Button");
        Func<string, MethodDefinition> setter = name => mappingType.Methods.First(m => m.Name == "set_" + name);
        var code = profile.Body.Instructions;
        ILProcessor il = profile.Body.GetILProcessor();
        // new InputControlMapping[8] { ... } as compiled: the array's size, then per element
        // dup, index, newobj, then dup, value, set_X for Name, Target and Source, and stelem.
        Instruction size = code.First(i => i.Next != null && i.Next.OpCode == OpCodes.Newarr &&
                                           ((TypeReference)i.Next.Operand).FullName == mappingType.FullName);
        Instruction store = code.First(i => CalledName(i) == "set_ButtonMappings");
        int mapped = LoadedInt(size) ?? -1;
        if (mapped != 8) throw new Exception("PS Vita profile: expected 8 button mappings, found " + mapped);
        var retarget = new Dictionary<string, Tuple<string, string>>
        {
            { "Left Bumper", Tuple.Create("L", "LeftTrigger") },
            { "Right Bumper", Tuple.Create("R", "RightTrigger") },
        };
        int changed = 0;
        foreach (Instruction name in code.Where(i => i.OpCode == OpCodes.Ldstr && retarget.ContainsKey((string)i.Operand)).ToArray())
        {
            Tuple<string, string> to = retarget[(string)name.Operand];
            Instruction target = name.Next.Next.Next;
            var set = target.Next.Operand as MethodReference;
            if (set == null || set.Name != "set_Target") throw new Exception("PS Vita profile: unexpected mapping shape");
            name.Operand = to.Item1;
            target.OpCode = OpCodes.Ldc_I4;
            target.Operand = control(to.Item2);
            changed++;
        }
        if (changed != 2) throw new Exception("PS Vita profile: bumper mappings not found");
        size.OpCode = OpCodes.Ldc_I4;
        size.Operand = mapped + 2;
        var added = new[]
        {
            Tuple.Create("Front Touch Left", "LeftBumper", (int)controls.Fields.First(f => f.Name == "TouchLeftButton").Constant),
            Tuple.Create("Front Touch Right", "RightBumper", (int)controls.Fields.First(f => f.Name == "TouchRightButton").Constant),
        };
        for (int k = 0; k < added.Length; k++)
            InsertBefore(il, store, il.Create(OpCodes.Dup), il.Create(OpCodes.Ldc_I4, mapped + k),
                         il.Create(OpCodes.Newobj, mappingType.Methods.First(m => m.IsConstructor && !m.IsStatic && m.Parameters.Count == 0)),
                         il.Create(OpCodes.Dup), il.Create(OpCodes.Ldstr, added[k].Item1), il.Create(OpCodes.Callvirt, setter("Name")),
                         il.Create(OpCodes.Dup), il.Create(OpCodes.Ldc_I4, control(added[k].Item2)), il.Create(OpCodes.Callvirt, setter("Target")),
                         il.Create(OpCodes.Dup), il.Create(OpCodes.Ldc_I4, added[k].Item3), il.Create(OpCodes.Call, button),
                         il.Create(OpCodes.Callvirt, setter("Source")), il.Create(OpCodes.Stelem_Ref));
        return changed + added.Length;
    }

    // UnityInputDevice.ReadRawButtonState(index) starts with
    // "if (Controls.VirtualButton(index)) return true;", for every button: the touch halves are
    // buttons 20 and 21 (Unity has 0-19; InControl read the others as released), and Windows
    // previews of the Vita's controls press any button. PatchCrtCombo prepends its check later.
    static int VirtualButtons(ModuleDefinition module)
    {
        MethodDefinition read = module.GetType("InControl.UnityInputDevice").Methods.First(m => m.Name == "ReadRawButtonState");
        ILProcessor il = read.Body.GetILProcessor();
        Instruction original = read.Body.Instructions[0];
        Prepend(read, il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Call, CompatMethod(module, "Controls", "VirtualButton")),
                il.Create(OpCodes.Brfalse, original), il.Create(OpCodes.Ldc_I4_1), il.Create(OpCodes.Ret));
        return 1;
    }

    // InputManager.EnableNativeInput reads go through Controls.NativeInput (Windows previews of
    // the Vita's controls).
    static int NativeInputReads(ModuleDefinition module)
    {
        int count = 0;
        MethodReference nativeInput = CompatMethod(module, "Controls", "NativeInput");
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
            count += CallAfter(method, i => Calls(i, "InControl.InputManager", "get_EnableNativeInput"), nativeInput);
        return count;
    }

    // The PS Vita's prompts (see Compat Controls): QuestText.EvaluateImageTagMatch shows
    // Controls.Glyph(the [tag]'s m_sprite).
    static int GlyphTags(ModuleDefinition module)
    {
        TypeDefinition text = GameType(module, "PowerTools.Quest.QuestText");
        if (text == null) return 0;
        MethodDefinition evaluate = text.Methods.First(m => m.Name == "EvaluateImageTagMatch" && m.HasBody);
        return CallAfter(evaluate, i => i.OpCode == OpCodes.Ldfld && ((FieldReference)i.Operand).Name == "m_sprite" &&
                                        ((FieldReference)i.Operand).DeclaringType.Name == "TextSpriteData", CompatMethod(module, "Controls", "Glyph"));
    }

    // QuestText.FindByTag reads its platform through Controls.TextSpritePlatform.
    static int GlyphPlatform(ModuleDefinition module)
    {
        TypeDefinition text = GameType(module, "PowerTools.Quest.QuestText");
        if (text == null) return 0;
        MethodDefinition find = text.Methods.First(m => m.Name == "FindByTag" && m.HasBody);
        return CallAfter(find, i => i.OpCode == OpCodes.Ldsfld && ((FieldReference)i.Operand).Name == "s_textSpritePlatform",
                         CompatMethod(module, "Controls", "TextSpritePlatform"));
    }

    // GuiPlatformContent.Update shows Controls.Glyph(m_spritePS).
    static int GlyphContent(ModuleDefinition module)
    {
        TypeDefinition content = GameType(module, "GuiPlatformContent");
        if (content == null) return 0;
        MethodDefinition update = content.Methods.First(m => m.Name == "Update" && m.HasBody);
        FieldDefinition ps = content.Fields.First(f => f.Name == "m_spritePS");
        return CallAfter(update, i => i.OpCode == OpCodes.Ldfld && i.Operand == ps, CompatMethod(module, "Controls", "Glyph"));
    }

    // The PS Vita's touches are not a mouse (see Compat Controls): the mouse reads of every
    // assembly go through Controls.MousePosition and Controls.MouseButton.
    static int FilterTouchMouse(ModuleDefinition module)
    {
        var filters = new Dictionary<string, string>
        {
            { "get_mousePosition", "MousePosition" }, { "GetMouseButton", "MouseButton" },
            { "GetMouseButtonDown", "MouseButton" }, { "GetMouseButtonUp", "MouseButton" },
            { "get_mousePresent", "MouseButton" },
        };
        int count = 0;
        foreach (TypeDefinition type in AllTypes(module.Types))
        foreach (MethodDefinition method in type.Methods)
        {
            if (!method.HasBody) continue;
            ILProcessor il = method.Body.GetILProcessor();
            foreach (Instruction instruction in method.Body.Instructions.ToArray())
            {
                var called = instruction.Operand as MethodReference;
                string filter;
                if (called == null || called.DeclaringType.FullName != "UnityEngine.Input" || !filters.TryGetValue(called.Name, out filter))
                    continue;
                il.InsertAfter(instruction, il.Create(OpCodes.Call, CompatMethod(module, "Controls", filter)));
                count++;
            }
        }
        return count;
    }

    // UnityEngine.KeyCode values the intro skip looks for.
    const int KeyCodeReturn = 13, KeyCodeEscape = 27;

    // Cross skips the intro on the PS Vita (see Compat Controls.LogoSkipKey), in four passes.
    // The logos: LoadingScreen.Update starts with Controls.LogoFrame; its
    // Input.GetKey(KeyCode.Return) goes through LogoSkipKey and its skip check's m_ready through
    // LogoSkip, as do SetState's reads of m_skip.
    static int SkipLogos(ModuleDefinition module)
    {
        TypeDefinition loading = GameType(module, "LoadingScreen");
        if (loading == null) return 0;
        MethodDefinition update = loading.Methods.First(m => m.Name == "Update" && m.HasBody);
        int keys = CallAfter(update, i => Calls(i, "UnityEngine.Input", "GetKey") && i.Previous != null && LoadedInt(i.Previous) == KeyCodeReturn,
                             CompatMethod(module, "Controls", "LogoSkipKey"));
        if (keys != 1) throw new Exception("LoadingScreen.Update: expected one Input.GetKey(KeyCode.Return), found " + keys);
        // The m_ready read of the skip check: the last one before Input.GetKey(KeyCode.Escape).
        Instruction escape = update.Body.Instructions.First(i => CalledName(i) == "GetKey" && i.Previous != null && LoadedInt(i.Previous) == KeyCodeEscape);
        Instruction ready = escape;
        while (ready != null && !(ready.OpCode == OpCodes.Ldfld && ((FieldReference)ready.Operand).Name == "m_ready")) ready = ready.Previous;
        if (ready == null) throw new Exception("LoadingScreen.Update: the skip check's m_ready not found");
        MethodReference skip = CompatMethod(module, "Controls", "LogoSkip");
        ILProcessor il = update.Body.GetILProcessor();
        il.InsertAfter(ready, il.Create(OpCodes.Call, skip));
        MethodDefinition setState = loading.Methods.First(m => m.Name == "SetState" && m.HasBody);
        int skips = CallAfter(setState, i => i.OpCode == OpCodes.Ldfld && ((FieldReference)i.Operand).Name == "m_skip", skip);
        Prepend(update, il.Create(OpCodes.Call, CompatMethod(module, "Controls", "LogoFrame")));
        return keys + 1 + skips + 1;
    }

    // The title room's intro is a cutscene: SystemInput.UpdateNoPause's first
    // Buttons.SkipCutscene.WasPressed goes through Controls.SkipCutscenePressed, and its first
    // PowerQuest.SkipCutscene() through CutsceneSkipped.
    static int SkipCutscenes(ModuleDefinition module)
    {
        TypeDefinition input = GameType(module, "SystemInput");
        if (input == null) return 0;
        MethodDefinition noPause = input.Methods.First(m => m.Name == "UpdateNoPause" && m.HasBody);
        Instruction pressed = noPause.Body.Instructions.FirstOrDefault(i => CalledName(i) == "get_WasPressed" && i.Previous != null &&
            i.Previous.Operand is FieldReference && ((FieldReference)i.Previous.Operand).Name == "SkipCutscene");
        if (pressed == null) throw new Exception("SystemInput.UpdateNoPause: Buttons.SkipCutscene.WasPressed not found");
        ILProcessor il = noPause.Body.GetILProcessor();
        il.InsertAfter(pressed, il.Create(OpCodes.Call, CompatMethod(module, "Controls", "SkipCutscenePressed")));
        Instruction skip = noPause.Body.Instructions.First(i => CalledName(i) == "SkipCutscene" && ((MethodReference)i.Operand).DeclaringType.Name == "PowerQuest");
        il.InsertAfter(skip, il.Create(OpCodes.Call, CompatMethod(module, "Controls", "CutsceneSkipped")));
        return 2;
    }

    // RoomTitle.OnEnterRoomAfterFade's StartCutscene and EndCutscene (also run when it is
    // skipped) bracket where Cross may skip the intro: Controls.TitleIntroStarted and
    // TitleIntroEnded follow them.
    static int MarkTitleIntro(ModuleDefinition module)
    {
        TypeDefinition title = GameType(module, "RoomTitle");
        TypeDefinition intro = title == null ? null : title.NestedTypes.FirstOrDefault(t => t.Name.Contains("OnEnterRoomAfterFade"));
        if (intro == null) return 0;
        MethodDefinition moveNext = intro.Methods.First(m => m.Name == "MoveNext");
        int started = CallAfter(moveNext, i => CalledName(i) == "StartCutscene", CompatMethod(module, "Controls", "TitleIntroStarted"));
        int ended = CallAfter(moveNext, i => CalledName(i) == "EndCutscene", CompatMethod(module, "Controls", "TitleIntroEnded"));
        if (started != 1 || ended != 1)
            throw new Exception("RoomTitle.OnEnterRoomAfterFade: expected one StartCutscene and one EndCutscene, found " + started + " and " + ended);
        return started + ended;
    }

    // The menu shows as soon as the intro is skipped: GlobalScript.UpdateNoPause's menu submit
    // reads Menu Ok and Use through Controls.AfterIntroSkip, so the Cross press that skipped
    // does not also submit.
    static int SubmitAfterIntroSkip(ModuleDefinition module)
    {
        TypeDefinition global = GameType(module, "GlobalScript");
        if (global == null) return 0;
        MethodDefinition noPause = global.Methods.First(m => m.Name == "UpdateNoPause" && m.HasBody);
        int submits = CallAfter(noPause, i => i.Previous != null && i.Previous.OpCode == OpCodes.Ldfld &&
            ((((FieldReference)i.Previous.Operand).Name == "MenuOk" && CalledName(i) == "get_WasPressed") ||
             (((FieldReference)i.Previous.Operand).Name == "Use" && CalledName(i) == "op_Implicit")), CompatMethod(module, "Controls", "AfterIntroSkip"));
        if (submits != 2) throw new Exception("GlobalScript.UpdateNoPause: expected Menu Ok and Use in the menu submit, found " + submits);
        return submits;
    }
}
