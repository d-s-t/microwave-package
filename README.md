# pedestal_experiment

The client pipeline for the pedestal experiment, in
[`microwave_package/`](microwave_package/): one script (or a web UI on
top of it) that, end to end, builds a SolidWorks STEP file per point in a
parameter sweep, builds an AEDT model from each, queues them for
analysis, waits for and monitors the worker's progress, and plots the
combined results once they're in. Resumable at every stage.

A run asks HFSS one of two questions, chosen per run:

- **Eigenmode** -- what does this cavity resonate at? Source-free, no
  ports; the answer is a frequency and a Q per mode.
- **Driven Modal** -- what does this package do to a signal? Drives the
  two SMP ports over a frequency sweep; the answer is S11 and S21 across
  the band, plus a Touchstone file per combination.

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
pip install -r requirements.txt
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

## Run

### Web UI

```
python -m microwave_package.webapp
```

Open <http://127.0.0.1:5000/> (or set `PORT` to serve somewhere else).
Pick which parameter(s) from `base_parts/default.txt` to sweep and over
what range, choose what to solve -- Eigenmode or Driven Modal, which
decides which analysis settings the form then asks for -- and start the
run. The status page updates live as STEP files
get built, models get queued, the worker finishes each analysis, and
this pipeline reopens each analyzed project to pull out its results (one
at a time, never in parallel) -- the plot appears there automatically
once everything's done. Existing runs (including ones interrupted
mid-way) are listed on the home page and can be resumed with one click.

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
count: each one is a full model build + analysis. To resume an
interrupted run instead of starting a new one:

```
python -m microwave_package.pipeline --resume run_20260819_140102
```

A run is Eigenmode unless told otherwise. For S-parameters:

```
python -m microwave_package.pipeline --solution-type DrivenModal --f-start-ghz 1 --f-stop-ghz 15 --sweep pedestal_depth=0:10:2
```

`--reference-plane connector` (the default) puts a wave port on the SMP
bead's coax cross-section, so the connector transition is inside the
measurement; `--reference-plane launch` puts a lumped port under the CPW
trace instead and measures the board alone.

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
