"""
The DrivenModal setup, and the frequency sweep beside it.

Two objects, not one: the setup does the adaptive meshing at a single
frequency, and the sweep reuses that mesh across the band. Both have to
exist before the project is queued, because the worker runs whatever
setups it finds and never creates or configures anything itself.
"""
from __future__ import annotations

import logging

from . import windows

logger = logging.getLogger("microwave_package")

SETUP_NAME = "Setup_1"
# Eigenmode has no sweep at all, so this name only ever exists in a
# driven project -- which is what results.py keys on to tell the sweep's
# solution from the adaptive one.
SWEEP_NAME = "Sweep_1"


def create_setup(hfss, dimensions_txt_content: str, analysis_settings: dict,
                 log=logger.info):
    """Create the project's single DrivenModal setup and its frequency sweep.

    `dimensions_txt_content` is unused -- it is in the solver contract
    (see hfss/registry.py) because the eigenmode setup derives its
    starting frequency from the geometry, while a driven run is given its
    band outright.

    The adaptive pass is meshed at the TOP of the band. The mesh a
    solution frequency produces has to resolve the shortest wavelength
    the sweep will ask about, and a mesh refined at 1 GHz does not
    describe 15 GHz -- while one refined at 15 GHz is merely finer than
    1 GHz needs.

    The sweep is interpolating: it solves a handful of frequencies and
    fits a rational model through them, rather than solving all
    `sweep_points` of them. That is what makes a few hundred reported
    points cost about the same as a few dozen, and it is the right choice
    for an S-parameter curve, which is smooth apart from its resonances.
    """
    f_start = float(analysis_settings["f_start_ghz"])
    f_stop = float(analysis_settings["f_stop_ghz"])
    if f_stop <= f_start:
        raise ValueError(
            f"The frequency sweep needs f_stop > f_start; got {f_start} -> {f_stop} GHz."
        )

    # The eigenmodes of this geometry, when the run was paired with an
    # Eigenmode run: model_builder puts them in the settings it passes here.
    modes = analysis_settings.get("eigenmodes") or ()
    mesh_ghz = windows.mesh_frequency_ghz(analysis_settings, modes)

    setup = hfss.create_setup(name=SETUP_NAME)
    setup.props["Frequency"] = f"{mesh_ghz}GHz"
    setup.props["MaxDeltaS"] = analysis_settings["max_delta_s"]
    setup.props["MaximumPasses"] = analysis_settings["max_passes"]
    setup.props["MinimumPasses"] = analysis_settings["min_passes"]
    setup.props["MinimumConvergedPasses"] = analysis_settings["min_converged"]
    setup.update()

    broad = analysis_settings.get("coarse_sweep", "on") != "off"
    if broad:
        sweep = hfss.create_linear_count_sweep(
            setup=SETUP_NAME,
            unit="GHz",
            start_frequency=f_start,
            stop_frequency=f_stop,
            num_of_freq_points=int(analysis_settings["sweep_points"]),
            name=SWEEP_NAME,
            sweep_type="Interpolating",
            # The S-matrix is what this run is for, and an interpolating
            # sweep cannot save fields at its interpolated points anyway.
            # Not saving them keeps the solved project small enough to copy
            # back over the queue share.
            save_fields=False,
        )
        if not sweep:
            raise RuntimeError(
                f"Could not add the {SWEEP_NAME} frequency sweep to {SETUP_NAME}. Without "
                "it the setup solves one frequency and there is no S-parameter curve."
            )

    elif not (windows.parse_manual(analysis_settings.get("fine_windows_manual"))
              or analysis_settings.get("fine_windows", "auto") == "auto" and modes):
        raise ValueError("The broad sweep is off and there is no fine window to solve instead.")
    log(
        f"Driven setup: adaptive at {mesh_ghz:g} GHz (MaxDeltaS "
        f"{analysis_settings['max_delta_s']}), "
        + (f"interpolating sweep {f_start}-{f_stop} GHz over {analysis_settings['sweep_points']} points."
           if broad else "no broad sweep, fine windows only.")
    )
    add_fine_sweeps(hfss, analysis_settings, modes, log)
    return setup


def add_fine_sweeps(hfss, analysis_settings: dict, modes, log=logger.info) -> list:
    """Add one narrow interpolating sweep per fine window (see windows.py).

    HFSS allows several sweeps in one setup and solves each against the
    setup's single adaptive mesh, so a window costs a few extra frequency
    solves rather than another adaptive run. A window that cannot be
    added is logged and skipped: the coarse sweep still gives a curve,
    and one bad window is not worth losing the model over.

    Returns the windows that were added.
    """
    planned = windows.plan(analysis_settings, modes)
    if not planned:
        if modes:
            log("Driven setup: no fine windows (turned off, or no mode inside the band).")
        return []
    added = []
    for index, window in enumerate(planned, start=1):
        sweep = hfss.create_linear_count_sweep(
            setup=SETUP_NAME, unit="GHz",
            start_frequency=window.lo, stop_frequency=window.hi,
            num_of_freq_points=window.points,
            name=f"{windows.FINE_SWEEP_PREFIX}{index}",
            sweep_type="Interpolating", save_fields=False,
        )
        if not sweep:
            log(f"WARNING: could not add fine window {window.lo:.4f}-{window.hi:.4f} GHz.")
            continue
        added.append(window)
    log(
        f"Driven setup: {len(added)} fine window(s), {windows.total_points(added)} points "
        "-- " + "; ".join(f"{w.lo:.4f}-{w.hi:.4f} GHz @ {w.step * 1e3:.2f} MHz" for w in added)
    )
    return added
