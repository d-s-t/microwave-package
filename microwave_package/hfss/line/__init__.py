"""
The line-impedance check: is the CPW actually 50 ohm?

The board's line is designed from an analytic calculation (`design/cpw.py`):
the gap that gives 50 ohm for a trace on a substrate. This solver does not
rely on it. It builds a short straight section of the line as it is BUILT --
substrate, trace, gap, ground, via posts, and the lid sitting on the ground
copper with its relief channel -- and asks HFSS for the impedance of the
mode its wave ports find, across the band, against a tolerance. It is the
independent referee for the calculation: a gap the formula is happy with
and HFSS is not is a line that will not be 50 ohm.

It is a "DrivenModal" design to HFSS and to the worker (`hfss_solution_type`),
a different question to this pipeline: it needs no SolidWorks (it draws its
own model) and no ports on connectors. A board with a chip has a second line,
the die's, checked the same way with `which_line` = chip.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..registry import Solver, register
from . import build, results
from .section import BOARD, CHIP

LINE_IMPEDANCE = "LineImpedance"

FIELDS: List[Dict[str, Any]] = [
    {
        "name": "which_line", "label": "Which line", "kind": "select", "default": BOARD,
        "options": [
            {"value": BOARD, "label": "Board",
             "help": "The PCB's CPW, from a connector to the chip: substrate, trace, gap, ground "
                     "below and the lid over it."},
            {"value": CHIP, "label": "Chip",
             "help": "The die's own CPW (silicon, its own trace and gap, no ground below). Needs a "
                     "board with a chip. The depth of air under the die and of the lid's cover over it "
                     "are the least certain numbers in the model."},
        ],
    },
    {"name": "section_length_mm", "label": "Section length (mm)", "kind": "float", "default": 3.0,
     "help": "A few millimetres is enough: the ports find the line's mode, so this is not a length "
             "that has to resemble the board. Keep it under a half-wavelength at the top of the band "
             "(about 4.5 mm at 26 GHz on 5880): at a half-wavelength the impedance cannot be derived "
             "from the S-parameters and those points are dropped."},
    {"name": "section_half_width_mm", "label": "Section half-width (mm)", "kind": "float", "default": 1.5,
     "help": "How far either side of the trace the section reaches. Its walls are perfect conductors, "
             "so it must be wide enough that they do not load the line and clear of the lid's channel; "
             "much wider and the air and substrate beside the via fence resonate inside the band "
             "(a 3 mm half-width collapsed the impedance above 14 GHz)."},
    {"name": "line_gap_mm", "label": "Gap by hand (mm, 0 = as the layout has it)", "kind": "float", "default": 0.0,
     "help": "Build the section with this gap instead of the layout's: to see what HFSS makes of a "
             "particular gap, or to find the one it wants. The trace and everything else are the run's."},
    {"name": "line_f_start_ghz", "label": "Band start (GHz)", "kind": "float", "default": 1.0},
    {"name": "line_f_stop_ghz", "label": "Band stop (GHz)", "kind": "float", "default": 26.0},
    {"name": "line_points", "label": "Frequency points", "kind": "int", "default": 53,
     "help": "Reported by the interpolating sweep; far fewer are solved."},
    {"name": "line_delta_s", "label": "Max delta S per pass", "kind": "float", "default": 0.02},
    {"name": "line_z0_ohm", "label": "Target impedance (ohm)", "kind": "float", "default": 50.0},
    {"name": "line_tolerance_ohm", "label": "Tolerance (+/- ohm)", "kind": "float", "default": 2.5,
     "help": "The line passes when its impedance is inside target +- tolerance at EVERY frequency."},
    {"name": "line_port_ohm", "label": "Port reference (ohm)", "kind": "float", "default": 50.0,
     "help": "What the ports are renormalised to. The impedance is derived from the section's "
             "S-parameters against this reference, and from the port's own Zo when HFSS reports it."},
    {"name": "line_modes", "label": "Port modes", "kind": "int", "default": 1, "max": 4},
]

RESULT_FIELDS: Dict[str, str] = {
    "z0_ohm": "Zo from S-parameters, real part (ohm)",
    "z0_im_ohm": "Zo from S-parameters, imaginary part (ohm)",
    "z0_abs_ohm": "|Zo| from S-parameters (ohm)",
    "zport_ohm": "Port Zo reported by HFSS, real part (ohm)",
    "zport_im_ohm": "Port Zo reported by HFSS, imaginary part (ohm)",
    "target_ohm": "Target impedance (ohm)",
}


def default_axes(df, swept_columns: List[str]) -> Dict[str, Optional[str]]:
    """Impedance across the band, one curve per combination."""
    return {
        "x": "freq_ghz",
        "y": "z0_ohm" if "z0_ohm" in df.columns else "s11_db",
        "color": "combo_id" if "combo_id" in df.columns else None,
    }


def _create_setup(hfss, dimensions_txt_content, settings, log=None):
    return build.create_setup(hfss, dimensions_txt_content, settings, log=log or (lambda _m: None))


SOLVER = register(Solver(
    value=LINE_IMPEDANCE,
    label="Line impedance -- is the CPW 50 ohm? (HFSS check of the built line)",
    help="Solves a short section of the board's CPW as built, lid included, and reports its "
         "impedance across the band against a tolerance. An independent check of the 50 ohm gap "
         "calculation. Cheap: a few millimetres of line, no connectors.",
    fields=FIELDS,
    result_fields=RESULT_FIELDS,
    create_setup=_create_setup,
    read_results=results.read_results,
    default_axes=default_axes,
    task_id_prefix="line",
    hfss_solution_type="DrivenModal",
    build_model=build.build_model,
    needs_solid=False,
))
