"""
The DrivenModal setup, and the frequency sweep beside it.

Two objects, not one: the setup does the adaptive meshing at a single
frequency, and the sweep reuses that mesh across the band. Both have to
exist before the project is queued, because the worker runs whatever
setups it finds and never creates or configures anything itself.
"""
from __future__ import annotations

import logging

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

    setup = hfss.create_setup(name=SETUP_NAME)
    setup.props["Frequency"] = f"{f_stop}GHz"
    setup.props["MaxDeltaS"] = analysis_settings["max_delta_s"]
    setup.props["MaximumPasses"] = analysis_settings["max_passes"]
    setup.props["MinimumPasses"] = analysis_settings["min_passes"]
    setup.props["MinimumConvergedPasses"] = analysis_settings["min_converged"]
    setup.update()

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
    log(
        f"Driven setup: adaptive at {f_stop} GHz (MaxDeltaS "
        f"{analysis_settings['max_delta_s']}), interpolating sweep "
        f"{f_start}-{f_stop} GHz over {analysis_settings['sweep_points']} points."
    )
    return setup
