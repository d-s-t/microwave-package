"""
Rounds the corners of the pedestal recess in base_parts/box.SLDPRT, driven
by `pedestal_corner_radius` in default.txt.

Run once, on the machine with SolidWorks, with nothing else using it:

    python base_parts/add_pedestal_radius.py [--no-save]

The recess is `Cut-Extrude3` through `Sketch2`: an octagon, a square of the
die's size with a 45-degree chamfer of `chip_leg` at each corner. The part is
mirrored after the cut (`box`), so the sketch only needs its two corners in
the first quadrant: (3.15, 2.35) and (2.35, 3.15), in sketch coordinates.
This adds a sketch fillet at each of them. SolidWorks adds a radius dimension
to each fillet (D7 and D11 of Sketch2); both are linked to the equation, so
the mirror gives all eight corners at pedestal_corner_radius.

A sketch fillet cannot have a zero radius, and a dimension set to zero keeps
its last value without an error, so design/pedestal.py refuses 0.

The fillets are made at 1 mm and then linked, so the part is built at the
value of pedestal_corner_radius in default.txt. The part's equation link is
re-pointed at THIS checkout's default.txt first, and the new parameter is
imported exactly once.
"""
import argparse
import os
import sys

import pythoncom
import win32com.client

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from microwave_package.design import step_generation as sg  # noqa: E402

INPUT_DIM_ON_CREATE = 10   # swInputDimValOnCreate: the Modify box that blocks automation
MM = 0.001
SKETCH = "Sketch2"
RADIUS_EQUATION = "pedestal_corner_radius"
CREATE_RADIUS = 1.0        # mm: the radius the fillets are made at, before linking
WALL = 3.15                # chip_length / 2 (sketch x)
HALF_SIDE = 3.15           # chip_side / 2 (sketch y)
LEG = 0.8                  # chip_leg
TOL = 1e-3                 # mm


def null():
    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


def ends(seg):
    """A sketch line's two ends in mm, sketch coordinates."""
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


def radius_dims(m):
    """Names and mm values of the sketch's dimensions D1..D60 that exist now."""
    found = {}
    for i in range(1, 61):
        name = f"D{i}@{SKETCH}"
        p = m.Parameter(name)
        if p is not None:
            found[name] = p.SystemValue / MM
    return found


def relink_and_import(m, root):
    """Point the equation link at this checkout's default.txt, then bring in
    the new parameter exactly once (unlink and relink, as add_vents does)."""
    em = m.GetEquationMgr
    em.LinkToFile = False
    em.FilePath = os.path.join(root, "default.txt")
    em.LinkToFile = True
    em.UpdateValuesFromExternalEquationFile
    have = [em.Equation(i).replace(" ", "") for i in range(em.GetCount)]
    if sum(e.startswith(f'"{RADIUS_EQUATION}"=') for e in have) != 1:
        sys.exit(f"{RADIUS_EQUATION} did not come in from default.txt exactly once")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    root = os.path.dirname(os.path.abspath(__file__))
    part = os.path.join(root, "box.SLDPRT")
    app, err = sg.connect_solidworks()
    if not app:
        sys.exit(err)
    was = app.GetUserPreferenceToggle(INPUT_DIM_ON_CREATE)
    app.SetUserPreferenceToggle(INPUT_DIM_ON_CREATE, False)
    try:
        m, err = sg.open_and_activate_part(app, part)
        if not m:
            sys.exit(err)
        if m.Parameter(f"D7@{SKETCH}") is not None:
            sys.exit(f"{SKETCH} already has the radius dimensions -- the part is rounded")
        relink_and_import(m, root)
        em = m.GetEquationMgr
        print("volume before", m.Extension.CreateMassProperty.Volume * 1e9)
        before = radius_dims(m)

        m.ClearSelection2(True)
        if not m.Extension.SelectByID2(SKETCH, "SKETCH", 0, 0, 0, False, 0, null(), 0):
            sys.exit(f"could not select {SKETCH} -- nothing was changed")
        sm = m.SketchManager
        sm.InsertSketch(True)
        segs = list(sm.ActiveSketch.GetSketchSegments)

        # Corner A (3.15, 2.35): the wall x = 3.15 and the chamfer meeting it.
        wall = find_line(segs, (WALL, HALF_SIDE - LEG), lambda q: close(q, (WALL, 0.0)))
        chamfer = find_line(segs, (WALL, HALF_SIDE - LEG), lambda q: close(q, (WALL - LEG, HALF_SIDE)))
        m.ClearSelection2(True)
        wall.Select4(False, null())
        chamfer.Select4(True, null())
        if sm.CreateFillet(CREATE_RADIUS * MM, 1) is None:    # 1: keep the geometry
            sys.exit("the first corner fillet was not made -- nothing was saved")
        m.ClearSelection2(True)

        # Corner B (2.35, 3.15): the top wall y = 3.15 and the chamfer piece left
        # after corner A, which now ends at the fillet's tangent point.
        segs = list(sm.ActiveSketch.GetSketchSegments)
        top = find_line(segs, (WALL - LEG, HALF_SIDE), lambda q: close(q, (0.0, HALF_SIDE)))
        chamfer = find_line(segs, (WALL - LEG, HALF_SIDE),
                            lambda q: abs(q[0] + q[1] - (WALL + HALF_SIDE - LEG)) < TOL and q[0] > WALL - LEG)
        top.Select4(False, null())
        chamfer.Select4(True, null())
        if sm.CreateFillet(CREATE_RADIUS * MM, 1) is None:
            sys.exit("the second corner fillet was not made -- nothing was saved")
        m.ClearSelection2(True)
        sm.InsertSketch(True)                 # leave the sketch
        m.ClearSelection2(True)

        new = {k: v for k, v in radius_dims(m).items() if k not in before}
        if len(new) != 2 or any(abs(v - CREATE_RADIUS) > 1e-6 for v in new.values()):
            sys.exit(f"expected two new 1 mm radius dimensions, found {new} -- nothing was saved")
        for name in sorted(new, key=lambda n: int(n[1:n.index("@")])):
            em.Add2(-1, f'"{name}"="{RADIUS_EQUATION}"', True)
            print("linked", name, "to", RADIUS_EQUATION)
        m.ForceRebuild3(False)
        print("radius dimensions now", {k: round(v, 6) for k, v in radius_dims(m).items() if k in new})
        print("volume after", m.Extension.CreateMassProperty.Volume * 1e9)
        if not args.no_save:
            err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            warn = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            print("saved", m.Save3(1, err, warn), err.value, warn.value)
    finally:
        app.SetUserPreferenceToggle(INPUT_DIM_ON_CREATE, was)


if __name__ == "__main__":
    main()
