"""
Where the wire bonds go.

Bonds are drawn twice over: Altium places the pads they land on, and
HFSS builds the wires themselves. If each worked the positions out for
itself they would drift apart the first time a parameter changed, and a
bond landing next to its pad rather than on it is the kind of thing that
looks fine in a tree and is wrong in a field solve. So the layout is
computed once, here, and written to a file both sides read.

The arrangement:

  * The signal is bonded at each end of the feedline, with TWO wires
    rather than one. A single wire is an inductance in series with the
    line; two in parallel roughly halve it.

  * The ground is bonded all the way round the die, not only at the
    feedline ends. The die sits in a pocket surrounded by ground, and
    leaving three sides unbonded leaves the die's ground referenced only
    through the two ends.

  * Ground bonds keep clear of the CPW gap by `via_gnd_clearance` -- the
    same setback the via fence uses. Landing them at the gap edge puts
    metal where the field between line and ground is strongest.

There is deliberately no "number of bonds" parameter. The count follows
from the die size and the pad pitch, which is how it works on the bench:
you fit as many as the perimeter takes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

SIGNAL = "signal"
GROUND = "ground"

# Wires per signal bond, at each end of the line.
SIGNAL_WIRES = 2


@dataclass(frozen=True)
class Bond:
    """One wire, from a pad on the board to a pad on the die."""
    kind: str
    pcb_x: float
    pcb_y: float
    chip_x: float
    chip_y: float


def _frange(start: float, stop: float, step: float) -> List[float]:
    """Positions from `start` up to `stop`, inclusive of neither end."""
    out = []
    x = start
    while x < stop:
        out.append(x)
        x += step
    return out


def layout(values: Dict[str, float], gap: float) -> List[Bond]:
    """Every bond on the package, board pad to die pad.

    `gap` is the board's solved CPW gap; it decides how far out the
    ground bonds have to start at the feedline ends.
    """
    chip_half_l = values.get("chip_length", 6.3) / 2.0
    chip_half_s = values.get("chip_side", 6.3) / 2.0
    span = values.get("bonding_clearence", 1.0)
    pitch = values.get("bond_pad_pitch", 0.4)
    pad_w = values.get("bond_pad_width", 0.15)
    inset = values.get("bond_pad_length", 0.3)
    trace = values.get("cpw_trace_width", 0.2)
    clearance = values.get("via_gnd_clearance", 0.15)

    bonds: List[Bond] = []

    # --- the two feedline ends -----------------------------------------
    for sign in (-1.0, 1.0):
        pcb_x = sign * (chip_half_l + span)
        chip_x = sign * (chip_half_l - inset)

        # Signal: two wires side by side, both on the centre conductor.
        offset = trace / 4.0
        for wire in (-1.0, 1.0):
            bonds.append(Bond(SIGNAL, pcb_x, wire * offset, chip_x, wire * offset))

        # Ground: out beyond the gap, then every pitch to the die corner.
        first = trace / 2.0 + gap + clearance + pad_w / 2.0
        for y in _frange(first, chip_half_s, pitch):
            for side in (-1.0, 1.0):
                bonds.append(Bond(GROUND, pcb_x, side * y, chip_x, side * y))

    # --- the two long sides --------------------------------------------
    # Nothing here is near the line, so these are ground the whole way.
    for sign in (-1.0, 1.0):
        pcb_y = sign * (chip_half_s + span)
        chip_y = sign * (chip_half_s - inset)
        for x in _frange(-chip_half_l + pitch / 2.0, chip_half_l, pitch):
            bonds.append(Bond(GROUND, x, pcb_y, x, chip_y))

    return bonds


def write_file(path: str, bonds: List[Bond]) -> None:
    """Write the layout for Altium and HFSS to share.

    One record per line: `kind|pcb_x|pcb_y|chip_x|chip_y`, millimetres,
    relative to the board centre.
    """
    lines = [
        "# wire bonds, millimetres, relative to the board centre",
        "# kind|pcb_x|pcb_y|chip_x|chip_y",
    ]
    for b in bonds:
        lines.append(f"{b.kind}|{b.pcb_x!r}|{b.pcb_y!r}|{b.chip_x!r}|{b.chip_y!r}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def read_file(path: str) -> List[Bond]:
    bonds = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            kind, px, py, cx, cy = line.split("|")
            bonds.append(Bond(kind, float(px), float(py), float(cx), float(cy)))
    return bonds
