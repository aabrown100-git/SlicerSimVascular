import logging
import math
import traceback
import json
from typing import Annotated, Optional

import numpy as np
import vtk, qt, ctk, slicer
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper, WithinRange, Choice


def correspondenceColor(s):
  """Shared cyclic seam color used by both the Qt and VTK views."""
  color = qt.QColor()
  color.setHsvF(float(s % 1.0), 0.85, 0.95)
  return color


def projectPointsToBestFitPlane(points):
  """Project ordered 3D points to PCA plane coordinates in millimeters."""
  points = np.asarray(points, dtype=float)
  if len(points) < 3:
    raise ValueError('At least three seam points are required for projection')
  centered = points - np.mean(points, axis=0)
  _, singularValues, axes = np.linalg.svd(centered, full_matrices=False)
  if len(singularValues) < 2 or singularValues[1] < 1.0e-8:
    raise ValueError('The seam points do not define a plane')
  projected = centered.dot(axes[:2].T)
  signedArea = 0.5*np.sum(
    projected[:, 0]*np.roll(projected[:, 1], -1)
    - projected[:, 1]*np.roll(projected[:, 0], -1))
  if signedArea < 0.0:
    projected[:, 1] *= -1.0
  return projected


class MembraneDesigner(ScriptedLoadableModule):
  def __init__(self, parent):
    ScriptedLoadableModule.__init__(self, parent)
    self.parent.title = "Membrane Designer"
    self.parent.categories = ["Surface Models"]
    self.parent.contributors = ["Aaron Brown (Stanford)"]
    self.parent.helpText = """
Interactively edit a flat membrane outline and preview the pressurized membrane
sewn to a 3D closed curve. Boundary colors show normalized seam correspondence;
the inflated surface provides Green-Lagrange strain scalars.
"""


@parameterNodeWrapper
class MembraneDesignerParameterNode:
  seamCurve: Optional[slicer.vtkMRMLMarkupsClosedCurveNode] = None
  outputModel: Optional[slicer.vtkMRMLModelNode] = None
  pressureKPa: Annotated[float, WithinRange(0.0, 50.0)] = 2.0
  youngMPa: Annotated[float, WithinRange(0.01, 100.0)] = 1.0
  thicknessMm: Annotated[float, WithinRange(0.01, 10.0)] = 0.5
  damping: Annotated[float, WithinRange(0.8, 0.9999)] = 0.985
  visualizationMetric: Annotated[str, Choice([
    "Maximum principal strain", "Areal strain", "Correspondence only"])] = "Maximum principal strain"
  splineControlPointsJson: str = ""


class PeriodicSpline:
  @staticmethod
  def sample(controlPoints, count):
    """Uniform periodic cubic B-spline samples."""
    p = np.asarray(controlPoints, dtype=float)
    if len(p) < 4:
      raise ValueError("At least four control points are required")
    u = np.arange(count, dtype=float) * len(p) / count
    i = np.floor(u).astype(int)
    t = u - i
    b0 = (1-t)**3 / 6.0
    b1 = (3*t**3 - 6*t**2 + 4) / 6.0
    b2 = (-3*t**3 + 3*t**2 + 3*t + 1) / 6.0
    b3 = t**3 / 6.0
    return (b0[:,None]*p[(i-1)%len(p)] + b1[:,None]*p[i%len(p)] +
            b2[:,None]*p[(i+1)%len(p)] + b3[:,None]*p[(i+2)%len(p)])


class MembraneMesh:
  @staticmethod
  def _insidePolygon(points, polygon):
    """Vectorized even-odd test for candidate interior points."""
    points=np.asarray(points); polygon=np.asarray(polygon)
    inside=np.zeros(len(points),dtype=bool)
    x=points[:,0]; y=points[:,1]
    for i in range(len(polygon)):
      x0,y0=polygon[i-1]; x1,y1=polygon[i]
      crosses=((y0>y)!=(y1>y))
      xCross=x0+(y-y0)*(x1-x0)/(y1-y0+1e-30)
      inside ^= crosses & (x<xCross)
    return inside

  @staticmethod
  def create(controlPoints, angular=64, resolution=16):
    """Create a constrained, unstructured triangular mesh of the pattern."""
    boundary = PeriodicSpline.sample(controlPoints, angular)
    lower=np.min(boundary,axis=0); upper=np.max(boundary,axis=0)
    targetInterior=max(angular,angular*resolution-angular)
    polygonArea=0.5*abs(np.sum(boundary[:,0]*np.roll(boundary[:,1],-1)
                                - boundary[:,1]*np.roll(boundary[:,0],-1)))
    spacing=math.sqrt(max(polygonArea,1e-9)/targetInterior)
    dy=spacing*math.sqrt(3.0)/2.0
    candidates=[]
    rng=np.random.default_rng(1731)
    row=0; y=lower[1]+0.5*dy
    while y<upper[1]:
      x=lower[0]+0.5*spacing+(row%2)*0.5*spacing
      while x<upper[0]:
        jitter=rng.uniform(-0.12,0.12,2)*np.array([spacing,dy])
        candidates.append([x+jitter[0],y+jitter[1]])
        x+=spacing
      y+=dy; row+=1
    candidates=np.asarray(candidates,dtype=float)
    interior=candidates[MembraneMesh._insidePolygon(candidates,boundary)]
    points=np.vstack((boundary,interior))

    vtkPoints=vtk.vtkPoints()
    for point in points: vtkPoints.InsertNextPoint(float(point[0]),float(point[1]),0.0)
    inputPoly=vtk.vtkPolyData(); inputPoly.SetPoints(vtkPoints)
    boundaryLines=vtk.vtkCellArray()
    for i in range(angular):
      line=vtk.vtkLine(); line.GetPointIds().SetId(0,i); line.GetPointIds().SetId(1,(i+1)%angular)
      boundaryLines.InsertNextCell(line)
    source=vtk.vtkPolyData(); source.SetPoints(vtkPoints); source.SetLines(boundaryLines)
    delaunay=vtk.vtkDelaunay2D(); delaunay.SetInputData(inputPoly); delaunay.SetSourceData(source)
    delaunay.BoundingTriangulationOff(); delaunay.SetTolerance(1e-6); delaunay.Update()
    output=delaunay.GetOutput(); triangles=[]
    for cellId in range(output.GetNumberOfCells()):
      cell=output.GetCell(cellId)
      if cell.GetNumberOfPoints()==3:
        tri=[cell.GetPointId(i) for i in range(3)]
        a,b,c=points[tri]
        signedTwiceArea=(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
        if signedTwiceArea<0: tri[1],tri[2]=tri[2],tri[1]
        triangles.append(tri)
    if not triangles:
      raise ValueError('Could not triangulate the membrane boundary')
    return points, np.asarray(triangles,dtype=np.int32), np.arange(angular,dtype=np.int32)


class XPBDSolver:
  """Compact XPBD membrane solver; topology and rest metric remain fixed."""
  KPA_TO_NEWTON_PER_MM2 = 1.0e-3
  KG_PER_M3_TO_NEWTON_SECOND2_PER_MM4 = 1.0e-12
  DEFAULT_DENSITY_KG_PER_M3 = 1000.0

  def __init__(self, rest2d, triangles, boundaryIds, seam3d):
    self.rest2d = np.asarray(rest2d, dtype=float)
    self.triangles = np.asarray(triangles, dtype=np.int32)
    self.boundaryIds = np.asarray(boundaryIds, dtype=np.int32)
    self.seam3d = np.asarray(seam3d, dtype=float)
    self.edges = self._uniqueEdges(self.triangles)
    d = self.rest2d[self.edges[:,1]] - self.rest2d[self.edges[:,0]]
    self.restLengths = np.linalg.norm(d, axis=1)
    self.positions = self._initialPositions()

  def _lumpedMasses(self, thicknessMm, densityKgPerM3=DEFAULT_DENSITY_KG_PER_M3):
    """Return vertex masses in N*s^2/mm for an mm-N-s simulation."""
    a, b, c = self.triangles.T
    ab = self.rest2d[b] - self.rest2d[a]
    ac = self.rest2d[c] - self.rest2d[a]
    restAreas = 0.5 * np.abs(ab[:, 0]*ac[:, 1] - ab[:, 1]*ac[:, 0])
    density = densityKgPerM3 * self.KG_PER_M3_TO_NEWTON_SECOND2_PER_MM4
    triangleMasses = density * thicknessMm * restAreas
    masses = np.zeros(len(self.rest2d))
    share = triangleMasses / 3.0
    np.add.at(masses, a, share)
    np.add.at(masses, b, share)
    np.add.at(masses, c, share)
    return masses

  @staticmethod
  def _uniqueEdges(triangles):
    edges = np.vstack((triangles[:,[0,1]], triangles[:,[1,2]], triangles[:,[2,0]]))
    edges.sort(axis=1)
    return np.unique(edges, axis=0)

  def _initialPositions(self):
    boundary2d=self.rest2d[self.boundaryIds]
    design=np.column_stack((boundary2d,np.ones(len(boundary2d))))
    affine=np.linalg.lstsq(design,self.seam3d,rcond=None)[0]
    x=np.column_stack((self.rest2d,np.ones(len(self.rest2d)))).dot(affine)
    x[self.boundaryIds] = self.seam3d
    return x

  def solve(self, pressureKPa=2.0, youngMPa=1.0, thicknessMm=0.5,
            damping=0.985, steps=260, iterations=8):
    x = self.positions.copy()
    v = np.zeros_like(x)
    fixed = np.zeros(len(x), dtype=bool); fixed[self.boundaryIds] = True
    masses = self._lumpedMasses(thicknessMm)
    invMass = np.zeros(len(x))
    movingMass = (~fixed) & (masses > 0.0)
    invMass[movingMass] = 1.0 / masses[movingMass]
    # Explicit pressure integration must resolve the membrane's elastic wave
    # timescale.  The previous 1/120 s preview step only appeared stable because
    # every vertex had an unphysical unit mass.
    dt = 1.0e-4
    referenceDt = 1.0/120.0
    dampingPerStep = damping**(dt/referenceDt)
    # Compliance is deliberately scaled for mm/kPa numerical units.
    compliance = 1.0 / max(youngMPa*thicknessMm*2.0e4, 1e-6)
    alpha = compliance/(dt*dt)
    lambdas = np.zeros(len(self.edges))
    for step in range(steps):
      old = x.copy()
      forces = np.zeros_like(x)
      # kPa -> N/mm^2; area normals are in mm^2, so pf is in newtons.
      p = (pressureKPa * self.KPA_TO_NEWTON_PER_MM2
           * min(1.0, (step+1)/max(30, steps//4)))
      a, b, c = self.triangles.T
      areaNormals = 0.5*np.cross(x[b]-x[a], x[c]-x[a])
      pf = p*areaNormals/3.0
      np.add.at(forces, a, pf); np.add.at(forces, b, pf); np.add.at(forces, c, pf)
      v = dampingPerStep*(v + dt*forces*invMass[:,None])
      x += dt*v
      x[fixed] = self.seam3d
      lambdas.fill(0.0)
      for _ in range(iterations):
        i=self.edges[:,0]; j=self.edges[:,1]
        delta=x[i]-x[j]; length=np.linalg.norm(delta,axis=1)
        valid=length>1e-10
        w=invMass[i]+invMass[j]
        dl=np.zeros(len(self.edges))
        dl[valid]=(-(length[valid]-self.restLengths[valid])-alpha*lambdas[valid])/(w[valid]+alpha)
        corr=np.zeros_like(delta); corr[valid]=dl[valid,None]*delta[valid]/length[valid,None]
        accumulated=np.zeros_like(x); counts=np.zeros(len(x))
        np.add.at(accumulated,i,invMass[i,None]*corr)
        np.add.at(accumulated,j,-invMass[j,None]*corr)
        np.add.at(counts,i,(invMass[i]>0).astype(float)); np.add.at(counts,j,(invMass[j]>0).astype(float))
        moving=counts>0
        x[moving] += 0.9*accumulated[moving]/counts[moving,None]
        lambdas += dl
        x[fixed] = self.seam3d
      v = (x-old)/dt
      if step > 40 and np.max(np.linalg.norm(v[~fixed], axis=1)) < 2e-3:
        break
    self.positions = x
    return x, step+1

  def strain(self):
    values = np.zeros(len(self.triangles))
    areal = np.zeros(len(self.triangles))
    for ci, tri in enumerate(self.triangles):
      r0,r1,r2 = self.rest2d[tri]
      x0,x1,x2 = self.positions[tri]
      Dm = np.column_stack((r1-r0,r2-r0))
      if abs(np.linalg.det(Dm)) < 1e-12: continue
      F = np.column_stack((x1-x0,x2-x0)).dot(np.linalg.inv(Dm))
      E = 0.5*(F.T.dot(F)-np.eye(2))
      eig = np.linalg.eigvalsh(E)
      values[ci] = eig[-1]
      areal[ci] = math.sqrt(max(np.linalg.det(F.T.dot(F)),0.0))-1.0
    return values, areal


class PatternCanvas(qt.QGraphicsView):
  def __init__(self, changedCallback, parent=None):
    qt.QGraphicsView.__init__(self, parent)
    self.changedCallback = changedCallback
    self.scene2d = qt.QGraphicsScene(self)
    self.setScene(self.scene2d)
    self.setRenderHint(qt.QPainter.Antialiasing)
    self.setMinimumWidth(420)
    self.controlItems = []
    self.pathItems = []
    self._last = None
    self.pollTimer = qt.QTimer(self)
    self.pollTimer.setInterval(80)
    self.pollTimer.connect('timeout()', self._poll)
    self.pollTimer.start()
    self.setEllipse()

  def setEllipse(self, n=12, rx=45.0, ry=32.0):
    self.setControls([[rx*math.cos(2*math.pi*i/n), ry*math.sin(2*math.pi*i/n)] for i in range(n)])

  def setControls(self, controlPoints):
    self.scene2d.clear(); self.controlItems=[]; self.pathItems=[]
    for x, y in controlPoints:
      item=qt.QGraphicsEllipseItem(-5,-5,10,10)
      item.setPos(float(x), -float(y))
      item.setBrush(qt.QBrush(qt.QColor('#ffffff')))
      item.setPen(qt.QPen(qt.QColor('#202020'),1.5))
      item.setFlag(qt.QGraphicsItem.ItemIsMovable, True)
      self.scene2d.addItem(item); self.controlItems.append(item)
    self._last=self.controls().copy(); self._redraw(); self.fitInView(self.scene2d.itemsBoundingRect(), qt.Qt.KeepAspectRatio)

  def controls(self):
    return np.array([[i.pos().x(), -i.pos().y()] for i in self.controlItems])

  def _poll(self):
    now=self.controls()
    if self._last is None or not np.allclose(now,self._last):
      self._last=now.copy(); self._redraw(); self.changedCallback()

  @staticmethod
  def colorAt(s):
    return correspondenceColor(s)

  def _redraw(self):
    for item in self.pathItems: self.scene2d.removeItem(item)
    self.pathItems=[]
    pts=PeriodicSpline.sample(self.controls(),128)
    for i in range(len(pts)):
      j=(i+1)%len(pts)
      line=qt.QGraphicsLineItem(pts[i,0],-pts[i,1],pts[j,0],-pts[j,1])
      line.setPen(qt.QPen(self.colorAt(i/len(pts)),4.0))
      self.scene2d.addItem(line); self.pathItems.append(line)
    marker=qt.QGraphicsEllipseItem(pts[0,0]-7,-pts[0,1]-7,14,14)
    marker.setBrush(qt.QBrush(qt.QColor('#ffffff'))); marker.setPen(qt.QPen(qt.QColor('#000000'),3))
    self.scene2d.addItem(marker); self.pathItems.append(marker)


class MembraneDesignerWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
  def __init__(self, parent=None):
    ScriptedLoadableModuleWidget.__init__(self,parent); VTKObservationMixin.__init__(self)
    self.logic=None; self.canvasDock=None; self.canvas=None; self.updateTimer=None
    self._parameterNode=None; self._parameterNodeGuiTag=None; self._restoringCanvas=False

  def setup(self):
    ScriptedLoadableModuleWidget.setup(self)
    uiWidget=slicer.util.loadUI(self.resourcePath('UI/MembraneDesigner.ui'))
    self.layout.addWidget(uiWidget); self.ui=slicer.util.childWidgetVariables(uiWidget)
    uiWidget.setMRMLScene(slicer.mrmlScene)
    self.logic=MembraneDesignerLogic()
    self._createCanvas()
    self.ui.seamSelector.setMRMLScene(slicer.mrmlScene)
    self.ui.outputModelSelector.setMRMLScene(slicer.mrmlScene)
    self.updateTimer=qt.QTimer(); self.updateTimer.setSingleShot(True); self.updateTimer.setInterval(300)
    self.updateTimer.connect('timeout()', self.onSimulate)
    self.ui.demoButton.clicked.connect(self.onCreateDemo)
    self.ui.simulateButton.clicked.connect(self.onSimulate)
    self.ui.metricComboBox.currentIndexChanged.connect(self.onMetricChanged)
    self.ui.seamSelector.currentNodeChanged.connect(self.onSeamChanged)
    self.addObserver(slicer.mrmlScene,slicer.mrmlScene.StartCloseEvent,self.onSceneStartClose)
    self.addObserver(slicer.mrmlScene,slicer.mrmlScene.EndCloseEvent,self.onSceneEndClose)
    self.initializeParameterNode()

  def _createCanvas(self):
    self.canvasDock=qt.QDockWidget('Flat pattern (2D)',slicer.util.mainWindow())
    self.canvasDock.objectName='MembraneDesignerPatternDock'
    self.canvasDock.setAllowedAreas(qt.Qt.LeftDockWidgetArea | qt.Qt.RightDockWidgetArea)
    self.canvas=PatternCanvas(self.onPatternChanged)
    self.canvasDock.setWidget(self.canvas)
    slicer.util.mainWindow().addDockWidget(qt.Qt.RightDockWidgetArea,self.canvasDock)
    self.canvasDock.setMinimumWidth(420)
    self.canvasDock.show()
    self.canvasDock.raise_()

  def cleanup(self):
    self.removeObservers()
    self.setParameterNode(None)
    if self.canvasDock: self.canvasDock.close(); self.canvasDock.deleteLater(); self.canvasDock=None

  def enter(self):
    self.initializeParameterNode()
    if self.canvasDock: self.canvasDock.show()

  def exit(self):
    if self.canvasDock: self.canvasDock.hide()
    self.setParameterNode(None)

  def onSceneStartClose(self,caller=None,event=None): self.setParameterNode(None)
  def onSceneEndClose(self,caller=None,event=None):
    if self.parent.isEntered: self.initializeParameterNode()

  def initializeParameterNode(self):
    self.setParameterNode(self.logic.getParameterNode())

  def setParameterNode(self,node):
    if self._parameterNode:
      if self._parameterNodeGuiTag is not None: self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
      self.removeObserver(self._parameterNode,vtk.vtkCommand.ModifiedEvent,self.onParameterNodeModified)
    self._parameterNode=node; self._parameterNodeGuiTag=None
    if node:
      # Migrate the short-lived prototype's integer visualization value.
      rawMetric=node.parameterNode.GetParameter('visualizationMetric')
      if rawMetric in ('0','1','2'):
        node.parameterNode.SetParameter('visualizationMetric',[
          'Maximum principal strain','Areal strain','Correspondence only'][int(rawMetric)])
      self._parameterNodeGuiTag=node.connectGui(self.ui)
      self.addObserver(node,vtk.vtkCommand.ModifiedEvent,self.onParameterNodeModified)
      self.restoreCanvasFromParameterNode()
      seam = node.seamCurve
      self._lastSeamNodeId = seam.GetID() if seam else None
      if seam and not node.splineControlPointsJson:
        self._lastSeamNodeId = None
        self.onSeamChanged(seam)

  def onParameterNodeModified(self,caller=None,event=None):
    if not self.canvas or not self._parameterNode: return

  def restoreCanvasFromParameterNode(self):
    if not self._parameterNode or not self._parameterNode.splineControlPointsJson: return
    try:
      points=json.loads(self._parameterNode.splineControlPointsJson)
      if len(points)>=4:
        self._restoringCanvas=True; self.canvas.setControls(points); self._restoringCanvas=False
    except Exception: logging.warning('Could not restore membrane spline controls')

  def onPatternChanged(self):
    if self._restoringCanvas or not self._parameterNode: return
    self._parameterNode.splineControlPointsJson=json.dumps(self.canvas.controls().tolist())
    if self.updateTimer and self._parameterNode.seamCurve: self.updateTimer.start()

  def onSeamChanged(self, node=None):
    seam = node if node is not None else self.ui.seamSelector.currentNode()
    seamId = seam.GetID() if seam else None
    if seamId == getattr(self, '_lastSeamNodeId', None): return
    self._lastSeamNodeId = seamId
    if not seam or not self._parameterNode: return
    try:
      if self._parameterNode.seamCurve is not seam:
        self._parameterNode.seamCurve = seam
      seamSamples = self._seamPoints(seam, 16)
      self.canvas.setControls(projectPointsToBestFitPlane(seamSamples))
      self.onPatternChanged()
    except Exception as exc:
      logging.warning('Could not initialize flat pattern from seam: %s', exc)

  def onCreateDemo(self):
    self.canvas.setEllipse(12,45.0,32.0)
    node=slicer.mrmlScene.GetFirstNodeByName('Demo suture contour')
    if not node or not node.IsA('vtkMRMLMarkupsClosedCurveNode'):
      node=slicer.mrmlScene.AddNewNodeByClass('vtkMRMLMarkupsClosedCurveNode','Demo suture contour')
    else:
      node.RemoveAllControlPoints()
    for i in range(48):
      t=2*math.pi*i/48
      node.AddControlPoint(vtk.vtkVector3d(45*math.cos(t),32*math.sin(t),0.0))
    self._parameterNode.seamCurve=node; self.ui.seamSelector.setCurrentNode(node); self.onSimulate()

  def _seamPoints(self,node,count):
    curve=node.GetCurvePointsWorld()
    pts=np.array([curve.GetPoint(i) for i in range(curve.GetNumberOfPoints())])
    if len(pts)<3: raise ValueError('The suture contour needs at least three sampled points')
    d=np.linalg.norm(np.roll(pts,-1,axis=0)-pts,axis=1); cumulative=np.r_[0,np.cumsum(d)]
    total=cumulative[-1]; samples=np.arange(count)*total/count
    out=[]
    for s in samples:
      k=min(np.searchsorted(cumulative,s,side='right')-1,len(pts)-1)
      a=(s-cumulative[k])/max(d[k],1e-12); out.append((1-a)*pts[k]+a*pts[(k+1)%len(pts)])
    return np.array(out)

  def onSimulate(self):
    try:
      seam=self._parameterNode.seamCurve if self._parameterNode else None
      if not seam: return
      self.ui.statusLabel.text='Solving…'; slicer.app.processEvents()
      rest,tri,boundary=MembraneMesh.create(self.canvas.controls(),64,16)
      seamPts=self._seamPoints(seam,len(boundary))
      solver=XPBDSolver(rest,tri,boundary,seamPts)
      positions,steps=solver.solve(self._parameterNode.pressureKPa,self._parameterNode.youngMPa,self._parameterNode.thicknessMm,self._parameterNode.damping)
      principal,areal=solver.strain()
      creatingOutputModel = self._parameterNode.outputModel is None
      model=self.logic.updateModel(positions,tri,boundary,principal,areal,self._parameterNode.outputModel)
      if creatingOutputModel: self._parameterNode.outputModel=model
      self.logic.updateSeamModel(seamPts)
      self.logic.setMetric(model,['Maximum principal strain','Areal strain','Correspondence only'].index(self._parameterNode.visualizationMetric))
      if creatingOutputModel: slicer.util.resetThreeDViews()
      self.ui.statusLabel.text=f'Converged preview: {len(positions)} vertices, {len(tri)} triangles, {steps} steps. Max strain {100*np.max(principal):.1f}%.'
    except Exception as exc:
      logging.error(traceback.format_exc()); self.ui.statusLabel.text='Simulation failed: '+str(exc)

  def onMetricChanged(self,index):
    node=self._parameterNode.outputModel if self._parameterNode else None
    if node: self.logic.setMetric(node,index)


class MembraneDesignerLogic(ScriptedLoadableModuleLogic):
  def getParameterNode(self):
    return MembraneDesignerParameterNode(super().getParameterNode())

  def _polyData(self,points,triangles):
    poly=vtk.vtkPolyData(); vtkPts=vtk.vtkPoints(); vtkPts.SetNumberOfPoints(len(points))
    for i,p in enumerate(points): vtkPts.SetPoint(i,*p)
    cells=vtk.vtkCellArray()
    for tri in triangles:
      cell=vtk.vtkTriangle()
      for j,v in enumerate(tri): cell.GetPointIds().SetId(j,int(v))
      cells.InsertNextCell(cell)
    poly.SetPoints(vtkPts); poly.SetPolys(cells); return poly

  def updateModel(self,points,triangles,boundary,principal,areal,node=None):
    if not node: node=slicer.mrmlScene.AddNewNodeByClass('vtkMRMLModelNode','Inflated membrane'); node.CreateDefaultDisplayNodes()
    poly=self._polyData(points,triangles)
    for name,values in [('MaximumPrincipalStrain',principal),('ArealStrain',areal)]:
      arr=vtk.vtkFloatArray(); arr.SetName(name); arr.SetNumberOfTuples(len(values))
      for i,v in enumerate(values): arr.SetValue(i,float(v))
      poly.GetCellData().AddArray(arr)
    s=vtk.vtkFloatArray(); s.SetName('BoundaryArclength'); s.SetNumberOfTuples(len(points)); s.Fill(-1)
    for i,v in enumerate(boundary): s.SetValue(int(v),i/len(boundary))
    poly.GetPointData().AddArray(s); node.SetAndObservePolyData(poly); return node

  def updateSeamModel(self,seam):
    node=slicer.mrmlScene.GetFirstNodeByName('Membrane seam correspondence')
    if not node: node=slicer.mrmlScene.AddNewNodeByClass('vtkMRMLModelNode','Membrane seam correspondence'); node.CreateDefaultDisplayNodes()
    pts=vtk.vtkPoints(); lines=vtk.vtkCellArray(); colors=vtk.vtkUnsignedCharArray(); colors.SetName('CorrespondenceColor'); colors.SetNumberOfComponents(3)
    for i,p in enumerate(seam):
      pts.InsertNextPoint(*p); c=correspondenceColor(i/len(seam)); colors.InsertNextTuple3(c.red(),c.green(),c.blue())
    line=vtk.vtkPolyLine(); line.GetPointIds().SetNumberOfIds(len(seam)+1)
    for i in range(len(seam)): line.GetPointIds().SetId(i,i)
    line.GetPointIds().SetId(len(seam),0); lines.InsertNextCell(line)
    poly=vtk.vtkPolyData(); poly.SetPoints(pts); poly.SetLines(lines); poly.GetPointData().SetScalars(colors); node.SetAndObservePolyData(poly)
    d=node.GetDisplayNode(); d.SetLineWidth(6); d.SetScalarVisibility(True); d.SetActiveScalarName('CorrespondenceColor'); d.SetScalarRangeFlag(d.UseDirectMapping)

  def setMetric(self,node,index):
    d=node.GetDisplayNode(); d.SetScalarVisibility(index<2)
    if index<2:
      d.SetActiveScalar(['MaximumPrincipalStrain','ArealStrain'][index],vtk.vtkAssignAttribute.CELL_DATA)
      d.SetAndObserveColorNodeID('vtkMRMLColorTableNodeFileDivergingBlueRed.txt')
    else:
      d.SetScalarVisibility(False); d.SetColor(.85,.85,.85)


class MembraneDesignerTest(ScriptedLoadableModuleTest):
  def runTest(self):
    self.setUp(); self.test_MembraneDesignerCore()

  def setUp(self):
    slicer.mrmlScene.Clear()

  def test_MembraneDesignerCore(self):
    controls=np.array([[50*math.cos(2*math.pi*i/12),35*math.sin(2*math.pi*i/12)] for i in range(12)])
    rest,tri,boundary=MembraneMesh.create(controls,32,8)
    seam=np.column_stack((50*np.cos(np.arange(32)*2*math.pi/32),35*np.sin(np.arange(32)*2*math.pi/32),np.zeros(32)))
    solver=XPBDSolver(rest,tri,boundary,seam); x,_=solver.solve(1.0,1.0,.5,steps=20,iterations=3)
    principal,areal=solver.strain()
    self.assertGreater(len(x),len(boundary)); self.assertEqual(len(principal),len(tri))
    self.assertTrue(np.array_equal(boundary,np.arange(32)))
    edges=XPBDSolver._uniqueEdges(tri)
    edgeSet={tuple(edge) for edge in edges}
    self.assertTrue(all(tuple(sorted((i,(i+1)%32))) in edgeSet for i in range(32)))
    self.assertTrue(np.all(np.isfinite(x))); self.assertTrue(np.allclose(x[boundary],seam))
    tilted=np.column_stack((seam[:,0],seam[:,1],0.2*seam[:,0]-0.1*seam[:,1]+7.0))
    projected=projectPointsToBestFitPlane(tilted)
    self.assertEqual(projected.shape,(32,2))
    self.assertTrue(np.allclose(np.mean(projected,axis=0),0.0,atol=1e-10))
    self.assertGreater(np.sum(projected[:,0]*np.roll(projected[:,1],-1)
                              - projected[:,1]*np.roll(projected[:,0],-1)),0.0)
    self.assertTrue(np.allclose(np.linalg.norm(tilted[1:]-tilted[:-1],axis=1),
                                np.linalg.norm(projected[1:]-projected[:-1],axis=1)))
