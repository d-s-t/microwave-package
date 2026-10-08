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

A run asks HFSS one of two questions, chosen per run:

- **Eigenmode** -- what does this cavity resonate at? Source-free, no
  ports; the answer is a frequency and a Q per mode.
- **Driven Modal** -- what does this package do to a signal? Drives the
  two SMP ports over a frequency sweep; the answer is S11 and S21 across
  the band, plus a Touchstone file per combination.

By default a run does **both**, on the same geometry (see
[Eigenmode and Driven together](#eigenmode-and-driven-together)).

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

An HFSS design has one solution type, and a queue task names one project,
one design and one solution type, so an Eigenmode design and a Driven
Modal design cannot share a setup or a task. A combination is therefore
solved as **a pair of ordinary runs over the same sweep**, linked on
their status pages and in their configs (`eigen_run_id` / `driven_run_id`;
see [`workflow/companion.py`](microwave_package/workflow/companion.py)).
The queue schema is unchanged and the worker needs no change.

- Tick **Both, on the same geometry** under *What to solve* (on by
  default; `--no-pair` on the command line turns it off). It needs
  `port_count = 2`, and is refused up front otherwise.
- The **Eigenmode run goes first**. The **Driven run** then starts from its
  geometry (the STEP files and copper, so Altium and SolidWorks are not
  asked again) and reads its modes. Starting, stopping or resuming the
  Driven run does the same to the Eigenmode run it waits for; resuming the
  Eigenmode run alone works as before. If the Eigenmode run fails, the
  Driven run still goes ahead with the coarse sweep alone and says so.
- **Fine windows.** Box modes have Q of about 60-500, so they are 25-35 MHz
  wide while the coarse sweep steps about 100 MHz, and a narrow dip is
  under-sampled. Beside the coarse sweep, each eigenmode below the sweep
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
  GHz, for windows you pick yourself -- those work without an Eigenmode run
  too. The fine points are folded into the run's results, in frequency
  order, with a `sweep` column saying which sweep each came from.
- **Mesh frequency.** *Adapt the mesh at* is the top of the sweep by default
  (as before); the other choice is just above the highest eigenmode in
  the band.
- **Plots.** The driven run's S-parameter plot against frequency shows the
  paired run's eigenmodes as triangles along the foot of the curves; they
  follow the combination filter. Hover for the mode's frequency and Q.
- Each run is complete in itself, so the comparison page, results CSVs and
  deleting a run work on each as before. Runs made before this exist
  unpaired and are untouched.

### Refining a frequency range afterwards

On a finished Driven run's page, **Refine a frequency range** re-solves
chosen combinations over narrower ranges, for as many ranges as you like
(`lo:hi:step` in GHz, `;`-separated; `lo:hi` alone takes 100 steps; or
*+ use the plot's zoom range* to take whatever the plot is zoomed to).
It starts a new Driven run linked to the first: the same geometry (borrowed,
not rebuilt), the same settings, only the chosen combinations, no broad
sweep, and the mesh adapted at the frequency the original adapted at so
the two line up. Refining again adds more.

The original run's plot then has a **Data** selector: *Broad only*, *Fine
ranges only* (each range its own piece, not one line across the gaps), or
*Broad + fine, one trace* (the default), where a fine window's points
replace the broad sweep's interpolated samples inside it. The fine windows
placed around eigenmodes automatically are fine data too, so the same
selector works on them. The refinement is a run in its own right, with its
own page and results.

Caveat: the refinement rebuilds the model and re-adapts the mesh rather than
reusing the solved project, so with identical settings it should land on the
same mesh, but a slight offset between broad and fine data is possible.

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
