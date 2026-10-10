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

  * The board-side fingers sit midway between the edge of the PCB's
    opening and the limit `bonding_clearence` sets for the lid -- not
    on that limit, and not measured from the die's own edge, which is
    inside the hole. See `constraints.finger_offset`.

  * Every pad lands WHOLLY on the die's copper, and no two pads touch.
    See `_row` for why that needs saying.

There is deliberately no "number of bonds" parameter. The count follows
from the die size and the pad pitch, which is how it works on the bench:
you fit as many as the perimeter takes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import parameter_meta
from .constraints import CHIP_COPPER_INSET, chip_trace, finger_offset, ground_bond_start

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


def _frange(start: float, limit: float, step: float) -> List[float]:
    """Positions from `start`, none of them past `limit`."""
    out = []
    x = start
    while x <= limit + 1e-9:
        out.append(x)
        x += step
    return out


def _row(limit: float, pitch: float) -> List[float]:
    """A row of positions on `pitch`, centred, none of them past `limit`.

    Centred rather than started from one end: the die side is rarely a
    whole number of pitches, and a row grown from one end puts the
    leftover at the other, so the pads sit off-centre and the last one
    hangs over the edge.
    """
    if limit < 0 or pitch <= 0:
        return []
    count = int(math.floor(2.0 * limit / pitch + 1e-9)) + 1
    first = -pitch * (count - 1) / 2.0
    return [first + i * pitch for i in range(count)]


def layout(values: Dict[str, float], gap: float, chip_gap: Optional[float] = None) -> List[Bond]:
    """Every bond on the package, board pad to die pad.

    `gap` is the board's solved CPW gap, and `chip_gap` the die's (which has
    its own trace, `chip_trace_width`); the wider of the two lines decides how
    far out the ground bonds have to start at the feedline ends, and each
    signal pad sits on its own line's centre conductor.

    The bare board (`no_chip`) has no die to bond to, so no bonds.
    """
    if parameter_meta.is_bare_board(values):
        return []

    # `span` below is the finger's own offset from the die's edge. It is
    # NOT bonding_clearence: that is where the lid has to stay clear to,
    # and the finger goes midway between the opening and it.
    chip_half_l = values.get("chip_length", 6.3) / 2.0
    chip_half_s = values.get("chip_side", 6.3) / 2.0
    span = finger_offset(values.get("bonding_clearence", 1.0),
                         values.get("cavity_clearance", 0.1))
    pitch = values.get("bond_pad_pitch", 0.4)
    pad_w = values.get("bond_pad_width", 0.15)
    inset = values.get("bond_pad_length", 0.3)
    trace = values.get("cpw_trace_width", 0.2)
    die_trace = chip_trace(values)

    bonds: List[Bond] = []

    # Where the die's copper actually stops. The substrate is the die's
    # real size, but its metal is pulled in by CHIP_COPPER_INSET, so
    # this -- not the die edge -- is what a pad has to stay inside.
    copper_half_l = chip_half_l - CHIP_COPPER_INSET
    copper_half_s = chip_half_s - CHIP_COPPER_INSET

    # A pad is drawn `inset` along x by `pad_w` along y whichever edge it
    # sits on, so a row's own bound depends on which way it runs.
    #
    # Rows used to run to the die edge in both directions, which put the
    # last pad of every row over the edge of the die's copper: the pads
    # nearest each corner hung 0.07-0.20 mm out into bare silicon, and
    # one corner pad reached past the die outline itself. United with the
    # ground plane that reads as four ragged, notched corners, and the
    # wire on the overhanging pad lands on substrate, not metal.
    end_limit = copper_half_s - pad_w / 2.0

    # The side rows stop a full pitch short of the end rows' line. Bound
    # only by the copper edge they would still reach x = 2.85 while the
    # end pads span 2.70..3.00 -- overlapping pads at all four corners,
    # which is the same ragged outline by another route.
    side_limit = min(chip_half_l - inset - pitch, copper_half_l - inset / 2.0)

    # --- the two feedline ends -----------------------------------------
    for sign in (-1.0, 1.0):
        pcb_x = sign * (chip_half_l + span)
        chip_x = sign * (chip_half_l - inset)

        # Signal: two wires side by side, both on the centre conductor --
        # the board's at the board end, the die's at the die end.
        offset = trace / 4.0
        chip_offset = die_trace / 4.0
        for wire in (-1.0, 1.0):
            bonds.append(Bond(SIGNAL, pcb_x, wire * offset, chip_x, wire * chip_offset))

        # Ground: out beyond the gap, then every pitch toward the corner.
        first = ground_bond_start(values, gap, chip_gap) + pad_w / 2.0
        for y in _frange(first, end_limit, pitch):
            for side in (-1.0, 1.0):
                bonds.append(Bond(GROUND, pcb_x, side * y, chip_x, side * y))

    # --- the two long sides --------------------------------------------
    # Nothing here is near the line, so these are ground the whole way.
    for sign in (-1.0, 1.0):
        pcb_y = sign * (chip_half_s + span)
        chip_y = sign * (chip_half_s - inset)
        for x in _row(side_limit, pitch):
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
