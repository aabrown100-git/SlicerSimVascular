import json
import os
import struct
import subprocess
import tempfile

import numpy as np
import vtk, qt, ctk, slicer
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin

LABEL_ARRAY = "ModelFaceID"

#
# FaceAwareRemesh
#

class FaceAwareRemesh(ScriptedLoadableModule):
  """Uses ScriptedLoadableModule base class, available at:
  https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
  """

  def __init__(self, parent):
    ScriptedLoadableModule.__init__(self, parent)
    self.parent.title = "Face Aware Remesh"
    self.parent.categories = ["Surface Models"]
    self.parent.dependencies = []
    self.parent.contributors = ["Aaron Brown (Stanford)"]
    self.parent.helpText = """
Remesh a surface model to a target edge length while keeping its "ModelFaceID"
face labels, so a labelled model stays usable by SimVascular after remeshing.

Face seams -- the boundaries between labelled faces -- can be held in three ways.
<b>Slide</b> resamples the seam at the target edge length but keeps every seam
vertex on the original seam curve, which is what removes the sliver triangles a
pinned seam leaves behind. <b>Pin</b> holds the seam vertex for vertex, for the
cases that need the old discretization back unchanged. <b>Free</b> leaves the
seam unconstrained, which lets labels smear, and is there for comparison.

Because sliding lets a collapse chord across a bend in the seam, the seam corner
angle pins the vertices where the seam turns by more than that angle. Lower it to
hold the seam's shape more tightly, raise it to let more of the seam re-space.
"""
    self.parent.acknowledgementText = """
Remeshing is performed by <a href="https://github.com/gradientspace/geometry3Sharp">
geometry3Sharp</a> (Ryan Schmidt, gradientspace, Boost licence), whose
MeshConstraints model provides the per-edge and per-vertex constraints this module
builds face seams out of. Face groups in that library map one to one onto
SimVascular's ModelFaceID.
"""

#
# FaceAwareRemeshWidget
#

class FaceAwareRemeshWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
  """Uses ScriptedLoadableModuleWidget base class, available at:
  https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
  """

  def __init__(self, parent=None):
    ScriptedLoadableModuleWidget.__init__(self, parent)
    VTKObservationMixin.__init__(self)
    self.logic = None

  def setup(self):
    """
    Called when the user opens the module the first time and the widget is initialized.
    """
    ScriptedLoadableModuleWidget.setup(self)
    self.logic = FaceAwareRemeshLogic()

    # The UI is built in Python rather than from a .ui file, to match PaintModel,
    # the module this one is normally used after.
    self.setupInputsUI()
    self.setupSeamUI()
    self.setupAdvancedUI()
    self.setupRunUI()
    self.layout.addStretch(1)
    self.updateHelperStatus()
    self.updateApplyEnabled()

  def setupInputsUI(self):
    collapsible = ctk.ctkCollapsibleButton()
    collapsible.text = "Inputs"
    self.layout.addWidget(collapsible)
    form = qt.QFormLayout(collapsible)

    self.inputSelector = slicer.qMRMLNodeComboBox()
    self.inputSelector.nodeTypes = ["vtkMRMLModelNode"]
    self.inputSelector.addEnabled = False
    self.inputSelector.removeEnabled = False
    self.inputSelector.noneEnabled = True
    self.inputSelector.setMRMLScene(slicer.mrmlScene)
    self.inputSelector.toolTip = f"Surface model carrying a {LABEL_ARRAY} cell array"
    form.addRow("Input model: ", self.inputSelector)

    self.outputSelector = slicer.qMRMLNodeComboBox()
    self.outputSelector.nodeTypes = ["vtkMRMLModelNode"]
    self.outputSelector.addEnabled = True
    self.outputSelector.renameEnabled = True
    self.outputSelector.removeEnabled = True
    self.outputSelector.noneEnabled = True
    self.outputSelector.baseName = "Remeshed"
    self.outputSelector.setMRMLScene(slicer.mrmlScene)
    self.outputSelector.toolTip = "Model to write the remeshed surface into"
    form.addRow("Output model: ", self.outputSelector)

    self.targetEdgeSpinBox = qt.QDoubleSpinBox()
    self.targetEdgeSpinBox.setRange(0.01, 100.0)
    self.targetEdgeSpinBox.setDecimals(3)
    self.targetEdgeSpinBox.setSingleStep(0.05)
    self.targetEdgeSpinBox.setValue(0.85)
    self.targetEdgeSpinBox.setSuffix(" mm")
    self.targetEdgeSpinBox.toolTip = "Edge length the remesher aims for"
    form.addRow("Target edge length: ", self.targetEdgeSpinBox)

    self.inputSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.onInputChanged)
    self.outputSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.updateApplyEnabled)

    self.inputInfoLabel = qt.QLabel("")
    self.inputInfoLabel.setWordWrap(True)
    form.addRow("", self.inputInfoLabel)

  def setupSeamUI(self):
    collapsible = ctk.ctkCollapsibleButton()
    collapsible.text = "Face seams"
    self.layout.addWidget(collapsible)
    form = qt.QFormLayout(collapsible)

    self.seamModeComboBox = qt.QComboBox()
    for label, value in [("Slide along the original seam curve", "slide"),
                         ("Pin vertex for vertex", "pin"),
                         ("Free (labels may smear)", "free")]:
      self.seamModeComboBox.addItem(label, value)
    self.seamModeComboBox.toolTip = "How the boundaries between face labels are held"
    form.addRow("Seam mode: ", self.seamModeComboBox)

    self.seamCornerSpinBox = qt.QDoubleSpinBox()
    self.seamCornerSpinBox.setRange(0.0, 180.0)
    self.seamCornerSpinBox.setDecimals(1)
    self.seamCornerSpinBox.setValue(20.0)
    self.seamCornerSpinBox.setSuffix(" deg")
    self.seamCornerSpinBox.toolTip = (
      "Pin seam vertices where the seam turns by more than this angle. "
      "0 pins none, which lets the seam chord across its bends.")
    form.addRow("Seam corner angle: ", self.seamCornerSpinBox)

    self.boundaryModeComboBox = qt.QComboBox()
    for label, value in [("Slide along the original boundary", "slide"),
                         ("Pin vertex for vertex", "pin"),
                         ("Free", "free")]:
      self.boundaryModeComboBox.addItem(label, value)
    self.boundaryModeComboBox.toolTip = "How the model's own open boundaries are held"
    form.addRow("Open boundary mode: ", self.boundaryModeComboBox)

  def setupAdvancedUI(self):
    collapsible = ctk.ctkCollapsibleButton()
    collapsible.text = "Advanced"
    collapsible.collapsed = True
    self.layout.addWidget(collapsible)
    form = qt.QFormLayout(collapsible)

    self.featureModeComboBox = qt.QComboBox()
    for label, value in [("Ignore sharp edges", "free"),
                         ("Slide along sharp edges", "slide"),
                         ("Pin sharp edges", "pin")]:
      self.featureModeComboBox.addItem(label, value)
    form.addRow("Sharp edges: ", self.featureModeComboBox)

    self.featureAngleSpinBox = qt.QDoubleSpinBox()
    self.featureAngleSpinBox.setRange(0.0, 180.0)
    self.featureAngleSpinBox.setValue(45.0)
    self.featureAngleSpinBox.setSuffix(" deg")
    form.addRow("Sharp edge angle: ", self.featureAngleSpinBox)

    self.iterationsSpinBox = qt.QSpinBox()
    self.iterationsSpinBox.setRange(1, 500)
    self.iterationsSpinBox.setValue(25)
    form.addRow("Iterations: ", self.iterationsSpinBox)

    self.smoothSpeedSpinBox = qt.QDoubleSpinBox()
    self.smoothSpeedSpinBox.setRange(0.0, 1.0)
    self.smoothSpeedSpinBox.setDecimals(2)
    self.smoothSpeedSpinBox.setSingleStep(0.05)
    self.smoothSpeedSpinBox.setValue(0.1)
    self.smoothSpeedSpinBox.toolTip = (
      "How hard vertices are smoothed towards their neighbours. This is the single "
      "most consequential setting: raising it above about 0.2 welds pairs of "
      "vertices into zero-area triangles where the surface has thin features, and "
      "does not reduce sliver triangles either.")
    form.addRow("Smoothing speed: ", self.smoothSpeedSpinBox)

    self.projectCheckBox = qt.QCheckBox()
    self.projectCheckBox.setChecked(True)
    self.projectCheckBox.toolTip = (
      "Keep remeshed vertices on the input surface. Turning this off lets "
      "smoothing shrink the model.")
    form.addRow("Project onto input: ", self.projectCheckBox)

    self.helperStatusLabel = qt.QLabel("")
    self.helperStatusLabel.setWordWrap(True)
    self.helperStatusLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    form.addRow("Remesher: ", self.helperStatusLabel)

  def setupRunUI(self):
    self.applyButton = qt.QPushButton("Remesh")
    self.applyButton.toolTip = "Remesh the input model, keeping its face labels"
    self.applyButton.enabled = False
    self.layout.addWidget(self.applyButton)
    self.applyButton.connect("clicked()", self.onApply)

    self.reportTextEdit = qt.QTextEdit()
    self.reportTextEdit.setReadOnly(True)
    self.reportTextEdit.setMinimumHeight(180)
    self.reportTextEdit.setLineWrapMode(qt.QTextEdit.NoWrap)
    self.reportTextEdit.setFontFamily("Courier")
    self.layout.addWidget(self.reportTextEdit)

  def updateHelperStatus(self):
    try:
      command = self.logic.resolveHelperCommand()
      self.helperStatusLabel.text = " ".join(command)
      self.helperStatusLabel.setStyleSheet("")
    except FaceAwareRemeshError as error:
      self.helperStatusLabel.text = str(error)
      self.helperStatusLabel.setStyleSheet("color: #b00;")

  def onInputChanged(self, node=None):
    node = self.inputSelector.currentNode()
    if node is None or node.GetPolyData() is None:
      self.inputInfoLabel.text = ""
    else:
      polydata = node.GetPolyData()
      array = polydata.GetCellData().GetArray(LABEL_ARRAY)
      if array is None:
        self.inputInfoLabel.text = (
          f"<span style='color:#b00'>No {LABEL_ARRAY} cell array. Create face "
          f"groups with the Paint Model module first.</span>")
      else:
        labels = np.unique(vtk_to_numpy(array).astype(int))
        self.inputInfoLabel.text = (
          f"{polydata.GetNumberOfCells()} triangles, "
          f"{len(labels)} face labels: {', '.join(str(int(v)) for v in labels)}")
    self.updateApplyEnabled()

  def updateApplyEnabled(self, node=None):
    inputNode = self.inputSelector.currentNode()
    hasLabels = (
      inputNode is not None
      and inputNode.GetPolyData() is not None
      and inputNode.GetPolyData().GetCellData().GetArray(LABEL_ARRAY) is not None)
    self.applyButton.enabled = hasLabels and self.outputSelector.currentNode() is not None

  def onApply(self):
    inputNode = self.inputSelector.currentNode()
    outputNode = self.outputSelector.currentNode()
    with slicer.util.tryWithErrorDisplay("Failed to remesh.", waitCursor=True):
      report = self.logic.remeshModelNode(
        inputNode,
        outputNode,
        targetEdgeLength=self.targetEdgeSpinBox.value,
        seamMode=self.seamModeComboBox.currentData,
        seamCornerAngle=self.seamCornerSpinBox.value,
        boundaryMode=self.boundaryModeComboBox.currentData,
        featureMode=self.featureModeComboBox.currentData,
        featureAngle=self.featureAngleSpinBox.value,
        iterations=self.iterationsSpinBox.value,
        smoothSpeed=self.smoothSpeedSpinBox.value,
        projectToInput=self.projectCheckBox.checked,
      )
    self.reportTextEdit.setPlainText(formatReport(report))

#
# FaceAwareRemeshError
#

class FaceAwareRemeshError(RuntimeError):
  """Raised when the remesher cannot be found or refuses the input."""

#
# FaceAwareRemeshLogic
#

class FaceAwareRemeshLogic(ScriptedLoadableModuleLogic):
  """Remeshes a labelled surface by handing it to the geometry3Sharp helper.

  The helper is a separate .NET process rather than an in-process library, for the
  same reason the SimVascular stages in the baffle pipeline are: it keeps a
  foreign runtime out of Slicer's own, and the only thing crossing the boundary is
  a mesh file. `G3M1` is that file -- vertices, triangles, and one integer label
  per triangle, and nothing else, because the labels are the whole point of the
  round trip and formats that carry groups as names (OBJ) lose the integers that
  downstream SimVascular code thresholds on.
  """

  MAGIC = b"G3M1"

  def __init__(self):
    ScriptedLoadableModuleLogic.__init__(self)

  #
  # locating the helper
  #

  def extensionDirectory(self):
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

  def resolveHelperCommand(self):
    """Return the command that runs the remesher, or explain why there isn't one.

    The self-contained binary is preferred over the framework-dependent build even
    when both exist, because it carries its own runtime. Slicer is not launched
    from a shell, so it does not inherit a developer's PATH or DOTNET_ROOT: a
    `dotnet` that works in a terminal is routinely absent here, and preferring the
    .dll would fail in the GUI while passing every test run from a shell.
    """
    override = os.environ.get("SLICERSIMVASCULAR_G3REMESH")
    if override:
      if not os.path.exists(override):
        raise FaceAwareRemeshError(
          f"SLICERSIMVASCULAR_G3REMESH points at {override}, which does not exist.")
      return self._commandFor(override)

    root = self.extensionDirectory()
    candidates = [
      os.path.join(root, "Helper", "bin", "g3remesh"),
      os.path.join(root, "Helper", "bin", "g3remesh.exe"),
      os.path.join(root, "Helper", "g3remesh", "bin", "Release", "net8.0", "g3remesh.dll"),
    ]
    for candidate in candidates:
      if os.path.exists(candidate):
        return self._commandFor(candidate)

    onPath = shutilWhich("g3remesh")
    if onPath:
      return [onPath]

    raise FaceAwareRemeshError(
      "The g3remesh helper is not built. Run\n"
      f"    {os.path.join(root, 'Helper', 'build.sh')} --self-contained\n"
      "which needs the .NET 8 SDK to build but leaves a binary that needs no "
      "runtime. Or set SLICERSIMVASCULAR_G3REMESH to an existing build.")

  def _commandFor(self, path):
    if not path.endswith(".dll"):
      return [path]
    dotnet = self._findDotnet()
    if dotnet is None:
      raise FaceAwareRemeshError(
        f"Found {path}, but it is a framework-dependent build and there is no "
        f"dotnet runtime to run it. Slicer does not inherit your shell's PATH, so "
        f"a dotnet that works in a terminal is often not visible here. Run\n"
        f"    {os.path.join(self.extensionDirectory(), 'Helper', 'build.sh')} --self-contained\n"
        f"to produce a binary that needs no runtime.")
    return [dotnet, path]

  def _findDotnet(self):
    """Locate a dotnet runtime, including the places Slicer's PATH will not reach."""
    onPath = shutilWhich("dotnet")
    if onPath:
      return onPath
    dotnetRoot = os.environ.get("DOTNET_ROOT")
    searched = [os.path.join(dotnetRoot, "dotnet")] if dotnetRoot else []
    searched += [
      "/usr/local/share/dotnet/dotnet",
      "/opt/homebrew/bin/dotnet",
      "/usr/lib/dotnet/dotnet",
      os.path.expanduser("~/.dotnet/dotnet"),
    ]
    for candidate in searched:
      if os.path.exists(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None

  #
  # the G3M1 exchange format
  #

  def writeG3M(self, polydata, path, labelArrayName=LABEL_ARRAY):
    points, triangles, labels = triangleArrays(polydata, labelArrayName)
    with open(path, "wb") as handle:
      handle.write(self.MAGIC)
      handle.write(struct.pack("<ii", points.shape[0], triangles.shape[0]))
      handle.write(np.ascontiguousarray(points, dtype="<f8").tobytes())
      handle.write(np.ascontiguousarray(triangles, dtype="<i4").tobytes())
      handle.write(np.ascontiguousarray(labels, dtype="<i4").tobytes())

  def readG3M(self, path, labelArrayName=LABEL_ARRAY):
    with open(path, "rb") as handle:
      blob = handle.read()
    if blob[:4] != self.MAGIC:
      raise FaceAwareRemeshError(f"{path} is not a G3M1 mesh.")
    vertexCount, triangleCount = struct.unpack_from("<ii", blob, 4)
    offset = 12
    points = np.frombuffer(blob, dtype="<f8", count=3 * vertexCount, offset=offset)
    points = points.reshape((-1, 3))
    offset += 24 * vertexCount
    triangles = np.frombuffer(blob, dtype="<i4", count=3 * triangleCount, offset=offset)
    triangles = triangles.reshape((-1, 3))
    offset += 12 * triangleCount
    labels = np.frombuffer(blob, dtype="<i4", count=triangleCount, offset=offset)
    return buildPolyData(points, triangles, labels, labelArrayName)

  #
  # remeshing
  #

  def remesh(self, polydata, targetEdgeLength=0.85, seamMode="slide",
             seamCornerAngle=20.0, boundaryMode="slide", featureMode="free",
             featureAngle=45.0, iterations=25, smoothSpeed=0.1,
             projectToInput=True, labelArrayName=LABEL_ARRAY):
    """Remesh `polydata` and return (remeshed polydata, report dictionary)."""
    command = self.resolveHelperCommand()
    if polydata.GetCellData().GetArray(labelArrayName) is None:
      raise FaceAwareRemeshError(
        f"The input surface has no {labelArrayName} cell array.")

    workingDirectory = tempfile.mkdtemp(prefix="FaceAwareRemesh-")
    source = os.path.join(workingDirectory, "input.g3m")
    destination = os.path.join(workingDirectory, "output.g3m")
    self.writeG3M(polydata, source, labelArrayName)

    command = command + [
      "--input", source,
      "--output", destination,
      "--target-edge", str(float(targetEdgeLength)),
      "--iterations", str(int(iterations)),
      "--seam-mode", seamMode,
      "--seam-corner-angle", str(float(seamCornerAngle)),
      "--boundary-mode", boundaryMode,
      "--feature-mode", featureMode,
      "--feature-angle", str(float(featureAngle)),
      "--smooth-type", "uniform",
      "--smooth-speed", str(float(smoothSpeed)),
    ]
    if not projectToInput:
      command.append("--no-projection")

    finished = subprocess.run(command, capture_output=True, text=True)
    if finished.returncode != 0:
      raise FaceAwareRemeshError(
        "The remesher failed:\n" + (finished.stderr or finished.stdout)[-2000:])

    remeshed = self.readG3M(destination, labelArrayName)
    helperReport = {}
    lines = [line for line in finished.stdout.strip().splitlines() if line.startswith("{")]
    if lines:
      helperReport = json.loads(lines[-1])
    report = measureRemesh(polydata, remeshed, labelArrayName)
    report["helper"] = helperReport
    return remeshed, report

  def remeshModelNode(self, inputNode, outputNode, **keywordArguments):
    """Remesh one model node into another, and return the report."""
    if inputNode is None or inputNode.GetPolyData() is None:
      raise FaceAwareRemeshError("No input model.")
    if outputNode is None:
      raise FaceAwareRemeshError("No output model.")
    remeshed, report = self.remesh(inputNode.GetPolyData(), **keywordArguments)
    outputNode.SetAndObservePolyData(remeshed)
    outputNode.CreateDefaultDisplayNodes()
    display = outputNode.GetDisplayNode()
    if display:
      display.SetActiveScalarName(LABEL_ARRAY)
      display.SetScalarVisibility(True)
    return report

#
# measurement
#

def measureRemesh(original, remeshed, labelArrayName=LABEL_ARRAY):
  """Report what the remesh did to the labels, the seams, and the triangles.

  The seam distance is deliberately two-sided. The forward direction alone is
  flattering to the point of being useless: a remesher that projects every seam
  vertex onto the original seam polyline scores exactly zero forward error by
  construction, while the polyline through those vertices can still cut the
  corners off every bend. The reverse direction is what measures that.
  """
  originalPoints, originalTriangles, originalLabels = triangleArrays(original, labelArrayName)
  remeshedPoints, remeshedTriangles, remeshedLabels = triangleArrays(remeshed, labelArrayName)

  originalSeam = seamSegments(originalTriangles, originalLabels)
  remeshedSeam = seamSegments(remeshedTriangles, remeshedLabels)

  seam = {"input_segments": int(len(originalSeam)), "output_segments": int(len(remeshedSeam))}
  if len(originalSeam) and len(remeshedSeam):
    forward = pointToSegmentsDistance(
      remeshedPoints[np.unique(remeshedSeam)],
      originalPoints[originalSeam[:, 0]], originalPoints[originalSeam[:, 1]])
    reverse = pointToSegmentsDistance(
      originalPoints[np.unique(originalSeam)],
      remeshedPoints[remeshedSeam[:, 0]], remeshedPoints[remeshedSeam[:, 1]])
    seam.update({
      "off_curve_max": float(forward.max()),
      "corner_cut_max": float(reverse.max()),
      "corner_cut_p95": float(np.percentile(reverse, 95)),
      "hausdorff": float(max(forward.max(), reverse.max())),
    })

  originalAngles = minimumAngles(originalPoints, originalTriangles)
  remeshedAngles = minimumAngles(remeshedPoints, remeshedTriangles)
  originalLengths = edgeLengths(originalPoints, originalTriangles)
  remeshedLengths = edgeLengths(remeshedPoints, remeshedTriangles)

  originalIds = set(int(v) for v in np.unique(originalLabels))
  remeshedIds = set(int(v) for v in np.unique(remeshedLabels))
  originalAreas = labelAreas(originalPoints, originalTriangles, originalLabels)
  remeshedAreas = labelAreas(remeshedPoints, remeshedTriangles, remeshedLabels)

  boundaryEdges, nonManifoldEdges = topologyCounts(remeshed)

  return {
    "labels": {
      "input": sorted(originalIds),
      "output": sorted(remeshedIds),
      "lost": sorted(originalIds - remeshedIds),
      "invented": sorted(remeshedIds - originalIds),
      "area_change_percent": {
        int(key): 100.0 * (remeshedAreas.get(int(key), 0.0) - value) / value
        for key, value in originalAreas.items() if value > 0
      },
    },
    "seam": seam,
    "quality": {
      "triangles_in": int(len(originalTriangles)),
      "triangles_out": int(len(remeshedTriangles)),
      "edge_median_in": float(np.median(originalLengths)),
      "edge_median_out": float(np.median(remeshedLengths)),
      "edge_min_out": float(remeshedLengths.min()),
      "edge_max_out": float(remeshedLengths.max()),
      "worst_angle_in": float(originalAngles.min()),
      "worst_angle_out": float(remeshedAngles.min()),
      "under_20deg_in": int(np.count_nonzero(originalAngles < 20)),
      "under_20deg_out": int(np.count_nonzero(remeshedAngles < 20)),
    },
    "topology": {
      "free_edges": boundaryEdges,
      "non_manifold_edges": nonManifoldEdges,
    },
  }


def formatReport(report):
  labels = report["labels"]
  quality = report["quality"]
  seam = report["seam"]
  topology = report["topology"]
  lines = [
    "labels",
    f"  input           {labels['input']}",
    f"  output          {labels['output']}",
    f"  lost            {labels['lost'] or 'none'}",
    f"  invented        {labels['invented'] or 'none'}",
    "  area change     " + ", ".join(
      f"{key}: {value:+.2f}%" for key, value in sorted(labels["area_change_percent"].items())),
    "",
    "seam",
    f"  segments        {seam['input_segments']} -> {seam['output_segments']}",
  ]
  if "off_curve_max" in seam:
    lines += [
      f"  off curve max   {seam['off_curve_max']:.4f} mm",
      f"  corner cut max  {seam['corner_cut_max']:.4f} mm  (p95 {seam['corner_cut_p95']:.4f} mm)",
    ]
  lines += [
    "",
    "triangles",
    f"  count           {quality['triangles_in']} -> {quality['triangles_out']}",
    f"  edge median     {quality['edge_median_in']:.4f} -> {quality['edge_median_out']:.4f} mm",
    f"  edge range out  {quality['edge_min_out']:.4f} .. {quality['edge_max_out']:.4f} mm",
    f"  worst angle     {quality['worst_angle_in']:.3f} -> {quality['worst_angle_out']:.3f} deg",
    f"  under 20 deg    {quality['under_20deg_in']} -> {quality['under_20deg_out']}",
    "",
    "topology",
    f"  free edges      {topology['free_edges']}",
    f"  non-manifold    {topology['non_manifold_edges']}",
  ]
  helper = report.get("helper") or {}
  if "seconds" in helper:
    lines += ["", f"remeshed in {helper['seconds']} s"]
  return "\n".join(lines)

#
# mesh helpers
#

def vtk_to_numpy(array):
  from vtk.util.numpy_support import vtk_to_numpy as convert
  return convert(array)


def shutilWhich(name):
  import shutil
  return shutil.which(name)


def triangleArrays(polydata, labelArrayName=LABEL_ARRAY):
  """Return (points, triangles, labels) with every cell a triangle."""
  triangulate = vtk.vtkTriangleFilter()
  triangulate.SetInputData(polydata)
  triangulate.PassLinesOff()
  triangulate.PassVertsOff()
  triangulate.Update()
  surface = triangulate.GetOutput()

  points = vtk_to_numpy(surface.GetPoints().GetData()).astype(np.float64)
  cells = vtk_to_numpy(surface.GetPolys().GetData()).reshape((-1, 4))
  if not np.all(cells[:, 0] == 3):
    raise FaceAwareRemeshError("The surface still has non-triangular cells.")
  triangles = cells[:, 1:].astype(np.int32)

  array = surface.GetCellData().GetArray(labelArrayName)
  if array is None:
    raise FaceAwareRemeshError(f"The surface has no {labelArrayName} cell array.")
  labels = vtk_to_numpy(array).astype(np.int32)
  if labels.shape[0] != triangles.shape[0]:
    raise FaceAwareRemeshError(
      f"{labelArrayName} has {labels.shape[0]} values for {triangles.shape[0]} triangles.")
  return points, triangles, labels


def buildPolyData(points, triangles, labels, labelArrayName=LABEL_ARRAY):
  from vtk.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
  polydata = vtk.vtkPolyData()
  vtkPoints = vtk.vtkPoints()
  vtkPoints.SetData(numpy_to_vtk(np.ascontiguousarray(points, dtype=np.float64), deep=True))
  polydata.SetPoints(vtkPoints)

  triangleCount = triangles.shape[0]
  connectivity = np.hstack([
    np.full((triangleCount, 1), 3, dtype=np.int64),
    triangles.astype(np.int64),
  ]).ravel()
  cells = vtk.vtkCellArray()
  cells.SetCells(triangleCount, numpy_to_vtkIdTypeArray(
    np.ascontiguousarray(connectivity), deep=True))
  polydata.SetPolys(cells)

  labelArray = numpy_to_vtk(np.ascontiguousarray(labels, dtype=np.int32), deep=True)
  labelArray.SetName(labelArrayName)
  polydata.GetCellData().AddArray(labelArray)
  polydata.GetCellData().SetActiveScalars(labelArrayName)
  return polydata


def seamSegments(triangles, labels):
  """Vertex-index pairs of every edge between two differently labelled cells."""
  edges = {}
  for index, triangle in enumerate(triangles):
    for corner in range(3):
      first = int(triangle[corner])
      second = int(triangle[(corner + 1) % 3])
      edges.setdefault((min(first, second), max(first, second)), []).append(index)
  segments = [
    key for key, cells in edges.items()
    if len(cells) == 2 and labels[cells[0]] != labels[cells[1]]
  ]
  return np.asarray(segments, dtype=np.int64).reshape((-1, 2))


def pointToSegmentsDistance(query, starts, ends, chunk=2000):
  """Shortest distance from each query point to a set of 3-D line segments."""
  direction = ends - starts
  lengthSquared = np.sum(direction * direction, axis=1)
  lengthSquared[lengthSquared == 0.0] = 1e-30
  out = np.empty(len(query))
  for begin in range(0, len(query), chunk):
    block = query[begin:begin + chunk]
    offset = block[:, None, :] - starts[None, :, :]
    parameter = np.clip(
      np.sum(offset * direction[None, :, :], axis=2) / lengthSquared[None, :], 0.0, 1.0)
    closest = starts[None, :, :] + parameter[:, :, None] * direction[None, :, :]
    out[begin:begin + chunk] = np.sqrt(
      np.min(np.sum((block[:, None, :] - closest) ** 2, axis=2), axis=1))
  return out


def minimumAngles(points, triangles):
  a = points[triangles[:, 0]]
  b = points[triangles[:, 1]]
  c = points[triangles[:, 2]]

  def angle(corner, first, second):
    u = first - corner
    v = second - corner
    cosine = np.sum(u * v, axis=1) / (
      np.linalg.norm(u, axis=1) * np.linalg.norm(v, axis=1) + 1e-30)
    return np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))

  return np.minimum(np.minimum(angle(a, b, c), angle(b, a, c)), angle(c, a, b))


def edgeLengths(points, triangles):
  edges = np.vstack([triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]])
  edges = np.unique(np.sort(edges, axis=1), axis=0)
  return np.linalg.norm(points[edges[:, 0]] - points[edges[:, 1]], axis=1)


def triangleAreas(points, triangles):
  a = points[triangles[:, 0]]
  b = points[triangles[:, 1]]
  c = points[triangles[:, 2]]
  return 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)


def labelAreas(points, triangles, labels):
  areas = triangleAreas(points, triangles)
  return {int(value): float(areas[labels == value].sum()) for value in np.unique(labels)}


def topologyCounts(polydata):
  """Return (free edge count, non-manifold edge count)."""
  features = vtk.vtkFeatureEdges()
  features.SetInputData(polydata)
  features.FeatureEdgesOff()
  features.ManifoldEdgesOff()
  features.BoundaryEdgesOn()
  features.NonManifoldEdgesOff()
  features.Update()
  freeEdges = features.GetOutput().GetNumberOfCells()

  features.BoundaryEdgesOff()
  features.NonManifoldEdgesOn()
  features.Update()
  nonManifoldEdges = features.GetOutput().GetNumberOfCells()
  return freeEdges, nonManifoldEdges

#
# FaceAwareRemeshTest
#

class FaceAwareRemeshTest(ScriptedLoadableModuleTest):
  """Uses ScriptedLoadableModuleTest base class, available at:
  https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
  """

  def setUp(self):
    slicer.mrmlScene.Clear()

  def runTest(self):
    self.setUp()
    self.test_LabelRoundTrip()
    self.setUp()
    self.test_SeamHeldWhilePinned()
    self.setUp()
    self.test_SeamSlidesButStaysOnCurve()

  def labelledSphere(self, resolution=40):
    """A sphere split into two labels by the z = 0 plane, so it has one seam loop."""
    source = vtk.vtkSphereSource()
    source.SetRadius(10.0)
    source.SetThetaResolution(resolution)
    source.SetPhiResolution(resolution)
    triangulate = vtk.vtkTriangleFilter()
    triangulate.SetInputConnection(source.GetOutputPort())
    triangulate.Update()
    sphere = triangulate.GetOutput()

    centers = vtk.vtkCellCenters()
    centers.SetInputData(sphere)
    centers.Update()
    points = centers.GetOutput().GetPoints()
    labels = np.asarray(
      [1 if points.GetPoint(i)[2] >= 0 else 2 for i in range(sphere.GetNumberOfCells())],
      dtype=np.int32)
    from vtk.util.numpy_support import numpy_to_vtk
    array = numpy_to_vtk(np.ascontiguousarray(labels), deep=True)
    array.SetName(LABEL_ARRAY)
    sphere.GetCellData().AddArray(array)
    sphere.GetCellData().SetActiveScalars(LABEL_ARRAY)
    return sphere

  def logicOrSkip(self):
    logic = FaceAwareRemeshLogic()
    try:
      logic.resolveHelperCommand()
    except FaceAwareRemeshError as error:
      self.delayDisplay(f"Skipping: {error}")
      return None
    return logic

  def test_LabelRoundTrip(self):
    """Every triangle comes back labelled, with no label lost or invented."""
    self.delayDisplay("Label round trip")
    logic = self.logicOrSkip()
    if logic is None:
      return
    sphere = self.labelledSphere()
    remeshed, report = logic.remesh(sphere, targetEdgeLength=1.0, seamMode="slide")
    self.assertEqual(report["labels"]["lost"], [])
    self.assertEqual(report["labels"]["invented"], [])
    self.assertEqual(report["labels"]["output"], [1, 2])
    self.assertIsNotNone(remeshed.GetCellData().GetArray(LABEL_ARRAY))
    self.assertEqual(
      remeshed.GetCellData().GetArray(LABEL_ARRAY).GetNumberOfTuples(),
      remeshed.GetNumberOfCells())
    self.assertEqual(report["topology"]["non_manifold_edges"], 0)
    self.delayDisplay("Passed")

  def test_SeamHeldWhilePinned(self):
    """A pinned seam comes back with the same segment count and no drift."""
    self.delayDisplay("Pinned seam")
    logic = self.logicOrSkip()
    if logic is None:
      return
    sphere = self.labelledSphere()
    _, report = logic.remesh(sphere, targetEdgeLength=1.0, seamMode="pin")
    self.assertEqual(report["seam"]["input_segments"], report["seam"]["output_segments"])
    self.assertLess(report["seam"]["hausdorff"], 1e-9)
    self.delayDisplay("Passed")

  def test_SeamSlidesButStaysOnCurve(self):
    """A sliding seam is re-spaced, and every seam vertex stays on the old curve."""
    self.delayDisplay("Sliding seam")
    logic = self.logicOrSkip()
    if logic is None:
      return
    sphere = self.labelledSphere()
    _, report = logic.remesh(
      sphere, targetEdgeLength=2.0, seamMode="slide", seamCornerAngle=0.0)
    self.assertNotEqual(report["seam"]["input_segments"], report["seam"]["output_segments"])
    self.assertLess(report["seam"]["off_curve_max"], 1e-6)
    self.delayDisplay("Passed")
