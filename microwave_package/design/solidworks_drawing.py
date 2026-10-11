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

from . import venting

logger = logging.getLogger("microwave_package")

# Sheet: ISO A3, landscape (metres).
SHEET_W, SHEET_H = 0.420, 0.297
MARGIN = 0.010
UNITS_LINEAR = 47          # swUserPreferenceIntegerValue_e.swUnitsLinear, found by probing
UNIT_MM = 0                # swLengthUnit_e.swMM
TOLERANCE_SYMMETRIC = 4    # swTolType_e.swTolSYMMETRIC
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
    search: float = 0.0     # if the edges are not where expected, try points up to `steps` of this (mm) away
    steps: int = 2          # how many steps of `search` either side to try, on each axis
    tolerance: float = 0.0  # symmetric +- tolerance (mm) to print with the value; 0 prints none
    id: str = ""            # what the dimension is, stable between combinations (see assign_ids)


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
    section_axis: str = "h"                     # "h": cut line along u at v=section_at; "v": along v at u=section_at
    flipped: bool = False                       # a section that comes out upside down (v -> -v)
    rotated: bool = False                       # a lengthwise cut: thickness runs across the sheet, the cut's width up it
    extent: Tuple[float, float] = (0.0, 0.0)    # width and height of the part in this view (mm)
    detail_of: Optional[str] = None             # key of the view this is an enlarged detail of
    detail_at: Tuple[float, float] = (0.0, 0.0)  # centre of the circle, in the parent's frame (mm)
    detail_radius: float = 0.0                  # radius of that circle (mm)
    detail_label: str = ""


def assign_ids(views: List["View"]) -> List["View"]:
    """Give every dimension a name that means the same thing in every
    combination: a candidate group is named for the group, anything else
    for its kind, side and tier and how many like it came before in its
    view. Dimensions that exist only in some combinations (the screws', the
    chip recess's, the vents') have a kind/side/tier of their own, so the
    ones that are always there keep their names whichever of those are
    left out. A drawing kept as a template finds its dimensions by these
    names, and tells which no longer apply by their absence."""
    for view in views:
        seen: Dict[str, int] = {}
        for dim in view.dims:
            if dim.group:
                dim.id = f"{view.key}:{dim.group}"
                continue
            stem = f"{dim.kind}.{dim.side}.{dim.tier}"
            dim.id = f"{view.key}:{stem}.{seen.get(stem, 0)}"
            seen[stem] = seen.get(stem, 0) + 1
    return views


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
        sec_a = View("*Top", "sec_a", 0.30, 0.238, sec_scale, section_of="top", section_at=plan.positions[0][1],
                     section_label="A", extent=(total_l, height_mm), flipped=True)
        sec_a.dims += [
            Dim("v", (-15, -h2), (-15, floor_holes - h2), "right", 0, floor_holes),
        ]
        out.append(sec_a)
    sec_b = View("*Top", "sec_b", 0.30, 0.175, sec_scale, section_of="top", section_at=0.0,
                 section_label="B", extent=(total_l, height_mm), flipped=True)
    sec_b.dims += [
        Dim("h", (-body_l / 2, 4.0 - h2), (-body_l / 2 + wall, 4.0 - h2), "above", 0, wall),
        Dim("v", (-body_l / 2 + wall / 2, h2), (-15.0, floor_holes - h2), "left", 0,
            height_mm - floor_holes, search=0.1, steps=3),
    ]
    if g["chip"] and g["pedestal"] > 0:
        sec_b.dims += [
            Dim("v", (0, -h2), (0, floor_pocket - h2), "right", 0, floor_pocket),
            Dim("v", (0, floor_pocket - h2), (-8, floor_holes - h2), "right", 1, g["pedestal"]),
        ]
    out.append(sec_b)
    vents = venting.plan(values)
    if vents:
        vent = vents[0]
        floor_top = venting.floor_top(values)
        groove_mid = floor_top - vent.depth / 2.0       # mid-depth of the groove, from the floor's underside
        sec_v = View("*Top", "sec_v", 0.30, 0.112, sec_scale, section_of="top", section_at=8.0,
                     section_label="E", extent=(total_l, height_mm), flipped=True)
        out.append(sec_v)
        # The groove is a fraction of a millimetre: dimensioned on an enlargement of the section.
        detail = View("*Top", "vent_detail", 0.17, 0.075, 15.0, flipped=True, extent=(2.4, 2.4),
                      detail_of="sec_v", detail_at=(vent.start[0], groove_mid - h2), detail_radius=1.2,
                      detail_label="F")
        detail.dims += [
            # the groove's bottom edge is as long as the groove is wide: one pick
            Dim("h", (vent.width / 2, 0.0), None, "above", 0, vent.width, search=0.1, steps=8),
            # from that edge to the floor's top face, picked to the left of the groove
            Dim("v", (vent.width / 2, 0.0), (-1.0, vent.depth), "left", 0, vent.depth, search=0.1),
        ]
        out.append(detail)
    return assign_ids(out)


def lid_layout(values: Dict[str, float], thickness_mm: float,
               measured: Optional[Dict[str, float]] = None) -> List[View]:
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
    along = View("*Top", "sec_c", 0.30, 0.262, sec_scale, section_of="outer", section_at=0.0,
                 section_label="C", extent=(length, thickness_mm), flipped=True)
    h2 = thickness_mm / 2.0          # v = Y - h2, Y measured from the inner face; this view reads upside down
    along.dims += [Dim("v", (-length / 2 + 2, -h2), (-length / 2 + 2, h2), "left", 0, thickness_mm)]
    out.append(along)
    out += relief_sections(values, thickness_mm, length, channel, inner, outer, side, measured)
    return assign_ids(out)


# The depths the lid's relief steps are cut to. The channel and the chip
# pocket set the line impedance over the die and the launch, so the shop is
# given each depth with a tolerance, on a detail enlarged enough to show it.
RELIEF_TOLERANCE = 0.02       # mm, +-
CHANNEL_DEPTH_DEFAULT = 0.2   # route_clearence
DEEP_DEPTH_CANDIDATES = (0.4, 0.3, 0.5, 0.6)   # the deeper steps: read off the section, not assumed
DETAIL_SCALE = 15.0
DETAIL_RADIUS = 1.2           # mm


def relief_sections(values: Dict[str, float], thickness_mm: float, length: float, channel: float,
                    inner: float, outer: float, side: float,
                    measured: Optional[Dict[str, float]] = None) -> List[View]:
    """Three cross-sections of the inner (relief) face, each with an
    enlarged detail that carries the depth, so a 0.3 mm step is not
    dimensioned from a view where it is a few pixels:

        G  across the channel, mid-way along it: its width and depth
        H  across the channel again, in the last millimetre before the
           connector pocket, where it is cut deeper
        K  across the chip pocket, clear of the channel: the pocket depth

    A section of the inner face keeps its inner face at the bottom (K) or
    at the right-hand edge (G, H: their strip is turned across the sheet);
    a detail's circle is placed in the section's own frame, whose origin is
    the middle of the lid's thickness, with the lateral coordinate first.
    """
    h2 = thickness_mm / 2.0
    measured = measured or {}
    # The depths themselves are measured off the part (`measure_relief`), not
    # worked out from parameters: the deeper steps are not parameter-driven.
    depth = measured.get("channel") or _g(values, "route_clearence", CHANNEL_DEPTH_DEFAULT)
    deep = {"sec_h": measured.get("end"), "sec_k": measured.get("pocket")}
    scale = 2.5
    chip = 7.5                                   # the chip pocket (measured off the drawing, see tests)
    centre_y = 0.3                               # the detail is centred this far into the lid (mm from the inner face)
    # key, label, axis, cut, section on the sheet, detail on the sheet, circle centre (lateral)
    sections = (
        ("sec_g", "G", "v", inner / 2.0, (0.235, 0.212), (0.225, 0.098), 0.0),
        ("sec_h", "H", "v", inner - 0.5, (0.315, 0.212), (0.290, 0.098), 0.0),
        ("sec_k", "K", "h", 2.0, (0.330, 0.158), (0.355, 0.098), -chip / 2.0),
    )
    views: List[View] = []
    for index, (key, label, axis, cut, at, detail_at, lateral) in enumerate(sections):
        view = View("*Bottom", key, at[0], at[1], scale, section_of="inner",
                    section_at=cut, section_axis=axis, section_label=label,
                    extent=(side if axis == "v" else length, thickness_mm), rotated=(axis == "v"))
        views.append(view)
        detail = View("*Bottom", key + "_detail", detail_at[0], detail_at[1], DETAIL_SCALE,
                      extent=(2 * DETAIL_RADIUS, 2 * DETAIL_RADIUS), detail_of=key,
                      detail_at=((h2 - centre_y, lateral) if axis == "v" else (lateral, centre_y - h2)),
                      detail_radius=DETAIL_RADIUS,
                      detail_label=chr(ord("M") + index), rotated=(axis == "v"))
        face = (0.9 if axis == "v" else 1.0) + 0.0   # a point on the inner face, clear of the recess
        if key == "sec_g":
            detail.dims += [
                # the channel floor is a line as long as the channel is wide: one pick
                Dim("v", (0.0, depth - centre_y), None, "left", 0, channel, search=0.2, steps=5),
                Dim("h", (face, -centre_y), (0.0, depth - centre_y), "above", 0, depth, search=0.2, steps=5,
                    tolerance=RELIEF_TOLERANCE),
            ]
        else:
            if axis == "v" and deep.get(key):
                # the channel's width at its deeper floor: found first, it says where this
                # view's content really is, and the depth dimension then starts there
                detail.dims.append(Dim("v", (0.0, deep[key] - centre_y), None, "left", 0, channel,
                                       search=0.1, steps=8))
            for candidate in ((deep[key],) if deep.get(key) else DEEP_DEPTH_CANDIDATES):
                at_floor = (0.0, candidate - centre_y)
                at_face = (face if axis == "v" else -1.0, -centre_y)
                kind = "h" if axis == "v" else "v"
                detail.dims.append(Dim(kind, at_face, at_floor if axis == "v" else (1.0, candidate - centre_y),
                                       "above" if axis == "v" else "left", 0, candidate,
                                       search=0.2 if axis == "v" else 0.1,
                                       steps=5 if axis == "v" else 2, 
                                       tolerance=RELIEF_TOLERANCE, group="deep"))
        views.append(detail)
    return views


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
        self._found_at: Dict[str, Tuple[float, float]] = {}   # view key -> offset (mm) that worked last
        self.placed: Dict[Tuple[str, str], str] = {}          # (view key, dimension id) -> its name in the drawing

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
        cut = view.section_at / 1000.0
        if view.section_axis == "v":
            reach = (parent.extent[1] / 2 + 6) / 1000.0
            line = sketcher.CreateLine(cut, -reach, 0, cut, reach, 0)
        else:
            reach = (parent.extent[0] / 2 + 6) / 1000.0
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

    def add_detail(self, view: View) -> None:
        """An enlarged circle of another view -- how a 0.3 mm groove becomes
        something whose edges can be told apart and dimensioned."""
        parent = self.views[view.detail_of]
        sw_parent = self.sw_views[view.detail_of]
        self.drw.ActivateView(sw_parent.Name)
        sketcher = self.drw.SketchManager
        sketcher.InsertSketch(True)
        # The circle is placed in the section's own sketch frame, whose second axis
        # is the height above the part's middle whichever way the view reads.
        u, v = view.detail_at
        circle = sketcher.CreateCircleByRadius(u / 1000.0, v / 1000.0, 0, view.detail_radius / 1000.0)
        sketcher.InsertSketch(True)
        self.drw.ClearSelection2(True)
        circle.Select4(False, self._none)
        sw = self.drw.CreateDetailViewAt4(view.x, view.y, 0, 0, view.scale, 1.0, view.detail_label,
                                          0, True, False, False, 0)
        if sw is None:
            raise RuntimeError("SolidWorks would not create a detail view")
        self.views[view.key] = view
        self.sw_views[view.key] = sw

    # -- dimensions
    def _pt(self, view: View, point: Tuple[float, float]) -> Tuple[float, float]:
        u, v = point
        if view.rotated:
            return view.x - view.scale * v / 1000.0, view.y + view.scale * u / 1000.0
        if view.flipped:
            v = -v
        return view.x + view.scale * u / 1000.0, view.y + view.scale * v / 1000.0

    def _select(self, x: float, y: float, append: bool) -> bool:
        return bool(self.ext.SelectByID2("", "EDGE", x, y, 0, append, 0, self._none, 0))

    def dimension(self, key: str, dim: Dim) -> bool:
        """Add `dim` to the view `key`, searching around where its edges are
        expected when `dim.search` is set (a detail view's centre is not
        always where it was asked for). False, and nothing left behind, if
        it could not be made to measure what it should."""
        if not dim.search or dim.expected is None:
            return self._dimension_at(key, dim)
        n = dim.steps
        grid = sorted(((du * dim.search, dv * dim.search) for du in range(-n, n + 1) for dv in range(-n, n + 1)),
                      key=lambda t: (abs(t[0]) + abs(t[1]), t))
        # where the last dimension of this view was found goes first
        remembered = self._found_at.get(key)
        offsets = ([remembered] if remembered else []) + grid
        rejected = len(self.rejected)
        for du, dv in offsets:
            moved = Dim(dim.kind, (dim.a[0] + du, dim.a[1] + dv),
                        None if dim.b is None else (dim.b[0] + du, dim.b[1] + dv),
                        dim.side, dim.tier, dim.expected, dim.prefix, dim.suffix,
                        tolerance=dim.tolerance, id=dim.id)
            if self._dimension_at(key, moved, quiet=True):
                self._found_at[key] = (du, dv)
                del self.rejected[rejected:]
                return True
        del self.rejected[rejected:]
        return self._reject(key, dim, "no pair of edges near there measures " + f"{dim.expected:g}")

    def _dimension_at(self, key: str, dim: Dim, quiet: bool = False) -> bool:
        view = self.views[key]
        half_w = view.scale * view.extent[0] / 2000.0
        half_h = view.scale * view.extent[1] / 2000.0
        out = STANDOFF + TEXT_PITCH * dim.tier
        self.drw.ClearSelection2(True)
        xa, ya = self._pt(view, dim.a)
        if not self._select(xa, ya, False):
            return self._reject(key, dim, "first edge not found", quiet)
        xb, yb = xa, ya
        if dim.b is not None:
            xb, yb = self._pt(view, dim.b)
            if not self._select(xb, yb, True):
                return self._reject(key, dim, "second edge not found", quiet)
        mid_x, mid_y = (xa + xb) / 2, (ya + yb) / 2
        if dim.side == "above":
            px, py = mid_x, view.y + half_h + out
        elif dim.side == "below":
            px, py = mid_x, view.y - half_h - out
        elif dim.side == "left":
            px, py = view.x - half_w - out, mid_y
        else:
            px, py = view.x + half_w + out, mid_y
        before = self._count(key)
        if dim.kind == "h":
            shown = self.drw.AddHorizontalDimension2(px, py, 0)
        elif dim.kind == "v":
            shown = self.drw.AddVerticalDimension2(px, py, 0)
        else:
            shown = self.drw.AddDiameterDimension2(px, py, 0)
        self.drw.ClearSelection2(True)
        if shown is None:
            return self._reject(key, dim, "SolidWorks made no dimension", quiet)
        value = shown.GetDimension2(0).Value
        if dim.expected is not None and abs(value - dim.expected) > max(0.01, 0.002 * dim.expected):
            self._delete(shown, key, before)
            return self._reject(key, dim, f"measured {value:.3f}, expected {dim.expected:.3f}", quiet)
        try:
            shown.ShowParenthesis = False
        except Exception:
            pass
        try:
            self.placed[(key, dim.id)] = str(shown.GetDimension2(0).FullName)
        except Exception:
            pass
        if dim.tolerance:
            self._tolerate(shown, dim.tolerance)
        if dim.prefix:
            shown.SetText(1, dim.prefix)
        if dim.suffix:
            shown.SetText(2, dim.suffix)
        return True

    def _count(self, key: str) -> int:
        """How many dimensions the view `key` carries."""
        try:
            found = self.sw_views[key].GetDisplayDimensions
            return 0 if found is None else len(found)
        except Exception:
            return 0

    def _tolerate(self, shown, plus_minus: float) -> None:
        """Print a symmetric +- tolerance beside the dimension. The lid's
        relief depths set the line impedance, so they carry one."""
        try:
            tolerance = shown.GetDimension2(0).Tolerance
            tolerance.Type = TOLERANCE_SYMMETRIC
            # the API takes metres whatever the drawing's units
            tolerance.SetValues(plus_minus / 1000.0, plus_minus / 1000.0)
        except Exception as exc:
            self.log(f"WARNING: could not put a tolerance on a dimension ({exc})")

    def _delete(self, shown, key: str, before: int) -> None:
        """Take back a dimension that measured the wrong thing. Deleting the
        selected annotation is tried first, then undo; the count says
        whether either worked, because a wrong dimension left behind
        also catches the next pick."""
        try:
            shown.GetAnnotation.Select3(False, self._none)
            self.drw.EditDelete()
        except Exception as exc:
            self.log(f"WARNING: could not delete a wrong dimension ({exc})")
        if self._count(key) > before:
            try:
                self.drw.EditUndo2(1)
            except Exception as exc:
                self.log(f"WARNING: could not undo a wrong dimension ({exc})")
        if self._count(key) > before:
            self.log("WARNING: a wrong dimension is still on the drawing")

    def _reject(self, key: str, dim: Dim, why: str, quiet: bool = False) -> bool:
        message = f"{key}: {dim.kind} dimension at {dim.a}: {why}"
        self.rejected.append(message)
        if not quiet:
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

    def set_properties(self, props: Dict[str, str]) -> None:
        """Set the drawing's custom properties (what `$PRP:"Name"` shows)."""
        manager = self.drw.Extension.CustomPropertyManager("")
        for name, value in props.items():
            manager.Add3(name, 30, value, 1)          # swCustomInfoText, replace if present

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


OMITTED_LINES = 3
PROPERTIES = ("RunCombo", "Material", "Date", "Scales") + tuple(f"Omitted{i}" for i in range(1, OMITTED_LINES + 1))


def title_properties(title: str, material: str, scales: str, omitted: Optional[List[str]] = None) -> Dict[str, str]:
    """The text the title block and notes show, as the drawing's custom
    properties: a drawing kept as a template has its notes point at them
    (`$PRP:"Name"`), so filling it for a combination is setting these."""
    lines = list(omitted or [])[:OMITTED_LINES]
    lines += [""] * (OMITTED_LINES - len(lines))
    props = {"RunCombo": title, "Material": f"MATERIAL: {material or 'see order'}",
             "Date": f"{_dt.date.today().isoformat()}  UNITS: mm", "Scales": scales}
    props.update({f"Omitted{i + 1}": line for i, line in enumerate(lines)})
    return props


def _title_block(sheet: "_Sheet", part: str, props: Dict[str, str]) -> None:
    """Frame, general notes and the title block, in sheet coordinates (m).
    What changes from one combination to the next is a property (see
    `title_properties`), not text typed into a note."""
    x1, y1, x2, y2 = MARGIN, MARGIN, SHEET_W - MARGIN, SHEET_H - MARGIN
    block_w, block_h = 0.150, 0.050
    bx, by = x2 - block_w, y1
    sheet.frame(x1, y1, x2, y2)
    sheet.lines([
        (bx, by, bx, by + block_h), (bx, by + block_h, x2, by + block_h),
        (bx, by + 0.034, x2, by + 0.034), (bx, by + 0.018, x2, by + 0.018),
        (bx + 0.085, by, bx + 0.085, by + 0.018),
    ])
    sheet.set_properties(props)
    name = {"box": "BOX", "lid_inside": "LID"}.get(part, part.upper())
    sheet.text(bx + 0.004, by + 0.040, f"{name} - MANUFACTURING DRAWING")
    sheet.text(bx + 0.004, by + 0.024, '$PRP:"RunCombo"')
    sheet.text(bx + 0.004, by + 0.009, '$PRP:"Material"')
    sheet.text(bx + 0.089, by + 0.009, '$PRP:"Date"')
    notes = [
        "NOTES",
        "1. ALL DIMENSIONS IN MILLIMETRES.",
        "2. GENERAL TOLERANCE ISO 2768-m UNLESS STATED.",
        "3. BREAK SHARP EDGES 0.1 TO 0.2.",
        "4. SURFACE ROUGHNESS Ra 3.2 EXCEPT WHERE STATED.",
        '5. VIEW SCALES AS SHOWN ($PRP:"Scales").',
    ]
    for i, line in enumerate(notes):
        sheet.text(x1 + 0.006, y1 + 0.040 - i * 0.0065, line)
    # dimensions a combination does not have are listed here, never printed
    for i in range(OMITTED_LINES):
        sheet.text(x1 + 0.006, y1 + 0.052 + (OMITTED_LINES - 1 - i) * 0.0065, f'$PRP:"Omitted{i + 1}"')


def measure_relief(model, inner: float) -> Dict[str, float]:
    """The depths of the lid's relief steps, measured off the part: a ray
    from below the inner face, straight up through each place, reports the
    height of the first face it meets, which is the depth there.

    The channel and the chip pocket set the line impedance, so the depths
    printed on the drawing are what the part holds, not what a parameter
    says it should. Anything that cannot be measured is left out and the
    layout falls back to its candidates."""
    extension = model.Extension
    selections = model.SelectionManager

    def floor(u: float, v: float) -> Optional[float]:
        model.ClearSelection2(True)
        try:
            # model coordinates in metres: X along the feedline, Y the thickness, Z across
            hit = extension.SelectByRay(u / 1000.0, -0.01, v / 1000.0, 0, 1, 0, 1e-5, 2, False, 0, 0)
            if not hit or selections.GetSelectedObjectCount2(-1) == 0:
                return None
            return round(selections.GetSelectionPoint2(1, -1)[1] * 1000.0, 3)
        except Exception:
            return None
        finally:
            model.ClearSelection2(True)

    found = {
        "channel": floor(inner / 2.0, 0.0),        # mid-way along the channel
        "end": floor(inner - 0.5, 0.0),            # its last millimetre, before the connector pocket
        "pocket": floor(2.0, 2.0),                 # the chip pocket, clear of the channel
    }
    return {k: v for k, v in found.items() if v is not None and v > 0}


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


def layout_for_part(model, part: str, values: Dict[str, float],
                    log: Callable[[str], None] = logger.info) -> List["View"]:
    """The views and dimensions for one combination of `part` (open as
    `model`). Everything that has to be measured off the part is measured
    here, so it must be called before a drawing is open: a ray picked in the
    part makes the part's window the active one, and a drawing sketched in
    meanwhile goes astray."""
    hide_sketches(model)
    box = model.GetPartBox(True)
    height_mm = (box[4] - box[1]) * 1000.0     # along the part's own Y
    if part == "box":
        return box_layout(values, height_mm)
    inner_edge = (_g(values, "pcb_length", 50.0) / 2 - _g(values, "port_space", 6.0)
                  - _g(values, "port_length", 5.1))
    measured = measure_relief(model, inner_edge)
    log(f"lid relief depths measured off the part: {measured}")
    return lid_layout(values, height_mm, measured)


def build_drawing(app, model, part_path: str, part: str, title: str, values: Dict[str, float],
                  log: Callable[[str], None] = logger.info, material: str = "",
                  layout: Optional[List["View"]] = None):
    """Draw `part` (open as `model`, saved at `part_path`) from scratch.

    Returns (drawing, sheet, layout) with the drawing left open for the
    caller to export or save. Dimensions that did not measure what the
    layout expected are in `sheet.rejected`, never on the drawing. `layout`
    may be passed if the caller has made it already (see `layout_for_part`).
    """
    if layout is None:
        layout = layout_for_part(model, part, values, log)
    drawing = app.NewDocument(find_template(), 12, SHEET_W, SHEET_H)
    if drawing is None:
        raise RuntimeError("SolidWorks would not create a drawing")
    sheet = _Sheet(app, drawing, part_path, log)
    try:
        sheet.ext.SetUserPreferenceInteger(UNITS_LINEAR, 0, UNIT_MM)
        # The sheet's own furniture goes in before any view exists: once a
        # view has been worked on it stays the active one, and anything
        # sketched afterwards lands in it, at its scale.
        scales = ", ".join(f"{sc:g}:1" for sc in sorted({v.scale for v in layout}))
        _title_block(sheet, part, title_properties(title, material, scales))
        for view in layout:
            if view.detail_of:
                sheet.add_detail(view)
            elif view.section_of:
                sheet.add_section(view)
            else:
                sheet.add_view(view)
        for view in layout:
            done = set()
            for dim in view.dims:
                if dim.group and dim.group in done:
                    continue
                if sheet.dimension(view.key, dim) and dim.group:
                    done.add(dim.group)
    except Exception:
        try:
            app.CloseDoc(drawing.GetTitle)
        except Exception:
            pass
        raise
    return drawing, sheet, layout


def finish_pdf(app, drawing, sheet, out_pdf: str, preview: str = "") -> None:
    """Write the PDF of an open drawing, check it, and close the drawing."""
    try:
        if preview:
            sheet.export_png(preview)
        sheet.save_pdf(out_pdf)
        if not os.path.isfile(out_pdf) or os.path.getsize(out_pdf) < 1000:
            raise RuntimeError("SolidWorks wrote no PDF")
        check_pdf(out_pdf)
    finally:
        try:
            app.CloseDoc(drawing.GetTitle)
        except Exception:
            pass


def make_pdf(app, model, part_path: str, part: str, out_pdf: str, title: str,
             values: Dict[str, float], log: Callable[[str], None] = logger.info,
             material: str = "", preview: str = "") -> List[str]:
    """Draw `part` from scratch and write the PDF.

    `preview`, if given, is a path to write a PNG of the sheet to (for
    looking at it without a PDF reader).

    Returns the dimensions that were dropped because they did not measure
    what the layout expected (empty when the drawing is complete). Raises
    RuntimeError when no PDF comes out.
    """
    drawing, sheet, _layout = build_drawing(app, model, part_path, part, title, values, log, material)
    finish_pdf(app, drawing, sheet, out_pdf, preview)
    return sheet.rejected
