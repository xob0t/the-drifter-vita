using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

// Rewrites game assemblies built against a newer Unity so they load against an
// older engine's assemblies: calls to an API missing from the target version go to the
// compat assembly (DrifterCompat). The other passes (Passes lists them) patch game
// methods for the PS Vita's controls and costs, each with its rule in DrifterCompat.
//
// Usage: DowngradeAssemblies <input dir> <output dir> <compat.dll> <roots> <reference dir>...
// <roots> is a comma-separated list of assembly names; only they and the input
// assemblies they reference are written.
//
// The rewriter is this file and rewriter/*.cs, compiled together in the order of
// pipeline.REWRITER_SOURCES; DowngradeAssemblies is one partial class across them:
//   DowngradeAssemblies.cs  Main, Rewrite, Passes (every pass in the order they run), body sizes
//   Patches.cs              single game methods: CRT filter, console menus, PowerQuest's startup,
//                           post-processing volumes, saves, reflection, audio source filters,
//                           water sprites; CachedCalls, CacheCalls' table
//   ScriptCosts.cs          per-frame script costs: GetScript, CacheCalls, effect sprites, lookups
//   Walking.cs              walking costs
//   Allocs.cs               per-frame allocations
//   Gameplay.cs             more per-frame costs: animations, GUI, regions, pathfinder geometry, text
//   Controls.cs             InControl, the PS Vita's controls, prompts and intro skip
//   Redirect.cs             missing types and members to DrifterCompat, attributes, references,
//                           the IL helpers the passes share
partial class DowngradeAssemblies
{
    // Types that exist in the target under another name.
    static readonly Dictionary<string, string> renamedTypes = new Dictionary<string, string>
    {
        { "UnityEngine.ExecuteAlways", "UnityEngine.ExecuteInEditMode" },
        { "UnityEngine.Rendering.RenderPipelineAsset", "UnityEngine.Experimental.Rendering.RenderPipelineAsset" },
    };

    // Unity reserves these names for assemblies compiled from project scripts.
    static readonly Dictionary<string, string> renamedAssemblies = new Dictionary<string, string>
    {
        { "Assembly-CSharp", "Recovered.Drifter" },
        { "Assembly-CSharp-firstpass", "Recovered.PowerTools" },
    };

    static readonly Dictionary<string, TypeDefinition> target = new Dictionary<string, TypeDefinition>();
    static readonly Dictionary<string, TypeDefinition> compat = new Dictionary<string, TypeDefinition>();
    static readonly HashSet<string> shipped = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    static readonly List<string> problems = new List<string>();
    // Methods a pass could not inspect because a type or method they name failed to resolve
    // (printed as SKIPPED; the run still succeeds): the pass left them as they were, so its rule
    // may be missing there. For MarkNativeCallbacks that means a native callback IL2CPP would
    // reject at run time; for CacheEnumNames only a slower enum name.
    static readonly List<string> skipped = new List<string>();
    static readonly HashSet<string> strippedTypes = new HashSet<string>();
    static ModuleDefinition compatModule;

    // Which assemblies a pass runs on: all, the two game assemblies, or InControl.
    enum Scope { All, Game, InControl }

    static bool IsGameAssembly(ModuleDefinition module)
    {
        return renamedAssemblies.ContainsValue(module.Assembly.Name.Name);
    }

    class Pass
    {
        public readonly string Group;
        public readonly Scope Scope;
        public readonly Func<ModuleDefinition, int> Run;

        public Pass(string group, Scope scope, Func<ModuleDefinition, int> run)
        {
            Group = group;
            Scope = scope;
            Run = run;
        }

        public string Name { get { return Run.Method.Name; } }
    }

    // The passes after the type retargeting, in the order they run: later ones build on earlier
    // ones (their comments say which). Each returns the number of sites it changed; the stats
    // show them by group, and a pass that changed nothing in any assembly fails the run.
    static readonly Pass[] Passes =
    {
        new Pass("redirect", Scope.All, RedirectMembers),
        new Pass("redirect", Scope.All, MarkNativeCallbacks),
        // Controls.cs
        new Pass("input", Scope.InControl, PatchInControl),
        new Pass("input", Scope.InControl, PatchVitaProfile),
        new Pass("input", Scope.InControl, VirtualButtons),
        new Pass("input", Scope.InControl, NativeInputReads),
        new Pass("input", Scope.InControl, FastJoystickButtons),
        // Patches.cs and Controls.cs
        new Pass("patches", Scope.All, HookPostProcessingVolume),
        new Pass("patches", Scope.All, CacheReflection),
        new Pass("patches", Scope.All, PatchPowerQuestAwake),
        new Pass("patches", Scope.All, MemoizeTypePredicates),
        new Pass("patches", Scope.All, SkipMissingSaveFields),
        new Pass("patches", Scope.All, RedirectSaveCompression),
        new Pass("patches", Scope.All, FastSaveFields),
        new Pass("patches", Scope.All, MemoizeSurrogateLookup),
        new Pass("patches", Scope.All, PatchCrt),
        new Pass("patches", Scope.All, PatchCrtCombo),
        new Pass("patches", Scope.All, PatchConsoleUi),
        new Pass("patches", Scope.All, GlyphTags),
        new Pass("patches", Scope.All, GlyphPlatform),
        new Pass("patches", Scope.All, GlyphContent),
        new Pass("patches", Scope.All, FilterTouchMouse),
        new Pass("patches", Scope.All, SkipLogos),
        new Pass("patches", Scope.All, SkipCutscenes),
        new Pass("patches", Scope.All, MarkTitleIntro),
        new Pass("patches", Scope.All, SubmitAfterIntroSkip),
        new Pass("patches", Scope.Game, PatchSourceFilters),
        new Pass("patches", Scope.All, PatchWaterSprite),
        // CacheCalls and the per-frame costs (ScriptCosts.cs, Gameplay.cs)
        new Pass("scriptCosts", Scope.Game, RewriteGetScript),
        new Pass("scriptCosts", Scope.Game, CacheCalls),
        new Pass("scriptCosts", Scope.Game, FastCharacterIds),
        new Pass("scriptCosts", Scope.Game, CacheEnumNames),
        new Pass("scriptCosts", Scope.Game, MarkIconReplays),
        new Pass("scriptCosts", Scope.Game, PatchSpriteAnimPlay),
        new Pass("scriptCosts", Scope.Game, FastPathfinder),
        new Pass("scriptCosts", Scope.Game, RedirectPathMath),
        new Pass("scriptCosts", Scope.Game, FastTextWidths),
        new Pass("scriptCosts", Scope.Game, CacheClickableAngles),
        new Pass("scriptCosts", Scope.Game, CacheAnimationDirections),
        new Pass("scriptCosts", Scope.Game, DeferMouthActivation),
        new Pass("scriptCosts", Scope.Game, InlineKeyClosures),
        new Pass("scriptCosts", Scope.Game, CacheTransformReads),
        new Pass("scriptCosts", Scope.Game, CacheGetComponent),
        new Pass("scriptCosts", Scope.Game, BatchSprites),
        new Pass("scriptCosts", Scope.Game, FastRainReuse),
        new Pass("scriptCosts", Scope.Game, MemoizeScriptableLookups),
        new Pass("scriptCosts", Scope.Game, MemoizeSpriteLookups),
        new Pass("scriptCosts", Scope.Game, MemoizeNameLookups),
        new Pass("scriptCosts", Scope.Game, FastRegionStates),
        new Pass("scriptCosts", Scope.Game, HookAnimatorCulling),
        // ReduceAllocations (Allocs.cs)
        new Pass("allocations", Scope.Game, ReuseClosures),
        new Pass("allocations", Scope.Game, CacheThisLambdas),
        new Pass("allocations", Scope.Game, PoolChildArrays),
        new Pass("allocations", Scope.Game, UseScratchLists),
        new Pass("allocations", Scope.Game, CacheColliderPoints),
        new Pass("allocations", Scope.Game, ReuseClickables),
        new Pass("allocations", Scope.Game, MemoizeWrapText),
        new Pass("allocations", Scope.Game, CacheNumberText),
        new Pass("allocations", Scope.Game, PoolPathLinks),
        new Pass("allocations", Scope.Game, CacheRegexes),
        new Pass("allocations", Scope.Game, CacheSpriteVertices),
        new Pass("allocations", Scope.Game, MemoizeConcats),
        new Pass("allocations", Scope.Game, MemoizeTextIds),
        // FasterWalking (Walking.cs): after FastPathfinder, UseScratchLists and InlineKeyClosures,
        // whose output InnerAreaTests, CacheClipLookups and CacheCueLookups expect; the inside
        // memo uses the clone InnerAreaTests makes and the snapshot MemoizeClosestPoints makes.
        new Pass("walking", Scope.Game, LazyTargetLinks),
        new Pass("walking", Scope.Game, InnerAreaTests),
        new Pass("walking", Scope.Game, CacheClipLookups),
        new Pass("walking", Scope.Game, MemoizeClosestPoints),
        new Pass("walking", Scope.Game, FlatSight),
        new Pass("walking", Scope.Game, CacheCueLookups),
        new Pass("walking", Scope.Game, MemoizeInsideTests),
        new Pass("scriptCosts", Scope.Game, SkipGuiReshows),
    };

    // Sites changed per rule (a pass, a row of a rule table, a game type a pass looks for),
    // summed over the assemblies: a rule with none in any assembly fails the run, as the game
    // no longer has what it expects. Passes run per assembly, so none in one is normal.
    static readonly Dictionary<string, int> matches = new Dictionary<string, int>();

    static void Count(string rule, int sites)
    {
        int sum;
        matches.TryGetValue(rule, out sum);
        matches[rule] = sum + sites;
    }

    // module.GetType(name), counted: a game type a pass looks for must be in some assembly.
    static TypeDefinition GameType(ModuleDefinition module, string name)
    {
        TypeDefinition type = module.GetType(name);
        Count("type " + name, type == null ? 0 : 1);
        return type;
    }

    // References in the game assemblies name each other by their new names (Recovered.*);
    // their files keep the original names.
    class RenamedAssemblyResolver : DefaultAssemblyResolver
    {
        public override AssemblyDefinition Resolve(AssemblyNameReference name)
        {
            string original = renamedAssemblies.Where(p => p.Value == name.Name).Select(p => p.Key).FirstOrDefault();
            return base.Resolve(original == null ? name : new AssemblyNameReference(original, name.Version));
        }
    }

    static int Main(string[] args)
    {
        string input = Path.GetFullPath(args[0]), output = Path.GetFullPath(args[1]);
        compatModule = ModuleDefinition.ReadModule(args[2]);
        foreach (TypeDefinition type in compatModule.Types) compat[type.FullName] = type;
        foreach (string dir in args.Skip(4))
            foreach (string file in Directory.GetFiles(dir, "*.dll").OrderBy(Priority))
                Index(file);
        var available = Directory.GetFiles(input, "*.dll")
            .Where(f => !Path.GetFileName(f).StartsWith("UnityEngine."))
            .ToDictionary(f => Path.GetFileNameWithoutExtension(f), StringComparer.OrdinalIgnoreCase);
        string[] roots = args[3].Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries);
        string[] absent = roots.Where(root => !available.ContainsKey(root)).ToArray();
        if (absent.Length > 0)
        {
            Console.WriteLine("MISSING root assemblies, not in " + input + ": " + string.Join(", ", absent));
            return 2;
        }
        // The roots, then what they reference from the input folder (the rest, such as
        // mscorlib, comes from the reference folders).
        var pending = new Queue<string>(roots);
        while (pending.Count > 0)
        {
            string name = pending.Dequeue();
            if (!available.ContainsKey(name) || !shipped.Add(name)) continue;
            using (var module = ModuleDefinition.ReadModule(available[name]))
                foreach (AssemblyNameReference reference in module.AssemblyReferences) pending.Enqueue(reference.Name);
        }
        Directory.CreateDirectory(output);
        var resolver = new RenamedAssemblyResolver();
        resolver.AddSearchDirectory(input);
        foreach (string dir in args.Skip(4)) resolver.AddSearchDirectory(dir);
        resolver.AddSearchDirectory(Path.GetDirectoryName(Path.GetFullPath(args[2])));
        foreach (string file in shipped.Select(n => available[n]).ToArray())
        {
            using (var assembly = AssemblyDefinition.ReadAssembly(file, new ReaderParameters { AssemblyResolver = resolver }))
            {
                string stats = Rewrite(assembly);
                string destination = Path.Combine(output, assembly.Name.Name + ".dll");
                assembly.Write(destination);
                Console.WriteLine(Path.GetFileName(file) + " -> " + Path.GetFileName(destination) + " " + stats);
            }
        }
        foreach (string problem in problems.Distinct()) Console.WriteLine("UNRESOLVED " + problem);
        foreach (string site in skipped.Distinct()) Console.WriteLine("SKIPPED " + site);
        string[] unmatched = matches.Where(m => m.Value == 0).Select(m => m.Key).ToArray();
        foreach (string rule in unmatched) Console.WriteLine("NO MATCH " + rule + " (in none of the assemblies; the game no longer has what it expects)");
        return problems.Count > 0 ? 3 : unmatched.Length > 0 ? 4 : 0;
    }

    static int Priority(string file)
    {
        string name = Path.GetFileNameWithoutExtension(file);
        return name == "mscorlib" ? 0 : name == "System" ? 1 : name == "System.Core" ? 2 : 3;
    }

    // The public types of a reference assembly. The module stays open (it is never disposed):
    // Cecil reads the indexed types' members from it later, on demand.
    static void Index(string file)
    {
        ModuleDefinition module;
        try { module = ModuleDefinition.ReadModule(file); }
        catch (BadImageFormatException) { return; }
        foreach (TypeDefinition type in AllTypes(module.Types))
            if (type.IsPublic || type.IsNestedPublic || type.IsNestedFamily || type.IsNestedFamilyOrAssembly)
                if (!target.ContainsKey(type.FullName)) target[type.FullName] = type;
    }

    static string Rewrite(AssemblyDefinition assembly)
    {
        ModuleDefinition module = assembly.MainModule;
        string newName;
        if (renamedAssemblies.TryGetValue(assembly.Name.Name, out newName))
        {
            assembly.Name.Name = newName;
            module.Name = newName + ".dll";
        }
        foreach (AssemblyNameReference reference in module.AssemblyReferences)
            if (renamedAssemblies.TryGetValue(reference.Name, out newName)) reference.Name = newName;

        // Each body's size before the passes (see WidenChangedBodies).
        var sizes = new Dictionary<MethodDefinition, long>();
        foreach (TypeDefinition type in AllTypes(module.Types))
            foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody)) sizes[method] = BodySize(method.Body);
        // Before the retargeting: it fills strippedTypes, whose references the loop leaves alone.
        int stripped = StripAttributes(module);
        int retargeted = 0, shimmedTypes = 0;
        foreach (TypeReference type in module.GetTypeReferences().ToArray())
        {
            if (type.DeclaringType != null || !IsExternal(type) || strippedTypes.Contains(type.FullName)) continue;
            string name;
            if (renamedTypes.TryGetValue(type.FullName, out name))
            {
                int dot = name.LastIndexOf('.');
                type.Namespace = name.Substring(0, dot);
                type.Name = name.Substring(dot + 1);
            }
            TypeDefinition definition;
            if (target.TryGetValue(type.FullName, out definition))
            {
                if (SetScope(module, type, definition.Module.Assembly.Name)) retargeted++;
            }
            else if (compat.TryGetValue(CompatName(type.FullName), out definition))
            {
                type.Namespace = definition.Namespace;
                type.Name = definition.Name;
                SetScope(module, type, compatModule.Assembly.Name);
                shimmedTypes++;
            }
            else problems.Add(module.Name + ": type " + type.FullName);
        }
        // Each group's line of the stats, "  <group>: <pass>=<sites> ...", for groups that changed something.
        var groups = new List<string>();
        var lines = new Dictionary<string, string>();
        var changed = new HashSet<string>();
        foreach (Pass pass in Passes)
        {
            if ((pass.Scope == Scope.Game && !IsGameAssembly(module)) || (pass.Scope == Scope.InControl && assembly.Name.Name != "InControl"))
                continue;
            int sites = pass.Run(module);
            Count("pass " + pass.Name, sites);
            if (!lines.ContainsKey(pass.Group))
            {
                groups.Add(pass.Group);
                lines[pass.Group] = "\n  " + pass.Group + ":";
            }
            lines[pass.Group] += " " + pass.Name + "=" + sites;
            if (sites > 0) changed.Add(pass.Group);
        }
        int outOfRange;
        int widened = WidenChangedBodies(module, sizes, out outOfRange);
        int removed = RemoveUnusedReferences(module);
        return "retargeted=" + retargeted + " shimTypes=" + shimmedTypes + " strippedAttributes=" + stripped +
               " widenedBodies=" + widened + " (short branches out of range in " + outOfRange + ")" +
               " removedReferences=" + removed + string.Concat(groups.Where(changed.Contains).Select(g => lines[g]));
    }

    // Compat's method DrifterCompat.<type>.<name> (the only one of that name), imported, and
    // instantiated with the type arguments given.
    static MethodReference CompatMethod(ModuleDefinition module, string type, string name, params TypeReference[] typeArguments)
    {
        MethodReference method = module.ImportReference(compat["DrifterCompat." + type].Methods.Single(m => m.Name == name));
        if (typeArguments.Length == 0) return method;
        var instance = new GenericInstanceMethod(method);
        foreach (TypeReference argument in typeArguments) instance.GenericArguments.Add(argument);
        return instance;
    }

    // Instruction count and encoded size of a body.
    static long BodySize(MethodBody body)
    {
        long size = 0;
        foreach (Instruction instruction in body.Instructions) size += instruction.GetSize();
        return ((long)body.Instructions.Count << 32) | size;
    }

    // Cecil writes a short branch whose target ended up out of its range without complaint
    // (the offset wraps; it reads back with no target). After the passes, every body whose
    // instructions changed (inserted, removed or resized), every new body, and any body with
    // a short branch out of range gets the long branch forms.
    static int WidenChangedBodies(ModuleDefinition module, Dictionary<MethodDefinition, long> sizes, out int outOfRange)
    {
        int count = 0;
        outOfRange = 0;
        foreach (TypeDefinition type in AllTypes(module.Types))
            foreach (MethodDefinition method in type.Methods.Where(m => m.HasBody))
            {
                long size;
                bool changed = !sizes.TryGetValue(method, out size) || size != BodySize(method.Body);
                bool wrong = ShortBranchOutOfRange(method.Body);
                if (wrong) outOfRange++;
                if (!changed && !wrong) continue;
                if (!method.Body.Instructions.Any(i => i.OpCode.OperandType == OperandType.ShortInlineBrTarget)) continue;
                WidenBranches(method.Body);
                count++;
            }
        return count;
    }

    static bool ShortBranchOutOfRange(MethodBody body)
    {
        var offsets = new Dictionary<Instruction, int>();
        int offset = 0;
        foreach (Instruction instruction in body.Instructions)
        {
            offsets[instruction] = offset;
            offset += instruction.GetSize();
        }
        foreach (Instruction instruction in body.Instructions)
        {
            if (instruction.OpCode.OperandType != OperandType.ShortInlineBrTarget) continue;
            var target = instruction.Operand as Instruction;
            int at;
            if (target == null || !offsets.TryGetValue(target, out at)) return true;
            int delta = at - (offsets[instruction] + instruction.GetSize());
            if (delta < sbyte.MinValue || delta > sbyte.MaxValue) return true;
        }
        return false;
    }
}
