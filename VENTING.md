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
| 10 | Distortion pockets (`no_distortion` = 0) | experimental geometry | blind pockets in the box under holes in the PCB | Not vented: an experiment, not a build. If it is ever built, it needs the same treatment as 5 |

## The vents

`vents_on` (default 1), `vent_width` (0.5 mm), `vent_depth` (0.3 mm), all in
`base_parts/default.txt`, in the web form, and drawn in
`base_parts/parameters_visual/vent_width.svg` and `vent_depth.svg`.

* **Pedestal vent:** a groove in the top of the box floor, along z through the
  middle of the recess (x = 0), through the wall, out of both side faces.
  Created only when `pedestal_depth` > 0 and not `no_chip`.
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

## For manufacturing

* Mill the grooves before the box is anodised/plated; deburr the exit at the
  side face. Check an air path with a thin wire or compressed air before
  assembly.
* Vent holes in the lid are NOT needed: the relief channel to the SMP holes is
  the lid's route.
