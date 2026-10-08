"""
The four M2 screws at the board's corners: where they go, how big every
hole is, and which length of screw fits.

Like `cavity` and `bonds`, this is the one place that answers those
questions, because three halves of the pipeline ask them. Altium keeps its
vias clear of the holes, the HFSS builder cuts the holes and stands the
screws up, and the layout check refuses a board with no room for them. Three
independent derivations of one feature drift apart the first time a
parameter moves.

**Where.** The screws sit INSIDE the board's corners, `screw_inset` in from
the PCB edge on both axes -- not in bosses outside the box outline. The box
wall is `wall_width` (1 mm) thick, which cannot hold an M2 thread (a Ø1.6
tap-drill hole leaves 0.2 mm of metal either side of it), so the screw goes
through the board into the floor instead. That puts a metal post in each
corner of the cavity, which is the point of the `screws_on` switch: build the
box with and without to see what the screws do to the modes.

The inset is held to at least half the head's diameter plus a margin, so the
countersunk head stays inside lid_inside's outline (which is the board's
outline) rather than running out over the box wall.

**Which parts.** Three holes along one axis:

    lid         clearance hole, with a 90 degree countersink the ISO 10642
                head sits flush in
    PCB         clearance hole, with a copper keep-out round it
    box floor   tap-drill hole the screw cuts its thread into

In the HFSS model the box's hole is the screw's own diameter, not the tap
drill: the thread is a surface the model does not resolve, and a screw
overlapping the metal it is threaded into is an error HFSS refuses to
validate. The tap drill is for the part drawing.

**Which screw.** ISO 10642 is a countersunk head, measured from the head's
flat top to the tip, so the screw is as long as lid + board + the thread it
bites into. The lid's thickness is not known here (it is a SolidWorks part),
so `choose_length` is given the measured span and picks the longest
standard length that does not come out of the floor's underside.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from . import parameter_meta

# ISO 10642 M2: nominal shank diameter, and the standard lengths the
# vendor's STEP files come in (Screw_ISO_10642_M2x{L}_A2.STEP).
SCREW_DIAMETER = 2.0
STANDARD_LENGTHS = (3, 4, 5, 6, 8, 10, 12, 16, 20)

# The countersink is 90 degrees included, so a head's cone drops one
# millimetre in radius per millimetre of depth.
COUNTERSINK_HALF_ANGLE_DEG = 45.0

# What must stay between the PCB hole and the board's edge, and between
# the head and lid_inside's outline.
EDGE_MARGIN = 0.5

# Floor left under the thread's tip. A thread that comes out of the box's
# underside is a hole in the case.
MIN_FLOOR_LEFT = 0.1

# A thread that bites less than this into the floor is not holding anything.
MIN_ENGAGEMENT = 0.3


@dataclass(frozen=True)
class Screws:
    """The four screws of one combination, in board coordinates (millimetres
    from the board centre, u along the feedline, v across it)."""
    positions: Tuple[Tuple[float, float], ...]
    shank_diameter: float
    head_diameter: float
    head_height: float          # the cone, from the flat top to the shank
    clearance_diameter: float   # PCB and lid
    tap_diameter: float         # box floor, for the drawing
    keepout_diameter: float     # copper keep-out round each PCB hole
    engagement: float           # how far the thread is meant to bite into the floor

    @property
    def countersink_depth(self) -> float:
        """How deep the lid is countersunk: down to where the cone meets the
        clearance hole, which is a little shallower than the head's own cone
        (it ends at the narrower shank)."""
        return (self.head_diameter - self.clearance_diameter) / 2.0

    @property
    def min_lid_thickness(self) -> float:
        """The lid must be at least as thick as the head is tall."""
        return self.head_height


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
    inset = values.get("screw_inset", 2.2)
    u, v = half_l - inset, half_s - inset
    head = values.get("screw_head_diameter", 3.8)
    return Screws(
        positions=((-u, -v), (-u, v), (u, -v), (u, v)),
        shank_diameter=SCREW_DIAMETER,
        head_diameter=head,
        head_height=(head - SCREW_DIAMETER) / 2.0,
        clearance_diameter=values.get("screw_clearance_diameter", 2.3),
        tap_diameter=values.get("screw_tap_diameter", 1.6),
        keepout_diameter=values.get("screw_keepout_diameter", 3.0),
        engagement=values.get("screw_engagement", 0.8),
    )


def choose_length(span_to_board_bottom: float, engagement: float,
                  floor: float) -> int:
    """The longest standard M2 length that does not come out of the floor.

    `span_to_board_bottom` is the distance from the lid's flat top to the
    underside of the board (the top of the box floor); `floor` is how thick
    the floor is there. The thread starts at the floor's top, so a screw of
    length L bites L - span into it, which has to be at least
    MIN_ENGAGEMENT and leave MIN_FLOOR_LEFT under the tip. `engagement` is
    the wanted bite: the longest length that stays within it is used, and if
    none stays within it the shortest that reaches the floor is.

    Raises ValueError, saying what to change, when no standard length works.
    """
    most = min(engagement, floor - MIN_FLOOR_LEFT)
    fits = [n for n in STANDARD_LENGTHS if span_to_board_bottom + MIN_ENGAGEMENT <= n
            <= span_to_board_bottom + most]
    if fits:
        return max(fits)
    reaching = [n for n in STANDARD_LENGTHS if n >= span_to_board_bottom + MIN_ENGAGEMENT]
    if reaching and reaching[0] <= span_to_board_bottom + floor - MIN_FLOOR_LEFT:
        return reaching[0]
    raise ValueError(
        f"no standard M2 length fits: the lid and board are {span_to_board_bottom:g} mm thick, "
        f"so the thread would bite {MIN_ENGAGEMENT:g}-{max(most, 0):g} mm into a "
        f"{floor:g} mm floor, and the lengths are {', '.join(map(str, STANDARD_LENGTHS))} mm. "
        f"Thicken the floor (wall_width) or change the lid"
    )
