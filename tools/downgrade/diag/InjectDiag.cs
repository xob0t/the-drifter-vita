using System;
using System.IO;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

// Adds a call to DrifterDiag.Hook.Init() at the start of a method in a player's
// assembly. Only used on throwaway copies made for diagnostics.
// Usage: InjectDiag <assembly> <type full name> <method> <DrifterDiag.dll> [reference dir...]
//        InjectDiag <assembly> --time-methods <first id> <DrifterDiag.dll> [reference dir...]
// The second form wraps every parameterless instance Update, LateUpdate and FixedUpdate (and Awake,
// OnEnable, Start, OnDisable, OnDestroy) in
// DrifterDiag.ScriptTimer.Begin(id, name) and End(id), ids counting up from <first id>, and
// also the methods in TimedExtra (any signature; overloads are told apart by their parameter
// count, "Type.Method/n"), which split the cost of walking: stick walking (SystemInput.Walk
// from PowerQuest's main loop coroutine, which runs at the end of the frame), pathfinding,
// footstep events and sounds.
class InjectDiag
{
    // The lifecycle methods (Awake, OnEnable, Start, OnDisable, OnDestroy) split a room change's
    // frames (-diag-time-room-change); they run once per object, so they cost nothing per frame.
    static readonly string[] TimedMethods = { "Update", "LateUpdate", "FixedUpdate", "Awake", "OnEnable", "Start", "OnDisable", "OnDestroy" };
    static readonly string[] TimedCallSites = { "CharacterComponent.UpdateLipSync" };
    // Call sites timed one by one only with -diag-time-calls (ScriptTimer.CallBegin/CallEnd), to
    // split the per-frame cost of the game's costliest scripts on the PS Vita; a callee called
    // from several sites of one method is numbered ("Type.Method>Callee.Name#2").
    static readonly string[] DetailCallSites =
    {
        "PowerQuest.Update", "GuiControllerTute.Update", "HdFontSelector.LateUpdate", "GlobalScript.Update",
        "GlobalScript.UpdateNoPause", "SystemInput.UpdateNoPause", "SystemGame.LateUpdate", "GuiBgRed.Update",
        "GuiControllerInv.Update", "QuestMenuManager.Update", "InputManager.UpdateInternal", "UnityInputDevice.Update",
        "PlayerActionSet.Update", "PlayerAction.UpdateBindings", "CharacterShadow.LateUpdate", "CharacterComponent.Update",
        "SystemInput.UpdateInput", "RoomDrowning.Update", "GuiControllerTopics.Update", "PropComponent.LateUpdate",
        "UnityMouseProvider.Update", "UnityKeyboardProvider.Update", "InputManager.UpdateDevices", "InputManager.CommitDevices",
        "UnityInputDeviceManager.Update", "GuiPlatformContent.Update", "QuestCursorComponent.Update", "SystemAudio.Update",
        "PowerQuestExAmbient.Update", "CharacterRimlight.LateUpdate", "QuestText.LateUpdate", "SpriteAnim.Update",
        "PropComponent.OnSetPosition", "PropComponent.UpdateBaseline", "RegionComponent.UpdateCharactersOnRegion",
        "RegionComponent.UpdateCharacterOnRegionState", "PowerQuest.UpdateRegions", "SystemGame.UpdateTopBarGui",
        // A room change (-diag-time-room-change): the room load PowerQuest runs from sceneLoaded and at the frame's end.
        "PowerQuest.OnSceneLoaded", "PowerQuest.LoadRoomSequence", "PowerQuest.StartRoomTransition", "PowerQuest.CoroutineRoomTransition",
        "PowerQuest.LoadAtlasAsync", "PowerQuest.CoroutineFadeOut",
    };
    static readonly string[] TimedExtra =
    {
        "SystemInput.UpdateBlocking", "SystemInput.Walk", "SystemInput.UpdateInput",
        "CharacterComponent.CalcWalkToFirstPoint", "CharacterComponent.WalkTo", "CharacterComponent.WalkToInternal",
        "CharacterComponent.AnimFootstep", "CharacterComponent.PlayAnimInternal", "CharacterComponent.UpdateWalking",
        "Character.WalkToBG", "Character.GetCharacters_SaveFlagNotDirtied",
        "PowerQuest.ProcessClick", "PowerQuest.StartScriptInteraction", "PowerQuest.OnInteraction", "PowerQuest.UpdateRegions",
        "PowerQuest.GetCharacters_SaveFlagNotDirtied",
        "Pathfinder.FindPath", "Pathfinder.UpdateObstacles", "Pathfinder.CalculateLinks", "Pathfinder.EvaluateDijkstra",
        "Pathfinder.IsPointInArea", "Pathfinder.GetClosestPointToArea", "Pathfinder.HasLineOfSight", "Pathfinder.<LinkDeferred>", "Pathfinder.EnableObstacle", "Pathfinder.DisableObstacle",
        "Pathfinder.RemoveNode", "Pathfinder.AddPathPolyNodes", "Pathfinder.RemovePathPolyNodes",
        "SystemAudio.Play", "SystemAudio.UpdateActiveAudio", "SystemAudio.SpawnAudioSource",
        "SpriteAnimEventHandler._Anim", "SpriteAnim.Play",
        "SystemAudio.StopHandleInternal", "AudioHandle.Stop",
        "RegionComponent.UpdateCharactersOnRegion", "RegionComponent.UpdateCharacterOnRegionState", "RegionComponent.GetFadeRatio",
        "RegionComponent.GetScaleAt", "RegionPolyUtil.CalcDistToEdge",
        "SystemInput.GetControllerClickableAngles", "SystemInput.ControllerAnglePush", "ControllerHud.UpdateSelectedItem",
        "QuestCursorComponent.CalcCursorVisuals", "PowerQuest.GetAllClickables", "PowerQuest.SetMouseOverClickableOverride",
        "SystemInput.GetControllerRange", "PowerSprite.set_Outline", "SystemInput.OnWheelSelectVibrate",
        "QuestText.SetText", "QuestText.ParseImages", "QuestText.WrapText", "QuestText.WrapToKeepTextOnScreen", "QuestText.UpdateOutline",
        "QuestText.ProcessTypedText", "QuestText.CheckMaterial", "SystemText.GetDisplayText", "TextWrapper.WrapText", "TextWrapper.Truncate",
        "TextWrapper.WrapTextMinimiseWidth", "Character.FaceBG", "Character.FaceDirectionBG", "CharacterComponent.FaceDirection",
        "PowerQuest.CancelCurrentInteraction", "PowerQuest.GetMouseOverDescription", "GuiControllerTute.SetActiveButtons",
        "QuestCursorComponent.Update", "QuestCursorComponent.UpdateCursorVisuals",
        "CharacterComponent.GetTurnAnim", "CharacterComponent.FindDirectionalAnimationName", "CharacterComponent.StartTurnAnimation",
        "PowerQuest.StartQuestCoroutine", "CharacterComponent.UpdateFacingVisuals", "CharacterComponent.FindDirectionalAnimation",
        "Character.FaceInternal", "CharacterComponent.GetAnimations", "Character.WaitForTransition", "CharacterComponent.GetPlayingTransition",
        "CharacterComponent.UpdateTurnToFace", "CharacterComponent.UpdateLipSync", "CharacterComponent.UpdateAnimating",
        "CharacterComponent.CheckTargetDirectionWillChangeAnim", "CharacterComponent.OnAnimStateChange", "CharacterComponent.ExUpdate",
        "Character.UpdateFacingCharacter", "Character.SetFaceDirection", "Character.EndChat", "SpriteAnim.SetNormalizedTime",
        "SpriteAnim.get_NormalizedTime", "SpriteAnim.IsPlaying", "SpriteAnim.Pause", "SystemText.FindTextData",
        "SystemText.GetLipSyncAnimTime", "CharacterComponent.UpdateUseSpriteAsHotspot", "Utils.GetTimeIncrementPassed",
        "Character.get_Talking", "CharacterComponent.get_Talking", "Character.GetDialogAudioSource",
        "Rain.Spawn", "RainDrop.Spawn", "RainDrop.set_Active",
    };
    static readonly System.Collections.Generic.Dictionary<Code, OpCode> LongForms = new System.Collections.Generic.Dictionary<Code, OpCode>
    {
        { Code.Br_S, OpCodes.Br }, { Code.Brfalse_S, OpCodes.Brfalse }, { Code.Brtrue_S, OpCodes.Brtrue },
        { Code.Beq_S, OpCodes.Beq }, { Code.Bge_S, OpCodes.Bge }, { Code.Bgt_S, OpCodes.Bgt }, { Code.Ble_S, OpCodes.Ble },
        { Code.Blt_S, OpCodes.Blt }, { Code.Bne_Un_S, OpCodes.Bne_Un }, { Code.Bge_Un_S, OpCodes.Bge_Un },
        { Code.Bgt_Un_S, OpCodes.Bgt_Un }, { Code.Ble_Un_S, OpCodes.Ble_Un }, { Code.Blt_Un_S, OpCodes.Blt_Un },
        { Code.Leave_S, OpCodes.Leave },
    };

    // The inserted code can put short branch targets out of range.
    static void WidenBranches(MethodBody body)
    {
        foreach (Instruction branch in body.Instructions)
        {
            OpCode wide;
            if (LongForms.TryGetValue(branch.OpCode.Code, out wide)) branch.OpCode = wide;
        }
    }

    static void TimeMethods(AssemblyDefinition assembly, AssemblyDefinition diag, int id)
    {
        TypeDefinition timer = diag.MainModule.GetType("DrifterDiag.ScriptTimer");
        MethodReference begin = assembly.MainModule.ImportReference(timer.Methods.First(m => m.Name == "Begin"));
        MethodReference end = assembly.MainModule.ImportReference(timer.Methods.First(m => m.Name == "End"));
        // DrifterDiag.AllocTimer (-diag-alloc-scripts) brackets the same methods, inside ScriptTimer.
        TypeDefinition allocTimer = diag.MainModule.GetType("DrifterDiag.AllocTimer");
        MethodReference allocBegin = assembly.MainModule.ImportReference(allocTimer.Methods.First(m => m.Name == "Begin"));
        MethodReference allocEnd = assembly.MainModule.ImportReference(allocTimer.Methods.First(m => m.Name == "End"));
        int first = id;
        foreach (TypeDefinition type in assembly.MainModule.GetTypes())
        {
            foreach (MethodDefinition method in type.Methods)
            {
                if (!method.HasBody || method.HasGenericParameters || type.HasGenericParameters || method.IsConstructor) continue;
                bool extra = TimedExtra.Contains(type.Name + "." + method.Name);
                if (!extra && (method.IsStatic || method.HasParameters || !TimedMethods.Contains(method.Name))) continue;
                string label = type.Name + "." + method.Name + (method.HasParameters ? "/" + method.Parameters.Count : "");
                ILProcessor il = method.Body.GetILProcessor();
                WidenBranches(method.Body);
                // Each ret becomes "ldc id; call End; ret": the old instruction is reused as the
                // ldc, so branches and handler boundaries that pointed at the ret stay correct.
                foreach (Instruction ret in method.Body.Instructions.Where(i => i.OpCode == OpCodes.Ret).ToList())
                {
                    ret.OpCode = OpCodes.Ldc_I4;
                    ret.Operand = id;
                    Instruction allocCall = il.Create(OpCodes.Call, allocEnd);
                    Instruction timerId = il.Create(OpCodes.Ldc_I4, id);
                    Instruction call = il.Create(OpCodes.Call, end);
                    il.InsertAfter(ret, allocCall);
                    il.InsertAfter(allocCall, timerId);
                    il.InsertAfter(timerId, call);
                    il.InsertAfter(call, il.Create(OpCodes.Ret));
                }
                Instruction start = method.Body.Instructions[0];
                foreach (MethodReference timerBegin in new[] { begin, allocBegin })
                {
                    il.InsertBefore(start, il.Create(OpCodes.Ldc_I4, id));
                    il.InsertBefore(start, il.Create(OpCodes.Ldstr, label));
                    il.InsertBefore(start, il.Create(OpCodes.Call, timerBegin));
                }
                id++;
            }
        }
        // Call sites inside these methods are timed one by one ("Type.Method>Callee"), to find
        // which engine call in a method's own body costs the time.
        MethodReference callBegin = assembly.MainModule.ImportReference(timer.Methods.First(m => m.Name == "CallBegin"));
        MethodReference callEnd = assembly.MainModule.ImportReference(timer.Methods.First(m => m.Name == "CallEnd"));
        foreach (TypeDefinition type in assembly.MainModule.GetTypes())
            foreach (MethodDefinition method in type.Methods)
            {
                // An iterator's body (MoveNext of its compiler class, "<Name>d__N") counts as the
                // method that wrote it ("PowerQuest.LoadRoomSequence").
                string site = method.Name == "MoveNext" && type.Name.StartsWith("<") && type.Name.Contains(">d__") && type.DeclaringType != null
                    ? type.DeclaringType.Name + "." + type.Name.Substring(1, type.Name.IndexOf('>') - 1) : type.Name + "." + method.Name;
                bool always = TimedCallSites.Contains(type.Name + "." + method.Name);
                if (!method.HasBody || (!always && !DetailCallSites.Contains(site))) continue;
                ILProcessor il = method.Body.GetILProcessor();
                WidenBranches(method.Body);
                var seen = new System.Collections.Generic.Dictionary<string, int>();
                foreach (Instruction call in method.Body.Instructions.Where(i => (i.OpCode == OpCodes.Call || i.OpCode == OpCodes.Callvirt) &&
                         i.Operand is MethodReference && !((MethodReference)i.Operand).DeclaringType.FullName.StartsWith("DrifterDiag") &&
                         (i.Previous == null || i.Previous.OpCode != OpCodes.Constrained)).ToList())
                {
                    var callee = (MethodReference)call.Operand;
                    string label = site + ">" + callee.DeclaringType.Name + "." + callee.Name;
                    if (!always)
                    {
                        int n;
                        seen[label] = n = seen.TryGetValue(label, out n) ? n + 1 : 1;
                        if (n > 1) label += "#" + n;
                    }
                    Instruction callTimer = il.Create(OpCodes.Ldc_I4, id);
                    il.InsertBefore(call, callTimer);
                    il.InsertBefore(call, il.Create(OpCodes.Ldstr, label));
                    il.InsertBefore(call, il.Create(OpCodes.Call, always ? begin : callBegin));
                    // Branches (and handler bounds) that went to the call now go to its callTimer, so the
                    // callTimer is never left as unreachable code with values on the stack (IL2CPP rejects
                    // that: "Invalid global variables count").
                    foreach (Instruction branch in method.Body.Instructions)
                    {
                        if (branch.Operand == call && branch != callTimer) branch.Operand = callTimer;
                        var cases = branch.Operand as Instruction[];
                        if (cases != null) for (int c = 0; c < cases.Length; c++) if (cases[c] == call) cases[c] = callTimer;
                    }
                    foreach (ExceptionHandler handler in method.Body.ExceptionHandlers)
                    {
                        if (handler.TryStart == call) handler.TryStart = callTimer;
                        if (handler.TryEnd == call) handler.TryEnd = callTimer;
                        if (handler.HandlerStart == call) handler.HandlerStart = callTimer;
                        if (handler.HandlerEnd == call) handler.HandlerEnd = callTimer;
                        if (handler.FilterStart == call) handler.FilterStart = callTimer;
                    }
                    Instruction after = il.Create(OpCodes.Ldc_I4, id);
                    il.InsertAfter(call, after);
                    il.InsertAfter(after, il.Create(OpCodes.Call, always ? end : callEnd));
                    id++;
                }
            }
        Console.WriteLine("timed " + (id - first) + " methods");
        // The probe measures its own frame under the id DrifterDiag.Probe.ProbeFrameId, above
        // every assembly's range (diag.py); a timed method there would share its sums.
        FieldDefinition probeFrame = diag.MainModule.GetType("DrifterDiag.Probe").Fields.First(f => f.Name == "ProbeFrameId");
        if (id > (int)probeFrame.Constant)
            throw new InvalidOperationException("timed method ids reach " + (id - 1) + ", not below Probe.ProbeFrameId " + probeFrame.Constant);
    }

    static int Main(string[] args)
    {
        string path = args[0], typeName = args[1], methodName = args[2], diagPath = args[3];
        var resolver = new DefaultAssemblyResolver();
        resolver.AddSearchDirectory(Path.GetDirectoryName(path));
        foreach (string directory in args.Skip(4)) resolver.AddSearchDirectory(directory);
        byte[] bytes = File.ReadAllBytes(path);
        using (var assembly = AssemblyDefinition.ReadAssembly(new MemoryStream(bytes), new ReaderParameters { AssemblyResolver = resolver }))
        using (var diag = AssemblyDefinition.ReadAssembly(diagPath))
        {
            string injected = null;
            if (typeName == "--time-methods") TimeMethods(assembly, diag, int.Parse(methodName));
            else
            {
                TypeDefinition type = assembly.MainModule.GetType(typeName);
                if (type == null) { Console.WriteLine("type not found: " + typeName); return 2; }
                MethodDefinition method = type.Methods.FirstOrDefault(m => m.Name == methodName && !m.HasParameters);
                if (method == null) { Console.WriteLine("method not found: " + methodName); return 2; }
                MethodDefinition init = diag.MainModule.GetType("DrifterDiag.Hook").Methods.First(m => m.Name == "Init");
                ILProcessor il = method.Body.GetILProcessor();
                if (method.Body.Instructions.Count > 0 && method.Body.Instructions[0].Operand is MethodReference &&
                    ((MethodReference)method.Body.Instructions[0].Operand).FullName == init.FullName)
                {
                    Console.WriteLine("already injected");
                    return 0;
                }
                il.InsertBefore(method.Body.Instructions[0], il.Create(OpCodes.Call, assembly.MainModule.ImportReference(init)));
                injected = "injected into " + type.FullName + "." + methodName;
            }
            // Writing straight to the path truncates it before a failed write.
            var output = new MemoryStream();
            assembly.Write(output);
            File.WriteAllBytes(path, output.ToArray());
            if (injected != null) Console.WriteLine(injected);
        }
        return 0;
    }
}
