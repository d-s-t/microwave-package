"""
Replaces the octagon pedestal recess in base_parts/box.SLDPRT with two
equation-driven recess shapes, chosen by `pedestal_shape` in default.txt.

Run once, on the machine with SolidWorks, with nothing else using it:

    python base_parts/add_pedestal_shapes.py [--no-save]

What it does to the part:

    Cut-Extrude3      the old octagon (chip_length x chip_side with chip_leg
                      chamfers) stops cutting: its suppression equation
                      becomes 1. Its sketch stays in the tree, harmless.
    Pedestal_Square   a square of `pedestal_size`, centred on the origin, its
                      four corners rounded by sketch fillets whose radii are
                      linked to `pedestal_corner_radius`. Suppressed unless
                      pedestal_shape = 0.
    Pedestal_Circle   a circle of diameter `pedestal_diameter`, centred on the
                      origin. Suppressed unless pedestal_shape = 1.

The square and the circle have their own sizes: `pedestal_size` (the square's
side, 6.2 mm) and `pedestal_diameter` (the circle's diameter, 6.25 mm). A box
built before the split is migrated by base_parts/add_pedestal_diameter.py.

Both cut `pedestal_depth` down from the cavity floor, the face the PCB rests
on, the face base_parts/add_vents.py sketches its grooves on. Both are added
after the part's final mirror (`box`) and after the vents, and each is centred
on the origin, so neither depends on the mirror. Both are suppressed when
pedestal_depth is 0 or the part is a bare board (no_chip), as the vents are.

The part's equation link is re-pointed at THIS checkout's default.txt first
(as add_pedestal_radius.py does), and the two new parameters are imported
exactly once. The part is saved here unless --no-save is given. A check that
fails before the save leaves the file as it was.

design/pedestal.py holds the matching checks and the die-fall rule.
"""
import argparse
import os
import sys

import pythoncom
import win32com.client

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from microwave_package.design import step_generation as sg  # noqa: E402

import add_vents as av  # noqa: E402  (the floor-face sketch and the dimensioned rectangle)

INPUT_DIM_ON_CREATE = 10   # swInputDimValOnCreate: the Modify box that blocks automation
MM = 0.001
SWSKETCH_LINE = 0          # ISketchSegment.GetType value for a line

OCTAGON_CUT = "Cut-Extrude3"
SQUARE_FEATURE = "Pedestal_Square"
CIRCLE_FEATURE = "Pedestal_Circle"
NEW_PARAMETERS = ("pedestal_size", "pedestal_diameter", "pedestal_shape")
RADIUS_EQUATION = "pedestal_corner_radius"
SIZE_EQUATION = "pedestal_size"          # the square's side
DIAMETER_EQUATION = "pedestal_diameter"  # the circle's diameter
CREATE_RADIUS = 1.0        # mm: the radius the fillets are made at, before linking
HALF_SIZE = 6.25 / 2.0     # mm: the real die's half side, the size the recess is drawn at

# The recesses are cut when there is a depth and the part is not a bare board
# (no_chip), the same condition design/venting.py and add_vents.py use.
SQUARE_SUPPRESS = ('IIF ( "pedestal_shape" < 0.5 , IIF ( "pedestal_depth" > 0 , '
                   'IIF ( "no_chip" > 0.5 , 1 , 0 ) , 1 ) , 1 )')
CIRCLE_SUPPRESS = ('IIF ( "pedestal_shape" > 0.5 , IIF ( "pedestal_depth" > 0 , '
                   'IIF ( "no_chip" > 0.5 , 1 , 0 ) , 1 ) , 1 )')


def null():
    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


def line_ends(seg):
    """A sketch line's two ends in mm (sketch coordinates)."""
    a, b = seg.GetStartPoint2, seg.GetEndPoint2
    return (a.X / MM, a.Y / MM), (b.X / MM, b.Y / MM)


def line_on(segs, axis, value):
    """The sketch line lying on x = value or y = value (mm), both ends on it.

    A fillet trims a line's ends but keeps its support, so the square's sides
    stay findable after its corners are rounded.
    """
    k = 1 if axis == "y" else 0
    for seg in segs:
        if seg.GetType != SWSKETCH_LINE:
            continue
        ends = line_ends(seg)
        if all(abs(p[k] - value) < 1e-3 for p in ends):
            return seg
    sys.exit(f"no sketch line on {axis} = {value} mm -- nothing was saved")


def dims_of(m, sketch):
    """Name -> mm value of every dimension of `sketch` that exists now (D1..D60)."""
    found = {}
    for i in range(1, 61):
        name = f"D{i}@{sketch}"
        p = m.Parameter(name)
        if p is not None:
            found[name] = p.SystemValue / MM
    return found


def feature_names(m):
    """Names of every top-level feature in the tree, in order."""
    out = []
    f = m.FirstFeature
    while f is not None:
        out.append(f.Name)
        f = f.GetNextFeature
    return out


def relink_and_import(m, root):
    """Point the equation link at this checkout's default.txt, then bring in the
    two new parameters exactly once (as add_pedestal_radius.py does)."""
    em = m.GetEquationMgr
    em.LinkToFile = False
    em.FilePath = os.path.join(root, "default.txt")
    em.LinkToFile = True
    em.UpdateValuesFromExternalEquationFile
    have = [em.Equation(i).replace(" ", "") for i in range(em.GetCount)]
    for name in NEW_PARAMETERS + (RADIUS_EQUATION,):
        n = sum(e.startswith(f'"{name}"=') for e in have)
        if n != 1:
            sys.exit(f"{name} did not come in from default.txt exactly once (found {n}) "
                     f"-- nothing was saved")


def retire_octagon(m):
    """The octagon stops cutting: its suppression equation becomes 1."""
    em = m.GetEquationMgr
    for i in range(em.GetCount):
        if em.Equation(i).replace(" ", "").startswith(f'"{OCTAGON_CUT}"='):
            em.Equation(i, f'"{OCTAGON_CUT}"= 1')
            print("octagon suppression is now:", em.Equation(i))
            return
    sys.exit(f"no {OCTAGON_CUT} suppression equation in the part -- nothing was saved")


def leave_and_name(m, b, before):
    """Leave the active sketch and return the name of the one sketch feature
    added since `before`."""
    b.sm.InsertSketch(True)
    new = [n for n in feature_names(m) if n not in before and n.startswith("Sketch")]
    if len(new) != 1:
        sys.exit(f"expected one new sketch, found {new} -- nothing was saved")
    return new[0]


def dim_origin_to_line(m, b, line, horizontal):
    """Dimension the origin to one side of the square, its distance across the
    side, linked to pedestal_size / 2. The dimension is on the line, not on a
    corner point, so a fillet that trims a corner does not delete it."""
    m.ClearSelection2(True)
    if not m.Extension.SelectByID2("Point1@Origin", "EXTSKETCHPOINT", 0, 0, 0, False, 0, null(), 0):
        sys.exit("could not select the origin -- nothing was saved")
    line.Select4(True, null())
    add = m.AddHorizontalDimension2 if horizontal else m.AddVerticalDimension2
    disp = add(0.02, 0.02, 0)
    m.ClearSelection2(True)
    if disp is None:
        sys.exit("a side dimension was not made -- nothing was saved")
    b.equation('"%s"=%s' % (disp.GetDimension2(0).FullName, '"%s" / 2' % SIZE_EQUATION))


def build_square(m, b):
    """A square of side pedestal_size centred on the origin, its corners rounded
    by four sketch fillets. Returns the sketch's name."""
    before = feature_names(m)
    b.new_sketch_on_floor()
    h = HALF_SIZE
    pts = [(-h, -h, h, -h), (h, -h, h, h), (h, h, -h, h), (-h, h, -h, -h)]
    sm = b.sm
    sm.AddToDB = True      # no snapping to the face's projected edges
    for a in pts:
        sm.CreateLine(a[0] * MM, a[1] * MM, 0, a[2] * MM, a[3] * MM, 0)
    sm.AddToDB = False
    segs = list(sm.ActiveSketch.GetSketchSegments)
    bottom, right = line_on(segs, "y", -h), line_on(segs, "x", h)
    top, left = line_on(segs, "y", h), line_on(segs, "x", -h)
    for pair, kind in (((bottom, top), "sgHORIZONTAL2D"), ((left, right), "sgVERTICAL2D")):
        m.ClearSelection2(True)
        for seg in pair:
            seg.Select4(True, null())
        m.SketchAddConstraints(kind)
    m.ClearSelection2(True)
    dim_origin_to_line(m, b, left, horizontal=True)
    dim_origin_to_line(m, b, right, horizontal=True)
    dim_origin_to_line(m, b, bottom, horizontal=False)
    dim_origin_to_line(m, b, top, horizontal=False)
    corners = [(h, -h), (h, h), (-h, h), (-h, -h)]
    for cx, cy in corners:
        horizontal = line_on(segs, "y", cy)
        vertical = line_on(segs, "x", cx)
        m.ClearSelection2(True)
        horizontal.Select4(False, null())
        vertical.Select4(True, null())
        if b.sm.CreateFillet(CREATE_RADIUS * MM, 1) is None:
            sys.exit(f"the fillet at ({cx}, {cy}) was not made -- nothing was saved")
        m.ClearSelection2(True)
        segs = list(b.sm.ActiveSketch.GetSketchSegments)
    name = leave_and_name(m, b, before)
    return name


def build_circle(m, b):
    """A circle of diameter pedestal_size centred on the origin. Returns the
    sketch's name."""
    before = feature_names(m)
    b.new_sketch_on_floor()
    circle = b.sm.CreateCircle(0, 0, 0, HALF_SIZE * MM, 0, 0)    # centre, then a point on it
    if circle is None:
        sys.exit("the circle was not made -- nothing was saved")
    # Centre on the origin.
    m.ClearSelection2(True)
    if not m.Extension.SelectByID2("Point1@Origin", "EXTSKETCHPOINT", 0, 0, 0, False, 0, null(), 0):
        sys.exit("could not select the origin -- nothing was saved")
    circle.GetCenterPoint2.Select4(True, null())
    m.SketchAddConstraints("sgCOINCIDENT")
    m.ClearSelection2(True)
    # Diameter linked to pedestal_diameter (the square's side is pedestal_size).
    circle.Select4(False, null())
    disp = m.AddDiameterDimension2(0.02, 0.02, 0)
    if disp is None:
        sys.exit("the diameter dimension was not made -- nothing was saved")
    m.ClearSelection2(True)
    b.equation(f'"{disp.GetDimension2(0).FullName}"="{DIAMETER_EQUATION}"')
    name = leave_and_name(m, b, before)
    return name


def cut_recess(m, b, sketch, feature, suppress):
    """Cut the sketch's closed profile `pedestal_depth` down, name the cut, and
    wire its depth and suppression to equations (as add_vents.Builder.cut does)."""
    m.ClearSelection2(True)
    if not m.Extension.SelectByID2(sketch, "SKETCH", 0, 0, 0, False, 0, null(), 0):
        sys.exit(f"could not select {sketch} for {feature} -- nothing was saved")
    feat = b.fm.FeatureCut4(True, False, False, 0, 0, 0.3 * MM, 0, False, False, False, False,
                            0, 0, False, False, False, False, False, True, True, False, False,
                            False, 0, 0, False, False)
    if feat is None:
        sys.exit(f"the {feature} cut was not made -- nothing was saved")
    feat.Name = feature
    b.equation(f'"D1@{feature}"="pedestal_depth"')
    b.equation(f'"{feature}"={suppress}')
    m.ClearSelection2(True)
    return feat


def check_sketch_has_its_dimensions(m, sketch, expected_radii):
    """Count-assert the square's dimensions: its corner dimensions survived the
    fillets, and each fillet added one radius dimension at CREATE_RADIUS."""
    dims = dims_of(m, sketch)
    radii = [k for k, v in dims.items() if abs(v - CREATE_RADIUS) < 1e-6]
    sides = [k for k, v in dims.items() if abs(v - HALF_SIZE) < 1e-6]
    if len(radii) != expected_radii:
        sys.exit(f"{sketch}: expected {expected_radii} radius dimensions, found {radii} "
                 f"-- nothing was saved")
    if len(sides) != 4:
        sys.exit(f"{sketch}: expected the four side dimensions after the fillets, found "
                 f"{sides} -- nothing was saved")
    return sorted(radii, key=lambda n: int(n[1:n.index("@")]))


def link_radii(m, b, radii):
    for name in radii:
        b.equation(f'"{name}"="{RADIUS_EQUATION}"')
        print("linked", name, "to", RADIUS_EQUATION)


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
    m = None
    try:
        m, err = sg.open_and_activate_part(app, part)
        if not m:
            sys.exit(err)
        if os.path.normcase(m.GetPathName) != os.path.normcase(part):
            sys.exit(f"opened {m.GetPathName}, not {part} -- nothing was changed")
        if SQUARE_FEATURE in feature_names(m):
            sys.exit("the part already has the pedestal shape features -- nothing was changed")
        relink_and_import(m, root)
        print("volume before (mm3)", m.Extension.CreateMassProperty.Volume * 1e9)
        retire_octagon(m)
        b = av.Builder(m)

        # The circle is cut first: it lies inside the square, and a cut made
        # after the square would remove nothing and be refused. Cut in this order,
        # the square then removes its four corners, which is a real change.
        circle = build_circle(m, b)
        cut_recess(m, b, circle, CIRCLE_FEATURE, CIRCLE_SUPPRESS)

        square = build_square(m, b)
        radii = check_sketch_has_its_dimensions(m, square, expected_radii=4)
        link_radii(m, b, radii)
        cut_recess(m, b, square, SQUARE_FEATURE, SQUARE_SUPPRESS)

        if not m.ForceRebuild3(False):
            print("warning: ForceRebuild3 reported failure -- check the tree before saving")
        states = {}
        for name in (SQUARE_FEATURE, CIRCLE_FEATURE):
            f = m.FeatureByName(name)
            if f is None:
                sys.exit(f"{name} is missing after the build -- nothing was saved")
            states[name] = bool(f.IsSuppressed)
        print("suppressed now:", states)
        print("volume after (mm3)", m.Extension.CreateMassProperty.Volume * 1e9)
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
