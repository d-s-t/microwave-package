"""
Time-domain reflectometry (TDR) of a run's S-parameters: where along the
line the impedance changes, and what it is at each point.

    python -m microwave_package.workflow.tdr <run dir | .s2p | results.csv> --eps-eff 2.0 --out <dir>

Read-only. Pure numpy; matplotlib only for the PNG, drawn without pyplot.

Method
------
1. Harmonic grid. S11 (port 1; S22 with ``port=2``) is put on f_k = k*df,
   k = 0..N, df = the sweep step. A sweep that starts above df, or sits off the
   grid, is interpolated after a linear phase (the delay estimated from the
   phase increments of the lowest points, where the extrapolation happens) is
   removed. The DC value and the bins below the
   first sweep frequency are extrapolated with a quadratic fit to the lowest
   ``fit_span_ghz`` of the data (at least 8 points). The DC value is made real:
   a lossless, reciprocal line into a matched load has a real reflection at DC.
   ``dc=`` overrides it.
2. Window. A half-Kaiser window (flat at DC, falling to 1/I0(beta) at f_max).
   beta trades resolution (rise time) against the ringing of the step response.
3. Impulse response. The one-sided spectrum is extended Hermitian (so the
   response is real), zero-padded to M = pad*(2N+2) points (pad 4 to 8) and
   inverse-transformed. a[n] is the sampled reflection impulse response with
   sample interval T = 1/(M*df), and sum(a) = S11(DC). Its cumulative sum is the
   reflected step a scope shows, rho(t); the naive TDR impedance is
   Z = Z0 (1+rho)/(1-rho). That is right only up to the first strong mismatch,
   because it ignores the multiple reflections.
4. Layer peeling (method "peel"). One sample is one round trip through one
   layer of one-way length dx = v*T/2, v = c/sqrt(eps_eff). With R_k the
   remaining response (R_0 = sum a[n] z^-n) and r_k its first sample, the
   reflection at interface k:
       X = (R_k - r_k) / (1 - r_k R_k)       (power series in z^-1)
       R_{k+1} = z X                          (shift by one sample)
       Z_{k+1} = Z_k (1 + r_k) / (1 - r_k),   Z_0 = Z0 (the port)
   Z_{k+1} is the impedance just beyond interface k, at x = k*dx. This handles
   the multiple reflections exactly, but it is ill-conditioned: its error grows
   by about 1e6 over 40 layers (exact input gives an exact profile; 1e-9 input
   noise gives 3e-3 ohm; 1e-4 makes it unstable). Band-limited windowed data is
   far from exact, so the peeled profile is only indicative beyond the first
   few strong steps. `stable` says whether the recursion stayed in |r| < 1.
5. Regularised fit (method "fit"). N section impedances (cells of half the
   resolution) are fitted to the UNWINDOWED S11 through the exact ABCD cascade
   of uniform lossless sections, with a first-difference smoothness penalty on
   ln Z:  minimise sum |S11_model - S11_data|^2 + lam sum (u_{i+1}-u_i)^2,
   u = ln(Z/Z0), Levenberg-Marquardt in numpy. lam is the largest value (the
   smoothest profile) whose RMS residual is within `tol` (0.5 % of |S11|, about
   -46 dB): the discrepancy principle. It needs no DC extrapolation.
6. Positions. x = v*t/2 with t the round trip, so the time axis is primary and
   distance is derived. eps_eff is a parameter; every plot prints it.
7. Resolution. The 10-90 % rise time of the windowed step is kappa(beta)/f_max
   (kappa from the window alone; 0.445 for no taper, 0.69 for beta 3), and the
   distance resolution is v*t_rise/2. Features narrower than that blur together.

Physics assumptions and limits
------------------------------
* Lossless line. Loss is not compensated: attenuation makes far-end features
  look weaker and the impedance drift at long delays.
* Dispersion-free line: eps_eff is one number at every frequency. A CPW's
  effective permittivity rises slightly with frequency, so distances at high
  frequency are a little long and the impedance a little off.
* One real reference impedance Z0 at both ports (the file's R; 50 ohm here).
* A feature narrower than the resolution is not separated from its neighbours.
  A short low-impedance section makes a reflection dipole whose band-limited
  amplitude is far below its true |r|: a 0.3 mm, 35 ohm section (true |r| 0.18
  at each edge) peels to |r| ~ 0.008 on the 0.05-100 GHz band, below the 0.03
  threshold (see tdr_validation). Part of the loss is the window: its taper
  leaves about 0.2 of the weight at f_max for beta 3.
* Per-sample |r| depends on the sample spacing: the layer peel spreads a step
  over about 1/(2 f_max T) samples, so a per-sample threshold means different
  things at different pads and bandwidths. This is a known limit of
  the threshold rule, not yet corrected.
* The sweep must cover the features. A sweep that starts at 2 GHz leaves the
  band below it to extrapolation (method "peel") or unconstrained (method
  "fit"); the DC-extrapolation error is reported by tdr_validation.
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
DEFAULT_FIT_CELL = 0.5  # fit cell = this x resolution
LOW_START_GHZ = 1.0  # a sweep starting above this does not reach the band the profile needs
FIT_TOL = 0.005
FIT_LAMBDA_GRID = tuple(10.0 ** e for e in range(3, -7, -1))  # decades, descending
FIT_MAX_POINTS = 300  # even decimation: the line's delays (< ~1 ns) need df < 0.5 GHz
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


def _forward_blocks(z: np.ndarray, cell_mm: float, freq_ghz: np.ndarray, eps_eff: float):
    """Per-section transfer matrices (N, F, 2, 2) and sinh(theta) (F,) of one cell."""
    beta = 2.0 * math.pi * freq_ghz * math.sqrt(eps_eff) / C_MM_PER_NS
    theta = 1j * beta * cell_mm
    ch, sh = np.cosh(theta), np.sinh(theta)
    blocks = np.empty((z.size, freq_ghz.size, 2, 2), dtype=complex)
    blocks[:, :, 0, 0] = ch
    blocks[:, :, 1, 1] = ch
    blocks[:, :, 0, 1] = z[:, None] * sh[None, :]
    blocks[:, :, 1, 0] = sh[None, :] / z[:, None]
    return blocks, sh


def _s11_from_total(total: np.ndarray, z0: float) -> np.ndarray:
    a, b, c, d = total[:, 0, 0], total[:, 0, 1], total[:, 1, 0], total[:, 1, 1]
    return (a + b / z0 - c * z0 - d) / (a + b / z0 + c * z0 + d)


def model_s11(u: np.ndarray, cell_mm: float, freq_ghz, eps_eff: float, z0: float) -> np.ndarray:
    """S11 of len(u) uniform sections (Z_i = z0 exp(u_i)) followed by a matched load."""
    f = np.asarray(freq_ghz, dtype=float)
    blocks, _ = _forward_blocks(z0 * np.exp(u), cell_mm, f, eps_eff)
    total = np.broadcast_to(np.eye(2, dtype=complex), (f.size, 2, 2)).copy()
    for block in blocks:
        total = total @ block
    return _s11_from_total(total, z0)


def model_s11_and_jacobian(u: np.ndarray, cell_mm: float, freq_ghz, eps_eff: float, z0: float):
    """S11 and dS11/du (F x N, complex) from prefix and suffix products of the cascade."""
    f = np.asarray(freq_ghz, dtype=float)
    z = z0 * np.exp(u)
    blocks, sh = _forward_blocks(z, cell_mm, f, eps_eff)
    n_sec = z.size
    eye = np.broadcast_to(np.eye(2, dtype=complex), (f.size, 2, 2)).copy()
    suffix = np.empty((n_sec + 1, f.size, 2, 2), dtype=complex)
    suffix[n_sec] = eye
    for i in range(n_sec - 1, -1, -1):
        suffix[i] = blocks[i] @ suffix[i + 1]
    total = suffix[0]
    s11 = _s11_from_total(total, z0)
    a, b, c, d = total[:, 0, 0], total[:, 0, 1], total[:, 1, 0], total[:, 1, 1]
    den = a + b / z0 + c * z0 + d
    num = a + b / z0 - c * z0 - d
    jac = np.empty((f.size, n_sec), dtype=complex)
    prefix = eye.copy()
    for i in range(n_sec):
        dblock = np.zeros((f.size, 2, 2), dtype=complex)
        dblock[:, 0, 1] = z[i] * sh          # d(Z sinh)/du = Z sinh
        dblock[:, 1, 0] = -sh / z[i]         # d(sinh/Z)/du = -sinh/Z
        dtotal = prefix @ dblock @ suffix[i + 1]
        da, db = dtotal[:, 0, 0], dtotal[:, 0, 1]
        dc, dd = dtotal[:, 1, 0], dtotal[:, 1, 1]
        dnum = da + db / z0 - z0 * dc - dd
        dden = da + db / z0 + z0 * dc + dd
        jac[:, i] = (dnum * den - num * dden) / den ** 2
        prefix = prefix @ blocks[i]
    return s11, jac


def _smoothing_gram(n: int) -> np.ndarray:
    """D^T D for first differences (D u)_i = u_{i+1} - u_i."""
    d = np.zeros((n - 1, n))
    idx = np.arange(n - 1)
    d[idx, idx] = -1.0
    d[idx, idx + 1] = 1.0
    return d.T @ d


def _fit_at_lambda(u0: np.ndarray, lam: float, cell_mm, f, s_data, eps_eff, z0,
                   max_iter: int = 60) -> Tuple[np.ndarray, float]:
    """Levenberg-Marquardt on ||S(u) - S_data||^2 + lam u^T D^T D u; returns (u, RMS residual)."""
    n = u0.size
    gram = _smoothing_gram(n)
    u = u0.copy()
    s, jac = model_s11_and_jacobian(u, cell_mm, f, eps_eff, z0)
    r = s - s_data
    cost = float(np.sum(np.abs(r) ** 2) + lam * u @ gram @ u)
    mu = 1e-3
    for _ in range(max_iter):
        jr = np.vstack([jac.real, jac.imag])
        rr = np.concatenate([r.real, r.imag])
        grad = jr.T @ rr + lam * gram @ u
        hess = jr.T @ jr + lam * gram
        accepted = False
        for _ in range(30):
            step = np.linalg.solve(hess + mu * np.eye(n), -grad)
            u_try = u + step
            r_try = model_s11(u_try, cell_mm, f, eps_eff, z0) - s_data
            cost_try = float(np.sum(np.abs(r_try) ** 2) + lam * u_try @ gram @ u_try)
            if cost_try < cost:
                accepted = True
                break
            mu *= 4.0
        if not accepted:
            break
        decrease = cost - cost_try
        u, cost = u_try, cost_try
        mu = max(mu / 3.0, 1e-12)
        s, jac = model_s11_and_jacobian(u, cell_mm, f, eps_eff, z0)
        r = s - s_data
        if decrease <= 1e-7 * max(cost, 1e-30):
            break
    return u, float(np.sqrt(np.mean(np.abs(r) ** 2)))


def fit_impedance(freq_ghz, reflection, *, z0: float, eps_eff: float, cell_mm: float,
                  max_mm: float, tol: float = FIT_TOL) -> Tuple[np.ndarray, float, float]:
    """The smoothest cell impedances whose S11 matches the data to tol (RMS).

    Continuation: lam is lowered one decade at a time, each fit starting from the
    previous solution, and the first lam whose RMS residual is within tol is the
    largest such lam, i.e. the smoothest acceptable profile. (A bisection over lam
    jumps between basins: the LM then stalls on a local minimum at small lam.)
    Returns (Z per cell, achieved RMS residual, lam). Cells are cell_mm long,
    starting at the port; the line beyond the last cell is matched.
    """
    f = np.asarray(freq_ghz, dtype=float).ravel()
    s_data = np.asarray(reflection, dtype=complex).ravel()
    if f.size > FIT_MAX_POINTS:  # evenly decimated, both band edges kept
        keep = np.unique(np.round(np.linspace(0, f.size - 1, FIT_MAX_POINTS)).astype(int))
        f, s_data = f[keep], s_data[keep]
    n = int(math.ceil(max_mm / cell_mm))
    u = np.zeros(n)
    rms = float("nan")
    lam = FIT_LAMBDA_GRID[-1]
    for lam in FIT_LAMBDA_GRID:  # descending: strongest smoothing first
        u, rms = _fit_at_lambda(u, lam, cell_mm, f, s_data, eps_eff, z0)
        if rms <= tol:
            break
    return z0 * np.exp(u), rms, lam


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


def analyse_fit(freq_ghz, reflection, *, name: str = "", z0: float = DEFAULT_Z0,
                eps_eff: float = DEFAULT_EPS_EFF, beta: float = DEFAULT_BETA,
                pad: int = DEFAULT_PAD, dc: Optional[float] = None,
                fit_span_ghz: float = DEFAULT_FIT_SPAN_GHZ, max_mm: float = DEFAULT_MAX_MM,
                port: int = 1, cell_fraction: float = DEFAULT_FIT_CELL,
                tol: float = FIT_TOL) -> TDRResult:
    """The regularised profile (method "fit"), with the same naive TDR and resolution.

    Its cells are cell_fraction x resolution long; r_k is the fitted step at
    each cell boundary, so the reflections are the cell-to-cell steps.
    """
    freq = np.asarray(freq_ghz, dtype=float).ravel()
    spec, a, sample_ns, v, rise, resolution = _prepare(
        freq, reflection, eps_eff=eps_eff, beta=beta, pad=pad, dc=dc, fit_span_ghz=fit_span_ghz)
    n_naive = min(int(math.floor(max_mm / (v * sample_ns / 2.0) + 1e-9)), a.size - 1)
    naive_t = np.arange(n_naive) * sample_ns
    naive_x = np.arange(n_naive) * v * sample_ns / 2.0
    rho = np.cumsum(a)[:n_naive]
    with np.errstate(divide="ignore", invalid="ignore"):
        z_naive = np.where(np.abs(rho) < 1.0, z0 * (1.0 + rho) / (1.0 - rho), np.nan)
    cell_mm = max(cell_fraction * resolution, 1e-3)
    z_cells, rms, lam = fit_impedance(freq, reflection, z0=z0, eps_eff=eps_eff,
                                      cell_mm=cell_mm, max_mm=max_mm, tol=tol)
    z_prev = np.concatenate(([z0], z_cells[:-1]))
    r = (z_cells - z_prev) / (z_cells + z_prev)
    n = z_cells.size
    fit_sample_ns = 2.0 * cell_mm / v
    notes = []
    if spec.n_extrapolated:
        notes.append(f"{spec.n_extrapolated} bin(s) below the first sweep point extrapolated "
                     f"(naive step only; the fit uses the data as measured)")
    if float(freq.min()) > LOW_START_GHZ:
        notes.append(f"sweep starts at {float(freq.min()):g} GHz: nothing constrains the profile below it, "
                     f"so the profile is indicative only (tdr_validation: a 2 GHz start fails)")
    notes.append(f"fit: {n} cells of {cell_mm:.3f} mm, RMS residual {rms:.4f} (tol {tol:g}), "
                 f"lambda {lam:.3g}" + ("" if rms <= tol else " -- tolerance NOT reached"))
    return TDRResult(
        name=name, port=port, method="regularised fit", z0=z0, eps_eff=eps_eff, beta=beta,
        pad=pad, df_ghz=spec.df_ghz, f_min_ghz=float(freq.min()), f_max_ghz=spec.f_max_ghz,
        n_points=int(freq.size), dc_value=float(np.real(spec.s[0])), dc_source=spec.dc_source,
        n_extrapolated=spec.n_extrapolated, delay_removed_ns=spec.delay_ns,
        sample_ns=sample_ns, rise_time_ns=rise, resolution_mm=resolution,
        naive_t_ns=naive_t, naive_x_mm=naive_x, rho_step=rho, z_naive=z_naive,
        layer_mm=cell_mm, profile_t_ns=np.arange(n) * fit_sample_ns,
        profile_x_mm=np.arange(n) * cell_mm, r=r, z_peel=z_cells,
        stable=bool(rms <= tol), notes=notes,
    )


def analyse_sparameters(sp: SParameters, *, port: int = 1, method: str = "peel",
                        **kwargs) -> TDRResult:
    """Port 1 uses S11; port 2 uses S22 (distance then runs from port 2)."""
    if port not in (1, 2):
        raise ValueError("port must be 1 or 2")
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    column = 0 if port == 1 else 1
    name = kwargs.pop("name", sp.source)
    runner = analyse if method == "peel" else analyse_fit
    return runner(sp.freq_ghz, sp.s[:, column, column], z0=sp.z0, port=port, name=name, **kwargs)


# ---------------------------------------------------------------- mismatches


@dataclass(frozen=True)
class Mismatch:
    position_mm: float
    z_before_ohm: float
    z_after_ohm: float
    reflection: float
    z_extreme_ohm: float
    note: str = ""


@dataclass(frozen=True)
class FirstDeviation:
    position_mm: float
    z_ohm: float


def find_mismatches(result: TDRResult, threshold: float = DEFAULT_THRESHOLD,
                    deviation_tol: float = DEFAULT_DEVIATION_TOL):
    """(mismatches, first_deviation) from the profile.

    A mismatch is a run of consecutive interfaces with |r| >= threshold. Its
    position is the |r|-weighted centre of the run. Two features whose centres
    are closer than one resolution are reported as one, with a note. The first
    deviation is the first interface whose impedance differs from Z0 by more
    than deviation_tol (relative).
    """
    n = result.r.size
    mag = np.abs(result.r)
    x = result.profile_x_mm[:n]
    z_before = result.z_before_peel
    z_after = result.z_peel
    above = mag >= threshold
    groups: List[Tuple[int, int]] = []
    i = 0
    while i < n:
        if above[i]:
            j = i
            while j < n and above[j]:
                j += 1
            groups.append((i, j))
            i = j
        else:
            i += 1
    found: List[Mismatch] = []
    for start, stop in groups:
        w = mag[start:stop]
        position = float(np.sum(x[start:stop] * w) / np.sum(w))
        zb = float(z_before[start])
        za = float(z_after[stop - 1])
        excursion = z_after[start:stop]
        z_extreme = float(excursion[np.argmax(np.abs(excursion - zb))])
        found.append(Mismatch(position, zb, za, float(np.max(w)), z_extreme))

    merged: List[Mismatch] = []
    for mismatch in found:
        if merged and abs(mismatch.position_mm - merged[-1].position_mm) < result.resolution_mm:
            previous = merged[-1]
            separation = abs(mismatch.position_mm - previous.position_mm)
            dominant = max((previous, mismatch), key=lambda m: abs(m.z_extreme_ohm - previous.z_before_ohm))
            merged[-1] = Mismatch(
                position_mm=0.5 * (previous.position_mm + mismatch.position_mm),
                z_before_ohm=previous.z_before_ohm,
                z_after_ohm=mismatch.z_after_ohm,
                reflection=max(previous.reflection, mismatch.reflection),
                z_extreme_ohm=dominant.z_extreme_ohm,
                note=(f"two features {separation:.2f} mm apart, closer than the resolution "
                      f"({result.resolution_mm:.2f} mm): reported as one"),
            )
        else:
            merged.append(mismatch)

    deviating = np.abs(z_after - result.z0) > deviation_tol * result.z0
    first = None
    if np.any(deviating):
        idx = int(np.argmax(deviating))
        first = FirstDeviation(float(x[idx]), float(z_after[idx]))
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
        lines.append(f"  x = {m.position_mm:7.2f} mm   Z {m.z_before_ohm:6.1f} -> {m.z_after_ohm:6.1f} ohm"
                     f"   |r| = {m.reflection:.3f}   extreme Z {m.z_extreme_ohm:6.1f}"
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
                        help="effective permittivity of the line (default 2.0, CPW on RO3003)")
    parser.add_argument("--out", required=True, help="folder for the profile CSVs and plots")
    parser.add_argument("--port", type=int, choices=(1, 2), default=1,
                        help="which port the distance runs from (1: S11, 2: S22)")
    parser.add_argument("--method", choices=("peel", "fit", "both"), default="both",
                        help="layer peel, regularised fit, or both (default both)")
    parser.add_argument("--beta", type=float, default=DEFAULT_BETA, help="Kaiser window beta")
    parser.add_argument("--pad", type=int, default=DEFAULT_PAD, help="zero-padding factor (4 to 8)")
    parser.add_argument("--dc", type=float, default=None, help="DC value of S11 (default: fitted)")
    parser.add_argument("--fit-span-ghz", type=float, default=DEFAULT_FIT_SPAN_GHZ,
                        help="span of the lowest data used to extrapolate DC")
    parser.add_argument("--max-mm", type=float, default=DEFAULT_MAX_MM, help="longest distance reported")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="|r| for a mismatch")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    methods = ("peel", "fit") if args.method == "both" else (args.method,)
    for label, sp in load_inputs(args.path):
        for method in methods:
            kwargs = dict(eps_eff=args.eps_eff, beta=args.beta, pad=args.pad, max_mm=args.max_mm,
                          name=label)
            if method == "peel":
                kwargs.update(dc=args.dc, fit_span_ghz=args.fit_span_ghz)
            result = analyse_sparameters(sp, port=args.port, method=method, **kwargs)
            stem = f"{_cli_output_name(label)}_{method}"
            write_profile_csv(result, out / f"{stem}_profile.csv")
            plot_profile(result, out / f"{stem}_tdr.png", threshold=args.threshold)
            mismatches, first = find_mismatches(result, threshold=args.threshold)
            text = summary_text(result, mismatches, first, args.threshold)
            (out / f"{stem}_summary.txt").write_text(text + "\n", encoding="utf-8")
            print(text)
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
