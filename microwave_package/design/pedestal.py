"""
The pedestal recess in the box floor under the die: its shape and size, and
the checks that keep a die sitting on it.

Two shapes, chosen by `pedestal_shape`, both cut `pedestal_depth` deep into
the cavity floor and centred on the box:

    0  a square of side `pedestal_size` with its four corners rounded by
       `pedestal_corner_radius`. The square is smaller than the die (6.2 mm
       against 6.25 mm), so the die cannot fall in: it bridges the recess on
       its flat walls and rests on the rim around the square, 0.025 mm out on
       every side.
    1  a circle of diameter `pedestal_diameter`. At the die's size (6.25 mm)
       its edge touches the die's sides at their midpoints and its corners
       stand over the rim.

The two sizes are separate parameters: the square's side is `pedestal_size`
and the circle's diameter is `pedestal_diameter`. Changing one does not move
the other.

The die cannot fall in: at the largest play the die can take in the PCB
opening (`cavity_clearance` each way), its corners must still stand over the
rim. `check` tests that for the chosen shape.

The SolidWorks box part builds both shapes as separate features, each
suppressed by its own equation (see base_parts/add_pedestal_shapes.py and
base_parts/add_pedestal_diameter.py). The square's corners are sketch fillets
linked to `pedestal_corner_radius`, and a sketch fillet cannot be zero, so a
square radius of 0 is refused here rather than built. The HFSS side needs no
cut of its own: the box STEP it imports carries whatever the SolidWorks part
makes.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

from . import parameter_meta

# The real die's side (mm). No recess may be bigger than this, and the square
# must be smaller.
CHIP_SIZE = 6.25

SQUARE = 0
CIRCLE = 1

DEFAULT_SIZE = 6.2                   # the square's side (mm), 0.025 mm inside the die
DEFAULT_DIAMETER = CHIP_SIZE         # the circle's diameter (mm), the die's own side
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
    """The square recess's side in mm (`pedestal_size`)."""
    return float(values.get("pedestal_size", DEFAULT_SIZE))


def diameter(values: Dict[str, float]) -> float:
    """The circular recess's diameter in mm (`pedestal_diameter`)."""
    return float(values.get("pedestal_diameter", DEFAULT_DIAMETER))


def recess_size(values: Dict[str, float]) -> float:
    """The size that applies to the chosen shape: the square's side for
    SQUARE, the circle's diameter for CIRCLE."""
    return diameter(values) if shape(values) == CIRCLE else size(values)


def corner_radius(values: Dict[str, float]) -> float:
    """The square's corner radius for one combination, in mm (see `check`)."""
    return float(values.get("pedestal_corner_radius", DEFAULT_RADIUS))


def rim_support_area(values: Dict[str, float], die: float = CHIP_SIZE) -> float:
    """The rim under a square die of side `die`, centred on the recess, in mm^2:
    the die's footprint minus the recess, with the recess inside the die's
    footprint. This is the area that carries the die.

    The square recess loses (4 - pi) R^2 at its four corners to its rounding,
    which is why it is a little less than side^2. At the defaults: the 6.2 mm
    square with R 1 leaves 1.4809 mm^2; the 6.25 mm circle leaves 8.3829 mm^2.
    """
    if shape(values) == CIRCLE:
        pocket = math.pi * (diameter(values) / 2.0) ** 2
    else:
        side = size(values)
        radius = corner_radius(values)
        pocket = side * side - (4.0 - math.pi) * radius * radius
    return die * die - pocket


def check(values: Dict[str, float]) -> Optional[str]:
    """Why the recess cannot be built with these values, or None.

    Only a recess under a die is checked: a bare board (`no_chip`) has no
    recess, and `pedestal_depth` 0 cuts none. Returns a sentence naming the
    parameter to change, in the style of the other checks in `constraints`.
    """
    if parameter_meta.is_bare_board(values) or values.get("pedestal_depth", 0.0) <= 0:
        return None

    which = shape(values)
    if which not in (SQUARE, CIRCLE):
        return "pedestal_shape must be 0 (square) or 1 (circle)"

    if which == CIRCLE:
        side = diameter(values)
        name = "pedestal_diameter"
    else:
        side = size(values)
        name = "pedestal_size"
    if side <= 0:
        return f"{name} must be positive: it is the size of the recess"
    if which == SQUARE and side >= CHIP_SIZE - 1e-9:
        return (f"pedestal_size {side:g} mm is not smaller than the {CHIP_SIZE:g} mm chip: the "
                f"square recess must be smaller than the die so that it cannot drop in -- "
                f"reduce pedestal_size")
    if which == CIRCLE and side > CHIP_SIZE + 1e-9:
        return (f"pedestal_diameter {side:g} mm is bigger than the {CHIP_SIZE:g} mm chip: a recess "
                f"bigger than the die would let it fall in -- reduce pedestal_diameter")

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
                    f"{side:g} mm circle -- reduce pedestal_diameter or cavity_clearance")
    return None
