"""
The SMP connector land pattern, taken from the vendor library.

Würth Elektronik WR-SMP, part 60124102122403, read out of the KiCad
footprint shipped in `base_parts/WE_60124102122403_Libraries.zip`
(`J_Wurth_WR-SMP_60124102122403.kicad_mod`). These are the vendor's
numbers, not estimates.

Coordinates are in the connector's own frame, in millimetres:

    v  runs along the signal axis; the centre pin exits toward +v
    u  runs across it

The three ground pads form a U that is open toward +v, and its closed
end -- the strap at v = -2.45 -- joins the two sides of the ground. That
matters more than it looks: on a coplanar board those two sides are the
CPW's own grounds, so the strap deliberately shorts them together behind
the launch. The centre conductor therefore has to STOP before it, which
is what `launch_centre_v` is for.

The board's own `port_length` and `port_width` parameters describe the
aperture in the box wall, not this land pattern, and are deliberately not
used here.
"""
from __future__ import annotations

from typing import Dict


# (u, v, size_u, size_v) per pad, straight from the vendor footprint.
SIGNAL_PAD = (0.00, 2.90, 0.48, 1.40)
GROUND_ARM_LEFT = (-2.45, -0.10, 0.30, 4.00)
GROUND_ARM_RIGHT = (2.45, -0.20, 0.30, 4.00)
GROUND_STRAP = (0.00, -2.45, 4.00, 0.30)

PADS = (SIGNAL_PAD, GROUND_ARM_LEFT, GROUND_ARM_RIGHT, GROUND_STRAP)

# The outermost and innermost copper along the signal axis. The outer
# edge is what gets placed `port_space` from the board edge; the inner
# edge is how far the launch reaches back onto the board, which is what
# everything else has to keep clear of.
OUTER_V = min(v - size_v / 2 for _u, v, _su, size_v in PADS)   # -2.60
INNER_V = max(v + size_v / 2 for _u, v, _su, size_v in PADS)   # +3.60

# How much room the whole launch needs, measured from the board edge.
LENGTH = INNER_V - OUTER_V

# Where the centre conductor ends, measured from the outer edge inward.
SIGNAL_V = SIGNAL_PAD[1]


def as_parameters() -> Dict[str, float]:
    """The footprint as flat values for the Altium parameter file.

    Passed through rather than hard-coded in the DelphiScript so the
    vendor's numbers live in exactly one place.
    """
    values: Dict[str, float] = {}
    for name, (u, v, size_u, size_v) in (
        ("smp_sig", SIGNAL_PAD),
        ("smp_arml", GROUND_ARM_LEFT),
        ("smp_armr", GROUND_ARM_RIGHT),
        ("smp_strap", GROUND_STRAP),
    ):
        values[f"{name}_u"] = u
        values[f"{name}_v"] = v
        values[f"{name}_su"] = size_u
        values[f"{name}_sv"] = size_v
    values["smp_outer_v"] = OUTER_V
    values["smp_inner_v"] = INNER_V
    return values


def launch_origin(half_length: float, port_space: float) -> float:
    """Where the connector's own origin sits along the board axis.

    Placed so the outermost copper of the launch is exactly `port_space`
    from the board edge.
    """
    return half_length - port_space + OUTER_V


def launch_centre(half_length: float, port_space: float) -> float:
    """Where the centre conductor should end: the signal pad's centre."""
    return launch_origin(half_length, port_space) - SIGNAL_V


def launch_inner_edge(half_length: float, port_space: float) -> float:
    """The furthest the launch reaches back onto the board.

    Nothing else -- bond fingers, the die, the shield ring -- may come
    past this point.
    """
    return launch_origin(half_length, port_space) - INNER_V


# --- the connector's own 3D model -------------------------------------
#
# The vendor ships one solid. There is no separated pin / dielectric /
# shell version in any format they provide -- STEP, IGES, Cadence, Eagle
# and KiCad all carry a single closed shell -- so the part goes into the
# model as one body and is treated as a perfect conductor. That loses the
# dielectric inside the launch, which is a real approximation and worth
# revisiting if the launch discontinuity turns out to matter.
#
# The STEP sits in the connector's own frame, which is NOT the footprint
# frame and not the board's:
#
#     Xs  along the signal axis, but negated against the footprint's v
#     Ys  across, matching the footprint's u
#     Zs  height above the board
#
# so v = -Xs and u = Ys.

MODEL_X_SPAN = (-3.30, 2.50)   # along the signal axis
MODEL_Y_SPAN = (-2.50, 2.50)   # across


def model_rotation_degrees() -> float:
    """Rotation about X that stands the model up in assembly axes.

    The model has Z up; the assembly has Y up. -90 degrees about X sends
    (x, y, z) to (x, z, -y), which is the same mapping `Frame` applies to
    everything else -- they have to agree or the connector lands in a
    different plane from the board it sits on.
    """
    return -90.0


def model_offset(frame, board_top: float, half_length: float,
                 port_space: float, sign: float) -> list:
    """Where to move the model after rotating it.

    `sign` picks the end of the board: +1 is the +x end. The far
    connector is the near one turned through half a revolution about the
    up axis, so its offset is mirrored rather than recomputed.
    """
    origin = launch_origin(half_length, port_space)
    x_along = sign * origin
    return [x_along + frame.x_offset, board_top, frame.z_offset]
