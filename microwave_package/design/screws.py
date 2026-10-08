"""
The four M2 corner screws: where they go, how big every hole is, and how
long a screw the stack wants.

Like `cavity` and `bonds`, this is the one place that answers those
questions, because three halves of the pipeline ask them. Altium keeps its
vias clear of the holes, the HFSS builder cuts the holes and stands the
screws up, and the layout check refuses a board with no room for them.

**The screw is the vendor's model**, not a drawing of one. The STEP files
in base_parts/screws/ (ISO 10642 M2, A2 steel, every length from 3 to
20 mm) are imported by `hfss.screws`; the numbers below were read off them:

    axis             Y, tip at Y = 0, flat top of the head at Y = length
    shank            diameter 2.0
    head cone        90 degree included angle, radius 1.0 at 1.14 mm below the
                     top, out to 1.97 at 0.17 mm below it; the top is flat to
                     radius 1.9 with a small round on the rim
    (the thread helix is modelled, with its construction geometry reaching
    to Y = -1.35; that is not part of the body)

**Where.** The screws sit INSIDE the board's corners, `screw_inset` in from
the PCB edge on both axes, not in bosses outside the box outline. The wall
is `wall_width` (1 mm), which cannot hold an M2 thread. The board rests on
the box floor there (the "pedestal" is a recess cut under the chip, not a
raised boss), so the screw clamps it against the floor, and the thread has
`floor_width + pedestal_depth` of metal to go into.

**Which parts.** Three holes along one axis:

    lid         clearance hole, with a 90 degree countersink the head sits
                flush in
    PCB         clearance hole, with a copper keep-out round it
    box         tap-drill hole into the floor

In the HFSS model the box's hole is the screw's own diameter, not the tap
drill: the thread is a surface the model does not resolve, and a screw
overlapping the metal it is threaded into is an error HFSS refuses to
validate. The tap drill is for the part drawing.

**How long.** Screws can be shortened, so the length is not picked from a
list of what the vendor drew: it is the exact distance from the lid's top to
a tip `screw_engagement` below the floor top (or `screw_length`, if
given). The model used is the shortest STEP at least that long, with the
excess cut off the tip, which is what shortening one on the bench does.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from . import parameter_meta

# Read off the vendor STEP; see the module docstring.
SCREW_DIAMETER = 2.0
HEAD_HEIGHT = 1.14           # flat top down to where the cone meets the shank
HEAD_MAX_DIAMETER = 3.94     # widest point of the cone, just under the rim
# The head cone is 90 degrees included: radius grows 1 mm per mm of height,
# from the shank's radius at the cone's base. Continued up to the lid's top
# face it reaches this diameter, which is what the countersink is cut to so
# that its surface continues the head's.
COUNTERSINK_DIAMETER = SCREW_DIAMETER + 2.0 * HEAD_HEIGHT

# Lengths the vendor drew (Screw_ISO_10642_M2x{L}_A2.STEP in base_parts/screws).
MODEL_LENGTHS = (3, 4, 5, 6, 8, 10, 12, 16, 20)

# Minimums, not parameters: with the screw fixed, each is the difference
# of two dimensions that are already parameters (floor_width and the hole's
# depth; lid_width and the head's height), so a third would be a second way
# to set the same number.
MIN_FLOOR_SKIN = 0.8     # metal left under the bottom of the blind threaded hole
MIN_LID_SEAT = 0.3       # lid material left under the countersink
MIN_ENGAGEMENT = 1.0     # a thread that bites less than this is not holding anything

# What must stay between the PCB hole and the board's edge, and between
# the countersink and lid_inside's outline.
EDGE_MARGIN = 0.5


@dataclass(frozen=True)
class Screws:
    """The four screws of one combination, in board coordinates (millimetres
    from the board centre, u along the feedline, v across it)."""
    positions: Tuple[Tuple[float, float], ...]
    clearance_diameter: float   # PCB and lid
    tap_diameter: float         # box, for the drawing
    keepout_diameter: float     # copper keep-out round each PCB hole
    engagement: float           # thread engaged in the box, below the floor top
    hole_runout: float          # tap-drill depth beyond the engaged thread
    length: float               # 0 = work it out from the stack

    @property
    def countersink_depth(self) -> float:
        """How deep the lid is countersunk, down to where the cone reaches
        the clearance hole."""
        return (COUNTERSINK_DIAMETER - self.clearance_diameter) / 2.0

    @property
    def min_lid_thickness(self) -> float:
        return HEAD_HEIGHT + MIN_LID_SEAT

    @property
    def hole_depth(self) -> float:
        return self.engagement + self.hole_runout


def enabled(values: Dict[str, float]) -> bool:
    try:
        return float(values.get(parameter_meta.SCREWS_PARAM, 0)) > 0.5
    except (TypeError, ValueError):
        return False


def plan(values: Dict[str, float]) -> Optional[Screws]:
    """The screws for one combination's values, or None when `screws_on` is off."""
    if not enabled(values):
        return None
    half_l = values.get("pcb_length", 50.0) / 2.0
    half_s = values.get("pcb_side", 25.0) / 2.0
    inset = values.get("screw_inset", 2.5)
    u, v = half_l - inset, half_s - inset
    return Screws(
        positions=((-u, -v), (-u, v), (u, -v), (u, v)),
        clearance_diameter=values.get("screw_clearance_diameter", 2.3),
        tap_diameter=values.get("screw_tap_diameter", 1.6),
        keepout_diameter=values.get("screw_keepout_diameter", 3.0),
        engagement=values.get("screw_engagement", 3.0),
        hole_runout=values.get("screw_hole_runout", 1.0),
        length=values.get("screw_length", 0.0),
    )


def length_for(screws: Screws, lid_top: float, floor_top: float) -> float:
    """The screw's length, head top to tip.

    `lid_top` is the lid's flat top face and `floor_top` the face the
    board rests on (the board's underside), both in the model's up axis.
    Unless `screw_length` says otherwise, the tip is `engagement` below the
    floor top.
    """
    if screws.length > 0:
        return screws.length
    return (lid_top - floor_top) + screws.engagement


def model_length(needed: float) -> int:
    """The shortest vendor model at least `needed` long, which is then
    shortened to fit. Raises ValueError, saying what to change, when the
    screw is longer than any model."""
    for length in MODEL_LENGTHS:
        if length >= needed - 1e-9:
            return length
    raise ValueError(
        f"the screw would need to be {needed:.2f} mm long and the longest model is "
        f"{MODEL_LENGTHS[-1]} mm -- reduce screw_engagement, lid_width or pedestal_depth"
    )