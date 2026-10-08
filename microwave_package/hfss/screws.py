"""
The corner screws in the HFSS model: the holes they sit in, and the screws.

Everything geometric is decided by `design.screws`; this only builds it.
The holes are cut here, in the imported box and lid_inside and in the
board built by `copper_geometry`, rather than in the SolidWorks parts: the
parts are binaries driven by the equations file, and the model has to hold
the features whatever the parts do. The SolidWorks parts need the same
holes for the real thing -- see the README.

Three deliberate choices:

* The hole in the box floor is cut to the screw's own diameter, not the
  tap drill. The thread is a surface this model does not resolve, and a
  screw overlapping the metal it is threaded into is an overlap of two
  conductors that validation refuses.
* The lid's countersink is cut to exactly the head's cone, so the head
  sits flush with the lid's top face and shares its surface; below the
  cone the lid's clearance hole is wider than the shank, leaving the small
  air gap there is on the bench.
* The screw is a cylinder and a cone, not the vendor's STEP. The STEP's
  threads and hex socket are detail below what a mesh here can use, and an
  imported solid would have to be placed and rotated onto four positions.
  Its dimensions were read off the STEP files and agree: shank 2.0 mm,
  head 3.8 mm across on a 90 degree cone, flat top at the stated length.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from ..design import screws as screw_plan

logger = logging.getLogger("microwave_package")

# Lets a cutting tool stand proud of the face it cuts through, so the cut
# does not leave a skin of zero thickness on that face.
OVERSHOOT = 0.05


def _hole_tool(modeler, frame, u, v, radius, bottom, top, name):
    """A cylinder along the up axis, from `bottom` to `top`."""
    tool = modeler.create_cylinder(
        orientation="Y", origin=frame.point(u, v, bottom),
        radius=radius, height=top - bottom,
    )
    tool.name = name
    return tool.name


def _countersink_tool(modeler, frame, u, v, screws, lid_top, lid_bottom, name):
    """The lid's hole: the clearance cylinder all the way through, and the
    90 degree cone from the head's diameter at the top face down to the
    clearance diameter."""
    clear_r = screws.clearance_diameter / 2.0
    head_r = screws.head_diameter / 2.0
    depth = screws.countersink_depth
    bore = _hole_tool(modeler, frame, u, v, clear_r,
                      lid_bottom - OVERSHOOT, lid_top, f"{name}_bore")
    cone = modeler.create_cone(
        orientation="Y", origin=frame.point(u, v, lid_top - depth),
        bottom_radius=clear_r, top_radius=head_r, height=depth,
    )
    cone.name = f"{name}_cone"
    modeler.unite([bore, cone.name])
    return bore


def build_screws(hfss, frame, stack, values, copper_targets: List[str],
                 log=logger.info) -> Optional[List[str]]:
    """Cut the holes and stand the four screws up. Returns their names, or
    None when `screws_on` is off.

    `copper_targets` are the board's copper bodies, which the keep-out is
    cut out of. Raises ValueError, naming what to change, if the lid is too
    thin for the head or no standard screw fits the stack.
    """
    screws = screw_plan.plan(values)
    if screws is None:
        return None
    modeler = hfss.modeler

    lid = modeler["lid_inside"].bounding_box
    lid_bottom, lid_top = lid[1], lid[4]
    if lid_top - lid_bottom < screws.min_lid_thickness:
        raise ValueError(
            f"lid_inside is {lid_top - lid_bottom:.3f} mm thick, thinner than the "
            f"{screws.head_height:.2f} mm countersunk head -- the head would come through the "
            f"lid. Thicken the lid where the screws are"
        )
    # The thread bites into the floor, whose top face is wall_width above
    # the box's underside. The board may sit higher than that (the pedestal
    # lifts it), and the screw crosses the gap in air.
    box_bottom = modeler["box"].bounding_box[1]
    floor = values.get("wall_width", 1.0)
    floor_top = box_bottom + floor
    length = screw_plan.choose_length(lid_top - floor_top, screws.engagement, floor)
    tip = lid_top - length
    log(f"Corner screws: M2x{length} (ISO 10642), flat top at {lid_top:.3f} mm, tip at "
        f"{tip:.3f} mm in a {floor:.3f} mm floor; lid {lid_top - lid_bottom:.3f} mm thick.")

    shank_r = screws.shank_diameter / 2.0
    head_r = screws.head_diameter / 2.0
    keepout_r = screws.keepout_diameter / 2.0
    clear_r = screws.clearance_diameter / 2.0
    # Where the head's cone meets the shank, measured from the flat top.
    cone_base = lid_top - screws.head_height

    names: List[str] = []
    for index, (u, v) in enumerate(screws.positions, start=1):
        tag = f"screw_{index}"

        # box floor: the screw's own diameter, from below the tip up to the
        # floor's top face and a little beyond.
        hole = _hole_tool(modeler, frame, u, v, shank_r, tip,
                          stack.pcb_bottom, f"{tag}_floor_hole")
        modeler.subtract("box", [hole], keep_originals=False)

        # board: the clearance hole through the substrate
        pcb_hole = _hole_tool(modeler, frame, u, v, clear_r,
                              stack.pcb_bottom - OVERSHOOT, stack.pcb_top + OVERSHOOT,
                              f"{tag}_pcb_hole")
        modeler.subtract("pcb_substrate", [pcb_hole], keep_originals=False)

        # board copper: the keep-out, through both layers
        if copper_targets:
            span = max(stack.copper_thickness, OVERSHOOT)
            keepout = _hole_tool(modeler, frame, u, v, keepout_r,
                                 stack.pcb_bottom - span - OVERSHOOT,
                                 stack.pcb_copper_top + OVERSHOOT, f"{tag}_keepout")
            modeler.subtract(list(copper_targets), [keepout], keep_originals=False)

        # lid: clearance bore and countersink
        sink = _countersink_tool(modeler, frame, u, v, screws, lid_top, lid_bottom, f"{tag}_sink")
        modeler.subtract("lid_inside", [sink], keep_originals=False)

        # the screw: shank from the tip, cone head to the flat top
        shank = modeler.create_cylinder(
            orientation="Y", origin=frame.point(u, v, tip),
            radius=shank_r, height=cone_base - tip,
        )
        head = modeler.create_cone(
            orientation="Y", origin=frame.point(u, v, cone_base),
            bottom_radius=shank_r, top_radius=head_r, height=screws.head_height,
        )
        modeler.unite([shank.name, head.name])
        shank.name = tag
        names.append(tag)

    log(f"Cut the screw holes in box, lid_inside, pcb_substrate and the copper, and built "
        f"{', '.join(names)}.")
    return names
