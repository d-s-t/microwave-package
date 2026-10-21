"""
Dog-bone relief at the eight corners of the two SMP pockets in the lid
(base_parts/lid_inside.SLDPRT), driven by `smp_relief_radius` in default.txt.

Run once, on the machine with SolidWorks, with nothing else using it:

    python base_parts/add_smp_relief.py --depth 1.5 [--no-save]

Why relief and not a rounded corner: the connector body is 5.0 x 4.1 mm with
sharp corners and sits in the 5.1 mm pocket. A rounded R1 corner would cut into
it (0.074 mm at best), so each corner of the pocket is over-cut with a circle
of the tool radius, centred on the corner (a dog-bone). The square connector
then fits in the square part of the pocket.

How it is built: the circles go in a NEW sketch on the lid's top face (y =
1.5 mm), as their own cut, after the pocket. A circle drawn in the pocket's own
sketch would overlap the pocket's lines, and the through-all cut that makes the
pocket ignores that (it removed nothing, 2026-10-20). A separate cut over an
existing void is what add_pedestal_shapes.py does too.

The eight centres are the corners of the two pockets: x = +-13.9 and +-19 mm,
z = +-2.55 mm. The set is symmetric under x -> -x and z -> -z, so the sketch's
own axes can be flipped without changing anything; the volume check below is
what proves the centres sit on the corners.

The cut is `--depth` deep (1.5 mm, the plate through: lid_width). The relief
removes (3 pi / 4) R^2 x depth per corner (the circle's part outside the square
corner): eight corners. The check runs in memory and refuses to save unless
the volume change matches. The part is saved only when every check passes.
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

INPUT_DIM_ON_CREATE = 10
MM = 0.001
RADIUS_EQUATION = "smp_relief_radius"
CUT_NAME = "SMP_Relief"
CREATE_RADIUS = 1.0
VOLUME_TOL = 1e-3
TOP_FACE_POINT = (10.0, 8.0, 1.5)      # mm, a point on the lid's top face (y = 1.5)
HALF_WIDTH = 2.55                       # half of the 5.1 mm port, along z
INNER_X, OUTER_X = 13.9, 19.0           # the pocket's inner and outer ends, along x
CENTRES = [(sx * x, sz * HALF_WIDTH) for x in (INNER_X, OUTER_X) for sx in (1, -1) for sz in (1, -1)]


def null():
    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


def feature_names(m):
    out = []
    f = m.FirstFeature
    while f is not None:
        out.append(f.Name)
        f = f.GetNextFeature
    return out


def radius_dims(m, sketch):
    found = {}
    for i in range(1, 61):
        name = f"D{i}@{sketch}"
        p = m.Parameter(name)
        if p is not None:
            found[name] = p.SystemValue / MM
    return found


def relink_and_import(m, root):
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
                    help="depth in mm of the relief cut from the top face (1.5, the plate through)")
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
        if CUT_NAME in feature_names(m):
            sys.exit(f"{CUT_NAME} is already in the part -- nothing was changed")
        relink_and_import(m, root)
        base = volume(m)
        print("volume before the reliefs (mm3)", round(base, 4))

        before_names = feature_names(m)
        m.ClearSelection2(True)
        px, py, pz = TOP_FACE_POINT
        if not m.Extension.SelectByID2("", "FACE", px * MM, py * MM, pz * MM, False, 0, null(), 0):
            sys.exit("could not select the lid's top face -- nothing was changed")
        sm = m.SketchManager
        sm.InsertSketch(True)
        for cx, cz in CENTRES:
            m.ClearSelection2(True)
            circle = sm.CreateCircle(cx * MM, cz * MM, 0, (cx + CREATE_RADIUS) * MM, cz * MM, 0)
            if circle is None:
                sys.exit("a relief circle was not made -- nothing was changed")
            circle.Select4(False, null())
            disp = m.AddRadialDimension2(0.02, 0.02, 0)
            if disp is None:
                sys.exit("a relief radius was not dimensioned -- nothing was changed")
            m.ClearSelection2(True)
        sm.InsertSketch(True)
        m.ClearSelection2(True)
        new_sketches = [n for n in feature_names(m) if n not in before_names and n.startswith("Sketch")]
        if len(new_sketches) != 1:
            sys.exit(f"expected one new sketch, found {new_sketches} -- nothing was changed")
        sketch = new_sketches[0]

        new = {k: v for k, v in radius_dims(m, sketch).items()}
        if len(new) != len(CENTRES) or any(abs(v - CREATE_RADIUS) > 1e-6 for v in new.values()):
            sys.exit(f"expected {len(CENTRES)} {CREATE_RADIUS:g} mm radius dimensions in {sketch}, "
                     f"found {new} -- nothing was changed")
        for name in new:
            m.GetEquationMgr.Add2(-1, f'"{name}"="{RADIUS_EQUATION}"', True)
            print("linked", name, "to", RADIUS_EQUATION)

        m.ClearSelection2(True)
        if not m.Extension.SelectByID2(sketch, "SKETCH", 0, 0, 0, False, 0, null(), 0):
            sys.exit(f"could not select {sketch} for the cut -- nothing was changed")
        feat = m.FeatureManager.FeatureCut4(True, False, False, 0, 0, args.depth * MM, 0, False, False,
                                            False, False, 0, 0, False, False, False, False, False, True,
                                            True, False, False, False, 0, 0, False, False)
        if feat is None:
            sys.exit("the relief cut was not made -- nothing was changed")
        feat.Name = CUT_NAME
        m.ClearSelection2(True)

        change = volume(m) - base
        per_corner = 3 * math.pi / 4 * CREATE_RADIUS ** 2
        expected = len(CENTRES) * per_corner * args.depth
        print("volume change (mm3)", round(change, 4), "expected", round(-expected, 4),
              f"(8 x 3 pi/4 R^2 x {args.depth:g})")
        if abs(change + expected) > VOLUME_TOL:
            sys.exit(f"the reliefs changed the volume by {change:+.4f} mm3, not {-expected:+.4f} -- "
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
