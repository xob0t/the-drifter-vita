using System;
using System.Collections.Generic;
using UnityEngine;

// Effect sprites, continued: Collider2D.OverlapPoint answered from the collider's shape.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    public static partial class EffectSprites
    {
        // A box or single-path polygon collider's shape in its local space, read at most once a
        // frame (ShapeOf).
        sealed class Shape
        {
            public Collider2D collider;
            public int frame = -1, pointsFrame, pointCount = -1;
            public bool usable;
            public bool box;
            public Matrix4x4 toLocal;
            public Vector2 offset, half;
            public Vector2[] points;
            public float minX, minY, maxX, maxY;
            // The polygon's edges (x0, y0, x1, y1) by horizontal band: band k lists every edge whose
            // y range meets the band's (Contains then tests only those; the crossings are the same).
            public float[] edges;
            public int[][] bands;
            public float bandScale;
        }

        // Horizontal bands a polygon's edges are indexed by.
        const int Bands = 32;
        // A polygon's points are read again every PointsFrames frames (2 s at 60 fps; at once
        // when their count changes). Shapes are known for the colliders asked about, those of
        // destroyed ones dropped once there are MaxShapes.
        const int PointsFrames = 120, MaxShapes = 64;
        static readonly List<Shape> shapes = new List<Shape>();

        public static bool OverlapPoint(Collider2D collider, Vector2 point)
        {
            if (!Enabled() || (object)collider == null || !MainThread.Current) return collider.OverlapPoint(point);
            Shape shape = ShapeOf(collider);
            if (shape == null) return collider.OverlapPoint(point);
            StatShapeCalls++;
            bool result = Contains(shape, point);
            if (checkColliders)
            {
                StatCheckCalls++;
                if (collider.OverlapPoint(point) != result) StatCheckMismatches++;
            }
            return result;
        }

        static Shape ShapeOf(Collider2D collider)
        {
            Shape shape = null;
            for (int i = 0; i < shapes.Count; i++)
                if (ReferenceEquals(shapes[i].collider, collider)) { shape = shapes[i]; break; }
            if (shape == null)
            {
                if (shapes.Count >= MaxShapes) shapes.RemoveAll(s => s.collider == null);
                shape = new Shape { collider = collider };
                shapes.Add(shape);
            }
            int now = Time.frameCount;
            if (shape.frame != now)
            {
                shape.frame = now;
                shape.usable = Read(shape, now);
            }
            return shape.usable ? shape : null;
        }

        // A collider the engine has in its world (enabled, active) of a shape answered here.
        static bool Read(Shape shape, int now)
        {
            Collider2D collider = shape.collider;
            if (collider == null || !collider.isActiveAndEnabled || collider.usedByComposite) return false;
            var box = collider as BoxCollider2D;
            var polygon = collider as PolygonCollider2D;
            if ((object)box == null && (object)polygon == null) return false;
            shape.toLocal = collider.transform.worldToLocalMatrix;
            shape.offset = collider.offset;
            if ((object)box != null)
            {
                if (box.edgeRadius != 0f) return false;
                shape.box = true;
                shape.half = box.size * 0.5f;
                return true;
            }
            shape.box = false;
            if (polygon.pathCount != 1) return false;
            int pointCount = polygon.GetTotalPointCount();
            if (shape.points == null || pointCount != shape.pointCount || now - shape.pointsFrame >= PointsFrames)
            {
                shape.points = polygon.GetPath(0);
                shape.pointCount = pointCount;
                shape.pointsFrame = now;
                shape.minX = shape.minY = float.MaxValue;
                shape.maxX = shape.maxY = float.MinValue;
                foreach (Vector2 p in shape.points)
                {
                    shape.minX = Mathf.Min(shape.minX, p.x);
                    shape.minY = Mathf.Min(shape.minY, p.y);
                    shape.maxX = Mathf.Max(shape.maxX, p.x);
                    shape.maxY = Mathf.Max(shape.maxY, p.y);
                }
                Index(shape);
            }
            return shape.points.Length >= 3;
        }

        static bool Contains(Shape shape, Vector2 point)
        {
            Vector3 local = shape.toLocal.MultiplyPoint3x4(new Vector3(point.x, point.y, 0f));
            float x = local.x - shape.offset.x, y = local.y - shape.offset.y;
            if (shape.box) return Mathf.Abs(x) <= shape.half.x && Mathf.Abs(y) <= shape.half.y;
            if (x < shape.minX || x > shape.maxX || y < shape.minY || y > shape.maxY) return false;
            // Even-odd crossings of a ray towards +x, over the edges of the point's band.
            int band = (int)((y - shape.minY) * shape.bandScale);
            if (band < 0) band = 0;
            if (band >= Bands) band = Bands - 1;
            int[] list = shape.bands[band];
            float[] edges = shape.edges;
            bool inside = false;
            for (int k = 0; k < list.Length; k++)
            {
                int e = list[k];
                float ax = edges[e], ay = edges[e + 1], bx = edges[e + 2], by = edges[e + 3];
                if ((ay > y) != (by > y) && x < (bx - ax) * (y - ay) / (by - ay) + ax) inside = !inside;
            }
            return inside;
        }

        static void Index(Shape shape)
        {
            Vector2[] points = shape.points;
            int n = points.Length;
            shape.edges = new float[n * 4];
            float height = shape.maxY - shape.minY;
            shape.bandScale = height > 0f ? Bands / height : 0f;
            var lists = new List<int>[Bands];
            for (int k = 0; k < Bands; k++) lists[k] = new List<int>();
            for (int i = 0, j = n - 1; i < n; j = i++)
            {
                Vector2 a = points[i], b = points[j];
                int e = i * 4;
                shape.edges[e] = a.x;
                shape.edges[e + 1] = a.y;
                shape.edges[e + 2] = b.x;
                shape.edges[e + 3] = b.y;
                // Bands as Contains computes them for the edge's lowest and highest y (one more on
                // each side against rounding).
                int first = (int)((Mathf.Min(a.y, b.y) - shape.minY) * shape.bandScale) - 1;
                int last = (int)((Mathf.Max(a.y, b.y) - shape.minY) * shape.bandScale) + 1;
                for (int k = Math.Max(first, 0); k <= Math.Min(last, Bands - 1); k++) lists[k].Add(e);
            }
            shape.bands = new int[Bands][];
            for (int k = 0; k < Bands; k++) shape.bands[k] = lists[k].ToArray();
        }
    }
}
