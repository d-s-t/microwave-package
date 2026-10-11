# Before manufacturing: features, tests, and where the candidate stands

What has to be true of the package before a board and a box are ordered,
how each thing is checked, and which checks the candidate board has
passed. The automatic part is one command:

```
python -m microwave_package.preflight --candidate --out preflight_report.md
```

It evaluates every check that needs no simulation and no person, writes a
Markdown report, and lists the rest as `manual` with how to do each. It
exits 1 if any check fails. Without `--candidate` it checks the shipped
defaults; `--set NAME=VALUE` (repeatable), `--mat-pcb "..."`,
`--fab min_trace=0.1` (the fabricator's limits) and `--no-chip` change what
is checked. Run it again after every parameter change; a stale report is
worse than none.

Status words used below: **passed** (checked, meets the criterion),
**open** (not yet checked, or checked and not met), **blocked** (waiting
on something else).

## 1. What the design consists of

The candidate is the shipped defaults (`base_parts/default.txt`) with the
changes in bold. Anything not listed is a default.

| Feature | Value / rule | Where it comes from |
|---|---|---|
| Board | 50 x 25 mm, 381 um laminate, 35 um copper (1 oz) both sides | `pcb_length/side/width`, `copper_thickness` |
| Laminate | **Rogers RT/duroid 5880** (eps 2.2). Default was TMM 10i | `mat_pcb` (PCB materials in the new-run form) |
| Line | CPW, **trace 0.45 mm**, gap solved for 50 ohm under the lid (**129 um** on 5880, 1.0 x 200 um relief), ground below | `cpw_trace_width`, `design/cpw.py` |
| Via fence | 0.3 mm vias on a 0.8 mm lattice, fence 0.15 mm from the gap edge, drill 0.15 mm | `via_*` |
| Lid | 1.5 mm thick, sits on the ground copper; relief channel over the line **1.0 mm wide, 200 um deep** | `lid_width`, `route_width`, `route_clearence` |
| Box | aluminium, 1 mm walls, tapped floor 3 mm at the corners | `wall_width`, `screw_engagement` |
| Screws | 4 x M2 countersunk, 3.5 mm in from the board edge; 2.3 mm clearance holes in lid and board, 1.6 mm tap drill in the box, 3.0 mm copper keep-out | `screw_*` |
| Connectors | WR-SMP 60124102122403 at both ends, edge launch | `design/smp.py`, `base_parts/smp_connector_split.step` |
| Chip | 6.3 x 6.3 x 0.381 mm silicon die in a pocket (0.1 mm per-side clearance) on a 0.5 mm pedestal | `chip_*`, `cavity_clearance`, `pedestal_depth` |
| Wire bonds | 25 um, loop 0.2 mm, pads 0.3 x 0.15 mm at 0.4 mm pitch | `bond_*` |
| Chip line | niobium on silicon, 200 nm | `chip_metal_thickness`, `mat_conductor_chip` |

Rules the tool enforces on every run (`design/constraints.py`), so a layout
that breaks one is refused before anything is drawn:

- The CPW gap is solved, never typed, and must be at least **100 um**
  (the fab's etch limit); a trace that would give less is refused.
- `route_width` must be at least `cpw_trace_width` + 0.1 mm, or the lid
  shorts the trace. The preflight additionally warns when the channel does
  not clear the trace plus both gaps plus 0.2 mm.
- The via lattice has to be placeable (spacing, clearance to pads, tracks,
  other vias and the edge), and the fence has to fit between the line and
  the board edge.
- The lid must be thick enough for the countersunk head (1.14 mm head plus
  0.3 mm seat), and the thread in the box at least 1.5 x the screw.
- A bare board (`no_chip`) skips the chip, opening, pedestal and bonds.

## 2. Simulations that must pass

Each is run from the web app's new-run form (or the command line) and
read on the run page. "Pass" is the criterion; do not order a board on a
run that does not meet it.

| # | Simulation | How | Pass |
|---|---|---|---|
| S1 | **Line impedance, board** | Solution type *Line impedance*, *Which line* = board, the candidate's parameters. CLI: `python -m microwave_package.pipeline --solution-type LineImpedance --mat-pcb "Rogers RT/duroid 5880 (tm)" --sweep cpw_trace_width=0.45 --sweep route_width=1.0 --sweep route_clearence=0.2` | Zo inside 50 +- 2.5 ohm at every frequency 1-26 GHz (the verdict table on the run page); the HFSS-solved gap reaches 50 ohm with the lid on |
| S2 | **Line impedance, chip** | the same with *Which line* = chip (`--setting which_line=chip`) | the same |
| S3 | Gap against the analytic one | read from S1: *gap solved* vs *gap analytic* on the run page. To find the gap HFSS wants, run S1 at three gaps and interpolate (`gap_to_reach` in `hfss/line/impedance.py`) | analytic gap within the tolerance of what HFSS wants; if not, the formula is wrong for this stack-up and the solved gap must be corrected |
| S4 | Driven Modal with the chip, whole band | *Driven Modal*, *Both*, connector reference plane, 2-26 GHz on a grid of 0.1 GHz or finer, mesh as in B20 of `RESULTS_CONCLUSIONS.md` | S21 above -3 dB across the band the experiment needs; every notch accounted for by an eigenmode (S5); S11 read too |
| S5 | Eigenmodes cover the band | the *Both* run's eigenmode half, enough modes to pass the top of the band | each S21 notch within a few tens of MHz of a mode, or explained |
| S6 | Finite-conductivity box | untick *Perfect E on box/lid_inside*, `box_conductivity` about 1e8 S/m | the clean band survives the loss; notches shallower, not new ones |
| S7 | Fine windows | the *Both* run does them | no notch hidden between windows; grid finer than a mode's width f/Q |
| S8 | Tolerances | sweep `cpw_trace_width` (+-25 um), the gap, eps (+-3 %), `route_clearence`, `route_width` about the candidate | the band holds across the fabricator's stated tolerance |
| S9 | Venting (see section 6) | _to be written by the venting work_ | |

The line check is the cheap one: it solves a few millimetres of line, no
connectors and no SolidWorks, in minutes. Run S1 and S2 first and after
every change to the trace, the laminate or the lid; the full-package runs
(S4-S8) only when those pass.

Reading S1 and S2: the page shows a PASS/FAIL pill, the minimum and
maximum Zo over the band, the worst frequency, the gap solved and the
analytic gap, and where the geometry came from (the run's copper layout,
or re-derived from the parameters if it had none). Zo is derived from the
section's S-parameters by ABCD inversion against the port reference, and
the port's own Zo is shown beside it when HFSS reports one; which is which
is named on the page.

A simulated TDR (the driven run's S11 transformed to time) is not built
into the tool yet; the TDR in section 5 is a measurement, and the
line-impedance check is its frequency-domain referee.

## 3. Design-for-manufacture checks

Automatic ones are in the preflight (`fab.*`, `layout.*`, `mech.*`); the
fabricator's real limits replace the defaults with `--fab`.

| Check | Rule | Automatic? |
|---|---|---|
| Narrowest line | >= fab min trace (default 100 um) | yes |
| Narrowest gap | >= fab min gap (default 100 um) | yes |
| Via drill and aspect ratio | drill >= fab min (150 um); board thickness / drill <= 8:1 | yes |
| Annular ring | (via - drill) / 2 >= 75 um | yes |
| Via and pad spacing | `via_min_spacing` >= fab min space | yes |
| Bond pad size | pad >= fab min pad and the pad the bonder needs | yes |
| Screw holes and thread | clearance, tap drill, 1.5 x diameter engagement | yes |
| SMP footprint | land pattern against the datasheet drawing of the exact part number, checked in the fab's CAM | **manual** |
| Surface finish | soft gold or ENEPIG on bond pads (ENIG's nickel bonds poorly) | **manual** |
| Via fill or cap | see section 6 | **manual** |
| Mechanical fit | holes align across lid, board and box; lid flat on the ground copper; nothing holds it off | **manual** |
| Fab capability | confirm the fab can hold the 129 um gap, the 150 um drill and 5880's lamination and drilling | **manual** |
| Gerber / drill review | view the actual fab output, not the Altium file | **manual** |

## 4. Cryogenic checks

The package is used cold, so room-temperature behaviour is necessary and
not sufficient.

- **Laminate expansion.** RT/duroid 5880 expands about 237 ppm/degC through
  the thickness against copper's 17, which works the plated via barrels on
  every thermal cycle. The preflight warns for it. Either obtain the
  fabricator's cryogenic via-reliability data for 5880, or use a
  copper-matched laminate (RO3003, RO4003, TMM 10i); the candidate's
  0.45 mm trace and 1.0 mm lid channel were found for 5880 and would have to
  be re-found for another laminate.
- **Aluminium box.** Superconducting below about 1.2 K. Perfect E is the
  default wall model and exaggerates how deep box-mode notches are
  (positions are right); run S6 for the depth.
- **Contraction and clamping.** Aluminium, laminate and steel screws shrink
  differently: check that the lid stays pressed on the ground copper cold
  and that the board does not buckle between screws. Dimensions are the
  room-temperature ones.
- **Permittivity at temperature.** The dielectric constant shifts when
  cold; modes move with it. The cold measurement (section 5) is the check.
- **Thermalization.** The board's heat path to the cold plate is through
  the box floor and the screws; the chip line's superconductor and the
  bond wire material (aluminium or gold) set what dissipates.
- **Thermal cycling.** At least a few cycles room temperature to base
  before the real cooldown; look for lifted bond wires, cracked vias,
  loosened screws.
- **Connectors.** SMP and its solder joints through cooldown.

## 5. Measurement plan

1. **Incoming inspection.** Visual, board thickness, trace width and gap
   against the drawing (microscope), via continuity.
2. **Room temperature, in the box.** VNA S11 and S21, 1-26 GHz, calibrated
   to the connector plane; compare with S4. A **TDR** of the line:
   impedance along it should sit at 50 ohm +- the tolerance of S1, with
   only the connector and the chip transitions visible. A line that
   passes S1 and fails the TDR is a fabrication problem (etch, laminate
   thickness, eps), not a design one.
3. **Thermal cycling** to base (section 4), a room-temperature check
   after.
4. **Cold.** The same S-parameters at base temperature, compared with the
   room-temperature set: modes move with eps; if the shifts are not those
   of S8, find out why before trusting the chip results.
5. **Chip.** With the chip bonded, S11 and S21 at the chip plane
   (de-embedded) against the S4 chip-plane run.

## 6. Open points that are not settled

- **Vias: solid posts or drilled holes.** The HFSS model draws the vias as
  solid posts; the Altium board has drilled holes. Whether they are
  filled or capped, and what that does to the pockets of trapped air in
  a cold, evacuated or sealed package, is pending the user's PI. The
  simulations do not currently tell the two apart.
- **Venting.** _Reserved. Another session is adding vents; their section
  goes here: where air can leak out of the box and the cavities, the vent
  geometry, and the simulation S9 that checks the vents do not open a
  path for the box modes._
- **Finite-conductivity run on the candidate (S6)** has not been made.
- **Lid-aware analytic gap.** `design/cpw.py` now solves the gap under the
  lid (PR #64) and the die's line on its own `chip_trace_width`. S1 agrees
  with it to within the check's accuracy; S1 and S2 remain the referee.

## 7. The candidate: status

As of 2026-10-11. Preflight: 0 fail, 1 warn, 14 pass, 13 manual.

| Item | Status | Notes |
|---|---|---|
| Layout builds, vias placeable, geometry fits | **passed** | automatic |
| Gap >= 100 um (129 um) | **passed** | automatic; a 0.4 mm trace under the same lid solves to 94 um and is now refused |
| Lid channel clears the trace | **passed** (preflight) / **open** (HFSS) | analytic margin only; S1 is the real check |
| Fab limits (trace, drill, ring, spacing, pads) | **passed** against the defaults | **open**: confirm against the real fab's limits |
| Screws, countersink, thread | **passed** | automatic |
| Cryo: 5880 z-axis expansion | **open** | warning; fab data or change laminate |
| S1 line impedance, board | **passed** (2026-10-11, run_20261011_125037) | Zo 50.5-51.6 ohm over 1-26 GHz, inside 50 +- 2.5; the gap Altium drew, 128.85 um, against the analytic 128.8 um. HFSS reads 1.4 ohm above the 2D Extractor's 50.17 ohm (0.9 ohm with a 6 mm section): that is the check's own accuracy |
| S2 line impedance, chip | **open** | the chip gap bug is fixed (PR #64: `chip_trace_width` 0.2 mm gives 169 um on silicon); not yet run |
| S4/S5/S7 driven + eigen + fine windows, with chip | **passed on the previous model** | B20: S21 above -3 dB for 99 % of 2-20 GHz and 61 % of 20-26 GHz; narrow notches at 16.94, 18.58 and 19.20 GHz; redo after the chip-gap fix |
| S6 finite conductivity | **open** | not run |
| S8 tolerances | **open** | one combination per setting so far |
| S9 venting | **open** | section 6 |
| Via fill/cap | **open** | section 6 |
| SMP footprint, finish, fits, fab capability, Gerber review | **open** | manual |
| Room-temperature, cold and TDR measurements | **open** | after manufacture |

Update this table, not just the report, when a row changes.
