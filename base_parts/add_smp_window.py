"""
Grows the two SMP connector windows in the lid (base_parts/lid_inside.SLDPRT) by
`smp_window_extra` on each side, keeping their centres, and rounds their corners with R
`smp_window_radius`.

Run once, on the machine with SolidWorks, with nothing else using it:

    python base_parts/add_smp_window.py [--no-save]

The window is dimensioned in Sketch3 by equations already (port_space, port_length,
port_width). Three of them change, so the window grows about its centre:

    D1@Sketch3 = port_space - smp_window_extra          outer wall at 25 - 6 + extra
    D2@Sketch3 = port_length + 2 x smp_window_extra     window length (5.1 -> 5.6)
    D3@Sketch3 = port_width + 2 x smp_window_extra      window width (5.1 -> 5.6)

The route channel's end and its deeper end (D5, 1.0 mm) sit on the inner wall, so they move
with it. Four sketch fillets, one per corner, round the window (their radius dimensions
are linked to smp_window_radius, as add_chip_pocket_radius.py does for the chip window).
The -x window is the copy made by the 'lid' pattern, so it takes the same change.

Checks, in memory, one step at a time. The lid volume is in mm3, tolerance 1e-3 mm3:
  1. the window grows with sharp corners: two windows, each (5.6^2 - 5.1^2) x 1.5 deeper,
     less the channel shortened by the extra at each end (route_width x extra x
     route_clearence per connector), so the volume falls by the rest;
  2. the eight R corners (four per window) give back (1 - pi/4) R^2 x 1.5 each.
The part is saved only when every check passes. A failed run leaves the file as it was on
disk.
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

INPUT_DIM_ON_CREATE = 10     # swInputDimValOnCreate: the Modify box that blocks automation
MM = 0.001
SKETCH = "Sketch3"          # the window and channel sketch of the lid's +x half
RADIUS_EQUATION = "smp_window_radius"
CREATE_RADIUS = 1.0          # mm: the radius the fillets are made at, before linking
TOL = 1e-3                   # mm
VOLUME_TOL = 1e-3            # mm^3
OLD_WINDOW_SIDE = 5.1        # mm: port_length = port_width before smp_window_extra
LID_PARAMS = ("smp_window_extra", "smp_window_radius")


def null():
    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


def ends(seg):
    a, b = seg.GetStartPoint2, seg.GetEndPoint2
    return (a.X / MM, a.Y / MM), (b.X / MM, b.Y / MM)


def close(p, q):
    return abs(p[0] - q[0]) < TOL and abs(p[1] - q[1]) < TOL


def walls(segs, xi, xo, hw):
    """The window's four walls (bottom z = -hw, top z = +hw, inner x = xi, outer x = xo) as
    sketch lines. The inner wall is the longest line on x = xi: the route channel's end lies
    on the same line, 1.2 mm long, and is shorter than the wall (3.6 mm or more once the
    corners are rounded)."""
    lines = [s for s in segs if s.GetType == 0]

    def on(seg, axis, value):
        p, q = ends(seg)
        return abs(p[axis] - value) < TOL and abs(q[axis] - value) < TOL

    def length(seg):
        (px, py), (qx, qy) = ends(seg)
        return math.hypot(qx - px, qy - py)

    def only(found, label):
        if len(found) != 1:
            shown = [tuple(round(c, 3) for c in ends(s)[0] + ends(s)[1]) for s in lines][:20]
            sys.exit(f"expected one {label} wall, found {len(found)} (lines {shown}) -- nothing was changed")
        return found[0]

    bottom = only([s for s in lines if on(s, 1, -hw)], "bottom")
    top = only([s for s in lines if on(s, 1, hw)], "top")
    outer = only([s for s in lines if on(s, 0, xo)], "outer")
    at_inner = [s for s in lines if on(s, 0, xi)]
    if not at_inner:
        sys.exit("no inner wall found -- nothing was changed")
    inner = max(at_inner, key=length)
    return bottom, top, inner, outer


def axis_line(segs, xo):
    """The sketch's horizontal line at y = 0 that runs out past the outer wall (the lid's centre
    line, from the lid's end): the axis the window's z walls are made symmetric about."""
    found = [s for s in segs if s.GetType == 0 and all(abs(c[1]) < TOL for c in ends(s))
             and max(c[0] for c in ends(s)) > xo + TOL]
    if len(found) != 1:
        sys.exit(f"expected one centre line at y = 0 past the outer wall, found {len(found)} -- "
                 f"nothing was changed")
    return found[0]


def edit_sketch(m, sm):
    """Put SKETCH into edit mode, once: InsertSketch toggles, so it is entered only when it is
    not already open (a sketch relation leaves the edit)."""
    if sm.ActiveSketch is None:
        m.ClearSelection2(True)
        if not m.Extension.SelectByID2(SKETCH, "SKETCH", 0, 0, 0, False, 0, null(), 0):
            sys.exit(f"could not select {SKETCH} -- nothing was changed")
        sm.InsertSketch(True)
        if sm.ActiveSketch is None:
            sys.exit(f"could not edit {SKETCH} -- nothing was changed")


def radius_dims(m, sketch):
    found = {}
    for i in range(1, 61):
        name = f"D{i}@{sketch}"
        p = m.Parameter(name)
        if p is not None:
            found[name] = p.SystemValue / MM
    return found


def relink_and_import(m, root):
    """Point the equation link at this checkout's default.txt and check that the new
    parameters came in exactly once (as add_chip_pocket_radius.py does)."""
    em = m.GetEquationMgr
    em.LinkToFile = False
    em.FilePath = os.path.join(root, "default.txt")
    em.LinkToFile = True
    em.UpdateValuesFromExternalEquationFile
    have = [em.Equation(i).replace(" ", "") for i in range(em.GetCount)]
    for name in LID_PARAMS:
        if sum(e.startswith(f'"{name}"=') for e in have) != 1:
            sys.exit(f"{name} did not come in from default.txt exactly once -- nothing was changed")


def set_equation(em, name, text):
    """Replace the equation that drives the dimension `name` (e.g. "D1@Sketch3")."""
    for i in range(em.GetCount):
        if em.Equation(i).replace(" ", "").startswith(f'"{name}"='):
            em.Delete(i)
            em.Add2(i, text, True)
            return
    sys.exit(f"no equation drives {name} -- nothing was saved")


def volume(m):
    m.ForceRebuild3(False)
    return m.Extension.CreateMassProperty.Volume * 1e9


def check(label, change, expected):
    print(f"{label}: change {change:+.4f} mm3, expected {expected:+.4f} mm3")
    if abs(change - expected) > VOLUME_TOL:
        sys.exit(f"{label} changed the volume by {change:+.4f} mm3, not {expected:+.4f} -- "
                 f"nothing was saved")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    root = os.path.dirname(os.path.abspath(__file__))
    part = os.path.join(root, "lid_inside.SLDPRT")
    values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    reason = cons.check_smp_window(values)
    if reason:
        sys.exit(reason + " -- nothing was changed")
    extra = float(values.get(cons.SMP_WINDOW_EXTRA_PARAM, cons.DEFAULT_SMP_WINDOW_EXTRA))
    radius = float(values.get(cons.SMP_WINDOW_RADIUS_PARAM, cons.DEFAULT_SMP_WINDOW_RADIUS))
    win = cons.smp_window(values)
    depth = float(values["lid_width"])
    route_width = float(values.get("route_width", 1.2))
    route_depth = float(values.get("route_clearence", 0.3))
    expected_window = (-2 * depth * (win["length"] * win["width"] - OLD_WINDOW_SIDE ** 2)
                       + 2 * route_width * extra * route_depth)
    expected_fillets = 8 * depth * (1 - math.pi / 4) * radius ** 2
    print(f"smp_window_extra {extra:g} mm, smp_window_radius {radius:g} mm; window x "
          f"{win['xi']:.3f} .. {win['xo']:.3f}, z +-{win['hw']:.3f}")
    print(f"expected volume steps (mm3): window {expected_window:+.6f}, corners {expected_fillets:+.6f}, "
          f"total {expected_window + expected_fillets:+.6f}")

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
        em = m.GetEquationMgr
        v0 = volume(m)
        print("volume as it is (mm3)", round(v0, 4))

        # 1. the window grows about its centre; the channel's end and deep end follow the wall
        set_equation(em, "D1@Sketch3", '"D1@Sketch3"="port_space"-"smp_window_extra"')
        set_equation(em, "D2@Sketch3", '"D2@Sketch3"="port_length"+2*"smp_window_extra"')
        set_equation(em, "D3@Sketch3", '"D3@Sketch3"="port_width"+2*"smp_window_extra"')
        v2 = volume(m)
        check("window grown (sharp corners)", v2 - v0, expected_window)

        # 2. the four corners of each window are rounded R
        sm = m.SketchManager
        edit_sketch(m, sm)
        xi, xo, hw = win["xi"], win["xo"], win["hw"]
        # Pin the window's centre in z first. The sketch has no dimension for it, and the
        # fillets moved both z walls by 0.5 mm without this (2026-10-22): the top to +2.3 and
        # the bottom to -3.3. The walls are made symmetric about the sketch's horizontal line.
        segs = list(sm.ActiveSketch.GetSketchSegments)
        bottom, top, _, _ = walls(segs, xi, xo, hw)
        axis = axis_line(segs, xo)
        m.ClearSelection2(True)
        bottom.Select4(False, null())
        top.Select4(True, null())
        axis.Select4(True, null())
        m.SketchAddConstraints("sgSYMMETRIC")
        m.ClearSelection2(True)
        edit_sketch(m, sm)
        before = radius_dims(m, SKETCH)
        for at in ((xi, -hw), (xo, -hw), (xi, hw), (xo, hw)):
            # The walls are found by where they lie, not by their ends: each fillet trims
            # the lines it meets, so their end points move.
            segs = list(sm.ActiveSketch.GetSketchSegments)
            bottom, top, inner, outer = walls(segs, xi, xo, hw)
            horizontal = bottom if at[1] < 0 else top
            vertical = inner if abs(at[0] - xi) < TOL else outer
            m.ClearSelection2(True)
            horizontal.Select4(False, null())
            vertical.Select4(True, null())
            if sm.CreateFillet(CREATE_RADIUS * MM, 1) is None:
                sys.exit(f"the window corner fillet at {at} was not made -- nothing was changed")
            m.ClearSelection2(True)
        if sm.ActiveSketch is not None:
            sm.InsertSketch(True)
        m.ClearSelection2(True)
        corners = 4
        new = {k: v for k, v in radius_dims(m, SKETCH).items() if k not in before}
        if len(new) != 4 or any(abs(v - CREATE_RADIUS) > 1e-6 for v in new.values()):
            sys.exit(f"expected 4 new {CREATE_RADIUS:g} mm radius dimensions, found {new} -- "
                     f"nothing was changed")
        for name in new:
            em.Add2(-1, f'"{name}"="{RADIUS_EQUATION}"', True)
            print("linked", name, "to", RADIUS_EQUATION)
        v3 = volume(m)
        check("corners rounded", v3 - v2, expected_fillets)
        check("total", v3 - v0, expected_window + expected_fillets)

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
