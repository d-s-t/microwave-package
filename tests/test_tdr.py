"""Tests for the TDR calculator (microwave_package/workflow/tdr.py) and its
validation on exact dummy lines (workflow/tdr_validation.py).

Run: python -m unittest discover -s tests

The validation criteria are the ones written in tdr_validation.py, asserted here
exactly. A criterion the current method does not meet is marked expectedFailure
with the measured value in its docstring: the criterion is not loosened, and the
test starts failing ("unexpected success") as soon as the method meets it. The
2 GHz start is a characterisation (no pass/fail): its numbers and its warning are
checked, not its plateau.
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
        """a[n] = ifft of the Hermitian spectrum, so sum(a) = S(DC) and the DFT returns the
        spectrum on the grid bins (no extra scale)."""
        n = 64
        m_factor = 8
        spectrum = np.zeros(n + 1, dtype=complex)
        spectrum[0] = 0.25
        spectrum[1:] = 0.1 * np.exp(-0.3j * np.arange(1, n + 1))
        a, sample_ns = tdr.impulse_sequence(spectrum, df_ghz=0.05, pad=m_factor)
        self.assertAlmostEqual(float(np.sum(a)), 0.25, places=10)  # sum(a) = S(DC)
        m = m_factor * (2 * n + 2)
        self.assertAlmostEqual(sample_ns, 1.0 / (m * 0.05), places=12)
        np.testing.assert_allclose(np.fft.fft(a)[: n + 1], spectrum, atol=1e-12)


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


class TestLossyFitModel(unittest.TestCase):
    def test_lossy_model_agrees_with_the_independent_abcd_cascade(self):
        """Loss a_c sqrt(f) + a_d f and the cells, S11, S21 and S22, against the ABCD
        code of tdr_validation (which is written independently)."""
        f = np.linspace(0.5, 40.0, 30)
        line = v.DummyLine("chk", tuple(v.Section(z, 0.5) for z in [50.0, 500.0, 50.0]),
                           eps_eff=2.0, alpha_sqrt_np_per_mm=2e-5, alpha_lin_np_per_mm=1.5e-5)
        model = tdr.LineModel(f, 0.5, 3, 2.0, Z0, loss=True, delay=True)
        p = np.zeros(model.n_params)
        p[:3] = np.log(np.array([50.0, 500.0, 50.0]) / Z0)
        p[model.n] = math.log(2e-5)
        p[model.n + 1] = math.log(1.5e-5)
        got = model.s_params(p)
        ref = line.s_parameters(f)
        np.testing.assert_allclose(got["11"], ref[:, 0, 0], atol=1e-12)
        np.testing.assert_allclose(got["21"], ref[:, 1, 0], atol=1e-12)
        np.testing.assert_allclose(got["22"], ref[:, 1, 1], atol=1e-12)

    def test_analytic_jacobian_matches_central_differences(self):
        """All parameters (profile, a_c, a_d, delay scale), all three blocks."""
        rng = np.random.default_rng(5)
        f = np.linspace(0.5, 30.0, 60)
        model = tdr.LineModel(f, 0.4, 10, 2.0, Z0, loss=True, delay=True, blocks=("11", "21", "22"))
        p = rng.normal(0.0, 0.3, size=model.n_params)
        p[model.n] = math.log(1e-5)
        p[model.n + 1] = math.log(2e-5)
        data = model.s_params(p)
        data = {k: val * (1.0 + 0.02 * rng.normal(size=f.size)) for k, val in data.items()}
        weights = {"11": 1.0, "21": 1.0, "22": 1.0}
        _, jac = model.residual_and_jacobian(p, data, weights)
        h = 1e-6
        for col in range(model.n_params):
            up, dn = p.copy(), p.copy()
            up[col] += h
            dn[col] -= h
            rp, _ = model.residual_and_jacobian(up, data, weights, jacobian=False)
            rm, _ = model.residual_and_jacobian(dn, data, weights, jacobian=False)
            np.testing.assert_allclose(jac[:, col], (rp - rm) / (2 * h), atol=1e-6)


class TestRegularisedFit(unittest.TestCase):
    def test_fit_recovers_the_realistic_ro3003_loss_on_the_real_band(self):
        """The S21 block fixes the loss: RO3003 tan delta 0.001 (1.48e-5 Np/mm per GHz) is
        recovered to within 10 %, and the lossless line gets (almost) no loss."""
        line = v.sharp_line_ro3003()
        freq = v.BANDS["real"][1]
        result = tdr.analyse_fit_multi(freq, line.s_parameters(freq), line_length_mm=line.length_mm,
                                       eps_eff=2.0)
        self.assertTrue(result.stable)
        truth = v.RO3003_ALPHA_LIN_NP_PER_MM_PER_GHZ
        self.assertLessEqual(abs(result.fit["a_d_np_per_mm_per_ghz"] / truth - 1.0), 0.10)
        lossless = v.sharp_line()
        result0 = tdr.analyse_fit_multi(freq, lossless.s_parameters(freq), line_length_mm=lossless.length_mm,
                                        eps_eff=2.0)
        self.assertLess(result0.fit["a_d_np_per_mm_per_ghz"], 1e-6)

    def test_fit_reaches_its_tolerance_on_the_sharp_line(self):
        line = v.sharp_line()
        freq = v.BANDS["real"][1]
        result = tdr.analyse_fit_multi(freq, line.s_parameters(freq), line_length_mm=line.length_mm,
                                       eps_eff=2.0)
        self.assertTrue(result.fit["converged"])
        self.assertLessEqual(result.fit["rms"], tdr.FIT_TOL)

    def test_port_two_sees_the_same_trace_from_the_other_end(self):
        """A mismatch at x from port 1 appears at L - x from port 2 (SHARP: 500 ohm from
        20 to 35 mm of a 50 mm line, so from port 2 at 15 and 30 mm)."""
        line = v.sharp_line()
        freq = v.BANDS["real"][1]
        result = tdr.analyse_fit_multi(freq, line.s_parameters(freq), line_length_mm=line.length_mm,
                                       eps_eff=2.0, port=2)
        steps = sorted(m.position_mm for m in tdr.find_mismatches(result)[0] if m.kind == "step")
        self.assertEqual(len(steps), 2)
        self.assertLessEqual(abs(steps[0] - 15.0), result.resolution_mm)
        self.assertLessEqual(abs(steps[1] - 30.0), result.resolution_mm)


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


def _synthetic(z_cells, cell_mm, resolution_mm, f_max_ghz=20.0):
    """A TDRResult whose profile is z_cells (ohm) on cells of cell_mm."""
    n = len(z_cells)
    x = np.arange(n) * cell_mm
    t = np.zeros(n)
    return tdr.TDRResult(
        name="synthetic", port=1, method="regularised fit", z0=Z0, eps_eff=2.0, beta=3.0, pad=8,
        df_ghz=0.05, f_min_ghz=0.1, f_max_ghz=f_max_ghz, n_points=10, dc_value=0.0, dc_source="given",
        n_extrapolated=0, delay_removed_ns=0.0, sample_ns=0.01, rise_time_ns=0.03,
        resolution_mm=resolution_mm, naive_t_ns=t, naive_x_mm=x, rho_step=np.zeros(n),
        z_naive=np.full(n, np.nan), layer_mm=cell_mm, profile_t_ns=t, profile_x_mm=x,
        r=np.zeros(n), z_peel=np.asarray(z_cells, dtype=float), stable=True)


class TestFeatures(unittest.TestCase):
    """Steps and excursions of a profile (the detector), on synthetic profiles."""

    def test_step_pair_is_found_at_the_edges(self):
        z = np.where(np.arange(40) < 10, 50.0, np.where(np.arange(40) < 30, 100.0, 50.0))
        features, first = tdr.find_mismatches(_synthetic(z, 0.1, 1.0))
        self.assertEqual([f.kind for f in features], ["step", "step"])
        self.assertAlmostEqual(features[0].position_mm, 1.0, delta=0.05)
        self.assertAlmostEqual(features[1].position_mm, 3.0, delta=0.05)
        self.assertAlmostEqual(features[0].z_before_ohm, 50.0, places=6)
        self.assertAlmostEqual(features[0].z_after_ohm, 100.0, places=6)
        self.assertAlmostEqual(features[0].reflection, math.tanh(math.log(2.0) / 2.0), places=6)
        self.assertAlmostEqual(features[1].z_after_ohm, 50.0, places=6)
        self.assertAlmostEqual(first.position_mm, 1.0, delta=0.05)

    def test_narrow_dip_is_an_excursion_reported_by_its_area(self):
        """A 0.3 mm, 35 ohm dip in 50 ohm: area -4.5 ohm mm, peak -15 ohm. The threshold
        is the Born area at f_max (2.5 ohm mm at 20 GHz), so it is reported."""
        z = np.full(60, 50.0)
        z[20:23] = 35.0
        features, _ = tdr.find_mismatches(_synthetic(z, 0.1, 1.0))
        self.assertEqual([f.kind for f in features], ["excursion"])
        dip = features[0]
        self.assertAlmostEqual(dip.area_ohm_mm, -4.5, places=6)
        self.assertAlmostEqual(dip.peak_ohm, -15.0, places=6)
        self.assertAlmostEqual(dip.position_mm, 2.15, delta=0.01)
        self.assertAlmostEqual(dip.width_mm, 0.3, places=6)

    def test_excursion_below_the_area_threshold_is_not_reported(self):
        """A 0.2 mm, 45 ohm dip has area -1.0 ohm mm, under the 2.5 ohm mm Born area at 20 GHz."""
        z = np.full(60, 50.0)
        z[20:22] = 45.0
        features, _ = tdr.find_mismatches(_synthetic(z, 0.1, 1.0))
        self.assertEqual(features, [])

    def test_two_features_closer_than_the_resolution_are_one_with_a_note(self):
        z = np.full(60, 50.0)
        z[20:23] = 35.0
        z[25:28] = 35.0  # 0.5 mm further on: closer than the 1 mm resolution
        features, _ = tdr.find_mismatches(_synthetic(z, 0.1, 1.0))
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0].kind, "excursion")
        self.assertIn("reported as one", features[0].note)
        self.assertAlmostEqual(features[0].area_ohm_mm, -9.0, places=6)

    def test_smooth_taper_has_no_features(self):
        """A 50 -> 150 ohm ramp over 20 mm changes ln Z by 0.055 per resolution (1 mm), below
        the 0.06 that a 0.03 reflection needs: no step and no excursion."""
        z = 50.0 * np.exp(np.linspace(0.0, math.log(3.0), 200))
        features, _ = tdr.find_mismatches(_synthetic(z, 0.1, 1.0))
        self.assertEqual(features, [])


class TestCalculatorBehaviour(unittest.TestCase):
    def test_dc_can_be_given(self):
        line = v.sharp_line()
        freq = v.BANDS["real"][1]
        s11 = line.s_parameters(freq)[:, 0, 0]
        result = tdr.analyse(freq, s11, dc=0.0, eps_eff=2.0, max_mm=line.length_mm + 6.0)
        self.assertEqual(result.dc_source, "given")
        self.assertEqual(result.dc_value, 0.0)

    def test_port_two_peel_measures_from_the_other_end(self):
        """SHARP read from port 2 with the layer peel: its 500 ohm section starts at 15 mm."""
        line = v.sharp_line()
        freq = v.BANDS["real"][1]
        sp = tdr.SParameters(freq, line.s_parameters(freq), Z0, "sharp")
        result = tdr.analyse_sparameters(sp, port=2, eps_eff=2.0, max_mm=line.length_mm + 6.0)
        steps = sorted(m.position_mm for m in tdr.find_mismatches(result)[0] if m.kind == "step")
        self.assertLessEqual(abs(steps[0] - 15.0), result.resolution_mm)
        self.assertLessEqual(abs(steps[1] - 30.0), result.resolution_mm)

    def test_two_ghz_start_warns_and_says_the_profile_is_indicative(self):
        """The 2 GHz start is characterised: the warning is kept (not a pass/fail)."""
        line = v.sharp_line()
        freq = v.BANDS["from_2ghz"][1]
        result = tdr.analyse_fit_multi(freq, line.s_parameters(freq), line_length_mm=line.length_mm,
                                       eps_eff=2.0)
        self.assertTrue(any("sweep starts at 2 GHz" in note for note in result.notes))

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

    def test_cli_whole_two_port_fit_with_a_line_length(self):
        line = v.sharp_line()
        freq = v.BANDS["real"][1]
        s = line.s_parameters(freq)
        with tempfile.TemporaryDirectory() as tmp:
            s2p = Path(tmp) / "sharp.s2p"
            _write_touchstone(s2p, freq, s[:, 0, 0], s[:, 1, 0], s[:, 0, 1], s[:, 1, 1])
            out = Path(tmp) / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                code = tdr.main([str(s2p), "--out", str(out), "--method", "fit", "--line-length-mm", "50"])
            self.assertEqual(code, 0)
            summary = (out / "sharp_fit_summary.txt").read_text(encoding="utf-8")
            self.assertIn("fit 11+21+22", summary)


class TestValidationCriteria(unittest.TestCase):
    """The criteria of tdr_validation at the generous band (0.05-100 GHz) and the real band
    (0.1-20 GHz), with both methods, plus the RO3003 and small-loss cases. The 2 GHz start is
    a characterisation. The measured table is printed once; with TDR_VALIDATION_OUT set the
    overlay PNGs are saved there."""

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
                edges=(20.0, 35.0), z_plateau=500.0, characterise=True)
            cases[("SHARP, small loss", "real", method)] = v.run_case(
                v.sharp_line_lossy(), "real", method=method, kind="sharp", case="SHARP, small loss",
                edges=(20.0, 35.0), z_plateau=500.0)
            cases[("SHARP, RO3003 loss", "real", method)] = v.run_case(
                v.sharp_line_ro3003(), "real", method=method, kind="sharp", case="SHARP, RO3003 loss",
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

    # --- criteria the methods meet ----------------------------------------------------
    def test_peel_sharp_real_band_edges_and_plateau(self):
        n = self._numbers("SHARP", "real", "peel")
        for err in n["edge_err_res"]:
            self.assertLessEqual(err, 1.0)
        self.assertLessEqual(n["plateau_err"], 0.10)

    def test_peel_sharp_generous_band_edges_and_plateau(self):
        n = self._numbers("SHARP", "generous", "peel")
        for err in n["edge_err_res"]:
            self.assertLessEqual(err, 0.5)
        self.assertLessEqual(n["plateau_err"], 0.05)

    def test_peel_gradual_generous_band(self):
        n = self._numbers("GRADUAL", "generous", "peel")
        self.assertLessEqual(n["rms_pct"], 0.05)
        self.assertEqual(n["false_edges"], 0)

    def test_peel_gradual_real_band(self):
        self.assertLessEqual(self._numbers("GRADUAL", "real", "peel")["rms_pct"], 0.08)

    def test_fit_sharp_generous_band_all_three_limits(self):
        n = self._numbers("SHARP", "generous", "fit")
        for err in n["edge_err_res"]:
            self.assertLessEqual(err, 0.5)
        self.assertLessEqual(n["plateau_err"], 0.05)
        self.assertLessEqual(n["outer_max_err"], 0.03)

    def test_fit_gradual_generous_band(self):
        n = self._numbers("GRADUAL", "generous", "fit")
        self.assertLessEqual(n["rms_pct"], 0.05)
        self.assertEqual(n["false_edges"], 0)

    def test_fit_gradual_real_band(self):
        self.assertLessEqual(self._numbers("GRADUAL", "real", "fit")["rms_pct"], 0.08)

    def test_fit_corners_generous_band_all_found_and_no_extras(self):
        n = self._numbers("CORNERS", "generous", "fit")
        self.assertTrue(all(n["matched"]))
        self.assertEqual(n["extra"], 0)

    def test_fit_sharp_real_band_edges_and_plateau(self):
        n = self._numbers("SHARP", "real", "fit")
        for err in n["edge_err_res"]:
            self.assertLessEqual(err, 1.0)
        self.assertLessEqual(n["plateau_err"], 0.10)

    def test_ro3003_loss_real_band_both_methods(self):
        for method in ("peel", "fit"):
            n = self._numbers("SHARP, RO3003 loss", "real", method)
            for err in n["edge_err_res"]:
                self.assertLessEqual(err, 1.0)
            self.assertLessEqual(n["plateau_err"], 0.10)

    def test_small_loss_real_band_fit(self):
        """The constant 1.15 Np/m line is outside the model family; its edges and plateau pass."""
        n = self._numbers("SHARP, small loss", "real", "fit")
        for err in n["edge_err_res"]:
            self.assertLessEqual(err, 1.0)
        self.assertLessEqual(n["plateau_err"], 0.10)

    def test_two_ghz_start_is_characterised_not_judged(self):
        for method in ("peel", "fit"):
            rep = self.reports[("SHARP, starts 2 GHz", "from_2ghz", method)]
            self.assertIsNone(rep.passed)
            self.assertTrue(np.isfinite(rep.numbers["plateau_err"]))

    # --- criteria the current methods do not meet (measured, not loosened) -------------
    @unittest.expectedFailure
    def test_peel_sharp_generous_outer_parts_within_3_percent(self):
        """Peel: outer parts 146 % off 50 ohm (the tail after the second edge)."""
        self.assertLessEqual(self._numbers("SHARP", "generous", "peel")["outer_max_err"], 0.03)

    @unittest.expectedFailure
    def test_peel_corners_generous_band_all_found(self):
        """Peel: 3 of 5 found at 0.05-100 GHz; the two 42 ohm bends (area 1.6 ohm mm) are missed."""
        self.assertTrue(all(self._numbers("CORNERS", "generous", "peel")["matched"]))

    @unittest.expectedFailure
    def test_peel_corners_real_band_all_found(self):
        """Peel: none of the 5 found at 0.1-20 GHz (the features are below the resolution)."""
        self.assertTrue(all(self._numbers("CORNERS", "real", "peel")["matched"]))

    @unittest.expectedFailure
    def test_peel_small_loss_plateau_within_ten_percent(self):
        """Peel: plateau 22.5 % off with the constant 1.15 Np/m loss (the lossless peel)."""
        self.assertLessEqual(self._numbers("SHARP, small loss", "real", "peel")["plateau_err"], 0.10)

    @unittest.expectedFailure
    def test_fit_corners_real_band_all_found(self):
        """Fit: none of the 5 found at 0.1-20 GHz. TV regularisation smooths the 35 ohm corners
        to about 2 ohm over one 1.2 mm cell: an area of 2.4 ohm mm against 4.5 true and the
        2.5 ohm mm Born threshold at 20 GHz."""
        self.assertTrue(all(self._numbers("CORNERS", "real", "fit")["matched"]))


if __name__ == "__main__":
    unittest.main()
