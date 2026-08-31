# Face Aware Remesh

> **Archived:** This is the original geometry3Sharp/.NET implementation retained
> for reference. It is not registered or built by SlicerSimVascular. The active
> implementation is a pure-Python port that needs no helper binary and no .NET
> runtime — see [`FaceAwareRemesh/`](../../FaceAwareRemesh/) and
> [`Docs/FaceAwareRemesh.md`](../../Docs/FaceAwareRemesh.md).

Remesh a surface model to a target edge length while keeping its `ModelFaceID`
face labels, and choose how the seams between labelled faces are held.

This is the module to reach for after [Paint Model](../../Docs/PaintModel.md) has partitioned
a model into face groups: `ModelFaceID` is the array both modules work in terms of,
and it is the labelling SimVascular's meshing and boundary-condition setup depend
on. A remesher that does not understand it forces you to relabel afterwards by
proximity lookup, which smears labels across face boundaries exactly where
precision matters.

## How it works

Remeshing is done by [geometry3Sharp](https://github.com/gradientspace/geometry3Sharp)
(gradientspace, Boost licence), whose `MeshConstraints` carries a per-edge and
per-vertex constraint, each with an optional projection target. Labels survive
because `ModelFaceID` maps one to one onto that library's `DMesh3` triangle
groups: a split inherits its parent triangle's group, and a collapse cannot merge
two groups without crossing a constrained edge. Nothing is relabelled afterwards.

The library is C#, so it is built as a small console helper and run as a
subprocess. See [Building the helper](#building-the-helper).

## Seam modes

A seam is the boundary between two labelled faces.

| Mode | What it does |
|---|---|
| **Slide** | Resamples the seam at the target edge length, but keeps every seam vertex on the *original seam curve*. This constrains the seam's geometry rather than its discretization. |
| **Pin** | Holds the seam vertex for vertex: no split, collapse or flip. Use it when the old discretization has to come back unchanged, for example when the surface will be welded to another mesh whose vertices must coincide. |
| **Free** | No seam constraint. Labels smear. Present for comparison, not for use. |

Sliding is the interesting one, and the reason is that a pinned seam is where
sliver triangles come from: its vertex placement is inherited from whatever cut
produced it and cannot be improved.

### Seam corner angle

Projecting seam vertices onto the original curve guarantees they sit *on* it, but
nothing stops a collapse from chording across a bend, and that chord error is
bounded by nothing but the local geometry. The seam corner angle pins the vertices
where the seam turns by more than that angle, so the stretches that slide are the
ones where sliding costs little. Lower it to hold the seam's shape more tightly;
raise it to let more of the seam re-space.

On a clinical heart model with six labelled faces, sweeping it traded seam
fidelity against sliver count as follows (0.85 mm target, 28k triangles in):

| Corner angle | Seam segments | Seam deviation, max | Triangles under 20° |
|---|---|---|---|
| 0° (pin none) | 442 | 1.02 mm | 19 |
| 20° | 495 | 0.09 mm | 29 |
| 60° | 468 | 0.25 mm | 24 |
| *pin mode* | 551 | 0 mm | 36 |

## Settings that matter

**Smoothing speed is the one to be careful with.** It defaults to 0.1, which is
geometry3Sharp's own default. Raising it welds pairs of vertices into zero-area
triangles wherever the surface has thin features, and it does not buy any
reduction in slivers. On the 124k-triangle clipped heart model:

| Smoothing speed | Zero-area triangles | Triangles under 20° |
|---|---|---|
| 0.00 | 0 | 2946 |
| 0.05 | 0 | 874 |
| **0.10** | **0** | **360** |
| 0.20 | 1 | 296 |
| 0.35 | 76 | 344 |
| 0.50 | 92 | 404 |

Note that free-edge and non-manifold-edge counts stay at zero across that whole
range: two vertices at the same position are perfectly well-formed
topologically. Degeneracy of this kind has to be checked geometrically, which is
why the module's report includes the output edge-length range.

**Project onto input** keeps remeshed vertices on the surface you started from.
Leave it on; turning it off lets smoothing shrink the model.

## Measured behaviour

Three real surfaces from a CHiPS baffle case, 0.85 mm target, smoothing speed 0.1.
No label was lost or invented in any run, and every output was closed with zero
free and zero non-manifold edges.

| Surface | Mode | Triangles | Under 20° | Seam deviation, max | Surface shift, max | Zero-area |
|---|---|---|---|---|---|---|
| Clipped heart model, 124k in, 3 labels | pin | 80730 | 372 | 0 mm | 0.41 mm | 0 |
| | slide | 80328 | 339 | 0.41 mm | 0.34 mm | 0 |
| | slide + corner 20° | 80584 | 395 | 0.06 mm | 0.04 mm | 0 |
| Capped domain, 28k in, 6 labels | pin | 22586 | 66 | 0 mm | 0.03 mm | 0 |
| | slide | 22388 | 29 | 0.61 mm | 0.02 mm | 0 |
| | slide + corner 20° | 22464 | 55 | 0.08 mm | 0.01 mm | 0 |
| Baffle merge, 15k in, 4 labels | pin | 9060 | 947 | 0 mm | 0.04 mm | 0 |
| | slide | 8072 | **78** | 0.34 mm | 0.03 mm | 0 |
| | slide + corner 20° | 8278 | 229 | 0.09 mm | 0.01 mm | 10 |

The third surface is the one that makes the case for sliding. Its input carries
859 triangles under 20°, produced by the seam where a baffle patch was stitched to
a ventricular wall. Pinning that seam keeps them all — 947 out, no better than
going in. Letting it slide takes it to 78. The seam is the problem, and holding it
fixed preserves the problem.

Note the last row: at a tight corner angle that surface came back with ten
zero-area triangles, where pure sliding produced none. Tighter seam fidelity is
not free, and if the output is headed for a volume mesher the edge-length range in
the report is worth reading before trusting it.

## Building the helper

Needs the .NET 8 SDK to build. From the extension directory:

    Helper/build.sh --self-contained

That fetches geometry3Sharp at a pinned commit and publishes a standalone binary
into `Helper/bin` that carries its own runtime. The module finds it automatically
and shows the resolved command under **Advanced**.

**Use `--self-contained` unless you have a reason not to.** Slicer is not launched
from a shell, so it does not inherit your `PATH` or `DOTNET_ROOT`: a plain
`Helper/build.sh` produces a framework-dependent `.dll` that runs fine from a
terminal and then fails inside Slicer with "no dotnet runtime to run it". The
module prefers the self-contained binary over the `.dll` for exactly this reason,
and looks for `dotnet` in the usual install locations as well as on `PATH` before
giving up.

The binary is a subprocess, not a loaded library, so its architecture has to match
the host rather than Slicer's — an arm64 helper is correct even when Slicer itself
is the x86_64 build running under Rosetta.

`Helper/bin` is deliberately not committed, so a fresh clone has to run the build
once. To point the module at a build somewhere else, set
`SLICERSIMVASCULAR_G3REMESH` to the executable or `.dll`.

## Known limitations

- **Sliver triangles at the seam are reduced, not eliminated.** geometry3Sharp's
  edge-flip test is purely a valence-balance test with no shape term, so a
  triangle whose three vertices have gone collinear is only flipped away if that
  also happens to even out the valences. When such a triangle sits on a seam, its
  seam edges cannot flip without moving the seam, and its short interior edge
  cannot collapse either, because collapsing an edge whose two ends lie on the
  same seam curve would pinch the seam. A few of these survive every run.
- **`Vector3d.AngleD` in geometry3Sharp is wrong for non-unit vectors** — it feeds
  an unnormalized dot product straight to `Acos`. The helper computes its own
  angles rather than using it. Worth knowing if you extend the helper.
- **The seam deviation tolerance is approximate.** The corner angle is a proxy for
  a deviation bound, not a bound. `MeshConstraints` has no hook to reject a
  collapse whose chord error exceeds a tolerance, so enforcing a true tolerance
  would mean patching the vendored `Remesher`'s collapse test.
