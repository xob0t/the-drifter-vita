// Serialized-field model generator for Unity 2018.2 MonoBehaviour type trees.
// Reads assemblies with Mono.Cecil and emits, per requested class, a JSON tree
// describing the fields Unity's managed serializer writes, in declaration order.
// Usage: TypeModelGen <managedDir> <requests.json> <output.json>
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;
using Mono.Cecil;

public class Req
{
    [JsonPropertyName("assembly")] public string Assembly { get; set; }
    [JsonPropertyName("fullname")] public string FullName { get; set; }
}

public static class Program
{
    static DefaultAssemblyResolver resolver;
    static string managedDir;
    static readonly Dictionary<string, AssemblyDefinition> asmCache = new Dictionary<string, AssemblyDefinition>();

    public static int Main(string[] args)
    {
        managedDir = args[0];
        resolver = new DefaultAssemblyResolver();
        resolver.AddSearchDirectory(managedDir);
        if (args.Length > 3 && args[3] == "dump")
        {
            // Debug aid: list every field of one type with its attributes and flags.
            var parts = args[1].Split('|');
            var dtd = FindType(parts[0], parts[1]);
            foreach (var f in dtd.Fields)
                Console.WriteLine($"{f.Name} : {f.FieldType.FullName} pub={f.IsPublic} static={f.IsStatic} nser={f.IsNotSerialized} attrs=[{string.Join(",", f.CustomAttributes.Select(a => a.AttributeType.FullName))}]");
            Console.WriteLine($"base: {dtd.BaseType?.FullName}");
            return 0;
        }
        var reqs = JsonSerializer.Deserialize<List<Req>>(File.ReadAllText(args[1]));
        var outList = new List<Dictionary<string, object>>();
        foreach (var r in reqs)
        {
            var entry = new Dictionary<string, object>
            {
                ["assembly"] = r.Assembly,
                ["fullname"] = r.FullName,
            };
            try
            {
                var td = FindType(r.Assembly, r.FullName);
                if (td == null)
                    entry["error"] = "type not found";
                else
                    entry["model"] = Struct(td, new Dictionary<string, TypeReference>(), new List<string>());
            }
            catch (Exception e)
            {
                entry["error"] = e.GetType().Name + ": " + e.Message;
            }
            outList.Add(entry);
        }
        var opts = new JsonSerializerOptions { WriteIndented = true };
        File.WriteAllText(args[2], JsonSerializer.Serialize(outList, opts));
        return 0;
    }

    static TypeDefinition FindType(string assemblyFile, string fullName)
    {
        AssemblyDefinition asm;
        if (!asmCache.TryGetValue(assemblyFile, out asm))
        {
            var rp = new ReaderParameters { AssemblyResolver = resolver, ReadingMode = ReadingMode.Deferred };
            asm = AssemblyDefinition.ReadAssembly(Path.Combine(managedDir, assemblyFile), rp);
            asmCache[assemblyFile] = asm;
        }
        return asm.MainModule.GetType(fullName);
    }

    static Dictionary<string, object> Prim(string t, int size)
    {
        return new Dictionary<string, object> { ["k"] = "prim", ["t"] = t, ["size"] = size };
    }

    static Dictionary<string, object> Unsupported(string reason)
    {
        return new Dictionary<string, object> { ["k"] = "unsupported", ["reason"] = reason };
    }

    static Dictionary<string, object> PrimitiveFor(string fullName)
    {
        switch (fullName)
        {
            case "System.Boolean": return Prim("bool", 1);
            case "System.Byte": return Prim("UInt8", 1);
            case "System.SByte": return Prim("SInt8", 1);
            case "System.Int16": return Prim("short", 2);
            case "System.UInt16": return Prim("unsigned short", 2);
            case "System.Int32": return Prim("int", 4);
            case "System.UInt32": return Prim("unsigned int", 4);
            case "System.Int64": return Prim("long long", 8);
            case "System.UInt64": return Prim("unsigned long long", 8);
            case "System.Single": return Prim("float", 4);
            case "System.Double": return Prim("double", 8);
            case "System.Char": return Prim("char", 2);
        }
        return null;
    }

    // Substitutes a generic parameter from the map; other references are returned unchanged.
    static TypeReference Subst(TypeReference t, Dictionary<string, TypeReference> map)
    {
        if (t is GenericParameter gp)
        {
            TypeReference v;
            return map.TryGetValue(gp.Name, out v) ? v : null;
        }
        return t;
    }

    // Returns null when Unity's serializer would not write the field at all.
    static Dictionary<string, object> Node(TypeReference tr0, Dictionary<string, TypeReference> map, List<string> stack)
    {
        var tr = Subst(tr0, map);
        if (tr == null) return Unsupported("unbound generic parameter " + tr0.Name);
        if (tr is ByReferenceType || tr is PointerType) return null;

        if (tr is ArrayType at)
        {
            if (at.Rank != 1) return Unsupported("multi-dimensional array");
            var e = Node(ElementOf(at.ElementType, map), map, stack);
            if (e == null) return Unsupported("array of non-serialized element " + at.ElementType.FullName);
            return new Dictionary<string, object> { ["k"] = "array", ["e"] = e };
        }

        if (tr is GenericInstanceType git && git.ElementType.FullName == "System.Collections.Generic.List`1")
        {
            var e = Node(ElementOf(git.GenericArguments[0], map), map, stack);
            if (e == null) return Unsupported("List of non-serialized element");
            return new Dictionary<string, object> { ["k"] = "array", ["e"] = e };
        }

        if (tr.FullName == "System.String") return new Dictionary<string, object> { ["k"] = "string" };
        // Engine structs whose serialized storage is a private native field.
        if (tr.FullName == "UnityEngine.LayerMask") return new Dictionary<string, object> { ["k"] = "bitfield" };
        if (tr.FullName == "UnityEngine.AnimationCurve") return new Dictionary<string, object> { ["k"] = "curve" };
        var prim = PrimitiveFor(tr.FullName);
        if (prim != null) return prim;

        var td = tr.Resolve();
        if (td == null) return Unsupported("cannot resolve " + tr.FullName);

        if (td.IsEnum)
        {
            var value = td.Fields.First(f => f.Name == "value__").FieldType.FullName;
            return PrimitiveFor(value) ?? Unsupported("enum underlying " + value);
        }

        if (IsUnityObject(td))
            return new Dictionary<string, object> { ["k"] = "pptr", ["t"] = SimpleName(td) };

        if (td.IsInterface) return null;
        bool serializable = td.IsSerializable || (td.IsValueType && td.Namespace.StartsWith("UnityEngine"));
        if (!serializable) return null;

        var targetMap = MapFor(tr, td, map);
        if (td.HasGenericParameters && targetMap.Count < td.GenericParameters.Count)
            return Unsupported("open generic " + td.FullName);

        string key = td.FullName + "<" + string.Join(",", targetMap.Values.Select(v => v.FullName)) + ">";
        if (stack.Contains(key)) return Unsupported("recursive serialized type " + key);
        stack.Add(key);
        var node = Struct(td, targetMap, stack);
        stack.RemoveAt(stack.Count - 1);
        return node;
    }

    // Type argument of a list or array, with outer generic parameters substituted.
    static TypeReference ElementOf(TypeReference t, Dictionary<string, TypeReference> map)
    {
        return Subst(t, map) ?? t;
    }

    static Dictionary<string, TypeReference> MapFor(TypeReference tr, TypeDefinition td, Dictionary<string, TypeReference> outerMap)
    {
        var map = new Dictionary<string, TypeReference>();
        if (tr is GenericInstanceType git)
        {
            for (int i = 0; i < td.GenericParameters.Count && i < git.GenericArguments.Count; i++)
            {
                var arg = git.GenericArguments[i];
                map[td.GenericParameters[i].Name] = Subst(arg, outerMap) ?? arg;
            }
        }
        return map;
    }

    static string SimpleName(TypeDefinition td)
    {
        return td.Name.Contains('`') ? td.Name.Substring(0, td.Name.IndexOf('`')) : td.Name;
    }

    static bool IsUnityObject(TypeDefinition td)
    {
        var cur = td;
        int guard = 0;
        while (cur != null && guard++ < 64)
        {
            if (cur.FullName == "UnityEngine.Object") return true;
            if (cur.BaseType == null) return false;
            cur = cur.BaseType.Resolve();
        }
        return false;
    }

    static bool IsRootBase(TypeReference bt)
    {
        if (bt.FullName == "System.Object" || bt.FullName == "System.ValueType" || bt.FullName == "System.Enum") return true;
        return bt.Namespace.StartsWith("UnityEngine");
    }

    static bool HasAttr(ICustomAttributeProvider p, string name)
    {
        return p.CustomAttributes.Any(a => a.AttributeType.Name == name || a.AttributeType.Name == name + "Attribute");
    }

    static Dictionary<string, object> Struct(TypeDefinition td, Dictionary<string, TypeReference> map, List<string> stack)
    {
        var fields = new List<object>();
        CollectFields(td, map, stack, fields);
        return new Dictionary<string, object>
        {
            ["k"] = "struct",
            ["t"] = SimpleName(td),
            ["full"] = td.FullName,
            ["fields"] = fields,
        };
    }

    static void CollectFields(TypeDefinition td, Dictionary<string, TypeReference> map, List<string> stack, List<object> fields)
    {
        // Base-class fields are serialized first, as if declared in the derived class.
        if (td.BaseType != null && !IsRootBase(td.BaseType))
        {
            var btd = td.BaseType.Resolve();
            if (btd != null)
                CollectFields(btd, MapFor(td.BaseType, btd, map), stack, fields);
        }
        foreach (var f in td.Fields)
        {
            if (f.IsStatic || f.IsLiteral || f.IsNotSerialized) continue;
            if (!(f.IsPublic || HasAttr(f, "SerializeField"))) continue;
            if (f.Name.Contains('<')) continue;
            var node = Node(f.FieldType, map, stack);
            if (node == null)
            {
                fields.Add(new Dictionary<string, object>
                {
                    ["n"] = f.Name,
                    ["skipped"] = f.FieldType.FullName,
                });
                continue;
            }
            fields.Add(new Dictionary<string, object>
            {
                ["n"] = f.Name,
                ["readonly"] = f.IsInitOnly,
                ["node"] = node,
            });
        }
    }
}
