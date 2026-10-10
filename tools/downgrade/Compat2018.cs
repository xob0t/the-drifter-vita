using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;

// Implementations of Unity 2020 APIs that Unity 2018.2 lacks. The assembly
// downgrader redirects missing references here by naming convention:
//   missing type  Namespace.Name        -> DrifterCompat.Namespace_Name
//   missing member Namespace.Type::M    -> static DrifterCompat.Namespace_Type.M
// Instance members take the instance first (by ref for structs); constructors
// are static methods named "ctor".
//
// DrifterCompat is this file and compat/*.cs, compiled together in the order of
// pipeline.COMPAT_SOURCES (all in namespace DrifterCompat; the rewriter and the
// probe find its classes by name, not by file). In that order:
//   Compat2018.cs        AssemblyAliases, TypeNames (the renamed assemblies; the save binder's cache)
//   Saves.cs             the shared PowerQuest copy, reflection and type caches, SaveFields
//   StoredSaves.cs       stored gzip saves (SaveCompression, StoredGzipStream)
//   FastFields.cs        FastFields, SaveSurrogate
//   Engine.cs            MainThread, SceneUnloads, the caches' shared helpers, TransformReads,
//                        ParticleCameras, LoopSteps, ParticleRate, AnimatorCulling
//   UnloadedRoomRefs.cs  asset references of destroyed room scripts
//   Noise.cs             FastNoise, WaterMasks
//   Platform.cs          Platform, Controls, CrtToggle, VitaOptions, UpdateHost, PowerTicks, MemorySetup
//   Crt.cs, CrtCompare.cs  the CRT filter's PS Vita paths, and its test flags
//   ApiShims.cs          Unity 2020 API that 2018.2 lacks (the naming convention above)
//   ScriptCache.cs       per-frame script costs: reflected calls, list helpers
//   Audio.cs             SourceFilters, AudioData
//   ScriptCosts.cs       per-frame script costs: names, list lookups, script kinds, shader ids,
//                        animator overrides
//   PathMath.cs          the pathfinder's geometry
//   RepeatedCalls.cs     per-frame script costs: text widths, clickable angles, animation
//                        directions, input keys, components, unchanged writes
//   Walking.cs           WalkRules, SightEdges, AreaMemo
//   Allocs.cs            allocation rules and GuiReshows
//   Scriptables.cs       per-frame script costs: PowerQuest's scriptable lookups
//   EffectSprites.cs, EffectShapes.cs  rain, sand and bees drawn as pages; OverlapPoint
namespace DrifterCompat
{
    // The downgrader renames Assembly-CSharp(-firstpass), which Unity reserves for
    // project scripts. Saves written by BinaryFormatter and assembly-qualified type
    // names still use the original names; resolve those to the renamed assemblies.
    public static class AssemblyAliases
    {
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Install()
        {
            AppDomain.CurrentDomain.AssemblyResolve += (sender, args) =>
            {
                string name = new System.Reflection.AssemblyName(args.Name).Name;
                string target = name == "Assembly-CSharp" ? "Recovered.Drifter"
                    : name == "Assembly-CSharp-firstpass" ? "Recovered.PowerTools" : null;
                return target == null ? null
                    : AppDomain.CurrentDomain.GetAssemblies().FirstOrDefault(a => a.GetName().Name == target);
            };
        }
    }

    // Type.GetType calls in the renamed assemblies. Names written by the source version
    // (BinaryFormatter saves, generic arguments the game's own binder passes through)
    // still say Assembly-CSharp; AssemblyResolve never fires under IL2CPP (the PS Vita's player).
    // A name without an assembly is looked up in the calling assembly, which is now this one
    // instead of the game's: InControl creates its device profiles that way, and every
    // profile came back null, so controllers were unknown devices. Such names are also
    // looked up in every loaded assembly.
    public static class TypeNames
    {
        public static Type GetType(string name) { return GetType(name, false, false); }
        public static Type GetType(string name, bool throwOnError) { return GetType(name, throwOnError, false); }
        // Names found are remembered: PowerQuest constructs every room's, character's and
        // item's script by name (QuestUtils.ConstructByName) at each save restore, and IL2CPP
        // parses the name and searches its images every time.
        static readonly Dictionary<string, Type> found = new Dictionary<string, Type>();
        static readonly Dictionary<string, Type> foundIgnoringCase = new Dictionary<string, Type>();

        public static Type GetType(string name, bool throwOnError, bool ignoreCase)
        {
            Dictionary<string, Type> cache = name != null && MainThread.Current ? ignoreCase ? foundIgnoringCase : found : null;
            Type type;
            if (cache != null && cache.TryGetValue(name, out type))
                return type != null || !throwOnError ? type : Type.GetType(Rename(name), true, ignoreCase);
            string renamed = Rename(name);
            type = Type.GetType(renamed, false, ignoreCase);
            if (type == null && renamed.IndexOf(',') < 0)
                foreach (System.Reflection.Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
                    if ((type = assembly.GetType(renamed, false, ignoreCase)) != null) break;
            if (type == null)
            {
                // A miss scans every assembly. The PS Vita's IL2CPP player never loads another
                // one, so a miss there stays a miss; other players (Mono) could, so they keep asking.
                if (cache != null && Application.platform == RuntimePlatform.PSP2) cache[name] = null;
                return throwOnError ? Type.GetType(renamed, true, ignoreCase) : null;
            }
            if (cache != null) cache[name] = type;
            return type;
        }

        // Replaces BinaryFormatter.Binder assignments. When the game's binder cannot
        // resolve a name, the formatter would load the named assembly itself, which
        // only works for the renamed ones through AssemblyResolve.
        public static void SetBinder(System.Runtime.Serialization.Formatters.Binary.BinaryFormatter formatter,
                                     System.Runtime.Serialization.SerializationBinder binder)
        {
            formatter.Binder = binder == null || binder is AliasBinder ? binder : new AliasBinder(binder);
        }

        sealed class AliasBinder : System.Runtime.Serialization.SerializationBinder
        {
            readonly System.Runtime.Serialization.SerializationBinder inner;
            public AliasBinder(System.Runtime.Serialization.SerializationBinder inner) { this.inner = inner; }

            // From the command line or the probe's flags (the only way to pass it on the PS Vita),
            // read again when the probe hands its flags over.
            static string[] logFor;
            static bool logBinds;
            static bool log
            {
                get
                {
                    if (Platform.NewArgs(ref logFor)) logBinds = Platform.HasArg("-compat-log-binds");
                    return logBinds;
                }
            }

            // PowerQuest restores every saved object with a new formatter run, so the same
            // names are bound again for each of them (thousands of Type.GetType calls with
            // name parsing, which a PS Vita restore spends seconds on). The answer for a name
            // and binder type does not change; remember it.
            static readonly Dictionary<string, Type> bindings = new Dictionary<string, Type>();

            public override Type BindToType(string assemblyName, string typeName)
            {
                if (log || !MainThread.Current) return Bind(assemblyName, typeName);
                string key = inner.GetType().FullName + "|" + assemblyName + "|" + typeName;
                Type type;
                if (!bindings.TryGetValue(key, out type)) bindings[key] = type = Bind(assemblyName, typeName);
                return type;
            }

            Type Bind(string assemblyName, string typeName)
            {
                Type bound = inner.BindToType(assemblyName, typeName);
                Type type = bound ?? Type.GetType(Rename(typeName) + ", " + RenameAssembly(assemblyName));
                if (log) Debug.Log("BIND " + assemblyName + " | " + typeName + " -> " + (bound != null ? "game " : "alias ") +
                                   (type == null ? "null" : type.AssemblyQualifiedName));
                return type;
            }

            // Saves name the original assemblies, so they stay compatible with the source version.
            public override void BindToName(Type serializedType, out string assemblyName, out string typeName)
            {
                inner.BindToName(serializedType, out assemblyName, out typeName);
                if (assemblyName == null) assemblyName = Original(serializedType.Assembly.FullName);
                if (typeName == null) typeName = Original(serializedType.FullName);
            }
        }

        static string Original(string name)
        {
            return name.Replace("Recovered.PowerTools,", "Assembly-CSharp-firstpass,").Replace("Recovered.Drifter,", "Assembly-CSharp,");
        }

        // The original assemblies' names after a comma (in a type name), or at the start (an
        // assembly name).
        static readonly System.Text.RegularExpressions.Regex assemblies =
            new System.Text.RegularExpressions.Regex(@"(?<=,\s*)Assembly-CSharp(-firstpass)?(?=\s*(,|\]|$))");
        static readonly System.Text.RegularExpressions.Regex assemblyName =
            new System.Text.RegularExpressions.Regex(@"(?<=^\s*)Assembly-CSharp(-firstpass)?(?=\s*(,|\]|$))");

        static string Rename(string name)
        {
            return Rename(name, assemblies);
        }

        static string RenameAssembly(string name)
        {
            return Rename(name, assemblyName);
        }

        static string Rename(string name, System.Text.RegularExpressions.Regex original)
        {
            if (name == null || name.IndexOf("Assembly-CSharp", StringComparison.Ordinal) < 0) return name;
            return original.Replace(name, m => m.Groups[1].Success ? "Recovered.PowerTools" : "Recovered.Drifter");
        }
    }
}
