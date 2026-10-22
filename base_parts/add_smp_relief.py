"""
Relief circles at the eight corners of the two SMP connector windows in the lid
(base_parts/lid_inside.SLDPRT), driven by `smp_relief_radius` and `smp_relief_inset`
in default.txt.

The reliefs are OPTIONAL: smp_relief_on = 0 (off) is the default, and then the part's SMP_Relief
feature is suppressed by equation ("SMP_Relief" = IIF(smp_relief_on > 0.5, 0, 1), set up by
base_parts/add_smp_window.py), so the checks below would find no relief. Set smp_relief_on = 1 in
default.txt before running this script; the script refuses to run with it at 0.

Run once, on the machine with SolidWorks, with nothing else using it:

    python base_parts/add_smp_relief.py --depth 1.5 [--redo] [--no-save]

Why reliefs: the SMP connector housing is 5.0 x 5.0 mm in plan and 4.1 mm high, with
its four plan corners cut back about 0.42 mm at 45 degrees. It sits in the 5.1 x 5.1 mm
window: flush with the outer wall, 0.10 mm from the inner wall, 0.05 mm from the long
walls. Each window corner gets a circle of the tool radius (1.0 mm, the 1 mm end mill),
centred `smp_relief_inset` (0.6 mm) inside the two walls that meet there. The circle
covers the window corner and bites radius - inset (0.4 mm) into the lid, and the housing
corners sit inside it with about 0.39 mm to spare. The window itself stays a sharp square
in the CAD. Inset 0 puts each centre on the window corner: the old dog-bone of PR #87,
which removed 3 pi/4 R^2 per corner and bit 1.0 mm into the lid.

How it is built: the circles go in a NEW sketch on the lid's top face (the face at
z = 1.5 mm in the part), as their own cut, after the window cuts. A circle drawn in the
window's own sketch would overlap its lines, and the through-all cut that makes the window
ignores that (it removed nothing, 2026-10-20). The eight centres are placed by coordinate,
not dimensioned, so changing smp_relief_inset in default.txt needs --redo to rebuild.
Only the radius is linked to its equation.

--redo removes the SMP_Relief cut and the sketch it was cut from (the old reliefs), then
cuts the new ones. Checks, in memory, before the save:
  - the old reliefs remove 8 x depth x 3 pi/4 R^2 (when there are some);
  - the new reliefs remove 8 x depth x A(inset) more, where A is smp_relief_area():
    the lid material one relief removes at one corner. At inset 0 that is the same
    3 pi/4 R^2. The lid volume changes by exactly that much (tolerance 1e-3 mm3).
The part is saved only when every check passes.
"""
import argparse
import math
import os
import sys

import pythoncom
import win32com.client

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from microwave_package import paths  # noqa: E402
from microwave_package.design import constraints as cons  # noqa: E402
from microwave_package.design import equations as eq  # noqa: E402
from microwave_package.design import step_generation as sg  # noqa: E402

INPUT_DIM_ON_CREATE = 10
MM = 0.001
RADIUS_EQUATION = "smp_relief_radius"
CUT_NAME = "SMP_Relief"
VOLUME_TOL = 1e-3
TOP_FACE_POINT = (10.0, 8.0, 1.5)      # mm, a point on the lid's top face (the face at z = 1.5)


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


def remove_old_relief(m, names):
    """Delete the SMP_Relief cut and the sketch it was cut from (the feature before it)."""
    i = names.index(CUT_NAME)
    sketch = names[i - 1] if i > 0 else ""
    if not sketch.startswith("Sketch"):
        sys.exit(f"the feature before {CUT_NAME} is {sketch!r}, not its sketch -- nothing was changed")
    for name, kind in ((CUT_NAME, "BODYFEATURE"), (sketch, "SKETCH")):
        m.ClearSelection2(True)
        if not m.Extension.SelectByID2(name, kind, 0, 0, 0, False, 0, null(), 0):
            sys.exit(f"could not select {name} to delete it -- nothing was changed")
        m.EditDelete()
    m.ClearSelection2(True)
    left = feature_names(m)
    if CUT_NAME in left or sketch in left:
        sys.exit(f"{CUT_NAME} or {sketch} is still in the part -- nothing was saved")
    return sketch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=float, required=True,
                    help="depth in mm of the relief cut from the top face (1.5, the plate through)")
    ap.add_argument("--redo", action="store_true",
                    help="replace the SMP_Relief cut already in the part (removes it and its sketch first)")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    root = os.path.dirname(os.path.abspath(__file__))
    part = os.path.join(root, "lid_inside.SLDPRT")
    values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    if not cons.smp_relief_enabled(values):
        sys.exit("smp_relief_on is 0 in default.txt: the SMP reliefs are off, so there is nothing to "
                 "cut or check -- set smp_relief_on = 1 first -- nothing was changed")
    radius = float(values.get(RADIUS_EQUATION, cons.DEFAULT_SMP_RELIEF_RADIUS))
    inset = float(values.get(cons.SMP_RELIEF_INSET_PARAM, cons.DEFAULT_SMP_RELIEF_INSET))
    depth = args.depth
    centres = cons.smp_relief_centres(values)
    a_corner = cons.smp_relief_area(radius, inset)
    a_old = 3 * math.pi / 4 * radius ** 2
    if abs(cons.smp_relief_area(radius, 0.0) - a_old) > 1e-6:
        sys.exit("smp_relief_area(R, 0) is not 3 pi/4 R^2 -- nothing was changed")
    print(f"radius {radius:g} mm, inset {inset:g} mm, depth {depth:g} mm; centres (mm):",
          [(round(x, 3), round(z, 3)) for x, z in centres])
    print(f"expected per corner: old {a_old:.6f} mm2, new {a_corner:.6f} mm2")

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
        names = feature_names(m)
        had_old = CUT_NAME in names
        if had_old and not args.redo:
            sys.exit(f"{CUT_NAME} is already in the part -- nothing was changed (use --redo to replace it)")
        relink_and_import(m, root)
        old_volume = volume(m)
        print("volume as it is (mm3)", round(old_volume, 4))
        if had_old:
            sketch_old = remove_old_relief(m, names)
            print("removed", CUT_NAME, "and", sketch_old)
        base = volume(m)
        if had_old:
            removed_old = base - old_volume
            expected_old = 8 * depth * a_old
            print("old reliefs removed (mm3)", round(removed_old, 4), "expected", round(expected_old, 4))
            if abs(removed_old - expected_old) > VOLUME_TOL:
                sys.exit(f"the old reliefs removed {removed_old:.4f} mm3, not {expected_old:.4f} -- "
                         f"nothing was saved")
        print("volume with no reliefs (mm3)", round(base, 4))

        before_names = feature_names(m)
        m.ClearSelection2(True)
        px, py, pz = TOP_FACE_POINT
        if not m.Extension.SelectByID2("", "FACE", px * MM, py * MM, pz * MM, False, 0, null(), 0):
            sys.exit("could not select the lid's top face -- nothing was changed")
        sm = m.SketchManager
        sm.InsertSketch(True)
        for cx, cz in centres:
            m.ClearSelection2(True)
            circle = sm.CreateCircle(cx * MM, cz * MM, 0, (cx + radius) * MM, cz * MM, 0)
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

        new = radius_dims(m, sketch)
        if len(new) != len(centres) or any(abs(v - radius) > 1e-6 for v in new.values()):
            sys.exit(f"expected {len(centres)} {radius:g} mm radius dimensions in {sketch}, "
                     f"found {new} -- nothing was changed")
        for name in new:
            m.GetEquationMgr.Add2(-1, f'"{name}"="{RADIUS_EQUATION}"', True)
            print("linked", name, "to", RADIUS_EQUATION)

        m.ClearSelection2(True)
        if not m.Extension.SelectByID2(sketch, "SKETCH", 0, 0, 0, False, 0, null(), 0):
            sys.exit(f"could not select {sketch} for the cut -- nothing was changed")
        feat = m.FeatureManager.FeatureCut4(True, False, False, 0, 0, depth * MM, 0, False, False,
                                            False, False, 0, 0, False, False, False, False, False, True,
                                            True, False, False, False, 0, 0, False, False)
        if feat is None:
            sys.exit("the relief cut was not made -- nothing was changed")
        feat.Name = CUT_NAME
        m.ClearSelection2(True)

        change = volume(m) - base
        expected = -8 * depth * a_corner
        print("volume change from the new reliefs (mm3)", round(change, 4), "expected", round(expected, 4),
              f"(8 x {depth:g} x -A({inset:g}))")
        if abs(change - expected) > VOLUME_TOL:
            sys.exit(f"the reliefs changed the volume by {change:+.4f} mm3, not {expected:+.4f} -- "
                     f"nothing was saved")
        if had_old:
            total = volume(m) - old_volume
            expected_total = 8 * depth * (a_old - a_corner)
            print("volume change from the old lid (mm3)", round(total, 4), "expected", round(expected_total, 4))
            if abs(total - expected_total) > VOLUME_TOL:
                sys.exit(f"the lid volume is {total:+.4f} mm3 from the old lid, not {expected_total:+.4f} -- "
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
