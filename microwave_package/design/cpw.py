"""
Characteristic impedance of a conductor-backed coplanar waveguide with a
metal cover over it, and the gap that makes it 50 ohm.

The layout generator needs a trace width *and* a gap, but only one of
them can be a free parameter if the line is to stay matched. The trace
width is the fabrication-limited dimension, so `default.txt` sets that
and the gap is solved for here -- once per substrate, because the PCB
(Rogers) and the chip (silicon) have different permittivities and so
need different gaps for the same width.

The line that is built is not an open-top GCPW. The lid rests on the
board's top copper, and all that keeps it off the trace is a relief
channel (`route_width` wide, `route_clearence` deep), so the trace has a
metal ceiling `route_clearence` above it. On a low-permittivity board
that ceiling is most of the capacitance. The solve therefore takes the
cover into account (`cover_height`, `channel_width`), and the layout
check refuses a lid that cannot carry a 50 ohm line (`ReliefChannelError`).

The impedance model
-------------------
Conformal mapping, in the partial-capacitance form of Simons (*Coplanar
Waveguide Circuits, Components and Systems*): a ground plane on either side
of the CPW enters as a modulus mapped through its distance. With
a = trace_width/2, b = a + gap, the substrate thickness h (the bottom ground
at depth h) and the top ground at height hc:

    k_sub = tanh(pi a / 2h) / tanh(pi b / 2h)       (hc -> inf:  k_air = a / b)
    k_air = tanh(pi a / 2hc) / tanh(pi b / 2hc)

    r_sub = K(k_sub) / K(k_sub')        (K the complete elliptic integral)
    r_air = K(k_air) / K(k_air')  +  s(t, gap)

    eps_eff = (r_air + eps_r * r_sub) / (r_air + r_sub)
    Z0      = 60 pi / ( sqrt(eps_eff) * (r_air + r_sub) )

The substrate half sees only the bottom ground; the air half sees the cover
(or, with none, an open half-space). `cover_height=None` is the open-top
GCPW of the previous version of this module and `cover_height -> infinity`
gives the same number.

Conductor thickness is `s(t, gap)`, a term added to the air side only:

    s(t, gap) = t / gap  +  SIDEWALL_FRINGE * (1 - exp(-t / 10 um))

The first term is the parallel-plate capacitance of the two metal
sidewalls that face each other across each slot. It is exact in the limit
t >> gap, and it is why thickness matters here: 35 um of copper on a
100 um slot is a 0.35 term on an r_air of about 1. The second is the
fringe at the top edges, fitted. The sidewall field is in air, so it
belongs to the air side and does not scale with eps_r.

This replaces the usual first-order correction, which widens the trace by delta = (1.25 t / pi)(1 + ln(4 pi w / t)) and narrows
the slot by the same, for both halves. That is a perturbation formula, and
35 um of copper on a 100 um slot is not one: it widened a 0.4 mm trace by 83
um and put the gap that it called 50 ohm, on RT/duroid 5880, at 62 ohm in the
field solve below (TMM 10i: 55 ohm). The delta that would fit is 1.1-1.4 t on
a 2.2 board and 0.8-1.1 t on a 9.8 one, not a constant or a function of the
trace, which is the sign of a correction applied to the wrong half.

The model was fitted to and checked against a 2-D quasi-static field solve of
the real cross-section (`base_parts/cpw_fd_check.py`: the copper as a solid,
the substrate, the lid with its channel and the channel's side walls; and, for
the open top, a gridless boundary-element solve, `base_parts/cpw_bem_check.py`,
which agrees with it to 0.3 % with 35 um of copper; and AEDT's own 2D Extractor,
`base_parts/cpw_q2d_check.py`, which agrees with it to 0.5 % on nine boards), over
eps_r 2.2-10.2, substrate 0.254-0.635 mm, copper 18-70 um, trace 0.15-0.8 mm
and covers 0.04-3 mm high (484 geometries). The solved gap lands within
0.9 % rms of 50 ohm in the field solve (5.7 % at worst); in impedance, over
any geometry, the error is 0.6 % rms (1.7 % at most) for covers 0.18 mm or
more, 1 % rms (2.8 %) at 0.15 mm, 2.8 % (6 %) at 0.1 mm and 6 % (13 %) at
40 um, where the line is far under 50 ohm anyway.

Validity
--------
* The cover is a plane at `cover_height` above the *top face of the metal*
  (the lid rests on the copper, so this is the relief depth). The accuracy
  above is the model's; below 0.15 mm treat it as indicative.
* The plane stands for a channel only if its side walls are out of the way.
  The walls change the impedance by under 1.5 % once they stand `wall_
  clearance(cover_height)` outside the slot edges, and by up to 30 % when they
  stand 50 um over the slot. `solve_gap_for_z0` refuses a channel narrower
  than trace + 2 gap + 2 wall_clearance rather than extrapolate.
* Slots up to `MAX_GAP_PER_HEIGHT` (2) substrate thicknesses are looked for. A
  wider trace can level off just over 50 ohm and then have a root at an absurd gap
  (0.4 mm on a 0.381 mm silicon die: 28.8 mm); that is refused as unreachable.
* Quasi-static: no dispersion (fine well under the board's 26 GHz limit for
  these dimensions), lossless conductors, an infinitely wide substrate, and
  the vias of the fence ignored (they are in the ground plane).

So this picks the starting geometry and HFSS has the last word, which is
why the solved value is recorded per run. Altium's own layer-stack field
solver is a useful independent check on the same number.

Everything here is in millimetres, like `default.txt` itself.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

# Relative permittivity of each substrate, keyed by the HFSS material
# name used in the run's material settings -- so the number the gap is
# solved with and the number HFSS meshes with cannot drift apart.
# Anything not listed here has to be passed explicitly.
PERMITTIVITY: Dict[str, float] = {
    "Rogers TMM 10i (tm)": 9.8,
    # The other boards the new-run form offers (model_builder.MATERIAL_OPTIONS):
    # without these a run that chose one failed before anything was built.
    # Values are the data-sheet ones HFSS's library carries for each.
    "Rogers RT/duroid 5880 (tm)": 2.2,
    "Rogers RO3003 (tm)": 3.0,
    # The library's RO4003 carries the design value, 3.55 (loss tangent
    # 0.0027), not the 3.38 process value on the RO4003C data sheet: the gap
    # is solved for what HFSS meshes with.
    "Rogers RO4003 (tm)": 3.55,
    "Rogers RT/duroid 6010/6010LM (tm)": 10.2,
    "FR4_epoxy": 4.4,
    "silicon": 11.9,
    "sapphire": 10.0,
    "vacuum": 1.0,
}

TARGET_Z0_OHM = 50.0

# Free-space impedance / 2pi, the leading constant of the CPW result.
_ETA0_OVER_2PI = 59.9585

# Fringe field at the top edges of thick metal, in units of r_air: fitted to
# a 2-D field solve (see the module docstring), and independent of eps_r and
# the trace width. It is a property of having an edge, so it dies away with
# the metal: the chip's 200 nm film, drawn as a sheet, does not get it.
SIDEWALL_FRINGE = 0.09
_FRINGE_METAL_SCALE_MM = 0.010

# How far outside the slot edges the lid's channel walls have to stand for the
# cover to be a plane to the line (see `wall_clearance`).
MIN_WALL_CLEARANCE_MM = 0.1

# The widest slot the solve will look for, as a multiple of the substrate
# thickness. Past about one thickness the coplanar grounds stop mattering to
# the substrate half and the line is a microstrip with distant grounds, whose
# impedance flattens out towards its microstrip value. A trace too wide for 50
# ohm as a CPW can still have that value just above 50 ohm, and then the
# bisection reports a root at an absurd gap: a 0.4 mm trace on the 0.381 mm
# silicon die levels off at 50.9 ohm and "solved" to a 29 mm gap, which put the
# die's ground 29 mm from its line. Slots up to two thicknesses are real CPWs
# (the boards here run 0.4-1.2 thicknesses); beyond that the answer is "no".
MAX_GAP_PER_HEIGHT = 2.0


class ReliefChannelError(ValueError):
    """The lid's relief channel cannot carry a 50 ohm line of this trace.

    `kind` says why, so the layout check can name the parameter to change:

    * ``"shallow"``: even with the slots opened right up the line stays
      under the target, because the ceiling is too close. `min_height` is the
      shallowest cover that would reach it, when there is one.
    * ``"narrow"``: the 50 ohm gap exists, but the CPW it makes
      (trace + 2 gap) does not fit the channel with the walls clear of it.
      `gap` is that gap, `needed_width` the channel width it takes.
    """

    def __init__(self, message: str, kind: str, gap: Optional[float] = None,
                 needed_width: Optional[float] = None, min_height: Optional[float] = None):
        super().__init__(message)
        self.kind = kind
        self.gap = gap
        self.needed_width = needed_width
        self.min_height = min_height


def complete_elliptic_k(k: float) -> float:
    """Complete elliptic integral of the first kind, K(k).

    Computed by the arithmetic-geometric mean, which converges
    quadratically and needs nothing outside the standard library --
    scipy is not a dependency of this project and is not worth becoming
    one for a single special function.
    """
    if not 0.0 <= k < 1.0:
        raise ValueError(f"modulus k must be in [0, 1), got {k}")
    a, b = 1.0, math.sqrt(1.0 - k * k)
    for _ in range(60):
        if abs(a - b) < 1e-15 * abs(a):
            break
        a, b = 0.5 * (a + b), math.sqrt(a * b)
    return math.pi / (2.0 * a)


def _k_ratio(k: float) -> float:
    """K(k)/K(k'), the shape that every partial capacitance takes here."""
    return complete_elliptic_k(k) / complete_elliptic_k(math.sqrt(1.0 - k * k))


def sidewall_ratio(gap: float, conductor_thickness: float) -> float:
    """What a conductor of finite thickness adds to the air-side partial
    capacitance, in the units of `_k_ratio`: the parallel-plate field between
    the sidewalls across each slot, plus a fitted fringe at the top edges.

    Zero for a zero-thickness conductor, and it does not depend on the
    substrate -- the field is in the air above the board.
    """
    if conductor_thickness <= 0:
        return 0.0
    return conductor_thickness / gap + SIDEWALL_FRINGE * (
        1.0 - math.exp(-conductor_thickness / _FRINGE_METAL_SCALE_MM)
    )


def wall_clearance(cover_height: float) -> float:
    """How far outside the slot edge a channel wall has to stand, per side,
    for the cover to behave as a plane: half the relief depth, and never
    under 0.1 mm. Walls that far out move the impedance by under 1.5 %;
    the field reaches further from a higher cover, hence the depth.
    """
    return max(MIN_WALL_CLEARANCE_MM, 0.5 * cover_height)


def z0_gcpw(
    trace_width: float,
    gap: float,
    substrate_height: float,
    eps_r: float,
    conductor_thickness: float = 0.0,
    cover_height: Optional[float] = None,
) -> float:
    """Characteristic impedance in ohms of a conductor-backed CPW, with a
    metal cover over it if `cover_height` is given.

    `trace_width` is the centre conductor, `gap` the slot either side of
    it, `substrate_height` the dielectric thickness to the backing
    ground, `conductor_thickness` the metal thickness (0 for the ideal
    zero-thickness case), `cover_height` the distance from the top face of
    the metal to the cover's underside (None: nothing above, an open top)
    -- all in millimetres. The model and its limits are in the module
    docstring.
    """
    if min(trace_width, gap, substrate_height) <= 0:
        raise ValueError("trace_width, gap and substrate_height must all be positive")
    if conductor_thickness < 0:
        raise ValueError("conductor_thickness cannot be negative")
    if cover_height is not None and cover_height <= 0:
        raise ValueError(f"cover_height must be positive, got {cover_height}")

    a = trace_width / 2.0
    b = a + gap

    # The substrate half: the same ratio, mapped through the substrate depth.
    k_sub = math.tanh(math.pi * a / (2.0 * substrate_height)) / math.tanh(math.pi * b / (2.0 * substrate_height))
    # The air half: the bare aspect ratio when open, mapped through the
    # cover's height when covered. A cover close enough to saturate both
    # tanh terms is a short; the clamp keeps the modulus below 1 and the
    # impedance it returns is a low number, which the solve reads as such.
    if cover_height is None:
        k_air = a / b
    else:
        k_air = min(
            math.tanh(math.pi * a / (2.0 * cover_height)) / math.tanh(math.pi * b / (2.0 * cover_height)),
            1.0 - 1e-15,
        )

    r_sub = _k_ratio(k_sub)
    r_air = _k_ratio(k_air) + sidewall_ratio(gap, conductor_thickness)

    # The two partial capacitances add; the effective permittivity is
    # their dielectric-weighted mean.
    eps_eff = (r_air + eps_r * r_sub) / (r_air + r_sub)
    return _ETA0_OVER_2PI * math.pi / (math.sqrt(eps_eff) * (r_air + r_sub))


def _bisect_gap(trace_width, substrate_height, eps_r, z0, conductor_thickness,
                low, high, tolerance, cover_height):
    """The gap in [low, high] that gives z0, or None if z0 is not in the
    span of the bracket. Impedance rises monotonically with the gap
    (widening the slots lowers the capacitance to ground), so a bisection is
    both sufficient and unconditionally stable -- no derivatives, no
    starting guess to get wrong."""
    def z_of(gap):
        return z0_gcpw(trace_width, gap, substrate_height, eps_r, conductor_thickness, cover_height)

    if not z_of(low) <= z0 <= z_of(high):
        return None
    while high - low > tolerance:
        mid = 0.5 * (low + high)
        if z_of(mid) < z0:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


def min_cover_height(
    trace_width: float,
    substrate_height: float,
    eps_r: float,
    z0: float = TARGET_Z0_OHM,
    conductor_thickness: float = 0.0,
    max_gap: Optional[float] = None,
) -> Optional[float]:
    """The lowest cover, in millimetres, over which a trace of this width
    reaches `z0` with a gap of at most `max_gap` (by default the widest slot
    the solve looks for, `MAX_GAP_PER_HEIGHT` substrate thicknesses). None if
    it does not, even with the cover far away.

    A higher cover loads the line less, so the impedance a given gap gives
    only rises with it, and a bisection finds the crossing.
    """
    if max_gap is None:
        max_gap = MAX_GAP_PER_HEIGHT * substrate_height

    def reaches(height):
        return z0_gcpw(trace_width, max_gap, substrate_height, eps_r, conductor_thickness, height) >= z0

    top = 100.0
    if not reaches(top):
        return None
    low, high = 1e-3, top
    if reaches(low):
        return low
    while high - low > 1e-6:
        mid = 0.5 * (low + high)
        if reaches(mid):
            high = mid
        else:
            low = mid
    return high


def solve_gap_for_z0(
    trace_width: float,
    substrate_height: float,
    eps_r: float,
    z0: float = TARGET_Z0_OHM,
    conductor_thickness: float = 0.0,
    gap_bounds: Optional[tuple] = None,
    tolerance: float = 1e-9,
    *,
    cover_height: Optional[float] = None,
    channel_width: Optional[float] = None,
) -> float:
    """The gap, in millimetres, that gives `z0` for this trace width.

    With `cover_height` (the lid's relief depth, `route_clearence`) the line
    is solved with the cover over it. With `channel_width` too (`route_width`)
    the solve also checks that the channel holds the CPW it comes to: wide
    enough that its walls stand `wall_clearance` outside the slots, since a
    plane at the ceiling is only a fair stand-in for the channel when they do.
    Either failure raises `ReliefChannelError`; an open-top line that cannot
    reach `z0` raises a plain ValueError, as before.

    The gap is looked for between 0.1 um and `MAX_GAP_PER_HEIGHT` substrate
    thicknesses (`gap_bounds` overrides). A target that is only reached by a
    wider slot than that is refused as unreachable, rather than solved to a
    gap at which the line is no longer a CPW.
    """
    if channel_width is not None and cover_height is None:
        raise ValueError("channel_width needs a cover_height: a channel is a cut of some depth")

    low, high = gap_bounds if gap_bounds is not None else (1e-4, MAX_GAP_PER_HEIGHT * substrate_height)
    gap = _bisect_gap(trace_width, substrate_height, eps_r, z0, conductor_thickness,
                      low, high, tolerance, cover_height)
    if gap is None:
        z_low = z0_gcpw(trace_width, low, substrate_height, eps_r, conductor_thickness, cover_height)
        z_high = z0_gcpw(trace_width, high, substrate_height, eps_r, conductor_thickness, cover_height)
        message = (
            f"{z0} ohm is unreachable for trace_width={trace_width} mm on "
            f"h={substrate_height} mm, eps_r={eps_r}"
            + (f" under a cover {cover_height} mm above the metal" if cover_height is not None else "")
            + f": the gap range {low}..{high} mm spans only {z_low:.2f}..{z_high:.2f} ohm. "
            "Choose a different trace width."
        )
        if cover_height is not None and z_high < z0:
            reach = min_cover_height(trace_width, substrate_height, eps_r, z0, conductor_thickness)
            message += (
                f" The cover is the limit: it needs to be at least {reach * 1000:.0f} um above the "
                "trace for a slot of a usable width to reach it."
                if reach is not None else ""
            )
            raise ReliefChannelError(message, kind="shallow", min_height=reach)
        raise ValueError(message)

    if channel_width is not None:
        needed = trace_width + 2.0 * gap + 2.0 * wall_clearance(cover_height)
        if channel_width < needed:
            raise ReliefChannelError(
                f"the {gap * 1000:.0f} um gap that makes a {trace_width} mm trace {z0:g} ohm under a "
                f"cover {cover_height} mm above it makes a CPW {trace_width + 2.0 * gap:.3f} mm wide, "
                f"which needs a channel at least {needed:.3f} mm wide (its walls "
                f"{wall_clearance(cover_height):.2f} mm outside the slots); this one is "
                f"{channel_width} mm.",
                kind="narrow", gap=gap, needed_width=needed,
            )
    return gap


def permittivity_of(material_name: str) -> float:
    """eps_r for a material named in the run's material settings."""
    try:
        return PERMITTIVITY[material_name]
    except KeyError:
        raise KeyError(
            f"No permittivity known for material {material_name!r}. Add it to "
            f"cpw.PERMITTIVITY -- known: {sorted(PERMITTIVITY)}."
        ) from None
