using System;
using System.Collections.Generic;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

// Walking costs, the FasterWalking passes of DowngradeAssemblies.Passes (Compat WalkRules;
// docs/pipeline.md "Walking").
// Part of the assembly rewriter; see DowngradeAssemblies.cs.
partial class DowngradeAssemblies
{
    // Pathfinder.FindPath(start, end) adds two nodes S and T to the room's graph, links each to
    // every node in sight (CalculateLinks: a line of sight test per node and new node, about 2 x
    // 64 a path in CemeteryGrave, the bulk of a path's cost), runs Dijkstra from S until T is
    // taken from the open list, and removes S and T again (RemoveNode removes the links to them).
    // The links to T are read only when Dijkstra takes a node from the open list (it then walks
    // that node's links, T's link being the last one, added after everything else); T's own
    // links are never walked (taking T ends the search) and RemoveNode only removes what was
    // added. So T's link to a node can be made when that node is taken, instead of up front:
    // in CalculateLinks the tests (node, T) for the graph's own nodes are left out
    // (<SightOrDefer> answers false for them), and EvaluateDijkstra, right after marking a node
    // visited, tests that node against T and adds both links as CalculateLinks would have
    // (<LinkDeferred>: same test, same cost, same order, the node's link to T last). T is
    // usually a few units ahead of the player, so only the few nodes nearer than T get
    // tested. Every link Dijkstra reads is there and in the same place; RemoveNode leaves the
    // same graph. The deferral is set in FindPath right before CalculateLinks and cleared right
    // after the search (and at the start of FindPath and FindNextPoint).
    static int LazyTargetLinks(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Pathfinder");
        if (type == null) return 0;
        TypeDefinition node = type.NestedTypes.Single(t => t.Name == "PathNode"), link = type.NestedTypes.Single(t => t.Name == "PathLink");
        FieldDefinition position = node.Fields.Single(f => f.Name == "m_position"), links = node.Fields.Single(f => f.Name == "m_links");
        FieldDefinition visited = node.Fields.Single(f => f.Name == "m_visited"), nodes = type.Fields.Single(f => f.Name == "m_nodes");
        FieldDefinition linkNode = link.Fields.Single(f => f.Name == "m_node"), linkCost = link.Fields.Single(f => f.Name == "m_cost");
        MethodDefinition sight = type.Methods.Single(m => m.Name == "HasLineOfSight" && m.Parameters.Count == 2);
        MethodDefinition calculate = type.Methods.Single(m => m.Name == "CalculateLinks");
        MethodDefinition dijkstra = type.Methods.Single(m => m.Name == "EvaluateDijkstra");
        MethodDefinition findPath = type.Methods.Single(m => m.Name == "FindPath" && m.Parameters.Count == 2);
        MethodDefinition findNext = type.Methods.SingleOrDefault(m => m.Name == "FindNextPoint");
        Func<Instruction, MethodReference> called = i => i.Operand as MethodReference;

        // CalculateLinks' only line of sight call, on two nodes' positions.
        Instruction[] sights = calculate.Body.Instructions.Where(i => called(i) == sight).ToArray();
        Instruction[] removes = dijkstra.Body.Instructions.Where(i => called(i) != null && called(i).Name == "Remove").ToArray();
        Instruction[] pathSearches = findPath.Body.Instructions.Where(i => called(i) == dijkstra).ToArray();
        Instruction[] pathLinks = findPath.Body.Instructions.Where(i => called(i) == calculate).ToArray();
        Func<Instruction, bool> positionOfNode = i => i != null && i.OpCode == OpCodes.Ldfld && i.Operand == position &&
                                                      i.Previous != null && LocalOf(calculate, i.Previous, false) != null &&
                                                      LocalOf(calculate, i.Previous, false).VariableType == node;
        Instruction visit = removes.Length == 1 && removes[0].Next != null && removes[0].Next.OpCode == OpCodes.Pop ? removes[0].Next.Next : null;
        VariableDefinition popped = visit == null ? null : LocalOf(dijkstra, visit, false);
        bool visitShape = visit != null && popped != null && visit.Next != null && visit.Next.OpCode == OpCodes.Ldc_I4_1 &&
                          visit.Next.Next != null && visit.Next.Next.OpCode == OpCodes.Stfld && visit.Next.Next.Operand == visited;
        if (sights.Length != 1 || !positionOfNode(sights[0].Previous) || !positionOfNode(sights[0].Previous.Previous.Previous) ||
            !visitShape || pathSearches.Length != 1 || pathLinks.Length != 2 || dijkstra.Parameters.Count != 2)
            throw new Exception("PowerTools.Pathfinder does not have the expected shape for lazy links");
        // The FindPath call CalculateLinks(m_nodes.Count - 2) is the second one (the first is the
        // dirty rebuild, CalculateLinks(0)); the search is EvaluateDijkstra(start, end), two locals.
        Instruction newLinks = pathLinks[1];
        VariableDefinition start = LocalOf(findPath, pathSearches[0].Previous.Previous, false), end = LocalOf(findPath, pathSearches[0].Previous, false);
        if (start == null || end == null || newLinks.Previous.OpCode != OpCodes.Sub)
            throw new Exception("PowerTools.Pathfinder.FindPath does not have the expected shape for lazy links");

        var lazy = new LazyLinks
        {
            Type = type, Node = node, Link = link, Position = position, Links = links, Nodes = nodes, LinkNode = linkNode, LinkCost = linkCost,
            Sight = sight, Calculate = calculate,
            Subtraction = calculate.Body.Instructions.Select(called).Single(m => m != null && m.Name == "op_Subtraction"),
            Magnitude = calculate.Body.Instructions.Select(called).Single(m => m != null && m.Name == "get_magnitude"),
            Owner = new FieldDefinition("<deferOwner>", FieldAttributes.Private | FieldAttributes.Static, type),
            Start = new FieldDefinition("<deferStart>", FieldAttributes.Private | FieldAttributes.Static, node),
            End = new FieldDefinition("<deferEnd>", FieldAttributes.Private | FieldAttributes.Static, node),
            Bound = new FieldDefinition("<deferBound>", FieldAttributes.Private | FieldAttributes.Static, module.TypeSystem.Single),
        };
        FieldDefinition owner = lazy.Owner, deferStart = lazy.Start, deferEnd = lazy.End, bound = lazy.Bound;
        MethodReference subtraction = lazy.Subtraction, magnitude = lazy.Magnitude;
        foreach (FieldDefinition field in new[] { owner, deferStart, deferEnd, bound }) type.Fields.Add(field);
        MethodReference on = CompatMethod(module, "WalkRules", "get_LazyLinks");
        MethodDefinition defer = SightOrDeferMethod(module, lazy);
        MethodDefinition linkDeferred = LinkDeferredMethod(module, lazy);

        // CalculateLinks: the line of sight call takes the two nodes.
        Erase(sights[0].Previous.Previous.Previous);
        Erase(sights[0].Previous);
        sights[0].Operand = defer;
        // EvaluateDijkstra: after "node.m_visited = true", link it to the deferred end node.
        ILProcessor search = dijkstra.Body.GetILProcessor();
        InsertAfter(search, visit.Next.Next, search.Create(OpCodes.Ldarg_0), search.Create(OpCodes.Ldloc, popped), search.Create(OpCodes.Call, linkDeferred));
        // FindPath: defer right before CalculateLinks(Count - 2), stop right after the search;
        // clear at the start of FindPath and FindNextPoint.
        Func<ILProcessor, Instruction[]> clear = x => new[]
        {
            x.Create(OpCodes.Ldnull), x.Create(OpCodes.Stsfld, owner), x.Create(OpCodes.Ldnull), x.Create(OpCodes.Stsfld, deferStart),
            x.Create(OpCodes.Ldnull), x.Create(OpCodes.Stsfld, deferEnd), x.Create(OpCodes.Ldc_R4, float.PositiveInfinity), x.Create(OpCodes.Stsfld, bound),
        };
        ILProcessor path = findPath.Body.GetILProcessor();
        // newLinks is "ldarg.0; ldarg.0; ldfld m_nodes; get_Count; ldc 2; sub; call": insert before its first ldarg.0.
        const int argumentsLength = 6;
        Instruction linksStart = newLinks;
        for (int i = 0; i < argumentsLength; i++) linksStart = linksStart.Previous;
        if (linksStart.OpCode != OpCodes.Ldarg_0) throw new Exception("PowerTools.Pathfinder.FindPath does not have the expected shape for lazy links");
        Instruction skip = path.Create(OpCodes.Nop);
        // The bound: no far links unless S sees T (then T's cost is at most |S - T|); with a
        // margin far above float rounding of path costs (see WalkRules.FarLinks).
        var span = new VariableDefinition(position.FieldType);
        findPath.Body.Variables.Add(span);
        findPath.Body.InitLocals = true;
        Instruction noBound = path.Create(OpCodes.Nop);
        InsertBeforeRetarget(findPath, linksStart, path.Create(OpCodes.Call, on), path.Create(OpCodes.Brfalse, skip), path.Create(OpCodes.Ldarg_0),
                             path.Create(OpCodes.Stsfld, owner), path.Create(OpCodes.Ldloc, start), path.Create(OpCodes.Stsfld, deferStart),
                             path.Create(OpCodes.Ldloc, end), path.Create(OpCodes.Stsfld, deferEnd),
                             path.Create(OpCodes.Ldc_R4, float.PositiveInfinity), path.Create(OpCodes.Stsfld, bound),
                             path.Create(OpCodes.Call, CompatMethod(module, "WalkRules", "get_FarLinks")), path.Create(OpCodes.Brfalse, noBound),
                             path.Create(OpCodes.Ldarg_0), path.Create(OpCodes.Ldloc, start), path.Create(OpCodes.Ldfld, position),
                             path.Create(OpCodes.Ldloc, end), path.Create(OpCodes.Ldfld, position), path.Create(OpCodes.Call, sight),
                             path.Create(OpCodes.Brfalse, noBound),
                             path.Create(OpCodes.Ldloc, start), path.Create(OpCodes.Ldfld, position), path.Create(OpCodes.Ldloc, end),
                             path.Create(OpCodes.Ldfld, position), path.Create(OpCodes.Call, subtraction), path.Create(OpCodes.Stloc, span),
                             path.Create(OpCodes.Ldloca, span), path.Create(OpCodes.Call, magnitude),
                             path.Create(OpCodes.Call, CompatMethod(module, "WalkRules", "FarBound")), path.Create(OpCodes.Stsfld, bound),
                             noBound, skip);
        InsertAfter(path, pathSearches[0], clear(path));
        Prepend(findPath, clear(path));
        if (findNext != null) Prepend(findNext, clear(findNext.Body.GetILProcessor()));
        return 1;
    }

    // The pathfinder's members the lazy links' methods use, and the deferral's static fields
    // (one path search at a time): owner, S, T and the far links' bound.
    class LazyLinks
    {
        public TypeDefinition Type, Node, Link;
        public FieldDefinition Position, Links, Nodes, LinkNode, LinkCost;
        public MethodDefinition Sight, Calculate;
        public MethodReference Subtraction, Magnitude;
        public FieldDefinition Owner, Start, End, Bound;
    }

    // bool <SightOrDefer>(PathNode a, PathNode b): false for (graph node, deferred end node T)
    // and, for the far links (WalkRules.FarLinks), for (graph node, S) farther from S than the
    // bound; otherwise HasLineOfSight(a.m_position, b.m_position).
    static MethodDefinition SightOrDeferMethod(ModuleDefinition module, LazyLinks lazy)
    {
        FieldDefinition position = lazy.Position, owner = lazy.Owner, deferStart = lazy.Start, deferEnd = lazy.End, bound = lazy.Bound;
        MethodReference subtraction = lazy.Subtraction, magnitude = lazy.Magnitude, sight = lazy.Sight;
        var defer = new MethodDefinition("<SightOrDefer>", MethodAttributes.Private | MethodAttributes.HideBySig, module.TypeSystem.Boolean);
        var a = new ParameterDefinition("a", ParameterAttributes.None, lazy.Node);
        var b = new ParameterDefinition("b", ParameterAttributes.None, lazy.Node);
        defer.Parameters.Add(a);
        defer.Parameters.Add(b);
        var apart = new VariableDefinition(position.FieldType);
        defer.Body.Variables.Add(apart);
        defer.Body.InitLocals = true;
        ILProcessor il = defer.Body.GetILProcessor();
        Instruction test = il.Create(OpCodes.Ldarg_0), toStart = il.Create(OpCodes.Ldarg, b);
        il.Emit(OpCodes.Ldsfld, owner);
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Bne_Un, test);
        il.Emit(OpCodes.Ldarg, b);
        il.Emit(OpCodes.Ldsfld, deferEnd);
        il.Emit(OpCodes.Bne_Un, toStart);
        il.Emit(OpCodes.Ldarg, a);
        il.Emit(OpCodes.Ldsfld, deferStart);
        il.Emit(OpCodes.Beq, test);
        il.Emit(OpCodes.Ldc_I4_0);
        il.Emit(OpCodes.Ret);
        // if (b == S && a != T && (a.m_position - b.m_position).magnitude > bound) return false;
        il.Append(toStart);
        il.Emit(OpCodes.Ldsfld, deferStart);
        il.Emit(OpCodes.Bne_Un, test);
        il.Emit(OpCodes.Ldarg, a);
        il.Emit(OpCodes.Ldsfld, deferEnd);
        il.Emit(OpCodes.Beq, test);
        il.Emit(OpCodes.Ldarg, a);
        il.Emit(OpCodes.Ldfld, position);
        il.Emit(OpCodes.Ldarg, b);
        il.Emit(OpCodes.Ldfld, position);
        il.Emit(OpCodes.Call, subtraction);
        il.Emit(OpCodes.Stloc, apart);
        il.Emit(OpCodes.Ldloca, apart);
        il.Emit(OpCodes.Call, magnitude);
        il.Emit(OpCodes.Ldsfld, bound);
        il.Emit(OpCodes.Ble_Un, test);
        il.Emit(OpCodes.Ldc_I4_0);
        il.Emit(OpCodes.Ret);
        il.Append(test);
        il.Emit(OpCodes.Ldarg, a);
        il.Emit(OpCodes.Ldfld, position);
        il.Emit(OpCodes.Ldarg, b);
        il.Emit(OpCodes.Ldfld, position);
        il.Emit(OpCodes.Call, sight);
        il.Emit(OpCodes.Ret);
        lazy.Type.Methods.Add(defer);
        return defer;
    }

    // void <LinkDeferred>(PathNode p): CalculateLinks' body for (p, deferred end node).
    static MethodDefinition LinkDeferredMethod(ModuleDefinition module, LazyLinks lazy)
    {
        TypeDefinition node = lazy.Node, link = lazy.Link;
        FieldDefinition position = lazy.Position, links = lazy.Links, nodes = lazy.Nodes, linkNode = lazy.LinkNode, linkCost = lazy.LinkCost;
        FieldDefinition owner = lazy.Owner, deferStart = lazy.Start, deferEnd = lazy.End;
        MethodReference subtraction = lazy.Subtraction, magnitude = lazy.Magnitude, sight = lazy.Sight;
        var linkDeferred = new MethodDefinition("<LinkDeferred>", MethodAttributes.Private | MethodAttributes.HideBySig, module.TypeSystem.Void);
        var p = new ParameterDefinition("p", ParameterAttributes.None, node);
        linkDeferred.Parameters.Add(p);
        var target = new VariableDefinition(node);
        var cost = new VariableDefinition(module.TypeSystem.Single);
        var difference = new VariableDefinition(position.FieldType);
        linkDeferred.Body.Variables.Add(target);
        linkDeferred.Body.Variables.Add(cost);
        linkDeferred.Body.Variables.Add(difference);
        linkDeferred.Body.InitLocals = true;
        MethodReference take = CompatMethod(module, "PathPool", "Take", link);
        // The list calls CalculateLinks itself makes.
        Func<string, string, MethodReference> listCall = (name, item) => lazy.Calculate.Body.Instructions.Select(i => i.Operand as MethodReference)
            .FirstOrDefault(m => m != null && m.Name == name && m.DeclaringType.FullName == "System.Collections.Generic.List`1<" + item + ">");
        MethodReference nodesCount = listCall("get_Count", node.FullName), nodesItem = listCall("get_Item", node.FullName),
            linksAdd = listCall("Add", link.FullName);
        if (nodesCount == null || nodesItem == null || linksAdd == null)
            throw new Exception("PowerTools.Pathfinder.CalculateLinks does not have the expected shape for lazy links");
        ILProcessor il = linkDeferred.Body.GetILProcessor();
        Instruction done = il.Create(OpCodes.Ret);
        il.Emit(OpCodes.Ldsfld, deferEnd);
        il.Emit(OpCodes.Stloc, target);
        il.Emit(OpCodes.Ldloc, target);
        il.Emit(OpCodes.Brfalse, done);
        il.Emit(OpCodes.Ldsfld, owner);
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Bne_Un, done);
        il.Emit(OpCodes.Ldarg, p);
        il.Emit(OpCodes.Ldloc, target);
        il.Emit(OpCodes.Beq, done);
        il.Emit(OpCodes.Ldarg, p);
        il.Emit(OpCodes.Ldsfld, deferStart);
        il.Emit(OpCodes.Beq, done);
        // The end node is still the graph's last node (a stale deferral is never acted on).
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldfld, nodes);
        il.Emit(OpCodes.Callvirt, nodesCount);
        il.Emit(OpCodes.Ldc_I4_1);
        il.Emit(OpCodes.Blt, done);
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldfld, nodes);
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldfld, nodes);
        il.Emit(OpCodes.Callvirt, nodesCount);
        il.Emit(OpCodes.Ldc_I4_1);
        il.Emit(OpCodes.Sub);
        il.Emit(OpCodes.Callvirt, nodesItem);
        il.Emit(OpCodes.Ldloc, target);
        il.Emit(OpCodes.Bne_Un, done);
        // if (HasLineOfSight(p.m_position, t.m_position)) { cost = (p.m_position - t.m_position).magnitude; ... }
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldarg, p);
        il.Emit(OpCodes.Ldfld, position);
        il.Emit(OpCodes.Ldloc, target);
        il.Emit(OpCodes.Ldfld, position);
        il.Emit(OpCodes.Call, sight);
        il.Emit(OpCodes.Brfalse, done);
        il.Emit(OpCodes.Ldarg, p);
        il.Emit(OpCodes.Ldfld, position);
        il.Emit(OpCodes.Ldloc, target);
        il.Emit(OpCodes.Ldfld, position);
        il.Emit(OpCodes.Call, subtraction);
        il.Emit(OpCodes.Stloc, difference);
        il.Emit(OpCodes.Ldloca, difference);
        il.Emit(OpCodes.Call, magnitude);
        il.Emit(OpCodes.Stloc, cost);
        foreach (bool forward in new[] { true, false })
        {
            // p.m_links.Add(new PathLink { m_node = t, m_cost = cost }), then the other way.
            if (forward) il.Emit(OpCodes.Ldarg, p); else il.Emit(OpCodes.Ldloc, target);
            il.Emit(OpCodes.Ldfld, links);
            il.Emit(OpCodes.Call, take);
            il.Emit(OpCodes.Dup);
            if (forward) il.Emit(OpCodes.Ldloc, target); else il.Emit(OpCodes.Ldarg, p);
            il.Emit(OpCodes.Stfld, linkNode);
            il.Emit(OpCodes.Dup);
            il.Emit(OpCodes.Ldloc, cost);
            il.Emit(OpCodes.Stfld, linkCost);
            il.Emit(OpCodes.Callvirt, linksAdd);
        }
        il.Append(done);
        lazy.Type.Methods.Add(linkDeferred);
        return linkDeferred;
    }

    // See Compat WalkRules.AreaUpdates: IsPointInAreaUpdated, IsPointInArea without its
    // UpdateObstacles, for the calls in GetClosestPointToArea and FindPath (after theirs).
    static int InnerAreaTests(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Pathfinder");
        if (type == null) return 0;
        MethodDefinition inside = type.Methods.Single(m => m.Name == "IsPointInArea" && m.Parameters.Count == 1);
        MethodDefinition update = type.Methods.Single(m => m.Name == "UpdateObstacles");
        MethodDefinition closest = type.Methods.Single(m => m.Name == "GetClosestPointToArea");
        MethodDefinition findPath = type.Methods.Single(m => m.Name == "FindPath" && m.Parameters.Count == 2);
        Instruction[] updates = inside.Body.Instructions.Where(i => i.Operand == update).ToArray();
        Instruction[] inClosest = closest.Body.Instructions.Where(i => i.Operand == inside).ToArray();
        Instruction[] inPath = findPath.Body.Instructions.Where(i => i.Operand == inside).ToArray();
        // FindPath: UpdateObstacles, the dirty rebuild (links only), then IsPointInArea(start).
        Instruction pathUpdate = findPath.Body.Instructions.FirstOrDefault(i => i.Operand == update);
        bool between = pathUpdate != null && inPath.Length == 1 && findPath.Body.Instructions.IndexOf(pathUpdate) < findPath.Body.Instructions.IndexOf(inPath[0]);
        for (Instruction i = pathUpdate; between && i != inPath[0]; i = i.Next)
        {
            var m = i.Operand as MethodReference;
            if (m != null && m != update && m.Name != "CalculateLinks" && !m.Name.StartsWith("get_")) between = false;
        }
        // GetClosestPointToArea: UpdateObstacles first; after it only geometry, list and Compat
        // math calls besides IsPointInArea.
        var pure = new HashSet<string> { "UnityEngine.Vector2", "UnityEngine.Vector3", "System.Collections.Generic.List`1", "DrifterCompat.PathMath",
                                         "DrifterCompat.ScratchLists", "PowerTools.LineIntersector" };
        bool geometry = closest.Body.Instructions.Select(i => i.Operand as MethodReference).Where(m => m != null && m != update && m != inside)
                                                 .All(m => pure.Contains(m.DeclaringType.GetElementType().FullName));
        if (updates.Length != 1 || updates[0].Previous.OpCode != OpCodes.Ldarg_0 || inClosest.Length < 1 || !between || !geometry ||
            closest.Body.Instructions.First(i => i.Operand is MethodReference).Operand != update)
            throw new Exception("PowerTools.Pathfinder does not have the expected shape for inner area tests");
        MethodDefinition clone = CloneMethod(inside, "IsPointInAreaUpdated");
        Instruction call = clone.Body.Instructions[inside.Body.Instructions.IndexOf(updates[0])];
        Erase(call.Previous);
        Erase(call);
        ILProcessor il = clone.Body.GetILProcessor();
        Instruction body = clone.Body.Instructions[0];
        Prepend(clone, il.Create(OpCodes.Call, CompatMethod(module, "WalkRules", "get_AreaUpdates")), il.Create(OpCodes.Brtrue, body),
                il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Call, inside), il.Create(OpCodes.Ret));
        foreach (Instruction i in inClosest.Concat(inPath)) i.Operand = clone;
        return inClosest.Length + inPath.Length;
    }

    // See Compat AreaMemo: GetClosestPointToArea, after its UpdateObstacles, hands the polygons
    // to AreaMemo and asks TryClosest; its answers go to StoreClosest.
    static int MemoizeClosestPoints(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Pathfinder");
        if (type == null) return 0;
        TypeDefinition poly = type.NestedTypes.Single(t => t.Name == "PathPoly");
        FieldDefinition polys = type.Fields.Single(f => f.Name == "m_pathPolys"), main = type.Fields.Single(f => f.Name == "m_mainPoly");
        FieldDefinition enabled = poly.Fields.Single(f => f.Name == "m_enabled"), verts = poly.Fields.Single(f => f.Name == "m_verts"),
            inflated = poly.Fields.Single(f => f.Name == "m_vertsInflated");
        MethodDefinition closest = type.Methods.Single(m => m.Name == "GetClosestPointToArea");
        MethodDefinition update = type.Methods.Single(m => m.Name == "UpdateObstacles");
        Instruction[] updates = closest.Body.Instructions.Where(i => i.Operand == update).ToArray();
        // Every field it reads is one of the snapshot's (or its own locals' data).
        bool reads = closest.Body.Instructions.Where(i => i.OpCode == OpCodes.Ldfld || i.OpCode == OpCodes.Ldflda || i.OpCode == OpCodes.Ldsfld)
            .Select(i => (FieldReference)i.Operand)
            .All(f => f == polys || f == enabled || f == verts || f == inflated || f.DeclaringType.Namespace == "UnityEngine");
        if (updates.Length != 1 || closest.Body.Instructions.IndexOf(updates[0]) != 1 || !reads ||
            closest.Parameters.Count != 1 || closest.ReturnType.FullName != "UnityEngine.Vector2" || closest.Body.HasExceptionHandlers)
            throw new Exception("PowerTools.Pathfinder.GetClosestPointToArea does not have the expected shape");
        // void <SnapshotPolys>(): AreaMemo.Begin, Poly for each polygon, End(m_mainPoly).
        MethodDefinition snapshot = SnapshotPolys(module, "<SnapshotPolys>", polys,
            new[] { Instruction.Create(OpCodes.Call, CompatMethod(module, "AreaMemo", "Begin")) }, CompatMethod(module, "AreaMemo", "Poly"),
            new[] { Instruction.Create(OpCodes.Ldarg_0), Instruction.Create(OpCodes.Ldfld, main), Instruction.Create(OpCodes.Call, CompatMethod(module, "AreaMemo", "End")) },
            enabled, verts, inflated);
        // In GetClosestPointToArea, after UpdateObstacles: the snapshot and the memo.
        ILProcessor il = closest.Body.GetILProcessor();
        StoreAnswers(closest, CompatMethod(module, "AreaMemo", "StoreClosest"), () => new[] { il.Create(OpCodes.Ldarg_1) });
        Instruction rest = updates[0].Next;
        ReturnIfKnown(closest, rest, CompatMethod(module, "AreaMemo", "TryClosest"), il.Create(OpCodes.Call, CompatMethod(module, "WalkRules", "get_AreaMemo")),
                      il.Create(OpCodes.Brfalse, rest), il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Call, snapshot), il.Create(OpCodes.Ldarg_1));
        return 1;
    }

    // void <name>() on the pathfinder: begin, then for each polygon of m_pathPolys "polygon, the
    // given fields of it (PathPoly's), call poly", then end.
    static MethodDefinition SnapshotPolys(ModuleDefinition module, string name, FieldDefinition polys, Instruction[] begin, MethodReference poly,
                                          Instruction[] end, params FieldDefinition[] fields)
    {
        TypeDefinition type = polys.DeclaringType;
        // The polygon list's calls, from the code that walks it.
        MethodReference[] calls = type.Methods.Where(m => m.HasBody).SelectMany(m => m.Body.Instructions).Select(i => i.Operand as MethodReference)
            .Where(m => m != null && m.DeclaringType.FullName == polys.FieldType.FullName).ToArray();
        MethodReference getItem = calls.FirstOrDefault(m => m.Name == "get_Item"), getCount = calls.FirstOrDefault(m => m.Name == "get_Count");
        if (getItem == null || getCount == null) throw new Exception("PowerTools.Pathfinder does not have the expected shape for " + name);
        var snapshot = new MethodDefinition(name, MethodAttributes.Private | MethodAttributes.HideBySig, module.TypeSystem.Void);
        var list = new VariableDefinition(polys.FieldType);
        var index = new VariableDefinition(module.TypeSystem.Int32);
        var item = new VariableDefinition(fields[0].DeclaringType);
        foreach (VariableDefinition variable in new[] { list, index, item }) snapshot.Body.Variables.Add(variable);
        snapshot.Body.InitLocals = true;
        ILProcessor il = snapshot.Body.GetILProcessor();
        Instruction condition = il.Create(OpCodes.Ldloc, index), body = il.Create(OpCodes.Ldloc, list);
        foreach (Instruction instruction in begin) il.Append(instruction);
        il.Emit(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldfld, polys);
        il.Emit(OpCodes.Stloc, list);
        il.Emit(OpCodes.Ldc_I4_0);
        il.Emit(OpCodes.Stloc, index);
        il.Emit(OpCodes.Br, condition);
        il.Append(body);
        il.Emit(OpCodes.Ldloc, index);
        il.Emit(OpCodes.Callvirt, getItem);
        il.Emit(OpCodes.Stloc, item);
        il.Emit(OpCodes.Ldloc, item);
        foreach (FieldDefinition field in fields)
        {
            il.Emit(OpCodes.Ldloc, item);
            il.Emit(OpCodes.Ldfld, field);
        }
        il.Emit(OpCodes.Call, poly);
        il.Emit(OpCodes.Ldloc, index);
        il.Emit(OpCodes.Ldc_I4_1);
        il.Emit(OpCodes.Add);
        il.Emit(OpCodes.Stloc, index);
        il.Append(condition);
        il.Emit(OpCodes.Ldloc, list);
        il.Emit(OpCodes.Callvirt, getCount);
        il.Emit(OpCodes.Blt, body);
        foreach (Instruction instruction in end) il.Append(instruction);
        il.Emit(OpCodes.Ret);
        type.Methods.Add(snapshot);
        return snapshot;
    }

    // See Compat WalkRules.InsideMemo. bool <InsideMemo>(Vector2 p): AreaMemo.TryInside, else
    // IsPointInAreaUpdated(p) given to StoreInside. IsPointInArea starts with
    // "if (WalkRules.InsideMemo && m_mainPoly != null) { UpdateObstacles(); <SnapshotPolys>();
    // return <InsideMemo>(point); }" (the same null test and update as its own body, which the
    // clone then runs without the update), and GetClosestPointToArea's inside tests (after its
    // own snapshot) call <InsideMemo>.
    static int MemoizeInsideTests(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Pathfinder");
        if (type == null) return 0;
        MethodDefinition inside = type.Methods.Single(m => m.Name == "IsPointInArea" && m.Parameters.Count == 1);
        // InnerAreaTests' clone and MemoizeClosestPoints' snapshot (earlier passes).
        MethodDefinition updated = type.Methods.Single(m => m.Name == "IsPointInAreaUpdated");
        MethodDefinition update = type.Methods.Single(m => m.Name == "UpdateObstacles");
        MethodDefinition snapshot = type.Methods.Single(m => m.Name == "<SnapshotPolys>");
        MethodDefinition closest = type.Methods.Single(m => m.Name == "GetClosestPointToArea");
        MethodDefinition inPoly = type.Methods.Single(m => m.Name == "IsPointInPoly");
        FieldDefinition main = type.Fields.Single(f => f.Name == "m_mainPoly"), polys = type.Fields.Single(f => f.Name == "m_pathPolys");
        TypeDefinition poly = type.NestedTypes.Single(t => t.Name == "PathPoly");
        FieldDefinition enabled = poly.Fields.Single(f => f.Name == "m_enabled"), verts = poly.Fields.Single(f => f.Name == "m_verts");
        // IsPointInArea: "if (m_mainPoly == null) return true; UpdateObstacles();" then a loop over
        // the polygons reading only their enabled flag and vertices, through IsPointInPoly (by
        // now Compat PathMath's, or the game's own if its body is geometry alone).
        Func<MethodDefinition, IEnumerable<MethodReference>> callees = m => m.Body.Instructions.Select(i => i.Operand as MethodReference).Where(c => c != null);
        var pureTypes = new HashSet<string> { "UnityEngine.Vector2", "UnityEngine.Mathf", "PowerTools.LineIntersector", "DrifterCompat.PathMath" };
        Instruction[] body = inside.Body.Instructions.Where(i => i.OpCode != OpCodes.Nop).ToArray();
        const int prologueLength = 7;  // ldarg.0; ldfld m_mainPoly; brtrue; ldc.i4.1; ret; ldarg.0; call UpdateObstacles
        bool shape = body.Length >= prologueLength && body[0].OpCode == OpCodes.Ldarg_0 && body[1].OpCode == OpCodes.Ldfld && body[1].Operand == main &&
                     (body[2].OpCode == OpCodes.Brtrue || body[2].OpCode == OpCodes.Brtrue_S) && body[3].OpCode == OpCodes.Ldc_I4_1 &&
                     body[4].OpCode == OpCodes.Ret && body[5].OpCode == OpCodes.Ldarg_0 && body[6].Operand == update &&
                     callees(inside).Count(c => c == update) == 1 &&
                     callees(inside).All(c => c == update || c == inPoly || (c.DeclaringType.FullName == "DrifterCompat.PathMath" && c.Name == "IsPointInPoly") ||
                                              c.DeclaringType.FullName.StartsWith("System.Collections.Generic.List`1") ||
                                              (c.DeclaringType.FullName == "System.IDisposable" && c.Name == "Dispose")) &&
                     inside.Body.Instructions.Where(i => i.OpCode == OpCodes.Ldfld || i.OpCode == OpCodes.Ldflda || i.OpCode == OpCodes.Ldsfld)
                         .All(i => i.Operand == main || i.Operand == polys || i.Operand == enabled || i.Operand == verts ||
                                   ((FieldReference)i.Operand).DeclaringType.FullName.StartsWith("System.Collections.Generic.List`1")) &&
                     !inside.Body.Instructions.Any(i => i.OpCode == OpCodes.Stfld || i.OpCode == OpCodes.Stsfld) &&
                     (!callees(inside).Contains(inPoly) || callees(inPoly).All(c => pureTypes.Contains(c.DeclaringType.FullName)));
        Instruction[] inClosest = closest.Body.Instructions.Where(i => i.Operand == updated).ToArray();
        if (!shape || inClosest.Length < 1 || !closest.Body.Instructions.Any(i => i.Operand == snapshot))
            throw new Exception("PowerTools.Pathfinder does not have the expected shape for inside memos");
        MethodReference on = CompatMethod(module, "WalkRules", "get_InsideMemo");
        // bool <InsideMemo>(Vector2 p)
        var memo = new MethodDefinition("<InsideMemo>", MethodAttributes.Private | MethodAttributes.HideBySig, module.TypeSystem.Boolean);
        var point = new ParameterDefinition("p", ParameterAttributes.None, inside.Parameters[0].ParameterType);
        memo.Parameters.Add(point);
        var result = new VariableDefinition(module.TypeSystem.Boolean);
        memo.Body.Variables.Add(result);
        memo.Body.InitLocals = true;
        ILProcessor il = memo.Body.GetILProcessor();
        Instruction miss = il.Create(OpCodes.Ldarg_0);
        il.Emit(OpCodes.Ldarg, point);
        il.Emit(OpCodes.Ldloca, result);
        il.Emit(OpCodes.Call, CompatMethod(module, "AreaMemo", "TryInside"));
        il.Emit(OpCodes.Brfalse, miss);
        il.Emit(OpCodes.Ldloc, result);
        il.Emit(OpCodes.Ret);
        il.Append(miss);
        il.Emit(OpCodes.Ldarg, point);
        il.Emit(OpCodes.Call, updated);
        il.Emit(OpCodes.Ldarg, point);
        il.Emit(OpCodes.Call, CompatMethod(module, "AreaMemo", "StoreInside"));
        il.Emit(OpCodes.Ret);
        type.Methods.Add(memo);
        foreach (Instruction call in inClosest) call.Operand = memo;
        // IsPointInArea's prologue.
        il = inside.Body.GetILProcessor();
        Instruction original = inside.Body.Instructions[0];
        Prepend(inside, il.Create(OpCodes.Call, on), il.Create(OpCodes.Brfalse, original),
                il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldfld, main), il.Create(OpCodes.Brfalse, original),
                il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Call, update),
                il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Call, snapshot),
                il.Create(OpCodes.Ldarg_0), il.Create(OpCodes.Ldarg_1), il.Create(OpCodes.Call, memo), il.Create(OpCodes.Ret));
        return 1 + inClosest.Length;
    }

    // See Compat SightEdges: FindPath hands its polygons to SightEdges right after its
    // UpdateObstacles (and stops it after the search, and at the start of FindPath and
    // FindNextPoint); HasLineOfSight asks SightEdges.TryAnswer first.
    static int FlatSight(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Pathfinder");
        if (type == null) return 0;
        TypeDefinition poly = type.NestedTypes.Single(t => t.Name == "PathPoly");
        FieldDefinition polys = type.Fields.Single(f => f.Name == "m_pathPolys");
        FieldDefinition enabled = poly.Fields.Single(f => f.Name == "m_enabled"), verts = poly.Fields.Single(f => f.Name == "m_verts");
        MethodDefinition sight = type.Methods.Single(m => m.Name == "HasLineOfSight" && m.Parameters.Count == 2);
        MethodDefinition update = type.Methods.Single(m => m.Name == "UpdateObstacles");
        MethodDefinition findPath = type.Methods.Single(m => m.Name == "FindPath" && m.Parameters.Count == 2);
        MethodDefinition dijkstra = type.Methods.Single(m => m.Name == "EvaluateDijkstra");
        MethodDefinition findNext = type.Methods.SingleOrDefault(m => m.Name == "FindNextPoint");
        // HasLineOfSight is called only from CalculateLinks, the lazy links' helpers and FindPath's
        // far bound (after its UpdateObstacles), which run only inside FindPath and FindNextPoint.
        MethodDefinition[] callers = type.Methods.Where(m => m.HasBody && m.Body.Instructions.Any(i => i.Operand == sight)).ToArray();
        MethodDefinition calculate = type.Methods.Single(m => m.Name == "CalculateLinks");
        var calculateCallers = type.Methods.Where(m => m.HasBody && m.Body.Instructions.Any(i => i.Operand == calculate)).ToArray();
        Instruction[] pathUpdates = findPath.Body.Instructions.Where(i => i.Operand == update).ToArray();
        Instruction[] searches = findPath.Body.Instructions.Where(i => i.Operand == dijkstra).ToArray();
        if (pathUpdates.Length != 1 || searches.Length != 1 ||
            callers.Any(m => m != calculate && m != findPath && !m.Name.StartsWith("<")) || calculateCallers.Any(m => m != findPath && m != findNext) ||
            sight.Parameters.Any(p => p.ParameterType.FullName != "UnityEngine.Vector2") || sight.ReturnType.FullName != "System.Boolean")
            throw new Exception("PowerTools.Pathfinder does not have the expected shape for flat sight");
        // void <SnapshotSight>(): SightEdges.Begin(this), Poly for each polygon, End.
        MethodDefinition snapshot = SnapshotPolys(module, "<SnapshotSight>", polys,
            new[] { Instruction.Create(OpCodes.Ldarg_0), Instruction.Create(OpCodes.Call, CompatMethod(module, "SightEdges", "Begin")) },
            CompatMethod(module, "SightEdges", "Poly"), new[] { Instruction.Create(OpCodes.Call, CompatMethod(module, "SightEdges", "End")) }, enabled, verts);
        MethodReference stop = CompatMethod(module, "SightEdges", "Stop");
        // FindPath: stop at the start, snapshot after UpdateObstacles, stop after the search.
        ILProcessor path = findPath.Body.GetILProcessor();
        InsertAfter(path, pathUpdates[0], path.Create(OpCodes.Ldarg_0), path.Create(OpCodes.Call, snapshot));
        path.InsertAfter(searches[0], path.Create(OpCodes.Call, stop));
        Prepend(findPath, path.Create(OpCodes.Call, stop));
        if (findNext != null) Prepend(findNext, findNext.Body.GetILProcessor().Create(OpCodes.Call, stop));
        // HasLineOfSight: if (SightEdges.TryAnswer(this, a, b, out r)) return r;
        ILProcessor test = sight.Body.GetILProcessor();
        ReturnIfKnown(sight, sight.Body.Instructions[0], CompatMethod(module, "SightEdges", "TryAnswer"),
                      test.Create(OpCodes.Ldarg_0), test.Create(OpCodes.Ldarg_1), test.Create(OpCodes.Ldarg_2));
        return 1;
    }

    // See Compat WalkRules.AnimLookup: FindDirectionalAnimation's clip search goes to
    // NamedLists.FindNamed.
    static int CacheClipLookups(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.CharacterComponent");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "FindDirectionalAnimation" && m.Parameters.Count == 2);
        Instruction find = NamedFind(method, "UnityEngine.AnimationClip");
        if (find == null || method.Body.Instructions.Count(i => CalledName(i) == "GetAnimations") != 1)
            throw new Exception("CharacterComponent.FindDirectionalAnimation does not have the expected shape");
        // The matcher must be "item alive and named the key, ignoring case".
        MethodDefinition matcher = type.Methods.SingleOrDefault(m => m.Name.Contains("FindDirectionalAnimation") && m.Name.EndsWith("Match"));
        if (matcher == null || !matcher.Body.Instructions.Any(i => CalledName(i) == "Equals") ||
            !matcher.Body.Instructions.Any(i => CalledName(i) == "op_Equality") || !IgnoresCase(matcher))
            throw new Exception("CharacterComponent.FindDirectionalAnimation's match does not have the expected shape");
        find.Operand = CompatMethod(module, "NamedLists", "FindNamed", ((GenericInstanceMethod)find.Operand).GenericArguments[0]);
        return 1;
    }

    // A method's only call to Compat Lists.Find<item, string> (a List.Find by name, after
    // InlineKeyClosures), or null.
    static Instruction NamedFind(MethodDefinition method, string item)
    {
        Instruction[] finds = method.Body.Instructions.Where(i => i.Operand is GenericInstanceMethod && Calls(i, "DrifterCompat.Lists", "Find")).ToArray();
        if (finds.Length != 1) return null;
        var find = (GenericInstanceMethod)finds[0].Operand;
        return find.GenericArguments[0].FullName == item && find.GenericArguments[1].FullName == "System.String" ? finds[0] : null;
    }

    // Whether a matcher loads StringComparison.OrdinalIgnoreCase.
    static bool IgnoresCase(MethodDefinition matcher)
    {
        return matcher.Body.Instructions.Any(i => LoadedInt(i) == (int)StringComparison.OrdinalIgnoreCase);
    }

    // See Compat WalkRules.CueLookup: SystemAudio.Play(cueName, emitter)'s cue search (by then
    // Compat Lists.Find with a static matcher, InlineKeyClosures) goes to NamedLists.FindCue.
    static int CacheCueLookups(ModuleDefinition module)
    {
        TypeDefinition type = GameType(module, "PowerTools.Quest.SystemAudio");
        if (type == null) return 0;
        MethodDefinition method = type.Methods.Single(m => m.Name == "Play" && m.Parameters.Count == 2 &&
                                                           m.Parameters[0].ParameterType.FullName == "System.String");
        Instruction find = NamedFind(method, "PowerTools.Quest.AudioCue");
        if (find == null || method.Body.Instructions.Any(i => i.OpCode == OpCodes.Starg || i.OpCode == OpCodes.Starg_S))
            throw new Exception("SystemAudio.Play(string, Transform) does not have the expected shape");
        // The matcher must be "item alive and named the key, ignoring case": item != null &&
        // string.Equals(key, item.name, OrdinalIgnoreCase).
        MethodDefinition matcher = type.Methods.SingleOrDefault(m => m.Name.StartsWith("<Play_b__") && m.Name.EndsWith("Match"));
        if (matcher == null || matcher.Parameters.Count != 2 ||
            matcher.Body.Instructions.Count(i => CalledName(i) == "Equals") != 1 ||
            !matcher.Body.Instructions.Any(i => CalledName(i) == "op_Inequality") ||
            !matcher.Body.Instructions.Any(i => CalledName(i) == "get_name" || (CalledName(i) == "Get" && ((MethodReference)i.Operand).DeclaringType.Name == "Names")) ||
            !IgnoresCase(matcher) ||
            !method.Body.Instructions.Any(i => i.OpCode == OpCodes.Ldftn && i.Operand == matcher) &&
            !type.Methods.Any(m => m.Name == matcher.Name.Replace("Match", "Get") && method.Body.Instructions.Any(i => i.Operand == m)))
            throw new Exception("SystemAudio.Play(string, Transform)'s match does not have the expected shape");
        find.Operand = CompatMethod(module, "NamedLists", "FindCue", ((GenericInstanceMethod)find.Operand).GenericArguments[0]);
        return 1;
    }
}
