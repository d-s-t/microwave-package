# pedestal_experiment

The client pipeline for the pedestal eigenmode experiment, in
[`eigen_mode_analyze/`](eigen_mode_analyze/): one script (or a web UI on
top of it) that, end to end, builds a SolidWorks STEP file per point in a
parameter sweep, builds an AEDT model from each, queues them for
analysis, waits for and monitors the worker's progress, and plots the
combined results once they're in. Resumable at every stage.

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
`eigen_mode_analyze`.

## Set the queue location

```
setx ANSYS_ANALYZE_QUEUE_PATH "\\some\shared\path\ansys_queue"
```

Both this project and the worker (usually running on a different
machine) need this pointed at the same shared location.

## Run

### Web UI

```
python -m eigen_mode_analyze.webapp
```

Open <http://127.0.0.1:5000/>. Pick which parameter(s) from
`base_parts/default.txt` to sweep and over what range, set the analysis
settings, and start the run. The status page updates live as STEP files
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
python -m eigen_mode_analyze.fetch_vendor
```

Skip it and everything else still works; the 3D view button just stays
hidden.

### Command line

```
python -m eigen_mode_analyze.pipeline --sweep pedestal_depth=0:10:2
```

`--sweep` is repeatable -- sweeping more than one parameter runs every
combination of them (the cartesian product), so watch the combination
count: each one is a full model build + analysis. To resume an
interrupted run instead of starting a new one:

```
python -m eigen_mode_analyze.pipeline --resume run_20260819_140102
```

Run `python -m eigen_mode_analyze.pipeline --help` for every analysis
setting (mode count, pass counts, materials, etc.).

### If the queue share is unreachable

Both entry points treat a temporarily-unreachable
`ANSYS_ANALYZE_QUEUE_PATH` (dropped network share, VPN blip, ...) as
something to wait out rather than a fatal error -- the pipeline pauses
and retries until the share comes back, then continues from wherever it
was. You can also just stop it (Ctrl-C, or close the web UI's process)
and resume later with `--resume`/the web UI's Resume button; nothing
already built or queued is lost.

## Output

Every run gets its own folder under `eigen_mode_analyze/runs/<run_id>/`:
the generated STEP/AEDT files per combination (`models/<combo_id>/`),
the run's resumable state (`state.json`, `run_config.json`), and the
final combined CSV + interactive plot (`plots/`). This folder is
git-ignored -- it's backed up elsewhere, not committed.

## Extending

- **New scripts for this experiment**: add them under `eigen_mode_analyze/`
  alongside the existing modules (`equations.py`, `sweep.py`,
  `step_generation.py`, `model_builder.py`, `monitor.py`,
  `post_processing.py`, `postprocess.py`, `results.py`, `plotting.py`,
  `run_manager.py`) and wire them into `pipeline.py` / `webapp.py` as
  needed.
- **New analysis types**: see `ansys-analyze-worker`'s
  `docs/ARCHITECTURE.md` section 6 -- add a handler there; this repo's
  `task_type` (currently always `"eigenmode_analyze"`, set in
  `model_builder.build_and_queue`) just needs to name it. Remember the
  worker only ever runs a setup this repo already created (§8.1/§12 of
  that doc) -- it never creates one, and it never post-processes the
  result either (that's `postprocess.py`, run sequentially, one project
  at a time, right after the worker finishes each task).
- **New sweepable parameters**: anything in `base_parts/default.txt`
  with a plain numeric value (not a formula) is automatically offered in
  the web UI and usable via `--sweep NAME=...` -- no code changes needed.

See `ansys-analyze-worker`'s `docs/ARCHITECTURE.md` for the full design
doc (task/result schema, queue layout, troubleshooting) that this
project's pipeline follows.
