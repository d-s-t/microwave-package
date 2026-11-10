"""
Time-domain reflectometry (TDR) of a run's S-parameters: where along the
line the impedance changes, and what it is at each point.

    python -m microwave_package.workflow.tdr <run dir | .s2p | results.csv> --eps-eff 2.0 \
        --line-length-mm 50 --out <dir>

Read-only. Pure numpy; matplotlib only for the PNG, drawn without pyplot.

Method
------
1. Harmonic grid. S11 (port 1; port 2 swaps S11 and S22) is put on f_k = k*df,
   k = 0..N, df = the sweep step. A sweep that starts above df, or sits off the grid, is
   interpolated after a linear phase (the delay of the lowest points) is removed. The DC value
   and the bins below the first sweep frequency are extrapolated with a quadratic fit to the
   lowest ``fit_span_ghz`` of the data; the DC value is made real.
2. Window. A half-Kaiser window (flat at DC, falling to 1/I0(beta) at f_max). beta trades the
   rise time against the ringing of the step response.
3. Impulse response and naive TDR. The Hermitian, zero-padded inverse transform gives the
   sampled reflection impulse response a[n] (sum(a) = S11(DC)); its cumulative sum is the reflected
   step a scope shows, and Z = Z0 (1+rho)/(1-rho) is the naive impedance: right only up to the first
   strong mismatch.
4. Layer peel (method "peel"). One sample is one round trip through one layer of one-way length
   dx = v T/2. With R_k the remaining response and r_k its first sample: X = (R_k - r_k)/(1 - r_k R_k),
   R_{k+1} = z X, Z_{k+1} = Z_k (1+r_k)/(1-r_k). The recursion is exact on exact input but its error
   grows by about 1e6 over 40 layers, so it is only indicative on band-limited data.
5. Regularised fit (method "fit", the main method for a 2-port). The trace is N uniform LOSSY
   cells, each of about one third of the resolution, between 50 ohm ports, cascaded as ABCD
   matrices (exact for uniform lines). The unknowns are u_i = ln(Z_i/Z0) per cell, the loss
   alpha(f) = a_c sqrt(f) + a_d f (non-negative: a = exp(theta)), and a delay scale s = exp(sigma) on
   the given eps_eff, so that the fitted eps is eps s^2. The data are S11, S21 and S22 (S12 = S21),
   complex, each block divided by its RMS magnitude so that each counts equally. The objective is the
   sum of squared residuals plus lam times a penalty on the profile: total variation of ln Z by
   iteratively reweighted least squares (weights 1/sqrt(d^2 + eps^2), re-evaluated each outer pass), so
   edges are kept sharp. lam is the largest value of a descending half-decade grid whose RMS residual
   is within tol (0.5 %, the discrepancy principle). Levenberg-Marquardt with an analytic Jacobian
   (checked against central differences). The start is the layer peel where it is stable, otherwise
   50 ohm everywhere. Given a 1-port data set, the same fit uses S11 alone (analyse_fit).
6. Features. A step is a change of the median ln-impedance across one resolution, reported when
   |rho| = tanh(|dln Z|/2) >= threshold. An excursion is a narrow region (at most one resolution wide,
   between two wide levels that agree) that departs from them, reported by its area in ohm mm, the
   integral of (Z - baseline) dx, when the area is at least the Born area: |S11| = beta A / Z0 reaches
   the threshold at f_max, so A = Z0 threshold / beta. Excursions are removed before steps are looked
   for. Features closer than one resolution are merged, with a note.
7. Positions. x = v t/2 with t the round trip; eps_eff is a parameter, and every plot prints it.
8. Resolution. The 10-90 % rise time of the windowed step is kappa(beta)/f_max (kappa from the window
   alone, 0.445 for no taper and 0.69 for beta 3), and the distance resolution is v t_rise/2.

Physics assumptions and limits
------------------------------
* The fit is lossy: a dielectric term a_d f (RO3003 tan delta 0.001: 1.48e-5 Np/mm per GHz) and a
  conductor term a_c sqrt(f). The HFSS models use perfect-E conductors, so a_c should go to zero. The
  peel is lossless and does not compensate loss.
* Dispersion: the fit's delay scale absorbs an effective-permittivity offset (a 1 % eps change is a
  0.5 % delay change), and a dispersion-free line is assumed at every frequency.
* The whole-2-port fit needs the distance between the two reference planes (line_length_mm): S22 is
  the trace seen from the far end, so it must be placed. One-port data use max_mm instead.
* Cells are uniform: a discontinuity between cell boundaries is represented by a cell of intermediate
  impedance, which at 100 GHz leaves a residual of about 0.01 (the generous-band lossy fit does not
  reach 0.5 %).
* A feature narrower than the resolution is reported by its area, not its peak: a corner smoothed by
  the fit keeps its area but loses its depth, and a feature whose area is below the Born threshold is not
  reported.
* A sweep that starts above 1 GHz leaves the band below it unconstrained; the calculator warns (see
  tdr_validation, which characterises a 2 GHz start rather than judging it).
* Any profile can be unreliable: the convergence flag and the notes say when a fit has not met its
  tolerance, and the peel reports only that its recursion stayed in |r| < 1.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np

C_MM_PER_NS = 299.792458

DEFAULT_EPS_EFF = 2.0
DEFAULT_Z0 = 50.0
# beta = 3: a 0.82 step (the 50 -> 500 ohm edge) rings by ~0.012 through the
# window's step overshoot (1.4 %), under half the 0.03 detection threshold. beta
# 2.5 would ring by ~0.019 and beta 0 (no taper) by ~0.07. The price is the rise
# time: kappa(3) = 0.69 against 0.445 for no taper (see rise_time_factor).
DEFAULT_BETA = 3.0
DEFAULT_PAD = 8
DEFAULT_MAX_MM = 120.0
DEFAULT_THRESHOLD = 0.03
DEFAULT_FIT_SPAN_GHZ = 1.0
DEFAULT_DEVIATION_TOL = 0.02
DEFAULT_FIT_CELL = 1.0 / 3.0  # fit cell = this x resolution
LOW_START_GHZ = 1.0  # a sweep starting above this does not reach the band the profile needs
MIN_FIT_POINTS = 8
METHODS = ("peel", "fit")

# The broad sweep's rows in a run's results.csv are tagged "coarse"; the fine
# windows are "Fine_n" (see workflow/plotting.py, BROAD_SWEEP).
BROAD_SWEEP = "coarse"

_UNIT_TO_GHZ = {"HZ": 1e-9, "KHZ": 1e-6, "MHZ": 1e-3, "GHZ": 1.0}


def velocity_mm_per_ns(eps_eff: float) -> float:
    """Propagation speed on the line, v = c / sqrt(eps_eff)."""
    if not eps_eff > 0:
        raise ValueError("eps_eff must be positive")
    return C_MM_PER_NS / math.sqrt(eps_eff)


# ---------------------------------------------------------------- input


@dataclass(frozen=True)
class SParameters:
    """A 2-port S-matrix on one frequency grid (GHz) with reference impedance z0.

    ``s[:, 0, 0]`` is S11 and ``s[:, 1, 1]`` is S22, in the Touchstone order
    S11 S21 S12 S22 of the file.
    """

    freq_ghz: np.ndarray
    s: np.ndarray
    z0: float
    source: str


def read_touchstone(path) -> SParameters:
    """Read a 2-port Touchstone file (v1: '# <unit> S <MA|DB|RI> R <z0>')."""
    path = Path(path)
    unit_ghz, fmt, z0 = 1.0, "MA", DEFAULT_Z0
    numbers: List[float] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            text = raw.split("!", 1)[0].strip()
            if not text:
                continue
            if text.startswith("#"):
                words = text[1:].upper().split()
                for index, word in enumerate(words):
                    if word in _UNIT_TO_GHZ:
                        unit_ghz = _UNIT_TO_GHZ[word]
                    elif word in ("MA", "DB", "RI"):
                        fmt = word
                    elif word == "R" and index + 1 < len(words):
                        z0 = float(words[index + 1])
                continue
            numbers.extend(float(token) for token in text.split())
    values = np.asarray(numbers, dtype=float)
    if values.size == 0 or values.size % 9:
        raise ValueError(f"{path.name}: a 2-port file has 9 numbers per frequency, found {values.size}")
    rows = values.reshape(-1, 9)
    freq = rows[:, 0] * unit_ghz
    first, second = rows[:, 1::2], rows[:, 2::2]
    if fmt == "MA":
        pairs = first * np.exp(1j * np.deg2rad(second))
    elif fmt == "DB":
        pairs = 10.0 ** (first / 20.0) * np.exp(1j * np.deg2rad(second))
    else:
        pairs = first + 1j * second
    s = np.empty((freq.size, 2, 2), dtype=complex)
    s[:, 0, 0], s[:, 1, 0], s[:, 0, 1], s[:, 1, 1] = pairs.T
    order = np.argsort(freq, kind="stable")
    return SParameters(freq[order], s[order], z0, path.name)


def _complex_from_db_deg(db: str, deg: str) -> complex:
    magnitude = 10.0 ** (float(db) / 20.0)
    angle = math.radians(float(deg))
    return complex(magnitude * math.cos(angle), magnitude * math.sin(angle))


def read_results_csv(path) -> SParameters:
    """The broad-sweep rows of a run's results.csv (fine windows dropped)."""
    path = Path(path)
    with path.open(newline="") as handle:
        rows = [row for row in csv.DictReader(handle)
                if (row.get("sweep") or BROAD_SWEEP) == BROAD_SWEEP]
    if not rows:
        raise ValueError(f"{path}: no broad-sweep rows")
    setups = sorted({row.get("setup", "") for row in rows})
    freq, s, seen = [], [], set()
    for row in rows:
        if row.get("setup", "") != setups[0]:
            continue
        key = float(row["freq_ghz"])
        if key in seen:
            continue
        seen.add(key)
        freq.append(key)
        matrix = np.zeros((2, 2), dtype=complex)
        matrix[0, 0] = _complex_from_db_deg(row["s11_db"], row["s11_deg"])
        matrix[1, 0] = _complex_from_db_deg(row["s21_db"], row["s21_deg"])
        matrix[0, 1] = _complex_from_db_deg(row["s12_db"], row["s12_deg"])
        matrix[1, 1] = _complex_from_db_deg(row["s22_db"], row["s22_deg"])
        s.append(matrix)
    order = np.argsort(freq, kind="stable")
    note = f" (setups {setups}: first used)" if len(setups) > 1 else ""
    return SParameters(np.asarray(freq)[order], np.asarray(s)[order], DEFAULT_Z0,
                       f"{path.name}{note}")


def broad_sparameters(folder) -> Optional[SParameters]:
    """A combo folder's broad sweep: the Touchstone file, else results.csv."""
    folder = Path(folder)
    candidates = sorted(p for p in folder.glob("*.s2p") if "fine" not in p.name.lower())
    broad = [p for p in candidates if p.name.endswith("_Sweep_1.s2p")] or candidates
    if broad:
        return read_touchstone(broad[0])
    csv_path = folder / "results.csv"
    if csv_path.is_file():
        try:
            return read_results_csv(csv_path)
        except ValueError:
            return None
    return None


def load_inputs(path) -> List[Tuple[str, SParameters]]:
    """(label, S-parameters) for each thing to analyse: a .s2p, a results.csv,
    one combo folder, or a run folder (every models/<combo>/ with a broad sweep)."""
    path = Path(path)
    if path.is_file():
        if path.suffix.lower() == ".s2p":
            return [(path.stem, read_touchstone(path))]
        if path.name.lower() == "results.csv":
            return [(path.parent.name, read_results_csv(path))]
        raise ValueError(f"{path}: expected a .s2p, a results.csv or a folder")
    models = path / "models"
    folders = sorted(d for d in models.iterdir() if d.is_dir()) if models.is_dir() else [path]
    found = []
    for folder in folders:
        sp = broad_sparameters(folder)
        if sp is not None:
            found.append((folder.name, sp))
    if not found:
        raise FileNotFoundError(f"no broad sweep (.s2p or results.csv) under {path}")
    return found


# ---------------------------------------------------------------- spectrum


@dataclass(frozen=True)
class HarmonicSpectrum:
    """The reflection on f_k = k*df, k = 0..N (k = 0 is DC)."""

    df_ghz: float
    s: np.ndarray
    delay_ns: float
    dc_source: str
    n_extrapolated: int

    @property
    def f_max_ghz(self) -> float:
        return (self.s.size - 1) * self.df_ghz


def harmonic_spectrum(freq_ghz, reflection, *, dc: Optional[float] = None,
                      fit_span_ghz: float = DEFAULT_FIT_SPAN_GHZ) -> HarmonicSpectrum:
    """Put a reflection sweep on the harmonic grid (see the module docstring, step 1)."""
    f = np.asarray(freq_ghz, dtype=float).ravel()
    s_in = np.asarray(reflection, dtype=complex).ravel()
    if f.shape != s_in.shape or f.size < MIN_FIT_POINTS:
        raise ValueError(f"need at least {MIN_FIT_POINTS} matching frequency and S11 points")
    order = np.argsort(f, kind="stable")
    f, s_in = f[order], s_in[order]
    unique = np.concatenate(([True], np.diff(f) > 0))
    f, s_in = f[unique], s_in[unique]
    df = float(np.median(np.diff(f)))
    if not df > 0:
        raise ValueError("the frequency axis must increase")
    n_fit = min(f.size, max(MIN_FIT_POINTS, int(np.count_nonzero(f <= f[0] + fit_span_ghz))))
    # Linear phase from the mean phase increment of the LOWEST points: that is where
    # the residual is extrapolated, and it must be slowly varying there. (The mean
    # over the whole band is dominated by the strongest reflections' delays and
    # leaves a residual that rotates by ~1 rad/GHz at the band edge.)
    increments = np.angle(s_in[1:n_fit] * np.conj(s_in[: n_fit - 1]))
    delay_ns = -float(np.mean(increments)) / (2.0 * math.pi * df)
    residual = s_in * np.exp(2j * math.pi * f * delay_ns)

    n_top = int(math.floor(f[-1] / df + 1e-9))
    k = np.arange(n_top + 1)
    fk = k * df
    grid = np.empty(n_top + 1, dtype=complex)
    inside = fk >= f[0] - 1e-9 * df
    grid[inside] = (np.interp(fk[inside], f, residual.real)
                    + 1j * np.interp(fk[inside], f, residual.imag))

    below = ~inside
    x_fit = f[:n_fit] - f[0]
    degree = min(2, n_fit - 1)
    re_fit = np.polyfit(x_fit, residual[:n_fit].real, degree)
    im_fit = np.polyfit(x_fit, residual[:n_fit].imag, degree)
    x_below = fk[below] - f[0]
    grid[below] = np.polyval(re_fit, x_below) + 1j * np.polyval(im_fit, x_below)

    if dc is not None:
        grid[0] = float(np.real(dc))
        dc_source = "given"
    else:
        grid[0] = float(np.real(grid[0]))
        dc_source = "fit" if below[0] else "data"
    s_grid = grid * np.exp(-2j * math.pi * fk * delay_ns)
    return HarmonicSpectrum(df, s_grid, delay_ns, dc_source, int(np.count_nonzero(below)))


def half_kaiser(n_bins: int, beta: float) -> np.ndarray:
    """Right half of a Kaiser window: 1 at DC, 1/I0(beta) at the top bin."""
    n = n_bins - 1
    if n <= 0:
        return np.ones(max(n_bins, 1))
    return np.kaiser(2 * n + 1, beta)[n:]


def impulse_sequence(s_grid, df_ghz: float, pad: int = DEFAULT_PAD) -> Tuple[np.ndarray, float]:
    """The real sampled impulse response a[n] and its sample interval T (ns).

    The one-sided spectrum is extended Hermitian and zero-padded to
    M = pad*(2N+2) points, so a[n] reproduces S11(k*df) = sum_n a[n] exp(-j 2 pi k n / M)
    exactly (sum(a) = S11(DC)).
    """
    s_grid = np.asarray(s_grid, dtype=complex)
    n = s_grid.size - 1
    m = int(round(pad * (2 * n + 2)))
    spectrum = np.zeros(m, dtype=complex)
    spectrum[: n + 1] = s_grid
    spectrum[m - n:] = np.conj(s_grid[1:][::-1])
    a = np.fft.ifft(spectrum)
    return a.real.copy(), 1.0 / (m * df_ghz)


_RISE_CACHE: dict = {}


def rise_time_factor(beta: float, pad: int = DEFAULT_PAD) -> float:
    """kappa(beta): the 10-90 % rise time of the windowed step, times f_max (ns x GHz).

    Measured on a unit-f_max grid of the windowed flat spectrum. For a brick-wall
    band (beta = 0) it is 0.446 (the analytic 2.80/(2 pi) = 0.446).
    """
    key = (round(float(beta), 9), int(pad))
    if key not in _RISE_CACHE:
        n_grid = 512
        window = half_kaiser(n_grid + 1, beta).astype(complex)
        a, t_sample = impulse_sequence(window, df_ghz=1.0 / n_grid, pad=pad)
        centre = a.size // 2
        step = np.cumsum(np.roll(a, centre))
        t = (np.arange(a.size) - centre) * t_sample
        final = step[-1]
        i10 = int(np.argmax(step >= 0.1 * final))
        i90 = int(np.argmax(step >= 0.9 * final))
        t10 = np.interp(0.1 * final, step[i10 - 1:i10 + 1], t[i10 - 1:i10 + 1])
        t90 = np.interp(0.9 * final, step[i90 - 1:i90 + 1], t[i90 - 1:i90 + 1])
        _RISE_CACHE[key] = float(t90 - t10)
    return _RISE_CACHE[key]


def rise_time_ns(f_max_ghz: float, beta: float) -> float:
    return rise_time_factor(beta) / f_max_ghz


# ---------------------------------------------------------------- layer peeling


def _series_divide(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """Quotient of two causal power series in z^-1 (den[0] != 0), truncated."""
    q = np.empty_like(num)
    d0 = den[0]
    for n in range(num.size):
        acc = num[n]
        if n:
            acc -= np.dot(den[1:n + 1], q[n - 1::-1])
        q[n] = acc / d0
    return q


@dataclass(frozen=True)
class LayerPeel:
    r: np.ndarray        # reflection at each interface (x = k*dx)
    z_after: np.ndarray  # impedance just beyond interface k
    stable: bool
    note: str


def layer_peel(a, z0: float, n_layers: int) -> LayerPeel:
    """Peel n_layers from the sampled reflection response a[n] (step 4)."""
    a = np.asarray(a, dtype=float)
    n_layers = int(min(n_layers, a.size - 1))
    response = a[: n_layers + 1].copy()
    r_out: List[float] = []
    z_out: List[float] = []
    z = float(z0)
    for k in range(n_layers):
        rk = float(response[0])
        if not abs(rk) < 1.0:
            return LayerPeel(np.asarray(r_out), np.asarray(z_out), False,
                             f"layer peel unstable: |r| = {abs(rk):.3f} >= 1 at interface {k}")
        r_out.append(rk)
        z = z * (1.0 + rk) / (1.0 - rk)
        z_out.append(z)
        if k == n_layers - 1:
            break
        numerator = response.copy()
        numerator[0] -= rk
        denominator = -rk * response
        denominator[0] += 1.0
        response = _series_divide(numerator, denominator)[1:]
    return LayerPeel(np.asarray(r_out), np.asarray(z_out), True, "")


# ---------------------------------------------------------------- regularised fit
#
# The fit models the trace as N uniform LOSSY cells of length cell_mm between
# 50 ohm ports, cascaded as ABCD matrices (exact for uniform lines):
#     u_i = ln(Z_i / Z0)                  one per cell: the profile
#     alpha(f) = a_c sqrt(f) + a_d f      loss in Np/mm (f in GHz), a = exp(theta) >= 0
#     beta(f)  = s 2 pi f sqrt(eps) / c   s = exp(sigma) rescales the GIVEN eps_eff,
#                                         so the fitted eps is eps * s^2 (it fits v)
# gamma = alpha + j beta and each cell has electrical length gamma * cell_mm. The
# HFSS models use perfect-E conductors, so a_c should go to ~0; a_d is the dielectric
# loss (RO3003, tan delta 0.001: 1.48e-5 Np/mm per GHz, 0.15 Np/m at 10 GHz).
# Objective: sum_b || w_b (S_b(model) - S_b(data)) ||^2 + lam sum_i omega_i (u_{i+1} - u_i)^2,
# omega_i = 1 for the L2 penalty, IRLS weights 1/sqrt(d_i^2 + eps^2) for total variation.
# lam: the largest value of a descending grid whose RMS residual is within tol.

FIT_TOL = 0.005
FIT_MAX_POINTS = 300  # even decimation: the line's delays (< ~1 ns) need df < 0.5 GHz
FIT_LAMBDA_GRID = tuple(float(10.0 ** (e / 2.0)) for e in range(4, -10, -1))  # 1e2 .. 3e-5
LOSS_START_NP_PER_MM = 1e-5  # starting a_c and a_d (Np/mm per sqrt(GHz) and per GHz)
TV_EPS = 1e-3
TV_OUTER = 3
LM_INNER = 12


def _s_from_total(total: np.ndarray, z0: float):
    """S11, S21, S22 of a reciprocal cascade (det = 1), and the pieces their derivatives need."""
    a = total[:, 0, 0]
    b = total[:, 0, 1]
    c = total[:, 1, 0]
    d = total[:, 1, 1]
    den = a + b / z0 + c * z0 + d
    num11 = a + b / z0 - c * z0 - d
    num22 = -a + b / z0 - c * z0 + d
    return {"11": num11 / den, "21": 2.0 / den, "22": num22 / den}, den, num11, num22


def _s_derivatives(d_total: np.ndarray, den: np.ndarray, num11: np.ndarray, num22: np.ndarray,
                   z0: float):
    """dS11, dS21, dS22 from the derivative of the cascade matrix (F, 2, 2)."""
    da = d_total[:, 0, 0]
    db = d_total[:, 0, 1]
    dc = d_total[:, 1, 0]
    dd = d_total[:, 1, 1]
    dden = da + db / z0 + z0 * dc + dd
    dnum11 = da + db / z0 - z0 * dc - dd
    dnum22 = -da + db / z0 - z0 * dc + dd
    inv = 1.0 / den ** 2
    return {"11": (dnum11 * den - num11 * dden) * inv,
            "21": -2.0 * dden * inv,
            "22": (dnum22 * den - num22 * dden) * inv}


class LineModel:
    """N uniform lossy cells of length cell_mm between 50 ohm ports (see the section note).

    Parameter vector p: [u_0 .. u_{N-1}, theta_c, theta_d, sigma]. loss=False and
    delay=False drop theta_c/theta_d and sigma from the fit (a_c = a_d = 0, s = 1).
    """

    def __init__(self, freq_ghz, cell_mm: float, n_cells: int, eps_eff: float,
                 z0: float = DEFAULT_Z0, *, loss: bool = True, delay: bool = True,
                 blocks: Sequence[str] = ("11",)):
        self.f = np.asarray(freq_ghz, dtype=float).ravel()
        self.cell_mm = float(cell_mm)
        self.n = int(n_cells)
        self.eps_eff = float(eps_eff)
        self.z0 = float(z0)
        self.loss = bool(loss)
        self.delay = bool(delay)
        self.blocks = tuple(blocks)
        self._beta0 = 2.0 * math.pi * self.f * math.sqrt(self.eps_eff) / C_MM_PER_NS
        self._sqrt_f = np.sqrt(self.f)

    @property
    def n_params(self) -> int:
        return self.n + 3

    def active(self) -> np.ndarray:
        idx = list(range(self.n))
        if self.loss:
            idx += [self.n, self.n + 1]
        if self.delay:
            idx += [self.n + 2]
        return np.asarray(idx, dtype=int)

    def unpack(self, p: np.ndarray):
        """(a_c, a_d, s) of a parameter vector."""
        ac = math.exp(p[self.n]) if self.loss else 0.0
        ad = math.exp(p[self.n + 1]) if self.loss else 0.0
        s = math.exp(p[self.n + 2]) if self.delay else 1.0
        return ac, ad, s

    def _constants(self, p: np.ndarray):
        ac, ad, s = self.unpack(p)
        beta = s * self._beta0
        alpha = ac * self._sqrt_f + ad * self.f
        theta = (alpha + 1j * beta) * self.cell_mm
        z = self.z0 * np.exp(p[: self.n])
        d_theta = (ac * self._sqrt_f * self.cell_mm,   # d theta / d theta_c
                   ad * self.f * self.cell_mm,         # d theta / d theta_d
                   1j * beta * self.cell_mm)           # d theta / d sigma
        return z, np.cosh(theta), np.sinh(theta), d_theta

    def _cascade(self, z: np.ndarray, ch: np.ndarray, sh: np.ndarray):
        n, nf = self.n, self.f.size
        cells = np.empty((n, nf, 2, 2), dtype=complex)
        cells[:, :, 0, 0] = ch
        cells[:, :, 1, 1] = ch
        cells[:, :, 0, 1] = z[:, None] * sh[None, :]
        cells[:, :, 1, 0] = sh[None, :] / z[:, None]
        suffix = np.empty((n + 1, nf, 2, 2), dtype=complex)
        suffix[n] = np.eye(2)
        for i in range(n - 1, -1, -1):
            suffix[i] = cells[i] @ suffix[i + 1]
        return cells, suffix

    def s_params(self, p: np.ndarray) -> dict:
        """S11, S21, S22 of the model (dict of complex arrays)."""
        z, ch, sh, _ = self._constants(p)
        _, suffix = self._cascade(z, ch, sh)
        return _s_from_total(suffix[0], self.z0)[0]

    def residual_and_jacobian(self, p: np.ndarray, data: dict, weights: dict, jacobian: bool = True):
        """Complex weighted residual over the blocks (stacked), and dr/dp (complex)."""
        n, nf = self.n, self.f.size
        z, ch, sh, d_theta = self._constants(p)
        cells, suffix = self._cascade(z, ch, sh)
        s, den, num11, num22 = _s_from_total(suffix[0], self.z0)
        r = np.concatenate([weights[b] * (s[b] - data[b]) for b in self.blocks])
        if not jacobian:
            return r, None
        jac = np.zeros((len(self.blocks) * nf, self.n_params), dtype=complex)
        prefix = np.broadcast_to(np.eye(2, dtype=complex), (nf, 2, 2)).copy()
        m_theta = np.zeros((nf, 2, 2), dtype=complex)
        for i in range(n):
            d_u = np.zeros((nf, 2, 2), dtype=complex)  # d cell / d u_i (z = z0 e^u)
            d_u[:, 0, 1] = z[i] * sh
            d_u[:, 1, 0] = -sh / z[i]
            d_t = np.zeros((nf, 2, 2), dtype=complex)  # d cell / d theta
            d_t[:, 0, 0] = sh
            d_t[:, 1, 1] = sh
            d_t[:, 0, 1] = z[i] * ch
            d_t[:, 1, 0] = ch / z[i]
            right = suffix[i + 1]
            ds = _s_derivatives(prefix @ d_u @ right, den, num11, num22, self.z0)
            for k, b in enumerate(self.blocks):
                jac[k * nf:(k + 1) * nf, i] = weights[b] * ds[b]
            m_theta += prefix @ d_t @ right
            prefix = prefix @ cells[i]
        global_cols = []
        if self.loss:
            global_cols += [(n, d_theta[0]), (n + 1, d_theta[1])]
        if self.delay:
            global_cols += [(n + 2, d_theta[2])]
        for col, g in global_cols:
            ds = _s_derivatives(g[:, None, None] * m_theta, den, num11, num22, self.z0)
            for k, b in enumerate(self.blocks):
                jac[k * nf:(k + 1) * nf, col] = weights[b] * ds[b]
        return r, jac


def _penalty_gram(n_cells: int, n_params: int, lam: float, omega: np.ndarray) -> np.ndarray:
    """lam * D^T diag(omega) D with (D u)_i = u_{i+1} - u_i (profile entries only)."""
    d = np.zeros((n_cells - 1, n_params))
    idx = np.arange(n_cells - 1)
    d[idx, idx] = -1.0
    d[idx, idx + 1] = 1.0
    return lam * (d.T @ (omega[:, None] * d))


def _penalised_lm(model: LineModel, p0: np.ndarray, data: dict, weights: dict,
                  gram: np.ndarray, max_iter: int):
    """Levenberg-Marquardt on ||r(p)||^2 + p^T gram p; returns (p, RMS of r)."""
    act = model.active()
    gram_a = gram[np.ix_(act, act)]
    p = p0.copy()
    r, jac = model.residual_and_jacobian(p, data, weights)
    cost = float(np.sum(np.abs(r) ** 2) + p @ gram @ p)
    mu = 1e-3
    for _ in range(max_iter):
        jr = np.vstack([jac.real[:, act], jac.imag[:, act]])
        rr = np.concatenate([r.real, r.imag])
        grad = jr.T @ rr + gram_a @ p[act]
        hess = jr.T @ jr + gram_a
        accepted = False
        for _ in range(25):
            step = np.linalg.solve(hess + mu * np.eye(act.size), -grad)
            p_try = p.copy()
            p_try[act] += step
            r_try, _ = model.residual_and_jacobian(p_try, data, weights, jacobian=False)
            cost_try = float(np.sum(np.abs(r_try) ** 2) + p_try @ gram @ p_try)
            if cost_try < cost:
                accepted = True
                break
            mu *= 4.0
        if not accepted:
            break
        decrease = cost - cost_try
        p, cost = p_try, cost_try
        mu = max(mu / 3.0, 1e-12)
        r, jac = model.residual_and_jacobian(p, data, weights)
        if decrease <= 1e-7 * max(cost, 1e-30):
            break
    return p, float(np.sqrt(np.mean(np.abs(r) ** 2)))


def fit_profile(model: LineModel, data: dict, weights: dict, *, tol: float, p0: np.ndarray,
                tv: bool = False):
    """Continuation over lam (descending): the first lam whose RMS residual is within tol.

    Returns (p, RMS, lam, converged). With tv=True each lam runs TV_OUTER IRLS passes,
    the weights re-evaluated from the current profile at each pass.
    """
    p = np.asarray(p0, dtype=float).copy()
    rms = float("nan")
    lam = FIT_LAMBDA_GRID[-1]
    # a trial step that overflows has a non-finite cost, so it is rejected by _penalised_lm
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        for lam in FIT_LAMBDA_GRID:
            for _ in range(TV_OUTER if tv else 1):
                if tv:
                    du = np.diff(p[: model.n])
                    omega = 1.0 / np.sqrt(du * du + TV_EPS ** 2)
                else:
                    omega = np.ones(model.n - 1)
                gram = _penalty_gram(model.n, model.n_params, lam, omega)
                p, rms = _penalised_lm(model, p, data, weights, gram, LM_INNER)
            if rms <= tol:
                return p, rms, lam, True
    return p, rms, lam, False


def _decimate(freq: np.ndarray, s: np.ndarray):
    """Evenly decimated to FIT_MAX_POINTS (both band edges kept)."""
    if freq.size <= FIT_MAX_POINTS:
        return freq, s
    keep = np.unique(np.round(np.linspace(0, freq.size - 1, FIT_MAX_POINTS)).astype(int))
    return freq[keep], s[keep]


# ---------------------------------------------------------------- results


@dataclass
class TDRResult:
    """One line's TDR. The naive step lives on its own (sample) grid; the profile
    (r, z_peel) on its profile grid. For method "peel" the two grids coincide."""

    name: str
    port: int
    method: str
    z0: float
    eps_eff: float
    beta: float
    pad: int
    df_ghz: float
    f_min_ghz: float
    f_max_ghz: float
    n_points: int
    dc_value: float
    dc_source: str
    n_extrapolated: int
    delay_removed_ns: float
    sample_ns: float
    rise_time_ns: float
    resolution_mm: float
    naive_t_ns: np.ndarray
    naive_x_mm: np.ndarray
    rho_step: np.ndarray
    z_naive: np.ndarray
    layer_mm: float
    profile_t_ns: np.ndarray
    profile_x_mm: np.ndarray
    r: np.ndarray        # reflection at each interface k (x = profile_x_mm[k])
    z_peel: np.ndarray   # impedance just beyond interface k
    stable: bool
    notes: List[str] = field(default_factory=list)
    fit: dict = field(default_factory=dict)

    @property
    def velocity_mm_per_ns(self) -> float:
        return velocity_mm_per_ns(self.eps_eff)

    @property
    def z_before_peel(self) -> np.ndarray:
        """Impedance just before each interface (the port's Z0 before the first)."""
        if not self.z_peel.size:
            return self.z_peel
        return np.concatenate(([self.z0], self.z_peel[:-1]))

    def resolution_text(self) -> str:
        return (f"10-90 % rise time {self.rise_time_ns * 1e3:.1f} ps from f_max = "
                f"{self.f_max_ghz:.2f} GHz, beta = {self.beta:g}: distance resolution "
                f"{self.resolution_mm:.2f} mm at eps_eff = {self.eps_eff:.2f}; "
                f"features shorter than this blur together")


def _prepare(freq_ghz, reflection, *, eps_eff, beta, pad, dc, fit_span_ghz):
    spec = harmonic_spectrum(freq_ghz, reflection, dc=dc, fit_span_ghz=fit_span_ghz)
    window = half_kaiser(spec.s.size, beta)
    a, sample_ns = impulse_sequence(spec.s * window, spec.df_ghz, pad)
    v = velocity_mm_per_ns(eps_eff)
    rise = rise_time_ns(spec.f_max_ghz, beta)
    return spec, a, sample_ns, v, rise, v * rise / 2.0


def analyse(freq_ghz, reflection, *, name: str = "", z0: float = DEFAULT_Z0,
            eps_eff: float = DEFAULT_EPS_EFF, beta: float = DEFAULT_BETA,
            pad: int = DEFAULT_PAD, dc: Optional[float] = None,
            fit_span_ghz: float = DEFAULT_FIT_SPAN_GHZ, max_mm: float = DEFAULT_MAX_MM,
            port: int = 1) -> TDRResult:
    """The layer-peeled profile seen from one port (method "peel")."""
    freq = np.asarray(freq_ghz, dtype=float).ravel()
    spec, a, sample_ns, v, rise, resolution = _prepare(
        freq, reflection, eps_eff=eps_eff, beta=beta, pad=pad, dc=dc, fit_span_ghz=fit_span_ghz)
    layer_mm = v * sample_ns / 2.0
    n_layers = min(int(math.floor(max_mm / layer_mm + 1e-9)), a.size - 1)
    k = np.arange(n_layers)
    t_ns = k * sample_ns
    x_mm = k * layer_mm
    rho = np.cumsum(a)[:n_layers]
    with np.errstate(divide="ignore", invalid="ignore"):
        z_naive = np.where(np.abs(rho) < 1.0, z0 * (1.0 + rho) / (1.0 - rho), np.nan)
    peel = layer_peel(a, z0, n_layers)
    notes = []
    if spec.n_extrapolated:
        notes.append(f"{spec.n_extrapolated} bin(s) below the first sweep point (incl. DC) extrapolated")
    if float(freq.min()) > LOW_START_GHZ:
        notes.append(f"sweep starts at {float(freq.min()):g} GHz: the band below it is extrapolated, "
                     f"so the profile is indicative only (tdr_validation: a 2 GHz start fails)")
    if not peel.stable:
        notes.append(peel.note)
    m = peel.r.size
    return TDRResult(
        name=name, port=port, method="layer peel", z0=z0, eps_eff=eps_eff, beta=beta, pad=pad,
        df_ghz=spec.df_ghz, f_min_ghz=float(freq.min()), f_max_ghz=spec.f_max_ghz,
        n_points=int(freq.size), dc_value=float(np.real(spec.s[0])), dc_source=spec.dc_source,
        n_extrapolated=spec.n_extrapolated, delay_removed_ns=spec.delay_ns,
        sample_ns=sample_ns, rise_time_ns=rise, resolution_mm=resolution,
        naive_t_ns=t_ns, naive_x_mm=x_mm, rho_step=rho, z_naive=z_naive,
        layer_mm=layer_mm, profile_t_ns=t_ns[:m], profile_x_mm=x_mm[:m],
        r=peel.r, z_peel=peel.z_after, stable=peel.stable, notes=notes,
    )


def analyse_fit_multi(freq_ghz, s_matrix, *, line_length_mm: float, name: str = "",
                      z0: float = DEFAULT_Z0, eps_eff: float = DEFAULT_EPS_EFF,
                      beta: float = DEFAULT_BETA, pad: int = DEFAULT_PAD, dc: Optional[float] = None,
                      fit_span_ghz: float = DEFAULT_FIT_SPAN_GHZ, port: int = 1,
                      cell_fraction: float = DEFAULT_FIT_CELL, tol: float = FIT_TOL,
                      loss: bool = True, delay: bool = True,
                      blocks: Sequence[str] = ("11", "21", "22"), penalty: str = "tv",
                      start: str = "peel") -> TDRResult:
    """The regularised profile from the whole 2-port: S11, S21 and S22 in one fit.

    line_length_mm is the distance between the two reference planes: the model's far
    port sits there, so S22 sees the trace from the right end. Each block is divided by
    its own RMS magnitude, so each counts equally in the residual, and tol is relative
    to that scale. port=2 analyses the same line from the other end (S11 and S22
    swapped, S12 = S21), so a mismatch at x from port 1 appears at L - x from port 2.

    penalty "tv" (default) is the edge-preserving total variation of ln Z (IRLS);
    "l2" is the first-difference L2 penalty. start "peel" begins from the layer peel
    where that is stable, otherwise from 50 ohm everywhere.
    """
    freq = np.asarray(freq_ghz, dtype=float).ravel()
    s = np.asarray(s_matrix, dtype=complex).reshape(-1, 2, 2)
    if port == 2:
        s = s[:, ::-1, ::-1]
    reflection = s[:, 0, 0]
    spec, a, sample_ns, v, rise, resolution = _prepare(
        freq, reflection, eps_eff=eps_eff, beta=beta, pad=pad, dc=dc, fit_span_ghz=fit_span_ghz)
    n_naive = min(int(math.floor((line_length_mm + 6.0) / (v * sample_ns / 2.0) + 1e-9)), a.size - 1)
    naive_t = np.arange(n_naive) * sample_ns
    naive_x = np.arange(n_naive) * v * sample_ns / 2.0
    rho = np.cumsum(a)[:n_naive]
    with np.errstate(divide="ignore", invalid="ignore"):
        z_naive = np.where(np.abs(rho) < 1.0, z0 * (1.0 + rho) / (1.0 - rho), np.nan)

    n_cells = max(1, int(round(line_length_mm / (cell_fraction * resolution))))
    cell_mm = line_length_mm / n_cells
    keep = np.arange(freq.size)
    if freq.size > FIT_MAX_POINTS:
        keep = np.unique(np.round(np.linspace(0, freq.size - 1, FIT_MAX_POINTS)).astype(int))
    f_fit = freq[keep]
    full = {"11": s[:, 0, 0], "21": s[:, 1, 0], "22": s[:, 1, 1]}
    data = {b: full[b][keep] for b in blocks}
    weights = {b: 1.0 / math.sqrt(float(np.mean(np.abs(data[b]) ** 2))) for b in blocks}
    model = LineModel(f_fit, cell_mm, n_cells, eps_eff, z0, loss=loss, delay=delay, blocks=blocks)

    u0 = np.zeros(n_cells)
    start_note = "start 50 ohm"
    if start == "peel":
        peel = analyse(freq, reflection, eps_eff=eps_eff, beta=beta, pad=pad, dc=dc,
                       fit_span_ghz=fit_span_ghz, max_mm=line_length_mm + 6.0, z0=z0)
        if peel.stable and peel.r.size > 1:
            x_peel = peel.profile_x_mm + 0.5 * peel.layer_mm
            x_cells = (np.arange(n_cells) + 0.5) * cell_mm
            z_start = np.clip(np.interp(x_cells, x_peel, peel.z_peel), 0.2 * z0, 20.0 * z0)
            u0 = np.log(z_start / z0)
            start_note = "start from the layer peel"
    p0 = np.zeros(model.n_params)
    p0[:n_cells] = u0
    p0[n_cells] = math.log(LOSS_START_NP_PER_MM)
    p0[n_cells + 1] = math.log(LOSS_START_NP_PER_MM)
    p, rms, lam, converged = fit_profile(model, data, weights, tol=tol, p0=p0,
                                         tv=(penalty == "tv"))
    ac, ad, scale = model.unpack(p)
    z_cells = z0 * np.exp(p[:n_cells])
    z_prev = np.concatenate(([z0], z_cells[:-1]))
    r = (z_cells - z_prev) / (z_cells + z_prev)
    fit_sample_ns = 2.0 * cell_mm / v
    loss_db_m_10 = (ac * math.sqrt(10.0) + ad * 10.0) * 1000.0 * 8.685889638
    fit = {"a_c_np_per_mm": ac, "a_d_np_per_mm_per_ghz": ad, "loss_db_per_m_at_10ghz": loss_db_m_10,
           "delay_scale": scale, "eps_fit": eps_eff * scale ** 2, "rms": rms, "lambda": lam,
           "converged": converged, "blocks": list(blocks), "penalty": penalty, "start": start_note}
    notes = []
    if spec.n_extrapolated:
        notes.append(f"{spec.n_extrapolated} bin(s) below the first sweep point extrapolated "
                     f"(naive step only; the fit uses the data as measured)")
    if float(freq.min()) > LOW_START_GHZ:
        notes.append(f"sweep starts at {float(freq.min()):g} GHz: nothing constrains the profile below it, "
                     f"so the profile is indicative only (tdr_validation: a 2 GHz start fails)")
    notes.append(f"fit {'+'.join(blocks)} ({penalty}, {start_note}): {n_cells} cells of {cell_mm:.3f} mm "
                 f"over {line_length_mm:g} mm, RMS residual {rms:.4f} (tol {tol:g}), lambda {lam:.3g}"
                 + ("" if converged else " -- tolerance NOT reached"))
    notes.append(f"loss a_d {ad:.3g} Np/mm per GHz ({loss_db_m_10:.3g} dB/m at 10 GHz), "
                 f"eps_fit {eps_eff * scale ** 2:.3f} (given {eps_eff:g})")
    return TDRResult(
        name=name, port=port, method="regularised fit", z0=z0, eps_eff=eps_eff, beta=beta,
        pad=pad, df_ghz=spec.df_ghz, f_min_ghz=float(freq.min()), f_max_ghz=spec.f_max_ghz,
        n_points=int(freq.size), dc_value=float(np.real(spec.s[0])), dc_source=spec.dc_source,
        n_extrapolated=spec.n_extrapolated, delay_removed_ns=spec.delay_ns,
        sample_ns=sample_ns, rise_time_ns=rise, resolution_mm=resolution,
        naive_t_ns=naive_t, naive_x_mm=naive_x, rho_step=rho, z_naive=z_naive,
        layer_mm=cell_mm, profile_t_ns=np.arange(n_cells) * fit_sample_ns,
        profile_x_mm=np.arange(n_cells) * cell_mm, r=r, z_peel=z_cells,
        stable=bool(converged), notes=notes, fit=fit,
    )


def analyse_fit(freq_ghz, reflection, *, name: str = "", z0: float = DEFAULT_Z0,
                eps_eff: float = DEFAULT_EPS_EFF, beta: float = DEFAULT_BETA,
                pad: int = DEFAULT_PAD, dc: Optional[float] = None,
                fit_span_ghz: float = DEFAULT_FIT_SPAN_GHZ, max_mm: float = DEFAULT_MAX_MM,
                port: int = 1, cell_fraction: float = DEFAULT_FIT_CELL,
                tol: float = FIT_TOL, loss: bool = True, delay: bool = True,
                penalty: str = "tv", start: str = "peel") -> TDRResult:
    """One-port regularised fit (S11 only, or the port's reflection as given): the same
    model and fit as `analyse_fit_multi` with the one block. The far end is not a
    reference plane here, so the cells cover max_mm."""
    freq = np.asarray(freq_ghz, dtype=float).ravel()
    s_one = np.zeros((freq.size, 2, 2), dtype=complex)
    s_one[:, 0, 0] = np.asarray(reflection, dtype=complex).ravel()
    result = analyse_fit_multi(freq, s_one, line_length_mm=max_mm, name=name, z0=z0, eps_eff=eps_eff,
                               beta=beta, pad=pad, dc=dc, fit_span_ghz=fit_span_ghz, port=1,
                               cell_fraction=cell_fraction, tol=tol, loss=loss, delay=delay,
                               blocks=("11",), penalty=penalty, start=start)
    result.port = port
    return result


def analyse_sparameters(sp: SParameters, *, port: int = 1, method: str = "peel",
                        **kwargs) -> TDRResult:
    """Port 1 measures from S11 (or the whole matrix for the multi-port fit); port 2 from S22.

    method "peel" is the layer peel of the port's reflection; method "fit" is the
    regularised fit: the whole 2-port when line_length_mm is given, else S11/S22 only.
    """
    if port not in (1, 2):
        raise ValueError("port must be 1 or 2")
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    name = kwargs.pop("name", sp.source)
    if method == "fit" and kwargs.get("line_length_mm") is not None:
        return analyse_fit_multi(sp.freq_ghz, sp.s, port=port, name=name, z0=sp.z0, **kwargs)
    kwargs.pop("line_length_mm", None)
    column = 0 if port == 1 else 1
    runner = analyse if method == "peel" else analyse_fit
    return runner(sp.freq_ghz, sp.s[:, column, column], z0=sp.z0, port=port, name=name, **kwargs)


# ---------------------------------------------------------------- mismatches


@dataclass(frozen=True)
class Mismatch:
    """A step (a level change that holds across one resolution) or an excursion (a narrow
    feature, at most one resolution wide, that departs from the levels on both sides, which
    agree). An excursion is reported by its area (ohm mm) and peak: a feature narrower than the
    resolution is not dropped by a per-sample threshold."""

    kind: str                 # "step" or "excursion"
    position_mm: float
    z_before_ohm: float
    z_after_ohm: float
    reflection: float         # |rho| equivalent: of the step, or of the largest departure of an excursion
    area_ohm_mm: float = float("nan")   # excursion: integral of (Z - baseline) dx
    peak_ohm: float = float("nan")      # excursion: the extreme of (Z - baseline), signed
    width_mm: float = 0.0
    note: str = ""


@dataclass(frozen=True)
class FirstDeviation:
    position_mm: float
    z_ohm: float


def _window_medians(v: np.ndarray, w: int):
    """(left, right): the median of v over the w cells before and after each boundary k.

    A median, not a mean: a feature narrower than half a window does not move it. The port
    (ln Z0 = 0) lies before cell 0, so the left window is padded with port cells.
    """
    n = v.size
    left = np.zeros(n)
    right = np.zeros(n)
    for k in range(n):
        before = v[max(0, k - w):k]
        left[k] = float(np.median(np.concatenate((np.zeros(max(0, w - k)), before))))
        right[k] = float(np.median(v[k:min(n, k + w)]))
    return left, right


def _sub_departures(dev: np.ndarray, eps: float) -> int:
    """How many separate runs of cells depart from the baseline by at least eps (ln)."""
    on = np.abs(dev) >= eps
    return int(np.sum(on & ~np.concatenate(([False], on[:-1]))))


def _narrow_excursions(v: np.ndarray, z: np.ndarray, z0: float, jump_thr: float, w: int):
    """Regions at most w cells wide, between two wide levels that agree, that depart from them.

    A wide level is a run of more than w cells between cell-to-cell jumps of at least jump_thr.
    The port (ln Z0 = 0, Z0 ohm) is a wide level before cell 0. Everything between two consecutive
    wide levels is one region, so a compound dip (two features, a short return to the baseline
    between them) is one excursion. Returns the regions with their departure from the baseline.
    """
    n = v.size
    v_prev = np.concatenate(([0.0], v[:-1]))
    brk = np.flatnonzero(np.abs(v - v_prev) >= jump_thr)
    starts = np.unique(np.concatenate(([0], brk)))
    ends = np.append(starts[1:], n)
    segs = [(int(a), int(b), float(np.mean(v[a:b])), float(np.mean(z[a:b])))
            for a, b in zip(starts, ends)]
    wide = [-1] + [i for i, seg in enumerate(segs) if seg[1] - seg[0] > w]
    out = []
    for p, q in zip(wide[:-1], wide[1:]):
        if p != -1 and q - p < 2:
            continue  # two wide levels with nothing between them: a step
        if p == -1:
            lv, zv, edge_left = 0.0, z0, 0
        else:
            edge_left, lv, zv = segs[p][1], segs[p][2], segs[p][3]
        if q >= len(segs):
            continue  # no wide level on the far side
        edge_right, lr, zr = segs[q][0], segs[q][2], segs[q][3]
        if edge_right <= edge_left or edge_right - edge_left > w:
            continue
        if abs(lv - lr) >= jump_thr:
            continue  # the levels on the two sides disagree: a step, not an excursion
        base = 0.5 * (lv + lr)
        dev = v[edge_left:edge_right] - base
        if float(np.max(np.abs(dev))) < jump_thr:
            continue  # no departure from the baseline
        out.append({"a": edge_left, "b": edge_right, "zp": zv, "zn": zr,
                    "zb": 0.5 * (zv + zr), "dev_max": float(np.max(np.abs(dev))),
                    "sub": _sub_departures(dev, 0.5 * jump_thr)})
    return out


def find_mismatches(result: TDRResult, threshold: float = DEFAULT_THRESHOLD,
                    deviation_tol: float = DEFAULT_DEVIATION_TOL):
    """(features, first_deviation) from the profile.

    Thresholds are reflection-equivalent. A step is a change of the median ln-impedance across one
    resolution, dv = ln(Z_after / Z_before), reported when |rho| = tanh(|dv| / 2) >= threshold. An
    excursion is a narrow region (see _narrow_excursions) whose area, the integral of (Z - baseline)
    dx, is at least the Born area: |S11| = beta A / Z0 reaches the threshold at f_max, so
    A = Z0 threshold / beta. Excursions are taken out of the profile before the steps are looked
    for, so a dip cannot pose as a step at its own edge. Features closer than one resolution are
    merged, with a note. The first deviation is the first cell whose impedance differs from Z0 by
    more than deviation_tol (relative).
    """
    n = result.r.size
    if n == 0:
        return [], None
    h = result.layer_mm
    x = result.profile_x_mm[:n]
    xc = x + 0.5 * h
    z = result.z_peel[:n]
    z0 = result.z0
    v = np.log(z / z0)
    jump_thr = 2.0 * math.atanh(min(threshold, 0.999))
    w = max(1, int(round(result.resolution_mm / h)))
    beta_fmax = 2.0 * math.pi * result.f_max_ghz * math.sqrt(result.eps_eff) / C_MM_PER_NS
    area_thr = z0 * threshold / beta_fmax

    excursions = _narrow_excursions(v, z, z0, jump_thr, w)
    v_clean = v.copy()
    z_clean = z.copy()
    for e in excursions:
        v_clean[e["a"]:e["b"]] = np.log(e["zb"] / z0)
        z_clean[e["a"]:e["b"]] = e["zb"]

    found: List[Mismatch] = []

    # Steps, on the profile with the excursions taken out.
    left, right = _window_medians(v_clean, w)
    delta = right - left
    sign = np.sign(delta)
    big = np.abs(delta) >= jump_thr
    k = 0
    while k < n:
        if not big[k]:
            k += 1
            continue
        j = k
        while j < n and big[j] and sign[j] == sign[k]:
            j += 1
        top = float(np.max(np.abs(delta[k:j])))
        flat = np.flatnonzero(np.abs(delta[k:j]) >= top * (1.0 - 1e-9)) + k
        kstar = int(round(float(np.mean(flat))))
        wl = slice(max(0, kstar - w), kstar)
        wr = slice(kstar, min(n, kstar + w))
        z_before = float(np.median(z_clean[wl])) if kstar > 0 else z0
        z_after = float(np.median(z_clean[wr]))
        found.append(Mismatch("step", float(kstar * h), z_before, z_after,
                              float(math.tanh(top / 2.0))))
        k = j

    # Excursions, by area.
    for e in excursions:
        a, b = e["a"], e["b"]
        dz = z[a:b] - e["zb"]
        area = float(np.sum(dz) * h)
        if abs(area) < area_thr:
            continue
        idx = int(np.argmax(np.abs(dz)))
        weights = np.abs(dz)
        centre = float(np.sum(xc[a:b] * weights) / np.sum(weights))
        note = ""
        if e["sub"] >= 2:
            note = (f"{e['sub']} features closer than the resolution ({result.resolution_mm:.2f} mm): "
                    f"reported as one")
        found.append(Mismatch("excursion", centre, e["zp"], e["zn"],
                              float(math.tanh(e["dev_max"] / 2.0)),
                              area_ohm_mm=area, peak_ohm=float(dz[idx]), width_mm=(b - a) * h, note=note))

    found.sort(key=lambda m: m.position_mm)
    merged: List[Mismatch] = []
    for m in found:
        if merged and abs(m.position_mm - merged[-1].position_mm) < result.resolution_mm:
            p = merged[-1]
            separation = abs(m.position_mm - p.position_mm)
            note = (f"two features {separation:.2f} mm apart, closer than the resolution "
                    f"({result.resolution_mm:.2f} mm): reported as one")
            both_excursions = p.kind == "excursion" and m.kind == "excursion"
            area = (p.area_ohm_mm + m.area_ohm_mm) if both_excursions else float("nan")
            dominant = m if abs(np.nan_to_num(m.peak_ohm)) > abs(np.nan_to_num(p.peak_ohm)) else p
            merged[-1] = Mismatch(
                kind=p.kind if p.kind == m.kind else "step",
                position_mm=0.5 * (p.position_mm + m.position_mm),
                z_before_ohm=p.z_before_ohm, z_after_ohm=m.z_after_ohm,
                reflection=max(p.reflection, m.reflection),
                area_ohm_mm=area, peak_ohm=dominant.peak_ohm,
                width_mm=p.width_mm + m.width_mm, note=note)
        else:
            merged.append(m)

    deviating = np.abs(z - z0) > deviation_tol * z0
    first = None
    if np.any(deviating):
        idx = int(np.argmax(deviating))
        first = FirstDeviation(float(x[idx]), float(z[idx]))
    return merged, first


# ---------------------------------------------------------------- output


def write_profile_csv(result: TDRResult, path) -> None:
    """One row per profile interface: distance, round trip, reflection, impedances.
    The naive TDR impedance is interpolated onto the profile's distances."""
    n = result.r.size
    if result.naive_x_mm.size and n:
        z_naive = np.interp(result.profile_x_mm[:n], result.naive_x_mm, result.z_naive)
    else:
        z_naive = np.full(n, np.nan)
    with Path(path).open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["index", "distance_mm", "round_trip_ns", "r_interface",
                         "z_profile_after_ohm", "z_naive_interp_ohm"])
        for k in range(n):
            writer.writerow([k, f"{result.profile_x_mm[k]:.4f}", f"{result.profile_t_ns[k]:.6f}",
                             f"{result.r[k]:.6f}", f"{result.z_peel[k]:.4f}", f"{z_naive[k]:.4f}"])


def summary_text(result: TDRResult, mismatches: Sequence[Mismatch],
                 first: Optional[FirstDeviation], threshold: float) -> str:
    lines = [
        f"{result.name}: port {result.port}, method {result.method}, {result.n_points} points "
        f"{result.f_min_ghz:g}-{result.f_max_ghz:g} GHz (df {result.df_ghz * 1e3:.2f} MHz)",
        f"eps_eff = {result.eps_eff:.3f}, v = {result.velocity_mm_per_ns:.2f} mm/ns, "
        f"Z0 = {result.z0:g} ohm, profile step = {result.layer_mm:.3f} mm",
        f"DC: {result.dc_value:+.4f} ({result.dc_source}); delay removed {result.delay_removed_ns * 1e3:.1f} ps",
        result.resolution_text(),
        (f"{result.method}: " + ("recursion stable" if result.method == "layer peel" else "converged")
         if result.stable else f"{result.method}: NOT stable (cut short or not converged)")
        + (f"; notes: {'; '.join(result.notes)}" if result.notes else ""),
        "first deviation from Z0: "
        + (f"{first.position_mm:.2f} mm, Z = {first.z_ohm:.1f} ohm" if first else "none"),
        f"mismatches with |r| >= {threshold:g}: {len(mismatches)}",
    ]
    for m in mismatches:
        if m.kind == "excursion":
            body = (f"excursion, width {m.width_mm:.2f} mm, area {m.area_ohm_mm:+.2f} ohm mm, "
                    f"peak {m.peak_ohm:+.1f} ohm, from {m.z_before_ohm:.1f} ohm")
        else:
            body = f"step {m.z_before_ohm:6.1f} -> {m.z_after_ohm:6.1f} ohm"
        lines.append(f"  x = {m.position_mm:7.2f} mm   {body}   |rho| = {m.reflection:.3f}"
                     + (f"   [{m.note}]" if m.note else ""))
    return "\n".join(lines)


def plot_profile(result: TDRResult, path, *, threshold: float = DEFAULT_THRESHOLD,
                 truth=None, title_suffix: str = "") -> None:
    """Z against round-trip time (primary axis), distance on the top axis.

    ``truth`` is an optional vectorised callable x_mm -> Z(ohm) for the true
    profile, drawn stepped. Drawn with the Agg canvas (no pyplot, no window).
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(9.0, 7.0), dpi=110)
    FigureCanvasAgg(fig)
    top = fig.add_subplot(2, 1, 1)
    bottom = fig.add_subplot(2, 1, 2, sharex=top)
    v = result.velocity_mm_per_ns
    n = result.r.size
    if result.naive_t_ns.size:
        top.plot(result.naive_t_ns, result.z_naive, color="#9aa0a6", lw=1.0,
                 label="naive TDR (step response)")
    if n:
        top.step(result.profile_t_ns, result.z_peel, where="post", color="#1a73e8", lw=1.6,
                 label=f"{result.method} profile")
    if truth is not None and n:
        z_true = np.asarray(truth(result.profile_x_mm + 0.5 * result.layer_mm), dtype=float)
        top.step(result.profile_t_ns, z_true, where="post", color="#d93025", lw=1.0, ls="--",
                 label="true profile")
    values = np.concatenate([result.z_naive[np.isfinite(result.z_naive)], result.z_peel])
    if values.size:
        top.set_ylim(0.0, max(2.0 * result.z0, 1.15 * float(np.max(values))))
    top.set_ylabel("impedance (ohm)")
    top.grid(alpha=0.3)
    top.legend(loc="upper right", fontsize=8)
    top.set_title(f"{result.name}: eps_eff = {result.eps_eff:.2f} (v = {v:.1f} mm/ns), "
                  f"resolution {result.resolution_mm:.2f} mm, beta {result.beta:g}{title_suffix}",
                  fontsize=9)
    secondary = top.secondary_xaxis("top", functions=(lambda tt: 0.5 * v * tt, lambda xx: 2.0 * xx / v))
    secondary.set_xlabel("distance from the port (mm), x = v t / 2")
    bottom.plot(result.profile_t_ns, np.abs(result.r), color="#1a73e8", lw=1.0)
    bottom.axhline(threshold, color="#d93025", lw=0.8, ls=":")
    bottom.set_ylabel("|r| per interface")
    bottom.set_xlabel("round-trip time (ns)")
    bottom.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(str(path))


def _cli_output_name(label: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in label)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m microwave_package.workflow.tdr",
        description="TDR of a run's broad S-parameters: impedance along the line and its mismatches.")
    parser.add_argument("path", help="a run folder, a combo folder, a .s2p or a results.csv")
    parser.add_argument("--eps-eff", type=float, default=DEFAULT_EPS_EFF,
                        help="effective permittivity given as the start of the fit (default 2.0, CPW on RO3003)")
    parser.add_argument("--out", required=True, help="folder for the profile CSVs and plots")
    parser.add_argument("--port", type=int, choices=(1, 2), default=1,
                        help="which port the distance runs from (1: S11, 2: S22)")
    parser.add_argument("--method", choices=("peel", "fit", "both"), default="both",
                        help="layer peel, regularised fit, or both (default both)")
    parser.add_argument("--line-length-mm", type=float, default=None,
                        help="distance between the two reference planes: the fit then uses S11, S21 and S22 "
                             "(without it, the fit uses S11 only over --max-mm)")
    parser.add_argument("--beta", type=float, default=DEFAULT_BETA, help="Kaiser window beta")
    parser.add_argument("--pad", type=int, default=DEFAULT_PAD, help="zero-padding factor (4 to 8)")
    parser.add_argument("--dc", type=float, default=None, help="DC value of S11 (default: fitted)")
    parser.add_argument("--fit-span-ghz", type=float, default=DEFAULT_FIT_SPAN_GHZ,
                        help="span of the lowest data used to extrapolate DC")
    parser.add_argument("--max-mm", type=float, default=DEFAULT_MAX_MM, help="longest distance reported")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="|rho| for a feature")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    methods = ("peel", "fit") if args.method == "both" else (args.method,)
    for label, sp in load_inputs(args.path):
        for method in methods:
            if method == "peel":
                kwargs = dict(eps_eff=args.eps_eff, beta=args.beta, pad=args.pad, max_mm=args.max_mm,
                              dc=args.dc, fit_span_ghz=args.fit_span_ghz)
            elif args.line_length_mm is not None:
                kwargs = dict(eps_eff=args.eps_eff, beta=args.beta, pad=args.pad,
                              line_length_mm=args.line_length_mm)
            else:
                kwargs = dict(eps_eff=args.eps_eff, beta=args.beta, pad=args.pad, max_mm=args.max_mm)
            result = analyse_sparameters(sp, port=args.port, method=method, name=label, **kwargs)
            stem = f"{_cli_output_name(label)}_{method}"
            write_profile_csv(result, out / f"{stem}_profile.csv")
            plot_profile(result, out / f"{stem}_tdr.png", threshold=args.threshold)
            features, first = find_mismatches(result, threshold=args.threshold)
            text = summary_text(result, features, first, args.threshold)
            (out / f"{stem}_summary.txt").write_text(text + "\n", encoding="utf-8")
            print(text)
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
