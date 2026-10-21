# Pedestal depth sweep, 2026-10-21 (task H2)

Status: Part 1b done. Part 2 (driven S21 at new depths) not started; waiting for the manager's "GO driven" with the chosen depths and settings.

## Part 1 (H, 2026-10-20): die-line Zo vs depth

Run `run_20261020_154847` (LineImpedance, which_line=chip, 1-20 GHz, 2 mm section, die gap forced 0.119 mm, circle pocket 6.25 mm, TMM 10i). Zo over 1-20 GHz, mean (min..max), ohm:

| Depth (mm) | Zo mean (min..max) | Valid |
|---|---|---|
| 0 | 49.00 (46.32..58.47) | yes |
| 0.1 | 49.19 (48.71..53.77) | yes |
| 0.2 | 49.25 (48.76..49.74) | yes |
| 0.3 | 50.77 (50.24..51.72) | yes |
| 0.5 | 49.86 (49.29..50.29) | yes |
| 0.75 | 49.03, flat (53 of 53 points equal) | no, see Part 1b |
| 1.0 | 49.00, flat | no, see Part 1b |

## Part 1b (2026-10-21): re-solve of depth 0.75 and 1.0

Re-solve run `run_20261021_115901` (same line settings as H, two combinations, queued 2026-10-21 11:59).

| Depth | Run | Solve time | Worker status | Zo (ohm) |
|---|---|---|---|---|
| 0.75 | H, `run_20261020_154847` | 16 min 14 s | "Error in Solving Setup_1", task marked success | 49.03 flat |
| 0.75 | `run_20261021_115901` | 16 min 31 s | same | 49.03 flat (identical value) |
| 1.0 | H, `run_20261020_154847` | 8 min 26 s | same | 49.00 flat |
| 1.0 | `run_20261021_115901` | 8 min 22 s | same | 49.00 flat (identical value) |

**Result:** the failure reproduces exactly at both depths, and the solved value is the same flat number each time, so the 0.75 and 1.0 Zo values are invalid. Re-solving the same model will fail the same way.

**Cause (not fully isolated):** the worker log records only "Error in Solving Setup_1"; the AEDT message log is not kept. The HFSS mesh-quality file (`current.g3derr`) in each results folder lists "Small mesh segment detected on body: vacuum", with segments of 0.083 to 0.113 um at x 1.13 to 1.6 mm, y 1.131 mm, z -1.5 mm (adaptive pass 1, 20 GHz). Depth 0.5 also has 12 such warnings and solved in 36 s, so the slivers alone do not explain the failure. The solver profiles show no mesh size limit: 0.75 stopped after adaptive pass 5, while 0.5 and 1.0 reached pass 6. So this is a model (geometry) problem at these depths, not a worker glitch. The worker still reports success with a flat result, so a line result must be checked for flatness before it is used.

## Why B and C took 4.5 h and 9.75 h (read-only check)

The 2026-10-18 runs did not use the same solver settings as the 2026-10-20 runs (run configs compared):

| Run | max_passes | max_delta_s | min_passes | min_converged | max_delta_f | Adaptive passes | Largest tet count (profile) | Solve time (worker log) |
|---|---|---|---|---|---|---|---|---|
| 2026-10-18 `run_20261018_165006` | 4 | 0.05 | 1 | 1 | 5.0 | 4 | about 0.88 M | 7 to 18 min |
| 2026-10-20 B `run_20261020_103237` | 99 | 0.02 | 3 | 2 | 3.0 | 16 | about 1.79 M | 4 h 27 min |
| 2026-10-20 C `run_20261020_150512` | 99 | 0.02 | 3 | 2 | 3.0 | 14 | about 2.27 M | 9 h 45 min |

The 2026-10-18 profile stops at its pass cap with the last Delta S at 0.84, well above the 0.05 target, so those runs were not converged. The C profile's Delta S falls to 0.014 by pass 14, below the 0.02 target. (The Delta S units are read from the profile and assumed to match max_delta_s; check before quoting numbers.)

**Consequence:** the slowdown comes from the tighter convergence settings (more passes, a larger mesh), not from the machine. The 2026-10-18 comparison numbers are under-converged. The 2026-10-20 B and C numbers are the converged ones.

## Part 2: not started

Waiting for the manager's "GO driven". The planned settings are those of `run_20261020_150512` (checked: all analysis settings match, including DrivenModal, 2-20 GHz, 73 points, fine windows off, connector reference plane, no pairing). Estimated time: 4 to 10 h per driven solve on the worker, so about 1 to 2 days for four depths. The die gap is solved by the pipeline per combination; whether it changes with depth has not been checked yet.
