"""
The client pipeline for the pedestal experiment.

    design/    parameters -> physical design -> STEP and layout files
    hfss/      STEP -> HFSS model, and the solvers that run on it
    workflow/  running a sweep: state, queue, results, plots

`pipeline.py` and `webapp.py` at this level are the two entry points.
"""
