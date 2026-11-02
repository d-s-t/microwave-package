"""
The Eigenmode solver: what a cavity resonates at.

Source-free -- no excitation, no ports, no frequency sweep. HFSS is given
a starting frequency and asked for the first N resonances above it, and
the answer is a frequency and a Q per mode, plus whatever the field looks
like on the chip at each one.

This was the only thing the pipeline did before driven runs existed, so
it is registered first and is therefore the default for a run that does
not say.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..registry import Solver, register
from . import results, setup, terminations

EIGENMODE = "Eigenmode"

FIELDS: List[Dict[str, Any]] = [
    {"name": "modes", "label": "Modes", "kind": "int", "default": 20, "max": 20},
    {"name": "max_delta_f", "label": "Max delta freq per pass (%)", "kind": "float", "default": 3.0},
    {
        "name": "min_freq_safety_factor", "label": "Starting-frequency safety factor",
        "kind": "float", "default": 0.1,
        "help": "The cavity's estimated lowest resonance is multiplied by "
                "this to pick MinimumFrequency, so the sweep starts below "
                "the first real mode rather than on top of it.",
    },
    {
        "name": "connector_termination_ohm", "label": "Connector termination (ohm, 0 = off)",
        "kind": "float", "default": 0.0,
        "help": "Terminates each SMP connector in this resistance across its air bore, "
                "so the modes are found with the connector matched rather than shorted. "
                "0 leaves the bore's top as HFSS's perfect-E outer boundary, which shorts "
                "each pin to its shell. 50 is the reference impedance the driven runs "
                "terminate in.",
    },
]

RESULT_FIELDS: Dict[str, str] = {
    "freq_ghz": "Frequency (GHz)",
    "q": "Q factor",
    "chip_mag_E_max": "Chip max |E| (V/m)",
    "chip_mag_E_mean": "Chip mean |E| (V/m)",
    "vacuum_mag_E_max": "Vacuum max |E| (V/m)",
    "vacuum_mag_E_mean": "Vacuum mean |E| (V/m)",
}


def post_processing_ops(analysis_settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The per-mode field work the client's post-processing step should do.

    These are per-MODE quantities -- "where is the energy in this
    resonance" -- which is why they belong to this solver and not to the
    pipeline: a solution type without modes has nothing to run them
    against.
    """
    return [
        # The four numbers the run page plots. The chip's are taken on its
        # top face only -- the surface the circuit is on -- while the
        # vacuum's cover the whole cavity volume.
        {"op": "field_statistics", "params": {
            "object_key": "chip", "quantity": "Mag_E",
            "region": "top_surface", "key": "chip_mag_E",
        }},
        {"op": "field_statistics", "params": {
            "object_key": "vacuum", "quantity": "Mag_E",
            "region": "volume", "key": "vacuum_mag_E",
        }},
        # Best-effort picture of that same chip face.
        {"op": "field_plot_surface", "params": {"object_key": "chip", "quantity": "Mag_E"}},
    ]


def default_axes(df, swept_columns: List[str]) -> Dict[str, Optional[str]]:
    """An eigenmode sweep reads across combinations.

    The parameter you varied along the bottom, the frequency it produced
    up the side, one point per mode -- which is the question the run was
    started to answer.
    """
    return {
        "x": swept_columns[0] if swept_columns else "combo_id",
        "y": "freq_ghz",
        "color": "chip_mag_E_max" if "chip_mag_E_max" in df.columns else (
            swept_columns[1] if len(swept_columns) > 1 else None),
    }


SOLVER = register(Solver(
    value=EIGENMODE,
    label="Eigenmode -- resonant frequencies and Q",
    help="Source-free. Finds what the cavity resonates at. No ports.",
    fields=FIELDS,
    result_fields=RESULT_FIELDS,
    create_setup=setup.create_setup,
    place_ports=terminations.place_terminations,
    read_results=results.read_results,
    post_processing_ops=post_processing_ops,
    default_axes=default_axes,
    task_id_prefix="eigenmode",
))
