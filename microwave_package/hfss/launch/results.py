"""
Reading the launch study back out of the solved project.

One row per frequency (S11 in dB and degrees, the same columns every driven
run has, so the run page's plots and Smith chart read them), plus the real and
imaginary parts of S11. The launch is judged by a handful of numbers
(`summarize`), written beside the project as `launch_summary.json`, and its
S11 is also written as `smith.csv` (frequency, re, im) for any other tool.

The time-domain read is the one `hfss/driven/s11_analysis.py` applies to the
full runs, so the near-launch peak here can be set against the near peak of a
full run: same window, same permittivity assumption, (er + 1) / 2.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
from typing import Any, Dict, List, Optional

import numpy as np

from ..driven import results as driven_results, s11_analysis as tdr
from . import build

logger = logging.getLogger("microwave_package")

SUMMARY_FILE = "launch_summary.json"
SMITH_FILE = "smith.csv"
# Bands the S11 means are quoted in (GHz): the full runs' own, up to this sweep's top.
BANDS = ((2.0, 10.0, True), (10.0, 17.0, True), (17.0, 20.0, True))


def _record(out_dir: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(out_dir, build.RECORD_FILE), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def s11_of(rows: List[Dict[str, Any]]):
    """(frequencies in GHz, complex S11), sorted, from rows with s11_db / s11_deg."""
    pairs = sorted(
        (float(r["freq_ghz"]), tdr.s_complex(float(r["s11_db"]), float(r["s11_deg"])))
        for r in rows if r.get("s11_db") is not None and r.get("s11_deg") is not None)
    return np.array([f for f, _ in pairs]), np.array([s for _, s in pairs], dtype=complex)


def summarize(freq: np.ndarray, s11: np.ndarray, eps_r: Optional[float]) -> Dict[str, Any]:
    """The numbers a launch is compared by."""
    out: Dict[str, Any] = {"points": int(len(freq))}
    if not len(freq):
        return out
    db = 20.0 * np.log10(np.maximum(np.abs(s11), 1e-12))
    bands = {}
    for lo, hi, closed in BANDS:
        mask = tdr.in_band(freq, (lo, hi, closed))
        if mask.any():
            bands[f"{lo:g}-{hi:g}"] = float(np.mean(db[mask]))
    out["s11_mean_db"] = bands
    worst = int(np.argmax(db))
    out["s11_max_db"] = float(db[worst])
    out["s11_max_at_ghz"] = float(freq[worst])
    out["s11_at_ghz"] = {f"{g:g}": float(np.interp(g, freq, db))
                         for g in (5.0, 10.0, 15.0, 17.0, 20.0) if freq[0] <= g <= freq[-1]}

    steps = np.diff(freq)
    if len(freq) < 8 or not np.allclose(steps, steps[0], atol=1e-6) or freq[0] < steps[0] * 0.5:
        out["tdr_note"] = "the frequency grid is not uniform from the origin, so no time-domain read"
        return out
    eps_eff = ((eps_r or 1.0) + 1.0) / 2.0
    t_ns, response = tdr.impulse_response(s11, freq)
    peaks = tdr.top_peaks(t_ns, response, eps_eff, count=3, max_ns=1.0)
    out["tdr_eps_eff_assumed"] = eps_eff
    out["tdr_peaks"] = [{"t_ns": t, "distance_mm": d, "gamma": g} for t, d, g in peaks]
    out["launch_gamma_3mm"] = tdr.launch_magnitude(t_ns, response, eps_eff, 3.0)
    out["tdr_resolution_mm"] = tdr.resolution_mm(eps_eff, float(freq[-1] - freq[0]))
    return out


def read_results(hfss, setup_names, task, out_dir: str, log) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for setup_name in setup_names:
        setup = hfss.get_setup(setup_name)
        for row in driven_results.get_sparameter_results(hfss, setup, log, out_dir=out_dir):
            row["setup"] = setup_name
            rows.append(row)
    for row in rows:
        if row.get("s11_db") is not None and row.get("s11_deg") is not None:
            value = tdr.s_complex(float(row["s11_db"]), float(row["s11_deg"]))
            row["s11_re"], row["s11_im"] = value.real, value.imag

    record = _record(out_dir)
    freq, s11 = s11_of(rows)
    summary = {**{k: record.get(k) for k in (
        "trace_width_mm", "gap_mm", "pad_width_mm", "pad_length_mm", "taper_length_mm",
        "void_radius_mm", "bottom_cutout_radius_mm", "launch_clearance_mm", "relief_depth_mm", "launch_relief_depth_mm",
        "substrate", "eps_r", "board_length_mm")}, **summarize(freq, s11, record.get("eps_r"))}
    with open(os.path.join(out_dir, SUMMARY_FILE), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, default=str)
    with open(os.path.join(out_dir, SMITH_FILE), "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["freq_ghz", "s11_re", "s11_im"])
        for f, s in zip(freq, s11):
            writer.writerow([f"{f:.6g}", f"{s.real:.8g}", f"{s.imag:.8g}"])
    peak = (summary.get("tdr_peaks") or [{}])[0]
    log(f"Launch S11: max {summary.get('s11_max_db', float('nan')):.1f} dB at "
        f"{summary.get('s11_max_at_ghz', float('nan')):g} GHz; band means {summary.get('s11_mean_db')}; "
        f"near-launch |G| {summary.get('launch_gamma_3mm', float('nan')):.3f}"
        + (f"; first peak {peak.get('distance_mm', float('nan')):.2f} mm." if peak else "."))
    return rows
