"""
Validation of workflow/tdr.py on dummy lines whose mismatch positions and
impedances are known exactly.

The dummy S-parameters are exact frequency-domain ABCD cascades of uniform
lossless sections between 50 ohm ports (a constant loss can be added). They are
built here, independently of the time-domain code in tdr.py, so the check is
not circular. The line is dispersion-free with one eps_eff, the assumption the
calculator makes.

    python -m microwave_package.workflow.tdr_validation --out <dir>

writes one overlay PNG per case and method (true Z(x), naive TDR, the profile)
and a Markdown table of the numbers. The unit tests assert the same criteria.

Pass criteria (fixed before the results were seen; not loosened afterwards)
----------------------------------------------------------------------------
Generous band (0.05-100 GHz, 2000 points):
  SHARP   both edges within 0.5 resolution of the true positions; plateau mean
          within 5 % of 500 ohm; outer parts (50 ohm, more than one resolution
          from each edge) within 3 % everywhere (max deviation).
  GRADUAL RMS error of Z(x), over the line excluding one resolution at each end,
          <= 5 % of the Z range (100 ohm); no detection with |r| >= 0.03.
  CORNERS every feature found within 1 resolution of its centre; no detection
          with |r| >= 0.03 that is not a feature.
Real band (0.1-20 GHz, 401 points), and the two extra cases (2 GHz start, small
loss), at the looser limits:
  SHARP   edges within 1 resolution; plateau mean within 10 %.
  GRADUAL RMS <= 8 % of the range.
  CORNERS features more than one resolution apart found within 1 resolution;
          a pair closer than one resolution reported as one, with a note.

Resolution: the 10-90 % rise time of the windowed step from f_max, times v/2
(tdr.rise_time_ns). Distances are measured from port 1 (x = 0).
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import tdr

C_MM_PER_NS = 299.792458
Z0 = 50.0
GRADUAL_RANGE_OHM = 150.0 - 50.0
MISMATCH_THRESHOLD = 0.03
LOSS_DB_PER_CM = 0.1  # the "small loss" case: a constant attenuation
METHODS = ("peel", "fit")


@dataclass(frozen=True)
class Section:
    z_ohm: float
    length_mm: float


@dataclass(frozen=True)
class DummyLine:
    """Uniform lossless sections from port 1 to port 2, between 50 ohm ports."""

    name: str
    sections: Tuple[Section, ...]
    eps_eff: float = 2.0
    z0: float = Z0
    alpha_np_per_mm: float = 0.0

    @property
    def length_mm(self) -> float:
        return float(sum(s.length_mm for s in self.sections))

    def true_z(self, x_mm) -> np.ndarray:
        """True impedance at distance x from port 1 (piecewise constant)."""
        bounds = np.concatenate(([0.0], np.cumsum([s.length_mm for s in self.sections])))
        values = np.asarray([s.z_ohm for s in self.sections], dtype=float)
        x = np.atleast_1d(np.asarray(x_mm, dtype=float))
        index = np.clip(np.searchsorted(bounds, x, side="right") - 1, 0, len(values) - 1)
        return values[index]

    def s_parameters(self, freq_ghz) -> np.ndarray:
        """(N, 2, 2) S-matrix [[S11, S12], [S21, S22]] from the ABCD cascade."""
        f = np.asarray(freq_ghz, dtype=float).ravel()
        total = np.broadcast_to(np.eye(2, dtype=complex), (f.size, 2, 2)).copy()
        beta_per_mm = 2.0 * math.pi * f * math.sqrt(self.eps_eff) / C_MM_PER_NS
        for section in self.sections:
            theta = (self.alpha_np_per_mm + 1j * beta_per_mm) * section.length_mm
            ch, sh = np.cosh(theta), np.sinh(theta)
            block = np.empty((f.size, 2, 2), dtype=complex)
            block[:, 0, 0] = ch
            block[:, 0, 1] = section.z_ohm * sh
            block[:, 1, 0] = sh / section.z_ohm
            block[:, 1, 1] = ch
            total = total @ block
        a, b = total[:, 0, 0], total[:, 0, 1]
        c, d = total[:, 1, 0], total[:, 1, 1]
        z0 = self.z0
        den = a + b / z0 + c * z0 + d
        det = a * d - b * c
        s = np.empty((f.size, 2, 2), dtype=complex)
        s[:, 0, 0] = (a + b / z0 - c * z0 - d) / den
        s[:, 0, 1] = 2.0 * det / den
        s[:, 1, 0] = 2.0 * det / den
        s[:, 1, 1] = (-a + b / z0 - c * z0 + d) / den
        return s


def _raised_cosine_ramp(z_start: float, z_end: float, length_mm: float, n: int) -> List[Section]:
    """n thin sections whose ln Z follows a raised cosine from z_start to z_end."""
    s = (np.arange(n) + 0.5) / n
    ln_ratio = math.log(z_end / z_start)
    z = z_start * np.exp(ln_ratio * (1.0 - np.cos(math.pi * s)) / 2.0)
    return [Section(float(zi), length_mm / n) for zi in z]


def sharp_line() -> DummyLine:
    """50 ohm 20 mm, 500 ohm 15 mm, 50 ohm 15 mm (analytic: 500 ohm is not a buildable CPW here)."""
    return DummyLine("SHARP", (Section(50.0, 20.0), Section(500.0, 15.0), Section(50.0, 15.0)))


def sharp_line_lossy() -> DummyLine:
    """The SHARP line with a constant attenuation of LOSS_DB_PER_CM."""
    alpha = LOSS_DB_PER_CM / 8.685889638 / 10.0  # Np per mm
    base = sharp_line()
    return DummyLine("SHARP small loss", base.sections, alpha_np_per_mm=alpha)


def gradual_line(n_per_ramp: int = 250) -> DummyLine:
    """50 -> 150 ohm (raised cosine in ln Z over 25 mm), 10 mm plateau, back to 50 ohm
    over 25 mm; 10 mm of 50 ohm either side. Thin sections (0.1 mm for 250 per ramp)
    are the exact model, and the staircase is the truth the calculator is judged against."""
    up = _raised_cosine_ramp(50.0, 150.0, 25.0, n_per_ramp)
    down = list(reversed(_raised_cosine_ramp(50.0, 150.0, 25.0, n_per_ramp)))
    sections = [Section(50.0, 10.0)] + up + [Section(150.0, 10.0)] + down + [Section(50.0, 10.0)]
    return DummyLine("GRADUAL", tuple(sections))


# (start mm, length mm, Z ohm, label)
CORNER_FEATURES = (
    (8.0, 0.3, 35.0, "corner"),
    (17.0, 0.2, 42.0, "bend"),
    (26.0, 0.3, 35.0, "corner"),
    (27.8, 0.2, 42.0, "bend"),  # 1.75 mm centre-to-centre from the corner above
    (34.0, 0.3, 35.0, "corner"),
)


def corners_line() -> DummyLine:
    """A 50 ohm, 40 mm line with short low-impedance corners and bends."""
    sections: List[Section] = []
    position = 0.0
    for start, length, z, _ in CORNER_FEATURES:
        if start > position:
            sections.append(Section(Z0, start - position))
        sections.append(Section(z, length))
        position = start + length
    sections.append(Section(Z0, 40.0 - position))
    return DummyLine("CORNERS", tuple(sections))


def corner_centres() -> List[float]:
    return [start + 0.5 * length for start, length, _, _ in CORNER_FEATURES]


BANDS: Dict[str, Tuple[str, np.ndarray]] = {
    "generous": ("0.05-100 GHz, 2000 pts", np.arange(1, 2001) * 0.05),
    "real": ("0.1-20 GHz, 401 pts", np.linspace(0.1, 20.0, 401)),
    "from_2ghz": ("2-20 GHz, 361 pts (older runs)", np.arange(40, 401) * 0.05),
}

# Judged at the generous band's limits (tight) or the looser ones.
TIGHT_BANDS = ("generous",)


@dataclass
class CaseReport:
    case: str
    band: str
    method: str
    kind: str
    criteria: str
    passed: bool
    numbers: Dict[str, object]
    result: tdr.TDRResult
    mismatches: List[tdr.Mismatch]
    failures: List[str] = field(default_factory=list)


def _nearest_error(mismatches, position: float) -> float:
    if not mismatches:
        return float("inf")
    return min(abs(m.position_mm - position) for m in mismatches)


def evaluate(line: DummyLine, result: tdr.TDRResult, *, band_key: str, kind: str,
             edges: Sequence[float] = (), z_plateau: float = 0.0, z_outer: float = Z0
             ) -> Tuple[Dict[str, object], List[tdr.Mismatch], List[str]]:
    """The numbers of one case, and the criteria failures at its band's limits."""
    mismatches, _ = tdr.find_mismatches(result, threshold=MISMATCH_THRESHOLD)
    res = result.resolution_mm
    m = result.r.size
    x_c = result.profile_x_mm[:m] + 0.5 * result.layer_mm
    z_meas = result.z_peel
    z_true = line.true_z(x_c)
    numbers: Dict[str, object] = {"res_mm": res, "rise_ns": result.rise_time_ns, "layers": m,
                                  "stable": result.stable, "dc": result.dc_value,
                                  "dc_source": result.dc_source}
    failures: List[str] = []
    tight = band_key in TIGHT_BANDS
    edge_limit = 0.5 if tight else 1.0
    plateau_limit = 0.05 if tight else 0.10
    rms_limit = 0.05 if tight else 0.08

    if kind == "sharp":
        errors = [_nearest_error(mismatches, e) / res for e in edges]
        numbers["edge_err_res"] = errors
        plateau_lo, plateau_hi = edges[0] + res, edges[1] - res
        in_plateau = (x_c >= plateau_lo) & (x_c <= plateau_hi)
        plateau_mean = float(np.mean(z_meas[in_plateau])) if np.any(in_plateau) else float("nan")
        plateau_err = abs(plateau_mean - z_plateau) / z_plateau
        numbers["plateau_ohm"] = plateau_mean
        numbers["plateau_err"] = plateau_err
        outer = (((x_c >= res) & (x_c <= edges[0] - res))
                 | ((x_c >= edges[1] + res) & (x_c <= line.length_mm - res)))
        outer_err = float(np.max(np.abs(z_meas[outer] - z_outer)) / z_outer) if np.any(outer) else 0.0
        numbers["outer_max_err"] = outer_err
        for err_res in errors:
            if not err_res <= edge_limit:
                failures.append(f"edge {err_res:.2f} res off (limit {edge_limit:g})")
        if not plateau_err <= plateau_limit:
            failures.append(f"plateau {plateau_err * 100:.1f} % (limit {plateau_limit * 100:.0f} %)")
        if tight and not outer_err <= 0.03:
            failures.append(f"outer parts {outer_err * 100:.1f} % (limit 3 %)")

    elif kind == "gradual":
        inside = (x_c >= res) & (x_c <= line.length_mm - res)
        rms = float(np.sqrt(np.mean((z_meas[inside] - z_true[inside]) ** 2)))
        rms_pct = rms / GRADUAL_RANGE_OHM
        numbers["rms_ohm"] = rms
        numbers["rms_pct"] = rms_pct
        numbers["max_err_ohm"] = float(np.max(np.abs(z_meas[inside] - z_true[inside])))
        numbers["false_edges"] = len(mismatches)
        if not rms_pct <= rms_limit:
            failures.append(f"RMS {rms_pct * 100:.1f} % (limit {rms_limit * 100:.0f} %)")
        if tight and len(mismatches):
            failures.append(f"{len(mismatches)} detection(s) with |r| >= {MISMATCH_THRESHOLD}")

    elif kind == "corners":
        centres = corner_centres()
        labels = [label for _, _, _, label in CORNER_FEATURES]
        close = {i for i in range(len(centres)) for j in range(i + 1, len(centres))
                 if abs(centres[j] - centres[i]) < res}
        close |= {j for i in range(len(centres)) for j in range(i + 1, len(centres))
                  if abs(centres[j] - centres[i]) < res}
        close_pairs = [(round(centres[i], 2), round(centres[j], 2), round(abs(centres[j] - centres[i]), 2))
                       for i in range(len(centres)) for j in range(i + 1, len(centres))
                       if abs(centres[j] - centres[i]) < res]
        matched = [_nearest_error(mismatches, c) <= res for c in centres]
        extra = [mm for mm in mismatches if min(abs(mm.position_mm - c) for c in centres) > res]
        numbers.update({"centres_mm": centres, "matched": matched, "labels": labels,
                        "extra": len(extra), "n_mismatches": len(mismatches),
                        "close_pairs": close_pairs,
                        "merged_notes": [mm.note for mm in mismatches if mm.note]})
        for i, c in enumerate(centres):
            if i not in close and not matched[i]:
                failures.append(f"{labels[i]} at {c:.2f} mm not found within 1 res")
        if tight and extra:
            failures.append(f"{len(extra)} extra detection(s)")
        if close_pairs and not numbers["merged_notes"]:
            failures.append("close pair not reported as one, with a note")
    return numbers, mismatches, failures


def run_case(line: DummyLine, band_key: str, *, method: str, kind: str, case: str,
             edges: Sequence[float] = (), z_plateau: float = 0.0, z_outer: float = Z0,
             beta: float = tdr.DEFAULT_BETA, pad: int = tdr.DEFAULT_PAD) -> CaseReport:
    """Build the dummy S-parameters for one band, analyse with one method, and judge."""
    band_label, freq = BANDS[band_key]
    s11 = line.s_parameters(freq)[:, 0, 0]
    runner = tdr.analyse if method == "peel" else tdr.analyse_fit
    result = runner(freq, s11, name=f"{case} ({band_key}, {method})", z0=line.z0,
                    eps_eff=line.eps_eff, beta=beta, pad=pad, max_mm=line.length_mm + 6.0)
    numbers, mismatches, failures = evaluate(line, result, band_key=band_key, kind=kind,
                                             edges=edges, z_plateau=z_plateau, z_outer=z_outer)
    if not result.stable:
        failures.append("profile not reliable (" + "; ".join(result.notes) + ")")
    return CaseReport(case=case, band=band_key, method=method, kind=kind, criteria=band_label,
                      passed=not failures, numbers=numbers, result=result,
                      mismatches=mismatches, failures=failures)


def _truth_for(case: str):
    lines = {"SHARP": sharp_line, "GRADUAL": gradual_line, "CORNERS": corners_line,
             "SHARP, starts 2 GHz": sharp_line, "SHARP, small loss": sharp_line_lossy}
    return lines[case]().true_z


def run_suite(beta: float = tdr.DEFAULT_BETA, pad: int = tdr.DEFAULT_PAD,
              methods: Sequence[str] = METHODS) -> List[CaseReport]:
    """All cases at the generous and the real band, plus the 2 GHz and the lossy extras."""
    reports: List[CaseReport] = []
    for method in methods:
        sharp = sharp_line()
        for key in ("generous", "real"):
            reports.append(run_case(sharp, key, method=method, kind="sharp", case="SHARP",
                                    edges=(20.0, 35.0), z_plateau=500.0, beta=beta, pad=pad))
            reports.append(run_case(gradual_line(), key, method=method, kind="gradual", case="GRADUAL",
                                    beta=beta, pad=pad))
            reports.append(run_case(corners_line(), key, method=method, kind="corners", case="CORNERS",
                                    beta=beta, pad=pad))
        reports.append(run_case(sharp, "from_2ghz", method=method, kind="sharp",
                                case="SHARP, starts 2 GHz", edges=(20.0, 35.0), z_plateau=500.0,
                                beta=beta, pad=pad))
        reports.append(run_case(sharp_line_lossy(), "real", method=method, kind="sharp",
                                case="SHARP, small loss", edges=(20.0, 35.0), z_plateau=500.0,
                                beta=beta, pad=pad))
    return reports


def dc_extrapolation_error(method: str, beta: float = tdr.DEFAULT_BETA,
                           pad: int = tdr.DEFAULT_PAD) -> Dict[str, float]:
    """The 2 GHz start against the 0.1 GHz start of the same SHARP line (same top frequency)."""
    line = sharp_line()
    runner = tdr.analyse if method == "peel" else tdr.analyse_fit
    _, f_full = BANDS["real"]
    _, f_2 = BANDS["from_2ghz"]
    full = runner(f_full, line.s_parameters(f_full)[:, 0, 0], eps_eff=line.eps_eff, beta=beta, pad=pad,
                  max_mm=line.length_mm + 6.0)
    short = runner(f_2, line.s_parameters(f_2)[:, 0, 0], eps_eff=line.eps_eff, beta=beta, pad=pad,
                   max_mm=line.length_mm + 6.0)
    m = min(full.r.size, short.r.size)
    x = full.profile_x_mm[:m] + 0.5 * full.layer_mm
    keep = (x > 3.0) & (x < line.length_mm - 3.0)
    diff = np.abs(full.z_peel[:m][keep] - short.z_peel[:m][keep])
    out = {"max_profile_diff_ohm": float(np.max(diff)), "rms_profile_diff_ohm": float(np.sqrt(np.mean(diff ** 2)))}
    if method == "peel":
        out["dc_fit_2ghz"] = short.dc_value
    return out


def markdown_table(reports: Sequence[CaseReport]) -> str:
    header = ("| method | case | band | result | edge err (res) | plateau err | outer max | RMS (% range) "
              "| false edges | corners matched | extra | resolution mm | rise ps | notes |")
    rule = "|" + "---|" * 14
    rows = [header, rule]
    for rep in reports:
        n = rep.numbers
        edges = ", ".join(f"{e:.2f}" for e in n.get("edge_err_res", [])) or "-"
        plateau = f"{n['plateau_err'] * 100:.1f} %" if "plateau_err" in n else "-"
        outer = f"{n['outer_max_err'] * 100:.1f} %" if "outer_max_err" in n else "-"
        rms = f"{n['rms_pct'] * 100:.1f} %" if "rms_pct" in n else "-"
        false_edges = str(n["false_edges"]) if "false_edges" in n else "-"
        corners = f"{sum(n['matched'])}/{len(n['matched'])}" if "matched" in n else "-"
        extra = str(n["extra"]) if "extra" in n else "-"
        notes = list(rep.failures)
        if n.get("close_pairs"):
            notes.append("close pair " + ", ".join(f"{a}/{b} ({d} mm)" for a, b, d in n["close_pairs"]))
        rows.append(
            f"| {rep.method} | {rep.case} | {rep.band} | {'PASS' if rep.passed else 'FAIL'} | {edges} "
            f"| {plateau} | {outer} | {rms} | {false_edges} | {corners} | {extra} | {n['res_mm']:.2f} "
            f"| {n['rise_ns'] * 1e3:.1f} | {'; '.join(notes) or '-'} |")
    return "\n".join(rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m microwave_package.workflow.tdr_validation",
                                     description="Validate workflow/tdr.py on exact dummy lines.")
    parser.add_argument("--out", required=True, help="folder for the overlay PNGs and the table")
    parser.add_argument("--beta", type=float, default=tdr.DEFAULT_BETA)
    parser.add_argument("--pad", type=int, default=tdr.DEFAULT_PAD)
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    reports = run_suite(beta=args.beta, pad=args.pad)
    table = markdown_table(reports)
    for rep in reports:
        slug = rep.case.replace(", ", "_").replace(" ", "_").lower()
        tdr.plot_profile(rep.result, out / f"{slug}_{rep.band}_{rep.method}.png",
                         truth=_truth_for(rep.case), threshold=MISMATCH_THRESHOLD)
    (out / "validation_table.md").write_text(table + "\n", encoding="utf-8")
    print(table)
    print()
    for method in METHODS:
        print(f"DC extrapolation, {method} (2 GHz start vs 0.1 GHz start): "
              f"{dc_extrapolation_error(method, args.beta, args.pad)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
