"""Band means and a time-domain reflection read of stored driven results.

Read-only: takes the complex S11 and S21 a DrivenModal run already wrote to
``runs/<run>/models/<combo>/results.csv`` (``s11_db``/``s11_deg``,
``s21_db``/``s21_deg``) and reports, per run:

* the mean S11 in dB in the four bands the mismatch questions use, and
* a windowed inverse FFT of S11 (a bandpass TDR): where the reflection sits
  in time, and so along the line, with the one-way delay taken from the
  S21 phase slope.

    python -m microwave_package.hfss.driven.s11_analysis --runs-dir D:/microwave-package/runs

Each band keeps the convention of the notes: [2,10] and [10,17] include both
ends, [17,20) and [20,26) do not include the top edge.
"""
from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

C_MM_PER_NS = 299.792458

# (low GHz, high GHz, include high edge)
BANDS: Tuple[Tuple[float, float, bool], ...] = (
    (2.0, 10.0, True),
    (10.0, 17.0, True),
    (17.0, 20.0, False),
    (20.0, 26.0, False),
)

# The TDR zero-pads the 97 points onto this many bins at 0.25 GHz; the
# unambiguous window is then 1 / 0.25 GHz = 4 ns, far longer than any delay here.
TDR_BINS = 8192


@dataclass(frozen=True)
class Case:
    """One stored result. Geometry is what the run's notes record; gap and
    pad-to-ground come from RESULTS_CONCLUSIONS.md / .handoff, not from the
    run folder, which does not store the solved gap."""

    label: str
    run: str
    combo: str
    coarse_only: bool
    board: str
    eps_r: float
    trace_mm: float
    gap_um: Optional[float]
    lid: str
    launch_clearance_mm: Optional[float]
    chip: bool


CASES: Tuple[Case, ...] = (
    Case("TMM old lid, bare", "run_20261009_191045", "default", True, "TMM 10i", 9.8,
         0.2, 271.0, "0.5 x 0.04 mm (old)", None, False),
    Case("TMM old lid, chip", "run_20261009_204719", "default", False, "TMM 10i", 9.8,
         0.2, 271.0, "0.5 x 0.04 mm (old)", None, True),
    Case("RO3003 bare", "run_20261010_215132", "default", False, "RO3003", 3.0,
         0.4, 133.0, "1.0 x 0.2 mm", None, False),
    Case("RO4003 bare", "run_20261010_223533", "default", False, "RO4003", 3.55,
         0.4, 153.0, "1.0 x 0.2 mm", None, False),
    Case("TMM launch 120 um", "run_20261011_154407", "launch_clearance-120um", False,
         "TMM 10i", 9.8, 0.2, 247.0, "1.2 x 0.3 mm", 0.12, False),
    Case("TMM launch 200 um", "run_20261011_154407", "launch_clearance-200um", False,
         "TMM 10i", 9.8, 0.2, 247.0, "1.2 x 0.3 mm", 0.20, False),
    Case("TMM launch 300 um", "run_20261011_154407", "launch_clearance-300um", False,
         "TMM 10i", 9.8, 0.2, 247.0, "1.2 x 0.3 mm", 0.30, False),
)


@dataclass
class Sweep:
    freq_ghz: np.ndarray
    s11: np.ndarray  # complex
    s21: np.ndarray  # complex


def s_complex(db: float, degrees: float) -> complex:
    """Complex S-parameter from magnitude in dB and phase in degrees (the same
    conversion as hfss.line.impedance.s_complex, inlined to keep pyaedt out)."""
    return complex(10.0 ** (db / 20.0) * math.cos(math.radians(degrees)),
                   10.0 ** (db / 20.0) * math.sin(math.radians(degrees)))


def load_sweep(run_dir: Path, combo: str, coarse_only: bool) -> Sweep:
    """Read the driven rows of one combo. ``coarse_only`` drops the adaptive
    ``Fine_n`` refinement rows, which sit on a non-uniform grid."""
    path = run_dir / "models" / combo / "results.csv"
    freq, s11, s21 = [], [], []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if coarse_only and row.get("sweep", "coarse") != "coarse":
                continue
            freq.append(float(row["freq_ghz"]))
            s11.append(s_complex(float(row["s11_db"]), float(row["s11_deg"])))
            s21.append(s_complex(float(row["s21_db"]), float(row["s21_deg"])))
    order = np.argsort(freq)
    return Sweep(np.asarray(freq)[order], np.asarray(s11)[order], np.asarray(s21)[order])


def in_band(freq: np.ndarray, band: Tuple[float, float, bool]) -> np.ndarray:
    lo, hi, closed = band
    return (freq >= lo) & ((freq <= hi) if closed else (freq < hi))


def band_means_db(sweep: Sweep) -> List[Tuple[float, int]]:
    """Mean of s11 in dB over each band, with the point count."""
    s11_db = 20.0 * np.log10(np.abs(sweep.s11))
    out = []
    for band in BANDS:
        mask = in_band(sweep.freq_ghz, band)
        out.append((float(np.mean(s11_db[mask])), int(mask.sum())))
    return out


def impulse_response(values: np.ndarray, freq_ghz: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Windowed inverse FFT of a band of complex values (S11 or S21).

    Returns (time_ns, |response|). The 97 points go on their true bins
    (f / df), zero elsewhere, so the time axis is the real delay and not an
    envelope. A Kaiser window (beta 6) tapers the band edges. Scaled so that a
    lone reflection of coefficient G shows as G at its delay.
    """
    df = float(np.median(np.diff(freq_ghz)))
    bins = np.rint(freq_ghz / df).astype(int)
    window = np.kaiser(len(freq_ghz), 6.0)
    spectrum = np.zeros(TDR_BINS, dtype=complex)
    spectrum[bins] = values * window
    response = np.abs(np.fft.ifft(spectrum)) * TDR_BINS / np.sum(window)
    t_ns = np.arange(TDR_BINS) / (TDR_BINS * df)
    return t_ns, response


def to_distance_mm(t_ns: np.ndarray, eps_eff: float) -> np.ndarray:
    """Round-trip time to one-way distance along a line of effective permittivity."""
    return C_MM_PER_NS * t_ns / 2.0 / math.sqrt(eps_eff)


def top_peaks(t_ns: np.ndarray, response: np.ndarray, eps_eff: float,
              count: int = 4, max_ns: float = 2.0) -> List[Tuple[float, float, float]]:
    """The largest local maxima up to max_ns: (time ns, distance mm, |G|)."""
    window = (t_ns > 0.0) & (t_ns <= max_ns)
    inner = np.zeros_like(response, dtype=bool)
    inner[1:-1] = (response[1:-1] > response[:-2]) & (response[1:-1] >= response[2:])
    idx = np.flatnonzero(inner & window)
    idx = idx[np.argsort(response[idx])[::-1]][:count]
    return [(float(t_ns[i]), float(to_distance_mm(t_ns[i:i + 1], eps_eff)[0]),
             float(response[i])) for i in sorted(idx)]


def launch_magnitude(t_ns: np.ndarray, response: np.ndarray, eps_eff: float,
                     limit_mm: float = 3.0) -> float:
    """Largest |G| within limit_mm of the connector reference plane."""
    near = to_distance_mm(t_ns, eps_eff) < limit_mm
    return float(np.max(np.where(near, response, 0.0)))


def resolution_mm(eps_eff: float, bandwidth_ghz: float = 24.0) -> float:
    """Distance resolution of a 2-26 GHz band, one-way, rectangular window."""
    return C_MM_PER_NS / (2.0 * bandwidth_ghz * math.sqrt(eps_eff))


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    print("## Band mean S11 (dB), coarse grid, 0.25 GHz")
    print("| case | run | combo | board | trace mm | gap um | lid | launch mm | chip | "
          "2-10 | 10-17 | 17-20 | 20-26 | points |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    sweeps: Dict[str, Sweep] = {}
    for case in CASES:
        sweep = load_sweep(args.runs_dir / case.run, case.combo, case.coarse_only)
        sweeps[case.label] = sweep
        means = band_means_db(sweep)
        gap = "n/a" if case.gap_um is None else f"{case.gap_um:.0f}"
        launch = "n/a" if case.launch_clearance_mm is None else f"{case.launch_clearance_mm:.2f}"
        cells = " | ".join(f"{m:.2f}" for m, _ in means)
        pts = "/".join(str(n) for _, n in means)
        print(f"| {case.label} | {case.run} | {case.combo} | {case.board} | {case.trace_mm} | "
              f"{gap} | {case.lid} | {launch} | {'yes' if case.chip else 'no'} | {cells} | {pts} |")

    print()
    print("## TDR of S11 (windowed IFFT); distance uses eps_eff = (er+1)/2 (assumed)")
    print("| case | eps_eff | er | resolution mm | launch \\|G\\| (<3 mm) | top S11 peaks: ns (mm) \\|G\\| |")
    print("|---|---|---|---|---|---|")
    for case in CASES:
        eps_eff = (case.eps_r + 1.0) / 2.0
        t_ns, response = impulse_response(sweeps[case.label].s11, sweeps[case.label].freq_ghz)
        peaks = "; ".join(f"{t:.3f} ns ({d:.1f} mm) {g:.3f}"
                          for t, d, g in top_peaks(t_ns, response, eps_eff))
        print(f"| {case.label} | {eps_eff:.2f} | {case.eps_r} | {resolution_mm(eps_eff):.1f} | "
              f"{launch_magnitude(t_ns, response, eps_eff):.3f} | {peaks} |")

    print()
    print("## Check only: S21 impulse peak (ns), not a line-delay measure")
    print("| case | S21 peak ns \\|g\\| |")
    print("|---|---|")
    for case in CASES:
        t_ns, response = impulse_response(sweeps[case.label].s21, sweeps[case.label].freq_ghz)
        window = (t_ns > 0.02) & (t_ns <= 2.0)
        i = int(np.argmax(np.where(window, response, 0.0)))
        print(f"| {case.label} | {t_ns[i]:.3f} {response[i]:.4f} |")


if __name__ == "__main__":
    main()
