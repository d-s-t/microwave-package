"""
Rounds the four vertical inside corners of the box cavity in
base_parts/box.SLDPRT, and the matching four outer corners of the lid plug in
base_parts/lid_inside.SLDPRT, driven by `cavity_corner_radius` in default.txt.

Run once, on the machine with SolidWorks, with nothing else using it:

    python base_parts/add_cavity_radius.py [--no-save]

Both parts are built from a quarter sketch that the part then mirrors into
four. The cavity is `Cut-Extrude2` through `Sketch2` in the box, and the plug is
`Boss-Extrude1` through `Sketch1` in the lid. Both quarters are 25 x 12.5 mm
(pcb_length / 2 by pcb_side / 2), so the cavity and the plug share an outline at
zero clearance. Each quarter has one sharp corner at (25, 12.5) where its two
long edges meet. A sketch fillet there, linked to the equation, rounds that
corner; the mirror gives all four.

The box is checked first, then the lid, and each is saved only once both pass.
Both parts' equation links are re-pointed at this checkout's default.txt and the
new parameter is imported exactly once. `design/step_generation.link_equations`
repoints the link on every build anyway, so a path saved here is not a problem.

A sketch fillet cannot have a zero radius, so the fillets are made at 1 mm and
then linked, as add_pedestal_radius.py does. `constraints.check_cavity_corners`
refuses a value of 0.
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
RADIUS_EQUATION = "cavity_corner_radius"
CREATE_RADIUS = 1.0        # mm: the radius the fillet is made at, before linking
CORNER = (25.0, 12.5)      # pcb_length / 2, pcb_side / 2: the quarter's sharp corner
TOL = 1e-3                 # mm

# The depth each corner's fillet runs through, from default.txt: the box's
# cavity is cut `pcb_width + 2 copper + lid_width` deep (Cut-Extrude2) and the
# lid plate is `lid_width` thick (Boss-Extrude1).
VALUES = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
BOX_DEPTH = VALUES["pcb_width"] + 2 * VALUES["copper_thickness"] + VALUES["lid_width"]
LID_DEPTH = VALUES["lid_width"]

# (part, sketch holding the quarter outline, depth of the corner, sign of the
# volume change: the cavity gains the corner's material, the plug loses it)
PARTS = [
    ("box.SLDPRT", "Sketch2", BOX_DEPTH, +1),
    ("lid_inside.SLDPRT", "Sketch1", LID_DEPTH, -1),
]
VOLUME_TOL = 1e-3          # mm^3


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


def radius_dims(m, sketch):
    """Names and mm values of the sketch's dimensions D1..D60 that exist now."""
    found = {}
    for i in range(1, 61):
        name = f"D{i}@{sketch}"
        p = m.Parameter(name)
        if p is not None:
            found[name] = p.SystemValue / MM
    return found


def relink_and_import(m, root):
    """Point the equation link at this checkout's default.txt, then bring in
    the new parameter exactly once (unlink and relink, as add_pedestal_radius does)."""
    em = m.GetEquationMgr
    em.LinkToFile = False
    em.FilePath = os.path.join(root, "default.txt")
    em.LinkToFile = True
    em.UpdateValuesFromExternalEquationFile
    have = [em.Equation(i).replace(" ", "") for i in range(em.GetCount)]
    if sum(e.startswith(f'"{RADIUS_EQUATION}"=') for e in have) != 1:
        sys.exit(f"{RADIUS_EQUATION} did not come in from default.txt exactly once")


def already_linked(m):
    """Whether the part already has a dimension linked to the radius."""
    em = m.GetEquationMgr
    return any(RADIUS_EQUATION in em.Equation(i) and "@" in em.Equation(i).split("=")[0]
               for i in range(em.GetCount))


def volume(m):
    return m.Extension.CreateMassProperty.Volume * 1e9


def round_corner(m, root, part_name, sketch, depth, sign):
    """Round the corner of one open part in memory. Exits on any failure; the
    caller closes the documents without saving.

    The part is rebuilt after the relink, before the baseline volume is read.
    A saved part can carry geometry from older equation values, and an unbuilt
    part reports that stale volume, so the fillet's change would be measured
    against the wrong starting point.
    """
    if already_linked(m):
        sys.exit(f"{part_name} already has a dimension linked to {RADIUS_EQUATION} -- "
                 f"the corner is rounded")
    relink_and_import(m, root)
    m.ForceRebuild3(False)
    base = volume(m)
    print(part_name, "volume after rebuild, before the fillet", round(base, 4))
    before = radius_dims(m, sketch)

    m.ClearSelection2(True)
    if not m.Extension.SelectByID2(sketch, "SKETCH", 0, 0, 0, False, 0, null(), 0):
        sys.exit(f"could not select {sketch} in {part_name} -- nothing was changed")
    sm = m.SketchManager
    sm.InsertSketch(True)
    segs = list(sm.ActiveSketch.GetSketchSegments)

    # The sharp corner (25, 12.5): the long edge down the cavity wall x = 25
    # and the long edge across the top z = 12.5.
    wall = find_line(segs, CORNER, lambda q: close(q, (CORNER[0], 0.0)))
    top = find_line(segs, CORNER, lambda q: close(q, (0.0, CORNER[1])))
    m.ClearSelection2(True)
    wall.Select4(False, null())
    top.Select4(True, null())
    if sm.CreateFillet(CREATE_RADIUS * MM, 1) is None:    # 1: keep the geometry
        sys.exit(f"the corner fillet was not made in {part_name} -- nothing was saved")
    m.ClearSelection2(True)
    sm.InsertSketch(True)                 # leave the sketch
    m.ClearSelection2(True)

    new = {k: v for k, v in radius_dims(m, sketch).items() if k not in before}
    if len(new) != 1 or any(abs(v - CREATE_RADIUS) > 1e-6 for v in new.values()):
        sys.exit(f"expected one new {CREATE_RADIUS:g} mm radius dimension in {part_name}, "
                 f"found {new} -- nothing was saved")
    for name in new:
        m.GetEquationMgr.Add2(-1, f'"{name}"="{RADIUS_EQUATION}"', True)
        print(part_name, "linked", name, "to", RADIUS_EQUATION)
    m.ForceRebuild3(False)
    change = volume(m) - base
    # Each corner takes off (1 - pi/4) R^2 of the part's cross-section over the
    # depth it runs through; the cavity gains that, the plug loses it.
    expected = 4 * (1 - math.pi / 4) * CREATE_RADIUS ** 2 * depth
    print(part_name, "volume change", round(change, 4), "expected", round(sign * expected, 4))
    if abs(change - sign * expected) > VOLUME_TOL:
        sys.exit(f"{part_name}: the four corners changed the volume by {change:+.4f} mm3, not "
                 f"{sign * expected:+.4f} mm3 as expected -- nothing was saved")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    root = os.path.dirname(os.path.abspath(__file__))
    app, err = sg.connect_solidworks()
    if not app:
        sys.exit(err)
    was = app.GetUserPreferenceToggle(INPUT_DIM_ON_CREATE)
    app.SetUserPreferenceToggle(INPUT_DIM_ON_CREATE, False)
    opened = []     # (part name, model), so every document can be closed
    ok = False
    try:
        # Both parts are changed in memory first, and saved only when both pass.
        for part_name, sketch, depth, sign in PARTS:
            m, err = sg.open_and_activate_part(app, os.path.join(root, part_name))
            if not m:
                sys.exit(err)
            opened.append((part_name, m))
            round_corner(m, root, part_name, sketch, depth, sign)
        if not args.no_save:
            for part_name, m in opened:
                err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                warn = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                print("saved", part_name, m.Save3(1, err, warn), err.value, warn.value)
        ok = True
    finally:
        # Close every document opened. Unsaved, unless the save above ran; a
        # failed run leaves both files as they were on disk.
        for part_name, _ in opened:
            print("close", part_name, app.CloseDoc(part_name))
        app.SetUserPreferenceToggle(INPUT_DIM_ON_CREATE, was)
        if not ok:
            print("NOTHING SAVED: a check failed, see above")


if __name__ == "__main__":
    main()
