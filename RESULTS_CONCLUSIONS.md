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
3. Sweep distortion on the current design. The old result says the effect is
   large.
4. Run the wire-bond comparison again with the sweep reaching past 17 GHz.
5. Pick runs by dip position, not dip depth.

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
