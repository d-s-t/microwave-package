"""
A matched load across each SMP connector, for an Eigenmode run.

An eigenmode run has no ports, so nothing ends the connectors. HFSS makes
the top of each connector bead an outer perfect-E boundary, which shorts each
pin to its shell, and the modes it finds are then not the ones the driven
runs see: those terminate each connector in the port impedance (driven/ports.py).

`connector_termination_ohm` -- 0, the default, is off -- leaves the bore above
each bead open at its top (`_bore_section` without its cap) and puts an
impedance boundary on that top face. A TEM wave in the bore then sees a load
of that many ohms, so the modes are found with the connectors terminated. A
boundary is not a port, so nothing here is reported as one.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List

from ..registry import PortPlacementError

logger = logging.getLogger("microwave_package")


def place_terminations(hfss, layout, analysis_settings: Dict[str, Any], log=logger.info) -> List[str]:
    """Put a resistive load across each connector's bore, if the run asks for one.

    Returns [] whatever it does: a termination is not a port, so there is no
    name to report. This is the Eigenmode solver's `place_ports`.
    """
    ohm = float(analysis_settings.get("connector_termination_ohm") or 0.0)
    if not math.isfinite(ohm) or ohm < 0:
        raise PortPlacementError(
            f"connector_termination_ohm is {ohm:g}. A connector's termination is a passive "
            "load, so the setting is 0 (off) or a positive resistance."
        )
    if ohm == 0:
        return []
    if layout is None:
        log(
            "WARNING: connector_termination_ohm is set, but this combination has no layout, so "
            "it has no connectors to terminate -- the modes are those of the model as built."
        )
        return []

    # Imported here, not at the top: importing the driven package registers
    # its solver, and this one has to register first to stay the default
    # (see hfss/__init__.py).
    from ..driven import ports as driven_ports

    beads = sorted(
        name for name in hfss.modeler.object_names
        if name.startswith("connector_") and name.endswith("_dielectric")
    )
    if not beads:
        raise PortPlacementError(
            "No connector bead bodies (connector_N_dielectric) in the model, so there is no "
            "coax cross-section to terminate. The SMP model either did not import or did not "
            "split into three bodies -- see the build log above."
        )
    vacuum = next((n for n in hfss.modeler.object_names if n.lower() == "vacuum"), None)

    for bead in beads:
        index = bead.split("_")[1]
        centre = f"connector_{index}_center"
        if centre not in hfss.modeler.object_names:
            raise PortPlacementError(
                f"{bead} has no {centre} beside it, so there is no centre conductor to terminate."
            )
        name = _termination_name(index)
        air = driven_ports._bore_section(hfss, bead, centre, vacuum, name, log=log, cap=False)
        face = driven_ports._bore_top_face(hfss, air)
        inner, outer = driven_ports._annulus_radii(face, air, log=log)
        zs = surface_impedance(ohm, inner, outer)
        log(
            f"{name}: impedance boundary on {air}'s top face (id {face.id}): {zs:.3f} ohm per "
            f"square, a {ohm:g} ohm load across r={inner:.3f}..{outer:.3f} mm."
        )
        _assign_impedance(hfss, face, name, zs, log=log)
    return []


def surface_impedance(ohm: float, inner: float, outer: float) -> float:
    """The impedance boundary's resistance per square, in ohms, for a load of `ohm`.

    In a coax of these radii a TEM wave has E/H = ohm * 2*pi / ln(outer/inner)
    across the bore -- E radial, H azimuthal -- which is the ratio an impedance
    boundary on the bore's top face asks for, so the face reads as a load of
    `ohm`. For a 0.3 mm pin in a 1.0 mm bore, 50 ohm is 260.9 ohm per square.
    """
    return ohm * 2.0 * math.pi / math.log(outer / inner)


def _termination_name(index: str) -> str:
    """`connector_term_a` for connector 1 and so on: letters, as the driven
    terminations' names are, so a number never reads as one of the run's ports."""
    return f"connector_term_{chr(ord('a') + int(index) - 1)}"


def _assign_impedance(hfss, face, name: str, zs: float, log) -> None:
    """Put the impedance boundary on `face`: PyAEDT's helper first, then the
    native call it wraps.

    The helper returns False in some eigenmode designs, so the native call is
    the fallback. If both refuse, the model is refused: a connector left
    unterminated is shorted, and the result would be wrong without a sign of it.
    """
    try:
        # The face itself, not its id: PyAEDT reads a bare int as an object id,
        # which can name a different body.
        made = hfss.assign_impedance_to_sheet(
            assignment=[face], name=name, resistance=zs, reactance=0.0, is_infinite_ground=False,
        )
        helper = "returned False"
    except Exception as exc:
        made, helper = False, f"raised {exc}"
    if made is not False:
        return
    log(f"{name}: PyAEDT's impedance helper {helper}; trying the native AssignImpedance.")
    try:
        if hfss.oboundary.AssignImpedance(_native_impedance(name, face, zs)) is not False:
            return
        outcome = "returned False"
    except Exception as exc:
        outcome = f"raised {exc}"
    raise PortPlacementError(
        f"Could not put the impedance boundary {name} on face {face.id} (centre {face.center}): "
        f"PyAEDT's helper {helper}, and the native AssignImpedance {outcome}. Left unterminated, "
        "the connector would be shorted, so the model is not saved."
    )


def _native_impedance(name: str, face, zs: float) -> list:
    """The property list `assign_impedance_to_sheet` builds for the same
    boundary, flattened the way PyAEDT flattens it for the native API."""
    return [
        "NAME:" + name,
        "Faces:=", [face.id],
        "Resistance:=", str(zs),
        "Reactance:=", "0.0",
        "InfGroundPlane:=", False,
    ]
