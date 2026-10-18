"""
The pedestal recess in the box floor under the die: its shape and size, and
the checks that keep a die sitting on it.

Two shapes, chosen by `pedestal_shape`, both cut `pedestal_depth` deep into
the cavity floor and centred on the box:

    0  a square of `pedestal_size` with its four corners rounded by
       `pedestal_corner_radius`. The walls run flush with the die's edges, so
       the die bridges the recess on its flat walls and rests on the four
       small corner slivers of rim.
    1  a circle of diameter `pedestal_size`. The die's edges touch it at their
       midpoints and its corners stand over the rim.

`pedestal_size` is the real die side, 6.25 mm, not the 6.3 mm the PCB opening
uses: the recess needs no margin, and a bigger one would let the die fall in.

The die cannot fall in: at the largest play the die can take in the PCB
opening (`cavity_clearance` each way), its corners must still stand over the
rim. `check` tests that for the chosen shape.

The SolidWorks box part builds both shapes as separate features, each
suppressed by its own equation (see base_parts/add_pedestal_shapes.py). The
square's corners are sketch fillets linked to `pedestal_corner_radius`, and a
sketch fillet cannot be zero, so a square radius of 0 is refused here rather
than built. The HFSS side needs no cut of its own: the box STEP it imports
carries whatever the SolidWorks part makes.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

from . import parameter_meta

# The real die's side (mm). The recess is never bigger than this.
CHIP_SIZE = 6.25

SQUARE = 0
CIRCLE = 1

DEFAULT_SIZE = CHIP_SIZE
DEFAULT_SHAPE = SQUARE
# The default radius (mm), and the one a part built before this parameter
# existed gets. A 1 mm end mill cannot cut a sharper internal corner.
DEFAULT_RADIUS = 1.0

# A fillet at a 90-degree corner of the square sits at the corner's distance
# (1 - 1/sqrt(2)) * R inside the square's corner. Used by the die-fall check.
CORNER_INSET = 1.0 - 1.0 / math.sqrt(2.0)


def shape(values: Dict[str, float]) -> float:
    """The recess shape for one combination: SQUARE (0) or CIRCLE (1)."""
    return float(values.get("pedestal_shape", DEFAULT_SHAPE))


def size(values: Dict[str, float]) -> float:
    """The recess's size in mm: the square side or the circle diameter."""
    return float(values.get("pedestal_size", DEFAULT_SIZE))


def corner_radius(values: Dict[str, float]) -> float:
    """The square's corner radius for one combination, in mm (see `check`)."""
    return float(values.get("pedestal_corner_radius", DEFAULT_RADIUS))


def check(values: Dict[str, float]) -> Optional[str]:
    """Why the recess cannot be built with these values, or None.

    Only a recess under a die is checked: a bare board (`no_chip`) has no
    recess, and `pedestal_depth` 0 cuts none. Returns a sentence naming the
    parameter to change, in the style of `constraints.check_combination`.
    """
    if parameter_meta.is_bare_board(values) or values.get("pedestal_depth", 0.0) <= 0:
        return None

    which = shape(values)
    if which not in (SQUARE, CIRCLE):
        return "pedestal_shape must be 0 (square) or 1 (circle)"

    side = size(values)
    if side <= 0:
        return "pedestal_size must be positive: it is the side of the square or the diameter of the circle"
    if side > CHIP_SIZE + 1e-9:
        return (f"pedestal_size {side:g} mm is bigger than the {CHIP_SIZE:g} mm chip: a recess "
                f"bigger than the die would let it fall in -- reduce pedestal_size")

    # The die's corners, pushed in by the largest play the PCB opening allows.
    clearance = values.get("cavity_clearance", 0.1)
    half_die = CHIP_SIZE / 2.0
    reach = half_die - clearance

    if which == SQUARE:
        radius = corner_radius(values)
        if radius <= 0:
            # The SolidWorks fillet cannot be zero, and a zero dimension keeps its
            # last value with no error, so the part would silently keep another radius.
            return ("pedestal_corner_radius must be above 0: the square recess is rounded with a "
                    "fillet, which cannot have zero radius")
        if radius >= side / 2.0:
            return (f"pedestal_corner_radius {radius:g} mm must be less than half pedestal_size "
                    f"({side / 2.0:g} mm), or the straight walls of the square vanish -- reduce it")
        # The die's corner must stay outside the fillet at the largest play:
        # the fillet's centre is (side/2 - R) each way, and the die corner at
        # (reach, reach) has to be at least R from it.
        if reach <= side / 2.0 - radius * CORNER_INSET:
            return (f"at cavity_clearance {clearance:g} mm the die's corner would drop past a "
                    f"{radius:g} mm corner of the {side:g} mm square -- reduce pedestal_size or "
                    f"cavity_clearance, or raise pedestal_corner_radius")
    else:
        # The die's corners must stand outside the circle at the largest play.
        if math.sqrt(2.0) * reach <= side / 2.0:
            return (f"at cavity_clearance {clearance:g} mm the die's corner would fall inside the "
                    f"{side:g} mm circle -- reduce pedestal_size or cavity_clearance")
    return None
