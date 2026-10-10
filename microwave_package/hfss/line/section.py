"""
The cross-section the line-impedance check solves, as plain data.

A short straight piece of the board's CPW, exactly as built: the substrate,
the signal trace, the two gaps, the ground pours either side, the ground
under it, the via posts, and the LID sitting on the ground copper with its
relief channel over the trace. Nothing here touches HFSS, so all of it can
be tested -- `build.py` turns it into solids.

Where the numbers come from decides what the check proves. Taken from the
run's copper file (`from_copper`) they are the layout Altium actually drew
-- the trace width of the longest track, the gap where the ground pours
end, the vias that really stand next to the line -- so the solve checks the
BUILT line. Without a copper file (`from_parameters`) they are re-derived
from the equations and the analytic gap, which is the calculation the check
exists to referee, so the result then says so (`source`).

Axes. The line runs along x from 0 to `length`. y is up, from the bottom of
the board's lower copper, and z is across, with the trace centred on 0:
the same up axis as the HFSS model (see copper_geometry.Frame).

The stack, bottom to top, for the board line:

    lower copper   [0, t]            ground plane under the board
    substrate      [t, t + h]        mat_pcb
    upper copper   [t + h, 2t + h]   trace + ground pours, gaps between
    lid            [2t + h, ...]     sits ON the upper copper; the relief
                                     channel (route_width wide, route_clearence
                                     deep) is cut out of its underside

For the chip line the die has no ground under it: it lies over air, the
pedestal's height above the box floor, so the stack is floor / air / die
metal, with the lid's cover over it. The depth of the air and of the cover
there are the least certain numbers in the model (see `from_parameters`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

BOARD = "board"
CHIP = "chip"
WHICH = (BOARD, CHIP)

# Which board's copper in copper.txt holds each line.
COPPER_BOARD_NAME = {BOARD: "package", CHIP: "chip"}

# How far past the line's edge a gap's far side may be searched, and the
# tolerance used when comparing coordinates read from a text file.
_EPS = 1e-6


@dataclass(frozen=True)
class Part:
    """One solid in the section.

    `name` follows the rules `model_builder.MATERIAL_RULES` match on
    ("pcb_substrate", "pcb_copper_top", "pcb_via", "lid_inside", "box"...),
    so the model gets exactly the materials and boundaries the full
    package does. A "box" is `lo` to `hi` in (x, y, z); a "cylinder" stands
    along y at (x, z) = (lo[0], lo[2]) from lo[1] to hi[1].
    """
    name: str
    shape: str                               # "box" | "cylinder"
    lo: Tuple[float, float, float]
    hi: Tuple[float, float, float]
    radius: float = 0.0
    conductor: bool = True

    @property
    def thickness(self) -> float:
        return self.hi[1] - self.lo[1]


@dataclass
class Section:
    which: str
    source: str                              # "copper" | "parameters"
    length: float
    half_width: float
    substrate_height: float
    copper_thickness: float
    trace_width: float
    gap: float
    eps_r: float
    substrate_material: str
    # What sits under the substrate: a ground plane of copper (the board),
    # or `air_below` mm of air and then the box floor (the die).
    ground_below: bool
    air_below: float = 0.0
    # The lid over the line: channel width and depth, and how thick the lid is.
    route_width: float = 0.0
    route_clearence: float = 0.0
    lid_width: float = 1.5
    # Via posts as (x, z, diameter); coordinates in the section's own frame.
    vias: List[Tuple[float, float, float]] = field(default_factory=list)
    # Where in the board the section was taken from, for the report.
    board_x_range: Tuple[float, float] = (0.0, 0.0)
    notes: List[str] = field(default_factory=list)

    # ---- the stack's heights ------------------------------------------------
    @property
    def floor(self) -> float:
        """The bottom of the cross-section (the box floor, or the lower
        copper's underside)."""
        return 0.0

    @property
    def substrate_bottom(self) -> float:
        return self.copper_thickness if self.ground_below else self.air_below

    @property
    def substrate_top(self) -> float:
        return self.substrate_bottom + self.substrate_height

    @property
    def copper_top(self) -> float:
        return self.substrate_top + self.copper_thickness

    @property
    def top(self) -> float:
        return self.copper_top + self.lid_width

    @property
    def height(self) -> float:
        return self.top - self.floor

    # ---- what is wrong with it ------------------------------------------------
    def problems(self) -> List[str]:
        """Why this section cannot be solved as it stands, or [] if it can.

        The same shorts the layout check refuses are checked again here,
        because a section taken from a copper file or an older run's
        parameters never passed that check.
        """
        found = []
        if self.trace_width <= 0 or self.gap <= 0:
            found.append(f"trace {self.trace_width} mm and gap {self.gap} mm must both be positive")
        if self.route_width and self.route_width < self.trace_width + 0.1 - _EPS:
            found.append(
                f"the lid's relief channel ({self.route_width:g} mm) is less than 0.1 mm wider "
                f"than the trace ({self.trace_width:g} mm): the lid would sit on the trace edges"
            )
        if self.trace_width / 2.0 + self.gap >= self.half_width - _EPS:
            found.append(
                f"the section is only {self.half_width:g} mm either side of the trace, which "
                f"the gap ({self.gap:g} mm) already fills -- widen it"
            )
        return found

    # ---- the solids -------------------------------------------------------------
    def parts(self) -> List[Part]:
        """Every solid of the section, bottom to top."""
        hw, L = self.half_width, self.length
        t = self.copper_thickness
        parts: List[Part] = []
        kind = "pcb" if self.which == BOARD else "chip"

        if self.ground_below:
            parts.append(Part(f"{kind}_copper_bottom", "box", (0.0, 0.0, -hw), (L, t, hw)))
        # Without a ground below (the die over its air gap) the section's
        # floor -- the bottom face of the vacuum box -- is the box floor, and
        # HFSS makes the outer faces of the region perfect conductors.
        parts.append(Part(f"{kind}_substrate", "box",
                          (0.0, self.substrate_bottom, -hw), (L, self.substrate_top, hw),
                          conductor=False))

        half_signal = self.trace_width / 2.0
        ground_edge = half_signal + self.gap
        y0, y1 = self.substrate_top, self.copper_top
        parts.append(Part(f"{kind}_copper_top", "box", (0.0, y0, -half_signal), (L, y1, half_signal)))
        parts.append(Part(f"{kind}_copper_top_ground_a", "box", (0.0, y0, ground_edge), (L, y1, hw)))
        parts.append(Part(f"{kind}_copper_top_ground_b", "box", (0.0, y0, -hw), (L, y1, -ground_edge)))

        for index, (x, z, diameter) in enumerate(self.vias, start=1):
            bottom = self.substrate_bottom - (t if self.ground_below else 0.0)
            parts.append(Part(f"{kind}_via_{index}", "cylinder", (x, bottom, z),
                              (x, y1, z), radius=diameter / 2.0))

        # The lid: a slab sitting on the upper copper with the relief channel
        # cut out of its underside. Two boxes either side of the channel and
        # one over it -- no boolean needed, and the channel is air by omission.
        lid_lo, lid_hi = y1, y1 + self.lid_width
        channel_top = y1 + self.route_clearence
        half_channel = self.route_width / 2.0
        if self.route_width > 0 and self.route_clearence > 0:
            parts.append(Part("lid_inside_a", "box", (0.0, lid_lo, half_channel), (L, lid_hi, hw)))
            parts.append(Part("lid_inside_b", "box", (0.0, lid_lo, -hw), (L, lid_hi, -half_channel)))
            parts.append(Part("lid_inside_c", "box", (0.0, channel_top, -half_channel),
                              (L, lid_hi, half_channel)))
        else:
            parts.append(Part("lid_inside", "box", (0.0, lid_lo, -hw), (L, lid_hi, hw)))
        return parts

    def region(self) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
        """The vacuum box the whole section sits in."""
        return (0.0, self.floor, -self.half_width), (self.length, self.top, self.half_width)

    def port_sheet(self, end: int) -> Tuple[float, float, float, float, float]:
        """End `end` (0 = x 0, 1 = x length) as (x, y_lo, y_hi, z_lo, z_hi)."""
        x = 0.0 if end == 0 else self.length
        return x, self.floor, self.top, -self.half_width, self.half_width

    def integration_line(self, end: int) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
        """From the signal trace across one gap to the ground pour, on the
        top surface of the copper, at end `end`: the port's voltage path."""
        x = 0.0 if end == 0 else self.length
        y = (self.substrate_top + self.copper_top) / 2.0
        half_signal = self.trace_width / 2.0
        return (x, y, half_signal), (x, y, half_signal + self.gap)

    def summary(self) -> Dict[str, object]:
        """What the report says about the geometry that was solved."""
        return {
            "line": self.which,
            "geometry_from": self.source,
            "trace_width_mm": self.trace_width,
            "gap_mm": self.gap,
            "substrate": self.substrate_material,
            "eps_r": self.eps_r,
            "substrate_height_mm": self.substrate_height,
            "relief_width_mm": self.route_width,
            "relief_depth_mm": self.route_clearence,
            "section_length_mm": self.length,
            "section_half_width_mm": self.half_width,
            "vias": len(self.vias),
            "ground_below": self.ground_below,
        }


# ---------------------------------------------------------------------------
# Taking the numbers from the layout
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Measured:
    """What `measure_line` found in a board's copper."""
    trace_width: float
    gap: Optional[float]
    y_centre: float
    x_range: Tuple[float, float]          # the track's own extent
    ground_below: bool
    vias: Tuple[Tuple[float, float, float], ...]   # (x, y, diameter) in board coordinates


def _covers(points: Sequence[Tuple[float, float]], x_lo: float, x_hi: float) -> bool:
    xs = [p[0] for p in points]
    return min(xs) <= x_lo + _EPS and max(xs) >= x_hi - _EPS


def measure_line(board, length: float, half_width: float) -> Optional[Measured]:
    """The line of one board as drawn: trace, gap, ground, nearby vias.

    `board` is a `copper_geometry.BoardCopper`. The window is `length` long
    and centred on the middle of the longest straight track on the top
    layer, clipped to that track. Returns None for a board with no such
    track.
    """
    tracks = [t for t in board.tracks
              if t.layer == "top" and abs(t.y1 - t.y2) < _EPS and abs(t.x2 - t.x1) > _EPS]
    if not tracks:
        return None
    track = max(tracks, key=lambda t: abs(t.x2 - t.x1))
    x_lo, x_hi = sorted((track.x1, track.x2))
    centre_x = (x_lo + x_hi) / 2.0
    window = min(length, x_hi - x_lo)
    w_lo, w_hi = centre_x - window / 2.0, centre_x + window / 2.0
    y0 = track.y1
    half_signal = track.width / 2.0

    # The gap: the nearest vertex of a ground pour on either side, among
    # pours that run the whole window.
    above, below = [], []
    for region in board.regions:
        if region.layer != "top" or not _covers(region.points, w_lo, w_hi):
            continue
        for _x, y in region.points:
            if y - y0 > half_signal + _EPS:
                above.append(y - y0 - half_signal)
            elif y0 - y > half_signal + _EPS:
                below.append(y0 - y - half_signal)
    sides = [min(side) for side in (above, below) if side]
    gap = sum(sides) / len(sides) if sides else None

    ground_below = any(
        r.layer == "bottom" and _covers(r.points, w_lo, w_hi)
        and min(p[1] for p in r.points) <= y0 + _EPS and max(p[1] for p in r.points) >= y0 - _EPS
        for r in board.regions
    )

    vias = tuple(
        (v.x, v.y, v.diameter) for v in board.vias
        if w_lo - _EPS <= v.x <= w_hi + _EPS and abs(v.y - y0) <= half_width
    )
    return Measured(track.width, gap, y0, (w_lo, w_hi), ground_below, vias)


def from_copper(board, which: str, values: Dict[str, float], substrate_material: str, eps_r: float,
                length: float, half_width: float, analytic_gap: Optional[float] = None) -> Optional[Section]:
    """The section of one board's line as Altium drew it, or None if its
    copper has no line to measure.

    `analytic_gap` stands in when the pours leave no measurable gap in the
    window; the section then says so in its notes.
    """
    measured = measure_line(board, length, half_width)
    if measured is None:
        return None
    notes = []
    gap = measured.gap
    if gap is None:
        gap = analytic_gap
        notes.append("no ground pour found beside the line in the copper file; the analytic gap is used")
    if gap is None:
        return None
    window = measured.x_range[1] - measured.x_range[0]
    x_start = measured.x_range[0]
    section = _section(
        which, "copper", values, substrate_material, eps_r, window, half_width,
        measured.trace_width, gap,
        ground_below=measured.ground_below if which == BOARD else False,
    )
    section.board_x_range = measured.x_range
    section.vias = [(x - x_start, y - measured.y_centre, d) for x, y, d in measured.vias
                    if abs(y - measured.y_centre) > measured.trace_width / 2.0 + gap + _EPS]
    section.notes = notes
    return section


def from_parameters(which: str, values: Dict[str, float], substrate_material: str, eps_r: float,
                    length: float, half_width: float, gap: float) -> Section:
    """The section re-derived from the equations, with `gap` the analytic gap.

    One row of ground vias a setback beyond each gap, every `via_max_spacing`
    along the line -- the fence as the equations describe it, which is NOT
    verified against a layout. The report says so.
    """
    trace = values["cpw_trace_width"]
    section = _section(which, "parameters", values, substrate_material, eps_r, length, half_width,
                       trace, gap, ground_below=(which == BOARD))
    diameter = values.get("via_diameter", 0.3)
    pitch = max(values.get("via_max_spacing", 0.8), diameter * 1.5)
    z_row = trace / 2.0 + gap + values.get("via_gnd_clearance", 0.15) + diameter / 2.0
    if which == BOARD and z_row + diameter / 2.0 < half_width:
        count = max(int(length // pitch), 1)
        offset = (length - (count - 1) * pitch) / 2.0
        for i in range(count):
            for side in (-1.0, 1.0):
                section.vias.append((offset + i * pitch, side * z_row, diameter))
    section.notes.append(
        "geometry re-derived from the parameters, not read from a layout: the via fence is the one "
        "the equations describe"
    )
    return section


def _section(which, source, values, substrate_material, eps_r, length, half_width,
             trace, gap, ground_below) -> Section:
    if which not in WHICH:
        raise ValueError(f"which line? {which!r} is not one of {WHICH}")
    if which == BOARD:
        height = values.get("pcb_width", 0.381)
        thickness = values.get("copper_thickness", 0.035)
        air_below = 0.0
        route_clearence = values.get("route_clearence", 0.04)
    else:
        height = values.get("chip_width", 0.381)
        thickness = values.get("chip_metal_thickness", 0.0002)
        # The die lies on the pedestal's legs over the box floor.
        air_below = values.get("pedestal_depth", 0.5)
        # The chip's own relief depth, and if the equations leave it at 0
        # the board's: a lid flat on the die's metal would short the line.
        route_clearence = values.get("route_chip_clearence", 0.0) or values.get("route_clearence", 0.04)
    return Section(
        which=which, source=source, length=length, half_width=half_width,
        substrate_height=height, copper_thickness=thickness,
        trace_width=trace, gap=gap, eps_r=eps_r, substrate_material=substrate_material,
        ground_below=ground_below, air_below=air_below,
        route_width=values.get("route_width", 0.5), route_clearence=route_clearence,
        lid_width=values.get("lid_width", 1.5),
    )
