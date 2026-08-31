# Membrane Designer Agent Guide

> **Archived:** These instructions applied to the module while it was active.
> They are kept for whoever picks the prototype back up; see the archived note in
> [`README.md`](README.md) first for why it was shelved.

These instructions apply to all work under `Archive/MembraneDesigner/`.

## Purpose and maturity

Membrane Designer is a research prototype for mapping an editable flat 2D membrane pattern to a pressurized 3D membrane sewn to a Slicer closed curve.

The module currently provides a working Slicer UI, persistent parameter node,
eight-control-point 2D periodic-spline editor, best-fit-plane seam projection,
equal-total-perimeter enforcement, constrained unstructured meshing,
arclength-based seam-correspondence coloring, a CPU NumPy XPBD preview, and
Green-Lagrange strain visualization. It does **not** yet implement Taichi,
triangle-strain DiffXPBD, analytical gradients, calibrated mechanics, verified
static equilibrium, or inverse optimization.

Do not describe the current edge-constraint preview as a completed DiffXPBD solver or as clinically validated biomechanics.

Read `README.md` before changing solver architecture or scope.

## Important files

- `MembraneDesigner.py`: module, parameter node, geometry utilities, preview solver, canvas, Slicer widget/logic, and core test.
- `Resources/UI/MembraneDesigner.ui`: module control panel. User-facing controls belong here unless they are part of the interactive graphics canvas.
- `CMakeLists.txt`: scripted-module build and resource registration.
- `README.md`: implementation status, limitations, and development roadmap.

The parent extension registers the module in `../CMakeLists.txt`.

## Required Slicer patterns

- Preserve the Qt Designer `.ui` workflow. Do not replace the control panel with programmatically constructed forms.
- Persist user inputs through `MembraneDesignerParameterNode` and `SlicerParameterName` properties in the `.ui` file.
- When adding a parameter:
  1. Add its typed field and default to the parameter-node wrapper.
  2. Add or update the `.ui` control with the matching `SlicerParameterName`.
  3. Add migration logic if an existing serialized value changes type or meaning.
  4. Test scene save/reload behavior.
- Use `Optional[...]` for nullable MRML node references.
- A `QComboBox` connected to a string parameter requires `Annotated[str, Choice([...])]` in Slicer 5.10.
- Explicitly call `setMRMLScene(slicer.mrmlScene)` on MRML selectors if changing UI initialization.
- Only mutate MRML and VTK objects on Slicer's main thread.
- Disconnect parameter-node GUI bindings and remove observers during cleanup, scene close, and module exit.
- Keep the 2D pattern dock owned by the widget and close it during cleanup.

## Geometry and correspondence invariants

- Maintain separate 2D rest coordinates and 3D simulated coordinates.
- Boundary vertex ordering is persistent and maps to the 3D seam by normalized arclength.
- `BoundaryArclength` uses `s ∈ [0,1)` and must survive simulation and export.
- The same arclength color convention must be used in the 2D and 3D views.
- Preserve an explicit seam-start marker because a cyclic color map is ambiguous at its wrap point.
- Boundary particles are hard-constrained to their sampled seam targets.
- The discretized 2D boundary and 3D seam must have equal total perimeter.
  Enforce this after seam projection, after every 2D edit, and before solving.
  Both sides use 64 uniform-arclength samples for this invariant.
- Do not equate equal total perimeter with exact local no-stretch sewing. The
  current editor does not constrain every corresponding boundary-edge length.
- The rest mesh is a constrained unstructured triangulation. Boundary vertices
  occupy the first contiguous point-ID block in normalized-arclength order;
  preserve this invariant when changing the mesher.
- Do not silently reorder, reverse, or phase-shift a boundary. Any future landmark-based phase/orientation operation must be explicit and persisted.
- Reject or clearly report invalid rest triangles, inverted current triangles, and self-intersecting boundaries before reporting strain or gradients.

## Solver guidance

The current `XPBDSolver` is a functional fallback, not the intended final
solver. Pressure (`kPa` to `N/mm²`) and lumped mass (1000 kg/m³ in mm–N–s
units) are dimensionally consistent. The edge compliance law is provisional.

Physical lumped masses give inverse masses around `10^9`. In the current XPBD
denominator these overwhelm the modulus-dependent compliance term, so changing
Young's modulus has little practical effect. Pressure also creates large
accelerations, and the fixed-duration damped solve may stop in a transient
state. Do not tune constants merely to make a plausible bulge. Replace the
constitutive formulation and add equilibrium diagnostics. A forward-solver
change is not validated until pressure, modulus, and thickness sensitivity are
physically ordered and stable under timestep, iteration, and mesh refinement.

The next solver milestone is:

1. Taichi fields with Metal/CUDA and CPU fallback.
2. Per-triangle Green-strain constraints for stretch and shear.
3. Unit-consistent plane-stress material parameters.
4. Weak hinge bending regularization.
5. Consistently oriented pressure forces.
6. Inversion detection and convergence-based stopping.
7. Synthetic refinement and gradient validation.

Implement and validate the forward triangle-strain solver before adding inverse optimization.

When introducing Taichi:

- Keep a small deterministic CPU path for tests.
- Isolate solver code from Slicer UI and MRML code.
- Do not import or initialize Taichi at module-import time if that would prevent the module UI from loading when Taichi is unavailable.
- Provide a clear dependency check and installation action rather than installing packages automatically on module entry.
- Preserve NumPy/VTK buffer boundaries so worker code never holds mutable MRML objects.
- Avoid unrolling long simulations into a full autodiff tape. The intended differentiable path is the DiffXPBD analytical/adjoint formulation.

## Strain rules

- Compute strain from the 2D rest metric and current 3D triangle geometry.
- Rigid transformations must produce zero strain.
- Store triangle quantities as VTK cell-data arrays.
- In Slicer 5.10, activate cell scalars using:

```python
displayNode.SetActiveScalar(arrayName, vtk.vtkAssignAttribute.CELL_DATA)
```

- Do not use the unavailable `SetScalarModeToUseCellData()` method.
- Do not report strain for inverted or degenerate triangles.
- Report averages using rest-area weighting so irregular mesh density does not
  bias the statistic. The current status reports rest-area-weighted average and
  maximum principal Green-Lagrange strain.
- Label current strain as an engineering diagnostic until material and solver validation are complete.

## Validation commands

Run these from the `SlicerSimVascular` repository root after every meaningful change:

```bash
python3 -m py_compile Archive/MembraneDesigner/Module/MembraneDesigner.py
xmllint --noout Archive/MembraneDesigner/Module/Resources/UI/MembraneDesigner.ui
git diff --check
```

Run the Slicer-native core test with the relevant local Slicer application path:

```bash
"/Applications/Slicer 2.app/Contents/MacOS/Slicer" \
  --no-main-window \
  --disable-cli-modules \
  --python-code "import sys; sys.path.insert(0, '/absolute/path/to/SlicerSimVascular/Archive/MembraneDesigner/Module'); import MembraneDesigner; MembraneDesigner.MembraneDesignerTest().runTest(); print('MEMBRANE_TEST_OK'); slicer.app.exit(0)"
```

Unrelated startup warnings from other installed Slicer extensions may appear in headless output. The membrane test must still reach `MEMBRANE_TEST_OK` with a successful process exit.

For UI-affecting changes:

1. Load the source extension through **Extension Wizard → Select Extension**.
2. Open **Membrane Designer**.
3. Use **Reload** after source edits.
4. Run **Create demo seam**.
5. Confirm the 2D dock appears, controls are draggable, the 3D model updates, strain coloring is active, and the seam correspondence is visible.
6. Save and reload a scene to verify parameter persistence when relevant.

## Test expectations

At minimum, preserve tests for:

- Periodic spline closure and sampling.
- Valid unstructured connectivity, preserved boundary edges, and finite coordinates.
- Equality of discretized 2D and 3D total perimeter after initialization and editing.
- Exact hard-boundary enforcement.
- Rigid-motion zero strain.
- Analytical affine stretch and shear strain.
- No invalid or inverted triangles in the planar demo.
- Parameter-node round trips, including node references and spline JSON.
- CPU/GPU agreement once Taichi is added.
- Adjoint gradients against central finite differences once DiffXPBD is added.

Add focused regression tests for every runtime error found in Slicer.

## Development safety

- Preserve unrelated changes in the parent repository.
- Do not edit installed Slicer application files to develop this module. Load the source directory through Extension Wizard or use an additional module path.
- Do not commit generated caches such as `__pycache__`.
- Keep the built-in demo idempotent; repeated use must not accumulate duplicate demo nodes.
- Prefer explicit errors over displaying a plausible-looking but invalid solution.
- Do not make clinical-performance or safety claims.

## Near-term priorities

Work in this order unless the user explicitly changes priorities:

1. Expand deterministic geometry, strain, inversion, and parameter-persistence tests.
2. Split the monolithic Python file into geometry, solver, and visualization components without changing behavior.
3. Implement and validate a statically converged, material-sensitive
   triangle-strain forward solver, then port it to Taichi.
4. Add background cancellation and streamed main-thread previews.
5. Add seam landmarks, mesh-quality/refinement controls, and SVG import/export.
6. Implement DiffXPBD adjoints and finite-difference gradient checks.
7. Add inverse spline optimization only after the forward and gradient validations pass.
