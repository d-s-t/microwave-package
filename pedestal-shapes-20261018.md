# Pedestal shape comparison, 2026-10-18 (task D2)

## Summary

- **The pedestal shape barely changes the result.** Across the three shapes the 2–10 GHz band is the same (mean S11 -7.2 to -7.4 dB, clean 76 % in all three). The 10–17 GHz band is where the differences are, and they are small: the square (b) is 0.5 dB better than the octagon (a) on S11 and 7 points lower on clean share; the circle (c) is 0.7 dB worse than the square on S11 and 7 points higher on clean share.
- **Against the noise:** the stated noise is about ±0.5 dB on S11 and 1–2 % of clean share. The 10–17 GHz S11 differences (0.5 and 0.7 dB) are at or just above that line; the clean-share differences (7 points, about 2 of the 29 points in that band) are just above it too. One run per variant, so run-to-run noise was not measured directly. Treat these as **marginal, not a demonstrated effect**.
- **17–20 GHz** has 12 points, so one point is 8 %. The 17 % → 8 % step for (a) → (b) and the 8 % for (c) are one-point changes.
- **Confound:** (a) differs from (b) by the pocket shape **and** by the cavity and PCB rounding (the rounded box cavity (#79) and rounded PCB (#84) are only on the (b)/(c) stack). So the (a) → (b) difference is not a pure pocket effect. **(b) vs (c) is the clean comparison**: same stack, only the pocket shape changes.
- **(a) reproduces the earlier reference:** (a) matches the reference chip run `run_20261018_165006` (2026-10-18, same settings, new lid, TMM 10i) to within one point per band (-7.3 / -5.7 / -1.5 dB, clean 76 / 45 / 17 % vs 76 / 48 / 8 %).
- **Not run:** the line-impedance check. The line model (`microwave_package/hfss/line/section.py`) reads only `pedestal_depth` (air below the die), not the pocket shape, so the chip-line Zo would not change with the shape. The eigenmode run was also skipped (optional in the brief).

## Table

Bands: 2–10 GHz (33 points), 10–17 GHz (29 points), 17–20 GHz (12 points). Mean S11 in dB over each band. "Clean" = share of points with S21 above -3 dB. All runs: driven only (`--no-pair`), 2–20 GHz, 0.25 GHz step (73 points), coarse sweep only, connector reference plane, TMM 10i, chip on, cpw/chip trace 0.20 mm, new lid (1.2 x 0.3 mm), default pedestal depth.

| Case | Run | Date | Pocket | Stack | S11 2–10 | S11 10–17 | S11 17–20 | Clean 2–10 | Clean 10–17 | Clean 17–20 |
|---|---|---|---|---|---|---|---|---|---|---|
| (a) octagon, main | run_20261018_185704 | 2026-10-18 | octagon, R=1 fillets (default) | main (square cavity, sharp PCB) | -7.3 | -5.7 | -1.5 | 76 % | 45 % | 17 % |
| (b) square R=1 | run_20261018_190052 | 2026-10-18 | 6.25 mm square, R=1.0 (`pedestal_shape=0`) | #79 + #84 + #82 | -7.2 | -5.2 | -2.0 | 76 % | 38 % | 8 % |
| (c) circle | run_20261018_190522 | 2026-10-18 | 6.25 mm diameter (`pedestal_shape=1`) | #79 + #84 + #82 | -7.4 | -4.5 | -1.6 | 76 % | 45 % | 8 % |
| Reference: TMM 10i chip, new lid | run_20261018_165006 | 2026-10-18 | octagon (main at the time) | main | -7.3 | -5.7 | -1.5 | 76 % | 48 % | 8 % |

Queue ids: (a) `driven_20261018_190015_152795`, (b) `driven_20261018_190506_512462`, (c) `driven_20261018_191041_262403`.

Comparisons:

| Step | Δ S11 2–10 | Δ S11 10–17 | Δ S11 17–20 | Δ clean 2–10 | Δ clean 10–17 | Δ clean 17–20 | Note |
|---|---|---|---|---|---|---|---|
| (a) → (b) | +0.1 | +0.5 | -0.5 | 0 | -7 | -9 | includes cavity + PCB rounding (confounded) |
| (b) → (c) | -0.2 | +0.7 | +0.4 | 0 | +7 | 0 | clean comparison, same stack |
| (a) → (c) | -0.1 | +1.2 | -0.1 | 0 | 0 | -9 | includes cavity + PCB rounding (confounded) |

CSV copy: `pedestal-shapes-20261018.csv` in this folder.
