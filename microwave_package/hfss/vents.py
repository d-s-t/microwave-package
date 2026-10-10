"""
The vent grooves in the HFSS model: cut in the imported box.

Everything geometric is decided by `design.venting`; this only cuts it.
box.SLDPRT carries the same grooves (its `vents_on` features), so for a
box built by the current part this subtracts nothing new: it is there so
the model has them whatever state the part is in, the same reason
`screws` cuts its own holes.

The groove is cut down from the floor's top face, which is the board's
underside, and runs under the board and through the wall. Past the
board's edge the groove is inside the box's own wall, where the vacuum
body does not reach, so in the solved model it ends against the wall: a
dead-end slot far below cutoff, which is the conservative reading of one
that goes on to the outside.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from ..design import venting

logger = logging.getLogger("microwave_package")


def build_vents(hfss, frame, stack, values, log=logger.info) -> Optional[List[str]]:
    """Cut every vent into `box`. Returns what was cut, or None when
    `vents_on` is off or there is nothing to cut."""
    vents = venting.plan(values)
    if not vents:
        return None
    reason = venting.check(values)
    if reason:
        raise ValueError(reason)
    modeler = hfss.modeler
    top = stack.pcb_bottom
    names: List[str] = []
    for vent in vents:
        u_min, v_min, u_max, v_max = vent.rectangle()
        origin, sizes = frame.box(u_min, v_min, u_max, v_max, top - vent.depth, vent.depth)
        tool = modeler.create_box(origin=origin, sizes=sizes)
        tool.name = f"vent_{vent.name}_tool"
        modeler.subtract("box", [tool.name], keep_originals=False)
        names.append(vent.name)
    log(f"Vents: cut {', '.join(names)} -- {vents[0].width:g} x {vents[0].depth:g} mm, "
        f"cutoff {venting.cutoff_ghz(vents[0].width, vents[0].depth):.0f} GHz.")
    return names
