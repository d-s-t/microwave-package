"""
eigen_mode_analyze
====================

The end-to-end client pipeline for the pedestal eigenmode experiment:
generates SolidWorks STEP files for a parameter sweep, builds an AEDT
model per point, queues each for analysis by the worker (see the
`ansys-analyze-worker` repo), monitors progress, and plots the combined
results -- resumable at every stage, and drivable either from the
command line (`pipeline.py`) or a web UI (`webapp.py`).

See this repo's README for setup and usage.
"""
from __future__ import annotations

__version__ = "0.1.0"
