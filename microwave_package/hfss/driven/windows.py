"""
Fine frequency windows for the driven sweep.

A box mode has a Q of some tens to some hundreds, so its dip in S21 is
f/Q wide -- 25-35 MHz for the modes this package sees -- while the coarse
sweep reports a point every ~100 MHz. Between the sweep's own samples the
interpolation draws a dip that is only as deep as the nearest sample
happens to be, which is the likely reason dip DEPTH did not reproduce
between runs while dip POSITION did (see RESULTS_CONCLUSIONS.md).

So the coarse sweep stays, and each eigenmode below the top of the band
gets a narrow window of its own, a few points per linewidth across:

    half-width = halfwidth_linewidths * f/Q   (default 0.5 -> f/(2Q))
                 + margin_percent of f        (the eigenfrequency is a hint)
    step       = f/Q / points_per_linewidth   (default 20 -> f/(20Q))

The eigenmode solve has no connectors, so the driven resonance sits a
little off the eigenfrequency; the margin is what keeps the dip inside
its window. Overlapping windows merge, and a total point cap coarsens
every step by the same factor rather than dropping modes.

Pure arithmetic, with no AEDT in it, so it is all testable offline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Names the extra sweeps in the driven setup. Sweep_1 (setup.SWEEP_NAME)
# is the coarse one; results.py reads every sweep whose name starts here.
FINE_SWEEP_PREFIX = "Fine_"

# Fewest points a window may be thinned to by the cap: the two ends and a
# middle. Below that it is not a window.
MIN_WINDOW_POINTS = 3


@dataclass(frozen=True)
class Window:
    """One fine window, in GHz."""
    lo: float
    hi: float
    step: float

    @property
    def points(self) -> int:
        return max(int(math.floor((self.hi - self.lo) / self.step + 1e-9)) + 1, MIN_WINDOW_POINTS)


def total_points(windows: Iterable[Window]) -> int:
    return sum(w.points for w in windows)


def _usable_q(q: Any, fallback: float) -> float:
    try:
        value = float(q)
    except (TypeError, ValueError):
        return fallback
    # PEC walls give an infinite or enormous Q, and a missing one reads
    # back as NaN: neither says anything about the DRIVEN linewidth, which
    # the ports and the connector loss set.
    return value if math.isfinite(value) and value > 0 else fallback


def windows_from_modes(
    modes: Sequence[Tuple[float, Any]],
    f_start_ghz: float,
    f_stop_ghz: float,
    halfwidth_linewidths: float = 0.5,
    points_per_linewidth: float = 20.0,
    margin_percent: float = 1.0,
    fallback_q: float = 200.0,
) -> List[Window]:
    """One window per eigenmode that falls inside the sweep band.

    `modes` is `(freq_ghz, q)` pairs. A mode above `f_stop_ghz` is left
    out -- the sweep does not look there -- and a window is clipped to the
    band rather than reaching outside it.
    """
    if halfwidth_linewidths <= 0 or points_per_linewidth <= 0:
        raise ValueError("The fine-window half-width and points per linewidth must be positive.")
    windows = []
    for f, q in modes:
        try:
            f = float(f)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(f) or f <= 0 or f >= f_stop_ghz:
            continue
        q = _usable_q(q, fallback_q)
        linewidth = f / q
        half = halfwidth_linewidths * linewidth + f * margin_percent / 100.0
        lo, hi = max(f - half, f_start_ghz), min(f + half, f_stop_ghz)
        if hi <= lo:
            continue
        windows.append(Window(lo, hi, linewidth / points_per_linewidth))
    return windows


def parse_manual(text: Optional[str]) -> List[Window]:
    """Windows typed by hand: `lo:hi:step` in GHz, separated by `;` or
    newlines -- the same shape as a swept parameter's `start:stop:step`."""
    windows = []
    for item in (text or "").replace("\n", ";").split(";"):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        try:
            if len(parts) != 3:
                raise ValueError
            lo, hi, step = (float(p) for p in parts)
        except ValueError:
            raise ValueError(
                f"Fine window {item!r} is not lo:hi:step in GHz (for example 12.1:12.4:0.002)."
            ) from None
        if not (0 < lo < hi) or step <= 0:
            raise ValueError(f"Fine window {item!r} needs 0 < lo < hi and a positive step.")
        windows.append(Window(lo, hi, step))
    return windows


def merge(windows: Iterable[Window]) -> List[Window]:
    """Overlapping or touching windows become one, at the finer step."""
    merged: List[Window] = []
    for w in sorted(windows, key=lambda w: w.lo):
        if merged and w.lo <= merged[-1].hi:
            last = merged[-1]
            merged[-1] = Window(last.lo, max(last.hi, w.hi), min(last.step, w.step))
        else:
            merged.append(w)
    return merged


def cap_points(windows: List[Window], max_points: int) -> List[Window]:
    """Coarsen every step by one common factor until the total fits.

    Dropping windows would silently lose modes; a coarser step loses only
    resolution, and does so evenly. If even the minimum per window does
    not fit, the windows are returned as thin as they can go.
    """
    if max_points <= 0 or total_points(windows) <= max_points:
        return windows
    floor_total = MIN_WINDOW_POINTS * len(windows)
    if floor_total >= max_points:
        return [Window(w.lo, w.hi, (w.hi - w.lo) / (MIN_WINDOW_POINTS - 1)) for w in windows]
    scale = total_points(windows) / max_points
    while True:
        thinned = [Window(w.lo, w.hi, w.step * scale) for w in windows]
        if total_points(thinned) <= max_points:
            return thinned
        scale *= 1.02


def plan(
    settings: Dict[str, Any],
    modes: Sequence[Tuple[float, Any]] = (),
) -> List[Window]:
    """Every fine window a driven setup should get, from its settings.

    The automatic windows (when `fine_windows` is "auto" and there are
    eigenmodes to centre them on) and the hand-typed ones go through the
    same merge and the same point cap.
    """
    f_start = float(settings["f_start_ghz"])
    f_stop = float(settings["f_stop_ghz"])
    windows = parse_manual(settings.get("fine_windows_manual"))
    if settings.get("fine_windows", "auto") == "auto":
        windows += windows_from_modes(
            modes, f_start, f_stop,
            halfwidth_linewidths=float(settings.get("fine_halfwidth_linewidths", 0.5)),
            points_per_linewidth=float(settings.get("fine_points_per_linewidth", 20.0)),
            margin_percent=float(settings.get("fine_margin_percent", 1.0)),
            fallback_q=float(settings.get("fine_q_fallback", 200.0)),
        )
    windows = [Window(max(w.lo, f_start), min(w.hi, f_stop), w.step) for w in windows
               if w.hi > f_start and w.lo < f_stop]
    return cap_points(merge(windows), int(settings.get("fine_max_points", 2000)))


def mesh_frequency_ghz(settings: Dict[str, Any], modes: Sequence[Tuple[float, Any]] = ()) -> float:
    """Where the adaptive mesh is refined.

    The top of the band by default. "highest_mode" refines at just above
    the highest eigenmode inside the band instead -- cheaper when the
    sweep reaches well past the modes of interest, at the cost of a mesh
    that is coarser than the shortest wavelength the sweep asks about.
    """
    f_stop = float(settings["f_stop_ghz"])
    if settings.get("adapt_at", "f_stop") != "highest_mode":
        return f_stop
    inside = [float(f) for f, _q in modes if 0 < float(f) < f_stop]
    if not inside:
        return f_stop
    margin = float(settings.get("fine_margin_percent", 1.0)) / 100.0
    return min(max(inside) * (1.0 + margin), f_stop)
