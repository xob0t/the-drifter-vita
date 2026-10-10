using UnityEngine;

// Script costs: the pathfinder's geometry (PathMath).
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // PowerTools' Pathfinder geometry (see FastPathfinder in rewriter/Gameplay.cs). Stick
    // walking finds a path twice a frame (SystemInput.Walk: CharacterComponent.CalcWalkToFirstPoint,
    // then WalkTo); each links the two new nodes to every node with HasLineOfSight, which tests
    // the segment against every edge of every enabled polygon through LineIntersector.Calculate
    // (66 nodes and 95 edges in LabKitchen: about 40 us a test on the PS Vita, 2-3 ms a path).
    // The geometry is the standard parametric segment test (two segments cross where the
    // parameters along both, t and u, lie strictly between 0 and 1) and the even-odd rule for
    // inside tests, kept in the game's float order (the operations LineIntersector.Calculate,
    // HasIntersection and Pathfinder.IsPointInPoly do, in the order they do them) for parity, so
    // they give the same answers; only the per-edge calls, struct copies and bounds checks are
    // gone. Calculate's stored result (m_resultRatioFromStart) is not kept: Pathfinder's
    // intersector is private and only HasLineOfSight uses it, which reads nothing back.
    public static unsafe class PathMath
    {
        // Values the original keeps in Vector2 fields or float locals are kept in float locals
        // here, and expressions it evaluates in one go are written the same way, so the
        // rounding is the same also where the runtime evaluates float expressions in double
        // precision (the PC's Mono).

        // (pointA - pointB).sqrMagnitude < float.Epsilon
        public static bool Near(Vector2 a, Vector2 b)
        {
            float x = a.x - b.x, y = a.y - b.y;
            float square = x * x + y * y;
            return square < float.Epsilon;
        }

        // Whether LineIntersector.SetFirstLine(start, end), then Calculate(verts[i - 1], verts[i])
        // for each edge, starting with (verts[n - 1], verts[0]), returns true for any edge.
        // Which edge crosses does not matter, so the edge that crossed last time is tried first
        // (a line of sight blocked once is mostly blocked by the same wall from nearby points):
        // the answer is the same in any order. HasLineOfSight likewise starts with the polygon
        // that blocked last (LastPoly). Only hints: any value gives the same answers.
        public static int LastPoly;
        static Vector2[] lastVerts;
        static int lastEdge;

        public static bool SegmentCrosses(Vector2 start, Vector2 end, Vector2[] verts)
        {
            bool prefilter = WalkRules.SightPrefilter;
            int count = verts.Length;
            Vector2 last = verts[count - 1];
            float ex = end.x - start.x, ey = end.y - start.y;
            int first = ReferenceEquals(verts, lastVerts) && lastEdge < count ? lastEdge : 0;
            fixed (Vector2* points = verts)
            {
                for (int n = 0; n < count; n++)
                {
                    int j = first + n < count ? first + n : first + n - count;
                    Vector2 previous = j == 0 ? last : points[j - 1];
                    float lx = previous.x, ly = previous.y;
                    float vx = points[j].x - lx, vy = points[j].y - ly;
                    float denom = ex * vy - ey * vx;
                    if (denom == 0f) continue;
                    if (prefilter)
                    {
                        // Edges the test below rejects through t <= 0 or u <= 0, found from
                        // the signs alone, before the division (see WalkRules: SightPrefilter).
                        float tNumer = (start.y - ly) * vx - (start.x - lx) * vy;
                        if (denom > 0f ? tNumer <= 0f : tNumer >= 0f) continue;
                        float uNumer = (start.y - ly) * ex - (start.x - lx) * ey;
                        if (denom > 0f ? uNumer <= 0f : uNumer >= 0f) continue;
                    }
                    denom = 1f / denom; // now its reciprocal, as the game keeps it
                    float t = ((start.y - ly) * vx - (start.x - lx) * vy) * denom;
                    float u = ((start.y - ly) * ex - (start.x - lx) * ey) * denom;
                    if (t <= 0f || t >= 1f || u <= 0f || u >= 1f) continue;
                    lastVerts = verts;
                    lastEdge = j;
                    return true;
                }
            }
            return false;
        }

        // LineIntersector.HasIntersection: the parametric test for the segments start1-end1 and
        // start2-end2 (t along the first, u along the second).
        public static bool HasIntersection(Vector2 start1, Vector2 end1, Vector2 start2, Vector2 end2)
        {
            float ax = end1.x - start1.x, ay = end1.y - start1.y;
            float bx = end2.x - start2.x, by = end2.y - start2.y;
            float cx = start1.x - start2.x, cy = start1.y - start2.y;
            float denom = ax * by - ay * bx;
            if (denom == 0f) return false;
            denom = 1f / denom;
            float t = (cy * bx - cx * by) * denom;
            float u = (cy * ax - cx * ay) * denom;
            return !(t <= 0f || t >= 1f || u <= 0f || u >= 1f);
        }

        // Pathfinder.IsPointInPoly: the even-odd rule. The point is inside when the horizontal
        // segment to it from 0.1 left of the polygon's leftmost x (or of x = 0, whichever is
        // less) crosses an odd number of edges, each tested as HasIntersection tests it.
        // With WalkRules.InsidePrefilter, edges that do not reach across the point's height are
        // skipped before the division: the segment is horizontal (ay = point.y - sy is exactly
        // zero), so denom = ax * by and u's numerator is cy * ax exactly (minus a zero), and
        // u = (cy * ax) * (1 / (ax * by)) has the sign of cy * by (no product of room
        // coordinates is near the denormals; subtraction gives the exact sign). An edge with by
        // zero (denom zero), or cy zero or of the other sign than by (u <= 0) counts no
        // crossing either way; NaN values (no comparison true) go through the test as before.
        public static bool IsPointInPoly(Vector2[] polyPoints, Vector2 point)
        {
            int count = polyPoints.Length;
            float minX = 0f;
            bool prefilter = WalkRules.InsidePrefilter;
            fixed (Vector2* points = polyPoints)
            {
                for (int i = 0; i < count; i++) minX = Mathf.Min(minX, points[i].x);
                float sx = minX - 0.1f, sy = point.y;
                float ax = point.x - sx, ay = point.y - sy;
                int crossings = 0;
                for (int j = 0; j < count; j++)
                {
                    // points[(j + 1) % count] without the division (the Cortex-A9 has no
                    // integer divide instruction)
                    Vector2 s = points[j], e = points[j + 1 == count ? 0 : j + 1];
                    float bx = e.x - s.x, by = e.y - s.y;
                    float cx = sx - s.x, cy = sy - s.y;
                    if (prefilter && (by > 0f ? cy <= 0f : by < 0f ? cy >= 0f : by == 0f)) continue;
                    float denom = ax * by - ay * bx;
                    if (denom == 0f) continue;
                    denom = 1f / denom;
                    float t = (cy * bx - cx * by) * denom;
                    float u = (cy * ax - cx * ay) * denom;
                    if (!(t <= 0f || t >= 1f || u <= 0f || u >= 1f)) crossings++;
                }
                return (crossings & 1) == 1;
            }
        }
    }
}
