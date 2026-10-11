"""
The numbers the line-impedance check reports, from the section's S-parameters.

Two ways to get a characteristic impedance out of a solved section, and the
check records both so that one can referee the other:

* the PORT's own Zo, which HFSS computes from the mode on the port face
  (Zpi, Zpv or Zvi depending on the definition -- see `build.py` for which
  the port is asked for), when the solved design reports it; and
* the impedance DERIVED from the section's two-port S-parameters, with the
  ports renormalised to a known reference. For a uniform, reciprocal,
  symmetric section the ABCD parameters give

      Zc = Zref * sqrt( ((1 + S11)^2 - S21^2) / ((1 - S11)^2 - S21^2) )

  which needs nothing from HFSS's report names and does not depend on how
  the port defines its voltage -- only on the section being a piece of
  uniform line. A short section with ports that do not match its modes
  breaks that assumption, which shows up as a Zc that wanders with frequency
  at a rate no transmission line has.
"""
from __future__ import annotations

import cmath
import math
from typing import Dict, Iterable, List, Optional

DEFAULT_TARGET_OHM = 50.0
DEFAULT_TOLERANCE_OHM = 2.5

# The ABCD inversion divides by (1 - S21^2), which vanishes where the section
# is a whole number of half-wavelengths long (S21 = +-1): there a hair of S11
# noise becomes ohms. A 6 mm section on 5880 did this at 20 GHz (Zo read 35
# ohm in a line that read 51 either side). Points under this are not reported;
# the section length is chosen so none of the band is.
ILL_CONDITIONED_BELOW = 0.1


def s_complex(db: float, degrees: float) -> complex:
    """A complex S-parameter from its magnitude in dB and phase in degrees."""
    return cmath.rect(10.0 ** (db / 20.0), math.radians(degrees))


def zc_from_sparams(s11: complex, s21: complex, z_ref: float = DEFAULT_TARGET_OHM) -> complex:
    """The characteristic impedance of a uniform two-port from S11 and S21
    (reference impedance `z_ref`). The root with a positive real part.

    A perfectly matched section (S11 = 0) comes back as `z_ref`; a section
    with S21 = 0 (nothing gets through) is not a line, and raises
    ValueError rather than reporting an impedance for it.
    """
    numerator = (1.0 + s11) ** 2 - s21 ** 2
    denominator = (1.0 - s11) ** 2 - s21 ** 2
    if abs(denominator) < 1e-12:
        raise ValueError("no impedance: the section is an open or a short (denominator 0)")
    if abs(s21) < 1e-9:
        raise ValueError("no impedance: nothing is transmitted through the section")
    if abs(1.0 - s21 ** 2) < ILL_CONDITIONED_BELOW:
        raise ValueError("ill-conditioned: the section is about a half-wavelength long here (or a "
                         "whole number of them), where S21 = +-1 and the impedance cannot be derived")
    zc = z_ref * cmath.sqrt(numerator / denominator)
    return zc if zc.real >= 0 else -zc


def derive_rows(rows: Iterable[Dict[str, float]], z_ref: float = DEFAULT_TARGET_OHM) -> List[Dict[str, float]]:
    """Add `z0_ohm` (real part), `z0_im_ohm` and `z0_abs_ohm` -- derived from
    S11 and S21 -- to each row that has them. A row the derivation cannot
    handle gets `z0_error` instead."""
    out = []
    for row in rows:
        row = dict(row)
        try:
            zc = zc_from_sparams(
                s_complex(row["s11_db"], row["s11_deg"]), s_complex(row["s21_db"], row["s21_deg"]), z_ref)
            row["z0_ohm"], row["z0_im_ohm"], row["z0_abs_ohm"] = zc.real, zc.imag, abs(zc)
        except (KeyError, ValueError) as exc:
            row["z0_error"] = str(exc)
        out.append(row)
    return out


def evaluate(rows: Iterable[Dict[str, float]], target: float = DEFAULT_TARGET_OHM,
             tolerance: float = DEFAULT_TOLERANCE_OHM, column: str = "z0_ohm") -> Dict[str, object]:
    """Pass or fail a line against `target` +- `tolerance` ohm.

    A line passes when EVERY frequency's impedance is inside the window --
    one frequency outside is a mismatch somewhere in the band. Also
    reports the extremes, the mean and the share of the band inside, which
    is how far a failing line is from passing.
    """
    points = [(r["freq_ghz"], r[column]) for r in rows if r.get(column) is not None
              and not (isinstance(r.get(column), float) and math.isnan(r[column]))]
    if not points:
        return {"passed": False, "points": 0, "reason": "no impedance could be read from the solve"}
    values = [z for _f, z in points]
    inside = [abs(z - target) <= tolerance for z in values]
    worst_f, worst_z = max(points, key=lambda p: abs(p[1] - target))
    return {
        "passed": all(inside),
        "points": len(points),
        "target_ohm": target,
        "tolerance_ohm": tolerance,
        "mean_ohm": sum(values) / len(values),
        "min_ohm": min(values),
        "max_ohm": max(values),
        "worst_ohm": worst_z,
        "worst_freq_ghz": worst_f,
        "share_in_tolerance": sum(inside) / len(inside),
    }


def gap_to_reach(target: float, measured: float, gap: float, trace: float,
                 slope_ohm_per_mm: Optional[float] = None) -> Optional[float]:
    """A rough gap correction: where the gap would have to go to move the
    measured impedance onto the target, by a first-order guess.

    A wider gap raises the impedance of a CPW, so a line that reads low
    wants a wider gap and one that reads high a narrower one. Without a
    measured slope the guess is that the impedance is proportional to the
    gap's share of the pitch (trace + 2 gap), which is crude but has the
    right sign; the point is the direction and the size of the miss, not
    a design value.
    """
    if measured <= 0 or gap <= 0:
        return None
    if slope_ohm_per_mm:
        return max(gap + (target - measured) / slope_ohm_per_mm, 0.0)
    return gap * (target / measured)
