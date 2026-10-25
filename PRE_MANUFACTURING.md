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
- The ground beside the SMP signal pad stays at least **100 um** from it.
  The pad (0.48 mm) is wider than the trace, so a ground edge that followed
  the trace would leave only gap - (0.48 - trace) / 2: 71 um for a 0.4 mm
  trace with a 111 um gap, 54 um with 94 um. The layout pulls the ground
  back to `launch_clearance` (default **0.2 mm**) off the pad and that far
  past its end, keeps the via fence out of the notch, and refuses a
  `launch_clearance` under 100 um where the gap alone would leave less.
  HFSS (TMM 10i, bare board, 120 / 200 / 300 um, 2-26 GHz): below 10 GHz the
  three give the same S11 (mean -6.9 dB) and S21 (within 0.2 dB), so 0.2 mm
  was kept for the margin over the fab floor; 300 um is slightly worse above
  18 GHz (mean S11 -1.35 against -1.06 dB).
- `route_width` must be at least `cpw_trace_width` + 0.1 mm, or the lid
  shorts the trace. The preflight additionally warns when the channel does
  not clear the trace plus both gaps plus 0.2 mm.
- The via lattice has to be placeable (spacing, clearance to pads, tracks,
  other vias and the edge), and the fence has to fit between the line and
  the board edge.
- The lid must be thick enough for the countersunk head (1.14 mm head plus
  0.3 mm seat), and the thread in the box at least 1.5 x the screw.
- A bare board (`no_chip`) skips the chip, opening and bonds. It may still carry a pedestal recess (`pedestal_depth` > 0) under its solid bottom copper; the model carries that copper over the recess on a skin of the box, so the air pocket sits under the board.

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

Accuracy of the check: against AEDT's 2D Extractor (50.17 ohm for the
candidate) the 3-D section reads 1.4 ohm high with a 3 mm section and 0.9 ohm
high with a 6 mm one, so the offset depends a little on the section length
and the ports. It is well inside the +- 2.5 ohm tolerance, which is why that
is the default; read a result near the edge of the window with it in mind.
The section is kept under a half-wavelength at the top of the band (3 mm):
at a half-wavelength the impedance cannot be derived from S-parameters, and
those points are dropped.

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
| Ground to SMP signal pad | >= 100 um (`launch_clearance`, default 200 um) | yes |
| Via drill and aspect ratio | drill >= fab min (150 um); board thickness / drill <= 8:1 | yes |
| Annular ring | (via - drill) / 2 >= 75 um | yes |
| Via and pad spacing | `via_min_spacing` >= fab min space | yes |
| Bond pad size | pad >= fab min pad and the pad the bonder needs | yes |
| Screw holes and thread | clearance, tap drill, 1.5 x diameter engagement | yes |
| SMP footprint | land pattern against the datasheet drawing of the exact part number, checked in the fab's CAM | **manual** |
| Surface finish | soft gold or ENEPIG on bond pads (ENIG's nickel bonds poorly) | **manual** |
| Via fill or cap | copper-filled and planarised (decided 2026-10-19); fab to confirm the fill process | **manual** |
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

- **Vias: decided 2026-10-19: copper-filled, plated shut, planarised.**
  The user chose to fill every via with copper, the same material as the
  via barrel, so the board has no open via bores. The HFSS model already
  draws the vias as solid posts, so the simulation needs no change. The
  fab note is in the fab drawing (note 4), in the preview README and in the
  Altium supplement note (`workflow/manufacturing.py`). The Altium output
  itself still needs its via fill set (see the table in section 7).
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
| S2 line impedance, chip | **passed** (2026-10-18, run_20261018_144211, final default: formula gap from Altium's copper) | TMM 10i, `cpw_trace_width` 0.20, default lid 1.2 x 0.3 mm, `chip_trace_width` 0.2, `which_line=chip`, 2 mm section, 1-20 GHz (the goal band). The gap Altium drew is 119.03 um (`chip_cpw_gap`, from `cpw.solve_chip_gap_for_z0`). Zo mean 50.11 ohm, 49.51-50.47 ohm over 1-20 GHz: inside 50 +- 2.5 at every point. The earlier analytic gap of 168.69 um read 54.6 ohm and failed (run_20261018_135026); see S3 |
| S3 chip gap against the analytic one (2026-10-18) | **formula corrected; analytic gap was 28 % too wide** | HFSS reads 50 ohm at **121 um** (run_20261018_135800, mean 50.48 ohm, PASS). The open-top formula gave 168.69 um. The formula now scales it by `CHIP_HFSS_IMPEDANCE_FACTOR` = 1.079 (empirical, see the note below the Zo table), which gives 119.03 um for the default die. Check on a second width: `chip_trace_width` 0.25 at the formula's 176.6 um reads 50.58 ohm, 49.78-51.16 ohm, **PASS** (run_20261018_143910). Physical terms (lid and air under the die) were tried against a 2-D field solve first and do not reproduce HFSS; see the note |
| S4/S5/S7 driven + eigen + fine windows, with chip | **passed on the previous model** | B20: S21 above -3 dB for 99 % of 2-20 GHz and 61 % of 20-26 GHz; narrow notches at 16.94, 18.58 and 19.20 GHz; redo after the chip-gap fix |
| S6 finite conductivity | **open** | not run |
| S8 tolerances | **open** | one combination per setting so far |
| S9 venting | **open** | section 6 |
| Via fill/cap | **decided 2026-10-19: copper-filled, planarised** | section 6; the Altium output's via fill still to be set and confirmed with the fab |

**Chip line Zo against gap (S2, S3), 2026-10-18.** 1-20 GHz, 2 mm section, `cpw_trace_width` 0.2, `which_line=chip`. The mean is not smooth in the gap (neighbouring steps differ by 0.05 to 0.14 ohm per um), so treat each mean as +-0.5 ohm.

| gap (um) | run | geometry | mean (ohm) | min-max (ohm) | verdict |
|---|---|---|---|---|---|
| 168.69 (analytic) | run_20261018_135026 | copper layout | 54.60 | 53.84-55.86 | FAIL |
| 155 | run_20261018_135452 | forced, from parameters | 52.98 | 52.39-53.56 | FAIL |
| 145 | run_20261018_135932 | forced | 52.34 | 51.77-52.82 | FAIL (max) |
| 140 | run_20261018_135330 | forced | 51.38 | 50.77-51.95 | pass |
| 125 | run_20261018_135208 | forced | 50.57 | 49.99-50.98 | pass |
| **121 (confirm)** | run_20261018_135800 | forced | **50.48** | **50.02-51.43** | **pass** |
| 119.03 (formula, default) | run_20261018_144211 | copper layout | 50.11 | 49.51-50.47 | **pass** |
| 176.6 (w 0.25 mm, formula) | run_20261018_143910 | forced | 50.58 | 49.78-51.16 | **pass** |
| 168.69 ped 0, lid 0.3 (diagnostic) | run_20261018_143518 | forced, `pedestal_depth` 0 | 51.14 | 49.31-59.82 | fail (20 GHz spike) |
| 168.69 ped 0.5, lid 1.2 (diagnostic) | run_20261018_143647 | forced, `route_chip_clearence` 1.2 | 57.13 | 56.47-57.70 | fail |
| 168.69 ped 0, lid 1.2 (diagnostic) | run_20261018_143750 | forced, both | 53.97 | 51.55-60.55 | fail (20 GHz spike) |
| 115 | run_20261018_135625 | forced | 49.18 | 48.70-49.50 | pass |

The 105 um point was not run: 115 um already brackets the low side. The forced-gap runs build the section from parameters while the analytic run uses the copper layout; the 155 and 168.69 um points lie on one slope, which suggests the difference is small, but this is not separately proven.

**Chip gap: cause and fix (2026-10-18).** The open-top formula ignores two things the die has in HFSS: the air under it (`pedestal_depth`) and the lid over it (`route_clearence`). A 2-D field solve of the same cross-section (scratch solver, same scheme as `base_parts/cpw_fd_check.py`, mesh converged to 0.2 ohm) gives: lid 0.3 mm alone lowers the line by about 2.7 ohm; the 0.5 mm air under the die raises it by about 5.5 ohm; together the 50 ohm gap is about 141 um, not 121 um. HFSS agrees with the field solve on the lid (lid 0.3 to 1.2 mm: +2.5 ohm in HFSS, +2.8 in the field solve) but shows only about 3.5 ohm from the air (the field solve: 5.5), so no closed form for the stack reproduces its gap slope (a layered-substrate candidate was tried and rejected). The fix is therefore empirical: the open-top formula times `CHIP_HFSS_IMPEDANCE_FACTOR` = 1.079, the mean of HFSS/formula over the seven gaps of S2 and S3 (1.069-1.092, sd 0.8 %). It is validated at one other width (0.25 mm, S3 above). It does not follow `route_clearence` or `pedestal_depth`: changing either needs a Line impedance check in HFSS, and the diagnostic runs above (lid 1.2 mm reads about 2.5 ohm above lid 0.3 mm; no air reads about 3.5 ohm below) show the size of that effect. The solve also now reaches wider traces on silicon (0.34 mm gives a 460 um gap; 0.38 mm is unreachable); 0.34 mm is outside the validated range. The field-solve numbers are in the shared notes, not the repo.

**Goal band.** The "1-26 GHz" in the S1 and S2 wording above is not the goal band; the goal band is 1-20 GHz. The S1 result stays on 1-26 GHz as recorded.
| SMP footprint, finish, fits, fab capability, Gerber review | **open** | manual |
| Room-temperature, cold and TDR measurements | **open** | after manufacture |

Update this table, not just the report, when a row changes.
