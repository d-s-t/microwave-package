"""
A dimensioned manufacturing drawing of the box or the lid, made in
SolidWorks from the part itself and exported to PDF.

The drawing is built through the API on an ISO A3 sheet: the part's
views, two section views, then dimensions placed one at a time by
selecting the edges they measure -- `_Sheet.dimension` -- rather than
importing every dimension the model carries. The model's own dimensions
sit on sketches, many of which overlap on a drawing view and none of
which say what a machine shop needs to read (a hole is a diameter, a
count and a depth, not four sketch dimensions). Every dimension added
here is checked after it is made: the value SolidWorks measured must be
the value the layout expected, or the dimension is deleted and the
mismatch logged, so a dimension that landed on the wrong edge never
reaches the PDF.

Where a dimension goes is worked out from the part's numbers (the
equation values of the combination) and the view's scale and position, so
the same layout serves every combination; what depends on a feature the
combination does not have (the pedestal under the chip, the screws) is
left out with it.

Needs SolidWorks and pywin32. Everything about *what* is dimensioned is
in `box_layout` / `lid_layout`, which are plain functions of the values and
are tested without SolidWorks.
"""
from __future__ import annotations

import datetime as _dt
import glob
import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("microwave_package")

# Sheet: ISO A3, landscape (metres).
SHEET_W, SHEET_H = 0.420, 0.297
MARGIN = 0.010
UNITS_LINEAR = 47          # swUserPreferenceIntegerValue_e.swUnitsLinear, found by probing
UNIT_MM = 0                # swLengthUnit_e.swMM
# Lines sketched on the sheet come out twice the size they are asked for
# (a frame 0.01 m in lands 0.02 m in) on the ISO template of SolidWorks 2026,
# with the sheet at 1:1 and no view active; notes and views are placed
# correctly. `check_pdf` is what would notice if that ever changed.
SKETCH_FACTOR = 0.5

SCALES = (10.0, 5.0, 4.0, 3.0, 2.5, 2.0, 1.5, 1.0, 0.5)


# -- the layout, as data -------------------------------------------------

@dataclass
class Dim:
    """One dimension: what it measures, where, and what it must read.

    `a` and `b` are the two points (millimetres, in the view's own frame:
    u right, v up, from the centre of the part's extent) each of which
    picks the edge nearest it. `at` is where the dimension line goes, as
    millimetres from the view's centre on the axis across the measured
    direction, then pushed out by `tier` (each tier is one text height
    further, so stacked dimensions do not meet). `expected` is the value
    (mm) the measurement must give; None means "take whatever it is".
    """
    kind: str                                   # "h", "v", "dia"
    a: Tuple[float, float]
    b: Optional[Tuple[float, float]]
    side: str                                   # "above", "below", "left", "right"
    tier: int = 0
    expected: Optional[float] = None
    prefix: str = ""
    suffix: str = ""
    group: str = ""         # candidates for one unknown value: the first that measures right is kept


@dataclass
class View:
    name: str                                   # SolidWorks standard view, e.g. "*Top"
    key: str                                    # how the layout refers to it
    x: float                                    # sheet position of its centre (m)
    y: float
    scale: float
    dims: List[Dim] = field(default_factory=list)
    section_of: Optional[str] = None            # key of the parent view
    section_at: float = 0.0                     # cut position in the parent (mm, across)
    section_label: str = ""
    flipped: bool = False                       # a section that comes out upside down (v -> -v)
    extent: Tuple[float, float] = (0.0, 0.0)    # width and height of the part in this view (mm)


def pick_scale(width_mm: float, height_mm: float, room_w_mm: float, room_h_mm: float) -> float:
    for scale in SCALES:
        if width_mm * scale <= room_w_mm and height_mm * scale <= room_h_mm:
            return scale
    return SCALES[-1]


def _g(values: Dict[str, float], name: str, default: float) -> float:
    try:
        return float(values.get(name, default))
    except (TypeError, ValueError):
        return default


def box_geometry(values: Dict[str, float]) -> dict:
    from . import screws
    plan = screws.plan(values)
    length = _g(values, "pcb_length", 50.0) + 2 * _g(values, "wall_width", 1.0)
    side = _g(values, "pcb_side", 25.0) + 2 * _g(values, "wall_width", 1.0)
    wing = _g(values, "wing_length", 5.0)
    return {"body_l": length, "body_s": side, "total_l": length + 2 * wing, "wing": wing,
            "wing_t": _g(values, "wing_width", 1.0), "screws": plan,
            "pedestal": _g(values, "pedestal_depth", 0.5), "wall": _g(values, "wall_width", 1.0),
            "floor_holes": (plan.engagement if plan is not None else 0.0)
            or (screws.floor_width(values) + _g(values, "pedestal_depth", 0.5)),
            "floor_pocket": screws.floor_width(values),
            "chip": values.get("no_chip", 0.0) < 0.5}


def box_layout(values: Dict[str, float], height_mm: float) -> List[View]:
    """The box's views and dimensions. `height_mm` is its extent along the
    axis the Front view looks across (the box's height)."""
    g = box_geometry(values)
    total_l, body_l, body_s = g["total_l"], g["body_l"], g["body_s"]
    scale = pick_scale(total_l, body_s, 150, 60)

    sec_scale = pick_scale(total_l, height_mm, 160, 40)
    top = View("*Top", "top", 0.115, 0.215, scale, extent=(total_l, body_s))
    top.dims += [
        Dim("h", (-total_l / 2, 0), (total_l / 2, 0), "above", 1, total_l),
        Dim("h", (-body_l / 2, 0), (body_l / 2, 0), "above", 0, body_l),
        Dim("v", (0, -body_s / 2), (0, body_s / 2), "left", 0, body_s),
        # the cavity the board and lid sit in, and the recess under the chip
        Dim("h", (-(body_l / 2 - g["wall"]), 3.0), (body_l / 2 - g["wall"], 3.0), "below", 2,
            body_l - 2 * g["wall"]),
        Dim("v", (10.0, -(body_s / 2 - g["wall"])), (10.0, body_s / 2 - g["wall"]), "right", 1,
            body_s - 2 * g["wall"]),
    ]
    if g["chip"] and g["pedestal"] > 0:
        top.dims += [
            Dim("h", (-3.0, 0), (3.0, 0), "below", 1, None),
            Dim("v", (0, -3.0), (0, 3.0), "left", 2, None),
        ]
    plan = g["screws"]
    if plan is not None:
        r = plan.tap_diameter / 2
        (ax, ay), (bx, by) = plan.positions[0], plan.positions[3]
        top.dims += [
            Dim("h", (ax + r, ay), (-ax - r, ay), "below", 0, abs(2 * ax)),
            Dim("v", (ax, ay + r), (ax, by - r), "left", 1, abs(2 * ay)),
            Dim("dia", (bx + r, by), None, "right", 3, plan.tap_diameter,
                prefix="4X <MOD-DIAM>", suffix=" THRU (M2 TAP DRILL)"),
        ]
    front = View("*Front", "front", 0.115, 0.125, scale, extent=(total_l, height_mm))
    front.dims += [
        Dim("v", (0, -height_mm / 2), (0, height_mm / 2), "right", 0, height_mm),
        Dim("v", (-(total_l + body_l) / 4, -height_mm / 2), (-(total_l + body_l) / 4, -height_mm / 2 + g["wing_t"]),
            "left", 0, g["wing_t"]),
    ]
    out = [top, front]
    wall = g["wall"]
    floor_holes = g["floor_holes"]
    floor_pocket = g["floor_pocket"]
    h2 = height_mm / 2.0                 # v = Y - h2, Y measured up from the underside of the floor
    if plan is not None:
        sec_a = View("*Top", "sec_a", 0.30, 0.215, sec_scale, section_of="top", section_at=plan.positions[0][1],
                     section_label="A", extent=(total_l, height_mm), flipped=True)
        sec_a.dims += [
            Dim("v", (-15, -h2), (-15, floor_holes - h2), "right", 0, floor_holes),
        ]
        out.append(sec_a)
    sec_b = View("*Top", "sec_b", 0.30, 0.125, sec_scale, section_of="top", section_at=0.0,
                 section_label="B", extent=(total_l, height_mm), flipped=True)
    sec_b.dims += [
        Dim("h", (-body_l / 2, 4.0 - h2), (-body_l / 2 + wall, 4.0 - h2), "above", 0, wall),
        Dim("v", (-body_l / 2 + wall / 2, h2), (-body_l / 2 + 3, floor_holes - h2), "left", 0,
            height_mm - floor_holes),
    ]
    if g["chip"] and g["pedestal"] > 0:
        sec_b.dims += [
            Dim("v", (0, -h2), (0, floor_pocket - h2), "right", 0, floor_pocket),
            Dim("v", (0, floor_pocket - h2), (-8, floor_holes - h2), "right", 1, g["pedestal"]),
        ]
    out.append(sec_b)
    return out


def lid_layout(values: Dict[str, float], thickness_mm: float) -> List[View]:
    """The lid's views and dimensions: the countersunk screw holes on the
    outer face, the relief pockets and the channel between them on the
    inner face, and a section along the feed line for their depths."""
    from . import screws

    length = _g(values, "pcb_length", 50.0)
    side = _g(values, "pcb_side", 25.0)
    plan = screws.plan(values)
    scale = pick_scale(length, side, 150, 62)
    port_len = _g(values, "port_length", 5.1)
    outer = length / 2 - _g(values, "port_space", 6.0)      # far edge of a connector pocket
    inner = outer - port_len                                # its near edge
    channel = _g(values, "route_width", 1.0)

    inner_face = View("*Bottom", "inner", 0.115, 0.215, scale, extent=(length, side))
    inner_face.dims += [
        Dim("h", (-length / 2, 0), (length / 2, 0), "above", 1, length),
        Dim("v", (0, -side / 2), (0, side / 2), "left", 0, side),
        Dim("h", (-inner, port_len / 4), (inner, port_len / 4), "below", 0, 2 * inner),
        Dim("h", (-outer, port_len / 4), (outer, port_len / 4), "below", 1, 2 * outer),
        Dim("h", (inner, 1.5), (outer, 1.5), "above", 0, port_len),
        Dim("v", (outer - port_len / 2, -port_len / 2), (outer - port_len / 2, port_len / 2), "right", 1, port_len),
        Dim("v", (inner / 2, -channel / 2), (inner / 2, channel / 2), "right", 0, channel),
        Dim("h", (-3.5, 1.5), (3.5, 1.5), "above", 0, None),
    ]
    outer_face = View("*Top", "outer", 0.115, 0.125, scale, extent=(length, side))
    if plan is not None:
        (ax, ay), (bx, by) = plan.positions[0], plan.positions[3]
        r = plan.clearance_diameter / 2
        outer_face.dims += [
            Dim("h", (ax, ay + r), (-ax, ay + r), "below", 0, abs(2 * ax)),
            Dim("v", (ax - r, ay), (ax - r, by), "left", 0, abs(2 * ay)),
            Dim("dia", (bx + r, by), None, "right", 4, plan.clearance_diameter,
                prefix="4X <MOD-DIAM>", suffix=" THRU, CSK <MOD-DIAM>4.28 X 90<MOD-DEG>"),
        ]
    out = [inner_face, outer_face]
    sec_scale = pick_scale(length, thickness_mm, 175, 60)
    along = View("*Top", "sec_c", 0.30, 0.215, sec_scale, section_of="outer", section_at=0.0,
                 section_label="C", extent=(length, thickness_mm), flipped=True)
    h2 = thickness_mm / 2.0          # v = Y - h2, Y measured from the inner face; this view reads upside down
    along.dims += [Dim("v", (-length / 2 + 2, -h2), (-length / 2 + 2, h2), "left", 0, thickness_mm)]
    out.append(along)
    return out


# -- SolidWorks ----------------------------------------------------------

def find_template() -> str:
    """The ISO drawing template of the installed SolidWorks."""
    candidates = []
    for root in (r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\data\templates",
                 r"C:\Program Files\SolidWorks Corp\SolidWorks\data\templates"):
        candidates += glob.glob(os.path.join(root, "iso.drwdot"))
    candidates += glob.glob(r"C:\ProgramData\SolidWorks\*\templates\Drawing.DRWDOT")
    if not candidates:
        raise RuntimeError("no SolidWorks drawing template (iso.drwdot) found")
    return candidates[0]


TEXT_PITCH = 0.008        # sheet distance between stacked dimension lines (m)
STANDOFF = 0.011          # first dimension line's distance from the part's outline (m)


class _Sheet:
    """One drawing document: its views, and dimensions placed on them."""

    def __init__(self, app, drawing, part_path: str, log: Callable[[str], None]) -> None:
        import pythoncom
        import win32com.client

        self.app, self.drw, self.part_path, self.log = app, drawing, part_path, log
        self.ext = drawing.Extension
        self._none = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        self.views: Dict[str, View] = {}
        self.sw_views: Dict[str, object] = {}
        self.rejected: List[str] = []

    def _byref(self):
        import pythoncom
        import win32com.client
        return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

    # -- views
    def add_view(self, view: View) -> None:
        sw = self.drw.CreateDrawViewFromModelView3(self.part_path, view.name, view.x, view.y, 0)
        if sw is None:
            raise RuntimeError(f"SolidWorks would not create the {view.name} view")
        sw.UseSheetScale = False
        sw.ScaleDecimal = view.scale
        self.views[view.key] = view
        self.sw_views[view.key] = sw

    def add_section(self, view: View) -> None:
        parent = self.views[view.section_of]
        sw_parent = self.sw_views[view.section_of]
        self.drw.ActivateView(sw_parent.Name)
        sketcher = self.drw.SketchManager
        sketcher.InsertSketch(True)
        reach = (parent.extent[0] / 2 + 6) / 1000.0
        cut = view.section_at / 1000.0
        line = sketcher.CreateLine(-reach, cut, 0, reach, cut, 0)
        sketcher.InsertSketch(True)
        self.drw.ClearSelection2(True)
        line.Select4(False, self._none)
        sw = self.drw.CreateSectionViewAt5(view.x, view.y, 0, view.section_label, 0, self._none, 0.0)
        if sw is None:
            raise RuntimeError("SolidWorks would not create a section view")
        sw.UseSheetScale = False
        sw.ScaleDecimal = view.scale
        # A section is born aligned with the view it cuts, which would
        # put it on top of the next view along; break that and place it.
        sw.RemoveAlignment()
        import pythoncom
        import win32com.client
        sw.Position = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (view.x, view.y))
        self.views[view.key] = view
        self.sw_views[view.key] = sw

    # -- dimensions
    def _pt(self, view: View, point: Tuple[float, float]) -> Tuple[float, float]:
        u, v = point
        if view.flipped:
            v = -v
        return view.x + view.scale * u / 1000.0, view.y + view.scale * v / 1000.0

    def _select(self, x: float, y: float, append: bool) -> bool:
        return bool(self.ext.SelectByID2("", "EDGE", x, y, 0, append, 0, self._none, 0))

    def dimension(self, key: str, dim: Dim) -> bool:
        """Add `dim` to the view `key`. False (and nothing left behind) if
        the edges could not be picked or it measured something else."""
        view = self.views[key]
        half_w = view.scale * view.extent[0] / 2000.0
        half_h = view.scale * view.extent[1] / 2000.0
        out = STANDOFF + TEXT_PITCH * dim.tier
        self.drw.ClearSelection2(True)
        xa, ya = self._pt(view, dim.a)
        if not self._select(xa, ya, False):
            return self._reject(key, dim, "first edge not found")
        xb, yb = xa, ya
        if dim.b is not None:
            xb, yb = self._pt(view, dim.b)
            if not self._select(xb, yb, True):
                return self._reject(key, dim, "second edge not found")
        mid_x, mid_y = (xa + xb) / 2, (ya + yb) / 2
        if dim.side == "above":
            px, py = mid_x, view.y + half_h + out
        elif dim.side == "below":
            px, py = mid_x, view.y - half_h - out
        elif dim.side == "left":
            px, py = view.x - half_w - out, mid_y
        else:
            px, py = view.x + half_w + out, mid_y
        if dim.kind == "h":
            shown = self.drw.AddHorizontalDimension2(px, py, 0)
        elif dim.kind == "v":
            shown = self.drw.AddVerticalDimension2(px, py, 0)
        else:
            shown = self.drw.AddDiameterDimension2(px, py, 0)
        self.drw.ClearSelection2(True)
        if shown is None:
            return self._reject(key, dim, "SolidWorks made no dimension")
        value = shown.GetDimension2(0).Value
        if dim.expected is not None and abs(value - dim.expected) > max(0.01, 0.002 * dim.expected):
            self._delete(shown)
            return self._reject(key, dim, f"measured {value:.3f}, expected {dim.expected:.3f}")
        try:
            shown.ShowParenthesis = False
        except Exception:
            pass
        if dim.prefix:
            shown.SetText(1, dim.prefix)
        if dim.suffix:
            shown.SetText(2, dim.suffix)
        return True

    def _delete(self, shown) -> None:
        try:
            shown.GetAnnotation.Select3(False, self._none)
            self.drw.EditDelete()
        except Exception as exc:
            self.log(f"WARNING: could not remove a wrong dimension ({exc})")

    def _reject(self, key: str, dim: Dim, why: str) -> bool:
        message = f"{key}: {dim.kind} dimension at {dim.a}: {why}"
        self.rejected.append(message)
        self.log("WARNING: dropped " + message)
        return False

    def export_png(self, path: str) -> None:
        self.drw.ViewZoomtofit2()
        self.ext.SaveAs(path, 0, 1, self._none, self._byref(), self._byref())

    # -- sheet furniture
    def text(self, x: float, y: float, text: str) -> None:
        self.drw.ClearSelection2(True)       # a selection would give the note a leader to it
        note = self.drw.InsertNote(text)
        if note is None:
            self.log(f"WARNING: could not place the note {text[:30]!r}")
            return
        note.GetAnnotation.SetPosition2(x, y, 0)

    def _on_sheet(self) -> None:
        """Make the sheet, not the last view worked on, the thing sketches
        go into. A line sketched while a view is still active belongs to
        that view and is drawn at its scale -- the frame came out twice
        the sheet's size."""
        self.drw.ClearSelection2(True)
        self.drw.ActivateSheet(self.drw.GetCurrentSheet.GetName)
        self.drw.EditSheet()

    def frame(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self._on_sheet()
        self.drw.SketchManager.InsertSketch(True)
        f = SKETCH_FACTOR
        self.drw.SketchManager.CreateCornerRectangle(x1 * f, y1 * f, 0, x2 * f, y2 * f, 0)
        self.drw.SketchManager.InsertSketch(True)

    def lines(self, segments) -> None:
        self._on_sheet()
        self.drw.SketchManager.InsertSketch(True)
        for x1, y1, x2, y2 in segments:
            self.drw.SketchManager.CreateLine(x1 * SKETCH_FACTOR, y1 * SKETCH_FACTOR, 0,
                                              x2 * SKETCH_FACTOR, y2 * SKETCH_FACTOR, 0)
        self.drw.SketchManager.InsertSketch(True)

    def save_pdf(self, path: str) -> None:
        if os.path.exists(path):
            os.remove(path)
        self.ext.SaveAs(path, 0, 1, self._none, self._byref(), self._byref())


def hide_sketches(model) -> None:
    """Hide every sketch of the part: a curve-pattern's path sketch would
    otherwise be drawn on every view as a stray ellipse."""
    model.ClearSelection2(True)
    feature = model.FirstFeature
    picked = 0
    while feature is not None:
        if feature.GetTypeName2 == "ProfileFeature":
            feature.Select2(True, 0)
            picked += 1
        feature = feature.GetNextFeature
    if picked:
        model.BlankSketch()
    model.ClearSelection2(True)


def _title_block(sheet: "_Sheet", part: str, title: str, material: str, scale_note: str) -> None:
    """Frame, general notes and the title block, in sheet coordinates (m)."""
    x1, y1, x2, y2 = MARGIN, MARGIN, SHEET_W - MARGIN, SHEET_H - MARGIN
    block_w, block_h = 0.150, 0.050
    bx, by = x2 - block_w, y1
    sheet.frame(x1, y1, x2, y2)
    sheet.lines([
        (bx, by, bx, by + block_h), (bx, by + block_h, x2, by + block_h),
        (bx, by + 0.034, x2, by + 0.034), (bx, by + 0.018, x2, by + 0.018),
        (bx + 0.085, by, bx + 0.085, by + 0.018),
    ])
    name = {"box": "BOX", "lid_inside": "LID"}.get(part, part.upper())
    sheet.text(bx + 0.004, by + 0.040, f"{name} - MANUFACTURING DRAWING")
    sheet.text(bx + 0.004, by + 0.024, title)
    sheet.text(bx + 0.004, by + 0.009, f"MATERIAL: {material or 'see order'}")
    sheet.text(bx + 0.089, by + 0.009, f"{_dt.date.today().isoformat()}  UNITS: mm")
    notes = [
        "NOTES",
        "1. ALL DIMENSIONS IN MILLIMETRES.",
        "2. GENERAL TOLERANCE ISO 2768-m UNLESS STATED.",
        "3. BREAK SHARP EDGES 0.1 TO 0.2.",
        "4. SURFACE ROUGHNESS Ra 3.2 EXCEPT WHERE STATED.",
        f"5. VIEW SCALES AS SHOWN ({scale_note}).",
    ]
    for i, line in enumerate(notes):
        sheet.text(x1 + 0.006, y1 + 0.040 - i * 0.0065, line)


def pdf_extent_mm(path: str) -> Tuple[float, float, float, float]:
    """(min x, min y, max x, max y) in millimetres of every line the PDF
    draws, read from its content streams. Raises ValueError if there are none."""
    import re
    import zlib

    with open(path, "rb") as f:
        data = f.read()
    xs: List[float] = []
    ys: List[float] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        try:
            text = zlib.decompress(match.group(1)).decode("latin-1")
        except Exception:
            continue
        for x, y in re.findall(r"([-\d.]+) ([-\d.]+) [ml]\b", text):
            xs.append(float(x) * 25.4 / 72)
            ys.append(float(y) * 25.4 / 72)
    if not xs:
        raise ValueError("no drawn lines found in the PDF")
    return min(xs), min(ys), max(xs), max(ys)


def check_pdf(path: str) -> None:
    """Raise RuntimeError if anything is drawn outside the A3 page."""
    x0, y0, x1, y1 = pdf_extent_mm(path)
    page_w, page_h = SHEET_W * 1000, SHEET_H * 1000
    if x0 < -0.5 or y0 < -0.5 or x1 > page_w + 0.5 or y1 > page_h + 0.5:
        raise RuntimeError(
            f"the drawing runs off the page: it spans {x0:.0f}..{x1:.0f} x {y0:.0f}..{y1:.0f} mm "
            f"on a {page_w:.0f} x {page_h:.0f} mm sheet")


def make_pdf(app, model, part_path: str, part: str, out_pdf: str, title: str,
             values: Dict[str, float], log: Callable[[str], None] = logger.info,
             material: str = "", preview: str = "") -> List[str]:
    """Draw `part` (open as `model`, saved at `part_path`) and write the PDF.

    `preview`, if given, is a path to write a PNG of the sheet to (for
    looking at it without a PDF reader).

    Returns the dimensions that were dropped because they did not measure
    what the layout expected (empty when the drawing is complete). Raises
    RuntimeError when no PDF comes out.
    """
    hide_sketches(model)
    box = model.GetPartBox(True)
    height_mm = (box[4] - box[1]) * 1000.0     # along the part's own Y
    drawing = app.NewDocument(find_template(), 12, SHEET_W, SHEET_H)
    if drawing is None:
        raise RuntimeError("SolidWorks would not create a drawing")
    sheet = _Sheet(app, drawing, part_path, log)
    try:
        sheet.ext.SetUserPreferenceInteger(UNITS_LINEAR, 0, UNIT_MM)
        layout = box_layout(values, height_mm) if part == "box" else lid_layout(values, height_mm)
        # The sheet's own furniture goes in before any view exists: once a
        # view has been worked on it stays the active one, and anything
        # sketched afterwards lands in it, at its scale.
        scales = sorted({v.scale for v in layout})
        _title_block(sheet, part, title, material, ", ".join(f"{s:g}:1" for s in scales))
        for view in layout:
            (sheet.add_section if view.section_of else sheet.add_view)(view)
        for view in layout:
            done = set()
            for dim in view.dims:
                if dim.group and dim.group in done:
                    continue
                if sheet.dimension(view.key, dim) and dim.group:
                    done.add(dim.group)
        if preview:
            sheet.export_png(preview)
        sheet.save_pdf(out_pdf)
        if not os.path.isfile(out_pdf) or os.path.getsize(out_pdf) < 1000:
            raise RuntimeError("SolidWorks wrote no PDF")
        check_pdf(out_pdf)
        return sheet.rejected
    finally:
        try:
            app.CloseDoc(drawing.GetTitle)
        except Exception:
            pass
