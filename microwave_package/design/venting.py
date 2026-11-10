"""
Vent channels for the box's trapped air pockets.

The package goes into a vacuum chamber and a cryostat. Any enclosed
pocket of air outgasses slowly, or holds its pressure and bursts a seal
when the chamber is pumped down or the box warms back up. So every
enclosed volume needs a route out, and this is the one place that says
where those routes are and how big, because the SolidWorks box, the HFSS
model and the layout check all ask.

THE AUDIT (default board, read off the exported box and lid STEP files)
-----------------------------------------------------------------------
Vented already:
    lid relief channel    1.0 x 0.2 mm (0.4 mm in its last millimetre) in
                          the lid's underside, from the chip pocket out to
                          x = +-13.9. The SMP holes in the lid start at
                          x = +-13.9 and are open to the outside round the
                          connector barrel, so the channel really does reach
                          them: the two share a face.
    chip pocket in lid    7.5 x 7.5 x 0.4 mm over the die and its bond
                          fingers: it is the channel's own end.
    chip cavity           the 0.1 mm ring round the die in the board's
                          opening: open at the top into the lid pocket.
    SMP lid holes         5.1 x 5.1 mm through the lid; open to the top.
    screw holes           lid and board clearance bores and countersinks
                          lead down the M2 thread; the tapped hole goes right
                          through the floor, so it is not blind. (See below
                          for why that alone is not trusted.)
Not vented, and the vents below:
    pedestal recess       a 6.3 x 6.3 x 0.5 mm recess (about 19 mm3) in the
                          floor under the die, closed by the die lying over
                          it and by the board round it.
    screw bores           the through-tapped hole's lower end is closed by the
                          screw tip, flush with the underside that bolts
                          against the cold plate, so the only way out is down
                          the thread and between two flat faces. A side
                          groove makes it a real route.
Pending the PI's decision, not vented here:
    plated vias           sealed when the box is assembled; filled, capped or
                          vented is the PI's call.
Not pockets: the 35 um copper steps against the lid (closed by the relief
channel where they matter), and the thin film between the board and the floor.

THE VENTS
---------
A vent is a narrow groove in the TOP of the box floor, running under the
board, out through the wall, and so out of the side face. Out of the side,
not the underside: the underside bolts to the cold plate, and a hole into
a flat face that is clamped shut vents nothing.

    pedestal vent   one slot straight across the feedline at the middle of
                    the die, through the recess and out of both side faces
    screw vents     from each screw's tapped hole, to the nearest side face

Why they are harmless: the groove lies BELOW the board's bottom copper,
which is a solid ground plane (the screw keep-out leaves the plane
open at the corner, but that is 9 mm from the line), so nothing couples to
it. A groove that is `vent_width` x `vent_depth` is a waveguide whose
cutoff is c / (2 max(width, depth)): 300 GHz at the default 0.5 mm, more
than ten times the band's top. It is also off the CPW, the launch and the
die's bond fingers, so the check below only asks for the size.

`vent_depth` is measured down from the floor's top face.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import parameter_meta

SPEED_OF_LIGHT_MM_GHZ = 299.792458   # mm * GHz: c in the units the cutoff comes out in

# The band's top is 26 GHz. A vent is "far below cutoff" for the band when
# its cutoff is at least this many times the band's top.
BAND_TOP_GHZ = 26.0
CUTOFF_MARGIN = 5.0

# What a milling cutter and a deburring pass can leave, and what must
# stay of the floor under the groove.
MIN_VENT_WIDTH = 0.3
MIN_FLOOR_LEFT = 0.5

# How far past the recess's edge a vent starts (so the cut overlaps the
# recess rather than just touching it), and past the wall's outer face it
# ends (so it breaks cleanly out).
OVERLAP = 0.2


@dataclass(frozen=True)
class Vent:
    """One groove, in board coordinates (u along the feedline, v across
    it, millimetres from the board centre): the segment's end points, its
    width, and how far it is cut down from the floor's top face."""
    name: str
    start: Tuple[float, float]
    end: Tuple[float, float]
    width: float
    depth: float

    @property
    def length(self) -> float:
        return abs(self.end[0] - self.start[0]) + abs(self.end[1] - self.start[1])

    def rectangle(self) -> Tuple[float, float, float, float]:
        """(u_min, v_min, u_max, v_max): the groove seen from above. Vents
        run along an axis, so it is always an axis-aligned rectangle."""
        half = self.width / 2.0
        (u0, v0), (u1, v1) = self.start, self.end
        return (min(u0, u1) - (half if u0 == u1 else 0.0),
                min(v0, v1) - (half if v0 == v1 else 0.0),
                max(u0, u1) + (half if u0 == u1 else 0.0),
                max(v0, v1) + (half if v0 == v1 else 0.0))


def enabled(values: Dict[str, float]) -> bool:
    try:
        return float(values.get(parameter_meta.VENTS_PARAM, 0)) > 0.5
    except (TypeError, ValueError):
        return False


def cutoff_ghz(width: float, depth: float) -> float:
    """TE10 cutoff of an air-filled rectangular guide: c / (2 a), a the longer side."""
    return SPEED_OF_LIGHT_MM_GHZ / (2.0 * max(width, depth))


def max_dimension() -> float:
    """The widest or deepest a vent may be and stay far below cutoff."""
    return SPEED_OF_LIGHT_MM_GHZ / (2.0 * BAND_TOP_GHZ * CUTOFF_MARGIN)


def floor_top(values: Dict[str, float]) -> float:
    """Height of the floor's top face above the box's underside (the face
    the board rests on), which `vent_depth` is measured down from."""
    from . import screws as screw_plan
    return screw_plan.floor_width(values) + values.get("pedestal_depth", 0.0)


def plan(values: Dict[str, float]) -> List[Vent]:
    """The vents for one combination's values; empty when `vents_on` is off.

    The pedestal vent exists only if there is a recess under a die to vent:
    `pedestal_depth` > 0 and not a bare board (`no_chip`). A bare board has
    no die and no opening in its bottom copper, so a recess there is not a
    pocket this vent serves (and a bare board's recess is refused when its run
    is made). box.SLDPRT's `Vent_Pedestal` is suppressed by the same condition.
    The screw vents exist only when the screws are on.
    """
    if not enabled(values):
        return []
    from . import screws as screw_plan

    width = values.get("vent_width", 0.5)
    depth = values.get("vent_depth", 0.3)
    half_side = values.get("pcb_side", 25.0) / 2.0
    outside = half_side + values.get("wall_width", 1.0) + OVERLAP
    vents: List[Vent] = []

    if values.get("pedestal_depth", 0.0) > 0 and not parameter_meta.is_bare_board(values):
        # One slot, wall to wall through the middle of the recess: both
        # ends open, so the recess breathes whichever way is clear.
        vents.append(Vent("pedestal", (0.0, -outside), (0.0, outside), width, depth))

    screws = screw_plan.plan(values)
    if screws is not None:
        for index, (u, v) in enumerate(screws.positions, start=1):
            sign = 1.0 if v >= 0 else -1.0
            vents.append(Vent(f"screw_{index}", (u, v), (u, sign * outside), width, depth))
    return vents


def check(values: Dict[str, float]) -> Optional[str]:
    """Why the vents cannot be made on this board, or None if they can
    (or `vents_on` is off). Said in terms of the parameter to change."""
    if not enabled(values):
        return None
    width = values.get("vent_width", 0.5)
    depth = values.get("vent_depth", 0.3)
    limit = max_dimension()
    if width < MIN_VENT_WIDTH:
        return (f"vent_width {width:g} mm is under the {MIN_VENT_WIDTH:g} mm a cutter can hold "
                f"-- increase it")
    if depth <= 0:
        return f"vent_depth {depth:g} mm leaves no groove -- increase it"
    if max(width, depth) > limit:
        return (f"a {width:g} x {depth:g} mm vent has a cutoff of "
                f"{cutoff_ghz(width, depth):.0f} GHz, under {CUTOFF_MARGIN:g} x the "
                f"{BAND_TOP_GHZ:g} GHz band top -- keep both under {limit:.2f} mm")
    left = floor_top(values) - depth
    if left < MIN_FLOOR_LEFT:
        return (f"a {depth:g} mm vent leaves {left:g} mm of floor under the groove, under "
                f"{MIN_FLOOR_LEFT:g} mm -- reduce vent_depth or thicken the floor")
    return None
