"""The PCB and the die, for the 3D view in the browser.

The box and the lid come from their own STEP files. The PCB and the die do
not: the HFSS model builder makes them from dimensions.txt. So the view
makes them from the same file with the same functions -- `layout_stack`,
`cavity.plan`, `pcb_outline` -- rather than drawing its own, which would
drift the first time a parameter moved.

The answer is a footprint in board coordinates (u along the feedline, v
across it, centred on the board, as `copper_geometry.Frame` has them) and
heights along the box's up axis, which the browser turns into solids and
places at the box's own centre. Nothing here reads STEP or runs AEDT.
"""
from __future__ import annotations

from typing import Dict, List, Mapping

from ..design import cavity as cavity_plan
from ..design import pcb_outline
from . import model_builder


def _rectangle(half_length: float, half_side: float) -> List[List[float]]:
    """A rectangle about the board centre, counter-clockwise."""
    return [[half_length, half_side], [-half_length, half_side],
            [-half_length, -half_side], [half_length, -half_side]]


def board(values: Mapping[str, float]) -> Dict:
    """The board and the die for one combination's equation values.

    Returns a JSON-ready dict:

      centre_fallback  [x, z] of the board centre in the box, from the
                       equations; used only when the box cannot be measured
      lid_bottom       the height the lid's underside sits at (on the copper)
      pcb              outline   rounded board, counter-clockwise
                       opening   the die's opening as a rectangle, or None
                                 for a bare board (no die, no opening)
                       bottom,
                       top       heights of the board's two faces
                       floor_top height of the floor under a blind pocket,
                                 or None for a through cutout (and a bare board)
      die              outline, bottom, top -- None for a bare board
    """
    stack = model_builder.layout_stack(values)
    centre_x, centre_z = model_builder.board_centre_fallback(values)
    radius = pcb_outline.corner_radius_of(values)
    outline = pcb_outline.outline_points(
        values.get("pcb_length", 26.0) / 2.0, values.get("pcb_side", 10.0) / 2.0, radius)
    pcb = {
        "outline": [[u, v] for u, v in outline],
        "opening": None,
        "bottom": stack.pcb_bottom,
        "top": stack.pcb_top,
        "floor_top": None,
    }
    die = None
    # The opening is the one the Altium generator and the HFSS model cut.
    # A bare board has none, and `plan` says so with an empty opening.
    opening = cavity_plan.plan(values)
    if not opening.is_empty:
        pcb["opening"] = _rectangle(opening.half_length, opening.half_side)
        if not opening.through:
            pcb["floor_top"] = stack.pcb_top - opening.depth
        die = {
            "outline": _rectangle(values.get("chip_length", 6.3) / 2.0,
                                  values.get("chip_side", 6.3) / 2.0),
            "bottom": stack.chip_bottom,
            "top": stack.chip_top,
        }
    return {
        "centre_fallback": [centre_x, centre_z],
        "lid_bottom": stack.pcb_copper_top,
        "pcb": pcb,
        "die": die,
    }
