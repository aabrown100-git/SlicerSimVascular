// Face-label-aware remeshing helper, built on geometry3Sharp.
//
// Reads a triangle surface with one integer group per triangle (the carrier for
// SimVascular's ModelFaceID), remeshes it to a target edge length, and writes the
// result back with every triangle still labelled.  The labels survive because the
// remesher works on DMesh3 triangle groups directly: a split inherits its parent's
// group and a collapse cannot merge two groups without crossing a constrained edge.
//
// Group boundaries -- the seams between labelled faces -- get one of three
// treatments, chosen with --seam-mode:
//
//   slide  the seam's discretization is free but its geometry is held: seam vertices
//          are resampled at the target edge length and reprojected onto the original
//          seam curve.  Junctions where three or more faces meet stay pinned.
//   pin    the seam is held vertex for vertex, no split, collapse or flip.
//   free   no seam constraint at all (for measuring what the constraints buy).
//
// The same three modes apply to the mesh's own open boundaries (--boundary-mode) and
// to sharp feature edges (--feature-mode, with --feature-angle).

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using g3;
using gs;

namespace SlicerSimVascular
{
    internal enum ConstraintMode { Free, Slide, Pin }

    internal static class Program
    {
        private const string Magic = "G3M1";

        private static int Main(string[] args)
        {
            try
            {
                return Run(args);
            }
            catch (Exception exc)
            {
                Console.Error.WriteLine("g3remesh: " + exc.Message);
                Console.Error.WriteLine(exc.StackTrace);
                return 1;
            }
        }

        private static int Run(string[] args)
        {
            var opts = ParseArgs(args);
            if (opts == null)
            {
                Usage();
                return 2;
            }

            DMesh3 mesh = ReadG3M(opts.Input);
            var report = new Dictionary<string, object>
            {
                ["input_vertices"] = mesh.VertexCount,
                ["input_triangles"] = mesh.TriangleCount,
                ["input_groups"] = GroupHistogram(mesh),
            };

            // The projection target keeps remeshed vertices on the surface we were
            // handed, rather than letting smoothing shrink the anatomy.
            DMesh3 original = new DMesh3(mesh);
            MeshProjectionTarget surfaceTarget = MeshProjectionTarget.Auto(original, false);

            var remesher = new RemesherPro(mesh);
            remesher.SetTargetEdgeLength(opts.TargetEdgeLength);
            if (opts.MinEdgeFraction > 0.0)
                remesher.MinEdgeLength = opts.TargetEdgeLength * opts.MinEdgeFraction;
            if (opts.MaxEdgeFraction > 0.0)
                remesher.MaxEdgeLength = opts.TargetEdgeLength * opts.MaxEdgeFraction;
            remesher.SmoothSpeedT = opts.SmoothSpeed;
            remesher.SmoothType = opts.SmoothType;
            remesher.EnableSmoothing = opts.Smooth;
            remesher.EnableFlips = true;
            remesher.EnableCollapses = true;
            remesher.EnableSplits = true;
            remesher.PreventNormalFlips = opts.PreventNormalFlips;
            if (opts.ProjectToInput)
            {
                remesher.SetProjectionTarget(surfaceTarget);
                remesher.ProjectionMode = Remesher.TargetProjectionMode.AfterRefinement;
            }
            remesher.SetExternalConstraints(new MeshConstraints());

            int constrainedChains = 0;
            int pinnedVertices = 0;

            // Sharp features first, so a seam constraint can overwrite a feature one
            // where the two coincide -- the seam is the stronger claim.
            if (opts.FeatureMode != ConstraintMode.Free)
            {
                double cosLimit = Math.Cos(opts.FeatureAngleDeg * MathUtil.Deg2Rad);
                constrainedChains += ApplyChainConstraints(
                    remesher.Constraints, mesh,
                    eid => IsSharpEdge(mesh, eid, cosLimit),
                    opts.FeatureMode, 1000, opts.SeamCornerAngleDeg, ref pinnedVertices);
            }

            if (opts.SeamMode != ConstraintMode.Free)
            {
                constrainedChains += ApplyChainConstraints(
                    remesher.Constraints, mesh,
                    eid => mesh.IsGroupBoundaryEdge(eid),
                    opts.SeamMode, 2000, opts.SeamCornerAngleDeg, ref pinnedVertices);
            }

            if (opts.BoundaryMode == ConstraintMode.Pin)
            {
                MeshConstraintUtil.FixAllBoundaryEdges(remesher.Constraints, mesh);
            }
            else if (opts.BoundaryMode == ConstraintMode.Slide)
            {
                MeshConstraintUtil.PreserveBoundaryLoops(remesher.Constraints, mesh);
            }

            report["constrained_chains"] = constrainedChains;
            report["pinned_vertices"] = pinnedVertices;

            var clock = System.Diagnostics.Stopwatch.StartNew();
            switch (opts.Schedule)
            {
                case "fastest":
                    remesher.FastestRemesh(opts.Iterations);
                    break;
                case "basic":
                    remesher.Precompute();
                    for (int k = 0; k < opts.Iterations; ++k)
                        remesher.BasicRemeshPass();
                    break;
                case "sharp":
                    remesher.SharpEdgeReprojectionRemesh(opts.Iterations, opts.Iterations / 2);
                    break;
                default:
                    throw new Exception($"unknown schedule {opts.Schedule}");
            }
            if (opts.ShapeFlipRounds > 0)
            {
                report["shape_flips"] = ShapeFlipPass(
                    mesh, remesher.Constraints, opts.ShapeFlipRounds);
                foreach (var kv in FlipRejections)
                    report["flip_rejected_" + kv.Key] = kv.Value;
            }
            clock.Stop();

            report["seconds"] = Math.Round(clock.Elapsed.TotalSeconds, 3);
            report["output_vertices"] = mesh.VertexCount;
            report["output_triangles"] = mesh.TriangleCount;
            report["output_groups"] = GroupHistogram(mesh);
            report["ungrouped_triangles"] = mesh.TriangleIndices().Count(t => mesh.GetTriangleGroup(t) <= 0);
            report["is_closed"] = mesh.IsClosed();
            report["boundary_edges"] = mesh.EdgeIndices().Count(e => mesh.IsBoundaryEdge(e));
            AddEdgeLengthStats(mesh, report);
            AddAngleStats(mesh, report);

            WriteG3M(opts.Output, mesh);
            if (opts.Report != null)
                File.WriteAllText(opts.Report, ToJson(report));
            Console.Out.WriteLine(ToJson(report));
            return 0;
        }

        // --------------------------------------------------------------- shape flips

        /// <summary>
        /// Flip edges that would improve the worst angle of the two triangles on them.
        ///
        /// The Remesher's own flip test is purely a valence-balance test, so a triangle
        /// whose three vertices have gone collinear is never flipped away unless doing so
        /// also happens to even out the valences.  That is what leaves slivers along a
        /// constrained seam: their short edges cannot collapse either, because collapsing
        /// an edge whose two ends sit on the same seam curve would pinch the seam, and
        /// can_collapse_vtx correctly refuses it.  Flipping is the only move left.
        ///
        /// This pass is safe to run on a labelled mesh for a structural reason rather
        /// than an empirical one: any edge it is allowed to touch has the same group on
        /// both sides -- an edge between two groups is a group-boundary edge and carries
        /// NoFlip -- so no flip here can move a label.  Flips move no vertices either, so
        /// the seam geometry the constraints just protected is untouched.
        /// </summary>
        private static readonly Dictionary<string, int> FlipRejections = new()
        {
            ["constrained"] = 0, ["edge_exists"] = 0, ["no_improvement"] = 0,
            ["would_fold"] = 0, ["flip_failed"] = 0,
        };

        private static int ShapeFlipPass(DMesh3 mesh, MeshConstraints cons, int rounds)
        {
            const double improvementDeg = 0.5;
            int total = 0;
            for (int round = 0; round < rounds; ++round)
            {
                int flipped = 0;
                foreach (int eid in mesh.EdgeIndices().ToArray())
                {
                    if (!mesh.IsEdge(eid) || mesh.IsBoundaryEdge(eid))
                        continue;
                    if (cons != null && cons.GetEdgeConstraint(eid).CanFlip == false)
                    {
                        FlipRejections["constrained"]++;
                        continue;
                    }

                    Index2i ev = mesh.GetEdgeV(eid);
                    Index2i et = mesh.GetEdgeT(eid);
                    int c = IndexUtil.find_tri_other_vtx(ev.a, ev.b, mesh.GetTriangle(et.a));
                    int d = IndexUtil.find_tri_other_vtx(ev.a, ev.b, mesh.GetTriangle(et.b));
                    if (c == DMesh3.InvalidID || d == DMesh3.InvalidID)
                        continue;
                    if (mesh.FindEdge(c, d) != DMesh3.InvalidID)
                    {
                        FlipRejections["edge_exists"]++;
                        continue;   // the flipped edge already exists
                    }

                    Vector3d pa = mesh.GetVertex(ev.a), pb = mesh.GetVertex(ev.b);
                    Vector3d pc = mesh.GetVertex(c), pd = mesh.GetVertex(d);

                    double before = Math.Min(MinAngleDeg(pa, pb, pc), MinAngleDeg(pa, pb, pd));
                    double after = Math.Min(MinAngleDeg(pc, pd, pa), MinAngleDeg(pc, pd, pb));
                    if (after < before + improvementDeg)
                    {
                        FlipRejections["no_improvement"]++;
                        continue;
                    }

                    // Refuse a flip that would fold the surface over: the two new
                    // triangles must still face the way the two old ones did.
                    Vector3d oldNormal = Vector3d.Cross(pb - pa, pc - pa) +
                                         Vector3d.Cross(pd - pa, pb - pa);
                    Vector3d newNormal = Vector3d.Cross(pd - pc, pa - pc) +
                                         Vector3d.Cross(pb - pc, pd - pc);
                    if (oldNormal.Dot(newNormal) <= 0)
                    {
                        FlipRejections["would_fold"]++;
                        continue;
                    }

                    if (mesh.FlipEdge(eid, out DMesh3.EdgeFlipInfo _) == MeshResult.Ok)
                        flipped++;
                    else
                        FlipRejections["flip_failed"]++;
                }
                total += flipped;
                if (flipped == 0)
                    break;
            }
            return total;
        }

        private static double MinAngleDeg(Vector3d a, Vector3d b, Vector3d c)
        {
            return Math.Min(
                AngleDeg(b - a, c - a),
                Math.Min(AngleDeg(a - b, c - b), AngleDeg(a - c, b - c)));
        }

        // ---------------------------------------------------------------- constraints

        /// <summary>
        /// Split the edges picked out by <paramref name="isConstrained"/> into maximal
        /// chains between junctions and constrain each one as a unit.
        ///
        /// Chains, not individual edges, are the right unit: sliding a vertex needs a
        /// curve to slide along, and that curve is the chain it sits on.  A vertex where
        /// three chains meet has no single curve, so it is pinned.
        /// </summary>
        private static int ApplyChainConstraints(
            MeshConstraints cons, DMesh3 mesh,
            Func<int, bool> isConstrained, ConstraintMode mode,
            int setIDBase, double cornerAngleDeg, ref int pinnedVertices)
        {
            var chainEdges = new HashSet<int>();
            foreach (int eid in mesh.EdgeIndices())
                if (isConstrained(eid))
                    chainEdges.Add(eid);
            if (chainEdges.Count == 0)
                return 0;

            var incident = new Dictionary<int, List<int>>();
            foreach (int eid in chainEdges)
            {
                Index2i ev = mesh.GetEdgeV(eid);
                Incident(incident, ev.a).Add(eid);
                Incident(incident, ev.b).Add(eid);
            }

            // A vertex is a junction if the chain forks there, dead-ends there, meets
            // the open boundary, has three or more groups around it -- or turns sharply.
            //
            // The sharp-turn case is what keeps a sliding seam honest.  Projecting seam
            // vertices onto the original curve guarantees they sit *on* it, but nothing
            // stops a collapse from chording across a bend, and the chord is bounded by
            // nothing but the local geometry.  Breaking the chain at its corners pins
            // them, so the parts that slide are the parts where sliding costs little.
            var junctions = new HashSet<int>();
            foreach (var kv in incident)
            {
                int vid = kv.Key;
                bool fork = kv.Value.Count != 2;
                if (fork || mesh.IsBoundaryVertex(vid) ||
                    (mesh.HasTriangleGroups && mesh.IsGroupJunctionVertex(vid)))
                {
                    junctions.Add(vid);
                    continue;
                }
                if (cornerAngleDeg > 0.0 && TurnsSharply(mesh, vid, kv.Value, cornerAngleDeg))
                    junctions.Add(vid);
            }

            if (mode == ConstraintMode.Pin)
            {
                foreach (int eid in chainEdges)
                {
                    cons.SetOrUpdateEdgeConstraint(eid, EdgeConstraint.FullyConstrained);
                    Index2i ev = mesh.GetEdgeV(eid);
                    cons.SetOrUpdateVertexConstraint(ev.a, VertexConstraint.Pinned);
                    cons.SetOrUpdateVertexConstraint(ev.b, VertexConstraint.Pinned);
                }
                pinnedVertices += incident.Count;
                return CountChains(mesh, chainEdges, incident, junctions);
            }

            int chains = 0;
            int setID = setIDBase;
            var visited = new HashSet<int>();

            // Open chains, running junction to junction.
            foreach (int start in junctions)
            {
                foreach (int eid in incident[start].ToList())
                {
                    if (visited.Contains(eid))
                        continue;
                    List<int> span = WalkChain(mesh, incident, junctions, start, eid, visited);
                    if (span.Count < 2)
                        continue;
                    if (span.Count == 2)
                    {
                        // A lone edge between two junctions has nowhere to slide to.
                        int e = mesh.FindEdge(span[0], span[1]);
                        if (e != DMesh3.InvalidID)
                            cons.SetOrUpdateEdgeConstraint(e, EdgeConstraint.FullyConstrained);
                        cons.SetOrUpdateVertexConstraint(span[0], VertexConstraint.Pinned);
                        cons.SetOrUpdateVertexConstraint(span[1], VertexConstraint.Pinned);
                        pinnedVertices += 2;
                        chains++;
                        continue;
                    }
                    var curve = new DCurve3(span.Select(v => mesh.GetVertex(v)), false);
                    MeshConstraintUtil.ConstrainVtxSpanTo(
                        cons, mesh, span, new DCurveProjectionTarget(curve), setID++);
                    pinnedVertices += 2;   // the two junction endpoints
                    chains++;
                }
            }

            // Closed chains, with no junction anywhere on them.
            foreach (int eid in chainEdges)
            {
                if (visited.Contains(eid))
                    continue;
                Index2i ev = mesh.GetEdgeV(eid);
                List<int> loop = WalkChain(mesh, incident, junctions, ev.a, eid, visited);
                if (loop.Count < 3)
                    continue;
                if (loop[loop.Count - 1] == loop[0])
                    loop.RemoveAt(loop.Count - 1);
                var curve = new DCurve3(loop.Select(v => mesh.GetVertex(v)), true);
                MeshConstraintUtil.ConstrainVtxLoopTo(
                    cons, mesh, loop, new DCurveProjectionTarget(curve), setID++);
                chains++;
            }

            return chains;
        }

        /// <summary>
        /// True if the chain bends by more than <paramref name="limitDeg"/> at this vertex.
        /// </summary>
        private static bool TurnsSharply(DMesh3 mesh, int vid, List<int> twoEdges, double limitDeg)
        {
            Index2i first = mesh.GetEdgeV(twoEdges[0]);
            Index2i second = mesh.GetEdgeV(twoEdges[1]);
            int before = (first.a == vid) ? first.b : first.a;
            int after = (second.a == vid) ? second.b : second.a;
            Vector3d here = mesh.GetVertex(vid);
            return AngleDeg(here - mesh.GetVertex(before), mesh.GetVertex(after) - here) > limitDeg;
        }

        private static List<int> WalkChain(
            DMesh3 mesh, Dictionary<int, List<int>> incident, HashSet<int> junctions,
            int startVertex, int startEdge, HashSet<int> visited)
        {
            var vertices = new List<int> { startVertex };
            int vid = startVertex;
            int eid = startEdge;
            while (true)
            {
                visited.Add(eid);
                Index2i ev = mesh.GetEdgeV(eid);
                int next = (ev.a == vid) ? ev.b : ev.a;
                vertices.Add(next);
                if (junctions.Contains(next) || next == startVertex)
                    break;
                List<int> options = incident[next];
                int following = DMesh3.InvalidID;
                foreach (int candidate in options)
                {
                    if (candidate != eid && !visited.Contains(candidate))
                    {
                        following = candidate;
                        break;
                    }
                }
                if (following == DMesh3.InvalidID)
                    break;
                vid = next;
                eid = following;
            }
            return vertices;
        }

        private static int CountChains(
            DMesh3 mesh, HashSet<int> chainEdges,
            Dictionary<int, List<int>> incident, HashSet<int> junctions)
        {
            var visited = new HashSet<int>();
            int count = 0;
            foreach (int start in junctions)
                foreach (int eid in incident[start].ToList())
                    if (!visited.Contains(eid))
                    {
                        WalkChain(mesh, incident, junctions, start, eid, visited);
                        count++;
                    }
            foreach (int eid in chainEdges)
                if (!visited.Contains(eid))
                {
                    Index2i ev = mesh.GetEdgeV(eid);
                    WalkChain(mesh, incident, junctions, ev.a, eid, visited);
                    count++;
                }
            return count;
        }

        private static List<int> Incident(Dictionary<int, List<int>> map, int key)
        {
            if (!map.TryGetValue(key, out List<int> list))
            {
                list = new List<int>(2);
                map[key] = list;
            }
            return list;
        }

        private static bool IsSharpEdge(DMesh3 mesh, int eid, double cosLimit)
        {
            Index2i et = mesh.GetEdgeT(eid);
            if (et.b == DMesh3.InvalidID)
                return false;
            Vector3d na = mesh.GetTriNormal(et.a);
            Vector3d nb = mesh.GetTriNormal(et.b);
            return na.Dot(nb) < cosLimit;
        }

        // ----------------------------------------------------------------------- stats

        private static Dictionary<string, object> GroupHistogram(DMesh3 mesh)
        {
            var counts = new SortedDictionary<int, int>();
            foreach (int tid in mesh.TriangleIndices())
            {
                int gid = mesh.GetTriangleGroup(tid);
                counts.TryGetValue(gid, out int n);
                counts[gid] = n + 1;
            }
            var result = new Dictionary<string, object>();
            foreach (var kv in counts)
                result[kv.Key.ToString(CultureInfo.InvariantCulture)] = kv.Value;
            return result;
        }

        private static void AddEdgeLengthStats(DMesh3 mesh, Dictionary<string, object> report)
        {
            var lengths = new List<double>();
            foreach (int eid in mesh.EdgeIndices())
            {
                Index2i ev = mesh.GetEdgeV(eid);
                lengths.Add(mesh.GetVertex(ev.a).Distance(mesh.GetVertex(ev.b)));
            }
            lengths.Sort();
            report["edge_length_min"] = Round(lengths[0]);
            report["edge_length_median"] = Round(lengths[lengths.Count / 2]);
            report["edge_length_mean"] = Round(lengths.Average());
            report["edge_length_max"] = Round(lengths[lengths.Count - 1]);
        }

        private static void AddAngleStats(DMesh3 mesh, Dictionary<string, object> report)
        {
            double worst = 180.0;
            var minAngles = new List<double>();
            int under20 = 0, under10 = 0;
            foreach (int tid in mesh.TriangleIndices())
            {
                Index3i tv = mesh.GetTriangle(tid);
                Vector3d a = mesh.GetVertex(tv.a), b = mesh.GetVertex(tv.b), c = mesh.GetVertex(tv.c);
                // Not Vector3d.AngleD: it feeds an unnormalized dot product straight to
                // Acos, so it only returns the angle for unit-length inputs.
                double smallest = Math.Min(
                    AngleDeg(b - a, c - a),
                    Math.Min(AngleDeg(a - b, c - b), AngleDeg(a - c, b - c)));
                minAngles.Add(smallest);
                worst = Math.Min(worst, smallest);
                if (smallest < 20.0) under20++;
                if (smallest < 10.0) under10++;
            }
            minAngles.Sort();
            report["min_angle_worst"] = Round(worst);
            report["min_angle_median"] = Round(minAngles[minAngles.Count / 2]);
            report["triangles_min_angle_under_20deg"] = under20;
            report["triangles_min_angle_under_10deg"] = under10;
        }

        private static double AngleDeg(Vector3d u, Vector3d v)
        {
            double denominator = u.Length * v.Length;
            if (denominator < 1e-30)
                return 0.0;
            return Math.Acos(MathUtil.Clamp(u.Dot(v) / denominator, -1.0, 1.0)) * MathUtil.Rad2Deg;
        }

        private static double Round(double value) => Math.Round(value, 4);

        // -------------------------------------------------------------------------- io

        private static DMesh3 ReadG3M(string path)
        {
            using var stream = File.OpenRead(path);
            using var reader = new BinaryReader(stream, Encoding.ASCII);
            string magic = new string(reader.ReadChars(4));
            if (magic != Magic)
                throw new Exception($"{path} is not a G3M1 mesh (magic was '{magic}')");
            int vertexCount = reader.ReadInt32();
            int triangleCount = reader.ReadInt32();

            var mesh = new DMesh3(MeshComponents.None);
            mesh.EnableTriangleGroups(0);
            for (int i = 0; i < vertexCount; ++i)
                mesh.AppendVertex(new Vector3d(
                    reader.ReadDouble(), reader.ReadDouble(), reader.ReadDouble()));

            var triangles = new Index3i[triangleCount];
            for (int i = 0; i < triangleCount; ++i)
                triangles[i] = new Index3i(
                    reader.ReadInt32(), reader.ReadInt32(), reader.ReadInt32());

            int nonManifold = 0, duplicate = 0;
            for (int i = 0; i < triangleCount; ++i)
            {
                int group = reader.ReadInt32();
                int tid = mesh.AppendTriangle(triangles[i], group);
                if (tid == DMesh3.NonManifoldID) nonManifold++;
                else if (tid == DMesh3.InvalidID) duplicate++;
            }
            if (nonManifold > 0 || duplicate > 0)
                Console.Error.WriteLine(
                    $"g3remesh: dropped {nonManifold} non-manifold and {duplicate} invalid triangles");
            return mesh;
        }

        private static void WriteG3M(string path, DMesh3 mesh)
        {
            // Remeshing leaves the index spaces sparse, so compact them on the way out.
            var compact = new DMesh3(mesh, true, MeshComponents.FaceGroups);
            using var stream = File.Create(path);
            using var writer = new BinaryWriter(stream, Encoding.ASCII);
            writer.Write(Magic.ToCharArray());
            writer.Write(compact.VertexCount);
            writer.Write(compact.TriangleCount);
            for (int vid = 0; vid < compact.VertexCount; ++vid)
            {
                Vector3d v = compact.GetVertex(vid);
                writer.Write(v.x); writer.Write(v.y); writer.Write(v.z);
            }
            for (int tid = 0; tid < compact.TriangleCount; ++tid)
            {
                Index3i tv = compact.GetTriangle(tid);
                writer.Write(tv.a); writer.Write(tv.b); writer.Write(tv.c);
            }
            for (int tid = 0; tid < compact.TriangleCount; ++tid)
                writer.Write(compact.GetTriangleGroup(tid));
        }

        private static string ToJson(object value)
        {
            var builder = new StringBuilder();
            AppendJson(builder, value);
            return builder.ToString();
        }

        private static void AppendJson(StringBuilder builder, object value)
        {
            switch (value)
            {
                case null:
                    builder.Append("null");
                    break;
                case bool flag:
                    builder.Append(flag ? "true" : "false");
                    break;
                case string text:
                    builder.Append('"').Append(text.Replace("\"", "\\\"")).Append('"');
                    break;
                case int number:
                    builder.Append(number.ToString(CultureInfo.InvariantCulture));
                    break;
                case double number:
                    builder.Append(number.ToString("R", CultureInfo.InvariantCulture));
                    break;
                case IDictionary<string, object> map:
                    builder.Append('{');
                    bool first = true;
                    foreach (var kv in map)
                    {
                        if (!first) builder.Append(',');
                        first = false;
                        builder.Append('"').Append(kv.Key).Append("\":");
                        AppendJson(builder, kv.Value);
                    }
                    builder.Append('}');
                    break;
                default:
                    AppendJson(builder, value.ToString());
                    break;
            }
        }

        // ------------------------------------------------------------------- arguments

        private sealed class Options
        {
            public string Input;
            public string Output;
            public string Report;
            public double TargetEdgeLength = 1.0;
            public int Iterations = 25;
            // geometry3Sharp's own default. Raising it welds pairs of vertices into
            // zero-area triangles where the surface has thin features: at 0.5 the
            // operator's clipped heart model came back with 92 of them, at 0.2 with one,
            // and the sliver count is no better for the extra speed either.
            public double SmoothSpeed = 0.1;
            public bool Smooth = true;
            public bool ProjectToInput = true;
            public string Schedule = "fastest";
            // Uniform, not the library's MeanValue default. Mean-value and cotangent
            // weights go negative on obtuse triangles, and on the clinical surfaces this
            // was measured against they filled the interior with slivers: about a
            // thousand triangles under 20 degrees on a 28k-triangle heart model, against
            // nineteen -- the input's own count -- with uniform weights.
            public Remesher.SmoothTypes SmoothType = Remesher.SmoothTypes.Uniform;
            public bool PreventNormalFlips = true;
            public double MinEdgeFraction = 0.0;
            public double MaxEdgeFraction = 0.0;
            public int ShapeFlipRounds = 4;
            public double SeamCornerAngleDeg = 0.0;
            public ConstraintMode SeamMode = ConstraintMode.Slide;
            public ConstraintMode BoundaryMode = ConstraintMode.Slide;
            public ConstraintMode FeatureMode = ConstraintMode.Free;
            public double FeatureAngleDeg = 45.0;
        }

        private static Options ParseArgs(string[] args)
        {
            var opts = new Options();
            for (int i = 0; i < args.Length; ++i)
            {
                string key = args[i];
                string Next() => (++i < args.Length) ? args[i] : throw new Exception($"{key} needs a value");
                switch (key)
                {
                    case "--input": opts.Input = Next(); break;
                    case "--output": opts.Output = Next(); break;
                    case "--report": opts.Report = Next(); break;
                    case "--target-edge": opts.TargetEdgeLength = ParseDouble(Next()); break;
                    case "--iterations": opts.Iterations = int.Parse(Next(), CultureInfo.InvariantCulture); break;
                    case "--smooth-speed": opts.SmoothSpeed = ParseDouble(Next()); break;
                    case "--no-smoothing": opts.Smooth = false; break;
                    case "--no-projection": opts.ProjectToInput = false; break;
                    case "--allow-normal-flips": opts.PreventNormalFlips = false; break;
                    case "--schedule": opts.Schedule = Next(); break;
                    case "--seam-corner-angle": opts.SeamCornerAngleDeg = ParseDouble(Next()); break;
                    case "--shape-flip-rounds": opts.ShapeFlipRounds = int.Parse(Next(), CultureInfo.InvariantCulture); break;
                    case "--smooth-type": opts.SmoothType = ParseSmoothType(Next()); break;
                    case "--min-edge-fraction": opts.MinEdgeFraction = ParseDouble(Next()); break;
                    case "--max-edge-fraction": opts.MaxEdgeFraction = ParseDouble(Next()); break;
                    case "--seam-mode": opts.SeamMode = ParseMode(Next()); break;
                    case "--boundary-mode": opts.BoundaryMode = ParseMode(Next()); break;
                    case "--feature-mode": opts.FeatureMode = ParseMode(Next()); break;
                    case "--feature-angle": opts.FeatureAngleDeg = ParseDouble(Next()); break;
                    default: throw new Exception($"unknown argument {key}");
                }
            }
            if (opts.Input == null || opts.Output == null)
                return null;
            return opts;
        }

        private static double ParseDouble(string text) =>
            double.Parse(text, CultureInfo.InvariantCulture);

        private static Remesher.SmoothTypes ParseSmoothType(string text) => text switch
        {
            "uniform" => Remesher.SmoothTypes.Uniform,
            "cotan" => Remesher.SmoothTypes.Cotan,
            "meanvalue" => Remesher.SmoothTypes.MeanValue,
            _ => throw new Exception($"smooth type must be uniform, cotan or meanvalue, not {text}"),
        };

        private static ConstraintMode ParseMode(string text) => text switch
        {
            "free" => ConstraintMode.Free,
            "slide" => ConstraintMode.Slide,
            "pin" => ConstraintMode.Pin,
            _ => throw new Exception($"mode must be free, slide or pin, not {text}"),
        };

        private static void Usage()
        {
            Console.Error.WriteLine(
                "usage: g3remesh --input in.g3m --output out.g3m [--target-edge 0.85]\n" +
                "                [--iterations 25] [--smooth-speed 0.1] [--no-smoothing]\n" +
                "                [--no-projection] [--schedule fastest|basic|sharp]\n" +
                "                [--smooth-type uniform|cotan|meanvalue] [--report stats.json]\n" +
                "                [--seam-mode free|slide|pin] [--boundary-mode free|slide|pin]\n" +
                "                [--feature-mode free|slide|pin] [--feature-angle 45]\n" +
                "                [--seam-corner-angle 20] [--shape-flip-rounds 4]");
        }
    }
}
