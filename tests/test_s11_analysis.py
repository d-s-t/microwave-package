"""Tests for the S11 band means and the time-domain reflection read.

The reflection read is checked on a synthetic S11 whose reflections are
known: a coefficient of 0.2 at 0.5 ns and 0.1 at 1.2 ns must come back at
those times with those heights. The band edges follow the notes: [2,10]
and [10,17] include both ends, [17,20) and [20,26) do not include the top.

Run: python -m unittest discover -s tests
"""
import math
import unittest

import numpy as np

from microwave_package.hfss.driven import s11_analysis as s


def grid():
    return np.arange(2.0, 26.0 + 1e-9, 0.25)


class TestImpulseResponse(unittest.TestCase):
    def test_known_reflections_come_back_at_their_delays_and_heights(self):
        f = grid()
        s11 = 0.2 * np.exp(-2j * np.pi * f * 0.5) + 0.1 * np.exp(-2j * np.pi * f * 1.2)
        t, response = s.impulse_response(s11, f)
        peaks = s.top_peaks(t, response, eps_eff=1.0, count=2, max_ns=2.0)
        times = [round(p[0], 3) for p in peaks]
        heights = [round(p[2], 3) for p in peaks]
        self.assertEqual(times, [0.5, 1.2])
        self.assertAlmostEqual(heights[0], 0.2, delta=0.005)
        self.assertAlmostEqual(heights[1], 0.1, delta=0.005)

    def test_distance_is_one_way_along_the_line(self):
        # 1 ns round trip on a line of eps_eff 1 is c / 2 = 149.9 mm one way.
        distance = s.to_distance_mm(np.array([1.0]), eps_eff=1.0)[0]
        self.assertAlmostEqual(distance, s.C_MM_PER_NS / 2.0, places=6)


class TestBands(unittest.TestCase):
    def test_closed_and_half_open_edges(self):
        f = np.array([2.0, 10.0, 17.0, 20.0, 26.0])
        low = s.in_band(f, s.BANDS[0])
        mid = s.in_band(f, s.BANDS[1])
        high = s.in_band(f, s.BANDS[2])
        top = s.in_band(f, s.BANDS[3])
        self.assertEqual(low.tolist(), [True, True, False, False, False])
        self.assertEqual(mid.tolist(), [False, True, True, False, False])
        self.assertEqual(high.tolist(), [False, False, True, False, False])
        self.assertEqual(top.tolist(), [False, False, False, True, False])


class TestBandMeans(unittest.TestCase):
    def test_flat_reflection_gives_its_dB_value_in_every_band(self):
        f = grid()
        sweep = s.Sweep(f, np.full(f.shape, 0.5 + 0j), np.zeros(f.shape, dtype=complex))
        means = [round(m, 6) for m, _ in s.band_means_db(sweep)]
        expected = round(20 * math.log10(0.5), 6)
        self.assertEqual(means, [expected] * 4)


if __name__ == "__main__":
    unittest.main()
