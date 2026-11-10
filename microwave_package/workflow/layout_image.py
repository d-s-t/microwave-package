"""
A top view of a run's real layout, drawn from its own copper, so that a TDR
result can be read against the geometry it came from.

    python -m microwave_package.workflow.layout_image <run dir | copper.txt> --out layout.png

Read-only. Pure numpy and matplotlib (Agg, drawn without pyplot). Inputs are the
run's own files: `models/<combo>/copper.txt` (required), `dimensions.txt` (the board
outline and die size), `bonds.txt` (the wire bonds). Nothing is built in AEDT.

What is drawn
-------------
* the board outline (rounded rectangle from `pcb_length`, `pcb_side`, `pcb_corner_radius`),
* top-layer ground copper: pours, regions, ground pads, and the die's ground,
* signal copper: the signal tracks and the pads they reach, identified by connectivity
  from the port pad (see `signal_copper`), not by guessing from shape,
* vias as dots of their own diameter, the die outline, the wire bonds (signal and ground),
* the signal route (the centre line from port 1 to port 2) with arc-length ticks every 5 mm
  from port 1, and the landmarks: launch lands, bond fingers, die edge.

The bottom layer is not drawn: this is the view from above.

Colouring the route by a value
------------------------------
`paint_route(ax, route_xy, values, cmap, norm)` colours the route segments by a value at
each route point. With `norm = TwoSlopeNorm(vmin, vcenter=50, vmax)` and a diverging
colormap the centre is 50 ohm: red above, blue below. `values_along_route` maps a profile
given against distance along the line (the TDR's x) onto the route points by arc length.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..design import bonds as bonds_mod
from ..design import copper_geometry as cg
from ..design import equations, pcb_outline

Point = Tuple[float, float]

Z0_OHM = 50.0
TICK_MM = 5.0
TRACK_JOIN_MM = 0.01  # two track ends this close are one node
PAD_TOUCH_MM = 0.15  # a track end this close to a pad is connected to it (the bond fingers sit 0.1 mm off the track ends)
BOND_MATCH_MM = 0.1  # a bond end lands this close to a pad (or it does not connect)

GROUND_RGB = "#c3ccd6"
GROUND_PAD_RGB = "#93a4b8"
SIGNAL_RGB = "#d9781f"
ROUTE_RGB = "#a1002c"
VIA_RGB = "#5e6b78"
BOARD_RGB = "#f6f3ea"
DIE_RGB = "#fbf7e6"


@dataclass(frozen=True)
class Landmark:
    label: str
    x: float
    y: float


@dataclass
class Layout:
    """Everything the picture is drawn from, in board millimetres."""
    name: str
    package: cg.BoardCopper
    chip: Optional[cg.BoardCopper]
    bonds: List[bonds_mod.Bond]
    outline: List[Point]
    die_half: Optional[float]
    route: List[Point]  # the centre line, port 1 to port 2
    signal_tracks: List[cg.Track]
    signal_pads: List[Tuple[str, cg.Pad]]  # (board name, pad)
    port1: Point
    port2: Point
    landmarks: List[Landmark] = field(default_factory=list)
    label: str = ""

    @property
    def length_mm(self) -> float:
        return float(route_arc_lengths(self.route)[-1])


# --------------------------------------------------------------- reading


def _resolve(path: str) -> Tuple[str, str]:
    """(copper.txt, its folder) for a run folder or a copper.txt path."""
    if os.path.isfile(path):
        return path, os.path.dirname(os.path.abspath(path))
    models = os.path.join(path, "models")
    if os.path.isdir(models):
        combos = sorted(d for d in os.listdir(models) if os.path.isfile(os.path.join(models, d, "copper.txt")))
        if "default" in combos:
            combos = ["default"]
        if len(combos) != 1:
            raise FileNotFoundError(
                f"{path}: {len(combos)} combinations have a copper.txt ({', '.join(combos) or 'none'}); "
                "give the combination's folder or its copper.txt")
        folder = os.path.join(models, combos[0])
        return os.path.join(folder, "copper.txt"), folder
    candidate = os.path.join(path, "copper.txt")
    if os.path.isfile(candidate):
        return candidate, path
    raise FileNotFoundError(f"no copper.txt under {path}")


def _dimensions(folder: str) -> Dict[str, float]:
    path = os.path.join(folder, "dimensions.txt")
    if not os.path.isfile(path):
        return {}
    return equations.get_numeric_parameters(equations.read_file(path))


def _run_name(folder: str) -> str:
    """The run's folder name for models/<combo>/, else the folder's own name."""
    parts = os.path.normpath(os.path.abspath(folder)).split(os.sep)
    if len(parts) >= 3 and parts[-2] == "models":
        return parts[-3]
    return parts[-1] or "layout"


def _label(folder: str) -> str:
    try:
        with open(os.path.join(folder, "..", "..", "run_config.json"), encoding="utf-8") as f:
            return str(json.load(f).get("label", "") or "")
    except (OSError, ValueError):
        return ""


def read_layout(path: str) -> Layout:
    """The layout of a run folder (or of one copper.txt) ready to draw."""
    copper_path, folder = _resolve(path)
    boards = cg.read_file(copper_path)
    if "package" not in boards:
        raise ValueError(f"{copper_path}: no 'board=package' section")
    package = boards["package"]
    chip = boards.get("chip")
    dims = _dimensions(folder)
    bond_path = os.path.join(folder, "bonds.txt")
    bond_list = bonds_mod.read_file(bond_path) if os.path.isfile(bond_path) else []

    outline = _outline(dims, package)
    die_half = _die_half(dims, chip)
    tracks, pads, route, port1, port2 = signal_copper(package, chip, bond_list)

    layout = Layout(
        name=_run_name(folder),
        package=package, chip=chip, bonds=list(bond_list), outline=outline, die_half=die_half,
        route=route, signal_tracks=tracks, signal_pads=pads, port1=port1, port2=port2,
        label=_label(folder),
    )
    layout.landmarks = _landmarks(layout)
    return layout


def _outline(dims: Dict[str, float], package: cg.BoardCopper) -> List[Point]:
    if "pcb_length" in dims and "pcb_side" in dims:
        radius = pcb_outline.corner_radius_of(dims)
        return pcb_outline.outline_points(dims["pcb_length"] / 2.0, dims["pcb_side"] / 2.0, radius)
    # No dimensions file: the copper's own extent is the best available outline.
    xs = [p[0] for r in package.regions for p in r.points]
    ys = [p[1] for r in package.regions for p in r.points]
    if not xs:
        return []
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _die_half(dims: Dict[str, float], chip: Optional[cg.BoardCopper]) -> Optional[float]:
    if chip is None:
        return None
    if "chip_length" in dims:
        return dims["chip_length"] / 2.0
    xs = [abs(p[0]) for r in chip.regions for p in r.points]
    return max(xs) if xs else None


# ----------------------------------------------------------- connectivity


def _pad_gap(pad: cg.Pad, x: float, y: float) -> float:
    """Distance from a point to a pad's footprint (0 when it is inside)."""
    a = math.radians(pad.rotation)
    dx, dy = x - pad.x, y - pad.y
    u = dx * math.cos(a) + dy * math.sin(a)
    v = -dx * math.sin(a) + dy * math.cos(a)
    return math.hypot(max(abs(u) - pad.xsize / 2.0, 0.0), max(abs(v) - pad.ysize / 2.0, 0.0))


def _ends(track: cg.Track) -> Tuple[Point, Point]:
    return (track.x1, track.y1), (track.x2, track.y2)


def _nearest_pad(pads: Sequence[cg.Pad], x: float, y: float, reach: float) -> Optional[int]:
    best, best_gap = None, reach
    for index, pad in enumerate(pads):
        gap = _pad_gap(pad, x, y)
        if gap <= best_gap + 1e-12:
            if best is None or gap < best_gap - 1e-12 or (
                    abs(gap - best_gap) <= 1e-12 and math.hypot(pad.x - x, pad.y - y)
                    < math.hypot(pads[best].x - x, pads[best].y - y)):
                best, best_gap = index, gap
    return best


def signal_copper(package: cg.BoardCopper, chip: Optional[cg.BoardCopper],
                  bond_list: Sequence[bonds_mod.Bond]):
    """The signal path, found by connectivity from the port pads.

    Nodes are the top-layer tracks and pads of both boards. Two tracks join when
    their ends meet; a track joins a pad when an end is within PAD_TOUCH_MM of its
    footprint; a SIGNAL bond joins the package pad at its PCB end to the chip pad at
    its die end. Ground bonds are not linked: they are not part of the signal, and
    linking them would merge the ground network into it.

    The ports are the outermost package pads that the first package track reaches,
    so ground lands that touch no track (the SMP ground pads) are never ports.

    Returns (tracks, pads, route, port1, port2): the signal tracks and pads the port
    reaches, the centre line from port 1 to port 2 as points, and the two ports.
    """
    boards = [("package", package)] + ([("chip", chip)] if chip is not None else [])
    nodes: List[Tuple[str, str, object]] = []  # (board, kind, item)
    for name, copper in boards:
        for track in copper.tracks:
            if track.layer == "top":
                nodes.append((name, "track", track))
        for pad in copper.pads:
            if pad.layer == "top":
                nodes.append((name, "pad", pad))
    adjacency: Dict[int, set] = {i: set() for i in range(len(nodes))}

    def link(i: int, j: int) -> None:
        adjacency[i].add(j)
        adjacency[j].add(i)

    for i, (board_i, kind_i, item_i) in enumerate(nodes):
        if kind_i != "track":
            continue
        for j in range(i + 1, len(nodes)):
            board_j, kind_j, item_j = nodes[j]
            if board_j != board_i:
                continue
            if kind_j == "track":
                touch = any(
                    math.hypot(a[0] - b[0], a[1] - b[1]) <= TRACK_JOIN_MM
                    for a in _ends(item_i) for b in _ends(item_j)
                )
            else:
                touch = min(_pad_gap(item_j, x, y) for x, y in _ends(item_i)) <= PAD_TOUCH_MM
            if touch:
                link(i, j)

    index_of = {id(item): i for i, (_, _, item) in enumerate(nodes)}
    package_pads = [item for name, kind, item in nodes if name == "package" and kind == "pad"]
    chip_pads = [item for name, kind, item in nodes if name == "chip" and kind == "pad"]
    for bond in bond_list:
        if bond.kind != "signal":
            continue
        p = _nearest_pad(package_pads, bond.pcb_x, bond.pcb_y, BOND_MATCH_MM)
        q = _nearest_pad(chip_pads, bond.chip_x, bond.chip_y, BOND_MATCH_MM)
        if p is not None and q is not None:
            link(index_of[id(package_pads[p])], index_of[id(chip_pads[q])])

    package_tracks = [i for i, (name, kind, _) in enumerate(nodes) if name == "package" and kind == "track"]
    if not package_tracks:
        raise ValueError("no top-layer signal track on the package board")
    start = package_tracks[0]
    reached = _reach(adjacency, start)
    port_candidates = [i for i in reached if nodes[i][0] == "package" and nodes[i][1] == "pad"]
    if len(port_candidates) < 2:
        raise ValueError("the signal track does not reach a pad at each end")
    port1_i = min(port_candidates, key=lambda i: (nodes[i][2].x, abs(nodes[i][2].y)))
    port2_i = max(port_candidates, key=lambda i: (nodes[i][2].x, -abs(nodes[i][2].y)))
    path = _path(adjacency, port1_i, port2_i)
    if path is None:
        raise ValueError("no signal path between the two ports")

    tracks = [nodes[i][2] for i in sorted(reached) if nodes[i][1] == "track"]
    pads = [(nodes[i][0], nodes[i][2]) for i in sorted(reached) if nodes[i][1] == "pad"]
    port1_pad, port2_pad = nodes[port1_i][2], nodes[port2_i][2]
    route: List[Point] = [(port1_pad.x, port1_pad.y)]
    for i in path:
        name, kind, item = nodes[i]
        if kind != "track":
            continue
        a, b = _ends(item)
        last = route[-1]
        entry, exit_ = (a, b) if math.dist(a, last) <= math.dist(b, last) else (b, a)
        # Every track end is a vertex, and the gap from one track to the next (the
        # bond finger, the die's bond) is a straight run between them.
        if math.dist(entry, last) > TRACK_JOIN_MM:
            route.append(entry)
        route.append(exit_)
    if math.dist(route[-1], (port2_pad.x, port2_pad.y)) > TRACK_JOIN_MM:
        route.append((port2_pad.x, port2_pad.y))
    return tracks, pads, route, (port1_pad.x, port1_pad.y), (port2_pad.x, port2_pad.y)


def _reach(adjacency: Dict[int, set], start: int) -> set:
    seen = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for other in adjacency[node]:
            if other not in seen:
                seen.add(other)
                queue.append(other)
    return seen


def _path(adjacency: Dict[int, set], start: int, goal: int) -> Optional[List[int]]:
    parent = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node == goal:
            out = []
            while node is not None:
                out.append(node)
                node = parent[node]
            return list(reversed(out))
        for other in adjacency[node]:
            if other not in parent:
                parent[other] = node
                queue.append(other)
    return None


# ------------------------------------------------------------ the route


def route_arc_lengths(route_xy) -> np.ndarray:
    """Distance along the route from its first point, one value per point."""
    pts = np.asarray(route_xy, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < 1:
        raise ValueError("route_xy must be (N, 2) with N >= 1")
    steps = np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1]))
    return np.concatenate([[0.0], np.cumsum(steps)])


def values_along_route(route_xy, positions_mm, values, offset_mm: float = 0.0) -> np.ndarray:
    """A profile given against distance along the line, sampled at the route points.

    `positions_mm` are the profile's distances (the TDR's x); `offset_mm` is the distance from
    the profile's reference plane to port 1 of the route, so that route arc length s is at
    profile position s + offset_mm. Points beyond the profile's ends take the end values.
    """
    arc = route_arc_lengths(route_xy)
    xs = np.asarray(positions_mm, dtype=float)
    ys = np.asarray(values, dtype=float)
    if xs.shape != ys.shape or xs.ndim != 1 or len(xs) < 1:
        raise ValueError("positions_mm and values must be equal-length 1-D arrays")
    order = np.argsort(xs)
    return np.interp(arc + offset_mm, xs[order], ys[order])


def paint_route(ax, route_xy, values, cmap, norm, linewidth: float = 3.0, zorder: float = 6.0):
    """Colour the route's segments by a value at each route point.

    `values` has one entry per point of `route_xy`; each segment takes the mean of its two
    end values, and a NaN end leaves its segment unpainted. `cmap` and `norm` are as in
    matplotlib (for a diverging scale about 50 ohm use `TwoSlopeNorm(vmin, vcenter=50, vmax)`).
    Returns the LineCollection, which is the mappable for a colour bar.
    """
    from matplotlib.collections import LineCollection

    pts = np.asarray(route_xy, dtype=float)
    vals = np.asarray(values, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < 2:
        raise ValueError("route_xy must be (N, 2) with N >= 2")
    if vals.shape != (len(pts),):
        raise ValueError("values must have one entry per route point")
    segments = np.stack([pts[:-1], pts[1:]], axis=1)
    mids = np.ma.masked_invalid(0.5 * (vals[:-1] + vals[1:]))
    colours = _cmap_with_gaps(cmap)
    collection = LineCollection(segments, cmap=colours, norm=norm, linewidths=linewidth,
                                zorder=zorder, capstyle="round", joinstyle="round")
    collection.set_array(mids)
    ax.add_collection(collection)
    return collection


def _cmap_with_gaps(cmap):
    import matplotlib

    colours = matplotlib.colormaps[cmap] if isinstance(cmap, str) else cmap
    # A NaN value leaves its segment unpainted: the colour map's "bad" colour is transparent.
    return colours.with_extremes(bad=(0.0, 0.0, 0.0, 0.0))


def _landmarks(layout: Layout) -> List[Landmark]:
    out: List[Landmark] = []
    for x, y in (layout.port1, layout.port2):
        out.append(Landmark("launch (SMP land)", x, y))
    for sign in (-1.0, 1.0):
        fingers = [pad.x for name, pad in layout.signal_pads
                   if name == "package" and 3.0 < abs(pad.x) < 4.0 and sign * pad.x > 0]
        if fingers:
            out.append(Landmark("bond fingers", sum(fingers) / len(fingers), 0.0))
    if layout.die_half is not None:
        out.append(Landmark("die edge", -layout.die_half, 0.0))
        out.append(Landmark("die edge", layout.die_half, 0.0))
    return out


# -------------------------------------------------------------- drawing


def _rect_corners(x1, y1, x2, y2, width) -> List[Point]:
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        return []
    nx, ny = -dy / length * width / 2.0, dx / length * width / 2.0
    return [(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)]


def render(layout: Layout, out_path: str, *, profile=None, cmap="RdBu_r", norm=None,
           title: Optional[str] = None, dpi: int = 150) -> str:
    """Draw the layout to a PNG. `profile` = (positions_mm, z_ohm, offset_mm) paints the route."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.collections import EllipseCollection, LineCollection, PolyCollection
    from matplotlib.colors import TwoSlopeNorm
    from matplotlib.figure import Figure
    from matplotlib.patches import Patch, Polygon, Rectangle

    fig = Figure(figsize=(12.0, 7.4), dpi=dpi, facecolor="white")
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0.055, 0.085, 0.93, 0.815])
    ax.set_aspect("equal")
    ax.set_facecolor("white")

    if layout.outline:
        ax.add_patch(Polygon(layout.outline, closed=True, facecolor=BOARD_RGB,
                             edgecolor="#333333", linewidth=1.2, zorder=1))
    if layout.die_half is not None:
        ax.add_patch(Rectangle((-layout.die_half, -layout.die_half), 2 * layout.die_half,
                               2 * layout.die_half, facecolor=DIE_RGB, edgecolor="#333333",
                               linewidth=0.8, linestyle="--", zorder=1.5))

    signal_ids = {id(pad) for _, pad in layout.signal_pads}
    signal_track_ids = {id(t) for t in layout.signal_tracks}

    ground_regions = _top_regions(layout)
    if ground_regions:
        ax.add_collection(PolyCollection(ground_regions, facecolors=GROUND_RGB, edgecolors="none", zorder=2))

    ground_pads, signal_pad_polys = [], []
    for copper in (layout.package, layout.chip):
        if copper is None:
            continue
        for pad in copper.pads:
            if pad.layer != "top":
                continue
            if id(pad) in signal_ids:
                signal_pad_polys.append(pad.corners())
            else:
                ground_pads.append(pad.corners())
    if ground_pads:
        ax.add_collection(PolyCollection(ground_pads, facecolors=GROUND_PAD_RGB, edgecolors="none", zorder=2.5))
    if signal_pad_polys:
        ax.add_collection(PolyCollection(signal_pad_polys, facecolors=SIGNAL_RGB, edgecolors="none", zorder=3))

    signal_tracks = []
    for copper in (layout.package, layout.chip):
        if copper is None:
            continue
        for track in copper.tracks:
            if track.layer != "top":
                continue
            corners = _rect_corners(track.x1, track.y1, track.x2, track.y2, track.width)
            if not corners:
                continue
            if id(track) in signal_track_ids:
                signal_tracks.append(corners)
            else:
                ax.add_collection(PolyCollection([corners], facecolors=GROUND_PAD_RGB, edgecolors="none", zorder=2.5))
    if signal_tracks:
        ax.add_collection(PolyCollection(signal_tracks, facecolors=SIGNAL_RGB, edgecolors="none", zorder=3.2))

    vias = layout.package.vias
    if vias:
        xy = np.array([(v.x, v.y) for v in vias])
        dia = np.array([v.diameter for v in vias])
        ax.add_collection(EllipseCollection(dia, dia, np.zeros(len(vias)), units="xy", offsets=xy,
                                            offset_transform=ax.transData, facecolors=VIA_RGB,
                                            edgecolors="none", zorder=4))
        hole = np.array([v.hole for v in vias])
        ax.add_collection(EllipseCollection(hole, hole, np.zeros(len(vias)), units="xy", offsets=xy,
                                            offset_transform=ax.transData, facecolors="white",
                                            edgecolors="none", zorder=4.1))

    if layout.bonds:
        segments, colours = [], []
        for bond in layout.bonds:
            segments.append([(bond.pcb_x, bond.pcb_y), (bond.chip_x, bond.chip_y)])
            colours.append(SIGNAL_RGB if bond.kind == "signal" else "#4a5561")
        ax.add_collection(LineCollection(segments, colors=colours, linewidths=0.8, zorder=5))

    route = np.asarray(layout.route, dtype=float)
    if profile is not None:
        positions, z_values, offset = profile
        values = values_along_route(route, positions, z_values, offset_mm=offset)
        norm = norm or TwoSlopeNorm(vmin=min(float(np.min(z_values)), Z0_OHM - 1e-6),
                                    vcenter=Z0_OHM, vmax=max(float(np.max(z_values)), Z0_OHM + 1e-6))
        collection = paint_route(ax, route, values, cmap, norm)
        cbar_ax = fig.add_axes([0.30, 0.02, 0.40, 0.022])
        fig.colorbar(collection, cax=cbar_ax, orientation="horizontal", label="impedance, ohm (centre 50)")
    else:
        ax.plot(route[:, 0], route[:, 1], color=ROUTE_RGB, linewidth=1.8, zorder=6, solid_capstyle="round")

    _draw_ticks(ax, route, layout.length_mm)
    _draw_landmarks(ax, layout)

    xlim = (-27.0, 27.0)
    ylim = (-14.6, 14.6)
    if layout.outline:
        xs = [p[0] for p in layout.outline]
        ys = [p[1] for p in layout.outline]
        xlim = (min(xs) - 2.0, max(xs) + 2.0)
        ylim = (min(ys) - 2.6, max(ys) + 2.0)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_xlabel("x along the feedline, mm from the board centre")
    ax.set_ylabel("y, mm")
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_linewidth(0.6)

    _draw_scale_bar(ax, xlim, ylim)
    legend = [
        Patch(facecolor=GROUND_RGB, label="ground copper (top)"),
        Patch(facecolor=SIGNAL_RGB, label="signal copper (tracks, pads, bonds)"),
        Patch(facecolor=VIA_RGB, label="via (hole in white)"),
        Patch(facecolor=ROUTE_RGB, label="signal route, port 1 to port 2"),
    ]
    ax.legend(handles=legend, loc="upper right", fontsize=7.5, framealpha=0.92)

    heading = title or layout.name
    sub = f"{layout.label}  " if layout.label else ""
    fig.suptitle(f"{heading}", fontsize=12, x=0.055, ha="left", y=0.975)
    fig.text(0.055, 0.935, f"{sub}top view; signal route {layout.length_mm:.2f} mm port to port"
             f" (launch to launch {abs(layout.port2[0] - layout.port1[0]):.2f} mm along x)",
             fontsize=8.5, color="#333333")

    directory = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(directory, exist_ok=True)
    fig.savefig(out_path, dpi=dpi, facecolor="white")
    return out_path


def _top_regions(layout: Layout):
    out = []
    for copper in (layout.package, layout.chip):
        if copper is None:
            continue
        for region in copper.regions:
            if region.layer == "top":
                out.append(list(region.points))
    return out


def _draw_ticks(ax, route, length_mm: float) -> None:
    arc = route_arc_lengths(route)
    total = float(arc[-1])
    s = 0.0
    while s <= total + 1e-9:
        x, y, dx, dy = _point_at(route, arc, s)
        n = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / n, dx / n
        ax.plot([x - 0.9 * nx, x + 0.9 * nx], [y - 0.9 * ny, y + 0.9 * ny],
                color=ROUTE_RGB, linewidth=1.2, zorder=7)
        ax.text(x + 1.25 * nx, y + 1.25 * ny, f"{s:g}", fontsize=6.5, color=ROUTE_RGB,
                ha="center", va="center", zorder=8)
        s += TICK_MM
    ax.text(route[0, 0] + 0.4, route[0, 1] - 1.9, "s, mm from port 1", fontsize=6.5, color=ROUTE_RGB,
            ha="left", va="center", bbox=dict(facecolor="white", edgecolor="none", pad=0.6, alpha=0.9), zorder=9)


def _point_at(route, arc, s: float):
    i = int(np.searchsorted(arc, s, side="right")) - 1
    i = min(max(i, 0), len(route) - 2)
    span = arc[i + 1] - arc[i]
    t = 0.0 if span == 0 else (s - arc[i]) / span
    p = route[i] + t * (route[i + 1] - route[i])
    d = route[i + 1] - route[i]
    return float(p[0]), float(p[1]), float(d[0]), float(d[1])


def _draw_landmarks(ax, layout: Layout) -> None:
    for landmark in layout.landmarks:
        if landmark.label.startswith("launch"):
            ax.annotate(landmark.label, xy=(landmark.x, landmark.y), xytext=(landmark.x, 4.6),
                        fontsize=7, ha="center", va="bottom", color="#222222",
                        arrowprops=dict(arrowstyle="-", color="#555555", lw=0.6))
        elif landmark.label == "bond fingers":
            ax.annotate("bond fingers", xy=(landmark.x, landmark.y), xytext=(landmark.x, -4.6),
                        fontsize=7, ha="center", va="top", color="#222222",
                        arrowprops=dict(arrowstyle="-", color="#555555", lw=0.6))
        elif landmark.label == "die edge":
            ax.annotate("die edge", xy=(landmark.x, landmark.y + 0.6 * landmark.x / abs(landmark.x)),
                        xytext=(landmark.x, 6.6), fontsize=7, ha="center", va="bottom", color="#222222",
                        arrowprops=dict(arrowstyle="-", color="#555555", lw=0.6))


def _draw_scale_bar(ax, xlim, ylim) -> None:
    x0 = xlim[0] + 1.5
    y0 = ylim[0] + 1.2
    ax.plot([x0, x0 + TICK_MM], [y0, y0], color="#111111", linewidth=2.2, zorder=9, solid_capstyle="butt")
    ax.text(x0 + TICK_MM / 2.0, y0 - 0.45, "5 mm", fontsize=7, ha="center", va="top", color="#111111")


# ------------------------------------------------------------------- CLI


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Draw a run's real layout from its copper.txt.")
    parser.add_argument("path", help="a run folder, a combination folder, or a copper.txt")
    parser.add_argument("--out", help="PNG to write (default: layout.png beside the copper.txt)")
    parser.add_argument("--title", help="heading for the picture (default: the run's folder name)")
    args = parser.parse_args(argv)
    try:
        layout = read_layout(args.path)
    except (OSError, ValueError) as exc:
        print(f"layout_image: {exc}", file=sys.stderr)
        return 2
    copper_path, folder = _resolve(args.path)
    out = args.out or os.path.join(folder, "layout.png")
    render(layout, out, title=args.title)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
