"""
Characteristic impedance of a conductor-backed coplanar waveguide, and
the gap that makes it 50 ohm.

The layout generator needs a trace width *and* a gap, but only one of
them can be a free parameter if the line is to stay matched. The trace
width is the fabrication-limited dimension, so `default.txt` sets that
and the gap is solved for here -- once per substrate, because the PCB
(Rogers) and the chip (silicon) have different permittivities and so
need different gaps for the same width.

The impedance model is the standard conformal-mapping result for a CPW
with a ground plane on the far side of the substrate (Simons, *Coplanar
Waveguide Circuits, Components and Systems*): two partial capacitances,
one for the coplanar grounds and one for the backing plane, each a ratio
of complete elliptic integrals. Conductor thickness is accounted for by
the usual first-order correction, because 35 um of copper on a 100 um
gap is not a perturbation you can wave away -- it widens the trace and
narrows the slot, pulling the impedance down by several ohms.

What is still idealised: lossless conductors and an infinitely wide
substrate. So this picks the starting geometry and HFSS has the last
word, which is why the solved value is recorded per run. Altium's own
layer-stack field solver is a useful independent check on the same
number.

Everything here is in millimetres, like `default.txt` itself.
"""
from __future__ import annotations

import math
from typing import Dict

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
    "Rogers RT/duroid 6010/6010LM (tm)": 10.2,
    "FR4_epoxy": 4.4,
    "silicon": 11.9,
    "sapphire": 10.0,
    "vacuum": 1.0,
}

TARGET_Z0_OHM = 50.0

# Free-space impedance / 2pi, the leading constant of the CPW result.
_ETA0_OVER_2PI = 59.9585


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


def thickness_correction(trace_width: float, conductor_thickness: float) -> float:
    """How much a conductor of finite thickness effectively widens the
    trace, in millimetres.

    Real copper has sidewalls, and the field crossing the slot sees them.
    The standard first-order treatment folds that into the geometry: the
    trace behaves as if it were wider by this amount and the gap
    correspondingly narrower, both by the same delta.
    """
    if conductor_thickness <= 0:
        return 0.0
    return (1.25 * conductor_thickness / math.pi) * (
        1.0 + math.log(4.0 * math.pi * trace_width / conductor_thickness)
    )


def z0_gcpw(
    trace_width: float,
    gap: float,
    substrate_height: float,
    eps_r: float,
    conductor_thickness: float = 0.0,
) -> float:
    """Characteristic impedance in ohms of a conductor-backed CPW.

    `trace_width` is the centre conductor, `gap` the slot either side of
    it, `substrate_height` the dielectric thickness to the backing
    ground, `conductor_thickness` the metal thickness (0 for the ideal
    zero-thickness case) -- all in millimetres.
    """
    if min(trace_width, gap, substrate_height) <= 0:
        raise ValueError("trace_width, gap and substrate_height must all be positive")
    if conductor_thickness < 0:
        raise ValueError("conductor_thickness cannot be negative")

    delta = thickness_correction(trace_width, conductor_thickness)
    if delta >= gap:
        raise ValueError(
            f"conductor_thickness={conductor_thickness} mm is too large for a "
            f"{gap} mm gap: the thickness correction ({delta:.4f} mm) closes the "
            "slot entirely. Widen the gap or thin the metal."
        )
    trace_width = trace_width + delta
    gap = gap - delta

    a = trace_width / 2.0
    b = a + gap

    # Coplanar grounds: the modulus is the bare aspect ratio.
    k1 = a / b
    # Backing plane: the same ratio, mapped through the substrate depth.
    k3 = math.tanh(math.pi * a / (2.0 * substrate_height)) / math.tanh(math.pi * b / (2.0 * substrate_height))

    r1 = _k_ratio(k1)
    r3 = _k_ratio(k3)

    # The two partial capacitances add; the effective permittivity is
    # their dielectric-weighted mean.
    eps_eff = (1.0 + eps_r * (r3 / r1)) / (1.0 + (r3 / r1))
    return _ETA0_OVER_2PI * math.pi / (math.sqrt(eps_eff) * (r1 + r3))


def solve_gap_for_z0(
    trace_width: float,
    substrate_height: float,
    eps_r: float,
    z0: float = TARGET_Z0_OHM,
    conductor_thickness: float = 0.0,
    gap_bounds: tuple = (1e-4, 100.0),
    tolerance: float = 1e-9,
) -> float:
    """The gap, in millimetres, that gives `z0` for this trace width.

    Impedance rises monotonically with the gap (widening the slots
    lowers the capacitance to ground), so a bisection over the bracket
    is both sufficient and unconditionally stable -- no derivatives, no
    starting guess to get wrong.
    """
    low, high = gap_bounds
    # A gap at or below the thickness correction is not a real geometry,
    # so the bracket starts just clear of it rather than at gap_bounds[0].
    low = max(low, thickness_correction(trace_width, conductor_thickness) * 1.000001)
    z_low = z0_gcpw(trace_width, low, substrate_height, eps_r, conductor_thickness)
    z_high = z0_gcpw(trace_width, high, substrate_height, eps_r, conductor_thickness)
    if not z_low <= z0 <= z_high:
        raise ValueError(
            f"{z0} ohm is unreachable for trace_width={trace_width} mm on "
            f"h={substrate_height} mm, eps_r={eps_r}: the gap range "
            f"{low}..{high} mm spans only {z_low:.2f}..{z_high:.2f} ohm. "
            "Choose a different trace width."
        )

    while high - low > tolerance:
        mid = 0.5 * (low + high)
        if z0_gcpw(trace_width, mid, substrate_height, eps_r, conductor_thickness) < z0:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


def permittivity_of(material_name: str) -> float:
    """eps_r for a material named in the run's material settings."""
    try:
        return PERMITTIVITY[material_name]
    except KeyError:
        raise KeyError(
            f"No permittivity known for material {material_name!r}. Add it to "
            f"cpw.PERMITTIVITY -- known: {sorted(PERMITTIVITY)}."
        ) from None
