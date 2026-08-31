# Face Aware Remesh

Remesh a surface model to a target edge length while keeping its `ModelFaceID` face labels, and
choose how the seams between labelled faces are held.

This is the module to reach for after [Paint Model](PaintModel.md) has partitioned a model into
face groups: `ModelFaceID` is the array both modules work in terms of, and it is the labelling
SimVascular's meshing and boundary-condition setup depend on. A remesher that does not
understand it forces you to relabel afterwards by proximity lookup, which smears labels across
face boundaries exactly where precision matters.

## How it works

Remeshing is a Python port of [geometry3Sharp](https://github.com/gradientspace/geometry3Sharp)'s
constrained remesher (gradientspace, Boost licence) onto a dynamic mesh of the package's own. The
algorithm is the incremental one: a pass over every edge that splits it if it is longer than 4/3
of the target, collapses it if it is shorter than 4/5, and otherwise flips it when that brings
the vertex valences closer to six, followed by a Laplacian smoothing pass with every vertex
reprojected onto the surface it started from.

Labels survive because `ModelFaceID` *is* the mesh's triangle grouping rather than something
looked up afterwards: a split inherits its parent triangle's group, and a collapse cannot merge
two groups without crossing a constrained edge. Nothing is relabelled after the fact.

There is no helper binary and no .NET runtime. The library imports numpy and VTK and nothing
else, both of which Slicer ships — see [Using it outside Slicer](#using-it-outside-slicer).

## Seam modes

A seam is the boundary between two labelled faces.

| Mode | What it does |
|---|---|
| **Slide** | Resamples the seam at the target edge length, but keeps every seam vertex on the *original seam curve*. This constrains the seam's geometry rather than its discretization. |
| **Pin** | Holds the seam vertex for vertex: no split, collapse or flip. Use it when the old discretization has to come back unchanged, for example when the surface will be welded to another mesh whose vertices must coincide. |

Sliding is the default, and the reason is that a pinned seam is where sliver triangles come
from: its vertex placement is inherited from whatever cut produced it and cannot be improved. On
a clinical heart case's merged flow domain at a 0.85 mm target, sliding takes the seam band's
worst aspect ratio from 935 to 8.6 and its 99th percentile from 116 to 2.0, with every face
still present.

Pinning a fine seam next to a coarsened interior is the opposite trap, and it is worse: on a
lone patch it took the seam band's worst aspect ratio from 50.6 to 21728. Pin only when a
caller downstream genuinely needs the vertex list back unchanged.

### Seam corner angle

Projecting seam vertices onto the original curve guarantees they sit *on* it, but nothing stops
a collapse from chording across a bend. The seam corner angle pins the vertices where the seam
turns by more than that angle, so the stretches that slide are the ones where sliding costs
little. Lower it to hold the seam's shape more tightly; raise it to let more of the seam
re-space.

## Settings that matter

**Smoothing speed** defaults to 0.1, which is geometry3Sharp's own default. Raising it welds
pairs of vertices into zero-area triangles wherever the surface has thin features, and it does
not buy any reduction in slivers. Unlike the original, every position change here is gated on
the `1e-12` cross-norm a volume mesher refuses a surface at, so a smoothing pass cannot take a
triangle to exactly zero area — but the gate is a floor, not a licence to raise the speed.

**Passes** repeats the complete remesh rather than adding sweeps to one. A second pass rebuilds
the seam constraints and the projection target on the first result, which is what finishes a
face that a single pass leaves uneven.

**Queued** uses the ported `RemesherPro` sweep, which enqueues the neighbourhood of whatever an
operation touched instead of walking every edge each iteration. It holds up when the input sits
within roughly 1.0–2.0 times its own median edge of the target and degrades outside that band.

## What it does not do

- **It is not a repair.** A surface that crosses itself goes in and comes out crossing itself.
- **It is not a decimator you can point anywhere.** It drives toward a uniform edge length, so a
  surface whose features are finer than the target loses them.
- **It refuses rather than degrading.** A pass that would leave a degenerate triangle, tear an
  open boundary, or remesh a face away is reported in the log and the input model is left
  untouched. The panel copies the input before it starts, which is what makes that guarantee
  real.
- **Sliver triangles at the seam are reduced, not eliminated.** A triangle whose seam edges
  cannot flip without moving the seam, and whose interior edge cannot collapse without pinching
  it, survives. A few do every run.

## Using it outside Slicer

The remesher is `svremesh`, a package beside the module that imports nothing from Slicer:

```
pip install -e FaceAwareRemesh/
```

```python
import svremesh

surface, record = svremesh.remesh_preserving_faces(polydata, 0.85)
print(record["faces"])            # cells per ModelFaceID after the pass
```

`remesh_preserving_faces` is the entry point every frontend should call — going under it to
`remesh_labelled_surface` skips the reporting and the active-scalar assignment, which is how two
hosts start producing different surfaces from one input.

Inside Slicer no install is needed: the module's own directory is already on the Python path.

Its tests run headlessly and need only numpy and VTK:

```
cd FaceAwareRemesh && PYTHONPATH=. python -m unittest discover -s tests
```
