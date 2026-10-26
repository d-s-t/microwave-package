"""
The launch study: how good is ONE SMP connector launch, on its own?

The full package has a connector at each end of a 50 mm board, and its S21
ripples because the two launches reflect and the board between them is a
resonator. This solver cuts that apart: one connector and a few millimetres of
the board behind it, matched at the cut by a 50 ohm CPW wave port, so S11 is
the launch and nothing else. It is the cheap model the launch geometry is
swept on (trace width, pad, taper, the anti-pad void, the lid over it).

It is a "DrivenModal" design to HFSS and to the worker (`hfss_solution_type`);
to this pipeline it is a different question, like the line-impedance check:
it draws its own model (`needs_solid=False`: no SolidWorks) and its copper
(`needs_altium=False`: no Altium either, `geometry.py` draws it), and it is
never half of an Eigenmode pair.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..registry import Solver, register
from . import build, results

LAUNCH_STUDY = "LaunchStudy"

FIELDS: List[Dict[str, Any]] = [
    {"name": "launch_trace_length_mm", "label": "Board behind the launch (mm)", "kind": "float", "default": 8.0,
     "help": "How much 50 ohm CPW the model carries between the launch's inner edge and the cut. "
             "Its far end is the matched CPW port."},
    {"name": "launch_half_width_mm", "label": "Board half-width (mm)", "kind": "float", "default": 3.5,
     "help": "Either side of the line. Must clear the connector's 2.6 mm ground land by 0.3 mm; "
             "much wider and the board beside the via fence resonates in the band."},
    {"name": "launch_f_start_ghz", "label": "Band start (GHz)", "kind": "float", "default": 2.0},
    {"name": "launch_f_stop_ghz", "label": "Band stop (GHz)", "kind": "float", "default": 20.0},
    {"name": "launch_points", "label": "Frequency points", "kind": "int", "default": 73,
     "help": "On a 0.25 GHz grid from 2 to 20 GHz; the time-domain read needs an even grid."},
    {"name": "launch_delta_s", "label": "Max delta S per pass", "kind": "float", "default": 0.02},
    {"name": "launch_z0_ohm", "label": "Line impedance to solve the gap for (ohm)", "kind": "float",
     "default": 50.0},
    {"name": "launch_port_ohm", "label": "Port reference (ohm)", "kind": "float", "default": 50.0},
    {"name": "launch_modes", "label": "CPW port modes", "kind": "int", "default": 1, "max": 4},
]

RESULT_FIELDS: Dict[str, str] = {
    "s11_re": "S11, real part",
    "s11_im": "S11, imaginary part",
}


def default_axes(df, swept_columns: List[str]) -> Dict[str, Optional[str]]:
    """S11 across the band, one curve per combination."""
    return {
        "x": "freq_ghz",
        "y": "s11_db" if "s11_db" in df.columns else None,
        "color": "combo_id" if "combo_id" in df.columns else None,
    }


def _create_setup(hfss, dimensions_txt_content, settings, log=None):
    return build.create_setup(hfss, dimensions_txt_content, settings, log=log or (lambda _m: None))


SOLVER = register(Solver(
    value=LAUNCH_STUDY,
    label="Launch study -- one SMP launch and a matched line (S11 of the launch alone)",
    help="One connector on a short board, cut with a 50 ohm CPW wave port, so S11 is the launch "
         "reflection without the standing wave. Sweep the trace width, pad, taper, anti-pad void "
         "or lid relief to see what lowers it. Cheap: a few millimetres of board, one connector; "
         "no SolidWorks and no Altium.",
    fields=FIELDS,
    result_fields=RESULT_FIELDS,
    create_setup=_create_setup,
    read_results=results.read_results,
    default_axes=default_axes,
    task_id_prefix="launch",
    hfss_solution_type="DrivenModal",
    build_model=build.build_model,
    needs_solid=False,
    needs_altium=False,
    standalone=True,
))
