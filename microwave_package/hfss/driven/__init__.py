"""
The DrivenModal solver: what the package does to a signal.

Drives port 1, sweeps frequency, and reports the scattering matrix --
S11, what comes back, and S21, what gets through. The answer is two
curves per combination rather than a handful of numbers.

The one choice with no safe default is where the ports go, because that
is what the S-parameters are referenced to. See `ports.py`: an S11
referenced at the connector is a different number from one referenced at
the CPW, and neither is more correct than the other.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..registry import Solver, register
from . import ports, results, setup

DRIVEN_MODAL = "DrivenModal"

FIELDS: List[Dict[str, Any]] = [
    {"name": "f_start_ghz", "label": "Sweep start (GHz)", "kind": "float", "default": 1.0},
    {"name": "f_stop_ghz", "label": "Sweep stop (GHz)", "kind": "float", "default": 20.0},
    {
        "name": "sweep_points", "label": "Sweep points", "kind": "int", "default": 401,
        "help": "Frequencies the interpolating sweep reports. It solves far "
                "fewer than this and interpolates between them, so a large "
                "number here is cheap.",
    },
    {
        "name": "max_delta_s", "label": "Max delta S per pass", "kind": "float", "default": 0.02,
        "help": "Adaptive convergence target, the driven equivalent of max "
                "delta freq.",
    },
    {
        "name": "fine_windows", "label": "Fine windows around the eigenmodes", "kind": "select",
        "default": "auto",
        "options": [
            {"value": "auto", "label": "Automatic",
             "help": "When this run is paired with an Eigenmode run, every mode "
                     "below the sweep stop gets a narrow, densely sampled window "
                     "of its own beside the coarse sweep. Without eigenmodes "
                     "only the windows typed below are added."},
            {"value": "off", "label": "Off",
             "help": "The coarse sweep alone. A box mode is only f/Q wide -- "
                     "25-35 MHz -- so a ~100 MHz step under-samples its dip and "
                     "its depth is then a matter of where the samples fall."},
        ],
    },
    {
        "name": "fine_halfwidth_linewidths", "label": "Window half-width (linewidths)",
        "kind": "float", "default": 0.5,
        "help": "Half-width of a window in units of the mode's linewidth f/Q. "
                "0.5 is f/(2Q), the half-width at half depth.",
    },
    {
        "name": "fine_points_per_linewidth", "label": "Points per linewidth", "kind": "float",
        "default": 20.0,
        "help": "Window step = f/Q divided by this: the default 20 is f/(20Q).",
    },
    {
        "name": "fine_margin_percent", "label": "Window margin (% of f)", "kind": "float",
        "default": 1.0,
        "help": "Added to each side of a window. The eigenmode solve has no "
                "connectors or ports, so it is only a hint of where the driven "
                "dip lands -- the margin keeps the dip inside its window.",
    },
    {
        "name": "fine_q_fallback", "label": "Q to assume when an eigenmode has none",
        "kind": "float", "default": 200.0,
        "help": "Used for a mode whose Q is missing or infinite (a lossless "
                "perfect-E cavity has no finite Q).",
    },
    {
        "name": "fine_max_points", "label": "Max fine-window points", "kind": "int",
        "default": 2000,
        "help": "Across all windows. Over this every window's step is made "
                "coarser by the same factor -- no mode is dropped.",
    },
    {
        "name": "fine_windows_manual", "label": "Extra fine windows (GHz)", "kind": "text",
        "default": "",
        "help": "lo:hi:step, separated by ; -- for example 12.1:12.4:0.002; "
                "15.0:15.2:0.001. Added to the automatic windows (or used alone "
                "with them off), merged where they overlap.",
    },
    {
        "name": "adapt_at", "label": "Adapt the mesh at", "kind": "select", "default": "f_stop",
        "options": [
            {"value": "f_stop", "label": "Top of the sweep",
             "help": "The mesh resolves the shortest wavelength the sweep asks about."},
            {"value": "highest_mode", "label": "Just above the highest eigenmode",
             "help": "Cheaper when the sweep reaches well past the modes of interest, "
                     "but the mesh is then coarser than the sweep's top end needs. "
                     "Needs a paired Eigenmode run; otherwise the top of the sweep."},
        ],
    },
    {
        "name": "port_impedance_ohm", "label": "Port impedance (ohm)", "kind": "float", "default": 50.0,
        "help": "S-parameters are renormalised to this. The CPW gap is "
                "already solved for 50 ohm, so changing it here only changes "
                "the reference, not the geometry.",
    },
    {
        "name": "reference_plane", "label": "Reference plane", "kind": "select",
        "default": ports.DEFAULT_REFERENCE_PLANE, "options": ports.REFERENCE_PLANES,
    },
]

RESULT_FIELDS: Dict[str, str] = {
    # Magnitudes first: they are what the run is read for, and the axis
    # picker shows them in this order.
    "s11_db": "S11 (dB)",
    "s21_db": "S21 (dB)",
    "s22_db": "S22 (dB)",
    "s12_db": "S12 (dB)",
    "s11_deg": "S11 phase (deg)",
    "s21_deg": "S21 phase (deg)",
    "s22_deg": "S22 phase (deg)",
    "s12_deg": "S12 phase (deg)",
}


def default_axes(df, swept_columns: List[str]) -> Dict[str, Optional[str]]:
    """A driven run reads along the band.

    Frequency across the bottom, S11 up the side, one curve per
    combination -- because the shape of the curve IS the result, and
    comparing combinations means overlaying them.
    """
    return {
        "x": "freq_ghz",
        "y": "s11_db" if "s11_db" in df.columns else "s21_db",
        "color": "combo_id" if "combo_id" in df.columns else None,
    }


def validate(port_counts) -> Optional[str]:
    """Why this geometry cannot be driven, or None.

    Geometry, not settings: two real ports are what makes an S21 exist at
    all, and `port_count` is an equation-file value rather than an
    analysis setting.

    Takes every port count the run would build, not one, because
    `port_count` can itself be swept -- and a sweep where only some
    combinations have two ports is still a run that cannot finish.
    """
    wrong = sorted({int(count) for count in port_counts if int(count) != 2})
    if wrong:
        return (
            f"A driven run needs exactly 2 ports to report S21, but this run would "
            f"build combinations with port_count = {', '.join(str(c) for c in wrong)}. "
            f"Set port_count to 2, or use an Eigenmode run."
        )
    return None


SOLVER = register(Solver(
    value=DRIVEN_MODAL,
    label="Driven Modal -- S-parameters (S11 / S21)",
    help="Drives port 1 and sweeps frequency. Needs two ports, so it needs "
         "port_count = 2.",
    fields=FIELDS,
    result_fields=RESULT_FIELDS,
    create_setup=setup.create_setup,
    read_results=results.read_results,
    place_ports=ports.place_ports,
    default_axes=default_axes,
    validate=validate,
    task_id_prefix="driven",
))
