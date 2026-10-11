"""
Adds the vent-groove features to base_parts/box.SLDPRT, equation-driven.

Run once, on the machine with SolidWorks, with nothing else using it:

    python base_parts/add_vents.py [--no-save]

It adds two features after the part's final mirror (`box`), each sketched
on the cavity floor face (the face the PCB rests on) and cut `vent_depth`
down into the floor:

    Vent_Pedestal   one slot across the whole box at x = 0, `vent_width`
                    wide, through the pedestal recess and out of both side
                    faces. Suppressed unless vents_on = 1, pedestal_depth > 0 and no_chip = 0.
    Vent_Screws     four slots, from each tapped hole to the nearest side
                    face. Suppressed unless vents_on = 1 and screws_on = 1.

Every sketch dimension and the cut depth is an equation of default.txt
(vent_width, vent_depth, pcb_length, pcb_side, screw_inset, box_side), so
the grooves follow a sweep. The geometry must match design/venting.py.

The part's equation link is re-pointed at THIS checkout's default.txt
first (step_generation.link_equations), and the part is saved here.
"""
import argparse
import os
import sys
import time

import pythoncom
import win32com.client

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from microwave_package.design import step_generation as sg  # noqa: E402

INPUT_DIM_ON_CREATE = 10   # swInputDimValOnCreate: the Modify box that blocks automation
OUT = '"box_side" / 2 + 0.2'    # past the wall's outer face (venting.OVERLAP)
MM = 0.001
# 1 suppresses the feature. Same condition as design.venting.plan: a recess
# under a die, so not on a bare board (no_chip).
PEDESTAL_SUPPRESS = ('IIF ( "vents_on" > 0.5 , IIF ( "pedestal_depth" > 0 , '
                     'IIF ( "no_chip" > 0.5 , 1 , 0 ) , 1 ) , 1 )')


def null():
    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


class Builder:
    def __init__(self, model):
        self.m = model
        self.sm = model.SketchManager
        self.fm = model.FeatureManager
        self.em = model.GetEquationMgr

    def equation(self, text):
        self.em.Add2(-1, text, True)

    def new_sketch_on_floor(self):
        self.m.ClearSelection2(True)
        # a point on the cavity floor, clear of every hole and the recess
        if not self.m.Extension.SelectByID2("", "FACE", 10 * MM, 3 * MM, 5 * MM, False, 0, null(), 0):
            raise RuntimeError("could not select the cavity floor face")
        self.sm.InsertSketch(True)
        time.sleep(1.0)
        return self.sm.ActiveSketch

    def rectangle(self, x1, y1, x2, y2, eqs):
        """A rectangle corner to corner (mm), its four corner-to-origin
        distances driven by `eqs` = [x1, y1, x2, y2] equations (absolute
        distances of the lower-left and upper-right corners)."""
        sm, m = self.sm, self.m
        pts = [(x1, y1, x2, y1), (x2, y1, x2, y2), (x2, y2, x1, y2), (x1, y2, x1, y1)]
        sm.AddToDB = True      # no snapping to the face's projected edges
        for a in pts:
            sm.CreateLine(a[0] * MM, a[1] * MM, 0, a[2] * MM, a[3] * MM, 0)
        sm.AddToDB = False
        segs = list(sm.ActiveSketch.GetSketchSegments)

        def find(a):
            for s in segs:
                q = (s.GetStartPoint2.X, s.GetStartPoint2.Y, s.GetEndPoint2.X, s.GetEndPoint2.Y)
                if all(abs(q[i] - a[i] * MM) < 1e-7 for i in range(4)):
                    return s
            raise RuntimeError(f"sketch line {a} not found")

        lines = [find(a) for a in pts]
        for pair, kind in (((0, 2), "sgHORIZONTAL2D"), ((1, 3), "sgVERTICAL2D")):
            m.ClearSelection2(True)
            for i in pair:
                lines[i].Select4(True, null())
            m.SketchAddConstraints(kind)
        for corner, (ex, ey) in ((lines[0].GetStartPoint2, eqs[0:2]),
                                 (lines[2].GetStartPoint2, eqs[2:4])):
            for horizontal, eqn in ((True, ex), (False, ey)):
                m.ClearSelection2(True)
                m.Extension.SelectByID2("Point1@Origin", "EXTSKETCHPOINT", 0, 0, 0, False, 0, null(), 0)
                corner.Select4(True, null())
                add = m.AddHorizontalDimension2 if horizontal else m.AddVerticalDimension2
                disp = add(0.02, 0.02, 0)
                self.equation('"%s"=%s' % (disp.GetDimension2(0).FullName, eqn))
        m.ClearSelection2(True)

    def cut(self, sketch_name, feature_name, suppress_equation):
        m = self.m
        self.sm.InsertSketch(True)       # leave the sketch
        m.ClearSelection2(True)
        m.Extension.SelectByID2(sketch_name, "SKETCH", 0, 0, 0, False, 0, null(), 0)
        feat = self.fm.FeatureCut4(True, False, False, 0, 0, 0.3 * MM, 0, False, False, False, False,
                                   0, 0, False, False, False, False, False, True, True, False, False, False, 0, 0, False, False)
        if feat is None:
            raise RuntimeError("the cut was not made")
        feat.Name = feature_name
        self.equation('"D1@%s"="vent_depth"' % feature_name)
        self.equation('"%s"=%s' % (feature_name, suppress_equation))
        return feat


def fix_pedestal_suppression(m, save=True):
    """Rewrite the part's `"Vent_Pedestal"=...` equation as PEDESTAL_SUPPRESS."""
    em = m.GetEquationMgr
    for i in range(em.GetCount):
        if em.Equation(i).replace(" ", "").startswith('"Vent_Pedestal"='):
            em.Equation(i, '"Vent_Pedestal"=' + PEDESTAL_SUPPRESS)
            break
    else:
        sys.exit("no Vent_Pedestal suppression equation in the part")
    m.ForceRebuild3(False)
    print("Vent_Pedestal suppression is now:", em.Equation(i))
    if save:
        err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warn = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        print("saved", m.Save3(1, err, warn), err.value, warn.value)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--fix-pedestal-suppression", action="store_true",
                    help="on a part that already has the vent features, change Vent_Pedestal's "
                         "suppression equation to the current one (adds the no_chip term) and save")
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
        sg.link_equations(m, os.path.join(root, "default.txt"))
        names, f = [], m.FirstFeature
        while f:
            names.append(f.Name)
            f = f.GetNextFeature
        if "Vent_Pedestal" in names:
            if args.fix_pedestal_suppression:
                fix_pedestal_suppression(m)
                return
            sys.exit("box.SLDPRT already has the vent features")
        if args.fix_pedestal_suppression:
            sys.exit("box.SLDPRT has no Vent_Pedestal feature to fix")
        # The part imports the file's variables only when it is linked, so
        # the three new ones are not in it yet. Unlinking and linking again
        # brings them in (appended after the old ones). They must exist
        # before the equations that use them: SolidWorks evaluates the list
        # in order, and a name defined further down reads as 0.
        em = m.GetEquationMgr
        em.LinkToFile = False
        em.FilePath = os.path.join(root, "default.txt")
        em.LinkToFile = True
        em.UpdateValuesFromExternalEquationFile
        have = [em.Equation(i).replace(" ", "") for i in range(em.GetCount)]
        for name in ("vents_on", "vent_width", "vent_depth"):
            if sum(e.startswith('"%s"=' % name) for e in have) != 1:
                sys.exit(f"{name} did not come in from default.txt exactly once")
        print("volume before", m.Extension.CreateMassProperty.Volume * 1e9)
        b = Builder(m)

        sk = b.new_sketch_on_floor()
        name = sk.Name
        b.rectangle(-0.25, -13.7, 0.25, 13.7,
                    ['"vent_width" / 2', OUT, '"vent_width" / 2', OUT])
        b.cut(name, "Vent_Pedestal",
              PEDESTAL_SUPPRESS)
        print("volume after pedestal", m.Extension.CreateMassProperty.Volume * 1e9)

        sk = b.new_sketch_on_floor()
        name = sk.Name
        u = '"pcb_length" / 2 - "screw_inset"'
        v = '"pcb_side" / 2 - "screw_inset"'
        for sx in (1, -1):
            for sy in (1, -1):
                x0, y0 = sx * 21.25, sy * 9.0
                x1, y1 = sx * 21.75, sy * 13.7
                (xa, xb), (ya, yb) = sorted((x0, x1)), sorted((y0, y1))
                # lower-left corner, then upper-right, as absolute distances
                eqs = [f'{u} - "vent_width" / 2', v, f'{u} + "vent_width" / 2', OUT]
                if sx < 0:
                    eqs[0], eqs[2] = eqs[2], eqs[0]
                if sy < 0:
                    eqs[1], eqs[3] = eqs[3], eqs[1]
                b.rectangle(xa, ya, xb, yb, eqs)
        b.cut(name, "Vent_Screws",
              'IIF ( "vents_on" > 0.5 , IIF ( "screws_on" > 0.5 , 0 , 1 ) , 1 )')

        m.ForceRebuild3(False)
        print("volume after screws", m.Extension.CreateMassProperty.Volume * 1e9)
        if not args.no_save:
            err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            warn = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            print("saved", m.Save3(1, err, warn), err.value, warn.value)
    finally:
        app.SetUserPreferenceToggle(INPUT_DIM_ON_CREATE, was)


if __name__ == "__main__":
    main()
