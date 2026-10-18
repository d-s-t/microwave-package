"""The PCB's rounded outline, as plain geometry (no AEDT).

The board is a rectangle `2 * half_length` by `2 * half_side` with its four
vertical corners rounded to `pcb_corner_radius`. The box cavity's corners are
rounded to `cavity_corner_radius` and the PCB sits in the cavity with no gap
(see design/constraints.py), so the board's own corners have to be rounded
too: a sharp corner would poke into the cavity's arc.

Everything here works in BOARD coordinates, the same (u, v) plane Altium
draws in, centred on the board. `hfss/model_builder.py` maps them into the
assembly with `copper_geometry.Frame`. Keeping the arithmetic here means the
outline, the cut-outs and the copper decisions can be tested without AEDT.

Corner rule: a feature is clipped if its bounding box reaches a corner's
square (the R x R corner of the rectangle). That is deliberately conservative:
a feature that touches the square but not the arc is clipped by a boolean that
removes nothing, which is harmless. A feature that misses every square cannot
reach the arc, so it is left alone.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Mapping, Sequence, Tuple

PCB_CORNER_PARAM = "pcb_corner_radius"
CAVITY_CORNER_PARAM = "cavity_corner_radius"


def corner_radius_of(values: Mapping[str, float]) -> float:
    """The PCB's corner radius for these equations, 0 for a sharp board.

    `pcb_corner_radius` where the equations file has one. A file from the
    stage between the rounded cavity and the rounded board has only the
    cavity's radius, and the board is meant to match it. A file with neither
    is from before the cavity was rounded, and its box and board were both
    sharp, so 0 is the faithful answer.
    """
    if PCB_CORNER_PARAM in values:
        return float(values[PCB_CORNER_PARAM])
    if CAVITY_CORNER_PARAM in values:
        return float(values[CAVITY_CORNER_PARAM])
    return 0.0


def outline_area(half_length: float, half_side: float, radius: float) -> float:
    """Area of the rounded rectangle: the rectangle less four corners of
    (1 - pi/4) r^2 each."""
    return 4.0 * half_length * half_side - (4.0 - math.pi) * radius * radius


def outline_points(half_length: float, half_side: float, radius: float,
                   steps: int = 16) -> List[Tuple[float, float]]:
    """The outline as a closed polygon, counter-clockwise, `steps` segments
    per arc. Sharp corners (radius 0) give the four corners."""
    x, y = half_length, half_side
    if radius <= 0:
        return [(x, y), (-x, y), (-x, -y), (x, -y)]
    cx, cy = x - radius, y - radius
    points: List[Tuple[float, float]] = []
    # One quadrant at a time, from the +u axis round to the -v axis.
    for sx, sy, start in ((1, 1, 0.0), (-1, 1, 90.0), (-1, -1, 180.0), (1, -1, 270.0)):
        for k in range(steps + 1):
            a = math.radians(start + 90.0 * k / steps)
            points.append((sx * cx + radius * math.cos(a), sy * cy + radius * math.sin(a)))
    return points


def corner_arcs(half_length: float, half_side: float,
                radius: float) -> List[Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]]:
    """The four corner arcs of the outline, counter-clockwise from the
    bottom-left, as (centre, start point, end point). The outline runs
    start -> end, then a straight edge to the next arc's start. This is the
    order GenerateFromParams.pas draws them in (CornerArc), so a Gerber
    plot of the outline follows the Altium board's arcs exactly.
    """
    r = radius
    centres = [(-half_length + r, -half_side + r), (half_length - r, -half_side + r),
               (half_length - r, half_side - r), (-half_length + r, half_side - r)]
    start_angles = (180.0, 270.0, 0.0, 90.0)
    arcs = []
    for (cx, cy), a0 in zip(centres, start_angles):
        a1, a2 = math.radians(a0), math.radians(a0 + 90.0)
        start = (cx + r * math.cos(a1), cy + r * math.sin(a1))
        end = (cx + r * math.cos(a2), cy + r * math.sin(a2))
        arcs.append(((cx, cy), start, end))
    return arcs


@dataclass(frozen=True)
class CornerCut:
    """One corner's piece to take off the board, cavity or vacuum.

    `box` is the corner's R x R square pushed R further outward, so the cut's
    outer faces lie outside the board's edges and never coincide with them.
    The cut is `box` less the disc of radius `radius` centred on `centre`.
    """
    centre: Tuple[float, float]
    box: Tuple[float, float, float, float]   # u1, v1, u2, v2 with u1 < u2, v1 < v2
    radius: float


def corner_cuts(half_length: float, half_side: float, radius: float) -> List[CornerCut]:
    """The four corner cut-outs. None for a sharp board."""
    if radius <= 0:
        return []
    x, y, r = half_length, half_side, radius
    cuts = []
    for sx in (1, -1):
        for sy in (1, -1):
            cu, cv = sx * (x - r), sy * (y - r)
            u1, u2 = sorted((cu, sx * (x + r)))
            v1, v2 = sorted((cv, sy * (y + r)))
            cuts.append(CornerCut(centre=(cu, cv), box=(u1, v1, u2, v2), radius=r))
    return cuts


def corner_squares(half_length: float, half_side: float,
                   radius: float) -> List[Tuple[float, float, float, float]]:
    """Each corner's R x R square, inside the board. Empty for a sharp board."""
    if radius <= 0:
        return []
    x, y, r = half_length, half_side, radius
    squares = []
    for sx in (1, -1):
        for sy in (1, -1):
            u1, u2 = sorted((sx * (x - r), sx * x))
            v1, v2 = sorted((sy * (y - r), sy * y))
            squares.append((u1, v1, u2, v2))
    return squares


def clearance_inside(x: float, y: float, half_length: float, half_side: float,
                     radius: float) -> float:
    """How far (x, y) lies inside the rounded outline: its distance to the
    nearest edge or arc, positive inside and negative outside.

    A point in a corner square is measured to the corner's arc, not to the
    square's own edges, which is what makes a via there clear or not. Used
    to check copper written by Altium against the outline it was drawn in.
    """
    ax, ay = abs(x), abs(y)
    if radius <= 0:
        return min(half_length - ax, half_side - ay)
    cu, cv = half_length - radius, half_side - radius
    if ax <= cu or ay <= cv:
        return min(half_length - ax, half_side - ay)
    return radius - math.hypot(ax - cu, ay - cv)


def reaches_corner(bounds: Sequence[float], half_length: float, half_side: float,
                   radius: float) -> bool:
    """Whether the box `bounds` = (u1, v1, u2, v2) touches any corner square.

    Closed intervals, so a box that just touches a square counts. That is the
    conservative side: see the module docstring.
    """
    u1, v1, u2, v2 = bounds
    for cu1, cv1, cu2, cv2 in corner_squares(half_length, half_side, radius):
        if u2 >= cu1 and u1 <= cu2 and v2 >= cv1 and v1 <= cv2:
            return True
    return False
