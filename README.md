# pedestal_experiment

Part 1 and Part 3 for the pedestal eigenmode experiment: builds the HFSS
model + boundaries for each pedestal depth, queues the analysis, and
later merges/plots the results. Part 2 -- the generic background worker
that actually runs the analyses -- lives in its own repo,
[`ansys-analyze-worker`](https://github.com/d-s-t/ansys-analyze-worker),
which this project depends on.

## Install

```
pip install -r requirements.txt
```

This installs `ansys-analyze-worker` straight from GitHub (see
`requirements.txt` for how to pin a specific version instead of tracking
its default branch). This project only needs that package's
`queue_common` module, so it pulls in the lightweight base install --
none of the AEDT/tray-icon extras that only the worker itself (running on
a different machine, in the common case) needs.

## Set the queue location

```
setx ANSYS_ANALYZE_QUEUE_PATH "\\some\shared\path\ansys_queue"
```

Both this project's scripts and `ansys-analyze-worker` (usually running
on a different machine) need this pointed at the same shared location.

## Run

On the student-version machine, per batch of STEP files:

```
python part1_build_model.py --step_dir pedestal_experiment --dim_file pedestal_experiment\dimensions.txt
```

On the full-license machine, keep the worker running (see
`ansys-analyze-worker`'s README) -- it picks up tasks Part 1 queues
automatically.

Check on progress or clean up failed runs from either machine:

```
python part3_plot_results.py --check_status
python part3_plot_results.py --cleanup
```

Once some tasks are done, merge and plot:

```
python part3_plot_results.py --runs -3:
```

See `ansys-analyze-worker`'s `docs/ARCHITECTURE.md` for the full design
doc (task/result schema, queue layout, troubleshooting) and the guide
these two scripts follow -- that's the canonical reference for how this
project talks to the worker.
