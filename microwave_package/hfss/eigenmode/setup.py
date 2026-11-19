"""
The Eigenmode setup: where the sweep starts, and how hard it looks.

An eigenmode solve has no excitation and no frequency sweep. It is given
a floor -- `MinimumFrequency` -- and asked for the first `NumModes`
resonances at or above it, so the only real question at build time is
where that floor goes.
"""
from __future__ import annotations

import logging

from ...design import equations as eq

logger = logging.getLogger("microwave_package")

SETUP_NAME = "Setup_1"


def calculate_min_freq(dimensions_txt_content: str, safety_factor: float = 0.4,
                       floor_ghz: float = 0.1) -> float:
    """
    Estimate a starting MinimumFrequency for the eigenmode sweep from the
    PCB footprint, then pull it DOWN by `safety_factor` before returning.

    The naive estimate (half-wavelength resonance across the larger PCB
    dimension) only accounts for the PCB itself -- the actual enclosure
    (box/lid/pedestal cavity) is typically comparable in size or larger,
    so its true lowest cavity mode can sit below that naive estimate.
    Starting the sweep too high silently skips real low-frequency modes.
    `safety_factor` (default 0.4, i.e. start at 40% of the naive value)
    gives headroom to catch those; `floor_ghz` is a hard lower bound.
    """
    params = eq.get_numeric_parameters(dimensions_txt_content)
    try:
        pcb_length = params["pcb_length"]
        pcb_side = params["pcb_side"]
    except KeyError as exc:
        raise ValueError(f"Could not find {exc} in the equations file") from exc

    max_dim_m = max(pcb_length, pcb_side) * 1e-3
    c = 299792458
    wave_lambda = 2 * max_dim_m
    naive_min_freq_ghz = (c / wave_lambda) / 1e9
    return max(naive_min_freq_ghz * safety_factor, floor_ghz)


def create_setup(hfss, dimensions_txt_content: str, analysis_settings: dict,
                 log=logger.info):
    """Create the project's single Eigenmode setup.

    The student licence covers `create_setup` -- it is only
    `analyze`/`analyze_setup` that needs the full one, which is why this
    runs client-side and the worker only ever runs the setup it is
    given (see ansys-analyze-worker's ARCHITECTURE.md section 8).
    """
    explicit = analysis_settings.get("eigen_min_ghz")
    if explicit:
        # The child of a driven run: the floor is the band's, not the PCB's estimate.
        min_freq_ghz = float(explicit)
    else:
        min_freq_ghz = calculate_min_freq(
            dimensions_txt_content, safety_factor=analysis_settings["min_freq_safety_factor"]
        )
    setup = hfss.create_setup(name=SETUP_NAME)
    setup.props["MinimumFrequency"] = f"{min_freq_ghz}GHz"
    setup.props["NumModes"] = analysis_settings["modes"]
    setup.props["MaximumPasses"] = analysis_settings["max_passes"]
    setup.props["MinimumPasses"] = analysis_settings["min_passes"]
    setup.props["MinimumConvergedPasses"] = analysis_settings["min_converged"]
    setup.props["MaxDeltaFreq"] = analysis_settings["max_delta_f"]
    log(f"Eigenmode setup: {analysis_settings['modes']} mode(s) from {min_freq_ghz:.3f} GHz.")
    return setup
