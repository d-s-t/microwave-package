# Pedestal depth sweep, 2026-10-21 (task H2)

Pocket: circle, pedestal_diameter 6.25 mm (pedestal_shape 1), TMM 10i chip board, cpw and chip trace 0.20 mm, connector reference plane. Die gap is solved, not typed: 0.11903 mm at every depth.

## Conclusion

- **Zo (line model, 1-20 GHz):** no depth dependence over 0 to 0.5 mm. The means are 49.00, 49.19, 49.25, 50.77 and 49.86 ohm, all within the +-2.5 ohm target band. The largest step, 0.2 to 0.3 mm (+1.5 ohm), is above the 1-ohm rule, but it is not monotone (0.3 to 0.5 mm goes back by 0.9 ohm), so it is treated as noise. The 0.75 and 1.0 mm line results are invalid (solver error, reproduced), so Zo above 0.5 mm is not known.
- **Driven S21, 2-10 GHz:** saturated from 0 mm. Band-mean S21 stays between -2.23 and -2.40 dB, and every neighbour step is below 0.3 dB.
- **Driven S21, 10-17 GHz:** the mean S21 changes are +0.25, +0.03, -0.42 and -0.08 dB across 0, 0.1, 0.25, 0.5 and 1.0 mm. The 0.5 to 1.0 mm step is below 0.3 dB, and the 0.25 to 0.5 mm step (0.42 dB) is inside the quick-setting error (see caveats). The clean share rises 32, 39, 46, 46, 54 %.
- **Driven S21, 17-20 GHz:** 12 points only; mean S21 -17.9 to -19.8 dB, clean share 0 to 8 %. No conclusion; the quick-setting error here is about 2 dB.
- **Saturation depth:** by your rules (Zo change below 1 ohm, band-mean S21 change below 0.3 dB, clean share change below 2 %), the 2-10 GHz band and Zo are saturated from 0 mm. The 10-17 GHz band reaches the mean-S21 rule at **0.5 mm**. The clean-share rule is not met by 1.0 mm (46 % at 0.5 mm, 54 % at 1.0 mm). Neither rule can be checked below one point, which is 3.6 % of the 10-17 GHz band.
- **What it means for the pocket:** a 0.25 mm pocket gives the same 10-17 GHz clean share as 0.5 mm (46 %) and a mean S21 within 0.42 dB, which is inside the quick-setting error. It is therefore a candidate for an easier-to-mill pocket, but only a converged run at 0.25 mm can confirm it. Going deeper than 0.5 mm gives at most a small gain in 10-17 GHz. The die gap does not depend on depth, and the line-model Zo stays within 1 ohm of the 0.5 mm value from 0 to 0.3 mm, so the 0.5 mm calibration of the die gap still holds for a shallower pocket. Zo at 0.25 mm itself has not been run; the 0.2 and 0.3 mm line results bracket it at 49.25 and 50.77 ohm.

## Caveats (read before quoting any number)

- **Quick settings have a measurable error.** At the same depth (0.5 mm, same circle pocket), the converged run 150512 differs from the quick run as follows: 2-10 GHz mean S21 by 0.06 dB and mean S11 by 0.55 dB; 10-17 GHz mean S21 by 0.75 dB and mean S11 by 0.34 dB; 17-20 GHz mean S21 by 2.2 dB. The clean share differs by one point (46 % vs 50 % in 10-17 GHz). Differences smaller than these are not real with the quick settings.
- **Neighbour spread is the noise floor.** Band-mean S21 steps between neighbouring depths are 0.10, 0.07, 0.09 and 0.04 dB in 2-10 GHz, and 0.25, 0.03, 0.42 and 0.08 dB in 10-17 GHz. Steps smaller than these are not real.
- **One run per depth, no repeats.** Run-to-run scatter was not measured directly.
- **The 2026-10-18 comparison runs are under-converged.** Their settings were max_passes 4, max_delta_s 0.05, min_passes 1 and min_converged 1. The 2026-10-20 B and C runs used max_passes 99, max_delta_s 0.02, min_passes 3 and min_converged 2, which accounts for their 4.5 h and 9.75 h solves (see below). The 2026-10-18 pocket was not pinned in its config, so its pocket shape was the code default at that time.
- **0.75 mm was not run in the driven sweep.** The line model failed at 0.75 and 1.0 mm (below). The driven model at 1.0 mm solved normally, so that failure is specific to the line model's geometry, not to the depth itself.
- **Die gap:** 0.11903 mm at all five depths. The gap solver's inputs (trace width, die thickness, silicon permittivity, target Z0, metal thickness) do not include depth. The value was checked by evaluating `solve_chip_gap_for_z0` from each combination's dimensions file. The gap is not stored in copper.txt.

## Driven S21 table (quick settings, 2-20 GHz, 73 points, run_20261021_141838)

Bands: 2-10 GHz (33 points), 10-17 GHz (28), 17-20 GHz (12). Clean share = share of points with S21 above -3 dB. All five solves finished with "solved correctly" and no "Error in Solving Setup_1"; each depth has 72 or 73 distinct S21 values (0.001 dB resolution).

| Depth (mm) | Band | Mean S11 (dB) | Mean S21 (dB) | Clean (%) | Solve time |
|---|---|---|---|---|---|
| 0 | 2-10 | -7.24 | -2.23 | 73 | 20 min 11 s |
| 0 | 10-17 | -5.03 | -5.76 | 32 | |
| 0 | 17-20 | -1.05 | -19.05 | 0 | |
| 0.1 | 2-10 | -7.62 | -2.33 | 73 | 16 min 47 s |
| 0.1 | 10-17 | -4.55 | -5.51 | 39 | |
| 0.1 | 17-20 | -2.12 | -17.88 | 0 | |
| 0.25 | 2-10 | -7.92 | -2.40 | 70 | 18 min 11 s |
| 0.25 | 10-17 | -5.54 | -5.48 | 46 | |
| 0.25 | 17-20 | -1.28 | -18.94 | 0 | |
| 0.5 | 2-10 | -7.52 | -2.31 | 73 | 18 min 1 s |
| 0.5 | 10-17 | -4.38 | -5.90 | 46 | |
| 0.5 | 17-20 | -1.77 | -18.49 | 8 | |
| 1.0 (probe) | 2-10 | -7.58 | -2.35 | 73 | 18 min 43 s |
| 1.0 (probe) | 10-17 | -4.52 | -5.98 | 54 | |
| 1.0 (probe) | 17-20 | -3.12 | -19.80 | 8 | |
| 0.5 converged reference (run_20261020_150512, not in the sweep) | 2-10 | -6.97 | -2.25 | 73 | 9 h 45 min |
| 0.5 converged reference | 10-17 | -4.72 | -6.65 | 50 | |
| 0.5 converged reference | 17-20 | -2.42 | -16.30 | 8 | |

## Zo table (line model, 1-20 GHz, 53 points each, run_20261020_154847 with 0.119 mm gap)

| Depth (mm) | Mean (ohm) | Min (ohm) | Max (ohm) | Status |
|---|---|---|---|---|
| 0 | 49.00 | 46.32 | 58.47 | valid |
| 0.1 | 49.19 | 48.71 | 53.77 | valid |
| 0.2 | 49.25 | 48.76 | 49.74 | valid |
| 0.3 | 50.77 | 50.24 | 51.72 | valid |
| 0.5 | 49.86 | 49.29 | 50.29 | valid |
| 0.75 | 49.03 (flat) | 49.03 | 49.03 | invalid: solver error, reproduced in run_20261021_115901 |
| 1.0 | 49.00 (flat) | 49.00 | 49.00 | invalid: solver error, reproduced in run_20261021_115901 |

## Part 1b (2026-10-21): line model at 0.75 and 1.0 mm

Re-solve run `run_20261021_115901` (line model, same settings as H) reproduced the failure at both depths: "Error in Solving Setup_1" after 16 min 31 s (0.75) and 8 min 22 s (1.0), and the worker still marked the tasks successful with a flat Zo. The mesh-quality file lists vacuum slivers of 0.08 to 0.11 um at x 1.13 to 1.6 mm, y 1.131 mm, z -1.5 mm. Depth 0.5 has 12 such warnings too and solves, so the slivers alone do not explain the failure. The adaptive profiles show no mesh-size limit. Judgement: a deterministic model (geometry) problem at 0.75 and 1.0 mm in the line model. Re-solving the same model will fail again.

## Why B and C took 4.5 h and 9.75 h (read-only check)

| Run | max_passes | max_delta_s | min_passes | min_converged | max_delta_f | Adaptive passes | Largest tet count (profile) | Solve time |
|---|---|---|---|---|---|---|---|---|
| 2026-10-18 `run_20261018_165006` | 4 | 0.05 | 1 | 1 | 5.0 | 4 | about 0.88 M | 7 to 18 min |
| B `run_20261020_103237` | 99 | 0.02 | 3 | 2 | 3.0 | 16 | about 1.79 M | 4 h 27 min |
| C `run_20261020_150512` | 99 | 0.02 | 3 | 2 | 3.0 | 14 | about 2.27 M | 9 h 45 min |

The 2026-10-18 profile stops at its pass cap with the last Delta S at 0.84, above its 0.05 target. The C profile's Delta S falls to 0.014 by pass 14, below the 0.02 target. Delta S units are read from the profile and assumed to match max_delta_s.

## Settings check

- The quick sweep's analysis settings match `run_20261018_165006` (2026-10-18, run config in `chip-gap-fix-77482c`) on every key. The worktree path `comparison-b2-86700f` in the brief does not exist; the run is under `chip-gap-fix-77482c`.
- Pinned: no_chip 0, cpw_trace_width 0.2, chip_trace_width 0.2, pedestal_shape 1, pedestal_diameter 6.25. Depths swept: 0, 0.1, 0.25, 0.5, 1.0 (one sweep run, `run_20261021_141838`).

## Figures (`D:\microwave-package\.handoff\decision-images\`)

- `zo_vs_depth_2026-10-21.svg`: Zo vs depth, 0 to 0.5 mm with min-max bars, and the invalid 0.75 and 1.0 mm marked.
- `s21_overlay_2026-10-21.svg`: S21 2-20 GHz for depths 0, 0.1, 0.25, 0.5 and 1.0 mm, with the converged 0.5 mm reference dashed. Full range and zoom panels.
- `bands_vs_depth_2026-10-21.svg`: mean S11, mean S21 and clean share per band vs depth, with the converged reference as an open marker at 0.5 mm.

## Open items

- A converged run at 0.25 mm (and, if wanted, a converged 0.75 mm) would settle whether 0.25 mm matches 0.5 mm. Any run at converged settings takes 4 to 10 h on the worker.
- The line-model geometry at 0.75 and 1.0 mm needs a model fix before those Zo values exist.
