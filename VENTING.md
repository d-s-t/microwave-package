# Venting: every closed air pocket needs a way out

The package goes into a vacuum chamber and a cryostat. A closed pocket of air
outgasses slowly, or keeps its pressure and pushes on a seal when the chamber
is pumped down and when the box warms up. Rule: wherever there is an air
pocket there is an exhaust route. This is the audit of the box + lid + PCB +
screws + SMP connectors as `default.txt` builds them, and the vents added.
The numbers are read off the exported `box` and `lid_inside` STEP files of the
default board (axes: x along the feedline, y up from the box underside, z
across it; the 5880/TMM defaults are not what matters here, the shapes are).

## Audit

| # | Volume | Size (default) | Closed? | Route out |
|---|--------|----------------|---------|-----------|
| 1 | Lid relief channel over the trace | 1.0 wide x 0.2 deep (0.4 deep in its last mm, x 12.9 to 13.9), x 3.75 to 13.9 each side | no | Yes, and it does reach the connector holes: the channel ends at x = +-13.9, which is exactly where the 5.1 x 5.1 mm SMP hole starts (x 13.9 to 19.0, z +-2.55), so channel and hole share a face and the channel opens into the hole over z +-0.5 |
| 2 | SMP holes in the lid | 5.1 x 5.1 x 1.5 (through) | no | Open to the top round the SMP barrel (barrel radius at most 2.35 against a half-width of 2.55: 0.2 mm at the sides, more at the corners) |
| 3 | Chip pocket in the lid | 7.5 x 7.5 x 0.4 (die + `bonding_clearence` each side) | no | It is the channel's inner end |
| 4 | Chip cavity (ring round the die in the PCB opening) | 0.1 mm ring, 0.381 deep | no | Open at the top into the lid pocket (3) |
| 5 | Pedestal recess in the box floor under the die | 6.3 x 6.3 x 0.5 less four 0.8 mm corner legs, about 19 mm3 | **yes** | None: the die lies over it (die edge = recess edge) and the PCB rests on the floor round it. **Vented by the pedestal vent** |
| 6 | Lid clearance bore + countersink, PCB hole, box tap hole at each screw | bore 2.3 dia through the lid and PCB, tap 1.6 dia through the 3.0 mm floor | no, but weak | The tapped hole goes right through the floor (not blind), so gas can leave down the thread. But the screw tip is flush with the underside, which is clamped to the cold plate, so the path is a thread and a closed flat face. **Given a real route by the screw vents** |
| 7 | Gap between lid_inside and box wall | none in the model (lid is the cavity size) | n/a | Real parts have a fit clearance, which is open to the top |
| 8 | 35 um copper steps under the lid, film between board and floor | micron-scale | virtual leaks, not pockets | Where it matters (the CPW slots) the relief channel covers them |
| 9 | Plated vias | 0.3 mm barrels through the board | yes, when the box is assembled | **Pending PI decision (fill, cap or vent). Not handled here.** |
| 10 | Distortion pockets (`no_distortion` = 0), removed 2026-10-22 | was an experimental geometry: blind pockets in the box under holes in the PCB | blind pockets (no route out) | Not applicable: the option is gone from the project and no run builds it. If it is ever reintroduced it needs the same treatment as 5 |

## The vents

`vents_on` (default 1), `vent_width` (0.5 mm), `vent_depth` (0.3 mm), all in
`base_parts/default.txt`, in the web form, and drawn in
`base_parts/parameters_visual/vent_width.svg` and `vent_depth.svg`.

* **Pedestal vent:** a groove in the top of the box floor, along z through the
  middle of the recess (x = 0), through the wall, out of both side faces.
  Created only when `pedestal_depth` > 0 and the board is not a bare board (`no_chip` = 0): a bare board has no die and no opening in its bottom copper.
* **Screw vents:** from each tapped hole, along z to the nearest side face
  (3.5 mm and the wall away). Created only when `screws_on` = 1.

Why a groove in the floor's top face, out of the side: the underside bolts to
the cold plate, so a hole down is a hole into a clamped flat face; the side
face is free. The groove lies under the PCB's bottom copper, which is a ground
plane, so it is outside the field region. The groove is a rectangular guide
of 0.5 x 0.3 mm: cutoff c / (2 x 0.5 mm) = **300 GHz**, over ten times the
26 GHz band top. It is away from the CPW, the launch, the die and its bond
fingers (the screws are at 9 mm from the line). `design/venting.py` refuses a
vent under 0.3 mm (cannot be cut), with a cutoff under 5 x the band top (both
sides under 1.15 mm), or one that leaves under 0.5 mm of floor.

HFSS: `hfss/vents.py` cuts the same grooves into the imported box (a no-op if
the part already has them), and the model carries them through the STEP. Past
the board's edge the groove is inside the wall, where the vacuum body does not
reach, so in the solve it is a dead-end slot far below cutoff.

## What was added in SolidWorks (box.SLDPRT)

Made by `base_parts/add_vents.py` (SolidWorks automation, rerunnable on a
fresh copy of the part; it refuses if the features exist). Two features after
the final `box` mirror, each sketched on the cavity floor face, cut
`vent_depth` down:

* `Sketch6` + **`Vent_Pedestal`**: one rectangle, `vent_width` x (`box_side` + 0.4),
  centred on the origin. Suppressed by the equation
  `IIF(vents_on > 0.5, IIF(pedestal_depth > 0, IIF(no_chip > 0.5, 1, 0), 1), 1)`
  (the `no_chip` term was added after the first version; see the note below).
* `Sketch7` + **`Vent_Screws`**: four rectangles, `vent_width` wide, from the
  tap-hole centre line (`pcb_side/2 - screw_inset`) to `box_side/2 + 0.2`.
  Suppressed by `IIF(vents_on > 0.5, IIF(screws_on > 0.5, 0, 1), 1)`.

All sketch dimensions and the cut depth are equations of `default.txt`.
`vents_on`, `vent_width` and `vent_depth` were brought into the part by
unlinking and relinking its equation file (SolidWorks imports a file's variables only
when it links), and placed before the equations that use them (the list is
evaluated in order). The part's link now points at this checkout's
`default.txt`; the pipeline re-points it per checkout anyway.

Checked by rebuilding with different equation files: volume 4739.12 mm3 with
`vents_on = 0`, 4733.79 on (5.33 mm3 removed), 4731.64 at `vent_width` 0.7,
4730.23 at `vent_depth` 0.5; the exported STEP has the groove floors at
y = 2.7 from the recess edge (z = 3.15) to the side face (z = 13.5), the
screw grooves from the tap hole (z = 9.76) to z = 13.5, and the wall's roof
intact. `lid_inside.SLDPRT` is not changed.

## Do the vents change the electrical result? (HFSS, default board, vents off vs on)

Default board (chip, pedestal 0.5 mm, screws on, perfect-E box, connector reference plane),
driven 2 to 26 GHz in 97 points (4 passes, delta S 0.05) and the 10-mode
eigenmode, once with `vents_on = 0` (run_20261011_035254) and once with
`vents_on = 1` (run_20261011_135243). The vented model has 615 403 mesh
elements against 611 606. The unvented run's fine windows were lost, so S21
is compared on the 97-point coarse sweep only.

* **Modes:** the ten modes move by 0.1 to 6.7 MHz (largest 0.063 %, the 10.73 GHz mode,
  6.7 MHz down); Q changes by at most 0.43 %. Mode 5 and the 12.38 GHz mode, the
  high-Q ones, move by 0.3 and 0.1 MHz.
* **S21 / S11:** the share of points above -3 dB per band (2-10, 10-17, 17-20, 20-26 GHz)
  is 69 / 57 / 25 / 4 % both ways, and the mean S11 is -6.5 / -5.9 / -4.3 / -0.9 dB
  (off) against -6.4 / -6.0 / -4.2 / -0.9 dB (on). The median |dS21| is 0.05 dB and 62 of 97 points differ by under
  0.1 dB. The 12 points that differ by more than 0.5 dB are all on the flanks or at the bottom of the
  sharp dips (15.5 to 16.0, 20.5 to 23.0, 25.5 to 25.75 GHz); the worst is the 15.75 GHz dip, -19.2 against -12.1 dB,
  on a 0.25 GHz grid that has one point on a notch a few MHz wide.
* **Dips:** the dips below -3 dB are in the same places (6.75, 8.75, 12.75, 14.75, 15.75, 16.25,
  17.25, 19.25, 20.25-20.5, 22.0, 24.0, 25.5 GHz); on the 0.25 GHz grid the 20.25 and 22.0/22.5 dips resolve
  one step apart, as a few-MHz shift of a narrow notch does.

So the vents are electrically invisible within the model's mesh noise: no new mode, no new band of
loss, and the existing resonances move by under 0.1 %.

How the vented model is built, for whoever reruns it (all in `hfss/vents.py` and the importer):
the box body comes out of SolidWorks named after its last feature (`Vent_Screws`), so the importer
renames it to `box`; and the model draws the bottom copper 35 um down into the floor, so a groove
under it would leave the copper half over air (HFSS: "pcb_copper_bottom and box intersect"). The
importer gives the box back as a skin two copper thicknesses deep under the copper over each groove (from the die opening
to the wall for the pedestal vent; from just inside the keep-out circle to the wall for a screw vent).
The skin is model bookkeeping, not part of the real part, whose grooves are open to the board's underside.

**Pending, needs SolidWorks:** the `no_chip` term in `Vent_Pedestal`'s suppression is
in `add_vents.py` and `design/venting.py` but the committed `box.SLDPRT` still has the
first equation (`IIF(vents_on > 0.5, IIF(pedestal_depth > 0, 0, 1), 1)`). The HFSS side
does not depend on it (`hfss/vents.py` follows `venting.plan`, and the box's own groove
is only an unused tunnel on a bare board with a recess). To bring the part in line, with the
tools token: `python base_parts/add_vents.py --fix-pedestal-suppression` (written, not yet
run; if `Equation(i, text)` does not set through late-bound COM, edit the one equation by
hand in the Equations dialog: feature `Vent_Pedestal`).

## For manufacturing

* Mill the grooves before the box is anodised/plated; deburr the exit at the
  side face. Check an air path with a thin wire or compressed air before
  assembly.
* Vent holes in the lid are NOT needed: the relief channel to the SMP holes is
  the lid's route.
