using System;
using Mono.Cecil;
using Mono.Cecil.Cil;

// Writes a copy of an assembly whose methods do nothing and return defaults, for
// wrappers of native libraries a target platform lacks (e.g. Steamworks.NET on
// PS Vita). P/Invoke declarations become ordinary stubbed methods, so the native
// library is never loaded. Strings return "" and booleans false.
// Usage: StubAssembly <input.dll> <output.dll>
class StubAssembly
{
    static int Main(string[] args)
    {
        int count = 0;
        // The old bodies are never read, and nothing here resolves another assembly.
        using (var assembly = AssemblyDefinition.ReadAssembly(args[0]))
        {
            ModuleDefinition module = assembly.MainModule;
            foreach (TypeDefinition type in AllTypes(module.Types))
            {
                foreach (MethodDefinition method in type.Methods)
                {
                    if (method.IsAbstract || method.IsConstructor || method.IsRuntime) continue;
                    if (method.IsPInvokeImpl)
                    {
                        method.PInvokeInfo = null; // Cecil's setter also sets IsPInvokeImpl
                        method.IsPInvokeImpl = false;
                        method.IsPreserveSig = false;
                        method.ImplAttributes = MethodImplAttributes.IL | MethodImplAttributes.Managed;
                    }
                    Stub(method);
                    count++;
                }
            }
            assembly.Write(args[1]);
        }
        Console.WriteLine("stubbed " + count + " methods");
        return 0;
    }

    static void Stub(MethodDefinition method)
    {
        var body = new MethodBody(method);
        method.Body = body;
        ILProcessor il = body.GetILProcessor();
        // Out parameters must be assigned before returning.
        foreach (ParameterDefinition parameter in method.Parameters)
        {
            if (!parameter.IsOut || !(parameter.ParameterType is ByReferenceType)) continue;
            il.Emit(OpCodes.Ldarg, parameter);
            il.Emit(OpCodes.Initobj, ((ByReferenceType)parameter.ParameterType).ElementType);
        }
        TypeReference returns = method.ReturnType;
        // A reference has no default to return (ldnull there would be invalid IL).
        if (returns.IsByReference) throw new Exception(method.FullName + " returns by reference; there is no default to stub it with");
        if (returns.MetadataType == MetadataType.String) il.Emit(OpCodes.Ldstr, "");
        else if (returns.IsValueType || returns.IsGenericParameter)
        {
            var local = new VariableDefinition(returns);
            body.Variables.Add(local);
            body.InitLocals = true;
            il.Emit(OpCodes.Ldloca, local);
            il.Emit(OpCodes.Initobj, returns);
            il.Emit(OpCodes.Ldloc, local);
        }
        else if (returns.MetadataType != MetadataType.Void) il.Emit(OpCodes.Ldnull);
        il.Emit(OpCodes.Ret);
    }

    static System.Collections.Generic.IEnumerable<TypeDefinition> AllTypes(System.Collections.Generic.IEnumerable<TypeDefinition> types)
    {
        foreach (TypeDefinition type in types)
        {
            yield return type;
            foreach (TypeDefinition nested in AllTypes(type.NestedTypes)) yield return nested;
        }
    }
}
