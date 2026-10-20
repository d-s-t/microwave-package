"""
Splits the pedestal recess's size in base_parts/box.SLDPRT, which
base_parts/add_pedestal_shapes.py built with one size for both shapes.

The circle's diameter moves from `pedestal_size` to `pedestal_diameter`
(6.25 mm). The square's four side dimensions keep following `pedestal_size`
(now the square's side, 6.2 mm). The square is smaller than the die, so the
die cannot fall in and rests on the rim (see design/pedestal.py).

Run once, on the machine with SolidWorks, with nothing else using it:

    python base_parts/add_pedestal_diameter.py [--no-save]

Every check runs in memory before the save, and a failed check leaves the file
as it was on disk:

  - exactly one equation sets a dimension to `pedestal_size` on its own. It must
    belong to the sketch just before Pedestal_Circle in the tree, and it is
    re-pointed at `pedestal_diameter`.
  - the four square side dimensions still read `pedestal_size` / 2.
  - the volume changes, by switching the equations in memory and rebuilding
    (vents off unless stated):
        square 6.25 minus square 6.2   = (A(6.25) - A(6.2)) x depth
        circle minus square 6.2        = (A(6.2) - A_circle) x depth
    where A(6.2) = 6.2^2 - (4 - pi) R^2 with R = pedestal_corner_radius,
    A(6.25) the same at 6.25, A_circle = pi (6.25 / 2)^2 and depth =
    pedestal_depth. The baseline is then restored and read back.
  - the vents-on minus vents-off volume, for each shape, is reported (not
    checked): the vent should still cut the same amount.
"""
import argparse
import math
import os
import sys

import pythoncom  # noqa: F401  (the SolidWorks COM calls need it loaded)
import win32com.client  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from microwave_package import paths  # noqa: E402
from microwave_package.design import equations as eq  # noqa: E402
from microwave_package.design import step_generation as sg  # noqa: E402

import add_pedestal_shapes as aps  # noqa: E402

INPUT_DIM_ON_CREATE = 10
MM = 0.001
TOL = 0.01                       # mm3: the tolerance on each volume check
CHECK_VOLUME_EQUATIONS = {"vents_on", "pedestal_size", "pedestal_shape"}


def norm(text):
    return text.replace(" ", "")


def equations(em):
    return [(i, norm(em.Equation(i))) for i in range(em.GetCount)]


def default(name):
    values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    return values[name]


def volume(m):
    m.ForceRebuild3(False)
    return m.Extension.CreateMassProperty.Volume * 1e9


def set_value(em, name, value):
    """Set one parameter's value in memory (the file is not touched)."""
    hits = [i for i, e in equations(em) if e.startswith(f'"{name}"=')]
    if len(hits) != 1:
        sys.exit(f"{name}: {len(hits)} equations found, expected 1 -- nothing was saved")
    em.Equation(hits[0], f'"{name}"= {value}')


def check(label, got, want):
    ok = abs(got - want) <= TOL
    print(f"{label}: {got:.4f} mm3, expected {want:.4f} mm3, residual {got - want:+.4f} "
          f"-- {'ok' if ok else 'FAIL'}")
    if not ok:
        sys.exit(f"{label} is off by {got - want:+.4f} mm3 -- nothing was saved")


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

        names = aps.feature_names(m)
        for need in (aps.SQUARE_FEATURE, aps.CIRCLE_FEATURE):
            if need not in names:
                sys.exit(f"{need} is not in the tree -- run add_pedestal_shapes.py first")
        circle_at = names.index(aps.CIRCLE_FEATURE)
        square_at = names.index(aps.SQUARE_FEATURE)
        circle_sketch = names[circle_at - 1]
        square_sketch = names[square_at - 1]
        if not (circle_sketch.startswith("Sketch") and square_sketch.startswith("Sketch")):
            sys.exit(f"the sketches before the recesses are {circle_sketch}, {square_sketch} -- nothing was changed")
        print("circle sketch", circle_sketch, "square sketch", square_sketch)

        # The relink brings pedestal_diameter in exactly once (and checks the others).
        aps.relink_and_import(m, root)
        em = m.GetEquationMgr

        # 1. The circle's diameter, which is the one dimension set to pedestal_size on its own.
        plain = [(i, e) for i, e in equations(em)
                 if e.endswith(f'="{aps.SIZE_EQUATION}"') and not e.startswith('"pedestal_size"')]
        if len(plain) != 1:
            sys.exit(f"{len(plain)} equations set a dimension to pedestal_size on its own, "
                     f"expected 1: {[e for _, e in plain]} -- nothing was saved")
        index, text = plain[0]
        dim = text.split("=")[0].strip('"')
        # The name is D<n>@<sketch>@<part>, so the sketch is the middle field.
        if dim.split("@")[1] != circle_sketch:
            sys.exit(f"{dim} is not in {circle_sketch}, the circle's sketch -- nothing was saved")
        em.Equation(index, f'"{dim}"="{aps.DIAMETER_EQUATION}"')
        print("circle diameter", dim, "now", f'"{aps.DIAMETER_EQUATION}"')

        # 2. The square's four sides still follow pedestal_size / 2.
        halves = [e for _, e in equations(em) if e.endswith(f'="{aps.SIZE_EQUATION}"/2')]
        if len(halves) != 4:
            sys.exit(f"expected 4 square side equations on pedestal_size / 2, found {len(halves)} "
                     f"-- nothing was saved")
        m.ForceRebuild3(False)
        diameter_now = m.Parameter(dim).SystemValue / MM
        print("circle diameter read back", round(diameter_now, 4), "mm")
        if abs(diameter_now - default("pedestal_diameter")) > 1e-6:
            sys.exit("the circle's diameter does not read pedestal_diameter -- nothing was saved")

        # 3. Volumes, in memory. The baseline is the square at pedestal_size, vents on.
        depth = default("pedestal_depth")
        radius = default("pedestal_corner_radius")
        size = default("pedestal_size")
        diameter = default("pedestal_diameter")
        old_size = 6.25
        pocket = lambda s: s * s - (4 - math.pi) * radius * radius
        circle_pocket = math.pi * (diameter / 2.0) ** 2
        print(f"pocket areas: square {size:g} {pocket(size):.4f}, square {old_size:g} "
              f"{pocket(old_size):.4f}, circle {diameter:g} {circle_pocket:.4f} mm2")

        # The variants are typed in memory. With the link to default.txt on, a rebuild
        # re-reads the file and undoes them, so the link is off for the variants and
        # back on (to this checkout's default.txt) before the save.
        em.LinkToFile = False
        base = volume(m)
        print("baseline: square, vents on", round(base, 4))
        set_value(em, "vents_on", 0)
        sq_off = volume(m)
        set_value(em, "pedestal_size", old_size)
        sq_old_off = volume(m)
        set_value(em, "pedestal_size", size)
        set_value(em, "pedestal_shape", 1)
        circ_off = volume(m)
        set_value(em, "vents_on", 1)
        circ_on = volume(m)
        set_value(em, "pedestal_shape", 0)
        base_again = volume(m)

        # The pedestal vent on its own (screws off), for each shape: vents on minus
        # off. The slot runs wall to wall through the middle of the recess, so it is
        # cut across the pocket's full size for either shape.
        set_value(em, "screws_on", 0)
        pedvent = {}
        for label, which in (("square", 0), ("circle", 1)):
            set_value(em, "pedestal_shape", which)
            set_value(em, "vents_on", 1)
            on = volume(m)
            set_value(em, "vents_on", 0)
            pedvent[label] = volume(m) - on
        set_value(em, "vents_on", default("vents_on"))
        set_value(em, "pedestal_shape", default("pedestal_shape"))
        set_value(em, "screws_on", default("screws_on"))
        print(f"pedestal vent alone (screws off): square {pedvent['square']:.4f} mm3, "
              f"circle {pedvent['circle']:.4f} mm3 removed (reported, not checked)")

        check("square 6.25 minus square 6.2 (vents off)", sq_old_off - sq_off,
              (pocket(old_size) - pocket(size)) * depth * -1.0)
        check("circle minus square 6.2 (vents off)", circ_off - sq_off,
              (pocket(size) - circle_pocket) * depth)
        print(f"vents: square 6.2 on-minus-off {base - sq_off:.4f} mm3; "
              f"circle on-minus-off {circ_on - circ_off:.4f} mm3 (reported, not checked)")
        check("baseline restored (values in memory)", base_again, base)
        # Back on the file: the values must come back as default.txt has them, and the
        # volume must still be the baseline.
        aps.relink_and_import(m, root)
        em = m.GetEquationMgr
        check("baseline restored (linked to default.txt)", volume(m), base)

        states = {}
        for name in (aps.SQUARE_FEATURE, aps.CIRCLE_FEATURE):
            states[name] = bool(m.FeatureByName(name).IsSuppressed)
        print("suppressed:", states)
        if states[aps.SQUARE_FEATURE] or not states[aps.CIRCLE_FEATURE]:
            sys.exit("the square should be on and the circle suppressed at pedestal_shape 0 -- nothing was saved")

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
