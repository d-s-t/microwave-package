"""
The fabrication drawing for the package board: one A3 sheet a PCB house
can quote from -- the outline and its holes, the layer stack, the drill
table, and the notes a fab asks about (finish, tolerances, minimum
trace/gap).

It is drawn here from the same numbers the board was generated from
(the combination's `dimensions.txt`, the run's materials, and the
`copper.txt` the Altium export left beside it) rather than printed from
Altium. An Altium fab drawing needs an output job and a template that
only a person can set up, and anything Altium prints is only as legible
as that template; this sheet has one thing to get right, which is that
every number on it can be read.

Nothing here knows about Altium, SolidWorks or the web app, so it runs
(and is tested) without any of them.
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional, Tuple

from ..workflow import simple_pdf
from . import cavity, cpw, screws

# Standard drawing scales to pick from, largest first.
_SCALES = (20.0, 10.0, 5.0, 4.0, 2.5, 2.0, 1.0, 0.5)

MIN_TEXT_PT = 9.0          # nothing on the sheet is smaller than this

# A lamination of this kind has copper both sides; the generator draws
# the signal layer on top and a ground plane below, joined by vias.
COPPER_LAYERS = ("Top copper (signal + CPW ground)", "Bottom copper (ground plane)")

FINISH_NOTE = (
    "Surface finish: ENEPIG or electrolytic soft gold (Ni 3-5 um, Au >= 0.8 um) on all exposed copper "
    "if the board is to be wire-bonded; ENIG (Au 0.05-0.1 um) is acceptable for a connector-only board. "
    "Do not use HASL or immersion tin/silver on bond pads."
)


def parse_package_copper(text: str) -> Dict[str, list]:
    """The package board's tracks, vias and pads out of a copper.txt.

    The file holds both boards back to back, each under a `board=` line;
    only the package one is a board that is made.
    """
    out: Dict[str, list] = {"track": [], "via": [], "pad": [], "region": []}
    board = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("board="):
            board = line.split("=", 1)[1]
            continue
        if board != "package":
            continue
        parts = line.split("|")
        kind = parts[0]
        if kind not in out:
            continue
        try:
            if kind == "via":
                out[kind].append(tuple(float(p) for p in parts[1:5]))
            elif kind == "track":
                out[kind].append((parts[1],) + tuple(float(p) for p in parts[2:7]))
            elif kind == "pad":
                out[kind].append((parts[1],) + tuple(float(p) for p in parts[2:7]))
            else:
                coords = [float(p) for p in parts[2].split(",")]
                out[kind].append((parts[1], coords))
        except (ValueError, IndexError):
            continue
    return out


def drill_table(values: Dict[str, float], copper: Dict[str, list]) -> List[dict]:
    """Every distinct hole: its finished diameter, how many, and whether it
    is plated. Vias are plated through; the screw holes are clearance
    holes for the M2 screws and are not plated."""
    rows: List[dict] = []
    vias: Dict[float, int] = {}
    for _x, _y, _dia, hole in copper.get("via", []):
        vias[round(hole, 4)] = vias.get(round(hole, 4), 0) + 1
    for hole, count in sorted(vias.items()):
        rows.append({"diameter": hole, "count": count, "plated": True, "what": "via (plated through)"})
    plan = screws.plan(values)
    if plan is not None:
        rows.append({"diameter": plan.clearance_diameter, "count": len(plan.positions),
                     "plated": False, "what": "screw clearance (M2), not plated"})
    return rows


def stack_rows(values: Dict[str, float], mat_pcb: str) -> List[Tuple[str, str, str]]:
    copper_um = values.get("copper_thickness", 0.035) * 1000.0
    oz = copper_um / 35.0
    return [
        (COPPER_LAYERS[0], f"{copper_um:.0f} um ({oz:.2g} oz)", "copper"),
        (f"Dielectric: {mat_pcb}", f"{values.get('pcb_width', 0.381) * 1000.0:.0f} um", "laminate, no prepreg"),
        (COPPER_LAYERS[1], f"{copper_um:.0f} um ({oz:.2g} oz)", "copper"),
    ]


def build(
    values: Dict[str, float],
    mat_pcb: str,
    copper: Optional[Dict[str, list]],
    run_id: str,
    combo_id: str,
    date: Optional[_dt.date] = None,
) -> simple_pdf.Document:
    """The fabrication drawing as a PDF document (A3 landscape)."""
    copper = copper or {"track": [], "via": [], "pad": [], "region": []}
    date = date or _dt.date.today()
    doc = simple_pdf.Document(f"Fabrication drawing {run_id} / {combo_id}")
    page = doc.add_page(simple_pdf.A3_LANDSCAPE)
    W, H = simple_pdf.A3_LANDSCAPE
    text_mm = MIN_TEXT_PT / simple_pdf.PT_PER_MM

    length = values.get("pcb_length", 50.0)
    side = values.get("pcb_side", 25.0)
    thick = values.get("pcb_width", 0.381)
    plan = screws.plan(values)
    cav = cavity.plan(values)

    # Sheet frame.
    page.line_width(0.5)
    page.rect(8, 8, W - 16, H - 16)

    # --- the plan view -------------------------------------------------
    area_x0, area_y0, area_w, area_h = 20.0, 80.0, 255.0, 175.0
    margin = 24.0       # room round the board for its dimension lines
    scale = next((s for s in _SCALES
                  if length * s <= area_w - 2 * margin and side * s <= area_h - 2 * margin), 0.5)
    cx, cy = area_x0 + area_w / 2.0, area_y0 + area_h / 2.0

    def px(u: float) -> float:
        return cx + u * scale

    def py(v: float) -> float:
        return cy + v * scale

    page.text(area_x0, area_y0 + area_h + 6, "PLAN VIEW (top)", 12, bold=True)
    page.text(area_x0 + 60, area_y0 + area_h + 6, f"scale {scale:g}:1, dimensions in mm", MIN_TEXT_PT)

    # Copper first, in grey, so the outline and dimensions read over it.
    page.gray(0.55)
    page.line_width(0.1)
    for layer, x, y, xs, ys, rot in copper.get("pad", []):
        if layer != "top":
            continue
        if int(round(rot)) % 180 == 90:
            xs, ys = ys, xs
        page.rect(px(x - xs / 2), py(y - ys / 2), xs * scale, ys * scale, fill=True)
    for layer, x1, y1, x2, y2, width in copper.get("track", []):
        if layer != "top":
            continue
        page.line_width(max(width * scale, 0.1))
        page.line(px(x1), py(y1), px(x2), py(y2))
    page.gray(0)

    # Outline.
    page.line_width(0.5)
    page.rect(px(-length / 2), py(-side / 2), length * scale, side * scale)
    if not cav.is_empty:
        page.line_width(0.25)
        page.dash(2, 1)
        page.rect(px(-cav.half_length), py(-cav.half_side), cav.length * scale, cav.side * scale)
        page.dash()
        page.text(px(0), py(cav.half_side) + 1.5,
                  f"{cav.kind}: {cav.length:.2f} x {cav.side:.2f}, depth {cav.depth:.3f}",
                  MIN_TEXT_PT, align="center")

    # Vias (plated), small open circles.
    page.line_width(0.15)
    for x, y, dia, hole in copper.get("via", []):
        page.circle(px(x), py(y), max(hole * scale / 2, 0.3))

    # Screw holes with centre marks.
    if plan is not None:
        page.line_width(0.3)
        for (u, v) in plan.positions:
            r = plan.clearance_diameter * scale / 2
            page.circle(px(u), py(v), r)
            page.line(px(u) - r - 1.5, py(v), px(u) + r + 1.5, py(v))
            page.line(px(u), py(v) - r - 1.5, px(u), py(v) + r + 1.5)

    # Overall dimensions, outside the board.
    page.line_width(0.25)
    top = py(side / 2)
    bottom = py(-side / 2)
    left = px(-length / 2)
    right = px(length / 2)
    y_dim = bottom - 9
    page.line(left, bottom - 1, left, y_dim - 2)
    page.line(right, bottom - 1, right, y_dim - 2)
    page.arrow_dimension(left, y_dim, right, y_dim, f"{length:g}", size=MIN_TEXT_PT + 2)
    x_dim = right + 9
    page.line(right + 1, bottom, x_dim + 2, bottom)
    page.line(right + 1, top, x_dim + 2, top)
    page.arrow_dimension(x_dim, bottom, x_dim, top, f"{side:g}", size=MIN_TEXT_PT + 2, offset=1)
    if plan is not None:
        u, v = plan.positions[3]
        inset = values.get("screw_inset", 3.5)
        y_in = top + 7
        page.line(right, top + 1, right, y_in + 2)
        page.line(px(u), py(v) + plan.clearance_diameter * scale / 2 + 1, px(u), y_in + 2)
        page.arrow_dimension(px(u), y_in, right, y_in, f"{inset:g}", size=MIN_TEXT_PT)
        x_in = left - 7
        bl_u, bl_v = plan.positions[0]
        page.line(left - 1, bottom, x_in - 2, bottom)
        page.line(px(bl_u) - plan.clearance_diameter * scale / 2 - 1, py(bl_v), x_in - 2, py(bl_v))
        page.arrow_dimension(x_in, bottom, x_in, py(bl_v), f"{inset:g}", size=MIN_TEXT_PT)
        page.text(px(u) + plan.clearance_diameter * scale / 2 + 3, py(v) - 3,
                  f"4x Ø{plan.clearance_diameter:g} (screw)", MIN_TEXT_PT)

    # --- tables on the right/below -------------------------------------
    y = area_y0 - 6
    x0 = 20.0
    page.text(x0, y, "LAYER STACK", 12, bold=True)
    y -= 6
    page.line_width(0.25)
    for name, thickness, note in stack_rows(values, mat_pcb):
        page.text(x0, y, name, MIN_TEXT_PT)
        page.text(x0 + 105, y, thickness, MIN_TEXT_PT)
        page.text(x0 + 135, y, note, MIN_TEXT_PT)
        y -= 5
    page.text(x0, y, f"Finished board thickness {thick + 2 * values.get('copper_thickness', 0.035):.3f} mm "
              f"(laminate {thick:.3f} mm + 2 x copper)", MIN_TEXT_PT)

    # Drill table.
    dx = 175.0
    dy = area_y0 - 6
    page.text(dx, dy, "DRILL TABLE", 12, bold=True)
    dy -= 6
    page.text(dx, dy, "finished dia (mm)", MIN_TEXT_PT, bold=True)
    page.text(dx + 40, dy, "qty", MIN_TEXT_PT, bold=True)
    page.text(dx + 55, dy, "plating / use", MIN_TEXT_PT, bold=True)
    dy -= 5
    for row in drill_table(values, copper):
        page.text(dx, dy, f"{row['diameter']:.3f}", MIN_TEXT_PT)
        page.text(dx + 40, dy, str(row["count"]), MIN_TEXT_PT)
        page.text(dx + 55, dy, row["what"], MIN_TEXT_PT)
        dy -= 5

    # Notes.
    trace = values.get("cpw_trace_width", 0.2)
    try:
        gap = cpw.solve_gap_for_z0(trace, thick, cpw.permittivity_of(mat_pcb), 50.0,
                                   values.get("copper_thickness", 0.035))
        gap_note = f"{gap * 1000:.0f} um"
    except (ValueError, KeyError):
        gap = None
        gap_note = "see model"
    notes = [
        "NOTES",
        f"1. Material: {mat_pcb}, {thick:.3f} mm laminate, copper {values.get('copper_thickness', 0.035) * 1000:.0f} um both sides.",
        f"2. Controlled impedance: 50 ohm grounded coplanar waveguide; signal trace {trace * 1000:.0f} um, gap {gap_note} "
        "(minimum trace and gap on the board). Etch tolerance +/- 10 um on trace and gap.",
        "3. " + FINISH_NOTE,
        f"4. Outline {length:g} x {side:g} mm, tolerance +/- 0.05 mm; routed. Hole position tolerance +/- 0.05 mm.",
        "5. Solder mask and legend: as given by the mask and silkscreen layers of the Gerber set.",
        "6. Fabrication files: Gerber RS-274X, Excellon NC drill (plated and non-plated), ODB++, IPC-2581 as supplied.",
        "7. All dimensions in millimetres. Do not scale from this drawing.",
    ]
    ny = area_y0 + area_h - 2
    nx = 20.0
    # Notes live under the title block on the right-hand side of the plan.
    ny = 62.0
    page.text(nx, ny, notes[0], 12, bold=True)
    ny -= 5
    for note in notes[1:]:
        for wrapped in _wrap(note, 235.0, MIN_TEXT_PT):
            page.text(nx, ny, wrapped, MIN_TEXT_PT)
            ny -= 4.4
    # (Notes occupy the strip above the title block; tables sit above it.)

    # --- title block ----------------------------------------------------
    tb_w, tb_h = 130.0, 28.0
    tx, ty = W - 8 - tb_w, 8.0
    page.line_width(0.35)
    page.rect(tx, ty, tb_w, tb_h)
    page.line(tx, ty + tb_h / 2, tx + tb_w, ty + tb_h / 2)
    page.line(tx + 80, ty, tx + 80, ty + tb_h)
    page.text(tx + 3, ty + tb_h - 6, "PACKAGE PCB - FABRICATION DRAWING", 11, bold=True)
    page.text(tx + 3, ty + tb_h / 2 + 3, f"Run {run_id}", MIN_TEXT_PT)
    page.text(tx + 3, ty + 5, f"Combination {combo_id}", MIN_TEXT_PT)
    page.text(tx + 83, ty + tb_h / 2 + 3, f"Date {date.isoformat()}", MIN_TEXT_PT)
    page.text(tx + 83, ty + 5, "Units mm  Sheet 1/1", MIN_TEXT_PT)
    return doc


def _wrap(text: str, max_mm: float, size: float) -> List[str]:
    lines: List[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if simple_pdf.text_width(trial, size) <= max_mm or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
