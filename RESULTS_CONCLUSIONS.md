# What the runs showed

Written 2026-10-07 from the 29 runs left in `runs/` after the cleanup. The
target is the band above 20 GHz (17 GHz and up is where it starts to matter).

## In short

1. **The box modes decide everything, and board size sets them.** Every dip
   in S21 lines up with a cavity mode. On the 50 x 25 mm board the lowest
   mode is about 3 GHz and S21 is above -1 dB for only about 30 % of
   0.1-20 GHz. Shorter boards move the lowest mode up fast: 70 mm gives
   1.8 GHz, 40 mm gives 4.3 GHz.
2. **Vias and pedestal do not move the lowest box mode.** Via diameter,
   via spacing and pedestal depth each change it by 1 % or less. They only
   reshape the S21 dips.
3. **Pedestal on versus off matters a little. How deep does not.**
   Any depth from 0.25 to 10 mm gives about the same S21. All of them beat
   no pedestal at 15-20 GHz by about 1 dB on average, and cost about 0.3 dB
   at 1-5 GHz.
4. **A deliberate distortion in the box is the biggest lever besides size.**
   It moved the lowest mode by -24 % to +20 % in the older design, with Q
   barely changing.
5. **Wire bonds pull a high mode down.** The fifth mode goes from 17.25 GHz
   without bonds to 16.12 GHz with them. The lowest mode moves only 1.4 %.
6. **Nothing tried so far puts a clean S21 above 20 GHz.** The best run
   still has dips at 15.6-18 GHz and 19-20 GHz.
7. **The deepest notches above 16 GHz are not box modes** (box study, 2026-10-10).
   On the 50 x 25 mm bare board S21 is clean for 80 % of 2-10 GHz, 50 % of
   10-17 GHz and about none above 17 GHz. The notches at 16.75, 18.0 and 25.75
   GHz stay when the board's length or width changes and shrink when the SMP
   connector is taken out of the model, so they belong to the connector and its
   launch. The chip makes every band worse (66 / 32 / 8 / 0 % clean).
8. **Those notches belong to the via lattice and the launch, and they can be moved
   but not removed** (box study, iterations 3-5). The deep notch near 25 GHz is a low-Q
   (Q 550) substrate mode set by the via step: 25.0 GHz at 0.8 mm, 24.1 GHz at
   1.2 mm. The one near 16.75 GHz moves with the setback of the via fence from the
   CPW gap, about -5 GHz per mm (14.5 GHz at 600 um). A wider via step (1.2 mm) or
   a 600 um setback makes 17-20 GHz half clean (from 1 %); a lid relief of 200 um
   makes 10-17 GHz 75 % clean (from 50 %). Combining them is worse than either, and
   no set tried cleans 20-26 GHz.
9. **A low-permittivity substrate removes those notches** (substrate study, 2026-10-10).
   On RT/duroid 5880 with a 0.4 mm trace (gap 111 um) the bare board is clean for
   99 % of 10-17 GHz and 72 % of 17-20 GHz (TMM 10i: 50 % and 1 %), and every
   remaining notch is a box mode, which board size can move. 20-26 GHz is 2 % clean:
   the 20.04 and 20.07 GHz box modes and a weak notch at 25.75 GHz. The traps on the
   way, now caught by the layout check: a 0.2 mm trace needs an 86 um gap on 5880
   and does not transmit (limit 100 um), and a lid relief channel no wider than the
   trace shorts it (limit trace + 0.1 mm).
10. **The candidate board is clean to 20 GHz** (lid cover study, 2026-10-10; the "with the chip"
   numbers here and in B19/B20 are for a die with broken copper, see "the cover-aware gap"): RT/duroid
   5880, `cpw_trace_width` 0.4 mm, `route_width` 1.0 mm, `route_clearence` 200 um, all
   else default. S21 is above -3 dB for 100 % of 2-20 GHz and 54 % of 20-26 GHz with the
   chip (75 % without it); S11 averages -12.9 dB in 2-10 GHz. It needs both the
   substrate and the wide lid channel: 5880 with the default lid is 38 / 99 / 72 / 2 %
   and TMM 10i with the wide lid is 69 / 43 / 17 / 4 %. On a finer 0.1 GHz grid and a tighter mesh (B20, with the chip) it is 99 % of
   2-20 GHz and 61 % of 20-26 GHz: three narrow notches at box modes (16.94, 18.58 and 19.20
   GHz, down to -9.5 dB) and a cluster at 24.6-25.8 GHz (down to -42 dB) remain.
11. **The CPW gap is now solved for the line under the lid** (cover-aware gap, 2026-10-10). The old
   gaps were not 50 ohm lines even before the lid: a 2-D field solve puts the old 111 um gap on 5880 at
   62 ohm with nothing over it, 52 ohm under the candidate lid, and the old default lid (0.5 mm / 40 um)
   at 24-28 ohm on both boards. The default lid is now 1.2 mm / 300 um, since the old one cannot
   reach 50 ohm at any gap.

## How much to trust it

- Every conclusion below comes from one board outline per comparison and one
  mesh per point. Where I say "no effect", the spread was inside what a
  repeat of the same setup gives.
- **Dip depth is not reproducible; dip position is.** The same setup run
  twice (`0929_102232` and `1005_130249`) agreed on where each dip sits to
  within 0.1 GHz, but the depth of the narrow ones differed by tens of dB.
  Differences of a few dB in dip depth should be read as noise.
- The design changed under these runs. The `0820`-`0902` runs are an older
  design (no CPW or via lattice). The `0916`-`0922` runs are the SMP and
  early via design. `0928` onward is the CPW design with a mesh fix. The
  via-spacing S21 sweep (`0929_142652`) used the via layout from before
  2026-10-05, when the vias became one symmetric lattice. Compare across
  generations only as a rough direction, never as a number.
- Eigenmode runs show only the box modes. Driven runs add the connector
  transitions, so a dip in S21 is a mode only when an eigenmode run puts a
  mode at that frequency.

## Board size

Eigenmode sweep of PCB length (`0928_163245`, 25 mm wide, port space 5 mm,
vias at 1.0 mm):

| PCB length (mm) | 40 | 45 | 50 | 55 | 60 | 65 | 70 |
|---|---|---|---|---|---|---|---|
| Lowest mode (GHz) | 4.25 | 3.49 | 2.97 | 2.57 | 2.27 | 2.02 | 1.83 |
| Q | 125 | 101 | 86 | 77 | 70 | 63 | 60 |

- The fall is steeper than 1/length: from 40 to 70 mm the mode drops by a
  factor of 2.3 while the length grows by 1.75. Roughly f goes as
  length^-1.5 over this range. I would not extrapolate it to 20 GHz. Doing
  so gives a length near 14 mm, which does not leave room for the two
  connectors.
- A second mode near 8.5 GHz does not move with length, so some modes depend
  on the width and not the length.
- Width matters too. In the older design a 15 mm wide board has its lowest
  mode at 4.07 GHz and a 20 mm wide one at 3.25 GHz (`0827_154251`,
  `0820_200714`).
- The 50 x 25 driven runs show a dip at 3.0-3.7 GHz. That matches the 2.97
  GHz eigenmode, so the mode is visible in transmission.
- Every 50 x 25 driven run also first drops below -3 dB at about 2.1 GHz.
  Nothing I changed (vias, pedestal, port plane) moved it. I did not find
  its cause.

## Pedestal depth

Driven S21 sweep, 50 x 25, vias 1.5 mm, 0.1-20 GHz (`1005_183834`). The
vacuum carve fix was in for this run.

| Pedestal (mm) | Mean S21 1-5 GHz | Mean S21 15-20 GHz | Worst S21 15-20 GHz | Mean S11 15-20 GHz |
|---|---|---|---|---|
| 0 | -2.24 dB | -8.70 dB | -35.2 dB | -3.7 dB |
| 0.25 | -2.54 | -7.06 | -26.1 | -5.0 |
| 0.5 | -2.54 | -7.06 | -24.7 | -5.2 |
| 1 | -2.56 | -7.46 | -25.8 | -5.2 |
| 3 | -2.59 | -7.80 | -27.1 | -5.3 |
| 10 | -2.59 | -7.68 | -26.6 | -5.0 |

- Depths 0.25 to 10 mm are within about 0.8 dB of each other and show no
  trend, so any pedestal that fits is as good as a deep one.
- The 0.25 and 0.5 mm pedestals are the best by a small margin at 15-20 GHz.
- The deepest dip, at 11.0-12.8 GHz, does not move with pedestal depth.
- The eigenmode sweeps agree. The lowest mode stays at 3.25 +/- 0.02 GHz
  (20 mm wide) and 4.07 +/- 0.04 GHz (15 mm wide) over 0-10 mm, with Q
  between 511 and 516.
- This is a single board and a single series. Treat "a shallow pedestal is
  slightly better than none" as likely, not settled.

## Vias

- **Eigenmode:** the lowest mode barely moves. Via spacing 1, 2 and 3 mm
  gives 1.61, 1.60 and 1.59 GHz (70 x 30 mm board, `0928_134432`). Via
  diameter 0.2 to 0.5 mm gives 1.61 down to 1.60 GHz (`0928_155218`).
- **Driven (old via layout, `0929_142652`):** spacing moves the deepest dip.

| Via spacing (mm) | 3.0 | 2.0 | 1.5 | 1.0 | 0.8 | 0.6 |
|---|---|---|---|---|---|---|
| Deepest dip (GHz) | 8.4 | 9.8 | 11.8 | 14.2 | 14.5 | 11.4 |
| Its depth (dB) | -25 | -39 | -43 | -56 | -64 | -14 |
| Band above -1 dB | 29 % | 38 % | 43 % | 37 % | 36 % | 42 % |

  A tighter grid pushes the deepest dip up until 0.8-1.0 mm, then 0.6 mm
  reverses it. The fraction of the band above -1 dB hardly changes, so this
  is a shift of the dips, not an improvement in transmission.
- This sweep has to be repeated on the current symmetric via layout before it
  is used to pick a number. At 1.5 mm the old layout puts the deepest dip at
  11.8 GHz and the new one at 12.3 GHz.

## Distortion of the box (older design)

Two sweeps of `distortion_radius`, `distortion_x` and `distortion_y`
(`0901_210825`, 266 points; `0902_123231`, 126 points, zoomed in):

- Lowest mode over the first sweep: 7.83 to 12.31 GHz, against 10.26 GHz
  with no distortion. Q stays between 492 and 577.
- The highest points (11.9-12.3 GHz) are at radius 0, `distortion_x` 1.75-2
  and `distortion_y` 1.5-2.5. The lowest (7.8-8.0 GHz) are at radius 0.125
  and `distortion_y` 2.5.
- A small hole is the worst case. Mean lowest mode by radius: 9.3 GHz at
  0.125, rising to 10.8 GHz at 1.0.
- The zoomed sweep (radius 0.8-1.1) stays within 10.95-11.81 GHz. There,
  larger radius helps slightly: mean 11.22 GHz at 0.8 and 11.59 GHz at 1.1.
- This was run on the old design, so it shows that distortion is a strong
  lever. It does not give numbers for the current design.

## Wire bonds

Identical setups with and without bonds (`0922_123524`, `0922_124101`):

| Mode | With bonds (GHz) | Without (GHz) |
|---|---|---|
| 1 | 5.82 | 5.90 |
| 3 | 14.15 | 14.24 |
| 5 | 16.12 | 17.25 |

Q for the lowest mode is 181 against 180. The bonds cost 1.4 % on the first
mode and 6.5 % on the fifth. Since the target starts near 17 GHz, the fifth
mode is the one to watch.

## Runs that cannot be read as transmission

`1006_145337` (gap ports at the chip, connectors terminated) sits near -6 dB
at every frequency from 0.1 to 20 GHz (-8.2 dB at 1 GHz, -4.7 dB at 15 GHz).
A gap port taps the line, so that floor comes from the port, not the board.
It says nothing about the board's transmission.

## Box walls: perfect E against finite conductivity

Written 2026-10-09 from the post-merge check of the tools, one combination
each, default board with chip and corner screws, 3 eigenmodes, run on the
worker with loose convergence (4 passes, 5 % delta f). Perfect E is the
default for the box; the other run gives the box and lid a finite-
conductivity boundary of 1e8 S/m (6061 aluminium at helium temperature is
of that order).

| Mode | Perfect E: f (GHz), Q | 1e8 S/m: f (GHz), Q |
|---|---|---|
| 1 | 2.387, 221 | 2.355, 125 |
| 2 | 3.728, 275 | 3.701, 217 |
| 3 | 6.304, 631 | 6.222, 254 |

- **Positions move by about 1 %** (the lowest by 1.3 %, the third by 1.3 %), so
  perfect E is good enough for placing modes and dips, as assumed.
- **Q drops by a factor of 1.3-2.5**, more for the higher modes, so perfect E does
  exaggerate how deep and how narrow a dip is. Read depth from a finite-
  conductivity run, and only to within a factor of a few.
- Sweeping `pedestal_depth` over 0.25, 0.5 and 0.75 mm on the same board moved the
  three modes by less than 0.5 % (2.352-2.355, 3.700-3.715, 6.212-6.222 GHz),
  with Q unchanged within 3 %. It does not move the low modes, as before.

Side findings of the same check, now fixed: a bare board failed validation
because the SolidWorks parts read the equations file of another worktree
(see the README, "The SolidWorks parts' equation link"), and the form's
"Corner screws" box did nothing when unticked.

## Box study, 2026-10-09 to 10: where the S21 notches come from (up to 26 GHz)

How it was run: one combination per run unless noted, perfect-E box, corner
screws on, 2-26 GHz, 97 broad points (0.25 GHz step), loose convergence (4
adaptive passes, max delta S 0.05), driven runs only except B1. Solve time on
the worker: a 20-mode eigenmode run 48 minutes; a driven 2-26 GHz run about 30
minutes (the mesh at 26 GHz is not much dearer than at 12 GHz). "Clean" below
means S21 above -3 dB. Runs are in `runs/`; ids in brackets.

**B1, bare board (no_chip), 50 x 25 mm, eigenmodes + S21** [run_20261009_191045].
S21 is clean for 80 % of 2-10 GHz, 50 % of 10-17 GHz, 1 % of 17-20 GHz and 0 %
of 20-26 GHz. The 20 modes found run from 2.38 to 18.58 GHz. Notches at 3.73,
9.96, 13.165, 15.55, 17.155 and 18.577 GHz each have an eigenmode within
0.03 GHz (17.155 is 0.07 GHz from its mode at 17.084): these are box modes.
Notches at 10.75, 16.75 (-38 dB), 18.0 and 20.25 GHz have no mode near them;
above 18.6 GHz the eigenmode run does not reach (20 modes), so the notches at
22.0, 25.25 and 25.75 GHz (-58 and -68 dB) are unexplained for now.

**B1c, same board with the chip, driven only** [run_20261009_204719]. Worse
everywhere: clean for 66 / 32 / 8 / 0 % of the four bands. The deep notches at
16.75 (-39 dB) and 25.25 GHz (-75 dB) are the same as without the chip, so they
do not come from the chip. The chip adds notches (14.25, 19.0, 19.75, 21.25,
23.75 GHz).

**B2, length of the bare board** [run_20261009_211800]. At 40 mm the notch set
changes (4.75, 13.75, 21.75, 22.75 GHz...), as box modes should, but notches
near 17.0, 18.0 and about 25.75 GHz stay. At 30 mm the solve is degenerate:
S11 = -0.18 dB and S21 = -49.9 dB at every one of the 97 frequencies, to the
last digit, phase included. That is a failed solve, not a reflecting line: the
copper and the connector positions in the saved model are consistent with
the 40 mm board (pads at +-3.5 mm, pins landing on them). Not understood yet;
30 mm is not a usable result and 35 mm should be tried.

**B3, width of the bare board** [run_20261009_220431]. At 35 mm wide (against
25 mm) the same fixed notches appear at 17.0 (-66 dB), 18.0, 20.25 and 25.75
GHz (-60 dB). A 15 mm side is refused by the Altium stage: the copper the plane
gives up round a screw reaches 2 mm from the centre line but the SMP launch is
2.6 mm either side.

**B4, no connector** (`launch` reference plane, a lumped port under the trace)
[run_20261009_230504]. S21 is poor everywhere because that port is a tap, so
the clean fractions cannot be compared, but the depths can: the notches at
16.75 and 17.15 GHz fall from -38 and -52 dB to -4 dB, and 25.75 GHz from -68 to
-40 dB. This suggests the deepest notches are tied to the SMP connector and its launch (the lumped port is a different port, so it is not a clean A/B);
the box modes alone (B1) have none within 0.2 GHz of 16.75 GHz.

**What this says for the design.** (1) The deepest notches above 16 GHz do not
move with the board's length or width, are absent as eigenmodes, and shrink
when the connector is removed: look at the connector and its launch (the
capped bore and the pin in the model, the ground fence and `route_clearence`
at the launch), not at the box. (2) The chip makes S21 worse in every band (measured); whether
removing or shrinking it is worth more than the box levers is not measured yet. (3) The box modes still sit at
the places the earlier runs found; board size remains the lever that moves
them.

**B5, via lattice step** (`via_max_spacing`; bare 50 x 25 mm, connector plane)
[run_20261009_232127]. The deep
notch moves with the lattice: 17.5 GHz (-47 dB) at 0.5 mm, 16.75 GHz (-38 dB)
at 0.8 mm, 15.75 GHz (-68 dB) at 1.2 mm, and the one near 25.75 GHz moves to
24.5 GHz at 1.2 mm. So they depend on the via lattice, not on the connector
alone. Clean fractions of 2-10 / 10-17 / 17-20 / 20-26 GHz: 0.5 mm 75 / 39 / 8 / 0 %;
0.8 mm 80 / 50 / 1 / 0 %; 1.2 mm 72 / 32 / 50 / 0 %.

**B6, eigenmodes from about 15 GHz at via 0.5 and 1.2 mm** [run_20261010_002210] (8 modes, loose
convergence). The modes split in two kinds. Those with Q of 1000-2100 do not
move with the via lattice (15.13, 15.55, 16.94, 17.08 and 18.58 GHz): box
modes. Those with Q of 450-580 do, and monotonically: one goes 15.88, 15.67,
15.46 GHz for 0.5, 0.8, 1.2 mm, another 17.61, 17.41 GHz for 0.5, 0.8 mm: modes
of the substrate between the via fences, which the lattice sets, with the lower Q
of a lossy dielectric. None lies at 16.75 GHz (within 0.15 GHz), so that notch
is not an eigenmode of the structure as modelled (the eigenmode model has no
ports); these runs only reach 19 GHz, so 25.75 GHz is not covered.

**B7, lid relief depth** (`route_clearence`, entered in micrometres; the first
attempt used 0.2 and 0.5 *um* by mistake, which shorts the trace: S21 -90 dB at
2 GHz) at 200 and 500 um against the 40 um default [run_20261010_012304]. Clean
fractions 62 / 75 / 17 / 0 % (200 um) and 62 / 75 / 8 / 0 % (500 um), against
80 / 50 / 1 / 0 % for 40 um: a deeper relief lifts the 10-17 GHz band from 50 to
75 % clean at the price of 18 points in 2-10 GHz. The notches at 16.75 and 25.25 GHz stay.

**Summary of the levers so far** (bare 50 x 25 mm board, the share of each band that is
clean; baseline 80 / 50 / 1 / 0 %):

| Change | 2-10 | 10-17 | 17-20 | 20-26 GHz |
|---|---|---|---|---|
| via step 0.5 mm | 75 | 39 | 8 | 0 |
| via step 1.2 mm | 72 | 32 | 50 | 0 |
| lid relief 200 um | 62 | 75 | 17 | 0 |
| lid relief 500 um | 62 | 75 | 8 | 0 |
| board length 40 mm | 59 | 50 | 0 | 4 |
| board width 35 mm | 75 | 36 | 8 | 0 |
| with the chip (B1c) | 66 | 32 | 8 | 0 |

No single change cleans 20-26 GHz: a deep notch between 24.5 and 25.75 GHz
survives all of them, moving with the via step but never leaving the band. These
are 0.25 GHz-step scans, so a narrow notch can be missed and the fractions are
a guide to position, not a measurement of depth (perfect E exaggerates it).

**B8, via step and lid relief together** [run_20261010_022423] (via 1.0 and 1.2 mm
with 200 um relief): 59 / 43 / 33 / 0 % and 59 / 61 / 33 / 0 %. Not additive:
against relief alone (62 / 75 / 17 / 0 %) the 10-17 GHz band is worse, and against
via 1.2 mm alone (72 / 32 / 50 / 0 %) the 17-20 GHz band is worse.

**B9, eigenmodes from about 23 GHz** [run_20261010_025450] (4 modes, via 0.8 and 1.2 mm).
At via 0.8 mm: 23.77 (Q 3900), 24.21 (Q 18000), 24.30 (Q 6100) and 24.995 GHz
(Q 553). At 1.2 mm: 23.77 (Q 3900), 24.11 and 24.14 (Q 514 each) and 24.21 GHz
(Q 17700). The three with Q in the thousands do not move; the Q 550 one goes
from 25.0 to about 24.1 GHz with the via step, as the driven notch does (25.25 to
24.5 GHz). So **the deep notch near 25 GHz is a low-Q substrate mode set by the
via lattice**, not a box mode and not the connector. It moves by about -2.2 GHz per
mm of via step; the in-between values (0.5 mm) are the way up, but at 0.5 mm the
deepest 20-26 GHz notch is only -28 dB (B5), still in the band.

**B10, setback of the via fence from the CPW gap** (`via_gnd_clearance`, in
micrometres; 150 um default) [run_20261010_031023]. The deep launch-side notch
moves almost linearly with it, about -5 GHz per mm: 16.75 GHz at 150 um, 16.0 GHz at
300 um, 14.5 GHz at 600 um. Clean fractions 75 / 32 / 17 / 0 % at 300 um and
69 / 32 / 50 / 0 % at 600 um. That is the behaviour of a resonance of the strip of
ground between the gap and the fence; it cannot be pushed past about 17.5 GHz
by a smaller setback (the line extrapolates to 17.5 GHz at zero setback). The 25 GHz
notch does not move with it.

**B11, via 1.2 mm + setback 600 um + relief 200 um** [run_20261010_034053]: 59 / 46 / 8 / 0 %. Worse
than the best single changes: stacking the levers does not stack the gains.

**Best sets so far** (bare 50 x 25 mm board, perfect E, 0.25 GHz step; the share clean in
2-10 / 10-17 / 17-20 / 20-26 GHz):

| For | Set | Clean |
|---|---|---|
| The 17-20 GHz band | `via_max_spacing` 1.2 mm, rest default | 72 / 32 / 50 / 0 % |
| The 17-20 GHz band | `via_gnd_clearance` 600 um, rest default | 69 / 32 / 50 / 0 % |
| The 10-17 GHz band | `route_clearence` 200 um, rest default | 62 / 75 / 17 / 0 % |
| Everything up to 10 GHz | the defaults (0.8 mm, 150 um, 40 um) | 80 / 50 / 1 / 0 % |

Nothing reaches 20-26 GHz: the notch near 25 GHz is a substrate mode of the via
lattice, and with a lattice fine enough to push it out of the band (under about
0.5 mm) the board still has notches at 21-21.5 GHz. A clean 20-26 GHz band is not
available from these levers on this board; the remaining ones are the board size
(a shorter board has fewer and higher box modes but the 30 mm one failed to solve),
removing the chip, and a different substrate or thinner board for the lattice modes.

Next: the 30 mm degenerate solve and board lengths between 30 and 40 mm; the
substrate (thickness and permittivity set the lattice modes); and field plots of the
Q 550 mode on a coarser mesh to say where it lives.

## Box study, substrate: RT/duroid 5880 instead of TMM 10i (2026-10-10)

The lever suggested by the 25 GHz result: the via-lattice and launch notches
are resonances of the substrate, which scale with its permittivity (TMM 10i
9.8, RT/duroid 5880 2.2). Same bare 50 x 25 mm board, perfect-E box, corner
screws, 2-26 GHz, 97 broad points, loose convergence. The CPW gap is solved
for 50 ohm on the substrate, so it changes with it (trace 0.2 mm: 0.271 mm gap
on TMM, 0.086 mm on 5880; 0.4 mm: 0.111 mm; 0.5 mm: 0.127 mm).

**B12, the default 0.2 mm trace** [run_20261010_040124] and **B15** [run_20261010_071748]:
no transmission (S11 -0.02 dB, S21 -91 dB at 2 GHz) even with the lid channel
widened to 0.8 mm. The 86 um gap is the only difference to a working board, so
it is refused by the layout check from now on (minimum 100 um). Cause not
found beyond that; the model has the right material (eps 2.2, tand 0.0009) and
the gap is drawn in the copper.

**B13, 0.4 and 0.5 mm traces** [run_20261010_043508]. At 0.4 mm (gap 111 um,
manufacturable) S21 is clean for **41 / 100 / 75 / 8 %** of 2-10 / 10-17 / 17-20 /
20-26 GHz (TMM 10i baseline: 80 / 50 / 1 / 0). At 0.5 mm no transmission: the
lid's relief channel is route_width = 0.5 mm wide, exactly the trace, so the lid
lands on the trace edges and shorts it. **B15** confirms it: with route_width
0.8 mm the 0.5 mm trace transmits, **28 / 79 / 100 / 0 %** (17-20 GHz entirely clean).
The layout check now refuses a channel less than 0.1 mm wider than the trace.

**B14, 5880 / 0.4 mm trace, eigenmodes and S21** [run_20261010_053717] (20 modes,
fine windows at each mode): clean **38 / 99 / 72 / 2 %**. Every notch has an
eigenmode within 41 MHz: 7.195 (mode 7.185 GHz, Q 1224), 9.945 (9.956),
13.19 (13.175), 18.579 (18.581), 19.233 (19.274) and 20.041 GHz (20.071), plus one at
6.0 GHz and the shallow ones at 22.5 and 25.75 GHz (-27 dB). The deep notches that
were not box modes on TMM 10i (16.75 and about 25 GHz) are gone.

**What moved, against 1/sqrt(eps).** The modes of the air cavity do not depend on the
substrate and did not move: 7.18 / 7.19, 9.95 / 9.96, 12.38 / 12.39, 13.17 / 13.18,
13.49 / 13.50, 15.13 / 15.13, 15.55 / 15.55, 16.95 / 16.96, 17.08 / 17.09 and
18.58 / 18.58 GHz (TMM 10i / 5880), each within 0.01 GHz. The modes that live in the
substrate moved up: the lowest from 2.38 to 3.04 GHz (x1.28) and the next from
3.76 to 4.96 GHz (x1.32), and modes at 6.55, 8.30, 12.91, 13.89 and 17.41 GHz on TMM are
replaced by others at different places. A substrate mode moves as 1/sqrt(eps_eff)
with eps_eff a mix of the substrate and the air above it, so the factor is less than
sqrt(9.8 / 2.2) = 2.1; 1.3 is what these two give. A notch that does not move is a
box or air mode, and these are what remains on 5880.

**Candidate design so far** (bare board, 50 x 25 mm, perfect E): RT/duroid 5880,
`cpw_trace_width` 0.4 mm (gap 111 um), everything else default: clean
38 / 99 / 72 / 2 % of 2-10 / 10-17 / 17-20 / 20-26 GHz, the best of the study.
With `route_width` 0.8 mm and a 0.5 mm trace (gap 127 um) 17-20 GHz is fully clean
and 10-17 GHz 79 %, but 2-10 GHz only 28 %. What is left are box modes, which board
size moves (7.19, 9.96 and 13.18 GHz in 2-17 GHz, and 20.04 and 20.07 GHz at the bottom
of the 20-26 GHz band) and the weak notch at 22.5 and 25.75 GHz. Not yet tried on
5880: the chip, the via step and the relief, the board length.

## Box study, the lid cover and the candidate board (2026-10-10)

**B16, board length on 5880** [run_20261010_075040] (bare, 0.4 mm trace, default lid). Clean in
2-10 / 10-17 / 17-20 / 20-26 GHz: 40 mm 44 / 64 / 25 / 21 %, 60 mm 56 / 71 / 92 / 29 %
(50 mm, from B14: 38 / 99 / 72 / 2 %). Length moves the box modes in and out of the
20-26 GHz band, and it is the one lever that cleaned part of it before the lid was
changed.

**B17, a wide lid channel** [run_20261010_082104]. The 50 ohm gap is solved for the
substrate alone, but the lid sits on the board's ground copper with only a 40 um relief
over the trace, so the line is really a covered CPW, and on a low-permittivity board
the cover loads it far more than on TMM 10i. On 5880 with the 0.4 mm trace,
`route_width` 1.0 mm (the channel clears the trace and both gaps) and
`route_clearence` 200 um: clean **100 / 100 / 100 / 75 %**; with 300 um 100 / 100 / 100 /
71 %. S11 in 2-10 GHz averages -14.4 dB (worst -8.3 dB), against -4.8 dB (worst -1.0 dB)
with the default 0.5 mm channel and 40 um relief; in 10-17 GHz -10.2 dB. S21 in
2-20 GHz never falls below -2.0 dB. What is left of 20-26 GHz is a notch at 25.75 GHz
(-21 dB) and nothing else (worst -21 dB).

**B18, the same lid on TMM 10i** [run_20261010_085138] (0.2 mm trace, `route_width` 1.0 mm,
200 um relief): 69 / 43 / 17 / 4 %, with the 16.75 and 25.5 GHz notches back. So the gain is
the 5880 substrate and the wide lid together; the lid alone does not rescue the
high-permittivity board.

**B19, the board with the chip** [run_20261010_093710] (**the die's copper was broken in this run, see
"the cover-aware gap", item 3**; 5880, 0.4 mm trace, 1.0 mm channel,
200 um relief, pedestal 0.5 mm, corner screws on): clean **100 / 100 / 100 / 54 %**; S11
in 2-10 GHz -12.9 dB on average (worst -4.1 dB), in 10-17 GHz -9.5 dB (worst -4.1 dB).
The chip, which made every band worse on TMM 10i, costs almost nothing here: S21 stays
above -2.2 dB from 2 to 20 GHz and 20-26 GHz is 54 % clean, worst -26.6 dB at 25.0 GHz.

**The candidate** (`default.txt` values that differ, plus the material). Both the bare
board and the board with the chip were run with exactly this:

| Setting | Candidate | Default at the time (lid since changed to 1.2 mm / 300 um) |
|---|---|---|
| `mat_pcb` (new-run form, PCB material) | RT/duroid 5880 (eps 2.2) | Rogers TMM 10i (eps 9.8) |
| `cpw_trace_width` | 0.4 mm | 0.2 mm |
| `route_width` (lid relief width) | 1.0 mm | 0.5 mm |
| `route_clearence` (lid relief depth) | 200 um (0.2 mm) | 40 um |

Everything else is the default (50 x 25 mm board, 0.8 mm via step, 150 um via fence
setback, corner screws on, perfect-E box). The 50 ohm gap that followed from the trace
was 111 um on 5880 (the solve then ignored the lid, so it did not change with it; solved
under the lid it is 94 um, see "the cover-aware gap").
A bare board is the same with `no_chip` 1 and `pedestal_depth` 0 (the form pins both).

**Done afterwards, see the next section:** the CPW 50 ohm solve ignored the lid cover (and its thickness
correction was off): it now solves the line under the lid and the layout check refuses a relief that
is too shallow or too narrow for it. The runs above used the old gaps (111 um on the candidate) and
0.25 GHz steps with loose convergence (4 passes, delta S 0.05); B20 repeats the candidate with
tighter settings.

## Box study, the cover-aware gap (2026-10-10)

The follow-up the lid cover section proposed is done: `cpw.solve_gap_for_z0` now solves the
50 ohm gap for the line under the lid, and the layout check refuses a lid that cannot carry
it. Two things came out of doing it that the earlier sections did not know.

**1. The gap was never right for 35 um copper either.** A 2-D finite-element solve of the real
cross-section (`base_parts/cpw_fd_check.py`: the copper as a solid with sidewalls, the board,
the lid with its channel and the channel's side walls; thin metal agrees with conformal mapping
to 0.5 %, and the sidewall capacitance of thick metal is exactly the parallel-plate t/gap in the
limit) says the old thickness correction overcorrected by about two. With nothing over the line, the
gap the old solve called 50 ohm is a **62 ohm** line on RT/duroid 5880 (0.4 mm trace, 111 um)
and 55 ohm on TMM 10i (0.2 mm, 271 um). The old correction widens the trace by
1.25 t/pi (1 + ln(4 pi w/t)), 83 um at 0.4 mm, a first-order formula applied where it does not hold;
the delta that fits the field solve is 1.1-1.4 t on 5880 and 0.8-1.1 t on TMM, so it is not a
geometry shift at all. The sidewalls add capacitance in the air above the board, and the model is now
that: `t/gap + 0.09 (1 - exp(-t/10 um))` added to the air-side partial capacitance. With the lid as a
ceiling `route_clearence` over the trace it reproduces the field solve: the gap it solves is 50 ohm to
0.9 % rms (5.7 % at worst) over 484 geometries (eps_r 2.2-10.2, board 0.254-0.635 mm, copper 18-70
um, trace 0.15-0.8 mm, lid 0.04-3 mm high), and the impedance of any geometry to 0.6 % rms (1.7 %
at most) once the lid is 0.18 mm or more over the line. It is why the candidate worked at 111 um:
the lid brought the 62 ohm line down to 52.

| Board, trace | Lid: channel / relief | Old gap | Field solve at the old gap | New gap | Field solve at the new gap |
|---|---|---|---|---|---|
| 5880, 0.4 mm | 1.0 mm / 200 um (candidate) | 111 um | 52.0 ohm | **94 um** | 50.3 ohm |
| 5880, 0.4 mm | 1.0 mm / 300 um | 111 um | 56.6 ohm | 71 um | 50.4 ohm |
| 5880, 0.4 mm | none (open top) | 111 um | 62.4 ohm | 57 um | 50.4 ohm |
| 5880, 0.4 mm | 0.5 mm / 40 um (the old default) | 111 um | 23.5 ohm | refused | |
| TMM 10i, 0.2 mm | 0.5 mm / 40 um (the old default) | 271 um | 27.6 ohm | refused | |
| TMM 10i, 0.2 mm | 1.2 mm / 300 um (the new default) | 271 um | 50.7 ohm | 247 um | 50.1 ohm |
| TMM 10i, 0.2 mm | 1.0 mm / 200 um (B18) | 271 um | 47.6 ohm | refused | |

B17 is the HFSS side of the first three rows: the candidate at the old fixed 111 um gap, relief
200 and 300 um, read S11 -14.4 dB and -12.5 dB on average in 2-10 GHz. The field solve has the
line going from 52.0 to 56.6 ohm between them. The default lid on 5880 (23.5 ohm) read -4.8 dB.

**Which one is wrong, the field solve or the old formula?** Three solvers on the same open-top,
35 um geometry (ohm); `base_parts/cpw_bem_check.py` has no grid at all (charge on the conductor
surfaces, the exact Green's function of the slab on its ground plane), and
`tests/test_cpw_field_check.py` keeps all of this:

| Board, trace, gap | Boundary element (no grid) | Finite element | New formula | Old formula |
|---|---|---|---|---|
| 5880, 0.4 mm, 111 um | 62.6 | 62.4 | 61.8 | **50.0** |
| 5880, 0.5 mm, 127 um | 59.7 | 59.5 | 58.9 | **50.1** |
| TMM 10i, 0.2 mm, 271 um | 55.0 | 54.9 | 54.5 | **50.0** |
| 5880, 0.4 mm, 111 um, 0.5 um metal | 71.2 | 70.9 | 71.2 | 71.5 (the old formula's thin limit) |

With thin metal all four agree to 0.5 %, so the backing plane, the finite ground width and the
substrate are not the difference; the two independent numerical solvers agree with each other to
0.3 % with 35 um copper, and the old formula is 20-25 % away. The disagreement is the
conductor-thickness term alone: the old correction widens a 0.4 mm trace by 83 um and
narrows a 111 um slot by as much, a first-order formula applied where the correction is three
quarters of the slot (it refuses a 57 um gap outright, "closes the slot"). The new term is the
parallel-plate field between the sidewalls, which a solve of a thick conductor must contain.
**AEDT's own 2D Extractor settles it** (`base_parts/cpw_q2d_check.py`, run 2026-10-11; the copper as a
solid under the lid and its channel, Z0 = sqrt(L/C) of the exported matrices, 8 seconds a case):

| Q2D | Finite element | Formula | Case |
|---|---|---|---|
| **62.39** | 62.44 | 61.77 | 5880, 0.4 mm, 111 um gap, open top (the old solve called this 50 ohm) |
| 51.99 | 52.03 | 51.75 | 5880, 0.4 mm, 111 um gap, lid 1.0 mm x 200 um (B17) |
| 50.29 | 50.29 | 50.00 | 5880, 0.4 mm, **94 um** gap, lid 1.0 mm x 200 um |
| 56.52 | 56.55 | 56.10 | 5880, 0.4 mm, 111 um gap, lid 1.0 mm x 300 um (B17) |
| **54.55** | 54.87 | 54.47 | TMM 10i, 0.2 mm, 271 um gap, open top (the old solve called this 50 ohm) |
| 50.15 | 50.13 | 50.00 | TMM 10i, 0.2 mm, **247 um** gap, default lid 1.2 mm x 300 um |
| 23.78 | 23.51 | 24.75 | 5880, 0.4 mm, 111 um gap, old default lid 0.5 mm x 40 um (B13) |
| 50.38 | 50.38 | 50.00 | 5880, 0.5 mm, **106 um** gap, default lid 1.2 mm x 300 um |
| 50.17 | 50.23 | 50.01 | 5880, 0.45 mm, **129 um** gap, lid 1.0 mm x 200 um (B23) |

AEDT agrees with the finite-element solve to 0.5 % or better everywhere (1.1 % on the 40 um lid), and
puts the old solve's open-top "50 ohm" gaps at 62.4 and 54.6 ohm. The gaps the new solve gives
(bold) are 50.15-50.38 ohm in AEDT. The numbers are pinned in `tests/test_cpw.py`.

Direction of the change: a cover lowers Z, so under the new model it needs a *wider* gap than
the same line open top, and it does (57 um open top, 71 um under a 300 um relief, 94 um under
200 um). The old 111 um is wider than all of them because the old open-top number was too wide
by the thickness error. What B17 had was a 52 ohm line (200 um relief) and a 56.6 ohm line
(300 um) by the field solve, 51.8 and 56.1 ohm by the formula; the old model's own belief was 50.

**2. The default lid cannot carry a 50 ohm line.** With a ceiling 40 um over the trace the
air side is so large a capacitance that no gap reaches 50 ohm: a 0.2 mm trace on TMM 10i
is 27.6 ohm at 271 um and tops out under 32 ohm however wide the gap, a 0.4 mm trace on 5880
under 27 ohm. It needs a relief at least 183 um deep (TMM, 0.2 mm) or 118 um (5880,
0.4 mm) before a usable slot (up to two substrate thicknesses, 0.76 mm) can reach 50 ohm at all. So the old default was a mismatched line
on both boards, and the layout check refuses it. `default.txt` now has `route_width` 1.2 mm and
`route_clearence` 300 um: the default board's gap is 247 um (CPW 0.69 mm, walls 0.25 mm clear of it).
Every number above this section in this file is for the old default lid (0.5 mm / 40 um) unless
it says otherwise, and for the old gaps.

**3. The chip-board runs B19 and B20 had a broken die.** The die's CPW gap is solved on the silicon
for the trace, and the trace was the board's 0.4 mm. A 0.4 mm trace on the 0.381 mm silicon die is
no CPW: its impedance levels off at 50.9 ohm at a 100 mm gap, so the bisection found a root at
28.8 mm and the die's `copper.txt` has `track|top|-3|0|3|0|0.4` with ground regions from
|y| = 3 to 29.5 mm (`region|top|-3,-29.50118,3,-29.50118,3,-3,-3,-3` and its mirror): no ground
beside the line, and a metal sheet reaching 26 mm past the die into the board, lid and walls.
**run_20261010_093710 (B19) and run_20261010_102243 (B20), and every number quoted from them
("with the chip": 100 / 100 / 100 / 54 %, 99 % of 2-20 GHz and 61 % of 20-26 GHz, S11 -12.2 dB,
the eigenmodes with the chip) are for that broken die and are not the candidate.** The bare-board
runs (B13-B18) have no die and are not affected. With a 0.2 mm trace (the die's line until then) the
gap was 0.17 mm and fine.
The fixes (this branch): the solve looks no wider than two substrate thicknesses and refuses
what only a wider slot reaches (the 29 mm root is now "unreachable"); the die's line has its own
trace, `chip_trace_width` (0.2 mm by default, so nothing changes for a board that already used
0.2 mm; the Altium generator, the bond pads and the die's gap ports use it, and the bond wires
run from the board's signal pads to the die's); and the layout check refuses a die trace that is no
CPW on its silicon, or a die with no room for the ground bonds beside its line.
The Altium script change (three lines, `ChipTrace`) was run: the 0.4 mm board with a 0.2 mm die
gets `track|top|-3|0|3|0|0.2` and grounds from |y| = 0.269 to 3 mm on the die, a 0.4 mm track and
0.2 mm signal pads (y = +-0.1) on the board, 0.1 mm signal pads (y = +-0.05) on the die.

**B22 and B23, the chip board with the die fixed** [run_20261010_232950, run_20261011_021452] (5880, lid
1.0 mm x 200 um, pedestal 0.5, corner screws on, B19's settings: 2-26 GHz, 97 points, delta S 0.05,
4 passes, connector plane, perfect-E box). **B22**, the candidate itself (0.4 mm trace, 94 um gap), did not
build (see the 100 um minimum below) and nothing was solved. **B23** has a 0.45 mm board trace (129 um gap,
the die's trace 0.2 mm with a 169 um gap): clean (S21 above -3 dB) for **100 / 100 / 85 / 12 %** of 2-10 / 10-17
/ 17-20 / 20-26 GHz; S11 averages -14.4 dB in 2-10 GHz (worst -7.0), -9.0 dB in 10-17 GHz and -11.9 dB in
17-20 GHz; S21 never falls below -2.6 dB under 17 GHz. What is not clean: a notch at 17.75 GHz (-7.4 dB), a
broad dip from 20 to 22 GHz (down to -5.5 dB), and 23-25.75 GHz (-4 to -12.6 dB, with -38.8 dB at the 26.0 GHz
end point). B19, the same lid with the broken die and a 0.4 mm trace, read 100 / 100 / 92 / 52 %: with a real
die the 20-26 GHz band is 12 % clean, not 54 %, and 17-20 GHz is no better. (The traces differ, 0.45 against
0.4 mm, so this is the die's effect and the 129 um gap together; the candidate with the die was not run.) So
the "clean to 20 GHz" of the box study holds for the bare board; with the chip, S21 is above -3 dB to
17 GHz and mostly to 20 GHz, not above.

**The 100 um minimum gap (`MIN_CPW_GAP`) is a model-build limit, and the candidate is below it.** Solved under
its lid the candidate (0.4 mm, 1.0 mm / 200 um) has a 94 um gap, so `check_layout` refuses it, and it
should: built with the limit relaxed (B22, the candidate with the chip and the die fixed),
AEDT would not unite the board's copper ("would not unite the pieces of 'pcb_copper_top'"), the
pieces were left with no conductor material, and the model's own validation refused it (overlapping
bodies, an empty vacuum; nothing was queued). Rebuilding it from the same files gave the same failure
every time; the same layout with the 111 um board's copper (B19's) built, and so did a 129 um gap
(B23, above). Swapping parts of the 94 um layout for the 111 um one narrowed it to the ground-plane
regions around the launch (the strap behind the connector and the two planes: the only regions that
differ); the vias, the bond pads, the die and the lid are not it. Why a 94 um slot defeats the boolean
there is not known (nothing in the region list sits within a micron of anything else). Copper left
in pieces is a line with no metal in it, which is very likely what the 86 um board of B12 and B15 was:
S11 0 dB and S21 -91 dB were a model with no conductor, not a mismatch (its line was 37.9 ohm by the field
solve, and the 23.5 ohm board of B13 transmitted). So the limit is not only a fab number: it stays at
100 um, and the buildable 5880 boards are the ones whose gap is above it: for a 1.0 mm / 200 um lid a
0.45 mm trace (129 um; 0.42 mm gives 106 um, 0.44 mm 120 um), for the default 1.2 mm / 300 um lid a 0.5 mm trace
(106 um). The threshold between 94 and 106 um has not been located.

**The rules the layout check now applies** (`constraints.check_layout`, messages name the number
to change):
- The relief has to be deep enough for the trace to reach 50 ohm at a slot of at most two substrate
  thicknesses (0.76 mm): `route_clearence` at least 118 um for the candidate, 183 um for the default
  board.
- The channel has to be at least trace + 2 gaps + 2 x max(0.1 mm, depth/2) wide, the gap being
  the solved one. A wall over a slot pulls the line down by up to a third (field solve, 50 um over the
  slot edge); out at that clearance it moves it under 1.5 %. The candidate needs 0.79 mm, the
  default board 0.99 mm. This replaces nothing: the old rule (channel at least 0.1 mm wider than
  the trace, so the lid does not short it) is still checked first.
- A relief of zero depth is refused (the lid would sit on the trace).

A sweep over `route_clearence` or `route_width` re-solves the gap for every combination, so
each one stays 50 ohm instead of drifting off it (B7 and B17 varied the relief at a fixed gap,
so their combinations were not the same line). The chip's line stays open-top: it sits
in the lid's pocket over the die, whose ceiling is not one of the relief parameters, and its
200 nm film is a sheet.

## Box study, where it stands (written 2026-10-10)

**The answer so far.** Clean S21 up to 20 GHz, and more than half of 20-26 GHz, is
reachable: RT/duroid 5880 instead of TMM 10i, `cpw_trace_width` 0.4 mm,
`route_width` 1.0 mm, `route_clearence` 200 um, everything else default (the full table
is in the lid cover section above). With the chip (B19): S21 above -3 dB for 100 / 100 /
100 / 54 % of 2-10 / 10-17 / 17-20 / 20-26 GHz, S11 -12.9 dB on average in 2-10 GHz; without
it (B17) 100 / 100 / 100 / 75 %. On the default board (TMM 10i, 0.2 mm trace, the
default lid) the same bands were 80 / 50 / 1 / 0 % (bare) and 66 / 32 / 8 / 0 % (with the
chip). What caused each notch, in the order they were found:

| Notch | Cause | Lever |
|---|---|---|
| 3.7, 10, 13.2, 15.5, 17.1, 18.6 GHz and the like | box (air cavity) modes, within 0.03 GHz of eigenmodes, do not move with the substrate or the vias | board length and width |
| 16.75 GHz (TMM 10i) | ground strip between the CPW gap and the via fence | fence setback (-5 GHz per mm); gone on 5880 |
| about 25 GHz (TMM 10i) | via-lattice substrate mode, Q 550, -2.2 GHz per mm of via step | substrate permittivity; gone on 5880 |
| 2-10 GHz mismatch on 5880 | the lid covers the line (CPW 50 ohm solve ignores it) | wide lid channel, 200 um relief |
| 0.5 mm trace in the default lid, and 0.2 mm trace on 5880 | the lid shorts the trace; a gap under 100 um | now refused by the layout check |

**B20, the validation of the candidate with the chip** [run_20261010_102243] (**same broken die as B19**; both solves; 20 eigenmodes at
delta f 1 % and up to 8 passes; the driven sweep 2-26 GHz on a 0.1 GHz grid (241
points) at delta S 0.02 and up to 8 passes; fine windows at every mode, 1717 points in all).
The eigenmode step: the box modes are where they were with the loose settings (7.18, 9.95,
12.38, 13.17, 13.49, 15.12, 15.54, 16.94, 17.08, 18.58 and 19.27 GHz), and the chip adds
lower-Q modes (Q 370-860) at 3.21 (twice), 5.67, 8.72, 10.99, 13.74, 16.37 and 19.12 GHz;
20 modes reach only 19.3 GHz. The S-parameters: on the 0.1 GHz grid S21 is above -3 dB for
**99 % of 2-20 GHz and 61 % of 20-26 GHz** (100 / 99 / 98 / 62 % of the four bands, counting
the fine-window points too), against 100 / 100 / 100 / 54 % at 0.25 GHz and a looser mesh. So
"clean to 20 GHz" holds as "almost clean": the finer look finds three narrow notches at box
modes that the coarse grid stepped over, **16.944 GHz (-6.8 dB), 18.581 GHz (-7.3 dB) and
19.202 GHz (-9.5 dB)**, each within 6 MHz of an eigenmode (Q 1700, 10500 and 2500), and the
worst S21 from 2 to 10 GHz is -2.8 dB (at the 9.947 GHz mode). S11 averages -12.2 dB in 2-10
GHz (worst -3.9), -9.6 dB in 10-17 GHz and -10.6 dB in 17-20 GHz. In 20-26 GHz the notches
are a cluster from 24.6 to 25.8 GHz (-17 to -23 dB, and **-42.5 dB at 25.8 GHz**, deeper than
the -27 dB the coarse grid saw) and a weak one at 22.8 GHz (-3.8 dB); the rest of the band is
clean. Perfect-E walls exaggerate how deep and narrow box-mode notches are (see the
finite-conductivity section).

**Open questions, none settled by the runs so far**
1. Answered by B20: almost. Three narrow notches at box modes (16.94, 18.58, 19.20 GHz, down
   to -9.5 dB) show on the 0.1 GHz grid; the rest of 2-20 GHz stays above -3 dB. Whether the
   real box, which is not perfect E, makes them shallower is not checked (question 3).
2. The cluster of notches from 24.6 to 25.8 GHz (-42.5 dB at 25.8 GHz in B20) is not
   explained on 5880: no eigenmode run was made above 20 GHz for it.
3. The box walls were perfect E throughout. Depth, and with it the band's cleanliness, was
   not checked with a finite-conductivity box (1e8 S/m) on the candidate; on TMM 10i that
   changed Q by 1.3-2.5x and positions by about 1 %.
4. Settled in code (see "the cover-aware gap"): the CPW solve now includes the lid cover, and the
   candidate's 0.4 mm trace at 1.0 mm / 200 um gets a 94 um gap, 17 um narrower than the 111 um the
   runs above used. Whether HFSS prefers it was not run when this was written (see that section).
5. The 30 mm bare board gave an identical S11 and S21 at all 97 frequencies; the cause was
   not found (it was not the lid channel width, which is a trace + 0.3 mm there, and the
   saved model looked sound).
6. Tolerances: one combination per setting was run; a trace width, a permittivity or a relief
   depth a little off was not tried. The trace widths either side of 0.4 mm that were run
   (0.2 and 0.5 mm) failed only for the reasons found: the 0.2 mm trace's 86 um gap is under
   the 100 um limit, and the 0.5 mm trace failed only with the default 0.5 mm lid channel
   (with a 0.8 mm channel it transmits: 28 / 79 / 100 / 0 %, B15).
7. Board length 60 mm on the default lid cleaned 29 % of 20-26 GHz; it was not tried with
   the wide lid on 5880.

## What to do next

0. Run the bare board (`no_chip`, the "Bare board" tick on the form). It
   builds the PCB and connectors only: a straight 50 ohm CPW from one SMP to
   the other, with no die, opening, pedestal, bond pads or wire bonds. That
   separates the board and connector from the chip. First thing to check
   with it: the unexplained -3 dB at about 2.1 GHz on every 50 x 25 driven
   run. If it is still there with no chip, it comes from the board, the box
   or the connectors, not from the die or its bonds. Use the `connector`
   reference plane; the `bonds` and `chip` planes need a die.
0b. The corner screws (`screws_on`, on by default) put a metal post in each
   corner of the cavity, so they should move the box modes. If the 2.1 GHz dip
   or the box modes matter, run once with `screws_on = 0` for the same board to
   see by how much.
1. Simulate a smaller board, for example 26 x 10 mm. Every run here used
   50 x 25 mm or larger, and size is the strongest lever found.
2. Repeat the via-spacing sweep on the symmetric lattice, 0.1-20 GHz, and
   include 0.5-1.2 mm.
3. ~~Sweep distortion on the current design.~~ Dropped 2026-10-22: the
   distortion option was removed from the project.
4. Run the wire-bond comparison again with the sweep reaching past 17 GHz.
5. Pick runs by dip position, not dip depth.
6. **Re-run the pair `0929_102232` / `1005_130249` with fine windows, to
   test whether the depth scatter was under-sampling.** Solve it with
   *Both, on the same geometry* (the same setup twice, as two pairs) so the
   Driven run gets a window around each eigenmode below its stop, with
   `fine_windows` on `auto` and the defaults (half-width f/(2Q), step
   f/(20Q)). If under-sampling was the cause, the narrow dips should now
   agree in depth between the two repeats to within a few dB; if they
   still differ by tens of dB the cause is somewhere else -- the mesh
   (try *Adapt the mesh at* the top of the sweep and a lower *Max delta S*)
   or the interpolating sweep itself. Compare the two with the comparison
   page. The same pair, with *Perfect E on box/lid_inside* off, gives the
   dip depth a lossy wall would show.

   **New:** one run now solves both the eigenmodes and the S-parameters of
   each combination, with fine windows around each mode, the modes drawn on
   the S21 plot, and a *Refine a frequency range* section on the run's page
   for narrower re-solves (all part of the same run). See the README,
   "Eigenmode and Driven together". Checked on AEDT with one combination:
   20 modes, 12 fine windows, 2365 merged points; the fine windows resolved a
   very sharp step near 15.8 GHz that the broad sweep only showed as a jump.
   Refining 16.6-17.1 GHz and 15.75-15.9 GHz afterwards (151 and 251
   points): the 16.87 GHz dip came back at the same place (5 MHz) and depth
   (-90.6 vs -92.8 dB), while the sharp step near 15.8 GHz moved by about 50 MHz
   against the run's own fine window -- a mesh-sensitive feature, so dip depth
   and position of the very sharp ones are still not something to compare
   across solves.

## Geometric approximations to know about

- **Screw keep-out in the copper.** Altium regions cannot hold an interior
  hole, so each ground plane (top and bottom) gives up a square round every
  screw, running out to the board's corner (the keep-out diameter plus 1 mm),
  and four pieces with the keep-out circle bitten out are drawn back. The
  copper is whole and the circle is empty, with no slot to the edge. The
  circle is a polygon of 8 steps per quarter (about 0.3 % under the true
  area), the same kind of approximation as the connector's anti-pad arcs.
- **Screw thread.** The box's tapped hole is modelled at the screw's own
  diameter, not the 1.6 mm tap drill, and the thread is not modelled.
- **Field exports** on the student licence fail above its mesh limit; see the
  README.

## Defaults changed

| Default | Was | Now | Why |
|---|---|---|---|
| `pedestal_depth` (`base_parts/default.txt`) | 0.0 mm | 0.5 mm | Best 15-20 GHz result in the sweep, tied with 0.25 mm. Least certain of the lot. |
| Driven sweep stop (`f_stop_ghz`, web form and `--f-stop-ghz`) | 15 GHz | 20 GHz | Target is above 20 GHz. Every 15 GHz run ends inside a dip and cannot see the band. |
| `pcb_length` x `pcb_side` | 26 x 10 mm | 50 x 25 mm | Your choice. Every driven run here used this board. Note that the data says a smaller board raises the box modes. |
| `port_space` | 1.5 mm | 6.0 mm | Your choice of "bigger". See the note below. |
| Max delta freq per pass | 1 % | 3 % | Your choice. |
| Starting-frequency safety factor | 0.4 | 0.1 | Your choice. |
| Min adaptive passes | 5 | 3 | Your choice. |
| Min converged passes | 3 | 2 | Your choice. |

Modes stays 20.

**`port_space` and the vias.** The generator keeps every via exactly 0.2 mm
(`via_min_spacing`) from the connector's signal pad at `port_space` 1.5, 4,
5, 6 and 8 mm, so no value in that range puts a via on a pad. The copper
test `test_no_via_lands_on_a_bond_pad` used to report overlaps at 4, 5 and 8
mm. It compared vias against the pad's width and height in the pad's own
frame and ignored the quarter-turn the connector's lands have. It now takes
the extents from the rotated corners.

Left as they are, with the reason:

- `via_max_spacing` stays 0.8 mm. It is within the 0.8-1.0 mm span that
  pushed the deepest dip highest, but the evidence is from the old layout.
- `via_diameter` stays 0.3 mm. Nothing in the 0.2-0.5 mm range mattered.
- Wire bonds stay on. They are physically there and the effect is small.
