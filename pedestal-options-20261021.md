# Pedestal options B and C, 2026-10-21 (task G)

Final design on the TMM 10i chip board: cpw_trace_width 0.20, chip_trace_width 0.20, new default lid, default pedestal depth, everything else as in run_20261018_165006. Driven only, coarse sweep, 2-20 GHz, 0.25 GHz step (73 points), connector reference plane. The runs were started on 2026-10-20 and finished on 2026-10-21.

- **B** = `pedestal_shape=0`: rounded square, 6.2 mm side, R 1.0 mm. Run `run_20261020_103237` (2026-10-20).
- **C** = `pedestal_shape=1`: circle, 6.25 mm diameter. Run `run_20261020_150512` (2026-10-20 to 21).
- Reference (from PR #85, branch claude/pedestal-compare-d2-374cec): **(a)** octagon `run_20261018_185704`, **(b)** 6.25 mm square R 1.0 `run_20261018_190052`. These two were built on an older stack, so they are references only.

Figure: `D:\microwave-package\.handoff\decision-images\pedestal_B_vs_C_2026-10-21.svg` (S21 and S11 vs frequency, 2-20 GHz, four overlaid curves, plus the per-band table). It is SVG, not PNG, because matplotlib is not installed; the plotting script is `plot_pedestal_compare.py` at the repo root.

## Per-band table

Bands: 2-10 GHz is f <= 10 (33 points), 10-17 GHz is 10 < f <= 17 (28 points), 17-20 GHz is f > 17 (12 points). This non-overlapping split is what the script uses; the D2 report's 29-point middle band uses a slightly different edge, so compare the clean shares with that in mind. "Clean" = share of points with S21 above -3 dB.

| Case | Run | S11 2-10 | S11 10-17 | S11 17-20 | Clean 2-10 | Clean 10-17 | Clean 17-20 |
|---|---|---|---|---|---|---|---|
| (a) octagon (D2 reference) | run_20261018_185704 | -7.3 | -5.6 | -2.2 | 76 % | 43 % | 25 % |
| (b) square 6.25 R1 (D2 reference) | run_20261018_190052 | -7.2 | -5.1 | -3.2 | 76 % | 36 % | 17 % |
| **B** square 6.2 R1.0 | run_20261020_103237 | -7.0 | -4.7 | -3.2 | 70 % | 43 % | 17 % |
| **C** circle 6.25 | run_20261020_150512 | -7.0 | -4.7 | -2.4 | 73 % | 50 % | 8 % |

Mean S11 in dB over each band; clean share as above.

## Conclusion

**B and C are not different by more than the noise.** Their mean S11 is the same in 2-10 GHz (-7.0 dB) and 10-17 GHz (-4.7 dB), and differs by 0.8 dB in 17-20 GHz, where the band has only 12 points (one point moves its clean share by 8 %). The S21 clean shares differ by one point in 2-10 GHz (3 %), by two points in 10-17 GHz (7 %; one point is 3.6 % of 28 points), and by one point in 17-20 GHz (8 %). Against the stated noise (about ±0.5 dB on S11, 1-2 % on the clean share), the 17-20 GHz S11 gap and the 10-17 GHz clean-share gap sit at or just above the line, but each rests on one or two points and on one run per option. Treat them as marginal, the same conclusion as the D2 comparison.

Against the D2 references, both B and C sit close to the octagon and the 6.25 square; the largest difference is that B and C are a little worse in 2-10 GHz clean share (70 % and 73 % vs 76 %), which is two or three points and within noise.

**Caveats**
- One run per option; run-to-run noise was not measured directly.
- The two references are on an older stack, so differences from them are not pocket-shape effects alone.
- During the AEDT model build, the log shows repeated "Error in intersection. Reverting Operation" messages (14 in run B, similar in run C). They come after the model is saved, during the reopen and object parse for post-processing, and both runs finished with 73 result rows. They have not been tied to a cause; they may be related to the rounded-outline script in PR #86, which has not been verified.
