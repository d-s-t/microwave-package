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

from ..design import cavity as cavity_plan, venting

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
    _restore_copper_skin(hfss, frame, stack, values, vents, log)
    log(f"Vents: cut {', '.join(names)} -- {vents[0].width:g} x {vents[0].depth:g} mm, "
        f"cutoff {venting.cutoff_ghz(vents[0].width, vents[0].depth):.0f} GHz.")
    return names


def _restore_copper_skin(hfss, frame, stack, values, vents, log) -> List[str]:
    """Put the box back under the board's bottom copper over the pedestal vent.

    The bottom copper is drawn growing DOWN from the board's underside, into
    the floor's top `copper_thickness`, so everywhere else it lies wholly
    inside the box and the two do not "intersect". The groove, cut to the
    floor's top face, would leave the copper half in metal and half over
    air, and HFSS refuses that ("pcb_copper_bottom and box intersect").
    The copper is whole on the real board, so the groove's top
    `copper_thickness` is given back to the box as a skin under it, from
    the die's opening to the board's edge: the groove runs below the
    ground plane, which is what it is meant to do.

    The screw vents need none: the ground plane gives up the whole corner
    round each screw, so no copper lies over them.
    """
    thickness = stack.copper_thickness
    if thickness <= 0 or not any(v.name == "pedestal" for v in vents):
        return []
    half_side = values.get("pcb_side", 25.0) / 2.0
    inner = cavity_plan.plan(values).half_side
    vent = next(v for v in vents if v.name == "pedestal")
    u_min, _, u_max, _ = vent.rectangle()
    made: List[str] = []
    modeler = hfss.modeler
    for index, (v0, v1) in enumerate(((inner, half_side), (-half_side, -inner))):
        if v1 - v0 <= 0:
            continue
        origin, sizes = frame.box(u_min, v0, u_max, v1, stack.pcb_bottom - thickness, thickness)
        skin = modeler.create_box(origin=origin, sizes=sizes)
        skin.name = f"vent_skin_{index}"
        modeler.unite(["box", skin.name])
        made.append(skin.name)
    return made
