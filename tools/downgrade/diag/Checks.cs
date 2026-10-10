using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using UnityEngine;

// Checks of Compat's rules against the game's own answers: -diag-check-walk, -diag-check-allocs,
// -diag-check-path and -diag-check-text at dump time, -diag-check-keys and -diag-check-anim-cull
// every frame (summed up at dump time), and -diag-check-water (run after the dump, Experiments.cs).
// Part of the parity probe (DrifterDiag); see Diag.cs.
namespace DrifterDiag
{
    public static partial class Probe
    {
        // -diag-check-walk <n>: at dump time, asks the pathfinder and the walk animations the
        // same questions with Compat's walking rules on and off (-compat-walk-original, which
        // Compat WalkRules re-reads) and counts differing answers (check-walk event): n paths
        // from the player to points around it (as the stick asks: 5 units away in every
        // direction) and n between random points of the room, each with the whole graph after
        // it (every node's position and links, which RemoveNode must leave as they were), 3n
        // inside tests (one point asked twice) and n closest points (each asked twice), every
        // cue name's search as SystemAudio.Play(name) makes it (as named, in upper case, and with
        // a letter more; each twice), and every character's FindDirectionalAnimation for the base
        // names of its clips (the clip and the flip).
        static int checkWalk;

        // How many differing answers, mismatches and key reads a check names in its event.
        const int ListedDifferences = 3, ListedMismatches = 5, ListedKeyDifferences = 20;

        // PowerQuest's Pathfinder, and the same only when its navigation mesh is valid (else null).
        static object Pathfinder()
        {
            return questInstance.GetType().GetProperty("Pathfinder").GetValue(questInstance, null);
        }

        static object ValidPathfinder()
        {
            object pathfinder = Pathfinder();
            return pathfinder != null && (bool)pathfinder.GetType().GetMethod("GetValid").Invoke(pathfinder, null) ? pathfinder : null;
        }

        // The corners of the box around the pathfinder's walkable area (its main polygon).
        static void WalkableBounds(object pathfinder, out Vector2 min, out Vector2 max)
        {
            object main = pathfinder.GetType().GetField("m_mainPoly", AnyMember).GetValue(pathfinder);
            var verts = (Vector2[])main.GetType().GetField("m_verts").GetValue(main);
            min = new Vector2(verts.Min(v => v.x), verts.Min(v => v.y));
            max = new Vector2(verts.Max(v => v.x), verts.Max(v => v.y));
        }

        // A random point of the box min-max widened by 20 units on every side.
        static Vector2 RandomPoint(System.Random random, Vector2 min, Vector2 max)
        {
            return new Vector2(Mathf.Lerp(min.x - 20, max.x + 20, (float)random.NextDouble()),
                               Mathf.Lerp(min.y - 20, max.y + 20, (float)random.NextDouble()));
        }

        static void CheckWalk(int n)
        {
            if (questInstance == null) { Event("check-walk no PowerQuest"); return; }
            const System.Reflection.BindingFlags flags = AnyMember;
            System.Reflection.Assembly game = questInstance.GetType().Assembly;
            var report = new StringBuilder("check-walk");
            var first = new StringBuilder();
            var compare = Comparer("-compat-walk-original", 80, report, first);
            object pathfinder = ValidPathfinder();
            object player = questInstance.GetType().GetProperty("Player").GetValue(questInstance, null);
            if (pathfinder != null)
            {
                Type finder = pathfinder.GetType();
                Vector2 min, max;
                WalkableBounds(pathfinder, out min, out max);
                Vector2 from = player == null ? (min + max) * 0.5f : (Vector2)player.GetType().GetMethod("GetPosition", Type.EmptyTypes).Invoke(player, null);
                System.Reflection.MethodInfo find = finder.GetMethod("FindPath"), inside = finder.GetMethod("IsPointInArea"),
                    closest = finder.GetMethod("GetClosestPointToArea");
                var nodes = (System.Collections.IList)finder.GetField("m_nodes", flags).GetValue(pathfinder);
                Func<string> graph = () =>
                {
                    var text = new StringBuilder();
                    foreach (object node in nodes)
                    {
                        text.Append(Bits(node.GetType().GetField("m_position").GetValue(node)) + ">");
                        foreach (object link in (System.Collections.IList)node.GetType().GetField("m_links").GetValue(node))
                            text.Append(nodes.IndexOf(link.GetType().GetField("m_node").GetValue(link)) + ":" +
                                        Bits(link.GetType().GetField("m_cost").GetValue(link)) + ",");
                        text.Append(";");
                    }
                    return text.Length + ":" + text.ToString().GetHashCode();
                };
                compare("paths", () =>
                {
                    var random = new System.Random(11);
                    var answers = new List<string>();
                    for (int i = 0; i < n; i++)
                    {
                        float angle = (float)random.NextDouble() * Mathf.PI * 2f;
                        Vector2 start = from + new Vector2((float)random.NextDouble() - 0.5f, (float)random.NextDouble() - 0.5f);
                        Vector2 stick = start + new Vector2(Mathf.Cos(angle), Mathf.Sin(angle)) * 5f;
                        if (!(bool)inside.Invoke(pathfinder, new object[] { stick })) stick = (Vector2)closest.Invoke(pathfinder, new object[] { stick });
                        answers.Add(Bits(find.Invoke(pathfinder, new object[] { start, stick })) + "|" + graph());
                        Vector2 a = RandomPoint(random, min, max), b = RandomPoint(random, min, max);
                        answers.Add(Bits(find.Invoke(pathfinder, new object[] { a, b })) + "|" + inside.Invoke(pathfinder, new object[] { a }) + "|" +
                                    inside.Invoke(pathfinder, new object[] { a }) + "|" + inside.Invoke(pathfinder, new object[] { stick }) + "|" +
                                    Bits(closest.Invoke(pathfinder, new object[] { b })) + "|" + Bits(closest.Invoke(pathfinder, new object[] { b })) + "|" + graph());
                    }
                    return answers;
                });
            }
            // SystemAudio.Play(name)'s cue search (Compat NamedLists.FindCue, WalkRules.CueLookup)
            // for every cue's name, in upper case and with a letter more, each asked twice.
            Type audioType = game.GetType("PowerTools.Quest.SystemAudio");
            Type namedLists = FindType("DrifterCompat.NamedLists");
            System.Reflection.MethodInfo matcherGet = audioType == null ? null : audioType.GetMethods(flags).FirstOrDefault(m => m.Name.StartsWith("<Play_b__") && m.Name.EndsWith("Get"));
            object audioSystem = audioType == null ? null : audioType.GetField("m_instance", flags | System.Reflection.BindingFlags.FlattenHierarchy).GetValue(null);
            if (namedLists != null && matcherGet != null && audioSystem != null)
            {
                var cues = (System.Collections.IList)audioType.GetField("m_audioCues", flags).GetValue(audioSystem);
                object matcher = matcherGet.Invoke(null, null);
                Type cueType = cues.GetType().GetGenericArguments()[0];
                System.Reflection.MethodInfo findCue = namedLists.GetMethod("FindCue").MakeGenericMethod(cueType);
                var cueNames = new List<string>();
                foreach (object item in cues)
                {
                    var cueObject = item as UnityEngine.Object;
                    if (cueObject == null) continue;
                    cueNames.Add(cueObject.name);
                    cueNames.Add(cueObject.name.ToUpperInvariant());
                    cueNames.Add(cueObject.name + "x");
                }
                compare("cues", () =>
                {
                    var answers = new List<string>();
                    foreach (string name in cueNames)
                        for (int twice = 0; twice < 2; twice++)
                        {
                            var found = findCue.Invoke(null, new[] { cues, name, matcher }) as UnityEngine.Object;
                            answers.Add(found == null ? "null" : found.GetInstanceID().ToString());
                        }
                    return answers;
                });
            }
            Type characterType = game.GetType("PowerTools.Quest.CharacterComponent");
            if (characterType != null)
            {
                System.Reflection.MethodInfo directional = characterType.GetMethods(flags).First(m => m.Name == "FindDirectionalAnimation" && m.GetParameters().Length == 2);
                System.Reflection.FieldInfo clipsField = characterType.GetField("m_animations", flags);
                UnityEngine.Object[] characters = UnityEngine.Object.FindObjectsOfType(characterType);
                compare("animations", () =>
                {
                    var answers = new List<string>();
                    foreach (UnityEngine.Object character in characters)
                    {
                        var clips = clipsField.GetValue(character) as List<AnimationClip>;
                        if (clips == null) continue;
                        var names = new HashSet<string>();
                        foreach (AnimationClip clip in clips.Where(c => c != null))
                            for (int cut = 0; cut <= 2 && cut < clip.name.Length; cut++) names.Add(clip.name.Substring(0, clip.name.Length - cut));
                        foreach (string name in names.OrderBy(x => x, StringComparer.Ordinal).Concat(new[] { "Nothing", "" }))
                            for (int twice = 0; twice < 2; twice++)
                            {
                                var arguments = new object[] { name, false };
                                var clip = directional.Invoke(character, arguments) as AnimationClip;
                                answers.Add((clip == null ? "null" : clip.GetInstanceID().ToString()) + "|" + arguments[1]);
                            }
                    }
                    return answers;
                });
            }
            Event(report.ToString() + first);
        }

        // Asks run's questions with Compat's rules on and off (flag absent, then present), and
        // appends the count of differing answers to report and the first ListedDifferences (cut to
        // width) to first.
        static Action<string, Func<List<string>>> Comparer(string flag, int width, StringBuilder report, StringBuilder first)
        {
            return (label, run) =>
            {
                List<string> on, off;
                try
                {
                    SetCompatFlag(flag, false);
                    on = run();
                    SetCompatFlag(flag, true);
                    off = run();
                }
                catch (Exception e)
                {
                    report.Append(" " + label + " failed " + Failure(e));
                    return;
                }
                finally { SetCompatFlag(flag, false); }
                int differ = 0;
                for (int i = 0; i < Math.Max(on.Count, off.Count); i++)
                {
                    string x = i < on.Count ? on[i] : "(none)", y = i < off.Count ? off[i] : "(none)";
                    if (x == y) continue;
                    if (differ++ < ListedDifferences) first.Append(" [" + label + " " + i + ": " + x.Substring(0, Math.Min(width, x.Length)) + " vs " + y.Substring(0, Math.Min(width, y.Length)) + "]");
                }
                report.Append(" " + label + " " + on.Count + " differ " + differ);
            };
        }

        // A check's answer as text, floats bit for bit.
        static string Bits(object value)
        {
            var vectors = value as Vector2[];
            if (vectors != null) return string.Join(";", vectors.Select(v => Bits(v)).ToArray());
            if (value is Vector2) { var v = (Vector2)value; return Bits(v.x) + "," + Bits(v.y); }
            if (value is float) return BitConverter.ToString(BitConverter.GetBytes((float)value));
            return value == null ? "null" : Convert.ToString(value, Inv);
        }

        static void CheckAllocs(int n)
        {
            if (questInstance == null) { Event("check-allocs no PowerQuest"); return; }
            const System.Reflection.BindingFlags flags = AnyMember;
            System.Reflection.Assembly game = questInstance.GetType().Assembly;
            var report = new StringBuilder("check-allocs");
            var first = new StringBuilder();
            var compare = Comparer("-compat-alloc-original", 60, report, first);

            // Path searches.
            object pathfinder = ValidPathfinder();
            if (pathfinder != null)
            {
                Type finder = pathfinder.GetType();
                Vector2 min, max;
                WalkableBounds(pathfinder, out min, out max);
                System.Reflection.MethodInfo find = finder.GetMethod("FindPath"), inside = finder.GetMethod("IsPointInArea"),
                    closest = finder.GetMethod("GetClosestPointToArea");
                compare("paths", () =>
                {
                    var random = new System.Random(7);
                    var answers = new List<string>();
                    for (int i = 0; i < n; i++)
                    {
                        Vector2 a = RandomPoint(random, min, max), b = RandomPoint(random, min, max);
                        answers.Add(Bits(find.Invoke(pathfinder, new object[] { a, b })) + "|" + inside.Invoke(pathfinder, new object[] { a }) + "|" +
                                    Bits(closest.Invoke(pathfinder, new object[] { b })));
                    }
                    return answers;
                });
            }

            // Region distances (collider points).
            Type regionType = game.GetType("PowerTools.Quest.RegionComponent");
            if (regionType != null)
            {
                System.Reflection.MethodInfo distance = regionType.GetMethod("GetDistanceIntoRegion");
                UnityEngine.Object[] regions = UnityEngine.Object.FindObjectsOfType(regionType);
                compare("regions", () =>
                {
                    var random = new System.Random(3);
                    var answers = new List<string>();
                    foreach (UnityEngine.Object region in regions)
                        for (int i = 0; i < Math.Max(1, n / 10); i++)
                        {
                            var at = new Vector2((float)random.NextDouble() * 400 - 200, (float)random.NextDouble() * 200 - 100);
                            answers.Add(Bits(distance.Invoke(region, new object[] { at })));
                            answers.Add(Bits(distance.Invoke(region, new object[] { at })));
                        }
                    return answers;
                });
            }

            // Text wrapping: each string twice with the same wrapper (stored, then remembered).
            Type wrapperType = FindType("PowerTools.TextWrapper");
            Type textType = game.GetType("PowerTools.Quest.SystemText");
            UnityEngine.Object system = textType == null ? null : UnityEngine.Object.FindObjectOfType(textType);
            TextMesh someMesh = UnityEngine.Object.FindObjectsOfType<TextMesh>().FirstOrDefault();
            List<string> strings = GameStrings(textType, system, 200);
            if (wrapperType != null && someMesh != null)
            {
                compare("wrap", () =>
                {
                    var wrap = TextWrapperMethod<Func<string, float, float, string>>(wrapperType, someMesh, "WrapTextMinimiseWidth");
                    var answers = new List<string>();
                    foreach (string text in strings)
                        foreach (float width in new[] { 60f, 120f, 181f })
                        {
                            answers.Add(wrap(text, width, 0f));
                            answers.Add(wrap(text, width, 0f));
                        }
                    return answers;
                });
            }
            if (system != null)
            {
                System.Reflection.MethodInfo parse = textType.GetMethod("ParseIdFromText");
                compare("ids", () =>
                {
                    var answers = new List<string>();
                    foreach (string text in strings.Take(50).Select((t, i) => "&" + (i * 37) + " " + t).Concat(new[] { "&x y", "plain", "&12", "" }))
                        for (int twice = 0; twice < 2; twice++)
                        {
                            var arguments = new object[] { text };
                            object id = parse.Invoke(system, arguments);
                            answers.Add(id + "|" + arguments[0] + "|" + ReferenceEquals(arguments[0], text));
                        }
                    return answers;
                });
            }

            // Transition animations (Regex.IsMatch).
            Type characterType = game.GetType("PowerTools.Quest.CharacterComponent");
            if (characterType != null)
            {
                System.Reflection.MethodInfo transition = characterType.GetMethod("GetTransitionAnim");
                System.Reflection.FieldInfo clipsField = characterType.GetField("m_animations", flags);
                UnityEngine.Object[] characters = UnityEngine.Object.FindObjectsOfType(characterType);
                compare("transitions", () =>
                {
                    var answers = new List<string>();
                    foreach (UnityEngine.Object character in characters)
                    {
                        var clips = clipsField == null ? null : clipsField.GetValue(character) as List<AnimationClip>;
                        string[] names = clips == null ? new string[0] : clips.Where(c => c != null).Select(c => c.name).Take(25).ToArray();
                        foreach (string from in names)
                            foreach (string to in names)
                                foreach (bool flip in new[] { false, true })
                                    answers.Add(transition.Invoke(character, new object[] { from, false, to, flip }) as string ?? "null");
                    }
                    return answers;
                });
            }

            // GetAllClickables(true) and the copy filling a given list.
            System.Reflection.MethodInfo all = questInstance.GetType().GetMethod("GetAllClickables", new[] { typeof(bool) });
            System.Reflection.MethodInfo into = questInstance.GetType().GetMethod("GetAllClickablesInto", flags);
            if (all != null && into != null)
            {
                compare("clickables", () =>
                {
                    var answers = new List<string>();
                    var list = (System.Collections.IList)Activator.CreateInstance(all.ReturnType);
                    for (int i = 0; i < 3; i++)
                    {
                        var fresh = (System.Collections.IList)all.Invoke(questInstance, new object[] { true });
                        var filled = (System.Collections.IList)into.Invoke(questInstance, new object[] { true, list });
                        answers.Add(fresh.Count + "|" + filled.Count + "|" + Enumerable.Range(0, fresh.Count).All(k => k < filled.Count && ReferenceEquals(fresh[k], filled[k])));
                    }
                    return answers;
                });
            }

            // GUI layout rects (child arrays, sprite vertices, scratch lists).
            Type guiUtils = game.GetType("PowerTools.Quest.GuiUtils");
            Type controlType = game.GetType("PowerTools.Quest.GuiControl");
            if (guiUtils != null && controlType != null)
            {
                System.Reflection.MethodInfo rect = guiUtils.GetMethod("CalculateGuiRect");
                UnityEngine.Object[] controls = UnityEngine.Object.FindObjectsOfType(controlType);
                compare("guiRects", () =>
                {
                    var answers = new List<string>();
                    foreach (UnityEngine.Object control in controls)
                        foreach (bool children in new[] { true, false })
                        {
                            object value = rect.Invoke(null, new object[] { ((Component)control).transform, children, null, null, null });
                            answers.Add(string.Join(",", value.GetType().GetFields(flags).Select(f => Bits(f.GetValue(value))).ToArray()));
                        }
                    return answers;
                });
            }
            Event(report.ToString() + first);
        }

        // At most count of the game's strings (SystemText's table), evenly spaced through it; none
        // without a SystemText.
        static List<string> GameStrings(Type textType, UnityEngine.Object system, int count)
        {
            var strings = new List<string>();
            if (system != null)
                foreach (object data in (System.Collections.IList)textType.GetField("m_strings", AnyInstance).GetValue(system))
                {
                    string text = data.GetType().GetField("m_string").GetValue(data) as string;
                    if (!string.IsNullOrEmpty(text)) strings.Add(text);
                }
            return strings.Where((t, i) => i % Math.Max(1, strings.Count / count) == 0).Take(count).ToList();
        }

        // One of PowerTools.TextWrapper's methods, on a new wrapper for that mesh.
        static T TextWrapperMethod<T>(Type wrapperType, TextMesh mesh, string method) where T : class
        {
            object wrapper = Activator.CreateInstance(wrapperType, new object[] { mesh });
            return Delegate.CreateDelegate(typeof(T), wrapper, wrapperType.GetMethod(method)) as T;
        }

        // -diag-check-path: at dump time, checks the translation's Compat PathMath (Pathfinder's
        // line of sight, point in polygon and segment intersection) against the game's own code
        // on the room's own geometry: every pair of path nodes and polygon vertices, a grid over
        // the room and random points. Logs the cases and mismatches. The references:
        // - Segment intersection: the game's LineIntersector.HasIntersection (no rule touches it).
        // - Point in polygon: the game's Pathfinder.IsPointInPoly, through reflection. Its body is
        //   the game's; only its call to LineIntersector.HasIntersection goes to PathMath's, which
        //   the intersection check compares with the game's.
        // - Edge crossing and line of sight: the translation replaces HasLineOfSight's body, so
        //   these follow the rule as stated (Blocks), asking the game's LineIntersector per edge.
        static bool checkPath;
        // The point-in-polygon probes: a grid with these steps over the room, and this many random points.
        const float ProbeGridX = 3.7f, ProbeGridY = 2.9f;
        const int RandomProbes = 2000;

        static void CheckPath()
        {
            Type questType = QuestType();
            Type math = FindType("DrifterCompat.PathMath");
            Type lineType = questType == null ? null : questType.Assembly.GetType("PowerTools.LineIntersector");
            if (math == null || lineType == null || questInstance == null) { Event("check-path missing PathMath or LineIntersector"); return; }
            const System.Reflection.BindingFlags flags = AnyInstance;
            object finder = Pathfinder();
            var polys = new List<Vector2[]>();
            foreach (object poly in (System.Collections.IList)finder.GetType().GetField("m_pathPolys", flags).GetValue(finder))
            {
                polys.Add((Vector2[])poly.GetType().GetField("m_verts").GetValue(poly));
                polys.Add((Vector2[])poly.GetType().GetField("m_vertsInflated").GetValue(poly));
            }
            var points = new List<Vector2>();
            foreach (object node in (System.Collections.IList)finder.GetType().GetField("m_nodes", flags).GetValue(finder))
                points.Add((Vector2)node.GetType().GetField("m_position").GetValue(node));
            foreach (Vector2[] verts in polys) points.AddRange(verts);
            float minX = points.Min(v => v.x) - 20, maxX = points.Max(v => v.x) + 20, minY = points.Min(v => v.y) - 20, maxY = points.Max(v => v.y) + 20;
            var random = new System.Random(1);
            var probes = new List<Vector2>(points);
            for (float x = minX; x <= maxX; x += ProbeGridX) for (float y = minY; y <= maxY; y += ProbeGridY) probes.Add(new Vector2(x, y));
            for (int i = 0; i < RandomProbes; i++) probes.Add(new Vector2(minX + (float)random.NextDouble() * (maxX - minX), minY + (float)random.NextDouble() * (maxY - minY)));
            object intersector = Activator.CreateInstance(lineType);
            var setFirst = (Action<Vector2, Vector2>)Delegate.CreateDelegate(typeof(Action<Vector2, Vector2>), intersector, lineType.GetMethod("SetFirstLine"));
            var calculate = (Func<Vector2, Vector2, bool>)Delegate.CreateDelegate(typeof(Func<Vector2, Vector2, bool>), intersector, lineType.GetMethod("Calculate"));
            var hasIntersection = (Func<Vector2, Vector2, Vector2, Vector2, bool>)Delegate.CreateDelegate(typeof(Func<Vector2, Vector2, Vector2, Vector2, bool>), lineType.GetMethod("HasIntersection"));
            var crosses = (Func<Vector2, Vector2, Vector2[], bool>)Delegate.CreateDelegate(typeof(Func<Vector2, Vector2, Vector2[], bool>), math.GetMethod("SegmentCrosses"));
            var inPoly = (Func<Vector2[], Vector2, bool>)Delegate.CreateDelegate(typeof(Func<Vector2[], Vector2, bool>), math.GetMethod("IsPointInPoly"));
            var gameInPoly = (Func<Vector2[], Vector2, bool>)Delegate.CreateDelegate(typeof(Func<Vector2[], Vector2, bool>),
                finder.GetType().GetMethod("IsPointInPoly", System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public));
            var near = (Func<Vector2, Vector2, bool>)Delegate.CreateDelegate(typeof(Func<Vector2, Vector2, bool>), math.GetMethod("Near"));
            var fastIntersection = (Func<Vector2, Vector2, Vector2, Vector2, bool>)Delegate.CreateDelegate(typeof(Func<Vector2, Vector2, Vector2, Vector2, bool>), math.GetMethod("HasIntersection"));
            long segments = 0, inside = 0, pairs = 0;
            int mismatches = 0, hits = 0, insides = 0;
            var first = new StringBuilder();
            Action<string> mismatch = what => { if (mismatches++ < ListedMismatches) first.Append(" [" + what + "]"); };
            for (int i = 0; i < points.Count; i++)
                for (int j = 0; j < points.Count; j++)
                {
                    Vector2 a = points[i], b = points[j];
                    if (near(a, b) != ((a - b).sqrMagnitude < float.Epsilon)) mismatch("near " + i + "," + j);
                    for (int v = 0; v < polys.Count; v += 2)  // m_verts, as HasLineOfSight
                    {
                        Vector2[] verts = polys[v];
                        bool original = Blocks(setFirst, calculate, a, b, verts);
                        segments++;
                        if (original) hits++;
                        if (crosses(a, b, verts) != original) mismatch("crosses " + a.x.ToString("R", Inv) + "," + a.y.ToString("R", Inv) + " " +
                                                                   b.x.ToString("R", Inv) + "," + b.y.ToString("R", Inv));
                    }
                    if (j % 3 == 0)
                    {
                        Vector2 c = points[(i * 7 + j) % points.Count], d = points[(i + j * 5) % points.Count];
                        pairs++;
                        if (hasIntersection(a, b, c, d) != fastIntersection(a, b, c, d)) mismatch("intersection " + i + "," + j);
                    }
                }
            foreach (Vector2 point in probes)
                foreach (Vector2[] verts in polys)
                {
                    bool original = gameInPoly(verts, point);
                    inside++;
                    if (original) insides++;
                    if (inPoly(verts, point) != original) mismatch("inPoly " + point.x.ToString("R", Inv) + "," + point.y.ToString("R", Inv));
                }
            // The translated Pathfinder.HasLineOfSight against the rule over its enabled polygons,
            // for every pair of points (in both directions, so its hints change).
            var sightMethod = finder.GetType().GetMethod("HasLineOfSight", flags);
            var sight = (Func<Vector2, Vector2, bool>)Delegate.CreateDelegate(typeof(Func<Vector2, Vector2, bool>), finder, sightMethod);
            var enabledVerts = new List<Vector2[]>();
            foreach (object poly in (System.Collections.IList)finder.GetType().GetField("m_pathPolys", flags).GetValue(finder))
                if ((bool)poly.GetType().GetField("m_enabled").GetValue(poly)) enabledVerts.Add((Vector2[])poly.GetType().GetField("m_verts").GetValue(poly));
            long sights = 0;
            int visible = 0;
            for (int i = 0; i < points.Count; i++)
                for (int j = 0; j < points.Count; j++)
                {
                    Vector2 a = points[i], b = points[j];
                    // Two points see each other when they coincide (squared distance below
                    // float.Epsilon) or when no enabled polygon blocks the segment between them.
                    bool original = (a - b).sqrMagnitude < float.Epsilon || !enabledVerts.Any(verts => Blocks(setFirst, calculate, a, b, verts));
                    sights++;
                    if (original) visible++;
                    if (sight(a, b) != original) mismatch("sight " + i + "," + j);
                }
            Event("check-path segments " + segments + " (" + hits + " blocked) points " + inside + " (" + insides + " inside) intersections " + pairs +
                  " sights " + sights + " (" + visible + " visible) mismatches " + mismatches + first);
        }

        // Pathfinder's rule for a blocked line, as stated: a polygon blocks the segment a-b when
        // the game's LineIntersector, given a-b as its first line, reports a crossing for any of
        // the polygon's edges, each vertex joined to the next and the last one to the first.
        static bool Blocks(Action<Vector2, Vector2> setFirst, Func<Vector2, Vector2, bool> crosses, Vector2 a, Vector2 b, Vector2[] polygon)
        {
            setFirst(a, b);
            int corners = polygon.Length;
            for (int k = 0; k < corners; k++)
                if (crosses(polygon[k], polygon[(k + 1) % corners])) return true;
            return false;
        }

        // -diag-check-text: at dump time, checks the translation's TextWrapper.GetTextWidth (Compat
        // TextWidths) against the game's rule (ReferenceTextWidth) on up to 6 of the scene's
        // TextMeshes, for 400 of the game's strings: same widths, same text left on the mesh.
        // Also times WrapTextMinimiseWidth on the longest strings.
        static bool checkText;

        // -diag-check-keys: every frame, reads joysticks 1-8's buttons 0-19 both by name
        // (Input.GetKey("joystick N button M"), as InControl does) and by KeyCode
        // (Input.GetKey(KeyCode.JoystickNButtonM), as Compat InputKeys reads them instead), and
        // counts the reads that differ, naming the first ListedKeyDifferences in events; the
        // totals are an event at dump time ("check-keys").
        static bool checkKeys;

        // -diag-check-anim-cull: every 10 frames (at the first camera, after the animation step),
        // each Animator that Compat AnimatorCulling has culled (CullCompletely) is evaluated once
        // (AlwaysAnimate, Update(0); at speed 0 what the skipped step would have done), and its
        // state, the sprites, colours and transforms under it and its SpriteAnimNodes are compared
        // with before. Any difference means the skip changed something.
        static bool checkAnimCull;
        static int animCullSamples, animCullChecked, animCullDiffer;

        static void CheckAnimCull()
        {
            if (Time.frameCount % 10 != 0) return;
            animCullSamples++;
            foreach (Animator animator in UnityEngine.Object.FindObjectsOfType<Animator>())
            {
                if (animator.cullingMode != AnimatorCullingMode.CullCompletely || !animator.isActiveAndEnabled) continue;
                string before = AnimatedState(animator);
                animator.cullingMode = AnimatorCullingMode.AlwaysAnimate;
                animator.Update(0f);
                string after = AnimatedState(animator);
                animator.cullingMode = AnimatorCullingMode.CullCompletely;
                animCullChecked++;
                if (before == after) continue;
                if (animCullDiffer++ < ListedMismatches) Event("check-anim-cull differ " + Path(animator.transform) + " " + before + " -> " + after);
            }
        }

        static string AnimatedState(Animator animator)
        {
            var text = new StringBuilder();
            AnimatorStateInfo state = animator.GetCurrentAnimatorStateInfo(0);
            text.Append(state.fullPathHash + " " + state.normalizedTime.ToString("R", Inv) + " " + animator.speed.ToString("R", Inv));
            foreach (SpriteRenderer sprite in animator.GetComponentsInChildren<SpriteRenderer>(true))
                text.Append(" " + (sprite.sprite ? sprite.sprite.GetInstanceID().ToString() : "null") + "/" + Invariant(sprite.color) + "/" + sprite.flipX + sprite.flipY + sprite.enabled);
            foreach (Transform transform in animator.GetComponentsInChildren<Transform>(true))
                text.Append(" " + Numbers("R", transform.localPosition.x, transform.localPosition.y, transform.localPosition.z) +
                            "/" + Numbers("R", transform.localScale.x, transform.localScale.y) + "/" + transform.localRotation.z.ToString("R", Inv));
            foreach (MonoBehaviour behaviour in animator.GetComponents<MonoBehaviour>())
            {
                if (behaviour == null || behaviour.GetType().Name != "SpriteAnimNodes") continue;
                foreach (var field in behaviour.GetType().GetFields(AnyInstance))
                    if (field.FieldType == typeof(Vector2) || field.FieldType == typeof(Vector3) || field.FieldType == typeof(float))
                        text.Append(" " + field.Name + "=" + Invariant(field.GetValue(behaviour)));
            }
            return text.ToString();
        }

        static int keyFrames, keyReads, keyPressed, keyDiffer;

        static void CheckKeys()
        {
            keyFrames++;
            for (int i = 0; i < JoystickButtonNames.Length; i++)
            {
                bool byName = Input.GetKey(JoystickButtonNames[i]);
                bool byCode = Input.GetKey((KeyCode)((int)KeyCode.Joystick1Button0 + i));
                keyReads++;
                if (byName) keyPressed++;
                if (byName != byCode)
                {
                    if (keyDiffer < ListedKeyDifferences) Event("check-keys differ " + JoystickButtonNames[i] + " name " + byName + " code " + byCode);
                    keyDiffer++;
                }
            }
        }

        // -diag-check-water: at dump time, for each renderer Compat WaterMasks gave the split water
        // shaders (Hidden/DrifterWaterSpriteSplit, Hidden/DrifterPropBurbleSplit) and a _WaterMask, draws its sprite's texture
        // through the game's WaterSprite shader and through the split shader (all passes, into
        // textures with a stencil buffer) at the texture's size and at 1.37 times it (positions
        // between texel centres), for several timer values and offset distances, with its mask
        // and with the default black one, and counts pixels that differ from the original
        // (water-check.txt). "noise" counts pixels the timer step changes in the original, to
        // show that the noise moves reads at all. Renderers still on WaterSprite are listed.
        // A sprite region mask (_WaterMaskRect not (1, 1, 0, 0): a sprite on a room atlas) is
        // checked over the sprite's texels only (a quad whose texture coordinates span them, as
        // Graphics.Blit resets _MainTex_ST), as the renderer reads nothing else.
        static bool checkWater;

        static void CheckWater()
        {
            var text = new StringBuilder();
            int maskId = Shader.PropertyToID("_WaterMask");
            var block = new MaterialPropertyBlock();
            var originals = new Dictionary<string, string> { { "Hidden/DrifterWaterSpriteSplit", "WaterSprite" }, { "Hidden/DrifterPropBurbleSplit", "PropBurble" } };
            // Compat WaterMasks' state (enabled, masks drawn, material copies).
            Type masks = Type.GetType("DrifterCompat.WaterMasks, DrifterCompat");
            if (masks != null)
            {
                var enabledMethod = masks.GetMethod("Enabled", AnyStatic);
                Shader splitShader = Shader.Find("Hidden/DrifterWaterSpriteSplit"), maskShader = Shader.Find("Hidden/DrifterWaterMask");
                text.Append("compat enabled " + (enabledMethod != null ? enabledMethod.Invoke(null, null) : "?") +
                            " split " + (splitShader != null ? splitShader.isSupported.ToString() : "missing") +
                            " mask " + (maskShader != null ? maskShader.isSupported.ToString() : "missing"));
                foreach (string name in new[] { "masks", "copies" })
                {
                    var field = masks.GetField(name, AnyStatic);
                    var dictionary = field != null ? field.GetValue(null) as System.Collections.IDictionary : null;
                    text.Append(" " + name + " " + (dictionary != null ? dictionary.Count.ToString() : "?"));
                }
                text.AppendLine();
            }
            foreach (SpriteRenderer renderer in UnityEngine.Object.FindObjectsOfType<SpriteRenderer>())
            {
                Material shared = renderer.sharedMaterial;
                if (shared == null || shared.shader == null || renderer.sprite == null) continue;
                string shader = shared.shader.name;
                if (!originals.ContainsKey(shader) && !originals.ContainsValue(shader)) continue;
                renderer.GetPropertyBlock(block);
                Texture mask = block.GetTexture(maskId);
                Vector4 maskRect = block.GetVector("_WaterMaskRect");
                Texture texture = renderer.sprite.texture;
                text.AppendLine("renderer " + Path(renderer.transform) + " layer " + renderer.gameObject.layer + " shader " + shader + " texture " + texture.name + " " +
                                texture.width + "x" + texture.height + " " + texture.filterMode + " mask " + (mask != null ? mask.name : "none") +
                                " offsetDist " + block.GetFloat("_OffsetDist").ToString(Inv));
                Shader water = originals.ContainsKey(shader) ? Shader.Find(originals[shader]) : null;
                if (mask == null || mask == Texture2D.blackTexture || water == null) continue;
                bool burble = originals[shader] == "PropBurble";
                var original = new Material(shared);
                original.shader = water;
                var split = new Material(shared);
                // Sprite region masks: the texture region the mask covers is x0..x0 + w (texels),
                // 1 texel wider on each side and 16 taller than the sprite (Compat WaterMasks).
                int regionX = 0, regionY = 0, regionW = texture.width, regionH = texture.height;
                bool region = maskRect != Vector4.zero && !maskRect.Equals(new Vector4(1f, 1f, 0f, 0f));
                if (region)
                {
                    int w = Mathf.RoundToInt(texture.width / maskRect.x), h = Mathf.RoundToInt(texture.height / maskRect.y);
                    regionX = Mathf.RoundToInt(-maskRect.z * w) + 1;
                    regionY = Mathf.RoundToInt(-maskRect.w * h) + 16;
                    regionW = w - 2;
                    regionH = h - 32;
                    waterRegion = new Vector4((float)regionW / texture.width, (float)regionH / texture.height,
                                              (float)regionX / texture.width, (float)regionY / texture.height);
                    split.SetVector("_WaterMaskRect", maskRect);
                    text.AppendLine("  region " + regionX + "," + regionY + " " + regionW + "x" + regionH + " mask " + mask.width + "x" + mask.height);
                }
                long total = 0, differing = 0, differingBlack = 0, noise = 0;
                foreach (float scale in new[] { 1f, 1.37f })
                    foreach (float offset in Application.platform == RuntimePlatform.PSP2 ? new[] { 0.5f, 2f, 10f } : new[] { 0.5f, 1f, 2f, 5f, 10f })
                    {
                        Color32[] previous = null;
                        for (int step = 0; step < (Application.platform == RuntimePlatform.PSP2 ? 2 : 4); step++)
                        {
                            float timer = 1.3f + step * 7.77f;
                            int width = Mathf.RoundToInt(regionW * scale), height = Mathf.RoundToInt(regionH * scale);
                            foreach (Material m in new[] { original, split })
                            {
                                if (burble) { m.SetFloat("_ScaleX", offset * 0.006f); m.SetFloat("_ScaleY", offset * 0.002f); }
                                else m.SetFloat("_OffsetDist", offset);
                                m.SetFloat("_Timer", timer);
                            }
                            Color32[] reference = BlitRead(texture, original, width, height);
                            split.SetTexture(maskId, mask);
                            int count = Differing(reference, BlitRead(texture, split, width, height));
                            split.SetTexture(maskId, Texture2D.blackTexture);
                            int countBlack = Differing(reference, BlitRead(texture, split, width, height));
                            int moved = previous != null ? Differing(reference, previous) : 0;
                            previous = reference;
                            total += reference.Length;
                            differing += count;
                            differingBlack += countBlack;
                            noise += moved;
                            text.AppendLine("  scale " + scale.ToString(Inv) + " offsetDist " + offset.ToString(Inv) + " timer " + timer.ToString(Inv) + " differing " + count +
                                            " (black mask " + countBlack + ") of " + reference.Length + " noise " + moved);
                        }
                    }
                text.AppendLine("  total differing " + differing + " (black mask " + differingBlack + ") of " + total + " noise " + noise);
                // GPU time of one draw at three times the texture's size (as QuestCamera draws the
                // HighRes layer), into textures with and without a stencil buffer.
                int w3 = regionW * 3, h3 = regionH * 3;
                split.SetTexture(maskId, mask);
                text.AppendLine("  ms per draw at 3x: original " + TimeBlit(texture, original, w3, h3, 24).ToString("0.00", Inv) +
                                " split " + TimeBlit(texture, split, w3, h3, 24).ToString("0.00", Inv) +
                                " split without stencil " + TimeBlit(texture, split, w3, h3, 0).ToString("0.00", Inv));
                UnityEngine.Object.Destroy(original);
                UnityEngine.Object.Destroy(split);
                waterRegion = WholeRegion;
            }
            File.WriteAllText(System.IO.Path.Combine(outDir, "water-check.txt"), text.ToString());
            Debug.Log("[diag] water check:\n" + text);
        }

        // The texture region the water check draws (uv scale xy, offset zw).
        static readonly Vector4 WholeRegion = new Vector4(1f, 1f, 0f, 0f);
        static Vector4 waterRegion = WholeRegion;

        // Graphics.Blit, or for a region a quad over the target with the region's texture
        // coordinates, drawn with every pass of the material in order as Blit does.
        static void WaterBlit(Texture source, RenderTexture target, Material material)
        {
            if (waterRegion.Equals(WholeRegion)) { Graphics.Blit(source, target, material); return; }
            RenderTexture.active = target;
            material.mainTexture = source;
            GL.PushMatrix();
            GL.LoadOrtho();
            float u0 = waterRegion.z, v0 = waterRegion.w, u1 = waterRegion.z + waterRegion.x, v1 = waterRegion.w + waterRegion.y;
            for (int pass = 0; pass < material.passCount; pass++)
            {
                material.SetPass(pass);
                GL.Begin(GL.QUADS);
                GL.Color(Color.white);
                GL.TexCoord2(u0, v0); GL.Vertex3(0f, 0f, 0f);
                GL.TexCoord2(u0, v1); GL.Vertex3(0f, 1f, 0f);
                GL.TexCoord2(u1, v1); GL.Vertex3(1f, 1f, 0f);
                GL.TexCoord2(u1, v0); GL.Vertex3(1f, 0f, 0f);
                GL.End();
            }
            GL.PopMatrix();
        }

        static float TimeBlit(Texture source, Material material, int width, int height, int depth)
        {
            var target = RenderTexture.GetTemporary(width, height, depth, RenderTextureFormat.ARGB32, RenderTextureReadWrite.Linear);
            var read = new Texture2D(1, 1, TextureFormat.RGBA32, false);
            RenderTexture previous = RenderTexture.active;
            float best = float.MaxValue;
            for (int run = 0; run < 3; run++)
            {
                RenderTexture.active = target;
                GL.Clear(true, true, new Color(0, 0, 0, 0));
                read.ReadPixels(new Rect(0, 0, 1, 1), 0, 0);
                float start = Time.realtimeSinceStartup;
                for (int k = 0; k < 4; k++) WaterBlit(source, target, material);
                RenderTexture.active = target;
                read.ReadPixels(new Rect(0, 0, 1, 1), 0, 0);
                best = Mathf.Min(best, (Time.realtimeSinceStartup - start) * 1000f / 4f);
            }
            RenderTexture.active = previous;
            RenderTexture.ReleaseTemporary(target);
            UnityEngine.Object.Destroy(read);
            return best;
        }

        static int Differing(Color32[] a, Color32[] b)
        {
            int count = 0;
            for (int k = 0; k < a.Length; k++)
                if (a[k].r != b[k].r || a[k].g != b[k].g || a[k].b != b[k].b || a[k].a != b[k].a)
                    count++;
            return count;
        }

        static Color32[] BlitRead(Texture source, Material material, int width, int height)
        {
            var target = RenderTexture.GetTemporary(width, height, 24, RenderTextureFormat.ARGB32, RenderTextureReadWrite.Linear);
            RenderTexture previous = RenderTexture.active;
            RenderTexture.active = target;
            GL.Clear(true, true, new Color(0, 0, 0, 0));
            WaterBlit(source, target, material);
            RenderTexture.active = target;
            // One readback texture per size: Destroy is deferred to the frame's end, and dozens
            // of new ones ran the PS Vita out of memory inside ReadPixels.
            if (waterRead == null || waterRead.width != width || waterRead.height != height)
            {
                if (waterRead != null) UnityEngine.Object.DestroyImmediate(waterRead);
                waterRead = new Texture2D(width, height, TextureFormat.RGBA32, false);
            }
            waterRead.ReadPixels(new Rect(0, 0, width, height), 0, 0);
            RenderTexture.active = previous;
            RenderTexture.ReleaseTemporary(target);
            return waterRead.GetPixels32();
        }

        static Texture2D waterRead;

        // TextWrapper's width of a string, as stated (the translation replaces its body): the sum,
        // in reading order, of the widths of the characters outside markup tags, a tag running
        // from '<' to the next '>'. A character's width comes from the wrapper's table; one not in
        // it yet is measured by showing it alone on the TextMesh and taking the renderer's bounds
        // width, and is added to the table. The mesh shows its own text again afterwards.
        static float ReferenceTextWidth(TextMesh mesh, Renderer renderer, System.Collections.Hashtable table, string s)
        {
            var shown = new List<char>();
            bool inTag = false;
            foreach (char c in s)
            {
                if (c == '<') inTag = true;
                else if (inTag) inTag = c != '>';
                else shown.Add(c);
            }
            string text = mesh.text;
            float sum = 0f;
            foreach (char c in shown)
            {
                if (!table.ContainsKey(c))
                {
                    mesh.text = c.ToString();
                    table[c] = renderer.bounds.size.x;
                }
                sum += (float)table[c];
            }
            mesh.text = text;
            return sum;
        }

        static void CheckText()
        {
            Type questType = QuestType();
            Type wrapperType = FindType("PowerTools.TextWrapper");
            Type textType = questType == null ? null : questType.Assembly.GetType("PowerTools.Quest.SystemText");
            if (wrapperType == null || textType == null) { Event("check-text missing TextWrapper or SystemText"); return; }
            List<string> strings = GameStrings(textType, UnityEngine.Object.FindObjectOfType(textType), 400);
            strings.Add("Looking <sprite=a> at <b>tags</b> and üñíçødé €");
            int meshes = 0, checks = 0, mismatches = 0;
            var first = new StringBuilder();
            foreach (TextMesh mesh in UnityEngine.Object.FindObjectsOfType<TextMesh>().Take(6))
            {
                meshes++;
                Renderer renderer = mesh.GetComponent<Renderer>();
                var width = TextWrapperMethod<Func<string, float>>(wrapperType, mesh, "GetTextWidth");
                var widths = new System.Collections.Hashtable();
                string before = mesh.text;
                foreach (string s in strings)
                {
                    string saved = mesh.text;
                    float expected = ReferenceTextWidth(mesh, renderer, widths, s);
                    float actual = width(s);
                    checks++;
                    if (!actual.Equals(expected) || mesh.text != saved)
                    {
                        if (mismatches++ < ListedMismatches)
                            first.Append(" [" + mesh.name + " '" + s.Substring(0, Math.Min(20, s.Length)) + "' " + expected.ToString("R", Inv) + " vs " + actual.ToString("R", Inv) + "]");
                    }
                }
                if (mesh.text != before) { mismatches++; first.Append(" [" + mesh.name + " text changed]"); }
            }
            var text = new StringBuilder("check-text meshes " + meshes + " strings " + strings.Count + " checks " + checks + " mismatches " + mismatches + first);
            TextMesh any = UnityEngine.Object.FindObjectsOfType<TextMesh>().FirstOrDefault();
            if (any != null)
            {
                var wrap = TextWrapperMethod<Func<string, float, float, string>>(wrapperType, any, "WrapTextMinimiseWidth");
                var watch = System.Diagnostics.Stopwatch.StartNew();
                int count = 0;
                foreach (string s in strings.OrderByDescending(s => s.Length).Take(20)) { wrap(s, 150f, 0f); count++; }
                text.Append(" wrap " + (watch.Elapsed.TotalMilliseconds / Math.Max(1, count)).ToString("0.00", Inv) + "ms each");
            }
            Event(text.ToString());
        }
    }
}
