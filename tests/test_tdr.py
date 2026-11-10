"""Tests for the TDR calculator (microwave_package/workflow/tdr.py) and its
validation on exact dummy lines (workflow/tdr_validation.py).

Run: python -m unittest discover -s tests

The validation criteria are the ones written in tdr_validation.py, asserted here
exactly. A criterion the current method does not meet is marked expectedFailure
with the measured value in its docstring: the criterion is not loosened, and the
test starts failing ("unexpected success") as soon as the method meets it.
"""
import contextlib
import csv
import io
import math
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from microwave_package.workflow import tdr, tdr_validation as v

Z0 = 50.0


def _ladder_reflection_series(z_ladder, length: int) -> np.ndarray:
    """Exact reflection series of a lossless ladder (every layer one sample) between
    50 ohm ports: a Mobius recursion in z^-1, independent of the peeling code."""
    zs = [Z0] + list(z_ladder) + [Z0]
    r_list = [(zs[k + 1] - zs[k]) / (zs[k + 1] + zs[k]) for k in range(len(zs) - 1)]
    response = np.zeros(length)
    for rk in reversed(r_list):
        numerator = np.zeros(length)
        numerator[0] = rk
        numerator[1:] = response[:-1]
        denominator = np.zeros(length)
        denominator[0] = 1.0
        denominator[1:] = rk * response[:-1]
        quotient = np.zeros(length)
        for n in range(length):
            acc = numerator[n]
            if n:
                acc -= np.dot(denominator[1:n + 1], quotient[n - 1::-1])
            quotient[n] = acc / denominator[0]
        response = quotient
    return response


def _write_touchstone(path: Path, freq_ghz, s11, s21=None, s12=None, s22=None, fmt="MA") -> None:
    s21 = s21 if s21 is not None else np.zeros_like(s11)
    s12 = s12 if s12 is not None else s21
    s22 = s22 if s22 is not None else np.zeros_like(s11)
    lines = [f"# GHZ S {fmt} R 50"]
    for i, f in enumerate(freq_ghz):
        values = [s11[i], s21[i], s12[i], s22[i]]
        if fmt == "MA":
            cells = [f"{abs(z):.9g} {math.degrees(np.angle(z)):.9g}" for z in values]
        else:
            cells = [f"{z.real:.9g} {z.imag:.9g}" for z in values]
        lines.append(f"{f:.6g} " + " ".join(cells))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestSpectrum(unittest.TestCase):
    def test_half_kaiser_is_one_at_dc_and_i0_at_the_top(self):
        for beta in (0.0, 3.0):
            w = tdr.half_kaiser(101, beta)
            self.assertAlmostEqual(w[0], 1.0, places=12)
            self.assertAlmostEqual(w[-1], 1.0 / np.i0(beta), places=12)

    def test_brick_wall_rise_time_factor(self):
        """No taper: the 10-90 % rise of a brick-wall band is 2.8018 / (2 pi f_max)."""
        self.assertAlmostEqual(tdr.rise_time_factor(0.0), 2.8018 / (2.0 * math.pi), delta=0.003)

    def test_impulse_sequence_reproduces_the_spectrum(self):
        """a[n] = ifft of the Hermitian spectrum, so sum(a) = S(DC) and a single
        reflection r at sample n0 comes back as r at n0 (no extra scale)."""
        n = 64
        m_factor = 8
        spectrum = np.zeros(n + 1, dtype=complex)
        spectrum[0] = 0.25
        spectrum[1:] = 0.1 * np.exp(-0.3j * np.arange(1, n + 1))
        a, sample_ns = tdr.impulse_sequence(spectrum, df_ghz=0.05, pad=m_factor)
        self.assertAlmostEqual(float(np.sum(a)), 0.25, places=10)  # sum(a) = S(DC)
        m = m_factor * (2 * n + 2)
        self.assertAlmostEqual(sample_ns, 1.0 / (m * 0.05), places=12)
        # The DFT of a returns the spectrum on the grid bins.
        dft = np.fft.fft(a)
        np.testing.assert_allclose(dft[: n + 1], spectrum, atol=1e-12)


class TestInputs(unittest.TestCase):
    def test_touchstone_formats_and_port_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            freq = np.array([1.0, 2.0, 3.0])
            s11 = np.array([0.3 + 0.1j, 0.2 - 0.4j, -0.1 + 0.05j])
            s21 = np.array([0.9 - 0.1j, 0.8 + 0.2j, 0.95 + 0.0j])
            s22 = np.array([-0.2j, 0.1 + 0.1j, 0.05 - 0.02j])
            for fmt in ("MA", "RI"):
                path = Path(tmp) / f"x_{fmt}.s2p"
                _write_touchstone(path, freq, s11, s21, s21, s22, fmt=fmt)
                sp = tdr.read_touchstone(path)
                np.testing.assert_allclose(sp.freq_ghz, freq)
                np.testing.assert_allclose(sp.s[:, 0, 0], s11, atol=1e-7)
                np.testing.assert_allclose(sp.s[:, 1, 0], s21, atol=1e-7)  # S21 is the second column
                np.testing.assert_allclose(sp.s[:, 1, 1], s22, atol=1e-7)
                self.assertEqual(sp.z0, 50.0)

    def test_results_csv_keeps_only_the_broad_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.csv"
            header = ["freq_ghz", "s11_db", "s11_deg", "s21_db", "s21_deg", "s22_db", "s22_deg",
                      "s12_db", "s12_deg", "setup", "touchstone", "solution_type", "combo_id", "sweep"]
            rows = [
                [2.0, -20, 0, -0.1, 0, -20, 0, -0.1, 0, "Setup_1", "", "DrivenModal", "default", "coarse"],
                [2.05, -18, 30, -0.1, 0, -18, 30, -0.1, 0, "Setup_1", "", "DrivenModal", "default", "coarse"],
                [2.02, -5, 90, -0.1, 0, -5, 90, -0.1, 0, "Setup_1", "", "DrivenModal", "default", "Fine_1"],
            ]
            with path.open("w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerows(rows)
            sp = tdr.read_results_csv(path)
            self.assertEqual(sp.freq_ghz.size, 2)
            self.assertAlmostEqual(abs(sp.s[0, 0, 0]), 0.1, places=9)

    def test_run_folder_is_found_by_its_models_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            combo = Path(tmp) / "models" / "default"
            combo.mkdir(parents=True)
            freq = np.linspace(2, 20, 10)
            _write_touchstone(combo / "Setup_1_Sweep_1.s2p", freq, np.full(10, 0.1 + 0j))
            found = tdr.load_inputs(tmp)
            self.assertEqual([label for label, _ in found], ["default"])


class TestLayerPeel(unittest.TestCase):
    def test_exact_ladder_is_recovered(self):
        """Peeling the exact reflection series returns the ladder's impedances."""
        rng = np.random.default_rng(1)
        z_ladder = rng.uniform(30.0, 150.0, size=40)
        series = _ladder_reflection_series(z_ladder, 41)
        peel = tdr.layer_peel(series, Z0, 40)
        self.assertTrue(peel.stable)
        np.testing.assert_allclose(peel.z_after, z_ladder, rtol=1e-9)

    def test_unstable_input_is_flagged_not_hidden(self):
        peel = tdr.layer_peel(np.array([1.2, 0.1, 0.0, 0.0]), Z0, 3)
        self.assertFalse(peel.stable)
        self.assertIn("unstable", peel.note)


class TestJacobianAndFit(unittest.TestCase):
    def test_analytic_jacobian_matches_central_differences(self):
        rng = np.random.default_rng(3)
        f = np.linspace(0.5, 30.0, 60)
        u = rng.normal(0.0, 0.5, size=12)
        _, jac = tdr.model_s11_and_jacobian(u, 0.4, f, 2.0, Z0)
        h = 1e-6
        for i in range(u.size):
            up, dn = u.copy(), u.copy()
            up[i] += h
            dn[i] -= h
            fd = (tdr.model_s11(up, 0.4, f, 2.0, Z0) - tdr.model_s11(dn, 0.4, f, 2.0, Z0)) / (2 * h)
            np.testing.assert_allclose(jac[:, i], fd, atol=1e-7)

    def test_model_agrees_with_the_independent_abcd_cascade(self):
        f = np.linspace(0.5, 30.0, 25)
        line = v.DummyLine("check", (v.Section(50.0, 1.0), v.Section(500.0, 1.0), v.Section(50.0, 1.0)))
        u = np.log(np.array([50.0, 500.0, 50.0]) / Z0)
        np.testing.assert_allclose(tdr.model_s11(u, 1.0, f, 2.0, Z0), line.s_parameters(f)[:, 0, 0],
                                   atol=1e-12)

    def test_fit_reaches_its_tolerance_on_the_sharp_line(self):
        line = v.sharp_line()
        freq = v.BANDS["real"][1]
        s11 = line.s_parameters(freq)[:, 0, 0]
        result = tdr.analyse_fit(freq, s11, eps_eff=2.0, max_mm=line.length_mm + 6.0)
        self.assertTrue(result.stable)
        self.assertIn("RMS residual", result.notes[-1])


class TestNaiveAndResolution(unittest.TestCase):
    def test_single_edge_naive_and_peeled_impedance(self):
        """50 ohm to 100 ohm at 10 mm, back to 50 ohm at 20 mm (the real band): both
        the naive step and the peeled profile read 100 ohm between the edges."""
        line = v.DummyLine("step", (v.Section(50.0, 10.0), v.Section(100.0, 10.0), v.Section(50.0, 10.0)))
        freq = v.BANDS["real"][1]
        s11 = line.s_parameters(freq)[:, 0, 0]
        result = tdr.analyse(freq, s11, eps_eff=2.0, max_mm=line.length_mm + 6.0)
        x = result.naive_x_mm
        inside = (x > 10.0 + result.resolution_mm) & (x < 20.0 - result.resolution_mm)
        self.assertTrue(np.all(np.abs(result.z_naive[inside] - 100.0) < 0.05 * 100.0))
        xp = result.profile_x_mm[: result.r.size] + 0.5 * result.layer_mm
        inside_p = (xp > 10.0 + result.resolution_mm) & (xp < 20.0 - result.resolution_mm)
        self.assertLess(float(np.max(np.abs(result.z_peel[inside_p] - 100.0))), 5.0)

    def test_resolution_numbers(self):
        """Rise time and distance resolution at 20 GHz, beta 3, eps_eff 2 (the numbers
        the reports quote)."""
        rise_ns = tdr.rise_time_ns(20.0, 3.0)
        self.assertAlmostEqual(rise_ns * 1e3, 34.7, delta=0.3)
        resolution = tdr.velocity_mm_per_ns(2.0) * rise_ns / 2.0
        self.assertAlmostEqual(resolution, 3.68, delta=0.05)


class TestMismatches(unittest.TestCase):
    def _result(self, r, z_after, resolution_mm=1.0, layer_mm=0.1):
        n = len(r)
        x = np.arange(n) * layer_mm
        return tdr.TDRResult(
            name="synthetic", port=1, method="layer peel", z0=Z0, eps_eff=2.0, beta=3.0, pad=8,
            df_ghz=0.05, f_min_ghz=0.1, f_max_ghz=20.0, n_points=10, dc_value=0.0, dc_source="given",
            n_extrapolated=0, delay_removed_ns=0.0, sample_ns=0.01, rise_time_ns=0.03,
            resolution_mm=resolution_mm, naive_t_ns=x, naive_x_mm=x, rho_step=np.zeros(n),
            z_naive=np.full(n, np.nan), layer_mm=layer_mm, profile_t_ns=x, profile_x_mm=x,
            r=np.asarray(r, dtype=float), z_peel=np.asarray(z_after, dtype=float), stable=True)

    def test_step_and_dip_are_found_at_their_centres(self):
        r = np.zeros(40)
        r[10:13] = [0.05, 0.10, 0.05]          # an upward step at about 1.05 mm
        r[30:33] = [-0.04, -0.08, -0.04]       # a downward step at about 3.05 mm
        z_after = np.where(np.arange(40) < 10, 50.0, np.where(np.arange(40) < 30, 100.0, 50.0))
        mismatches, first = tdr.find_mismatches(self._result(r, z_after))
        self.assertEqual(len(mismatches), 2)
        self.assertAlmostEqual(mismatches[0].position_mm, 1.1, delta=0.05)
        self.assertAlmostEqual(mismatches[1].position_mm, 3.1, delta=0.05)
        self.assertAlmostEqual(mismatches[0].z_before_ohm, 50.0)
        self.assertAlmostEqual(mismatches[0].z_after_ohm, 100.0)
        self.assertAlmostEqual(mismatches[1].z_after_ohm, 50.0)
        self.assertAlmostEqual(first.position_mm, 1.0, delta=0.05)

    def test_two_features_closer_than_the_resolution_are_one_with_a_note(self):
        r = np.zeros(40)
        r[10] = 0.2
        r[14] = -0.2
        z_after = np.where(np.arange(40) < 10, 50.0, np.where(np.arange(40) < 14, 100.0, 50.0))
        mismatches, _ = tdr.find_mismatches(self._result(r, z_after, resolution_mm=1.0))
        self.assertEqual(len(mismatches), 1)
        self.assertIn("reported as one", mismatches[0].note)


class TestCalculatorBehaviour(unittest.TestCase):
    def test_dc_can_be_given(self):
        line = v.sharp_line()
        freq = v.BANDS["real"][1]
        s11 = line.s_parameters(freq)[:, 0, 0]
        result = tdr.analyse(freq, s11, dc=0.0, eps_eff=2.0, max_mm=line.length_mm + 6.0)
        self.assertEqual(result.dc_source, "given")
        self.assertEqual(result.dc_value, 0.0)

    def test_port_two_measures_from_the_other_end(self):
        """SHARP read from port 2: its 500 ohm section starts at 15 mm (not 20 mm)."""
        line = v.sharp_line()
        freq = v.BANDS["real"][1]
        sp = tdr.SParameters(freq, line.s_parameters(freq), Z0, "sharp")
        result = tdr.analyse_sparameters(sp, port=2, eps_eff=2.0, max_mm=line.length_mm + 6.0)
        mismatches, _ = tdr.find_mismatches(result)
        positions = sorted(m.position_mm for m in mismatches)
        self.assertLessEqual(abs(positions[0] - 15.0), result.resolution_mm)
        self.assertLessEqual(abs(positions[1] - 30.0), result.resolution_mm)

    def test_cli_writes_profile_csv_and_plot(self):
        line = v.sharp_line()
        freq = v.BANDS["real"][1]
        s = line.s_parameters(freq)
        with tempfile.TemporaryDirectory() as tmp:
            s2p = Path(tmp) / "sharp.s2p"
            _write_touchstone(s2p, freq, s[:, 0, 0], s[:, 1, 0], s[:, 0, 1], s[:, 1, 1])
            out = Path(tmp) / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                code = tdr.main([str(s2p), "--out", str(out), "--method", "peel", "--max-mm", "56"])
            self.assertEqual(code, 0)
            csv_path = out / "sharp_peel_profile.csv"
            self.assertTrue(csv_path.is_file())
            with csv_path.open() as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0][:3], ["index", "distance_mm", "round_trip_ns"])
            self.assertGreater(len(rows), 100)
            summary = (out / "sharp_peel_summary.txt").read_text(encoding="utf-8")
            self.assertIn("eps_eff = 2.000", summary)
            self.assertIn("distance resolution", summary)
            self.assertTrue((out / "sharp_peel_tdr.png").is_file())


class TestValidationCriteria(unittest.TestCase):
    """The criteria of tdr_validation, at the generous band (0.05-100 GHz) and the
    real band (0.1-20 GHz), with both methods; plus the 2 GHz and small-loss extras.
    The measured table is printed once, and saved as PNGs when TDR_VALIDATION_OUT is set."""

    reports = {}

    @classmethod
    def setUpClass(cls):
        cases = {}
        for method in ("peel", "fit"):
            sharp = v.sharp_line()
            for key in ("generous", "real"):
                cases[("SHARP", key, method)] = v.run_case(sharp, key, method=method, kind="sharp",
                                                           case="SHARP", edges=(20.0, 35.0), z_plateau=500.0)
                cases[("GRADUAL", key, method)] = v.run_case(v.gradual_line(), key, method=method,
                                                             kind="gradual", case="GRADUAL")
                cases[("CORNERS", key, method)] = v.run_case(v.corners_line(), key, method=method,
                                                             kind="corners", case="CORNERS")
            cases[("SHARP, starts 2 GHz", "from_2ghz", method)] = v.run_case(
                sharp, "from_2ghz", method=method, kind="sharp", case="SHARP, starts 2 GHz",
                edges=(20.0, 35.0), z_plateau=500.0)
            cases[("SHARP, small loss", "real", method)] = v.run_case(
                v.sharp_line_lossy(), "real", method=method, kind="sharp", case="SHARP, small loss",
                edges=(20.0, 35.0), z_plateau=500.0)
        cls.reports = cases
        reports = list(cases.values())
        print("\n" + v.markdown_table(reports) + "\n")
        out_dir = os.environ.get("TDR_VALIDATION_OUT")
        if out_dir:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            for rep in reports:
                slug = rep.case.replace(", ", "_").replace(" ", "_").lower()
                tdr.plot_profile(rep.result, Path(out_dir) / f"{slug}_{rep.band}_{rep.method}.png",
                                 truth=v._truth_for(rep.case), threshold=v.MISMATCH_THRESHOLD)

    def _numbers(self, case, band, method):
        return self.reports[(case, band, method)].numbers

    # --- passing criteria --------------------------------------------------------
    def test_sharp_real_band_edges_and_plateau(self):
        n = self._numbers("SHARP", "real", "peel")
        for err in n["edge_err_res"]:
            self.assertLessEqual(err, 1.0)
        self.assertLessEqual(n["plateau_err"], 0.10)

    def test_sharp_generous_band_edges_and_plateau(self):
        n = self._numbers("SHARP", "generous", "peel")
        for err in n["edge_err_res"]:
            self.assertLessEqual(err, 0.5)
        self.assertLessEqual(n["plateau_err"], 0.05)

    def test_gradual_generous_band(self):
        n = self._numbers("GRADUAL", "generous", "peel")
        self.assertLessEqual(n["rms_pct"], 0.05)
        self.assertEqual(n["false_edges"], 0)

    def test_gradual_real_band(self):
        self.assertLessEqual(self._numbers("GRADUAL", "real", "peel")["rms_pct"], 0.08)

    def test_gradual_fit_generous_band(self):
        n = self._numbers("GRADUAL", "generous", "fit")
        self.assertLessEqual(n["rms_pct"], 0.05)
        self.assertEqual(n["false_edges"], 0)

    def test_gradual_fit_real_band(self):
        self.assertLessEqual(self._numbers("GRADUAL", "real", "fit")["rms_pct"], 0.08)

    # --- criteria the current method does not meet (measured, not loosened) ------
    @unittest.expectedFailure
    def test_sharp_generous_outer_parts_within_3_percent(self):
        """Peel: outer parts 146 % off 50 ohm within one resolution of the edges' tails."""
        self.assertLessEqual(self._numbers("SHARP", "generous", "peel")["outer_max_err"], 0.03)

    @unittest.expectedFailure
    def test_sharp_fit_generous_edges_within_half_resolution(self):
        """Fit: second edge 1.05 resolutions off (plateau-ripple detections)."""
        for err in self._numbers("SHARP", "generous", "fit")["edge_err_res"]:
            self.assertLessEqual(err, 0.5)

    @unittest.expectedFailure
    def test_sharp_fit_real_band_edges_within_one_resolution(self):
        """Fit: edges 2.26 and 1.82 resolutions off at the real band."""
        for err in self._numbers("SHARP", "real", "fit")["edge_err_res"]:
            self.assertLessEqual(err, 1.0)

    @unittest.expectedFailure
    def test_corners_generous_band_all_found(self):
        """Peel: 0 of 5 found at 0.05-100 GHz (|r| ~ 0.01 for these narrow dips)."""
        self.assertTrue(all(self._numbers("CORNERS", "generous", "peel")["matched"]))

    @unittest.expectedFailure
    def test_corners_fit_generous_band_all_found(self):
        """Fit: 3 of 5 found; the two 42 ohm bends are missed."""
        self.assertTrue(all(self._numbers("CORNERS", "generous", "fit")["matched"]))

    @unittest.expectedFailure
    def test_corners_real_band_all_found(self):
        """Peel: none of the 5 found at 0.1-20 GHz (the narrow features are below the resolution)."""
        self.assertTrue(all(self._numbers("CORNERS", "real", "peel")["matched"]))

    @unittest.expectedFailure
    def test_two_ghz_start_plateau_within_ten_percent(self):
        """Peel: the DC extrapolation from 2 GHz (fitted DC +0.78, true 0) wrecks the plateau."""
        self.assertLessEqual(self._numbers("SHARP, starts 2 GHz", "from_2ghz", "peel")["plateau_err"], 0.10)

    @unittest.expectedFailure
    def test_two_ghz_start_fit_plateau_within_ten_percent(self):
        """Fit: plateau 11 % off (no DC extrapolation, but the band below 2 GHz is unconstrained)."""
        self.assertLessEqual(self._numbers("SHARP, starts 2 GHz", "from_2ghz", "fit")["plateau_err"], 0.10)

    @unittest.expectedFailure
    def test_small_loss_plateau_within_ten_percent_peel(self):
        """Peel: plateau 22 % off with 0.1 dB/cm (the lossless model cannot follow the loss)."""
        self.assertLessEqual(self._numbers("SHARP, small loss", "real", "peel")["plateau_err"], 0.10)

    @unittest.expectedFailure
    def test_small_loss_plateau_within_ten_percent_fit(self):
        """Fit: plateau 23 %; the fit does not reach its tolerance on lossy data."""
        self.assertLessEqual(self._numbers("SHARP, small loss", "real", "fit")["plateau_err"], 0.10)


if __name__ == "__main__":
    unittest.main()
