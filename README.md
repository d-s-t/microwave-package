# pedestal_experiment

The client pipeline for the pedestal experiment, in
[`microwave_package/`](microwave_package/): one script (or a web UI on
top of it) that, end to end, draws each point of a parameter sweep in
Altium, builds its SolidWorks STEP file, turns the two into an AEDT
model, queues them for analysis, waits for and monitors the worker's
progress, and plots the combined results once they're in. Resumable at
every stage, and stoppable at any point.

The three programs work at the same time, each on its own combination
(see [`workflow/stages.py`](microwave_package/workflow/stages.py)):
Altium draws combination 5's boards while SolidWorks rebuilds
combination 4's solid and AEDT imports combination 3's STEP. A
combination reaches AEDT once both of its halves have landed.

A run asks HFSS one of three questions, chosen per run:

- **Eigenmode** -- what does this cavity resonate at? Source-free, no
  ports; the answer is a frequency and a Q per mode.
- **Driven Modal** -- what does this package do to a signal? Drives the
  two SMP ports over a frequency sweep; the answer is S11 and S21 across
  the band, plus a Touchstone file per combination.

- **Line impedance** -- is the CPW really 50 ohm? A short straight section
  of the line as built (substrate, trace, solved gap, ground, via posts,
  the lid with its relief channel), wave ports at both ends; the answer is
  Zo against frequency with a pass/fail against 50 +- 2.5 ohm. An
  independent check of the analytic gap calculation, cheap to run. See
  [Line impedance](#line-impedance).

By default an Eigenmode or Driven run does **both**, on the same geometry (see
[Eigenmode and Driven together](#eigenmode-and-driven-together)).

What has to be checked before a board is ordered, and the command that does
the automatic part, is in [PRE_MANUFACTURING.md](PRE_MANUFACTURING.md).

They share the same geometry and differ only in what is solved on it.
Each is a solver plugged into [`hfss/registry.py`](microwave_package/hfss/registry.py),
which is where the contract between a solution type and the rest of the
pipeline lives. See
[`hfss/driven/ports.py`](microwave_package/hfss/driven/ports.py) for
where a driven run's ports go -- that choice fixes the reference plane,
so it is asked rather than assumed.

## Layout

```
microwave_package/
  design/     parameters -> physical design -> STEP and layout files
  hfss/       STEP -> HFSS model, and the solvers that run on it
    registry.py   what a solver is; nothing here knows the solvers
    eigenmode/    resonant frequencies and Q, and the chip |E| pictures
    driven/       S-parameters: the sweep, the ports, the Touchstone
  workflow/   running a sweep: state, queue, results, plots
    stages.py     Altium, SolidWorks and AEDT as three parallel queues
  pipeline.py, webapp.py    the two entry points
runs/         every run's output (git-ignored)
base_parts/   the SolidWorks part library and the Altium generator
```

`design/` and `workflow/` know nothing about solution types. Where a step
needs to -- which setup to make, where the ports go, how to read the
results back -- it asks the run's solver rather than branching.

The worker -- the generic background service that actually runs the
AEDT analyses -- lives in its own repo,
[`ansys-analyze-worker`](https://github.com/d-s-t/ansys-analyze-worker),
which this project depends on for the shared task-queue schema.

## Install

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

This installs `ansys-analyze-common` (the shared queue schema) straight
from GitHub -- see `requirements.txt` for how to pin a specific version
instead of tracking its default branch -- plus `pywin32` (SolidWorks COM
automation), `pyaedt`, `pandas`, `plotly`, and `Flask`.

SolidWorks and AEDT (student version is enough for the model-building
steps) both need to be installed and licensed on the machine that runs
`microwave_package`.

## Set the queue location

```
setx ANSYS_ANALYZE_QUEUE_PATH "\\some\shared\path\ansys_queue"
```

Both this project and the worker (usually running on a different
machine) need this pointed at the same shared location.

`setx` only reaches programs started after it. `webapp.cmd` fills in any
variable its own environment is missing from what Windows has saved, so it
picks the setting up even when launched from an IDE or app that was already
open; anything else (a terminal, `python -m ...`) needs restarting first.

## Run

### Web UI

```
webapp.cmd
```

This starts the web UI with the first Python it finds: `MICROWAVE_PYTHON`
if set, the activated venv, `.venv` at the repo root, or `python` on
PATH -- and says which one it picked. `.claude/launch.json` runs it too.
To use an interpreter that lives elsewhere:

```
setx MICROWAVE_PYTHON "D:\envs\lab\Scripts\python.exe"
```

(`python -m microwave_package.webapp` with any interpreter works just
as well.)

Open <http://127.0.0.1:5000/> (or set `PORT` to serve somewhere else).
Pick which parameter(s) from `base_parts/default.txt` to sweep and over
what range, choose what to solve -- Eigenmode or Driven Modal, which
decides which analysis settings the form then asks for -- and start the
run.

The status page updates live, with a bar per phase rather than one bar
for the whole run -- because several of them are happening at once:

    Altium          the boards, drawn in Altium          \ at the same
    SolidWorks      the solid, rebuilt and exported      / time
    AEDT model      the HFSS model, built here
    Copy to queue   the project, copied to the share
    Analysis        the solve, run by the worker
    Copy back       the analysed files, copied off the share
    Results         the results, read out of the solved project

Every combination carries the same seven, so its row says where it
actually is ("SolidWorks...", "Queued...", "Copy back...") instead of
just "running". The plot appears automatically once everything's done.
Existing runs (including ones interrupted or stopped mid-way) are listed
on the home page and can be resumed with one click.

**Comparing runs.** Tick two or more runs with results in the run list
and press **Compare** (up to eight at a time). The comparison page
first says what the runs' own records disagree on -- analysis settings,
materials, and every dimension of the geometry they were built with,
including equation-file values nobody typed in -- rather than trusting
their names. It then lines their results up point by point (the same
swept values and mode, whatever each run called its combinations), with
the difference against a base run you pick, and draws them all in one
plot, each run keeping its own colour and marker. One filter scopes the
table and the plot together; clicking a point opens that combination
on its own run's page.

**Bare board.** The "Bare board" tick (`no_chip = 1` in the equations
file) simulates only the PCB and its connectors: a straight 50 ohm CPW
from one SMP to the other, with no die, opening, pedestal, bond pads or
wire bonds. It exists to separate board and connector behaviour from
chip behaviour. It is pinned for the whole run, never swept, and off by
default. Altium draws the trace unbroken and skips the chip board; HFSS
builds the substrate unpocketed and no bonds; the layout check no longer
reserves room for the die and its bonds (the connector launch still has
to fit). Driven runs use the `connector` (or `launch`) reference plane;
`bonds` and `chip` are refused because they sit at the wire bonds. Not
automated: the SolidWorks parts. `box.SLDPRT` and `lid_inside.SLDPRT` are
binaries driven by the chip parameters, so a bare-board STEP still
carries whatever the pedestal and chip lid features make from
`pedestal_depth`, `chip_length`, `chip_side` and `bonding_clearence`.
The form also pins `pedestal_depth = 0` for a bare-board run (the pedestal
stands where an ordinary board's bottom ground plane has its opening, so
on a whole plane AEDT rejects the model as intersecting); a value typed
into the form still wins. Started any other way, set it by hand. Eigenmode
runs solve normally, but the chip-field columns come back flagged
"object 'chip' not found", since there is no chip to measure.

**The SolidWorks parts' equation link.** `box.SLDPRT` and `lid_inside.SLDPRT`
read their dimensions from `base_parts/default.txt` through a link that is
stored in the part as an *absolute path*; as committed, that path is in
whichever worktree last saved them, not in your checkout. Each run
therefore re-points the link at the equations file it is writing and
re-reads it when it opens the parts (`step_generation.link_equations`),
in memory only -- the parts are never saved back, so a run leaves no
modified binaries in the checkout. If you re-save the parts yourself, do it
from the checkout you work in; the stored path is then harmless either way.

**Corner screws.** The "Corner screws" tick (`screws_on = 1`) holds the lid,
PCB and box together with four M2 ISO 10642 countersunk screws, `screw_inset`
in from the PCB edge on both axes, with holes in all three parts: a clearance
hole in the lid with a 90 degree countersink the head sits flush in, a
clearance hole and copper keep-out in the PCB, and a tap-drill hole in the box
through the floor under each corner (the board rests on the floor there; the
pedestal is a recess cut under the chip). They sit inside the
PCB corners, not in the box wall, which is `wall_width` = 1 mm thick and
cannot hold an M2 thread.

The screws are the vendor's STEP models in `base_parts/screws/`, imported,
cut to the exact length the stack needs (from the shortest model at least that
long; screws can be shortened) and moved onto the corners. They take the box's
material and perfect-E setting. The generator keeps its ground vias out of the
keep-out; HFSS cuts the holes in `box`, `lid_inside` and the board's
substrate. Altium draws the four holes as round board cutouts, and the copper
keep-out round each one is in the copper itself, the way the die's opening and
the connector's anti-pad are: a region cannot carry an interior hole, so each
ground plane gives up a square round the screw out to the board's corner
(`screw_keepout_diameter` plus half a millimetre) and four pieces with the
keep-out bitten out are drawn back. `copper.txt` carries it into HFSS. The layout check refuses a board
where the keep-out would reach the CPW, the SMP launch or the die and its bond
fingers, or a lid too thin for the head (`lid_width` against the 1.14 mm head
plus 0.3 mm of seat).

On by default. In SolidWorks the lid has a Hole Wizard countersunk hole and the
box a through-tapped M2 hole at each corner, both before the final mirror and
positioned by `screw_inset` from the outer edges. `floor_width` is a formula
(`screw_engagement - pedestal_depth`, never under `wall_width`), so the floor at
the corners is as thick as the thread is long; `lid_width` sets the lid and
the cavity depth, so the lid ends flush with the box top. Set `screws_on = 0`
to build without screws: both hole features are suppressed by an equation,
`floor_width` falls back to `wall_width`, and the Altium holes and the HFSS
screws are not made. The box's tap-drill diameter follows
`screw_tap_diameter`.

Reading fields back from a solved project is limited by the student licence:
a project whose solved mesh is over its element limit (the default 50 x 25 mm
board with screws is about a million elements) cannot have its field
statistics or plots exported on this machine, and the frequencies and Q come
out regardless. Each affected row is marked skipped with that reason, instead
of a list of unrelated AEDT errors.

**Stop this run** stops a run without losing anything. It is cooperative
rather than a kill -- none of the three programs can be interrupted
mid-call -- so each finishes the combination it is in the middle of,
records it, and the run ends where it stood. Resume carries on from
there: nothing already drawn, built, modelled or analysed is done twice.

Each combination on a run's status page has an **Open in AEDT** button
(opens that combination's project in a visible AEDT window on this
machine) and, once the STEP viewer's assets are present, a **3D view**
button that shows the combination's geometry in the browser -- no
external program. Those assets (three.js + occt-import-js, ~8 MB, not
committed) are fetched once with:

```
python -m microwave_package.fetch_vendor
```

Skip it and everything else still works; the 3D view button just stays
hidden.

### Command line

```
python -m microwave_package.pipeline --sweep pedestal_depth=0:10:2
```

`--sweep` is repeatable -- sweeping more than one parameter runs every
combination of them (the cartesian product), so watch the combination
count: each one is a full model build + analysis.

Ctrl-C stops a run the same way the web UI's Stop button does: each
program finishes the combination it is on and the run ends resumable
(press it again to quit without waiting -- the run stays resumable
either way). To resume a stopped or interrupted run instead of starting
a new one:

```
python -m microwave_package.pipeline --resume run_20260819_140102
```

A run is Eigenmode unless told otherwise. For S-parameters:

```
python -m microwave_package.pipeline --solution-type DrivenModal --f-start-ghz 1 --f-stop-ghz 15 --sweep pedestal_depth=0:10:2
```

`--reference-plane connector` (the default) puts a wave port across the SMP's
air bore just above its bead (the barrel above is capped off), so the
connector transition is inside the measurement; `--reference-plane launch` puts a lumped port under the CPW
trace instead. `--reference-plane bonds` moves that lumped port in to just before the
wire bonds, and `--reference-plane chip` puts a pair of gap ports on the die's own
CPW, trace to coplanar ground. In those three planes the connectors are terminated in
the port impedance and the line runs on past each port to them, so a port is a tap:
even a perfect line reads S11 = S21 = -6 dB at low frequency. They are for seeing
where a feature comes from -- a notch the connector plane shows and the chip plane
does not is made outside the die.

Without `--no-pair` the command above solves both: an Eigenmode run first
and then the Driven run, which gets fine frequency windows around the
modes (`--no-pair` solves just the `--solution-type`). `--fine-windows
off`, `--fine-windows-manual LO:HI:STEP[;...]`, `--adapt-at
highest_mode` and `--box-conductivity` (with `--no-perfect-e-box`) set the
options described below; the finer factors are settings in the web form.

Run `python -m microwave_package.pipeline --help` for every analysis
setting -- they are grouped by which solution type they apply to.

### If the queue share is unreachable

Both entry points treat a temporarily-unreachable
`ANSYS_ANALYZE_QUEUE_PATH` (dropped network share, VPN blip, ...) as
something to wait out rather than a fatal error -- the pipeline pauses
and retries until the share comes back, then continues from wherever it
was. You can also just stop it (Ctrl-C, or close the web UI's process)
and resume later with `--resume`/the web UI's Resume button; nothing
already built or queued is lost.

## Eigenmode and Driven together

A run can solve both questions for every combination on the same
geometry, and it is **one run** in the web app: one entry in the run list,
one status page, one results view with the eigenmodes marked on the S21
plot and listed under it.

Underneath, an HFSS design has one solution type, and a queue task names
one project, one design and one solution type, so the two solves cannot
share a setup or a task. The run therefore keeps its eigenmode part as a
**hidden part** of itself (`part_of` in its config, a folder named
`<run>_eigen` beside it; see
[`workflow/companion.py`](microwave_package/workflow/companion.py)). The
queue schema is unchanged and the worker needs no change. Hidden parts are
not listed, their pages redirect to the run's, and deleting the run deletes
them with it.

- Tick **Both, on the same geometry** under *What to solve* (on by
  default; `--no-pair` on the command line turns it off). It needs
  `port_count = 2`, and is refused up front otherwise.
- **Eigenmodes first.** The run's stage reads "Solving eigenmodes first
  (step 1 of 2)" and its page and list row show that step's progress; then
  the S-parameter step starts from the same geometry (the STEP files and
  copper -- Altium and SolidWorks are not asked again) and reads the modes.
  Stopping or resuming the run does the same to both steps. If the
  eigenmode step fails, the S-parameter step still goes ahead with the
  broad sweep alone and says so.
- **Fine windows.** Box modes have Q of about 60-500, so they are 25-35 MHz
  wide while the broad sweep steps about 100 MHz, and a narrow dip is
  under-sampled. Beside the broad sweep, each eigenmode below the sweep
  stop gets a window of its own, as extra interpolating sweeps (`Fine_1`,
  `Fine_2`, ...) in the same setup:
  half-width = 0.5 x f/Q (that is f/(2Q)) plus a 1 % margin on f, step =
  f/Q / 20 (that is f/(20Q)). The margin is there because the eigenmode
  solve has no connectors, so its frequency is only a hint of where the
  driven dip lands. Overlapping windows merge, and a cap of 2000 points
  across all windows makes every step coarser by the same factor rather
  than dropping a mode. All of these are driven settings (*Window
  half-width*, *Points per linewidth*, *Window margin*, *Q to assume when
  an eigenmode has none*, *Max fine-window points*), as are *Fine windows*
  (automatic or off) and *Extra fine windows*, a list of `lo:hi:step` in
  GHz, for windows you pick yourself -- those work without eigenmodes
  too. The fine points are folded into the run's results, in frequency
  order, with a `sweep` column saying which sweep each came from.
- **Mesh frequency.** *Adapt the mesh at* is the top of the sweep by default
  (as before); the other choice is just above the highest eigenmode in
  the band.
- **Results.** The S-parameter plot against frequency marks the eigenmodes
  as triangles along its foot (they follow the combination filter; hover for
  frequency and Q), and an *Eigenmodes* table under it lists them, with a
  CSV. The chip-field heatmaps come from the eigenmode step too.

### Refining a frequency range afterwards

On a finished run's page, **Refine a frequency range** re-solves chosen
combinations over narrower ranges. Any number of ranges, each
`lo:hi:step` in GHz (`lo:hi` alone takes 100 steps), `;`-separated, typed
in or added with:

- *+ plot's zoom range* -- whatever the plot above is zoomed to; or
- *+ around the eigenmodes between [from] and [to]* -- a window around each
  eigenmode in that range (blank = the whole band) for the ticked
  combinations, sampled twice as densely as the automatic windows.

The refinement is **part of the same run**, not a new entry: it is listed
in that section with its state and Stop/Resume, it uses the run's
geometry and settings (borrowed, not rebuilt), solves only the chosen
ranges for the chosen combinations with no broad sweep, and adapts its mesh
where the original did so the two line up. Its points join the run's
results and CSV, and the plot's **Data** selector then switches between
*Broad only*, *Fine ranges only* (each range its own piece, not one line
across the gaps) and *Broad + fine, one trace* (the default), where a fine
window's points replace the broad sweep's interpolated samples inside it.
The automatic windows count as fine data too. Refining again adds more.

Caveat: the refinement rebuilds the model and re-adapts the mesh (at the
frequency the run adapted at) rather than reusing the solved project, and
a very sharp, high-Q feature is sensitive to the mesh. Measured on one
combination: a broad dip at 16.87 GHz reproduced to 5 MHz and 2 dB, but a
sharp step near 15.8 GHz sat about 50 MHz away in the refinement from where
the run's own fine window had it. So read broad and fine data as two
independent solves where a feature is that sharp, and treat a feature that
moves between them as mesh-limited, not as a measurement.

### The box's conductor model

*Perfect E on box/lid_inside* stays on by default: the box is probably
aluminium, which is superconducting below about 1.2 K. Perfect E puts the
mode and dip **positions** in the right place but exaggerates dip
**depth** and Q, because nothing is lost in the walls. Turning it off
gives the box and lid a finite-conductivity boundary of *Box conductivity*
(S/m, default 1e8, about 6061 aluminium at liquid-helium temperature;
3.8e7 at room temperature), with no thickness -- an Eigenmode design
refuses one on that boundary. The setting is `box_conductivity` in a run's
`materials`, and is listed on the comparison page.

## Line impedance

The third solution type checks the CPW rather than the package. It builds
only a few millimetres of the line from the run's own copper layout (or from
the parameters when there is none), including the lid sitting on the
ground copper with its relief channel, which the analytic 50 ohm gap
(`design/cpw.py`) does not see, puts a wave port on each end face, and sweeps
the band. Zo is derived from the section's S-parameters (and the port's own
Zo is shown when HFSS reports it); the run page shows PASS/FAIL against the
target and tolerance, the Zo range, the gap solved and the analytic gap.

From the web form pick *Line impedance*; from the command line:

```
python -m microwave_package.pipeline --solution-type LineImpedance --sweep cpw_trace_width=0.4 --mat-pcb "Rogers RT/duroid 5880 (tm)"
python -m microwave_package.pipeline --solution-type LineImpedance --setting which_line=chip --sweep cpw_trace_width=0.4
```

`--setting NAME=VALUE` (repeatable) sets any analysis setting that has no flag of its
own -- `which_line` (board or chip), `section_length_mm`, `line_f_start_ghz`,
`line_f_stop_ghz`, `line_z0_ohm`, `line_tolerance_ohm` and the rest. Sweeping
`cpw_trace_width` or the gap and comparing the verdicts is how to see what HFSS
wants against what the formula gives. It needs no SolidWorks, so a run goes
straight from the Altium step to AEDT, and it is never paired with an Eigenmode run.

The chip line (`which_line = chip`) rests on two numbers that are the least
certain in the model -- the air under the die and the lid's cover over it.

## Preflight

```
python -m microwave_package.preflight --candidate --out preflight_report.md
```

Runs every pre-manufacturing check that needs no simulation (layout, CPW gap,
fabricator limits, screws, cryogenic expansion) and lists the ones that do as
manual with how to do each. Options and the checklist itself are in
[PRE_MANUFACTURING.md](PRE_MANUFACTURING.md).

## Output

Every run gets its own folder under `runs/<run_id>/` at the repo root
(set `MICROWAVE_RUNS_PATH` to put them on another disk entirely):
the generated STEP/AEDT files per combination (`models/<combo_id>/`),
the run's resumable state (`state.json`, `run_config.json`), and the
final combined CSV + interactive plot (`plots/`). A driven run also
leaves a Touchstone (`.s2p`) file per combination beside its analysed
project, so its S-parameters are readable by anything else. This folder
is git-ignored -- it's backed up elsewhere, not committed.

## The SMP connector model

The launch connector is Würth Elektronik WR-SMP, part **60124102122403**.
The vendor ships its 3D model as a single fused solid, which HFSS would
import as one object with no metal/insulator interfaces. The companion
project
[`wr-smp-connector`](https://github.com/d-s-t/wr-smp-connector) splits it
into three named solids -- outer conductor, centre conductor and
dielectric bead -- and that split file is committed here as
`base_parts/smp_connector_split.step`; `model_builder.place_connectors`
imports it as three bodies so the bead is modelled as LCP rather than
swallowed into a PEC lump. Nothing at build time reaches outside this
repo -- the split STEP is checked in, and `wr-smp-connector` is only a
reference for how it was reconstructed and how to regenerate it.

## Extending

- **New scripts for this experiment**: add them to whichever of
  `design/`, `hfss/` or `workflow/` matches what they do, and wire them
  into `pipeline.py` / `webapp.py` as needed.
- **New solution types**: a new folder under `hfss/`, and a
  `register(Solver(...))` call in its `__init__.py` saying what its
  settings are, how to make its setup, where its ports go if it has any,
  how to read its results, and how to plot them. Nothing upstream
  changes: `hfss/registry.py` does not know its solvers, and `design/`
  and `workflow/` do not know solution types exist.

  Usually no worker change either. Every task this repo queues is a
  `"run_setups"` task -- "open the project and run the setup(s) already
  in it" -- and the kind of solve is carried separately in the task's
  `solution_type`. A new handler in `ansys-analyze-worker`
  (`docs/ARCHITECTURE.md` section 6) is only for a genuinely different
  *workflow* -- one where the worker has to do something other than run
  what it was given. Remember it only ever runs a setup this repo
  already created (§8.1/§12 of that doc) -- it never creates one, and it
  never post-processes the result either (that's
  `workflow/postprocess.py`, run sequentially, one project at a time,
  right after the worker finishes each task).
- **New sweepable parameters**: anything in `base_parts/default.txt`
  with a plain numeric value (not a formula) is automatically offered in
  the web UI and usable via `--sweep NAME=...` -- no code changes needed.

See `ansys-analyze-worker`'s `docs/ARCHITECTURE.md` for the full design
doc (task/result schema, queue layout, troubleshooting) that this
project's pipeline follows.
