"""
Rounds the four inside corners of the 7.5 mm chip window in the lid plug
(base_parts/lid_inside.SLDPRT), driven by `chip_pocket_radius` in default.txt.

Run once, on the machine with SolidWorks, with nothing else using it:

    python base_parts/add_chip_pocket_radius.py --depth 1.5 [--no-save]

The window is the notch at the origin corner of the plug's quarter sketch
(`Sketch1`, 25 x 12.5 mm): a 3.75 x 3.75 mm square at (0, 0). Mirrored into
four, the notch makes the 7.5 x 7.5 mm window in the middle of the plug. Its
corner at (3.75, 3.75) is a reflex corner of the material, which is the
inside corner the 1 mm end mill cannot cut sharp. A sketch fillet there,
linked to the equation, rounds it, and the mirror gives all four.

The fillet adds material: (1 - pi/4) R^2 x depth per corner, four corners. The
depth is the depth of the feature the sketch belongs to, which must be given
with --depth: 1.5 mm (`lid_width`, the plug's Boss-Extrude1) or 0.4 mm (the
window cut, Cut-Extrude14, `bond_height` x 2). The check runs in memory and
refuses to save unless the volume change matches the given depth.

The part is saved only when every check passes. A failed run leaves the file
as it was on disk.
"""
import argparse
import math
import os
import sys

import pythoncom
import win32com.client

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from microwave_package import paths  # noqa: E402
from microwave_package.design import equations as eq  # noqa: E402
from microwave_package.design import step_generation as sg  # noqa: E402

INPUT_DIM_ON_CREATE = 10   # swInputDimValOnCreate: the Modify box that blocks automation
MM = 0.001
SKETCH = "Sketch1"        # the plug's quarter sketch, the base of Boss-Extrude1
RADIUS_EQUATION = "chip_pocket_radius"
CREATE_RADIUS = 1.0        # mm: the radius the fillet is made at, before linking
CORNER = (3.75, 3.75)      # half the 7.5 mm window: the quarter's reflex corner
TOL = 1e-3                 # mm
VOLUME_TOL = 1e-3          # mm^3


def null():
    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


def ends(seg):
    a, b = seg.GetStartPoint2, seg.GetEndPoint2
    return (a.X / MM, a.Y / MM), (b.X / MM, b.Y / MM)


def close(p, q):
    return abs(p[0] - q[0]) < TOL and abs(p[1] - q[1]) < TOL


def find_line(segs, at, other):
    """The sketch line that ends at `at` and whose other end satisfies `other`."""
    for seg in segs:
        if seg.GetType != 0:
            continue
        p, q = ends(seg)
        if close(p, at) and other(q):
            return seg
        if close(q, at) and other(p):
            return seg
    sys.exit(f"sketch line at {at} not found -- nothing was changed")


def radius_dims(m, sketch):
    found = {}
    for i in range(1, 61):
        name = f"D{i}@{sketch}"
        p = m.Parameter(name)
        if p is not None:
            found[name] = p.SystemValue / MM
    return found


def relink_and_import(m, root):
    """Point the equation link at this checkout's default.txt and bring in the new
    parameter exactly once (as add_cavity_radius.py does)."""
    em = m.GetEquationMgr
    em.LinkToFile = False
    em.FilePath = os.path.join(root, "default.txt")
    em.LinkToFile = True
    em.UpdateValuesFromExternalEquationFile
    have = [em.Equation(i).replace(" ", "") for i in range(em.GetCount)]
    if sum(e.startswith(f'"{RADIUS_EQUATION}"=') for e in have) != 1:
        sys.exit(f"{RADIUS_EQUATION} did not come in from default.txt exactly once -- nothing was changed")


def volume(m):
    m.ForceRebuild3(False)
    return m.Extension.CreateMassProperty.Volume * 1e9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=float, required=True,
                    help="depth in mm of the feature the corner belongs to (1.5 lid_width, or 0.4)")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    root = os.path.dirname(os.path.abspath(__file__))
    part = os.path.join(root, "lid_inside.SLDPRT")
    app, err = sg.connect_solidworks()
    if not app:
        sys.exit(err)
    was = app.GetUserPreferenceToggle(INPUT_DIM_ON_CREATE)
    app.SetUserPreferenceToggle(INPUT_DIM_ON_CREATE, False)
    m = None
    try:
        m, err = sg.open_and_activate_part(app, part)
        if not m:
            sys.exit(err)
        if os.path.normcase(m.GetPathName) != os.path.normcase(part):
            sys.exit(f"opened {m.GetPathName}, not {part} -- nothing was changed")
        relink_and_import(m, root)
        base = volume(m)
        print("volume before the fillet (mm3)", round(base, 4))
        before = radius_dims(m, SKETCH)

        m.ClearSelection2(True)
        if not m.Extension.SelectByID2(SKETCH, "SKETCH", 0, 0, 0, False, 0, null(), 0):
            sys.exit(f"could not select {SKETCH} -- nothing was changed")
        sm = m.SketchManager
        sm.InsertSketch(True)
        segs = list(sm.ActiveSketch.GetSketchSegments)
        # The notch's two sides: the wall x = 3.75 down to y = 0, and the top y = 3.75 back to x = 0.
        wall = find_line(segs, CORNER, lambda q: close(q, (CORNER[0], 0.0)))
        top = find_line(segs, CORNER, lambda q: close(q, (0.0, CORNER[1])))
        m.ClearSelection2(True)
        wall.Select4(False, null())
        top.Select4(True, null())
        if sm.CreateFillet(CREATE_RADIUS * MM, 1) is None:
            sys.exit("the window corner fillet was not made -- nothing was changed")
        m.ClearSelection2(True)
        sm.InsertSketch(True)
        m.ClearSelection2(True)

        new = {k: v for k, v in radius_dims(m, SKETCH).items() if k not in before}
        if len(new) != 1 or any(abs(v - CREATE_RADIUS) > 1e-6 for v in new.values()):
            sys.exit(f"expected one new {CREATE_RADIUS:g} mm radius dimension, found {new} -- nothing was changed")
        for name in new:
            m.GetEquationMgr.Add2(-1, f'"{name}"="{RADIUS_EQUATION}"', True)
            print("linked", name, "to", RADIUS_EQUATION)
        change = volume(m) - base
        per_corner = (1 - math.pi / 4) * CREATE_RADIUS ** 2
        print("volume change (mm3)", round(change, 4))
        for depth in (1.5, 0.4):
            print(f"  if depth {depth:g} mm: expected +{4 * per_corner * depth:.4f} mm3")
        expected = 4 * per_corner * args.depth
        print(f"checked against depth {args.depth:g} mm: expected +{expected:.4f} mm3")
        if abs(change - expected) > VOLUME_TOL:
            sys.exit(f"the four corners changed the volume by {change:+.4f} mm3, not {expected:+.4f} -- "
                     f"nothing was saved")
        if not args.no_save:
            e = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            w = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            print("saved", m.Save3(1, e, w), e.value, w.value)
        else:
            print("--no-save: nothing written")
    finally:
        if m is not None:
            app.CloseDoc(os.path.basename(part))
        app.SetUserPreferenceToggle(INPUT_DIM_ON_CREATE, was)


if __name__ == "__main__":
    main()
