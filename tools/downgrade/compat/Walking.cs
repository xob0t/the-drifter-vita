using System;
using System.Collections.Generic;
using UnityEngine;

// Walking: the pathfinder's rules (lazy and far links, flat line of sight, area memos).
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // Walking costs (the FasterWalking passes, rewriter/Walking.cs; docs/pipeline.md "Walking"):
    // rules that make the pathfinder and the walk animations cheaper with the same answers.
    // -compat-walk-original turns all of them off; each has its own switch (named at its class or
    // property).
    public static class WalkRules
    {
        static string[] configuredFor;
        static bool prefilter, lazyLinks, areaUpdates, animLookup, areaMemo, flatSight, farLinks, cueLookup, insideMemo, insidePrefilter;

        static void Check()
        {
            if (!Platform.NewArgs(ref configuredFor)) return;
            bool on = !Platform.HasArg("-compat-walk-original");
            prefilter = on && !Platform.HasArg("-compat-no-sight-prefilter");
            lazyLinks = on && !Platform.HasArg("-compat-no-lazy-links");
            areaUpdates = on && !Platform.HasArg("-compat-no-area-updates");
            animLookup = on && !Platform.HasArg("-compat-no-anim-lookup");
            areaMemo = on && !Platform.HasArg("-compat-no-area-memo");
            flatSight = on && !Platform.HasArg("-compat-no-flat-sight");
            farLinks = on && !Platform.HasArg("-compat-no-far-links");
            cueLookup = on && !Platform.HasArg("-compat-no-cue-lookup");
            insideMemo = on && !Platform.HasArg("-compat-no-inside-memo");
            insidePrefilter = on && !Platform.HasArg("-compat-no-inside-prefilter");
        }

        // PathMath.SegmentCrosses and SightEdges (every line of sight test) are the parametric
        // segment test: the sight line and the edge cross where both parameters, t along the line
        // and u along the edge, lie strictly between 0 and 1, each its numerator (tNumer, uNumer)
        // times the reciprocal of the shared denominator (denom). The numerators are float values
        // well above the denormals or exactly zero (differences of products of room coordinates),
        // and 1 / denom is finite, so t <= 0 exactly when tNumer is zero or of the other sign than
        // denom, likewise u: those edges are skipped before the division, with the numerators
        // computed as the test computes them (in float locals; rounding a double to float keeps
        // its sign, so this holds in either precision). Then the test runs as before.
        // -compat-no-sight-prefilter.
        public static bool SightPrefilter { get { Check(); return prefilter; } }

        // Pathfinder.FindPath links the end point's new node to the graph's nodes lazily: see
        // LazyTargetLinks in rewriter/Walking.cs. -compat-no-lazy-links.
        public static bool LazyLinks { get { Check(); return lazyLinks; } }

        // Pathfinder.GetClosestPointToArea and FindPath call IsPointInArea, which first brings
        // the obstacles up to date (UpdateObstacles: each enabled polygon's Transform position,
        // a node rebuild when one moved), right after they did it themselves; nothing runs in
        // between that could move one (their own geometry math), so the calls after the first
        // find nothing to do. Those inner calls skip it. -compat-no-area-updates.
        public static bool AreaUpdates { get { Check(); return areaUpdates; } }

        // CharacterComponent.FindDirectionalAnimation finds the clip by name among all of the
        // character's clips (Mick has hundreds; 2-3 ms a call on the PS Vita, at every animation
        // or facing change while walking): answered from Compat NamedLists (kept per list and
        // name with the items up to the answer, while no name was assigned and the answer is
        // alive). -compat-no-anim-lookup.
        public static bool AnimLookup { get { Check(); return animLookup; } }

        // SystemAudio.Play(cueName, emitter) (footsteps and most of the game's sounds) finds the
        // cue by name among SystemAudio's 1584 cues: the first one alive and named cueName,
        // ignoring case (FootstepGravel is the 1286th: about 4 ms a footstep on the PS Vita).
        // Answered from Compat NamedLists like AnimLookup (the list is SystemAudio's own and
        // only changes in the editor). -compat-no-cue-lookup.
        public static bool CueLookup { get { Check(); return cueLookup; } }

        // Pathfinder.FindPath, when the start S sees the end point T (tested first, as
        // CalculateLinks would): T gets the cost |S - T| when Dijkstra takes S, so its final cost
        // is at most that. A graph node j can only be taken before T with a cost at most T's, and
        // any path's cost to j is at least |S - j| up to float rounding (link costs are float
        // distances, summed in float: well under 1e-4 relative over a thousand links). So a node
        // with |S - j| > FarBound(|S - T|) is never taken before T: its link from S would only put
        // it in Dijkstra's open list with a cost that is never the smallest before the search
        // ends, so the line of sight test (S, j) and the link are left out (rewriter/Walking.cs,
        // <SightOrDefer>). The path and the graph afterwards are the same; most of
        // a path's remaining tests go (the room's nodes beyond the point 5 units ahead).
        // -compat-no-far-links.
        public static bool FarLinks { get { Check(); return farLinks; } }

        public static float FarBound(float distance)
        {
            return distance * 1.0001f + 0.001f;
        }

        // See PathMath.IsPointInPoly (every inside test: the walk target and start, closest
        // points). -compat-no-inside-prefilter.
        public static bool InsidePrefilter { get { Check(); return insidePrefilter; } }

        // See SightEdges. -compat-no-flat-sight.
        public static bool FlatSight { get { Check(); return flatSight; } }

        // See AreaMemo. -compat-no-area-memo.
        public static bool AreaMemo { get { Check(); return areaMemo; } }

        // Pathfinder.IsPointInArea(p) (the walk target, asked by CalcWalkToFirstPoint and again
        // by WalkTo each frame of stick walking) and the inside tests in GetClosestPointToArea
        // (each inflated vertex nearer than the best so far, and the projections that cross an
        // edge: the bulk of its cost) depend only on p and the polygons after the caller's
        // UpdateObstacles. With AreaMemo's snapshot (taken right after that update, compared bit
        // for bit) the answers are kept per point while the polygons stay the same (Compat
        // AreaMemo.TryInside/StoreInside; rewriter/Walking.cs, <InsideMemo>). Needs
        // the area memo and the inner area tests on. -compat-no-inside-memo.
        public static bool InsideMemo { get { Check(); return insideMemo && areaMemo && areaUpdates; } }
    }

    public static unsafe class SightEdges
    {
        // Pathfinder.HasLineOfSight(a, b) during a path search (rewriter/Walking.cs FlatSight:
        // FindPath hands its polygons over right after its UpdateObstacles, and the search's line
        // of sight tests ask TryAnswer first). It answers whether any edge of an enabled polygon
        // crosses the segment, with PathMath.SegmentCrosses' test edge by edge (same float
        // operations on the same values; the order of the edges does not change the answer), over
        // one array holding the enabled polygons' vertices, so a test makes one call instead of
        // one per polygon, through no game code. The polygons cannot change during the search
        // (only UpdateObstacles moves or enables them, and it has just run); they are compared
        // bit for bit with the last search's and copied when they differ (PolySnapshot).
        // Searches test the graph's fixed nodes over and over as the start: the sign of the
        // prefilter's numerator tNumer = (start.y - ly) * vx - (start.x - lx) * vy depends only on
        // the start and the edge, so it is kept per start (bits) and edge while the polygons stay
        // the same: the same float expression on the same values, the same sign. Without the
        // prefilter (WalkRules.SightPrefilter) the edges are tested as SegmentCrosses tests them
        // then. Main thread only.
        static readonly PolySnapshot snapshot = new PolySnapshot();
        static bool active, prefilter;
        static object owner;
        static Vector2[] flat = new Vector2[0];
        static int[] polyStart = new int[0], polyLength = new int[0];
        static int polys, lastPoly;
        static readonly Dictionary<long, sbyte[]> signs = new Dictionary<long, sbyte[]>();
        // Starts whose signs are kept, and starts seen, before all are dropped: only a bound
        // (they are dropped whenever the polygons change).
        const int MaxSigns = 512, MaxSeen = 1024;

        public static void Begin(object pathfinder)
        {
            owner = pathfinder;
            snapshot.Begin();
            active = false;
        }

        public static void Poly(object poly, bool isEnabled, Vector2[] verts)
        {
            snapshot.Poly(poly, isEnabled, verts, null);
        }

        public static void End()
        {
            if (snapshot.End()) Flatten();
            prefilter = WalkRules.SightPrefilter;
            active = MainThread.Current && WalkRules.FlatSight;
        }

        // The enabled polygons' vertices into one array; the signs kept were for the old ones.
        static void Flatten()
        {
            Vector2[][] verts = snapshot.Verts;
            bool[] enabled = snapshot.Enabled;
            int count = snapshot.Count, total = 0;
            polys = 0;
            for (int i = 0; i < count; i++)
                if (enabled[i] && verts[i] != null && verts[i].Length > 0) { total += verts[i].Length; polys++; }
            flat = new Vector2[total];
            polyStart = new int[polys];
            polyLength = new int[polys];
            int at = 0, p = 0;
            for (int i = 0; i < count; i++)
            {
                if (!enabled[i] || verts[i] == null || verts[i].Length == 0) continue;
                polyStart[p] = at;
                polyLength[p] = verts[i].Length;
                Array.Copy(verts[i], 0, flat, at, verts[i].Length);
                at += verts[i].Length;
                p++;
            }
            signs.Clear();
            seen.Clear();
            lastPoly = 0;
        }

        public static void Stop()
        {
            active = false;
            owner = null;
        }

        internal static long Key(Vector2 point)
        {
            return ((long)FloatBits.Of(point.x) << 32) | (uint)FloatBits.Of(point.y);
        }

        // Starts seen once (the search's own start, new each path) get no signs kept: only a
        // start seen again (a graph node) does.
        static readonly HashSet<long> seen = new HashSet<long>();

        // 1, -1, 0, or 2 for NaN (which no test rejects); null for a start seen the first time.
        static sbyte[] Signs(Vector2 start)
        {
            long key = Key(start);
            sbyte[] found;
            if (signs.TryGetValue(key, out found)) return found;
            if (seen.Count >= MaxSeen) seen.Clear();
            if (seen.Add(key)) return null;
            found = new sbyte[flat.Length];
            fixed (Vector2* points = flat)
                for (int p = 0; p < polys; p++)
                {
                    Vector2* poly = points + polyStart[p];
                    int length = polyLength[p];
                    Vector2 last = poly[length - 1];
                    for (int j = 0; j < length; j++)
                    {
                        Vector2 previous = j == 0 ? last : poly[j - 1];
                        float lx = previous.x, ly = previous.y;
                        float vx = poly[j].x - lx, vy = poly[j].y - ly;
                        float tNumer = (start.y - ly) * vx - (start.x - lx) * vy;
                        found[polyStart[p] + j] = (sbyte)(tNumer > 0f ? 1 : tNumer < 0f ? -1 : tNumer == 0f ? 0 : 2);
                    }
                }
            if (signs.Count >= MaxSigns) signs.Clear();
            signs[key] = found;
            return found;
        }

        public static bool TryAnswer(object pathfinder, Vector2 a, Vector2 b, out bool sight)
        {
            sight = true;
            if (!active || !ReferenceEquals(pathfinder, owner)) return false;
            if (PathMath.Near(a, b)) return true;
            sbyte[] sign = prefilter ? Signs(a) : null;
            fixed (Vector2* points = flat)
            {
                for (int n = 0; n < polys; n++)
                {
                    int p = lastPoly + n < polys ? lastPoly + n : lastPoly + n - polys;
                    if (Crosses(a, b, points + polyStart[p], polyLength[p], sign, polyStart[p]))
                    {
                        lastPoly = p;
                        sight = false;
                        return true;
                    }
                }
            }
            return true;
        }

        // PathMath.SegmentCrosses, with the prefilter as WalkRules.SightPrefilter says (the sign
        // of tNumer read from the kept signs when there are some).
        static bool Crosses(Vector2 start, Vector2 end, Vector2* points, int count, sbyte[] sign, int offset)
        {
            Vector2 last = points[count - 1];
            float ex = end.x - start.x, ey = end.y - start.y;
            for (int j = 0; j < count; j++)
            {
                Vector2 previous = j == 0 ? last : points[j - 1];
                float lx = previous.x, ly = previous.y;
                float vx = points[j].x - lx, vy = points[j].y - ly;
                float denom = ex * vy - ey * vx;
                if (denom == 0f) continue;
                if (prefilter)
                {
                    if (sign != null)
                    {
                        int tSign = sign[offset + j];
                        if (denom > 0f ? tSign == 0 || tSign == -1 : tSign == 0 || tSign == 1) continue;
                    }
                    else
                    {
                        float tNumer = (start.y - ly) * vx - (start.x - lx) * vy;
                        if (denom > 0f ? tNumer <= 0f : tNumer >= 0f) continue;
                    }
                    float uNumer = (start.y - ly) * ex - (start.x - lx) * ey;
                    if (denom > 0f ? uNumer <= 0f : uNumer >= 0f) continue;
                }
                denom = 1f / denom;
                float t = ((start.y - ly) * vx - (start.x - lx) * vy) * denom;
                float u = ((start.y - ly) * ex - (start.x - lx) * ey) * denom;
                if (t <= 0f || t >= 1f || u <= 0f || u >= 1f) continue;
                return true;
            }
            return false;
        }

        // When a scene unloads (SceneUnloads).
        public static void Clear()
        {
            snapshot.Clear();
            flat = new Vector2[0];
            polys = 0;
            signs.Clear();
            seen.Clear();
            Stop();
        }
    }

    // Pathfinder.GetClosestPointToArea(point): the nearest point of the walkable area (a pass
    // over every polygon edge, with inside tests). Walking with the stick asks it twice a frame
    // for the same point when the point 5 units ahead is outside the area (CalcWalkToFirstPoint,
    // then WalkTo). Its answer depends only on the point and the polygons (the list, each one's
    // enabled flag, vertices and inflated vertices, and the main polygon) after its own
    // UpdateObstacles. There the downgrader (rewriter/Walking.cs MemoizeClosestPoints) hands the
    // polygons to Begin/Poly/End, which compare them bit for bit with the last ones (copying them
    // when they differ: PolySnapshot), and asks TryClosest; the answer is stored per point while
    // they stay the same. Main thread only. -compat-no-area-memo.
    public static class AreaMemo
    {
        static readonly PolySnapshot snapshot = new PolySnapshot();
        static object mainPoly;
        static bool armed;
        // The last answers (a ring): the two calls a frame ask for one point, so a few are enough.
        const int Closest = 8;
        static readonly Vector2[] points = new Vector2[Closest], answers = new Vector2[Closest];
        static int stored, next;

        public static void Begin()
        {
            snapshot.Begin();
            armed = false;
            insideArmed = false;
        }

        public static void Poly(object poly, bool isEnabled, Vector2[] polyVerts, Vector2[] polyInflated)
        {
            snapshot.Poly(poly, isEnabled, polyVerts, polyInflated);
        }

        // Ends a call's snapshot: drops the kept answers if the polygons changed, then lets the call keep its own.
        public static void End(object main)
        {
            bool changed = snapshot.End();
            if (!ReferenceEquals(main, mainPoly)) changed = true;
            mainPoly = main;
            if (changed)
            {
                stored = 0;
                inside.Clear();
            }
            armed = MainThread.Current;
            insideArmed = armed;
        }

        // Inside tests (WalkRules.InsideMemo), per point while the snapshot stays the same; the
        // callers (IsPointInArea, GetClosestPointToArea) take the snapshot first in each call.
        static readonly Dictionary<long, bool> inside = new Dictionary<long, bool>();
        static bool insideArmed;
        // Points kept before all are dropped: only a bound for a long stay with the same polygons.
        const int MaxInside = 4096;

        public static bool TryInside(Vector2 point, out bool result)
        {
            result = false;
            if (!insideArmed || !WalkRules.InsideMemo) return false;
            return inside.TryGetValue(SightEdges.Key(point), out result);
        }

        public static bool StoreInside(bool result, Vector2 point)
        {
            if (!insideArmed || !WalkRules.InsideMemo) return result;
            if (inside.Count >= MaxInside) inside.Clear();
            inside[SightEdges.Key(point)] = result;
            return result;
        }

        public static bool TryClosest(Vector2 point, out Vector2 result)
        {
            result = default(Vector2);
            if (!armed) return false;
            long key = SightEdges.Key(point);
            for (int i = 0; i < stored; i++)
                if (SightEdges.Key(points[i]) == key)
                {
                    result = answers[i];
                    armed = false;
                    return true;
                }
            return false;
        }

        public static Vector2 StoreClosest(Vector2 result, Vector2 point)
        {
            if (!armed || !WalkRules.AreaMemo) return result;
            armed = false;
            points[next] = point;
            answers[next] = result;
            next = (next + 1) % points.Length;
            if (stored < points.Length) stored++;
            return result;
        }

        // When a scene unloads (SceneUnloads).
        public static void Clear()
        {
            snapshot.Clear();
            mainPoly = null;
            stored = 0;
            armed = false;
            inside.Clear();
            insideArmed = false;
        }
    }

    // The pathfinder's polygons as a call handed them over (Begin, Poly for each, End), kept
    // from call to call: each polygon (its object, enabled flag, vertices and inflated vertices)
    // is compared bit for bit with the one at its index last time and copied only when it
    // differs, into the kept arrays when they have the length (no garbage while a polygon moves).
    // Main thread only.
    sealed unsafe class PolySnapshot
    {
        object[] polys = new object[0];
        public bool[] Enabled = new bool[0];
        public Vector2[][] Verts = new Vector2[0][], Inflated = new Vector2[0][];
        public int Count;
        int index;
        bool changed;

        public void Begin()
        {
            index = 0;
            changed = false;
        }

        public void Poly(object poly, bool isEnabled, Vector2[] verts, Vector2[] inflated)
        {
            if (index >= polys.Length)
            {
                int size = Math.Max(4, index * 2);
                Array.Resize(ref polys, size);
                Array.Resize(ref Enabled, size);
                Array.Resize(ref Verts, size);
                Array.Resize(ref Inflated, size);
            }
            if (index >= Count || !ReferenceEquals(polys[index], poly) || Enabled[index] != isEnabled ||
                !Same(Verts[index], verts) || !Same(Inflated[index], inflated))
            {
                changed = true;
                polys[index] = poly;
                Enabled[index] = isEnabled;
                Verts[index] = Copy(Verts[index], verts);
                Inflated[index] = Copy(Inflated[index], inflated);
            }
            index++;
        }

        // Whether the polygons differ from the last call's.
        public bool End()
        {
            if (index != Count) changed = true;
            for (int i = index; i < Count; i++) { polys[i] = null; Verts[i] = null; Inflated[i] = null; }
            Count = index;
            return changed;
        }

        public void Clear()
        {
            Array.Clear(polys, 0, polys.Length);
            Array.Clear(Verts, 0, Verts.Length);
            Array.Clear(Inflated, 0, Inflated.Length);
            Count = 0;
        }

        static bool Same(Vector2[] kept, Vector2[] now)
        {
            if (kept == null || now == null) return ReferenceEquals(kept, now);
            if (kept.Length != now.Length) return false;
            fixed (Vector2* a = kept, b = now)
            {
                int* x = (int*)a, y = (int*)b;
                for (int i = 0; i < kept.Length * 2; i++)
                    if (x[i] != y[i]) return false;
            }
            return true;
        }

        static Vector2[] Copy(Vector2[] kept, Vector2[] values)
        {
            if (values == null) return null;
            if (kept == null || kept.Length != values.Length) return (Vector2[])values.Clone();
            Array.Copy(values, kept, values.Length);
            return kept;
        }
    }
}
