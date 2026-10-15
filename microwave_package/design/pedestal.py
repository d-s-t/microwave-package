"""
The pedestal recess in the box floor under the die: its outline, the
rounding of its corners, and the checks that keep a die sitting on it.

The recess is a square of the die's size (`chip_length` x `chip_side`)
whose four corners are cut back by 45-degree chamfers of `chip_leg`. The
four solid corner triangles are the legs the die lies on, and the recess
walls run flush with the die's edges. So the die bridges the recess on
the flat walls and rests on the legs at its corners.

Every one of the eight corners of that octagon is a pocket corner, which
a milling cutter cannot reach sharply. `pedestal_corner_radius` rounds
them all with one radius. A fillet adds metal to the box, so it only ever
adds support under the die, never removes it.

The chamfer legs limit the radius. A fillet at a 135-degree corner uses
R * tan(22.5 deg) of each edge it meets, and a leg's chamfer is
`chip_leg` * sqrt(2) long, so both ends of a chamfer must share it.

The die cannot drop in: at the largest play the die can take in the PCB
opening (`cavity_clearance` each way), its corners still stand over the
legs as long as the legs are wider than twice that play. That is what
`check` tests, rather than a rim on the flat walls, which the play would
close up completely.

The SolidWorks box part rounds these corners with two sketch fillets on
`Sketch2`, each radius linked to `pedestal_corner_radius` (see
base_parts/add_pedestal_radius.py). A sketch fillet cannot be zero, so a
radius of 0 is refused here rather than built. The HFSS side needs no cut
of its own: the box STEP it imports carries whatever the SolidWorks part makes.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

from . import parameter_meta

# The default radius (mm), and the one a part built before this parameter
# existed gets. A 1 mm end mill cannot cut a sharper internal corner.
DEFAULT_RADIUS = 1.0

# Half the angle of a fillet at the octagon's 135-degree corners: a fillet of
# radius R uses R * tan(22.5 deg) along each edge it meets.
TAN_HALF_TURN = math.tan(math.radians(22.5))


def corner_radius(values: Dict[str, float]) -> float:
    """The corner radius for one combination, in mm (above 0; see `check`)."""
    return float(values.get("pedestal_corner_radius", DEFAULT_RADIUS))


def leg_limit(leg: float) -> float:
    """The largest radius a chamfer leg of `leg` mm can hold at its two ends."""
    return leg * math.sqrt(2.0) / (2.0 * TAN_HALF_TURN)


def check(values: Dict[str, float]) -> Optional[str]:
    """Why the recess cannot be built with these values, or None.

    Only a recess under a die is checked: a bare board (`no_chip`) has no
    recess, and `pedestal_depth` 0 cuts none. Returns a sentence naming the
    parameter to change, in the style of `constraints.check_combination`.
    """
    if parameter_meta.is_bare_board(values) or values.get("pedestal_depth", 0.0) <= 0:
        return None

    radius = corner_radius(values)
    leg = values.get("chip_leg", 0.8)
    clearance = values.get("cavity_clearance", 0.1)
    half_length = values.get("chip_length", 6.3) / 2.0
    half_side = values.get("chip_side", 6.3) / 2.0

    if radius < 0:
        return "pedestal_corner_radius cannot be negative"
    if leg <= 0:
        return ("chip_leg must be positive: the four legs are the corners the die lies on, "
                "and a recess without them has no chamfers to round")
    if leg >= min(half_length, half_side):
        return (f"chip_leg {leg:g} mm is as long as half the die, so the legs cover the whole "
                f"recess -- reduce chip_leg")
    if leg <= 2.0 * clearance:
        return (f"chip_leg {leg:g} mm must be more than twice cavity_clearance "
                f"({2.0 * clearance:g} mm): a die shifted by the clearance in both directions "
                f"would otherwise come off its legs -- raise chip_leg or reduce cavity_clearance")
    if radius == 0:
        # The SolidWorks fillet cannot be zero, and a zero dimension keeps its
        # last value with no error, so the part would silently keep another radius.
        return ("pedestal_corner_radius must be above 0: the SolidWorks box rounds the recess with "
                "a fillet, which cannot have zero radius (a square recess is not built)")
    if radius > min(half_length, half_side):
        return (f"pedestal_corner_radius {radius:g} mm is more than half the recess's smaller "
                f"side ({min(half_length, half_side):g} mm) -- reduce it")
    if radius > leg_limit(leg):
        return (f"pedestal_corner_radius {radius:g} mm does not fit the {leg:g} mm legs: a fillet "
                f"at each end of a leg's chamfer needs {2.0 * radius * TAN_HALF_TURN:.3f} mm of its "
                f"{leg * math.sqrt(2.0):.3f} mm length -- reduce pedestal_corner_radius to at most "
                f"{leg_limit(leg):.3f} mm, or raise chip_leg")
    return None
