# Lid and substrate comparison, 2026-10-18 (task B2)

## Summary

- **Best substrate: RO3003** (0.45 mm trace). Bare, it is clean for 100 / 97 / 75 % of 2-10 / 10-17 / 17-20 GHz with mean S11 -13.3 / -8.2 / -7.5 dB. With the chip it is 100 / 72 / 50 %. RO4003 is close on the bare board (100 / 97 / 50 %) and worse with the chip (100 / 69 / 17 %). TMM 10i is clean for only 73 % of 2-10 GHz and 8 % of 17-20 GHz, bare.
- **The -7 dB mean S11 is specific to TMM 10i.** It shows up at 2-10 GHz on TMM 10i (-6.8 dB bare, -7.3 dB with the chip). RO3003 and RO4003 sit at -12 to -14 dB in the same band. This agrees with the earlier S11 analysis (PR #75).
- **What the new lid and keep-out changed (TMM 10i, against the old-lid reference):** bare, the 10-17 GHz clean share rose from 34 % to 45 %, the 2-10 GHz share fell from 76 % to 73 % and the mean S11 there got worse (-8.0 to -6.8 dB), and 17-20 GHz is unchanged at 8 %. With the chip, 10-17 GHz went from 31 % to 48 % clean. The lid also changed the gap (271 um to 247 um), so these effects are not from the lid alone.
- **The chip** costs little on TMM 10i with the new lid, but costs a lot on RO3003 and RO4003 above 10 GHz (10-17 GHz clean 97 to 72 % on RO3003, 97 to 69 % on RO4003).
- **Caveats:** every run here uses coarse 0.25 GHz points only (73 points, 2-20 GHz), with no fine windows and driven only. The reference was re-read with coarse rows only, so its numbers differ from the 80 / 50 / 1 % in RESULTS_CONCLUSIONS.md, which include fine-window rows. The 17-20 GHz band has only 12 points, so its share moves in steps of about 8 %. The RO3003 and RO4003 gaps are the layout-check values (134 and 135 um), not read back from the solver. The chip gap of 119 um was recomputed from the solver on the same inputs; it is not stored in the run folder.

## Table

Bands: 2-10 GHz (33 points), 10-17 GHz (29 points), 17-20 GHz (12 points). Mean S11 in dB over each band. "Clean" = share of points with S21 above -3 dB. All runs: driven only, 2-20 GHz, 0.25 GHz step, connector reference plane.

| Case | Run | Date | Trace (mm) | Gap (um) | Lid | Chip | S11 2-10 | S11 10-17 | S11 17-20 | Clean 2-10 | Clean 10-17 | Clean 17-20 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| TMM 10i bare, new lid | run_20261018_161144 | 2026-10-18 | 0.20 | 247 | 1.2 x 0.3 mm | no chip | -6.8 | -5.4 | -2.1 | 73 % | 45 % | 8 % |
| RO3003 bare, new lid | run_20261018_162625 | 2026-10-18 | 0.45 | 134 | 1.2 x 0.3 mm | no chip | -13.3 | -8.2 | -7.5 | 100 % | 97 % | 75 % |
| RO4003 bare, new lid | run_20261018_163903 | 2026-10-18 | 0.40 | 135 | 1.2 x 0.3 mm | no chip | -12.0 | -7.8 | -7.3 | 100 % | 97 % | 50 % |
| TMM 10i chip, new lid | run_20261018_165006 | 2026-10-18 | 0.20 | 247 (die 119) | 1.2 x 0.3 mm | die 0.20 mm | -7.3 | -5.7 | -1.5 | 76 % | 48 % | 8 % |
| RO3003 chip, new lid | run_20261018_171149 | 2026-10-18 | 0.45 | 134 (die 119) | 1.2 x 0.3 mm | die 0.20 mm | -13.9 | -8.0 | -4.0 | 100 % | 72 % | 50 % |
| RO4003 chip, new lid | run_20261018_172821 | 2026-10-18 | 0.40 | 135 (die 119) | 1.2 x 0.3 mm | die 0.20 mm | -12.6 | -7.3 | -5.1 | 100 % | 69 % | 17 % |
| TMM 10i bare, old lid (reference) | run_20261009_191045 | 2026-10-09 | 0.20 | 271 | 0.5 x 0.04 mm | no chip | -8.0 | -4.9 | -2.7 | 76 % | 34 % | 8 % |
| TMM 10i chip, old lid (reference) | run_20261009_204719 | 2026-10-09 | 0.20 | 271 | 0.5 x 0.04 mm | die 0.20 mm | -7.9 | -3.5 | -1.6 | 64 % | 31 % | 8 % |

The reference runs cover 2-26 GHz (97 points); only the 2-20 GHz rows enter the bands above (same 33 / 29 / 12 points as the new runs).

CSV copy: `comparison-20261018.csv` in this folder.
