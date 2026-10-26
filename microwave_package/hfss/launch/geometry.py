"""
The copper of one SMP launch and a few millimetres of the line behind it, as data.

`LaunchStudy` solves ONE connector launch on a short board, cut with a 50 ohm
CPW wave port, so its S11 is the launch alone. The board's copper for that
model is drawn here -- in Python, not by Altium -- so the pad, a taper, the
anti-pad void and the trace width can be swept without touching the Delphi
generator. The baseline (no taper, vendor pad, vendor void) is the same
copper `base_parts/altium/GenerateFromParams.pas` draws round a launch;
`tests/test_launch_geometry.py` compares it, point by point, with a finished
run's `copper.txt`. A geometry that wins the study is ported to the
generator as its own change.

Axes are the board's (u along the line, v across), centred on the short
board: u runs from -H (the board end, where the connector stands) to +H (the
cut, where the CPW port is). The launch is the near one, at -x, exactly as
`model_builder.place_connectors` puts connector 1; its origin is `origin_u`
and the centre pin `pin_u`, 2.9 mm inboard of it.

What is the vendor's, and so not swept here: the ground land (a 5.2 mm
square), its slot and the four ground pads. The void radius is swept, but
only up to the land's half-size: past it the land is no longer one piece and
the plane's outline needs a union this module does not do.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ...design import copper_geometry as cg, smp

# Facets round the void, as in GenerateFromParams.pas (VoidSteps).
VOID_STEPS = 32
# The void may come this close to the land's edge (mm) and no closer: the
# land stays one piece of copper.
LAND_MARGIN = 0.02
# A via may fall this far short of its clearance (mm): the generator's own
# GapTolerance, for the same reason (floating point at exact fits).
VIA_TOLERANCE = 0.001
# How far the via lattice keeps from the cut end, where the port face is.
PORT_MARGIN = 0.4

Point = Tuple[float, float]


@dataclass(frozen=True)
class Launch:
    """Everything the copper of the launch study depends on."""
    trace: float
    gap: float
    pad_width: float               # signal land, across the line
    pad_length: float              # signal land, along the line
    taper_length: float            # 0 = the pad steps straight to the trace
    void_radius: float
    clearance: float               # launch_clearance
    port_space: float
    trace_length: float            # board behind the launch's inner edge
    half_width: float
    bottom_cutout_radius: float = 0.0   # hole in the bottom copper (and a pocket in the floor) under the pad
    via_diameter: float = 0.3
    via_pitch: float = 0.8
    via_clearance: float = 0.15
    via_min_gap: float = 0.2
    # The full board the lattice of vias is anchored on (pcb_length): the
    # generator anchors it on the board's centre.
    full_length: float = 50.0

    # ---- derived ---------------------------------------------------------
    @property
    def gnd_inner(self) -> float:
        """Half the width the coplanar grounds leave: trace/2 + gap."""
        return self.trace / 2.0 + self.gap

    @property
    def half_length(self) -> float:
        """H: the board's half-length. The launch's inner edge stands
        `trace_length` from the cut."""
        reach = self.port_space - smp.OUTER_V + smp.INNER_V
        return (reach + self.trace_length) / 2.0

    @property
    def origin_u(self) -> float:
        """The connector's origin along u (negative: the -x end)."""
        return -smp.launch_origin(self.half_length, self.port_space)

    @property
    def pin_u(self) -> float:
        return self.origin_u + smp.SIGNAL_V

    @property
    def pull_edge(self) -> float:
        return self.pad_width / 2.0 + self.clearance

    @property
    def pull_reach(self) -> float:
        return smp.SIGNAL_V + self.pad_length / 2.0 + self.clearance

    def problems(self) -> List[str]:
        found = []
        land = smp.GROUND_LAND_HALF
        if self.trace <= 0 or self.gap <= 0:
            found.append("the trace and the gap must be positive")
        if self.void_radius <= smp.SLOT_HALF_U:
            found.append(f"the void radius ({self.void_radius:g} mm) must exceed the slot's half-width "
                         f"({smp.SLOT_HALF_U:g} mm)")
        if self.void_radius > land - LAND_MARGIN:
            found.append(
                f"the void radius ({self.void_radius:g} mm) must stay under the vendor land's half-size "
                f"({land:g} mm) by {LAND_MARGIN:g} mm: a bigger void splits the land, which this "
                "geometry does not draw")
        if self.void_radius <= self.gnd_inner:
            found.append(f"the void radius ({self.void_radius:g} mm) must exceed the trace's half-width "
                         f"plus gap ({self.gnd_inner:g} mm)")
        if self.void_radius >= self.half_width:
            found.append("the void does not fit inside the board's half-width")
        if self.half_width < land + 0.3:
            found.append(f"the half-width ({self.half_width:g} mm) leaves under 0.3 mm beyond the land "
                         f"({land:g} mm)")
        if self.pad_width <= 0 or self.pad_length <= 0:
            found.append("the pad needs a positive width and length")
        if self.bottom_cutout_radius < 0 or self.bottom_cutout_radius > self.half_width - 0.3:
            found.append("the bottom cutout's radius must be between 0 and the half-width less 0.3 mm")
        if self.taper_length < 0:
            found.append("the taper cannot be negative")
        if self.trace_length < self.taper_length + 1.0:
            found.append("the board behind the launch must be at least 1 mm longer than the taper")
        return found


def from_values(values, gap: float, trace_length: float = 8.0, half_width: float = 3.5) -> Launch:
    """The study's launch from a combination's equations.

    The launch_* parameters read 0 as "the vendor's", so a combination that
    does not set them is the plain launch of every other run.
    """
    return Launch(
        trace=values["cpw_trace_width"],
        gap=gap,
        pad_width=values.get("launch_pad_width") or 2.0 * smp.SIGNAL_HALF_W,
        pad_length=values.get("launch_pad_length") or 2.0 * smp.SIGNAL_HALF_L,
        taper_length=values.get("launch_taper_length", 0.0),
        void_radius=values.get("launch_void_radius") or smp.VOID_RADIUS,
        clearance=values.get("launch_clearance", 0.2),
        port_space=values.get("port_space", 1.5),
        trace_length=trace_length,
        half_width=half_width,
        bottom_cutout_radius=values.get("launch_bottom_cutout_radius", 0.0),
        via_diameter=values.get("via_diameter", 0.3),
        via_pitch=values.get("via_max_spacing", 0.8),
        via_clearance=values.get("via_gnd_clearance", 0.15),
        via_min_gap=values.get("via_min_spacing", 0.2),
        full_length=values.get("pcb_length", 50.0),
    )


# ---------------------------------------------------------------------------
# The copper
# ---------------------------------------------------------------------------

def _plane(launch: Launch, side: float) -> Tuple[Point, ...]:
    """One top ground plane (side +1 above the line, -1 below), with the
    anti-pad bitten out of its inner edge -- the contour of
    `AddGroundPlane` for the near launch, run from the cut towards the end."""
    H, o = launch.half_length, launch.origin_u
    gi, vr = launch.gnd_inner, launch.void_radius
    slot, land = smp.SLOT_HALF_U, smp.GROUND_LAND_HALF
    pe, pr = launch.pull_edge, launch.pull_reach

    # a step under 10 um is a sliver AEDT cannot mesh: no pull-back then
    has_pull = pe > gi + 0.01 and pe < slot and pr > land and o + pr < H
    pts: List[Point] = [(H, side * gi)]
    if has_pull:
        pts += [(o + pr, side * gi), (o + pr, side * pe), (o + land, side * pe)]
    else:
        pts += [(o + land, side * gi)]
    pts.append((o + land, side * slot))

    x_slot = math.sqrt(vr * vr - slot * slot)
    x_gap = math.sqrt(vr * vr - gi * gi)
    a_slot = math.atan2(slot, x_slot)
    a_gap = math.atan2(gi, -x_gap)
    for i in range(VOID_STEPS + 1):
        angle = a_slot + (a_gap - a_slot) * i / VOID_STEPS
        pts.append((o + vr * math.cos(angle), side * vr * math.sin(angle)))
    hw = launch.half_width
    pts += [(-H, side * gi), (-H, side * hw), (H, side * hw)]
    return tuple(pts)


def _land(launch: Launch) -> Tuple[Point, ...]:
    """The vendor's ground land: a 5.2 mm square round the connector's origin,
    with the void's disc and the slot (open to the board) cut out of it."""
    o = launch.origin_u
    land, slot, vr = smp.GROUND_LAND_HALF, smp.SLOT_HALF_U, launch.void_radius
    x_slot = math.sqrt(vr * vr - slot * slot)
    a = math.atan2(slot, x_slot)
    pts: List[Point] = [(o + land, land), (o - land, land), (o - land, -land), (o + land, -land),
                        (o + land, -slot), (o + x_slot, -slot)]
    # Round the outboard side of the disc, from -a down through 180 degrees to +a.
    for i in range(VOID_STEPS + 1):
        angle = -a - (2.0 * math.pi - 2.0 * a) * i / VOID_STEPS
        pts.append((o + vr * math.cos(angle), vr * math.sin(angle)))
    pts += [(o + x_slot, slot), (o + land, slot)]
    return tuple(pts)


def _rect_obstacles(launch: Launch) -> List[Tuple[float, float, float, float]]:
    """(x0, x1, y0, y1) of the copper a via keeps clear of: the land, the
    signal pad and the four ground pads."""
    o, land = launch.origin_u, smp.GROUND_LAND_HALF
    rects = [(o - land, o + land, -land, land)]
    rects.append((launch.pin_u - launch.pad_length / 2.0, launch.pin_u + launch.pad_length / 2.0,
                  -launch.pad_width / 2.0, launch.pad_width / 2.0))
    for u, v, su, sv in smp.GROUND_PADS:
        rects.append((o + u - su / 2.0, o + u + su / 2.0, v - sv / 2.0, v + sv / 2.0))
    return rects


def _distance_to_rect(x: float, y: float, rect) -> float:
    x0, x1, y0, y1 = rect
    return math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1))


def _via_allowed(launch: Launch, x: float, y: float) -> bool:
    """`TryVia`'s rules for a via at (x, y), for the near launch: clear of the
    board's edges, the void, the keep-out beside the pad, and the launch's
    own copper."""
    H, o = launch.half_length, launch.origin_u
    reach = launch.via_diameter / 2.0 + launch.via_min_gap - VIA_TOLERANCE
    if x < -H + reach or x > H - PORT_MARGIN or abs(y) + reach > launch.half_width:
        return False
    if launch.bottom_cutout_radius > 0 and (
            math.hypot(x - launch.pin_u, y) < launch.bottom_cutout_radius + reach):
        return False
    if math.hypot(x - o, y) < launch.void_radius + reach:
        return False
    if launch.pull_edge > 0 and x > o:
        if (x - o) < launch.pull_reach + reach and abs(y) < launch.pull_edge + reach:
            return False
    return all(_distance_to_rect(x, y, r) >= reach for r in _rect_obstacles(launch))


def vias(launch: Launch) -> List[cg.Via]:
    """The stitching lattice of `AddViaLattice` over the short board.

    Rows start `via_gnd_clearance` + radius off the gap edge and step by the
    via pitch; along a row the nodes are half a pitch apart, alternate rows
    staggered, and anchored on the FULL board's centre (so the baseline lands
    on the same nodes as the real board). The centre row of vias behind the
    launch (y = 0) is the odd row, kept only past the void."""
    H, d = launch.half_length, launch.via_diameter
    step = launch.via_pitch / 2.0
    first_row = launch.gnd_inner + launch.via_clearance + d / 2.0
    # u_real = u + H - full_length / 2 is the position on the full board.
    shift = H - launch.full_length / 2.0
    out: List[cg.Via] = []

    def row(y: float, parity: int, past_void: bool) -> None:
        j_lo = math.floor((-H + shift) / step) - 1
        j_hi = math.ceil((H + shift) / step) + 1
        for j in range(j_lo, j_hi + 1):
            if j % 2 != parity:
                continue
            x = j * step - shift
            if past_void and x > launch.origin_u - launch.void_radius:
                continue
            if _via_allowed(launch, x, y):
                out.append(cg.Via(x, y, d, 0.0))

    k = 0
    while first_row + k * launch.via_pitch + d / 2.0 <= launch.half_width:
        y = first_row + k * launch.via_pitch
        for sign in (1.0, -1.0):
            row(sign * y, k % 2, False)
        k += 1
    row(0.0, 1, True)
    return out


def top_copper(launch: Launch) -> List:
    """Regions, tracks and pads of the top layer."""
    H, o, gi = launch.half_length, launch.origin_u, launch.gnd_inner
    land = smp.GROUND_LAND_HALF
    regions = [cg.Region(cg.TOP, _plane(launch, 1.0)), cg.Region(cg.TOP, _plane(launch, -1.0)),
               cg.Region(cg.TOP, _land(launch)),
               # The strap: ground between the land and the board end, across the corridor.
               cg.Region(cg.TOP, ((-H, -gi), (o - land, -gi), (o - land, gi), (-H, gi)))]
    pads = [cg.Pad(cg.TOP, launch.pin_u, 0.0, launch.pad_length, launch.pad_width, 0.0)]
    for u, v, su, sv in smp.GROUND_PADS:
        pads.append(cg.Pad(cg.TOP, o + u, v, su, sv, 0.0))

    track_from = launch.pin_u
    if launch.taper_length > 0:
        edge = launch.pin_u + launch.pad_length / 2.0
        a, b = launch.pad_width / 2.0, launch.trace / 2.0
        regions.append(cg.Region(cg.TOP, ((edge, -a), (edge + launch.taper_length, -b),
                                          (edge + launch.taper_length, b), (edge, a))))
        track_from = edge + launch.taper_length
    tracks = [cg.Track(cg.TOP, track_from, 0.0, H, 0.0, launch.trace)]
    return [regions, tracks, pads]


def board(launch: Launch) -> cg.BoardCopper:
    """The whole short board: top copper, the bottom plane and the vias."""
    H, hw = launch.half_length, launch.half_width
    regions, tracks, pads = top_copper(launch)
    regions = regions + [cg.Region(cg.BOTTOM, ((-H, -hw), (H, -hw), (H, hw), (-H, hw)))]
    return cg.BoardCopper(name="package", tracks=tracks, vias=vias(launch), pads=pads, regions=regions)


# ---------------------------------------------------------------------------
# Reading copper back (for the tests and the report)
# ---------------------------------------------------------------------------

def _inside(points, x: float, y: float) -> bool:
    inside = False
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def covers(copper: cg.BoardCopper, layer: str, x: float, y: float) -> bool:
    """Whether the layer has metal at (x, y): any region, track or pad."""
    tracks, pads, regions = copper.on_layer(layer)
    if any(_inside(r.points, x, y) for r in regions):
        return True
    if any(_inside(p.corners(), x, y) for p in pads):
        return True
    for t in tracks:
        x_lo, x_hi = sorted((t.x1, t.x2))
        if x_lo <= x <= x_hi and abs(y - t.y1) <= t.width / 2.0:
            return True
    return False
