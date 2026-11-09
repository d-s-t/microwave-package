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

# How far the copper skin reaches into the groove's walls, so it fuses with the box.
SKIN_OVERLAP = 0.1
# How far it runs on into the wall past the board's edge. Ending exactly at the
# wall's inner face leaves the skin touching the wall along one edge only,
# which AEDT's union refuses ("Error in uniting objects"); inside the wall it
# overlaps solid metal.
WALL_OVERLAP = 0.2
# The skin is this many copper thicknesses deep. One would end exactly on the
# copper's own underside, and AEDT's validation calls two bodies whose faces
# coincide like that intersecting (the copper wholly inside the box, with no
# face in common, passes).
SKIN_DEPTH = 2.0
# The keep-out circle in the copper is a polygon whose chords lie a few microns
# inside the true circle, so copper starts a hair before the circle's radius.
# The screw skin starts this much earlier so none of it is left over air.
KEEPOUT_SLACK = 0.2


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


def build_recess_skin(hfss, frame, stack, values, log=logger.info) -> Optional[str]:
    """Put the box back under the board's whole bottom copper over its recess.

    Where the bottom copper is whole -- a bare board has no opening, and a
    blind pocket leaves its back plane whole under the die -- it lies over
    the pedestal recess as well as the rim. As with a vent groove (see
    `_restore_copper_skin`), the copper is drawn 35 um down into the floor, so
    the recess would leave it half over air at its walls. The copper is
    whole on the real board, so the recess's top two copper thicknesses are
    given back to the box as a skin, and the air pocket under the board sits
    below that skin, closed by the solid ground plane on every side.

    Returns the skin's name, or None when there is nothing to skin: a die board
    with a through opening (its copper has the opening over the recess and sits
    on the rim), no recess, or a copper layer drawn as a sheet.
    """
    from ..design import parameter_meta, pedestal

    thickness = stack.copper_thickness
    if thickness <= 0:
        return None
    # A through opening takes the copper out over the recess. A bare board has
    # no opening, so its plan is never through and its copper stays whole.
    if cavity_plan.plan(values).through:
        return None
    if values.get("pedestal_depth", 0.0) <= 0:
        return None
    half = pedestal.recess_size(values) / 2.0 + SKIN_OVERLAP
    origin, sizes = frame.box(-half, -half, half, half,
                              stack.pcb_bottom - SKIN_DEPTH * thickness, SKIN_DEPTH * thickness)
    skin = hfss.modeler.create_box(origin=origin, sizes=sizes)
    skin.name = "recess_skin"
    hfss.modeler.unite(["box", skin.name])
    kind = "Bare board" if parameter_meta.is_bare_board(values) else "Blind pocket"
    log(f"{kind}: the bottom copper over the pedestal recess is carried on a "
        f"{SKIN_DEPTH * thickness * 1000:g} um skin of the box.")
    return skin.name


def _restore_copper_skin(hfss, frame, stack, values, vents, log) -> List[str]:
    """Put the box back under the board's bottom copper over each groove.

    The bottom copper is drawn growing DOWN from the board's underside, into
    the floor's top `copper_thickness`, so everywhere else it lies wholly
    inside the box and the two do not "intersect". A groove cut to the
    floor's top face would leave the copper half in metal and half over
    air, and HFSS refuses that ("pcb_copper_bottom and box intersect").
    The copper is whole on the real board, so the groove's top
    two copper thicknesses are given back to the box as a skin under it, wherever
    copper lies over the groove: for the pedestal vent from the die's opening
    to the board's edge (all the way across it under a blind pocket, whose back
    plane is whole), for a screw vent from the keep-out circle round
    the screw to the board's edge (inside the circle there is no copper).
    The groove runs below the ground plane, which is what it is meant to do.
    The skin floats above the groove's floor, so it is joined to the box
    through the groove's walls.
    """
    from ..design import screws as screw_plan

    thickness = stack.copper_thickness
    if thickness <= 0:
        return []
    reach = values.get("pcb_side", 25.0) / 2.0 + WALL_OVERLAP
    opening = cavity_plan.plan(values)
    inner = opening.half_side
    screws = screw_plan.plan(values)
    keepout = screws.keepout_diameter / 2.0 if screws is not None else 0.0
    modeler = hfss.modeler
    made: List[str] = []
    for vent in vents:
        if vent.name == "pedestal":
            if inner <= 0:      # no die opening to start from: nothing is over this groove
                continue
            if opening.through:
                spans = ((inner, reach), (-reach, -inner))
            else:
                # A blind pocket leaves the back plane whole under the die, so the
                # copper lies over the groove all the way across it, not only past
                # the opening's edge.
                spans = ((-reach, reach),)
        else:
            sign = 1.0 if vent.end[1] > 0 else -1.0
            start = abs(vent.start[1]) + keepout - KEEPOUT_SLACK
            spans = (tuple(sorted((sign * start, sign * reach))),)
        u_min, _, u_max, _ = vent.rectangle()
        for index, (v0, v1) in enumerate(spans):
            if v1 - v0 <= 0:
                continue
            # a skin as wide as the groove only touches its walls edge to edge, and
            # a union of bodies that merely touch fails; it reaches into them
            origin, sizes = frame.box(u_min - SKIN_OVERLAP, v0, u_max + SKIN_OVERLAP, v1,
                                      stack.pcb_bottom - SKIN_DEPTH * thickness, SKIN_DEPTH * thickness)
            skin = modeler.create_box(origin=origin, sizes=sizes)
            skin.name = f"vent_skin_{vent.name}_{index}"
            modeler.unite(["box", skin.name])
            made.append(skin.name)
    return made
