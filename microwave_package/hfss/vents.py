"""
The vent grooves: refused, because simulations stay vent-free.

Vents are cut in the box after manufacturing, not in the model. The model imports
the box as SolidWorks made it and adds nothing to it: it cuts no groove from the box
and puts no skin under the copper. AEDT refuses a groove under the bottom copper with
no skin there ("pcb_copper_bottom and box intersect"), so a vented run cannot be built
here. `constraints.check_vents` refuses such a run when it is made, and a refinement
or field image of an older vented run. `build_vents` refuses it again as the last
check, before anything is cut.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..design import constraints

logger = logging.getLogger("microwave_package")


def build_vents(hfss, frame, stack, values, log=logger.info) -> Optional[str]:
    """Refuse a combination with vents on. Returns None when `vents_on` is off.

    Nothing is cut from `box` and nothing is added to it (see the module docstring).
    """
    reason = constraints.check_vents(values)
    if reason:
        raise ValueError(reason)
    return None
